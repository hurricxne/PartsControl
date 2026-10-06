"""require_portal() → PortalScope. ÚNICO lugar que resuelve a qué clientes ve una cuenta.

Quién entra: solo cuentas `tipo_acceso='portal'`, activas, vigentes y con al menos un
cliente en el puente. Flujo:
  1) auth.get_usuario_autenticado valida el token (401 si no hay sesión, si la cuenta
     está desactivada o si el token se emitió con una clave que la cuenta ya no tiene).
     NO se usa get_current_user: esa puerta es la del ERP y niega justamente a las
     cuentas portal.
  2) Cuenta que no es portal → 403. El personal no navega el portal con su cuenta: lo
     que vería es la unión de todos los clientes, y esa vista no existe a propósito.
  3) Cuenta vencida por inactividad → 403 (service.cuenta_vencida).
  4) Puente vacío → 403. NUNCA "sin filtro = ve todo".

Las rutas del portal declaran esta dependencia en el ROUTER (imposible de olvidar en un
endpoint nuevo) y además filtran cada consulta por `scope.cliente_ids`: doble candado,
la convención de compras_contab/router.py.

DOS PUERTAS, UNA DENTRO DE LA OTRA
  · require_portal        la identidad (/portal/yo y el cambio de clave).
  · require_portal_listo  require_portal + "ya reemplazó la clave temporal". Es la de
                          todo lo que entrega datos del cliente (router_ordenes.py).
    portal/tests/test_aislamiento.py falla si una ruta /api/portal/* que no sea esas
    dos de identidad queda sin require_portal_listo.

SI FALTA UNA TABLA DEL MÓDULO (deploy sin `portal.init_db`), las rutas responden 503 con
el comando exacto en vez de un 500 mudo: lo hace RutaDelPortal, la clase de ruta de los
routers del portal. Solo traduce "no existe la tabla / la columna"; cualquier otro error
de la base sigue su curso y queda en el log.
"""
import logging
from dataclasses import dataclass
from typing import Callable

from fastapi import Depends, HTTPException, Request, Response
from fastapi.routing import APIRoute
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from auth import TIPO_PORTAL, get_usuario_autenticado
from database import get_db
from models.models import User
from .models import PERFIL_BODEGA, PERFIL_PROCUREMENT, PortalAcceso, UsuarioCliente
from .service import cuenta_vencida

logger = logging.getLogger("portal")

DETALLE_NO_ES_PORTAL = "Acceso exclusivo de las cuentas del Portal de Clientes"
DETALLE_VENCIDA = (
    "Tu cuenta del portal está suspendida por inactividad. "
    "Pide a tu ejecutivo de MachParts que la reactive."
)
DETALLE_CLAVE_TEMPORAL_VENCIDA = (
    "La clave temporal que te entregaron ya venció. "
    "Pide a tu ejecutivo de MachParts que te genere una nueva."
)
DETALLE_SIN_CLIENTE = "Tu cuenta no tiene un cliente asignado. Contacta a tu ejecutivo de MachParts."
DETALLE_ESQUEMA = (
    "El Portal de Clientes no está instalado en esta base: "
    "corre `python -m portal.init_db` desde backend/ y reinicia."
)
DETALLE_DEBE_CAMBIAR_CLAVE = (
    "Antes de ver tus pedidos tienes que reemplazar la clave temporal por una propia."
)

# Códigos de MySQL para "la tabla no existe" y "la columna no existe".
_ERRORES_DE_ESQUEMA = (1146, 1054)


def esquema_incompleto(error: Exception) -> bool:
    """¿El error de la base es porque falta una tabla o una columna? (y no una conexión
    caída, un bloqueo o una consulta mal escrita, que no se arreglan con init_db)."""
    original = getattr(error, "orig", None)
    codigo = (getattr(original, "args", None) or [None])[0]
    return codigo in _ERRORES_DE_ESQUEMA


class RutaDelPortal(APIRoute):
    """Clase de ruta de los routers del portal (`APIRouter(route_class=RutaDelPortal)`).
    Ver «SI FALTA UNA TABLA DEL MÓDULO» en el encabezado."""

    def get_route_handler(self) -> Callable:
        atender = super().get_route_handler()

        async def atender_avisando_esquema(request: Request) -> Response:
            try:
                return await atender(request)
            except (ProgrammingError, OperationalError) as error:
                if not esquema_incompleto(error):
                    raise
                logger.exception("portal: falta una tabla o columna del módulo (%s)", request.url.path)
                raise HTTPException(503, DETALLE_ESQUEMA)

        return atender_avisando_esquema


@dataclass(frozen=True)
class PortalScope:
    usuario_id: int
    cliente_ids: frozenset  # jamás vacío: require_portal responde 403 antes
    perfil: str             # 'procurement' | 'bodega'
    # La cuenta sigue con la clave temporal que le entregó su ejecutivo.
    debe_cambiar_clave: bool = False

    @property
    def ve_montos(self) -> bool:
        """Solo el perfil 'procurement' ve precios, totales, facturas y los archivos de
        las guías (que traen precios). LA regla de montos del portal: todo lo que
        decide si se muestra un monto pregunta acá."""
        return self.perfil == PERFIL_PROCUREMENT


def require_portal(
    user: User = Depends(get_usuario_autenticado),
    db: Session = Depends(get_db),
) -> PortalScope:
    if getattr(user, "tipo_acceso", None) != TIPO_PORTAL:
        raise HTTPException(403, DETALLE_NO_ES_PORTAL)
    acceso = db.query(PortalAcceso).filter(PortalAcceso.usuario_id == user.id).first()
    filas = db.query(UsuarioCliente).filter(UsuarioCliente.usuario_id == user.id).all()
    if cuenta_vencida(acceso):
        raise HTTPException(403, DETALLE_VENCIDA)
    if not filas:
        raise HTTPException(403, DETALLE_SIN_CLIENTE)
    # Un contacto con varios clientes y perfiles distintos opera con el MÁS restrictivo:
    # ve montos solo si en TODOS es exactamente 'procurement'. Cualquier otra cosa —
    # 'bodega', o un valor desconocido que alguien haya escrito a mano en la base— cae a
    # 'bodega': el scope tiene un solo perfil y, ante la duda, se muestra de menos.
    perfil = (PERFIL_PROCUREMENT if all(f.perfil == PERFIL_PROCUREMENT for f in filas)
              else PERFIL_BODEGA)
    return PortalScope(
        usuario_id=user.id,
        cliente_ids=frozenset(f.cliente_id for f in filas),
        perfil=perfil,
        debe_cambiar_clave=acceso.clave_temporal_desde is not None,
    )


def require_portal_listo(scope: PortalScope = Depends(require_portal)) -> PortalScope:
    """require_portal y, además, la cuenta ya eligió su propia clave.

    Es la puerta de las rutas que entregan DATOS del cliente (pedidos, documentos, fotos).
    La clave temporal viaja por correo o WhatsApp: quien la intercepte no debe alcanzar a
    ver nada. Con la temporal solo se puede consultar /portal/yo y cambiar la clave
    (portal/router.py); y el token de quien entró con ella deja de valer cuando la clave
    se cambia (auth.token_para).
    """
    if scope.debe_cambiar_clave:
        raise HTTPException(403, DETALLE_DEBE_CAMBIAR_CLAVE)
    return scope
