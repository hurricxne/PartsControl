"""EL CANDADO DEL CLIENTE: el filtro que decide qué órdenes ve una cuenta, en UN lugar.

Todo lo que el portal le muestra a un cliente cuelga de una orden (`oc_cliente`), y una
orden es suya si su cotización está anclada a una de sus fichas:

    Cotizacion.cliente_id IN scope.cliente_ids

Ese filtro vive SOLO acá. Las órdenes, las guías, las facturas y las fotos se consultan
pasando por `de_sus_ordenes`; ningún otro archivo del módulo escribe esa condición. Así,
para auditar el aislamiento basta con leer esta función y buscar quién consulta
`OcCliente` sin llamarla (portal/tests/test_ordenes.py lo hace por cada ruta).

Se filtra por el ENTERO cliente_id y por nada más: `cotizaciones.cliente` es texto libre
y `rut_cliente` es editable — filtrar por cualquiera de los dos sería un bypass.
`scope.cliente_ids` nunca llega vacío (portal/deps.py::require_portal responde 403 antes).
"""
from models.models import Cotizacion, OcCliente
from .deps import PortalScope


def de_sus_ordenes(consulta, scope: PortalScope):
    """Restringe a las órdenes del cliente una consulta que ya incluye a OcCliente
    (parte de ella o llegó a ella con un join)."""
    return (
        consulta
        .join(Cotizacion, Cotizacion.id == OcCliente.cotizacion_id)
        .filter(Cotizacion.cliente_id.in_(scope.cliente_ids))
    )
