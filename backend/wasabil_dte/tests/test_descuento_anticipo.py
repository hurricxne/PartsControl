"""Descuento de anticipo en el DTE 33 — el neto que sale al SII tiene que ser el MISMO
neto que quedó registrado en el libro de ventas.

Grupo AM NO tenía ni un test de `aplicar_descuento_lineas`, y el reparto se hacía en
CENTAVOS mientras Contabilidad calcula el neto en PESOS half-up (_total_linea). Los dos
descuadres, medidos con aritmética:

  · 3 líneas de $8.889,50 con un anticipo de $17.779 → el libro da $8.891 y el DTE salía
    por $8.890: un peso por debajo del documento tributario registrado.
  · 3 líneas de $8.889,50 con un anticipo de $26.668,50 → el libro da $1,50 (emitible) y
    el propio piso del módulo BLOQUEABA una factura legítima.

La suite fija además: el folio de la referencia 33 debe ser un correlativo NUMÉRICO del
SII (lo teclea el operador), una línea negativa SIN `anticipo_factura_id` es un problema
bloqueante (no un descuento fantasma sin referencia que lo respalde), `total_neto_lineas`
descuenta el `discount`, y el cinturón de `armar_guia` (fecha de OC y folio de OC dentro
del límite del SII).

Funciones PURAS: sin BD, sin red, sin fakes — nada de esto puede tocar el SII.

Corre con:  ./venv/bin/python -m pytest wasabil_dte/tests/test_descuento_anticipo.py -q
(también:   ./venv/bin/python wasabil_dte/tests/test_descuento_anticipo.py)
"""
import os
import sys
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import routers.contabilidad as cont  # noqa: E402
from wasabil_dte.service import (  # noqa: E402
    FOLIO_REF_MAX, NETO_MINIMO_DTE, TIPO_REF_ANTICIPO, aplicar_descuento_lineas,
    armar_guia, armar_lineas_factura, armar_referencias_factura, neto_linea_dte,
    payload_a_rest, total_neto_lineas,
)

_fails: list = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {str(extra)[:300]}"))
    if not cond:
        _fails.append(name)


# ─── dobles de las líneas persistidas (ContFacturaClienteItem) ───────────────────
def _item(**kw):
    """`armar_lineas_factura` solo LEE atributos (numero_parte, descripcion, cantidad,
    precio_unit_neto, total_neto, despacho_item_id, anticipo_factura_id): un
    SimpleNamespace basta y mantiene la suite sin BD."""
    base = dict(id=1, numero_parte="P1", descripcion="Pieza 1", cantidad=1,
                precio_unit_neto=80000, total_neto=80000, despacho_item_id=None,
                anticipo_factura_id=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _descuento(monto, anticipo_id=555, **kw):
    """La línea LOCAL de descuento tal como la persiste _persistir_factura: numero_parte
    'DESCUENTO', cantidad 1, precio y total NEGATIVOS y FK a la factura de anticipo."""
    base = dict(id=99, numero_parte="DESCUENTO",
                descripcion=f"Descuento anticipo Factura N° {anticipo_id}",
                cantidad=1, precio_unit_neto=-monto, total_neto=-monto,
                despacho_item_id=None, anticipo_factura_id=anticipo_id)
    base.update(kw)
    return SimpleNamespace(**base)


def _neto_dte(lineas):
    """Neto que el SII liquidará: Σ por línea de (precio × cantidad) × (1 − discount%),
    half-up a PESO EN CADA LÍNEA. El redondeo por línea NO es un detalle: en un DTE en
    CLP cada MontoItem es un ENTERO, y ése es el dominio en que Contabilidad calculó el
    neto local. Se calcula acá a mano (no con total_neto_lineas) para que la suite mida
    el documento y no se limite a repetir la implementación."""
    return float(sum(
        Decimal(str(x["price"] * x["quantity"] * (1 - x.get("discount", 0) / 100.0)))
        .quantize(Decimal("1"), rounding=ROUND_HALF_UP) for x in lineas))


def _neto_local(filas, descuento=0):
    """Neto que registra Contabilidad: Σ _total_linea (half-up a peso) − descuento.
    `filas` = [(cantidad, precio)]. Se usa la función REAL del módulo contable, no una
    copia: si aquélla cambiara de criterio, esta suite lo detectaría."""
    return sum(cont._total_linea(p, q) for q, p in filas) - descuento


def _positivas(lineas):
    return all(x["price"] > 0 and x["quantity"] > 0 for x in lineas)


def _lineas_de(filas, descuento):
    items = [_item(id=i + 1, numero_parte=f"P{i + 1}", cantidad=q, precio_unit_neto=p,
                   total_neto=cont._total_linea(p, q)) for i, (q, p) in enumerate(filas)]
    items.append(_descuento(descuento))
    return armar_lineas_factura(items)


def run():
    # ═══ A · EL CASO DEL HALLAZGO, con su aritmética exacta ═════════════════════
    # 3 líneas de 8.889,50 con anticipo 17.779: libro 8.891 · DTE viejo 8.890.
    filas_h = [(1, 8889.5), (1, 8889.5), (1, 8889.5)]
    lns_h, pr_h = _lineas_de(filas_h, 17779)
    check("hallazgo: el libro de ventas da 8.891 (3 × 8.890 − 17.779)",
          _neto_local(filas_h, 17779) == 8891, _neto_local(filas_h, 17779))
    check("hallazgo: el neto del DTE da 8.891 — cuadra con el libro (medía en centavos "
          "y salía 8.890, un peso por debajo del documento registrado)",
          _neto_dte(lns_h) == 8891.0 and not pr_h, (_neto_dte(lns_h), pr_h))
    check("hallazgo: ninguna línea viaja con price<=0 ni quantity<=0 (el API los rechaza)",
          _positivas(lns_h), lns_h)
    check("hallazgo: todos los `discount` quedan en (0, 100] — el API rechaza fuera de ahí",
          all(0 < x["discount"] <= 100 for x in lns_h if "discount" in x),
          [x.get("discount") for x in lns_h])
    # No-vacuidad: la suma en CENTAVOS —el criterio viejo— sigue sin dar el neto local.
    viejo = round(sum(round(x["price"] * x["quantity"], 2) *
                      (1 - x.get("discount", 0) / 100.0) for x in lns_h), 2)
    check("hallazgo (no-vacuidad): la suma en centavos NO da 8.891, así que el check de "
          "arriba no pasa 'por construcción'", viejo != 8891, viejo)

    # El mismo caso con anticipo 26.668,50: el libro da 1,50 y es emitible; el piso viejo
    # (medido en centavos) lo bloqueaba.
    filas_b = [(1, 8889.5), (1, 8889.5), (1, 8889.5)]
    lns_b, pr_b = _lineas_de(filas_b, 26668.5)
    check("borde: con anticipo 26.668,50 el libro da 1,50 y la factura NO se bloquea "
          "(el piso viejo mataba una factura legítima)",
          not pr_b and _neto_local(filas_b, 26668.5) == 1.5, (pr_b, _neto_local(filas_b, 26668.5)))

    # ═══ B · CUADRATURA GENERAL: neto DTE == neto local en varios repartos ══════
    repartos = [
        ("80.000 + 20.000 − 30.000", [(1, 80000), (1, 20000)], 30000),
        ("4×15.000 + 20×2.500 − 50.000 (la venta patrón)", [(4, 15000), (20, 2500)], 50000),
        ("80.000 + 20.000 − 90.000 (se reparte en DOS líneas)", [(1, 80000), (1, 20000)], 90000),
        ("3×33.333 − 33.333", [(3, 33333)], 33333),
        ("200.000 − 33.333", [(1, 200000)], 33333),
        ("7×1.111 + 3×2.222 − 5.000", [(7, 1111), (3, 2222)], 5000),
        ("5×12.345 − 40.000", [(1, 12345)] * 5, 40000),
        ("100.000 + 1 − 100.000 (la chica sobrevive con $1)", [(1, 100000), (1, 1)], 100000),
        ("33.333,5 + 22.221,5 + 11.111,5 − 40.000", [(1, 33333.5), (1, 22221.5), (1, 11111.5)], 40000),
        ("dos líneas .5, descuento chico", [(1, 1000.5), (1, 2000.5)], 10),
        ("línea .5 consumida al 100%", [(1, 10000.5), (1, 5000.0)], 10001),
        ("cantidad fraccionaria 2,5 × 1.111,1", [(2.5, 1111.1), (1, 900.4)], 1000),
        ("seis líneas .5 − 50.000 (descuento casi total)",
         [(1, 12345.5), (1, 9876.5), (1, 8765.5), (1, 7654.5), (1, 6543.5), (1, 5432.5)], 50000),
        ("centavos puros − 1", [(1, 100.99), (1, 200.01)], 1),
    ]
    todos_cuadran = True
    for etiqueta, filas, dsc in repartos:
        esperado = _neto_local(filas, dsc)
        lns, pr = _lineas_de(filas, dsc)
        pcts_ok = all(0 < x["discount"] <= 100 for x in lns if "discount" in x)
        ok = _neto_dte(lns) == float(esperado) and not pr and _positivas(lns) and pcts_ok
        todos_cuadran = todos_cuadran and ok
        check(f"cuadratura {etiqueta} → neto DTE {esperado:,.0f}".replace(",", "."), ok,
              (_neto_dte(lns), esperado, pr, [x.get("discount") for x in lns]))
    check("INVARIANTE: en TODOS los repartos el neto del DTE == el neto local (mismo "
          "dominio de redondeo: half-up a PESO, como _total_linea)", todos_cuadran)

    # El % NO se redondea: Wasabil calcula con la precisión enviada (verificado en
    # borrador real: 16,6665 % sobre 200.000 dio 166.667 exacto).
    lineas_p = [{"name": "X", "code": "X", "quantity": 1, "price": 200000.0}]
    probs_p = aplicar_descuento_lineas(lineas_p, 33333.0)
    pct = lineas_p[0].get("discount")
    check("decimales feos: 200.000 − 33.333 → discount 16.6665 y neto 166.667 EXACTO",
          pct == 16.6665 and _neto_dte(lineas_p) == 166667.0 and not probs_p,
          (pct, _neto_dte(lineas_p), probs_p))
    check("el porcentaje NO viene redondeado a 2 decimales (16.6665 != 16.67)",
          pct != round(pct, 2), pct)
    rest = payload_a_rest({"details": lineas_p})
    check("`discount` sobrevive la traducción a snake_case (viaja en details tal cual)",
          any(d.get("discount") == 16.6665 for d in rest["details"]), rest["details"])

    # Reparto sobre dos líneas: la grande al 100 % y la chica el resto.
    lns, _pr = _lineas_de([(1, 80000), (1, 20000)], 90000)
    d80 = next(x for x in lns if x["price"] == 80000)
    d20 = next(x for x in lns if x["price"] == 20000)
    check("descuento mayor que la línea grande: 100% en la de 80.000 y 50% en la de 20.000",
          d80.get("discount") == 100.0 and d20.get("discount") == 50.0,
          [x.get("discount") for x in lns])

    # ═══ C · PISO DE $1 (doble: lo que liquida el SII y el monto fino) ══════════
    _l, probs_cero = _lineas_de([(1, 100000)], 99999.6)
    check(f"descuento que deja el DTE bajo ${NETO_MINIMO_DTE:.0f} → BLOQUEA",
          bool(probs_cero) and any("en $0" in p for p in probs_cero), probs_cero)
    _l, probs_099 = _lineas_de([(1, 100000)], 99999.01)
    check("neto de $0,99 tras el descuento → BLOQUEA (el DTE lo redondearía a $1, pero "
          "saldría por un monto distinto al registrado)", bool(probs_099), probs_099)
    lns_uno, probs_uno = _lineas_de([(1, 100000)], 99999)
    check("neto de $1 EXACTO tras el descuento → NO bloquea (el borde legítimo pasa)",
          not probs_uno and _neto_dte(lns_uno) == 1.0, (probs_uno, _neto_dte(lns_uno)))
    _l, probs_sup = _lineas_de([(1, 10000)], 50000)
    check("descuento MAYOR que el total de las líneas → BLOQUEA",
          bool(probs_sup) and any("supera el total" in p for p in probs_sup), probs_sup)
    _l, probs_solo = armar_lineas_factura([_descuento(5000)])
    check("factura que SOLO trae la línea de descuento → BLOQUEA (no se emite un DTE vacío)",
          bool(probs_solo), probs_solo)

    # ═══ D · CÓMO SE RECONOCE UNA LÍNEA DE DESCUENTO: SOLO LA FK ════════════════
    lns_fk, pr_fk = armar_lineas_factura([
        _item(id=1, cantidad=1, precio_unit_neto=50000, total_neto=50000),
        _descuento(10000),
    ])
    check("con FK: la línea negativa se convierte en descuento (neto 40.000, sin problemas)",
          _neto_dte(lns_fk) == 40000.0 and not pr_fk, (_neto_dte(lns_fk), pr_fk))
    lns_sinfk, pr_sinfk = armar_lineas_factura([
        _item(id=1, cantidad=1, precio_unit_neto=50000, total_neto=50000),
        _descuento(10000, anticipo_id=None),   # negativa SIN FK (línea histórica)
    ])
    check("SIN FK: la línea negativa vuelve a ser problema BLOQUEANTE (no se emite un DTE "
          "rebajado sin la referencia 33 que lo respalde)", bool(pr_sinfk), pr_sinfk)
    check("...y la línea buena NO queda con `discount` (el descuento fantasma no se aplica)",
          all("discount" not in x for x in lns_sinfk), lns_sinfk)
    check("...con mensaje PROPIO: dice 'NEGATIVO' y no manda a buscar el folio de un "
          "anticipo inexistente",
          any("NEGATIVO" in p for p in pr_sinfk) and not any("folio" in p for p in pr_sinfk),
          pr_sinfk)
    _l, pr_qty = armar_lineas_factura([
        _item(id=1, cantidad=1, precio_unit_neto=50000, total_neto=50000),
        _item(id=2, numero_parte="RARA", cantidad=-2, precio_unit_neto=0, total_neto=-1000),
    ])
    check("línea con total negativo por CANTIDAD negativa y sin FK → también BLOQUEA",
          any("NEGATIVO" in p for p in pr_qty), pr_qty)

    # ═══ E · FOLIO DE LA REFERENCIA 33: correlativo NUMÉRICO del SII ════════════
    refs, probs_ref = armar_referencias_factura(
        numero_oc="OC-4501", fecha_oc=date(2026, 6, 10),
        guia_folio="137", guia_fecha=date(2026, 7, 21),
        anticipos=[{"folio": "901", "fecha": date(2026, 7, 20)}])
    check("referencias 801 + 52 + 33 en ese orden, sin problemas",
          [r["documentType"] for r in refs] == ["801", "52", "33"] and not probs_ref,
          (refs, probs_ref))
    check("la 33 lleva folio, fecha y reason 'Descuento anticipo'",
          refs[2] == {"documentType": TIPO_REF_ANTICIPO, "folio": "901",
                      "date": "2026-07-20", "reason": "Descuento anticipo"}, refs[2])
    # El folio del anticipo lo TECLEA el operador en la vía manual: entra cualquier cosa.
    for malo in ("N/A-99", "FAC 123", "N/A", "0", "-7", "12.345", "٣"):
        _r, p = armar_referencias_factura(numero_oc="OC-1", fecha_oc=date(2026, 1, 2),
                                          anticipos=[{"folio": malo, "fecha": None}])
        check(f"folio de anticipo '{malo}' → BLOQUEA (la 33 exige folio numérico del SII)",
              any("no es un número" in x for x in p), p)
    _r, p_ok = armar_referencias_factura(numero_oc="OC-1", fecha_oc=date(2026, 1, 2),
                                         anticipos=[{"folio": "116", "fecha": None}])
    check("folio de anticipo numérico ('116') sigue pasando", not p_ok, p_ok)
    _r, p_ph = armar_referencias_factura(numero_oc="OC-1", fecha_oc=date(2026, 1, 2),
                                         anticipos=[{"folio": "#77", "fecha": None}])
    check("anticipo SIN folio del SII (placeholder '#77') → BLOQUEA con su propio mensaje",
          any("folio SII" in x for x in p_ph), p_ph)

    # ═══ F · total_neto_lineas DESCUENTA el `discount` ══════════════════════════
    lns_d = [{"name": "X", "quantity": 1, "price": 100000.0, "discount": 30.0}]
    check("total_neto_lineas descuenta el `discount` (100.000 al 30% → 70.000, no 100.000)",
          total_neto_lineas(lns_d) == 70000.0, total_neto_lineas(lns_d))
    check("neto_linea_dte redondea a PESO half-up (10,5 → 11; el round() nativo daría 10)",
          neto_linea_dte({"price": 10.5, "quantity": 1}) == 11.0,
          neto_linea_dte({"price": 10.5, "quantity": 1}))
    check("sin `discount` el resultado es el de siempre (half-up POR LÍNEA: 10,5 + 2×6,25)",
          total_neto_lineas([{"price": 10.5, "quantity": 1},
                             {"price": 6.25, "quantity": 2}]) == 11 + 13)

    # ═══ G · CINTURÓN de armar_guia (fecha de OC y folio dentro del límite SII) ══
    base = dict(numero_oc="OC-4501", fecha_oc=date(2026, 6, 10), numero_despacho="DSP-0001",
                lineas=[{"name": "Filtro", "quantity": 4, "price": 15990.4}])
    doc = armar_guia(**base)
    check("armar_guia normal: referencia 801 con el folio ya strippeado y sin emitir",
          doc["references"][0]["folio"] == "OC-4501" and doc["issue"] is False, doc["references"])
    for etiqueta, kw in (("sin fecha de OC", {"fecha_oc": None}),
                         (f"N° de OC de más de {FOLIO_REF_MAX} chars",
                          {"numero_oc": "OC-DEMASIADO-LARGA-123456"})):
        try:
            armar_guia(**{**base, **kw})
            check(f"cinturón armar_guia: {etiqueta} → ValueError", False, "no lanzó")
        except ValueError:
            check(f"cinturón armar_guia: {etiqueta} → ValueError", True)

    if _fails:
        print(f"\nFALLARON {len(_fails)}: {_fails}")
    assert not _fails, f"{len(_fails)} fallos: {_fails}"
    print("\n=== DESCUENTO DE ANTICIPO EN EL DTE 33: TODO OK ===")


def test_wasabil_descuento_anticipo(): run()


if __name__ == "__main__":
    run()
