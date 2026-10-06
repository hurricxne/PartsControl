"""Qué despachos, facturas y fotos EXISTEN para el cliente. Una sola definición de cada
regla, escrita como condición SQL.

Las usan el buscador, el estado de los ítems, el seguimiento, los documentos y la subida
de fotos. Si cada uno escribiera su propio filtro, tarde o temprano el buscador
encontraría una orden por el folio de una guía que después no aparece en la orden, o el
personal subiría fotos que el cliente nunca ve.

  · Un DESPACHO es visible cuando está cerrado ('despachado'). En preparación todavía no
    salió nada (aunque la guía electrónica ya esté emitida: se emite antes de cerrar);
    anulado, nunca salió.
  · El NÚMERO DE GUÍA de un despacho visible se muestra salvo mientras su guía
    electrónica está a medio emitir: en esa ventana el número que quedó tecleado en el
    despacho ya no es de fiar (lo va a reemplazar el folio del SII).
  · Una FACTURA es visible cuando está emitida (tiene folio) y es de Grupo AM. Sin folio
    es una emisión en curso o rechazada por el SII: no es un documento.
    A esta regla le falta una parte que no se puede escribir en SQL: la factura emitida
    a un RUT que ya no es el de la venta tampoco se muestra. Esa parte la aplica
    portal/documentos.py::facturas_visibles, y el buscador la respeta excluyendo esas
    facturas por id (documentos.facturas_vetadas_que_calzan).
  · Las FOTOS de un ítem se muestran salvo mientras el ítem está en reclamo al proveedor
    (lo fotografiado puede ser justo lo que falló). Por lo mismo, en ese estado tampoco
    se pueden subir.
"""
from sqlalchemy import and_, exists, or_

from models.models import ContFacturaCliente, Despacho, ItemCotizacion
from wasabil_dte.models import STATUS_EMITIDO, STATUS_FALLIDO, WasabilDte

ESTADO_DESPACHO_VISIBLE = "despachado"
EMPRESA_DEL_PORTAL = "mineria"
ESTADO_ITEM_EN_RECLAMO = "reclamo_proveedor"


def despacho_visible():
    """Condición SQL sobre Despacho."""
    return Despacho.estado == ESTADO_DESPACHO_VISIBLE


def guia_en_emision():
    """Condición SQL sobre Despacho: su guía electrónica existe y todavía no tiene folio
    del SII (ni fue rechazada). Es la misma regla con que documentos._folio_y_fecha deja
    el folio en blanco."""
    emitida_con_folio = and_(
        WasabilDte.status_id == STATUS_EMITIDO, WasabilDte.folio.isnot(None), WasabilDte.folio != "")
    rechazada = WasabilDte.status_id == STATUS_FALLIDO
    return exists().where(and_(
        WasabilDte.despacho_id == Despacho.id,
        # `IS NOT TRUE` y no `NOT (…)`: con status NULL (fila recién creada, todavía sin
        # respuesta de Wasabil) la comparación da NULL, y NOT NULL sigue siendo NULL: esa
        # guía, que es justo la que está a medio emitir, quedaría fuera.
        or_(emitida_con_folio, rechazada).is_not(True),
    ))


def guia_con_numero_a_la_vista():
    """Condición SQL sobre Despacho: es visible y su `numero_guia` es el folio que la
    orden muestra. La usa el buscador."""
    return and_(despacho_visible(), ~guia_en_emision())


def factura_emitida():
    """Condición SQL sobre ContFacturaCliente."""
    return and_(
        ContFacturaCliente.numero_factura.isnot(None),
        ContFacturaCliente.numero_factura != "",
        ContFacturaCliente.empresa == EMPRESA_DEL_PORTAL,
    )


def con_fotos_publicables():
    """Condición SQL sobre ItemCotizacion: las fotos de este ítem se le muestran al
    cliente. La usan el conteo, la lista y la entrega de la imagen, para que la lista no
    ofrezca una foto que después responde 404."""
    return or_(ItemCotizacion.estado_item.is_(None),
               ItemCotizacion.estado_item != ESTADO_ITEM_EN_RECLAMO)
