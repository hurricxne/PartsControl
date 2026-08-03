"""Agrega a monza_cotizaciones las columnas guia_firmada y guia_firmada_archivo.

El MODELO (monza_models.MonzaCotizacion) ya las declara, pero la tabla de la base
venía con 31 columnas y el modelo con 33: cualquier INSERT desde el ORM fallaba con
"Unknown column 'guia_firmada' in 'field list'". Por eso los tests de integración de
Monza estaban rojos desde antes de esta rama.

Idempotente: si las columnas ya existen, no hace nada.
Correr desde backend/:  python -m migrations.monza_guia_firmada_cotizacion
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import text  # noqa: E402

from database import SessionLocal  # noqa: E402

COLUMNAS = (
    ("guia_firmada", "INTEGER DEFAULT 0"),
    ("guia_firmada_archivo", "VARCHAR(255) NULL"),
)


def main() -> None:
    db = SessionLocal()
    try:
        existentes = {r[0] for r in db.execute(text("SHOW COLUMNS FROM monza_cotizaciones"))}
        for nombre, tipo in COLUMNAS:
            if nombre in existentes:
                print(f"  = {nombre}: ya existe")
                continue
            db.execute(text(f"ALTER TABLE monza_cotizaciones ADD COLUMN {nombre} {tipo}"))
            print(f"  + {nombre}: agregada")
        db.commit()
        print("OK")
    finally:
        db.close()


if __name__ == "__main__":
    main()
