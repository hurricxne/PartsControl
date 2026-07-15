export interface PricingConfig {
  tipo_cambio_usd: number
  costo_shipping_usd_kg: number
  adicionales_shipping_usd: number
  costo_agencia_pct: number
  costo_agencia_minimo_clp: number
  desconsolidado_clp: number
  bodegaje_clp: number
  margen_venta_pct: number
}

export interface ItemInput {
  id: number
  item_num: number
  descripcion: string
  numero_parte: string
  marca: string
  cantidad: number
  precio_unit_cotizacion: number
  peso_unit_lbs: number
  margen_pct?: number | null
  plazo?: string
  precio_finning?: number | null
  nombre_cat?: string
  precio_cat?: number | null
  url_cat?: string
  encontrado?: number
}

export interface ItemCalculado extends ItemInput {
  total_exwork_usd: number
  peso_total_lbs: number
  peso_total_kg: number
  shipping_item_clp: number
  adic_shipping_clp: number
  cif_clp: number
  gastos_locales_clp: number
  costo_total_clp: number
  costo_unitario_clp: number
  margen_efectivo: number
  precio_venta_clp: number
  total_venta_clp: number
}

export interface Totales {
  total_peso_kg: number
  total_cif_clp: number
  gastos_locales_total_clp: number
  subtotal_neto_clp: number
  iva_clp: number
  total_con_iva_clp: number
  total_exwork_usd: number
}

export interface ResultadoCalculo {
  items: ItemCalculado[]
  totales: Totales
}

export function calcularCotizacion(
  items: ItemInput[],
  config: PricingConfig
): ResultadoCalculo {
  const tc = config.tipo_cambio_usd || 940
  const shippingRate = config.costo_shipping_usd_kg || 3.8
  const awbFijoUsd = config.adicionales_shipping_usd || 440
  const agenciaPct = config.costo_agencia_pct || 0.01
  const agenciaMin = config.costo_agencia_minimo_clp || 160000
  const desconsolidado = config.desconsolidado_clp || 90000
  const bodegaje = config.bodegaje_clp || 90000
  const margenDefault = config.margen_venta_pct || 0.19

  if (!items.length) {
    return {
      items: [],
      totales: {
        total_peso_kg: 0, total_cif_clp: 0, gastos_locales_total_clp: 0,
        subtotal_neto_clp: 0, iva_clp: 0, total_con_iva_clp: 0, total_exwork_usd: 0,
      },
    }
  }

  // Rama venta_clp: el precio unitario YA es venta (margen incluido); sin USD/CIF/IVA.
  if ((config as any).origen === 'venta_clp') {
    const resItems = items.map(item => {
      const qty = item.cantidad || 0
      const costo = item.precio_unit_cotizacion || 0
      const margen = item.margen_pct || 0
      const pv = costo * (1 + margen)
      const tv = pv * qty
      return {
        ...item,
        peso_total_lbs: 0, peso_total_kg: 0, shipping_item_clp: 0, adic_shipping_clp: 0,
        cif_clp: 0, gastos_locales_clp: 0, costo_total_clp: tv, costo_unitario_clp: costo,
        margen_efectivo: margen, precio_venta_clp: pv, total_venta_clp: tv,
        total_exwork_usd: 0,
      } as any
    })
    const sub = resItems.reduce((a: number, r: any) => a + r.total_venta_clp, 0)
    return {
      items: resItems as any,
      totales: {
        total_peso_kg: 0, total_cif_clp: 0, gastos_locales_total_clp: 0,
        subtotal_neto_clp: sub, iva_clp: sub * 0.19, total_con_iva_clp: sub * 1.19, total_exwork_usd: 0,
      },
    }
  }

  // Pasada 1: calcular shipping por ítem
  const pass1 = items.map(item => {
    const qty = item.cantidad || 0
    const precioUsd = item.precio_unit_cotizacion || 0
    const pesoLbs = item.peso_unit_lbs || 0

    const totalExworkUsd = qty * precioUsd
    const pesoTotalLbs = qty * pesoLbs
    const pesoTotalKg = pesoTotalLbs * 0.45359
    const shippingItemClp = pesoTotalKg * shippingRate * tc

    return { ...item, totalExworkUsd, pesoTotalLbs, pesoTotalKg, shippingItemClp }
  })

  const totalPesoKg = pass1.reduce((s, r) => s + r.pesoTotalKg, 0)

  // CIF por ítem
  const pass2 = pass1.map(r => {
    const adic = totalPesoKg > 0
      ? (r.pesoTotalKg / totalPesoKg) * awbFijoUsd * tc
      : 0
    const cifClp = r.totalExworkUsd * tc + r.shippingItemClp + adic
    return { ...r, adicShippingClp: adic, cifClp }
  })

  const totalCif = pass2.reduce((s, r) => s + r.cifClp, 0)

  // Gastos locales totales
  const gastosLocalesTotal =
    Math.max(agenciaMin, agenciaPct * totalCif) + desconsolidado + bodegaje

  // Pasada 2: gastos proporcionales → precio venta
  const resultItems: ItemCalculado[] = pass2.map(r => {
    const gastosLoc = totalCif > 0 ? (r.cifClp / totalCif) * gastosLocalesTotal : 0
    const costoTotal = r.cifClp + gastosLoc
    const qty = r.cantidad || 1
    const costoUnit = qty ? costoTotal / qty : 0
    const margen = r.margen_pct != null ? r.margen_pct : margenDefault
    const precioVenta = costoUnit * (1 + margen)
    const totalVenta = precioVenta * qty

    return {
      id: r.id,
      item_num: r.item_num,
      descripcion: r.descripcion,
      numero_parte: r.numero_parte,
      marca: r.marca,
      cantidad: r.cantidad,
      precio_unit_cotizacion: r.precio_unit_cotizacion,
      peso_unit_lbs: r.peso_unit_lbs,
      margen_pct: r.margen_pct,
      plazo: r.plazo,
      precio_finning: r.precio_finning,
      nombre_cat: r.nombre_cat,
      precio_cat: r.precio_cat,
      url_cat: r.url_cat,
      encontrado: r.encontrado,
      total_exwork_usd: r.totalExworkUsd,
      peso_total_lbs: r.pesoTotalLbs,
      peso_total_kg: r.pesoTotalKg,
      shipping_item_clp: r.shippingItemClp,
      adic_shipping_clp: r.adicShippingClp,
      cif_clp: r.cifClp,
      gastos_locales_clp: gastosLoc,
      costo_total_clp: costoTotal,
      costo_unitario_clp: costoUnit,
      margen_efectivo: margen,
      precio_venta_clp: precioVenta,
      total_venta_clp: totalVenta,
    }
  })

  const subtotalNeto = resultItems.reduce((s, r) => s + r.total_venta_clp, 0)
  const iva = subtotalNeto * 0.19

  return {
    items: resultItems,
    totales: {
      total_peso_kg: totalPesoKg,
      total_cif_clp: totalCif,
      gastos_locales_total_clp: gastosLocalesTotal,
      subtotal_neto_clp: subtotalNeto,
      iva_clp: iva,
      total_con_iva_clp: subtotalNeto + iva,
      total_exwork_usd: resultItems.reduce((s, r) => s + r.total_exwork_usd, 0),
    },
  }
}

export const clp = (v: number | null | undefined): string =>
  v != null ? `$${Math.round(v).toLocaleString('es-CL')}` : '-'

export const usd = (v: number | null | undefined): string =>
  v != null ? `US$${v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : '-'

export const pct = (v: number | null | undefined): string =>
  v != null ? `${(v * 100).toFixed(1)}%` : '-'
