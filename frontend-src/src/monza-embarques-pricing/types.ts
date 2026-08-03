// Tipos del módulo Embarques Pricing MonzaParts (costo landed). Espejan el JSON del
// backend (backend/monza_embarques_pricing/router.py). Igual que Grupo AM, salvo que el
// peso va en kg y que el origen 'factura' NO se deduce de una tabla de facturas de
// proveedor (Monza no la tiene): lo MARCA Contabilidad al teclear el FOB real acá.

export type PricingEstado = 'sin_pricing' | 'borrador' | 'calculado' | 'cerrado'
export type TipoEmbarque = 'normal' | 'courier' | 'baukat' | 'fastmark'
// Origen del FOB unitario usado en el costo landed:
//   'factura'    → precio REAL de la factura del proveedor (lo marcó Contabilidad)
//   'manual'     → corrección a mano (no hay factura que lo respalde)
//   'cotizacion' → costo estimado del ítem de cotización (default)
//   'auto'       → sin dato (la cotización no trae costo y nadie cargó el FOB)
// 'factura' y 'manual' tienen la MISMA precedencia sobre 'cotizacion'; la diferencia
// es informativa: dice si el costo está calculado con el precio real pagado o estimado.
export type FobOrigen = 'factura' | 'cotizacion' | 'manual' | 'auto'
// Origen del peso que prorratea el flete: 'auto' = peso_kg de la cotización.
export type PesoOrigen = 'auto' | 'manual'

export interface EmbarquePricingRow {
  embarque_id: number
  correlativo: number | null
  numero: string | null
  estado_logistica: string | null
  forwarder: string | null
  awb: string | null
  // N° con el que el forwarder identifica la carga. Buscable desde la Fase 9 (antes era el
  // único identificador del embarque por el que no se podía buscar acá).
  tracking: string | null
  fecha_despacho: string | null
  n_items: number
  // Adjuntos del embarque en monza_documentos (espejo del docs_count de GA;
  // en Monza es un conteo simple porque las categorías son abiertas, no 5 slots).
  docs_count: number
  tipo_embarque: TipoEmbarque
  pricing_estado: PricingEstado
  moneda: string | null
  tc_valor: number | null
  costo_total_clp: number | null
}

// Documento adjunto del embarque (tabla genérica monza_documentos, entidad="embarque").
export interface EmbarqueDocumento {
  id: number
  categoria: string
  original_name: string | null
  fecha: string | null
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
  peso_unit_kg: number
  // Peso de la cotización (a lo que vuelve el "reset"); en cerrado == el congelado.
  peso_default: number
  peso_origen: PesoOrigen
  peso_total_kg: number
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
    tracking: string | null
    fecha_despacho: string | null
    fecha_llegada_est: string | null
    n_items: number
    // Adjuntos subidos por Logística (solo lectura acá) → trazabilidad, espejo GA.
    documentos: EmbarqueDocumento[]
  }
  pricing: {
    id: number
    correlativo: number
    tipo_embarque: TipoEmbarque
    tc_tipo: string
    tc_valor: number
    // TC vigente de MonzaConfig (sugerencia "sugerido: X" en la UI, espejo GA).
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
  // Avisos no bloqueantes del backend (hoy: ítems en más de una moneda). Vacío = OK.
  advertencias: string[]
  gastos: GastoLinea[]
  totales_gastos: {
    total_capitaliza: number
    total_iva: number
    iva_importacion: number
  }
  items: PricingItem[]
  totales: {
    n_items: number
    peso_total_kg: number
    fob_total_me: number
    fob_clp: number
    shipping_clp: number
    cif_clp: number
    gastos_clp: number
    costo_total_clp: number
  }
}

// Override por ítem. Los flags son TRI-ESTADO y espejan al backend:
//   true = fijar manual · false = volver a auto · ausente = no tocar ese campo.
// FOB y peso comparten esta fila, así que editar uno NO debe pisar el otro:
// setFob/setPeso mergean sobre el override existente.
export interface ItemOverride {
  embarque_item_id: number
  fob_unit?: number
  fob_manual?: boolean
  // ¿El FOB tecleado viene de la factura REAL del proveedor? true → fob_origen
  // 'factura'; false/ausente → 'manual'. Solo lo lee el backend cuando fob_manual=true.
  fob_es_factura?: boolean
  peso_unit_kg?: number
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
