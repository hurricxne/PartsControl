"""PEDIDOS del Portal de Clientes — lo que ve el cliente de sus órdenes, con la app
completa, cuentas reales y sesiones reales. (Cómo se arma cada ítem —unidades, estados,
fechas— está en test_renglones.py.)

LO QUE ESTA SUITE PROTEGE (/api/portal/resumen, /ordenes, /ordenes/{id}, …/items/{id})
  §A  CADA CLIENTE VE LO SUYO: la orden, el ítem, la foto y los documentos de OTRO
      cliente responden 404 (no 403: un 403 confirmaría que el id existe). Una venta sin
      ficha no la ve nadie. Y el filtro del cliente se escribe en UN solo archivo
      (portal/candado.py): se comprueba leyendo el código.
  §B  NO SALE NADA INTERNO: cada respuesta trae EXACTAMENTE los campos del contrato (lista
      blanca), y ningún valor interno sembrado —costo, margen, proveedor, país, forwarder,
      AWB, transportista, N° de cotización, RUT— aparece en ninguna.
  §C  MONTOS = PRECIO DE VENTA, IGUAL AL DE LA FACTURA: en una venta de origen 'costo' el
      portal muestra exactamente lo que calcula Contabilidad (routers/contabilidad.py),
      y no el costo. Una venta sin precio congelado no muestra montos. El perfil
      'bodega' no recibe NINGÚN monto.
  §D  UNA CUENTA CON VARIAS EMPRESAS ve las de todas, cada orden dice de cuál es, y opera
      con el perfil más restrictivo de sus fichas.
  §E  LISTADO: filtros, año, paginación sin repetidos, parámetros fuera de rango.
  §F  CLAVE TEMPORAL: mientras la cuenta no la reemplace, las rutas de datos responden
      403; y el token de quien entró con la temporal deja de valer cuando se cambia.
  §G  EL TABLERO SUMA LO MISMO que se ve al recorrer las órdenes una por una; sus tres
      montos salen del mismo conjunto de órdenes; avisa cuando no las muestra todas.
  §H  TOPE DE FRECUENCIA: una cuenta en bucle recibe 429; las demás no se enteran.

MUTACIONES verificadas al construir la suite (quitar → rojo → restaurar → verde):
  el filtro de candado.de_sus_ordenes y el de anios_con_ordenes (→ §A), el ítem validado
  contra la orden (→ §A), un campo agregado a la respuesta y el precio tomado de la
  columna de costo (→ §B), la condición de precio congelado y el corte de montos por
  perfil (→ §C), el perfil «más restrictivo» (→ §D), la puerta de la clave temporal y la
  huella de la clave en el token (→ §F), el universo de «facturado», el tope del tablero
  y su filtro por año (→ §G), y los tres cupos (→ §H).

Corre con:  ./venv/bin/python -m pytest portal/tests/test_ordenes.py -q
(también:   ./venv/bin/python portal/tests/test_ordenes.py)
"""
import ast
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi.testclient import TestClient  # noqa: E402

from config import settings  # noqa: E402
from database import SessionLocal  # noqa: E402
from main import app  # noqa: E402
from portal import cupo, precios, resumen  # noqa: E402
from portal.deps import DETALLE_DEBE_CAMBIAR_CLAVE  # noqa: E402
from portal.models import PortalAcceso, UsuarioCliente  # noqa: E402
from portal.tests import _datos as D  # noqa: E402
from portal.tiempo import hoy_chile  # noqa: E402
from routers.contabilidad import _iva_clp, _precios_de_cotizacion, _total_linea  # noqa: E402

MARK = "ZZPORTALORD"  # SIN guiones bajos: en LIKE el _ es comodín
PORTAL = Path(__file__).resolve().parent.parent

client = TestClient(app)
_fails: list = []

FOTO_DE_PRECIOS = json.dumps({
    "tipo_cambio_usd": 950.0, "costo_shipping_usd_kg": 4.0, "adicionales_shipping_usd": 300.0,
    "costo_agencia_pct": 0.01, "costo_agencia_minimo_clp": 150000.0,
    "desconsolidado_clp": 80000.0, "bodegaje_clp": 70000.0, "margen_venta_pct": 0.25,
})
# Costos en USD con cifras reconocibles: si alguna apareciera en una respuesta, se nota.
COSTO_USD_1, COSTO_USD_2 = 1234.56, 777.77
# Valores internos sembrados con texto reconocible: ninguno puede salir hacia el cliente.
INTERNOS = {
    "el proveedor": "ZZPROVEEDORSECRETO", "el país de origen": "ZZPAISORIGEN",
    "el forwarder": "ZZFORWARDER", "el N° de AWB": "ZZAWB123", "las notas del embarque": "ZZNOTAEMBARQUE",
    "el transportista": "ZZCOURIER", "el N° de expedición": "ZZEXPEDICION",
    "la dirección de entrega": "ZZDIRECCION", "las observaciones del despacho": "ZZOBSERVACION",
}

# EL CONTRATO: los campos que trae cada pieza de una respuesta. Un campo de más es una fuga
# en potencia; uno de menos rompe la pantalla (frontend-src/src/portal/types.ts).
CAMPOS = {
    "sello": {"actualizado_at", "hoy"},
    "cabecera": {"id", "numero_oc", "empresa", "fecha_oc", "fecha_ingreso", "estado", "items",
                 "avance_pct", "proxima_fecha", "fecha_completa", "atrasados", "totales"},
    "item": {"id", "linea", "numero_parte", "descripcion", "marca", "cantidad", "estado", "entregado",
             "partes", "fecha_prometida", "vencida", "cantidad_entregada", "cantidad_por_entregar", "monto", "fotos"},
    "guia": {"id", "orden_id", "folio", "fecha", "despachada_el", "recepcion", "fecha_recepcion",
             "items", "pdf", "firmada"},
    "linea_de_guia": {"numero_parte", "descripcion", "cantidad", "cantidad_recibida"},
    "factura": {"id", "orden_id", "folio", "fecha", "vencimiento", "neto_clp", "iva_clp", "total_clp",
                "es_anticipo", "pdf"},
    "hito": {"clave", "etiqueta", "cumplido", "actual", "fecha", "detalle"},
    "foto": {"id", "ancho", "alto", "fecha"},
    "tablero": {"anio", "anios", "truncado", "ordenes", "items", "pendientes_por_estado",
                "proxima_fecha", "montos"},
    "montos": {"comprado_neto_clp", "por_entregar_neto_clp", "facturado_neto_clp", "ordenes_sin_monto"},
}


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _login(email: str, clave: str = D.CLAVE):
    return client.post("/api/auth/login", json={"email": email, "password": clave})


def _cab(email: str, clave: str = D.CLAVE) -> dict:
    r = _login(email, clave)
    assert r.status_code == 200, f"login de {email}: {r.status_code} {r.text[:120]}"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _get(ruta: str, cab: dict, **params):
    return client.get(f"/api/portal{ruta}", headers=cab, params=params or None)


def _cifras(dato, ruta="") -> list:
    """Dónde hay un monto con valor en un JSON: un bloque monto/totales/montos que no es
    null, o cualquier campo en pesos (`*_clp`)."""
    halladas = []
    if isinstance(dato, dict):
        for k, v in dato.items():
            if v is not None and (k in ("monto", "totales", "montos") or k.endswith("_clp")):
                halladas.append(f"{ruta}.{k}")
            halladas += _cifras(v, f"{ruta}.{k}")
    elif isinstance(dato, list):
        for n, v in enumerate(dato):
            halladas += _cifras(v, f"{ruta}[{n}]")
    return halladas


# ── Siembra ─────────────────────────────────────────────────────────────────────────────

def _sembrar() -> dict:
    hoy = hoy_chile()
    db = SessionLocal()
    try:
        fa, fb = D.ficha(db, MARK, "A", 72000011), D.ficha(db, MARK, "B", 72000022)
        asesor = D.interno(db, MARK, "asesor")
        operador = D.interno(db, MARK, "op")
        pa = D.portal(db, MARK, "a", [fa.id])
        pb = D.portal(db, MARK, "b", [fb.id])
        bodega = D.portal(db, MARK, "bod", [fa.id], perfil="bodega")
        # Un contacto de las DOS empresas.
        dos = D.portal(db, MARK, "dos", [fa.id, fb.id])
        # Cuenta con la clave temporal sin reemplazar.
        temporal = D.portal(db, MARK, "tmp", [fa.id])
        db.query(PortalAcceso).filter(PortalAcceso.usuario_id == temporal.id).update(
            {"clave_temporal_desde": D.ahora()})

        # A1: venta en pesos con de todo: compra, embarque y despacho con datos internos
        # reconocibles, guía firmada, factura y foto.
        _, oc1, it1 = D.venta(db, MARK, "A1", fa, [
            dict(numero_parte="1R-0716", descripcion="FILTRO DE ACEITE", cantidad=7, precio=33333.5,
                 estado="embarcado", promesa=hoy + timedelta(days=10)),
            dict(numero_parte="7T-1997", descripcion="SELLO", cantidad=2, precio=50000,
                 estado="despachado", promesa=hoy - timedelta(days=30)),
            dict(numero_parte="5P-0001", descripcion="PERNO", cantidad=10, precio=1200,
                 estado="comprado", promesa=hoy - timedelta(days=2)),
        ], asesor=asesor, fecha_oc="10/06/2026")
        D.compra(db, MARK, "A1", it1, proveedor=INTERNOS["el proveedor"], pais=INTERNOS["el país de origen"])
        D.embarque(db, MARK, "A1", [it1[0]], "en_aduana", forwarder=INTERNOS["el forwarder"],
                   awb_numero=INTERNOS["el N° de AWB"], notas=INTERNOS["las notas del embarque"])
        d1 = D.despacho(db, MARK, "A1a", oc1, [(it1[1], 2)], numero_guia="7001", firmada=True,
                        archivo_firmado=D.archivo_de_guia_firmada(1),
                        transportista=INTERNOS["el transportista"],
                        numero_expedicion=INTERNOS["el N° de expedición"],
                        direccion_entrega=INTERNOS["la dirección de entrega"],
                        observaciones=INTERNOS["las observaciones del despacho"])
        D.factura(db, MARK, "A1", oc1, [(it1[1], 2, 50000)])
        foto = D.foto(db, it1[1])

        # A2: venta de origen 'costo' con la foto de precios congelada.
        cot2, oc2, it2 = D.venta(db, MARK, "A2", fa, [
            dict(numero_parte="2C-0002", cantidad=3, precio=COSTO_USD_1, peso=12.0),
            dict(numero_parte="2C-0003", cantidad=7, precio=COSTO_USD_2, peso=3.5, margen=0.3147),
        ], origen="costo", pricing_snapshot=FOTO_DE_PRECIOS, fecha_oc="no es una fecha",
            fecha_entrega=hoy + timedelta(days=20))
        # Abierta y ANTIGUA (2023): más vieja que la entregada de 2024. Así «abiertas
        # primero» no sale gratis por ordenar por fecha.
        oc2.created_at = datetime(2023, 8, 1, 15, 0)
        # A3: venta de origen 'costo' SIN foto (cerrada antes de que existiera el
        # congelado), ya despachada y FACTURADA: no entra en ninguna suma del tablero.
        _, oc3, it3 = D.venta(db, MARK, "A3", fa, [
            dict(numero_parte="3D-0003", cantidad=1, precio=500.0, estado="despachado"),
            dict(numero_parte="3D-0004", cantidad=1, precio=500.0),
        ], origen="costo")
        D.despacho(db, MARK, "A3a", oc3, [(it3[0], 1)], numero_guia="7003")
        D.factura(db, MARK, "A3", oc3, [(it3[0], 1, 480000)])
        # A4: venta entregada completa, ingresada en 2024.
        _, oc4, _ = D.venta(db, MARK, "A4", fa, [dict(numero_parte="4E-0004", cantidad=1, precio=1000,
                                                     estado="despachado")])
        oc4.created_at = datetime(2024, 5, 10, 15, 0)
        # B1: del otro cliente, ingresada en 2019. Y una venta sin ficha.
        _, ocb, itb = D.venta(db, MARK, "B1", fb, [dict(numero_parte="8B-8888", cantidad=1, precio=4000,
                                                       estado="despachado")])
        ocb.created_at = datetime(2019, 3, 1, 15, 0)
        db1 = D.despacho(db, MARK, "B1a", ocb, [(itb[0], 1)], numero_guia="9001")
        fotob = D.foto(db, itb[0])
        _, ocs, _ = D.venta(db, MARK, "S1", None, [dict(numero_parte="0S-0000")], rut="76.999.999-9")
        db.commit()
        return {
            "oc1": oc1.id, "oc2": oc2.id, "oc3": oc3.id, "oc4": oc4.id, "ocb": ocb.id, "ocs": ocs.id,
            "it1": [i.id for i in it1], "it2": [i.id for i in it2], "itb": itb[0].id,
            "cot2": cot2.id, "d1": d1.id, "db1": db1.id, "foto": foto.id, "fotob": fotob.id,
            "email_a": pa.email, "email_b": pb.email, "email_bod": bodega.email, "email_dos": dos.email,
            "email_tmp": temporal.email, "uid_tmp": temporal.id, "uid_dos": dos.id, "uid_a": pa.id,
            "email_op": operador.email, "asesor_email": asesor.email, "rut_a": fa.rut,
            "ficha_a": fa.id, "ficha_b": fb.id, "nombre_a": fa.nombre, "nombre_b": fb.nombre,
        }
    finally:
        db.close()


# ── §A ──────────────────────────────────────────────────────────────────────────────────

def _paso_a_cada_cliente_ve_lo_suyo(cab_a, cab_b, ids):
    propias = {o["id"] for o in _get("/ordenes", cab_a, page_size=50).json()["items"]}
    check("§A el cliente A lista SUS cuatro órdenes y ninguna otra",
          propias == {ids["oc1"], ids["oc2"], ids["oc3"], ids["oc4"]}, propias)
    de_b = {o["id"] for o in _get("/ordenes", cab_b, page_size=50).json()["items"]}
    check("§A el cliente B lista solo la suya", de_b == {ids["ocb"]}, de_b)

    ajenas = {
        "orden": f"/ordenes/{ids['ocb']}",
        "ítem": f"/ordenes/{ids['ocb']}/items/{ids['itb']}",
        "ítem ajeno bajo una orden propia": f"/ordenes/{ids['oc1']}/items/{ids['itb']}",
        "foto": f"/fotos/{ids['fotob']}",
        "PDF de guía": f"/guias/{ids['db1']}/pdf",
        "guía firmada": f"/guias/{ids['db1']}/firmada",
        "orden sin ficha": f"/ordenes/{ids['ocs']}",
    }
    for nombre, ruta in ajenas.items():
        r = _get(ruta, cab_a)
        check(f"§A {nombre} de otro cliente → 404 (no 403, no 200)", r.status_code == 404,
              f"{r.status_code} {r.text[:100]}")
    for nombre, ruta in (("orden", f"/ordenes/{ids['oc1']}"), ("foto", f"/fotos/{ids['foto']}"),
                         ("ítem", f"/ordenes/{ids['oc1']}/items/{ids['it1'][1]}")):
        check(f"§A la {nombre} propia responde 200", _get(ruta, cab_a).status_code == 200)
        check(f"§A esa misma {nombre}, pedida por B → 404", _get(ruta, cab_b).status_code == 404)

    ra, rb = _get("/resumen", cab_a).json(), _get("/resumen", cab_b).json()
    check("§A el tablero de cada uno cuenta solo sus órdenes",
          ra["ordenes"]["total"] == 4 and rb["ordenes"]["total"] == 1, (ra["ordenes"], rb["ordenes"]))
    anio = hoy_chile().year
    check("§A …y los años que ofrece son solo los de SUS órdenes (2019 es de B)",
          ra["anios"] == [anio, 2024, 2023] and rb["anios"] == [2019], (ra["anios"], rb["anios"]))


def _paso_a_el_candado_vive_en_un_solo_archivo():
    """Lee el código: `Cotizacion.cliente_id` (la columna que decide de quién es una
    orden) solo se nombra en candado.py. Si otro archivo del portal la usa, está
    escribiendo su propio filtro de cliente — y ese es el que un día se olvida."""
    usos = {}
    for archivo in sorted(PORTAL.glob("*.py")):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        cuantos = sum(
            1 for nodo in ast.walk(arbol)
            if isinstance(nodo, ast.Attribute) and nodo.attr == "cliente_id"
            and isinstance(nodo.value, ast.Name) and nodo.value.id == "Cotizacion")
        if cuantos:
            usos[archivo.name] = cuantos
    # Estos MANTIENEN el vínculo (lo escriben, o lo cuentan para el personal); no deciden
    # qué ve un cliente.
    permitidos = {"candado.py", "router_admin.py", "seed_piloto.py", "service.py"}
    fuera = {a: n for a, n in usos.items() if a not in permitidos}
    check("§A `Cotizacion.cliente_id` no se usa fuera de candado.py en lo que ve el cliente",
          not fuera and usos.get("candado.py") == 1, usos)


# ── §B ──────────────────────────────────────────────────────────────────────────────────

def _paso_b_no_sale_nada_interno(cab_a, ids):
    tablero = _get("/resumen", cab_a)
    listado = _get("/ordenes", cab_a, page_size=50)
    busqueda = _get("/ordenes", cab_a, q="7T-1997")
    orden = _get(f"/ordenes/{ids['oc1']}", cab_a)
    orden_costo = _get(f"/ordenes/{ids['oc2']}", cab_a)
    seguimiento = _get(f"/ordenes/{ids['oc1']}/items/{ids['it1'][1]}", cab_a)
    respuestas = [tablero, listado, busqueda, orden, orden_costo, seguimiento,
                  _get(f"/ordenes/{ids['oc1']}/items/{ids['it1'][0]}", cab_a),
                  _get(f"/ordenes/{ids['oc2']}/items/{ids['it2'][0]}", cab_a)]
    check("§B todas las rutas de datos responden 200", all(r.status_code == 200 for r in respuestas),
          [r.status_code for r in respuestas])

    t, l, o, s = tablero.json(), listado.json(), orden.json(), seguimiento.json()
    estructura = {
        "el tablero": (set(t), CAMPOS["tablero"] | CAMPOS["sello"]),
        "los montos del tablero": (set(t["montos"]), CAMPOS["montos"]),
        "el listado": (set(l), {"total", "page", "page_size", "items", "q_efectivo"} | CAMPOS["sello"]),
        "una orden del listado": (set(l["items"][0]), CAMPOS["cabecera"]),
        "una orden encontrada al buscar": (set(busqueda.json()["items"][0]), CAMPOS["cabecera"] | {"coincidencias"}),
        "el detalle": (set(o), {"orden", "items", "guias", "facturas"} | CAMPOS["sello"]),
        "la cabecera del detalle": (set(o["orden"]), CAMPOS["cabecera"] | {"ejecutivo"}),
        "los ítems de la cabecera": (set(o["orden"]["items"]), {"total", "entregados", "por_entregar"}),
        "los ítems del tablero": (set(t["items"]), {"total", "entregados", "pendientes", "por_entregar", "atrasados"}),
        "los totales": (set(o["orden"]["totales"]), {"neto_clp", "iva_clp", "total_clp"}),
        "el monto de un ítem": (set(o["items"][0]["monto"]), {"unitario_clp", "total_clp"}),
        "el estado de un ítem": (set(o["items"][0]["estado"]), {"clave", "etiqueta"}),
        "el ejecutivo": (set(o["orden"]["ejecutivo"]), {"nombre", "email"}),
        "un ítem": (set(o["items"][0]), CAMPOS["item"]),
        "una guía": (set(o["guias"][0]), CAMPOS["guia"]),
        "una línea de guía": (set(o["guias"][0]["items"][0]), CAMPOS["linea_de_guia"]),
        "una factura": (set(o["facturas"][0]), CAMPOS["factura"]),
        "el seguimiento": (set(s), {"orden", "item", "hitos", "guias", "facturas", "fotos"} | CAMPOS["sello"]),
        "la orden del seguimiento": (set(s["orden"]), {"id", "numero_oc"}),
        "el ítem del seguimiento": (set(s["item"]), CAMPOS["item"]),
        "un hito": (set(s["hitos"][0]), CAMPOS["hito"]),
        "una foto": (set(s["fotos"][0]), CAMPOS["foto"]),
    }
    for nombre, (visto, esperado) in estructura.items():
        check(f"§B {nombre} trae exactamente los campos del contrato", visto == esperado,
              f"de más: {sorted(visto - esperado)} · faltan: {sorted(esperado - visto)}")

    texto = "".join(r.text for r in respuestas)
    valores = {**INTERNOS, "el costo en USD del ítem 1": "1234.56", "el costo en USD del ítem 2": "777.77",
               "el margen del ítem": "0.3147", "el número interno de cotización": f"{MARK}-A1",
               "el RUT del cliente": ids["rut_a"], "el número interno del despacho": f"{MARK}-DSP",
               "el número del embarque": f"{MARK}-EMB", "el número de la orden al proveedor": f"{MARK}-OCP",
               "el nombre en disco de un archivo": D.PREFIJO_ARCHIVO}
    for nombre, valor in valores.items():
        check(f"§B {nombre} no aparece en ninguna respuesta", valor not in texto, valor)


# ── §C ──────────────────────────────────────────────────────────────────────────────────

def _paso_c_montos(cab_a, cab_bod, ids):
    # Lo que calcula Contabilidad para la venta A2 (la referencia: es lo que se factura).
    db = SessionLocal()
    try:
        items_db, pmap, _ = _precios_de_cotizacion(db, ids["cot2"], precios.config_global(db))
        esperado = {
            it.id: (round(float(pmap[it.id]["precio_venta_clp"]), 2),
                    int(_total_linea(pmap[it.id]["precio_venta_clp"], it.cantidad)))
            for it in items_db
        }
    finally:
        db.close()
    neto = sum(total for _, total in esperado.values())
    iva = int(_iva_clp(neto))

    orden = _get(f"/ordenes/{ids['oc2']}", cab_a).json()
    visto = {l["id"]: (l["monto"]["unitario_clp"], l["monto"]["total_clp"]) for l in orden["items"] if l["monto"]}
    check("§C venta de origen 'costo': unitario y total de cada ítem = los de Contabilidad",
          visto == esperado, (visto, esperado))
    check("§C …y neto, IVA y total de la orden también",
          orden["orden"]["totales"] == {"neto_clp": neto, "iva_clp": iva, "total_clp": neto + iva},
          (orden["orden"]["totales"], neto, iva))
    check("§C el precio mostrado NO es el costo (es mayor: lleva internación y margen)",
          visto[ids["it2"][0]][0] > COSTO_USD_1 * 950, visto)

    sin_foto = _get(f"/ordenes/{ids['oc3']}", cab_a).json()
    check("§C venta 'costo' sin precio congelado: sin totales y sin monto en NINGÚN ítem, tampoco en el ya facturado",
          sin_foto["orden"]["totales"] is None and [l["monto"] for l in sin_foto["items"]] == [None, None],
          (sin_foto["orden"]["totales"], [l["monto"] for l in sin_foto["items"]]))
    check("§C …su factura se sigue viendo como documento", [f["neto_clp"] for f in sin_foto["facturas"]] == [480000],
          sin_foto["facturas"])

    for nombre, ruta in (("orden", f"/ordenes/{ids['oc1']}"), ("listado", "/ordenes"),
                         ("ítem", f"/ordenes/{ids['oc1']}/items/{ids['it1'][1]}"), ("tablero", "/resumen")):
        r = _get(ruta, cab_bod)
        check(f"§C perfil bodega: el {nombre} no trae ningún monto",
              r.status_code == 200 and not _cifras(r.json()), (r.status_code, _cifras(r.json())[:5]))
    check("§C …y el mismo detalle, pedido por procurement, sí los trae (la sonda mira lo correcto)",
          len(_cifras(_get(f"/ordenes/{ids['oc1']}", cab_a).json())) > 5)
    orden_bod = _get(f"/ordenes/{ids['oc1']}", cab_bod).json()
    check("§C perfil bodega: sin facturas y sin archivos de guía, pero ve la guía y sus ítems",
          orden_bod["facturas"] == [] and len(orden_bod["guias"]) == 1
          and not orden_bod["guias"][0]["pdf"] and not orden_bod["guias"][0]["firmada"]
          and len(orden_bod["guias"][0]["items"]) == 1, orden_bod["guias"])


# ── §D ──────────────────────────────────────────────────────────────────────────────────

def _paso_d_varias_empresas(cab_a, ids):
    cab_dos = _cab(ids["email_dos"])
    ordenes = _get("/ordenes", cab_dos, page_size=50).json()["items"]
    check("§D un contacto de dos empresas ve las órdenes de las dos",
          {o["id"] for o in ordenes} == {ids["oc1"], ids["oc2"], ids["oc3"], ids["oc4"], ids["ocb"]},
          [o["id"] for o in ordenes])
    de_cual = {o["id"]: o["empresa"] for o in ordenes}
    check("§D …y cada orden dice de cuál empresa es",
          de_cual[ids["oc1"]] == ids["nombre_a"] and de_cual[ids["ocb"]] == ids["nombre_b"], de_cual)
    check("§D a quien representa una sola empresa no se le repite el nombre en cada orden",
          all(o["empresa"] is None for o in _get("/ordenes", cab_a, page_size=50).json()["items"]))
    check("§D con perfil 'procurement' en las dos, ve montos",
          client.get("/api/portal/yo", headers=cab_dos).json()["perfil"] == "procurement"
          and len(_cifras(_get(f"/ordenes/{ids['oc1']}", cab_dos).json())) > 5)

    db = SessionLocal()
    try:
        db.query(UsuarioCliente).filter(UsuarioCliente.usuario_id == ids["uid_dos"],
                                        UsuarioCliente.cliente_id == ids["ficha_b"]).update({"perfil": "bodega"})
        db.commit()
    finally:
        db.close()
    check("§D si en UNA de sus empresas es 'bodega', opera como bodega en todas: sin montos ni en la otra",
          client.get("/api/portal/yo", headers=cab_dos).json()["perfil"] == "bodega"
          and not _cifras(_get(f"/ordenes/{ids['oc1']}", cab_dos).json())
          and _get(f"/ordenes/{ids['oc1']}", cab_dos).json()["facturas"] == []
          and _get(f"/guias/{ids['d1']}/firmada", cab_dos).status_code == 404)


# ── §E ──────────────────────────────────────────────────────────────────────────────────

def _paso_e_listado(cab_a, ids):
    abiertas = {o["id"] for o in _get("/ordenes", cab_a, estado="abiertas", page_size=50).json()["items"]}
    entregadas = {o["id"] for o in _get("/ordenes", cab_a, estado="entregadas", page_size=50).json()["items"]}
    check("§E filtro: la orden entregada completa está en «entregadas» y no en «abiertas»",
          ids["oc4"] in entregadas and ids["oc4"] not in abiertas and ids["oc1"] in abiertas
          and ids["oc1"] not in entregadas, (abiertas, entregadas))
    todas = _get("/ordenes", cab_a, page_size=50).json()
    check("§E la insignia de cada orden coincide con el filtro que la trajo",
          all((o["estado"] == "entregada") == (o["id"] in entregadas) for o in todas["items"]),
          [(o["id"], o["estado"]) for o in todas["items"]])
    check("§E abiertas primero (aunque una abierta sea más antigua que la entregada) y, dentro de cada grupo, la más reciente arriba",
          [o["id"] for o in todas["items"]] == [ids["oc3"], ids["oc1"], ids["oc2"], ids["oc4"]],
          [(o["id"], o["estado"]) for o in todas["items"]])

    vistos = []
    for pagina in range(1, 10):
        r = _get("/ordenes", cab_a, page=pagina, page_size=2).json()
        if not r["items"]:
            break
        vistos += [o["id"] for o in r["items"]]
    check("§E paginando de a 2 se recorren todas las órdenes una sola vez",
          len(vistos) == len(set(vistos)) == todas["total"], (vistos, todas["total"]))

    check("§E filtro por año: 2024 trae la de 2024; un año sin órdenes, ninguna; el de B, ninguna",
          {o["id"] for o in _get("/ordenes", cab_a, anio=2024).json()["items"]} == {ids["oc4"]}
          and _get("/ordenes", cab_a, anio=2001).json()["total"] == 0
          and _get("/ordenes", cab_a, anio=2019).json()["total"] == 0)
    cab1 = next(o for o in todas["items"] if o["id"] == ids["oc1"])
    check("§E la fecha de la OC llega normalizada (10/06/2026 → 2026-06-10); la que no es fecha, null",
          cab1["fecha_oc"] == "2026-06-10"
          and next(o for o in todas["items"] if o["id"] == ids["oc2"])["fecha_oc"] is None, cab1["fecha_oc"])
    check("§E el ejecutivo de la venta viene con su nombre y correo",
          (_get(f"/ordenes/{ids['oc1']}", cab_a).json()["orden"]["ejecutivo"] or {}).get("email") == ids["asesor_email"])
    for nombre, params, esperado in (
        ("estado desconocido", {"estado": "pagadas"}, 400), ("page_size sobre el tope", {"page_size": 51}, 422),
        ("page 0", {"page": 0}, 422), ("año fuera de rango", {"anio": 99999}, 422),
        ("page no numérico", {"page": "abc"}, 422), ("q de 300 caracteres", {"q": "x" * 300}, 422),
    ):
        r = _get("/ordenes", cab_a, **params)
        check(f"§E {nombre} → {esperado}", r.status_code == esperado, f"{r.status_code} {r.text[:80]}")
    for ruta in ("/ordenes/0", "/ordenes/-1", "/ordenes/99999999", f"/ordenes/{ids['oc1']}/items/0"):
        check(f"§E {ruta} → 404", _get(ruta, cab_a).status_code == 404)
    check("§E un id que no es número → 422", _get("/ordenes/abc", cab_a).status_code == 422)


# ── §F ──────────────────────────────────────────────────────────────────────────────────

def _paso_f_clave_temporal(ids):
    duenio, interceptor = _cab(ids["email_tmp"]), _cab(ids["email_tmp"])
    yo = client.get("/api/portal/yo", headers=duenio)
    check("§F con la clave temporal, /portal/yo responde y avisa que hay que cambiarla",
          yo.status_code == 200 and yo.json()["debe_cambiar_clave"] is True, yo.text[:120])
    rutas = ("/resumen", "/ordenes", f"/ordenes/{ids['oc1']}", f"/ordenes/{ids['oc1']}/items/{ids['it1'][0]}",
             f"/fotos/{ids['foto']}", f"/guias/{ids['d1']}/pdf", f"/guias/{ids['d1']}/firmada", "/facturas/1/pdf")
    for ruta in rutas:
        r = _get(ruta, duenio)
        check(f"§F con la clave temporal, GET {ruta} → 403 con el motivo",
              r.status_code == 403 and r.json().get("detail") == DETALLE_DEBE_CAMBIAR_CLAVE,
              f"{r.status_code} {r.text[:80]}")

    nueva = "otra-clave-larga-2026"
    r = client.post("/api/portal/yo/clave", headers=duenio, json={"clave_actual": D.CLAVE, "clave_nueva": nueva})
    check("§F el dueño cambia la clave y recibe un token nuevo",
          r.status_code == 200 and r.json().get("ok") is True and len(r.json().get("access_token", "")) > 20, r.text[:120])
    renovado = {"Authorization": f"Bearer {r.json().get('access_token')}"}
    check("§F …con ese token ya ve sus pedidos", _get("/ordenes", renovado).status_code == 200)
    check("§F el token de quien había entrado con la temporal deja de valer: 401 en los datos y en /yo",
          _get("/ordenes", interceptor).status_code == 401
          and client.get("/api/portal/yo", headers=interceptor).status_code == 401,
          (_get("/ordenes", interceptor).status_code, client.get("/api/portal/yo", headers=interceptor).status_code))
    check("§F …y también el del dueño anterior al cambio (un token vale mientras valga su clave)",
          _get("/ordenes", duenio).status_code == 401)

    r = client.post(f"/api/portal-admin/cuentas/{ids['uid_tmp']}/restablecer-clave", headers=_cab(ids["email_op"]))
    check("§F cuando el personal restablece la clave, la sesión abierta también deja de valer",
          r.status_code == 200 and _get("/ordenes", renovado).status_code == 401,
          (r.status_code, _get("/ordenes", renovado).status_code))
    check("§F el token del personal no lleva huella y sigue funcionando",
          client.get("/api/auth/me", headers=_cab(ids["email_op"])).status_code == 200)


# ── §G ──────────────────────────────────────────────────────────────────────────────────

def _paso_g_tablero(cab_a, ids):
    tablero = _get("/resumen", cab_a).json()
    ordenes = _get("/ordenes", cab_a, page_size=50).json()["items"]
    detalles = {o["id"]: _get(f"/ordenes/{o['id']}", cab_a).json() for o in ordenes}
    items = [l for d in detalles.values() for l in d["items"]]
    check("§G órdenes abiertas y entregadas = las del listado",
          tablero["ordenes"] == {
              "total": len(ordenes), "abiertas": sum(o["estado"] == "abierta" for o in ordenes),
              "entregadas": sum(o["estado"] == "entregada" for o in ordenes)}, tablero["ordenes"])
    check("§G ítems totales, entregados, pendientes, por entregar y atrasados = los de las órdenes",
          tablero["items"] == {
              "total": len(items), "entregados": sum(l["entregado"] for l in items),
              "pendientes": sum(not l["entregado"] for l in items),
              "por_entregar": sum(l["cantidad_por_entregar"] > 0 for l in items),
              "atrasados": sum(l["vencida"] for l in items)}, tablero["items"])
    check("§G …y cada orden cuenta los suyos igual",
          all(o["items"] == {"total": len(detalles[o["id"]]["items"]),
                             "entregados": sum(l["entregado"] for l in detalles[o["id"]]["items"]),
                             "por_entregar": sum(l["cantidad_por_entregar"] > 0 for l in detalles[o["id"]]["items"])}
              for o in ordenes), [o["items"] for o in ordenes])
    por_estado = {}
    for l in items:
        if not l["entregado"]:
            por_estado[l["estado"]["clave"]] = por_estado.get(l["estado"]["clave"], 0) + 1
    check("§G «dónde viene lo que falta» = el estado de cada ítem pendiente",
          {p["clave"]: p["items"] for p in tablero["pendientes_por_estado"]} == por_estado,
          (tablero["pendientes_por_estado"], por_estado))

    # Los tres montos, sobre las órdenes que tienen precio en TODOS sus ítems.
    con_monto = [o for o in ordenes if o["totales"] is not None]
    comprado = sum(o["totales"]["neto_clp"] for o in con_monto)
    # (En estas órdenes cada ítem tiene un solo precio y nada «no recibido»: lo que falta
    # por entregar vale unitario × lo que no ha salido. Los demás cruces: test_valor.py.)
    por_entregar = sum(
        precios.total_de_linea(l["monto"]["unitario_clp"], l["cantidad"] - l["cantidad_entregada"])
        for o in con_monto for l in detalles[o["id"]]["items"])
    facturado = sum(f["neto_clp"] for o in con_monto for f in detalles[o["id"]]["facturas"])
    check("§G comprado y por entregar = la suma de las órdenes con precio",
          tablero["montos"]["comprado_neto_clp"] == comprado
          and tablero["montos"]["por_entregar_neto_clp"] == por_entregar, (tablero["montos"], comprado, por_entregar))
    check("§G «facturado» sale del MISMO conjunto: la factura de la orden sin precios no se suma",
          tablero["montos"]["facturado_neto_clp"] == facturado == 100000, (tablero["montos"], facturado))
    check("§G el tablero avisa cuántas órdenes no entraron en las sumas",
          tablero["montos"]["ordenes_sin_monto"] == len(ordenes) - len(con_monto) == 1, tablero["montos"])
    comprometidas = [l["fecha_prometida"] for l in items if not l["entregado"] and l["fecha_prometida"]]
    check("§G la fecha del tablero es la más antigua entre lo que se espera (acá, una que ya venció)",
          tablero["proxima_fecha"] == min(comprometidas) < hoy_chile().isoformat(),
          (tablero["proxima_fecha"], min(comprometidas)))

    de_2024 = _get("/resumen", cab_a, anio=2024).json()
    check("§G el tablero de un año cuenta solo las órdenes de ese año",
          de_2024["anio"] == 2024 and de_2024["ordenes"] == {"total": 1, "abiertas": 0, "entregadas": 1}
          and de_2024["montos"]["comprado_neto_clp"] == 1000, de_2024)
    check("§G …y el de un año sin órdenes queda en cero",
          _get("/resumen", cab_a, anio=2019).json()["ordenes"]["total"] == 0)

    check("§G con todas las órdenes a la vista, no avisa recorte", tablero["truncado"] is False)
    tope = resumen.ORDENES_MAXIMAS
    resumen.ORDENES_MAXIMAS = 3
    try:
        recortado = _get("/resumen", cab_a).json()
    finally:
        resumen.ORDENES_MAXIMAS = tope
    check("§G si hay más órdenes que el tope, lo avisa y deja fuera una ENTREGADA (no una abierta)",
          recortado["truncado"] is True and recortado["ordenes"] == {"total": 3, "abiertas": 3, "entregadas": 0},
          (recortado["truncado"], recortado["ordenes"]))


# ── §H ──────────────────────────────────────────────────────────────────────────────────

def _paso_h_cupo(cab_a, cab_b, ids):
    cupo.reset()
    codigos = [_get("/resumen", cab_a).status_code for _ in range(cupo.MAXIMOS[cupo.TABLERO] + 1)]
    check("§H el tablero pedido en bucle: las primeras pasan y la que excede el cupo recibe 429",
          set(codigos[:-1]) == {200} and codigos[-1] == 429, codigos[-3:])
    r = _get("/resumen", cab_a)
    check("§H …con un texto para la persona y cuánto esperar",
          r.status_code == 429 and r.json()["detail"] == cupo.DETALLE and int(r.headers["retry-after"]) >= 1,
          (r.status_code, dict(r.headers)))
    check("§H …las demás rutas de esa cuenta siguen (es el cupo del tablero) y otra cuenta no se entera",
          _get("/ordenes", cab_a).status_code == 200 and _get("/resumen", cab_b).status_code == 200)
    cupo.reset()
    codigos = [_get(f"/guias/{ids['d1']}/firmada", cab_a).status_code for _ in range(cupo.MAXIMOS[cupo.DESCARGAS] + 1)]
    check("§H descargas en bucle: 429 al pasar el cupo de descargas",
          set(codigos[:-1]) == {200} and codigos[-1] == 429, codigos[-3:])
    cupo.reset()
    general = cupo.MAXIMOS[cupo.GENERAL]
    cupo.MAXIMOS[cupo.GENERAL] = 5
    try:
        codigos = [_get("/ordenes", cab_a).status_code for _ in range(6)]
    finally:
        cupo.MAXIMOS[cupo.GENERAL] = general
    check("§H el cupo general cubre todas las rutas de datos", codigos == [200] * 5 + [429], codigos)
    # El cupo es por MINUTO: lo anotado hace más de una ventana deja de contar.
    codigos = [_get("/resumen", cab_a).status_code for _ in range(cupo.MAXIMOS[cupo.TABLERO] + 1)]
    ventana = cupo.VENTANA_SEGUNDOS
    cupo.VENTANA_SEGUNDOS = 0
    try:
        tras_la_ventana = _get("/resumen", cab_a).status_code
    finally:
        cupo.VENTANA_SEGUNDOS = ventana
    check("§H pasada la ventana de tiempo, la cuenta vuelve a entrar sola", codigos[-1] == 429 and tras_la_ventana == 200,
          (codigos[-1], tras_la_ventana))
    # El interruptor de emergencia (PORTAL_CUPO_ENABLED=false en backend/.env).
    cupo.reset()
    encendido, tope = settings.PORTAL_CUPO_ENABLED, cupo.MAXIMOS[cupo.TABLERO]
    settings.PORTAL_CUPO_ENABLED, cupo.MAXIMOS[cupo.TABLERO] = False, 1
    try:
        codigos = [_get("/resumen", cab_a).status_code for _ in range(3)]
    finally:
        settings.PORTAL_CUPO_ENABLED, cupo.MAXIMOS[cupo.TABLERO] = encendido, tope
    check("§H con PORTAL_CUPO_ENABLED apagado no se corta nada", codigos == [200] * 3, codigos)
    cupo.reset()
    check("§H al limpiar los contadores la cuenta vuelve a entrar", _get("/resumen", cab_a).status_code == 200)


def run():
    del _fails[:]
    D.limpiar(MARK)
    try:
        with D.carpetas_de_prueba():
            ids = _sembrar()
            cab_a, cab_b, cab_bod = _cab(ids["email_a"]), _cab(ids["email_b"]), _cab(ids["email_bod"])
            _paso_a_cada_cliente_ve_lo_suyo(cab_a, cab_b, ids)
            _paso_a_el_candado_vive_en_un_solo_archivo()
            _paso_b_no_sale_nada_interno(cab_a, ids)
            _paso_c_montos(cab_a, cab_bod, ids)
            _paso_e_listado(cab_a, ids)
            _paso_g_tablero(cab_a, ids)
            _paso_d_varias_empresas(cab_a, ids)
            _paso_f_clave_temporal(ids)
            _paso_h_cupo(cab_a, cab_b, ids)
        print()
        print(f"FALLARON {len(_fails)}: {_fails}" if _fails else "SUITE VERDE: pedidos del portal")
        assert not _fails, _fails
    finally:
        D.limpiar(MARK)


def test_ordenes_portal():
    run()


if __name__ == "__main__":
    run()
