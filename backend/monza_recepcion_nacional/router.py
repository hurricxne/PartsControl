"""API del módulo Recepción Nacional MonzaParts (camino físico de la compra nacional).

Prefijo: /api/monza/recepcion-nacional (montado SIN prefix extra; el router ya lo
trae, patrón de todos los routers monza — OJO: GA en cambio monta con prefix=/api).
Bodega registra "cuánto llegó" de cada ítem de una OC-Proveedor NACIONAL cuando el
camión del proveedor llega con su guía de despacho. Al cerrar la recepción, los
ítems utilizables con cantidad > 0 pasan a 'en_bodega' y quedan despachables,
capados por lo recibido (tope físico F2/G6). Scope: SOLO MonzaParts ('automotriz').

El backend es la autoridad: valida qué ítems pertenecen a la OC, que la OC sea
nacional, y NUNCA confía en el cliente para el tope. Locks with_for_update donde
hay stock/estado concurrente (cerrar/anular).

ADAPTACIÓN Monza (vs recepcion_nacional/router.py de GA): sin tabla OcProveedorItem
— la pertenencia se valida por MonzaCotizacionItem.oc_proveedor_id directo, y el
estado de línea vive en MonzaCotizacionItem.estado_linea (no estado_item).
"""
from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload
from pydantic import BaseModel, Field

from database import get_db
from auth import get_current_user
from empresa_guard import require_empresa
from models.models import User
from monza_models import (
    MonzaOcProveedor, MonzaCotizacionItem, MonzaDespacho, MonzaDespachoItem,
)

from .models import (
    MonzaRecepcionNacional, MonzaRecepcionNacionalItem,
    RECEPCION_UTILIZABLE, ESTADOS_VALIDOS,
)
from .service import _f, parse_date_estricta, serialize_recepcion

# Módulo SOLO MonzaParts ('automotriz'). Los routers del programador no llevan
# guard; los módulos nuevos SÍ (patrón monza_compras_contab/router.py): el guard
# de router deniega (403) el acceso a usuarios de otra empresa.
router = APIRouter(
    prefix="/api/monza/recepcion-nacional",
    tags=["monza-recepcion-nacional"],
    dependencies=[Depends(require_empresa("automotriz"))],
)

TOL = 0.001  # holgura en unidades para comparar tope vs despachado/costeado

# Estados de MonzaCotizacionItem.estado_linea sobre los que se puede registrar una
# entrega nacional: 'comprado' (recién asignado a la OC) o 'en_bodega' (entregas
# sucesivas sobre el remanente que ya recibió parte). Un nacional NUNCA pasa por
# preparado/embarcado.
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
    MonzaCotizacionItem compartidos con costeo y despachos) puede elegir una
    víctima; MySQL la mata y aquí se reintenta la transacción completa en vez de
    devolver un 500 al operador (mismo patrón que crear despachos Monza / GA)."""
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


def _cerrar(db: Session, rec: MonzaRecepcionNacional) -> None:
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
            db.query(MonzaCotizacionItem)
            .filter(MonzaCotizacionItem.id.in_(ids))
            .order_by(MonzaCotizacionItem.id.asc())
            .populate_existing().with_for_update().all())}
    for ri in rec.items:
        item = items_map.get(ri.item_cotizacion_id)
        if not item:
            continue
        # Solo se avanza desde 'comprado': cerrar una recepción tardía (que quedó
        # abierta y el ítem ya se despachó) NO debe retroceder 'despachado' →
        # 'en_bodega' (regresión de estado detectada en la revisión G13).
        if (ri.estado_recepcion in RECEPCION_UTILIZABLE and _f(ri.qty_recibida) > 0
                and item.estado_linea == "comprado"):
            item.estado_linea = "en_bodega"
    rec.estado = "cerrada"
    rec.fecha_cierre = datetime.utcnow()


def _avisar_ventas_listas(db: Session, item_ids: list) -> None:
    """Hallazgo #11: la vía embarque avisaba «venta lista para despacho» al cerrar la
    recepción y la vía NACIONAL no, así que con compras nacionales (F8) la venta
    quedaba lista y nadie se enteraba en Despachos (ni salía la alerta de «plazo
    crítico» en llegadas parciales).

    Se REUSA _notificar_ventas_listas de monza_router_bodega en vez de duplicarlo:
    ya trae su anti-duplicado (misma venta + mismo título sin leer) y su filtro
    cot.estado in ('vendida','despachado'), así que no hace falta lógica extra.

    Import LOCAL dentro de la función (estilo del repo, igual que
    monza_router_bodega → monza_router_despachos): verificado que
    monza_router_bodega NO importa monza_recepcion_nacional, así que no hay ciclo.

    SIEMPRE fuera de la transacción (después del commit): crear_notif commitea por
    su cuenta. Y envuelto en try/except vacío — un fallo de notificación jamás
    puede tumbar un cierre de recepción que ya está persistido.
    """
    if not item_ids:
        return
    try:
        from monza_router_bodega import _notificar_ventas_listas
        _notificar_ventas_listas(db, item_ids)
    except Exception:
        pass


def _despachado_por_item(db: Session, item_ids: list) -> dict:
    """{item_cotizacion_id: Σ qty_despachada} de despachos NO anulados. Batch."""
    if not item_ids:
        return {}
    rows = (db.query(MonzaDespachoItem.item_id,
                     func.coalesce(func.sum(MonzaDespachoItem.qty_despachada), 0))
            .join(MonzaDespacho, MonzaDespacho.id == MonzaDespachoItem.despacho_id)
            .filter(MonzaDespachoItem.item_id.in_(item_ids),
                    MonzaDespacho.estado != "anulado")
            .group_by(MonzaDespachoItem.item_id).all())
    return {i: _f(q) for i, q in rows}


def _costeado_por_item(db: Session, item_ids: list) -> dict:
    """{item_cotizacion_id: Σ cantidad costeada} en compras nacionales ACTIVAS. Batch.
    Import local para no cablear monza_compras_contab al importar este módulo.
    Sin filtro de empresa: monza_cont_compra es una tabla exclusiva de MonzaParts
    (no lleva columna empresa, a diferencia de cont_compra en GA)."""
    if not item_ids:
        return {}
    try:
        # CONTRATO con el costeo por ítem (F8): lo crea monza_compras_contab (C4).
        from monza_compras_contab.models import MonzaContCompra, MonzaContCompraItem
    except ImportError:
        # Aún sin costeo por ítem desplegado → nada costeado que proteger.
        return {}
    rows = (db.query(MonzaContCompraItem.item_cotizacion_id,
                     func.coalesce(func.sum(MonzaContCompraItem.cantidad), 0))
            .join(MonzaContCompra, MonzaContCompra.id == MonzaContCompraItem.compra_id)
            .filter(MonzaContCompraItem.item_cotizacion_id.in_(item_ids),
                    MonzaContCompra.anulado.is_(False))
            .group_by(MonzaContCompraItem.item_cotizacion_id).all())
    return {i: _f(q) for i, q in rows}


def _costeo_por_item_disponible(db: Session) -> bool:
    """¿Existe la tabla de costeo por ítem (`monza_cont_compra_item`) en esta base?

    Dos fallas distintas, las dos benignas:
      · ImportError → el paquete monza_compras_contab no está en el deploy.
      · MySQL 1146 (ProgrammingError) → el paquete está, pero su init_db no se corrió,
        así que la tabla NO existe. MonzaParts está MÁS expuesta que Grupo AM:
        monza_compras_contab se importa DENTRO del gate MONZA_CONTAB_ENABLED
        (main.py), o sea DESPUÉS del create_all, así que monza_cont_compra_item NUNCA
        se autocrea — el `try/except ImportError` de acá protegía el IMPORT, no la
        QUERY, y el retry del endpoint solo atrapa OperationalError 1213/1205.
    En los dos casos ese módulo JAMÁS costeó nada que proteger, así que la comprobación
    se apaga sola en vez de tumbar la anulación con un 500 (patrón de
    `monza_router_despachos::_opcional`).

    Se pregunta ACÁ, al principio del endpoint y ANTES de tomar cualquier lock, y no
    envolviendo la query del guard: ese `db.rollback()` SUELTA los locks del llamador
    (el de MonzaCotizacionItem en orden id ASC que serializa costeo / despachos /
    anulación) y volver a leer sin lock reabriría justo el write-skew que el guard
    existe para cerrar. Acá el rollback no cuesta nada: todavía no se leyó ni lockeó.
    """
    from sqlalchemy.exc import ProgrammingError
    try:
        # CONTRATO F8: la tabla de costeo por ítem la crea monza_compras_contab (C4).
        from monza_compras_contab.models import MonzaContCompra, MonzaContCompraItem
    except ImportError:
        return False
    try:
        # Toca las DOS tablas del guard (el JOIN real), no solo una.
        (db.query(MonzaContCompraItem.id)
         .join(MonzaContCompra, MonzaContCompra.id == MonzaContCompraItem.compra_id)
         .limit(1).all())
        return True
    except ProgrammingError as e:
        if getattr(getattr(e, "orig", None), "args", [None])[0] == 1146:
            db.rollback()
            return False
        raise


def _recibido_utilizable_por_item(db: Session, ocp_id: int) -> dict:
    """{item_cotizacion_id: Σ qty_recibida utilizable} de las recepciones nacionales
    CERRADAS de esta OC-Proveedor. Batch (sin N+1)."""
    rows = (db.query(MonzaRecepcionNacionalItem.item_cotizacion_id,
                     func.coalesce(func.sum(MonzaRecepcionNacionalItem.qty_recibida), 0))
            .join(MonzaRecepcionNacional,
                  MonzaRecepcionNacional.id == MonzaRecepcionNacionalItem.recepcion_id)
            .filter(MonzaRecepcionNacional.oc_proveedor_id == ocp_id,
                    MonzaRecepcionNacional.estado == "cerrada",
                    MonzaRecepcionNacionalItem.estado_recepcion.in_(RECEPCION_UTILIZABLE))
            .group_by(MonzaRecepcionNacionalItem.item_cotizacion_id).all())
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
    ocp = (db.query(MonzaOcProveedor)
           .filter(MonzaOcProveedor.id == payload.oc_proveedor_id)
           .with_for_update().first())
    if not ocp:
        raise HTTPException(404, "OC-Proveedor no encontrada")
    if (ocp.tipo_origen or "internacional") != "nacional":
        raise HTTPException(400, "Esta OC es internacional; use el flujo de embarque/bodega")

    # Carga por lotes de los MonzaCotizacionItem para validar pertenencia/estado y
    # snapshotear datos. ADAPTACIÓN Monza: la pertenencia se valida contra
    # item.oc_proveedor_id directo (no hay tabla de asignación como en GA); un
    # ítem inexistente cae en el mismo 400 'no pertenece' (no se puede distinguir
    # sin la tabla intermedia, y para el operador es el mismo error).
    ids = [ln.item_cotizacion_id for ln in payload.items]
    items_db = {it.id: it for it in db.query(MonzaCotizacionItem)
                .filter(MonzaCotizacionItem.id.in_(ids)).all()}

    vistos = set()
    lineas = []
    for ln in payload.items:
        iid = ln.item_cotizacion_id
        if iid in vistos:
            raise HTTPException(400, f"El ítem {iid} aparece dos veces en la entrega")
        vistos.add(iid)
        it = items_db.get(iid)
        if not it or it.oc_proveedor_id != ocp.id:
            raise HTTPException(400, f"El ítem {iid} no pertenece a esta OC-Proveedor")
        if it.estado_linea not in _ESTADOS_ITEM_RECIBIBLES:
            raise HTTPException(
                400,
                f"El ítem {it.numero_parte or iid} no está en un estado recibible "
                f"(estado: {it.estado_linea})")
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
        lineas.append((ln, it))

    rec = MonzaRecepcionNacional(
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
    for ln, it in lineas:
        db.add(MonzaRecepcionNacionalItem(
            recepcion_id=rec.id,
            item_cotizacion_id=ln.item_cotizacion_id,
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
    if payload.cerrar:
        _avisar_ventas_listas(db, [ln.item_cotizacion_id for ln, _it in lineas
                                   if ln.item_cotizacion_id])
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
    rec = (db.query(MonzaRecepcionNacional)
           .filter(MonzaRecepcionNacional.id == recepcion_id)
           .with_for_update().first())
    if not rec:
        raise HTTPException(404, "Recepción no encontrada")
    if rec.estado == "cerrada":
        raise HTTPException(400, "La recepción ya está cerrada")
    _cerrar(db, rec)
    item_ids = [ri.item_cotizacion_id for ri in rec.items if ri.item_cotizacion_id]
    db.commit()
    db.refresh(rec)
    _avisar_ventas_listas(db, item_ids)
    return serialize_recepcion(rec)


# ─── Anular (con reversa direccionalmente segura) ────────────────────────────
@router.delete("/{recepcion_id}")
def anular_recepcion(
    recepcion_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _con_retry_deadlock(db, lambda: _anular_recepcion_tx(recepcion_id, db))


def _anular_recepcion_tx(recepcion_id: int, db: Session):
    """Anula una recepción. Si estaba ABIERTA, se borra sin guardas (nunca sumó al
    tope). Si estaba CERRADA, se rechaza (409) si dejaría el tope por debajo de lo
    ya despachado o de lo ya costeado en una compra activa: primero hay que anular
    esos documentos aguas abajo. Si es segura, revierte 'en_bodega'→'comprado' donde
    ya no quede nada recibido y borra la recepción (CASCADE borra sus líneas)."""
    # ¿Está desplegado el costeo por ítem? Se pregunta ANTES de cualquier lock: si no
    # lo está (deploy sin correr monza_compras_contab.init_db), la comprobación de
    # costeo se apaga en vez de reventar con un 500 en medio de la anulación. Va acá
    # porque el rollback de rescate soltaría los locks si se preguntara más abajo.
    costeo_disponible = _costeo_por_item_disponible(db)
    rec = (db.query(MonzaRecepcionNacional)
           .filter(MonzaRecepcionNacional.id == recepcion_id)
           .with_for_update().first())
    if not rec:
        raise HTTPException(404, "Recepción no encontrada")

    if rec.estado != "cerrada":
        db.delete(rec)
        db.commit()
        return {"ok": True, "recepcion_id": recepcion_id, "estado_previo": "abierta"}

    # ── Serialización anti write-skew (lección G13) ───────────────────────────
    # TODOS los caminos que mueven stock/dinero de estos ítems (costear la compra,
    # crear despacho, y esta anulación) se serializan en el lock de
    # MonzaCotizacionItem (orden id ASC, el mismo ancla que despachos/costeo). Sin
    # esto, un costeo concurrente commiteaba entre la lectura del guard y el
    # borrado, dejando Σ costeado > recibido y el tope físico desacotado.
    lineas = list(rec.items)
    item_ids = sorted({ln.item_cotizacion_id for ln in lineas
                       if ln.item_cotizacion_id is not None})
    if item_ids:
        (db.query(MonzaCotizacionItem)
         .filter(MonzaCotizacionItem.id.in_(item_ids))
         .order_by(MonzaCotizacionItem.id.asc())
         .populate_existing().with_for_update().all())

    # Lecturas BLOQUEANTES (current read) para los guards: bajo REPEATABLE READ una
    # lectura normal no vería lo que un costeo/despacho concurrente commiteó después
    # de nacer el snapshot de esta transacción. El orden (compras → recepciones →
    # despachos) espeja el del costeo (MonzaCotizacionItem → compras → recepciones);
    # un deadlock residual lo absorbe el retry del endpoint. Se suma fila a fila en
    # Python porque func.sum() + FOR UPDATE no combinan. Nota: un ítem nacional no
    # participa en embarques (guard anti-embarque), así que su tope proviene solo de
    # recepciones nacionales (la parte de embarque del tope compartido es 0).
    costeado: dict = {}
    if costeo_disponible:
        # CONTRATO F8: tabla de costeo por ítem de monza_compras_contab (C4).
        from monza_compras_contab.models import MonzaContCompra, MonzaContCompraItem
        for iid, cant in (db.query(MonzaContCompraItem.item_cotizacion_id,
                                   MonzaContCompraItem.cantidad)
                          .join(MonzaContCompra,
                                MonzaContCompra.id == MonzaContCompraItem.compra_id)
                          .filter(MonzaContCompraItem.item_cotizacion_id.in_(item_ids),
                                  MonzaContCompra.anulado.is_(False))
                          .with_for_update().all()):
            if iid is not None:
                costeado[iid] = costeado.get(iid, 0.0) + _f(cant)
    tope_actual: dict = {}
    for iid, qty in (db.query(MonzaRecepcionNacionalItem.item_cotizacion_id,
                              MonzaRecepcionNacionalItem.qty_recibida)
                     .join(MonzaRecepcionNacional,
                           MonzaRecepcionNacional.id == MonzaRecepcionNacionalItem.recepcion_id)
                     .filter(MonzaRecepcionNacionalItem.item_cotizacion_id.in_(item_ids),
                             MonzaRecepcionNacional.estado == "cerrada",
                             MonzaRecepcionNacionalItem.estado_recepcion.in_(RECEPCION_UTILIZABLE))
                     .with_for_update().all()):
        if iid is not None:
            tope_actual[iid] = tope_actual.get(iid, 0.0) + _f(qty)
    despachado: dict = {}
    for iid, qty in (db.query(MonzaDespachoItem.item_id, MonzaDespachoItem.qty_despachada)
                     .join(MonzaDespacho, MonzaDespacho.id == MonzaDespachoItem.despacho_id)
                     .filter(MonzaDespachoItem.item_id.in_(item_ids),
                             MonzaDespacho.estado != "anulado")
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
            item = (db.query(MonzaCotizacionItem)
                    .filter(MonzaCotizacionItem.id == iid)
                    .with_for_update().first())
            if item and item.estado_linea == "en_bodega":
                item.estado_linea = "comprado"

    db.delete(rec)   # CASCADE borra las líneas
    db.commit()
    return {"ok": True, "recepcion_id": recepcion_id, "estado_previo": "cerrada"}


# ─── Pendientes por recibir de una OC nacional (fuente del modal) ────────────
# DECLARADO ANTES de GET /{recepcion_id} para que 'pendientes' no matchee como id.
@router.get("/pendientes/{ocp_id}")
def pendientes(
    ocp_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Ítems 'comprado'/'en_bodega' de una OC nacional con su remanente por recibir
    (cantidad vendida − Σ recibido utilizable). Batch, sin N+1."""
    ocp = db.query(MonzaOcProveedor).filter(MonzaOcProveedor.id == ocp_id).first()
    if not ocp:
        raise HTTPException(404, "OC-Proveedor no encontrada")
    if (ocp.tipo_origen or "internacional") != "nacional":
        raise HTTPException(400, "Esta OC es internacional; no usa recepción nacional")

    # ADAPTACIÓN Monza: los ítems de la OC salen del vínculo directo
    # MonzaCotizacionItem.oc_proveedor_id (no hay tabla de asignación).
    items = (db.query(MonzaCotizacionItem)
             .filter(MonzaCotizacionItem.oc_proveedor_id == ocp.id)
             .order_by(MonzaCotizacionItem.id.asc()).all())
    recibido = _recibido_utilizable_por_item(db, ocp.id)

    out = []
    for it in items:
        if it.estado_linea not in _ESTADOS_ITEM_RECIBIBLES:
            continue
        cant = _f(it.cantidad)
        recib = recibido.get(it.id, 0.0)
        out.append({
            "item_cotizacion_id": it.id,
            "numero_parte": it.numero_parte,
            "descripcion": it.descripcion,
            "estado_item": it.estado_linea,
            "cantidad": cant,
            "recibido": recib,
            "remanente": max(cant - recib, 0.0),
        })
    return {
        "oc_proveedor_id": ocp.id,
        "numero": ocp.numero,
        "numero_oc": ocp.numero_oc,
        "proveedor": ocp.proveedor_nombre,
        "items": out,
    }


# ─── Listado / detalle (trazabilidad) ────────────────────────────────────────
@router.get("")
def listar_recepciones(
    ocp_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    base = (db.query(MonzaRecepcionNacional)
            .options(selectinload(MonzaRecepcionNacional.items)))
    if ocp_id:
        base = base.filter(MonzaRecepcionNacional.oc_proveedor_id == int(ocp_id))
    rows = base.order_by(MonzaRecepcionNacional.id.desc()).all()
    return {"recepciones": [serialize_recepcion(r) for r in rows], "total": len(rows)}


@router.get("/{recepcion_id}")
def detalle_recepcion(
    recepcion_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rec = (db.query(MonzaRecepcionNacional)
           .options(selectinload(MonzaRecepcionNacional.items))
           .filter(MonzaRecepcionNacional.id == recepcion_id).first())
    if not rec:
        raise HTTPException(404, "Recepción no encontrada")
    return serialize_recepcion(rec)
