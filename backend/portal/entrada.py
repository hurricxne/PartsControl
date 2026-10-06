"""La entrada de una cuenta del Portal de Clientes por el LOGIN (`entrada_por_login`).

El cliente entra por el mismo `POST /api/auth/login` que el personal (routers/auth.py).
Lo que ese login hace de más con una cuenta portal vive acá, en el módulo; el router
del núcleo solo lo llama. Son dos cortes y anotar el uso:
  · Corte por inactividad (`service.cuenta_vencida`), el mismo que `require_portal`
    aplica después en cada petición.
  · Corte por clave temporal vencida (`service.clave_temporal_vencida`). Existe SOLO
    acá: `require_portal` no lo repite a propósito, para que quien entró el último día
    del plazo alcance a cambiar su clave (las rutas de datos rechazan cualquier clave
    temporal, vencida o no: `require_portal_listo`).
  · `service.registrar_acceso`: el token dura 8 horas, así que cada día de uso real pasa
    por el login y no hace falta escribir en cada petición.

Si falta la tabla o una columna del módulo (deploy sin `portal.init_db`) responde el 503
con el comando exacto, igual que `RutaDelPortal` en las rutas del portal: el login no
pasa por esa clase de ruta y sin esto el cliente recibiría un 500 mudo. Cualquier otro
error de la base sigue su curso y queda en el log.
"""
import logging

from fastapi import HTTPException
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from models.models import User
from .deps import (
    DETALLE_CLAVE_TEMPORAL_VENCIDA, DETALLE_ESQUEMA, DETALLE_VENCIDA, esquema_incompleto,
)
from .models import PortalAcceso
from .service import clave_temporal_vencida, cuenta_vencida, registrar_acceso

logger = logging.getLogger("portal")


def entrada_por_login(db: Session, user: User) -> None:
    """Dos cortes y anotar el uso (hace el commit del último acceso).

    `login` la llama DESPUÉS de verificar la clave: quien no la sabe sigue recibiendo el
    401 genérico, así que estos mensajes no sirven para averiguar qué correos existen.
    """
    try:
        acceso = db.query(PortalAcceso).filter(PortalAcceso.usuario_id == user.id).first()
    except (ProgrammingError, OperationalError) as error:
        if not esquema_incompleto(error):
            raise
        logger.exception("portal: falta una tabla o columna del módulo (login)")
        raise HTTPException(503, DETALLE_ESQUEMA)
    if cuenta_vencida(acceso):
        raise HTTPException(403, DETALLE_VENCIDA)
    if clave_temporal_vencida(acceso):
        raise HTTPException(403, DETALLE_CLAVE_TEMPORAL_VENCIDA)
    registrar_acceso(db, user.id)
    db.commit()
