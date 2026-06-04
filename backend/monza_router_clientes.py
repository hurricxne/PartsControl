"""
Gestión de clientes MonzaParts.
CRUD completo: listar, crear, actualizar, desactivar.
Prefix: /api/monza/clientes
"""
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import or_
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

from database import get_db
from auth import get_current_user
from monza_models import MonzaCliente, MonzaLead

router = APIRouter(prefix="/api/monza/clientes", tags=["monza-clientes"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class ClienteIn(BaseModel):
    nombre: str
    rut: Optional[str] = None
    telefono: Optional[str] = None
    email: Optional[str] = None
    vehiculos: Optional[list] = None
    etiquetas: Optional[list] = None


class ClienteUpdate(BaseModel):
    nombre: Optional[str] = None
    rut: Optional[str] = None
    telefono: Optional[str] = None
    email: Optional[str] = None
    vehiculos: Optional[list] = None
    etiquetas: Optional[list] = None
    is_active: Optional[bool] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cliente_dict(c: MonzaCliente, include_leads: bool = False) -> dict:
    d = {
        "id": c.id,
        "nombre": c.nombre,
        "rut": c.rut,
        "telefono": c.telefono,
        "email": c.email,
        "vehiculos": c.vehiculos or [],
        "etiquetas": c.etiquetas or [],
        "ltv": float(c.ltv or 0),
        "leads_total": c.leads_total or 0,
        "vendidos_total": c.vendidos_total or 0,
        "is_active": c.is_active,
        "fecha_creacion": c.fecha_creacion.isoformat() if c.fecha_creacion else None,
        "fecha_actualizacion": c.fecha_actualizacion.isoformat() if c.fecha_actualizacion else None,
    }
    if include_leads:
        d["leads"] = [
            {
                "id": l.id,
                "numero": l.numero,
                "estado": l.estado,
                "vehiculo": l.vehiculo,
                "total_estimado": float(l.total_estimado or 0),
                "fecha_creacion": l.fecha_creacion.isoformat(),
            }
            for l in (c.leads or [])
        ]
    return d


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("")
def list_clientes(
    q: str = Query("", description="Búsqueda por nombre, teléfono, email, RUT"),
    activos: bool = Query(True),
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Lista de clientes con búsqueda y paginación."""
    query = db.query(MonzaCliente)
    if activos:
        query = query.filter(MonzaCliente.is_active == True)

    if q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(or_(
            MonzaCliente.nombre.ilike(like),
            MonzaCliente.telefono.ilike(like),
            MonzaCliente.email.ilike(like),
            MonzaCliente.rut.ilike(like),
        ))

    total = query.count()
    clientes = (
        query
        .order_by(MonzaCliente.nombre)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_cliente_dict(c) for c in clientes],
    }


@router.post("", status_code=201)
def create_cliente(
    body: ClienteIn,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Crea un cliente nuevo. Si ya existe RUT o teléfono, retorna el existente."""
    existing = None
    if body.rut:
        existing = db.query(MonzaCliente).filter(MonzaCliente.rut == body.rut).first()
    if not existing and body.telefono:
        existing = db.query(MonzaCliente).filter(MonzaCliente.telefono == body.telefono).first()

    if existing:
        # Actualizar datos si llegan campos nuevos
        if body.nombre: existing.nombre = body.nombre
        if body.email and not existing.email: existing.email = body.email
        if body.vehiculos is not None: existing.vehiculos = body.vehiculos
        existing.is_active = True
        db.commit()
        db.refresh(existing)
        return _cliente_dict(existing)

    cliente = MonzaCliente(
        nombre=body.nombre,
        rut=body.rut,
        telefono=body.telefono,
        email=body.email,
        vehiculos=body.vehiculos or [],
        etiquetas=body.etiquetas or [],
        is_active=True,
    )
    db.add(cliente)
    db.commit()
    db.refresh(cliente)
    return _cliente_dict(cliente)


@router.get("/{cliente_id}")
def get_cliente(
    cliente_id: int,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Detalle completo del cliente con historial de leads."""
    c = db.query(MonzaCliente).filter(MonzaCliente.id == cliente_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    return _cliente_dict(c, include_leads=True)


@router.patch("/{cliente_id}")
def update_cliente(
    cliente_id: int,
    body: ClienteUpdate,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Actualiza datos del cliente."""
    c = db.query(MonzaCliente).filter(MonzaCliente.id == cliente_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")

    for field, value in body.model_dump(exclude_none=True).items():
        setattr(c, field, value)
    c.fecha_actualizacion = datetime.utcnow()
    db.commit()
    db.refresh(c)
    return _cliente_dict(c)


@router.delete("/{cliente_id}", status_code=204)
def delete_cliente(
    cliente_id: int,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Desactiva un cliente (soft delete). No elimina leads asociados."""
    c = db.query(MonzaCliente).filter(MonzaCliente.id == cliente_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    c.is_active = False
    c.fecha_actualizacion = datetime.utcnow()
    db.commit()
