// Cliente API del módulo Compras / Cuentas por Pagar. Reutiliza la instancia
// axios compartida (auth + manejo de 401 ya configurados en services/api.ts).
import api from '../services/api'
import type {
  Catalogos, Compra, CompraListResponse, CostosEmbarqueResponse, Kpis,
  OcNacionalesResponse,
} from './types'

export interface CompraListParams {
  tipo?: string
  estado_pago?: string
  categoria?: string
  periodo?: string
  q?: string
  proveedor_id?: number
  incluir_anulados?: boolean
  page?: number
  page_size?: number
}

// ─── Payloads (espejo de los schemas Pydantic del backend) ──────────────────────
export interface PagoInlinePayload {
  fecha?: string
  monto_clp?: number
  medio?: string
  banco?: string
  cuenta_origen_id?: number
  fecha_mov_bancario?: string
  numero_operacion?: string
  observaciones?: string
}

// Línea de costeo por ítem de una compra NACIONAL (espejo de CompraItemIn del backend).
export interface CompraItemPayload {
  item_cotizacion_id: number
  oc_proveedor_item_id?: number | null
  numero_parte?: string | null
  descripcion?: string | null
  cantidad: number
  precio_unit: number    // NETO unitario en moneda factura (CLP en nacional)
}

export interface CompraCreatePayload {
  tipo_gasto: string
  categoria?: string
  cuenta_contable_id?: number
  es_anticipo?: boolean
  origen?: string
  proveedor_id?: number
  acreedor?: string
  proveedor_rut?: string
  fecha?: string
  referencia?: string
  descripcion?: string
  numero_documento?: string
  tipo_doc?: string
  moneda?: string
  tc?: number
  monto_neto?: number
  iva?: number
  monto_total?: number
  afecto_iva?: boolean
  condicion_pago?: string
  plazo_dias?: number
  embarque_id?: number
  emb_pricing_gasto_id?: number
  factura_proveedor_id?: number
  // Compra NACIONAL con detalle de ítems (la factura ES el costo de esos repuestos).
  oc_proveedor_id?: number
  items?: CompraItemPayload[]
  observaciones?: string
  pago?: PagoInlinePayload
}

export interface PagoInPayload {
  fecha?: string
  monto_clp: number
  medio?: string
  banco?: string
  cuenta_origen_id?: number
  fecha_mov_bancario?: string
  numero_operacion?: string
  observaciones?: string
}

export interface EgresoDetallePayload { compra_id: number; monto_clp: number }

export interface EgresoCreatePayload {
  fecha?: string
  medio?: string
  cuenta_origen_id?: number
  banco?: string
  numero_operacion?: string
  beneficiario?: string
  beneficiario_rut?: string
  glosa?: string
  moneda?: string
  tc?: number
  fecha_mov_bancario?: string
  detalles: EgresoDetallePayload[]
}

export interface EgresoUpdatePayload { fecha_mov_bancario?: string; referencia_bancaria?: string }

export const comprasContabAPI = {
  list: (params?: CompraListParams) =>
    api.get<CompraListResponse>('/compras-contab', { params }),
  detalle: (id: number) => api.get<Compra>(`/compras-contab/${id}`),
  crear: (data: CompraCreatePayload) => api.post<Compra>('/compras-contab', data),
  // Pago de UNA compra (crea un egreso de 1 detalle).
  registrarPago: (id: number, data: PagoInPayload) =>
    api.post<Compra>(`/compras-contab/${id}/pagos`, data),
  // Egreso consolidado: una salida de dinero que paga varias compras.
  crearEgresoConsolidado: (data: EgresoCreatePayload) => api.post('/compras-contab/egresos', data),
  listarEgresos: (params?: { conciliado?: boolean; q?: string; page?: number; page_size?: number }) =>
    api.get('/compras-contab/egresos', { params }),
  actualizarEgreso: (egresoId: number, data: EgresoUpdatePayload) =>
    api.patch(`/compras-contab/egresos/${egresoId}`, data),
  eliminarEgreso: (egresoId: number) => api.delete(`/compras-contab/egresos/${egresoId}`),
  // Editar la fecha del banco (cartola) del egreso al que pertenece este pago.
  actualizarPago: (id: number, pagoId: number, data: EgresoUpdatePayload) =>
    api.patch<Compra>(`/compras-contab/${id}/pagos/${pagoId}`, data),
  // Revierte el egreso completo al que pertenece este pago.
  eliminarPago: (id: number, pagoId: number) =>
    api.delete(`/compras-contab/${id}/pagos/${pagoId}`),
  anular: (id: number, motivo?: string) =>
    api.post<Compra>(`/compras-contab/${id}/anular`, { motivo }),
  eliminar: (id: number) => api.delete(`/compras-contab/${id}`),
  kpis: (params?: { periodo?: string; tipo?: string }) =>
    api.get<Kpis>('/compras-contab/kpis', { params }),
  catalogos: () => api.get<Catalogos>('/compras-contab/catalogos'),
  costosEmbarque: () => api.get<CostosEmbarqueResponse>('/compras-contab/costos-embarque'),
  // OC nacionales con sus ítems costeables (para el detalle por ítem del alta).
  ocNacionales: () => api.get<OcNacionalesResponse>('/compras-contab/oc-nacionales'),
}
