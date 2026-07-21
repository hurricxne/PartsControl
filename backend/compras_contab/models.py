"""Tablas del módulo Compras / Cuentas por Pagar (AP) — aisladas y aditivas.

Tablas NUEVAS (no tocan ninguna existente):

  cont_plan_cuenta    → plan de cuentas NIIF (catálogo, importado del Excel del dueño).
  cont_compra         → 1 fila por compra/gasto (clasificación, cuenta contable imputada,
                        montos, condición de pago y estado de pago DERIVADO de sus egresos).
  cont_egreso         → Comprobante de Egreso: UNA salida real de dinero (que puede pagar
                        varias compras) y unidad que se concilia con el banco.
  cont_egreso_detalle → asignación egreso → compra (cuánto de esa salida pagó cada compra).

(El pago de una compra ya NO es una fila propia: son sus cont_egreso_detalle.)
Se crean con `compras_contab/init_db.py` (standalone) o con el `Base.metadata.create_all()`
de arranque una vez cableado el módulo en main.py.
"""
from sqlalchemy import (
    Column, Integer, String, Numeric, Boolean, Text, Date, DateTime,
    ForeignKey, UniqueConstraint, Index, Computed,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from database import Base


class ContCompra(Base):
    """Compra/gasto: unidad de Cuentas por Pagar (espejo de ContFacturaCliente)."""
    __tablename__ = "cont_compra"
    # Documento único por (empresa, proveedor, N° documento) SOLO entre compras
    # ACTIVAS: la unicidad se hace sobre `numero_documento_activo` (columna generada
    # que vale NULL cuando la compra está anulada). Así, tras anular una compra se
    # puede volver a registrar el mismo N° de documento (caso real: corregir una
    # carga errónea). Los NULL no colisionan en MySQL → gastos sin documento/RUT
    # ni compras anuladas chocan entre sí.
    __table_args__ = (
        UniqueConstraint("empresa", "proveedor_rut", "numero_documento_activo",
                         name="uq_cont_compra_empresa_prov_doc"),
        # Índices pensados para el listado/KPIs filtrados por empresa (el filtro más usado).
        Index("ix_cont_compra_empresa_anulado_fecha", "empresa", "anulado", "fecha"),
        Index("ix_cont_compra_empresa_tipo", "empresa", "tipo_gasto"),
        Index("ix_cont_compra_estado_pago", "estado_pago"),
        Index("ix_cont_compra_fecha_venc", "fecha_vencimiento"),
        # InnoDB explícito: SELECT ... FOR UPDATE (locks anti doble-pago) lo requiere.
        {"mysql_engine": "InnoDB"},
    )

    id = Column(Integer, primary_key=True, index=True)
    empresa = Column(String(50), nullable=False, server_default="mineria")

    # ── Clasificación ──
    origen = Column(String(20), default="MANUAL")            # MANUAL | EMBARQUE
    tipo_gasto = Column(String(30), nullable=False, server_default="gasto_operacional")
    # cogs | gasto_operacional | gasto_no_operacional | otros
    categoria = Column(String(80), nullable=True)            # categoría fina (texto libre)
    # Imputación contable NIIF: cuenta del NETO (el IVA va automático a 1.4.01/1.4.02).
    cuenta_contable_id = Column(Integer, ForeignKey("cont_plan_cuenta.id", ondelete="SET NULL"), nullable=True, index=True)
    es_anticipo = Column(Boolean, default=False)             # SWIFT anticipo a prov. extranjero (NIC 21, no monetario)

    # ── Acreedor / proveedor ──
    proveedor_id = Column(Integer, ForeignKey("proveedores.id", ondelete="SET NULL"), nullable=True, index=True)
    acreedor = Column(String(255), nullable=True)            # snapshot del nombre
    proveedor_rut = Column(String(50), nullable=True, index=True)

    # ── Identidad del documento ──
    fecha = Column(Date, nullable=True)                      # fecha de la compra/factura
    referencia = Column(String(120), nullable=True)         # ej "Emb 1"
    descripcion = Column(String(500), nullable=True)
    numero_documento = Column(String(100), nullable=True, index=True)
    tipo_doc = Column(String(20), default="factura")        # factura|boleta|nota|recibo|sin_documento

    # ── Montos (congelados al registrar) ──
    moneda = Column(String(10), default="CLP")              # CLP | USD | EUR
    tc = Column(Numeric(14, 4), default=1)                  # tipo de cambio a CLP
    monto_neto = Column(Numeric(16, 2), default=0)          # en `moneda`
    iva = Column(Numeric(16, 2), default=0)                 # en `moneda`
    monto_total = Column(Numeric(16, 2), default=0)         # neto+iva en `moneda`
    monto_total_clp = Column(Numeric(16, 2), default=0)     # base de AP (= total × tc)

    # ── Condición de pago / vencimiento ──
    condicion_pago = Column(String(20), default="credito")  # contado | credito
    plazo_dias = Column(Integer, nullable=True)
    fecha_vencimiento = Column(Date, nullable=True)

    # ── Estado de pago (derivado, igual que AR) ──
    estado_pago = Column(String(20), default="pendiente")   # pendiente|parcial|pagado|vencido|anulado
    monto_pagado_clp = Column(Numeric(16, 2), default=0)
    saldo_clp = Column(Numeric(16, 2), default=0)

    # ── Anulación (soft, como la columna ANULADO del Excel) ──
    anulado = Column(Boolean, default=False, index=True)
    motivo_anulacion = Column(String(255), nullable=True)

    # Columna generada para el unique "solo activas": = numero_documento si la compra
    # NO está anulada, NULL si lo está. (No se inserta nunca; la calcula MySQL.)
    numero_documento_activo = Column(
        String(100),
        Computed("CASE WHEN anulado = 1 THEN NULL ELSE numero_documento END", persisted=True),
    )

    # ── Punteros suaves a embarques (enlace/dedup futuro; no tocan esos módulos) ──
    embarque_id = Column(Integer, ForeignKey("embarques.id", ondelete="SET NULL"), nullable=True, index=True)
    emb_pricing_gasto_id = Column(Integer, ForeignKey("emb_pricing_gasto.id", ondelete="SET NULL"), nullable=True, index=True)
    factura_proveedor_id = Column(Integer, ForeignKey("facturas_proveedor.id", ondelete="SET NULL"), nullable=True, index=True)
    # FK suave a la OC-Proveedor (compra nacional): solo pista/filtro de cabecera; el
    # costo real por ítem vive en cont_compra_item. SET NULL: borrar la OC no borra la
    # compra. La columna la crea compras_contab/init_db (create_all no altera tablas).
    oc_proveedor_id = Column(Integer, ForeignKey("oc_proveedor.id", ondelete="SET NULL"), nullable=True, index=True)

    observaciones = Column(Text, nullable=True)
    usuario_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    egreso_detalles = relationship("ContEgresoDetalle", back_populates="compra",
                                   cascade="all, delete-orphan")
    cuenta = relationship("ContPlanCuenta", foreign_keys=[cuenta_contable_id])
    items = relationship("ContCompraItem", back_populates="compra",
                         cascade="all, delete-orphan")


class ContEgreso(Base):
    """Comprobante de Egreso de tesorería: UNA salida real de dinero (transferencia,
    cheque, SWIFT, tarjeta) que puede pagar VARIAS compras (ver detalles). Es el flujo
    de salida NIC 7 y la unidad que se concilia con el banco. Reemplaza el antiguo
    cont_compra_pago (modelo tipo Defontana/Nubox: cabecera + detalle)."""
    __tablename__ = "cont_egreso"
    __table_args__ = (
        Index("ix_cont_egreso_empresa_fecha", "empresa", "fecha"),
        Index("ix_cont_egreso_conciliado", "conciliado"),
        {"mysql_engine": "InnoDB"},
    )
    id = Column(Integer, primary_key=True, index=True)
    empresa = Column(String(50), nullable=False, server_default="mineria")
    fecha = Column(Date, nullable=True)                     # fecha del pago
    medio = Column(String(30), default="transferencia")    # transferencia|cheque|efectivo|tarjeta
    cuenta_origen_id = Column(Integer, ForeignKey("cont_plan_cuenta.id", ondelete="SET NULL"), nullable=True, index=True)  # banco/caja
    banco = Column(String(100), nullable=True)
    numero_operacion = Column(String(100), nullable=True)
    beneficiario = Column(String(255), nullable=True)
    beneficiario_rut = Column(String(50), nullable=True)
    glosa = Column(String(500), nullable=True)
    moneda = Column(String(10), default="CLP")
    tc = Column(Numeric(14, 4), default=1)
    monto_origen = Column(Numeric(16, 2), default=0)        # monto en la moneda del pago
    monto_total_clp = Column(Numeric(16, 2), default=0)     # = Σ detalle.monto_clp (salida real del banco)
    # ── Conciliación bancaria (la cartola es la fuente de verdad) ──
    conciliado = Column(Boolean, default=False)  # idx via __table_args__ (evita duplicado en MySQL)
    conciliado_at = Column(DateTime(timezone=True), nullable=True)
    fecha_mov_bancario = Column(Date, nullable=True)
    referencia_bancaria = Column(String(120), nullable=True)
    observaciones = Column(Text, nullable=True)
    usuario_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    detalles = relationship("ContEgresoDetalle", back_populates="egreso",
                            cascade="all, delete-orphan")
    cuenta_origen = relationship("ContPlanCuenta", foreign_keys=[cuenta_origen_id])


class ContEgresoDetalle(Base):
    """Asignación de un egreso a una compra: cuánto de esa salida pagó esta compra.
    Guarda tc/monto en la moneda del pago para la diferencia de cambio (NIC 21)."""
    __tablename__ = "cont_egreso_detalle"
    __table_args__ = (
        Index("ix_cont_egreso_det_compra", "compra_id"),
        {"mysql_engine": "InnoDB"},
    )
    id = Column(Integer, primary_key=True, index=True)
    egreso_id = Column(Integer, ForeignKey("cont_egreso.id", ondelete="CASCADE"), index=True)
    compra_id = Column(Integer, ForeignKey("cont_compra.id", ondelete="CASCADE"), index=True)
    monto_clp = Column(Numeric(16, 2), default=0)
    tc_aplicado = Column(Numeric(14, 4), default=1)
    monto_origen = Column(Numeric(16, 2), default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    egreso = relationship("ContEgreso", back_populates="detalles")
    compra = relationship("ContCompra", back_populates="egreso_detalles")


class ContCompraItem(Base):
    """Costo por ítem de una compra NACIONAL (la factura ES el costo; sin flete-por-peso
    ni gastos-por-CIF). Espejo conceptual de emb_pricing_item, SIN prorrateo. Costo =
    NETO de la línea en CLP; el IVA es crédito fiscal recuperable → NO capitaliza
    (distinto del iva_importacion internacional). La tabla la crea compras_contab/init_db."""
    __tablename__ = "cont_compra_item"
    __table_args__ = (
        Index("ix_cont_compra_item_compra", "compra_id"),
        Index("ix_cont_compra_item_itemcot", "item_cotizacion_id"),
        {"mysql_engine": "InnoDB"},
    )

    id = Column(Integer, primary_key=True, index=True)
    compra_id = Column(Integer, ForeignKey("cont_compra.id", ondelete="CASCADE"), nullable=False)
    # Clave de costeo: liga la línea de la factura al repuesto vendido.
    item_cotizacion_id = Column(Integer, ForeignKey("items_cotizacion.id", ondelete="SET NULL"), nullable=True)
    oc_proveedor_id = Column(Integer, nullable=True)                # trazabilidad (pista, sin FK dura)
    oc_proveedor_item_id = Column(Integer, ForeignKey("oc_proveedor_items.id", ondelete="SET NULL"), nullable=True)
    numero_parte = Column(String(100), nullable=True)              # snapshot
    descripcion = Column(String(500), nullable=True)               # snapshot
    cantidad = Column(Numeric(12, 4), default=0)
    precio_unit = Column(Numeric(16, 4), default=0)                # neto unit en moneda factura (auditoría)
    costo_unit_clp = Column(Numeric(16, 2), default=0)             # = precio_unit × compra.tc
    costo_total_clp = Column(Numeric(16, 2), default=0)            # = cantidad × costo_unit_clp (BASE rentabilidad futura)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    compra = relationship("ContCompra", back_populates="items")


class ContPlanCuenta(Base):
    """Plan de cuentas NIIF (catálogo). Se importa 1:1 del Excel del dueño
    (hoja 'Plan de cuentas'); NO se inventan cuentas. Cada compra se imputa a una de
    estas cuentas. Trae los metadatos de NIC 7 (actividad_flujo, efectivo_equiv) y
    NIC 21 (monetaria, moneda) que el plan ya define."""
    __tablename__ = "cont_plan_cuenta"
    __table_args__ = (
        UniqueConstraint("empresa", "codigo", name="uq_cont_plan_cuenta_emp_cod"),
        Index("ix_cont_plan_cuenta_empresa", "empresa"),
        {"mysql_engine": "InnoDB"},
    )
    id = Column(Integer, primary_key=True, index=True)
    empresa = Column(String(50), nullable=False, server_default="mineria")
    codigo = Column(String(20), nullable=False)          # ej "1.4.02"
    nombre = Column(String(200), nullable=False)
    clase = Column(String(40), nullable=True)            # ACTIVO|PASIVO|PATRIMONIO|INGRESO|COSTO|GASTO
    grupo = Column(String(120), nullable=True)           # "Efectivo y equivalentes", ...
    actividad_flujo = Column(String(20), nullable=True)  # OPERACION|INVERSION|FINANCIAMIENTO|NA (NIC 7)
    efectivo_equiv = Column(Boolean, default=False)      # NIC 7
    monetaria = Column(Boolean, default=False)           # NIC 21 (revaloriza a cierre)
    moneda = Column(String(10), default="CLP")
    requiere_auxiliar = Column(Boolean, default=False)   # exige RUT/auxiliar al imputar
    cod_f29 = Column(String(20), nullable=True)
    imputable = Column(Boolean, default=True, index=True)  # cuentas hoja (no de mayor/título)
    activa = Column(Boolean, default=True, index=True)
    orden = Column(Integer, default=0)
    notas = Column(Text, nullable=True)
