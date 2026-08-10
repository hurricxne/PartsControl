"""Candado del GATE de MonzaParts y de las puertas del job del libro SII.

Automatiza el «paso 4» de `deploy/README.md` (la *Verificación crítica ANTES de
reiniciar*), que hasta ahora vivía SOLO como un bloque para copiar y pegar a mano en el
servidor. Cuando el Libro de compras del SII de Monza empezó a montarse fuera del gate,
ese paso pasó a fallar en TODA promoción a producción y nadie se enteró hasta la
auditoría: un chequeo que solo existe en un documento no protege nada entre deploy y
deploy.

Lo que se protege acá:

 1. Con `MONZA_CONTAB_ENABLED=false`, importar `main` no deja NI UNA ruta contable de
    MonzaParts montada. Es lo que el flag gobierna de verdad.
 2. …y tampoco deja NI UNA de sus 25 tablas en `Base.metadata`, así que el `create_all`
    del arranque no las crea. Es el invariante que `deploy/README.md` declara y que
    `deploy/audit_schema.py` da por sentado en `PREFIJOS_SOLO_CON_GATE`.
 3. Con el flag encendido, las rutas SÍ están: si no, los dos checks de arriba pasarían
    por la razón equivocada (un rename de prefijo los dejaría verdes para siempre).
 4. El matcher banco↔libro NO corre desatendido de noche mientras el dueño no lo
    habilite, y el barrido del libro SÍ corre igual. Es la puerta de estreno.

No toca la base de datos ni la red: los subprocesos corren con `AUTO_CREATE_TABLES=false`
(sin eso, `import main` CREA tablas) y el job corre con el barrido y el hook del matcher
reemplazados por grabadoras.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent

# Prefijos de las tablas y de las rutas que el flag gobierna. Copiados a propósito del
# paso 4 de deploy/README.md y de PREFIJOS_SOLO_CON_GATE de deploy/audit_schema.py: si
# alguien agrega un módulo Monza-contab y no toca los tres lugares, esto se pone rojo.
PREF_TABLA = ("monza_cont", "monza_tes", "monza_emb_", "monza_sii_")
PREF_RUTA = (
    "/api/monza/contabilidad",
    "/api/monza/tesoreria",
    "/api/monza/compras-contab",
    "/api/monza/embarques-pricing",
    "/api/monza/sii-libro",
    "/api/monza/wasabil",
)

_MIRA_EL_GATE = f"""
import main
from models.models import Base
PREF_TABLA = {PREF_TABLA!r}
PREF_RUTA = {PREF_RUTA!r}
rutas = [r.path for r in main.app.routes
         if getattr(r, 'path', '').startswith(PREF_RUTA)]
tablas = sorted(t for t in Base.metadata.tables if t.startswith(PREF_TABLA))
print('RUTAS', len(rutas))
print('TABLAS', len(tablas))
print('DETALLE', tablas[:30])
"""


def _correr(codigo: str, **env_extra) -> str:
    """Corre `codigo` en un intérprete limpio, desde backend/, sin tocar el esquema."""
    env = dict(os.environ, AUTO_CREATE_TABLES="false", **env_extra)
    res = subprocess.run([sys.executable, "-c", codigo], capture_output=True, text=True,
                         cwd=BACKEND, env=env, timeout=180)
    assert res.returncode == 0, (
        f"el intérprete murió (rc={res.returncode}):\n{res.stderr[-2000:]}")
    return res.stdout


def test_gate_apagado_no_monta_rutas_ni_registra_tablas_monza_contab():
    """El paso 4 del deploy, automatizado: con el flag en false, ni rutas ni metadata."""
    salida = _correr(_MIRA_EL_GATE, MONZA_CONTAB_ENABLED="false")
    rutas = int(salida.split("RUTAS")[1].split()[0])
    tablas = int(salida.split("TABLAS")[1].split()[0])
    detalle = salida.split("DETALLE")[1].strip()

    assert rutas == 0, (
        f"con MONZA_CONTAB_ENABLED=false quedaron {rutas} rutas contables de MonzaParts "
        "montadas. El flag gobierna las RUTAS: tienen que dar 404.")
    assert tablas == 0, (
        f"con MONZA_CONTAB_ENABLED=false quedaron {tablas} tablas monza-contab en "
        f"Base.metadata, así que el create_all del arranque las crea: {detalle}. "
        "Casi siempre la causa es un import de un paquete Monza-contab colado FUERA "
        "del `if settings.MONZA_CONTAB_ENABLED` de main.py — ojo con los models.py que "
        "cierran su grafo de FKs importando media marca.")


def test_gate_encendido_si_monta_las_rutas():
    """Sonda de poder discriminante: encendido, las rutas están. Si no, el test de
    arriba estaría verde por la razón equivocada (por ejemplo, un prefijo renombrado)."""
    salida = _correr(_MIRA_EL_GATE, MONZA_CONTAB_ENABLED="true")
    rutas = int(salida.split("RUTAS")[1].split()[0])
    tablas = int(salida.split("TABLAS")[1].split()[0])

    assert rutas > 0, ("con MONZA_CONTAB_ENABLED=true no se montó ninguna ruta contable "
                       "de MonzaParts: los prefijos de este candado quedaron viejos")
    assert tablas >= 25, (f"con el gate encendido se esperaban 25+ tablas monza-contab "
                          f"en la metadata y hay {tablas}")


# El job del libro SII, con el barrido y el hook del matcher reemplazados por
# grabadoras: ni red ni base de datos. Imprime qué se ejecutó, en orden.
_MIRA_LAS_PUERTAS = """
import scheduler
import wasabil_compras.client as ga_cli, wasabil_compras.sync as ga_sync
import monza_wasabil_compras.client as mz_cli, monza_wasabil_compras.sync as mz_sync
hechos = []
ga_cli.esta_configurado = lambda: True
mz_cli.esta_configurado = lambda: True
ga_sync.barrido_nocturno = lambda: hechos.append('barrido_ga')
mz_sync.barrido_nocturno = lambda: hechos.append('barrido_monza')
scheduler._matcher_post_barrido_ga = lambda desde: hechos.append('matcher_ga')
scheduler._matcher_post_barrido_monza = lambda desde: hechos.append('matcher_monza')
scheduler.run_sii_libro_job()
print('HECHOS', ','.join(hechos))
"""


def _hechos(**env_extra) -> list[str]:
    salida = _correr(_MIRA_LAS_PUERTAS, **env_extra)
    crudo = salida.split("HECHOS")[1].strip()
    return [x for x in crudo.split(",") if x]


def test_el_matcher_nocturno_nace_apagado_y_el_barrido_igual_corre():
    """Puerta de estreno: sin la variable, el motor no escribe solo — pero el espejo del
    libro se llena igual, que es lo que hay que mirar el día del estreno."""
    hechos = _hechos(MONZA_CONTAB_ENABLED="true")
    assert "barrido_ga" in hechos and "barrido_monza" in hechos, (
        f"el barrido del libro tiene que correr igual con el matcher apagado: {hechos}")
    assert "matcher_ga" not in hechos and "matcher_monza" not in hechos, (
        "el matcher corrió DESATENDIDO sin que nadie lo habilite: la primera noche "
        f"recorrería la cartola histórica completa y marcaría plata conciliada. {hechos}")


def test_el_matcher_nocturno_corre_cuando_el_dueno_lo_habilita():
    """La otra mitad de la sonda: prendido, sí corre (si no, la puerta sería un tapón)."""
    hechos = _hechos(MONZA_CONTAB_ENABLED="true", SII_MATCHER_NOCTURNO="true",
                     SII_MATCHER_NOCTURNO_MONZA="true")
    assert hechos == ["barrido_ga", "matcher_ga", "barrido_monza", "matcher_monza"], hechos


def test_con_el_gate_apagado_monzaparts_ni_barre():
    """Las TRES piezas de Monza apagadas juntas: sin router, sin pantalla y sin barrido.
    Antes el job barría igual y construía de noche un espejo que nadie podía mirar."""
    hechos = _hechos(MONZA_CONTAB_ENABLED="false")
    assert "barrido_monza" not in hechos and "matcher_monza" not in hechos, (
        f"con la contabilidad de MonzaParts apagada, su libro SII no debe correr: {hechos}")
    assert "barrido_ga" in hechos, (
        f"…y el de Grupo AM tiene que seguir corriendo: {hechos}")
