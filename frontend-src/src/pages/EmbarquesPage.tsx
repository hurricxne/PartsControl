import { useState, useEffect, useCallback, useRef } from 'react'
import {
  Ship, ChevronDown, ChevronRight, Loader2,
  CheckCircle2, Clock, X, Check, FileText, AlertTriangle,
  RefreshCw, Edit2, Upload, Trash2,
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
}

interface Embarque {
  id: number
  numero: string
  estado: string
  forwarder: string
  awb: string
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
function fmtKg(lbs?: number | null, qty = 1) {
  if (!lbs) return '—'
  return (lbs * 0.453592 * qty).toFixed(2) + ' kg'
}

const BADGE: Record<string, { cls: string; label: string }> = {
  en_transito: { cls: 'bg-blue-500/10 text-blue-400 border-blue-400/20',          label: 'En tránsito' },
  armando:     { cls: 'bg-gray-500/10 text-gray-400 border-gray-500/20',          label: 'Armando'    },
  despachado:  { cls: 'bg-blue-500/10 text-blue-400 border-blue-400/20',          label: 'Despachado' },
  en_aduana:   { cls: 'bg-amber-500/10 text-amber-400 border-amber-400/20',       label: 'En Aduana'  },
  en_bodega:   { cls: 'bg-brand-500/10 text-brand-400 border-brand-400/20',       label: 'En Bodega'  },
  entregado:   { cls: 'bg-emerald-500/10 text-emerald-500 border-emerald-500/20', label: 'Entregado'  },
}
const ESTADOS = ['en_transito', 'armando', 'despachado', 'en_aduana', 'en_bodega', 'entregado']

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

// ── InlineEdit ────────────────────────────────────────────────────────────

function InlineEdit({
  label, value, onSave, type = 'text', placeholder = '—',
}: {
  label: string; value: string; onSave: (v: string) => void
  type?: string; placeholder?: string
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
        autoFocus type={type} value={val}
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

  // Docs local state (initialized from emb, updated via DocField)
  const toDocFile = (s: string) => s ? { filename: s, original: s } : null
  const [docs, setDocs] = useState({
    awb:                toDocFile(emb.awb),
    factura_comercial:  toDocFile(emb.factura_comercial),
    packing_list:       toDocFile(emb.packing_list),
    certificado_origen: toDocFile(emb.certificado_origen),
    doc_adicional:      toDocFile(emb.doc_adicional),
  })

  const badge = BADGE[emb.estado] ?? BADGE.en_transito

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
      toast.success('Estado actualizado')
      onRefresh()
      setEditEstado(false)
    } catch { toast.error('Error') } finally { setSavingEstado(false) }
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
              {editEstado ? (
                <div className="flex items-center gap-1">
                  <select className="input text-xs py-0.5 px-1.5 h-6"
                    value={estadoEdit} onChange={e => setEstadoEdit(e.target.value)}>
                    {ESTADOS.map(e => <option key={e} value={e}>{BADGE[e]?.label ?? e}</option>)}
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
                <button onClick={() => setEditEstado(true)}
                  className={`inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-medium border cursor-pointer hover:opacity-80 ${badge.cls}`}>
                  {emb.estado === 'entregado' ? <CheckCircle2 className="w-3 h-3" /> : <Clock className="w-3 h-3" />}
                  {badge.label}
                </button>
              )}
            </div>

            {/* Metadata editable */}
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-x-4 gap-y-2 mt-3">
              <InlineEdit label="Forwarder" value={emb.forwarder} onSave={v => saveField('forwarder', v)} />
              <InlineEdit label="F. Despacho" value={emb.fecha_despacho} onSave={v => saveField('fecha_despacho', v)} type="date" />
              <InlineEdit label="ETA" value={emb.fecha_llegada_est} onSave={v => saveField('fecha_llegada_est', v)} type="date" />
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
              <DocField label="Otros Documentos"
                value={docs.doc_adicional}
                onChange={v => saveDoc('doc_adicional', v)} />
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
                        {['N° Parte','Descripción','Marca','Qty','Peso','Plazo','Días Rest.','Total Compra USD','OCP','COT / Cliente',''].map(h => (
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
                          <td className="px-3 py-2.5 font-semibold whitespace-nowrap text-brand-400">{fmtClp(item.total_venta_clp)}</td>
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
                    Total compra USD:{' '}
                    <span className="text-brand-400">
                      {fmtClp(detalle.items.reduce((s, i) => s + (i.total_venta_clp || 0), 0))}
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

  const load = useCallback(() => {
    setLoading(true)
    comprasAPI.listEmbarques()
      .then(r => setEmbarques(r.data))
      .catch(() => toast.error('Error al cargar embarques'))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load() }, [load])

  const enTransito  = embarques.filter(e => ['en_transito','despachado','en_aduana'].includes(e.estado)).length
  const entregados  = embarques.filter(e => e.estado === 'entregado').length

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

      {!loading && (
        <div className="grid grid-cols-3 gap-3">
          {[
            { l: 'Total',       v: embarques.length, c: 'text-brand-400'    },
            { l: 'En tránsito', v: enTransito,        c: 'text-amber-400'   },
            { l: 'Entregados',  v: entregados,        c: 'text-emerald-400' },
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
            {embarques.length} embarque(s) · Expande cada fila para ver ítems
          </p>
          {embarques.map(emb => <EmbCard key={emb.id} emb={emb} onRefresh={load} />)}
        </div>
      )}
    </div>
  )
}
