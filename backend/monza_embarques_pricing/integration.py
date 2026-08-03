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


# Monedas FOB con TC parametrizado en MonzaConfig. Cualquier otra NO tiene TC y por eso
# `tc_de_config` devuelve 0 (fail closed) en vez de inventarle uno.
MONEDAS_CON_TC = ("USD", "EUR")


def tc_de_config(cfg, moneda: str) -> float:
    """TC por defecto desde MonzaConfig según la moneda FOB.

    FAIL CLOSED: cada moneda tiene su columna (`tc_usd_clp` / `tc_eur_clp`) y **solo** esas
    dos existen. Cualquier otra cosa devuelve 0, el pricing nace en `manual` con 0 y OBLIGA
    a Contabilidad a teclear el TC real. Antes el `else` servía el TC del DÓLAR a todo lo que
    no fuera exactamente 'EUR' ('CLP', 'EURO', '', 'GBP' → el TC USD), o sea un error de
    costo SILENCIOSO con etiqueta de autoridad (`tc_tipo='config'`) en vez de un 0 evidente.
    """
    if not cfg:
        return 0.0
    m = (moneda or "").strip().upper()
    if m == "EUR":
        return _f(getattr(cfg, "tc_eur_clp", 0))
    if m == "USD":
        return _f(getattr(cfg, "tc_usd_clp", 0))
    return 0.0


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


def iva_rate_de_config(cfg) -> float:
    """Tasa de IVA (fracción) para la precarga de los gastos: la de MonzaConfig, que
    Monza sí parametriza, y si no hay dato el 19 % del catálogo. Es solo el DEFAULT de
    la línea; el IVA de cada gasto se edita con la factura del agente en la mano."""
    pct = _f(getattr(cfg, "iva_pct", 0)) if cfg else 0.0
    return (pct / 100.0) if pct > 0 else IVA_RATE


def _param_no_negativo(cfg, campo: str) -> float:
    """Parámetro de gasto de MonzaConfig, con piso en 0.

    ESTA ES LA SEGUNDA PUERTA A monza_emb_pricing_gasto: el `ge=0` del router solo cubre el
    payload del PUT, y estos 3 montos entran por acá copiados de MonzaConfig, cuyo editor
    (`monza_router_config.ConfigIn`) no valida signo. `service.total_gastos_que_capitalizan`
    SUMA los netos, así que un negativo RESTA del pozo que se prorratea a TODOS los ítems del
    embarque y `cerrar_pricing` lo CONGELA (solo exige costo_total > 0). Se recorta a 0 en vez
    de reventar: un parámetro mal tecleado no puede dejar mercadería recibida sin poder
    costearse. El guard del router (`_validar_gastos_no_negativos`) es el cinturón por si el
    dato ya quedó negativo en la BD.
    """
    return max(0.0, _f(getattr(cfg, campo, 0))) if cfg else 0.0


def seed_gastos(db: Session, pricing: MonzaEmbPricing, cfg) -> None:
    """Crea las 6 líneas de GASTOS LOCALES con los defaults de MonzaConfig (editables).

    Espejo de embarques_pricing.integration.seed_gastos. Antes nacían las 6 en 0: si el
    contador se olvidaba de cargarlas, el costo landed se congelaba SIN gastos de
    internación (cerrar_pricing solo exige costo_total > 0) y todos los ítems del
    embarque quedaban subvaluados en silencio. Los montos de MonzaConfig nacen en 0
    (no se copiaron los de Grupo AM): el contador los carga UNA vez en Configuración.
    """
    netos = {
        "desconsolidacion": _param_no_negativo(cfg, "desconsolidado_clp"),
        "almacenaje": _param_no_negativo(cfg, "bodegaje_clp"),
        "agencia": _param_no_negativo(cfg, "costo_agencia_minimo_clp"),
        "arancel": 0.0,
        "otros": 0.0,
        "iva_importacion": 0.0,
    }
    iva_rate = iva_rate_de_config(cfg)
    for cat in GASTOS_CATALOGO:
        neto = netos.get(cat["tipo"], 0.0)
        # Afectos (desconsolidación / almacenaje / agencia) llevan IVA; arancel y
        # "otros" nacen sin IVA (el usuario aplica la tasa si corresponde). Mismo
        # criterio que el PUT del router, que fuerza IVA 0 en arancel/iva_importacion.
        iva = round(neto * iva_rate, 0) if cat["tipo"] in ("desconsolidacion", "almacenaje", "agencia") else 0.0
        db.add(MonzaEmbPricingGasto(
            pricing_id=pricing.id, tipo=cat["tipo"], glosa=cat["glosa"],
            monto_neto=neto, iva=iva, capitaliza=cat["capitaliza"],
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
    # TC seed según la MONEDA del embarque. Si Config no tiene TC para esa moneda (solo hay
    # USD y EUR), queda en 0 con tc_tipo='manual' para FORZAR que Contabilidad lo ingrese:
    # etiquetar 'config' un 0 —o peor, el TC del dólar— sería darle autoridad a un número que
    # nadie parametrizó (espejo de embarques_pricing.integration).
    tc_seed = tc_de_config(cfg, moneda)
    pricing = MonzaEmbPricing(
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
        db.rollback()
        return (
            db.query(MonzaEmbPricing)
            .filter(MonzaEmbPricing.embarque_id == embarque.id)
            .first()
        )
    db.refresh(pricing)
    return pricing
