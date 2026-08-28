"""Leads PASADOS: paginación real, búsqueda honesta y «Míos» que funciona (2026-08-22).

LO QUE RECLAMÓ EL EQUIPO
    «En la página de leads solo se ven los primeros; después no se pueden buscar leads
    pasados». La causa era triple y todas se prueban acá:
      1. El backend paginaba pero la pantalla nunca mandaba `page` (y el pie no tenía
         botones): solo los 30 más recientes eran alcanzables.
      2. El buscador prometía VIN, N° de parte y N° de cotización y no los buscaba.
      3. El filtro de estado no alcanzaba a los leads GANADOS (que terminan en
         'cerrado'), y el checkbox «Míos» comparaba ids de TABLAS distintas.

SONDAS DE PODER DISCRIMINANTE
    · §2 siembra 12 leads con la MISMA fecha_creacion (empate real: el bridge de leads
      inserta en ráfaga) y afirma que la unión de las páginas es COMPLETA y SIN
      repetidos — sin el desempate por id, MySQL puede repetir o saltarse filas.
    · §3 usa un token único por vía (VIN, N° parte, COT, RUT) + un control negativo: si
      el token se colara por otro campo, el check no probaría la rama que dice probar.
    · §4 «Míos» siembra un asesor cuyo id NO coincide con el user id — con la
      comparación vieja, ese caso devolvía los leads de otro (o ninguno).
    · §5 el lead creado a las 15:00 del propio día `hasta` DEBE aparecer (con el
      comparador viejo quedaba fuera el día entero).

Sin red. Datos con MARK propio y limpieza verificada al final.

Corre con:  ./venv/bin/python -m pytest monza_tests/test_leads_paginacion_busqueda.py -q
"""
import os
import sys
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import SessionLocal  # noqa: E402
from auth import get_current_user  # noqa: E402
from monza_fechas import _dia_chile_a_utc_naive  # noqa: E402
from monza_models import (  # noqa: E402
    MonzaAsesor, MonzaCliente, MonzaCotizacion, MonzaCotizacionItem,
    MonzaLead, MonzaLeadActividad, MonzaLeadItem, MonzaLog,
)
from monza_router_leads import router as leads_router  # noqa: E402
from models.models import User  # noqa: E402

MARK = "test-mzleadpag"
LEAD_MARK = "L-MZLP"
COT_MARK = "CMZLP"
EMAIL = f"{MARK}@test.invalid"

# El usuario del test se crea DE VERDAD en `users` (monza_asesores.user_id tiene FK):
# así el id del ASESOR sale del autoincrement y queda distinto del id del USUARIO —
# que es exactamente el caso que delata la comparación vieja de «Míos».
_CTX = {"user_id": 0}

app = FastAPI()
app.include_router(leads_router)
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=_CTX["user_id"], email=EMAIL, empresa="automotriz", rol="admin")
client = TestClient(app)

_fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


# Tokens ÚNICOS por vía de búsqueda: si el mismo texto apareciera en dos campos, el
# check no probaría la rama que dice probar (sonda que no prueba nada).
TOK_VIN = "VINUNICO9Z9"
TOK_PARTE = "PARTEUNICA7X7"
TOK_COT_SUF = "K7K7"
RUT_FORMATEADO = "76.543.210-8"


def _seed():
    """12 leads con la MISMA fecha (empate deliberado) + los casos de búsqueda."""
    db = SessionLocal()
    try:
        usuario = User(email=EMAIL, nombre=f"{MARK} USUARIO",
                       hashed_password="x", is_active=1, empresa="automotriz")
        db.add(usuario)
        db.flush()
        _CTX["user_id"] = usuario.id
        otro_usuario = User(email=f"otro-{MARK}@test.invalid", nombre=f"{MARK} OTRO USUARIO",
                            hashed_password="x", is_active=1, empresa="automotriz")
        db.add(otro_usuario)
        db.flush()
        asesor = MonzaAsesor(nombre=f"{MARK} ASESOR", email=f"asesor-{MARK}@test.invalid",
                             slug=f"{MARK}-a", activo=True, user_id=usuario.id)
        db.add(asesor)
        db.flush()
        # SONDA: el asesor del usuario tiene un id DISTINTO al del usuario (los
        # autoincrement de las dos tablas no se alinean) — con la comparación vieja
        # («asesor_id == current_user.id») este caso devolvía los leads de otro.
        assert asesor.id != usuario.id, (
            "el seed necesita ids distintos para que la sonda de «Míos» discrimine")
        otro = MonzaAsesor(nombre=f"{MARK} OTRO", email=f"otro2-{MARK}@test.invalid",
                           slug=f"{MARK}-b", activo=True, user_id=otro_usuario.id)
        db.add(otro)
        db.flush()

        cli = MonzaCliente(nombre=f"{MARK} SpA", rut=RUT_FORMATEADO, telefono="+56900000000")
        db.add(cli)
        db.flush()

        # Fecha fija (NO utcnow): 12:00 de Chile de un día conocido. Todos los leads
        # comparten el segundo exacto — el empate que la paginación debe resistir.
        dia = "2026-05-20"
        fecha = _dia_chile_a_utc_naive(datetime.fromisoformat(dia).date()) + timedelta(hours=12)
        ids = []
        for i in range(12):
            lead = MonzaLead(
                numero=f"{LEAD_MARK}-{i:02d}-{uuid.uuid4().hex[:4].upper()}",
                cliente_id=cli.id, estado="pendiente",
                asesor_id=asesor.id if i < 8 else otro.id,   # 8 míos / 4 del otro
                vehiculo="TOYOTA HILUX", fecha_creacion=fecha, fecha_actualizacion=fecha,
            )
            db.add(lead)
            db.flush()
            ids.append(lead.id)

        # Casos de búsqueda, cada uno en SU lead (tokens únicos por vía).
        db.query(MonzaLead).filter(MonzaLead.id == ids[0]).update({"vin": TOK_VIN})
        db.add(MonzaLeadItem(lead_id=ids[1], descripcion="Filtro", numero_parte=TOK_PARTE, cantidad=1))
        # Un SEGUNDO ítem con el mismo N° de parte: el EXISTS no debe duplicar la fila.
        db.add(MonzaLeadItem(lead_id=ids[1], descripcion="Filtro gemelo", numero_parte=TOK_PARTE, cantidad=1))
        cot = MonzaCotizacion(numero=f"{COT_MARK}-{TOK_COT_SUF}", cliente_id=cli.id,
                              lead_id=ids[2], estado="propuesta", total_bruto=1000)
        db.add(cot)
        # Estados: uno 'vendido' (marcado a mano) y uno 'cerrado' (lo escribe el despacho).
        db.query(MonzaLead).filter(MonzaLead.id == ids[3]).update({"estado": "vendido"})
        db.query(MonzaLead).filter(MonzaLead.id == ids[4]).update({"estado": "cerrado"})
        db.commit()
        return ids
    finally:
        db.close()


def _listar(**params):
    params.setdefault("q", LEAD_MARK)      # aísla de los datos reales de la base
    r = client.get("/api/monza/leads", params=params)
    assert r.status_code == 200, r.text[:300]
    return r.json()


def _limpiar():
    db = SessionLocal()
    try:
        S = "fetch"
        lead_ids = [r[0] for r in db.query(MonzaLead.id)
                    .filter(MonzaLead.numero.like(f"{LEAD_MARK}%")).all()]
        cot_ids = [r[0] for r in db.query(MonzaCotizacion.id)
                   .filter(MonzaCotizacion.numero.like(f"{COT_MARK}%")).all()]
        db.query(MonzaCotizacionItem).filter(
            MonzaCotizacionItem.cotizacion_id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaCotizacion).filter(
            MonzaCotizacion.id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaLeadActividad).filter(
            MonzaLeadActividad.lead_id.in_(lead_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaLeadItem).filter(
            MonzaLeadItem.lead_id.in_(lead_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaLead).filter(MonzaLead.id.in_(lead_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaLog).filter(MonzaLog.user_email == EMAIL).delete(synchronize_session=S)
        db.query(MonzaCliente).filter(MonzaCliente.nombre.like(f"{MARK}%")).delete(synchronize_session=S)
        db.query(MonzaAsesor).filter(MonzaAsesor.nombre.like(f"{MARK}%")).delete(synchronize_session=S)
        db.query(User).filter(User.email.like(f"%{MARK}%")).delete(synchronize_session=S)
        db.commit()
    finally:
        db.close()


def _verificar_limpieza():
    db = SessionLocal()
    try:
        restos = (db.query(MonzaLead).filter(MonzaLead.numero.like(f"{LEAD_MARK}%")).count()
                  + db.query(MonzaCliente).filter(MonzaCliente.nombre.like(f"{MARK}%")).count()
                  + db.query(MonzaAsesor).filter(MonzaAsesor.nombre.like(f"{MARK}%")).count()
                  + db.query(User).filter(User.email.like(f"%{MARK}%")).count())
        assert restos == 0, f"quedaron {restos} filas {MARK} tras la limpieza"
    finally:
        db.close()
    print("Cleanup OK (verificado con sesión nueva)")


def run():
    _limpiar()
    try:
        ids = _seed()

        # ── 1) Paginación: el contrato completo ──────────────────────────────────
        p1 = _listar(page=1, page_size=5)
        check("1a la página 1 trae 5 y el total son los 12",
              len(p1["items"]) == 5 and p1["total"] == 12, (len(p1["items"]), p1["total"]))
        p3 = _listar(page=3, page_size=5)
        check("1b la última página trae el resto (2)", len(p3["items"]) == 2, len(p3["items"]))
        vacia = _listar(page=99, page_size=5)
        check("1c una página fuera de rango es vacía, con el total intacto",
              vacia["items"] == [] and vacia["total"] == 12, (vacia["items"], vacia["total"]))
        r = client.get("/api/monza/leads", params={"q": LEAD_MARK, "page_size": 500})
        check("1d page_size fuera del tope → 422 (no una consulta gigante)",
              r.status_code == 422, r.status_code)

        # ── 2) SONDA: empates de fecha no rompen la paginación ───────────────────
        p2 = _listar(page=2, page_size=5)
        vistos = [x["id"] for x in p1["items"] + p2["items"] + p3["items"]]
        check("2a SONDA: las 3 páginas cubren los 12 leads SIN repetir "
              "(sin el desempate por id, MySQL puede repetir o saltarse filas)",
              sorted(vistos) == sorted(ids), (len(vistos), len(set(vistos))))
        check("2b y dentro de la página el orden es descendente por id (empate resuelto)",
              [x["id"] for x in p1["items"]] == sorted([x["id"] for x in p1["items"]], reverse=True),
              [x["id"] for x in p1["items"]])

        # ── 3) Búsqueda honesta: cada vía con su token único ─────────────────────
        por_vin = _listar(q=TOK_VIN)
        check("3a SONDA (RED antes): busca por VIN",
              [x["id"] for x in por_vin["items"]] == [ids[0]], por_vin["items"])
        por_parte = _listar(q=TOK_PARTE)
        check("3b SONDA (RED antes): busca por N° de parte del ítem",
              [x["id"] for x in por_parte["items"]] == [ids[1]], por_parte["items"])
        check("3c y el lead con DOS ítems que calzan aparece UNA sola vez "
              "(EXISTS, no JOIN: un join duplicaría la fila y el total)",
              por_parte["total"] == 1, por_parte["total"])
        por_cot = _listar(q=TOK_COT_SUF)
        check("3d SONDA (RED antes): busca por N° de cotización",
              [x["id"] for x in por_cot["items"]] == [ids[2]], por_cot["items"])
        # RUT en OTRO formato que el guardado: el corazón del arreglo bilateral.
        por_rut = _listar(q="76543210-8")
        check("3e SONDA (RED antes): el RUT tecleado SIN puntos encuentra al cliente "
              "guardado CON puntos", por_rut["total"] == 12, por_rut["total"])
        por_rut2 = _listar(q=RUT_FORMATEADO)
        check("3f y con el formato exacto también", por_rut2["total"] == 12, por_rut2["total"])
        # Control negativo: un texto que no es de ninguna vía no trae nada.
        nada = _listar(q="ZZZ-NO-EXISTE-ZZZ")
        check("3g control negativo: un término inexistente no trae leads",
              nada["total"] == 0, nada["total"])

        # ── 4) Estado y «Míos» ───────────────────────────────────────────────────
        vendidos = _listar(estado="vendido")
        check("4a SONDA (RED antes): 'vendido' incluye también los 'cerrado' "
              "(el mismo estado de negocio con dos nombres)",
              sorted(x["id"] for x in vendidos["items"]) == sorted([ids[3], ids[4]]),
              vendidos["items"])
        pendientes = _listar(estado="pendiente")
        check("4b y 'pendiente' sigue siendo exacto (no se contamina)",
              all(x["estado"] == "pendiente" for x in pendientes["items"])
              and pendientes["total"] == 10, pendientes["total"])
        mios = _listar(solo_mios=True, page_size=50)
        check("4c SONDA (RED antes): «Míos» resuelve el ASESOR del usuario "
              "(id distinto del user id) y trae sus 8 leads",
              mios["total"] == 8, mios["total"])
        check("4d y ninguno es del otro asesor",
              all(x["id"] in ids[:8] for x in mios["items"]), [x["id"] for x in mios["items"]])

        # ── 5) Fechas: el día `hasta` entra COMPLETO ─────────────────────────────
        mismo_dia = _listar(desde="2026-05-20", hasta="2026-05-20", page_size=50)
        check("5a SONDA (RED antes): los leads de las 12:00 del propio día 'hasta' "
              "APARECEN (antes el `<=` contra la medianoche los dejaba fuera)",
              mismo_dia["total"] == 12, mismo_dia["total"])
        antes = _listar(hasta="2026-05-19", page_size=50)
        check("5b y el día anterior no los trae", antes["total"] == 0, antes["total"])
        desde_despues = _listar(desde="2026-05-21", page_size=50)
        check("5c ni un 'desde' posterior", desde_despues["total"] == 0, desde_despues["total"])
        r = client.get("/api/monza/leads", params={"q": LEAD_MARK, "hasta": "20-05-2026"})
        check("5d fecha inválida → 422 (antes: 500 con traceback)",
              r.status_code == 422, (r.status_code, r.text[:120]))

        # ── 6) El candado de empresa (DEC-1) ─────────────────────────────────────
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            id=_CTX["user_id"], email=EMAIL, empresa="mineria", rol="admin")
        try:
            r = client.get("/api/monza/leads", params={"q": LEAD_MARK})
            check("6a SONDA: un usuario de MINERÍA no entra al CRM de Monza (403)",
                  r.status_code == 403, (r.status_code, r.text[:120]))
            r = client.post("/api/monza/leads", json={"canal_origen": "WhatsApp",
                                                      "cliente": {"nombre": f"{MARK} INTRUSO"}})
            check("6b y tampoco puede crear (la ruta NO se recorre: 403 del router)",
                  r.status_code == 403, (r.status_code, r.text[:120]))
        finally:
            app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
                id=_CTX["user_id"], email=EMAIL, empresa="automotriz", rol="admin")
        db = SessionLocal()
        try:
            check("6c el intento de minería no dejó ninguna fila",
                  db.query(MonzaCliente).filter(MonzaCliente.nombre.like(f"{MARK} INTRUSO%")).count() == 0, "")
        finally:
            db.close()

    finally:
        _limpiar()
    _verificar_limpieza()
    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_leads_paginacion_busqueda():
    run()


if __name__ == "__main__":
    run()
