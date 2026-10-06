"""Mantiene cotizaciones.cliente_id al día en CADA escritura: gancho de sesión.

POR QUÉ UN GANCHO Y NO UNA LLAMADA EN CADA ROUTER
    El RUT de una cotización lo escriben al menos cinco caminos (carga del Excel,
    creación manual, venta en CLP, el editor con su setattr genérico y la facturación) y
    mañana habrá otro. Si el vínculo dependiera de acordarse de llamar a un helper en
    cada uno, el primero que se olvide deja una venta colgando de la ficha del RUT
    ANTERIOR — y el contacto de ese cliente la sigue viendo en su portal. El gancho corre
    en el flush, que es el único punto por el que pasan todos.

QUÉ HACE
    Antes de cada flush mira las cotizaciones NUEVAS y las que cambiaron `rut_cliente`,
    y les deja cliente_id = ficha de su RUT actual (o NULL). El destino sale del mapa de
    fichas, leído sin candado, y se CONFIRMA releyendo esa ficha con candado compartido
    sobre UNA fila de clientes (service.cliente_para_rut_confirmado): entre el mapa y la
    escritura otra persona puede haberle cambiado el RUT a la ficha o borrado. Nada más:
    una cotización a la que no le tocaron el RUT no se recalcula (para eso están el
    Cierre de Venta, el botón «Vincular ventas» y `python -m portal.vincular_clientes`).

LÍMITES CONOCIDOS
    · Solo ve escrituras por el ORM sobre la entidad. Un `query.update({...})` masivo o
      SQL crudo sobre rut_cliente NO pasa por acá (hoy no hay ninguno).
    · Se registra al importar este módulo. La app lo importa vía portal/service.py
      (main.py monta los routers del portal); un script suelto que escriba cotizaciones
      sin importar `portal` no lo tiene.

Dos fallas distintas. Si falla el CÁLCULO del mapa, las cotizaciones tocadas quedan SIN
vincular y el error va al log: ante la duda la venta desaparece del portal, nunca queda
a la vista del cliente equivocado. Si falla la CONFIRMACIÓN de la ficha (bloqueo mutuo,
espera agotada, conexión), falla la escritura completa: el flush se aborta y no se
escribe nada.
"""
import logging

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from models.models import Cotizacion

logger = logging.getLogger("portal")


def _cambio_el_rut(cot: Cotizacion) -> bool:
    return inspect(cot).attrs.rut_cliente.history.has_changes()


@event.listens_for(Session, "before_flush")
def _sincronizar_cliente_de_cotizaciones(session, flush_context, instances) -> None:
    tocadas = [o for o in session.new if isinstance(o, Cotizacion)]
    tocadas += [o for o in session.dirty if isinstance(o, Cotizacion) and _cambio_el_rut(o)]
    if not tocadas:
        return
    from .service import cliente_para_rut_confirmado, mapa_clientes  # local: service importa este módulo

    try:
        with session.no_autoflush:
            mapa, _ = mapa_clientes(session)
    except Exception:  # noqa: BLE001 — se registra y se falla cerrado, ver cabecera
        logger.exception("portal: no se pudo leer `clientes` para vincular %s cotización(es)", len(tocadas))
        mapa = {}
    confirmadas: dict = {}
    for cot in tocadas:
        # SIN try: si la base falla al confirmar la ficha (bloqueo, conexión), el flush se
        # aborta y no se escribe nada. Seguir después de un deadlock dejaría la
        # transacción deshecha en MySQL y la sesión escribiendo el resto en otra.
        with session.no_autoflush:
            destino = cliente_para_rut_confirmado(session, mapa, cot.rut_cliente, confirmadas)
        if cot.cliente_id != destino:
            cot.cliente_id = destino
