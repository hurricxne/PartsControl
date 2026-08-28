"""Índice UNIQUE en los correlativos de OC de proveedor y de embarques (MonzaParts).

EL PROBLEMA QUE CIERRA
    `_gen_numero_ocp` y `_gen_numero_emb` leen el último número y le suman 1 sin lock ni
    reintento — el mismo patrón que ya causó pérdidas de leads y de cotizaciones. Pero
    acá el desenlace es PEOR: como esas dos tablas no tienen índice único en `numero`, la
    carrera no falla, DUPLICA el número en silencio. No hay error, no hay 500, no hay
    log. A partir de ahí «OCP-2026-0007» deja de identificar una compra y pasa a
    identificar dos, con el proveedor, la recepción de bodega y el costeo de CxP colgando
    de un número ambiguo.

QUÉ HACE ESTE ÍNDICE, Y QUÉ NO
    Convierte una corrupción silenciosa en un error ruidoso: con el UNIQUE puesto, la
    segunda inserción simultánea falla con un 500 en vez de crear el gemelo. Es un
    cambio de fallo, no una cura — el reintento de estos dos generadores queda como deuda
    registrada (sus routers son código del programador y el dueño no autorizó tocarlos).
    Se elige así deliberadamente: entre perder una operación de forma visible y corromper
    la numeración sin que nadie se entere, la casa prefiere lo primero.

FAIL-CLOSED
    Si ya existen números repetidos, el script NO crea el índice y lo dice: crearlo
    fallaría a medias y hay que decidir a mano cuál duplicado se renumera. Verificado el
    2026-08-27: 0 duplicados en las dos tablas.

Uso (desde backend/, con el venv activo):
    python -m migrations.monza_unique_correlativos
"""
from sqlalchemy import text

from database import SessionLocal

_TABLAS = (
    ("monza_oc_proveedor", "uq_monza_ocp_numero"),
    ("monza_embarques", "uq_monza_emb_numero"),
)


def main() -> int:
    db = SessionLocal()
    problemas = 0
    try:
        for tabla, indice in _TABLAS:
            existe_tabla = db.execute(text(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = :t"
            ), {"t": tabla}).scalar()
            if not existe_tabla:
                print(f"·  {tabla}: la tabla no existe todavía — nada que hacer")
                continue

            ya_esta = db.execute(text(
                "SELECT COUNT(*) FROM information_schema.statistics "
                "WHERE table_schema = DATABASE() AND table_name = :t AND index_name = :i"
            ), {"t": tabla, "i": indice}).scalar()
            if ya_esta:
                print(f"✓  {tabla}: {indice} ya existe")
                continue

            duplicados = db.execute(text(
                f"SELECT numero, COUNT(*) c FROM {tabla} "
                "WHERE numero IS NOT NULL GROUP BY numero HAVING c > 1"
            )).fetchall()
            if duplicados:
                # FAIL-CLOSED: no se crea el índice ni se toca ningún dato. Renumerar
                # documentos ya emitidos es una decisión de negocio, no de un script.
                print(f"✗  {tabla}: NO se creó {indice} — ya hay números repetidos:")
                for numero, cuantos in duplicados:
                    print(f"     {numero} aparece {cuantos} veces")
                print("   Renumera esos documentos y vuelve a correr este script.")
                problemas += 1
                continue

            db.execute(text(f"ALTER TABLE {tabla} ADD UNIQUE INDEX {indice} (numero)"))
            db.commit()
            print(f"✓  {tabla}: {indice} creado")
    finally:
        db.close()
    return 1 if problemas else 0


if __name__ == "__main__":
    raise SystemExit(main())
