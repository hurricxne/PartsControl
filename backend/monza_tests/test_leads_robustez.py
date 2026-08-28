"""Robustez de la pantalla de Leads: fechas nulas, teléfono y correlativo simultáneo.

Tres hallazgos ALTOS del equipo de testing (2026-08-27), los tres reproducidos:

  1. UN SOLO lead con `fecha_creacion`/`fecha_actualizacion` en NULL tumbaba la lista
     ENTERA con un 500. `_lead_dict` corre por cada fila de la página, así que la fila
     mala se llevaba a las otras 29. Y como el orden es `fecha_creacion DESC` y MySQL
     manda los NULL al final, esas filas caen en las ÚLTIMAS páginas: la pantalla andaba
     en la página 1 y se caía al llegar a los leads viejos — el síntoma exacto que
     reportó el dueño («solo veo los primeros»). Productor conocido: la migración desde
     Postgres, cuyo `parse_dt` devuelve None ante una fecha ilegible.
  2. El buscador comparaba el TELÉFONO literal, así que el vendedor que copia el número
     desde WhatsApp no encontraba a un cliente que sí existe.
  3. Dos creaciones simultáneas —dos vendedores, o un vendedor y el webhook de Nexor—
     calculaban el mismo correlativo y la segunda moría con un 500, perdiendo el lead
     que se estaba tipeando.
"""
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from auth import get_current_user
from database import SessionLocal
from monza_models import (MonzaCliente, MonzaLead, MonzaLeadActividad, MonzaLeadItem,
                          MonzaLog)
from monza_router_leads import router as leads_router

MARK = "test-leads-rob"
EMAIL = f"{MARK}@test.invalid"
PREFIJO = "L-ROB"


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
    from monza_models import MonzaCotizacion, MonzaCotizacionCierre, MonzaCotizacionItem
    _cots = [r[0] for r in db.query(MonzaCotizacion.id).filter(MonzaCotizacion.numero.like("CSX-%")).all()]
    db.query(MonzaCotizacionCierre).filter(MonzaCotizacionCierre.cotizacion_id.in_(_cots or [0])).delete(synchronize_session=S)
    db.query(MonzaCotizacionItem).filter(MonzaCotizacionItem.cotizacion_id.in_(_cots or [0])).delete(synchronize_session=S)
    db.query(MonzaCotizacion).filter(MonzaCotizacion.id.in_(_cots or [0])).delete(synchronize_session=S)
    ids = [r[0] for r in db.query(MonzaCliente.id).filter(MonzaCliente.nombre.like(f"{MARK}%")).all()]
    lids = [r[0] for r in db.query(MonzaLead.id).filter(
        MonzaLead.cliente_id.in_(ids or [0]) | MonzaLead.numero.like(f"{PREFIJO}%")).all()]
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


# ─────────────── 1. La fila con fecha NULL ───────────────

def test_un_lead_con_fechas_nulas_no_tumba_la_lista(cli):
    """SONDA: la fila mala se pinta con lo que hay y las demás siguen llegando."""
    db = SessionLocal()
    c = MonzaCliente(nombre=f"{MARK} CLI")
    db.add(c)
    db.flush()
    bueno = MonzaLead(numero=f"{PREFIJO}-OK", cliente_id=c.id, estado="pendiente")
    malo = MonzaLead(numero=f"{PREFIJO}-NULL", cliente_id=c.id, estado="pendiente")
    db.add_all([bueno, malo])
    db.flush()
    malo_id = malo.id
    # Exactamente lo que deja la migración desde Postgres cuando parse_dt no entiende
    # la fecha: NULL en las dos columnas.
    db.execute(text("UPDATE monza_leads SET fecha_creacion=NULL, fecha_actualizacion=NULL "
                    "WHERE id=:i"), {"i": malo_id})
    db.commit()
    db.close()

    r = cli.get("/api/monza/leads", params={"q": PREFIJO, "page_size": 50})
    assert r.status_code == 200, f"la lista se cayó por una fila con fechas nulas: {r.text[:200]}"
    assert r.json()["total"] == 2, "los leads sanos de la misma página deben seguir llegando"

    fila = next(x for x in r.json()["items"] if x["numero"] == f"{PREFIJO}-NULL")
    assert fila["fecha_creacion"] is None, "la fecha ausente se informa como tal, no se inventa"
    assert fila["sin_contactar_dias"] == 0

    assert cli.get(f"/api/monza/leads/{malo_id}").status_code == 200, "el detalle también debe abrir"


# ─────────────── 2. El teléfono como lo teclea el vendedor ───────────────

@pytest.mark.parametrize("tecleado", ["988877766", "+56988877766", "9 8887 7766",
                                      "56988877766", "+56 9 8887 7766"])
def test_busca_por_telefono_en_cualquier_formato(cli, tecleado):
    """La ficha guarda un formato y el vendedor teclea otro: deben encontrarse igual."""
    db = SessionLocal()
    c = MonzaCliente(nombre=f"{MARK} WHATSAPP", telefono="+56 9 8887 7766")
    db.add(c)
    db.flush()
    db.add(MonzaLead(numero=f"{PREFIJO}-TEL", cliente_id=c.id, estado="pendiente"))
    db.commit()
    db.close()

    r = cli.get("/api/monza/leads", params={"q": tecleado})
    assert r.status_code == 200
    assert r.json()["total"] >= 1, f"'{tecleado}' no encontró al cliente que sí existe"


def test_el_piso_de_digitos_es_del_helper_no_del_endpoint():
    """El piso de 8 dígitos vive en `telefono_identidad` y se prueba AHÍ.

    Probarlo por el endpoint no discriminaría nada: la búsqueda tiene además la rama
    CRUDA de siempre (`telefono ILIKE %q%`), que encuentra «22» dentro de «+56 9 2222
    2222» aunque la rama normalizada no se encienda. Una sonda de endpoint pasaría en
    verde con el piso puesto y con el piso quitado — no probaría nada. El valor añadido
    de la rama normalizada es lo que la cruda NO puede hacer, y eso es justo lo que
    verifica `test_busca_por_telefono_en_cualquier_formato`.
    """
    from monza_telefono import telefono_identidad
    for corto in ("22", "2342", "0", "-", "", None, "123456", "  "):
        assert telefono_identidad(corto) == "", f"'{corto}' no debería identificar a nadie"
    for bueno, esperado in (("22345678", "22345678"), ("+56 9 8887 7766", "988877766"),
                            ("56988877766", "988877766")):
        assert telefono_identidad(bueno) == esperado


# ─────────────── 3. El correlativo bajo creaciones simultáneas ───────────────

def test_ocho_leads_simultaneos_se_crean_todos(cli):
    """SONDA de concurrencia real: sin el reintento, uno pasa y el resto da 500.

    Ocho es muy por encima de lo que ocurre en la práctica (dos vendedores y el webhook),
    y sirve justamente para que el margen quede probado, no supuesto.
    """
    N = 8
    resultados = [None] * N
    barrera = threading.Barrier(N)

    def crear(i):
        barrera.wait()  # los N arrancan en el MISMO instante
        try:
            r = cli.post("/api/monza/leads", json={
                "cliente": {"nombre": f"{MARK} C{i}", "telefono": f"9111100{i:02d}"},
                "marca": "Toyota"})
            resultados[i] = (r.status_code, r.json().get("numero") if r.status_code == 201 else r.text[:120])
        except Exception as e:  # noqa: BLE001 - se reporta al assert, no se traga
            resultados[i] = ("EXC", f"{type(e).__name__}: {e}"[:150])

    hilos = [threading.Thread(target=crear, args=(i,)) for i in range(N)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()

    fallidos = [r for r in resultados if r[0] != 201]
    assert not fallidos, f"creaciones simultáneas caídas: {fallidos}"

    numeros = [r[1] for r in resultados]
    assert len(set(numeros)) == N, f"correlativos repetidos: {numeros}"

    db = SessionLocal()
    ids = [r[0] for r in db.query(MonzaCliente.id).filter(MonzaCliente.nombre.like(f"{MARK} C%")).all()]
    guardados = db.query(MonzaLead).filter(MonzaLead.cliente_id.in_(ids or [0])).count()
    db.close()
    assert guardados == N, f"se respondió 201 pero en la base quedaron {guardados} de {N}"


def test_ocho_leads_simultaneos_del_MISMO_cliente(cli):
    """SONDA que le faltaba a la de arriba, y es la que importa.

    La prueba de «clientes distintos» pasaba 8/8 mientras ESTA forma estaba rota: dos
    leads del mismo cliente no chocan en el UNIQUE del correlativo sino en un DEADLOCK
    (uno toma el lock del índice de `numero`, el otro el de la fila del cliente, en orden
    cruzado). Medido antes del arreglo: 2 de 6 sobrevivían.

    Y es la forma MÁS frecuente en la operación real: el mismo cliente llamando y dos
    personas registrándolo, el webhook de Nexor entrando sobre una ficha que un vendedor
    está atendiendo, o el bridge insertando en ráfaga sobre la misma ficha.
    """
    db = SessionLocal()
    c = MonzaCliente(nombre=f"{MARK} EL MISMO")
    db.add(c)
    db.commit()
    cid = c.id
    db.close()

    N = 6
    resultados = [None] * N
    barrera = threading.Barrier(N)

    def crear(i):
        barrera.wait()
        try:
            r = cli.post("/api/monza/leads",
                         json={"cliente_id": cid, "marca": "Toyota", "modelo": f"M{i}"})
            resultados[i] = (r.status_code, r.json().get("numero") if r.status_code == 201 else r.text[:150])
        except Exception as e:  # noqa: BLE001
            resultados[i] = ("EXC", f"{type(e).__name__}: {e}"[:180])

    hilos = [threading.Thread(target=crear, args=(i,)) for i in range(N)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()

    fallidos = [r for r in resultados if r[0] != 201]
    assert not fallidos, f"leads perdidos con el mismo cliente: {fallidos}"
    assert len({r[1] for r in resultados}) == N, f"correlativos repetidos: {resultados}"


def test_dos_patch_vendido_simultaneos_cuentan_UNA_venta(cli):
    """SONDA: el lock tiene que estar sobre el LEAD, que es donde se DECIDE.

    `vendidos_total` se protegió primero bloqueando la ficha del cliente, que es donde se
    ESCRIBE. No alcanzaba: la evidencia que decide si esta venta ya se contó es
    `old_estado`, y vive en la fila del LEAD. Sin bloquear esa fila, dos PATCH simultáneos
    con estado='vendido' leen los dos el mismo estado abierto, los dos pasan el guard, y
    la ficha termina con DOS ventas donde hubo una.

    Verificado por mutación: quitando el `with_for_update()` del lead, las 3 rondas fallan.
    """
    db = SessionLocal()
    c = MonzaCliente(nombre=f"{MARK} DOBLE PATCH", vendidos_total=0)
    db.add(c)
    db.flush()
    lead = MonzaLead(numero=f"{PREFIJO}-DP", cliente_id=c.id, estado="en_proceso")
    db.add(lead)
    db.commit()
    cid, lid = c.id, lead.id
    db.close()

    barrera = threading.Barrier(2)
    codigos = [None, None]

    def patch(i):
        barrera.wait()
        try:
            codigos[i] = cli.patch(f"/api/monza/leads/{lid}", json={"estado": "vendido"}).status_code
        except Exception as e:  # noqa: BLE001
            codigos[i] = type(e).__name__

    hilos = [threading.Thread(target=patch, args=(i,)) for i in (0, 1)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()

    verif = SessionLocal()
    contados = verif.query(MonzaCliente).filter(MonzaCliente.id == cid).first().vendidos_total or 0
    verif.close()
    assert contados == 1, f"la ficha contó {contados} ventas donde hubo una (códigos: {codigos})"


def test_cierre_de_venta_y_patch_del_asesor_simultaneos(cli):
    """SONDA: los DOS caminos que deciden sobre el estado del lead, a la vez.

    El lock se puso primero solo en el PATCH del asesor. El cierre de la venta —el otro
    decisor del mismo contador— seguía leyendo la fila del lead sin bloquearla, así que
    un cierre y un PATCH simultáneos leían ambos el mismo estado abierto y la ficha
    terminaba con DOS ventas, con 200 en las dos peticiones (silencioso).

    Verificado por mutación: quitando el `with_for_update()` del cierre, la sonda falla.
    Se repite la carrera 5 veces porque no es determinista.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from monza_models import MonzaCotizacion, MonzaCotizacionItem
    from monza_router_cotizaciones import router as cot_router

    app = FastAPI()
    app.include_router(cot_router)
    app.dependency_overrides[get_current_user] = lambda: _Usuario()
    cotc = TestClient(app)

    for ronda in range(5):
        db = SessionLocal()
        c = MonzaCliente(nombre=f"{MARK} SX{ronda}", vendidos_total=0)
        db.add(c)
        db.flush()
        lead = MonzaLead(numero=f"{PREFIJO}-SX{ronda}", cliente_id=c.id, estado="en_proceso")
        db.add(lead)
        db.flush()
        cot = MonzaCotizacion(numero=f"CSX-{PREFIJO}-{ronda}", lead_id=lead.id, cliente_id=c.id,
                              estado="enviada", total_neto=100000, total_bruto=119000, iva_pct=19)
        db.add(cot)
        db.flush()
        db.add(MonzaCotizacionItem(cotizacion_id=cot.id, descripcion="P", cantidad=1,
                                   precio_unitario_clp=100000, subtotal_clp=100000))
        db.commit()
        cid, lid, cotid = c.id, lead.id, cot.id
        db.close()

        barrera = threading.Barrier(2)

        def cerrar():
            barrera.wait()
            cotc.patch(f"/api/monza/cotizaciones/{cotid}",
                       json={"estado": "vendida", "oc_cliente": "OC", "oc_fecha": "2026-08-01",
                             "pct_adelanto": 0, "forma_pago": "contado"})

        def marcar():
            barrera.wait()
            cli.patch(f"/api/monza/leads/{lid}", json={"estado": "vendido"})

        hilos = [threading.Thread(target=cerrar), threading.Thread(target=marcar)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()

        verif = SessionLocal()
        contados = verif.query(MonzaCliente).filter(MonzaCliente.id == cid).first().vendidos_total or 0
        verif.close()

        limpia = SessionLocal()
        limpia.query(MonzaCotizacionItem).filter(MonzaCotizacionItem.cotizacion_id == cotid).delete(synchronize_session="fetch")
        limpia.query(MonzaCotizacion).filter(MonzaCotizacion.id == cotid).delete(synchronize_session="fetch")
        limpia.commit()
        limpia.close()

        assert contados == 1, f"ronda {ronda}: la ficha contó {contados} ventas donde hubo una"
