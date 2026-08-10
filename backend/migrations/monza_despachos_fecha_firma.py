"""Agrega a monza_despachos las columnas fecha_firma y usuario_firma_id.

Soportan el flujo "Marcar guía firmada" de Despachos MonzaParts (2026-08-06), que
convierte la firma en REQUISITO para facturar (paridad con MachParts):
  · fecha_firma      = cuándo el cliente firmó la guía recibida. Es la CUARTA fecha
                       del despacho — no confundir con fecha (creación), fecha_despacho
                       (cierre) ni fecha_guia (emisión SII de la guía en papel).
  · usuario_firma_id = quién marcó la firma en el sistema (traza, sin FK — estilo del
                       módulo).

Las hermanas guia_firmada / guia_firmada_archivo ya existen (las crea
monza_contabilidad/init_db.py desde la fase 1 del espejo).

También agrega monza_cont_factura_cliente.sin_guia (0 canal guía | 1 retiro en
oficina): el CANAL de la factura, sin el cual el neteo guía↔retiro del gate
descontaba la misma mercadería dos veces (hallazgo HIGH del multienjambre) y
mercadería entregada quedaba infacturable. NOT NULL DEFAULT 0 = las históricas
quedan como "canal guía" (atribución conservadora; el techo global evita que algo
quede atrapado).

Idempotente: si las columnas ya existen, no hace nada. El init_db del módulo
(monza_contabilidad/init_db.py) agrega LAS MISMAS columnas — cualquiera de los dos
caminos deja el esquema listo (a prueba del olvido de uno u otro).
Correr desde backend/:  python -m migrations.monza_despachos_fecha_firma
ANTES de reiniciar el backend en el deploy (el ORM ya declara las columnas y un
INSERT/UPDATE con el modelo nuevo contra la tabla vieja fallaría con
"Unknown column 'fecha_firma' in 'field list'").
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import text  # noqa: E402

from database import SessionLocal  # noqa: E402

COLUMNAS_POR_TABLA = (
    ("monza_despachos", (
        ("fecha_firma", "DATETIME NULL"),
        ("usuario_firma_id", "INTEGER NULL"),
    )),
    ("monza_cont_factura_cliente", (
        ("sin_guia", "INT NOT NULL DEFAULT 0"),
    )),
)


def main() -> None:
    db = SessionLocal()
    try:
        for tabla, columnas in COLUMNAS_POR_TABLA:
            existentes = {r[0] for r in db.execute(text(f"SHOW COLUMNS FROM {tabla}"))}
            for nombre, tipo in columnas:
                if nombre in existentes:
                    print(f"  = {tabla}.{nombre}: ya existe")
                    continue
                db.execute(text(f"ALTER TABLE {tabla} ADD COLUMN {nombre} {tipo}"))
                print(f"  + {tabla}.{nombre}: agregada")
        db.commit()
        print("OK")
    finally:
        db.close()


if __name__ == "__main__":
    main()
