"""Migración: N° de bulto (caja) del despacho — MachParts / Grupo AM.

QUÉ AGREGA
    despachos.bulto_numero      VARCHAR(50) NULL   — MachParts / Grupo AM (empresa 'mineria')

POR QUÉ
    El picking/packing real: el operador arma los despachos MIENTRAS empaca, y cada
    despacho (= una guía) viaja dentro de un BULTO (caja) que él numera a mano dentro de
    la caja más grande del envío. Ese número no existía como dato: el reparto de bultos
    se reconstruía de memoria al escribir el mail al transportista (Samex). Con la
    columna, la pantalla de Despachos puede mostrar por OC cómo van repartidos los
    bultos (bulto → guía → partes) y armar ese mail con un clic.

    Texto LIBRE a propósito («1», «B2», «Cajas 2-3»): un despacho grande puede ocupar
    dos cajas físicas y un entero no lo expresa. El número no participa en ningún
    cálculo ni cruce contable — es rotulado logístico. NULL/"" = sin bulto asignado.

SEPARACIÓN POR MARCA
    Solo la tabla de MachParts: la feature es de esta marca (decisión del dueño,
    2026-08-25). Cuando se construya el espejo MonzaParts, se agrega la entrada
    "monza_despachos" a TABLAS y se vuelve a correr este mismo script (es idempotente).

IDEMPOTENTE
    Se puede correr las veces que sea: si la columna ya está, la deja como está.
    No toca ni un solo dato existente — los despachos ya creados quedan con NULL.

RADIO DE EXPLOSIÓN SI SE OLVIDA (correr ANTES de reiniciar el backend)
    El modelo declara la columna: sin ella en la BD, toda lectura de la entidad
    Despacho revienta con 1054 — expandir una OC en Despachos, cerrar, firmar, anular,
    la emisión de guías SII y la facturación. El listado de OCs PARECE sano (selecciona
    columnas sueltas): la trampa clásica de este patrón.

Uso (desde backend/, con el venv activo):
    python -m migrations.despacho_bulto_numero
"""
from sqlalchemy import text

from database import engine

# tabla -> {columna: definición DDL}. Ver SEPARACIÓN POR MARCA en el docstring.
TABLAS = {
    "despachos": {"bulto_numero": "VARCHAR(50) NULL"},   # MachParts / Grupo AM
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

    Cada tabla va en su propia transacción a propósito. MySQL hace COMMIT IMPLÍCITO en
    cada DDL, así que meter varias tablas en un solo `engine.begin()` PARECE atómico y no
    lo es: si la segunda fallara, la primera ya quedó aplicada igual. Separarlas hace
    visible lo que de verdad pasa y deja que el fallo de una no oculte el éxito de la otra.
    """
    with engine.begin() as conn:
        if not _tabla_existe(conn, tabla):
            # No es un error: una base puede no tener esta marca.
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
            # Se sigue con las demás y se resume al final: con un traceback pelado, el
            # operador no sabía cuál tabla quedó lista y cuál no.
            fallidas.append(tabla)
            print(f"[migracion] ERROR en {tabla}: {type(exc).__name__}: {exc}")
    if fallidas:
        raise SystemExit(
            f"[migracion] FALLÓ en: {', '.join(fallidas)}. Las demás tablas SÍ quedaron "
            "aplicadas. Corrige la causa y vuelve a correr este mismo script "
            "(es idempotente: no repite lo ya hecho). NO reinicies el backend hasta que "
            "esta migración salga limpia: falta la columna y la pantalla de Despachos "
            "responderá 1054 al expandir una OC, cerrar, firmar, emitir o facturar.")
    print("[migracion] completada")


if __name__ == "__main__":
    run()
