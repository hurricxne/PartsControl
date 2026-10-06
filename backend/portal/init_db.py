"""Crea/actualiza el esquema del Portal de Clientes (standalone, idempotente).

Uso (desde backend/, con el venv activo):
    python -m portal.init_db

QUÉ AGREGA
    1) Dos columnas ADITIVAS en tablas del núcleo (create_all NO altera tablas que ya
       existen, por eso van acá):
         users.tipo_acceso        VARCHAR(20) NOT NULL DEFAULT 'interno'
         cotizaciones.cliente_id  INT NULL  + índice + FK a clientes(id) ON DELETE SET NULL
    2) Las tablas propias del módulo (solo si faltan):
         usuario_cliente   puente usuario ↔ cliente ↔ perfil
         portal_acceso     vigencia, último uso y clave temporal de cada cuenta portal
         foto_articulo     las fotos de los repuestos que ve el cliente
         portal_descarga_log  quién descargó qué documento

POR QUÉ
    Fase 1 del Portal de Clientes (docs/fase-1-portal-identidad-2026-10-02.md): las
    cuentas de los clientes viven en la misma tabla `users` y se distinguen por
    `tipo_acceso`; cada venta se ancla a su cliente por `cotizaciones.cliente_id`.

IDEMPOTENTE
    Se puede correr las veces que sea. No toca ni un dato existente: todo usuario que ya
    existía queda 'interno' (que es lo que era) y toda cotización queda con cliente_id
    NULL (= sin vincular; el vínculo lo hace `python -m portal.vincular_clientes`).

EFECTO SI NO SE CORRE (severidad CRÍTICA)
    Los modelos del núcleo YA declaran las dos columnas → el primer SELECT sobre `users`
    revienta con 1054 «Unknown column users.tipo_acceso», y ese SELECT es el LOGIN y
    auth.get_current_user: NADIE puede entrar ni usar el ERP, en ninguna de las dos
    marcas. Lo mismo `cotizaciones.cliente_id` en toda pantalla que lea cotizaciones.
    Correr ESTE script ANTES de reiniciar el backend.

`main()` devuelve la lista de pasos que quedaron PENDIENTES (vacía = todo aplicado) y la
imprime al final: un paso saltado no puede pasar por un deploy exitoso en silencio
(molde: tesoreria/init_db.py).
"""
from sqlalchemy import inspect, text

from database import Base, engine
import models.models  # noqa: F401  users / clientes / cotizaciones (destino de las FK)
from .models import FotoArticulo, PortalAcceso, PortalDescargaLog, UsuarioCliente

TABLAS_PROPIAS = (
    UsuarioCliente.__table__, PortalAcceso.__table__,
    FotoArticulo.__table__, PortalDescargaLog.__table__,
)

# tabla -> {columna: definición DDL}
COLUMNAS_NUCLEO = {
    "users": {"tipo_acceso": "VARCHAR(20) NOT NULL DEFAULT 'interno'"},
    "cotizaciones": {"cliente_id": "INT NULL"},
}

# Columnas agregadas a tablas PROPIAS después de su primera versión. En una base nueva
# las crea create_all; en una que ya tenía la tabla, create_all no la altera.
COLUMNAS_PROPIAS = {
    "portal_acceso": {"clave_temporal_desde": "DATETIME NULL"},
}

INDICE_CLIENTE = "ix_cotizaciones_cliente_id"
FK_CLIENTE = "fk_cotizaciones_cliente"


def _columna_existe(conn, tabla: str, columna: str) -> bool:
    return bool(conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"
        ),
        {"t": tabla, "c": columna},
    ).scalar())


def _indice_en_columna(conn, tabla: str, columna: str) -> bool:
    """¿Hay ALGÚN índice que empiece por esta columna? Se busca por columna y no por
    nombre: en una base creada por create_all con el modelo nuevo el índice ya existe
    con el nombre que le puso SQLAlchemy, y por nombre se crearía un segundo.
    (Molde: compras_contab/init_db.py.)"""
    return bool(conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name = :t "
            "AND column_name = :c AND seq_in_index = 1"
        ),
        {"t": tabla, "c": columna},
    ).scalar())


def _fk_en_columna(conn, tabla: str, columna: str, destino: str) -> bool:
    """¿La columna ya tiene una FK hacia `destino`? Por columna, misma razón que el índice."""
    return bool(conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.key_column_usage "
            "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c "
            "AND referenced_table_name = :d"
        ),
        {"t": tabla, "c": columna, "d": destino},
    ).scalar())


def _paso(pendientes: list, nombre: str, sql: str, ya_esta) -> None:
    """Un DDL = una transacción. MySQL hace COMMIT implícito en cada DDL, así que
    agruparlos PARECE atómico y no lo es; separados, el fallo de uno no oculta el éxito
    de los demás y el resumen final dice exactamente cuál quedó pendiente."""
    try:
        with engine.begin() as conn:
            if ya_esta(conn):
                print(f"[portal]   {nombre}: ya existe — ok")
                return
            conn.execute(text(sql))
            print(f"[portal] + {nombre}")
    except Exception as exc:  # noqa: BLE001 — se reporta y se sigue con el resto
        pendientes.append(nombre)
        print(f"[portal] ERROR en {nombre}: {type(exc).__name__}: {exc}")


def main() -> list:
    pendientes: list = []

    print("[portal] Columnas aditivas en tablas del núcleo…")
    for tabla, columnas in COLUMNAS_NUCLEO.items():
        for col, ddl in columnas.items():
            _paso(
                pendientes, f"{tabla}.{col}",
                f"ALTER TABLE {tabla} ADD COLUMN {col} {ddl}",
                lambda conn, t=tabla, c=col: _columna_existe(conn, t, c),
            )
    _paso(
        pendientes, f"índice {INDICE_CLIENTE}",
        f"CREATE INDEX {INDICE_CLIENTE} ON cotizaciones (cliente_id)",
        lambda conn: _indice_en_columna(conn, "cotizaciones", "cliente_id"),
    )
    _paso(
        pendientes, f"FK {FK_CLIENTE}",
        f"ALTER TABLE cotizaciones ADD CONSTRAINT {FK_CLIENTE} "
        "FOREIGN KEY (cliente_id) REFERENCES clientes (id) ON DELETE SET NULL",
        lambda conn: _fk_en_columna(conn, "cotizaciones", "cliente_id", "clientes"),
    )

    print("[portal] Tablas propias (checkfirst=True)…")
    try:
        Base.metadata.create_all(bind=engine, tables=list(TABLAS_PROPIAS), checkfirst=True)
    except Exception as exc:  # noqa: BLE001
        pendientes.append("tablas propias")
        print(f"[portal] ERROR creando tablas: {type(exc).__name__}: {exc}")
    insp = inspect(engine)
    for tabla in TABLAS_PROPIAS:
        existe = insp.has_table(tabla.name)
        print(f"  - {tabla.name}: {'OK' if existe else 'FALTA'}")
        if not existe and "tablas propias" not in pendientes:
            pendientes.append(tabla.name)

    for tabla, columnas in COLUMNAS_PROPIAS.items():
        for col, ddl in columnas.items():
            _paso(
                pendientes, f"{tabla}.{col}",
                f"ALTER TABLE {tabla} ADD COLUMN {col} {ddl}",
                lambda conn, t=tabla, c=col: _columna_existe(conn, t, c),
            )

    if pendientes:
        print("\n" + "!" * 78)
        print("[portal] QUEDARON PASOS PENDIENTES: " + ", ".join(pendientes))
        print("[portal] NO reinicies el backend hasta que este script salga limpio:")
        print("[portal] sin users.tipo_acceso el LOGIN responde 1054 y nadie entra.")
        print("!" * 78)
    else:
        print("[portal] completado — esquema al día")
    return pendientes


if __name__ == "__main__":
    raise SystemExit(1 if main() else 0)
