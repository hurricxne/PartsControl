import { Receipt, Plus, Search, AlertCircle, CheckCircle2, Clock, TrendingUp, DollarSign } from 'lucide-react'

const DEMO_FACTURAS = [
  { folio: 'F-3841', fecha: '18-03-2026', cliente: 'Minera Los Pelambres',     venta: 'OC-2026-0124', total: '$2.310.000', cobrado: 0,           promesa: '17-04-2026', estado: 'sin_cobrar',  diasVenc: 30 },
  { folio: 'F-3835', fecha: '10-03-2026', cliente: 'Codelco Div. El Teniente', venta: 'OC-2026-0118', total: '$7.200.000', cobrado: 3600000,      promesa: '09-04-2026', estado: 'parcial',     diasVenc: 30 },
  { folio: 'F-3820', fecha: '28-02-2026', cliente: 'Anglo American Chile',     venta: 'OC-2026-0112', total: '$2.730.000', cobrado: 2730000,      promesa: '30-03-2026', estado: 'cobrada',     diasVenc: 0  },
  { folio: 'F-3812', fecha: '20-02-2026', cliente: 'Antofagasta Minerals',     venta: 'OC-2026-0105', total: '$5.130.000', cobrado: 0,            promesa: '21-03-2026', estado: 'vencida',     diasVenc: -2 },
  { folio: 'F-3798', fecha: '12-02-2026', cliente: 'H-E Parts International', venta: 'OC-2026-0098', total: '$10.660.000',cobrado: 10660000,     promesa: '14-03-2026', estado: 'factoring',   diasVenc: 0  },
]

const estadoBadge = (e: string) => {
  const map: Record<string, { cls: string; label: string }> = {
    sin_cobrar: { cls: 'bg-blue-500/10 text-blue-400 border-blue-400/20',          label: 'Sin Cobrar'  },
    parcial:    { cls: 'bg-amber-500/10 text-amber-400 border-amber-400/20',       label: 'Pago Parcial'},
    cobrada:    { cls: 'bg-emerald-500/10 text-emerald-500 border-emerald-500/20', label: 'Cobrada'     },
    vencida:    { cls: 'bg-red-500/10 text-red-400 border-red-400/20',             label: 'Vencida'     },
    factoring:  { cls: 'bg-purple-500/10 text-purple-400 border-purple-400/20',    label: 'Factoring'   },
    incobrable: { cls: 'bg-gray-500/10 text-gray-400 border-gray-500/20',          label: 'Incobrable'  },
  }
  return map[e] ?? { cls: 'bg-gray-500/10 text-gray-400', label: e }
}

const fmtClp = (v: number) => `$${v.toLocaleString('es-CL')}`

export default function FacturasPage() {
  const totalXCobrar = DEMO_FACTURAS.filter(f => f.estado !== 'cobrada' && f.estado !== 'factoring')
    .reduce((acc, f) => acc + parseInt(f.total.replace(/[$.]/g, '')), 0)

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-2xl font-bold" style={{ color: 'var(--text-primary)' }}>Facturas y Cobranzas</h1>
          <p className="text-sm mt-1" style={{ color: 'var(--text-muted)' }}>
            Módulo 14 — Cuentas por cobrar · Antigüedad · Factoring · Alertas de vencimiento
          </p>
        </div>
        <button className="btn-primary flex items-center justify-center gap-2 w-full sm:w-auto shrink-0">
          <Plus className="w-4 h-4" /> Registrar Factura
        </button>
      </div>

      {/* Alerta vencidas */}
      <div className="flex items-center gap-3 p-3 rounded-xl bg-red-500/10 border border-red-500/20">
        <AlertCircle className="w-4 h-4 text-red-400 shrink-0" />
        <p className="text-xs text-red-400 font-medium">
          1 factura vencida: <strong>F-3812 / Antofagasta Minerals — $5.130.000</strong> — 2 días sin pago.
        </p>
      </div>

      {/* KPIs */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        {[
          { icon: Receipt,    label: 'Facturas emitidas', value: '5',       sub: 'Último mes',         color: 'text-brand-400'   },
          { icon: DollarSign, label: 'x Cobrar',          value: '$10.2M',  sub: 'CLP pendiente',      color: 'text-amber-400'   },
          { icon: CheckCircle2,label:'Cobrado',           value: '$13.4M',  sub: 'Ingresado 2026',     color: 'text-emerald-500' },
          { icon: TrendingUp, label: 'En Factoring',      value: '$10.7M',  sub: 'Cedido a empresa',   color: 'text-purple-400'  },
        ].map(s => (
          <div key={s.label} className="rounded-2xl p-3 sm:p-4 border" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
            <div className="w-8 h-8 rounded-xl flex items-center justify-center mb-2" style={{ backgroundColor: 'var(--surface-300)' }}>
              <s.icon className={`w-4 h-4 ${s.color}`} />
            </div>
            <p className="text-[10px] uppercase tracking-widest leading-tight" style={{ color: 'var(--text-faint)' }}>{s.label}</p>
            <p className={`text-lg sm:text-xl font-bold mt-0.5 ${s.color}`}>{s.value}</p>
            <p className="text-xs mt-0.5 leading-tight" style={{ color: 'var(--text-muted)' }}>{s.sub}</p>
          </div>
        ))}
      </div>

      {/* Search */}
      <div className="relative max-w-sm">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4" style={{ color: 'var(--text-faint)' }} />
        <input className="input pl-9 w-full" placeholder="Buscar por folio, cliente o venta…" />
      </div>

      {/* Table */}
      <div className="rounded-2xl border overflow-hidden" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
                {['Folio','Fecha','Cliente','Venta/OC','Total CLP','Cobrado','Promesa Pago','Estado'].map(h => (
                  <th key={h} className="text-left px-4 py-3 text-xs font-semibold uppercase tracking-wider whitespace-nowrap" style={{ color: 'var(--text-faint)' }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y" style={{ borderColor: 'var(--border)' }}>
              {DEMO_FACTURAS.map(row => {
                const { cls, label } = estadoBadge(row.estado)
                const cobradoNum = row.cobrado
                const totalNum = parseInt(row.total.replace(/[$.]/g, ''))
                const pct = totalNum > 0 ? Math.round((cobradoNum / totalNum) * 100) : 0
                return (
                  <tr key={row.folio} className="hover:bg-[var(--surface-200)] transition-colors">
                    <td className="px-4 py-3 font-mono font-semibold text-brand-400 whitespace-nowrap">{row.folio}</td>
                    <td className="px-4 py-3 whitespace-nowrap" style={{ color: 'var(--text-muted)' }}>{row.fecha}</td>
                    <td className="px-4 py-3 font-medium max-w-[180px] truncate" style={{ color: 'var(--text-primary)' }}>{row.cliente}</td>
                    <td className="px-4 py-3 font-mono text-xs text-brand-400">{row.venta}</td>
                    <td className="px-4 py-3 font-semibold whitespace-nowrap" style={{ color: 'var(--text-primary)' }}>{row.total}</td>
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-2">
                        <div className="w-16 h-1.5 rounded-full" style={{ backgroundColor: 'var(--surface-300)' }}>
                          <div className="h-full rounded-full bg-emerald-500" style={{ width: `${pct}%` }} />
                        </div>
                        <span className="text-xs text-emerald-500 font-medium">{pct}%</span>
                      </div>
                    </td>
                    <td className="px-4 py-3 whitespace-nowrap">
                      <span className={row.diasVenc < 0 ? 'text-red-400 font-medium' : ''} style={row.diasVenc >= 0 ? { color: 'var(--text-muted)' } : {}}>
                        {row.promesa}
                        {row.diasVenc < 0 && <span className="ml-1 text-xs">({Math.abs(row.diasVenc)}d)</span>}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium border ${cls}`}>
                        {row.estado === 'cobrada' ? <CheckCircle2 className="w-3 h-3" />
                          : row.estado === 'vencida' ? <AlertCircle className="w-3 h-3" />
                          : <Clock className="w-3 h-3" />
                        }
                        {label}
                      </span>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </div>

      {/* Antigüedad resumen */}
      <div className="rounded-2xl border p-4" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
        <h3 className="font-semibold text-sm mb-3" style={{ color: 'var(--text-primary)' }}>Antigüedad de Cartera</h3>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          {[
            { rango: '0–30 días',  monto: '$9.510.000',  color: 'text-emerald-500' },
            { rango: '31–60 días', monto: '$5.130.000',  color: 'text-amber-400'   },
            { rango: '61–90 días', monto: '$0',          color: 'text-gray-400'    },
            { rango: '+90 días',   monto: '$0',          color: 'text-red-400'     },
          ].map(r => (
            <div key={r.rango} className="text-center p-3 rounded-xl" style={{ backgroundColor: 'var(--surface-200)' }}>
              <p className="text-xs" style={{ color: 'var(--text-faint)' }}>{r.rango}</p>
              <p className={`text-base font-bold mt-1 ${r.color}`}>{r.monto}</p>
            </div>
          ))}
        </div>
      </div>

      <div className="flex items-center gap-2 p-3 rounded-xl bg-amber-500/10 border border-amber-500/20">
        <Receipt className="w-4 h-4 text-amber-400 shrink-0" />
        <p className="text-xs text-amber-400">Módulo en desarrollo — datos de demostración. El flujo de caja estimado y las alertas de cobranza automatizadas estarán en la siguiente versión.</p>
      </div>
    </div>
  )
}
