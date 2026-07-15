// Cálculo de costo landed en el cliente, SOLO para vista previa instantánea mientras el
// usuario edita. La fuente de verdad es el backend (backend/monza_embarques_pricing/
// service.py); esta función ESPEJA esa lógica 1:1 para que el preview coincida con lo que
// se guarda. Mantener ambas en sync. (Peso en kg; el prorrateo es proporcional.)

export interface LandedInput {
  embarque_item_id: number
  cantidad: number
  peso_unit_kg: number
  fob_unit: number
  tc_valor: number
}

export interface LandedRow extends LandedInput {
  peso_total_kg: number
  fob_total: number
  fob_clp: number
  shipping_clp: number
  cif_clp: number
  gastos_clp: number
  costo_total_clp: number
  costo_unit_clp: number
}

export interface LandedTotals {
  peso_total_kg: number
  fob_total_me: number
  fob_clp: number
  shipping_clp: number
  cif_clp: number
  gastos_clp: number
  costo_total_clp: number
}

function prorratear(rows: LandedRow[], total: number, baseKey: keyof LandedRow, outKey: keyof LandedRow) {
  const n = rows.length
  if (!n) return
  const suma = rows.reduce((s, r) => s + (Number(r[baseKey]) || 0), 0)
  if (suma > 0) {
    rows.forEach((r) => { (r[outKey] as number) = total * ((Number(r[baseKey]) || 0) / suma) })
  } else {
    const parte = total / n
    rows.forEach((r) => { (r[outKey] as number) = parte })
  }
}

export function calcularLanded(
  items: LandedInput[],
  shippingTotalClp: number,
  totalGastosCapitaliza: number,
): { rows: LandedRow[]; totales: LandedTotals } {
  const ship = Number(shippingTotalClp) || 0
  const gastos = Number(totalGastosCapitaliza) || 0

  const rows: LandedRow[] = items.map((it) => {
    const cant = Number(it.cantidad) || 0
    const pesoU = Number(it.peso_unit_kg) || 0
    const fobU = Number(it.fob_unit) || 0
    const tc = Number(it.tc_valor) || 0
    const pesoTotal = cant * pesoU
    const fobTotal = cant * fobU
    return {
      ...it,
      cantidad: cant, peso_unit_kg: pesoU, fob_unit: fobU, tc_valor: tc,
      peso_total_kg: pesoTotal, fob_total: fobTotal, fob_clp: fobTotal * tc,
      shipping_clp: 0, cif_clp: 0, gastos_clp: 0, costo_total_clp: 0, costo_unit_clp: 0,
    }
  })

  const sumaPeso = rows.reduce((s, r) => s + r.peso_total_kg, 0)
  prorratear(rows, ship, sumaPeso > 0 ? 'peso_total_kg' : 'fob_clp', 'shipping_clp')
  rows.forEach((r) => { r.cif_clp = r.fob_clp + r.shipping_clp })
  prorratear(rows, gastos, 'cif_clp', 'gastos_clp')
  rows.forEach((r) => {
    r.costo_total_clp = r.cif_clp + r.gastos_clp
    r.costo_unit_clp = r.cantidad ? r.costo_total_clp / r.cantidad : 0
  })

  const totales: LandedTotals = {
    peso_total_kg: rows.reduce((s, r) => s + r.peso_total_kg, 0),
    fob_total_me: rows.reduce((s, r) => s + r.fob_total, 0),
    fob_clp: rows.reduce((s, r) => s + r.fob_clp, 0),
    shipping_clp: rows.reduce((s, r) => s + r.shipping_clp, 0),
    cif_clp: rows.reduce((s, r) => s + r.cif_clp, 0),
    gastos_clp: rows.reduce((s, r) => s + r.gastos_clp, 0),
    costo_total_clp: rows.reduce((s, r) => s + r.costo_total_clp, 0),
  }
  return { rows, totales }
}
