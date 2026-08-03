"""Migración: fecha de la OC del cliente en `monza_cotizaciones` (Fase 3 espejo GA).

`Base.metadata.create_all` no agrega columnas a tablas existentes. Este script agrega,
idempotente y portable (MySQL/MariaDB):

  monza_cotizaciones.oc_fecha DATE NULL — fecha de la OC del cliente. La referencia
  801 del SII exige N° Y fecha de la OC; se captura al cerrar la venta.

Sin backfill: las ventas cerradas antes quedan con NULL (editable después por el PATCH).

Uso (desde backend/, con el venv activo):
    python -m migrations.monza_oc_fecha_fase3
"""
from sqlalchemy import text

from database import engine

TABLA = "monza_cotizaciones"
COLUMNS = {"oc_fecha": "DATE NULL"}


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
