"""Creación / seed del pricing de un embarque MonzaParts (fuente única).

Aislado del router (NO importa FastAPI) para poder auto-crear el pricing desde otros
puntos (p.ej. Logística al embarcar) sin arrastrar el grafo del router. Mismo diseño
que el módulo de Grupo AM.

Reglas de flete por tipo (mismas que Grupo AM):
  · normal  (LATAM): flete elegible (CLP o prepagado ME). Default: CLP.
  · courier (DHL):   elegible. Default: CLP.
  · baukat  (Europa): flete SIEMPRE prepagado por el proveedor (EUR).
  · fastmark:        flete SIEMPRE local en CLP.
`flete_en_me` es editable en Contabilidad; esto solo fija el default.
"""
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from monza_models import MonzaEmbarque, MonzaEmbarqueItem, MonzaCotizacionItem, MonzaConfig
from .models import MonzaEmbPricing, MonzaEmbPricingGasto
from .service import GASTOS_CATALOGO, IVA_RATE, _f

FLETE_EN_ME_DEFAULT = {
    "normal": False,
    "courier": False,
    "baukat": True,
    "fastmark": False,
}


def detect_tipo(forwarder: Optional[str], moneda: str) -> str:
    """Auto-detecta el tipo de embarque por forwarder / moneda (editable luego)."""
    f = (forwarder or "").lower()
    if "baukat" in f:
        return "baukat"
    if any(c in f for c in ("dhl", "fedex", "ups", "courier")):
        return "courier"
    if "fast" in f:
        return "fastmark"
    if (moneda or "").upper() == "EUR":
        return "baukat"
    return "normal"


def get_cfg(db: Session) -> Optional[MonzaConfig]:
    return db.query(MonzaConfig).order_by(MonzaConfig.id.asc()).first()


def tc_de_config(cfg, moneda: str) -> float:
    """TC por defecto desde MonzaConfig según la moneda FOB."""
    if not cfg:
        return 0.0
    return _f(cfg.tc_eur_clp) if (moneda or "").upper() == "EUR" else _f(cfg.tc_usd_clp)


def moneda_de_embarque(db: Session, embarque_id: int) -> str:
    """Moneda FOB del embarque: la del 1er ítem de cotización del embarque (default USD)."""
    ei = (
        db.query(MonzaEmbarqueItem)
        .filter(MonzaEmbarqueItem.embarque_id == embarque_id)
        .order_by(MonzaEmbarqueItem.id.asc())
        .first()
    )
    if ei:
        it = db.query(MonzaCotizacionItem).filter(MonzaCotizacionItem.id == ei.item_id).first()
        if it and it.moneda:
            return it.moneda.upper()
    return "USD"


def seed_gastos(db: Session, pricing: MonzaEmbPricing) -> None:
    """Crea las 6 líneas de GASTOS LOCALES en 0 (MonzaParts los carga a mano)."""
    for cat in GASTOS_CATALOGO:
        db.add(MonzaEmbPricingGasto(
            pricing_id=pricing.id, tipo=cat["tipo"], glosa=cat["glosa"],
            monto_neto=0, iva=0, capitaliza=cat["capitaliza"],
            nro_factura=None, orden=cat["orden"],
        ))


def ensure_pricing_for_embarque(db: Session, embarque: MonzaEmbarque, *, commit: bool = True) -> Optional[MonzaEmbPricing]:
    """Crea (si no existe) el pricing del embarque con sus 6 gastos locales seed.

    Idempotente: si ya existe, lo devuelve sin tocar. Con commit=True maneja la carrera
    concurrente (UNIQUE embarque_id) re-consultando.
    """
    pricing = (
        db.query(MonzaEmbPricing)
        .filter(MonzaEmbPricing.embarque_id == embarque.id)
        .first()
    )
    if pricing:
        return pricing

    cfg = get_cfg(db)
    moneda = moneda_de_embarque(db, embarque.id)
    tipo = detect_tipo(embarque.forwarder, moneda)
    pricing = MonzaEmbPricing(
        embarque_id=embarque.id,
        tipo_embarque=tipo,
        tc_tipo="config",
        tc_valor=tc_de_config(cfg, moneda),
        moneda=moneda,
        flete_en_me=FLETE_EN_ME_DEFAULT.get(tipo, False),
        shipping_me=0,
        shipping_clp=0,
        estado="borrador",
    )
    db.add(pricing)
    if not commit:
        db.flush()
        seed_gastos(db, pricing)
        db.flush()
        return pricing
    try:
        db.flush()
        seed_gastos(db, pricing)
        db.commit()
    except IntegrityError:
        db.rollback()
        return (
            db.query(MonzaEmbPricing)
            .filter(MonzaEmbPricing.embarque_id == embarque.id)
            .first()
        )
    db.refresh(pricing)
    return pricing
