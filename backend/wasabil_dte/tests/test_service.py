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
    TIPO_REF_OC, acortar_nombre, parse_fecha_oc, cuadratura, armar_lineas,
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


# ─── acortar_nombre (límite 25 del SII) ─────────────────────────────────────────
# v2 (hallazgo folio 136): el nombre es la DESCRIPCIÓN limpia — el N° de parte ya
# viaja en `code` y anteponerlo duplicaba el dato y cortaba la descripción.
def test_acortar_nombre_es_la_descripcion():
    nombre = acortar_nombre("144-9799", "CONJUNTO DE BRAZO (DERECHO)")
    assert len(nombre) <= NOMBRE_MAX
    assert nombre.startswith("CONJUNTO")        # descripción, no el n° de parte
    assert "144-9799" not in nombre             # la parte va en `code`, no acá


def test_acortar_nombre_caso_folio_136():
    # El caso real de la primera emisión: antes salía "ROD-INF-PV351 RODILLO INF"
    assert acortar_nombre("ROD-INF-PV351", "RODILLO INFERIOR") == "RODILLO INFERIOR"


def test_acortar_nombre_casos_borde():
    assert acortar_nombre("1R-0716", "Filtro") == "Filtro"
    assert acortar_nombre("1R-0716", None) == "1R-0716"     # sin descripción: la parte
    assert acortar_nombre(None, "Filtro de aceite") == "Filtro de aceite"
    assert acortar_nombre(None, None) == "ITEM"
    assert len(acortar_nombre("X" * 40, None)) == NOMBRE_MAX
    assert len(acortar_nombre(None, "D" * 40)) == NOMBRE_MAX
    # rstrip REAL: el espacio cae en el índice 24 (dentro del corte a 25) y debe salir
    assert acortar_nombre(None, ("A" * 24) + " EXTRA") == "A" * 24


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
    assert lineas[1]["name"] == "Sello de polvo"      # nombre = descripción
    assert lineas[1]["code"] == "6I-2503"             # la parte viaja en code


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
