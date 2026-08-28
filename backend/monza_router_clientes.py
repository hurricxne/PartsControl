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
from empresa_guard import require_empresa
from monza_rut import buscar_ficha_por_rut, parece_rut, rut_identidad, rut_norm_py, rut_norm_sql
from monza_telefono import buscar_ficha_por_telefono, telefono_identidad, telefono_norm_sql
from monza_models import MonzaCliente, MonzaLead

# CANDADO DE EMPRESA a nivel de ROUTER (2026-08-22): el CRM de MonzaParts estaba
# abierto a cualquier usuario autenticado —incluidos los de minería— mientras
# Despachos, Bodega y el PATCH de Cotizaciones ya lo tenían desde la auditoría F6.
# Router COMPLETO (lecturas incluidas): candar solo las escrituras deja la lectura
# de los datos del cliente como puerta del costado. Ver monza_router_leads.py.
router = APIRouter(
    prefix="/api/monza/clientes",
    tags=["monza-clientes"],
    dependencies=[Depends(require_empresa("automotriz"))],
)


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
                # Tercera puerta del mismo bug: una fecha NULL —lo que deja la
                # migración desde Postgres cuando no entiende el dato— tumbaba la ficha
                # del cliente con un 500, igual que tumbaba la lista y el detalle del
                # lead antes de arreglarlos.
                "fecha_creacion": l.fecha_creacion.isoformat() if l.fecha_creacion else None,
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
        criterios = [
            MonzaCliente.nombre.ilike(like),
            MonzaCliente.telefono.ilike(like),
            MonzaCliente.email.ilike(like),
            MonzaCliente.rut.ilike(like),
        ]
        # El RUT se guarda como lo digitó quien creó la ficha ('76.000.000-0' o
        # '76000000-0'), así que el ilike crudo solo encuentra al que teclea el MISMO
        # formato. Se compara normalizando los dos lados (ver monza_rut), y solo cuando
        # el término parece un RUT: si no, buscar «MARIA» activaría esta rama.
        if parece_rut(q):
            criterios.append(rut_norm_sql(MonzaCliente.rut).like(f"%{rut_norm_py(q)}%"))
        # Teléfono con el mismo tratamiento bilateral (ver monza_telefono): la lista de
        # clientes es donde el operador confirma si la ficha ya existe antes de crearla.
        llave_tel = telefono_identidad(q)
        if llave_tel:
            criterios.append(telefono_norm_sql(MonzaCliente.telefono).like(f"%{llave_tel}%"))
        query = query.filter(or_(*criterios))

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
    """Crea un cliente nuevo. Si ya existe RUT o teléfono, DEVUELVE el existente.

    DOS ARREGLOS (2026-08-22), los dos sobre el mismo dedupe:

    1. YA NO RENOMBRA. El bloque de «actualizar datos» pisaba el nombre de la ficha
       —compartida por todos los leads, ventas y facturas del cliente, y receptora del
       DTE 33— con lo que el operador acababa de tipear. Como `nombre` es obligatorio,
       eso pasaba SIEMPRE que había dedupe: crear «Juan Pérez» sobre el RUT de
       «Comercial JP SpA» renombraba la empresa entera, en silencio. Ahora la regla es
       COMPLETAR, jamás pisar: un campo vacío se llena, uno con dato se respeta.

    2. DEDUPEA POR RUT CANÓNICO. La comparación era literal, así que «76.000.000-0» y
       «76000000-0» creaban DOS fichas del mismo cliente. Arreglar solo el buscador
       (que sí normaliza) habría dejado la costura: se encuentra la ficha, pero el POST
       sigue duplicando.

    La respuesta lleva `reutilizado` para que la pantalla pueda avisar «se vinculó al
    cliente existente X» en vez de decir que creó uno nuevo.
    """
    # `buscar_ficha_por_rut` decide con la llave de IDENTIDAD (monza_rut): un RUT
    # malformado o de pura puntuación NO dedupea con nadie — comparando la forma laxa,
    # un '-' mal tipeado colapsaba a "" y enganchaba con la ficha de un tercero.
    existing = buscar_ficha_por_rut(db, MonzaCliente, body.rut)
    if not existing and body.telefono:
        # `buscar_ficha_por_telefono` (monza_telefono) en vez de la comparación literal.
        # Cierra el hallazgo CRÍTICO del equipo de testing: el teléfono FUSIONABA fichas
        # de contribuyentes distintos —un número compartido, o un '-' de relleno tecleado
        # en las dos— y el RUT recién digitado se descartaba en silencio, así que la
        # factura 33 salía al RUT del otro. Ahora el número no puede unir dos fichas cuyos
        # RUT se contradicen, y un teléfono que no identifica ('-', '0', '2342') no
        # dedupea con nadie.
        existing = buscar_ficha_por_telefono(db, MonzaCliente, body.telefono, body.rut)

    if existing:
        # COMPLETAR lo que falta; NUNCA pisar lo que ya está (el nombre jamás se toca).
        # Solo un RUT que IDENTIFICA se escribe en la ficha: si el match entró por
        # teléfono, un '-' basura le pisaba el campo a un cliente real (y '-' es
        # truthy, así que apagaba el guard de «completa el RUT» del módulo DTE).
        if rut_identidad(body.rut) and not existing.rut: existing.rut = body.rut
        if body.telefono and not existing.telefono: existing.telefono = body.telefono
        if body.email and not existing.email: existing.email = body.email
        if body.vehiculos and not existing.vehiculos: existing.vehiculos = body.vehiculos
        existing.is_active = True
        db.commit()
        db.refresh(existing)
        return {**_cliente_dict(existing), "reutilizado": True}

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
    return {**_cliente_dict(cliente), "reutilizado": False}


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
