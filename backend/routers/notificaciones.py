from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from database import get_db
from auth import get_current_user
from models.models import User, Notificacion

router = APIRouter(prefix="/notificaciones", tags=["notificaciones"])

ROL_MAP = {
    "admin": ["ventas", "abastecimiento", "logistica", "bodega"],
    "ventas": ["ventas"],
    "abastecimiento": ["abastecimiento"],
    "logistica": ["logistica"],
    "bodega": ["bodega"],
}


def get_roles(user: User) -> list[str]:
    role = getattr(user, 'rol', 'admin') or 'admin'
    return ROL_MAP.get(role, ["ventas", "abastecimiento", "logistica", "bodega"])


@router.get("")
def list_notificaciones(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    roles = get_roles(current_user)
    items = (
        db.query(Notificacion)
        .filter(Notificacion.destinatario_rol.in_(roles))
        .order_by(Notificacion.fecha_creacion.desc())
        .limit(50)
        .all()
    )
    return [
        {
            "id": n.id,
            "destinatario_rol": n.destinatario_rol,
            "severidad": n.severidad,
            "titulo": n.titulo,
            "mensaje": n.mensaje,
            "entidad_tipo": n.entidad_tipo,
            "entidad_id": n.entidad_id,
            "link": n.link,
            "leida": n.leida,
            "fecha_creacion": n.fecha_creacion.isoformat() if n.fecha_creacion else None,
        }
        for n in items
    ]


@router.get("/no-leidas/count")
def count_no_leidas(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    roles = get_roles(current_user)
    count = (
        db.query(Notificacion)
        .filter(
            Notificacion.destinatario_rol.in_(roles),
            Notificacion.leida == False,
        )
        .count()
    )
    return {"count": count}


@router.patch("/{notif_id}/leida")
def marcar_leida(
    notif_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    n = db.query(Notificacion).filter(Notificacion.id == notif_id).first()
    if n:
        n.leida = True
        db.commit()
    return {"ok": True}


@router.patch("/marcar-todas-leidas")
def marcar_todas_leidas(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    roles = get_roles(current_user)
    db.query(Notificacion).filter(
        Notificacion.destinatario_rol.in_(roles),
        Notificacion.leida == False,
    ).update({"leida": True}, synchronize_session=False)
    db.commit()
    return {"ok": True}
