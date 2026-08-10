"""Agrega monza_cotizaciones.cliente_sin_oc — la marca de VENTA A CLIENTE PARTICULAR.

POR QUÉ
-------
Un consumidor final no emite orden de compra, pero el cierre de venta la exigía
(«El N° de OC del cliente es obligatorio para cerrar la venta») y, más abajo, la
emisión electrónica también: la referencia 801 del SII lleva N° Y fecha de la OC, así
que una venta sin esos datos cierra pero NO se puede facturar ni despachar con guía.

Con la marca puesta, el cierre usa la COTIZACIÓN como documento de respaldo: graba su
N° en `oc_cliente` y su fecha de emisión en `oc_fecha`. La referencia 801 sigue
apuntando a un documento real y verificable — emitido por nosotros, no por el cliente.

La marca se PERSISTE (y no es solo un parámetro del request) para dejar constancia de
por qué esa OC coincide con el N° de cotización: sin ella, quien audite la venta más
tarde lo lee como un error de digitación.

Idempotente: si la columna ya existe, no hace nada.
Correr desde backend/:  python -m migrations.monza_cliente_sin_oc
ANTES de reiniciar el backend (el ORM ya declara la columna y un SELECT contra la tabla
vieja falla con "Unknown column 'cliente_sin_oc' in 'field list'").
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import text  # noqa: E402

from database import SessionLocal  # noqa: E402

TABLA = "monza_cotizaciones"
COLUMNA = "cliente_sin_oc"
# DEFAULT 0: toda venta existente se cerró CON una OC real del cliente, que es
# justamente lo que 0 significa. No hace falta backfill.
TIPO = "INT NOT NULL DEFAULT 0"


def main() -> None:
    db = SessionLocal()
    try:
        existentes = {r[0] for r in db.execute(text(f"SHOW COLUMNS FROM {TABLA}"))}
        if COLUMNA in existentes:
            print(f"  = {TABLA}.{COLUMNA}: ya existe")
        else:
            db.execute(text(f"ALTER TABLE {TABLA} ADD COLUMN {COLUMNA} {TIPO}"))
            print(f"  + {TABLA}.{COLUMNA}: agregada")
        db.commit()
        print("OK")
    finally:
        db.close()


if __name__ == "__main__":
    main()
