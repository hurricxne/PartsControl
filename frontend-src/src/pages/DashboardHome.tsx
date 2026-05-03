import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  AreaChart, Area, BarChart, Bar, PieChart, Pie, Cell,
  XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend
} from 'recharts'
import {
  FileSpreadsheet, TrendingUp, CheckCircle2, Clock,
  Package, Users, RefreshCw, ArrowUpRight
} from 'lucide-react'
import { cotizacionesAPI } from '../services/api'
import { useTheme } from '../context/ThemeContext'

/* ── Helpers ──────────────────────────────────────────────── */
function fmt(n: number) { return n.toLocaleString('es-CL') }

const MONTHS = ['Ene','Feb','Mar','Abr','May','Jun','Jul','Ago','Sep','Oct','Nov','Dic']
const PIE_COLORS = ['#1a5cf0', '#94a3b8']
const BAR_COLOR  = '#1a5cf0'

/* ── Themed tooltip ───────────────────────────────────────── */
function ThemedTooltip({ active, payload, label }: any) {
  if (!active || !payload?.length) return null
  return (
    <div className="rounded-xl px-3 py-2 shadow-xl text-xs border"
         style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)', color: 'var(--text-primary)' }}>
      <p className="font-semibold mb-1" style={{ color: 'var(--text-muted)' }}>{label}</p>
      {payload.map((p: any) => (
        <p key={p.name}><span style={{ color: p.color }}>{p.name}: </span>{p.value}</p>
      ))}
    </div>
  )
}

/* ── Stat card ────────────────────────────────────────────── */
function StatCard({ icon: Icon, label, value, sub, color, trend }: {
  icon: any; label: string; value: string | number; sub: string; color: string; trend?: string
}) {
  return (
    <div className="rounded-2xl p-5 border flex flex-col gap-3 hover:border-brand-500/30 transition-all duration-200"
         style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>{label}</span>
        <div className={`w-9 h-9 rounded-xl flex items-center justify-center ${color}`}>
          <Icon className="w-4.5 h-4.5" />
        </div>
      </div>
      <div>
        <span className="text-3xl font-bold" style={{ color: 'var(--text-primary)' }}>{value}</span>
        {trend && (
          <span className="ml-2 inline-flex items-center gap-0.5 text-xs font-medium text-emerald-500">
            <ArrowUpRight className="w-3 h-3" />{trend}
          </span>
        )}
      </div>
      <span className="text-xs" style={{ color: 'var(--text-muted)' }}>{sub}</span>
    </div>
  )
}

/* ── Main component ───────────────────────────────────────── */
export default function DashboardHome() {
  const { theme } = useTheme()
  const gridColor = theme === 'dark' ? '#1e293b' : '#e2e8f0'
  const axisColor = theme === 'dark' ? '#475569' : '#94a3b8'

  const { data: raw = [], isLoading } = useQuery<any[]>({
    queryKey: ['cotizaciones'],
    queryFn: () => cotizacionesAPI.list().then(r => r.data),
  })

  /* ── Computed analytics ───────────────────────────────── */
  const stats = useMemo(() => {
    const total      = raw.length
    const completado = raw.filter(c => c.estado === 'completado').length
    const procesando = raw.filter(c => ['procesando','pendiente'].includes(c.estado)).length
    const clientes   = new Set(raw.map(c => c.cliente).filter(Boolean)).size
    const partesOk   = raw.reduce((a, c) => a + (c.items_encontrados || 0), 0)
    const partesTotal= raw.reduce((a, c) => a + (c.total_items || 0), 0)
    const tasa       = partesTotal > 0 ? Math.round((partesOk / partesTotal) * 100) : 0
    return { total, completado, procesando, clientes, partesOk, partesTotal, tasa }
  }, [raw])

  /* ── Monthly chart data ───────────────────────────────── */
  const monthlyData = useMemo(() => {
    const map: Record<string, { mes: string; cotizaciones: number; partes: number }> = {}
    raw.forEach(c => {
      const d   = new Date(c.created_at)
      const key = `${MONTHS[d.getMonth()]} ${d.getFullYear()}`
      if (!map[key]) map[key] = { mes: key, cotizaciones: 0, partes: 0 }
      map[key].cotizaciones++
      map[key].partes += c.items_encontrados || 0
    })
    // Last 6 months ordering
    const sorted = Object.values(map).slice(-6)
    return sorted.length ? sorted : [{ mes: 'Sin datos', cotizaciones: 0, partes: 0 }]
  }, [raw])

  /* ── Pie chart data ───────────────────────────────────── */
  const pieData = useMemo(() => [
    { name: 'Encontradas',     value: stats.partesOk },
    { name: 'No encontradas',  value: stats.partesTotal - stats.partesOk },
  ], [stats])

  /* ── Top clients bar chart ────────────────────────────── */
  const clientData = useMemo(() => {
    const map: Record<string, number> = {}
    raw.forEach(c => {
      if (!c.cliente) return
      map[c.cliente] = (map[c.cliente] || 0) + (c.total_items || 0)
    })
    return Object.entries(map)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 6)
      .map(([cliente, partes]) => ({ cliente: cliente.slice(0, 16), partes }))
  }, [raw])

  /* ── Recent cotizaciones ──────────────────────────────── */
  const recent = useMemo(() => raw.slice(0, 5), [raw])

  if (isLoading) return (
    <div className="flex items-center justify-center h-64 gap-3" style={{ color: 'var(--text-muted)' }}>
      <RefreshCw className="w-5 h-5 animate-spin" />
      <span>Cargando datos...</span>
    </div>
  )

  return (
    <div className="space-y-6 max-w-7xl animate-slide-up">

      {/* ── Page title ────────────────────────────────────── */}
      <div>
        <h1 className="text-2xl font-bold" style={{ color: 'var(--text-primary)' }}>Dashboard</h1>
        <p className="text-sm mt-1" style={{ color: 'var(--text-muted)' }}>
          Resumen de actividad y métricas del sistema
        </p>
      </div>

      {/* ── Stat cards ────────────────────────────────────── */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard icon={FileSpreadsheet} label="Cotizaciones"    value={fmt(stats.total)}      sub="Total historial"          color="bg-brand-500/10 text-brand-500" />
        <StatCard icon={Package}         label="Partes validadas" value={fmt(stats.partesOk)}   sub={`de ${fmt(stats.partesTotal)} totales`} color="bg-emerald-500/10 text-emerald-500" trend={`${stats.tasa}%`} />
        <StatCard icon={CheckCircle2}    label="Completadas"     value={fmt(stats.completado)}  sub="Con resultado Excel"       color="bg-violet-500/10 text-violet-500" />
        <StatCard icon={Users}           label="Clientes"        value={fmt(stats.clientes)}    sub="Empresas distintas"        color="bg-amber-500/10 text-amber-500" />
      </div>


      {/* ── Charts row ────────────────────────────────────── */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">

        {/* Area chart – cotizaciones por mes */}
        <div className="lg:col-span-2 rounded-2xl border p-5"
             style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
          <div className="flex items-center justify-between mb-5">
            <div>
              <h3 className="font-semibold text-sm" style={{ color: 'var(--text-primary)' }}>Actividad mensual</h3>
              <p className="text-xs mt-0.5" style={{ color: 'var(--text-muted)' }}>Cotizaciones y partes por mes</p>
            </div>
            <TrendingUp className="w-4 h-4 text-brand-500" />
          </div>
          <ResponsiveContainer width="100%" height={220}>
            <AreaChart data={monthlyData}>
              <defs>
                <linearGradient id="cot" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%"  stopColor="#1a5cf0" stopOpacity={0.3} />
                  <stop offset="95%" stopColor="#1a5cf0" stopOpacity={0}   />
                </linearGradient>
                <linearGradient id="parts" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%"  stopColor="#22c55e" stopOpacity={0.3} />
                  <stop offset="95%" stopColor="#22c55e" stopOpacity={0}   />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke={gridColor} />
              <XAxis dataKey="mes"       tick={{ fontSize: 11, fill: axisColor }} axisLine={false} tickLine={false} />
              <YAxis allowDecimals={false} tick={{ fontSize: 11, fill: axisColor }} axisLine={false} tickLine={false} />
              <Tooltip content={<ThemedTooltip />} />
              <Legend iconType="circle" iconSize={8} wrapperStyle={{ fontSize: 11, color: axisColor }} />
              <Area type="monotone" dataKey="cotizaciones" name="Cotizaciones" stroke="#1a5cf0" fill="url(#cot)"   strokeWidth={2} />
              <Area type="monotone" dataKey="partes"       name="Partes OK"    stroke="#22c55e" fill="url(#parts)" strokeWidth={2} />
            </AreaChart>
          </ResponsiveContainer>
        </div>

        {/* Pie chart – encontradas vs no */}
        <div className="rounded-2xl border p-5"
             style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
          <div className="flex items-center justify-between mb-5">
            <div>
              <h3 className="font-semibold text-sm" style={{ color: 'var(--text-primary)' }}>Tasa de éxito</h3>
              <p className="text-xs mt-0.5" style={{ color: 'var(--text-muted)' }}>Partes encontradas</p>
            </div>
            <span className="text-lg font-bold text-brand-500">{stats.tasa}%</span>
          </div>
          <ResponsiveContainer width="100%" height={180}>
            <PieChart>
              <Pie data={pieData} cx="50%" cy="50%" innerRadius={55} outerRadius={80} paddingAngle={3} dataKey="value">
                {pieData.map((_, i) => (
                  <Cell key={i} fill={PIE_COLORS[i]} />
                ))}
              </Pie>
              <Tooltip content={<ThemedTooltip />} />
            </PieChart>
          </ResponsiveContainer>
          <div className="flex flex-col gap-2 mt-2">
            {pieData.map((d, i) => (
              <div key={d.name} className="flex items-center justify-between text-xs">
                <div className="flex items-center gap-2">
                  <span className="w-2.5 h-2.5 rounded-full shrink-0" style={{ backgroundColor: PIE_COLORS[i] }} />
                  <span style={{ color: 'var(--text-muted)' }}>{d.name}</span>
                </div>
                <span className="font-semibold" style={{ color: 'var(--text-primary)' }}>{fmt(d.value)}</span>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* ── Bottom row ────────────────────────────────────── */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">

        {/* Bar chart – top clientes */}
        <div className="rounded-2xl border p-5"
             style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
          <div className="flex items-center justify-between mb-5">
            <div>
              <h3 className="font-semibold text-sm" style={{ color: 'var(--text-primary)' }}>Clientes más activos</h3>
              <p className="text-xs mt-0.5" style={{ color: 'var(--text-muted)' }}>Por partes solicitadas</p>
            </div>
            <Users className="w-4 h-4 text-amber-500" />
          </div>
          {clientData.length === 0 ? (
            <div className="flex items-center justify-center h-40 text-sm" style={{ color: 'var(--text-faint)' }}>
              Sin datos de clientes aún
            </div>
          ) : (
            <ResponsiveContainer width="100%" height={200}>
              <BarChart data={clientData} layout="vertical">
                <CartesianGrid strokeDasharray="3 3" stroke={gridColor} horizontal={false} />
                <XAxis type="number" tick={{ fontSize: 11, fill: axisColor }} axisLine={false} tickLine={false} />
                <YAxis type="category" dataKey="cliente" tick={{ fontSize: 10, fill: axisColor }} axisLine={false} tickLine={false} width={80} />
                <Tooltip content={<ThemedTooltip />} />
                <Bar dataKey="partes" name="Partes" fill={BAR_COLOR} radius={[0, 4, 4, 0]} />
              </BarChart>
            </ResponsiveContainer>
          )}
        </div>

        {/* Recent cotizaciones */}
        <div className="rounded-2xl border p-5"
             style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
          <div className="flex items-center justify-between mb-5">
            <div>
              <h3 className="font-semibold text-sm" style={{ color: 'var(--text-primary)' }}>Últimas cotizaciones</h3>
              <p className="text-xs mt-0.5" style={{ color: 'var(--text-muted)' }}>Actividad reciente</p>
            </div>
            <Clock className="w-4 h-4" style={{ color: 'var(--text-faint)' }} />
          </div>

          {recent.length === 0 ? (
            <div className="flex items-center justify-center h-40 text-sm" style={{ color: 'var(--text-faint)' }}>
              Sin cotizaciones aún
            </div>
          ) : (
            <div className="space-y-2.5">
              {recent.map((c: any) => (
                <div key={c.id} className="flex items-center gap-3 p-3 rounded-xl transition-colors hover:bg-[var(--surface-200)]">
                  <StatusDot estado={c.estado} />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-semibold" style={{ color: 'var(--text-primary)' }}>COT-{c.numero}</span>
                      {c.cliente && <span className="text-xs truncate" style={{ color: 'var(--text-muted)' }}>{c.cliente}</span>}
                    </div>
                    <p className="text-xs mt-0.5" style={{ color: 'var(--text-faint)' }}>
                      {c.total_items} partes · {new Date(c.created_at).toLocaleDateString('es-CL')}
                    </p>
                  </div>
                  <EstadoBadge estado={c.estado} />
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* ── In-process alert ──────────────────────────────── */}
      {stats.procesando > 0 && (
        <div className="flex items-center gap-3 px-5 py-3.5 rounded-2xl border border-amber-500/20 bg-amber-500/5">
          <RefreshCw className="w-4 h-4 text-amber-500 animate-spin shrink-0" />
          <p className="text-sm" style={{ color: 'var(--text-muted)' }}>
            <span className="font-semibold text-amber-500">{stats.procesando} cotización(es)</span> en procesamiento actualmente.
            Los datos se actualizan automáticamente.
          </p>
        </div>
      )}
    </div>
  )
}

function StatusDot({ estado }: { estado: string }) {
  const colors: Record<string, string> = {
    completado: 'bg-emerald-400',
    procesando: 'bg-brand-400',
    pendiente:  'bg-amber-400',
    error:      'bg-red-400',
  }
  return <span className={`w-2 h-2 rounded-full shrink-0 ${colors[estado] ?? 'bg-gray-400'}`} />
}

function EstadoBadge({ estado }: { estado: string }) {
  const map: Record<string, { label: string; cls: string }> = {
    completado: { label: 'Completado', cls: 'text-emerald-500 bg-emerald-500/10' },
    procesando: { label: 'Procesando', cls: 'text-brand-500 bg-brand-500/10' },
    pendiente:  { label: 'Pendiente',  cls: 'text-amber-500 bg-amber-500/10'  },
    error:      { label: 'Error',      cls: 'text-red-500 bg-red-500/10'      },
  }
  const { label, cls } = map[estado] ?? { label: estado, cls: '' }
  return <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${cls}`}>{label}</span>
}
