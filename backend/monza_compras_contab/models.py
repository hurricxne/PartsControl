"""Tablas del módulo Compras / Cuentas por Pagar de MonzaParts — aisladas y aditivas.

Espejo de las cont_* de Grupo AM (backend/compras_contab/models.py), en tablas
monza_cont_* propias (la separación entre empresas en MonzaParts es POR TABLA, no por
columna — mismo criterio que el resto de tablas monza_* del sistema; el candado de
empresa vive en el router).

  monza_cont_plan_cuenta    → plan de cuentas NIIF (catálogo, importado del Excel).
  monza_cont_compra         → 1 fila por compra/gasto (clasificación, cuenta imputada,
                              montos, condición de pago y estado DERIVADO de sus egresos).
  monza_cont_egreso         → Comprobante de Egreso: UNA salida real de dinero (puede
                              pagar varias compras) y unidad que se concilia con el banco.
  monza_cont_egreso_detalle → asignación egreso → compra (cuánto pagó a cada compra).

Se crean con `monza_compras_contab/init_db.py` o con el create_all de main.py.
Dinero en Numeric (decimal exacto). InnoDB explícito (SELECT ... FOR UPDATE).
"""
from sqlalchemy import (
    Column, Integer, String, Numeric, Boolean, Text, Date, DateTime,
    ForeignKey, UniqueConstraint, Index, Computed,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from database import Base


class MonzaContPlanCuenta(Base):
    """Plan de cuentas NIIF (catálogo propio de MonzaParts). Se importa 1:1 del Excel
    del dueño (hoja 'Plan de cuentas'); NO se inventan cuentas. Trae los metadatos de
    NIC 7 (actividad_flujo, efectivo_equiv) y NIC 21 (monetaria, moneda)."""
    __tablename__ = "monza_cont_plan_cuenta"
    __table_args__ = (
        UniqueConstraint("codigo", name="uq_monza_cont_plan_cuenta_cod"),
        {"mysql_engine": "InnoDB"},
    )
    id = Column(Integer, primary_key=True, index=True)
    codigo = Column(String(20), nullable=False)          # ej "1.4.02"
    nombre = Column(String(200), nullable=False)
    clase = Column(String(40), nullable=True)            # ACTIVO|PASIVO|PATRIMONIO|INGRESO|COSTO|GASTO
    grupo = Column(String(120), nullable=True)
    actividad_flujo = Column(String(20), nullable=True)  # OPERACION|INVERSION|FINANCIAMIENTO|NA (NIC 7)
    efectivo_equiv = Column(Boolean, default=False)      # NIC 7
    monetaria = Column(Boolean, default=False)           # NIC 21 (revaloriza a cierre)
    moneda = Column(String(10), default="CLP")
    requiere_auxiliar = Column(Boolean, default=False)
    cod_f29 = Column(String(20), nullable=True)
    imputable = Column(Boolean, default=True, index=True)  # cuentas hoja (no de mayor/título)
    activa = Column(Boolean, default=True, index=True)
    orden = Column(Integer, default=0)
    notas = Column(Text, nullable=True)


class MonzaContCompra(Base):
    """Compra/gasto: unidad de Cuentas por Pagar de MonzaParts."""
    __tablename__ = "monza_cont_compra"
    # Documento único por (proveedor, N° documento) SOLO entre compras ACTIVAS: la
    # unicidad usa `numero_documento_activo` (columna generada que vale NULL si la
    # compra está anulada) → tras anular se puede re-registrar el mismo documento.
    __table_args__ = (
        UniqueConstraint("proveedor_rut", "numero_documento_activo",
                         name="uq_monza_cont_compra_prov_doc"),
        Index("ix_monza_cont_compra_anulado_fecha", "anulado", "fecha"),
        Index("ix_monza_cont_compra_tipo", "tipo_gasto"),
        Index("ix_monza_cont_compra_estado_pago", "estado_pago"),
        Index("ix_monza_cont_compra_fecha_venc", "fecha_vencimiento"),
        {"mysql_engine": "InnoDB"},
    )

    id = Column(Integer, primary_key=True, index=True)

    # ── Clasificación ──
    origen = Column(String(20), default="MANUAL")            # MANUAL | EMBARQUE
    tipo_gasto = Column(String(30), nullable=False, server_default="gasto_operacional")
    # cogs | gasto_operacional | gasto_no_operacional | otros
    categoria = Column(String(80), nullable=True)
    # Imputación contable NIIF: cuenta del NETO (el IVA va automático a 1.4.01/1.4.02).
    cuenta_contable_id = Column(Integer, ForeignKey("monza_cont_plan_cuenta.id", ondelete="SET NULL"), nullable=True, index=True)
    es_anticipo = Column(Boolean, default=False)             # anticipo a prov. extranjero (NIC 21, no monetario)

    # ── Acreedor / proveedor ──
    proveedor_id = Column(Integer, ForeignKey("monza_proveedores.id", ondelete="SET NULL"), nullable=True, index=True)
    acreedor = Column(String(255), nullable=True)            # snapshot del nombre
    proveedor_rut = Column(String(50), nullable=True, index=True)

    # ── Identidad del documento ──
    fecha = Column(Date, nullable=True)
    referencia = Column(String(120), nullable=True)          # ej "EMB-2026-001"
    descripcion = Column(String(500), nullable=True)
    numero_documento = Column(String(100), nullable=True, index=True)
    tipo_doc = Column(String(20), default="factura")         # factura|boleta|nota|recibo|sin_documento

    # ── Montos (congelados al registrar) ──
    moneda = Column(String(10), default="CLP")               # CLP | USD | EUR
    tc = Column(Numeric(14, 4), default=1)
    monto_neto = Column(Numeric(16, 2), default=0)           # en `moneda`
    iva = Column(Numeric(16, 2), default=0)
    monto_total = Column(Numeric(16, 2), default=0)          # neto+iva en `moneda`
    monto_total_clp = Column(Numeric(16, 2), default=0)      # base de AP (= total × tc)

    # ── Condición de pago / vencimiento ──
    condicion_pago = Column(String(20), default="credito")   # contado | credito
    plazo_dias = Column(Integer, nullable=True)
    fecha_vencimiento = Column(Date, nullable=True)

    # ── Estado de pago (derivado de sus egresos) ──
    estado_pago = Column(String(20), default="pendiente")    # pendiente|parcial|pagado|vencido|anulado
    monto_pagado_clp = Column(Numeric(16, 2), default=0)
    saldo_clp = Column(Numeric(16, 2), default=0)

    # ── Anulación (soft) ──
    anulado = Column(Boolean, default=False, index=True)
    motivo_anulacion = Column(String(255), nullable=True)
    numero_documento_activo = Column(
        String(100),
        Computed("CASE WHEN anulado = 1 THEN NULL ELSE numero_documento END", persisted=True),
    )

    # ── Punteros suaves a embarques Monza (enlace/trazabilidad; no tocan esos módulos) ──
    # SET NULL es deliberado: la compra es un SNAPSHOT con montos propios congelados, así
    # que sigue siendo válida y auditable aunque después se borre el gasto/embarque origen
    # (el puntero solo se pierde como traza; origen queda 'EMBARQUE' como marca histórica).
    embarque_id = Column(Integer, ForeignKey("monza_embarques.id", ondelete="SET NULL"), nullable=True, index=True)
    emb_pricing_gasto_id = Column(Integer, ForeignKey("monza_emb_pricing_gasto.id", ondelete="SET NULL"), nullable=True, index=True)

    observaciones = Column(Text, nullable=True)
    usuario_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    egreso_detalles = relationship("MonzaContEgresoDetalle", back_populates="compra",
                                   cascade="all, delete-orphan")
    cuenta = relationship("MonzaContPlanCuenta", foreign_keys=[cuenta_contable_id])


class MonzaContEgreso(Base):
    """Comprobante de Egreso de tesorería: UNA salida real de dinero (transferencia,
    cheque, SWIFT, tarjeta) que puede pagar VARIAS compras. Es el flujo de salida
    NIC 7 y la unidad que se conciliará con el banco."""
    __tablename__ = "monza_cont_egreso"
    __table_args__ = (
        Index("ix_monza_cont_egreso_fecha", "fecha"),
        Index("ix_monza_cont_egreso_conciliado", "conciliado"),
        {"mysql_engine": "InnoDB"},
    )
    id = Column(Integer, primary_key=True, index=True)
    fecha = Column(Date, nullable=True)                      # fecha del pago
    medio = Column(String(30), default="transferencia")      # transferencia|cheque|efectivo|tarjeta
    cuenta_origen_id = Column(Integer, ForeignKey("monza_cont_plan_cuenta.id", ondelete="SET NULL"), nullable=True, index=True)
    banco = Column(String(100), nullable=True)
    numero_operacion = Column(String(100), nullable=True)
    beneficiario = Column(String(255), nullable=True)
    beneficiario_rut = Column(String(50), nullable=True)
    glosa = Column(String(500), nullable=True)
    moneda = Column(String(10), default="CLP")
    tc = Column(Numeric(14, 4), default=1)
    monto_origen = Column(Numeric(16, 2), default=0)         # monto en la moneda del pago
    monto_total_clp = Column(Numeric(16, 2), default=0)      # = Σ detalle.monto_clp
    # ── Conciliación bancaria (para el futuro módulo Monza; la cartola manda) ──
    # (el índice de `conciliado` es el Index explícito de __table_args__; no usar
    #  index=True acá o el CREATE emite el mismo nombre dos veces)
    conciliado = Column(Boolean, default=False)
    conciliado_at = Column(DateTime(timezone=True), nullable=True)
    fecha_mov_bancario = Column(Date, nullable=True)
    referencia_bancaria = Column(String(120), nullable=True)
    observaciones = Column(Text, nullable=True)
    usuario_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    detalles = relationship("MonzaContEgresoDetalle", back_populates="egreso",
                            cascade="all, delete-orphan")
    cuenta_origen = relationship("MonzaContPlanCuenta", foreign_keys=[cuenta_origen_id])


class MonzaContEgresoDetalle(Base):
    """Asignación de un egreso a una compra: cuánto de esa salida pagó esta compra.
    Guarda tc/monto en la moneda del pago para la diferencia de cambio (NIC 21)."""
    __tablename__ = "monza_cont_egreso_detalle"
    __table_args__ = (
        Index("ix_monza_cont_egr_det_compra", "compra_id"),
        {"mysql_engine": "InnoDB"},
    )
    id = Column(Integer, primary_key=True, index=True)
    egreso_id = Column(Integer, ForeignKey("monza_cont_egreso.id", ondelete="CASCADE"), index=True)
    compra_id = Column(Integer, ForeignKey("monza_cont_compra.id", ondelete="CASCADE"), index=True)
    monto_clp = Column(Numeric(16, 2), default=0)
    tc_aplicado = Column(Numeric(14, 4), default=1)
    monto_origen = Column(Numeric(16, 2), default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    egreso = relationship("MonzaContEgreso", back_populates="detalles")
    compra = relationship("MonzaContCompra", back_populates="egreso_detalles")
