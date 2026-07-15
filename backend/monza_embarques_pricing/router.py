"""API del módulo Embarques Pricing MonzaParts (Contabilidad → costo landed).

SOLO MonzaParts: candado require_empresa("automotriz"). Espejo del módulo de Grupo AM
(backend/embarques_pricing/router.py), apuntando a las tablas monza_*.

Integración NO invasiva con Logística: lee los embarques que crea Logística
(monza_embarques) y superpone su pricing. Por eso TODO embarque "aparece" acá; el
registro de pricing se crea diferido la primera vez que Contabilidad lo abre.

FOB por ítem: DEFAULT = costo del ítem de cotización (estimado), editable a mano.
Peso por ítem: peso_kg del ítem de cotización (editable a mano vía override no aplica:
el peso sale de la cotización; el FOB es el override manual, igual que Grupo AM).

Prefijo: /api/monza/embarques-pricing (montado sin prefix; el router ya lo trae).
"""
from datetime import datetime
from typing import List, Literal, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

from database import get_db
from auth import get_current_user
from empresa_guard import require_empresa
from models.models import User
from monza_models import MonzaEmbarque, MonzaEmbarqueItem, MonzaCotizacionItem
from .models import MonzaEmbPricing, MonzaEmbPricingGasto, MonzaEmbPricingItem
from .service import calcular_landed, total_gastos_que_capitalizan, GASTOS_CATALOGO, _f
from .integration import detect_tipo as _detect_tipo, ensure_pricing_for_embarque

router = APIRouter(
    prefix="/api/monza/embarques-pricing",
    tags=["monza-embarques-pricing"],
    dependencies=[Depends(require_empresa("automotriz"))],
)

ESTADO_BLOQUEADO = "cerrado"


def _get_or_create_pricing(db: Session, embarque: MonzaEmbarque) -> MonzaEmbPricing:
    """Crea (si falta) o devuelve el pricing del embarque. Nunca None hacia el endpoint."""
    pricing = ensure_pricing_for_embarque(db, embarque, commit=True)
    if pricing is None:
        raise HTTPException(500, "No se pudo crear el registro de pricing del embarque")
    return pricing


def _embarque_or_404(db: Session, embarque_id: int) -> MonzaEmbarque:
    emb = db.query(MonzaEmbarque).filter(MonzaEmbarque.id == embarque_id).first()
    if not emb:
        raise HTTPException(404, "Embarque no encontrado")
    return emb


def _embarque_items(db: Session, embarque_id: int) -> List[Tuple[MonzaEmbarqueItem, Optional[MonzaCotizacionItem]]]:
    """(MonzaEmbarqueItem, MonzaCotizacionItem|None) del embarque, en 2 queries (sin N+1)."""
    eis = (
        db.query(MonzaEmbarqueItem)
        .filter(MonzaEmbarqueItem.embarque_id == embarque_id)
        .order_by(MonzaEmbarqueItem.id.asc())
        .all()
    )
    item_ids = [ei.item_id for ei in eis if ei.item_id]
    cot_by_id = {}
    if item_ids:
        cot_by_id = {
            it.id: it for it in
            db.query(MonzaCotizacionItem).filter(MonzaCotizacionItem.id.in_(item_ids)).all()
        }
    return [(ei, cot_by_id.get(ei.item_id)) for ei in eis]


# ─── FOB por ítem: costo de la cotización → 0 (manual) ────────────────────────
def _fob_defaults(pairs) -> dict:
    """item_cotizacion_id → (fob_unit_default, origen). MonzaParts: el FOB estimado es el
    costo del ítem de cotización; si no hay, 0 con origen "auto" (sin dato, a cargar a
    mano). "manual" se reserva para overrides reales del usuario (así el front muestra el
    botón de "volver al FOB de la cotización" solo cuando hay algo que revertir)."""
    out: dict = {}
    for _ei, cot in pairs:
        if cot is None:
            continue
        if _f(cot.costo) > 0:
            out[cot.id] = (_f(cot.costo), "cotizacion")
        else:
            out[cot.id] = (0.0, "auto")
    return out


def _build_inputs(db: Session, pricing: MonzaEmbPricing, pairs) -> List[dict]:
    """Arma los inputs por ítem mezclando defaults (cotización) + overrides de FOB guardados."""
    fob_def = _fob_defaults(pairs)
    stored = {
        si.embarque_item_id: si
        for si in db.query(MonzaEmbPricingItem)
        .filter(MonzaEmbPricingItem.pricing_id == pricing.id).all()
    }
    tc_header = _f(pricing.tc_valor)
    inputs: List[dict] = []
    for ei, cot in pairs:
        icid = cot.id if cot else None
        default_fob, default_origen = fob_def.get(icid, (0.0, "manual"))
        s = stored.get(ei.id)
        # Override manual solo si trae un valor > 0: un "manual" en 0 (ítem que
        # nunca tuvo precio, o 0 explícito) no es un precio real y NO debe
        # bloquear el FOB default de la cotización que llega/cambia después.
        if s is not None and s.fob_origen == "manual" and _f(s.fob_unit) > 0:
            fob_unit, origen = _f(s.fob_unit), "manual"
        else:
            fob_unit, origen = default_fob, default_origen
        inputs.append({
            "embarque_item_id": ei.id,
            "item_cotizacion_id": icid,
            "numero_parte": (cot.numero_parte if cot else None) or "",
            "descripcion": (cot.descripcion if cot else None) or "",
            "moneda": pricing.moneda,
            "cantidad": _f(cot.cantidad) if cot else 0.0,
            "peso_unit": _f(cot.peso_kg) if cot else 0.0,
            "fob_unit": fob_unit,
            "fob_default": default_fob,
            "fob_origen": origen,
            "tc_valor": tc_header,
        })
    return inputs


def _shipping_total_clp(pricing: MonzaEmbPricing) -> float:
    """Flete total en CLP: ME × TC si viene en moneda extranjera, o el CLP directo."""
    if pricing.flete_en_me:
        return _f(pricing.shipping_me) * _f(pricing.tc_valor)
    return _f(pricing.shipping_clp)


def _serialize_gasto(g: MonzaEmbPricingGasto) -> dict:
    neto, iva = _f(g.monto_neto), _f(g.iva)
    return {
        "id": g.id, "tipo": g.tipo, "glosa": g.glosa,
        "monto_neto": neto, "iva": iva, "total_bruto": neto + iva,
        "capitaliza": bool(g.capitaliza), "nro_factura": g.nro_factura,
        "fecha_factura": g.fecha_factura, "banco": g.banco, "orden": g.orden,
    }


def _snapshot_items(db: Session, pricing: MonzaEmbPricing) -> List[dict]:
    """Filas del snapshot persistido (pricing cerrado → costo CONGELADO).

    Devuelve las filas guardadas al cerrar, con la misma forma que el recálculo
    en vivo, para que el detalle cuadre siempre con el listado.
    """
    snap = (
        db.query(MonzaEmbPricingItem)
        .filter(MonzaEmbPricingItem.pricing_id == pricing.id)
        .order_by(MonzaEmbPricingItem.id.asc())
        .all()
    )
    return [{
        "embarque_item_id": s.embarque_item_id,
        "item_cotizacion_id": s.item_cotizacion_id,
        "numero_parte": s.numero_parte or "",
        "descripcion": s.descripcion or "",
        "moneda": s.moneda,
        "cantidad": _f(s.cantidad),
        "peso_unit_kg": _f(s.peso_unit_kg),
        "peso_total_kg": round(_f(s.peso_total_kg), 2),
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


def _compute_detail(db: Session, embarque: MonzaEmbarque, pricing: MonzaEmbPricing) -> dict:
    gastos = sorted(pricing.gastos, key=lambda g: (g.orden or 0, g.id))
    gastos_dicts = [_serialize_gasto(g) for g in gastos]
    total_cap = total_gastos_que_capitalizan(
        [{"monto_neto": g["monto_neto"], "capitaliza": g["capitaliza"]} for g in gastos_dicts]
    )
    total_iva = sum(g["iva"] for g in gastos_dicts if g["capitaliza"])
    iva_importacion = sum(g["monto_neto"] for g in gastos_dicts if g["tipo"] == "iva_importacion")

    shipping_total = _shipping_total_clp(pricing)
    pairs = _embarque_items(db, embarque.id)

    # Cerrado → el detalle sale del snapshot persistido al cerrar (costo
    # CONGELADO, igual que el listado). Abierto → recálculo en vivo.
    if pricing.estado == ESTADO_BLOQUEADO:
        items_out = _snapshot_items(db, pricing)
    else:
        inputs = _build_inputs(db, pricing, pairs)
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
                "peso_unit_kg": r["peso_unit"],
                "peso_total_kg": round(r["peso_total"], 2),
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
            "forwarder": embarque.forwarder, "awb": embarque.awb, "tracking": embarque.tracking,
            "fecha_despacho": embarque.fecha_despacho, "fecha_llegada_est": embarque.fecha_llegada_est,
            "n_items": len(pairs),
        },
        "pricing": {
            "id": pricing.id,
            "correlativo": pricing.id,
            "tipo_embarque": pricing.tipo_embarque,
            "tc_tipo": pricing.tc_tipo, "tc_valor": _f(pricing.tc_valor),
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
        "totales": {
            "n_items": len(items_out),
            "peso_total_kg": round(sum(r["peso_total_kg"] for r in items_out), 2),
            "fob_total_me": round(sum(r["fob_total"] for r in items_out), 2),
            "fob_clp": sum(r["fob_clp"] for r in items_out),
            "shipping_clp": sum(r["shipping_clp"] for r in items_out),
            "cif_clp": sum(r["cif_clp"] for r in items_out),
            "gastos_clp": sum(r["gastos_clp"] for r in items_out),
            "costo_total_clp": sum(r["costo_total_clp"] for r in items_out),
        },
    }


def _persist_snapshot(db: Session, pricing: MonzaEmbPricing, detail: dict) -> None:
    """Guarda el snapshot por ítem (inputs + computados) para congelar el costo."""
    db.query(MonzaEmbPricingItem).filter(MonzaEmbPricingItem.pricing_id == pricing.id).delete()
    db.flush()  # materializa el borrado antes de re-insertar (sin colisiones en el identity map)
    for r in detail["items"]:
        db.add(MonzaEmbPricingItem(
            pricing_id=pricing.id,
            embarque_item_id=r["embarque_item_id"],
            item_cotizacion_id=r["item_cotizacion_id"],
            numero_parte=r["numero_parte"], descripcion=r["descripcion"], moneda=r["moneda"],
            cantidad=r["cantidad"], peso_unit_kg=r["peso_unit_kg"],
            peso_total_kg=r["peso_total_kg"], fob_unit=r["fob_unit"],
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
    embarques = db.query(MonzaEmbarque).order_by(MonzaEmbarque.id.desc()).all()
    pricings = {p.embarque_id: p for p in db.query(MonzaEmbPricing).all()}
    # Conteo de ítems por embarque (1 query agrupada).
    n_items = {
        eid: int(n) for eid, n in
        db.query(MonzaEmbarqueItem.embarque_id, func.count(MonzaEmbarqueItem.id))
        .group_by(MonzaEmbarqueItem.embarque_id).all()
    }
    # Costo total por pricing (1 query agrupada).
    costos = {
        pid: _f(total) for pid, total in
        db.query(MonzaEmbPricingItem.pricing_id, func.sum(MonzaEmbPricingItem.costo_total_clp))
        .group_by(MonzaEmbPricingItem.pricing_id).all()
    }
    out = []
    for e in embarques:
        if q:
            ql = q.lower()
            hay = " ".join([e.numero or "", e.forwarder or "", e.awb or ""]).lower()
            if ql not in hay:
                continue
        p = pricings.get(e.id)
        out.append({
            "embarque_id": e.id,
            "correlativo": p.id if p else None,
            "numero": e.numero,
            "estado_logistica": e.estado,
            "forwarder": e.forwarder,
            "awb": e.awb,
            "fecha_despacho": e.fecha_despacho,
            "n_items": n_items.get(e.id, 0),
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
    embarque = _embarque_or_404(db, embarque_id)
    pricing = _get_or_create_pricing(db, embarque)
    return _compute_detail(db, embarque, pricing)


# ── Payloads ──
GastoTipo = Literal["desconsolidacion", "almacenaje", "agencia", "arancel", "otros", "iva_importacion"]
TipoEmbarque = Literal["normal", "courier", "baukat", "fastmark"]


# Montos siempre >= 0 (un negativo corrompería el costo landed); strings con tope de
# longitud para fallar limpio en la API antes de tocar la BD.
class GastoIn(BaseModel):
    tipo: GastoTipo
    glosa: Optional[str] = Field(None, max_length=120)
    monto_neto: float = Field(0, ge=0)
    iva: float = Field(0, ge=0)
    capitaliza: bool = True
    nro_factura: Optional[str] = Field(None, max_length=100)
    fecha_factura: Optional[str] = Field(None, max_length=30)
    banco: Optional[str] = Field(None, max_length=100)
    orden: int = 0


class ItemOverrideIn(BaseModel):
    embarque_item_id: int
    fob_unit: Optional[float] = Field(None, ge=0)
    fob_manual: bool = False


class PricingSaveIn(BaseModel):
    tipo_embarque: Optional[TipoEmbarque] = None
    tc_tipo: Optional[Literal["manual", "config"]] = None
    # tc_valor >= 0: 0 = "aún sin TC" (borrador). La regla TC > 0 para calcular/cerrar
    # se valida aparte en los endpoints.
    tc_valor: Optional[float] = Field(None, ge=0)
    moneda: Optional[Literal["USD", "EUR"]] = None
    flete_en_me: Optional[bool] = None
    shipping_me: Optional[float] = Field(None, ge=0)
    shipping_clp: Optional[float] = Field(None, ge=0)
    observaciones: Optional[str] = Field(None, max_length=65535)
    gastos: Optional[List[GastoIn]] = None
    items: Optional[List[ItemOverrideIn]] = None


@router.put("/{embarque_id}")
def guardar_embarque_pricing(
    embarque_id: int,
    payload: PricingSaveIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Guarda encabezado + gastos + overrides de FOB, recalcula y persiste el snapshot."""
    embarque = _embarque_or_404(db, embarque_id)
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

    if pricing.flete_en_me and _f(pricing.shipping_me) > 0 and _f(pricing.tc_valor) <= 0:
        raise HTTPException(400, "Defina un TC > 0 para convertir el flete en moneda extranjera a CLP")
    if pricing.flete_en_me:
        pricing.shipping_clp = round(_f(pricing.shipping_me) * _f(pricing.tc_valor), 0)

    # 2) Gastos: SIEMPRE las 6 líneas canónicas (el backend es la autoridad).
    if payload.gastos is not None:
        enviados = {g.tipo: g for g in payload.gastos}
        iva_exento = {"arancel", "iva_importacion"}
        db.query(MonzaEmbPricingGasto).filter(MonzaEmbPricingGasto.pricing_id == pricing.id).delete()
        db.flush()
        for cat in GASTOS_CATALOGO:
            g = enviados.get(cat["tipo"])
            neto = _f(g.monto_neto) if g else 0.0
            iva = 0.0 if cat["tipo"] in iva_exento else (_f(g.iva) if g else 0.0)
            db.add(MonzaEmbPricingGasto(
                pricing_id=pricing.id, tipo=cat["tipo"], glosa=cat["glosa"],
                monto_neto=neto, iva=iva, capitaliza=cat["capitaliza"],
                nro_factura=(g.nro_factura if g else None),
                fecha_factura=(g.fecha_factura if g else None),
                banco=(g.banco if g else None),
                orden=cat["orden"],
            ))
        db.flush()

    # 3) Overrides por ítem (FOB manual)
    overrides = {o.embarque_item_id: o for o in (payload.items or [])}
    if overrides:
        # Solo se aceptan overrides de ítems que pertenecen a ESTE embarque (evita
        # contaminar el snapshot con embarque_item_id ajenos o inexistentes).
        valid_ids = {ei.id for ei, _ in _embarque_items(db, embarque.id)}
        invalidos = sorted(eiid for eiid in overrides if eiid not in valid_ids)
        if invalidos:
            raise HTTPException(400, f"embarque_item_id no pertenece a este embarque: {invalidos}")
        existing = {
            si.embarque_item_id: si
            for si in db.query(MonzaEmbPricingItem)
            .filter(MonzaEmbPricingItem.pricing_id == pricing.id).all()
        }
        for eiid, o in overrides.items():
            row = existing.get(eiid)
            if o.fob_manual and o.fob_unit is not None:
                if row is None:
                    row = MonzaEmbPricingItem(pricing_id=pricing.id, embarque_item_id=eiid)
                    db.add(row)
                row.fob_unit = _f(o.fob_unit)
                row.fob_origen = "manual"
            elif not o.fob_manual and row is not None and row.fob_origen == "manual":
                row.fob_origen = "auto"
                row.fob_unit = 0
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
    detail["pricing"]["estado"] = pricing.estado
    detail["pricing"]["calculado_at"] = pricing.calculado_at.isoformat() if pricing.calculado_at else None
    return detail


@router.post("/{embarque_id}/cerrar")
def cerrar_pricing(
    embarque_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    embarque = _embarque_or_404(db, embarque_id)
    pricing = _get_or_create_pricing(db, embarque)
    # Ya cerrado → NO recalcular ni sobreescribir el costo congelado.
    if pricing.estado == ESTADO_BLOQUEADO:
        raise HTTPException(409, "El pricing ya está cerrado; reábralo antes de volver a cerrarlo")
    if _f(pricing.tc_valor) <= 0:
        raise HTTPException(400, "Defina un TC mayor a 0 antes de cerrar")
    detail = _compute_detail(db, embarque, pricing)
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
    embarque = _embarque_or_404(db, embarque_id)
    pricing = _get_or_create_pricing(db, embarque)
    pricing.estado = "calculado" if _f(pricing.tc_valor) > 0 else "borrador"
    db.commit()
    db.refresh(pricing)
    return _compute_detail(db, embarque, pricing)
