"""Parte la tarifa aérea de MonzaParts en DOS: una por EUR y otra por USD.

POR QUÉ
-------
`monza_config` tenía UNA tarifa (`tarifa_aerea_por_kg`) + la moneda en que estaba
expresada (`moneda_tarifa`). Cuando la calculadora ganó el selector de moneda del
flete (commit 87aec91), elegir USD seguía usando ese MISMO número: una tarifa de
4,5 EUR/kg se cotizaba como 4,5 USD/kg. El flete quedaba mal y el error viajaba
dentro del precio de venta, sin aviso. El courier cobra precios distintos por
moneda, así que la tarifa deja de ser "un número con etiqueta" y pasa a ser dos.

QUÉ HACE
--------
1. Agrega `tarifa_aerea_eur_por_kg` y `tarifa_aerea_usd_por_kg` (FLOAT NULL).
2. BACKFILL conservador: copia la tarifa que ya existía a la columna de SU moneda
   (la que dice `moneda_tarifa`). La otra queda NULL = "no configurada", y la
   calculadora BLOQUEA al elegirla con un mensaje que manda a Configuración —
   nunca cotiza con 0, que subvaluaría la venta en silencio.

Así, el día del deploy, toda cotización que ya funcionaba sigue funcionando igual
(misma moneda, misma tarifa) y solo la moneda nueva pide el dato una vez.

NULL vs 0: se distinguen a propósito. NULL es "nadie la cargó todavía"; 0 sería
"el flete es gratis", que es una afirmación sobre el negocio y no un vacío.

Idempotente: si las columnas ya existen, no toca nada (tampoco re-hace el backfill,
para no pisar un valor que el operador haya corregido a mano después).

Correr desde backend/:  python -m migrations.monza_tarifa_aerea_por_moneda
ANTES de reiniciar el backend (el ORM ya declara las columnas y un SELECT contra la
tabla vieja falla con "Unknown column 'tarifa_aerea_eur_por_kg' in 'field list'").
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import text  # noqa: E402

from database import SessionLocal  # noqa: E402

TABLA = "monza_config"
COLUMNAS = (
    ("tarifa_aerea_eur_por_kg", "FLOAT NULL"),
    ("tarifa_aerea_usd_por_kg", "FLOAT NULL"),
)
# moneda_tarifa → columna nueva que recibe el valor legado.
DESTINO_BACKFILL = {
    "EUR": "tarifa_aerea_eur_por_kg",
    "USD": "tarifa_aerea_usd_por_kg",
}


def main() -> None:
    db = SessionLocal()
    try:
        existentes = {r[0] for r in db.execute(text(f"SHOW COLUMNS FROM {TABLA}"))}
        agregadas = []
        for nombre, tipo in COLUMNAS:
            if nombre in existentes:
                print(f"  = {TABLA}.{nombre}: ya existe")
                continue
            db.execute(text(f"ALTER TABLE {TABLA} ADD COLUMN {nombre} {tipo}"))
            agregadas.append(nombre)
            print(f"  + {TABLA}.{nombre}: agregada")

        if agregadas:
            # Backfill SOLO en la corrida que creó las columnas: en una segunda pasada
            # las filas ya podrían tener valores corregidos a mano y pisarlos sería peor
            # que no migrar.
            filas = db.execute(text(
                f"SELECT id, moneda_tarifa, tarifa_aerea_por_kg FROM {TABLA}"
            )).fetchall()
            for cfg_id, moneda, tarifa in filas:
                destino = DESTINO_BACKFILL.get((moneda or "EUR").upper())
                if not destino or destino not in dict(COLUMNAS) or tarifa is None:
                    print(f"  · fila id={cfg_id}: sin tarifa o moneda desconocida "
                          f"({moneda!r}) — queda para cargar en Configuración")
                    continue
                db.execute(
                    text(f"UPDATE {TABLA} SET {destino} = :t WHERE id = :i"),
                    {"t": float(tarifa), "i": cfg_id},
                )
                otra = next(c for c, _ in COLUMNAS if c != destino)
                print(f"  ~ fila id={cfg_id}: {tarifa} → {destino} "
                      f"(la otra, {otra}, queda por cargar en Configuración)")
        db.commit()
        print("OK")
    finally:
        db.close()


if __name__ == "__main__":
    main()
