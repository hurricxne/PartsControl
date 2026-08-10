import axios from 'axios'
import { useAuthStore } from '../stores/authStore'

const api = axios.create({
  baseURL: '/api',
  timeout: 60_000,
})

api.interceptors.request.use((config) => {
  const token = useAuthStore.getState().token
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

api.interceptors.response.use(
  (res) => res,
  (err) => {
    if (err.response?.status === 401) {
      useAuthStore.getState().logout()
      window.location.href = '/login'
    }
    return Promise.reject(err)
  }
)

export default api

// Auth
export const authAPI = {
  login: (email: string, password: string) =>
    api.post('/auth/login', { email, password }),
  me: () => api.get('/auth/me'),
  users: () => api.get('/auth/users'),
}

// Worker
export const workerAPI = {
  status: () => api.get('/worker/status'),
}

// Cotizaciones
export const cotizacionesAPI = {
  upload: (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return api.post('/cotizaciones/upload', fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 300_000,
    })
  },
  uploadVenta: (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return api.post('/cotizaciones/upload-venta', fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 120_000,
    })
  },
  list: () => api.get('/cotizaciones/'),
  get: (id: number) => api.get(`/cotizaciones/${id}`),
  status: (id: number) => api.get(`/cotizaciones/${id}/status`),
  reprocesar: (id: number) => api.post(`/cotizaciones/${id}/reprocesar`),
  updateFase: (id: number, fase: string) => api.patch(`/cotizaciones/${id}/fase`, { fase }),
  eliminar: (id: number) => api.delete(`/cotizaciones/${id}`),
  downloadUrl: (id: number) => `/api/cotizaciones/${id}/download`,
  download: (id: number) => api.get(`/cotizaciones/${id}/download`, { responseType: 'arraybuffer' }),
  crearManual: (data: {
    cliente: string; referencia: string;
    rut_cliente?: string; contacto_cliente?: string; email_cliente?: string;
    terminos_condiciones?: string;
    items: any[]
  }) => api.post('/cotizaciones/manual', data),
  getClientes: () => api.get('/cotizaciones/clientes'),
}

// Cotizador Editor
export const cotizadorAPI = {
  getConfig: () => api.get('/cotizador/config'),
  updateConfig: (data: Record<string, number>) => api.put('/cotizador/config', data),
  get: (id: number) => api.get(`/cotizador/${id}`),
  updateMeta: (id: number, data: Record<string, string>) => api.put(`/cotizador/${id}/meta`, data),
  updateItem: (id: number, itemId: number, data: Record<string, any>) =>
    api.put(`/cotizador/${id}/items/${itemId}`, data),
  deleteItem: (id: number, itemId: number) =>
    api.delete(`/cotizador/${id}/items/${itemId}`),
  generarFormal: (id: number) => api.post(`/cotizador/${id}/formal`),
  buscarFinning: (id: number) => api.post(`/cotizador/${id}/buscar-finning`),
  finningProgress: (id: number) => api.get(`/cotizador/${id}/finning-progress`),
  downloadFormalUrl: (id: number) => `/api/cotizador/${id}/formal/download`,
  downloadFormal: (id: number) => api.get(`/cotizador/${id}/formal/download`, { responseType: 'arraybuffer' }),
  pdfUrl: (id: number) => `/api/cotizador/${id}/pdf`,
  downloadPdf: (id: number) => api.get(`/cotizador/${id}/pdf`, { responseType: 'arraybuffer' }),
  getVersiones: (id: number) => api.get(`/cotizador/${id}/versiones`),
  updateTerminos: (id: number, text: string) => api.put(`/cotizador/${id}/terminos`, { terminos: text }),
  guardarVersion: (id: number) => api.post(`/cotizador/${id}/versiones`),
  getVersionDetalle: (id: number, vnum: number) => api.get(`/cotizador/${id}/versiones/${vnum}`),
  restaurarVersion: (id: number, vnum: number) => api.post(`/cotizador/${id}/versiones/${vnum}/restaurar`),
  bulkUpdateEstados: (id: number, items: {id: number, estado_item: string}[]) => api.post(`/cotizador/${id}/items/estados`, { items }),
}

// Clientes
export const clientesAPI = {
  list: (q?: string) => api.get('/clientes/', { params: q ? { q } : {} }),
  create: (data: Record<string, string>) => api.post('/clientes/', data),
  update: (id: number, data: Record<string, string>) => api.put(`/clientes/${id}`, data),
  byRut: (rut: string) => api.get(`/clientes/by-rut/${encodeURIComponent(rut)}`),
  upsert: (data: Record<string, string>) => api.post('/clientes/upsert', data),
  remove: (id: number) => api.delete(`/clientes/${id}`),
}

// Ventas
export const ventasAPI = {
  list: (q?: string, periodo?: string) =>
    api.get('/ventas/', { params: { ...(q ? { q } : {}), ...(periodo ? { periodo } : {}) } }),
  kpis: (periodo?: string) =>
    api.get('/ventas/kpis', { params: periodo ? { periodo } : {} }),
  mensual: (anio?: number) =>
    api.get('/ventas/mensual', { params: anio ? { anio } : {} }),
  exportUrl: (periodo?: string, q?: string) => {
    const params = new URLSearchParams()
    if (periodo) params.set('periodo', periodo)
    if (q) params.set('q', q)
    const qs = params.toString()
    return `/api/ventas/export${qs ? '?' + qs : ''}`
  },
}

// Compras
export const comprasAPI = {
  listOcCliente: () => api.get('/compras/oc-cliente'),
  crearOcCliente: (data: Record<string, any>) => api.post('/compras/oc-cliente', data),
  actualizarOcCliente: (id: number, data: Record<string, any>) => api.put(`/compras/oc-cliente/${id}`, data),
  listOcProveedor: () => api.get('/compras/oc-proveedor'),
  crearOcProveedor: (data: Record<string, any>) => api.post('/compras/oc-proveedor', data),
  actualizarOcProveedor: (id: number, data: Record<string, any>) => api.put(`/compras/oc-proveedor/${id}`, data),
  asignarItems: (ocpId: number, data: Record<string, any>) =>
    api.post(`/compras/oc-proveedor/${ocpId}/items`, data),
  // Asignación PARCIAL a OC-Proveedor: el backend PARTE la línea (lo asignado queda
  // 'comprado' con su vínculo; el remanente vuelve al panel como línea hermana
  // 'cerrado'). Contrato: { oc_cliente_id, items: [{ item_id, cantidad?, plazo_dias_prov? }] }
  // — `cantidad` AUSENTE = línea entera (sentinela del backend, jamás mandar 0).
  // Respuesta: { ok, asignados, particiones: [{ item_id, qty_asignada, qty_original,
  // qty_remanente, item_id_remanente }] }.
  asignarItemsParcial: (ocpId: number, data: Record<string, any>) =>
    api.post(`/compras/oc-proveedor/${ocpId}/items-parcial`, data),
  listProveedores: () => api.get('/compras/proveedores'),
  createProveedor: (data: Record<string, any>) => api.post('/compras/proveedores', data),
  updateProveedor: (id: number, data: Record<string, any>) => api.put(`/compras/proveedores/${id}`, data),
  deleteProveedor: (id: number) => api.delete(`/compras/proveedores/${id}`),
  getCounts: () => api.get('/compras/counts'),
  prepararItems: (item_ids: number[]) => api.post('/compras/items/preparar', { item_ids }),
  prepararItemsParcial: (items: { item_id: number; cantidad?: number }[]) =>
    api.post('/compras/items/preparar-parcial', { items }),
  updatePreEmbarqueItem: (preId: number, itemId: number, data: Record<string, any>) =>
    api.patch(`/compras/pre-embarques/${preId}/items/${itemId}`, data),
  getItemsComprados: () => api.get('/compras/items/comprados'),
  getItemsPreparados: () => api.get('/compras/items/preparados'),
  // Pre-Embarques
  listPreEmbarques: () => api.get('/compras/pre-embarques'),
  crearPreEmbarque: (data: Record<string, any>) => api.post('/compras/pre-embarques', data),
  cerrarPreEmbarque: (id: number, data: Record<string, any>) => api.post(`/compras/pre-embarques/${id}/cerrar`, data),
  addItemPreEmbarque: (preId: number, itemId: number) => api.post(`/compras/pre-embarques/${preId}/items`, { item_id: itemId }),
  removeItemPreEmbarque: (preId: number, itemId: number) => api.delete(`/compras/pre-embarques/${preId}/items/${itemId}`),
  // Embarques
  listEmbarques: () => api.get('/compras/embarques-list'),
  crearEmbarque: (data: Record<string, any>) => api.post('/compras/embarques-list', data),
  actualizarEmbarque: (id: number, data: Record<string, any>) => api.put(`/compras/embarques-list/${id}`, data),
  downloadOcClienteExcel: (id: number) => api.get(`/compras/oc-cliente/${id}/excel`, { responseType: 'arraybuffer' }),
  updatePreEmbarque: (id: number, data: Record<string, any>) => api.put(`/compras/pre-embarques/${id}`, data),
  getEmbarque: (id: number) => api.get(`/compras/embarques-list/${id}`),
  desembarcarItem: (embId: number, embarqueItemId: number) => api.delete(`/compras/embarques-list/${embId}/items/${embarqueItemId}`),
  // Documentos adicionales (N) del embarque — botón "Otros"
  listEmbarqueDocs: (embId: number) => api.get(`/compras/embarques-list/${embId}/docs`),
  addEmbarqueDoc: (embId: number, data: { nombre: string; archivo: string }) =>
    api.post(`/compras/embarques-list/${embId}/docs`, data),
  deleteEmbarqueDoc: (embId: number, docId: number) =>
    api.delete(`/compras/embarques-list/${embId}/docs/${docId}`),
  downloadDoc: (filename: string) =>
    api.get(`/despachos/docs/${filename}`, { responseType: 'arraybuffer' }),
  updatePrecioUsd: (itemId: number, unit_price_usd: number) =>
    api.patch(`/compras/items/${itemId}/precio-usd`, { unit_price_usd }),
}

// Facturas Proveedor
export const facturasAPI = {
  parsePdf: (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return api.post('/facturas/parse-pdf', fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },
  list: (ocpId: number) => api.get('/facturas', { params: { ocp_id: ocpId } }),
  create: (data: Record<string, any>) => api.post('/facturas', data),
  update: (id: number, data: Record<string, any>) => api.put(`/facturas/${id}`, data),
  updateItem: (facturaId: number, itemId: number, data: Record<string, any>) =>
    api.patch(`/facturas/${facturaId}/items/${itemId}`, data),
  remove: (id: number) => api.delete(`/facturas/${id}`),
  validarPreEmbarque: (preEmbarqueId: number, invoxOcpIds?: number[]) =>
    api.get(`/facturas/validar-pre-embarque/${preEmbarqueId}`, {
      params: invoxOcpIds && invoxOcpIds.length > 0
        ? { invox_ocp_ids: invoxOcpIds.join(',') }
        : undefined,
    }),
}


// Notificaciones
export const notificacionesAPI = {
  list: () => api.get('/notificaciones'),
  countNoLeidas: () => api.get('/notificaciones/no-leidas/count'),
  marcarLeida: (id: number) => api.patch(`/notificaciones/${id}/leida`),
  marcarTodasLeidas: () => api.patch('/notificaciones/marcar-todas-leidas'),
}

// Bodega
export const bodegaAPI = {
  listEmbarques: () => api.get('/bodega/embarques'),
  historialEmbarques: () => api.get('/bodega/embarques/historial'),
  getEmbarque: (id: number) => api.get(`/bodega/embarques/${id}`),
  iniciarRecepcion: (embarqueId: number) => api.post(`/bodega/embarques/${embarqueId}/recibir`),
  getRecepcion: (recepcionId: number) => api.get(`/bodega/recepciones/${recepcionId}`),
  marcarItem: (recepcionId: number, itemId: number, data: Record<string, any>) =>
    api.patch(`/bodega/recepciones/${recepcionId}/items/${itemId}`, data),
  subirFoto: (recepcionId: number, riId: number, file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return api.post(`/bodega/recepciones/${recepcionId}/items/${riId}/foto`, fd, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },
  eliminarFoto: (recepcionId: number, riId: number, fotoId: number) =>
    api.delete(`/bodega/recepciones/${recepcionId}/items/${riId}/foto/${fotoId}`),
  cerrarRecepcion: (recepcionId: number, obs?: string) =>
    api.post(`/bodega/recepciones/${recepcionId}/cerrar`, { observacion_general: obs }),
  listReclamos: () => api.get('/bodega/reclamos'),
  actualizarReclamo: (id: number, data: Record<string, any>) =>
    api.patch(`/bodega/reclamos/${id}`, data),
  listoParaDespachar: () => api.get('/bodega/ventas/listo-para-despachar'),
  alertasPlazo: () => api.get('/bodega/ventas/alertas-plazo-critico'),
}

// --- Contabilidad (Ventas + Facturas/Cobranzas/Factoring) ---
// KPIs del período (GET /contabilidad/kpis). Se declara completo a propósito: la pantalla
// pintaba 5 tarjetas y el backend devuelve 6 montos, así que `anticipo_factoring_clp`
// —lo que YA adelantó el factor, dinero recibido que no viene del cliente— quedaba
// invisible y "cobrado" no cuadraba con lo que entró por caja. MonzaParts lo pinta.
export interface ContabKpis {
  n_facturas: number
  facturado_clp: number
  /** Todo lo cobrado: pagos del cliente + anticipos y liquidaciones del factor. */
  cobrado_clp: number
  /** Solo lo que pagó el CLIENTE (excluye los medios de factoring). */
  cobrado_cliente_clp: number
  /** Lo que puso el FACTOR (adelanto + retención liquidada). cobrado = cliente + factor. */
  anticipo_factoring_clp: number
  por_cobrar_clp: number
  /** Excluye las facturas con factoring vigente: ese riesgo de cobro lo tomó el factor. */
  vencido_clp: number
  en_factoring_clp: number
}

export const contabilidadAPI = {
  // Ventas (agrupado por OC, expandible a ítem — solo venta)
  listVentas: (q?: string, periodo?: string) =>
    api.get('/contabilidad/ventas', { params: { q: q || undefined, periodo: periodo || undefined } }),
  ventaDetalle: (ocId: number) => api.get(`/contabilidad/ventas/${ocId}`),
  despachosFacturables: (ocId: number) => api.get(`/contabilidad/ventas/${ocId}/despachos-facturables`),
  // Facturas / Cuentas por cobrar
  listFacturas: (estado?: string, q?: string) =>
    api.get('/contabilidad/facturas', { params: { estado: estado || undefined, q: q || undefined } }),
  // Previsualiza la factura (líneas + neto/IVA/total + receptor + problemas) sin emitir
  previewFactura: (data: Record<string, any>) => api.post('/contabilidad/facturas/preview', data),
  crearFactura: (data: Record<string, any>) => api.post('/contabilidad/facturas', data),
  eliminarFactura: (id: number) => api.delete(`/contabilidad/facturas/${id}`),
  // Cobranzas
  registrarCobranza: (facturaId: number, data: Record<string, any>) =>
    api.post(`/contabilidad/facturas/${facturaId}/cobranzas`, data),
  eliminarCobranza: (facturaId: number, cobranzaId: number) =>
    api.delete(`/contabilidad/facturas/${facturaId}/cobranzas/${cobranzaId}`),
  // Factoring (por factura)
  setFactoring: (facturaId: number, data: Record<string, any>) =>
    api.post(`/contabilidad/facturas/${facturaId}/factoring`, data),
  liquidarFactoring: (facturaId: number) =>
    api.post(`/contabilidad/facturas/${facturaId}/factoring/liquidar`),
  kpis: (periodo?: string) => api.get<ContabKpis>('/contabilidad/kpis', { params: periodo ? { periodo } : {} }),
  // Adelantos de cliente: Comercial INFORMA (Cierre de Venta / Ventas) y Tesorería
  // APRUEBA (tesoreriaAPI). Acepta oc_cliente_id o cotizacion_id (recién cerrada).
  informarAdelanto: (data: { oc_cliente_id?: number; cotizacion_id?: number; monto_esperado?: number; pct?: number; observaciones?: string }) =>
    api.post('/contabilidad/ventas/adelantos', data),
  adelantosDeVenta: (ocId: number) => api.get(`/contabilidad/ventas/${ocId}/adelantos`),
  editarAdelanto: (id: number, data: Record<string, any>) =>
    api.patch(`/contabilidad/adelantos/${id}`, data),
  anularAdelanto: (id: number) => api.post(`/contabilidad/adelantos/${id}/anular`),
}

// --- Wasabil DTE (guías de despacho electrónicas al SII) ---
// Flujo: preview (no toca el SII) → emitir (con OK explícito del usuario) → sondeo
// de estado hasta Emitido (folio + PDF) o Fallido (reintento seguro).
export const wasabilAPI = {
  config: () => api.get('/wasabil/config'),
  previewGuia: (despachoId: number, tipoTraslado?: number) =>
    api.post(`/wasabil/despachos/${despachoId}/preview`, null,
      tipoTraslado ? { params: { tipo_traslado: tipoTraslado } } : undefined),
  emitirGuia: (despachoId: number, tipoTraslado?: number) =>
    api.post(`/wasabil/despachos/${despachoId}/emitir`, null,
      tipoTraslado ? { params: { tipo_traslado: tipoTraslado } } : undefined),
  estadoGuia: (despachoId: number) => api.get(`/wasabil/despachos/${despachoId}/estado`),
  reintentarGuia: (despachoId: number, tipoTraslado?: number) =>
    api.post(`/wasabil/despachos/${despachoId}/reintentar`, null,
      tipoTraslado ? { params: { tipo_traslado: tipoTraslado } } : undefined),
  estadoBatch: (despachoIds: number[]) =>
    api.get('/wasabil/despachos/estado-batch', { params: { ids: despachoIds.join(',') } }),
  // Fase B — FACTURAS electrónicas (DTE 33). El payload es el MISMO de crearFactura
  // pero SIN numero_factura (el folio lo asigna el SII al emitir).
  previewFacturaSII: (payload: Record<string, any>) =>
    api.post('/wasabil/facturas/preview', payload),
  emitirFacturaSII: (payload: Record<string, any>) =>
    api.post('/wasabil/facturas/emitir', payload),
  estadoFacturaSII: (facturaId: number) => api.get(`/wasabil/facturas/${facturaId}/estado`),
  reintentarFacturaSII: (facturaId: number) =>
    api.post(`/wasabil/facturas/${facturaId}/reintentar`),
}

// --- Despachos ---
/** Abre un documento subido (guía firmada, etc.) en una pestaña nueva, autenticado. */
export async function abrirDocumento(filename: string) {
  const res = await api.get(`/despachos/docs/${filename}`, { responseType: 'blob' })
  const url = URL.createObjectURL(res.data as Blob)
  window.open(url, '_blank')
}

export const despachosAPI = {
  getCounts: () => api.get('/despachos/counts').then(r => r.data),
  listOcClientes: (tab: string, q?: string) =>
    api.get('/despachos/oc-clientes', { params: { tab, q: q || undefined } }).then(r => r.data),
  getOcDetail: (ocId: number) =>
    api.get(`/despachos/oc-clientes/${ocId}`).then(r => r.data),
  create: (payload: any) =>
    api.post('/despachos/', payload).then(r => r.data),
  get: (id: number) => api.get(`/despachos/${id}`).then(r => r.data),
  update: (id: number, data: Record<string, any>) =>
    api.put(`/despachos/${id}`, data).then(r => r.data),
  cerrar: (id: number) => api.post(`/despachos/${id}/cerrar`).then(r => r.data),
  firmar: (id: number, data?: { fecha_firma?: string; numero_guia?: string; archivo?: string }) =>
    api.post(`/despachos/${id}/firmar`, data || {}).then(r => r.data),
  uploadDoc: (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return api.post('/compras/docs/upload', fd, { headers: { 'Content-Type': 'multipart/form-data' } }).then(r => r.data)
  },
  anular: (id: number) => api.delete(`/despachos/${id}`).then(r => r.data),
}

// --- Recepción Nacional (camino físico de la compra NACIONAL: camión + guía de
// despacho del proveedor, sin embarque). Bodega registra "cuánto llegó" por ítem;
// al cerrar la entrega, los utilizables con qty>0 pasan a en_bodega y quedan
// despachables, capados por lo recibido. Scope minería (Grupo AM). ---
export interface EntregaNacionalItemPayload {
  item_cotizacion_id: number
  qty_recibida: number
  estado_recepcion: string
  observacion?: string
}
export interface RegistrarEntregaNacionalPayload {
  oc_proveedor_id: number
  numero_guia_proveedor?: string
  fecha?: string
  documento?: string
  observacion?: string
  cerrar?: boolean
  items: EntregaNacionalItemPayload[]
}
export const recepcionNacionalAPI = {
  // Registra una entrega (una recepción). cerrar:true → cierra y los utilizables
  // pasan a en_bodega (despachables). Backend es la autoridad del tope físico.
  registrar: (data: RegistrarEntregaNacionalPayload) =>
    api.post('/recepcion-nacional', data),
  cerrar: (id: number) => api.post(`/recepcion-nacional/${id}/cerrar`),
  anular: (id: number) => api.delete(`/recepcion-nacional/${id}`),
  // Ítems 'comprado'/'en_bodega' de una OC nacional con su remanente por recibir.
  pendientes: (ocpId: number) => api.get(`/recepcion-nacional/pendientes/${ocpId}`),
  listar: (ocpId?: number) =>
    api.get('/recepcion-nacional', { params: ocpId ? { ocp_id: ocpId } : {} }),
}
