from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Date, ForeignKey, Numeric, Numeric,
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
    empresa = Column(String(50), nullable=False, server_default='mineria')
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
    fecha_entrega = Column(Date, nullable=True)
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
    fecha_llegada_est = Column(Date, nullable=True)
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


# ─── v2 Models ───────────────────────────────────────────────────────────────

class Notificacion(Base):
    """In-app notifications per rol"""
    __tablename__ = "notificaciones"
    id = Column(Integer, primary_key=True, index=True)
    destinatario_rol = Column(String(50), nullable=False)
    severidad = Column(String(20), default='info')
    titulo = Column(String(255), nullable=False)
    mensaje = Column(Text, nullable=False)
    entidad_tipo = Column(String(50), nullable=True)
    entidad_id = Column(Integer, nullable=True)
    link = Column(String(500), nullable=True)
    leida = Column(Boolean, default=False)
    fecha_creacion = Column(DateTime(timezone=True), server_default=func.now())
    regla = Column(String(150), nullable=True)


class RecepcionEmbarque(Base):
    """Physical reception of a shipment at warehouse"""
    __tablename__ = "recepciones_embarque"
    id = Column(Integer, primary_key=True, index=True)
    embarque_id = Column(Integer, ForeignKey("embarques.id"), unique=True)
    fecha_inicio = Column(DateTime(timezone=True), server_default=func.now())
    fecha_cierre = Column(DateTime(timezone=True), nullable=True)
    usuario_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    estado = Column(String(20), default='abierta')
    observacion_general = Column(Text, nullable=True)

    embarque = relationship("Embarque")
    usuario = relationship("User", foreign_keys="[RecepcionEmbarque.usuario_id]")
    items = relationship("RecepcionEmbarqueItem", back_populates="recepcion",
                         cascade="all, delete-orphan")


class RecepcionEmbarqueItem(Base):
    """Per-item reception status"""
    __tablename__ = "recepcion_embarque_items"
    id = Column(Integer, primary_key=True, index=True)
    recepcion_id = Column(Integer, ForeignKey("recepciones_embarque.id", ondelete="CASCADE"))
    embarque_item_id = Column(Integer, ForeignKey("embarque_items.id"))
    qty_recibida = Column(Integer, default=0)
    qty_danada = Column(Integer, default=0)
    estado_recepcion = Column(String(30), nullable=True)
    observacion = Column(Text, nullable=True)
    fecha_marcado = Column(DateTime(timezone=True), onupdate=func.now())
    usuario_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    recepcion = relationship("RecepcionEmbarque", back_populates="items")
    embarque_item = relationship("EmbarqueItem")
    fotos = relationship("RecepcionItemFoto", back_populates="item_recepcion",
                         cascade="all, delete-orphan")


class RecepcionItemFoto(Base):
    """Photos attached to a damaged/incomplete reception item"""
    __tablename__ = "recepcion_item_fotos"
    id = Column(Integer, primary_key=True, index=True)
    recepcion_item_id = Column(Integer, ForeignKey("recepcion_embarque_items.id", ondelete="CASCADE"))
    url_foto = Column(String(500), nullable=False)
    subida_por = Column(Integer, ForeignKey("users.id"), nullable=True)
    fecha_subida = Column(DateTime(timezone=True), server_default=func.now())

    item_recepcion = relationship("RecepcionEmbarqueItem", back_populates="fotos")


class ReclamoProveedor(Base):
    """Supplier claim generated from failed reception items"""
    __tablename__ = "reclamos_proveedor"
    id = Column(Integer, primary_key=True, index=True)
    oc_proveedor_id = Column(Integer, ForeignKey("oc_proveedor.id"), nullable=True)
    item_cotizacion_id = Column(Integer, ForeignKey("items_cotizacion.id"), nullable=True)
    recepcion_item_id = Column(Integer, ForeignKey("recepcion_embarque_items.id"), nullable=True)
    motivo = Column(String(30), nullable=False)
    qty_afectada = Column(Integer, default=0)
    estado = Column(String(20), default='pendiente')
    observacion = Column(Text, nullable=True)
    usuario_resuelve_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    fecha_creacion = Column(DateTime(timezone=True), server_default=func.now())
    fecha_resolucion = Column(DateTime(timezone=True), nullable=True)

    item_cotizacion = relationship("ItemCotizacion")
    recepcion_item = relationship("RecepcionEmbarqueItem")


class Despacho(Base):
    """Cliente dispatch from warehouse"""
    __tablename__ = "despachos"
    id = Column(Integer, primary_key=True, index=True)
    numero_despacho = Column(String(50), unique=True, index=True)
    oc_cliente_id = Column(Integer, ForeignKey("oc_cliente.id"))
    numero_guia = Column(String(100), nullable=True)
    transportista = Column(String(255), nullable=True)
    contacto_destinatario = Column(String(255), nullable=True)
    direccion_entrega = Column(String(500), nullable=True)
    observaciones = Column(Text, nullable=True)
    estado = Column(String(20), default="en_preparacion")
    fecha_creacion = Column(DateTime(timezone=True), server_default=func.now())
    fecha_despacho = Column(DateTime(timezone=True), nullable=True)
    usuario_creacion_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    oc_cliente = relationship("OcCliente")
    usuario_creacion = relationship("User", foreign_keys=[usuario_creacion_id])
    items = relationship("DespachoItem", back_populates="despacho",
                         cascade="all, delete-orphan")


class DespachoItem(Base):
    """Item included in a dispatch"""
    __tablename__ = "despacho_items"
    id = Column(Integer, primary_key=True, index=True)
    despacho_id = Column(Integer, ForeignKey("despachos.id", ondelete="CASCADE"))
    item_cotizacion_id = Column(Integer, ForeignKey("items_cotizacion.id"))
    qty_despachada = Column(Float, default=0)

    despacho = relationship("Despacho", back_populates="items")
    item_cotizacion = relationship("ItemCotizacion")
