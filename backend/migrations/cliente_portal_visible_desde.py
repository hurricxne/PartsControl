"""Migración: corte de visibilidad del Portal de Clientes por ficha (`clientes`).

`Base.metadata.create_all` no agrega columnas a tablas que ya existen. Este script agrega,
de forma idempotente, la columna:

  - portal_visible_desde : DATETIME NULL. El cliente ve en el portal solo las ventas
                           ingresadas al sistema (oc_cliente.created_at) desde este
                           momento. NULL = todas, que es lo de siempre: agregarla no
                           cambia lo que ve ningún cliente.

Se fija por ficha con `python -m portal.visible_desde` (pedido de HEPI, 2026-11-09).
Correr ANTES de reiniciar la API: el modelo `Cliente` ya la declara y sin ella toda
consulta a `clientes` falla.

Uso (desde backend/, con el venv activo):
    python -m migrations.cliente_portal_visible_desde
"""
from sqlalchemy import text

from database import engine

TABLA = "clientes"
COLUMNS = {
    "portal_visible_desde": "DATETIME NULL",
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
