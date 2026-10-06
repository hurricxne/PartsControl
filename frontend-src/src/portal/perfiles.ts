// Cómo se le NOMBRA al personal cada perfil de una cuenta del portal. Un solo lugar para
// que el formulario de alta y la tabla de cuentas no lo escriban cada uno a su manera.
//
// Las descripciones le dicen al personal qué va a ver el contacto con cada perfil
// (backend/portal/deps.py::PortalScope.ve_montos decide lo que de verdad se le manda).
import type { PerfilPortal } from './types'

interface PerfilInfo {
  label: string
  descripcion: string
}

export const PERFILES: Record<PerfilPortal, PerfilInfo> = {
  procurement: {
    label: 'Adquisiciones',
    descripcion: 'Verá sus pedidos con montos.',
  },
  bodega: {
    label: 'Bodega',
    descripcion: 'Verá lo que viene en camino, sin montos.',
  },
}

/** Etiqueta de un perfil; si el backend agrega uno que esta pantalla no conoce, se
 *  muestra el valor crudo en vez de un hueco. */
export const etiquetaPerfil = (perfil: string): string =>
  PERFILES[perfil as PerfilPortal]?.label ?? perfil
