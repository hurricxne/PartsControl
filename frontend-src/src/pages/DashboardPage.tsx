import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { FileSpreadsheet, CheckCircle2, Users, TrendingUp, RefreshCw, BarChart3 } from 'lucide-react'
import { AreaChart, Area, PieChart, Pie, Cell, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from 'recharts'
import { cotizacionesAPI } from '../services/api'
import { useTheme } from '../context/ThemeContext'

const MONTHS = ['Ene','Feb','Mar','Abr','May','Jun','Jul','Ago','Sep','Oct','Nov','Dic']
const PIE_COLORS = ['#1a5cf0', '#ef4444']

function StatCard({ icon: Icon, label, value, sub, color, trend }: any) {
  return (
    <div className="card p-5 flex flex-col gap-2 hover:border-brand-600/40 transition-all duration-200">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium uppercase tracking-wide" style={{ color: 'var(--text-faint)' }}>{label}</span>
        <div className={`w-8 h-8 rounded-xl flex items-center justify-center ${color}`}>
          <Icon className="w-4 h-4" />
        </div>
      </div>
      <div className="flex items-end gap-2">
        <span className="text-3xl font-bold" style={{ color: 'var(--text-primary)' }}>{value}</span>
        {trend && <span className="text-xs text-emerald-500 font-semibold mb-1">{trend}</span>}
      </div>
      <span className="text-xs" style={{ color: 'var(--text-faint)' }}>{sub}</span>
    </div>
  )
}

export default function DashboardPage() {
  const { theme } = useTheme()
  const gridColor  = theme === 'dark' ? '#1e293b' : '#e2e8f0'
  const tickColor  = theme === 'dark' ? '#64748b' : '#94a3b8'

  const { data: cotizaciones = [], isLoading } = useQuery<any[]>({
    queryKey: ['cotizaciones'],
    queryFn: () => cotizacionesAPI.list().then(r => r.data),
  })

  const stats = useMemo(() => {
    const total     = cotizaciones.length
    const completado = cotizaciones.filter(c => c.estado === 'completado').length
    const procesando = cotizaciones.filter(c => ['procesando','pendiente','esperando_agente'].includes(c.estado)).length
    const clientes   = new Set(cotizaciones.map(c => c.cliente).filter(Boolean)).size
    const partesOk   = cotizaciones.reduce((a, c) => a + (c.items_encontrados || 0), 0)
    const partesTotal = cotizaciones.reduce((a, c) => a + (c.total_items || 0), 0)
    const tasa       = partesTotal > 0 ? Math.round(partesOk / partesTotal * 100) : 0
    return { total, completado, procesando, clientes, partesOk, partesTotal, tasa }
  }, [cotizaciones])

  const monthlyData = useMemo(() => {
    const map: Record<string, { mes: string; cotizaciones: number; partes: number }> = {}
    cotizaciones.forEach(c => {
      const d = new Date(c.created_at)
      const key = `${MONTHS[d.getMonth()]} ${d.getFullYear()}`
      if (!map[key]) map[key] = { mes: key, cotizaciones: 0, partes: 0 }
      map[key].cotizaciones++
      map[key].partes += c.items_encontrados || 0
    })
    const arr = Object.values(map).slice(-6)
    return arr.length ? arr : [{ mes: 'Sin datos', cotizaciones: 0, partes: 0 }]
  }, [cotizaciones])

  const pieData = useMemo(() => [
    { name: 'Encontradas',     value: stats.partesOk },
    { name: 'No encontradas',  value: stats.partesTotal - stats.partesOk },
  ], [stats])

  const topClientes = useMemo(() => {
    const map: Record<string, number> = {}
    cotizaciones.forEach(c => { if (c.cliente) map[c.cliente] = (map[c.cliente] || 0) + (c.total_items || 0) })
    return Object.entries(map).sort((a,b) => b[1]-a[1]).slice(0,6).map(([cliente, partes]) => ({ cliente: cliente.slice(0,22), partes }))
  }, [cotizaciones])

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64 gap-3" style={{ color: 'var(--text-muted)' }}>
        <RefreshCw className="w-5 h-5 animate-spin" />
        <span>Cargando datos...</span>
      </div>
    )
  }

  return (
    <div className="space-y-6 max-w-5xl mx-auto animate-slide-up">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold" style={{ color: 'var(--text-primary)' }}>Dashboard</h1>
        <p className="text-sm mt-1" style={{ color: 'var(--text-muted)' }}>Resumen de actividad y métricas del sistema</p>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard icon={FileSpreadsheet} label="Cotizaciones"    value={stats.total}     sub="Total historial"        color="bg-brand-500/10 text-brand-500" />
        <StatCard icon={TrendingUp}      label="Partes validadas" value={stats.partesOk}  sub={`de ${stats.partesTotal} totales`} color="bg-emerald-500/10 text-emerald-500" trend={`${stats.tasa}%`} />
        <StatCard icon={CheckCircle2}    label="Completadas"     value={stats.completado} sub="Con resultado Excel"    color="bg-violet-500/10 text-violet-500" />
        <StatCard icon={Users}           label="Clientes"        value={stats.clientes}   sub="Empresas distintas"    color="bg-amber-500/10 text-amber-500" />
      </div>

      {/* Charts row */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* Monthly chart */}
        <div className="lg:col-span-2 rounded-2xl border p-5" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
          <div className="flex items-center justify-between mb-5">
            <div>
              <h3 className="font-semibold text-sm" style={{ color: 'var(--text-primary)' }}>Actividad mensual</h3>
              <p className="text-xs mt-0.5" style={{ color: 'var(--text-muted)' }}>Cotizaciones y partes por mes</p>
            </div>
            <BarChart3 className="w-4 h-4 text-brand-500" />
          </div>
          <ResponsiveContainer width="100%" height={220}>
            <AreaChart data={monthlyData}>
              <defs>
                <linearGradient id="gCot" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%"  stopColor="#1a5cf0" stopOpacity={0.3} />
                  <stop offset="95%" stopColor="#1a5cf0" stopOpacity={0} />
                </linearGradient>
                <linearGradient id="gParts" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%"  stopColor="#22c55e" stopOpacity={0.3} />
                  <stop offset="95%" stopColor="#22c55e" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke={gridColor} />
              <XAxis dataKey="mes" tick={{ fontSize: 11, fill: tickColor }} axisLine={false} tickLine={false} />
              <YAxis allowDecimals={false} tick={{ fontSize: 11, fill: tickColor }} axisLine={false} tickLine={false} />
              <Tooltip contentStyle={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)', borderRadius: 12, fontSize: 12 }} />
              <Legend iconType="circle" iconSize={8} wrapperStyle={{ fontSize: 11, color: tickColor }} />
              <Area type="monotone" dataKey="cotizaciones" name="Cotizaciones" stroke="#1a5cf0" fill="url(#gCot)" strokeWidth={2} />
              <Area type="monotone" dataKey="partes"       name="Partes OK"   stroke="#22c55e" fill="url(#gParts)" strokeWidth={2} />
            </AreaChart>
          </ResponsiveContainer>
        </div>

        {/* Pie chart */}
        <div className="rounded-2xl border p-5" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
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
                {pieData.map((_, i) => <Cell key={i} fill={PIE_COLORS[i]} />)}
              </Pie>
              <Tooltip contentStyle={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)', borderRadius: 12, fontSize: 12 }} />
            </PieChart>
          </ResponsiveContainer>
          <div className="flex flex-col gap-2 mt-2">
            {pieData.map((item, i) => (
              <div key={item.name} className="flex items-center gap-2">
                <span className="w-2.5 h-2.5 rounded-full shrink-0" style={{ backgroundColor: PIE_COLORS[i] }} />
                <span className="text-xs" style={{ color: 'var(--text-muted)' }}>{item.name}</span>
                <span className="text-xs font-semibold ml-auto" style={{ color: 'var(--text-primary)' }}>{item.value}</span>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Bottom row */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Recent cotizaciones */}
        <div className="rounded-2xl border p-5" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
          <h3 className="font-semibold text-sm mb-4" style={{ color: 'var(--text-primary)' }}>Últimas cotizaciones</h3>
          {cotizaciones.slice(0, 5).length === 0 ? (
            <p className="text-sm text-center py-6" style={{ color: 'var(--text-faint)' }}>Sin cotizaciones aún</p>
          ) : (
            <div className="space-y-2">
              {cotizaciones.slice(0, 5).map((c: any) => (
                <div key={c.id} className="flex items-center gap-3 py-2">
                  <div className={`w-2 h-2 rounded-full shrink-0 ${
                    c.estado === 'completado' ? 'bg-green-400' :
                    c.estado === 'error'      ? 'bg-red-400'   : 'bg-amber-400'
                  }`} />
                  <span className="text-sm font-medium" style={{ color: 'var(--text-primary)' }}>COT-{c.numero}</span>
                  {c.cliente && <span className="text-xs truncate flex-1" style={{ color: 'var(--text-muted)' }}>{c.cliente}</span>}
                  <span className="text-xs shrink-0" style={{ color: 'var(--text-faint)' }}>
                    {new Date(c.created_at).toLocaleDateString('es-CL')}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Top clients */}
        <div className="rounded-2xl border p-5" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
          <h3 className="font-semibold text-sm mb-4" style={{ color: 'var(--text-primary)' }}>Top clientes</h3>
          {topClientes.length === 0 ? (
            <p className="text-sm text-center py-6" style={{ color: 'var(--text-faint)' }}>Sin datos aún</p>
          ) : (
            <div className="space-y-3">
              {topClientes.map(({ cliente, partes }) => (
                <div key={cliente} className="flex items-center gap-3">
                  <span className="text-sm flex-1 truncate" style={{ color: 'var(--text-muted)' }}>{cliente}</span>
                  <div className="flex-1 h-1.5 rounded-full overflow-hidden" style={{ backgroundColor: 'var(--surface-300)' }}>
                    <div
                      className="h-full bg-brand-500 rounded-full"
                      style={{ width: `${topClientes[0] ? (partes / topClientes[0].partes) * 100 : 0}%` }}
                    />
                  </div>
                  <span className="text-xs font-semibold w-8 text-right text-brand-500">{partes}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
