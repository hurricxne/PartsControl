"""Lo que el portal LEE de un DTE: la fila de wasabil_dte de una guía (52) o una factura
(33). El portal no emite ni corrige documentos; de esa fila toma cuatro cosas:

  · si el documento está emitido (`emitido`) y con qué fecha tributaria
    (`fecha_del_documento`);
  · a qué RUT se emitió (`receptor`), para no entregarle a un cliente el documento de otro;
  · a qué precio viajó cada línea de una guía (`precios_de_guia`): es el precio con que
    esa línea se factura después (routers/contabilidad.py::_precios_congelados_guia).

A QUIÉN SE EMITIÓ — por qué importa
    El vínculo venta ↔ cliente se recalcula si se corrige el RUT de la venta. Un DTE ya
    emitido al RUT anterior no es del cliente nuevo. La evidencia es lo que respondió
    Wasabil: `wasabil_dte.respuesta_json["receiver_rut"]`. Dos reglas, en este orden:
      1. `emitido_a_otro`: consta que fue a un RUT que NO es el de la venta → ni el
         documento ni sus archivos son de este cliente, esté el DTE en el estado que esté.
      2. `archivos_permitidos`: los ARCHIVOS de un documento que el SII ya aceptó solo se
         ofrecen si consta que fue al RUT de la venta. Si no se puede leer a quién, no se
         ofrecen: falla cerrado.
    CUÁNDO NO SE PUEDE LEER: la respuesta de una emisión normal trae el receptor (y
    también la que deja el sondeo cuando el documento pasa a emitido). No lo trae la que
    deja el registro MANUAL de un folio (wasabil_dte/router.py::_registrar_folio_a_mano
    la reemplaza por el folio y quién lo registró): esos documentos se listan en el
    portal, pero sin archivos, y hoy nada los repone. El checklist de deploy trae la
    consulta para contarlos antes de publicar.

Toda lectura de acá tolera un JSON ausente, recortado o con otra forma: responde "no
consta" (None, {}), nunca una excepción.
"""
import json
import re
from typing import Dict, Iterable, Optional

from sqlalchemy.orm import Session

from wasabil_dte.models import STATUS_EMITIDO, WasabilDte
from .service import rut_canonico, rut_valido
from .tiempo import fecha_de_texto

# Para leer el receptor de una respuesta que quedó recortada (se guarda cortada a 60.000
# caracteres: la de un documento con muchas líneas queda como JSON inválido).
_RECEPTOR_EN_TEXTO = re.compile(r'"receiver_rut"\s*:\s*"([^"]+)"')


def de_documentos(db: Session, *, despacho_ids: Iterable[int] = (),
                  factura_ids: Iterable[int] = ()) -> Dict[tuple, WasabilDte]:
    """{('guia', despacho_id) | ('factura', factura_id): su DTE, en el estado que esté}."""
    filas: Dict[tuple, WasabilDte] = {}
    despacho_ids, factura_ids = list(despacho_ids), list(factura_ids)
    if despacho_ids:
        for dte in db.query(WasabilDte).filter(WasabilDte.despacho_id.in_(despacho_ids)):
            filas[("guia", dte.despacho_id)] = dte
    if factura_ids:
        for dte in db.query(WasabilDte).filter(WasabilDte.factura_id.in_(factura_ids)):
            filas[("factura", dte.factura_id)] = dte
    return filas


def aceptado_por_el_sii(dte: Optional[WasabilDte]) -> bool:
    """El documento existe ante el SII (aunque su folio todavía no haya llegado acá)."""
    return dte is not None and dte.status_id == STATUS_EMITIDO


def emitido(dte: Optional[WasabilDte]) -> bool:
    """El SII lo aceptó y ya se conoce su folio: es un documento que se puede mostrar."""
    return bool(aceptado_por_el_sii(dte) and dte.folio)


def _json(texto: Optional[str]) -> Optional[dict]:
    try:
        datos = json.loads(texto or "")
    except (ValueError, TypeError):
        return None
    return datos if isinstance(datos, dict) else None


def fecha_del_documento(dte: WasabilDte) -> Optional[str]:
    """La fecha tributaria con que se emitió (`documentDate` de lo que se envió)."""
    enviado = _json(dte.payload_json)
    return fecha_de_texto(str(enviado.get("documentDate") or "")) if enviado else None


def receptor(dte: WasabilDte) -> Optional[str]:
    """El RUT al que se emitió, según lo que respondió Wasabil; None si no consta."""
    texto = dte.respuesta_json or ""
    respuesta = _json(texto)
    if respuesta is not None:
        valor = respuesta.get("receiver_rut")
    else:
        calce = _RECEPTOR_EN_TEXTO.search(texto)
        valor = calce.group(1) if calce else None
    return valor if isinstance(valor, str) and rut_valido(valor) else None


def emitido_a_otro(dte: Optional[WasabilDte], rut_venta: Optional[str]) -> bool:
    """Consta que el DTE fue a un RUT que NO es el de la venta."""
    a_quien = receptor(dte) if dte is not None else None
    if a_quien is None or not rut_valido(rut_venta):
        return False
    return rut_canonico(a_quien) != rut_canonico(rut_venta)


def archivos_permitidos(dte: Optional[WasabilDte], rut_venta: Optional[str]) -> bool:
    """¿Se pueden ofrecer los archivos del documento (su PDF, la guía firmada)? Ver «A
    QUIÉN SE EMITIÓ» en el encabezado."""
    if emitido_a_otro(dte, rut_venta):
        return False
    if not aceptado_por_el_sii(dte):
        # Guía en papel, emisión en curso o rechazada: no hay un documento aceptado por
        # el SII que contrastar.
        return True
    a_quien = receptor(dte)
    return (a_quien is not None and rut_valido(rut_venta)
            and rut_canonico(a_quien) == rut_canonico(rut_venta))


def precios_de_guia(dte: WasabilDte) -> Dict[int, float]:
    """{despacho_item_id: precio unitario neto} de las líneas de una guía, leídos de lo
    que se envió al emitirla (`details[].externalId` y `price`)."""
    enviado = _json(dte.payload_json)
    detalles = enviado.get("details") if enviado else None
    precios: Dict[int, float] = {}
    for linea in detalles if isinstance(detalles, list) else ():
        if not isinstance(linea, dict):
            continue
        try:
            despacho_item_id, precio = int(linea.get("externalId")), float(linea.get("price"))
        except (TypeError, ValueError):
            continue
        if precio > 0:
            precios[despacho_item_id] = round(precio, 2)
    return precios
