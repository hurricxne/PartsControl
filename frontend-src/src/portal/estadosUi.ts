// Cómo se VE un estado en el portal del cliente: su tono. Del tono salen la forma del
// ícono y el color (piezas.tsx::InsigniaEstado y las reglas [data-tono] de portal.css).
//
// La PALABRA no se decide acá: es `estado.etiqueta`, la escribe el backend
// (backend/portal/estados.py) y se pinta tal cual. Tampoco se importan etiquetas ni
// colores de las pantallas internas del ERP: allá los estados son otros (los del
// personal) y cambian sin avisar.
import type { ClaveEstado } from './types'

//   ok        ✓  la mercadería ya salió hacia el cliente
//   curso     ●  viene avanzando (compra, tránsito, aduana, bodega)
//   pendiente ○  pedido tomado, todavía sin movimiento
//   atencion  !  incidencia: el ítem se está reponiendo, o no se recibió en la entrega
export type Tono = 'ok' | 'curso' | 'pendiente' | 'atencion'

// Record completo y no un switch con `default`: agregar una clave a ClaveEstado obliga a
// decidir su tono acá (deja de compilar hasta hacerlo).
const TONO_POR_CLAVE: Record<ClaveEstado, Tono> = {
  confirmado: 'pendiente',
  en_compra: 'curso',
  listo_origen: 'curso',
  en_transito: 'curso',
  en_aduana: 'curso',
  en_recepcion: 'curso',
  en_bodega: 'curso',
  despachado: 'ok',
  facturado: 'ok',
  en_gestion: 'atencion',
  no_recibido: 'atencion',
}

/**
 * Tono de un estado. Una clave que esta pantalla no conoce (backend más nuevo que el
 * frontend) cae en 'curso': la etiqueta igual se lee, y un tono neutro no promete una
 * entrega ni inventa una incidencia.
 */
export function tonoDeEstado(clave: ClaveEstado): Tono {
  return TONO_POR_CLAVE[clave] ?? 'curso'
}
