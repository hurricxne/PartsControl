"""Fase 6 — funciones PURAS del armado de la factura electrónica (DTE 33) de MonzaParts.

Sin BD, sin red, sin fakes: `monza_wasabil_dte.service` no importa client ni router,
y estas pruebas ejercen exactamente eso. Cubren lo que el router da por hecho:

  · `armar_referencias_factura`  → 801 (OC) siempre + 52 (guía) cuando la hay, SIN
    `reason` (formato v3), con todos sus bloqueos.
  · `armar_lineas_factura`       → líneas desde MonzaContFacturaClienteItem congelados,
    externalId que cruza 1:1 con la guía 52, piso de $1, topes.
  · `armar_factura`              → tipo 33, issue=False POR DEFECTO, paymentMethod
    obligatorio y saneado, invoiceReference = FACT-<id>.
  · `payload_a_rest`             → traducción camelCase → snake_case del REST real.

Corre con:  ./venv/bin/python -m pytest monza_wasabil_dte/tests/test_factura_service.py -q
(también:   ./venv/bin/python monza_wasabil_dte/tests/test_factura_service.py)
"""
import os
import sys
from datetime import date
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from monza_wasabil_dte.service import (  # noqa: E402
    FOLIO_REF_MAX, MAX_LINEAS, MAX_REFERENCIAS, NETO_MINIMO_DTE, NOMBRE_MAX,
    TIPO_DOC_FACTURA, armar_factura, armar_lineas_factura, armar_referencias_factura,
    hoy_chile, payload_a_rest,
)

_fails: list = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _item(**kw):
    """Doble de MonzaContFacturaClienteItem: `armar_lineas_factura` solo lee atributos
    (id, numero_parte, descripcion, cantidad, precio_unit_neto, despacho_item_id), así
    que un SimpleNamespace basta y la prueba queda sin BD."""
    base = dict(id=1, numero_parte="A2761800009", descripcion="Filtro de aceite motor",
                cantidad=4, precio_unit_neto=15000, despacho_item_id=77)
    base.update(kw)
    return SimpleNamespace(**base)


def run():
    # ── REFERENCIAS: 801 sola (retiro en oficina) ──────────────────────────────
    refs, probs = armar_referencias_factura(numero_oc="OC-4501", fecha_oc=date(2026, 6, 10))
    check("801 sola: 1 referencia, sin problemas", refs == [
        {"documentType": "801", "folio": "OC-4501", "date": "2026-06-10"}] and not probs,
        (refs, probs))
    check("801 SIN reason (formato v3: el tipo ya imprime 'ORDEN DE COMPRA')",
          "reason" not in refs[0], refs[0])

    # ── REFERENCIAS: 801 + 52 con el folio de la guía ──────────────────────────
    refs, probs = armar_referencias_factura(
        numero_oc="OC-4501", fecha_oc=date(2026, 6, 10),
        guia_folio="777", guia_fecha=date(2026, 6, 20))
    check("801 + 52 en ese orden", [r["documentType"] for r in refs] == ["801", "52"], refs)
    check("52 lleva folio y fecha de la guía",
          refs[1] == {"documentType": "52", "folio": "777", "date": "2026-06-20"}, refs[1])
    check("52 SIN reason (el tipo ya imprime 'GUIA DE DESPACHO')",
          "reason" not in refs[1] and not probs, (refs[1], probs))

    # La fecha de la guía es OPCIONAL: sin ella la referencia sale igual (con folio).
    refs, probs = armar_referencias_factura(numero_oc="OC-1", fecha_oc=date(2026, 1, 2),
                                            guia_folio="777")
    check("52 sin fecha: referencia válida y sin clave 'date'",
          refs[1] == {"documentType": "52", "folio": "777"} and not probs, refs)

    # Guía vacía / en blanco → NO se agrega referencia 52 (retiro en oficina, o guía
    # todavía sin folio: el router ya bloqueó ese caso aparte).
    refs, _ = armar_referencias_factura(numero_oc="OC-1", fecha_oc=date(2026, 1, 2),
                                        guia_folio="   ")
    check("guía en blanco no agrega 52", len(refs) == 1, refs)

    # ── REFERENCIAS: bloqueos ──────────────────────────────────────────────────
    _refs, probs = armar_referencias_factura(numero_oc="", fecha_oc=date(2026, 6, 10))
    check("sin N° de OC → bloquea", probs and any("N° de OC" in p for p in probs), probs)
    _refs, probs = armar_referencias_factura(numero_oc="OC-4501", fecha_oc=None)
    check("sin FECHA de OC → bloquea", probs and any("FECHA" in p for p in probs), probs)
    _refs, probs = armar_referencias_factura(numero_oc="", fecha_oc=None)
    check("sin N° ni fecha de OC → DOS problemas (la lista es completa, no el primero)",
          len(probs) == 2, probs)
    oc_larga = "OC-" + "9" * FOLIO_REF_MAX
    _refs, probs = armar_referencias_factura(numero_oc=oc_larga, fecha_oc=date(2026, 6, 10))
    check(f"N° de OC > {FOLIO_REF_MAX} chars → bloquea con el límite en el mensaje",
          probs and any(str(FOLIO_REF_MAX) in p for p in probs), probs)
    refs_g, probs = armar_referencias_factura(
        numero_oc="OC-1", fecha_oc=date(2026, 1, 2), guia_folio="G" * (FOLIO_REF_MAX + 1))
    check("folio de guía manual demasiado largo → bloquea", bool(probs), probs)
    check("folio de guía inválido: la 52 NO se agrega mutilada (la 801 sí sale)",
          [r["documentType"] for r in refs_g] == ["801"], refs_g)
    # Con la OC inválida NI SIQUIERA sale la 801: una referencia legal a medias en un
    # documento irreversible es peor que ninguna (el problema ya bloquea la emisión).
    refs_oc, _probs = armar_referencias_factura(numero_oc="", fecha_oc=None, guia_folio="777")
    check("con la OC inválida la 801 no se emite a medias",
          [r["documentType"] for r in refs_oc] == ["52"], refs_oc)

    # ── LÍNEAS ────────────────────────────────────────────────────────────────
    lineas, probs = armar_lineas_factura([
        _item(id=1, cantidad=4, precio_unit_neto=15000, despacho_item_id=77),
        _item(id=2, numero_parte="A0009884399", descripcion="Sello de polvo",
              cantidad=20, precio_unit_neto=2500, despacho_item_id=78),
    ])
    check("2 líneas sin problemas", len(lineas) == 2 and not probs, (lineas, probs))
    check("name = descripción limpia (la parte va en code, no concatenada)",
          lineas[0]["name"] == "Filtro de aceite motor"
          and lineas[0]["code"] == "A2761800009", lineas[0])
    check("externalId = despacho_item_id (cruce 1:1 con la guía 52)",
          lineas[0]["externalId"] == "77" and lineas[1]["externalId"] == "78",
          [x["externalId"] for x in lineas])
    check("quantity/price viajan tal cual (líneas congeladas)",
          lineas[0]["quantity"] == 4 and lineas[0]["price"] == 15000, lineas[0])
    # Este check decía "Monza no tiene facturas de anticipo: eso es la Fase 7". Cambió
    # porque la Fase 7 LLEGÓ: hoy sí existen líneas locales negativas (el descuento de
    # una factura de anticipo) y `armar_lineas_factura` las excluye del DTE para
    # aplicarlas como `discount` porcentual. Lo que este caso afirma —y siempre quiso
    # afirmar— es el escenario SIN anticipos: una factura normal no inventa líneas
    # negativas ni descuentos. El camino CON anticipo vive en test_factura_anticipo_sii.py.
    check("factura SIN anticipos: ninguna línea negativa y ningún `discount`",
          all(x["price"] > 0 and x["quantity"] > 0 and "discount" not in x
              for x in lineas), lineas)

    # Retiro en oficina: la línea no viene de un despacho → externalId 'fi-<id>'
    lineas, _probs = armar_lineas_factura([_item(id=42, despacho_item_id=None)])
    check("sin despacho_item_id → externalId 'fi-<id de la línea>'",
          lineas[0]["externalId"] == "fi-42", lineas[0])

    # Nombre recortado al límite del SII
    lineas, _probs = armar_lineas_factura([_item(descripcion="X" * 200)])
    check(f"name recortado a {NOMBRE_MAX} chars",
          len(lineas[0]["name"]) == NOMBRE_MAX, lineas[0]["name"])

    # Cantidad ~0 → la línea no viaja (no es un error: simplemente no hay qué facturar)
    lineas, probs = armar_lineas_factura([_item(cantidad=0.0005),
                                          _item(id=2, cantidad=4, precio_unit_neto=15000)])
    check("línea con cantidad ~0 se salta sin romper", len(lineas) == 1 and not probs,
          (lineas, probs))

    # Precio 0 → BLOQUEA (el SII rechaza price<=0 y emitir es irreversible)
    _lineas, probs = armar_lineas_factura([_item(precio_unit_neto=0)])
    check("precio $0 → bloquea", probs and any("$0" in p for p in probs), probs)

    # Sin líneas emitibles → bloquea
    _lineas, probs = armar_lineas_factura([])
    check("factura sin líneas → bloquea", probs and any("sin líneas" in p or "no tiene líneas" in p
                                                        for p in probs), probs)

    # Más de MAX_LINEAS → bloquea
    _lineas, probs = armar_lineas_factura(
        [_item(id=i, despacho_item_id=i) for i in range(1, MAX_LINEAS + 2)])
    check(f"> {MAX_LINEAS} líneas → bloquea", probs and any(str(MAX_LINEAS) in p for p in probs),
          probs)

    # Piso de $1: bajo un peso el neto llega al SII como $0 y lo rechaza.
    _lineas, probs = armar_lineas_factura([_item(cantidad=1, precio_unit_neto=0.4)])
    check("neto bajo $1 → bloquea (piso NETO_MINIMO_DTE, primera de las dos capas)",
          probs and any("bajo $1" in p for p in probs), (probs, NETO_MINIMO_DTE))
    lineas, probs = armar_lineas_factura([_item(cantidad=1, precio_unit_neto=1)])
    check("neto de $1 exacto → NO bloquea (el borde legítimo pasa)",
          lineas and not probs, (lineas, probs))

    # ── DOCUMENTO ─────────────────────────────────────────────────────────────
    lineas, _ = armar_lineas_factura([_item()])
    refs, _ = armar_referencias_factura(numero_oc="OC-4501", fecha_oc=date(2026, 6, 10),
                                        guia_folio="777", guia_fecha=date(2026, 6, 20))
    doc = armar_factura(referencia_interna="FACT-123", lineas=lineas, referencias=refs,
                        client_id=160065, fecha_emision=date(2026, 6, 25))
    check("tipo de documento 33", doc["siiDocumentTypeCode"] == TIPO_DOC_FACTURA, doc)
    check("issue=False POR DEFECTO (el preview jamás emite)", doc["issue"] is False, doc)
    check("invoiceReference = FACT-<id local> (ancla de recuperación, sin la OC adentro)",
          doc["invoiceReference"] == "FACT-123", doc)
    check("clientId presente (Wasabil autocompleta el receptor)",
          doc["clientId"] == 160065, doc)
    check("documentDate = la fecha de emisión de la factura local",
          doc["documentDate"] == "2026-06-25", doc)
    check("paymentMethod SIEMPRE presente (es obligatorio en el esquema del 33)",
          doc["paymentMethod"] == "credito", doc)
    check("referencias y líneas viajan tal cual",
          doc["references"] == refs and doc["details"] == lineas, doc)

    doc_contado = armar_factura(referencia_interna="FACT-1", lineas=lineas, referencias=refs,
                                payment_method="contado")
    check("paymentMethod 'contado' se respeta", doc_contado["paymentMethod"] == "contado")
    doc_basura = armar_factura(referencia_interna="FACT-1", lineas=lineas, referencias=refs,
                               payment_method="30 días")
    check("paymentMethod fuera del vocabulario → cae a 'credito' (saneo)",
          doc_basura["paymentMethod"] == "credito", doc_basura["paymentMethod"])
    check("sin client_id la clave no viaja (no se manda clientId nulo)",
          "clientId" not in doc_contado, doc_contado)
    doc_hoy = armar_factura(referencia_interna="FACT-1", lineas=lineas, referencias=refs)
    check("sin fecha explícita usa la fecha de CHILE (no date.today() del server UTC)",
          doc_hoy["documentDate"] == hoy_chile().isoformat(), doc_hoy["documentDate"])
    doc_issue = armar_factura(referencia_interna="FACT-1", lineas=lineas, referencias=refs,
                              issue=True)
    check("issue=True solo si se pide explícitamente", doc_issue["issue"] is True)

    check(f"tope de referencias del esquema declarado en {MAX_REFERENCIAS}",
          MAX_REFERENCIAS >= len(refs), MAX_REFERENCIAS)

    # ── TRADUCCIÓN AL REST REAL ───────────────────────────────────────────────
    rest = payload_a_rest(doc)
    check("payload_a_rest: snake_case del tipo y de la referencia interna",
          rest["sii_document_type_code"] == 33 and rest["invoice_reference"] == "FACT-123",
          rest)
    check("payload_a_rest: referencias en snake_case (document_type/folio/date)",
          rest["references"][0] == {"document_type": "801", "folio": "OC-4501",
                                    "date": "2026-06-10"}, rest["references"])
    check("payload_a_rest: client_id y payment_method presentes",
          rest.get("client_id") == 160065 and rest.get("payment_method") == "credito", rest)
    check("payload_a_rest: issue viaja tal cual (False en el preview)",
          rest["issue"] is False, rest)

    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_monza_factura_service():
    run()


if __name__ == "__main__":
    run()
