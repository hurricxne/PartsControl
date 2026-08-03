"""Migración: fecha de EMISIÓN de la guía de despacho (FchRef de la referencia 52).

QUÉ AGREGA
    despachos.fecha_guia        DATE NULL   — MachParts / Grupo AM (empresa 'mineria')
    monza_despachos.fecha_guia  DATE NULL   — MonzaParts (empresa 'automotriz')

POR QUÉ
    Cuando la guía de despacho se emite FUERA de PartsControl (portal del SII o talonario
    en papel) y en el sistema solo se registra su NÚMERO, la factura electrónica (DTE 33)
    tiene que citarla en su referencia tipo 52 con el folio Y la fecha de EMISIÓN de esa
    guía. Hasta ahora esa fecha no existía como dato: el sistema usaba `fecha_despacho`
    (el instante en que se CERRÓ el despacho, puesto por el reloj del servidor) como
    sustituto, y el DTE 33 salía al SII referenciando la guía con una fecha que no era la
    suya. Un DTE emitido no se corrige: había que dejar de adivinar la fecha.

    Esta columna es el dato real, tecleado por el operador. Es distinta de las otras tres
    fechas que ya tiene un despacho:
        fecha_creacion / fecha  → cuándo se armó el despacho en PartsControl
        fecha_despacho          → cuándo se cerró el despacho (salió la mercadería)
        fecha_firma             → cuándo el cliente firmó la guía recibida

    Con guía ELECTRÓNICA emitida por Wasabil la columna queda vacía y no se usa: ahí la
    fecha tributaria sale del `documentDate` del propio DTE 52.

SEPARACIÓN POR MARCA
    Son DOS tablas independientes de DOS módulos independientes (MachParts y MonzaParts no
    comparten datos). Van en un solo script sólo para que el deploy sea UN comando; el
    script las trata por separado y si una tabla no existe en esta base de datos, avisa y
    sigue con la otra.

IDEMPOTENTE
    Se puede correr las veces que sea: si la columna ya está, la deja como está.
    No toca ni un solo dato existente — los despachos ya creados quedan con fecha_guia NULL.

EFECTO ESPERADO DESPUÉS DE CORRER
    Los despachos con guía EN PAPEL que todavía no tengan fecha cargada no van a poder
    facturarse electrónicamente hasta que se les cargue (Despachos → Editar). Eso es
    deliberado: antes se emitían con la fecha equivocada.

Uso (desde backend/, con el venv activo):
    python -m migrations.despacho_fecha_guia
"""
from sqlalchemy import text

from database import engine

# tabla -> {columna: definición DDL}. Una entrada por MARCA; no se cruzan.
TABLAS = {
    "despachos": {"fecha_guia": "DATE NULL"},          # MachParts / Grupo AM
    "monza_despachos": {"fecha_guia": "DATE NULL"},    # MonzaParts
}


def _tabla_existe(conn, tabla: str) -> bool:
    return bool(conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = :t"
        ),
        {"t": tabla},
    ).scalar())


def _columnas_existentes(conn, tabla: str) -> set:
    rows = conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = :t"
        ),
        {"t": tabla},
    )
    return {r[0] for r in rows}


def _parchar(tabla: str, columnas: dict) -> None:
    """Agrega a UNA tabla las columnas que le falten. Conexión propia por tabla.

    Cada marca va en su propia transacción a propósito. MySQL hace COMMIT IMPLÍCITO en
    cada DDL, así que meter las dos tablas en un solo `engine.begin()` PARECE atómico y no
    lo es: si la segunda fallara, la primera ya quedó aplicada igual. Separarlas hace
    visible lo que de verdad pasa y deja que el fallo de una no oculte el éxito de la otra.
    """
    with engine.begin() as conn:
        if not _tabla_existe(conn, tabla):
            # No es un error: una base puede tener sólo una de las dos marcas.
            print(f"[migracion] tabla {tabla} no existe en esta base — se omite")
            return
        existentes = _columnas_existentes(conn, tabla)
        for col, ddl in columnas.items():
            if col in existentes:
                print(f"[migracion] {tabla}.{col} ya existe — ok")
                continue
            conn.execute(text(f"ALTER TABLE {tabla} ADD COLUMN {col} {ddl}"))
            print(f"[migracion] {tabla}.{col} agregada")


def run() -> None:
    fallidas = []
    for tabla, columnas in TABLAS.items():
        try:
            _parchar(tabla, columnas)
        except Exception as exc:  # noqa: BLE001
            # Se sigue con la otra marca y se resume al final: con un traceback pelado, el
            # operador no sabía cuál de las dos tablas quedó lista y cuál no.
            fallidas.append(tabla)
            print(f"[migracion] ERROR en {tabla}: {type(exc).__name__}: {exc}")
    if fallidas:
        raise SystemExit(
            f"[migracion] FALLÓ en: {', '.join(fallidas)}. Las demás tablas SÍ quedaron "
            "aplicadas. Corrige la causa y vuelve a correr este mismo script "
            "(es idempotente: no repite lo ya hecho). NO reinicies el backend hasta que "
            "esta migración salga limpia: falta la columna y las pantallas de Despachos "
            "de esa marca responderán 1054.")
    print("[migracion] completada")


if __name__ == "__main__":
    run()
