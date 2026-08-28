"""La pestaña Ventas lista SOLO ventas — y sus KPIs cuentan lo mismo (arreglo 7, 2026-08-21).

EL PROBLEMA QUE CIERRA
    El filtro viejo era ["vendida", "propuesta", "enviada"]: mezclaba cotizaciones no
    vendidas (que viven en la pestaña Cotizaciones) y EXCLUÍA las despachadas — una venta
    desaparecía de la pestaña el día que salía a reparto. Y los KPIs contaban solo
    estado=='vendida', así que con el filtro nuevo la lista mostraría despachadas que las
    tarjetas de arriba (y el Dashboard, y los KPIs de Leads) restaban.

SONDAS DE PODER DISCRIMINANTE (doble RED contra el código viejo)
    · §1 "la despachada APARECE" cae con el filtro antiguo (la excluía).
    · §1 "la enviada NO aparece" cae con el filtro antiguo (la incluía).
    · §3/§4 los KPIs van por DELTA (antes vs después del seed) porque corren contra la
      base real: la despachada del mes debe SUMAR en vendidas_mes/total_mes y NO sumar
      en pendientes_entrega; en Leads, vendidos_mes/total_cotizado_mes suben en 2.

Sin red. Datos con MARK propio y limpieza verificada al final.

Corre con:  ./venv/bin/python -m pytest monza_tests/test_ventas_solo_ventas.py -q
"""
import os
import sys
import uuid
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import SessionLocal  # noqa: E402
from auth import get_current_user  # noqa: E402
from monza_models import MonzaCliente, MonzaCotizacion, MonzaCotizacionItem, MonzaLog  # noqa: E402
from monza_router_leads import router as leads_router  # noqa: E402
from monza_router_ventas import router as ventas_router  # noqa: E402

MARK = "test-mzventas"
COT_MARK = "CVTS"          # MonzaCotizacion.numero: prefijo corto propio
EMAIL = f"{MARK}@test.invalid"

app = FastAPI()
app.include_router(ventas_router)
app.include_router(leads_router)
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=1, email=EMAIL, empresa="automotriz")
client = TestClient(app)

_fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


ESTADOS_SEED = ["propuesta", "enviada", "vendida", "despachado", "rechazada"]
TOTAL_VENDIDA = 111000
TOTAL_DESPACHADA = 222000


def _seed():
    """5 cotizaciones MARK, una por estado; las cerradas con fecha_venta DE ESTE MES."""
    db = SessionLocal()
    try:
        cli = MonzaCliente(nombre=f"{MARK} SpA", rut="76.000.000-0")
        db.add(cli)
        db.flush()
        ahora = datetime.utcnow()
        ids = {}
        for estado in ESTADOS_SEED:
            total = (TOTAL_VENDIDA if estado == "vendida"
                     else TOTAL_DESPACHADA if estado == "despachado" else 99000)
            cot = MonzaCotizacion(
                numero=f"{COT_MARK}-{uuid.uuid4().hex[:6].upper()}",
                cliente_id=cli.id,
                estado=estado,
                total_neto=round(total / 1.19),
                iva_monto=total - round(total / 1.19),
                total_bruto=total,
                fecha_venta=(ahora if estado in ("vendida", "despachado") else None),
                # Las DOS cerradas con fecha prometida: prueba que pendientes_entrega
                # cuenta la vendida y EXCLUYE la despachada (ya no está pendiente).
                fecha_entrega_est=(ahora.date() if estado in ("vendida", "despachado") else None),
            )
            db.add(cot)
            db.flush()
            db.add(MonzaCotizacionItem(cotizacion_id=cot.id, descripcion="Repuesto",
                                       cantidad=1, precio_unitario_clp=total,
                                       subtotal_clp=total, estado_linea="cotizado"))
            ids[estado] = cot.id
        db.commit()
        return ids
    finally:
        db.close()


def _kpis_ventas():
    r = client.get("/api/monza/ventas/kpis")
    assert r.status_code == 200, r.text[:200]
    return r.json()


def _kpis_leads():
    r = client.get("/api/monza/leads/kpis")
    assert r.status_code == 200, r.text[:200]
    return r.json()


def _limpiar():
    db = SessionLocal()
    try:
        S = "fetch"
        cot_ids = [r[0] for r in db.query(MonzaCotizacion.id)
                   .filter(MonzaCotizacion.numero.like(f"{COT_MARK}%")).all()]
        db.query(MonzaCotizacionItem).filter(
            MonzaCotizacionItem.cotizacion_id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaCotizacion).filter(
            MonzaCotizacion.id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaLog).filter(MonzaLog.user_email == EMAIL).delete(synchronize_session=S)
        db.query(MonzaCliente).filter(MonzaCliente.nombre.like(f"{MARK}%")).delete(
            synchronize_session=S)
        db.commit()
    finally:
        db.close()


def _verificar_limpieza():
    db = SessionLocal()
    try:
        restos = (db.query(MonzaCotizacion).filter(
            MonzaCotizacion.numero.like(f"{COT_MARK}%")).count()
            + db.query(MonzaCliente).filter(MonzaCliente.nombre.like(f"{MARK}%")).count())
        assert restos == 0, f"quedaron {restos} filas {MARK} tras la limpieza"
    finally:
        db.close()
    print("Cleanup OK (verificado con sesión nueva)")


def run():
    _limpiar()
    try:
        antes_v = _kpis_ventas()
        antes_l = _kpis_leads()
        ids = _seed()

        # ── 1) La lista trae SOLO ventas cerradas ─────────────────────────────────────
        r = client.get("/api/monza/ventas", params={"q": MARK, "page_size": 50})
        check("1a la lista responde 200", r.status_code == 200, r.text[:200])
        estados = sorted(x["estado"] for x in r.json()["items"])
        check("1b SONDA (RED con el filtro viejo): la DESPACHADA aparece",
              "despachado" in estados, estados)
        check("1c la vendida aparece", "vendida" in estados, estados)
        check("1d SONDA (RED con el filtro viejo): enviada/propuesta NO aparecen",
              "enviada" not in estados and "propuesta" not in estados, estados)
        check("1e la rechazada tampoco", "rechazada" not in estados, estados)
        check("1f son exactamente 2 filas", len(estados) == 2, estados)

        # ── 2) El param estado filtra DENTRO del par ──────────────────────────────────
        r = client.get("/api/monza/ventas", params={"q": MARK, "estado": "despachado"})
        check("2a estado=despachado trae solo esa", [x["estado"] for x in r.json()["items"]] == ["despachado"],
              r.json()["items"])
        r = client.get("/api/monza/ventas", params={"q": MARK, "estado": "vendida"})
        check("2b estado=vendida trae solo esa", [x["estado"] for x in r.json()["items"]] == ["vendida"],
              r.json()["items"])
        r = client.get("/api/monza/ventas", params={"q": MARK, "estado": "todas"})
        check("2c estado=todas trae las 2", len(r.json()["items"]) == 2, r.json()["items"])

        # ── 3) _venta_dict de la despachada viaja íntegro ─────────────────────────────
        r = client.get("/api/monza/ventas", params={"q": MARK, "estado": "despachado"})
        fila = r.json()["items"][0]
        check("3a id/numero/total/cliente presentes",
              fila["id"] == ids["despachado"] and fila["total_bruto"] == TOTAL_DESPACHADA
              and (fila["cliente"] or {}).get("nombre", "").startswith(MARK), fila)
        check("3b fecha_venta viaja (el corte mensual de los KPIs depende de ella)",
              bool(fila["fecha_venta"]), fila)

        # ── 4) KPIs de la MISMA pestaña, por DELTA ────────────────────────────────────
        despues_v = _kpis_ventas()
        check("4a vendidas_mes sube en 2 (la vendida Y la despachada del mes)",
              despues_v["vendidas_mes"] - antes_v["vendidas_mes"] == 2,
              (antes_v["vendidas_mes"], despues_v["vendidas_mes"],
               "con el filtro viejo subiría solo 1"))
        check("4b total_mes sube en la suma de AMBAS",
              round(despues_v["total_mes"] - antes_v["total_mes"]) == TOTAL_VENDIDA + TOTAL_DESPACHADA,
              (antes_v["total_mes"], despues_v["total_mes"]))
        check("4c pendientes_entrega sube SOLO en 1: la despachada ya no está pendiente",
              despues_v["pendientes_entrega"] - antes_v["pendientes_entrega"] == 1,
              (antes_v["pendientes_entrega"], despues_v["pendientes_entrega"]))

        # ── 5) KPIs de Leads (mismo par: tasa de cierre y Dashboard dependen de esto) ──
        despues_l = _kpis_leads()
        check("5a leads.vendidos_mes sube en 2",
              despues_l["vendidos_mes"] - antes_l["vendidos_mes"] == 2,
              (antes_l["vendidos_mes"], despues_l["vendidos_mes"]))
        check("5b leads.total_cotizado_mes sube en la suma de ambas",
              round(despues_l["total_cotizado_mes"] - antes_l["total_cotizado_mes"])
              == TOTAL_VENDIDA + TOTAL_DESPACHADA,
              (antes_l["total_cotizado_mes"], despues_l["total_cotizado_mes"]))

    finally:
        _limpiar()
    _verificar_limpieza()
    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_ventas_solo_ventas():
    run()


if __name__ == "__main__":
    run()
