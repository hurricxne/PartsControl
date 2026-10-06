"""Tablas PROPIAS del Portal de Clientes. Cero columnas en tablas ajenas desde acá
(las dos columnas aditivas del núcleo se declaran en models/models.py, donde vive su
tabla, y las crea portal/init_db.py).

  usuario_cliente  El puente: qué cliente(s) ve una cuenta portal y con qué perfil.
                   Tabla puente y no un cliente_id en users porque una minera tiene
                   varios contactos y un contacto puede cubrir varios RUT o faenas; con
                   un 1:1 habría que reescribir el guard el día que eso pase.
  portal_acceso    Una fila por cuenta portal: desde cuándo está vigente y cuándo se usó
                   por última vez. De acá sale el corte automático por inactividad
                   (service.cuenta_vencida). Va aparte de `users` para no ensanchar una
                   tabla del núcleo con datos que solo le importan al portal.

  foto_articulo    La foto de un repuesto que VE EL CLIENTE. Tabla aparte de
                   recepcion_item_fotos (la evidencia interna de la recepción en bodega),
                   y no un interruptor "visible para el cliente" sobre esa misma tabla:
                   un interruptor se marca mal algún día, y ese error es silencioso e
                   irreversible (una foto ya descargada no se des-publica). La evidencia
                   de recepción puede mostrar un daño, la caja del fabricante o el
                   packing slip con el precio de compra; con dos tablas, publicarla no es
                   un error posible. Ver portal/fotos.py.
  portal_descarga_log  Quién descargó qué documento y cuándo. Se escribe ANTES de
                   entregar el archivo.

Las fechas son UTC naive (datetime.utcnow), igual que el resto de las marcas de tiempo
que el backend escribe a mano: inmunes al cambio de hora.
"""
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.sql import func

from database import Base

# Perfiles de una cuenta portal. 'bodega' existe para el bodeguero de faena: necesita
# ver qué guía viene en camino y NO debe ver montos. Sin este eje es todo o nada, y el
# cliente no le da cuenta a su bodeguero.
PERFIL_PROCUREMENT = "procurement"
PERFIL_BODEGA = "bodega"
PERFILES = (PERFIL_PROCUREMENT, PERFIL_BODEGA)


class UsuarioCliente(Base):
    __tablename__ = "usuario_cliente"
    __table_args__ = (
        UniqueConstraint("usuario_id", "cliente_id", name="uq_usuario_cliente"),
    )

    id = Column(Integer, primary_key=True, index=True)
    usuario_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    cliente_id = Column(Integer, ForeignKey("clientes.id"), nullable=False, index=True)
    perfil = Column(String(20), nullable=False, server_default=PERFIL_PROCUREMENT)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class PortalAcceso(Base):
    __tablename__ = "portal_acceso"

    usuario_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    # Desde cuándo corre el plazo de inactividad: la creación de la cuenta o su última
    # reactivación. Sin este campo, una cuenta recién reactivada volvería a nacer vencida
    # (su último acceso sigue siendo el de hace meses).
    vigente_desde = Column(DateTime, nullable=False)
    ultimo_acceso_at = Column(DateTime, nullable=True)
    # Desde cuándo la clave vigente es la TEMPORAL que entregó el personal (al crear la
    # cuenta o al restablecerla). NULL = el contacto ya puso la suya. Con esto la
    # temporal caduca (service.clave_temporal_vencida) en vez de quedar para siempre.
    clave_temporal_desde = Column(DateTime, nullable=True)
    creado_por_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class FotoArticulo(Base):
    __tablename__ = "foto_articulo"

    id = Column(Integer, primary_key=True, index=True)
    item_cotizacion_id = Column(
        Integer, ForeignKey("items_cotizacion.id"), nullable=False, index=True)
    # Nombre del archivo en disco (uuid4 + .jpg), dentro de portal/fotos.DIRECTORIO. Es
    # SIEMPRE el derivado: reducido y sin metadatos. El original del teléfono no se guarda.
    archivo = Column(String(64), nullable=False, unique=True)
    ancho = Column(Integer, nullable=False)
    alto = Column(Integer, nullable=False)
    peso_bytes = Column(Integer, nullable=False)
    subida_por_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False)


# Qué se descargó (portal_descarga_log.tipo) y a qué tabla apunta su referencia_id.
DESCARGA_GUIA_PDF = "guia_pdf"          # despachos.id
DESCARGA_GUIA_FIRMADA = "guia_firmada"  # despachos.id
DESCARGA_FACTURA_PDF = "factura_pdf"    # cont_factura_cliente.id


class PortalDescargaLog(Base):
    __tablename__ = "portal_descarga_log"

    id = Column(Integer, primary_key=True, index=True)
    usuario_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # Una de las constantes DESCARGA_* de arriba, con el id del documento en
    # referencia_id. Texto corto y no una FK por tipo: la bitácora tiene que sobrevivir
    # aunque el documento se anule o se borre.
    tipo = Column(String(20), nullable=False)
    referencia_id = Column(Integer, nullable=False)
    ip = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False)
