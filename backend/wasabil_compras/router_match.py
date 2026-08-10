"""API del matcher banco ↔ libro SII ↔ egresos — prefijo /api/sii-libro/match.

Mismo estilo (y mismo candado 'mineria') que el router hermano del libro. Router
PROPIO: el caller lo monta en main.py — este módulo no toca main ni el router
existente.

LO QUE PROTEGEN ESTOS ENDPOINTS (spec, reglas 2 y 3):
  · Confirmar NO es un UPDATE de estado: es la MISMA transacción con candado que la
    conciliación interna — SELECT…FOR UPDATE + populate_existing del movimiento,
    revalidación COMPLETA adentro (doc vivo, Σ topes frescos, hash del doc) y retry
    de deadlock. El score guardado es cache de pantalla, JAMÁS autorización.
  · Confirmar enciende mov.conciliado: con eso el guard EXISTENTE de Tesorería
    responde 409 al DELETE del movimiento/cartola (regla 5) y a una conciliación
    interna contradictoria posterior (carrera de la regla 3) — sin tocar ese router.
  · Un grupo (subset factura−NC, agregado mensual) se confirma o descarta ATÓMICO.
"""
import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from empresa_guard import require_empresa
from models.models import User

from .matcher import (
    ERROR_FENCING, MatcherEnCurso, TOL_PESO, _con_retry_deadlock, _p, cargar_config,
    correr_matcher, docs_no_bancarios,
)
from .models import SiiLibroDoc, SiiLibroMatch, SiiMatchEtiquetaMov, SiiMatchRun
from .rut import rut_formateado

logger = logging.getLogger("wasabil_compras")

router = APIRouter(
    prefix="/api/sii-libro/match",
    tags=["sii-libro-match"],
    dependencies=[Depends(require_empresa("mineria"))],
)

ESTADOS_BANDEJA = ("sugerido", "en_duda", "conflicto")
# Lo que la bandeja puede SERVIR si se lo piden explícitamente. Es un superconjunto de
# ESTADOS_BANDEJA, que sigue siendo lo que se lista por defecto (los que piden decisión).
#
# Hallazgo H2 (lente de usabilidad 2026-08-08, CRÍTICO): Tesorería frena el borrado de una
# cartola o de un movimiento diciendo «el match #412 está CONFIRMADO: deshágalo en el Libro
# SII», pero un confirmado no aparecía en NINGUNA pantalla — ni acá ni en Tesorería. El
# operador quedaba en un punto muerto: la cartola no se podía borrar y el movimiento no se
# podía desconciliar. El deshacer ya existía (POST /{id}/descartar libera el flag
# `conciliado`); lo que faltaba era poder VER la fila para apretarlo.
ESTADOS_CONSULTABLES = ESTADOS_BANDEJA + ("confirmado",)


class DescartarIn(BaseModel):
    motivo: Optional[str] = Field(None, max_length=400)


# ─── Helpers ──────────────────────────────────────────────────────────────────────

def _match_scoped(db: Session, match_id: int) -> SiiLibroMatch:
    m = (db.query(SiiLibroMatch)
         .filter(SiiLibroMatch.id == match_id,
                 SiiLibroMatch.empresa == "mineria").first())
    if not m:
        raise HTTPException(404, "Match no encontrado")
    return m


def _grupo_de(db: Session, m: SiiLibroMatch) -> list:
    """Las filas nacidas juntas (grupo_uuid) se deciden ATÓMICAS: confirmar la mitad
    de un combo factura−NC dejaría la otra mitad colgando sin sentido contable."""
    if not m.grupo_uuid:
        return [m]
    return (db.query(SiiLibroMatch)
            .filter(SiiLibroMatch.grupo_uuid == m.grupo_uuid)
            .order_by(SiiLibroMatch.id).all())


def _mov_lock(db: Session, mov_id: int):
    """CALCA _mov_scoped lock=True (tesoreria/router.py:108-121): LECTURA DE PLATA
    bajo lock → populate_existing obligatorio (el identity map serviría un monto
    viejo aunque el FOR UPDATE serialice)."""
    from tesoreria.models import MovimientoBancario
    mov = (db.query(MovimientoBancario)
           .filter(MovimientoBancario.id == mov_id,
                   MovimientoBancario.empresa == "mineria")
           .populate_existing().with_for_update().first())
    if not mov:
        raise HTTPException(404, "Movimiento bancario no encontrado")
    return mov


def _solo_cuentas_clp(cuenta) -> None:
    """Mismo criterio y mensaje que tesoreria/router.py:221-226 (regla 21)."""
    if cuenta is not None and (cuenta.moneda or "CLP") != "CLP":
        raise HTTPException(400, f"La cuenta está en {cuenta.moneda}: la conciliación "
                                 "automática solo está disponible para cuentas en CLP "
                                 "(fase futura)")


def _enlaces_internos(db: Session, mov_id: int) -> tuple:
    from tesoreria.models import Conciliacion, ConciliacionIngreso
    egresos = db.query(Conciliacion).filter(
        Conciliacion.movimiento_id == mov_id).all()
    ingresos = db.query(ConciliacionIngreso.id).filter(
        ConciliacionIngreso.movimiento_id == mov_id).first()
    return egresos, ingresos


def _serializar(db: Session, filas: list) -> list:
    doc_ids = {m.doc_id for m in filas}
    docs = {d.id: d for d in db.query(SiiLibroDoc)
            .filter(SiiLibroDoc.id.in_(doc_ids)).all()} if doc_ids else {}
    mov_ids = [m.movimiento_id for m in filas if m.movimiento_id is not None]
    movs = {}
    if mov_ids:
        from tesoreria.models import MovimientoBancario
        movs = {mv.id: mv for mv in db.query(MovimientoBancario)
                .filter(MovimientoBancario.id.in_(mov_ids)).all()}
    out = []
    for m in filas:
        d = docs.get(m.doc_id)
        mv = movs.get(m.movimiento_id)
        try:
            motivo = json.loads(m.motivo or "[]")
        except (ValueError, TypeError):
            motivo = [str(m.motivo)]
        try:
            candidatos = (json.loads(m.candidatos_evaluados or "{}")
                          .get("candidatos", []))
        except (ValueError, TypeError):
            candidatos = []
        out.append({
            "id": m.id, "estado": m.estado, "via": m.via, "origen": m.origen,
            "score": m.score, "grupo_uuid": m.grupo_uuid,
            "monto_asignado": float(m.monto_asignado or 0),
            "motivo": motivo, "candidatos": candidatos,
            "divergente": m.divergente, "divergencia_detalle": m.divergencia_detalle,
            "decidido_at": m.decidido_at.isoformat() if m.decidido_at else None,
            "doc": {
                "id": d.id, "uuid": d.uuid, "folio": d.folio,
                "tipo": d.sii_document_type_id,
                "fecha": d.document_date.isoformat() if d.document_date else None,
                "rut": d.rut_emisor_canonico,
                "rut_formateado": rut_formateado(d.rut_emisor_canonico),
                "emisor": d.receiver_name,
                "monto_efectivo": float(d.monto_efectivo or 0),
                "estado_espejo": d.estado_espejo,
            } if d else None,
            "movimiento": {
                "id": mv.id, "fecha": mv.fecha.isoformat() if mv.fecha else None,
                "glosa": mv.glosa, "tipo": mv.tipo,
                "monto": float(mv.monto or 0), "referencia": mv.referencia,
                "conciliado": mv.conciliado,
            } if mv else None,
        })
    return out


# ─── Endpoints ────────────────────────────────────────────────────────────────────

@router.post("/correr")
def correr(db: Session = Depends(get_db),
           current_user: User = Depends(get_current_user)):
    """Corrida MANUAL (botón). El motor además corre solo: tras cada barrido nocturno
    exitoso y al importar una cartola. 409 si hay barrido del espejo (u otra corrida) en curso —
    el motor jamás compite con la transacción larga del barrido (regla 22)."""
    try:
        run = correr_matcher(db, origen="manual",
                             usuario_id=getattr(current_user, "id", None))
    except MatcherEnCurso as e:
        raise HTTPException(409, str(e))
    return {
        "exito": run.exito, "error": run.error,
        "sugeridos_nuevos": run.sugeridos_nuevos, "autos_nuevos": run.autos_nuevos,
        "conflictos": run.conflictos, "degradados_en_duda": run.degradados_en_duda,
        "caducados": run.caducados,
    }


@router.get("/pendientes")
def pendientes(
    estado: Optional[str] = Query(
        None, description="sugerido | en_duda | conflicto | confirmado"),
    match_id: Optional[int] = Query(
        None, description="N° exacto del match (el que nombran los 409 de Tesorería)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """La bandeja: sugerencias, dudas y conflictos con sus motivos LEGIBLES y los
    candidatos evaluados (por qué fue único / quién compite), paginado.

    `estado=confirmado` (H2) lista los YA conciliados para poder deshacerlos; hay que
    pedirlo explícitamente — el default sigue siendo solo lo que espera decisión, que es
    el trabajo del día. `match_id` va con él: el 409 de Tesorería nombra un número y con
    cientos de conciliados no se puede paginar a ciegas buscándolo.
    """
    q = db.query(SiiLibroMatch).filter(SiiLibroMatch.empresa == "mineria")
    if estado:
        if estado not in ESTADOS_CONSULTABLES:
            raise HTTPException(400, f"Estado inválido '{estado}': usa "
                                     f"{' | '.join(ESTADOS_CONSULTABLES)}")
        q = q.filter(SiiLibroMatch.estado == estado)
    else:
        q = q.filter(SiiLibroMatch.estado.in_(ESTADOS_BANDEJA))
    if match_id is not None:
        q = q.filter(SiiLibroMatch.id == match_id)
    total = q.count()
    # score DESC: en MySQL los NULL (conflictos, sin score) quedan al final — el
    # orden del operador es «lo más seguro primero».
    filas = (q.order_by(SiiLibroMatch.score.desc(), SiiLibroMatch.id.desc())
             .offset((page - 1) * page_size).limit(page_size).all())
    return {"items": _serializar(db, filas), "total": total,
            "page": page, "page_size": page_size}


@router.post("/{match_id}/confirmar")
def confirmar(match_id: int, db: Session = Depends(get_db),
              current_user: User = Depends(get_current_user)):
    """Confirmación HUMANA bajo el candado del movimiento (regla 3): revalida el doc
    vivo, los Σ topes frescos (regla 1: jamás dos confirmaciones que sumen más que el
    movimiento o que el documento) y la coherencia de la conciliación interna. El
    grupo se confirma ATÓMICO."""
    m = _match_scoped(db, match_id)
    if m.estado not in ("sugerido", "en_duda", "conflicto"):
        raise HTTPException(409, f"El match está '{m.estado}': solo se confirma un "
                                 "sugerido, un en_duda o un conflicto")
    if m.movimiento_id is None:
        raise HTTPException(409, "El movimiento de este match fue eliminado: no hay "
                                 "qué confirmar (re-importe la cartola primero)")
    grupo = _grupo_de(db, m)
    uid = getattr(current_user, "id", None)

    def _tx():
        # Orden global de locks: movimientos por id ASCENDENTE (regla 3); los docs
        # JAMÁS se lockean (regla 22) — su tope se revalida fresco bajo el lock del
        # movimiento.
        mov_ids = sorted({g.movimiento_id for g in grupo if g.movimiento_id})
        if len(mov_ids) != len({g.movimiento_id for g in grupo}):
            raise HTTPException(409, "El grupo tiene filas sin movimiento (cartola "
                                     "borrada): re-córralo antes de confirmar")
        movs = {}
        for mid in mov_ids:
            mv = _mov_lock(db, mid)
            _solo_cuentas_clp(mv.cuenta)
            movs[mid] = mv
        # Σ topes por movimiento y por doc, FRESCOS dentro de la transacción.
        ids_grupo = [g.id for g in grupo]
        for mid, mv in movs.items():
            # Tope por MOVIMIENTO con suma FIRMADA (hallazgo #6, revisión
            # 2026-08-06): con Σ|abs| el combo factura−NC de la regla 8 era
            # INCONFIRMABLE — Σ|v| > Σv siempre que el grupo trae una NC, así que
            # TODO combo moría 409 'Doble uso' en la última milla. El consumo real
            # del movimiento es la suma CON SIGNO proyectada a su lado (cargo=+1 /
            # abono=−1), con 0−tol ≤ consumo ≤ mov.monto+tol. El tope por DOCUMENTO
            # (más abajo) sigue en abs(): cada fila es un doc distinto y la NC
            # consume su propio |monto_efectivo|.
            confirmado = (db.query(func.coalesce(
                func.sum(SiiLibroMatch.monto_asignado), 0))
                .filter(SiiLibroMatch.movimiento_id == mid,
                        SiiLibroMatch.estado == "confirmado",
                        ~SiiLibroMatch.id.in_(ids_grupo)).scalar())
            nuevo = sum(_p(g.monto_asignado) for g in grupo
                        if g.movimiento_id == mid)
            signo_mov = 1 if mv.tipo == "cargo" else -1
            consumo = (_p(confirmado) + nuevo) * signo_mov
            if consumo > _p(mv.monto) + TOL_PESO:
                raise HTTPException(
                    409, "Doble uso del monto: lo confirmado más este match excede "
                         f"el movimiento (${_p(mv.monto):,})".replace(",", "."))
            if consumo < -TOL_PESO:
                raise HTTPException(
                    409, "El grupo no explica el movimiento: la suma firmada de los "
                         "montos asignados quedaría negativa para este lado")
            # «Des-conciliado a medias»: el flag y los enlaces internos deben contar
            # la misma historia antes de sumar un match encima.
            egresos, ingresos = _enlaces_internos(db, mid)
            if (egresos or ingresos) and not mv.conciliado:
                raise HTTPException(
                    409, "El movimiento tiene enlaces de conciliación pero no está "
                         "marcado conciliado: estado a medias — repare en Tesorería "
                         "antes de confirmar")
        # ── Serialización del lado DOC (hallazgo #8, revisión 2026-08-06): dos
        # confirmaciones paralelas de movimientos DISTINTOS sobre el MISMO doc
        # leían ambas el Σ del doc sin lock (ambas veían 0) y commiteaban 2× el
        # documento — la sonda concurrente de la regla 2 exige 'uno gana, el otro
        # 409'. FOR UPDATE sobre las filas de sii_libro_match de cada doc (índice
        # ix_sii_match_doc_estado) — JAMÁS sobre la tabla del espejo (regla 22
        # intacta: el barrido no toca la tabla de matches). Orden global de locks:
        # movimientos primero (arriba, por id ASC), docs por doc_id ASC acá.
        for did in sorted({g.doc_id for g in grupo}):
            (db.query(SiiLibroMatch.id)
             .filter(SiiLibroMatch.doc_id == did)
             .with_for_update().all())
        for g in grupo:
            doc = (db.query(SiiLibroDoc).filter(SiiLibroDoc.id == g.doc_id)
                   .populate_existing().first())
            if doc is None or doc.estado_espejo != "ACTIVO":
                raise HTTPException(409, "El documento del match ya no está ACTIVO "
                                         "en el espejo")
            if g.hash_doc_al_matchear and doc.hash_remoto != g.hash_doc_al_matchear:
                raise HTTPException(
                    409, "El documento cambió en Wasabil después de la sugerencia: "
                         "re-corra el matcher y revise antes de confirmar")
            confirmado_doc = (db.query(func.coalesce(
                func.sum(func.abs(SiiLibroMatch.monto_asignado)), 0))
                .filter(SiiLibroMatch.doc_id == g.doc_id,
                        SiiLibroMatch.estado == "confirmado",
                        ~SiiLibroMatch.id.in_(ids_grupo)).scalar())
            if _p(confirmado_doc) + abs(_p(g.monto_asignado)) \
                    > abs(_p(doc.monto_efectivo)) + TOL_PESO:
                raise HTTPException(
                    409, "Posible doble pago: el documento ya está cubierto por "
                         "otros matches confirmados")
            # Contradicción de tercera pata FRESCA (carrera de la regla 3: si otro
            # request conciliara este mov con un egreso ajeno, acá se ve).
            egresos, _ing = _enlaces_internos(db, g.movimiento_id)
            if egresos:
                from compras_contab.models import ContCompra, ContEgreso, ContEgresoDetalle
                from .matcher import _folio_estricto, _rut_canon_sucio
                ruts, folios = set(), set()
                for c in egresos:
                    e = db.query(ContEgreso).filter(
                        ContEgreso.id == c.egreso_id).first()
                    if e is None:
                        continue
                    canon_b = _rut_canon_sucio(e.beneficiario_rut)
                    if canon_b:
                        ruts.add(canon_b)
                    for det, compra in (db.query(ContEgresoDetalle, ContCompra)
                                        .join(ContCompra,
                                              ContCompra.id == ContEgresoDetalle.compra_id)
                                        .filter(ContEgresoDetalle.egreso_id == e.id)
                                        .all()):
                        canon = _rut_canon_sucio(compra.proveedor_rut)
                        if canon:
                            ruts.add(canon)
                            folios.add((canon, _folio_estricto(compra.numero_documento)))
                if ruts and doc.rut_emisor_canonico \
                        and doc.rut_emisor_canonico not in ruts:
                    raise HTTPException(
                        409, "El movimiento quedó conciliado con un egreso de OTRO "
                             "RUT: contradicción — resuelva primero cuál de las dos "
                             "conciliaciones está mala")
            g.estado = "confirmado"
            g.origen = "humano"
            g.usuario_id = uid
            g.decidido_at = datetime.utcnow()
            g.divergente = False
            g.divergencia_detalle = None
        for mv in movs.values():
            if not mv.conciliado:
                # El match confirmado deja el movimiento EXPLICADO: mismo flag que
                # protege contra DELETE (409 en Tesorería) y da la exclusión mutua
                # NC↔abono vs cobranza (regla 14). Descartar lo apaga si es del match.
                mv.conciliado = True
                mv.conciliado_at = datetime.utcnow()
        db.commit()
        return {"ok": True, "confirmados": [g.id for g in grupo]}

    resultado = _con_retry_deadlock(db, _tx)
    if resultado is None:
        raise HTTPException(409, "Conflicto momentáneo con otra operación "
                                 "simultánea: reintente")
    logger.info("Matcher: match %s confirmado por usuario %s", match_id, uid)
    return resultado


@router.post("/{match_id}/descartar")
def descartar(match_id: int, body: DescartarIn = None,
              db: Session = Depends(get_db),
              current_user: User = Depends(get_current_user)):
    """Descarte (o DESHACER de un confirmado) — acción humana explícita con usuario y
    fecha. El descartado OCUPA el par (mov, doc): el motor jamás lo revive. Si el
    match era el dueño del flag conciliado del movimiento, lo libera; la conciliación
    interna de Tesorería jamás se toca."""
    m = _match_scoped(db, match_id)
    if m.estado == "descartado":
        raise HTTPException(409, "El match ya está descartado")
    grupo = _grupo_de(db, m)
    uid = getattr(current_user, "id", None)
    motivo_txt = (body.motivo or "").strip() if body else ""

    def _tx():
        from .matcher import _liberar_conciliado_si_es_del_match, _motivo_append
        mov_ids = sorted({g.movimiento_id for g in grupo
                          if g.movimiento_id is not None})
        eran_confirmados = [g for g in grupo if g.estado == "confirmado"]
        for g in grupo:
            g.estado = "descartado"
            g.usuario_id = uid
            g.decidido_at = datetime.utcnow()
            if motivo_txt:
                _motivo_append(g, f"descartado: {motivo_txt}")
        if eran_confirmados:
            for mid in mov_ids:
                _liberar_conciliado_si_es_del_match(db, mid)
        db.commit()
        return {"ok": True, "descartados": [g.id for g in grupo]}

    resultado = _con_retry_deadlock(db, _tx)
    if resultado is None:
        raise HTTPException(409, "Conflicto momentáneo con otra operación "
                                 "simultánea: reintente")
    logger.info("Matcher: match %s descartado por usuario %s", match_id, uid)
    return resultado


@router.get("/resumen")
def resumen(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    """El tablero del matcher: autos vivos, sugeridos, conflictos, % del libro
    conciliado POR MONTO, etiquetas de exclusión y la última corrida (sin bitácora,
    «¿de cuándo son estas sugerencias?» no tiene respuesta)."""
    conteos = dict(db.query(SiiLibroMatch.estado, func.count(SiiLibroMatch.id))
                   .filter(SiiLibroMatch.empresa == "mineria")
                   .group_by(SiiLibroMatch.estado).all())
    autos_vivos = (db.query(func.count(SiiLibroMatch.id))
                   .filter(SiiLibroMatch.estado == "confirmado",
                           SiiLibroMatch.origen == "auto",
                           SiiLibroMatch.divergente.is_(False)).scalar())
    # % del libro conciliado por MONTO: Σ |asignado confirmado| / Σ |monto_efectivo|
    # de los docs ACTIVOS (los montos mandan: 100 docs chicos no son el libro).
    total_libro = float(db.query(func.coalesce(
        func.sum(func.abs(SiiLibroDoc.monto_efectivo)), 0))
        .filter(SiiLibroDoc.estado_espejo == "ACTIVO").scalar() or 0)
    conciliado = float(db.query(func.coalesce(
        func.sum(func.abs(SiiLibroMatch.monto_asignado)), 0))
        .filter(SiiLibroMatch.estado == "confirmado").scalar() or 0)
    etiquetas = dict(db.query(SiiMatchEtiquetaMov.etiqueta,
                              func.count(SiiMatchEtiquetaMov.id))
                     .filter(SiiMatchEtiquetaMov.revertida.is_(False))
                     .group_by(SiiMatchEtiquetaMov.etiqueta).all())
    # Hallazgo #5 (revisión 2026-08-08): «la última corrida» se tomaba por id a secas, y
    # la corrida que PIERDE el fencing es siempre la de id mayor — con exito=False. El
    # tablero pintaba «Última corrida: … · falló» aunque la ganadora hubiera trabajado
    # bien, y se quedaba pegado así hasta la corrida siguiente (la ganadora tiene id
    # MENOR y nunca vuelve a ser la última). Retirarse por fencing NO es fallar: es
    # abstenerse. Se excluye para que el listón hable de la corrida que SÍ trabajó; un
    # fallo REAL sigue apareciendo como falló.
    # coalesce obligatorio: en SQL `error != 'x'` con error NULL da NULL, no True — sin
    # esto se perderían justamente las corridas exitosas, que dejan el error en NULL.
    ultima = (db.query(SiiMatchRun)
              .filter(func.coalesce(SiiMatchRun.error, "") != ERROR_FENCING)
              .order_by(SiiMatchRun.id.desc()).first())
    cfg = cargar_config(db)
    return {
        "autos_vivos": int(autos_vivos or 0),
        "sugeridos": int(conteos.get("sugerido", 0)),
        "en_duda": int(conteos.get("en_duda", 0)),
        "conflictos": int(conteos.get("conflicto", 0)),
        "confirmados": int(conteos.get("confirmado", 0)),
        "descartados": int(conteos.get("descartado", 0)),
        # Regla 17: contador propio de la cubeta (docs saldados por efectivo/tarjeta
        # que JAMÁS son candidatos — el invariante del auditor necesita verlos).
        "no_bancario": len(docs_no_bancarios(db)),
        "pct_libro_conciliado_por_monto": round(
            100.0 * conciliado / total_libro, 1) if total_libro else 0.0,
        "etiquetas": etiquetas,
        "rut_en_glosa_activo": bool(cfg.get("rut_en_glosa_activo")),
        "ultima_corrida": {
            "origen": ultima.origen, "exito": ultima.exito,
            "terminado_at": (ultima.terminado_at.isoformat()
                             if ultima.terminado_at else None),
            "error": ultima.error,
            "sugeridos_nuevos": ultima.sugeridos_nuevos,
            "autos_nuevos": ultima.autos_nuevos,
        } if ultima else None,
    }
