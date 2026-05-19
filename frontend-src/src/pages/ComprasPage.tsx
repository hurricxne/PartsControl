import { useState, useEffect, useCallback } from 'react'
import {
  ShoppingCart, Plus, Search, Package, ChevronDown, ChevronRight,
  Loader2, CheckCircle2, AlertTriangle, X, AlertCircle, FileDown,
} from 'lucide-react'
import toast from 'react-hot-toast'
import { comprasAPI, bodegaAPI } from '../services/api'

interface OcClienteItem {
  id: number
  item_num: number
  numero_parte: string
  descripcion: string
  marca: string
  cantidad: number
  precio_unit_cotizacion: number
  total_cotizacion: number
  total_venta_clp: number
  estado_item: string
  oc_proveedor_id: number | null
  oc_proveedor_numero: string | null
  numero_oc_prov: string | null
  plazo_entrega_max: number | null
  plazo_dias_prov: number | null
  dias_restantes: number | null
}

interface OcCliente {
  id: number
  cotizacion_id: number
  numero_cot: string
  cliente: string
  numero_oc: string
  fecha_oc: string
  cond_pago: string
  fecha_entrega: string
  total_items: number
  items_con_oc: number
  items: OcClienteItem[]
  created_at: string | null
}

interface OcProveedor {
  id: number
  numero: string       // correlativo interno OCP-2026-XXXX
  numero_oc?: string   // N° manual del proveedor (lo que el usuario tipea)
  proveedor: string
  pais: string
  moneda: string
  estado: string
}

interface ProveedorMaestro {
  id: number
  nombre: string
  tipo: string
  pais: string
  moneda: string
}

function fmtClp(v?: number) {
  if (!v) return '—'
  return `$${Math.round(v).toLocaleString('es-CL')}`
}

const ESTADO_ITEM: Record<string, { label: string; color: string; bg: string }> = {
  cerrado:       { label: 'Disponible',         color: '#10b981', bg: 'rgba(16,185,129,0.15)' },
  comprado:      { label: 'Seguimiento',         color: '#f59e0b', bg: 'rgba(245,158,11,0.15)' },
  preparado:     { label: 'Listo p/ Embarcar',   color: '#8b5cf6', bg: 'rgba(139,92,246,0.15)' },
  pre_embarcado: { label: 'Pre-embarcado',       color: '#06b6d4', bg: 'rgba(6,182,212,0.15)'  },
  embarcado:     { label: 'Embarcado',           color: '#3b82f6', bg: 'rgba(59,130,246,0.15)' },
}

function DiasRestantes({ dias }: { dias: number | null }) {
  if (dias === null) return <span style={{ color: 'var(--text-faint)' }}>—</span>
  if (dias <= 0) return <span className="font-semibold text-[11px] text-red-400">Vencido</span>
  const color = dias <= 5 ? 'text-amber-400' : 'text-emerald-400'
  return <span className={`font-semibold text-[11px] ${color}`}>{dias}d</span>
}

// Modal para crear/seleccionar OC-Proveedor y asignar
function AsignarModal({
  ocCliente,
  selectedItemIds,
  ocProveedores,
  proveedoresMaestro,
  onClose,
  onSuccess,
}: {
  ocCliente: OcCliente
  selectedItemIds: number[]
  ocProveedores: OcProveedor[]
  proveedoresMaestro: ProveedorMaestro[]
  onClose: () => void
  onSuccess: () => void
}) {
  const [modo, setModo] = useState<'existente' | 'nueva'>('nueva')
  const [ocpId, setOcpId] = useState<number | ''>(ocProveedores[0]?.id ?? '')
  const [proveedorMaestroId, setProveedorMaestroId] = useState<number | ''>(proveedoresMaestro[0]?.id ?? '')
  const [numeroOc, setNumeroOc] = useState('')
  const [ocError, setOcError] = useState(false)
  const [saving, setSaving] = useState(false)

  // Per-item plazo_dias_prov
  const selectedItems = ocCliente.items.filter(i => selectedItemIds.includes(i.id))
  const [itemPlazos, setItemPlazos] = useState<Record<number, string>>(() => {
    const init: Record<number, string> = {}
    selectedItems.forEach(i => { init[i.id] = '' })
    return init
  })

  // A-1.2: Row multi-select for "Duplicar valor"
  const [selectedRows, setSelectedRows] = useState<Set<number>>(new Set())
  const [lastSelected, setLastSelected] = useState<number | null>(null)
  const [activeInput, setActiveInput] = useState<number | null>(null)

  const handleRowClick = (itemId: number, e: React.MouseEvent) => {
    if (e.shiftKey && lastSelected !== null) {
      const idxLast = selectedItems.findIndex(i => i.id === lastSelected)
      const idxCurr = selectedItems.findIndex(i => i.id === itemId)
      const [lo, hi] = idxLast < idxCurr ? [idxLast, idxCurr] : [idxCurr, idxLast]
      const rangeIds = selectedItems.slice(lo, hi + 1).map(i => i.id)
      setSelectedRows(prev => {
        const next = new Set(prev)
        rangeIds.forEach(id => next.add(id))
        return next
      })
    } else {
      setSelectedRows(prev => {
        const next = new Set(prev)
        next.has(itemId) ? next.delete(itemId) : next.add(itemId)
        return next
      })
      setLastSelected(itemId)
    }
  }

  const handleDuplicar = () => {
    if (selectedRows.size < 2) return
    const ids = selectedItems.filter(i => selectedRows.has(i.id)).map(i => i.id)
    const refVal = itemPlazos[ids[0]] ?? ''
    if (!refVal) { return }
    setItemPlazos(prev => {
      const next = { ...prev }
      ids.forEach(id => { next[id] = refVal })
      return next
    })
  }

  const handlePlazoKeyDown = (e: React.KeyboardEvent<HTMLInputElement>, itemId: number) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'v') {
      // Let the native paste event fire, handled in onPaste
    }
  }

  const handlePlazoPaste = (e: React.ClipboardEvent<HTMLInputElement>, itemId: number) => {
    const text = e.clipboardData.getData('text')
    // Detect multi-line/tab paste (Excel column)
    const lines = text.split(/\r?\n|\t/).map(l => l.trim()).filter(Boolean)
    if (lines.length > 1) {
      e.preventDefault()
      const startIdx = selectedItems.findIndex(i => i.id === itemId)
      if (startIdx < 0) return
      setItemPlazos(prev => {
        const next = { ...prev }
        lines.forEach((val, offset) => {
          const target = selectedItems[startIdx + offset]
          if (target && !isNaN(Number(val))) {
            next[target.id] = val
          }
        })
        return next
      })
    }
    // Single value: let native paste handle it
  }

  const proveedorSeleccionado = proveedoresMaestro.find(p => p.id === proveedorMaestroId)

  const handleSave = async () => {
    if (!numeroOc.trim()) {
      setOcError(true)
      toast.error('El N° OC del proveedor es obligatorio')
      return
    }
    if (modo === 'existente' && !ocpId) {
      toast.error('Selecciona una OC-Proveedor')
      return
    }
    if (modo === 'nueva' && !proveedorMaestroId) {
      toast.error('Selecciona un proveedor del maestro')
      return
    }
    setSaving(true)
    try {
      let targetId = ocpId as number
      if (modo === 'existente' && ocpId) {
        await comprasAPI.actualizarOcProveedor(ocpId as number, { numero_oc: numeroOc.trim() })
      }
      if (modo === 'nueva') {
        const prov = proveedoresMaestro.find(p => p.id === proveedorMaestroId)
        const { data } = await comprasAPI.crearOcProveedor({
          proveedor: prov?.nombre ?? '',
          numero_oc: numeroOc.trim(),
          pais: prov?.pais ?? undefined,
          moneda: prov?.moneda ?? 'USD',
        })
        targetId = data.id
        toast.success(`OC-Proveedor ${data.numero} creada`)
      }

      // Build item_plazos array with per-item plazo
      const item_plazos = selectedItems.map(i => ({
        id: i.id,
        plazo_dias_prov: itemPlazos[i.id] ? Number(itemPlazos[i.id]) : null,
      }))

      await comprasAPI.asignarItems(targetId, {
        item_ids: selectedItemIds,
        oc_cliente_id: ocCliente.id,
        item_plazos,
      })
      toast.success(`${selectedItemIds.length} item(s) asignados`)
      onSuccess()
    } catch (e: any) {
      const detail = e.response?.data?.detail
      toast.error(typeof detail === 'string' ? detail : 'Error al asignar')
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
        className="w-full max-w-lg rounded-2xl border shadow-2xl p-6 space-y-5 max-h-[90vh] overflow-y-auto"
        style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}
      >
        <div className="flex items-center justify-between">
          <h3 className="font-semibold text-base" style={{ color: 'var(--text-primary)' }}>
            Asignar a OC-Proveedor
          </h3>
          <button onClick={onClose} className="rounded-lg p-1 hover:bg-[var(--surface-200)] transition-colors">
            <X className="w-4 h-4" style={{ color: 'var(--text-muted)' }} />
          </button>
        </div>

        <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
          Asignando <strong>{selectedItemIds.length}</strong> item(s) de <strong>COT-{ocCliente.numero_cot}</strong>
        </p>

        {/* N° OC Proveedor */}
        <div>
          <label className="block text-xs font-semibold uppercase tracking-wider mb-1.5"
            style={{ color: ocError ? '#f87171' : 'var(--text-faint)' }}>
            N° OC del Proveedor <span className="text-red-400">*</span>
          </label>
          <input
            className="input w-full"
            placeholder="Ej: PO-2026-00415"
            value={numeroOc}
            onChange={e => { setNumeroOc(e.target.value); setOcError(false) }}
            style={ocError ? { borderColor: '#f87171' } : undefined}
            autoFocus
          />
          {ocError && <p className="text-xs mt-1 text-red-400">Campo obligatorio</p>}
        </div>

        {/* Modo selector */}
        <div className="flex gap-2">
          {(['nueva', 'existente'] as const).map(m => (
            <button
              key={m}
              onClick={() => setModo(m)}
              className={`flex-1 py-2 rounded-xl text-xs font-semibold border transition-colors ${
                modo === m
                  ? 'border-brand-500 bg-brand-500/10 text-brand-400'
                  : 'border-[var(--border)] text-[var(--text-muted)] hover:border-brand-500/50'
              }`}
            >
              {m === 'nueva' ? 'Nueva OC-Proveedor' : 'OC-Proveedor existente'}
            </button>
          ))}
        </div>

        {modo === 'existente' ? (
          <div>
            <label className="block text-xs font-semibold uppercase tracking-wider mb-1.5" style={{ color: 'var(--text-faint)' }}>
              Seleccionar OC-Proveedor
            </label>
            {ocProveedores.length === 0 ? (
              <p className="text-xs text-center py-4" style={{ color: 'var(--text-faint)' }}>
                No hay OC-Proveedores creadas. Cambia a "Nueva OC-Proveedor".
              </p>
            ) : (
              <select className="input w-full" value={ocpId} onChange={e => setOcpId(Number(e.target.value))}>
                {ocProveedores.map(o => (
                  <option key={o.id} value={o.id}>{(o.numero_oc || o.numero)} — {o.proveedor}</option>
                ))}
              </select>
            )}
          </div>
        ) : (
          <div className="space-y-3">
            <div>
              <label className="block text-xs font-semibold uppercase tracking-wider mb-1.5" style={{ color: 'var(--text-faint)' }}>
                Proveedor <span className="text-red-400">*</span>
              </label>
              {proveedoresMaestro.length === 0 ? (
                <div className="rounded-xl border p-3 text-center" style={{ borderColor: 'var(--border)' }}>
                  <p className="text-xs" style={{ color: 'var(--text-faint)' }}>
                    Sin proveedores en el maestro.{' '}
                    <a href="/proveedores" className="text-brand-400 hover:underline">Crear en Admin Proveedores</a>
                  </p>
                </div>
              ) : (
                <select
                  className="input w-full"
                  value={proveedorMaestroId}
                  onChange={e => setProveedorMaestroId(Number(e.target.value))}
                >
                  {proveedoresMaestro.map(p => (
                    <option key={p.id} value={p.id}>
                      {p.nombre}{p.pais ? ` — ${p.pais}` : ''}{p.moneda ? ` (${p.moneda})` : ''}
                    </option>
                  ))}
                </select>
              )}
            </div>

            {proveedorSeleccionado && (
              <div className="rounded-xl px-3 py-2 text-xs flex items-center gap-3"
                style={{ backgroundColor: 'var(--surface-200)', color: 'var(--text-muted)' }}>
                <span className={`px-2 py-0.5 rounded-full text-xs font-semibold border ${
                  proveedorSeleccionado.tipo === 'SWIFT'
                    ? 'border-brand-500/40 bg-brand-500/10 text-brand-400'
                    : 'border-amber-500/40 bg-amber-500/10 text-amber-400'
                }`}>{proveedorSeleccionado.tipo}</span>
                {proveedorSeleccionado.pais && <span>{proveedorSeleccionado.pais}</span>}
                {proveedorSeleccionado.moneda && <span className="font-mono font-semibold">{proveedorSeleccionado.moneda}</span>}
              </div>
            )}
          </div>
        )}

        {/* Per-item plazo proveedor */}
        <div>
          <div className="flex items-center justify-between mb-2">
            <label className="text-xs font-semibold uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>
              Plazo por item (dias habiles proveedor)
            </label>
            {selectedRows.size >= 2 && (
              <button onClick={handleDuplicar}
                className="text-[11px] px-2.5 py-1 rounded-lg border border-brand-400/40 text-brand-400 hover:bg-brand-500/10 transition-colors font-semibold">
                ⬇ Duplicar valor ({selectedRows.size} filas)
              </button>
            )}
          </div>
          <div className="rounded-xl border overflow-hidden" style={{ borderColor: 'var(--border)' }}>
            <table className="w-full text-xs">
              <thead>
                <tr style={{ backgroundColor: 'var(--surface-200)', borderBottom: '1px solid var(--border)' }}>
                  <th className="px-3 py-2 w-8">
                    <input type="checkbox"
                      checked={selectedRows.size === selectedItems.length && selectedItems.length > 0}
                      onChange={e => setSelectedRows(e.target.checked ? new Set(selectedItems.map(i => i.id)) : new Set())}
                      className="rounded" />
                  </th>
                  <th className="px-3 py-2 text-left font-semibold uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>
                    N° Parte
                  </th>
                  <th className="px-3 py-2 text-left font-semibold uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>
                    Plazo Max. cliente
                  </th>
                  <th className="px-3 py-2 text-left font-semibold uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>
                    Plazo prov. (dias)
                  </th>
                </tr>
              </thead>
              <tbody>
                {selectedItems.map((item, idx) => (
                  <tr key={item.id}
                    onClick={e => handleRowClick(item.id, e)}
                    className={`cursor-pointer transition-colors ${selectedRows.has(item.id) ? 'bg-brand-500/10' : 'hover:bg-[var(--surface-200)]'}`}
                    style={{ borderBottom: idx < selectedItems.length - 1 ? '1px solid var(--border)' : undefined }}>
                    <td className="px-3 py-2" onClick={e => e.stopPropagation()}>
                      <input type="checkbox"
                        checked={selectedRows.has(item.id)}
                        onChange={() => {
                          setSelectedRows(prev => {
                            const next = new Set(prev)
                            next.has(item.id) ? next.delete(item.id) : next.add(item.id)
                            return next
                          })
                          setLastSelected(item.id)
                        }}
                        className="rounded" />
                    </td>
                    <td className="px-3 py-2 font-mono text-brand-400 font-semibold whitespace-nowrap">
                      {item.numero_parte}
                    </td>
                    <td className="px-3 py-2" style={{ color: 'var(--text-muted)' }}>
                      {item.plazo_entrega_max ? `${item.plazo_entrega_max}d` : '—'}
                    </td>
                    <td className="px-3 py-2" onClick={e => e.stopPropagation()}>
                      <input
                        type="number"
                        min="0"
                        className="input w-20 py-1 text-xs"
                        placeholder="Ej: 20"
                        value={itemPlazos[item.id] ?? ''}
                        onChange={e => setItemPlazos(prev => ({ ...prev, [item.id]: e.target.value }))}
                        onFocus={() => setActiveInput(item.id)}
                        onKeyDown={e => handlePlazoKeyDown(e, item.id)}
                        onPaste={e => handlePlazoPaste(e, item.id)}
                        title="Tip: pega una columna de Excel para rellenar en orden"
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className="flex gap-3 pt-1">
          <button onClick={onClose} className="btn-secondary flex-1">Cancelar</button>
          <button
            onClick={handleSave}
            disabled={saving}
            className="btn-primary flex-1 flex items-center justify-center gap-2"
          >
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <CheckCircle2 className="w-4 h-4" />}
            {saving ? 'Guardando...' : 'Asignar'}
          </button>
        </div>
      </div>
    </div>
  )
}

// Card de OC-Cliente expandible
function OcClienteCard({
  oc,
  ocProveedores,
  proveedoresMaestro,
  onRefresh,
}: {
  oc: OcCliente
  ocProveedores: OcProveedor[]
  proveedoresMaestro: ProveedorMaestro[]
  onRefresh: () => void
}) {
  const [expanded, setExpanded] = useState(false)
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [showModal, setShowModal] = useState(false)
  const [downloading, setDownloading] = useState(false)

  const handleDownloadExcel = async (e: React.MouseEvent) => {
    e.stopPropagation()
    setDownloading(true)
    try {
      const resp = await comprasAPI.downloadOcClienteExcel(oc.id)
      const blob = new Blob([resp.data], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `OC-${oc.numero_oc || oc.numero_cot}.xlsx`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch {
      toast.error('Error al descargar Excel')
    } finally {
      setDownloading(false)
    }
  }

  const pendientes = oc.items.filter(i => !i.oc_proveedor_id)
  const toggleItem = (id: number) => {
    setSelected(prev => {
      const s = new Set(prev)
      s.has(id) ? s.delete(id) : s.add(id)
      return s
    })
  }
  const toggleAll = () => {
    const pendienteIds = pendientes.map(i => i.id)
    if (selected.size === pendientes.length) {
      setSelected(new Set())
    } else {
      setSelected(new Set(pendienteIds))
    }
  }

  const allAssigned = oc.total_items > 0 && oc.items_con_oc === oc.total_items

  return (
    <>
      <div
        className="rounded-2xl border transition-shadow"
        style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}
      >
        {/* Header row */}
        <div
          className="flex items-center gap-3 p-4 cursor-pointer hover:bg-[var(--surface-200)] rounded-2xl transition-colors"
          onClick={() => setExpanded(v => !v)}
        >
          <div className="w-9 h-9 rounded-xl bg-brand-500/10 flex items-center justify-center shrink-0">
            <Package className="w-4 h-4 text-brand-400" />
          </div>
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="font-mono font-bold text-sm text-brand-400">
                OC-{oc.numero_oc || oc.numero_cot}
              </span>
              {allAssigned && (
                <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium border bg-emerald-500/10 text-emerald-400 border-emerald-500/20">
                  <CheckCircle2 className="w-3 h-3" /> Completado
                </span>
              )}
            </div>
            <p className="text-sm mt-0.5 font-medium truncate" style={{ color: 'var(--text-primary)' }}>
              {oc.cliente || '—'}
            </p>
            <p className="text-xs mt-0.5" style={{ color: 'var(--text-muted)' }}>
              COT-{oc.numero_cot}
              {oc.numero_oc && <> · OC #{oc.numero_oc}</>}
              {oc.fecha_oc && <> · {oc.fecha_oc}</>}
              {oc.cond_pago && <> · {oc.cond_pago}</>}
              {oc.fecha_entrega && <> · Entrega: {oc.fecha_entrega}</>}
            </p>
            {/* Per-OC progress bar */}
            {oc.total_items > 0 && (
              <div className="mt-2 space-y-1">
                <div className="flex items-center justify-between">
                  <span className="text-[10px] uppercase tracking-wider font-semibold" style={{ color: 'var(--text-faint)' }}>
                    Progreso compra
                  </span>
                  <span className="text-[10px] font-bold" style={{
                    color: allAssigned ? '#10b981' : 'var(--text-muted)'
                  }}>
                    {oc.items_con_oc}/{oc.total_items} · {Math.round((oc.items_con_oc / oc.total_items) * 100)}%
                  </span>
                </div>
                <div className="w-full h-1.5 rounded-full overflow-hidden" style={{ backgroundColor: 'var(--surface-300)' }}>
                  <div
                    className="h-full rounded-full transition-all duration-500"
                    style={{
                      width: `${Math.round((oc.items_con_oc / oc.total_items) * 100)}%`,
                      background: allAssigned
                        ? 'linear-gradient(90deg,#10b981,#059669)'
                        : 'linear-gradient(90deg,#f59e0b,#d97706)',
                    }}
                  />
                </div>
              </div>
            )}
          </div>
          <button
            onClick={handleDownloadExcel}
            disabled={downloading}
            title="Descargar Excel OC"
            className="shrink-0 p-1.5 rounded-lg transition-colors hover:bg-emerald-500/10 disabled:opacity-50"
            style={{ color: 'var(--text-faint)' }}
          >
            {downloading
              ? <Loader2 className="w-4 h-4 animate-spin" />
              : <FileDown className="w-4 h-4 hover:text-emerald-400" />}
          </button>
          <div className="shrink-0 text-[var(--text-faint)]">
            {expanded ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
          </div>
        </div>

        {/* Expanded items table */}
        {expanded && (
          <div className="border-t" style={{ borderColor: 'var(--border)' }}>
            {oc.items.length === 0 ? (
              <p className="text-xs text-center py-5" style={{ color: 'var(--text-faint)' }}>
                No hay items cerrados
              </p>
            ) : (
              <>
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr style={{ backgroundColor: 'var(--surface-200)', borderBottom: '1px solid var(--border)' }}>
                        <th className="px-3 py-2 w-8">
                          <input
                            type="checkbox"
                            checked={selected.size === pendientes.length && pendientes.length > 0}
                            onChange={toggleAll}
                            className="w-3.5 h-3.5 rounded cursor-pointer accent-brand-500"
                            disabled={pendientes.length === 0}
                          />
                        </th>
                        {['N° Parte', 'Descripcion', 'Marca', 'Qty', 'Plazo Prov.', 'Unit Venta CLP', 'Total Venta CLP', 'Plazo Máx.', 'Días Rest.', 'Estado', 'OC-Proveedor'].map(h => (
                          <th key={h} className="px-3 py-2 text-left text-xs font-semibold uppercase tracking-wider"
                            style={{ color: 'var(--text-faint)' }}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {oc.items.map((item, idx) => {
                        const meta = ESTADO_ITEM[item.estado_item] || ESTADO_ITEM.cerrado
                        const isAssigned = !!item.oc_proveedor_id
                        const isChecked = selected.has(item.id)
                        return (
                          <tr
                            key={item.id}
                            onClick={() => !isAssigned && toggleItem(item.id)}
                            className="transition-colors"
                            style={{
                              borderBottom: idx < oc.items.length - 1 ? '1px solid var(--border)' : undefined,
                              cursor: isAssigned ? 'default' : 'pointer',
                              opacity: isAssigned ? 0.7 : 1,
                              backgroundColor: isChecked ? 'rgba(59,130,246,0.07)' : undefined,
                            }}
                          >
                            <td className="px-3 py-2.5">
                              <input
                                type="checkbox"
                                checked={isChecked}
                                readOnly
                                disabled={isAssigned}
                                className="w-3.5 h-3.5 rounded cursor-pointer accent-brand-500"
                              />
                            </td>
                            <td className="px-3 py-2.5 font-mono text-xs text-brand-400 font-semibold whitespace-nowrap">
                              {item.numero_parte}
                            </td>
                            <td className="px-3 py-2.5 text-xs max-w-[160px] truncate" style={{ color: 'var(--text-primary)' }}>
                              {item.descripcion || '—'}
                            </td>
                            <td className="px-3 py-2.5 text-xs" style={{ color: 'var(--text-muted)' }}>
                              {item.marca || '—'}
                            </td>
                            <td className="px-3 py-2.5 text-xs text-center" style={{ color: 'var(--text-muted)' }}>
                              {item.cantidad}
                            </td>
                            <td className="px-3 py-2.5 text-xs text-center" style={{ color: 'var(--text-muted)' }}>
                              {item.plazo_dias_prov ? `${item.plazo_dias_prov}d` : '—'}
                            </td>
                            <td className="px-3 py-2.5 text-xs text-right font-mono" style={{ color: 'var(--text-muted)' }}>
                              {item.cantidad > 0 ? fmtClp((item.total_venta_clp ?? item.total_cotizacion) / item.cantidad) : '—'}
                            </td>
                            <td className="px-3 py-2.5 text-xs text-right font-mono font-semibold" style={{ color: 'var(--text-primary)' }}>
                              {fmtClp(item.total_venta_clp ?? item.total_cotizacion)}
                            </td>
                            <td className="px-3 py-2.5 text-xs text-center" style={{ color: 'var(--text-muted)' }}>
                              {item.plazo_entrega_max ? `${item.plazo_entrega_max}d` : '—'}
                            </td>
                            <td className="px-3 py-2.5 text-xs text-center">
                              <DiasRestantes dias={item.dias_restantes} />
                            </td>
                            <td className="px-3 py-2.5">
                              <span className="text-[10px] font-semibold px-2 py-0.5 rounded-full whitespace-nowrap"
                                style={{ background: meta.bg, color: meta.color }}>
                                {meta.label}
                              </span>
                            </td>
                            <td className="px-3 py-2.5 text-xs" style={{ color: isAssigned ? 'var(--text-muted)' : 'var(--text-faint)' }}>
                              {item.numero_oc_prov || item.oc_proveedor_numero
                                ? <span className="font-mono font-semibold text-brand-400">
                                    {item.numero_oc_prov || item.oc_proveedor_numero}
                                  </span>
                                : <span className="italic">Sin asignar</span>}
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>

                {/* Action bar */}
                {selected.size > 0 && (
                  <div
                    className="flex items-center justify-between px-4 py-3 border-t"
                    style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}
                  >
                    <span className="text-xs font-semibold" style={{ color: 'var(--text-muted)' }}>
                      {selected.size} item(s) seleccionados
                    </span>
                    <button
                      className="btn-primary text-xs px-4 py-1.5 flex items-center gap-1.5"
                      onClick={e => { e.stopPropagation(); setShowModal(true) }}
                    >
                      <Plus className="w-3.5 h-3.5" /> Asignar a OC-Proveedor
                    </button>
                  </div>
                )}
              </>
            )}
          </div>
        )}
      </div>

      {showModal && (
        <AsignarModal
          ocCliente={oc}
          selectedItemIds={[...selected]}
          ocProveedores={ocProveedores}
          proveedoresMaestro={proveedoresMaestro}
          onClose={() => setShowModal(false)}
          onSuccess={() => { setShowModal(false); setSelected(new Set()); onRefresh() }}
        />
      )}
    </>
  )
}

interface Reclamo {
  id: number
  motivo: string
  qty_afectada: number
  estado: string
  observacion: string | null
  fecha_creacion: string
  numero_parte: string
  descripcion: string
  proveedor_nombre: string
  oc_proveedor_numero: string
  numero_oc_prov?: string
}

export default function ComprasPage() {
  const [ocClientes, setOcClientes] = useState<OcCliente[]>([])
  const [ocProveedores, setOcProveedores] = useState<OcProveedor[]>([])
  const [proveedoresMaestro, setProveedoresMaestro] = useState<ProveedorMaestro[]>([])
  const [loading, setLoading] = useState(true)
  const [q, setQ] = useState('')
  const [activeTab, setActiveTab] = useState<'oc' | 'reclamos'>('oc')
  const [reclamos, setReclamos] = useState<Reclamo[]>([])
  const [loadingReclamos, setLoadingReclamos] = useState(false)
  const [savingReclamo, setSavingReclamo] = useState<number | null>(null)

  const load = useCallback(() => {
    setLoading(true)
    Promise.all([
      comprasAPI.listOcCliente(),
      comprasAPI.listOcProveedor(),
      comprasAPI.listProveedores(),
    ]).then(([r1, r2, r3]) => {
      setOcClientes(r1.data)
      setOcProveedores(r2.data)
      setProveedoresMaestro(r3.data)
    }).catch(() => toast.error('Error al cargar compras'))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load() }, [load])

  const loadReclamos = useCallback(async () => {
    setLoadingReclamos(true)
    try {
      const { data } = await bodegaAPI.listReclamos()
      setReclamos(data)
    } catch { /* silent */ } finally {
      setLoadingReclamos(false)
    }
  }, [])

  useEffect(() => { if (activeTab === 'reclamos') loadReclamos() }, [activeTab, loadReclamos])

  const handleActualizarReclamo = async (id: number, estado: string, obs?: string) => {
    setSavingReclamo(id)
    try {
      await bodegaAPI.actualizarReclamo(id, { estado, observacion: obs })
      toast.success('Reclamo actualizado')
      loadReclamos()
    } catch {
      toast.error('Error al actualizar reclamo')
    } finally {
      setSavingReclamo(null)
    }
  }

  const filtered = ocClientes.filter(oc =>
    !q ||
    oc.cliente.toLowerCase().includes(q.toLowerCase()) ||
    oc.numero_cot.includes(q) ||
    oc.numero_oc.includes(q)
  )

  const totalItems = ocClientes.reduce((s, o) => s + o.total_items, 0)
  const totalConOc = ocClientes.reduce((s, o) => s + o.items_con_oc, 0)
  const sinAsignar = totalItems - totalConOc

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold" style={{ color: 'var(--text-primary)' }}>Panel de Compras</h1>
          <p className="text-sm mt-1" style={{ color: 'var(--text-muted)' }}>
            OC-Clientes generadas desde cierre de ventas — asigna items a OC-Proveedor
          </p>
        </div>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        {[
          { label: 'OC-Clientes',    value: ocClientes.length, sub: 'Ventas cerradas',       color: 'text-brand-400'   },
          { label: 'Total items',    value: totalItems,         sub: 'Items cerrados',        color: 'text-brand-400'   },
          { label: 'Con OC-Proveedor', value: totalConOc,      sub: 'Asignados a compra',    color: 'text-emerald-500' },
          { label: 'Sin asignar',    value: sinAsignar,         sub: 'Pendientes de compra',  color: sinAsignar > 0 ? 'text-amber-400' : 'text-emerald-500' },
        ].map(s => (
          <div key={s.label} className="rounded-2xl p-3 sm:p-4 border"
            style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
            <p className="text-[10px] uppercase tracking-widest leading-tight" style={{ color: 'var(--text-faint)' }}>{s.label}</p>
            <p className={`text-lg sm:text-xl font-bold mt-1 ${s.color}`}>{s.value}</p>
            <p className="text-xs mt-0.5 leading-tight" style={{ color: 'var(--text-muted)' }}>{s.sub}</p>
          </div>
        ))}
      </div>

      {/* Tabs */}
      <div className="flex gap-1 p-1 rounded-xl border w-fit" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
        {(['oc', 'reclamos'] as const).map(tab => (
          <button key={tab} onClick={() => setActiveTab(tab)}
            className={`px-4 py-1.5 rounded-lg text-xs font-semibold transition-colors ${
              activeTab === tab ? 'bg-brand-500/20 text-brand-400' : 'text-[var(--text-muted)] hover:text-[var(--text-primary)]'
            }`}>
            {tab === 'oc' ? 'OC-Clientes' : (
              <span className="flex items-center gap-1.5">
                <AlertTriangle className="w-3.5 h-3.5" />
                Reclamos
                {reclamos.filter(r => r.estado === 'pendiente').length > 0 && (
                  <span className="ml-0.5 px-1.5 py-0.5 rounded-full bg-amber-500/20 text-amber-400 text-[10px] font-bold">
                    {reclamos.filter(r => r.estado === 'pendiente').length}
                  </span>
                )}
              </span>
            )}
          </button>
        ))}
      </div>

      {activeTab === 'oc' && (
      <>
      {/* Search */}
      <div className="relative max-w-sm">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4" style={{ color: 'var(--text-faint)' }} />
        <input
          className="input pl-9 w-full"
          placeholder="Buscar por cliente, N° COT u OC..."
          value={q}
          onChange={e => setQ(e.target.value)}
        />
      </div>

      {/* Content */}
      {loading ? (
        <div className="flex items-center justify-center py-16">
          <Loader2 className="w-6 h-6 animate-spin" style={{ color: 'var(--text-faint)' }} />
        </div>
      ) : filtered.length === 0 ? (
        <div className="card p-10 text-center space-y-3">
          <ShoppingCart className="w-10 h-10 mx-auto" style={{ color: 'var(--text-faint)' }} />
          <p className="font-semibold" style={{ color: 'var(--text-primary)' }}>
            {ocClientes.length === 0 ? 'Sin ventas cerradas' : 'Sin resultados'}
          </p>
          <p className="text-sm" style={{ color: 'var(--text-muted)' }}>
            {ocClientes.length === 0
              ? 'Cuando cierres una venta, aparecera aqui para asignar los items a OC-Proveedor.'
              : 'Ajusta el termino de busqueda.'}
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          {filtered.map(oc => (
            <OcClienteCard
              key={oc.id}
              oc={oc}
              ocProveedores={ocProveedores}
              proveedoresMaestro={proveedoresMaestro}
              onRefresh={load}
            />
          ))}
        </div>
      )}
      </>
      )}

      {activeTab === 'reclamos' && (
        <div className="space-y-4">
          {loadingReclamos ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="w-6 h-6 animate-spin" style={{ color: 'var(--text-faint)' }} />
            </div>
          ) : reclamos.length === 0 ? (
            <div className="card p-10 text-center space-y-2">
              <CheckCircle2 className="w-10 h-10 mx-auto text-emerald-400 opacity-50" />
              <p className="font-semibold text-sm" style={{ color: 'var(--text-muted)' }}>Sin reclamos registrados</p>
            </div>
          ) : (
            <>
              {(['pendiente', 'reclamado', 'resuelto', 'anulado'] as const).map(estado => {
                const grupo = reclamos.filter(r => r.estado === estado)
                if (grupo.length === 0) return null
                const colors: Record<string, string> = {
                  pendiente: 'text-amber-400',
                  reclamado: 'text-blue-400',
                  resuelto: 'text-emerald-400',
                  anulado: 'text-[var(--text-faint)]',
                }
                const labels: Record<string, string> = {
                  pendiente: 'Pendiente',
                  reclamado: 'Reclamado al proveedor',
                  resuelto: 'Resuelto',
                  anulado: 'Anulado',
                }
                return (
                  <div key={estado}>
                    <h3 className={`text-xs font-bold uppercase tracking-wider mb-2 ${colors[estado]}`}>
                      {labels[estado]} ({grupo.length})
                    </h3>
                    <div className="space-y-2">
                      {grupo.map(r => (
                        <div key={r.id} className="rounded-xl border p-4 space-y-2"
                          style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
                          <div className="flex items-start justify-between gap-3">
                            <div className="min-w-0">
                              <span className="font-mono font-bold text-xs text-brand-400">{r.numero_parte}</span>
                              <span className="text-xs ml-2" style={{ color: 'var(--text-muted)' }}>{r.descripcion}</span>
                              <div className="text-[11px] mt-0.5 space-x-2" style={{ color: 'var(--text-faint)' }}>
                                <span>Proveedor: {r.proveedor_nombre}</span>
                                <span>·</span>
                                <span>OCP: {r.numero_oc_prov || r.oc_proveedor_numero}</span>
                              </div>
                            </div>
                            <div className="shrink-0 text-right">
                              <span className={`text-[10px] font-bold uppercase px-2 py-0.5 rounded-full ${colors[estado]}`}
                                style={{ backgroundColor: 'currentColor', opacity: 0.1 }}>
                                {r.motivo.replace(/_/g, ' ')}
                              </span>
                              <div className="text-xs mt-1 font-semibold" style={{ color: 'var(--text-muted)' }}>
                                Qty afectada: {r.qty_afectada}
                              </div>
                            </div>
                          </div>
                          {r.observacion && (
                            <p className="text-[11px] px-2 py-1 rounded-lg"
                              style={{ backgroundColor: 'var(--surface-200)', color: 'var(--text-muted)' }}>
                              {r.observacion}
                            </p>
                          )}
                          {estado === 'pendiente' && (
                            <div className="flex gap-2 pt-1">
                              <button
                                onClick={() => handleActualizarReclamo(r.id, 'reclamado')}
                                disabled={savingReclamo === r.id}
                                className="text-[11px] px-3 py-1 rounded-lg border border-blue-400/30 text-blue-400 hover:bg-blue-500/10 transition-colors font-semibold disabled:opacity-50">
                                Marcar reclamado
                              </button>
                              <button
                                onClick={() => handleActualizarReclamo(r.id, 'anulado')}
                                disabled={savingReclamo === r.id}
                                className="text-[11px] px-3 py-1 rounded-lg border border-[var(--border)] hover:bg-[var(--surface-200)] transition-colors font-semibold disabled:opacity-50"
                                style={{ color: 'var(--text-faint)' }}>
                                Anular
                              </button>
                            </div>
                          )}
                          {estado === 'reclamado' && (
                            <div className="flex gap-2 pt-1">
                              <button
                                onClick={() => handleActualizarReclamo(r.id, 'resuelto')}
                                disabled={savingReclamo === r.id}
                                className="text-[11px] px-3 py-1 rounded-lg border border-emerald-400/30 text-emerald-400 hover:bg-emerald-500/10 transition-colors font-semibold disabled:opacity-50">
                                Marcar resuelto
                              </button>
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                )
              })}
            </>
          )}
        </div>
      )}
    </div>
  )
}
