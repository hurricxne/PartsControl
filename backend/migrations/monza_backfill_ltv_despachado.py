"""Reparación de datos: recalcula el LTV de los clientes de MonzaParts.

POR QUÉ
    El LTV (`monza_clientes.ltv`) se sumaba SOLO cuando una venta pasaba a 'despachado'
    por el PATCH administrativo. Pero el camino real de la operación es otro: al cerrar
    el último despacho, `cerrar_despacho` volteaba la venta a 'despachado' por su cuenta
    y no sumaba nada — y después el PATCH ya no podía (veía la venta despachada y se
    consideraba idempotente). Resultado: casi todas las fichas muestran un LTV en cero o
    incompleto, y el histórico también arrastra dobles conteos del camino angosto
    «despachar → anular → re-despachar» que la reversa no siempre cerró.

    El arreglo de código (2026-08-22) hace que de aquí en adelante ambos caminos sumen
    exactamente una vez. Este script repara lo que ya pasó.

POR QUÉ RECOMPUTE Y NO UN AJUSTE INCREMENTAL
    No existe marca por venta de «esta ya sumó»: un incremental tendría que adivinar
    cuáles sumaron y cuáles no. El recompute es determinista e IDEMPOTENTE — se puede
    correr las veces que sea y siempre deja el mismo número:

        ltv(cliente) = Σ total_bruto de sus cotizaciones en estado 'despachado',
                       TENGAN O NO lead (el mismo conjunto que usa el código vivo)

    OJO CON EL DELTA: por esto las fichas de ventas SIN lead también suben. Mientras el
    LTV colgó del lead, esas ventas no sumaban nunca y una venta cuyo lead se borró
    quedaba sumada sin poder devolverse. El número que va a subir es plata que un bug
    nunca contó, no ventas nuevas — es exactamente el salto de KPIs que el checklist de
    deploy obliga a avisarle al equipo antes de correr esto.

    …acreditado al cliente FACTURADO (`monza_cotizaciones.cliente_id`), que con «Cotizar
    a» puede no ser el del lead: la plata es de quien compró.

ANTES DE CORRERLO
    · `--dry-run` (recomendado primero) imprime el ANTES → DESPUÉS por cliente y NO
      escribe nada.
    · Verificar que sigan existiendo SOLO dos escritores de `ltv` en el código
      (la suma del despacho y la resta de la reversa). Si alguien agregó un ajuste
      manual de ficha, el recompute lo borraría en silencio.
    · Solo toca MonzaParts (`monza_clientes`). Grupo AM no tiene CRM ni LTV.

Uso (desde backend/, con el venv activo):
    python -m migrations.monza_backfill_ltv_despachado --dry-run
    python -m migrations.monza_backfill_ltv_despachado
"""
import sys

from sqlalchemy import text

from database import engine

# El conjunto es el MISMO que cubre el código vivo desde 2026-08-27: toda venta
# 'despachado' del cliente facturado, CON o SIN lead. El gate por `lead_id` que había
# antes acá y en el código dejaba dos agujeros: una venta sin lead nunca sumaba, y —peor—
# borrar el lead de una venta ya sumada apagaba la RESTA y el LTV quedaba inflado para
# siempre. OJO en el deploy: las fichas de ventas sin lead SUBEN el día que esto corra
# (correr el --dry-run primero y avisar el salto, como manda el checklist §4.a-bis).
SQL_ESPERADO = """
    SELECT c.id                         AS cliente_id,
           c.nombre                     AS nombre,
           COALESCE(c.ltv, 0)           AS ltv_actual,
           COALESCE(SUM(cot.total_bruto), 0) AS ltv_esperado
      FROM monza_clientes c
      LEFT JOIN monza_cotizaciones cot
             ON cot.cliente_id = c.id
            AND cot.estado = 'despachado'
     GROUP BY c.id, c.nombre, c.ltv
    HAVING ABS(COALESCE(c.ltv, 0) - COALESCE(SUM(cot.total_bruto), 0)) > 0.5
     ORDER BY c.id
"""


def run(dry_run: bool = False) -> None:
    with engine.begin() as conn:
        existe = conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = 'monza_clientes'"
        )).scalar()
        if not existe:
            print("[backfill] monza_clientes no existe en esta base — se omite")
            return

        filas = conn.execute(text(SQL_ESPERADO)).fetchall()
        if not filas:
            print("[backfill] no hay diferencias: el LTV de todos los clientes ya cuadra")
            return

        print(f"[backfill] {len(filas)} cliente(s) con LTV descuadrado:")
        total_delta = 0.0
        for f in filas:
            delta = float(f.ltv_esperado) - float(f.ltv_actual)
            total_delta += delta
            print(f"  · #{f.cliente_id} {f.nombre[:40]:<40} "
                  f"{float(f.ltv_actual):>14,.0f} → {float(f.ltv_esperado):>14,.0f} "
                  f"({delta:+,.0f})")
        print(f"[backfill] delta total: {total_delta:+,.0f}")

        if dry_run:
            print("[backfill] --dry-run: NO se escribió nada. "
                  "Revisa los números y vuelve a correr sin la bandera.")
            return

        for f in filas:
            conn.execute(
                text("UPDATE monza_clientes SET ltv = :v WHERE id = :i"),
                {"v": float(f.ltv_esperado), "i": f.cliente_id},
            )
        print(f"[backfill] {len(filas)} ficha(s) actualizada(s). "
              "El script es idempotente: correrlo de nuevo no cambia nada.")


if __name__ == "__main__":
    run(dry_run="--dry-run" in sys.argv)
