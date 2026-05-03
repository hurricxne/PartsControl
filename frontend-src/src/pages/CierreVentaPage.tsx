import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  ShoppingBag, Zap, ArrowRight, Info, CheckCircle2,
  Package, TruckIcon, BookOpen, Loader2, AlertCircle,
  Search, X,
} from 'lucide-react'
import toast from 'react-hot-toast'
import { cotizacionesAPI, cotizadorAPI, authAPI, comprasAPI } from '../services/api'

interface CotItem {
  id: number
  item_num: number
  numero_parte: string
  descripcion: string
  cantidad: number
  precio_venta_unitario_clp?: number
  total_venta_clp?: number
  costo_unitario_clp?: number
  costo_total_clp?: number
  plazo_entrega_min?: number
  plazo_entrega_max?: number
  plazo?: string        // string fallback "30" | "30-45 días hábiles"
}

// ── Días hábiles chilenos ──────────────────────────────────

/** Algoritmo de Meeus para Pascua */
function easterDate(year: number): Date {
  const a = year % 19
  const b = Math.floor(year / 100)
  const c = year % 100
  const d = Math.floor(b / 4)
  const e = b % 4
  const f = Math.floor((b + 8) / 25)
  const g = Math.floor((b - f + 1) / 3)
  const h = (19 * a + b - d - g + 15) % 30
  const i = Math.floor(c / 4)
  const k = c % 4
  const l = (32 + 2 * e + 2 * i - h - k) % 7
  const m = Math.floor((a + 11 * h + 22 * l) / 451)
  const month = Math.floor((h + l - 7 * m + 114) / 31) - 1
  const day = ((h + l - 7 * m + 114) % 31) + 1
  return new Date(year, month, day)
}

/** Devuelve la fecha del n-ésimo weekday (0=Dom…6=Sáb) del mes/año */
function nthWeekday(year: number, month: number, weekday: number, n: number): Date {
  const d = new Date(year, month - 1, 1)
  let count = 0
  while (true) {
    if (d.getDay() === weekday) { count++; if (count === n) return new Date(d) }
    d.setDate(d.getDate() + 1)
  }
}

/** Feriados legales Chile — incluye Jueves Santo, Viernes Santo y Pueblos Indígenas */
function isHoliday(date: Date): boolean {
  const mo = date.getMonth() + 1
  const dd = date.getDate()
  const y  = date.getFullYear()

  // Feriados fijos
  const fixed: [number, number][] = [
    [1,1],   // Año Nuevo
    [5,1],   // Día del Trabajo
    [5,21],  // Glorias Navales
    [6,29],  // San Pedro y San Pablo
    [7,16],  // Virgen del Carmen
    [8,15],  // Asunción
    [9,18],  // Fiestas Patrias
    [9,19],  // Día del Ejército
    [10,12], // Encuentro Dos Mundos
    [10,31], // Iglesias Evangélicas
    [11,1],  // Todos los Santos
    [12,8],  // Inmaculada Concepción
    [12,25], // Navidad
  ]
  if (fixed.some(([fm, fd]) => fm === mo && fd === dd)) return true

  // Pascua → Jueves Santo (−3) y Viernes Santo (−2)
  const easter = easterDate(y)
  const juevesSanto = new Date(easter); juevesSanto.setDate(easter.getDate() - 3)
  const viernesSanto = new Date(easter); viernesSanto.setDate(easter.getDate() - 2)
  if (dd === juevesSanto.getDate()  && mo === juevesSanto.getMonth()  + 1) return true
  if (dd === viernesSanto.getDate() && mo === viernesSanto.getMonth() + 1) return true

  // Día de los Pueblos Indígenas — 3er lunes de junio (desde 2021)
  if (y >= 2021) {
    const pueblos = nthWeekday(y, 6, 1, 3) // lunes=1, 3er ocurrencia, junio
    if (mo === 6 && dd === pueblos.getDate()) return true
  }

  return false
}

function addBusinessDays(start: Date, days: number): Date {
  const result = new Date(start)
  let count = 0
  while (count < days) {
    result.setDate(result.getDate() + 1)
    const dow = result.getDay()
    if (dow !== 0 && dow !== 6 && !isHoliday(result)) count++
  }
  return result
}

/** Formatea Date a YYYY-MM-DD usando fecha LOCAL (evita desfase UTC en Chile UTC-3/−4) */
function toDateInput(d: Date): string {
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

/** Formatea Date a DD/MM/YYYY para mostrar en tabla */
function fmtDate(d: Date): string {
  const day = String(d.getDate()).padStart(2, '0')
  const m   = String(d.getMonth() + 1).padStart(2, '0')
  return `${day}/${m}/${d.getFullYear()}`
}

interface Cot {
  id: number
  numero: string
  cliente: string
  fase_comercial: string
  total_items: number
}

interface Asesor {
  id: number
  nombre: string
  email: string
}

const TRIGGERS = [
  {
    key: 'compras',
    route: '/compras',
    color: 'bg-emerald-500/10 border-emerald-500/30 text-emerald-400',
    icon: Package,
    modulo: 'Abastecimiento',
    desc: 'Genera una alerta de compra pendiente con los ítems y N° de parte, para que el área de compras inicie la importación.',
  },
  {
    key: 'embarques',
    route: '/embarques',
    color: 'bg-amber-500/10 border-amber-500/30 text-amber-400',
    icon: TruckIcon,
    modulo: 'Logística',
    desc: 'Notifica al área de embarques que hay una nueva venta, iniciando el conteo del plazo prometido de entrega.',
  },
  {
    key: 'ventas-contab',
    route: '/ventas-contab',
    color: 'bg-blue-500/10 border-blue-400/30 text-blue-400',
    icon: BookOpen,
    modulo: 'Contabilidad',
    desc: 'Registra automáticamente la venta en el módulo de Ventas para su seguimiento contable y emisión de factura.',
  },
]

function fmtClp(v?: number) {
  if (!v) return '—'
  return `$${Math.round(v).toLocaleString('es-CL')}`
}

export default function CierreVentaPage() {
  const navigate = useNavigate()

  // Data
  const [cotizaciones, setCotizaciones] = useState<Cot[]>([])
  const [asesores, setAsesores] = useState<Asesor[]>([])
  const [items, setItems] = useState<CotItem[]>([])
  const [selectedCotId, setSelectedCotId] = useState<number | ''>('')
  const [selectedItems, setSelectedItems] = useState<Set<number>>(new Set())
  const [loadingItems, setLoadingItems] = useState(false)

  // Search
  const [search, setSearch] = useState('')
  const [searchOpen, setSearchOpen] = useState(false)

  // Form
  const [oc, setOc] = useState('')
  const [fechaOc, setFechaOc] = useState('')
  const [condPago, setCondPago] = useState('30 días contra factura')
  const [fechaEntrega, setFechaEntrega] = useState('')
  const [asesorId, setAsesorId] = useState<number | ''>('')
  const [cerrando, setCerrando] = useState(false)
  const [plazoDias, setPlazoDias] = useState<number | null>(null)
  const [plazoDefault, setPlazoDefault] = useState<number>(45)  // fallback config

  // Load cotizaciones (fase validada o enviada)
  useEffect(() => {
    cotizacionesAPI.list().then(({ data }) => {
      const elegibles = (data as any[]).filter(
        c => c.fase_comercial === 'ingresada' && c.estado === 'completado'
      )
      setCotizaciones(elegibles)
      if (elegibles.length > 0) setSelectedCotId(elegibles[0].id)
    }).catch(() => toast.error('Error al cargar cotizaciones'))

    authAPI.users().then(({ data }) => {
      setAsesores(data)
      if (data.length > 0) setAsesorId(data[0].id)
    }).catch(() => {})
  }, [])

  // Load items when cot changes
  useEffect(() => {
    if (!selectedCotId) { setItems([]); return }
    setLoadingItems(true)
    cotizadorAPI.get(selectedCotId as number).then(({ data }) => {
      // Plazo por ítem: preferir plazo_entrega_max, luego plazo_entrega_min,
      // luego parsear el string "plazo" ("30" | "30-45 días hábiles")
      const parsePlazoStr = (s?: string): number | undefined => {
        if (!s) return undefined
        const nums = s.replace(/[^0-9]/g, ' ').trim().split(/\s+/).map(Number).filter(n => n > 0)
        return nums.length > 0 ? Math.max(...nums) : undefined
      }

      const itemsRaw: CotItem[] = (data.items || []).map((it: any) => ({
        id: it.id,
        item_num: it.item_num,
        numero_parte: it.numero_parte,
        descripcion: it.descripcion || it.nombre_cat || '',
        cantidad: it.cantidad,
        precio_venta_unitario_clp: it.precio_venta_clp || it.precio_venta_unitario_clp || it.precio_unit_cotizacion,
        total_venta_clp: it.total_venta_clp || it.total_cotizacion,
        costo_unitario_clp: it.costo_unitario_clp || it.precio_unit_cotizacion,
        costo_total_clp: it.costo_total_clp || it.total_cotizacion,
        plazo_entrega_min: it.plazo_entrega_min ?? undefined,
        plazo_entrega_max: it.plazo_entrega_max
          ?? it.plazo_entrega_min
          ?? parsePlazoStr(it.plazo)
          ?? undefined,
        plazo: it.plazo,
      }))
      setItems(itemsRaw)
      setSelectedItems(new Set(itemsRaw.map(i => i.id)))

      // Fecha prometida = max(plazo_entrega_max por ítem) en días hábiles.
      // Si ningún ítem tiene plazo, usar plazo_max_default del config como fallback.
      const plazoConfig: number = data.config?.plazo_max_default ?? 45
      setPlazoDefault(plazoConfig)
      const plazos = itemsRaw
        .map(i => i.plazo_entrega_max)
        .filter((v): v is number => v != null && v > 0)
      const maxPlazo = plazos.length > 0 ? Math.max(...plazos) : plazoConfig
      setPlazoDias(maxPlazo)
      setFechaEntrega(toDateInput(addBusinessDays(new Date(), maxPlazo)))
    }).catch(() => toast.error('Error al cargar ítems'))
      .finally(() => setLoadingItems(false))
  }, [selectedCotId])

  const toggleItem = (id: number) => {
    setSelectedItems(prev => {
      const s = new Set(prev)
      s.has(id) ? s.delete(id) : s.add(id)
      return s
    })
  }

  const selectedCot = cotizaciones.find(c => c.id === selectedCotId)

  const filteredCots = cotizaciones.filter(c => {
    if (!search.trim()) return true
    const q = search.toLowerCase()
    return (
      c.numero.toLowerCase().includes(q) ||
      (c.cliente || '').toLowerCase().includes(q)
    )
  })
  const itemsSeleccionados = items.filter(i => selectedItems.has(i.id))
  const totalVenta = itemsSeleccionados.reduce((sum, i) =>
    sum + (i.total_venta_clp || i.costo_total_clp || 0), 0)

  const handleCerrar = async () => {
    if (!selectedCotId) { toast.error('Selecciona una cotización'); return }
    if (itemsSeleccionados.length === 0) { toast.error('Selecciona al menos un ítem'); return }

    setCerrando(true)
    try {
      // 1. Create OC Cliente (visible en panel de Compras)
      await comprasAPI.crearOcCliente({
        cotizacion_id: selectedCotId,
        numero_oc: oc || null,
        fecha_oc: fechaOc || null,
        cond_pago: condPago || null,
        fecha_entrega: fechaEntrega || null,
        asesor_id: asesorId || null,
      })
      // 2. Advance fase to validada
      await cotizacionesAPI.updateFase(selectedCotId as number, 'validada')
      toast.success(`Venta cerrada — COT-${selectedCot?.numero} aparece ahora en Compras`)
      setTimeout(() => navigate('/compras'), 1500)
    } catch (err: any) {
      toast.error(err.response?.data?.detail || 'Error al cerrar la venta')
    } finally {
      setCerrando(false)
    }
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold" style={{ color: 'var(--text-primary)' }}>Cierre de Venta</h1>
        <p className="text-sm mt-1" style={{ color: 'var(--text-muted)' }}>
          Formaliza la venta de una cotización aprobada por el cliente y activa los módulos de despacho y contabilidad
        </p>
      </div>

      {/* Info banner */}
      <div className="flex items-start gap-3 px-4 py-3 rounded-xl border"
        style={{ backgroundColor: 'rgba(59,130,246,0.06)', borderColor: 'rgba(59,130,246,0.2)', color: '#60a5fa' }}>
        <Info className="w-4 h-4 mt-0.5 shrink-0" />
        <p className="text-xs leading-relaxed">
          <strong>¿Qué hace este módulo?</strong> Toma una cotización en estado <em>Ingresada</em> (procesada por el sistema),
          registra la Orden de Compra del cliente y cierra la venta formalmente.
          Al confirmar, la cotización avanza al estado <em>Validada</em> y se activan tres triggers automáticos:
          se notifica a <strong>Abastecimiento</strong> para iniciar la compra,
          a <strong>Logística</strong> para preparar el despacho,
          y a <strong>Contabilidad</strong> para el registro de la venta.
        </p>
      </div>

      {cotizaciones.length === 0 ? (
        <div className="card p-10 text-center space-y-3">
          <AlertCircle className="w-10 h-10 mx-auto" style={{ color: 'var(--text-faint)' }} />
          <p className="font-semibold" style={{ color: 'var(--text-primary)' }}>Sin cotizaciones disponibles</p>
          <p className="text-sm" style={{ color: 'var(--text-muted)' }}>
            No hay cotizaciones formales en estado <em>Ingresada</em>.<br />
            Procesa una cotización en el Cotizador (ajusta precios y guarda) para que aparezca aquí.
          </p>
          <button onClick={() => navigate('/cotizaciones')} className="btn-secondary mx-auto">
            Ir a Cotizaciones
          </button>
        </div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* ── Form Panel ── */}
          <div className="lg:col-span-2 rounded-2xl border p-6 space-y-5"
            style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>

            {/* Cotización selector — buscador */}
            <div>
              <label className="block text-xs font-semibold uppercase tracking-wider mb-1.5"
                style={{ color: 'var(--text-faint)' }}>Cotización de origen</label>

              {/* Selected badge */}
              {selectedCot && !searchOpen && (
                <div className="flex items-center gap-2 mb-2 px-3 py-2 rounded-xl border"
                  style={{ backgroundColor: 'var(--surface-200)', borderColor: 'var(--border)' }}>
                  <div className="flex-1 min-w-0">
                    <span className="font-mono font-bold text-sm text-brand-400">COT-{selectedCot.numero}</span>
                    <span className="mx-2 text-xs" style={{ color: 'var(--text-faint)' }}>·</span>
                    <span className="text-sm" style={{ color: 'var(--text-primary)' }}>{selectedCot.cliente || 'Sin cliente'}</span>
                    <span className="ml-2 text-xs" style={{ color: 'var(--text-faint)' }}>{selectedCot.total_items} ítem(s)</span>
                  </div>
                  <button onClick={() => setSearchOpen(true)}
                    className="text-xs px-2 py-0.5 rounded hover:bg-[var(--surface-300)] transition-colors"
                    style={{ color: 'var(--text-faint)' }}>
                    Cambiar
                  </button>
                </div>
              )}

              {/* Search input */}
              {(!selectedCot || searchOpen) && (
                <div className="relative">
                  <div className="relative">
                    <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 pointer-events-none"
                      style={{ color: 'var(--text-faint)' }} />
                    <input
                      autoFocus={searchOpen}
                      className="input w-full pl-9 pr-8"
                      placeholder="Buscar por N° cotización o cliente…"
                      value={search}
                      onChange={e => setSearch(e.target.value)}
                    />
                    {search && (
                      <button onClick={() => setSearch('')}
                        className="absolute right-2.5 top-1/2 -translate-y-1/2 p-0.5 rounded hover:bg-[var(--surface-300)]">
                        <X className="w-3.5 h-3.5" style={{ color: 'var(--text-faint)' }} />
                      </button>
                    )}
                  </div>

                  {/* Results list */}
                  <div className="mt-1 rounded-xl border overflow-hidden"
                    style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-100)' }}>
                    {filteredCots.length === 0 ? (
                      <p className="text-xs text-center py-4" style={{ color: 'var(--text-faint)' }}>
                        Sin resultados para "{search}"
                      </p>
                    ) : (
                      filteredCots.map((c, idx) => (
                        <button key={c.id}
                          onClick={() => {
                            setSelectedCotId(c.id)
                            setSearch('')
                            setSearchOpen(false)
                          }}
                          className="w-full flex items-center gap-3 px-3 py-2.5 text-left hover:bg-[var(--surface-200)] transition-colors"
                          style={{ borderTop: idx > 0 ? '1px solid var(--border)' : undefined }}>
                          <span className="font-mono font-bold text-sm text-brand-400 shrink-0">
                            COT-{c.numero}
                          </span>
                          <span className="flex-1 text-sm truncate" style={{ color: 'var(--text-primary)' }}>
                            {c.cliente || <em style={{ color: 'var(--text-faint)' }}>Sin cliente</em>}
                          </span>
                          <span className="text-xs shrink-0" style={{ color: 'var(--text-faint)' }}>
                            {c.total_items} ítem(s)
                          </span>
                        </button>
                      ))
                    )}
                  </div>

                  {searchOpen && selectedCot && (
                    <button onClick={() => { setSearch(''); setSearchOpen(false) }}
                      className="mt-1 text-xs" style={{ color: 'var(--text-faint)' }}>
                      ← Volver a {selectedCot ? `COT-${selectedCot.numero}` : ''}
                    </button>
                  )}
                </div>
              )}
            </div>

            {/* Items */}
            <div>
              <label className="block text-xs font-semibold uppercase tracking-wider mb-1.5"
                style={{ color: 'var(--text-faint)' }}>
                Ítems a incluir en la venta
                {items.length > 0 && (
                  <span className="ml-2 normal-case font-normal" style={{ color: 'var(--text-muted)' }}>
                    {selectedItems.size} / {items.length} seleccionados
                  </span>
                )}
              </label>
              <div className="rounded-xl border overflow-hidden" style={{ borderColor: 'var(--border)' }}>
                {loadingItems ? (
                  <div className="flex items-center justify-center py-8">
                    <Loader2 className="w-5 h-5 animate-spin" style={{ color: 'var(--text-faint)' }} />
                  </div>
                ) : items.length === 0 ? (
                  <p className="text-xs text-center py-6" style={{ color: 'var(--text-faint)' }}>
                    No hay ítems para esta cotización
                  </p>
                ) : (
                  <table className="w-full text-sm">
                    <thead>
                      <tr style={{ backgroundColor: 'var(--surface-200)', borderBottom: '1px solid var(--border)' }}>
                        <th className="px-3 py-2 w-8">
                          <input type="checkbox"
                            checked={selectedItems.size === items.length}
                            onChange={e => setSelectedItems(e.target.checked ? new Set(items.map(i => i.id)) : new Set())}
                            className="w-3.5 h-3.5 rounded cursor-pointer accent-brand-500" />
                        </th>
                        {['N° Parte', 'Descripción', 'Qty', 'Precio Unit.', 'Total', 'Fecha Máx.'].map(h => (
                          <th key={h} className="px-3 py-2 text-left text-xs font-semibold uppercase tracking-wider"
                            style={{ color: 'var(--text-faint)' }}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {items.map((item, idx) => {
                        const checked = selectedItems.has(item.id)
                        const pu = item.precio_venta_unitario_clp || item.costo_unitario_clp
                        const tot = item.total_venta_clp || item.costo_total_clp
                        // Plazo efectivo: del ítem o fallback al default del config
                        const plazoEfectivo = item.plazo_entrega_max ?? plazoDefault
                        const esDefault = !item.plazo_entrega_max
                        const fechaMax = fmtDate(addBusinessDays(new Date(), plazoEfectivo))
                        return (
                          <tr key={item.id} onClick={() => toggleItem(item.id)}
                            className="cursor-pointer transition-colors hover:bg-[var(--surface-200)]"
                            style={{
                              borderBottom: idx < items.length - 1 ? '1px solid var(--border)' : undefined,
                              opacity: checked ? 1 : 0.45,
                            }}>
                            <td className="px-3 py-2.5">
                              <input type="checkbox" checked={checked} readOnly
                                className="w-3.5 h-3.5 rounded cursor-pointer accent-brand-500" />
                            </td>
                            <td className="px-3 py-2.5 font-mono text-xs text-brand-400 font-semibold whitespace-nowrap">
                              {item.numero_parte}
                            </td>
                            <td className="px-3 py-2.5 text-xs max-w-[180px] truncate" style={{ color: 'var(--text-primary)' }}>
                              {item.descripcion || '—'}
                            </td>
                            <td className="px-3 py-2.5 text-xs text-center" style={{ color: 'var(--text-muted)' }}>
                              {item.cantidad}
                            </td>
                            <td className="px-3 py-2.5 text-xs text-right font-mono" style={{ color: 'var(--text-primary)' }}>
                              {fmtClp(pu)}
                            </td>
                            <td className="px-3 py-2.5 text-xs text-right font-mono font-semibold" style={{ color: 'var(--text-primary)' }}>
                              {fmtClp(tot)}
                            </td>
                            <td className="px-3 py-2.5 text-xs text-center whitespace-nowrap"
                              style={{ color: esDefault ? 'var(--text-faint)' : 'var(--text-primary)' }}>
                              <span className="text-[10px] mr-1" style={{ color: 'var(--text-faint)' }}>
                                {plazoEfectivo}d{esDefault ? '*' : ''}
                              </span>
                              <span className="font-medium">{fechaMax}</span>
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                    {itemsSeleccionados.length > 0 && (
                      <tfoot>
                        <tr style={{ borderTop: '2px solid var(--border)', backgroundColor: 'var(--surface-200)' }}>
                          <td colSpan={5} className="px-3 py-2 text-xs font-semibold text-right" style={{ color: 'var(--text-muted)' }}>
                            TOTAL VENTA NETO
                          </td>
                          <td className="px-3 py-2 text-xs font-bold text-right font-mono" style={{ color: 'var(--empresa-primary, #1550d4)' }}>
                            {fmtClp(totalVenta)}
                          </td>
                          <td className="px-3 py-2">{/* Fecha Máx. — vacío en totales */}
                          </td>
                        </tr>
                      </tfoot>
                    )}
                  </table>
                )}
              </div>
            </div>

            {/* OC + Fechas */}
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div>
                <label className="block text-xs font-semibold uppercase tracking-wider mb-1.5" style={{ color: 'var(--text-faint)' }}>
                  N° OC del Cliente
                </label>
                <input className="input w-full" placeholder="Ej: OC-98712"
                  value={oc} onChange={e => setOc(e.target.value)} />
              </div>
              <div>
                <label className="block text-xs font-semibold uppercase tracking-wider mb-1.5" style={{ color: 'var(--text-faint)' }}>
                  Fecha OC
                </label>
                <input type="date" className="input w-full"
                  value={fechaOc} onChange={e => setFechaOc(e.target.value)} />
              </div>
              <div>
                <label className="block text-xs font-semibold uppercase tracking-wider mb-1.5" style={{ color: 'var(--text-faint)' }}>
                  Condiciones de Pago
                </label>
                <select className="input w-full" value={condPago} onChange={e => setCondPago(e.target.value)}>
                  {['Contado', '30 días contra factura', '60 días contra factura', '90 días contra factura'].map(o => (
                    <option key={o}>{o}</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="block text-xs font-semibold uppercase tracking-wider mb-1.5" style={{ color: 'var(--text-faint)' }}>
                  Fecha prometida de entrega
                  {plazoDias != null && (
                    <span className="ml-2 normal-case font-normal" style={{ color: 'var(--text-muted)' }}>
                      — {plazoDias} días hábiles
                    </span>
                  )}
                </label>
                <input type="date" className="input w-full"
                  value={fechaEntrega} onChange={e => setFechaEntrega(e.target.value)} />
                {plazoDias != null && (
                  <p className="text-xs mt-1" style={{ color: 'var(--text-faint)' }}>
                    {items.some(i => i.plazo_entrega_max)
                      ? `Calculado desde el mayor plazo de los ítems (${plazoDias}d háb.). Puedes ajustar.`
                      : `Usando plazo por defecto del cotizador (${plazoDias}d háb.). Puedes ajustar.`}
                  </p>
                )}
              </div>
              {asesores.length > 0 && (
                <div className="sm:col-span-2">
                  <label className="block text-xs font-semibold uppercase tracking-wider mb-1.5" style={{ color: 'var(--text-faint)' }}>
                    Asesor responsable
                  </label>
                  <select className="input w-full" value={asesorId} onChange={e => setAsesorId(Number(e.target.value))}>
                    {asesores.map(a => (
                      <option key={a.id} value={a.id}>{a.nombre}</option>
                    ))}
                  </select>
                </div>
              )}
            </div>
          </div>

          {/* ── Right Panel ── */}
          <div className="space-y-4">
            {/* Triggers */}
            <div className="rounded-2xl border p-5"
              style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
              <div className="flex items-center gap-2 mb-4">
                <Zap className="w-4 h-4 text-amber-400" />
                <span className="font-semibold text-sm" style={{ color: 'var(--text-primary)' }}>
                  3 Triggers al cerrar
                </span>
              </div>
              <div className="space-y-3">
                {TRIGGERS.map(t => (
                  <button key={t.key} onClick={() => navigate(t.route)}
                    className={`w-full rounded-xl border p-3 text-left transition-opacity hover:opacity-80 ${t.color}`}>
                    <div className="flex items-center justify-between mb-1">
                      <p className="text-xs font-bold flex items-center gap-1.5">
                        <t.icon className="w-3.5 h-3.5" />
                        {t.modulo}
                      </p>
                      <ArrowRight className="w-3 h-3 opacity-60" />
                    </div>
                    <p className="text-xs opacity-80 leading-relaxed">{t.desc}</p>
                  </button>
                ))}
              </div>
            </div>

            {/* Summary + confirm */}
            <div className="rounded-2xl border p-5 space-y-4"
              style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
              {selectedCot && (
                <div className="space-y-1.5 text-xs" style={{ color: 'var(--text-muted)' }}>
                  <div className="flex justify-between">
                    <span>Cotización</span>
                    <span className="font-mono font-semibold" style={{ color: 'var(--text-primary)' }}>
                      COT-{selectedCot.numero}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span>Cliente</span>
                    <span className="font-medium" style={{ color: 'var(--text-primary)' }}>
                      {selectedCot.cliente || '—'}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span>Ítems seleccionados</span>
                    <span className="font-semibold" style={{ color: 'var(--text-primary)' }}>
                      {selectedItems.size} / {items.length}
                    </span>
                  </div>
                  {totalVenta > 0 && (
                    <div className="flex justify-between pt-1 border-t" style={{ borderColor: 'var(--border)' }}>
                      <span className="font-semibold">Total Neto</span>
                      <span className="font-bold font-mono" style={{ color: 'var(--empresa-primary, #1550d4)' }}>
                        {fmtClp(totalVenta)}
                      </span>
                    </div>
                  )}
                </div>
              )}

              <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
                Al confirmar, se genera la OC Cliente, la cotización pasa a <strong>Validada</strong> y queda visible en el panel de Compras.
              </p>

              <button onClick={handleCerrar}
                disabled={cerrando || selectedItems.size === 0}
                className="btn-primary w-full flex items-center justify-center gap-2">
                {cerrando
                  ? <Loader2 className="w-4 h-4 animate-spin" />
                  : <CheckCircle2 className="w-4 h-4" />
                }
                {cerrando ? 'Cerrando venta…' : 'Confirmar y enviar a Compras'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
