"""Clasificación de los errores de autenticación. Fase 0 del Portal (2026-08-20).

POR QUÉ EXISTE ESTE ARCHIVO
    `auth.py` usa `HTTPBearer()` con el `auto_error=True` que trae por defecto. Eso
    significa que una petición SIN el header `Authorization` no recibe 401 sino
    **403 "Not authenticated"**. Es una decisión de la librería, no del sistema, pero
    mezcla dos situaciones que el frontend necesita distinguir:

        · "no tengo sesión"      -> hay que mandar al login
        · "mi sesión no alcanza" -> hay que avisar, NO desloguear

    `main.py` traduce la primera a 401. Esta función es el criterio de esa traducción,
    y vive acá —y no dentro de `main.py`— por dos razones prácticas:

    1. SE PUEDE PROBAR SIN LEVANTAR LA APP. Importar `main` arrastra el `create_all`,
       los 30 routers y el planificador de tareas. Un test que solo quiere verificar
       "¿esta cadena cuenta como falta de sesión?" no debería pagar eso.
    2. LAS DOS CADENAS MÁGICAS QUEDAN EN UN SOLO LUGAR, con su explicación al lado.
       El frontend tiene la lista gemela en `frontend-src/src/services/authErrors.ts`;
       si algún día FastAPI cambia esos textos, hay exactamente dos archivos que tocar
       y este comentario dice cuál es el otro. Que las dos listas sigan iguales lo
       exige `routers/tests/test_auth_errors_espejo_frontend.py`.

SI ALGÚN DÍA FALLA
    Síntoma: el usuario sin token se queda en la pantalla con avisos en vez de ir al
    login. Causa probable: FastAPI cambió el texto del error. Verificarlo con:
        curl -i http://localhost:8002/api/auth/me      (sin header Authorization)
    y agregar el texto nuevo a `DETALLES_SIN_SESION` de acá y a `DETALLES_SESION` del
    archivo gemelo del frontend.
"""

# Textos EXACTOS que emite fastapi.security.HTTPBearer cuando no hay credenciales
# utilizables. No son mensajes nuestros: son de la librería.
DETALLES_SIN_SESION = (
    "Not authenticated",              # falta el header Authorization
    "Invalid authentication credentials",  # el esquema no es Bearer
)


def es_403_por_falta_de_sesion(status_code: int, detail) -> bool:
    """¿Este 403 significa en realidad "no hay sesión"?

    Devuelve False para cualquier 403 propio del negocio (`empresa_guard`,
    `role_guard` y los que se agreguen después): esos SÍ son "tu sesión no alcanza"
    y tienen que seguir llegando al frontend como 403.

    Es deliberadamente estricto —compara contra una lista cerrada— porque el costo de
    los dos errores es asimétrico: reclasificar de más desloguea a alguien que sí
    tenía sesión, y eso es peor que dejar pasar un 403 raro.
    """
    return status_code == 403 and detail in DETALLES_SIN_SESION
