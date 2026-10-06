"""Los cerrojos que solo existen en `main.py`, probados contra la app REAL.
Fase 0 del Portal de Clientes (2026-08-20).

POR QUÉ ESTE ARCHIVO EXISTE
    Los otros tests de la Fase 0 montan un `FastAPI()` de juguete con un solo router.
    Eso es rápido y aislado, pero deja DOS cerrojos sin cobertura, porque no viven en
    ningún router sino en `main.py`:

      · el manejador que traduce a 401 el 403 de "falta el token" (cerrojo C4);
      · el retiro del `app.mount("/uploads/bodega", ...)` público (cerrojo C1).

    El comité lo señaló: se podían revertir los dos y la suite seguía verde. Así que
    acá se importa la aplicación de verdad, con sus 30 routers y su manejador.

QUÉ NO HACE
    No dispara el evento de arranque (no se usa `with TestClient(...)`), así que el
    planificador de tareas no se levanta. Solo se piden rutas.

SONDAS DE PODER DISCRIMINANTE
    · `_paso_1` ← borrar el manejador `_sesion_ausente_es_401` de `main.py`, o hacer que
                  `es_403_por_falta_de_sesion` devuelva siempre False: el "sin token"
                  vuelve a llegar como 403 y el frontend deja de mandar al login.
    · `_paso_2` ← que ese manejador reclasifique CUALQUIER 403: el guard de empresa
                  empezaría a llegar como 401 y desloguearía a gente con sesión válida.
                  Se prueba con una sesión REAL de la otra marca (un token basura no
                  sirve: nunca llega al guard).
    · `_paso_3` ← volver a montar `uploads/bodega` sin condición, o poner
                  `SERVIR_FOTOS_BODEGA_SIN_AUTH=true`.

Corre con:  ./venv/bin/python -m pytest routers/tests/test_main_cerrojos.py -q
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi.testclient import TestClient  # noqa: E402

from auth import get_password_hash  # noqa: E402
from config import settings  # noqa: E402
from database import SessionLocal  # noqa: E402
from main import app  # noqa: E402
from models.models import User  # noqa: E402
from routers.bodega import UPLOAD_DIR  # noqa: E402
from sqlalchemy import text  # noqa: E402

client = TestClient(app)

MARK = "ZZTESTMAINCERROJOSZZ"
EMAIL = f"{MARK}@test.invalid".lower()
CLAVE = "clave-de-prueba-larga-2026"
ARCHIVO_PUBLICO = f"{MARK}-publico.jpg"

_fails: list = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _paso_1_sin_token_es_401_no_403():
    """`auth.py` usa `HTTPBearer()`, que responde 403 "Not authenticated" cuando falta
    el header. El frontend necesita distinguir "no tengo sesión" (ir al login) de "mi
    sesión no alcanza" (avisar), así que `main.py` traduce el primero a 401."""
    for ruta in ("/api/auth/me", "/api/bodega/fotos/1", "/api/despachos/counts"):
        r = client.get(ruta)
        check(f"{ruta} sin token responde 401", r.status_code == 401,
              f"{r.status_code} {r.text[:80]}")
        check(f"{ruta} trae WWW-Authenticate",
              "www-authenticate" in {k.lower() for k in r.headers}, dict(r.headers))


def _paso_2_el_403_de_negocio_NO_se_traduce():
    """El manejador solo puede tocar los DOS detalles que emite la librería.

    Si reclasificara cualquier 403, el guard de empresa empezaría a llegar al frontend
    como 401 y desloguearía a gente con sesión válida — el daño exacto que el diseño
    dice evitar. Se prueba con una sesión REAL de la otra marca contra un endpoint
    candado a `mineria`: tiene que seguir siendo 403.

    (Un token basura no sirve para esto: `get_current_user` responde 401 por su cuenta
    y nunca llega al guard de empresa. La primera versión de esta etapa hacía eso y por
    lo tanto no discriminaba: verificado por mutación.)
    """
    # Token basura: 401 con su propio mensaje, sin pasar por el traductor.
    r = client.get("/api/auth/me", headers={"Authorization": "Bearer token-basura"})
    check("token inválido responde 401", r.status_code == 401, r.status_code)
    check("y conserva su propio mensaje, no el del traductor",
          (r.json() or {}).get("detail", "") != "Not authenticated", r.text[:100])

    # Sesión válida de la marca equivocada contra un endpoint de Grupo AM.
    r = client.post("/api/auth/login", json={"email": EMAIL, "password": CLAVE})
    check("la cuenta de prueba puede iniciar sesión", r.status_code == 200, r.text[:150])
    if r.status_code != 200:
        return
    token = r.json()["access_token"]
    cab = {"Authorization": f"Bearer {token}"}
    r = client.get("/api/bodega/fotos/1", headers=cab)
    check("una sesión de la otra marca recibe 403, NO 401", r.status_code == 403,
          f"{r.status_code} {r.text[:120]}")
    check("y el detalle es el del guard de empresa",
          "empresa" in (r.json() or {}).get("detail", "").lower(), r.text[:120])


def _paso_3_uploads_bodega_ya_no_es_publico():
    """El mount público se retiró: las fotos van por GET /api/bodega/fotos/{id}.

    OJO CON EL ARCHIVO — la primera versión de esta etapa pedía un nombre inventado, y
    entonces `StaticFiles` respondía 404 igual **con el mount puesto**: la sonda pasaba
    con y sin el defecto. Verificado por mutación. Hay que pedir un archivo que EXISTA
    de verdad en la carpeta: con el mount da 200, sin el mount da 404.
    """
    check("el flag de rollback está apagado",
          settings.SERVIR_FOTOS_BODEGA_SIN_AUTH is False,
          settings.SERVIR_FOTOS_BODEGA_SIN_AUTH)

    ruta = os.path.join(UPLOAD_DIR, ARCHIVO_PUBLICO)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    with open(ruta, "wb") as f:
        f.write(b"\xff\xd8\xff\xdb" + b"\x00" * 32)
    try:
        r = client.get(f"/uploads/bodega/{ARCHIVO_PUBLICO}")
        check("un archivo que SÍ existe tampoco se sirve sin sesión",
              r.status_code == 404, f"{r.status_code} ct={r.headers.get('content-type')}")
        check("y desde luego no devuelve la imagen",
              not r.headers.get("content-type", "").startswith("image/"),
              r.headers.get("content-type"))
    finally:
        if os.path.exists(ruta):
            os.remove(ruta)


def _paso_4_el_404_normal_sigue_intacto():
    """El manejador reemplaza el de FastAPI para TODAS las HTTPException, así que hay
    que comprobar que no rompió el camino normal: un 404 tiene que seguir siendo un 404
    con su cuerpo JSON."""
    r = client.get("/api/ruta-que-no-existe-jamas")
    check("una ruta inexistente sigue dando 404", r.status_code == 404, r.status_code)
    check("y responde JSON con detail", "detail" in (r.json() or {}), r.text[:120])


def _crear_cuenta(db):
    _borrar_cuenta(db)
    db.add(User(email=EMAIL, nombre=MARK, hashed_password=get_password_hash(CLAVE),
                is_active=1, empresa="automotriz"))
    db.commit()


def _borrar_cuenta(db):
    db.execute(text("DELETE FROM users WHERE email = :e"), {"e": EMAIL})
    db.commit()


def run():
    db = SessionLocal()
    try:
        _crear_cuenta(db)
        _paso_1_sin_token_es_401_no_403()
        _paso_2_el_403_de_negocio_NO_se_traduce()
        _paso_3_uploads_bodega_ya_no_es_publico()
        _paso_4_el_404_normal_sigue_intacto()
    finally:
        _borrar_cuenta(db)
        db.close()
        print("Cleanup OK")
    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_main_cerrojos():
    run()


if __name__ == "__main__":
    run()
