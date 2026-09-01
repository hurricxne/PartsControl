import { useState, useEffect, useCallback, useRef } from 'react'
import {
  Ship, ChevronDown, ChevronRight, Loader2,
  CheckCircle2, Clock, X, Check, FileText, AlertTriangle,
  RefreshCw, Edit2, Upload, Trash2, Search,
} from 'lucide-react'
import toast from 'react-hot-toast'
import { comprasAPI } from '../services/api'

// ── Types ─────────────────────────────────────────────────────────────────

interface DocFile { filename: string; original: string }

interface EmbItem {
  id: number
  embarque_item_id: number
  numero_parte: string
  descripcion: string
  marca: string
  cantidad: number
  peso_unit_lbs: number | null
  total_cotizacion: number
  total_venta_clp: number
  plazo_entrega_max: number | null
  dias_restantes: number | null
  oc_proveedor_numero: string
  oc_proveedor_nombre: string
  numero_cot: string
  cliente: string
  unit_price_usd: number | null
  numero_oc_cliente: string
  numero_factura: string | null
}

interface Embarque {
  id: number
  numero: string
  estado: string
  forwarder: string
  awb: string
  awb_numero: string
  fecha_despacho: string
  fecha_llegada_est: string
  factura_comercial: string
  packing_list: string
  certificado_origen: string
  notas: string
  doc_adicional: string
  pre_embarque_id: number | null
  pre_embarque_numero: string | null
  total_items: number
  created_at: string | null
}

interface EmbarqueDetalle extends Embarque {
  items: EmbItem[]
}

// ── Helpers ──────────────────────────────────────────────────────────────

function fmtClp(v?: number | null) {
  if (!v) return '—'
  return '$' + Math.round(v).toLocaleString('es-CL')
}
function fmtUsd(v: number | null): string {
  if (v == null) return '—'
  return '$' + v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

function fmtKg(lbs?: number | null, qty = 1) {
  if (!lbs) return '—'
  return (lbs * 0.453592 * qty).toFixed(2) + ' kg'
}

const BADGE: Record<string, { cls: string; label: string }> = {
  en_bodega_proveedor: { cls: 'bg-gray-500/10 text-gray-400 border-gray-500/20',         label: 'En Bodega Proveedor' },
  en_transito:         { cls: 'bg-blue-500/10 text-blue-400 border-blue-400/20',         label: 'En Tránsito'         },
  en_aduana:           { cls: 'bg-amber-500/10 text-amber-400 border-amber-400/20',      label: 'En Aduana'           },
  en_bodega:           { cls: 'bg-brand-500/10 text-brand-400 border-brand-400/20',      label: 'En Bodega'           },
  despachado:          { cls: 'bg-emerald-500/10 text-emerald-500 border-emerald-500/20', label: 'Despachado'          },
}
// Estados seleccionables manualmente (despachado se setea automaticamente)
const ESTADOS = ['en_bodega_proveedor', 'en_transito', 'en_aduana', 'en_bodega']
// Orden secuencial para validar transiciones
const ESTADO_ORDER: Record<string, number> = {
  en_bodega_proveedor: 1,
  en_transito: 2,
  en_aduana: 3,
  en_bodega: 4,
  despachado: 5,
}
// Estados terminales: no se puede retroceder ni editar manualmente
const ESTADOS_TERMINALES = new Set(['en_bodega', 'despachado'])
function estadosPermitidos(actual: string): string[] {
  const rankActual = ESTADO_ORDER[actual] ?? 0
  return ESTADOS.filter(e => (ESTADO_ORDER[e] ?? 0) >= rankActual)
}

function getToken() {
  try {
    return JSON.parse(localStorage.getItem('machparts-auth') || '{}')?.state?.token || ''
  } catch { return '' }
}

// ── Upload helper ─────────────────────────────────────────────────────────

async function uploadDoc(file: File): Promise<DocFile> {
  const fd = new FormData()
  fd.append('file', file)
  const res = await fetch('/api/compras/docs/upload', {
    method: 'POST',
    headers: { Authorization: `Bearer ${getToken()}` },
    body: fd,
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || 'Error al subir archivo')
  }
  return res.json()
}

// ── DocField ──────────────────────────────────────────────────────────────

function DocField({
  label, value, onChange,
}: {
  label: string
  value: DocFile | null
  onChange: (v: DocFile | null) => void
}) {
  const [uploading, setUploading] = useState(false)
  const ref = useRef<HTMLInputElement>(null)

  const handleFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    setUploading(true)
    try {
      const result = await uploadDoc(file)
      onChange(result)
    } catch (err: any) {
      toast.error(err.message || 'Error al subir')
    } finally {
      setUploading(false)
      if (ref.current) ref.current.value = ''
    }
  }

  return (
    <div>
      <p className="text-[10px] uppercase tracking-wider mb-0.5" style={{ color: 'var(--text-faint)' }}>{label}</p>
      <input ref={ref} type="file" className="hidden"
        accept=".pdf,.jpg,.jpeg,.png,.doc,.docx,.xls,.xlsx" onChange={handleFile} />
      {value ? (
        <div className="flex items-center gap-1.5 px-2 py-1.5 rounded-lg border"
          style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-100)' }}>
          <FileText className="w-3.5 h-3.5 text-emerald-400 shrink-0" />
          <span className="text-[11px] truncate flex-1" style={{ color: 'var(--text-primary)' }}
            title={value.original}>{value.original}</span>
          <button onClick={() => ref.current?.click()} title="Reemplazar"
            className="p-0.5 rounded hover:bg-[var(--surface-300)]">
            <Upload className="w-3 h-3" style={{ color: 'var(--text-faint)' }} />
          </button>
          <button onClick={() => onChange(null)} title="Quitar"
            className="p-0.5 rounded hover:bg-red-500/10">
            <X className="w-3 h-3 text-red-400" />
          </button>
        </div>
      ) : (
        <button onClick={() => ref.current?.click()} disabled={uploading}
          className="flex items-center gap-1.5 w-full px-2 py-1.5 rounded-lg border text-[11px] transition-colors hover:bg-[var(--surface-200)] disabled:opacity-50"
          style={{ borderColor: 'var(--border)', color: 'var(--text-faint)', borderStyle: 'dashed' }}>
          {uploading
            ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
            : <Upload className="w-3.5 h-3.5" />}
          {uploading ? 'Subiendo...' : 'Adjuntar'}
        </button>
      )}
    </div>
  )
}

// ── OtrosDocs: N documentos adicionales por embarque ──────────────────────

function OtrosDocs({ embId }: { embId: number }) {
  const [docs, setDocs] = useState<{ id: number; nombre: string; archivo: string }[]>([])
  const [uploading, setUploading] = useState(false)
  const ref = useRef<HTMLInputElement>(null)

  const load = useCallback(async () => {
    try {
      const { data } = await comprasAPI.listEmbarqueDocs(embId)
      setDocs(data)
    } catch {
      // la card sigue usable aunque no carguen los docs
    }
  }, [embId])
  useEffect(() => { load() }, [load])

  const handleFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    setUploading(true)
    try {
      const up = await uploadDoc(file)
      await comprasAPI.addEmbarqueDoc(embId, { nombre: up.original || file.name, archivo: up.filename })
      toast.success('Documento adjuntado')
      await load()
    } catch (err: any) {
      toast.error(err.message || 'Error al subir')
    } finally {
      setUploading(false)
      if (ref.current) ref.current.value = ''
    }
  }

  const descargar = async (d: { nombre: string; archivo: string }) => {
    try {
      const resp = await comprasAPI.downloadDoc(d.archivo)
      const url = URL.createObjectURL(new Blob([resp.data]))
      const a = document.createElement('a')
      a.href = url
      a.download = d.nombre
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch {
      toast.error('No se pudo descargar')
    }
  }

  const quitar = async (id: number) => {
    try {
      await comprasAPI.deleteEmbarqueDoc(embId, id)
      await load()
    } catch {
      toast.error('No se pudo quitar')
    }
  }

  return (
    <div>
      <p className="text-[10px] uppercase tracking-wider mb-0.5" style={{ color: 'var(--text-faint)' }}>
        Otros Documentos{docs.length > 0 ? ` (${docs.length})` : ''}
      </p>
      <input ref={ref} type="file" className="hidden"
        accept=".pdf,.jpg,.jpeg,.png,.doc,.docx,.xls,.xlsx" onChange={handleFile} />
      <div className="space-y-1">
        {docs.map(d => (
          <div key={d.id} className="flex items-center gap-1.5 px-2 py-1.5 rounded-lg border"
            style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-100)' }}>
            <FileText className="w-3.5 h-3.5 text-emerald-400 shrink-0" />
            <button onClick={() => descargar(d)} title={`Descargar ${d.nombre}`}
              className="text-[11px] truncate flex-1 text-left hover:underline"
              style={{ color: 'var(--text-primary)' }}>{d.nombre}</button>
            <button onClick={() => quitar(d.id)} title="Quitar"
              className="p-0.5 rounded hover:bg-red-500/10">
              <X className="w-3 h-3 text-red-400" />
            </button>
          </div>
        ))}
        <button onClick={() => ref.current?.click()} disabled={uploading}
          className="flex items-center gap-1.5 w-full px-2 py-1.5 rounded-lg border text-[11px] transition-colors hover:bg-[var(--surface-200)] disabled:opacity-50"
          style={{ borderColor: 'var(--border)', color: 'var(--text-faint)', borderStyle: 'dashed' }}>
          {uploading
            ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
            : <Upload className="w-3.5 h-3.5" />}
          {uploading ? 'Subiendo...' : docs.length ? 'Agregar otro' : 'Adjuntar'}
        </button>
      </div>
    </div>
  )
}

// ── InlineEdit ────────────────────────────────────────────────────────────

function InlineEdit({
  label, value, onSave, type = 'text', placeholder = '—', maxLength,
}: {
  label: string; value: string; onSave: (v: string) => void
  type?: string; placeholder?: string; maxLength?: number  // opcional: tope de longitud (p.ej. N° AWB = 100, columna VARCHAR(100))
}) {
  const [editing, setEditing] = useState(false)
  const [val, setVal] = useState(value)
  useEffect(() => { setVal(value) }, [value])

  if (!editing) return (
    <div>
      <p className="text-[10px] uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>{label}</p>
      <button onClick={() => setEditing(true)}
        className="group flex items-center gap-1 text-xs font-medium mt-0.5 px-1 -ml-1 rounded hover:bg-[var(--surface-300)] transition-colors w-full text-left"
        style={{ color: val ? 'var(--text-primary)' : 'var(--text-faint)' }}>
        <span className="flex-1 truncate">{val || placeholder}</span>
        <Edit2 className="w-2.5 h-2.5 opacity-0 group-hover:opacity-40 shrink-0" />
      </button>
    </div>
  )
  return (
    <div>
      <p className="text-[10px] uppercase tracking-wider mb-0.5" style={{ color: 'var(--text-faint)' }}>{label}</p>
      <input
        autoFocus type={type} value={val} maxLength={maxLength}
        onChange={e => setVal(e.target.value)}
        onBlur={() => { onSave(val); setEditing(false) }}
        onKeyDown={e => {
          if (e.key === 'Enter') { onSave(val); setEditing(false) }
          if (e.key === 'Escape') { setVal(value); setEditing(false) }
        }}
        className="w-full rounded text-xs px-2 py-1 outline-none"
        style={{ backgroundColor: 'var(--surface)', border: '2px solid #3b82f6', color: 'var(--text-primary)' }}
      />
    </div>
  )
}

// ── DiasRestantes ─────────────────────────────────────────────────────────

function DiasRestantes({ dias }: { dias: number | null }) {
  if (dias === null || dias === undefined)
    return <span style={{ color: 'var(--text-faint)' }}>—</span>
  const color = dias < 0 ? 'text-red-400' : dias <= 5 ? 'text-amber-400' : 'text-emerald-400'
  return <span className={`font-bold text-xs ${color}`}>{dias > 0 ? `+${dias}d` : `${dias}d`}</span>
}

// ── EmbCard ───────────────────────────────────────────────────────────────

function EmbCard({ emb, onRefresh }: { emb: Embarque; onRefresh: () => void }) {
  const [open, setOpen] = useState(false)
  const [detalle, setDetalle] = useState<EmbarqueDetalle | null>(null)
  const [loadingDetail, setLoadingDetail] = useState(false)
  const [savingEstado, setSavingEstado] = useState(false)
  const [editEstado, setEditEstado] = useState(false)
  const [estadoEdit, setEstadoEdit] = useState(emb.estado)
  const [confirmDes, setConfirmDes] = useState<EmbItem | null>(null)
  const [desembarcando, setDesembarcando] = useState<number | null>(null)
  const [usdEdits, setUsdEdits]       = useState<Record<number, string>>({})
  const [usdValues, setUsdValues]     = useState<Record<number, number | null>>({})
  const [savingUsd, setSavingUsd]     = useState<Record<number, boolean>>({})

  const handleSaveUsdEmb = async (itemId: number) => {
    const raw = usdEdits[itemId]
    if (raw === undefined || raw === '') return
    const val = parseFloat(raw)
    if (isNaN(val) || val < 0) return
    setSavingUsd(prev => ({ ...prev, [itemId]: true }))
    try {
      const { data } = await comprasAPI.updatePrecioUsd(itemId, val)
      setUsdValues(prev => ({ ...prev, [itemId]: data.unit_price_usd }))
      toast.success('Precio USD guardado')
    } catch {
      toast.error('Error al guardar precio USD')
    } finally {
      setSavingUsd(prev => ({ ...prev, [itemId]: false }))
      setUsdEdits(prev => { const n = { ...prev }; delete n[itemId]; return n })
    }
  }

  // Docs local state (initialized from emb, updated via DocField)
  const toDocFile = (s: string) => s ? { filename: s, original: s } : null
  const [docs, setDocs] = useState({
    awb:                toDocFile(emb.awb),
    factura_comercial:  toDocFile(emb.factura_comercial),
    packing_list:       toDocFile(emb.packing_list),
    certificado_origen: toDocFile(emb.certificado_origen),
    doc_adicional:      toDocFile(emb.doc_adicional),
  })

  // Fallback honesto: ante un estado desconocido, mostrar el valor crudo (o "Desconocido"),
  // NO "En Bodega Proveedor" — antes ese default confundia al operador.
  const badge = BADGE[emb.estado] ?? { cls: 'bg-gray-500/10 text-gray-400 border-gray-500/20', label: emb.estado || 'Desconocido' }

  const fetchDetail = useCallback(async () => {
    setLoadingDetail(true)
    try {
      const res = await fetch(`/api/compras/embarques-list/${emb.id}`, {
        headers: { Authorization: `Bearer ${getToken()}` },
      })
      if (!res.ok) throw new Error()
      const d: EmbarqueDetalle = await res.json()
      setDetalle(d)
      setEstadoEdit(d.estado)
      setDocs({
        awb:                toDocFile(d.awb),
        factura_comercial:  toDocFile(d.factura_comercial),
        packing_list:       toDocFile(d.packing_list),
        certificado_origen: toDocFile(d.certificado_origen),
        doc_adicional:      toDocFile(d.doc_adicional),
      })
    } catch { toast.error('Error al cargar detalle del embarque') }
    finally { setLoadingDetail(false) }
  }, [emb.id])

  const handleToggle = () => {
    if (!open && !detalle) fetchDetail()
    setOpen(v => !v)
  }

  const saveField = async (field: string, value: string) => {
    try {
      await comprasAPI.actualizarEmbarque(emb.id, { [field]: value || null })
      onRefresh()
    } catch { toast.error('Error al guardar') }
  }

  const saveEstado = async () => {
    setSavingEstado(true)
    try {
      await comprasAPI.actualizarEmbarque(emb.id, { estado: estadoEdit })
      if (estadoEdit === 'en_bodega') {
        toast.success('Embarque marcado como En Bodega — ítems disponibles en Despachos')
      } else {
        toast.success('Estado actualizado')
      }
      onRefresh()
      setEditEstado(false)
    } catch (err: any) {
      const detail = err?.response?.data?.detail
      toast.error(typeof detail === 'string' ? detail : 'Error al actualizar estado')
    } finally { setSavingEstado(false) }
  }

  const saveDoc = async (field: keyof typeof docs, val: DocFile | null) => {
    setDocs(prev => ({ ...prev, [field]: val }))
    try {
      await comprasAPI.actualizarEmbarque(emb.id, { [field]: val?.filename || null })
      toast.success(val ? 'Documento guardado' : 'Documento eliminado')
      onRefresh()
    } catch { toast.error('Error al guardar documento') }
  }

  const handleDesembarcar = async (item: EmbItem) => {
    setDesembarcando(item.embarque_item_id)
    try {
      const res = await fetch(`/api/compras/embarques-list/${emb.id}/items/${item.embarque_item_id}`, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${getToken()}` },
      })
      if (!res.ok) throw new Error()
      toast.success(`${item.numero_parte} devuelto a preparado`)
      setConfirmDes(null)
      await fetchDetail()
      onRefresh()
    } catch { toast.error('Error al desembarcar') }
    finally { setDesembarcando(null) }
  }

  return (
    <>
      <div className="rounded-2xl border" style={{ backgroundColor: 'var(--surface-50)', borderColor: 'var(--border)' }}>

        {/* Header */}
        <div className="flex items-start gap-3 p-4">
          <div className="w-10 h-10 rounded-xl bg-brand-500/10 flex items-center justify-center shrink-0 mt-0.5">
            <Ship className="w-5 h-5 text-brand-400" />
          </div>

          <div className="flex-1 min-w-0">
            {/* Número + estado */}
            <div className="flex items-center gap-2 flex-wrap">
              <span className="font-mono font-bold text-sm text-brand-400">{emb.numero}</span>
              {emb.pre_embarque_numero && (
                <span className="text-[10px] px-1.5 py-0.5 rounded font-mono"
                  style={{ backgroundColor: 'var(--surface-300)', color: 'var(--text-faint)' }}>
                  ← {emb.pre_embarque_numero}
                </span>
              )}
              {emb.awb_numero && (
                <span className="text-[10px] px-1.5 py-0.5 rounded font-mono"
                  style={{ backgroundColor: 'var(--surface-300)', color: 'var(--text-muted)' }}
                  title="N° AWB / BL">
                  AWB {emb.awb_numero}
                </span>
              )}
              {editEstado ? (
                <div className="flex items-center gap-1">
                  <select className="input text-xs py-0.5 px-1.5 h-6"
                    value={estadoEdit} onChange={e => setEstadoEdit(e.target.value)}>
                    {estadosPermitidos(emb.estado).map(e => (
                      <option key={e} value={e}>{BADGE[e]?.label ?? e}</option>
                    ))}
                  </select>
                  <button onClick={saveEstado} disabled={savingEstado}
                    className="p-1 rounded text-emerald-400 hover:bg-emerald-500/10">
                    {savingEstado ? <Loader2 className="w-3 h-3 animate-spin" /> : <Check className="w-3 h-3" />}
                  </button>
                  <button onClick={() => { setEditEstado(false); setEstadoEdit(emb.estado) }}
                    className="p-1 rounded hover:bg-[var(--surface-200)]">
                    <X className="w-3 h-3" style={{ color: 'var(--text-faint)' }} />
                  </button>
                </div>
              ) : (
                <button
                  onClick={() => !ESTADOS_TERMINALES.has(emb.estado) && setEditEstado(true)}
                  disabled={ESTADOS_TERMINALES.has(emb.estado)}
                  title={ESTADOS_TERMINALES.has(emb.estado)
                    ? 'Estado final — no se puede modificar manualmente'
                    : 'Click para cambiar estado'}
                  className={`inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-medium border ${
                    ESTADOS_TERMINALES.has(emb.estado) ? 'cursor-not-allowed' : 'cursor-pointer hover:opacity-80'
                  } ${badge.cls}`}>
                  {emb.estado === 'despachado' ? <CheckCircle2 className="w-3 h-3" /> : <Clock className="w-3 h-3" />}
                  {badge.label}
                </button>
              )}
            </div>

            {/* Metadata editable */}
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-x-4 gap-y-2 mt-3">
              <InlineEdit label="Forwarder" value={emb.forwarder} onSave={v => saveField('forwarder', v)} />
              <InlineEdit label="F. Despacho" value={emb.fecha_despacho} onSave={v => saveField('fecha_despacho', v)} type="date" />
              <InlineEdit label="ETA" value={emb.fecha_llegada_est} onSave={v => saveField('fecha_llegada_est', v)} type="date" />
              <InlineEdit label="N° AWB / BL" value={emb.awb_numero} onSave={v => saveField('awb_numero', v)} maxLength={100} />
            </div>

            {/* Docs */}
            <div className="grid grid-cols-1 sm:grid-cols-3 lg:grid-cols-5 gap-2 mt-3">
              <DocField label="AWB / BL"
                value={docs.awb}
                onChange={v => saveDoc('awb', v)} />
              <DocField label="Factura Comercial"
                value={docs.factura_comercial}
                onChange={v => saveDoc('factura_comercial', v)} />
              <DocField label="Packing List"
                value={docs.packing_list}
                onChange={v => saveDoc('packing_list', v)} />
              <DocField label="Cert. de Origen"
                value={docs.certificado_origen}
                onChange={v => saveDoc('certificado_origen', v)} />
              <OtrosDocs embId={emb.id} />
            </div>

            {/* Notas */}
            <div className="mt-2">
              <InlineEdit label="Notas" value={emb.notas} onSave={v => saveField('notas', v)} placeholder="Sin notas" />
            </div>

            <p className="text-[10px] mt-2" style={{ color: 'var(--text-faint)' }}>
              {emb.total_items} ítem(s)
              {emb.created_at && <> · {new Date(emb.created_at).toLocaleDateString('es-CL')}</>}
            </p>
          </div>

          {/* Toggle expand */}
          <button onClick={handleToggle}
            className="shrink-0 p-1 rounded-lg hover:bg-[var(--surface-200)] transition-colors mt-1"
            style={{ color: 'var(--text-faint)' }}>
            {loadingDetail && !detalle
              ? <Loader2 className="w-4 h-4 animate-spin" />
              : open ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
          </button>
        </div>

        {/* Items table */}
        {open && (
          <div className="border-t" style={{ borderColor: 'var(--border)' }}>
            {loadingDetail && !detalle ? (
              <div className="flex justify-center py-6">
                <Loader2 className="w-5 h-5 animate-spin" style={{ color: 'var(--text-faint)' }} />
              </div>
            ) : !detalle || detalle.items.length === 0 ? (
              <p className="text-xs text-center py-5" style={{ color: 'var(--text-faint)' }}>
                Sin ítems en este embarque
              </p>
            ) : (
              <>
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead>
                      <tr style={{ backgroundColor: 'var(--surface-200)', borderBottom: '1px solid var(--border)' }}>
                        {['N° Parte','Descripción','Marca','Qty','Peso','Plazo','Días Rest.','OC Cliente','Invoice','Unit USD','Total USD','OCP','COT / Cliente',''].map(h => (
                          <th key={h} className="px-3 py-2 text-left font-semibold uppercase tracking-wider whitespace-nowrap"
                            style={{ color: 'var(--text-faint)' }}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {detalle.items.map((item, idx) => (
                        <tr key={item.embarque_item_id}
                          style={{ borderBottom: idx < detalle.items.length - 1 ? '1px solid var(--border)' : undefined }}>
                          <td className="px-3 py-2.5 font-mono font-bold text-brand-400 whitespace-nowrap">{item.numero_parte || '—'}</td>
                          <td className="px-3 py-2.5 max-w-[160px] truncate" style={{ color: 'var(--text-primary)' }} title={item.descripcion}>{item.descripcion || '—'}</td>
                          <td className="px-3 py-2.5" style={{ color: 'var(--text-muted)' }}>{item.marca || '—'}</td>
                          <td className="px-3 py-2.5 text-center" style={{ color: 'var(--text-muted)' }}>{item.cantidad}</td>
                          <td className="px-3 py-2.5 whitespace-nowrap" style={{ color: 'var(--text-muted)' }}>{fmtKg(item.peso_unit_lbs, item.cantidad)}</td>
                          <td className="px-3 py-2.5 text-center" style={{ color: 'var(--text-muted)' }}>
                            {item.plazo_entrega_max ? `${item.plazo_entrega_max}d háb.` : '—'}
                          </td>
                          <td className="px-3 py-2.5 text-center"><DiasRestantes dias={item.dias_restantes} /></td>
                          {/* A-3.4: OC Cliente */}
                          <td className="px-3 py-2.5 font-mono whitespace-nowrap text-xs" style={{ color: 'var(--text-muted)' }}>
                            {item.numero_oc_cliente || '—'}
                          </td>
                          {/* A-3.4: Invoice */}
                          <td className="px-3 py-2.5 font-mono whitespace-nowrap text-xs" style={{ color: 'var(--text-muted)' }}>
                            {item.numero_factura || '—'}
                          </td>
                          {/* A-3.2: Unit USD inline edit */}
                          <td className="px-3 py-2.5 text-xs" onClick={e => e.stopPropagation()}>
                            <div className="flex items-center gap-1">
                              <input
                                type="number"
                                step="0.01"
                                min="0"
                                value={usdEdits[item.id] !== undefined ? usdEdits[item.id] : (usdValues[item.id] ?? item.unit_price_usd ?? '')}
                                placeholder="—"
                                onChange={e => setUsdEdits(prev => ({ ...prev, [item.id]: e.target.value }))}
                                onBlur={() => handleSaveUsdEmb(item.id)}
                                onKeyDown={e => { if (e.key === 'Enter') handleSaveUsdEmb(item.id) }}
                                className="w-20 text-right text-xs rounded px-1 py-0.5 font-mono outline-none"
                                style={{ backgroundColor: 'var(--surface)', border: '1px solid var(--border)', color: 'var(--text-primary)' }}
                              />
                              {savingUsd[item.id] && <Loader2 className="w-3 h-3 animate-spin text-brand-400 shrink-0" />}
                            </div>
                          </td>
                          {/* A-3.4: Total USD */}
                          <td className="px-3 py-2.5 font-semibold whitespace-nowrap text-xs text-emerald-400">
                            {(() => {
                              const u = usdValues[item.id] ?? item.unit_price_usd
                              return u != null ? fmtUsd(u * item.cantidad) : '—'
                            })()}
                          </td>
                          <td className="px-3 py-2.5 font-mono whitespace-nowrap" style={{ color: 'var(--text-muted)' }}>{item.oc_proveedor_numero || '—'}</td>
                          <td className="px-3 py-2.5 whitespace-nowrap">
                            <p className="font-mono text-[10px] text-brand-400">{item.numero_cot ? `COT-${item.numero_cot}` : '—'}</p>
                            <p className="text-[10px]" style={{ color: 'var(--text-faint)' }}>{item.cliente || '—'}</p>
                          </td>
                          <td className="px-3 py-2.5">
                            <button
                              onClick={() => setConfirmDes(item)}
                              title="Desembarcar — devolver a preparado"
                              className="p-1 rounded hover:bg-red-500/10 transition-colors group"
                              style={{ color: 'var(--text-faint)' }}>
                              <Trash2 className="w-3.5 h-3.5 group-hover:text-red-400" />
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>

                {/* Totales */}
                <div className="flex justify-end gap-6 px-4 py-2.5 border-t text-xs font-semibold"
                  style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-100)' }}>
                  <span style={{ color: 'var(--text-muted)' }}>
                    Peso total:{' '}
                    <span style={{ color: 'var(--text-primary)' }}>
                      {detalle.items.reduce((s, i) => s + (i.peso_unit_lbs || 0) * 0.453592 * i.cantidad, 0).toFixed(2)} kg
                    </span>
                  </span>
                  <span style={{ color: 'var(--text-muted)' }}>
                    Total USD:{' '}
                    <span className="text-emerald-400">
                      {fmtUsd(detalle.items.reduce((s, i) => {
                        const u = usdValues[i.id] ?? i.unit_price_usd
                        return s + (u != null ? u * i.cantidad : 0)
                      }, 0))}
                    </span>
                  </span>
                </div>
              </>
            )}
          </div>
        )}
      </div>

      {/* Confirm desembarcar */}
      {confirmDes && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm">
          <div className="w-full max-w-sm rounded-2xl border shadow-2xl p-5 space-y-4"
            style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
            <div className="flex items-center gap-2">
              <AlertTriangle className="w-5 h-5 text-amber-400 shrink-0" />
              <h3 className="font-bold" style={{ color: 'var(--text-primary)' }}>¿Desembarcar ítem?</h3>
            </div>
            <p className="text-sm" style={{ color: 'var(--text-muted)' }}>
              El ítem <span className="font-mono font-bold text-brand-400">{confirmDes.numero_parte}</span>{' '}
              volverá a estado <strong>preparado</strong> y quedará disponible en pre-embarques.
            </p>
            <div className="flex justify-end gap-2">
              <button onClick={() => setConfirmDes(null)} className="btn-secondary text-sm">Cancelar</button>
              <button
                onClick={() => handleDesembarcar(confirmDes)}
                disabled={desembarcando === confirmDes.embarque_item_id}
                className="flex items-center gap-2 px-4 py-2 rounded-xl text-sm font-semibold bg-red-500/10 text-red-400 border border-red-500/30 hover:bg-red-500/20 transition-colors disabled:opacity-50">
                {desembarcando === confirmDes.embarque_item_id
                  ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  : <Trash2 className="w-3.5 h-3.5" />}
                Desembarcar
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  )
}

// ── Main Page ─────────────────────────────────────────────────────────────

export default function EmbarquesPage() {
  const [embarques, setEmbarques] = useState<Embarque[]>([])
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState('')

  const load = useCallback(() => {
    setLoading(true)
    comprasAPI.listEmbarques()
      .then(r => setEmbarques(r.data))
      .catch(() => toast.error('Error al cargar embarques'))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load() }, [load])

  const enTransito  = embarques.filter(e => ['en_bodega_proveedor','en_transito','en_aduana'].includes(e.estado)).length
  const enBodega    = embarques.filter(e => e.estado === 'en_bodega').length
  const despachados = embarques.filter(e => e.estado === 'despachado').length

  // Buscador client-side sobre la lista ya cargada: N° de embarque, N° AWB o forwarder.
  const q = search.trim().toLowerCase()
  const visibles = q
    ? embarques.filter(e =>
        (e.numero || '').toLowerCase().includes(q) ||
        (e.awb_numero || '').toLowerCase().includes(q) ||
        (e.forwarder || '').toLowerCase().includes(q))
    : embarques

  return (
    <div className="space-y-6">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold" style={{ color: 'var(--text-primary)' }}>Embarques</h1>
          <p className="text-sm mt-1" style={{ color: 'var(--text-muted)' }}>
            Seguimiento logístico · Documentos · Estado de entrega
          </p>
        </div>
        <button onClick={load} className="btn-secondary flex items-center gap-2 self-start">
          <RefreshCw className="w-4 h-4" /> Actualizar
        </button>
      </div>

      <div className="relative">
        <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2" style={{ color: 'var(--text-faint)' }} />
        <input type="text" value={search} onChange={e => setSearch(e.target.value)}
          placeholder="Buscar por N° de embarque, N° AWB o forwarder…" className="input pl-10 pr-10" />
        {search && (
          <button onClick={() => setSearch('')}
            className="absolute right-3 top-1/2 -translate-y-1/2 hover:opacity-100 opacity-60"
            style={{ color: 'var(--text-muted)' }}>
            <X className="w-4 h-4" />
          </button>
        )}
      </div>

      {!loading && (
        <div className="grid grid-cols-4 gap-3">
          {[
            { l: 'Total',        v: embarques.length, c: 'text-brand-400'    },
            { l: 'En tránsito',  v: enTransito,       c: 'text-amber-400'    },
            { l: 'En bodega',    v: enBodega,         c: 'text-brand-400'    },
            { l: 'Despachados',  v: despachados,      c: 'text-emerald-400'  },
          ].map(s => (
            <div key={s.l} className="rounded-2xl p-4 border"
              style={{ backgroundColor: 'var(--surface-50)', borderColor: 'var(--border)' }}>
              <p className="text-[10px] uppercase tracking-widest" style={{ color: 'var(--text-faint)' }}>{s.l}</p>
              <p className={`text-xl font-bold mt-1 ${s.c}`}>{s.v}</p>
            </div>
          ))}
        </div>
      )}

      {loading ? (
        <div className="flex items-center justify-center py-14">
          <Loader2 className="w-6 h-6 animate-spin" style={{ color: 'var(--text-faint)' }} />
        </div>
      ) : embarques.length === 0 ? (
        <div className="rounded-2xl border p-10 text-center space-y-3"
          style={{ backgroundColor: 'var(--surface-50)', borderColor: 'var(--border)' }}>
          <Ship className="w-8 h-8 mx-auto" style={{ color: 'var(--text-faint)' }} />
          <p className="font-semibold" style={{ color: 'var(--text-primary)' }}>Sin embarques generados</p>
          <p className="text-sm" style={{ color: 'var(--text-muted)' }}>
            Genera embarques cerrando pre-embarques desde el módulo de Pre-Embarques.
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          <p className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>
            {visibles.length} embarque(s) · Expande cada fila para ver ítems
          </p>
          {q && visibles.length === 0 ? (
            <p className="text-sm py-6 text-center" style={{ color: 'var(--text-muted)' }}>
              Sin coincidencias para “{search.trim()}”.
            </p>
          ) : (
            visibles.map(emb => <EmbCard key={emb.id} emb={emb} onRefresh={load} />)
          )}
        </div>
      )}
    </div>
  )
}
