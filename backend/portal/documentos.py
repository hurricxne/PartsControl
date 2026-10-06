"""Las guías de despacho y las facturas de una venta, como DOCUMENTOS del pedido: cuáles
existen para el cliente y qué se dice de cada una. La entrega de los archivos (PDF, guía
firmada) está en portal/descargas.py.

QUÉ SE MUESTRA (qué despacho y qué factura "existen" lo define portal/visibles.py)
  · Guías: los despachos cerrados de la venta. Folio, fecha, qué repuestos y cuántos
    viajaron, cómo las recibió el cliente, y si hay archivo: el PDF de la guía
    electrónica (DTE 52) y la guía firmada escaneada.
  · Facturas: las emitidas de la venta. Folio, fecha, neto, IVA, total y vencimiento, y
    el PDF si se emitió electrónicamente (DTE 33).

QUÉ NO SE MUESTRA, POR DECISIÓN DEL DUEÑO
    Saldos, pagos, estado de pago, factoring y notas de crédito. Una factura acá es un
    documento ("existe, este es su folio"), no una cuenta por cobrar. Con perfil 'bodega'
    (`scope.ve_montos` falso) no hay facturas ni archivos: el PDF de la guía y la guía
    firmada traen precios.
    OJO: el ERP no registra las notas de crédito (se emiten directo en Wasabil), así que
    una factura anulada con nota de crédito sigue apareciendo acá como emitida.

LA VENTA, NO LA FILA oc_cliente
    Los documentos se cargan por la COTIZACIÓN de las órdenes que se muestran. Si una
    venta tuviera más de una fila oc_cliente (pasó antes de que el Cierre fuera
    idempotente), el portal muestra una sola orden —portal/ordenes.py::base— con las
    guías y facturas de todas.

DOCUMENTO EMITIDO A OTRO RUT
    Una factura que consta emitida a un RUT que no es el de la venta no se muestra, y los
    archivos de un documento electrónico solo se ofrecen si consta que fue al RUT de la
    venta. Las dos reglas, y de dónde sale la evidencia, están en portal/dtes.py.

CUIDADO AL LLAMAR: `guias`, `facturas_visibles`, `facturado_por_item` y `folios` reciben
órdenes que YA pasaron el candado del cliente (portal/ordenes.py::base). No filtran por
cliente: quien las llame con órdenes ajenas, muestra documentos ajenos. Las únicas
funciones de acá que reciben un id tecleado por el cliente y lo resuelven contra sus
órdenes son `despacho_del_cliente` y `factura_del_cliente`.
"""
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from models.models import (
    ContFacturaCliente, ContFacturaClienteItem, Cotizacion, Despacho, DespachoItem,
    ItemCotizacion, OcCliente,
)
from wasabil_dte.models import STATUS_FALLIDO, WasabilDte
from . import busqueda, dtes
from .candado import de_sus_ordenes
from .deps import PortalScope
from .tiempo import dia_chile, dia_tecleado
from .valor import LineaFacturada
from .visibles import despacho_visible, factura_emitida

# Adjuntos del ERP (routers/compras.py::upload_doc → uploads/docs). Es una carpeta
# COMPARTIDA: ahí también están el AWB, la factura comercial y el packing list del
# proveedor. Por eso el portal jamás recibe ni devuelve un nombre de archivo: llega por
# el id del despacho y solo sirve lo que ese despacho apunta como guía firmada.
DIRECTORIO_DOCS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "uploads", "docs"))

# Lista cerrada: la subida del ERP acepta también .doc y .xls, y eso no es una guía
# firmada. El nombre debe tener la forma exacta que genera la subida (uuid4 en hex).
_ARCHIVO_GUIA_FIRMADA = re.compile(r"^[0-9a-f]{32}\.(pdf|jpg|jpeg|png)$")
TIPOS_GUIA_FIRMADA = {"pdf": "application/pdf", "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}

# Cómo recibió el cliente una guía.
RECEPCION_PENDIENTE = "pendiente"        # todavía no hay guía firmada
RECEPCION_CONFORME = "conforme"          # firmó todo lo que viajó
RECEPCION_CON_FALTANTE = "con_faltante"  # firmó menos de lo que viajó en alguna línea
TOLERANCIA = 0.001


def _por_cotizacion(ocs: List[OcCliente]) -> Tuple[Dict[int, int], Dict[int, Optional[str]]]:
    """({cotizacion_id: id de la orden que se muestra}, {cotizacion_id: RUT de la venta})."""
    return ({oc.cotizacion_id: oc.id for oc in ocs},
            {oc.cotizacion_id: oc.cotizacion.rut_cliente for oc in ocs})


# ── Guías ──────────────────────────────────────────────────────────────────────────────

def _ruta_guia_firmada(despacho: Despacho) -> Optional[str]:
    nombre = (despacho.guia_firmada_archivo or "").lower()
    if not despacho.guia_firmada or not _ARCHIVO_GUIA_FIRMADA.match(nombre):
        return None
    ruta = os.path.abspath(os.path.join(DIRECTORIO_DOCS, despacho.guia_firmada_archivo))
    if not ruta.startswith(DIRECTORIO_DOCS + os.sep) or not os.path.isfile(ruta):
        return None
    return ruta


def _oferta_de_archivos(despacho: Despacho, dte: Optional[WasabilDte],
                        rut_venta: Optional[str]) -> Tuple[bool, Optional[str]]:
    """(¿hay PDF de guía electrónica?, ruta de la guía firmada o None). La MISMA regla
    para lo que la lista ofrece y lo que la descarga entrega."""
    if not dtes.archivos_permitidos(dte, rut_venta):
        return False, None
    return dtes.emitido(dte), _ruta_guia_firmada(despacho)


def _folio_y_fecha(despacho: Despacho, dte: Optional[WasabilDte]) -> Tuple[str, Optional[str]]:
    """(folio, fecha AAAA-MM-DD) de la guía como documento.

    Guía electrónica emitida: su folio del SII y su fecha tributaria. Emisión a medio
    camino: el número que quedó en el despacho ya no es de fiar; no se muestra folio
    hasta que llegue el del SII (misma regla, en SQL, en visibles.guia_en_emision). Guía
    en papel (sin DTE, o DTE rechazado): lo que tecleó el operador, y si no tecleó
    fecha, el día en que salió el despacho.
    """
    salida = dia_chile(despacho.fecha_despacho)
    if dtes.emitido(dte):
        return dte.folio, dtes.fecha_del_documento(dte) or salida
    if dte is not None and dte.status_id != STATUS_FALLIDO:
        return "", salida
    fecha = despacho.fecha_guia.isoformat() if despacho.fecha_guia else salida
    return despacho.numero_guia or "", fecha


def _despachos_visibles(db: Session, cot_ids: List[int]) -> List[Tuple[Despacho, int]]:
    """[(despacho cerrado, cotizacion_id)] de esas ventas, el más reciente primero."""
    return (
        db.query(Despacho, OcCliente.cotizacion_id)
        .join(OcCliente, OcCliente.id == Despacho.oc_cliente_id)
        .filter(OcCliente.cotizacion_id.in_(cot_ids), despacho_visible())
        .order_by(Despacho.fecha_despacho.desc(), Despacho.id.desc()).all()
    )


def _contenido_de(db: Session, despachos: List[Despacho]) -> Dict[int, List[dict]]:
    """{despacho_id: sus líneas}. Cada línea lleva además `_item_id` (se quita antes de
    responder) para poder filtrar las guías de un ítem."""
    firmados = {d.id for d in despachos if d.guia_firmada}
    contenido: Dict[int, List[dict]] = {}
    if not despachos:
        return contenido
    for di, it in (
        db.query(DespachoItem, ItemCotizacion)
        .join(ItemCotizacion, ItemCotizacion.id == DespachoItem.item_cotizacion_id)
        .filter(DespachoItem.despacho_id.in_([d.id for d in despachos]))
        .order_by(DespachoItem.id).all()
    ):
        viajaron = float(di.qty_despachada or 0)
        recibidas = None
        if di.despacho_id in firmados:
            # Lo que el cliente firmó como recibido. NULL = firmó todo; 0 es un valor
            # legítimo (declaró no haber recibido esa línea): por eso `is not None` y no
            # `or`, que publicaría un 0 como entrega completa.
            recibidas = float(di.qty_firmada) if di.qty_firmada is not None else viajaron
        contenido.setdefault(di.despacho_id, []).append({
            "_item_id": it.id,
            "numero_parte": it.numero_parte or "",
            "descripcion": it.descripcion or it.nombre_cat or "",
            "cantidad": viajaron,
            # None mientras la guía no esté firmada: nadie ha dicho todavía qué recibió.
            "cantidad_recibida": recibidas,
        })
    return contenido


def _recepcion(despacho: Despacho, lineas: List[dict]) -> str:
    if not despacho.guia_firmada:
        return RECEPCION_PENDIENTE
    falto_algo = any(l["cantidad_recibida"] < l["cantidad"] - TOLERANCIA for l in lineas)
    return RECEPCION_CON_FALTANTE if falto_algo else RECEPCION_CONFORME


def guias(db: Session, scope: PortalScope, ocs: List[OcCliente],
          solo_items: Optional[set] = None) -> List[dict]:
    """Guías de las ventas de `ocs`, la más reciente primero. Con `solo_items` (aunque
    sea un conjunto vacío), solo las que llevan alguno de esos ítems."""
    orden_de, rut_de = _por_cotizacion(ocs)
    if not orden_de:
        return []
    filas = _despachos_visibles(db, list(orden_de))
    contenido = _contenido_de(db, [d for d, _ in filas])
    de_guia = dtes.de_documentos(db, despacho_ids=[d.id for d, _ in filas])
    salida = []
    for d, cot_id in filas:
        lineas = contenido.get(d.id, [])
        if solo_items is not None and not any(l["_item_id"] in solo_items for l in lineas):
            continue
        dte = de_guia.get(("guia", d.id))
        folio, fecha = _folio_y_fecha(d, dte)
        hay_pdf, ruta_firmada = _oferta_de_archivos(d, dte, rut_de.get(cot_id))
        salida.append({
            "id": d.id,
            "orden_id": orden_de[cot_id],
            "folio": folio,
            "fecha": fecha,
            "despachada_el": dia_chile(d.fecha_despacho),
            "recepcion": _recepcion(d, lineas),
            "fecha_recepcion": dia_tecleado(d.fecha_firma) if d.guia_firmada else None,
            "items": [{k: v for k, v in l.items() if k != "_item_id"} for l in lineas],
            "pdf": scope.ve_montos and hay_pdf,
            "firmada": scope.ve_montos and ruta_firmada is not None,
        })
    return salida


# ── Facturas ───────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FacturaVisible:
    """Una factura que existe para el cliente, con lo que hace falta para mostrarla."""
    factura: ContFacturaCliente
    orden_id: int       # la orden con que el portal muestra esa venta
    con_pdf: bool       # hay DTE emitido y sus archivos se pueden ofrecer


@dataclass(frozen=True)
class Facturado:
    """Lo que ya se facturó de un ítem."""
    primera: ContFacturaCliente          # la factura más antigua que lo incluye
    lineas: Tuple[LineaFacturada, ...]   # sus líneas en todas las facturas que el cliente ve

    @property
    def cantidad(self) -> float:
        return sum(linea.cantidad for linea in self.lineas)


def orden_de_emision(factura: ContFacturaCliente) -> tuple:
    """Clave para ordenar facturas de la más antigua a la más nueva (sin fecha, al final)."""
    return (factura.fecha_emision is None, factura.fecha_emision, factura.id)


def facturas_visibles(db: Session, ocs: List[OcCliente]) -> List[FacturaVisible]:
    """Las facturas de esas ventas que EXISTEN para el cliente (emitidas y no emitidas a
    otro RUT), la más reciente primero. No mira el perfil: eso lo decide quien las muestra."""
    orden_de, rut_de = _por_cotizacion(ocs)
    if not orden_de:
        return []
    filas = (
        db.query(ContFacturaCliente, OcCliente.cotizacion_id)
        .join(OcCliente, OcCliente.id == ContFacturaCliente.oc_cliente_id)
        .filter(OcCliente.cotizacion_id.in_(list(orden_de)), factura_emitida())
        .order_by(ContFacturaCliente.fecha_emision.desc(), ContFacturaCliente.id.desc()).all()
    )
    de_factura = dtes.de_documentos(db, factura_ids=[f.id for f, _ in filas])
    visibles = []
    for f, cot_id in filas:
        dte, rut = de_factura.get(("factura", f.id)), rut_de.get(cot_id)
        if dtes.emitido_a_otro(dte, rut):
            continue
        visibles.append(FacturaVisible(
            factura=f, orden_id=orden_de[cot_id],
            con_pdf=dtes.emitido(dte) and dtes.archivos_permitidos(dte, rut)))
    return visibles


def facturado_por_item(db: Session, visibles: List[FacturaVisible]) -> Dict[int, Facturado]:
    """{item_id: lo facturado de ese ítem}, mirando solo las facturas visibles. Una
    factura de anticipo no cuenta (respalda un adelanto, no factura el repuesto), ni una
    línea de descuento de anticipo, ni una línea sin cantidad."""
    por_id = {v.factura.id: v.factura for v in visibles if not v.factura.es_anticipo}
    if not por_id:
        return {}
    de_item: Dict[int, List[Tuple[ContFacturaCliente, LineaFacturada]]] = {}
    for li in (
        db.query(ContFacturaClienteItem)
        .filter(ContFacturaClienteItem.factura_id.in_(list(por_id)),
                ContFacturaClienteItem.item_cotizacion_id.isnot(None),
                ContFacturaClienteItem.anticipo_factura_id.is_(None))
        .order_by(ContFacturaClienteItem.id).all()
    ):
        cantidad = float(li.cantidad or 0)
        if cantidad <= 0:
            continue
        de_item.setdefault(li.item_cotizacion_id, []).append((por_id[li.factura_id], LineaFacturada(
            cantidad=cantidad, neto=float(li.total_neto or 0),
            unitario=float(li.precio_unit_neto or 0), despacho_item_id=li.despacho_item_id)))
    return {
        item_id: Facturado(
            primera=min((factura for factura, _ in suyas), key=orden_de_emision),
            lineas=tuple(linea for _, linea in suyas))
        for item_id, suyas in de_item.items()
    }


def facturas(db: Session, scope: PortalScope, visibles: List[FacturaVisible],
             solo_items: Optional[set] = None) -> List[dict]:
    """Las facturas como se le muestran al cliente. Con `solo_items`, solo las que
    tienen alguna línea de esos ítems. Con perfil 'bodega', ninguna."""
    if not scope.ve_montos:
        return []
    if solo_items is not None:
        visibles = [v for v in visibles
                    if any(li.item_cotizacion_id in solo_items for li in v.factura.items)]
    return [
        {
            "id": v.factura.id,
            "orden_id": v.orden_id,
            "folio": v.factura.numero_factura,
            "fecha": v.factura.fecha_emision.isoformat() if v.factura.fecha_emision else None,
            "vencimiento": (v.factura.fecha_vencimiento.isoformat()
                            if v.factura.fecha_vencimiento else None),
            "neto_clp": int(round(float(v.factura.monto_neto or 0))),
            "iva_clp": int(round(float(v.factura.iva or 0))),
            "total_clp": int(round(float(v.factura.monto_bruto or 0))),
            "es_anticipo": bool(v.factura.es_anticipo),
            "pdf": v.con_pdf,
        }
        for v in visibles
    ]


def facturado_neto(visibles: List[FacturaVisible]) -> int:
    """Σ neto de esas facturas. Una factura de anticipo suma su neto y la factura final
    lo descuenta con una línea negativa: la suma de todas es lo facturado de verdad."""
    return sum(int(round(float(v.factura.monto_neto or 0))) for v in visibles)


# ── Lo que usa el buscador ──────────────────────────────────────────────────────────────

def folios(db: Session, scope: PortalScope, ocs: List[OcCliente],
           visibles: List[FacturaVisible]) -> Dict[int, List[dict]]:
    """{orden_id: [{tipo: 'guia'|'factura', id, folio}]}: los folios que cada orden
    MUESTRA (los mismos que arma `guias` y `facturas`), para decir por cuál calzó la
    búsqueda. Las facturas, solo si la cuenta las ve."""
    orden_de, _ = _por_cotizacion(ocs)
    por_orden: Dict[int, List[dict]] = {}
    if not orden_de:
        return por_orden
    filas = _despachos_visibles(db, list(orden_de))
    de_guia = dtes.de_documentos(db, despacho_ids=[d.id for d, _ in filas])
    for d, cot_id in filas:
        folio, _ = _folio_y_fecha(d, de_guia.get(("guia", d.id)))
        if folio:
            por_orden.setdefault(orden_de[cot_id], []).append({"tipo": "guia", "id": d.id, "folio": folio})
    if scope.ve_montos:
        for v in visibles:
            por_orden.setdefault(v.orden_id, []).append(
                {"tipo": "factura", "id": v.factura.id, "folio": v.factura.numero_factura})
    return por_orden


def facturas_vetadas_que_calzan(db: Session, scope: PortalScope, palabras: List[str]) -> set:
    """ids de las facturas del cliente cuyo folio calza con lo buscado y que NO se le
    muestran por haberse emitido a otro RUT. El buscador las excluye: sin esto encontraría
    una orden por el folio de una factura que la orden no muestra."""
    candidatas = (
        de_sus_ordenes(
            db.query(ContFacturaCliente.id, Cotizacion.rut_cliente)
            .join(OcCliente, OcCliente.id == ContFacturaCliente.oc_cliente_id), scope)
        .filter(factura_emitida(), busqueda.calza_con_alguna(ContFacturaCliente.numero_factura, palabras))
        .all()
    )
    if not candidatas:
        return set()
    de_factura = dtes.de_documentos(db, factura_ids=[factura_id for factura_id, _ in candidatas])
    return {factura_id for factura_id, rut in candidatas
            if dtes.emitido_a_otro(de_factura.get(("factura", factura_id)), rut)}


# ── Un documento pedido por su id: se resuelve contra las órdenes del cliente ───────────

def despacho_del_cliente(db: Session, scope: PortalScope, despacho_id: int) -> Optional[Despacho]:
    """El despacho SI es de una venta del cliente y su perfil puede bajar archivos."""
    if not scope.ve_montos:
        return None
    return de_sus_ordenes(
        db.query(Despacho).join(OcCliente, OcCliente.id == Despacho.oc_cliente_id), scope,
    ).filter(Despacho.id == despacho_id, despacho_visible()).first()


def factura_del_cliente(db: Session, scope: PortalScope, factura_id: int) -> Optional[ContFacturaCliente]:
    """La factura SI es de una venta del cliente, está emitida y su perfil ve facturas."""
    if not scope.ve_montos:
        return None
    return de_sus_ordenes(
        db.query(ContFacturaCliente).join(OcCliente, OcCliente.id == ContFacturaCliente.oc_cliente_id),
        scope,
    ).filter(ContFacturaCliente.id == factura_id, factura_emitida()).first()


def _rut_de_la_venta(db: Session, oc_cliente_id: int) -> Optional[str]:
    return (
        db.query(Cotizacion.rut_cliente)
        .join(OcCliente, OcCliente.cotizacion_id == Cotizacion.id)
        .filter(OcCliente.id == oc_cliente_id).scalar()
    )


def descargables_de_guia(db: Session, despacho: Despacho) -> Tuple[Optional[WasabilDte], Optional[str]]:
    """Lo descargable de un despacho que YA es del cliente: (DTE de la guía electrónica
    o None, ruta de la guía firmada o None)."""
    dte = dtes.de_documentos(db, despacho_ids=[despacho.id]).get(("guia", despacho.id))
    hay_pdf, ruta_firmada = _oferta_de_archivos(dte=dte, despacho=despacho,
                                                rut_venta=_rut_de_la_venta(db, despacho.oc_cliente_id))
    return (dte if hay_pdf else None), ruta_firmada


def dte_de_factura(db: Session, factura: ContFacturaCliente) -> Optional[WasabilDte]:
    """El DTE descargable de una factura que YA es del cliente, o None (factura emitida
    fuera del sistema, o que no consta emitida al RUT de la venta)."""
    dte = dtes.de_documentos(db, factura_ids=[factura.id]).get(("factura", factura.id))
    if not dtes.emitido(dte) or not dtes.archivos_permitidos(dte, _rut_de_la_venta(db, factura.oc_cliente_id)):
        return None
    return dte
