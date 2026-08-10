"""Crea la tabla `monza_cotizacion_cierre` (historial de versiones del cierre de venta).

POR QUÉ HACE FALTA UN SCRIPT SI `create_all` LA CREA SOLA
---------------------------------------------------------
La crea sola, sí — pero al REINICIAR, o sea DESPUÉS del paso obligatorio que audita el
esquema (`deploy/audit_schema.py`, §1.e del checklist). En el deploy que estrena la tabla,
ese auditor la encuentra ausente, la cuenta como PROBLEMA, sale con rc=1 y cierra con
«CORRER LAS MIGRACIONES ANTES DE REINICIAR» — una orden imposible si la migración no
existe. Un paso que sale rojo por diseño le enseña al operador a ignorar el rojo, que es
justo lo que ese auditor existe para impedir.

La tabla es del NÚCLEO Monza (`monza_models.py`), así que se crea aunque
`MONZA_CONTAB_ENABLED=false`: la escribe el cierre de venta de MonzaParts, que se monta
fuera del gate.

No es una tabla de plata: es auditoría del cierre (qué OC, qué fecha, qué % de adelanto
tenía la venta cada vez que se cerró). Sin ella, un re-cierre pisa los datos anteriores
sin dejar huella.

Idempotente: si la tabla ya existe, no hace nada.
Correr desde backend/:  python -m migrations.monza_cotizacion_cierre
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import text  # noqa: E402

from database import Base, engine  # noqa: E402
import monza_models  # noqa: E402,F401  (registra monza_cotizaciones + la tabla nueva)

TABLA = "monza_cotizacion_cierre"
# FKs: monza_cotizaciones (CASCADE) y users (SET NULL). Las dos son del núcleo y existen
# en cualquier base inicializada; se comprueban igual para que un orden equivocado dé un
# mensaje legible en vez de un errno 150 de MySQL.
REQUISITOS = ("monza_cotizaciones", "users")


def _existe(conn, tabla: str) -> bool:
    return bool(conn.execute(
        text("SELECT COUNT(*) FROM information_schema.tables "
             "WHERE table_schema = DATABASE() AND table_name = :t"),
        {"t": tabla},
    ).scalar())


def main() -> None:
    with engine.connect() as conn:
        if _existe(conn, TABLA):
            print(f"  = {TABLA}: ya existe")
            print("OK")
            return
        faltan = [t for t in REQUISITOS if not _existe(conn, t)]
    if faltan:
        raise SystemExit(
            f"[{TABLA}] faltan las tablas de las que dependen sus FKs: "
            f"{', '.join(faltan)}. No se creó nada."
        )

    Base.metadata.create_all(bind=engine, tables=[Base.metadata.tables[TABLA]],
                             checkfirst=True)
    with engine.connect() as conn:
        print(f"  + {TABLA}: {'creada' if _existe(conn, TABLA) else 'NO SE CREÓ'}")
    print("OK")


if __name__ == "__main__":
    main()
