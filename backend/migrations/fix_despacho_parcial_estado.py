"""Reparación: líneas atascadas por el bug del despacho parcial.

Antes del arreglo en `routers/despachos.py`, cerrar una guía con cantidad PARCIAL
marcaba la línea completa como `estado_item='despachado'`, dejando el remanente
imposible de despachar y de facturar. Este script encuentra esas líneas (ítems en
estado 'despachado' cuya cantidad acumulada en despachos no anulados NO cubre la
línea completa) y las devuelve a 'en_bodega' para que el remanente sea despachable.

Idempotente y de solo-lectura hasta confirmar qué corregir: primero lista, luego
aplica. No toca ítems sin despachos (podrían venir de otros flujos). Reutiliza el
engine configurado (DATABASE_URL) — sin credenciales hardcodeadas.

Uso (desde backend/, con el venv activo):
    python -m migrations.fix_despacho_parcial_estado
"""
from sqlalchemy import text

from database import engine

TOL_QTY = 0.001


def run() -> None:
    with engine.begin() as conn:
        rows = conn.execute(text(
            """
            SELECT ic.id, ic.numero_parte, ic.cantidad,
                   COALESCE(SUM(di.qty_despachada), 0) AS despachada
            FROM items_cotizacion ic
            JOIN despacho_items di ON di.item_cotizacion_id = ic.id
            JOIN despachos d ON d.id = di.despacho_id AND d.estado != 'anulado'
            WHERE ic.estado_item = 'despachado'
            GROUP BY ic.id, ic.numero_parte, ic.cantidad
            HAVING COALESCE(SUM(di.qty_despachada), 0) + :tol < ic.cantidad
            """
        ), {"tol": TOL_QTY}).all()

        if not rows:
            print("[reparacion] sin líneas atascadas — nada que corregir")
            return

        for item_id, numero_parte, cantidad, despachada in rows:
            print(
                f"[reparacion] item {item_id} ({numero_parte}): "
                f"despachada {despachada} de {cantidad} → vuelve a 'en_bodega'"
            )
            conn.execute(
                text("UPDATE items_cotizacion SET estado_item = 'en_bodega' WHERE id = :i"),
                {"i": item_id},
            )
        print(f"[reparacion] {len(rows)} línea(s) corregida(s)")


if __name__ == "__main__":
    run()
