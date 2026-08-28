"""ESPEJO del frontend: Contado = adelanto del 100% (arreglo 8 del equipo, 2026-08-21).

POR QUÉ UN ESPEJO
    La mitad que decide la condición de pago es TypeScript (constants/adelanto.ts y
    opcionDeVenta en MonzaCotizacionesPage.tsx): pct_adelanto es «el dato que mueve
    plata» y el backend lo recibe ya decidido. Un check solo-Python no se enteraría si
    la constante volviera a 0 — el mismo punto ciego que test_tarifa_espejo_frontend
    existe para cubrir. El código del frontend se COMPILA (esbuild) y se ejecuta tal
    cual está en el repo, sin copiarlo aquí.

QUÉ PINEA
    · La opción 'contado' lleva pct 100 y forma "Contado": con 0, el cierre al contado
      no pedía verificación de Tesorería y Abastecimiento compraba sin confirmar la
      plata (el pedido #8 del equipo).
    · CASO LEGADO: opcionDeVenta("Contado", 0) —venta cerrada cuando contado valía 0—
      preselecciona Contado. El re-cierre enviará 100: es DELIBERADO (freeze-forward
      al re-cierre) y el modal lo avisa (subeAdelanto); sin la rama legado caería en
      "Crédito (30 días)", una condición que el cliente jamás pactó.
    · Los créditos 60/90 y el % libre siguen resolviendo igual (no romper el hallazgo A4).

CINTURÓN ESTÁTICO
    Aunque falte node, el test lee el .ts y exige pct: 100 en la opción contado — el
    espejo compilado queda en skip CON MOTIVO, nunca en verde vacío.

Corre con:  ./venv/bin/python -m pytest monza_tests/test_adelanto_espejo_frontend.py -q
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

_BACK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FRONT = os.path.join(os.path.dirname(_BACK), "frontend-src")
_ADELANTO = os.path.join(_FRONT, "src", "constants", "adelanto.ts")
_PAGINA = os.path.join(_FRONT, "src", "pages", "MonzaCotizacionesPage.tsx")


def _leer(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_estatico_contado_pct_100():
    """Cinturón sin node: la constante del repo dice pct 100 en la opción contado."""
    src = _leer(_ADELANTO)
    m = re.search(r'id:\s*"contado".*?pct:\s*(\d+).*?forma:\s*"([^"]+)"', src, re.S)
    assert m, "no se encontró la opción contado en constants/adelanto.ts"
    assert m.group(1) == "100", (
        f"contado lleva pct {m.group(1)}: con un valor distinto de 100 la venta al "
        "contado no exige verificación de Tesorería (pedido #8 del equipo)")
    assert m.group(2) == "Contado", m.group(2)


def _extraer_opcion_de_venta() -> str:
    """La función opcionDeVenta tal cual está en el .tsx (sin copiarla aquí)."""
    src = _leer(_PAGINA)
    ini = src.index("function opcionDeVenta")
    fin = src.index("\n}", ini) + 2
    return src[ini:fin]


def test_espejo_compilado_contado_y_caso_legado():
    if not shutil.which("node"):
        pytest.skip("node no está disponible: el espejo del frontend NO se verificó")
    if not os.path.isdir(os.path.join(_FRONT, "node_modules", "esbuild")):
        pytest.skip("esbuild no instalado (npm install): el espejo del frontend NO se verificó")

    # adelanto.ts es autocontenido; opcionDeVenta referencia PAGO_OPCIONES/PagoOpcion,
    # que quedan definidos arriba en el mismo módulo compilado.
    guion = _leer(_ADELANTO) + "\n" + _extraer_opcion_de_venta() + """
const contado = PAGO_OPCIONES.find((o) => o.id === "contado");
const salida = {
  contado_pct: contado ? contado.pct : null,
  contado_forma: contado ? contado.forma : null,
  // PIN del caso legado: venta vieja forma "Contado" + pct 0 → preselecciona Contado.
  // El re-cierre subirá 0→100 A PROPÓSITO (el modal lo avisa con subeAdelanto).
  legado_contado: opcionDeVenta("Contado", 0).id,
  contado_nuevo: opcionDeVenta("Contado", 100).id,
  adelanto50: opcionDeVenta("50% adelanto", 50).id,
  credito60: opcionDeVenta("60 días contra factura", 0).id,
  credito90: opcionDeVenta("90 días contra factura", 0).id,
  libre30: opcionDeVenta("adelanto", 30).id,
};
console.log(JSON.stringify(salida));
"""
    tmp = tempfile.mkdtemp(prefix="espejo-adelanto-")
    try:
        ts = os.path.join(tmp, "sonda.ts")
        js = os.path.join(tmp, "sonda.cjs")
        with open(ts, "w", encoding="utf-8") as f:
            f.write(guion)
        subprocess.run(["npx", "esbuild", ts, "--format=cjs", f"--outfile={js}"],
                       cwd=_FRONT, check=True, capture_output=True, timeout=180)
        r = subprocess.run(["node", js], check=True, capture_output=True, text=True,
                           timeout=120)
        out = json.loads(r.stdout.strip().splitlines()[-1])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    fallos = []
    esperado = {
        "contado_pct": 100,
        "contado_forma": "Contado",
        "legado_contado": "contado",
        "contado_nuevo": "contado",
        "adelanto50": "adelanto50",
        "credito60": "credito60",
        "credito90": "credito90",
        "libre30": "adelanto_libre",
    }
    for clave, valor in esperado.items():
        if out.get(clave) != valor:
            fallos.append(f"{clave}: esperado {valor!r}, la pantalla resuelve {out.get(clave)!r}")
    assert not fallos, "El frontend NO refleja la regla del Contado:\n  - " + "\n  - ".join(fallos)
    print("Espejo compilado OK:", out)


if __name__ == "__main__":
    test_estatico_contado_pct_100()
    test_espejo_compilado_contado_y_caso_legado()
