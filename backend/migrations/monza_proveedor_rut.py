"""Migración: RUT canónico del proveedor Monza (Fase 0 del libro de compras del SII).

QUÉ AGREGA
    monza_proveedores.rut  VARCHAR(20) NULL  + índice ix_monza_proveedores_rut — MonzaParts.
    (Molde: migrations/proveedor_rut.py de Grupo AM — duplicación entre marcas deliberada.)

POR QUÉ
    El ERP no guardaba el RUT del proveedor Monza en ninguna parte (verificado: la tabla
    `monza_proveedores` no tenía la columna; el único RUT era monza_cont_compra.proveedor_rut,
    texto libre a mano). El libro de compras del SII identifica a cada emisor por RUT: sin
    una columna canónica no hay cruce posible, y el informe de faltantes mentiría el día
    uno. Forma canónica: '76513680-6' (monza_wasabil_compras/rut.py).

SIN BACKFILL a propósito
    Los proveedores existentes quedan NULL (regla de la casa: los datos legados no se
    adivinan). Se van completando al usarlos: la bandeja de la Fase A2 ofrece asociar el
    RUT del documento al proveedor con un clic, y ese camino escribe canónico validado.

IDEMPOTENTE
    Correr N veces = correr 1 vez. No toca ni un dato existente.

Uso (desde backend/, con el venv activo):
    python -m migrations.monza_proveedor_rut
"""
from sqlalchemy import text

from database import engine

TABLA = "monza_proveedores"
COLUMNA = "rut"
DDL = "VARCHAR(20) NULL"
INDICE = "ix_monza_proveedores_rut"


def _existe_columna(conn) -> bool:
    return bool(conn.execute(
        text("SELECT COUNT(*) FROM information_schema.columns "
             "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"),
        {"t": TABLA, "c": COLUMNA},
    ).scalar())


def _existe_indice(conn) -> bool:
    return bool(conn.execute(
        text("SELECT COUNT(*) FROM information_schema.statistics "
             "WHERE table_schema = DATABASE() AND table_name = :t AND index_name = :i"),
        {"t": TABLA, "i": INDICE},
    ).scalar())


def run() -> None:
    with engine.begin() as conn:
        existe_tabla = conn.execute(
            text("SELECT COUNT(*) FROM information_schema.tables "
                 "WHERE table_schema = DATABASE() AND table_name = :t"),
            {"t": TABLA},
        ).scalar()
        if not existe_tabla:
            raise SystemExit(
                f"[migracion] la tabla {TABLA} no existe en esta base — esta migración es "
                "de MonzaParts; revisa contra qué base estás corriendo")
        if _existe_columna(conn):
            print(f"[migracion] {TABLA}.{COLUMNA} ya existe — ok")
        else:
            conn.execute(text(f"ALTER TABLE {TABLA} ADD COLUMN {COLUMNA} {DDL}"))
            print(f"[migracion] {TABLA}.{COLUMNA} agregada")
        if _existe_indice(conn):
            print(f"[migracion] índice {INDICE} ya existe — ok")
        else:
            # El índice es lo que hace barato el cruce por RUT de la Fase A2 (una consulta
            # por documento del libro contra monza_proveedores). La tabla es chica: el
            # ALTER no bloquea de forma apreciable.
            conn.execute(text(f"CREATE INDEX {INDICE} ON {TABLA} ({COLUMNA})"))
            print(f"[migracion] índice {INDICE} creado")
    print("[migracion] completada")


if __name__ == "__main__":
    run()
