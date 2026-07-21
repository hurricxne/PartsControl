"""API del módulo Embarques Pricing (Contabilidad → costo landed).

Integración NO invasiva con Logística: lee los embarques que crea Compras
(tabla `embarques`) y superpone su propio pricing. Por eso TODO embarque creado
por Logística "aparece" automáticamente acá; el registro de pricing se crea de
forma diferida la primera vez que Contabilidad lo abre.

Prefijo: /embarques-pricing  (montado en main.py con prefix=/api → /api/embarques-pricing)
"""
from datetime import datetime
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel

from database import get_db
from auth import get_current_user
from empresa_guard import require_empresa
from models.models import (
    User, Embarque, EmbarqueItem, ItemCotizacion, OcProveedor, OcProveedorItem,
    FacturaProveedor, FacturaProveedorItem, ConfiguracionCotizador,
)
from .models import EmbarquePricing, EmbarquePricingGasto, EmbarquePricingItem
from .service import (
    calcular_landed, total_gastos_que_capitalizan, GASTOS_CATALOGO, IVA_RATE, _f,
)
# Creación / seed del pricing: lógica compartida (también la usa Logística para
# auto-crear el pricing al embarcar). Fuente única en integration.py.
from .integration import (
    detect_tipo as _detect_tipo,
    get_cfg as _cfg,
    ensure_pricing_for_embarque,
)

router = APIRouter(
    prefix="/embarques-pricing",
    tags=["embarques-pricing"],
    dependencies=[Depends(require_empresa("mineria"))],  # candado: solo Grupo AM
)

ESTADO_BLOQUEADO = "cerrado"


def _get_or_create_pricing(db: Session, embarque: Embarque) -> EmbarquePricing:
    """Crea (si falta) o devuelve el pricing del embarque. Delega en integration.

    Nunca devuelve None hacia los endpoints: si la creación falla de forma
    irrecuperable (no por una carrera concurrente, que sí se recupera), corta con
    500 en vez de propagar un AttributeError en _compute_detail.
    """
    pricing = ensure_pricing_for_embarque(db, embarque, commit=True)
    if pricing is None:
        raise HTTPException(500, "No se pudo crear el registro de pricing del embarque")
    return pricing


def _load_embarque(db: Session, embarque_id: int):
    """Carga el embarque con sus ítems y la cotización de cada ítem en pocas
    queries (evita N+1 al recorrer embarque.items / ei.item_cotizacion)."""
    return (
        db.query(Embarque)
        .options(selectinload(Embarque.items).selectinload(EmbarqueItem.item_cotizacion))
        .filter(Embarque.id == embarque_id)
        .first()
    )


# ─── FOB por ítem: factura proveedor → cotización → 0 ─────────────────────────
def _fob_defaults(db: Session, embarque: Embarque) -> dict:
    """Mapea (item_cotizacion_id, oc_proveedor_id) → (fob_unit_default, origen).

    Se keyea por par (ítem, OC proveedor) para no tomar el precio de OTRA orden
    cuando el mismo ítem se re-compró en distintas OCs con precios distintos.
    Prioridad: precio de la factura del proveedor (FOB real) → precio de la
    cotización (estimado) → 0.
    """
    item_ids = [ei.item_cotizacion_id for ei in embarque.items if ei.item_cotizacion_id]
    if not item_ids:
        return {}

    # OcProveedorItem → id, indexado por par (ítem, OC) y con fallback por ítem.
    ocp_items = (
        db.query(OcProveedorItem)
        .filter(OcProveedorItem.item_cotizacion_id.in_(item_ids))
        .order_by(OcProveedorItem.id.asc())
        .all()
    )
    ocp_item_by_pair: dict = {}
    ocp_item_first_by_item: dict = {}
    for oi in ocp_items:
        ocp_item_by_pair.setdefault((oi.item_cotizacion_id, oi.oc_proveedor_id), oi.id)
        ocp_item_first_by_item.setdefault(oi.item_cotizacion_id, oi.id)

    # FacturaProveedorItem por ocp_item_id → unit_price_usd (la más reciente gana)
    fob_by_ocp_item: dict = {}
    ocp_item_ids = [oi.id for oi in ocp_items]
    if ocp_item_ids:
        fpis = (
            db.query(FacturaProveedorItem)
            .filter(FacturaProveedorItem.ocp_item_id.in_(ocp_item_ids))
            .order_by(FacturaProveedorItem.id.asc())
            .all()
        )
        for fpi in fpis:
            if fpi.unit_price_usd is not None:
                fob_by_ocp_item[fpi.ocp_item_id] = _f(fpi.unit_price_usd)

    out: dict = {}
    for ei in embarque.items:
        icid = ei.item_cotizacion_id
        if icid is None:
            continue
        # OcProveedorItem del MISMO embarque (par ítem+OC); fallback al 1º del ítem.
        ocp_item_id = ocp_item_by_pair.get((icid, ei.oc_proveedor_id)) or ocp_item_first_by_item.get(icid)
        if ocp_item_id and ocp_item_id in fob_by_ocp_item:
            out[(icid, ei.oc_proveedor_id)] = (fob_by_ocp_item[ocp_item_id], "factura")
            continue
        # Fallback: precio de la cotización (ítem ya cargado vía relación, sin N+1)
        item = ei.item_cotizacion
        if item and item.precio_unit_cotizacion:
            out[(icid, ei.oc_proveedor_id)] = (_f(item.precio_unit_cotizacion), "cotizacion")
        else:
            out[(icid, ei.oc_proveedor_id)] = (0.0, "manual")
    return out


# ─── Construcción de inputs y cómputo del detalle ─────────────────────────────
def _build_inputs(db: Session, embarque: Embarque, pricing: EmbarquePricing) -> List[dict]:
    """Arma los inputs por ítem mezclando defaults + overrides guardados."""
    fob_def = _fob_defaults(db, embarque)
    stored = {
        si.embarque_item_id: si
        for si in db.query(EmbarquePricingItem)
        .filter(EmbarquePricingItem.pricing_id == pricing.id)
        .all()
    }
    tc_header = _f(pricing.tc_valor)
    inputs: List[dict] = []
    for ei in embarque.items:
        item = ei.item_cotizacion
        icid = ei.item_cotizacion_id
        default_fob, default_origen = fob_def.get((icid, ei.oc_proveedor_id), (0.0, "manual"))
        s = stored.get(ei.id)

        # Override manual solo si trae un valor > 0: un "manual" en 0 (ítem que
        # nunca tuvo precio, o 0 explícito) no es un precio real y NO debe
        # bloquear el FOB de la factura del proveedor que llega después.
        if s is not None and s.fob_origen == "manual" and _f(s.fob_unit) > 0:
            fob_unit, origen = _f(s.fob_unit), "manual"
        else:
            fob_unit, origen = default_fob, default_origen

        # Peso: default de la cotización; override manual solo si trae valor > 0.
        # Espejo del FOB: un "manual" en 0 no es un peso real (una pieza física
        # pesa > 0) y NO debe pisar el peso de la cotización.
        default_peso = _f(item.peso_unit_lbs) if item else 0.0
        if s is not None and (s.peso_origen or "auto") == "manual" and _f(s.peso_unit_lbs) > 0:
            peso_unit, peso_origen = _f(s.peso_unit_lbs), "manual"
        else:
            peso_unit, peso_origen = default_peso, "auto"

        # TC del encabezado para todos los ítems. El TC por orden (FastMark
        # multi-OC) es una mejora futura: hoy un embarque usa un TC único, que
        # es el caso de Normal/Courier/Baukat. Así un cambio de TC se propaga
        # siempre (no queda "pegado" en el snapshot del ítem).
        tc_item = tc_header

        inputs.append({
            "embarque_item_id": ei.id,
            "item_cotizacion_id": icid,
            "numero_parte": (item.numero_parte if item else None) or "",
            "descripcion": (item.descripcion if item else None) or "",
            "moneda": pricing.moneda,
            "cantidad": _f(item.cantidad) if item else 0.0,
            "peso_unit_lbs": peso_unit,
            "peso_default": default_peso,
            "peso_origen": peso_origen,
            "fob_unit": fob_unit,
            "fob_default": default_fob,
            "fob_origen": origen,
            "tc_valor": tc_item,
        })
    return inputs


def _shipping_total_clp(pricing: EmbarquePricing) -> float:
    """Flete total en CLP: ME × TC si viene en moneda extranjera, o el CLP directo."""
    if pricing.flete_en_me:
        return _f(pricing.shipping_me) * _f(pricing.tc_valor)
    return _f(pricing.shipping_clp)


def _serialize_gasto(g: EmbarquePricingGasto) -> dict:
    neto, iva = _f(g.monto_neto), _f(g.iva)
    return {
        "id": g.id, "tipo": g.tipo, "glosa": g.glosa,
        "monto_neto": neto, "iva": iva, "total_bruto": neto + iva,
        "capitaliza": bool(g.capitaliza), "nro_factura": g.nro_factura,
        "fecha_factura": g.fecha_factura, "banco": g.banco, "orden": g.orden,
    }


def _snapshot_items(db: Session, pricing: EmbarquePricing) -> List[dict]:
    """Filas del snapshot persistido (pricing cerrado → costo CONGELADO).

    Devuelve las filas guardadas al cerrar, con la misma forma que el recálculo
    en vivo, para que el detalle cuadre siempre con el listado.
    """
    snap = (
        db.query(EmbarquePricingItem)
        .filter(EmbarquePricingItem.pricing_id == pricing.id)
        .order_by(EmbarquePricingItem.id.asc())
        .all()
    )
    return [{
        "embarque_item_id": s.embarque_item_id,
        "item_cotizacion_id": s.item_cotizacion_id,
        "numero_parte": s.numero_parte or "",
        "descripcion": s.descripcion or "",
        "moneda": s.moneda,
        "cantidad": _f(s.cantidad),
        "peso_unit_lbs": _f(s.peso_unit_lbs),
        # Cerrado no se edita: el default mostrado es el mismo peso congelado.
        "peso_default": _f(s.peso_unit_lbs),
        "peso_origen": s.peso_origen or "auto",
        "peso_total_lbs": round(_f(s.peso_total_lbs), 2),
        "fob_unit": _f(s.fob_unit),
        # Cerrado no se edita: el default mostrado es el mismo valor congelado.
        "fob_default": _f(s.fob_unit),
        "fob_origen": s.fob_origen,
        "tc_valor": _f(s.tc_valor),
        "fob_total": round(_f(s.fob_total), 2),
        "fob_clp": round(_f(s.fob_clp), 0),
        "shipping_clp": round(_f(s.shipping_clp), 0),
        "cif_clp": round(_f(s.cif_clp), 0),
        "gastos_clp": round(_f(s.gastos_clp), 0),
        "costo_total_clp": round(_f(s.costo_total_clp), 0),
        "costo_unit_clp": round(_f(s.costo_unit_clp), 0),
    } for s in snap]


def _compute_detail(db: Session, embarque: Embarque, pricing: EmbarquePricing) -> dict:
    cfg = _cfg(db)
    gastos = sorted(pricing.gastos, key=lambda g: (g.orden or 0, g.id))
    gastos_dicts = [_serialize_gasto(g) for g in gastos]
    total_cap = total_gastos_que_capitalizan(
        [{"monto_neto": g["monto_neto"], "capitaliza": g["capitaliza"]} for g in gastos_dicts]
    )
    total_iva = sum(g["iva"] for g in gastos_dicts if g["capitaliza"])
    iva_importacion = sum(g["monto_neto"] for g in gastos_dicts if g["tipo"] == "iva_importacion")

    shipping_total = _shipping_total_clp(pricing)

    # Cerrado → el detalle sale del snapshot persistido al cerrar (costo
    # CONGELADO, igual que el listado). Abierto → recálculo en vivo.
    if pricing.estado == ESTADO_BLOQUEADO:
        items_out = _snapshot_items(db, pricing)
    else:
        inputs = _build_inputs(db, embarque, pricing)
        calc_rows, _ = calcular_landed(inputs, shipping_total, total_cap)

        items_out = []
        for r in calc_rows:
            items_out.append({
                "embarque_item_id": r["embarque_item_id"],
                "item_cotizacion_id": r["item_cotizacion_id"],
                "numero_parte": r["numero_parte"],
                "descripcion": r["descripcion"],
                "moneda": r["moneda"],
                "cantidad": r["cantidad"],
                "peso_unit_lbs": r["peso_unit_lbs"],
                "peso_default": r.get("peso_default", 0.0),
                "peso_origen": r.get("peso_origen", "auto"),
                "peso_total_lbs": round(r["peso_total_lbs"], 2),
                "fob_unit": r["fob_unit"],
                "fob_default": r.get("fob_default", 0.0),
                "fob_origen": r["fob_origen"],
                "tc_valor": r["tc_valor"],
                "fob_total": round(r["fob_total"], 2),
                "fob_clp": round(r["fob_clp"], 0),
                "shipping_clp": round(r["shipping_clp"], 0),
                "cif_clp": round(r["cif_clp"], 0),
                "gastos_clp": round(r["gastos_clp"], 0),
                "costo_total_clp": round(r["costo_total_clp"], 0),
                "costo_unit_clp": round(r["costo_unit_clp"], 0),
            })

    return {
        "embarque": {
            "id": embarque.id, "numero": embarque.numero, "estado": embarque.estado,
            "forwarder": embarque.forwarder, "awb": embarque.awb,
            "awb_numero": embarque.awb_numero,
            "fecha_despacho": embarque.fecha_despacho,
            "fecha_llegada_est": embarque.fecha_llegada_est.isoformat() if embarque.fecha_llegada_est else None,
            "n_items": len(embarque.items),
            # Documentos del embarque (Logística los sube o no) → trazabilidad.
            "documentos": {
                "awb": embarque.awb,
                "factura_comercial": embarque.factura_comercial,
                "packing_list": embarque.packing_list,
                "certificado_origen": embarque.certificado_origen,
                "doc_adicional": embarque.doc_adicional,
            },
        },
        "pricing": {
            "id": pricing.id,
            # Correlativo de pricing partiendo desde 1 (== id autoincrement).
            "correlativo": pricing.id,
            "tipo_embarque": pricing.tipo_embarque,
            "tc_tipo": pricing.tc_tipo, "tc_valor": _f(pricing.tc_valor),
            # TC sugerido de Config: SOLO aplica a USD (la Config no tiene TC EUR;
            # sugerir el TC USD en un embarque EUR induciría a error)
            "tc_config": (_f(getattr(cfg, "tipo_cambio_usd", 0))
                          if cfg and (pricing.moneda or "USD") == "USD" else 0.0),
            "moneda": pricing.moneda, "flete_en_me": bool(pricing.flete_en_me),
            "shipping_me": _f(pricing.shipping_me), "shipping_clp": _f(pricing.shipping_clp),
            "shipping_total_clp": round(shipping_total, 0),
            "estado": pricing.estado, "observaciones": pricing.observaciones,
            "calculado_at": pricing.calculado_at.isoformat() if pricing.calculado_at else None,
        },
        "gastos": gastos_dicts,
        "totales_gastos": {
            "total_capitaliza": round(total_cap, 0),
            "total_iva": round(total_iva, 0),
            "iva_importacion": round(iva_importacion, 0),
        },
        "items": items_out,
        # Totales sumando los valores YA redondeados de cada fila → el pie cuadra
        # columna por columna con lo que ve el contador.
        "totales": {
            "n_items": len(items_out),
            "peso_total_lbs": round(sum(r["peso_total_lbs"] for r in items_out), 2),
            "fob_total_me": round(sum(r["fob_total"] for r in items_out), 2),
            "fob_clp": sum(r["fob_clp"] for r in items_out),
            "shipping_clp": sum(r["shipping_clp"] for r in items_out),
            "cif_clp": sum(r["cif_clp"] for r in items_out),
            "gastos_clp": sum(r["gastos_clp"] for r in items_out),
            "costo_total_clp": sum(r["costo_total_clp"] for r in items_out),
        },
    }


def _persist_snapshot(db: Session, pricing: EmbarquePricing, detail: dict) -> None:
    """Guarda el snapshot por ítem (inputs + computados) para congelar el costo."""
    db.query(EmbarquePricingItem).filter(
        EmbarquePricingItem.pricing_id == pricing.id
    ).delete()
    for r in detail["items"]:
        db.add(EmbarquePricingItem(
            pricing_id=pricing.id,
            embarque_item_id=r["embarque_item_id"],
            item_cotizacion_id=r["item_cotizacion_id"],
            numero_parte=r["numero_parte"], descripcion=r["descripcion"], moneda=r["moneda"],
            cantidad=r["cantidad"], peso_unit_lbs=r["peso_unit_lbs"],
            peso_total_lbs=r["peso_total_lbs"], peso_origen=r.get("peso_origen", "auto"),
            fob_unit=r["fob_unit"],
            fob_origen=r["fob_origen"], tc_valor=r["tc_valor"],
            fob_total=r["fob_total"], fob_clp=r["fob_clp"], shipping_clp=r["shipping_clp"],
            cif_clp=r["cif_clp"], gastos_clp=r["gastos_clp"],
            costo_total_clp=r["costo_total_clp"], costo_unit_clp=r["costo_unit_clp"],
        ))


# ─── Endpoints ────────────────────────────────────────────────────────────────
@router.get("")
def listar_embarques_pricing(
    q: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Lista TODOS los embarques de Logística con el estado de su pricing."""
    # selectinload evita el N+1 al leer len(e.items) por cada embarque.
    embarques = (
        db.query(Embarque)
        .options(selectinload(Embarque.items))
        .order_by(Embarque.id.desc())
        .all()
    )
    pricings = {p.embarque_id: p for p in db.query(EmbarquePricing).all()}
    # Costo total por pricing: suma agrupada en SQL (no carga toda la tabla).
    costos: dict = {
        pid: _f(total)
        for pid, total in db.query(
            EmbarquePricingItem.pricing_id, func.sum(EmbarquePricingItem.costo_total_clp)
        ).group_by(EmbarquePricingItem.pricing_id).all()
    }

    out = []
    for e in embarques:
        if q:
            ql = q.lower()
            hay = " ".join([e.numero or "", e.forwarder or "", e.awb or "", e.awb_numero or ""]).lower()
            if ql not in hay:
                continue
        p = pricings.get(e.id)
        docs = [e.awb, e.factura_comercial, e.packing_list, e.certificado_origen, e.doc_adicional]
        out.append({
            "embarque_id": e.id,
            "correlativo": p.id if p else None,
            "numero": e.numero,
            "estado_logistica": e.estado,
            "forwarder": e.forwarder,
            "awb": e.awb,
            "awb_numero": e.awb_numero,
            "fecha_despacho": e.fecha_despacho,
            "n_items": len(e.items),
            "docs_count": sum(1 for d in docs if d),
            "tipo_embarque": p.tipo_embarque if p else _detect_tipo(e.forwarder, "USD"),
            "pricing_estado": p.estado if p else "sin_pricing",
            "moneda": p.moneda if p else None,
            "tc_valor": _f(p.tc_valor) if p else None,
            "costo_total_clp": round(costos.get(p.id, 0.0), 0) if p else None,
        })
    return out


@router.get("/{embarque_id}")
def detalle_embarque_pricing(
    embarque_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    embarque = _load_embarque(db, embarque_id)
    if not embarque:
        raise HTTPException(404, "Embarque no encontrado")
    pricing = _get_or_create_pricing(db, embarque)
    return _compute_detail(db, embarque, pricing)


# ── Payloads de guardado ──
GastoTipo = Literal["desconsolidacion", "almacenaje", "agencia", "arancel", "otros", "iva_importacion"]
TipoEmbarque = Literal["normal", "courier", "baukat", "fastmark"]


class GastoIn(BaseModel):
    tipo: GastoTipo
    glosa: Optional[str] = None
    monto_neto: float = 0
    iva: float = 0
    capitaliza: bool = True
    nro_factura: Optional[str] = None
    fecha_factura: Optional[str] = None
    banco: Optional[str] = None
    orden: int = 0


class ItemOverrideIn(BaseModel):
    embarque_item_id: int
    # Tri-estado: True=fijar manual · False=volver a auto · None=no tocar este
    # campo (el usuario no lo editó). Evita que editar SOLO el peso revierta un
    # FOB manual guardado (y viceversa). El backend es la autoridad del override.
    fob_unit: Optional[float] = None
    fob_manual: Optional[bool] = None
    peso_unit_lbs: Optional[float] = None
    peso_manual: Optional[bool] = None


class PricingSaveIn(BaseModel):
    tipo_embarque: Optional[TipoEmbarque] = None
    tc_tipo: Optional[Literal["manual", "config", "florida", "baukat"]] = None
    tc_valor: Optional[float] = None
    moneda: Optional[Literal["USD", "EUR"]] = None
    flete_en_me: Optional[bool] = None
    shipping_me: Optional[float] = None
    shipping_clp: Optional[float] = None
    observaciones: Optional[str] = None
    gastos: Optional[List[GastoIn]] = None
    items: Optional[List[ItemOverrideIn]] = None


@router.put("/{embarque_id}")
def guardar_embarque_pricing(
    embarque_id: int,
    payload: PricingSaveIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Guarda encabezado + gastos + overrides de FOB/TC, recalcula y persiste el snapshot."""
    embarque = _load_embarque(db, embarque_id)
    if not embarque:
        raise HTTPException(404, "Embarque no encontrado")
    pricing = _get_or_create_pricing(db, embarque)
    if pricing.estado == ESTADO_BLOQUEADO:
        raise HTTPException(409, "El pricing está cerrado; reábralo para editar")

    # 1) Encabezado
    if payload.tipo_embarque is not None:
        pricing.tipo_embarque = payload.tipo_embarque
    if payload.tc_tipo is not None:
        pricing.tc_tipo = payload.tc_tipo
    if payload.tc_valor is not None:
        pricing.tc_valor = payload.tc_valor
    if payload.moneda is not None:
        pricing.moneda = payload.moneda.upper()
    if payload.flete_en_me is not None:
        pricing.flete_en_me = payload.flete_en_me
    if payload.shipping_me is not None:
        pricing.shipping_me = payload.shipping_me
    if payload.shipping_clp is not None:
        pricing.shipping_clp = payload.shipping_clp
    if payload.observaciones is not None:
        pricing.observaciones = payload.observaciones
    pricing.usuario_id = getattr(current_user, "id", None)

    # Validación: flete prepagado en moneda extranjera necesita TC para convertir.
    if pricing.flete_en_me and _f(pricing.shipping_me) > 0 and _f(pricing.tc_valor) <= 0:
        raise HTTPException(400, "Defina un TC > 0 para convertir el flete en moneda extranjera a CLP")

    # Mantener shipping_clp coherente cuando el flete viene en ME (= ME × TC).
    # CLP se redondea a entero (el peso chileno no usa decimales), igual que el
    # resto de la app y que los totales mostrados → cuadre consistente.
    if pricing.flete_en_me:
        pricing.shipping_clp = round(_f(pricing.shipping_me) * _f(pricing.tc_valor), 0)

    # 2) Gastos: SIEMPRE las 6 líneas predeterminadas canónicas. El backend es la
    #    autoridad de las reglas de negocio (no confía en el cliente):
    #      · estructura fija de 6 tipos (si el cliente manda menos, se completan en 0);
    #      · glosa, capitaliza y orden se derivan del catálogo canónico;
    #      · iva=0 forzado para Arancel e IVA Importación (son exentos);
    #      · solo se toman del cliente los montos y los datos de factura/banco.
    if payload.gastos is not None:
        enviados = {g.tipo: g for g in payload.gastos}
        iva_exento = {"arancel", "iva_importacion"}
        db.query(EmbarquePricingGasto).filter(
            EmbarquePricingGasto.pricing_id == pricing.id
        ).delete()
        db.flush()
        for cat in GASTOS_CATALOGO:
            g = enviados.get(cat["tipo"])
            neto = _f(g.monto_neto) if g else 0.0
            iva = 0.0 if cat["tipo"] in iva_exento else (_f(g.iva) if g else 0.0)
            db.add(EmbarquePricingGasto(
                pricing_id=pricing.id, tipo=cat["tipo"], glosa=cat["glosa"],
                monto_neto=neto, iva=iva, capitaliza=cat["capitaliza"],
                nro_factura=(g.nro_factura if g else None),
                fecha_factura=(g.fecha_factura if g else None),
                banco=(g.banco if g else None),
                orden=cat["orden"],
            ))
        db.flush()

    # 3) Overrides por ítem (FOB y/o peso manual) — guardados como input que
    #    _build_inputs lee. FOB y peso comparten la MISMA fila emb_pricing_item y
    #    son INDEPENDIENTES: un flag en None significa "el usuario no tocó ese
    #    campo" y no se altera (evita que editar el peso revierta un FOB manual).
    overrides = {o.embarque_item_id: o for o in (payload.items or [])}
    if overrides:
        existing = {
            si.embarque_item_id: si
            for si in db.query(EmbarquePricingItem)
            .filter(EmbarquePricingItem.pricing_id == pricing.id).all()
        }
        for eiid, o in overrides.items():
            row = existing.get(eiid)
            # FOB manual válido solo con valor (evita "manual + vacío" → costo 0).
            quiere_fob = o.fob_manual is True and o.fob_unit is not None
            # Peso manual válido solo con valor > 0: un peso 0/negativo no es real
            # y debe caer al peso de la cotización.
            quiere_peso = o.peso_manual is True and o.peso_unit_lbs is not None and _f(o.peso_unit_lbs) > 0

            # Crear la fila de override una sola vez si algún campo la necesita
            # (FOB y peso comparten fila).
            if (quiere_fob or quiere_peso) and row is None:
                row = EmbarquePricingItem(pricing_id=pricing.id, embarque_item_id=eiid)
                db.add(row)

            # FOB
            if quiere_fob:
                row.fob_unit = _f(o.fob_unit)
                row.fob_origen = "manual"
            elif o.fob_manual is False and row is not None and row.fob_origen == "manual":
                # Quitar override manual → volver al FOB por defecto (factura/cotización).
                row.fob_origen = "auto"
                row.fob_unit = 0

            # Peso (espejo del FOB)
            if quiere_peso:
                row.peso_unit_lbs = _f(o.peso_unit_lbs)
                row.peso_origen = "manual"
            elif o.peso_manual is False and row is not None and (row.peso_origen or "auto") == "manual":
                # Quitar override → volver al peso de la cotización.
                row.peso_origen = "auto"
                row.peso_unit_lbs = 0
        db.flush()

    # FLUSH antes del refresh: refresh expira el objeto y lo recarga desde la DB,
    # así que un PUT solo-encabezado (sin gastos ni ítems, que ya flushean arriba)
    # perdería silenciosamente los cambios pendientes del encabezado.
    db.flush()
    db.refresh(pricing)
    detail = _compute_detail(db, embarque, pricing)

    # 4) Persistir snapshot + estado
    _persist_snapshot(db, pricing, detail)
    tiene_costo = _f(pricing.tc_valor) > 0 and detail["totales"].get("costo_total_clp", 0) > 0
    pricing.estado = "calculado" if tiene_costo else "borrador"
    pricing.calculado_at = datetime.utcnow() if tiene_costo else None
    db.commit()
    # El snapshot ya es consistente con `detail`; solo reflejamos el estado nuevo
    # (evita recomputar todo el detalle por segunda vez).
    detail["pricing"]["estado"] = pricing.estado
    detail["pricing"]["calculado_at"] = pricing.calculado_at.isoformat() if pricing.calculado_at else None
    return detail


@router.post("/{embarque_id}/cerrar")
def cerrar_pricing(
    embarque_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    embarque = _load_embarque(db, embarque_id)
    if not embarque:
        raise HTTPException(404, "Embarque no encontrado")
    pricing = _get_or_create_pricing(db, embarque)
    # Ya cerrado → NO recalcular ni sobreescribir el costo congelado.
    if pricing.estado == ESTADO_BLOQUEADO:
        raise HTTPException(409, "El pricing ya está cerrado; reábralo antes de volver a cerrarlo")
    if _f(pricing.tc_valor) <= 0:
        raise HTTPException(400, "Defina un TC mayor a 0 antes de cerrar")
    # Asegurar snapshot al día antes de congelar
    detail = _compute_detail(db, embarque, pricing)
    # No congelar un costo vacío: debe haber al menos un costo > 0 (FOB/flete/gastos).
    if detail["totales"].get("costo_total_clp", 0) <= 0:
        raise HTTPException(400, "El costo landed es 0. Cargue FOB, flete o gastos antes de cerrar")
    _persist_snapshot(db, pricing, detail)
    pricing.estado = ESTADO_BLOQUEADO
    pricing.calculado_at = pricing.calculado_at or datetime.utcnow()
    db.commit()
    db.refresh(pricing)
    return _compute_detail(db, embarque, pricing)


@router.post("/{embarque_id}/reabrir")
def reabrir_pricing(
    embarque_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    embarque = _load_embarque(db, embarque_id)
    if not embarque:
        raise HTTPException(404, "Embarque no encontrado")
    pricing = _get_or_create_pricing(db, embarque)
    pricing.estado = "calculado" if _f(pricing.tc_valor) > 0 else "borrador"
    db.commit()
    db.refresh(pricing)
    return _compute_detail(db, embarque, pricing)
