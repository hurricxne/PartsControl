"""
Router de Clientes — CRUD básico.
"""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from models.models import Cliente, User

router = APIRouter(prefix="/clientes", tags=["clientes"])


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
    for k, v in body.dict(exclude_none=True).items():
        setattr(c, k, v)
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
    db.commit()
