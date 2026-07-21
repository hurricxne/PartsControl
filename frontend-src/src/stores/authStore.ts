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
}

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
    { name: 'machparts-auth' }
  )
)
