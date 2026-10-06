"""El buscador del portal: por N° de orden de compra, N° de parte, descripción, marca y
folio de guía o factura, tolerante al guion (1R-0716 = 1R0716 = 1r 0716).

REGLA DE SEGURIDAD QUE HAY QUE ENTENDER ANTES DE TOCAR ESTO
    Este módulo NO sabe de clientes: recibe una consulta que YA viene filtrada por
    `Cotizacion.cliente_id IN scope.cliente_ids` y solo le agrega condiciones con AND.
    Nunca reconstruye la consulta desde cero ni recibe "la tabla": si lo hiciera, el
    candado del cliente se perdería y la búsqueda devolvería órdenes ajenas. El candado
    lo pone portal/ordenes.py (`base`), y la prueba de que sobrevive a la búsqueda es
    portal/tests/test_busqueda.py.

EL CONTRATO DE `q` es el del buscador de operador de la casa
(docs/spec-buscadores-bodega-despachos-2026-08-05.md; routers/despachos.py), copiado y no
importado porque el portal no depende de routers/:
  · recortar, colapsar espacios, truncar a 64; menos de 2 caracteres = no filtrar;
  · hasta 4 palabras: AND entre palabras, OR entre campos;
  · `%` y `_` se escapan SIEMPRE: un `_` tecleado es un guion bajo literal (sin escape,
    `100_4567` encontraría `100-4567` y `100X4567`, que son dos repuestos distintos);
  · `.like()` y no `.ilike()`: las columnas son utf8mb4_unicode_ci, ya insensibles a
    mayúsculas y tildes, y `lower(col)` anularía el índice a cambio de nada;
  · lo que vive en una tabla hija (ítems, guías, facturas) entra como EXISTS, nunca
    como JOIN: un join multiplica la orden por cada ítem que calza y rompe el conteo y
    la paginación.

TODO CAMPO BUSCABLE ES UN CAMPO REVELADO
    Buscar por un campo le dice al cliente "calza / no calza". Por eso solo se busca en
    lo que la pantalla le muestra (portal/visibles.py):
      · el número de guía, solo de los despachos cuyo número está a la vista;
      · el folio de factura, solo para las cuentas que ven facturas (`con_facturas`) y
        sin las facturas que no se le muestran por haberse emitido a otro RUT
        (`facturas_excluidas`, que calcula documentos.facturas_vetadas_que_calzan).

LOS EXISTS SE ANCLAN EN `OcCliente.cotizacion_id`, no en `Cotizacion.id`: OcCliente
    siempre está en el FROM de la consulta. Anclados en Cotizacion, si alguien cambiara
    el candado por una subconsulta (sin join), el EXISTS quedaría sin correlacionar y
    respondería "sí" cuando CUALQUIER cliente tiene esa parte.

TRES DIFERENCIAS DELIBERADAS CON EL BUSCADOR INTERNO
  1) Campos: solo lo que el cliente conoce de su propio pedido, y solo documentos que
     el portal le muestra (portal/visibles.py): si se buscara por el folio de una guía
     en preparación, la orden aparecería "sin motivo" a la vista. Quedan FUERA el número
     interno de cotización, el nombre y RUT del cliente (permitirían tantear otros
     clientes), todo lo del proveedor, el embarque, el transportista y los costos.
  2) La comparación sin guiones corre SIEMPRE, junto a la literal, y también sobre el
     N° de orden de compra. El buscador interno la deja para una segunda pasada porque
     recorre la tabla entera; acá la consulta ya está acotada a las órdenes de un
     cliente, así que el costo es despreciable y desaparecen los dos casos en que la
     segunda pasada no llegaba a correr.
  3) Se toleran las costumbres de quien escribe desde un correo: una palabra-rótulo
     suelta («OC 4500123», «N° 4500123», «orden 4500123») no cuenta como palabra a
     buscar, y la raya larga que pega Outlook («1R–0716») vale como guion.
"""
import re
import unicodedata
from typing import Iterable, List, Optional

from sqlalchemy import and_, exists, func, or_
from sqlalchemy.orm import aliased

from models.models import ContFacturaCliente, Despacho, ItemCotizacion, OcCliente
from .visibles import factura_emitida, guia_con_numero_a_la_vista

# Prefijos que la gente pega al número: «OC-4500123», «#4500123».
_PREFIJOS = ("OC-", "OC:", "N°", "Nº", "#")
# Palabras que solo anuncian lo que viene. Se ignoran si hay otra palabra que buscar.
_ROTULOS = frozenset({"oc", "oc:", "o/c", "n°", "nº", "nro", "nro.", "orden", "pedido"})
# Rayas que los programas de correo ponen en lugar del guion.
_RAYAS = str.maketrans({"–": "-", "—": "-", "‑": "-", "−": "-"})
_MAX_LARGO = 64
_MIN_LARGO = 2
_MAX_PALABRAS = 4


def normalizar(q: Optional[str]) -> List[str]:
    """Lo que escribió el cliente → palabras a buscar (lista vacía = no filtrar)."""
    limpio = re.sub(r"\s+", " ", (q or "").translate(_RAYAS).strip())[:_MAX_LARGO]
    # Una palabra de un solo carácter calza con casi todo: es ruido, no un criterio.
    palabras = [p for p in limpio.split(" ") if len(p) >= _MIN_LARGO]
    utiles = [p for p in palabras if p.lower() not in _ROTULOS]
    return (utiles or palabras)[:_MAX_PALABRAS]


def _variantes(palabra: str) -> List[str]:
    """[tal cual, sin el prefijo que la gente antepone]: «OC-4500123» y «4500123»."""
    variantes = [palabra]
    mayus = palabra.upper()
    for pref in _PREFIJOS:
        if mayus.startswith(pref) and len(palabra) - len(pref) >= _MIN_LARGO:
            variantes.append(palabra[len(pref):])
            break
    return variantes


def _patron(valor: str) -> str:
    """Patrón LIKE de «contiene», con los comodines escapados."""
    for c in ("\\", "%", "_"):
        valor = valor.replace(c, "\\" + c)
    return f"%{valor}%"


def colapsar(texto: str) -> str:
    """Sin guiones ni espacios, en mayúsculas: la forma en que 7T1997 y 7T-1997 coinciden."""
    return re.sub(r"[-\s]", "", texto or "").upper()


def _colapsada(col):
    return func.replace(func.replace(func.upper(col), "-", ""), " ", "")


def _compactas(palabra: str) -> List[str]:
    """Las formas sin guiones de la palabra que vale la pena comparar: solo las que
    tienen algún dígito (un N° de parte o de orden; «filtro-aceite» no es un número)."""
    formas = []
    for variante in _variantes(palabra):
        compacta = colapsar(variante)
        if len(compacta) >= _MIN_LARGO and any(c.isdigit() for c in compacta) and compacta not in formas:
            formas.append(compacta)
    return formas


def _item_de_la_orden(*condiciones):
    return exists().where(and_(ItemCotizacion.cotizacion_id == OcCliente.cotizacion_id, or_(*condiciones)))


# Los documentos son de la VENTA: se buscan en todas las filas oc_cliente de la misma
# cotización, igual que los carga portal/documentos.py (una venta puede tener más de una).
_OcDeLaVenta = aliased(OcCliente)


def _guia_de_la_venta(condicion):
    return exists().where(and_(
        Despacho.oc_cliente_id == _OcDeLaVenta.id,
        _OcDeLaVenta.cotizacion_id == OcCliente.cotizacion_id,
        guia_con_numero_a_la_vista(),
        condicion,
    ))


def _factura_de_la_venta(condicion, excluidas: List[int]):
    condiciones = [
        ContFacturaCliente.oc_cliente_id == _OcDeLaVenta.id,
        _OcDeLaVenta.cotizacion_id == OcCliente.cotizacion_id,
        factura_emitida(),
        condicion,
    ]
    if excluidas:
        condiciones.append(ContFacturaCliente.id.notin_(excluidas))
    return exists().where(and_(*condiciones))


def calza_con_alguna(columna, palabras: List[str]):
    """Condición SQL: `columna` contiene alguna de las palabras buscadas (tal cual o sin
    el prefijo que la gente antepone)."""
    return or_(*[columna.like(_patron(variante), escape="\\")
                 for palabra in palabras for variante in _variantes(palabra)])


def filtrar(query, palabras: List[str], con_facturas: bool, facturas_excluidas: Iterable[int] = ()):
    """Agrega a `query` (sobre OcCliente, YA acotada al cliente) una condición por
    palabra. Devuelve la misma consulta, más restringida. `con_facturas`: la cuenta ve
    facturas, así que también se busca por su folio, salvo el de `facturas_excluidas`."""
    excluidas = list(facturas_excluidas)
    for palabra in palabras:
        condiciones = []
        for variante in _variantes(palabra):
            pat = _patron(variante)
            condiciones += [
                OcCliente.numero_oc.like(pat, escape="\\"),
                _item_de_la_orden(
                    ItemCotizacion.numero_parte.like(pat, escape="\\"),
                    ItemCotizacion.descripcion.like(pat, escape="\\"),
                    ItemCotizacion.nombre_cat.like(pat, escape="\\"),
                    ItemCotizacion.marca.like(pat, escape="\\"),
                ),
                _guia_de_la_venta(Despacho.numero_guia.like(pat, escape="\\")),
            ]
            if con_facturas:
                condiciones.append(_factura_de_la_venta(
                    ContFacturaCliente.numero_factura.like(pat, escape="\\"), excluidas))
        for compacta in _compactas(palabra):
            pat = _patron(compacta)
            condiciones += [
                _colapsada(OcCliente.numero_oc).like(pat, escape="\\"),
                _item_de_la_orden(_colapsada(ItemCotizacion.numero_parte).like(pat, escape="\\")),
            ]
        query = query.filter(or_(*condiciones))
    return query


def _plano(texto: str) -> str:
    """Sin tildes y en minúsculas: como compara la base (utf8mb4_unicode_ci), para que
    «camion» calce con «CAMIÓN» también acá."""
    sin_tildes = "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c))
    return sin_tildes.casefold()


def coincide(texto: Optional[str], palabras: List[str]) -> bool:
    """¿Alguna palabra buscada aparece en `texto`? (literal o sin guiones). Para decir
    en la pantalla POR QUÉ salió una orden: se evalúa sobre lo ya paginado, no en SQL."""
    if not texto or not palabras:
        return False
    plano = _plano(texto)
    compacto = colapsar(plano)
    for palabra in palabras:
        if any(_plano(variante) in plano for variante in _variantes(palabra)):
            return True
        if any(colapsar(_plano(forma)) in compacto for forma in _compactas(palabra)):
            return True
    return False
