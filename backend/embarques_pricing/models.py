"""Tablas del módulo Embarques Pricing (costo landed).

3 tablas NUEVAS y aditivas. No tocan ninguna tabla existente. Se crean solas
con `Base.metadata.create_all()` porque este módulo se importa desde el router,
que a su vez se importa en main.py antes del create_all.

  emb_pricing       → 1 fila por embarque: TC, flete, estado del pricing.
  emb_pricing_gasto → líneas del bloque GASTOS LOCALES (neto, IVA, factura).
  emb_pricing_item  → snapshot del costo landed por ítem (al "calcular"/"cerrar").
"""
from sqlalchemy import (
    Column, Integer, String, Numeric, Boolean, Text, DateTime, ForeignKey,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from database import Base


class EmbarquePricing(Base):
    """Cabecera del pricing de un embarque (1:1 con embarques.id)."""
    __tablename__ = "emb_pricing"

    id = Column(Integer, primary_key=True, index=True)
    # ON DELETE CASCADE: si Logística borra un embarque, su pricing se va con él
    # (no bloquea la operación de Logística → integración no invasiva).
    embarque_id = Column(
        Integer, ForeignKey("embarques.id", ondelete="CASCADE"),
        unique=True, index=True, nullable=False,
    )

    # Tipo de embarque (auto-detectado del forwarder/moneda; editable): normal|courier|baukat|fastmark
    tipo_embarque = Column(String(20), default="normal")

    # Tipo de cambio para convertir FOB (ME → CLP)
    tc_tipo = Column(String(20), default="manual")        # manual|config|florida|baukat
    tc_valor = Column(Numeric(14, 4), default=0)
    moneda = Column(String(10), default="USD")            # moneda FOB (de la OC proveedor)

    # Flete internacional
    flete_en_me = Column(Boolean, default=False)          # True: el flete viene en moneda extranjera
    shipping_me = Column(Numeric(14, 4), nullable=True)   # flete en ME (si flete_en_me)
    shipping_clp = Column(Numeric(16, 2), default=0)      # flete total en CLP (directo o = ME × TC)

    estado = Column(String(20), default="borrador")       # borrador|calculado|cerrado
    observaciones = Column(Text, nullable=True)

    calculado_at = Column(DateTime(timezone=True), nullable=True)
    usuario_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    gastos = relationship("EmbarquePricingGasto", back_populates="pricing",
                          cascade="all, delete-orphan")
    items = relationship("EmbarquePricingItem", back_populates="pricing",
                         cascade="all, delete-orphan")


class EmbarquePricingGasto(Base):
    """Línea del bloque GASTOS LOCALES (con desglose tributario).

    IDENTIDAD ESTABLE — leer antes de tocar el guardado
    ---------------------------------------------------
    La PK de esta fila es una LLAVE DE PLATA: `cont_compra.emb_pricing_gasto_id` la
    referencia para saber que la factura del forwarder YA está registrada como CxP, y de
    ella cuelgan el lock y el anti-duplicado de `compras_contab.crear_compra`. Esa FK es
    `ON DELETE SET NULL`, así que BORRAR una fila de acá **desengancha la CxP en silencio**
    y el mismo gasto se puede volver a cargar (Σ CxP duplicada, reproducido en
    `tests/test_llave_gasto_estable.py`).

    Por eso la identidad se define por la LLAVE NATURAL `(pricing_id, tipo)` y el
    guardado hace UPSERT sobre ella (jamás delete + re-insert). La llave es única de
    verdad, no por convención: los DOS únicos escritores (`integration.seed_gastos` y el
    PUT del router) recorren `service.GASTOS_CATALOGO`, que son 6 tipos FIJOS, y el PUT
    colapsa el payload del cliente por tipo → no puede haber dos líneas "Otros".
    El UniqueConstraint de abajo lo deja declarado en la BD (migración aditiva en
    `init_db.py`) para que el invariante no dependa de que nadie lo rompa desde el código.
    """
    __tablename__ = "emb_pricing_gasto"
    __table_args__ = (
        UniqueConstraint("pricing_id", "tipo", name="uq_emb_pricing_gasto_tipo"),
    )

    id = Column(Integer, primary_key=True, index=True)
    pricing_id = Column(Integer, ForeignKey("emb_pricing.id", ondelete="CASCADE"), index=True)

    tipo = Column(String(30), nullable=False)   # desconsolidacion|almacenaje|agencia|arancel|otros|iva_importacion
    glosa = Column(String(120), nullable=False)
    monto_neto = Column(Numeric(16, 2), default=0)
    iva = Column(Numeric(16, 2), default=0)
    capitaliza = Column(Boolean, default=True)  # True → su neto prorratea el landed
    nro_factura = Column(String(100), nullable=True)
    fecha_factura = Column(String(30), nullable=True)   # fecha de la factura del gasto (ISO YYYY-MM-DD)
    banco = Column(String(100), nullable=True)          # banco con que se paga el gasto
    orden = Column(Integer, default=0)

    pricing = relationship("EmbarquePricing", back_populates="gastos")


class EmbarquePricingItem(Base):
    """Snapshot del costo landed por ítem (se persiste al calcular/cerrar)."""
    __tablename__ = "emb_pricing_item"

    id = Column(Integer, primary_key=True, index=True)
    pricing_id = Column(Integer, ForeignKey("emb_pricing.id", ondelete="CASCADE"), index=True)
    # SET NULL hacia tablas de Logística/Ventas: borrar un ítem allá no se bloquea
    # por el snapshot del pricing (solo pierde el vínculo).
    embarque_item_id = Column(Integer, ForeignKey("embarque_items.id", ondelete="SET NULL"), nullable=True, index=True)
    item_cotizacion_id = Column(Integer, ForeignKey("items_cotizacion.id", ondelete="SET NULL"), nullable=True, index=True)

    numero_parte = Column(String(100), nullable=True)
    descripcion = Column(String(500), nullable=True)
    moneda = Column(String(10), default="USD")

    cantidad = Column(Numeric(14, 4), default=0)
    peso_unit_lbs = Column(Numeric(14, 4), default=0)
    peso_total_lbs = Column(Numeric(16, 4), default=0)
    # Origen del peso usado en el prorrateo del flete: cotizacion(auto) | manual.
    # Espejo de fob_origen: si el peso de la cotización vino mal, Contabilidad lo
    # corrige a mano y el flete se re-prorratea. 'auto' = se lee de la cotización.
    peso_origen = Column(String(20), default="auto")

    # FOB unitario en moneda extranjera + de dónde salió (factura|cotizacion|manual)
    fob_unit = Column(Numeric(16, 4), default=0)
    fob_origen = Column(String(20), default="manual")
    tc_valor = Column(Numeric(14, 4), default=0)

    fob_total = Column(Numeric(16, 4), default=0)
    fob_clp = Column(Numeric(16, 2), default=0)
    shipping_clp = Column(Numeric(16, 2), default=0)
    cif_clp = Column(Numeric(16, 2), default=0)
    gastos_clp = Column(Numeric(16, 2), default=0)
    costo_total_clp = Column(Numeric(16, 2), default=0)
    costo_unit_clp = Column(Numeric(16, 2), default=0)

    pricing = relationship("EmbarquePricing", back_populates="items")
