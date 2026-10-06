/**
 * Política ÚNICA de errores de sesión y permiso.
 *
 * POR QUÉ EXISTE
 *   Hay DOS clientes axios (`services/api.ts` para MachParts y `services/monzaApi.ts`
 *   para MonzaParts). Unificarlos sería tocar más de mil líneas de un ERP en producción;
 *   en cambio comparten esta función, que es la política, y cada uno conserva su
 *   `baseURL` y sus interceptores. Con una sola política:
 *     · el 401 se trata igual en las dos marcas, y el cierre de sesión pasa siempre por
 *       `logout()` del store (borrar localStorage a mano deja el token vivo en memoria
 *       hasta recargar);
 *     · el 403 tiene quien lo atienda: sin esto, un usuario sin permiso se queda dentro
 *       del cascarón interno, con las tablas vacías y sin ninguna explicación.
 *
 * LAS TRES CLASES DE ERROR
 *   · sesion  → no hay sesión válida. Desloguear e ir al login. **Es el ÚNICO caso que
 *               navega**, porque es el único sin ambigüedad: sin sesión no hay nada que
 *               mostrar en ninguna pantalla.
 *   · ambito  → la sesión es buena, pero esa marca, ese rol o ese tipo de cuenta
 *               (interna / Portal de Clientes) no alcanzan. Solo se AVISA.
 *   · negocio → un guard de negocio dijo que no. El interceptor no hace nada: lo maneja
 *               el `catch` de la pantalla.
 *
 * EL LOGIN QUEDA FUERA
 *   En `POST /auth/login` se juzgan el correo y la clave del formulario, no el token: su
 *   401 significa «credenciales incorrectas» y no dice nada de la sesión que ya hubiera
 *   en el navegador. Cerrarla por una clave mal tipeada dejaría fuera, además, a las
 *   otras pestañas: `logout()` escribe en localStorage y `stores/authStore.ts` hace que
 *   todas adopten el cambio. Ver `RUTA_LOGIN`.
 *   De lo que el backend le responde al navegador, es el único 401 que no habla de la
 *   sesión. Un endpoint nuevo que niegue por otro motivo tiene que usar otro código (400,
 *   403, 409): con un 401, esta política le cerraría la sesión al usuario.
 *
 * UNA PETICIÓN QUE VIAJÓ CON OTRO TOKEN TAMPOCO CUENTA
 *   El token puede cambiar con una petición en vuelo: el cambio de clave del Portal de
 *   Clientes emite uno nuevo (el anterior muere al instante), y otra pestaña puede entrar
 *   con otra cuenta. El 401 de esa petición habla del token ANTERIOR; cerrar la sesión
 *   por él expulsaría a quien acaba de renovarla. Por eso, si la petición fallida llevaba
 *   un Authorization distinto del token vigente, acá no se hace nada.
 *
 * POR QUÉ EL 403 DE ÁMBITO NO NAVEGA
 *   Mandar al usuario a su inicio haría tres daños y no aporta nada:
 *     1. Un `PUT` denegado lo sacaría de un formulario a medio llenar.
 *     2. El aviso moriría en la recarga: llegaría a otra pantalla sin explicación.
 *     3. Taparía los mensajes que las pantallas ya traen escritos, que son mejores.
 *        `MonzaLeadsPage` dice "Avisa al administrador para que revise la marca asignada
 *        a tu cuenta"; un aviso genérico más una redirección lo esconderían.
 *   Quedarse donde está, con un aviso, no pierde nada y no rompe nada.
 *
 * NOTA SOBRE LAS RESPUESTAS `blob`
 *   Cuando la petición pide `responseType: 'blob'` (fotos, PDF), `err.response.data` es
 *   un Blob y `data.detail` es `undefined`. Esos 403 caen en `negocio`, o sea: no pasa
 *   nada. Es el comportamiento correcto — no hay que "arreglarlo".
 *
 * NOTA SOBRE EL 403 "Not authenticated"
 *   `backend/auth.py` usa `HTTPBearer()`, que responde **403** —no 401— cuando falta el
 *   header Authorization. `backend/main.py` lo traduce a 401, pero `DETALLES_SESION` se
 *   conserva como cinturón: si el frontend se publica antes que el backend, o si ese
 *   handler se quita, el usuario sin token sigue llegando al login en vez de quedar
 *   mirando avisos para siempre.
 *
 * LOS TEXTOS DE ESTE ARCHIVO SON GEMELOS DE LOS DEL BACKEND
 *   `DETALLES_SESION` ↔ `backend/auth_errors.py`; `DETALLES_AMBITO` ↔ `empresa_guard.py`,
 *   `role_guard.py` y `auth.py` (`DETALLE_CUENTA_PORTAL`); `RUTA_LOGIN` ↔ `authAPI.login` en
 *   `services/api.ts`.
 *   Si un lado cambia y el otro no, el aviso deja de salir sin que nada falle a la vista:
 *   lo detecta `backend/routers/tests/test_auth_errors_espejo_frontend.py`.
 */
import axios from 'axios'
import toast from 'react-hot-toast'
import { useAuthStore } from '../stores/authStore'

/** 403 que en realidad significan "no hay sesión" (los emite FastAPI/HTTPBearer). */
const DETALLES_SESION = ['Not authenticated', 'Invalid authentication credentials']

/**
 * 403 de ámbito: `backend/empresa_guard.py`, `backend/role_guard.py` y `backend/auth.py`
 * (`DETALLE_CUENTA_PORTAL`: `get_current_user` niega TODO el ERP a una cuenta del Portal
 * de Clientes). Este último no debería verse nunca —`PrivateRoute` desvía a la sesión
 * portal antes de que pida nada interno—, pero si alguien manipula su localStorage para
 * entrar al cascarón, sin esta línea cae en `negocio` y ve tablas vacías sin aviso.
 */
const DETALLES_AMBITO = [
  'Acceso restringido a esta empresa',
  'Acceso restringido para tu rol',
  'Acceso restringido: esta cuenta es del Portal de Clientes',
]

const MENSAJE_AMBITO = 'Tu cuenta no tiene acceso a esta sección.'

/**
 * La única petición cuyos errores no hablan de la sesión vigente (ver «EL LOGIN QUEDA
 * FUERA» en el encabezado). Es la ruta tal como la pide `authAPI.login`, sin `baseURL`.
 */
const RUTA_LOGIN = '/auth/login'

export type ClaseError = 'sesion' | 'ambito' | 'negocio' | 'ninguna'

export function clasificarError(status?: number, detail?: unknown): ClaseError {
  const d = typeof detail === 'string' ? detail : ''
  if (status === 401) return 'sesion'
  if (status === 403) {
    if (DETALLES_SESION.includes(d)) return 'sesion'
    // `startsWith` y no igualdad exacta: si alguien enriquece el mensaje del backend
    // (por ejemplo agregándole la marca entre paréntesis), esto sigue clasificando
    // bien en vez de degradar en silencio a `negocio`.
    if (DETALLES_AMBITO.some((a) => d.startsWith(a))) return 'ambito'
    return 'negocio'
  }
  return 'ninguna'
}

/**
 * Una sola redirección al login por carga de página.
 *
 * Sin esto: una pantalla dispara N peticiones en paralelo, las N fallan con el mismo
 * 401 y las N intentan navegar. Con `replace` eso puede dejar el historial en un estado
 * raro y, en el peor caso, competir entre sí. No hace falta volver a ponerla en false:
 * `window.location.replace` recarga la página y este módulo empieza de cero.
 */
let yaRedirigiendo = false

function cerrarSesion() {
  try {
    useAuthStore.getState().logout()
  } catch {
    // El middleware `persist` escribe en localStorage y en modo privado puede lanzar.
    // Da igual: lo importante es salir de acá e ir al login.
  }
}

/**
 * Aplica la política. Devuelve la clase aplicada, para que el llamador pueda loguear.
 * NUNCA lanza: es un interceptor, y una excepción acá se traga el error original.
 */
export function manejarErrorAuth(err: unknown): ClaseError {
  const esAxios = axios.isAxiosError(err)
  // Antes de clasificar: nada de lo que responda el login cambia la sesión vigente. Su
  // error lo muestra LoginPage.
  if (esAxios && (err.config?.url ?? '').endsWith(RUTA_LOGIN)) return 'ninguna'
  // Ni lo que responda una petición que viajó con OTRO token (ver el encabezado).
  const autorizacion = esAxios ? err.config?.headers?.Authorization : undefined
  const tokenVigente = useAuthStore.getState().token
  if (typeof autorizacion === 'string' && tokenVigente && autorizacion !== `Bearer ${tokenVigente}`) {
    return 'ninguna'
  }

  const status = esAxios ? err.response?.status : undefined
  const detail = esAxios
    ? (err.response?.data as { detail?: unknown } | undefined)?.detail
    : undefined
  const clase = clasificarError(status, detail)

  if (clase === 'sesion') {
    cerrarSesion()
    if (!yaRedirigiendo && window.location.pathname !== '/login') {
      yaRedirigiendo = true
      // `replace` y no `href`: si dejáramos la pantalla protegida en el historial, el
      // botón Atrás vuelve ahí, vuelve a pedir y vuelve a fallar.
      window.location.replace('/login')
    }
    return clase
  }

  if (clase === 'ambito') {
    // `id` fijo: react-hot-toast colapsa en UNO los N avisos de las N peticiones que
    // fallaron a la vez en la misma pantalla. No se navega (ver el encabezado).
    toast.error(MENSAJE_AMBITO, { id: 'error-403-ambito' })
    return clase
  }

  // 'negocio' y 'ninguna': el interceptor no hace NADA. Lo maneja la pantalla.
  return clase
}
