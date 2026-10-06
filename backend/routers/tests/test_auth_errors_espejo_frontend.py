"""ESPEJO del frontend: la política de sesión y permiso de `services/authErrors.ts`.

POR QUÉ UN ESPEJO
    La mitad que decide qué hacer con un 401 o un 403 es TypeScript y el frontend no
    tiene pruebas propias. Tres cosas pueden romperse sin que nada falle a la vista:

    · LOS TEXTOS GEMELOS. `DETALLES_AMBITO` y `DETALLES_SESION` repiten textos que emite
      el backend (`empresa_guard`, `role_guard`, `auth.DETALLE_CUENTA_PORTAL`,
      `auth_errors.DETALLES_SIN_SESION`). Si un lado cambia y el otro no, el 403 cae en
      `negocio`: el aviso deja de salir y el usuario mira tablas vacías.
    · EL LOGIN. Un 401 de `POST /auth/login` significa «credenciales incorrectas», no
      «sesión inválida»: no puede cerrar la sesión que ya hubiera en el navegador (y en
      las otras pestañas, que adoptan lo que `logout()` escribe en localStorage).
    · LOS DOS CLIENTES. La política sirve solo si `services/api.ts` (MachParts) y
      `services/monzaApi.ts` (MonzaParts) la llaman desde su interceptor.

CÓMO
    El código del frontend se COMPILA (el esbuild que ya vive en frontend-src/node_modules)
    y se ejecuta tal cual está en el repo: los dos clientes axios reales, con sus
    interceptores, el store real y `authErrors.ts`. Lo único que se reemplaza es la red
    (un adaptador de axios que responde lo que cada paso programa), el navegador
    (`window`, `localStorage`) y el toast. Los textos del backend no se copian acá: se le
    piden al backend (las constantes se importan y los guards se ejecutan para que digan
    su `detail`). Importar `auth` crea el engine sin conectarse: no usa la base.

CINTURÓN ESTÁTICO
    Aunque falte node, `test_cinturon_estatico` lee el .ts y exige los mismos textos y la
    misma ruta de login que `authAPI.login`. El espejo compilado queda en skip CON
    MOTIVO, nunca en verde vacío.

DÓNDE BUSCA EL FRONTEND
    En frontend-src, junto a backend/. La variable de entorno PARTSCONTROL_FRONTEND_SRC
    apunta a otra carpeta con la misma forma (src/… y node_modules): es como el arnés de
    mutaciones (`portal/tests/mutaciones/`, lista `frontend.py`) prueba una COPIA mutada
    del frontend sin tocar el árbol de trabajo.

SONDAS DE PODER DISCRIMINANTE (cada mutación pone rojo el paso que se indica)
    · quitar de `manejarErrorAuth` la línea que excluye el login           -> paso 2
    · ampliar la exclusión a todo `/auth/` (p. ej. `includes('/auth/')`)    -> paso 4
    · quitar `manejarErrorAuth(err)` del interceptor de `monzaApi.ts`       -> pasos 3 y 4
    · cambiar el `detail` de `empresa_guard` sin tocar `DETALLES_AMBITO`    -> paso 1 y cinturón
    · cambiar `RUTA_LOGIN` sin cambiar `authAPI.login` (o al revés)         -> paso 2 y cinturón
    · quitar `yaRedirigiendo = true`                                        -> paso 4
    · quitar la exclusión del token distinto (`autorizacion !== …`)         -> paso 5
    · quitar `{ id: 'error-403-ambito' }` del `toast.error`                 -> paso 3

Corre con:  ./venv/bin/python -m pytest routers/tests/test_auth_errors_espejo_frontend.py -q
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest

_BACK = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, _BACK)

from fastapi import HTTPException  # noqa: E402

from auth import DETALLE_CUENTA_PORTAL  # noqa: E402
from auth_errors import DETALLES_SIN_SESION  # noqa: E402
from empresa_guard import require_empresa  # noqa: E402
from role_guard import require_rol  # noqa: E402

_FRONT = (os.environ.get("PARTSCONTROL_FRONTEND_SRC")
          or os.path.join(os.path.dirname(_BACK), "frontend-src"))
_AUTH_ERRORS = os.path.join(_FRONT, "src", "services", "authErrors.ts")
_STORE = os.path.join(_FRONT, "src", "stores", "authStore.ts")
_API = os.path.join(_FRONT, "src", "services", "api.ts")
_MONZA_API = os.path.join(_FRONT, "src", "services", "monzaApi.ts")
_ESBUILD = os.path.join(_FRONT, "node_modules", ".bin", "esbuild")


def _leer(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _detail_de(guard, usuario) -> str:
    """El `detail` que el guard emite DE VERDAD al negar a `usuario`."""
    try:
        guard(current_user=usuario)
    except HTTPException as e:
        assert e.status_code == 403, e.status_code
        return e.detail
    raise AssertionError("el guard dejó pasar a un usuario que debía negar")


def _textos_de_ambito() -> dict:
    return {
        "empresa_guard": _detail_de(require_empresa("mineria"), SimpleNamespace(empresa="automotriz")),
        "role_guard": _detail_de(require_rol("admin"), SimpleNamespace(rol="bodega")),
        "auth.DETALLE_CUENTA_PORTAL": DETALLE_CUENTA_PORTAL,
    }


def _ruta_de_login_en_api_ts() -> str:
    m = re.search(r"login:\s*\([^)]*\)\s*=>\s*api\.post\(\s*['\"]([^'\"]+)['\"]", _leer(_API))
    assert m, "no se encontró authAPI.login en services/api.ts: ¿cambió de forma? Este espejo quedaría ciego."
    return m.group(1)


def _literales(src: str, constante: str) -> list:
    m = re.search(rf"const {constante}\s*=\s*\[(.*?)\]", src, re.S)
    assert m, f"no se encontró `const {constante} = [...]` en authErrors.ts"
    return [a or b for a, b in re.findall(r"'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\"", m.group(1))]


def _exigir_frontend():
    if not os.path.isfile(_AUTH_ERRORS):
        pytest.skip("frontend-src no está junto al backend: el espejo de authErrors.ts NO se verificó")


def test_cinturon_estatico():
    """Sin node: los textos del backend están cubiertos por las listas del .ts, y la ruta
    que excluye del cierre de sesión es la que pide authAPI.login."""
    _exigir_frontend()
    src = _leer(_AUTH_ERRORS)
    fallos = []

    ambito = _literales(src, "DETALLES_AMBITO")
    for origen, texto in _textos_de_ambito().items():
        # Mismo criterio que clasificarError: el texto del backend EMPIEZA con el del .ts.
        if not any(texto.startswith(a) for a in ambito):
            fallos.append(f"{origen} emite {texto!r} y DETALLES_AMBITO no lo cubre")

    sesion = _literales(src, "DETALLES_SESION")
    for texto in DETALLES_SIN_SESION:
        if texto not in sesion:
            fallos.append(f"auth_errors.DETALLES_SIN_SESION trae {texto!r} y DETALLES_SESION no")

    m = re.search(r"const RUTA_LOGIN\s*=\s*['\"]([^'\"]+)['\"]", src)
    ruta_api = _ruta_de_login_en_api_ts()
    if not m:
        fallos.append("authErrors.ts no declara RUTA_LOGIN: el 401 del login vuelve a cerrar la sesión vigente")
    elif not ruta_api.endswith(m.group(1)):
        fallos.append(f"authAPI.login pide {ruta_api!r} y RUTA_LOGIN dice {m.group(1)!r}: "
                      "la exclusión del login no se aplica a ninguna petición")

    assert not fallos, ("authErrors.ts NO refleja al backend:\n  - " + "\n  - ".join(fallos)
                        + "\nLos dos lados se cambian juntos (ver el encabezado de authErrors.ts).")


_ENTORNO = """
// Dobles del navegador y de la red. Se importa PRIMERO: el store (zustand persist) toma
// `window.localStorage` al crearse, y los dos clientes heredan el adaptador al hacer
// `axios.create`.
import axios, { AxiosError } from 'axios'

const almacen = new Map<string, string>()
const localStorage = {
  getItem: (k: string) => (almacen.has(k) ? almacen.get(k)! : null),
  setItem: (k: string, v: string) => { almacen.set(k, String(v)) },
  removeItem: (k: string) => { almacen.delete(k) },
}
export const navegador = {
  pathname: '/ventas',
  reemplazos: [] as string[],
  almacen,
  respuesta: { status: 200, detail: undefined as unknown },
  pedidos: [] as string[],
  // Corre al llegar cada petición, ANTES de responder: simula lo que pasa mientras una
  // petición está en vuelo (p. ej. la sesión cambia de token en otra pestaña).
  enVuelo: null as (() => void) | null,
}
const window = {
  localStorage,
  location: {
    get pathname() { return navegador.pathname },
    replace: (a: string) => { navegador.reemplazos.push(a) },
  },
  addEventListener() {},
}
for (const [nombre, value] of Object.entries({ localStorage, window })) {
  Object.defineProperty(globalThis, nombre, { configurable: true, writable: true, value })
}

// La red: toda petición recibe lo que diga `navegador.respuesta`.
axios.defaults.adapter = async (config) => {
  navegador.pedidos.push(`${config.baseURL ?? ''}${config.url ?? ''}`)
  navegador.enVuelo?.()
  const { status, detail } = navegador.respuesta
  const response = { status, statusText: '', data: { detail }, headers: {}, config }
  if (status < 400) return response
  throw new AxiosError(`HTTP ${status}`, AxiosError.ERR_BAD_RESPONSE, config, null, response)
}
"""

_TOAST = """
export const avisos: Array<{ texto: string; id?: string }> = []
const toast = Object.assign(() => {}, {
  error: (texto: string, opciones?: { id?: string }) => { avisos.push({ texto, id: opciones?.id }) },
  success: () => {},
})
export default toast
"""

_SONDA = """
import { navegador } from './entorno'
import { avisos } from 'react-hot-toast'
import { clasificarError } from %(auth_errors)s
import { useAuthStore } from %(store)s
import { authAPI } from %(api)s
import { monzaConfigAPI } from %(monza_api)s

const datos = JSON.parse(process.argv[2])
const USUARIO = { id: 7, email: 'a@b.cl', nombre: 'Prueba', empresa: 'mineria' as const }
const abrirSesion = () => useAuthStore.getState().setAuth('tok-vivo', USUARIO)
const token = () => useAuthStore.getState().token
const guardado = () => JSON.parse(navegador.almacen.get('machparts-auth') ?? '{}')?.state?.token ?? null
// Hace la llamada REAL con la respuesta programada. Que la llamada falle es lo esperado:
// lo que se mira después es qué hizo el interceptor con la sesión.
const pedir = async (llamada: () => Promise<unknown>, status: number, detail?: unknown) => {
  navegador.respuesta = { status, detail }
  try { await llamada() } catch { /* esperado */ }
}

async function correr() {
  const salida: Record<string, unknown> = {}

  // Paso 1: los textos del backend, por el clasificarError real.
  salida.ambito = Object.fromEntries(Object.entries(datos.ambito as Record<string, string>)
    .map(([origen, texto]) => [origen, [clasificarError(403, texto), clasificarError(403, texto + ' (mineria)')]]))
  salida.sesion = (datos.sesion as string[]).map((t) => clasificarError(403, t))
  salida.controles = [clasificarError(403, 'Stock insuficiente'), clasificarError(401), clasificarError(500, 'x')]

  // Paso 2: sesión viva + clave mal tipeada en el login (otra pestaña, parada en /login).
  abrirSesion()
  navegador.pathname = '/login'
  await pedir(() => authAPI.login('a@b.cl', 'clave-mala'), 401, 'Email o contraseña incorrectos')
  salida.login_401 = { pedido: navegador.pedidos.at(-1), token: token(), guardado: guardado(), reemplazos: [...navegador.reemplazos] }

  // Paso 3: 403 de ámbito por los DOS clientes. Avisan con el mismo id y no tocan la sesión.
  abrirSesion()
  await pedir(() => authAPI.me(), 403, datos.ambito.empresa_guard)
  await pedir(() => monzaConfigAPI.get(), 403, datos.ambito.empresa_guard)
  salida.ambito_403 = { avisos: [...avisos], token: token(), reemplazos: [...navegador.reemplazos] }

  // Paso 4: el 401 de cualquier OTRA ruta sí cierra la sesión, por los dos clientes.
  navegador.pathname = '/ventas'
  await pedir(() => authAPI.me(), 401, 'Could not validate credentials')
  const trasMachParts = { pedido: navegador.pedidos.at(-1), token: token(), guardado: guardado() }
  abrirSesion()
  await pedir(() => monzaConfigAPI.get(), 401, 'Could not validate credentials')
  const trasMonza = { pedido: navegador.pedidos.at(-1), token: token(), guardado: guardado() }
  salida.otros_401 = { trasMachParts, trasMonza, reemplazos: [...navegador.reemplazos] }

  // Paso 5: la petición salió con el token de hoy y, mientras volaba, la sesión se renovó
  // (cambio de clave, otra pestaña). Su 401 es del token ANTERIOR: la sesión nueva queda.
  abrirSesion()
  const reemplazosAntes = navegador.reemplazos.length
  navegador.enVuelo = () => useAuthStore.getState().setAuth('tok-renovado', USUARIO)
  await pedir(() => authAPI.me(), 401, 'Could not validate credentials')
  navegador.enVuelo = null
  salida.token_viejo_401 = {
    token: token(), guardado: guardado(),
    reemplazos: navegador.reemplazos.length - reemplazosAntes,
  }

  console.log(JSON.stringify(salida))
}
correr()
"""


def _correr_en_node(datos: dict) -> dict:
    tmp = tempfile.mkdtemp(prefix="espejo-auth-errors-")
    try:
        for nombre, texto in (("entorno.ts", _ENTORNO), ("toast.ts", _TOAST)):
            with open(os.path.join(tmp, nombre), "w", encoding="utf-8") as f:
                f.write(texto)
        with open(os.path.join(tmp, "sonda.ts"), "w", encoding="utf-8") as f:
            f.write(_SONDA % {nombre: json.dumps(ruta[:-3]) for nombre, ruta in (
                ("auth_errors", _AUTH_ERRORS), ("store", _STORE), ("api", _API), ("monza_api", _MONZA_API))})
        js = os.path.join(tmp, "sonda.cjs")
        # --bundle: los clientes, el store (zustand) y axios son los de verdad; solo el
        # toast se reemplaza por un doble que anota los avisos. NODE_PATH: los dobles viven
        # fuera de frontend-src y tienen que encontrar el MISMO axios que usan los clientes
        # (el adaptador se instala en `axios.defaults`).
        r = subprocess.run(
            [_ESBUILD, os.path.join(tmp, "sonda.ts"), "--bundle", "--platform=node",
             "--format=cjs", f"--alias:react-hot-toast={os.path.join(tmp, 'toast.ts')}",
             f"--outfile={js}", "--log-level=error"],
            cwd=_FRONT, capture_output=True, text=True, timeout=180,
            env={**os.environ, "NODE_PATH": os.path.join(_FRONT, "node_modules")})
        assert r.returncode == 0, "esbuild no pudo compilar el espejo:\n" + r.stderr[:2000]
        r = subprocess.run(["node", js, json.dumps(datos)], capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, "el espejo compilado no pudo correr en node:\n" + r.stderr[-2000:]
        return json.loads(r.stdout.strip().splitlines()[-1])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_espejo_compilado():
    _exigir_frontend()
    if not shutil.which("node"):
        pytest.skip("node no está disponible: el espejo de authErrors.ts NO se verificó")
    if not os.path.isfile(_ESBUILD):
        pytest.skip("esbuild no instalado (npm install): el espejo de authErrors.ts NO se verificó")

    ambito = _textos_de_ambito()
    out = _correr_en_node({"ambito": ambito, "sesion": list(DETALLES_SIN_SESION)})
    fallos = []

    def check(nombre, cond, extra=""):
        print(("OK  " if cond else "FAIL") + " | " + nombre + ("" if cond else f"  -> {extra}"))
        if not cond:
            fallos.append(nombre)

    # 1. Textos gemelos
    for origen, texto in ambito.items():
        check(f"1. el 403 de {origen} ({texto!r}) se clasifica 'ambito', también con sufijo",
              out["ambito"][origen] == ["ambito", "ambito"], out["ambito"][origen])
    for texto, clase in zip(DETALLES_SIN_SESION, out["sesion"]):
        check(f"1. el 403 {texto!r} se clasifica 'sesion'", clase == "sesion", clase)
    check("1. controles: otro 403 es 'negocio', el 401 es 'sesion', un 500 no es nada",
          out["controles"] == ["negocio", "sesion", "ninguna"], out["controles"])

    # 2. El login no cierra la sesión vigente
    login = out["login_401"]
    check("2. el 401 de authAPI.login NO cierra la sesión que ya había (ni en memoria ni en localStorage)",
          login["token"] == "tok-vivo" and login["guardado"] == "tok-vivo", login)
    check("2. ...ni navega", login["reemplazos"] == [], login)

    # 3. Ámbito: avisa, no desloguea, no navega
    amb = out["ambito_403"]
    check("3. el 403 de ámbito avisa por los DOS clientes y con el MISMO id (el toast los funde en uno)",
          [a.get("id") for a in amb["avisos"]] == ["error-403-ambito"] * 2, amb["avisos"])
    check("3. ...y la sesión sigue en pie, sin navegar",
          amb["token"] == "tok-vivo" and amb["reemplazos"] == [], amb)

    # 4. Cualquier otro 401 sí cierra la sesión, una sola redirección
    otros = out["otros_401"]
    for cliente in ("trasMachParts", "trasMonza"):
        r = otros[cliente]
        check(f"4. un 401 de {r['pedido']} cierra la sesión en memoria y en localStorage ({cliente})",
              r["token"] is None and r["guardado"] is None, r)
    check("4. ...y dos 401 seguidos navegan UNA sola vez al login",
          otros["reemplazos"] == ["/login"], otros["reemplazos"])

    viejo = out["token_viejo_401"]
    check("5. un 401 de una petición que viajó con OTRO token no cierra la sesión renovada ni navega",
          viejo["token"] == "tok-renovado" and viejo["guardado"] == "tok-renovado" and viejo["reemplazos"] == 0,
          viejo)

    assert not fallos, f"{len(fallos)} checks fallaron: {fallos}"


if __name__ == "__main__":
    test_cinturon_estatico()
    test_espejo_compilado()
    print("\n=== TODO OK ===")
