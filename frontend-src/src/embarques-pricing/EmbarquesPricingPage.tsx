import { useState, useEffect, useCallback, useMemo } from 'react'
import {
  Calculator, Search, Loader2, RefreshCw, ChevronDown, ChevronUp, Ship,
  Lock, Unlock, Save, Info, RotateCcw, Package, Plane, FileText, Hash, CheckCircle2,
} from 'lucide-react'
import toast from 'react-hot-toast'
import { embarquesPricingAPI } from './api'
import { calcularLanded, type LandedInput } from './compute'
import type {
  EmbarquePricingRow, PricingDetail, GastoLinea, ItemOverride, PricingSavePayload, PesoOrigen,
} from './types'

// Etiquetas de flete según quién paga, por tipo de embarque.
const FLETE_HINT: Record<string, string> = {
  normal: 'LATAM: el flete puede pagarse en CLP en Chile, o venir prepagado por el proveedor (USD).',
  courier: 'DHL: el flete puede pagarse en CLP, o venir prepagado por el proveedor (USD/EUR).',
  baukat: 'Baukat: el flete viene prepagado por el proveedor en EUR (neteado del pago).',
  fastmark: 'Fast Mark: el flete se paga localmente en CLP (no viene prepagado).',
}

// ─── Helpers de formato ─────────────────────────────────────────────────────
const fmtClp = (n: number | null | undefined) =>
  n == null || Number.isNaN(n) ? '—' : '$' + Math.round(n).toLocaleString('es-CL')
const fmtNum = (n: number | null | undefined, dec = 2) =>
  n == null ? '—' : Number(n).toLocaleString('es-CL', { minimumFractionDigits: 0, maximumFractionDigits: dec })

// ─── Badges ─────────────────────────────────────────────────────────────────
const TIPO_BADGE: Record<string, { cls: string; label: string }> = {
  normal:   { cls: 'bg-blue-500/10 text-blue-400 border-blue-400/20',       label: 'Normal · LATAM' },
  courier:  { cls: 'bg-purple-500/10 text-purple-400 border-purple-400/20', label: 'Courier' },
  baukat:   { cls: 'bg-amber-600/10 text-amber-500 border-amber-500/20',    label: 'Baukat · Europa' },
  fastmark: { cls: 'bg-emerald-500/10 text-emerald-400 border-emerald-400/20', label: 'Fast Mark' },
}
const PRICING_BADGE: Record<string, { cls: string; label: string }> = {
  sin_pricing: { cls: 'bg-gray-500/10 text-gray-400 border-gray-500/20',       label: 'Sin pricing' },
  borrador:    { cls: 'bg-amber-500/10 text-amber-400 border-amber-500/20',    label: 'Borrador' },
  calculado:   { cls: 'bg-brand-500/10 text-brand-400 border-brand-500/20',    label: 'Calculado' },
  cerrado:     { cls: 'bg-emerald-500/10 text-emerald-500 border-emerald-500/20', label: 'Cerrado' },
}
function Badge({ map, k }: { map: Record<string, { cls: string; label: string }>; k: string }) {
  const m = map[k] ?? { cls: 'bg-gray-500/10 text-gray-400 border-gray-500/20', label: k }
  return <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-medium border ${m.cls}`}>{m.label}</span>
}

// ─── Editor de un embarque ──────────────────────────────────────────────────
function PricingEditor({ detail: initial, onSaved }: { detail: PricingDetail; onSaved: (d: PricingDetail) => void }) {
  const embId = initial.embarque.id
  const [pricing, setPricing] = useState(initial.pricing)
  const [gastos, setGastos] = useState<GastoLinea[]>(initial.gastos)
  const [items, setItems] = useState(initial.items)
  const [overrides, setOverrides] = useState<Record<number, ItemOverride>>({})
  const [fobInputs, setFobInputs] = useState<Record<number, string>>({})
  const [pesoInputs, setPesoInputs] = useState<Record<number, string>>({})
  const [saving, setSaving] = useState(false)

  const locked = pricing.estado === 'cerrado'

  // ── Campos editables del encabezado ──
  const [tcValor, setTcValor] = useState(String(pricing.tc_valor || ''))
  const [tcTipo, setTcTipo] = useState(pricing.tc_tipo || 'manual')
  const [tipoEmb, setTipoEmb] = useState(pricing.tipo_embarque)
  const [moneda, setMoneda] = useState(pricing.moneda)
  const [fleteEnMe, setFleteEnMe] = useState(pricing.flete_en_me)
  const [shippingMe, setShippingMe] = useState(String(pricing.shipping_me || ''))
  const [shippingClp, setShippingClp] = useState(String(pricing.shipping_clp || ''))
  const [observaciones, setObservaciones] = useState(pricing.observaciones || '')

  const tc = Number(tcValor) || 0
  const shippingTotal = fleteEnMe ? (Number(shippingMe) || 0) * tc : (Number(shippingClp) || 0)
  const totalGastosCap = useMemo(
    () => gastos.filter(g => g.capitaliza).reduce((s, g) => s + (Number(g.monto_neto) || 0), 0),
    [gastos],
  )
  // IVA solo de los gastos que capitalizan — espeja al backend (total_iva).
  const totalIva = gastos.filter(g => g.capitaliza).reduce((s, g) => s + (Number(g.iva) || 0), 0)
  const ivaImport = gastos.filter(g => g.tipo === 'iva_importacion').reduce((s, g) => s + (Number(g.monto_neto) || 0), 0)

  // FOB efectivo por ítem (override manual / reset / actual del server)
  const fobEfectivo = useCallback((it: typeof items[number]) => {
    const ov = overrides[it.embarque_item_id]
    if (ov?.fob_manual === true) return ov.fob_unit ?? 0
    if (ov?.fob_manual === false) return it.fob_default
    return it.fob_unit
  }, [overrides])

  // Peso efectivo por ítem (override manual / reset / actual del server). Espejo
  // del FOB: el peso decide el prorrateo del flete (shipping por peso).
  const pesoEfectivo = useCallback((it: typeof items[number]) => {
    const ov = overrides[it.embarque_item_id]
    if (ov?.peso_manual === true) return ov.peso_unit_lbs ?? 0
    if (ov?.peso_manual === false) return it.peso_default
    return it.peso_unit_lbs
  }, [overrides])

  // ── Vista previa en vivo (espeja el backend) ──
  const preview = useMemo(() => {
    const inputs: LandedInput[] = items.map(it => ({
      embarque_item_id: it.embarque_item_id,
      cantidad: it.cantidad,
      peso_unit_lbs: pesoEfectivo(it),
      fob_unit: fobEfectivo(it),
      tc_valor: tc,
    }))
    const { rows, totales } = calcularLanded(inputs, shippingTotal, totalGastosCap)
    const byId = new Map(rows.map(r => [r.embarque_item_id, r]))
    return { byId, totales }
  }, [items, fobEfectivo, pesoEfectivo, tc, shippingTotal, totalGastosCap])

  // ── Edición de gastos ──
  type GastoField = 'monto_neto' | 'iva' | 'nro_factura' | 'fecha_factura' | 'banco'
  const setGasto = (idx: number, field: GastoField, val: string) => {
    const numerico = field === 'monto_neto' || field === 'iva'
    setGastos(gs => gs.map((g, i) => i === idx ? {
      ...g,
      [field]: numerico ? (Number(val) || 0) : val,
    } : g))
  }
  const autoIva = (idx: number) => {
    setGastos(gs => gs.map((g, i) => i === idx ? { ...g, iva: Math.round((Number(g.monto_neto) || 0) * 0.19) } : g))
  }

  // ── Edición de FOB ──
  // MERGEA sobre el override existente (...o[id]) para no pisar un override de
  // peso: FOB y peso comparten overrides[embarque_item_id].
  const setFob = (id: number, val: string) => {
    setFobInputs(f => ({ ...f, [id]: val }))
    setOverrides(o => ({ ...o, [id]: { ...o[id], embarque_item_id: id, fob_unit: Number(val) || 0, fob_manual: true } }))
  }
  const resetFob = (id: number) => {
    setFobInputs(f => { const n = { ...f }; delete n[id]; return n })
    setOverrides(o => ({ ...o, [id]: { ...o[id], embarque_item_id: id, fob_manual: false } }))
  }

  // ── Edición de peso (espejo del FOB; también mergea para no pisar el FOB) ──
  const setPeso = (id: number, val: string) => {
    setPesoInputs(f => ({ ...f, [id]: val }))
    setOverrides(o => ({ ...o, [id]: { ...o[id], embarque_item_id: id, peso_unit_lbs: Number(val) || 0, peso_manual: true } }))
  }
  const resetPeso = (id: number) => {
    setPesoInputs(f => { const n = { ...f }; delete n[id]; return n })
    setOverrides(o => ({ ...o, [id]: { ...o[id], embarque_item_id: id, peso_manual: false } }))
  }

  const buildPayload = (): PricingSavePayload => ({
    tipo_embarque: tipoEmb,
    tc_tipo: tcTipo,
    tc_valor: tc,
    moneda,
    flete_en_me: fleteEnMe,
    shipping_me: Number(shippingMe) || 0,
    shipping_clp: Number(shippingClp) || 0,
    observaciones,
    gastos: gastos.map(g => ({
      tipo: g.tipo, glosa: g.glosa, monto_neto: Number(g.monto_neto) || 0,
      iva: Number(g.iva) || 0, capitaliza: g.capitaliza, nro_factura: g.nro_factura,
      fecha_factura: g.fecha_factura, banco: g.banco, orden: g.orden,
    })),
    items: Object.values(overrides),
  })

  const applyDetail = (d: PricingDetail) => {
    setPricing(d.pricing)
    setGastos(d.gastos)
    setItems(d.items)        // refrescar FOB/origen confirmados por el server
    setOverrides({})
    setFobInputs({})
    setPesoInputs({})
    setTcValor(String(d.pricing.tc_valor || ''))
    setTcTipo(d.pricing.tc_tipo || 'manual')
    setTipoEmb(d.pricing.tipo_embarque)
    setMoneda(d.pricing.moneda)
    setFleteEnMe(d.pricing.flete_en_me)
    setShippingMe(String(d.pricing.shipping_me || ''))
    setShippingClp(String(d.pricing.shipping_clp || ''))
    setObservaciones(d.pricing.observaciones || '')
  }

  const handleSave = async () => {
    if (tc <= 0) { toast.error('Ingresa un Tipo de Cambio mayor a 0'); return }
    setSaving(true)
    try {
      const { data } = await embarquesPricingAPI.save(embId, buildPayload())
      // El server puede recalcular el FOB/ítems; refrescamos ítems vía recarga del detalle.
      applyDetail(data)
      toast.success('Pricing calculado y guardado')
      onSaved(data)
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || 'No se pudo guardar el pricing')
    } finally { setSaving(false) }
  }

  const handleCerrar = async () => {
    if (tc <= 0) { toast.error('Define un TC antes de cerrar'); return }
    setSaving(true)
    try {
      const { data } = await embarquesPricingAPI.cerrar(embId)
      applyDetail(data)
      toast.success('Pricing cerrado (costo congelado)')
      onSaved(data)
    } catch (e: any) { toast.error(e?.response?.data?.detail || 'No se pudo cerrar el pricing') } finally { setSaving(false) }
  }

  const handleReabrir = async () => {
    setSaving(true)
    try {
      const { data } = await embarquesPricingAPI.reabrir(embId)
      applyDetail(data)
      toast.success('Pricing reabierto para editar')
      onSaved(data)
    } catch (e: any) { toast.error(e?.response?.data?.detail || 'No se pudo reabrir') } finally { setSaving(false) }
  }

  const inputCls = 'w-full px-2.5 py-1.5 rounded-lg border text-sm font-mono text-right focus:outline-none focus:ring-2 focus:ring-brand-500/40 disabled:opacity-60'
  const inputStyle = { backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)', color: 'var(--text-primary)' }

  return (
    <div className="border-t px-5 py-5 space-y-5" style={{ borderColor: 'var(--border)' }}>
      {/* Aviso bloqueado */}
      {locked && (
        <div className="flex items-center gap-2 p-3 rounded-xl bg-emerald-500/10 border border-emerald-500/20">
          <Lock className="w-4 h-4 text-emerald-500 shrink-0" />
          <p className="text-xs text-emerald-500">Pricing cerrado — el costo está congelado. Reábralo para editar.</p>
        </div>
      )}

      {/* ── Documentos del embarque (los sube Logística; pueden faltar) ── */}
      <div className="rounded-xl border p-3" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
        <div className="flex items-center justify-between mb-2 gap-2 flex-wrap">
          <p className="text-xs font-semibold uppercase tracking-wider flex items-center gap-1.5" style={{ color: 'var(--text-faint)' }}>
            <FileText className="w-3.5 h-3.5" /> Documentos del embarque
            <span className="inline-flex items-center gap-0.5 normal-case font-normal" style={{ color: 'var(--text-faint)' }} title="Los carga Logística; aquí son solo lectura">
              <Lock className="w-3 h-3" /> solo lectura
            </span>
          </p>
          <span className="text-[11px]" style={{ color: 'var(--text-faint)' }}>
            {initial.embarque.numero || `EMB #${initial.embarque.id}`}
            {initial.embarque.forwarder ? ` · ${initial.embarque.forwarder}` : ''}
            {initial.embarque.estado ? ` · ${initial.embarque.estado}` : ''}
            {initial.embarque.awb_numero ? ` · AWB ${initial.embarque.awb_numero}` : ''}
          </span>
        </div>
        <div className="flex flex-wrap gap-2">
          {[
            { label: 'AWB / BL', val: initial.embarque.documentos.awb },
            { label: 'Factura comercial', val: initial.embarque.documentos.factura_comercial },
            { label: 'Packing list', val: initial.embarque.documentos.packing_list },
            { label: 'Cert. origen', val: initial.embarque.documentos.certificado_origen },
            { label: 'Otros docs', val: initial.embarque.documentos.doc_adicional },
          ].map(d => (
            <span key={d.label}
              className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg border text-[11px] ${d.val ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-400' : ''}`}
              style={!d.val ? { borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)', color: 'var(--text-faint)' } : undefined}
              title={d.val || 'No cargado por Logística'}>
              {d.val
                ? <CheckCircle2 className="w-3 h-3" />
                : <span className="w-3 h-3 rounded-full border inline-block" style={{ borderColor: 'var(--text-faint)' }} />}
              {d.label}
            </span>
          ))}
        </div>
      </div>

      {/* ── Encabezado: TC + Flete ── */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* TC */}
        <div className="rounded-xl border p-4 space-y-3" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
          <p className="text-xs font-semibold uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>Tipo de cambio y moneda</p>
          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <span className="text-[11px]" style={{ color: 'var(--text-muted)' }}>Moneda FOB</span>
              <select value={moneda} disabled={locked} onChange={e => setMoneda(e.target.value)}
                className="w-full mt-1 px-2.5 py-1.5 rounded-lg border text-sm disabled:opacity-60" style={inputStyle}>
                {['USD', 'EUR'].map(m => <option key={m} value={m}>{m}</option>)}
              </select>
            </label>
            <label className="block">
              <span className="text-[11px]" style={{ color: 'var(--text-muted)' }}>Tipo de embarque</span>
              <select value={tipoEmb} disabled={locked} onChange={e => setTipoEmb(e.target.value as typeof tipoEmb)}
                className="w-full mt-1 px-2.5 py-1.5 rounded-lg border text-sm disabled:opacity-60" style={inputStyle}>
                {['normal', 'courier', 'baukat', 'fastmark'].map(t => <option key={t} value={t}>{t}</option>)}
              </select>
            </label>
          </div>
          <label className="block">
            <span className="text-[11px]" style={{ color: 'var(--text-muted)' }}>
              TC (1 {moneda} = ? CLP){pricing.tc_config ? ` · sugerido ${fmtNum(pricing.tc_config, 2)}` : ''}
            </span>
            <input value={tcValor} disabled={locked} onChange={e => setTcValor(e.target.value)}
              placeholder="ej. 962" className={inputCls + ' mt-1'} style={inputStyle} />
          </label>
        </div>

        {/* Flete */}
        <div className="rounded-xl border p-4 space-y-3" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
          <div className="flex items-center justify-between">
            <p className="text-xs font-semibold uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>Flete internacional</p>
            <label className="flex items-center gap-2 text-[11px] cursor-pointer" style={{ color: 'var(--text-muted)' }}>
              <input type="checkbox" checked={fleteEnMe} disabled={locked} onChange={e => setFleteEnMe(e.target.checked)} />
              Flete en {moneda} (lo paga el proveedor)
            </label>
          </div>
          {fleteEnMe ? (
            <label className="block">
              <span className="text-[11px]" style={{ color: 'var(--text-muted)' }}>Shipping en {moneda} (se convierte con el TC)</span>
              <input value={shippingMe} disabled={locked} onChange={e => setShippingMe(e.target.value)}
                placeholder="ej. 1200" className={inputCls + ' mt-1'} style={inputStyle} />
            </label>
          ) : (
            <label className="block">
              <span className="text-[11px]" style={{ color: 'var(--text-muted)' }}>Shipping en CLP (pagado en Chile)</span>
              <input value={shippingClp} disabled={locked} onChange={e => setShippingClp(e.target.value)}
                placeholder="ej. 450000" className={inputCls + ' mt-1'} style={inputStyle} />
            </label>
          )}
          <div className="flex justify-between text-xs pt-1 border-t" style={{ borderColor: 'var(--border)' }}>
            <span style={{ color: 'var(--text-faint)' }}>Shipping total a prorratear</span>
            <span className="font-mono font-bold text-amber-400">{fmtClp(shippingTotal)}</span>
          </div>
          <p className="text-[10px] leading-snug" style={{ color: 'var(--text-faint)' }}>{FLETE_HINT[tipoEmb]}</p>
        </div>
      </div>

      {/* ── Gastos locales ── */}
      <div>
        <p className="text-xs font-semibold uppercase tracking-wider mb-2" style={{ color: 'var(--text-faint)' }}>
          Gastos locales (desglose tributario)
        </p>
        <div className="overflow-x-auto rounded-xl border" style={{ borderColor: 'var(--border)' }}>
          <table className="w-full text-xs">
            <thead>
              <tr style={{ backgroundColor: 'var(--surface-200)', borderBottom: '1px solid var(--border)' }}>
                {['Concepto', 'Monto Neto', 'IVA', 'Total Bruto', 'N° Factura', 'Fecha factura', 'Banco', '¿Costo?'].map((h, i) => (
                  <th key={i} className="px-3 py-2 text-left font-semibold uppercase tracking-wider whitespace-nowrap" style={{ color: 'var(--text-faint)' }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {gastos.map((g, idx) => (
                <tr key={g.tipo} style={{ borderBottom: '1px solid var(--border)', backgroundColor: g.tipo === 'iva_importacion' ? 'var(--surface-100)' : 'transparent' }}>
                  <td className="px-3 py-1.5 font-medium whitespace-nowrap" style={{ color: 'var(--text-primary)' }}>{g.glosa}</td>
                  <td className="px-2 py-1.5">
                    <input value={g.monto_neto || ''} disabled={locked} onChange={e => setGasto(idx, 'monto_neto', e.target.value)}
                      className={inputCls} style={inputStyle} placeholder="0" />
                  </td>
                  <td className="px-2 py-1.5">
                    {g.tipo === 'iva_importacion' || g.tipo === 'arancel' ? (
                      <span className="block text-right pr-2 italic" style={{ color: 'var(--text-faint)' }} title="Arancel / IVA Importación: sin IVA">—</span>
                    ) : (
                      <div className="flex items-center gap-1">
                        <input value={g.iva || ''} disabled={locked} onChange={e => setGasto(idx, 'iva', e.target.value)}
                          className={inputCls} style={inputStyle} placeholder="0" />
                        {!locked && (
                          <button onClick={() => autoIva(idx)} title="IVA 19%" className="text-[10px] px-1 rounded text-brand-400 hover:underline shrink-0">19%</button>
                        )}
                      </div>
                    )}
                  </td>
                  <td className="px-3 py-1.5 text-right font-mono whitespace-nowrap" style={{ color: 'var(--text-muted)' }}>
                    {fmtClp((Number(g.monto_neto) || 0) + (Number(g.iva) || 0))}
                  </td>
                  <td className="px-2 py-1.5">
                    <input value={g.nro_factura || ''} disabled={locked} onChange={e => setGasto(idx, 'nro_factura', e.target.value)}
                      className="w-28 px-2.5 py-1.5 rounded-lg border text-sm focus:outline-none focus:ring-2 focus:ring-brand-500/40 disabled:opacity-60"
                      style={inputStyle} placeholder="—" />
                  </td>
                  <td className="px-2 py-1.5">
                    <input type="date" value={g.fecha_factura || ''} disabled={locked} onChange={e => setGasto(idx, 'fecha_factura', e.target.value)}
                      className="w-36 px-2.5 py-1.5 rounded-lg border text-sm focus:outline-none focus:ring-2 focus:ring-brand-500/40 disabled:opacity-60"
                      style={inputStyle} />
                  </td>
                  <td className="px-2 py-1.5">
                    <input value={g.banco || ''} disabled={locked} onChange={e => setGasto(idx, 'banco', e.target.value)}
                      className="w-32 px-2.5 py-1.5 rounded-lg border text-sm focus:outline-none focus:ring-2 focus:ring-brand-500/40 disabled:opacity-60"
                      style={inputStyle} placeholder="—" />
                  </td>
                  <td className="px-3 py-1.5 text-center">
                    {g.capitaliza
                      ? <span className="text-[10px] text-emerald-400" title="Capitaliza al costo">Sí</span>
                      : <span className="text-[10px]" style={{ color: 'var(--text-faint)' }} title="Recuperable, no es costo">No</span>}
                  </td>
                </tr>
              ))}
              <tr style={{ backgroundColor: 'var(--surface-200)' }}>
                <td className="px-3 py-2 font-bold" style={{ color: 'var(--text-primary)' }}>TOTAL GASTOS (capitalizan)</td>
                <td className="px-3 py-2 text-right font-mono font-bold text-brand-400">{fmtClp(totalGastosCap)}</td>
                <td className="px-3 py-2 text-right font-mono" style={{ color: 'var(--text-muted)' }}>{fmtClp(totalIva)}</td>
                <td colSpan={5} className="px-3 py-2 text-right text-[11px]" style={{ color: 'var(--text-faint)' }}>
                  IVA Importación (recuperable): <span className="font-mono">{fmtClp(ivaImport)}</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p className="text-[11px] mt-1.5 flex items-center gap-1" style={{ color: 'var(--text-faint)' }}>
          <Info className="w-3 h-3" /> Solo los gastos marcados como costo prorratean el landed. El IVA y el IVA Importación son recuperables.
        </p>
      </div>

      {/* ── Ítems: costo landed ── */}
      <div>
        <p className="text-xs font-semibold uppercase tracking-wider mb-2" style={{ color: 'var(--text-faint)' }}>
          Costo landed por ítem
        </p>
        <div className="overflow-x-auto rounded-xl border" style={{ borderColor: 'var(--border)' }}>
          <table className="w-full text-xs">
            <thead>
              <tr style={{ backgroundColor: 'var(--surface-200)', borderBottom: '1px solid var(--border)' }}>
                {['N° Parte', 'Descripción', 'Cant', 'Peso lb', `FOB unit ${moneda}`, 'FOB CLP', 'Shipping', 'CIF CLP', 'Gastos', 'Costo Total', 'Costo Unit'].map(h => (
                  <th key={h} className="px-3 py-2 text-left font-semibold uppercase tracking-wider whitespace-nowrap" style={{ color: 'var(--text-faint)' }}
                    title={h === 'Peso lb' ? 'El peso se lee de la cotización; edítalo si vino mal y el flete se re-prorratea' : undefined}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {items.map((it, idx) => {
                const p = preview.byId.get(it.embarque_item_id)
                const ov = overrides[it.embarque_item_id]
                const isManual = ov?.fob_manual === true || (ov?.fob_manual !== false && it.fob_origen === 'manual')
                const origen = ov?.fob_manual === true ? 'manual' : ov?.fob_manual === false ? it.fob_origen : it.fob_origen
                const pesoOrigen: PesoOrigen = ov?.peso_manual === true ? 'manual'
                  : ov?.peso_manual === false ? 'auto' : it.peso_origen
                const pesoIsManual = pesoOrigen === 'manual'
                return (
                  <tr key={`${it.embarque_item_id ?? 's'}-${idx}`} style={{ borderBottom: '1px solid var(--border)', backgroundColor: idx % 2 ? 'var(--surface-100)' : 'transparent' }}>
                    <td className="px-3 py-1.5 font-mono font-semibold whitespace-nowrap text-brand-400">{it.numero_parte || '—'}</td>
                    <td className="px-3 py-1.5 max-w-[160px] truncate" style={{ color: 'var(--text-primary)' }} title={it.descripcion}>{it.descripcion}</td>
                    <td className="px-3 py-1.5 text-right" style={{ color: 'var(--text-muted)' }}>{fmtNum(it.cantidad, 0)}</td>
                    <td className="px-2 py-1.5 min-w-[110px]">
                      <div className="flex items-center gap-1">
                        <input
                          value={pesoInputs[it.embarque_item_id] ?? String(pesoEfectivo(it) || '')}
                          disabled={locked}
                          onChange={e => setPeso(it.embarque_item_id, e.target.value)}
                          className={inputCls} style={inputStyle} placeholder="0" />
                        {!locked && pesoIsManual && (
                          <button onClick={() => resetPeso(it.embarque_item_id)} title="Volver al peso de la cotización" className="shrink-0 text-brand-400 hover:text-brand-300">
                            <RotateCcw className="w-3.5 h-3.5" />
                          </button>
                        )}
                      </div>
                      <span className="text-[9px] uppercase tracking-wide" style={{ color: pesoOrigen === 'manual' ? 'var(--text-muted)' : 'var(--text-faint)' }}>
                        {pesoOrigen === 'manual' ? 'manual' : 'de cotización'}
                      </span>
                    </td>
                    <td className="px-2 py-1.5 min-w-[120px]">
                      <div className="flex items-center gap-1">
                        <input
                          value={fobInputs[it.embarque_item_id] ?? String(fobEfectivo(it) || '')}
                          disabled={locked}
                          onChange={e => setFob(it.embarque_item_id, e.target.value)}
                          className={inputCls} style={inputStyle} placeholder="0" />
                        {!locked && isManual && (
                          <button onClick={() => resetFob(it.embarque_item_id)} title="Volver al FOB de la factura" className="shrink-0 text-brand-400 hover:text-brand-300">
                            <RotateCcw className="w-3.5 h-3.5" />
                          </button>
                        )}
                      </div>
                      <span className="text-[9px] uppercase tracking-wide" style={{ color: origen === 'manual' ? 'var(--text-muted)' : 'var(--text-faint)' }}>
                        {origen === 'factura' ? 'de factura' : origen === 'cotizacion' ? 'de cotización' : origen === 'manual' ? 'manual' : 'sin dato'}
                      </span>
                    </td>
                    <td className="px-3 py-1.5 text-right font-mono whitespace-nowrap" style={{ color: 'var(--text-primary)' }}>{fmtClp(p?.fob_clp)}</td>
                    <td className="px-3 py-1.5 text-right font-mono whitespace-nowrap text-amber-400">{fmtClp(p?.shipping_clp)}</td>
                    <td className="px-3 py-1.5 text-right font-mono whitespace-nowrap" style={{ color: 'var(--text-primary)' }}>{fmtClp(p?.cif_clp)}</td>
                    <td className="px-3 py-1.5 text-right font-mono whitespace-nowrap text-amber-400">{fmtClp(p?.gastos_clp)}</td>
                    <td className="px-3 py-1.5 text-right font-mono font-semibold whitespace-nowrap text-brand-400">{fmtClp(p?.costo_total_clp)}</td>
                    <td className="px-3 py-1.5 text-right font-mono font-bold whitespace-nowrap text-emerald-400">{fmtClp(p?.costo_unit_clp)}</td>
                  </tr>
                )
              })}
            </tbody>
            <tfoot>
              <tr style={{ backgroundColor: 'var(--surface-200)', fontWeight: 700 }}>
                <td className="px-3 py-2" style={{ color: 'var(--text-primary)' }} colSpan={5}>TOTAL</td>
                <td className="px-3 py-2 text-right font-mono" style={{ color: 'var(--text-primary)' }}>{fmtClp(preview.totales.fob_clp)}</td>
                <td className="px-3 py-2 text-right font-mono text-amber-400">{fmtClp(preview.totales.shipping_clp)}</td>
                <td className="px-3 py-2 text-right font-mono" style={{ color: 'var(--text-primary)' }}>{fmtClp(preview.totales.cif_clp)}</td>
                <td className="px-3 py-2 text-right font-mono text-amber-400">{fmtClp(preview.totales.gastos_clp)}</td>
                <td className="px-3 py-2 text-right font-mono text-brand-400">{fmtClp(preview.totales.costo_total_clp)}</td>
                <td className="px-3 py-2" />
              </tr>
            </tfoot>
          </table>
        </div>
      </div>

      {/* ── Observaciones + acciones ── */}
      <div className="flex flex-col lg:flex-row gap-3 lg:items-end justify-between">
        <label className="block flex-1">
          <span className="text-[11px]" style={{ color: 'var(--text-muted)' }}>Observaciones</span>
          <input value={observaciones} disabled={locked} onChange={e => setObservaciones(e.target.value)}
            className="w-full mt-1 px-3 py-2 rounded-lg border text-sm focus:outline-none focus:ring-2 focus:ring-brand-500/40 disabled:opacity-60"
            style={inputStyle} placeholder="Notas del pricing…" />
        </label>
        <div className="flex items-center gap-2 shrink-0">
          {locked ? (
            <button onClick={handleReabrir} disabled={saving} className="btn-secondary flex items-center gap-2">
              {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Unlock className="w-4 h-4" />} Reabrir
            </button>
          ) : (
            <>
              <button onClick={handleSave} disabled={saving} className="btn-primary flex items-center gap-2">
                {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />} Guardar y calcular
              </button>
              <button onClick={handleCerrar} disabled={saving} className="btn-secondary flex items-center gap-2">
                <Lock className="w-4 h-4" /> Cerrar
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

// ─── Tarjeta de embarque (lazy-load del detalle al expandir) ────────────────
function EmbarqueCard({ row, onChanged }: { row: EmbarquePricingRow; onChanged: () => void }) {
  const [open, setOpen] = useState(false)
  const [detail, setDetail] = useState<PricingDetail | null>(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState(false)

  const fetchDetail = async () => {
    setLoading(true); setErr(false)
    try {
      const { data } = await embarquesPricingAPI.get(row.embarque_id)
      setDetail(data)
    } catch { setErr(true) } finally { setLoading(false) }
  }

  const toggle = async () => {
    const next = !open
    setOpen(next)
    if (next && !detail) await fetchDetail()
  }

  return (
    <div className="rounded-2xl border overflow-hidden" style={{ backgroundColor: 'var(--surface-50)', borderColor: 'var(--border)' }}>
      <button onClick={toggle} className="w-full text-left px-5 py-4 flex flex-col sm:flex-row sm:items-center gap-3 hover:bg-white/5 transition-colors">
        <div className="w-9 h-9 rounded-xl bg-brand-500/10 flex items-center justify-center shrink-0">
          <Plane className="w-4 h-4 text-brand-400" />
        </div>
        <div className="flex-1 min-w-0 space-y-1.5">
          <div className="flex items-center gap-2 flex-wrap">
            {row.correlativo != null && (
              <span className="inline-flex items-center gap-0.5 text-[11px] font-mono font-bold px-1.5 py-0.5 rounded-md"
                style={{ backgroundColor: 'var(--surface-200)', color: 'var(--text-muted)' }} title="Correlativo de pricing">
                <Hash className="w-3 h-3" />{row.correlativo}
              </span>
            )}
            <span className="font-mono font-bold text-sm text-brand-400">{row.numero || `EMB #${row.embarque_id}`}</span>
            {row.awb_numero && (
              <span className="text-[11px] font-mono px-1.5 py-0.5 rounded-md"
                style={{ backgroundColor: 'var(--surface-200)', color: 'var(--text-muted)' }} title="N° AWB / BL">
                AWB {row.awb_numero}
              </span>
            )}
            <Badge map={TIPO_BADGE} k={row.tipo_embarque} />
            <Badge map={PRICING_BADGE} k={row.pricing_estado} />
          </div>
          <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
            {row.forwarder && <span>{row.forwarder} · </span>}
            {row.estado_logistica && <span>Logística: {row.estado_logistica} · </span>}
            <span>{row.n_items} ítem(s)</span>
            {row.tc_valor ? <span> · TC {fmtNum(row.tc_valor, 2)}</span> : null}
            <span> · </span>
            <span className="inline-flex items-center gap-0.5" style={{ color: row.docs_count ? 'var(--text-muted)' : 'var(--text-faint)' }}>
              <FileText className="w-3 h-3" />{row.docs_count}/5 docs
            </span>
          </p>
        </div>
        <div className="flex sm:flex-col items-center sm:items-end gap-4 sm:gap-1 shrink-0">
          <div className="text-right">
            <p className="text-[10px] uppercase tracking-wider font-semibold" style={{ color: 'var(--text-faint)' }}>Costo landed</p>
            <p className="font-bold text-base" style={{ color: row.costo_total_clp ? 'var(--text-primary)' : 'var(--text-faint)' }}>
              {row.costo_total_clp ? fmtClp(row.costo_total_clp) : '—'}
            </p>
          </div>
          <div style={{ color: 'var(--text-muted)' }}>{open ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}</div>
        </div>
      </button>

      {open && (
        <>
          {loading && <div className="flex items-center justify-center py-10"><Loader2 className="w-5 h-5 animate-spin text-brand-400" /></div>}
          {!loading && err && (
            <div className="px-5 py-6 text-center text-sm border-t" style={{ color: 'var(--text-muted)', borderColor: 'var(--border)' }}>
              No se pudo cargar el detalle.
              <button onClick={fetchDetail} className="ml-2 text-brand-400 underline">Reintentar</button>
            </div>
          )}
          {!loading && detail && (
            <PricingEditor detail={detail} onSaved={(d) => { setDetail(d); onChanged() }} />
          )}
        </>
      )}
    </div>
  )
}

// ─── Página ─────────────────────────────────────────────────────────────────
export default function EmbarquesPricingPage() {
  const [rows, setRows] = useState<EmbarquePricingRow[]>([])
  const [loading, setLoading] = useState(true)
  const [q, setQ] = useState('')
  const [error, setError] = useState('')

  const load = useCallback(async (search?: string) => {
    setLoading(true); setError('')
    try {
      const { data } = await embarquesPricingAPI.list(search)
      setRows(data)
    } catch { setError('No se pudieron cargar los embarques.') } finally { setLoading(false) }
  }, [])

  useEffect(() => { load() }, [load])

  const handleSearch = (v: string) => {
    setQ(v)
    if (v.length === 0 || v.length >= 2) load(v || undefined)
  }

  const pendientes = rows.filter(r => r.pricing_estado === 'sin_pricing' || r.pricing_estado === 'borrador').length

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-2xl font-bold flex items-center gap-2" style={{ color: 'var(--text-primary)' }}>
            <Calculator className="w-6 h-6 text-brand-400" /> Embarques — Pricing
          </h1>
          <p className="text-sm mt-1" style={{ color: 'var(--text-muted)' }}>
            Costo landed (CIF + gastos locales) por ítem · shipping prorrateado por peso, gastos por valor CIF
          </p>
        </div>
        <button onClick={() => load(q || undefined)} className="btn-secondary flex items-center gap-2 self-start">
          <RefreshCw className="w-4 h-4" />
        </button>
      </div>

      {/* Info */}
      <div className="flex items-start gap-2.5 p-4 rounded-xl border bg-brand-500/5 border-brand-500/20">
        <Info className="w-4 h-4 text-brand-400 shrink-0 mt-0.5" />
        <p className="text-xs leading-relaxed" style={{ color: 'var(--text-muted)' }}>
          Todo embarque creado por <strong>Logística</strong> aparece acá automáticamente. Contabilidad completa el
          TC, el flete y los gastos locales; el costo landed por ítem se calcula al instante y se congela al cerrar.
        </p>
      </div>

      {/* Search */}
      <div className="relative">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4" style={{ color: 'var(--text-faint)' }} />
        <input type="text" placeholder="Buscar por N° de embarque, forwarder o AWB…" value={q}
          onChange={e => handleSearch(e.target.value)}
          className="w-full pl-10 pr-4 py-2.5 rounded-xl border text-sm focus:outline-none focus:ring-2 focus:ring-brand-500/40"
          style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)', color: 'var(--text-primary)' }} />
      </div>

      {error && <div className="rounded-xl border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-400">{error}</div>}
      {loading && <div className="flex items-center justify-center py-16"><Loader2 className="w-7 h-7 animate-spin text-brand-400" /></div>}
      {!loading && !error && rows.length === 0 && (
        <div className="rounded-2xl border py-16 text-center" style={{ backgroundColor: 'var(--surface-50)', borderColor: 'var(--border)' }}>
          <Ship className="w-10 h-10 mx-auto mb-3 opacity-20" style={{ color: 'var(--text-muted)' }} />
          <p className="text-sm font-medium" style={{ color: 'var(--text-muted)' }}>Aún no hay embarques creados por Logística</p>
        </div>
      )}
      {!loading && rows.length > 0 && (
        <div className="space-y-3">
          <p className="text-xs font-semibold uppercase tracking-wider flex items-center gap-2" style={{ color: 'var(--text-faint)' }}>
            <Package className="w-3.5 h-3.5" />
            {rows.length} embarque(s){pendientes > 0 ? ` · ${pendientes} pendiente(s) de pricing` : ''}
          </p>
          {rows.map(r => <EmbarqueCard key={r.embarque_id} row={r} onChanged={() => load(q || undefined)} />)}
        </div>
      )}
    </div>
  )
}
