"""API del módulo Contabilidad MonzaParts — Ventas + Facturas/Cobranzas/Factoring.

Prefijo: /api/monza/contabilidad (se monta sin prefix; el router ya lo trae, como el
resto de routers Monza). SOLO MonzaParts: candado require_empresa("automotriz").

Es el lado de CUENTAS POR COBRAR de MonzaParts. Espejo de routers/contabilidad.py de
Grupo AM, pero:
  - La VENTA es una MonzaCotizacion (estado 'vendida'/'despachado'); cliente y montos
    ya viven en la cotización y sus ítems (precio neto ya calculado → sin pricing_service).
  - Se factura una guía de despacho 'despachado' (MonzaDespacho), con doble tope por
    ÍTEM y por GUÍA contra lo ya facturado. La firma de la guía es OPCIONAL/registrable
    (no bloquea la emisión).

Endpoints (todos requieren autenticación + empresa automotriz):
  GET    /ventas                              listado de ventas (cotizaciones) + resumen cobranza
  GET    /ventas/{cot_id}                     detalle de una venta (ítems, guías, facturas)
  GET    /ventas/{cot_id}/despachos-facturables  guías despachadas aún facturables
  PATCH  /ventas/despachos/{desp_id}/guia-firmada  marca/registra la guía firmada (opcional)
  POST   /ventas/{cot_id}/adelanto/verificar  Contabilidad verifica el adelanto informado

Flujo de ADELANTO (ej. 50% personas naturales):
  Comercial cierra la venta indicando pct_adelanto (campo en MonzaCotizacion) → la venta
  queda "por_verificar" → Contabilidad verifica (POST .../adelanto/verificar: guarda
  MonzaContAdelanto con monto/fecha/banco y marca adelanto_verificado=1) → al EMITIR la
  factura, _aplicar_adelanto registra el adelanto como cobranza 'adelanto' (descuenta saldo);
  monto_aplicado evita aplicarlo dos veces y se devuelve si se revierte esa cobranza.
  Trazabilidad: la cobranza medio='adelanto' liga el dinero a la factura.
  GET    /facturas                            listado de facturas + antigüedad de cartera
  POST   /facturas                            EMITIR una factura (desde una guía o ítems)
  DELETE /facturas/{id}                       borrado seguro (no si hay pagos/factoring)
  POST   /facturas/{id}/cobranzas             registrar un pago del cliente
  DELETE /facturas/{id}/cobranzas/{id}        revertir un pago
  POST   /facturas/{id}/factoring             ceder la factura a un factor
  POST   /facturas/{id}/factoring/liquidar    liquidar el factoring
  GET    /kpis                                indicadores de cobranza
"""
import logging
from datetime import date, timedelta
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session, selectinload, joinedload, contains_eager
from sqlalchemy.exc import IntegrityError

from database import get_db
from auth import get_current_user
from empresa_guard import require_empresa
from models.models import User
from monza_models import (
    MonzaCotizacion, MonzaCotizacionItem, MonzaDespacho, MonzaDespachoItem, MonzaConfig, MonzaCliente,
)
from .models import (
    MonzaContFacturaCliente, MonzaContFacturaClienteItem, MonzaContCobranza, MonzaContFactoring,
    MonzaContAdelanto,
)
from .schemas import FacturaItemIn, FacturaCreate, CobranzaIn, FactoringIn, GuiaFirmadaIn, AdelantoVerificarIn
from .service import (
    TOL, TOL_QTY, TOL_PAGO, MEDIO_FACT_ADELANTO, MEDIO_FACT_RETENCION, MEDIO_ADELANTO,
    _f, _parse_date, _es_medio_factoring, iva_rate_de,
    _semaforo, _recompute_factura, _serialize_factura, _resumen_cobranza, _periodo_filter,
    periodo_floor, estado_adelanto,
)
# Solo lectura de los enlaces de conciliación de Tesorería (los MODELS de ambos módulos
# solo importan database, así que no hay ciclo):
#   · MonzaTesConciliacionIngreso (abono ↔ cobranza): una cobranza conciliada con el
#     banco no se puede borrar sin desconciliarla primero allá.
#   · MonzaTesConciliacion (abono ↔ adelanto): un adelanto conciliado no se puede
#     re-verificar (editar monto) sin desconciliar el abono primero.
from monza_tesoreria.models import MonzaTesConciliacion, MonzaTesConciliacionIngreso

logger = logging.getLogger("monza_contabilidad")

# Estados de cotización que cuentan como "venta" facturable.
ESTADOS_VENTA = ("vendida", "despachado")

# Eager loading de las relaciones hijas de la factura (evita N+1 al serializar).
_FACTURA_EAGER = (
    selectinload(MonzaContFacturaCliente.items),
    selectinload(MonzaContFacturaCliente.cobranzas),
    selectinload(MonzaContFacturaCliente.factoring),
)

router = APIRouter(
    prefix="/api/monza/contabilidad",
    tags=["monza-contabilidad"],
    dependencies=[Depends(require_empresa("automotriz"))],
)


# ── Helpers de BD ──────────────────────────────────────────────────────────────
def _config(db: Session) -> Optional[MonzaConfig]:
    return db.query(MonzaConfig).order_by(MonzaConfig.id.asc()).first()


def _despacho_items_de_cot(db: Session, cot_id: int):
    """(MonzaDespachoItem, MonzaDespacho) facturables de la cotización: despachos en
    estado 'despachado'. La firma de la guía NO se exige (es opcional)."""
    return (
        db.query(MonzaDespachoItem, MonzaDespacho)
        .filter(
            MonzaDespacho.id == MonzaDespachoItem.despacho_id,
            MonzaDespacho.cotizacion_id == cot_id,
            MonzaDespacho.estado == "despachado",
        )
        .all()
    )


def _qty_despachada_por_item(db: Session, cot_id: int) -> dict:
    out = {}
    for di, _d in _despacho_items_de_cot(db, cot_id):
        out[di.item_id] = out.get(di.item_id, 0.0) + _f(di.qty_despachada)
    return out


def _qty_facturada_por_item(db: Session, cot_id: int) -> dict:
    rows = (
        db.query(MonzaContFacturaClienteItem.item_cotizacion_id, MonzaContFacturaClienteItem.cantidad)
        .join(MonzaContFacturaCliente, MonzaContFacturaCliente.id == MonzaContFacturaClienteItem.factura_id)
        .filter(MonzaContFacturaCliente.cotizacion_id == cot_id)
        .all()
    )
    out = {}
    for iid, qty in rows:
        if iid is not None:
            out[iid] = out.get(iid, 0.0) + _f(qty)
    return out


def _qty_facturada_por_despacho_item(db: Session, cot_id: int) -> dict:
    rows = (
        db.query(MonzaContFacturaClienteItem.despacho_item_id, MonzaContFacturaClienteItem.cantidad)
        .join(MonzaContFacturaCliente, MonzaContFacturaCliente.id == MonzaContFacturaClienteItem.factura_id)
        .filter(
            MonzaContFacturaCliente.cotizacion_id == cot_id,
            MonzaContFacturaClienteItem.despacho_item_id.isnot(None),
        )
        .all()
    )
    out = {}
    for did, qty in rows:
        out[did] = out.get(did, 0.0) + _f(qty)
    return out


def _facturas_de_cot(db: Session, cot_id: int) -> List[MonzaContFacturaCliente]:
    return (
        db.query(MonzaContFacturaCliente)
        .options(*_FACTURA_EAGER)
        .filter(MonzaContFacturaCliente.cotizacion_id == cot_id)
        .order_by(MonzaContFacturaCliente.id.asc())
        .all()
    )


def _adelanto_de_cot(db: Session, cot_id: int) -> Optional[MonzaContAdelanto]:
    return db.query(MonzaContAdelanto).filter(MonzaContAdelanto.cotizacion_id == cot_id).first()


def _adelantos_by_cot(db: Session, cot_ids: List[int]) -> dict:
    """Adelantos de varias ventas en una sola query (evita N+1 en el listado)."""
    out: dict = {}
    if cot_ids:
        for a in db.query(MonzaContAdelanto).filter(MonzaContAdelanto.cotizacion_id.in_(cot_ids)).all():
            out[a.cotizacion_id] = a
    return out


def _aplicar_adelanto(db: Session, cot: MonzaCotizacion, factura: MonzaContFacturaCliente,
                      usuario_id=None) -> None:
    """Aplica el adelanto VERIFICADO de la venta (si tiene saldo no aplicado) como una
    cobranza 'adelanto' sobre esta factura, hasta el monto de la factura. `monto_aplicado`
    evita aplicar dos veces y soporta facturación parcial. Se llama dentro de crear_factura,
    con la cotización ya bloqueada (serializa la aplicación concurrente).

    INVARIANTE: adel.monto_aplicado == suma de cobranzas 'adelanto' de las facturas de la
    venta. Si se revierte una cobranza 'adelanto' (eliminar_cobranza), se descuenta de vuelta.
    """
    adel = _adelanto_de_cot(db, cot.id)
    if adel is None:
        return
    pendiente = _f(adel.monto) - _f(adel.monto_aplicado)
    aplicar = round(min(pendiente, _f(factura.monto_bruto)), 2)
    if aplicar <= TOL:
        return
    db.add(MonzaContCobranza(
        factura_id=factura.id, fecha=adel.fecha_pago or date.today(),
        monto=aplicar, medio=MEDIO_ADELANTO, banco=adel.banco,
        numero_operacion=adel.numero_operacion,
        observaciones=f"Adelanto {int(getattr(cot, 'pct_adelanto', 0) or 0)}% aplicado",
        usuario_id=usuario_id,
    ))
    adel.monto_aplicado = round(_f(adel.monto_aplicado) + aplicar, 2)


# ── Ventas (agrupado por cotización vendida/despachada) ────────────────────────
@router.get("/ventas")
def listar_ventas(
    q: Optional[str] = None,
    periodo: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Lista las ventas (cotizaciones vendidas/despachadas) con su resumen de cobranza.
    `q` busca en N° cotización / cliente / RUT / OC cliente; `periodo` filtra por fecha
    de venta (semana | mes | anio)."""
    base = (
        db.query(MonzaCotizacion)
        .options(selectinload(MonzaCotizacion.items), contains_eager(MonzaCotizacion.cliente))
        .outerjoin(MonzaCliente, MonzaCliente.id == MonzaCotizacion.cliente_id)
        .filter(MonzaCotizacion.estado.in_(ESTADOS_VENTA))
    )
    if q:
        like = f"%{q}%"
        base = base.filter(or_(
            MonzaCotizacion.numero.ilike(like), MonzaCotizacion.oc_cliente.ilike(like),
            MonzaCliente.nombre.ilike(like), MonzaCliente.rut.ilike(like),
        ))
    cots = base.order_by(MonzaCotizacion.id.desc()).all()
    # Facturas de TODAS las ventas en una sola query (evita N+1), con hijos eager.
    cot_ids = [c.id for c in cots]
    fac_by_cot: dict = {}
    if cot_ids:
        for f in (
            db.query(MonzaContFacturaCliente).options(*_FACTURA_EAGER)
            .filter(MonzaContFacturaCliente.cotizacion_id.in_(cot_ids)).all()
        ):
            fac_by_cot.setdefault(f.cotizacion_id, []).append(f)
    adel_by_cot = _adelantos_by_cot(db, cot_ids)
    result = []
    for cot in cots:
        fecha_ref = cot.fecha_venta or cot.fecha_creacion
        if not _periodo_filter(fecha_ref, periodo):
            continue
        cli = cot.cliente
        facturas = fac_by_cot.get(cot.id, [])
        resumen = _resumen_cobranza(facturas)
        result.append({
            "cotizacion_id": cot.id,
            "numero_cotizacion": cot.numero,
            "cliente": (cli.nombre if cli else "") or "",
            "rut_cliente": (cli.rut if cli else "") or "",
            "oc_cliente": cot.oc_cliente,
            "vehiculo": cot.vehiculo,
            "estado": cot.estado,
            "fecha_venta": cot.fecha_venta.isoformat() if cot.fecha_venta else None,
            "fecha_creacion": cot.fecha_creacion.isoformat() if cot.fecha_creacion else None,
            "cond_pago": cot.forma_pago,
            "total_items": len(cot.items),
            "total_neto_clp": round(_f(cot.total_neto), 0),
            "iva_clp": round(_f(cot.iva_monto), 0),
            "total_con_iva_clp": round(_f(cot.total_bruto), 0),
            **resumen,
            **estado_adelanto(cot, adel_by_cot.get(cot.id)),
        })
    return result


@router.get("/ventas/{cot_id}")
def detalle_venta(
    cot_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Detalle de una venta: por cada ítem su precio de venta neto, las guías de
    despacho (con estado/firma) y las facturas asociadas; más las facturas serializadas
    y el resumen de cobranza. 404 si la cotización no existe."""
    cot = (
        db.query(MonzaCotizacion)
        .options(selectinload(MonzaCotizacion.items), joinedload(MonzaCotizacion.cliente))
        .filter(MonzaCotizacion.id == cot_id)
        .first()
    )
    if not cot:
        raise HTTPException(404, "Venta (cotización) no encontrada")
    cli = cot.cliente

    # ítem -> guías (despachos no anulados de la cotización)
    desp_rows = (
        db.query(MonzaDespachoItem, MonzaDespacho)
        .filter(
            MonzaDespacho.id == MonzaDespachoItem.despacho_id,
            MonzaDespacho.cotizacion_id == cot.id,
            MonzaDespacho.estado != "anulado",
        )
        .all()
    )
    guias_por_item = {}
    for di, d in desp_rows:
        guias_por_item.setdefault(di.item_id, []).append({
            "despacho_item_id": di.id, "despacho_id": d.id,
            "numero_despacho": d.numero, "numero_guia": d.numero_guia,
            "estado": d.estado, "qty_despachada": _f(di.qty_despachada),
            "guia_firmada": bool(getattr(d, "guia_firmada", 0)),
            "guia_firmada_archivo": getattr(d, "guia_firmada_archivo", None),
        })

    # ítem -> facturas
    fac_rows = (
        db.query(MonzaContFacturaClienteItem, MonzaContFacturaCliente)
        .filter(
            MonzaContFacturaCliente.id == MonzaContFacturaClienteItem.factura_id,
            MonzaContFacturaCliente.cotizacion_id == cot.id,
        )
        .all()
    )
    facturas_por_item = {}
    for fi, f in fac_rows:
        facturas_por_item.setdefault(fi.item_cotizacion_id, []).append({
            "factura_id": f.id, "numero_factura": f.numero_factura,
            "fecha_emision": f.fecha_emision.isoformat() if f.fecha_emision else None,
            "plazo_dias": f.plazo_dias,
            "fecha_vencimiento": f.fecha_vencimiento.isoformat() if f.fecha_vencimiento else None,
            "estado_pago": f.estado_pago, "cantidad": _f(fi.cantidad),
        })

    items_out = []
    for it in cot.items:
        items_out.append({
            "id": it.id,
            "numero_parte": it.numero_parte or "",
            "descripcion": it.descripcion or "",
            "marca": it.marca or "",
            "cantidad": _f(it.cantidad),
            "precio_unit_venta_clp": round(_f(it.precio_unitario_clp), 0),
            "total_venta_clp": round(_f(it.subtotal_clp), 0),
            "estado_linea": it.estado_linea or "cotizado",
            "guias": guias_por_item.get(it.id, []),
            "facturas": facturas_por_item.get(it.id, []),
        })

    facturas = _facturas_de_cot(db, cot.id)
    return {
        "cotizacion_id": cot.id,
        "numero_cotizacion": cot.numero,
        "cliente": (cli.nombre if cli else "") or "",
        "rut_cliente": (cli.rut if cli else "") or "",
        "oc_cliente": cot.oc_cliente,
        "vehiculo": cot.vehiculo,
        "cond_pago": cot.forma_pago,
        "fecha_entrega_est": cot.fecha_entrega_est.isoformat() if cot.fecha_entrega_est else None,
        "total_neto_clp": round(_f(cot.total_neto), 0),
        "iva_clp": round(_f(cot.iva_monto), 0),
        "total_con_iva_clp": round(_f(cot.total_bruto), 0),
        "items": items_out,
        "facturas": [_serialize_factura(f) for f in facturas],
        "resumen": _resumen_cobranza(facturas),
        **estado_adelanto(cot, _adelanto_de_cot(db, cot.id)),
    }


@router.get("/ventas/{cot_id}/despachos-facturables")
def despachos_facturables(
    cot_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Guías de despacho FACTURABLES: despachos 'despachado' con saldo aún facturable.
    Alimenta el selector del modal 'Emitir factura'. Incluye el estado de firma (opcional)."""
    if not db.query(MonzaCotizacion.id).filter(MonzaCotizacion.id == cot_id).first():
        raise HTTPException(404, "Venta (cotización) no encontrada")
    fact_di = _qty_facturada_por_despacho_item(db, cot_id)
    by_desp = {}
    for di, d in _despacho_items_de_cot(db, cot_id):
        facturable = _f(di.qty_despachada) - fact_di.get(di.id, 0.0)
        e = by_desp.setdefault(d.id, {
            "id": d.id, "numero_despacho": d.numero,
            "numero_guia": d.numero_guia,
            "guia_firmada": bool(getattr(d, "guia_firmada", 0)),
            "guia_firmada_archivo": getattr(d, "guia_firmada_archivo", None),
            "items_count": 0, "facturable": 0.0,
        })
        e["items_count"] += 1
        e["facturable"] += max(facturable, 0.0)
    return [e for e in by_desp.values() if e["facturable"] > TOL_QTY]


@router.patch("/ventas/despachos/{desp_id}/guia-firmada")
def marcar_guia_firmada(
    desp_id: int,
    payload: GuiaFirmadaIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Marca/registra (opcional) que la guía de un despacho fue firmada por el cliente.
    Es informativo: NO es requisito para facturar."""
    desp = db.query(MonzaDespacho).filter(MonzaDespacho.id == desp_id).first()
    if not desp:
        raise HTTPException(404, "Despacho no encontrado")
    # Defensa anti-IDOR: el despacho debe pertenecer a una venta (cotización) real.
    if not desp.cotizacion_id or not db.query(MonzaCotizacion.id).filter(
        MonzaCotizacion.id == desp.cotizacion_id
    ).first():
        raise HTTPException(404, "El despacho no pertenece a una venta válida")
    desp.guia_firmada = 1 if payload.firmada else 0
    if payload.archivo is not None:
        desp.guia_firmada_archivo = payload.archivo or None
    db.commit()
    return {
        "id": desp.id,
        "guia_firmada": bool(desp.guia_firmada),
        "guia_firmada_archivo": desp.guia_firmada_archivo,
    }


@router.post("/ventas/{cot_id}/adelanto/verificar")
def verificar_adelanto(
    cot_id: int,
    payload: AdelantoVerificarIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Contabilidad VERIFICA el adelanto que Comercial informó al cerrar la venta:
    registra monto/fecha/banco/N° operación y marca la venta como adelanto verificado.
    El monto se aplicará como cobranza al emitir la(s) factura(s) de esta venta.
    Permite editar mientras no se haya aplicado a una factura."""
    # Lock de la cotización: serializa verificaciones concurrentes de la misma venta, de
    # modo que un doble envío actualice el mismo adelanto en vez de chocar con el UNIQUE.
    cot = (
        db.query(MonzaCotizacion)
        .filter(MonzaCotizacion.id == cot_id)
        .with_for_update(of=MonzaCotizacion)
        .first()
    )
    if not cot:
        raise HTTPException(404, "Venta (cotización) no encontrada")
    if cot.estado not in ESTADOS_VENTA:
        raise HTTPException(400, "La cotización debe estar vendida para verificar el adelanto")
    if int(getattr(cot, "pct_adelanto", 0) or 0) <= 0:
        raise HTTPException(400, "Esta venta no tiene un adelanto informado por Comercial")
    if payload.monto > _f(cot.total_bruto) + TOL_PAGO:
        raise HTTPException(400, f"El monto del adelanto no puede exceder el total de la venta ({_f(cot.total_bruto):.0f})")
    adel = _adelanto_de_cot(db, cot_id)
    if adel is not None and _f(adel.monto_aplicado) > TOL:
        logger.warning("Re-verificación de adelanto bloqueada: cot=%s monto_aplicado=%s", cot_id, _f(adel.monto_aplicado))
        raise HTTPException(409, f"El adelanto ya fue aplicado a una factura (aplicado {_f(adel.monto_aplicado):.0f}); revierta esa cobranza antes de modificarlo")
    # Misma regla que Tesorería (mantener en sync con monza_tesoreria aprobar_adelanto):
    # si el adelanto ya está conciliado con un abono del banco, editar su monto dejaría
    # el cruce bancario apuntando a otro monto → primero desconciliar allá.
    if adel is not None and db.query(MonzaTesConciliacion).filter(
            MonzaTesConciliacion.adelanto_id == adel.id).first():
        raise HTTPException(409, "El adelanto ya está conciliado con un abono del banco; "
                                 "desconcilie el abono en Tesorería antes de modificar el adelanto")
    if adel is None:
        adel = MonzaContAdelanto(cotizacion_id=cot_id)
        db.add(adel)
    adel.monto = payload.monto
    adel.fecha_pago = _parse_date(payload.fecha_pago) or date.today()
    adel.banco = payload.banco
    adel.numero_operacion = payload.numero_operacion
    adel.observaciones = payload.observaciones
    adel.usuario_id = getattr(current_user, "id", None)
    cot.adelanto_verificado = 1
    db.commit()
    db.refresh(cot)
    db.refresh(adel)
    return estado_adelanto(cot, adel)


# ── Facturas / Cobranzas / Factoring ───────────────────────────────────────────
@router.get("/facturas")
def listar_facturas(
    estado: Optional[str] = None,
    q: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Lista las facturas (filtrables por estado_pago y texto `q`) y la ANTIGÜEDAD de
    cartera: saldo por cobrar en 0-30 / 31-60 / 61-90 / 91+ días desde la emisión."""
    base = db.query(MonzaContFacturaCliente).options(*_FACTURA_EAGER)
    if q:
        like = f"%{q}%"
        base = base.filter(or_(
            MonzaContFacturaCliente.cliente_nombre.ilike(like),
            MonzaContFacturaCliente.rut_cliente.ilike(like),
            MonzaContFacturaCliente.numero_factura.ilike(like),
            MonzaContFacturaCliente.numero_cotizacion.ilike(like),
        ))
    facturas = base.order_by(MonzaContFacturaCliente.id.desc()).all()
    out = []
    aging = {"0_30": 0.0, "31_60": 0.0, "61_90": 0.0, "91_mas": 0.0}
    hoy = date.today()
    for f in facturas:
        d = _serialize_factura(f)
        # filtra por el estado EN VIVO del serializador (no el persistido, que puede
        # estar obsoleto para 'vencida')
        if estado and d["estado_pago"] != estado:
            continue
        saldo = d["saldo"]
        if saldo > TOL and f.fecha_emision:
            dias = (hoy - f.fecha_emision).days
            if dias <= 30:
                aging["0_30"] += saldo
            elif dias <= 60:
                aging["31_60"] += saldo
            elif dias <= 90:
                aging["61_90"] += saldo
            else:
                aging["91_mas"] += saldo
        out.append(d)
    return {"facturas": out, "antiguedad": {k: round(v, 0) for k, v in aging.items()}}


@router.post("/facturas")
def crear_factura(
    payload: FacturaCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """EMITE una factura a cliente. Tres modos: `despacho_id` (deriva líneas de una guía
    'despachado', tope por despachado), `sin_guia=True` (RETIRO EN OFICINA: factura el
    saldo de la cotización sin requerir despacho, tope por lo VENDIDO − ya facturado), o
    `items` explícitos. Reglas: folio único; nunca facturar más de lo permitido por el
    modo; congela montos (neto, IVA, bruto) y snapshots de cliente/guía. El control
    anti-doble-facturación es compartido (fact_qty_item cuenta TODAS las facturas, así
    retiro y guía no se solapan)."""
    # Lock de la cotización: serializa la facturación concurrente de la misma venta.
    cot = (
        db.query(MonzaCotizacion)
        .options(selectinload(MonzaCotizacion.items))  # evita lazy-load de items dentro del lock
        .filter(MonzaCotizacion.id == payload.cotizacion_id)
        .with_for_update(of=MonzaCotizacion)
        .first()
    )
    if not cot:
        raise HTTPException(404, "Cotización (venta) no encontrada")
    if cot.estado not in ESTADOS_VENTA:
        raise HTTPException(400, "La cotización no está vendida; no se puede facturar")

    # Folio único
    if payload.numero_factura:
        dup = db.query(MonzaContFacturaCliente).filter(
            MonzaContFacturaCliente.numero_factura == payload.numero_factura,
        ).first()
        if dup:
            raise HTTPException(409, f"El folio {payload.numero_factura} ya existe")

    # 'Retiro en oficina' (sin_guia) factura el SALDO de la venta: es EXCLUYENTE con
    # despacho e ítems explícitos (evita estados ambiguos / modos mezclados).
    if payload.sin_guia and (payload.despacho_id is not None or payload.items is not None):
        raise HTTPException(400, "Retiro en oficina (sin guía) factura el saldo de la venta: no indique despacho ni ítems")

    items_by_id = {i.id: i for i in cot.items}

    # Despachos 'despachado' de la cotización (lo que se puede facturar)
    desp_items = _despacho_items_de_cot(db, cot.id)
    di_by_id = {di.id: di for di, _d in desp_items}
    desp_qty_item = _qty_despachada_por_item(db, cot.id)
    fact_qty_item = _qty_facturada_por_item(db, cot.id)
    fact_qty_di = _qty_facturada_por_despacho_item(db, cot.id)

    # Determinar líneas a facturar
    lineas: List[FacturaItemIn] = []
    desp = None
    if payload.items:
        lineas = list(payload.items)
    elif payload.sin_guia:
        # Retiro en oficina: derivar del saldo pendiente de la cotización (sin despacho).
        for it in cot.items:
            disp = _f(it.cantidad) - fact_qty_item.get(it.id, 0.0)
            if disp > TOL_QTY:
                lineas.append(FacturaItemIn(item_cotizacion_id=it.id, cantidad=round(disp, 4)))
        if not lineas:
            if not cot.items:
                raise HTTPException(400, "La venta no tiene ítems para facturar")
            raise HTTPException(409, "Esta venta ya fue facturada por completo")
    elif payload.despacho_id:
        desp = db.query(MonzaDespacho).filter(
            MonzaDespacho.id == payload.despacho_id,
            MonzaDespacho.cotizacion_id == cot.id,
        ).first()
        if not desp:
            raise HTTPException(404, "Despacho no encontrado para esta venta")
        if desp.estado != "despachado":
            raise HTTPException(400, "Solo se puede facturar una guía en estado 'despachado'")
        usado_deriv = {}
        # Ítems del despacho elegido
        desp_item_rows = db.query(MonzaDespachoItem).filter(
            MonzaDespachoItem.despacho_id == desp.id
        ).all()
        for di in desp_item_rows:
            disp_di = _f(di.qty_despachada) - fact_qty_di.get(di.id, 0.0)
            disp_item = (
                desp_qty_item.get(di.item_id, 0.0)
                - fact_qty_item.get(di.item_id, 0.0)
                - usado_deriv.get(di.item_id, 0.0)
            )
            disponible = min(disp_di, disp_item)
            if disponible > TOL_QTY:
                lineas.append(FacturaItemIn(
                    item_cotizacion_id=di.item_id,
                    despacho_item_id=di.id,
                    cantidad=round(disponible, 4),
                ))
                usado_deriv[di.item_id] = usado_deriv.get(di.item_id, 0.0) + disponible
        if not lineas:
            raise HTTPException(409, "El despacho ya fue facturado por completo")
    if not lineas:
        raise HTTPException(400, "Debe indicar ítems o un despacho a facturar")

    # Validación por línea con acumuladores (guía + ítem) dentro del request
    usado_di = {}
    usado_item = {}
    validadas = []
    for ln in lineas:
        it = items_by_id.get(ln.item_cotizacion_id)
        if not it:
            raise HTTPException(400, f"Ítem {ln.item_cotizacion_id} no pertenece a esta venta")
        cantidad = ln.cantidad if ln.cantidad is not None else _f(it.cantidad)
        if cantidad <= 0:
            raise HTTPException(400, f"Cantidad inválida para {it.numero_parte or it.descripcion}")

        if payload.sin_guia:
            # RETIRO EN OFICINA: tope por lo VENDIDO − ya facturado (no requiere despacho ni guía).
            disponible = (
                _f(it.cantidad)
                - fact_qty_item.get(ln.item_cotizacion_id, 0.0)
                - usado_item.get(ln.item_cotizacion_id, 0.0)
            )
            if cantidad > disponible + TOL_QTY:
                raise HTTPException(409, f"{it.numero_parte or it.descripcion}: cantidad excede lo vendido/no facturado (disp {max(disponible,0):.0f})")
        else:
            # FLUJO CON GUÍA: tope por lo DESPACHADO − ya facturado, y por GUÍA si aplica.
            despachado_item = desp_qty_item.get(ln.item_cotizacion_id, 0.0)
            if despachado_item <= 0:
                raise HTTPException(400, f"{it.numero_parte or it.descripcion} no ha sido despachado; no se puede facturar")
            disponible = (
                despachado_item
                - fact_qty_item.get(ln.item_cotizacion_id, 0.0)
                - usado_item.get(ln.item_cotizacion_id, 0.0)
            )
            if ln.despacho_item_id is not None:
                di = di_by_id.get(ln.despacho_item_id)
                if not di or di.item_id != ln.item_cotizacion_id:
                    raise HTTPException(400, f"Guía/despacho inválido para {it.numero_parte or it.descripcion}")
                disp_di = _f(di.qty_despachada) - fact_qty_di.get(di.id, 0.0) - usado_di.get(di.id, 0.0)
                disponible = min(disponible, disp_di)
            if cantidad > disponible + TOL_QTY:
                raise HTTPException(409, f"{it.numero_parte or it.descripcion}: cantidad excede lo despachado/no facturado (disp {max(disponible,0):.0f})")
            if ln.despacho_item_id is not None:
                usado_di[ln.despacho_item_id] = usado_di.get(ln.despacho_item_id, 0.0) + cantidad
        usado_item[ln.item_cotizacion_id] = usado_item.get(ln.item_cotizacion_id, 0.0) + cantidad

        precio = ln.precio_unit_neto if ln.precio_unit_neto is not None else _f(it.precio_unitario_clp)
        if precio < 0:
            raise HTTPException(400, f"Precio inválido para {it.numero_parte or it.descripcion}")
        validadas.append((it, ln, cantidad, precio))

    cfg = _config(db)
    iva_rate = iva_rate_de(cot, cfg)
    fecha_emision = _parse_date(payload.fecha_emision) or date.today()
    # `is not None`: plazo 0 días (contado) también debe generar vencimiento (= emisión)
    fecha_venc = (fecha_emision + timedelta(days=int(payload.plazo_dias))
                  if payload.plazo_dias is not None else None)
    cli = cot.cliente
    # Trazabilidad: marca el retiro en oficina si el usuario no puso observación propia.
    observaciones = payload.observaciones or ("Retiro en oficina (sin guía)" if payload.sin_guia else None)

    # Snapshot de la guía: en modo despacho es directo; en modo 'items' se deriva si
    # TODAS las líneas provienen de un único despacho (queda trazable en la factura).
    snap_desp_id = desp.id if desp else None
    snap_guia = desp.numero_guia if desp else None
    if desp is None:
        desp_ids = {
            di_by_id[ln.despacho_item_id].despacho_id
            for _it, ln, _c, _p in validadas
            if ln.despacho_item_id is not None and ln.despacho_item_id in di_by_id
        }
        if len(desp_ids) == 1:
            _d = db.query(MonzaDespacho).filter(MonzaDespacho.id == next(iter(desp_ids))).first()
            if _d:
                snap_desp_id = _d.id
                snap_guia = _d.numero_guia

    factura = MonzaContFacturaCliente(
        cotizacion_id=cot.id,
        numero_cotizacion=cot.numero,
        cliente_nombre=(cli.nombre if cli else None),
        rut_cliente=(cli.rut if cli else None),
        despacho_id=snap_desp_id,
        numero_guia=snap_guia,
        numero_factura=payload.numero_factura,
        tipo_doc=payload.tipo_doc or "factura",
        fecha_emision=fecha_emision, condicion_pago=payload.condicion_pago,
        plazo_dias=payload.plazo_dias, fecha_vencimiento=fecha_venc,
        observaciones=observaciones, usuario_id=getattr(current_user, "id", None),
    )
    try:
        db.add(factura)
        db.flush()
        neto = 0.0
        for it, ln, cantidad, precio in validadas:
            total = round(precio * cantidad, 0)
            neto += total
            db.add(MonzaContFacturaClienteItem(
                factura_id=factura.id, item_cotizacion_id=ln.item_cotizacion_id,
                despacho_item_id=ln.despacho_item_id,
                numero_parte=it.numero_parte, descripcion=it.descripcion,
                cantidad=cantidad, precio_unit_neto=round(precio, 2), total_neto=total,
            ))
        iva = round(neto * iva_rate, 0)
        factura.monto_neto = neto
        factura.iva = iva
        factura.monto_bruto = neto + iva
        db.flush()
        # Adelanto verificado de la venta → se aplica como cobranza en esta factura.
        _aplicar_adelanto(db, cot, factura, getattr(current_user, "id", None))
        db.flush()
        _recompute_factura(factura)
        db.commit()
    except IntegrityError as e:
        db.rollback()
        orig = str(getattr(e, "orig", e))
        if "uq_monza_cont_factura_folio" in orig:
            raise HTTPException(409, "Folio de factura duplicado")
        logger.error("IntegrityError al crear factura Monza: %s", orig)
        raise HTTPException(409, "No se pudo guardar la factura (conflicto de integridad)")
    db.refresh(factura)
    return _serialize_factura(factura)


@router.post("/facturas/{factura_id}/cobranzas")
def registrar_cobranza(
    factura_id: int,
    payload: CobranzaIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Registra un pago real del cliente. Bloquea la factura, rechaza medios de
    factoring y el SOBRE-PAGO (recalcula el saldo desde las cobranzas reales). Si hay
    factoring vigente, exige liquidarlo antes."""
    factura = (
        db.query(MonzaContFacturaCliente)
        .options(selectinload(MonzaContFacturaCliente.cobranzas), selectinload(MonzaContFacturaCliente.factoring))
        .filter(MonzaContFacturaCliente.id == factura_id)
        .with_for_update(of=MonzaContFacturaCliente)
        .first()
    )
    if not factura:
        raise HTTPException(404, "Factura no encontrada")
    if payload.monto <= 0:
        raise HTTPException(400, "El monto debe ser mayor a 0")
    if _es_medio_factoring(payload.medio):
        raise HTTPException(400, "Las cobranzas de factoring se gestionan desde el panel de factoring")
    if factura.factoring and factura.factoring.estado == "vigente":
        raise HTTPException(409, "La factura tiene un factoring vigente; liquídelo antes de registrar cobranzas")
    pagado_actual = sum(_f(c.monto) for c in factura.cobranzas)
    saldo_actual = round(_f(factura.monto_bruto) - pagado_actual, 2)
    if payload.monto > saldo_actual + TOL_PAGO:
        raise HTTPException(400, f"El monto excede el saldo pendiente ({max(saldo_actual, 0):.0f})")
    db.add(MonzaContCobranza(
        factura_id=factura.id, fecha=_parse_date(payload.fecha) or date.today(),
        monto=payload.monto, medio=payload.medio or "transferencia",
        banco=payload.banco, numero_operacion=payload.numero_operacion,
        observaciones=payload.observaciones, usuario_id=getattr(current_user, "id", None),
    ))
    db.flush()
    db.refresh(factura)
    _recompute_factura(factura)
    db.commit()
    db.refresh(factura)
    return _serialize_factura(factura)


@router.delete("/facturas/{factura_id}/cobranzas/{cobranza_id}")
def eliminar_cobranza(
    factura_id: int,
    cobranza_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Revierte un pago real (no de factoring) y recalcula saldo/estado."""
    # Lock de la factura PRIMERO: serializa la reversión concurrente de cobranzas
    # (mismo patrón que registrar_cobranza) y evita race en el recálculo del saldo.
    factura = (
        db.query(MonzaContFacturaCliente)
        .filter(MonzaContFacturaCliente.id == factura_id)
        .with_for_update(of=MonzaContFacturaCliente)
        .first()
    )
    if not factura:
        raise HTTPException(404, "Factura no encontrada")
    c = db.query(MonzaContCobranza).filter(
        MonzaContCobranza.id == cobranza_id, MonzaContCobranza.factura_id == factura_id
    ).first()
    if not c:
        raise HTTPException(404, "Cobranza no encontrada")
    if _es_medio_factoring(c.medio):
        raise HTTPException(400, "Las cobranzas de factoring se revierten desde el panel de factoring")
    conciliada = (db.query(MonzaTesConciliacionIngreso)
                  .filter(MonzaTesConciliacionIngreso.cobranza_id == c.id).first())
    if conciliada:
        raise HTTPException(409, "La cobranza está conciliada con el banco; desconcíliela en Tesorería primero")
    # Si la cobranza es la aplicación de un adelanto, devolver el monto a monto_aplicado
    # para mantener la invariante (permite re-aplicarlo a otra factura).
    if c.medio == MEDIO_ADELANTO and factura.cotizacion_id:
        adel = _adelanto_de_cot(db, factura.cotizacion_id)
        if adel is not None:
            adel.monto_aplicado = round(max(_f(adel.monto_aplicado) - _f(c.monto), 0.0), 2)
    db.delete(c)
    db.flush()
    # Re-leer cobranzas frescas (post-borrado) sobre la factura ya bloqueada.
    db.refresh(factura)
    _recompute_factura(factura)
    db.commit()
    return {"ok": True}


@router.post("/facturas/{factura_id}/factoring")
def set_factoring(
    factura_id: int,
    payload: FactoringIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Crea o actualiza el factoring (1 por factura). Valida adelanto <= cupo (bruto -
    pagos reales), deriva la retención si falta, y genera SOLO la cobranza de ADELANTO.
    No editable si ya está liquidado."""
    factura = (
        db.query(MonzaContFacturaCliente)
        .options(selectinload(MonzaContFacturaCliente.cobranzas), selectinload(MonzaContFacturaCliente.factoring))
        .filter(MonzaContFacturaCliente.id == factura_id)
        .with_for_update(of=MonzaContFacturaCliente)
        .first()
    )
    if not factura:
        raise HTTPException(404, "Factura no encontrada")
    fac = factura.factoring
    if fac and fac.estado == "liquidada":
        raise HTTPException(400, "El factoring ya fue liquidado; no se puede modificar")

    bruto = _f(factura.monto_bruto)
    pagado_no_fact = sum(_f(c.monto) for c in factura.cobranzas if not _es_medio_factoring(c.medio))
    if payload.monto_adelantado < 0:
        raise HTTPException(400, "El adelanto no puede ser negativo")
    cupo = bruto - pagado_no_fact
    if payload.monto_adelantado > cupo + TOL_PAGO:
        raise HTTPException(400, f"El adelanto excede el saldo financiable ({cupo:.0f})")
    retencion = payload.retencion
    if retencion is None:
        retencion = round(max(cupo - payload.monto_adelantado, 0), 0)

    if not fac:
        fac = MonzaContFactoring(factura_id=factura.id)
        db.add(fac)
    fac.usuario_id = getattr(current_user, "id", None)
    fac.empresa_factoring = payload.empresa_factoring
    fac.id_operacion = payload.id_operacion
    fac.fecha_operacion = _parse_date(payload.fecha_operacion) or date.today()
    fac.monto_adelantado = payload.monto_adelantado
    fac.costo_factoring = payload.costo_factoring
    fac.retencion = retencion
    fac.banco = payload.banco
    fac.observaciones = payload.observaciones
    fac.estado = "vigente"
    fac.fecha_liquidacion = None

    # Reemplazar solo la cobranza de ADELANTO (nunca la de retención liquidada).
    # Si esa cobranza ya está conciliada con un abono del banco en Tesorería, se
    # rechaza: borrarla dejaría el movimiento bancario conciliado sin destino.
    for c in list(factura.cobranzas):
        if c.medio == MEDIO_FACT_ADELANTO:
            conciliada = (db.query(MonzaTesConciliacionIngreso)
                          .filter(MonzaTesConciliacionIngreso.cobranza_id == c.id).first())
            if conciliada:
                raise HTTPException(409, "El adelanto del factoring está conciliado con el banco; desconcílielo en Tesorería antes de modificar el factoring")
            db.delete(c)
    db.flush()
    if payload.monto_adelantado > 0:
        db.add(MonzaContCobranza(
            factura_id=factura.id, fecha=fac.fecha_operacion, monto=payload.monto_adelantado,
            medio=MEDIO_FACT_ADELANTO, banco=payload.banco, numero_operacion=payload.id_operacion,
            observaciones=f"Adelanto factoring {payload.empresa_factoring or ''}".strip(),
            usuario_id=getattr(current_user, "id", None),
        ))
    db.flush()
    db.refresh(factura)
    _recompute_factura(factura)
    db.commit()
    db.refresh(factura)
    return _serialize_factura(factura)


@router.post("/facturas/{factura_id}/factoring/liquidar")
def liquidar_factoring(
    factura_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Liquida el factoring vigente: libera el saldo pendiente REAL como retención,
    cerrando la factura en saldo 0, y marca estado 'liquidada'."""
    factura = (
        db.query(MonzaContFacturaCliente)
        .options(selectinload(MonzaContFacturaCliente.cobranzas), selectinload(MonzaContFacturaCliente.factoring))
        .filter(MonzaContFacturaCliente.id == factura_id)
        .with_for_update(of=MonzaContFacturaCliente)
        .first()
    )
    if not factura:
        raise HTTPException(404, "Factura no encontrada")
    fac = factura.factoring
    if not fac or fac.estado != "vigente":
        raise HTTPException(400, "No hay factoring vigente para liquidar")
    pagado_actual = sum(_f(c.monto) for c in factura.cobranzas)
    liberar = round(max(_f(factura.monto_bruto) - pagado_actual, 0.0), 2)
    fac.retencion = liberar
    if liberar > TOL:
        db.add(MonzaContCobranza(
            factura_id=factura.id, fecha=date.today(), monto=liberar,
            medio=MEDIO_FACT_RETENCION, banco=fac.banco, numero_operacion=fac.id_operacion,
            observaciones="Liquidación retención factoring", usuario_id=getattr(current_user, "id", None),
        ))
    fac.estado = "liquidada"
    fac.fecha_liquidacion = date.today()
    fac.usuario_liquidacion_id = getattr(current_user, "id", None)
    db.flush()
    db.refresh(factura)
    _recompute_factura(factura)
    db.commit()
    db.refresh(factura)
    return _serialize_factura(factura)


@router.delete("/facturas/{factura_id}")
def eliminar_factura(
    factura_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Borrado SEGURO: se rechaza (409) si tiene factoring o cobranzas reales — primero
    hay que revertir esos pagos. El cascade borra solo las líneas."""
    factura = (
        db.query(MonzaContFacturaCliente)
        .options(selectinload(MonzaContFacturaCliente.cobranzas), selectinload(MonzaContFacturaCliente.factoring))
        .filter(MonzaContFacturaCliente.id == factura_id)
        .with_for_update(of=MonzaContFacturaCliente)
        .first()
    )
    if not factura:
        raise HTTPException(404, "Factura no encontrada")
    if factura.factoring:
        raise HTTPException(409, "La factura tiene una operación de factoring; no se puede eliminar")
    if any(not _es_medio_factoring(c.medio) for c in factura.cobranzas):
        raise HTTPException(409, "Revierta las cobranzas antes de eliminar la factura")
    db.delete(factura)
    db.commit()
    return {"ok": True}


@router.get("/kpis")
def get_kpis(
    periodo: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Indicadores de cobranza (filtrables por `periodo`: semana|mes|anio sobre fecha
    de emisión)."""
    base = db.query(MonzaContFacturaCliente).options(*_FACTURA_EAGER)
    floor = periodo_floor(periodo)
    if floor is not None:
        # Pre-filtro grueso en SQL (reduce filas en RAM); _periodo_filter refina abajo.
        base = base.filter(or_(
            MonzaContFacturaCliente.fecha_emision.is_(None),
            MonzaContFacturaCliente.fecha_emision >= floor,
        ))
    facturas = [
        f for f in base.all()
        if _periodo_filter(f.fecha_emision or f.created_at, periodo)
    ]
    hoy = date.today()
    facturado = sum(_f(f.monto_bruto) for f in facturas)
    cobrado = sum(_f(f.monto_pagado) for f in facturas)
    cobrado_cliente = sum(_f(c.monto) for f in facturas for c in f.cobranzas
                          if not _es_medio_factoring(c.medio))
    por_cobrar = sum(_f(f.saldo) for f in facturas if _f(f.saldo) > TOL)
    vencido = sum(_f(f.saldo) for f in facturas
                  if _f(f.saldo) > TOL and f.fecha_vencimiento and f.fecha_vencimiento < hoy
                  and not (f.factoring and f.factoring.estado == "vigente"))
    en_factoring = sum(_f(f.monto_bruto) for f in facturas
                       if f.factoring and f.factoring.estado == "vigente")
    anticipo_factoring = sum(_f(c.monto) for f in facturas for c in f.cobranzas
                             if _es_medio_factoring(c.medio))
    return {
        "n_facturas": len(facturas),
        "facturado_clp": round(facturado, 0),
        "cobrado_clp": round(cobrado, 0),
        "cobrado_cliente_clp": round(cobrado_cliente, 0),
        "anticipo_factoring_clp": round(anticipo_factoring, 0),
        "por_cobrar_clp": round(por_cobrar, 0),
        "vencido_clp": round(vencido, 0),
        "en_factoring_clp": round(en_factoring, 0),
    }
