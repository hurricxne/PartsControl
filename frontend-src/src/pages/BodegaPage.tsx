import { Package, CheckCircle2, AlertCircle, Clock, Search, Truck, Ship, Activity } from 'lucide-react'

const DEMO_RECEPCION = [
  {
    embarque: 'EMB-2026-018', carrier: 'LATAM Cargo', fechaEsperada: '25-03-2026', fechaReal: null,
    estado: 'en_aduana',
    items: [
      { parte: '4V0907',   desc: 'SEAL-O-RING',         cliente: 'Minera Los Pelambres',           cant: 2,  recibido: 0, estado: 'pendiente' },
      { parte: '1649578',  desc: 'CAP AS',               cliente: 'Minera Los Pelambres',           cant: 5,  recibido: 0, estado: 'pendiente' },
      { parte: '8E5600',   desc: 'BEARING',              cliente: 'Codelco Div. El Teniente',       cant: 1,  recibido: 0, estado: 'pendiente' },
    ]
  },
  {
    embarque: 'EMB-2026-012', carrier: 'Fast Mark', fechaEsperada: '10-03-2026', fechaReal: '11-03-2026',
    estado: 'en_bodega',
    items: [
      { parte: '7T1997',   desc: 'SPACER',               cliente: 'Anglo American Chile',           cant: 4,  recibido: 4, estado: 'recibido' },
      { parte: '3P0366',   desc: 'GASKET',               cliente: 'Anglo American Chile',           cant: 10, recibido: 10,estado: 'recibido' },
      { parte: '2346225',  desc: 'FILTER AS-FUEL',       cliente: 'Antofagasta Minerals',           cant: 6,  recibido: 5, estado: 'faltante' },
    ]
  },
  {
    embarque: 'EMB-2026-009', carrier: 'LATAM Cargo', fechaEsperada: '28-02-2026', fechaReal: '01-03-2026',
    estado: 'despachado',
    items: [
      { parte: '4590686',  desc: 'SEAL O-RING',          cliente: 'H-E Parts International',       cant: 8,  recibido: 8, estado: 'despachado' },
      { parte: '1R0716',   desc: 'FILTER AS-OIL',        cliente: 'H-E Parts International',       cant: 12, recibido: 12,estado: 'despachado' },
    ]
  },
]

const estadoEmbarque: Record<string, { cls: string; label: string }> = {
  en_transito: { cls: 'bg-blue-500/10 text-blue-400 border-blue-400/20',          label: 'En Tránsito'  },
  en_aduana:   { cls: 'bg-amber-500/10 text-amber-400 border-amber-400/20',       label: 'En Aduana'    },
  en_bodega:   { cls: 'bg-brand-500/10 text-brand-400 border-brand-400/20',       label: 'En Bodega'    },
  despachado:  { cls: 'bg-emerald-500/10 text-emerald-500 border-emerald-500/20', label: 'Despachado'   },
}

const estadoItem: Record<string, { cls: string; icon: JSX.Element }> = {
  pendiente:   { cls: 'text-amber-400',   icon: <Clock className="w-3.5 h-3.5" />           },
  recibido:    { cls: 'text-emerald-500', icon: <CheckCircle2 className="w-3.5 h-3.5" />    },
  faltante:    { cls: 'text-red-400',     icon: <AlertCircle className="w-3.5 h-3.5" />     },
  danado:      { cls: 'text-red-400',     icon: <AlertCircle className="w-3.5 h-3.5" />     },
  despachado:  { cls: 'text-purple-400',  icon: <Truck className="w-3.5 h-3.5" />           },
}

const DASHBOARD_LOG = [
  { id: 'EMB-2026-018', carrier: 'LATAM Cargo', origen: 'USA', estado: 'En Aduana',    eta: '25-03-2026', diasTransito: 3,  semaforo: 'amber' },
  { id: 'EMB-2026-015', carrier: 'Aeropost',    origen: 'USA', estado: 'En Bodega',    eta: '17-03-2026', diasTransito: 11, semaforo: 'brand' },
  { id: 'EMB-2026-012', carrier: 'Fast Mark',   origen: 'ALE', estado: 'Entregado',    eta: '10-03-2026', diasTransito: 6,  semaforo: 'green' },
]

export default function BodegaPage() {
  const totalEmbarques = DEMO_RECEPCION.length
  const enBodega = DEMO_RECEPCION.filter(e => e.estado === 'en_bodega').length
  const faltantes = DEMO_RECEPCION.flatMap(e => e.items).filter(i => i.estado === 'faltante').length

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-2xl font-bold" style={{ color: 'var(--text-primary)' }}>Dashboard Logística + Bodega</h1>
          <p className="text-sm mt-1" style={{ color: 'var(--text-muted)' }}>
            Módulo 11 — Seguimiento de embarques en tránsito · Recepción · Despacho al cliente
          </p>
        </div>
        <button className="btn-secondary flex items-center justify-center gap-2 w-full sm:w-auto shrink-0">
          <Package className="w-4 h-4" /> Recibir Embarque
        </button>
      </div>

      {/* Dashboard logística */}
      <div className="rounded-2xl border overflow-hidden" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
        <div className="px-4 py-3 border-b flex items-center gap-2" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
          <Activity className="w-4 h-4 text-brand-400" />
          <h2 className="font-semibold text-sm" style={{ color: 'var(--text-primary)' }}>Estado de embarques activos</h2>
        </div>
        <div className="divide-y" style={{ borderColor: 'var(--border)' }}>
          {DASHBOARD_LOG.map(e => (
            <div key={e.id} className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 px-4 py-3">
              <div className="flex items-center gap-3">
                <Ship className="w-4 h-4 text-brand-400 shrink-0" />
                <div>
                  <p className="text-xs font-mono font-bold text-brand-400">{e.id}</p>
                  <p className="text-xs mt-0.5" style={{ color: 'var(--text-muted)' }}>{e.carrier} · Origen: {e.origen} · ETA: {e.eta}</p>
                </div>
              </div>
              <div className="flex items-center gap-3 shrink-0">
                <span className="text-xs" style={{ color: 'var(--text-faint)' }}>{e.diasTransito}d en tránsito</span>
                <span className={`text-xs font-semibold px-2.5 py-1 rounded-full ${
                  e.semaforo === 'green' ? 'bg-emerald-500/10 text-emerald-500'
                  : e.semaforo === 'amber' ? 'bg-amber-500/10 text-amber-400'
                  : 'bg-brand-500/10 text-brand-400'
                }`}>{e.estado}</span>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        {[
          { label: 'Embarques',   value: totalEmbarques.toString(), sub: 'Con ítems en flujo',     color: 'text-brand-400'   },
          { label: 'En bodega',   value: enBodega.toString(),       sub: 'Pend. despacho cliente', color: 'text-amber-400'   },
          { label: 'Faltantes',   value: faltantes.toString(),      sub: 'Alerta a Abastecimiento',color: 'text-red-400'     },
          { label: 'Despachados', value: '1',                       sub: 'Entregados este mes',    color: 'text-emerald-500' },
        ].map(s => (
          <div key={s.label} className="rounded-2xl p-3 sm:p-4 border" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
            <p className="text-[10px] uppercase tracking-widest leading-tight" style={{ color: 'var(--text-faint)' }}>{s.label}</p>
            <p className={`text-lg sm:text-xl font-bold mt-1 ${s.color}`}>{s.value}</p>
            <p className="text-xs mt-0.5 leading-tight" style={{ color: 'var(--text-muted)' }}>{s.sub}</p>
          </div>
        ))}
      </div>

      {/* Search */}
      <div className="relative max-w-sm">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4" style={{ color: 'var(--text-faint)' }} />
        <input className="input pl-9 w-full" placeholder="Buscar por embarque o N° parte…" />
      </div>

      {/* Embarques */}
      <div className="space-y-4">
        {DEMO_RECEPCION.map(emb => {
          const badge = estadoEmbarque[emb.estado] ?? { cls: 'bg-gray-500/10 text-gray-400', label: emb.estado }
          return (
            <div key={emb.embarque} className="rounded-2xl border overflow-hidden"
              style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
              {/* Cabecera embarque */}
              <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 px-4 py-3 border-b"
                style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
                <div className="flex items-center gap-3">
                  <span className="font-mono font-bold text-sm text-brand-400">{emb.embarque}</span>
                  <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium border ${badge.cls}`}>
                    {badge.label}
                  </span>
                  <span className="text-xs" style={{ color: 'var(--text-faint)' }}>{emb.carrier}</span>
                </div>
                <div className="flex items-center gap-3 text-xs" style={{ color: 'var(--text-muted)' }}>
                  <span>Esperado: {emb.fechaEsperada}</span>
                  {emb.fechaReal && <span className="text-emerald-500">Llegó: {emb.fechaReal}</span>}
                </div>
              </div>

              {/* Items */}
              <div className="divide-y" style={{ borderColor: 'var(--border)' }}>
                {emb.items.map((item, i) => {
                  const st = estadoItem[item.estado] ?? estadoItem.pendiente
                  return (
                    <div key={i} className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 px-4 py-3">
                      <div className="flex items-center gap-3">
                        <span className={st.cls}>{st.icon}</span>
                        <div>
                          <span className="font-mono text-xs font-semibold" style={{ color: 'var(--text-primary)' }}>{item.parte}</span>
                          <span className="ml-2 text-xs" style={{ color: 'var(--text-muted)' }}>{item.desc}</span>
                        </div>
                      </div>
                      <div className="flex items-center gap-4 text-xs shrink-0">
                        <span style={{ color: 'var(--text-faint)' }}>{item.cliente}</span>
                        <span style={{ color: 'var(--text-muted)' }}>
                          {item.recibido}/{item.cant} uds
                        </span>
                        <span className={`font-semibold capitalize ${st.cls}`}>{item.estado}</span>
                      </div>
                    </div>
                  )
                })}
              </div>

              {emb.estado === 'en_bodega' && (
                <div className="px-4 py-3 border-t flex justify-end" style={{ borderColor: 'var(--border)' }}>
                  <button className="btn-primary text-xs px-4 py-1.5">
                    <Truck className="w-3.5 h-3.5 inline mr-1.5" />
                    Despachar al Cliente
                  </button>
                </div>
              )}
            </div>
          )
        })}
      </div>

      <div className="flex items-center gap-2 p-3 rounded-xl bg-amber-500/10 border border-amber-500/20">
        <Package className="w-4 h-4 text-amber-400 shrink-0" />
        <p className="text-xs text-amber-400">Módulo en desarrollo — datos de demostración. El registro de diferencias y las alertas automáticas a Abastecimiento y Comercial estarán disponibles en la siguiente versión.</p>
      </div>
    </div>
  )
}
