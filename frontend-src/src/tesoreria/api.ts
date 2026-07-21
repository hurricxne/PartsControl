// Cliente API del módulo Tesorería. Reutiliza la instancia axios compartida
// (auth + manejo de 401 ya configurados en services/api.ts).
import api from '../services/api'
import type {
  Cuenta, MovListResp, EgresosResp, CobranzasResp, Resumen, Cartola, Movimiento,
  EgresoMatch, CobranzaMatch, AdelantoMatch, PorPagarResp, FlujoCaja,
  AprobacionesResp,
} from './types'

// Payloads (espejo de los schemas Pydantic del backend)
export interface CuentaPayload {
  banco: string
  nombre?: string
  numero_cuenta?: string
  moneda?: string
  activo?: boolean
  observaciones?: string
}

export interface MovimientoPayload {
  cuenta_id: number
  fecha?: string
  glosa?: string
  tipo?: string
  monto: number
  referencia?: string
  saldo?: number
  cartola_id?: number
}

// Espejo de tesoreria.schemas.AprobarAdelantoIn (Tesorería confirma la plata del
// adelanto SIN exigir cartola; la conciliación con el abono viene después).
export interface AprobarAdelantoPayload {
  monto: number
  fecha_pago?: string
  banco?: string
  numero_operacion?: string
  observaciones?: string
}

// Espejo de compras_contab.schemas.EgresoCreate (Tesorería aprueba el pago con la
// misma regla de negocio de Compras).
export interface PagoDetallePayload { compra_id: number; monto_clp: number }
export interface PagoPayload {
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
  detalles: PagoDetallePayload[]
}

export const tesoreriaAPI = {
  // Cuentas
  cuentas: (incluir_inactivas = false) =>
    api.get<{ cuentas: Cuenta[]; bancos_sugeridos: string[] }>('/tesoreria/cuentas', { params: { incluir_inactivas } }),
  crearCuenta: (data: CuentaPayload) => api.post<Cuenta>('/tesoreria/cuentas', data),
  actualizarCuenta: (id: number, data: CuentaPayload) => api.put<Cuenta>(`/tesoreria/cuentas/${id}`, data),
  eliminarCuenta: (id: number) => api.delete(`/tesoreria/cuentas/${id}`),

  // Cartolas
  importarCartola: (cuentaId: number, file: File, nombre?: string) => {
    const fd = new FormData()
    fd.append('cuenta_id', String(cuentaId))
    if (nombre) fd.append('nombre', nombre)
    fd.append('file', file)
    return api.post('/tesoreria/cartolas/importar', fd, { headers: { 'Content-Type': 'multipart/form-data' } })
  },
  cartolas: (cuentaId?: number) => api.get<Cartola[]>('/tesoreria/cartolas', { params: { cuenta_id: cuentaId } }),
  eliminarCartola: (id: number) => api.delete(`/tesoreria/cartolas/${id}`),

  // Movimientos
  movimientos: (params: { cuenta_id?: number; estado?: string; tipo?: string; q?: string; page?: number; page_size?: number }) =>
    api.get<MovListResp>('/tesoreria/movimientos', { params }),
  crearMovimiento: (data: MovimientoPayload) => api.post<Movimiento>('/tesoreria/movimientos', data),
  eliminarMovimiento: (id: number) => api.delete(`/tesoreria/movimientos/${id}`),

  // Conciliación (movimiento ↔ egreso / cobranza / adelanto)
  sugerencias: (movId: number) =>
    api.get<{ movimiento_id: number; monto?: number; sugerencias: (EgresoMatch | CobranzaMatch | AdelantoMatch)[]; nota?: string }>(`/tesoreria/movimientos/${movId}/sugerencias`),
  conciliarEgreso: (movId: number, egresoId: number) =>
    api.post<Movimiento>(`/tesoreria/movimientos/${movId}/conciliar`, { egreso_id: egresoId }),
  conciliarCobranza: (movId: number, cobranzaId: number) =>
    api.post<Movimiento>(`/tesoreria/movimientos/${movId}/conciliar`, { cobranza_id: cobranzaId }),
  conciliarAdelanto: (movId: number, adelantoId: number) =>
    api.post<Movimiento>(`/tesoreria/movimientos/${movId}/conciliar`, { adelanto_id: adelantoId }),
  desconciliar: (movId: number) => api.post<Movimiento>(`/tesoreria/movimientos/${movId}/desconciliar`),
  egresosPendientes: (q?: string, page = 1) => api.get<EgresosResp>('/tesoreria/egresos-pendientes', { params: { q, page } }),
  cobranzasPendientes: (q?: string, page = 1) => api.get<CobranzasResp>('/tesoreria/cobranzas-pendientes', { params: { q, page } }),
  adelantosPendientes: () =>
    api.get<{ adelantos: AdelantoMatch[]; total: number }>('/tesoreria/adelantos-pendientes'),

  // Por pagar / aprobar pagos
  porPagar: (params?: { q?: string; page?: number; page_size?: number }) =>
    api.get<PorPagarResp>('/tesoreria/por-pagar', { params }),
  aprobarPago: (data: PagoPayload) => api.post('/tesoreria/pagos', data),

  // Aprobación de adelantos de cliente (la ORDEN la da Tesorería; sin exigir cartola)
  aprobaciones: () => api.get<AprobacionesResp>('/tesoreria/aprobaciones'),
  aprobarAdelanto: (adelantoId: number, data: AprobarAdelantoPayload) =>
    api.post(`/tesoreria/adelantos/${adelantoId}/aprobar`, data),

  // Flujo de caja NIC 7
  flujoCaja: () => api.get<FlujoCaja>('/tesoreria/flujo-caja'),

  // Resumen
  resumen: (cuentaId?: number) => api.get<Resumen>('/tesoreria/resumen', { params: { cuenta_id: cuentaId } }),
}
