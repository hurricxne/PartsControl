"""Tests unitarios (puros, sin DB ni red) de la lógica de service.py.

Corre con:  ./venv/bin/python -m pytest wasabil_dte/tests/test_service.py -q
(también:   ./venv/bin/python wasabil_dte/tests/test_service.py)
"""
import os
import sys
from datetime import date
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from wasabil_dte.service import (  # noqa: E402
    NOMBRE_MAX, REASON_MAX, CONTACTO_MAX, TIPO_DOC_GUIA, TIPO_TRASLADO_VENTA,
    TIPO_REF_OC, MAX_LINEAS_SII_GRATUITO, acortar_nombre, sanitizar_latin1,
    advertencia_lineas_sii_gratuito, parse_fecha_oc, cuadratura, armar_lineas,
    armar_guia, payload_a_rest, total_neto_lineas,
)


def _di(qty, item_id=1, parte="1R-0716", desc="Filtro de aceite motor", existe=True):
    """DespachoItem simulado (con su item_cotizacion)."""
    it = SimpleNamespace(id=item_id, numero_parte=parte, descripcion=desc) if existe else None
    return SimpleNamespace(id=100 + item_id, item_cotizacion_id=item_id,
                           qty_despachada=qty, item_cotizacion=it)


# ─── parse_fecha_oc (texto libre de oc_cliente.fecha_oc) ────────────────────────
def test_parse_fecha_oc_formatos_reales():
    assert parse_fecha_oc("2026-06-10") == date(2026, 6, 10)
    assert parse_fecha_oc("10/06/2026") == date(2026, 6, 10)
    assert parse_fecha_oc("10-06-2026") == date(2026, 6, 10)
    assert parse_fecha_oc("2026-06-10 00:00:00") == date(2026, 6, 10)
    assert parse_fecha_oc("  2026/06/10 ") == date(2026, 6, 10)


def test_parse_fecha_oc_basura_no_revienta():
    assert parse_fecha_oc(None) is None
    assert parse_fecha_oc("") is None
    assert parse_fecha_oc("por confirmar") is None
    assert parse_fecha_oc("junio 2026") is None


# ─── acortar_nombre (formato v4: la parte PRIMERO, tope real 80 de NmbItem) ─────
# INVERSIÓN DELIBERADA de los tests v2: aquellos afirmaban que la parte NO iba en
# `name` (porque el tope autoimpuesto de 25 la cortaba a media palabra). La
# evidencia de PDFs reales del 2026-08-25 (folio 233 vs 234/235) demostró que con
# ≥6 líneas Wasabil imprime SOLO `name` — sin la parte ahí, la guía sale sin
# número de parte, y el dueño lo exige SIEMPRE visible. Ver docstring de
# acortar_nombre para la historia completa v1→v2→v4.
def test_acortar_nombre_v4_parte_primero():
    nombre = acortar_nombre("144-9799", "CONJUNTO DE BRAZO (DERECHO)")
    assert nombre == "144-9799 CONJUNTO DE BRAZO (DERECHO)"  # completo: cabe en 80
    assert nombre.startswith("144-9799")        # la parte SIEMPRE visible y primero


def test_acortar_nombre_v4_caso_folio_136_ahora_completo():
    # El caso que motivó v2 ("ROD-INF-PV351 RODILLO INF" cortado a 25): con el
    # tope real de 80 sale ENTERO — el corte a media palabra era del 25, no del formato
    assert acortar_nombre("ROD-INF-PV351", "RODILLO INFERIOR") == "ROD-INF-PV351 RODILLO INFERIOR"


def test_acortar_nombre_v4_corta_solo_la_descripcion_en_palabra():
    parte = "250-7213"
    desc = "CONJUNTO DE MANGUERA HIDRAULICA REFORZADA PARA SISTEMA DE LEVANTE DELANTERO IZQUIERDO"
    nombre = acortar_nombre(parte, desc)
    assert len(nombre) <= NOMBRE_MAX
    assert nombre.startswith(parte + " ")       # la parte ÍNTEGRA, jamás cortada
    resto = nombre[len(parte) + 1:]
    assert desc.startswith(resto)               # la descripción es un prefijo real...
    palabras = desc.split()
    assert resto.split() == palabras[:len(resto.split())]  # ...cortado en límite de palabra
    assert not nombre.endswith(" ")             # rstrip: sin espacio colgando


def test_acortar_nombre_v4_casos_borde():
    assert acortar_nombre("1R-0716", "Filtro") == "1R-0716 Filtro"
    assert acortar_nombre("1R-0716", None) == "1R-0716"          # sin descripción: la parte
    assert acortar_nombre(None, "Filtro de aceite") == "Filtro de aceite"
    assert acortar_nombre(None, None) == "ITEM"
    # descripción sola sobre 80 → corte en límite de palabra dentro del tope
    largo = acortar_nombre(None, "FILTRO " * 20)                  # 140 chars
    assert len(largo) <= NOMBRE_MAX and largo.endswith("FILTRO")
    # caso teórico: parte sola sobre 80 (numero_parte es String(100) en BD) → [:80]
    assert len(acortar_nombre("X" * 90, None)) == NOMBRE_MAX
    # parte que deja sin espacio a la descripción → la parte manda, sin media palabra
    assert acortar_nombre("P" * 80, "DESCRIPCION") == "P" * 80

    # ── El VALOR de la constante, no solo el respeto a la constante ────────────
    # POR QUÉ: todas las aserciones de largo de arriba son RELATIVAS (`<= NOMBRE_MAX`,
    # `== NOMBRE_MAX`), así que con NOMBRE_MAX = 81 la suite entera queda verde y el
    # primer nombre largo real hace que el SII rechace el DTE COMPLETO por esquema.
    # Es exactamente el error que v4 acaba de corregir: el tope 25 anterior llevaba
    # años con el comentario «límite del SII» y era falso.
    assert NOMBRE_MAX == 80   # NmbItem: Formato DTE v2.5 pág. 37 / XSD maxLength=80
    # Borde ABSOLUTO (no vía la constante): descripción de UNA SOLA PALABRA, para
    # que _cortar_en_palabra no retroceda al espacio anterior y el largo aterrice
    # EXACTO en el tope. Con `<=` y una descripción normal la sonda no discrimina:
    # el corte en límite de palabra deja el resultado corto y pasa con cualquier tope.
    assert len(acortar_nombre("250-7213", "X" * 100)) == 80


# ─── cuadratura (IVA 19% half-up, == SII/F-1) ──────────────────────────────────
def test_cuadratura_iva_19():
    assert cuadratura(100000) == (100000, 19000, 119000)


def test_cuadratura_half_up_no_bankers():
    # 150 × 0.19 = 28.5 → half-up 29 (el round() de Python daría 28: banker's)
    neto, iva, total = cuadratura(150)
    assert iva == 29
    assert total == 179


def test_cuadratura_cero():
    assert cuadratura(0) == (0, 0, 0)


# ─── armar_lineas (ítems del despacho × precios de la cotización) ───────────────
def test_armar_lineas_feliz():
    precios = {1: {"precio_venta_clp": 15990.4}, 2: {"precio_venta_clp": 2500}}
    lineas, problemas = armar_lineas(
        [_di(4, item_id=1), _di(20, item_id=2, parte="6I-2503", desc="Sello de polvo")],
        precios,
    )
    assert problemas == []
    assert len(lineas) == 2
    assert lineas[0]["quantity"] == 4 and lineas[0]["price"] == 15990.4
    assert lineas[0]["code"] == "1R-0716"
    # v4: name = «PARTE Descripción» (con ≥6 líneas el PDF imprime SOLO name)
    assert lineas[1]["name"] == "6I-2503 Sello de polvo"
    assert lineas[1]["code"] == "6I-2503"             # la parte viaja TAMBIÉN en code


def test_armar_lineas_sin_precio_bloquea():
    lineas, problemas = armar_lineas([_di(4, item_id=1)], {1: {"precio_venta_clp": 0}})
    assert lineas == []
    assert any("sin precio" in p for p in problemas)


def test_armar_lineas_qty_cero_se_omite():
    precios = {1: {"precio_venta_clp": 1000}, 2: {"precio_venta_clp": 1000}}
    lineas, problemas = armar_lineas([_di(0, item_id=1), _di(3, item_id=2)], precios)
    assert problemas == []
    assert len(lineas) == 1 and lineas[0]["quantity"] == 3


def test_armar_lineas_item_borrado_bloquea():
    _lineas, problemas = armar_lineas([_di(4, item_id=1, existe=False)], {})
    assert any("ya no existe" in p for p in problemas)


def test_armar_lineas_despacho_vacio_bloquea():
    _lineas, problemas = armar_lineas([], {})
    assert any("no tiene cantidades" in p for p in problemas)


def test_total_neto_redondea_por_linea_half_up():
    # Half-up POR LÍNEA (== factura y == lo que pinta el frontend con Math.round):
    # 10.5 → 11 y 12.5 → 13 (el round() banker's de Python daría 10 y 12)
    lineas = [{"price": 10.5, "quantity": 1}, {"price": 6.25, "quantity": 2}]
    assert total_neto_lineas(lineas) == 11 + 13


# ─── armar_guia (documento 52 completo) ────────────────────────────────────────
def _guia(**kw):
    base = dict(
        numero_oc="OC-4501", fecha_oc=date(2026, 6, 10), numero_despacho="DSP-0001",
        lineas=[{"name": "1R-0716 Filtro", "description": "Filtro de aceite",
                 "code": "1R-0716", "quantity": 4, "price": 15990.4}],
    )
    base.update(kw)
    return armar_guia(**base)


def test_armar_guia_estructura_sii():
    doc = _guia()
    assert doc["siiDocumentTypeCode"] == TIPO_DOC_GUIA
    assert doc["dispatchGuide"] == {"dispatchTypeCode": TIPO_TRASLADO_VENTA}
    assert doc["issue"] is False  # el preview JAMÁS emite: issue explícito al confirmar


def test_armar_guia_tipo_traslado():
    # Por defecto: venta (1). El operador puede elegir traslado interno (5) u otro.
    assert _guia()["dispatchGuide"]["dispatchTypeCode"] == 1
    assert _guia(tipo_traslado=5)["dispatchGuide"]["dispatchTypeCode"] == 5
    assert payload_a_rest(_guia(tipo_traslado=5))["dispatch_guide"] == {"dispatch_type_code": 5}
    # Código fuera de la tabla del SII → ValueError (no se arma un documento inválido)
    for malo in (0, 10, 99):
        try:
            _guia(tipo_traslado=malo)
            assert False, f"tipo_traslado {malo} debió fallar"
        except ValueError:
            pass


def test_armar_guia_referencia_oc_con_fecha():
    ref = _guia()["references"][0]
    assert ref["documentType"] == TIPO_REF_OC
    assert ref["folio"] == "OC-4501"
    assert ref["date"] == "2026-06-10"
    # v3 (hallazgo folio 137): SIN `reason`. Wasabil imprime la etiqueta del tipo
    # ("ORDEN DE COMPRA", derivada del 801) junto al folio, así que un reason como
    # "Orden de compra OC-4501" hacía salir la etiqueta Y el número DOS veces en el
    # papel. El campo es opcional (RazonRef del SII) y payload_a_rest lo omite.
    assert "reason" not in ref, ref


def test_armar_guia_referencia_interna_reencontrable():
    # v2 (hallazgo folio 136): invoice_reference = SOLO el N° de despacho (única por
    # despacho, reencontrable ante un reintento). La OC NO va aquí: Wasabil imprime
    # este campo y la OC salía referenciada dos veces — la 801 es la única legal.
    doc = _guia()
    assert doc["invoiceReference"] == "DSP-0001"
    assert "OC-4501" not in doc["invoiceReference"]


def test_armar_guia_receptor_y_contacto():
    doc = _guia(client_id=158381, contacto="  Juan Pérez +56 9 1234 5678  ")
    assert doc["clientId"] == 158381
    assert doc["receiverContact"] == "Juan Pérez +56 9 1234 5678"
    doc2 = _guia(contacto="X" * 200)
    assert len(doc2["receiverContact"]) <= CONTACTO_MAX
    assert "clientId" not in _guia()  # sin cliente Wasabil no se manda clientId


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
    # details conservan sus claves (iguales en ambos vocabularios); references
    # se traducen a snake_case como TODO el resto del payload REST
    assert rest["details"][0]["name"] == "1R-0716 Filtro"
    ref = rest["references"][0]
    assert ref["document_type"] == TIPO_REF_OC
    assert ref["folio"] == "OC-4501" and ref["date"] == "2026-06-10"
    # nada del vocabulario camelCase se filtra al REST
    assert "siiDocumentTypeCode" not in rest and "dispatchGuide" not in rest
    assert "documentType" not in ref


# ─── claim "en vuelo" y estados de recuperación ─────────────────────────────────
def _dte(**kw):
    base = dict(id=1, tipo_dte=52, despacho_id=9, uuid=None, status_id=None,
                en_vuelo_desde=None, folio=None, pdf_url=None, xml_url=None,
                error=None, monto_neto=0, iva=0, monto_total=0,
                created_at=None, updated_at=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_claim_vigente_fresco_y_vencido():
    from datetime import datetime, timedelta
    from wasabil_dte.service import claim_vigente
    from wasabil_dte.models import CLAIM_TTL_SEGUNDOS
    ahora = datetime.utcnow()  # el claim usa UTC naive (inmune a cambios de hora)
    assert claim_vigente(_dte(en_vuelo_desde=ahora), ahora) is True
    vencido = ahora - timedelta(seconds=CLAIM_TTL_SEGUNDOS + 1)
    assert claim_vigente(_dte(en_vuelo_desde=vencido), ahora) is False
    assert claim_vigente(_dte(en_vuelo_desde=None), ahora) is False
    assert claim_vigente(None, ahora) is False


def test_serialize_estados_de_recuperacion():
    from datetime import datetime
    from wasabil_dte.service import serialize_dte
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
    # EMITIDO manda sobre la ausencia de uuid: un DTE con status 3 y folio pero uuid
    # NULL (la respuesta trajo el estado y el folio, y el uuid se perdió) se pintaba
    # "no enviado" CON botón de Reintentar al lado, sobre algo IRREVERSIBLE.
    s = serialize_dte(_dte(status_id=3, folio="9500"))
    assert s["estado"] == "emitido" and s["puede_reintentar"] is False, s
    # ...y también sin folio (el sondeo lo rescata; el botón no se ofrece igual)
    s = serialize_dte(_dte(status_id=3))
    assert s["estado"] == "emitido" and s["puede_reintentar"] is False, s
    # factura_id lo expone el SERIALIZADOR: el modal de facturas sondea por factura, y
    # sin este campo la respuesta de "emitir" no le dice a quién consultar.
    s = serialize_dte(_dte(despacho_id=None, factura_id=77, tipo_dte=33))
    assert s["factura_id"] == 77, s
    # Un DTE de guía (sin el atributo en la fila) no revienta: getattr con default
    assert serialize_dte(_dte())["factura_id"] is None


# ─── buscar_documentos: paginación defensiva del anti-duplicados (client.py) ────
def test_buscar_documentos_paginacion():
    """El reintento sin uuid depende de esta búsqueda: si la lista queda truncada
    por paginación, `completo=False` obliga al router a abortar (no re-crear).

    Se carga una copia FRESCA de client.py: test_integration.py instala su Wasabil
    simulado pisando los atributos del módulo compartido, y este test necesita la
    implementación real de buscar_documentos (solo se simula el HTTP de _request)."""
    import importlib.util

    ruta = os.path.join(os.path.dirname(__file__), "..", "client.py")
    spec = importlib.util.spec_from_file_location("wasabil_client_fresco", ruta)
    wasabil_client = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wasabil_client)

    orig = wasabil_client._request
    try:
        # Lista plana sin metadatos → es todo lo que hay
        wasabil_client._request = lambda m, p, json_body=None, params=None: [{"uuid": "a"}]
        docs, completo = wasabil_client.buscar_documentos("DSP-1")
        assert [d["uuid"] for d in docs] == ["a"] and completo is True

        # Formato REAL {items: [...]} sin lastPage → página única
        wasabil_client._request = lambda m, p, json_body=None, params=None: {"items": [{"uuid": "b"}]}
        docs, completo = wasabil_client.buscar_documentos("DSP-1")
        assert [d["uuid"] for d in docs] == ["b"] and completo is True

        # Paginado con lastPage (formato REAL del API): recorre TODAS las páginas
        paginas = {
            1: {"items": [{"uuid": "p1"}], "total": 3, "lastPage": 3},
            2: {"items": [{"uuid": "p2"}], "total": 3, "lastPage": 3},
            3: {"items": [{"uuid": "p3"}], "total": 3, "lastPage": 3},
        }
        wasabil_client._request = lambda m, p, json_body=None, params=None: paginas[params["page"]]
        docs, completo = wasabil_client.buscar_documentos("DSP-1")
        assert [d["uuid"] for d in docs] == ["p1", "p2", "p3"] and completo is True

        # Tolerancia snake_case (last_page) por si el API varía la convención
        wasabil_client._request = lambda m, p, json_body=None, params=None: {
            "items": [{"uuid": "s1"}], "last_page": 1}
        docs, completo = wasabil_client.buscar_documentos("DSP-1")
        assert [d["uuid"] for d in docs] == ["s1"] and completo is True

        # Paginación que no se agota → corta en MAX_PAGINAS_BUSQUEDA y reporta INCOMPLETO
        wasabil_client._request = lambda m, p, json_body=None, params=None: {
            "items": [{"uuid": f"z{params['page']}"}], "lastPage": 99,
        }
        docs, completo = wasabil_client.buscar_documentos("DSP-1")
        assert len(docs) == wasabil_client.MAX_PAGINAS_BUSQUEDA and completo is False

        # Cuerpo 2xx inesperado (null) → WasabilError, jamás AttributeError crudo
        wasabil_client._request = lambda m, p, json_body=None, params=None: None
        try:
            wasabil_client.buscar_documentos("DSP-1")
            assert False, "debió lanzar WasabilError"
        except wasabil_client.WasabilError:
            pass

        # Metadatos ilegibles (lastPage no numérico) → incompleto, sin reventar
        wasabil_client._request = lambda m, p, json_body=None, params=None: {
            "items": [{"uuid": "w1"}], "lastPage": "N/A",
        }
        docs, completo = wasabil_client.buscar_documentos("DSP-1")
        assert [d["uuid"] for d in docs] == ["w1"] and completo is False
    finally:
        wasabil_client._request = orig


def _client_fresco():
    """Carga una copia FRESCA de client.py (test_integration pisa el módulo
    compartido con su Wasabil simulado; aquí necesitamos la implementación real)."""
    import importlib.util
    ruta = os.path.join(os.path.dirname(__file__), "..", "client.py")
    spec = importlib.util.spec_from_file_location("wasabil_client_fresco2", ruta)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_desenvolver_envelope_real():
    """El API real envuelve las respuestas OK en {success, status, data}; se
    desenvuelve al contenido de data. Sin envoltorio, la respuesta pasa tal cual."""
    wc = _client_fresco()
    # Listado real: {success, status, data: {items, total, lastPage}}
    env = {"success": True, "status": 200,
           "data": {"items": [{"rut": "1-9"}], "total": 1, "lastPage": 1}}
    inner = wc._desenvolver(env)
    assert inner == {"items": [{"rut": "1-9"}], "total": 1, "lastPage": 1}
    assert wc._items(inner) == [{"rut": "1-9"}]
    # Objeto individual envuelto (p.ej. documento) → devuelve el objeto directo
    assert wc._desenvolver({"success": True, "data": {"uuid": "x"}}) == {"uuid": "x"}
    # Sin envoltorio: intacto (defensivo)
    assert wc._desenvolver([{"a": 1}]) == [{"a": 1}]
    assert wc._desenvolver({"uuid": "y"}) == {"uuid": "y"}


def test_como_dict_blinda_documento_unico():
    """Un 2xx con data:null (o no-dict) en crear/estado/obtener documento debe ser
    WasabilError AMBIGUO — nunca un None que reviente con 500 en el router."""
    wc = _client_fresco()
    assert wc._como_dict({"uuid": "x"}, "probar") == {"uuid": "x"}
    for malo in (None, [1, 2], "texto", 42):
        try:
            wc._como_dict(malo, "probar")
            assert False, f"debió lanzar WasabilError con {malo!r}"
        except wc.WasabilError as e:
            assert e.ambiguo is True
    # De punta a punta: el envelope real {success,status,data:null} en el endpoint
    # de estado termina en WasabilError (el sondeo del router degrada elegante)
    orig = wc._request
    try:
        wc._request = lambda m, p, json_body=None, params=None: wc._desenvolver(
            {"success": True, "status": 200, "data": None})
        for fn in (lambda: wc.estado_documento("u-1"),
                   lambda: wc.obtener_documento("u-1"),
                   lambda: wc.crear_documento({"issue": False})):
            try:
                fn()
                assert False, "debió lanzar WasabilError"
            except wc.WasabilError:
                pass
    finally:
        wc._request = orig


def test_armar_lineas_precio_subcentavo_bloquea():
    """El guard evalúa el precio YA redondeado (lo que viaja en la línea): un
    sub-centavo (0.004 → round=0.0) debe bloquear, no emitir una línea en $0."""
    lineas, problemas = armar_lineas([_di(4)], {1: {"precio_venta_clp": 0.004}})
    assert not lineas and any("precio" in p for p in problemas)
    # y un precio normal sigue pasando con el valor redondeado
    lineas, problemas = armar_lineas([_di(4)], {1: {"precio_venta_clp": 15990.404}})
    assert not problemas and lineas[0]["price"] == 15990.4


def test_normalizar_cliente_formato_real():
    """La ficha del API trae giro y dirección ANIDADOS en giros[]/addresses[]
    (marcados 'default'); se aplanan a las claves que usa el router."""
    wc = _client_fresco()
    cli = {
        "id": 158381, "rut": "78.279.030-7", "name": "H-E PARTS INTERNATIONAL CHILE SPA",
        "giros": [{"name": "OTRO", "default": False},
                  {"name": "VENTA DE REPUESTOS", "default": True}],
        "addresses": [{"address": "RUTA 26 KM 15", "comuna": "Antofagasta",
                       "city": "Antofagasta", "default": True}],
    }
    n = wc._normalizar_cliente(cli)
    assert n["giro"] == "VENTA DE REPUESTOS"          # toma el default, no el primero
    assert n["address"] == "RUTA 26 KM 15"
    assert n["comuna"] == "Antofagasta" and n["city"] == "Antofagasta"
    assert n["id"] == 158381 and n["name"] == "H-E PARTS INTERNATIONAL CHILE SPA"
    # Sin 'default' explícito → toma el primero; listas vacías → None sin reventar
    assert wc._normalizar_cliente({"giros": [{"name": "UNICO"}]})["giro"] == "UNICO"
    vacio = wc._normalizar_cliente({"rut": "1-9"})
    assert vacio["giro"] is None and vacio["address"] is None


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))


# ─── FASE B: armadores del DTE 33 ───────────────────────────────────────────────
def test_referencias_factura_matriz_completa():
    from wasabil_dte.service import armar_referencias_factura
    refs, problemas = armar_referencias_factura(
        numero_oc="OC-4501", fecha_oc=date(2026, 6, 10),
        guia_folio="136", guia_fecha=date(2026, 7, 20),
        anticipos=[{"folio": "88", "fecha": date(2026, 7, 19)}])
    assert problemas == []
    assert [r["documentType"] for r in refs] == ["801", "52", "33"]
    assert refs[1]["folio"] == "136" and refs[2]["folio"] == "88"


def test_referencias_factura_bloqueos():
    from wasabil_dte.service import armar_referencias_factura
    # N° OC sobre 18 chars → bloquea (límite SII del folio de referencia)
    _refs, p = armar_referencias_factura(numero_oc="X" * 19, fecha_oc=date(2026, 1, 1))
    assert any("18" in x for x in p)
    # anticipo sin folio SII (placeholder #id) → bloquea
    _refs, p = armar_referencias_factura(
        numero_oc="OC-1", fecha_oc=date(2026, 1, 1),
        anticipos=[{"folio": "#77", "fecha": None}])
    assert any("anticipo" in x.lower() for x in p)
    # más de 5 referencias → bloquea
    _refs, p = armar_referencias_factura(
        numero_oc="OC-1", fecha_oc=date(2026, 1, 1), guia_folio="1",
        anticipos=[{"folio": str(n), "fecha": None} for n in range(1, 6)])
    assert any("máximo" in x or "5" in x for x in p)


def test_armar_factura_estructura():
    from wasabil_dte.service import armar_factura, TIPO_DOC_FACTURA
    doc = armar_factura(
        referencia_interna="FACT-9", lineas=[{"name": "X", "quantity": 1, "price": 10}],
        referencias=[], client_id=160065, issue=False, payment_method="credito")
    assert doc["siiDocumentTypeCode"] == TIPO_DOC_FACTURA
    assert doc["issue"] is False           # el preview JAMÁS emite
    assert doc["invoiceReference"] == "FACT-9"   # ancla v2: sin OC (Wasabil la imprime)
    assert doc["paymentMethod"] == "credito"
    rest = payload_a_rest(doc)
    assert rest["payment_method"] == "credito" and rest["sii_document_type_code"] == 33


def test_referencias_sin_texto_redundante_v3():
    """v3 (hallazgo folio 137): el `reason` NUNCA repite lo que el tipo y el folio
    ya imprimen. Wasabil escribe la etiqueta legible del tipo de referencia, así que
    'Orden de compra 1788' junto a la 801 hacía salir la OC dos veces en el papel.

    Se conserva el reason SOLO donde aporta algo que el tipo no dice: la referencia
    33 a una factura de anticipo explica que se está DESCONTANDO (sin repetir la
    palabra 'Factura' ni el folio, que ya se imprimen)."""
    from datetime import date as _d
    from wasabil_dte.service import armar_referencias_factura
    refs, problemas = armar_referencias_factura(
        numero_oc="1788", fecha_oc=_d(2026, 7, 13),
        guia_folio="137", guia_fecha=_d(2026, 7, 21),
        anticipos=[{"folio": "901", "fecha": _d(2026, 7, 20)}])
    assert not problemas, problemas
    por_tipo = {r["documentType"]: r for r in refs}

    # 801 (OC) y 52 (guía): sin reason — el tipo ya imprime su etiqueta
    assert "reason" not in por_tipo["801"], por_tipo["801"]
    assert "reason" not in por_tipo["52"], por_tipo["52"]

    # 33 (anticipo): conserva el motivo, pero sin repetir tipo ni folio
    razon = por_tipo["33"]["reason"]
    assert razon == "Descuento anticipo", razon
    assert "901" not in razon and "actura" not in razon, razon

    # y ningún reason del documento repite el folio de su propia referencia
    for r in refs:
        assert str(r["folio"]) not in (r.get("reason") or ""), r


# ═══════════════════════════════════════════════════════════════════════════════
# FORMATO v4 — sanitización latin-1, external_id y tope de la vía SII gratuito
# (molde run() + check() + wrapper; sin BD ni red: todo es lógica pura)
# ═══════════════════════════════════════════════════════════════════════════════
def _check_en(fails, name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {str(extra)[:300]}"))
    if not cond:
        fails.append(name)


def run_sanitizacion():
    """El XML del DTE viaja en ISO-8859-1 y el SII rechaza el documento completo
    con «Invalid Character» — sanitizar_latin1 translitera lo tipográfico y bota
    lo incodificable, SIN tocar tildes ni ñ (que sí son latin-1)."""
    fails: list = []
    check = lambda n, c, e="": _check_en(fails, n, c, e)  # noqa: E731

    r = sanitizar_latin1("“ORIGINAL” „OEM„")
    check("comillas dobles tipográficas → rectas", r == '"ORIGINAL" "OEM"', r)
    r = sanitizar_latin1("‘usado’ ‚ok‚")
    check("comillas simples tipográficas → rectas", r == "'usado' 'ok'", r)
    r = sanitizar_latin1("CAT – KOMATSU — VOLVO ― FIN")
    check("guiones – — ― → '-'", r == "CAT - KOMATSU - VOLVO - FIN", r)
    r = sanitizar_latin1("KIT COMPLETO…")
    check("puntos suspensivos … → '...'", r == "KIT COMPLETO...", r)
    r = sanitizar_latin1("• SELLO FRONTAL")
    check("viñeta • → '-'", r == "- SELLO FRONTAL", r)
    r = sanitizar_latin1("PERNO\u00a0M12")   # NBSP en escape: visible y a prueba de editores
    check("espacio no separable → espacio normal", r == "PERNO M12", r)
    r = sanitizar_latin1("FILTRO 🔧 DE ACEITE")
    check("emoji desaparece (y el doble espacio se colapsa)", r == "FILTRO DE ACEITE", r)
    r = sanitizar_latin1("CAÑERÍA HIDRÁULICA Ñ áéíóú")
    check("ñ y tildes SOBREVIVEN intactas (son latin-1)", r == "CAÑERÍA HIDRÁULICA Ñ áéíóú", r)
    check("None y vacío → cadena vacía", sanitizar_latin1(None) == "" and sanitizar_latin1("") == "")
    r = sanitizar_latin1("  A   B  ")
    check("espacios repetidos se colapsan", r == "A B", r)
    # El resultado SIEMPRE es codificable en latin-1 (la garantía que pide el SII)
    for feo in ("日本語 ❄ ☃", "mix ñ – 🚀 “x”"):
        try:
            sanitizar_latin1(feo).encode("latin-1")
            check(f"resultado codificable latin-1: {feo!r}", True)
        except UnicodeEncodeError as e:
            check(f"resultado codificable latin-1: {feo!r}", False, e)
    return fails


def test_sanitizar_latin1():
    assert run_sanitizacion() == [], run_sanitizacion()


def run_lineas_v4():
    """armar_lineas en formato v4: la parte encabeza `name`, external_id viaja en
    AMBAS grafías (el API REST solo lee snake_case y botaba el camelCase en
    silencio; el matching local del precio congelado lee externalId) y la
    description va sanitizada."""
    fails: list = []
    check = lambda n, c, e="": _check_en(fails, n, c, e)  # noqa: E731

    di = _di(4, item_id=1, parte="250-7213", desc="MANGUERA “REFORZADA” 🔧 CAÑERÍA")
    lineas, problemas = armar_lineas([di], {1: {"precio_venta_clp": 1000}})
    check("sin problemas en el caso feliz", problemas == [], problemas)
    ln = lineas[0]
    check("name empieza con el número de parte", ln["name"].startswith("250-7213 "), ln["name"])
    check("external_id presente (snake_case, el que lee el API)", ln.get("external_id") == str(di.id), ln)
    check("externalId se conserva (lo lee el matching local)", ln.get("externalId") == str(di.id), ln)
    check("external_id == externalId (la MISMA identidad)", ln["external_id"] == ln["externalId"])
    check("description sanitizada: comillas rectas, sin emoji, ñ viva",
          ln["description"] == 'MANGUERA "REFORZADA" CAÑERÍA', ln["description"])
    check("name también sanitizado", "🔧" not in ln["name"] and '"REFORZADA"' in ln["name"], ln["name"])
    # payload_a_rest NO toca los details: ambas grafías llegan al REST tal cual
    doc = armar_guia(numero_oc="OC-1", fecha_oc=date(2026, 6, 10),
                     numero_despacho="DSP-1", lineas=lineas)
    rest = payload_a_rest(doc)
    check("ambas grafías sobreviven la traducción REST",
          rest["details"][0].get("external_id") == str(di.id)
          and rest["details"][0].get("externalId") == str(di.id), rest["details"][0])
    return fails


def test_armar_lineas_formato_v4():
    assert run_lineas_v4() == [], run_lineas_v4()


def run_tope_sii_gratuito():
    """La vía SII gratuito rechaza >10 ítems por documento (los 3 únicos fallidos
    históricos de la cuenta). Es ADVERTENCIA, jamás bloqueo: tope de la vía, no
    del formato. Se prueba el generador del aviso + que el router lo cablea en
    los DOS previews (guía y factura)."""
    fails: list = []
    check = lambda n, c, e="": _check_en(fails, n, c, e)  # noqa: E731

    aviso = advertencia_lineas_sii_gratuito(11, "guía")
    check("11 líneas en guía → advertencia", bool(aviso), aviso)
    check("el aviso nombra el tope 10", aviso and str(MAX_LINEAS_SII_GRATUITO) in aviso, aviso)
    # El consejo debe ser EJECUTABLE desde donde se lee: en la guía «dividir»
    # significa ANULAR el despacho y rearmar dos — decir solo «divide el despacho»
    # dejaba al operador sin saber cómo, con la caja ya rotulada.
    check("el aviso de guía nombra el camino real (anular el despacho)",
          aviso and "anula el despacho" in aviso and "NO EMITAS" in aviso, aviso)
    check("10 líneas exactas NO avisan", advertencia_lineas_sii_gratuito(10, "guía") is None)
    check("0 líneas no avisan", advertencia_lineas_sii_gratuito(0, "guía") is None)
    af = advertencia_lineas_sii_gratuito(15, "factura")
    # La factura sale de UNA guía ya emitida e irreversible: «divide la factura en
    # dos documentos» era literalmente inejecutable (el modal manda un despacho_id).
    check("factura con 15 líneas → advertencia de factura",
          bool(af) and "factura" in af, af)
    check("el aviso de factura NO promete una división que no existe",
          af and "divide la factura" not in af and "vía alternativa/manual" in af, af)

    # armar_lineas con 11 ítems NO lo reporta como problema (el aviso es de OTRA lista)
    precios = {i: {"precio_venta_clp": 1000} for i in range(1, 12)}
    lineas, problemas = armar_lineas([_di(1, item_id=i) for i in range(1, 12)], precios)
    check("11 líneas no son problema bloqueante", len(lineas) == 11 and problemas == [], problemas)

    # Sonda ANTI-DERIVA del cableado: los dos previews llaman al generador y lo
    # vuelcan en `advertencias` (armar el preview entero exige BD; aquí se afirma
    # que la RUTA existe en el código real del router — si alguien la borra, cae).
    import inspect
    from wasabil_dte import router as ga_router
    for fn in (ga_router._preparar_emision, ga_router._preparar_emision_factura):
        src = inspect.getsource(fn)
        check(f"{fn.__name__} llama al generador del aviso",
              "advertencia_lineas_sii_gratuito(" in src, fn.__name__)
        check(f"{fn.__name__} lo agrega a advertencias (no a problemas)",
              "advertencias.append(aviso_tope)" in src and
              "problemas.append(aviso_tope)" not in src, fn.__name__)
    return fails


def test_advertencia_tope_sii_gratuito():
    assert run_tope_sii_gratuito() == [], run_tope_sii_gratuito()


def _preview_guia_con_n_lineas(n: int) -> dict:
    """Ejecuta el endpoint REAL POST /despachos/{id}/preview con `n` líneas.

    Los ÚNICOS dobles son las 4 puertas de datos del router (_cargar_contexto,
    _dte_de_despacho, _precios y el cliente de Wasabil): todo lo demás —
    armar_lineas, advertencia_lineas_sii_gratuito, el `advertencias.append` y la
    serialización del payload— corre de verdad. Por eso la sonda cae cuando
    alguien envuelve el append en `if False:`, cambia `len(lineas)` por una
    expresión que nunca supere el tope, o deja de serializar `advertencias`.
    Sin BD y sin red: el viaje con BD real es de las suites de integración."""
    from wasabil_dte import router as ga_router

    items = [_di(1, item_id=i, parte=f"P-{i:04d}") for i in range(1, n + 1)]
    despacho = SimpleNamespace(
        id=1, estado="en_preparacion", numero_despacho="DSP-2026-0051",
        numero_guia=None, direccion_entrega=None, contacto_destinatario=None,
        items=items,
    )
    oc = SimpleNamespace(id=1, numero_oc="OC-4711", fecha_oc="2026-08-01")
    cot = SimpleNamespace(id=1, rut_cliente="76.123.456-7", cliente="H-E PARTS")

    class _WasabilFake:
        WasabilError = ga_router.wasabil.WasabilError

        @staticmethod
        def esta_configurado():
            return True

        @staticmethod
        def buscar_cliente_por_rut(rut):
            # Ficha COMPLETA a propósito: una incompleta agrega SUS PROPIAS
            # advertencias y la sonda dejaría de discriminar cuál llegó.
            return {"id": 99, "rut": rut, "name": "H-E PARTS", "giro": "Minería",
                    "address": "Av. Siempreviva 742", "comuna": "Antofagasta",
                    "city": "Antofagasta"}

    originales = {k: getattr(ga_router, k) for k in
                  ("_cargar_contexto", "_dte_de_despacho", "_precios", "wasabil")}
    ga_router._cargar_contexto = lambda db, did: (despacho, oc, cot)
    ga_router._dte_de_despacho = lambda db, did, lock=False: None
    ga_router._precios = lambda db, c: {
        i: {"precio_venta_clp": 1000} for i in range(1, n + 1)}
    ga_router.wasabil = _WasabilFake
    try:
        return ga_router.preview_guia(
            despacho_id=1, tipo_traslado=TIPO_TRASLADO_VENTA, db=None, current_user=None)
    finally:
        for k, v in originales.items():
            setattr(ga_router, k, v)


def run_advertencia_tope_llega_al_payload():
    """El aviso de >10 líneas VIAJA en el payload del preview de guía — ejecutado,
    no leído del fuente.

    POR QUÉ existe: el cableado solo estaba pinzado por `inspect.getsource`, que no
    ejecuta nada: envolver el append en `if False:` dejaba los 4 checks verdes y el
    operador emitía una guía de 11 líneas que el SII rechaza («Se ha superado la
    cantidad máxima de detalles/items permitidos... 10 o menos») — los 3 únicos
    documentos fallidos de la historia de la cuenta."""
    fails: list = []
    check = lambda n, c, e="": _check_en(fails, n, c, e)  # noqa: E731

    p11 = _preview_guia_con_n_lineas(11)
    avisos = p11.get("advertencias") or []
    check("11 líneas: el aviso del tope LLEGA a advertencias del payload real",
          any(f"{MAX_LINEAS_SII_GRATUITO} ítems" in a for a in avisos), avisos)
    check("sigue siendo ADVERTENCIA, no bloqueo: puede_emitir True",
          p11.get("puede_emitir") is True, p11.get("problemas"))
    check("el aviso NO se cuela en problemas (bloquearía una vía que sí puede migrar)",
          not any(f"{MAX_LINEAS_SII_GRATUITO} ítems" in x
                  for x in (p11.get("problemas") or [])), p11.get("problemas"))
    check("y las 11 líneas efectivamente viajan (el aviso no es un fantasma)",
          len(p11.get("lineas") or []) == 11, len(p11.get("lineas") or []))

    # EL BORDE, sin el cual la sonda no discrimina: una mutación que avise SIEMPRE
    # (o que ignore len(lineas)) pasaría verde con solo el caso de 11.
    p2 = _preview_guia_con_n_lineas(2)
    avisos2 = p2.get("advertencias") or []
    check("≤10 líneas: NINGÚN aviso de tope en el payload",
          not any(f"{MAX_LINEAS_SII_GRATUITO} ítems" in a for a in avisos2), avisos2)
    check("el caso feliz sigue pudiendo emitir", p2.get("puede_emitir") is True,
          p2.get("problemas"))
    return fails


def test_advertencia_tope_llega_al_payload_del_preview():
    assert run_advertencia_tope_llega_al_payload() == [], \
        run_advertencia_tope_llega_al_payload()


# ═══════════════════════════════════════════════════════════════════════════════
# SONDAS 2026-08-26 — huecos que dejó la revisión de las suites
# (mismo molde run()/check() de los grupos v4; sin BD ni red: la única pieza que
# vive en routers/ se prueba con una sesión SIMULADA, ver run_cruce_por_parte)
# ═══════════════════════════════════════════════════════════════════════════════
def _fi(fid=1, parte="1R-0716", desc="Filtro de aceite motor", qty=4.0,
        precio=15990.4, despacho_item_id=501, anticipo=None):
    """ContFacturaClienteItem simulado (línea PERSISTIDA de la factura local)."""
    return SimpleNamespace(id=fid, numero_parte=parte, descripcion=desc,
                           cantidad=qty, precio_unit_neto=precio,
                           total_neto=round(qty * precio, 2),
                           despacho_item_id=despacho_item_id,
                           anticipo_factura_id=anticipo)


def run_lineas_factura_v4():
    """GEMELO del lado factura de run_lineas_v4: revertir el formato v4 SOLO en
    armar_lineas_factura dejaba el gate verde (la sonda v4 existente pinza
    únicamente armar_lineas, el lado guía). Mismos contratos: la parte encabeza
    `name`, external_id viaja en AMBAS grafías con la MISMA identidad, la
    description va sanitizada — y el fallback `fi-{id}` cuando la línea no nació
    de un despacho_item (factura manual sin guía electrónica)."""
    fails: list = []
    check = lambda n, c, e="": _check_en(fails, n, c, e)  # noqa: E731
    from wasabil_dte.service import armar_lineas_factura

    it = _fi(fid=9, parte="250-7213", desc="MANGUERA “REFORZADA” 🔧 CAÑERÍA",
             despacho_item_id=501)
    lineas, problemas = armar_lineas_factura([it])
    check("sin problemas en el caso feliz", problemas == [], problemas)
    ln = lineas[0]
    check("name empieza con el número de parte", ln["name"].startswith("250-7213 "), ln["name"])
    check("external_id presente (snake_case, el que lee el API)",
          ln.get("external_id") == "501", ln)
    check("externalId se conserva (lo lee el matching local)",
          ln.get("externalId") == "501", ln)
    check("external_id == externalId (la MISMA identidad)",
          ln["external_id"] == ln["externalId"])
    check("description sanitizada: comillas rectas, sin emoji, ñ viva",
          ln["description"] == 'MANGUERA "REFORZADA" CAÑERÍA', ln["description"])
    check("name también sanitizado", "🔧" not in ln["name"] and '"REFORZADA"' in ln["name"],
          ln["name"])
    # Fallback de identidad: sin despacho_item_id el ancla es la propia línea local
    lineas2, p2 = armar_lineas_factura([_fi(fid=77, despacho_item_id=None)])
    check("fallback fi-{id} cuando despacho_item_id es None (y en AMBAS grafías)",
          p2 == [] and lineas2[0]["external_id"] == "fi-77"
          and lineas2[0]["externalId"] == "fi-77", (p2, lineas2 and lineas2[0]))
    return fails


def test_armar_lineas_factura_formato_v4():
    assert run_lineas_factura_v4() == [], run_lineas_factura_v4()


def run_code_sanitizado():
    """`code` viaja en el MISMO XML ISO-8859-1 que name/description: un guión
    largo pegado desde el PDF del proveedor en el n° de parte rechaza el DTE
    completo («Invalid Character») aunque `name` vaya limpio — misma clase de
    veneno, otra puerta. Los DOS armadores (guía Y factura) deben limpiarlo."""
    fails: list = []
    check = lambda n, c, e="": _check_en(fails, n, c, e)  # noqa: E731
    from wasabil_dte.service import armar_lineas_factura
    precios = {1: {"precio_venta_clp": 1000}}

    # «1R–0716» trae guión largo (en-dash), el clásico del copy/paste desde PDF
    lineas, p = armar_lineas([_di(4, item_id=1, parte="1R–0716")], precios)
    check("guía: guión largo en numero_parte → code «1R-0716» limpio",
          p == [] and lineas[0]["code"] == "1R-0716", (p, lineas and lineas[0].get("code")))
    check("guía: name arranca con la parte LIMPIA (misma sanitización)",
          lineas[0]["name"].startswith("1R-0716 "), lineas[0]["name"])
    lf, pf = armar_lineas_factura([_fi(parte="1R–0716")])
    check("factura: guión largo en numero_parte → code «1R-0716» limpio",
          pf == [] and lf[0]["code"] == "1R-0716", (pf, lf and lf[0].get("code")))

    # Parte solo-emoji: la sanitización la vacía → code None (no basura cruda)
    lineas, p = armar_lineas([_di(4, item_id=1, parte="🔧")], precios)
    check("guía: parte solo-emoji → code None", p == [] and lineas[0]["code"] is None,
          (p, lineas and lineas[0].get("code")))
    lf, pf = armar_lineas_factura([_fi(parte="🔧")])
    check("factura: parte solo-emoji → code None", pf == [] and lf[0]["code"] is None,
          (pf, lf and lf[0].get("code")))
    return fails


def test_code_sanitizado_ambos_armadores():
    assert run_code_sanitizado() == [], run_code_sanitizado()


def run_controles_c0():
    """Controles C0 (\\x00-\\x1f): SON latin-1 válidos —el encode('latin-1') los
    deja pasar— pero ILEGALES en XML 1.0: el mismo rechazo del SII por otra
    puerta. El criterio NO es «C0 sí / C0 no» sino SEPARADOR vs BASURA:

      · todo lo que `str.isspace()` reconoce —\\t \\n \\r y también \\x0b \\x0c
        \\x1c-\\x1f, más los espacios tipográficos Unicode— se vuelve UN espacio,
        porque botarlo PEGA las palabras en un documento irreversible;
      · lo que no es separador (\\x00, \\x01, \\x7f DEL) desaparece SIN espacio;
      · los de ANCHO CERO (U+200B ZWSP, U+FEFF BOM) también desaparecen sin
        espacio: no son un hueco visual y meterles uno partiría la palabra."""
    fails: list = []
    check = lambda n, c, e="": _check_en(fails, n, c, e)  # noqa: E731

    r = sanitizar_latin1("A\x01B")
    check("control C0 crudo (\\x01) desaparece sin dejar espacio", r == "AB", r)
    r = sanitizar_latin1("A\nB")
    check("salto de línea → UN espacio (no pega las palabras)", r == "A B", r)
    check("tab y CR → espacio también",
          sanitizar_latin1("A\tB") == "A B" and sanitizar_latin1("A\rB") == "A B")
    r = sanitizar_latin1("A\x00B\x1fC")
    check("NUL desaparece (basura) y \\x1f queda como espacio (separador)",
          r == "AB C", r)
    r = sanitizar_latin1("A\x7fB")
    check("DEL (\\x7f) desaparece: es control, no separador", r == "AB", r)

    # ── Separadores que NO son \t\n\r: el caso de papel real ───────────────────
    # Word guarda el salto manual (Shift+Enter) como U+000B VERTICAL TAB y las
    # descripciones se pegan desde Word/Excel/PDF del proveedor. Antes salía
    # impreso «RODILLOINFERIOR» en una guía 52 / factura 33 IRREVERSIBLE.
    for nombre, sep in (("\\x0b VERTICAL TAB (Shift+Enter de Word)", "\x0b"),
                        ("\\x0c FORM FEED", "\x0c"),
                        ("\\x1c FILE SEPARATOR", "\x1c"),
                        ("U+2002 EN SPACE", " "),
                        ("U+2003 EM SPACE", " "),
                        ("U+2009 THIN SPACE", " "),
                        ("U+202F NARROW NBSP", " "),
                        ("U+3000 IDEOGRAPHIC SPACE", "　"),
                        ("U+2028 LINE SEPARATOR", " ")):
        r = sanitizar_latin1(f"RODILLO{sep}INFERIOR")
        check(f"separador {nombre} → UN espacio (no pega las palabras)",
              r == "RODILLO INFERIOR", r)
    # Los tipográficos (U+2002 y siguientes) NO son latin-1: si el reemplazo se
    # hiciera DESPUÉS del encode('latin-1','ignore') ya estarían botados y estos
    # checks caerían — por eso el orden de sanitizar_latin1 es parte del contrato.
    r = sanitizar_latin1("ROD​ILLO")
    check("ancho cero U+200B (ZWSP) desaparece SIN inyectar espacio", r == "RODILLO", r)
    r = sanitizar_latin1("ROD﻿ILLO")
    check("ancho cero U+FEFF (BOM) desaparece SIN inyectar espacio", r == "RODILLO", r)
    r = acortar_nombre("7T-1997", "RODILLO\x0bINFERIOR DE CADENA")
    check("acortar_nombre no imprime «RODILLOINFERIOR» en el name del DTE",
          r == "7T-1997 RODILLO INFERIOR DE CADENA", r)
    # Colado en una descripción real: armar_lineas no lo deja sobrevivir
    di = _di(4, item_id=1, desc="Filtro\x00 sucio\ncon\tcontrol\x1f de flujo")
    lineas, p = armar_lineas([di], {1: {"precio_venta_clp": 1000}})
    d = lineas[0]["description"]
    check("descripción por armar_lineas: controles fuera, palabras separadas",
          p == [] and d == "Filtro sucio con control de flujo", (p, d))
    check("ningún control sobrevive en name ni description",
          all(ord(c) >= 32 for c in lineas[0]["name"] + d), (lineas[0]["name"], d))
    return fails


def test_sanitizar_controles_c0():
    assert run_controles_c0() == [], run_controles_c0()


def run_receiver_contact():
    """receiverContact es texto LIBRE del operador y viaja en el mismo XML
    ISO-8859-1 que las líneas: comillas tipográficas/emoji acá rechazan el DTE
    igual. armar_guia lo sanitiza; si queda vacío, la clave NO va en el doc."""
    fails: list = []
    check = lambda n, c, e="": _check_en(fails, n, c, e)  # noqa: E731

    doc = _guia(contacto="Juan “El Rápido” Pérez 🚀 +56 9 1234 5678")
    rc = doc.get("receiverContact")
    check("comillas tipográficas → rectas y el emoji desaparece",
          rc == 'Juan "El Rápido" Pérez +56 9 1234 5678', rc)
    try:
        rc.encode("latin-1")
        check("receiverContact codificable latin-1 (la garantía del SII)", True)
    except UnicodeEncodeError as e:
        check("receiverContact codificable latin-1 (la garantía del SII)", False, e)
    # Contacto que la sanitización deja VACÍO → sin clave (no un "" fantasma)
    doc2 = _guia(contacto="🚀🔧")
    check("contacto solo-emoji → la clave NO va en el doc",
          "receiverContact" not in doc2, doc2.get("receiverContact"))
    check("…y tampoco se filtra al REST", "receiver_contact" not in payload_a_rest(doc2))
    return fails


def test_receiver_contact_sanitizado():
    assert run_receiver_contact() == [], run_receiver_contact()


def run_cruce_por_parte():
    """El cruce del precio congelado por n° de parte sanitiza SUS DOS lados
    (routers/contabilidad.py): las claves de por_parte que arma
    _precios_congelados_guia Y el lookup `parte = _san_l1(it.numero_parte)` del
    consumidor. Aquí se pinza la mitad UNITARIA del contrato, sin BD:
      · idempotencia (sanitizar dos veces == una: el lado ya-limpio no se mueve)
      · crudo y limpio colapsan a la MISMA clave
      · _precios_congelados_guia con un WasabilDte de MENTIRA (sesión simulada:
        el payload_json viejo con code crudo produce claves SANEADAS, y dos
        grafías del mismo n° cuentan como duplicado → fuera del fallback).
    El viaje completo guía→factura con BD real lo cubre la integración
    (tests_contabilidad + wasabil_dte/tests/test_facturas_integration.py)."""
    fails: list = []
    check = lambda n, c, e="": _check_en(fails, n, c, e)  # noqa: E731

    for crudo in ("1R–0716", "1R-0716", "“X” – 🔧 Ñ", "  A   B  ", "A\x01B"):
        una = sanitizar_latin1(crudo)
        check(f"idempotente: {crudo!r}", sanitizar_latin1(una) == una,
              (una, sanitizar_latin1(una)))
    check("crudo y limpio colapsan a la MISMA clave",
          sanitizar_latin1("1R–0716") == sanitizar_latin1("1R-0716") == "1R-0716")

    # _precios_congelados_guia con sesión simulada (nada toca la BD: el fake
    # devuelve el DTE directo; los filtros de SQLAlchemy se construyen y se botan)
    import json as _json
    from routers.contabilidad import _precios_congelados_guia

    class _QueryFake:
        def __init__(self, dte):
            self._dte = dte

        def filter(self, *a, **k):
            return self

        def first(self):
            return self._dte

    class _DbFake:
        def __init__(self, dte):
            self._dte = dte

        def query(self, *a, **k):
            return _QueryFake(self._dte)

    dte = SimpleNamespace(payload_json=_json.dumps({"details": [
        {"code": "1R–0716", "price": 100.0, "externalId": "501"},  # guión largo CRUDO viejo
        {"code": "6I-2503", "price": 50.0},                        # sin externalId (guía antigua)
    ]}))
    por_di, por_parte = _precios_congelados_guia(_DbFake(dte), 999)
    check("externalId → clave int en por_despacho_item", por_di == {501: 100.0}, por_di)
    check("clave por_parte SANEADA (el code crudo viejo no fabrica clave fantasma)",
          por_parte.get("1R-0716") == 100.0 and "1R–0716" not in por_parte, por_parte)
    check("el lookup del consumidor (_san_l1 del numero_parte crudo) la encuentra",
          por_parte.get(sanitizar_latin1("1R–0716")) == 100.0, por_parte)
    check("la parte sin externalId también entra al fallback",
          por_parte.get("6I-2503") == 50.0, por_parte)

    # Dos GRAFÍAS del mismo n° (crudo y limpio) colapsan → duplicado → el fallback
    # las descarta (matchear "con certeza" cualquiera de las dos sería mentir)
    dte2 = SimpleNamespace(payload_json=_json.dumps({"details": [
        {"code": "1R–0716", "price": 100.0},
        {"code": "1R-0716", "price": 200.0},
    ]}))
    _di2, pp2 = _precios_congelados_guia(_DbFake(dte2), 999)
    check("grafías crudo+limpio del MISMO n° → duplicado, fuera del fallback",
          pp2 == {}, pp2)
    return fails


def test_cruce_por_parte_simetrico():
    assert run_cruce_por_parte() == [], run_cruce_por_parte()
