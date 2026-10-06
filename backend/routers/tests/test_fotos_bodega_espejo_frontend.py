"""ESPEJO del frontend de las fotos de recepción: el caché de <FotoBodega> y la fecha de
la galería de Despachos. El código del frontend se COMPILA con el esbuild que ya vive en
node_modules y se ejecuta en Node tal cual está en el repo, sin copiarlo acá (mismo
molde que monza_tests/test_tarifa_espejo_frontend.py).

QUÉ PROTEGE
    · El caché de fotos es de UNA sesión. Sus blobs se bajaron con el permiso de quien
      estaba conectado y salir navega por la SPA sin recargar: tiene que vaciarse con
      cada cambio de token (botón de salir, otro inicio de sesión, la otra pestaña) y lo
      que venía viajando no puede volver a entrar.
    · La fecha de cada foto en la galería de Despachos es el día de CHILE: el servidor la
      guarda en UTC y sin zona, y leída como hora local una foto de las 20:30 se rotula
      con el día siguiente.

SONDAS DE PODER DISCRIMINANTE — qué hay que quitar para que cada prueba falle
    · test_cache_de_fotos_es_de_una_sesion
        ← en `limpiarCacheFotos`, quitar `for (const id of enVuelo.keys())
          generacion.set(id, gen(id) + 1)`: la foto que venía viajando entra al caché.
        ← quitar el `useAuthStore.subscribe(...)` de FotoBodega.tsx: ningún cambio de
          sesión vacía el caché.
        ← quitar la condición `estado.token !== anterior.token`: releer la misma sesión
          bota las fotos ya cargadas.
        ← agregar `montadas.clear()` a `limpiarCacheFotos`: el conteo de las miniaturas
          que siguen en pantalla queda en cero.
    · test_fecha_de_la_foto_es_el_dia_de_chile
        ← en utils/format.ts::fmtFechaServidor, quitar el `+ 'Z'` (falla con
          TZ=America/Santiago) o quitar `timeZone: 'America/Santiago'` (falla con TZ=UTC).
    · test_estatico_la_galeria_usa_el_formateador_compartido
        ← que la figcaption vuelva a formatear con `new Date(f.fecha)`: la cadena
          `fmtFechaServidor(f.fecha)` desaparece. La segunda aserción es una red para
          una vuelta PARCIAL (`cond ? fmtFechaServidor(f.fecha) : new Date(f.fecha)`)
          y no prohíbe `Date` en el resto del archivo.
    Estas mutaciones viven en archivos del frontend, fuera del alcance del arnés de
    portal/tests/mutaciones (que copia solo backend/): se verifican sobre una copia
    temporal de frontend-src, nunca sobre el árbol de trabajo.

Si falta node o esbuild, las pruebas compiladas se SALTAN con motivo; la estática corre
siempre. No toca la base de datos.

Corre con:  ./venv/bin/python -m pytest routers/tests/test_fotos_bodega_espejo_frontend.py -q
"""
import json
import os
import shutil
import subprocess
import tempfile

import pytest

_BACK = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_FRONT = os.path.join(os.path.dirname(_BACK), "frontend-src")


def _ruta(*partes: str) -> str:
    return os.path.join(_FRONT, "src", *partes)


def _leer(*partes: str) -> str:
    with open(_ruta(*partes), encoding="utf-8") as f:
        return f.read()


def _exigir_node() -> None:
    if not shutil.which("node"):
        pytest.skip("node no está disponible: el espejo del frontend NO se verificó")
    if not os.path.isfile(os.path.join(_FRONT, "node_modules", ".bin", "esbuild")):
        pytest.skip("esbuild no instalado (npm install): el espejo del frontend NO se verificó")


def _correr_en_node(guion: str, zona: str = "UTC") -> dict:
    """Empaqueta el guion (con lo que importe del frontend) y devuelve su última línea."""
    tmp = tempfile.mkdtemp(prefix="espejo-fotos-")
    try:
        ts, js = os.path.join(tmp, "sonda.ts"), os.path.join(tmp, "sonda.cjs")
        with open(ts, "w", encoding="utf-8") as f:
            f.write(guion)
        subprocess.run(
            [os.path.join(_FRONT, "node_modules", ".bin", "esbuild"), ts, "--bundle",
             "--platform=node", "--format=cjs", "--log-level=warning", f"--outfile={js}"],
            cwd=_FRONT, check=True, capture_output=True, text=True, timeout=180)
        r = subprocess.run(["node", js], check=True, capture_output=True, text=True,
                           timeout=120, env={**os.environ, "TZ": zona})
        return json.loads(r.stdout.strip().splitlines()[-1])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ─── El caché de <FotoBodega> ──────────────────────────────────────────────────

def _bloque_del_cache() -> str:
    """El caché tal cual está en FotoBodega.tsx: desde sus mapas hasta antes del
    componente. No incluye React ni axios; `api` y el store los pone el guion."""
    src = _leer("components", "FotoBodega.tsx")
    marca_ini, marca_fin = "const cache = new Map<number, string>()", "type Props = {"
    assert marca_ini in src and marca_fin in src, (
        "no se encontró el bloque del caché en components/FotoBodega.tsx: si se movió, "
        "hay que actualizar las dos marcas de esta prueba")
    return src[src.index(marca_ini):src.index(marca_fin)]


_PRELUDIO = r"""
// `api` de mentira: cada GET queda pendiente hasta que el guion lo hace llegar.
const peticiones: { ruta: string; llegar: (b: unknown) => void }[] = []
const api = {
  get: (ruta: string, _opciones?: unknown) =>
    new Promise<{ data: unknown }>((resolver) => {
      peticiones.push({ ruta, llegar: (b) => resolver({ data: b }) })
    }),
}
const creadas: string[] = []
const revocadas: string[] = []
;(URL as any).createObjectURL = () => { const u = `blob:falso-${creadas.length + 1}`; creadas.push(u); return u }
;(URL as any).revokeObjectURL = (u: string) => { revocadas.push(u) }

// localStorage de mentira, y captura del oyente `storage` de stores/authStore.ts (así
// llega a una pestaña el cambio de sesión hecho en OTRA).
const almacen = new Map<string, string>()
const oyentes: ((e: { key: string; newValue: string | null }) => void)[] = []
;(globalThis as any).window = {
  localStorage: {
    getItem: (k: string) => almacen.get(k) ?? null,
    setItem: (k: string, v: string) => { almacen.set(k, v) },
    removeItem: (k: string) => { almacen.delete(k) },
  },
  addEventListener: (tipo: string, fn: any) => { if (tipo === 'storage') oyentes.push(fn) },
}
// `require` y no `import`: el store tiene que evaluarse DESPUÉS de armar `window`.
const { useAuthStore } = require(__AUTHSTORE__)
"""

_GUION_CACHE = r"""
const fallos: string[] = []
const check = (nombre: string, cond: boolean, extra: unknown = '') => {
  if (!cond) fallos.push(`${nombre} -> ${JSON.stringify(extra)}`)
}
const tick = () => new Promise((r) => setTimeout(r, 0))
const usuario = (id: number) => ({ id, email: `u${id}@test.invalid`, nombre: 'x', empresa: 'mineria' })
const CLAVE = 'machparts-auth'

async function llenar(id: number) {
  const p = cargar(id)
  peticiones[peticiones.length - 1].llegar({ foto: id })
  return p
}

async function main() {
  useAuthStore.getState().setAuth('T1', usuario(1))

  // 0. Contrapeso: sin cambio de sesión el caché SÍ guarda (si no, lo demás pasaría vacío).
  const url1 = await llenar(1)
  const antes = peticiones.length
  check('control: la foto queda en caché', cache.get(1) === url1, [...cache])
  check('control: pedirla de nuevo no sale a la red',
        (await cargar(1)) === url1 && peticiones.length === antes, peticiones.length)

  // 1. Botón de salir, con una foto viajando y una miniatura en pantalla.
  const viajando = cargar(7)
  const pedido7 = peticiones[peticiones.length - 1]
  let rechazo = ''
  viajando.catch((e: Error) => { rechazo = e.message })
  montar(9)
  useAuthStore.getState().logout()
  check('salir vacía el caché', cache.size === 0, [...cache])
  check('salir revoca el blob que había', revocadas.includes(url1), revocadas)
  check('el conteo de las miniaturas en pantalla sobrevive', montadas.get(9) === 1, [...montadas])
  pedido7.llegar({ foto: 7 })
  await tick(); await tick()
  check('la foto que venía viajando NO entra al caché', !cache.has(7), [...cache])
  check('y su blob se revoca', revocadas.includes(creadas[creadas.length - 1]), { creadas, revocadas })
  check('y quien la esperaba recibe el rechazo', rechazo !== '', rechazo)
  const n = peticiones.length
  void cargar(7).catch(() => {})
  check('pedirla después sale de nuevo a la red', peticiones.length === n + 1, peticiones.length)
  peticiones[peticiones.length - 1].llegar({ foto: 7 })
  await tick(); await tick()
  desmontar(9)

  // 2. Iniciar sesión con otra cuenta, sin haber salido.
  useAuthStore.getState().setAuth('T2', usuario(2))
  check('otro inicio de sesión vacía el caché', cache.size === 0, [...cache])
  await llenar(2)
  check('control: con la sesión nueva se vuelve a llenar', cache.has(2), [...cache])

  // 3. La OTRA pestaña cambia la sesión: llega por el evento `storage`.
  check('authStore registró su oyente de storage', oyentes.length === 1, oyentes.length)
  const otra = JSON.stringify({ state: { token: 'T3', user: usuario(3) }, version: 0 })
  almacen.set(CLAVE, otra)
  oyentes[0]({ key: CLAVE, newValue: otra })
  await tick(); await tick()
  check('el store adoptó la sesión de la otra pestaña',
        useAuthStore.getState().token === 'T3', useAuthStore.getState().token)
  check('el cambio desde otra pestaña vacía el caché', cache.size === 0, [...cache])

  // 4. Releer el store SIN cambio de token no bota nada.
  await llenar(4)
  oyentes[0]({ key: CLAVE, newValue: otra })
  await tick(); await tick()
  check('releer la misma sesión no vacía el caché', cache.has(4), [...cache])

  console.log(JSON.stringify({ fallos }))
}
main().catch((e) => {
  console.log(JSON.stringify({ fallos: ['el guion reventó: ' + String((e && e.stack) || e)] }))
})
"""


def test_cache_de_fotos_es_de_una_sesion():
    _exigir_node()
    preludio = _PRELUDIO.replace("__AUTHSTORE__", json.dumps(_ruta("stores", "authStore.ts")))
    salida = _correr_en_node(preludio + _bloque_del_cache() + _GUION_CACHE)
    assert not salida["fallos"], (
        "El caché de <FotoBodega> no se comporta como de UNA sesión:\n  - "
        + "\n  - ".join(salida["fallos"]))


# ─── La fecha de la galería de Despachos ───────────────────────────────────────

# (ISO que guarda el servidor, en UTC y sin zona) -> día de Chile en que se tomó la foto.
_FECHAS = [
    ("2026-08-21T00:30:00", "20-08-2026"),   # 20:30 del 20 de agosto (invierno, UTC-4)
    ("2026-12-16T00:30:00", "15-12-2026"),   # 21:30 del 15 de diciembre (verano, UTC-3)
    ("2026-08-20T19:00:00", "20-08-2026"),   # 15:00: de día no hay corrimiento
    ("2026-08-21T04:00:00", "21-08-2026"),   # medianoche exacta de Chile: ya es el 21
]


def test_fecha_de_la_foto_es_el_dia_de_chile():
    _exigir_node()
    guion = ("import { fmtFechaServidor } from %s\n" % json.dumps(_ruta("utils", "format.ts"))
             + "const casos: string[] = %s\n" % json.dumps([iso for iso, _ in _FECHAS])
             + "console.log(JSON.stringify({ salida: casos.map((c) => fmtFechaServidor(c)) }))\n")
    esperado = [dia for _, dia in _FECHAS]
    # Dos zonas a propósito: con una sola, una de las dos mitades de la regla (declarar
    # el UTC; formatear en la zona de Chile) puede faltar sin que cambie el resultado.
    for zona in ("America/Santiago", "UTC"):
        salida = _correr_en_node(guion, zona=zona)["salida"]
        assert salida == esperado, (
            f"fmtFechaServidor no da el día de Chile con el equipo en {zona}: "
            f"{dict(zip([iso for iso, _ in _FECHAS], salida))}")


def test_estatico_la_galeria_usa_el_formateador_compartido():
    """Sin node: la fecha de la foto pasa por el formateador compartido, no por una
    lectura propia de `f.fecha`. Es una sonda de texto sobre ESA fecha: el resto del
    archivo puede usar `Date` para lo que necesite."""
    src = _leer("components", "FotosItemModal.tsx")
    assert "fmtFechaServidor(f.fecha)" in src, (
        "FotosItemModal.tsx no pinta la fecha de la foto con fmtFechaServidor "
        "(utils/format.ts)")
    assert "new Date(f.fecha)" not in src, (
        "la figcaption de FotosItemModal.tsx volvió a leer `f.fecha` con `new Date(...)`: "
        "sin declarar el UTC, una foto subida de noche se rotula con el día siguiente")
