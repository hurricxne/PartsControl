"""Crea/actualiza las tablas del módulo Tesorería (standalone, idempotente).

Uso (desde backend/):
    python -m tesoreria.init_db

1) Importa los modelos referenciados por las FK (users, cont_egreso, cont_cobranza) para
   resolver las referencias y crea SOLO las tablas que falten (checkfirst=True), incluida
   cont_adelanto (adelantos de cliente Grupo AM).
2) Migra columnas/constraints ADITIVAS en tablas existentes (create_all NO las altera):
   · cont_factura_cliente.es_anticipo            (factura de anticipo, sin guía)
   · cont_factura_cliente_item.anticipo_factura_id (línea de descuento por anticipo)
   · conc_conciliacion_ingreso.adelanto_id + cobranza_id NULLABLE + UNIQUE + CHECK
     (vía de conciliación abono ↔ adelanto, espejo de monza_tes_conciliacion)
Las conc_* del antiguo módulo Conciliación Bancaria se conservan con sus datos.
"""
from sqlalchemy import inspect, text

from database import Base, engine
import models.models            # noqa: F401  users / cont_factura_cliente / cont_adelanto
import compras_contab.models    # noqa: F401  cont_egreso (FK destino de conc_conciliacion)
from . import models as _tes_models  # noqa: F401  conc_* + conc_conciliacion_ingreso

TABLES = (
    "conc_cuenta_bancaria", "conc_cartola", "conc_movimiento",
    "conc_conciliacion", "conc_conciliacion_ingreso", "cont_adelanto",
)


def _columna_existe(conn, tabla: str, columna: str) -> bool:
    row = conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"
        ),
        {"t": tabla, "c": columna},
    ).scalar()
    return bool(row)


def _columna_nullable(conn, tabla: str, columna: str) -> bool:
    row = conn.execute(
        text(
            "SELECT IS_NULLABLE FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"
        ),
        {"t": tabla, "c": columna},
    ).scalar()
    return (row or "").upper() == "YES"


def _constraint_existe(conn, tabla: str, nombre: str) -> bool:
    """UNIQUE / FOREIGN KEY / CHECK, por nombre, en information_schema."""
    row = conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.table_constraints "
            "WHERE table_schema = DATABASE() AND table_name = :t AND constraint_name = :n"
        ),
        {"t": tabla, "n": nombre},
    ).scalar()
    return bool(row)


def _alter(conn, sql: str, msg: str) -> None:
    conn.execute(text(sql))
    print(f"[tesoreria] + {msg}")


def main() -> None:
    print("[tesoreria] Creando tablas faltantes (checkfirst=True)…")
    Base.metadata.create_all(bind=engine, checkfirst=True)
    insp = inspect(engine)
    for t in TABLES:
        print(f"  - {t}: {'OK' if insp.has_table(t) else 'FALTA'}")

    # ── Migración aditiva (adelantos de cliente) ────────────────────────────────
    with engine.begin() as conn:
        # Factura de anticipo (respalda un adelanto ante el SII; no exige guía firmada)
        if not _columna_existe(conn, "cont_factura_cliente", "es_anticipo"):
            _alter(conn,
                   "ALTER TABLE cont_factura_cliente ADD COLUMN es_anticipo INT DEFAULT 0",
                   "columna cont_factura_cliente.es_anticipo")
        else:
            print("[tesoreria] = cont_factura_cliente.es_anticipo ya existe")

        # Línea de descuento por anticipo facturado (referencia a la factura de anticipo)
        if not _columna_existe(conn, "cont_factura_cliente_item", "anticipo_factura_id"):
            _alter(conn,
                   "ALTER TABLE cont_factura_cliente_item "
                   "ADD COLUMN anticipo_factura_id INT NULL",
                   "columna cont_factura_cliente_item.anticipo_factura_id")
        else:
            print("[tesoreria] = cont_factura_cliente_item.anticipo_factura_id ya existe")
        if not _constraint_existe(conn, "cont_factura_cliente_item",
                                  "fk_cont_fact_item_anticipo"):
            # Sin ON DELETE: la FK bloquea borrar una factura de anticipo ya descontada
            _alter(conn,
                   "ALTER TABLE cont_factura_cliente_item "
                   "ADD CONSTRAINT fk_cont_fact_item_anticipo "
                   "FOREIGN KEY (anticipo_factura_id) REFERENCES cont_factura_cliente(id)",
                   "FK cont_factura_cliente_item.anticipo_factura_id")
        else:
            print("[tesoreria] = FK fk_cont_fact_item_anticipo ya existe")

        # Trazabilidad cobranza 'adelanto' → adelanto que la generó (reversión exacta)
        if not _columna_existe(conn, "cont_cobranza", "adelanto_id"):
            _alter(conn,
                   "ALTER TABLE cont_cobranza ADD COLUMN adelanto_id INT NULL",
                   "columna cont_cobranza.adelanto_id")
        else:
            print("[tesoreria] = cont_cobranza.adelanto_id ya existe")
        if not _constraint_existe(conn, "cont_cobranza", "fk_cont_cobranza_adelanto"):
            _alter(conn,
                   "ALTER TABLE cont_cobranza "
                   "ADD CONSTRAINT fk_cont_cobranza_adelanto "
                   "FOREIGN KEY (adelanto_id) REFERENCES cont_adelanto(id)",
                   "FK cont_cobranza.adelanto_id")
        else:
            print("[tesoreria] = FK fk_cont_cobranza_adelanto ya existe")

        # Conciliación abono ↔ adelanto (exactamente uno: cobranza_id XOR adelanto_id)
        if not _columna_nullable(conn, "conc_conciliacion_ingreso", "cobranza_id"):
            _alter(conn,
                   "ALTER TABLE conc_conciliacion_ingreso MODIFY cobranza_id INT NULL",
                   "conc_conciliacion_ingreso.cobranza_id ahora NULLABLE")
        else:
            print("[tesoreria] = conc_conciliacion_ingreso.cobranza_id ya es nullable")
        if not _columna_existe(conn, "conc_conciliacion_ingreso", "adelanto_id"):
            _alter(conn,
                   "ALTER TABLE conc_conciliacion_ingreso ADD COLUMN adelanto_id INT NULL",
                   "columna conc_conciliacion_ingreso.adelanto_id")
        else:
            print("[tesoreria] = conc_conciliacion_ingreso.adelanto_id ya existe")
        if not _constraint_existe(conn, "conc_conciliacion_ingreso",
                                  "fk_conc_concil_ing_adelanto"):
            _alter(conn,
                   "ALTER TABLE conc_conciliacion_ingreso "
                   "ADD CONSTRAINT fk_conc_concil_ing_adelanto "
                   "FOREIGN KEY (adelanto_id) REFERENCES cont_adelanto(id) ON DELETE CASCADE",
                   "FK conc_conciliacion_ingreso.adelanto_id")
        else:
            print("[tesoreria] = FK fk_conc_concil_ing_adelanto ya existe")
        if not _constraint_existe(conn, "conc_conciliacion_ingreso",
                                  "uq_conc_concil_ing_adelanto"):
            _alter(conn,
                   "ALTER TABLE conc_conciliacion_ingreso "
                   "ADD CONSTRAINT uq_conc_concil_ing_adelanto UNIQUE (adelanto_id)",
                   "UNIQUE conc_conciliacion_ingreso.adelanto_id")
        else:
            print("[tesoreria] = UNIQUE uq_conc_concil_ing_adelanto ya existe")
        if not _constraint_existe(conn, "conc_conciliacion_ingreso",
                                  "ck_conc_concil_ing_un_destino"):
            _alter(conn,
                   "ALTER TABLE conc_conciliacion_ingreso "
                   "ADD CONSTRAINT ck_conc_concil_ing_un_destino "
                   "CHECK ((cobranza_id IS NOT NULL) + (adelanto_id IS NOT NULL) = 1)",
                   "CHECK exactamente-un-destino en conc_conciliacion_ingreso")
        else:
            print("[tesoreria] = CHECK ck_conc_concil_ing_un_destino ya existe")

        # Snapshot del egreso al conciliar (desconciliar restaura fecha/ref previas
        # del operador en vez de limpiarlas por igualdad de valor)
        if not _columna_existe(conn, "conc_conciliacion", "fecha_egreso_previa"):
            _alter(conn,
                   "ALTER TABLE conc_conciliacion "
                   "ADD COLUMN fecha_egreso_previa DATE NULL",
                   "columna conc_conciliacion.fecha_egreso_previa")
        else:
            print("[tesoreria] = conc_conciliacion.fecha_egreso_previa ya existe")
        if not _columna_existe(conn, "conc_conciliacion", "referencia_egreso_previa"):
            _alter(conn,
                   "ALTER TABLE conc_conciliacion "
                   "ADD COLUMN referencia_egreso_previa VARCHAR(120) NULL",
                   "columna conc_conciliacion.referencia_egreso_previa")
        else:
            print("[tesoreria] = conc_conciliacion.referencia_egreso_previa ya existe")

    print("[tesoreria] Listo.")


if __name__ == "__main__":
    main()
