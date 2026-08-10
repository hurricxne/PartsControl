"""Crea las 7 tablas del Libro de compras del SII de Grupo AM (standalone, idempotente).

Uso (desde backend/):
    python -m wasabil_compras.init_db

POR QUÉ EXISTE ESTE SCRIPT SI `create_all` YA LAS CREABA SOLO
------------------------------------------------------------
Las creaba, sí — pero AL REINICIAR, o sea DESPUÉS del paso obligatorio del deploy que
audita el esquema (`deploy/audit_schema.py`, §1.e del checklist). Resultado: en el primer
deploy de este módulo el auditor encontraba 7 tablas ausentes, las contaba como PROBLEMA,
salía con rc=1 y cerraba con «CORRER LAS MIGRACIONES ANTES DE REINICIAR» — una orden
imposible, porque no había ninguna migración que correr. Un checklist que sale rojo por
diseño le enseña al operador a ignorar el rojo, que es exactamente la falla que ese
auditor existe para impedir (incidente 1054 de julio 2026).

Con este script el paso queda verde por construcción y el rojo vuelve a significar una
sola cosa: falta algo de verdad.

BONUS QUE NO ES ADORNO: si una FK contra `conc_movimiento` fallara (errno 150 por
charset, motor o tipo de columna), hoy reventaría el `create_all` del arranque y el
backend quedaría en crash-loop. Acá el error aparece durante las migraciones, con el
operador mirando la pantalla y la aplicación todavía arriba.

ORDEN: después de `tesoreria.init_db` — `sii_libro_match` y `sii_match_etiqueta_mov`
tienen FK a `conc_movimiento`, que es de Tesorería. Si falta, el script corta con un
mensaje que lo dice, en vez de dejar un errno 150 sin traducir.

Es DUPLICADO deliberado de `monza_wasabil_compras/init_db.py`: las dos marcas son
productos separados y no comparten código.
"""
from sqlalchemy import text

from database import Base, engine
from wasabil_compras import models as _models  # noqa: F401  (registra las 7 tablas)

# Las 7 tablas del módulo, en orden de dependencia (doc y sync_run antes que match).
TABLAS = (
    "sii_libro_doc",
    "sii_libro_sync_run",
    "sii_libro_regla_rut",
    "sii_libro_match",
    "sii_match_run",
    "sii_match_etiqueta_mov",
    "sii_match_config",
)

# Tablas de OTROS módulos a las que apuntan las FKs. Se comprueban antes de crear nada
# para que un orden equivocado del deploy dé un mensaje en castellano y no un errno 150.
REQUISITOS = {
    "users": "núcleo Grupo AM (models/models.py)",
    "conc_movimiento": "Tesorería GA — correr antes `python -m tesoreria.init_db`",
}


def _tabla_existe(conn, tabla: str) -> bool:
    return bool(conn.execute(
        text("SELECT COUNT(*) FROM information_schema.tables "
             "WHERE table_schema = DATABASE() AND table_name = :t"),
        {"t": tabla},
    ).scalar())


def main() -> None:
    with engine.connect() as conn:
        faltan = [f"  · {t} — {para_que}"
                  for t, para_que in REQUISITOS.items() if not _tabla_existe(conn, t)]
    if faltan:
        raise SystemExit(
            "[wasabil_compras] FALTAN tablas de las que dependen las FKs de este "
            "módulo:\n" + "\n".join(faltan) +
            "\nNo se creó nada. Corregir el ORDEN del deploy y volver a correr."
        )

    print("[wasabil_compras] Creando las tablas del libro SII que falten "
          "(checkfirst=True)…")
    Base.metadata.create_all(
        bind=engine,
        tables=[Base.metadata.tables[t] for t in TABLAS],
        checkfirst=True,
    )

    with engine.connect() as conn:
        for t in TABLAS:
            print(f"  - {t}: {'OK' if _tabla_existe(conn, t) else 'FALTA'}")

    print("[wasabil_compras] Listo (sin migraciones pendientes).")


if __name__ == "__main__":
    main()
