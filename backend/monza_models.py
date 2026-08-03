from sqlalchemy import Column, Integer, String, Float, DateTime, Date, Boolean, Text, JSON, ForeignKey, Numeric
from sqlalchemy.orm import relationship
from datetime import datetime
from models.models import Base, User


# ── Clientes ──────────────────────────────────────────────────────────────────

class MonzaCliente(Base):
    __tablename__ = "monza_clientes"

    id = Column(Integer, primary_key=True)
    nombre = Column(String(200), nullable=False)
    rut = Column(String(20), nullable=True)
    telefono = Column(String(30), nullable=True)
    email = Column(String(100), nullable=True)
    vehiculos = Column(JSON, default=list)
    etiquetas = Column(JSON, default=list)
    ltv = Column(Float, default=0)
    leads_total = Column(Integer, default=0)
    vendidos_total = Column(Integer, default=0)
    # Columnas migradas desde Postgres
    code = Column(String(50), nullable=True)
    customer_since = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)
    pg_id = Column(String(30), nullable=True, unique=True)
    fecha_creacion = Column(DateTime, default=datetime.utcnow)
    fecha_actualizacion = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    leads = relationship("MonzaLead", back_populates="cliente")
    markups = relationship("MonzaMarkup", back_populates="cliente", cascade="all, delete-orphan")
    cliente_tags = relationship("MonzaClienteTag", back_populates="cliente", cascade="all, delete-orphan")


# ── Asesores comerciales ───────────────────────────────────────────────────────

class MonzaAsesor(Base):
    __tablename__ = "monza_asesores"

    id = Column(Integer, primary_key=True)
    slug = Column(String(50), nullable=False, unique=True)
    nombre = Column(String(200), nullable=False)
    email = Column(String(100), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    activo = Column(Boolean, default=True)
    pg_id = Column(String(30), nullable=True, unique=True)
    fecha_creacion = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", foreign_keys=[user_id])


# ── Tags / etiquetas ──────────────────────────────────────────────────────────

class MonzaTag(Base):
    __tablename__ = "monza_tags"

    id = Column(Integer, primary_key=True)
    label = Column(String(100), nullable=False, unique=True)
    color_token = Column(String(100), nullable=True)
    activo = Column(Boolean, default=True)
    pg_id = Column(String(30), nullable=True, unique=True)
    fecha_creacion = Column(DateTime, default=datetime.utcnow)

    cliente_tags = relationship("MonzaClienteTag", back_populates="tag")


class MonzaClienteTag(Base):
    __tablename__ = "monza_cliente_tags"

    cliente_id = Column(Integer, ForeignKey("monza_clientes.id"), primary_key=True)
    tag_id = Column(Integer, ForeignKey("monza_tags.id"), primary_key=True)
    asignado_en = Column(DateTime, default=datetime.utcnow)

    cliente = relationship("MonzaCliente", back_populates="cliente_tags")
    tag = relationship("MonzaTag", back_populates="cliente_tags")


# ── Markup matrix ─────────────────────────────────────────────────────────────

class MonzaMarkup(Base):
    __tablename__ = "monza_markups"

    id = Column(Integer, primary_key=True)
    cliente_id = Column(Integer, ForeignKey("monza_clientes.id"), nullable=False)
    calidad = Column(String(20), nullable=False)  # genuino/oem/aftermarket
    valor = Column(Numeric(8, 4), nullable=False)

    cliente = relationship("MonzaCliente", back_populates="markups")


# ── Proveedores y catálogo ────────────────────────────────────────────────────

class MonzaProveedor(Base):
    __tablename__ = "monza_proveedores"

    id = Column(Integer, primary_key=True)
    code = Column(String(50), nullable=False, unique=True)
    nombre = Column(String(200), nullable=False)
    pais = Column(String(10), nullable=True)
    formato = Column(String(20), nullable=True)
    linea_default = Column(String(20), nullable=True)
    notas = Column(Text, nullable=True)
    activo = Column(Boolean, default=True)
    pg_id = Column(String(30), nullable=True, unique=True)
    fecha_creacion = Column(DateTime, default=datetime.utcnow)

    listas = relationship("MonzaListaPrecios", back_populates="proveedor")


class MonzaListaPrecios(Base):
    __tablename__ = "monza_listas_precios"

    id = Column(Integer, primary_key=True)
    proveedor_id = Column(Integer, ForeignKey("monza_proveedores.id"), nullable=False)
    code = Column(String(50), nullable=False)
    nombre = Column(String(200), nullable=True)
    lead_time_dias = Column(Integer, nullable=True)
    delivery_profile = Column(String(200), nullable=True)
    notas = Column(Text, nullable=True)
    activo = Column(Boolean, default=True)
    pg_id = Column(String(30), nullable=True, unique=True)
    fecha_creacion = Column(DateTime, default=datetime.utcnow)

    proveedor = relationship("MonzaProveedor", back_populates="listas")
    partes = relationship("MonzaParteCatalogo", back_populates="lista")


class MonzaParteCatalogo(Base):
    __tablename__ = "monza_partes_catalogo"

    id = Column(Integer, primary_key=True)
    lista_id = Column(Integer, ForeignKey("monza_listas_precios.id"), nullable=False)
    sku = Column(String(100), nullable=False)
    descripcion = Column(String(500), nullable=False)
    marca = Column(String(100), nullable=True)
    calidad = Column(String(20), nullable=False, default="GENUINO")  # GENUINO/OEM/AFTERMARKET
    costo = Column(Numeric(12, 4), nullable=False)
    moneda = Column(String(5), nullable=False, default="EUR")
    peso_real_kg = Column(Numeric(12, 4), nullable=True)
    peso_vol_kg = Column(Numeric(12, 4), nullable=True)
    pfand = Column(Numeric(12, 4), nullable=True)
    moq = Column(Integer, nullable=True)
    nueva_parte_sku = Column(String(100), nullable=True)
    disponible = Column(Boolean, default=True)
    pg_id = Column(String(30), nullable=True, unique=True)
    ultima_importacion = Column(DateTime, nullable=True)
    fecha_creacion = Column(DateTime, default=datetime.utcnow)

    lista = relationship("MonzaListaPrecios", back_populates="partes")


# ── Leads ─────────────────────────────────────────────────────────────────────

class MonzaLead(Base):
    __tablename__ = "monza_leads"

    id = Column(Integer, primary_key=True)
    numero = Column(String(20), unique=True, nullable=False)  # L-2026-0001
    cliente_id = Column(Integer, ForeignKey("monza_clientes.id"), nullable=True)
    vehiculo = Column(String(200), nullable=True)
    marca = Column(String(100), nullable=True)
    modelo = Column(String(100), nullable=True)
    anio = Column(String(10), nullable=True)
    vin = Column(String(50), nullable=True)
    canal_origen = Column(String(50), default="WhatsApp")
    asesor_id = Column(Integer, ForeignKey("monza_asesores.id"), nullable=True)
    estado = Column(String(30), default="pendiente")  # pendiente/en_proceso/vendido/rechazado
    comentario = Column(Text, nullable=True)
    linea = Column(String(20), nullable=True)  # autos/maquinaria
    total_estimado = Column(Float, default=0)
    sin_contactar_dias = Column(Integer, default=0)
    # Columnas migradas
    origen_crm = Column(String(30), nullable=True)
    vehicle_label = Column(String(200), nullable=True)
    pg_id = Column(String(30), nullable=True, unique=True)
    fecha_creacion = Column(DateTime, default=datetime.utcnow)
    fecha_actualizacion = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    cliente = relationship("MonzaCliente", back_populates="leads")
    asesor = relationship("MonzaAsesor", foreign_keys=[asesor_id])
    items = relationship("MonzaLeadItem", back_populates="lead", cascade="all, delete-orphan")
    actividades = relationship("MonzaLeadActividad", back_populates="lead", cascade="all, delete-orphan",
                               order_by="MonzaLeadActividad.fecha.desc()")
    proximos_pasos = relationship("MonzaProximoPaso", back_populates="lead", cascade="all, delete-orphan")
    cotizaciones = relationship("MonzaCotizacion", back_populates="lead")


class MonzaLeadItem(Base):
    __tablename__ = "monza_lead_items"

    id = Column(Integer, primary_key=True)
    lead_id = Column(Integer, ForeignKey("monza_leads.id"), nullable=False)
    descripcion = Column(String(300), nullable=False)
    numero_parte = Column(String(100), nullable=True)
    marca = Column(String(100), nullable=True)
    procedencia = Column(String(100), nullable=True)
    calidad = Column(String(30), default="sin_calificar")  # sin_calificar/genuine/oem/aftermarket
    cantidad = Column(Integer, default=1)
    precio_clp = Column(Float, nullable=True)  # precio calculado/aplicado (sin IVA, por unidad)
    supplier_part_pg_id = Column(String(30), nullable=True)
    plazo_entrega = Column(String(100), nullable=True)

    lead = relationship("MonzaLead", back_populates="items")


class MonzaLeadActividad(Base):
    __tablename__ = "monza_lead_actividades"

    id = Column(Integer, primary_key=True)
    lead_id = Column(Integer, ForeignKey("monza_leads.id"), nullable=False)
    tipo = Column(String(30))  # lead_creado/llamada/whatsapp/email/visita/nota/cotizacion
    descripcion = Column(Text)
    usuario = Column(String(100))
    usuario_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    fecha = Column(DateTime, default=datetime.utcnow)

    lead = relationship("MonzaLead", back_populates="actividades")


class MonzaProximoPaso(Base):
    __tablename__ = "monza_proximos_pasos"

    id = Column(Integer, primary_key=True)
    lead_id = Column(Integer, ForeignKey("monza_leads.id"), nullable=False)
    tipo = Column(String(20))  # llamada/whatsapp/email/visita
    cuando = Column(DateTime, nullable=True)
    asesor_id = Column(Integer, ForeignKey("monza_asesores.id"), nullable=True)
    completado = Column(Boolean, default=False)
    fecha_creacion = Column(DateTime, default=datetime.utcnow)

    lead = relationship("MonzaLead", back_populates="proximos_pasos")
    asesor = relationship("MonzaAsesor", foreign_keys=[asesor_id])


# ── Cotizaciones ──────────────────────────────────────────────────────────────

class MonzaCotizacion(Base):
    __tablename__ = "monza_cotizaciones"

    id = Column(Integer, primary_key=True)
    numero = Column(String(20), unique=True, nullable=False)  # COT-2026-0001
    lead_id = Column(Integer, ForeignKey("monza_leads.id"), nullable=True)
    cliente_id = Column(Integer, ForeignKey("monza_clientes.id"), nullable=False)
    estado = Column(String(20), default="propuesta")  # propuesta/enviada/vendida/rechazada
    # Adelanto (verificado por Contabilidad) — aditivo para modulo monza_contabilidad
    pct_adelanto = Column(Integer, default=0)
    adelanto_verificado = Column(Integer, default=0)
    guia_firmada = Column(Integer, default=0)
    guia_firmada_archivo = Column(String(255), nullable=True)
    forma_pago = Column(String(100), nullable=True)
    tipo_cotizacion = Column(String(100), nullable=True)
    linea = Column(String(20), nullable=True)  # autos/maquinaria
    vehiculo = Column(String(200), nullable=True)
    anio = Column(String(10), nullable=True)
    total_neto = Column(Float, default=0)
    iva_monto = Column(Float, default=0)
    total_bruto = Column(Float, default=0)
    fecha_venta = Column(DateTime, nullable=True)
    fecha_entrega_est = Column(Date, nullable=True)
    oc_cliente = Column(String(100), nullable=True)
    # Fecha de la OC del cliente (Fase 3 espejo GA): la referencia 801 del SII exige
    # N° Y fecha de la OC — sin esta columna la guía/factura electrónica (Fase 5/6)
    # no puede armar la referencia. Se pide junto al N° al cerrar la venta.
    oc_fecha = Column(Date, nullable=True)
    vin = Column(String(50), nullable=True)
    asesor_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    condiciones_servicio = Column(Text, nullable=True)
    fecha_despacho = Column(Date, nullable=True)
    numero_factura = Column(String(50), nullable=True)
    tipo_documento = Column(String(20), nullable=True)
    documento_path = Column(String(255), nullable=True)
    # Snapshot de config al momento de crear
    tc_usd_clp = Column(Float, nullable=True)
    tc_eur_clp = Column(Float, nullable=True)
    # Moneda de la tarifa aérea al momento de cotizar (EUR/USD): sin congelarla la
    # foto de precios no es reproducible (espejo del pricing_snapshot de GA).
    # Freeze-forward: cotizaciones anteriores quedan NULL, sin backfill.
    moneda_tarifa = Column(String(10), nullable=True)
    tarifa_aerea = Column(Float, nullable=True)
    iva_pct = Column(Float, nullable=True)
    archivo_pdf = Column(String(200), nullable=True)
    fecha_creacion = Column(DateTime, default=datetime.utcnow)

    lead = relationship("MonzaLead", back_populates="cotizaciones")
    cliente = relationship("MonzaCliente")
    asesor = relationship("User", foreign_keys=[asesor_id])
    items = relationship("MonzaCotizacionItem", back_populates="cotizacion", cascade="all, delete-orphan")


class MonzaCotizacionItem(Base):
    __tablename__ = "monza_cotizacion_items"

    id = Column(Integer, primary_key=True)
    cotizacion_id = Column(Integer, ForeignKey("monza_cotizaciones.id"), nullable=False)
    descripcion = Column(String(300), nullable=False)
    numero_parte = Column(String(100), nullable=True)
    marca = Column(String(100), nullable=True)
    procedencia = Column(String(100), nullable=True)
    calidad = Column(String(30), nullable=True)
    cantidad = Column(Integer, default=1)
    costo = Column(Float, nullable=True)
    moneda = Column(String(10), default="EUR")
    peso_kg = Column(Float, default=0)
    tc_aplicado = Column(Float, nullable=True)
    tarifa_aerea = Column(Float, nullable=True)
    markup_pct = Column(Float, default=0)  # decimal, ej 0.28 = 28%
    precio_unitario_clp = Column(Float, nullable=True)  # neto sin IVA por unidad
    subtotal_clp = Column(Float, nullable=True)
    plazo_entrega = Column(String(100), nullable=True)
    estado_linea = Column(String(30), default="cotizado")  # cotizado por_comprar comprado en_transito en_bodega despachado reclamo
    oc_proveedor_id = Column(Integer, nullable=True)

    cotizacion = relationship("MonzaCotizacion", back_populates="items")


# ── Configuración ─────────────────────────────────────────────────────────────

class MonzaConfig(Base):
    __tablename__ = "monza_config"

    id = Column(Integer, primary_key=True)
    tc_usd_clp = Column(Float, default=950)
    tc_eur_clp = Column(Float, default=1100)
    tarifa_aerea_por_kg = Column(Float, default=4.5)
    moneda_tarifa = Column(String(10), default="EUR")  # EUR/USD
    iva_pct = Column(Float, default=19)
    # ── Gastos locales de internación por DEFECTO (CLP, netos) ────────────────
    # Con estos montos nacen las 3 líneas afectas del pricing de cada embarque
    # (Desconsolidación / Almacenaje / Agencia de Aduana). Espejo de
    # ConfiguracionCotizador.{desconsolidado_clp, bodegaje_clp,
    # costo_agencia_minimo_clp} de Grupo AM, con los mismos nombres para que
    # `seed_gastos` sea idéntico en las dos marcas.
    # POR QUÉ: Monza sembraba las 6 líneas en 0, así que si el contador se olvidaba
    # de cargarlas el costo landed se CONGELABA sin gastos de internación
    # (cerrar_pricing solo exige costo_total > 0) y todos los ítems del embarque
    # quedaban subvaluados, en silencio.
    # DEFAULT 0 a propósito: NO se copian los montos de Grupo AM (otro negocio, otra
    # logística — inventar montos sería peor que no tenerlos). Mientras estén en 0 el
    # comportamiento es idéntico al de hoy; el contador los carga UNA vez en
    # Configuración en vez de en cada embarque, y siguen siendo editables por embarque.
    desconsolidado_clp = Column(Float, default=0)
    bodegaje_clp = Column(Float, default=0)
    costo_agencia_minimo_clp = Column(Float, default=0)
    # Datos empresa MonzaParts (para PDF)
    razon_social = Column(String(200), default="MonzaParts SpA")
    rut_empresa = Column(String(20), default="78.121.316-0")
    direccion = Column(String(200), default="Roger de Flor 2996")
    giro = Column(String(200), default="Venta de repuestos automotrices")
    email_empresa = Column(String(100), default="logistica@monzaparts.cl")
    banco = Column(String(100), nullable=True)
    tipo_cuenta = Column(String(50), nullable=True)
    numero_cuenta = Column(String(50), nullable=True)
    condiciones_default = Column(Text, default=(
        "1.- Validez de la cotización 5 días y sujeta a disponibilidad de los repuestos.\n"
        "2.- Se considera entrega a cliente en terreno.\n"
        "3.- Repuestos genuinos, en su packing original."
    ))
    ultima_actualizacion = Column(DateTime, default=datetime.utcnow)
    usuario_email = Column(String(100), nullable=True)


# ── Logs de operaciones CRUD ──────────────────────────────────────────────────

class MonzaLog(Base):
    __tablename__ = "monza_logs"

    id          = Column(Integer, primary_key=True)
    user_email  = Column(String(100), nullable=True)
    accion      = Column(String(20), nullable=False)   # CREATE UPDATE DELETE UPLOAD LOGIN
    entidad     = Column(String(30), nullable=False)   # lead cotizacion cliente item
    entidad_id  = Column(Integer, nullable=True)
    entidad_ref = Column(String(200), nullable=True)   # numero/nombre para display
    detalle     = Column(Text, nullable=True)
    ip          = Column(String(60), nullable=True)
    fecha       = Column(DateTime, default=datetime.utcnow)


# ── Abastecimiento: Proveedores de compra y OC de proveedor ───────────────────

class MonzaProvAbast(Base):
    """Proveedor de compra para abastecimiento (distinto del catálogo de precios)."""
    __tablename__ = "monza_prov_abastecimiento"

    id        = Column(Integer, primary_key=True)
    nombre    = Column(String(200), nullable=False)
    pais      = Column(String(100), nullable=True)
    moneda    = Column(String(10), default="EUR")
    contacto  = Column(String(200), nullable=True)
    email     = Column(String(150), nullable=True)
    telefono  = Column(String(60), nullable=True)
    notas     = Column(Text, nullable=True)
    activo    = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.utcnow)


class MonzaOcProveedor(Base):
    __tablename__ = "monza_oc_proveedor"

    id               = Column(Integer, primary_key=True)
    numero           = Column(String(50), nullable=True)
    proveedor_id     = Column(Integer, nullable=True)
    proveedor_nombre = Column(String(200), nullable=True)
    pais             = Column(String(100), nullable=True)
    moneda           = Column(String(10), default="EUR")
    # Origen de la compra: 'internacional' (embarque + aduana + costo landed) o
    # 'nacional' (camión + guía de despacho del proveedor, sin embarque). Fuente
    # ÚNICA de verdad del camino físico y de la UI; NO se deriva de pais/moneda
    # (pais es texto libre y un proveedor nacional podría facturar en USD). Todo lo
    # histórico queda 'internacional'. La columna la crea monza_recepcion_nacional/init_db
    # (create_all NO altera tablas ya existentes). Lectura SIEMPRE coalescida:
    # (ocp.tipo_origen or "internacional").
    tipo_origen      = Column(String(20), nullable=False, server_default="internacional", index=True)
    estado           = Column(String(30), default="emitida")  # emitida en_transito recibida cancelada
    plazo_dias       = Column(Integer, nullable=True)
    numero_oc        = Column(String(100), nullable=True)   # N OC manual del proveedor
    awb              = Column(String(100), nullable=True)   # guia aerea
    tracking         = Column(String(150), nullable=True)   # tracking courier/forwarder
    notas            = Column(Text, nullable=True)
    asesor_email     = Column(String(150), nullable=True)
    created_at       = Column(DateTime, default=datetime.utcnow)
    updated_at       = Column(DateTime, nullable=True)


# ── Bodega: Reclamos a proveedor ──────────────────────────────────────────────

class MonzaReclamo(Base):
    __tablename__ = "monza_reclamos"

    id               = Column(Integer, primary_key=True)
    item_id          = Column(Integer, nullable=True)
    oc_proveedor_id  = Column(Integer, nullable=True)
    cot_numero       = Column(String(50), nullable=True)
    descripcion      = Column(String(300), nullable=True)
    motivo           = Column(String(30), nullable=False)   # danado faltante no_llego
    qty_afectada     = Column(Integer, default=0)
    estado           = Column(String(20), default="pendiente")  # pendiente reclamado resuelto anulado
    observacion      = Column(Text, nullable=True)
    user_email       = Column(String(150), nullable=True)
    fecha_creacion   = Column(DateTime, default=datetime.utcnow)
    fecha_resolucion = Column(DateTime, nullable=True)


# -- Notificaciones in-app --------------------------------------------------

class MonzaNotificacion(Base):
    __tablename__ = "monza_notificaciones"

    id         = Column(Integer, primary_key=True)
    titulo     = Column(String(200), nullable=False)
    mensaje    = Column(String(400), nullable=True)
    tipo       = Column(String(20), default="info")   # info success warning danger
    entidad    = Column(String(30), nullable=True)
    entidad_id = Column(Integer, nullable=True)
    link       = Column(String(200), nullable=True)
    leida      = Column(Integer, default=0)
    fecha      = Column(DateTime, default=datetime.utcnow)
    # ── Endurecimiento para el barrido diario de alertas (paridad con la tabla
    # `notificaciones` de Grupo AM, models/models.py:381) ─────────────────────────
    # `regla` es la LLAVE de la idempotencia de 24 h: el job de las 06:00 vuelve a
    # evaluar las mismas condiciones todos los días, y sin un identificador estable de
    # la regla cada corrida crearía otra vez el mismo aviso hasta que el dueño deje de
    # mirar la campana (ruido = peor que no tener alertas).
    # `destinatario_rol` dice A QUIÉN le toca actuar y `severidad` cuán urgente es
    # (info/warning/critical, vocabulario de Grupo AM). Se conserva `tipo` porque es lo
    # que pinta el badge del frontend: son ejes distintos, no duplicados.
    # Las 3 columnas son NULLABLE a propósito: las notificaciones históricas y los 8
    # llamadores ya existentes de crear_notif() siguen funcionando sin tocarse.
    # OJO: NO llevan index=True — el ALTER de la migración solo agrega columnas, y un
    # index=True dejaría la BD fresca (create_all) con un índice que prod no tendría.
    destinatario_rol = Column(String(50), nullable=True)
    severidad        = Column(String(20), nullable=True)
    regla            = Column(String(150), nullable=True)


# -- Documentos adjuntos (generico para cualquier entidad) ------------------

class MonzaDocumento(Base):
    __tablename__ = "monza_documentos"

    id            = Column(Integer, primary_key=True)
    entidad       = Column(String(30), nullable=False)   # cotizacion oc_proveedor item lead
    entidad_id    = Column(Integer, nullable=False)
    categoria     = Column(String(40), default="otro")   # factura guia tracking foto otro
    filename      = Column(String(255), nullable=False)
    original_name = Column(String(255), nullable=True)
    content_type  = Column(String(100), nullable=True)
    uploaded_by   = Column(String(150), nullable=True)
    fecha         = Column(DateTime, default=datetime.utcnow)


# -- Alineacion core MachParts: Embarque, Recepcion, Despacho --------------------

class MonzaEmbarque(Base):
    __tablename__ = "monza_embarques"
    id                = Column(Integer, primary_key=True)
    numero            = Column(String(50), nullable=True)
    estado            = Column(String(30), default="en_bodega_proveedor")  # en_bodega_proveedor en_transito en_aduana en_bodega
    awb               = Column(String(100), nullable=True)
    forwarder         = Column(String(150), nullable=True)
    tracking          = Column(String(150), nullable=True)
    fecha_despacho    = Column(String(30), nullable=True)
    fecha_llegada_est = Column(String(30), nullable=True)
    notas             = Column(Text, nullable=True)
    asesor_email      = Column(String(150), nullable=True)
    created_at        = Column(DateTime, default=datetime.utcnow)
    updated_at        = Column(DateTime, nullable=True)


class MonzaEmbarqueItem(Base):
    __tablename__ = "monza_embarque_items"
    id          = Column(Integer, primary_key=True)
    embarque_id = Column(Integer, nullable=False)
    item_id     = Column(Integer, nullable=False)
    created_at  = Column(DateTime, default=datetime.utcnow)


class MonzaRecepcion(Base):
    __tablename__ = "monza_recepciones"
    id            = Column(Integer, primary_key=True)
    embarque_id   = Column(Integer, nullable=False)
    estado        = Column(String(20), default="abierta")  # abierta cerrada
    fecha_inicio  = Column(DateTime, default=datetime.utcnow)
    fecha_cierre  = Column(DateTime, nullable=True)
    usuario_email = Column(String(150), nullable=True)
    observacion   = Column(Text, nullable=True)


class MonzaRecepcionItem(Base):
    __tablename__ = "monza_recepcion_items"
    id               = Column(Integer, primary_key=True)
    recepcion_id     = Column(Integer, nullable=False)
    item_id          = Column(Integer, nullable=False)
    estado_recepcion = Column(String(30), nullable=True)  # completo faltante sobrante danado_utilizable danado_no_utilizable no_llego
    qty_recibida     = Column(Integer, default=0)
    qty_danada       = Column(Integer, default=0)
    observacion      = Column(Text, nullable=True)
    fecha            = Column(DateTime, nullable=True)


class MonzaDespacho(Base):
    __tablename__ = "monza_despachos"
    id               = Column(Integer, primary_key=True)
    # UNIQUE (índice creado por migrations/monza_despachos_ciclo_vida.py): sostiene el
    # retry del correlativo DSP ante creación simultánea (espejo de Grupo AM).
    numero           = Column(String(50), unique=True, nullable=True)
    cotizacion_id    = Column(Integer, nullable=True)
    cliente_nombre   = Column(String(200), nullable=True)
    numero_guia      = Column(String(100), nullable=True)
    # Fecha de EMISIÓN de la guía ante el SII cuando se emitió FUERA del sistema (portal
    # del SII / papel) y aquí solo se registra su número. Es el FchRef de la referencia 52
    # de la factura. NO es `fecha` (creación), ni `fecha_despacho` (cierre), ni la firma.
    # Con guía ELECTRÓNICA manda el documentDate del propio DTE 52 y esta columna se ignora.
    # Espejo de despachos.fecha_guia de MachParts — tabla y módulo SEPARADOS por marca.
    fecha_guia       = Column(Date, nullable=True)
    transportista    = Column(String(150), nullable=True)
    destinatario     = Column(String(200), nullable=True)
    direccion_entrega= Column(String(400), nullable=True)
    observaciones    = Column(Text, nullable=True)
    # Ciclo de vida (espejo de Grupo AM): en_preparacion (borrador editable/anulable)
    # → despachado (cerrado: la mercadería salió) → anulado (solo desde borrador).
    # Los despachos legados nacieron directamente 'despachado' (default histórico).
    estado           = Column(String(20), default="despachado")
    asesor_email     = Column(String(150), nullable=True)
    fecha            = Column(DateTime, default=datetime.utcnow)
    # Cuándo se CERRÓ el despacho (fecha ≠ fecha de creación). Flujo guía-primero:
    # el N° de expedición lo entrega el courier DESPUÉS, por el PUT de cabecera.
    fecha_despacho   = Column(DateTime, nullable=True)
    numero_expedicion= Column(String(120), nullable=True)
    # Guía firmada por el cliente (informativo, NO gatea la facturación en Monza).
    # Las columnas EXISTEN en la tabla desde monza_contabilidad/init_db.py, pero el
    # modelo no las declaraba: marcar_guia_firmada escribía un atributo Python suelto
    # y la firma se PERDÍA en silencio (bug visible recién al envolver la suite vieja
    # en pytest, auditoría de paridad 2026-07-28).
    guia_firmada     = Column(Integer, default=0)
    guia_firmada_archivo = Column(String(255), nullable=True)


class MonzaDespachoItem(Base):
    __tablename__ = "monza_despacho_items"
    id             = Column(Integer, primary_key=True)
    despacho_id    = Column(Integer, nullable=False)
    item_id        = Column(Integer, nullable=False)
    qty_despachada = Column(Integer, default=0)


class MonzaWebhookLog(Base):
    __tablename__ = "monza_webhook_log"
    id = Column(Integer, primary_key=True)
    fuente = Column(String(40), default="nexor")
    headers = Column(Text, nullable=True)
    payload = Column(Text, nullable=True)
    procesado = Column(Integer, default=0)
    lead_id = Column(Integer, nullable=True)
    lead_numero = Column(String(20), nullable=True)
    error = Column(String(400), nullable=True)
    external_id = Column(String(120), nullable=True)
    fecha = Column(DateTime, default=datetime.utcnow)


class MonzaTicket(Base):
    """Ticket de soporte / solicitud de cambio (MonzaParts). Hilo en MonzaTicketRespuesta."""
    __tablename__ = "monza_tickets"
    id = Column(Integer, primary_key=True)
    numero = Column(String(30), unique=True, index=True)
    titulo = Column(String(255), nullable=False)
    descripcion = Column(Text, nullable=False)
    categoria = Column(String(30), default="soporte")
    prioridad = Column(String(20), default="media")
    estado = Column(String(20), default="abierto")
    solicitante_id = Column(Integer, nullable=True)
    solicitante_nombre = Column(String(255), nullable=True)
    fecha_creacion = Column(DateTime, default=datetime.utcnow)
    fecha_actualizacion = Column(DateTime, default=datetime.utcnow)
    fecha_cierre = Column(DateTime, nullable=True)
    respuestas = relationship("MonzaTicketRespuesta", back_populates="ticket",
                              cascade="all, delete-orphan")


class MonzaTicketRespuesta(Base):
    __tablename__ = "monza_ticket_respuestas"
    id = Column(Integer, primary_key=True)
    ticket_id = Column(Integer, ForeignKey("monza_tickets.id"), nullable=False, index=True)
    autor_id = Column(Integer, nullable=True)
    autor_nombre = Column(String(255), nullable=True)
    es_solicitante = Column(Integer, default=0)
    mensaje = Column(Text, nullable=False)
    fecha_creacion = Column(DateTime, default=datetime.utcnow)
    ticket = relationship("MonzaTicket", back_populates="respuestas")
