"""Tablas del módulo Contabilidad MonzaParts (Ventas + Facturas/Cobranzas/Factoring).

4 tablas NUEVAS y aditivas. SOLO MonzaParts. Espejo de las cont_* de Grupo AM, pero
ligadas a monza_cotizaciones. La factura es un SNAPSHOT inmutable: además del
cotizacion_id guarda copia de número de cotización, cliente, RUT, guía y de cada línea
(número de parte, descripción), para que el documento sobreviva aunque cambie/borre la
cotización de origen. El dinero va en Numeric (decimal exacto), no en Float.

  monza_cont_factura_cliente       → factura a cliente (cuentas por cobrar)
  monza_cont_factura_cliente_item  → líneas de la factura
  monza_cont_cobranza              → pagos reales del cliente (+ adelanto/retención factoring)
  monza_cont_factoring             → cesión de la factura a un factor (1:1)

Las tablas se crean solas con Base.metadata.create_all() (este módulo se importa desde
main.py, que registra los modelos antes del create_all).
"""
from sqlalchemy import (
    Column, Integer, String, Numeric, Text, Date, DateTime, ForeignKey, UniqueConstraint,
    text,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from database import Base


class MonzaContFacturaCliente(Base):
    """Factura a cliente de MonzaParts (cuentas por cobrar)."""
    __tablename__ = "monza_cont_factura_cliente"
    __table_args__ = (
        # Folio único (los NULL/borradores sin folio no colisionan en MySQL).
        UniqueConstraint("numero_factura", name="uq_monza_cont_factura_folio"),
        {"mysql_engine": "InnoDB"},
    )

    id = Column(Integer, primary_key=True, index=True)
    # Vínculo a la venta (cotización). SET NULL: si Ventas borra la cotización, la
    # factura conserva sus snapshots y no se cae (documento contable inmutable).
    cotizacion_id = Column(
        Integer, ForeignKey("monza_cotizaciones.id", ondelete="SET NULL"),
        index=True, nullable=True,
    )
    # Snapshots de identidad de la venta (sobreviven a cambios en la cotización)
    numero_cotizacion = Column(String(20), nullable=True)
    cliente_nombre = Column(String(200), nullable=True)
    rut_cliente = Column(String(50), nullable=True)
    # Guía de despacho facturada (snapshot, sin FK cross-módulo: los despachos Monza
    # no usan FKs y pueden recrearse).
    despacho_id = Column(Integer, index=True, nullable=True)
    numero_guia = Column(String(100), nullable=True)

    numero_factura = Column(String(50), nullable=True, index=True)  # folio SII
    tipo_doc = Column(String(20), default="factura")               # factura|boleta|nota_credito
    # Factura de ANTICIPO (Fase 7, vía B): respalda ante el SII un adelanto que el
    # cliente pagó ANTES de que llegara la mercadería. Es la ÚNICA excepción a la regla
    # rectora "toda factura nace de una guía firmada" (por eso su despacho_id queda NULL
    # a la fuerza). Su neto se descuenta después, con una línea NEGATIVA, de las facturas
    # del despacho real → Σ brutos de la venta == total de la venta y el cliente no paga
    # dos veces.
    # server_default + nullable=False (no solo `default=0`, que es un default de PYTHON):
    # con el default de Python, create_all emitía la columna SIN DEFAULT y NULLABLE, así
    # que una BD FRESCA quedaba con un esquema distinto al de la BD migrada (donde el
    # ALTER de init_db sí pone DEFAULT 0). Un INSERT que no nombrara la columna dejaba
    # NULL, y con NULL el `ORDER BY es_anticipo DESC` invierte el FIFO del adelanto
    # (MySQL manda los NULL al final en DESC) → la plata entraba a la factura equivocada.
    es_anticipo = Column(Integer, nullable=False, default=0,
                         server_default=text("0"))                 # 0 normal | 1 factura de anticipo
    # CANAL de la factura (regla 2026-08-06, gate de la guía firmada): 1 = RETIRO EN
    # OFICINA (sin_guia). Sin esta marca, las líneas sin despacho_item_id no tienen
    # canal y el neteo guía↔retiro descuenta la misma mercadería DOS veces (hallazgo
    # HIGH del multienjambre: retiro primero + guía después dejaba unidades entregadas
    # infacturables para siempre). Las facturas históricas quedan en 0 ("consumo del
    # canal guía"): atribución imperfecta pero con el techo global vendido−facturado
    # nada queda atrapado ni se sobre-factura. Mismo patrón server_default que
    # es_anticipo (ver la nota de arriba: el default de Python no basta).
    sin_guia = Column(Integer, nullable=False, default=0,
                      server_default=text("0"))                    # 0 canal guía | 1 retiro en oficina
    fecha_emision = Column(Date, nullable=True, index=True)         # índice: filtros por período (KPIs/listados)
    condicion_pago = Column(String(100), nullable=True)
    plazo_dias = Column(Integer, nullable=True)
    fecha_vencimiento = Column(Date, nullable=True)

    monto_neto = Column(Numeric(16, 2), default=0)
    iva = Column(Numeric(16, 2), default=0)
    monto_bruto = Column(Numeric(16, 2), default=0)
    monto_pagado = Column(Numeric(16, 2), default=0)
    saldo = Column(Numeric(16, 2), default=0)
    estado_pago = Column(String(20), default="por_cobrar", index=True)  # por_cobrar|parcial|pagada|vencida|factorizada

    observaciones = Column(Text, nullable=True)
    usuario_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # foreign_keys EXPLÍCITO: la línea tiene DOS FKs a esta misma tabla (factura_id, la
    # dueña, y anticipo_factura_id, la factura de anticipo que descuenta) y SQLAlchemy no
    # puede inferir el join → sin esto el backend ni siquiera arranca
    # (AmbiguousForeignKeysError al configurar los mappers).
    items = relationship(
        "MonzaContFacturaClienteItem", back_populates="factura",
        foreign_keys="MonzaContFacturaClienteItem.factura_id",
        cascade="all, delete-orphan",
    )
    cobranzas = relationship(
        "MonzaContCobranza", back_populates="factura",
        cascade="all, delete-orphan",
    )
    factoring = relationship(
        "MonzaContFactoring", back_populates="factura",
        uselist=False, cascade="all, delete-orphan",
    )


class MonzaContFacturaClienteItem(Base):
    """Línea de una factura (snapshot del ítem facturado)."""
    __tablename__ = "monza_cont_factura_cliente_item"
    __table_args__ = ({"mysql_engine": "InnoDB"},)

    id = Column(Integer, primary_key=True, index=True)
    factura_id = Column(
        Integer, ForeignKey("monza_cont_factura_cliente.id", ondelete="CASCADE"),
        index=True, nullable=False,
    )
    # IDs de origen como SNAPSHOT (sin FK: permiten el control anti-doble-facturación
    # sin acoplarse al ciclo de vida de cotización/despacho).
    item_cotizacion_id = Column(Integer, index=True, nullable=True)
    despacho_item_id = Column(Integer, index=True, nullable=True)
    # Línea de DESCUENTO por anticipo facturado (total_neto NEGATIVO): apunta a la
    # factura de anticipo que está descontando. El "anticipo pendiente de descontar" NO
    # se guarda, se DERIVA: neto de la factura de anticipo − Σ(−total_neto) de las líneas
    # vivas que la referencian. Por eso borrar la factura final devuelve el cupo solo
    # (cascade), sin código de reversión que pueda desincronizarse.
    # A diferencia del resto de los IDs de origen de esta tabla, este SÍ lleva FK real y
    # SIN ondelete a propósito: la base de datos bloquea borrar una factura de anticipo ya
    # descontada, como segundo cinturón del 409 explícito de eliminar_factura.
    anticipo_factura_id = Column(
        Integer, ForeignKey("monza_cont_factura_cliente.id"),
        index=True, nullable=True,
    )

    numero_parte = Column(String(100), nullable=True)
    descripcion = Column(String(500), nullable=True)
    cantidad = Column(Numeric(14, 4), default=0)
    precio_unit_neto = Column(Numeric(16, 2), default=0)
    total_neto = Column(Numeric(16, 2), default=0)

    # foreign_keys explícito también en este lado (ver el comentario de `items`): con dos
    # FKs a monza_cont_factura_cliente hay que decirle a SQLAlchemy cuál es la dueña.
    factura = relationship("MonzaContFacturaCliente", back_populates="items",
                           foreign_keys=[factura_id])


class MonzaContCobranza(Base):
    """Pago del cliente. Las cobranzas de factoring (adelanto/retención) también se
    registran aquí, marcadas con medio 'factoring_*'."""
    __tablename__ = "monza_cont_cobranza"
    __table_args__ = ({"mysql_engine": "InnoDB"},)

    id = Column(Integer, primary_key=True, index=True)
    factura_id = Column(
        Integer, ForeignKey("monza_cont_factura_cliente.id", ondelete="CASCADE"),
        index=True, nullable=False,
    )
    fecha = Column(Date, nullable=True)
    monto = Column(Numeric(16, 2), default=0)
    medio = Column(String(40), default="transferencia")  # transferencia|cheque|efectivo|adelanto|factoring_adelanto|factoring_retencion ('adelanto' = aplicación automática del adelanto verificado)
    banco = Column(String(100), nullable=True)
    numero_operacion = Column(String(100), nullable=True)
    observaciones = Column(Text, nullable=True)
    usuario_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    factura = relationship("MonzaContFacturaCliente", back_populates="cobranzas")


class MonzaContAdelanto(Base):
    """Verificación por Contabilidad del adelanto (ej. 50%) que Comercial informó al cerrar
    la venta. 1 por cotización. Su EXISTENCIA = pago verificado. El monto verificado se
    aplica como cobranza al emitir la(s) factura(s) de esa venta (campo monto_aplicado para
    soportar facturación parcial sin aplicar de más).

    `estado` (informado|aprobado|anulado, espejo de ContAdelanto de Grupo AM): la fila
    ANULADA se conserva para trazabilidad y deja de contar como plata comprometida. No es
    una columna de adorno: sin ella un adelanto verificado por error era IRREVERSIBLE —
    los dos únicos escritores eran creación/upsert y `adelanto_verificado` solo se escribía
    = 1—, así que Abastecimiento seguía comprando contra un 50% que nunca llegó y el 409
    de monza_router_cotizaciones.py («Revierta el adelanto en Contabilidad/Tesorería
    primero») era una instrucción sin implementación.
    Como el UNIQUE deja UN adelanto por venta, la fila anulada NO se reemplaza: se REUSA
    (service.reactivar_adelanto) cuando el cliente sí deposita."""
    __tablename__ = "monza_cont_adelanto"
    __table_args__ = (
        UniqueConstraint("cotizacion_id", name="uq_monza_cont_adelanto_cotizacion"),
        {"mysql_engine": "InnoDB"},
    )

    id = Column(Integer, primary_key=True, index=True)
    cotizacion_id = Column(
        Integer, ForeignKey("monza_cotizaciones.id", ondelete="CASCADE"),
        index=True, nullable=False,
    )
    # server_default + nullable=False (no solo `default=`, que es un default de PYTHON):
    # con el default de Python, create_all emitiría la columna NULLABLE y SIN DEFAULT, así
    # que una BD FRESCA quedaría con otro esquema que la migrada y un INSERT que no nombre
    # la columna dejaría NULL. El lector tolera el NULL como 'aprobado' (service.
    # estado_de_adelanto) porque leerlo como 'anulado' apagaría el cortafuego de
    # Abastecimiento de todas las ventas viejas — misma lección que es_anticipo.
    estado = Column(String(20), nullable=False, default="aprobado",
                    server_default=text("'aprobado'"))  # informado|aprobado|anulado
    monto = Column(Numeric(16, 2), default=0)          # monto verificado del adelanto
    monto_aplicado = Column(Numeric(16, 2), default=0)  # cuánto ya se aplicó a facturas
    fecha_pago = Column(Date, nullable=True)
    banco = Column(String(100), nullable=True)
    numero_operacion = Column(String(100), nullable=True)
    observaciones = Column(Text, nullable=True)
    usuario_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    fecha_verificacion = Column(DateTime(timezone=True), server_default=func.now())
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class MonzaContFactoring(Base):
    """Cesión de la factura a un factor (1 por factura)."""
    __tablename__ = "monza_cont_factoring"
    __table_args__ = ({"mysql_engine": "InnoDB"},)

    id = Column(Integer, primary_key=True, index=True)
    factura_id = Column(
        Integer, ForeignKey("monza_cont_factura_cliente.id", ondelete="CASCADE"),
        unique=True, index=True, nullable=False,
    )
    empresa_factoring = Column(String(150), nullable=True)
    id_operacion = Column(String(100), nullable=True)
    fecha_operacion = Column(Date, nullable=True)
    monto_adelantado = Column(Numeric(16, 2), default=0)
    costo_factoring = Column(Numeric(16, 2), default=0)
    retencion = Column(Numeric(16, 2), default=0)
    banco = Column(String(100), nullable=True)
    estado = Column(String(20), default="vigente")  # vigente|liquidada
    fecha_liquidacion = Column(Date, nullable=True)
    usuario_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    usuario_liquidacion_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    observaciones = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    factura = relationship("MonzaContFacturaCliente", back_populates="factoring")
