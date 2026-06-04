"""Helper para crear notificaciones in-app de MonzaParts."""


def crear_notif(db, titulo, mensaje=None, tipo="info", link=None, entidad=None, entidad_id=None):
    """Crea una notificacion. No rompe el flujo si falla."""
    try:
        from monza_models import MonzaNotificacion
        db.add(MonzaNotificacion(
            titulo=titulo, mensaje=mensaje, tipo=tipo,
            link=link, entidad=entidad, entidad_id=entidad_id,
        ))
        db.commit()
    except Exception:
        db.rollback()
