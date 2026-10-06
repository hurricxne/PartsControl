"""Backfill histórico: ancla cada cotización a su ficha de cliente por RUT saneado.

Uso (desde backend/, con el venv activo):
    python -m portal.vincular_clientes            # ENSAYO: muestra qué haría, no escribe
    python -m portal.vincular_clientes --aplicar  # escribe cotizaciones.cliente_id

POR QUÉ
    El Portal de Clientes filtra por cotizaciones.cliente_id. Las ventas nuevas quedan
    vinculadas solas al Cierre de Venta; este script es para lo que se cerró antes y
    para volver a pasar después de crear o corregir fichas en la pantalla Clientes.

QUÉ HACE CON LO YA VINCULADO
    Lo repasa también: el vínculo es un dato DERIVADO del RUT actual de la venta. Una
    venta cuyo RUT ya no calza con su ficha (se corrigió el RUT, o la ficha quedó
    ambigua) se DESVINCULA, y el reporte dice cuántas.

QUÉ NO HACE
    No crea fichas de cliente (eso es decisión de una persona: ojo con meter a un
    contratista bajo el RUT de la minera madre). No adivina: sin RUT, con RUT mal
    formado, sin ficha o con dos fichas para el mismo RUT, la venta queda sin vincular y
    aparece en el reporte de huérfanas.

IDEMPOTENTE
    Se puede correr las veces que sea. Lo mismo hace el botón «Vincular ventas» de la
    pantalla Cuentas del portal (POST /api/portal-admin/vinculacion/aplicar).
"""
import sys

from database import SessionLocal
from .service import vincular_historico


def _imprimir(resultado: dict) -> None:
    modo = "APLICADO" if resultado["aplicado"] else "ENSAYO (no se escribió nada)"
    print(f"[portal] Vinculación de cotizaciones — {modo}")
    print(f"  ya vinculadas antes : {resultado['ya_vinculadas']}")
    verbo = "vinculadas ahora    " if resultado["aplicado"] else "se vincularían      "
    print(f"  {verbo}: {resultado['vinculadas_ahora']}")
    verbo = "desvinculadas ahora " if resultado["aplicado"] else "se desvincularían   "
    print(f"  {verbo}: {resultado['desvinculadas_ahora']}  (su RUT ya no calza con su ficha)")
    huerfanas = resultado["huerfanas"]
    print(f"  ventas cerradas HUÉRFANAS (el cliente no las verá): {len(huerfanas)}")
    for h in huerfanas:
        print(f"    · cot #{h['cotizacion_id']} {h['numero']!s:<18} "
              f"{h['cliente'][:38]!s:<38} RUT {h['rut_cliente'] or '—':<14} → {h['motivo']}")
    for amb in resultado["ruts_ambiguos"]:
        print(f"  RUT AMBIGUO {amb['rut']}: fichas {amb['cliente_ids']} — unificar en Clientes")
    if not resultado["aplicado"] and (resultado["vinculadas_ahora"] or resultado["desvinculadas_ahora"]):
        print("  → para escribir: python -m portal.vincular_clientes --aplicar")


def main(argv: list) -> int:
    aplicar = "--aplicar" in argv
    db = SessionLocal()
    try:
        resultado = vincular_historico(db, aplicar=aplicar)
        if aplicar:
            db.commit()
        else:
            db.rollback()
        _imprimir(resultado)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
