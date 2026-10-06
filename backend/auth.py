import hashlib
import hmac
from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from database import get_db
from models.models import User
from config import settings

security = HTTPBearer()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        clave = plain_password.encode("utf-8")
    except UnicodeEncodeError:
        # Texto que no es Unicode válido (un surrogate suelto en el JSON): no puede ser la
        # clave de nadie. Se responde False; la excepción llegaría al cliente como un 500.
        return False
    return bcrypt.checkpw(clave, hashed_password.encode("utf-8"))


def get_password_hash(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


# Tipos de cuenta (users.tipo_acceso). 'interno' = personal, entra al ERP. 'portal' =
# contacto de un cliente, entra SOLO al Portal de Clientes (backend/portal/).
TIPO_INTERNO = "interno"
TIPO_PORTAL = "portal"

# El frontend reconoce este 403 por el COMIENZO del texto (`DETALLES_AMBITO` en
# frontend-src/src/services/authErrors.ts) y avisa «tu cuenta no tiene acceso» en vez de
# dejar la pantalla vacía. Si se cambia el comienzo acá, cambiarlo también allá.
DETALLE_CUENTA_PORTAL = "Acceso restringido: esta cuenta es del Portal de Clientes"


def huella_clave(hashed_password: str) -> str:
    """Huella corta de la clave vigente de una cuenta. Cambia cada vez que la clave se
    escribe (bcrypt usa una sal nueva en cada hash), y no se puede calcular sin la
    SECRET_KEY."""
    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"), hashed_password.encode("utf-8"), hashlib.sha256,
    ).hexdigest()[:16]


def token_para(user: User) -> str:
    """El token de sesión de `user`. Único lugar que decide qué lleva adentro.

    El de una cuenta del Portal de Clientes lleva además `hc`, la huella de su clave
    vigente, y get_usuario_autenticado la compara en cada petición: el token deja de
    valer en el instante en que esa clave se cambia o se restablece. La clave temporal
    viaja por correo o WhatsApp; quien haya entrado con ella no conserva la sesión cuando
    el dueño de la cuenta pone la suya. El token del personal no cambia.
    """
    datos = {"sub": str(user.id)}
    if getattr(user, "tipo_acceso", None) == TIPO_PORTAL:
        datos["hc"] = huella_clave(user.hashed_password)
    return create_access_token(datos)


def get_usuario_autenticado(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db)
) -> User:
    """Valida el token y devuelve el usuario activo, SEA DEL TIPO QUE SEA.

    Es el cuerpo que siempre tuvo get_current_user. Solo deben depender de esta función
    las rutas pensadas para una cuenta portal: portal/deps.py (require_portal) y
    GET /auth/me. Todo lo demás usa get_current_user, que además exige cuenta interna.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Credenciales inválidas",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(credentials.credentials, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        user_id: int = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = db.query(User).filter(User.id == int(user_id), User.is_active == 1).first()
    if user is None:
        raise credentials_exception
    # Cuenta portal: el token tiene que haberse emitido con la clave que la cuenta tiene
    # HOY (ver token_para). Un token sin huella, o con la de una clave anterior, no vale.
    if getattr(user, "tipo_acceso", None) == TIPO_PORTAL:
        huella = str(payload.get("hc") or "")
        if not hmac.compare_digest(huella, huella_clave(user.hashed_password)):
            raise credentials_exception
    return user


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db)
) -> User:
    """La puerta de TODO el ERP: usuario autenticado Y de tipo 'interno'.

    El Portal de Clientes usa el MISMO login, así que el token de un cliente es un token
    válido. Sin este corte, ese token serviría en cada endpoint interno que solo pide
    sesión (cotizaciones con margen, compras con el proveedor, contabilidad): un router
    propio para el portal aísla lo que el portal SIRVE, no lo que el token ALCANZA.
    El corte va acá y no router por router porque esta es la única dependencia por la
    que pasan todos, incluidos require_empresa y require_rol.

    Falla cerrado: se deja pasar solo lo que es exactamente 'interno'. Un valor
    desconocido, vacío o ausente se niega igual que 'portal'.
    """
    user = get_usuario_autenticado(credentials, db)
    if getattr(user, "tipo_acceso", None) != TIPO_INTERNO:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=DETALLE_CUENTA_PORTAL,
        )
    return user
