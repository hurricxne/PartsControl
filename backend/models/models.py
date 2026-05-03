from sqlalchemy import (
    Column, Integer, String, Float, DateTime, ForeignKey, Numeric, Numeric,
    Text, Enum, Boolean
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import enum
from database import Base


class EstadoCotizacion(str, enum.Enum):
    PENDIENTE = "pendiente"
    PROCESANDO = "procesando"
    ESPERANDO_AGENTE = "esperando_agente"
    COMPLETADO = "completado"
    ERROR = "error"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    nombre = Column(String(255), nullable=False)
    hashed_password = Column(String(255), nullable=False)
    is_active = Column(Integer, default=1)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    cotizaciones = relationship("Cotizacion", back_populates="user")


class Cotizacion(Base):
    __tablename__ = "cotizaciones"

    id = Column(Integer, primary_key=True, index=True)
    numero = Column(String(50), nullable=False)
    cliente = Column(String(255))
    rut_cliente = Column(String(50), nullable=True)
    contacto_cliente = Column(String(255), nullable=True)
    email_cliente = Column(String(255), nullable=True)
    telefono_cliente = Column(String(100), nullable=True)
    direccion_cliente = Column(String(500), nullable=True)
    referencia = Column(String(255))
    archivo_original = Column(String(500))
    archivo_resultado = Column(String(500))
    archivo_formal = Column(String(500), nullable=True)
    total_items = Column(Integer, default=0)
    items_procesados = Column(Integer, default=0)
    items_encontrados = Column(Integer, default=0)
    estado = Column(Enum(EstadoCotizacion), default=EstadoCotizacion.PENDIENTE)
    es_formal = Column(Integer, default=0)
    terminos_condiciones = Column(Text, nullable=True)
    error_msg = Column(Text, nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    fase_comercial = Column(String(50), default="ingresada")

    user = relationship("User", back_populates="cotizaciones")
    items = relationship(
        "ItemCotizacion", back_populates="cotizacion", cascade="all, delete-orphan"
    )
    versiones = relationship(
        "CotizacionVersion", back_populates="cotizacion", cascade="all, delete-orphan"
    )
    oc_cliente = relationship("OcCliente", back_populates="cotizacion", uselist=False)


class ItemCotizacion(Base):
    __tablename__ = "items_cotizacion"

    id = Column(Integer, primary_key=True, index=True)
    cotizacion_id = Column(Integer, ForeignKey("cotizaciones.id"), nullable=False)

    item_num = Column(Integer)
    descripcion = Column(String(500))
    numero_parte = Column(String(100), index=True)
    marca = Column(String(100))
    cantidad = Column(Float)
    precio_unit_cotizacion = Column(Float)
    total_cotizacion = Column(Float)
    plazo = Column(String(200))
    peso_unit_lbs = Column(Float, nullable=True)
    margen_pct = Column(Float, nullable=True)
    precio_finning = Column(Float, nullable=True)
    plazo_entrega_min = Column(Integer, nullable=True)
    plazo_entrega_max = Column(Integer, nullable=True)
    estado_item = Column(String(50), default="ingresado")

    nombre_cat = Column(String(500))
    precio_cat = Column(Float)
    moneda_cat = Column(String(20))
    retiro_estimado = Column(String(200))
    url_cat = Column(String(500))
    imagen_url = Column(String(500))
    encontrado = Column(Integer, default=0)
    scraping_error = Column(String(500))

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    cotizacion = relationship("Cotizacion", back_populates="items")


class ConfiguracionCotizador(Base):
    __tablename__ = "configuracion_cotizador"

    id = Column(Integer, primary_key=True, default=1)
    tipo_cambio_usd = Column(Float, default=940.0)
    costo_shipping_usd_kg = Column(Float, default=3.8)
    adicionales_shipping_usd = Column(Float, default=440.0)
    costo_agencia_pct = Column(Float, default=0.01)
    costo_agencia_minimo_clp = Column(Float, default=160000.0)
    desconsolidado_clp = Column(Float, default=90000.0)
    bodegaje_clp = Column(Float, default=90000.0)
    margen_venta_pct = Column(Float, default=0.19)
    plazo_min_default = Column(Integer, default=30)
    plazo_max_default = Column(Integer, default=45)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class PartsCache(Base):
    __tablename__ = "parts_cache"

    id = Column(Integer, primary_key=True, index=True)
    numero_parte = Column(String(100), unique=True, index=True)
    nombre_cat = Column(String(500))
    precio_cat = Column(Float)
    moneda_cat = Column(String(20))
    retiro_estimado = Column(String(200))
    url_cat = Column(String(500))
    imagen_url = Column(String(500))
    encontrado = Column(Integer, default=0)
    last_updated = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class OcCliente(Base):
    __tablename__ = "oc_cliente"

    id = Column(Integer, primary_key=True, index=True)
    cotizacion_id = Column(Integer, ForeignKey("cotizaciones.id"))
    numero_oc = Column(String(100))
    fecha_oc = Column(String(50))
    cond_pago = Column(String(200))
    fecha_entrega = Column(String(50))
    asesor_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    cotizacion = relationship("Cotizacion", back_populates="oc_cliente")
    asesor = relationship("User", foreign_keys=[asesor_id])
    items = relationship("OcProveedorItem", back_populates="oc_cliente")


class OcProveedor(Base):
    __tablename__ = "oc_proveedor"

    id = Column(Integer, primary_key=True, index=True)
    numero = Column(String(50))
    numero_oc = Column(String(100))
    proveedor = Column(String(255))
    pais = Column(String(100))
    moneda = Column(String(20), default="USD")
    estado = Column(String(50), default="borrador")
    plazo_dias = Column(Integer)
    awb = Column(String(100))
    notas = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    items = relationship("OcProveedorItem", back_populates="oc_proveedor")


class OcProveedorItem(Base):
    __tablename__ = "oc_proveedor_items"

    id = Column(Integer, primary_key=True, index=True)
    oc_proveedor_id = Column(Integer, ForeignKey("oc_proveedor.id"))
    oc_cliente_id = Column(Integer, ForeignKey("oc_cliente.id"))
    item_cotizacion_id = Column(Integer, ForeignKey("items_cotizacion.id"))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    plazo_dias_prov = Column(Integer, nullable=True)
    fecha_asignacion = Column(DateTime(timezone=True), nullable=True)

    item_cotizacion = relationship("ItemCotizacion")
    oc_proveedor = relationship("OcProveedor", back_populates="items")
    oc_cliente = relationship("OcCliente", back_populates="items")


class Embarque(Base):
    __tablename__ = "embarques"

    id = Column(Integer, primary_key=True, index=True)
    numero = Column(String(50))
    estado = Column(String(50), default="en_transito")
    forwarder = Column(String(255))
    awb = Column(String(100))
    fecha_despacho = Column(String(50))
    fecha_llegada_est = Column(String(50))
    notas = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    pre_embarque_id = Column(Integer, nullable=True)
    factura_comercial = Column(String(500), nullable=True)
    packing_list = Column(String(500), nullable=True)
    certificado_origen = Column(String(500), nullable=True)
    doc_adicional       = Column(String(500), nullable=True)

    items = relationship("EmbarqueItem", back_populates="embarque")


class EmbarqueItem(Base):
    __tablename__ = "embarque_items"

    id = Column(Integer, primary_key=True, index=True)
    embarque_id = Column(Integer, ForeignKey("embarques.id"))
    item_cotizacion_id = Column(Integer, ForeignKey("items_cotizacion.id"))
    oc_proveedor_id = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    embarque = relationship("Embarque", back_populates="items")
    item_cotizacion = relationship("ItemCotizacion")


class PreEmbarque(Base):
    __tablename__ = "pre_embarques"

    id = Column(Integer, primary_key=True, index=True)
    numero = Column(String(50))
    estado = Column(String(50), default="en_preparacion")
    notas = Column(Text)
    fecha_llegada_est = Column(String(50), nullable=True)
    doc_adicional = Column(String(500), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    items = relationship(
        "PreEmbarqueItem",
        back_populates="pre_embarque",
        cascade="all, delete-orphan",
    )


class PreEmbarqueItem(Base):
    __tablename__ = "pre_embarque_items"

    id = Column(Integer, primary_key=True, index=True)
    pre_embarque_id = Column(
        Integer, ForeignKey("pre_embarques.id", ondelete="CASCADE")
    )
    item_cotizacion_id = Column(Integer, ForeignKey("items_cotizacion.id"))
    oc_proveedor_id = Column(Integer, nullable=True)
    cantidad_despacho = Column(Numeric(12, 4), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    pre_embarque = relationship("PreEmbarque", back_populates="items")
    item_cotizacion = relationship("ItemCotizacion")
    oc_proveedor = relationship(
        "OcProveedor",
        primaryjoin="PreEmbarqueItem.oc_proveedor_id == OcProveedor.id",
        foreign_keys="[PreEmbarqueItem.oc_proveedor_id]",
    )


class Proveedor(Base):
    __tablename__ = "proveedores"

    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(255), nullable=False)
    pais = Column(String(100))
    moneda = Column(String(20))
    contacto = Column(String(255))
    email = Column(String(255))
    telefono = Column(String(100))
    sitio_web = Column(String(500))
    notas = Column(Text)
    tipo = Column(String(50), default="SWIFT")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class Cliente(Base):
    __tablename__ = "clientes"

    id = Column(Integer, primary_key=True, index=True)
    rut = Column(String(50), unique=True, index=True)
    nombre = Column(String(255))
    contacto = Column(String(255))
    email = Column(String(255))
    telefono = Column(String(100))
    direccion = Column(String(500))
    ciudad = Column(String(100))
    giro = Column(String(255))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class CotizacionVersion(Base):
    __tablename__ = "cotizacion_versiones"

    id = Column(Integer, primary_key=True, index=True)
    cotizacion_id = Column(
        Integer, ForeignKey("cotizaciones.id", ondelete="CASCADE")
    )
    version_num = Column(Integer)
    descripcion = Column(String(500))
    snapshot_json = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    cotizacion = relationship("Cotizacion", back_populates="versiones")


class FacturaProveedor(Base):
    __tablename__ = "facturas_proveedor"

    id          = Column(Integer, primary_key=True, index=True)
    ocp_id      = Column(Integer, ForeignKey("oc_proveedor.id", ondelete="CASCADE"))
    invoice_no  = Column(String(100))
    fecha       = Column(String(30))
    total_usd   = Column(Numeric(14, 2), nullable=True)
    freight_usd = Column(Numeric(14, 2), nullable=True)
    archivo     = Column(String(255), nullable=True)
    notas       = Column(Text, nullable=True)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())

    items = relationship("FacturaProveedorItem", back_populates="factura",
                         cascade="all, delete-orphan")
    oc_proveedor = relationship("OcProveedor")


class FacturaProveedorItem(Base):
    __tablename__ = "factura_proveedor_items"

    id                      = Column(Integer, primary_key=True, index=True)
    factura_id              = Column(Integer, ForeignKey("facturas_proveedor.id", ondelete="CASCADE"))
    ocp_item_id             = Column(Integer, ForeignKey("oc_proveedor_items.id", ondelete="SET NULL"), nullable=True)
    descripcion             = Column(String(500), nullable=True)
    qty_facturada           = Column(Numeric(12, 4), default=0)
    weight_lbs              = Column(Numeric(12, 4), nullable=True)
    unit_price_usd          = Column(Numeric(14, 4), nullable=True)
    freight_prorrateado_usd = Column(Numeric(14, 4), nullable=True)
    notas                   = Column(String(255), nullable=True)

    factura  = relationship("FacturaProveedor", back_populates="items")
    ocp_item = relationship("OcProveedorItem", foreign_keys=[ocp_item_id])
