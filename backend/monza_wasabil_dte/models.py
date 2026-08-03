"""Tabla del módulo Wasabil DTE de MonzaParts — aislada y aditiva.

Tabla NUEVA (no toca ninguna existente):

  monza_wasabil_dte → 1 fila por documento tributario electrónico (DTE) de
                      MonzaParts gestionado vía Wasabil. Fase 5: guías de
                      despacho (tipo 52) ligadas a un despacho Monza.
                      Fase 6 (Fase B en GA): facturas electrónicas (tipo 33)
                      ligadas a una factura de cliente Monza.

Guía y factura conviven en la MISMA tabla, discriminadas por `tipo_dte` (52|33)
y por cuál de los dos orígenes viene poblado (despacho_id XOR factura_id).

La fila es el ancla ANTI DOBLE EMISIÓN: se crea ANTES de llamar a Wasabil y el
uuid que devuelve Wasabil se persiste de inmediato. Los índices únicos por
despacho y por factura garantizan a nivel de BD que un despacho no puede tener
dos guías electrónicas ni una factura dos facturas electrónicas. Los dos UNIQUE
conviven porque en MySQL los NULL no colisionan entre sí.
El folio real del SII se registra SOLO cuando el documento queda Emitido
(status_id 3) — nunca antes.

Espejo de wasabil_dte/models.py de Grupo AM con las adaptaciones Monza:
FK a monza_despachos.id / monza_cont_factura_cliente.id y empresa
server_default='automotriz' (trampa n°1 del server_default olvidado: una fila
insertada sin empresa NO debe nacer 'mineria').
"""
from sqlalchemy import (
    Column, Integer, String, Numeric, Text, DateTime, ForeignKey, UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from database import Base
# POR QUÉ este import aquí: la FK `factura_id` y el relationship apuntan a
# MonzaContFacturaCliente, que vive en otro paquete monza_*. Este módulo se
# importa también con MONZA_CONTAB_ENABLED apagado (el guard de despachos hace
# un import LOCAL de MonzaWasabilDte para no dejar anular una guía SII viva), y
# ahí monza_contabilidad NO está cargado. Sin registrar su modelo, el primer
# configure_mappers() reventaría con "failed to locate a name" y tumbaría CUALQUIER
# query ORM del proceso. Importar el módulo de TABLAS (nunca el router) no crea
# ciclo: monza_contabilidad/models.py solo importa sqlalchemy + database.
import monza_contabilidad.models  # noqa: F401

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


class MonzaWasabilDte(Base):
    """Un DTE de MonzaParts emitido (o en proceso de emisión) a través de Wasabil."""
    __tablename__ = "monza_wasabil_dte"
    __table_args__ = (
        # 1 guía electrónica por despacho, garantizado por la BD (nivel más bajo
        # del anti doble emisión: aunque fallara todo lo demás, el INSERT gemelo
        # revienta con IntegrityError → 409).
        UniqueConstraint("despacho_id", name="uq_monza_wasabil_dte_despacho"),
        # 1 factura electrónica por factura de cliente (Fase 6, DTE 33): el MISMO
        # candado anti doble emisión, ahora por el otro origen. OJO: este UNIQUE
        # protege una factura YA creada; NO cubre el doble clic en "emitir factura
        # nueva" (cada request crearía una factura con id distinto) — para eso está
        # el candado de intención por cotización en el router.
        UniqueConstraint("factura_id", name="uq_monza_wasabil_dte_factura"),
        # InnoDB explícito: SELECT ... FOR UPDATE (lock anti doble-emisión) lo requiere.
        {"mysql_engine": "InnoDB"},
    )

    id = Column(Integer, primary_key=True, index=True)
    # server_default 'automotriz' (NO 'mineria'): candado de marca a nivel de dato,
    # además del require_empresa('automotriz') del router.
    empresa = Column(String(50), nullable=False, server_default="automotriz")
    tipo_dte = Column(Integer, nullable=False, default=52)  # 52 guía · 33 factura

    # Origen del documento (uno u otro según tipo_dte; NUNCA ambos). Ambos nullable
    # como en GA: las filas de guía llevan factura_id NULL y las de factura llevan
    # despacho_id NULL. Los NULL no colisionan en los UNIQUE de MySQL, así que los
    # dos candados conviven en la misma tabla.
    despacho_id = Column(Integer, ForeignKey("monza_despachos.id"), nullable=True,
                         index=True)
    factura_id = Column(Integer, ForeignKey("monza_cont_factura_cliente.id"),
                        nullable=True, index=True)  # Fase 6: factura electrónica (33)

    # Identidad y estado en Wasabil/SII
    uuid = Column(String(64), nullable=True, index=True)   # asignado por Wasabil al crear
    status_id = Column(Integer, nullable=True)             # 6|2|3|4 (None = aún sin respuesta)
    # Claim anti doble emisión: se marca (bajo lock) ANTES de llamar a Wasabil y se
    # limpia al obtener uuid o al confirmar que NO se creó documento (error 4xx /
    # conexión rechazada). Si la respuesta se perdió (timeout), queda puesto y
    # expira a los CLAIM_TTL_SEGUNDOS. UTC naive (datetime.utcnow(), solo lo
    # escribe/lee Python — inmune a cambios de hora local chilena).
    en_vuelo_desde = Column(DateTime, nullable=True)
    folio = Column(String(100), nullable=True)             # folio SII (solo con status 3)
    pdf_url = Column(String(500), nullable=True)
    xml_url = Column(String(500), nullable=True)
    error = Column(Text, nullable=True)                    # display_error del SII o error de red

    # Montos CONGELADOS al emitir: neto desde el precio congelado de la venta
    # (MonzaCotizacionItem.precio_unitario_clp) e IVA half-up con la TASA DE LA
    # VENTA (iva_pct congelado; el 0.19 es solo el fallback) — la misma cuadratura
    # que usará la factura Monza. La respuesta cruda de Wasabil queda en
    # respuesta_json para auditoría.
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

    despacho = relationship("MonzaDespacho")
    factura = relationship("MonzaContFacturaCliente")
