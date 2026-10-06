"""Mutaciones de la política de sesión y permiso del frontend:
routers/tests/test_auth_errors_espejo_frontend.py.

Cada entrada rompe UNA regla de services/authErrors.ts, de uno de los dos clientes axios
(services/api.ts, services/monzaApi.ts) o del guard del backend cuyo texto el frontend
repite (empresa_guard.py), y el espejo tiene que ponerse rojo. Los archivos del frontend
se nombran desde la raíz del repo (`frontend-src/src/…`): el arnés los muta en su COPIA
de frontend-src (ver la cabecera del paquete), nunca en el árbol de trabajo.
"""

_AUTH_ERRORS = 'frontend-src/src/services/authErrors.ts'
_API = 'frontend-src/src/services/api.ts'
_MONZA_API = 'frontend-src/src/services/monzaApi.ts'

_EXCLUSION_DEL_LOGIN = "  if (esAxios && (err.config?.url ?? '').endsWith(RUTA_LOGIN)) return 'ninguna'\n"

MUTACIONES = {
    'routers/tests/test_auth_errors_espejo_frontend.py': [
        ('un 401 que viajó con el token anterior vuelve a cerrar la sesión renovada',  # → paso 5
         _AUTH_ERRORS,
         "  if (typeof autorizacion === 'string' && tokenVigente && autorizacion !== `Bearer ${tokenVigente}`) {\n    return 'ninguna'\n  }\n",
         ''),
        # ── El login queda fuera de la política ──────────────────────────────────────
        ('el 401 del login vuelve a cerrar la sesión vigente (sin la exclusión)',  # → paso 2
         _AUTH_ERRORS,
         _EXCLUSION_DEL_LOGIN,
         ''),
        ('la exclusión se ensancha a todo /auth/ (el 401 de /auth/me deja de cerrar la sesión)',  # → paso 4
         _AUTH_ERRORS,
         _EXCLUSION_DEL_LOGIN,
         "  if (esAxios && (err.config?.url ?? '').includes('/auth/')) return 'ninguna'\n"),
        ('RUTA_LOGIN cambia sin cambiar authAPI.login',  # → paso 2 y cinturón
         _AUTH_ERRORS,
         "const RUTA_LOGIN = '/auth/login'\n",
         "const RUTA_LOGIN = '/auth/signin'\n"),
        ('authAPI.login cambia de ruta sin cambiar RUTA_LOGIN',  # → paso 2 y cinturón
         _API,
         "    api.post('/auth/login', { email, password }),\n",
         "    api.post('/auth/entrar', { email, password }),\n"),
        # ── Los dos clientes llaman a la política desde su interceptor ───────────────
        ('el cliente de MachParts deja de llamar a la política desde su interceptor',  # → pasos 3 y 4
         _API,
         '    manejarErrorAuth(err)\n    return Promise.reject(err)\n',
         '    return Promise.reject(err)\n'),
        ('el cliente de MonzaParts deja de llamar a la política desde su interceptor',  # → pasos 3 y 4
         _MONZA_API,
         '    manejarErrorAuth(err);\n    return Promise.reject(err);\n',
         '    return Promise.reject(err);\n'),
        # ── Una sola redirección, un solo aviso ──────────────────────────────────────
        ('dos 401 seguidos navegan dos veces (sin la bandera yaRedirigiendo)',  # → paso 4
         _AUTH_ERRORS,
         '      yaRedirigiendo = true\n',
         ''),
        ('los avisos de ámbito pierden el id que los funde en uno',  # → paso 3
         _AUTH_ERRORS,
         "    toast.error(MENSAJE_AMBITO, { id: 'error-403-ambito' })\n",
         '    toast.error(MENSAJE_AMBITO)\n'),
        # ── Los textos gemelos ───────────────────────────────────────────────────────
        ('el detail de empresa_guard cambia sin tocar DETALLES_AMBITO',  # → paso 1 y cinturón
         'empresa_guard.py',
         '                detail="Acceso restringido a esta empresa",\n',
         '                detail="Acceso denegado a esta empresa",\n'),
        ('DETALLES_AMBITO pierde el texto de empresa_guard',  # → paso 1 y cinturón
         _AUTH_ERRORS,
         "  'Acceso restringido a esta empresa',\n",
         ''),
        ('DETALLES_SESION pierde «Invalid authentication credentials»',  # → paso 1 y cinturón
         _AUTH_ERRORS,
         "const DETALLES_SESION = ['Not authenticated', 'Invalid authentication credentials']\n",
         "const DETALLES_SESION = ['Not authenticated']\n"),
    ],
}
