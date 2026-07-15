// Página "Ventas — Contabilidad": lista las VENTAS agrupadas por OC de cliente (solo lectura)
// y, al expandir cada una, muestra sus ítems, guías de despacho y facturas. Consume
// contabilidadAPI (GET /ventas, /ventas/{oc}). El alta de facturas vive en FacturasPage.
import { useState, useEffect, useCallback } from 'react'
import {
  TrendingUp, Search, DollarSign, CreditCard, CheckCircle2, AlertCircle,
  Loader2, RefreshCw, ChevronDown, ChevronUp, Receipt, Truck, Clock,
} from 'lucide-react'
import { contabilidadAPI } from '../services/api'
import { fmtClp, fmtDate } from '../utils/format'

// ─── Tipos ────────────────────────────────────────────────────────────────────
interface VentaRow {
  oc_cliente_id: number
  cotizacion_id: number
  numero_oc: string | null
  numero_cotizacion: string
  cliente: string
  rut_cliente: string
  fecha_oc: string | null
  fecha_venta: string | null
  cond_pago: string | null
  total_items: number
  total_neto_clp: number
  iva_clp: number
  total_con_iva_clp: number
  n_facturas: number
  facturado_clp: number
  cobrado_clp: number
  por_cobrar_clp: number
  estado_cobranza: string
}

interface GuiaRef {
  numero_guia: string | null
  numero_despacho: string
  estado: string
  qty_despachada: number
  // El backend (detalle_venta) también envía estos campos de la guía firmada / expedición:
  despacho_id?: number
  despacho_item_id?: number
  guia_firmada?: boolean
  numero_expedicion?: string | null
  guia_firmada_archivo?: string | null
}
interface FacturaRef {
  factura_id: number
  numero_factura: string | null
  fecha_vencimiento: string | null
  plazo_dias: number | null
  estado_pago: string
}
interface VentaItem {
  id: number
  item_num: number
  numero_parte: string
  descripcion: string
  marca: string
  cantidad: number
  precio_unit_venta_clp: number
  total_venta_clp: number
  estado_item: string
  guias: GuiaRef[]
  facturas: FacturaRef[]
}
interface VentaDetalle {
  oc_cliente_id: number
  cotizacion_id: number
  numero_oc: string | null
  numero_cotizacion: string
  cliente: string
  rut_cliente: string
  fecha_oc: string | null
  cond_pago: string | null
  fecha_entrega: string | null
  total_neto_clp: number
  iva_clp: number
  total_con_iva_clp: number
  items: VentaItem[]
  // El endpoint también devuelve estas; no se renderizan en esta tabla (se ven en Facturas):
  facturas?: unknown[]
  resumen?: Record<string, number | string>
}
interface Kpis {
  facturado_clp: number
  cobrado_clp: number
  cobrado_cliente_clp?: number
  por_cobrar_clp: number
  vencido_clp: number
  en_factoring_clp: number
}

const COBRANZA_BADGE: Record<string, { cls: string; label: string }> = {
  sin_factura: { cls: 'bg-gray-500/10 text-gray-400 border-gray-500/20', label: 'Sin factura' },
  por_cobrar:  { cls: 'bg-blue-500/10 text-blue-400 border-blue-400/20', label: 'Por cobrar' },
  parcial:     { cls: 'bg-amber-500/10 text-amber-400 border-amber-400/20', label: 'Pago parcial' },
  cobrada:     { cls: 'bg-emerald-500/10 text-emerald-500 border-emerald-500/20', label: 'Cobrada' },
  vencida:     { cls: 'bg-red-500/10 text-red-400 border-red-400/20', label: 'Vencida' },
}
function CobranzaBadge({ estado }: { estado: string }) {
  const m = COBRANZA_BADGE[estado] ?? COBRANZA_BADGE.sin_factura
  return <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-medium border ${m.cls}`}>{m.label}</span>
}

const PAGO_BADGE: Record<string, { cls: string; label: string }> = {
  por_cobrar:  { cls: 'bg-blue-500/10 text-blue-400', label: 'Por cobrar' },
  parcial:     { cls: 'bg-amber-500/10 text-amber-400', label: 'Parcial' },
  pagada:      { cls: 'bg-emerald-500/10 text-emerald-500', label: 'Pagada' },
  vencida:     { cls: 'bg-red-500/10 text-red-400', label: 'Vencida' },
  factorizada: { cls: 'bg-purple-500/10 text-purple-400', label: 'Factoring' },
}

const PERIODO_LABELS: Record<string, string> = {
  '': 'Todo', semana: 'Semana', mes: 'Mes', anio: 'Año',
}

// ─── Tarjeta de venta (por OC, expandible a ítem) ─────────────────────────────
function VentaCard({ venta }: { venta: VentaRow }) {
  const [open, setOpen] = useState(false)
  const [detalle, setDetalle] = useState<VentaDetalle | null>(null)
  const [loading, setLoading] = useState(false)
  const [errDet, setErrDet] = useState(false)

  const fetchDetalle = async () => {
    setLoading(true); setErrDet(false)
    try {
      const { data } = await contabilidadAPI.ventaDetalle(venta.oc_cliente_id)
      setDetalle(data)
    } catch { setErrDet(true) } finally { setLoading(false) }
  }

  const toggle = async () => {
    const next = !open
    setOpen(next)
    if (next && !detalle) await fetchDetalle()
  }

  return (
    <div className="rounded-2xl border overflow-hidden" style={{ backgroundColor: 'var(--surface-50)', borderColor: 'var(--border)' }}>
      <button onClick={toggle} className="w-full text-left px-5 py-4 flex flex-col sm:flex-row sm:items-center gap-3 hover:bg-white/5 transition-colors">
        <div className="flex-1 min-w-0 space-y-1.5">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-mono font-bold text-sm text-brand-400">
              OC {venta.numero_oc || `#${venta.oc_cliente_id}`}
            </span>
            <span className="text-[10px] font-medium px-2 py-0.5 rounded-full border" style={{ background: 'var(--surface-200)', borderColor: 'var(--border)', color: 'var(--text-muted)' }}>
              COT-{venta.numero_cotizacion}
            </span>
            <CobranzaBadge estado={venta.estado_cobranza} />
          </div>
          <p className="font-semibold text-sm truncate" style={{ color: 'var(--text-primary)' }}>{venta.cliente || '—'}</p>
          <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
            {venta.rut_cliente && <span>{venta.rut_cliente} · </span>}
            {venta.fecha_oc && <span>OC: {fmtDate(venta.fecha_oc)} · </span>}
            {venta.cond_pago && <span>{venta.cond_pago} · </span>}
            <span>{venta.total_items} ítems · {venta.n_facturas} factura(s)</span>
          </p>
        </div>
        <div className="flex sm:flex-col items-center sm:items-end gap-4 sm:gap-1 shrink-0">
          <div className="text-right">
            <p className="text-[10px] uppercase tracking-wider font-semibold" style={{ color: 'var(--text-faint)' }}>Total c/IVA</p>
            <p className="font-bold text-base" style={{ color: 'var(--text-primary)' }}>{fmtClp(venta.total_con_iva_clp)}</p>
          </div>
          <div className="text-right">
            <p className="text-[10px] uppercase tracking-wider font-semibold" style={{ color: 'var(--text-faint)' }}>Por cobrar</p>
            <p className="font-bold text-sm text-amber-400">{fmtClp(venta.por_cobrar_clp)}</p>
          </div>
          <div className="ml-2 sm:ml-0" style={{ color: 'var(--text-muted)' }}>
            {open ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
          </div>
        </div>
      </button>

      {open && (
        <div className="border-t" style={{ borderColor: 'var(--border)' }}>
          {loading && (
            <div className="flex items-center justify-center py-8"><Loader2 className="w-5 h-5 animate-spin text-brand-400" /></div>
          )}
          {!loading && errDet && (
            <div className="px-5 py-6 text-center text-sm" style={{ color: 'var(--text-muted)' }}>
              No se pudo cargar el detalle.
              <button onClick={fetchDetalle} className="ml-2 text-brand-400 underline">Reintentar</button>
            </div>
          )}
          {!loading && detalle && (
            <>
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr style={{ backgroundColor: 'var(--surface-100)', borderBottom: '1px solid var(--border)' }}>
                      {['#', 'N° Parte', 'Descripción', 'Cant.', 'P. Unit', 'Total', 'Guía despacho', 'Factura', 'Plazo / Vence', 'Estado pago'].map(h => (
                        <th key={h} className="px-3 py-2 text-left font-semibold uppercase tracking-wider whitespace-nowrap" style={{ color: 'var(--text-faint)' }}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {detalle.items.map((item, idx) => {
                      const facs = item.facturas
                      // Estado agregado: pagada si todas pagadas; vencida si alguna; parcial si mezcla
                      const estados = facs.map(x => x.estado_pago)
                      const estadoAgg = !estados.length ? null
                        : estados.every(e => e === estados[0]) ? estados[0]
                        : estados.includes('vencida') ? 'vencida' : 'parcial'
                      const pago = estadoAgg ? (PAGO_BADGE[estadoAgg] ?? { cls: 'bg-gray-500/10 text-gray-400', label: estadoAgg }) : null
                      return (
                        <tr key={item.id} style={{ backgroundColor: idx % 2 === 0 ? 'transparent' : 'var(--surface-100)', borderBottom: '1px solid var(--border)' }}>
                          <td className="px-3 py-2 font-mono text-[11px]" style={{ color: 'var(--text-muted)' }}>{item.item_num}</td>
                          <td className="px-3 py-2 font-mono font-semibold whitespace-nowrap" style={{ color: 'var(--text-primary)' }}>{item.numero_parte || '—'}</td>
                          <td className="px-3 py-2 max-w-[200px] truncate" style={{ color: 'var(--text-primary)' }} title={item.descripcion}>{item.descripcion}</td>
                          <td className="px-3 py-2 text-right" style={{ color: 'var(--text-muted)' }}>{item.cantidad}</td>
                          <td className="px-3 py-2 text-right font-medium whitespace-nowrap" style={{ color: 'var(--text-primary)' }}>{fmtClp(item.precio_unit_venta_clp)}</td>
                          <td className="px-3 py-2 text-right font-bold whitespace-nowrap text-brand-400">{fmtClp(item.total_venta_clp)}</td>
                          <td className="px-3 py-2 whitespace-nowrap" style={{ color: 'var(--text-muted)' }}>
                            {item.guias.length ? (
                              <span className="inline-flex items-center gap-1">
                                <Truck className="w-3 h-3 text-emerald-400" />
                                {item.guias.map(g => g.numero_guia || g.numero_despacho).join(', ')}
                              </span>
                            ) : <span className="italic" style={{ color: 'var(--text-faint)' }}>sin despachar</span>}
                          </td>
                          <td className="px-3 py-2 whitespace-nowrap" style={{ color: 'var(--text-muted)' }}>
                            {facs.length ? <span className="inline-flex items-center gap-1"><Receipt className="w-3 h-3 text-brand-400" />{facs.map(x => x.numero_factura || `#${x.factura_id}`).join(', ')}</span>
                                 : <span className="italic" style={{ color: 'var(--text-faint)' }}>sin facturar</span>}
                          </td>
                          <td className="px-3 py-2 whitespace-nowrap" style={{ color: 'var(--text-muted)' }}>
                            {facs.length === 1 ? <span>{facs[0].plazo_dias ? `${facs[0].plazo_dias}d · ` : ''}{fmtDate(facs[0].fecha_vencimiento)}</span>
                              : facs.length > 1 ? <span>varias</span> : '—'}
                          </td>
                          <td className="px-3 py-2 whitespace-nowrap">
                            {pago ? <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-semibold ${pago.cls}`}>{pago.label}</span> : '—'}
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
              <div className="px-5 py-3 flex flex-wrap justify-end gap-6 text-xs font-semibold" style={{ backgroundColor: 'var(--surface-100)', borderTop: '1px solid var(--border)' }}>
                <span><span style={{ color: 'var(--text-faint)' }}>Neto: </span><span style={{ color: 'var(--text-primary)' }}>{fmtClp(detalle.total_neto_clp)}</span></span>
                <span><span style={{ color: 'var(--text-faint)' }}>IVA: </span><span style={{ color: 'var(--text-primary)' }}>{fmtClp(detalle.iva_clp)}</span></span>
                <span><span style={{ color: 'var(--text-faint)' }}>Total: </span><span className="text-brand-400 font-bold text-sm">{fmtClp(detalle.total_con_iva_clp)}</span></span>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}

// ─── Página ───────────────────────────────────────────────────────────────────
export default function VentasContabPage() {
  const [ventas, setVentas] = useState<VentaRow[]>([])
  const [kpis, setKpis] = useState<Kpis | null>(null)
  const [loading, setLoading] = useState(true)
  const [q, setQ] = useState('')
  const [periodo, setPeriodo] = useState('')
  const [error, setError] = useState('')

  const load = useCallback(async (search?: string, per?: string) => {
    setLoading(true); setError('')
    try {
      const [vRes, kRes] = await Promise.all([
        contabilidadAPI.listVentas(search, per),
        contabilidadAPI.kpis(per),
      ])
      setVentas(vRes.data)
      setKpis(kRes.data)
    } catch {
      setError('No se pudieron cargar las ventas.')
    } finally { setLoading(false) }
  }, [])

  useEffect(() => { load(q || undefined, periodo || undefined) }, [periodo])

  const handleSearch = (v: string) => {
    setQ(v)
    if (v.length === 0 || v.length >= 2) load(v || undefined, periodo || undefined)
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-2xl font-bold" style={{ color: 'var(--text-primary)' }}>Ventas — Contabilidad</h1>
          <p className="text-sm mt-1" style={{ color: 'var(--text-muted)' }}>
            Por orden de compra · despliega cada venta para ver sus ítems, guías de despacho y facturas
          </p>
        </div>
        <button onClick={() => load(q || undefined, periodo || undefined)} className="btn-secondary flex items-center gap-2 self-start">
          <RefreshCw className="w-4 h-4" />
        </button>
      </div>

      {/* Periodo */}
      <div className="flex items-center gap-2 flex-wrap">
        {Object.entries(PERIODO_LABELS).map(([key, label]) => (
          <button key={key} onClick={() => setPeriodo(key)}
            className={`px-4 py-1.5 rounded-full text-sm font-medium border transition-all ${periodo === key ? 'border-brand-500 bg-brand-500/10 text-brand-400' : 'border-transparent'}`}
            style={periodo !== key ? { backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)', color: 'var(--text-muted)' } : {}}>
            {label}
          </button>
        ))}
      </div>

      {/* KPIs */}
      {kpis && (
        <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
          {[
            { icon: <DollarSign className="w-5 h-5" />, label: 'Facturado', value: fmtClp(kpis.facturado_clp), color: 'text-brand-400' },
            { icon: <CheckCircle2 className="w-5 h-5" />, label: 'Cobrado', value: fmtClp(kpis.cobrado_cliente_clp ?? kpis.cobrado_clp), color: 'text-emerald-400' },
            { icon: <CreditCard className="w-5 h-5" />, label: 'Por cobrar', value: fmtClp(kpis.por_cobrar_clp), color: 'text-amber-400' },
            { icon: <AlertCircle className="w-5 h-5" />, label: 'Vencido', value: fmtClp(kpis.vencido_clp), color: 'text-red-400' },
            { icon: <Clock className="w-5 h-5" />, label: 'En factoring', value: fmtClp(kpis.en_factoring_clp), color: 'text-purple-400' },
          ].map(({ icon, label, value, color }) => (
            <div key={label} className="rounded-2xl border p-4 flex items-center gap-3" style={{ backgroundColor: 'var(--surface-50)', borderColor: 'var(--border)' }}>
              <div className={`p-2 rounded-xl ${color} opacity-80`} style={{ backgroundColor: 'var(--surface-200)' }}>{icon}</div>
              <div className="min-w-0">
                <p className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>{label}</p>
                <p className={`font-bold text-lg leading-tight truncate ${color}`}>{value}</p>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Search */}
      <div className="relative">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4" style={{ color: 'var(--text-faint)' }} />
        <input type="text" placeholder="Buscar por cliente, OC, cotización o RUT…" value={q}
          onChange={e => handleSearch(e.target.value)}
          className="w-full pl-10 pr-4 py-2.5 rounded-xl border text-sm focus:outline-none focus:ring-2 focus:ring-brand-500/40"
          style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)', color: 'var(--text-primary)' }} />
      </div>

      {error && <div className="rounded-xl border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-400">{error}</div>}
      {loading && <div className="flex items-center justify-center py-16"><Loader2 className="w-7 h-7 animate-spin text-brand-400" /></div>}
      {!loading && !error && ventas.length === 0 && (
        <div className="rounded-2xl border py-16 text-center" style={{ backgroundColor: 'var(--surface-50)', borderColor: 'var(--border)' }}>
          <TrendingUp className="w-10 h-10 mx-auto mb-3 opacity-20" style={{ color: 'var(--text-muted)' }} />
          <p className="text-sm font-medium" style={{ color: 'var(--text-muted)' }}>No hay ventas{periodo ? ' para el período' : ''}</p>
        </div>
      )}
      {!loading && ventas.length > 0 && (
        <div className="space-y-3">
          <p className="text-xs font-semibold uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>
            {ventas.length} {ventas.length === 1 ? 'venta' : 'ventas'}
          </p>
          {ventas.map(v => <VentaCard key={v.oc_cliente_id} venta={v} />)}
        </div>
      )}
    </div>
  )
}
