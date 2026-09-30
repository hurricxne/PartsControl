"""Migración: folio único POR TIPO + auditoría del registro manual en `monza_cont_factura_cliente`.

QUÉ CIERRA (2026-09-29, registro manual verificado de facturas/boletas MonzaParts)
    1. El UNIQUE del folio era GLOBAL (`uq_monza_cont_factura_folio` sobre numero_factura).
       Ante el SII cada tipo de documento tiene su propia numeración: la boleta N° 35 y la
       factura N° 35 son documentos distintos y legítimos, pero el índice rechazaba
       registrar la boleta («El folio 35 ya existe»). Se reemplaza por
       `uq_monza_cont_factura_tipo_folio (tipo_doc, numero_factura)`.
    2. Columnas de auditoría del registro manual (todas NULLABLE, sin backfill: las
       facturas históricas y las emitidas por la vía SII quedan en NULL, que es lo que
       corresponde — nacieron sin esta verificación):
         origen_folio          VARCHAR(20) NULL   'wasabil' | 'externo'
         wasabil_uuid          VARCHAR(64) NULL   documento verificado en Wasabil
         folio_verificado_por  INT NULL (FK users.id ON DELETE SET NULL)
         folio_verificado_at   DATETIME NULL
         declaracion_externo   TEXT NULL          solo origen 'externo'

QUÉ SE ROMPE SI NO SE CORRE
    `monza_contabilidad/models.py` ya declara las 5 columnas: sin ellas, TODA lectura de
    facturas Monza (listado de Facturas y cobranzas, Ventas, Tesorería, emisión SII) cae con
    `error 1054 Unknown column` → HTTP 500. Correr ANTES de reiniciar el backend.

ÚNICO CAMBIO DE DATOS
    Filas con tipo_doc NULL → 'factura' (lo que el código ya asumía). Sin esto, en MySQL
    esas filas quedarían fuera del UNIQUE nuevo (los NULL no colisionan).

ORDEN SEGURO DEL ÍNDICE
    Primero se CREA el nuevo y recién después se BORRA el viejo: en ningún momento la
    tabla queda sin unicidad de folio. El viejo es más estricto que el nuevo, así que si
    el viejo existe no puede haber duplicados (tipo, folio) — igual se verifica.

FAIL-CLOSED
    Si ya hubiera folios repetidos para el mismo tipo, NO se crea el índice, NO se borra
    el viejo y no se renumera nada (renumerar documentos tributarios es una decisión de
    negocio). OJO: para entonces las columnas nuevas y el tipo_doc NULL → 'factura' YA
    quedaron aplicados (en MariaDB cada DDL confirma solo; no hay vuelta atrás dentro del
    script). Es inocuo: son aditivos y re-correr el script los reconoce.

Idempotente y portable (MySQL/MariaDB). Uso (desde backend/, con el venv activo):
    python -m migrations.monza_factura_folio_por_tipo
"""
from sqlalchemy import text

from database import engine

TABLA = "monza_cont_factura_cliente"
INDICE_NUEVO = "uq_monza_cont_factura_tipo_folio"
INDICE_VIEJO = "uq_monza_cont_factura_folio"
FK_VERIFICADOR = "fk_monza_cont_factura_folio_verificador"

# El DDL tiene que coincidir con monza_contabilidad/models.py para que una BD migrada y una
# BD fresca (create_all) queden con el MISMO esquema.
COLUMNS = {
    "origen_folio": "VARCHAR(20) NULL",
    "wasabil_uuid": "VARCHAR(64) NULL",
    "folio_verificado_por": "INT NULL",
    "folio_verificado_at": "DATETIME NULL",
    "declaracion_externo": "TEXT NULL",
}


def _scalar(conn, sql: str, **params):
    return conn.execute(text(sql), params).scalar()


def _tabla_existe(conn) -> bool:
    return bool(_scalar(conn,
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema = DATABASE() AND table_name = :t", t=TABLA))


def _columna_existe(conn, columna: str) -> bool:
    return bool(_scalar(conn,
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c",
        t=TABLA, c=columna))


def _indice_existe(conn, indice: str) -> bool:
    return bool(_scalar(conn,
        "SELECT COUNT(*) FROM information_schema.statistics "
        "WHERE table_schema = DATABASE() AND table_name = :t AND index_name = :i",
        t=TABLA, i=indice))


def _fk_verificador_existe(conn) -> bool:
    # Cualquier FK de la columna hacia users cuenta (create_all le pone nombre propio).
    return bool(_scalar(conn,
        "SELECT COUNT(*) FROM information_schema.key_column_usage "
        "WHERE table_schema = DATABASE() AND table_name = :t "
        "AND column_name = 'folio_verificado_por' AND referenced_table_name = 'users'",
        t=TABLA))


def run() -> int:
    with engine.begin() as conn:
        if not _tabla_existe(conn):
            # BD nueva (o gate MONZA_CONTAB apagado): el create_all del arranque la crea
            # ya con las columnas y el índice nuevo. No es un error.
            print(f"[migracion] {TABLA} no existe aún — la creará el create_all del arranque")
            return 0

        for col, ddl in COLUMNS.items():
            if _columna_existe(conn, col):
                print(f"[migracion] {TABLA}.{col} ya existe — ok")
                continue
            conn.execute(text(f"ALTER TABLE {TABLA} ADD COLUMN {col} {ddl}"))
            print(f"[migracion] {TABLA}.{col} agregada")

        if _fk_verificador_existe(conn):
            print(f"[migracion] FK folio_verificado_por → users ya existe — ok")
        else:
            conn.execute(text(
                f"ALTER TABLE {TABLA} ADD CONSTRAINT {FK_VERIFICADOR} "
                "FOREIGN KEY (folio_verificado_por) REFERENCES users (id) ON DELETE SET NULL"))
            print(f"[migracion] FK {FK_VERIFICADOR} creada")

        # tipo_doc NULL (filas legadas o insertadas a mano): el código ya las trata como
        # 'factura' (`tipo_doc or "factura"`) y el default del modelo es 'factura'. Hay
        # que DECIRLO en la BD antes del índice nuevo: en MySQL los NULL no colisionan en
        # un UNIQUE, así que (NULL, '35') y ('factura', '35') convivirían y el folio
        # quedaría duplicado sin que el índice lo impida. El índice global viejo sí lo
        # impedía; sin este paso, se perdería esa garantía para esas filas.
        nulos = _scalar(conn, f"SELECT COUNT(*) FROM {TABLA} WHERE tipo_doc IS NULL")
        if nulos:
            conn.execute(text(f"UPDATE {TABLA} SET tipo_doc = 'factura' WHERE tipo_doc IS NULL"))
            print(f"[migracion] {nulos} fila(s) con tipo_doc NULL marcadas como 'factura' "
                  "(lo que el sistema ya asumía)")

        if _indice_existe(conn, INDICE_NUEVO):
            print(f"[migracion] {INDICE_NUEVO} ya existe — ok")
        else:
            duplicados = conn.execute(text(
                f"SELECT tipo_doc, numero_factura, COUNT(*) c FROM {TABLA} "
                "WHERE numero_factura IS NOT NULL "
                "GROUP BY tipo_doc, numero_factura HAVING c > 1")).fetchall()
            if duplicados:
                print(f"[migracion] ✗ NO se creó {INDICE_NUEVO} — hay folios repetidos por tipo:")
                for tipo, folio, cuantos in duplicados:
                    print(f"     {tipo} N° {folio} aparece {cuantos} veces")
                print("   Corrige esos registros y vuelve a correr este script "
                      f"({INDICE_VIEJO} se deja puesto).")
                return 1
            conn.execute(text(
                f"ALTER TABLE {TABLA} ADD UNIQUE INDEX {INDICE_NUEVO} (tipo_doc, numero_factura)"))
            print(f"[migracion] {INDICE_NUEVO} creado")

        # Recién ahora, con el nuevo puesto, se retira el global.
        if _indice_existe(conn, INDICE_VIEJO):
            conn.execute(text(f"ALTER TABLE {TABLA} DROP INDEX {INDICE_VIEJO}"))
            print(f"[migracion] {INDICE_VIEJO} (folio global) eliminado")
        else:
            print(f"[migracion] {INDICE_VIEJO} ya no existe — ok")

    print("[migracion] completada")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
