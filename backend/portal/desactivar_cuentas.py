"""ROLLBACK del Portal de Clientes: desactiva toda cuenta que no sea del personal.

Uso (desde backend/, con el venv activo):
    python -m portal.desactivar_cuentas            # muestra cuántas hay, no escribe
    python -m portal.desactivar_cuentas --aplicar  # las desactiva (is_active = 0)

POR QUÉ EXISTE — LEER ANTES DE REVERTIR EL CÓDIGO DE LA FASE 1
    Las cuentas de los clientes viven en la misma tabla `users`, activas y con empresa
    'mineria'. Lo ÚNICO que las mantiene fuera del ERP es el corte de
    auth.get_current_user, que es código de la Fase 1. Si ese código se revierte
    (git revert, o desplegar un paquete anterior) y las cuentas siguen activas, el
    get_current_user viejo no mira `tipo_acceso`: cada cuenta de cliente pasa a ser un
    usuario interno con acceso a cotizaciones, compras y todo router que solo pide sesión.

    Por eso el rollback tiene ORDEN: primero este script con --aplicar, después el
    código. Un `is_active = 0` lo entiende también el código viejo.

QUÉ DESACTIVA
    Todo lo que ese corte mantiene fuera del ERP: lo que NO es 'interno', y no «las que
    dicen 'portal'». Con el código viejo, una cuenta activa con un tipo_acceso
    desconocido (escrito a mano en la base) es personal igual que una portal. Esas
    cuentas no aparecen en Cuentas del portal: para volver a habilitarlas hay que
    corregir su tipo_acceso en la base.

IDEMPOTENTE
    Se puede correr las veces que sea. No borra nada: para volver a habilitar una cuenta
    portal (con la Fase 1 otra vez desplegada), Admin → Cuentas del portal → Reactivar.
"""
import sys

from sqlalchemy import text

from database import engine

# Toda cuenta que auth.get_current_user mantiene fuera del ERP: es el complemento de ese
# corte y no «las que dicen 'portal'». La comparación es la de la base (no distingue
# mayúsculas): una cuenta 'Interno' escrita a mano no se desactiva, y con el código
# viejo esa cuenta es personal. COALESCE: un NULL (si la columna quedara anulable en
# alguna base) también se cierra.
_FUERA_DEL_ERP = "COALESCE(tipo_acceso, '') <> 'interno'"


def resumen(conn) -> list:
    """[(tipo_acceso, cuentas, activas)] de todo lo que se desactivaría."""
    return [
        (tipo, int(cuentas), int(activas))
        for tipo, cuentas, activas in conn.execute(text(
            "SELECT tipo_acceso, COUNT(*), COALESCE(SUM(is_active = 1), 0) "
            f"FROM users WHERE {_FUERA_DEL_ERP} GROUP BY tipo_acceso"
        )).fetchall()
    ]


def desactivar(conn) -> None:
    conn.execute(text(f"UPDATE users SET is_active = 0 WHERE {_FUERA_DEL_ERP}"))


def main(argv: list) -> int:
    aplicar = "--aplicar" in argv
    with engine.begin() as conn:
        filas = resumen(conn)
        activas = sum(a for _, _, a in filas)
        for tipo, cuentas, activas_del_tipo in filas:
            print(f"[portal] tipo_acceso {tipo!r}: {cuentas} cuenta(s) · activas: {activas_del_tipo}")
            if tipo != "portal":
                print(f"[portal] ATENCIÓN: {cuentas} cuenta(s) con tipo_acceso desconocido {tipo!r}: el ERP "
                      "ya les niega la entrada y también se desactivan. No aparecen en Cuentas del "
                      "portal: para volver a habilitarlas hay que corregir su tipo_acceso en la base.")
        print(f"[portal] cuentas fuera del ERP: {sum(c for _, c, _ in filas)} · activas: {activas}")
        if not aplicar:
            if activas:
                print("[portal] ENSAYO — no se escribió nada. Para desactivarlas: "
                      "python -m portal.desactivar_cuentas --aplicar")
            return 0
        desactivar(conn)
        print(f"[portal] {activas} cuenta(s) desactivada(s). Ya se puede revertir el código.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
