"""/api/portal/* — lo que ve el CLIENTE. Solo cuentas portal (require_portal en el router).

La identidad. Dos rutas, las ÚNICAS que atienden a una cuenta que sigue con la clave
temporal:
  GET  /portal/yo        quién soy y qué cliente(s) veo — lo primero que pide la pantalla
  POST /portal/yo/clave  cambiar mi clave (la temporal que entrega el ejecutivo no puede
                         ser para siempre)
Los pedidos, el seguimiento, los documentos y las fotos están en portal/router_ordenes.py.

Reglas que valen para todo lo que se agregue acá:
  · Cada consulta filtra por `scope.cliente_ids` además del guard del router.
  · Recurso de otro cliente → 404, no 403 (un 403 confirma que el id existe).
  · Las respuestas se arman a mano, campo por campo. Nunca se devuelve un modelo ni un
    dict de los routers internos: de ahí salen el margen y el proveedor.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

import rate_limit_login as rl
from auth import get_password_hash, token_para, verify_password
from config import settings
from database import get_db
from models.models import Cliente, User
from .deps import PortalScope, RutaDelPortal, require_portal
from .models import PortalAcceso
from .service import BYTES_MAXIMOS_CLAVE, LARGO_MINIMO_CLAVE

logger = logging.getLogger("portal")

router = APIRouter(
    prefix="/portal",
    tags=["portal"],
    route_class=RutaDelPortal,
    dependencies=[Depends(require_portal)],
)


class CambioClave(BaseModel):
    clave_actual: str
    clave_nueva: str


@router.get("/yo")
def yo(scope: PortalScope = Depends(require_portal), db: Session = Depends(get_db)):
    usuario = db.query(User).filter(User.id == scope.usuario_id).first()
    clientes = (
        db.query(Cliente)
        .filter(Cliente.id.in_(scope.cliente_ids))
        .order_by(Cliente.nombre)
        .all()
    )
    return {
        "usuario": {"nombre": usuario.nombre, "email": usuario.email},
        "perfil": scope.perfil,
        "clientes": [{"id": c.id, "nombre": c.nombre or "", "rut": c.rut or ""} for c in clientes],
        # La cuenta sigue con la clave temporal que le entregó su ejecutivo: la pantalla
        # le exige cambiarla antes de mostrar nada (las rutas de datos la rechazan:
        # deps.require_portal_listo) y el login la rechaza pasado su plazo.
        "debe_cambiar_clave": scope.debe_cambiar_clave,
    }


@router.post("/yo/clave")
def cambiar_clave(
    body: CambioClave,
    request: Request,
    scope: PortalScope = Depends(require_portal),
    db: Session = Depends(get_db),
):
    """Cambia la clave del propio usuario.

    El orden importa. Primero lo barato (la forma de la clave nueva): un 400 por una clave
    corta no gasta cupo ni CPU. Después el límite de intentos, y recién entonces el bcrypt.
    El tope va ANTES del bcrypt porque este es el único bcrypt al alcance de una cuenta
    EXTERNA con sesión: sin tope sería un oráculo de la clave actual y una forma de gastar
    la CPU que comparte todo el ERP. La llave es LA MISMA del login (`rl.llave_de_cuenta`):
    un contador por cuenta, no dos. Un cambio correcto borra los fallos, no los intentos
    totales (regla 2 de `rate_limit_login.py`): quien sabe la clave tampoco puede
    cambiarla en bucle.
    """
    if len(body.clave_nueva) < LARGO_MINIMO_CLAVE:
        raise HTTPException(400, f"La clave nueva debe tener al menos {LARGO_MINIMO_CLAVE} caracteres")
    try:
        bytes_clave_nueva = len(body.clave_nueva.encode("utf-8"))
    except UnicodeEncodeError:
        # Un surrogate suelto en el JSON no es texto; sin esto el encode sale como 500.
        raise HTTPException(400, "La clave nueva tiene caracteres no válidos")
    if bytes_clave_nueva > BYTES_MAXIMOS_CLAVE:
        raise HTTPException(400, f"La clave nueva es demasiado larga (máximo {BYTES_MAXIMOS_CLAVE} bytes)")
    if body.clave_nueva == body.clave_actual:
        raise HTTPException(400, "La clave nueva debe ser distinta de la actual")

    llave, ip = rl.llave_de_cuenta(scope.usuario_id), rl.ip_cliente(request)
    if settings.LOGIN_RATE_LIMIT_ENABLED:
        # 429 si se pasó del tope; va ANTES del bcrypt. `origen` marca la línea del log:
        # el cupo es el del login, pero quien lo lee distingue un cambio de clave.
        rl.reservar_intento(llave, ip, origen="cambio-clave")
    usuario = db.query(User).filter(User.id == scope.usuario_id).first()
    if not verify_password(body.clave_actual, usuario.hashed_password):
        raise HTTPException(400, "La clave actual no es correcta")
    if settings.LOGIN_RATE_LIMIT_ENABLED:
        rl.limpiar_exito(llave, ip)

    usuario.hashed_password = get_password_hash(body.clave_nueva)
    acceso = db.query(PortalAcceso).filter(PortalAcceso.usuario_id == usuario.id).first()
    if acceso is not None:
        acceso.clave_temporal_desde = None  # ya no es la temporal: deja de caducar
    db.commit()
    logger.info("portal: usuario %s cambió su clave", usuario.id)
    # El token con que llegó esta petición quedó atado a la clave anterior y ya no vale
    # (auth.token_para): se devuelve uno nuevo para que la sesión siga sin volver a entrar.
    return {"ok": True, "access_token": token_para(usuario)}
