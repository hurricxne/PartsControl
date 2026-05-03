import { useState, useCallback, useRef, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowLeft, Plus, Trash2, Send, ClipboardPaste, AlertCircle, RefreshCw } from 'lucide-react'
import toast from 'react-hot-toast'
import { cotizacionesAPI } from '../services/api'

interface ManualRow {
  id: number
  item_num: string
  numero_parte: string
  descripcion: string
  marca: string
  cantidad: string
  peso_unit_lbs: string
  precio_unit_cotizacion: string
}

const EMPTY_ROW = (id: number): ManualRow => ({
  id,
  item_num: String(id),
  numero_parte: '',
  descripcion: '',
  marca: 'CAT',
  cantidad: '1',
  peso_unit_lbs: '',
  precio_unit_cotizacion: '',
})

const COLS: { key: keyof ManualRow; label: string; width: number; type?: string; placeholder?: string }[] = [
  { key: 'item_num',               label: '#',           width: 50,  type: 'number', placeholder: '1' },
  { key: 'numero_parte',           label: 'Part Number', width: 130, placeholder: '1960423' },
  { key: 'descripcion',            label: 'Description', width: 200, placeholder: 'CONE-SPL T' },
  { key: 'marca',                  label: 'Brand',       width: 80,  placeholder: 'CAT' },
  { key: 'cantidad',               label: 'Qty',         width: 70,  type: 'number', placeholder: '1' },
  { key: 'peso_unit_lbs',          label: 'Unit Weight', width: 100, type: 'number', placeholder: '298' },
  { key: 'precio_unit_cotizacion', label: 'Unit Price',  width: 110, type: 'number', placeholder: '11517.58' },
]

// Parse tab-separated text (Excel copy) → rows
//
// Handles two copy scenarios from the official Excel format:
//   Full  (10 cols): Item#(0) | Part#(1) | Desc(2) | Brand(3) | Loc(4) | Qty(5) | Avail(6) | Weight(7) | Price(8) | Total(9)
//   No-item (9 cols): Part#(0) | Desc(1) | Brand(2) | Loc(3) | Qty(4) | Avail(5) | Weight(6) | Price(7) | Total(8)
//
// Detection: if cells[0] is a small integer (≤999) → Item# column present (offset=0)
//            otherwise it's a Part Number → offset=-1 shifts all indices left by 1
function parseTabText(text: string, startId: number): ManualRow[] {
  const lines = text.trim().split(/\r?\n/).filter(l => l.trim())
  const parsed: ManualRow[] = []
  let idCounter = startId

  for (const line of lines) {
    const cells = line.split('\t').map(c => c.trim())
    // Skip header rows
    const first = cells[0]?.toUpperCase() || ''
    if (first === 'ITEM #' || first === 'ITEM' || first === '#' || first === 'NO.' || first === 'N°') continue
    // Skip blank rows
    if (!cells[0] && !cells[1]) continue

    // Detect column offset:
    // cells[0] is a small sequential number → Item# column is present
    const hasItemCol = /^\d+$/.test(cells[0]) && parseInt(cells[0]) <= 999
    const o = hasItemCol ? 0 : -1  // offset applied to every column index

    parsed.push({
      id: idCounter++,
      item_num:               hasItemCol ? cells[0] : String(parsed.length + 1),
      numero_parte:           cells[1 + o] || '',
      descripcion:            cells[2 + o] || '',
      marca:                  cells[3 + o] || 'CAT',
      // col 4+o = Location (skipped), col 5+o = Qty Required
      cantidad:               cells[5 + o] || '1',
      // col 6+o = Available (skipped), col 7+o = Unit Weight
      peso_unit_lbs:          cells[7 + o] || '',
      // col 8+o = Unit Price
      precio_unit_cotizacion: cells[8 + o] || '',
    })
  }
  return parsed
}

export default function CotizacionManual() {
  const navigate = useNavigate()
  const [cliente, setCliente] = useState('')
  const [referencia, setReferencia] = useState('')
  const [rows, setRows] = useState<ManualRow[]>([EMPTY_ROW(1)])
  const [submitting, setSubmitting] = useState(false)
  const nextId = useRef(2)
  const pasteAreaRef = useRef<HTMLDivElement>(null)

  // Auto-focus the paste area on mount so Ctrl+V works immediately
  useEffect(() => {
    pasteAreaRef.current?.focus()
  }, [])

  const addRow = useCallback(() => {
    const id = nextId.current++
    setRows(prev => [...prev, EMPTY_ROW(id)])
  }, [])

  const deleteRow = useCallback((id: number) => {
    setRows(prev => prev.filter(r => r.id !== id))
  }, [])

  const updateCell = useCallback((id: number, key: keyof ManualRow, value: string) => {
    setRows(prev => prev.map(r => r.id === id ? { ...r, [key]: value } : r))
  }, [])

  const applyParsed = useCallback((parsed: ManualRow[]) => {
    if (parsed.length === 0) return
    setRows(prev => {
      const nonEmpty = prev.filter(r => r.numero_parte.trim() || r.descripcion.trim())
      return [...nonEmpty, ...parsed]
    })
    toast.success(`${parsed.length} fila${parsed.length !== 1 ? 's' : ''} pegada${parsed.length !== 1 ? 's' : ''} correctamente`)
  }, [])

  // Handler for native paste event (Ctrl+V while table area is focused)
  const handlePaste = useCallback((e: React.ClipboardEvent) => {
    const text = e.clipboardData.getData('text/plain')
    if (!text || !text.includes('\t')) return
    e.preventDefault()
    const parsed = parseTabText(text, nextId.current)
    nextId.current += parsed.length
    applyParsed(parsed)
  }, [applyParsed])

  // Handler for the "Pegar desde Excel" button → uses Clipboard API
  const handleClipboardButton = useCallback(async () => {
    try {
      const text = await navigator.clipboard.readText()
      if (!text || !text.includes('\t')) {
        toast.error('El portapapeles no contiene datos de Excel (celdas con tabulaciones)')
        return
      }
      const parsed = parseTabText(text, nextId.current)
      nextId.current += parsed.length
      applyParsed(parsed)
    } catch {
      // Clipboard API denied → ask user to use Ctrl+V directly
      toast.error('No se pudo leer el portapapeles. Haz clic en la tabla y presiona Ctrl+V')
      pasteAreaRef.current?.focus()
    }
  }, [applyParsed])

  const handleSubmit = async () => {
    const validRows = rows.filter(r => r.numero_parte.trim())
    if (validRows.length === 0) {
      toast.error('Agrega al menos una parte con número de parte')
      return
    }

    const items = validRows.map((r, i) => ({
      item_num: parseInt(r.item_num) || i + 1,
      numero_parte: r.numero_parte.trim(),
      descripcion: r.descripcion.trim(),
      marca: r.marca.trim(),
      cantidad: parseFloat(r.cantidad) || 1,
      peso_unit_lbs: parseFloat(r.peso_unit_lbs) || 0,
      precio_unit_cotizacion: parseFloat(r.precio_unit_cotizacion) || 0,
    }))

    setSubmitting(true)
    try {
      const { data } = await cotizacionesAPI.crearManual({ cliente, referencia, items })
      toast.success(data.message || 'Cotización creada')
      navigate('/cotizaciones')
    } catch (err: any) {
      toast.error(err.response?.data?.detail || 'Error al crear cotización')
    } finally {
      setSubmitting(false)
    }
  }

  const validCount = rows.filter(r => r.numero_parte.trim()).length

  return (
    <div className="space-y-5 animate-slide-up">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-3">
          <button onClick={() => navigate('/cotizaciones')} className="btn-secondary text-sm py-1.5 px-3">
            <ArrowLeft className="w-4 h-4" />
          </button>
          <div>
            <h1 className="text-xl font-bold" style={{ color: 'var(--text-primary)' }}>
              Nueva Cotización Manual
            </h1>
            <p className="text-sm" style={{ color: 'var(--text-muted)' }}>
              Ingresa los datos a mano o pega desde Excel · {validCount} parte(s) cargada(s)
            </p>
          </div>
        </div>
        <button
          onClick={handleSubmit}
          disabled={submitting || validCount === 0}
          className="btn-primary"
        >
          {submitting ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
          Crear Cotización ({validCount})
        </button>
      </div>

      {/* Meta fields */}
      <div className="card p-5">
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div>
            <label className="block text-xs font-semibold mb-1.5 uppercase tracking-wide" style={{ color: 'var(--text-faint)' }}>
              Cliente
            </label>
            <input
              className="input"
              placeholder="Nombre del cliente"
              value={cliente}
              onChange={e => setCliente(e.target.value)}
            />
          </div>
          <div>
            <label className="block text-xs font-semibold mb-1.5 uppercase tracking-wide" style={{ color: 'var(--text-faint)' }}>
              Referencia / OT
            </label>
            <input
              className="input"
              placeholder="Número de orden o referencia"
              value={referencia}
              onChange={e => setReferencia(e.target.value)}
            />
          </div>
        </div>
      </div>

      {/* Paste hint + button */}
      <div className="flex items-center gap-3 px-4 py-3 rounded-xl text-sm flex-wrap"
        style={{ backgroundColor: 'rgba(59,130,246,0.07)', border: '1px solid rgba(59,130,246,0.2)', color: '#60a5fa' }}>
        <ClipboardPaste className="w-4 h-4 flex-shrink-0" />
        <span className="flex-1 min-w-0">
          <strong>Tip:</strong> Copia las filas desde Excel y usa el botón o presiona <kbd className="px-1.5 py-0.5 rounded text-xs font-mono" style={{ background: 'rgba(255,255,255,0.12)' }}>Ctrl+V</kbd> con la tabla enfocada.
          Columnas esperadas: <span className="text-xs opacity-70">Item · Part Number · Description · Brand · Location · Qty · Available · Unit Weight · Unit Price</span>
        </span>
        <button
          type="button"
          onClick={handleClipboardButton}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold flex-shrink-0 transition-colors"
          style={{ background: 'rgba(59,130,246,0.2)', border: '1px solid rgba(59,130,246,0.35)', color: '#93c5fd' }}
        >
          <ClipboardPaste className="w-3.5 h-3.5" />
          Pegar desde Excel
        </button>
      </div>

      {/* Table — tabIndex+ref so Ctrl+V works on the container */}
      <div className="card overflow-hidden">
        <div
          ref={pasteAreaRef}
          tabIndex={0}
          onPaste={handlePaste}
          className="overflow-x-auto outline-none"
          style={{ cursor: 'default' }}
          onClick={() => pasteAreaRef.current?.focus()}
        >
          <table className="w-full text-xs border-collapse" style={{ minWidth: '900px' }}>
            <thead style={{ backgroundColor: 'var(--surface-200)', borderBottom: '2px solid var(--border)' }}>
              <tr>
                {COLS.map(col => (
                  <th
                    key={col.key}
                    className="px-3 py-2.5 text-left font-semibold text-[10px] uppercase tracking-wide whitespace-nowrap"
                    style={{ color: 'var(--text-faint)', minWidth: col.width }}
                  >
                    {col.label}
                    {col.key === 'numero_parte' && (
                      <span className="ml-1 text-red-400">*</span>
                    )}
                  </th>
                ))}
                <th className="px-3 py-2.5 text-left font-semibold text-[10px] uppercase tracking-wide"
                  style={{ color: 'var(--text-faint)' }}>
                  Total
                </th>
                <th style={{ width: 44 }} />
              </tr>
            </thead>
            <tbody>
              {rows.map((row, idx) => {
                const total = (parseFloat(row.precio_unit_cotizacion) || 0) * (parseFloat(row.cantidad) || 0)
                const rowBg = idx % 2 === 0 ? 'var(--surface-100)' : 'var(--surface)'
                return (
                  <tr key={row.id} style={{ backgroundColor: rowBg, borderBottom: '1px solid var(--border)' }}>
                    {COLS.map(col => (
                      <td key={col.key} className="px-1 py-1" style={{ minWidth: col.width }}>
                        <input
                          type={col.type || 'text'}
                          value={row[col.key]}
                          onChange={e => updateCell(row.id, col.key, e.target.value)}
                          placeholder={col.placeholder}
                          className="w-full rounded px-2 py-1 text-xs outline-none transition-colors"
                          style={{
                            backgroundColor: 'transparent',
                            border: '1px solid transparent',
                            color: 'var(--text-primary)',
                          }}
                          onFocus={e => {
                            e.target.style.backgroundColor = 'var(--surface-200)'
                            e.target.style.borderColor = 'var(--empresa-primary, #1550d4)'
                          }}
                          onBlur={e => {
                            e.target.style.backgroundColor = 'transparent'
                            e.target.style.borderColor = 'transparent'
                          }}
                        />
                      </td>
                    ))}
                    <td className="px-2 py-1 text-right text-[11px]" style={{ color: 'var(--text-muted)' }}>
                      {total > 0 ? `$${total.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : '—'}
                    </td>
                    <td className="px-2 py-1 text-center">
                      <button
                        onClick={() => deleteRow(row.id)}
                        className="p-1 rounded hover:bg-red-500/10 transition-colors"
                        title="Eliminar fila"
                      >
                        <Trash2 className="w-3.5 h-3.5" style={{ color: 'var(--text-faint)' }} />
                      </button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>

        {/* Add row footer */}
        <div className="px-4 py-3 flex items-center justify-between"
          style={{ borderTop: '1px solid var(--border)', backgroundColor: 'var(--surface-200)' }}>
          <button onClick={addRow} className="btn-secondary text-xs py-1.5 px-3">
            <Plus className="w-3.5 h-3.5" />
            Agregar fila
          </button>
          {validCount > 0 && (
            <span className="text-xs" style={{ color: 'var(--text-muted)' }}>
              <span className="font-semibold" style={{ color: 'var(--text-primary)' }}>{validCount}</span> parte(s) · sin precio: {rows.filter(r => r.numero_parte.trim() && !r.precio_unit_cotizacion.trim()).length}
            </span>
          )}
        </div>
      </div>

      {/* Warning if no prices */}
      {validCount > 0 && rows.some(r => r.numero_parte.trim() && !r.precio_unit_cotizacion.trim()) && (
        <div className="flex items-center gap-3 px-4 py-3 rounded-xl text-sm"
          style={{ backgroundColor: 'rgba(234,179,8,0.08)', border: '1px solid rgba(234,179,8,0.2)', color: '#fbbf24' }}>
          <AlertCircle className="w-4 h-4 flex-shrink-0" />
          <span>Algunas partes no tienen precio. Se crearán igualmente y podrás editarlas en el cotizador.</span>
        </div>
      )}
    </div>
  )
}
