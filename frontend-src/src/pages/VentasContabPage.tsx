import { TrendingUp, Info, Eye } from 'lucide-react'

const VENTAS = [
  {
    id: 'OC-0124',
    cliente: 'Los Pelambres',
    venta: 2310000,
    costo: null,
    utilidad: null,
    margen: '28% est.',
    margenEst: true,
    factura: null,
    estado: 'Sin Factura',
    estadoCls: 'bg-gray-500/10 text-gray-400 border-gray-500/20',
  },
  {
    id: 'OC-0118',
    cliente: 'Codelco El Teniente',
    venta: 7200000,
    costo: 4810000,
    utilidad: 2390000,
    margen: '33%',
    margenEst: false,
    factura: 'F-3835',
    estado: 'Parcial',
    estadoCls: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
  },
  {
    id: 'OC-0112',
    cliente: 'Anglo American',
    venta: 2730000,
    costo: 2100000,
    utilidad: 630000,
    margen: '23%',
    margenEst: false,
    factura: 'F-3820',
    estado: 'Cobrada',
    estadoCls: 'bg-emerald-500/10 text-emerald-500 border-emerald-500/20',
  },
  {
    id: 'OC-0105',
    cliente: 'Antofagasta Minerals',
    venta: 5130000,
    costo: 3800000,
    utilidad: 1330000,
    margen: '26%',
    margenEst: false,
    factura: 'F-3812',
    estado: 'Vencida',
    estadoCls: 'bg-red-500/10 text-red-400 border-red-400/20',
  },
  {
    id: 'OC-0098',
    cliente: 'H-E Parts Intl',
    venta: 10660000,
    costo: 8200000,
    utilidad: 2460000,
    margen: '23%',
    margenEst: false,
    factura: 'F-3798',
    estado: 'Cobrada',
    estadoCls: 'bg-emerald-500/10 text-emerald-500 border-emerald-500/20',
  },
]

const clp = (n: number) => `$${n.toLocaleString('es-CL')}`

export default function VentasContabPage() {
  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="min-w-0">
        <h1 className="text-2xl font-bold" style={{ color: 'var(--text-primary)' }}>
          Ventas — Contabilidad
        </h1>
        <p className="text-sm mt-1" style={{ color: 'var(--text-muted)' }}>
          Registro automático desde cierre de venta · Costo real · Utilidad · Cobranza
        </p>
      </div>

      {/* KPIs */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        {[
          { label: 'Ventas registradas', value: '47',    sub: 'Total acumulado',    color: 'text-brand-400'   },
          { label: 'Ingresos totales',   value: '$32,9M', sub: 'CLP neto',           color: 'text-emerald-500' },
          { label: 'Utilidad bruta real',value: '$8,1M', sub: 'Con costo completado', color: 'text-amber-400' },
          { label: 'Por cobrar',         value: '$14,9M', sub: 'Facturas pendientes', color: 'text-red-400'    },
        ].map(s => (
          <div key={s.label} className="rounded-2xl p-3 sm:p-4 border"
            style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
            <p className="text-[10px] uppercase tracking-widest leading-tight" style={{ color: 'var(--text-faint)' }}>
              {s.label}
            </p>
            <p className={`text-lg sm:text-xl font-bold mt-1 ${s.color}`}>{s.value}</p>
            <p className="text-xs mt-0.5 leading-tight" style={{ color: 'var(--text-muted)' }}>{s.sub}</p>
          </div>
        ))}
      </div>

      {/* Info box */}
      <div className="flex items-start gap-2.5 p-4 rounded-xl border bg-brand-500/5 border-brand-500/20">
        <Info className="w-4 h-4 text-brand-400 shrink-0 mt-0.5" />
        <p className="text-xs leading-relaxed" style={{ color: 'var(--text-muted)' }}>
          Este módulo se actualiza automáticamente cuando Comercial cierra una venta (Trigger 3).
          El costo real se completa desde el pricing de embarques.
        </p>
      </div>

      {/* Table */}
      <div className="rounded-2xl border overflow-hidden"
        style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr style={{ backgroundColor: 'var(--surface-200)', borderBottom: '1px solid var(--border)' }}>
                {['ID Venta', 'Cliente', 'Venta Neta CLP', 'Costo Real CLP', 'Utilidad Real', '% Margen', 'N° Factura', 'Estado Pago', 'Acción'].map(h => (
                  <th key={h} className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider"
                    style={{ color: 'var(--text-faint)' }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {VENTAS.map((v, i) => (
                <tr key={v.id}
                  style={{ borderBottom: i < VENTAS.length - 1 ? '1px solid var(--border)' : undefined }}
                  className="hover:bg-white/[0.02] transition-colors">
                  <td className="px-4 py-3 font-mono text-xs font-bold text-brand-400">{v.id}</td>
                  <td className="px-4 py-3 font-medium" style={{ color: 'var(--text-primary)' }}>{v.cliente}</td>
                  <td className="px-4 py-3 font-mono text-xs font-semibold" style={{ color: 'var(--text-primary)' }}>
                    {clp(v.venta)}
                  </td>
                  <td className="px-4 py-3 font-mono text-xs">
                    {v.costo !== null
                      ? <span style={{ color: 'var(--text-primary)' }}>{clp(v.costo)}</span>
                      : <span className="italic" style={{ color: 'var(--text-faint)' }}>pendiente</span>}
                  </td>
                  <td className="px-4 py-3 font-mono text-xs">
                    {v.utilidad !== null
                      ? <span className="text-emerald-400 font-semibold">{clp(v.utilidad)}</span>
                      : <span className="italic" style={{ color: 'var(--text-faint)' }}>—</span>}
                  </td>
                  <td className="px-4 py-3 text-xs font-semibold">
                    <span className={v.margenEst ? 'text-amber-400' : 'text-brand-400'}>{v.margen}</span>
                  </td>
                  <td className="px-4 py-3 font-mono text-xs">
                    {v.factura
                      ? <span style={{ color: 'var(--text-primary)' }}>{v.factura}</span>
                      : <span style={{ color: 'var(--text-faint)' }}>—</span>}
                  </td>
                  <td className="px-4 py-3">
                    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium border ${v.estadoCls}`}>
                      {v.estado}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    <button className="btn-secondary text-xs px-3 py-1.5 flex items-center gap-1.5">
                      <Eye className="w-3 h-3" /> Ver
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="flex items-center gap-2 p-3 rounded-xl bg-amber-500/10 border border-amber-500/20">
        <TrendingUp className="w-4 h-4 text-amber-400 shrink-0" />
        <p className="text-xs text-amber-400">
          Módulo en desarrollo — datos de demostración. La cobranza y emisión de facturas estarán disponibles en la siguiente versión.
        </p>
      </div>
    </div>
  )
}
