"""Migración: moneda de la tarifa aérea en `monza_cotizaciones` (foto de precios completa).

`Base.metadata.create_all` no agrega columnas a tablas existentes. Este script agrega,
idempotente y portable (MySQL/MariaDB):

  monza_cotizaciones.moneda_tarifa VARCHAR(10) NULL — moneda de la tarifa aérea (EUR/USD)
  congelada al crear la cotización. Sin ella la foto de precios (tc_usd_clp/tc_eur_clp/
  tarifa_aerea/iva_pct) no permite reproducir el cálculo exacto (espejo del
  pricing_snapshot de Grupo AM, criterio freeze-forward de9629f).

Sin backfill: las cotizaciones creadas antes quedan con NULL (la foto solo aplica de
aquí en adelante, igual que el TC congelado de GA).

Uso (desde backend/, con el venv activo):
    python -m migrations.monza_moneda_tarifa
"""
from sqlalchemy import text

from database import engine

TABLA = "monza_cotizaciones"
COLUMNS = {"moneda_tarifa": "VARCHAR(10) NULL"}


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
