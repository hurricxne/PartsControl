import { BarChart3, TrendingUp, TrendingDown, DollarSign, Users, FileText, Package, Download } from 'lucide-react'
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer,
  PieChart, Pie, Cell, AreaChart, Area, CartesianGrid, Legend,
} from 'recharts'

const ventasMensuales = [
  { mes: 'Ene', venta: 8200000,  costo: 6100000,  utilidad: 2100000 },
  { mes: 'Feb', venta: 11400000, costo: 8300000,  utilidad: 3100000 },
  { mes: 'Mar', venta: 15800000, costo: 11500000, utilidad: 4300000 },
]

const porAsesor = [
  { asesor: 'Aldo Ibaceta',     cotizaciones: 18, ventas: 12, monto: 14200000 },
  { asesor: 'Ricardo Castillo', cotizaciones: 15, ventas: 10, monto: 11800000 },
  { asesor: 'Francisco Lopez',  cotizaciones: 11, ventas:  7, monto: 9400000  },
]

const estadosCot = [
  { name: 'Aceptadas', value: 28, color: '#10b981' },
  { name: 'Enviadas',  value: 8,  color: '#60a5fa' },
  { name: 'Perdidas',  value: 6,  color: '#f87171' },
]

const topClientes = [
  { cliente: 'Codelco',         monto: 24200000 },
  { cliente: 'H-E Parts',       monto: 18500000 },
  { cliente: 'Anglo American',  monto: 14600000 },
  { cliente: 'Los Pelambres',   monto: 9800000  },
  { cliente: 'Cap Minería',     monto: 5300000  },
]

const fmtClp = (v: number) => `$${(v / 1000000).toFixed(1)}M`
const fmtK   = (v: number) => `$${(v / 1000).toFixed(0)}K`

const TooltipCustom = ({ active, payload, label }: any) => {
  if (!active || !payload?.length) return null
  return (
    <div className="rounded-xl border p-3 text-xs shadow-xl"
      style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)', color: 'var(--text-primary)' }}>
      <p className="font-semibold mb-1">{label}</p>
      {payload.map((p: any) => (
        <p key={p.name} style={{ color: p.color }}>{p.name}: {fmtClp(p.value)}</p>
      ))}
    </div>
  )
}

export default function ReportesPage() {
  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-2xl font-bold" style={{ color: 'var(--text-primary)' }}>Reportes</h1>
          <p className="text-sm mt-1" style={{ color: 'var(--text-muted)' }}>
            Análisis comercial — ventas y márgenes 2026
          </p>
        </div>
        <button className="btn-secondary flex items-center justify-center gap-2 w-full sm:w-auto shrink-0">
          <Download className="w-4 h-4" /> Exportar Excel
        </button>
      </div>

      {/* KPIs */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        {[
          { icon: DollarSign, label: 'Facturación',  value: '$35.4M', delta: '+22% vs 2025',  up: true,  color: 'text-emerald-500', bg: 'bg-emerald-500/10' },
          { icon: TrendingUp, label: 'Margen prom.', value: '31.2%',  delta: '+3.1pp vs 2025',up: true,  color: 'text-brand-400',   bg: 'bg-brand-500/10'   },
          { icon: FileText,   label: 'Cotizaciones', value: '42',     delta: '+8 vs 2025',    up: true,  color: 'text-blue-400',    bg: 'bg-blue-500/10'    },
          { icon: Package,    label: 'Embarques',    value: '18',     delta: '3 en tránsito', up: null,  color: 'text-amber-400',   bg: 'bg-amber-500/10'   },
        ].map(s => (
          <div key={s.label} className="rounded-2xl p-3 sm:p-4 border" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
            <div className={`w-8 h-8 sm:w-9 sm:h-9 rounded-xl flex items-center justify-center mb-2 sm:mb-3 ${s.bg}`}>
              <s.icon className={`w-4 h-4 sm:w-5 sm:h-5 ${s.color}`} />
            </div>
            <p className="text-[10px] uppercase tracking-widest leading-tight" style={{ color: 'var(--text-faint)' }}>{s.label}</p>
            <p className={`text-xl sm:text-2xl font-bold mt-0.5 ${s.color}`}>{s.value}</p>
            <p className={`text-xs mt-0.5 flex items-center gap-1 ${s.up === true ? 'text-emerald-500' : s.up === false ? 'text-red-400' : ''}`}
              style={s.up === null ? { color: 'var(--text-muted)' } : {}}>
              {s.up === true && <TrendingUp className="w-3 h-3 shrink-0" />}
              {s.up === false && <TrendingDown className="w-3 h-3 shrink-0" />}
              {s.delta}
            </p>
          </div>
        ))}
      </div>

      {/* Charts row 1 */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* Ventas mensuales area */}
        <div className="lg:col-span-2 rounded-2xl p-4 border" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
          <h3 className="font-semibold text-sm mb-4" style={{ color: 'var(--text-primary)' }}>Ventas vs Costo vs Utilidad (CLP)</h3>
          <ResponsiveContainer width="100%" height={200}>
            <AreaChart data={ventasMensuales}>
              <defs>
                <linearGradient id="gVenta" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="#6366f1" stopOpacity={0.3} />
                  <stop offset="95%" stopColor="#6366f1" stopOpacity={0} />
                </linearGradient>
                <linearGradient id="gUtil" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="#10b981" stopOpacity={0.3} />
                  <stop offset="95%" stopColor="#10b981" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
              <XAxis dataKey="mes" tick={{ fontSize: 11, fill: 'var(--text-faint)' }} axisLine={false} tickLine={false} />
              <YAxis tickFormatter={fmtK} tick={{ fontSize: 10, fill: 'var(--text-faint)' }} axisLine={false} tickLine={false} width={50} />
              <Tooltip content={<TooltipCustom />} />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              <Area type="monotone" dataKey="venta"    name="Venta"    stroke="#6366f1" fill="url(#gVenta)" strokeWidth={2} />
              <Area type="monotone" dataKey="utilidad" name="Utilidad" stroke="#10b981" fill="url(#gUtil)"  strokeWidth={2} />
            </AreaChart>
          </ResponsiveContainer>
        </div>

        {/* Pie estados */}
        <div className="rounded-2xl p-4 border" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
          <h3 className="font-semibold text-sm mb-2" style={{ color: 'var(--text-primary)' }}>Estado Cotizaciones</h3>
          <ResponsiveContainer width="100%" height={160}>
            <PieChart>
              <Pie data={estadosCot} cx="50%" cy="50%" innerRadius={40} outerRadius={70} dataKey="value" paddingAngle={3}>
                {estadosCot.map((e, i) => <Cell key={i} fill={e.color} />)}
              </Pie>
              <Tooltip formatter={(v: any) => [`${v} cotizaciones`]} />
            </PieChart>
          </ResponsiveContainer>
          <div className="space-y-1.5 mt-1">
            {estadosCot.map(e => (
              <div key={e.name} className="flex items-center justify-between text-xs">
                <div className="flex items-center gap-1.5">
                  <span className="w-2.5 h-2.5 rounded-full shrink-0" style={{ backgroundColor: e.color }} />
                  <span style={{ color: 'var(--text-muted)' }}>{e.name}</span>
                </div>
                <span className="font-semibold" style={{ color: 'var(--text-primary)' }}>{e.value}</span>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Charts row 2 */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Por asesor */}
        <div className="rounded-2xl p-4 border" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
          <h3 className="font-semibold text-sm mb-4" style={{ color: 'var(--text-primary)' }}>Rendimiento por Asesor</h3>
          <div className="space-y-3">
            {porAsesor.map(a => {
              const pct = Math.round((a.monto / 14200000) * 100)
              return (
                <div key={a.asesor}>
                  <div className="flex justify-between text-xs mb-1">
                    <span style={{ color: 'var(--text-primary)' }} className="font-medium">{a.asesor}</span>
                    <span style={{ color: 'var(--text-faint)' }}>{a.cotizaciones} cot · {a.ventas} ventas</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <div className="flex-1 h-2 rounded-full" style={{ backgroundColor: 'var(--surface-300)' }}>
                      <div className="h-full rounded-full bg-brand-500 transition-all" style={{ width: `${pct}%` }} />
                    </div>
                    <span className="text-xs font-semibold text-brand-400 w-16 text-right">{fmtClp(a.monto)}</span>
                  </div>
                </div>
              )
            })}
          </div>
        </div>

        {/* Top clientes */}
        <div className="rounded-2xl p-4 border" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
          <h3 className="font-semibold text-sm mb-4" style={{ color: 'var(--text-primary)' }}>Top Clientes por Monto</h3>
          <ResponsiveContainer width="100%" height={180}>
            <BarChart data={topClientes} layout="vertical">
              <XAxis type="number" tickFormatter={fmtK} tick={{ fontSize: 10, fill: 'var(--text-faint)' }} axisLine={false} tickLine={false} />
              <YAxis type="category" dataKey="cliente" width={90} tick={{ fontSize: 11, fill: 'var(--text-muted)' }} axisLine={false} tickLine={false} />
              <Tooltip formatter={(v: any) => [fmtClp(Number(v)), 'Ventas']} />
              <Bar dataKey="monto" name="Ventas" fill="#6366f1" radius={[0, 6, 6, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div className="flex items-center gap-2 p-3 rounded-xl bg-amber-500/10 border border-amber-500/20">
        <BarChart3 className="w-4 h-4 text-amber-400 shrink-0" />
        <p className="text-xs text-amber-400">Módulo en desarrollo — datos de demostración. Los reportes se generarán automáticamente desde la BD real en la siguiente versión.</p>
      </div>
    </div>
  )
}
