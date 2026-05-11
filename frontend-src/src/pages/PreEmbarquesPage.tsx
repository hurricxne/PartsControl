import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import {
  Package, Search, Loader2, ArrowUpDown, ArrowUp, ArrowDown,
  CheckCircle2, X, Ship, Scale, Clock, AlertTriangle, ChevronDown,
  ChevronRight, Filter, RefreshCw, FileText, Anchor, CalendarDays,
  DollarSign, Upload, Minus, PlusCircle, Trash2, Edit2 } from 'lucide-react'
import toast from 'react-hot-toast'
import { comprasAPI, facturasAPI } from '../services/api'

interface PrepItem {
  id: number
  item_num: number
  numero_parte: string
  descripcion: string
  marca: string
  cantidad: number
  peso_unit_lbs: number | null
  total_cotizacion: number
  precio_unit_cotizacion: number
  plazo_entrega_max: number | null
  plazo_dias_prov: number | null
  dias_restantes: number | null
  fecha_asignacion: string | null
  estado_item: string
  numero_cot: string
  cliente: string
  oc_proveedor_id: number | null
  oc_proveedor_numero: string
  oc_proveedor_nombre: string
  oc_proveedor_pais: string
  numero_oc_prov: string
  numero_oc_cliente: string
  fecha_entrega_cliente: string
  cantidad_despacho: number | null
  unit_price_usd: number | null
}

interface PreEmbarque {
  id: number
  numero: string
  estado: string
  notas: string
  doc_adicional: string
  total_items: number
  proveedores: { oc_proveedor_id: number; oc_proveedor_numero: string; oc_proveedor_nombre: string; oc_proveedor_pais: string; item_count: number }[]
  items: PrepItem[]
  embarque_id: number | null
  embarque_numero: string | null
  embarque_awb: string | null
  fecha_llegada_est: string
  created_at: string | null
  total_usd_exwork: number
  total_clp_exwork: number
  peso_total_kg: number
  max_dias_restantes: number | null
}

function easterDate(year: number): Date {
  const a = year % 19, b = Math.floor(year / 100), cc = year % 100
  const d = Math.floor(b / 4), e = b % 4, f = Math.floor((b + 8) / 25)
  const g = Math.floor((b - f + 1) / 3), h = (19 * a + b - d - g + 15) % 30
  const i = Math.floor(cc / 4), k = cc % 4, l = (32 + 2 * e + 2 * i - h - k) % 7
  const m = Math.floor((a + 11 * h + 22 * l) / 451)
  const month = Math.floor((h + l - 7 * m + 114) / 31) - 1
  const day = ((h + l - 7 * m + 114) % 31) + 1
  return new Date(year, month, day)
}
function nthWeekdayOfMonth(year: number, month: number, weekday: number, n: number): number {
  // Retorna el día del mes del n-ésimo 'weekday' (0=Dom..6=Sáb) del mes dado
  let count = 0
  for (let d = 1; d <= 31; d++) {
    const dt = new Date(year, month - 1, d)
    if (dt.getMonth() !== month - 1) break
    if (dt.getDay() === weekday) { count++; if (count === n) return d }
  }
  return -1
}
function isHoliday(date: Date): boolean {
  const mo = date.getMonth() + 1, d = date.getDate(), y = date.getFullYear()
  const fixed: [number, number][] = [
    [1,1],[5,1],[5,21],[6,29],[7,16],[8,15],[9,18],[9,19],[10,12],[10,31],[11,1],[12,8],[12,25],
  ]
  if (fixed.some(([fm, fd]) => fm === mo && fd === d)) return true
  // Día de los Pueblos Indígenas: tercer lunes de junio
  if (mo === 6 && d === nthWeekdayOfMonth(y, 6, 1, 3)) return true
  // Viernes Santo
  const easter = easterDate(y); const vs = new Date(easter); vs.setDate(easter.getDate() - 2)
  return d === vs.getDate() && mo === vs.getMonth() + 1
}
function addBusinessDays(start: Date, days: number): Date {
  const r = new Date(start); let count = 0
  while (count < days) {
    r.setDate(r.getDate() + 1)
    const dow = r.getDay()
    if (dow !== 0 && dow !== 6 && !isHoliday(r)) count++
  }
  return r
}
function toDateInput(d: Date): string { return d.toISOString().split('T')[0] }

function fmtUsd(v?: number) {
  if (!v) return '—'
  return '$' + v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' USD'
}
function fmtKgTotal(kg: number) { return kg > 0 ? kg.toFixed(2) + ' kg' : '—' }
function fmtKgItem(lbs?: number | null, qty = 1) {
  if (!lbs) return '—'
  return (lbs * 0.453592 * qty).toFixed(2) + ' kg'
}

function DiasRestantes({ dias }: { dias: number | null }) {
  if (dias === null) return <span style={{ color: 'var(--text-faint)' }}>—</span>
  if (dias <= 0) return <span className="inline-flex items-center gap-1 text-red-400 font-bold text-[11px]"><AlertTriangle className="w-3 h-3" />Vencido</span>
  if (dias <= 3) return <span className="inline-flex items-center gap-1 text-red-400 font-semibold text-[11px]"><Clock className="w-3 h-3" />{dias}d</span>
  if (dias <= 7) return <span className="inline-flex items-center gap-1 text-amber-400 font-semibold text-[11px]"><Clock className="w-3 h-3" />{dias}d</span>
  return <span className="text-emerald-400 font-semibold text-[11px]">{dias}d</span>
}

type SortKey = 'numero_parte' | 'descripcion' | 'marca' | 'oc_proveedor_nombre' | 'plazo_dias_prov' | 'plazo_entrega_max' | 'dias_restantes' | 'peso_unit_lbs' | 'total_cotizacion'
type SortDir = 'asc' | 'desc'

// ── Modal Cerrar → Embarque ───────────────────────────────────────────────

function CerrarEmbarqueModal({ pre, onClose, onSuccess }: {
  pre: PreEmbarque; onClose: () => void; onSuccess: () => void
}) {
  const [awb, setAwb] = useState('')
  const [forwarder, setForwarder] = useState('')
  const [fechaDespacho, setFechaDespacho] = useState('')
  const [fechaLlegada, setFechaLlegada] = useState(() => {
    if (pre.max_dias_restantes && pre.max_dias_restantes > 0)
      return toDateInput(addBusinessDays(new Date(), pre.max_dias_restantes))
    return ''
  })
  const [packingList, setPackingList] = useState('')
  const [certOrigen, setCertOrigen] = useState('')
  const [notas, setNotas] = useState('')
  const [invoxMap, setInvoxMap] = useState<Record<number, string>>({})
  const [saving, setSaving] = useState(false)

  const uniqueOcps = useMemo(() => {
    const map = new Map<number, { id: number; numero: string; nombre: string; itemCount: number }>()
    for (const item of pre.items) {
      if (!item.oc_proveedor_id) continue
      if (!map.has(item.oc_proveedor_id)) {
        map.set(item.oc_proveedor_id, {
          id: item.oc_proveedor_id,
          numero: item.oc_proveedor_numero || item.numero_oc_prov || ('OCP-' + item.oc_proveedor_id),
          nombre: item.oc_proveedor_nombre || '—',
          itemCount: 0,
        })
      }
      map.get(item.oc_proveedor_id)!.itemCount++
    }
    return Array.from(map.values())
  }, [pre.items])

  const handleSubmit = async () => {
    const invoxItems = uniqueOcps
      .filter(ocp => invoxMap[ocp.id]?.trim())
      .map(ocp => ({ oc_proveedor_id: ocp.id, numero_invox: invoxMap[ocp.id].trim() }))
    setSaving(true)
    try {
    // ── Validar facturas de proveedor ─────────────────────────────────────
    try {
      const { data: val } = await facturasAPI.validarPreEmbarque(pre.id)
      if (!val.ok) {
        const lines = val.discrepancias
          .map((d: any) => `• ${d.numero_parte}: ${d.motivo}`)
          .join('\n')
        toast.error(
          `No se puede cerrar: discrepancias en facturas:\n${lines}`,
          { duration: 8000 }
        )
        return
      }
    } catch { /* si falla validación, permitir continuar */ }
    // ─────────────────────────────────────────────────────────────────────
      const { data } = await comprasAPI.cerrarPreEmbarque(pre.id, {
        awb: awb || null, forwarder: forwarder || null,
        fecha_despacho: fechaDespacho || null, fecha_llegada_est: fechaLlegada || null,
        packing_list: packingList || null, certificado_origen: certOrigen || null,
        notas: notas || null,
        invox_items: invoxItems.length > 0 ? invoxItems : null,
      })
      toast.success('Embarque ' + data.numero + ' generado desde ' + pre.numero)
      onSuccess()
    } catch (err: any) {
      toast.error(err.response?.data?.detail || 'Error al generar embarque')
    } finally { setSaving(false) }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm overflow-y-auto">
      <div className="w-full max-w-2xl rounded-2xl border shadow-2xl my-4"
        style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
        <div className="flex items-center justify-between px-5 py-4 border-b" style={{ borderColor: 'var(--border)' }}>
          <div>
            <h2 className="text-base font-bold flex items-center gap-2" style={{ color: 'var(--text-primary)' }}>
              <Anchor className="w-4 h-4 text-brand-400" />
              Cerrar {pre.numero} → Embarque
            </h2>
            <p className="text-xs mt-0.5" style={{ color: 'var(--text-muted)' }}>
              {pre.items.length} ítem(s) · {fmtUsd(pre.total_usd_exwork)} · {fmtKgTotal(pre.peso_total_kg)}
            </p>
          </div>
          <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-[var(--surface-200)]">
            <X className="w-4 h-4" style={{ color: 'var(--text-faint)' }} />
          </button>
        </div>
        <div className="p-5 space-y-5">
          {uniqueOcps.length > 0 && (
            <div>
              <label className="block text-xs font-semibold uppercase tracking-wider mb-2" style={{ color: 'var(--text-faint)' }}>
                N° INVOX por Orden de Compra Proveedor
              </label>
              <div className="rounded-xl border overflow-hidden" style={{ borderColor: 'var(--border)' }}>
                <div className="px-3 py-2 grid grid-cols-[1fr_1fr_64px] gap-3 text-[10px] font-semibold uppercase tracking-wider"
                  style={{ backgroundColor: 'var(--surface-200)', color: 'var(--text-faint)', borderBottom: '1px solid var(--border)' }}>
                  <span>OCP</span><span>N° INVOX</span><span className="text-center">Ítems</span>
                </div>
                {uniqueOcps.map(ocp => (
                  <div key={ocp.id} className="grid grid-cols-[1fr_1fr_64px] gap-3 px-3 py-2.5 items-center"
                    style={{ borderBottom: '1px solid var(--border)' }}>
                    <div>
                      <p className="text-xs font-mono font-bold text-brand-400">{ocp.numero}</p>
                      <p className="text-[10px]" style={{ color: 'var(--text-faint)' }}>{ocp.nombre}</p>
                    </div>
                    <input className="input text-xs py-1.5" placeholder="Ej: INV-2026-001"
                      value={invoxMap[ocp.id] || ''}
                      onChange={e => setInvoxMap(m => ({ ...m, [ocp.id]: e.target.value }))} />
                    <p className="text-xs text-center" style={{ color: 'var(--text-muted)' }}>{ocp.itemCount}</p>
                  </div>
                ))}
              </div>
            </div>
          )}
          <div>
            <label className="block text-xs font-semibold uppercase tracking-wider mb-2" style={{ color: 'var(--text-faint)' }}>
              Documentación de embarque
            </label>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div>
                <label className="block text-[10px] uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>Forwarder</label>
                <input className="input w-full text-xs" placeholder="Ej: DHL / Kuehne+Nagel"
                  value={forwarder} onChange={e => setForwarder(e.target.value)} />
              </div>
              <div>
                <label className="block text-[10px] uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>AWB / BL</label>
                <input className="input w-full text-xs" placeholder="Número de guía aérea o BL"
                  value={awb} onChange={e => setAwb(e.target.value)} />
              </div>
              <div>
                <label className="block text-[10px] uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>Fecha despacho</label>
                <input type="date" className="input w-full text-xs"
                  value={fechaDespacho} onChange={e => setFechaDespacho(e.target.value)} />
              </div>
              <div>
                <label className="block text-[10px] uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>
                  Fecha llegada estimada
                  {pre.max_dias_restantes != null && (
                    <span className="ml-1 normal-case font-normal" style={{ color: 'var(--text-muted)' }}>
                      — auto: {pre.max_dias_restantes}d háb.
                    </span>
                  )}
                </label>
                <input type="date" className="input w-full text-xs"
                  value={fechaLlegada} onChange={e => setFechaLlegada(e.target.value)} />
              </div>
              <div>
                <label className="block text-[10px] uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>Packing List</label>
                <input className="input w-full text-xs" placeholder="N° o referencia"
                  value={packingList} onChange={e => setPackingList(e.target.value)} />
              </div>
              <div>
                <label className="block text-[10px] uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>Certificado de origen</label>
                <input className="input w-full text-xs" placeholder="N° certificado"
                  value={certOrigen} onChange={e => setCertOrigen(e.target.value)} />
              </div>
              <div className="sm:col-span-2">
                <label className="block text-[10px] uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>Notas</label>
                <textarea className="input w-full text-xs resize-none" rows={2}
                  placeholder="Observaciones adicionales..."
                  value={notas} onChange={e => setNotas(e.target.value)} />
              </div>
            </div>
          </div>
        </div>
        <div className="flex justify-end gap-3 px-5 py-4 border-t" style={{ borderColor: 'var(--border)' }}>
          <button onClick={onClose} className="btn-secondary">Cancelar</button>
          <button onClick={handleSubmit} disabled={saving} className="btn-primary flex items-center gap-2">
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Ship className="w-4 h-4" />}
            {saving ? 'Generando…' : 'Generar Embarque'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Modal Generar Pre-Embarque ────────────────────────────────────────────

// ── helpers ──────────────────────────────────────────────────────────────────

async function uploadDoc(file: File): Promise<{ filename: string; original: string }> {
  const fd = new FormData()
  fd.append('file', file)
  const token = (() => { try { return JSON.parse(localStorage.getItem('machparts-auth') || '{}')?.state?.token || '' } catch { return '' } })()
  const res = await fetch('/api/compras/docs/upload', {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}` },
    body: fd,
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || 'Error al subir archivo')
  }
  return res.json()
}

interface DocFile { filename: string; original: string }

function DocUploadField({
  label,
  required,
  value,
  onChange,
  error,
}: {
  label: string
  required?: boolean
  value: DocFile | null
  onChange: (v: DocFile | null) => void
  error?: boolean
}) {
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)

  const handleFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    setUploading(true)
    setUploadError('')
    try {
      const result = await uploadDoc(file)
      onChange(result)
    } catch (err: any) {
      setUploadError(err.message || 'Error al subir')
    } finally {
      setUploading(false)
      if (inputRef.current) inputRef.current.value = ''
    }
  }

  return (
    <div>
      <label className="text-[10px] uppercase tracking-wider block mb-0.5"
        style={{ color: error ? '#f87171' : 'var(--text-faint)' }}>
        {label} {required && <span className="text-red-400">*</span>}
      </label>
      {value ? (
        <div className="flex items-center gap-1.5 px-2 py-1.5 rounded-lg border"
          style={{ borderColor: error ? '#f87171' : 'var(--border)', backgroundColor: 'var(--surface-100)' }}>
          <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400 shrink-0" />
          <span className="text-xs truncate flex-1" style={{ color: 'var(--text-primary)' }}
            title={value.original}>{value.original}</span>
          <button type="button" onClick={() => onChange(null)}
            className="text-red-400 hover:text-red-300 shrink-0">
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
      ) : (
        <button type="button"
          onClick={() => inputRef.current?.click()}
          disabled={uploading}
          className="flex items-center gap-1.5 w-full px-2 py-1.5 rounded-lg border text-xs transition-colors"
          style={{
            borderColor: error ? '#f87171' : 'var(--border)',
            color: uploading ? 'var(--text-faint)' : 'var(--text-muted)',
            backgroundColor: 'var(--surface-100)',
          }}>
          {uploading
            ? <><Loader2 className="w-3.5 h-3.5 animate-spin" /> Subiendo…</>
            : <><Upload className="w-3.5 h-3.5" /> Seleccionar archivo…</>}
        </button>
      )}
      {uploadError && <p className="text-[10px] text-red-400 mt-0.5">{uploadError}</p>}
      <input ref={inputRef} type="file" className="hidden"
        accept=".pdf,.jpg,.jpeg,.png,.doc,.docx,.xls,.xlsx"
        onChange={handleFile} />
    </div>
  )
}

function GenPreEmbarqueModal({ selectedItems, onClose, onSuccess }: {
  selectedItems: PrepItem[]; onClose: () => void; onSuccess: () => void
}) {
  const [notas, setNotas] = useState('')
  const [awb, setAwb] = useState<{filename:string;original:string}|null>(null)
  const [facturaComercial, setFacturaComercial] = useState<{filename:string;original:string}|null>(null)
  const [packingList, setPackingList] = useState<{filename:string;original:string}|null>(null)
  const [certOrigen, setCertOrigen] = useState<{filename:string;original:string}|null>(null)
  const [saving, setSaving] = useState(false)
  const [qtyEdits, setQtyEditsP] = useState<Record<number, string>>({})

  const totalKg = useMemo(() => {
    const kg = selectedItems.reduce((s, i) =>
      s + (i.peso_unit_lbs ? i.peso_unit_lbs * 0.453592 * (i.cantidad || 1) : 0), 0)
    return kg > 0 ? kg.toFixed(2) + ' kg' : '—'
  }, [selectedItems])

  const totalUsd = useMemo(() =>
    selectedItems.reduce((s, i) => s + (i.precio_unit_cotizacion * (i.cantidad || 1) || 0), 0),
  [selectedItems])

  const maxPlazo = useMemo(() => {
    const p = selectedItems.map(i => i.plazo_entrega_max).filter((v): v is number => v != null && v > 0)
    return p.length > 0 ? Math.max(...p) : null
  }, [selectedItems])

  const fechaEstimada = useMemo(() =>
    maxPlazo ? addBusinessDays(new Date(), maxPlazo).toLocaleDateString('es-CL') : null,
  [maxPlazo])

  const handleSubmit = async () => {
    setSaving(true)
    try {
      const { data } = await comprasAPI.crearPreEmbarque({
        item_ids: selectedItems.map(i => i.id),
        notas: notas || null,
        awb: awb?.filename || null,
        factura_comercial: facturaComercial?.filename || null,
        packing_list: packingList?.filename || null,
        certificado_origen: certOrigen?.filename || null,
      })
      // Batch-update cantidad_despacho for items that were edited
      const editedItems = selectedItems.filter(i => qtyEdits[i.id] !== undefined)
      if (editedItems.length > 0) {
        await Promise.all(editedItems.map(i => {
          const qty = Math.max(1, Math.min(parseFloat(qtyEdits[i.id]) || i.cantidad, i.cantidad))
          return comprasAPI.updatePreEmbarqueItem(data.id, i.id, { cantidad_despacho: qty })
        }))
      }
      toast.success(data.numero + ' creado')
      onSuccess()
    } catch { toast.error('Error al crear pre-embarque') }
    finally { setSaving(false) }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm">
      <div className="w-full max-w-xl rounded-2xl border shadow-2xl"
        style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
        <div className="flex items-center justify-between px-5 py-4 border-b" style={{ borderColor: 'var(--border)' }}>
          <div>
            <h2 className="text-base font-bold" style={{ color: 'var(--text-primary)' }}>Generar Pre-Embarque</h2>
            <p className="text-xs mt-0.5" style={{ color: 'var(--text-muted)' }}>
              {selectedItems.length} ítem(s) · {totalKg} · {fmtUsd(totalUsd)}
            </p>
          </div>
          <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-[var(--surface-200)]">
            <X className="w-4 h-4" style={{ color: 'var(--text-faint)' }} />
          </button>
        </div>
        <div className="p-5 space-y-4">
          <div className="rounded-xl border overflow-hidden" style={{ borderColor: 'var(--border)' }}>
            <div className="px-3 py-2 text-xs font-semibold uppercase tracking-wider"
              style={{ backgroundColor: 'var(--surface-200)', color: 'var(--text-faint)', borderBottom: '1px solid var(--border)' }}>
              Ítems a incluir
            </div>
            <div className="divide-y divide-[var(--border)] max-h-52 overflow-y-auto">
              {selectedItems.map(item => (
                <div key={item.id} className="flex items-center justify-between px-3 py-2 text-xs gap-2">
                  <div className="flex items-center gap-2 min-w-0 flex-1">
                    <span className="font-mono font-bold text-brand-400 shrink-0">{item.numero_parte || '—'}</span>
                    <span className="truncate" style={{ color: 'var(--text-muted)' }}>{item.descripcion}</span>
                  </div>
                  <div className="flex items-center gap-1.5 shrink-0">
                    <input
                      type="number"
                      min={1}
                      max={item.cantidad}
                      step={1}
                      className="input text-xs py-0.5 w-16 text-center"
                      value={qtyEdits[item.id] ?? String(item.cantidad)}
                      onChange={e => setQtyEditsP(q => ({ ...q, [item.id]: e.target.value }))}
                      onBlur={e => {
                        const v = parseFloat(e.target.value)
                        const clamped = isNaN(v) ? item.cantidad : Math.max(1, Math.min(v, item.cantidad))
                        setQtyEditsP(q => ({ ...q, [item.id]: String(clamped) }))
                      }}
                    />
                    <span className="text-[10px]" style={{ color: 'var(--text-faint)' }}>/ {item.cantidad}</span>
                    {item.plazo_entrega_max && (
                      <span className="text-[10px] text-amber-400">{item.plazo_entrega_max}d</span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            {[
              ['Ítems', String(selectedItems.length)],
              ['Peso total', totalKg],
              ['Total USD', fmtUsd(totalUsd)],
              ['Plazo máx.', maxPlazo ? maxPlazo + 'd háb.' : '—'],
            ].map(([label, value]) => (
              <div key={label} className="rounded-xl p-3 text-center" style={{ backgroundColor: 'var(--surface-200)' }}>
                <p className="text-[10px] uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>{label}</p>
                <p className="font-bold text-xs mt-0.5 text-brand-400">{value}</p>
              </div>
            ))}
          </div>
          {fechaEstimada && (
            <p className="text-xs px-3 py-2 rounded-lg flex items-center gap-1.5"
              style={{ backgroundColor: 'var(--surface-200)', color: 'var(--text-muted)' }}>
              <CalendarDays className="w-3 h-3 shrink-0" style={{ color: 'var(--text-faint)' }} />
              Fecha estimada de llegada:{' '}
              <strong style={{ color: 'var(--text-primary)' }}>{fechaEstimada}</strong>
              <span style={{ color: 'var(--text-faint)' }}>({maxPlazo} días hábiles)</span>
            </p>
          )}
          {/* Documentos opcionales */}
          <div className="rounded-xl border p-3 space-y-3" style={{ borderColor: 'var(--border)' }}>
            <p className="text-[10px] font-semibold uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>
              Documentos <span className="normal-case font-normal">(opcionales)</span>
            </p>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              <DocUploadField label="AWB / BL" value={awb} onChange={setAwb} />
              <DocUploadField label="Factura Comercial" value={facturaComercial} onChange={setFacturaComercial} />
              <DocUploadField label="Packing List" value={packingList} onChange={setPackingList} />
              <DocUploadField label="Cert. Origen" value={certOrigen} onChange={setCertOrigen} />
            </div>
          </div>
          <div>
            <label className="text-xs font-medium uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>Notas</label>
            <textarea className="input w-full mt-1 resize-none" rows={2}
              placeholder="Observaciones adicionales..."
              value={notas} onChange={e => setNotas(e.target.value)} />
          </div>
        </div>
        <div className="flex justify-end gap-3 px-5 py-4 border-t" style={{ borderColor: 'var(--border)' }}>
          <button onClick={onClose} className="btn-secondary">Cancelar</button>
          <button onClick={handleSubmit} disabled={saving} className="btn-primary flex items-center gap-2">
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Package className="w-4 h-4" />}
            Generar Pre-Embarque
          </button>
        </div>
      </div>
    </div>
  )
}


// ── Modal Generar Embarque directo ────────────────────────────────────────

function GenEmbarqueModal({ items, onClose, onSuccess }: {
  items: PrepItem[]; onClose: () => void; onSuccess: () => void
}) {
  const [awb, setAwb] = useState<DocFile|null>(null)
  const [facturaComercial, setFacturaComercial] = useState<DocFile|null>(null)
  const [packingList, setPackingList] = useState<DocFile|null>(null)
  const [certOrigen, setCertOrigen] = useState<DocFile|null>(null)
  const [docAdicional, setDocAdicional] = useState<DocFile|null>(null)
  const [invoxMap, setInvoxMap] = useState<Record<number, string>>({})
  const [qtyEdits, setQtyEdits] = useState<Record<number, string>>({})
  const [saving, setSaving] = useState(false)
  const [awbError, setAwbError] = useState(false)
  const [facturaError, setFacturaError] = useState(false)

  interface OcpFacturaState {
    existentes: any[] | null
    loaded: boolean
    uploading: boolean
    parsed: any[] | null
    numero: string
    fecha: string
  }
  const [facturasOcp, setFacturasOcp] = useState<Record<number, OcpFacturaState>>({})
  const factInputRefs = useRef<Record<number, HTMLInputElement | null>>({})

  const uniqueOcps = useMemo(() => {
    const map = new Map<number, { id: number; numero: string; nombre: string; itemCount: number }>()
    for (const item of items) {
      if (!item.oc_proveedor_id) continue
      if (!map.has(item.oc_proveedor_id)) {
        map.set(item.oc_proveedor_id, {
          id: item.oc_proveedor_id,
          numero: item.oc_proveedor_numero || item.numero_oc_prov || ('OCP-' + item.oc_proveedor_id),
          nombre: item.oc_proveedor_nombre || '—',
          itemCount: 0,
        })
      }
      map.get(item.oc_proveedor_id)!.itemCount++
    }
    return Array.from(map.values())
  }, [items])

  // Load existing facturas per OCP on mount
  useEffect(() => {
    const ocpIds = items
      .map(i => i.oc_proveedor_id)
      .filter((id): id is number => id !== null)
    const uniqueIds = [...new Set(ocpIds)]
    for (const ocpId of uniqueIds) {
      facturasAPI.list(ocpId).then(({ data }) => {
        setFacturasOcp(prev => ({
          ...prev,
          [ocpId]: {
            existentes: data,
            loaded: true,
            uploading: false,
            parsed: prev[ocpId]?.parsed ?? null,
            numero: prev[ocpId]?.numero ?? '',
            fecha: prev[ocpId]?.fecha ?? '',
          }
        }))
      }).catch(() => {
        setFacturasOcp(prev => ({
          ...prev,
          [ocpId]: {
            existentes: [],
            loaded: true,
            uploading: false,
            parsed: prev[ocpId]?.parsed ?? null,
            numero: prev[ocpId]?.numero ?? '',
            fecha: prev[ocpId]?.fecha ?? '',
          }
        }))
      })
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const handleParseFact = async (ocpId: number, file: File) => {
    setFacturasOcp(prev => ({
      ...prev,
      [ocpId]: { ...(prev[ocpId] ?? { existentes: null, loaded: false, parsed: null, numero: '', fecha: '' }), uploading: true }
    }))
    try {
      const { data } = await facturasAPI.parsePdf(file)
      const rows = Array.isArray(data) ? data : (data.items ?? [])
      const numero = data.invoice_number ?? data.numero ?? ''
      const fecha = data.invoice_date ?? data.fecha ?? ''
      setFacturasOcp(prev => ({
        ...prev,
        [ocpId]: { ...(prev[ocpId] ?? { existentes: null, loaded: false }), uploading: false, parsed: rows, numero, fecha }
      }))
      toast.success('PDF procesado: ' + rows.length + ' ítem(s)')
    } catch {
      toast.error('Error al procesar PDF de factura')
      setFacturasOcp(prev => ({
        ...prev,
        [ocpId]: { ...(prev[ocpId] ?? { existentes: null, loaded: false, parsed: null, numero: '', fecha: '' }), uploading: false }
      }))
    }
  }

  const handleSubmit = async () => {
    let valid = true
    if (!awb) { setAwbError(true); valid = false }
    if (!facturaComercial) { setFacturaError(true); valid = false }
    if (!valid) { toast.error('AWB/BL y Factura Comercial son obligatorios'); return }

    const invox_items = uniqueOcps
      .filter(ocp => invoxMap[ocp.id]?.trim())
      .map(ocp => ({ oc_proveedor_id: ocp.id, numero_invox: invoxMap[ocp.id].trim() }))

    setSaving(true)
    try {
      const { data: pre } = await comprasAPI.crearPreEmbarque({
        item_ids: items.map(i => i.id),
        notas: null,
      })

      // Update cantidad_despacho per item if edited
      const qtyItems = items.filter(i => qtyEdits[i.id] !== undefined)
      if (qtyItems.length > 0) {
        await Promise.all(qtyItems.map(i => {
          const qty = Math.max(1, Math.min(parseFloat(qtyEdits[i.id]) || i.cantidad, i.cantidad))
          return comprasAPI.updatePreEmbarqueItem(pre.id, i.id, { cantidad_despacho: qty })
        }))
      }

      // Save doc_adicional if uploaded
      if (docAdicional) {
        await comprasAPI.updatePreEmbarque(pre.id, { doc_adicional: docAdicional.filename }).catch(() => null)
      }

      // Create facturas for OCPs that have parsed data and no existing facturas
      for (const ocp of uniqueOcps) {
        const fo = facturasOcp[ocp.id]
        if (fo?.parsed && fo.parsed.length > 0 && (!fo.existentes || fo.existentes.length === 0)) {
          await facturasAPI.create({
            oc_proveedor_id: ocp.id,
            numero_factura: fo.numero || invoxMap[ocp.id] || 'S/N',
            fecha: fo.fecha || new Date().toISOString().split('T')[0],
            items: fo.parsed,
          }).catch(() => null)
        }
      }

      // Validate facturas before cerrar
      try {
        const { data: val } = await facturasAPI.validarPreEmbarque(pre.id)
        if (!val.ok) {
          const lines = val.discrepancias
            .map((d: any) => `• ${d.numero_parte}: ${d.motivo}`)
            .join('\n')
          toast.error(
            `No se puede cerrar: discrepancias en facturas:\n${lines}`,
            { duration: 8000 }
          )
          setSaving(false)
          return
        }
      } catch { /* si falla validación, permitir continuar */ }

      await comprasAPI.cerrarPreEmbarque(pre.id, {
        awb: awb?.filename || '',
        factura_comercial: facturaComercial?.filename || null,
        packing_list: packingList?.filename || null,
        certificado_origen: certOrigen?.filename || null,
        invox_items: invox_items.length > 0 ? invox_items : null,
      })
      toast.success('Embarque generado correctamente')
      onSuccess()
    } catch (err: any) {
      toast.error(err.response?.data?.detail || 'Error al generar embarque')
    } finally { setSaving(false) }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 p-4 backdrop-blur-sm overflow-y-auto">
      <div className="w-full max-w-2xl rounded-2xl border shadow-2xl my-4"
        style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
        <div className="flex items-center justify-between px-5 py-4 border-b" style={{ borderColor: 'var(--border)' }}>
          <div>
            <h2 className="text-base font-bold flex items-center gap-2" style={{ color: 'var(--text-primary)' }}>
              <Ship className="w-4 h-4 text-emerald-400" />
              Generar Embarque
            </h2>
            <p className="text-xs mt-0.5" style={{ color: 'var(--text-muted)' }}>
              {items.length} ítem(s) seleccionados · Pre-embarque + embarque en un paso
            </p>
          </div>
          <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-[var(--surface-200)]">
            <X className="w-4 h-4" style={{ color: 'var(--text-faint)' }} />
          </button>
        </div>

        <div className="p-5 space-y-5">
          {/* Items list with editable qty */}
          <div className="rounded-xl border overflow-hidden" style={{ borderColor: 'var(--border)' }}>
            <div className="px-3 py-2 grid grid-cols-[1fr_auto] gap-2 text-[10px] font-semibold uppercase tracking-wider"
              style={{ backgroundColor: 'var(--surface-200)', color: 'var(--text-faint)', borderBottom: '1px solid var(--border)' }}>
              <span>Ítem a embarcar</span>
              <span className="text-right pr-1">Cant. Despacho</span>
            </div>
            <div className="divide-y divide-[var(--border)] max-h-48 overflow-y-auto">
              {items.map(item => (
                <div key={item.id} className="flex items-center justify-between px-3 py-2 text-xs gap-2">
                  <div className="flex items-center gap-2 min-w-0 flex-1">
                    <span className="font-mono font-bold text-brand-400 shrink-0">{item.numero_parte || '—'}</span>
                    <span className="truncate" style={{ color: 'var(--text-muted)' }}>{item.descripcion}</span>
                  </div>
                  <div className="flex items-center gap-1.5 shrink-0">
                    <input
                      type="number"
                      min={1}
                      max={item.cantidad}
                      step={1}
                      className="input text-xs py-0.5 w-16 text-center"
                      value={qtyEdits[item.id] ?? String(item.cantidad)}
                      onChange={e => setQtyEdits(q => ({ ...q, [item.id]: e.target.value }))}
                      onBlur={e => {
                        const v = parseFloat(e.target.value)
                        const clamped = isNaN(v) ? item.cantidad : Math.max(1, Math.min(v, item.cantidad))
                        setQtyEdits(q => ({ ...q, [item.id]: String(clamped) }))
                      }}
                    />
                    <span className="text-[10px]" style={{ color: 'var(--text-faint)' }}>/ {item.cantidad}</span>
                  </div>
                </div>
              ))}
            </div>
          </div>

          {/* INVOX per OCP */}
          {uniqueOcps.length > 0 && (
            <div>
              <label className="block text-xs font-semibold uppercase tracking-wider mb-2" style={{ color: 'var(--text-faint)' }}>
                N° INVOX por Orden de Compra Proveedor
              </label>
              <div className="rounded-xl border overflow-hidden" style={{ borderColor: 'var(--border)' }}>
                <div className="px-3 py-2 grid grid-cols-[1fr_1fr_64px] gap-3 text-[10px] font-semibold uppercase tracking-wider"
                  style={{ backgroundColor: 'var(--surface-200)', color: 'var(--text-faint)', borderBottom: '1px solid var(--border)' }}>
                  <span>OCP</span><span>N° INVOX</span><span className="text-center">Ítems</span>
                </div>
                {uniqueOcps.map(ocp => (
                  <div key={ocp.id} className="grid grid-cols-[1fr_1fr_64px] gap-3 px-3 py-2.5 items-center"
                    style={{ borderBottom: '1px solid var(--border)' }}>
                    <div>
                      <p className="text-xs font-mono font-bold text-brand-400">{ocp.numero}</p>
                      <p className="text-[10px]" style={{ color: 'var(--text-faint)' }}>{ocp.nombre}</p>
                    </div>
                    <input className="input text-xs py-1.5" placeholder="Ej: INV-2026-001"
                      value={invoxMap[ocp.id] || ''}
                      onChange={e => setInvoxMap(m => ({ ...m, [ocp.id]: e.target.value }))} />
                    <p className="text-xs text-center" style={{ color: 'var(--text-muted)' }}>{ocp.itemCount}</p>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Facturas de Proveedor per OCP */}
          {uniqueOcps.length > 0 && (
            <div>
              <label className="block text-xs font-semibold uppercase tracking-wider mb-2" style={{ color: 'var(--text-faint)' }}>
                Facturas de Proveedor
              </label>
              <div className="space-y-2">
                {uniqueOcps.map(ocp => {
                  const fo = facturasOcp[ocp.id]
                  const hasExisting = fo?.existentes && fo.existentes.length > 0
                  const hasParsed = fo?.parsed && fo.parsed.length > 0
                  return (
                    <div key={ocp.id} className="rounded-xl border p-3" style={{ borderColor: 'var(--border)' }}>
                      <div className="flex items-center justify-between gap-2 mb-2">
                        <div>
                          <p className="text-xs font-mono font-bold text-brand-400">{ocp.numero}</p>
                          <p className="text-[10px]" style={{ color: 'var(--text-faint)' }}>{ocp.nombre}</p>
                        </div>
                        {!fo?.loaded && (
                          <span className="text-[10px]" style={{ color: 'var(--text-faint)' }}>
                            <Loader2 className="w-3 h-3 animate-spin inline mr-1" />Cargando…
                          </span>
                        )}
                        {fo?.loaded && hasExisting && (
                          <span className="inline-flex items-center gap-1 text-[10px] text-emerald-400">
                            <CheckCircle2 className="w-3 h-3" />
                            {fo.existentes!.length} factura(s) registrada(s)
                          </span>
                        )}
                        {fo?.loaded && !hasExisting && !hasParsed && (
                          <button
                            type="button"
                            disabled={fo?.uploading}
                            onClick={() => factInputRefs.current[ocp.id]?.click()}
                            className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg border text-[10px] transition-colors"
                            style={{ borderColor: 'var(--border)', color: 'var(--text-muted)', backgroundColor: 'var(--surface-200)' }}>
                            {fo?.uploading
                              ? <><Loader2 className="w-3 h-3 animate-spin" />Procesando…</>
                              : <><Upload className="w-3 h-3" />Subir Factura PDF</>}
                          </button>
                        )}
                        {fo?.loaded && !hasExisting && hasParsed && (
                          <button
                            type="button"
                            onClick={() => setFacturasOcp(prev => ({ ...prev, [ocp.id]: { ...prev[ocp.id], parsed: null } }))}
                            className="text-[10px] text-red-400 hover:text-red-300">
                            <X className="w-3 h-3 inline" /> Quitar
                          </button>
                        )}
                        <input
                          ref={el => { factInputRefs.current[ocp.id] = el }}
                          type="file" accept=".pdf" className="hidden"
                          onChange={e => { const f = e.target.files?.[0]; if (f) handleParseFact(ocp.id, f); e.target.value = '' }}
                        />
                      </div>
                      {hasParsed && (
                        <div className="mt-1">
                          <p className="text-[10px] text-emerald-400 mb-1">
                            <CheckCircle2 className="w-3 h-3 inline mr-1" />
                            {fo!.parsed!.length} ítem(s) parseados del PDF
                            {fo!.numero && <span className="ml-2 text-[var(--text-faint)]">N° {fo!.numero}</span>}
                          </p>
                          <div className="rounded-lg overflow-hidden border" style={{ borderColor: 'var(--border)' }}>
                            <div className="divide-y divide-[var(--border)] max-h-24 overflow-y-auto">
                              {fo!.parsed!.slice(0, 5).map((row: any, idx: number) => (
                                <div key={idx} className="flex items-center justify-between px-2 py-1 text-[10px]">
                                  <span className="font-mono text-brand-400">{row.numero_parte || row.part_number || '—'}</span>
                                  <span style={{ color: 'var(--text-faint)' }}>×{row.cantidad ?? row.quantity ?? row.qty ?? '?'}</span>
                                </div>
                              ))}
                              {fo!.parsed!.length > 5 && (
                                <div className="px-2 py-1 text-[10px] text-center" style={{ color: 'var(--text-faint)' }}>
                                  +{fo!.parsed!.length - 5} más…
                                </div>
                              )}
                            </div>
                          </div>
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            </div>
          )}

          {/* Documentación */}
          <div>
            <label className="block text-xs font-semibold uppercase tracking-wider mb-2" style={{ color: 'var(--text-faint)' }}>
              Documentación de embarque
            </label>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <DocUploadField label="AWB / BL" required value={awb}
                onChange={v => { setAwb(v); setAwbError(false) }} error={awbError} />
              <DocUploadField label="Factura Comercial" required value={facturaComercial}
                onChange={v => { setFacturaComercial(v); setFacturaError(false) }} error={facturaError} />
              <DocUploadField label="Packing List" value={packingList} onChange={setPackingList} />
              <DocUploadField label="Cert. Origen" value={certOrigen} onChange={setCertOrigen} />
              <DocUploadField label="Otros Documentos" value={docAdicional} onChange={setDocAdicional} />
            </div>
          </div>
        </div>

        <div className="flex justify-end gap-3 px-5 py-4 border-t" style={{ borderColor: 'var(--border)' }}>
          <button onClick={onClose} className="btn-secondary">Cancelar</button>
          <button onClick={handleSubmit} disabled={saving}
            className="btn-primary flex items-center gap-2 bg-emerald-600 hover:bg-emerald-500 border-emerald-600">
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Ship className="w-4 h-4" />}
            {saving ? 'Generando…' : 'Generar Embarque'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ── SortHeader ────────────────────────────────────────────────────────────

function SortHeader({ col, current, dir, onClick, children }: {
  col: SortKey; current: SortKey; dir: SortDir; onClick: (k: SortKey) => void; children: React.ReactNode
}) {
  const active = col === current
  return (
    <th className="px-3 py-2 text-left text-xs font-semibold uppercase tracking-wider cursor-pointer select-none whitespace-nowrap"
      style={{ color: active ? 'var(--text-primary)' : 'var(--text-faint)' }}
      onClick={() => onClick(col)}>
      <span className="flex items-center gap-1">
        {children}
        {active
          ? dir === 'asc' ? <ArrowUp className="w-3 h-3" /> : <ArrowDown className="w-3 h-3" />
          : <ArrowUpDown className="w-3 h-3 opacity-30" />}
      </span>
    </th>
  )
}

// ── PreEmbarqueCard ───────────────────────────────────────────────────────

function PreEmbarqueCard({ pre, availableItems, onRefresh }: { pre: PreEmbarque; availableItems: PrepItem[]; onRefresh: () => void }) {
  const [open, setOpen] = useState(false)
  const [showCerrar, setShowCerrar] = useState(false)
  const [showAddItems, setShowAddItems] = useState(false)
  const [loadingItem, setLoadingItem] = useState<number | null>(null)
  const [editingFecha, setEditingFecha] = useState(false)
  const [fechaEst, setFechaEst] = useState(pre.fecha_llegada_est || '')
  const [savingFecha, setSavingFecha] = useState(false)
  const [savingQty, setSavingQty] = useState<number | null>(null)
  const [qtyEdits, setQtyEdits] = useState<Record<number, string>>({})
  const [usdEdits, setUsdEdits]   = useState<Record<number, string>>({})
  const [usdValues, setUsdValues] = useState<Record<number, number | null>>({})

  const handleSaveUsdPre = async (itemId: number) => {
    const raw = usdEdits[itemId]
    if (raw === undefined || raw === '') return
    const val = parseFloat(raw)
    if (isNaN(val) || val < 0) return
    try {
      const { data } = await comprasAPI.updatePrecioUsd(itemId, val)
      setUsdValues(prev => ({ ...prev, [itemId]: data.unit_price_usd }))
      toast.success('Precio USD guardado')
    } catch {
      toast.error('Error al guardar precio USD')
    } finally {
      setUsdEdits(prev => { const n = { ...prev }; delete n[itemId]; return n })
    }
  }

  const handleSaveFecha = async (val: string) => {
    setSavingFecha(true)
    try {
      await comprasAPI.updatePreEmbarque(pre.id, { fecha_llegada_est: val || null })
      setFechaEst(val)
      toast.success('Fecha estimada guardada')
      onRefresh()
    } catch { toast.error('Error al guardar fecha') }
    finally { setSavingFecha(false); setEditingFecha(false) }
  }
  const isEmbarcado = pre.estado === 'embarcado'

  // Items preparados NO in this pre-embarque
  const preItemIds = new Set(pre.items.map(i => i.id))
  const addableItems = availableItems.filter(i => !preItemIds.has(i.id))

  const handleQtyChange = async (item: PrepItem, newQty: number) => {
    const qty = Math.max(1, Math.min(newQty, item.cantidad))
    setSavingQty(item.id)
    try {
      await comprasAPI.updatePreEmbarqueItem(pre.id, item.id, { cantidad_despacho: qty })
      toast.success(`Cant. despacho actualizada: ${qty}`)
      onRefresh()
    } catch { toast.error('Error al actualizar cantidad') }
    finally { setSavingQty(null) }
  }

  const handleRemoveItem = async (itemId: number) => {
    setLoadingItem(itemId)
    try {
      await comprasAPI.removeItemPreEmbarque(pre.id, itemId)
      toast.success('Item removido del pre-embarque')
      onRefresh()
    } catch { toast.error('Error al remover item') }
    finally { setLoadingItem(null) }
  }

  const handleAddItem = async (itemId: number) => {
    setLoadingItem(itemId)
    try {
      await comprasAPI.addItemPreEmbarque(pre.id, itemId)
      toast.success('Item agregado al pre-embarque')
      onRefresh()
    } catch (e: any) { toast.error(e?.response?.data?.detail || 'Error al agregar item') }
    finally { setLoadingItem(null) }
  }

  return (
    <>
      <div className="rounded-2xl border overflow-hidden"
        style={{ backgroundColor: 'var(--surface-50)', borderColor: 'var(--border)' }}>
        <div className="px-4 pt-4 pb-3">
          <div className="flex items-start gap-3">
            <div className={'w-9 h-9 rounded-xl flex items-center justify-center shrink-0 mt-0.5 ' + (isEmbarcado ? 'bg-emerald-500/10' : 'bg-brand-500/10')}>
              {isEmbarcado ? <Ship className="w-4 h-4 text-emerald-400" /> : <Package className="w-4 h-4 text-brand-400" />}
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="font-mono font-bold text-sm text-brand-400">{pre.numero}</span>
                <span className={'text-[10px] font-semibold px-2 py-0.5 rounded-full border ' + (isEmbarcado
                  ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20'
                  : 'bg-amber-500/10 text-amber-400 border-amber-500/20')}>
                  {isEmbarcado ? '✓ Embarcado' : 'Abierto — en preparación'}
                </span>
                {pre.embarque_numero && (
                  <span className="text-[10px] px-2 py-0.5 rounded-full bg-blue-500/10 text-blue-400 border border-blue-400/20 font-mono">
                    → {pre.embarque_numero}
                  </span>
                )}
              </div>
              <p className="text-[11px] mt-0.5" style={{ color: 'var(--text-faint)' }}>
                {pre.created_at ? new Date(pre.created_at).toLocaleDateString('es-CL') : '—'}
                {pre.notas && <span className="ml-2 italic">{pre.notas}</span>}
              </p>
              {pre.proveedores?.length > 0 && (
                <div className="flex flex-wrap gap-1 mt-1.5">
                  {pre.proveedores.map(pv => (
                    <span key={pv.oc_proveedor_id}
                      className="inline-flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full border font-mono"
                      style={{ borderColor: 'var(--border)', color: 'var(--text-muted)', backgroundColor: 'var(--surface-200)' }}>
                      <span className="text-brand-400 font-semibold">{pv.oc_proveedor_numero}</span>
                      <span style={{ color: 'var(--text-faint)' }}>·</span>
                      {pv.oc_proveedor_nombre || '—'}
                      <span className="ml-0.5 text-[9px]" style={{ color: 'var(--text-faint)' }}>({pv.item_count})</span>
                    </span>
                  ))}
                </div>
              )}
            </div>
            {!isEmbarcado && (
              <button onClick={() => setShowCerrar(true)}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold shrink-0 bg-brand-500/10 text-brand-400 border border-brand-500/30 hover:bg-brand-500/20 transition-colors">
                <Anchor className="w-3 h-3" />Cerrar a Embarque
              </button>
            )}
          </div>

          {/* stat strip */}
          <div className="mt-3 grid grid-cols-2 sm:grid-cols-4 gap-2">
            <div className="rounded-lg px-3 py-2" style={{ backgroundColor: 'var(--surface-200)' }}>
              <p className="text-[9px] uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>Ítems</p>
              <p className="text-sm font-bold mt-0.5 text-brand-400">{pre.items.length || pre.total_items}</p>
            </div>
            <div className="rounded-lg px-3 py-2" style={{ backgroundColor: 'var(--surface-200)' }}>
              <p className="text-[9px] uppercase tracking-wider flex items-center gap-1" style={{ color: 'var(--text-faint)' }}>
                <DollarSign className="w-2.5 h-2.5" />USD Ex Work
              </p>
              <p className="text-xs font-bold mt-0.5 font-mono" style={{ color: 'var(--text-primary)' }}>
                {pre.total_usd_exwork > 0 ? '$' + pre.total_usd_exwork.toLocaleString('en-US', { minimumFractionDigits: 2 }) : '—'}
              </p>
            </div>
            <div className="rounded-lg px-3 py-2" style={{ backgroundColor: 'var(--surface-200)' }}>
              <p className="text-[9px] uppercase tracking-wider flex items-center gap-1" style={{ color: 'var(--text-faint)' }}>
                <Scale className="w-2.5 h-2.5" />Peso total
              </p>
              <p className="text-xs font-bold mt-0.5" style={{ color: 'var(--text-primary)' }}>{fmtKgTotal(pre.peso_total_kg)}</p>
            </div>
            <div className="rounded-lg px-3 py-2" style={{ backgroundColor: 'var(--surface-200)' }}>
              <p className="text-[9px] uppercase tracking-wider flex items-center gap-1" style={{ color: 'var(--text-faint)' }}>
                <Clock className="w-2.5 h-2.5" />Días háb. rest.
              </p>
              <div className="mt-0.5"><DiasRestantes dias={pre.max_dias_restantes} /></div>
            </div>
            <div className="rounded-lg px-3 py-2 col-span-2 sm:col-span-1" style={{ backgroundColor: 'var(--surface-200)' }}>
              <p className="text-[9px] uppercase tracking-wider flex items-center gap-1 mb-1" style={{ color: 'var(--text-faint)' }}>
                <CalendarDays className="w-2.5 h-2.5" />Llegada estimada
              </p>
              {editingFecha ? (
                <div className="flex items-center gap-1">
                  <input
                    type="date"
                    autoFocus
                    value={fechaEst}
                    onChange={e => setFechaEst(e.target.value)}
                    className="rounded text-[11px] px-1.5 py-0.5 outline-none font-mono"
                    style={{ backgroundColor: 'var(--surface)', border: '2px solid #3b82f6', color: 'var(--text-primary)', width: '120px' }}
                    onBlur={() => handleSaveFecha(fechaEst)}
                    onKeyDown={e => { if (e.key === 'Enter') handleSaveFecha(fechaEst); if (e.key === 'Escape') setEditingFecha(false) }}
                  />
                  {savingFecha && <Loader2 className="w-3 h-3 animate-spin" style={{ color: 'var(--text-faint)' }} />}
                </div>
              ) : (
                <button
                  onClick={() => !isEmbarcado && setEditingFecha(true)}
                  className={"group flex items-center gap-1 text-xs font-mono font-semibold rounded px-1 -ml-1 transition-colors " + (!isEmbarcado ? "hover:bg-[var(--surface-300)] cursor-pointer" : "cursor-default")}
                  style={{ color: fechaEst ? 'var(--text-primary)' : 'var(--text-faint)' }}
                >
                  {fechaEst
                    ? new Date(fechaEst + 'T00:00:00').toLocaleDateString('es-CL')
                    : (isEmbarcado ? '—' : 'Agregar fecha...')}
                  {!isEmbarcado && <Edit2 className="w-2.5 h-2.5 opacity-0 group-hover:opacity-50 shrink-0" />}
                </button>
              )}
            </div>
          </div>

          {!isEmbarcado && (
            <div className="mt-2">
              <DocUploadField
                label="Documento adicional"
                value={pre.doc_adicional ? { filename: pre.doc_adicional, original: pre.doc_adicional } : null}
                onChange={async (v) => {
                  try {
                    await comprasAPI.updatePreEmbarque(pre.id, { doc_adicional: v?.filename || null })
                    toast.success(v ? 'Documento adjuntado' : 'Documento eliminado')
                    onRefresh()
                  } catch { toast.error('Error al guardar documento') }
                }}
              />
            </div>
          )}

          <button onClick={() => setOpen(o => !o)}
            className="mt-2 flex items-center gap-1 text-[10px] hover:opacity-70 transition-opacity"
            style={{ color: 'var(--text-faint)' }}>
            {open
              ? <><ChevronDown className="w-3 h-3" />Ocultar detalle</>
              : <><ChevronRight className="w-3 h-3" />Ver {pre.items.length} ítem(s)</>}
          </button>
        </div>

        {open && pre.items.length > 0 && (
          <div className="border-t overflow-x-auto" style={{ borderColor: 'var(--border)' }}>
            <table className="w-full text-xs">
              <thead>
                <tr style={{ backgroundColor: 'var(--surface-200)', borderBottom: '1px solid var(--border)' }}>
                  {['#','N° Parte','Descripción','Proveedor / OCP','Qty','Qty Desp.','Pzo.Máx','Días Rest.','Peso (kg)','Unit USD','Total USD',...(!isEmbarcado ? [''] : [])].map(h => (
                    <th key={h} className="px-3 py-2 text-left font-semibold uppercase tracking-wider whitespace-nowrap"
                      style={{ color: 'var(--text-faint)' }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {pre.items.map((item, idx) => (
                  <tr key={idx} style={{ borderBottom: idx < pre.items.length - 1 ? '1px solid var(--border)' : undefined }}>
                    <td className="px-3 py-2.5 text-center" style={{ color: 'var(--text-faint)' }}>{idx + 1}</td>
                    <td className="px-3 py-2.5 font-mono font-semibold text-brand-400 whitespace-nowrap">{item.numero_parte || '—'}</td>
                    <td className="px-3 py-2.5 max-w-[160px] truncate" style={{ color: 'var(--text-primary)' }}>{item.descripcion}</td>
                    <td className="px-3 py-2.5 whitespace-nowrap">
                      <p className="font-semibold" style={{ color: 'var(--text-primary)' }}>{item.oc_proveedor_nombre || '—'}</p>
                      {item.oc_proveedor_numero && (
                        <p className="text-[10px] font-mono" style={{ color: 'var(--text-faint)' }}>{item.oc_proveedor_numero}</p>
                      )}
                    </td>
                    <td className="px-3 py-2.5 text-center" style={{ color: 'var(--text-muted)' }}>{item.cantidad}</td>
                    <td className="px-3 py-2.5 text-center">
                      {isEmbarcado ? (
                        <span style={{ color: 'var(--text-muted)' }}>
                          {item.cantidad_despacho ?? item.cantidad}
                        </span>
                      ) : (
                        <div className="flex items-center justify-center gap-1">
                          <input
                            type="number"
                            min={1}
                            max={item.cantidad}
                            value={qtyEdits[item.id] ?? (item.cantidad_despacho ?? item.cantidad)}
                            onChange={e => setQtyEdits(prev => ({ ...prev, [item.id]: e.target.value }))}
                            onBlur={e => {
                              const v = parseFloat(e.target.value)
                              if (!isNaN(v) && v !== (item.cantidad_despacho ?? item.cantidad)) {
                                handleQtyChange(item, v)
                              }
                            }}
                            onKeyDown={e => {
                              if (e.key === 'Enter') {
                                const v = parseFloat((e.target as HTMLInputElement).value)
                                if (!isNaN(v)) handleQtyChange(item, v)
                              }
                            }}
                            className="w-14 text-center text-xs rounded px-1 py-0.5 outline-none"
                            style={{ backgroundColor: 'var(--surface)', border: '1px solid var(--border)', color: 'var(--text-primary)' }}
                            disabled={savingQty === item.id}
                          />
                          {savingQty === item.id && <Loader2 className="w-3 h-3 animate-spin" style={{ color: 'var(--text-faint)' }} />}
                          {(item.cantidad_despacho ?? item.cantidad) < item.cantidad && (
                            <span className="text-[9px] text-amber-400 ml-0.5" title="Despacho parcial">
                              {item.cantidad - (item.cantidad_despacho ?? item.cantidad)} pendiente
                            </span>
                          )}
                        </div>
                      )}
                    </td>
                    <td className="px-3 py-2.5 text-center" style={{ color: 'var(--text-muted)' }}>
                      {item.plazo_entrega_max ? item.plazo_entrega_max + 'd' : '—'}
                    </td>
                    <td className="px-3 py-2.5"><DiasRestantes dias={item.dias_restantes} /></td>
                    <td className="px-3 py-2.5 text-right font-mono" style={{ color: 'var(--text-muted)' }}>
                      {fmtKgItem(item.peso_unit_lbs, item.cantidad)}
                    </td>
                    {/* A-3.2: Unit USD inline edit */}
                    <td className="px-3 py-2.5 text-right" onClick={e => e.stopPropagation()}>
                      <input
                        type="number"
                        step="0.01"
                        min="0"
                        value={usdEdits[item.id] !== undefined ? usdEdits[item.id] : (usdValues[item.id] ?? item.unit_price_usd ?? '')}
                        placeholder="—"
                        onChange={e => setUsdEdits(prev => ({ ...prev, [item.id]: e.target.value }))}
                        onBlur={() => handleSaveUsdPre(item.id)}
                        onKeyDown={e => { if (e.key === 'Enter') handleSaveUsdPre(item.id) }}
                        className="w-20 text-right text-xs rounded px-1 py-0.5 font-mono outline-none"
                        style={{ backgroundColor: 'var(--surface)', border: '1px solid var(--border)', color: 'var(--text-primary)' }}
                      />
                    </td>
                    {/* A-3.2: Total USD computed */}
                    <td className="px-3 py-2.5 text-right font-mono font-semibold text-emerald-400">
                      {(() => {
                        const u = usdValues[item.id] ?? item.unit_price_usd
                        return u != null ? ('$' + (u * item.cantidad).toLocaleString('en-US', { minimumFractionDigits: 2 })) : '—'
                      })()}
                    </td>
                    {!isEmbarcado && (
                      <td className="px-3 py-2.5 text-center">
                        <button
                          onClick={() => handleRemoveItem(item.id)}
                          disabled={loadingItem === item.id}
                          className="p-1 rounded-md transition-colors hover:bg-red-500/10 text-red-400/40 hover:text-red-400"
                          title="Quitar del pre-embarque"
                        >
                          {loadingItem === item.id
                            ? <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                            : <Minus className="w-3.5 h-3.5" />}
                        </button>
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
              <tfoot>
                <tr style={{ borderTop: '2px solid var(--border)', backgroundColor: 'var(--surface-200)' }}>
                  <td colSpan={!isEmbarcado ? 7 : 7} className="px-3 py-2 text-xs font-semibold text-right" style={{ color: 'var(--text-muted)' }}>TOTALES</td>
                  <td className="px-3 py-2 text-xs font-bold text-right font-mono" style={{ color: 'var(--text-primary)' }}>
                    {fmtKgTotal(pre.peso_total_kg)}
                  </td>
                  <td className="px-3 py-2 text-xs font-bold text-right font-mono text-brand-400">
                    {pre.total_usd_exwork > 0 ? '$' + pre.total_usd_exwork.toLocaleString('en-US', { minimumFractionDigits: 2 }) : '—'}
                  </td>
                </tr>
              </tfoot>
            </table>
          </div>
        )}
        {/* Add items panel — only for open pre-embarques */}
        {!isEmbarcado && open && (
          <div className="border-t" style={{ borderColor: 'var(--border)' }}>
            <button
              onClick={() => setShowAddItems(s => !s)}
              className="flex items-center gap-2 px-4 py-2.5 w-full text-left text-xs font-semibold transition-colors hover:bg-[var(--surface-200)]"
              style={{ color: 'var(--empresa-primary)' }}
            >
              <PlusCircle className="w-3.5 h-3.5" />
              {showAddItems ? 'Ocultar panel de agregado' : `Agregar ítems (${addableItems.length} disponibles)`}
            </button>
            {showAddItems && (
              addableItems.length === 0 ? (
                <p className="px-4 py-3 text-xs" style={{ color: 'var(--text-faint)' }}>No hay ítems en estado preparado disponibles para agregar.</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead>
                      <tr style={{ backgroundColor: 'var(--surface-300)', borderBottom: '1px solid var(--border)' }}>
                        {['N° Parte', 'Descripción', 'Proveedor', 'Qty', 'Días Rest.', ''].map(h => (
                          <th key={h} className="px-3 py-2 text-left font-semibold uppercase tracking-wider whitespace-nowrap" style={{ color: 'var(--text-faint)' }}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {addableItems.map((item, idx) => (
                        <tr key={item.id} style={{ borderBottom: idx < addableItems.length - 1 ? '1px solid var(--border)' : undefined, backgroundColor: 'var(--surface-50)' }}>
                          <td className="px-3 py-2 font-mono font-semibold text-brand-400">{item.numero_parte}</td>
                          <td className="px-3 py-2 max-w-[180px] truncate" style={{ color: 'var(--text-primary)' }}>{item.descripcion}</td>
                          <td className="px-3 py-2 whitespace-nowrap">
                            <p className="font-semibold" style={{ color: 'var(--text-primary)' }}>{item.oc_proveedor_nombre || '—'}</p>
                            {item.oc_proveedor_numero && <p className="text-[10px] font-mono" style={{ color: 'var(--text-faint)' }}>{item.oc_proveedor_numero}</p>}
                          </td>
                          <td className="px-3 py-2 text-center" style={{ color: 'var(--text-muted)' }}>{item.cantidad}</td>
                          <td className="px-3 py-2"><DiasRestantes dias={item.dias_restantes} /></td>
                          <td className="px-3 py-2 text-right">
                            <button
                              onClick={() => handleAddItem(item.id)}
                              disabled={loadingItem === item.id}
                              className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-[11px] font-semibold transition-colors bg-brand-500/10 text-brand-400 hover:bg-brand-500/20 border border-brand-500/30"
                            >
                              {loadingItem === item.id
                                ? <RefreshCw className="w-3 h-3 animate-spin" />
                                : <PlusCircle className="w-3 h-3" />}
                              Agregar
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )
            )}
          </div>
        )}
      </div>

      {showCerrar && (
        <CerrarEmbarqueModal pre={pre} onClose={() => setShowCerrar(false)}
          onSuccess={() => { setShowCerrar(false); onRefresh() }} />
      )}
    </>
  )
}

// ── Main ──────────────────────────────────────────────────────────────────

export default function PreEmbarquesPage() {
  const [items, setItems] = useState<PrepItem[]>([])
  const [preEmbarques, setPreEmbarques] = useState<PreEmbarque[]>([])
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [showModal, setShowModal] = useState(false)
  const [showEmbarqueModal, setShowEmbarqueModal] = useState(false)
  const [q, setQ] = useState('')
  const [sortKey, setSortKey] = useState<SortKey>('dias_restantes')
  const [sortDir, setSortDir] = useState<SortDir>('asc')
  const [groupByProv, setGroupByProv] = useState(false)

  const load = useCallback(() => {
    setLoading(true)
    Promise.all([comprasAPI.getItemsPreparados(), comprasAPI.listPreEmbarques()])
      .then(([r1, r2]) => { setItems(r1.data); setPreEmbarques(r2.data); setSelected(new Set()) })
      .catch(() => toast.error('Error al cargar datos'))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load() }, [load])

  const toggleSort = (key: SortKey) => {
    if (sortKey === key) setSortDir(d => d === 'asc' ? 'desc' : 'asc')
    else { setSortKey(key); setSortDir('asc') }
  }

  const filtered = useMemo(() => {
    let list = items.filter(i => {
      if (!q) return true
      const lq = q.toLowerCase()
      return i.numero_parte.toLowerCase().includes(lq) || i.descripcion.toLowerCase().includes(lq) ||
        i.marca.toLowerCase().includes(lq) || i.oc_proveedor_nombre.toLowerCase().includes(lq) ||
        i.numero_cot.includes(lq) || i.cliente.toLowerCase().includes(lq)
    })
    list = [...list].sort((a, b) => {
      let av: any = a[sortKey]; let bv: any = b[sortKey]
      if (av == null) av = sortDir === 'asc' ? Infinity : -Infinity
      if (bv == null) bv = sortDir === 'asc' ? Infinity : -Infinity
      if (typeof av === 'string') return sortDir === 'asc' ? av.localeCompare(bv) : bv.localeCompare(av)
      return sortDir === 'asc' ? av - bv : bv - av
    })
    return list
  }, [items, q, sortKey, sortDir])

  const selectedItems = useMemo(() => filtered.filter(i => selected.has(i.id)), [filtered, selected])
  const totalPesoKg = useMemo(() =>
    selectedItems.reduce((s, i) => s + (i.peso_unit_lbs ? i.peso_unit_lbs * 0.453592 * (i.cantidad || 1) : 0), 0),
  [selectedItems])
  const totalUsdSelected = useMemo(() =>
    selectedItems.reduce((s, i) => s + (i.precio_unit_cotizacion * (i.cantidad || 1) || 0), 0),
  [selectedItems])

  const toggleItem = (id: number) => {
    setSelected(prev => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n })
  }
  const toggleAll = () => {
    if (selected.size === filtered.length) setSelected(new Set())
    else setSelected(new Set(filtered.map(i => i.id)))
  }

  const groups = useMemo(() => {
    if (!groupByProv) return null
    const map: Record<string, PrepItem[]> = {}
    filtered.forEach(i => { const k = i.oc_proveedor_nombre || '—'; if (!map[k]) map[k] = []; map[k].push(i) })
    return Object.entries(map).sort(([a], [b]) => a.localeCompare(b))
  }, [filtered, groupByProv])

  const TableRows = ({ rowItems }: { rowItems: PrepItem[] }) => (
    <>
      {rowItems.map((item, idx) => {
        const isChecked = selected.has(item.id)
        return (
          <tr key={item.id} onClick={() => toggleItem(item.id)} className="cursor-pointer transition-colors"
            style={{
              borderBottom: idx < rowItems.length - 1 ? '1px solid var(--border)' : undefined,
              backgroundColor: isChecked ? 'rgba(99,102,241,0.06)' : undefined,
            }}>
            <td className="px-3 py-2.5" onClick={e => e.stopPropagation()}>
              <input type="checkbox" checked={isChecked} onChange={() => toggleItem(item.id)}
                className="rounded w-3.5 h-3.5 accent-brand-500" />
            </td>
            <td className="px-3 py-2.5 font-mono text-xs text-brand-400 font-semibold whitespace-nowrap">{item.numero_parte || '—'}</td>
            <td className="px-3 py-2.5 max-w-[180px]" style={{ color: 'var(--text-primary)' }}>
              <p className="text-xs truncate">{item.descripcion || '—'}</p>
              {item.marca && <p className="text-[10px]" style={{ color: 'var(--text-faint)' }}>{item.marca}</p>}
            </td>
            <td className="px-3 py-2.5 text-xs whitespace-nowrap">
              <p className="font-semibold" style={{ color: 'var(--text-primary)' }}>{item.oc_proveedor_nombre || '—'}</p>
              {item.oc_proveedor_pais && <p className="text-[10px]" style={{ color: 'var(--text-faint)' }}>{item.oc_proveedor_pais}</p>}
            </td>
            <td className="px-3 py-2.5 text-xs whitespace-nowrap">
              {item.numero_oc_prov
                ? <span className="font-mono font-semibold text-brand-400">{item.numero_oc_prov}</span>
                : item.oc_proveedor_numero
                  ? <span className="font-mono text-brand-400">{item.oc_proveedor_numero}</span>
                  : <span style={{ color: 'var(--text-faint)' }}>—</span>}
            </td>
            <td className="px-3 py-2.5 text-xs text-center" style={{ color: 'var(--text-muted)' }}>{item.cantidad}</td>
            <td className="px-3 py-2.5 text-xs text-center" style={{ color: 'var(--text-muted)' }}>
              {item.plazo_dias_prov ? item.plazo_dias_prov + 'd' : '—'}
            </td>
            <td className="px-3 py-2.5 text-xs text-center" style={{ color: 'var(--text-muted)' }}>
              {item.plazo_entrega_max ? item.plazo_entrega_max + 'd' : '—'}
            </td>
            <td className="px-3 py-2.5 text-center"><DiasRestantes dias={item.dias_restantes} /></td>
            <td className="px-3 py-2.5 text-xs text-right font-semibold font-mono" style={{ color: 'var(--text-primary)' }}>
              {item.precio_unit_cotizacion > 0
                ? '$' + (item.precio_unit_cotizacion * item.cantidad).toLocaleString('en-US', { minimumFractionDigits: 2 })
                : '—'}
            </td>
            <td className="px-3 py-2.5 text-xs text-right font-mono" style={{ color: 'var(--text-muted)' }}>
              {item.peso_unit_lbs ? fmtKgItem(item.peso_unit_lbs, item.cantidad) : '—'}
            </td>
          </tr>
        )
      })}
    </>
  )

  const TableHead = () => (
    <tr style={{ backgroundColor: 'var(--surface-200)', borderBottom: '1px solid var(--border)' }}>
      <th className="px-3 py-2 w-8">
        <input type="checkbox"
          checked={filtered.length > 0 && selected.size === filtered.length}
          ref={el => { if (el) el.indeterminate = selected.size > 0 && selected.size < filtered.length }}
          onChange={toggleAll} className="rounded w-3.5 h-3.5 accent-brand-500" />
      </th>
      <SortHeader col="numero_parte" current={sortKey} dir={sortDir} onClick={toggleSort}>N° Parte</SortHeader>
      <SortHeader col="descripcion" current={sortKey} dir={sortDir} onClick={toggleSort}>Descripción</SortHeader>
      <SortHeader col="oc_proveedor_nombre" current={sortKey} dir={sortDir} onClick={toggleSort}>Proveedor</SortHeader>
      <th className="px-3 py-2 text-left text-xs font-semibold uppercase tracking-wider whitespace-nowrap" style={{ color: 'var(--text-faint)' }}>OCP</th>
      <th className="px-3 py-2 text-center text-xs font-semibold uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>Qty</th>
      <SortHeader col="plazo_dias_prov" current={sortKey} dir={sortDir} onClick={toggleSort}>Pzo. Prov.</SortHeader>
      <SortHeader col="plazo_entrega_max" current={sortKey} dir={sortDir} onClick={toggleSort}>Pzo. Máx.</SortHeader>
      <SortHeader col="dias_restantes" current={sortKey} dir={sortDir} onClick={toggleSort}>Días Rest.</SortHeader>
      <SortHeader col="total_cotizacion" current={sortKey} dir={sortDir} onClick={toggleSort}>Total USD</SortHeader>
      <SortHeader col="peso_unit_lbs" current={sortKey} dir={sortDir} onClick={toggleSort}>Peso (kg)</SortHeader>
    </tr>
  )

  return (
    <div className="space-y-6">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold" style={{ color: 'var(--text-primary)' }}>Pre-Embarques</h1>
          <p className="text-sm mt-1" style={{ color: 'var(--text-muted)' }}>
            Selecciona ítems preparados · genera pre-embarque · cierra a embarque con INVOX y documentación
          </p>
        </div>
        <button onClick={load} className="btn-secondary flex items-center gap-2 self-start">
          <RefreshCw className="w-4 h-4" />
        </button>
      </div>

      {/* Sección 1: Ítems preparados */}
      <div className="space-y-3">
        <div className="flex items-center gap-2">
          <div className="w-1 h-5 rounded-full bg-brand-500" />
          <h2 className="text-sm font-bold uppercase tracking-wider" style={{ color: 'var(--text-muted)' }}>
            Ítems preparados disponibles
          </h2>
        </div>
        <div className="flex flex-col sm:flex-row items-start sm:items-center gap-2">
          <div className="relative flex-1 min-w-0">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4" style={{ color: 'var(--text-faint)' }} />
            <input className="input w-full pl-9" placeholder="Buscar por N° parte, descripción, proveedor, COT…"
              value={q} onChange={e => setQ(e.target.value)} />
          </div>
          <button onClick={() => setGroupByProv(g => !g)}
            className={'flex items-center gap-2 px-3 py-2 rounded-xl text-xs font-semibold border transition-colors shrink-0 ' + (
              groupByProv ? 'border-brand-500 bg-brand-500/10 text-brand-400' : 'border-[var(--border)] text-[var(--text-muted)]'
            )}>
            <Filter className="w-3.5 h-3.5" />Agrupar por proveedor
          </button>
        </div>

        {selected.size > 0 && (
          <div className="flex items-center justify-between p-3 rounded-xl bg-brand-500/10 border border-brand-500/20">
            <div className="flex items-center gap-4 flex-wrap">
              <p className="text-sm text-brand-400 font-medium">{selected.size} ítem(s) seleccionados</p>
              {totalPesoKg > 0 && (
                <span className="flex items-center gap-1 text-xs" style={{ color: 'var(--text-muted)' }}>
                  <Scale className="w-3.5 h-3.5" /><strong>{totalPesoKg.toFixed(2)} kg</strong>
                </span>
              )}
              {totalUsdSelected > 0 && (
                <span className="flex items-center gap-1 text-xs" style={{ color: 'var(--text-muted)' }}>
                  <DollarSign className="w-3.5 h-3.5" />
                  <strong>${totalUsdSelected.toLocaleString('en-US', { minimumFractionDigits: 2 })} USD</strong>
                </span>
              )}
            </div>
            <div className="flex items-center gap-2">
              <button onClick={() => setSelected(new Set())} className="btn-secondary text-xs px-3 py-1.5">Limpiar</button>
              <button onClick={() => setShowModal(true)} className="btn-primary text-xs px-4 py-1.5 flex items-center gap-1.5">
                <Package className="w-3.5 h-3.5" />Generar Pre-Embarque
              </button>
              <button onClick={() => setShowEmbarqueModal(true)} className="btn-secondary text-xs px-4 py-1.5 flex items-center gap-1.5 border-emerald-500/40 text-emerald-400 hover:bg-emerald-500/10">
                <Ship className="w-3.5 h-3.5" />Generar Embarque
              </button>
            </div>
          </div>
        )}

        {loading ? (
          <div className="flex items-center justify-center py-14">
            <Loader2 className="w-6 h-6 animate-spin" style={{ color: 'var(--text-faint)' }} />
          </div>
        ) : filtered.length === 0 ? (
          <div className="rounded-2xl border p-10 text-center space-y-3"
            style={{ backgroundColor: 'var(--surface-50)', borderColor: 'var(--border)' }}>
            <CheckCircle2 className="w-8 h-8 mx-auto text-emerald-500" />
            <p className="font-semibold" style={{ color: 'var(--text-primary)' }}>Sin ítems preparados</p>
            <p className="text-sm" style={{ color: 'var(--text-muted)' }}>
              {items.length === 0 ? 'Marca ítems como preparados desde Seguimiento.' : 'Ajusta el término de búsqueda.'}
            </p>
          </div>
        ) : groupByProv && groups ? (
          <div className="space-y-4">
            {groups.map(([provNombre, provItems]) => (
              <div key={provNombre} className="rounded-2xl border overflow-hidden"
                style={{ backgroundColor: 'var(--surface-50)', borderColor: 'var(--border)' }}>
                <div className="px-4 py-2 flex items-center justify-between"
                  style={{ backgroundColor: 'var(--surface-200)', borderBottom: '1px solid var(--border)' }}>
                  <span className="font-semibold text-sm" style={{ color: 'var(--text-primary)' }}>{provNombre}</span>
                  <span className="text-xs" style={{ color: 'var(--text-muted)' }}>{provItems.length} ítems</span>
                </div>
                <div className="overflow-x-auto">
                  <table className="w-full text-sm"><thead><TableHead /></thead><tbody><TableRows rowItems={provItems} /></tbody></table>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="rounded-2xl border overflow-hidden"
            style={{ backgroundColor: 'var(--surface-50)', borderColor: 'var(--border)' }}>
            <div className="overflow-x-auto">
              <table className="w-full text-sm"><thead><TableHead /></thead><tbody><TableRows rowItems={filtered} /></tbody></table>
            </div>
          </div>
        )}
      </div>

      {/* Sección 2: Pre-Embarques generados */}
      {preEmbarques.length > 0 && (
        <div className="space-y-3">
          <div className="border-t pt-5" style={{ borderColor: 'var(--border)' }}>
            <div className="flex items-center gap-2 mb-1">
              <div className="w-1 h-5 rounded-full bg-amber-500" />
              <h2 className="text-sm font-bold uppercase tracking-wider" style={{ color: 'var(--text-muted)' }}>
                Pre-Embarques generados
              </h2>
              <span className="text-xs px-2 py-0.5 rounded-full bg-amber-500/10 text-amber-400 border border-amber-500/20 font-semibold">
                {preEmbarques.filter(p => p.estado !== 'embarcado').length} abierto(s)
              </span>
            </div>
            <p className="text-xs mb-3 flex items-center gap-1" style={{ color: 'var(--text-muted)' }}>
              <FileText className="w-3 h-3" />Adjunta documentación e INVOX por OCP para cerrar a Embarque
            </p>
          </div>
          <div className="space-y-3">
            {preEmbarques.map(pre => (
              <PreEmbarqueCard key={pre.id} pre={pre} availableItems={items} onRefresh={load} />
            ))}
          </div>
        </div>
      )}

      {showModal && (
        <GenPreEmbarqueModal selectedItems={selectedItems}
          onClose={() => setShowModal(false)}
          onSuccess={() => { setShowModal(false); load() }} />
      )}
      {showEmbarqueModal && (
        <GenEmbarqueModal items={filtered.filter(i => selected.has(i.id))}
          onClose={() => setShowEmbarqueModal(false)}
          onSuccess={() => { setShowEmbarqueModal(false); setSelected(new Set()); load() }} />
      )}
    </div>
  )
}
