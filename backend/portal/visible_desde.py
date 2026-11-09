"""Corte de visibilidad del Portal de Clientes por ficha (clientes.portal_visible_desde).

Con corte, el cliente ve solo las ventas ingresadas al sistema (oc_cliente.created_at)
desde ese momento; sin corte (NULL), todas. Lo aplica portal/candado.py.
Pedido de HEPI (2026-11-09): «solo desde la última orden».

Sin --aplicar solo muestra qué pasaría. Desde backend/, con el venv:
    python -m portal.visible_desde                                  # fichas con corte
    python -m portal.visible_desde --cliente 1 --desde-oc 64972     # ver el efecto
    python -m portal.visible_desde --cliente 1 --desde-oc 64972 --aplicar
    python -m portal.visible_desde --cliente 1 --quitar --aplicar   # vuelve a ver todo

--desde-oc toma el momento en que se ingresó esa OC del cliente: ella y lo posterior
quedan visibles. Se usa la fecha de ingreso y no la fecha de la OC (texto que se escribe
a mano: una venta nueva con una OC atrasada quedaría oculta).
"""
import argparse
import sys

from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from database import SessionLocal
from models.models import Cliente, Cotizacion, OcCliente


def _ventas_visibles(db, cliente_id: int, desde) -> int:
    """Ventas que vería el cliente con ese corte: una por venta, por su primera OC (la
    misma regla que portal/ordenes.base)."""
    otra = aliased(OcCliente)
    primera = select(func.min(otra.id)).where(otra.cotizacion_id == OcCliente.cotizacion_id).scalar_subquery()
    q = (db.query(func.count(OcCliente.id))
         .join(Cotizacion, Cotizacion.id == OcCliente.cotizacion_id)
         .filter(Cotizacion.cliente_id == cliente_id, OcCliente.id == primera))
    if desde is not None:
        q = q.filter(OcCliente.created_at >= desde)
    return q.scalar()


def _listar(db) -> None:
    fichas = db.query(Cliente).filter(Cliente.portal_visible_desde.isnot(None)).order_by(Cliente.id).all()
    if not fichas:
        print("Ninguna ficha tiene corte: todos los clientes ven todas sus ventas.")
    for f in fichas:
        print(f"  ficha {f.id} · {f.nombre} · desde {f.portal_visible_desde} · "
              f"{_ventas_visibles(db, f.id, f.portal_visible_desde)} de {_ventas_visibles(db, f.id, None)} ventas visibles")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cliente", type=int, help="id de la ficha (clientes.id)")
    accion = p.add_mutually_exclusive_group()
    accion.add_argument("--desde-oc", help="N° de OC del cliente desde la que se muestra")
    accion.add_argument("--quitar", action="store_true", help="quita el corte: vuelve a ver todo")
    p.add_argument("--aplicar", action="store_true", help="escribe el cambio (sin esto, solo muestra)")
    a = p.parse_args(argv)

    db = SessionLocal()
    try:
        if a.cliente is None:
            _listar(db)
            return 0
        if not (a.desde_oc or a.quitar):
            p.error("con --cliente va --desde-oc N o --quitar")
        ficha = db.get(Cliente, a.cliente)
        if ficha is None:
            print(f"ERROR: no existe la ficha {a.cliente}")
            return 1
        if a.quitar:
            nuevo = None
        else:
            nuevo = (db.query(func.min(OcCliente.created_at))
                     .join(Cotizacion, Cotizacion.id == OcCliente.cotizacion_id)
                     .filter(Cotizacion.cliente_id == ficha.id, OcCliente.numero_oc == a.desde_oc.strip())
                     .scalar())
            if nuevo is None:
                print(f"ERROR: la ficha {ficha.id} ({ficha.nombre}) no tiene ninguna venta con la OC {a.desde_oc!r}")
                return 1
        total = _ventas_visibles(db, ficha.id, None)
        print(f"Ficha {ficha.id} · {ficha.nombre}")
        print(f"  corte actual: {ficha.portal_visible_desde or 'ninguno'} → "
              f"{_ventas_visibles(db, ficha.id, ficha.portal_visible_desde)} de {total} ventas visibles")
        print(f"  corte nuevo:  {nuevo or 'ninguno'} → {_ventas_visibles(db, ficha.id, nuevo)} de {total} ventas visibles")
        if not a.aplicar:
            print("Sin --aplicar: no se cambió nada.")
            return 0
        ficha.portal_visible_desde = nuevo
        db.commit()
        print("Aplicado. Las cuentas del cliente lo ven en su próxima consulta (no hace falta reiniciar).")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
