import { useState, useEffect, useCallback } from 'react'
import {
  TrendingUp, Search, DollarSign, CreditCard,
  CheckCircle2, Loader2, RefreshCw, Download,
  ChevronDown, ChevronUp, FileText, Package,
  Truck, ShoppingCart, Clock, BarChart2,
} from 'lucide-react'
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, Area, AreaChart,
} from 'recharts'
import { ventasAPI, bodegaAPI } from '../services/api'

// ─── Types ──────────────────────────────────────────────────────────────────

interface ItemVenta {
  id: number
  item_num: number
  numero_parte: string
  descripcion: string
  cantidad: number
  precio_unit_venta_clp: number
  total_venta_clp: number
  estado_item: string
  plazo_entrega_max: number | null
  ocp_numero: string
  ocp_numero_oc: string
  ocp_proveedor: string
  ocp_pais: string
  embarque_numero: string
  embarque_estado: string
  embarque_awb: string
  embarque_fecha_despacho: string
  embarque_fecha_llegada: string
}

interface Venta {
  id: number
  numero: string
  cliente: string
  rut_cliente: string
  referencia: string
  asesor: string
  fase_comercial: string
  total_items: number
  items_vendidos: number
  pct_vendido: number
  total_neto_clp: number
  iva_clp: number
  total_con_iva_clp: number
  numero_oc_cliente: string
  fecha_oc: string
  cond_pago: string
  fecha_entrega: string
  created_at: string | null
  items: ItemVenta[]
}

interface OcListaDespacho {
  oc_cliente_id: number
  numero_oc: string
  cliente: string
  fecha_entrega: string | null
  total_items: number
  items_en_bodega: number
}

interface OcAlertaPlazo {
  oc_cliente_id: number
  numero_oc: string
  cliente: string
  fecha_entrega: string | null
  dias_habiles_restantes: number
  items_en_bodega: number
  total_items: number
}

interface Kpis {
  total_ventas: number
  total_facturado_clp: number
  total_x_cobrar_clp: number
  total_cobrado_clp: number
}

interface MesData {
  mes_num: number
  mes: string
  neto_clp: number
  ventas: number
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function fmtClp(n: number) {
  if (!n) return '$0'
  return '$' + Math.round(n).toLocaleString('es-CL')
}

function fmtDate(s: string | null) {
  if (!s) return '—'
  const d = new Date(s)
  return isNaN(d.getTime()) ? s : d.toLocaleDateString('es-CL', { day: '2-digit', month: '2-digit', year: 'numeric' })
}

const PERIODO_LABELS: Record<string, string> = {
  '': 'Todo el tiempo',
  semana: 'Esta semana',
  mes: 'Este mes',
  anio: 'Este año',
}

// ─── Item estado badge ────────────────────────────────────────────────────────

function EstadoBadge({ estado }: { estado: string }) {
  const map: Record<string, { cls: string; icon: React.ReactNode; label: string }> = {
    ingresado:  { cls: 'bg-gray-500/10 text-gray-400 border-gray-500/20',           icon: <Clock className="w-3 h-3" />,        label: 'Ingresado'  },
    cerrado:    { cls: 'bg-blue-500/10 text-blue-400 border-blue-500/20',            icon: <FileText className="w-3 h-3" />,      label: 'Cerrado'    },
    comprado:   { cls: 'bg-purple-500/10 text-purple-400 border-purple-500/20',      icon: <ShoppingCart className="w-3 h-3" />,  label: 'Comprado'   },
    preparado:  { cls: 'bg-amber-500/10 text-amber-400 border-amber-500/20',         icon: <Package className="w-3 h-3" />,       label: 'Preparado'  },
    embarcado:  { cls: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20',   icon: <Truck className="w-3 h-3" />,         label: 'Embarcado'  },
  }
  const m = map[estado] ?? { cls: 'bg-gray-500/10 text-gray-400 border-gray-500/20', icon: null, label: estado }
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-semibold border ${m.cls}`}>
      {m.icon}{m.label}
    </span>
  )
}

// ─── Fase badge ──────────────────────────────────────────────────────────────

function FaseBadge({ fase }: { fase: string }) {
  const map: Record<string, { cls: string; label: string }> = {
    aceptada:       { cls: 'bg-blue-500/10 text-blue-400 border-blue-400/20',          label: 'Aceptada'   },
    pendiente_pago: { cls: 'bg-amber-500/10 text-amber-400 border-amber-500/20',       label: 'Pend. Pago' },
    pagada:         { cls: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20', label: 'Pagada'     },
  }
  const m = map[fase] ?? { cls: 'bg-gray-500/10 text-gray-400 border-gray-500/20', label: fase }
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-semibold border ${m.cls}`}>
      {m.label}
    </span>
  )
}

// ─── Custom Tooltip for chart ─────────────────────────────────────────────────

function ChartTooltip({ active, payload, label }: any) {
  if (!active || !payload?.length) return null
  return (
    <div className="rounded-xl border px-3 py-2 text-xs shadow-xl"
      style={{ background: 'var(--surface-100)', borderColor: 'var(--border)', color: 'var(--text-primary)' }}>
      <p className="font-bold mb-1">{label}</p>
      <p style={{ color: '#818cf8' }}>Neto: {fmtClp(payload[0]?.value ?? 0)}</p>
      {payload[1] && <p style={{ color: 'var(--text-muted)' }}>Ventas: {payload[1]?.value}</p>}
    </div>
  )
}

// ─── Venta Card ───────────────────────────────────────────────────────────────

function VentaCard({ venta }: { venta: Venta }) {
  const [open, setOpen] = useState(false)

  const itemsVendidos = venta.items.filter(i => i.estado_item !== 'ingresado')

  return (
    <div className="rounded-2xl border overflow-hidden"
      style={{ backgroundColor: 'var(--surface-50)', borderColor: 'var(--border)' }}>

      {/* Card header */}
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full text-left px-5 py-4 flex flex-col sm:flex-row sm:items-center gap-3 hover:bg-white/5 transition-colors">

        {/* Left info */}
        <div className="flex-1 min-w-0 space-y-1.5">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-mono font-bold text-sm text-brand-400">
              COT-{venta.numero}
            </span>
            <FaseBadge fase={venta.fase_comercial} />
            {venta.numero_oc_cliente && (
              <span className="text-[10px] font-medium px-2 py-0.5 rounded-full border"
                style={{ background: 'var(--surface-200)', borderColor: 'var(--border)', color: 'var(--text-muted)' }}>
                OC #{venta.numero_oc_cliente}
              </span>
            )}
          </div>

          <p className="font-semibold text-sm truncate" style={{ color: 'var(--text-primary)' }}>
            {venta.cliente || '—'}
          </p>

          <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
            {venta.rut_cliente && <span>{venta.rut_cliente} · </span>}
            {venta.fecha_oc && <span>OC: {venta.fecha_oc} · </span>}
            {venta.cond_pago && <span>{venta.cond_pago} · </span>}
            {venta.fecha_entrega && <span>Entrega: {venta.fecha_entrega}</span>}
            {venta.asesor && <span> · Asesor: {venta.asesor}</span>}
          </p>

          {/* Progress bar */}
          {venta.total_items > 0 && (
            <div className="space-y-1 pt-0.5">
              <div className="flex items-center justify-between">
                <span className="text-[10px] uppercase tracking-wider font-semibold" style={{ color: 'var(--text-faint)' }}>
                  Ítems vendidos
                </span>
                <span className="text-[10px] font-bold" style={{
                  color: venta.pct_vendido === 100 ? '#10b981' : 'var(--text-muted)'
                }}>
                  {venta.items_vendidos}/{venta.total_items} · {venta.pct_vendido}%
                </span>
              </div>
              <div className="w-full h-1.5 rounded-full overflow-hidden" style={{ backgroundColor: 'var(--surface-300)' }}>
                <div className="h-full rounded-full transition-all duration-500"
                  style={{
                    width: `${venta.pct_vendido}%`,
                    background: venta.pct_vendido === 100
                      ? 'linear-gradient(90deg,#10b981,#059669)'
                      : 'linear-gradient(90deg,#818cf8,#6366f1)',
                  }} />
              </div>
            </div>
          )}
        </div>

        {/* Right totals */}
        <div className="flex sm:flex-col items-center sm:items-end gap-4 sm:gap-1 shrink-0">
          <div className="text-right">
            <p className="text-[10px] uppercase tracking-wider font-semibold" style={{ color: 'var(--text-faint)' }}>
              Neto
            </p>
            <p className="font-bold text-sm text-brand-400">{fmtClp(venta.total_neto_clp)}</p>
          </div>
          <div className="text-right">
            <p className="text-[10px] uppercase tracking-wider font-semibold" style={{ color: 'var(--text-faint)' }}>
              Total c/IVA
            </p>
            <p className="font-bold text-base" style={{ color: 'var(--text-primary)' }}>
              {fmtClp(venta.total_con_iva_clp)}
            </p>
          </div>
          <div className="ml-2 sm:ml-0 sm:mt-2" style={{ color: 'var(--text-muted)' }}>
            {open ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
          </div>
        </div>
      </button>

      {/* Expanded items */}
      {open && (
        <div className="border-t" style={{ borderColor: 'var(--border)' }}>
          {/* OC meta */}
          {(venta.numero_oc_cliente || venta.fecha_oc || venta.cond_pago || venta.fecha_entrega) && (
            <div className="px-5 py-3 flex flex-wrap gap-x-6 gap-y-1 text-xs"
              style={{ backgroundColor: 'var(--surface-100)', borderBottom: '1px solid var(--border)' }}>
              {venta.numero_oc_cliente && (
                <span><span style={{ color: 'var(--text-faint)' }}>N° OC Cliente: </span>
                  <span className="font-semibold" style={{ color: 'var(--text-primary)' }}>{venta.numero_oc_cliente}</span>
                </span>
              )}
              {venta.fecha_oc && (
                <span><span style={{ color: 'var(--text-faint)' }}>Fecha OC: </span>
                  <span className="font-semibold" style={{ color: 'var(--text-primary)' }}>{venta.fecha_oc}</span>
                </span>
              )}
              {venta.cond_pago && (
                <span><span style={{ color: 'var(--text-faint)' }}>Cond. Pago: </span>
                  <span className="font-semibold" style={{ color: 'var(--text-primary)' }}>{venta.cond_pago}</span>
                </span>
              )}
              {venta.fecha_entrega && (
                <span><span style={{ color: 'var(--text-faint)' }}>F. Entrega: </span>
                  <span className="font-semibold" style={{ color: 'var(--text-primary)' }}>{venta.fecha_entrega}</span>
                </span>
              )}
            </div>
          )}

          {/* Items table */}
          {itemsVendidos.length === 0 ? (
            <div className="px-5 py-4 text-sm" style={{ color: 'var(--text-muted)' }}>
              Sin ítems vendidos en esta cotización.
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr style={{ backgroundColor: 'var(--surface-100)', borderBottom: '1px solid var(--border)' }}>
                    {['#', 'N° Parte', 'Descripción', 'Cant.', 'P.Unit', 'Total', 'Estado', 'OCP', 'Embarque'].map(h => (
                      <th key={h} className="px-3 py-2 text-left font-semibold uppercase tracking-wider whitespace-nowrap"
                        style={{ color: 'var(--text-faint)' }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {itemsVendidos.map((item, idx) => (
                    <tr key={item.id}
                      style={{
                        backgroundColor: idx % 2 === 0 ? 'transparent' : 'var(--surface-100)',
                        borderBottom: '1px solid var(--border)',
                      }}>
                      <td className="px-3 py-2 font-mono text-[11px]" style={{ color: 'var(--text-muted)' }}>
                        {item.item_num}
                      </td>
                      <td className="px-3 py-2 font-mono font-semibold whitespace-nowrap" style={{ color: 'var(--text-primary)' }}>
                        {item.numero_parte || '—'}
                      </td>
                      <td className="px-3 py-2 max-w-[200px] truncate" style={{ color: 'var(--text-primary)' }}
                        title={item.descripcion}>
                        {item.descripcion}
                      </td>
                      <td className="px-3 py-2 text-right" style={{ color: 'var(--text-muted)' }}>
                        {item.cantidad}
                      </td>
                      <td className="px-3 py-2 text-right font-medium whitespace-nowrap" style={{ color: 'var(--text-primary)' }}>
                        {fmtClp(item.precio_unit_venta_clp)}
                      </td>
                      <td className="px-3 py-2 text-right font-bold whitespace-nowrap text-brand-400">
                        {fmtClp(item.total_venta_clp)}
                      </td>
                      <td className="px-3 py-2 whitespace-nowrap">
                        <EstadoBadge estado={item.estado_item} />
                      </td>
                      <td className="px-3 py-2 whitespace-nowrap" style={{ color: 'var(--text-muted)' }}>
                        {item.ocp_numero ? (
                          <div>
                            <p className="font-semibold" style={{ color: 'var(--text-primary)' }}>{item.ocp_numero}</p>
                            {item.ocp_proveedor && <p className="text-[10px]">{item.ocp_proveedor}</p>}
                            {item.ocp_numero_oc && <p className="text-[10px]">OC: {item.ocp_numero_oc}</p>}
                          </div>
                        ) : '—'}
                      </td>
                      <td className="px-3 py-2 whitespace-nowrap" style={{ color: 'var(--text-muted)' }}>
                        {item.embarque_numero ? (
                          <div>
                            <p className="font-semibold" style={{ color: 'var(--text-primary)' }}>{item.embarque_numero}</p>
                            {item.embarque_awb && <p className="text-[10px]">AWB: {item.embarque_awb}</p>}
                            {item.embarque_fecha_llegada && <p className="text-[10px]">Llegada: {item.embarque_fecha_llegada}</p>}
                          </div>
                        ) : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* Totals footer */}
          <div className="px-5 py-3 flex flex-wrap justify-end gap-6 text-xs font-semibold"
            style={{ backgroundColor: 'var(--surface-100)', borderTop: '1px solid var(--border)' }}>
            <span><span style={{ color: 'var(--text-faint)' }}>Neto: </span>
              <span style={{ color: 'var(--text-primary)' }}>{fmtClp(venta.total_neto_clp)}</span>
            </span>
            <span><span style={{ color: 'var(--text-faint)' }}>IVA: </span>
              <span style={{ color: 'var(--text-primary)' }}>{fmtClp(venta.iva_clp)}</span>
            </span>
            <span><span style={{ color: 'var(--text-faint)' }}>Total: </span>
              <span className="text-brand-400 font-bold text-sm">{fmtClp(venta.total_con_iva_clp)}</span>
            </span>
          </div>
        </div>
      )}
    </div>
  )
}

// ─── Main Page ────────────────────────────────────────────────────────────────

export default function VentasPage() {
  const [ventas, setVentas] = useState<Venta[]>([])
  const [kpis, setKpis] = useState<Kpis | null>(null)
  const [mensual, setMensual] = useState<MesData[]>([])
  const [loading, setLoading] = useState(true)
  const [q, setQ] = useState('')
  const [periodo, setPeriodo] = useState<string>('')
  const [error, setError] = useState('')
  const [exporting, setExporting] = useState(false)
  const [listos, setListos] = useState<OcListaDespacho[]>([])
  const [alertas, setAlertas] = useState<OcAlertaPlazo[]>([])
  const [loadingPaneles, setLoadingPaneles] = useState(false)

  const load = useCallback(async (search?: string, per?: string) => {
    setLoading(true)
    setError('')
    try {
      const [vRes, kRes, mRes] = await Promise.all([
        ventasAPI.list(search, per),
        ventasAPI.kpis(per),
        ventasAPI.mensual(),
      ])
      setVentas(vRes.data)
      setKpis(kRes.data)
      setMensual(mRes.data)
    } catch {
      setError('No se pudieron cargar las ventas.')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load(q || undefined, periodo || undefined) }, [periodo])

  // B6/B7: Load bodega panels on mount
  useEffect(() => {
    const loadPaneles = async () => {
      setLoadingPaneles(true)
      try {
        const [lRes, aRes] = await Promise.all([
          bodegaAPI.listoParaDespachar(),
          bodegaAPI.alertasPlazo(),
        ])
        setListos(lRes.data ?? [])
        setAlertas(aRes.data ?? [])
      } catch { /* silent */ } finally {
        setLoadingPaneles(false)
      }
    }
    loadPaneles()
  }, [])

  const handleSearch = (v: string) => {
    setQ(v)
    if (v.length === 0 || v.length >= 2) load(v || undefined, periodo || undefined)
  }

  const handleExport = async () => {
    setExporting(true)
    try {
      const url = ventasAPI.exportUrl(periodo || undefined, q || undefined)
      const link = document.createElement('a')
      link.href = url
      link.download = `ventas_${new Date().toISOString().slice(0, 10)}.xlsx`
      document.body.appendChild(link)
      link.click()
      document.body.removeChild(link)
    } finally {
      setTimeout(() => setExporting(false), 1500)
    }
  }

  const totalVentas = ventas.length
  const totalItems = ventas.reduce((s, v) => s + v.total_items, 0)
  const totalItemsVendidos = ventas.reduce((s, v) => s + v.items_vendidos, 0)
  const pctGlobal = totalItems > 0 ? Math.round((totalItemsVendidos / totalItems) * 100) : 0

  return (
    <div className="space-y-6">

      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-2xl font-bold" style={{ color: 'var(--text-primary)' }}>Ventas</h1>
          <p className="text-sm mt-1" style={{ color: 'var(--text-muted)' }}>
            Cotizaciones aceptadas · pendientes de pago · pagadas
          </p>
        </div>
        <div className="flex items-center gap-2 self-start">
          <button
            onClick={handleExport}
            disabled={exporting || ventas.length === 0}
            className="btn-secondary flex items-center gap-2">
            {exporting
              ? <Loader2 className="w-4 h-4 animate-spin" />
              : <Download className="w-4 h-4" />}
            Exportar Excel
          </button>
          <button
            onClick={() => load(q || undefined, periodo || undefined)}
            className="btn-secondary flex items-center gap-2">
            <RefreshCw className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* Period selector */}
      <div className="flex items-center gap-2 flex-wrap">
        {Object.entries(PERIODO_LABELS).map(([key, label]) => (
          <button
            key={key}
            onClick={() => setPeriodo(key)}
            className={`px-4 py-1.5 rounded-full text-sm font-medium border transition-all ${
              periodo === key
                ? 'border-brand-500 bg-brand-500/10 text-brand-400'
                : 'border-transparent text-sm'
            }`}
            style={periodo !== key ? {
              backgroundColor: 'var(--surface-100)',
              borderColor: 'var(--border)',
              color: 'var(--text-muted)',
            } : {}}>
            {label}
          </button>
        ))}
      </div>

      {/* KPI cards */}
      {kpis && (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          {[
            { icon: <ShoppingCart className="w-5 h-5" />, label: 'Cotizaciones', value: kpis.total_ventas.toString(), color: 'text-brand-400' },
            { icon: <DollarSign className="w-5 h-5" />, label: 'Total Facturado', value: fmtClp(kpis.total_facturado_clp), color: 'text-brand-400' },
            { icon: <CreditCard className="w-5 h-5" />, label: 'Por Cobrar', value: fmtClp(kpis.total_x_cobrar_clp), color: 'text-amber-400' },
            { icon: <CheckCircle2 className="w-5 h-5" />, label: 'Cobrado', value: fmtClp(kpis.total_cobrado_clp), color: 'text-emerald-400' },
          ].map(({ icon, label, value, color }) => (
            <div key={label} className="rounded-2xl border p-4 flex items-center gap-3"
              style={{ backgroundColor: 'var(--surface-50)', borderColor: 'var(--border)' }}>
              <div className={`p-2 rounded-xl ${color} opacity-80`}
                style={{ backgroundColor: 'var(--surface-200)' }}>
                {icon}
              </div>
              <div className="min-w-0">
                <p className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>
                  {label}
                </p>
                <p className={`font-bold text-lg leading-tight truncate ${color}`}>{value}</p>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* B6: Listo para despachar */}
      {!loadingPaneles && listos.length > 0 && (
        <div className="rounded-2xl border p-5 space-y-3"
          style={{ backgroundColor: 'var(--surface-50)', borderColor: 'var(--border)' }}>
          <div className="flex items-center gap-2">
            <CheckCircle2 className="w-4 h-4 text-emerald-400" />
            <h2 className="font-semibold text-sm text-emerald-400">
              Listo para despachar ({listos.length})
            </h2>
          </div>
          <div className="space-y-2">
            {listos.map(oc => (
              <div key={oc.oc_cliente_id}
                className="flex items-center justify-between px-3 py-2 rounded-xl border"
                style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
                <div className="flex items-center gap-3 min-w-0">
                  <Package className="w-3.5 h-3.5 text-emerald-400 shrink-0" />
                  <div className="min-w-0">
                    <span className="font-semibold text-xs text-emerald-400">{oc.numero_oc || `OC #${oc.oc_cliente_id}`}</span>
                    <span className="text-xs ml-2" style={{ color: 'var(--text-muted)' }}>{oc.cliente}</span>
                  </div>
                </div>
                <div className="flex items-center gap-3 shrink-0 text-xs" style={{ color: 'var(--text-muted)' }}>
                  <span>{oc.items_en_bodega}/{oc.total_items} ítems</span>
                  {oc.fecha_entrega && (
                    <span className="font-mono px-2 py-0.5 rounded-lg text-[10px] bg-emerald-500/10 text-emerald-400">
                      Entrega: {oc.fecha_entrega}
                    </span>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* B7: Alertas plazo critico */}
      {!loadingPaneles && alertas.length > 0 && (
        <div className="rounded-2xl border p-5 space-y-3"
          style={{ backgroundColor: 'var(--surface-50)', borderColor: 'rgba(239,68,68,0.3)' }}>
          <div className="flex items-center gap-2">
            <Clock className="w-4 h-4 text-red-400" />
            <h2 className="font-semibold text-sm text-red-400">
              Plazo crítico ≤ 3 días hábiles ({alertas.length})
            </h2>
          </div>
          <div className="space-y-2">
            {alertas.map(oc => (
              <div key={oc.oc_cliente_id}
                className="flex items-center justify-between px-3 py-2 rounded-xl border border-red-500/20"
                style={{ backgroundColor: 'rgba(239,68,68,0.05)' }}>
                <div className="flex items-center gap-3 min-w-0">
                  <Clock className="w-3.5 h-3.5 text-red-400 shrink-0" />
                  <div className="min-w-0">
                    <span className="font-semibold text-xs text-red-400">{oc.numero_oc || `OC #${oc.oc_cliente_id}`}</span>
                    <span className="text-xs ml-2" style={{ color: 'var(--text-muted)' }}>{oc.cliente}</span>
                  </div>
                </div>
                <div className="flex items-center gap-3 shrink-0 text-xs" style={{ color: 'var(--text-muted)' }}>
                  <span>{oc.items_en_bodega}/{oc.total_items} en bodega</span>
                  <span className="font-mono px-2 py-0.5 rounded-lg text-[10px] bg-red-500/10 text-red-400 font-bold">
                    {oc.dias_habiles_restantes <= 0 ? 'VENCIDO' : `${oc.dias_habiles_restantes}d hábiles`}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Global progress + chart row */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">

        {/* Progress summary */}
        <div className="rounded-2xl border p-5 space-y-4"
          style={{ backgroundColor: 'var(--surface-50)', borderColor: 'var(--border)' }}>
          <div className="flex items-center gap-2">
            <BarChart2 className="w-4 h-4 text-brand-400" />
            <h2 className="font-semibold text-sm" style={{ color: 'var(--text-primary)' }}>
              Resumen de ítems
            </h2>
          </div>
          <div className="space-y-3">
            <div className="space-y-1.5">
              <div className="flex items-center justify-between text-xs">
                <span style={{ color: 'var(--text-muted)' }}>Ítems vendidos</span>
                <span className="font-bold text-brand-400">{totalItemsVendidos} / {totalItems}</span>
              </div>
              <div className="w-full h-2 rounded-full overflow-hidden" style={{ backgroundColor: 'var(--surface-300)' }}>
                <div className="h-full rounded-full transition-all duration-500"
                  style={{
                    width: `${pctGlobal}%`,
                    background: pctGlobal === 100
                      ? 'linear-gradient(90deg,#10b981,#059669)'
                      : 'linear-gradient(90deg,#818cf8,#6366f1)',
                  }} />
              </div>
              <p className="text-xs" style={{ color: 'var(--text-faint)' }}>
                {pctGlobal}% del total de ítems vendidos
              </p>
            </div>
            {/* State legend */}
            <div className="pt-2 grid grid-cols-2 gap-1.5">
              {[
                { key: 'embarcado', label: 'Embarcados', color: '#10b981' },
                { key: 'preparado', label: 'Preparados', color: '#f59e0b' },
                { key: 'comprado', label: 'Comprados', color: '#a78bfa' },
                { key: 'cerrado', label: 'Cerrados', color: '#60a5fa' },
              ].map(({ key, label, color }) => {
                const count = ventas.flatMap(v => v.items).filter(i => i.estado_item === key).length
                return (
                  <div key={key} className="flex items-center gap-1.5 text-xs">
                    <div className="w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: color }} />
                    <span style={{ color: 'var(--text-muted)' }}>{label}:</span>
                    <span className="font-bold" style={{ color: 'var(--text-primary)' }}>{count}</span>
                  </div>
                )
              })}
            </div>
          </div>
        </div>

        {/* Monthly chart */}
        <div className="lg:col-span-2 rounded-2xl border p-5"
          style={{ backgroundColor: 'var(--surface-50)', borderColor: 'var(--border)' }}>
          <div className="flex items-center justify-between mb-4">
            <div className="flex items-center gap-2">
              <TrendingUp className="w-4 h-4 text-brand-400" />
              <h2 className="font-semibold text-sm" style={{ color: 'var(--text-primary)' }}>
                Ventas Mensuales {new Date().getFullYear()}
              </h2>
            </div>
          </div>
          {mensual.length > 0 ? (
            <ResponsiveContainer width="100%" height={180}>
              <AreaChart data={mensual} margin={{ top: 5, right: 10, left: 0, bottom: 0 }}>
                <defs>
                  <linearGradient id="colorNeto" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="#818cf8" stopOpacity={0.4} />
                    <stop offset="95%" stopColor="#818cf8" stopOpacity={0.02} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
                <XAxis dataKey="mes" tick={{ fontSize: 11, fill: 'var(--text-faint)' }} axisLine={false} tickLine={false} />
                <YAxis
                  tick={{ fontSize: 10, fill: 'var(--text-faint)' }}
                  axisLine={false} tickLine={false}
                  tickFormatter={(v) => v >= 1_000_000 ? `$${(v/1_000_000).toFixed(1)}M` : v >= 1_000 ? `$${(v/1_000).toFixed(0)}K` : `$${v}`}
                  width={55}
                />
                <Tooltip content={<ChartTooltip />} />
                <Area
                  type="monotone" dataKey="neto_clp" name="Neto"
                  stroke="#818cf8" strokeWidth={2}
                  fill="url(#colorNeto)"
                  dot={{ r: 3, fill: '#818cf8', strokeWidth: 0 }}
                  activeDot={{ r: 5, fill: '#818cf8' }}
                />
              </AreaChart>
            </ResponsiveContainer>
          ) : (
            <div className="h-[180px] flex items-center justify-center text-sm" style={{ color: 'var(--text-muted)' }}>
              Sin datos para el año actual
            </div>
          )}
        </div>
      </div>

      {/* Search */}
      <div className="relative">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4" style={{ color: 'var(--text-faint)' }} />
        <input
          type="text"
          placeholder="Buscar por cliente, cotización, referencia…"
          value={q}
          onChange={e => handleSearch(e.target.value)}
          className="w-full pl-10 pr-4 py-2.5 rounded-xl border text-sm focus:outline-none focus:ring-2 focus:ring-brand-500/40"
          style={{
            backgroundColor: 'var(--surface-100)',
            borderColor: 'var(--border)',
            color: 'var(--text-primary)',
          }}
        />
      </div>

      {/* Error */}
      {error && (
        <div className="rounded-xl border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-400">
          {error}
        </div>
      )}

      {/* Loading */}
      {loading && (
        <div className="flex items-center justify-center py-16">
          <Loader2 className="w-7 h-7 animate-spin text-brand-400" />
        </div>
      )}

      {/* Empty */}
      {!loading && !error && ventas.length === 0 && (
        <div className="rounded-2xl border py-16 text-center"
          style={{ backgroundColor: 'var(--surface-50)', borderColor: 'var(--border)' }}>
          <TrendingUp className="w-10 h-10 mx-auto mb-3 opacity-20" style={{ color: 'var(--text-muted)' }} />
          <p className="text-sm font-medium" style={{ color: 'var(--text-muted)' }}>
            No hay ventas{periodo ? ` para el período seleccionado` : ''}
          </p>
        </div>
      )}

      {/* Ventas list */}
      {!loading && ventas.length > 0 && (
        <div className="space-y-3">
          <p className="text-xs font-semibold uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>
            {totalVentas} {totalVentas === 1 ? 'cotización' : 'cotizaciones'} · {PERIODO_LABELS[periodo]}
          </p>
          {ventas.map(v => <VentaCard key={v.id} venta={v} />)}
        </div>
      )}
    </div>
  )
}
