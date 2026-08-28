"""Guard de empresa para los módulos de contabilidad (solo MachParts / Grupo AM).

El backend autentica con `auth.get_current_user` pero NO verifica a qué empresa pertenece
el usuario. Este helper agrega esa verificación a nivel de router, sin tocar `auth.py`
(código compartido del programador): solo lo importa en lectura.

Uso:
    from empresa_guard import require_empresa
    router = APIRouter(..., dependencies=[Depends(require_empresa("mineria"))])
"""
from fastapi import Depends, HTTPException, status

from auth import get_current_user
from models.models import User


def require_empresa(*allowed: str):
    """Dependency de router: deniega con 403 si la empresa del usuario no está en `allowed`.
    Los módulos de contabilidad de MachParts se candan con require_empresa("mineria")."""
    def _guard(current_user: User = Depends(get_current_user)) -> User:
        # Excepción soporte Bigcode: las cuentas @bigcode.cl (equipo interno) acceden a
        # AMBAS marcas — misma excepción documentada del bypass de login (commit 71c4f06).
        # El candado de empresa separa CLIENTES entre marcas, no al equipo de soporte.
        email = (getattr(current_user, "email", "") or "").lower()
        if email.endswith("@bigcode.cl"):
            return current_user
        # Sin default mágico: un usuario sin empresa definida queda bloqueado en TODOS
        # los módulos (más seguro). En la práctica `empresa` es NOT NULL en la BD.
        empresa = getattr(current_user, "empresa", None)
        if empresa not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso restringido a esta empresa",
            )
        return current_user

    return _guard
