import { create } from 'zustand'
import { persist } from 'zustand/middleware'

interface User {
  id: number
  email: string
  nombre: string
  empresa: 'mineria' | 'automotriz'
  // Forward-compat: hoy login/me no lo devuelven (queda undefined). Cuando el backend propague
  // el rol, los gates de "Editar OC" y el guard require_rol del backend empiezan a candar.
  rol?: string
  // Portal de Clientes (Fase 1): 'portal' = contacto de un cliente, entra SOLO a /portal.
  // OPCIONAL A PROPÓSITO: las sesiones del personal ya guardadas en localStorage
  // ('machparts-auth') nacieron antes de este campo y no lo traen. Por eso `undefined`
  // se lee como 'interno' en todo el frontend: la única comparación válida es
  // `=== 'portal'` (ver esCuentaPortal). Exigir `=== 'interno'` expulsaría a todo el
  // personal con sesión abierta el día del deploy.
  tipo_acceso?: 'interno' | 'portal'
}

/**
 * ¿La sesión es de una cuenta del Portal de Clientes?
 *
 * ÚNICO lugar donde se decide, para que ninguna pantalla lo compare a su manera y
 * alguna termine escribiendo `!== 'interno'` (que trata a las sesiones antiguas, sin el
 * campo, como portal). Es solo ruteo: la barrera real es del backend, que responde 403
 * a un token portal en todo el ERP.
 */
export const esCuentaPortal = (
  user: Pick<User, 'tipo_acceso'> | null | undefined
): boolean => user?.tipo_acceso === 'portal'

// Llave de localStorage. NO cambiarla: otras pantallas leen el token directo de ahí.
const CLAVE_SESION = 'machparts-auth'

interface AuthState {
  token: string | null
  user: User | null
  setAuth: (token: string, user: User) => void
  logout: () => void
  isAuthenticated: () => boolean
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      token: null,
      user: null,
      setAuth: (token, user) => set({ token, user }),
      logout: () => set({ token: null, user: null }),
      isAuthenticated: () => !!get().token,
    }),
    { name: CLAVE_SESION }
  )
)

// UNA sola sesión por navegador, igual en todas las pestañas. `persist` escribe en
// localStorage pero no escucha a las otras pestañas: si en la pestaña B se inicia sesión
// con otra cuenta (el caso de todos los días desde que existe el portal: el personal
// prueba la cuenta de un cliente en su propio navegador), la pestaña A seguía con el
// token anterior EN MEMORIA mientras todo lo que lee el token directo de localStorage
// (services/monzaApi.ts, subidas y descargas con fetch) ya mandaba el nuevo: mitad
// interna, mitad portal, con 403 sin explicación. Al releer el store, la pestaña A
// adopta la sesión vigente y PrivateRoute / PortalRoute la rutean como corresponde.
// `rehydrate` no vuelve a escribir en localStorage, así que las pestañas no se rebotan
// el evento entre sí. Sin `newValue` (llave borrada a mano) no hay nada que adoptar.
if (typeof window !== 'undefined') {
  window.addEventListener('storage', (e) => {
    if (e.key === CLAVE_SESION && e.newValue) void useAuthStore.persist.rehydrate()
  })
}
