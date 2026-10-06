"""MONTOS que ve el cliente — cuánto vale cada ítem de su pedido.

LO QUE ESTA SUITE PROTEGE (portal/valor.py, portal/precios.py y lo que de ellos sale por
/api/portal/ordenes/{id} y /api/portal/resumen)
  §A  (funciones puras, sin base) EL VALOR SE ARMA POR TRAMOS DE UNIDADES: lo facturado
      vale lo que dicen sus facturas; lo que viajó en una guía electrónica emitida y aún
      no se factura, el precio de esa guía; el resto, el precio calculado. Hay unitario
      solo si todas las unidades tienen el mismo precio. La tabla de casos de abajo ES
      la especificación.
  §B  (con la app y la base) LO MISMO, LEYENDO FACTURAS Y GUÍAS REALES: una guía
      rechazada por el SII no fija precio; una línea de guía ya facturada no se cuenta
      dos veces; una factura antigua que no dice qué guía factura no duplica unidades.
  §C  SIN PRECIO CONGELADO NO HAY MONTOS, tenga o no facturas la venta; un precio $0 es
      un dato que falta; una foto de precios incompleta no congela nada.
  §D  «POR ENTREGAR» no incluye lo que el cliente firmó como no recibido.
  §E  EL TABLERO suma lo mismo que se ve orden por orden, y cuenta las órdenes sin monto.

MUTACIONES verificadas al construir la suite: la lista está en portal/tests/mutaciones/
(`python -m portal.tests.mutaciones valor` la vuelve a correr).

Corre con:  ./venv/bin/python -m pytest portal/tests/test_valor.py -q
(la parte con base, sola:  ./venv/bin/python portal/tests/test_valor.py)
"""
import json
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import SessionLocal  # noqa: E402
from main import app  # noqa: E402
from portal import valor  # noqa: E402
from portal.tests import _datos as D  # noqa: E402
from services.pricing_service import CLAVES_PRICING  # noqa: E402
from wasabil_dte.models import STATUS_FALLIDO  # noqa: E402

MARK = "ZZPORTALVAL"  # SIN guiones bajos: en LIKE el _ es comodín
client = TestClient(app)
_fails: list = []

Fila, Factura, Guia = valor.FilaValorizable, valor.LineaFacturada, valor.EnGuia


def _fila(cantidad, calculado, facturadas=(), en_guias=()):
    return Fila(cantidad=cantidad, calculado=calculado,
                facturadas=[Factura(*f) for f in facturadas], en_guias=[Guia(*g) for g in en_guias])


# (qué prueba, filas del renglón, unidades por entregar) → (unitario, total, valor por entregar)
# Una línea de factura es (cantidad, total_neto, precio_unitario, id de su línea de guía).
CASOS = [
    ("sin documentos: precio calculado × cantidad",
     [_fila(10, 100.0)], 10, (100.0, 1000, 1000)),
    ("renglón partido en dos filas: UN redondeo sobre el total (41), no uno por fila (21 + 21)",
     [_fila(2, 10.25), _fila(2, 10.25)], 4, (10.25, 41, 41)),
    ("facturado completo a un precio distinto del calculado: manda la factura",
     [_fila(3, 1000.0, [(3, 3300, 1100.0, 7)])], 0, (1100.0, 3300, 0)),
    ("dos tandas facturadas a precios distintos: el total es lo que suman las facturas; sin unitario",
     [_fila(10, 180000.0, [(6, 925759.2, 154293.20, 1), (4, 844714.92, 211178.73, 2)])], 0, (None, 1770474, 0)),
    ("facturado en parte: lo facturado a su precio, el resto al calculado",
     [_fila(10, 1000.0, [(6, 7407, 1234.5, 1)])], 4, (None, 11407, 4000)),
    ("facturado en parte al MISMO precio que el calculado: sí hay unitario",
     [_fila(10, 1000.0, [(6, 6000, 1000.0, 1)])], 4, (1000.0, 10000, 4000)),
    ("guía emitida sin facturar: esas unidades valen el precio de la guía",
     [_fila(5, 1000.0, en_guias=[(2, 1500.0)])], 3, (None, 6000, 3000)),
    ("una guía no puede valorizar más unidades que las que quedan sin factura (la más nueva primero)",
     [_fila(10, 1000.0, [(6, 7200, 1200.0, None)], [(4, 1500.0), (6, 1200.0)])], 0, (None, 13200, 0)),
    ("sin precio calculado no hay monto, aunque haya factura",
     [_fila(10, None, [(6, 2880000, 480000.0, 1)])], 4, (None, None, None)),
    ("…y basta una fila sin precio para que el renglón no tenga monto",
     [_fila(2, 1000.0), _fila(3, None)], 5, (None, None, None)),
    ("facturación adelantada: lo que falta por entregar vale el precio de su factura",
     [_fila(5, 1000.0, [(5, 5500, 1100.0, None)])], 5, (1100.0, 5500, 5500)),
    ("falta por entregar más de lo que queda sin documento: el exceso, al promedio documentado",
     [_fila(10, 1000.0, [(8, 8800, 1100.0, None)])], 5, (None, 10800, 2000 + 3300)),
    ("facturado de más (corrección manual): el total es lo facturado",
     [_fila(2, 1000.0, [(3, 3000, 1000.0, None)])], 0, (1000.0, 3000, 0)),
    ("nada por entregar → 0 (y una cantidad negativa también)",
     [_fila(4, 250.0)], -3, (250.0, 1000, 0)),
]


@pytest.mark.parametrize("nombre,filas,por_entregar,esperado", CASOS, ids=[c[0] for c in CASOS])
def test_a_valor_por_tramos(nombre, filas, por_entregar, esperado):
    assert valor.montos(filas, por_entregar) == esperado


def test_a_los_tramos_suman_la_cantidad_del_renglon():
    tramos = valor.tramos_de([_fila(10, 1000.0, [(6, 7407, 1234.5, 1)], [(3, 1500.0)])])
    assert [(t.unidades, t.neto, t.unitario, t.documentado) for t in tramos] == [
        (6, 7407, 1234.5, True), (3, 4500, 1500.0, True), (1, 1000, 1000.0, False)]


# ── La parte con base ───────────────────────────────────────────────────────────────────

FOTO_COMPLETA = {
    "tipo_cambio_usd": 950.0, "costo_shipping_usd_kg": 4.0, "adicionales_shipping_usd": 300.0,
    "costo_agencia_pct": 0.01, "costo_agencia_minimo_clp": 150000.0,
    "desconsolidado_clp": 80000.0, "bodegaje_clp": 70000.0, "margen_venta_pct": 0.25,
}
# Fotos de precios que NO congelan nada: ilegibles, vacías, incompletas o con basura.
FOTOS_QUE_NO_CONGELAN = {
    "cortada": "{",
    "lista": "[]",
    "vacia": "{}",
    "nula": "null",
    "ajena": '{"x": 1}',
    "sin-una-clave": json.dumps({k: v for k, v in FOTO_COMPLETA.items() if k != "tipo_cambio_usd"}),
    "clave-nula": json.dumps({**FOTO_COMPLETA, "tipo_cambio_usd": None}),
    "clave-texto": json.dumps({**FOTO_COMPLETA, "tipo_cambio_usd": "abc"}),
    # Estas dos NO hacen fallar el cálculo: darían un precio (absurdo) si se aceptaran.
    "clave-booleana": json.dumps({**FOTO_COMPLETA, "tipo_cambio_usd": True}),
    "clave-infinita": json.dumps({**FOTO_COMPLETA, "margen_venta_pct": float("inf")}),
}


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _cab(email: str) -> dict:
    r = client.post("/api/auth/login", json={"email": email, "password": D.CLAVE})
    assert r.status_code == 200, f"login de {email}: {r.status_code} {r.text[:120]}"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _orden(cab: dict, oc_id: int) -> dict:
    r = client.get(f"/api/portal/ordenes/{oc_id}", headers=cab)
    assert r.status_code == 200, f"orden {oc_id}: {r.status_code} {r.text[:160]}"
    return r.json()


def _uuid(n: int) -> str:
    return f"{D.PREFIJO_ARCHIVO}-0000-4000-8000-{n:012d}"


def _sembrar() -> dict:
    db = SessionLocal()
    try:
        fa, fb = D.ficha(db, MARK, "A", 78000011), D.ficha(db, MARK, "B", 78000022)
        pa, pb = D.portal(db, MARK, "a", [fa.id]), D.portal(db, MARK, "b", [fb.id])
        ids = {"email_a": pa.email, "email_b": pb.email}

        # V1 — la única orden de la cuenta A: todos sus ítems tienen precio (venta en
        # pesos, $1.000 la unidad salvo el último), y cada uno prueba un cruce con sus documentos.
        _, oc, it = D.venta(db, MARK, "V1", fa, [
            dict(numero_parte="1V-0001", cantidad=10, precio=1000, estado="despachado"),  # 6 de 10 facturadas a otro precio
            dict(numero_parte="1V-0002", cantidad=3, precio=1000, estado="despachado"),   # todo facturado a otro precio
            dict(item_num=3, numero_parte="1V-0003", descripcion="BUJE", cantidad=2, precio=1000, estado="despachado"),
            dict(item_num=3, numero_parte="1V-0003", descripcion="BUJE", cantidad=3, precio=1000, estado="cerrado"),
            dict(numero_parte="1V-0004", cantidad=10, precio=1000, estado="despachado"),  # dos tandas, dos precios
            dict(numero_parte="1V-0005", cantidad=5, precio=1000, estado="en_bodega"),    # guía emitida sin facturar
            dict(numero_parte="1V-0006", cantidad=5, precio=1000, estado="en_bodega"),    # guía RECHAZADA por el SII
            dict(numero_parte="1V-0007", cantidad=10, precio=1000, estado="despachado"),  # factura antigua + guía nueva
            dict(numero_parte="1V-0008", cantidad=10, precio=1000, estado="en_bodega"),   # línea de guía YA facturada
            dict(numero_parte="1V-0009", cantidad=10, precio=100000, estado="despachado"),  # firmó 8 de 10
        ])
        i1, i2, i3a, _i3b, i4, i5, i6, i7, i8, i9 = it
        a = D.despacho(db, MARK, "V1a", oc, [(i1, 6), (i2, 3), (i3a, 2)], numero_guia="8001")
        D.despacho(db, MARK, "V1b", oc, [(i1, 4)], numero_guia="8002")
        D.factura(db, MARK, "V1-1", oc, [(i1, 6, 1234.5), (i2, 3, 1100, a), (i3a, 2, 1100, a)], fecha=date(2026, 4, 12))
        tanda1 = D.despacho(db, MARK, "V1c", oc, [(i4, 6)], numero_guia="8003")
        tanda2 = D.despacho(db, MARK, "V1d", oc, [(i4, 4)], numero_guia="8004")
        D.factura(db, MARK, "V1-2", oc, [(i4, 6, 1200, tanda1)])
        D.factura(db, MARK, "V1-3", oc, [(i4, 4, 1500, tanda2)])
        emitida = D.despacho(db, MARK, "V1e", oc, [(i5, 2)], numero_guia="8005")
        D.dte(db, despacho=emitida, uuid=_uuid(5), folio="8005", receptor_rut=fa.rut, precios={i5.id: 1500})
        rechazada = D.despacho(db, MARK, "V1f", oc, [(i6, 2)], numero_guia="8006")
        D.dte(db, despacho=rechazada, uuid=_uuid(6), folio=None, status_id=STATUS_FALLIDO, precios={i6.id: 1500})
        # Guía antigua (ya facturada, pero su factura no dice qué línea de guía factura) y
        # guía nueva sin facturar, las dos electrónicas.
        antigua = D.despacho(db, MARK, "V1g", oc, [(i7, 6)], numero_guia="8007", fecha=datetime(2026, 3, 1, 15, 0))
        D.dte(db, despacho=antigua, uuid=_uuid(7), folio="8007", receptor_rut=fa.rut, precios={i7.id: 1200})
        nueva = D.despacho(db, MARK, "V1h", oc, [(i7, 4)], numero_guia="8008", fecha=datetime(2026, 3, 9, 15, 0))
        D.dte(db, despacho=nueva, uuid=_uuid(8), folio="8008", receptor_rut=fa.rut, precios={i7.id: 1500})
        D.factura(db, MARK, "V1-4", oc, [(i7, 6, 1200)])
        # Guía electrónica cuya línea ya está facturada (a un precio que no es el de la guía).
        facturada = D.despacho(db, MARK, "V1i", oc, [(i8, 3)], numero_guia="8009")
        D.dte(db, despacho=facturada, uuid=_uuid(9), folio="8009", receptor_rut=fa.rut, precios={i8.id: 999})
        D.factura(db, MARK, "V1-5", oc, [(i8, 3, 1100, facturada)])
        con_faltante = D.despacho(db, MARK, "V1j", oc, [(i9, 10, 8)], numero_guia="8010", firmada=True)
        D.factura(db, MARK, "V1-6", oc, [(i9, 8, 100000, con_faltante)])
        ids["v1"] = oc.id

        # La cuenta B: lo que NO muestra montos, y una venta 'costo' con la foto completa.
        _, oc, _ = D.venta(db, MARK, "V2", fb, [dict(numero_parte="2V-0001", cantidad=2, precio=700),
                                                 dict(numero_parte="2V-0002", cantidad=1, precio=0)])
        ids["v2"] = oc.id
        _, oc, it = D.venta(db, MARK, "V3", fb, [dict(numero_parte="3V-0001", cantidad=10, precio=500.0, peso=2.0,
                                                      estado="en_bodega")], origen="costo")
        guia = D.despacho(db, MARK, "V3a", oc, [(it[0], 6)], numero_guia="8101")
        D.factura(db, MARK, "V3", oc, [(it[0], 6, 480000, guia)])
        ids["v3"] = oc.id
        _, oc, _ = D.venta(db, MARK, "V4", fb, [
            dict(item_num=1, numero_parte="4V-0001", descripcion="BOMBA", cantidad=6, precio=1000.0, peso=10.0, margen=0.25),
            dict(item_num=1, numero_parte="4V-0001", descripcion="BOMBA", cantidad=4, precio=1000.0, peso=10.0, margen=0.40),
        ], origen="costo", pricing_snapshot=json.dumps(FOTO_COMPLETA))
        ids.update(v4=oc.id, sin_congelar={})
        for nombre, foto in FOTOS_QUE_NO_CONGELAN.items():
            _, oc, _ = D.venta(db, MARK, f"V5-{nombre}", fb, [dict(numero_parte="5V-0001", precio=500.0, peso=1.0)],
                               origen="costo", pricing_snapshot=foto)
            ids["sin_congelar"][nombre] = oc.id
        db.commit()
        return ids
    finally:
        db.close()


def _paso_b_documentos_reales(cab_a, ids):
    orden = _orden(cab_a, ids["v1"])
    monto = {l["numero_parte"]: l["monto"] for l in orden["items"]}
    esperado = {
        "1V-0001": ("6 de 10 facturadas a $1.234,5: esas a su precio, las otras 4 al calculado; sin unitario",
                    {"unitario_clp": None, "total_clp": 7407 + 4000}),
        "1V-0002": ("todo facturado a $1.100: manda la factura, no los $1.000 de la cotización",
                    {"unitario_clp": 1100.0, "total_clp": 3300}),
        "1V-0003": ("renglón en dos filas, una facturada a otro precio: sin unitario, y el total suma las dos",
                    {"unitario_clp": None, "total_clp": 2200 + 3000}),
        "1V-0004": ("dos tandas facturadas a $1.200 y $1.500: el total es lo que suman sus dos facturas",
                    {"unitario_clp": None, "total_clp": 7200 + 6000}),
        "1V-0005": ("2 unidades en una guía electrónica emitida a $1.500 y aún sin facturar: valen ese precio",
                    {"unitario_clp": None, "total_clp": 3000 + 3000}),
        "1V-0006": ("la guía RECHAZADA por el SII no fija ningún precio",
                    {"unitario_clp": 1000.0, "total_clp": 5000}),
        "1V-0007": ("factura antigua de 6 (no dice qué guía factura) + guía nueva de 4 a $1.500: no se duplican unidades",
                    {"unitario_clp": None, "total_clp": 7200 + 6000}),
        "1V-0008": ("la línea de guía que ya está facturada no se valoriza otra vez con el precio de la guía",
                    {"unitario_clp": None, "total_clp": 3300 + 7000}),
        "1V-0009": ("firmó 8 de 10, facturadas las 8: el pedido sigue valiendo las 10 que pidió",
                    {"unitario_clp": 100000.0, "total_clp": 1000000}),
    }
    for parte, (nombre, valor_esperado) in esperado.items():
        check(f"§B {nombre}", monto[parte] == valor_esperado, (parte, monto[parte]))
    neto = sum(m["total_clp"] for _, m in esperado.values())
    check("§B el neto de la orden es la suma de sus ítems, y el IVA va sobre ese neto",
          orden["orden"]["totales"] == {"neto_clp": neto, "iva_clp": round(neto * 0.19), "total_clp": neto + round(neto * 0.19)},
          (orden["orden"]["totales"], neto))
    return neto, sum(f["neto_clp"] for f in orden["facturas"])


def _paso_c_sin_precio_congelado(cab_b, ids):
    v2 = _orden(cab_b, ids["v2"])
    montos = {l["numero_parte"]: l["monto"] for l in v2["items"]}
    check("§C un ítem a $0 no tiene monto (es un precio que falta), y entonces la orden no tiene totales",
          montos == {"2V-0001": {"unitario_clp": 700.0, "total_clp": 1400}, "2V-0002": None}
          and v2["orden"]["totales"] is None, (montos, v2["orden"]["totales"]))
    v3 = _orden(cab_b, ids["v3"])
    check("§C venta sin precio congelado con 6 de 10 facturadas: el ítem NO muestra monto (ni el de su factura)",
          v3["items"][0]["monto"] is None and v3["orden"]["totales"] is None, v3["items"][0]["monto"])
    check("§C …y su factura se sigue viendo como documento, con su neto",
          [f["neto_clp"] for f in v3["facturas"]] == [2880000], v3["facturas"])
    v4 = _orden(cab_b, ids["v4"])
    check("§C venta 'costo' con la foto completa: dos filas que solo difieren en el margen son dos ítems, cada uno con su precio",
          len(v4["items"]) == 2 and len({l["monto"]["unitario_clp"] for l in v4["items"]}) == 2
          and v4["orden"]["totales"] is not None, [l["monto"] for l in v4["items"]])
    for nombre, oc_id in ids["sin_congelar"].items():
        orden = _orden(cab_b, oc_id)
        check(f"§C foto de precios «{nombre}»: no congela nada → la orden va sin montos",
              orden["orden"]["totales"] is None and all(l["monto"] is None for l in orden["items"]),
              orden["orden"]["totales"])
    check("§C (la sonda sirve) la foto completa trae exactamente las claves que el cálculo exige",
          set(FOTO_COMPLETA) == set(CLAVES_PRICING), set(CLAVES_PRICING) ^ set(FOTO_COMPLETA))
    return v4["orden"]["totales"]["neto_clp"]


def _paso_d_y_e_tablero(cab_a, cab_b, ids, neto_v1, facturado_v1, neto_v4):
    tablero = client.get("/api/portal/resumen", headers=cab_a).json()
    # Lo que todavía se espera de V1: 3 del buje sin despachar, 3 + 3 + 7 que siguen en
    # bodega; y NADA por las 2 unidades que el cliente firmó como no recibidas.
    check("§D «por entregar» vale lo que todavía se espera (16.000) y no las 2 unidades no recibidas (200.000 más)",
          tablero["montos"]["por_entregar_neto_clp"] == 3000 + 3000 + 3000 + 7000, tablero["montos"])
    check("§E comprado y facturado del tablero = el neto de la orden y la suma de sus facturas",
          tablero["montos"]["comprado_neto_clp"] == neto_v1 and tablero["montos"]["facturado_neto_clp"] == facturado_v1
          and tablero["montos"]["ordenes_sin_monto"] == 0, (tablero["montos"], neto_v1, facturado_v1))

    tablero_b = client.get("/api/portal/resumen", headers=cab_b).json()
    sin_monto = 2 + len(ids["sin_congelar"])  # V2 (un precio $0), V3 (sin foto) y las fotos que no congelan
    check("§E las órdenes sin monto se cuentan aparte y no entran en ninguna suma (ni sus facturas)",
          tablero_b["montos"] == {"comprado_neto_clp": neto_v4, "por_entregar_neto_clp": neto_v4,
                                  "facturado_neto_clp": 0, "ordenes_sin_monto": sin_monto}, tablero_b["montos"])


def run():
    del _fails[:]
    D.limpiar(MARK)
    try:
        with D.carpetas_de_prueba():
            ids = _sembrar()
            cab_a, cab_b = _cab(ids["email_a"]), _cab(ids["email_b"])
            neto_v1, facturado_v1 = _paso_b_documentos_reales(cab_a, ids)
            neto_v4 = _paso_c_sin_precio_congelado(cab_b, ids)
            _paso_d_y_e_tablero(cab_a, cab_b, ids, neto_v1, facturado_v1, neto_v4)
        print()
        print(f"FALLARON {len(_fails)}: {_fails}" if _fails else "SUITE VERDE: montos del portal")
        assert not _fails, _fails
    finally:
        D.limpiar(MARK)


def test_montos_portal():
    run()


if __name__ == "__main__":
    run()
