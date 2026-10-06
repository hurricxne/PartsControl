"""BUSCADOR del Portal de Clientes — con la app completa, cuentas reales y sesiones reales.

LO QUE ESTA SUITE PROTEGE (GET /api/portal/ordenes?q=…, portal/busqueda.py)
  §A  SE ENCUENTRA COMO LA GENTE ESCRIBE: con o sin guion, con espacio, en minúsculas,
      con la raya larga que pega el correo, con «OC», «N°», «#» u «orden» por delante,
      sin tildes; por N° de orden, N° de parte, descripción, marca y folio de guía o
      factura. Varias palabras = todas (cada una en cualquier campo).
  §B  LO QUE NO ES UN COMODÍN NO SE COMPORTA COMO COMODÍN: `_` y `%` se buscan
      literales, y una búsqueda hostil no rompe nada.
  §C  LA BÚSQUEDA NO ABRE EL CANDADO: un cliente no encuentra la parte, la orden, la guía
      ni la factura de otro, y el buscador sigue correlacionado aunque el candado se
      escribiera sin join.
  §D  TODO CAMPO BUSCABLE ES UN CAMPO REVELADO: no se busca por la guía de un despacho
      que no ha salido, ni por el número tecleado de una guía que está a medio emitir
      (la orden no lo muestra), ni por una factura sin folio, de otra empresa o emitida
      a otro RUT; y la cuenta que no ve facturas (perfil bodega) no puede tantear folios.
  §E  LA PANTALLA SABE POR QUÉ SALIÓ CADA ORDEN (coincidencias), qué se buscó de verdad
      (q_efectivo), y una orden con varios ítems que calzan cuenta UNA vez.
  §F  `colapsar` del portal y el del buscador interno (routers/despachos.py) dan lo mismo.

MUTACIONES verificadas al construir la suite (quitar → rojo → restaurar → verde):
  el escape de comodines (→ §B), la comparación sin guiones en N° de parte y en N° de
  orden (→ §A), las variantes sin prefijo, las palabras-rótulo y la raya larga (→ §A),
  el filtro del cliente (→ §C), el ancla de los EXISTS (→ §C), el despacho visible, la
  guía a medio emitir (con estado y sin estado todavía), la guía rechazada, la factura
  emitida, la factura emitida a otro RUT y el corte por perfil (→ §D), y la comparación
  sin tildes (→ §E).

Corre con:  ./venv/bin/python -m pytest portal/tests/test_busqueda.py -q
(también:   ./venv/bin/python portal/tests/test_busqueda.py)
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi.testclient import TestClient  # noqa: E402

from database import SessionLocal  # noqa: E402
from main import app  # noqa: E402
from models.models import Cotizacion, OcCliente  # noqa: E402
from portal import busqueda  # noqa: E402
from portal.tests import _datos as D  # noqa: E402
from routers.despachos import _colapsar as colapsar_interno  # noqa: E402
from wasabil_dte.models import STATUS_FALLIDO, STATUS_PROCESANDO  # noqa: E402

MARK = "ZZPORTALBUS"  # SIN guiones bajos: en LIKE el _ es comodín
client = TestClient(app)
_fails: list = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _cab(email: str) -> dict:
    r = client.post("/api/auth/login", json={"email": email, "password": D.CLAVE})
    assert r.status_code == 200, f"login de {email}: {r.status_code} {r.text[:120]}"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _buscar(cab: dict, q: str, **params) -> dict:
    r = client.get("/api/portal/ordenes", headers=cab, params={"q": q, "page_size": 50, **params})
    assert r.status_code == 200, f"q={q!r}: {r.status_code} {r.text[:160]}"
    return r.json()


def _ids(cab: dict, q: str, **params) -> set:
    return {o["id"] for o in _buscar(cab, q, **params)["items"]}


def _todas_las_de_a(ids: dict) -> set:
    return {ids[k] for k in ("oc1", "oc2", "oc3", "oc4", "oc5")}


def _sembrar() -> dict:
    db = SessionLocal()
    try:
        fa, fb = D.ficha(db, MARK, "MINERA ESCONDIDA", 73000011), D.ficha(db, MARK, "B", 73000022)
        pa, pb = D.portal(db, MARK, "a", [fa.id]), D.portal(db, MARK, "b", [fb.id])
        bodega = D.portal(db, MARK, "bod", [fa.id], perfil="bodega")

        _, oc1, it1 = D.venta(db, MARK, "A1", fa, [
            dict(numero_parte="1R-0716", descripcion="FILTRO DE ACEITE", marca="CAT"),
            dict(numero_parte="7T1997", descripcion="SELLO CAMIÓN", marca="KOMATSU"),
            dict(numero_parte="1R-0750", descripcion="FILTRO DE COMBUSTIBLE", marca="CAT"),
        ], numero_oc="4500-123456")
        d1 = D.despacho(db, MARK, "A1a", oc1, [(it1[0], 1)], numero_guia="7001")
        D.despacho(db, MARK, "A1b", oc1, [(it1[1], 1)], numero_guia="7999", estado="en_preparacion")
        f1 = D.factura(db, MARK, "A1", oc1, [(it1[0], 1, 1000)], folio="55123")
        D.factura(db, MARK, "A1x", oc1, [(it1[1], 1, 1000)], folio="66123", empresa="automotriz")

        # Tres órdenes cuyo N° de parte solo difiere en el carácter del medio.
        _, oc2, _ = D.venta(db, MARK, "A2", fa, [dict(numero_parte="100_4567", descripcion="BUJE")])
        _, oc3, _ = D.venta(db, MARK, "A3", fa, [dict(numero_parte="100-4567", descripcion="PASADOR")])
        _, oc4, _ = D.venta(db, MARK, "A4", fa, [dict(numero_parte="100X4567", descripcion="GOLILLA 50%",
                                                     estado="despachado")])

        # A5 — documentos que la orden muestra de una forma y la base guarda de otra.
        _, oc5, it5 = D.venta(db, MARK, "A5", fa, [dict(numero_parte="5K-0005", cantidad=10, estado="despachado")])

        def guia(sufijo, numero, n, **del_dte):
            d = D.despacho(db, MARK, sufijo, oc5, [(it5[0], 1)], numero_guia=numero)
            D.dte(db, despacho=d, uuid=f"{D.PREFIJO_ARCHIVO}-0000-4000-8000-{n:012d}", **del_dte)
            return d
        guia("A5a", "GM-4101", 1, folio=None, status_id=STATUS_PROCESANDO)  # a medio emitir
        guia("A5b", "GM-4102", 2, folio=None, status_id=None)               # recién creada, sin respuesta
        guia("A5c", "GM-4103", 3, folio=None, status_id=STATUS_FALLIDO)     # rechazada: vale la de papel
        emitida = guia("A5d", "4104", 4, folio="4104", receptor_rut=fa.rut)
        # Una factura emitida al RUT de OTRO cliente (la venta cambió de dueño) y una normal.
        a_otro_rut = D.factura(db, MARK, "A5v", oc5, [(it5[0], 1, 1000)], folio="77451")
        D.dte(db, factura=a_otro_rut, uuid=f"{D.PREFIJO_ARCHIVO}-0000-4000-8000-{5:012d}", folio="77451",
              receptor_rut=fb.rut)
        normal = D.factura(db, MARK, "A5n", oc5, [(it5[0], 1, 1000)], folio="77452")

        _, ocb, itb = D.venta(db, MARK, "B1", fb, [
            dict(numero_parte="9X-8888", descripcion="BOMBA HIDRAULICA UNICA", marca="HITACHI")],
            numero_oc="4500-999999")
        D.despacho(db, MARK, "B1a", ocb, [(itb[0], 1)], numero_guia="8001")
        D.factura(db, MARK, "B1", ocb, [(itb[0], 1, 1000)], folio="88123")
        db.commit()
        return {
            "oc1": oc1.id, "oc2": oc2.id, "oc3": oc3.id, "oc4": oc4.id, "oc5": oc5.id, "ocb": ocb.id,
            "guia_emitida": emitida.id, "factura_normal": normal.id,
            "it1": [i.id for i in it1], "d1": d1.id, "f1": f1.id, "ficha_a": fa.id,
            "email_a": pa.email, "email_b": pb.email, "email_bod": bodega.email,
        }
    finally:
        db.close()


# ── §A ──────────────────────────────────────────────────────────────────────────────────

def _paso_a_como_escribe_la_gente(cab_a, ids):
    todas = _todas_las_de_a(ids)
    solo_1 = {ids["oc1"]}
    for nombre, q in (
        ("el N° de parte tal cual", "1R-0716"), ("sin guion", "1R0716"), ("en minúsculas", "1r0716"),
        ("con espacio en vez de guion", "1r 0716"), ("con la raya larga del correo", "1R–0716"),
        ("con guion aunque esté guardado sin guion", "7T-1997"), ("un trozo de la descripción", "aceite"),
        ("la descripción sin tilde", "camion"), ("la marca", "komatsu"),
        ("el N° de orden tal cual", "4500-123456"), ("el N° de orden sin guion", "4500123456"),
        ("«OC» y el número", "OC 4500123456"), ("«OC-» pegado y sin guion interno", "OC-4500123456"),
        ("«#» pegado", "#4500-123456"), ("«N°» y el número", "N° 4500123456"),
        ("«orden» y el número", "orden 4500-123456"), ("el folio de la guía", "7001"),
        ("el folio de la factura", "55123"),
        ("dos palabras que están en ítems distintos de la misma orden", "aceite camion"),
        ("parte y marca", "1R0716 cat"),
    ):
        encontrado = _ids(cab_a, q)
        check(f"§A encuentra la orden por {nombre} ({q!r})", encontrado == solo_1, encontrado)
    check("§A dos palabras = las dos: si una no está en la orden, no sale",
          _ids(cab_a, "aceite pasador") == set())
    check("§A solo cuentan las 4 primeras palabras", _ids(cab_a, "aceite camion cat komatsu noexiste") == solo_1)
    check("§A con menos de 2 caracteres no se filtra (y se dice: q_efectivo vacío)",
          _buscar(cab_a, "a")["q_efectivo"] == "" and _ids(cab_a, "a") == todas)
    check("§A palabras de un carácter son ruido: «- 1» no filtra nada",
          _buscar(cab_a, "- 1")["q_efectivo"] == "" and _ids(cab_a, "- 1") == todas)
    check("§A una palabra-rótulo sola sí se busca como texto («orden» no es un comodín)",
          _buscar(cab_a, "orden")["q_efectivo"] == "orden" and _ids(cab_a, "orden") == set())
    check("§A la búsqueda se combina con el filtro de estado",
          _ids(cab_a, "4567", estado="entregadas") == {ids["oc4"]}
          and _ids(cab_a, "4567", estado="abiertas") == {ids["oc2"], ids["oc3"]},
          _ids(cab_a, "4567", estado="entregadas"))


# ── §B ──────────────────────────────────────────────────────────────────────────────────

def _paso_b_comodines(cab_a, ids):
    check("§B «100_4567» encuentra SOLO el guion bajo literal (no 100-4567 ni 100X4567)",
          _ids(cab_a, "100_4567") == {ids["oc2"]}, _ids(cab_a, "100_4567"))
    check("§B «100-4567» encuentra el guion y no el guion bajo ni la X",
          _ids(cab_a, "100-4567") == {ids["oc3"]}, _ids(cab_a, "100-4567"))
    check("§B «50%» encuentra el porcentaje literal", _ids(cab_a, "50%") == {ids["oc4"]}, _ids(cab_a, "50%"))
    check("§B «%%» no es «cualquier cosa»: no encuentra nada", _ids(cab_a, "%%") == set(), _ids(cab_a, "%%"))
    check("§B «__» tampoco", _ids(cab_a, "__") == set(), _ids(cab_a, "__"))
    for hostil in ("' OR 1=1 --", "\\\\", "\\\\%", "\"; DROP TABLE oc_cliente; --", "\x00\x00", "💥💥"):
        r = client.get("/api/portal/ordenes", headers=cab_a, params={"q": hostil})
        check(f"§B búsqueda hostil {hostil!r} → 200 y sin resultados",
              r.status_code == 200 and r.json()["total"] == 0, f"{r.status_code} {r.text[:100]}")
    # La base compara los dígitos arábigo-índicos como si fueran 0-9 (es su collation):
    # «٠١٢٣» calza con «…0123…». No es una fuga (sigue siendo lo propio); solo no debe romper.
    r = client.get("/api/portal/ordenes", headers=cab_a, params={"q": "٠١٢٣"})
    check("§B dígitos de otro alfabeto → 200 y solo órdenes propias",
          r.status_code == 200 and {o["id"] for o in r.json()["items"]} <= _todas_las_de_a(ids),
          f"{r.status_code} {r.text[:100]}")
    check("§B la tabla sigue ahí", _ids(cab_a, "") == _todas_las_de_a(ids))


# ── §C ──────────────────────────────────────────────────────────────────────────────────

def _paso_c_el_candado_sobrevive(cab_a, cab_b, ids):
    for nombre, q in (("la parte", "9X-8888"), ("la parte sin guion", "9X8888"), ("la orden", "4500-999999"),
                      ("la orden sin guion", "4500999999"), ("la guía", "8001"), ("la factura", "88123"),
                      ("la descripción", "hidraulica"), ("la marca", "hitachi")):
        check(f"§C el cliente A no encuentra {nombre} del cliente B", _ids(cab_a, q) == set(), _ids(cab_a, q))
        check(f"§C …y B sí encuentra {nombre} suya", _ids(cab_b, q) == {ids["ocb"]}, _ids(cab_b, q))
    check("§C un prefijo común a las dos órdenes («4500») trae solo la propia",
          _ids(cab_a, "4500") == {ids["oc1"]} and _ids(cab_b, "4500") == {ids["ocb"]})

    # El filtro del buscador sobre una consulta cuyo candado NO une con `cotizaciones`
    # (una subconsulta). Si los EXISTS dependieran de ese join, la parte de B "existiría"
    # para A y volverían todas sus órdenes.
    db = SessionLocal()
    try:
        propias = db.query(Cotizacion.id).filter(Cotizacion.cliente_id == ids["ficha_a"])
        base = db.query(OcCliente).filter(OcCliente.cotizacion_id.in_(propias))
        ajena = busqueda.filtrar(base, busqueda.normalizar("9X-8888"), con_facturas=True).count()
        propia = busqueda.filtrar(base, busqueda.normalizar("1R-0716"), con_facturas=True).count()
    finally:
        db.close()
    check("§C sin el join del candado, el buscador sigue correlacionado con la orden",
          ajena == 0 and propia == 1, (ajena, propia))


# ── §D ──────────────────────────────────────────────────────────────────────────────────

def _paso_d_lo_buscable_es_lo_visible(cab_a, cab_bod, ids):
    check("§D no se busca por la guía de un despacho en preparación", _ids(cab_a, "7999") == set())
    check("§D no se busca por la factura de otra empresa colgada de la orden", _ids(cab_a, "66123") == set())
    check("§D no se busca por el número tecleado de una guía que está a medio emitir (la orden no lo muestra)",
          _ids(cab_a, "GM-4101") == set(), _ids(cab_a, "GM-4101"))
    check("§D …ni por el de una cuyo documento recién se creó y todavía no tiene respuesta",
          _ids(cab_a, "GM-4102") == set(), _ids(cab_a, "GM-4102"))
    check("§D una guía rechazada por el SII sí se busca por su número en papel (es el que la orden muestra)",
          _ids(cab_a, "GM-4103") == {ids["oc5"]}, _ids(cab_a, "GM-4103"))
    c = _buscar(cab_a, "4104")["items"]
    check("§D una guía emitida se busca por su folio, y la coincidencia la nombra",
          [o["id"] for o in c] == [ids["oc5"]]
          and c[0]["coincidencias"] == [{"tipo": "guia", "id": ids["guia_emitida"], "texto": "4104", "detalle": ""}], c)
    check("§D no se busca por el folio de una factura emitida a otro RUT (la orden no la muestra)",
          _ids(cab_a, "77451") == set(), _ids(cab_a, "77451"))
    c = _buscar(cab_a, "7745")["items"]
    check("§D …y si lo buscado calza con esa y con una factura que sí se muestra, la orden sale solo por la que se muestra",
          [o["id"] for o in c] == [ids["oc5"]]
          and c[0]["coincidencias"] == [{"tipo": "factura", "id": ids["factura_normal"], "texto": "77452", "detalle": ""}], c)
    check("§D el perfil bodega no encuentra por folio de factura (no ve facturas)",
          _ids(cab_bod, "55123") == set(), _ids(cab_bod, "55123"))
    check("§D …pero sí por guía y por parte", _ids(cab_bod, "7001") == {ids["oc1"]}
          and _ids(cab_bod, "1R0716") == {ids["oc1"]})
    for campo, q in (("el número interno de cotización", f"{MARK}-A1"), ("el nombre del cliente", "escondida"),
                     ("el número interno del despacho", f"{MARK}-DSP"), ("el RUT", "73.000.011")):
        check(f"§D no se busca por {campo}", _ids(cab_a, q) == set(), _ids(cab_a, q))


# ── §E ──────────────────────────────────────────────────────────────────────────────────

def _paso_e_explicacion(cab_a, cab_bod, ids):
    r = _buscar(cab_a, "filtro")
    check("§E una orden con dos ítems que calzan aparece UNA vez y el total es 1",
          r["total"] == 1 and len(r["items"]) == 1, (r["total"], len(r["items"])))
    coincidencias = r["items"][0]["coincidencias"]
    check("§E …y dice cuáles ítems calzaron (id, N° de parte y descripción)",
          {(c["tipo"], c["id"], c["texto"]) for c in coincidencias}
          == {("item", ids["it1"][0], "1R-0716"), ("item", ids["it1"][2], "1R-0750")}, coincidencias)
    c = _buscar(cab_a, "camion")["items"][0]["coincidencias"]
    check("§E la coincidencia sin tilde también se explica («camion» ↔ «CAMIÓN»)",
          [x["texto"] for x in c] == ["7T1997"], c)
    c = _buscar(cab_a, "7t-1997")["items"][0]["coincidencias"]
    check("§E la coincidencia sin guion también se explica", [x["texto"] for x in c] == ["7T1997"], c)
    c = _buscar(cab_a, "7001")["items"][0]["coincidencias"]
    check("§E si salió por la guía, lo dice", c == [{"tipo": "guia", "id": ids["d1"], "texto": "7001", "detalle": ""}], c)
    c = _buscar(cab_a, "55123")["items"][0]["coincidencias"]
    check("§E si salió por la factura, lo dice", c == [{"tipo": "factura", "id": ids["f1"], "texto": "55123", "detalle": ""}], c)
    check("§E sin búsqueda no hay campo de coincidencias", "coincidencias" not in _buscar(cab_a, "")["items"][0])
    check("§E q_efectivo son las palabras que se buscaron de verdad",
          _buscar(cab_a, "  OC   4500123456  ")["q_efectivo"] == "4500123456"
          and _buscar(cab_a, "uno dos tres cuatro cinco")["q_efectivo"] == "uno dos tres cuatro")
    # «123» está en el N° de orden (4500-123456) y también en el folio de la factura (55123):
    # la orden sale por su número, y la factura solo se nombra a quien ve facturas.
    con_facturas = _buscar(cab_a, "123")["items"][0]["coincidencias"]
    check("§E una palabra que calza con el N° de orden y con una factura: procurement ve la factura nombrada",
          [c["texto"] for c in con_facturas if c["tipo"] == "factura"] == ["55123"], con_facturas)
    texto = client.get("/api/portal/ordenes", headers=cab_bod, params={"q": "123"}).text
    check("§E …y al perfil bodega la misma búsqueda le trae la orden SIN el folio de la factura",
          str(ids["oc1"]) in texto and "55123" not in texto, texto[:200])


# ── §F ──────────────────────────────────────────────────────────────────────────────────

def _paso_f_paridad_con_el_buscador_interno():
    muestras = ["1R-0716", "1r 0716", " 7T-1997 ", "100_4567", "OC-4500 123", "", "a-b-c", "Ñandú-1"]
    distintos = [m for m in muestras if busqueda.colapsar(m) != colapsar_interno(m)]
    check("§F colapsar() del portal = _colapsar() de Despachos en todas las muestras", not distintos, distintos)


def run():
    del _fails[:]
    D.limpiar(MARK)
    try:
        ids = _sembrar()
        cab_a, cab_b, cab_bod = _cab(ids["email_a"]), _cab(ids["email_b"]), _cab(ids["email_bod"])
        _paso_a_como_escribe_la_gente(cab_a, ids)
        _paso_b_comodines(cab_a, ids)
        _paso_c_el_candado_sobrevive(cab_a, cab_b, ids)
        _paso_d_lo_buscable_es_lo_visible(cab_a, cab_bod, ids)
        _paso_e_explicacion(cab_a, cab_bod, ids)
        _paso_f_paridad_con_el_buscador_interno()
        print()
        print(f"FALLARON {len(_fails)}: {_fails}" if _fails else "SUITE VERDE: buscador del portal")
        assert not _fails, _fails
    finally:
        D.limpiar(MARK)


def test_busqueda_portal():
    run()


if __name__ == "__main__":
    run()
