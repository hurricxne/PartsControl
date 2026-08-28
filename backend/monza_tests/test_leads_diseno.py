"""Leads: insignia de motivo, contador de ventas, tasa de cierre y ventas cerradas.

Hallazgos del equipo de testing y del comité de revisión (2026-08-27):

  · La fila no decía POR QUÉ calzó. Se busca por repuesto, patente, VIN y N° de
    cotización, y ninguno de esos datos está en la tabla: el vendedor recibía filas de
    aspecto idéntico y tenía que abrirlas una por una. La capacidad de búsqueda quedaba
    construida y prácticamente inusable.
  · `vendidos_total` se duplicaba: el cierre de la venta lo suma, el despacho deja el
    lead en 'cerrado', y el asesor que abre ese lead y lo marca «vendido» —lo natural—
    lo sumaba otra vez.
  · «Tasa de cierre 300%»: dividía VENTAS del mes (que vienen de leads viejos, el ciclo
    de importación dura semanas) por LEADS NUEVOS del mes. Dos universos distintos.
  · Un lead con la venta despachada volvía al embudo con un clic, y se podía ELIMINAR
    dejando la venta sin su origen — ambas cosas en silencio.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth import get_current_user
from database import SessionLocal
from monza_models import (MonzaCliente, MonzaCotizacion, MonzaCotizacionCierre, MonzaLead,
                          MonzaLeadActividad, MonzaLeadItem, MonzaLog)
from monza_router_leads import router as leads_router

MARK = "test-leads-dis"
EMAIL = f"{MARK}@test.invalid"
LEAD = "L-DIS"
COT = "COT-DIS"


class _Usuario:
    id, email, empresa, rol = 1, EMAIL, "automotriz", "admin"


@pytest.fixture()
def cli():
    app = FastAPI()
    app.include_router(leads_router)
    app.dependency_overrides[get_current_user] = lambda: _Usuario()
    return TestClient(app)


def _limpiar():
    db, S = SessionLocal(), "fetch"
    ids = [r[0] for r in db.query(MonzaCliente.id).filter(MonzaCliente.nombre.like(f"{MARK}%")).all()]
    lids = [r[0] for r in db.query(MonzaLead.id).filter(MonzaLead.numero.like(f"{LEAD}%")).all()]
    _cots = [r[0] for r in db.query(MonzaCotizacion.id).filter(MonzaCotizacion.numero.like(f"{COT}%")).all()]
    db.query(MonzaCotizacionCierre).filter(MonzaCotizacionCierre.cotizacion_id.in_(_cots or [0])).delete(synchronize_session=S)
    db.query(MonzaCotizacion).filter(MonzaCotizacion.id.in_(_cots or [0])).delete(synchronize_session=S)
    db.query(MonzaLeadActividad).filter(MonzaLeadActividad.lead_id.in_(lids or [0])).delete(synchronize_session=S)
    db.query(MonzaLeadItem).filter(MonzaLeadItem.lead_id.in_(lids or [0])).delete(synchronize_session=S)
    db.query(MonzaLead).filter(MonzaLead.id.in_(lids or [0])).delete(synchronize_session=S)
    db.query(MonzaLog).filter(MonzaLog.user_email == EMAIL).delete(synchronize_session=S)
    db.query(MonzaCliente).filter(MonzaCliente.id.in_(ids or [0])).delete(synchronize_session=S)
    db.commit()
    db.close()


@pytest.fixture(autouse=True)
def limpieza():
    _limpiar()
    yield
    _limpiar()


def _sembrar_lead_completo():
    """Un lead con TODOS los datos por los que se puede buscar."""
    db = SessionLocal()
    c = MonzaCliente(nombre=f"{MARK} TALLER SUR", rut="76.555.444-3", telefono="+56 9 8877 6655")
    db.add(c)
    db.flush()
    l = MonzaLead(numero=f"{LEAD}-0001", cliente_id=c.id, estado="pendiente",
                  vehiculo="Toyota Hilux", vin="ZZVIN0001",
                  comentario="Patente KLZR99 - campaña web")
    db.add(l)
    db.flush()
    db.add(MonzaLeadItem(lead_id=l.id, descripcion="Amortiguador delantero ZZUNICO",
                         numero_parte="AMT-9911", cantidad=1))
    db.add(MonzaCotizacion(numero=f"{COT}-0001", lead_id=l.id, cliente_id=c.id, estado="propuesta"))
    db.commit()
    ids = (l.id, c.id)
    db.close()
    return ids


# ─────────────── La insignia de motivo ───────────────

@pytest.mark.parametrize("termino,campo", [
    ("ZZUNICO", "repuesto"),          # descripción: lo que llenan los leads del bridge
    ("AMT-9911", "numero_parte"),
    ("KLZR99", "comentario"),         # la patente, enterrada en el comentario
    (f"{COT}-0001", "cotizacion"),
    ("ZZVIN0001", "vin"),
    ("76555444-3", "rut"),            # RUT en otro formato que el guardado
    ("988776655", "telefono"),        # teléfono como lo tiene en WhatsApp
    ("Hilux", "vehiculo"),
    ("TALLER SUR", "cliente"),
])
def test_la_fila_dice_por_que_calzo(cli, termino, campo):
    _sembrar_lead_completo()
    r = cli.get("/api/monza/leads", params={"q": termino})
    assert r.status_code == 200, r.text
    fila = next((x for x in r.json()["items"] if x["numero"] == f"{LEAD}-0001"), None)
    assert fila is not None, f"'{termino}' no encontró el lead"
    campos = [m["campo"] for m in fila.get("match", [])]
    assert campo in campos, f"buscando '{termino}' la insignia debía decir '{campo}', dijo {campos}"


def test_sin_busqueda_no_hay_insignia(cli):
    """La tabla normal no se ensucia con insignias que no explican nada."""
    _sembrar_lead_completo()
    r = cli.get("/api/monza/leads")
    fila = next(x for x in r.json()["items"] if x["numero"] == f"{LEAD}-0001")
    assert fila["match"] == []


# ─────────────── El contador de ventas de la ficha ───────────────

def _contador(cid):
    db = SessionLocal()
    v = (db.query(MonzaCliente).filter(MonzaCliente.id == cid).first().vendidos_total or 0)
    db.close()
    return v


def test_marcar_vendido_un_lead_ya_cerrado_no_vuelve_a_contar(cli):
    """SONDA: cerrar la venta suma, el despacho deja el lead en 'cerrado', y el asesor
    que lo marca «vendido» —lo natural— sumaba una segunda venta que no existió."""
    lid, cid = _sembrar_lead_completo()
    assert cli.patch(f"/api/monza/leads/{lid}", json={"estado": "vendido"}).status_code == 200
    assert _contador(cid) == 1

    db = SessionLocal()
    db.query(MonzaLead).filter(MonzaLead.id == lid).update({"estado": "cerrado"})
    db.commit()
    db.close()

    assert cli.patch(f"/api/monza/leads/{lid}", json={"estado": "vendido"}).status_code == 200
    assert _contador(cid) == 1, "la ficha contó dos ventas donde hubo una sola"


def test_una_venta_nueva_si_suma(cli):
    """El guard no puede congelar el contador: un lead reabierto y vuelto a ganar cuenta."""
    lid, cid = _sembrar_lead_completo()
    cli.patch(f"/api/monza/leads/{lid}", json={"estado": "vendido"})
    db = SessionLocal()
    db.query(MonzaLead).filter(MonzaLead.id == lid).update({"estado": "en_proceso"})
    db.commit()
    db.close()
    cli.patch(f"/api/monza/leads/{lid}", json={"estado": "vendido"})
    assert _contador(cid) == 2


# ─────────────── La tasa de cierre ───────────────

def test_la_tasa_de_cierre_nunca_pasa_de_cien(cli):
    """SONDA: con ventas del mes y pocos leads nuevos, la tarjeta daba «300%»."""
    lid, _ = _sembrar_lead_completo()
    cli.patch(f"/api/monza/leads/{lid}", json={"estado": "vendido"})
    r = cli.get("/api/monza/leads/kpis")
    assert r.status_code == 200
    tasa = r.json()["tasa_cierre_pct"]
    assert tasa is None or 0 <= tasa <= 100, f"tasa fuera de rango: {tasa}"


def test_la_tasa_es_null_cuando_no_hay_con_que_medir():
    """Sin leads del mes no hay tasa. Un 0% mentiría diciendo «lo hacemos pésimo»."""
    from monza_router_leads import get_kpis as kpis_endpoint
    db = SessionLocal()
    try:
        from monza_fechas import inicio_mes_utc
        hay_leads_del_mes = db.query(MonzaLead).filter(
            MonzaLead.fecha_creacion >= inicio_mes_utc()).count()
        datos = kpis_endpoint(db=db)
        if hay_leads_del_mes == 0:
            assert datos["tasa_cierre_pct"] is None
        else:
            # Con leads del mes hay tasa: se comprueba que sea un porcentaje sensato.
            assert 0 <= datos["tasa_cierre_pct"] <= 100
    finally:
        db.close()


# ─────────────── El lead con venta cerrada ───────────────

def _cerrar_la_venta(lid):
    db = SessionLocal()
    db.query(MonzaCotizacion).filter(MonzaCotizacion.lead_id == lid).update({"estado": "despachado"})
    db.query(MonzaLead).filter(MonzaLead.id == lid).update({"estado": "cerrado"})
    db.commit()
    db.close()


def test_no_se_puede_reabrir_un_lead_con_venta_despachada(cli):
    """Volvía al embudo con un clic: contado como oportunidad abierta y como venta."""
    lid, _ = _sembrar_lead_completo()
    _cerrar_la_venta(lid)
    r = cli.patch(f"/api/monza/leads/{lid}", json={"estado": "en_proceso"})
    assert r.status_code == 409, f"debía rebotar, dio {r.status_code}"
    assert f"{COT}-0001" in r.json()["detail"], "el mensaje debe nombrar la venta"


def test_no_se_puede_eliminar_un_lead_con_venta_cerrada(cli):
    """Devolvía 204 en silencio y la venta perdía de qué consulta nació."""
    lid, _ = _sembrar_lead_completo()
    _cerrar_la_venta(lid)
    r = cli.delete(f"/api/monza/leads/{lid}")
    assert r.status_code == 409, f"debía rebotar, dio {r.status_code}"
    assert f"{COT}-0001" in r.json()["detail"]

    db = SessionLocal()
    assert db.query(MonzaLead).filter(MonzaLead.id == lid).first() is not None
    db.close()


def test_un_lead_sin_ventas_se_elimina_igual_que_siempre(cli):
    """El guard no puede impedir limpiar los duplicados, que es para lo que se usa."""
    db = SessionLocal()
    c = MonzaCliente(nombre=f"{MARK} SUELTO")
    db.add(c)
    db.flush()
    l = MonzaLead(numero=f"{LEAD}-SUELTO", cliente_id=c.id, estado="pendiente")
    db.add(l)
    db.commit()
    lid = l.id
    db.close()

    assert cli.delete(f"/api/monza/leads/{lid}").status_code == 204
    db = SessionLocal()
    assert db.query(MonzaLead).filter(MonzaLead.id == lid).first() is None
    db.close()


def test_una_cotizacion_solo_propuesta_no_bloquea(cli):
    """Solo las ventas CERRADAS ('vendida'/'despachado') bloquean: una cotización que
    todavía es propuesta no es una venta, y ese lead debe poder cerrarse o borrarse."""
    lid, _ = _sembrar_lead_completo()   # deja la cotización en 'propuesta'
    assert cli.patch(f"/api/monza/leads/{lid}", json={"estado": "rechazado"}).status_code == 200
    assert cli.delete(f"/api/monza/leads/{lid}").status_code == 204


# ─────────────── El monto de la tarjeta «Vendidos» ───────────────

def test_el_monto_de_vendidos_es_el_de_las_mismas_ventas_que_cuenta(cli):
    """La tarjeta decía «3 vendidos · Total $476.000» cuando esas ventas sumaban otra cosa.

    El número contaba por `fecha_venta` y el monto sumaba por `fecha_creacion`: dos
    cohortes distintas en la misma tarjeta. `total_cotizado_mes` sigue existiendo con su
    propio significado (lo consume el Dashboard); lo que se agregó es el par honesto.
    """
    r = cli.get("/api/monza/leads/kpis")
    assert r.status_code == 200
    datos = r.json()
    assert "total_vendido_mes" in datos, "falta el monto que acompaña a `vendidos_mes`"
    assert datos["total_vendido_mes"] >= 0
    # Si no hay ventas del mes, el monto tiene que ser 0 — no el de otra cohorte.
    if datos["vendidos_mes"] == 0:
        assert datos["total_vendido_mes"] == 0


# ─────────────── El contador ante un RE-cierre ───────────────

def test_recerrar_la_misma_venta_no_cuenta_una_segunda(cli):
    """SONDA: cerrar → revertir → reabrir el lead → re-cerrar es un camino permitido
    (los tres pasos devuelven 200) y sumaba DOS ventas donde hubo una sola.

    La transición del lead no alcanza como evidencia de «ya se contó»; las versiones de
    cierre de la cotización sí. Verificado por mutación: sin el guard, el contador da 2.
    """
    db = SessionLocal()
    c = MonzaCliente(nombre=f"{MARK} RECIERRE", vendidos_total=0)
    db.add(c)
    db.flush()
    lead = MonzaLead(numero=f"{LEAD}-RC", cliente_id=c.id, estado="en_proceso")
    db.add(lead)
    db.flush()
    cot = MonzaCotizacion(numero=f"{COT}-RC", lead_id=lead.id, cliente_id=c.id,
                          estado="enviada", total_neto=100000, total_bruto=119000, iva_pct=19)
    db.add(cot)
    db.flush()
    db.add(MonzaLeadItem(lead_id=lead.id, descripcion="P", cantidad=1))
    db.commit()
    cid, lid, cot_id = c.id, lead.id, cot.id
    db.close()

    from monza_router_cotizaciones import router as cot_router
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(cot_router)
    app.dependency_overrides[get_current_user] = lambda: _Usuario()
    cotc = TestClient(app)

    cierre = {"estado": "vendida", "oc_cliente": "OC-RC", "oc_fecha": "2026-08-01",
              "pct_adelanto": 0, "forma_pago": "contado"}
    assert cotc.patch(f"/api/monza/cotizaciones/{cot_id}", json=cierre).status_code == 200
    assert _contador(cid) == 1

    assert cotc.patch(f"/api/monza/cotizaciones/{cot_id}",
                      json={"estado": "propuesta", "motivo_reversion": "se cayó"}).status_code == 200
    assert cli.patch(f"/api/monza/leads/{lid}", json={"estado": "en_proceso"}).status_code == 200
    assert cotc.patch(f"/api/monza/cotizaciones/{cot_id}", json={"estado": "enviada"}).status_code == 200
    assert cotc.patch(f"/api/monza/cotizaciones/{cot_id}", json=cierre).status_code == 200

    assert _contador(cid) == 1, "el re-cierre de la MISMA venta contó una segunda"
