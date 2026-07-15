import { useState, useCallback } from 'react'
import { useDropzone } from 'react-dropzone'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import {
  Upload, FileSpreadsheet, CheckCircle2, Clock,
  Download, RefreshCw, Search, AlertCircle, TrendingUp,
  Package, Eye, ChevronDown, RotateCcw, Ship, FileText,
  Send, ThumbsUp, ThumbsDown, DollarSign, Hourglass, Trash2, Calculator, Edit3,
  FilePlus2, List,
} from 'lucide-react'
import toast from 'react-hot-toast'
import { cotizacionesAPI, cotizadorAPI, comprasAPI } from '../../services/api'
import CotizacionTable from './CotizacionTable'
import ProcessingCard from './ProcessingCard'

// ── Ciclo de vida comercial ────────────────────────────────────────────────────

type Fase =
  | 'ingresada' | 'validada' | 'semi_validada'
  | 'pre_embarcado' | 'embarcado' | 'pagado'

const FASE_META: Record<Fase, { label: string; color: string; icon: any }> = {
  ingresada:     { label: 'Ingresada',     color: 'slate',   icon: FileSpreadsheet },
  validada:      { label: 'Validada',      color: 'emerald', icon: CheckCircle2 },
  semi_validada: { label: 'Semi-validada', color: 'amber',   icon: Clock },
  pre_embarcado: { label: 'Pre-embarcado', color: 'blue',    icon: Package },
  embarcado:     { label: 'Embarcado',     color: 'violet',  icon: Ship },
  pagado:        { label: 'Pagado',        color: 'green',   icon: DollarSign },
}

const TRANSICIONES: Record<Fase, Fase[]> = {
  ingresada:     ['validada', 'semi_validada'],
  validada:      ['pre_embarcado'],
  semi_validada: ['validada', 'pre_embarcado'],
  pre_embarcado: ['embarcado'],
  embarcado:     ['pagado'],
  pagado:        [],
}

const PASOS_LINEALES: Fase[] = ['ingresada', 'validada', 'pre_embarcado', 'embarcado', 'pagado']

function faseIndex(fase: Fase): number {
  if (fase === 'semi_validada') return 1
  return PASOS_LINEALES.indexOf(fase)
}

const COLOR_CLASSES: Record<string, { pill: string; dot: string; text: string }> = {
  slate:   { pill: 'bg-slate-500/15 border-slate-500/30 text-slate-400',   dot: 'bg-slate-400',   text: 'text-slate-400' },
  blue:    { pill: 'bg-blue-500/15 border-blue-500/30 text-blue-400',      dot: 'bg-blue-400',    text: 'text-blue-400'  },
  violet:  { pill: 'bg-violet-500/15 border-violet-500/30 text-violet-400',dot: 'bg-violet-400',  text: 'text-violet-400'},
  green:   { pill: 'bg-green-500/15 border-green-500/30 text-green-400',   dot: 'bg-green-400',   text: 'text-green-400' },
  red:     { pill: 'bg-red-500/15 border-red-500/30 text-red-400',         dot: 'bg-red-400',     text: 'text-red-400'   },
  emerald: { pill: 'bg-emerald-500/15 border-emerald-500/30 text-emerald-400', dot: 'bg-emerald-400', text: 'text-emerald-400' },
  amber:   { pill: 'bg-amber-500/15 border-amber-500/30 text-amber-400',   dot: 'bg-amber-400',   text: 'text-amber-400' },
}

// Colores de borde izquierdo y fondo sutil por fase
const FASE_CARD_STYLE: Record<string, { borderColor: string; bg?: string; iconColor: string }> = {
  ingresada:     { borderColor: 'rgba(100,116,139,0.35)', iconColor: '#94a3b8' },
  semi_validada: { borderColor: 'rgba(245,158,11,0.5)',   iconColor: '#f59e0b' },
  validada:      { borderColor: 'rgba(16,185,129,0.7)',   bg: 'rgba(16,185,129,0.03)', iconColor: '#10b981' },
  pre_embarcado: { borderColor: 'rgba(59,130,246,0.6)',   bg: 'rgba(59,130,246,0.03)', iconColor: '#3b82f6' },
  embarcado:     { borderColor: 'rgba(139,92,246,0.6)',   bg: 'rgba(139,92,246,0.03)', iconColor: '#8b5cf6' },
  pagado:        { borderColor: 'rgba(34,197,94,0.7)',    bg: 'rgba(34,197,94,0.03)',  iconColor: '#22c55e' },
}

function FaseBadge({ fase }: { fase: Fase }) {
  const meta = FASE_META[fase] || FASE_META.ingresada
  const cls  = COLOR_CLASSES[meta.color]
  const Icon = meta.icon
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium border ${cls.pill}`}>
      <Icon className="w-3 h-3" />
      {meta.label}
    </span>
  )
}

interface LifecycleStepperProps {
  cotId: number
  fase: Fase
  onFaseChange: (nuevaFase: Fase) => void
}

function LifecycleStepper({ cotId, fase, onFaseChange }: LifecycleStepperProps) {
  const queryClient  = useQueryClient()
  const faseActual   = fase in FASE_META ? fase : 'ingresada'
  const siguientes   = TRANSICIONES[faseActual] ?? []
  const currentIndex = faseIndex(faseActual)

  const mutation = useMutation({
    mutationFn: (nuevaFase: Fase) => cotizacionesAPI.updateFase(cotId, nuevaFase),
    onSuccess: (_, nuevaFase) => {
      onFaseChange(nuevaFase)
      queryClient.invalidateQueries({ queryKey: ['cotizaciones'] })
      toast.success(`Fase actualizada: ${FASE_META[nuevaFase]?.label}`)
    },
    onError: () => toast.error('Error al actualizar la fase'),
  })

  return (
    <div className="mt-2.5 flex flex-wrap items-center gap-2">
      <div className="flex items-center gap-0.5">
        {PASOS_LINEALES.map((paso, i) => {
          const done    = currentIndex > i
          const active  = currentIndex === i
          const isLast  = i === PASOS_LINEALES.length - 1

          const displayFase: Fase =
            i === 1 && faseActual === 'semi_validada' ? 'semi_validada' :
            paso

          const meta = FASE_META[displayFase]
          const cls  = COLOR_CLASSES[meta.color]

          return (
            <div key={paso} className="flex items-center">
              <div className={`
                flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium
                transition-all duration-200
                ${active
                  ? `border ${cls.pill} ring-1 ring-offset-1 ring-offset-[var(--surface-100)]`
                  : done
                  ? 'bg-brand-600/20 text-brand-400 border border-brand-600/30'
                  : 'bg-[var(--surface-200)] text-[var(--text-faint)] border border-[var(--border)]'
                }
              `}>
                {done ? (
                  <CheckCircle2 className="w-2.5 h-2.5 text-brand-400" />
                ) : (
                  <span className={`w-2 h-2 rounded-full ${active ? cls.dot : 'bg-[var(--text-faint)]'}`} />
                )}
                <span className="hidden sm:inline">{meta.label}</span>
              </div>
              {!isLast && (
                <div className={`w-3 h-px mx-0.5 ${done ? 'bg-brand-600/40' : 'bg-[var(--border)]'}`} />
              )}
            </div>
          )
        })}
      </div>

      {siguientes.length > 0 && (
        <div className="flex items-center gap-1.5 ml-1">
          {siguientes.map(sig => {
            const meta = FASE_META[sig]
            const cls  = COLOR_CLASSES[meta.color]
            const Icon = meta.icon
            return (
              <button
                key={sig}
                onClick={() => mutation.mutate(sig)}
                disabled={mutation.isPending}
                className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-semibold border transition-all duration-150 hover:opacity-80 ${cls.pill}`}
              >
                <Icon className="w-2.5 h-2.5" />
                {meta.label}
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}

// ── Componente principal ───────────────────────────────────────────────────────

type Tab = 'precotizacion' | 'registro'

export default function ValidaCotizacion() {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [tab, setTab] = useState<Tab>('precotizacion')
  const [selectedCot, setSelectedCot] = useState<number | null>(null)
  const [expandedCot, setExpandedCot] = useState<number | null>(null)
  const [fasesLocales, setFasesLocales] = useState<Record<number, Fase>>({})
  const [confirmDelete, setConfirmDelete] = useState<number | null>(null)
  const [downloadingExcel, setDownloadingExcel] = useState<number | null>(null)
  const [downloadingPdf, setDownloadingPdf] = useState<number | null>(null)
  const [downloadingFormal, setDownloadingFormal] = useState<number | null>(null)

  const handleDownloadExcel = async (e: React.MouseEvent, cotId: number, numero: string) => {
    e.stopPropagation()
    setDownloadingExcel(cotId)
    try {
      const resp = await cotizacionesAPI.download(cotId)
      const blob = new Blob([resp.data], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url; a.download = `CAT-${numero}.xlsx`
      document.body.appendChild(a); a.click(); document.body.removeChild(a)
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch { toast.error('Error al descargar Excel') }
    finally { setDownloadingExcel(null) }
  }

  const handleDownloadPdf = async (e: React.MouseEvent, cotId: number, numero: string) => {
    e.stopPropagation()
    setDownloadingPdf(cotId)
    try {
      const resp = await cotizadorAPI.downloadPdf(cotId)
      const blob = new Blob([resp.data], { type: 'application/pdf' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url; a.download = `Cotizacion-${numero}.pdf`
      document.body.appendChild(a); a.click(); document.body.removeChild(a)
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch { toast.error('Error al descargar PDF') }
    finally { setDownloadingPdf(null) }
  }

  const handleDownloadFormal = async (e: React.MouseEvent, cotId: number, numero: string) => {
    e.stopPropagation()
    setDownloadingFormal(cotId)
    try {
      const resp = await cotizadorAPI.downloadFormal(cotId)
      const blob = new Blob([resp.data], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url; a.download = `CotizacionFormal-${numero}.xlsx`
      document.body.appendChild(a); a.click(); document.body.removeChild(a)
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch { toast.error('Error al descargar Excel formal') }
    finally { setDownloadingFormal(null) }
  }

  const { data: cotizacionesRaw, isLoading: loadingList } = useQuery<any[]>({
    queryKey: ['cotizaciones'],
    queryFn: () => cotizacionesAPI.list().then((r) => r.data),
    refetchInterval: (query) => {
      const data = query.state.data
      const hasProcessing = data?.some((c: any) =>
        ['procesando', 'pendiente', 'esperando_agente'].includes(c.estado)
      )
      return hasProcessing ? 3000 : false
    },
  })
  const cotizaciones: any[] = cotizacionesRaw ?? []

  const { data: preEmbarquesRaw } = useQuery<any[]>({
    queryKey: ['pre-embarques'],
    queryFn: () => comprasAPI.listPreEmbarques().then((r: any) => r.data),
    staleTime: 30000,
  })
  const preEmbarquesAbiertos = (preEmbarquesRaw ?? []).filter((pe: any) => pe.estado !== 'embarcado')

  const uploadMutation = useMutation({
    mutationFn: (file: File) => cotizacionesAPI.upload(file),
    onSuccess: (data) => {
      toast.success(`COT-${data.data.numero}: ${data.data.total_items} partes recibidas`)
      queryClient.invalidateQueries({ queryKey: ['cotizaciones'] })
      setSelectedCot(data.data.id)
    },
    onError: (err: any) => {
      toast.error(err.response?.data?.detail || 'Error al subir el archivo')
    },
  })

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
    onSuccess: (_, id) => {
      toast.success('Cotización enviada a reprocesar')
      queryClient.invalidateQueries({ queryKey: ['cotizaciones'] })
      setSelectedCot(id)
    },
    onError: () => toast.error('Error al reprocesar'),
  })

  const onDrop = useCallback((accepted: File[]) => {
    if (!accepted.length) return
    const file = accepted[0]
    if (!file.name.match(/\.(xlsx|xls)$/i)) {
      toast.error('Solo se aceptan archivos Excel (.xlsx, .xls)')
      return
    }
    uploadMutation.mutate(file)
  }, [uploadMutation])

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    accept: {
      'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': ['.xlsx'],
      'application/vnd.ms-excel': ['.xls'],
    },
    multiple: false,
  })

  // Subir Excel con precios de VENTA en CLP (margen incluido, con días)
  const uploadVentaMutation = useMutation({
    mutationFn: (file: File) => cotizacionesAPI.uploadVenta(file),
    onSuccess: (data: any) => {
      toast.success(`Cotización ${data.data.numero}: ${data.data.items} ítems (precios de venta CLP)`)
      queryClient.invalidateQueries({ queryKey: ['cotizaciones'] })
      setSelectedCot(data.data.id)
    },
    onError: (err: any) => {
      toast.error(err.response?.data?.detail || 'Error al subir el Excel de precios de venta')
    },
  })

  const onDropVenta = useCallback((accepted: File[]) => {
    if (!accepted.length) return
    const file = accepted[0]
    if (!file.name.match(/\.(xlsx|xls)$/i)) {
      toast.error('Solo se aceptan archivos Excel (.xlsx, .xls)')
      return
    }
    uploadVentaMutation.mutate(file)
  }, [uploadVentaMutation])

  const { getRootProps: getRootPropsVenta, getInputProps: getInputPropsVenta, isDragActive: isDragActiveVenta } = useDropzone({
    onDrop: onDropVenta,
    accept: {
      'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': ['.xlsx'],
      'application/vnd.ms-excel': ['.xls'],
    },
    multiple: false,
  })

  const stats = {
    total: cotizaciones.length,
    completados: cotizaciones.filter((c: any) => c.estado === 'completado').length,
    procesando: cotizaciones.filter((c: any) =>
      ['procesando', 'pendiente', 'esperando_agente'].includes(c.estado)
    ).length,
    totalPartes: cotizaciones.reduce((a: number, c: any) => a + (c.items_encontrados || 0), 0),
  }

  const getFase = (cot: any): Fase =>
    (fasesLocales[cot.id] ?? cot.fase_comercial ?? 'ingresada') as Fase

  const TABS: { id: Tab; label: string; icon: any; count?: number }[] = [
    { id: 'precotizacion', label: 'Pre-cotización', icon: FilePlus2 },
    { id: 'registro',      label: 'Registro',       icon: List, count: cotizaciones.length || undefined },
  ]

  return (
    <div className="space-y-5 max-w-5xl mx-auto animate-slide-up">

      {/* ── Header ── */}
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-bold" style={{ color: 'var(--text-primary)' }}>
            Cotizaciones
          </h1>
          <p className="text-sm mt-1" style={{ color: 'var(--text-muted)' }}>
            Genera y gestiona pre-cotizaciones con precios CAT y Finning
          </p>
        </div>
        <button
          onClick={() => queryClient.invalidateQueries({ queryKey: ['cotizaciones'] })}
          className="btn-secondary text-sm"
        >
          <RefreshCw className="w-4 h-4" />
          Actualizar
        </button>
      </div>

      {/* ── Tabs ── */}
      <div
        className="flex gap-1 p-1 rounded-xl w-fit"
        style={{ backgroundColor: 'var(--surface-200)', border: '1px solid var(--border)' }}
      >
        {TABS.map(t => {
          const Icon = t.icon
          const isActive = tab === t.id
          return (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-all duration-150"
              style={isActive ? {
                backgroundColor: 'var(--surface-100)',
                color: 'var(--empresa-primary)',
                boxShadow: '0 1px 3px rgba(0,0,0,0.12)',
                border: '1px solid var(--border)',
              } : {
                backgroundColor: 'transparent',
                color: 'var(--text-faint)',
                border: '1px solid transparent',
              }}
            >
              <Icon className="w-4 h-4" />
              {t.label}
              {t.count !== undefined && (
                <span
                  className="text-xs font-semibold px-1.5 py-0.5 rounded-full"
                  style={isActive
                    ? { backgroundColor: 'var(--empresa-primary-sh)', color: 'var(--empresa-primary)' }
                    : { backgroundColor: 'var(--surface-300)', color: 'var(--text-faint)' }
                  }
                >
                  {t.count}
                </span>
              )}
            </button>
          )
        })}
      </div>

      {/* ══ TAB: Pre-cotización ══ */}
      {tab === 'precotizacion' && (
        <div className="space-y-5">

          {/* Opciones de ingreso */}
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
            {/* Subir Excel */}
            <div
              {...getRootProps()}
              className={`
                relative border-2 border-dashed rounded-2xl p-8 text-center cursor-pointer transition-all duration-300
                ${isDragActive
                  ? 'border-[var(--empresa-primary)] bg-[var(--empresa-primary-sh)] scale-[1.01]'
                  : uploadMutation.isPending
                  ? 'border-amber-500/50 bg-amber-500/5 cursor-not-allowed'
                  : 'border-[var(--border)] hover:border-[var(--empresa-primary)] hover:bg-[var(--empresa-primary-sh)] bg-[var(--surface-100)]'
                }
              `}
            >
              <input {...getInputProps()} disabled={uploadMutation.isPending} />
              <div className="flex flex-col items-center gap-3">
                {uploadMutation.isPending ? (
                  <>
                    <div className="w-12 h-12 rounded-2xl bg-amber-500/20 flex items-center justify-center">
                      <RefreshCw className="w-6 h-6 text-amber-400 animate-spin" />
                    </div>
                    <div>
                      <p className="font-semibold text-amber-400">Procesando Excel...</p>
                      <p className="text-sm mt-1" style={{ color: 'var(--text-faint)' }}>Por favor espera</p>
                    </div>
                  </>
                ) : isDragActive ? (
                  <>
                    <div className="w-12 h-12 rounded-2xl flex items-center justify-center"
                      style={{ backgroundColor: 'var(--empresa-primary-sh)' }}>
                      <Upload className="w-6 h-6" style={{ color: 'var(--empresa-primary)' }} />
                    </div>
                    <p className="font-semibold" style={{ color: 'var(--empresa-primary)' }}>Suelta el archivo aquí</p>
                  </>
                ) : (
                  <>
                    <div className="w-12 h-12 rounded-2xl bg-[var(--surface-200)] flex items-center justify-center">
                      <FileSpreadsheet className="w-6 h-6" style={{ color: 'var(--text-muted)' }} />
                    </div>
                    <div>
                      <p className="font-semibold" style={{ color: 'var(--text-primary)' }}>
                        Subir Excel de cotización
                      </p>
                      <p className="text-sm mt-1" style={{ color: 'var(--text-muted)' }}>
                        Arrastra o <span className="font-medium" style={{ color: 'var(--empresa-primary)' }}>haz clic</span> · .xlsx / .xls
                      </p>
                    </div>
                    <div className="flex flex-wrap justify-center items-center gap-3 mt-1 text-xs" style={{ color: 'var(--text-faint)' }}>
                      <span className="flex items-center gap-1"><Package className="w-3 h-3" /> Lee N° PARTE</span>
                      <span className="flex items-center gap-1"><Search className="w-3 h-3" /> Busca en CAT</span>
                      <span className="flex items-center gap-1"><Download className="w-3 h-3" /> Genera Excel</span>
                    </div>
                  </>
                )}
              </div>
            </div>

            {/* Subir Excel con precios de VENTA (CLP) */}
            <div
              {...getRootPropsVenta()}
              className={`relative border-2 border-dashed rounded-2xl p-8 text-center cursor-pointer transition-all duration-300 ${isDragActiveVenta ? 'border-[var(--empresa-primary)] bg-[var(--empresa-primary-sh)] scale-[1.01]' : uploadVentaMutation.isPending ? 'border-amber-500/50 bg-amber-500/5 cursor-not-allowed' : 'border-[var(--border)] hover:border-[var(--empresa-primary)] hover:bg-[var(--empresa-primary-sh)] bg-[var(--surface-100)]'}`}
            >
              <input {...getInputPropsVenta()} disabled={uploadVentaMutation.isPending} />
              <div className="flex flex-col items-center gap-3">
                {uploadVentaMutation.isPending ? (
                  <>
                    <div className="w-12 h-12 rounded-2xl bg-amber-500/20 flex items-center justify-center">
                      <RefreshCw className="w-6 h-6 text-amber-400 animate-spin" />
                    </div>
                    <div>
                      <p className="font-semibold text-amber-400">Procesando Excel...</p>
                      <p className="text-sm mt-1" style={{ color: 'var(--text-faint)' }}>Por favor espera</p>
                    </div>
                  </>
                ) : isDragActiveVenta ? (
                  <>
                    <div className="w-12 h-12 rounded-2xl flex items-center justify-center" style={{ backgroundColor: 'var(--empresa-primary-sh)' }}>
                      <Upload className="w-6 h-6" style={{ color: 'var(--empresa-primary)' }} />
                    </div>
                    <p className="font-semibold" style={{ color: 'var(--empresa-primary)' }}>Suelta el archivo aquí</p>
                  </>
                ) : (
                  <>
                    <div className="w-12 h-12 rounded-2xl bg-[var(--surface-200)] flex items-center justify-center">
                      <FileSpreadsheet className="w-6 h-6" style={{ color: 'var(--text-muted)' }} />
                    </div>
                    <div>
                      <p className="font-semibold" style={{ color: 'var(--text-primary)' }}>
                        Subir Excel con precios de venta
                      </p>
                      <p className="text-sm mt-1" style={{ color: 'var(--text-muted)' }}>
                        Precios en CLP (margen incluido) · .xlsx / .xls
                      </p>
                    </div>
                    <div className="flex flex-wrap justify-center items-center gap-3 mt-1 text-xs" style={{ color: 'var(--text-faint)' }}>
                      <span className="flex items-center gap-1"><Package className="w-3 h-3" /> Precio CLP</span>
                      <span className="flex items-center gap-1"><Download className="w-3 h-3" /> Con días</span>
                    </div>
                  </>
                )}
              </div>
            </div>

            {/* Ingreso Manual */}
            <button
              onClick={() => navigate('/cotizaciones/nueva-manual')}
              className="relative border-2 border-dashed rounded-2xl p-8 text-center transition-all duration-300 border-[var(--border)] hover:border-[var(--empresa-primary)] hover:bg-[var(--empresa-primary-sh)] bg-[var(--surface-100)] group"
            >
              <div className="flex flex-col items-center gap-3">
                <div className="w-12 h-12 rounded-2xl bg-[var(--surface-200)] flex items-center justify-center group-hover:bg-[var(--empresa-primary-sh)] transition-colors">
                  <Edit3 className="w-6 h-6" style={{ color: 'var(--text-muted)' }} />
                </div>
                <div>
                  <p className="font-semibold" style={{ color: 'var(--text-primary)' }}>
                    Ingreso Manual
                  </p>
                  <p className="text-sm mt-1" style={{ color: 'var(--text-muted)' }}>
                    Pega desde Excel o ingresa fila a fila
                  </p>
                </div>
                <div className="flex flex-wrap justify-center items-center gap-3 mt-1 text-xs" style={{ color: 'var(--text-faint)' }}>
                  <span className="flex items-center gap-1"><Upload className="w-3 h-3" /> Ctrl+V desde Excel</span>
                  <span className="flex items-center gap-1"><Edit3 className="w-3 h-3" /> Digitación directa</span>
                </div>
              </div>
            </button>
          </div>

          {/* Processing card */}
          {selectedCot && (
            <ProcessingCard cotizacionId={selectedCot} onClose={() => setSelectedCot(null)} />
          )}

          {/* Últimas cotizaciones (vista reducida) */}
          {cotizaciones.length > 0 && (
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <h2 className="text-xs font-semibold uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>
                  Recientes
                </h2>
                <button
                  onClick={() => setTab('registro')}
                  className="text-xs font-medium transition-colors"
                  style={{ color: 'var(--empresa-primary)' }}
                >
                  Ver todas →
                </button>
              </div>
              {cotizaciones.slice(0, 3).map((cot: any) => {
                const faseR = getFase(cot)
                const cardStyle = FASE_CARD_STYLE[faseR] ?? FASE_CARD_STYLE.ingresada
                const faseMeta = FASE_META[faseR] ?? FASE_META.ingresada
                const FaseIconR = faseMeta.icon
                return (
                <div key={cot.id} className="card px-4 py-3 flex items-center gap-3 overflow-hidden"
                  style={{ borderLeft: `3px solid ${cardStyle.borderColor}`, backgroundColor: cardStyle.bg }}>
                  {faseR !== 'ingresada'
                    ? <div className="w-8 h-8 rounded-xl flex items-center justify-center shrink-0"
                        style={{ backgroundColor: `${cardStyle.iconColor}18` }}>
                        <FaseIconR className="w-4 h-4" style={{ color: cardStyle.iconColor }} />
                      </div>
                    : <StatusIcon estado={cot.estado} />
                  }
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-semibold text-sm" style={{ color: 'var(--text-primary)' }}>
                        {cot.es_formal ? 'COT' : 'PRE-COT'}-{cot.numero}
                      </span>
                      {cot.cliente && <span className="text-xs truncate" style={{ color: 'var(--text-muted)' }}>{cot.cliente}</span>}
                      <FaseBadge fase={getFase(cot)} />
                    </div>
                    <p className="text-xs mt-0.5" style={{ color: 'var(--text-faint)' }}>
                      {cot.total_items} partes · {new Date(cot.created_at).toLocaleDateString('es-CL')}
                    </p>
                  </div>
                  <div className="flex items-center gap-1.5 shrink-0">
                    {cot.tiene_resultado ? (
                      <button
                        onClick={(e) => handleDownloadExcel(e, cot.id, cot.numero)}
                        disabled={downloadingExcel === cot.id}
                        className="btn-secondary text-xs py-1.5 px-2"
                        title="Descargar Excel CAT"
                      >
                        {downloadingExcel === cot.id
                          ? <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                          : <Download className="w-3.5 h-3.5" />}
                      </button>
                    ) : (
                      <span className="btn-secondary text-xs py-1.5 px-2 opacity-30 cursor-not-allowed" title="Excel no disponible aún">
                        <Download className="w-3.5 h-3.5" />
                      </span>
                    )}
                    {cot.estado === 'completado' && (
                      <button
                        onClick={(e) => handleDownloadPdf(e, cot.id, cot.numero)}
                        disabled={downloadingPdf === cot.id}
                        className="btn-secondary text-xs py-1.5 px-2"
                        title="PDF cotización cliente"
                      >
                        {downloadingPdf === cot.id
                          ? <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                          : <FileText className="w-3.5 h-3.5" />}
                      </button>
                    )}
                    {cot.es_formal && (
                      <button
                        onClick={(e) => handleDownloadFormal(e, cot.id, cot.numero)}
                        disabled={downloadingFormal === cot.id}
                        className="btn-secondary text-xs py-1.5 px-2"
                        title="Descargar Excel formal"
                      >
                        {downloadingFormal === cot.id
                          ? <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                          : <FileSpreadsheet className="w-3.5 h-3.5" />}
                      </button>
                    )}
                    {cot.estado === 'completado' && (
                      <button
                        onClick={() => navigate(`/cotizaciones/${cot.id}/editor`)}
                        className="btn-primary text-xs py-1.5 px-3"
                      >
                        <Calculator className="w-3.5 h-3.5" />
                        Cotizador
                      </button>
                    )}
                  </div>
                </div>
                )})}
            </div>
          )}

          {/* Pre-Embarques abiertos */}
          {preEmbarquesAbiertos.length > 0 && (
            <div className="space-y-2">
              <h2 className="text-xs font-semibold uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>
                Pre-Embarques pendientes
              </h2>
              {preEmbarquesAbiertos.map((pe: any) => {
                const diasPend = pe.max_dias_restantes != null
                  ? pe.max_dias_restantes
                  : Math.floor((Date.now() - new Date(pe.created_at).getTime()) / 86400000)
                return (
                  <div key={pe.id} className="card px-4 py-3 flex items-center gap-3">
                    <div className="w-8 h-8 rounded-lg bg-blue-500/20 flex items-center justify-center shrink-0">
                      <Package className="w-4 h-4 text-blue-400" />
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="font-semibold text-sm" style={{ color: 'var(--text-primary)' }}>
                          {pe.numero}
                        </span>
                        <span className="text-xs px-1.5 py-0.5 rounded bg-blue-500/20 text-blue-300">
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
          )}
        </div>
      )}

      {/* ══ TAB: Registro ══ */}
      {tab === 'registro' && (
        <div className="space-y-5">

          {/* Stats */}
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            <div className="stat-card">
              <div className="flex items-center justify-between">
                <span className="text-xs font-medium uppercase tracking-wide" style={{ color: 'var(--text-faint)' }}>Cotizaciones</span>
                <FileSpreadsheet className="w-4 h-4 text-brand-400" />
              </div>
              <span className="text-3xl font-bold" style={{ color: 'var(--text-primary)' }}>{stats.total}</span>
              <span className="text-xs" style={{ color: 'var(--text-faint)' }}>Total procesadas</span>
            </div>
            <div className="stat-card">
              <div className="flex items-center justify-between">
                <span className="text-xs font-medium uppercase tracking-wide" style={{ color: 'var(--text-faint)' }}>Completadas</span>
                <CheckCircle2 className="w-4 h-4 text-green-400" />
              </div>
              <span className="text-3xl font-bold text-green-400">{stats.completados}</span>
              <span className="text-xs" style={{ color: 'var(--text-faint)' }}>Con resultado Excel</span>
            </div>
            <div className="stat-card">
              <div className="flex items-center justify-between">
                <span className="text-xs font-medium uppercase tracking-wide" style={{ color: 'var(--text-faint)' }}>En proceso</span>
                <Clock className="w-4 h-4 text-amber-400" />
              </div>
              <span className="text-3xl font-bold text-amber-400">{stats.procesando}</span>
              <span className="text-xs" style={{ color: 'var(--text-faint)' }}>Activas ahora</span>
            </div>
            <div className="stat-card">
              <div className="flex items-center justify-between">
                <span className="text-xs font-medium uppercase tracking-wide" style={{ color: 'var(--text-faint)' }}>Partes validadas</span>
                <TrendingUp className="w-4 h-4 text-brand-400" />
              </div>
              <span className="text-3xl font-bold text-brand-400">{stats.totalPartes}</span>
              <span className="text-xs" style={{ color: 'var(--text-faint)' }}>Encontradas en CAT</span>
            </div>
          </div>

          {/* Lista completa */}
          {loadingList ? (
            <div className="card p-8 flex items-center justify-center gap-3" style={{ color: 'var(--text-faint)' }}>
              <RefreshCw className="w-5 h-5 animate-spin" />
              <span>Cargando historial...</span>
            </div>
          ) : cotizaciones.length === 0 ? (
            <div className="card p-12 text-center">
              <FileSpreadsheet className="w-12 h-12 mx-auto mb-3" style={{ color: 'var(--text-faint)' }} />
              <p className="font-medium" style={{ color: 'var(--text-muted)' }}>No hay cotizaciones aún</p>
              <p className="text-sm mt-1" style={{ color: 'var(--text-faint)' }}>
                Ve a{' '}
                <button onClick={() => setTab('precotizacion')} className="font-medium" style={{ color: 'var(--empresa-primary)' }}>
                  Pre-cotización
                </button>
                {' '}para crear una
              </p>
            </div>
          ) : (
            <div className="space-y-3">
              {cotizaciones.map((cot: any) => {
                const fase = getFase(cot)
                const cardStyleF = FASE_CARD_STYLE[fase] ?? FASE_CARD_STYLE.ingresada
                const faseMetaF  = FASE_META[fase] ?? FASE_META.ingresada
                const FaseIconF  = faseMetaF.icon
                return (
                  <div key={cot.id} className="card overflow-hidden"
                    style={{ borderLeft: `3px solid ${cardStyleF.borderColor}`, backgroundColor: cardStyleF.bg }}>
                    <div className="flex items-start gap-4 p-4">
                      {fase !== 'ingresada'
                        ? <div className="w-10 h-10 rounded-xl flex items-center justify-center shrink-0 mt-0.5"
                            style={{ backgroundColor: `${cardStyleF.iconColor}18` }}>
                            <FaseIconF className="w-5 h-5" style={{ color: cardStyleF.iconColor }} />
                          </div>
                        : <StatusIcon estado={cot.estado} />
                      }
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="font-semibold" style={{ color: 'var(--text-primary)' }}>
                            {cot.es_formal ? 'COT' : 'PRE-COT'}-{cot.numero}
                          </span>
                          {cot.cliente && (
                            <span className="text-sm truncate" style={{ color: 'var(--text-muted)' }}>
                              · {cot.cliente}
                            </span>
                          )}
                          {cot.referencia && (
                            <span className="text-xs truncate" style={{ color: 'var(--text-faint)' }}>
                              ({cot.referencia})
                            </span>
                          )}
                          <FaseBadge fase={fase} />
                        </div>

                        <div className="flex items-center gap-4 mt-1 text-xs" style={{ color: 'var(--text-faint)' }}>
                          <span>{cot.total_items} partes</span>
                          {cot.estado === 'completado' && (
                            <>
                              <span className="text-green-400">{cot.items_encontrados} encontradas</span>
                              <span className="text-red-400/70">{cot.total_items - cot.items_encontrados} no encontradas</span>
                            </>
                          )}
                          <span>{new Date(cot.created_at).toLocaleDateString('es-CL')}</span>
                        </div>


                      </div>

                      {['procesando', 'pendiente', 'esperando_agente'].includes(cot.estado) && (
                        <div className="w-28 hidden sm:block shrink-0 pt-1">
                          <div className="flex justify-between text-xs mb-1" style={{ color: 'var(--text-faint)' }}>
                            <span>{cot.items_procesados}/{cot.total_items}</span>
                            <span>{cot.total_items > 0 ? Math.round((cot.items_procesados / cot.total_items) * 100) : 0}%</span>
                          </div>
                          <div className="h-1.5 bg-[var(--surface-300)] rounded-full overflow-hidden">
                            <div
                              className="h-full bg-brand-500 rounded-full transition-all duration-500"
                              style={{ width: `${cot.total_items > 0 ? (cot.items_procesados / cot.total_items) * 100 : 0}%` }}
                            />
                          </div>
                        </div>
                      )}

                      <div className="flex items-center gap-2 shrink-0">
                        {cot.estado === 'completado' && (
                          <button
                            onClick={(e) => { e.stopPropagation(); navigate(`/cotizaciones/${cot.id}/editor`) }}
                            className="btn-primary text-xs py-1.5 px-3"
                          >
                            <Calculator className="w-3.5 h-3.5" />
                            Cotizador
                          </button>
                        )}
                        {cot.tiene_resultado ? (
                          <button
                            onClick={(e) => handleDownloadExcel(e, cot.id, cot.numero)}
                            disabled={downloadingExcel === cot.id}
                            className="btn-secondary text-xs py-1.5 px-2"
                            title="Descargar Excel CAT"
                          >
                            {downloadingExcel === cot.id
                              ? <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                              : <Download className="w-3.5 h-3.5" />}
                          </button>
                        ) : (
                          <span
                            className="btn-secondary text-xs py-1.5 px-2 opacity-30 cursor-not-allowed"
                            title="Excel no disponible aún"
                          >
                            <Download className="w-3.5 h-3.5" />
                          </span>
                        )}
                        {cot.estado === 'completado' && (
                          <button
                            onClick={(e) => handleDownloadPdf(e, cot.id, cot.numero)}
                            disabled={downloadingPdf === cot.id}
                            className="btn-secondary text-xs py-1.5 px-2"
                            title="PDF cotización cliente"
                          >
                            {downloadingPdf === cot.id
                              ? <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                              : <FileText className="w-3.5 h-3.5" />}
                          </button>
                        )}
                        {cot.es_formal && (
                          <button
                            onClick={(e) => handleDownloadFormal(e, cot.id, cot.numero)}
                            disabled={downloadingFormal === cot.id}
                            className="btn-secondary text-xs py-1.5 px-2"
                            title="Descargar Excel formal"
                          >
                            {downloadingFormal === cot.id
                              ? <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                              : <FileSpreadsheet className="w-3.5 h-3.5" />}
                          </button>
                        )}
                        <button
                          title="Reprocesar partes no encontradas"
                          onClick={(e) => { e.stopPropagation(); reprocesarMutation.mutate(cot.id) }}
                          disabled={reprocesarMutation.isPending}
                          className="btn-secondary text-xs py-1.5 px-2"
                        >
                          <RotateCcw className={`w-3.5 h-3.5 ${reprocesarMutation.isPending ? 'animate-spin' : ''}`} />
                        </button>

                        {getFase(cot) === 'ingresada' && (
                          confirmDelete === cot.id ? (
                            <div className="flex items-center gap-1">
                              <span className="text-xs text-red-400">¿Eliminar?</span>
                              <button
                                onClick={() => eliminarMutation.mutate(cot.id)}
                                disabled={eliminarMutation.isPending}
                                className="text-xs px-2 py-1 rounded-lg bg-red-500/20 text-red-400 border border-red-500/30 hover:bg-red-500/30 transition-colors"
                              >Sí</button>
                              <button
                                onClick={() => setConfirmDelete(null)}
                                className="text-xs px-2 py-1 rounded-lg btn-secondary"
                              >No</button>
                            </div>
                          ) : (
                            <button
                              title="Eliminar cotización"
                              onClick={(e) => { e.stopPropagation(); setConfirmDelete(cot.id) }}
                              className="btn-secondary text-xs py-1.5 px-2 hover:text-red-400 hover:border-red-500/30 transition-colors"
                            >
                              <Trash2 className="w-3.5 h-3.5" />
                            </button>
                          )
                        )}

                        <button
                          onClick={() => setExpandedCot(expandedCot === cot.id ? null : cot.id)}
                          className="btn-secondary text-xs py-1.5 px-3"
                        >
                          <Eye className="w-3.5 h-3.5" />
                          <ChevronDown className={`w-3.5 h-3.5 transition-transform ${expandedCot === cot.id ? 'rotate-180' : ''}`} />
                        </button>
                      </div>
                    </div>

                    {expandedCot === cot.id && (
                      <div className="border-t" style={{ borderColor: 'var(--border)' }}>
                        <CotizacionTable cotizacionId={cot.id} />
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function StatusIcon({ estado }: { estado: string }) {
  const map: Record<string, { icon: any; cls: string; bg: string }> = {
    // FileSpreadsheet azul = scraping completado (NO confundir con fase "Validada" que es CheckCircle2 verde)
    completado:       { icon: FileSpreadsheet, cls: 'text-brand-400',              bg: 'bg-brand-500/10'  },
    procesando:       { icon: RefreshCw,       cls: 'text-amber-400 animate-spin', bg: 'bg-amber-500/10'  },
    pendiente:        { icon: Clock,           cls: 'text-slate-400',              bg: 'bg-slate-500/10'  },
    esperando_agente: { icon: Clock,           cls: 'text-slate-400',              bg: 'bg-slate-500/10'  },
    error:            { icon: AlertCircle,     cls: 'text-red-400',                bg: 'bg-red-500/10'    },
  }
  const { icon: Icon, cls, bg } = map[estado] || map.pendiente
  return (
    <div className={`w-9 h-9 rounded-xl ${bg} flex items-center justify-center shrink-0 mt-0.5`}>
      <Icon className={`w-4 h-4 ${cls}`} />
    </div>
  )
}
