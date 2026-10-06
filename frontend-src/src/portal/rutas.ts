// Las direcciones del portal del cliente, en un solo lugar. App.tsx declara las rutas;
// acá se arman los enlaces hacia ellas (y se lee lo que viene en ellas).
export const RUTA_DEL_PORTAL = '/portal'
export const RUTA_DE_PEDIDOS = '/portal/pedidos'

export const rutaDeOrden = (ordenId: number): string => `${RUTA_DE_PEDIDOS}/${ordenId}`
export const rutaDeItem = (ordenId: number, itemId: number): string => `${rutaDeOrden(ordenId)}/items/${itemId}`

/** «Mis pedidos» con el mismo año que se estaba mirando y, si se pide, el filtro de estado. */
export function rutaDePedidos(anio: number | undefined, estado?: 'abiertas'): string {
  const parametros = new URLSearchParams()
  if (estado) parametros.set('estado', estado)
  if (anio !== undefined) parametros.set('anio', String(anio))
  const consulta = parametros.toString()
  return consulta ? `${RUTA_DE_PEDIDOS}?${consulta}` : RUTA_DE_PEDIDOS
}

/**
 * Un id que viene en la URL (`:ordenId`, `:itemId`), o null si no es un entero positivo
 * escrito tal cual: «007» no es «7» (el enlace no vino del portal) y se trata como no
 * encontrado, igual en todas las pantallas.
 */
export function idDeRuta(texto: string | undefined): number | null {
  if (!texto || !/^[1-9]\d{0,14}$/.test(texto)) return null
  return Number(texto)
}
