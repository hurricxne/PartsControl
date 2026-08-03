"""Crea/actualiza las tablas del módulo Tesorería MonzaParts (standalone, idempotente).

Uso (desde backend/):
    python -m monza_tesoreria.init_db

1) Importa todos los modelos referenciados por las FK (users, monza_cont_egreso,
   monza_cont_adelanto, monza_cont_cobranza) para que SQLAlchemy resuelva las referencias,
   y crea SOLO las tablas que falten (checkfirst=True).
2) Migra columnas ADITIVAS en tablas existentes (create_all NO las altera):
   · monza_tes_conciliacion.fecha_egreso_previa / referencia_egreso_previa
     (snapshot del egreso al conciliar: desconciliar RESTAURA la fecha/ref que el
     operador tenía ingresada, en vez de limpiarlas por igualdad de valor;
     espejo de tesoreria/init_db.py de Grupo AM).
Correr ANTES de reiniciar el backend en cada deploy.
"""
from sqlalchemy import inspect, text

from database import Base, engine
import models.models              # noqa: F401  users
import monza_models               # noqa: F401  monza_cotizaciones...
import monza_contabilidad.models  # noqa: F401  monza_cont_adelanto / monza_cont_cobranza (FK destino)
import monza_compras_contab.models  # noqa: F401  monza_cont_egreso (FK destino)
from . import models as _m        # noqa: F401  monza_tes_*

TABLES = ("monza_tes_cuenta_bancaria", "monza_tes_cartola",
          "monza_tes_movimiento", "monza_tes_conciliacion",
          "monza_tes_conciliacion_ingreso")


def _columna_existe(conn, tabla: str, columna: str) -> bool:
    """Chequeo en information_schema ANTES de cada ALTER (migración idempotente)."""
    row = conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"
        ),
        {"t": tabla, "c": columna},
    ).scalar()
    return bool(row)


def _alter(conn, sql: str, msg: str) -> None:
    conn.execute(text(sql))
    print(f"[monza_tesoreria] + {msg}")


def main() -> None:
    print("[monza_tesoreria] Creando tablas faltantes (checkfirst=True)…")
    Base.metadata.create_all(bind=engine, checkfirst=True)
    insp = inspect(engine)
    for t in TABLES:
        print(f"  - {t}: {'OK' if insp.has_table(t) else 'FALTA'}")

    # ── Migración aditiva ───────────────────────────────────────────────────────
    with engine.begin() as conn:
        # Snapshot del egreso al conciliar (desconciliar restaura fecha/ref previas
        # del operador en vez de limpiarlas por igualdad de valor) — calcada de
        # tesoreria/init_db.py de Grupo AM. Enlaces pre-migración quedan con snapshot
        # NULL y el desconciliar cae al comportamiento histórico (limpiar).
        if not _columna_existe(conn, "monza_tes_conciliacion", "fecha_egreso_previa"):
            _alter(conn,
                   "ALTER TABLE monza_tes_conciliacion "
                   "ADD COLUMN fecha_egreso_previa DATE NULL",
                   "columna monza_tes_conciliacion.fecha_egreso_previa")
        else:
            print("[monza_tesoreria] = monza_tes_conciliacion.fecha_egreso_previa ya existe")
        if not _columna_existe(conn, "monza_tes_conciliacion", "referencia_egreso_previa"):
            _alter(conn,
                   "ALTER TABLE monza_tes_conciliacion "
                   "ADD COLUMN referencia_egreso_previa VARCHAR(120) NULL",
                   "columna monza_tes_conciliacion.referencia_egreso_previa")
        else:
            print("[monza_tesoreria] = monza_tes_conciliacion.referencia_egreso_previa ya existe")

    print("[monza_tesoreria] Listo.")


if __name__ == "__main__":
    main()
