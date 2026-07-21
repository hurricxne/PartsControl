import { useState, useEffect, useCallback, useRef } from 'react'
import {
  Package, CheckCircle2, ChevronDown, ChevronRight,
  Loader2, Check, AlertTriangle, Receipt,
  FileText, Upload, Plus, Trash2, X, Edit2, Truck,
} from 'lucide-react'
import toast from 'react-hot-toast'
import { comprasAPI, facturasAPI, recepcionNacionalAPI, despachosAPI } from '../services/api'

// ── Types ────────────────────────────────────────────────────────────────────

interface SeguimientoItem {
  id: number
  item_num: number
  numero_parte: string
  descripcion: string
  marca: string
  cantidad: number
  estado_item: string
  numero_cot: string
  cliente: string
  plazo_entrega_max: number | null
  plazo_dias_prov: number | null
  dias_restantes: number | null
  peso_unit_lbs: number | null
  precio_unit_cotizacion: number
  total_cotizacion: number
  ocp_numero: string
  ocp_numero_oc: string
  ocp_proveedor: string
  numero_oc_cliente: string
  ocp_item_id: number | null   // OcProveedorItem.id
  unit_price_usd: number | null
  dias_transcurridos: number | null
}

interface OcpGroup {
  oc_proveedor_id: number | null
  numero: string
  oc_proveedor_numero: string
  oc_proveedor_nombre: string
  proveedor: string
  numero_oc_prov: string
  numero_oc: string
  tipo_origen?: string   // 'nacional' → CTA "Registrar entrega nacional" (salta embarque)
  items: SeguimientoItem[]
}

// ── Recepción nacional (pendientes por recibir de una OC nacional) ─────────────
interface PendienteNacionalItem {
  item_cotizacion_id: number
  oc_proveedor_item_id: number | null
  numero_parte: string | null
  descripcion: string | null
  estado_item: string
  cantidad: number
  recibido: number
  remanente: number
}

// Los 6 estados de recepción. Los "utilizables" (suman al tope físico en Despachos)
// exigen cantidad > 0; no_llego / dañado_no_utilizable no cuentan (quedan 'comprado').
const ESTADOS_RECEPCION: { value: string; label: string; utilizable: boolean }[] = [
  { value: 'completo',              label: 'Completo',                utilizable: true },
  { value: 'faltante',              label: 'Faltante (llegó menos)',  utilizable: true },
  { value: 'sobrante',              label: 'Sobrante (llegó más)',    utilizable: true },
  { value: 'danado_utilizable',     label: 'Dañado pero utilizable',  utilizable: true },
  { value: 'danado_no_utilizable',  label: 'Dañado no utilizable',    utilizable: false },
  { value: 'no_llego',              label: 'No llegó',                utilizable: false },
]
const ESTADO_UTILIZABLE = new Set(
  ESTADOS_RECEPCION.filter(e => e.utilizable).map(e => e.value)
)

// ── Factura types ─────────────────────────────────────────────────────────────

interface FacturaItem {
  id: number
  ocp_item_id: number | null
  descripcion: string | null
  qty_facturada: number
  qty_sistema: number | null
  weight_lbs: number | null
  unit_price_usd: number | null
  freight_prorrateado_usd: number | null
  notas: string | null
  match: boolean
}

interface Factura {
  id: number
  ocp_id: number
  invoice_no: string
  fecha: string | null
  total_usd: number | null
  freight_usd: number | null
  archivo: string | null
  notas: string | null
  created_at: string | null
  items: FacturaItem[]
}

interface ParsedRow {
  invoice_no: string
  fecha: string
  customer_po: string
  total_usd: number | null
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function fmtKg(lbs: number | null, qty: number): string {
  if (!lbs) return '—'
  return (lbs * 0.453592 * qty).toFixed(2) + ' kg'
}

function fmtUsd(v: number | null): string {
  if (!v) return '—'
  return '$' + v.toLocaleString('en-US', { minimumFractionDigits: 2 })
}

function DiasRestantes({ dias }: { dias: number | null }) {
  if (dias == null) return <span style={{ color: 'var(--text-faint)' }}>—</span>
  if (dias <= 0) return <span className="text-red-400 font-bold text-xs">Vencido</span>
  if (dias <= 5) return <span className="text-red-400 font-semibold text-xs">{dias}d ⚠</span>
  if (dias <= 15) return <span className="text-amber-400 font-semibold text-xs">{dias}d</span>
  return <span className="text-emerald-400 text-xs">{dias}d</span>
}

function DeltaPlazo({ prometido, transcurrido }: { prometido: number | null; transcurrido: number | null }) {
  if (prometido == null || transcurrido == null) return <span style={{ color: 'var(--text-faint)' }}>—</span>
  const delta = transcurrido - prometido
  if (delta < 0) return <span className="text-emerald-400 font-semibold text-xs" title={`${Math.abs(delta)}d adelantado`}>▲ {Math.abs(delta)}d</span>
  if (delta === 0) return <span className="text-emerald-400 text-xs">En plazo</span>
  if (delta <= 3) return <span className="text-amber-400 font-semibold text-xs" title={`${delta}d de atraso`}>⚠ {delta}d</span>
  return <span className="text-red-400 font-bold text-xs" title={`${delta}d de atraso`}>● {delta}d atraso</span>
}

function MatchIcon({ match, qtySistema, qtyFacturada }: {
  match: boolean; qtySistema: number | null; qtyFacturada: number
}) {
  if (qtySistema === null)
    return <span title="Sin referencia" className="text-amber-400 text-[10px]">—</span>
  if (match)
    return <span title={`OK: ${qtyFacturada} = ${qtySistema}`}><CheckCircle2 className="w-4 h-4 text-emerald-400" /></span>
  return <span title={`Discrepancia: facturado ${qtyFacturada} ≠ sistema ${qtySistema}`}><AlertTriangle className="w-4 h-4 text-red-400" /></span>
}

// ── FacturasPanel ─────────────────────────────────────────────────────────────

function FacturasPanel({ ocpId, ocpItems }: { ocpId: number; ocpItems: SeguimientoItem[] }) {
  const [facturas, setFacturas]       = useState<Factura[]>([])
  const [loading, setLoading]         = useState(true)
  const [parsing, setParsing]         = useState(false)
  const [parsedRows, setParsedRows]   = useState<ParsedRow[] | null>(null)
  const [openIds, setOpenIds]         = useState<Set<number>>(new Set())
  const [showForm, setShowForm]       = useState(false)
  const [savingForm, setSavingForm]   = useState(false)
  const [deletingId, setDeletingId]   = useState<number | null>(null)
  const pdfRef = useRef<HTMLInputElement>(null)

  // New factura form
  const [fInvoice, setFInvoice]       = useState('')
  const [fFecha, setFFecha]           = useState('')
  const [fTotal, setFTotal]           = useState('')
  const [fFreight, setFFreight]       = useState('')

  // Inline item editing
  const [editingItem, setEditingItem] = useState<{ facturaId: number; item: FacturaItem } | null>(null)
  const [eQty, setEQty]               = useState('')
  const [eWeight, setEWeight]         = useState('')
  const [ePrice, setEPrice]           = useState('')

  const load = useCallback(() => {
    setLoading(true)
    facturasAPI.list(ocpId)
      .then(r => setFacturas(r.data))
      .catch(() => toast.error('Error al cargar facturas'))
      .finally(() => setLoading(false))
  }, [ocpId])

  useEffect(() => { load() }, [load])

  const toggleOpen = (id: number) =>
    setOpenIds(prev => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n })

  // ── PDF parse ──────────────────────────────────────────────────────────────
  const handleParsePdf = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    setParsing(true)
    try {
      const { data } = await facturasAPI.parsePdf(file)
      setParsedRows(data.parsed)
      if (data.count === 0) toast.error('No se encontraron facturas en el PDF')
      else toast.success(`${data.count} factura(s) extraídas del PDF`)
    } catch (err: any) {
      toast.error(err.response?.data?.detail || 'Error al parsear PDF')
    } finally {
      setParsing(false)
      if (pdfRef.current) pdfRef.current.value = ''
    }
  }

  const addParsedRow = async (row: ParsedRow) => {
    try {
      const { data } = await facturasAPI.create({
        ocp_id: ocpId, invoice_no: row.invoice_no,
        fecha: row.fecha || null, total_usd: row.total_usd, items: [],
      })
      setFacturas(prev => [...prev, data])
      setParsedRows(prev => prev ? prev.filter(r => r.invoice_no !== row.invoice_no) : null)
      toast.success(`Factura ${row.invoice_no} agregada`)
      setOpenIds(prev => new Set([...prev, data.id]))
    } catch (err: any) { toast.error(err.response?.data?.detail || 'Error') }
  }

  // ── Manual factura form ────────────────────────────────────────────────────
  const handleCreateFactura = async () => {
    if (!fInvoice.trim()) { toast.error('N° Factura es obligatorio'); return }
    setSavingForm(true)
    try {
      const { data } = await facturasAPI.create({
        ocp_id: ocpId, invoice_no: fInvoice.trim(),
        fecha: fFecha || null,
        total_usd: fTotal ? parseFloat(fTotal) : null,
        freight_usd: fFreight ? parseFloat(fFreight) : null,
        items: [],
      })
      setFacturas(prev => [...prev, data])
      toast.success(`Factura ${data.invoice_no} creada`)
      setShowForm(false); setFInvoice(''); setFFecha(''); setFTotal(''); setFFreight('')
      setOpenIds(prev => new Set([...prev, data.id]))
    } catch (err: any) { toast.error(err.response?.data?.detail || 'Error al crear factura') }
    finally { setSavingForm(false) }
  }

  // ── Delete factura ─────────────────────────────────────────────────────────
  const handleDelete = async (f: Factura) => {
    if (!confirm(`¿Eliminar factura ${f.invoice_no}?`)) return
    setDeletingId(f.id)
    try {
      await facturasAPI.remove(f.id)
      setFacturas(prev => prev.filter(x => x.id !== f.id))
      toast.success('Factura eliminada')
    } catch { toast.error('Error al eliminar') }
    finally { setDeletingId(null) }
  }

  // ── Build updated items list ───────────────────────────────────────────────
  const buildItems = (factura: Factura) =>
    factura.items.map(it => ({
      ocp_item_id: it.ocp_item_id, descripcion: it.descripcion,
      qty_facturada: it.qty_facturada, weight_lbs: it.weight_lbs,
      unit_price_usd: it.unit_price_usd, notas: it.notas,
    }))

  // ── Add OCP item to factura ────────────────────────────────────────────────
  const handleAddItem = async (factura: Factura, ocpItem: SeguimientoItem) => {
    try {
      const { data } = await facturasAPI.update(factura.id, {
        ocp_id: factura.ocp_id, invoice_no: factura.invoice_no,
        fecha: factura.fecha, total_usd: factura.total_usd,
        freight_usd: factura.freight_usd, notas: factura.notas,
        items: [
          ...buildItems(factura),
          { ocp_item_id: ocpItem.ocp_item_id, descripcion: ocpItem.descripcion,
            qty_facturada: ocpItem.cantidad, weight_lbs: ocpItem.peso_unit_lbs,
            unit_price_usd: null, notas: null },
        ],
      })
      setFacturas(prev => prev.map(f => f.id === factura.id ? data : f))
      toast.success(`${ocpItem.numero_parte} agregado a factura ${factura.invoice_no}`)
    } catch (err: any) { toast.error(err.response?.data?.detail || 'Error al agregar ítem') }
  }

  // ── Save inline item edit ──────────────────────────────────────────────────
  const handleSaveItem = async () => {
    if (!editingItem) return
    try {
      const { data } = await facturasAPI.updateItem(editingItem.facturaId, editingItem.item.id, {
        qty_facturada:  eQty    ? parseFloat(eQty)    : editingItem.item.qty_facturada,
        weight_lbs:     eWeight ? parseFloat(eWeight) : editingItem.item.weight_lbs,
        unit_price_usd: ePrice  ? parseFloat(ePrice)  : editingItem.item.unit_price_usd,
      })
      setFacturas(prev => prev.map(f => f.id === editingItem.facturaId ? data : f))
      setEditingItem(null)
    } catch { toast.error('Error al guardar') }
  }

  // ── Remove item from factura ───────────────────────────────────────────────
  const handleRemoveItem = async (factura: Factura, itemId: number) => {
    try {
      const { data } = await facturasAPI.update(factura.id, {
        ocp_id: factura.ocp_id, invoice_no: factura.invoice_no,
        fecha: factura.fecha, total_usd: factura.total_usd,
        freight_usd: factura.freight_usd, notas: factura.notas,
        items: buildItems(factura).filter((_, i) => factura.items[i]?.id !== itemId),
      })
      setFacturas(prev => prev.map(f => f.id === factura.id ? data : f))
    } catch { toast.error('Error al quitar ítem') }
  }

  // All ocp_item_ids already in some factura (to avoid duplicates)
  const allLinkedIds = new Set(
    facturas.flatMap(f => f.items.map(it => it.ocp_item_id).filter(Boolean))
  )

  const totalDiscrepancias = facturas.reduce(
    (s, f) => s + f.items.filter(it => !it.match && it.qty_sistema !== null).length, 0
  )

  return (
    <div className="border-t" style={{ borderColor: 'var(--border)' }}>

      {/* Panel header */}
      <div className="flex items-center justify-between px-4 py-2"
        style={{ backgroundColor: 'var(--surface-200)' }}>
        <div className="flex items-center gap-2">
          <Receipt className="w-3.5 h-3.5 text-brand-400" />
          <span className="text-xs font-semibold uppercase tracking-wider" style={{ color: 'var(--text-primary)' }}>
            Facturas de Proveedor
          </span>
          {facturas.length > 0 && (
            <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-brand-500/15 text-brand-400 font-bold">
              {facturas.length}
            </span>
          )}
          {totalDiscrepancias > 0 && (
            <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-red-500/15 text-red-400 font-bold flex items-center gap-0.5">
              <AlertTriangle className="w-2.5 h-2.5" />{totalDiscrepancias} discrepancia(s)
            </span>
          )}
        </div>
        <div className="flex items-center gap-1.5">
          <input ref={pdfRef} type="file" accept=".pdf" className="hidden" onChange={handleParsePdf} />
          <button onClick={() => pdfRef.current?.click()} disabled={parsing}
            className="flex items-center gap-1 text-[10px] px-2 py-1 rounded-lg border transition-colors hover:bg-[var(--surface-300)] disabled:opacity-50"
            style={{ borderColor: 'var(--border)', color: 'var(--text-muted)' }}
            title="Subir Sales Invoice List PDF">
            {parsing ? <Loader2 className="w-3 h-3 animate-spin" /> : <Upload className="w-3 h-3" />}
            {parsing ? 'Procesando...' : 'PDF Invoice List'}
          </button>
          <button onClick={() => setShowForm(v => !v)}
            className="flex items-center gap-1 text-[10px] px-2 py-1 rounded-lg border transition-colors hover:bg-[var(--surface-300)]"
            style={{ borderColor: 'var(--border)', color: 'var(--text-muted)' }}>
            <Plus className="w-3 h-3" />Manual
          </button>
        </div>
      </div>

      {/* Parsed PDF rows */}
      {parsedRows && parsedRows.length > 0 && (
        <div className="px-4 py-2.5 border-b space-y-1.5" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-50)' }}>
          <p className="text-[10px] font-semibold" style={{ color: 'var(--text-primary)' }}>
            Extraídas del PDF — agrega las que pertenecen a esta OCP:
          </p>
          {parsedRows.map(row => (
            <div key={row.invoice_no} className="flex items-center gap-3 text-[11px]">
              <span className="font-mono font-bold text-brand-400 w-16">{row.invoice_no}</span>
              <span style={{ color: 'var(--text-muted)' }} className="w-22">{row.fecha || '—'}</span>
              <span className="font-mono" style={{ color: 'var(--text-faint)' }}>PO:{row.customer_po}</span>
              <span className="font-mono" style={{ color: 'var(--text-muted)' }}>
                {row.total_usd ? `$${row.total_usd.toLocaleString('en-US')}` : '—'}
              </span>
              <button onClick={() => addParsedRow(row)}
                className="ml-auto text-[10px] px-2 py-0.5 rounded bg-brand-500/15 text-brand-400 hover:bg-brand-500/25 transition-colors">
                + Agregar
              </button>
            </div>
          ))}
          <button onClick={() => setParsedRows(null)} className="text-[10px] mt-1" style={{ color: 'var(--text-faint)' }}>
            Cerrar
          </button>
        </div>
      )}

      {/* Manual form */}
      {showForm && (
        <div className="px-4 py-2.5 border-b" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-50)' }}>
          <p className="text-[10px] font-semibold mb-2" style={{ color: 'var(--text-primary)' }}>Nueva factura</p>
          <div className="flex flex-wrap gap-2 items-end">
            <div>
              <p className="text-[9px] mb-0.5 uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>N° Factura *</p>
              <input value={fInvoice} onChange={e => setFInvoice(e.target.value)}
                placeholder="12345" className="input text-xs px-2 py-1 w-24" />
            </div>
            <div>
              <p className="text-[9px] mb-0.5 uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>Fecha</p>
              <input value={fFecha} onChange={e => setFFecha(e.target.value)}
                type="date" className="input text-xs px-2 py-1 w-32" />
            </div>
            <div>
              <p className="text-[9px] mb-0.5 uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>Total USD</p>
              <input value={fTotal} onChange={e => setFTotal(e.target.value)}
                placeholder="0.00" className="input text-xs px-2 py-1 w-20" />
            </div>
            <div>
              <p className="text-[9px] mb-0.5 uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>Freight USD</p>
              <input value={fFreight} onChange={e => setFFreight(e.target.value)}
                placeholder="0.00" className="input text-xs px-2 py-1 w-20" />
            </div>
            <button onClick={handleCreateFactura} disabled={savingForm}
              className="btn-primary text-xs px-3 py-1.5 flex items-center gap-1 disabled:opacity-50">
              {savingForm ? <Loader2 className="w-3 h-3 animate-spin" /> : <Check className="w-3 h-3" />}
              Guardar
            </button>
            <button onClick={() => setShowForm(false)} className="btn-secondary text-xs px-3 py-1.5">Cancelar</button>
          </div>
        </div>
      )}

      {/* Facturas list */}
      {loading ? (
        <div className="flex justify-center py-3">
          <Loader2 className="w-4 h-4 animate-spin" style={{ color: 'var(--text-faint)' }} />
        </div>
      ) : facturas.length === 0 ? (
        <p className="text-[11px] text-center py-3" style={{ color: 'var(--text-faint)' }}>
          Sin facturas registradas — sube PDF Invoice List o agrega manualmente.
        </p>
      ) : (
        <div>
          {facturas.map((f, fi) => {
            const isOpen = openIds.has(f.id)
            const matchAll  = f.items.length > 0 && f.items.every(it => it.match || it.qty_sistema === null)
            const hasDiscr  = f.items.some(it => !it.match && it.qty_sistema !== null)
            return (
              <div key={f.id} style={{ borderBottom: fi < facturas.length - 1 ? '1px solid var(--border)' : undefined }}>

                {/* Factura row header */}
                <div className="flex items-center gap-2 px-4 py-2 hover:bg-[var(--surface-200)] transition-colors cursor-pointer"
                  onClick={() => toggleOpen(f.id)}>
                  {isOpen ? <ChevronDown className="w-3 h-3 shrink-0" style={{ color: 'var(--text-faint)' }} />
                           : <ChevronRight className="w-3 h-3 shrink-0" style={{ color: 'var(--text-faint)' }} />}
                  <span className="font-mono font-bold text-[11px] text-brand-400 w-16">{f.invoice_no}</span>
                  <span className="text-[11px] w-20" style={{ color: 'var(--text-muted)' }}>{f.fecha || '—'}</span>
                  <span className="text-[11px] font-mono" style={{ color: 'var(--text-muted)' }}>
                    {f.total_usd ? `$${f.total_usd.toLocaleString('en-US')}` : '—'}
                  </span>
                  {f.freight_usd ? (
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-500/10 text-amber-400">
                      Frt ${f.freight_usd.toLocaleString('en-US')}
                    </span>
                  ) : null}
                  <span className="text-[10px]" style={{ color: 'var(--text-faint)' }}>{f.items.length} ítems</span>
                  {f.items.length > 0 && (matchAll
                    ? <span title="Cantidades OK"><CheckCircle2 className="w-3.5 h-3.5 text-emerald-400 shrink-0" /></span>
                    : hasDiscr
                      ? <span title="Discrepancias de cantidad"><AlertTriangle className="w-3.5 h-3.5 text-red-400 shrink-0" /></span>
                      : null)}
                  <button onClick={e => { e.stopPropagation(); handleDelete(f) }} disabled={deletingId === f.id}
                    className="ml-auto p-0.5 rounded hover:bg-red-500/10 transition-colors" title="Eliminar factura">
                    {deletingId === f.id
                      ? <Loader2 className="w-3 h-3 animate-spin text-red-400" />
                      : <Trash2 className="w-3 h-3 text-red-400 opacity-40 hover:opacity-100" />}
                  </button>
                </div>

                {/* Items detail */}
                {isOpen && (
                  <div className="px-4 pb-3 border-t" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-50)' }}>

                    {/* Quick-add buttons for OCP items not yet in this factura */}
                    {ocpItems.some(oi => oi.ocp_item_id && !allLinkedIds.has(oi.ocp_item_id)) && (
                      <div className="py-2">
                        <p className="text-[9px] uppercase tracking-wider mb-1.5 font-semibold" style={{ color: 'var(--text-faint)' }}>
                          Agregar ítems de esta OCP:
                        </p>
                        <div className="flex flex-wrap gap-1.5">
                          {ocpItems
                            .filter(oi => oi.ocp_item_id && !allLinkedIds.has(oi.ocp_item_id))
                            .map(oi => (
                              <button key={oi.id} onClick={() => handleAddItem(f, oi)}
                                className="text-[10px] px-2 py-0.5 rounded border flex items-center gap-1 hover:bg-brand-500/10 transition-colors"
                                style={{ borderColor: 'var(--border)', color: 'var(--text-muted)' }}>
                                <Plus className="w-2.5 h-2.5" />{oi.numero_parte} ×{oi.cantidad}
                              </button>
                            ))}
                        </div>
                      </div>
                    )}

                    {/* Items table */}
                    {f.items.length === 0 ? (
                      <p className="text-[11px] py-1.5" style={{ color: 'var(--text-faint)' }}>
                        Sin ítems — agrega desde los botones de arriba.
                      </p>
                    ) : (
                      <div className="overflow-x-auto mt-1">
                        <table className="w-full text-[11px]">
                          <thead>
                            <tr style={{ borderBottom: '1px solid var(--border)' }}>
                              {['N° Parte','Descripción','Qty Fac.','Qty Sis.','Estado','Peso lbs','P.Unit USD','Freight Pror.',''].map(h => (
                                <th key={h} className="py-1 pr-3 text-left text-[9px] font-semibold uppercase tracking-wider"
                                  style={{ color: 'var(--text-faint)' }}>{h}</th>
                              ))}
                            </tr>
                          </thead>
                          <tbody>
                            {f.items.map(it => {
                              const linked = ocpItems.find(oi => oi.ocp_item_id === it.ocp_item_id)
                              const isEd   = editingItem?.facturaId === f.id && editingItem?.item.id === it.id
                              return (
                                <tr key={it.id} style={{ borderBottom: '1px solid var(--border)' }}>
                                  <td className="py-1.5 pr-3 font-mono font-bold text-brand-400 whitespace-nowrap">
                                    {linked?.numero_parte || '—'}
                                  </td>
                                  <td className="py-1.5 pr-3 max-w-[130px] truncate" style={{ color: 'var(--text-primary)' }}
                                    title={it.descripcion || ''}>
                                    {it.descripcion || '—'}
                                  </td>
                                  <td className="py-1.5 pr-3 text-center">
                                    {isEd ? (
                                      <input value={eQty} onChange={e => setEQty(e.target.value)}
                                        className="input text-[10px] w-14 px-1 py-0.5" />
                                    ) : (
                                      <span className="font-semibold" style={{ color: 'var(--text-primary)' }}>{it.qty_facturada}</span>
                                    )}
                                  </td>
                                  <td className="py-1.5 pr-3 text-center" style={{ color: 'var(--text-muted)' }}>
                                    {it.qty_sistema ?? '—'}
                                  </td>
                                  <td className="py-1.5 pr-3">
                                    <MatchIcon match={it.match} qtySistema={it.qty_sistema} qtyFacturada={it.qty_facturada} />
                                  </td>
                                  <td className="py-1.5 pr-3 text-right font-mono" style={{ color: 'var(--text-muted)' }}>
                                    {isEd ? (
                                      <input value={eWeight} onChange={e => setEWeight(e.target.value)}
                                        className="input text-[10px] w-14 px-1 py-0.5" />
                                    ) : (
                                      it.weight_lbs?.toFixed(3) ?? '—'
                                    )}
                                  </td>
                                  <td className="py-1.5 pr-3 text-right font-mono" style={{ color: 'var(--text-muted)' }}>
                                    {isEd ? (
                                      <input value={ePrice} onChange={e => setEPrice(e.target.value)}
                                        className="input text-[10px] w-18 px-1 py-0.5" />
                                    ) : (
                                      it.unit_price_usd ? `$${it.unit_price_usd.toFixed(4)}` : '—'
                                    )}
                                  </td>
                                  <td className="py-1.5 pr-3 text-right font-mono text-amber-400">
                                    {it.freight_prorrateado_usd ? `$${it.freight_prorrateado_usd.toFixed(4)}` : '—'}
                                  </td>
                                  <td className="py-1.5">
                                    <div className="flex items-center gap-0.5">
                                      {isEd ? (
                                        <>
                                          <button onClick={handleSaveItem}
                                            className="p-0.5 rounded text-emerald-400 hover:bg-emerald-500/10">
                                            <Check className="w-3 h-3" />
                                          </button>
                                          <button onClick={() => setEditingItem(null)}
                                            className="p-0.5 rounded hover:bg-[var(--surface-300)]">
                                            <X className="w-3 h-3" style={{ color: 'var(--text-faint)' }} />
                                          </button>
                                        </>
                                      ) : (
                                        <>
                                          <button onClick={() => {
                                            setEditingItem({ facturaId: f.id, item: it })
                                            setEQty(String(it.qty_facturada))
                                            setEWeight(it.weight_lbs ? String(it.weight_lbs) : '')
                                            setEPrice(it.unit_price_usd ? String(it.unit_price_usd) : '')
                                          }} className="p-0.5 rounded hover:bg-[var(--surface-300)]" title="Editar">
                                            <Edit2 className="w-3 h-3" style={{ color: 'var(--text-faint)' }} />
                                          </button>
                                          <button onClick={() => handleRemoveItem(f, it.id)}
                                            className="p-0.5 rounded hover:bg-red-500/10" title="Quitar">
                                            <X className="w-3 h-3 text-red-400 opacity-50 hover:opacity-100" />
                                          </button>
                                        </>
                                      )}
                                    </div>
                                  </td>
                                </tr>
                              )
                            })}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

// ── RegistrarEntregaNacionalModal ─────────────────────────────────────────────
// El proveedor nacional llega con su camión y su guía de despacho. Bodega registra
// cuánto llegó de cada ítem; al cerrar (cerrar:true) los utilizables con qty>0 pasan
// a en_bodega y quedan despachables, capados por lo recibido. Salta preparado /
// pre-embarque / embarque. Fuente: GET /recepcion-nacional/pendientes/{ocp_id}.

interface RowEntrega { incluir: boolean; qty: string; estado: string }

function RegistrarEntregaNacionalModal({
  ocpId, titulo, onClose, onSuccess,
}: {
  ocpId: number
  titulo: string
  onClose: () => void
  onSuccess: () => void
}) {
  const hoy = () => {
    const d = new Date()
    const off = d.getTimezoneOffset()
    return new Date(d.getTime() - off * 60_000).toISOString().slice(0, 10)
  }
  const [pendientes, setPendientes] = useState<PendienteNacionalItem[]>([])
  const [rows, setRows] = useState<Record<number, RowEntrega>>({})
  const [loading, setLoading] = useState(true)
  const [numeroGuia, setNumeroGuia] = useState('')
  const [fecha, setFecha] = useState(hoy())
  const [documento, setDocumento] = useState<string | null>(null)
  const [docNombre, setDocNombre] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)
  const [saving, setSaving] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    setLoading(true)
    recepcionNacionalAPI.pendientes(ocpId)
      .then(({ data }) => {
        const items: PendienteNacionalItem[] = data.items || []
        setPendientes(items)
        const init: Record<number, RowEntrega> = {}
        items.forEach(it => {
          init[it.item_cotizacion_id] = {
            incluir: it.remanente > 0,
            qty: it.remanente > 0 ? String(it.remanente) : '',
            estado: 'completo',
          }
        })
        setRows(init)
      })
      .catch((e: any) => toast.error(e?.response?.data?.detail || 'Error al cargar ítems pendientes'))
      .finally(() => setLoading(false))
  }, [ocpId])

  const setRow = (id: number, patch: Partial<RowEntrega>) =>
    setRows(prev => ({ ...prev, [id]: { ...prev[id], ...patch } }))

  const onEstadoChange = (id: number, estado: string) => {
    // 'No llegó' no cuenta al tope: fuerza cantidad 0 y bloquea el input.
    if (estado === 'no_llego') setRow(id, { estado, qty: '0' })
    else setRow(id, { estado })
  }

  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    setUploading(true)
    try {
      const data = await despachosAPI.uploadDoc(file)
      setDocumento(data.filename)
      setDocNombre(data.original || file.name)
      toast.success('Guía adjuntada')
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || 'Error al subir la guía')
    } finally {
      setUploading(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  const incluidos = pendientes.filter(it => rows[it.item_cotizacion_id]?.incluir)

  const submit = async () => {
    if (incluidos.length === 0) {
      toast.error('Marca al menos un ítem que haya llegado en esta entrega')
      return
    }
    // Validación espejo del backend: un estado utilizable exige cantidad > 0.
    for (const it of incluidos) {
      const r = rows[it.item_cotizacion_id]
      const qty = Number(r.qty)
      if (isNaN(qty) || qty < 0) {
        toast.error(`Cantidad inválida en ${it.numero_parte || it.item_cotizacion_id}`)
        return
      }
      if (ESTADO_UTILIZABLE.has(r.estado) && qty <= 0) {
        toast.error(`${it.numero_parte || it.item_cotizacion_id}: "${ESTADOS_RECEPCION.find(e => e.value === r.estado)?.label}" exige cantidad mayor a 0`)
        return
      }
    }
    setSaving(true)
    try {
      await recepcionNacionalAPI.registrar({
        oc_proveedor_id: ocpId,
        numero_guia_proveedor: numeroGuia.trim() || undefined,
        fecha: fecha || undefined,
        documento: documento || undefined,
        cerrar: true,
        items: incluidos.map(it => ({
          item_cotizacion_id: it.item_cotizacion_id,
          qty_recibida: Number(rows[it.item_cotizacion_id].qty) || 0,
          estado_recepcion: rows[it.item_cotizacion_id].estado,
        })),
      })
      toast.success('Entrega nacional registrada')
      onSuccess()
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || 'Error al registrar la entrega')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ background: 'rgba(0,0,0,0.6)', backdropFilter: 'blur(4px)' }}
      onClick={e => { if (e.target === e.currentTarget) onClose() }}
    >
      <div
        className="w-full max-w-2xl rounded-2xl border shadow-2xl p-6 space-y-5 max-h-[90vh] overflow-y-auto"
        style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}
      >
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <div className="w-9 h-9 rounded-xl bg-brand-500/10 flex items-center justify-center shrink-0">
              <Truck className="w-4 h-4 text-brand-400" />
            </div>
            <div>
              <h3 className="font-semibold text-base" style={{ color: 'var(--text-primary)' }}>
                Registrar entrega nacional
              </h3>
              <p className="text-xs" style={{ color: 'var(--text-muted)' }}>{titulo}</p>
            </div>
          </div>
          <button onClick={onClose} className="rounded-lg p-1 hover:bg-[var(--surface-200)] transition-colors">
            <X className="w-4 h-4" style={{ color: 'var(--text-muted)' }} />
          </button>
        </div>

        {/* Cabecera de la guía */}
        <div className="grid sm:grid-cols-3 gap-3">
          <div>
            <label className="block text-[11px] font-semibold uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>
              N° guía del proveedor
            </label>
            <input className="input w-full" placeholder="Ej: 12345" value={numeroGuia} onChange={e => setNumeroGuia(e.target.value)} />
          </div>
          <div>
            <label className="block text-[11px] font-semibold uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>
              Fecha de recepción
            </label>
            <input type="date" className="input w-full" value={fecha} onChange={e => setFecha(e.target.value)} />
          </div>
          <div>
            <label className="block text-[11px] font-semibold uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>
              Guía escaneada (opcional)
            </label>
            <input ref={fileRef} type="file" className="hidden"
              accept=".pdf,.jpg,.jpeg,.png,.webp" onChange={handleUpload} />
            <button type="button" onClick={() => fileRef.current?.click()} disabled={uploading}
              className="w-full flex items-center justify-center gap-1.5 text-xs px-2 py-2 rounded-lg border transition-colors hover:bg-[var(--surface-200)] disabled:opacity-50"
              style={{ borderColor: 'var(--border)', color: 'var(--text-muted)' }}>
              {uploading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Upload className="w-3.5 h-3.5" />}
              {documento ? 'Cambiar' : 'Adjuntar'}
            </button>
            {docNombre && (
              <p className="text-[10px] mt-1 flex items-center gap-1 truncate" style={{ color: 'var(--text-faint)' }} title={docNombre}>
                <FileText className="w-2.5 h-2.5 shrink-0" /> {docNombre}
              </p>
            )}
          </div>
        </div>

        {/* Ítems por recibir */}
        {loading ? (
          <div className="flex justify-center py-8">
            <Loader2 className="w-6 h-6 animate-spin" style={{ color: 'var(--text-faint)' }} />
          </div>
        ) : pendientes.length === 0 ? (
          <p className="text-xs text-center py-6" style={{ color: 'var(--text-faint)' }}>
            No hay ítems pendientes de recibir en esta OC nacional.
          </p>
        ) : (
          <div className="rounded-xl border overflow-hidden" style={{ borderColor: 'var(--border)' }}>
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr style={{ backgroundColor: 'var(--surface-200)', borderBottom: '1px solid var(--border)' }}>
                    <th className="px-3 py-2 w-8">
                      <input type="checkbox"
                        checked={incluidos.length === pendientes.length && pendientes.length > 0}
                        onChange={e => {
                          const val = e.target.checked
                          setRows(prev => {
                            const next = { ...prev }
                            pendientes.forEach(it => { next[it.item_cotizacion_id] = { ...next[it.item_cotizacion_id], incluir: val } })
                            return next
                          })
                        }}
                        className="rounded" />
                    </th>
                    {['N° Parte', 'Descripción', 'Vendido', 'Recibido', 'Remanente', 'Llegó (qty)', 'Estado'].map(h => (
                      <th key={h} className="px-3 py-2 text-left font-semibold uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {pendientes.map((it, idx) => {
                    const r = rows[it.item_cotizacion_id] || { incluir: false, qty: '', estado: 'completo' }
                    const noLlego = r.estado === 'no_llego'
                    return (
                      <tr key={it.item_cotizacion_id}
                        style={{ borderBottom: idx < pendientes.length - 1 ? '1px solid var(--border)' : undefined, opacity: r.incluir ? 1 : 0.5 }}>
                        <td className="px-3 py-2">
                          <input type="checkbox" checked={r.incluir}
                            onChange={() => setRow(it.item_cotizacion_id, { incluir: !r.incluir })} className="rounded" />
                        </td>
                        <td className="px-3 py-2 font-mono text-brand-400 font-semibold whitespace-nowrap">{it.numero_parte || '—'}</td>
                        <td className="px-3 py-2 max-w-[160px] truncate" style={{ color: 'var(--text-primary)' }} title={it.descripcion || ''}>{it.descripcion || '—'}</td>
                        <td className="px-3 py-2 text-center" style={{ color: 'var(--text-muted)' }}>{it.cantidad}</td>
                        <td className="px-3 py-2 text-center" style={{ color: 'var(--text-muted)' }}>{it.recibido || 0}</td>
                        <td className="px-3 py-2 text-center font-semibold" style={{ color: 'var(--text-primary)' }}>{it.remanente}</td>
                        <td className="px-3 py-2">
                          <input type="number" min="0" step="any"
                            className="input w-20 py-1 text-xs"
                            value={r.qty}
                            disabled={!r.incluir || noLlego}
                            onChange={e => setRow(it.item_cotizacion_id, { qty: e.target.value })} />
                        </td>
                        <td className="px-3 py-2">
                          <select className="input py-1 text-xs" value={r.estado}
                            disabled={!r.incluir}
                            onChange={e => onEstadoChange(it.item_cotizacion_id, e.target.value)}>
                            {ESTADOS_RECEPCION.map(e => <option key={e.value} value={e.value}>{e.label}</option>)}
                          </select>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}

        <p className="text-[11px]" style={{ color: 'var(--text-faint)' }}>
          Al registrar, los ítems utilizables con cantidad recibida pasan a bodega y quedan
          despachables (topeados por lo recibido). "No llegó" y "Dañado no utilizable" no cuentan
          y siguen pendientes.
        </p>

        <div className="flex gap-3 pt-1">
          <button onClick={onClose} className="btn-secondary flex-1">Cancelar</button>
          <button onClick={submit} disabled={saving || loading || incluidos.length === 0}
            className="btn-primary flex-1 flex items-center justify-center gap-2 disabled:opacity-50">
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Truck className="w-4 h-4" />}
            {saving ? 'Registrando...' : `Registrar entrega (${incluidos.length})`}
          </button>
        </div>
      </div>
    </div>
  )
}

// ── OcpGroupCard ──────────────────────────────────────────────────────────────

function OcpGroupCard({
  group, selectedIds, onToggleItem, onToggleAll, onMarcarPreparado, saving, onRefresh,
}: {
  group: OcpGroup
  selectedIds: Set<number>
  onToggleItem: (id: number) => void
  onToggleAll: (group: OcpGroup) => void
  onMarcarPreparado: (items: { item_id: number; cantidad?: number }[]) => void
  saving: boolean
  onRefresh: () => void
}) {
  const [expanded, setExpanded] = useState(true)
  const [showEntrega, setShowEntrega] = useState(false)
  // Camino físico nacional: el flujo salta preparado/pre-embarque/embarque. En su lugar,
  // Bodega registra la entrega del camión (guía del proveedor) y los ítems recibidos
  // pasan directo a en_bodega, despachables.
  const esNacional = group.tipo_origen === 'nacional' && group.oc_proveedor_id != null
  const [qtyEdits, setQtyEdits] = useState<Record<number, string>>({})
  const [usdEdits, setUsdEdits] = useState<Record<number, string>>({})
  const [savingUsd, setSavingUsd] = useState<Record<number, boolean>>({})
  const [usdValues, setUsdValues] = useState<Record<number, number | null>>(
    Object.fromEntries(group.items.map(i => [i.id, i.unit_price_usd]))
  )
  const groupIds = group.items.map(i => i.id)

  const handleSaveUsd = async (itemId: number) => {
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
  const allSelected = groupIds.every(id => selectedIds.has(id))
  const someSelected = groupIds.some(id => selectedIds.has(id))
  const selectedInGroup = groupIds.filter(id => selectedIds.has(id))

  return (
    <div className="rounded-2xl border transition-shadow hover:shadow-md"
      style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>

      {/* Header */}
      <div className="flex items-center gap-3 p-4 cursor-pointer hover:bg-[var(--surface-200)] rounded-2xl transition-colors"
        onClick={() => setExpanded(v => !v)}>
        <div className="w-10 h-10 rounded-xl bg-brand-500/10 flex items-center justify-center shrink-0">
          <Package className="w-5 h-5 text-brand-400" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-mono font-bold text-sm text-brand-400">{group.numero_oc_prov || group.oc_proveedor_numero || group.numero}</span>
          </div>
          <p className="text-sm font-medium mt-0.5" style={{ color: 'var(--text-primary)' }}>
            {group.oc_proveedor_nombre || group.proveedor}
          </p>
          <p className="text-xs mt-0.5 flex items-center gap-1.5" style={{ color: 'var(--text-muted)' }}>
            {esNacional && (
              <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full text-[10px] font-semibold bg-brand-500/15 text-brand-400">
                <Truck className="w-2.5 h-2.5" /> Nacional
              </span>
            )}
            <span>{group.items.length} ítem(s) en estado <span className="text-amber-400 font-semibold">comprado</span></span>
          </p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {esNacional ? (
            <button onClick={e => { e.stopPropagation(); setShowEntrega(true) }}
              className="btn-primary text-xs px-3 py-1.5 flex items-center gap-1">
              <Truck className="w-3 h-3" /> Registrar entrega nacional
            </button>
          ) : selectedInGroup.length > 0 && (
            <button onClick={e => { e.stopPropagation(); onMarcarPreparado(selectedInGroup.map(id => ({ item_id: id, cantidad: qtyEdits[id] ? parseFloat(qtyEdits[id]) : undefined }))) }}
              disabled={saving} className="btn-primary text-xs px-3 py-1.5 flex items-center gap-1">
              {saving ? <Loader2 className="w-3 h-3 animate-spin" /> : <CheckCircle2 className="w-3 h-3" />}
              Preparar ({selectedInGroup.length})
            </button>
          )}
          <div style={{ color: 'var(--text-faint)' }}>
            {expanded ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
          </div>
        </div>
      </div>

      {expanded && (
        <>
          {/* Items table */}
          <div className="border-t" style={{ borderColor: 'var(--border)' }}>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr style={{ backgroundColor: 'var(--surface-200)', borderBottom: '1px solid var(--border)' }}>
                    <th className="px-3 py-2 text-left w-8">
                      {/* Nacional: sin selección — el botón GLOBAL "Preparar
                          seleccionados" los mandaría al embarque (el backend igual
                          los rechaza con 400, pero mejor no ofrecerlos). */}
                      {!esNacional && (
                        <input type="checkbox" checked={allSelected}
                          ref={el => { if (el) el.indeterminate = someSelected && !allSelected }}
                          onChange={() => onToggleAll(group)} className="rounded"
                          onClick={e => e.stopPropagation()} />
                      )}
                    </th>
                    {['N° Parte','Descripción','Marca','Qty','Qty Desp.','Peso','Unit USD','Total USD','Trans.','Delta','Pzo. Prov.','Pzo. Máx.','Días Rest.','COT','OC Cliente','Cliente'].map(h => (
                      <th key={h} className="px-3 py-2 text-left text-xs font-semibold uppercase tracking-wider"
                        style={{ color: 'var(--text-faint)' }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {group.items.map((item, idx) => (
                    <tr key={item.id}
                      className={`transition-colors ${esNacional ? '' : 'cursor-pointer'} ${selectedIds.has(item.id) ? 'bg-brand-500/5' : 'hover:bg-[var(--surface-200)]'}`}
                      style={{ borderBottom: idx < group.items.length - 1 ? '1px solid var(--border)' : undefined }}
                      onClick={() => { if (!esNacional) onToggleItem(item.id) }}>
                      <td className="px-3 py-2.5" onClick={e => e.stopPropagation()}>
                        {!esNacional && (
                          <input type="checkbox" checked={selectedIds.has(item.id)}
                            onChange={() => onToggleItem(item.id)} className="rounded" />
                        )}
                      </td>
                      <td className="px-3 py-2.5 font-mono text-xs text-brand-400 font-semibold whitespace-nowrap">
                        {item.numero_parte || '—'}
                      </td>
                      <td className="px-3 py-2.5 text-xs max-w-[160px]" style={{ color: 'var(--text-primary)' }}>
                        <p className="truncate">{item.descripcion || '—'}</p>
                      </td>
                      <td className="px-3 py-2.5 text-xs" style={{ color: 'var(--text-muted)' }}>{item.marca || '—'}</td>
                      <td className="px-3 py-2.5 text-xs text-center" style={{ color: 'var(--text-muted)' }}>{item.cantidad}</td>
                      <td className="px-3 py-2.5 text-xs text-center" onClick={e => e.stopPropagation()}>
                        <input
                          type="number"
                          min={1}
                          max={item.cantidad}
                          value={qtyEdits[item.id] ?? item.cantidad}
                          onChange={e => setQtyEdits(prev => ({ ...prev, [item.id]: e.target.value }))}
                          className="w-14 text-center text-xs rounded px-1 py-0.5 outline-none"
                          style={{ backgroundColor: 'var(--surface)', border: '1px solid var(--border)', color: 'var(--text-primary)' }}
                          title="Cantidad a preparar (mín. 1)"
                        />
                      </td>
                      <td className="px-3 py-2.5 text-xs text-right font-mono" style={{ color: 'var(--text-muted)' }}>
                        {fmtKg(item.peso_unit_lbs, item.cantidad)}
                      </td>
                      {/* A-2.4: Unit USD inline edit */}
                      <td className="px-3 py-2.5 text-xs text-right" onClick={e => e.stopPropagation()}>
                        <div className="flex items-center gap-1 justify-end">
                          <input
                            type="number"
                            step="0.01"
                            min="0"
                            value={usdEdits[item.id] !== undefined ? usdEdits[item.id] : (usdValues[item.id] ?? '')}
                            placeholder="—"
                            onChange={e => setUsdEdits(prev => ({ ...prev, [item.id]: e.target.value }))}
                            onBlur={() => handleSaveUsd(item.id)}
                            onKeyDown={e => { if (e.key === 'Enter') handleSaveUsd(item.id) }}
                            className="w-20 text-right text-xs rounded px-1 py-0.5 font-mono outline-none"
                            style={{ backgroundColor: 'var(--surface)', border: '1px solid var(--border)', color: 'var(--text-primary)' }}
                            title="Precio unitario USD de compra"
                          />
                          {savingUsd[item.id] && <Loader2 className="w-3 h-3 animate-spin text-brand-400 shrink-0" />}
                        </div>
                      </td>
                      {/* A-2.4: Total USD computed */}
                      <td className="px-3 py-2.5 text-xs text-right font-mono font-semibold" style={{ color: 'var(--text-primary)' }}>
                        {usdValues[item.id] != null
                          ? fmtUsd(usdValues[item.id]! * item.cantidad)
                          : <span style={{ color: 'var(--text-faint)' }}>—</span>}
                      </td>
                      {/* A-2.1: Días transcurridos */}
                      <td className="px-3 py-2.5 text-xs text-center font-mono" style={{ color: 'var(--text-muted)' }}>
                        {item.dias_transcurridos != null ? `${item.dias_transcurridos}d` : '—'}
                      </td>
                      {/* A-2.1: Delta semáforo */}
                      <td className="px-3 py-2.5 text-xs text-center">
                        <DeltaPlazo prometido={item.plazo_dias_prov} transcurrido={item.dias_transcurridos} />
                      </td>
                      <td className="px-3 py-2.5 text-xs text-center" style={{ color: 'var(--text-muted)' }}>
                        {item.plazo_dias_prov ? `${item.plazo_dias_prov}d` : '—'}
                      </td>
                      <td className="px-3 py-2.5 text-xs text-center" style={{ color: 'var(--text-muted)' }}>
                        {item.plazo_entrega_max ? `${item.plazo_entrega_max}d` : '—'}
                      </td>
                      <td className="px-3 py-2.5 text-xs text-center">
                        <DiasRestantes dias={item.dias_restantes} />
                      </td>
                      <td className="px-3 py-2.5 text-xs font-mono font-semibold text-brand-400">
                        {item.numero_cot ? `COT-${item.numero_cot}` : '—'}
                      </td>
                      <td className="px-3 py-2.5 text-xs font-mono" style={{ color: 'var(--text-muted)' }}>
                        {item.numero_oc_cliente || '—'}
                      </td>
                      <td className="px-3 py-2.5 text-xs" style={{ color: 'var(--text-muted)' }}>{item.cliente || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {/* Facturas panel USD (flujo internacional). Un grupo nacional no factura
              en USD ni se prorratea flete → se oculta. */}
          {group.oc_proveedor_id && !esNacional && (
            <FacturasPanel ocpId={group.oc_proveedor_id} ocpItems={group.items} />
          )}
        </>
      )}

      {showEntrega && group.oc_proveedor_id != null && (
        <RegistrarEntregaNacionalModal
          ocpId={group.oc_proveedor_id}
          titulo={`${group.numero_oc_prov || group.oc_proveedor_numero || group.numero} · ${group.oc_proveedor_nombre || group.proveedor}`}
          onClose={() => setShowEntrega(false)}
          onSuccess={() => { setShowEntrega(false); onRefresh() }}
        />
      )}
    </div>
  )
}

// ── Main Page ─────────────────────────────────────────────────────────────────

export default function SeguimientoPage() {
  const [groups, setGroups]     = useState<OcpGroup[]>([])
  const [loading, setLoading]   = useState(true)
  const [saving, setSaving]     = useState(false)
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())

  const load = useCallback(() => {
    setLoading(true)
    comprasAPI.getItemsComprados()
      .then(({ data }) => { setGroups(data); setSelectedIds(new Set()) })
      .catch(() => toast.error('Error al cargar seguimiento'))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load() }, [load])

  const toggleItem = (id: number) =>
    setSelectedIds(prev => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n })

  const toggleAll = (group: OcpGroup) => {
    const ids = group.items.map(i => i.id)
    const allSel = ids.every(id => selectedIds.has(id))
    setSelectedIds(prev => {
      const n = new Set(prev)
      allSel ? ids.forEach(id => n.delete(id)) : ids.forEach(id => n.add(id))
      return n
    })
  }

  const marcarPreparado = async (items: { item_id: number; cantidad?: number }[]) => {
    setSaving(true)
    try {
      const partial = items.some(i => i.cantidad !== undefined)
      if (partial) {
        await comprasAPI.prepararItemsParcial(items)
      } else {
        await comprasAPI.prepararItems(items.map(i => i.item_id))
      }
      toast.success(`${items.length} ítem(s) marcados como preparado`)
      load()
    } catch { toast.error('Error al marcar preparado') }
    finally { setSaving(false) }
  }

  const totalItems   = groups.reduce((s, g) => s + g.items.length, 0)
  const selectedCount = selectedIds.size

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold" style={{ color: 'var(--text-primary)' }}>Seguimiento de Compras</h1>
        <p className="text-sm mt-1" style={{ color: 'var(--text-muted)' }}>
          Ítems comprados · Facturas de proveedor · Preparación para embarque
        </p>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
        {[
          { label: 'OC-Proveedor activas', value: groups.length,  sub: 'Con ítems comprados', color: 'text-brand-400'   },
          { label: 'Ítems comprados',      value: totalItems,     sub: 'Pendientes preparar',  color: 'text-amber-400'   },
          { label: 'Seleccionados',        value: selectedCount,  sub: 'Listos para preparar', color: 'text-emerald-500' },
        ].map(s => (
          <div key={s.label} className="rounded-2xl p-3 sm:p-4 border"
            style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
            <p className="text-[10px] uppercase tracking-widest leading-tight" style={{ color: 'var(--text-faint)' }}>{s.label}</p>
            <p className={`text-lg sm:text-xl font-bold mt-1 ${s.color}`}>{s.value}</p>
            <p className="text-xs mt-0.5 leading-tight" style={{ color: 'var(--text-muted)' }}>{s.sub}</p>
          </div>
        ))}
      </div>

      {selectedCount > 0 && (
        <div className="flex items-center justify-between p-3 rounded-xl bg-brand-500/10 border border-brand-500/20">
          <p className="text-sm text-brand-400 font-medium">{selectedCount} ítem(s) seleccionados</p>
          <button onClick={() => marcarPreparado(Array.from(selectedIds).map(id => ({ item_id: id })))} disabled={saving}
            className="btn-primary flex items-center gap-2">
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />}
            Marcar preparado
          </button>
        </div>
      )}

      {loading ? (
        <div className="flex items-center justify-center py-16">
          <Loader2 className="w-6 h-6 animate-spin" style={{ color: 'var(--text-faint)' }} />
        </div>
      ) : groups.length === 0 ? (
        <div className="card p-10 text-center space-y-3">
          <CheckCircle2 className="w-10 h-10 mx-auto text-emerald-500" />
          <p className="font-semibold" style={{ color: 'var(--text-primary)' }}>Sin ítems comprados pendientes</p>
          <p className="text-sm" style={{ color: 'var(--text-muted)' }}>
            Todos los ítems comprados han sido preparados para embarque.
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          {groups.map((group, idx) => (
            <OcpGroupCard
              key={group.oc_proveedor_id ?? `no-ocp-${idx}`}
              group={group} selectedIds={selectedIds}
              onToggleItem={toggleItem} onToggleAll={toggleAll}
              onMarcarPreparado={marcarPreparado} saving={saving}
              onRefresh={load}
            />
          ))}
        </div>
      )}
    </div>
  )
}
