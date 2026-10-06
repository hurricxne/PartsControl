import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from pydantic import BaseModel, EmailStr
from database import get_db
from models.models import User
from auth import (
    verify_password, get_password_hash, token_para, get_current_user,
    get_usuario_autenticado, TIPO_INTERNO, TIPO_PORTAL,
)
from config import settings
from empresa_guard import require_empresa
import rate_limit_login as rl

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: str
    password: str


class RegisterRequest(BaseModel):
    email: str
    nombre: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str
    user: dict


# Hash de una clave que ninguna cuenta tiene, con el mismo costo que los reales (sale de
# la misma `get_password_hash`). `_clave_correcta` verifica contra él cuando el correo no
# tiene cuenta.
_HASH_SIN_CUENTA = get_password_hash("relleno: ninguna cuenta guarda este hash")


def _credenciales_incorrectas() -> HTTPException:
    """El único 401 del login: el mismo para un correo sin cuenta, una clave mala y un
    correo imposible."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Email o contraseña incorrectos",
    )


def _clave_correcta(user: Optional[User], clave: str) -> bool:
    """¿`clave` es la de `user`? Gasta un bcrypt haya cuenta o no.

    Sin cuenta se verifica contra `_HASH_SIN_CUENTA` y se responde False: si el bcrypt
    solo corriera cuando la cuenta existe, el tiempo de respuesta diría qué correos
    tienen cuenta. Ese bcrypt no cuelga de ninguna cuenta, así que tiene su propio techo
    (`rl.cupo_de_relleno`); con el techo agotado se responde sin gastarlo.
    """
    if user is not None:
        return verify_password(clave, user.hashed_password)
    if rl.cupo_de_relleno():
        verify_password(clave, _HASH_SIN_CUENTA)
    return False


@router.post("/login", response_model=TokenResponse)
def login(data: LoginRequest, request: Request, db: Session = Depends(get_db)):
    """Inicio de sesión, con límite de intentos. La política, la llave del contador y los
    modos de falla están en `rate_limit_login.py`; acá importa el ORDEN:

      1) Un correo que no puede ser de nadie (`rl.correo_imposible`) recibe el 401 de
         siempre, sin tocar la base ni el contador.
      2) El SELECT resuelve la cuenta ANTES del limitador, porque de ahí sale la llave del
         contador (`rl.llave_de_login`). Es un SELECT indexado, barato.
      3) `rl.reservar_intento` va ANTES del bcrypt, y comprueba y anota en una sola
         operación. Después del bcrypt, cada intento rechazado igual costaría un hash.
      4) El bcrypt corre haya cuenta o no (`_clave_correcta`).
      5) `rl.limpiar_exito` al entrar bien: la reserva contó el intento antes de saber si
         la clave era buena, y sin esta llamada cinco ingresos correctos en 15 minutos
         bloquearían al usuario.

    El 401 no distingue «correo sin cuenta» de «clave mala», ni por el texto ni por el
    tiempo de respuesta (por eso el punto 4).
    """
    if rl.correo_imposible(data.email):
        raise _credenciales_incorrectas()
    ip = rl.ip_cliente(request)
    user = db.query(User).filter(User.email == data.email, User.is_active == 1).first()
    llave = rl.llave_de_login(user.id if user else None, data.email)

    if settings.LOGIN_RATE_LIMIT_ENABLED:
        # Reserva el cupo y comprueba el tope en UNA sola operación, antes del bcrypt.
        rl.reservar_intento(llave, ip, correo=data.email)

    if not _clave_correcta(user, data.password):
        # No hay nada que anotar: el intento ya quedó contado al reservarlo.
        raise _credenciales_incorrectas()
    if settings.LOGIN_RATE_LIMIT_ENABLED:
        # OBLIGATORIO en el camino feliz: borra los fallos que dejó la reserva. Sin esto,
        # entrar bien 5 veces en 15 minutos bloquearía al usuario.
        rl.limpiar_exito(llave, ip)
    tipo_acceso = getattr(user, "tipo_acceso", None) or TIPO_INTERNO
    if tipo_acceso == TIPO_PORTAL:
        # Import local: el login del personal no depende del módulo portal.
        from portal.entrada import entrada_por_login
        entrada_por_login(db, user)
    token = token_para(user)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": user.id, "email": user.email, "nombre": user.nombre,
            "empresa": user.empresa or "mineria",
            # El frontend decide con esto a qué cascarón entra (ERP o portal). Es solo
            # ruteo: la barrera real es get_current_user, que no confía en el navegador.
            "tipo_acceso": tipo_acceso,
        }
    }

@router.post("/register", status_code=201)
def register(
    data: RegisterRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Crea un usuario. EXIGE sesión: NO es un registro público.

    Antes este endpoint era ANÓNIMO y creaba un usuario ACTIVO sin empresa (o sea,
    'mineria' por el server_default). Verificado en vivo el 2026-07-22: un desconocido
    podía llamarlo sin credenciales, iniciar sesión con la cuenta recién creada y leer
    /api/contabilidad/facturas y /api/contabilidad/kpis de GRUPO AM — toda la
    contabilidad de la empresa. Ningún punto del frontend lo llamaba: era código muerto
    abierto a internet.

    El usuario nuevo HEREDA la empresa de quien lo crea, para que una cuenta de una
    marca no pueda fabricar cuentas de la otra y saltarse el candado de empresa.
    """
    if db.query(User).filter(User.email == data.email).first():
        raise HTTPException(status_code=400, detail="El email ya está registrado")
    user = User(
        email=data.email,
        nombre=data.nombre,
        hashed_password=get_password_hash(data.password),
        empresa=getattr(current_user, "empresa", None) or "mineria",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"message": "Usuario creado", "id": user.id}


@router.get("/me")
def me(current_user: User = Depends(get_usuario_autenticado)):
    """Quién soy. Es la ÚNICA ruta de este router que acepta una cuenta del portal (por
    eso usa get_usuario_autenticado y no get_current_user): solo devuelve los datos del
    propio usuario."""
    return {
        "id": current_user.id, "email": current_user.email, "nombre": current_user.nombre,
        "empresa": current_user.empresa or "mineria",
        "tipo_acceso": getattr(current_user, "tipo_acceso", None) or TIPO_INTERNO,
    }


@router.get("/users")
def listar_usuarios(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Usuarios activos de la misma empresa — alimenta los selectores de 'asesor responsable'.

    Solo personal: las cuentas del Portal de Clientes viven en la misma tabla y con
    empresa 'mineria', y sin el filtro de tipo el contacto de un cliente aparecería como
    asesor seleccionable en el Cierre de Venta."""
    empresa = current_user.empresa or "mineria"
    usuarios = (
        db.query(User)
        .filter(User.is_active == 1, User.empresa == empresa, User.tipo_acceso == TIPO_INTERNO)
        .order_by(User.nombre)
        .all()
    )
    return [
        {"id": u.id, "nombre": u.nombre, "email": u.email, "empresa": u.empresa or "mineria"}
        for u in usuarios
    ]


@router.get("/rate-limit/estado", dependencies=[Depends(require_empresa("mineria"))])
def estado_rate_limit(
    email: str,
    ip: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Diagnóstico del límite de login — para no tener que leer los logs de pm2.

    Uso:  GET /api/auth/rate-limit/estado?email=juan@grupoam.cl&ip=200.1.2.3

    Responde por la MISMA llave que usa el login (`rl.llave_de_login`): `email` es el
    correo consultado y `llave` la que le corresponde (`uid:N` si tiene cuenta), que es
    además lo que hay que buscar en `pm2 logs`.

    OJO CON EL PARÁMETRO `ip` (corregido 2026-08-20 tras revisión): una versión
    anterior tomaba la IP DEL OPERADOR que consultaba, no la de la persona
    bloqueada, y el campo se llamaba `ip` a secas. A las 3 AM preguntabas por
    `juan@x.cl` y recibías los contadores de tu propio notebook. Ahora la IP se pasa
    explícita; si se omite, la respuesta trae `ip: null` y los campos de IP en cero,
    que es honesto en vez de engañoso.

    `pid` está a propósito: dos llamadas seguidas que devuelvan PID distintos son la
    prueba directa de que hay MÁS DE UN worker y cada uno lleva su propio contador en
    memoria — la hipótesis que plantea el encabezado de `rate_limit_login.py`.
    Síntoma que la dispara: el usuario ve 429 pero esto responde "bloqueado": false.

    Va candado con `require_empresa` porque expone `faltan_segundos` exacto: con ese
    dato un atacante cronometra su próxima ráfaga y sabe si su objetivo está bloqueado.
    """
    # Mismo SELECT y misma función que `login`: el diagnóstico tiene que mirar exactamente
    # la llave que el login escribe.
    u = db.query(User).filter(User.email == email, User.is_active == 1).first()
    llave = rl.llave_de_login(u.id if u else None, email)
    datos = rl.estado(llave, (ip or "").strip())
    datos["email"] = email
    datos["usuario_existe"] = u is not None
    datos["pid"] = os.getpid()
    return datos
