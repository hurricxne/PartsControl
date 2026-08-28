from fastapi import APIRouter, Depends, Query, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_, and_, exists, func
import os
import re
import unicodedata
import uuid
from typing import Optional
from datetime import date, datetime

from database import get_db
from auth import get_current_user
from monza_models import (
    MonzaCotizacion, MonzaCliente, MonzaCotizacionItem, MonzaDespacho, MonzaDespachoItem,
    MonzaRecepcion, MonzaRecepcionItem, MonzaEmbarque, MonzaEmbarqueItem, MonzaOcProveedor,
)
from monza_correlativos import siguiente_secuencia
from monza_fechas import business_days_remaining, hoy_chile, rango_dias
from pydantic import BaseModel
from typing import List
from monza_notif import crear_notif
from empresa_guard import require_empresa
# service.py de Monza es puro (sin imports de router ni de modelos): importar de ahí no
# arma ciclo. Espejo de lo que hace routers/despachos.py con wasabil_dte.service.
from monza_wasabil_dte.service import hoy_chile

# Candado de empresa (hallazgo #13 de la auditoría integral): este router quedó sin
# require_empresa mientras Grupo AM sí candó el suyo (routers/despachos.py), así que
# un usuario de 'mineria' leía y MUTABA despachos de MonzaParts (crear/cerrar/anular
# entraban a la lógica de negocio en vez de cortarse en la puerta). Se canda el router
# COMPLETO, no endpoint por endpoint, para no dejar mitades.
router = APIRouter(prefix="/api/monza/despachos", tags=["monza-despachos"],
                   dependencies=[Depends(require_empresa("automotriz"))])


# Antigüedad máxima aceptada para la fecha de emisión de una guía tecleada a mano. No es
# una regla del SII: es una malla ANCHA contra el dedazo grueso (un 2019 en vez de un
# 2026). NO ataja el typo de UN año, que cae dentro de los 730 días — ver la nota larga en
# routers/despachos.py y el cruce contra la fecha de la factura en service.py.
# Espejo del de routers/despachos.py — DUPLICADO A PROPÓSITO: MachParts y MonzaParts no
# comparten código de negocio, y un import cruzado entre routers de marcas distintas es
# justo lo que la separación por empresa existe para impedir.
_ANTIGUEDAD_MAX_FECHA_GUIA_DIAS = 730

# Formato aceptado: AAAA-MM-DD, opcionalmente con sufijo horario ISO. Cualquier otra cosa
# pegada a la fecha se RECHAZA en vez de truncarse (ver _parse_fecha_guia).
_RE_FECHA_GUIA = re.compile(r"^\d{4}-\d{2}-\d{2}([T ].*)?$")


def _parse_fecha_guia(valor) -> Optional[date]:
    """Texto YYYY-MM-DD → date, para la fecha de EMISIÓN de la guía en papel.

    Devuelve None cuando el operador la borra (null o ""), que es un estado legítimo:
    el despacho vuelve a "sin fecha" y la facturación electrónica se bloquea hasta que
    se cargue de nuevo.

    Valida ANTES de guardar porque esta fecha termina viajando como FchRef dentro de un
    DTE 33 REAL: una fecha inventada no se corrige después, sólo se anula el documento.
    Se rechaza: lo que no sea YYYY-MM-DD, una fecha FUTURA, y una fecha absurdamente
    vieja (ver _ANTIGUEDAD_MAX_FECHA_GUIA_DIAS).
    """
    if valor is None:
        return None
    texto = str(valor).strip()
    if not texto:
        return None
    # `_RE_FECHA_GUIA` antes de strptime: con `texto[:10]` a secas, "2026-07-155" se
    # truncaba a "2026-07-15" y se aceptaba en silencio un dato que el operador escribió
    # mal. Se admite el sufijo horario ("2026-07-15T10:00:00") porque algunos clientes
    # mandan datetime ISO, pero nada más pegado a la fecha.
    if not _RE_FECHA_GUIA.match(texto):
        raise HTTPException(
            400,
            "La fecha de emisión de la guía debe tener el formato AAAA-MM-DD "
            f"(recibido: '{texto}')",
        )
    try:
        fecha = datetime.strptime(texto[:10], "%Y-%m-%d").date()
    except ValueError:
        # Formato correcto pero fecha inexistente: 2026-02-31, 2026-13-01.
        raise HTTPException(
            400,
            f"La fecha de emisión de la guía '{texto[:10]}' no existe en el calendario",
        )
    hoy = hoy_chile()
    if fecha > hoy:
        raise HTTPException(
            400,
            f"La fecha de emisión de la guía ({fecha.isoformat()}) es futura: "
            f"revisa el dato (hoy en Chile es {hoy.isoformat()}). Debe ser la fecha en "
            "que el SII emitió la guía.",
        )
    if (hoy - fecha).days > _ANTIGUEDAD_MAX_FECHA_GUIA_DIAS:
        raise HTTPException(
            400,
            f"La fecha de emisión de la guía ({fecha.isoformat()}) tiene más de "
            f"{_ANTIGUEDAD_MAX_FECHA_GUIA_DIAS // 365} años: revisa el año antes de "
            "guardar (es el error de tipeo más común).",
        )
    return fecha


# ── Guía FIRMADA (respaldo de la entrega) ─────────────────────────────────────
# Repositorio FÍSICO de documentos de la casa: el mismo uploads/docs que usa GA
# (compras.py DOCS_DIR / despachos.py _DOCS_DIR). Se comparte el DIRECTORIO, no el
# endpoint: cada marca sube y sirve por SU router, con SU candado de empresa —
# el serve de GA exige 'mineria' y uno de Monza con él recibía 403.
_DOCS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "uploads", "docs")
)
os.makedirs(_DOCS_DIR, exist_ok=True)

# La guía firmada es una FOTO o un ESCANEO (el modal manda image/*+pdf): no se acepta
# Office como en el upload genérico de compras — un .xlsx jamás es una guía firmada.
_EXT_GUIA_FIRMADA = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".heic"}
_MAX_GUIA_FIRMADA_BYTES = 20 * 1024 * 1024  # 20 MB (espejo del upload de la casa)


def _parse_fecha_firma(valor: str, fecha_guia: Optional[date]) -> date:
    """Texto YYYY-MM-DD → date, para la fecha en que el CLIENTE FIRMÓ la guía.

    OBLIGATORIA (a diferencia de GA, que la defaultea a hoy): es el dato que el dueño
    pidió registrar y el que después ordena la cobranza (OC + factura + guía firmada).
    Mismas mallas que _parse_fecha_guia (formato estricto, no futura, tope 2 años) y una
    más: la firma no puede ser ANTERIOR a la emisión de la guía en papel (nadie firma
    un documento que aún no existe). No se valida contra fecha (creación) ni
    fecha_despacho (cierre): ambas son relojes del SERVIDOR en UTC y el registro en el
    sistema puede ir atrasado respecto del hecho físico — validar contra ellas
    fabricaría falsos rechazos justo en los despachos registrados tarde."""
    texto = (valor or "").strip()
    if not texto or not _RE_FECHA_GUIA.match(texto):
        raise HTTPException(
            400,
            "La fecha de la firma debe tener el formato AAAA-MM-DD "
            f"(recibido: '{texto}')",
        )
    try:
        fecha = datetime.strptime(texto[:10], "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(
            400,
            f"La fecha de la firma '{texto[:10]}' no existe en el calendario",
        )
    hoy = hoy_chile()
    if fecha > hoy:
        raise HTTPException(
            400,
            f"La fecha de la firma ({fecha.isoformat()}) es futura: revisa el dato "
            f"(hoy en Chile es {hoy.isoformat()}).",
        )
    if (hoy - fecha).days > _ANTIGUEDAD_MAX_FECHA_GUIA_DIAS:
        raise HTTPException(
            400,
            f"La fecha de la firma ({fecha.isoformat()}) tiene más de "
            f"{_ANTIGUEDAD_MAX_FECHA_GUIA_DIAS // 365} años: revisa el año antes de "
            "guardar (es el error de tipeo más común).",
        )
    if fecha_guia and fecha < fecha_guia:
        raise HTTPException(
            400,
            f"La fecha de la firma ({fecha.isoformat()}) es ANTERIOR a la emisión de "
            f"la guía ({fecha_guia.isoformat()}): el cliente no puede firmar una guía "
            "que todavía no existía. Revisa ambas fechas.",
        )
    return fecha


def _despacho_dict(c: MonzaCotizacion) -> dict:
    asesor_nombre = c.asesor.email.split("@")[0].title() if c.asesor else None
    return {
        "id": c.id,
        "numero": c.numero,
        "estado": c.estado,
        "linea": c.linea,
        "vehiculo": c.vehiculo,
        "vin": c.vin,
        "anio": c.anio,
        "oc_cliente": c.oc_cliente,
        "numero_factura": c.numero_factura,
        "tipo_documento": c.tipo_documento,
        "tiene_documento": bool(c.documento_path),
        "fecha_venta": c.fecha_venta.isoformat() if c.fecha_venta else None,
        "fecha_despacho": c.fecha_despacho.isoformat() if c.fecha_despacho else None,
        "total_bruto": c.total_bruto,
        "items_count": len(c.items),
        "asesor": asesor_nombre,
        "fecha_creacion": c.fecha_creacion.isoformat(),
        "cliente": {
            "id": c.cliente.id,
            "nombre": c.cliente.nombre,
            "rut": c.cliente.rut,
        } if c.cliente else None,
        "lead_numero": c.lead.numero if c.lead else None,
    }


# ── Buscador de operador (spec buscadores Bodega/Despachos 2026-08-05) ─────────
#
# Helper PROPIO de MonzaParts, DUPLICADO a propósito respecto de MachParts: las
# marcas comparten la FORMA del contrato (parámetro q, tokens AND/OR, escapado,
# doble pasada), nunca el código — un import cruzado entre routers de marcas es
# justo lo que la separación por empresa existe para impedir.
# Lo importa monza_router_bodega con import LOCAL (misma dirección de dependencia
# que ya usa cerrar_recepcion: bodega → despachos; despachos no importa bodega).

_Q_MAX_LEN = 64      # defensa contra pegados de Excel
_Q_MIN_LEN = 2       # bajo esto NO se filtra (misma vara que monza_router_catalog)
_Q_MAX_TOKENS = 4    # el 5º token y siguientes se descartan
# Prefijos que la UI imprime y el operador copia. En Monza el COT- SÍ se guarda,
# así que la variante sin prefijo es DEFENSIVA (en MachParts es obligatoria).
_Q_PREFIJOS = ("COT-", "OCP-", "OC-", "EMB-", "DSP-", "N°", "Nº", "#")


def _patron(v: str) -> str:
    """Patrón LIKE con \\, % y _ escapados. Usar SIEMPRE como
    col.like(_patron(tok), escape="\\\\"). Sin esto teclear '_' devuelve la tabla
    entera y '100_4567' matchea '100-4567' Y '100X4567' — dos partes DISTINTAS
    (el único defecto de la lista que cuesta plata, no tiempo)."""
    for c in ("\\", "%", "_"):
        v = v.replace(c, "\\" + c)
    return f"%{v}%"


def _normalizar_q(q: Optional[str]) -> dict:
    """Contrato común del parámetro q (una implementación por marca):
    strip + colapsar espacios internos + truncar a 64; con menos de 2 caracteres
    el filtro se IGNORA por completo (igual que hoy con q vacío); tokenizar por
    espacio con máximo 4 tokens. Semántica: AND entre tokens, OR entre campos."""
    q = re.sub(r"\s+", " ", (q or "").strip())[:_Q_MAX_LEN]
    if len(q) < _Q_MIN_LEN:
        return {"q": q, "tokens": [], "activo": False}
    return {"q": q, "tokens": q.split(" ")[:_Q_MAX_TOKENS], "activo": True}


def _variantes_q(tok: str) -> list:
    """El token crudo y, si trae un prefijo conocido de la UI, también sin él."""
    out = [tok]
    up = tok.upper()
    for p in _Q_PREFIJOS:
        if up.startswith(p) and len(tok) > len(p):
            out.append(tok[len(p):])
            break
    return out


def _colapsar_q(tok: str) -> str:
    """Forma colapsada del token para número de parte: sin guiones ni espacios,
    en mayúsculas. 7T-1997 y 7T1997 conviven como filas distintas en los datos
    (verificado en la spec): la comparación colapsada cubre las DOS direcciones."""
    return re.sub(r"[\s\-]", "", tok).upper()


def _q_tiene_digitos(ncfg: dict) -> bool:
    return any(any(ch.isdigit() for ch in t) for t in ncfg["tokens"])


def _np_colapsado_sql():
    """Columna numero_parte colapsada en SQL (para la pasada B). Full scan
    reconocido: corre SOLO como red de seguridad cuando la pasada A dio 0 filas,
    nunca en la primera pasada, y siempre bajo el filtro de estado que la acota."""
    return func.replace(func.replace(func.upper(MonzaCotizacionItem.numero_parte), "-", ""), " ", "")


def _cond_token_venta(tok: str, con_colapsado: bool = False):
    """Condición OR de un token sobre la VENTA (MonzaCotizacion) y sus hijas.

    REGLA INVIOLABLE: todo campo de tabla HIJA (ítems, embarques, despachos, OCP)
    entra como EXISTS correlacionado, NUNCA como .join() — un join a la colección
    multiplica la fila de la cabecera, infla total = query.count() y el LIMIT se
    come los cupos con duplicados (el operador ve "9 resultados" donde hay 3).

    Requiere que el llamador ya haya hecho join(MonzaCotizacion.cliente, isouter=True)
    (el joinedload NO habilita filtrar)."""
    ors = []
    for v in _variantes_q(tok):
        p = _patron(v)
        ors += [
            MonzaCotizacion.numero.like(p, escape="\\"),
            MonzaCotizacion.oc_cliente.like(p, escape="\\"),
            MonzaCotizacion.vehiculo.like(p, escape="\\"),
            MonzaCotizacion.vin.like(p, escape="\\"),
            MonzaCotizacion.numero_factura.like(p, escape="\\"),
            MonzaCliente.nombre.like(p, escape="\\"),
            MonzaCliente.rut.like(p, escape="\\"),
            # EXISTS a ítems: numero_parte / descripcion / marca
            exists().where(and_(
                MonzaCotizacionItem.cotizacion_id == MonzaCotizacion.id,
                or_(
                    MonzaCotizacionItem.numero_parte.like(p, escape="\\"),
                    MonzaCotizacionItem.descripcion.like(p, escape="\\"),
                    MonzaCotizacionItem.marca.like(p, escape="\\"),
                ),
            )),
            # EXISTS a embarque (2 saltos). OJO asimetría de marcas: en Monza `awb`
            # guarda EL NÚMERO (no el nombre del archivo, como en MachParts).
            exists().where(and_(
                MonzaCotizacionItem.cotizacion_id == MonzaCotizacion.id,
                MonzaEmbarqueItem.item_id == MonzaCotizacionItem.id,
                MonzaEmbarque.id == MonzaEmbarqueItem.embarque_id,
                or_(
                    MonzaEmbarque.numero.like(p, escape="\\"),
                    MonzaEmbarque.awb.like(p, escape="\\"),
                    MonzaEmbarque.tracking.like(p, escape="\\"),
                    MonzaEmbarque.forwarder.like(p, escape="\\"),
                ),
            )),
            # EXISTS a despachos: N° DSP, N° guía SII, expedición, transportista
            exists().where(and_(
                MonzaDespacho.cotizacion_id == MonzaCotizacion.id,
                or_(
                    MonzaDespacho.numero.like(p, escape="\\"),
                    MonzaDespacho.numero_guia.like(p, escape="\\"),
                    MonzaDespacho.numero_expedicion.like(p, escape="\\"),
                    MonzaDespacho.transportista.like(p, escape="\\"),
                    MonzaDespacho.destinatario.like(p, escape="\\"),
                ),
            )),
            # EXISTS a OCP (vínculo Integer suelto sin FK: se filtra igual)
            exists().where(and_(
                MonzaCotizacionItem.cotizacion_id == MonzaCotizacion.id,
                MonzaOcProveedor.id == MonzaCotizacionItem.oc_proveedor_id,
                or_(
                    MonzaOcProveedor.numero.like(p, escape="\\"),
                    MonzaOcProveedor.numero_oc.like(p, escape="\\"),
                ),
            )),
        ]
    if con_colapsado and any(ch.isdigit() for ch in tok):
        cc = _colapsar_q(tok)
        if cc:
            ors.append(exists().where(and_(
                MonzaCotizacionItem.cotizacion_id == MonzaCotizacion.id,
                _np_colapsado_sql().like(_patron(cc), escape="\\"),
            )))
    return or_(*ors)


def _filtro_q(query, ncfg: dict, con_colapsado: bool = False):
    """UN solo filtro para los endpoints de Despachos (la deuda era que el listado
    y /avance divergían: SQL vs Python, 5 vs 4 campos, tildes distintas). Hace su
    propio join explícito al cliente — el llamador NO debe joinearlo de nuevo."""
    if not ncfg["activo"]:
        return query
    query = query.join(MonzaCotizacion.cliente, isouter=True)
    for tok in ncfg["tokens"]:
        query = query.filter(_cond_token_venta(tok, con_colapsado))
    return query


# ── Motivo de coincidencia (insignia): lo calcula el BACKEND sobre la página ───

def _plegar_txt(s) -> str:
    """minúsculas + sin tildes, espejo en Python de la collation utf8mb4_unicode_ci
    (str.lower() a secas NO ignora tildes y la insignia se perdería en silencio)."""
    if not s:
        return ""
    return "".join(c for c in unicodedata.normalize("NFD", str(s).lower())
                   if not unicodedata.combining(c))


def _match_de_candidatos(candidatos, ncfg: dict, normalizado: bool = False) -> list:
    """[{campo, valor}] de los campos que explican por qué la fila salió.
    Se corre SOLO sobre la página ya paginada (≤50 filas): costo despreciable, y
    evita que el frontend re-implemente la lógica de matcheo y se desincronice.
    Con la pasada colapsada activa, numero_parte también se compara sin guiones y
    la insignia dice 'n° parte (sin guiones)' — no se puede mapear 6003113721
    sobre 600-311-3721 carácter a carácter sin mentir."""
    toks = [[_plegar_txt(v) for v in _variantes_q(t)] for t in ncfg["tokens"]]
    out, vistos = [], set()
    for campo, valor in candidatos:
        if not valor:
            continue
        plano = _plegar_txt(valor)
        if any(any(v and v in plano for v in vs) for vs in toks):
            clave = (campo, str(valor))
            if clave not in vistos:
                vistos.add(clave)
                out.append({"campo": campo, "valor": str(valor)})
        elif normalizado and campo == "numero_parte":
            colapsado_val = re.sub(r"[\s\-]", "", str(valor)).upper()
            if any(_colapsar_q(t) and _colapsar_q(t) in colapsado_val
                   for t in ncfg["tokens"] if any(ch.isdigit() for ch in t)):
                clave = ("numero_parte_sin_guiones", str(valor))
                if clave not in vistos:
                    vistos.add(clave)
                    out.append({"campo": "numero_parte_sin_guiones", "valor": str(valor)})
        if len(out) >= 6:
            break
    return out


def _match_ventas(db: Session, cots, ncfg: dict, normalizado: bool = False) -> dict:
    """{cot_id: match} para una PÁGINA de ventas. Los hijos (embarques, despachos,
    OCP) se precargan en LOTE sobre los ids de la página — nunca por venta."""
    if not ncfg["activo"] or not cots:
        return {}
    cot_ids = [c.id for c in cots]
    item_ids = [i.id for c in cots for i in (c.items or [])]
    emb_por_cot: dict = {}
    if item_ids:
        item_a_cot = {i.id: c.id for c in cots for i in (c.items or [])}
        rows = (
            db.query(MonzaEmbarqueItem.item_id, MonzaEmbarque)
            .join(MonzaEmbarque, MonzaEmbarque.id == MonzaEmbarqueItem.embarque_id)
            .filter(MonzaEmbarqueItem.item_id.in_(item_ids))
            .all()
        )
        for iid, e in rows:
            emb_por_cot.setdefault(item_a_cot.get(iid), []).append(e)
    dsp_por_cot: dict = {}
    for d in db.query(MonzaDespacho).filter(MonzaDespacho.cotizacion_id.in_(cot_ids)).all():
        dsp_por_cot.setdefault(d.cotizacion_id, []).append(d)
    ocp_ids = list({i.oc_proveedor_id for c in cots for i in (c.items or []) if i.oc_proveedor_id})
    ocps = {o.id: o for o in db.query(MonzaOcProveedor).filter(MonzaOcProveedor.id.in_(ocp_ids)).all()} if ocp_ids else {}

    out = {}
    for c in cots:
        cands = [
            ("cotizacion", c.numero), ("oc_cliente", c.oc_cliente),
            ("cliente", c.cliente.nombre if c.cliente else None),
            ("rut", c.cliente.rut if c.cliente else None),
            ("vehiculo", c.vehiculo), ("vin", c.vin), ("factura", c.numero_factura),
        ]
        for i in (c.items or []):
            cands += [("numero_parte", i.numero_parte), ("repuesto", i.descripcion), ("marca", i.marca)]
        for e in emb_por_cot.get(c.id, []):
            cands += [("embarque", e.numero), ("awb", e.awb), ("tracking", e.tracking), ("forwarder", e.forwarder)]
        for d in dsp_por_cot.get(c.id, []):
            cands += [("despacho", d.numero), ("guia", d.numero_guia),
                      ("expedicion", d.numero_expedicion), ("transportista", d.transportista),
                      ("destinatario", d.destinatario)]
        for i in (c.items or []):
            o = ocps.get(i.oc_proveedor_id)
            if o:
                cands += [("ocp", o.numero), ("ocp", o.numero_oc)]
        out[c.id] = _match_de_candidatos(cands, ncfg, normalizado)
    return out


@router.get("")
def list_despachos(
    q: Optional[str] = Query(None),
    desde: Optional[str] = Query(None),
    hasta: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    query = (
        db.query(MonzaCotizacion)
        .options(
            joinedload(MonzaCotizacion.cliente),
            joinedload(MonzaCotizacion.lead),
            joinedload(MonzaCotizacion.asesor),
            joinedload(MonzaCotizacion.items),
        )
        .filter(MonzaCotizacion.estado == "despachado")
    )

    # OJO: fecha_despacho es una columna Date CIVIL (un día, sin hora), así que acá
    # el `<=` INCLUSIVO es la semántica correcta y NO se aplica el semiabierto UTC de
    # sus hermanos — copiar aquel patrón rompería un filtro que está sano. Lo único
    # que cambia es el `except pass` por el 422 de rango_dias: un filtro que el
    # operador pidió no se ignora en silencio.
    desde_d, hasta_d = rango_dias(desde, hasta)
    if desde_d:
        query = query.filter(MonzaCotizacion.fecha_despacho >= desde_d)
    if hasta_d:
        query = query.filter(MonzaCotizacion.fecha_despacho <= hasta_d)

    # Buscador con el contrato común (spec 2026-08-05): _filtro_q reemplaza el or_
    # de 5 campos con ilike (que mataba índices y no cubría n° de parte, embarque
    # ni N° de guía) y unifica la semántica con /avance. Doble pasada: si la
    # literal da 0 y el término trae dígitos, se compara numero_parte COLAPSADO
    # (sin guiones/espacios) — 7T-1997 encuentra 7T1997 y viceversa.
    nq = _normalizar_q(q)
    normalizado = False
    if nq["activo"]:
        con_q = _filtro_q(query, nq)
        total = con_q.count()
        if total == 0 and _q_tiene_digitos(nq):
            normalizado = True
            con_q = _filtro_q(query, nq, con_colapsado=True)
            total = con_q.count()
        query = con_q
    else:
        total = query.count()

    items = (
        query.order_by(MonzaCotizacion.fecha_despacho.desc(), MonzaCotizacion.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    # Insignia de motivo: SOLO sobre la página ya paginada (≤ page_size filas).
    matches = _match_ventas(db, items, nq, normalizado)

    def _con_match(c):
        d = _despacho_dict(c)
        d["match"] = matches.get(c.id, [])
        return d

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        # Claves ADITIVAS del contrato (los consumidores viejos las ignoran):
        "q_efectivo": nq["q"] if nq["activo"] else "",
        "normalizado": normalizado,
        "items": [_con_match(c) for c in items],
    }


@router.get("/kpis")
def despachos_kpis(db: Session = Depends(get_db), _=Depends(get_current_user)):
    # El mes es el de CHILE: el 31 a las 21:30 de Santiago el servidor (UTC) ya está en
    # el mes siguiente y las tarjetas «del mes» se vaciaban las últimas 3-4 horas de cada
    # mes. `fecha_despacho` es una columna Date CIVIL, así que el corte es un `date`
    # puro y NO `inicio_mes_utc()` — mismo criterio que separa `rango_dias` de `rango_utc`.
    from datetime import date
    _hoy = hoy_chile()
    inicio_mes = date(_hoy.year, _hoy.month, 1)

    total_count = db.query(func.count(MonzaCotizacion.id)).filter(
        MonzaCotizacion.estado == "despachado"
    ).scalar() or 0

    mes_count = db.query(func.count(MonzaCotizacion.id)).filter(
        MonzaCotizacion.estado == "despachado",
        MonzaCotizacion.fecha_despacho >= inicio_mes,
    ).scalar() or 0

    total_monto = db.query(func.sum(MonzaCotizacion.total_bruto)).filter(
        MonzaCotizacion.estado == "despachado",
        MonzaCotizacion.fecha_despacho >= inicio_mes,
    ).scalar() or 0

    sin_doc = db.query(func.count(MonzaCotizacion.id)).filter(
        MonzaCotizacion.estado == "despachado",
        MonzaCotizacion.documento_path.is_(None),
    ).scalar() or 0

    return {
        "total_despachados": total_count,
        "despachados_mes": mes_count,
        "monto_mes": float(total_monto),
        "sin_documento": sin_doc,
    }


# ── Despacho como entidad (alineacion MachParts) ──────────────────────────────
#
# FASE 2 DEL ESPEJO GRUPO AM (docs/plan-espejo-monza-2026-07-23.md):
#   · TOPE FÍSICO: nunca se despacha más de lo RECIBIDO en bodega (recepciones
#     cerradas). La qty_recibida dejó de ser dato muerto.
#   · Despacho PARCIAL por cantidad, con remanente despachable después.
#   · Ciclo de vida: en_preparacion (borrador) → cerrar → anulado (reversible solo
#     en borrador). La línea recién voltea a 'despachado' al CERRAR, y solo cuando
#     los despachos CERRADOS cubren la cantidad completa.
#   · Flujo guía-primero: se crea SIN N° de guía; transportista/N° guía/N° de
#     expedición se completan después con el PUT de cabecera.
#   · Guía electrónica SII (Fase 5, monza_wasabil_dte/): con guía 52 VIVA no se
#     anula el despacho ni se pisa su folio a mano (_guia_electronica_activa +
#     _rechazar_si_pisa_folio, espejo de routers/despachos.py de GA).
#   · Fase 6 (facturas 33): CERRAR con la guía 52 todavía sin folio SÍ se permite;
#     el cierre solo registra un hecho físico. Quien bloquea es la EMISIÓN de la
#     factura 33 (_guia_electronica_en_proceso en monza_wasabil_dte/router.py).
#     El porqué de esa asimetría está en _cerrar_despacho_tx.
#   · Concurrencia: FOR UPDATE + populate_existing + retry del correlativo/deadlock
#     (regla de la casa: docs/regla-lecturas-de-plata.md).
# Arquitectura espejo de routers/despachos.py (Grupo AM); diferencias de modelo:
# la cotización ES la venta (no hay OcCliente) y la recepción apunta directo al ítem.

def _gen_num_desp(db):
    # El año es el de CHILE: entre las 21:00 y la medianoche del 31 de diciembre, UTC
    # ya está en el año siguiente y el correlativo saltaba de golpe (misma regla que
    # monza_correlativos, donde vive la versión de referencia).
    anio = hoy_chile().year
    # MÁXIMO de la serie, no «la fila con id más alto» (ver monza_correlativos, donde
    # vive la regla y el porqué). Conserva la tolerancia al sufijo legado no numérico:
    # el helper ignora esas filas en vez de tumbar la creación con un 500, y el índice
    # único más el reintento convierten cualquier choque en un 409 controlado.
    n = siguiente_secuencia(db, MonzaDespacho.numero, f"DSP-{anio}-")
    return f"DSP-{anio}-{n:04d}"


# Estados de recepción cuyas unidades quedan UTILIZABLES en bodega. 'faltante' está
# incluido porque su qty_recibida son las unidades que SÍ llegaron buenas (el faltante
# se reclama aparte); 'no_llego' y 'danado_no_utilizable' aportan 0. Espejo de GA.
_RECEPCION_UTILIZABLE = ("completo", "danado_utilizable", "sobrante", "faltante")


def _qty_recibida_utilizable(db: Session, item_ids):
    """{item_id: Σ qty_recibida utilizable} desde recepciones CERRADAS.

    Es el TOPE FÍSICO del despacho: no se puede despachar más de lo que Bodega
    recibió realmente. Solo aparecen los ítems que TIENEN registro de recepción —
    un ítem sin recepción (flujo antiguo o carga manual) no se acota, para no
    romper datos históricos.

    Suma DOS fuentes en el MISMO dict por item_cotizacion_id: (1) recepciones de
    EMBARQUE cerradas utilizables (internacional), (2) recepciones NACIONALES
    cerradas utilizables. Aditivo y direccionalmente seguro: para un ítem nacional
    solo puede BAJAR el tope (de 'todo lo vendido' a min(vendido, recibido)); no
    toca los internacionales (fuente distinta, item_ids disjuntos por construcción
    — el guard anti-embarque de abastecimiento/logística impide que un ítem
    nacional entre a un embarque).

    Compatibilidad con datos legados de Monza (solo fuente 1): qty_recibida era
    Optional y las recepciones viejas pudieron cerrarse sin número. Con
    qty_recibida NULL se interpreta por el estado: completo/sobrante = la cantidad
    vendida completa; danado_utilizable = cantidad − qty_danada; faltante = 0 (no
    se declaró cuánto llegó → conservador: no habilita despacho de mercadería no
    verificada)."""
    if not item_ids:
        return {}
    cant_por_item = {
        i.id: (i.cantidad or 0)
        for i in db.query(MonzaCotizacionItem).filter(MonzaCotizacionItem.id.in_(item_ids)).all()
    }
    # ── Fuente 1: embarques internacionales (SIN CAMBIOS) ──
    rows = (
        db.query(MonzaRecepcionItem)
        .join(MonzaRecepcion, MonzaRecepcion.id == MonzaRecepcionItem.recepcion_id)
        .filter(
            MonzaRecepcionItem.item_id.in_(item_ids),
            MonzaRecepcion.estado == "cerrada",
            MonzaRecepcionItem.estado_recepcion.in_(_RECEPCION_UTILIZABLE),
        )
        .all()
    )
    result = {}
    for ri in rows:
        if ri.qty_recibida is not None:
            qty = max(ri.qty_recibida, 0)
        elif ri.estado_recepcion in ("completo", "sobrante"):
            qty = cant_por_item.get(ri.item_id, 0)
        elif ri.estado_recepcion == "danado_utilizable":
            qty = max(cant_por_item.get(ri.item_id, 0) - (ri.qty_danada or 0), 0)
        else:  # 'faltante' sin número declarado
            qty = 0
        result[ri.item_id] = result.get(ri.item_id, 0) + qty
    # ── Fuente 2: recepciones NACIONALES (UNION aditivo) ──
    # Import LOCAL (patrón probado bodega→despachos): monza_recepcion_nacional.models
    # NO importa despachos → sin ciclo. Se reusa la MISMA constante
    # _RECEPCION_UTILIZABLE (verbatim idéntica a RECEPCION_UTILIZABLE del paquete).
    from monza_recepcion_nacional.models import MonzaRecepcionNacional, MonzaRecepcionNacionalItem
    nac = (
        db.query(MonzaRecepcionNacionalItem.item_cotizacion_id,
                 MonzaRecepcionNacionalItem.qty_recibida)
        .join(MonzaRecepcionNacional,
              MonzaRecepcionNacional.id == MonzaRecepcionNacionalItem.recepcion_id)
        .filter(
            MonzaRecepcionNacionalItem.item_cotizacion_id.in_(item_ids),
            MonzaRecepcionNacional.estado == "cerrada",
            MonzaRecepcionNacionalItem.estado_recepcion.in_(_RECEPCION_UTILIZABLE),
        )
        .all()
    )
    for item_id, qty in nac:
        if item_id is None:
            continue
        # float() en ambos operandos: evita mezclar Decimal (fuente nacional,
        # Numeric(12,4)) con el valor previo (int de la fuente 1, qty_recibida es
        # Integer en monza_recepcion_items) → nunca TypeError.
        result[item_id] = float(result.get(item_id, 0)) + max(float(qty or 0), 0.0)
    return result


def _tope_fisico_de(item_id: int, cantidad, recibidos: dict):
    """Igual que _tope_fisico pero por (id, cantidad) sueltos: lo usan las lecturas
    en lote que traen tuplas y no objetos ORM (/counts). Misma fórmula, un solo
    lugar donde vive la regla."""
    cant = cantidad or 0
    if item_id in recibidos:
        return min(cant, recibidos[item_id])
    return cant


def _tope_fisico(item: MonzaCotizacionItem, recibidos: dict):
    """Cantidad máxima despachable de la línea: lo RECIBIDO en bodega si hay
    recepción registrada (capado a la cantidad vendida), o la cantidad de la
    cotización si no la hay (comportamiento histórico)."""
    return _tope_fisico_de(item.id, item.cantidad, recibidos)


def _cupo_disponible(item_id: int, cantidad, recibidos: dict, ya: dict):
    """Cupo REALMENTE despachable de una línea: tope físico − lo ya reservado o
    despachado (borradores incluidos). Es la misma cuenta de /listos y del detalle
    de avance, extraída para que los tableros (hallazgo #17) no la reimplementen."""
    return max(_tope_fisico_de(item_id, cantidad, recibidos) - ya.get(item_id, 0), 0)


def _ya_despachado_lote(db: Session, cot_ids) -> dict:
    """{item_id: Σ qty} de despachos NO anulados de VARIAS ventas, en UNA query.
    Versión en lote de _qty_already_dispatched para los tableros de solo lectura
    (evita el N+1 de llamarlo por venta). Mismo criterio: un borrador reserva."""
    result: dict = {}
    if not cot_ids:
        return result
    rows = (
        db.query(MonzaDespachoItem.item_id, MonzaDespachoItem.qty_despachada)
        .join(MonzaDespacho, MonzaDespacho.id == MonzaDespachoItem.despacho_id)
        .filter(MonzaDespacho.cotizacion_id.in_(list(cot_ids)),
                MonzaDespacho.estado != "anulado")
        .all()
    )
    for item_id, qty in rows:
        result[item_id] = result.get(item_id, 0) + (qty or 0)
    return result


def _qty_already_dispatched(db: Session, cotizacion_id: int, con_lock: bool = False):
    """{item_id: Σ qty_despachada} de despachos NO anulados de la venta.

    Es el consumo de cupo del tope anti-sobredespacho: borradores y cerrados
    consumen por igual (un borrador reserva mercadería). `con_lock=True` en la
    creación: FOR UPDATE + populate_existing para validar contra lo COMMITEADO
    más reciente y serializar dos despachos simultáneos de la misma venta."""
    q = (
        db.query(MonzaDespachoItem.item_id, MonzaDespachoItem.qty_despachada)
        .join(MonzaDespacho, MonzaDespacho.id == MonzaDespachoItem.despacho_id)
        .filter(MonzaDespacho.cotizacion_id == cotizacion_id, MonzaDespacho.estado != "anulado")
    )
    if con_lock:
        q = q.populate_existing().with_for_update()
    result = {}
    for item_id, qty in q.all():
        result[item_id] = result.get(item_id, 0) + (qty or 0)
    return result


def _qty_dispatched_closed(db: Session, cotizacion_id: int, con_lock: bool = False):
    """Σ qty por ítem SOLO de despachos CERRADOS ('despachado').

    Es la cobertura que decide el flip de la línea al cerrar: un despacho abierto
    (en_preparacion) es un borrador anulable, no mercadería salida — contarlo
    marcaría 'despachado' prematuro con tandas abiertas (lección G16 de GA)."""
    q = (
        db.query(MonzaDespachoItem.item_id, MonzaDespachoItem.qty_despachada)
        .join(MonzaDespacho, MonzaDespacho.id == MonzaDespachoItem.despacho_id)
        .filter(MonzaDespacho.cotizacion_id == cotizacion_id, MonzaDespacho.estado == "despachado")
    )
    if con_lock:
        q = q.populate_existing().with_for_update()
    result = {}
    for item_id, qty in q.all():
        result[item_id] = result.get(item_id, 0) + (qty or 0)
    return result


# ── Guard DURO del SPLIT de líneas (Fase 9b: envíos parciales) ─────────────────
#
# La partición de una línea de venta (MonzaCotizacionItem) en dos hermanas —
# "se preparan 6 de 10, las 4 esperan el próximo AWB" — es segura mientras la
# línea NO haya tocado todavía ningún documento ni cantidad congelada. Después
# sí ROMPE, y de formas irreversibles:
#
#   · Bajar `cantidad` a posteriori hace que un despacho parcial ya existente
#     CUBRA la línea y la voltee a 'despachado' prematuramente
#     (_cerrar_despacho_tx: qty_total >= it.cantidad), y descuadra la reversa de
#     _anular_despacho_tx (misma comparación al revés).
#   · La guía 52 congela `quantity` y `price` en la respuesta de Wasabil, y el
#     externalId de cada línea es el MonzaDespachoItem.id
#     (monza_wasabil_dte/service.py): la factura 33 hace match 1:1 con la guía por
#     ese id. Partir desincroniza el par guía↔factura y descuadra un documento
#     tributario IRREVERSIBLE ante el SII.
#   · Las recepciones (embarque y nacional) y los costos congelados
#     (monza_emb_pricing_item / monza_cont_compra_item) apuntan al item_id: la
#     hermana nueva nace sin esas filas, y el ítem original quedaría con una
#     cantidad que ya no corresponde a lo recibido/costeado.
#
# Por eso el candado es un 409 y NO un clamp: no hay forma de "arreglar" un split
# sobre un documento vivo, solo de no hacerlo. Se llama ANTES de mutar nada, desde
# monza_router_abastecimiento._guard_duro_del_split — que es también quien decide a
# QUÉ líneas aplicarlo (solo las que de verdad se parten) y quien re-toma el lock
# después, porque este helper puede hacer rollback (ver el porqué más abajo).
#
# Vive en este módulo porque acá ya están los helpers de despacho y el import
# local del módulo DTE, y la dirección de dependencia establecida es
# abastecimiento/logística/bodega → despachos (despachos no importa ninguno).

def _rechazar_split_sobre_documento(db: Session, item_ids, *,
                                    incluir_recepciones: bool = True) -> None:
    """409 si alguna de las líneas pedidas ya no se puede PARTIR (7 comprobaciones).

    `incluir_recepciones=False` omite SOLO las comprobaciones 4 y 5 (recepción de
    embarque y nacional). Es para quien NO parte la línea y únicamente necesita el
    candado de los documentos que congelaron cantidad y precio — hoy, la DEVOLUCIÓN A
    COMPRAS de una línea completa (monza_router_abastecimiento.py). El motivo de 4 y 5
    es que partir rompe el vínculo recepción↔línea; sin partición ese vínculo queda
    intacto, y bloquear por él dejaba ATRAPADO el back order nacional legítimo (una
    entrega cerrada en la que el proveedor no mandó nada deja la línea en 'comprado'
    con su fila de recepción, que es justo el caso que hay que poder devolver).
    Los demás candados —despacho, guía 52, factura, costos congelados— se aplican
    SIEMPRE: no dependen de si la línea se parte, sino de que el documento ya exista.
    Default True: ningún llamador existente cambia de comportamiento.

    Los 5 módulos satélite (contabilidad, recepción nacional, embarques pricing,
    compras contab, DTE) pueden no tener su `init_db` corrido en un deploy: MySQL
    1146 (tabla inexistente) significa que ese módulo JAMÁS registró nada que
    proteger, así que la comprobación se apaga sola en vez de tumbar la operación
    con un 500 (patrón ya escrito en _guia_electronica_activa). Ese rollback SUELTA
    los locks del llamador: por eso _guard_duro_del_split re-toma el lock y re-valida
    el estado al volver. En ese punto no se ha mutado nada todavía."""
    if not item_ids:
        return
    ids = sorted({int(i) for i in item_ids})

    # Etiquetas legibles para el mensaje (una query): el dueño necesita saber CUÁL
    # repuesto bloquea, no un id.
    etiquetas = {
        iid: (parte or desc or f"ítem {iid}")
        for iid, parte, desc in db.query(
            MonzaCotizacionItem.id, MonzaCotizacionItem.numero_parte,
            MonzaCotizacionItem.descripcion,
        ).filter(MonzaCotizacionItem.id.in_(ids)).all()
    }

    def _nombres(offending) -> str:
        return ", ".join(sorted(etiquetas.get(i, f"ítem {i}") for i in set(offending)))

    def _opcional(fn):
        """Comprobación cuyo módulo puede no estar instalado (tabla inexistente)."""
        from sqlalchemy.exc import ProgrammingError
        try:
            return fn()
        except ProgrammingError as e:
            if getattr(getattr(e, "orig", None), "args", [None])[0] == 1146:
                db.rollback()
                return set()
            raise

    # 1) Despacho NO anulado (borrador incluido: un borrador reserva mercadería y su
    #    qty ya se comparará contra la cantidad de la línea al cerrar).
    con_despacho = {
        r[0] for r in db.query(MonzaDespachoItem.item_id)
        .join(MonzaDespacho, MonzaDespacho.id == MonzaDespachoItem.despacho_id)
        .filter(MonzaDespachoItem.item_id.in_(ids),
                MonzaDespacho.estado != "anulado").all()
    }
    if con_despacho:
        raise HTTPException(
            409,
            f"No se puede partir: {_nombres(con_despacho)} ya tiene un despacho "
            "registrado (aunque sea borrador). Anula el despacho primero.",
        )

    # 2) Guía electrónica 52 VIVA de CUALQUIER despacho de la línea, incluidos los
    #    ANULADOS: el folio del SII sobrevive a la anulación del despacho, y ese
    #    documento congeló cantidad y precio de esta línea. incluir_ambiguo=True por
    #    el mismo motivo que anular (una emisión sin confirmar PUDO nacer con folio).
    despachos_de_linea = {
        r[0] for r in db.query(MonzaDespachoItem.despacho_id)
        .filter(MonzaDespachoItem.item_id.in_(ids)).all() if r[0]
    }
    for dsp_id in sorted(despachos_de_linea):
        dte = _opcional(lambda: _guia_electronica_activa(
            db, dsp_id, incluir_ambiguo=True))
        if dte:
            raise HTTPException(
                409,
                "No se puede partir: hay una guía electrónica SII asociada a estas "
                f"líneas (despacho {dsp_id}"
                + (f", folio {dte.folio}" if getattr(dte, "folio", None) else "")
                + "). El documento congeló cantidad y precio.",
            )

    # 3) Factura de cliente con esa línea (el 33 ya declaró cantidad × precio al SII).
    facturados = _opcional(lambda: _split_ids_facturados(db, ids))
    if facturados:
        raise HTTPException(
            409,
            f"No se puede partir: {_nombres(facturados)} ya está facturado. "
            "La factura congeló cantidad y precio de la línea.",
        )

    # 4) Recepción de EMBARQUE (cualquier estado, abierta incluida: el bodeguero ya
    #    está contando físicamente contra la cantidad de esta línea).
    #    4 y 5 son las que `incluir_recepciones=False` omite: su motivo es que PARTIR
    #    rompe el vínculo recepción↔línea (ver el docstring).
    if incluir_recepciones:
        recibidos = {
            r[0] for r in db.query(MonzaRecepcionItem.item_id)
            .filter(MonzaRecepcionItem.item_id.in_(ids)).all()
        }
        if recibidos:
            raise HTTPException(
                409,
                f"No se puede partir: {_nombres(recibidos)} ya tiene recepción de "
                "bodega registrada. Se parte ANTES de que la mercadería llegue.",
            )

        # 5) Recepción NACIONAL (camión + guía del proveedor, sin embarque).
        nacionales = _opcional(lambda: _split_ids_recepcion_nacional(db, ids))
        if nacionales:
            raise HTTPException(
                409,
                f"No se puede partir: {_nombres(nacionales)} ya tiene recepción "
                "nacional registrada.",
            )

    # 6) Costo INTERNACIONAL congelado (landed cost del embarque).
    con_pricing = _opcional(lambda: _split_ids_pricing(db, ids))
    if con_pricing:
        raise HTTPException(
            409,
            f"No se puede partir: {_nombres(con_pricing)} ya tiene costo de "
            "embarque congelado (Embarques Pricing).",
        )

    # 7) Costo NACIONAL congelado (línea de factura de compra).
    con_costo = _opcional(lambda: _split_ids_costo_compra(db, ids))
    if con_costo:
        raise HTTPException(
            409,
            f"No se puede partir: {_nombres(con_costo)} ya tiene costo de compra "
            "asignado (Compras y Pagos).",
        )


# Las 4 lecturas de tablas de módulos satélite van en funciones sueltas para que el
# import LOCAL (anti-ciclo, patrón de la casa) quede al lado de su query y el
# try/except ProgrammingError de arriba las envuelva a todas por igual.

def _split_ids_facturados(db: Session, ids) -> set:
    from monza_contabilidad.models import MonzaContFacturaClienteItem
    return {
        r[0] for r in db.query(MonzaContFacturaClienteItem.item_cotizacion_id)
        .filter(MonzaContFacturaClienteItem.item_cotizacion_id.in_(ids)).all() if r[0]
    }


def _split_ids_recepcion_nacional(db: Session, ids) -> set:
    from monza_recepcion_nacional.models import MonzaRecepcionNacionalItem
    return {
        r[0] for r in db.query(MonzaRecepcionNacionalItem.item_cotizacion_id)
        .filter(MonzaRecepcionNacionalItem.item_cotizacion_id.in_(ids)).all() if r[0]
    }


def _split_ids_pricing(db: Session, ids) -> set:
    from monza_embarques_pricing.models import MonzaEmbPricingItem
    return {
        r[0] for r in db.query(MonzaEmbPricingItem.item_cotizacion_id)
        .filter(MonzaEmbPricingItem.item_cotizacion_id.in_(ids)).all() if r[0]
    }


def _split_ids_costo_compra(db: Session, ids) -> set:
    from monza_compras_contab.models import MonzaContCompraItem
    return {
        r[0] for r in db.query(MonzaContCompraItem.item_cotizacion_id)
        .filter(MonzaContCompraItem.item_cotizacion_id.in_(ids)).all() if r[0]
    }


# ── Tableros de avance (Fase 4 del espejo GA: G15b) ───────────────────────────
# Buckets del pipeline por estado_linea REAL de Monza (espejo de _STATE_BUCKETS de
# routers/despachos.py:57-74; los estados difieren porque el pipeline Monza es otro).
_STATE_BUCKETS = {
    "cotizado": "pendiente",
    "por_comprar": "pendiente",
    "comprado": "en_compras",
    "preparado": "en_transito",
    "embarcado": "en_transito",
    "en_bodega": "en_bodega",
    "despachado": "despachado",
    "reclamo": "reclamo",
}
_BUCKET_ORDER = ["pendiente", "en_compras", "en_transito", "en_bodega", "despachado", "reclamo"]


def _bucket_of(estado_linea):
    return _STATE_BUCKETS.get(estado_linea or "", "pendiente")


def _serialize_venta_card(c: MonzaCotizacion, cupos: Optional[dict] = None) -> dict:
    """Card de avance de una VENTA para el tablero de despachos (espejo de
    _serialize_oc_card de GA :315-413; aquí la cotización ES la venta).
    Sin plazo crítico por ítem: MonzaCotizacionItem.plazo_entrega es texto libre
    (String) y no se parsea — cuando exista un plazo estructurado por ítem, se
    espeja el worst-case de GA (_item_deadline).

    `cupos` (hallazgo #17): {item_id: qty despachable} precalculado en LOTE por el
    llamador. Con ese dato la etiqueta distingue 'listo' de 'en_espera_stock' — una
    línea puede seguir (correctamente) en_bodega porque le falta stock por llegar
    mientras lo recibido YA se despachó, y ahí no hay nada que despachar aunque la
    venta se pinte en la pestaña 'Listas'. `None` = comportamiento histórico (el
    detalle de avance no lo necesita: ya publica qty_disponible por ítem)."""
    items = c.items or []
    total = len(items)
    en_bodega = sum(1 for i in items if i.estado_linea == "en_bodega")
    con_cupo = None if cupos is None else sum(
        1 for i in items if i.estado_linea == "en_bodega" and cupos.get(i.id, 0) > 0)
    despachados = sum(1 for i in items if i.estado_linea == "despachado")
    progreso_estados = {b: 0 for b in _BUCKET_ORDER}
    for it in items:
        progreso_estados[_bucket_of(it.estado_linea)] += 1
    dias_restantes = None
    if c.fecha_entrega_est:
        try:
            dias_restantes = business_days_remaining(c.fecha_entrega_est)
        except Exception:
            dias_restantes = None
    if total and despachados == total:
        estado = "completado"
    elif despachados > 0 and en_bodega > 0:
        estado = "parcial"
    elif en_bodega > 0:
        # Hallazgo #17: en bodega SIN cupo despachable (lo recibido ya salió y el
        # resto de la línea todavía no llega) no es "listo": el encargado abría la
        # venta desde la pestaña Listas y no había nada que despachar. No se oculta
        # la venta — esconder una venta parcialmente en bodega esperando stock sería
        # peor —, se corrige la ETIQUETA.
        estado = "listo" if (con_cupo is None or con_cupo > 0) else "en_espera_stock"
    else:
        estado = "pendiente"
    return {
        "id": c.id,
        "numero": c.numero,
        "oc_cliente": c.oc_cliente,
        "oc_fecha": c.oc_fecha.isoformat() if c.oc_fecha else None,
        "fecha_venta": c.fecha_venta.isoformat() if c.fecha_venta else None,
        "fecha_entrega_est": c.fecha_entrega_est.isoformat() if c.fecha_entrega_est else None,
        "cliente": {"nombre": c.cliente.nombre, "rut": c.cliente.rut} if c.cliente else None,
        "vehiculo": c.vehiculo,
        "total_items": total,
        "items_en_bodega": en_bodega,
        # Ítems en bodega con cupo REAL despachable (None si el llamador no precargó
        # los cupos). Hallazgo #17: items_en_bodega solo cuenta estados de línea.
        "items_con_cupo": con_cupo,
        "items_despachados": despachados,
        "items_no_disponibles": total - en_bodega - despachados,
        "progreso_pct": int(despachados * 100 / total) if total else 0,
        "progreso_estados": progreso_estados,
        "estado": estado,
        "dias_restantes": dias_restantes,
    }


@router.get("/avance")
def avance_ventas(
    tab: str = Query("listas"),
    q: Optional[str] = Query(None),
    # page=None conserva la respuesta LEGADA (array pelado) para los consumidores
    # existentes; con page el sobre nuevo {items,total,...} trae paginación honesta.
    page: Optional[int] = Query(None, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Tablero de avance por venta: listas | en_curso | historial (espejo de
    /despachos/oc-clientes de GA :452-533). SOLO lectura, sin locks.
    'historial' = ventas con ≥1 despacho CERRADO (≠ cot.estado=='despachado': una
    venta parcialmente despachada aparece en historial aunque siga 'vendida')."""
    if tab not in ("listas", "en_curso", "historial"):
        raise HTTPException(400, f"tab inválido: {tab}")
    base = (
        db.query(MonzaCotizacion)
        .options(joinedload(MonzaCotizacion.cliente), joinedload(MonzaCotizacion.items))
        .filter(MonzaCotizacion.estado.in_(("vendida", "despachado")))
    )
    # Buscador con el MISMO _filtro_q del listado (la deuda era la divergencia:
    # aquí corría en Python sobre un haystack de 4 campos concatenados — "codelco
    # 4500" matcheaba si el cliente TERMINABA en codelco y la OC EMPEZABA con 4500
    # sin que ningún campo contuviera la frase — y str.lower() no ignora tildes,
    # al revés que la collation _ci del listado). La doble pasada se decide sobre
    # el resultado SQL completo (antes del filtro de pestaña): si hay coincidencias
    # en otra pestaña, el conteo cruzado las muestra y la pasada B no corre.
    nq = _normalizar_q(q)
    normalizado = False
    if nq["activo"]:
        cots = _filtro_q(base, nq).all()
        if not cots and _q_tiene_digitos(nq):
            normalizado = True
            cots = _filtro_q(base, nq, con_colapsado=True).all()
    else:
        cots = base.all()
    # Precarga en LOTE (anti N+1): qué ventas tienen despachos abiertos/cerrados.
    cot_ids = [c.id for c in cots]
    con_abierto, con_cerrado = set(), set()
    if cot_ids:
        for cot_id, estado in (
            db.query(MonzaDespacho.cotizacion_id, MonzaDespacho.estado)
            .filter(MonzaDespacho.cotizacion_id.in_(cot_ids),
                    MonzaDespacho.estado.in_(("en_preparacion", "despachado")))
            .all()
        ):
            (con_abierto if estado == "en_preparacion" else con_cerrado).add(cot_id)

    # Cupo real por ítem en bodega, en LOTE (hallazgo #17: sin esto la venta cuyo
    # cupo ya se consumió se etiquetaba 'listo'). 2 queries extra para TODO el
    # tablero, mismo patrón que /listos — nunca por venta.
    items_en_bod = [i for c in cots for i in (c.items or []) if i.estado_linea == "en_bodega"]
    recibidos = _qty_recibida_utilizable(db, [i.id for i in items_en_bod])
    ya = _ya_despachado_lote(db, cot_ids)
    cupos = {i.id: _cupo_disponible(i.id, i.cantidad, recibidos, ya) for i in items_en_bod}

    result = []
    cot_de_card = {}
    # Conteo CRUZADO de pestañas (spec, decisión 1/4): la pregunta real del
    # operador no es "filtrá esta lista" sino "dónde está esto" — se cuenta la
    # coincidencia en las TRES pestañas en la misma pasada, sin queries extra.
    tabs_count = {"listas": 0, "en_curso": 0, "historial": 0}
    for c in cots:
        card = _serialize_venta_card(c, cupos)
        en_listas = card["items_en_bodega"] > 0 and card["estado"] != "completado"
        if en_listas:
            tabs_count["listas"] += 1
        if c.id in con_abierto:
            tabs_count["en_curso"] += 1
        if c.id in con_cerrado:
            tabs_count["historial"] += 1
        if tab == "listas":
            if not en_listas:
                continue
        elif tab == "en_curso":
            if c.id not in con_abierto:
                continue
        else:  # historial
            if c.id not in con_cerrado:
                continue
        result.append(card)
        cot_de_card[card["id"]] = c
    # Urgente primero: fecha_entrega_est ascendente, sin fecha al final (espejo GA
    # :532) + desempate DETERMINISTA por id (sin él, con OFFSET, una fila puede
    # repetirse en la página 1 y perderse en la 2).
    result.sort(key=lambda card: (card["fecha_entrega_est"] is None,
                                  card["fecha_entrega_est"] or "", -card["id"]))
    if page is None:
        return result

    # Sobre nuevo (frontend del buscador): paginación honesta + insignias de motivo
    # calculadas SOLO sobre la página (≤ page_size filas).
    total = len(result)
    pagina = result[(page - 1) * page_size: (page - 1) * page_size + page_size]
    matches = _match_ventas(db, [cot_de_card[card["id"]] for card in pagina], nq, normalizado)
    for card in pagina:
        card["match"] = matches.get(card["id"], [])
    return {
        "items": pagina,
        "total": total,
        "page": page,
        "page_size": page_size,
        "q_efectivo": nq["q"] if nq["activo"] else "",
        "normalizado": normalizado,
        "tabs": tabs_count,
    }


@router.get("/avance/{cot_id}")
def avance_venta_detalle(cot_id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Detalle de avance de UNA venta: card + ítems (bucket, despachado, disponible)
    + despachos + embarques que trajeron sus ítems (espejo de oc_cliente_detail GA)."""
    c = (
        db.query(MonzaCotizacion)
        .options(joinedload(MonzaCotizacion.cliente), joinedload(MonzaCotizacion.items))
        .filter(MonzaCotizacion.id == cot_id)
        .first()
    )
    if not c:
        raise HTTPException(404, "Venta no encontrada")
    card = _serialize_venta_card(c)

    item_ids = [i.id for i in (c.items or [])]
    recibidos = _qty_recibida_utilizable(db, item_ids)
    ya = _qty_already_dispatched(db, c.id)
    cerrados = _qty_dispatched_closed(db, c.id)
    card["items"] = [{
        "id": i.id, "descripcion": i.descripcion, "numero_parte": i.numero_parte,
        "cantidad": i.cantidad, "estado_linea": i.estado_linea or "cotizado",
        "bucket": _bucket_of(i.estado_linea),
        "qty_despachada": cerrados.get(i.id, 0),
        "qty_disponible": (
            max(_tope_fisico(i, recibidos) - ya.get(i.id, 0), 0)
            if i.estado_linea == "en_bodega" else 0
        ),
    } for i in (c.items or [])]

    card["despachos"] = [
        _despacho_entidad_dict(db, d)
        for d in db.query(MonzaDespacho).filter(MonzaDespacho.cotizacion_id == c.id)
        .order_by(MonzaDespacho.id.desc()).all()
    ]

    embarques = []
    if item_ids:
        emb_ids = {
            r[0] for r in db.query(MonzaEmbarqueItem.embarque_id)
            .filter(MonzaEmbarqueItem.item_id.in_(item_ids)).all()
        }
        if emb_ids:
            counts = dict(
                db.query(MonzaEmbarqueItem.embarque_id, func.count(MonzaEmbarqueItem.id))
                .filter(MonzaEmbarqueItem.embarque_id.in_(emb_ids),
                        MonzaEmbarqueItem.item_id.in_(item_ids))
                .group_by(MonzaEmbarqueItem.embarque_id).all()
            )
            for e in db.query(MonzaEmbarque).filter(MonzaEmbarque.id.in_(emb_ids)).all():
                embarques.append({
                    "id": e.id, "numero": e.numero, "estado": e.estado,
                    "awb": e.awb, "forwarder": e.forwarder, "tracking": e.tracking,
                    # String(30) libre en el modelo: se entrega tal cual, sin parseo
                    "fecha_llegada_est": e.fecha_llegada_est,
                    "items_de_esta_venta": counts.get(e.id, 0),
                })
    card["embarques"] = embarques
    return card


@router.get("/counts")
def counts_despachos(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Contadores del tablero (espejo de /despachos/counts GA :417-449).

    Hallazgo #17: 'listo' es lo que se puede DESPACHAR, no solo el estado de línea.
    Un ítem en_bodega cuyo cupo ya se consumió (llegó parcial y esas unidades ya
    salieron) inflaba el contador y mandaba al encargado a una venta vacía. Se
    descuenta el cupo con la misma precarga en LOTE de /listos (2 queries extra
    sobre los ítems en bodega, sin N+1). 'despachados' no cambia."""
    filas = (
        db.query(MonzaCotizacionItem.cotizacion_id, MonzaCotizacionItem.id,
                 MonzaCotizacionItem.cantidad, MonzaCotizacionItem.estado_linea)
        .join(MonzaCotizacion, MonzaCotizacion.id == MonzaCotizacionItem.cotizacion_id)
        .filter(MonzaCotizacion.estado.in_(("vendida", "despachado")))
        .all()
    )
    en_bod = [f for f in filas if f[3] == "en_bodega"]
    recibidos = _qty_recibida_utilizable(db, [f[1] for f in en_bod])
    ya = _ya_despachado_lote(db, {f[0] for f in en_bod})
    con_cupo = [f for f in en_bod if _cupo_disponible(f[1], f[2], recibidos, ya) > 0]
    return {
        "ventas_listas": len({f[0] for f in con_cupo}),
        "items_listos": len(con_cupo),
        "items_despachados": sum(1 for f in filas if f[3] == "despachado"),
    }


@router.get("/listos")
def listos_despacho(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Cotizaciones con >=1 item en_bodega con cupo DISPONIBLE para despachar.

    disponible = min(vendido, recibido en bodega) − ya reservado/despachado.
    Un ítem con todo su cupo consumido por borradores o despachos cerrados ya no
    aparece (antes se ofrecía la cantidad completa siempre)."""
    cots = (
        db.query(MonzaCotizacion)
        .options(joinedload(MonzaCotizacion.cliente), joinedload(MonzaCotizacion.items))
        .filter(MonzaCotizacion.estado == "vendida")
        .all()
    )
    # Precarga en LOTE (anti N+1, patrón GA): recibidos y reservados de TODOS los
    # ítems en bodega en 3 queries totales, no 2 por cotización.
    todos_en_bod = [i for c in cots for i in c.items if i.estado_linea == "en_bodega"]
    recibidos = _qty_recibida_utilizable(db, [i.id for i in todos_en_bod])
    cot_ids = list({c.id for c in cots})
    ya_global: dict = {}
    if cot_ids:
        rows = (
            db.query(MonzaDespachoItem.item_id, MonzaDespachoItem.qty_despachada)
            .join(MonzaDespacho, MonzaDespacho.id == MonzaDespachoItem.despacho_id)
            .filter(MonzaDespacho.cotizacion_id.in_(cot_ids), MonzaDespacho.estado != "anulado")
            .all()
        )
        for item_id, qty in rows:
            ya_global[item_id] = ya_global.get(item_id, 0) + (qty or 0)
    out = []
    for c in cots:
        en_bod = [i for i in c.items if i.estado_linea == "en_bodega"]
        if not en_bod:
            continue
        ya = ya_global
        items_disp = []
        for i in en_bod:
            disponible = max(_tope_fisico(i, recibidos) - ya.get(i.id, 0), 0)
            if disponible <= 0:
                continue
            items_disp.append({
                "id": i.id, "descripcion": i.descripcion, "numero_parte": i.numero_parte,
                "cantidad": i.cantidad, "qty_disponible": disponible,
            })
        if not items_disp:
            continue
        total = len(c.items)
        out.append({
            "id": c.id, "numero": c.numero,
            "cliente": c.cliente.nombre if c.cliente else None,
            "vehiculo": c.vehiculo, "total_items": total, "en_bodega": len(items_disp),
            "listo_completo": len(items_disp) == total, "total_bruto": c.total_bruto,
            "items": items_disp,
        })
    return out


class CrearDespachoItem(BaseModel):
    item_id: int
    # int (la columna qty_despachada es Integer: un float como 2.5 se redondearía en
    # silencio en MySQL). None = sentinela de la vía legada (item_ids): "todo el
    # disponible". En la vía nueva, qty <= 0 se rechaza con 400 (espejo GA).
    qty: Optional[int] = None


class CrearDespachoBody(BaseModel):
    cotizacion_id: int
    # Vía nueva (parcial): items con cantidad explícita. Vía legada: item_ids solos
    # → se despacha el DISPONIBLE completo de cada ítem (no la cantidad vendida:
    # ese era el bug que permitía despachar más de lo recibido).
    item_ids: List[int] = []
    items: Optional[List[CrearDespachoItem]] = None
    numero_guia: str = ""      # flujo guía-primero: puede (y suele) venir vacío
    transportista: str = ""
    destinatario: str = ""
    direccion_entrega: str = ""
    observaciones: str = ""


@router.post("/crear")
def crear_despacho(body: CrearDespachoBody, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    # El correlativo DSP-AAAA-#### se calcula sin lock: si dos despachos simultáneos
    # chocan en el UNIQUE de numero, se recalcula y reintenta (máx. 3) — espejo GA.
    # También reintenta deadlock 1213/1205: crear lockea ítems y luego filas de
    # despacho, mientras cerrar/anular lockean el despacho primero (orden inverso).
    for _ in range(3):
        try:
            return _crear_despacho_tx(db, body, current_user)
        except IntegrityError as e:
            db.rollback()
            if "numero" not in str(getattr(e, "orig", e)):
                raise
        except OperationalError as e:
            db.rollback()
            code = getattr(getattr(e, "orig", None), "args", [None])[0]
            if code not in (1213, 1205):
                raise
    raise HTTPException(409, "No se pudo crear el despacho (creación simultánea): reintenta")


# ── Cortafuego de ADELANTO SIN VERIFICAR en las puertas de SALIDA (2026-08-22) ──
# El predicado es el MISMO que frena la OC de proveedor en Abastecimiento
# (monza_router_abastecimiento.py): una venta que exige adelanto (pct_adelanto > 0,
# incluido el Contado = 100%) y cuyo pago Tesorería todavía no verificó.
#
# POR QUÉ TAMBIÉN ACÁ: hasta ahora solo se frenaba la COMPRA, y el resto quedaba
# cubierto DE REBOTE por el camino físico (sin compra no hay mercadería que despachar).
# Pero mercadería que llega a bodega por otra vía —una reposición, el remanente de otra
# línea— podía salir despachada, con guía al SII y facturada, con el pago pendiente.
# Se replica el helper en cada módulo (patrón ESTADOS_VENTA de la casa) en vez de
# importarlo: acoplar los módulos aislados por un predicado de 3 líneas sale más caro
# que mantener las copias con este comentario.
def _adelanto_sin_verificar(cot) -> bool:
    return (int(getattr(cot, "pct_adelanto", 0) or 0) > 0
            and not int(getattr(cot, "adelanto_verificado", 0) or 0))


def _crear_despacho_tx(db: Session, body: CrearDespachoBody, current_user):
    cot = db.query(MonzaCotizacion).options(joinedload(MonzaCotizacion.cliente)).filter(MonzaCotizacion.id == body.cotizacion_id).first()
    if not cot:
        raise HTTPException(404, "Cotización no encontrada")
    if _adelanto_sin_verificar(cot):
        raise HTTPException(
            409,
            f"Adelanto no verificado por Tesorería en {cot.numero} "
            f"(adelanto {int(cot.pct_adelanto or 0)}%): no se despacha mercadería con el "
            f"pago pendiente. Tesorería debe registrar el pago recibido.",
        )

    pedidos = body.items if body.items else [CrearDespachoItem(item_id=i) for i in body.item_ids]
    if not pedidos:
        raise HTTPException(400, "Debe incluir al menos 1 ítem")
    ids_pedidos = [p.item_id for p in pedidos]
    # Una línea por ítem: dos líneas del mismo ítem burlarían la validación de
    # disponible (cada una se compararía sola contra el total).
    if len(set(ids_pedidos)) != len(ids_pedidos):
        raise HTTPException(400, "Hay ítems repetidos en el despacho: consolida la cantidad en una sola línea")

    # FOR UPDATE sobre los ítems: serializa despachos concurrentes de la misma venta.
    # populate_existing(): sin él, el identity map devolvería datos del snapshot viejo.
    # La pertenencia a la cotización se valida en la MISMA query (filtro cotizacion_id).
    items_db = (
        db.query(MonzaCotizacionItem)
        .filter(
            MonzaCotizacionItem.id.in_(ids_pedidos),
            MonzaCotizacionItem.cotizacion_id == cot.id,
        )
        .order_by(MonzaCotizacionItem.id.asc())
        .populate_existing()
        .with_for_update()
        .all()
    )
    if len(items_db) != len(set(ids_pedidos)):
        raise HTTPException(400, "Hay ítems que no pertenecen a esta cotización")
    items_by_id = {i.id: i for i in items_db}

    ya = _qty_already_dispatched(db, cot.id, con_lock=True)
    recibidos = _qty_recibida_utilizable(db, ids_pedidos)

    lineas = []   # (item, qty a despachar)
    for p in pedidos:
        it = items_by_id[p.item_id]
        if it.estado_linea != "en_bodega":
            raise HTTPException(400, f"Ítem '{it.descripcion}' no está en bodega (estado: {it.estado_linea})")
        if p.qty is not None and p.qty <= 0:
            # Espejo GA: una cantidad explícita inválida es error del cliente, no
            # "todo el disponible" (eso reservaría cupo en silencio).
            raise HTTPException(400, f"Cantidad inválida para '{it.descripcion}'")
        tope = _tope_fisico(it, recibidos)
        disponible = tope - ya.get(it.id, 0)
        qty = p.qty if p.qty is not None else disponible   # None = vía legada: todo el disponible
        if qty <= 0:
            raise HTTPException(400, f"Sin cupo disponible para '{it.descripcion}' (ya reservado/despachado)")
        if qty > disponible + 0.001:
            if tope < (it.cantidad or 0):
                raise HTTPException(
                    400,
                    f"Cantidad excede lo RECIBIDO en bodega para '{it.descripcion}' "
                    f"(recibido: {tope}, disponible: {max(disponible, 0)})",
                )
            raise HTTPException(400, f"Cantidad excede disponible para '{it.descripcion}' (disp: {disponible})")
        lineas.append((it, qty))

    dsp = MonzaDespacho(
        numero=_gen_num_desp(db), cotizacion_id=cot.id,
        cliente_nombre=cot.cliente.nombre if cot.cliente else None,
        numero_guia=body.numero_guia or None, transportista=body.transportista or None,
        destinatario=body.destinatario or None, direccion_entrega=body.direccion_entrega or None,
        observaciones=body.observaciones or None,
        estado="en_preparacion",   # borrador: la línea NO voltea hasta CERRAR
        asesor_email=current_user.email,
    )
    db.add(dsp); db.flush()
    for it, qty in lineas:
        db.add(MonzaDespachoItem(despacho_id=dsp.id, item_id=it.id, qty_despachada=qty))
    # UN solo commit con el log adentro (patrón GA): nada de trabajo después del
    # commit dentro de una función reintentada — un fallo posterior re-entraría la
    # tx sobre un estado ya commiteado y devolvería un error falso por un éxito.
    from monza_models import MonzaLog
    db.add(MonzaLog(user_email=current_user.email, accion="CREATE", entidad="despacho", entidad_id=dsp.id, entidad_ref=dsp.numero, detalle=f"Despacho {dsp.numero} creado (en preparación) · {len(lineas)} ítem(s) · {cot.numero}"))
    db.commit()
    return {"ok": True, "id": dsp.id, "numero": dsp.numero, "items": len(lineas), "estado": "en_preparacion"}


def _despacho_entidad_dict(db: Session, d: MonzaDespacho, con_items: bool = False) -> dict:
    cot = db.query(MonzaCotizacion).filter(MonzaCotizacion.id == d.cotizacion_id).first() if d.cotizacion_id else None
    out = {
        "id": d.id, "numero": d.numero, "cotizacion_id": d.cotizacion_id,
        "cotizacion_numero": cot.numero if cot else None,
        "cliente_nombre": d.cliente_nombre, "numero_guia": d.numero_guia,
        "fecha_guia": d.fecha_guia.isoformat() if d.fecha_guia else None,
        "transportista": d.transportista, "destinatario": d.destinatario,
        "direccion_entrega": d.direccion_entrega, "observaciones": d.observaciones,
        "estado": d.estado, "numero_expedicion": d.numero_expedicion,
        "fecha": d.fecha.isoformat() if d.fecha else None,
        "fecha_despacho": d.fecha_despacho.isoformat() if d.fecha_despacho else None,
        # Firma de la guía: desde 2026-08-06 gatea la facturación (ver firmar_despacho).
        "guia_firmada": bool(d.guia_firmada),
        "fecha_firma": d.fecha_firma.isoformat() if d.fecha_firma else None,
        "guia_firmada_archivo": d.guia_firmada_archivo,
    }
    if con_items:
        dis = db.query(MonzaDespachoItem).filter(MonzaDespachoItem.despacho_id == d.id).all()
        its = {
            i.id: i for i in db.query(MonzaCotizacionItem)
            .filter(MonzaCotizacionItem.id.in_([x.item_id for x in dis] or [0])).all()
        }
        out["items"] = [{
            "id": di.id, "item_id": di.item_id, "qty_despachada": di.qty_despachada,
            "descripcion": its[di.item_id].descripcion if di.item_id in its else None,
            "numero_parte": its[di.item_id].numero_parte if di.item_id in its else None,
            "cantidad_vendida": its[di.item_id].cantidad if di.item_id in its else None,
        } for di in dis]
    else:
        out["items_count"] = db.query(func.count(MonzaDespachoItem.id)).filter(MonzaDespachoItem.despacho_id == d.id).scalar() or 0
    return out


@router.get("/entidades")
def list_despachos_entidades(db: Session = Depends(get_db), _=Depends(get_current_user)):
    # Los borradores (en_preparacion) SIEMPRE completos + los cerrados con la guía SIN
    # FIRMAR también SIEMPRE completos + los últimos 100 del resto. El criterio es el
    # mismo para ambos grupos: una fila que EXIGE acción no puede desaparecer del único
    # panel que permite ejecutarla — el borrador espera confirmarse/anularse, y el
    # cerrado sin firmar espera "Marcar guía firmada", sin la cual NO SE FACTURA
    # (regla 2026-08-06). Antes el corte de 100 (contando anulados) sacaba de la vista
    # justo las guías que llevan semanas esperando la firma: quedaban infacturables
    # desde la UI (hallazgo HIGH del multienjambre 2026-08-07).
    # coalesce: filas legadas con guia_firmada NULL cuentan como sin firmar.
    abiertos = (
        db.query(MonzaDespacho).filter(MonzaDespacho.estado == "en_preparacion")
        .order_by(MonzaDespacho.id.desc()).all()
    )
    # Tope defensivo del grupo sin firmar: al ACTIVAR la regla, todo el histórico
    # cerrado nace con guia_firmada=0 y sin tope la respuesta traería la tabla entera
    # (panel inusable + N consultas de estado SII en tandas). 500 es holgadamente más
    # que cualquier backlog real de guías pendientes de firma, así que el arreglo del
    # hallazgo HIGH sigue en pie; los más NUEVOS primero (id DESC), que son los que
    # esperan firma de verdad — los legados viejos ya facturados no exigen acción.
    sin_firmar = (
        db.query(MonzaDespacho)
        .filter(MonzaDespacho.estado == "despachado",
                func.coalesce(MonzaDespacho.guia_firmada, 0) != 1)
        .order_by(MonzaDespacho.id.desc()).limit(500).all()
    )
    resto = (
        db.query(MonzaDespacho)
        .filter(MonzaDespacho.estado != "en_preparacion",
                or_(MonzaDespacho.estado != "despachado",
                    func.coalesce(MonzaDespacho.guia_firmada, 0) == 1))
        .order_by(MonzaDespacho.id.desc()).limit(100).all()
    )
    # DEDUP por id: los 3 grupos se leen en 3 SELECTs y bajo READ COMMITTED una
    # transición commiteada entre medio (confirmar un borrador, firmar una guía)
    # puede hacer que la MISMA fila caiga en dos grupos → la respuesta traía la fila
    # duplicada y React pintaba dos <tr> con la misma key.
    ds, vistos = [], set()
    for d in abiertos + sin_firmar + resto:
        if d.id not in vistos:
            vistos.add(d.id)
            ds.append(d)
    # Precarga en LOTE (anti N+1): números de cotización y conteo de ítems en 2 queries.
    cot_nums = {}
    cids = list({d.cotizacion_id for d in ds if d.cotizacion_id})
    if cids:
        cot_nums = dict(
            db.query(MonzaCotizacion.id, MonzaCotizacion.numero)
            .filter(MonzaCotizacion.id.in_(cids)).all()
        )
    counts = {}
    dids = [d.id for d in ds]
    if dids:
        counts = dict(
            db.query(MonzaDespachoItem.despacho_id, func.count(MonzaDespachoItem.id))
            .filter(MonzaDespachoItem.despacho_id.in_(dids))
            .group_by(MonzaDespachoItem.despacho_id).all()
        )
    out = []
    for d in sorted(ds, key=lambda x: x.id, reverse=True):
        out.append({
            "id": d.id, "numero": d.numero, "cotizacion_id": d.cotizacion_id,
            "cotizacion_numero": cot_nums.get(d.cotizacion_id),
            "cliente_nombre": d.cliente_nombre, "numero_guia": d.numero_guia,
            "fecha_guia": d.fecha_guia.isoformat() if d.fecha_guia else None,
            "transportista": d.transportista, "destinatario": d.destinatario,
            "direccion_entrega": d.direccion_entrega, "observaciones": d.observaciones,
            "estado": d.estado, "numero_expedicion": d.numero_expedicion,
            "fecha": d.fecha.isoformat() if d.fecha else None,
            "fecha_despacho": d.fecha_despacho.isoformat() if d.fecha_despacho else None,
            # Firma de la guía: el panel "Despachos en curso" pinta con esto el badge
            # "firmada ✓" o el botón "Marcar guía firmada" (solo en 'despachado').
            "guia_firmada": bool(d.guia_firmada),
            "fecha_firma": d.fecha_firma.isoformat() if d.fecha_firma else None,
            "guia_firmada_archivo": d.guia_firmada_archivo,
            "items_count": counts.get(d.id, 0),
        })
    return out


@router.get("/entidades/{despacho_id}")
def get_despacho_entidad(despacho_id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    d = db.query(MonzaDespacho).filter(MonzaDespacho.id == despacho_id).first()
    if not d:
        raise HTTPException(404, "Despacho no encontrado")
    return _despacho_entidad_dict(db, d, con_items=True)


def _guia_electronica_activa(db: Session, despacho_id: int, con_lock: bool = False,
                             incluir_ambiguo: bool = False):
    """Guía electrónica (SII 52) VIVA del despacho: emitida, en vuelo (claim) o en
    proceso en Wasabil. Mientras exista, las operaciones que la dejarían huérfana o
    pisarían su folio (anular el despacho, editar el N° de guía a mano) se rechazan:
    el folio es un documento tributario legal y su anulación se gestiona en Wasabil.
    Un DTE FALLIDO (rechazado por el SII, sin folio) no bloquea nada.

    `con_lock=True` (anular): lee con FOR UPDATE + populate_existing para ver el
    claim/estado COMMITEADO más reciente aunque la transacción tenga un snapshot
    anterior — cierra la carrera anular ↔ emitir.

    `incluir_ambiguo=True` (SOLO anular, hallazgos #2/#3 de la auditoría integral):
    además cuenta como viva la emisión AMBIGUA — uuid NULL pero `en_vuelo_desde`
    puesto — aunque su claim ya haya VENCIDO por TTL. Ahí Wasabil cortó la
    comunicación sin confirmar nada: el documento PUDO nacer con folio real en el
    SII. Decidir por TTL dejaba anular el despacho, liberaba la mercadería y
    habilitaba una SEGUNDA guía 52 por lo mismo (irreversible), además de dejar el
    documento sin recuperación. Es el mismo criterio de _bloqueo_dte_factura
    (monza_contabilidad/router.py). El fallo CONFIRMADO no queda atrapado:
    _emitir_en_wasabil limpia `en_vuelo_desde` cuando el error NO es ambiguo, así
    que un DTE fallido sigue permitiendo anular (el despacho nunca es imborrable).
    Por defecto False: el PUT de numero_guia (_rechazar_si_pisa_folio) y la señal
    informativa del cierre (_guia_sii_en_proceso) NO son irreversibles y ahí el
    criterio por TTL es el correcto — no se deben endurecer.

    Espejo de routers/despachos.py:_guia_electronica_activa de GA sobre el modelo
    Monza. Import LOCAL del paquete monza_wasabil_dte para no crear un ciclo de
    imports con el módulo DTE (y no cargarlo en los caminos que no lo necesitan)."""
    from sqlalchemy.exc import ProgrammingError
    from monza_wasabil_dte.models import (
        MonzaWasabilDte, STATUS_EMITIDO, STATUS_PROCESANDO, STATUS_PENDIENTE,
    )
    from monza_wasabil_dte.service import TIPO_DOC_GUIA, claim_vigente
    q = (
        db.query(MonzaWasabilDte)
        .filter(MonzaWasabilDte.despacho_id == despacho_id,
                MonzaWasabilDte.tipo_dte == TIPO_DOC_GUIA)
    )
    if con_lock:
        q = q.populate_existing().with_for_update()
    try:
        dte = q.first()
    except ProgrammingError as e:
        # Tabla monza_wasabil_dte inexistente (MySQL 1146): deploy sin correr el
        # init_db del módulo. Sin tabla JAMÁS hubo emisión que proteger → el guard
        # se apaga solo en vez de tumbar anular/editar con 500 (hallazgo del
        # multienjambre F5). Cualquier otro error de programación sí propaga.
        # OJO: NO se cortocircuita por MONZA_CONTAB_ENABLED — apagar el gate con
        # guías ya emitidas dejaría anular una guía SII viva.
        if getattr(getattr(e, "orig", None), "args", [None])[0] == 1146:
            db.rollback()
            return None
        raise
    if not dte:
        return None
    if dte.status_id == STATUS_EMITIDO:
        return dte
    if claim_vigente(dte):
        return dte
    if dte.uuid and dte.status_id in (STATUS_PROCESANDO, STATUS_PENDIENTE):
        return dte
    # Emisión AMBIGUA con el claim ya vencido (hallazgos #2/#3): sin uuid no sabemos
    # si el documento existe, pero `en_vuelo_desde` dice que el POST salió y nadie
    # confirmó el resultado. Solo bloquea a quien lo pide (anular).
    if incluir_ambiguo and dte.uuid is None and dte.en_vuelo_desde is not None:
        return dte
    return None


def _guia_sii_en_proceso(db: Session, despacho_id: int, con_lock: bool = False) -> bool:
    """True si el despacho tiene guía electrónica 52 VIVA pero TODAVÍA SIN FOLIO
    (pendiente/procesando en Wasabil, o claim de emisión en vuelo).

    Es la ventana peligrosa de la Fase 6: en ella `numero_guia` aún conserva lo que
    el operador haya tecleado a mano (el módulo Wasabil lo pisa con el folio real
    recién al confirmarse la emisión, monza_wasabil_dte/router.py), así que una
    factura 33 emitida en esa ventana referenciaría al SII un folio 52 que el SII
    NO reconoce — y eso es irreversible.

    Con folio ya asignado (guía emitida) devuelve False: ahí `numero_guia` ES el
    folio real y la referencia 52 de la factura es legítima. Una guía FALLIDA sin
    uuid ni claim tampoco está "en proceso" (no hay guía electrónica y el N° manual
    vuelve a ser la referencia válida): esa direccionalidad la resuelve
    _guia_electronica_activa y NO se debe invertir.

    Señal INFORMATIVA para el cierre (ver _cerrar_despacho_tx). El guard que
    BLOQUEA de verdad vive en la emisión de la factura 33
    (_guia_electronica_en_proceso, monza_wasabil_dte/router.py), que es donde
    ocurre la acción irreversible. Espejo del espíritu de GA (commit a0d4671)."""
    dte = _guia_electronica_activa(db, despacho_id, con_lock=con_lock)
    return bool(dte and not (dte.folio or "").strip())


def _rechazar_si_pisa_folio(db: Session, d: MonzaDespacho, numero_guia_nuevo: Optional[str]):
    """Bloquea el cambio manual de numero_guia cuando el despacho tiene guía
    electrónica viva (el N° correcto es el folio del SII, lo escribe el módulo
    Wasabil). Dejar pasar el mismo valor actual es inofensivo (no-op).

    Espejo de routers/despachos.py:_rechazar_si_pisa_folio de GA."""
    if (numero_guia_nuevo or "") == (d.numero_guia or ""):
        return
    dte = _guia_electronica_activa(db, d.id)
    if not dte:
        return
    if dte.folio:
        raise HTTPException(
            409,
            f"Este despacho tiene guía electrónica SII emitida (folio {dte.folio}): "
            "el N° de guía no se edita a mano",
        )
    raise HTTPException(
        409,
        "Este despacho tiene una emisión de guía SII en curso: el N° de guía "
        "quedará fijado por el folio del SII al terminar",
    )


class ActualizarDespachoBody(BaseModel):
    numero_guia: Optional[str] = None
    # Fecha de EMISIÓN de la guía ante el SII (YYYY-MM-DD), sólo para guía EN PAPEL.
    # Tri-estado gracias a exclude_unset: ausente = no tocar · "2026-07-15" = fijar ·
    # null/"" = borrar. Ver _parse_fecha_guia y monza_models.MonzaDespacho.fecha_guia.
    fecha_guia: Optional[str] = None
    transportista: Optional[str] = None
    numero_expedicion: Optional[str] = None
    destinatario: Optional[str] = None
    direccion_entrega: Optional[str] = None
    observaciones: Optional[str] = None


@router.put("/entidades/{despacho_id}")
def actualizar_despacho(despacho_id: int, body: ActualizarDespachoBody, db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Cabecera editable incluso cerrado: el transportista y el N° de expedición
    suelen llegar DESPUÉS del despacho (flujo guía-primero). Con guía electrónica
    SII viva (Fase 5) el folio queda protegido: _rechazar_si_pisa_folio bloquea
    solo el cambio manual de numero_guia, el resto de la cabecera sigue editable."""
    # FOR UPDATE + populate_existing (mismo cierre del TOCTOU que firmar_despacho):
    # sin el lock, la adopción del folio SII ("Reintentar" sobre una emisión ambigua)
    # podía intercalarse entre el guard del folio y el commit, y el N° manual la
    # pisaba dejando el estado incorregible por API.
    d = (
        db.query(MonzaDespacho)
        .filter(MonzaDespacho.id == despacho_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if not d:
        raise HTTPException(404, "Despacho no encontrado")
    if d.estado == "anulado":
        raise HTTPException(400, "No se puede editar un despacho anulado")
    cambios = body.dict(exclude_unset=True)
    if "numero_guia" in cambios:
        if len((cambios["numero_guia"] or "").strip()) > 100:
            # Columna String(100): mejor un 400 claro que el DataError del commit.
            raise HTTPException(400, "El N° de guía no puede superar los 100 caracteres")
        _rechazar_si_pisa_folio(db, d, cambios["numero_guia"])
    # fecha_guia llega como texto y la columna es Date: se convierte y valida ANTES del
    # setattr genérico (si no, SQLAlchemy guardaría el string crudo). Se saca del dict para
    # que el bucle de abajo no la pise con el valor sin parsear.
    if "fecha_guia" in cambios:
        nueva_fecha_guia = _parse_fecha_guia(cambios.pop("fecha_guia"))
        # Invariante de la firma (regla 2026-08-06): firmar exige fecha_firma >= fecha
        # de emisión. Editar la emisión DESPUÉS de la firma no puede romperlo por la
        # puerta de atrás — si la fecha buena de la guía es posterior a la firma, lo
        # que está mal es la firma: se corrige re-firmando (y ahí ambas quedan sanas).
        if (nueva_fecha_guia and d.guia_firmada and d.fecha_firma
                and nueva_fecha_guia > d.fecha_firma.date()):
            raise HTTPException(
                400,
                f"La guía quedó FIRMADA el {d.fecha_firma.date().isoformat()}: una fecha de "
                f"emisión posterior ({nueva_fecha_guia.isoformat()}) es inconsistente. "
                "Corrige primero la firma (Marcar guía firmada → Corregir) y luego la emisión.",
            )
        d.fecha_guia = nueva_fecha_guia
    for field, value in cambios.items():
        setattr(d, field, value)
    db.commit()
    return {"ok": True}


@router.post("/entidades/{despacho_id}/cerrar")
def cerrar_despacho(despacho_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    # Retry 1213/1205 (regla de la casa): cerrar toma locks (despacho + ítems) y puede
    # caer víctima de un deadlock contra crear/anular concurrentes.
    for _ in range(3):
        try:
            return _cerrar_despacho_tx(db, despacho_id, current_user)
        except OperationalError as e:
            db.rollback()
            code = getattr(getattr(e, "orig", None), "args", [None])[0]
            if code not in (1213, 1205):
                raise
    raise HTTPException(409, "Conflicto de concurrencia al cerrar el despacho: reintenta")


def _cerrar_despacho_tx(db: Session, despacho_id: int, current_user):
    d = (
        db.query(MonzaDespacho)
        .filter(MonzaDespacho.id == despacho_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if not d:
        raise HTTPException(404, "Despacho no encontrado")
    if d.estado != "en_preparacion":
        raise HTTPException(400, "Solo se pueden cerrar despachos en preparación")

    # ── DECISIÓN FASE 6 · guía 52 en proceso: el cierre NO se bloquea ──────────
    # La trampa: cerrar deja el despacho 'despachado', que es el ÚNICO estado
    # facturable, mientras la guía electrónica sigue sin folio y `numero_guia`
    # conserva el N° tecleado VIEJO. De ahí saldría una factura 33 REAL con una
    # referencia 52 a un folio que el SII no reconoce (irreversible).
    #
    # Se evaluaron las dos salidas y se eligió la de MENOR RIESGO OPERATIVO:
    #   (A) BLOQUEAR el cierre mientras la guía no tenga folio — DESCARTADA. El
    #       cierre registra un HECHO FÍSICO (la mercadería salió) y quedaría
    #       rehén de un sistema externo: un DTE atascado en pendiente(6) o en un
    #       fallo ambiguo dejaría el despacho imposible de cerrar, la línea sin
    #       voltear a 'despachado', la venta sin cerrar y la mercadería trabada,
    #       sin escape para bodega. Anular tampoco es salida (ya está bloqueado
    #       con guía viva, y con razón: destruiría un documento tributario).
    #   (B) PERMITIR el cierre y bloquear la EMISIÓN de la factura 33 mientras la
    #       guía esté en proceso — ELEGIDA. El guard vive donde ocurre la acción
    #       irreversible (monza_wasabil_dte/router.py: _guia_electronica_en_proceso,
    #       en el preview y en el armado del payload), con una salida natural:
    #       esperar el folio, que llega solo. Nada queda trabado en bodega.
    #       Es exactamente lo que hizo Grupo AM en el commit a0d4671.
    #
    # Desde acá se aporta la SEÑAL, no el candado: se avisa al operador (respuesta,
    # log y notificación) para que no intente facturar todavía. Si esta señal se
    # perdiera, el sistema sigue siendo seguro — el candado del 33 es el que manda.
    #
    # Lectura BAJO LOCK y ANTES de mutar nada: el orden despacho → dte es el mismo
    # de _anular_despacho_tx y de _reclamar_emision (anti-deadlock). Si la tabla del
    # módulo DTE no existiera (deploy sin init_db), el helper hace rollback y
    # devuelve None: como todavía no se ha mutado nada, basta con re-exigir el
    # estado para que un lock perdido no permita un doble cierre.
    guia_en_proceso = _guia_sii_en_proceso(db, d.id, con_lock=True)
    if d.estado != "en_preparacion":
        raise HTTPException(400, "Solo se pueden cerrar despachos en preparación")
    aviso = None
    if guia_en_proceso:
        aviso = ("La guía electrónica SII de este despacho aún no tiene folio "
                 "(emisión en proceso): espera a que quede Emitida antes de facturar.")

    dis = db.query(MonzaDespachoItem).filter(MonzaDespachoItem.despacho_id == d.id).all()
    item_ids = sorted({di.item_id for di in dis if di.item_id})
    items_db = []
    if item_ids:
        # Orden id ASC = orden canónico de lock de la casa (anti-deadlock).
        items_db = (
            db.query(MonzaCotizacionItem)
            .filter(MonzaCotizacionItem.id.in_(item_ids))
            .order_by(MonzaCotizacionItem.id.asc())
            .populate_existing()
            .with_for_update()
            .all()
        )

    # ── Cortafuego de adelanto SIN VERIFICAR, también al CERRAR ──────────────────
    # Crear el despacho es un borrador; CERRARLO es el momento en que la mercadería
    # sale. Con el guard solo en la creación quedaba una vía real: crear el despacho
    # con el pago verificado y cerrarlo después de que el adelanto se anulara (o
    # cerrar un borrador que venía de antes). Se lee con FOR UPDATE porque acá mismo
    # se decide, más abajo, si la venta pasa a 'despachado' (y con ella el LTV).
    cot_guard = (db.query(MonzaCotizacion)
                 .filter(MonzaCotizacion.id == d.cotizacion_id)
                 .populate_existing().with_for_update().first())
    if cot_guard is not None and _adelanto_sin_verificar(cot_guard):
        raise HTTPException(
            409,
            f"Adelanto no verificado por Tesorería en {cot_guard.numero} "
            f"(adelanto {int(cot_guard.pct_adelanto or 0)}%): no se cierra el despacho "
            f"—la mercadería sale— con el pago pendiente. Tesorería debe registrar el "
            f"pago recibido.",
        )

    # CERRAR PRIMERO y flush: la cobertura de abajo cuenta SOLO despachos cerrados
    # (incluido este). La línea voltea a 'despachado' únicamente cuando queda
    # cubierta completa — un cierre parcial deja el remanente en bodega.
    d.estado = "despachado"
    d.fecha_despacho = datetime.utcnow()
    db.flush()

    qty_total = _qty_dispatched_closed(db, d.cotizacion_id, con_lock=True)
    for it in items_db:
        if it.estado_linea == "en_bodega" and qty_total.get(it.id, 0) + 0.001 >= (it.cantidad or 0):
            it.estado_linea = "despachado"

    # La VENTA queda 'despachada' solo cuando TODAS sus líneas lo están (igual que
    # el flujo histórico, pero ahora la condición se evalúa al cerrar).
    #
    # FOR UPDATE (2026-08-22): antes se leía sin lock mientras el PATCH administrativo
    # SÍ la bloquea. Dos caminos concurrentes podían decidir ambos «transición nueva» y
    # sumar el LTV dos veces — plata real duplicada. Sin joinedload en la query del
    # lock: un FOR UPDATE con join lockea también las filas de los ítems y ensucia el
    # orden de locks (los ítems ya están bloqueados más arriba); `cot.items` se resuelve
    # igual por lazy load.
    cot = (db.query(MonzaCotizacion)
           .filter(MonzaCotizacion.id == d.cotizacion_id)
           .populate_existing().with_for_update().first())
    venta_recien_despachada = False
    if cot:
        db.flush()
        if cot.items and all(i.estado_linea == "despachado" for i in cot.items):
            # La TRANSICIÓN es el guard de idempotencia: si la venta ya estaba en
            # 'despachado' (el PATCH ganó la carrera), no se vuelven a aplicar efectos.
            venta_recien_despachada = cot.estado != "despachado"
            cot.estado = "despachado"
            if not cot.fecha_despacho:
                # Día de CHILE (ver monza_fechas): con utcnow().date() un cierre de
                # las 21:30 quedaba fechado MAÑANA en una columna civil.
                cot.fecha_despacho = hoy_chile()
            if venta_recien_despachada:
                # LTV al cliente facturado + cierre del lead. Este es el camino REAL de
                # la operación (el encargado cierra el último despacho): hasta hoy no
                # aplicaba ningún efecto de venta y el PATCH posterior ya no podía
                # hacerlo — el LTV casi nunca se sumaba. Import local: monza_router_
                # cotizaciones importa monza_models, traerlo arriba acoplaría dos
                # routers en el arranque (patrón de la casa).
                from monza_router_cotizaciones import aplicar_efectos_venta_despachada
                aplicar_efectos_venta_despachada(db, cot)
    # UN solo commit con el log adentro (patrón GA): un fallo después del commit
    # dentro de la función reintentada re-entraría la tx, vería el despacho ya
    # cerrado y devolvería un 400 falso por un cierre que SÍ ocurrió.
    from monza_models import MonzaLog
    numero_cot = cot.numero if cot else ""
    db.add(MonzaLog(user_email=current_user.email, accion="DESPACHADO", entidad="despacho", entidad_id=d.id, entidad_ref=d.numero, detalle=f"Despacho {d.numero} confirmado · {len(dis)} ítem(s)" + (f" · {numero_cot}" if numero_cot else "") + (" · guía SII en proceso (sin folio): no facturar aún" if guia_en_proceso else "")))
    db.commit()
    crear_notif(db, f"Despacho confirmado · {d.numero}", f"{numero_cot} — {len(dis)} ítem(s) despachado(s)" + (f" · {aviso}" if aviso else ""), "success", "/monzaparts/despachos", "despacho", d.id)
    return {"ok": True, "numero": d.numero, "guia_sii_en_proceso": guia_en_proceso, "aviso": aviso}


@router.delete("/entidades/{despacho_id}")
def anular_despacho(despacho_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    # Retry 1213/1205 (regla de la casa): anular toma los mismos locks que cerrar
    # (despacho + ítems) y puede caer en deadlock contra crear/cerrar concurrentes.
    for _ in range(3):
        try:
            return _anular_despacho_tx(db, despacho_id, current_user)
        except OperationalError as e:
            db.rollback()
            code = getattr(getattr(e, "orig", None), "args", [None])[0]
            if code not in (1213, 1205):
                raise
    raise HTTPException(409, "Conflicto de concurrencia al anular el despacho: reintenta")


def _anular_despacho_tx(db: Session, despacho_id: int, current_user):
    d = (
        db.query(MonzaDespacho)
        .filter(MonzaDespacho.id == despacho_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if not d:
        raise HTTPException(404, "Despacho no encontrado")
    if d.estado != "en_preparacion":
        raise HTTPException(400, "Solo se pueden anular despachos en preparación")
    # La guía 52 se emite con el despacho aún en preparación: anular acá dejaría un
    # documento tributario legal huérfano y la mercadería libre para emitir OTRA
    # guía por lo mismo (doble emisión ante el SII). Se bloquea mientras la guía viva.
    # El FOR UPDATE del despacho de arriba serializa contra _reclamar_emision (que
    # también lo bloquea); con_lock=True relee el DTE con FOR UPDATE + populate_existing
    # (espejo de routers/despachos.py:anular_despacho de GA).
    # incluir_ambiguo=True (hallazgos #2/#3): un claim VENCIDO sobre una emisión que
    # Wasabil nunca confirmó (uuid NULL + en_vuelo_desde puesto) también bloquea; si
    # no, la mercadería volvía a bodega y salía una SEGUNDA guía 52 real al SII.
    dte = _guia_electronica_activa(db, d.id, con_lock=True, incluir_ambiguo=True)
    if dte:
        if dte.folio:
            raise HTTPException(
                409,
                f"Este despacho tiene guía electrónica SII emitida (folio {dte.folio}): "
                "anúlala primero en Wasabil (anulación/nota de crédito) y luego anula el despacho",
            )
        # Import LOCAL (mismo motivo que en _guia_electronica_activa: evitar el ciclo
        # con el módulo DTE). Solo se llega acá con el DTE ya leído, nunca sin tabla.
        from monza_wasabil_dte.service import claim_vigente
        if dte.uuid is None and dte.en_vuelo_desde is not None and not claim_vigente(dte):
            # Ambiguo con claim vencido: "esperar" ya no resuelve nada, la única
            # salida es Reintentar (adopta el documento si existe, o re-emite tras
            # confirmar su ausencia). Texto espejo del de facturas.
            raise HTTPException(
                409,
                "No hay confirmación de Wasabil sobre esta emisión (se cortó la comunicación): "
                "la guía PUEDE existir ya ante el SII. Usa Reintentar para que el sistema lo "
                "verifique; si confirma que no se emitió, podrás anular",
            )
        raise HTTPException(
            409,
            "Este despacho tiene una emisión de guía SII en curso: espera el resultado "
            "(Emitida o Fallida) antes de anular",
        )
    d.estado = "anulado"
    db.flush()
    # Red de seguridad (espejo GA): si alguna línea quedó 'despachado' sin cobertura
    # completa de despachos CERRADOS (reversa de este anular o estado atascado
    # legado), vuelve a bodega; y si la venta estaba 'despachado' pero ya no todas
    # sus líneas lo están, vuelve a 'vendida'. Se evalúa SIEMPRE (no solo cuando
    # este anular revierte algo): también repara datos atascados de antes.
    dis = db.query(MonzaDespachoItem).filter(MonzaDespachoItem.despacho_id == d.id).all()
    item_ids = sorted({di.item_id for di in dis if di.item_id})
    if item_ids:
        qty_total = _qty_dispatched_closed(db, d.cotizacion_id)
        items_db = (
            db.query(MonzaCotizacionItem)
            .filter(MonzaCotizacionItem.id.in_(item_ids))
            .order_by(MonzaCotizacionItem.id.asc())
            .populate_existing()
            .with_for_update()
            .all()
        )
        for it in items_db:
            if it.estado_linea == "despachado" and qty_total.get(it.id, 0) + 0.001 < (it.cantidad or 0):
                it.estado_linea = "en_bodega"
        db.flush()
        # FOR UPDATE por la misma razón que en cerrar: acá puede DESHACERSE el LTV, y
        # decidir la transición sobre una lectura sin lock permite que dos caminos
        # concurrentes la vean ambos y resten dos veces.
        cot = (db.query(MonzaCotizacion)
               .filter(MonzaCotizacion.id == d.cotizacion_id)
               .populate_existing().with_for_update().first())
        if cot and cot.estado == "despachado" and any(i.estado_linea != "despachado" for i in cot.items):
            cot.estado = "vendida"
            # Reversa SIMÉTRICA del LTV que sumó el cierre. Va acá dentro —en la
            # TRANSICIÓN efectiva despachado→vendida— y no en el barrido de reparación
            # de arriba: una venta que ya estaba en 'vendida' nunca sumó nada, y
            # restarle plata le comería el LTV de sus otras ventas.
            from monza_router_cotizaciones import revertir_efectos_venta_despachada
            revertir_efectos_venta_despachada(db, cot)
    # UN solo commit con el log adentro (patrón GA, igual que crear/cerrar).
    from monza_models import MonzaLog
    db.add(MonzaLog(user_email=current_user.email, accion="ANULADO", entidad="despacho", entidad_id=d.id, entidad_ref=d.numero, detalle=f"Despacho {d.numero} anulado (borrador)"))
    db.commit()
    return {"ok": True}


@router.post("/entidades/{despacho_id}/firmar")
async def firmar_despacho(
    despacho_id: int,
    file: UploadFile = File(...),
    fecha_firma: str = Form(...),
    numero_guia: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Marca la guía del despacho como FIRMADA (entregada y firmada por el cliente).

    Desde 2026-08-06 la firma GATEA la facturación (paridad con MachParts,
    routers/despachos.py:firmar_despacho): monza_contabilidad rechaza emitir la
    factura de un despacho sin guia_firmada==1. Por eso la foto/PDF y la fecha de la
    firma son OBLIGATORIAS — son el respaldo de la entrega y lo que ordena la
    cobranza (OC + factura + guía firmada).

    A diferencia de GA (upload genérico de compras + PATCH con el filename), acá el
    archivo viaja EN el mismo request (multipart) y se guarda en una sola pasada:
    Monza no tiene endpoint genérico de docs propio, y montar la firma sobre el de
    compras de GA la dejaba detrás del candado 'mineria' (403 para automotriz).

    RE-FIRMAR está permitido y CORRIGE (reemplaza archivo y fecha; el archivo
    anterior queda huérfano en disco, igual que el resto de los uploads de la casa).
    DES-firmar NO existe: la facturación depende de esta marca y quitarla con una
    emisión en vuelo abriría la carrera firma↔factura — el error se corrige
    re-firmando con los datos buenos.
    """
    d = db.query(MonzaDespacho).filter(MonzaDespacho.id == despacho_id).first()
    if not d:
        raise HTTPException(404, "Despacho no encontrado")
    # Defensa anti-IDOR (heredada del PATCH viejo de contabilidad que este endpoint
    # reemplaza): el despacho debe pertenecer a una venta (cotización) real.
    if not d.cotizacion_id or not db.query(MonzaCotizacion.id).filter(
        MonzaCotizacion.id == d.cotizacion_id
    ).first():
        raise HTTPException(404, "El despacho no pertenece a una venta válida")
    if d.estado != "despachado":
        raise HTTPException(400, "Primero confirma (cierra) el despacho; luego marca la guía como firmada")

    # La fecha se valida ANTES de tocar el disco: un 400 no debe dejar archivos sueltos.
    fecha = _parse_fecha_firma(fecha_firma, fecha_guia=d.fecha_guia)

    numero_guia_limpio = (numero_guia or "").strip()
    if len(numero_guia_limpio) > 100:
        # La columna es String(100): sin este corte el commit revienta DESPUÉS de
        # escribir el archivo a disco (huérfano + 500 en vez de un 400 claro).
        raise HTTPException(400, "El N° de guía no puede superar los 100 caracteres")

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in _EXT_GUIA_FIRMADA:
        raise HTTPException(
            400,
            f"Tipo de archivo no permitido para la guía firmada: '{ext or 'sin extensión'}'. "
            "Sube la foto (JPG/PNG/WEBP/HEIC) o el PDF escaneado.",
        )
    # El archivo se LEE antes de tomar el lock: el await cede el event loop y no debe
    # ocurrir con la fila del despacho bloqueada.
    content = await file.read()
    if not content:
        raise HTTPException(400, "El archivo de la guía firmada llegó vacío: súbelo de nuevo")
    if len(content) > _MAX_GUIA_FIRMADA_BYTES:
        raise HTTPException(400, "Archivo demasiado grande (máximo 20 MB)")

    # ── Recarga BAJO LOCK y re-validación (cierra el TOCTOU del folio, hallazgo del
    # multienjambre 2026-08-07): entre el guard optimista de arriba y el commit, el
    # rescate de "Reintentar" puede ADOPTAR el folio SII de una emisión ambigua y
    # escribirlo en numero_guia — sin el lock, el N° manual staged lo pisaba y el
    # estado quedaba incorregible por API (el propio guard rechaza después toda
    # corrección). populate_existing: la relectura debe ver lo COMMITEADO, no el
    # snapshot de la sesión. La adopción escribe dte+despacho en una transacción, así
    # que el lock del despacho basta como punto de serialización.
    d = (
        db.query(MonzaDespacho)
        .filter(MonzaDespacho.id == despacho_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if not d or d.estado != "despachado":
        raise HTTPException(400, "El despacho cambió de estado mientras se firmaba: recarga y reintenta")
    # RE-VALIDACIÓN del invariante firma >= emisión con el dato FRESCO: la primera
    # pasada se hizo sobre el snapshot PRE-lock, y un PUT de cabecera que cargue
    # fecha_guia en esa ventana dejaría persistido justo el par que el guard del PUT
    # (R9) prohíbe. Barato y cierra la carrera por los dos lados.
    fecha = _parse_fecha_firma(fecha_firma, fecha_guia=d.fecha_guia)
    if numero_guia_limpio:
        # Con guía electrónica viva, el N° correcto ya es (o será) el folio del SII:
        # se bloquea el reemplazo manual. La firma en sí sigue funcionando sin tocar
        # el N° (espejo GA). El DTE se lee por PRIMERA vez en esta sesión (fresco bajo
        # READ COMMITTED); si la adopción del folio corre después de esta lectura,
        # queda esperando el lock del despacho y escribe DESPUÉS del commit de la
        # firma — el folio gana, que es el orden correcto.
        _rechazar_si_pisa_folio(db, d, numero_guia_limpio)
        d.numero_guia = numero_guia_limpio

    # uuid4().hex + ext = el MISMO formato del upload de la casa (32 hex + extensión):
    # cumple el _UPLOAD_FILE_RE de GA y jamás colisiona con otro documento.
    unique_name = f"{uuid.uuid4().hex}{ext}"
    with open(os.path.join(_DOCS_DIR, unique_name), "wb") as f_out:
        f_out.write(content)

    d.guia_firmada = 1
    # La columna es DATETIME: se guarda a medianoche del día firmado (espejo del
    # strptime de GA). La HORA de la firma no se captura — el dato de negocio es el día.
    d.fecha_firma = datetime.combine(fecha, datetime.min.time())
    d.usuario_firma_id = getattr(current_user, "id", None)
    d.guia_firmada_archivo = unique_name
    # UN solo commit con el log adentro (patrón de crear/cerrar/anular).
    from monza_models import MonzaLog
    db.add(MonzaLog(
        user_email=current_user.email, accion="GUIA_FIRMADA", entidad="despacho",
        entidad_id=d.id, entidad_ref=d.numero,
        detalle=f"Guía {d.numero_guia or d.numero} firmada por el cliente el {fecha.isoformat()} · {unique_name}",
    ))
    db.commit()
    return {
        "ok": True,
        "guia_firmada": True,
        "numero_guia": d.numero_guia,
        "fecha_firma": d.fecha_firma.isoformat(),
        "guia_firmada_archivo": d.guia_firmada_archivo,
    }


@router.get("/docs/{filename}")
def serve_doc(
    filename: str,
    current_user=Depends(get_current_user),
):
    """Sirve un documento del repositorio uploads/docs (hoy: la guía firmada).

    Espejo de routers/despachos.py:serve_doc de GA — mismo directorio físico, pero
    detrás del candado 'automotriz' de ESTE router: el serve de GA exige 'mineria'
    y un usuario Monza recibía 403 al intentar ver su propia guía firmada.

    Path traversal cortado en dos capas: formato estricto del nombre y resolución
    del path final contra el directorio (igual que GA)."""
    if not re.match(r"^[A-Za-z0-9._-]{1,255}$", filename):
        raise HTTPException(400, "Nombre de archivo inválido")
    full_path = os.path.abspath(os.path.join(_DOCS_DIR, filename))
    if not full_path.startswith(_DOCS_DIR + os.sep):
        raise HTTPException(400, "Ruta inválida")
    if not os.path.isfile(full_path):
        raise HTTPException(404, "Documento no encontrado")
    return FileResponse(full_path, filename=filename)
