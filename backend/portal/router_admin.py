"""/api/portal-admin/* — lo que opera el PERSONAL de Grupo AM sobre el portal.

Quién entra: cuentas internas de la marca 'mineria' (require_empresa en el router, que a
su vez pasa por auth.get_current_user → una cuenta portal recibe 403 acá como en todo el
ERP). Flujo de la pantalla "Cuentas del portal":
  1) GET  /clientes                     fichas de cliente con cuántas ventas y cuentas tienen
  2) GET  /cuentas                      cuentas portal con su estado y último acceso
  3) POST /cuentas                      crear cuenta → devuelve la clave temporal UNA vez
  4) POST /cuentas/{id}/restablecer-clave | /reactivar | /desactivar
     DELETE /cuentas/{id}               borra la cuenta (para poder borrar o re-rutear su ficha)
  5) GET  /vinculacion                  ensayo del vínculo venta ↔ cliente + huérfanas
     POST /vinculacion/aplicar          lo mismo, escribiendo

Regla dura de este archivo: toda ruta con {usuario_id} trabaja SOLO sobre cuentas portal
(_cuenta_portal). Este router no puede tocar, ni por error de id, la cuenta de alguien
del personal: restablecerle la clave o desactivarlo sería dejarlo fuera del ERP.
Las fichas de cliente se crean/editan en la pantalla Clientes de siempre (routers/clientes.py).
"""
import logging
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth import TIPO_PORTAL, get_current_user, get_password_hash
from database import get_db
from empresa_guard import require_empresa
from models.models import Cliente, Cotizacion, User
from .deps import RutaDelPortal
from .models import PERFIL_PROCUREMENT, PortalAcceso, UsuarioCliente
from .service import (
    CuentaError, DIAS_INACTIVIDAD_CORTE, clave_temporal, clave_temporal_vencida, crear_cuenta,
    cuenta_vencida, vencimiento_clave_temporal, vincular_historico,
)

logger = logging.getLogger("portal")

router = APIRouter(
    prefix="/portal-admin",
    tags=["portal-admin"],
    # 503 con el comando de init_db si falta una tabla del módulo: es la pantalla donde
    # el comando le llega a quien puede correrlo (el personal).
    route_class=RutaDelPortal,
    dependencies=[Depends(require_empresa("mineria"))],
)


class CuentaCreate(BaseModel):
    email: str = Field(..., max_length=255)
    nombre: str = Field(..., max_length=255)
    cliente_ids: List[int]
    perfil: str = PERFIL_PROCUREMENT


def _cuenta_portal(db: Session, usuario_id: int) -> User:
    usuario = (
        db.query(User)
        .filter(User.id == usuario_id, User.tipo_acceso == TIPO_PORTAL)
        .first()
    )
    if usuario is None:
        # 404 también para un id que existe pero es del personal: ver la regla dura.
        raise HTTPException(404, "Cuenta del portal no encontrada")
    return usuario


def _estado(usuario: User, acceso) -> str:
    if not usuario.is_active:
        return "desactivada"
    return "vencida" if cuenta_vencida(acceso) else "activa"


def _plazo_de_la_clave(acceso) -> dict:
    """El plazo de la clave temporal, con el MISMO cálculo que hace el login. El estado
    de arriba mira solo inactividad: una cuenta 'activa' cuya temporal venció no puede
    entrar, y sin estos campos el listado prometería lo contrario. `vencida` la decide
    el backend para no comparar contra el reloj del navegador."""
    vence = vencimiento_clave_temporal(acceso)
    return {
        "clave_temporal_vence_at": vence.isoformat() if vence else None,
        "clave_temporal_vencida": clave_temporal_vencida(acceso),
    }


@router.get("/clientes")
def listar_clientes(db: Session = Depends(get_db)):
    ventas = dict(
        db.query(Cotizacion.cliente_id, func.count(Cotizacion.id))
        .filter(Cotizacion.cliente_id.isnot(None))
        .group_by(Cotizacion.cliente_id)
        .all()
    )
    cuentas = dict(
        db.query(UsuarioCliente.cliente_id, func.count(UsuarioCliente.id))
        .group_by(UsuarioCliente.cliente_id)
        .all()
    )
    return [
        {
            "id": c.id,
            "rut": c.rut or "",
            "nombre": c.nombre or "",
            "ventas_vinculadas": int(ventas.get(c.id, 0)),
            "cuentas": int(cuentas.get(c.id, 0)),
        }
        for c in db.query(Cliente).order_by(Cliente.nombre).all()
    ]


@router.get("/cuentas")
def listar_cuentas(db: Session = Depends(get_db)):
    usuarios = (
        db.query(User).filter(User.tipo_acceso == TIPO_PORTAL).order_by(User.nombre).all()
    )
    ids = [u.id for u in usuarios]
    accesos = {
        a.usuario_id: a
        for a in db.query(PortalAcceso).filter(PortalAcceso.usuario_id.in_(ids or [0])).all()
    }
    puente: dict = {}
    for fila, nombre in (
        db.query(UsuarioCliente, Cliente.nombre)
        .join(Cliente, Cliente.id == UsuarioCliente.cliente_id)
        .filter(UsuarioCliente.usuario_id.in_(ids or [0]))
        .all()
    ):
        puente.setdefault(fila.usuario_id, []).append(
            {"id": fila.cliente_id, "nombre": nombre or "", "perfil": fila.perfil})
    salida = []
    for u in usuarios:
        acceso = accesos.get(u.id)
        salida.append({
            "id": u.id,
            "email": u.email,
            "nombre": u.nombre,
            "estado": _estado(u, acceso),
            "clientes": puente.get(u.id, []),
            "ultimo_acceso_at": acceso.ultimo_acceso_at.isoformat() if acceso and acceso.ultimo_acceso_at else None,
            "vigente_desde": acceso.vigente_desde.isoformat() if acceso and acceso.vigente_desde else None,
            **_plazo_de_la_clave(acceso),
        })
    return {"dias_inactividad_corte": DIAS_INACTIVIDAD_CORTE, "cuentas": salida}


@router.post("/cuentas", status_code=201)
def crear(
    body: CuentaCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        usuario, temporal = crear_cuenta(
            db, email=body.email, nombre=body.nombre, cliente_ids=body.cliente_ids,
            perfil=body.perfil, creado_por_id=current_user.id,
        )
    except CuentaError as exc:
        # También los rechazos que decide la base (otra alta del mismo correo en vuelo,
        # ficha eliminada a mitad del alta): crear_cuenta los traduce en el flush donde
        # ocurren, así que acá no hay nada que atrapar al confirmar.
        db.rollback()
        raise HTTPException(exc.codigo, exc.mensaje)
    db.commit()
    # El correo entra al mensaje porque el id solo no dice a quién se le abrió la puerta;
    # va con %r, que escapa un salto de línea. La clave temporal jamás entra a un mensaje.
    logger.info("portal-admin: usuario %s creó la cuenta portal %s (%r) para los clientes %s",
                current_user.id, usuario.id, usuario.email, sorted(set(body.cliente_ids)))
    # La clave temporal viaja UNA vez, en esta respuesta. No se guarda en claro ni se
    # puede volver a pedir: si se pierde, se restablece. Con ella va su plazo.
    acceso = db.query(PortalAcceso).filter(PortalAcceso.usuario_id == usuario.id).first()
    return {"id": usuario.id, "email": usuario.email, "clave_temporal": temporal, **_plazo_de_la_clave(acceso)}


@router.post("/cuentas/{usuario_id}/restablecer-clave")
def restablecer_clave(
    usuario_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    usuario = _cuenta_portal(db, usuario_id)
    temporal = clave_temporal()
    usuario.hashed_password = get_password_hash(temporal)
    acceso = db.query(PortalAcceso).filter(PortalAcceso.usuario_id == usuario.id).first()
    if acceso is not None:
        acceso.clave_temporal_desde = datetime.utcnow()  # caduca si no la cambian a tiempo
    db.commit()
    logger.info("portal-admin: usuario %s restableció la clave de la cuenta %s", current_user.id, usuario.id)
    return {"id": usuario.id, "clave_temporal": temporal, **_plazo_de_la_clave(acceso)}


@router.post("/cuentas/{usuario_id}/reactivar")
def reactivar(
    usuario_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    usuario = _cuenta_portal(db, usuario_id)
    usuario.is_active = 1
    ahora = datetime.utcnow()
    acceso = db.query(PortalAcceso).filter(PortalAcceso.usuario_id == usuario.id).first()
    if acceso is None:
        # Este módulo nunca deja una cuenta portal sin su registro de vigencia. Si falta,
        # no se sabe de dónde salió la clave que tiene: se trata como temporal (el
        # contacto tiene que cambiarla y, si no, vence).
        acceso = PortalAcceso(usuario_id=usuario.id, creado_por_id=current_user.id, clave_temporal_desde=ahora)
        db.add(acceso)
    acceso.vigente_desde = ahora  # el plazo de inactividad vuelve a correr
    db.commit()
    logger.info("portal-admin: usuario %s reactivó la cuenta %s", current_user.id, usuario.id)
    # Reactivar NO renueva la clave temporal (reviviría una credencial que viajó por
    # correo o WhatsApp): si venció, la respuesta lo dice y el operador la restablece.
    return {"id": usuario.id, "estado": _estado(usuario, acceso), **_plazo_de_la_clave(acceso)}


@router.post("/cuentas/{usuario_id}/desactivar")
def desactivar(
    usuario_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    usuario = _cuenta_portal(db, usuario_id)
    usuario.is_active = 0
    db.commit()
    logger.info("portal-admin: usuario %s desactivó la cuenta %s", current_user.id, usuario.id)
    return {"id": usuario.id, "estado": "desactivada"}


@router.delete("/cuentas/{usuario_id}", status_code=204)
def eliminar(
    usuario_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Borra la cuenta con su puente y su registro de vigencia. Es lo que destraba el
    borrado de una ficha de cliente o la corrección de su RUT (routers/clientes.py los
    rechaza mientras la ficha tenga cuentas). Para sacar a alguien sin perder el rastro,
    lo normal es DESACTIVAR."""
    usuario = _cuenta_portal(db, usuario_id)
    # Después del commit la cuenta ya no existe y su id no lleva a ninguna fila: lo que
    # la bitácora necesita se copia ahora.
    actor_id, correo = current_user.id, usuario.email
    db.query(UsuarioCliente).filter(UsuarioCliente.usuario_id == usuario.id).delete(synchronize_session=False)
    db.query(PortalAcceso).filter(PortalAcceso.usuario_id == usuario.id).delete(synchronize_session=False)
    db.delete(usuario)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # exc.orig nombra la tabla que todavía apunta a la cuenta (su bitácora de descargas).
        logger.warning("portal-admin: usuario %s no pudo eliminar la cuenta portal %s (%r): %s",
                       actor_id, usuario_id, correo, exc.orig)
        raise HTTPException(
            409, "La cuenta tiene historial asociado y no se puede borrar: desactívala.")
    logger.info("portal-admin: usuario %s eliminó la cuenta portal %s (%r)", actor_id, usuario_id, correo)


@router.get("/vinculacion")
def ensayo_vinculacion(db: Session = Depends(get_db)):
    """Qué pasaría al vincular, sin escribir nada."""
    resultado = vincular_historico(db, aplicar=False)
    db.rollback()
    return resultado


@router.post("/vinculacion/aplicar")
def aplicar_vinculacion(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    resultado = vincular_historico(db, aplicar=True)
    db.commit()
    logger.info(
        "portal-admin: usuario %s vinculó %s cotizaciones (%s huérfanas)",
        current_user.id, resultado["vinculadas_ahora"], len(resultado["huerfanas"]),
    )
    return resultado
