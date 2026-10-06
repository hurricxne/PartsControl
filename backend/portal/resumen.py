"""El tablero del cliente: cuánto compró, cuánto falta por entregar y DÓNDE viene lo que
falta.

UNA SOLA FÓRMULA
    Los números del tablero se obtienen armando las órdenes con las MISMAS funciones que
    usan el listado y el detalle (portal/renglones.py, portal/ordenes.py) y sumando. No
    hay una segunda cuenta en SQL: si el tablero dijera "7 ítems en tránsito" y al entrar
    a las órdenes se contaran 6, el portal perdería la credibilidad que es lo único que
    tiene.

QUÉ MONTOS, Y SOBRE QUÉ ÓRDENES
    Netos (sin IVA), que es como una empresa lleva sus órdenes de compra. Los tres
    —comprado, por entregar, facturado— se suman sobre EL MISMO conjunto: las órdenes
    que tienen precio en todos sus ítems. Las demás (ventas sin precio congelado, ver
    portal/precios.py) se cuentan en `ordenes_sin_monto` y no entran en ninguna suma:
    sumar sus facturas sin sumar lo comprado mostraría «comprado $0, facturado $480 M».
    «Facturado» no es un saldo ni dice cuánto se debe (eso el portal no lo responde, por
    decisión del dueño). Con perfil 'bodega' no sale ningún monto.

TOPE: se arman a lo más ORDENES_MAXIMAS órdenes, primero las que tienen algo pendiente y
dentro de eso las más recientes: si un cliente tiene más, las que quedan fuera son
órdenes ya entregadas, no las atrasadas. La pantalla avisa (`truncado`) y permite acotar
por año.
"""
from collections import Counter
from typing import Optional

from sqlalchemy import extract
from sqlalchemy.orm import Session

from models.models import OcCliente
from . import documentos, estados, ordenes, renglones
from .deps import PortalScope
from .tiempo import hoy_chile

ORDENES_MAXIMAS = 500


def tablero(db: Session, scope: PortalScope, anio: Optional[int]) -> dict:
    consulta = ordenes.base(db, scope)
    if anio:
        consulta = consulta.filter(extract("year", OcCliente.created_at) == anio)
    ocs = (consulta.order_by(ordenes.queda_algo_pendiente().desc(),
                             OcCliente.created_at.desc(), OcCliente.id.desc())
           .limit(ORDENES_MAXIMAS + 1).all())
    truncado = len(ocs) > ORDENES_MAXIMAS
    ocs = ocs[:ORDENES_MAXIMAS]

    datos = renglones.cargar(db, scope, ocs)
    hoy = hoy_chile()
    abiertas = entregadas = 0
    items_total = items_entregados = items_por_entregar = atrasados = 0
    por_estado: Counter = Counter()
    proxima: Optional[str] = None
    comprado = por_entregar = ordenes_sin_monto = 0
    con_monto = set()  # ids de las órdenes que entran en las sumas

    for oc in ocs:
        suyos = renglones.lineas(oc, datos, hoy)
        cab = ordenes.cabecera(oc, suyos, datos)
        if cab["estado"] == "entregada":
            entregadas += 1
        else:
            abiertas += 1
        items_total += cab["items"]["total"]
        items_entregados += cab["items"]["entregados"]
        items_por_entregar += cab["items"]["por_entregar"]
        atrasados += cab["atrasados"]
        if cab["proxima_fecha"] and (proxima is None or cab["proxima_fecha"] < proxima):
            proxima = cab["proxima_fecha"]
        for renglon in suyos:
            if not renglon.entregado:
                por_estado[renglon.estado.clave] += 1
        if not scope.ve_montos:
            continue
        if cab["totales"] is None:
            ordenes_sin_monto += 1 if suyos else 0
            continue
        con_monto.add(oc.id)
        comprado += cab["totales"]["neto_clp"]
        por_entregar += sum(renglon.por_entregar_clp for renglon in suyos)

    montos = None
    if scope.ve_montos:
        montos = {
            "comprado_neto_clp": comprado,
            "por_entregar_neto_clp": por_entregar,
            "facturado_neto_clp": documentos.facturado_neto(
                [f for f in datos.facturas if f.orden_id in con_monto]),
            "ordenes_sin_monto": ordenes_sin_monto,
        }
    return {
        "anio": anio,
        "anios": ordenes.anios_con_ordenes(db, scope),
        "truncado": truncado,
        "ordenes": {"total": len(ocs), "abiertas": abiertas, "entregadas": entregadas},
        "items": {
            "total": items_total, "entregados": items_entregados,
            "pendientes": items_total - items_entregados, "por_entregar": items_por_entregar,
            "atrasados": atrasados,
        },
        # Dónde vienen los ítems que faltan, en el orden del recorrido ('en gestión' primero).
        "pendientes_por_estado": [
            {"clave": clave, "etiqueta": estados.ETIQUETAS[clave], "items": cuantos}
            for clave, cuantos in sorted(por_estado.items(), key=lambda par: estados.posicion(par[0]))
        ],
        "proxima_fecha": proxima,
        "montos": montos,
    }
