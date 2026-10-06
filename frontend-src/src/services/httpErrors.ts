// Lectura de un error de axios para las pantallas: qué código respondió el servidor y
// qué texto mandó. Sirve con cualquiera de los dos clientes (services/api.ts y
// services/monzaApi.ts). La política de 401/403 no está acá: es services/authErrors.ts.
import axios from 'axios'

/** Código HTTP del error, o undefined si no hubo respuesta (red caída, timeout). */
export function estadoHttp(err: unknown): number | undefined {
  return axios.isAxiosError(err) ? err.response?.status : undefined
}

/**
 * El `detail` del backend SOLO si es un texto. Un 422 de FastAPI trae un arreglo de
 * objetos: pintarlo tal cual muestra "[object Object]" (o rompe el render), así que en
 * ese caso se devuelve null y la pantalla usa su mensaje propio.
 */
export function detalleDe(err: unknown): string | null {
  if (!axios.isAxiosError(err)) return null
  const detail = (err.response?.data as { detail?: unknown } | undefined)?.detail
  return typeof detail === 'string' && detail.trim() ? detail : null
}
