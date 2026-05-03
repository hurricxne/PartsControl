import { DollarSign, Plus, ArrowDownCircle, ArrowUpCircle, AlertCircle, TrendingDown } from 'lucide-react'

const DEMO_TC_USD = [
  { tipo: 'SWIFT',   fecha: '01-03-2026', referencia: 'Florida Engine #FE-2026-112', entrada: 50000, consumo: 0,     saldo: 50000, tc: 965, estado: 'disponible' },
  { tipo: 'CONSUMO', fecha: '05-03-2026', referencia: 'EMB-2026-007 / OC-2026-0098', entrada: 0,     consumo: 8420,  saldo: 41580, tc: 965, estado: 'consumido'  },
  { tipo: 'CONSUMO', fecha: '12-03-2026', referencia: 'EMB-2026-012 / OC-2026-0112', entrada: 0,     consumo: 12900, saldo: 28680, tc: 958, estado: 'consumido'  },
  { tipo: 'SWIFT',   fecha: '15-03-2026', referencia: 'Florida Engine #FE-2026-118', entrada: 30000, consumo: 0,     saldo: 58680, tc: 962, estado: 'disponible' },
  { tipo: 'CONSUMO', fecha: '18-03-2026', referencia: 'EMB-2026-015 / OC-2026-0118', entrada: 0,     consumo: 4150,  saldo: 54530, tc: 962, estado: 'consumido'  },
  { tipo: 'CONSUMO', fecha: '20-03-2026', referencia: 'EMB-2026-018 / OC-2026-0124', entrada: 0,     consumo: 9230,  saldo: 45300, tc: 965, estado: 'consumido'  },
]

const DEMO_TC_EUR = [
  { tipo: 'SWIFT',   fecha: '15-01-2026', referencia: 'Baukat GmbH #BAU-2026-001', entrada: 15000, consumo: 0,    saldo: 15000, tc: 990, estado: 'disponible' },
  { tipo: 'CONSUMO', fecha: '05-03-2026', referencia: 'EMB-2026-015 / OC-2026-0115', entrada: 0,   consumo: 4100, saldo: 10900, tc: 990, estado: 'consumido'  },
  { tipo: 'SWIFT',   fecha: '10-03-2026', referencia: 'Baukat GmbH #BAU-2026-002', entrada: 12000, consumo: 0,    saldo: 22900, tc: 1005,estado: 'disponible' },
  { tipo: 'CONSUMO', fecha: '20-03-2026', referencia: 'EMB-2026-018 / OC-2026-0120', entrada: 0,   consumo: 6200, saldo: 16700, tc: 997, estado: 'consumido'  },
]

const saldoActual = 45300
const totalIngresado = 80000
const totalConsumido = 34700
const tcPromedio = 962

const saldoEUR = 16700
const totalIngresadoEUR = 27000
const totalConsumidoEUR = 10300
const tcPromedioEUR = 997

export default function ControlTCPage() {
  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-2xl font-bold" style={{ color: 'var(--text-primary)' }}>Control TC</h1>
          <p className="text-sm mt-1" style={{ color: 'var(--text-muted)' }}>
            Inventario USD/EUR — FIFO · Florida Engine · Baukat GmbH
          </p>
        </div>
        <button className="btn-primary flex items-center justify-center gap-2 w-full sm:w-auto shrink-0">
          <Plus className="w-4 h-4" /> Registrar SWIFT
        </button>
      </div>

      {/* Balance cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        {/* USD */}
        <div className="rounded-2xl p-4 sm:p-5 border bg-gradient-to-br from-brand-900/40 to-brand-800/20"
          style={{ borderColor: 'var(--border)' }}>
          <p className="text-xs font-semibold text-brand-400 mb-1">Florida Engine — USD</p>
          <p className="text-2xl sm:text-3xl font-bold text-brand-400">${saldoActual.toLocaleString('es-CL')}</p>
          <p className="text-xs mt-1" style={{ color: 'var(--text-muted)' }}>≈ ${(saldoActual * tcPromedio).toLocaleString('es-CL')} CLP · TC FIFO ${tcPromedio}</p>
          <div className="flex gap-5 mt-3">
            <div>
              <p className="text-xs text-emerald-500">${totalIngresado.toLocaleString('es-CL')} ingresado</p>
            </div>
            <div>
              <p className="text-xs text-red-400">${totalConsumido.toLocaleString('es-CL')} consumido</p>
            </div>
          </div>
          <div className="mt-3 h-1.5 rounded-full bg-[var(--surface-300)]">
            <div className="h-full rounded-full bg-gradient-to-r from-brand-600 to-brand-400"
              style={{ width: `${(saldoActual / totalIngresado) * 100}%` }} />
          </div>
          <p className="text-xs mt-1" style={{ color: 'var(--text-faint)' }}>{((saldoActual / totalIngresado) * 100).toFixed(0)}% disponible</p>
        </div>
        {/* EUR */}
        <div className="rounded-2xl p-4 sm:p-5 border bg-gradient-to-br from-amber-900/20 to-amber-800/10"
          style={{ borderColor: 'var(--border)' }}>
          <p className="text-xs font-semibold text-amber-400 mb-1">Baukat GmbH — EUR</p>
          <p className="text-2xl sm:text-3xl font-bold text-amber-400">${saldoEUR.toLocaleString('es-CL')}</p>
          <p className="text-xs mt-1" style={{ color: 'var(--text-muted)' }}>≈ ${(saldoEUR * tcPromedioEUR).toLocaleString('es-CL')} CLP · TC FIFO ${tcPromedioEUR}</p>
          <div className="flex gap-5 mt-3">
            <div>
              <p className="text-xs text-emerald-500">${totalIngresadoEUR.toLocaleString('es-CL')} ingresado</p>
            </div>
            <div>
              <p className="text-xs text-red-400">${totalConsumidoEUR.toLocaleString('es-CL')} consumido</p>
            </div>
          </div>
          <div className="mt-3 h-1.5 rounded-full bg-[var(--surface-300)]">
            <div className="h-full rounded-full bg-gradient-to-r from-amber-600 to-amber-400"
              style={{ width: `${(saldoEUR / totalIngresadoEUR) * 100}%` }} />
          </div>
          <p className="text-xs mt-1" style={{ color: 'var(--text-faint)' }}>{((saldoEUR / totalIngresadoEUR) * 100).toFixed(0)}% disponible</p>
        </div>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        {[
          { label: 'TC USD FIFO', value: `$${tcPromedio}`,    sub: 'CLP por USD (Flor.)', color: 'text-brand-400'   },
          { label: 'TC EUR FIFO', value: `$${tcPromedioEUR}`, sub: 'CLP por EUR (Bau.)',  color: 'text-amber-400'   },
          { label: 'SWIFTs',      value: '4',                 sub: 'Recibidos 2026',      color: 'text-emerald-500' },
          { label: 'Consumos',    value: '6',                 sub: 'Embarques financ.',   color: 'text-blue-400'    },
        ].map(s => (
          <div key={s.label} className="rounded-2xl p-3 sm:p-4 border" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
            <p className="text-[10px] uppercase tracking-widest leading-tight" style={{ color: 'var(--text-faint)' }}>{s.label}</p>
            <p className={`text-base sm:text-lg font-bold mt-1 ${s.color}`}>{s.value}</p>
            <p className="text-xs mt-0.5 leading-tight" style={{ color: 'var(--text-muted)' }}>{s.sub}</p>
          </div>
        ))}
      </div>

      {/* Explicación FIFO */}
      <div className="rounded-2xl p-4 border flex gap-3" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
        <AlertCircle className="w-5 h-5 text-brand-400 shrink-0 mt-0.5" />
        <div>
          <p className="text-sm font-semibold" style={{ color: 'var(--text-primary)' }}>Sistema FIFO — First In, First Out</p>
          <p className="text-xs mt-1" style={{ color: 'var(--text-muted)' }}>
            Los USD ingresados primero son consumidos primero. El tipo de cambio queda bloqueado al valor del día del SWIFT,
            garantizando que cada embarque se costee al TC real de compra, sin pérdidas por variación cambiaria.
          </p>
        </div>
      </div>

      {/* Tabla helper */}
      {[
        { label: 'Movimientos Florida Engine — USD', data: DEMO_TC_USD, moneda: 'USD', color: 'text-brand-400' },
        { label: 'Movimientos Baukat GmbH — EUR',    data: DEMO_TC_EUR, moneda: 'EUR', color: 'text-amber-400' },
      ].map(({ label, data, moneda, color }) => (
        <div key={moneda} className="rounded-2xl border overflow-hidden" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
          <div className="px-4 py-3 border-b flex items-center justify-between" style={{ borderColor: 'var(--border)' }}>
            <h2 className={`font-semibold text-sm ${color}`}>{label}</h2>
            <span className="text-xs" style={{ color: 'var(--text-faint)' }}>{data.length} registros</span>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
                  {['Tipo','Fecha','Referencia',`Entrada ${moneda}`,`Consumo ${moneda}`,`Saldo ${moneda}`,'TC','Estado'].map(h => (
                    <th key={h} className="text-left px-4 py-3 text-xs font-semibold uppercase tracking-wider whitespace-nowrap" style={{ color: 'var(--text-faint)' }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y" style={{ borderColor: 'var(--border)' }}>
                {data.map((row, i) => (
                  <tr key={i} className="hover:bg-[var(--surface-200)] transition-colors">
                    <td className="px-4 py-3">
                      <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-bold border ${
                        row.tipo === 'SWIFT'
                          ? 'bg-emerald-500/10 text-emerald-500 border-emerald-500/20'
                          : 'bg-red-500/10 text-red-400 border-red-400/20'
                      }`}>
                        {row.tipo === 'SWIFT' ? <ArrowDownCircle className="w-3 h-3" /> : <ArrowUpCircle className="w-3 h-3" />}
                        {row.tipo}
                      </span>
                    </td>
                    <td className="px-4 py-3 whitespace-nowrap" style={{ color: 'var(--text-muted)' }}>{row.fecha}</td>
                    <td className="px-4 py-3 max-w-[220px] truncate" style={{ color: 'var(--text-primary)' }}>{row.referencia}</td>
                    <td className="px-4 py-3 font-semibold text-emerald-500">
                      {row.entrada > 0 ? `$${row.entrada.toLocaleString('es-CL')}` : '—'}
                    </td>
                    <td className="px-4 py-3 font-semibold text-red-400">
                      {row.consumo > 0 ? `$${row.consumo.toLocaleString('es-CL')}` : '—'}
                    </td>
                    <td className={`px-4 py-3 font-bold ${color}`}>${row.saldo.toLocaleString('es-CL')}</td>
                    <td className="px-4 py-3" style={{ color: 'var(--text-muted)' }}>${row.tc}</td>
                    <td className="px-4 py-3">
                      <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs border ${
                        row.estado === 'disponible'
                          ? 'bg-emerald-500/10 text-emerald-500 border-emerald-500/20'
                          : 'bg-gray-500/10 text-gray-400 border-gray-500/20'
                      }`}>
                        {row.estado}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}

      <div className="flex items-center gap-2 p-3 rounded-xl bg-amber-500/10 border border-amber-500/20">
        <TrendingDown className="w-4 h-4 text-amber-400 shrink-0" />
        <p className="text-xs text-amber-400">Módulo en desarrollo — datos de demostración. El sistema FIFO automático y los cálculos de TC por embarque estarán disponibles en la siguiente versión.</p>
      </div>
    </div>
  )
}
