"""Migración: endurecimiento de `monza_notificaciones` para el barrido diario de alertas.

`Base.metadata.create_all` NO agrega columnas a una tabla que ya existe, y
`monza_notificaciones` existe en producción desde el primer día de MonzaParts. Este script
agrega, idempotente y portable (MySQL/MariaDB):

  monza_notificaciones.destinatario_rol VARCHAR(50)  NULL — a quién le toca actuar
  monza_notificaciones.severidad        VARCHAR(20)  NULL — info | warning | critical
  monza_notificaciones.regla            VARCHAR(150) NULL — identificador de la regla

QUÉ SE ROMPE SI NO SE CORRE
El backend arranca igual, pero en cuanto el job de las 06:00 (o cualquier notificación de
Monza) intente escribir, MySQL responde "Unknown column 'regla' in field list": el INSERT
falla, `monza_notif.crear_notif` lo registra en el log y **NINGUNA notificación de
MonzaParts se crea** — ni las nuevas del barrido diario ni las instantáneas que hoy
funcionan (venta cerrada, despacho confirmado, embarque en tránsito, reclamos de bodega).

Las tres columnas son NULLABLE y sin backfill: las notificaciones históricas quedan con
NULL, que es exactamente lo que corresponde (nacieron sin regla y sin rol).

Uso (desde backend/, con el venv activo):
    python -m migrations.monza_notif_alertas
"""
from sqlalchemy import text

from database import engine

TABLA = "monza_notificaciones"
# El orden importa poco, pero el DDL sí: tiene que coincidir con monza_models.py:428 para
# que una BD migrada y una BD fresca (create_all) queden con el MISMO esquema.
COLUMNS = {
    "destinatario_rol": "VARCHAR(50) NULL",
    "severidad": "VARCHAR(20) NULL",
    "regla": "VARCHAR(150) NULL",
}


def _tabla_existe(conn, tabla: str) -> bool:
    row = conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = :t"
        ),
        {"t": tabla},
    ).scalar()
    return bool(row)


def _columna_existe(conn, tabla: str, columna: str) -> bool:
    row = conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"
        ),
        {"t": tabla, "c": columna},
    ).scalar()
    return bool(row)


def run() -> None:
    with engine.begin() as conn:
        if not _tabla_existe(conn, TABLA):
            # BD nueva: el create_all del arranque la crea ya con las 3 columnas del
            # modelo. No es un error, no hay nada que alterar.
            print(f"[migracion] {TABLA} no existe aún — la creará el create_all del arranque")
            return
        for col, ddl in COLUMNS.items():
            if _columna_existe(conn, TABLA, col):
                print(f"[migracion] {TABLA}.{col} ya existe — ok")
                continue
            conn.execute(text(f"ALTER TABLE {TABLA} ADD COLUMN {col} {ddl}"))
            print(f"[migracion] {TABLA}.{col} agregada")
    print("[migracion] completada")


if __name__ == "__main__":
    run()
