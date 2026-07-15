"""Creación / seed del pricing de un embarque (fuente única).

Aislado del router (NO importa FastAPI) para que Logística (compras.py) pueda
auto-crear el pricing al momento de embarcar sin arrastrar el grafo del router.

Reglas de flete por tipo de embarque (confirmadas con el dueño y el App Script):
  · normal  (LATAM): el flete puede pagarse en CLP en Chile, o venir prepagado
    por el proveedor en moneda extranjera (neteado del SWIFT). Default: CLP.
  · courier (DHL):   igual que LATAM, elegible. Default: CLP.
  · baukat  (Europa): el flete viene SIEMPRE prepagado por el proveedor (EUR).
  · fastmark:        el flete se paga SIEMPRE localmente en CLP (no prepagado).
`flete_en_me` es de todos modos editable en Contabilidad; esto solo fija el default.
"""
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from models.models import Embarque, OcProveedor, ConfiguracionCotizador
from .models import EmbarquePricing, EmbarquePricingGasto
from .service import GASTOS_CATALOGO, IVA_RATE, _f

# Default de "flete en moneda extranjera (prepagado por el proveedor)" por tipo.
FLETE_EN_ME_DEFAULT = {
    "normal": False,    # LATAM: default CLP, elegible a proveedor
    "courier": False,   # DHL: default CLP, elegible a proveedor
    "baukat": True,     # Europa: siempre prepagado por el proveedor (EUR)
    "fastmark": False,  # consolidado: siempre CLP local
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


def get_cfg(db: Session) -> Optional[ConfiguracionCotizador]:
    return db.query(ConfiguracionCotizador).filter(ConfiguracionCotizador.id == 1).first()


def moneda_de_embarque(db: Session, embarque: Embarque) -> str:
    """Moneda FOB del embarque: la de la 1ª OC proveedor de sus ítems (default USD)."""
    ocp_ids = [ei.oc_proveedor_id for ei in embarque.items if ei.oc_proveedor_id]
    if ocp_ids:
        ocp = db.query(OcProveedor).filter(OcProveedor.id == ocp_ids[0]).first()
        if ocp and ocp.moneda:
            return ocp.moneda.upper()
    return "USD"


def seed_gastos(db: Session, pricing: EmbarquePricing, cfg) -> None:
    """Crea las 6 líneas de GASTOS LOCALES con defaults de Config (editables)."""
    netos = {
        "desconsolidacion": _f(getattr(cfg, "desconsolidado_clp", 0)) if cfg else 0.0,
        "almacenaje": _f(getattr(cfg, "bodegaje_clp", 0)) if cfg else 0.0,
        "agencia": _f(getattr(cfg, "costo_agencia_minimo_clp", 0)) if cfg else 0.0,
        "arancel": 0.0,
        "otros": 0.0,
        "iva_importacion": 0.0,
    }
    for cat in GASTOS_CATALOGO:
        neto = netos.get(cat["tipo"], 0.0)
        # Afectos (desconsolidación/almacenaje/agencia) llevan IVA; arancel y
        # "otros" se dejan sin IVA por defecto (el usuario aplica el 19% si toca).
        iva = round(neto * IVA_RATE, 0) if cat["tipo"] in ("desconsolidacion", "almacenaje", "agencia") else 0.0
        db.add(EmbarquePricingGasto(
            pricing_id=pricing.id, tipo=cat["tipo"], glosa=cat["glosa"],
            monto_neto=neto, iva=iva, capitaliza=cat["capitaliza"],
            nro_factura=None, orden=cat["orden"],
        ))


def ensure_pricing_for_embarque(
    db: Session, embarque: Embarque, *, commit: bool = True
) -> Optional[EmbarquePricing]:
    """Crea (si no existe) el pricing del embarque con sus gastos locales seed.

    Idempotente: si ya existe, lo devuelve sin tocar. Pensado para llamarse:
      · desde Logística al crear el embarque (auto-creación, commit=True), y
      · de forma diferida desde el router al abrir el detalle.

    Con commit=True maneja la carrera concurrente (UNIQUE embarque_id) re-consultando.
    """
    pricing = (
        db.query(EmbarquePricing)
        .filter(EmbarquePricing.embarque_id == embarque.id)
        .first()
    )
    if pricing:
        return pricing

    cfg = get_cfg(db)
    moneda = moneda_de_embarque(db, embarque)
    tipo = detect_tipo(embarque.forwarder, moneda)
    # TC seed según la MONEDA del embarque: el TC USD de Config solo aplica a USD.
    # Para EUR (Baukat) se usa el TC EUR de Config si existe; si no, queda en 0
    # con tc_tipo manual para forzar que Contabilidad lo ingrese (nunca sembrar
    # silenciosamente el TC USD en un embarque EUR).
    if moneda == "EUR":
        tc_seed = _f(getattr(cfg, "tipo_cambio_eur", 0)) if cfg else 0.0
    else:
        tc_seed = _f(getattr(cfg, "tipo_cambio_usd", 0)) if cfg else 0.0
    pricing = EmbarquePricing(
        embarque_id=embarque.id,
        tipo_embarque=tipo,
        tc_tipo="config" if tc_seed > 0 else "manual",
        tc_valor=tc_seed,
        moneda=moneda,
        flete_en_me=FLETE_EN_ME_DEFAULT.get(tipo, False),
        shipping_me=0,
        shipping_clp=0,
        estado="borrador",
    )
    db.add(pricing)
    if not commit:
        db.flush()
        seed_gastos(db, pricing, cfg)
        db.flush()
        return pricing
    try:
        db.flush()
        seed_gastos(db, pricing, cfg)
        db.commit()
    except IntegrityError:
        # Carrera: otra transacción creó el pricing primero → usar el existente.
        db.rollback()
        return (
            db.query(EmbarquePricing)
            .filter(EmbarquePricing.embarque_id == embarque.id)
            .first()
        )
    db.refresh(pricing)
    return pricing
