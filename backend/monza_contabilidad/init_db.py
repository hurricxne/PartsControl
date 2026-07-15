"""Inicializa el módulo Contabilidad MonzaParts en la BD (idempotente).

1) Crea las tablas nuevas monza_cont_* (no toca las existentes).
2) Agrega a la tabla EXISTENTE monza_despachos las columnas guia_firmada /
   guia_firmada_archivo si faltan (create_all NO altera tablas existentes).

Correr una vez por entorno (local y producción):
    cd backend && python -m monza_contabilidad.init_db
"""
from sqlalchemy import text

from database import Base, engine
# Registra en el metadata las tablas referenciadas por las FK (users, monza_cotizaciones)
# y los modelos propios del módulo, antes del create_all.
import models.models  # noqa: F401  (users, etc.)
import monza_models  # noqa: F401  (monza_cotizaciones, monza_despachos, etc.)
from monza_contabilidad import models as _mc_models  # noqa: F401


def _columna_existe(conn, tabla: str, columna: str) -> bool:
    row = conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"
        ),
        {"t": tabla, "c": columna},
    ).scalar()
    return bool(row)


def _indice_existe(conn, tabla: str, indice: str) -> bool:
    row = conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name = :t AND index_name = :i"
        ),
        {"t": tabla, "i": indice},
    ).scalar()
    return bool(row)


def main() -> None:
    # 1) Tablas nuevas del módulo
    Base.metadata.create_all(bind=engine)
    print("[monza_contabilidad] tablas monza_cont_* creadas/verificadas.")

    # 2) Índice en fecha_emision para tablas ya creadas sin él (create_all no altera).
    with engine.begin() as conn:
        idx = "ix_monza_cont_factura_cliente_fecha_emision"
        if not _indice_existe(conn, "monza_cont_factura_cliente", idx):
            conn.execute(text(f"CREATE INDEX {idx} ON monza_cont_factura_cliente (fecha_emision)"))
            print(f"[monza_contabilidad] + índice {idx}")
        else:
            print(f"[monza_contabilidad] = índice {idx} ya existe")

    # 3) Columnas aditivas en monza_cotizaciones (adelanto: lo informa Comercial al cerrar)
    with engine.begin() as conn:
        existe_cot = conn.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = 'monza_cotizaciones'"
            )
        ).scalar()
        if existe_cot:
            if not _columna_existe(conn, "monza_cotizaciones", "pct_adelanto"):
                conn.execute(text("ALTER TABLE monza_cotizaciones ADD COLUMN pct_adelanto INT DEFAULT 0"))
                print("[monza_contabilidad] + columna monza_cotizaciones.pct_adelanto")
            else:
                print("[monza_contabilidad] = monza_cotizaciones.pct_adelanto ya existe")
            if not _columna_existe(conn, "monza_cotizaciones", "adelanto_verificado"):
                conn.execute(text("ALTER TABLE monza_cotizaciones ADD COLUMN adelanto_verificado INT DEFAULT 0"))
                print("[monza_contabilidad] + columna monza_cotizaciones.adelanto_verificado")
            else:
                print("[monza_contabilidad] = monza_cotizaciones.adelanto_verificado ya existe")

    # 4) Columnas aditivas en monza_despachos (firma opcional de la guía)
    with engine.begin() as conn:
        existe_tabla = conn.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = 'monza_despachos'"
            )
        ).scalar()
        if not existe_tabla:
            print("[monza_contabilidad] monza_despachos no existe aún (se creará por su propio módulo).")
            return
        if not _columna_existe(conn, "monza_despachos", "guia_firmada"):
            conn.execute(text("ALTER TABLE monza_despachos ADD COLUMN guia_firmada INT DEFAULT 0"))
            print("[monza_contabilidad] + columna monza_despachos.guia_firmada")
        else:
            print("[monza_contabilidad] = monza_despachos.guia_firmada ya existe")
        if not _columna_existe(conn, "monza_despachos", "guia_firmada_archivo"):
            conn.execute(text("ALTER TABLE monza_despachos ADD COLUMN guia_firmada_archivo VARCHAR(255) NULL"))
            print("[monza_contabilidad] + columna monza_despachos.guia_firmada_archivo")
        else:
            print("[monza_contabilidad] = monza_despachos.guia_firmada_archivo ya existe")

    print("[monza_contabilidad] init OK.")


if __name__ == "__main__":
    main()
