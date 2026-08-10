"""El ESPEJO de la tarifa aérea: la pantalla tiene que dar el MISMO número que el backend.

POR QUÉ EXISTE ESTE ARCHIVO
---------------------------
El precio de una cotización Monza lo calcula el NAVEGADOR: el modal de la calculadora
computa `precio_clp` y `/aplicar` lo persiste tal cual (`/cotizador/calcular` no tiene
ningún llamador en la UI). O sea, la mitad que fija el precio de venta es TypeScript.

Cuando la tarifa aérea se partió por moneda (2026-08-08), la suite cubrió solo la mitad
Python y el defecto sobrevivió VIVO en la pantalla: `tarifaEfectiva` recibía el config
con `moneda_tarifa` ya pisado por la moneda elegida, así que su respaldo del legado
comparaba la moneda contra sí misma —una tautología— y prestaba la tarifa de EUR a USD.
El aviso y el bloqueo que debían frenarlo eran código muerto. Lo encontró el
multienjambre, no los tests: siete checks verdes y el precio seguía mal.

QUÉ HACE
--------
Extrae VERBATIM del .tsx la interfaz `Config`, `tarifaEfectiva` y `calcularPrecio`, los
compila con el esbuild que ya vive en node_modules (sin dependencias nuevas) y ejecuta la
tabla de verdad en Node, comparando contra el resultado del backend para los MISMOS
datos. No reimplementa nada: si alguien edita el .tsx, este test corre el código nuevo.

Si no hay node/esbuild, el test se SALTA con un motivo explícito — nunca pasa en verde
fingiendo que verificó algo.

Corre con:  ./venv/bin/python -m pytest monza_tests/test_tarifa_espejo_frontend.py -q
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from monza_models import MonzaConfig  # noqa: E402
from monza_router_cotizador import _calcular_precio, _flete_efectivo  # noqa: E402

_RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_MODAL = os.path.join(_RAIZ, "frontend-src", "src", "pages", "MonzaCotizadorModal.tsx")
_FRONT = os.path.join(_RAIZ, "frontend-src")

# Escenarios: (tarifa EUR, tarifa USD, moneda del legado, tarifa legado).
# El 2º es EXACTAMENTE el estado que deja la migración el día del deploy (una moneda
# cargada, la otra en NULL), que es donde vivía el defecto.
ESCENARIOS = [
    (4.5, 6.0, "EUR", 4.5),      # las dos cargadas
    (4.5, None, "EUR", 4.5),     # ← el día del deploy: USD sin cargar
    (None, 6.0, "USD", 6.0),     # simétrico: EUR sin cargar
    (None, None, "EUR", 4.5),    # solo el legado
]
TC_EUR, TC_USD = 1000.0, 900.0
COSTO, MONEDA_ITEM, PESO, MARKUP = 100.0, "USD", 10.0, 28.0


def _extraer_codigo_del_modal() -> str:
    """La interfaz + los dos helpers, tal cual están en el .tsx (sin copiarlos aquí)."""
    src = open(_MODAL, encoding="utf-8").read()
    ini, fin = src.index("interface Config {"), src.index("function fmt(n: number)")
    return src[ini:fin]


def _correr_en_node(casos: list) -> list:
    """Compila el código REAL del modal y devuelve su resultado para cada caso."""
    guion = _extraer_codigo_del_modal() + """
const casos = JSON.parse(process.argv[2]);
const salida = casos.map((c) => {
  const cfg = { tc_usd_clp: c.tc_usd, tc_eur_clp: c.tc_eur, iva_pct: 19,
    tarifa_aerea_eur_por_kg: c.eur, tarifa_aerea_usd_por_kg: c.usd,
    tarifa_aerea_por_kg: c.legado, moneda_tarifa: c.moneda_legado };
  // EXACTAMENTE como lo arma el componente (cfgEfectivo).
  const ef = { ...cfg, moneda_tarifa: c.elegida, moneda_tarifa_legado: cfg.moneda_tarifa };
  const tarifa = tarifaEfectiva(ef, c.elegida);
  const p = calcularPrecio(c.costo, c.moneda_item, c.peso, c.markup, ef);
  return { tarifa, falta: tarifa == null, precio_neto: p.precio_neto };
});
console.log(JSON.stringify(salida));
"""
    tmp = tempfile.mkdtemp(prefix="espejo-tarifa-")
    try:
        ts = os.path.join(tmp, "sonda.ts")
        js = os.path.join(tmp, "sonda.cjs")
        with open(ts, "w", encoding="utf-8") as f:
            f.write(guion)
        subprocess.run(["npx", "esbuild", ts, "--format=cjs", f"--outfile={js}"],
                       cwd=_FRONT, check=True, capture_output=True, timeout=180)
        r = subprocess.run(["node", js, json.dumps(casos)],
                           check=True, capture_output=True, text=True, timeout=120)
        return json.loads(r.stdout.strip().splitlines()[-1])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _backend(eur, usd, moneda_legado, legado, elegida):
    """Lo que el backend calcula para el mismo caso: (tarifa, falta, precio_neto)."""
    cfg = MonzaConfig(id=1, tc_eur_clp=TC_EUR, tc_usd_clp=TC_USD, iva_pct=19,
                      tarifa_aerea_eur_por_kg=eur, tarifa_aerea_usd_por_kg=usd,
                      tarifa_aerea_por_kg=legado, moneda_tarifa=moneda_legado)
    try:
        _mon, tarifa = _flete_efectivo(cfg, elegida)
    except HTTPException:
        return None, True, 0          # falla cerrado: no hay precio
    p = _calcular_precio(COSTO, MONEDA_ITEM, PESO, MARKUP, cfg, elegida)
    return tarifa, False, p["precio_neto"]


def test_la_pantalla_da_el_mismo_numero_que_el_backend():
    if not shutil.which("node"):
        pytest.skip("node no está disponible: el espejo del frontend NO se verificó")
    if not os.path.isdir(os.path.join(_FRONT, "node_modules", "esbuild")):
        pytest.skip("esbuild no instalado (npm install): el espejo del frontend NO se verificó")

    casos = [
        {"eur": eur, "usd": usd, "legado": legado, "moneda_legado": mon, "elegida": elegida,
         "tc_eur": TC_EUR, "tc_usd": TC_USD, "costo": COSTO, "moneda_item": MONEDA_ITEM,
         "peso": PESO, "markup": MARKUP}
        for (eur, usd, mon, legado) in ESCENARIOS for elegida in ("EUR", "USD")
    ]
    front = _correr_en_node(casos)
    assert len(front) == len(casos)

    fallos = []
    for caso, f in zip(casos, front):
        b_tarifa, b_falta, b_precio = _backend(
            caso["eur"], caso["usd"], caso["moneda_legado"], caso["legado"], caso["elegida"])
        etiqueta = (f"EUR={caso['eur']} USD={caso['usd']} legado={caso['legado']}"
                    f"@{caso['moneda_legado']} → elige {caso['elegida']}")
        if f["falta"] != b_falta:
            fallos.append(f"{etiqueta}: la pantalla {'bloquea' if f['falta'] else 'cotiza'} "
                          f"y el backend {'bloquea' if b_falta else 'cotiza'}")
            continue
        if b_falta:
            continue          # los dos bloquean: no hay precio que comparar
        if abs((f["tarifa"] or 0) - (b_tarifa or 0)) > 1e-9:
            fallos.append(f"{etiqueta}: tarifa pantalla {f['tarifa']} ≠ backend {b_tarifa}")
        if f["precio_neto"] != b_precio:
            fallos.append(f"{etiqueta}: precio pantalla {f['precio_neto']} ≠ backend {b_precio}")
    assert not fallos, "La pantalla y el backend NO coinciden:\n  - " + "\n  - ".join(fallos)


def test_el_componente_preserva_la_moneda_del_legado_al_pisar_la_elegida():
    """Cinturón ESTÁTICO del punto ciego de este archivo.

    Los checks de arriba arman el config efectivo a mano (replicando lo que hace el
    componente), así que no se enterarían si el componente DEJARA de preservar
    `moneda_tarifa_legado`. Y ese olvido es exactamente el defecto original: sin ese
    campo, `tarifaEfectiva` compara la moneda contra sí misma y presta el legado.

    Por eso se verifica sobre el texto del .tsx que TODA construcción que pisa
    `moneda_tarifa` conserve además la moneda legada.
    """
    src = open(_MODAL, encoding="utf-8").read()
    # Se analiza por BLOQUE y no por línea: el objeto puede estar formateado en varias
    # líneas (lo está), así que buscar ambas cosas en el mismo renglón daría un verde
    # falso apenas alguien reformatee.
    bloques, i = [], src.find("...cfg")
    while i != -1:
        bloques.append(src[i:i + 500])
        i = src.find("...cfg", i + 1)
    derivados = [b for b in bloques if "moneda_tarifa:" in b]
    assert derivados, (
        "no se encontró ninguna construcción que derive de `cfg` pisando `moneda_tarifa`: "
        "¿se renombró el config efectivo? Este check quedaría ciego.")
    sin_legado = [b.splitlines()[0].strip() for b in derivados if "moneda_tarifa_legado" not in b]
    assert not sin_legado, (
        "hay construcciones que pisan `moneda_tarifa` SIN conservar `moneda_tarifa_legado`; "
        "ahí el respaldo del legado vuelve a ser una tautología y la tarifa de una moneda "
        "se le presta a la otra. Bloques sospechosos (primera línea):\n  - "
        + "\n  - ".join(sin_legado))


def test_sonda_el_legado_no_se_presta_a_la_otra_moneda_en_la_pantalla():
    """SONDA del defecto exacto que encontró el multienjambre.

    Estado del día del deploy (EUR cargada, USD en NULL, legado en EUR): elegir USD tiene
    que dejar la pantalla SIN tarifa —para que salte el aviso y se bloquee «Aplicar»—, y
    NO prestarle el 4,5 de EUR. Si alguien vuelve a comparar contra el `moneda_tarifa`
    pisado, este check cae: la pantalla devolvería 4,5 y un precio inventado.
    """
    if not shutil.which("node") or not os.path.isdir(os.path.join(_FRONT, "node_modules", "esbuild")):
        pytest.skip("node/esbuild no disponibles: la sonda del frontend NO se verificó")
    base = {"eur": 4.5, "usd": None, "legado": 4.5, "moneda_legado": "EUR",
            "tc_eur": TC_EUR, "tc_usd": TC_USD, "costo": COSTO,
            "moneda_item": MONEDA_ITEM, "peso": PESO, "markup": MARKUP}
    eur, usd = _correr_en_node([{**base, "elegida": "EUR"}, {**base, "elegida": "USD"}])
    assert eur["tarifa"] == 4.5 and not eur["falta"], eur
    assert usd["tarifa"] is None, (
        f"la pantalla le prestó la tarifa de EUR a USD (tarifa={usd['tarifa']}): "
        "el respaldo del legado volvió a compararse contra la moneda ya pisada")
    assert usd["falta"] is True, "sin tarifa, la pantalla debe avisar y bloquear «Aplicar»"
