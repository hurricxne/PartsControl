"""Tickets de soporte / solicitudes de cambio (MachParts). Hilo de conversación:
el solicitante y el equipo pueden responder y re-responder. Notifica in-app + correo
a soporte@bigcode.cl (best-effort)."""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db
from auth import get_current_user
from models.models import User, Ticket, TicketRespuesta
from config import settings

router = APIRouter(prefix="/tickets", tags=["tickets"])

CATEGORIAS = {"bug", "mejora", "soporte", "consulta"}
PRIORIDADES = {"baja", "media", "alta", "urgente"}
ESTADOS = {"abierto", "en_progreso", "resuelto", "cerrado"}


class TicketCreate(BaseModel):
    titulo: str
    descripcion: str
    categoria: str = "soporte"
    prioridad: str = "media"


class RespuestaCreate(BaseModel):
    mensaje: str


class EstadoUpdate(BaseModel):
    estado: str


def _next_numero(db: Session) -> str:
    year = datetime.now().year
    prefix = f"TCK-{year}-"
    last = (db.query(Ticket).filter(Ticket.numero.like(f"{prefix}%"))
            .order_by(Ticket.id.desc()).first())
    n = 0
    if last and last.numero:
        try:
            n = int(last.numero.rsplit("-", 1)[1])
        except (ValueError, IndexError):
            n = 0
    return f"{prefix}{n + 1:04d}"


def _notificar(db: Session, titulo: str, mensaje: str, tid: int):
    try:
        from notificaciones import crear_notificacion
        crear_notificacion(db, "admin", "info", titulo, mensaje,
                           entidad_tipo="ticket", entidad_id=tid, link=f"/tickets")
    except Exception:
        pass


def _correo(asunto: str, cuerpo: str, destino: Optional[str] = None):
    try:
        from services.mailer import enviar_correo
        enviar_correo(asunto, cuerpo, destino)
    except Exception:
        pass


def _ticket_dict(t: Ticket, con_hilo: bool = False) -> dict:
    d = {
        "id": t.id, "numero": t.numero, "titulo": t.titulo, "descripcion": t.descripcion,
        "categoria": t.categoria, "prioridad": t.prioridad, "estado": t.estado,
        "solicitante_id": t.solicitante_id, "solicitante_nombre": t.solicitante_nombre,
        "fecha_creacion": t.fecha_creacion.isoformat() if t.fecha_creacion else None,
        "fecha_actualizacion": t.fecha_actualizacion.isoformat() if t.fecha_actualizacion else None,
        "fecha_cierre": t.fecha_cierre.isoformat() if t.fecha_cierre else None,
        "n_respuestas": len(t.respuestas) if t.respuestas is not None else 0,
    }
    if con_hilo:
        d["respuestas"] = [
            {"id": r.id, "autor_id": r.autor_id, "autor_nombre": r.autor_nombre,
             "es_solicitante": bool(r.es_solicitante), "mensaje": r.mensaje,
             "fecha_creacion": r.fecha_creacion.isoformat() if r.fecha_creacion else None}
            for r in sorted(t.respuestas, key=lambda x: x.id)
        ]
    return d


@router.get("")
def listar(
    estado: Optional[str] = None,
    categoria: Optional[str] = None,
    prioridad: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = db.query(Ticket)
    if estado:
        q = q.filter(Ticket.estado == estado)
    if categoria:
        q = q.filter(Ticket.categoria == categoria)
    if prioridad:
        q = q.filter(Ticket.prioridad == prioridad)
    tickets = q.order_by(Ticket.fecha_actualizacion.desc(), Ticket.id.desc()).all()
    return [_ticket_dict(t) for t in tickets]


@router.get("/counts")
def counts(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    out = {e: 0 for e in ESTADOS}
    for estado, n in (db.query(Ticket.estado, func.count(Ticket.id)).group_by(Ticket.estado).all()):
        out[estado] = n
    out["abiertos_total"] = out.get("abierto", 0) + out.get("en_progreso", 0)
    return out


@router.post("", status_code=201)
def crear(body: TicketCreate, db: Session = Depends(get_db),
          current_user: User = Depends(get_current_user)):
    if not body.titulo.strip() or not body.descripcion.strip():
        raise HTTPException(400, "Título y descripción son obligatorios")
    cat = body.categoria if body.categoria in CATEGORIAS else "soporte"
    pri = body.prioridad if body.prioridad in PRIORIDADES else "media"
    t = Ticket(
        numero=_next_numero(db), empresa=getattr(current_user, "empresa", "mineria"),
        titulo=body.titulo.strip()[:255], descripcion=body.descripcion.strip(),
        categoria=cat, prioridad=pri, estado="abierto",
        solicitante_id=current_user.id, solicitante_nombre=current_user.nombre,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    _notificar(db, f"Nuevo ticket {t.numero}", f"{t.titulo} — {current_user.nombre}", t.id)
    _correo(
        f"[PartsControl] Nuevo ticket {t.numero}: {t.titulo}",
        f"Sistema  : MachParts\nFolio    : {t.numero}\nSolicita : {current_user.nombre} ({current_user.email})\n"
        f"Categoría: {cat}\nPrioridad: {pri}\n\n{t.descripcion}\n",
    )
    return _ticket_dict(t, con_hilo=True)


@router.get("/{ticket_id}")
def detalle(ticket_id: int, db: Session = Depends(get_db),
            current_user: User = Depends(get_current_user)):
    t = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not t:
        raise HTTPException(404, "Ticket no encontrado")
    return _ticket_dict(t, con_hilo=True)


@router.post("/{ticket_id}/respuestas", status_code=201)
def responder(ticket_id: int, body: RespuestaCreate, db: Session = Depends(get_db),
              current_user: User = Depends(get_current_user)):
    t = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not t:
        raise HTTPException(404, "Ticket no encontrado")
    if t.estado == "cerrado":
        raise HTTPException(400, "El ticket está cerrado; no admite respuestas")
    if not body.mensaje.strip():
        raise HTTPException(400, "El mensaje no puede estar vacío")
    es_solicitante = (current_user.id == t.solicitante_id)
    r = TicketRespuesta(
        ticket_id=t.id, autor_id=current_user.id, autor_nombre=current_user.nombre,
        es_solicitante=es_solicitante, mensaje=body.mensaje.strip(),
    )
    db.add(r)
    # el solicitante responde un ticket ya resuelto -> se reabre
    if es_solicitante and t.estado == "resuelto":
        t.estado = "en_progreso"
    t.fecha_actualizacion = func.now()
    db.commit()
    db.refresh(t)
    _notificar(db, f"Nueva respuesta en {t.numero}", f"{current_user.nombre}: {body.mensaje.strip()[:80]}", t.id)
    _correo(
        f"[PartsControl] Respuesta en ticket {t.numero}: {t.titulo}",
        f"Sistema : MachParts\nFolio   : {t.numero}\nEstado  : {t.estado}\nAutor   : {current_user.nombre}\n\n{body.mensaje.strip()}\n",
    )
    return _ticket_dict(t, con_hilo=True)


@router.patch("/{ticket_id}")
def cambiar_estado(ticket_id: int, body: EstadoUpdate, db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_user)):
    t = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not t:
        raise HTTPException(404, "Ticket no encontrado")
    if body.estado not in ESTADOS:
        raise HTTPException(400, f"Estado inválido: {body.estado}")
    t.estado = body.estado
    t.fecha_actualizacion = func.now()
    t.fecha_cierre = func.now() if body.estado == "cerrado" else None
    db.commit()
    db.refresh(t)
    _notificar(db, f"Ticket {t.numero} → {t.estado}", f"{t.titulo}", t.id)
    return _ticket_dict(t, con_hilo=True)
