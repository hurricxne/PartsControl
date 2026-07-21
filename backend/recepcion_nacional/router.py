"""API del módulo Recepción Nacional (camino físico de la compra nacional).

Prefijo: /recepcion-nacional (se monta con prefix=/api → /api/recepcion-nacional).
Bodega registra "cuánto llegó" de cada ítem de una OC-Proveedor NACIONAL cuando el
camión del proveedor llega con su guía de despacho. Al cerrar la recepción, los
ítems utilizables con cantidad > 0 pasan a 'en_bodega' y quedan despachables,
capados por lo recibido (tope físico G6). Scope por empresa (Grupo AM = 'mineria').

El backend es la autoridad: valida qué ítems pertenecen a la OC, que la OC sea
nacional, y NUNCA confía en el cliente para el tope. Locks with_for_update donde
hay stock/estado concurrente (cerrar/anular).
"""
from datetime import datetime, date
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload
from pydantic import BaseModel, Field

from database import get_db
from auth import get_current_user
from empresa_guard import require_empresa
from models.models import (
    User, OcProveedor, OcProveedorItem, ItemCotizacion, Despacho, DespachoItem,
)

from .models import (
    RecepcionNacional, RecepcionNacionalItem, RECEPCION_UTILIZABLE, ESTADOS_VALIDOS,
)
from .service import _f, empresa_de, parse_date_estricta, serialize_recepcion

# Módulo SOLO MachParts (Grupo AM = 'mineria'). El guard de router deniega (403) el
# acceso a usuarios de otra empresa, igual que compras_contab / despachos.
router = APIRouter(
    prefix="/recepcion-nacional",
    tags=["recepcion-nacional"],
    dependencies=[Depends(require_empresa("mineria"))],
)

TOL = 0.001  # holgura en unidades para comparar tope vs despachado/costeado

# Estados de ItemCotizacion sobre los que se puede registrar una entrega nacional:
# 'comprado' (recién asignado a la OC) o 'en_bodega' (entregas sucesivas sobre el
# remanente que ya recibió parte). Un nacional NUNCA pasa por preparado/embarcado.
_ESTADOS_ITEM_RECIBIBLES = ("comprado", "en_bodega")


# ─── Schemas ─────────────────────────────────────────────────────────────────
class EntregaItemIn(BaseModel):
    item_cotizacion_id: int
    qty_recibida: float = Field(..., ge=0)
    estado_recepcion: str
    observacion: Optional[str] = None


class RegistrarEntregaIn(BaseModel):
    oc_proveedor_id: int
    numero_guia_proveedor: Optional[str] = None
    fecha: Optional[str] = None
    documento: Optional[str] = None
    observacion: Optional[str] = None
    cerrar: bool = True
    items: List[EntregaItemIn] = Field(..., min_length=1)


# ─── Helpers ─────────────────────────────────────────────────────────────────
def _fecha(s, campo: str):
    try:
        return parse_date_estricta(s, campo=campo)
    except ValueError as e:
        raise HTTPException(400, str(e))


def _con_retry_deadlock(db: Session, operacion):
    """Ejecuta `operacion()` reintentando ante deadlock / lock-timeout de InnoDB
    (1213 / 1205): la serialización de los caminos de stock (locks de
    ItemCotizacion compartidos con costeo y despachos) puede elegir una víctima;
    MySQL la mata y aquí se reintenta la transacción completa en vez de devolver
    un 500 al operador (mismo patrón que crear_compra / create_despacho)."""
    from sqlalchemy.exc import OperationalError
    for _ in range(3):
        try:
            return operacion()
        except OperationalError as e:
            db.rollback()
            args = getattr(getattr(e, "orig", None), "args", None) or []
            if not args or args[0] not in (1213, 1205):
                raise
    raise HTTPException(
        409, "Conflicto momentáneo con otra operación simultánea: reintente")


def _cerrar(db: Session, rec: RecepcionNacional) -> None:
    """Cierra la recepción: los ítems UTILIZABLES con qty > 0 pasan a 'en_bodega'
    (despachables, topeados por lo recibido en Despachos). no_llego /
    danado_no_utilizable / faltante-con-0 NO tocan el estado (queda 'comprado' →
    puede llegar en una entrega posterior). El reclamo por faltante es acción
    opcional, no automática (a diferencia del embarque consolidado)."""
    ids = sorted({ri.item_cotizacion_id for ri in rec.items
                  if ri.item_cotizacion_id is not None})
    # Lock en el orden CANÓNICO id ASC (mismo ancla que costeo/anular/despachos):
    # bloquear fila a fila en el orden del payload INVERTÍA el orden contra esos
    # caminos y deadlockeaba con este cierre como víctima (ronda de cierre G13).
    items_map = {}
    if ids:
        items_map = {it.id: it for it in (
            db.query(ItemCotizacion)
            .filter(ItemCotizacion.id.in_(ids))
            .order_by(ItemCotizacion.id.asc())
            .populate_existing().with_for_update().all())}
    for ri in rec.items:
        item = items_map.get(ri.item_cotizacion_id)
        if not item:
            continue
        # Solo se avanza desde 'comprado': cerrar una recepción tardía (que quedó
        # abierta y el ítem ya se despachó) NO debe retroceder 'despachado' →
        # 'en_bodega' (regresión de estado detectada en la revisión G13).
        if (ri.estado_recepcion in RECEPCION_UTILIZABLE and _f(ri.qty_recibida) > 0
                and item.estado_item == "comprado"):
            item.estado_item = "en_bodega"
    rec.estado = "cerrada"
    rec.fecha_cierre = datetime.utcnow()


def _despachado_por_item(db: Session, item_ids: list) -> dict:
    """{item_cotizacion_id: Σ qty_despachada} de despachos NO anulados. Batch."""
    if not item_ids:
        return {}
    rows = (db.query(DespachoItem.item_cotizacion_id,
                     func.coalesce(func.sum(DespachoItem.qty_despachada), 0))
            .join(Despacho, Despacho.id == DespachoItem.despacho_id)
            .filter(DespachoItem.item_cotizacion_id.in_(item_ids),
                    Despacho.estado != "anulado")
            .group_by(DespachoItem.item_cotizacion_id).all())
    return {i: _f(q) for i, q in rows}


def _costeado_por_item(db: Session, item_ids: list, empresa: str) -> dict:
    """{item_cotizacion_id: Σ cantidad costeada} en compras nacionales ACTIVAS. Batch.
    Import local para no cablear compras_contab al importar este módulo."""
    if not item_ids:
        return {}
    from compras_contab.models import ContCompra, ContCompraItem
    rows = (db.query(ContCompraItem.item_cotizacion_id,
                     func.coalesce(func.sum(ContCompraItem.cantidad), 0))
            .join(ContCompra, ContCompra.id == ContCompraItem.compra_id)
            .filter(ContCompraItem.item_cotizacion_id.in_(item_ids),
                    ContCompra.empresa == empresa, ContCompra.anulado.is_(False))
            .group_by(ContCompraItem.item_cotizacion_id).all())
    return {i: _f(q) for i, q in rows}


def _recibido_utilizable_por_item(db: Session, ocp_id: int) -> dict:
    """{item_cotizacion_id: Σ qty_recibida utilizable} de las recepciones nacionales
    CERRADAS de esta OC-Proveedor. Batch (sin N+1)."""
    rows = (db.query(RecepcionNacionalItem.item_cotizacion_id,
                     func.coalesce(func.sum(RecepcionNacionalItem.qty_recibida), 0))
            .join(RecepcionNacional, RecepcionNacional.id == RecepcionNacionalItem.recepcion_id)
            .filter(RecepcionNacional.oc_proveedor_id == ocp_id,
                    RecepcionNacional.estado == "cerrada",
                    RecepcionNacionalItem.estado_recepcion.in_(RECEPCION_UTILIZABLE))
            .group_by(RecepcionNacionalItem.item_cotizacion_id).all())
    return {i: _f(q) for i, q in rows if i is not None}


# ─── Registrar entrega ──────────────────────────────────────────────────────
@router.post("")
def registrar_entrega(
    payload: RegistrarEntregaIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _con_retry_deadlock(db, lambda: _registrar_entrega_tx(payload, db, current_user))


def _registrar_entrega_tx(payload: RegistrarEntregaIn, db: Session, current_user: User):
    """Registra qué llegó de una OC-Proveedor NACIONAL (una entrega = una recepción).
    Si `cerrar` (default), los utilizables pasan a 'en_bodega' y quedan despachables."""
    # Lock de la OC: evita que se registre una entrega mientras otra transacción la
    # cambia (p.ej. la marca internacional por error).
    ocp = (db.query(OcProveedor)
           .filter(OcProveedor.id == payload.oc_proveedor_id)
           .with_for_update().first())
    if not ocp:
        raise HTTPException(404, "OC-Proveedor no encontrada")
    if (ocp.tipo_origen or "internacional") != "nacional":
        raise HTTPException(400, "Esta OC es internacional; use el flujo de embarque/bodega")

    # Ítems asignados a esta OC (item_cotizacion_id → asignación), para validar
    # pertenencia y snapshotear la trazabilidad.
    asign = {a.item_cotizacion_id: a
             for a in db.query(OcProveedorItem)
             .filter(OcProveedorItem.oc_proveedor_id == ocp.id).all()}

    # Carga por lotes de los ItemCotizacion para validar estado y snapshotear datos.
    ids = [ln.item_cotizacion_id for ln in payload.items]
    items_db = {it.id: it for it in db.query(ItemCotizacion)
                .filter(ItemCotizacion.id.in_(ids)).all()}

    vistos = set()
    lineas = []
    for ln in payload.items:
        iid = ln.item_cotizacion_id
        if iid in vistos:
            raise HTTPException(400, f"El ítem {iid} aparece dos veces en la entrega")
        vistos.add(iid)
        if iid not in asign:
            raise HTTPException(400, f"El ítem {iid} no pertenece a esta OC-Proveedor")
        it = items_db.get(iid)
        if not it:
            raise HTTPException(400, f"Ítem {iid} no encontrado")
        if it.estado_item not in _ESTADOS_ITEM_RECIBIBLES:
            raise HTTPException(
                400,
                f"El ítem {it.numero_parte or iid} no está en un estado recibible "
                f"(estado: {it.estado_item})")
        if ln.estado_recepcion not in ESTADOS_VALIDOS:
            raise HTTPException(400, f"estado_recepcion inválido: {ln.estado_recepcion}")
        qty = _f(ln.qty_recibida)
        if qty < 0:
            raise HTTPException(400, "La cantidad recibida no puede ser negativa")
        if ln.estado_recepcion in RECEPCION_UTILIZABLE and qty <= 0:
            raise HTTPException(
                400,
                f"El ítem {it.numero_parte or iid} está marcado como recibido "
                f"({ln.estado_recepcion}) pero con cantidad 0")
        lineas.append((ln, it, asign[iid]))

    rec = RecepcionNacional(
        oc_proveedor_id=ocp.id,
        numero_guia_proveedor=payload.numero_guia_proveedor,
        fecha=_fecha(payload.fecha, "fecha"),
        estado="abierta",
        documento=payload.documento,
        observacion=payload.observacion,
        usuario_id=getattr(current_user, "id", None),
    )
    db.add(rec)
    db.flush()
    for ln, it, asig in lineas:
        db.add(RecepcionNacionalItem(
            recepcion_id=rec.id,
            item_cotizacion_id=ln.item_cotizacion_id,
            oc_proveedor_item_id=asig.id,
            numero_parte=it.numero_parte,
            descripcion=it.descripcion,
            qty_recibida=_f(ln.qty_recibida),
            estado_recepcion=ln.estado_recepcion,
            observacion=ln.observacion,
        ))
    db.flush()
    if payload.cerrar:
        db.refresh(rec)
        _cerrar(db, rec)
    db.commit()
    db.refresh(rec)
    return serialize_recepcion(rec)


# ─── Cerrar (para una recepción creada 'abierta') ────────────────────────────
@router.post("/{recepcion_id}/cerrar")
def cerrar_recepcion(
    recepcion_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _con_retry_deadlock(db, lambda: _cerrar_recepcion_tx(recepcion_id, db))


def _cerrar_recepcion_tx(recepcion_id: int, db: Session):
    rec = (db.query(RecepcionNacional)
           .filter(RecepcionNacional.id == recepcion_id)
           .with_for_update().first())
    if not rec:
        raise HTTPException(404, "Recepción no encontrada")
    if rec.estado == "cerrada":
        raise HTTPException(400, "La recepción ya está cerrada")
    _cerrar(db, rec)
    db.commit()
    db.refresh(rec)
    return serialize_recepcion(rec)


# ─── Anular (con reversa direccionalmente segura) ────────────────────────────
@router.delete("/{recepcion_id}")
def anular_recepcion(
    recepcion_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _con_retry_deadlock(db, lambda: _anular_recepcion_tx(recepcion_id, db, current_user))


def _anular_recepcion_tx(recepcion_id: int, db: Session, current_user: User):
    """Anula una recepción. Si estaba ABIERTA, se borra sin guardas (nunca sumó al
    tope). Si estaba CERRADA, se rechaza (409) si dejaría el tope por debajo de lo
    ya despachado o de lo ya costeado en una compra activa: primero hay que anular
    esos documentos aguas abajo. Si es segura, revierte 'en_bodega'→'comprado' donde
    ya no quede nada recibido y borra la recepción (CASCADE borra sus líneas)."""
    empresa = empresa_de(current_user)
    rec = (db.query(RecepcionNacional)
           .filter(RecepcionNacional.id == recepcion_id)
           .with_for_update().first())
    if not rec:
        raise HTTPException(404, "Recepción no encontrada")

    if rec.estado != "cerrada":
        db.delete(rec)
        db.commit()
        return {"ok": True, "recepcion_id": recepcion_id, "estado_previo": "abierta"}

    # ── Serialización anti write-skew (revisión G13) ──────────────────────────
    # TODOS los caminos que mueven stock/dinero de estos ítems (costear la compra,
    # crear despacho, y esta anulación) se serializan en el lock de ItemCotizacion
    # (orden id ASC, el mismo ancla que despachos/costeo). Sin esto, un costeo
    # concurrente commiteaba entre la lectura del guard y el borrado, dejando
    # Σ costeado > recibido y el tope físico desacotado.
    lineas = list(rec.items)
    item_ids = sorted({ln.item_cotizacion_id for ln in lineas
                       if ln.item_cotizacion_id is not None})
    if item_ids:
        (db.query(ItemCotizacion)
         .filter(ItemCotizacion.id.in_(item_ids))
         .order_by(ItemCotizacion.id.asc())
         .populate_existing().with_for_update().all())

    # Lecturas BLOQUEANTES (current read) para los guards: bajo REPEATABLE READ una
    # lectura normal no vería lo que un costeo/despacho concurrente commiteó después
    # de nacer el snapshot de esta transacción. El orden (compras → recepciones →
    # despachos) espeja el del costeo (ItemCotizacion → compras → recepciones);
    # un deadlock residual lo absorbe el retry del endpoint. Nota: un ítem nacional
    # no participa en embarques, así que su tope proviene solo de recepciones
    # nacionales (la parte de embarque del tope compartido es 0 para estos ítems).
    from compras_contab.models import ContCompra, ContCompraItem
    costeado: dict = {}
    for iid, cant in (db.query(ContCompraItem.item_cotizacion_id, ContCompraItem.cantidad)
                      .join(ContCompra, ContCompra.id == ContCompraItem.compra_id)
                      .filter(ContCompraItem.item_cotizacion_id.in_(item_ids),
                              ContCompra.empresa == empresa,
                              ContCompra.anulado.is_(False))
                      .with_for_update().all()):
        if iid is not None:
            costeado[iid] = costeado.get(iid, 0.0) + _f(cant)
    tope_actual: dict = {}
    for iid, qty in (db.query(RecepcionNacionalItem.item_cotizacion_id,
                              RecepcionNacionalItem.qty_recibida)
                     .join(RecepcionNacional,
                           RecepcionNacional.id == RecepcionNacionalItem.recepcion_id)
                     .filter(RecepcionNacionalItem.item_cotizacion_id.in_(item_ids),
                             RecepcionNacional.estado == "cerrada",
                             RecepcionNacionalItem.estado_recepcion.in_(RECEPCION_UTILIZABLE))
                     .with_for_update().all()):
        if iid is not None:
            tope_actual[iid] = tope_actual.get(iid, 0.0) + _f(qty)
    despachado: dict = {}
    for iid, qty in (db.query(DespachoItem.item_cotizacion_id, DespachoItem.qty_despachada)
                     .join(Despacho, Despacho.id == DespachoItem.despacho_id)
                     .filter(DespachoItem.item_cotizacion_id.in_(item_ids),
                             Despacho.estado != "anulado")
                     .with_for_update().all()):
        if iid is not None:
            despachado[iid] = despachado.get(iid, 0.0) + _f(qty)

    # Guard: por cada ítem utilizable, cuánto quedaría de tope si quitamos esta rec.
    for ri in lineas:
        iid = ri.item_cotizacion_id
        if iid is None or ri.estado_recepcion not in RECEPCION_UTILIZABLE:
            continue
        restante = _f(tope_actual.get(iid, 0)) - _f(ri.qty_recibida)
        if despachado.get(iid, 0.0) > restante + TOL:
            raise HTTPException(
                409,
                f"El ítem {ri.numero_parte or iid} tiene unidades ya despachadas que "
                "dependen de esta recepción; anule primero esos despachos")
        if costeado.get(iid, 0.0) > restante + TOL:
            raise HTTPException(
                409,
                f"El ítem {ri.numero_parte or iid} está costeado en una compra activa "
                "por encima de lo que quedaría recibido; anule primero esa compra")

    # Reversa de estado: para los ítems no despachados donde ya no quede nada recibido,
    # 'en_bodega' → 'comprado' (vuelven a estar 'por recibir').
    for ri in lineas:
        iid = ri.item_cotizacion_id
        if iid is None or ri.estado_recepcion not in RECEPCION_UTILIZABLE:
            continue
        if despachado.get(iid, 0.0) > 0:
            continue
        restante = _f(tope_actual.get(iid, 0)) - _f(ri.qty_recibida)
        if restante <= TOL:
            item = (db.query(ItemCotizacion)
                    .filter(ItemCotizacion.id == iid)
                    .with_for_update().first())
            if item and item.estado_item == "en_bodega":
                item.estado_item = "comprado"

    db.delete(rec)   # CASCADE borra las líneas
    db.commit()
    return {"ok": True, "recepcion_id": recepcion_id, "estado_previo": "cerrada"}


# ─── Pendientes por recibir de una OC nacional (fuente del modal) ────────────
@router.get("/pendientes/{ocp_id}")
def pendientes(
    ocp_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Ítems 'comprado'/'en_bodega' de una OC nacional con su remanente por recibir
    (cantidad vendida − Σ recibido utilizable). Batch, sin N+1."""
    ocp = db.query(OcProveedor).filter(OcProveedor.id == ocp_id).first()
    if not ocp:
        raise HTTPException(404, "OC-Proveedor no encontrada")
    if (ocp.tipo_origen or "internacional") != "nacional":
        raise HTTPException(400, "Esta OC es internacional; no usa recepción nacional")

    asigns = (db.query(OcProveedorItem)
              .filter(OcProveedorItem.oc_proveedor_id == ocp.id).all())
    item_ids = [a.item_cotizacion_id for a in asigns if a.item_cotizacion_id is not None]
    items = {it.id: it for it in db.query(ItemCotizacion)
             .filter(ItemCotizacion.id.in_(item_ids)).all()} if item_ids else {}
    recibido = _recibido_utilizable_por_item(db, ocp.id)

    out = []
    for a in asigns:
        it = items.get(a.item_cotizacion_id)
        if not it or it.estado_item not in _ESTADOS_ITEM_RECIBIBLES:
            continue
        cant = _f(it.cantidad)
        recib = recibido.get(it.id, 0.0)
        out.append({
            "item_cotizacion_id": it.id,
            "oc_proveedor_item_id": a.id,
            "numero_parte": it.numero_parte,
            "descripcion": it.descripcion,
            "estado_item": it.estado_item,
            "cantidad": cant,
            "recibido": recib,
            "remanente": max(cant - recib, 0.0),
        })
    return {
        "oc_proveedor_id": ocp.id,
        "numero": ocp.numero,
        "numero_oc": ocp.numero_oc,
        "proveedor": ocp.proveedor,
        "items": out,
    }


# ─── Listado / detalle (trazabilidad) ────────────────────────────────────────
@router.get("")
def listar_recepciones(
    ocp_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    base = db.query(RecepcionNacional).options(selectinload(RecepcionNacional.items))
    if ocp_id:
        base = base.filter(RecepcionNacional.oc_proveedor_id == int(ocp_id))
    rows = base.order_by(RecepcionNacional.id.desc()).all()
    return {"recepciones": [serialize_recepcion(r) for r in rows], "total": len(rows)}


@router.get("/{recepcion_id}")
def detalle_recepcion(
    recepcion_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rec = (db.query(RecepcionNacional)
           .options(selectinload(RecepcionNacional.items))
           .filter(RecepcionNacional.id == recepcion_id).first())
    if not rec:
        raise HTTPException(404, "Recepción no encontrada")
    return serialize_recepcion(rec)
