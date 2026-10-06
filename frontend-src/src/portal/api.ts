// Cliente API del Portal de Clientes. Reutiliza la instancia axios compartida (token +
// política de 401/403 de services/authErrors.ts ya configurados en services/api.ts).
import { detalleDe, estadoHttp } from '../services/httpErrors'
import api from '../services/api'
import type {
  PortalYo, ClienteAdmin, CuentasResp, CuentaCreada, ClaveRestablecida, EstadoCambiado,
  CuentaReactivada, Vinculacion, PerfilPortal, Tablero, OrdenesParams, OrdenesResp,
  OrdenDetalle, SeguimientoItem, FotoPublicada, FotosPublicadas,
} from './types'

// Payloads (espejo de los schemas Pydantic del backend)
interface CambioClavePayload {
  clave_actual: string
  clave_nueva: string
}

interface CuentaNuevaPayload {
  email: string
  nombre: string
  cliente_ids: number[]
  perfil: PerfilPortal
}

/**
 * Lo que ve el CLIENTE. Solo responde a cuentas tipo_acceso='portal'.
 *
 * `yo` y `cambiarClave` atienden a cualquier cuenta portal. Todo lo demás responde 403
 * mientras la cuenta siga con la clave temporal (`PortalYo.debe_cambiar_clave`).
 * Una orden, un ítem, una foto o un documento que no es del cliente responde 404.
 */
export const portalAPI = {
  yo: () => api.get<PortalYo>('/portal/yo'),
  // El token de la sesión va atado a la clave (backend/auth.py::token_para): al cambiarla,
  // el anterior deja de valer y la respuesta trae el nuevo. Quien llame TIENE que
  // guardarlo (CambiarClaveForm.tsx) o la petición siguiente responde 401.
  cambiarClave: (data: CambioClavePayload) =>
    api.post<{ ok: boolean; access_token: string }>('/portal/yo/clave', data),

  tablero: (anio?: number) => api.get<Tablero>('/portal/resumen', { params: { anio } }),
  ordenes: (params: OrdenesParams) => api.get<OrdenesResp>('/portal/ordenes', { params }),
  orden: (ordenId: number) => api.get<OrdenDetalle>(`/portal/ordenes/${ordenId}`),
  item: (ordenId: number, itemId: number) =>
    api.get<SeguimientoItem>(`/portal/ordenes/${ordenId}/items/${itemId}`),

  // Archivos. Llegan como bytes por esta misma API (con el token de la sesión): no hay
  // un enlace que se pueda copiar y abrir sin sesión. 502 = el documento existe pero no
  // se pudo obtener ahora; 429 = demasiadas descargas seguidas (el tope es por cuenta y
  // por minuto: backend/portal/cupo.py). En los dos el `detail` trae el texto para el
  // cliente... dentro de un Blob (archivos.ts::mensajeDeFalla lo abre).
  foto: (fotoId: number) => api.get<Blob>(`/portal/fotos/${fotoId}`, { responseType: 'blob' }),
  guiaPdf: (despachoId: number) => api.get<Blob>(`/portal/guias/${despachoId}/pdf`, { responseType: 'blob' }),
  guiaFirmada: (despachoId: number) =>
    api.get<Blob>(`/portal/guias/${despachoId}/firmada`, { responseType: 'blob' }),
  facturaPdf: (facturaId: number) => api.get<Blob>(`/portal/facturas/${facturaId}/pdf`, { responseType: 'blob' }),
}

/** Lo que opera el PERSONAL de Grupo AM (empresa 'mineria') sobre el portal. */
export const portalAdminAPI = {
  clientes: () => api.get<ClienteAdmin[]>('/portal-admin/clientes'),
  cuentas: () => api.get<CuentasResp>('/portal-admin/cuentas'),
  crearCuenta: (data: CuentaNuevaPayload) => api.post<CuentaCreada>('/portal-admin/cuentas', data),
  restablecerClave: (id: number) =>
    api.post<ClaveRestablecida>(`/portal-admin/cuentas/${id}/restablecer-clave`),
  // Reactivar NO renueva la clave temporal: la respuesta dice si ya venció (y entonces
  // hace falta «Restablecer clave» para que el contacto pueda entrar).
  reactivar: (id: number) => api.post<CuentaReactivada>(`/portal-admin/cuentas/${id}/reactivar`),
  desactivar: (id: number) => api.post<EstadoCambiado>(`/portal-admin/cuentas/${id}/desactivar`),
  // Borra la cuenta con sus vínculos (204). Es lo que destraba corregir el RUT o borrar
  // una ficha de cliente (routers/clientes.py responde 409 mientras la ficha tenga
  // cuentas) y lo que libera un correo mal escrito. 409 si la cuenta tiene historial.
  eliminar: (id: number) => api.delete<void>(`/portal-admin/cuentas/${id}`),
  // Ensayo: qué pasaría al vincular, sin escribir nada.
  vinculacion: () => api.get<Vinculacion>('/portal-admin/vinculacion'),
  aplicarVinculacion: () => api.post<Vinculacion>('/portal-admin/vinculacion/aplicar'),

  // Fotos para el cliente (pantalla Despachos). SUBIR ES PUBLICAR: no hay paso de
  // aprobación. 409 = el ítem ya tiene el máximo de fotos o está en reclamo al proveedor;
  // 400 = el archivo no es una imagen aceptada (el `detail` lo explica).
  fotosDeItem: (itemId: number) => api.get<FotosPublicadas>(`/portal-admin/items/${itemId}/fotos`),
  subirFoto: (itemId: number, archivo: File) => {
    const cuerpo = new FormData()
    cuerpo.append('file', archivo)
    return api.post<FotoPublicada>(`/portal-admin/items/${itemId}/fotos`, cuerpo, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },
  fotoPublicada: (fotoId: number) => api.get<Blob>(`/portal-admin/fotos/${fotoId}`, { responseType: 'blob' }),
  borrarFoto: (fotoId: number) => api.delete<void>(`/portal-admin/fotos/${fotoId}`),
}

/**
 * El texto de un 403, que el backend escribe para el cliente (cuenta suspendida por
 * inactividad, clave temporal sin cambiar). null para cualquier otro error: el `detail`
 * de un 503 trae instrucciones para el equipo técnico y no se muestra.
 */
export function motivoDeBloqueo(err: unknown): string | null {
  return estadoHttp(err) === 403 ? detalleDe(err) : null
}

// Las pantallas del portal leen los errores con estos dos ayudantes. Son genéricos de
// axios y viven en services/; se reexportan para que el portal importe todo de acá.
export { detalleDe, estadoHttp }
