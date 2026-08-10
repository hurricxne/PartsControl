import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { createPortal } from 'react-dom'
import { useSearchParams } from 'react-router-dom'
import {
  Package, CheckCircle2, AlertTriangle, Clock, ChevronDown, ChevronRight,
  Loader2, Camera, Trash2, X, Check, History, Inbox, RefreshCw,
  AlertOctagon, Archive, ImagePlus, FileText, Download, Search,
} from 'lucide-react'
import toast from 'react-hot-toast'
import api, { bodegaAPI } from '../services/api'

// ── Types ─────────────────────────────────────────────────────────────────────

interface EmbarqueResumen {
  id: number
  numero: string
  estado: string
  forwarder: string
  awb: string
  awb_numero: string | null
  fecha_llegada_est: string | null
  total_items: number
  recepcion_id: number | null
  recepcion_estado: string | null
  items_marcados: number
}

interface RecepcionItem {
  id: number
  embarque_item_id: number
  recepcion_item_id: number | null
  numero_parte: string
  descripcion: string
  marca: string
  cantidad: number
  peso_kg: number | null
  unit_price_usd: number | null
  qty_recibida: number
  qty_danada: number
  estado_recepcion: string | null
  observacion: string | null
  fotos: { id: number; url_foto: string }[]
}

interface Recepcion {
  id: number
  embarque_id: number
  embarque_numero: string
  estado: string
  fecha_inicio: string
  fecha_cierre: string | null
  observacion_general: string | null
  items: RecepcionItem[]
}

// ── Buscador de operador (contrato común, 2026-08-05) ─────────────────────────

interface MatchMotivo {
  campo: string
  valor: string
}

/** Fila de GET /bodega/items (vista de ítems nueva; Bodega solo listaba embarques). */
interface BuscarItemRow {
  item_cotizacion_id: number
  numero_parte: string
  descripcion: string
  marca: string
  cantidad: number
  estado_item: string
  qty_recibida: number | null
  qty_disponible: number
  cotizacion_id: number
  numero_cotizacion: string
  cliente: string | null
  numero_oc_cliente: string | null
  embarques: { numero: string | null; awb_numero: string | null }[]
  guias_nacionales: string[]
  match: MatchMotivo[]
}

interface BuscarResponse {
  items: BuscarItemRow[]
  total: number
  page: number
  page_size: number
  q_efectivo: string
  normalizado: boolean
}

const DEBOUNCE_BUSQUEDA_MS = 350   // este usuario escribe a tirones y con guantes
const MIN_CARACTERES_BUSQUEDA = 2  // por debajo no se llama al servidor
const PAGE_SIZE_INICIAL = 50
const PAGE_SIZE_MAX = 200          // tope del backend (le=200)

// Espejo (solo para RESALTAR y filtrar en cliente) de los prefijos que el
// backend descarta: la UI imprime "COT-2026-0001", la base guarda "2026-0001".
const PREFIJOS_UI = ['COT-', 'OC-', 'OCP-', 'EMB-', 'DSP-', 'N°', 'Nº', '#']

function variantesToken(tok: string): string[] {
  const variantes = [tok]
  const mayus = tok.toUpperCase()
  for (const pref of PREFIJOS_UI) {
    if (mayus.startsWith(pref.toUpperCase()) && tok.length > pref.length) {
      const resto = tok.slice(pref.length).trim()
      if (resto.length >= MIN_CARACTERES_BUSQUEDA) variantes.push(resto)
      break
    }
  }
  return variantes
}

function tokensDe(q: string): string[] {
  const limpio = q.trim().replace(/\s+/g, ' ').slice(0, 64)
  if (limpio.length < MIN_CARACTERES_BUSQUEDA) return []
  return limpio.split(' ').slice(0, 4)
}

/** Filtro EN CLIENTE de las pestañas de embarques (Activos/Historial), sobre lo
 *  ya cargado: numero, awb_numero y forwarder. Da el conteo cruzado gratis. */
function embarqueMatchea(e: EmbarqueResumen, tokens: string[]): boolean {
  if (tokens.length === 0) return true
  const campos = [e.numero, e.awb_numero, e.forwarder]
  return tokens.every(t =>
    variantesToken(t).some(v =>
      campos.some(c => (c || '').toLowerCase().includes(v.toLowerCase()))
    )
  )
}

/** Resalta el primer fragmento coincidente partiendo el string EN REACT —
 *  PROHIBIDO dangerouslySetInnerHTML (el repo tiene 0 usos: que siga así). */
function Resaltado({ texto, tokens }: { texto?: string | null; tokens: string[] }) {
  if (!texto || tokens.length === 0) return <>{texto ?? ''}</>
  const lower = texto.toLowerCase()
  let idx = -1
  let len = 0
  for (const t of tokens) {
    for (const v of variantesToken(t)) {
      const i = lower.indexOf(v.toLowerCase())
      if (i >= 0 && (idx === -1 || i < idx)) {
        idx = i
        len = v.length
      }
    }
  }
  if (idx < 0) return <>{texto}</>
  return (
    <>
      {texto.slice(0, idx)}
      <mark
        className="rounded px-0.5"
        style={{ backgroundColor: 'rgba(245, 158, 11, 0.35)', color: 'inherit' }}
      >
        {texto.slice(idx, idx + len)}
      </mark>
      {texto.slice(idx + len)}
    </>
  )
}

const MATCH_LABELS: Record<string, string> = {
  numero_parte: 'n° parte',
  numero_parte_colapsado: 'n° parte (sin guiones)',
  repuesto: 'repuesto',
  marca: 'marca',
  cotizacion: 'cotización',
  cliente: 'cliente',
  oc_cliente: 'OC cliente',
  embarque: 'embarque',
  awb: 'AWB',
  guia_nacional: 'guía prov.',
}

// Campos que la tabla de resultados YA muestra: la insignia lleva solo la
// etiqueta; para el resto lleva el VALOR ("embarque EMB-2026-0007").
const CAMPOS_VISIBLES_TABLA = new Set([
  'numero_parte', 'numero_parte_colapsado', 'repuesto', 'marca',
  'cotizacion', 'cliente', 'oc_cliente',
])

/** Insignia de motivo: por qué salió la fila. Máximo 2, luego "+N". */
function MatchBadges({ match }: { match?: MatchMotivo[] }) {
  if (!match || match.length === 0) return null
  const mostrar = match.slice(0, 2)
  const extra = match.length - mostrar.length
  return (
    <>
      {mostrar.map(m => (
        <span
          key={m.campo}
          className="text-[10px] px-1.5 py-0.5 rounded-full font-semibold bg-amber-500/15 text-amber-600 dark:text-amber-400 whitespace-nowrap"
          title={`Coincidió por ${MATCH_LABELS[m.campo] ?? m.campo}: ${m.valor}`}
        >
          {MATCH_LABELS[m.campo] ?? m.campo}
          {CAMPOS_VISIBLES_TABLA.has(m.campo) ? '' : ` ${m.valor}`}
        </span>
      ))}
      {extra > 0 && (
        <span className="text-[10px] px-1.5 py-0.5 rounded-full font-semibold bg-slate-500/15 text-slate-500">
          +{extra}
        </span>
      )}
    </>
  )
}

const ESTADO_ITEM_BUSCAR: Record<string, { label: string; color: string }> = {
  comprado: { label: 'Comprado', color: 'text-blue-400' },
  preparado: { label: 'Preparado', color: 'text-blue-400' },
  pre_embarcado: { label: 'Pre-embarcado', color: 'text-amber-400' },
  embarcado: { label: 'En tránsito', color: 'text-amber-400' },
  en_bodega: { label: 'En bodega', color: 'text-emerald-400' },
  reclamo_proveedor: { label: 'Reclamo', color: 'text-red-400' },
}

// ── Helpers ───────────────────────────────────────────────────────────────────

const ESTADOS = [
  { val: 'completo',             label: 'Completo',             color: 'text-emerald-400' },
  { val: 'faltante',             label: 'Faltante',             color: 'text-amber-400' },
  { val: 'sobrante',             label: 'Sobrante',             color: 'text-blue-400' },
  { val: 'danado_utilizable',    label: 'Dañado utilizable',    color: 'text-orange-400' },
  { val: 'danado_no_utilizable', label: 'Dañado no utilizable', color: 'text-red-400' },
  { val: 'no_llego',             label: 'No llegó',             color: 'text-red-400' },
]

function estadoLabel(val: string | null) {
  if (!val) return null
  return ESTADOS.find(e => e.val === val)
}

function isDanado(estado: string | null) {
  return estado === 'danado_utilizable' || estado === 'danado_no_utilizable'
}

function fmtDate(s: string | null) {
  if (!s) return '—'
  return new Date(s).toLocaleDateString('es-CL')
}

function fmtUsd(v: number | null) {
  if (v == null) return '—'
  return '$' + v.toLocaleString('en-US', { minimumFractionDigits: 2 })
}

// ── RecepcionItemRow ──────────────────────────────────────────────────────────

function RecepcionItemRow({
  item, recepcionId, onUpdate,
}: {
  item: RecepcionItem
  recepcionId: number
  onUpdate: () => void
}) {
  const [estado, setEstado]     = useState(item.estado_recepcion || '')
  const [qtyRec, setQtyRec]     = useState(String(item.qty_recibida || item.cantidad))
  const [qtyDan, setQtyDan]     = useState(String(item.qty_danada || 0))
  const [obs, setObs]           = useState(item.observacion || '')
  const [saving, setSaving]     = useState(false)
  const [uploading, setUploading] = useState(false)
  const [fotos, setFotos]       = useState(item.fotos || [])
  const [expanded, setExpanded] = useState(!!item.estado_recepcion)

  const needsFoto = isDanado(estado) && fotos.length === 0

  const save = async () => {
    if (!estado) { toast.error('Selecciona un estado primero'); return }
    if (needsFoto) { toast.error('Debes subir al menos 1 foto para ítems dañados'); return }
    setSaving(true)
    try {
      await bodegaAPI.marcarItem(recepcionId, item.id, {
        embarque_item_id: item.embarque_item_id,
        estado_recepcion: estado,
        qty_recibida: parseInt(qtyRec) || 0,
        qty_danada: parseInt(qtyDan) || 0,
        observacion: obs || null,
      })
      toast.success('Ítem guardado')
      onUpdate()
    } catch (e: any) {
      toast.error(e.response?.data?.detail || 'Error al guardar ítem')
    } finally {
      setSaving(false)
    }
  }

  const handleFoto = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    setUploading(true)
    try {
      const { data } = await bodegaAPI.subirFoto(recepcionId, item.id, file)
      setFotos(prev => [...prev, { id: data.foto_id, url_foto: data.url }])
      toast.success('Foto subida')
    } catch {
      toast.error('Error al subir foto')
    } finally {
      setUploading(false)
      e.target.value = ''
    }
  }

  const handleDeleteFoto = async (fotoId: number) => {
    try {
      await bodegaAPI.eliminarFoto(recepcionId, item.id, fotoId)
      setFotos(prev => prev.filter(f => f.id !== fotoId))
      toast.success('Foto eliminada')
    } catch {
      toast.error('Error al eliminar foto')
    }
  }

  const estInfo = estadoLabel(estado)
  const isSaved = !!item.estado_recepcion

  return (
    <div className={`rounded-xl border transition-all ${isSaved ? 'border-emerald-500/30 bg-emerald-500/5' : 'border-[var(--border)] bg-[var(--surface-50)]'}`}>
      {/* Row header */}
      <div className="flex items-center gap-3 p-3 cursor-pointer" onClick={() => setExpanded(v => !v)}>
        <div className={`w-7 h-7 rounded-lg flex items-center justify-center shrink-0 ${isSaved ? 'bg-emerald-500/20' : 'bg-[var(--surface-200)]'}`}>
          {isSaved ? <CheckCircle2 className="w-4 h-4 text-emerald-400" /> : <Package className="w-4 h-4" style={{ color: 'var(--text-faint)' }} />}
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-mono text-xs font-bold text-brand-400">{item.numero_parte || '—'}</span>
            {estInfo && <span className={`text-[10px] font-semibold ${estInfo.color}`}>{estInfo.label}</span>}
            {needsFoto && <span className="text-[10px] text-red-400 flex items-center gap-0.5"><Camera className="w-3 h-3" />foto requerida</span>}
          </div>
          <p className="text-xs truncate" style={{ color: 'var(--text-muted)' }}>{item.descripcion}</p>
        </div>
        <div className="flex items-center gap-2 shrink-0 text-xs" style={{ color: 'var(--text-faint)' }}>
          <span>×{item.cantidad}</span>
          {item.unit_price_usd && <span className="font-mono text-emerald-400">{fmtUsd(item.unit_price_usd)}</span>}
          {expanded ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
        </div>
      </div>

      {/* Expanded form */}
      {expanded && (
        <div className="px-4 pb-4 space-y-3 border-t" style={{ borderColor: 'var(--border)' }}>
          {/* Estado selector */}
          <div>
            <label className="text-[10px] uppercase tracking-wider font-semibold mb-1.5 block" style={{ color: 'var(--text-faint)' }}>Estado recepción</label>
            <div className="flex flex-wrap gap-1.5">
              {ESTADOS.map(e => (
                <button key={e.val} onClick={() => setEstado(e.val)}
                  className={`text-[11px] px-2.5 py-1 rounded-lg border transition-all font-medium ${estado === e.val ? 'border-brand-400 bg-brand-500/10 text-brand-400' : 'border-[var(--border)] hover:border-[var(--border-hover)]'} ${e.color}`}>
                  {e.label}
                </button>
              ))}
            </div>
          </div>

          {/* Quantities */}
          <div className="flex gap-3">
            <div>
              <label className="text-[10px] uppercase tracking-wider font-semibold mb-1 block" style={{ color: 'var(--text-faint)' }}>Qty recibida</label>
              <input type="number" min={0} max={item.cantidad * 2} value={qtyRec}
                onChange={e => setQtyRec(e.target.value)}
                className="input w-20 text-center text-sm px-2 py-1" />
            </div>
            {isDanado(estado) && (
              <div>
                <label className="text-[10px] uppercase tracking-wider font-semibold mb-1 block" style={{ color: 'var(--text-faint)' }}>Qty dañada</label>
                <input type="number" min={0} max={item.cantidad} value={qtyDan}
                  onChange={e => setQtyDan(e.target.value)}
                  className="input w-20 text-center text-sm px-2 py-1" />
              </div>
            )}
          </div>

          {/* Observacion */}
          <div>
            <label className="text-[10px] uppercase tracking-wider font-semibold mb-1 block" style={{ color: 'var(--text-faint)' }}>Observación</label>
            <textarea value={obs} onChange={e => setObs(e.target.value)} rows={2}
              className="input w-full text-xs px-2 py-1.5 resize-none" placeholder="Opcional…" />
          </div>

          {/* Photos */}
          {isDanado(estado) && (
            <div>
              <div className="flex items-center gap-2 mb-2">
                <label className="text-[10px] uppercase tracking-wider font-semibold" style={{ color: 'var(--text-faint)' }}>
                  Fotos {needsFoto && <span className="text-red-400">(obligatorio)</span>}
                </label>
                <label className={`cursor-pointer flex items-center gap-1 text-[11px] px-2 py-0.5 rounded border transition-colors ${needsFoto ? 'border-red-400 text-red-400 hover:bg-red-400/10' : 'border-[var(--border)] hover:border-brand-400 text-brand-400'}`}>
                  {uploading ? <Loader2 className="w-3 h-3 animate-spin" /> : <ImagePlus className="w-3 h-3" />}
                  Agregar foto
                  <input type="file" accept="image/*" onChange={handleFoto} className="hidden" />
                </label>
              </div>
              {fotos.length > 0 && (
                <div className="flex flex-wrap gap-2">
                  {fotos.map(f => (
                    <div key={f.id} className="relative group">
                      <img src={f.url_foto} alt="foto" className="w-16 h-16 object-cover rounded-lg border" style={{ borderColor: 'var(--border)' }} />
                      <button onClick={() => handleDeleteFoto(f.id)}
                        className="absolute -top-1 -right-1 w-5 h-5 rounded-full bg-red-500 text-white flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity">
                        <X className="w-3 h-3" />
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* Save button */}
          <div className="flex justify-end">
            <button onClick={save} disabled={saving || !estado || needsFoto}
              className="btn-primary text-xs px-4 py-1.5 flex items-center gap-1.5 disabled:opacity-50">
              {saving ? <Loader2 className="w-3 h-3 animate-spin" /> : <Check className="w-3 h-3" />}
              {isSaved ? 'Actualizar' : 'Guardar'}
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

// ── RecepcionPanel ────────────────────────────────────────────────────────────

function RecepcionPanel({ recepcionId, onClose, onFinish }: {
  recepcionId: number
  onClose: () => void
  onFinish: () => void
}) {
  const [rec, setRec]               = useState<Recepcion | null>(null)
  const [loading, setLoading]       = useState(true)
  const [closing, setClosing]       = useState(false)
  const [obsGeneral, setObsGeneral] = useState('')
  const [confirmClose, setConfirmClose] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const { data } = await bodegaAPI.getRecepcion(recepcionId)
      setRec(data)
      setObsGeneral(data.observacion_general || '')
    } catch {
      toast.error('Error al cargar recepción')
    } finally {
      setLoading(false)
    }
  }, [recepcionId])

  useEffect(() => { load() }, [load])

  const marcados = rec?.items.filter(i => i.estado_recepcion).length ?? 0
  const total    = rec?.items.length ?? 0
  const pct      = total > 0 ? Math.round((marcados / total) * 100) : 0
  const allDone  = marcados === total && total > 0

  const handleCerrar = async () => {
    setClosing(true)
    try {
      await bodegaAPI.cerrarRecepcion(recepcionId, obsGeneral || undefined)
      toast.success('Recepción cerrada correctamente')
      onFinish()
    } catch (e: any) {
      toast.error(e.response?.data?.detail || 'Error al cerrar recepción')
    } finally {
      setClosing(false)
      setConfirmClose(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-[9999] flex items-start justify-center p-4 pt-10 md:pt-16"
      style={{ backgroundColor: 'rgba(0,0,0,0.65)', backdropFilter: 'blur(4px)' }}
      onClick={e => { if (e.target === e.currentTarget) onClose() }}
    >
      <div
        className="relative w-full max-w-2xl max-h-[88vh] flex flex-col rounded-2xl border shadow-2xl overflow-hidden"
        style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}
      >
        {/* Header */}
        <div className="flex items-center gap-3 px-4 py-3 border-b shrink-0"
          style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
          <div className="flex-1 min-w-0">
            <h2 className="font-bold text-sm leading-tight" style={{ color: 'var(--text-primary)' }}>
              {rec ? rec.embarque_numero : `Recepción #${recepcionId}`}
            </h2>
            <p className="text-xs mt-0.5" style={{ color: 'var(--text-muted)' }}>
              {marcados}/{total} ítems marcados
            </p>
          </div>
          {/* Progress bar */}
          <div className="w-20 shrink-0">
            <div className="h-1.5 rounded-full bg-[var(--surface-300)] overflow-hidden">
              <div className="h-full rounded-full bg-brand-500 transition-all duration-300"
                style={{ width: `${pct}%` }} />
            </div>
            <p className="text-[10px] text-center mt-0.5" style={{ color: 'var(--text-faint)' }}>{pct}%</p>
          </div>
          <button onClick={onClose}
            title="Salir sin cerrar recepción"
            className="p-1.5 rounded-lg hover:bg-[var(--surface-300)] transition-colors shrink-0"
            style={{ color: 'var(--text-muted)' }}>
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Items list — scrollable */}
        <div className="flex-1 overflow-y-auto px-4 py-4 space-y-2">
          {loading ? (
            <div className="flex items-center justify-center py-16">
              <Loader2 className="w-6 h-6 animate-spin" style={{ color: 'var(--text-faint)' }} />
            </div>
          ) : (
            rec?.items.map(item => (
              <RecepcionItemRow key={item.id} item={item} recepcionId={recepcionId} onUpdate={load} />
            ))
          )}
        </div>

        {/* Footer */}
        <div className="shrink-0 border-t px-4 py-3 space-y-2.5" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
          <textarea value={obsGeneral} onChange={e => setObsGeneral(e.target.value)}
            rows={2} placeholder="Observación general de la recepción (opcional)…"
            className="input w-full text-xs px-2.5 py-1.5 resize-none" />
          <div className="flex items-center gap-3">
            {!allDone && (
              <p className="text-xs text-amber-400 flex-1">
                ⚠ {total - marcados} ítem(s) sin marcar
              </p>
            )}
            <button onClick={() => setConfirmClose(true)} disabled={closing || total === 0}
              className={`ml-auto btn-primary text-sm px-5 py-2 flex items-center gap-2 disabled:opacity-50 ${!allDone ? 'opacity-80' : ''}`}>
              {closing ? <Loader2 className="w-4 h-4 animate-spin" /> : <Archive className="w-4 h-4" />}
              Cerrar recepción
            </button>
          </div>
        </div>

        {/* Confirm close — absolute inside modal */}
        {confirmClose && (
          <div className="absolute inset-0 z-10 flex items-center justify-center rounded-2xl"
            style={{ backgroundColor: 'rgba(0,0,0,0.6)', backdropFilter: 'blur(2px)' }}>
            <div className="w-full max-w-sm rounded-2xl border shadow-2xl p-5 space-y-4 mx-4"
              style={{ backgroundColor: 'var(--surface-200)', borderColor: 'var(--border)' }}>
              <div className="flex items-center gap-2">
                <AlertTriangle className="w-5 h-5 text-amber-400 shrink-0" />
                <h3 className="font-bold" style={{ color: 'var(--text-primary)' }}>¿Cerrar recepción?</h3>
              </div>
              {!allDone && (
                <p className="text-sm text-amber-400">
                  Hay {total - marcados} ítem(s) sin marcar. Se generarán reclamos automáticamente para los faltantes/no llegados.
                </p>
              )}
              <p className="text-sm" style={{ color: 'var(--text-muted)' }}>
                Esta acción es irreversible. Los ítems completos pasarán a estado <strong>en bodega</strong>.
              </p>
              <div className="flex gap-2 justify-end">
                <button onClick={() => setConfirmClose(false)} disabled={closing}
                  className="px-4 py-2 rounded-xl text-sm border transition-colors hover:bg-[var(--surface-300)]"
                  style={{ borderColor: 'var(--border)', color: 'var(--text-muted)' }}>
                  Cancelar
                </button>
                <button onClick={handleCerrar} disabled={closing}
                  className="btn-primary px-4 py-2 text-sm flex items-center gap-1.5">
                  {closing ? <Loader2 className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />}
                  Confirmar cierre
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

// ── EmbarqueCard ──────────────────────────────────────────────────────────────

function EmbarqueCard({ emb, onRefresh, onOpenRec, tokens = [] }: {
  emb: EmbarqueResumen
  onRefresh: () => void
  onOpenRec: (recId: number) => void
  tokens?: string[]
}) {
  const [starting, setStarting] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const [detail, setDetail] = useState<any>(null)
  const [loadingDet, setLoadingDet] = useState(false)

  const toggleDetail = async () => {
    const next = !expanded
    setExpanded(next)
    if (next && !detail) {
      setLoadingDet(true)
      try { const { data } = await bodegaAPI.getEmbarque(emb.id); setDetail(data) }
      catch { toast.error('No se pudo cargar el detalle') }
      finally { setLoadingDet(false) }
    }
  }

  const esDoc = (v?: string | null) => !!v && /^[A-Za-z0-9]{16,}\.\w+$/.test(v)
  const descargarDoc = async (filename: string) => {
    try {
      const resp = await api.get(`/despachos/docs/${filename}`, { responseType: 'arraybuffer' })
      const blob = new Blob([resp.data])
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url; a.download = filename
      document.body.appendChild(a); a.click(); document.body.removeChild(a)
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch { toast.error('No se pudo descargar el documento') }
  }

  const handleRecibir = async () => {
    setStarting(true)
    try {
      const { data } = await bodegaAPI.iniciarRecepcion(emb.id)
      onOpenRec(data.recepcion_id)
      toast.success('Recepción iniciada')
      onRefresh()
    } catch (e: any) {
      toast.error(e.response?.data?.detail || 'Error al iniciar recepción')
    } finally {
      setStarting(false)
    }
  }

  const EST_LABELS: Record<string, string> = { en_transito: 'En tránsito', en_aduana: 'En aduana', en_bodega: 'En bodega', en_recepcion: 'En recepción' }
  const estadoColor = emb.estado === 'en_recepcion' ? 'text-amber-400' : emb.estado === 'en_bodega' ? 'text-emerald-400' : 'text-blue-400'
  const estadoLabel = EST_LABELS[emb.estado] ?? 'En tránsito'
  const pct = emb.total_items > 0 ? Math.round((emb.items_marcados / emb.total_items) * 100) : 0

  return (
    <>
      <div className="rounded-2xl border p-4 space-y-3 transition-shadow hover:shadow-md"
        style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
        <div className="flex items-start gap-3">
          <div className="w-10 h-10 rounded-xl bg-brand-500/10 flex items-center justify-center shrink-0">
            <Package className="w-5 h-5 text-brand-400" />
          </div>
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="font-mono font-bold text-sm" style={{ color: 'var(--text-primary)' }}>
                <Resaltado texto={emb.numero || `EMB-${emb.id}`} tokens={tokens} />
              </span>
              <span className={`text-[11px] font-semibold ${estadoColor}`}>{estadoLabel}</span>
            </div>
            <div className="flex items-center gap-3 mt-0.5 flex-wrap text-xs" style={{ color: 'var(--text-muted)' }}>
              {emb.forwarder && <span><Resaltado texto={emb.forwarder} tokens={tokens} /></span>}
              {emb.awb_numero && <span className="font-mono">N° AWB: <Resaltado texto={emb.awb_numero} tokens={tokens} /></span>}
              {emb.awb && <span className="font-mono">AWB: {emb.awb}</span>}
              {emb.fecha_llegada_est && <span>ETA: {fmtDate(emb.fecha_llegada_est)}</span>}
            </div>
          </div>
          <div className="text-xs shrink-0" style={{ color: 'var(--text-faint)' }}>
            {emb.total_items} ítems
          </div>
        </div>

        {/* Progress for en_recepcion */}
        {emb.estado === 'en_recepcion' && emb.total_items > 0 && (
          <div>
            <div className="flex justify-between text-[10px] mb-1" style={{ color: 'var(--text-faint)' }}>
              <span>{emb.items_marcados}/{emb.total_items} marcados</span>
              <span>{pct}%</span>
            </div>
            <div className="h-1.5 rounded-full bg-[var(--surface-200)] overflow-hidden">
              <div className="h-full rounded-full bg-brand-500 transition-all" style={{ width: `${pct}%` }} />
            </div>
          </div>
        )}

        {/* Actions */}
        <div className="flex justify-between items-center gap-2">
          <button onClick={toggleDetail} className="text-xs flex items-center gap-1 hover:underline" style={{ color: 'var(--text-muted)' }}>
            {expanded ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
            {expanded ? 'Ocultar detalle' : 'Ver detalle'}
          </button>
          <div className="flex gap-2">
            {['en_transito', 'en_aduana', 'en_bodega'].includes(emb.estado) && (
              <button onClick={handleRecibir} disabled={starting}
                className="btn-primary text-xs px-4 py-1.5 flex items-center gap-1.5">
                {starting ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Inbox className="w-3.5 h-3.5" />}
                Recibir embarque
              </button>
            )}
            {emb.estado === 'en_recepcion' && emb.recepcion_id && (
              <button onClick={() => onOpenRec(emb.recepcion_id!)}
                className="btn-primary text-xs px-4 py-1.5 flex items-center gap-1.5">
                <CheckCircle2 className="w-3.5 h-3.5" />
                Ver recepción activa
              </button>
            )}
          </div>
        </div>

        {/* Detalle expandible: documentos, notas, ítems */}
        {expanded && (
          <div className="pt-3 mt-1 border-t space-y-3" style={{ borderColor: 'var(--border)' }}>
            {loadingDet ? (
              <div className="flex items-center gap-2 text-xs" style={{ color: 'var(--text-faint)' }}>
                <Loader2 className="w-3.5 h-3.5 animate-spin" /> Cargando detalle…
              </div>
            ) : detail ? (
              <>
                <div>
                  <p className="text-[10px] uppercase tracking-wider font-semibold mb-1.5" style={{ color: 'var(--text-faint)' }}>Documentos</p>
                  <div className="flex flex-wrap gap-2">
                    {([['AWB / BL', detail.awb], ['Factura comercial', detail.factura_comercial], ['Packing list', detail.packing_list]] as [string, string | null][]).map(([lbl, val]) =>
                      val ? (esDoc(val) ? (
                        <button key={lbl} onClick={() => descargarDoc(val)} className="text-[11px] flex items-center gap-1 px-2 py-1 rounded-lg border hover:border-brand-400 transition-colors" style={{ borderColor: 'var(--border)', color: 'var(--text-primary)' }}>
                          <FileText className="w-3 h-3 text-brand-400" /> {lbl} <Download className="w-3 h-3" style={{ color: 'var(--text-faint)' }} />
                        </button>
                      ) : (
                        <span key={lbl} className="text-[11px] px-2 py-1 rounded-lg border" style={{ borderColor: 'var(--border)', color: 'var(--text-muted)' }}>{lbl}: {val}</span>
                      )) : null
                    )}
                    {!detail.awb && !detail.factura_comercial && !detail.packing_list && (
                      <span className="text-[11px]" style={{ color: 'var(--text-faint)' }}>Sin documentos</span>
                    )}
                  </div>
                </div>

                {detail.notas && (
                  <div>
                    <p className="text-[10px] uppercase tracking-wider font-semibold mb-1" style={{ color: 'var(--text-faint)' }}>Notas</p>
                    <p className="text-xs whitespace-pre-wrap" style={{ color: 'var(--text-muted)' }}>{detail.notas}</p>
                  </div>
                )}

                <div>
                  <p className="text-[10px] uppercase tracking-wider font-semibold mb-1.5" style={{ color: 'var(--text-faint)' }}>Ítems ({detail.items?.length ?? 0})</p>
                  <div className="overflow-x-auto">
                    <table className="w-full text-[11px]" style={{ color: 'var(--text-muted)' }}>
                      <thead>
                        <tr className="text-left" style={{ color: 'var(--text-faint)' }}>
                          <th className="py-1 pr-3">N° Parte</th>
                          <th className="py-1 pr-3">Descripción</th>
                          <th className="py-1 pr-3">Marca</th>
                          <th className="py-1 pr-3 text-right">Cant.</th>
                          <th className="py-1 pr-3 text-right">Peso kg</th>
                          <th className="py-1 pr-3 text-right">Unit USD</th>
                          <th className="py-1 pr-3">OC Cliente</th>
                          <th className="py-1 pr-3">Invoice</th>
                          <th className="py-1 pr-3">OCP</th>
                        </tr>
                      </thead>
                      <tbody>
                        {(detail.items ?? []).map((it: any, idx: number) => (
                          <tr key={idx} style={{ borderTop: '1px solid var(--border)' }}>
                            <td className="py-1 pr-3 font-mono" style={{ color: 'var(--text-primary)' }}>{it.numero_parte || '—'}</td>
                            <td className="py-1 pr-3">{it.descripcion || '—'}</td>
                            <td className="py-1 pr-3">{it.marca || '—'}</td>
                            <td className="py-1 pr-3 text-right">{it.cantidad}</td>
                            <td className="py-1 pr-3 text-right">{it.peso_kg ?? '—'}</td>
                            <td className="py-1 pr-3 text-right">{it.unit_price_usd != null ? `$${it.unit_price_usd}` : '—'}</td>
                            <td className="py-1 pr-3">{it.numero_oc_cliente || '—'}</td>
                            <td className="py-1 pr-3">{it.invoice_no || '—'}</td>
                            <td className="py-1 pr-3">{it.ocp_numero_oc || it.ocp_numero || '—'}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              </>
            ) : null}
          </div>
        )}
      </div>

    </>
  )
}

// ── Historial panel ───────────────────────────────────────────────────────────
// Los datos viven en la página (se cargan una sola vez): así el buscador puede
// mostrar el conteo cruzado en la etiqueta de la pestaña sin pedirlos de nuevo.

function HistorialTab({ items, loading, q, tokens }: {
  items: EmbarqueResumen[]
  loading: boolean
  q: string
  tokens: string[]
}) {
  if (loading) return (
    <div className="flex items-center justify-center py-16">
      <Loader2 className="w-6 h-6 animate-spin" style={{ color: 'var(--text-faint)' }} />
    </div>
  )
  // Vacíos DIFERENCIADOS: "no coincide nada" ≠ "no hay nada cargado"
  if (!items.length) return (
    q ? (
      <p className="text-center py-12 text-sm" style={{ color: 'var(--text-faint)' }}>
        Sin resultados para «{q}» en Historial.
      </p>
    ) : (
      <p className="text-center py-12 text-sm" style={{ color: 'var(--text-faint)' }}>Sin embarques recibidos aún</p>
    )
  )

  return (
    <div className="space-y-3">
      {items.map(emb => (
        <div key={emb.id} className="rounded-2xl border p-4" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-xl bg-emerald-500/10 flex items-center justify-center shrink-0">
              <CheckCircle2 className="w-4.5 h-4.5 text-emerald-400" />
            </div>
            <div className="flex-1 min-w-0">
              <span className="font-mono font-bold text-sm" style={{ color: 'var(--text-primary)' }}>
                <Resaltado texto={emb.numero || `EMB-${emb.id}`} tokens={tokens} />
              </span>
              <div className="text-xs mt-0.5 flex gap-3 flex-wrap" style={{ color: 'var(--text-muted)' }}>
                {emb.awb_numero && <span className="font-mono">N° AWB: <Resaltado texto={emb.awb_numero} tokens={tokens} /></span>}
                {emb.awb && <span className="font-mono">AWB: {emb.awb}</span>}
                <span>{emb.total_items} ítems</span>
              </div>
            </div>
            <span className="text-[11px] font-semibold text-emerald-400">Recibido</span>
          </div>
        </div>
      ))}
    </div>
  )
}

// ── Buscar ítem (pestaña nueva: vista de ÍTEMS, servida por /bodega/items) ────

function BuscarItemsTab({ resp, buscando, q, pageSize, tokens, onVerMas, nActivos, nHistorial, irA }: {
  resp: BuscarResponse | null
  buscando: boolean
  q: string
  tokens: string[]
  pageSize: number
  onVerMas: () => void
  nActivos: number
  nHistorial: number
  irA: (t: 'activos' | 'historial' | 'buscar') => void
}) {
  if (!q) {
    return (
      <p className="text-center py-12 text-sm" style={{ color: 'var(--text-faint)' }}>
        Escribe al menos {MIN_CARACTERES_BUSQUEDA} caracteres para buscar un ítem
        por n° de parte, repuesto, COT, OC, cliente, embarque, guía o AWB.
      </p>
    )
  }
  if (buscando && !resp) {
    return (
      <div className="flex items-center justify-center py-16">
        <Loader2 className="w-6 h-6 animate-spin" style={{ color: 'var(--text-faint)' }} />
      </div>
    )
  }
  if (!resp) return null
  if (resp.items.length === 0) {
    return (
      <div className="text-center py-12 space-y-2">
        <p className="text-sm" style={{ color: 'var(--text-faint)' }}>
          Sin resultados para «{q}» en Buscar ítem.
        </p>
        {(nActivos > 0 || nHistorial > 0) && (
          <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
            Hay{' '}
            <button onClick={() => irA('activos')} className="underline text-brand-400 font-semibold">
              {nActivos} embarque(s) en Activos
            </button>
            {' '}·{' '}
            <button onClick={() => irA('historial')} className="underline text-brand-400 font-semibold">
              {nHistorial} en Historial
            </button>
          </p>
        )}
        <p className="text-xs" style={{ color: 'var(--text-faint)' }}>
          Los ítems recibidos en Chile (compra nacional) no tienen N° de embarque:
          buscá por el N° de guía del proveedor.
        </p>
      </div>
    )
  }
  return (
    <div className="space-y-2">
      {/* Encabezado HONESTO: nunca truncar en silencio */}
      <div className="flex items-center gap-2 text-xs px-1" style={{ color: 'var(--text-muted)' }}>
        <span>
          Mostrando {resp.items.length} de {resp.total} coincidencia{resp.total === 1 ? '' : 's'}
          {resp.total > resp.items.length ? ' — afiná la búsqueda' : ''}
        </span>
        {buscando && <Loader2 className="w-3 h-3 animate-spin" />}
      </div>
      {resp.normalizado && (
        <div className="text-xs px-1" style={{ color: 'var(--text-faint)' }}>
          Buscaste «{q}»; también busqué «{q.replace(/[-\s]/g, '')}» (sin guiones).
        </div>
      )}
      <div className="rounded-2xl border overflow-x-auto"
        style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
        <table className="w-full text-[11px]" style={{ color: 'var(--text-muted)' }}>
          <thead>
            <tr className="text-left" style={{ color: 'var(--text-faint)' }}>
              <th className="py-2 px-3">N° Parte</th>
              <th className="py-2 px-3">Repuesto</th>
              <th className="py-2 px-3">Marca</th>
              <th className="py-2 px-3 text-right">Cant.</th>
              <th className="py-2 px-3 text-right">Disp.</th>
              <th className="py-2 px-3">Estado</th>
              <th className="py-2 px-3">COT</th>
              <th className="py-2 px-3">Cliente</th>
              <th className="py-2 px-3">OC cliente</th>
              <th className="py-2 px-3">Embarque / Guía</th>
              <th className="py-2 px-3">Motivo</th>
            </tr>
          </thead>
          <tbody>
            {resp.items.map(it => {
              const est = ESTADO_ITEM_BUSCAR[it.estado_item] ?? { label: it.estado_item, color: '' }
              return (
                <tr key={it.item_cotizacion_id} style={{ borderTop: '1px solid var(--border)' }}>
                  <td className="py-1.5 px-3 font-mono" style={{ color: 'var(--text-primary)' }}>
                    <Resaltado texto={it.numero_parte || '—'} tokens={tokens} />
                  </td>
                  <td className="py-1.5 px-3">
                    <Resaltado texto={it.descripcion || '—'} tokens={tokens} />
                  </td>
                  <td className="py-1.5 px-3">
                    <Resaltado texto={it.marca || '—'} tokens={tokens} />
                  </td>
                  <td className="py-1.5 px-3 text-right">{it.cantidad}</td>
                  <td className="py-1.5 px-3 text-right">
                    {it.estado_item === 'en_bodega' ? (
                      <span className={it.qty_disponible > 0 ? 'text-emerald-400 font-semibold' : ''}>
                        {it.qty_disponible}
                      </span>
                    ) : '—'}
                  </td>
                  <td className={`py-1.5 px-3 font-semibold ${est.color}`}>{est.label}</td>
                  <td className="py-1.5 px-3 font-mono">
                    <Resaltado texto={it.numero_cotizacion} tokens={tokens} />
                  </td>
                  <td className="py-1.5 px-3">
                    <Resaltado texto={it.cliente || '—'} tokens={tokens} />
                  </td>
                  <td className="py-1.5 px-3 font-mono">
                    <Resaltado texto={it.numero_oc_cliente || '—'} tokens={tokens} />
                  </td>
                  <td className="py-1.5 px-3 font-mono">
                    {it.embarques.length > 0 ? (
                      <Resaltado
                        texto={it.embarques
                          .map(e => e.numero || e.awb_numero)
                          .filter(Boolean)
                          .join(', ')}
                        tokens={tokens}
                      />
                    ) : it.guias_nacionales.length > 0 ? (
                      // El ítem NACIONAL no tiene N° de embarque; se dice, no se inventa.
                      <span title="Compra nacional: no pasa por embarques">
                        Guía prov. <Resaltado texto={it.guias_nacionales.join(', ')} tokens={tokens} /> (nacional)
                      </span>
                    ) : '—'}
                  </td>
                  <td className="py-1.5 px-3">
                    <div className="flex flex-wrap gap-1">
                      <MatchBadges match={it.match} />
                    </div>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      {/* Nada de paginador numérico: el operador no navega páginas, achica */}
      {resp.total > resp.items.length && pageSize < PAGE_SIZE_MAX && (
        <button
          onClick={onVerMas}
          className="w-full py-2 text-sm rounded-xl border hover:bg-[var(--surface-200)] transition-colors"
          style={{ borderColor: 'var(--border)', color: 'var(--text-muted)' }}
        >
          Ver más (+50)
        </button>
      )}
      {resp.total > PAGE_SIZE_MAX && pageSize >= PAGE_SIZE_MAX && (
        <div className="text-xs text-center py-2" style={{ color: 'var(--text-faint)' }}>
          Demasiadas coincidencias. Agregá el N° de cotización o el cliente.
        </div>
      )}
    </div>
  )
}

// ── Main Page ─────────────────────────────────────────────────────────────────

type Tab = 'activos' | 'historial' | 'buscar'

export default function BodegaPage() {
  // El estado del buscador vive en la URL (?q=&tab=), no en localStorage: la
  // recarga no borra el término y "andá a buscar esto" pasa a ser un enlace.
  const [searchParams, setSearchParams] = useSearchParams()
  const tabParam = searchParams.get('tab')
  const [tab, setTabState] = useState<Tab>(
    tabParam === 'historial' || tabParam === 'buscar' ? tabParam : 'activos'
  )
  const [q, setQ] = useState(searchParams.get('q') ?? '')
  const [qDebounced, setQDebounced] = useState((searchParams.get('q') ?? '').trim())
  const [embarques, setEmbarques] = useState<EmbarqueResumen[]>([])
  const [loading, setLoading]   = useState(true)
  const [historial, setHistorial] = useState<EmbarqueResumen[]>([])
  const [loadingHist, setLoadingHist] = useState(true)
  const [activeRec, setActiveRec] = useState<number | null>(null)
  // Buscador de ítems en el SERVIDOR (pestaña "Buscar ítem")
  const [resultados, setResultados] = useState<BuscarResponse | null>(null)
  const [buscando, setBuscando] = useState(false)
  const [pageSize, setPageSize] = useState(PAGE_SIZE_INICIAL)
  const seqRef = useRef(0)

  const load = useCallback(() => {
    setLoading(true)
    bodegaAPI.listEmbarques()
      .then(r => setEmbarques(r.data))
      .catch(() => toast.error('Error al cargar embarques'))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load() }, [load])

  const loadHistorial = useCallback(() => {
    setLoadingHist(true)
    bodegaAPI.historialEmbarques()
      .then(r => setHistorial(r.data))
      .catch(() => toast.error('Error al cargar historial'))
      .finally(() => setLoadingHist(false))
  }, [])

  useEffect(() => { loadHistorial() }, [loadHistorial])

  // Debounce 350 ms del término (Enter lo saltea, ver onKeyDown de la caja).
  useEffect(() => {
    const t = setTimeout(() => setQDebounced(q.trim()), DEBOUNCE_BUSQUEDA_MS)
    return () => clearTimeout(t)
  }, [q])

  // replaceState mientras se escribe: Atrás no retrocede carácter por carácter.
  // Guardia anti-bucle: setSearchParams cambia de identidad en cada navegación
  // (react-router 6), así que solo se navega si la URL realmente difiere.
  useEffect(() => {
    if ((searchParams.get('q') ?? '') === q) return
    setSearchParams(
      prev => {
        const p = new URLSearchParams(prev)
        if (q) p.set('q', q)
        else p.delete('q')
        return p
      },
      { replace: true }
    )
  }, [q, searchParams, setSearchParams])

  // Mínimo 2 caracteres: por debajo no se filtra ni se llama al servidor.
  const qEfectivo = qDebounced.length >= MIN_CARACTERES_BUSQUEDA ? qDebounced : ''
  const qTokens = useMemo(() => tokensDe(qEfectivo), [qEfectivo])

  useEffect(() => { setPageSize(PAGE_SIZE_INICIAL) }, [qEfectivo])

  const setTab = (t: Tab) => {
    setTabState(t)
    // push (no replace) al cambiar de pestaña: Atrás vuelve a la anterior.
    setSearchParams(prev => {
      const p = new URLSearchParams(prev)
      if (t === 'activos') p.delete('tab')
      else p.set('tab', t)
      return p
    })
  }

  // Búsqueda en el servidor: corre SIEMPRE que haya término, aunque la pestaña
  // activa sea otra — el conteo cruzado "Buscar ítem (N)" es la respuesta real a
  // "¿dónde está esta parte?". Guardia de secuencia con id monótono: una
  // respuesta lenta y vieja no debe pisar a la vigente (acá es axios crudo, no
  // React Query).
  useEffect(() => {
    if (!qEfectivo) {
      setResultados(null)
      setBuscando(false)
      return
    }
    const seq = ++seqRef.current
    setBuscando(true)
    api.get('/bodega/items', { params: { q: qEfectivo, page_size: pageSize } })
      .then(r => { if (seq === seqRef.current) setResultados(r.data) })
      .catch(() => { if (seq === seqRef.current) toast.error('Error al buscar ítems') })
      .finally(() => { if (seq === seqRef.current) setBuscando(false) })
  }, [qEfectivo, pageSize])

  const enTransito = embarques.filter(e => ['en_transito', 'en_aduana', 'en_bodega'].includes(e.estado))
  const enRecepcion = embarques.filter(e => e.estado === 'en_recepcion')
  // Filtro EN CLIENTE de Activos/Historial (numero, awb_numero, forwarder) sobre
  // lo ya cargado: da el conteo cruzado de las pestañas gratis.
  const enTransitoFilt = enTransito.filter(e => embarqueMatchea(e, qTokens))
  const enRecepcionFilt = enRecepcion.filter(e => embarqueMatchea(e, qTokens))
  const historialFilt = historial.filter(e => embarqueMatchea(e, qTokens))
  const nActivos = enTransitoFilt.length + enRecepcionFilt.length

  return (
    <>
    <div className="p-4 md:p-6 space-y-6 max-w-4xl mx-auto">
      {/* Page header */}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div>
          <h1 className="text-xl font-bold" style={{ color: 'var(--text-primary)' }}>Bodega — Recepción</h1>
          <p className="text-sm mt-0.5" style={{ color: 'var(--text-muted)' }}>
            {enTransito.length} en tránsito · {enRecepcion.length} en recepción abierta
          </p>
        </div>
        <button onClick={load} disabled={loading}
          className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-xl border transition-colors hover:bg-[var(--surface-200)]"
          style={{ borderColor: 'var(--border)', color: 'var(--text-muted)' }}>
          <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
          Actualizar
        </button>
      </div>

      {/* Search — UNA caja, ARRIBA de las pestañas: filtra la pestaña activa y
          muestra el conteo de coincidencias en las demás. El placeholder es un
          CONTRATO: cada palabra es un campo que la consulta realmente toca. */}
      <div className="relative">
        <Search
          className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2"
          style={{ color: 'var(--text-faint)' }}
        />
        <input
          type="text"
          value={q}
          onChange={e => setQ(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter') setQDebounced(q.trim()) // saltea el debounce
            if (e.key === 'Escape') {
              setQ('')
              setQDebounced('')
            }
          }}
          placeholder="N° parte, repuesto, COT, OC, cliente, embarque, guía o AWB…"
          className="input pl-10 pr-10"
        />
        {q && (
          <button
            onClick={() => {
              setQ('')
              setQDebounced('')
            }}
            className="absolute right-3 top-1/2 -translate-y-1/2 hover:opacity-100 opacity-60"
            style={{ color: 'var(--text-muted)' }}
          >
            <X className="w-4 h-4" />
          </button>
        )}
      </div>

      {/* Tabs — con el conteo cruzado cuando hay término */}
      <div className="flex gap-1 p-1 rounded-xl" style={{ backgroundColor: 'var(--surface-200)' }}>
        {([
          ['activos', 'Activos', qEfectivo ? nActivos : enTransito.length + enRecepcion.length],
          ['historial', 'Historial recibidos', qEfectivo ? historialFilt.length : null],
          ['buscar', 'Buscar ítem', qEfectivo && resultados ? resultados.total : null],
        ] as [Tab, string, number | null][]).map(([t, label, cnt]) => (
          <button key={t} onClick={() => setTab(t)}
            className={`flex-1 py-2 text-sm font-medium rounded-lg transition-all flex items-center justify-center gap-1.5 ${tab === t ? 'bg-[var(--surface-100)] shadow-sm' : ''}`}
            style={{ color: tab === t ? 'var(--text-primary)' : 'var(--text-muted)' }}>
            {t === 'activos' ? <Package className="w-4 h-4" /> : t === 'historial' ? <History className="w-4 h-4" /> : <Search className="w-4 h-4" />}
            {label}
            {cnt !== null && (cnt > 0 || !!qEfectivo) && (
              <span className="text-[10px] bg-brand-500/10 text-brand-400 rounded-full px-1.5 font-bold">{cnt}</span>
            )}
          </button>
        ))}
      </div>

      {/* Content */}
      {tab === 'activos' ? (
        loading ? (
          <div className="flex items-center justify-center py-16">
            <Loader2 className="w-6 h-6 animate-spin" style={{ color: 'var(--text-faint)' }} />
          </div>
        ) : embarques.length === 0 ? (
          <div className="text-center py-16 space-y-2">
            <Package className="w-10 h-10 mx-auto opacity-20" style={{ color: 'var(--text-muted)' }} />
            <p className="text-sm" style={{ color: 'var(--text-faint)' }}>Sin embarques activos en bodega</p>
          </div>
        ) : qEfectivo && nActivos === 0 ? (
          // "No coincide nada" ≠ "no hay nada cargado" — y el salto de un clic a
          // donde SÍ hay coincidencias (la conducta de mayor valor del encargo).
          <div className="text-center py-16 space-y-2">
            <p className="text-sm" style={{ color: 'var(--text-faint)' }}>
              Sin resultados para «{qEfectivo}» en Activos.
            </p>
            <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
              Hay{' '}
              <button onClick={() => setTab('buscar')} className="underline text-brand-400 font-semibold">
                {resultados?.total ?? 0} ítem(s) en Buscar ítem
              </button>
              {' '}·{' '}
              <button onClick={() => setTab('historial')} className="underline text-brand-400 font-semibold">
                {historialFilt.length} en Historial
              </button>
            </p>
          </div>
        ) : (
          <div className="space-y-4">
            {enRecepcionFilt.length > 0 && (
              <div>
                <p className="text-xs uppercase tracking-wider font-semibold mb-2" style={{ color: 'var(--text-faint)' }}>
                  Recepción abierta ({enRecepcionFilt.length})
                </p>
                <div className="space-y-3">
                  {enRecepcionFilt.map(e => <EmbarqueCard key={e.id} emb={e} onRefresh={load} onOpenRec={setActiveRec} tokens={qTokens} />)}
                </div>
              </div>
            )}
            {enTransitoFilt.length > 0 && (
              <div>
                <p className="text-xs uppercase tracking-wider font-semibold mb-2" style={{ color: 'var(--text-faint)' }}>
                  Por recibir ({enTransitoFilt.length})
                </p>
                <div className="space-y-3">
                  {enTransitoFilt.map(e => <EmbarqueCard key={e.id} emb={e} onRefresh={load} onOpenRec={setActiveRec} tokens={qTokens} />)}
                </div>
              </div>
            )}
          </div>
        )
      ) : tab === 'historial' ? (
        <HistorialTab items={historialFilt} loading={loadingHist} q={qEfectivo} tokens={qTokens} />
      ) : (
        <BuscarItemsTab
          resp={resultados}
          buscando={buscando}
          q={qEfectivo}
          tokens={qTokens}
          pageSize={pageSize}
          onVerMas={() => setPageSize(s => Math.min(s + 50, PAGE_SIZE_MAX))}
          nActivos={nActivos}
          nHistorial={historialFilt.length}
          irA={setTab}
        />
      )}
    </div>

      {/* RecepcionPanel — portal at page level to break stacking context */}
      {activeRec !== null && createPortal(
        <RecepcionPanel
          recepcionId={activeRec}
          onClose={() => setActiveRec(null)}
          onFinish={() => { setActiveRec(null); load() }}
        />,
        document.body
      )}
    </>
  )
}