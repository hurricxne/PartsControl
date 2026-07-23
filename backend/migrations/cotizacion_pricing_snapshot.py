"""Migración: "foto" de precios congelada en la tabla `cotizaciones`.

`Base.metadata.create_all` crea tablas que faltan pero NO agrega columnas a tablas que
ya existen. Este script agrega, de forma idempotente y portable (MySQL/MariaDB), las dos
columnas del TC congelado (ver docs/tc-congelado-cotizacion.md):

  - pricing_snapshot     : JSON (TEXT) con los parámetros de precio congelados al Cierre
                           de Venta. NULL = sin foto → el motor usa el config global vivo.
  - pricing_snapshot_at  : cuándo se tomó la foto (auditoría).

NO hace backfill: las ventas cerradas ANTES de esta función quedan con NULL y siguen
usando el config global (decisión del dueño: congelar solo de aquí en adelante).

Reutiliza el engine configurado (DATABASE_URL) — sin credenciales hardcodeadas.

Uso (desde backend/, con el venv activo):
    python -m migrations.cotizacion_pricing_snapshot
"""
from sqlalchemy import text

from database import engine

TABLA = "cotizaciones"

# columna -> definición DDL
COLUMNS = {
    "pricing_snapshot": "TEXT NULL",
    "pricing_snapshot_at": "DATETIME NULL",
}


def _columnas_existentes(conn, tabla: str) -> set:
    rows = conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = :t"
        ),
        {"t": tabla},
    )
    return {r[0] for r in rows}


def run() -> None:
    with engine.begin() as conn:
        existentes = _columnas_existentes(conn, TABLA)
        for col, ddl in COLUMNS.items():
            if col in existentes:
                print(f"[migracion] {TABLA}.{col} ya existe — ok")
                continue
            conn.execute(text(f"ALTER TABLE {TABLA} ADD COLUMN {col} {ddl}"))
            print(f"[migracion] {TABLA}.{col} agregada")
    print("[migracion] completada")


if __name__ == "__main__":
    run()
