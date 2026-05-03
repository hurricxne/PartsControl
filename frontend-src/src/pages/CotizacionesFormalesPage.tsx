import { useState } from 'react'
import {
  FileText, Plus, Search, Download, Eye, CheckCircle2, Clock,
  XCircle, Send, Filter, Upload, Table2, FilePen, ArrowRight,
  RefreshCw, AlertCircle, ThumbsUp, ThumbsDown, DollarSign,
  Hourglass, Calculator, RotateCcw, Trash2, Package,
} from 'lucide-react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import toast from 'react-hot-toast'
import ValidaCotizacion from '../modules/ValidaCotizacion/ValidaCotizacion'
import { cotizacionesAPI, comprasAPI } from '../services/api'

// ── Tipos de fase ──────────────────────────────────────────────────────────────

type Fase = 'ingresada' | 'validada' | 'enviada' | 'aceptada' | 'rechazada' | 'pagada' | 'pendiente_pago'

const FASE_META: Record<string, { label: string; cls: string; icon: any }> = {
  ingresada:      { label: 'Ingresada',    cls: 'bg-slate-500/10 text-slate-400 border-slate-500/20',       icon: FileText     },
  validada:       { label: 'Validada',     cls: 'bg-blue-500/10 text-blue-400 border-blue-400/20',           icon: CheckCircle2 },
  enviada:        { label: 'Enviada',      cls: 'bg-blue-500/10 text-blue-400 border-blue-400/20',           icon: Send         },
  aceptada:       { label: 'Aceptada',     cls: 'bg-emerald-500/10 text-emerald-500 border-emerald-500/20',  icon: ThumbsUp     },
  rechazada:      { label: 'Rechazada',    cls: 'bg-red-500/10 text-red-400 border-red-400/20',              icon: ThumbsDown   },
  pagada:         { label: 'Pagada',       cls: 'bg-emerald-500/10 text-emerald-500 border-emerald-500/20',  icon: DollarSign   },
  pendiente_pago: { label: 'Pdte. Pago',  cls: 'bg-amber-500/10 text-amber-500 border-amber-500/20',        icon: Hourglass    },
  pendiente:      { label: 'Procesando',   cls: 'bg-amber-500/10 text-amber-400 border-amber-500/20',        icon: Clock        },
  procesando:     { label: 'Procesando',   cls: 'bg-amber-500/10 text-amber-400 border-amber-500/20',        icon: Clock        },
  completado:     { label: 'Completado',   cls: 'bg-green-500/10 text-green-400 border-green-500/20',        icon: CheckCircle2 },
  error:          { label: 'Error',        cls: 'bg-red-500/10 text-red-400 border-red-400/20',              icon: AlertCircle  },
}

function EstadoBadge({ fase, estado }: { fase?: string; estado?: string }) {
  const key = fase && fase !== 'ingresada' ? fase : (estado || 'ingresada')
  const meta = FASE_META[key] ?? FASE_META.ingresada
  const Icon = meta.icon
  return (
    <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium border ${meta.cls}`}>
      <Icon className="w-3 h-3" />
      {meta.label}
    </span>
  )
}

// ── Componente principal ───────────────────────────────────────────────────────

type Tab = 'registro' | 'importar'

export default function CotizacionesFormalesPage() {
  const queryClient = useQueryClient()
  const navigate    = useNavigate()
  const [tab, setTab]               = useState<Tab>('importar')
  const [search, setSearch]         = useState('')
  const [confirmDelete, setConfirmDelete] = useState<number | null>(null)

  // ── Datos reales ──
  const { data: rawList, isLoading } = useQuery<any[]>({
    queryKey: ['cotizaciones'],
    queryFn: () => cotizacionesAPI.list().then(r => r.data),
    refetchInterval: (q) => {
      const d = q.state.data
      return d?.some((c: any) => ['procesando','pendiente','esperando_agente'].includes(c.estado))
        ? 3000 : false
    },
  })
  const cotizaciones: any[] = rawList ?? []

  const { data: preEmbarquesRaw } = useQuery<any[]>({
    queryKey: ['pre-embarques'],
    queryFn: () => comprasAPI.listPreEmbarques().then((r: any) => r.data),
    staleTime: 30000,
  })
  const preEmbarquesAbiertos = (preEmbarquesRaw ?? []).filter((pe: any) => pe.estado !== 'embarcado')

  const eliminarMutation = useMutation({
    mutationFn: (id: number) => cotizacionesAPI.eliminar(id),
    onSuccess: (data) => {
      toast.success(data.data.mensaje || 'Cotización eliminada')
      queryClient.invalidateQueries({ queryKey: ['cotizaciones'] })
      setConfirmDelete(null)
    },
    onError: (err: any) => {
      toast.error(err.response?.data?.detail || 'Error al eliminar')
      setConfirmDelete(null)
    },
  })

  const reprocesarMutation = useMutation({
    mutationFn: (id: number) => cotizacionesAPI.reprocesar(id),
    onSuccess: () => {
      toast.success('Cotización enviada a reprocesar')
      queryClient.invalidateQueries({ queryKey: ['cotizaciones'] })
    },
    onError: () => toast.error('Error al reprocesar'),
  })

  // Filtro de búsqueda
  const filtered = cotizaciones.filter(c => {
    if (!search.trim()) return true
    const q = search.toLowerCase()
    return (
      String(c.numero).includes(q) ||
      (c.cliente || '').toLowerCase().includes(q) ||
      (c.referencia || '').toLowerCase().includes(q)
    )
  })

  // Stats reales
  const stats = [
    { label: 'Total',      value: cotizaciones.length,                                                            sub: 'Cotizaciones registradas',  color: 'text-brand-400'   },
    { label: 'Enviadas',   value: cotizaciones.filter(c => c.fase_comercial === 'enviada').length,                sub: 'Pendiente respuesta',        color: 'text-blue-400'    },
    { label: 'Aceptadas',  value: cotizaciones.filter(c => c.fase_comercial === 'aceptada').length,               sub: 'Convertidas a venta',        color: 'text-emerald-500' },
    { label: 'Completadas',value: cotizaciones.filter(c => c.estado === 'completado').length,                     sub: 'Con precios calculados',     color: 'text-green-400'   },
  ]

  return (
    <div className="space-y-6">

      {/* ── Header ── */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-2xl font-bold" style={{ color: 'var(--text-primary)' }}>Cotizaciones</h1>
          <p className="text-sm mt-1" style={{ color: 'var(--text-muted)' }}>
            Pre-cotización · Registro Unificado
          </p>
        </div>
        <button className="btn-primary flex items-center justify-center gap-2 w-full sm:w-auto shrink-0" onClick={() => setTab('importar')}>
          <Plus className="w-4 h-4" /> Nueva Cotización
        </button>
      </div>

      {/* ── Pre-Embarques abiertos ── */}
      {preEmbarquesAbiertos.length > 0 ? (
        <div className="rounded-2xl border p-4 space-y-2" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
          <p className="text-xs font-semibold uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>Pre-Embarques pendientes</p>
          {preEmbarquesAbiertos.map((pe: any) => {
            const diasPend = pe.max_dias_restantes != null
              ? pe.max_dias_restantes
              : Math.floor((Date.now() - new Date(pe.created_at).getTime()) / 86400000)
            return (
              <div key={pe.id} className="flex items-center gap-3 py-2 px-3 rounded-xl" style={{ backgroundColor: 'var(--surface-200)' }}>
                <div className="w-7 h-7 rounded-lg bg-blue-500/20 flex items-center justify-center shrink-0">
                  <Package className="w-3.5 h-3.5 text-blue-400" />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-semibold text-sm" style={{ color: 'var(--text-primary)' }}>{pe.numero}</span>
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-blue-500/20 text-blue-300">
                      {pe.estado === 'en_preparacion' ? 'En preparación' : pe.estado}
                    </span>
                  </div>
                  <div className="flex items-center gap-3 mt-0.5 text-xs" style={{ color: 'var(--text-faint)' }}>
                    <span>{(pe.items ?? []).length} ítems</span>
                    <span>{pe.peso_total_kg?.toFixed(1)} kg</span>
                    <span>${pe.total_usd_exwork?.toFixed(0)} USD</span>
                    <span className={diasPend <= 5 ? 'text-red-400 font-medium' : 'text-amber-400'}>
                      {diasPend} días pend.
                    </span>
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      ) : null}

      {/* ── Tabs ── */}
      <div className="flex gap-1 p-1 rounded-xl border" style={{ backgroundColor: 'var(--surface-200)', borderColor: 'var(--border)' }}>
        {[
          { id: 'importar' as Tab, icon: Upload,  label: 'Pre-cotización'          },
          { id: 'registro' as Tab, icon: Table2,  label: 'Registro de Cotizaciones', count: cotizaciones.length || undefined },
        ].map(t => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`flex-1 flex items-center justify-center gap-2 py-2 px-3 rounded-lg text-xs font-semibold transition-colors ${
              tab === t.id ? 'bg-[var(--surface)] shadow-sm border border-[var(--border)]' : 'hover:text-[var(--text-primary)] border border-transparent'
            }`}
            style={{ color: tab === t.id ? 'var(--empresa-primary)' : 'var(--text-faint)' }}
          >
            <t.icon className="w-3.5 h-3.5" />
            <span className="hidden sm:inline">{t.label}</span>
            {(t as any).count !== undefined && (
              <span className="text-[10px] font-bold px-1.5 py-0.5 rounded-full"
                style={{ backgroundColor: 'var(--surface-300)', color: 'var(--text-faint)' }}>
                {(t as any).count}
              </span>
            )}
          </button>
        ))}
      </div>

      {/* ══ TAB: PRE-COTIZACIÓN ══ */}
      {tab === 'importar' && (
        <div className="space-y-3">
          <div className="flex items-start gap-2 p-3 rounded-xl bg-blue-500/10 border border-blue-500/20">
            <Upload className="w-4 h-4 text-blue-400 shrink-0 mt-0.5" />
            <p className="text-xs text-blue-400">
              Módulo 3 — Sube el Excel del proveedor o ingresa manualmente. El sistema extrae N° de parte, valida precios en CAT y deja los datos listos para el Cotizador MAQ.
            </p>
          </div>
          <ValidaCotizacion />
        </div>
      )}


      {/* ══ TAB: REGISTRO ══ */}
      {tab === 'registro' && (
        <div className="space-y-5">

          {/* Stats reales */}
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            {stats.map(s => (
              <div key={s.label} className="rounded-2xl p-3 sm:p-4 border" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
                <p className="text-[10px] uppercase tracking-widest leading-tight" style={{ color: 'var(--text-faint)' }}>{s.label}</p>
                <p className={`text-lg sm:text-xl font-bold mt-1 ${s.color}`}>{s.value}</p>
                <p className="text-xs mt-0.5 leading-tight" style={{ color: 'var(--text-muted)' }}>{s.sub}</p>
              </div>
            ))}
          </div>

          {/* Búsqueda */}
          <div className="flex flex-col sm:flex-row gap-3">
            <div className="relative flex-1 max-w-sm">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4" style={{ color: 'var(--text-faint)' }} />
              <input
                className="input pl-9 w-full"
                placeholder="Buscar por N°, cliente, referencia…"
                value={search}
                onChange={e => setSearch(e.target.value)}
              />
            </div>
            <button
              onClick={() => queryClient.invalidateQueries({ queryKey: ['cotizaciones'] })}
              className="btn-secondary flex items-center gap-2"
            >
              <RefreshCw className="w-4 h-4" /> Actualizar
            </button>
          </div>

          {/* Tabla */}
          {isLoading ? (
            <div className="card p-8 flex items-center justify-center gap-3" style={{ color: 'var(--text-faint)' }}>
              <RefreshCw className="w-5 h-5 animate-spin" />
              <span>Cargando registros...</span>
            </div>
          ) : filtered.length === 0 ? (
            <div className="card p-12 text-center">
              <FileText className="w-12 h-12 mx-auto mb-3" style={{ color: 'var(--text-faint)' }} />
              <p className="font-medium" style={{ color: 'var(--text-muted)' }}>
                {search ? 'Sin resultados para tu búsqueda' : 'No hay cotizaciones aún'}
              </p>
              {!search && (
                <button onClick={() => setTab('importar')} className="mt-3 text-sm font-medium" style={{ color: 'var(--empresa-primary)' }}>
                  Crear primera cotización →
                </button>
              )}
            </div>
          ) : (
            <div className="rounded-2xl border overflow-hidden" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
                      {['N° Cot.', 'Fecha', 'Cliente', 'Referencia', 'Ítems', 'Estado', 'Acciones'].map(h => (
                        <th key={h} className="text-left px-4 py-3 text-xs font-semibold uppercase tracking-wider whitespace-nowrap" style={{ color: 'var(--text-faint)' }}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {filtered.map((cot: any) => (
                      <tr key={cot.id} className="border-b hover:bg-[var(--surface-200)] transition-colors" style={{ borderColor: 'var(--border)' }}>
                        <td className="px-4 py-3 font-mono font-semibold whitespace-nowrap" style={{ color: 'var(--empresa-primary)' }}>
                          {cot.es_formal ? 'COT' : 'PRE-COT'}-{String(cot.numero).padStart(6, '0')}
                        </td>
                        <td className="px-4 py-3 whitespace-nowrap text-xs" style={{ color: 'var(--text-muted)' }}>
                          {new Date(cot.created_at).toLocaleDateString('es-CL')}
                        </td>
                        <td className="px-4 py-3 font-medium max-w-[180px] truncate" style={{ color: 'var(--text-primary)' }}>
                          {cot.cliente || <span style={{ color: 'var(--text-faint)' }}>—</span>}
                        </td>
                        <td className="px-4 py-3 text-xs max-w-[140px] truncate" style={{ color: 'var(--text-muted)' }}>
                          {cot.referencia || <span style={{ color: 'var(--text-faint)' }}>—</span>}
                        </td>
                        <td className="px-4 py-3 text-center text-xs" style={{ color: 'var(--text-muted)' }}>
                          {cot.total_items}
                        </td>
                        <td className="px-4 py-3">
                          <EstadoBadge fase={cot.fase_comercial} estado={cot.estado} />
                        </td>
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-1">
                            {cot.estado === 'completado' && (
                              <button
                                onClick={() => navigate(`/cotizaciones/${cot.id}/editor`)}
                                className="btn-primary text-xs py-1 px-2"
                                title="Abrir cotizador"
                              >
                                <Calculator className="w-3 h-3" />
                              </button>
                            )}
                            {cot.tiene_resultado && (
                              <a
                                href={cotizacionesAPI.downloadUrl(cot.id)}
                                download
                                className="btn-secondary text-xs py-1 px-2"
                                title="Descargar Excel CAT"
                              >
                                <Download className="w-3 h-3" />
                              </a>
                            )}
                            <button
                              onClick={() => reprocesarMutation.mutate(cot.id)}
                              disabled={reprocesarMutation.isPending}
                              className="btn-secondary text-xs py-1 px-2"
                              title="Reprocesar"
                            >
                              <RotateCcw className={`w-3 h-3 ${reprocesarMutation.isPending ? 'animate-spin' : ''}`} />
                            </button>
                            {['ingresada', 'validada'].includes(cot.fase_comercial ?? 'ingresada') && (
                              confirmDelete === cot.id ? (
                                <div className="flex items-center gap-1">
                                  <span className="text-xs text-red-400">¿Eliminar?</span>
                                  <button onClick={() => eliminarMutation.mutate(cot.id)} className="text-xs px-2 py-1 rounded bg-red-500/20 text-red-400 border border-red-500/30">Sí</button>
                                  <button onClick={() => setConfirmDelete(null)} className="text-xs px-2 py-1 rounded btn-secondary">No</button>
                                </div>
                              ) : (
                                <button
                                  onClick={() => setConfirmDelete(cot.id)}
                                  className="btn-secondary text-xs py-1 px-2 hover:text-red-400 hover:border-red-500/30 transition-colors"
                                  title="Eliminar"
                                >
                                  <Trash2 className="w-3 h-3" />
                                </button>
                              )
                            )}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="px-4 py-3 border-t flex items-center justify-between" style={{ borderColor: 'var(--border)' }}>
                <p className="text-xs" style={{ color: 'var(--text-faint)' }}>
                  {filtered.length} de {cotizaciones.length} cotizaciones
                </p>
              </div>
            </div>
          )}
        </div>
      )}

    </div>
  )
}
