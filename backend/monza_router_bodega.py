"""
Bodega MonzaParts (alineacion MachParts): recepcion fisica por embarque.
Recibir embarque -> abrir recepcion -> marcar item x item -> cerrar.
Estados recepcion: completo faltante sobrante danado_utilizable danado_no_utilizable no_llego
Cierre: completo/danado_util/sobrante -> en_bodega ; resto -> reclamo
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

from database import get_db
from auth import get_current_user
from empresa_guard import require_empresa
from monza_models import (
    MonzaCotizacion, MonzaCotizacionItem, MonzaOcProveedor, MonzaReclamo,
    MonzaEmbarque, MonzaEmbarqueItem, MonzaRecepcion, MonzaRecepcionItem, MonzaDocumento,
    MonzaDespacho, MonzaDespachoItem,
)
from monza_notif import crear_notif

# Hallazgo #13: sin este candado un usuario autenticado de empresa='mineria' leía
# y mutaba datos de MonzaParts (recibir embarque, marcar ítem, cerrar recepción).
# Grupo AM ya canda su router de despachos; aquí se cierra la puerta de Bodega.
# DEUDA INVERSA anotada para la Fase 9 de paridad: routers/bodega.py de GA sigue
# abierto, así que Monza queda MÁS candado que GA (no es "44/44 en 403").
router = APIRouter(
    prefix="/api/monza/bodega",
    tags=["monza-bodega"],
    dependencies=[Depends(require_empresa("automotriz"))],
)

ESTADOS_RECEP = {"completo", "faltante", "sobrante", "danado_utilizable", "danado_no_utilizable", "no_llego"}
A_BODEGA = {"completo", "danado_utilizable", "sobrante"}


def _log(db, email, accion, entidad, eid=None, ref=None, det=None):
    from monza_models import MonzaLog
    db.add(MonzaLog(user_email=email, accion=accion, entidad=entidad, entidad_id=eid, entidad_ref=ref, detalle=det))
    db.commit()


def _item_dict(it, cot, ocp=None):
    return {
        "id": it.id, "cot_numero": cot.numero if cot else None,
        "cliente": cot.cliente.nombre if cot and cot.cliente else None, "vehiculo": cot.vehiculo if cot else None,
        "descripcion": it.descripcion, "numero_parte": it.numero_parte, "marca": it.marca,
        "calidad": it.calidad, "cantidad": it.cantidad, "estado_linea": it.estado_linea,
        "ocp_numero": (ocp.numero_oc or ocp.numero) if ocp else None,
        "ocp_proveedor": ocp.proveedor_nombre if ocp else None,
    }


def _ocp(db, oid, cache):
    if not oid: return None
    if oid not in cache:
        cache[oid] = db.query(MonzaOcProveedor).filter(MonzaOcProveedor.id == oid).first()
    return cache[oid]


# ── KPIs ──────────────────────────────────────────────────────────────────────
@router.get("/kpis")
def kpis(db: Session = Depends(get_db), _=Depends(get_current_user)):
    # embarques a recibir = estado en_transito/en_aduana/en_bodega sin recepcion cerrada
    recv = db.query(MonzaEmbarque).filter(MonzaEmbarque.estado.in_(["en_transito", "en_aduana", "en_bodega"])).all()
    a_recibir = 0
    for e in recv:
        r = db.query(MonzaRecepcion).filter(MonzaRecepcion.embarque_id == e.id, MonzaRecepcion.estado == "cerrada").first()
        if not r:
            a_recibir += 1
    en_bodega = db.query(func.count(MonzaCotizacionItem.id)).filter(MonzaCotizacionItem.estado_linea == "en_bodega").scalar() or 0
    despachado = db.query(func.count(MonzaCotizacionItem.id)).filter(MonzaCotizacionItem.estado_linea == "despachado").scalar() or 0
    reclamos = db.query(func.count(MonzaReclamo.id)).filter(MonzaReclamo.estado.in_(["pendiente", "reclamado"])).scalar() or 0
    return {"a_recibir": a_recibir, "en_bodega": en_bodega, "despachado": despachado, "reclamos_pendientes": reclamos}


# ── Embarques a recibir ───────────────────────────────────────────────────────
@router.get("/embarques")
def embarques_a_recibir(db: Session = Depends(get_db), _=Depends(get_current_user)):
    embs = db.query(MonzaEmbarque).filter(MonzaEmbarque.estado.in_(["en_transito", "en_aduana", "en_bodega"])).order_by(MonzaEmbarque.id.desc()).all()
    out = []
    for e in embs:
        rec = db.query(MonzaRecepcion).filter(MonzaRecepcion.embarque_id == e.id).order_by(MonzaRecepcion.id.desc()).first()
        if rec and rec.estado == "cerrada":
            continue
        n = db.query(func.count(MonzaEmbarqueItem.id)).filter(MonzaEmbarqueItem.embarque_id == e.id).scalar() or 0
        out.append({
            "id": e.id, "numero": e.numero, "estado": e.estado, "awb": e.awb,
            "forwarder": e.forwarder, "tracking": e.tracking, "fecha_llegada_est": e.fecha_llegada_est,
            "items_count": n, "recepcion_id": rec.id if rec else None,
            "recepcion_abierta": bool(rec and rec.estado == "abierta"),
        })
    return out


# ── Historial de embarques recepcionados (Fase 4 espejo GA) ───────────────────
@router.get("/embarques/historial")
def embarques_recepcionados(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Embarques con recepción CERRADA (el listado principal los oculta). El criterio
    es la recepción cerrada, NO MonzaEmbarque.estado=='en_bodega' (ese estado puede
    coexistir con una recepción aún abierta)."""
    filas = (
        db.query(MonzaRecepcion, MonzaEmbarque)
        .join(MonzaEmbarque, MonzaEmbarque.id == MonzaRecepcion.embarque_id)
        .filter(MonzaRecepcion.estado == "cerrada")
        .order_by(MonzaRecepcion.fecha_cierre.desc())
        .limit(100)
        .all()
    )
    emb_ids = [e.id for _r, e in filas]
    counts = {}
    if emb_ids:
        counts = dict(
            db.query(MonzaEmbarqueItem.embarque_id, func.count(MonzaEmbarqueItem.id))
            .filter(MonzaEmbarqueItem.embarque_id.in_(emb_ids))
            .group_by(MonzaEmbarqueItem.embarque_id).all()
        )
    out = []
    for r, e in filas:
        out.append({
            "id": e.id, "numero": e.numero, "estado": e.estado, "awb": e.awb,
            "forwarder": e.forwarder, "tracking": e.tracking,
            # String(30) libre en el modelo: tal cual, sin parseo
            "fecha_llegada_est": e.fecha_llegada_est,
            "items_count": counts.get(e.id, 0),
            "recepcion": {
                "id": r.id,
                "fecha_cierre": r.fecha_cierre.isoformat() if r.fecha_cierre else None,
                "usuario_email": r.usuario_email,
            },
        })
    return out


# ── Iniciar recepcion ─────────────────────────────────────────────────────────
@router.post("/embarques/{emb_id}/recibir")
def recibir(emb_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    # FOR UPDATE sobre el embarque: dos clics simultáneos serializan aquí (sin el
    # lock, ambos SELECT veían "sin recepción" y se creaban DOS recepciones).
    e = (
        db.query(MonzaEmbarque)
        .filter(MonzaEmbarque.id == emb_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if not e:
        raise HTTPException(404, "Embarque no encontrado")
    # A lo más UNA recepción por embarque (espejo GA): una recepción CERRADA ya
    # alimentó el tope físico de despacho — re-recepcionar el mismo embarque sumaría
    # sus cantidades DE NUEVO y habilitaría despachar unidades que jamás llegaron.
    # Una reposición llega en un embarque NUEVO, no re-recepcionando este.
    rec = (
        db.query(MonzaRecepcion)
        .filter(MonzaRecepcion.embarque_id == emb_id)
        .order_by(MonzaRecepcion.id.desc())
        .first()
    )
    if rec and rec.estado == "cerrada":
        raise HTTPException(400, "El embarque ya fue recepcionado (recepción cerrada); una reposición se recibe en un embarque nuevo")
    if rec:
        return {"ok": True, "recepcion_id": rec.id}
    rec = MonzaRecepcion(embarque_id=emb_id, estado="abierta", usuario_email=current_user.email)
    db.add(rec); db.commit(); db.refresh(rec)
    _log(db, current_user.email, "CREATE", "recepcion", rec.id, e.numero, f"Recepción abierta · {e.numero}")
    return {"ok": True, "recepcion_id": rec.id}


# ── Detalle recepcion (items con su estado_recepcion) ─────────────────────────
@router.get("/recepciones/{rec_id}")
def get_recepcion(rec_id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    rec = db.query(MonzaRecepcion).filter(MonzaRecepcion.id == rec_id).first()
    if not rec:
        raise HTTPException(404, "Recepción no encontrada")
    emb = db.query(MonzaEmbarque).filter(MonzaEmbarque.id == rec.embarque_id).first()
    eis = db.query(MonzaEmbarqueItem).filter(MonzaEmbarqueItem.embarque_id == rec.embarque_id).all()
    cache = {}
    items = []
    marcados = 0
    for ei in eis:
        it = db.query(MonzaCotizacionItem).filter(MonzaCotizacionItem.id == ei.item_id).first()
        if not it:
            continue
        cot = db.query(MonzaCotizacion).options(joinedload(MonzaCotizacion.cliente)).filter(MonzaCotizacion.id == it.cotizacion_id).first()
        ri = db.query(MonzaRecepcionItem).filter(MonzaRecepcionItem.recepcion_id == rec_id, MonzaRecepcionItem.item_id == it.id).first()
        nfotos = db.query(func.count(MonzaDocumento.id)).filter(MonzaDocumento.entidad == "recepcion_item", MonzaDocumento.entidad_id == it.id).scalar() or 0
        d = _item_dict(it, cot, _ocp(db, it.oc_proveedor_id, cache))
        d["estado_recepcion"] = ri.estado_recepcion if ri else None
        d["qty_recibida"] = ri.qty_recibida if ri else None
        d["qty_danada"] = ri.qty_danada if ri else 0
        d["observacion"] = ri.observacion if ri else None
        d["fotos"] = nfotos
        if ri and ri.estado_recepcion:
            marcados += 1
        items.append(d)
    return {
        "id": rec.id, "embarque_id": rec.embarque_id, "embarque_numero": emb.numero if emb else None,
        "estado": rec.estado, "total": len(items), "marcados": marcados, "items": items,
    }


# ── Marcar item ───────────────────────────────────────────────────────────────
class MarcarBody(BaseModel):
    estado_recepcion: str
    qty_recibida: Optional[int] = None
    qty_danada: Optional[int] = 0
    observacion: Optional[str] = None


@router.patch("/recepciones/{rec_id}/items/{item_id}")
def marcar_item(rec_id: int, item_id: int, body: MarcarBody, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    # Guard de integridad (espejo GA): una recepción CERRADA ya alimentó el tope
    # físico de despacho — marcar sobre ella alteraría cantidades ya consumidas.
    # FOR UPDATE + populate_existing: serializa contra un cierre concurrente (sin el
    # lock, un marcado tardío podía colarse mientras cerrar_recepcion procesaba).
    rec = (
        db.query(MonzaRecepcion)
        .filter(MonzaRecepcion.id == rec_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if not rec or rec.estado == "cerrada":
        raise HTTPException(400, "Recepción no encontrada o ya cerrada")
    # Pertenencia: el ítem debe venir en el embarque de ESTA recepción. Una fila
    # espuria con un item_id ajeno sumaría al tope físico de otra venta al cerrar
    # (cerrar la ignora, pero _qty_recibida_utilizable de despachos la contaría).
    pertenece = db.query(MonzaEmbarqueItem.id).filter(
        MonzaEmbarqueItem.embarque_id == rec.embarque_id,
        MonzaEmbarqueItem.item_id == item_id,
    ).first()
    if not pertenece:
        raise HTTPException(400, "El ítem no pertenece al embarque de esta recepción")
    if body.estado_recepcion not in ESTADOS_RECEP:
        raise HTTPException(400, f"Estado inválido: {body.estado_recepcion}")
    # qty_recibida alimenta el tope de despacho: negativa no tiene sentido físico
    if (body.qty_recibida or 0) < 0:
        raise HTTPException(400, "La cantidad recibida no puede ser negativa")
    if (body.qty_danada or 0) < 0:
        raise HTTPException(400, "La cantidad dañada no puede ser negativa")
    # Coherencia del 'faltante': marcar faltante con recibido >= vendido inflaría el
    # tope físico sin dejar reclamo (un clic sin ajustar el input mandaba lo vendido
    # completo). Si llegó todo, el estado correcto es 'completo'.
    if body.estado_recepcion == "faltante" and body.qty_recibida is not None:
        cant_vendida = db.query(MonzaCotizacionItem.cantidad).filter(
            MonzaCotizacionItem.id == item_id).scalar() or 0
        if cant_vendida and body.qty_recibida >= cant_vendida:
            raise HTTPException(
                400,
                "Con estado Faltante la cantidad recibida debe ser MENOR que la vendida "
                f"({cant_vendida}); si llegó todo, usa Completo",
            )
    # foto obligatoria si dañado
    if "danado" in body.estado_recepcion:
        nfotos = db.query(func.count(MonzaDocumento.id)).filter(MonzaDocumento.entidad == "recepcion_item", MonzaDocumento.entidad_id == item_id).scalar() or 0
        if nfotos == 0:
            raise HTTPException(400, "Debe adjuntar al menos una foto para ítems dañados")
    ri = db.query(MonzaRecepcionItem).filter(MonzaRecepcionItem.recepcion_id == rec_id, MonzaRecepcionItem.item_id == item_id).first()
    if not ri:
        ri = MonzaRecepcionItem(recepcion_id=rec_id, item_id=item_id)
        db.add(ri)
    ri.estado_recepcion = body.estado_recepcion
    ri.qty_recibida = body.qty_recibida
    ri.qty_danada = body.qty_danada or 0
    ri.observacion = body.observacion
    ri.fecha = datetime.utcnow()
    db.commit()
    return {"ok": True}


# ── Cerrar recepcion ──────────────────────────────────────────────────────────
#
# CIERRE PARTICIONADO (Fase 2 del espejo Grupo AM): antes la línea era binaria —
# toda a bodega o toda a reclamo — y una llegada parcial mandaba la línea ENTERA a
# reclamo, dejando indespachable lo que SÍ llegó. Ahora lo recibido queda en bodega
# (Despachos lo topea por qty_recibida) y solo el faltante REAL va a reclamo,
# acumulando lo recibido en recepciones cerradas ANTERIORES (una línea repartida en
# 2 embarques o una reposición no genera reclamos fantasma).

class CerrarRecepcionBody(BaseModel):
    forzar: bool = False   # cerrar aunque queden ítems sin marcar → reclamo 'no_llego' trazable


@router.post("/recepciones/{rec_id}/cerrar")
def cerrar_recepcion(rec_id: int, body: Optional[CerrarRecepcionBody] = None, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    forzar = bool(body and body.forzar)
    # FOR UPDATE + populate_existing: serializa un doble-cierre concurrente (sin el
    # lock, dos cierres simultáneos duplicaban reclamos) y contra marcar_item.
    rec = (
        db.query(MonzaRecepcion)
        .filter(MonzaRecepcion.id == rec_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if not rec:
        raise HTTPException(404, "Recepción no encontrada")
    if rec.estado == "cerrada":
        raise HTTPException(400, "La recepción ya está cerrada")
    eis = db.query(MonzaEmbarqueItem).filter(MonzaEmbarqueItem.embarque_id == rec.embarque_id).all()

    ri_map = {
        ri.item_id: ri
        for ri in db.query(MonzaRecepcionItem).filter(MonzaRecepcionItem.recepcion_id == rec_id).all()
    }
    pendientes = [ei for ei in eis if ei.item_id not in ri_map or not ri_map[ei.item_id].estado_recepcion]
    if pendientes and not forzar:
        raise HTTPException(400, f"Faltan {len(pendientes)} ítem(s) por marcar. Usa forzar=true para cerrar igual.")

    # Recibido utilizable ACUMULADO en recepciones cerradas ANTERIORES: el faltante
    # a reclamar se calcula contra lo que AÚN no llega en total, no contra la venta
    # completa en cada recepción. Import local desde despachos (misma dirección de
    # dependencia que GA: bodega → despachos; despachos no importa bodega).
    from monza_router_despachos import _qty_recibida_utilizable
    recibido_previo = _qty_recibida_utilizable(db, [ei.item_id for ei in eis if ei.item_id])

    def _reclamo(it, cot, motivo, qty_afectada, obs):
        db.add(MonzaReclamo(
            item_id=it.id, oc_proveedor_id=it.oc_proveedor_id,
            cot_numero=cot.numero if cot else None, descripcion=it.descripcion,
            motivo=motivo, qty_afectada=qty_afectada,
            estado="pendiente", observacion=obs, user_email=current_user.email,
        ))

    a_bodega = 0
    reclamos = 0
    for ei in eis:
        it = db.query(MonzaCotizacionItem).filter(MonzaCotizacionItem.id == ei.item_id).first()
        if not it:
            continue
        cot = db.query(MonzaCotizacion).filter(MonzaCotizacion.id == it.cotizacion_id).first()
        ri = ri_map.get(it.id)

        if not ri or not ri.estado_recepcion:
            # Solo con forzar=true: ítem sin marcar. Antes quedaba atascado en
            # 'embarcado' para siempre, invisible para Bodega y Despachos — se trata
            # como 'no_llego' con reclamo TRAZABLE que se gestiona cuando aparezca.
            it.estado_linea = "reclamo"
            _reclamo(it, cot, "no_llego", it.cantidad or 0, None)
            reclamos += 1
            continue

        cant = it.cantidad or 0
        # qty_recibida NULL en estados utilizables se interpreta por el estado
        # (misma regla que _qty_recibida_utilizable): completo/sobrante = todo;
        # danado_utilizable = cant − dañada; faltante = 0.
        if ri.qty_recibida is not None:
            recibida = max(ri.qty_recibida, 0)
        elif ri.estado_recepcion in ("completo", "sobrante"):
            recibida = cant
        elif ri.estado_recepcion == "danado_utilizable":
            recibida = max(cant - (ri.qty_danada or 0), 0)
        else:
            recibida = 0
        # Lo que sigue faltando de la línea considerando TODAS las recepciones:
        # lo vendido − (recibido en recepciones cerradas anteriores + esta)
        faltante_pendiente = max(cant - (recibido_previo.get(it.id, 0) + recibida), 0)

        if ri.estado_recepcion in A_BODEGA:
            it.estado_linea = "en_bodega"
            a_bodega += 1
            # 'completo' con MENOS unidades que lo vendido = llegada parcial: lo
            # recibido queda despachable y el faltante REAL se reclama aparte para
            # que no desaparezca sin traza.
            if ri.estado_recepcion == "completo" and recibida > 0 and faltante_pendiente > 0:
                _reclamo(it, cot, "faltante", faltante_pendiente, ri.observacion)
                reclamos += 1
        elif ri.estado_recepcion == "faltante":
            if recibida > 0:
                # Llegada PARCIAL: las unidades que SÍ llegaron quedan en bodega y
                # despachables (topeadas por qty_recibida en Despachos); solo la
                # diferencia REAL va a reclamo. Antes la línea ENTERA caía a reclamo.
                it.estado_linea = "en_bodega"
                a_bodega += 1
                if faltante_pendiente > 0:
                    _reclamo(it, cot, "faltante", faltante_pendiente, ri.observacion)
                    reclamos += 1
            else:
                it.estado_linea = "reclamo"
                _reclamo(it, cot, "faltante", faltante_pendiente or cant, ri.observacion)
                reclamos += 1
        elif ri.estado_recepcion == "no_llego":
            it.estado_linea = "reclamo"
            _reclamo(it, cot, "no_llego", cant, ri.observacion)
            reclamos += 1
        else:  # danado_no_utilizable
            it.estado_linea = "reclamo"
            _reclamo(it, cot, "danado_no_utilizable",
                     recibida if recibida > 0 else cant, ri.observacion)
            reclamos += 1

    rec.estado = "cerrada"
    rec.fecha_cierre = datetime.utcnow()
    emb = db.query(MonzaEmbarque).filter(MonzaEmbarque.id == rec.embarque_id).first()
    if emb:
        emb.estado = "en_bodega"
    db.commit()
    _log(db, current_user.email, "UPDATE", "recepcion", rec.id, emb.numero if emb else None, f"Recepción cerrada · {a_bodega} a bodega, {reclamos} reclamo(s)")
    if reclamos:
        crear_notif(db, f"Reclamos en recepción · {emb.numero if emb else ''}", f"{reclamos} ítem(s) con problema", "danger", "/monzaparts/bodega", "recepcion", rec.id)
    # Aviso a Despachos por venta afectada (Fase 4, espejo de la regla B6/B7 de GA):
    # SIEMPRE después del commit principal (crear_notif commitea por su cuenta) y
    # envuelto en try/except — un fallo de notificación no puede tumbar el cierre.
    try:
        _notificar_ventas_listas(db, [ei.item_id for ei in eis if ei.item_id])
    except Exception:
        pass
    return {"ok": True, "en_bodega": a_bodega, "reclamos": reclamos}


def _notificar_ventas_listas(db: Session, item_ids: list):
    """Por cada venta con ítems en este embarque: si TODOS sus ítems quedaron en
    bodega → 'lista para despacho'; si quedó parcial y el plazo comprometido está a
    ≤3 días hábiles → 'plazo crítico'. Anti-duplicado mínimo-invasivo: no se repite
    una notificación igual (misma venta + mismo título) que siga sin leer."""
    from monza_models import MonzaNotificacion
    from monza_fechas import business_days_remaining
    if not item_ids:
        return
    cot_ids = {
        r[0] for r in db.query(MonzaCotizacionItem.cotizacion_id)
        .filter(MonzaCotizacionItem.id.in_(item_ids)).all()
    }
    for cot_id in cot_ids:
        cot = db.query(MonzaCotizacion).options(joinedload(MonzaCotizacion.cliente), joinedload(MonzaCotizacion.items)).filter(MonzaCotizacion.id == cot_id).first()
        if not cot or cot.estado not in ("vendida", "despachado") or not cot.items:
            continue
        todos_en_bodega = all(i.estado_linea == "en_bodega" for i in cot.items)
        parcial_en_bodega = any(i.estado_linea == "en_bodega" for i in cot.items)
        titulo = None
        if todos_en_bodega:
            titulo = f"Venta {cot.numero} lista para despacho"
            mensaje = f"Todos los ítems están en bodega · {cot.cliente.nombre if cot.cliente else ''}"
            tipo = "success"
        elif parcial_en_bodega and cot.fecha_entrega_est:
            try:
                dias = business_days_remaining(cot.fecha_entrega_est)
            except Exception:
                dias = None
            if dias is not None and dias <= 3:
                titulo = f"Plazo crítico · {cot.numero}"
                mensaje = f"Quedan {dias} día(s) hábil(es) y la venta está parcial en bodega"
                tipo = "warning"
        if not titulo:
            continue
        ya_avisada = db.query(MonzaNotificacion).filter(
            MonzaNotificacion.entidad == "cotizacion",
            MonzaNotificacion.entidad_id == cot.id,
            MonzaNotificacion.titulo == titulo,
            MonzaNotificacion.leida == False,  # noqa: E712
        ).first()
        if ya_avisada:
            continue
        crear_notif(db, titulo, mensaje, tipo, "/monzaparts/despachos", "cotizacion", cot.id)


# ── Items en bodega (listos para despacho) ────────────────────────────────────
@router.get("/en-bodega")
def en_bodega(q: Optional[str] = Query(None), db: Session = Depends(get_db), _=Depends(get_current_user)):
    query = (
        db.query(MonzaCotizacionItem)
        .join(MonzaCotizacion, MonzaCotizacionItem.cotizacion_id == MonzaCotizacion.id)
        .options(joinedload(MonzaCotizacionItem.cotizacion).joinedload(MonzaCotizacion.cliente))
        .filter(MonzaCotizacionItem.estado_linea == "en_bodega")
    )
    if q:
        query = query.filter((MonzaCotizacionItem.descripcion.ilike(f"%{q}%")) | (MonzaCotizacion.numero.ilike(f"%{q}%")))
    items = query.order_by(MonzaCotizacionItem.id.desc()).all()

    # Hallazgo #10: "cantidad" es lo VENDIDO, no lo recibido. En una llegada parcial
    # (vendido 10, recibido 4 + reclamo por 6) el bodeguero leía "10 en bodega" y
    # "6 reclamadas" = 16 unidades sobre una línea de 10, mientras Despachos ofrecía
    # 4. Se enriquece SOLO este listado (no _item_dict, para no alterar el panel de
    # recepción) con lo realmente recibido y el cupo aún despachable, usando los
    # mismos helpers que gobiernan el tope físico en Despachos.
    # Import LOCAL: misma dirección bodega → despachos que ya usa cerrar_recepcion.
    from monza_router_despachos import _qty_recibida_utilizable, _tope_fisico
    recibidos = _qty_recibida_utilizable(db, [i.id for i in items])
    # Ya reservado/despachado en UNA sola query batch (patrón de listos_despacho en
    # monza_router_despachos.py): _qty_already_dispatched pide cotizacion_id y sería N+1.
    ya = {}
    cot_ids = list({i.cotizacion_id for i in items if i.cotizacion_id})
    if cot_ids:
        for item_id, qty in (
            db.query(MonzaDespachoItem.item_id, MonzaDespachoItem.qty_despachada)
            .join(MonzaDespacho, MonzaDespacho.id == MonzaDespachoItem.despacho_id)
            .filter(MonzaDespacho.cotizacion_id.in_(cot_ids), MonzaDespacho.estado != "anulado")
            .all()
        ):
            ya[item_id] = ya.get(item_id, 0) + (qty or 0)

    cache = {}
    out = []
    for it in items:
        d = _item_dict(it, it.cotizacion, _ocp(db, it.oc_proveedor_id, cache))
        # None (no la clave ausente) cuando el ítem NO tiene recepción registrada:
        # dato legado / carga manual, que a propósito no se acota (igual que _tope_fisico).
        d["qty_recibida"] = recibidos.get(it.id)
        d["qty_disponible"] = max(_tope_fisico(it, recibidos) - ya.get(it.id, 0), 0)
        out.append(d)
    return out


# ── Reclamos ──────────────────────────────────────────────────────────────────
@router.get("/reclamos")
def list_reclamos(estado: Optional[str] = Query(None), db: Session = Depends(get_db), _=Depends(get_current_user)):
    q = db.query(MonzaReclamo)
    if estado:
        q = q.filter(MonzaReclamo.estado == estado)
    cache = {}
    out = []
    for r in q.order_by(MonzaReclamo.id.desc()).all():
        ocp = _ocp(db, r.oc_proveedor_id, cache)
        out.append({
            "id": r.id, "cot_numero": r.cot_numero, "descripcion": r.descripcion,
            "motivo": r.motivo, "qty_afectada": r.qty_afectada, "estado": r.estado,
            "observacion": r.observacion, "ocp_proveedor": ocp.proveedor_nombre if ocp else None,
            "fecha_creacion": r.fecha_creacion.isoformat() if r.fecha_creacion else None,
        })
    return out


class ReclamoUpd(BaseModel):
    estado: Optional[str] = None
    observacion: Optional[str] = None


@router.patch("/reclamos/{rid}")
def update_reclamo(rid: int, body: ReclamoUpd, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    r = db.query(MonzaReclamo).filter(MonzaReclamo.id == rid).first()
    if not r:
        raise HTTPException(404, "No encontrado")
    if body.estado:
        r.estado = body.estado
        if body.estado in ("resuelto", "anulado"):
            r.fecha_resolucion = datetime.utcnow()
    if body.observacion is not None:
        r.observacion = body.observacion
    db.commit()
    return {"ok": True}
