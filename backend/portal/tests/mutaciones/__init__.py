"""MUTACIONES: la prueba de que cada suite del portal prueba lo que dice.

Una suite verde solo dice que el código de hoy pasa; no dice que la suite detectaría el
error que afirma proteger. Para saberlo se ROMPE el código a propósito (una mutación:
quitar una condición, invertir un signo, borrar un filtro) y se exige que la suite se
ponga roja. Una mutación que deja la suite verde es una comprobación que no discrimina:
hay que arreglar la prueba, no el código.

Cada lista (estados.py, items.py, pedidos.py, documentos.py, y las que se agreguen: todo
módulo de esta carpeta con un dict `MUTACIONES` entra solo) es la especificación de qué
regla protege cada suite: `(qué se rompe, archivo, texto original, texto mutado)`.

CÓMO CORRE (`python -m portal.tests.mutaciones`, desde backend/, con el venv)
  1. copia backend/ a una carpeta temporal (sin venv, uploads ni cachés) y, junto a él,
     frontend-src/ (src/ copiado, node_modules enlazado): el árbol de trabajo NO se
     toca nunca, y lo que corre mientras tanto no ve código roto;
  2. corre la suite sin mutar sobre la copia: tiene que estar verde;
  3. por cada mutación: la aplica en la copia, corre la suite (tiene que quedar ROJA) y
     restaura el archivo.
Usa la misma base local que las suites (cada una limpia sus datos por marca), así que no
conviene correr la misma suite en el árbol de trabajo al mismo tiempo.

Un archivo que empieza por `frontend-src/` (la ruta desde la raíz del repo, tal como
la escribe git) se muta en la copia del frontend; los espejos la leen porque está
junto a la copia de backend/ y porque PARTSCONTROL_FRONTEND_SRC apunta a ella.

Un cambio en el código que mueve el texto original de una mutación la deja en ERROR
(«el texto a mutar aparece 0 veces»): hay que actualizar la lista, que es parte del
trabajo de cambiar esa regla.
"""
import importlib
import os
import pkgutil
import shutil
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional, Tuple

Mutacion = Tuple[str, str, str, str]

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
FRONTEND = os.path.join(os.path.dirname(BACKEND), "frontend-src")
# Prefijo con que las listas nombran un archivo del frontend (ruta desde la raíz del repo).
PREFIJO_FRONTEND = "frontend-src/"
EXCLUIDOS = ("venv", "uploads", "__pycache__", ".pytest_cache", ".ruff_cache", ".git")
SEGUNDOS_MAXIMOS_POR_CORRIDA = 600


def todas() -> Dict[str, List[Mutacion]]:
    """{suite: sus mutaciones}, juntando todas las listas del paquete: cada módulo de
    esta carpeta que defina `MUTACIONES` (un dict suite → lista) entra solo."""
    juntas: Dict[str, List[Mutacion]] = {}
    for info in pkgutil.iter_modules([os.path.dirname(__file__)]):
        if info.name.startswith("_"):  # __main__ es la línea de comandos, no una lista
            continue
        modulo = importlib.import_module(f"{__name__}.{info.name}")
        for suite, lista in getattr(modulo, "MUTACIONES", {}).items():
            juntas.setdefault(suite, []).extend(lista)
    return juntas


def copiar_backend() -> str:
    destino = tempfile.mkdtemp(prefix="portal-mutaciones-")
    copia = os.path.join(destino, "backend")
    shutil.copytree(BACKEND, copia, ignore=shutil.ignore_patterns(*EXCLUIDOS))
    return copia


def copiar_frontend(junto_a: str) -> Optional[str]:
    """Copia de frontend-src al lado de la copia de backend/, con la forma que esperan los
    espejos (src/… y node_modules). src/ se copia; node_modules se enlaza, porque pesa
    demasiado y ninguna mutación lo toca. None si no hay frontend-src junto a backend/."""
    if not os.path.isdir(os.path.join(FRONTEND, "src")):
        return None
    copia = os.path.join(junto_a, "frontend-src")
    shutil.copytree(os.path.join(FRONTEND, "src"), os.path.join(copia, "src"))
    modulos = os.path.join(FRONTEND, "node_modules")
    if os.path.isdir(modulos):
        os.symlink(modulos, os.path.join(copia, "node_modules"))
    return copia


def ruta_en_copia(copia: str, copia_front: Optional[str], archivo: str) -> str:
    """Dónde vive `archivo` dentro de las copias: en la del frontend si empieza por
    PREFIJO_FRONTEND, si no en la de backend/."""
    if not archivo.startswith(PREFIJO_FRONTEND):
        return os.path.join(copia, archivo)
    if not copia_front:
        raise FileNotFoundError(f"no hay frontend-src junto a backend/: {archivo} no se puede mutar")
    return os.path.join(copia_front, archivo[len(PREFIJO_FRONTEND):])


def correr(copia: str, suite: str, copia_front: Optional[str] = None) -> Tuple[bool, str]:
    """(¿verde?, última línea de pytest) de la suite corrida sobre la copia."""
    entorno = dict(os.environ)
    if copia_front:
        # Los espejos del frontend leen la copia (mutada o no), nunca el árbol de trabajo.
        entorno["PARTSCONTROL_FRONTEND_SRC"] = copia_front
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", suite, "-x", "-q", "-p", "no:cacheprovider", "-W", "ignore"],
            cwd=copia, env=entorno, capture_output=True, text=True, timeout=SEGUNDOS_MAXIMOS_POR_CORRIDA)
    except subprocess.TimeoutExpired:
        return False, "no terminó a tiempo"
    ultima = (r.stdout.strip().splitlines() or [""])[-1]
    return r.returncode == 0, ultima


def mutar(copia: str, suite: str, mutaciones: List[Mutacion], copia_front: Optional[str] = None) -> int:
    """Corre todas las mutaciones de una suite. Devuelve cuántas NO se detectaron
    (o no se pudieron aplicar)."""
    verde, ultima = correr(copia, suite, copia_front)
    print(f"BASE  {suite}: {'verde' if verde else 'ROJA'}  ({ultima})", flush=True)
    if not verde:
        print("  la suite no está verde sin mutar: no se puede medir nada")
        return len(mutaciones)
    malas = 0
    for nombre, archivo, viejo, nuevo in mutaciones:
        try:
            ruta = ruta_en_copia(copia, copia_front, archivo)
            with open(ruta, encoding="utf-8", newline="") as f:
                original = f.read()
        except FileNotFoundError as e:
            print(f"ERROR | {nombre}: {e}", flush=True)
            malas += 1
            continue
        if "\r\n" in original:
            # Archivos del programador en CRLF (auth.py): las listas están escritas con
            # saltos de línea simples; se adaptan para no dejar el archivo mezclado.
            viejo, nuevo = viejo.replace("\n", "\r\n"), nuevo.replace("\n", "\r\n")
        if original.count(viejo) != 1:
            print(f"ERROR | {nombre}: el texto a mutar aparece {original.count(viejo)} veces en {archivo}", flush=True)
            malas += 1
            continue
        with open(ruta, "w", encoding="utf-8", newline="") as f:
            f.write(original.replace(viejo, nuevo))
        try:
            verde, ultima = correr(copia, suite, copia_front)
        finally:
            with open(ruta, "w", encoding="utf-8", newline="") as f:
                f.write(original)
        print(f"{'VERDE (NO DISCRIMINA)' if verde else 'roja'} | {nombre}  ({ultima[:70]})", flush=True)
        malas += 1 if verde else 0
    print(f"== {suite}: {len(mutaciones) - malas} de {len(mutaciones)} mutaciones detectadas", flush=True)
    return malas


def main(argv: List[str]) -> int:
    listas = todas()
    elegidas = [s for s in listas if not argv or any(a in s for a in argv)]
    if not elegidas:
        print("Suites:", *listas, sep="\n  ")
        return 2
    copia = copiar_backend()
    copia_front = copiar_frontend(os.path.dirname(copia))
    try:
        malas = sum(mutar(copia, suite, listas[suite], copia_front) for suite in elegidas)
    finally:
        shutil.rmtree(os.path.dirname(copia), ignore_errors=True)
    return 1 if malas else 0
