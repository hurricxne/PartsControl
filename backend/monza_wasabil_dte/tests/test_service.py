"""Tests unitarios (puros, sin DB ni red) de la lógica de monza_wasabil_dte/service.py.

Espejo de wasabil_dte/tests/test_service.py de GA con las adaptaciones Monza:
cuadratura PARAMETRIZADA por iva_rate (IVA por venta), armar_lineas con JOIN
manual por item_id sobre el precio CONGELADO precio_unitario_clp, armar_guia
con oc_fecha de columna Date (sin parse_fecha_oc) y bloqueo de folio OC >18.

Corre con:  ./venv/bin/python -m pytest monza_wasabil_dte/tests/test_service.py -q
(también:   ./venv/bin/python monza_wasabil_dte/tests/test_service.py)
"""
import os
import sys
from datetime import date, datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from monza_wasabil_dte.service import (  # noqa: E402
    NOMBRE_MAX, CONTACTO_MAX, FOLIO_REF_MAX, MAX_LINEAS, TIPO_DOC_GUIA,
    TIPO_TRASLADO_VENTA, TIPO_REF_OC, TZ_CHILE, hoy_chile, acortar_nombre,
    cuadratura, armar_lineas, armar_guia, payload_a_rest, total_neto_lineas,
    claim_vigente, serialize_dte,
)
from monza_wasabil_dte.models import CLAIM_TTL_SEGUNDOS  # noqa: E402


def _item(item_id=1, parte="A2761800009", desc="Filtro de aceite motor", precio=15000):
    """MonzaCotizacionItem simulado (solo los campos que usa armar_lineas)."""
    return SimpleNamespace(id=item_id, numero_parte=parte, descripcion=desc,
                           precio_unitario_clp=precio)


def _di(qty, item_id=1, di_id=None):
    """MonzaDespachoItem simulado: en Monza el JOIN a la cotización es MANUAL por
    item_id (no hay relación ORM), así que el ítem viaja aparte en items_por_id."""
    return SimpleNamespace(id=di_id or (100 + item_id), item_id=item_id,
                           qty_despachada=qty)


# ─── hoy_chile (fecha tributaria del DTE) ───────────────────────────────────────
def test_hoy_chile_es_fecha_de_chile():
    # La fecha del DTE es la de America/Santiago, jamás date.today() del server
    # (un VPS en UTC emitiría con fecha de mañana pasadas las ~20-21h en Chile).
    assert hoy_chile() == datetime.now(TZ_CHILE).date()


# ─── acortar_nombre (límite 25 del SII, formato v2 de nacimiento) ───────────────
def test_acortar_nombre_es_la_descripcion_sin_parte():
    # Monza NACE en v2 (hallazgo GA folio 136): el nombre es la DESCRIPCIÓN limpia,
    # el N° de parte viaja en `code` y NO se antepone.
    nombre = acortar_nombre("A2761800009", "BOMBA DE AGUA (MOTOR M276)")
    assert len(nombre) <= NOMBRE_MAX
    assert nombre.startswith("BOMBA")
    assert "A2761800009" not in nombre


def test_acortar_nombre_casos_borde():
    assert acortar_nombre("1R-0716", "Filtro") == "Filtro"
    assert acortar_nombre("1R-0716", None) == "1R-0716"     # sin descripción: la parte
    assert acortar_nombre(None, "Filtro de aceite") == "Filtro de aceite"
    assert acortar_nombre(None, None) == "ITEM"
    assert len(acortar_nombre("X" * 40, None)) == NOMBRE_MAX
    assert len(acortar_nombre(None, "D" * 40)) == NOMBRE_MAX
    # rstrip real: el espacio cae dentro del corte a 25 y debe salir
    assert acortar_nombre(None, ("A" * 24) + " EXTRA") == "A" * 24


# ─── cuadratura PARAMETRIZADA (IVA por venta — adaptación Monza) ────────────────
def test_cuadratura_con_tasa_19():
    assert cuadratura(100000, 0.19) == (100000, 19000, 119000)


def test_cuadratura_con_tasa_distinta():
    # La tasa es PARÁMETRO (iva_pct congelado por venta): con 10% la guía cuadra
    # con ESA tasa — una constante fija 0.19 descuadraría guía↔factura.
    assert cuadratura(100000, 0.10) == (100000, 10000, 110000)


def test_cuadratura_half_up_no_bankers():
    # 150 × 0.19 = 28.5 → half-up 29 (el round() nativo daría 28: banker's)
    assert cuadratura(150, 0.19) == (150, 29, 179)
    # mismo criterio con tasa por venta: 105 × 0.10 = 10.5 → 11 (round() daría 10)
    assert cuadratura(105, 0.10) == (105, 11, 116)


def test_cuadratura_cero():
    assert cuadratura(0, 0.19) == (0, 0, 0)


# ─── armar_lineas (ítems del despacho × precio CONGELADO de la venta) ──────────
def test_armar_lineas_feliz():
    items = {1: _item(1, precio=15990.4),
             2: _item(2, parte="A0009884399", desc="Sello de polvo", precio=2500)}
    lineas, problemas = armar_lineas([_di(4, item_id=1), _di(20, item_id=2)], items)
    assert problemas == []
    assert len(lineas) == 2
    assert lineas[0]["quantity"] == 4 and lineas[0]["price"] == 15990.4
    assert lineas[0]["code"] == "A2761800009"
    assert lineas[1]["name"] == "Sello de polvo"      # nombre = descripción (v2)
    assert lineas[1]["code"] == "A0009884399"         # la parte viaja en code


def test_armar_lineas_external_id_es_el_despacho_item():
    # externalId = str(MonzaDespachoItem.id): identidad 1:1 de la línea para que la
    # factura (Fase B) tome el precio congelado de ESTA guía sin depender del n° de parte.
    lineas, problemas = armar_lineas([_di(4, item_id=1, di_id=777)], {1: _item(1)})
    assert problemas == []
    assert lineas[0]["externalId"] == "777"


def test_armar_lineas_sin_precio_bloquea():
    lineas, problemas = armar_lineas([_di(4, item_id=1)], {1: _item(1, precio=0)})
    assert lineas == []
    assert any("sin precio" in p for p in problemas)


def test_armar_lineas_precio_subcentavo_bloquea():
    # El guard evalúa el precio YA redondeado (lo que viaja en la línea): un
    # sub-centavo (0.004 → round=0.0) debe bloquear, no emitir una línea en $0.
    lineas, problemas = armar_lineas([_di(4, item_id=1)], {1: _item(1, precio=0.004)})
    assert not lineas and any("precio" in p for p in problemas)
    # y un precio normal sigue pasando con el valor redondeado a 2 decimales
    lineas, problemas = armar_lineas([_di(4, item_id=1)], {1: _item(1, precio=15990.404)})
    assert not problemas and lineas[0]["price"] == 15990.4


def test_armar_lineas_qty_cero_se_omite():
    items = {1: _item(1), 2: _item(2)}
    lineas, problemas = armar_lineas([_di(0, item_id=1), _di(3, item_id=2)], items)
    assert problemas == []
    assert len(lineas) == 1 and lineas[0]["quantity"] == 3


def test_armar_lineas_item_borrado_bloquea():
    # El JOIN manual no encontró el ítem en la cotización (fue borrado)
    _lineas, problemas = armar_lineas([_di(4, item_id=99)], {})
    assert any("ya no existe" in p for p in problemas)


def test_armar_lineas_despacho_vacio_bloquea():
    _lineas, problemas = armar_lineas([], {})
    assert any("no tiene cantidades" in p for p in problemas)


def test_armar_lineas_tope_60():
    items = {i: _item(i, parte=f"P-{i}", desc=f"Repuesto {i}") for i in range(1, 62)}
    lineas, problemas = armar_lineas([_di(1, item_id=i) for i in range(1, 62)], items)
    assert len(lineas) == 61
    assert any(str(MAX_LINEAS) in p for p in problemas)


def test_total_neto_redondea_por_linea_half_up():
    # Half-up POR LÍNEA (== _total_linea de monza_contabilidad y == el Math.round
    # del frontend): 10.5 → 11 y 12.5 → 13 (round() banker's daría 10 y 12)
    lineas = [{"price": 10.5, "quantity": 1}, {"price": 6.25, "quantity": 2}]
    assert total_neto_lineas(lineas) == 11 + 13


# ─── armar_guia (documento 52 completo) ────────────────────────────────────────
def _guia(**kw):
    base = dict(
        numero_oc="OC-4501", fecha_oc=date(2026, 6, 10), numero_despacho="DSP-2026-0001",
        lineas=[{"name": "Filtro de aceite", "description": "Filtro de aceite motor",
                 "code": "A2761800009", "externalId": "101", "quantity": 4,
                 "price": 15990.4}],
    )
    base.update(kw)
    return armar_guia(**base)


def test_armar_guia_estructura_sii():
    doc = _guia()
    assert doc["siiDocumentTypeCode"] == TIPO_DOC_GUIA
    assert doc["dispatchGuide"] == {"dispatchTypeCode": TIPO_TRASLADO_VENTA}
    assert doc["issue"] is False  # el preview JAMÁS emite: issue explícito al confirmar


def test_armar_guia_tipo_traslado():
    assert _guia()["dispatchGuide"]["dispatchTypeCode"] == 1
    assert _guia(tipo_traslado=5)["dispatchGuide"]["dispatchTypeCode"] == 5
    assert payload_a_rest(_guia(tipo_traslado=5))["dispatch_guide"] == {"dispatch_type_code": 5}
    for malo in (0, 10, 99):
        try:
            _guia(tipo_traslado=malo)
            assert False, f"tipo_traslado {malo} debió fallar"
        except ValueError:
            pass


def test_armar_guia_referencia_801_con_fecha_de_columna_date():
    # oc_fecha viene DIRECTA de MonzaCotizacion.oc_fecha (columna Date, F3): sin
    # parse_fecha_oc — la fecha se serializa ISO tal cual.
    ref = _guia()["references"][0]
    assert ref["documentType"] == TIPO_REF_OC
    assert ref["folio"] == "OC-4501"
    assert ref["date"] == "2026-06-10"
    # v3 (hallazgo GA folio 137): SIN `reason` — Wasabil imprime la etiqueta del
    # tipo ("ORDEN DE COMPRA") junto al folio, y un reason la duplicaba en el papel.
    assert "reason" not in ref, ref


def test_armar_guia_fecha_oc_null_bloquea():
    # Cinturón del service (el router ya bloquea antes con problema legible):
    # una referencia 801 sin fecha es inválida ante el SII.
    try:
        _guia(fecha_oc=None)
        assert False, "fecha_oc None debió fallar"
    except ValueError as e:
        assert "fecha" in str(e).lower()


def test_armar_guia_folio_oc_largo_bloquea():
    # El SII limita el folio de referencia a 18 chars; el service revienta como
    # cinturón (el preview del router lo bloquea antes con mensaje al operador).
    try:
        _guia(numero_oc="X" * (FOLIO_REF_MAX + 1))
        assert False, "folio OC > 18 debió fallar"
    except ValueError as e:
        assert str(FOLIO_REF_MAX) in str(e)
    # y en el límite exacto pasa
    assert _guia(numero_oc="X" * FOLIO_REF_MAX)["references"][0]["folio"] == "X" * FOLIO_REF_MAX


def test_armar_guia_referencia_interna_solo_numero_despacho():
    # v2 de nacimiento: invoiceReference = SOLO el N° de despacho interno (ancla
    # única y reencontrable del anti doble emisión). La OC NO va aquí (Wasabil
    # imprime este campo: con la OC adentro salía referenciada dos veces).
    doc = _guia()
    assert doc["invoiceReference"] == "DSP-2026-0001"
    assert "OC-4501" not in doc["invoiceReference"]


def test_armar_guia_receptor_y_contacto():
    doc = _guia(client_id=160065, contacto="  Juan Pérez +56 9 1234 5678  ")
    assert doc["clientId"] == 160065
    assert doc["receiverContact"] == "Juan Pérez +56 9 1234 5678"
    assert len(_guia(contacto="X" * 200)["receiverContact"]) <= CONTACTO_MAX
    assert "clientId" not in _guia()  # sin ficha Wasabil no se manda clientId


def test_armar_guia_email_opcional():
    doc = _guia(receiver_email="cliente@empresa.cl")
    assert doc["receiverEmail"] == "cliente@empresa.cl" and doc["sendEmail"] is True
    assert "receiverEmail" not in _guia()


# ─── payload_a_rest (traducción al REST snake_case) ─────────────────────────────
def test_payload_a_rest_traduce_claves():
    rest = payload_a_rest(_guia(client_id=7))
    assert rest["sii_document_type_code"] == TIPO_DOC_GUIA
    assert rest["dispatch_guide"] == {"dispatch_type_code": TIPO_TRASLADO_VENTA}
    assert rest["client_id"] == 7
    assert "invoice_reference" in rest and "document_date" in rest
    assert rest["details"][0]["name"] == "Filtro de aceite"
    ref = rest["references"][0]
    assert ref["document_type"] == TIPO_REF_OC
    assert ref["folio"] == "OC-4501" and ref["date"] == "2026-06-10"
    # sin reason en la 801 → la clave NI aparece (payload_a_rest omite vacíos)
    assert "reason" not in ref
    # nada del vocabulario camelCase se filtra al REST
    assert "siiDocumentTypeCode" not in rest and "dispatchGuide" not in rest
    assert "documentType" not in ref


def test_payload_a_rest_omite_opcionales_vacios():
    # Sin contacto/email/clientId, esas claves NO viajan (Wasabil rechaza nulls raros)
    rest = payload_a_rest(_guia())
    for ausente in ("client_id", "receiver_contact", "receiver_email", "send_email"):
        assert ausente not in rest, rest.keys()


# ─── claim "en vuelo" y estados de recuperación ─────────────────────────────────
def _dte(**kw):
    base = dict(id=1, tipo_dte=52, despacho_id=9, uuid=None, status_id=None,
                en_vuelo_desde=None, folio=None, pdf_url=None, xml_url=None,
                error=None, monto_neto=0, iva=0, monto_total=0,
                created_at=None, updated_at=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_claim_vigente_fresco_y_vencido():
    # El claim usa UTC naive (utcnow) en AMBOS extremos: se envejece a mano con un
    # datetime deliberado (inmune a cambios de hora local chilena).
    ahora = datetime.utcnow()
    assert claim_vigente(_dte(en_vuelo_desde=ahora), ahora) is True
    vencido = ahora - timedelta(seconds=CLAIM_TTL_SEGUNDOS + 1)
    assert claim_vigente(_dte(en_vuelo_desde=vencido), ahora) is False
    assert claim_vigente(_dte(en_vuelo_desde=None), ahora) is False
    assert claim_vigente(None, ahora) is False


def test_serialize_estados_de_recuperacion():
    # Fallido del SII → reintentable
    s = serialize_dte(_dte(uuid="u1", status_id=4, error="rechazado"))
    assert s["estado"] == "fallido" and s["puede_reintentar"] is True
    # Error de envío (nunca llegó a Wasabil, claim liberado) → reintentable
    s = serialize_dte(_dte(error="conexión rechazada"))
    assert s["estado"] == "error_envio" and s["puede_reintentar"] is True
    # Claim en vuelo (respuesta perdida hace segundos) → NO reintentable todavía
    s = serialize_dte(_dte(error="timeout", en_vuelo_desde=datetime.utcnow()))
    assert s["estado"] == "enviando" and s["puede_reintentar"] is False
    # Emitido → jamás reintentable
    s = serialize_dte(_dte(uuid="u1", status_id=3, folio="777"))
    assert s["estado"] == "emitido" and s["puede_reintentar"] is False
    # En proceso en el SII → esperar, no reintentar
    s = serialize_dte(_dte(uuid="u1", status_id=2))
    assert s["estado"] == "procesando" and s["puede_reintentar"] is False


def test_serialize_server_caido_reintentable_sin_error():
    # Caso "el server se cayó (o hubo deploy) entre el claim y la respuesta": sin
    # uuid, SIN error registrado y claim vencido → puede_reintentar True (exigir
    # bool(error) dejaba la UI clavada en 'SII en proceso' sin botón — fix
    # heredado de GA, Monza nace con él).
    vencido = datetime.utcnow() - timedelta(seconds=CLAIM_TTL_SEGUNDOS + 60)
    s = serialize_dte(_dte(uuid=None, error=None, en_vuelo_desde=vencido))
    assert s["estado"] == "no_enviado"
    assert s["puede_reintentar"] is True


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
