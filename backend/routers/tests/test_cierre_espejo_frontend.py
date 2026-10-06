"""ESPEJO del Cierre de Venta: la pantalla cuenta los días hábiles con la MISMA tabla que
el backend, y sugiere la fecha de la orden con la MISMA regla con que el backend congela
la fecha de cada ítem.

POR QUÉ EXISTE ESTE ARCHIVO
---------------------------
La fecha prometida de cada ítem la congela el backend (crear_oc_cliente, con
add_business_days y _is_chile_holiday de routers/compras.py); la fecha de entrega que el
comercial ve y confirma la calcula el NAVEGADOR (isHoliday, addBusinessDays y
plazoDeLaOrden de CierreVentaPage.tsx). Son dos copias de una misma cuenta. Cuando la
pantalla descontaba Jueves Santo y el «tercer lunes de junio» y el backend el 20 de
junio fijo, el comercial confirmaba 2027-03-29 y el ítem quedaba sellado el 2027-03-25
(cierre 2027-02-11 + 30 hábiles): dos calendarios para una misma promesa, que ninguna
suite de un solo lado puede ver.

QUÉ HACE
--------
Extrae VERBATIM de CierreVentaPage.tsx el tramo entre `function easterDate(` y el JSDoc
de toDateInput (easterDate, isHoliday, addBusinessDays y plazoDeLaOrden), lo compila con
el esbuild que ya vive en frontend-src/node_modules (sin dependencias nuevas) y lo
ejecuta en Node con el huso de Chile, comparando contra las funciones reales del backend
para los MISMOS datos. No reimplementa nada: si alguien edita el .tsx, corre el código
nuevo (patrón de la casa: monza_tests/test_tarifa_espejo_frontend.py).

  m0  El calendario del backend fijado con LITERALES (sin node): así el espejo no puede
      ser dos tablas equivocadas que coinciden.
  m1  Día por día de 2026 a 2035: isHoliday(pantalla) == _is_chile_holiday(backend).
  m2  addBusinessDays(pantalla) == add_business_days(backend) para cada día de 2026 a
      2028 × plazos 1, 10, 30, 45 y 120 (incluye los dos casos del hallazgo).
  m3  plazoDeLaOrden(pantalla) == el mayor plazo_prometido(...) del backend: el ítem sin
      plazo propio cuenta con el plazo por defecto en los dos lados.

Sin node o sin esbuild se SALTA con motivo explícito (nunca verde fingiendo que
verificó). Si falta un marcador en el .tsx, FALLA: el espejo quedaría ciego.

DÓNDE BUSCA LA PANTALLA
    En frontend-src, junto a backend/. La variable de entorno PARTSCONTROL_FRONTEND_SRC
    apunta a otra carpeta con la misma forma (src/pages/… y node_modules): sirve para
    correr el espejo desde una copia de backend/ (el arnés de mutaciones copia solo
    backend/) y para probar una COPIA mutada de la pantalla sin tocar el árbol de
    trabajo.

Corre con:  ./venv/bin/python -m pytest routers/tests/test_cierre_espejo_frontend.py -q
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import pytest  # noqa: E402

from routers.compras import (  # noqa: E402
    _is_chile_holiday, add_business_days, plazo_prometido,
)

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_FRONT = (os.environ.get("PARTSCONTROL_FRONTEND_SRC")
          or os.path.join(os.path.dirname(_BACKEND), "frontend-src"))
_PAGINA = os.path.join(_FRONT, "src", "pages", "CierreVentaPage.tsx")

# Los dos marcadores existen UNA sola vez en el .tsx: el motor de fechas va entre ellos.
_INICIO = "function easterDate("
_FIN = "/** Formatea Date a YYYY-MM-DD"

PLAZOS = (1, 10, 30, 45, 120)
PRIMER_DIA = date(2026, 1, 1)
ULTIMO_ANIO_FERIADOS = 2035
ULTIMO_ANIO_SUMAS = 2028
# Los mismos cinco casos que arma el guion de Node (allá null y undefined son el ítem sin
# plazo propio; acá None): (plazos de los ítems, plazo por defecto del cotizador).
CASOS_PLAZO_DE_LA_ORDEN = [
    ([10, None], 45),
    ([10, 60, None], 45),
    ([], 45),
    ([0], 45),
    ([None, None], 30),
]

# La sonda que corre en Node a continuación del código real de la pantalla. La grilla se
# genera allá (mediodía, para que el cambio de hora no corra el día) y se compara acá.
_SONDA = f"""
const iso = (d: Date) => `${{d.getFullYear()}}-${{String(d.getMonth() + 1).padStart(2, '0')}}-${{String(d.getDate()).padStart(2, '0')}}`
const feriados: string[] = []
const sumas: Record<string, string> = {{}}
for (let d = new Date({PRIMER_DIA.year}, {PRIMER_DIA.month - 1}, {PRIMER_DIA.day}, 12); d.getFullYear() <= {ULTIMO_ANIO_FERIADOS}; d.setDate(d.getDate() + 1)) {{
  if (isHoliday(d)) feriados.push(iso(d))
  if (d.getFullYear() <= {ULTIMO_ANIO_SUMAS}) for (const n of {list(PLAZOS)}) sumas[`${{iso(d)}}+${{n}}`] = iso(addBusinessDays(d, n))
}}
const casosPlazo: [(number | null | undefined)[], number][] = [
  [[10, null], 45], [[10, 60, undefined], 45], [[], 45], [[0], 45], [[null, null], 30],
]
const plazos = (typeof plazoDeLaOrden === 'function')
  ? casosPlazo.map(([p, porDefecto]) => plazoDeLaOrden(p, porDefecto))
  : null
console.log(JSON.stringify({{ feriados, sumas, plazos }}))
"""


def _extraer_motor_de_la_pantalla() -> str:
    """El motor de fechas tal cual está en el .tsx (sin copiarlo aquí). Un marcador que
    falta o se repite es un FALLO, no un salto: el espejo quedaría ciego."""
    with open(_PAGINA, encoding="utf-8") as f:
        src = f.read()
    for marcador in (_INICIO, _FIN):
        assert src.count(marcador) == 1, (
            f"el marcador {marcador!r} aparece {src.count(marcador)} veces en "
            f"CierreVentaPage.tsx (tiene que aparecer una): el espejo no sabe qué extraer")
    return src[src.index(_INICIO):src.index(_FIN)]


def _correr_en_node() -> dict:
    """Compila el código REAL de la pantalla y devuelve lo que calcula."""
    guion = _extraer_motor_de_la_pantalla() + _SONDA
    tmp = tempfile.mkdtemp(prefix="espejo-cierre-")
    try:
        ts = os.path.join(tmp, "sonda.ts")
        js = os.path.join(tmp, "sonda.cjs")
        with open(ts, "w", encoding="utf-8") as f:
            f.write(guion)
        subprocess.run(["npx", "esbuild", ts, "--format=cjs", f"--outfile={js}"],
                       cwd=_FRONT, check=True, capture_output=True, timeout=180)
        # El huso del navegador del comercial: la pantalla trabaja con fechas locales.
        r = subprocess.run(["node", js], check=True, capture_output=True, text=True,
                           timeout=120, env={**os.environ, "TZ": "America/Santiago"})
        return json.loads(r.stdout.strip().splitlines()[-1])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(scope="module")
def pantalla() -> dict:
    """Lo que calcula la pantalla, compilada UNA vez para m1, m2 y m3."""
    if not shutil.which("node"):
        pytest.skip("node no está disponible: el espejo del frontend NO se verificó")
    if not os.path.isdir(os.path.join(_FRONT, "node_modules", "esbuild")):
        pytest.skip("esbuild no instalado (npm install en frontend-src): el espejo del "
                    "frontend NO se verificó")
    if not os.path.isfile(_PAGINA):
        pytest.skip(f"no está {_PAGINA} (PARTSCONTROL_FRONTEND_SRC apunta a la carpeta "
                    "frontend-src): el espejo del frontend NO se verificó")
    return _correr_en_node()


def _dias(hasta_anio: int):
    d = PRIMER_DIA
    while d.year <= hasta_anio:
        yield d
        d += timedelta(days=1)


# ══ m0 — El calendario del backend, con literales ════════════════════════════════════

def test_m0_el_calendario_del_backend_fijado_con_literales():
    """Fechas medidas a mano sobre la tabla del backend. Sin esto, el espejo podría ser
    dos tablas IGUALMENTE equivocadas que coinciden."""
    # 2027: Pascua el 28 de marzo. Jueves Santo (25) cuenta como hábil...
    assert add_business_days(date(2027, 2, 11), 30) == date(2027, 3, 25)
    # ...y Viernes Santo (26) no: el hábil siguiente es el lunes 29.
    assert add_business_days(date(2027, 2, 11), 31) == date(2027, 3, 29)
    # El 20 de junio (Día de los Pueblos Indígenas, fijo) no cuenta: 2028-06-20 es martes.
    assert add_business_days(date(2028, 6, 19), 1) == date(2028, 6, 21)


# ══ m1 — La misma tabla de feriados ══════════════════════════════════════════════════

def test_m1_la_pantalla_y_el_backend_tienen_la_misma_tabla_de_feriados(pantalla):
    de_la_pantalla = set(pantalla["feriados"])
    difs = []
    for d in _dias(ULTIMO_ANIO_FERIADOS):
        backend = _is_chile_holiday(d)
        if (d.isoformat() in de_la_pantalla) != backend:
            difs.append(f"{d.isoformat()}: la pantalla dice {'hábil' if backend else 'feriado'} "
                        f"y el backend {'feriado' if backend else 'hábil'}")
    assert not difs, (
        f"{len(difs)} fechas con dos calendarios (isHoliday de CierreVentaPage.tsx vs "
        "_is_chile_holiday de routers/compras.py):\n  - " + "\n  - ".join(difs[:40]))


# ══ m2 — La misma suma de días hábiles ═══════════════════════════════════════════════

def test_m2_la_pantalla_y_el_backend_suman_los_mismos_dias_habiles(pantalla):
    sumas = pantalla["sumas"]
    difs = []
    for d in _dias(ULTIMO_ANIO_SUMAS):
        for n in PLAZOS:
            backend = add_business_days(d, n).isoformat()
            de_la_pantalla = sumas.get(f"{d.isoformat()}+{n}")
            if de_la_pantalla != backend:
                difs.append(f"{d.isoformat()} + {n}: pantalla {de_la_pantalla}, backend {backend}")
    assert not difs, (
        f"{len(difs)} sumas distintas de {len(sumas)} (addBusinessDays de CierreVentaPage.tsx "
        "vs add_business_days de routers/compras.py):\n  - " + "\n  - ".join(difs[:40]))
    # Los dos casos del hallazgo, con nombre propio.
    assert sumas["2027-02-11+30"] == add_business_days(date(2027, 2, 11), 30).isoformat()
    assert sumas["2026-10-05+120"] == add_business_days(date(2026, 10, 5), 120).isoformat()


# ══ m3 — La fecha sugerida de la orden cuenta el default como el backend ═════════════

def test_m3_la_fecha_sugerida_de_la_orden_cuenta_el_default_como_el_backend(pantalla):
    assert pantalla["plazos"] is not None, (
        "la pantalla no define plazoDeLaOrden entre addBusinessDays y toDateInput: la "
        "fecha sugerida para la orden no sigue la regla con que el backend congela "
        "(plazo_prometido, routers/compras.py)")
    difs = []
    for (plazos, por_defecto), de_la_pantalla in zip(CASOS_PLAZO_DE_LA_ORDEN, pantalla["plazos"]):
        del_backend = max((plazo_prometido(p, por_defecto) for p in plazos),
                          default=plazo_prometido(None, por_defecto))
        if de_la_pantalla != del_backend:
            difs.append(f"plazos {plazos} con default {por_defecto}: pantalla "
                        f"{de_la_pantalla}, backend {del_backend}")
    assert not difs, (
        "plazoDeLaOrden (CierreVentaPage.tsx) y plazo_prometido (routers/compras.py) no "
        "cuentan igual al ítem sin plazo propio:\n  - " + "\n  - ".join(difs))
