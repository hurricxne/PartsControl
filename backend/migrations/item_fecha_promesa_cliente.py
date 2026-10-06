"""Migración: fecha prometida al cliente, congelada por ítem (`items_cotizacion`).

`Base.metadata.create_all` crea tablas que faltan pero NO agrega columnas a tablas que
ya existen. Este script agrega, de forma idempotente y portable (MySQL/MariaDB), la
columna de la Fase 2 del Portal de Clientes (docs/plan-portal-clientes-2026-08-20.md §7):

  - fecha_promesa_cliente : DATE con la fecha comprometida al cliente, congelada al
                            Cierre de Venta como día del cierre (hoy de Chile) + el
                            plazo del ítem en días hábiles (plazo_entrega_max o, si
                            falta, el plazo máximo por defecto del cotizador). NULL =
                            venta cerrada sin congelar o plazo que no sirve para
                            calcular → Despachos usa _item_deadline y el portal,
                            oc_cliente.fecha_entrega.

NO hace backfill: las ventas cerradas ANTES quedan con NULL (freeze-forward, el mismo
criterio que el dueño ya aprobó para pricing_snapshot / TC congelado).

Reutiliza el engine configurado (DATABASE_URL) — sin credenciales hardcodeadas.

Uso (desde backend/, con el venv activo):
    python -m migrations.item_fecha_promesa_cliente
"""
from sqlalchemy import text

from database import engine

TABLA = "items_cotizacion"

# columna -> definición DDL
COLUMNS = {
    "fecha_promesa_cliente": "DATE NULL",
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
