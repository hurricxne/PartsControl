"""Cuánto vale un renglón del pedido para el cliente: su total, lo que falta por entregar
y —cuando es uno solo— su precio unitario.

EL PRECIO QUE MANDA ES EL DEL DOCUMENTO, UNIDAD POR UNIDAD
    El ERP no le pone UN precio a una fila: valoriza cada guía al emitirla, con las filas
    y la configuración de ese día (wasabil_dte/router.py::_preparar_emision), y factura
    con el precio que quedó en la guía (routers/contabilidad.py::_construir_factura). Una
    fila de 10 unidades entregada en dos tandas puede tener dos precios. Por eso el valor
    de un renglón se arma por TRAMOS de unidades:
      1. lo FACTURADO vale lo que dicen sus líneas de factura (su total_neto);
      2. lo que viajó en una guía electrónica ya emitida y todavía no se factura vale el
         precio de esa guía, que es con el que se va a facturar;
      3. el resto vale el precio CALCULADO (portal/precios.py).
    Ejemplo: 10 unidades, 6 facturadas a $154.293,20 y 4 que salieron después a
    $211.178,73 → el total es lo que suman esas dos facturas, no 10 × el primer precio.

EL DOCUMENTO REEMPLAZA AL CÁLCULO, NO LO CREA
    Una fila sin precio calculado (venta cerrada antes de que existiera el congelado de
    precios) no muestra montos AUNQUE tenga facturas: sus unidades sin facturar se van a
    valorizar con el dólar del día de su guía, y un total armado hoy se movería. Esas
    órdenes siguen sin montos y cuentan en `ordenes_sin_monto` del tablero.

UNITARIO: solo cuando todas las unidades del renglón tienen el mismo precio. Con más de
uno no hay "un" precio que mostrar: va None y la pantalla muestra el total.

POR ENTREGAR: las unidades que todavía se esperan bajo esta orden, al precio de su tramo:
las que no tienen documento valen el precio calculado; si quedan unidades ya facturadas
que aún no salen (facturación adelantada), valen el precio promedio de lo documentado.
Las unidades que el cliente firmó como NO recibidas no entran: no se reponen por esta
venta (mismo criterio que contabilidad._mercaderia_pendiente_bruto).

EL REDONDEO es el de portal/precios.py: cada tramo se redondea una vez, sobre todas sus
unidades (las unidades sin documento de un renglón partido en varias filas forman UN tramo).
"""
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from . import dtes, precios

# Las cantidades son float: una resta puede dejar 1e-16 en vez de 0.
TOLERANCIA = 0.001


@dataclass(frozen=True)
class LineaFacturada:
    """Una línea de una factura que el cliente ve."""
    cantidad: float
    neto: float                       # total_neto de la línea
    unitario: float                   # precio_unit_neto
    despacho_item_id: Optional[int]   # la línea de guía que factura; None en facturas antiguas


@dataclass(frozen=True)
class EnGuia:
    """Unidades que viajaron en una guía electrónica emitida y que aún no se facturan."""
    cantidad: float
    unitario: float


@dataclass(frozen=True)
class FilaValorizable:
    """Lo que hace falta de una fila de items_cotizacion para valorizarla."""
    cantidad: float
    calculado: Optional[float]              # precio de portal/precios.py; None = sin monto
    facturadas: Sequence[LineaFacturada]
    en_guias: Sequence[EnGuia]              # la guía más nueva primero


@dataclass(frozen=True)
class Tramo:
    unidades: float
    neto: int
    unitario: float
    documentado: bool


def en_guias_sin_facturar(db: Session, lineas_de_despacho: Iterable[tuple],
                          facturadas: Dict[int, Sequence[LineaFacturada]]) -> Dict[int, List[EnGuia]]:
    """{item_id: sus unidades en guías electrónicas emitidas y todavía sin facturar}.

    `lineas_de_despacho` = [(despacho_item_id, item_id, despacho_id, cantidad)] de
    despachos cerrados, la más nueva primero. Solo se leen los DTE de los despachos que
    tienen alguna línea sin facturar: en régimen son los de los últimos días.
    """
    ya_facturadas = {linea.despacho_item_id for lineas in facturadas.values() for linea in lineas
                     if linea.despacho_item_id is not None}
    sin_facturar = [linea for linea in lineas_de_despacho if linea[0] not in ya_facturadas]
    if not sin_facturar:
        return {}
    guias = dtes.de_documentos(db, despacho_ids={despacho_id for _, _, despacho_id, _ in sin_facturar})
    precio_en = {despacho_id: dtes.precios_de_guia(dte)
                 for (_, despacho_id), dte in guias.items() if dtes.aceptado_por_el_sii(dte)}
    resultado: Dict[int, List[EnGuia]] = {}
    for despacho_item_id, item_id, despacho_id, cantidad in sin_facturar:
        unitario = precio_en.get(despacho_id, {}).get(despacho_item_id)
        if unitario is not None and cantidad > TOLERANCIA:
            resultado.setdefault(item_id, []).append(EnGuia(cantidad=cantidad, unitario=unitario))
    return resultado


def tramos_de(filas: Sequence[FilaValorizable]) -> Optional[List[Tramo]]:
    """Los tramos de un renglón (todas sus filas); None si a alguna fila le falta el
    precio calculado. Ver el encabezado."""
    if any(fila.calculado is None for fila in filas):
        return None
    tramos: List[Tramo] = []
    sin_documento: Dict[float, float] = {}  # precio calculado → unidades
    for fila in filas:
        resto = fila.cantidad
        for linea in fila.facturadas:
            tramos.append(Tramo(linea.cantidad, precios.a_peso(linea.neto), linea.unitario, True))
            resto -= linea.cantidad
        # Tope por cantidad: una factura antigua no dice qué línea de guía factura, y su
        # guía parecería "sin facturar". Lo que no cabe en la fila ya está en una factura;
        # se toma primero la guía más nueva, que es la que de verdad falta facturar.
        for guia in fila.en_guias:
            unidades = min(guia.cantidad, max(resto, 0.0))
            if unidades > TOLERANCIA:
                tramos.append(Tramo(unidades, precios.total_de_linea(guia.unitario, unidades),
                                    guia.unitario, True))
                resto -= unidades
        if resto > TOLERANCIA:
            sin_documento[fila.calculado] = sin_documento.get(fila.calculado, 0.0) + resto
    for unitario, unidades in sin_documento.items():
        tramos.append(Tramo(unidades, precios.total_de_linea(unitario, unidades), unitario, False))
    return tramos


def montos(filas: Sequence[FilaValorizable],
           por_entregar: float) -> Tuple[Optional[float], Optional[int], Optional[int]]:
    """(unitario, total, valor de lo que falta por entregar) de un renglón, en pesos
    netos; (None, None, None) si el renglón no tiene monto. `por_entregar` son las
    unidades que todavía se esperan bajo esta orden."""
    tramos = tramos_de(filas)
    if tramos is None:
        return None, None, None
    total = sum(tramo.neto for tramo in tramos)
    unitarios = {round(tramo.unitario, 2) for tramo in tramos}
    unitario = unitarios.pop() if len(unitarios) == 1 else None

    falta, valor_pendiente = por_entregar, 0
    for tramo in tramos:
        if tramo.documentado or falta <= TOLERANCIA:
            continue
        unidades = min(tramo.unidades, falta)
        valor_pendiente += precios.total_de_linea(tramo.unitario, unidades)
        falta -= unidades
    if falta > TOLERANCIA:
        documentadas = sum(tramo.unidades for tramo in tramos if tramo.documentado)
        if documentadas > TOLERANCIA:
            neto_documentado = sum(tramo.neto for tramo in tramos if tramo.documentado)
            valor_pendiente += precios.a_peso(neto_documentado / documentadas * falta)
    return unitario, total, valor_pendiente
