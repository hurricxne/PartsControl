"""Calculadora de precios MonzaParts.

Fórmulas (según documento):
  flete_clp      = peso_kg × tarifa_aerea_por_kg × tc_moneda_tarifa
  costo_clp      = costo × tc_moneda_item
  precio_neto    = (costo_clp + flete_clp) × (1 + markup_pct/100)   ← por unidad, sin IVA
  precio_bruto   = precio_neto × (1 + iva_pct/100)
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List

from database import get_db
from auth import get_current_user
from monza_models import MonzaLead, MonzaLeadItem, MonzaConfig

router = APIRouter(prefix="/api/monza/cotizador", tags=["monza-cotizador"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class CalidadIn(BaseModel):
    calidad: str          # genuine/oem/aftermarket/sin_calificar
    marca: Optional[str] = None
    procedencia: Optional[str] = None
    costo: float = 0
    moneda: str = "EUR"   # EUR/USD/CLP
    peso_kg: float = 0
    markup_pct: float = 0  # porcentaje entero, ej 28

class ItemCalculo(BaseModel):
    item_id: int
    calidades: List[CalidadIn]

class CalcularBody(BaseModel):
    lead_id: int
    items: List[ItemCalculo]
    tarifa_tipo: str = "aerea"  # por ahora solo aérea

class AplicarPrecioItem(BaseModel):
    item_id: int
    calidad: str
    marca: Optional[str] = None
    procedencia: Optional[str] = None
    precio_clp: float  # precio neto por unidad (sin IVA)
    plazo_entrega: Optional[str] = None

class AplicarBody(BaseModel):
    lead_id: int
    items: List[AplicarPrecioItem]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_config(db: Session) -> MonzaConfig:
    cfg = db.query(MonzaConfig).filter(MonzaConfig.id == 1).first()
    if not cfg:
        cfg = MonzaConfig(id=1)
        db.add(cfg)
        db.commit()
        db.refresh(cfg)
    return cfg


def _calcular_precio(costo: float, moneda: str, peso_kg: float, markup_pct: float, cfg: MonzaConfig) -> dict:
    """Retorna precio neto y bruto por unidad."""
    # TC para el costo
    if moneda == "EUR":
        tc_item = cfg.tc_eur_clp
    elif moneda == "USD":
        tc_item = cfg.tc_usd_clp
    else:
        tc_item = 1.0

    # TC para la tarifa aérea
    if cfg.moneda_tarifa == "EUR":
        tc_tarifa = cfg.tc_eur_clp
    else:
        tc_tarifa = cfg.tc_usd_clp

    costo_clp = costo * tc_item
    flete_clp = peso_kg * cfg.tarifa_aerea_por_kg * tc_tarifa
    precio_neto = (costo_clp + flete_clp) * (1 + markup_pct / 100)
    precio_bruto = precio_neto * (1 + cfg.iva_pct / 100)

    return {
        "costo_clp": round(costo_clp),
        "flete_clp": round(flete_clp),
        "precio_neto": round(precio_neto),
        "precio_bruto": round(precio_bruto),
        "tc_aplicado": tc_item,
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────


# ── Log helper ────────────────────────────────────────────────────────────────
def _log(db, user_email, accion, entidad, entidad_id=None, entidad_ref=None, detalle=None):
    from monza_models import MonzaLog
    lg = MonzaLog(user_email=user_email, accion=accion, entidad=entidad,
                  entidad_id=entidad_id, entidad_ref=entidad_ref, detalle=detalle)
    db.add(lg)
    db.commit()

@router.post("/calcular")
def calcular_precios(body: CalcularBody, db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Devuelve precios calculados sin persistir nada."""
    cfg = _get_config(db)

    lead = db.query(MonzaLead).filter(MonzaLead.id == body.lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead no encontrado")

    resultados = []
    total_neto = 0

    for item_calc in body.items:
        db_item = db.query(MonzaLeadItem).filter(
            MonzaLeadItem.id == item_calc.item_id,
            MonzaLeadItem.lead_id == body.lead_id,
        ).first()
        if not db_item:
            continue

        calidades_resultado = []
        for cal in item_calc.calidades:
            precios = _calcular_precio(
                costo=cal.costo,
                moneda=cal.moneda,
                peso_kg=cal.peso_kg,
                markup_pct=cal.markup_pct,
                cfg=cfg,
            )
            calidades_resultado.append({
                "calidad": cal.calidad,
                "marca": cal.marca,
                "procedencia": cal.procedencia,
                "costo": cal.costo,
                "moneda": cal.moneda,
                "peso_kg": cal.peso_kg,
                "markup_pct": cal.markup_pct,
                **precios,
            })

        # El neto para el total usa el primer precio con costo > 0
        for cr in calidades_resultado:
            if cr["precio_neto"] > 0:
                total_neto += cr["precio_neto"] * db_item.cantidad
                break

        resultados.append({
            "item_id": db_item.id,
            "descripcion": db_item.descripcion,
            "numero_parte": db_item.numero_parte,
            "cantidad": db_item.cantidad,
            "calidades": calidades_resultado,
        })

    return {
        "config": {
            "tc_usd_clp": cfg.tc_usd_clp,
            "tc_eur_clp": cfg.tc_eur_clp,
            "tarifa_aerea_por_kg": cfg.tarifa_aerea_por_kg,
            "moneda_tarifa": cfg.moneda_tarifa,
            "iva_pct": cfg.iva_pct,
        },
        "items": resultados,
        "total_neto_estimado": round(total_neto),
    }


@router.post("/aplicar")
def aplicar_precios(body: AplicarBody, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """Aplica precios calculados a los items del lead."""
    lead = db.query(MonzaLead).filter(MonzaLead.id == body.lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead no encontrado")

    from datetime import datetime
    from monza_models import MonzaLeadActividad

    for ap in body.items:
        item = db.query(MonzaLeadItem).filter(
            MonzaLeadItem.id == ap.item_id,
            MonzaLeadItem.lead_id == body.lead_id,
        ).first()
        if item:
            item.precio_clp = ap.precio_clp
            item.calidad = ap.calidad
            if ap.marca:
                item.marca = ap.marca
            if ap.procedencia:
                item.procedencia = ap.procedencia
            if ap.plazo_entrega:
                item.plazo_entrega = ap.plazo_entrega

    asesor_nombre = current_user.email.split("@")[0].title()
    db.add(MonzaLeadActividad(
        lead_id=body.lead_id,
        tipo="cotizacion",
        descripcion=f"Precios calculados y aplicados por {asesor_nombre} ({len(body.items)} ítem(s))",
        usuario=asesor_nombre,
        usuario_id=current_user.id,
    ))

    lead.fecha_actualizacion = datetime.utcnow()
    db.commit()
    _log(db, current_user.email, "UPDATE", "lead",
         body.lead_id, f"lead#{body.lead_id}", f"Calculadora: {len(body.items)} precio(s) aplicado(s)")
    return {"ok": True, "items_actualizados": len(body.items)}
