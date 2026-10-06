"""
Router de Clientes — CRUD básico.
"""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from models.models import Cliente, User

router = APIRouter(prefix="/clientes", tags=["clientes"])


# ── Portal de Clientes: la ficha es el ANCLA del aislamiento ──────────────────────────
# cotizaciones.cliente_id y usuario_cliente.cliente_id apuntan a esta tabla: cada cuenta
# de un cliente ve las ventas de SU ficha. Por eso crear, editar o borrar una ficha
# vuelve a calcular el vínculo de las ventas, y cambiarle el RUT o borrarla se rechaza
# mientras tenga cuentas del portal. Los imports de `portal` son locales, pero la
# dependencia es dura: van sin try y, si el portal falla, la ficha no se guarda.

def _resincronizar_ventas(db: Session) -> None:
    """Pone al día el vínculo venta ↔ cliente de todo, DENTRO de la transacción que está
    guardando la ficha. NO hace commit: lo hace quien llama, una sola vez.

    Sin esto, la ficha recién creada no tendría ventas hasta que alguien apretara
    «Vincular ventas», una ficha cuyo RUT se corrigió conservaría las del RUT viejo, y
    borrar la ficha duplicada de un RUT ambiguo no le devolvería las ventas a la que
    queda. Va en la misma transacción a propósito: con dos commits separados, si este
    paso fallaba el cambio de RUT quedaba aplicado con las ventas viejas todavía
    colgando — el vínculo fallaba ABIERTO. Así, o se guardan las dos cosas o ninguna."""
    from portal.service import vincular_historico
    db.flush()
    vincular_historico(db, aplicar=True)


def _rechazar_cambio_de_rut_con_cuentas(db: Session, ficha: Cliente, rut_nuevo: Optional[str]) -> None:
    """409 si se intenta cambiar el RUT de una ficha que tiene cuentas del portal.

    Cambiar el RUT es cambiar DE QUIÉN es la ficha: los contactos que hoy entran con sus
    cuentas pasarían a ver las ventas de otro contribuyente. Corregir un RUT mal digitado
    sigue siendo posible, pero primero hay que eliminar esas cuentas a conciencia."""
    from portal.models import UsuarioCliente
    from portal.service import rut_canonico
    if rut_nuevo is None or rut_canonico(rut_nuevo) == rut_canonico(ficha.rut):
        return
    # Con la ficha tomada se espera a un alta de cuenta en vuelo (su fila de usuario_cliente
    # todavía sin confirmar) y recién entonces se mira si tiene cuentas.
    db.query(Cliente.id).filter(Cliente.id == ficha.id).with_for_update().first()
    if db.query(UsuarioCliente).filter(UsuarioCliente.cliente_id == ficha.id).first():
        raise HTTPException(
            409,
            "No se puede cambiar el RUT: este cliente tiene cuentas del Portal de Clientes. "
            "Elimínalas primero en Admin → Cuentas del portal.",
        )


class ClienteCreate(BaseModel):
    rut: str
    nombre: Optional[str] = None
    contacto: Optional[str] = None
    email: Optional[str] = None
    telefono: Optional[str] = None
    direccion: Optional[str] = None
    ciudad: Optional[str] = None
    giro: Optional[str] = None


class ClienteUpdate(BaseModel):
    rut: Optional[str] = None
    nombre: Optional[str] = None
    contacto: Optional[str] = None
    email: Optional[str] = None
    telefono: Optional[str] = None
    direccion: Optional[str] = None
    ciudad: Optional[str] = None
    giro: Optional[str] = None


@router.get("/")
def listar_clientes(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    clientes = db.query(Cliente).order_by(Cliente.nombre).all()
    return [
        {
            "id": c.id,
            "rut": c.rut,
            "nombre": c.nombre or "",
            "contacto": c.contacto or "",
            "email": c.email or "",
            "telefono": c.telefono or "",
            "direccion": c.direccion or "",
            "ciudad": c.ciudad or "",
            "giro": c.giro or "",
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in clientes
    ]


@router.post("/", status_code=201)
def crear_cliente(
    body: ClienteCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    existing = db.query(Cliente).filter(Cliente.rut == body.rut).first()
    if existing:
        raise HTTPException(400, f"Ya existe un cliente con RUT {body.rut}")
    c = Cliente(**body.dict())
    db.add(c)
    _resincronizar_ventas(db)
    db.commit()
    db.refresh(c)
    return {"id": c.id, "rut": c.rut}



@router.get("/by-rut/{rut}")
def buscar_por_rut(
    rut: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    c = db.query(Cliente).filter(Cliente.rut == rut).first()
    if not c:
        return {"found": False}
    return {
        "found": True,
        "id": c.id,
        "rut": c.rut,
        "nombre": c.nombre or "",
        "contacto": c.contacto or "",
        "email": c.email or "",
        "telefono": c.telefono or "",
        "direccion": c.direccion or "",
        "ciudad": c.ciudad or "",
        "giro": c.giro or "",
    }


@router.post("/upsert")
def upsert_cliente(
    body: ClienteCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create or update a cliente by RUT."""
    if not body.rut:
        raise HTTPException(400, "RUT requerido")
    c = db.query(Cliente).filter(Cliente.rut == body.rut).first()
    if c:
        for k, v in body.dict(exclude_none=True).items():
            setattr(c, k, v)
    else:
        c = Cliente(**body.dict())
        db.add(c)
    _resincronizar_ventas(db)
    db.commit()
    db.refresh(c)
    return {"id": c.id, "rut": c.rut}


@router.put("/{cliente_id}")
def actualizar_cliente(
    cliente_id: int,
    body: ClienteUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    c = db.query(Cliente).filter(Cliente.id == cliente_id).first()
    if not c:
        raise HTTPException(404, "Cliente no encontrado")
    _rechazar_cambio_de_rut_con_cuentas(db, c, body.rut)
    for k, v in body.dict(exclude_none=True).items():
        setattr(c, k, v)
    _resincronizar_ventas(db)
    db.commit()
    return {"ok": True}


@router.delete('/{cliente_id}', status_code=204)
def eliminar_cliente(
    cliente_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    c = db.query(Cliente).filter(Cliente.id == cliente_id).first()
    if not c:
        raise HTTPException(404, 'Cliente no encontrado')
    db.delete(c)
    try:
        db.flush()
    except IntegrityError:
        # Las ventas de la ficha se desvinculan solas (FK ON DELETE SET NULL: desaparecen
        # del portal). Lo que la base NO deja borrar es una ficha con cuentas del portal
        # colgando (usuario_cliente): quedarían cuentas vivas sin cliente. Se dice por
        # qué en vez de responder un 500.
        db.rollback()
        raise HTTPException(
            409,
            'No se puede eliminar: el cliente tiene cuentas del Portal de Clientes. '
            'Elimínalas primero en Admin → Cuentas del portal.',
        )
    _resincronizar_ventas(db)
    db.commit()
