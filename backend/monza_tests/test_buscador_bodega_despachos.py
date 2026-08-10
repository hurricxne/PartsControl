"""Buscador de operador en BODEGA y DESPACHOS MonzaParts (spec 2026-08-05).

QUÉ CUBRE (contrato común del parámetro q, implementación PROPIA de Monza):
  · Normalización: strip + colapsar espacios + truncado, mínimo 2 caracteres
    (bajo eso el filtro se IGNORA), tokens AND (máx 4) con OR entre campos.
  · Escapado de % y _ del LIKE: '1Q0_45Z7' encuentra SOLO la parte que
    literalmente contiene el guion bajo — sin escape matchearía '1Q0-45Z7' y
    '1Q0X45Z7', dos partes DISTINTAS (el error que cuesta plata).
  · Doble pasada del número de parte EN LAS DOS DIRECCIONES: término con guiones
    encuentra el dato sin guiones y viceversa (pasada B colapsada, normalizado=true).
  · Cada campo prometido por el placeholder: n° parte, repuesto, COT, OC del
    cliente, embarque (numero/awb), guía del proveedor (recepción NACIONAL), OCP,
    cliente, N° de guía del despacho.
  · Paginación con total HONESTO y orden determinista (id DESC), sobre nuevo
    {items,total,...} SOLO con page — sin page la respuesta LEGADA (array pelado)
    se conserva para los consumidores existentes (test_aud_pipeline, frontend viejo).
  · match [{campo, valor}] calculado por el BACKEND sobre la página.
  · Los DOS endpoints de Despachos (listado y /avance) buscan IGUAL (la
    divergencia SQL-vs-Python era deuda declarada).
  · Cobertura parcial del card de avance: items_con_cupo < total_items viaja para
    pintar «Listo · N de M ítems».

SONDAS DE PODER DISCRIMINANTE
  · EXISTS vs JOIN: una venta con 3 líneas que matchean aparece UNA vez y total
    dice 1 — con .join() count() daría 3 y la página perdería filas.
  · Pasada colapsada: los términos ZB7T-1997 / ZB9311QQ43 SOLO matchean gracias a
    la normalización (la respuesta lo delata con normalizado=true); quitarla pone
    estos checks en rojo.
  · Escape: quitar el escape= hace que '1Q0_45Z7' devuelva 3 partes y el check
    de total==1 se cae.

Sin red. Datos con MARK propio y limpieza verificada al final con sesión nueva.

Corre con:  ./venv/bin/python -m pytest monza_tests/test_buscador_bodega_despachos.py -q
"""
import os
import sys
import uuid
from datetime import date, datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import SessionLocal  # noqa: E402
from auth import get_current_user  # noqa: E402
from monza_models import (  # noqa: E402
    MonzaCliente, MonzaCotizacion, MonzaCotizacionItem, MonzaOcProveedor,
    MonzaEmbarque, MonzaEmbarqueItem, MonzaRecepcion, MonzaRecepcionItem,
    MonzaDespacho, MonzaDespachoItem, MonzaLog,
)
from monza_recepcion_nacional.models import (  # noqa: E402
    MonzaRecepcionNacional, MonzaRecepcionNacionalItem,
)
from monza_router_bodega import router as bodega_router  # noqa: E402
from monza_router_despachos import router as despachos_router  # noqa: E402
from monza_router_despachos import _normalizar_q, _patron, _variantes_q  # noqa: E402

MARK = "test-mzbsc"
EMAIL = f"{MARK}@test.invalid"

app = FastAPI()
app.include_router(bodega_router)
app.include_router(despachos_router)
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=1, email=EMAIL, empresa="automotriz")
client = TestClient(app)

_fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _seed():
    """V1 (vendida, 6 líneas en bodega + 1 embarcada) y V2 (despachada, 3 líneas
    que matchean el MISMO término — la sonda EXISTS-vs-JOIN)."""
    db = SessionLocal()
    try:
        suf = uuid.uuid4().hex[:6].upper()
        cli = MonzaCliente(nombre=f"{MARK} Automotora Andina", rut="76.543.210-K")
        db.add(cli); db.flush()

        v1 = MonzaCotizacion(
            numero=f"BQ-{suf}", cliente_id=cli.id, estado="vendida",
            oc_cliente=f"OC-{MARK}-4500", vehiculo=f"{MARK} Toyota Hilux",
            vin=f"VIN{suf}9", total_bruto=100000)
        v2 = MonzaCotizacion(
            numero=f"BD-{suf}", cliente_id=cli.id, estado="despachado",
            fecha_despacho=date.today(), oc_cliente=f"OC-{MARK}-7700",
            numero_factura=f"F-{MARK}-321", vehiculo=f"{MARK} Kia Frontier",
            total_bruto=50000)
        db.add_all([v1, v2]); db.flush()

        def item(cot, desc, np=None, marca=None, cant=1, estado="en_bodega"):
            it = MonzaCotizacionItem(cotizacion_id=cot.id, descripcion=desc,
                                     numero_parte=np, marca=marca, cantidad=cant,
                                     estado_linea=estado)
            db.add(it)
            return it

        A = item(v1, f"{MARK} BOMBA HIDRAULICA", "ZB9-311-QQ43", "Komatsu", 4)
        B = item(v1, f"{MARK} FILTRO ACEITE", "ZB7T1997", cant=2)
        C1 = item(v1, f"{MARK} SELLO UNO", "1Q0_45Z7")
        C2 = item(v1, f"{MARK} SELLO DOS", "1Q0-45Z7")
        C3 = item(v1, f"{MARK} SELLO TRES", "1Q0X45Z7")
        PCT = item(v1, f"{MARK} KIT 50% DESC")
        D = item(v1, f"{MARK} RADIADOR", "ZD5-777-88", estado="embarcado")
        X1 = item(v2, f"{MARK} RETEN UNO", "ZM1-100-77A", estado="despachado")
        X2 = item(v2, f"{MARK} RETEN DOS", "ZM9100AB7", estado="despachado")
        X3 = item(v2, f"{MARK} RETEN TRES", estado="despachado")
        db.flush()

        # OCP vinculada a A (vínculo Integer suelto, como en producción)
        ocp = MonzaOcProveedor(numero=f"OCP-{MARK}-01", numero_oc=f"PO-{MARK}-88",
                               proveedor_nombre=f"{MARK} Proveedor")
        db.add(ocp); db.flush()
        A.oc_proveedor_id = ocp.id

        # Embarque E1 trae A (buscar por embarque/AWB desde Bodega y Despachos)
        e1 = MonzaEmbarque(numero=f"EMB-{MARK}-77", awb=f"AWB-{MARK}-999",
                           estado="en_bodega")
        db.add(e1); db.flush()
        db.add(MonzaEmbarqueItem(embarque_id=e1.id, item_id=A.id))

        # Embarque E2 con recepción CERRADA (historial) que trajo D
        e2 = MonzaEmbarque(numero=f"EMB-{MARK}-H2", awb=f"AWB-{MARK}-H2",
                           estado="en_bodega")
        db.add(e2); db.flush()
        db.add(MonzaEmbarqueItem(embarque_id=e2.id, item_id=D.id))
        db.add(MonzaRecepcion(embarque_id=e2.id, estado="cerrada",
                              fecha_cierre=datetime.utcnow(), usuario_email=EMAIL))

        # Recepción NACIONAL cerrada de B: su identificador es la guía del
        # proveedor (estos ítems JAMÁS pasan por embarque)
        rn = MonzaRecepcionNacional(numero_guia_proveedor=f"GN-{MARK}-555",
                                    estado="cerrada")
        db.add(rn); db.flush()
        db.add(MonzaRecepcionNacionalItem(recepcion_id=rn.id,
                                          item_cotizacion_id=B.id,
                                          qty_recibida=2,
                                          estado_recepcion="completo"))

        # Despacho CERRADO de V2 con guía (buscar por N° de guía / N° DSP)
        dsp = MonzaDespacho(numero=f"DSP-BD-{suf}", cotizacion_id=v2.id,
                            numero_guia=f"GUIA-{MARK}-123",
                            transportista=f"{MARK} Chilexpress",
                            estado="despachado", fecha_despacho=datetime.utcnow())
        db.add(dsp); db.flush()
        db.add(MonzaDespachoItem(despacho_id=dsp.id, item_id=X1.id, qty_despachada=1))

        db.commit()
        return {
            "suf": suf, "cli": cli.id, "v1": v1.id, "v2": v2.id,
            "A": A.id, "B": B.id, "C1": C1.id, "C2": C2.id, "C3": C3.id,
            "PCT": PCT.id, "D": D.id, "X1": X1.id, "X2": X2.id, "X3": X3.id,
            "e1": e1.id, "e2": e2.id, "ocp": ocp.id, "rn": rn.id, "dsp": dsp.id,
        }
    finally:
        db.close()


def _limpiar():
    db = SessionLocal()
    try:
        S = "fetch"
        cot_ids = [r[0] for r in db.query(MonzaCotizacion.id)
                   .join(MonzaCliente, MonzaCliente.id == MonzaCotizacion.cliente_id)
                   .filter(MonzaCliente.nombre.like(f"{MARK}%")).all()]
        item_ids = [r[0] for r in db.query(MonzaCotizacionItem.id)
                    .filter(MonzaCotizacionItem.cotizacion_id.in_(cot_ids or [0])).all()]
        dsp_ids = [r[0] for r in db.query(MonzaDespacho.id)
                   .filter(MonzaDespacho.cotizacion_id.in_(cot_ids or [0])).all()]
        emb_ids = [r[0] for r in db.query(MonzaEmbarque.id)
                   .filter(MonzaEmbarque.numero.like(f"EMB-{MARK}%")).all()]
        rec_ids = [r[0] for r in db.query(MonzaRecepcion.id)
                   .filter(MonzaRecepcion.embarque_id.in_(emb_ids or [0])).all()]
        rn_ids = [r[0] for r in db.query(MonzaRecepcionNacional.id)
                  .filter(MonzaRecepcionNacional.numero_guia_proveedor.like(f"GN-{MARK}%")).all()]
        db.query(MonzaRecepcionNacionalItem).filter(
            MonzaRecepcionNacionalItem.recepcion_id.in_(rn_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaRecepcionNacional).filter(
            MonzaRecepcionNacional.id.in_(rn_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaDespachoItem).filter(
            MonzaDespachoItem.despacho_id.in_(dsp_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaDespacho).filter(MonzaDespacho.id.in_(dsp_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaRecepcionItem).filter(
            MonzaRecepcionItem.recepcion_id.in_(rec_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaRecepcion).filter(MonzaRecepcion.id.in_(rec_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaEmbarqueItem).filter(
            MonzaEmbarqueItem.embarque_id.in_(emb_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaEmbarque).filter(MonzaEmbarque.id.in_(emb_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaOcProveedor).filter(
            MonzaOcProveedor.numero.like(f"OCP-{MARK}%")).delete(synchronize_session=S)
        db.query(MonzaCotizacionItem).filter(
            MonzaCotizacionItem.id.in_(item_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaCotizacion).filter(MonzaCotizacion.id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaLog).filter(MonzaLog.user_email == EMAIL).delete(synchronize_session=S)
        db.query(MonzaCliente).filter(MonzaCliente.nombre.like(f"{MARK}%")).delete(synchronize_session=S)
        db.commit()
    finally:
        db.close()


def _verificar_limpieza():
    """Sesión NUEVA (regla de la casa): una reutilizada serviría su propio snapshot."""
    db = SessionLocal()
    try:
        restos = (
            db.query(MonzaCliente).filter(MonzaCliente.nombre.like(f"{MARK}%")).count()
            + db.query(MonzaEmbarque).filter(MonzaEmbarque.numero.like(f"EMB-{MARK}%")).count()
            + db.query(MonzaOcProveedor).filter(MonzaOcProveedor.numero.like(f"OCP-{MARK}%")).count()
            + db.query(MonzaRecepcionNacional).filter(
                MonzaRecepcionNacional.numero_guia_proveedor.like(f"GN-{MARK}%")).count()
        )
        assert restos == 0, f"quedaron {restos} filas {MARK} tras la limpieza"
    finally:
        db.close()
    print("Cleanup OK (verificado con sesión nueva)")


def _bodega(params):
    r = client.get("/api/monza/bodega/en-bodega", params=params)
    assert r.status_code == 200, r.text[:300]
    return r.json()


def _ids(sobre):
    return [x["id"] for x in sobre["items"]]


def run():
    _limpiar()
    ids = _seed()
    suf = ids["suf"]
    try:
        # ══ A · El helper, sin base ══════════════════════════════════════════
        n = _normalizar_q("  COT-11   BOMBA  x y z ")
        check("a1 espacios colapsados + máx 4 tokens",
              n["q"] == "COT-11 BOMBA x y z" and n["tokens"] == ["COT-11", "BOMBA", "x", "y"], n)
        check("a2 con 1 carácter el filtro se ignora", _normalizar_q(" Z ")["activo"] is False)
        check("a3 _patron escapa % _ y backslash",
              _patron("a_b%c\\") == "%a\\_b\\%c\\\\%", _patron("a_b%c\\"))
        check("a4 variante sin prefijo COT-", _variantes_q("COT-2026-45") == ["COT-2026-45", "2026-45"])

        # ══ B · Bodega /en-bodega ════════════════════════════════════════════
        r = client.get("/api/monza/bodega/en-bodega")
        check("b1 retro-compat: sin page la respuesta sigue siendo ARRAY pelado",
              isinstance(r.json(), list) and any(x["id"] == ids["A"] for x in r.json()))

        s = _bodega({"q": MARK, "page": 1, "page_size": 50})
        check("b2 sobre del contrato {items,total,page,page_size,q_efectivo,normalizado}",
              isinstance(s, dict) and set(s) >= {"items", "total", "page", "page_size",
                                                "q_efectivo", "normalizado"}, s if not isinstance(s, dict) else "")
        check("b2b total honesto: las 6 líneas en bodega de la venta", s["total"] == 6, s["total"])

        s = _bodega({"q": "ZB9-311-QQ43", "page": 1})
        check("b3 por N° DE PARTE (el dato #1 del dueño, hoy ausente en producción)",
              s["total"] == 1 and _ids(s) == [ids["A"]], (s["total"], _ids(s)))
        check("b3b insignia de motivo: campo numero_parte",
              any(m["campo"] == "numero_parte" for m in s["items"][0]["match"]), s["items"][0].get("match"))
        check("b3c pasada literal: normalizado=false", s["normalizado"] is False)

        s = _bodega({"q": f"{MARK} bomba hidraulica", "page": 1})
        check("b4 minúsculas encuentran MAYÚSCULAS (collation _ci, .like sin lower())",
              s["total"] == 1 and _ids(s) == [ids["A"]], (s["total"], _ids(s)))

        s = _bodega({"q": f"BQ-{suf}", "page": 1})
        check("b5 por N° COT", s["total"] == 6, s["total"])

        s = _bodega({"q": f"OC-{MARK}-4500", "page": 1})
        check("b6 por OC DEL CLIENTE (columna de la cotización: la ventaja Monza)",
              s["total"] == 6 and any(m["campo"] == "oc_cliente" for m in s["items"][0]["match"]),
              (s["total"], s["items"][0].get("match") if s["items"] else None))
        check("b6b la salida trae oc_cliente para que el operador verifique",
              s["items"][0].get("oc_cliente") == f"OC-{MARK}-4500", s["items"][0].get("oc_cliente"))

        s = _bodega({"q": f"EMB-{MARK}-77", "page": 1})
        check("b7 por N° DE EMBARQUE (EXISTS, campo no visible → insignia con VALOR)",
              s["total"] == 1 and _ids(s) == [ids["A"]]
              and any(m["campo"] == "embarque" and m["valor"] == f"EMB-{MARK}-77"
                      for m in s["items"][0]["match"]),
              (s["total"], s["items"][0].get("match") if s["items"] else None))
        check("b7b la fila trae el embarque en la salida",
              s["items"][0].get("embarque") == f"EMB-{MARK}-77", s["items"][0].get("embarque"))

        s = _bodega({"q": f"AWB-{MARK}-999", "page": 1})
        check("b8 por AWB (en Monza `awb` ES el número, no el archivo)",
              s["total"] == 1 and _ids(s) == [ids["A"]], (s["total"], _ids(s)))

        s = _bodega({"q": f"GN-{MARK}-555", "page": 1})
        check("b9 por GUÍA DEL PROVEEDOR (recepción NACIONAL: nunca tuvo embarque)",
              s["total"] == 1 and _ids(s) == [ids["B"]], (s["total"], _ids(s)))
        check("b9b la fila lo dice: guia_nacional en la salida",
              s["items"][0].get("guia_nacional") == f"GN-{MARK}-555", s["items"][0].get("guia_nacional"))

        s = _bodega({"q": f"PO-{MARK}-88", "page": 1})
        check("b10 por OCP (numero_oc manual del proveedor)",
              s["total"] == 1 and _ids(s) == [ids["A"]], (s["total"], _ids(s)))

        s = _bodega({"q": "ZB7T-1997", "page": 1})
        check("b11 guiones dirección 1: término CON guion encuentra dato SIN guion",
              s["total"] == 1 and _ids(s) == [ids["B"]], (s["total"], _ids(s)))
        check("b11b SONDA: solo matchea gracias a la pasada colapsada (normalizado=true; "
              "quitar la normalización pone este check en rojo)", s["normalizado"] is True)
        check("b11c insignia 'n° parte (sin guiones)' (se resalta el campo COMPLETO)",
              any(m["campo"] == "numero_parte_sin_guiones" for m in s["items"][0]["match"]),
              s["items"][0].get("match"))

        s = _bodega({"q": "ZB9311QQ43", "page": 1})
        check("b12 guiones dirección 2: término SIN guiones encuentra dato CON guiones",
              s["total"] == 1 and _ids(s) == [ids["A"]] and s["normalizado"] is True,
              (s["total"], _ids(s), s["normalizado"]))

        s = _bodega({"q": "1Q0_45Z7", "page": 1})
        check("b13 ESCAPE de _: encuentra SOLO la parte que literalmente lleva guion bajo "
              "(sin escape devolvería también 1Q0-45Z7 y 1Q0X45Z7)",
              s["total"] == 1 and _ids(s) == [ids["C1"]], (s["total"], _ids(s)))

        s = _bodega({"q": "Q0_45", "page": 1})
        check("b13b escape de _ en término parcial", s["total"] == 1 and _ids(s) == [ids["C1"]],
              (s["total"], _ids(s)))

        s = _bodega({"q": "50% DESC", "page": 1})
        check("b14 ESCAPE de %: '50%' literal, no comodín",
              s["total"] == 1 and _ids(s) == [ids["PCT"]], (s["total"], _ids(s)))

        s = _bodega({"q": f"   {MARK}    FILTRO  ", "page": 1})
        check("b15 espacios pegados/duplicados: strip + colapsar (AND entre tokens)",
              s["total"] == 1 and _ids(s) == [ids["B"]], (s["total"], _ids(s)))

        s = _bodega({"q": "Z", "page": 1})
        check("b16 con MENOS de 2 caracteres el filtro se ignora (lista completa)",
              s["total"] >= 6 and s["normalizado"] is False, s["total"])

        s1 = _bodega({"q": MARK, "page": 1, "page_size": 2})
        s2 = _bodega({"q": MARK, "page": 2, "page_size": 2})
        s3 = _bodega({"q": MARK, "page": 3, "page_size": 2})
        todos = _ids(s1) + _ids(s2) + _ids(s3)
        check("b17 paginación honesta: total=6 con páginas de 2, sin duplicar ni perder",
              s1["total"] == 6 and len(_ids(s1)) == 2 and len(set(todos)) == 6,
              (s1["total"], todos))
        check("b17b orden determinista id DESC (con OFFSET, sin desempate se repiten filas)",
              todos == sorted(todos, reverse=True), todos)

        s = _bodega({"q": f"{MARK} INEXISTENTE99X", "page": 1})
        check("b18 AND entre tokens: un token sin campo detrás → 0 resultados",
              s["total"] == 0, s["total"])

        s = _bodega({"q": f"EMB-{MARK}-77", "page": 1})
        check("b19 los campos de cupo del hallazgo #10 siguen viajando (retro)",
              s["items"][0].get("qty_disponible") == 4, s["items"][0].get("qty_disponible"))

        # ══ C · Bodega /embarques/historial ══════════════════════════════════
        r = client.get("/api/monza/bodega/embarques/historial")
        check("c1 retro-compat historial: sin page sigue siendo ARRAY",
              isinstance(r.json(), list) and any(x["id"] == ids["e2"] for x in r.json()))

        r = client.get("/api/monza/bodega/embarques/historial",
                       params={"q": f"EMB-{MARK}-H2", "page": 1})
        s = r.json()
        check("c2 historial por N° de embarque, con sobre y total honesto",
              r.status_code == 200 and s["total"] == 1 and s["items"][0]["id"] == ids["e2"],
              (r.status_code, s if r.status_code == 200 else r.text[:200]))

        r = client.get("/api/monza/bodega/embarques/historial",
                       params={"q": "ZD5-777-88", "page": 1})
        s = r.json()
        check("c3 historial por N° DE PARTE del ítem que trajo (insignia con VALOR)",
              s["total"] == 1 and s["items"][0]["id"] == ids["e2"]
              and any(m["campo"] == "numero_parte" and m["valor"] == "ZD5-777-88"
                      for m in s["items"][0]["match"]),
              s)

        # ══ D · Despachos: listado (histórico) ═══════════════════════════════
        r = client.get("/api/monza/despachos", params={"q": f"{MARK} RETEN"})
        s = r.json()
        check("d1 SONDA EXISTS-vs-JOIN: la venta con 3 líneas que matchean aparece "
              "UNA vez y total dice 1 (con .join() count()=3 y la página pierde filas)",
              s["total"] == 1 and len(s["items"]) == 1 and s["items"][0]["id"] == ids["v2"],
              (s["total"], [x["id"] for x in s["items"]]))

        r = client.get("/api/monza/despachos", params={"q": "ZM1-100-77A"})
        s = r.json()
        check("d2 histórico por N° de parte del ítem (hoy imposible en producción)",
              s["total"] == 1 and s["items"][0]["id"] == ids["v2"], s["total"])

        r = client.get("/api/monza/despachos", params={"q": f"GUIA-{MARK}-123"})
        s = r.json()
        check("d3 histórico por N° DE GUÍA del despacho (el papel que cita el cliente)",
              s["total"] == 1 and s["items"][0]["id"] == ids["v2"]
              and any(m["campo"] == "guia" for m in s["items"][0]["match"]),
              (s["total"], s["items"][0].get("match") if s["items"] else None))

        r = client.get("/api/monza/despachos", params={"q": f"{MARK} andina"})
        s = r.json()
        check("d4 histórico por cliente en minúsculas", s["total"] == 1
              and s["items"][0]["id"] == ids["v2"], s["total"])

        r = client.get("/api/monza/despachos", params={"q": "ZM9-100-AB7"})
        s = r.json()
        check("d5 pasada colapsada también en Despachos (ZM9-100-AB7 → ZM9100AB7)",
              s["total"] == 1 and s["items"][0]["id"] == ids["v2"] and s["normalizado"] is True,
              (s["total"], s.get("normalizado")))
        check("d5b el sobre declara q_efectivo", s.get("q_efectivo") == "ZM9-100-AB7",
              s.get("q_efectivo"))

        # ══ E · Despachos: /avance (mismo helper → misma respuesta) ══════════
        r = client.get("/api/monza/despachos/avance", params={"tab": "listas"})
        check("e1 retro-compat /avance: sin page sigue siendo ARRAY",
              isinstance(r.json(), list))

        r = client.get("/api/monza/despachos/avance",
                       params={"tab": "listas", "q": f"EMB-{MARK}-77", "page": 1})
        s = r.json()
        check("e2 /avance encuentra la venta por su EMBARQUE (EXISTS del helper común)",
              s["total"] == 1 and s["items"][0]["id"] == ids["v1"]
              and any(m["campo"] == "embarque" for m in s["items"][0]["match"]),
              s)

        r = client.get("/api/monza/despachos/avance",
                       params={"tab": "historial", "q": f"GUIA-{MARK}-123", "page": 1})
        s = r.json()
        check("e3 PARIDAD: el MISMO término (N° de guía) que encontró V2 en el "
              "histórico la encuentra en /avance — los dos endpoints buscan IGUAL",
              s["total"] == 1 and s["items"][0]["id"] == ids["v2"], s)

        r = client.get("/api/monza/despachos/avance",
                       params={"tab": "listas", "q": "ZB7T-1997", "page": 1})
        s = r.json()
        check("e4 /avance con pasada colapsada (normalizado=true)",
              s["total"] == 1 and s["items"][0]["id"] == ids["v1"] and s["normalizado"] is True, s)

        r = client.get("/api/monza/despachos/avance",
                       params={"tab": "listas", "q": f"GUIA-{MARK}-123", "page": 1})
        s = r.json()
        check("e5 conteo CRUZADO de pestañas: 0 aquí, 1 en Historial (la conducta "
              "de mayor valor del encargo: 'dónde está esto')",
              s["total"] == 0 and s["tabs"]["historial"] == 1 and s["tabs"]["listas"] == 0,
              s.get("tabs"))

        # ══ F · Cobertura parcial del card («Listo · N de M ítems») ══════════
        r = client.get("/api/monza/despachos/avance",
                       params={"tab": "listas", "q": f"BQ-{suf}", "page": 1})
        s = r.json()
        card = s["items"][0] if s["items"] else {}
        check("f1 el card dice N de M cuando N < M: items_con_cupo=6 < total_items=7 "
              "con estado 'listo' (la venta se puede despachar PERO no completa)",
              card.get("estado") == "listo" and card.get("items_con_cupo") == 6
              and card.get("total_items") == 7
              and card["items_con_cupo"] < card["total_items"],
              {k: card.get(k) for k in ("estado", "items_con_cupo", "total_items")})

    finally:
        _limpiar()
    _verificar_limpieza()
    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_buscador_bodega_despachos_monza():
    run()


if __name__ == "__main__":
    run()
