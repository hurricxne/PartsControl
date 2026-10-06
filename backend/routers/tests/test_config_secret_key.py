"""La app no arranca con una SECRET_KEY que no es una clave.

QUÉ PROTEGE
    `config.py` hace fail-fast al importarse: con la clave vacía, con el default de
    `Settings` o con el placeholder de `backend/.env.example`, `import config` lanza. Con
    cualquiera de esos valores, quien tenga el repo firma un JWT con el `sub` de cualquier
    usuario y `auth.get_usuario_autenticado` lo acepta. El placeholder se LEE de la
    plantilla, no se copia acá: si alguien lo cambia en `.env.example` sin tocar
    `config.py`, esta suite se pone roja.

    Una clave corta solo produce un AVISO en el log (no corta el arranque): el largo de
    las claves ya desplegadas no se conoce desde el repo, y un `raise` dejaría el backend
    entero caído en el siguiente restart. La suite protege también esa decisión.

CÓMO
    Arranque real en un subproceso (`import config` desde backend/) con la variable de
    entorno SECRET_KEY puesta: manda sobre el `.env`. No abre conexión a la base.

SONDAS DE PODER DISCRIMINANTE — qué línea hay que romper para que la suite falle
    · quitar el placeholder de `_SECRET_KEY_SIN_CONFIGURAR` → el placeholder arranca.
    · quitar '' del conjunto → la clave vacía arranca.
    · quitar el `.strip()` de la comparación → la variante con espacios arranca.
    · cambiar el `raise RuntimeError` por un `print` → las cuatro arrancan.
    · quitar el `print` del aviso → la corta arranca sin aviso.
    · convertir el aviso en `raise` → la corta deja de arrancar.
  Se repiten con `python -m portal.tests.mutaciones secret_key` (portal/tests/mutaciones/login.py).

Corre con:  ./venv/bin/python -m pytest routers/tests/test_config_secret_key.py -q
"""
import os
import re
import secrets
import subprocess
import sys

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

# Las etapas se llaman `_paso_*` y NO `test_*` a propósito: `check()` acumula en
# `_fails` en vez de lanzar, así que si pytest las recolectara por separado cada una
# sería un verde incondicional. La única función recolectable es la de más abajo.
_fails: list = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _arrancar(secret_key: str) -> subprocess.CompletedProcess:
    """`import config` en un proceso aparte, con esa SECRET_KEY en el entorno (manda
    sobre el `.env` de backend/)."""
    entorno = {**os.environ, "SECRET_KEY": secret_key}
    return subprocess.run([sys.executable, "-c", "import config"], cwd=BACKEND, env=entorno,
                          capture_output=True, text=True, timeout=60)


def _placeholder_de_la_plantilla() -> str:
    """El valor de la línea `SECRET_KEY=` de backend/.env.example, leído de la plantilla."""
    with open(os.path.join(BACKEND, ".env.example"), encoding="utf-8") as f:
        for linea in f:
            m = re.match(r"^SECRET_KEY=(.*)$", linea.rstrip("\n"))
            if m:
                return m.group(1).strip().strip('"').strip("'")
    raise AssertionError("backend/.env.example no trae la línea SECRET_KEY=")


def _paso_1_los_valores_que_no_son_una_clave_no_arrancan():
    plantilla = _placeholder_de_la_plantilla()
    check("la plantilla trae un placeholder", bool(plantilla), plantilla)
    for nombre, valor in (
        ("el placeholder de .env.example", plantilla),
        ("la clave vacía", ""),
        ("solo espacios", "   "),
        ("el placeholder rodeado de espacios", f"  {plantilla}  "),
        ("el default del código", "changeme-in-production"),
    ):
        r = _arrancar(valor)
        check(f"con {nombre} NO arranca, y el error nombra SECRET_KEY",
              r.returncode != 0 and "SECRET_KEY" in r.stderr, (r.returncode, r.stderr[-200:]))


def _paso_2_una_clave_propia_arranca_y_la_corta_avisa():
    r = _arrancar(secrets.token_urlsafe(48))
    check("con una clave propia y larga arranca, sin aviso",
          r.returncode == 0 and "AVISO SECRET_KEY" not in r.stdout,
          (r.returncode, r.stdout[-200:], r.stderr[-200:]))
    r = _arrancar("corta-pero-propia")
    check("con una clave propia pero corta arranca Y avisa en el log",
          r.returncode == 0 and "AVISO SECRET_KEY" in r.stdout,
          (r.returncode, r.stdout[-200:], r.stderr[-200:]))


def run():
    _paso_1_los_valores_que_no_son_una_clave_no_arrancan()
    _paso_2_una_clave_propia_arranca_y_la_corta_avisa()
    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_config_secret_key():
    run()


if __name__ == "__main__":
    run()
