"""Las órdenes del cliente: las consultas y las respuestas que pide el router.

EL CANDADO — leer antes de tocar cualquier consulta de este archivo
    Toda consulta de órdenes nace de `base(db, scope)`, que aplica el filtro del cliente
    (portal/candado.py). Lo demás (búsqueda, filtros, orden, paginación, una orden por
    id) se le AGREGA encima; nada reconstruye la consulta desde la tabla. Una orden de
    otro cliente no "se rechaza": no está en la consulta, y el router responde 404 igual
    que si no existiera.
    `renglones.cargar` y las funciones de portal/documentos.py que reciben `ocs` NO
    filtran por cliente: trabajan sobre las órdenes que `base` ya entregó. Llamarlas con
    órdenes sacadas de otra parte es saltarse el candado.

LA UNIDAD ES LA VENTA
    El portal muestra una orden por cotización. Si una venta tiene más de una fila
    oc_cliente (ocurrió antes de que el Cierre de Venta fuera idempotente), `base` deja
    la de menor id, y los ítems, guías y facturas se cargan por cotización: el cliente no
    ve el pedido dos veces ni el tablero lo suma dos veces.

Cómo se arma cada ítem (los renglones, sus estados por unidad, sus montos) está en
portal/renglones.py. Acá se arma lo de la orden: su cabecera, el listado y el detalle.
"""
from datetime import date
from typing import List, Optional

from sqlalchemy import and_, exists, extract, func, or_, select
from sqlalchemy.orm import Session, aliased, contains_eager

from models.models import Despacho, DespachoItem, ItemCotizacion, OcCliente
from . import busqueda, documentos, precios
from .candado import de_sus_ordenes
from .deps import PortalScope
from .renglones import (
    ESTADO_ITEM_DESPACHADO, TOLERANCIA, Datos, Renglon, cargar, firmado_con_faltante, lineas,
)
from .tiempo import dia_chile, fecha_de_texto, hoy_chile
from .visibles import despacho_visible

PAGINA_MAXIMA = 50
COINCIDENCIAS_MAXIMAS = 3
FILTROS_DE_ESTADO = ("todas", "abiertas", "entregadas")

_OtraOcDeLaVenta = aliased(OcCliente)


# ── Las consultas ───────────────────────────────────────────────────────────────────────

def base(db: Session, scope: PortalScope):
    """LA consulta de órdenes del cliente: una por venta. Ver el encabezado."""
    primera_de_la_venta = (
        select(func.min(_OtraOcDeLaVenta.id))
        .where(_OtraOcDeLaVenta.cotizacion_id == OcCliente.cotizacion_id)
        .scalar_subquery()
    )
    return (
        de_sus_ordenes(db.query(OcCliente), scope)
        .filter(OcCliente.id == primera_de_la_venta)
        .options(contains_eager(OcCliente.cotizacion))
    )


def queda_algo_pendiente():
    """Condición SQL: la orden no está entregada completa (le falta algo por salir, o el
    cliente firmó como no recibido algo de lo que salió). Es el filtro «abiertas /
    entregadas» del listado, que tiene que resolverse en SQL para poder paginar; la
    insignia de cada orden se calcula después con el detalle de unidades (`cabecera`).
    Dicen lo mismo mientras el ERP pase la fila a 'despachado' al cerrar su último
    despacho, que es lo que hace."""
    fila_sin_despachar = exists().where(and_(
        ItemCotizacion.cotizacion_id == OcCliente.cotizacion_id,
        or_(ItemCotizacion.estado_item.is_(None),
            ItemCotizacion.estado_item != ESTADO_ITEM_DESPACHADO),
    ))
    recibido_con_faltante = exists().where(and_(
        ItemCotizacion.cotizacion_id == OcCliente.cotizacion_id,
        DespachoItem.item_cotizacion_id == ItemCotizacion.id,
        Despacho.id == DespachoItem.despacho_id,
        despacho_visible(), firmado_con_faltante(),
    ))
    return or_(fila_sin_despachar, recibido_con_faltante)


# ── La orden ────────────────────────────────────────────────────────────────────────────

def cabecera(oc: OcCliente, renglones: List[Renglon], datos: Datos) -> dict:
    """Lo que identifica y resume una orden. Sale igual en el listado, en el detalle y en
    el tablero, para que no puedan decir cosas distintas de la misma orden."""
    sin_entregar = [r for r in renglones if not r.entregado]
    # Las fechas de la orden son las de lo que TODAVÍA SE ESPERA (un renglón al que solo
    # le queda algo «no recibido en la entrega» ya no tiene un compromiso corriendo).
    comprometidas = sorted(r.fecha_prometida for r in renglones
                           if r.fecha_prometida and r.cantidad_por_entregar > TOLERANCIA)
    total = len(renglones)
    completa = bool(total and not sin_entregar)
    totales = None
    if renglones and all(r.total_clp is not None for r in renglones):
        t = precios.totales(r.total_clp for r in renglones)
        totales = {"neto_clp": t.neto_clp, "iva_clp": t.iva_clp, "total_clp": t.total_clp}
    ingreso = dia_chile(oc.created_at)
    return {
        "id": oc.id,
        "numero_oc": oc.numero_oc or "",
        # A cuál de sus empresas pertenece la orden (None si la cuenta representa una sola).
        "empresa": datos.empresas.get(oc.cotizacion.cliente_id),
        "fecha_oc": fecha_de_texto(oc.fecha_oc, referencia=date.fromisoformat(ingreso) if ingreso else None),
        "fecha_ingreso": ingreso,
        "estado": "entregada" if completa else "abierta",
        "items": {
            "total": total, "entregados": total - len(sin_entregar),
            # De cuántos ítems todavía se espera algo. La diferencia con los no entregados
            # son los que solo tienen algo «no recibido en la entrega».
            "por_entregar": sum(1 for r in renglones if r.cantidad_por_entregar > TOLERANCIA),
        },
        "avance_pct": _avance_pct(renglones, completa),
        # La MÁS ANTIGUA entre lo que se espera: si hay atrasados, ya pasó (la pantalla la
        # rotula «atrasado N días»). No es "la próxima que todavía no vence".
        "proxima_fecha": comprometidas[0].isoformat() if comprometidas else None,
        "fecha_completa": comprometidas[-1].isoformat() if comprometidas else None,
        "atrasados": sum(1 for r in renglones if r.vencida),
        "totales": totales,
    }


def _avance_pct(renglones: List[Renglon], completa: bool) -> int:
    """Avance de la orden: el promedio, entre sus ítems, de la fracción entregada de cada
    uno. Un ítem con 6 de 10 unidades entregadas aporta 0,6 (no 0), y un ítem de 500
    pernos pesa lo mismo que uno de 1 bomba (contar unidades lo haría pesar 500 veces
    más). Solo dice 100 cuando no queda nada pendiente: 99,6 % no se redondea a
    "completa"."""
    if completa:
        return 100
    if not renglones:
        return 0
    fracciones = [
        1.0 if r.entregado else
        min(r.cantidad_entregada / r.cantidad, 1.0) if r.cantidad > TOLERANCIA else 0.0
        for r in renglones
    ]
    return min(int(round(100 * sum(fracciones) / len(fracciones))), 99)


def ejecutivo(oc: OcCliente) -> Optional[dict]:
    """A quién escribirle por esta orden: el ejecutivo de la venta, si sigue activo."""
    asesor = oc.asesor
    if asesor is None or not asesor.is_active:
        return None
    return {"nombre": asesor.nombre or "", "email": asesor.email or ""}


# ── Lo que llama el router ──────────────────────────────────────────────────────────────

def listar(db: Session, scope: PortalScope, *, q: Optional[str], estado: str,
           anio: Optional[int], page: int, page_size: int) -> dict:
    consulta = base(db, scope)
    if anio:
        consulta = consulta.filter(extract("year", OcCliente.created_at) == anio)
    pendiente = queda_algo_pendiente()
    if estado == "abiertas":
        consulta = consulta.filter(pendiente)
    elif estado == "entregadas":
        consulta = consulta.filter(~pendiente).filter(
            exists().where(ItemCotizacion.cotizacion_id == OcCliente.cotizacion_id))
    palabras = busqueda.normalizar(q)
    if palabras:
        vetadas = (documentos.facturas_vetadas_que_calzan(db, scope, palabras)
                   if scope.ve_montos else set())
        consulta = busqueda.filtrar(consulta, palabras, con_facturas=scope.ve_montos,
                                    facturas_excluidas=vetadas)

    total = consulta.count()
    # Abiertas primero y, dentro de cada grupo, la más reciente arriba. El desempate por
    # id evita repetir o saltarse una orden entre páginas.
    ocs = (consulta.order_by(pendiente.desc(), OcCliente.created_at.desc(), OcCliente.id.desc())
           .offset((page - 1) * page_size).limit(page_size).all())
    datos = cargar(db, scope, ocs)
    hoy = hoy_chile()
    folios = documentos.folios(db, scope, ocs, datos.facturas) if palabras else {}
    filas = []
    for oc in ocs:
        renglones = lineas(oc, datos, hoy)
        fila = cabecera(oc, renglones, datos)
        if palabras:
            fila["coincidencias"] = _coincidencias(renglones, folios.get(oc.id, []), palabras)
        filas.append(fila)
    return {
        "total": total, "page": page, "page_size": page_size, "items": filas,
        # Las palabras que de verdad se buscaron (la pantalla dice «Buscaste: …»).
        "q_efectivo": " ".join(palabras),
    }


def _coincidencias(renglones: List[Renglon], folios: List[dict], palabras: List[str]) -> List[dict]:
    """Por qué salió esta orden en la búsqueda: sus repuestos y documentos que calzan.
    (Si calzó por el N° de orden, la pantalla lo ve en el propio número.)"""
    por_item = [
        {"tipo": "item", "id": r.id, "texto": r.numero_parte, "detalle": r.descripcion}
        for r in renglones
        if busqueda.coincide(r.numero_parte, palabras)
        or busqueda.coincide(r.descripcion, palabras)
        or busqueda.coincide(r.marca, palabras)
    ]
    por_documento = [
        {"tipo": f["tipo"], "id": f["id"], "texto": f["folio"], "detalle": ""}
        for f in folios if busqueda.coincide(f["folio"], palabras)
    ]
    return (por_item + por_documento)[:COINCIDENCIAS_MAXIMAS]


def buscar_orden(db: Session, scope: PortalScope, oc_id: int) -> Optional[OcCliente]:
    """La orden `oc_id` SI es del cliente; None si no existe o es de otro (el router
    responde 404 en los dos casos: un 403 confirmaría que el id existe)."""
    return base(db, scope).filter(OcCliente.id == oc_id).first()


def detalle(db: Session, scope: PortalScope, oc: OcCliente) -> dict:
    """Una orden completa: su cabecera, sus ítems, sus guías y sus facturas."""
    datos = cargar(db, scope, [oc])
    renglones = lineas(oc, datos, hoy_chile())
    return {
        "orden": {**cabecera(oc, renglones, datos), "ejecutivo": ejecutivo(oc)},
        "items": [r.publico() for r in renglones],
        "guias": documentos.guias(db, scope, [oc]),
        "facturas": documentos.facturas(db, scope, datos.facturas),
    }


def anios_con_ordenes(db: Session, scope: PortalScope) -> List[int]:
    filas = (de_sus_ordenes(db.query(extract("year", OcCliente.created_at)).select_from(OcCliente), scope)
             .distinct().all())
    return sorted({int(anio) for (anio,) in filas if anio}, reverse=True)
