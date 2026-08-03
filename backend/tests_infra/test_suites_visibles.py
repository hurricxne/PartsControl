"""Candado contra la SUITE INVISIBLE.

En este repo el molde de las suites es ``def run()`` + un wrapper de una línea
``def test_algo(): run()``. Sin ese wrapper pytest NO descubre nada y el archivo
queda verde por no existir: las 40 comprobaciones de adentro nunca corren.

Ya pasó DOS veces (``test_concurrencia_plata.py`` con 9 checks y otra antes).
Una nota en la memoria que dice "acordarse de revisar" es la defensa más débil
posible, porque depende de que alguien se acuerde. Esto lo vuelve imposible: si
alguien agrega una suite sin wrapper, el gate se pone ROJO acá y dice cuál es.

No toca base de datos ni red: es lectura estática del árbol.
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
EXCLUIDOS = {"venv", ".venv", "__pycache__", "node_modules", ".git"}


def _archivos_de_test() -> list[Path]:
    """Todo test_*.py del backend, saltando el venv y la basura de build."""
    return sorted(
        p for p in BACKEND.rglob("test_*.py")
        if not EXCLUIDOS.intersection(p.parts)
    )


def _funciones_top_level(arbol: ast.Module) -> list[ast.FunctionDef]:
    return [n for n in arbol.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _llama_a_run(fn: ast.FunctionDef) -> bool:
    """¿El cuerpo de este wrapper realmente invoca run()?"""
    return any(
        isinstance(nodo, ast.Call)
        and isinstance(nodo.func, ast.Name)
        and nodo.func.id == "run"
        for nodo in ast.walk(fn)
    )


def test_toda_suite_con_run_tiene_wrapper_visible():
    """Si un archivo define run() al nivel de módulo, DEBE tener un def test_ que lo llame."""
    archivos = _archivos_de_test()

    # Guarda contra el propio guard: si el barrido no encuentra nada, el test
    # pasaría en vacío y no estaría protegiendo nada.
    assert len(archivos) >= 20, (
        f"el barrido encontró solo {len(archivos)} archivos de test; se esperaban 20+. "
        "¿Cambió la ruta o el layout? Este candado quedó ciego."
    )

    invisibles: list[str] = []
    wrapper_hueco: list[str] = []

    for ruta in archivos:
        try:
            arbol = ast.parse(ruta.read_text(encoding="utf-8"))
        except SyntaxError as exc:  # un archivo roto también deja checks sin correr
            invisibles.append(f"{ruta.relative_to(BACKEND)} (no compila: {exc})")
            continue

        funciones = _funciones_top_level(arbol)
        tiene_run = any(f.name == "run" for f in funciones)
        if not tiene_run:
            continue  # usa pytest directo; no aplica el molde run()

        wrappers = [f for f in funciones if f.name.startswith("test_")]
        rel = str(ruta.relative_to(BACKEND))
        if not wrappers:
            invisibles.append(rel)
        elif not any(_llama_a_run(w) for w in wrappers):
            wrapper_hueco.append(rel)

    problemas = []
    if invisibles:
        problemas.append(
            "SUITES INVISIBLES (definen run() y NINGÚN def test_ — pytest no corre "
            f"ni una comprobación de adentro): {invisibles}. Arreglo: agregar al final "
            "`def test_<nombre>():` + `run()`."
        )
    if wrapper_hueco:
        problemas.append(
            "WRAPPER HUECO (hay def test_ pero ninguno llama a run(), así que el "
            f"cuerpo de la suite no se ejecuta): {wrapper_hueco}."
        )

    assert not problemas, " | ".join(problemas)


def test_no_hay_dos_suites_con_el_mismo_nombre_de_archivo():
    """Dos test_x.py con igual nombre base en paquetes distintos rompen la colecta.

    Con __init__.py en cada carpeta de tests esto hoy funciona, pero si a alguien
    se le olvida el __init__.py el segundo archivo tapa al primero y sus checks
    desaparecen sin aviso.
    """
    archivos = _archivos_de_test()
    por_nombre: dict[str, list[str]] = {}
    for ruta in archivos:
        por_nombre.setdefault(ruta.name, []).append(str(ruta.relative_to(BACKEND)))

    choques = {n: v for n, v in por_nombre.items() if len(v) > 1}
    for nombre, rutas in list(choques.items()):
        # Sin choque real si cada carpeta es un paquete de verdad.
        if all((BACKEND / r).parent.joinpath("__init__.py").exists() for r in rutas):
            del choques[nombre]

    assert not choques, (
        "archivos de test con el mismo nombre en carpetas que NO son paquetes "
        f"(falta __init__.py y pytest tapa uno con el otro): {choques}"
    )
