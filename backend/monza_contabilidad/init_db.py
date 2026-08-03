"""Inicializa el módulo Contabilidad MonzaParts en la BD (idempotente).

1) Crea las tablas nuevas monza_cont_* (no toca las existentes).
2) Agrega a la tabla EXISTENTE monza_despachos las columnas guia_firmada /
   guia_firmada_archivo si faltan (create_all NO altera tablas existentes).
3) Fase 7 (factura de ANTICIPO, vía B) sobre tablas monza_cont_* que ya existían:
     · columna monza_cont_factura_cliente.es_anticipo             (0 normal | 1 anticipo)
       + NORMALIZACIÓN a NOT NULL DEFAULT 0 (las tablas creadas por create_all antes de
       que el modelo llevara server_default quedaron nullable y sin default: un NULL
       invierte el orden con que el adelanto elige factura).
     · columna monza_cont_factura_cliente_item.anticipo_factura_id (línea de descuento)
     · índice  ix_monza_cont_fact_item_anticipo
     · FK      fk_monza_cont_fact_item_anticipo → monza_cont_factura_cliente(id), SIN
       ON DELETE a propósito: la BD bloquea borrar una factura de anticipo ya descontada.
4) Anulación de adelantos: columna monza_cont_adelanto.estado
   (informado|aprobado|anulado), NOT NULL DEFAULT 'aprobado' — las filas que ya existen
   son adelantos verificados. Sin esta columna el POST /adelantos/{id}/anular no puede
   revertir un adelanto y Abastecimiento sigue comprando contra plata que nunca llegó.

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


def _indice_en_columna(conn, tabla: str, columna: str) -> bool:
    """¿Existe ALGÚN índice sobre esa columna? (por COLUMNA, no por nombre).

    Patrón monza_wasabil_dte/init_db.py: en una BD fresca el `create_all` ya creó el
    índice del `index=True` con el autoname de SQLAlchemy; en una BD ya poblada lo crea
    este script con nombre propio. Detectar por columna evita dejar la BD fresca con un
    índice DUPLICADO (dos nombres distintos sobre la misma columna)."""
    row = conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"
        ),
        {"t": tabla, "c": columna},
    ).scalar()
    return bool(row)


def _columna_not_null_default_0(conn, tabla: str, columna: str) -> bool:
    """¿La columna ya está NOT NULL con DEFAULT 0?

    Se consulta ANTES de emitir el MODIFY para que el paso sea idempotente Y barato: un
    `ALTER ... MODIFY` incondicional reconstruye la tabla en cada corrida del init_db."""
    row = conn.execute(
        text(
            "SELECT is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"
        ),
        {"t": tabla, "c": columna},
    ).first()
    return bool(row) and row[0] == "NO" and str(row[1]) == "0"


def _columna_not_null_default_txt(conn, tabla: str, columna: str, esperado: str) -> bool:
    """Gemela de _columna_not_null_default_0 para un default de TEXTO.

    El `strip("'")` no es paranoia: MySQL 8 devuelve el default de un VARCHAR sin
    comillas en information_schema y MariaDB lo devuelve entrecomillado — sin normalizar,
    el chequeo daría False para siempre y cada init_db reconstruiría la tabla."""
    row = conn.execute(
        text(
            "SELECT is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"
        ),
        {"t": tabla, "c": columna},
    ).first()
    return bool(row) and row[0] == "NO" and str(row[1] or "").strip("'") == esperado


def _fk_en_columna(conn, tabla: str, columna: str) -> bool:
    """¿Existe ALGUNA FK saliente sobre esa columna? (por COLUMNA, no por nombre): en una
    BD fresca MySQL le pone un autoname impredecible (monza_cont_..._ibfk_N), así que
    buscar por nombre volvería a intentar crearla y reventaría con errno 121."""
    row = conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.key_column_usage "
            "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c "
            "AND referenced_table_name IS NOT NULL"
        ),
        {"t": tabla, "c": columna},
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

    # 3.b) Fase 7 — factura de ANTICIPO (vía B) sobre las monza_cont_* ya creadas.
    # Va ANTES del bloque de monza_despachos a propósito: ese bloque termina con un
    # `return` temprano cuando la tabla de despachos todavía no existe, y esta migración
    # no puede quedar colgando de esa condición.
    with engine.begin() as conn:
        if not _columna_existe(conn, "monza_cont_factura_cliente", "es_anticipo"):
            conn.execute(text(
                "ALTER TABLE monza_cont_factura_cliente ADD COLUMN es_anticipo INT DEFAULT 0"))
            print("[monza_contabilidad] + columna monza_cont_factura_cliente.es_anticipo")
        else:
            print("[monza_contabilidad] = monza_cont_factura_cliente.es_anticipo ya existe")

        # NORMALIZADOR (idempotente) del esquema de es_anticipo. Cubre las tablas nacidas
        # de create_all ANTES de que el modelo llevara server_default: ahí la columna
        # quedó NULLABLE y SIN DEFAULT, así que un INSERT que no la nombre deja NULL —y
        # un NULL invierte el `ORDER BY es_anticipo DESC` que rutea el adelanto (MySQL
        # pone los NULL al final en DESC), mandando la plata a la factura equivocada.
        # El UPDATE va PRIMERO: con filas NULL vivas, el MODIFY ... NOT NULL fallaría
        # (o las convertiría en silencio según el sql_mode).
        if not _columna_not_null_default_0(conn, "monza_cont_factura_cliente", "es_anticipo"):
            conn.execute(text(
                "UPDATE monza_cont_factura_cliente SET es_anticipo = 0 "
                "WHERE es_anticipo IS NULL"))
            conn.execute(text(
                "ALTER TABLE monza_cont_factura_cliente "
                "MODIFY es_anticipo INT NOT NULL DEFAULT 0"))
            print("[monza_contabilidad] ~ monza_cont_factura_cliente.es_anticipo → NOT NULL DEFAULT 0")
        else:
            print("[monza_contabilidad] = monza_cont_factura_cliente.es_anticipo ya es NOT NULL DEFAULT 0")

        if not _columna_existe(conn, "monza_cont_factura_cliente_item", "anticipo_factura_id"):
            conn.execute(text(
                "ALTER TABLE monza_cont_factura_cliente_item "
                "ADD COLUMN anticipo_factura_id INT NULL"))
            print("[monza_contabilidad] + columna monza_cont_factura_cliente_item.anticipo_factura_id")
        else:
            print("[monza_contabilidad] = monza_cont_factura_cliente_item.anticipo_factura_id ya existe")

        # El índice va ANTES de la FK, no es capricho: MySQL crea un índice implícito al
        # agregar la FK, y si la FK fuera primero este chequeo por columna daría True y la
        # BD migrada quedaría SIN el ix_ que sí tiene la BD fresca (index=True del modelo).
        if not _indice_en_columna(conn, "monza_cont_factura_cliente_item", "anticipo_factura_id"):
            conn.execute(text(
                "CREATE INDEX ix_monza_cont_fact_item_anticipo "
                "ON monza_cont_factura_cliente_item (anticipo_factura_id)"))
            print("[monza_contabilidad] + índice ix_monza_cont_fact_item_anticipo")
        else:
            print("[monza_contabilidad] = ya hay índice sobre anticipo_factura_id")

        # SIN ON DELETE: la FK es el segundo cinturón del 409 explícito — impide borrar
        # una factura de anticipo que ya fue descontada en otra factura.
        if not _fk_en_columna(conn, "monza_cont_factura_cliente_item", "anticipo_factura_id"):
            conn.execute(text(
                "ALTER TABLE monza_cont_factura_cliente_item "
                "ADD CONSTRAINT fk_monza_cont_fact_item_anticipo "
                "FOREIGN KEY (anticipo_factura_id) REFERENCES monza_cont_factura_cliente(id)"))
            print("[monza_contabilidad] + FK fk_monza_cont_fact_item_anticipo")
        else:
            print("[monza_contabilidad] = ya hay FK sobre anticipo_factura_id")

    # 3.c) ANULACIÓN de adelantos — columna monza_cont_adelanto.estado
    # (informado|aprobado|anulado, espejo de cont_adelanto de Grupo AM). Va en su propio
    # bloque, ANTES del de monza_despachos (que corta con un `return` temprano si esa
    # tabla todavía no existe): esta migración no puede quedar colgando de esa condición.
    with engine.begin() as conn:
        if not _columna_existe(conn, "monza_cont_adelanto", "estado"):
            # DEFAULT 'aprobado' y NOT NULL de una vez: las filas que ya existen SON
            # adelantos verificados (en Monza la existencia del registro era la
            # aprobación), así que el ALTER las deja en el estado correcto sin UPDATE.
            conn.execute(text(
                "ALTER TABLE monza_cont_adelanto "
                "ADD COLUMN estado VARCHAR(20) NOT NULL DEFAULT 'aprobado'"))
            print("[monza_contabilidad] + columna monza_cont_adelanto.estado")
        else:
            print("[monza_contabilidad] = monza_cont_adelanto.estado ya existe")

        # NORMALIZADOR idempotente (mismo patrón que es_anticipo): cubre la tabla nacida
        # de un create_all que hubiera emitido la columna NULLABLE/sin default. El UPDATE
        # va PRIMERO — con filas NULL vivas el MODIFY ... NOT NULL falla (o las convierte
        # en silencio según el sql_mode).
        if not _columna_not_null_default_txt(conn, "monza_cont_adelanto", "estado", "aprobado"):
            conn.execute(text(
                "UPDATE monza_cont_adelanto SET estado = 'aprobado' "
                "WHERE estado IS NULL OR estado = ''"))
            conn.execute(text(
                "ALTER TABLE monza_cont_adelanto "
                "MODIFY estado VARCHAR(20) NOT NULL DEFAULT 'aprobado'"))
            print("[monza_contabilidad] ~ monza_cont_adelanto.estado → NOT NULL DEFAULT 'aprobado'")
        else:
            print("[monza_contabilidad] = monza_cont_adelanto.estado ya es NOT NULL DEFAULT 'aprobado'")

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
