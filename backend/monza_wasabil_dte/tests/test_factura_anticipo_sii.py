"""Fase 7 — factura de ANTICIPO (vía B) por la vía SII: descuento porcentual, piso de
$1, referencias 33 y candado anti doble emisión de MonzaParts.

Qué protege esta suite, en una línea: **el neto que sale al SII tiene que ser el MISMO
neto que quedó registrado en Chile**. Todo lo demás son las cinco formas conocidas de
romperlo:

  1. La línea LOCAL de descuento (total_neto negativo) NO puede viajar como línea del
     DTE: el API real RECHAZA price<0 y quantity<0 y no tiene descuento a nivel de
     documento. Viaja como `discount` PORCENTUAL por línea.
  2. Ese porcentaje NO se redondea. Wasabil calcula con la precisión enviada
     (verificado por GA en borrador real: 16,6665% sobre 200.000 dio 166.667 exacto);
     redondearlo a 2 decimales emitiría un documento por un monto distinto al registrado.
  3. El SII no acepta un DTE en cero: el descuento que deja el documento bajo $1
     BLOQUEA, y el borde legítimo de $1 exacto pasa.
  4. Una factura de anticipo sin folio del SII no es referenciable (33): se BLOQUEA —
     ni se ignora el anticipo (le cobraría dos veces al cliente) ni se emite chueco.
  5. Dos clics en Emitir no pueden producir dos documentos reales. El candado es POR
     VENTA (_emision_33_en_vuelo_de_cot), no por factura: en el camino del anticipo
     cada request crearía una factura con id distinto y el índice único no aplica.

SEGURIDAD (innegociable): CERO llamadas al API real. La parte pura no importa ni
`client` ni `router`; la parte E2E usa `FakeWasabil` del arnés, que pisa SOLO
`monza_wasabil_dte.client` (nunca el de Grupo AM, que lleva el token de producción de
la otra marca). Los `issue=True` que aparecen en el payload emitido los intercepta el
fake: nunca salen de la app. Datos MARCADOS y limpieza verificada con sesión nueva.

Corre con:  ./venv/bin/python -m pytest monza_wasabil_dte/tests/test_factura_anticipo_sii.py -q
"""
import os
import sys
import threading
import time
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import wasabil_dte.client as ga_client            # noqa: E402
import monza_wasabil_dte.client as monza_client   # noqa: E402
from database import SessionLocal                 # noqa: E402
from monza_contabilidad.models import (  # noqa: E402
    MonzaContAdelanto, MonzaContCobranza, MonzaContFacturaCliente,
    MonzaContFacturaClienteItem,
)
from monza_contabilidad.service import _total_linea  # noqa: E402
from monza_wasabil_dte.models import STATUS_EMITIDO, STATUS_PROCESANDO  # noqa: E402
from monza_wasabil_dte.service import (  # noqa: E402
    FOLIO_REF_MAX, MAX_REFERENCIAS, NETO_MINIMO_DTE, TIPO_REF_ANTICIPO,
    aplicar_descuento_lineas, armar_factura, armar_lineas_factura,
    armar_referencias_factura, payload_a_rest,
)
from monza_wasabil_dte.tests.factura_harness import (  # noqa: E402
    Checker, FakeWasabil, cobranzas_de, crear_venta, despacho_extra, dte_de_factura,
    dte_guia, facturas_de, limpiar, montar_app, verificar_limpieza,
)

# MARK corto: MonzaCotizacion.numero es String(20) y crear_venta le pega "-COT-<n>".
MARK = "__MWF7A__"
CURRENT = {"empresa": "automotriz", "id": None}

client = montar_app(CURRENT)
fake = FakeWasabil(MARK)
fake.install()

BASE = "/api/monza/wasabil/facturas"
CONTAB = "/api/monza/contabilidad"


# ─── helpers ──────────────────────────────────────────────────────────────────

def _item(**kw):
    """Doble de MonzaContFacturaClienteItem. `armar_lineas_factura` solo lee atributos
    (numero_parte, descripcion, cantidad, precio_unit_neto, total_neto,
    despacho_item_id, anticipo_factura_id), así que un SimpleNamespace basta."""
    base = dict(id=1, numero_parte="P1", descripcion="Pieza 1", cantidad=1,
                precio_unit_neto=80000, total_neto=80000, despacho_item_id=None,
                anticipo_factura_id=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _descuento(monto, anticipo_id=555, **kw):
    """La línea LOCAL de descuento tal como la persiste `_persistir_factura`:
    numero_parte 'DESCUENTO', cantidad 1, precio y total NEGATIVOS, FK al anticipo y
    SIN item_cotizacion_id/despacho_item_id (no consume cantidad física)."""
    base = dict(id=99, numero_parte="DESCUENTO",
                descripcion=f"Descuento anticipo Factura N° {anticipo_id}",
                cantidad=1, precio_unit_neto=-monto, total_neto=-monto,
                despacho_item_id=None, anticipo_factura_id=anticipo_id)
    base.update(kw)
    return SimpleNamespace(**base)


def _neto_dte(lineas):
    """Neto que el SII verá: Σ por línea de (precio × cantidad) × (1 − discount%),
    redondeado a PESO con half-up EN CADA LÍNEA.

    El redondeo por línea NO es un detalle: en un DTE en CLP cada MontoItem es un
    ENTERO, y ese es exactamente el dominio en que Contabilidad calculó el neto local
    (_total_linea). Esta suite comparaba antes con una suma en CENTAVOS, y por eso no
    vio el descuadre que reportó la auditoría (3 líneas terminadas en .5 con un
    anticipo de $40.000: neto local 26.668, neto emitido 26.667)."""
    return float(sum(
        Decimal(str(x["price"] * x["quantity"] * (1 - x.get("discount", 0) / 100.0)))
        .quantize(Decimal("1"), rounding=ROUND_HALF_UP) for x in lineas))


def _neto_local(filas, descuento=0):
    """Neto que registra Contabilidad: Σ _total_linea (half-up a peso) − descuento.
    `filas` = [(cantidad, precio)]. Se calcula con la función REAL del módulo de
    Contabilidad, no con una copia: si aquella cambiara de criterio, esta suite lo
    detectaría en vez de seguir comparando contra su propia idea."""
    return sum(_total_linea(p, q) for q, p in filas) - descuento


def _positivas(lineas):
    return all(x["price"] > 0 and x["quantity"] > 0 for x in lineas)


# ═══════════════════════════════════════════════════════════════════════════════
# PARTE 1 — funciones PURAS (sin BD, sin red, sin fakes)
# ═══════════════════════════════════════════════════════════════════════════════

def run_puro():
    check = Checker()

    # ── A · REPARTO PORCENTUAL ────────────────────────────────────────────────
    lineas, probs = armar_lineas_factura([
        _item(id=1, numero_parte="P1", cantidad=1, precio_unit_neto=80000, total_neto=80000),
        _item(id=2, numero_parte="P2", descripcion="Pieza 2", cantidad=1,
              precio_unit_neto=20000, total_neto=20000),
        _descuento(30000),
    ])
    l80 = next((x for x in lineas if x["price"] == 80000), {})
    l20 = next((x for x in lineas if x["price"] == 20000), {})
    check("la línea LOCAL de descuento NO viaja al DTE (2 líneas, todas price>0 y qty>0)",
          len(lineas) == 2 and _positivas(lineas), lineas)
    check("la línea de 80.000 lleva discount 37.5 (= 30.000/80.000)",
          l80.get("discount") == 37.5, l80.get("discount"))
    check("la línea de 20.000 NO lleva discount (greedy mayor→menor: ya se agotó)",
          "discount" not in l20, l20)
    check("neto del DTE == 70.000 EXACTO", _neto_dte(lineas) == 70000.0, _neto_dte(lineas))
    check("reparto normal: sin problemas bloqueantes", not probs, probs)

    rest = payload_a_rest(armar_factura(referencia_interna="FACT-1", lineas=lineas,
                                        referencias=[], client_id=1))
    check("`discount` sobrevive la traducción a snake_case (viaja en details tal cual)",
          any(d.get("discount") == 37.5 for d in rest["details"]), rest["details"])
    check("el payload REST nace con issue=False (armar_factura no emite por armar)",
          rest["issue"] is False, rest["issue"])
    check("NINGUNA línea del REST tiene price<=0 ni quantity<=0 (el API las rechaza)",
          _positivas(rest["details"]), rest["details"])

    # Precisión COMPLETA del float: redondear el % rompería la cuadratura.
    lineas_p = [{"name": "X", "code": "X", "quantity": 1, "price": 200000.0}]
    probs_p = aplicar_descuento_lineas(lineas_p, 33333.0)
    pct = lineas_p[0].get("discount")
    check("decimales feos: 200.000 − 33.333 → discount 16.6665 y neto 166.667 EXACTO",
          pct == 16.6665 and _neto_dte(lineas_p) == 166667.0 and not probs_p,
          (pct, _neto_dte(lineas_p), probs_p))
    check("el porcentaje NO viene redondeado a 2 decimales (16.6665 != 16.67)",
          pct != round(pct, 2), pct)

    # ── B · CUADRATURA GENERAL: neto DTE == neto local en varios repartos ──────
    # (cantidades/precios, descuento). El neto local NO se escribe a mano: lo calcula
    # _neto_local con la función REAL de Contabilidad (_total_linea, half-up a PESO).
    # Ése es el punto del hallazgo B-1: el reparto se hacía en CENTAVOS y el documento
    # tributario salía descuadrado contra el libro de ventas por un peso.
    # Los casos con decimales son alcanzables en producción: precio_unitario_clp es
    # Float y entra del body sin redondear a peso.
    repartos = [
        ("80.000 + 20.000 − 30.000", [(1, 80000), (1, 20000)], 30000),
        ("4×15.000 + 20×2.500 − 50.000 (la venta patrón)", [(4, 15000), (20, 2500)], 50000),
        ("80.000 + 20.000 − 90.000 (se reparte en DOS líneas)",
         [(1, 80000), (1, 20000)], 90000),
        ("3×33.333 − 33.333", [(3, 33333)], 33333),
        ("200.000 − 33.333", [(1, 200000)], 33333),
        ("7×1.111 + 3×2.222 − 5.000", [(7, 1111), (3, 2222)], 5000),
        ("5×12.345 − 40.000", [(1, 12345)] * 5, 40000),
        ("100.000 + 1 − 100.000 (la chica sobrevive con $1)", [(1, 100000), (1, 1)], 100000),
        # EL CASO DE LA AUDITORÍA, tal cual: tres líneas terminadas en .5 y un anticipo
        # de $40.000. Antes del arreglo: neto local 26.668 · neto emitido 26.667.
        ("AUDITORÍA: 33.333,5 + 22.221,5 + 11.111,5 − 40.000",
         [(1, 33333.5), (1, 22221.5), (1, 11111.5)], 40000),
        ("dos líneas .5, descuento chico", [(1, 1000.5), (1, 2000.5)], 10),
        ("línea .5 consumida al 100%", [(1, 10000.5), (1, 5000.0)], 10001),
        ("cantidad fraccionaria 2,5 × 1.111,1", [(2.5, 1111.1), (1, 900.4)], 1000),
        ("seis líneas .5 − 50.000 (descuento casi total)",
         [(1, 12345.5), (1, 9876.5), (1, 8765.5), (1, 7654.5), (1, 6543.5), (1, 5432.5)],
         50000),
        ("diez líneas .25/.75 − 11.891",
         [(1, 4444.25), (2, 3333.75), (1, 2222.25), (3, 1111.75), (1, 999.5),
          (2, 888.25), (1, 777.75), (1, 666.5), (1, 555.25), (1, 444.75)], 11891),
        ("centavos puros − 1", [(1, 100.99), (1, 200.01)], 1),
    ]
    todos_cuadran = True
    for etiqueta, filas, dsc in repartos:
        esperado = _neto_local(filas, dsc)
        items = [_item(id=i + 1, numero_parte=f"P{i + 1}", cantidad=q, precio_unit_neto=p,
                       total_neto=_total_linea(p, q)) for i, (q, p) in enumerate(filas)]
        items.append(_descuento(dsc))
        lns, pr = armar_lineas_factura(items)
        # El % también se valida: el API rechaza discount fuera de (0, 100], y un resto
        # sub-peso podía producir uno NEGATIVO al medir la línea en pesos.
        pcts_ok = all(0 < x["discount"] <= 100 for x in lns if "discount" in x)
        ok = _neto_dte(lns) == float(esperado) and not pr and _positivas(lns) and pcts_ok
        todos_cuadran = todos_cuadran and ok
        check(f"cuadratura {etiqueta} → neto DTE {esperado:,.0f}".replace(",", "."), ok,
              (_neto_dte(lns), esperado, pr, [x.get("discount") for x in lns]))
    check("INVARIANTE: en TODOS los repartos el neto del DTE == el neto local "
          "(mismo dominio de redondeo: half-up a PESO, como _total_linea)",
          todos_cuadran)

    # No-vacuidad del invariante de arriba: el caso de la auditoría descuadraba en
    # −$1 con el reparto en centavos. Se re-mide el descuadre viejo para dejar claro
    # que estos checks no pasan "por construcción".
    filas_aud = [(1, 33333.5), (1, 22221.5), (1, 11111.5)]
    lns_aud, _pr = armar_lineas_factura(
        [_item(id=i + 1, cantidad=q, precio_unit_neto=p, total_neto=_total_linea(p, q))
         for i, (q, p) in enumerate(filas_aud)] + [_descuento(40000)])
    viejo = round(sum(round(x["price"] * x["quantity"], 2) *
                      (1 - x.get("discount", 0) / 100.0) for x in lns_aud), 2)
    check("no-vacuidad: en el caso de la auditoría el neto local NO es redondo "
          "(26.668) y la suma en centavos —el criterio viejo— sigue sin darlo",
          _neto_local(filas_aud, 40000) == 26668 and viejo != 26668,
          (_neto_local(filas_aud, 40000), viejo))

    # El reparto sobre DOS líneas deja la primera al 100% y la segunda al resto.
    lns, _pr = armar_lineas_factura([
        _item(id=1, cantidad=1, precio_unit_neto=80000, total_neto=80000),
        _item(id=2, cantidad=1, precio_unit_neto=20000, total_neto=20000),
        _descuento(90000),
    ])
    d80 = next(x for x in lns if x["price"] == 80000)
    d20 = next(x for x in lns if x["price"] == 20000)
    check("descuento mayor que la línea grande: 100% en la de 80.000 y 50% en la de 20.000",
          d80.get("discount") == 100.0 and d20.get("discount") == 50.0,
          [x.get("discount") for x in lns])

    # ── C · PISO DE $1 ────────────────────────────────────────────────────────
    _l, probs_cero = armar_lineas_factura([
        _item(id=1, cantidad=1, precio_unit_neto=100000, total_neto=100000),
        _descuento(99999.6),
    ])
    check(f"descuento que deja el DTE bajo ${NETO_MINIMO_DTE:.0f} → BLOQUEA",
          bool(probs_cero) and any("en $0" in p for p in probs_cero), probs_cero)
    _l, probs_099 = armar_lineas_factura([
        _item(id=1, cantidad=1, precio_unit_neto=100000, total_neto=100000),
        _descuento(99999.01),
    ])
    check("neto de $0,99 tras el descuento → BLOQUEA (redondea a $1 pero no llega)",
          bool(probs_099), probs_099)
    lns_uno, probs_uno = armar_lineas_factura([
        _item(id=1, cantidad=1, precio_unit_neto=100000, total_neto=100000),
        _descuento(99999),
    ])
    check("neto de $1 EXACTO tras el descuento → NO bloquea (el borde legítimo pasa)",
          not probs_uno and _neto_dte(lns_uno) == 1.0, (probs_uno, _neto_dte(lns_uno)))
    _l, probs_sup = armar_lineas_factura([
        _item(id=1, cantidad=1, precio_unit_neto=10000, total_neto=10000),
        _descuento(50000),
    ])
    check("descuento MAYOR que el total de las líneas → BLOQUEA",
          bool(probs_sup) and any("supera el total" in p for p in probs_sup), probs_sup)
    _l, probs_solo = armar_lineas_factura([_descuento(5000)])
    check("factura que SOLO trae la línea de descuento → BLOQUEA (no emite un DTE vacío)",
          bool(probs_solo), probs_solo)
    # Regresión Fase 6: sin anticipo el piso de siempre sigue vivo (y con SU mensaje).
    _l, probs_f6 = armar_lineas_factura([_item(cantidad=1, precio_unit_neto=0.4,
                                               total_neto=0.4)])
    check("regresión F6: sin descuento, el piso de $1 sigue bloqueando con su mensaje",
          bool(probs_f6) and any("bajo $1" in p for p in probs_f6), probs_f6)

    # ── D · CÓMO SE RECONOCE UNA LÍNEA DE DESCUENTO ───────────────────────────
    # SOLO la FK. El criterio viejo ("FK o precio<0") tragaba cualquier línea negativa
    # suelta y la convertía en descuento: una línea heredada sin anticipo detrás —que
    # ANTES de la Fase 7 BLOQUEABA la emisión— pasaba a rebajar el DTE sin ninguna
    # referencia 33 que lo respalde ante el SII (hallazgo B-4, medido con worktree).
    lns_fk, pr_fk = armar_lineas_factura([
        _item(id=1, cantidad=1, precio_unit_neto=50000, total_neto=50000),
        _descuento(10000),
    ])
    check("con FK: la línea negativa se convierte en descuento (neto 40.000, sin problemas)",
          _neto_dte(lns_fk) == 40000.0 and not pr_fk, (_neto_dte(lns_fk), pr_fk))
    lns_sinfk, pr_sinfk = armar_lineas_factura([
        _item(id=1, cantidad=1, precio_unit_neto=50000, total_neto=50000),
        _descuento(10000, anticipo_id=None),   # línea negativa SIN FK (histórica)
    ])
    check("SIN FK: la línea negativa vuelve a ser problema BLOQUEANTE (no se emite un "
          "DTE rebajado sin referencia 33 que lo respalde)",
          bool(pr_sinfk), pr_sinfk)
    check("...y la línea buena NO queda con `discount` (el descuento fantasma no se aplica)",
          all("discount" not in x for x in lns_sinfk), lns_sinfk)
    check("...con mensaje PROPIO: dice 'monto NEGATIVO', no manda a buscar el folio de "
          "un anticipo inexistente",
          any("NEGATIVO" in p for p in pr_sinfk)
          and not any("folio" in p for p in pr_sinfk), pr_sinfk)
    check("una línea de descuento JAMÁS se reporta como 'precio $0'",
          not any("precio $0" in p for p in pr_fk + pr_sinfk), pr_fk + pr_sinfk)
    # El guard mira el MONTO, no solo el precio unitario: una línea con precio 0 y
    # total negativo (cantidad negativa) tampoco puede colarse.
    _l, pr_qty = armar_lineas_factura([
        _item(id=1, cantidad=1, precio_unit_neto=50000, total_neto=50000),
        _item(id=2, numero_parte="RARA", cantidad=-2, precio_unit_neto=0, total_neto=-1000),
    ])
    check("línea con total negativo por CANTIDAD negativa y sin FK → también BLOQUEA",
          any("NEGATIVO" in p for p in pr_qty), pr_qty)

    # ── E · REFERENCIAS 33 ────────────────────────────────────────────────────
    refs, probs_ref = armar_referencias_factura(
        numero_oc="OC-4501", fecha_oc=date(2026, 6, 10),
        guia_folio="777", guia_fecha=date(2026, 6, 20),
        anticipos=[{"folio": "9001", "fecha": date(2026, 6, 1)}])
    # NOTA: todos los folios de anticipo de esta sección son NUMÉRICOS a propósito.
    # La referencia 33 apunta a un DTE y el SII exige su folio numérico (hallazgo B-2).
    check("factura final: 801 + 52 + 33 en ese orden, sin problemas",
          [r["documentType"] for r in refs] == ["801", "52", "33"] and not probs_ref,
          (refs, probs_ref))
    check("la 33 lleva folio, fecha y reason 'Descuento anticipo'",
          refs[2] == {"documentType": TIPO_REF_ANTICIPO, "folio": "9001",
                      "date": "2026-06-01", "reason": "Descuento anticipo"}, refs[2])
    refs_sf, _p = armar_referencias_factura(numero_oc="OC-1", fecha_oc=date(2026, 1, 2),
                                            anticipos=[{"folio": "9001", "fecha": None}])
    check("33 sin fecha: referencia válida y SIN la clave 'date'",
          refs_sf[1] == {"documentType": "33", "folio": "9001",
                         "reason": "Descuento anticipo"}, refs_sf)
    refs_2, _p = armar_referencias_factura(
        numero_oc="OC-1", fecha_oc=date(2026, 1, 2), guia_folio="777",
        anticipos=[{"folio": "9001", "fecha": None}, {"folio": "9002", "fecha": None}])
    check("dos anticipos descontados → dos referencias 33, en el orden recibido (FIFO)",
          [r["documentType"] for r in refs_2] == ["801", "52", "33", "33"]
          and [r["folio"] for r in refs_2[2:]] == ["9001", "9002"], refs_2)

    refs_h, probs_h = armar_referencias_factura(
        numero_oc="OC-4501", fecha_oc=date(2026, 6, 10), guia_folio="777",
        anticipos=[{"folio": "#12", "fecha": date(2026, 6, 1)}])
    check("folio de anticipo '#12' (placeholder sin folio SII) → BLOQUEA",
          bool(probs_h) and any("folio SII" in p for p in probs_h), probs_h)
    check("...y la referencia 33 NO se emite a medias (quedan 801 + 52)",
          [r["documentType"] for r in refs_h] == ["801", "52"], refs_h)
    for etiqueta, folio in (("vacío", ""), ("None", None), ("en blanco", "   ")):
        refs_v, probs_v = armar_referencias_factura(
            numero_oc="OC-1", fecha_oc=date(2026, 1, 2),
            anticipos=[{"folio": folio, "fecha": None}])
        check(f"folio de anticipo {etiqueta} → BLOQUEA y no agrega la 33",
              bool(probs_v) and [r["documentType"] for r in refs_v] == ["801"],
              (probs_v, refs_v))
    refs_lg, probs_lg = armar_referencias_factura(
        numero_oc="OC-1", fecha_oc=date(2026, 1, 2),
        anticipos=[{"folio": "9" * (FOLIO_REF_MAX + 1), "fecha": None}])
    check(f"folio de anticipo > {FOLIO_REF_MAX} chars → BLOQUEA y no agrega la 33",
          bool(probs_lg) and any("anticipo" in p for p in probs_lg)
          and [r["documentType"] for r in refs_lg] == ["801"], (probs_lg, refs_lg))

    # FOLIO NO NUMÉRICO (hallazgo B-2): en el modo "registrar un anticipo ya emitido"
    # el folio lo TECLEA el operador. Un 'N/A-99' viajaba en la referencia 33 y el SII
    # rechaza el documento — con el folio propio YA CONSUMIDO. Se bloquea antes.
    for etiqueta, folio in (("N/A-99", "N/A-99"), ("con espacio", "FAC 123"),
                            ("N/A", "N/A"), ("cero", "0"), ("negativo", "-5"),
                            ("con punto de miles", "12.345"),
                            ("dígitos no ASCII", "٣٤٥")):
        refs_nn, probs_nn = armar_referencias_factura(
            numero_oc="OC-1", fecha_oc=date(2026, 1, 2),
            anticipos=[{"folio": folio, "fecha": None}])
        check(f"folio de anticipo {etiqueta} ({folio!r}) → BLOQUEA y NO viaja la 33",
              bool(probs_nn) and any("numérico" in p for p in probs_nn)
              and [r["documentType"] for r in refs_nn] == ["801"], (probs_nn, refs_nn))
    for folio_ok in ("1", "9001", "570001", "9" * FOLIO_REF_MAX):
        refs_ok, probs_ok33 = armar_referencias_factura(
            numero_oc="OC-1", fecha_oc=date(2026, 1, 2),
            anticipos=[{"folio": folio_ok, "fecha": None}])
        check(f"folio de anticipo numérico {folio_ok!r} → pasa y viaja en la 33",
              not probs_ok33 and [r["documentType"] for r in refs_ok] == ["801", "33"],
              (probs_ok33, refs_ok))

    refs_max, probs_max = armar_referencias_factura(
        numero_oc="OC-1", fecha_oc=date(2026, 1, 2), guia_folio="777",
        anticipos=[{"folio": f"900{i}", "fecha": None} for i in range(1, 5)])
    # El mensaje viejo mandaba a "dividir el descuento de anticipos en más de una
    # factura": el operador NO tiene esa palanca (el descuento es automático y FIFO),
    # así que la factura quedaba imposible de emitir Y de arreglar (hallazgo B-6).
    check(f"801 + 52 + 4×33 = {len(refs_max)} referencias > {MAX_REFERENCIAS} → BLOQUEA",
          bool(probs_max), probs_max)
    check("...y el mensaje da salidas que el operador SÍ puede ejecutar (facturar en "
          "tandas más chicas o registrar por la vía manual), sin mandarlo a dividir un "
          "descuento que el sistema calcula solo",
          any("tandas más chicas" in p and "vía manual" in p for p in probs_max)
          and not any("divide el descuento" in p for p in probs_max), probs_max)
    check("...y dice CUÁNTOS anticipos son (el operador entiende de dónde salen las "
          "referencias)",
          any("4 facturas de anticipo" in p for p in probs_max), probs_max)

    # Regresión Fase 6: sin anticipos la matriz de referencias es la de siempre.
    for etiqueta, ant in (("None", None), ("lista vacía", [])):
        refs_n, probs_n = armar_referencias_factura(
            numero_oc="OC-1", fecha_oc=date(2026, 1, 2), guia_folio="777", anticipos=ant)
        check(f"regresión F6: anticipos={etiqueta} → solo 801 + 52",
              [r["documentType"] for r in refs_n] == ["801", "52"] and not probs_n,
              (refs_n, probs_n))
    refs_a, probs_a = armar_referencias_factura(numero_oc="OC-4501",
                                                fecha_oc=date(2026, 6, 10))
    check("factura de ANTICIPO: SOLO la 801 (ni 52 ni 33)",
          [r["documentType"] for r in refs_a] == ["801"] and not probs_a,
          (refs_a, probs_a))

    # El router NUNCA arma 52 ni 33 para un anticipo, aunque le pasen despacho y
    # anticipos. db=None a propósito: si la rama tocara la BD para buscar la guía,
    # esto reventaría — la prueba es que NI SIQUIERA lo intenta.
    from monza_wasabil_dte.router import _referencias_de_venta  # noqa: PLC0415
    cot = SimpleNamespace(oc_cliente="OC-4501", oc_fecha=date(2026, 6, 10))
    probs_r, adv_r = [], []
    refs_r = _referencias_de_venta(None, cot, sin_guia=False, despacho_id=999,
                                   problemas=probs_r, advertencias=adv_r,
                                   anticipos=[{"folio": "9001", "fecha": None}],
                                   es_anticipo=True)
    check("_referencias_de_venta(es_anticipo=True): solo 801, ni 52 ni 33, sin problemas",
          [r["documentType"] for r in refs_r] == ["801"] and not probs_r,
          (refs_r, probs_r))
    check("...y deja advertencia explicando por qué no hay guía",
          any("anticipo" in a.lower() for a in adv_r), adv_r)

    # ── F · ESTADO DE UN DOCUMENTO QUE EL API YA DIO POR EMITIDO ──────────────
    # Reproducido (B-5): status_id=3 con folio real del SII pero uuid NULL se pintaba
    # "no enviado" y CON botón de Reintentar, sobre un documento irreversible que ya
    # existe. El reintento lo ataja con un 409, pero la pantalla no puede mentir.
    from monza_wasabil_dte.service import serialize_dte  # noqa: PLC0415

    def _fila(**kw):
        base = dict(id=1, tipo_dte=33, despacho_id=None, factura_id=7, uuid=None,
                    status_id=None, folio=None, pdf_url=None, xml_url=None, error=None,
                    monto_neto=1000, iva=190, monto_total=1190, en_vuelo_desde=None,
                    created_at=None, updated_at=None)
        base.update(kw)
        return SimpleNamespace(**base)

    s = serialize_dte(_fila(status_id=STATUS_EMITIDO, folio="9500", uuid=None))
    check("status_id=3 + folio + uuid NULL → estado 'emitido' (el status manda sobre "
          "la ausencia de uuid)", s["estado"] == "emitido", s)
    check("...y puede_reintentar=False (no se ofrece re-emitir lo irreversible)",
          s["puede_reintentar"] is False, s)
    s_sf = serialize_dte(_fila(status_id=STATUS_EMITIDO, folio=None, uuid=None))
    check("status_id=3 sin folio y sin uuid → igual 'emitido', igual sin reintento",
          s_sf["estado"] == "emitido" and s_sf["puede_reintentar"] is False, s_sf)
    # Regresión Fase 6: los estados de recuperación que SÍ deben permitir reintento.
    s_nada = serialize_dte(_fila())
    check("regresión F6: sin uuid y sin status → 'no_enviado' y REINTENTABLE "
          "(el server se cayó entre el claim y la respuesta)",
          s_nada["estado"] == "no_enviado" and s_nada["puede_reintentar"] is True, s_nada)
    s_fall = serialize_dte(_fila(uuid="u-1", status_id=4, error="rechazado"))
    check("regresión F6: fallido con uuid → 'fallido' y REINTENTABLE",
          s_fall["estado"] == "fallido" and s_fall["puede_reintentar"] is True, s_fall)
    s_ok = serialize_dte(_fila(uuid="u-1", status_id=STATUS_EMITIDO, folio="9501"))
    check("regresión F6: emitido con uuid → 'emitido' y sin reintento",
          s_ok["estado"] == "emitido" and s_ok["puede_reintentar"] is False, s_ok)

    check.finish()


# ═══════════════════════════════════════════════════════════════════════════════
# PARTE 2 — camino COMPLETO con FakeWasabil (BD real, datos MARCADOS)
# ═══════════════════════════════════════════════════════════════════════════════

_FOLIO = {"n": 9970000}


def _folio_test() -> str:
    """Folio NUMÉRICO único para los anticipos registrados a mano.

    Numérico porque la referencia 33 apunta a un DTE y el SII exige el folio numérico
    (hallazgo B-2): esta suite usaba folios con el MARK adentro ('__MWF7A__-A1') y por
    eso no vio que un 'N/A-99' viajaba al SII. La banda 997xxxx no choca con folios
    reales (los del SII son correlativos chicos) y `limpiar` la borra igual, porque el
    barrido va por numero_cotizacion, no por folio."""
    _FOLIO["n"] += 1
    return str(_FOLIO["n"])


def _anticipo_manual(db, cot, neto, folio=None, **extra):
    """Factura de anticipo por la VÍA MANUAL (folio digitado): monta el escenario sin
    depender de la emisión simulada, que ya se ejercita aparte en el escenario 1."""
    r = client.post(f"{CONTAB}/facturas", json={
        "cotizacion_id": cot.id, "es_anticipo": True,
        "monto_neto_anticipo": neto, "numero_factura": folio or _folio_test(), **extra})
    assert r.status_code == 200, r.text
    return r.json()["id"]


# Montos de la venta patrón con adelanto (para escribirlos a mano en los asserts):
# neto 200.000 + IVA 19% = 238.000 de total; el despacho parcial vale 110.000 netos.
BRUTO_DESPACHO = 130900.0     # 110.000 × 1,19
ADELANTO = 59500.0            # 50% de la venta, en bruto
NETO_ANTICIPO = 50000.0       # neto de la factura de anticipo (bruto 59.500 = ADELANTO)


def _adelanto_de(db, cot_id):
    return (db.query(MonzaContAdelanto)
            .filter(MonzaContAdelanto.cotizacion_id == cot_id).first())


def _cobs_adelanto_de_venta(db, cot_id):
    """Cobranzas medio='adelanto' de TODAS las facturas de la venta — el otro lado del
    invariante `adel.monto_aplicado == Σ cobranzas 'adelanto'`."""
    ids = [f.id for f in facturas_de(db, cot_id)]
    if not ids:
        return []
    return (db.query(MonzaContCobranza)
            .filter(MonzaContCobranza.factura_id.in_(ids),
                    MonzaContCobranza.medio == "adelanto")
            .order_by(MonzaContCobranza.id.asc()).all())


def _anticipo_de(db, cot_id):
    return next((f for f in facturas_de(db, cot_id) if int(f.es_anticipo or 0) == 1), None)


def _venta_con_adelanto_en_otra_factura(db, guia: str, *, con_factoring: bool):
    """Monta el escenario del RE-RUTEO (Fase 7, vía B) en el ORDEN en que ocurre de
    verdad en la calle:

      1. la venta lleva adelanto informado 50%;
      2. la factura del DESPACHO ya está registrada (vía manual, con folio numérico);
      3. RECIÉN AHÍ se verifica el adelanto → la plata cae en la factura del despacho,
         porque la factura de anticipo todavía no existe;
      4. (opcional) esa factura se cede a un FACTOR: su saldo pasa a ser retención del
         factor, así que el re-ruteo NO va a poder mover la plata y tiene que AVISAR.

    Devuelve (cot, factura_despacho_id, folio_despacho)."""
    cot, desp, _a, _b = crear_venta(db, MARK, numero_guia_manual=guia, pct_adelanto=50)
    folio_desp = _folio_test()
    r = client.post(f"{CONTAB}/facturas", json={
        "cotizacion_id": cot.id, "despacho_id": desp.id, "numero_factura": folio_desp})
    assert r.status_code == 200, r.text
    fdesp_id = r.json()["id"]
    ra = client.post(f"{CONTAB}/ventas/{cot.id}/adelanto/verificar",
                     json={"monto": ADELANTO, "fecha_pago": "2026-07-13"})
    assert ra.status_code == 200, ra.text
    if con_factoring:
        rf = client.post(f"{CONTAB}/facturas/{fdesp_id}/factoring",
                         json={"monto_adelantado": 0, "empresa_factoring": "Factor Andes"})
        assert rf.status_code == 200, rf.text
    return cot, fdesp_id, folio_desp


def run_e2e():
    check = Checker()
    db = SessionLocal()
    fake.install()
    limpiar(db, MARK)
    try:
        CURRENT["empresa"] = "automotriz"

        # ── 0 · AISLAMIENTO: se habla con el fake, jamás con el API real ────────
        check("el client de Monza está pisado por el FAKE (cero llamadas al API real)",
              getattr(monza_client.crear_documento, "__self__", None) is fake)
        check("el client de Grupo AM (token de la OTRA marca) no interviene aquí",
              ga_client.crear_documento is not monza_client.crear_documento)

        # ═══ 1 · ANTICIPO POR LA VÍA SII + FACTURA DEL DESPACHO ════════════════
        cot, desp, _i1, _i2 = crear_venta(db, MARK, numero_guia_manual="G-1",
                                          pct_adelanto=50)
        dte_guia(db, desp, uuid="uuid-guia-f7a", status_id=STATUS_EMITIDO, folio="777",
                 payload_json='{"documentDate": "2026-06-20"}')
        total_venta = float(cot.total_bruto)   # 238.000 (200.000 + 19%)

        anticipo = {"cotizacion_id": cot.id, "es_anticipo": True,
                    "monto_neto_anticipo": 50000}
        p = client.post(f"{BASE}/preview", json=anticipo).json()
        check("preview del anticipo: puede_emitir",
              p.get("puede_emitir") is True, p.get("problemas"))
        check("1 sola línea, numero_parte 'ANTICIPO'",
              len(p["lineas"]) == 1 and p["lineas"][0]["numero_parte"] == "ANTICIPO",
              p["lineas"])
        check("totales con el IVA CONGELADO de la venta (no un 19% fijo)",
              p["totales"] == {"neto": 50000, "iva": 9500, "bruto": 59500,
                               "iva_rate": 0.19}, p["totales"])
        check("el ANTICIPO no lleva referencia 52 (ni 33): solo la 801 de la venta",
              [x["tipo"] for x in p["referencias"]] == ["801"], p["referencias"])
        check("el preview publica es_anticipo y descuentos vacíos (para el modal)",
              p["es_anticipo"] is True and p["descuentos"] == [], p)

        p = client.post(f"{BASE}/preview", json={**anticipo, "tipo_doc": "boleta"}).json()
        check("anticipo como BOLETA → bloquea (la vía B es un DTE 33)",
              p["puede_emitir"] is False
              and any("factura" in x for x in p["problemas"]), p["problemas"])

        p = client.post(f"{BASE}/preview", json={"cotizacion_id": cot.id,
                                                 "es_anticipo": True,
                                                 "monto_neto_anticipo": total_venta}).json()
        check("anticipo que excede el total de la venta → bloquea con 'excede'",
              p["puede_emitir"] is False
              and any("excede" in x for x in p["problemas"]), p["problemas"])

        # Emitir el anticipo CON despacho_id en el payload: se fuerza a NULL (segundo
        # cinturón) y el documento sigue sin referencia 52.
        creados_antes = len(fake.creados)
        fake.folio_emitido = "570001"
        r = client.post(f"{BASE}/emitir", json={**anticipo, "despacho_id": desp.id})
        check("emitir anticipo → 200", r.status_code == 200, r.text)
        db.rollback()
        facturas = facturas_de(db, cot.id)
        fa = facturas[0]
        check("la factura nace es_anticipo=1 y SIN despacho, aunque el payload traía uno",
              len(facturas) == 1 and int(fa.es_anticipo or 0) == 1
              and fa.despacho_id is None, (len(facturas), fa.es_anticipo, fa.despacho_id))
        check("un solo documento hacia Wasabil", len(fake.creados) == creados_antes + 1,
              len(fake.creados) - creados_antes)
        doc = fake.creados[-1]
        check("el documento enviado es un DTE 33", doc["sii_document_type_code"] == 33,
              doc.get("sii_document_type_code"))
        check("el payload del anticipo NO lleva referencia 52 ni 33",
              [x["document_type"] for x in doc["references"]] == ["801"], doc["references"])
        check("líneas del anticipo: 1, price>0, SIN discount",
              len(doc["details"]) == 1 and doc["details"][0]["price"] == 50000
              and "discount" not in doc["details"][0], doc["details"])

        client.get(f"{BASE}/{fa.id}/estado")
        db.rollback(); db.refresh(fa)
        check("tras el sondeo, el folio del SII queda en la factura de anticipo",
              (fa.numero_factura or "") == "570001", fa.numero_factura)

        # ── CANDADO ANTI DOBLE EMISIÓN, POR VENTA ──────────────────────────────
        # El claim se pone a mano (simula "otra pestaña emitiendo ahora mismo").
        fila = dte_de_factura(db, fa.id)
        fila.en_vuelo_desde = datetime.utcnow()
        db.commit()
        creados_antes = len(fake.creados)
        # confirmar_segundo_anticipo: sin la marca lo frenaría ANTES el guard A-1 (un
        # anticipo por venta, de otro bloque) y este check no probaría el candado. Con
        # la marca puesta, el ÚNICO que puede frenarlo es el candado por venta.
        r = client.post(f"{BASE}/emitir", json={"cotizacion_id": cot.id,
                                                "es_anticipo": True,
                                                "monto_neto_anticipo": 10000,
                                                "confirmar_segundo_anticipo": True})
        check("claim fresco → un SEGUNDO anticipo de la misma venta da 409 'EN CURSO'",
              r.status_code == 409 and "EN CURSO" in r.json().get("detail", ""), r.text)
        check("el 409 dice qué hacer ('Espera su resultado')",
              "Espera su resultado" in r.json().get("detail", ""), r.text)
        r2 = client.post(f"{BASE}/emitir", json={"cotizacion_id": cot.id,
                                                 "despacho_id": desp.id})
        check("el candado es POR VENTA: también frena la factura del DESPACHO",
              r2.status_code == 409 and "EN CURSO" in r2.json().get("detail", ""), r2.text)
        db.rollback()
        check("el emitir bloqueado NO creó ninguna factura nueva",
              len(facturas_de(db, cot.id)) == 1, facturas_de(db, cot.id))
        check("NADA llegó a Wasabil durante la ventana del claim",
              len(fake.creados) == creados_antes, fake.creados[creados_antes:])
        fila = dte_de_factura(db, fa.id)
        fila.en_vuelo_desde = None
        db.commit()

        # ── FACTURA DEL DESPACHO: descuenta el anticipo ────────────────────────
        despacho = {"cotizacion_id": cot.id, "despacho_id": desp.id}
        p = client.post(f"{BASE}/preview", json=despacho).json()
        check("preview del despacho: puede_emitir con el descuento aplicado",
              p["puede_emitir"] is True, p["problemas"])
        linea_dsc = [x for x in p["lineas"] if x["numero_parte"] == "DESCUENTO"]
        check("línea DESCUENTO con total negativo y el folio del anticipo en la glosa",
              len(linea_dsc) == 1 and linea_dsc[0]["total_neto"] == -50000
              and "570001" in (linea_dsc[0]["descripcion"] or ""), linea_dsc)
        check("el neto del preview ya viene DESCONTADO (110.000 − 50.000) y el IVA "
              "se recalcula sobre él",
              p["totales"]["neto"] == 60000 and p["totales"]["iva"] == round(60000 * 0.19),
              p["totales"])
        check("el preview publica el descuento para el modal (folio + monto neto)",
              p["descuentos"] and p["descuentos"][0]["folio"] == "570001"
              and p["descuentos"][0]["monto_neto"] == 50000, p["descuentos"])
        check("referencias 801 + 52 + 33 (el anticipo descontado)",
              [x["tipo"] for x in p["referencias"]] == ["801", "52", "33"],
              p["referencias"])
        ref33 = [x for x in p["referencias"] if x["tipo"] == "33"][0]
        check("la 33 apunta al folio 570001, con fecha y motivo 'Descuento anticipo'",
              ref33["folio"] == "570001" and ref33["fecha"]
              and ref33["descripcion"] == "Descuento anticipo", ref33)

        creados_antes = len(fake.creados)
        fake.folio_emitido = "570002"
        r = client.post(f"{BASE}/emitir", json=despacho)
        check("emitir la factura del despacho → 200", r.status_code == 200, r.text)
        doc = fake.creados[-1]
        check("un solo documento nuevo hacia Wasabil",
              len(fake.creados) == creados_antes + 1, len(fake.creados) - creados_antes)
        check("NINGUNA línea negativa ni con price<=0 viaja al payload REST",
              _positivas(doc["details"]), doc["details"])
        check("el descuento viaja como `discount` PORCENTUAL por línea",
              any("discount" in d for d in doc["details"]), doc["details"])
        check("neto del DTE == neto local (60.000): la cuadratura se mantiene",
              _neto_dte(doc["details"]) == 60000.0,
              (_neto_dte(doc["details"]), [d.get("discount") for d in doc["details"]]))
        check("la 33 del anticipo viaja también en el REST (snake_case)",
              [d["document_type"] for d in doc["references"]] == ["801", "52", "33"],
              doc["references"])

        db.rollback()
        final = facturas_de(db, cot.id)[-1]
        # NO-VACUIDAD del check de arriba: la factura local SÍ tiene una línea negativa
        # (y con su FK al anticipo, de la que depende _anticipos_referenciados en el
        # reintento) — lo que se prueba es que el DTE salió con una línea MENOS.
        negativas = [it for it in final.items if float(it.total_neto or 0) < 0]
        check("la línea de DESCUENTO persiste con anticipo_factura_id apuntando al anticipo",
              len(negativas) == 1 and negativas[0].anticipo_factura_id == fa.id
              and float(negativas[0].total_neto) == -50000,
              [(it.numero_parte, it.total_neto, it.anticipo_factura_id) for it in final.items])
        check("el DTE viajó con una línea MENOS que la factura local (la negativa se quedó)",
              len(doc["details"]) == len(list(final.items)) - 1,
              (len(doc["details"]), len(list(final.items))))
        # La cuadratura, cerrada en los TRES lugares donde vive el monto: la factura de
        # Contabilidad, la fila DTE que la respalda y el documento que salió al SII.
        fila_dte = dte_de_factura(db, final.id)
        check("cuadratura de punta a punta: neto factura == neto fila DTE == neto del "
              "documento emitido",
              float(final.monto_neto) == 60000.0
              and float(fila_dte.monto_neto) == 60000.0
              and _neto_dte(doc["details"]) == 60000.0,
              (final.monto_neto, fila_dte.monto_neto, _neto_dte(doc["details"])))
        client.get(f"{BASE}/{final.id}/estado")
        db.rollback()
        brutos = [float(f.monto_bruto) for f in facturas_de(db, cot.id)]
        check("Σ brutos == anticipo + factura final (59.500 + 71.400): sin doble cobro",
              round(sum(brutos), 0) == 130900, brutos)
        check("Σ brutos <= total de la venta (invariante central de la vía B)",
              sum(brutos) <= total_venta + 1, (sum(brutos), total_venta))

        # ── REINTENTO de la factura persistida con el anticipo sin folio ───────
        # Es el SEGUNDO cinturón: el de Contabilidad actúa antes, sobre una factura
        # que todavía no existe; éste protege el re-armado de una ya emitida.
        from monza_wasabil_dte.router import _armar_payload_factura  # noqa: PLC0415
        db.refresh(fa)
        folio_real, fa.numero_factura = fa.numero_factura, None
        db.commit(); db.refresh(final)
        _doc, probs_reint = _armar_payload_factura(db, final, client_id=160065, issue=False)
        check("anticipo sin folio → el REINTENTO de la factura persistida BLOQUEA "
              "(guard del placeholder '#<id>')",
              any("folio SII" in x for x in probs_reint), probs_reint)
        fa.numero_factura = folio_real
        db.commit(); db.refresh(final)
        _doc, probs_ok = _armar_payload_factura(db, final, client_id=160065, issue=False)
        check("con el folio de vuelta, el reintento re-arma el MISMO documento",
              not probs_ok
              and [r["documentType"] for r in _doc["references"]] == ["801", "52", "33"],
              (probs_ok, _doc["references"]))
        check("el reintento re-arma el descuento IDÉNTICO (neto 60.000, sin negativas)",
              _neto_dte(_doc["details"]) == 60000.0 and _positivas(_doc["details"]),
              _doc["details"])
        check("re-armar NO emite: el documento nace con issue=False",
              _doc["issue"] is False, _doc["issue"])

        # ═══ 2 · ANTICIPO SIN FOLIO SII → BLOQUEA la factura del despacho ══════
        cot2, desp2, _a, _b = crear_venta(db, MARK, numero_guia_manual="G-9")
        dte_guia(db, desp2, uuid="uuid-guia-f7b", status_id=STATUS_EMITIDO, folio="778",
                 payload_json='{"documentDate": "2026-06-20"}')
        fa2_id = _anticipo_manual(db, cot2, 20000)
        db.rollback()
        fa2 = db.get(MonzaContFacturaCliente, fa2_id)
        fa2.numero_factura = None      # simula "emisión electrónica en curso o fallida"
        db.commit()
        p = client.post(f"{BASE}/preview", json={"cotizacion_id": cot2.id,
                                                 "despacho_id": desp2.id}).json()
        check("anticipo SIN folio SII → el preview del despacho BLOQUEA "
              "(ni lo ignora ni lo descuenta)",
              p["puede_emitir"] is False
              and any("folio del SII" in x or "folio SII" in x for x in p["problemas"]),
              p["problemas"])
        creados_antes = len(fake.creados)
        r = client.post(f"{BASE}/emitir", json={"cotizacion_id": cot2.id,
                                                "despacho_id": desp2.id})
        check("...y el emitir también 409, sin mandar nada a Wasabil",
              r.status_code == 409 and len(fake.creados) == creados_antes, r.text)
        db.rollback()
        check("el 409 no dejó factura zombi consumiendo el cupo facturable",
              [int(f.es_anticipo or 0) for f in facturas_de(db, cot2.id)] == [1],
              facturas_de(db, cot2.id))

        # ═══ 3 · ANTICIPO POR TODO LO DESPACHADO → el DTE quedaría en $0 ═══════
        # La vía manual lo permite con ADVERTENCIA (es un caso legítimo del negocio),
        # pero el SII no acepta un DTE en cero: la vía electrónica BLOQUEA en el
        # PREVIEW, antes de crear la factura local.
        cot3, desp3, _c, _d = crear_venta(db, MARK, numero_guia_manual="G-3")
        dte_guia(db, desp3, uuid="uuid-guia-f7c", status_id=STATUS_EMITIDO, folio="779",
                 payload_json='{"documentDate": "2026-06-20"}')
        _anticipo_manual(db, cot3, 110000)   # == neto del despacho
        p = client.post(f"{BASE}/preview", json={"cotizacion_id": cot3.id,
                                                 "despacho_id": desp3.id}).json()
        check("anticipo que cubre TODO lo despachado → el preview BLOQUEA con 'en $0'",
              p["puede_emitir"] is False
              and any("en $0" in x for x in p["problemas"]), p["problemas"])
        check("el preview igual muestra el descuento y el neto 0 (el operador ve por qué)",
              p["totales"]["neto"] == 0 and p["descuentos"], (p["totales"], p["descuentos"]))
        creados_antes = len(fake.creados)
        r = client.post(f"{BASE}/emitir", json={"cotizacion_id": cot3.id,
                                                "despacho_id": desp3.id})
        db.rollback()
        check("...y el emitir da 409 sin tocar Wasabil ni crear factura zombi",
              r.status_code == 409 and len(fake.creados) == creados_antes
              and [int(f.es_anticipo or 0) for f in facturas_de(db, cot3.id)] == [1],
              (r.status_code, facturas_de(db, cot3.id)))

        # ═══ 4 · DOS anticipos: FIFO, dos referencias 33 y cuadratura ══════════
        cot4, desp4, _e, _f = crear_venta(db, MARK, numero_guia_manual="G-4")
        dte_guia(db, desp4, uuid="uuid-guia-f7d", status_id=STATUS_EMITIDO, folio="780",
                 payload_json='{"documentDate": "2026-06-20"}')
        # `confirmar_segundo_anticipo`: desde el arreglo A-1 la venta admite UN anticipo
        # y el segundo exige marca explícita. Este escenario prueba justamente el caso
        # de dos anticipos vivos, así que la pide (es la puerta legítima, no un bypass).
        folio_a1 = _folio_test()
        folio_a2 = _folio_test()
        _anticipo_manual(db, cot4, 30000, folio_a1)
        _anticipo_manual(db, cot4, 20000, folio_a2, confirmar_segundo_anticipo=True)
        p = client.post(f"{BASE}/preview", json={"cotizacion_id": cot4.id,
                                                 "despacho_id": desp4.id}).json()
        check("dos anticipos: el preview los descuenta AMBOS en orden FIFO",
              [d["folio"] for d in p["descuentos"]] == [folio_a1, folio_a2]
              and [d["monto_neto"] for d in p["descuentos"]] == [30000, 20000],
              p["descuentos"])
        check("dos anticipos: referencias 801 + 52 + 33 + 33",
              [x["tipo"] for x in p["referencias"]] == ["801", "52", "33", "33"],
              p["referencias"])
        check("dos anticipos: neto del preview 110.000 − 50.000 = 60.000",
              p["totales"]["neto"] == 60000, p["totales"])
        creados_antes = len(fake.creados)
        fake.folio_emitido = "570004"
        r = client.post(f"{BASE}/emitir", json={"cotizacion_id": cot4.id,
                                                "despacho_id": desp4.id})
        check("dos anticipos: emitir → 200", r.status_code == 200, r.text)
        doc = fake.creados[-1]
        check("dos anticipos: un solo documento, sin líneas negativas, neto DTE 60.000",
              len(fake.creados) == creados_antes + 1 and _positivas(doc["details"])
              and _neto_dte(doc["details"]) == 60000.0,
              (len(fake.creados) - creados_antes, doc["details"]))
        check("dos anticipos: las DOS referencias 33 viajan con sus folios",
              [d["folio"] for d in doc["references"] if d["document_type"] == "33"]
              == [folio_a1, folio_a2], doc["references"])
        db.rollback()
        brutos4 = [float(f.monto_bruto) for f in facturas_de(db, cot4.id)]
        check("dos anticipos: Σ brutos == 35.700 + 23.800 + 71.400 (sin doble cobro)",
              round(sum(brutos4), 0) == 130900, brutos4)

        # ═══ 5 · TOPE DE REFERENCIAS: 801 + 52 + 4×33 = 6 > 5 → BLOQUEA ════════
        cot5, desp5, _g, _h = crear_venta(db, MARK, numero_guia_manual="G-5")
        dte_guia(db, desp5, uuid="uuid-guia-f7e", status_id=STATUS_EMITIDO, folio="781",
                 payload_json='{"documentDate": "2026-06-20"}')
        for i in range(1, 5):
            _anticipo_manual(db, cot5, 10000, confirmar_segundo_anticipo=(i > 1))
        p = client.post(f"{BASE}/preview", json={"cotizacion_id": cot5.id,
                                                 "despacho_id": desp5.id}).json()
        check(f"cuatro anticipos → {MAX_REFERENCIAS + 1} referencias: el preview BLOQUEA "
              "con una salida EJECUTABLE (facturar en tandas / vía manual), no con la "
              "orden imposible de 'dividir el descuento' (hallazgo B-6)",
              p["puede_emitir"] is False
              and any("tandas más chicas" in x and "vía manual" in x
                      for x in p["problemas"]), p["problemas"])
        creados_antes = len(fake.creados)
        r = client.post(f"{BASE}/emitir", json={"cotizacion_id": cot5.id,
                                                "despacho_id": desp5.id})
        db.rollback()
        check("...y el emitir da 409 sin mandar nada ni dejar factura zombi",
              r.status_code == 409 and len(fake.creados) == creados_antes
              and all(int(f.es_anticipo or 0) == 1 for f in facturas_de(db, cot5.id)),
              (r.status_code, facturas_de(db, cot5.id)))

        # ═══ 6 · UN ANTICIPO NUNCA DESCUENTA A OTRO ═══════════════════════════
        # El descuento de anticipos vive en `_construir_factura`; la vía B tiene su
        # propia constructora y `_referencias_de_venta` le anula los anticipos. Si
        # alguna de las dos cosas se cayera, el segundo anticipo saldría al SII
        # descontando al primero: cobrar de menos un adelanto YA recibido.
        cot6, desp6, _i, _j = crear_venta(db, MARK, numero_guia_manual="G-6")
        dte_guia(db, desp6, uuid="uuid-guia-f7f", status_id=STATUS_EMITIDO, folio="782",
                 payload_json='{"documentDate": "2026-06-20"}')
        _anticipo_manual(db, cot6, 40000)
        # confirmar_segundo_anticipo: el guard A-1 (un anticipo por venta) es de otro
        # bloque y es correcto; lo que aquí se prueba es lo de MÁS ABAJO — que un
        # anticipo, aun autorizado, no descuente a otro anticipo.
        segundo = {"cotizacion_id": cot6.id, "es_anticipo": True,
                   "monto_neto_anticipo": 25000, "confirmar_segundo_anticipo": True}
        p = client.post(f"{BASE}/preview", json=segundo).json()
        check("2º anticipo sobre una venta que YA tiene uno con folio: puede_emitir, "
              "1 sola línea y CERO descuentos",
              p["puede_emitir"] is True and len(p["lineas"]) == 1
              and p["descuentos"] == [], (p["problemas"], p["lineas"], p["descuentos"]))
        check("2º anticipo: sigue sin 52 y SIN 33 (no referencia al primero)",
              [x["tipo"] for x in p["referencias"]] == ["801"], p["referencias"])
        creados_antes = len(fake.creados)
        fake.folio_emitido = "570006"
        r = client.post(f"{BASE}/emitir", json=segundo)
        doc = fake.creados[-1]
        check("2º anticipo: emitido con 1 línea de $25.000, sin discount y solo la 801",
              r.status_code == 200 and len(fake.creados) == creados_antes + 1
              and len(doc["details"]) == 1 and doc["details"][0]["price"] == 25000
              and "discount" not in doc["details"][0]
              and [x["document_type"] for x in doc["references"]] == ["801"],
              (r.status_code, doc["details"], doc["references"]))
        db.rollback()
        check("2º anticipo: no nació ninguna línea con anticipo_factura_id",
              db.query(MonzaContFacturaClienteItem).filter(
                  MonzaContFacturaClienteItem.factura_id.in_(
                      [f.id for f in facturas_de(db, cot6.id)]),
                  MonzaContFacturaClienteItem.anticipo_factura_id.isnot(None)).count() == 0)

        # ═══ 7 · CARRERA REAL: dos clics simultáneos en Emitir un ANTICIPO ═════
        # Es el caso donde el candado por venta es la ÚNICA defensa: en la factura de
        # un despacho, si el candado fallara, al perdedor todavía lo frena el tope "ese
        # despacho ya fue facturado por completo". El anticipo NO consume mercadería —
        # dos anticipos de $50.000 caben de sobra en una venta de $238.000— así que sin
        # el candado saldrían DOS documentos tributarios REALES.
        cot7, _d7, _k, _l = crear_venta(db, MARK, con_despacho=False)
        cot7_id = cot7.id
        db.close()   # el test no debe retener locks mientras corren los hilos
        creados_antes = len(fake.creados)
        fake.folio_emitido = "570007"
        # Ensancha la ventana HTTP del ganador: el perdedor llega al lock con el claim
        # ya commiteado y la respuesta todavía sin volver.
        fake.antes_de_crear = lambda payload: time.sleep(0.4)
        barrera = threading.Barrier(2)
        resultados: list = []

        def _correr():
            barrera.wait()
            try:
                r = client.post(f"{BASE}/emitir", json={
                    "cotizacion_id": cot7_id, "es_anticipo": True,
                    "monto_neto_anticipo": 50000})
                resultados.append((r.status_code, r.text[:200]))
            except Exception as exc:                     # pragma: no cover
                resultados.append((-1, repr(exc)[:200]))

        hilos = [threading.Thread(target=_correr) for _ in range(2)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join(timeout=90)
        fake.antes_de_crear = None
        db = SessionLocal()
        codigos = sorted(c for c, _t in resultados)
        check("carrera de anticipos: exactamente UN 200 y UN 409", codigos == [200, 409],
              resultados)
        check("carrera de anticipos: al perdedor lo frena el CANDADO por venta "
              "(no hay tope de mercadería que lo salve)",
              any("EN CURSO" in t for c, t in resultados if c == 409), resultados)
        check("carrera de anticipos: UN SOLO documento salió a Wasabil",
              len(fake.creados) == creados_antes + 1,
              [x.get("invoice_reference") for x in fake.creados[creados_antes:]])
        check("carrera de anticipos: UNA SOLA factura local (el perdedor no deja zombi)",
              len(facturas_de(db, cot7_id)) == 1, facturas_de(db, cot7_id))

        # ═══ 8 · CUADRATURA CON PRECIOS DECIMALES, DE PUNTA A PUNTA (B-1) ══════
        # El hallazgo B-1 vivía en el reparto del descuento, pero se manifiesta aquí:
        # el neto de la factura que registra Contabilidad tiene que ser EXACTAMENTE el
        # neto del documento que sale al SII. precio_unitario_clp es Float y entra del
        # body sin redondear a peso, así que las líneas terminadas en .5 son
        # alcanzables en producción. Con el reparto viejo (en centavos) esta venta
        # emitía $1 menos que lo registrado.
        cot8, _d8, i81, i82 = crear_venta(db, MARK, precio1=33333.5, precio2=11111.5,
                                          con_despacho=False)
        desp8 = despacho_extra(db, MARK, cot8, {i81.id: 1, i82.id: 1},
                               numero_guia="G-8")
        dte_guia(db, desp8, uuid="uuid-guia-f7h", status_id=STATUS_EMITIDO, folio="788",
                 payload_json='{"documentDate": "2026-06-20"}')
        # Neto local de la mercadería: half-up a PESO por línea (33.334 + 11.112).
        neto_desp8 = _total_linea(33333.5, 1) + _total_linea(11111.5, 1)
        check("montaje: las dos líneas .5 dan un neto local de 44.446 (no 44.445)",
              neto_desp8 == 44446, neto_desp8)
        _anticipo_manual(db, cot8, 40000)
        p = client.post(f"{BASE}/preview", json={"cotizacion_id": cot8.id,
                                                 "despacho_id": desp8.id}).json()
        check("decimales: el preview puede emitir y su neto es 44.446 − 40.000 = 4.446",
              p["puede_emitir"] is True and p["totales"]["neto"] == 4446,
              (p["problemas"], p["totales"]))
        creados_antes = len(fake.creados)
        fake.folio_emitido = "570008"
        r = client.post(f"{BASE}/emitir", json={"cotizacion_id": cot8.id,
                                                "despacho_id": desp8.id})
        check("decimales: emitir → 200", r.status_code == 200, r.text)
        doc8 = fake.creados[-1]
        db.rollback()
        final8 = facturas_de(db, cot8.id)[-1]
        check("DECIMALES · CUADRATURA: neto del DTE == neto de la factura local == 4.446 "
              "(con el reparto viejo, en centavos, el DTE salía en 4.445)",
              _neto_dte(doc8["details"]) == 4446.0 and float(final8.monto_neto) == 4446.0,
              (_neto_dte(doc8["details"]), final8.monto_neto,
               [d.get("discount") for d in doc8["details"]]))
        check("decimales: ninguna línea negativa ni ningún discount fuera de (0,100]",
              _positivas(doc8["details"])
              and all(0 < d["discount"] <= 100 for d in doc8["details"] if "discount" in d),
              doc8["details"])
        check("decimales: el IVA local se calculó sobre el neto ya descontado",
              float(final8.iva) == round(4446 * 0.19), (final8.iva, final8.monto_neto))

        # ═══ 9 · ANTICIPO CON FOLIO NO NUMÉRICO → BLOQUEA (B-2) ════════════════
        # El folio de un anticipo registrado a mano lo TECLEA el operador. Reproducido:
        # 'N/A-99' viajaba en la referencia 33 y el SII rechaza el documento — con el
        # folio propio de la factura YA CONSUMIDO.
        cot9, desp9, _m, _n = crear_venta(db, MARK, numero_guia_manual="G-9B")
        dte_guia(db, desp9, uuid="uuid-guia-f7i", status_id=STATUS_EMITIDO, folio="789",
                 payload_json='{"documentDate": "2026-06-20"}')
        # El folio malo se escribe DIRECTO en la BD: `crear_factura` ahora lo rechaza al
        # REGISTRAR el anticipo (cierre F7 — el error salía tarde y lejos, recién al
        # facturar el despacho), así que el escenario ya no se puede montar por el
        # endpoint. Sigue siendo un caso REAL —las filas registradas antes de ese guard—,
        # y lo que se prueba aquí es el ÚLTIMO cortafuego: el que arma la referencia 33.
        fa9_id = _anticipo_manual(db, cot9, 20000)
        db.rollback()
        db.get(MonzaContFacturaCliente, fa9_id).numero_factura = "N/A-99"
        db.commit()
        p = client.post(f"{BASE}/preview", json={"cotizacion_id": cot9.id,
                                                 "despacho_id": desp9.id}).json()
        check("anticipo con folio 'N/A-99' → el preview del despacho BLOQUEA",
              p["puede_emitir"] is False
              and any("numérico" in x for x in p["problemas"]), p["problemas"])
        check("...y NINGUNA referencia 33 se armó con esa basura",
              [x["tipo"] for x in p["referencias"]] == ["801", "52"], p["referencias"])
        creados_antes = len(fake.creados)
        r = client.post(f"{BASE}/emitir", json={"cotizacion_id": cot9.id,
                                                "despacho_id": desp9.id})
        db.rollback()
        check("...y el emitir da 409 sin mandar nada a Wasabil ni dejar factura zombi",
              r.status_code == 409 and len(fake.creados) == creados_antes
              and [int(f.es_anticipo or 0) for f in facturas_de(db, cot9.id)] == [1],
              (r.status_code, facturas_de(db, cot9.id)))
        # Corregido el folio por uno numérico, la misma venta emite sin tocar nada más:
        # el bloqueo es del DATO, no de la venta (el mensaje manda exactamente ahí).
        fa9 = db.get(MonzaContFacturaCliente, fa9_id)
        fa9.numero_factura = _folio_test()
        db.commit()
        p = client.post(f"{BASE}/preview", json={"cotizacion_id": cot9.id,
                                                 "despacho_id": desp9.id}).json()
        check("con el folio corregido a numérico, la MISMA venta ya puede emitir",
              p["puede_emitir"] is True
              and [x["tipo"] for x in p["referencias"]] == ["801", "52", "33"],
              (p["problemas"], p["referencias"]))

        # ═══ 10 · COLISIÓN DE FOLIO: el error tiene que traer el REMEDIO (B-3) ══
        # El UNIQUE de numero_factura es GLOBAL: si el folio que devuelve el SII ya
        # estaba registrado a mano en otra factura, ésta queda emitida ante el SII y
        # SIN N° local. Antes el error decía "resolver duplicado a mano" y no nombraba
        # a la culpable: el operador quedaba sin salida salvo SQL.
        cot10, _d10, _o, _p2 = crear_venta(db, MARK, con_despacho=False)
        folio_ocupado = _folio_test()
        ocupante_id = _anticipo_manual(db, cot10, 5000, folio_ocupado)
        cot11, _d11, _q, _r2 = crear_venta(db, MARK, con_despacho=False)
        fake.folio_emitido = folio_ocupado        # el SII devuelve un folio YA usado
        r = client.post(f"{BASE}/emitir", json={"cotizacion_id": cot11.id,
                                                "es_anticipo": True,
                                                "monto_neto_anticipo": 7000})
        check("colisión: la emisión igual responde 200 (el documento existe ante el SII)",
              r.status_code == 200, r.text)
        fa11_id = facturas_de(db, cot11.id)[-1].id if facturas_de(db, cot11.id) else None
        db.rollback()
        client.get(f"{BASE}/{fa11_id}/estado")
        db.rollback()
        fila11 = dte_de_factura(db, fa11_id)
        err = fila11.error or ""
        check("colisión: la factura local queda SIN N° y el DTE conserva su folio real "
              "(perderlo sería peor que el duplicado)",
              db.get(MonzaContFacturaCliente, fa11_id).numero_factura is None
              and fila11.folio == folio_ocupado,
              (db.get(MonzaContFacturaCliente, fa11_id).numero_factura, fila11.folio))
        check("colisión: el error NOMBRA la factura local culpable (id) — no deja al "
              "operador buscándola con SQL",
              f"#{ocupante_id}" in err, err)
        check("colisión: el error nombra la VENTA de esa factura",
              (db.get(MonzaContFacturaCliente, ocupante_id).numero_cotizacion or "") in err
              and MARK in err, err)
        check("colisión: el error trae el REMEDIO completo (corregir la otra factura y "
              "volver a consultar el estado) y prohíbe re-emitir",
              "Contabilidad" in err and "estado" in err and "No re-emitas" in err, err)
        # Y el remedio FUNCIONA: corregida la otra factura, el sondeo graba el folio solo.
        ocupante = db.get(MonzaContFacturaCliente, ocupante_id)
        ocupante.numero_factura = _folio_test()
        db.commit()
        client.get(f"{BASE}/{fa11_id}/estado")
        db.rollback()
        check("colisión: tras corregir la otra factura, el sondeo graba el folio SOLO "
              "(el remedio del mensaje es real, no una promesa)",
              (db.get(MonzaContFacturaCliente, fa11_id).numero_factura or "")
              == folio_ocupado,
              db.get(MonzaContFacturaCliente, fa11_id).numero_factura)

        # ═══ 11 · LA ADVERTENCIA DEL RE-RUTEO, POR LA VÍA SII ══════════════════
        # Fase 7. Cuando nace una factura de ANTICIPO y el adelanto de esa venta YA
        # estaba aplicado a otra factura, Contabilidad intenta RE-ENCAUZAR la plata
        # hacia el anticipo. Si no puede (factoring vigente en la otra factura, cobranza
        # conciliada con el banco, DTE sin emitir), devuelve una ADVERTENCIA con el
        # remedio — y la factura de anticipo queda POR COBRAR.
        #
        # Por la vía manual esa advertencia ya viajaba en la respuesta de crear_factura.
        # Por la vía SII se DESCARTABA: el adelanto se aplica DIFERIDO, al confirmarse el
        # folio (_finalizar_factura_emitida), y ese retorno se botaba. En pantalla se leía
        # «Factura emitida — Folio SII X» y nada más: el documento nacía impago EN
        # SILENCIO y nadie iba a cobrarlo, porque el sistema decía que estaba pagado.
        #
        # OJO con la idempotencia: el aviso nace UNA sola vez, en el request que GRABA el
        # folio. Por eso se verifican los DOS caminos por los que ese request puede
        # llegar —el propio `emitir` (cuando Wasabil ya responde EMITIDO) y el SONDEO
        # (cuando responde 'procesando' y el folio se confirma después)—: si solo uno
        # propagara el aviso, en producción se perdería según el timing del SII.
        print("\n───── 11 · la advertencia del re-ruteo por la vía SII ─────")

        # ── 11A · EL RE-RUTEO NO PUEDE: factoring vigente en la otra factura ────
        cotA, fdespA, folio_despA = _venta_con_adelanto_en_otra_factura(
            db, "G-W1", con_factoring=True)
        db.rollback()
        fdA = db.get(MonzaContFacturaCliente, fdespA)
        cobs_desp = [c for c in cobranzas_de(db, fdespA) if c.medio == "adelanto"]
        check("11A montaje: el adelanto cayó en la factura del DESPACHO (cobranza "
              "'adelanto' de 59.500), que es el escenario que dispara el re-ruteo",
              len(cobs_desp) == 1 and float(cobs_desp[0].monto) == ADELANTO,
              [(c.medio, float(c.monto)) for c in cobranzas_de(db, fdespA)])
        check("11A montaje: esa factura quedó CEDIDA a un factor (el re-ruteo no podrá "
              "mover la plata)",
              fdA.estado_pago == "factorizada", fdA.estado_pago)

        # Wasabil responde EMITIDO en el propio POST: el folio se confirma DENTRO del
        # `emitir`, así que es ESA respuesta la que tiene que traer el aviso.
        fake.status_respuesta = STATUS_EMITIDO
        folio_antA = _folio_test()
        fake.folio_emitido = folio_antA
        creados_antes = len(fake.creados)
        rA = client.post(f"{BASE}/emitir", json={
            "cotizacion_id": cotA.id, "es_anticipo": True,
            "monto_neto_anticipo": NETO_ANTICIPO})
        bodyA = rA.json() if rA.status_code == 200 else {}
        avisosA = bodyA.get("advertencias") or []
        print(f"     · POST /emitir → {rA.status_code} · folio={bodyA.get('folio')} "
              f"· advertencias={avisosA}")
        check("11A el anticipo SÍ se emite (el documento es real ante el SII: el "
              "problema es la plata, no el documento)",
              rA.status_code == 200 and bodyA.get("estado") == "emitido"
              and bodyA.get("folio") == folio_antA
              and len(fake.creados) == creados_antes + 1, rA.text[:300])
        check("11A la respuesta del EMITIR trae `advertencias` NO VACÍO "
              "(antes se perdía en silencio)", bool(avisosA), bodyA)
        check("11A el aviso dice el MOTIVO: la otra factura está cedida a un factor",
              any("cedida a un factor" in a for a in avisosA), avisosA)
        check("11A el aviso NOMBRA la factura culpable por su folio (no deja al dueño "
              "buscándola)", any(folio_despA in a for a in avisosA), avisosA)
        check("11A el aviso trae el MONTO que no se pudo traspasar, en formato chileno",
              any("$59.500" in a for a in avisosA), avisosA)
        check("11A el aviso dice el ESTADO en que quedó la factura (por cobrar)",
              any("queda por cobrar" in a for a in avisosA), avisosA)
        check("11A el aviso trae el REMEDIO EJECUTABLE (revertir esa cobranza / liquidar "
              "el factoring y volver a aprobar el adelanto en Tesorería)",
              any("revierte esa cobranza" in a and "liquida el factoring" in a
                  and "Tesorería" in a for a in avisosA), avisosA)
        db.rollback()
        fantA = _anticipo_de(db, cotA.id)
        check("11A el aviso dice la VERDAD: la factura de anticipo quedó por cobrar, "
              "con su saldo completo y sin cobranzas",
              fantA is not None and float(fantA.saldo) == ADELANTO
              and not cobranzas_de(db, fantA.id),
              (fantA and float(fantA.saldo), fantA and cobranzas_de(db, fantA.id)))
        adelA = _adelanto_de(db, cotA.id)
        cobsA = _cobs_adelanto_de_venta(db, cotA.id)
        check("11A sin fuga de plata: INVARIANTE monto_aplicado == Σ cobranzas 'adelanto' "
              "(la plata sigue donde estaba, en la factura cedida)",
              adelA is not None
              and abs(float(adelA.monto_aplicado) - sum(float(c.monto) for c in cobsA)) < 0.01
              and float(adelA.monto_aplicado) == ADELANTO,
              (adelA and float(adelA.monto_aplicado), [float(c.monto) for c in cobsA]))

        # ── 11B · CAMINO FELIZ: sin factoring el re-ruteo SÍ ocurre ─────────────
        # No-vacuidad de 11A: lo que bloquea es el FACTORING, no el mecanismo. Sin él la
        # plata se mueve sola, el anticipo nace PAGADO y no hay nada que advertir.
        cotB, fdespB, _folio_despB = _venta_con_adelanto_en_otra_factura(
            db, "G-W2", con_factoring=False)
        fake.status_respuesta = STATUS_EMITIDO
        folio_antB = _folio_test()
        fake.folio_emitido = folio_antB
        rB = client.post(f"{BASE}/emitir", json={
            "cotizacion_id": cotB.id, "es_anticipo": True,
            "monto_neto_anticipo": NETO_ANTICIPO})
        bodyB = rB.json() if rB.status_code == 200 else {}
        print(f"     · POST /emitir (sin factoring) → {rB.status_code} "
              f"· folio={bodyB.get('folio')} · advertencias={bodyB.get('advertencias')}")
        check("11B camino feliz: emitir → 200 y `advertencias` VACÍO (no se inventan "
              "avisos cuando no hay nada que avisar)",
              rB.status_code == 200 and bodyB.get("advertencias") == [], rB.text[:300])
        check("11B el campo `advertencias` es ADITIVO: viene SIEMPRE en la respuesta, "
              "aunque esté vacío (el frontend no tiene que adivinar)",
              "advertencias" in bodyB, sorted(bodyB.keys()))
        db.rollback()
        fantB = _anticipo_de(db, cotB.id)
        cobs_antB = cobranzas_de(db, fantB.id) if fantB else []
        check("11B el re-ruteo SÍ ocurrió: la factura de anticipo queda PAGADA con su "
              "cobranza 'adelanto' de 59.500",
              fantB is not None and float(fantB.saldo) == 0.0
              and fantB.estado_pago == "pagada"
              and [(c.medio, float(c.monto)) for c in cobs_antB] == [("adelanto", ADELANTO)],
              (fantB and float(fantB.saldo), fantB and fantB.estado_pago,
               [(c.medio, float(c.monto)) for c in cobs_antB]))
        fdB = db.get(MonzaContFacturaCliente, fdespB)
        check("11B la plata SALIÓ de la factura del despacho (vuelve a deber sus 130.900): "
              "se movió, no se duplicó",
              float(fdB.saldo) == BRUTO_DESPACHO
              and not [c for c in cobranzas_de(db, fdespB) if c.medio == "adelanto"],
              (float(fdB.saldo), [(c.medio, float(c.monto)) for c in cobranzas_de(db, fdespB)]))
        adelB = _adelanto_de(db, cotB.id)
        cobsB = _cobs_adelanto_de_venta(db, cotB.id)
        check("11B INVARIANTE monto_aplicado == Σ cobranzas 'adelanto' tras mover la plata",
              adelB is not None
              and abs(float(adelB.monto_aplicado) - sum(float(c.monto) for c in cobsB)) < 0.01
              and float(adelB.monto_aplicado) == ADELANTO,
              (adelB and float(adelB.monto_aplicado), [float(c.monto) for c in cobsB]))

        # ── 11C · EL SONDEO: cuando el folio se confirma DESPUÉS del emitir ─────
        # Es el caso NORMAL en producción (el SII tarda de segundos a minutos). El aviso
        # nace en el request que GRABA el folio: aquí ése es el sondeo, no el emitir.
        fake.status_respuesta = STATUS_PROCESANDO
        cotC, fdespC, folio_despC = _venta_con_adelanto_en_otra_factura(
            db, "G-W3", con_factoring=True)
        folio_antC = _folio_test()
        fake.folio_emitido = folio_antC
        rC = client.post(f"{BASE}/emitir", json={
            "cotizacion_id": cotC.id, "es_anticipo": True,
            "monto_neto_anticipo": NETO_ANTICIPO})
        bodyC = rC.json() if rC.status_code == 200 else {}
        print(f"     · POST /emitir (Wasabil 'procesando') → {rC.status_code} "
              f"· folio={bodyC.get('folio')} · advertencias={bodyC.get('advertencias')}")
        check("11C con Wasabil en 'procesando', el emitir responde 200 SIN folio y con "
              "`advertencias` vacío (todavía no hay adelanto que aplicar)",
              rC.status_code == 200 and not bodyC.get("folio")
              and bodyC.get("advertencias") == [], rC.text[:300])
        fantC_id = bodyC.get("factura_id")
        r1 = client.get(f"{BASE}/{fantC_id}/estado")
        body1 = r1.json()
        avisos1 = body1.get("advertencias") or []
        print(f"     · GET  /estado (1ª pasada) → folio={body1.get('folio')} "
              f"· advertencias={avisos1}")
        check("11C el SONDEO —el request que graba el folio— es el que trae el aviso",
              r1.status_code == 200 and body1.get("folio") == folio_antC
              and bool(avisos1), body1)
        check("11C ...con el mismo motivo y el mismo remedio que por el otro camino",
              any("cedida a un factor" in a and "Tesorería" in a for a in avisos1)
              and any(folio_despC in a for a in avisos1), avisos1)
        r2 = client.get(f"{BASE}/{fantC_id}/estado")
        body2 = r2.json()
        print(f"     · GET  /estado (2ª pasada) → folio={body2.get('folio')} "
              f"· advertencias={body2.get('advertencias')}")
        check("11C el aviso nace UNA sola vez: el 2º sondeo ya no lo repite (la función "
              "es idempotente — si no, el modal lo pintaría en bucle cada 3 segundos)",
              body2.get("advertencias") == [], body2)
        db.rollback()
        check("11C ...y el folio sigue grabado en el DTE y en la factura local (la "
              "idempotencia no deshace nada)",
              body2.get("folio") == folio_antC
              and (db.get(MonzaContFacturaCliente, fantC_id).numero_factura or "")
              == folio_antC,
              (body2.get("folio"), db.get(MonzaContFacturaCliente, fantC_id).numero_factura))
        fantC = _anticipo_de(db, cotC.id)
        check("11C la factura de anticipo del sondeo también quedó POR COBRAR "
              "(el aviso describe un hecho, no una hipótesis)",
              fantC is not None and float(fantC.saldo) == ADELANTO
              and not cobranzas_de(db, fantC.id),
              (fantC and float(fantC.saldo), fantC and cobranzas_de(db, fantC.id)))
        fake.status_respuesta = STATUS_PROCESANDO   # estado por defecto del arnés

        # ── cierre: nada de lo emitido en esta suite salió del fake ─────────────
        check("TODO lo 'emitido' quedó en el fake (ningún documento real)",
              all(d.get("sii_document_type_code") == 33 for d in fake.creados)
              and getattr(monza_client.crear_documento, "__self__", None) is fake,
              len(fake.creados))
    finally:
        limpiar(db, MARK)
        db.close()
        verificar_limpieza(MARK)

    check.finish()


def test_monza_factura_anticipo_sii_puro():
    run_puro()


def test_monza_factura_anticipo_sii_e2e():
    run_e2e()


if __name__ == "__main__":
    run_puro()
    run_e2e()
