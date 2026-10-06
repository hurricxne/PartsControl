"""ÍTEMS de una orden como los ve el cliente — unidades, estados, fechas y recorrido, con
la app completa, cuentas reales y sesiones reales. (Los montos están en test_valor.py.)

LO QUE ESTA SUITE PROTEGE (portal/renglones.py, seguimiento.py y lo que de ellos sale por
/api/portal/ordenes/{id} y …/items/{id})
  §A  UN RENGLÓN ES UN RENGLÓN: las filas de un split se muestran como un ítem con su
      desglose; su id no cambia; su estado es el de la parte que va más atrás.
  §B  LAS UNIDADES SE CUENTAN UNA VEZ Y EN SU LUGAR: lo que salió, lo que está en bodega
      y lo que falta. Un despacho en preparación no salió. Lo que falta es «en gestión»
      solo si hay un reclamo; en la compra nacional, que llega por tandas, es «en proceso
      de compra». La regla de «recibido» es la del tope físico de Despachos.
  §C  LO QUE EL CLIENTE FIRMÓ COMO NO RECIBIDO se muestra como eso («No recibido en la
      entrega»): no figura entregado, no promete una reposición, no deja el ítem
      «atrasado» ni suma en «por entregar»; la orden sigue abierta.
  §D  «FACTURADO» ES POR UNIDADES: solo las que ya figuran en una factura.
  §E  LA FECHA es la comprometida (congelada); «vencida» solo aplica a lo que todavía se
      espera y a partir del día siguiente; la fecha de la orden es la MÁS ANTIGUA entre
      lo que se espera (aunque ya haya pasado); los días son los de Chile.
  §F  EL RECORRIDO lleva las fechas en que cada paso ocurrió de verdad, no marca «en
      bodega» lo que nunca llegó, y no afirma tránsito ni aduana de lo que llegó sin
      embarque.
  §H  UNA VENTA, UNA ORDEN, aunque tenga dos filas oc_cliente.

MUTACIONES verificadas al construir la suite: la lista está en portal/tests/mutaciones/
(`python -m portal.tests.mutaciones renglones` la vuelve a correr).

Corre con:  ./venv/bin/python -m pytest portal/tests/test_renglones.py -q
(también:   ./venv/bin/python portal/tests/test_renglones.py)
"""
import json
import os
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi.testclient import TestClient  # noqa: E402

from database import SessionLocal  # noqa: E402
from main import app  # noqa: E402
from models.models import OcCliente  # noqa: E402
from portal import ordenes, precios, renglones  # noqa: E402
from portal.deps import PortalScope  # noqa: E402
from portal.tests import _datos as D  # noqa: E402
from portal.tiempo import hoy_chile  # noqa: E402
from routers import despachos as despachos_del_erp  # noqa: E402

MARK = "ZZPORTALREN"  # SIN guiones bajos: en LIKE el _ es comodín
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


def _orden(cab: dict, oc_id: int) -> dict:
    r = client.get(f"/api/portal/ordenes/{oc_id}", headers=cab)
    assert r.status_code == 200, f"orden {oc_id}: {r.status_code} {r.text[:160]}"
    return r.json()


def _item(cab: dict, oc_id: int, item_id: int) -> dict:
    r = client.get(f"/api/portal/ordenes/{oc_id}/items/{item_id}", headers=cab)
    assert r.status_code == 200, f"ítem {item_id}: {r.status_code} {r.text[:160]}"
    return r.json()


def _por_parte(orden: dict) -> dict:
    return {l["numero_parte"]: l for l in orden["items"]}


def _partes(linea: dict) -> dict:
    return {p["estado"]["clave"]: p["cantidad"] for p in linea["partes"]}


def _hitos(seguimiento: dict) -> dict:
    return {h["clave"]: h for h in seguimiento["hitos"]}


def _renglones_internos(oc_id: int, ids: dict) -> dict:
    """{numero_parte: Renglon} de una orden, armados como los arma el portal (para mirar
    lo que no viaja al navegador: unidades y valor por entregar)."""
    db = SessionLocal()
    try:
        scope = PortalScope(usuario_id=ids["uid_a"], cliente_ids=frozenset({ids["ficha_a"]}), perfil="procurement")
        oc = ordenes.buscar_orden(db, scope, oc_id)
        datos = renglones.cargar(db, scope, [oc])
        return {r.numero_parte: r for r in renglones.lineas(oc, datos, hoy_chile())}
    finally:
        db.close()


# ── Siembra ─────────────────────────────────────────────────────────────────────────────

def _sembrar() -> dict:
    hoy = hoy_chile()
    db = SessionLocal()
    try:
        fa = D.ficha(db, MARK, "A", 77000011)
        pa = D.portal(db, MARK, "a", [fa.id])
        inactivo = D.interno(db, MARK, "exasesor")
        inactivo.is_active = 0
        ids = {"email_a": pa.email, "uid_a": pa.id, "ficha_a": fa.id, "rut_a": fa.rut}

        # R1 — un renglón partido en dos filas; uno entregado; uno con llegada y despacho
        # parciales por embarque (con su reclamo, como lo deja Bodega); uno en reclamo.
        _, oc, it = D.venta(db, MARK, "R1", fa, [
            dict(item_num=1, numero_parte="1R-0716", descripcion="FILTRO DE ACEITE", cantidad=3,
                 precio=33333.5, estado="comprado", promesa=hoy + timedelta(days=10)),
            dict(item_num=1, numero_parte="1R-0716", descripcion="FILTRO DE ACEITE", cantidad=4,
                 precio=33333.5, estado="embarcado", promesa=hoy + timedelta(days=10)),
            dict(item_num=2, numero_parte="7T-1997", descripcion="SELLO", cantidad=2, precio=50000,
                 estado="despachado", promesa=hoy - timedelta(days=30)),
            dict(item_num=3, numero_parte="5P-0001", descripcion="PERNO", cantidad=10, precio=1200,
                 estado="en_bodega", promesa=hoy - timedelta(days=2)),
            dict(item_num=4, numero_parte="9X-4444", descripcion="EMPAQUE", cantidad=5, precio=900,
                 estado="reclamo_proveedor", promesa=hoy + timedelta(days=1)),
        ], asesor=inactivo)
        emb = D.embarque(db, MARK, "R1", [it[1], it[3]], "en_aduana")
        D.recepcion(db, emb, {it[3].id: (8, "faltante")})
        D.reclamo(db, it[3], motivo="faltante", cantidad=2)
        D.reclamo(db, it[4], motivo="no_llego", cantidad=5)
        D.despacho(db, MARK, "R1a", oc, [(it[2], 2), (it[3], 6)], numero_guia="7001")
        D.despacho(db, MARK, "R1b", oc, [(it[3], 2)], estado="en_preparacion", numero_guia="7002")
        ids.update(r1=oc.id, r1_items=[i.id for i in it])

        # R2 — compra nacional: llega por tandas, sin reclamo. Y tres filas 'en_bodega' que
        # NO deben acotarse: sin recepción, con recepción abierta, y despachada de más.
        _, oc, it = D.venta(db, MARK, "R2", fa, [
            dict(numero_parte="2N-0001", cantidad=10, estado="en_bodega"),
            dict(numero_parte="2N-0002", cantidad=5, estado="en_bodega"),
            dict(numero_parte="2N-0003", cantidad=5, estado="en_bodega"),
            dict(numero_parte="2N-0004", cantidad=5, estado="en_bodega"),
            dict(numero_parte="2N-0005", cantidad=4, estado="embarcado"),
            dict(numero_parte="2N-0006", cantidad=4, estado="en_bodega"),   # llegaron 3; su reclamo se ANULÓ
            dict(numero_parte="2N-0007", cantidad=2, estado="comprado"),    # comprado en Chile, aún no llega
            dict(numero_parte="2N-0008", cantidad=6, estado="en_bodega"),   # 4 recibidas; 2 en una recepción ABIERTA
        ])
        recibido = D.embarque(db, MARK, "R2v", [it[7]], "en_bodega")
        D.recepcion(db, recibido, {it[7].id: (4, "completo")})
        recibiendose = D.embarque(db, MARK, "R2w", [it[7]], "en_recepcion")
        D.recepcion(db, recibiendose, {it[7].id: (2, "completo")}, estado="abierta")
        D.compra(db, MARK, "R2", [it[0], it[6]], origen="nacional", fecha=datetime(2026, 1, 5, 12, 0))
        con_faltante = D.embarque(db, MARK, "R2z", [it[5]], "en_bodega")
        D.recepcion(db, con_faltante, {it[5].id: (3, "faltante")})
        D.reclamo(db, it[5], estado="anulado", motivo="faltante", cantidad=1)
        D.recepcion_nacional(db, MARK, "R2a", {it[0].id: (4, "completo")}, fecha=date(2026, 2, 3))
        D.recepcion_nacional(db, MARK, "R2b", {it[2].id: (3, "completo")}, estado="abierta")
        D.despacho(db, MARK, "R2a", oc, [(it[3], 7)], numero_guia="7003")
        # El mismo ítem en dos embarques: manda el último.
        D.embarque(db, MARK, "R2x", [it[4]], "en_bodega")
        D.embarque(db, MARK, "R2y", [it[4]], "en_transito")
        ids.update(r2=oc.id, r2_items=[i.id for i in it])

        # R3 — el recorrido con sus fechas reales, y un ítem que NO llegó.
        _, oc, it = D.venta(db, MARK, "R3", fa, [
            dict(numero_parte="3L-0001", cantidad=2, estado="despachado"),
            dict(numero_parte="3L-0002", cantidad=5, estado="reclamo_proveedor"),
            dict(numero_parte="3L-0003", cantidad=1, estado="comprado"),
        ])
        oc.created_at = datetime(2026, 1, 3, 1, 30)  # 2 de enero, 22:30 en Chile
        D.compra(db, MARK, "R3", [it[0], it[1], it[2]], fecha=datetime(2026, 1, 5, 12, 0))
        D.pre_embarque(db, MARK, "R3", [it[0], it[1]], fecha=datetime(2026, 1, 20, 12, 0))
        emb = D.embarque(db, MARK, "R3", [it[0], it[1]], "en_bodega")
        D.recepcion(db, emb, {it[0].id: (2, "completo"), it[1].id: (0, "no_llego")},
                    fecha=datetime(2026, 2, 10, 12, 0))
        D.reclamo(db, it[1], motivo="no_llego", cantidad=5, fecha=datetime(2026, 2, 10, 13, 0))
        D.despacho(db, MARK, "R3a", oc, [(it[0], 1)], numero_guia="7011", fecha=datetime(2026, 2, 16, 1, 30))
        D.despacho(db, MARK, "R3b", oc, [(it[0], 1)], numero_guia="7012", fecha=datetime(2026, 2, 20, 15, 0))
        ids.update(r3=oc.id, r3_items=[i.id for i in it])

        # R4 — firma parcial: de 10 que viajaron el cliente firmó 8 (y la fecha
        # comprometida ya pasó). Otra guía conforme y otra todavía sin firmar.
        _, oc, it = D.venta(db, MARK, "R4", fa, [
            dict(numero_parte="4F-0001", cantidad=10, precio=100000, estado="despachado",
                 promesa=hoy - timedelta(days=3)),
            dict(numero_parte="4F-0002", cantidad=3, precio=1000, estado="despachado"),
            dict(numero_parte="4F-0003", cantidad=1, precio=1000, estado="despachado"),
        ])
        parcial = D.despacho(db, MARK, "R4a", oc, [(it[0], 10, 8)], numero_guia="7021", firmada=True,
                             fecha_firma=date(2026, 3, 5))
        conforme = D.despacho(db, MARK, "R4b", oc, [(it[1], 3)], numero_guia="7022", firmada=True)
        sin_firmar = D.despacho(db, MARK, "R4c", oc, [(it[2], 1, 0)], numero_guia="7023")
        ids.update(r4=oc.id, r4_items=[i.id for i in it], guia_parcial=parcial.id,
                   guia_conforme=conforme.id, guia_sin_firmar=sin_firmar.id)

        # R5 — 10 unidades en dos guías; solo la primera (6) está facturada.
        _, oc, it = D.venta(db, MARK, "R5", fa, [dict(numero_parte="5U-0001", cantidad=10, precio=1000,
                                                      estado="despachado")])
        D.despacho(db, MARK, "R5a", oc, [(it[0], 6)], numero_guia="7031")
        D.despacho(db, MARK, "R5b", oc, [(it[0], 4)], numero_guia="7032")
        D.factura(db, MARK, "R5", oc, [(it[0], 6, 1000)], fecha=date(2026, 4, 12))
        ids.update(r5=oc.id, r5_items=[i.id for i in it])

        # R6 — una venta con DOS filas oc_cliente; la guía quedó colgada de la segunda.
        cot, oc, it = D.venta(db, MARK, "R6", fa, [dict(numero_parte="6D-0001", cantidad=1, precio=7000,
                                                        estado="despachado")])
        segunda = OcCliente(cotizacion_id=cot.id, numero_oc=f"{MARK}-OC-R6-REPETIDA")
        db.add(segunda)
        db.flush()
        guia = D.despacho(db, MARK, "R6a", segunda, [(it[0], 1)], numero_guia="7041", firmada=True,
                          archivo_firmado=D.archivo_de_guia_firmada(41))
        ids.update(r6=oc.id, r6_segunda=segunda.id, r6_guia=guia.id)

        # R7 — fechas: renglón partido con una fila ya entregada (promesa pasada) y otra
        # pendiente (promesa futura); un ítem comprometido para HOY; uno sin fecha alguna.
        _, oc, it = D.venta(db, MARK, "R7", fa, [
            dict(item_num=1, numero_parte="7H-0001", descripcion="ANILLO", cantidad=1, estado="despachado",
                 promesa=hoy - timedelta(days=5)),
            dict(item_num=1, numero_parte="7H-0001", descripcion="ANILLO", cantidad=1, estado="comprado",
                 promesa=hoy + timedelta(days=5)),
            dict(numero_parte="7H-0002", cantidad=1, estado="comprado", promesa=hoy),
            dict(numero_parte="7H-0003", cantidad=1, estado="comprado"),
        ])
        ids.update(r7=oc.id, r7_items=[i.id for i in it])

        # R9 — todo lo que se espera ya venció.
        _, oc, _ = D.venta(db, MARK, "R9", fa, [
            dict(numero_parte="9V-0001", estado="comprado", promesa=hoy - timedelta(days=4)),
            dict(numero_parte="9V-0002", estado="comprado", promesa=hoy - timedelta(days=9)),
        ])
        ids.update(r9=oc.id)

        # R10 — despachada completa en una guía que NADIE ha firmado (aunque la línea ya
        # traiga una cantidad firmada en 0: sin firma, ese dato todavía no dice nada).
        _, oc, it = D.venta(db, MARK, "R10", fa, [dict(numero_parte="10S-0001", cantidad=2, estado="despachado")])
        D.despacho(db, MARK, "R10a", oc, [(it[0], 2, 0)], numero_guia="7051")
        ids.update(r10=oc.id)

        # R11 — venta anterior al congelado de fechas: el ítem no tiene fecha propia y vale
        # la fecha de entrega de la orden.
        _, oc, _ = D.venta(db, MARK, "R11", fa, [dict(numero_parte="11E-0001", estado="comprado")],
                           fecha_entrega=hoy + timedelta(days=7))
        ids.update(r11=oc.id)
        db.commit()
        return ids
    finally:
        db.close()


# ── §A y §B ─────────────────────────────────────────────────────────────────────────────

def _paso_a_un_renglon_es_un_renglon(cab, ids):
    orden = _orden(cab, ids["r1"])
    por_parte = _por_parte(orden)
    check("§A la orden muestra 4 ítems (el split de dos filas es UN ítem)", len(orden["items"]) == 4,
          [l["numero_parte"] for l in orden["items"]])
    filtro = por_parte["1R-0716"]
    check("§A el ítem partido suma su cantidad y dice cuánto hay en cada estado",
          filtro["cantidad"] == 7 and _partes(filtro) == {"en_compra": 3, "en_aduana": 4}, (filtro["cantidad"], _partes(filtro)))
    check("§A su estado es el de la parte que va más atrás", filtro["estado"]["clave"] == "en_compra", filtro["estado"])
    check("§A su id es el menor de sus filas (la URL no cambia si se vuelve a partir)",
          filtro["id"] == min(ids["r1_items"][0], ids["r1_items"][1]), filtro["id"])
    otra_fila = _item(cab, ids["r1"], ids["r1_items"][1])["item"]
    check("§A pedir el seguimiento por la OTRA fila del split lleva al mismo ítem",
          otra_fila["id"] == filtro["id"] and otra_fila["cantidad"] == 7, otra_fila)
    check("§A cada ítem trae su posición en la orden de compra",
          {l["numero_parte"]: l["linea"] for l in orden["items"]}
          == {"1R-0716": 1, "7T-1997": 2, "5P-0001": 3, "9X-4444": 4})
    check("§A el total de un ítem partido es unitario × cantidad completa (33.333,5 × 7 → 233.335)",
          filtro["monto"] == {"unitario_clp": 33333.5, "total_clp": precios.total_de_linea(33333.5, 7)}
          and filtro["monto"]["total_clp"] == 233335, filtro["monto"])
    check("§A primero lo pendiente y lo más atrasado arriba; lo entregado al final",
          [l["numero_parte"] for l in orden["items"]] == ["5P-0001", "9X-4444", "1R-0716", "7T-1997"],
          [l["numero_parte"] for l in orden["items"]])
    check("§A un asesor que ya no está activo no se publica como ejecutivo", orden["orden"]["ejecutivo"] is None)
    check("§A el avance promedia la fracción entregada de cada ítem (0 + 1 + 0,6 + 0 de 4 → 40 %)",
          orden["orden"]["avance_pct"] == 40
          and orden["orden"]["items"] == {"total": 4, "entregados": 1, "por_entregar": 3}, orden["orden"])


def _paso_b_unidades(cab, ids):
    perno = _por_parte(_orden(cab, ids["r1"]))["5P-0001"]
    check("§B llegada y despacho parciales por embarque: 6 despachadas, 2 en bodega, 2 en gestión",
          _partes(perno) == {"despachado": 6, "en_bodega": 2, "en_gestion": 2}, _partes(perno))
    check("§B …las partes vienen en el orden del recorrido, con «en gestión» primero",
          [p["estado"]["clave"] for p in perno["partes"]] == ["en_gestion", "en_bodega", "despachado"])
    check("§B …el despacho en preparación no cuenta como salido, y el ítem sigue pendiente: 6 entregadas, 4 por entregar",
          perno["cantidad_entregada"] == 6 and perno["cantidad_por_entregar"] == 4 and not perno["entregado"], perno)
    check("§B …y su estado principal es la incidencia", perno["estado"]["clave"] == "en_gestion", perno["estado"])

    r2 = _por_parte(_orden(cab, ids["r2"]))
    nacional = r2["2N-0001"]
    check("§B compra nacional, llegaron 4 de 10 y NO hay reclamo: 4 en bodega y 6 en proceso de compra",
          _partes(nacional) == {"en_bodega": 4, "en_compra": 6} and nacional["estado"]["clave"] == "en_compra",
          (_partes(nacional), nacional["estado"]))
    check("§B una fila en bodega SIN registro de recepción no se acota: está toda en bodega",
          r2["2N-0002"]["estado"]["clave"] == "en_bodega" and r2["2N-0002"]["partes"] == [], r2["2N-0002"])
    check("§B una recepción todavía ABIERTA no mueve nada", r2["2N-0003"]["estado"]["clave"] == "en_bodega"
          and r2["2N-0003"]["partes"] == [], r2["2N-0003"])
    check("§B un despacho por más unidades que la fila no entrega más que la fila",
          r2["2N-0004"]["cantidad_entregada"] == 5 and r2["2N-0004"]["entregado"], r2["2N-0004"])
    check("§B un ítem en dos embarques muestra el estado del ÚLTIMO",
          r2["2N-0005"]["estado"]["clave"] == "en_transito", r2["2N-0005"]["estado"])
    check("§B lo contado en una recepción de embarque todavía ABIERTA no está en bodega: 4 en bodega, 2 por llegar",
          _partes(r2["2N-0008"]) == {"en_bodega": 4, "en_compra": 2}, _partes(r2["2N-0008"]))
    check("§B un reclamo ANULADO no deja el ítem «en gestión»: lo que falta está en proceso de compra",
          _partes(r2["2N-0006"]) == {"en_bodega": 3, "en_compra": 1}, _partes(r2["2N-0006"]))
    tablero = client.get("/api/portal/resumen", headers=cab).json()
    por_estado = {p["clave"]: p["items"] for p in tablero["pendientes_por_estado"]}
    check("§B el tablero cuenta «en gestión» solo lo que tiene un reclamo (no la entrega nacional por tandas "
          "ni lo que el cliente firmó como no recibido, que va aparte)",
          por_estado.get("en_gestion") == 3 and por_estado.get("no_recibido") == 1, por_estado)  # perno, empaque y 3L-0002
    check("§B …y las incidencias van primero en esa lista",
          [p["clave"] for p in tablero["pendientes_por_estado"]][:2] == ["en_gestion", "no_recibido"],
          tablero["pendientes_por_estado"])

    db = SessionLocal()
    try:
        todos = ids["r1_items"] + ids["r2_items"] + ids["r3_items"]
        oc_ids = [ids["r1"], ids["r2"], ids["r3"]]
        ocs = db.query(OcCliente).filter(OcCliente.id.in_(oc_ids)).all()
        cot_ids = [oc.cotizacion_id for oc in ocs]
        from models.models import ItemCotizacion

        def de_estas_ventas(consulta, columna):
            return (consulta.join(ItemCotizacion, ItemCotizacion.id == columna)
                    .filter(ItemCotizacion.cotizacion_id.in_(cot_ids)))
        del_portal = renglones._recibido_en_bodega(db, de_estas_ventas)
        del_erp = {k: float(v) for k, v in despachos_del_erp._qty_recibida_utilizable(db, todos).items()}
    finally:
        db.close()
    check("§B «recibido en bodega» del portal = el tope físico de Despachos, ítem por ítem",
          del_portal == del_erp and len(del_erp) >= 3
          and renglones.RECEPCION_UTILIZABLE == despachos_del_erp._RECEPCION_UTILIZABLE, (del_portal, del_erp))

    # La segunda entrega nacional completa el ítem: deja de haber unidades en compra.
    db = SessionLocal()
    try:
        D.recepcion_nacional(db, MARK, "R2c", {ids["r2_items"][0]: (6, "completo")})
        db.commit()
    finally:
        db.close()
    nacional = _por_parte(_orden(cab, ids["r2"]))["2N-0001"]
    check("§B con la segunda entrega nacional el ítem queda entero en bodega",
          nacional["estado"]["clave"] == "en_bodega" and nacional["partes"] == [], nacional)


# ── §C ──────────────────────────────────────────────────────────────────────────────────

def _paso_c_firma_parcial(cab, ids):
    orden = _orden(cab, ids["r4"])
    por_parte = _por_parte(orden)
    parcial = por_parte["4F-0001"]
    check("§C de 10 que viajaron el cliente firmó 8: 8 despachadas y 2 «no recibido en la entrega»",
          _partes(parcial) == {"despachado": 8, "no_recibido": 2} and parcial["cantidad_entregada"] == 8
          and not parcial["entregado"], (_partes(parcial), parcial["cantidad_entregada"]))
    check("§C …el ítem dice el hecho y no promete nada: ni «en gestión» ni la palabra «reposición»",
          parcial["estado"] == {"clave": "no_recibido", "etiqueta": "No recibido en la entrega"}
          and "reposici" not in json.dumps(orden, ensure_ascii=False).lower(), parcial["estado"])
    check("§C …no queda «atrasado» aunque su fecha comprometida ya pasó: de él no se espera nada más",
          parcial["vencida"] is False and parcial["fecha_prometida"] == (hoy_chile() - timedelta(days=3)).isoformat()
          and orden["orden"]["atrasados"] == 0, (parcial["vencida"], orden["orden"]["atrasados"]))
    check("§C …la orden NO figura entregada (2 de 3 ítems), pero de ninguno se espera nada más: "
          "ya no tiene una fecha comprometida corriendo",
          orden["orden"]["estado"] == "abierta"
          and orden["orden"]["items"] == {"total": 3, "entregados": 2, "por_entregar": 0}
          and orden["orden"]["proxima_fecha"] is None and orden["orden"]["fecha_completa"] is None, orden["orden"])
    interno = _renglones_internos(ids["r4"], ids)["4F-0001"]
    check("§C …y de esas 2 unidades no hay nada «por entregar» (ni en unidades ni en pesos)",
          parcial["cantidad_por_entregar"] == 0 and interno.por_entregar_clp == 0 and interno.total_clp == 1000000,
          (parcial["cantidad_por_entregar"], interno.por_entregar_clp, interno.total_clp))

    seguimiento = _item(cab, ids["r4"], ids["r4_items"][0])
    ultimo = seguimiento["hitos"][-1]
    check("§C el recorrido termina en «No recibido en la entrega», con el día de la firma y la guía",
          ultimo == {"clave": "no_recibido", "etiqueta": "No recibido en la entrega", "cumplido": True,
                     "actual": True, "fecha": "2026-03-05", "detalle": "Guía 7021"}, ultimo)
    hitos = _hitos(seguimiento)
    check("§C …conserva que sí se despachó, y no agrega una gestión que nadie está haciendo",
          hitos["despachado"]["cumplido"] and not hitos["despachado"]["actual"] and "en_gestion" not in hitos,
          list(hitos))

    guias = {g["id"]: g for g in orden["guias"]}
    check("§C la guía dice «con faltante», lo que se recibió y el DÍA que se firmó (no el anterior)",
          guias[ids["guia_parcial"]]["recepcion"] == "con_faltante"
          and guias[ids["guia_parcial"]]["items"][0]["cantidad_recibida"] == 8
          and guias[ids["guia_parcial"]]["fecha_recepcion"] == "2026-03-05", guias[ids["guia_parcial"]])
    check("§C una guía firmada sin faltante dice «conforme» y recibido = lo que viajó",
          guias[ids["guia_conforme"]]["recepcion"] == "conforme"
          and guias[ids["guia_conforme"]]["items"][0]["cantidad_recibida"] == 3
          and guias[ids["guia_conforme"]]["fecha_recepcion"] == hoy_chile().isoformat(), guias[ids["guia_conforme"]])
    check("§C una cantidad firmada en una guía que todavía NO está firmada no cuenta: el ítem figura despachado",
          por_parte["4F-0003"]["estado"]["clave"] == "despachado" and por_parte["4F-0003"]["entregado"]
          and por_parte["4F-0003"]["partes"] == [], por_parte["4F-0003"])
    check("§C una guía SIN firmar dice «pendiente» y no afirma cuánto se recibió",
          guias[ids["guia_sin_firmar"]]["recepcion"] == "pendiente"
          and guias[ids["guia_sin_firmar"]]["items"][0]["cantidad_recibida"] is None
          and guias[ids["guia_sin_firmar"]]["fecha_recepcion"] is None, guias[ids["guia_sin_firmar"]])
    abiertas = client.get("/api/portal/ordenes", headers=cab, params={"estado": "abiertas", "page_size": 50}).json()["items"]
    entregadas = client.get("/api/portal/ordenes", headers=cab, params={"estado": "entregadas", "page_size": 50}).json()["items"]
    check("§C la orden con faltante firmado sale en «abiertas» (no en «entregadas»), que es lo que dice su insignia",
          ids["r4"] in {o["id"] for o in abiertas} and ids["r4"] not in {o["id"] for o in entregadas}
          and next(o for o in abiertas if o["id"] == ids["r4"])["estado"] == "abierta")
    check("§C la orden despachada completa en una guía sin firmar sale en «entregadas», que es lo que dice su insignia",
          ids["r10"] in {o["id"] for o in entregadas} and ids["r10"] not in {o["id"] for o in abiertas}
          and next(o for o in entregadas if o["id"] == ids["r10"])["estado"] == "entregada")
    check("§C en todo el listado, la insignia de cada orden coincide con el filtro que la trae",
          all(o["estado"] == "abierta" for o in abiertas) and all(o["estado"] == "entregada" for o in entregadas),
          [(o["id"], o["estado"]) for o in abiertas + entregadas])


# ── §D ──────────────────────────────────────────────────────────────────────────────────

def _paso_d_facturado_por_unidades(cab, ids):
    orden = _orden(cab, ids["r5"])
    linea = _por_parte(orden)["5U-0001"]
    check("§D salieron 10 en dos guías y solo 6 están facturadas: 6 «facturado» y 4 «despachado»",
          _partes(linea) == {"facturado": 6, "despachado": 4} and linea["entregado"], _partes(linea))
    hitos = _hitos(_item(cab, ids["r5"], ids["r5_items"][0]))
    check("§D …y el paso «facturado» del recorrido no se da por cumplido para todo el ítem",
          hitos["despachado"]["actual"] and not hitos["facturado"]["cumplido"] and hitos["facturado"]["fecha"] is None,
          hitos.get("facturado"))


# ── §E ──────────────────────────────────────────────────────────────────────────────────

def _paso_e_fechas(cab, ids):
    hoy = hoy_chile()
    r1 = _orden(cab, ids["r1"])
    por_parte = _por_parte(r1)
    check("§E la fecha es la comprometida (congelada en el ítem)",
          por_parte["1R-0716"]["fecha_prometida"] == (hoy + timedelta(days=10)).isoformat()
          and por_parte["5P-0001"]["fecha_prometida"] == (hoy - timedelta(days=2)).isoformat())
    check("§E «vencida»: pendiente con la fecha pasada sí; entregado con la fecha pasada no",
          por_parte["5P-0001"]["vencida"] is True and por_parte["7T-1997"]["vencida"] is False
          and por_parte["1R-0716"]["vencida"] is False)
    check("§E la cabecera: 1 atrasado; la fecha de la orden es la MÁS ANTIGUA entre lo que se espera (la vencida, "
          "no la próxima por vencer); la última, cuándo queda completa",
          r1["orden"]["atrasados"] == 1 and r1["orden"]["proxima_fecha"] == (hoy - timedelta(days=2)).isoformat()
          and r1["orden"]["fecha_completa"] == (hoy + timedelta(days=10)).isoformat(), r1["orden"])
    r9 = _orden(cab, ids["r9"])["orden"]
    check("§E con TODO lo que se espera vencido, la orden sigue teniendo fecha (la más antigua), no «sin fecha»",
          r9["atrasados"] == 2 and r9["proxima_fecha"] == (hoy - timedelta(days=9)).isoformat()
          and r9["fecha_completa"] == (hoy - timedelta(days=4)).isoformat(), r9)

    r7 = _por_parte(_orden(cab, ids["r7"]))
    check("§E renglón con una fila entregada (promesa pasada) y otra pendiente: manda la de lo pendiente",
          r7["7H-0001"]["fecha_prometida"] == (hoy + timedelta(days=5)).isoformat() and r7["7H-0001"]["vencida"] is False,
          r7["7H-0001"])
    check("§E comprometido para HOY todavía no está vencido", r7["7H-0002"]["vencida"] is False
          and r7["7H-0002"]["fecha_prometida"] == hoy.isoformat(), r7["7H-0002"])
    check("§E sin fecha en el ítem ni en la orden: no hay fecha y no está vencido",
          r7["7H-0003"]["fecha_prometida"] is None and r7["7H-0003"]["vencida"] is False, r7["7H-0003"])
    r11 = _orden(cab, ids["r11"])
    check("§E sin fecha en el ítem pero con fecha de entrega en la orden: vale la de la orden",
          r11["items"][0]["fecha_prometida"] == (hoy + timedelta(days=7)).isoformat()
          and r11["orden"]["proxima_fecha"] == (hoy + timedelta(days=7)).isoformat(), r11["items"][0])

    r3 = _orden(cab, ids["r3"])
    check("§E el día de ingreso es el de Chile (un cierre a las 22:30 no sale fechado al día siguiente)",
          r3["orden"]["fecha_ingreso"] == "2026-01-02", r3["orden"]["fecha_ingreso"])
    salidas = sorted(g["despachada_el"] for g in r3["guias"])
    check("§E el día de salida de una guía también es el de Chile", salidas == ["2026-02-15", "2026-02-20"], salidas)


# ── §F ──────────────────────────────────────────────────────────────────────────────────

def _paso_f_recorrido(cab, ids):
    seguimiento = _item(cab, ids["r3"], ids["r3_items"][0])
    hitos = _hitos(seguimiento)
    check("§F cada paso cumplido lleva el día en que ocurrió",
          {c: h["fecha"] for c, h in hitos.items()} == {
              "confirmado": "2026-01-02", "en_compra": "2026-01-05", "listo_origen": "2026-01-20",
              "en_transito": None, "en_aduana": None, "en_recepcion": None,
              "en_bodega": "2026-02-10", "despachado": "2026-02-15"},
          {c: h["fecha"] for c, h in hitos.items()})
    check("§F «despachado» nombra la PRIMERA guía del ítem, y esa es su fecha",
          hitos["despachado"]["detalle"] == "Guía 7011" and hitos["despachado"]["actual"], hitos["despachado"])
    check("§F el seguimiento trae SOLO las guías que llevan ese ítem",
          sorted(g["folio"] for g in seguimiento["guias"]) == ["7011", "7012"], seguimiento["guias"])

    no_llego = _item(cab, ids["r3"], ids["r3_items"][1])
    hitos = _hitos(no_llego)
    check("§F un ítem que NO llegó en una recepción cerrada no marca «En nuestra bodega»",
          not hitos["en_bodega"]["cumplido"] and hitos["en_bodega"]["fecha"] is None, hitos["en_bodega"])
    check("§F …su recorrido llega hasta donde llegó de verdad y termina en la incidencia, con su fecha",
          hitos["listo_origen"]["cumplido"] and no_llego["hitos"][-1]["clave"] == "en_gestion"
          and no_llego["hitos"][-1]["actual"] and no_llego["hitos"][-1]["fecha"] == "2026-02-10",
          no_llego["hitos"][-1])
    check("§F …y no trae guías (no viajó en ninguna)", no_llego["guias"] == [])

    sin_embarcar = _hitos(_item(cab, ids["r3"], ids["r3_items"][2]))
    check("§F un ítem comprado fuera de Chile que aún no se embarca ya muestra el recorrido completo",
          list(sin_embarcar) == ["confirmado", "en_compra", "listo_origen", "en_transito", "en_aduana",
                                 "en_recepcion", "en_bodega", "despachado"], list(sin_embarcar))
    nacional = _hitos(_item(cab, ids["r2"], ids["r2_items"][0]))
    check("§F lo comprado en Chile muestra el recorrido corto, sin tránsito ni aduana",
          list(nacional) == ["confirmado", "en_compra", "en_bodega", "despachado"], list(nacional))
    nacional_en_camino = _hitos(_item(cab, ids["r2"], ids["r2_items"][6]))
    check("§F …también antes de llegar: lo comprado en Chile no va a pasar por aduana",
          list(nacional_en_camino) == ["confirmado", "en_compra", "en_bodega", "despachado"]
          and nacional_en_camino["en_compra"]["actual"], list(nacional_en_camino))
    check("§F …y sus pasos de embarque van como futuros: sin cumplir y sin fecha",
          not any(sin_embarcar[c]["cumplido"] or sin_embarcar[c]["fecha"]
                  for c in ("listo_origen", "en_transito", "en_aduana", "en_recepcion")), sin_embarcar)
    sin_asignar = _hitos(_item(cab, ids["r7"], ids["r7_items"][3]))
    check("§F un ítem todavía sin compra asignada muestra el recorrido completo (no se sabe que sea nacional)",
          "en_transito" in sin_asignar and "en_aduana" in sin_asignar, list(sin_asignar))
    en_bodega_sin_embarque = _hitos(_item(cab, ids["r2"], ids["r2_items"][1]))
    check("§F un ítem que YA está en bodega sin haber pasado por un embarque no afirma tránsito ni aduana",
          list(en_bodega_sin_embarque) == ["confirmado", "en_compra", "en_bodega", "despachado"],
          list(en_bodega_sin_embarque))
    despachado_sin_embarque = _hitos(_item(cab, ids["r4"], ids["r4_items"][1]))
    check("§F …ni uno ya despachado: recorrido corto, con todo cumplido",
          list(despachado_sin_embarque) == ["confirmado", "en_compra", "en_bodega", "despachado"]
          and all(h["cumplido"] for h in despachado_sin_embarque.values()), list(despachado_sin_embarque))
    con_embarque = _hitos(_item(cab, ids["r1"], ids["r1_items"][3]))
    check("§F un ítem que SÍ pasó por un embarque conserva el recorrido completo aunque ya esté en bodega",
          "en_aduana" in con_embarque and con_embarque["en_aduana"]["cumplido"], list(con_embarque))


# ── §H ──────────────────────────────────────────────────────────────────────────────────

def _paso_h_una_venta_una_orden(cab, ids):
    listado = client.get("/api/portal/ordenes", headers=cab, params={"page_size": 50}).json()
    propias = [o["id"] for o in listado["items"]]
    check("§H la venta con dos filas oc_cliente aparece UNA vez, con la primera",
          ids["r6"] in propias and ids["r6_segunda"] not in propias and len(propias) == len(set(propias)) == listado["total"],
          propias)
    check("§H la fila repetida no existe como orden → 404",
          client.get(f"/api/portal/ordenes/{ids['r6_segunda']}", headers=cab).status_code == 404)
    orden = _orden(cab, ids["r6"])
    check("§H la guía que quedó colgada de la fila repetida se ve en la orden, apuntando a ella",
          [(g["id"], g["orden_id"]) for g in orden["guias"]] == [(ids["r6_guia"], ids["r6"])], orden["guias"])
    check("§H …se encuentra por su folio", ids["r6"] in {o["id"] for o in client.get(
        "/api/portal/ordenes", headers=cab, params={"q": "7041"}).json()["items"]})
    check("§H …y su copia firmada se puede bajar",
          client.get(f"/api/portal/guias/{ids['r6_guia']}/firmada", headers=cab).status_code == 200)
    tablero = client.get("/api/portal/resumen", headers=cab).json()
    check("§H el tablero cuenta esa venta una sola vez", tablero["ordenes"]["total"] == listado["total"], tablero["ordenes"])


def run():
    del _fails[:]
    D.limpiar(MARK)
    try:
        with D.carpetas_de_prueba():
            ids = _sembrar()
            cab = _cab(ids["email_a"])
            _paso_a_un_renglon_es_un_renglon(cab, ids)
            _paso_c_firma_parcial(cab, ids)
            _paso_d_facturado_por_unidades(cab, ids)
            _paso_e_fechas(cab, ids)
            _paso_f_recorrido(cab, ids)
            _paso_h_una_venta_una_orden(cab, ids)
            _paso_b_unidades(cab, ids)  # al final: agrega una recepción a la venta R2
        print()
        print(f"FALLARON {len(_fails)}: {_fails}" if _fails else "SUITE VERDE: ítems del portal")
        assert not _fails, _fails
    finally:
        D.limpiar(MARK)


def test_renglones_portal():
    run()


if __name__ == "__main__":
    run()
