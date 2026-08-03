"""Tablas del módulo Recepción Nacional MonzaParts — aisladas y aditivas.

Tablas NUEVAS (no tocan ninguna existente):

  monza_recepcion_nacional      → 1 fila por entrega física de un proveedor NACIONAL
                                  (su camión + su guía de despacho). Cabecera.
  monza_recepcion_nacional_item → cuánto llegó de cada ítem en esa entrega.

Se crean con `monza_recepcion_nacional/init_db.py` (standalone) o con el
`Base.metadata.create_all()` de arranque una vez cableado el módulo en main.py.

ADAPTACIÓN Monza (vs backend/recepcion_nacional/ de Grupo AM): NO existe tabla de
asignación OcProveedorItem — el vínculo ítem↔OC es directo vía
MonzaCotizacionItem.oc_proveedor_id (monza_models.py:311), por eso la línea de
recepción NO lleva oc_proveedor_item_id.
"""
from sqlalchemy import (
    Column, Integer, String, Numeric, Text, Date, DateTime, ForeignKey, Index,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from database import Base

# DEBE COINCIDIR VERBATIM con monza_router_despachos.py::_RECEPCION_UTILIZABLE.
# Estados cuyas unidades quedan UTILIZABLES en bodega (suman al tope físico).
# 'faltante' incluye lo que SÍ llegó bueno (el faltante se reclama aparte);
# 'no_llego' y 'danado_no_utilizable' aportan 0.
RECEPCION_UTILIZABLE = ("completo", "danado_utilizable", "sobrante", "faltante")

# Los 6 estados válidos que el operador puede marcar por ítem.
ESTADOS_VALIDOS = (
    "completo", "faltante", "sobrante",
    "danado_utilizable", "danado_no_utilizable", "no_llego",
)


class MonzaRecepcionNacional(Base):
    """Entrega física de un proveedor NACIONAL (su camión + su guía de despacho).

    A diferencia del embarque consolidado, NO clona líneas ni fuerza reclamos: es un
    simple libro de 'cuánto recibí' por ítem que el tope de Despachos acumula.
    Entregas sucesivas = filas nuevas sobre el remanente (el tope suma)."""
    __tablename__ = "monza_recepcion_nacional"
    __table_args__ = (
        Index("ix_monza_recep_nac_ocp", "oc_proveedor_id"),
        Index("ix_monza_recep_nac_estado", "estado"),
        # InnoDB explícito: SELECT ... FOR UPDATE (locks al cerrar/anular) lo requiere.
        {"mysql_engine": "InnoDB"},
    )

    id = Column(Integer, primary_key=True, index=True)
    # SET NULL: borrar la OC-Proveedor no borra la recepción (histórico de bodega).
    oc_proveedor_id = Column(Integer, ForeignKey("monza_oc_proveedor.id", ondelete="SET NULL"), nullable=True)
    numero_guia_proveedor = Column(String(120), nullable=True)   # guía de despacho del proveedor
    fecha = Column(Date, nullable=True)
    estado = Column(String(20), default="abierta")               # abierta | cerrada
    documento = Column(String(255), nullable=True)               # UUID.ext en uploads/docs (guía escaneada)
    observacion = Column(Text, nullable=True)
    usuario_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    fecha_cierre = Column(DateTime(timezone=True), nullable=True)

    items = relationship("MonzaRecepcionNacionalItem", back_populates="recepcion",
                         cascade="all, delete-orphan")


class MonzaRecepcionNacionalItem(Base):
    """Cuánto llegó de cada ítem. estado_recepcion usa el vocabulario VERBATIM del
    tope físico (RECEPCION_UTILIZABLE suma; no_llego/danado_no_utilizable = 0).

    SIN oc_proveedor_item_id (adaptación Monza): no hay tabla de asignación — la
    pertenencia ítem↔OC vive en MonzaCotizacionItem.oc_proveedor_id."""
    __tablename__ = "monza_recepcion_nacional_item"
    __table_args__ = (
        Index("ix_monza_recep_nac_item_recep", "recepcion_id"),
        Index("ix_monza_recep_nac_item_itemcot", "item_cotizacion_id"),
        {"mysql_engine": "InnoDB"},
    )

    id = Column(Integer, primary_key=True, index=True)
    recepcion_id = Column(Integer, ForeignKey("monza_recepcion_nacional.id", ondelete="CASCADE"), nullable=False)
    # SET NULL hacia Ventas/Compras: borrar allá no bloquea la recepción (histórico).
    item_cotizacion_id = Column(Integer, ForeignKey("monza_cotizacion_items.id", ondelete="SET NULL"), nullable=True)
    numero_parte = Column(String(100), nullable=True)            # snapshot
    descripcion = Column(String(500), nullable=True)             # snapshot
    qty_recibida = Column(Numeric(12, 4), default=0)
    estado_recepcion = Column(String(30), nullable=True)
    observacion = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    recepcion = relationship("MonzaRecepcionNacional", back_populates="items")
