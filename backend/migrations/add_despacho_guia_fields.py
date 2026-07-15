"""Migración: columnas del flujo de guía firmada en la tabla `despachos`.

`Base.metadata.create_all` crea tablas que faltan pero NO agrega columnas a tablas que
ya existen. Este script agrega, de forma idempotente y portable (MySQL/MariaDB), las
columnas que el flujo de guía firmada necesita. Reutiliza el engine configurado
(DATABASE_URL) — sin credenciales hardcodeadas.

Uso (desde backend/, con el venv activo):
    python -m migrations.add_despacho_guia_fields
"""
from sqlalchemy import text

from database import engine

# columna -> definición DDL
COLUMNS = {
    "guia_firmada": "INT DEFAULT 0",
    "fecha_firma": "DATETIME NULL",
    "usuario_firma_id": "INT NULL",
    "numero_expedicion": "VARCHAR(120) NULL",
    "guia_firmada_archivo": "VARCHAR(255) NULL",
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
        existentes = _columnas_existentes(conn, "despachos")
        for col, ddl in COLUMNS.items():
            if col in existentes:
                print(f"[migracion] despachos.{col} ya existe — ok")
                continue
            conn.execute(text(f"ALTER TABLE despachos ADD COLUMN {col} {ddl}"))
            print(f"[migracion] despachos.{col} agregada")
    print("[migracion] completada")


if __name__ == "__main__":
    run()
