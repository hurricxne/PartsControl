"""Auditor de esquema del deploy: ¿la BD tiene TODO lo que los modelos declaran?

Una columna declarada en el modelo y ausente en la tabla NO impide arrancar: revienta
después, con `1054 Unknown column`, y en esta app eso significa **HTTP 500 en cascada**
(caso real de julio 2026: `migrations/add_despacho_guia_fields.py` venía en el paquete y
nunca se ejecutó → desaparecieron el detalle de Despachos, su botón "Crear Despacho" y el
detalle de Ventas; el síntoma no se parecía en nada a la causa). Este script es la red que
atrapa eso ANTES de reiniciar uvicorn.

Qué audita, tabla por tabla:
  1. **tabla ausente** (el `create_all` no corrió, o el módulo es nuevo)
  2. **columnas del modelo ausentes en la BD** → la falla clásica (1054 → 500 en cascada)
  3. **índices UNIQUE del modelo ausentes en la BD** → una migración saltada en silencio
     deja una invariante de plata sin respaldo de BD. Caso concreto: `tesoreria/init_db.py`
     NO crea `UNIQUE(egreso_id)` en `conc_conciliacion` si encuentra duplicados legados;
     avisa por pantalla y sale con éxito (rc=0). Si el operador no lee esa línea, el mismo
     pago podía quedar enlazado contra dos cargos del banco y nadie se enteraba.

⚠️ Por qué este archivo se reescribió el 2026-07-30: los **11 módulos satélite**
(contabilidad, tesorería, compras/CxP, costo landed, DTE al SII y recepción nacional, de
las dos marcas) declaran sus modelos en `<paquete>/models.py`, y `models.models` NO los
arrastra. El auditor registraba solo `models.models` + `monza_models`, así que
`Base.metadata` tenía **58 de 95 tablas**: era CIEGO a 37 y decía "sin problemas" con
columnas de esas tablas ausentes — exactamente la falla que este mismo script existe para
atrapar. Además la ruta del backend estaba cableada a `/var/www/machparts.bigcode.cl`, o
sea que en PROD (otra ruta) no corría, y una sola cláusula `except` tapaba la caída de
cualquier import: el silencio se leía como "todo bien".

Uso (desde el docroot del servidor, con su venv):
    venv/bin/python deploy/audit_schema.py                  # auditar la BD
    venv/bin/python deploy/audit_schema.py --autoprueba     # ¿el auditor VE de verdad?
    venv/bin/python deploy/audit_schema.py --pasos          # ¿el checklist y el .env.example
                                                            #  documentan todo lo que hay?
    venv/bin/python deploy/audit_schema.py --backend /ruta/a/backend

Código de salida: **0** sin problemas, **1** con desalineación (sirve para cortar un
script de deploy con `&&`). Los AVISOS no cambian el código de salida.
"""
import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

# --- Dónde vive el backend ---------------------------------------------------------
# `deploy/` vive al lado de `backend/`, así que la ruta se DEDUCE del lugar del script y
# funciona igual en PROD, en QA y en local. Se puede forzar con --backend o con la
# variable AUDIT_BACKEND_DIR (útil para auditar una copia de respaldo).
RUTA_HISTORICA_QA = Path("/var/www/machparts.bigcode.cl/backend")

# Los 2 núcleos + los 11 SATÉLITES. Cada uno se importa en su propio try/except y un fallo
# se CUENTA como problema (rc=1). La versión vieja hacía lo contrario: imprimía
# `(monza_models no cargado: …)` entre paréntesis y seguía hasta el "sin problemas" con
# rc=0, o sea que un rename de módulo apagaba media auditoría sin que nadie lo notara.
MODULOS = (
    ("models.models",                  "núcleo Grupo AM / MachParts"),
    ("monza_models",                   "núcleo MonzaParts"),
    ("tesoreria.models",               "Tesorería GA (conc_*)"),
    ("wasabil_dte.models",             "DTE al SII GA (wasabil_dte)"),
    ("compras_contab.models",          "Compras/CxP GA (cont_compra*, cont_egreso*, plan de cuentas)"),
    ("embarques_pricing.models",       "costo landed GA (emb_pricing_*)"),
    ("recepcion_nacional.models",      "compras nacionales GA (recepcion_nacional*)"),
    ("monza_contabilidad.models",      "Contabilidad Monza (monza_cont_factura/cobranza/adelanto/factoring)"),
    ("monza_tesoreria.models",         "Tesorería Monza (monza_tes_*)"),
    ("monza_compras_contab.models",    "Compras/CxP Monza (monza_cont_compra*, plan de cuentas)"),
    ("monza_embarques_pricing.models", "costo landed Monza (monza_emb_pricing_*)"),
    ("monza_recepcion_nacional.models", "compras nacionales Monza (router montado SIEMPRE)"),
    ("monza_wasabil_dte.models",       "DTE al SII Monza (monza_wasabil_dte)"),
    ("wasabil_compras.models",         "libro de compras del SII GA (sii_libro_*)"),
    ("monza_wasabil_compras.models",   "libro de compras del SII Monza (monza_sii_libro_*)"),
)

# Tablas que SOLO existen con `MONZA_CONTAB_ENABLED=true`: apagado el flag, `main.py` ni
# importa esos routers, `create_all` no crea sus 25 tablas y en PROD su ausencia es lo
# ESPERADO (decisión del cliente 2026-07-15). Se informan como AVISO, nunca como problema:
# si contaran como problema, el operador aprendería a ignorar la salida del auditor.
# OJO con el prefijo `monza_emb_`: NO matchea `monza_embarques` / `monza_embarque_items`
# (logística del núcleo Monza, obligatorias siempre), solo `monza_emb_pricing*`.
# `monza_sii_` (las 7 del Libro de compras del SII de Monza) entró el 2026-08-08, cuando
# ese router pasó a obedecer el mismo flag que su pantalla y su Tesorería: hasta entonces
# se montaba SIEMPRE, así que sus tablas nacían con el gate apagado —y de paso arrastraban
# a la metadata las 18 contables, por el cierre del grafo de FKs de su models.py.
PREFIJOS_SOLO_CON_GATE = ("monza_cont", "monza_tes", "monza_emb_", "monza_sii_")

NIVEL_PROBLEMA = "PROBLEMA"
NIVEL_AVISO = "AVISO"


def resolver_backend(explicita: Optional[str] = None) -> Path:
    """Devuelve la carpeta backend/ a auditar (y se muere si no la encuentra)."""
    for candidata in (explicita, os.environ.get("AUDIT_BACKEND_DIR")):
        if candidata:
            ruta = Path(candidata).expanduser().resolve()
            if not (ruta / "database.py").is_file():
                sys.exit(f"[FATAL] {ruta} no parece un backend/ (no encuentro database.py)")
            return ruta
    junto_al_script = Path(__file__).resolve().parent.parent / "backend"
    if (junto_al_script / "database.py").is_file():
        return junto_al_script
    if (RUTA_HISTORICA_QA / "database.py").is_file():
        return RUTA_HISTORICA_QA
    sys.exit(
        "[FATAL] no encuentro backend/. Correr desde el docroot (deploy/audit_schema.py) "
        "o pasar --backend /ruta/al/backend"
    )


def preparar_entorno(backend: Path) -> None:
    """sys.path + cwd apuntando al backend: `config.py` lee `.env` RELATIVO al cwd, así
    que sin el chdir el auditor se conectaría a la BD por defecto del código en vez de a
    la del servidor — y diría "sin problemas" sobre una base que no es la de nadie."""
    sys.path.insert(0, str(backend))
    os.chdir(backend)


MODULOS_NUCLEO = ("models.models", "monza_models")


def modulos_en_el_arbol(backend: Path) -> List[str]:
    """Todos los módulos de modelos que EXISTEN en backend/, sacados del árbol de archivos.

    ⚠️ Esta función es la que evita que MODULOS se quede viejo, y no es un adorno: la
    autoprueba compara contra ESTO y no contra MODULOS. Cuando la comparación salía de la
    misma constante que el arreglo, borrar una línea de MODULOS borraba también su
    comprobación y la autoprueba seguía VERDE con el auditor ya ciego a ese módulo (lo
    medí). Un módulo nuevo con su `models.py` entra solo y se hace exigible.
    """
    encontrados = []
    for archivo in sorted(backend.glob("*/models.py")):
        if (archivo.parent / "__init__.py").is_file():
            encontrados.append(f"{archivo.parent.name}.models")
    if (backend / "monza_models.py").is_file():
        encontrados.append("monza_models")
    return encontrados


def modulos_sin_registrar(backend: Path) -> List[str]:
    declarados = {m for m, _ in MODULOS}
    return [m for m in modulos_en_el_arbol(backend) if m not in declarados]


def _duenios_de_tabla(Base) -> dict:
    """tabla → módulo que la declara, sacado de los MAPPERS y no del nombre (`compras_contab`
    declara `cont_*`, `tesoreria` declara `conc_*`: por nombre no se adivina). Con esto la
    autoprueba puede plantar un defecto en CADA módulo satélite, y se pone roja si se cae
    UNO SOLO del registro — no solo si se caen todos."""
    duenio = {}
    for mapper in Base.registry.mappers:
        tabla = getattr(mapper, "local_table", None)
        if tabla is not None:
            duenio.setdefault(tabla.name, mapper.class_.__module__)
    return duenio


def registrar_modelos() -> Tuple[object, List[str], List[str]]:
    """Importa los 13 módulos de modelos y devuelve (Base, fallos, tablas_de_satelite).

    `tablas_de_satelite` son las tablas que NO declara el núcleo — o sea las que el auditor
    viejo no veía. Se calculan por DUEÑO (el módulo del mapper), no por el orden en que se
    importaron: así el resultado es el mismo se llame una vez o diez, y no depende de que
    MODULOS liste el núcleo primero.

    Un fallo de import es un PROBLEMA de primera clase: significa que el auditor quedó
    ciego a ese pedazo del esquema, que es justo el estado que este archivo vino a matar.
    """
    from database import Base

    fallos: List[str] = []
    for modulo, para_que in MODULOS:
        try:
            __import__(modulo)
        except Exception as exc:  # noqa: BLE001 — se reporta, no se traga
            fallos.append(f"[MÓDULO NO CARGADO] {modulo} ({para_que}): {exc}")

    duenio = _duenios_de_tabla(Base)
    satelites = sorted(t for t, m in duenio.items() if m not in MODULOS_NUCLEO)
    return Base, fallos, satelites


def _solo_con_gate(tabla: str) -> bool:
    return tabla.startswith(PREFIJOS_SOLO_CON_GATE)


def _uniques_del_modelo(tabla) -> List[Tuple[str, frozenset]]:
    """UNIQUE declarados en el modelo, como (nombre, {columnas}). Cubre las dos formas:
    `UniqueConstraint(...)` en __table_args__ y `Index(..., unique=True)`."""
    from sqlalchemy import UniqueConstraint

    salida = []
    for con in tabla.constraints:
        if isinstance(con, UniqueConstraint) and len(con.columns):
            salida.append((con.name or "(sin nombre)", frozenset(c.name for c in con.columns)))
    for ix in tabla.indexes:
        if ix.unique:
            salida.append((ix.name or "(sin nombre)", frozenset(c.name for c in ix.columns)))
    return salida


def _uniques_reales(insp, tabla: str) -> List[frozenset]:
    """UNIQUE que la BD tiene de verdad. Se comparan por CONJUNTO DE COLUMNAS, no por
    nombre: MySQL y MariaDB bautizan distinto lo que se creó sin nombre explícito, y lo
    que protege el dato es la columna, no la etiqueta."""
    reales = []
    for ix in insp.get_indexes(tabla):
        if ix.get("unique") and ix.get("column_names"):
            reales.append(frozenset(c for c in ix["column_names"] if c))
    for uq in insp.get_unique_constraints(tabla):
        if uq.get("column_names"):
            reales.append(frozenset(uq["column_names"]))
    return reales


def auditar(Base, insp, gate_monza: bool) -> List[Tuple[str, str, str]]:
    """Compara modelos vs BD. Devuelve [(nivel, tabla, detalle)] — sin imprimir, para que
    la autoprueba pueda revisar los hallazgos en vez de leerse la pantalla."""
    reales = set(insp.get_table_names())
    hallazgos: List[Tuple[str, str, str]] = []

    for nombre, tabla in sorted(Base.metadata.tables.items()):
        # Apagado el gate de Monza, sus 25 tablas contables NO deben existir: su ausencia
        # es la configuración correcta de PROD, no una desalineación.
        nivel = NIVEL_AVISO if (_solo_con_gate(nombre) and not gate_monza) else NIVEL_PROBLEMA

        if nombre not in reales:
            hallazgos.append((nivel, nombre, "TABLA FALTANTE"))
            continue

        en_la_bd = {c["name"] for c in insp.get_columns(nombre)}
        for col in sorted({c.name for c in tabla.columns} - en_la_bd):
            c = tabla.columns[col]
            hallazgos.append((nivel, nombre,
                              f"COLUMNA FALTANTE {col} ({c.type}) nullable={c.nullable}"))

        uniques_modelo = _uniques_del_modelo(tabla)
        if uniques_modelo:  # solo entonces vale reflejar índices (son 2 consultas más)
            cols_reales = _uniques_reales(insp, nombre)
            for uq_nombre, uq_cols in uniques_modelo:
                if uq_cols not in cols_reales:
                    hallazgos.append((nivel, nombre,
                                      f"UNIQUE FALTANTE {uq_nombre} ({', '.join(sorted(uq_cols))})"
                                      " — migración saltada: la invariante no tiene respaldo de BD"))
    return hallazgos


def imprimir(hallazgos: List[Tuple[str, str, str]], fallos_import: List[str],
             gate_monza: bool, tablas_satelite: List[str], total_tablas: int) -> int:
    """Imprime el informe y devuelve el código de salida."""
    for f in fallos_import:
        print(f)

    problemas = [h for h in hallazgos if h[0] == NIVEL_PROBLEMA]
    avisos = [h for h in hallazgos if h[0] == NIVEL_AVISO]

    for _, tabla, detalle in problemas:
        print(f"[{tabla}] {detalle}")

    if avisos:
        ausentes = sorted({t for niv, t, d in avisos if d == "TABLA FALTANTE"})
        otros = [(t, d) for niv, t, d in avisos if d != "TABLA FALTANTE"]
        if ausentes:
            print(f"\n(aviso) {len(ausentes)} tabla(s) monza-contab ausentes — ESPERADO con "
                  f"MONZA_CONTAB_ENABLED=false: {', '.join(ausentes)}")
        for tabla, detalle in otros:
            print(f"(aviso, gate apagado) [{tabla}] {detalle}")

    total = len(problemas) + len(fallos_import)
    # El conteo se imprime SIEMPRE: es lo que delata un auditor a medio ciego. Con solo el
    # núcleo registrado son 58 tablas y "0 de los satélites" — y así estuvo meses.
    print(f"\nflag MONZA_CONTAB_ENABLED={'true' if gate_monza else 'false'} · "
          f"{total_tablas} tabla(s) auditada(s), de las cuales {len(tablas_satelite)} "
          f"vienen de los {len(MODULOS) - len(MODULOS_NUCLEO)} módulos satélite")
    print("=== sin problemas ===" if total == 0
          else f"=== {total} problema(s): CORRER LAS MIGRACIONES ANTES DE REINICIAR ===")
    return 0 if total == 0 else 1


# --- Autoprueba: ¿quién audita al auditor? -----------------------------------------
# Este archivo ya estuvo ciego una vez sin que nadie lo notara, porque "sin problemas" se
# ve idéntico cuando no hay nada malo y cuando no se está mirando. La autoprueba planta
# defectos FALSOS en la metadata (en memoria: no toca la BD, no emite un solo DDL) y exige
# que el auditor los reporte. Los dos primeros defectos se plantan sobre una tabla de un
# módulo SATÉLITE: si el registro de satélites se rompe, esa tabla desaparece de la
# metadata, el defecto se vuelve invisible y la autoprueba se pone ROJA. Es la sonda del
# arreglo, no un adorno.
COL_FANTASMA = "zz_columna_fantasma_autoprueba"
UQ_FANTASMA = "uq_zz_fantasma_autoprueba"
TABLA_FANTASMA = "zz_tabla_fantasma_autoprueba"


def autoprueba(backend: Path) -> int:
    from sqlalchemy import Column, Integer, Table, UniqueConstraint, inspect
    from config import settings
    from database import engine

    Base, fallos, tablas_satelite = registrar_modelos()
    if fallos:
        for f in fallos:
            print(f)
        print("\n=== AUTOPRUEBA ROJA: hay módulos de modelos que no cargan ===")
        return 1

    gate_monza = bool(settings.MONZA_CONTAB_ENABLED)
    insp = inspect(engine)
    reales = set(insp.get_table_names())
    duenio = _duenios_de_tabla(Base)

    # Un defecto plantado POR MÓDULO SATÉLITE, recorriendo lo que hay en el ÁRBOL (no
    # MODULOS): si a MODULOS le falta una línea, acá se nota.
    #
    # El nivel esperado depende del flag, y esto es lo que hace que la autoprueba sirva
    # igual en PROD: con `MONZA_CONTAB_ENABLED=false` las 25 tablas monza-contab NO deben
    # existir, así que su desalineación es un AVISO a propósito. Exigir PROBLEMA ahí
    # pondría la autoprueba roja en PROD por la configuración correcta.
    esperados: List[Tuple[str, str, str, str]] = []  # (descripción, tabla, marca, nivel)
    sin_tabla: List[str] = []
    for modulo in modulos_en_el_arbol(backend):
        if modulo in MODULOS_NUCLEO:
            continue
        propias = [t for t in tablas_satelite if duenio.get(t) == modulo]
        if not propias:
            sin_tabla.append(modulo)
            continue
        # Se prefiere una tabla que exista en la BD (ahí el defecto plantado es una
        # COLUMNA faltante). Si ninguna existe —PROD con el flag apagado— igual se exige
        # que el auditor VEA la tabla y la cante como TABLA FALTANTE.
        presentes = [t for t in propias if t in reales]
        victima = presentes[0] if presentes else propias[0]
        nivel = NIVEL_AVISO if (_solo_con_gate(victima) and not gate_monza) else NIVEL_PROBLEMA
        if presentes:
            Base.metadata.tables[victima].append_column(Column(COL_FANTASMA, Integer))
            esperados.append((f"columna inventada en {victima} ← {modulo}",
                              victima, COL_FANTASMA, nivel))
        else:
            esperados.append((f"tabla ausente por el flag, vista en {victima} ← {modulo}",
                              victima, "TABLA FALTANTE", nivel))

    if sin_tabla or not esperados:
        for m in sin_tabla:
            print(f"FALLA · {m} existe en backend/ y NO aportó ni una tabla a la metadata "
                  "(falta en el registro MODULOS): el auditor es CIEGO a sus tablas")
        if not esperados and not sin_tabla:
            print("FALLA · no hay ni un módulo satélite que probar")
        print(f"\n=== AUTOPRUEBA ROJA: {len(sin_tabla)} módulo(s) de modelos fuera del "
              "registro — el auditor está CIEGO a esa parte del esquema (era el estado "
              "anterior al 2026-07-30) ===")
        return 1

    # Un UNIQUE inventado y una tabla inventada, sobre un satélite que SÍ exista en la BD
    # y que cuente como problema (si no, estas dos comprobaciones no probarían nada).
    grave = [e for e in esperados if e[3] == NIVEL_PROBLEMA and e[2] == COL_FANTASMA]
    if grave:
        tabla_uq = Base.metadata.tables[grave[0][1]]
        col_base = [c.name for c in tabla_uq.columns
                    if not c.primary_key and c.name != COL_FANTASMA][0]
        tabla_uq.append_constraint(UniqueConstraint(col_base, name=UQ_FANTASMA))
        esperados.append((f"UNIQUE inventado en {tabla_uq.name}", tabla_uq.name,
                          UQ_FANTASMA, NIVEL_PROBLEMA))
    Table(TABLA_FANTASMA, Base.metadata, Column("id", Integer, primary_key=True))
    esperados.append(("tabla inventada", TABLA_FANTASMA, "TABLA FALTANTE", NIVEL_PROBLEMA))

    hallazgos = auditar(Base, insp, gate_monza=gate_monza)
    graves = [(t, d) for niv, t, d in hallazgos if niv == NIVEL_PROBLEMA]
    por_nivel = {NIVEL_PROBLEMA: graves,
                 NIVEL_AVISO: [(t, d) for niv, t, d in hallazgos if niv == NIVEL_AVISO]}

    malos = 0
    for descripcion, tabla_esperada, marca, nivel in esperados:
        visto = any(t == tabla_esperada and marca in d for t, d in por_nivel[nivel])
        sufijo = "" if nivel == NIVEL_PROBLEMA else "  (como aviso: flag apagado)"
        print(f"{'OK  ' if visto else 'FALLA'} · {descripcion}{sufijo}")
        malos += 0 if visto else 1

    print(f"\ntablas que aportan los satélites: {len(tablas_satelite)} · en metadata: "
          f"{len(Base.metadata.tables) - 1} · en la BD: {len(reales)}")
    if malos:
        print(f"=== AUTOPRUEBA ROJA: {malos} defecto(s) plantado(s) que el auditor NO vio ===")
        print("(lo que sí vio: " + ("; ".join(f"{t}|{d}" for t, d in graves[:8]) or "nada") + ")")
        return 1
    print("=== AUTOPRUEBA VERDE: el auditor ve columna, UNIQUE y tabla faltantes, y ve los "
          f"{len(MODULOS) - len(MODULOS_NUCLEO)} módulos satélite ===")
    return 0


# --- ¿El checklist de deploy documenta todos los scripts que hay? -------------------
# A2-BD / M4-BD / M6-BD de la auditoría: el runbook listaba 1 migración de 18 y el
# checklist no mencionaba `import_plan_cuentas` en ninguna marca. La lista escrita a mano
# se queda vieja con cada módulo nuevo; esta comprobación la ata al árbol de archivos.
CHECKLIST_REL = Path("docs/CHECKLIST-DEPLOY-2026-07-20.md")

# Scripts de carga de datos de UNA SOLA VEZ (histórico), no pasos del deploy.
NO_SON_PASOS = {
    "migrate_business_data.py": "carga inicial de datos del Excel del dueño",
    "migrate_carat_catalog.py": "importación del catálogo CARAT",
    "migrate_monza.py": "creación inicial del esquema MonzaParts",
}


def pasos_esperados(backend: Path) -> List[str]:
    """Todo lo que un deploy tendría que correr, deducido del árbol de archivos."""
    pasos = []
    for init in sorted(backend.glob("*/init_db.py")):
        pasos.append(f"{init.parent.name}.init_db")
    for plan in sorted(backend.glob("*/import_plan_cuentas.py")):
        pasos.append(f"{plan.parent.name}.import_plan_cuentas")
    for mig in sorted((backend / "migrations").glob("*.py")):
        if mig.stem != "__init__":
            pasos.append(f"migrations.{mig.stem}")
    for suelto in sorted(backend.glob("migrate_*.py")):
        if suelto.name not in NO_SON_PASOS:
            pasos.append(suelto.name)
    return pasos


def variables_sin_documentar(backend: Path) -> List[str]:
    """Variables que `config.py` acepta y que `.env.example` no menciona.

    Un entorno nuevo se arma copiando `.env.example`: lo que no esté ahí queda apagado sin
    dejar pista. Es lo que pasó con el token de Wasabil de MonzaParts — `config.py` lo
    declaraba y la plantilla no, así que MonzaParts no podía emitir al SII y nadie sabía
    por qué. Los nombres salen de los campos REALES del Settings de pydantic, no de leer
    config.py con expresiones regulares."""
    plantilla = backend / ".env.example"
    if not plantilla.is_file():
        return ["(no existe backend/.env.example)"]
    try:
        from config import Settings
    except Exception as exc:  # noqa: BLE001
        print(f"(aviso) no pude importar config.Settings ({exc}): variables sin revisar")
        return []
    texto = plantilla.read_text(encoding="utf-8")
    # ⚠️ Se lee la PLANTILLA, nunca `.env`: acá no se toca ningún valor real.
    return [n for n in Settings.model_fields
            if not re.search(r"(?<![\w])" + re.escape(n) + r"(?![\w])", texto)]


def revisar_pasos(backend: Path) -> int:
    problemas = 0
    checklist = backend.parent / CHECKLIST_REL
    if not checklist.is_file():
        print(f"(aviso) no encuentro {CHECKLIST_REL} — en el servidor solo se copian "
              "backend/, frontend-src/ y assets/. Nada que revisar de los scripts.")
    else:
        texto = checklist.read_text(encoding="utf-8")
        # `in` a secas NO sirve: "compras_contab.init_db" vive dentro de
        # "monza_compras_contab.init_db", así que borrar la línea de Grupo AM pasaba
        # inadvertida (lo medí). Se exige que el paso no venga pegado a otro nombre.
        faltan = [p for p in pasos_esperados(backend)
                  if not re.search(r"(?<![\w.])" + re.escape(p) + r"(?![\w])", texto)]
        for p in faltan:
            print(f"[PASO NO DOCUMENTADO] {p} existe en backend/ y NO aparece en {CHECKLIST_REL}")
        print(f"pasos en el árbol: {len(pasos_esperados(backend))} · "
              f"no documentados: {len(faltan)} · "
              f"excluidos a propósito: {', '.join(sorted(NO_SON_PASOS))}")
        problemas += len(faltan)

    sin_doc = variables_sin_documentar(backend)
    for v in sin_doc:
        print(f"[VARIABLE NO DOCUMENTADA] {v} la acepta config.py y no está en "
              "backend/.env.example")
    print(f"variables de config.py sin documentar: {len(sin_doc)}")
    problemas += len(sin_doc)

    if problemas:
        print(f"\n=== {problemas} hueco(s) en la documentación del deploy ===")
        return 1
    print("\n=== los scripts del árbol y las variables de config.py están documentados ===")
    return 0


def main(argv: List[str]) -> int:
    modo_autoprueba = "--autoprueba" in argv
    modo_pasos = "--pasos" in argv
    explicita = None
    if "--backend" in argv:
        i = argv.index("--backend")
        explicita = argv[i + 1] if i + 1 < len(argv) else None
    else:
        sueltos = [a for a in argv if not a.startswith("-")]
        explicita = sueltos[0] if sueltos else None

    backend = resolver_backend(explicita)
    preparar_entorno(backend)
    print(f"backend auditado: {backend}\n")

    if modo_pasos:
        return revisar_pasos(backend)
    if modo_autoprueba:
        return autoprueba(backend)

    from sqlalchemy import inspect
    from config import settings
    from database import engine

    gate_monza = bool(settings.MONZA_CONTAB_ENABLED)
    Base, fallos, tablas_satelite = registrar_modelos()
    # Un módulo de modelos que existe en el árbol y no está en MODULOS deja al auditor
    # ciego a sus tablas SIN decir nada: es un problema, no una curiosidad.
    for modulo in modulos_sin_registrar(backend):
        fallos.append(f"[MÓDULO NO REGISTRADO] {modulo} existe en backend/ y no está en "
                      "MODULOS de este script: sus tablas NO se auditan")
    hallazgos = auditar(Base, inspect(engine), gate_monza)
    return imprimir(hallazgos, fallos, gate_monza, tablas_satelite,
                    len(Base.metadata.tables))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
