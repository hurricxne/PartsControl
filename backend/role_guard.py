"""Guard de ROL para endpoints que solo deben usar ciertos perfiles (solo MachParts / Grupo AM).

Gemelo de `empresa_guard.py`: agrega verificación de rol a nivel de endpoint sin tocar `auth.py`
(código compartido del programador); solo lo importa en lectura.

Estado actual (2026-07): el modelo `User` todavía NO tiene columna `rol`, así que este guard es
**forward-compatible y permisivo**: mientras el rol no esté provisionado deja pasar (para no bloquear
la operación), y empieza a candar automáticamente en cuanto exista `User.rol` y esté asignado.

Uso:
    from role_guard import require_rol
    @router.put(..., dependencies=[Depends(require_rol("comercial", "contabilidad", "admin"))])

── Activación futura de roles (checklist para el programador) ─────────────────────────────────
Cuando se implemente el módulo de Usuarios/roles, este guard queda operativo sin más cambios:
  1) Agregar la columna:  User.rol = Column(String(50), nullable=True)  (models/models.py)
     + migración idempotente estilo `backend/patch_add_empresa.py` (ADD COLUMN si no existe).
  2) Asignar el rol a cada usuario (admin, gerencia, comercial, abastecimiento, logistica, contabilidad).
  3) Propagar `rol` en la respuesta de login y /me (routers/auth.py) y en el store del frontend
     (frontend-src/src/stores/authStore.ts -> interface User + poblarlo al autenticar).
  4) Desde ese momento, require_rol(...) deniega con 403 a quien no tenga un rol permitido.
"""
from fastapi import Depends, HTTPException, status

from auth import get_current_user
from models.models import User


def require_rol(*allowed: str):
    """Dependency de endpoint: deniega con 403 si el rol del usuario no está en `allowed`.
    Permisivo mientras `rol` no exista/esté sin asignar (ver cabecera: activación futura)."""
    def _guard(current_user: User = Depends(get_current_user)) -> User:
        rol = getattr(current_user, "rol", None)
        # rol aún no provisionado -> permitir (coordinación con el módulo de Usuarios).
        if rol is None:
            return current_user
        if rol not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Acceso restringido para tu rol",
            )
        return current_user

    return _guard
