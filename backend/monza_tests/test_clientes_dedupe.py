"""La ficha del cliente NO se renombra al dedupear, y el RUT se compara canónico.

DOS BUGS QUE CIERRA (2026-08-22), ambos en el mismo POST /api/monza/clientes:

  1. RENOMBRE SILENCIOSO. Cuando el POST encontraba una ficha existente por RUT o
     teléfono, «actualizaba datos» pisando el NOMBRE con lo que el operador acababa de
     tipear. Como el nombre es obligatorio, eso pasaba SIEMPRE que había dedupe: crear
     «Juan Pérez» sobre el RUT de «Comercial JP SpA» renombraba la empresa entera — una
     ficha COMPARTIDA por todos sus leads, ventas y facturas, y receptora del DTE 33.

  2. DEDUPE POR RUT CRUDO. La comparación era literal, así que «76.000.000-0» y
     «76000000-0» creaban DOS fichas del mismo cliente. Arreglar solo el buscador habría
     dejado la costura abierta: se encuentra la ficha, pero el POST sigue duplicando.

SONDAS DE PODER DISCRIMINANTE
    · §1 el nombre de la ficha existente sobrevive al POST (contra el código anterior,
      la BD quedaba con el nombre tipeado).
    · §2 el POST con el RUT en OTRO formato NO crea una segunda ficha (antes sí).
    · §3 fill-if-empty: lo vacío se completa, lo que ya tiene dato se respeta.
    · §4 el buscador encuentra por RUT en cualquiera de los dos formatos.

Sin red. Datos con MARK propio y limpieza verificada al final.

Corre con:  ./venv/bin/python -m pytest monza_tests/test_clientes_dedupe.py -q
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import SessionLocal  # noqa: E402
from auth import get_current_user  # noqa: E402
from monza_models import (  # noqa: E402
    MonzaCliente, MonzaLead, MonzaLeadActividad, MonzaLeadItem, MonzaLog,
)
from monza_router_clientes import router as clientes_router  # noqa: E402
from monza_router_leads import router as leads_router  # noqa: E402

MARK = "test-mzdedupe"
EMAIL = f"{MARK}@test.invalid"
RUT_CON_PUNTOS = "76.111.222-3"
RUT_SIN_PUNTOS = "76111222-3"
NOMBRE_FICHA = f"{MARK} COMERCIAL JP SpA"

app = FastAPI()
app.include_router(clientes_router)
app.include_router(leads_router)
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=1, email=EMAIL, empresa="automotriz", rol="admin")
client = TestClient(app)

_fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _ficha(cid):
    db = SessionLocal()
    try:
        c = db.query(MonzaCliente).filter(MonzaCliente.id == cid).first()
        return {"nombre": c.nombre, "rut": c.rut, "telefono": c.telefono,
                "email": c.email} if c else None
    finally:
        db.close()


def _cuantas():
    db = SessionLocal()
    try:
        return db.query(MonzaCliente).filter(MonzaCliente.nombre.like(f"{MARK}%")).count()
    finally:
        db.close()


def _limpiar():
    db = SessionLocal()
    try:
        S = "fetch"
        cli_ids = [r[0] for r in db.query(MonzaCliente.id)
                   .filter(MonzaCliente.nombre.like(f"{MARK}%")).all()]
        lead_ids = [r[0] for r in db.query(MonzaLead.id)
                    .filter(MonzaLead.cliente_id.in_(cli_ids or [0])).all()]
        db.query(MonzaLeadActividad).filter(
            MonzaLeadActividad.lead_id.in_(lead_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaLeadItem).filter(
            MonzaLeadItem.lead_id.in_(lead_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaLead).filter(MonzaLead.id.in_(lead_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaLog).filter(MonzaLog.user_email == EMAIL).delete(synchronize_session=S)
        db.query(MonzaCliente).filter(MonzaCliente.id.in_(cli_ids or [0])).delete(
            synchronize_session=S)
        db.commit()
    finally:
        db.close()


def _verificar_limpieza():
    db = SessionLocal()
    try:
        assert db.query(MonzaCliente).filter(
            MonzaCliente.nombre.like(f"{MARK}%")).count() == 0, "quedaron fichas del test"
    finally:
        db.close()
    print("Cleanup OK (verificado con sesión nueva)")


def run():
    _limpiar()
    try:
        # Ficha original, creada con RUT CON puntos y sin teléfono ni email.
        r = client.post("/api/monza/clientes", json={
            "nombre": NOMBRE_FICHA, "rut": RUT_CON_PUNTOS})
        check("0a la ficha original se crea", r.status_code == 201, r.text[:200])
        original = r.json()["id"]
        check("0b y se declara como NUEVA (no reutilizada)",
              r.json().get("reutilizado") is False, r.json().get("reutilizado"))

        # ── 1) SONDA: el dedupe NO renombra ──────────────────────────────────────
        r = client.post("/api/monza/clientes", json={
            "nombre": f"{MARK} Juan Pérez", "rut": RUT_CON_PUNTOS})
        check("1a el POST con el mismo RUT devuelve la ficha existente",
              r.status_code in (200, 201) and r.json()["id"] == original,
              (r.status_code, r.json().get("id"), original))
        check("1b y lo declara con `reutilizado`",
              r.json().get("reutilizado") is True, r.json().get("reutilizado"))
        check("1c SONDA (antes la BD quedaba con «Juan Pérez»): el nombre de la ficha "
              "COMPARTIDA sobrevive", _ficha(original)["nombre"] == NOMBRE_FICHA,
              _ficha(original)["nombre"])
        check("1d y la respuesta devuelve el nombre REAL, no el tipeado",
              r.json()["nombre"] == NOMBRE_FICHA, r.json()["nombre"])

        # ── 2) SONDA: dedupe por RUT canónico (otro formato) ─────────────────────
        antes = _cuantas()
        r = client.post("/api/monza/clientes", json={
            "nombre": f"{MARK} OTRO NOMBRE", "rut": RUT_SIN_PUNTOS})
        check("2a el POST con el RUT SIN puntos encuentra la ficha guardada CON puntos",
              r.json().get("id") == original and r.json().get("reutilizado") is True,
              (r.json().get("id"), original, r.json().get("reutilizado")))
        check("2b SONDA (antes creaba una SEGUNDA ficha del mismo cliente): el total "
              "no cambió", _cuantas() == antes, (antes, _cuantas()))

        # ── 3) fill-if-empty: completa lo vacío, respeta lo que ya está ──────────
        r = client.post("/api/monza/clientes", json={
            "nombre": f"{MARK} IGNORADO", "rut": RUT_CON_PUNTOS,
            "telefono": "+56911112222", "email": "nuevo@test.invalid"})
        f = _ficha(original)
        check("3a el teléfono vacío se COMPLETA", f["telefono"] == "+56911112222", f)
        check("3b el email vacío también", f["email"] == "nuevo@test.invalid", f)
        r = client.post("/api/monza/clientes", json={
            "nombre": f"{MARK} IGNORADO 2", "rut": RUT_CON_PUNTOS,
            "telefono": "+56999999999", "email": "pisado@test.invalid"})
        f = _ficha(original)
        check("3c SONDA: lo que YA tenía dato NO se pisa (ni teléfono ni email)",
              f["telefono"] == "+56911112222" and f["email"] == "nuevo@test.invalid", f)
        check("3d y el nombre sigue intacto tras 4 POSTs", f["nombre"] == NOMBRE_FICHA, f)

        # ── 4) Los buscadores encuentran el RUT en cualquier formato ─────────────
        r = client.get("/api/monza/leads/clientes/search", params={"q": RUT_SIN_PUNTOS})
        check("4a search_clientes: el RUT sin puntos encuentra la ficha con puntos",
              any(c["id"] == original for c in r.json()), r.json())
        r = client.get("/api/monza/clientes", params={"q": RUT_SIN_PUNTOS})
        check("4b la lista de Clientes también",
              any(c["id"] == original for c in r.json()["items"]), r.json()["items"][:2])
        r = client.get("/api/monza/leads/clientes/search", params={"q": RUT_CON_PUNTOS})
        check("4c y con el formato exacto sigue funcionando",
              any(c["id"] == original for c in r.json()), r.json())
        # Control negativo: un texto que no es RUT no debe entrar por esa rama.
        r = client.get("/api/monza/leads/clientes/search", params={"q": "76111"})
        check("4d control: un número corto NO activa la rama de RUT (evita ruido)",
              not any(c["id"] == original for c in r.json()), r.json())

        # ── 5) SONDA: un RUT que NO identifica jamás dedupea ─────────────────────
        # Con el normalizador de BUSCAR, '-' y '.' colapsaban a "" y enganchaban con
        # toda ficha de RUT vacío: el POST devolvía la ficha de un TERCERO y el
        # vendedor seguía trabajando sobre ella (regresión detectada por el equipo de
        # testing). Ahora la identidad la decide `rut_identidad`.
        r1 = client.post("/api/monza/clientes", json={"nombre": f"{MARK} BASURA UNO", "rut": "-"})
        r2 = client.post("/api/monza/clientes", json={"nombre": f"{MARK} BASURA DOS", "rut": "."})
        check("5a un RUT de pura puntuación crea ficha NUEVA (no reutiliza)",
              r1.json().get("reutilizado") is False and r2.json().get("reutilizado") is False,
              (r1.json().get("reutilizado"), r2.json().get("reutilizado")))
        check("5b SONDA: y las dos son fichas DISTINTAS entre sí",
              r1.json()["id"] != r2.json()["id"], (r1.json()["id"], r2.json()["id"]))
        check("5c cada una conserva SU nombre (no el de un tercero)",
              r1.json()["nombre"].endswith("BASURA UNO")
              and r2.json()["nombre"].endswith("BASURA DOS"),
              (r1.json()["nombre"], r2.json()["nombre"]))
        check("5d y la ficha ancla no fue tocada por ninguna de las dos",
              _ficha(original)["nombre"] == NOMBRE_FICHA
              and _ficha(original)["rut"] == RUT_CON_PUNTOS, _ficha(original))

        # ── 6) La OTRA puerta: «cliente al vuelo» del lead nuevo ────────────────
        # POST /leads creaba la ficha comparando el RUT LITERAL, así que el mismo
        # cliente terminaba con dos fichas según por dónde entrara.
        antes6 = _cuantas()
        r = client.post("/api/monza/leads", json={
            "canal_origen": "WhatsApp", "marca": "TOYOTA", "modelo": "HILUX",
            "anio": "2020", "vin": f"VIN{MARK[-6:]}",
            "cliente": {"nombre": f"{MARK} DESDE LEAD", "rut": RUT_SIN_PUNTOS}})
        check("6a el lead se crea", r.status_code == 201, r.text[:200])
        check("6b SONDA: reusó la ficha existente (mismo RUT en otro formato) en vez "
              "de duplicarla", _cuantas() == antes6, (antes6, _cuantas()))
        check("6c y el lead quedó colgado de la ficha original",
              (r.json().get("cliente") or {}).get("id") == original,
              (r.json().get("cliente") or {}).get("id"))

    finally:
        _limpiar()
    _verificar_limpieza()
    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_clientes_dedupe():
    run()


if __name__ == "__main__":
    run()
