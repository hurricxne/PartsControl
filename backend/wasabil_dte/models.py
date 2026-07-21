"""Tabla del módulo Wasabil DTE — aislada y aditiva.

Tabla NUEVA (no toca ninguna existente):

  wasabil_dte → 1 fila por documento tributario electrónico (DTE) gestionado vía
                Wasabil. Hoy: guías de despacho (tipo 52) ligadas a un despacho.
                Mañana: facturas (tipo 33) ligadas a una factura de cliente
                (por eso factura_id ya existe, nullable).

La fila es el ancla ANTI DOBLE EMISIÓN: se crea ANTES de llamar a Wasabil y el
uuid que devuelve Wasabil se persiste de inmediato. El índice único por despacho
garantiza a nivel de BD que un despacho no puede tener dos guías electrónicas.
El folio real del SII se registra SOLO cuando el documento queda Emitido
(status_id 3) — nunca antes.

Se crea con el `Base.metadata.create_all()` de arranque (main.py importa el
router del módulo, que importa este archivo).
"""
from sqlalchemy import (
    Column, Integer, String, Numeric, Text, DateTime, ForeignKey, UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from database import Base

# status_id de Wasabil (espejo del API; ver README):
#   6 = Pendiente (borrador creado, NO enviado al SII, sin folio)
#   2 = Procesando (enviado al SII, esperando respuesta)
#   3 = Emitido   (aceptado por el SII; folio asignado; hay PDF/XML)
#   4 = Fallido   (rechazado por el SII; ver campo `error`)
STATUS_PENDIENTE = 6
STATUS_PROCESANDO = 2
STATUS_EMITIDO = 3
STATUS_FALLIDO = 4

STATUS_LABEL = {
    STATUS_PENDIENTE: "pendiente",
    STATUS_PROCESANDO: "procesando",
    STATUS_EMITIDO: "emitido",
    STATUS_FALLIDO: "fallido",
}

# Vida del claim "emisión en vuelo" (en_vuelo_desde): mientras esté fresco, NINGÚN
# otro request puede emitir/reintentar sobre el mismo despacho (anti doble emisión
# durante la ventana HTTP). Si la respuesta se perdió (timeout ambiguo), el claim
# expira solo y el reintento vuelve a estar disponible — que primero verifica en
# Wasabil si el documento llegó a crearse.
CLAIM_TTL_SEGUNDOS = 180


class WasabilDte(Base):
    """Un DTE emitido (o en proceso de emisión) a través de Wasabil."""
    __tablename__ = "wasabil_dte"
    __table_args__ = (
        # 1 guía electrónica por despacho, garantizado por la BD (los NULL de las
        # futuras filas de facturas no colisionan en MySQL).
        UniqueConstraint("despacho_id", name="uq_wasabil_dte_despacho"),
        # InnoDB explícito: SELECT ... FOR UPDATE (lock anti doble-emisión) lo requiere.
        {"mysql_engine": "InnoDB"},
    )

    id = Column(Integer, primary_key=True, index=True)
    empresa = Column(String(50), nullable=False, server_default="mineria")
    tipo_dte = Column(Integer, nullable=False, default=52)  # 52 guía · 33 factura (futuro)

    # Origen del documento (uno u otro según tipo_dte; nunca ambos)
    despacho_id = Column(Integer, ForeignKey("despachos.id"), nullable=True, index=True)
    factura_id = Column(Integer, ForeignKey("cont_factura_cliente.id"), nullable=True,
                        index=True)  # reservado para la fase de facturas (33)

    # Identidad y estado en Wasabil/SII
    uuid = Column(String(64), nullable=True, index=True)   # asignado por Wasabil al crear
    status_id = Column(Integer, nullable=True)             # 6|2|3|4 (None = aún sin respuesta)
    # Claim anti doble emisión: se marca (bajo lock) ANTES de llamar a Wasabil y se
    # limpia al obtener uuid o al confirmar que NO se creó documento (error 4xx /
    # conexión rechazada). Si la respuesta se perdió (timeout), queda puesto y
    # expira a los CLAIM_TTL_SEGUNDOS. UTC naive (datetime.utcnow(), solo lo
    # escribe/lee Python — inmune a cambios de hora local).
    en_vuelo_desde = Column(DateTime, nullable=True)
    folio = Column(String(100), nullable=True)             # folio SII (solo con status 3)
    pdf_url = Column(String(500), nullable=True)
    xml_url = Column(String(500), nullable=True)
    error = Column(Text, nullable=True)                    # display_error del SII o error de red

    # Montos CONGELADOS al emitir (calculados localmente con IVA half-up; la
    # respuesta cruda de Wasabil queda en respuesta_json — el cotejo fino de
    # semántica de campos se coteja en la primera emisión real, ver README)
    monto_neto = Column(Numeric(14, 2), default=0)
    iva = Column(Numeric(14, 2), default=0)
    monto_total = Column(Numeric(14, 2), default=0)

    # Trazabilidad: qué se envió y qué respondió Wasabil (auditoría / soporte)
    payload_json = Column(Text, nullable=True)
    respuesta_json = Column(Text, nullable=True)

    usuario_id = Column(Integer, ForeignKey("users.id"), nullable=True)  # quién emitió
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(),
                        onupdate=func.now())

    despacho = relationship("Despacho")
