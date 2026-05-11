from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from models.models import Notificacion


def crear_notificacion(
    db: Session,
    rol: str,
    severidad: str,
    titulo: str,
    mensaje: str,
    entidad_tipo: str = None,
    entidad_id: int = None,
    link: str = None,
    regla: str = None,
) -> Notificacion | None:
    """
    Create in-app notification with idempotency:
    Won't duplicate if same regla+entidad already has an unread notification in last 24h.
    """
    if regla and entidad_tipo and entidad_id:
        hace_24h = datetime.utcnow() - timedelta(hours=24)
        existente = db.query(Notificacion).filter(
            Notificacion.regla == regla,
            Notificacion.entidad_tipo == entidad_tipo,
            Notificacion.entidad_id == entidad_id,
            Notificacion.leida == False,
            Notificacion.fecha_creacion >= hace_24h,
        ).first()
        if existente:
            return None

    n = Notificacion(
        destinatario_rol=rol,
        severidad=severidad,
        titulo=titulo,
        mensaje=mensaje,
        entidad_tipo=entidad_tipo,
        entidad_id=entidad_id,
        link=link,
        regla=regla,
    )
    db.add(n)
    db.commit()
    db.refresh(n)
    return n
