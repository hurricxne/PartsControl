"""Crea las 7 tablas del Libro de compras del SII de MonzaParts (standalone, idempotente).

Uso (desde backend/):
    python -m monza_wasabil_compras.init_db

Espejo deliberado de `wasabil_compras/init_db.py` (Grupo AM) con las tablas y las
dependencias PROPIAS de MonzaParts: las dos marcas son productos separados y no
comparten código. El porqué de que exista el script está explicado allá y vale igual
acá: el auditor de esquema (`deploy/audit_schema.py`, §1.e del checklist) corre ANTES de
reiniciar, así que las tablas que solo nacían del `create_all` del arranque lo dejaban
rojo en el primer deploy, con la orden imposible de «correr las migraciones».

DOS DIFERENCIAS CON EL DE GRUPO AM
1. Este script es 🟡 **solo con el gate**: `monza_sii_libro_match` y
   `monza_sii_match_etiqueta_mov` tienen FK a `monza_tes_movimiento`, que es de la
   Tesorería de Monza y solo existe con `MONZA_CONTAB_ENABLED=true`. Con el gate
   apagado no hay nada que crear y el script lo dice, sin fallar.
2. ORDEN: después de `monza_tesoreria.init_db`.
"""
from sqlalchemy import text

from database import Base, engine
from monza_wasabil_compras import models as _models  # noqa: F401  (registra las 7 tablas)

TABLAS = (
    "monza_sii_libro_doc",
    "monza_sii_libro_sync_run",
    "monza_sii_libro_regla_rut",
    "monza_sii_libro_match",
    "monza_sii_match_run",
    "monza_sii_match_etiqueta_mov",
    "monza_sii_match_config",
)

# `users` es del núcleo (compartido). `monza_tes_movimiento` es de Tesorería Monza y
# solo existe con el gate contable encendido: si falta, este módulo no corresponde
# todavía y el script se abstiene en vez de reventar con un errno 150.
REQUISITO_NUCLEO = "users"
REQUISITO_GATE = "monza_tes_movimiento"


def _tabla_existe(conn, tabla: str) -> bool:
    return bool(conn.execute(
        text("SELECT COUNT(*) FROM information_schema.tables "
             "WHERE table_schema = DATABASE() AND table_name = :t"),
        {"t": tabla},
    ).scalar())


def main() -> None:
    with engine.connect() as conn:
        hay_nucleo = _tabla_existe(conn, REQUISITO_NUCLEO)
        hay_gate = _tabla_existe(conn, REQUISITO_GATE)

    if not hay_nucleo:
        raise SystemExit(
            f"[monza_wasabil_compras] falta la tabla `{REQUISITO_NUCLEO}` del núcleo: "
            "esta base no está inicializada. No se creó nada."
        )
    if not hay_gate:
        print(f"[monza_wasabil_compras] `{REQUISITO_GATE}` no existe: la contabilidad "
              "de MonzaParts está apagada (MONZA_CONTAB_ENABLED=false) y el Libro SII "
              "de Monza no se monta. No hay nada que crear — no es un error.")
        print("[monza_wasabil_compras] Listo (sin migraciones pendientes).")
        return

    print("[monza_wasabil_compras] Creando las tablas del libro SII Monza que falten "
          "(checkfirst=True)…")
    Base.metadata.create_all(
        bind=engine,
        tables=[Base.metadata.tables[t] for t in TABLAS],
        checkfirst=True,
    )

    with engine.connect() as conn:
        for t in TABLAS:
            print(f"  - {t}: {'OK' if _tabla_existe(conn, t) else 'FALTA'}")

    print("[monza_wasabil_compras] Listo (sin migraciones pendientes).")


if __name__ == "__main__":
    main()
