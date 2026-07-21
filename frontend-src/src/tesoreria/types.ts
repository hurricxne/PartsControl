// Tipos del JSON del backend de Tesorería.

export interface Cuenta {
  id: number
  banco: string
  nombre: string | null
  numero_cuenta: string | null
  moneda: string
  activo: boolean
  observaciones: string | null
}

export interface EgresoCompra {
  compra_id: number
  acreedor: string | null
  numero_documento: string | null
  monto_clp: number
  categoria?: string | null
  tipo_gasto?: string
}

// Destino de una conciliación: un egreso de Compras (clase='egreso')
// o una cobranza de Facturas (clase='cobranza').
export interface EgresoMatch {
  clase: 'egreso'
  egreso_id: number
  fecha: string | null
  monto_total_clp: number
  medio: string | null
  banco: string | null
  numero_operacion: string | null
  beneficiario: string | null
  n_compras: number
  compras: EgresoCompra[]
  dias_diferencia?: number
}

export interface CobranzaMatch {
  clase: 'cobranza'
  cobranza_id: number
  factura_id: number
  numero_factura: string | null
  fecha: string | null
  monto: number
  medio: string | null
  banco: string | null
  numero_operacion: string | null
  dias_diferencia?: number
}

// Adelanto de cliente aprobado (candidato de conciliación abono↔adelanto)
export interface AdelantoMatch {
  clase: 'adelanto'
  adelanto_id: number
  oc_cliente_id: number
  numero_oc: string | null
  cliente: string
  estado: string
  fecha: string | null
  monto: number
  banco: string | null
  numero_operacion: string | null
  dias_diferencia?: number
}

export type Destino = EgresoMatch | CobranzaMatch | AdelantoMatch

export interface Movimiento {
  id: number
  cuenta_id: number
  cartola_id: number | null
  fecha: string | null
  glosa: string | null
  tipo: string            // cargo | abono
  monto: number
  referencia: string | null
  saldo: number | null
  conciliado: boolean
  conciliado_at: string | null
  observaciones: string | null
  destino: Destino | null
}

export interface MovListResp { movimientos: Movimiento[]; total: number; page: number; page_size: number }
export interface EgresosResp { egresos: EgresoMatch[]; total: number; page: number; page_size: number }
export interface CobranzasResp { cobranzas: CobranzaMatch[]; total: number; page: number; page_size: number }

export interface Resumen {
  pagos_por_aprobar: number
  monto_por_pagar_clp: number
  por_pagar_vencido_clp: number
  adelantos_por_aprobar: number
  adelantos_por_aprobar_clp: number
  adelantos_sin_conciliar: number
  movimientos_total: number
  movimientos_conciliados: number
  cargos_pendientes: number
  abonos_pendientes: number
  egresos_sin_conciliar: number
  cobranzas_sin_conciliar: number
  movimientos_pendientes: number
}

// ─── Aprobación de adelantos de cliente ────────────────────────────────────────
export interface Adelanto {
  id: number
  oc_cliente_id: number
  estado: 'informado' | 'aprobado' | 'anulado'
  monto_esperado: number
  pct: number | null
  monto: number
  monto_aplicado: number
  pendiente_aplicar: number
  fecha_pago: string | null
  banco: string | null
  numero_operacion: string | null
  conciliado_banco: boolean
  factura_anticipo_id: number | null
  factura_anticipo_folio: string | null
  observaciones: string | null
  // info de la venta (cola de aprobaciones)
  numero_oc?: string | null
  numero_cotizacion?: string | null
  cliente?: string
  rut_cliente?: string
  abono_sugerido?: { movimiento_id: number; fecha: string | null; monto: number; glosa: string | null; referencia: string | null } | null
}

export interface AprobacionesResp {
  por_aprobar: Adelanto[]
  aprobadas: Adelanto[]
}

export interface Cartola {
  id: number
  cuenta_id: number
  nombre: string | null
  fecha_desde: string | null
  fecha_hasta: string | null
  archivo: string | null
  origen: string
  n_movimientos: number
  created_at: string | null
}

// ─── Por pagar (aprobación de pagos) ───────────────────────────────────────────
export type Bucket = 'vencido' | 'd0_7' | 'd8_30' | 'd31_60' | 'd61_mas' | 'sin_fecha'

export interface CompraPorPagar {
  compra_id: number
  acreedor: string | null
  proveedor_rut: string | null
  numero_documento: string | null
  categoria: string | null
  tipo_gasto: string
  condicion_pago: string
  fecha: string | null
  fecha_vencimiento: string | null
  bucket: Bucket
  monto_total_clp: number
  monto_pagado_clp: number
  saldo_clp: number
  estado_pago: string
}

export interface BucketInfo { monto: number; n: number }

export interface PorPagarResp {
  compras: CompraPorPagar[]
  total: number
  page: number
  page_size: number
  buckets: Record<Bucket, BucketInfo>
}

// ─── Flujo de caja NIC 7 ───────────────────────────────────────────────────────
export interface FlujoCaja {
  buckets: Bucket[]
  por_pagar: Record<Bucket, BucketInfo>
  por_cobrar: Record<Bucket, BucketInfo>
  neto: Record<Bucket, number>
  retenciones_factoring: { n: number; monto: number }
  adelantos_por_aprobar: { n: number; monto: number }
  adelantos_recibidos_sin_aplicar: { n: number; monto: number }
}
