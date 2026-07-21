// Tipos del JSON del backend del módulo Compras / Cuentas por Pagar.

// Una "línea de pago" de la compra = una asignación de un Comprobante de Egreso.
export interface Pago {
  id: number               // id de la asignación (detalle de egreso)
  egreso_id: number
  monto_clp: number
  tc_aplicado: number
  monto_origen: number
  fecha: string | null
  medio: string | null
  banco: string | null
  numero_operacion: string | null
  moneda: string | null
  conciliado: boolean
  fecha_mov_bancario: string | null
  n_compras: number        // >1 → egreso consolidado (paga varias compras)
}

// Línea de costeo por ítem de una compra NACIONAL (cont_compra_item): la factura ES
// el costo de esos repuestos. Costo por ítem = NETO en CLP (el IVA no capitaliza).
export interface ContCompraItem {
  id: number
  item_cotizacion_id: number | null
  oc_proveedor_item_id: number | null
  numero_parte: string | null
  descripcion: string | null
  cantidad: number
  precio_unit: number
  costo_unit_clp: number
  costo_total_clp: number
}

export interface Compra {
  id: number
  empresa: string
  origen: string
  tipo_gasto: string
  tipo_gasto_label: string
  categoria: string | null
  cuenta_contable_id: number | null
  cuenta_codigo: string | null
  cuenta_nombre: string | null
  es_anticipo: boolean
  proveedor_id: number | null
  acreedor: string | null
  proveedor_rut: string | null
  fecha: string | null
  referencia: string | null
  descripcion: string | null
  numero_documento: string | null
  tipo_doc: string
  moneda: string
  tc: number
  monto_neto: number
  iva: number
  monto_total: number
  monto_total_clp: number
  condicion_pago: string
  plazo_dias: number | null
  fecha_vencimiento: string | null
  estado_pago: string
  monto_pagado_clp: number
  saldo_clp: number
  semaforo: string
  dias_vencimiento: number | null
  anulado: boolean
  motivo_anulacion: string | null
  embarque_id: number | null
  emb_pricing_gasto_id: number | null
  factura_proveedor_id: number | null
  oc_proveedor_id: number | null
  observaciones: string | null
  created_at: string | null
  pagos: Pago[]
  items: ContCompraItem[]
}

export interface Antiguedad { '0_30': number; '31_60': number; '61_90': number; '91_mas': number }

export interface Kpis {
  n_compras: number
  total_comprado_clp: number
  pagado_clp: number
  por_pagar_clp: number
  vencido_clp: number
  por_tipo: Record<string, number>
}

export interface ProveedorOpt { id: number; nombre: string; moneda: string | null; pais: string | null }

export interface PlanCuenta {
  id: number
  codigo: string
  nombre: string
  clase: string | null
  grupo: string | null
  requiere_auxiliar: boolean
}

export interface Catalogos {
  tipos_gasto: { value: string; label: string }[]
  estados_pago?: string[]
  categorias_sugeridas: string[]
  medios_pago: string[]
  iva_rate?: number
  proveedores: ProveedorOpt[]
  plan_cuentas: PlanCuenta[]
  cuenta_default_por_tipo: Record<string, number | null>
}

export interface CostoEmbarque {
  id: number
  origen: string
  embarque_id: number
  embarque_numero: string | null
  tipo: string
  glosa: string
  acreedor: string | null
  monto_neto: number
  iva: number
  monto_total: number
  nro_factura: string | null
  fecha_factura: string | null
  banco: string | null
  capitaliza: boolean
}

export interface CompraListResponse {
  compras: Compra[]
  total: number
  page: number
  page_size: number
  antiguedad: Antiguedad
}
export interface CostosEmbarqueResponse { costos: CostoEmbarque[]; total_clp: number; n: number }

// Catálogo de OC nacionales costeables (GET /compras-contab/oc-nacionales): para el
// detalle por ítem del alta de una compra nacional. disponible_costear ya viene
// topeado por lo recibido en bodega menos lo ya costeado (backend autoridad).
export interface OcNacionalItem {
  item_cotizacion_id: number
  oc_proveedor_item_id: number | null
  numero_parte: string | null
  descripcion: string | null
  cantidad: number          // cantidad vendida (asignada a la OC)
  recibido: number          // Σ recibido nacional utilizable
  ya_costeado: number       // Σ cont_compra_item en compras activas
  disponible_costear: number // max(min(recibido, cantidad) − ya_costeado, 0)
}
export interface OcNacional {
  oc_proveedor_id: number
  numero: string | null
  numero_oc: string | null
  proveedor: string | null
  moneda: string | null
  items: OcNacionalItem[]
}
export interface OcNacionalesResponse { ocs: OcNacional[] }
