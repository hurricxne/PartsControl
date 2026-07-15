import { useState, useEffect, useCallback, useRef } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  ArrowLeft, RefreshCw, Save, FileText, Trash2, FileDown, FileCheck,
  AlertCircle, ExternalLink, Search, Edit2, History, User,
} from 'lucide-react'
import toast from 'react-hot-toast'
import { cotizadorAPI, clientesAPI } from '../../services/api'
import {
  calcularCotizacion, clp, usd,
  type ItemInput, type PricingConfig, type ItemCalculado,
} from './pricingCalc'

// ── tipos ──────────────────────────────────────────────────────────────────

interface EditableFields {
  margen_pct: number | null
  plazo: string
  plazo_entrega_min?: number | null
  plazo_entrega_max?: number | null
  cantidad?: number | null
  precio_unit_cotizacion?: number | null
}

// ── Celda editable inline ─────────────────────────────────────────────────

function CeldaEdit({
  value,
  onSave,
  type = 'number',
  placeholder = '',
  suffix = '',
}: {
  value: string
  onSave: (v: string) => void
  type?: 'number' | 'text'
  placeholder?: string
  suffix?: string
}) {
  const [editing, setEditing] = useState(false)
  const [val, setVal] = useState(value)

  useEffect(() => { setVal(value) }, [value])

  if (!editing) {
    return (
      <button
        onClick={() => setEditing(true)}
        className="group flex items-center gap-1 px-1.5 py-1 rounded w-full transition-colors text-left"
        style={{ color: 'var(--text-primary)' }}
        onMouseOver={e => (e.currentTarget.style.backgroundColor = 'var(--surface-300)')}
        onMouseOut={e => (e.currentTarget.style.backgroundColor = '')}
      >
        <span className="flex-1 min-w-0 truncate">{val || placeholder || '—'}</span>
        {suffix && <span className="text-[10px] opacity-60">{suffix}</span>}
        <Edit2 className="w-3 h-3 opacity-0 group-hover:opacity-50 shrink-0" style={{ color: 'var(--text-faint)' }} />
      </button>
    )
  }

  return (
    <div className="flex items-center gap-1">
      <input
        autoFocus
        type={type}
        value={val}
        onChange={e => setVal(e.target.value)}
        onKeyDown={e => {
          if (e.key === 'Enter') { onSave(val); setEditing(false) }
          if (e.key === 'Escape') { setVal(value); setEditing(false) }
        }}
        onBlur={() => { onSave(val); setEditing(false) }}
        className="rounded text-xs outline-none px-2 py-1 font-mono"
        style={{
          backgroundColor: 'var(--surface)',
          border: '2px solid #3b82f6',
          color: 'var(--text-primary)',
          width: type === 'number' ? '72px' : '120px',
        }}
      />
      {suffix && <span className="text-[10px]" style={{ color: 'var(--text-faint)' }}>{suffix}</span>}
    </div>
  )
}

// ── Badge diferencial vs Finning ──────────────────────────────────────────

function DiferencialBadge({ precioVenta, precioFinning }: { precioVenta: number; precioFinning: number }) {
  const diff = ((precioVenta - precioFinning) / precioFinning) * 100
  let color = '#22c55e'
  if (diff > 10) color = '#ef4444'
  else if (diff > 0) color = '#f59e0b'
  return (
    <span className="text-[9px] font-mono px-1 py-0.5 rounded" style={{ backgroundColor: color + '22', color }}>
      {diff > 0 ? '+' : ''}{diff.toFixed(1)}%
    </span>
  )
}

// ── Main editor ──────────────────────────────────────────────────────────

export default function CotizadorEditor() {
  const { id } = useParams<{ id: string }>()
  const cotId = Number(id)
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const [meta, setMeta] = useState({ numero: '', cliente: '', referencia: '', fase_comercial: '', rut_cliente: '', contacto_cliente: '', email_cliente: '', telefono_cliente: '', direccion_cliente: '' })
  const [cForm, setCForm] = useState({ rut: '', nombre: '', contacto: '', email: '', telefono: '', direccion: '', referencia: '' })
  const [rutError, setRutError] = useState('')
  const [lookingUp, setLookingUp] = useState(false)
  const [esFormal, setEsFormal] = useState(false)
  const [versionCount, setVersionCount] = useState(0)
  const [terminos, setTerminos] = useState('')
  const [dirtyItems, setDirtyItems] = useState<Set<number>>(new Set())
  const [generando, setGenerando] = useState(false)
  const [guardandoVersion, setGuardandoVersion] = useState(false)
  const [buscandoFinning, setBuscandoFinning] = useState(false)
  const [mostrarVersiones, setMostrarVersiones] = useState(false)
  const [resultado, setResultado] = useState<ReturnType<typeof calcularCotizacion> | null>(null)
  const [items, setItems] = useState<ItemInput[]>([])
  const [localEdits, setLocalEdits] = useState<Record<number, Partial<ItemInput>>>({})
  const [confirmDelete, setConfirmDelete] = useState<number | null>(null)
  const [savingTerminos, setSavingTerminos] = useState(false)
  const [showVersiones, setShowVersiones] = useState(false)
  const [versiones, setVersiones] = useState<any[]>([])
  const [loadingDetalle, setLoadingDetalle] = useState<number | null>(null)
  const [versionDetalle, setVersionDetalle] = useState<Record<number, any>>({})

  const { data: finningProg, isLoading: finningLoading } = useQuery({
    queryKey: ['finning-progress', cotId],
    queryFn: () => cotizadorAPI.finningProgress(cotId).then(r => r.data),
    refetchInterval: (q) => {
      const d = q.state.data as any
      return d && !d.completado ? 3000 : false
    },
    enabled: !!cotId,
  })

  const { data, isLoading, error, isFetching, refetch } = useQuery({
    queryKey: ['cotizador', cotId],
    queryFn: () => cotizadorAPI.get(cotId).then(r => r.data),
    enabled: !!cotId,
  })

  useEffect(() => {
    if (!data) return
    const raw: ItemInput[] = (data.items || []).map((i: any) => ({
      id: i.id,
      item_num: i.item_num,
      numero_parte: i.numero_parte,
      descripcion: i.descripcion,
      marca: i.marca,
      cantidad: i.cantidad,
      precio_unit_cotizacion: i.precio_unit_cotizacion,
      peso_unit_lbs: i.peso_unit_lbs || 0,
      margen_pct: i.margen_pct,
      plazo: i.plazo || '',
      plazo_entrega_min: i.plazo_entrega_min ?? data.config?.plazo_min_default ?? 30,
      plazo_entrega_max: i.plazo_entrega_max ?? data.config?.plazo_max_default ?? 45,
      precio_finning: i.precio_finning,
      nombre_cat: i.nombre_cat,
      precio_cat: i.precio_cat,
      url_cat: i.url_cat,
      encontrado: i.encontrado,
    }))
    setItems(raw)
    setMeta({ numero: data.numero || '', cliente: data.cliente || '', referencia: data.referencia || '', fase_comercial: data.fase_comercial || '', rut_cliente: data.rut_cliente || '', contacto_cliente: data.contacto_cliente || '', email_cliente: data.email_cliente || '', telefono_cliente: data.telefono_cliente || '', direccion_cliente: data.direccion_cliente || '' })
    setEsFormal(!!data.es_formal)
    setVersionCount(data.version_count || 0)
    // Load terminos (stored or default) - plazo dinamico desde items o config
    const plazos_min = raw.map((r: any) => Number(r.plazo_entrega_min)).filter((n: number) => Number.isFinite(n) && n > 0)
    const plazos_max = raw.map((r: any) => Number(r.plazo_entrega_max)).filter((n: number) => Number.isFinite(n) && n > 0)
    const cfg = data.config || {}
    const pmin = plazos_min.length > 0 ? Math.min(...plazos_min) : (cfg.plazo_min_default || 30)
    const pmax = plazos_max.length > 0 ? Math.max(...plazos_max) : (cfg.plazo_max_default || 45)
    const plazoTxt = `Plazo de entrega de ${pmin} a ${pmax} días.`
    const DEFAULT_TERMINOS = `• Precios en CLP no incluyen IVA.\n• Forma de pago: 50% anticipo, 50% contra entrega.\n• ${plazoTxt}\n• Garantía: 6 meses por defectos de fabricación.`
    setTerminos(data.terminos_condiciones || DEFAULT_TERMINOS)
    setCForm({
      rut: data.rut_cliente || '',
      nombre: data.cliente || '',
      contacto: data.contacto_cliente || '',
      email: data.email_cliente || '',
      telefono: data.telefono_cliente || '',
      direccion: data.direccion_cliente || '',
      referencia: data.referencia || '',
    })
  }, [data])

  useEffect(() => {
    if (!data?.config || items.length === 0) return
    const config: PricingConfig = { ...data.config, origen: (data as any).origen } as PricingConfig
    const merged = items.map(it => ({ ...it, ...(localEdits[it.id] || {}) }))
    const res = calcularCotizacion(merged, config)
    setResultado(res)
  }, [items, localEdits, data?.config])

  const updateItemMutation = useMutation({
    mutationFn: ({ itemId, updates }: { itemId: number; updates: Partial<ItemInput> }) =>
      cotizadorAPI.updateItem(cotId, itemId, updates),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['cotizador', cotId] }),
  })

  const deleteItemMutation = useMutation({
    mutationFn: (itemId: number) => cotizadorAPI.deleteItem(cotId, itemId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['cotizador', cotId] })
      toast.success('Ítem eliminado')
    },
  })

  const handleLocalEdit = useCallback((itemId: number, field: keyof EditableFields, value: unknown) => {
    setLocalEdits(prev => ({ ...prev, [itemId]: { ...prev[itemId], [field]: value } }))
    setDirtyItems(prev => new Set(prev).add(itemId))
  }, [])

  const handleSaveItem = useCallback(async (itemId: number) => {
    const edits = localEdits[itemId]
    if (!edits) return
    await updateItemMutation.mutateAsync({ itemId, updates: edits })
    setLocalEdits(prev => { const n = { ...prev }; delete n[itemId]; return n })
    setDirtyItems(prev => { const s = new Set(prev); s.delete(itemId); return s })
  }, [localEdits, updateItemMutation])

  const handleGuardarVersion = async () => {
    setGuardandoVersion(true)
    try {
      const res = await cotizadorAPI.guardarVersion(cotId)
      toast.success(`Versión ${res.data.version} guardada`)
      const v = await cotizadorAPI.getVersiones(cotId)
      setVersionCount(v.data.length)
    } catch { toast.error('Error guardando versión') }
    finally { setGuardandoVersion(false) }
  }

  const handleMostrarVersiones = async () => {
    if (!showVersiones) {
      try {
        const v = await cotizadorAPI.getVersiones(cotId)
        setVersiones(v.data)
        setVersionCount(v.data.length)
      } catch { toast.error('Error al cargar versiones') }
    }
    setShowVersiones(!showVersiones)
  }

  const handleGenerarFormal = async () => {
    if (!meta.cliente?.trim()) {
      toast.error('Ingresa el nombre del cliente antes de generar la cotización formal')
      return
    }
    setGenerando(true)
    try {
      await cotizadorAPI.generarFormal(cotId)
      toast.success('Cotización formal generada')
      queryClient.invalidateQueries({ queryKey: ['cotizador', cotId] })
    } catch { toast.error('Error al generar cotización formal') }
    finally { setGenerando(false) }
  }

  const handleDownloadPdf = async () => {
    setGenerando(true)
    try {
      const resp = await cotizadorAPI.downloadPdf(cotId)
      const blob = new Blob([resp.data], { type: 'application/pdf' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `CotizacionCliente-${meta.numero}.pdf`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch {
      toast.error('Error al descargar PDF')
    } finally {
      setGenerando(false)
    }
  }

  const handleSaveMeta = async (updates: Record<string, string>) => {
    setMeta(prev => ({ ...prev, ...updates }))
    try {
      await cotizadorAPI.updateMeta(cotId, updates)
    } catch {
      toast.error('Error al guardar datos del cliente')
    }
  }

  const handleDownloadFormal = async () => {
    setGenerando(true)
    try {
      const resp = await cotizadorAPI.downloadFormal(cotId)
      const url = URL.createObjectURL(new Blob([resp.data], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' }))
      const a = document.createElement('a')
      a.href = url
      a.download = `Cotizacion-${meta.numero}.xlsx`
      a.click()
      URL.revokeObjectURL(url)
    } catch { toast.error('Error al descargar Excel') } finally { setGenerando(false) }
  }

  const handleSaveTerminos = async (text: string) => {
    setTerminos(text)
    setSavingTerminos(true)
    try {
      await cotizadorAPI.updateTerminos(cotId, text)
    } catch { toast.error('Error al guardar términos') }
    finally { setSavingTerminos(false) }
  }

  const handleBuscarFinning = async () => {
    setBuscandoFinning(true)
    try {
      await cotizadorAPI.buscarFinning(cotId)
      queryClient.invalidateQueries({ queryKey: ['cotizador', cotId] })
      queryClient.invalidateQueries({ queryKey: ['finning-progress', cotId] })
    } catch { toast.error('Error al buscar precios Finning') }
    finally { setBuscandoFinning(false) }
  }

  const handleRestaurarVersion = async (vnum: number) => {
    try {
      const res = await cotizadorAPI.getVersionDetalle(cotId, vnum)
      if (res.data?.items) {
        setItems(res.data.items)
        toast.success(`Versión ${vnum} restaurada (sin guardar)`)
      }
      await cotizadorAPI.restaurarVersion(cotId, vnum)
      queryClient.invalidateQueries({ queryKey: ['cotizador', cotId] })
      toast.success(`Versión ${vnum} restaurada`)
    } catch { toast.error('Error al restaurar versión') }
  }

  // Derived: items merged with local edits
  const localItems = items.map(it => ({ ...it, ...(localEdits[it.id] || {}) }))

  // Convenience wrappers
  const updateField = (itemId: number, field: keyof EditableFields, value: unknown) => {
    handleLocalEdit(itemId, field, value as any)
  }

  // ── Aplicar por columna a TODOS + Guardar todo (usabilidad en listados) ──
  const bulkMargenRef = useRef<HTMLInputElement>(null)
  const bulkMinRef = useRef<HTMLInputElement>(null)
  const bulkMaxRef = useRef<HTMLInputElement>(null)

  const applyBulk = (ref: { current: HTMLInputElement | null }, field: keyof EditableFields, transform: (n: number) => number = (n) => n) => {
    const s = String(ref.current?.value ?? '').replace(',', '.').trim()
    const n = parseFloat(s)
    if (isNaN(n)) { toast.error('Ingresa un número'); return }
    // Validación de plazos (independiente del margen): máx no puede ser menor que mín.
    if (field === 'plazo_entrega_max') {
      const minN = parseFloat(String(bulkMinRef.current?.value ?? '').replace(',', '.').trim())
      if (!isNaN(minN) && n < minN) { toast.error('El plazo máximo no puede ser menor que el mínimo'); return }
    }
    if (field === 'plazo_entrega_min') {
      const maxN = parseFloat(String(bulkMaxRef.current?.value ?? '').replace(',', '.').trim())
      if (!isNaN(maxN) && n > maxN) { toast.error('El plazo mínimo no puede ser mayor que el máximo'); return }
    }
    items.forEach((it: any) => updateField(it.id, field, transform(n)))
    toast.success('Aplicado a todos los ítems')
  }

  const handleSaveAll = async () => {
    const ids = Object.keys(localEdits).map(Number)
    if (!ids.length) { toast('No hay cambios por guardar'); return }
    for (const id of ids) { await handleSaveItem(id) }
    toast.success(`Guardados ${ids.length} ítem(s)`)
  }

  // ── Scroll horizontal duplicado (arriba + abajo), sincronizado ──
  const topScrollRef = useRef<HTMLDivElement>(null)
  const bodyScrollRef = useRef<HTMLDivElement>(null)
  const [tableScrollW, setTableScrollW] = useState(2200)
  useEffect(() => {
    const measure = () => { if (bodyScrollRef.current) setTableScrollW(bodyScrollRef.current.scrollWidth) }
    measure()
    const t = setTimeout(measure, 150)
    window.addEventListener('resize', measure)
    return () => { clearTimeout(t); window.removeEventListener('resize', measure) }
  }, [items, resultado])
  const syncFromTop = () => { if (bodyScrollRef.current && topScrollRef.current) bodyScrollRef.current.scrollLeft = topScrollRef.current.scrollLeft }
  const syncFromBody = () => { if (bodyScrollRef.current && topScrollRef.current) topScrollRef.current.scrollLeft = bodyScrollRef.current.scrollLeft }

  const saveItemMutation = {
    isPending: updateItemMutation.isPending,
  }

  const saveItem = async (item: { id: number }) => {
    await handleSaveItem(item.id)
  }

  const handleVerVersion = async (vnum: number) => {
    if (versionDetalle[vnum]) {
      setVersionDetalle(prev => { const n = { ...prev }; delete n[vnum]; return n })
      return
    }
    setLoadingDetalle(vnum)
    try {
      const res = await cotizadorAPI.getVersionDetalle(cotId, vnum)
      setVersionDetalle(prev => ({ ...prev, [vnum]: res.data }))
    } catch { toast.error('Error al cargar versión') }
    finally { setLoadingDetalle(null) }
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64 gap-3" style={{ color: 'var(--text-faint)' }}>
        <RefreshCw className="w-5 h-5 animate-spin" />
        <span>Cargando cotización...</span>
      </div>
    )
  }

  if (error || !data) {
    return (
      <div className="card p-8 text-center">
        <AlertCircle className="w-8 h-8 text-red-400 mx-auto mb-2" />
        <p style={{ color: 'var(--text-muted)' }}>No se pudo cargar la cotización</p>
        <button onClick={() => navigate('/cotizaciones')} className="btn-secondary mt-4">
          <ArrowLeft className="w-4 h-4" /> Volver
        </button>
      </div>
    )
  }

  const totales = resultado?.totales
  const itemsCalc = resultado?.items ?? []
  const hayPendientesFinning = finningProg ? !finningProg.completado : false
  const FASES_CERRADAS = ['validada', 'semi_validada', 'pre_embarcado', 'embarcado', 'pagado']
  const faseCerrada = FASES_CERRADAS.includes(meta.fase_comercial)

  const ITEM_W = 46
  const PARTE_W = 134

  const TH = ({
    children,
    right = false,
    stickyLeft,
    minW,
  }: {
    children: string
    right?: boolean
    stickyLeft?: number
    minW?: number
  }) => (
    <th
      className={`px-2 py-2.5 font-semibold text-[10px] uppercase tracking-wide whitespace-nowrap ${right ? 'text-right' : 'text-left'}`}
      style={{
        color: 'var(--text-faint)',
        backgroundColor: 'var(--surface-200)',
        position: 'sticky',
        top: 0,
        zIndex: stickyLeft !== undefined ? 40 : 20,
        ...(stickyLeft !== undefined ? { left: stickyLeft, borderRight: '1px solid var(--border)' } : {}),
        ...(minW ? { minWidth: minW } : {}),
      }}
    >
      {children}
    </th>
  )

  // ── RUT helpers ─────────────────────────────────────────
  const cleanRut = (rut: string) => rut.replace(/[^0-9kK]/g, '').toUpperCase()

  const formatRut = (rut: string): string => {
    const clean = cleanRut(rut)
    if (clean.length < 2) return clean
    const body = clean.slice(0, -1)
    const dv = clean.slice(-1)
    const formatted = body.replace(/\B(?=(\d{3})+(?!\d))/g, '.')
    return `${formatted}-${dv}`
  }

  const validateRut = (rut: string): boolean => {
    const clean = cleanRut(rut)
    if (clean.length < 2) return false
    const body = clean.slice(0, -1)
    const dv = clean.slice(-1)
    if (!/^\d+$/.test(body)) return false
    let sum = 0; let factor = 2
    for (let i = body.length - 1; i >= 0; i--) {
      sum += parseInt(body[i]) * factor
      factor = factor === 7 ? 2 : factor + 1
    }
    const expected = 11 - (sum % 11)
    const dvExpected = expected === 11 ? '0' : expected === 10 ? 'K' : String(expected)
    return dv === dvExpected
  }

  const handleRutChange = (raw: string) => {
    // Allow typing freely, just strip invalid chars
    const clean = cleanRut(raw)
    if (clean.length === 0) { setCForm(p => ({ ...p, rut: '' })); setRutError(''); return }
    // Format on-the-fly only if it looks complete (has at least 7 chars)
    const display = clean.length >= 7 ? formatRut(clean) : raw.replace(/[^0-9kK.\-]/gi, '')
    setCForm(p => ({ ...p, rut: display }))
    if (rutError) setRutError('')
  }

  const handleRutBlur = async () => {
    if (!cForm.rut) { setRutError(''); await handleSaveMeta({ rut_cliente: '' }); return }
    const clean = cleanRut(cForm.rut)
    if (clean.length < 2) { setRutError('RUT inválido'); return }
    const formatted = formatRut(clean)
    setCForm(p => ({ ...p, rut: formatted }))
    if (!validateRut(clean)) { setRutError('RUT inválido — verifica el dígito verificador'); return }
    setRutError('')
    await handleSaveMeta({ rut_cliente: formatted })
    setLookingUp(true)
    try {
      const { data } = await clientesAPI.byRut(formatted)
      if (data.found) {
        const updates: Record<string, string> = {}
        const patch: Partial<typeof cForm> = {}
        if (data.nombre) { updates.cliente = data.nombre; patch.nombre = data.nombre }
        if (data.contacto) { updates.contacto_cliente = data.contacto; patch.contacto = data.contacto }
        if (data.email) { updates.email_cliente = data.email; patch.email = data.email }
        if (data.telefono) { updates.telefono_cliente = data.telefono; patch.telefono = data.telefono }
        if (data.direccion) { updates.direccion_cliente = data.direccion; patch.direccion = data.direccion }
        setCForm(p => ({ ...p, ...patch }))
        if (Object.keys(updates).length) await handleSaveMeta(updates)
        toast.success(`Cliente "${data.nombre}" encontrado — datos cargados`)
      } else {
        toast('RUT válido — cliente nuevo', { icon: 'ℹ️' })
      }
    } catch { /* ignore */ } finally { setLookingUp(false) }
  }

  return (<>
    <div className="space-y-4 animate-slide-up">
      {/* ── Header ── */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-3">
          <button onClick={() => navigate('/cotizaciones')} className="btn-secondary text-sm py-1.5 px-3">
            <ArrowLeft className="w-4 h-4" />
          </button>
          <div>
            <h1 className="text-xl font-bold" style={{ color: 'var(--text-primary)' }}>
              {esFormal || versionCount > 0 ? 'Cotización' : 'Pre-Cotización'} — {esFormal || versionCount > 0 ? 'COT-' : 'PRE-COT-'}{meta.numero}
            </h1>
            <div className="flex items-center gap-2 flex-wrap mt-0.5">
              <span className="flex items-center gap-1.5 text-sm" style={{ color: meta.cliente ? 'var(--text-muted)' : '#f87171' }}>
                <User className="w-3.5 h-3.5" />
                {meta.cliente || '⚠ Completa los datos del cliente'}
              </span>
              {meta.rut_cliente && <span className="text-xs font-mono" style={{ color: 'var(--text-faint)' }}>{meta.rut_cliente}</span>}
              {meta.referencia && <span className="text-xs" style={{ color: 'var(--text-faint)' }}>· Ref: {meta.referencia}</span>}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          {dirtyItems.size > 0 && (
            <span className="text-xs text-amber-400 flex items-center gap-1">
              <AlertCircle className="w-3 h-3" />
              {dirtyItems.size} cambio(s) sin guardar
            </span>
          )}
          {faseCerrada && (
            <span className="text-xs px-2 py-1 rounded-lg bg-amber-500/10 text-amber-400 border border-amber-500/20 flex items-center gap-1">
              Solo lectura — {meta.fase_comercial}
            </span>
          )}
          <button onClick={handleMostrarVersiones} className="btn-secondary text-sm flex items-center gap-1.5">
            <History className="w-4 h-4" />
            Historial
            {versionCount > 0 && (
              <span className="ml-0.5 text-[10px] font-bold px-1.5 py-0.5 rounded-full bg-brand-500/20 text-brand-400">
                {versionCount}
              </span>
            )}
          </button>
          {!faseCerrada && !esFormal && (
            <button onClick={handleGuardarVersion} disabled={guardandoVersion} className="btn-secondary text-sm flex items-center gap-1.5">
              {guardandoVersion ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
              Guardar versión
            </button>
          )}
          <button
            onClick={handleBuscarFinning}
            disabled={buscandoFinning}
            className="btn-secondary text-sm"
            title="Buscar precios en parts.cat.com/es/finningchile"
          >
            {buscandoFinning
              ? <RefreshCw className="w-4 h-4 animate-spin" />
              : <Search className="w-4 h-4" />
            }
            {hayPendientesFinning ? 'Buscar precios Finning' : 'Actualizar Finning'}
          </button>
          {isFetching && !isLoading && (
            <span className="text-xs flex items-center gap-1" style={{ color: 'var(--text-faint)' }}>
              <RefreshCw className="w-3 h-3 animate-spin" />
            </span>
          )}
          {esFormal ? (
            <>
              <button onClick={handleGenerarFormal} disabled={generando}
                className="btn-secondary text-sm flex items-center gap-1.5"
                title="Guarda una nueva versión sin descargar">
                {generando ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                Guardar versión
              </button>
              <button onClick={handleDownloadPdf} disabled={generando}
                className="text-sm flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-orange-500 text-white hover:bg-orange-600 transition-colors font-medium"
                title="PDF cotización para cliente">
                <FileText className="w-4 h-4" />
                PDF cliente
              </button>
              <button onClick={handleDownloadFormal} disabled={generando}
                className="btn-primary text-sm flex items-center gap-1.5"
                title="Guarda y descarga el Excel actualizado">
                {generando ? <RefreshCw className="w-4 h-4 animate-spin" /> : <FileDown className="w-4 h-4" />}
                Guardar y Descargar
              </button>
            </>
          ) : (
            <button onClick={handleGenerarFormal} disabled={generando} className="btn-primary text-sm">
              {generando ? <RefreshCw className="w-4 h-4 animate-spin" /> : <FileCheck className="w-4 h-4" />}
              Generar Cotización Formal
            </button>
          )}
        </div>
      </div>

      {/* ── Datos del Cliente ── */}
      <div className="card p-4">
        {/* Header */}
        <div className="flex items-center justify-between mb-4">
          <h3 className="font-semibold text-sm flex items-center gap-2" style={{ color: 'var(--text-primary)' }}>
            <User className="w-4 h-4" style={{ color: 'var(--empresa-primary)' }} />
            Datos del Cliente
            {!meta.cliente && (
              <span className="text-[10px] font-normal text-red-400">— completa el nombre para generar cotización formal</span>
            )}
          </h3>
          {lookingUp && (
            <span className="text-xs flex items-center gap-1.5 px-2 py-1 rounded-md" style={{ color: '#60a5fa', backgroundColor: 'rgba(59,130,246,0.08)' }}>
              <RefreshCw className="w-3 h-3 animate-spin" /> Buscando en base de datos…
            </span>
          )}
        </div>

        {/* Fila 1: RUT + Nombre */}
        <div className="grid grid-cols-12 gap-3 mb-3">
          <div className="col-span-3">
            <label className="block text-[10px] font-semibold uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>
              RUT <span className="font-normal normal-case" style={{ color: 'var(--text-faint)' }}>(busca cliente autom.)</span>
            </label>
            <input
              className="input w-full font-mono"
              placeholder="12.345.678-9"
              value={cForm.rut}
              onChange={e => handleRutChange(e.target.value)}
              onBlur={handleRutBlur}
              disabled={faseCerrada}
              style={rutError ? { borderColor: '#f87171' } : {}}
            />
            {rutError
              ? <p className="text-[10px] text-red-400 mt-0.5 flex items-center gap-1"><AlertCircle className="w-3 h-3" />{rutError}</p>
              : cForm.rut && !rutError && !lookingUp && <p className="text-[10px] mt-0.5" style={{ color: '#4ade80' }}>✓ RUT válido</p>
            }
          </div>
          <div className="col-span-9">
            <label className="block text-[10px] font-semibold uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>
              Nombre / Razón Social <span className="text-red-400">*</span>
            </label>
            <input
              className="input w-full"
              placeholder="Empresa o Persona"
              value={cForm.nombre}
              onChange={e => setCForm(p => ({ ...p, nombre: e.target.value }))}
              onBlur={e => handleSaveMeta({ cliente: e.target.value })}
              disabled={faseCerrada}
            />
          </div>
        </div>

        {/* Fila 2: Contacto + Email + Teléfono */}
        <div className="grid grid-cols-3 gap-3 mb-3">
          <div>
            <label className="block text-[10px] font-semibold uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>Contacto</label>
            <input
              className="input w-full"
              placeholder="Nombre contacto"
              value={cForm.contacto}
              onChange={e => setCForm(p => ({ ...p, contacto: e.target.value }))}
              onBlur={e => handleSaveMeta({ contacto_cliente: e.target.value })}
              disabled={faseCerrada}
            />
          </div>
          <div>
            <label className="block text-[10px] font-semibold uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>Email</label>
            <input
              className="input w-full"
              placeholder="contacto@empresa.cl"
              type="email"
              value={cForm.email}
              onChange={e => setCForm(p => ({ ...p, email: e.target.value }))}
              onBlur={e => handleSaveMeta({ email_cliente: e.target.value })}
              disabled={faseCerrada}
            />
          </div>
          <div>
            <label className="block text-[10px] font-semibold uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>Teléfono</label>
            <input
              className="input w-full"
              placeholder="+56 9 XXXX XXXX"
              value={cForm.telefono}
              onChange={e => setCForm(p => ({ ...p, telefono: e.target.value }))}
              onBlur={e => handleSaveMeta({ telefono_cliente: e.target.value })}
              disabled={faseCerrada}
            />
          </div>
        </div>

        {/* Fila 3: Dirección + Referencia */}
        <div className="grid grid-cols-3 gap-3">
          <div className="col-span-2">
            <label className="block text-[10px] font-semibold uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>Dirección</label>
            <input
              className="input w-full"
              placeholder="Av. Industrial 1234, Santiago"
              value={cForm.direccion}
              onChange={e => setCForm(p => ({ ...p, direccion: e.target.value }))}
              onBlur={e => handleSaveMeta({ direccion_cliente: e.target.value })}
              disabled={faseCerrada}
            />
          </div>
          <div>
            <label className="block text-[10px] font-semibold uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>Referencia / OT</label>
            <input
              className="input w-full"
              placeholder="OT-2026-001"
              value={cForm.referencia}
              onChange={e => setCForm(p => ({ ...p, referencia: e.target.value }))}
              onBlur={e => handleSaveMeta({ referencia: e.target.value })}
              disabled={faseCerrada}
            />
          </div>
        </div>

        <p className="text-[11px] mt-3 pt-3 border-t" style={{ color: 'var(--text-faint)', borderColor: 'var(--border)' }}>
          Cada campo se guarda automáticamente al salir. Ingresa el RUT para buscar el cliente en la base de datos y autocompletar.
        </p>
      </div>

      {/* ── Barra de progreso Finning ── */}
      {finningProg && !finningProg.completado && (
        <div
          className="rounded-lg px-4 py-3"
          style={{
            backgroundColor: 'rgba(59,130,246,0.08)',
            border: '1px solid rgba(59,130,246,0.2)',
          }}
        >
          <div className="flex items-center justify-between mb-2">
            <div className="flex items-center gap-2 text-sm" style={{ color: '#60a5fa' }}>
              <RefreshCw className="w-3.5 h-3.5 animate-spin flex-shrink-0" />
              <span>Buscando precios Finning en background...</span>
            </div>
            <span className="text-xs font-semibold" style={{ color: '#93c5fd' }}>
              {finningProg.con_precio} / {finningProg.total} ({finningProg.pct}%)
            </span>
          </div>
          <div className="h-2 rounded-full overflow-hidden" style={{ backgroundColor: 'rgba(59,130,246,0.15)' }}>
            <div
              className="h-2 rounded-full transition-all duration-700"
              style={{
                width: `${finningProg.pct}%`,
                backgroundColor: '#3b82f6',
                minWidth: finningProg.con_precio > 0 ? '6px' : '0',
              }}
            />
          </div>
          <p className="text-[11px] mt-1.5" style={{ color: 'rgba(147,197,253,0.7)' }}>
            {finningProg.sin_precio} parte(s) pendiente(s) · La columna "Precio Finning" se actualiza automáticamente
          </p>
        </div>
      )}

      {/* ── Aplicar a todos + Guardar todo ── */}
      {!faseCerrada && (
        <div className="card mb-3 p-3 flex flex-wrap items-end gap-3">
          <span className="text-xs font-semibold uppercase tracking-wide self-center" style={{ color: 'var(--text-faint)' }}>Aplicar a todos:</span>
          <div className="flex flex-col gap-1">
            <label className="text-[10px] uppercase" style={{ color: 'var(--text-faint)' }}>Margen %</label>
            <div className="flex gap-1">
              <input ref={bulkMargenRef} type="number" defaultValue="" placeholder="19"
                className="w-20 px-2 py-1 rounded text-xs" style={{ background: 'var(--surface-100)', border: '1px solid var(--border)', color: 'var(--text-primary)' }} />
              <button onClick={() => applyBulk(bulkMargenRef, 'margen_pct', n => n / 100)} className="btn-secondary text-xs px-2">Aplicar</button>
            </div>
          </div>
          <div className="flex flex-col gap-1">
            <label className="text-[10px] uppercase" style={{ color: 'var(--text-faint)' }}>Pzo mín (d)</label>
            <div className="flex gap-1">
              <input ref={bulkMinRef} type="number" defaultValue="" placeholder="10"
                className="w-20 px-2 py-1 rounded text-xs" style={{ background: 'var(--surface-100)', border: '1px solid var(--border)', color: 'var(--text-primary)' }} />
              <button onClick={() => applyBulk(bulkMinRef, 'plazo_entrega_min')} className="btn-secondary text-xs px-2">Aplicar</button>
            </div>
          </div>
          <div className="flex flex-col gap-1">
            <label className="text-[10px] uppercase" style={{ color: 'var(--text-faint)' }}>Pzo máx (d)</label>
            <div className="flex gap-1">
              <input ref={bulkMaxRef} type="number" defaultValue="" placeholder="20"
                className="w-20 px-2 py-1 rounded text-xs" style={{ background: 'var(--surface-100)', border: '1px solid var(--border)', color: 'var(--text-primary)' }} />
              <button onClick={() => applyBulk(bulkMaxRef, 'plazo_entrega_max')} className="btn-secondary text-xs px-2">Aplicar</button>
            </div>
          </div>
          <div className="ml-auto self-end">
            <button onClick={handleSaveAll} disabled={saveItemMutation.isPending}
              className="btn-primary text-sm flex items-center gap-1.5 px-4 py-2">
              <Save className="w-4 h-4" /> Guardar todo
            </button>
          </div>
        </div>
      )}

      {/* ── Tabla ── */}
      <div className="card overflow-hidden">
        {/* Scroll horizontal superior (sincronizado con el de abajo) */}
        <div ref={topScrollRef} onScroll={syncFromTop} className="overflow-x-auto" style={{ overflowY: 'hidden' }}>
          <div style={{ width: tableScrollW, height: 1 }} />
        </div>
        <div
          ref={bodyScrollRef}
          onScroll={syncFromBody}
          className="overflow-x-auto overflow-y-auto"
          style={{ maxHeight: 'calc(100vh - 310px)' }}
        >
          <table className="w-full text-xs border-collapse" style={{ minWidth: '2200px' }}>
            <thead>
              <tr style={{ borderBottom: '2px solid var(--border)' }}>
                <TH stickyLeft={0} minW={ITEM_W}>ITEM</TH>
                <TH stickyLeft={ITEM_W} minW={PARTE_W}>N° PARTE</TH>
                <TH minW={180}>DESCRIPCIÓN</TH>
                <TH minW={80}>MARCA</TH>
                <TH right minW={55}>QTY</TH>
                <TH right minW={100}>Ex Work</TH>
                <TH right minW={110}>Total Ex Work</TH>
                <TH right minW={82}>Peso Unit (lb)</TH>
                <TH right minW={82}>Peso Tot (lb)</TH>
                <TH right minW={82}>Peso Tot (kg)</TH>
                <TH right minW={95}>Shipping</TH>
                <TH right minW={95}>Adic. Ship.</TH>
                <TH right minW={110}>CIF CLP</TH>
                <TH right minW={95}>Gtos. Loc.</TH>
                <TH right minW={110}>Costo Total</TH>
                <TH right minW={110}>Costo Unit.</TH>
                <TH right minW={95}>Margen %</TH>
                <TH right minW={115}>Precio Venta</TH>
                <TH right minW={115}>Total Venta</TH>
                <TH right minW={115}>Precio Finning</TH>
                <TH right minW={115}>Total Finning</TH>
                <TH right minW={100}>Diferencial</TH>
                <TH minW={135}>Plazo Entrega</TH>
                <TH right minW={80}>Pzo. Mín.</TH>
                <TH right minW={80}>Pzo. Máx.</TH>
                <TH minW={70}>Acc.</TH>
              </tr>
            </thead>
            <tbody>
              {itemsCalc.map((item, idx) => {
                const isDirty = dirtyItems.has(item.id)
                const localItem = localItems.find(i => i.id === item.id)!
                const rowBg = isDirty
                  ? 'rgba(234,179,8,0.06)'
                  : idx % 2 === 0 ? 'var(--surface-100)' : 'var(--surface)'

                // Sticky cells need a solid (opaque) background so scrolled content
                // doesn't bleed through. We keep the dirty indicator as a left border.
                const solidBg = idx % 2 === 0 ? 'var(--surface-100)' : 'var(--surface)'

                const stickyTd = (left: number, extra?: React.CSSProperties): React.CSSProperties => ({
                  position: 'sticky',
                  left,
                  zIndex: 10,
                  backgroundColor: solidBg,
                  borderRight: '1px solid var(--border)',
                  ...(isDirty && left === 0 ? { borderLeft: '2px solid rgba(234,179,8,0.5)' } : {}),
                  ...extra,
                })

                const precioFinning = item.precio_cat
                const totalFinning = precioFinning != null ? precioFinning * item.cantidad : null

                return (
                  <tr key={item.id} style={{ borderBottom: '1px solid var(--border)' }}>
                    {/* ITEM — sticky */}
                    <td className="px-2 py-2 text-center text-[11px]"
                      style={stickyTd(0, { color: 'var(--text-faint)', width: ITEM_W })}>
                      {item.item_num}
                    </td>

                    {/* N° PARTE — sticky */}
                    <td className="px-2 py-2" style={stickyTd(ITEM_W, { width: PARTE_W })}>
                      <div className="flex items-center gap-1">
                        <span className="font-mono font-semibold text-[11px]" style={{ color: 'var(--text-primary)' }}>
                          {item.numero_parte}
                        </span>
                        {item.url_cat && (
                          <a href={item.url_cat} target="_blank" rel="noopener noreferrer">
                            <ExternalLink className="w-3 h-3 text-brand-400 hover:text-brand-300" />
                          </a>
                        )}
                      </div>
                    </td>

                    {/* DESCRIPCIÓN */}
                    <td className="px-2 py-2" style={{ color: 'var(--text-muted)', maxWidth: 200 }}>
                      <span className="line-clamp-2 text-[11px]">{item.descripcion}</span>
                    </td>

                    {/* MARCA */}
                    <td className="px-2 py-2 text-[11px]" style={{ color: 'var(--text-muted)' }}>
                      {item.marca || '—'}
                    </td>

                    {/* QTY — EDITABLE */}
                    <td className="px-1 py-1 text-right" style={{ color: 'var(--text-primary)', minWidth: 70 }}>
                      {!faseCerrada ? (
                        <CeldaEdit
                          value={String(item.cantidad ?? '')}
                          onSave={v => { const n = parseFloat(v); updateField(item.id, 'cantidad', isNaN(n) ? null : n) }}
                        />
                      ) : (
                        item.cantidad
                      )}
                    </td>

                    {/* Ex Work */}
                    <td className="px-2 py-2 text-right text-[11px]" style={{ color: 'var(--text-muted)' }}>
                      {usd(item.precio_unit_cotizacion)}
                    </td>

                    {/* Total Ex Work */}
                    <td className="px-2 py-2 text-right text-[11px]" style={{ color: 'var(--text-muted)' }}>
                      {usd(item.total_exwork_usd)}
                    </td>

                    {/* Peso Unit */}
                    <td className="px-2 py-2 text-right text-[11px]" style={{ color: 'var(--text-muted)' }}>
                      {item.peso_unit_lbs?.toFixed(2) ?? '—'}
                    </td>

                    {/* Peso Total lbs */}
                    <td className="px-2 py-2 text-right text-[11px]" style={{ color: 'var(--text-muted)' }}>
                      {item.peso_total_lbs?.toFixed(2) ?? '—'}
                    </td>

                    {/* Peso Total kg */}
                    <td className="px-2 py-2 text-right text-[11px]" style={{ color: 'var(--text-muted)' }}>
                      {item.peso_total_kg?.toFixed(2) ?? '—'}
                    </td>

                    {/* Shipping */}
                    <td className="px-2 py-2 text-right text-[11px]" style={{ color: 'var(--text-muted)' }}>
                      {clp(item.shipping_item_clp)}
                    </td>

                    {/* Adic. Shipping */}
                    <td className="px-2 py-2 text-right text-[11px]" style={{ color: 'var(--text-muted)' }}>
                      {clp(item.adic_shipping_clp)}
                    </td>

                    {/* CIF CLP */}
                    <td className="px-2 py-2 text-right font-medium text-[11px]" style={{ color: 'var(--text-primary)' }}>
                      {clp(item.cif_clp)}
                    </td>

                    {/* Gastos Locales */}
                    <td className="px-2 py-2 text-right text-[11px]" style={{ color: 'var(--text-muted)' }}>
                      {clp(item.gastos_locales_clp)}
                    </td>

                    {/* Costo Total */}
                    <td className="px-2 py-2 text-right text-[11px]" style={{ color: 'var(--text-muted)' }}>
                      {clp(item.costo_total_clp)}
                    </td>

                    {/* Costo Unitario */}
                    <td className="px-2 py-2 text-right text-[11px]" style={{ color: 'var(--text-muted)' }}>
                      {clp(item.costo_unitario_clp)}
                    </td>

                    {/* MARGEN % — EDITABLE */}
                    <td className="px-1 py-1 text-right" style={{ minWidth: 95 }}>
                      <CeldaEdit
                        value={(item.margen_efectivo * 100).toFixed(1)}
                        onSave={v => { const n = parseFloat(v); updateField(item.id, 'margen_pct', isNaN(n) ? null : n / 100) }}
                        suffix="%"
                      />
                    </td>

                    {/* Precio Venta — EDITABLE (venta_clp fija precio; costo mueve margen) */}
                    <td className="px-1 py-1 text-right font-semibold text-brand-400" style={{ minWidth: 110 }}>
                      {!faseCerrada ? (
                        <CeldaEdit
                          value={String(Math.round(item.precio_venta_clp || 0))}
                          onSave={v => {
                            const n = parseFloat(v.replace(/[^0-9.-]/g, ''))
                            if (isNaN(n)) return
                            const costo = item.costo_unitario_clp || 0
                            updateField(item.id, 'margen_pct', costo > 0 ? (n / costo - 1) : (item.margen_pct ?? null))
                          }}
                        />
                      ) : (
                        clp(item.precio_venta_clp)
                      )}
                    </td>

                    {/* Total Venta */}
                    <td className="px-2 py-2 text-right font-semibold text-brand-400">
                      {clp(item.total_venta_clp)}
                    </td>

                    {/* PRECIO FINNING */}
                    <td className="px-2 py-2 text-right text-[11px]" style={{ color: 'var(--text-muted)' }}>
                      {precioFinning != null
                        ? clp(precioFinning)
                        : hayPendientesFinning
                          ? <span style={{ color: 'var(--text-faint)' }}>⏳</span>
                          : <span style={{ color: 'var(--text-faint)' }}>—</span>
                      }
                    </td>

                    {/* TOTAL VENTA FINNING */}
                    <td className="px-2 py-2 text-right text-[11px]" style={{ color: 'var(--text-muted)' }}>
                      {totalFinning != null
                        ? clp(totalFinning)
                        : <span style={{ color: 'var(--text-faint)' }}>—</span>
                      }
                    </td>

                    {/* DIFERENCIAL */}
                    <td className="px-2 py-2 text-right">
                      {precioFinning != null
                        ? <DiferencialBadge precioVenta={item.precio_venta_clp} precioFinning={precioFinning} />
                        : <span className="text-[11px]" style={{ color: 'var(--text-faint)' }}>—</span>
                      }
                    </td>

                    {/* PLAZO — EDITABLE */}
                    <td className="px-1 py-1" style={{ minWidth: 135 }}>
                      {!faseCerrada ? (
                        <CeldaEdit
                          type="text"
                          value={(localItem as any)?.plazo || ''}
                          placeholder="Por confirmar"
                          onSave={v => updateField(item.id, 'plazo', v)}
                        />
                      ) : (
                        <span className="text-[11px] px-2" style={{ color: 'var(--text-muted)' }}>
                          {(localItem as any)?.plazo || '—'}
                        </span>
                      )}
                    </td>

                    {/* PLAZO MÍNIMO */}
                    <td className="px-1 py-1 text-right" style={{ minWidth: 80 }}>
                      {!faseCerrada ? (
                        <CeldaEdit
                          type="number"
                          value={(localItem as any)?.plazo_entrega_min != null ? String((localItem as any).plazo_entrega_min) : ''}
                          placeholder="—"
                          onSave={v => updateField(item.id, 'plazo_entrega_min', v)}
                          suffix="d"
                        />
                      ) : (
                        <span className="text-[11px] px-2" style={{ color: 'var(--text-muted)' }}>
                          {(localItem as any)?.plazo_entrega_min != null ? `${(localItem as any).plazo_entrega_min}d` : '—'}
                        </span>
                      )}
                    </td>

                    {/* PLAZO MÁXIMO */}
                    <td className="px-1 py-1 text-right" style={{ minWidth: 80 }}>
                      {!faseCerrada ? (
                        <CeldaEdit
                          type="number"
                          value={(localItem as any)?.plazo_entrega_max != null ? String((localItem as any).plazo_entrega_max) : ''}
                          placeholder="—"
                          onSave={v => updateField(item.id, 'plazo_entrega_max', v)}
                          suffix="d"
                        />
                      ) : (
                        <span className="text-[11px] px-2" style={{ color: 'var(--text-muted)' }}>
                          {(localItem as any)?.plazo_entrega_max != null ? `${(localItem as any).plazo_entrega_max}d` : '—'}
                        </span>
                      )}
                    </td>

                    {/* Acciones */}
                    <td className="px-2 py-2 text-center" style={{ minWidth: 70 }}>
                      <div className="flex items-center justify-center gap-1">
                        {isDirty && !faseCerrada && (
                          <button
                            onClick={() => saveItem(item)}
                            disabled={saveItemMutation.isPending}
                            title="Guardar"
                            className="p-1 rounded hover:bg-brand-500/10 transition-colors"
                          >
                            <Save className="w-3.5 h-3.5 text-brand-400" />
                          </button>
                        )}
                        {confirmDelete === item.id ? (
                          <div className="flex items-center gap-0.5">
                            <button
                              onClick={() => deleteItemMutation.mutate(item.id)}
                              className="px-1.5 py-0.5 rounded text-[10px] bg-red-500/20 text-red-400 border border-red-500/30 hover:bg-red-500/30"
                            >Sí</button>
                            <button
                              onClick={() => setConfirmDelete(null)}
                              className="px-1.5 py-0.5 rounded text-[10px] btn-secondary"
                            >No</button>
                          </div>
                        ) : (
                          <button
                            onClick={() => setConfirmDelete(item.id)}
                            title="Eliminar ítem"
                            className="p-1 rounded hover:bg-red-500/10 transition-colors"
                          >
                            <Trash2 className="w-3.5 h-3.5" style={{ color: 'var(--text-faint)' }} />
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>

        {/* ── Totales ── */}
        {totales && (
          <div
            className="border-t px-4 py-3 flex flex-wrap items-center justify-between gap-4"
            style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}
          >
            <div className="flex flex-wrap gap-6 text-xs">
              {([
                ['Ex Work total', usd(totales.total_exwork_usd)],
                ['Peso total', `${totales.total_peso_kg.toFixed(1)} kg`],
                ['CIF total', clp(totales.total_cif_clp)],
                ['Gastos locales', clp(totales.gastos_locales_total_clp)],
              ] as [string, string][]).map(([label, val]) => (
                <div key={label}>
                  <span className="block" style={{ color: 'var(--text-faint)' }}>{label}</span>
                  <p className="font-semibold text-sm" style={{ color: 'var(--text-primary)' }}>{val}</p>
                </div>
              ))}
            </div>
            <div className="flex flex-wrap gap-6 text-xs">
              <div className="text-right">
                <span style={{ color: 'var(--text-faint)' }}>Subtotal neto</span>
                <p className="font-bold text-sm text-brand-400">{clp(totales.subtotal_neto_clp)}</p>
              </div>
              <div className="text-right">
                <span style={{ color: 'var(--text-faint)' }}>IVA 19%</span>
                <p className="font-semibold text-sm" style={{ color: 'var(--text-primary)' }}>{clp(totales.iva_clp)}</p>
              </div>
              <div className="text-right px-3 py-1 rounded-lg" style={{ backgroundColor: 'var(--surface-300)' }}>
                <span style={{ color: 'var(--text-faint)' }}>TOTAL c/IVA</span>
                <p className="font-bold text-base text-brand-400">{clp(totales.total_con_iva_clp)}</p>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* ── Términos y Condiciones ── */}
      <div className="card p-4 space-y-2">
        <div className="flex items-center justify-between">
          <h3 className="font-semibold text-sm flex items-center gap-2" style={{ color: 'var(--text-primary)' }}>
            <FileText className="w-4 h-4" style={{ color: 'var(--empresa-primary)' }} />
            Términos y Condiciones
          </h3>
          {savingTerminos && <RefreshCw className="w-3.5 h-3.5 animate-spin" style={{ color: 'var(--text-faint)' }} />}
        </div>
        {faseCerrada ? (
          <pre className="text-xs whitespace-pre-wrap rounded-lg p-3" style={{ color: 'var(--text-muted)', backgroundColor: 'var(--surface-200)', fontFamily: 'inherit' }}>
            {terminos}
          </pre>
        ) : (
          <textarea
            value={terminos}
            onChange={e => setTerminos(e.target.value)}
            onBlur={e => handleSaveTerminos(e.target.value)}
            rows={5}
            placeholder="Ingrese los términos y condiciones de la cotización..."
            className="w-full rounded-lg px-3 py-2 text-xs resize-y outline-none transition-colors"
            style={{
              backgroundColor: 'var(--surface-200)',
              border: '1.5px solid var(--border)',
              color: 'var(--text-primary)',
              fontFamily: 'inherit',
              lineHeight: '1.6',
            }}
            onFocus={e => (e.target.style.borderColor = 'var(--empresa-primary)')}
          />
        )}
        {!faseCerrada && (
          <p className="text-[11px]" style={{ color: 'var(--text-faint)' }}>
            Se guarda automáticamente. El texto se incluirá en el Excel de cotización.
          </p>
        )}
      </div>

      {/* ── Historial de versiones ── */}
      {showVersiones && (
        <div className="card p-4 space-y-3">
          <h3 className="font-semibold text-sm" style={{ color: 'var(--text-primary)' }}>
            Historial de versiones
          </h3>
          {versiones.length === 0 ? (
            <p className="text-xs" style={{ color: 'var(--text-faint)' }}>No hay versiones guardadas aún.</p>
          ) : (
            <div className="space-y-2">
              {versiones.map(v => (
                <div key={v.version_num} className="rounded-xl border" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-100)' }}>
                  <div className="flex items-center justify-between px-4 py-2">
                    <div className="flex items-center gap-3">
                      <span className="font-mono font-bold text-xs text-brand-400">v{v.version_num}</span>
                      <span className="text-xs" style={{ color: 'var(--text-muted)' }}>
                        {new Date(v.created_at).toLocaleDateString('es-CL', { day: '2-digit', month: '2-digit', year: '2-digit', hour: '2-digit', minute: '2-digit' })}
                      </span>
                      {v.descripcion && <span className="text-xs" style={{ color: 'var(--text-faint)' }}>{v.descripcion}</span>}
                    </div>
                    <div className="flex items-center gap-2">
                      <button
                        onClick={() => handleVerVersion(v.version_num)}
                        className="btn-secondary text-xs py-1 px-2"
                        disabled={loadingDetalle === v.version_num}
                      >
                        {loadingDetalle === v.version_num ? <RefreshCw className="w-3 h-3 animate-spin" /> : (versionDetalle[v.version_num] ? 'Ocultar' : 'Ver detalle')}
                      </button>
                      <button
                        onClick={() => handleRestaurarVersion(v.version_num)}
                        className="btn-secondary text-xs py-1 px-2 text-amber-400 border-amber-500/30"
                        title={`Restaurar a v${v.version_num}`}
                      >
                        Restaurar v{v.version_num}
                      </button>
                    </div>
                  </div>
                  {versionDetalle[v.version_num] && (
                    <div className="border-t px-4 py-3 overflow-x-auto" style={{ borderColor: 'var(--border)' }}>
                      <table className="w-full text-xs" style={{ minWidth: '600px' }}>
                        <thead>
                          <tr style={{ borderBottom: '1px solid var(--border)' }}>
                            {['N° Parte', 'Descripción', 'QTY', 'P.Unit USD', 'Margen%', 'Pzo.Mín', 'Pzo.Máx'].map(h => (
                              <th key={h} className="text-left py-1.5 pr-4 text-[10px] font-semibold uppercase tracking-wide" style={{ color: 'var(--text-faint)' }}>{h}</th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {(versionDetalle[v.version_num]?.items || []).map((item: any, idx: number) => (
                            <tr key={idx} style={{ borderBottom: '1px solid var(--border)' }}>
                              <td className="py-1.5 pr-4 font-mono" style={{ color: 'var(--text-primary)' }}>{item.numero_parte}</td>
                              <td className="py-1.5 pr-4 max-w-[180px] truncate" style={{ color: 'var(--text-muted)' }}>{item.descripcion || '—'}</td>
                              <td className="py-1.5 pr-4 text-right" style={{ color: 'var(--text-primary)' }}>{item.cantidad}</td>
                              <td className="py-1.5 pr-4 text-right font-mono" style={{ color: 'var(--text-muted)' }}>{item.precio_unit_cotizacion != null ? `$${item.precio_unit_cotizacion.toFixed(2)}` : '—'}</td>
                              <td className="py-1.5 pr-4 text-right" style={{ color: 'var(--text-muted)' }}>{item.margen_pct != null ? `${(item.margen_pct * 100).toFixed(1)}%` : '—'}</td>
                              <td className="py-1.5 pr-4 text-right" style={{ color: 'var(--text-muted)' }}>{item.plazo_entrega_min != null ? `${item.plazo_entrega_min}d` : '—'}</td>
                              <td className="py-1.5 pr-4 text-right" style={{ color: 'var(--text-muted)' }}>{item.plazo_entrega_max != null ? `${item.plazo_entrega_max}d` : '—'}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* ── Guardar todos ── */}
      {dirtyItems.size > 0 && !faseCerrada && (
        <div className="flex justify-end">
          <button
            onClick={async () => {
              const ids = Array.from(dirtyItems)
              for (const itemId of ids) {
                const li = localItems.find(i => i.id === itemId)
                if (li) {
                  await cotizadorAPI.updateItem(cotId, itemId, {
                    margen_pct: li.margen_pct,
                    plazo: li.plazo,
                    plazo_entrega_min: (li as any).plazo_entrega_min ?? null,
                    plazo_entrega_max: (li as any).plazo_entrega_max ?? null,
                  })
                }
              }
              setDirtyItems(new Set())
              toast.success(`${ids.length} ítem(s) guardados`)
            }}
            className="btn-primary"
          >
            <Save className="w-4 h-4" />
            Guardar todos ({dirtyItems.size})
          </button>
        </div>
      )}
    </div>
  </>
  )
}