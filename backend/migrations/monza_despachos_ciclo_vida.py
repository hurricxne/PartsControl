"""Migración: ciclo de vida del despacho MonzaParts (Fase 2 del espejo Grupo AM).

`Base.metadata.create_all` crea tablas que faltan pero NO agrega columnas ni índices a
tablas que ya existen. Este script agrega, de forma idempotente:

  monza_despachos.fecha_despacho    DATETIME NULL   — cuándo se CERRÓ (≠ fecha creación)
  monza_despachos.numero_expedicion VARCHAR(120)    — N° de expedición del courier (llega después)
  UNIQUE INDEX uq_monza_despachos_numero (numero)   — sostiene el retry del correlativo DSP

El índice único solo se crea si NO hay números duplicados preexistentes (en ese caso
avisa y sigue: el sistema funciona igual, solo queda sin la protección de carrera del
correlativo hasta depurar los duplicados a mano).

NO toca datos: los despachos legados conservan su estado 'despachado' histórico.

Uso (desde backend/, con el venv activo):
    python -m migrations.monza_despachos_ciclo_vida
"""
from sqlalchemy import text

from database import engine

TABLA = "monza_despachos"

COLUMNS = {
    "fecha_despacho": "DATETIME NULL",
    "numero_expedicion": "VARCHAR(120) NULL",
}

INDICE_UNICO = "uq_monza_despachos_numero"


def _columnas_existentes(conn, tabla: str) -> set:
    rows = conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = :t"
        ),
        {"t": tabla},
    )
    return {r[0] for r in rows}


def _indices_existentes(conn, tabla: str) -> set:
    rows = conn.execute(
        text(
            "SELECT index_name FROM information_schema.statistics "
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

        if INDICE_UNICO in _indices_existentes(conn, TABLA):
            print(f"[migracion] índice {INDICE_UNICO} ya existe — ok")
        else:
            duplicados = conn.execute(text(
                f"SELECT COUNT(*) FROM (SELECT numero FROM {TABLA} "
                "WHERE numero IS NOT NULL GROUP BY numero HAVING COUNT(*) > 1) t"
            )).scalar()
            if duplicados:
                print(
                    f"[migracion] AVISO: {duplicados} número(s) de despacho duplicados — "
                    f"NO se crea el índice único {INDICE_UNICO}. Depurar a mano y re-correr."
                )
            else:
                conn.execute(text(
                    f"ALTER TABLE {TABLA} ADD UNIQUE INDEX {INDICE_UNICO} (numero)"
                ))
                print(f"[migracion] índice único {INDICE_UNICO} creado")
    print("[migracion] completada")


if __name__ == "__main__":
    run()
