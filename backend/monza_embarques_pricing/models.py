"""Tablas del módulo Embarques Pricing MonzaParts (costo landed).

3 tablas NUEVAS y aditivas. SOLO MonzaParts. Espejo de las emb_pricing_* de Grupo AM,
ligadas a monza_embarques. No tocan ninguna tabla existente. El dinero va en Numeric
(decimal exacto). El snapshot por ítem (monza_emb_pricing_item) congela el costo landed
al calcular/cerrar → auditable.

  monza_emb_pricing       → 1 fila por embarque: TC, flete, estado del pricing.
  monza_emb_pricing_gasto → líneas del bloque GASTOS LOCALES (neto, IVA, factura).
  monza_emb_pricing_item  → snapshot del costo landed por ítem.

Se crean solas con Base.metadata.create_all() (el router se importa en main.py antes
del create_all).
"""
from sqlalchemy import (
    Column, Integer, String, Numeric, Boolean, Text, DateTime, ForeignKey, UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from database import Base


class MonzaEmbPricing(Base):
    """Cabecera del pricing de un embarque (1:1 con monza_embarques.id)."""
    __tablename__ = "monza_emb_pricing"
    __table_args__ = (
        # ON DELETE CASCADE: si Logística borra el embarque, su pricing se va con él.
        UniqueConstraint("embarque_id", name="uq_monza_emb_pricing_embarque"),
        {"mysql_engine": "InnoDB"},
    )

    id = Column(Integer, primary_key=True, index=True)
    embarque_id = Column(
        Integer, ForeignKey("monza_embarques.id", ondelete="CASCADE"),
        unique=True, index=True, nullable=False,
    )

    tipo_embarque = Column(String(20), default="normal")   # normal|courier|baukat|fastmark
    tc_tipo = Column(String(20), default="config")         # manual|config
    tc_valor = Column(Numeric(14, 4), default=0)
    moneda = Column(String(10), default="USD")             # moneda FOB (USD|EUR)

    flete_en_me = Column(Boolean, default=False)           # True: flete prepagado en ME
    shipping_me = Column(Numeric(14, 4), nullable=True)    # flete en ME (si flete_en_me)
    shipping_clp = Column(Numeric(16, 2), default=0)       # flete total CLP (directo o ME × TC)

    estado = Column(String(20), default="borrador")        # borrador|calculado|cerrado
    observaciones = Column(Text, nullable=True)

    calculado_at = Column(DateTime(timezone=True), nullable=True)
    usuario_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    gastos = relationship("MonzaEmbPricingGasto", back_populates="pricing", cascade="all, delete-orphan")
    items = relationship("MonzaEmbPricingItem", back_populates="pricing", cascade="all, delete-orphan")


class MonzaEmbPricingGasto(Base):
    """Línea del bloque GASTOS LOCALES (con desglose tributario).

    IDENTIDAD ESTABLE — leer antes de tocar el guardado
    ---------------------------------------------------
    La PK de esta fila es una LLAVE DE PLATA: `monza_cont_compra.emb_pricing_gasto_id` la
    referencia para saber que la factura del forwarder YA está registrada como CxP, y de
    ella cuelgan el lock y el anti-duplicado de `monza_compras_contab.crear_compra`. Esa FK
    es `ON DELETE SET NULL`, así que BORRAR una fila de acá **desengancha la CxP en
    silencio** y el mismo gasto se puede volver a cargar (Σ CxP duplicada, reproducido en
    `tests/test_llave_gasto_estable.py`). En MonzaParts es peor que en Grupo AM porque el
    botón «Registrar como compra» ya está en pantalla: tras cualquier re-guardado del
    pricing la píldora "registrado" volvía a ser botón y eran DOS CLICS.

    Por eso la identidad se define por la LLAVE NATURAL `(pricing_id, tipo)` y el guardado
    hace UPSERT sobre ella (jamás delete + re-insert). La llave es única de verdad, no por
    convención: los DOS únicos escritores (`integration.seed_gastos` y el PUT del router)
    recorren `service.GASTOS_CATALOGO`, que son 6 tipos FIJOS, y el PUT colapsa el payload
    del cliente por tipo → no puede haber dos líneas "Otros". El UniqueConstraint de abajo
    lo deja declarado en la BD (migración aditiva en `init_db.py`).
    """
    __tablename__ = "monza_emb_pricing_gasto"
    __table_args__ = (
        UniqueConstraint("pricing_id", "tipo", name="uq_monza_emb_pricing_gasto_tipo"),
        {"mysql_engine": "InnoDB"},
    )

    id = Column(Integer, primary_key=True, index=True)
    pricing_id = Column(Integer, ForeignKey("monza_emb_pricing.id", ondelete="CASCADE"), index=True)

    tipo = Column(String(30), nullable=False)   # desconsolidacion|almacenaje|agencia|arancel|otros|iva_importacion
    glosa = Column(String(120), nullable=False)
    monto_neto = Column(Numeric(16, 2), default=0)
    iva = Column(Numeric(16, 2), default=0)
    capitaliza = Column(Boolean, default=True)  # True → su neto prorratea el landed
    nro_factura = Column(String(100), nullable=True)
    fecha_factura = Column(String(30), nullable=True)
    banco = Column(String(100), nullable=True)
    orden = Column(Integer, default=0)

    pricing = relationship("MonzaEmbPricing", back_populates="gastos")


class MonzaEmbPricingItem(Base):
    """Snapshot del costo landed por ítem (se persiste al calcular/cerrar)."""
    __tablename__ = "monza_emb_pricing_item"
    __table_args__ = ({"mysql_engine": "InnoDB"},)

    id = Column(Integer, primary_key=True, index=True)
    pricing_id = Column(Integer, ForeignKey("monza_emb_pricing.id", ondelete="CASCADE"), index=True)
    # IDs de origen como snapshot (sin FK: monza_embarque_items / monza_cotizacion_items
    # se modelan sueltos en Monza; el snapshot NO se acopla a su ciclo de vida). Esto es
    # deliberado: las columnas denormalizadas de abajo (numero_parte, descripcion, cantidad,
    # peso, fob, costo) congelan TODO lo necesario, así el costo sigue siendo auditable
    # aunque luego se borre el ítem de cotización origen (el id queda como mera traza).
    embarque_item_id = Column(Integer, index=True, nullable=True)
    item_cotizacion_id = Column(Integer, index=True, nullable=True)

    numero_parte = Column(String(100), nullable=True)
    descripcion = Column(String(500), nullable=True)
    moneda = Column(String(10), default="USD")

    cantidad = Column(Numeric(14, 4), default=0)
    peso_unit_kg = Column(Numeric(14, 4), default=0)
    peso_total_kg = Column(Numeric(16, 4), default=0)
    # Origen del peso usado en el prorrateo del flete: cotizacion(auto) | manual.
    # Espejo de fob_origen: si el peso_kg de la cotización vino mal, Contabilidad lo
    # corrige a mano y el flete se re-prorratea. 'auto' = se lee de la cotización.
    peso_origen = Column(String(20), default="auto")

    fob_unit = Column(Numeric(16, 4), default=0)
    fob_origen = Column(String(20), default="manual")  # cotizacion|manual|auto
    tc_valor = Column(Numeric(14, 4), default=0)

    fob_total = Column(Numeric(16, 4), default=0)
    fob_clp = Column(Numeric(16, 2), default=0)
    shipping_clp = Column(Numeric(16, 2), default=0)
    cif_clp = Column(Numeric(16, 2), default=0)
    gastos_clp = Column(Numeric(16, 2), default=0)
    costo_total_clp = Column(Numeric(16, 2), default=0)
    costo_unit_clp = Column(Numeric(16, 2), default=0)

    pricing = relationship("MonzaEmbPricing", back_populates="items")
