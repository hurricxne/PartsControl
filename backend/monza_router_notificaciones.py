"""Notificaciones in-app MonzaParts (globales para el equipo)."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import desc, func
from datetime import datetime

from database import get_db
from auth import get_current_user
from empresa_guard import require_empresa
from monza_models import MonzaNotificacion

router = APIRouter(prefix="/api/monza/notificaciones", tags=["monza-notificaciones"],
    # Candado de empresa (hallazgo del equipo de testing 2026-08-27).
    # Los títulos de las notificaciones llevan números de documento y nombres de cliente.
    dependencies=[Depends(require_empresa("automotriz"))],
)


def _dict(n: MonzaNotificacion) -> dict:
    return {
        "id": n.id,
        "titulo": n.titulo,
        "mensaje": n.mensaje,
        "tipo": n.tipo,
        "entidad": n.entidad,
        "entidad_id": n.entidad_id,
        "link": n.link,
        "leida": bool(n.leida),
        "fecha": n.fecha.isoformat() if n.fecha else None,
    }


@router.get("")
def listar(db: Session = Depends(get_db), _=Depends(get_current_user)):
    items = db.query(MonzaNotificacion).order_by(desc(MonzaNotificacion.fecha)).limit(30).all()
    return [_dict(n) for n in items]


@router.get("/count")
def count_no_leidas(db: Session = Depends(get_db), _=Depends(get_current_user)):
    n = db.query(func.count(MonzaNotificacion.id)).filter(MonzaNotificacion.leida == 0).scalar() or 0
    return {"unread": n}


@router.patch("/{notif_id}/leida")
def marcar_leida(notif_id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    n = db.query(MonzaNotificacion).filter(MonzaNotificacion.id == notif_id).first()
    if not n:
        raise HTTPException(status_code=404, detail="No encontrada")
    n.leida = 1
    db.commit()
    return {"ok": True}


@router.post("/marcar-todas")
def marcar_todas(db: Session = Depends(get_db), _=Depends(get_current_user)):
    db.query(MonzaNotificacion).filter(MonzaNotificacion.leida == 0).update({"leida": 1})
    db.commit()
    return {"ok": True}
