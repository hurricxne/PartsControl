import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  ShoppingBag, Zap, ArrowRight, Info, CheckCircle2,
  Package, TruckIcon, BookOpen, Loader2, AlertCircle,
  Search, X, HandCoins, Download,
} from 'lucide-react'
import toast from 'react-hot-toast'
import { cotizacionesAPI, cotizadorAPI, authAPI, comprasAPI, contabilidadAPI } from '../services/api'

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
  // Ya venían en GET /cotizaciones/ y la pantalla no los leía: la referencia es el otro
  // dato con que Comercial identifica la cotización (obra / solicitud del cliente), y
  // created_at permite acotar por período en vez de recorrer la lista completa.
  referencia?: string | null
  created_at?: string | null
}

interface Asesor {
  id: number
  nombre: string
  email: string
}

// Venta ya registrada para la cotización (GET /contabilidad/ventas). Solo lo que
// necesita el aviso de "esta venta ya tiene adelanto" al re-cerrar.
interface VentaConAdelantos {
  cotizacion_id: number
  numero_oc: string | null
  total_con_iva_clp: number
  adelantos?: { n: number; por_aprobar: number; aprobado_clp: number; pendiente_aplicar_clp: number }
}

// Ventanas de tiempo del filtro (client-side sobre created_at de la lista).
const PERIODOS: { key: string; label: string; dias: number | null }[] = [
  { key: '', label: 'Todo', dias: null },
  { key: 'semana', label: 'Últimos 7 días', dias: 7 },
  { key: 'mes', label: 'Últimos 30 días', dias: 30 },
  { key: 'anio', label: 'Último año', dias: 365 },
]
const MS_DIA = 24 * 60 * 60 * 1000

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
  const [periodo, setPeriodo] = useState('')
  const [descargandoPdf, setDescargandoPdf] = useState(false)
  // Venta ya cerrada para la cotización elegida (y sus adelantos), para no informar dos
  // veces el mismo adelanto al re-cerrar. null = sin venta previa / aún no consultado.
  const [ventaPrevia, setVentaPrevia] = useState<VentaConAdelantos | null>(null)

  // Form
  const [oc, setOc] = useState('')
  const [fechaOc, setFechaOc] = useState('')
  const [condPago, setCondPago] = useState('30 días contra factura')
  const [fechaEntrega, setFechaEntrega] = useState('')
  const [asesorId, setAsesorId] = useState<number | ''>('')
  const [cerrando, setCerrando] = useState(false)
  const [plazoDias, setPlazoDias] = useState<number | null>(null)
  const [plazoDefault, setPlazoDefault] = useState<number>(45)  // fallback config
  // Adelanto del cliente: Comercial lo INFORMA aquí; Tesorería lo aprueba después
  const [conAdelanto, setConAdelanto] = useState(false)
  const [adelPct, setAdelPct] = useState('50')
  const [adelMonto, setAdelMonto] = useState('')
  const [adelObs, setAdelObs] = useState('')

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
    // El adelanto es POR VENTA: al cambiar de cotización se limpia lo digitado
    // (un % o monto de la venta anterior no debe filtrarse a la nueva).
    setConAdelanto(false)
    setAdelPct('50')
    setAdelMonto('')
    setAdelObs('')
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

  // Adelanto ya informado/aprobado para esta cotización: se consulta al elegirla, para
  // que Comercial NO informe un segundo adelanto sobre la misma venta al re-cerrarla
  // (el backend los suma y topea al total, así que el duplicado se descubre tarde).
  useEffect(() => {
    setVentaPrevia(null)
    if (!selectedCotId) return
    const numero = cotizaciones.find(c => c.id === selectedCotId)?.numero
    let vigente = true
    contabilidadAPI.listVentas(numero || undefined)
      .then(({ data }) => {
        if (!vigente) return
        const rows = (data ?? []) as VentaConAdelantos[]
        setVentaPrevia(rows.find(r => r.cotizacion_id === selectedCotId) ?? null)
      })
      .catch(() => { /* informativo: si no se puede leer, no se bloquea el cierre */ })
    return () => { vigente = false }
  }, [selectedCotId, cotizaciones])

  const selectedCot = cotizaciones.find(c => c.id === selectedCotId)

  // Filtro de la lista: N° de cotización, cliente o REFERENCIA (obra / solicitud del
  // cliente), y ventana de tiempo por fecha de creación.
  const desdeMs = (() => {
    const dias = PERIODOS.find(p => p.key === periodo)?.dias ?? null
    return dias == null ? null : Date.now() - dias * MS_DIA
  })()
  const filteredCots = cotizaciones.filter(c => {
    if (desdeMs != null && c.created_at) {
      const t = new Date(c.created_at).getTime()
      // Sin created_at legible NO se descarta: mejor mostrarla que esconderla.
      if (Number.isFinite(t) && t < desdeMs) return false
    }
    if (!search.trim()) return true
    const q = search.toLowerCase()
    return (
      c.numero.toLowerCase().includes(q) ||
      (c.cliente || '').toLowerCase().includes(q) ||
      (c.referencia || '').toLowerCase().includes(q)
    )
  })
  const itemsSeleccionados = items.filter(i => selectedItems.has(i.id))
  const totalVenta = itemsSeleccionados.reduce((sum, i) =>
    sum + (i.total_venta_clp || i.costo_total_clp || 0), 0)
  // Base del % del adelanto: TODOS los ítems de la cotización (misma base que el
  // backend valida en el tope de adelantos, que no mira la selección), en bruto c/IVA.
  const totalBrutoCotizacion = items.reduce((sum, i) =>
    sum + (i.total_venta_clp || i.costo_total_clp || 0), 0) * 1.19
  const montoAdel = Number(adelMonto) || 0
  const adelantoExcede = totalBrutoCotizacion > 0 && montoAdel > totalBrutoCotizacion
  // Espejo inverso: % digitado → pesos. Misma fórmula que informar_adelanto en el
  // backend (total bruto × pct / 100, redondeado a peso): lo que se ve es lo que
  // quedará informado. Solo con % válido (1-100) y sin monto manual digitado.
  const pctNum = Number(adelPct)
  const montoDesdePct =
    !adelMonto && totalBrutoCotizacion > 0 && pctNum > 0 && pctNum <= 100
      ? Math.round(totalBrutoCotizacion * pctNum / 100)
      : null

  /** Normaliza el error del backend a texto legible (un 422 de FastAPI trae detail
   *  como array de objetos; sin esto el toast muestra [object Object]). */
  const msgError = (err: any, fallback: string): string => {
    const d = err?.response?.data?.detail
    if (Array.isArray(d)) return d.map((x: any) => x?.msg || JSON.stringify(x)).join('; ')
    if (typeof d === 'string') return d
    return fallback
  }

  /** Descarga el PDF formal de la cotización (el mismo que se le manda al cliente):
   *  quien cierra la venta necesita comparar contra el papel que el cliente aprobó,
   *  sin salir de la pantalla ni volver al Cotizador. */
  const handleDownloadPdf = async () => {
    if (!selectedCotId) return
    setDescargandoPdf(true)
    try {
      const resp = await cotizadorAPI.downloadPdf(selectedCotId as number)
      const blob = new Blob([resp.data], { type: 'application/pdf' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `CotizacionCliente-${selectedCot?.numero ?? selectedCotId}.pdf`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (err) {
      toast.error(msgError(err, 'No se pudo descargar el PDF de la cotización'))
    } finally { setDescargandoPdf(false) }
  }

  const handleCerrar = async () => {
    if (!selectedCotId) { toast.error('Selecciona una cotización'); return }
    if (itemsSeleccionados.length === 0) { toast.error('Selecciona al menos un ítem'); return }
    if (!oc.trim()) { toast.error('El N° OC del cliente es obligatorio'); return }
    // El adelanto se informa DESPUÉS de cerrar la venta: validarlo aquí evita
    // cerrar y recién ahí chocar con un 422 por un monto/porcentaje inválido.
    if (conAdelanto) {
      if (adelMonto && !(Number(adelMonto) > 0)) { toast.error('El monto del adelanto debe ser mayor a 0'); return }
      if (!adelMonto && !(Number(adelPct) > 0 && Number(adelPct) <= 100)) { toast.error('El % de adelanto debe estar entre 1 y 100'); return }
    }

    setCerrando(true)
    try {
      // 1. Create OC Cliente (visible en panel de Compras)
      await comprasAPI.crearOcCliente({
        cotizacion_id: selectedCotId,
        numero_oc: oc.trim(),
        fecha_oc: fechaOc || null,
        cond_pago: condPago || null,
        fecha_entrega: fechaEntrega || null,
        asesor_id: asesorId || null,
      })
      // 2. Advance fase to validada
      await cotizacionesAPI.updateFase(selectedCotId as number, 'validada')
      // 3. Adelanto informado por Comercial → cola de aprobación de Tesorería.
      //    Si falla, la venta YA quedó cerrada: se avisa cómo informarlo después.
      if (conAdelanto) {
        try {
          await contabilidadAPI.informarAdelanto({
            cotizacion_id: selectedCotId as number,
            pct: adelMonto ? undefined : (Number(adelPct) || undefined),
            monto_esperado: adelMonto ? Number(adelMonto) : undefined,
            observaciones: adelObs || undefined,
          })
          toast.success('Adelanto informado — queda esperando aprobación de Tesorería')
        } catch (err: any) {
          toast.error(msgError(err, 'No se pudo informar el adelanto')
            + ' — la venta quedó cerrada; infórmalo desde Contabilidad → Ventas', { duration: 8000 })
        }
      }
      toast.success(`Venta cerrada — COT-${selectedCot?.numero} aparece ahora en Compras`)
      // El botón queda deshabilitado hasta navegar: re-habilitarlo aquí abría una
      // ventana de 1,5 s donde un segundo clic crearía una OC-Cliente duplicada.
      setTimeout(() => navigate('/compras'), 1500)
    } catch (err: any) {
      toast.error(msgError(err, 'Error al cerrar la venta'))
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
                    {selectedCot.referencia && (
                      <span className="ml-2 text-xs" style={{ color: 'var(--text-faint)' }}>· ref. {selectedCot.referencia}</span>
                    )}
                  </div>
                  {/* PDF de la cotización: el papel que el cliente aprobó, para contrastarlo
                      antes de cerrar (el mismo documento del Cotizador). */}
                  <button onClick={handleDownloadPdf} disabled={descargandoPdf}
                    title="Descargar el PDF de la cotización (el que se le envió al cliente)"
                    className="text-xs px-2 py-0.5 rounded hover:bg-[var(--surface-300)] transition-colors inline-flex items-center gap-1 disabled:opacity-50"
                    style={{ color: 'var(--text-muted)' }}>
                    {descargandoPdf ? <Loader2 className="w-3 h-3 animate-spin" /> : <Download className="w-3 h-3" />}
                    PDF
                  </button>
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
                      placeholder="Buscar por N° cotización, cliente o referencia…"
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

                  {/* Ventana de tiempo: con historia, la lista de elegibles se vuelve un muro
                      y hasta ahora solo se podía recorrer a mano. */}
                  <div className="flex items-center gap-1.5 flex-wrap mt-2">
                    {PERIODOS.map(p => (
                      <button key={p.key} onClick={() => setPeriodo(p.key)}
                        className={`px-2.5 py-1 rounded-full text-[11px] font-medium border transition-all ${periodo === p.key ? 'border-brand-500 bg-brand-500/10 text-brand-400' : 'border-transparent'}`}
                        style={periodo !== p.key ? { backgroundColor: 'var(--surface-200)', borderColor: 'var(--border)', color: 'var(--text-muted)' } : {}}>
                        {p.label}
                      </button>
                    ))}
                    <span className="text-[11px] ml-auto" style={{ color: 'var(--text-faint)' }}>
                      {filteredCots.length} de {cotizaciones.length} elegible(s)
                    </span>
                  </div>

                  {/* Results list */}
                  <div className="mt-1 rounded-xl border overflow-hidden"
                    style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-100)' }}>
                    {filteredCots.length === 0 ? (
                      <p className="text-xs text-center py-4" style={{ color: 'var(--text-faint)' }}>
                        {search ? `Sin resultados para "${search}"` : 'Sin cotizaciones'}
                        {periodo ? ` en ${(PERIODOS.find(p => p.key === periodo)?.label || '').toLowerCase()}` : ''}
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
                            {c.referencia && (
                              <span className="ml-2 text-xs" style={{ color: 'var(--text-faint)' }}>ref. {c.referencia}</span>
                            )}
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
                  N° OC del Cliente <span className="text-red-400">*</span>
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

              {/* Adelanto del cliente (lo aprueba Tesorería después) */}
              <div className="sm:col-span-2 rounded-xl border p-3 space-y-3"
                style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
                {/* Adelanto YA vigente en esta venta: al re-cerrar, informar otro lo SUMA
                    (el backend topea la suma al total de la venta y el duplicado aparece
                    recién en la cola de Tesorería). */}
                {ventaPrevia && (ventaPrevia.adelantos?.n ?? 0) > 0 && (
                  <div className="flex items-start gap-2 px-3 py-2 rounded-lg border"
                    style={{ backgroundColor: 'rgba(245,158,11,0.08)', borderColor: 'rgba(245,158,11,0.3)' }}>
                    <AlertCircle className="w-4 h-4 mt-0.5 shrink-0 text-amber-400" />
                    <p className="text-xs leading-relaxed text-amber-400">
                      Esta venta ya tiene <b>{ventaPrevia.adelantos!.n} adelanto{ventaPrevia.adelantos!.n !== 1 ? 's' : ''}</b> registrado
                      {ventaPrevia.numero_oc ? <> en la <b>OC {ventaPrevia.numero_oc}</b></> : null}
                      {ventaPrevia.adelantos!.aprobado_clp > 0 && <> · aprobado <b>{fmtClp(ventaPrevia.adelantos!.aprobado_clp)}</b></>}
                      {ventaPrevia.adelantos!.por_aprobar > 0 && <> · {ventaPrevia.adelantos!.por_aprobar} esperando aprobación de Tesorería</>}
                      . Si informas otro se suma al mismo total de la venta — revísalos en Contabilidad → Ventas.
                    </p>
                  </div>
                )}
                {ventaPrevia && (ventaPrevia.adelantos?.n ?? 0) === 0 && ventaPrevia.numero_oc && (
                  <p className="text-xs" style={{ color: 'var(--text-faint)' }}>
                    Esta cotización ya tiene venta registrada (OC {ventaPrevia.numero_oc} · {fmtClp(ventaPrevia.total_con_iva_clp)} c/IVA), sin adelantos informados.
                  </p>
                )}
                <label className="flex items-center gap-2 text-sm font-medium cursor-pointer" style={{ color: 'var(--text-primary)' }}>
                  <input type="checkbox" checked={conAdelanto} onChange={e => setConAdelanto(e.target.checked)} />
                  <HandCoins className="w-4 h-4 text-emerald-500" />
                  Esta venta tiene adelanto del cliente
                </label>
                {conAdelanto && (
                  <>
                    <div className="grid grid-cols-2 gap-3">
                      <div>
                        <label className="block text-xs font-semibold uppercase tracking-wider mb-1.5" style={{ color: 'var(--text-faint)' }}>
                          % del total
                        </label>
                        {/* Con monto CLP digitado, el % se DERIVA en vivo sobre el total de
                            la cotización completa (misma base que el tope del backend; la
                            proporción es idéntica medida en neto o en bruto). */}
                        <input type="number" min={1} max={100} className="input w-full"
                          value={adelMonto
                            ? (totalBrutoCotizacion > 0
                                ? ((montoAdel / totalBrutoCotizacion) * 100).toFixed(1) : '')
                            : adelPct}
                          onChange={e => setAdelPct(e.target.value)} disabled={!!adelMonto} />
                        {!!adelMonto && totalBrutoCotizacion > 0 && (
                          <p className={`text-[11px] mt-1 ${adelantoExcede ? 'text-red-400' : ''}`}
                            style={adelantoExcede ? undefined : { color: 'var(--text-faint)' }}>
                            {adelantoExcede
                              ? `El monto supera el total de la venta (${fmtClp(Math.round(totalBrutoCotizacion))} c/IVA)`
                              : `del total c/IVA ${fmtClp(Math.round(totalBrutoCotizacion))}`}
                          </p>
                        )}
                      </div>
                      <div>
                        <label className="block text-xs font-semibold uppercase tracking-wider mb-1.5" style={{ color: 'var(--text-faint)' }}>
                          o monto exacto (CLP, con IVA)
                        </label>
                        <input type="number" className="input w-full" value={adelMonto}
                          onChange={e => setAdelMonto(e.target.value)} placeholder="opcional" />
                        {/* Espejo del %: al digitar el porcentaje, aquí se ve cuántos pesos
                            son — la MISMA fórmula con que el backend registra el adelanto
                            (total bruto de la cotización × % / 100, redondeado a peso). */}
                        {!adelMonto && montoDesdePct !== null && (
                          <p className="text-[11px] mt-1" style={{ color: 'var(--text-faint)' }}>
                            ≈ <b style={{ color: 'var(--text-primary)' }}>{fmtClp(montoDesdePct)}</b> c/IVA
                            {' '}({fmtClp(Math.round(montoDesdePct / 1.19))} neto)
                          </p>
                        )}
                      </div>
                    </div>
                    <input className="input w-full" value={adelObs} onChange={e => setAdelObs(e.target.value)}
                      placeholder="Observaciones (ej: cliente transfiere esta semana)" />
                    <p className="text-xs" style={{ color: 'var(--text-faint)' }}>
                      Queda <b>informado</b>: Tesorería confirma la plata recibida en su pestaña Adelantos, y al
                      emitir la(s) factura(s) el sistema lo aplica solo como pago.
                    </p>
                  </>
                )}
              </div>
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
                disabled={cerrando || selectedItems.size === 0 || !oc.trim()}
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
