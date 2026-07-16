"""Migración: columnas de dinero/margen del pipeline de precios FLOAT -> DOUBLE.

MySQL FLOAT (precisión simple) conserva ~6-7 dígitos significativos: los precios
CLP en millones y el round-trip precio->margen->precio (editar el precio de venta
mueve el margen) producían diferencias de pesos enteros (caso real del cliente:
guardar 5.200.000 devolvía 5.200.007 porque margen 2862,4361 se truncaba a 2862,44).
DOUBLE da 15-16 dígitos; junto con el redondeo a peso entero en pricing_service /
pricingCalc.ts el round-trip queda exacto.

Uso (desde backend/, con el venv del proyecto):
    python -m migrations.float_to_double_pricing

Idempotente: solo modifica columnas que sigan siendo FLOAT. Conserva nulabilidad
y default. No toca datos (ensanchamiento in-place).
"""
from sqlalchemy import text

from database import engine

TABLAS = ("items_cotizacion", "configuracion_cotizador", "despacho_items")


def main() -> None:
    in_list = ", ".join(f"'{t}'" for t in TABLAS)
    with engine.begin() as cx:
        rows = cx.execute(text(
            "SELECT TABLE_NAME, COLUMN_NAME, IS_NULLABLE, COLUMN_DEFAULT "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND DATA_TYPE = 'float' "
            f"AND TABLE_NAME IN ({in_list}) "
            "ORDER BY TABLE_NAME, ORDINAL_POSITION"
        )).fetchall()
        if not rows:
            print("[float->double] nada que migrar (ya aplicado)")
            return
        for t, c, nullable, default in rows:
            ddl = f"ALTER TABLE {t} MODIFY {c} DOUBLE"
            ddl += " NULL" if nullable == "YES" else " NOT NULL"
            if default is not None and str(default).upper() != "NULL":
                ddl += f" DEFAULT {default}"
            cx.execute(text(ddl))
            print(f"[float->double] {t}.{c} -> DOUBLE  OK")
    print(f"[float->double] listo ({len(rows)} columnas)")


if __name__ == "__main__":
    main()
