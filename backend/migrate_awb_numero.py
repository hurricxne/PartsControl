"""Migración idempotente: agrega embarques.awb_numero (N° AWB/BL escrito a mano).

POR QUÉ (negocio): hoy embarques.awb guarda el NOMBRE DEL ARCHIVO del documento
AWB/BL adjunto (lo sube Logística al cerrar), NO el número de la guía aérea. El
dueño necesita un identificador universal, escribible a mano y BUSCABLE en TODOS
los embarques → columna nueva awb_numero, independiente del adjunto. `awb` queda
INTACTA (sigue siendo el archivo).

Idempotente: se puede correr 2 veces sin error (verifica antes de ALTER/CREATE).

Uso (desde backend/):
    python migrate_awb_numero.py

DEPLOY: correr ANTES de reiniciar el backend (el modelo ya declara la columna; si
el proceso arranca contra la tabla vieja, cualquier SELECT/INSERT que la nombre
fallaría).
"""
from sqlalchemy import text

from database import engine

TABLA = "embarques"
COLUMNA = "awb_numero"
INDICE = "ix_embarques_awb_numero"  # == nombre que autogenera index=True en el modelo


def _columna_existe(conn, tabla: str, columna: str) -> bool:
    row = conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"
        ),
        {"t": tabla, "c": columna},
    ).scalar()
    return bool(row)


def _indice_existe(conn, tabla: str, indice: str) -> bool:
    row = conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name = :t AND index_name = :i"
        ),
        {"t": tabla, "i": indice},
    ).scalar()
    return bool(row)


def main() -> None:
    with engine.begin() as conn:
        if not _columna_existe(conn, TABLA, COLUMNA):
            conn.execute(text(f"ALTER TABLE {TABLA} ADD COLUMN {COLUMNA} VARCHAR(100) NULL"))
            print(f"[awb_numero] + columna {TABLA}.{COLUMNA}")
        else:
            print(f"[awb_numero] = {TABLA}.{COLUMNA} ya existe")

        if not _indice_existe(conn, TABLA, INDICE):
            conn.execute(text(f"CREATE INDEX {INDICE} ON {TABLA} ({COLUMNA})"))
            print(f"[awb_numero] + índice {INDICE}")
        else:
            print(f"[awb_numero] = índice {INDICE} ya existe")

    print("[awb_numero] Listo.")


if __name__ == "__main__":
    main()
