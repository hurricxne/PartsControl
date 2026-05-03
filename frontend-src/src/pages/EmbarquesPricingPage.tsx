import { useState } from 'react'
import { Plane, Info, CheckCircle2, Calculator, ChevronDown, ChevronUp } from 'lucide-react'

const ITEMS_EMB018 = [
  { parte: '7T-1997',  desc: 'SEAL-O-RING',         peso: 0.3,  fobUsd: 124,  shippingProrrat: 24800,  gastosProrrat: 16800, cifTotal: 157492,  costoUnit: 157492  },
  { parte: '4W-0259',  desc: 'FILTER AS-FUEL',       peso: 1.2,  fobUsd: 890,  shippingProrrat: 99200,  gastosProrrat: 67200, cifTotal: 1019760, costoUnit: 509880  },
  { parte: '1R-0749',  desc: 'ELEMENT AS-FILTER',    peso: 2.1,  fobUsd: 1420, shippingProrrat: 173600, gastosProrrat: 117600, cifTotal: 1615760, costoUnit: 269293 },
]

const ITEMS_EMB012 = [
  { parte: '9X-8522',  desc: 'BOLT',                 peso: 0.1, fobUsd: 42,   cifTotal: 58400,   costoUnit: 4867  },
  { parte: '6V-4378',  desc: 'GASKET',               peso: 0.4, fobUsd: 380,  cifTotal: 490200,  costoUnit: 490200 },
  { parte: '8T-4121',  desc: 'RING AS',              peso: 0.9, fobUsd: 720,  cifTotal: 911800,  costoUnit: 455900 },
  { parte: '3E-4175',  desc: 'SHIM',                 peso: 0.2, fobUsd: 210,  cifTotal: 279800,  costoUnit: 139900 },
  { parte: '5P-8075',  desc: 'LOCK-VALVE',           peso: 1.8, fobUsd: 980,  cifTotal: 1183000, costoUnit: 591500 },
]

const clp = (n: number) => `$${Math.round(n).toLocaleString('es-CL')}`

function PricingFormEMB018() {
  const [tcMode, setTcMode] = useState<'fifo' | 'manual'>('fifo')
  const [calculated, setCalculated] = useState(false)

  return (
    <div className="mt-4 space-y-4 pt-4 border-t" style={{ borderColor: 'var(--border)' }}>
      {/* TC */}
      <div>
        <p className="text-xs font-semibold uppercase tracking-wider mb-2" style={{ color: 'var(--text-faint)' }}>
          Tipo de cambio a usar
        </p>
        <div className="flex gap-3">
          <button
            onClick={() => setTcMode('fifo')}
            className={`flex items-center gap-2 px-3 py-2 rounded-xl border text-sm transition-all ${
              tcMode === 'fifo'
                ? 'bg-brand-500/10 border-brand-500/30 text-brand-400'
                : 'border-transparent hover:border-gray-500/30'
            }`}
            style={tcMode !== 'fifo' ? { backgroundColor: 'var(--surface-300)', borderColor: 'var(--border)' } : undefined}>
            <div className={`w-3 h-3 rounded-full border-2 flex items-center justify-center ${tcMode === 'fifo' ? 'border-brand-400' : ''}`}
              style={tcMode !== 'fifo' ? { borderColor: 'var(--text-faint)' } : undefined}>
              {tcMode === 'fifo' && <div className="w-1.5 h-1.5 rounded-full bg-brand-400" />}
            </div>
            FIFO automático <span className="font-mono font-bold text-brand-400">($962)</span>
          </button>
          <button
            onClick={() => setTcMode('manual')}
            className={`flex items-center gap-2 px-3 py-2 rounded-xl border text-sm transition-all ${
              tcMode === 'manual'
                ? 'bg-brand-500/10 border-brand-500/30 text-brand-400'
                : 'border-transparent hover:border-gray-500/30'
            }`}
            style={tcMode !== 'manual' ? { backgroundColor: 'var(--surface-300)', borderColor: 'var(--border)' } : undefined}>
            <div className={`w-3 h-3 rounded-full border-2 flex items-center justify-center ${tcMode === 'manual' ? 'border-brand-400' : ''}`}
              style={tcMode !== 'manual' ? { borderColor: 'var(--text-faint)' } : undefined}>
              {tcMode === 'manual' && <div className="w-1.5 h-1.5 rounded-full bg-brand-400" />}
            </div>
            Manual
          </button>
          {tcMode === 'manual' && (
            <input className="input w-28 text-sm font-mono" placeholder="TC manual" defaultValue="962" />
          )}
        </div>
      </div>

      {/* Costos */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <div>
          <label className="block text-xs font-semibold uppercase tracking-wider mb-1.5"
            style={{ color: 'var(--text-faint)' }}>Shipping total CLP</label>
          <input className="input w-full font-mono" defaultValue="450000" />
        </div>
        <div className="rounded-xl border p-3 space-y-1" style={{ backgroundColor: 'var(--surface-200)', borderColor: 'var(--border)' }}>
          <p className="text-xs font-semibold uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>
            Gastos locales (desde Config)
          </p>
          {[
            { label: 'Agencia Aduana', val: '$160.000' },
            { label: 'Desconsolidación', val: '$90.000' },
            { label: 'Bodegaje', val: '$90.000' },
          ].map(g => (
            <div key={g.label} className="flex justify-between text-xs">
              <span style={{ color: 'var(--text-muted)' }}>{g.label}</span>
              <span className="font-mono font-semibold" style={{ color: 'var(--text-primary)' }}>{g.val}</span>
            </div>
          ))}
          <div className="flex justify-between text-xs pt-1 border-t font-bold" style={{ borderColor: 'var(--border)' }}>
            <span style={{ color: 'var(--text-primary)' }}>Total gastos</span>
            <span className="font-mono text-brand-400">$340.000</span>
          </div>
        </div>
      </div>

      {/* Items table */}
      <div className="overflow-x-auto rounded-xl border" style={{ borderColor: 'var(--border)' }}>
        <table className="w-full text-xs">
          <thead>
            <tr style={{ backgroundColor: 'var(--surface-200)', borderBottom: '1px solid var(--border)' }}>
              {['N° Parte', 'Descripción', 'Peso kg', 'FOB USD', 'Shipping prorrat.', 'Gastos prorrat.', 'CIF Total CLP', 'Costo Unit. CLP'].map(h => (
                <th key={h} className="px-3 py-2 text-left text-xs font-semibold uppercase tracking-wider whitespace-nowrap"
                  style={{ color: 'var(--text-faint)' }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {ITEMS_EMB018.map((item, i) => (
              <tr key={item.parte}
                style={{ borderBottom: i < ITEMS_EMB018.length - 1 ? '1px solid var(--border)' : undefined }}>
                <td className="px-3 py-2.5 font-mono font-bold text-brand-400">{item.parte}</td>
                <td className="px-3 py-2.5" style={{ color: 'var(--text-primary)' }}>{item.desc}</td>
                <td className="px-3 py-2.5 font-mono text-center" style={{ color: 'var(--text-muted)' }}>{item.peso}</td>
                <td className="px-3 py-2.5 font-mono" style={{ color: 'var(--text-primary)' }}>${item.fobUsd}</td>
                {calculated ? (
                  <>
                    <td className="px-3 py-2.5 font-mono text-amber-400">{clp(item.shippingProrrat)}</td>
                    <td className="px-3 py-2.5 font-mono text-amber-400">{clp(item.gastosProrrat)}</td>
                    <td className="px-3 py-2.5 font-mono text-brand-400 font-semibold">{clp(item.cifTotal)}</td>
                    <td className="px-3 py-2.5 font-mono text-emerald-400 font-semibold">{clp(item.costoUnit)}</td>
                  </>
                ) : (
                  <>
                    {[...Array(4)].map((_, k) => (
                      <td key={k} className="px-3 py-2.5 italic" style={{ color: 'var(--text-faint)' }}>—</td>
                    ))}
                  </>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <button
        onClick={() => setCalculated(true)}
        className="btn-primary flex items-center gap-2">
        <Calculator className="w-4 h-4" /> Calcular Pricing
      </button>
    </div>
  )
}

function SummaryEMB012() {
  return (
    <div className="mt-4 pt-4 border-t space-y-4" style={{ borderColor: 'var(--border)' }}>
      {/* Summary row */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        {[
          { label: 'TC usado',    value: '$965'       },
          { label: 'Shipping',   value: '$280.000'    },
          { label: 'Gastos loc.', value: '$340.000'   },
          { label: 'CIF total',  value: '$4.850.000'  },
        ].map(s => (
          <div key={s.label} className="rounded-xl p-2.5 border text-center"
            style={{ backgroundColor: 'var(--surface-200)', borderColor: 'var(--border)' }}>
            <p className="text-[10px] uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>{s.label}</p>
            <p className="font-mono font-bold text-sm text-brand-400 mt-0.5">{s.value}</p>
          </div>
        ))}
      </div>

      <div className="overflow-x-auto rounded-xl border" style={{ borderColor: 'var(--border)' }}>
        <table className="w-full text-xs">
          <thead>
            <tr style={{ backgroundColor: 'var(--surface-200)', borderBottom: '1px solid var(--border)' }}>
              {['N° Parte', 'Descripción', 'Peso kg', 'FOB USD', 'CIF Total CLP', 'Costo Unit. CLP'].map(h => (
                <th key={h} className="px-3 py-2 text-left text-xs font-semibold uppercase tracking-wider whitespace-nowrap"
                  style={{ color: 'var(--text-faint)' }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {ITEMS_EMB012.map((item, i) => (
              <tr key={item.parte}
                style={{ borderBottom: i < ITEMS_EMB012.length - 1 ? '1px solid var(--border)' : undefined }}>
                <td className="px-3 py-2.5 font-mono font-bold text-brand-400">{item.parte}</td>
                <td className="px-3 py-2.5" style={{ color: 'var(--text-primary)' }}>{item.desc}</td>
                <td className="px-3 py-2.5 font-mono text-center" style={{ color: 'var(--text-muted)' }}>{item.peso}</td>
                <td className="px-3 py-2.5 font-mono" style={{ color: 'var(--text-primary)' }}>${item.fobUsd}</td>
                <td className="px-3 py-2.5 font-mono text-brand-400 font-semibold">{clp(item.cifTotal)}</td>
                <td className="px-3 py-2.5 font-mono text-emerald-400 font-semibold">{clp(item.costoUnit)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="flex items-center gap-2 text-xs text-emerald-400">
        <CheckCircle2 className="w-4 h-4" />
        <span className="font-semibold">Costo real propagado a Ventas</span>
      </div>
    </div>
  )
}

const EMBARQUES = [
  {
    id: 'EMB-2026-018',
    carrier: 'LATAM Cargo',
    estado: 'PENDIENTE PRICING',
    estadoCls: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
    completado: false,
  },
  {
    id: 'EMB-2026-012',
    carrier: 'Fast Mark',
    estado: 'PRICING COMPLETADO',
    estadoCls: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20',
    completado: true,
  },
]

export default function EmbarquesPricingPage() {
  const [expanded, setExpanded] = useState<string | null>('EMB-2026-018')

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="min-w-0">
        <h1 className="text-2xl font-bold" style={{ color: 'var(--text-primary)' }}>
          Embarques — Pricing
        </h1>
        <p className="text-sm mt-1" style={{ color: 'var(--text-muted)' }}>
          Contabilidad · Costo CIF real por ítem · Prorrateo por forwarder
        </p>
      </div>

      {/* Info box */}
      <div className="flex items-start gap-2.5 p-4 rounded-xl border bg-brand-500/5 border-brand-500/20">
        <Info className="w-4 h-4 text-brand-400 shrink-0 mt-0.5" />
        <p className="text-xs leading-relaxed" style={{ color: 'var(--text-muted)' }}>
          Este módulo se crea automáticamente cuando Logística cierra un embarque. Contabilidad
          completa el pricing con TC FIFO, shipping y gastos locales.
        </p>
      </div>

      {/* Embarque cards */}
      <div className="space-y-3">
        {EMBARQUES.map(emb => {
          const open = expanded === emb.id
          return (
            <div key={emb.id} className="rounded-2xl border overflow-hidden"
              style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
              {/* Card header */}
              <button
                className="w-full text-left px-5 py-4 flex items-center justify-between gap-3 hover:bg-white/[0.02] transition-colors"
                onClick={() => setExpanded(open ? null : emb.id)}>
                <div className="flex items-center gap-3">
                  <div className="w-9 h-9 rounded-xl bg-brand-500/10 flex items-center justify-center shrink-0">
                    <Plane className="w-4 h-4 text-brand-400" />
                  </div>
                  <div>
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-mono font-bold text-sm text-brand-400">{emb.id}</span>
                      <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium border ${emb.estadoCls}`}>
                        {emb.completado && <CheckCircle2 className="w-3 h-3 mr-1" />}
                        {emb.estado}
                      </span>
                    </div>
                    <p className="text-xs mt-0.5" style={{ color: 'var(--text-muted)' }}>
                      {emb.carrier}
                    </p>
                  </div>
                </div>
                {open
                  ? <ChevronUp className="w-4 h-4 shrink-0" style={{ color: 'var(--text-faint)' }} />
                  : <ChevronDown className="w-4 h-4 shrink-0" style={{ color: 'var(--text-faint)' }} />}
              </button>

              {/* Expanded content */}
              {open && (
                <div className="px-5 pb-5">
                  {emb.completado ? <SummaryEMB012 /> : <PricingFormEMB018 />}
                </div>
              )}
            </div>
          )
        })}
      </div>

      <div className="flex items-center gap-2 p-3 rounded-xl bg-amber-500/10 border border-amber-500/20">
        <Plane className="w-4 h-4 text-amber-400 shrink-0" />
        <p className="text-xs text-amber-400">
          Módulo en desarrollo — datos de demostración. El TC FIFO automático y la propagación a Ventas estarán activos en la siguiente versión.
        </p>
      </div>
    </div>
  )
}
