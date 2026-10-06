"""El seguimiento de UN ítem: su línea de tiempo, sus documentos y sus fotos.

Responde la pregunta para la que existe el portal: ¿en qué va este repuesto y cuándo lo
comprometieron?

LAS FECHAS DE LOS HITOS SON HECHOS, NO ESTIMACIONES
    Cada hito cumplido lleva el día en que OCURRIÓ, tomado de la fila que lo registra:

      Pedido confirmado     oc_cliente.created_at            (el Cierre de Venta)
      En proceso de compra  oc_proveedor_items.fecha_asignacion
      Listo en origen       pre_embarque_items.created_at
      En nuestra bodega     recepciones_embarque.fecha_cierre / recepcion_nacional.fecha,
                            solo de una recepción cerrada donde el ítem SÍ llegó
      Despachado            despachos.fecha_despacho  (+ folio de la guía)
      Facturado             cont_factura_cliente.fecha_emision  (+ folio, si ve montos)
      No recibido en la     despachos.fecha_firma de la guía que el cliente firmó con
      entrega               faltante  (+ folio de esa guía)

    Los dos últimos salen de las mismas guías y facturas que se listan más abajo en la
    pantalla (portal/documentos.py): la línea de tiempo no puede nombrar un documento
    que la lista no muestra.

    «En tránsito», «En internación aduanera» y «Llegó a Chile, en recepción» NO tienen
    fecha: el estado del embarque se cambia a mano en su pantalla y la base no guarda
    cuándo. Se muestran como paso cumplido o actual, sin día. Un hito futuro nunca lleva
    fecha: el portal no estima cuándo va a llegar algo.

    De la compra sale solo la FECHA. El proveedor, su orden y su plazo no salen de acá.

Cuando el renglón está partido en varias filas, cada hito toma la fecha MÁS TEMPRANA
entre ellas: es cuando ese paso empezó a ser cierto para el repuesto.

EL RECORRIDO CORTO (sin «listo en origen», tránsito, aduana ni recepción) — ver
`_viaja_en_embarque`. La línea de tiempo marca como cumplido todo paso anterior al estado
actual, así que mostrar esos cuatro pasos es AFIRMAR que ocurrieron:
  · con embarque o pre-embarque registrado → recorrido completo (ocurrieron);
  · sin ellos y todavía sin llegar a bodega → completo salvo que se haya comprado en
    Chile (OC de origen 'nacional'): es el camino de casi todo lo que vende Grupo AM, y
    los pasos se ven como futuros;
  · sin ellos y ya en bodega o despachado → corto: llegó por otro camino (compra en
    Chile anterior al campo de origen, carga manual a bodega) y no hay registro de que
    haya pasado por aduana.
"""
from typing import Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from models.models import (
    Despacho, DespachoItem, EmbarqueItem, ItemCotizacion, OcCliente, OcProveedor, OcProveedorItem,
    PreEmbarqueItem, ReclamoProveedor, RecepcionEmbarque, RecepcionEmbarqueItem,
)
from recepcion_nacional.models import RecepcionNacional, RecepcionNacionalItem
from . import documentos, estados, renglones
from .candado import de_sus_ordenes
from .deps import PortalScope
from .models import FotoArticulo
from .tiempo import dia_chile, dia_tecleado, hoy_chile
from .visibles import con_fotos_publicables, despacho_visible

ORIGEN_NACIONAL = "nacional"


def _primera(db: Session, columna, *filtros, joins=()) -> Optional[object]:
    """El valor más temprano de `columna` que cumple los filtros (None si no hay)."""
    consulta = db.query(func.min(columna))
    for destino, condicion in joins:
        consulta = consulta.join(destino, condicion)
    return consulta.filter(*filtros).scalar()


def _llegada_a_bodega(db: Session, item_ids: List[int]) -> Optional[object]:
    """Cuándo llegó el ítem a bodega: el cierre de la primera recepción donde tuvo
    unidades utilizables (misma regla que renglones._recibido_en_bodega). Una recepción
    cerrada donde el ítem figura como «no llegó» no es una llegada."""
    por_embarque = _primera(
        db, RecepcionEmbarque.fecha_cierre,
        EmbarqueItem.item_cotizacion_id.in_(item_ids),
        RecepcionEmbarque.estado == renglones.RECEPCION_CERRADA,
        RecepcionEmbarqueItem.estado_recepcion.in_(renglones.RECEPCION_UTILIZABLE),
        RecepcionEmbarqueItem.qty_recibida > 0,
        joins=((RecepcionEmbarqueItem, RecepcionEmbarqueItem.recepcion_id == RecepcionEmbarque.id),
               (EmbarqueItem, EmbarqueItem.id == RecepcionEmbarqueItem.embarque_item_id)))
    if por_embarque is not None:
        return por_embarque
    return _primera(
        db, RecepcionNacional.fecha,
        RecepcionNacionalItem.item_cotizacion_id.in_(item_ids),
        RecepcionNacional.estado == renglones.RECEPCION_CERRADA,
        RecepcionNacionalItem.estado_recepcion.in_(renglones.RECEPCION_UTILIZABLE),
        RecepcionNacionalItem.qty_recibida > 0,
        joins=((RecepcionNacionalItem, RecepcionNacionalItem.recepcion_id == RecepcionNacional.id),))


def _fechas_y_detalles(db: Session, scope: PortalScope, oc: OcCliente, renglon: renglones.Renglon,
                       guias: List[dict], datos: renglones.Datos) -> tuple:
    """({hito: día en que ocurrió}, {hito: detalle}) del renglón. `guias` son las suyas,
    tal como se le listan al cliente."""
    ids = list(renglon.item_ids)
    fechas: Dict[str, Optional[str]] = {"confirmado": dia_chile(oc.created_at)}
    detalles: Dict[str, str] = {}

    fechas["en_compra"] = dia_chile(_primera(
        db, func.coalesce(OcProveedorItem.fecha_asignacion, OcProveedorItem.created_at),
        OcProveedorItem.item_cotizacion_id.in_(ids)))
    fechas["listo_origen"] = dia_chile(_primera(
        db, PreEmbarqueItem.created_at, PreEmbarqueItem.item_cotizacion_id.in_(ids)))
    fechas["en_bodega"] = dia_chile(_llegada_a_bodega(db, ids))

    if guias:
        primera_guia = guias[-1]  # vienen de la más reciente a la más antigua
        fechas["despachado"] = primera_guia["despachada_el"]
        if primera_guia["folio"]:
            detalles["despachado"] = f"Guía {primera_guia['folio']}"

    facturas = [datos.facturado[i].primera for i in ids if i in datos.facturado]
    if facturas:
        primera_factura = min(facturas, key=documentos.orden_de_emision)
        if primera_factura.fecha_emision:
            fechas["facturado"] = primera_factura.fecha_emision.isoformat()
        if scope.ve_montos:
            detalles["facturado"] = f"Factura {primera_factura.numero_factura}"

    fechas[estados.EN_GESTION] = dia_chile(_primera(
        db, ReclamoProveedor.fecha_creacion, ReclamoProveedor.item_cotizacion_id.in_(ids)))

    firmada_con_faltante = _primera_guia_con_faltante(db, ids)
    if firmada_con_faltante is not None:
        despacho_id, firma = firmada_con_faltante
        fechas[estados.NO_RECIBIDO] = dia_tecleado(firma)
        folio = next((g["folio"] for g in guias if g["id"] == despacho_id), "")
        if folio:
            detalles[estados.NO_RECIBIDO] = f"Guía {folio}"
    return fechas, detalles


def _primera_guia_con_faltante(db: Session, item_ids: List[int]) -> Optional[tuple]:
    """(despacho_id, fecha de la firma) de la primera guía en que el cliente firmó menos
    de lo que viajó de esos ítems; None si no hay."""
    return (
        db.query(Despacho.id, Despacho.fecha_firma)
        .join(DespachoItem, DespachoItem.despacho_id == Despacho.id)
        .filter(DespachoItem.item_cotizacion_id.in_(item_ids), despacho_visible(),
                renglones.firmado_con_faltante())
        .order_by(Despacho.fecha_firma.asc(), Despacho.id.asc()).first()
    )


# Hitos que solo tienen fecha si el ítem ya pasó por bodega.
_DESDE_BODEGA = ("en_bodega", "despachado", "facturado")


def _viaja_en_embarque(db: Session, item_ids: List[int], ya_llego: bool) -> bool:
    """¿La línea de tiempo del ítem lleva los pasos de embarque? Ver «EL RECORRIDO
    CORTO». `ya_llego` = el ítem ya está en bodega o más adelante."""
    pasa_por_embarque = (
        db.query(EmbarqueItem.id).filter(EmbarqueItem.item_cotizacion_id.in_(item_ids)).first()
        or db.query(PreEmbarqueItem.id).filter(PreEmbarqueItem.item_cotizacion_id.in_(item_ids)).first()
    )
    if pasa_por_embarque:
        return True
    if ya_llego:
        return False
    comprado_en_chile = (
        db.query(OcProveedorItem.id)
        .join(OcProveedor, OcProveedor.id == OcProveedorItem.oc_proveedor_id)
        .filter(OcProveedorItem.item_cotizacion_id.in_(item_ids),
                OcProveedor.tipo_origen == ORIGEN_NACIONAL).first()
    )
    return not comprado_en_chile


def de_item(db: Session, scope: PortalScope, oc: OcCliente, item_id: int) -> Optional[dict]:
    """El seguimiento del renglón al que pertenece `item_id`, o None si ese ítem no es de
    la orden `oc` (que YA pasó el candado del cliente en ordenes.buscar_orden)."""
    datos = renglones.cargar(db, scope, [oc])
    renglon = next((r for r in renglones.lineas(oc, datos, hoy_chile()) if item_id in r.item_ids), None)
    if renglon is None:
        return None
    suyos = set(renglon.item_ids)
    guias = documentos.guias(db, scope, [oc], solo_items=suyos)
    fechas, detalles = _fechas_y_detalles(db, scope, oc, renglon, guias, datos)
    # Con una incidencia el estado no dice hasta dónde llegó el ítem: lo dicen las fechas.
    ya_llego = (estados.posicion(renglon.estado.clave) >= estados.posicion("en_bodega")
                or any(fechas.get(hito) for hito in _DESDE_BODEGA))
    hitos = estados.linea_de_tiempo(
        renglon.estado.clave, fechas, detalles, _viaja_en_embarque(db, list(suyos), ya_llego))

    fotos = (db.query(FotoArticulo)
             .join(ItemCotizacion, ItemCotizacion.id == FotoArticulo.item_cotizacion_id)
             .filter(FotoArticulo.item_cotizacion_id.in_(suyos), con_fotos_publicables())
             .order_by(FotoArticulo.created_at.asc(), FotoArticulo.id.asc()).all())
    return {
        "orden": {"id": oc.id, "numero_oc": oc.numero_oc or ""},
        "item": renglon.publico(),
        "hitos": [
            {"clave": h.clave, "etiqueta": h.etiqueta, "cumplido": h.cumplido,
             "actual": h.actual, "fecha": h.fecha, "detalle": h.detalle}
            for h in hitos
        ],
        "guias": guias,
        "facturas": documentos.facturas(db, scope, datos.facturas, solo_items=suyos),
        "fotos": [
            {"id": f.id, "ancho": f.ancho, "alto": f.alto, "fecha": dia_chile(f.created_at)}
            for f in fotos
        ],
    }


def foto_del_cliente(db: Session, scope: PortalScope, foto_id: int) -> Optional[FotoArticulo]:
    """La foto `foto_id` SI es de un ítem de una venta del cliente y ese ítem muestra
    sus fotos (visibles.con_fotos_publicables, la misma regla de la lista); None en
    cualquier otro caso. El router responde 404: los ids son correlativos y no se puede
    dejar que sirvan para tantear fotos ajenas."""
    return (
        de_sus_ordenes(
            db.query(FotoArticulo)
            .join(ItemCotizacion, ItemCotizacion.id == FotoArticulo.item_cotizacion_id)
            .join(OcCliente, OcCliente.cotizacion_id == ItemCotizacion.cotizacion_id),
            scope,
        )
        .filter(FotoArticulo.id == foto_id, con_fotos_publicables()).first()
    )
