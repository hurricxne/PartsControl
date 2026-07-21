// Tipos del módulo Embarques Pricing (costo landed). Espejan el JSON del backend
// (backend/embarques_pricing/router.py).

export type PricingEstado = 'sin_pricing' | 'borrador' | 'calculado' | 'cerrado'
export type TipoEmbarque = 'normal' | 'courier' | 'baukat' | 'fastmark'
export type FobOrigen = 'factura' | 'cotizacion' | 'manual' | 'auto'
export type PesoOrigen = 'auto' | 'manual'

export interface EmbarquePricingRow {
  embarque_id: number
  correlativo: number | null
  numero: string | null
  estado_logistica: string | null
  forwarder: string | null
  awb: string | null
  awb_numero: string | null
  fecha_despacho: string | null
  n_items: number
  docs_count: number
  tipo_embarque: TipoEmbarque
  pricing_estado: PricingEstado
  moneda: string | null
  tc_valor: number | null
  costo_total_clp: number | null
}

export interface EmbarqueDocumentos {
  awb: string | null
  factura_comercial: string | null
  packing_list: string | null
  certificado_origen: string | null
  doc_adicional: string | null
}

export interface GastoLinea {
  id?: number
  tipo: string
  glosa: string
  monto_neto: number
  iva: number
  total_bruto?: number
  capitaliza: boolean
  nro_factura: string | null
  fecha_factura: string | null
  banco: string | null
  orden: number
}

export interface PricingItem {
  embarque_item_id: number
  item_cotizacion_id: number | null
  numero_parte: string
  descripcion: string
  moneda: string
  cantidad: number
  peso_unit_lbs: number
  peso_default: number
  peso_origen: PesoOrigen
  peso_total_lbs: number
  fob_unit: number
  fob_default: number
  fob_origen: FobOrigen
  tc_valor: number
  fob_total: number
  fob_clp: number
  shipping_clp: number
  cif_clp: number
  gastos_clp: number
  costo_total_clp: number
  costo_unit_clp: number
}

export interface PricingDetail {
  embarque: {
    id: number
    numero: string | null
    estado: string | null
    forwarder: string | null
    awb: string | null
    awb_numero: string | null
    fecha_despacho: string | null
    fecha_llegada_est: string | null
    n_items: number
    documentos: EmbarqueDocumentos
  }
  pricing: {
    id: number
    correlativo: number
    tipo_embarque: TipoEmbarque
    tc_tipo: string
    tc_valor: number
    tc_config: number
    moneda: string
    flete_en_me: boolean
    shipping_me: number
    shipping_clp: number
    shipping_total_clp: number
    estado: PricingEstado
    observaciones: string | null
    calculado_at: string | null
  }
  gastos: GastoLinea[]
  totales_gastos: {
    total_capitaliza: number
    total_iva: number
    iva_importacion: number
  }
  items: PricingItem[]
  totales: {
    n_items: number
    peso_total_lbs: number
    fob_total_me: number
    fob_clp: number
    shipping_clp: number
    cif_clp: number
    gastos_clp: number
    costo_total_clp: number
  }
}

export interface ItemOverride {
  embarque_item_id: number
  fob_unit?: number
  fob_manual?: boolean
  peso_unit_lbs?: number
  peso_manual?: boolean
}

export interface PricingSavePayload {
  tipo_embarque?: string
  tc_tipo?: string
  tc_valor?: number
  moneda?: string
  flete_en_me?: boolean
  shipping_me?: number
  shipping_clp?: number
  observaciones?: string
  gastos?: Omit<GastoLinea, 'id' | 'total_bruto'>[]
  items?: ItemOverride[]
}
