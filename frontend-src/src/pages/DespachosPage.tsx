import { useState, useMemo, useEffect, useRef } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import api, { despachosAPI, cotizacionesAPI, cotizadorAPI, comprasAPI, wasabilAPI, abrirDocumento } from '../services/api'
import type { DespachoDetalle, DespachoItemDetalle, FirmaItemPayload } from '../services/api'
import {
  filtrarItems, contarSeleccion, contarMarcadasOcultas, contarLineasQueViajan,
  fmtQty as fmtQtyPicking,
  armarResumenBultos, colapsar, esDespachable,
} from '../picking/picking'
import type { PickingMatch } from '../picking/picking'
import {
  Truck, Package, CheckCircle2, AlertCircle, Search, X,
  ChevronRight, ChevronDown, Plus, Trash2, Send,
  FileSpreadsheet, FileText, FileDown, Loader2,
  Clock, AlertTriangle, Upload, Pencil, Eye, Receipt, Zap,
} from 'lucide-react'
import toast from 'react-hot-toast'
import { fmtDate, fmtFechaServidor } from '../utils/format'

/**
 * HOY en Chile, 'YYYY-MM-DD'. ÚNICA fuente del día en esta pantalla.
 *
 * POR QUÉ NO `hoyLocal()` (el día del NAVEGADOR, que es lo que había): el
 * negocio, el servidor y el SII viven en horario de Chile, no en el del PC del
 * operador. Con un notebook de zona corrida —o con el turno de noche conectado
 * desde otra zona— la «Fecha del reparto» del mail al transportista y la fecha
 * de firma de la guía salían con el día equivocado.
 * `en-CA` es el locale que imprime justamente 'YYYY-MM-DD', y `timeZone` deja
 * que el runtime resuelva verano/invierno (mismo criterio que el backend con
 * zoneinfo). Antes esta pantalla tenía DOS formas distintas de «hoy»: esta, solo
 * en el tope del selector de fecha_guia, y hoyLocal() en todo lo demás.
 *
 * NO sirve para decidir si un despacho se cerró hoy: ese veredicto lo da el
 * servidor (`cerrado_hoy`), el único que conoce la hora real del cierre.
 */
const hoyEnChile = (): string =>
  new Date().toLocaleDateString('en-CA', { timeZone: 'America/Santiago' })

interface DocumentosOc {
  excel_oc: boolean
  cot_formal_excel: boolean
  cot_pdf: boolean
  cot_original_excel: boolean
}

interface DocValue {
  value: string
  is_file: boolean
  filename: string | null
}

interface EmbarqueDocs {
  awb: DocValue | null
  factura_comercial: DocValue | null
  packing_list: DocValue | null
  certificado_origen: DocValue | null
  doc_adicional: DocValue | null
}

interface EmbarqueResumen {
  id: number
  numero: string
  estado?: string
  forwarder?: string
  fecha_despacho?: string
  fecha_llegada_est?: string | null
  items_de_esta_oc: number
  documentos: EmbarqueDocs
}

async function downloadBlob(
  fetcher: () => Promise<any>,
  filename: string,
  mime: string,
) {
  try {
    const resp = await fetcher()
    const blob = new Blob([resp.data], { type: mime })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (e: any) {
    toast.error(e?.response?.data?.detail || 'Error al descargar documento')
  }
}

type Tab = 'listas' | 'en_curso' | 'historial'

interface ProgresoEstados {
  pendiente: number
  en_compras: number
  en_transito: number
  en_bodega: number
  despachado: number
  reclamo: number
}

interface OcCard {
  id: number
  cotizacion_id?: number
  /** NULLABLE en la BD (OCs legacy sin N°: models.py `Column(String(100))` sin
   *  nullable=False). Declararlo `string` hacía que TS bendijera interpolaciones
   *  que imprimían literalmente «OC null» en el texto que se le copia al
   *  transportista: la identidad se resuelve en cada call site con `#id`. */
  numero_oc: string | null
  numero_cotizacion?: string
  cliente: string
  rut_cliente?: string
  fecha_oc?: string
  fecha_entrega?: string
  cond_pago?: string
  direccion?: string
  contacto?: string
  telefono?: string
  email?: string
  total_items: number
  items_en_bodega: number
  items_despachados: number
  items_no_disponibles: number
  progreso_pct: number
  progreso_estados?: ProgresoEstados
  dias_restantes_oc?: number | null
  dias_restantes_critico?: number | null
  estado: 'listo' | 'parcial' | 'completado' | 'pendiente'
  documentos?: DocumentosOc
  /** Solo tab «listas» (contrato backend 2026-08-26): n° de ítems con disponible
   *  REAL (recibido − tomado por despachos abiertos) y Σ de esas unidades (float,
   *  puede ser 0). Ausentes en otras tabs y con backend viejo. */
  items_despachables?: number
  unidades_despachables?: number
  /** Solo tab «listas» y SOLO cuando unidades_despachables es 0 (contrato
   *  backend 2026-08-27): POR QUÉ no hay cupo. El motivo lo deriva el backend,
   *  donde vive la fórmula del cupo — la pantalla no lo re-deduce.
   *  Ausente/null = backend viejo u otra tab → texto neutro, sin culpar a nadie. */
  motivo_sin_cupo?: 'en_preparacion' | 'despachado' | 'sin_stock' | null
}

/** Insignia gris «0 un. por despachar»: qué dice y qué hacer, según el MOTIVO.
 *  Antes había UN solo texto («En despachos abiertos… ciérralos o anúlalos») que
 *  mentía en dos de los tres casos: con el cupo consumido por un despacho ya
 *  CERRADO mandaba a anular un documento inanulable (y probablemente ya
 *  facturado), y con una recepción parcial en reclamo culpaba a despachos que no
 *  existen. */
const MOTIVO_SIN_CUPO: Record<string, { label: string; title: string }> = {
  en_preparacion: {
    label: 'En despachos abiertos',
    // Cerrar NO libera cupo (lo consume el despacho); liberar es SOLO anular.
    title: 'Todo lo recibido ya está tomado por despachos en preparación. Ciérralos para completar el despacho, o anúlalos para liberar cupo.',
  },
  despachado: {
    label: 'Ya despachado',
    title: 'Todo lo recibido ya se despachó y esos despachos están cerrados (no se anulan). Si el cliente necesita más, la reposición se pide en una cotización nueva.',
  },
  sin_stock: {
    label: 'Falta recepción en bodega',
    title: 'Lo vendido todavía no llegó a bodega (o está en reclamo al proveedor). No hay nada que anular ni que cerrar: hay que esperar la recepción.',
  },
}

/** Sin motivo (backend viejo): se dice el HECHO y nada más — una instrucción
 *  inventada es peor que ninguna. */
const MOTIVO_SIN_CUPO_NEUTRO = {
  label: 'Sin cupo disponible',
  title: 'No hay unidades despachables ahora mismo en esta OC.',
}

/** GET /despachos/counts. Los 2 campos nuevos son ADITIVOS (contrato backend
 *  2026-08-26): opcionales para no romper contra un backend viejo. */
interface DespachosCounts {
  ocs_listas: number
  items_listos: number
  items_despachados: number
  /** OCs con al menos 1 unidad despachable AHORA (no tomada por despachos abiertos). */
  ocs_con_disponible?: number
  /** Σ unidades despachables ahora (float). */
  unidades_despachables?: number
}

// ── Contrato GET /despachos/listo-para-despachar (panel de los KPIs) ──────────
interface ListoResumenItem {
  numero_parte: string
  descripcion: string
  qty_disponible: number
  cantidad: number
}

interface ListoResumenGrupo {
  oc_cliente_id: number
  /** Nullable en la BD (OCs legacy sin N°): el header y la búsqueda del «Ir a la
   *  OC» caen al id — asumirlo string tumbaba la página con setSearch(null). */
  numero_oc: string | null
  cliente: string
  dias_restantes_critico: number | null
  fecha_entrega: string | null
  total_unidades: number
  items: ListoResumenItem[]
}

interface ListoResumenResponse {
  hoy: string
  /** Solo OCs con disponible > 0, YA ordenadas por urgencia en el backend. */
  grupos: ListoResumenGrupo[]
}

interface ItemRow {
  id: number
  numero_parte: string
  descripcion: string
  marca: string
  cantidad: number
  qty_despachada: number
  qty_disponible: number
  estado_item: string
  en_reclamo: boolean
  plazo_entrega_max?: number | null
  plazo_entrega_min?: number | null
  deadline_item?: string | null
  dias_restantes?: number | null
}

interface DespachoRow {
  id: number
  numero_despacho: string
  numero_guia?: string
  /** Fecha de EMISIÓN de la guía ante el SII (YYYY-MM-DD). Sólo guía en papel: es la
   *  fecha que la factura cita en su referencia 52. Distinta de fecha_despacho (cierre)
   *  y de fecha_firma (recepción del cliente). */
  fecha_guia?: string | null
  transportista?: string
  estado: string
  numero_expedicion?: string
  guia_firmada?: boolean
  fecha_firma?: string
  guia_firmada_archivo?: string
  fecha_creacion?: string
  fecha_despacho?: string
  items_count: number
  /** Σ unidades declaradas como faltante de entrega al firmar la guía (0 o ausente
   *  = llegó todo). Lo manda el listado de la OC para pintar el badge ámbar. */
  faltante_total?: number
  /** Rótulo de la caja en que viaja este despacho (picking & packing). Texto libre
   *  ≤50; ""/null = sin rotular (sin chip). Contrato backend 2026-08-25. */
  bulto_numero?: string | null
  /** ¿El despacho se cerró HOY, en día de Chile? Lo calcula el SERVIDOR (contrato
   *  backend 2026-08-27, ADITIVO). La pantalla NO puede deducirlo: `fecha_despacho`
   *  viaja sin zona y el server corre en UTC, así que compararla contra el reloj
   *  del navegador fecha «mañana» todo lo cerrado después de las ~21:00 de Chile.
   *  Ausente = backend viejo → DESCONOCIDO (nunca se asume que ya viajó). */
  cerrado_hoy?: boolean
}

/** GET /despachos/{id} más el mismo campo ADITIVO `cerrado_hoy` del contrato
 *  2026-08-27. Se declara ACÁ y no en services/api.ts porque el reparto de bultos
 *  es su único consumidor; opcional para que un backend viejo siga compilando y
 *  corriendo (el `??` del llamador lo degrada a «desconocido»). */
type DespachoDetalleConCerrado = DespachoDetalle & { cerrado_hoy?: boolean }

interface OcDetail extends OcCard {
  /** Tope de ítems por documento de la vía «SII gratuito» (10). Lo emite el
   *  backend desde wasabil_dte.service para que la pantalla no lo hardcodee.
   *  Ausente = backend viejo: sin aviso (degradación silenciosa, no un 0). */
  max_lineas_sii_gratuito?: number
  items: ItemRow[]
  despachos: DespachoRow[]
  embarques?: EmbarqueResumen[]
}

const estadoLabel: Record<string, { label: string; color: string }> = {
  listo: { label: 'Listo para despacho', color: 'bg-emerald-500/15 text-emerald-500 dark:text-emerald-400' },
  parcial: { label: 'Parcial', color: 'bg-amber-500/15 text-amber-600 dark:text-amber-400' },
  completado: { label: 'Completado', color: 'bg-blue-500/15 text-blue-600 dark:text-blue-400' },
  pendiente: { label: 'Pendiente bodega', color: 'bg-slate-500/15 text-slate-500' },
}

// ── Buscador de operador (contrato común, 2026-08-05) ─────────────────────────

interface MatchMotivo {
  campo: string
  valor: string
}

/** Sobre del listado /despachos/oc-clientes (antes era un array pelado). */
interface OcListResponse {
  items: (OcCard & { match?: MatchMotivo[] })[]
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

// Espejo (solo para RESALTAR en cliente) de los prefijos que el backend descarta:
// la UI imprime "COT-2026-0001" pero la base guarda "2026-0001".
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

/** Campo de una línea bajo el filtro de picking. Acierto literal → reusa
 *  Resaltado (subraya el fragmento). Acierto por pasada COLAPSADA → el fragmento
 *  literal no existe en el texto (7T1997 vs 7T-1997), así que se resalta el
 *  campo COMPLETO (regla de la spec de buscadores 2026-08-05). */
function CampoFiltrado({
  texto,
  query,
  colapsado,
}: {
  texto: string
  query: string
  colapsado: boolean
}) {
  const q = query.trim()
  if (!q) return <>{texto}</>
  if (colapsado) {
    return (
      <mark
        className="rounded px-0.5"
        style={{ backgroundColor: 'rgba(245, 158, 11, 0.35)', color: 'inherit' }}
      >
        {texto}
      </mark>
    )
  }
  return <Resaltado texto={texto} tokens={[q]} />
}

const MATCH_LABELS: Record<string, string> = {
  numero_parte: 'n° parte',
  numero_parte_colapsado: 'n° parte (sin guiones)',
  repuesto: 'repuesto',
  marca: 'marca',
  cotizacion: 'cotización',
  cliente: 'cliente',
  rut: 'RUT',
  oc_cliente: 'OC cliente',
  embarque: 'embarque',
  awb: 'AWB',
  guia_nacional: 'guía prov.',
  despacho: 'N° despacho',
  guia: 'guía',
  expedicion: 'N° expedición',
  transportista: 'transportista',
}

// Campos que la card YA muestra: la insignia lleva solo la etiqueta. Si el campo
// NO es columna visible, la insignia lleva el VALOR ("embarque EMB-2026-0007").
const CAMPOS_VISIBLES_CARD = new Set(['cliente', 'cotizacion', 'oc_cliente'])

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
          className="text-[10px] px-1.5 py-0.5 rounded-full font-semibold bg-amber-500/15 text-amber-600 dark:text-amber-400"
          title={`Coincidió por ${MATCH_LABELS[m.campo] ?? m.campo}: ${m.valor}`}
        >
          {MATCH_LABELS[m.campo] ?? m.campo}
          {CAMPOS_VISIBLES_CARD.has(m.campo) ? '' : ` ${m.valor}`}
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

export default function DespachosPage() {
  // El estado del buscador vive en la URL (?q=&tab=), no en localStorage: la app
  // se recarga sola bajo los pies del operador y "andá a buscar esto" pasa a ser
  // un enlace. Un término que sobrevive del turno de ayer sería peor que nada.
  const [searchParams, setSearchParams] = useSearchParams()
  const tabParam = searchParams.get('tab')
  const [tab, setTabState] = useState<Tab>(
    tabParam === 'en_curso' || tabParam === 'historial' ? tabParam : 'listas'
  )
  const [search, setSearch] = useState(searchParams.get('q') ?? '')
  // Término EFECTIVO (debounce 350 ms): es el que entra a la queryKey. OJO: meter
  // `search` crudo en la queryKey re-ejecutaba el barrido completo por CADA tecla.
  const [searchQ, setSearchQ] = useState((searchParams.get('q') ?? '').trim())
  const [pageSize, setPageSize] = useState(PAGE_SIZE_INICIAL)
  const [expandedOc, setExpandedOc] = useState<number | null>(null)
  const [modalOc, setModalOc] = useState<OcDetail | null>(null)
  // Despacho completo (no solo el id): el modal de firma necesita saber si ya
  // está firmado (re-firma con faltante) y la fecha de firma previa.
  const [firmarDespacho, setFirmarDespacho] = useState<DespachoRow | null>(null)
  const [firmarDteFolio, setFirmarDteFolio] = useState<string | null>(null)
  const [editDespacho, setEditDespacho] = useState<DespachoRow | null>(null)
  const [editDespachoDteFolio, setEditDespachoDteFolio] = useState<string | null>(null)
  const [emitirGuia, setEmitirGuia] = useState<DespachoRow | null>(null)
  const qc = useQueryClient()

  // Debounce 350 ms del término (Enter lo saltea, ver onKeyDown de la caja).
  useEffect(() => {
    const t = setTimeout(() => setSearchQ(search.trim()), DEBOUNCE_BUSQUEDA_MS)
    return () => clearTimeout(t)
  }, [search])

  // replaceState mientras se escribe: que Atrás no retroceda carácter por carácter.
  // Guardia anti-bucle: setSearchParams cambia de identidad en cada navegación
  // (react-router 6), así que solo se navega si la URL realmente difiere.
  useEffect(() => {
    if ((searchParams.get('q') ?? '') === search) return
    setSearchParams(
      prev => {
        const p = new URLSearchParams(prev)
        if (search) p.set('q', search)
        else p.delete('q')
        return p
      },
      { replace: true }
    )
  }, [search, searchParams, setSearchParams])

  // Mínimo 2 caracteres: por debajo no se filtra (ni acá ni en el servidor).
  const qEfectivo = searchQ.length >= MIN_CARACTERES_BUSQUEDA ? searchQ : ''
  const qTokens = useMemo(() => tokensDe(qEfectivo), [qEfectivo])

  // Al cambiar el término se vuelve a la primera "página" (Ver más reinicia).
  useEffect(() => {
    setPageSize(PAGE_SIZE_INICIAL)
  }, [qEfectivo])

  const setTab = (t: Tab) => {
    setTabState(t)
    setPageSize(PAGE_SIZE_INICIAL)
    // Cambiar de pestaña CANCELA el scroll perseguidor (irAOcDesdePanel re-arma
    // el suyo DESPUÉS de llamar acá): un pendiente que sobrevive al cambio de
    // contexto saltaría a una card vieja en cualquier render futuro.
    setScrollOcPendiente(null)
    // push (no replace) al cambiar de pestaña: Atrás vuelve a la pestaña anterior.
    setSearchParams(prev => {
      const p = new URLSearchParams(prev)
      if (t === 'listas') p.delete('tab')
      else p.set('tab', t)
      return p
    })
  }

  const { data: counts } = useQuery<DespachosCounts>({
    queryKey: ['despachos', 'counts'],
    queryFn: despachosAPI.getCounts,
    refetchInterval: 60000,
  })

  // Panel «Listo para despachar» (se abre desde los 2 primeros KPIs).
  const [showListoResumen, setShowListoResumen] = useState(false)
  // Scroll pendiente al volver del panel con «Ir a la OC»: la card puede no estar
  // pintada todavía (cambio de tab o búsqueda asíncrona), así que se persigue por
  // efecto hasta que aparezca en el listado en vez de un setTimeout a ciegas.
  const [scrollOcPendiente, setScrollOcPendiente] = useState<number | null>(null)

  // La guardia de secuencia contra respuestas fuera de orden la da React Query:
  // cada combinación tab+q+page_size es una queryKey distinta y solo se pinta la
  // del estado vigente (por eso el debounce va ANTES de la queryKey, no después).
  const { data: ocResp, isLoading, isFetching } = useQuery<OcListResponse>({
    queryKey: ['despachos', 'oc-clientes', tab, qEfectivo, pageSize],
    queryFn: () =>
      api
        .get('/despachos/oc-clientes', {
          params: { tab, q: qEfectivo || undefined, page_size: pageSize },
        })
        .then(r => r.data),
  })
  const ocs = ocResp?.items ?? []
  const totalOcs = ocResp?.total ?? 0

  // Tab «listas»: las cards con unidades_despachables === 0 (todo lo recibido ya
  // está tomado por despachos abiertos) van al FINAL — no son accionables AHORA
  // y taparían a las que sí lo son. Partición estable con filter (conserva el
  // orden de urgencia del backend dentro de cada mitad) en vez de sort: el
  // criterio de urgencia vive en el servidor, acá no se re-compara nada.
  // Comparación estricta con 0: campo AUSENTE (otras tabs / backend viejo) no
  // reordena nada.
  const ocsOrdenadas = useMemo(() => {
    if (tab !== 'listas') return ocs
    const sinDisponible = ocs.filter(o => o.unidades_despachables === 0)
    if (sinDisponible.length === 0) return ocs
    return [...ocs.filter(o => o.unidades_despachables !== 0), ...sinDisponible]
  }, [ocs, tab])

  // «Ir a la OC» desde el panel: cerrar, asegurar tab listas y expandir la card.
  // Si la card no está en la página actual del listado, se setea el buscador con
  // el N° de OC (el deep-link ?q= ya existe y el backend busca por oc_cliente).
  // numero_oc es NULLABLE en la BD (OCs legacy): sin el fallback al id,
  // setSearch(null) reventaba en search.trim() y tumbaba la página entera.
  const irAOcDesdePanel = (ocId: number, numeroOc: string | null) => {
    setShowListoResumen(false)
    if (tab !== 'listas') setTab('listas') // setTab limpia el pendiente; se re-arma abajo
    const enPagina = tab === 'listas' && ocs.some(o => o.id === ocId)
    if (!enPagina) {
      const termino = numeroOc || String(ocId)
      setSearch(termino)
      setSearchQ(termino) // saltea el debounce: el operador ya eligió
    }
    setExpandedOc(ocId)
    setScrollOcPendiente(ocId)
  }

  // El scroll se ejecuta recién cuando la card pedida existe en el DOM (puede
  // tardar: la búsqueda / el cambio de tab traen la lista del servidor). Si la
  // query ASIENTA (isFetching → false) y la card no vino, el pendiente se cancela:
  // jamás un perseguidor eterno esperando una card que no va a llegar. (Editar la
  // búsqueda a mano o cambiar de tab también lo cancelan, en sus handlers.)
  useEffect(() => {
    if (scrollOcPendiente === null) return
    const el = document.getElementById(`oc-card-${scrollOcPendiente}`)
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'start' })
      setScrollOcPendiente(null)
    } else if (!isFetching) {
      setScrollOcPendiente(null)
    }
  }, [scrollOcPendiente, ocs, isFetching])

  const { data: ocDetail } = useQuery({
    queryKey: ['despachos', 'oc-detail', expandedOc],
    queryFn: () => (expandedOc ? despachosAPI.getOcDetail(expandedOc) : null),
    enabled: expandedOc !== null,
  })

  // El modal de crear pinta `modalOc` CONGELADO al abrirse. Cuando el crear rebota
  // con 400 (otro usuario tomó el cupo), el modal invalida ['despachos'] y el
  // refetch de oc-detail trae el cupo REAL: este efecto se lo pasa al modal sin
  // cerrar/reabrir — antes el operador reintentaba a ciegas contra el disponible
  // viejo. Solo se sincroniza la MISMA OC (el detalle expandido podría ser otra),
  // y JAMÁS se toca selectedItems del modal: lo marcado se conserva y las
  // cantidades se re-validan al enviar.
  useEffect(() => {
    if (!modalOc || !ocDetail) return
    const fresco = ocDetail as OcDetail
    if (fresco.id !== modalOc.id || fresco === modalOc) return
    setModalOc(fresco)
  }, [ocDetail, modalOc])

  const cerrarMut = useMutation({
    mutationFn: (despachoId: number) => despachosAPI.cerrar(despachoId),
    onSuccess: () => {
      toast.success('Despacho cerrado y notificado a Ventas')
      qc.invalidateQueries({ queryKey: ['despachos'] })
    },
    onError: (e: any) => toast.error(e?.response?.data?.detail || 'Error al cerrar'),
  })

  const anularMut = useMutation({
    mutationFn: (despachoId: number) => despachosAPI.anular(despachoId),
    onSuccess: () => {
      toast.success('Despacho anulado')
      qc.invalidateQueries({ queryKey: ['despachos'] })
    },
    onError: (e: any) => toast.error(e?.response?.data?.detail || 'Error al anular'),
  })

  return (
    <div className="space-y-4">
      {/* KPI cards */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        {/* Los 2 primeros KPIs usan los campos NUEVOS de /counts (descuentan lo ya
            tomado por despachos abiertos) y abren el panel «Listo para despachar».
            Fallback a ocs_listas (misma magnitud: n° de OCs) solo si el backend
            todavía no manda el campo aditivo. */}
        <KpiCard
          label="OCs Listas"
          value={counts?.ocs_con_disponible ?? counts?.ocs_listas ?? 0}
          icon={<Package className="w-5 h-5" />}
          color="text-brand-500"
          sub="Con un. por despachar"
          onClick={() => setShowListoResumen(true)}
        />
        <KpiCard
          label="Un. por Despachar"
          // Sin fallback numérico: con backend viejo (campo ausente) un «0» sería
          // mentira — y a diferencia del KPI 1, acá no existe otro campo de la
          // MISMA unidad al que caer. '—' dice honesto «no lo sé».
          value={
            typeof counts?.unidades_despachables === 'number'
              ? fmtQtyPicking(counts.unidades_despachables)
              : '—'
          }
          icon={<CheckCircle2 className="w-5 h-5" />}
          color="text-emerald-500"
          sub="Disponibles ahora en bodega"
          onClick={() => setShowListoResumen(true)}
        />
        <KpiCard
          label="Items Despachados"
          value={counts?.items_despachados ?? 0}
          icon={<Truck className="w-5 h-5" />}
          color="text-purple-500"
          sub="Total histórico"
        />
        <KpiCard
          label="OCs En Curso"
          value={ocs.filter((o: OcCard) => o.estado === 'parcial').length}
          icon={<AlertCircle className="w-5 h-5" />}
          color="text-amber-500"
          sub="Con despacho abierto"
        />
      </div>

      {/* Search — UNA caja, ARRIBA de las pestañas: el operador tiene UN número
          en la mano y no sabe clasificarlo. El placeholder es un CONTRATO: cada
          palabra es un campo que la consulta realmente toca. */}
      <div className="relative">
        <Search
          className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2"
          style={{ color: 'var(--text-faint)' }}
        />
        <input
          type="text"
          value={search}
          onChange={e => {
            setSearch(e.target.value)
            // Editar la búsqueda A MANO cancela el scroll perseguidor del panel:
            // el operador cambió de objetivo (irAOcDesdePanel setea search por
            // código, pero re-arma su pendiente DESPUÉS, así que no le afecta).
            setScrollOcPendiente(null)
          }}
          onKeyDown={e => {
            if (e.key === 'Enter') setSearchQ(search.trim()) // saltea el debounce
            if (e.key === 'Escape') {
              setSearch('')
              setSearchQ('')
              setScrollOcPendiente(null)
            }
          }}
          placeholder="N° parte, repuesto, COT, OC, cliente, embarque, N° despacho o guía…"
          className="input pl-10 pr-10"
        />
        {search && (
          <button
            onClick={() => {
              setSearch('')
              setSearchQ('')
              setScrollOcPendiente(null)
            }}
            className="absolute right-3 top-1/2 -translate-y-1/2 hover:opacity-100 opacity-60"
            style={{ color: 'var(--text-muted)' }}
          >
            <X className="w-4 h-4" />
          </button>
        )}
      </div>

      {/* Tabs */}
      <div className="flex gap-2">
        <TabBtn active={tab === 'listas'} onClick={() => setTab('listas')}>
          OC-Clientes Listas
        </TabBtn>
        <TabBtn active={tab === 'en_curso'} onClick={() => setTab('en_curso')}>
          Despachos en Curso
        </TabBtn>
        <TabBtn active={tab === 'historial'} onClick={() => setTab('historial')}>
          Historial
        </TabBtn>
      </div>

      {/* List */}
      {isLoading ? (
        <div className="text-center py-12" style={{ color: 'var(--text-faint)' }}>
          Cargando...
        </div>
      ) : ocs.length === 0 ? (
        // Vacíos DIFERENCIADOS: "no coincide nada" ≠ "no hay nada cargado".
        qEfectivo ? (
          <div className="text-center py-12" style={{ color: 'var(--text-faint)' }}>
            Sin resultados para «{qEfectivo}» en{' '}
            {tab === 'listas' ? 'OC-Clientes Listas' : tab === 'en_curso' ? 'Despachos en Curso' : 'Historial'}.
            <div className="text-xs mt-1">Probá en las otras pestañas o afiná el término.</div>
          </div>
        ) : (
          <div className="text-center py-12" style={{ color: 'var(--text-faint)' }}>
            No hay OCs {tab === 'listas' ? 'listas para despacho' : tab === 'en_curso' ? 'con despachos en curso' : 'en historial'}
          </div>
        )
      ) : (
        <div className="space-y-2">
          {/* Encabezado HONESTO: nunca truncar en silencio */}
          {totalOcs > ocs.length && (
            <div className="text-xs px-1" style={{ color: 'var(--text-muted)' }}>
              Mostrando {ocs.length} de {totalOcs}{' '}
              {qEfectivo ? 'coincidencias — afiná la búsqueda' : 'OCs'}
            </div>
          )}
          {ocResp?.normalizado && (
            <div className="text-xs px-1" style={{ color: 'var(--text-faint)' }}>
              Buscaste «{qEfectivo}»; también busqué «{qEfectivo.replace(/[-\s]/g, '')}» (sin guiones).
            </div>
          )}
          {ocsOrdenadas.map((oc: OcCard) => (
            <OcRow
              key={oc.id}
              oc={oc}
              qTokens={qTokens}
              expanded={expandedOc === oc.id}
              onExpand={() => setExpandedOc(expandedOc === oc.id ? null : oc.id)}
              detail={expandedOc === oc.id ? (ocDetail as OcDetail | undefined) : undefined}
              onCrearDespacho={() => setModalOc(ocDetail as OcDetail)}
              onCerrarDespacho={(id: number) => {
                if (confirm('¿Confirmar cierre del despacho? Los ítems pasarán a estado "despachado".')) {
                  cerrarMut.mutate(id)
                }
              }}
              onAnularDespacho={(id: number) => {
                if (confirm('¿Anular despacho? Los ítems volverán a estar disponibles.')) {
                  anularMut.mutate(id)
                }
              }}
              onFirmarDespacho={(d: DespachoRow, dteFolio: string | null) => {
                setFirmarDespacho(d)
                setFirmarDteFolio(dteFolio)
              }}
              onEditDespacho={(d: DespachoRow, dteFolio: string | null) => {
                setEditDespacho(d)
                setEditDespachoDteFolio(dteFolio)
              }}
              onEmitirGuia={(d: DespachoRow) => setEmitirGuia(d)}
            />
          ))}
          {/* Nada de paginador numérico: el operador no navega páginas, achica */}
          {totalOcs > ocs.length && pageSize < PAGE_SIZE_MAX && (
            <button
              onClick={() => setPageSize(s => Math.min(s + 50, PAGE_SIZE_MAX))}
              className="w-full py-2 text-sm rounded-xl border hover:bg-[var(--surface-200)] transition-colors"
              style={{ borderColor: 'var(--border)', color: 'var(--text-muted)' }}
            >
              Ver más (+50)
            </button>
          )}
          {totalOcs > PAGE_SIZE_MAX && pageSize >= PAGE_SIZE_MAX && (
            <div className="text-xs text-center py-2" style={{ color: 'var(--text-faint)' }}>
              Demasiadas coincidencias. Agregá el N° de cotización o el cliente.
            </div>
          )}
        </div>
      )}

      {/* Panel resumen «Listo para despachar» (desde los KPIs) */}
      {showListoResumen && (
        <ListoParaDespacharModal
          onClose={() => setShowListoResumen(false)}
          onIrAOc={irAOcDesdePanel}
        />
      )}

      {/* Modal crear despacho */}
      {modalOc && (
        <CrearDespachoModal
          oc={modalOc}
          onClose={() => setModalOc(null)}
          onCreated={() => {
            setModalOc(null)
            qc.invalidateQueries({ queryKey: ['despachos'] })
          }}
        />
      )}

      {/* Modal firmar guía (subir foto firmada, con firma parcial por ítem) */}
      {firmarDespacho !== null && (
        <FirmarGuiaModal
          despacho={firmarDespacho}
          dteFolio={firmarDteFolio}
          onClose={() => setFirmarDespacho(null)}
          onDone={() => {
            setFirmarDespacho(null)
            qc.invalidateQueries({ queryKey: ['despachos'] })
          }}
        />
      )}

      {/* Modal editar transportista / N° expedición */}
      {editDespacho && (
        <EditarDespachoModal
          despacho={editDespacho}
          dteFolio={editDespachoDteFolio}
          onClose={() => setEditDespacho(null)}
          onSaved={() => {
            setEditDespacho(null)
            qc.invalidateQueries({ queryKey: ['despachos'] })
          }}
        />
      )}

      {/* Modal emitir guía de despacho electrónica (SII) vía Wasabil */}
      {emitirGuia && (
        <EmitirGuiaSIIModal
          despacho={emitirGuia}
          onClose={() => {
            setEmitirGuia(null)
            // Refrescar SIEMPRE al cerrar: la emisión pudo avanzar aunque el
            // usuario cierre el modal a mitad de camino (folio/badges al día)
            qc.invalidateQueries({ queryKey: ['despachos'] })
            qc.invalidateQueries({ queryKey: ['wasabil'] })
          }}
          onDone={() => {
            qc.invalidateQueries({ queryKey: ['despachos'] })
            qc.invalidateQueries({ queryKey: ['wasabil'] })
          }}
        />
      )}
    </div>
  )
}

// onClick OPCIONAL: con él la card se vuelve un <button> real (teclado + foco
// gratis) con hint «ver detalle» al hover; sin él sigue siendo el div inerte de
// siempre (los otros 2 usos no cambian).
function KpiCard({ label, value, icon, color, sub, onClick }: any) {
  const contenido = (
    <>
      <div className="flex items-center justify-between mb-2">
        <span
          className="text-xs uppercase tracking-wider"
          style={{ color: 'var(--text-faint)' }}
        >
          {label}
        </span>
        <span className={color}>{icon}</span>
      </div>
      <div className={`text-3xl font-bold ${color}`}>{value}</div>
      <div className="text-xs mt-1 flex items-center justify-between gap-2" style={{ color: 'var(--text-muted)' }}>
        <span>{sub}</span>
        {onClick && (
          <span
            className="inline-flex items-center gap-0.5 text-[10px] font-semibold opacity-0 group-hover:opacity-100 group-focus-visible:opacity-100 transition-opacity shrink-0"
            style={{ color: 'var(--text-faint)' }}
          >
            ver detalle <ChevronRight className="w-3 h-3" />
          </span>
        )}
      </div>
    </>
  )
  if (!onClick) return <div className="card p-4">{contenido}</div>
  return (
    <button
      type="button"
      onClick={onClick}
      className="card p-4 text-left w-full group cursor-pointer hover:bg-[var(--surface-200)] transition-colors"
    >
      {contenido}
    </button>
  )
}

function TabBtn({ active, onClick, children }: any) {
  return (
    <button
      onClick={onClick}
      className={`px-4 py-2 rounded-xl text-sm font-semibold border transition-colors ${
        active
          ? 'bg-brand-500/15 text-brand-500 border-brand-500/40'
          : 'hover:bg-[var(--surface-200)]'
      }`}
      style={
        active
          ? undefined
          : { backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)', color: 'var(--text-muted)' }
      }
    >
      {children}
    </button>
  )
}

// Estados de un DTE realmente EN PROCESO (espejo del guard _guia_electronica_activa
// del backend): claim en vuelo o documento vivo en Wasabil sin resultado final.
// OJO: 'no_enviado' (limbo: claim expirado sin respuesta) NO está — el backend
// permite anular/editar en ese caso y el frontend no debe sobre-bloquear.
const DTE_EN_PROCESO = ['enviando', 'procesando', 'pendiente']

// Qué pasarle a los modales que pueden tocar el N° de guía (editar / firmar):
//   'verificando' = la consulta de DTEs no resolvió con éxito (bloquear por precaución)
//   folio SII     = guía electrónica emitida (bloqueado: el folio no se edita a mano)
//   'en_emision'  = guía en proceso en el SII, aún sin folio (bloqueado: llegará solo)
//   null          = sin guía electrónica (o fallida): el N° manual se puede editar
function folioParaModal(dtesListos: boolean, dte: any): string | null {
  if (!dtesListos) return 'verificando'
  if (dte?.folio) return dte.folio
  if (dte && DTE_EN_PROCESO.includes(dte.estado)) return 'en_emision'
  return null
}

function OcRow({
  oc,
  qTokens = [],
  expanded,
  onExpand,
  detail,
  onCrearDespacho,
  onCerrarDespacho,
  onAnularDespacho,
  onFirmarDespacho,
  onEditDespacho,
  onEmitirGuia,
}: any) {
  const badge = estadoLabel[oc.estado] ?? estadoLabel.pendiente
  // Estado de las guías electrónicas (Wasabil) de los despachos de esta OC —
  // solo BD, en lote, para pintar folio/PDF/fallida sin N llamadas.
  // `dtesListos` importa: mientras la consulta no resuelva CON ÉXITO, el folio SII
  // se trata como DESCONOCIDO (se bloquea la edición manual por precaución, no al
  // revés). isSuccess y no isFetched: una consulta FALLIDA también debe bloquear.
  const despachoIds = (detail?.despachos ?? []).map((d: DespachoRow) => d.id)
  const { data: dtes = {}, isSuccess: dtesListos } = useQuery({
    queryKey: ['wasabil', 'estado-batch', despachoIds],
    queryFn: () => wasabilAPI.estadoBatch(despachoIds).then(r => r.data),
    enabled: expanded && despachoIds.length > 0,
  })
  // Desplegable POR DESPACHO: qué ítems viajaron en cada guía. La carga es
  // perezosa (recién al abrir) y queda cacheada por React Query.
  const [despachosAbiertos, setDespachosAbiertos] = useState<Record<number, boolean>>({})
  // Filtro LOCAL de la tabla de ítems de la OC expandida (pendiente del paso 8 de
  // la spec de buscadores 2026-08-05): mismos helpers y mismo colapsado que el
  // buscador de picking del modal, para que las dos cajas encuentren lo mismo.
  const [filtroItems, setFiltroItems] = useState('')
  const resultadoItems = useMemo(
    () => filtrarItems<ItemRow>((detail?.items ?? []) as ItemRow[], filtroItems),
    [detail?.items, filtroItems],
  )
  // Reparto de bultos: solo sobre despachos NO anulados (uno anulado nunca viaja).
  const [showBultos, setShowBultos] = useState(false)
  const despachosNoAnulados = useMemo(
    () => ((detail?.despachos ?? []) as DespachoRow[]).filter(d => d.estado !== 'anulado'),
    [detail?.despachos],
  )
  // Texto de la insignia gris «0 un. por despachar». El motivo lo manda el
  // backend (única fórmula del cupo); acá solo se elige la redacción, y si el
  // campo no viaja se cae al neutro en vez de acusar a un despacho inexistente.
  const sinCupo =
    (oc.motivo_sin_cupo ? MOTIVO_SIN_CUPO[oc.motivo_sin_cupo] : undefined) ?? MOTIVO_SIN_CUPO_NEUTRO
  return (
    // id ancla del «Ir a la OC» del panel Listo para despachar (scrollIntoView).
    <div id={`oc-card-${oc.id}`} className="card overflow-hidden">
      <button
        onClick={onExpand}
        className="w-full p-4 flex items-center gap-4 hover:bg-[var(--surface-200)] transition text-left"
      >
        <div className="w-10 h-10 rounded-lg bg-brand-500/10 flex items-center justify-center text-brand-500 shrink-0">
          <Package className="w-5 h-5" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1 flex-wrap">
            <span className="text-xs text-brand-500 font-mono font-semibold">
              {/* numero_oc es NULLABLE (OCs legacy): sin el fallback la insignia
                  imprimía un «OC-» pelado. Misma convención que el resto de la
                  pantalla y que el texto del transportista: «OC #<id>». */}
              {oc.numero_oc
                ? <>OC-<Resaltado texto={oc.numero_oc} tokens={qTokens} /></>
                : `OC #${oc.id}`}
            </span>
            {/* Insignia ÚNICA y honesta. Con los campos nuevos (solo viajan en la
                tab «listas»), se CORRIGE el chip existente — no se agrega un
                segundo al lado: una sola verdad.
                - disponible > 0 → verde: qué se puede despachar AHORA (descuenta
                  lo ya tomado por despachos abiertos).
                - disponible = 0 → gris slate, JAMÁS rojo/ámbar: el chip informa,
                  no alarma. El texto y el porqué los pone el MOTIVO que manda el
                  backend (ver MOTIVO_SIN_CUPO), y no todos los motivos son
                  buenas noticias: 'en_preparacion' y 'despachado' son trabajo en
                  curso o terminado, pero 'sin_stock' es mercadería que no llegó
                  (o está en reclamo al proveedor). El gris es «esto no se
                  resuelve en esta pantalla», no «esto está bien».
                En otras tabs los campos no viajan y el chip queda como estaba. */}
            {typeof oc.items_despachables === 'number' &&
            typeof oc.unidades_despachables === 'number' ? (
              oc.unidades_despachables > 0 ? (
                <span className="text-xs px-2 py-0.5 rounded-full font-semibold inline-flex items-center gap-1 bg-emerald-500/15 text-emerald-500 dark:text-emerald-400">
                  <Zap className="w-3 h-3" />
                  {/* «X de Y ítems»: el denominador es el encargo original del
                      dueño — la cobertura de la OC, no solo lo despachable. */}
                  Listo · {oc.items_despachables} de {oc.total_items} ítem{oc.total_items === 1 ? '' : 's'} ·{' '}
                  {fmtQtyPicking(oc.unidades_despachables)} un. por despachar
                </span>
              ) : (
                /* Mensaje HONESTO por motivo (ver MOTIVO_SIN_CUPO): el cupo en 0
                   puede venir de despachos abiertos, de despachos ya CERRADOS o
                   de mercadería que no llegó, y cada caso tiene otra salida. */
                <span
                  className="text-xs px-2 py-0.5 rounded-full font-semibold bg-slate-500/15 text-slate-500"
                  title={sinCupo.title}
                >
                  {sinCupo.label} · 0 un. por despachar
                </span>
              )
            ) : (
              <span className={`text-xs px-2 py-0.5 rounded-full font-semibold ${badge.color}`}>
                {badge.label}
                {/* Cobertura REAL de la tarjeta: "Listo · 3 de 7 ítems" — con los
                    campos que el endpoint ya devuelve (encargo del dueño). */}
                {oc.estado === 'listo' && oc.total_items > 0 &&
                  ` · ${oc.items_en_bodega} de ${oc.total_items} ítems`}
              </span>
            )}
            <MatchBadges match={oc.match} />
            <DiasRestantesBadge
              dias={oc.dias_restantes_critico ?? oc.dias_restantes_oc ?? null}
              label="entrega"
            />
          </div>
          <div className="font-semibold truncate" style={{ color: 'var(--text-primary)' }}>
            <Resaltado texto={oc.cliente} tokens={qTokens} />
          </div>
          <div className="text-xs truncate" style={{ color: 'var(--text-muted)' }}>
            {/* numero_oc nullable: sin el fallback esta línea decía «· OC #». */}
            <Resaltado texto={oc.numero_cotizacion} tokens={qTokens} /> · OC #{oc.numero_oc || oc.id}
            {oc.fecha_oc && ` · ${oc.fecha_oc}`}
            {oc.cond_pago && ` · ${oc.cond_pago}`}
            {oc.fecha_entrega && ` · Entrega: ${oc.fecha_entrega}`}
          </div>
          {/* Progress bar multi-segment */}
          <PipelineProgress oc={oc} />
        </div>
        <div className="shrink-0" style={{ color: 'var(--text-faint)' }}>
          {expanded ? <ChevronDown className="w-5 h-5" /> : <ChevronRight className="w-5 h-5" />}
        </div>
      </button>

      {expanded && detail && (
        <div className="border-t p-4 space-y-4" style={{ borderColor: 'var(--border)' }}>
          {/* Documentos */}
          {detail.cotizacion_id && detail.documentos && (
            <DocumentosSection
              cotizacionId={detail.cotizacion_id}
              ocId={detail.id}
              docs={detail.documentos}
              numeroOc={detail.numero_oc}
              numeroCot={detail.numero_cotizacion}
              embarques={detail.embarques}
            />
          )}

          {/* Datos destinatario */}
          <div className="grid grid-cols-2 gap-4 text-sm">
            <div>
              <div className="text-xs uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>
                Destinatario
              </div>
              <div style={{ color: 'var(--text-primary)' }}>{detail.contacto || '—'}</div>
              <div className="text-xs" style={{ color: 'var(--text-muted)' }}>{detail.telefono || ''}</div>
              <div className="text-xs" style={{ color: 'var(--text-muted)' }}>{detail.email || ''}</div>
            </div>
            <div>
              <div className="text-xs uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>
                Dirección de Entrega
              </div>
              <div className="text-xs" style={{ color: 'var(--text-primary)' }}>
                {detail.direccion || 'Sin dirección'}
              </div>
            </div>
          </div>

          {/* Items */}
          <div>
            <div className="flex items-center justify-between gap-3 mb-2 flex-wrap">
              {/* aria-live: el lector de pantalla anuncia el "N de M" al filtrar. */}
              <div aria-live="polite" className="text-xs uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>
                Items ({filtroItems.trim()
                  ? `${resultadoItems.matches.length} de ${detail.items.length}`
                  : detail.items.length})
              </div>
              {/* Caja chica del filtro local: filtra EXACTAMENTE los dos campos que
                  promete el placeholder (el placeholder es contrato). */}
              <div className="relative w-64 max-w-full">
                <Search
                  className="w-3.5 h-3.5 absolute left-2.5 top-1/2 -translate-y-1/2 pointer-events-none"
                  style={{ color: 'var(--text-faint)' }}
                />
                <input
                  value={filtroItems}
                  onChange={e => setFiltroItems(e.target.value)}
                  onKeyDown={e => {
                    // Esc limpia el filtro y nada más (acá no hay modal que cerrar).
                    if (e.key === 'Escape') {
                      e.preventDefault()
                      setFiltroItems('')
                    }
                  }}
                  placeholder="Buscar por N° de parte o descripción"
                  className="input py-1 pl-8 pr-7 text-xs"
                />
                {filtroItems !== '' && (
                  <button
                    onClick={() => setFiltroItems('')}
                    className="absolute right-1.5 top-1/2 -translate-y-1/2 p-0.5 rounded hover:bg-[var(--surface-300)]"
                    style={{ color: 'var(--text-faint)' }}
                    title="Limpiar filtro"
                  >
                    <X className="w-3.5 h-3.5" />
                  </button>
                )}
              </div>
            </div>
            {filtroItems.trim() !== '' && resultadoItems.huboColapsado && (
              <div className="text-[11px] mb-1.5" style={{ color: 'var(--text-muted)' }}>
                también busqué <span className="font-mono font-semibold">{resultadoItems.queryColapsada}</span>
              </div>
            )}
            <div className="border rounded-xl overflow-x-auto" style={{ borderColor: 'var(--border)' }}>
              <table className="w-full text-sm">
                <thead style={{ backgroundColor: 'var(--surface-200)' }}>
                  <tr className="text-xs uppercase" style={{ color: 'var(--text-muted)' }}>
                    <th className="text-left p-2">N° Parte</th>
                    <th className="text-left p-2">Descripción</th>
                    <th className="text-left p-2">Marca</th>
                    <th className="text-right p-2">Cant.</th>
                    <th className="text-right p-2">Desp.</th>
                    <th className="text-right p-2">Disp.</th>
                    <th className="text-right p-2">Plazo</th>
                    <th className="text-center p-2">Días Rest.</th>
                    <th className="text-center p-2">Estado</th>
                  </tr>
                </thead>
                <tbody>
                  {filtroItems.trim() !== '' && resultadoItems.matches.length === 0 && (
                    <tr>
                      <td colSpan={9} className="p-4 text-center">
                        <span className="text-sm font-semibold" style={{ color: 'var(--text-primary)' }}>
                          No está en esta OC
                        </span>
                      </td>
                    </tr>
                  )}
                  {resultadoItems.matches.map((m: PickingMatch<ItemRow>) => {
                    const it = m.item
                    const yaDespachado = it.estado_item === 'despachado'
                    return (
                      <tr key={it.id} className="border-t" style={{ borderColor: 'var(--border)' }}>
                        <td className="p-2 font-mono text-xs text-brand-500">
                          <CampoFiltrado
                            texto={it.numero_parte}
                            query={filtroItems}
                            colapsado={m.porColapsado && m.camposColapsados.includes('numero_parte')}
                          />
                        </td>
                        <td className="p-2" style={{ color: 'var(--text-primary)' }}>
                          <CampoFiltrado
                            texto={it.descripcion}
                            query={filtroItems}
                            colapsado={m.porColapsado && m.camposColapsados.includes('descripcion')}
                          />
                        </td>
                        <td className="p-2 text-xs" style={{ color: 'var(--text-muted)' }}>{it.marca}</td>
                        <td className="p-2 text-right" style={{ color: 'var(--text-primary)' }}>{it.cantidad}</td>
                        <td className="p-2 text-right" style={{ color: 'var(--text-muted)' }}>{it.qty_despachada}</td>
                        <td className="p-2 text-right">
                          {/* esDespachable y no `> 0`: mismo umbral que el backend
                              (0.001), para que la celda no se pinte en verde por un
                              residuo flotante que la insignia de la card ya declaró
                              «0 un. por despachar». */}
                          <span
                            className={esDespachable(it.qty_disponible) ? 'text-emerald-500 font-semibold' : ''}
                            style={esDespachable(it.qty_disponible) ? undefined : { color: 'var(--text-faint)' }}
                          >
                            {it.qty_disponible}
                          </span>
                        </td>
                        <td className="p-2 text-right text-xs" style={{ color: 'var(--text-muted)' }}>
                          {it.plazo_entrega_max != null ? `${it.plazo_entrega_max}d` : '—'}
                        </td>
                        <td className="p-2 text-center">
                          {yaDespachado ? (
                            <span className="text-[11px]" style={{ color: 'var(--text-faint)' }}>
                              —
                            </span>
                          ) : (
                            <DiasRestantesBadge dias={it.dias_restantes} compact />
                          )}
                        </td>
                        <td className="p-2 text-center">
                          <ItemEstadoBadge estado={it.estado_item} reclamo={it.en_reclamo} />
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </div>

          {/* Despachos */}
          {detail.despachos.length > 0 && (
            <div>
              <div className="flex items-center justify-between gap-3 mb-2">
                <div className="text-xs uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>
                  Despachos ({detail.despachos.length})
                </div>
                {despachosNoAnulados.length > 0 && (
                  <button
                    onClick={() => setShowBultos(true)}
                    className="px-3 py-1.5 text-xs rounded-lg hover:bg-[var(--surface-300)] flex items-center gap-1 font-semibold border"
                    style={{ color: 'var(--text-muted)', borderColor: 'var(--border)' }}
                    title="Cómo se reparten las guías en los bultos de esta OC (texto listo para el transportista)"
                  >
                    <Package className="w-3 h-3" /> Bultos
                  </button>
                )}
              </div>
              <div className="space-y-2">
                {detail.despachos.map((d: DespachoRow) => {
                  const dte = (dtes as any)[d.id]
                  const abierto = !!despachosAbiertos[d.id]
                  return (
                  <div
                    key={d.id}
                    className="border rounded-xl overflow-hidden"
                    style={{ backgroundColor: 'var(--surface-200)', borderColor: 'var(--border)' }}
                  >
                  <div className="flex items-center justify-between p-3 gap-2">
                    <button
                      onClick={() => setDespachosAbiertos(p => ({ ...p, [d.id]: !p[d.id] }))}
                      className="p-1 rounded-lg hover:bg-[var(--surface-300)] shrink-0 self-start mt-0.5"
                      style={{ color: 'var(--text-faint)' }}
                      title={abierto ? 'Ocultar los ítems de este despacho' : 'Ver los ítems de este despacho'}
                    >
                      {abierto ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
                    </button>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="font-mono text-sm font-semibold" style={{ color: 'var(--text-primary)' }}>
                          {d.numero_despacho}
                        </span>
                        <DespachoEstadoBadge estado={d.estado} />
                        {/* Rótulo del bulto (caja física del empaque). "" o null = sin chip. */}
                        {(d.bulto_numero || '').trim() !== '' && (
                          <span
                            className="text-[10px] font-semibold px-2 py-0.5 rounded-full"
                            style={{ backgroundColor: 'var(--surface-300)', color: 'var(--text-muted)' }}
                            title="Bulto (caja) en que viaja este despacho"
                          >
                            📦 {d.bulto_numero}
                          </span>
                        )}
                        {dte?.estado === 'emitido' && (
                          <span className="text-[10px] font-semibold px-2 py-0.5 rounded-full bg-blue-500/15 text-blue-600 dark:text-blue-400 inline-flex items-center gap-1">
                            <Receipt className="w-3 h-3" /> Guía SII {dte.folio}
                          </span>
                        )}
                        {dte && dte.estado !== 'emitido' && (
                          <span className="text-[10px] font-semibold px-2 py-0.5 rounded-full bg-amber-500/15 text-amber-600 dark:text-amber-400 inline-flex items-center gap-1">
                            <AlertTriangle className="w-3 h-3" />
                            {dte.puede_reintentar ? 'Emisión SII fallida' : 'Emisión SII en proceso'}
                          </span>
                        )}
                        {d.guia_firmada && (
                          <span className="text-[10px] font-semibold px-2 py-0.5 rounded-full bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 inline-flex items-center gap-1">
                            <CheckCircle2 className="w-3 h-3" /> Guía firmada
                          </span>
                        )}
                        {/* Faltante de ENTREGA declarado al firmar: unidades de la guía
                            que no llegaron. No se facturan por esta guía. */}
                        {(d.faltante_total ?? 0) > 0 && (
                          <span
                            className="text-[10px] font-semibold px-2 py-0.5 rounded-full bg-amber-500/15 text-amber-600 dark:text-amber-400 inline-flex items-center gap-1"
                            title="Unidades de la guía que no llegaron al cliente (declaradas al firmar). No se facturan por esta guía. Abre el despacho para ver el motivo."
                          >
                            <AlertTriangle className="w-3 h-3" /> faltante: {d.faltante_total}
                          </span>
                        )}
                        {/* Guía EN PAPEL sin fecha de emisión: la factura al SII se va a
                            bloquear (la referencia 52 la exige). Se avisa acá, en Bodega,
                            y no recién en Contabilidad cuando ya se quiere facturar.
                            `dtesListos && !dte` (y no `dte?.estado !== 'emitido'`): al
                            emitir la guía electrónica el backend copia el folio del SII a
                            `numero_guia` y deja `fecha_guia` vacía a propósito, así que
                            sin este filtro el aviso salía en toda guía electrónica —
                            durante la carga siempre, y PEGADO si la consulta de estados
                            fallaba (tiene retry:1). Mismo criterio fail-closed que
                            folioParaModal: mientras no se sepa, no se acusa. */}
                        {d.numero_guia && !d.fecha_guia && dtesListos && !dte && (
                          <span
                            className="text-[10px] font-semibold px-2 py-0.5 rounded-full bg-amber-500/15 text-amber-600 dark:text-amber-400 inline-flex items-center gap-1"
                            title="La factura electrónica cita la guía con su fecha de emisión. Cárgala en Transportista / Editar."
                          >
                            <AlertTriangle className="w-3 h-3" /> Falta fecha de la guía
                          </span>
                        )}
                      </div>
                      <div className="text-xs mt-0.5" style={{ color: 'var(--text-muted)' }}>
                        {d.items_count} ítems
                        {d.transportista && ` · ${d.transportista}`}
                        {d.numero_guia && ` · Guía: ${d.numero_guia}`}
                        {/* Fecha de EMISIÓN de la guía (la que cita la factura), no la del
                            cierre del despacho. Se parte el ISO a mano: new Date('2026-07-15')
                            se interpreta en UTC y en Chile muestra el día anterior. */}
                        {d.fecha_guia && ` (${d.fecha_guia.slice(0, 10).split('-').reverse().join('-')})`}
                        {d.numero_expedicion && ` · Exp: ${d.numero_expedicion}`}
                        {d.fecha_despacho && ` · ${fmtFechaServidor(d.fecha_despacho)}`}
                      </div>
                    </div>
                    <div className="flex flex-wrap gap-2 shrink-0 items-center justify-end">
                      {d.estado === 'en_preparacion' && (
                        <>
                          {(!dte || dte.puede_reintentar) && (
                            <button
                              onClick={() => onEmitirGuia(d)}
                              className="px-3 py-1.5 text-xs bg-blue-500/15 text-blue-600 dark:text-blue-400 rounded-lg hover:bg-blue-500/25 flex items-center gap-1 font-semibold"
                              title="Emitir la guía de despacho electrónica al SII vía Wasabil"
                            >
                              <Receipt className="w-3 h-3" /> {dte ? 'Reintentar guía SII' : 'Emitir guía SII'}
                            </button>
                          )}
                          {/* Paso 3 del flujo: el transportista se agrega DESPUÉS de emitir
                              la guía (no viaja al SII). Reusa el modal de edición, que ya
                              blinda el folio del SII para que no se pise a mano. */}
                          <button
                            onClick={() => onEditDespacho(d, folioParaModal(dtesListos, dte))}
                            className="px-3 py-1.5 text-xs rounded-lg hover:bg-[var(--surface-300)] flex items-center gap-1 font-semibold border"
                            style={{ color: 'var(--text-muted)', borderColor: 'var(--border)' }}
                            title="Agregar o editar el transportista y el N° de expedición"
                          >
                            <Truck className="w-3 h-3" /> {d.transportista ? 'Transportista' : 'Agregar transportista'}
                          </button>
                          <button
                            onClick={() => onCerrarDespacho(d.id)}
                            className="px-3 py-1.5 text-xs bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 rounded-lg hover:bg-emerald-500/25 flex items-center gap-1 font-semibold"
                          >
                            <Send className="w-3 h-3" /> Confirmar
                          </button>
                          <button
                            onClick={() => {
                              // El backend rechaza (409) anular con guía SII viva; acá se
                              // explica de inmediato en vez de dejar intentar algo que fallará.
                              if (dte?.estado === 'emitido') {
                                toast.error(
                                  `Este despacho tiene guía SII EMITIDA (folio ${dte.folio}): ` +
                                  'anúlala primero en Wasabil y luego anula el despacho.'
                                )
                                return
                              }
                              if (dte && DTE_EN_PROCESO.includes(dte.estado)) {
                                toast.error('Este despacho tiene una emisión de guía SII en curso: espera el resultado antes de anular.')
                                return
                              }
                              onAnularDespacho(d.id)
                            }}
                            className="px-3 py-1.5 text-xs bg-red-500/10 text-red-500 rounded-lg hover:bg-red-500/20"
                          >
                            <Trash2 className="w-3 h-3" />
                          </button>
                        </>
                      )}
                      {d.estado === 'despachado' && !d.guia_firmada && (
                        <button
                          onClick={() => onFirmarDespacho(d, folioParaModal(dtesListos, dte))}
                          className="px-3 py-1.5 text-xs bg-blue-500/15 text-blue-600 dark:text-blue-400 rounded-lg hover:bg-blue-500/25 flex items-center gap-1 font-semibold"
                        >
                          <CheckCircle2 className="w-3 h-3" /> Marcar guía firmada
                        </button>
                      )}
                      {/* Re-firma: ya firmada CON faltante — se pueden corregir las
                          cantidades (el backend valida contra lo ya facturado). */}
                      {d.estado === 'despachado' && Boolean(d.guia_firmada) && (
                        <button
                          onClick={() => onFirmarDespacho(d, folioParaModal(dtesListos, dte))}
                          className="px-3 py-1.5 text-xs bg-amber-500/15 text-amber-600 dark:text-amber-400 rounded-lg hover:bg-amber-500/25 flex items-center gap-1 font-semibold"
                          title="Corregir las cantidades firmadas o el motivo del faltante"
                        >
                          <Pencil className="w-3 h-3" /> Editar firma
                        </button>
                      )}
                      {dte && dte.estado !== 'emitido' && !dte.puede_reintentar && (
                        <button
                          onClick={() => onEmitirGuia(d)}
                          className="px-3 py-1.5 text-xs bg-amber-500/15 text-amber-600 dark:text-amber-400 rounded-lg hover:bg-amber-500/25 flex items-center gap-1 font-semibold"
                          title="La emisión sigue en proceso: consultar el estado real en el SII"
                        >
                          <Clock className="w-3 h-3" /> Estado guía SII
                        </button>
                      )}
                      {dte?.pdf_url && (
                        <button
                          onClick={() => window.open(dte.pdf_url, '_blank', 'noopener,noreferrer')}
                          className="px-2.5 py-1.5 text-xs bg-blue-500/10 text-blue-600 dark:text-blue-400 rounded-lg hover:bg-blue-500/20 flex items-center gap-1"
                          title="Ver el PDF de la guía electrónica (SII)"
                        >
                          <FileText className="w-3 h-3" /> PDF SII
                        </button>
                      )}
                      {d.guia_firmada_archivo && (
                        <button
                          onClick={() => abrirDocumento(d.guia_firmada_archivo!)}
                          className="px-2.5 py-1.5 text-xs bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 rounded-lg hover:bg-emerald-500/20 flex items-center gap-1"
                          title="Ver guía firmada"
                        >
                          <Eye className="w-3 h-3" /> Guía
                        </button>
                      )}
                      {d.estado !== 'en_preparacion' && d.estado !== 'anulado' && (
                        <button
                          onClick={() => onEditDespacho(d, folioParaModal(dtesListos, dte))}
                          className="p-1.5 rounded-lg hover:bg-[var(--surface-300)]"
                          style={{ color: 'var(--text-muted)' }}
                          title="Editar transportista / N° expedición"
                        >
                          <Pencil className="w-3.5 h-3.5" />
                        </button>
                      )}
                    </div>
                  </div>
                  {abierto && <DespachoItemsDetalle despachoId={d.id} />}
                  </div>
                  )
                })}
              </div>
            </div>
          )}

          {/* Action — el gate usa la MISMA regla de cupo que el modal y que el
              backend (esDespachable, tolerancia 0.001): con `> 0` un residuo
              flotante dejaba el botón encendido sobre una OC que la insignia y
              el panel ya daban por sin cupo. */}
          {detail.items.some((it: ItemRow) => esDespachable(it.qty_disponible)) && (
            <div className="space-y-1.5">
              <button
                onClick={onCrearDespacho}
                className="btn-primary w-full justify-center"
              >
                <Plus className="w-4 h-4" /> Crear Despacho
              </button>
              <p className="text-[11px] text-center" style={{ color: 'var(--text-faint)' }}>
                Eliges qué se despacha. La guía SII, el transportista y la confirmación van después, en la fila del despacho.
              </p>
            </div>
          )}
        </div>
      )}

      {showBultos && detail && (
        <RepartoBultosModal
          oc={detail}
          despachos={despachosNoAnulados}
          onClose={() => setShowBultos(false)}
        />
      )}
    </div>
  )
}

// ─── Desplegable: ítems de UN despacho (GET /despachos/{id}) ──────────────────
// Qué viajó en esta guía y, si la firma fue parcial, cuánto llegó y por qué.
// Degrada con gracia: un backend previo que no manda qty_firmada / facturado
// pinta la tabla sin esas columnas.
function DespachoItemsDetalle({ despachoId }: { despachoId: number }) {
  const { data: detalle, isLoading, isError } = useQuery<DespachoDetalle>({
    queryKey: ['despachos', 'detalle', despachoId],
    queryFn: () => despachosAPI.get(despachoId),
  })

  if (isLoading) {
    return (
      <div className="px-3 py-2 border-t text-xs flex items-center gap-2" style={{ borderColor: 'var(--border)', color: 'var(--text-muted)' }}>
        <Loader2 className="w-3.5 h-3.5 animate-spin" /> Cargando los ítems del despacho…
      </div>
    )
  }
  if (isError || !detalle) {
    return (
      <div className="px-3 py-2 border-t text-xs" style={{ borderColor: 'var(--border)', color: 'var(--text-muted)' }}>
        No se pudieron cargar los ítems del despacho. Cierra y vuelve a abrir el detalle.
      </div>
    )
  }
  const items = detalle.items ?? []
  if (items.length === 0) {
    return (
      <div className="px-3 py-2 border-t text-xs" style={{ borderColor: 'var(--border)', color: 'var(--text-faint)' }}>
        Este despacho no tiene detalle de ítems disponible.
      </div>
    )
  }
  const conFirma = items.some(it => it.qty_firmada !== undefined)
  const conFacturado = items.some(it => it.facturado !== undefined)
  const faltanteTotal = detalle.faltante_total ?? 0
  return (
    <div className="border-t" style={{ borderColor: 'var(--border)' }}>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead style={{ backgroundColor: 'var(--surface-300)' }}>
            <tr className="text-[10px] uppercase" style={{ color: 'var(--text-muted)' }}>
              <th className="text-left p-2">N° Parte</th>
              <th className="text-left p-2">Descripción</th>
              <th className="text-left p-2">Marca</th>
              <th className="text-right p-2">Despachada</th>
              {conFirma && <th className="text-right p-2">Firmada</th>}
              {conFacturado && <th className="text-center p-2">Facturado</th>}
            </tr>
          </thead>
          <tbody>
            {items.map((it: DespachoItemDetalle) => {
              // null/ausente = firmada completa (el caso común no gana fricción)
              const firmada = it.qty_firmada ?? it.qty_despachada
              const faltaLinea = it.qty_firmada != null ? it.qty_despachada - it.qty_firmada : 0
              return (
                <tr key={it.id} className="border-t" style={{ borderColor: 'var(--border)' }}>
                  <td className="p-2 font-mono text-brand-500">{it.numero_parte}</td>
                  <td className="p-2" style={{ color: 'var(--text-primary)' }}>{it.descripcion}</td>
                  <td className="p-2" style={{ color: 'var(--text-muted)' }}>{it.marca}</td>
                  <td className="p-2 text-right" style={{ color: 'var(--text-primary)' }}>{it.qty_despachada}</td>
                  {conFirma && (
                    <td className="p-2 text-right">
                      {faltaLinea > 0 ? (
                        <span
                          className="text-amber-600 dark:text-amber-400 font-semibold"
                          title={`No llegaron ${faltaLinea} de ${it.qty_despachada}`}
                        >
                          {firmada} <span className="font-normal">(faltan {faltaLinea})</span>
                        </span>
                      ) : (
                        <span style={{ color: 'var(--text-muted)' }}>{firmada}</span>
                      )}
                    </td>
                  )}
                  {conFacturado && (
                    <td className="p-2 text-center">
                      {it.facturado ? (
                        <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded-full bg-blue-500/15 text-blue-600 dark:text-blue-400">
                          Sí
                        </span>
                      ) : (
                        <span style={{ color: 'var(--text-faint)' }}>—</span>
                      )}
                    </td>
                  )}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      {faltanteTotal > 0 && (
        <div className="px-3 py-2 border-t text-xs flex items-start gap-1.5 bg-amber-500/10" style={{ borderColor: 'var(--border)' }}>
          <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5 text-amber-500" />
          <span style={{ color: 'var(--text-muted)' }}>
            <b className="text-amber-600 dark:text-amber-400">
              {faltanteTotal} unidad{faltanteTotal === 1 ? '' : 'es'} faltante{faltanteTotal === 1 ? '' : 's'} de entrega
            </b>
            {detalle.faltante_motivo && <> — {detalle.faltante_motivo}</>}
          </span>
        </div>
      )}
    </div>
  )
}

function DocumentosSection({
  cotizacionId,
  ocId,
  docs,
  numeroOc,
  numeroCot,
  embarques,
}: {
  cotizacionId: number
  ocId: number
  docs: DocumentosOc
  /** NULLABLE de verdad (OC legacy sin N°): el `tag` de abajo ya cae a «OC-{id}». */
  numeroOc?: string | null
  numeroCot?: string
  embarques?: EmbarqueResumen[]
}) {
  const MIME_XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
  const MIME_PDF = 'application/pdf'
  const tag = numeroOc || `OC-${ocId}`
  const cotTag = numeroCot || `COT-${cotizacionId}`

  const buttons = [
    {
      key: 'excel_oc',
      label: 'Excel OC',
      hint: 'Listado de items para picking',
      icon: <FileSpreadsheet className="w-4 h-4" />,
      enabled: docs.excel_oc,
      run: () =>
        downloadBlob(() => comprasAPI.downloadOcClienteExcel(ocId), `${tag}.xlsx`, MIME_XLSX),
    },
    {
      key: 'cot_formal',
      label: 'Cotización Formal',
      hint: 'Excel formal enviado al cliente',
      icon: <FileSpreadsheet className="w-4 h-4" />,
      enabled: docs.cot_formal_excel,
      run: () =>
        downloadBlob(
          () => cotizadorAPI.downloadFormal(cotizacionId),
          `${cotTag}-formal.xlsx`,
          MIME_XLSX,
        ),
    },
    {
      key: 'cot_pdf',
      label: 'Cotización PDF',
      hint: 'PDF de la cotización formal',
      icon: <FileText className="w-4 h-4" />,
      enabled: docs.cot_pdf,
      run: () =>
        downloadBlob(
          () => cotizadorAPI.downloadPdf(cotizacionId),
          `${cotTag}.pdf`,
          MIME_PDF,
        ),
    },
    {
      key: 'cot_original',
      label: 'Excel Original',
      hint: 'Archivo Excel cargado al iniciar',
      icon: <FileDown className="w-4 h-4" />,
      enabled: docs.cot_original_excel,
      run: () =>
        downloadBlob(
          () => cotizacionesAPI.download(cotizacionId),
          `${cotTag}-original.xlsx`,
          MIME_XLSX,
        ),
    },
  ]

  return (
    <div className="space-y-4">
      <div>
        <div
          className="text-xs uppercase tracking-wider mb-2 font-semibold"
          style={{ color: 'var(--text-faint)' }}
        >
          Documentos de la Cotización
        </div>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
          {buttons.map(({ key, ...rest }) => (
            <DocButton key={key} {...rest} />
          ))}
        </div>
      </div>
      {embarques && embarques.length > 0 && (
        <div>
          <div
            className="text-xs uppercase tracking-wider mb-2 font-semibold"
            style={{ color: 'var(--text-faint)' }}
          >
            Embarques de Importación ({embarques.length})
          </div>
          <div className="space-y-2">
            {embarques.map(emb => (
              <EmbarqueDocsCard key={emb.id} emb={emb} />
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function EmbarqueDocsCard({ emb }: { emb: EmbarqueResumen }) {
  const labels: { key: keyof EmbarqueDocs; label: string }[] = [
    { key: 'awb', label: 'AWB / BL' },
    { key: 'factura_comercial', label: 'Factura Comercial' },
    { key: 'packing_list', label: 'Packing List' },
    { key: 'certificado_origen', label: 'Cert. Origen' },
    { key: 'doc_adicional', label: 'Otros' },
  ]
  return (
    <div
      className="border rounded-xl p-3"
      style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}
    >
      <div className="flex flex-wrap items-center gap-2 mb-2 pb-2 border-b" style={{ borderColor: 'var(--border)' }}>
        <span className="font-mono text-sm font-semibold text-brand-500">{emb.numero}</span>
        {emb.estado && (
          <span className="text-xs px-2 py-0.5 rounded-full font-semibold bg-amber-500/15 text-amber-600 dark:text-amber-400">
            {emb.estado}
          </span>
        )}
        <span className="text-xs" style={{ color: 'var(--text-muted)' }}>
          {emb.items_de_esta_oc} ítem{emb.items_de_esta_oc === 1 ? '' : 's'} de esta OC
        </span>
        {emb.forwarder && (
          <span className="text-xs" style={{ color: 'var(--text-muted)' }}>
            · {emb.forwarder}
          </span>
        )}
        {emb.fecha_llegada_est && (
          <span className="text-xs" style={{ color: 'var(--text-muted)' }}>
            · ETA: {emb.fecha_llegada_est}
          </span>
        )}
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-2">
        {labels.map(({ key, label }) => (
          <EmbDocField key={key} label={label} doc={emb.documentos[key]} />
        ))}
      </div>
    </div>
  )
}

function EmbDocField({ label, doc }: { label: string; doc: DocValue | null }) {
  const [loading, setLoading] = useState(false)
  const [showTextModal, setShowTextModal] = useState(false)

  const handleClick = async () => {
    if (!doc) return
    if (doc.is_file && doc.filename) {
      // Real file: download via authenticated endpoint
      setLoading(true)
      try {
        const token = (() => {
          try {
            return JSON.parse(localStorage.getItem('machparts-auth') || '{}')?.state?.token || ''
          } catch {
            return ''
          }
        })()
        const res = await fetch(`/api/despachos/docs/${encodeURIComponent(doc.filename)}`, {
          headers: { Authorization: `Bearer ${token}` },
        })
        if (!res.ok) {
          const err = await res.json().catch(() => ({}))
          throw new Error(err.detail || 'Error al descargar')
        }
        const blob = await res.blob()
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = doc.filename
        document.body.appendChild(a)
        a.click()
        document.body.removeChild(a)
        setTimeout(() => URL.revokeObjectURL(url), 1000)
      } catch (e: any) {
        toast.error(e?.message || 'Error al descargar')
      } finally {
        setLoading(false)
      }
    } else {
      // Text value: open modal with content + copy
      setShowTextModal(true)
    }
  }

  const isFile = doc?.is_file

  return (
    <>
      <div>
        <div
          className="text-[10px] uppercase tracking-wider mb-0.5"
          style={{ color: 'var(--text-faint)' }}
        >
          {label}
        </div>
        {!doc ? (
          <div
            className="text-xs px-2 py-1.5 rounded-lg border border-dashed"
            style={{ borderColor: 'var(--border)', color: 'var(--text-faint)' }}
          >
            —
          </div>
        ) : (
          <button
            onClick={handleClick}
            disabled={loading}
            title={isFile ? 'Descargar archivo' : 'Ver contenido'}
            className="w-full flex items-center gap-1.5 px-2 py-1.5 rounded-lg border transition-colors hover:bg-[var(--surface-200)] disabled:opacity-50 text-left"
            style={{
              borderColor: 'var(--border)',
              backgroundColor: 'var(--surface)',
              color: 'var(--text-primary)',
            }}
          >
            {loading ? (
              <Loader2 className="w-3.5 h-3.5 animate-spin text-emerald-500 shrink-0" />
            ) : (
              <FileText
                className={`w-3.5 h-3.5 shrink-0 ${isFile ? 'text-emerald-500' : 'text-brand-500'}`}
              />
            )}
            <span className="text-[11px] truncate flex-1" title={doc.value}>
              {doc.value}
            </span>
            <FileDown className="w-3 h-3 shrink-0 opacity-60" />
          </button>
        )}
      </div>

      {showTextModal && doc && (
        <TextDocModal
          label={label}
          value={doc.value}
          onClose={() => setShowTextModal(false)}
        />
      )}
    </>
  )
}

function TextDocModal({
  label,
  value,
  onClose,
}: {
  label: string
  value: string
  onClose: () => void
}) {
  const [copied, setCopied] = useState(false)

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      toast.error('No se pudo copiar')
    }
  }

  const downloadTxt = () => {
    const blob = new Blob([value], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${label.replace(/[^a-z0-9]+/gi, '_').toLowerCase()}.txt`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }

  return (
    <div
      className="fixed inset-0 z-[9999] flex items-center justify-center p-4"
      style={{ backgroundColor: 'rgba(0,0,0,0.65)', backdropFilter: 'blur(4px)' }}
      onClick={onClose}
    >
      <div
        className="w-full max-w-lg rounded-2xl border shadow-2xl overflow-hidden"
        style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}
        onClick={e => e.stopPropagation()}
      >
        <div
          className="p-4 border-b flex items-center justify-between"
          style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}
        >
          <div>
            <h3 className="font-bold text-sm" style={{ color: 'var(--text-primary)' }}>
              {label}
            </h3>
            <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
              Contenido registrado (referencia / tracking)
            </p>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg hover:bg-[var(--surface-300)]"
            style={{ color: 'var(--text-muted)' }}
          >
            <X className="w-5 h-5" />
          </button>
        </div>
        <div className="p-4">
          <pre
            className="text-sm whitespace-pre-wrap break-all p-3 rounded-lg border"
            style={{
              backgroundColor: 'var(--surface)',
              borderColor: 'var(--border)',
              color: 'var(--text-primary)',
              fontFamily: 'inherit',
            }}
          >
            {value}
          </pre>
        </div>
        <div
          className="p-3 border-t flex items-center justify-end gap-2"
          style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}
        >
          <button onClick={downloadTxt} className="btn-secondary text-sm">
            <FileDown className="w-4 h-4" />
            Descargar .txt
          </button>
          <button onClick={copy} className="btn-primary text-sm">
            {copied ? (
              <>
                <CheckCircle2 className="w-4 h-4" />
                Copiado
              </>
            ) : (
              <>
                <FileText className="w-4 h-4" />
                Copiar
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  )
}

function DocButton({
  label,
  hint,
  icon,
  enabled,
  run,
}: {
  label: string
  hint: string
  icon: React.ReactNode
  enabled: boolean
  run: () => Promise<void>
}) {
  const [loading, setLoading] = useState(false)
  const handle = async () => {
    if (!enabled || loading) return
    setLoading(true)
    try {
      await run()
    } finally {
      setLoading(false)
    }
  }
  return (
    <button
      onClick={handle}
      disabled={!enabled || loading}
      title={enabled ? hint : 'No disponible'}
      className="flex items-center gap-2 px-3 py-2.5 rounded-xl border transition-colors text-left disabled:opacity-40 disabled:cursor-not-allowed hover:bg-[var(--surface-200)]"
      style={{
        borderColor: 'var(--border)',
        backgroundColor: 'var(--surface-100)',
        color: enabled ? 'var(--text-primary)' : 'var(--text-faint)',
      }}
    >
      <span className={enabled ? 'text-emerald-500' : ''} style={enabled ? undefined : { color: 'var(--text-faint)' }}>
        {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : icon}
      </span>
      <div className="flex-1 min-w-0">
        <div className="text-xs font-semibold truncate">{label}</div>
        <div className="text-[10px] truncate" style={{ color: 'var(--text-muted)' }}>
          {enabled ? hint : 'No disponible'}
        </div>
      </div>
    </button>
  )
}

function DiasRestantesBadge({
  dias,
  label,
  compact = false,
}: {
  dias: number | null | undefined
  label?: string
  compact?: boolean
}) {
  if (dias === null || dias === undefined) {
    return compact ? (
      <span style={{ color: 'var(--text-faint)' }}>—</span>
    ) : null
  }
  const sufix = label ? ` · ${label}` : ''
  if (dias < 0) {
    return (
      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-semibold bg-red-500/15 text-red-500">
        <AlertTriangle className="w-3 h-3" />
        Vencido {Math.abs(dias)}d
      </span>
    )
  }
  if (dias === 0) {
    return (
      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-semibold bg-red-500/15 text-red-500">
        <Clock className="w-3 h-3" />
        Hoy{sufix}
      </span>
    )
  }
  let style = 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400'
  if (dias <= 3) style = 'bg-red-500/15 text-red-500'
  else if (dias <= 7) style = 'bg-amber-500/15 text-amber-600 dark:text-amber-400'
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-semibold ${style}`}>
      <Clock className="w-3 h-3" />
      {dias}d{sufix}
    </span>
  )
}

const PIPELINE_SEGMENTS: { key: keyof ProgresoEstados; label: string; color: string }[] = [
  { key: 'pendiente',   label: 'Pendiente',    color: '#94a3b8' },
  { key: 'en_compras',  label: 'En Compras',   color: '#f59e0b' },
  { key: 'en_transito', label: 'En Tránsito',  color: '#8b5cf6' },
  { key: 'en_bodega',   label: 'En Bodega',    color: '#10b981' },
  { key: 'despachado',  label: 'Despachado',   color: '#1a5cf0' },
  { key: 'reclamo',     label: 'Reclamo',      color: '#ef4444' },
]

function PipelineProgress({ oc }: { oc: OcCard }) {
  const total = oc.total_items
  const estados = oc.progreso_estados
  if (!total || total === 0) return null

  const segments = PIPELINE_SEGMENTS.map(s => ({
    ...s,
    count: estados?.[s.key] ?? 0,
  })).filter(s => s.count > 0)

  return (
    <div className="mt-2 space-y-1.5">
      <div className="flex items-center justify-between text-[11px]">
        <span className="uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>
          Estado de Avance
        </span>
        <span
          className={oc.progreso_pct === 100 ? 'text-emerald-500 font-semibold' : ''}
          style={oc.progreso_pct === 100 ? undefined : { color: 'var(--text-muted)' }}
        >
          {oc.items_despachados}/{total} despachados · {oc.progreso_pct}%
        </span>
      </div>
      <div
        className="h-2 rounded-full overflow-hidden flex"
        style={{ backgroundColor: 'var(--surface-300)' }}
      >
        {segments.map(s => (
          <div
            key={s.key}
            title={`${s.label}: ${s.count}`}
            className="h-full transition-all"
            style={{
              width: `${(s.count / total) * 100}%`,
              backgroundColor: s.color,
            }}
          />
        ))}
      </div>
      {/* Legend */}
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-[10px]" style={{ color: 'var(--text-muted)' }}>
        {segments.map(s => (
          <span key={s.key} className="inline-flex items-center gap-1">
            <span
              className="w-2 h-2 rounded-sm shrink-0"
              style={{ backgroundColor: s.color }}
            />
            {s.label} ({s.count})
          </span>
        ))}
      </div>
    </div>
  )
}

function ItemEstadoBadge({ estado, reclamo }: { estado: string; reclamo: boolean }) {
  if (reclamo) {
    return <span className="text-xs px-2 py-0.5 rounded-full font-semibold bg-red-500/15 text-red-500">Reclamo</span>
  }
  const map: Record<string, { label: string; color: string }> = {
    en_bodega: { label: 'En Bodega', color: 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400' },
    despachado: { label: 'Despachado', color: 'bg-blue-500/15 text-blue-600 dark:text-blue-400' },
    embarcado: { label: 'En Tránsito', color: 'bg-amber-500/15 text-amber-600 dark:text-amber-400' },
    reclamo_proveedor: { label: 'Reclamo', color: 'bg-red-500/15 text-red-500' },
  }
  const cfg = map[estado] ?? { label: estado, color: 'bg-slate-500/15 text-slate-500' }
  return <span className={`text-xs px-2 py-0.5 rounded-full font-semibold ${cfg.color}`}>{cfg.label}</span>
}

function DespachoEstadoBadge({ estado }: { estado: string }) {
  const map: Record<string, { label: string; color: string }> = {
    en_preparacion: { label: 'En Preparación', color: 'bg-amber-500/15 text-amber-600 dark:text-amber-400' },
    despachado: { label: 'Despachado', color: 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400' },
    anulado: { label: 'Anulado', color: 'bg-slate-500/15 text-slate-500' },
  }
  const cfg = map[estado] ?? { label: estado, color: 'bg-slate-500/15 text-slate-500' }
  return <span className={`text-xs px-2 py-0.5 rounded-full font-semibold ${cfg.color}`}>{cfg.label}</span>
}

function CrearDespachoModal({ oc, onClose, onCreated }: any) {
  // Este modal es el PASO 1 del despacho: definir QUÉ se despacha (ítems + a quién).
  // Los datos de transporte (transportista, N° de expedición) y el N° de guía NO se
  // piden aquí a propósito: la guía de despacho SII se emite en el paso 2 y su folio
  // lo asigna el SII; el transportista se agrega en el paso 3, ya con la guía emitida.
  // EXCEPCIÓN DELIBERADA: el N° de BULTO sí se pide en este paso 1, porque no es un
  // dato de transporte sino un hecho físico del EMPAQUE — el operador rotula la caja
  // mientras arma el despacho, con la caja en la mano.
  // Ver EditarDespachoModal (paso 3) y EmitirGuiaSIIModal (paso 2).
  const qc = useQueryClient()
  const [contacto, setContacto] = useState(oc.contacto || '')
  const [direccion, setDireccion] = useState(oc.direccion || '')
  const [observaciones, setObservaciones] = useState('')
  const [bulto, setBulto] = useState('')
  // Cantidad por línea como TEXTO mientras se edita (precedente del MISMO
  // archivo: FirmarGuiaModal.qtys — «el operador la edita a medias»). Un input
  // numérico controlado con estado numérico se COME el punto decimal: al teclear
  // «2.» el value del DOM queda "" (badInput), Number('')||0→0→clamp→1 y React
  // repinta borrando el buffer. Clamps y validación van al ENVIAR, no por tecla.
  const [selectedItems, setSelectedItems] = useState<Record<number, string>>({})
  // ── Picking: buscador local + paso de resumen dentro del mismo modal ──
  const [busqueda, setBusqueda] = useState('')
  const [ocultarMarcadas, setOcultarMarcadas] = useState(false)
  const [mostrarResumen, setMostrarResumen] = useState(false)
  const buscadorRef = useRef<HTMLInputElement | null>(null)
  // Refs a los inputs de cantidad: el ciclo del operador es caja en mano → buscar
  // → Enter → cantidad → Enter → siguiente caja, sin soltar el teclado.
  const qtyRefs = useRef<Record<number, HTMLInputElement | null>>({})

  // esDespachable (0.001) y no `> 0`: una línea decimal despachada en tandas
  // deja residuos binarios (0.2 + 0.7 de 0.9 → 1.1e-16) que con `> 0` se
  // ofrecían como cupo, precargaban «1.1102230246251565e-16» en la cantidad y
  // dejaban crear un despacho que la guía 52 nunca puede emitir.
  const disponibles = useMemo(
    () => oc.items.filter((it: ItemRow) => esDespachable(it.qty_disponible)),
    [oc.items]
  )

  // Filtro local INSTANTÁNEO, sin debounce: es un array en memoria, no el servidor.
  const resultado = useMemo(
    () => filtrarItems<ItemRow>(disponibles, busqueda),
    [disponibles, busqueda]
  )
  const hayBusqueda = busqueda.trim() !== ''
  // La búsqueda le GANA al toggle "Ocultar marcadas": con texto en la caja se busca
  // sobre TODAS las líneas (el toggle no puede esconderle al operador lo que busca).
  const visibles = useMemo(() => {
    if (hayBusqueda || !ocultarMarcadas) return resultado.matches
    return resultado.matches.filter(m => selectedItems[m.item.id] === undefined)
  }, [resultado, hayBusqueda, ocultarMarcadas, selectedItems])

  // Cuando la búsqueda no pega en NINGUNA línea despachable, se distingue si el
  // repuesto sí existe en la OC pero quedó oculto por el filtro qty_disponible>0
  // (ya despachado o aún no recibido): decirle "no está en esta OC" a algo que
  // sí está sería mentirle al operador.
  const enOcPeroSinDisponible = useMemo(() => {
    if (!hayBusqueda || resultado.matches.length > 0) return false
    return filtrarItems<ItemRow>(oc.items as ItemRow[], busqueda).matches.length > 0
  }, [hayBusqueda, resultado.matches.length, oc.items, busqueda])

  // El detalle de la OC se REFRESCA bajo el modal abierto (el padre sincroniza
  // `modalOc` cuando llega el cupo real tras un 400 por carrera). Una línea YA
  // MARCADA puede perder su cupo en ese refresco: sale de `disponibles`, así que
  // deja de renderizarse Y de validarse, pero SIGUE en selectedItems — o sea que
  // viajaría en el payload y el backend la rebotaría con 400 para siempre, sin que
  // el operador pueda verla ni desmarcarla. Se detectan acá para mostrarlas y
  // bloquear el envío con un camino de salida de un clic.
  const marcadasSinCupo = useMemo(() => {
    const porId = new Map<number, ItemRow>(
      (oc.items as ItemRow[]).map((it: ItemRow) => [it.id, it])
    )
    return Object.keys(selectedItems)
      .map(Number)
      .map(id => ({ id, it: porId.get(id) }))
      // COMPLEMENTO EXACTO de `disponibles`: mismo predicado (esDespachable), o
      // el invariante «o está en disponibles o está en marcadasSinCupo» se rompe
      // en la franja 0 < qty <= 0.001 y la línea desaparece de las dos listas.
      .filter(({ it }) => !it || !esDespachable(it.qty_disponible))
  }, [selectedItems, oc.items])

  const quitarSinCupo = () => {
    setSelectedItems(prev => {
      const copy = { ...prev }
      for (const { id } of marcadasSinCupo) delete copy[id]
      return copy
    })
    // Lo que sobrevive es exactamente `lineasQueVan` (la selección CON cupo). Si
    // no queda nada, se vuelve al picking: dejar al operador en un resumen vacío
    // con el botón apagado es el mismo callejón sin salida, un paso después.
    if (mostrarResumen && lineasQueVan.length === 0) setMostrarResumen(false)
    toast.success('Se quitaron de la selección las líneas sin cupo.')
  }

  // Vista NUMÉRICA tolerante de la selección (Number(v)||0: un texto a medio
  // tipear vale 0): solo para contadores y resumen — el payload valida en serio
  // al enviar. Mismas claves que selectedItems, así los conteos no mienten.
  const seleccionNumerica = useMemo(() => {
    const out: Record<number, number> = {}
    for (const [id, v] of Object.entries(selectedItems)) out[Number(id)] = Number(v) || 0
    return out
  }, [selectedItems])

  // Contadores SIEMPRE sobre la selección COMPLETA, nunca sobre lo filtrado.
  const contadores = contarSeleccion(seleccionNumerica, disponibles.length)
  const ocultas = contarMarcadasOcultas(seleccionNumerica, visibles.map(m => m.item.id))

  // Tope de la vía «SII gratuito»: se avisa AQUÍ, mientras dividir todavía es
  // gratis. Después de crear el despacho, dividir cuesta anularlo (guía) o es
  // derechamente imposible (factura) — hoy el rechazo llega recién al emitir.
  const topeSii = oc.max_lineas_sii_gratuito
  // Se cuenta contra `disponibles` (los ids CON cupo): una línea marcada que
  // perdió su cupo (marcadasSinCupo) no va a viajar en ningún documento — el
  // envío está bloqueado hasta quitarla — y sumarla corría el aviso del tope
  // justo en el borde 10/11 y lo descuadraba del «M líneas» del resumen.
  const lineasQueViajan = contarLineasQueViajan(seleccionNumerica, disponibles.map((it: ItemRow) => it.id))
  const excedeTopeSii = typeof topeSii === 'number' && topeSii > 0 && lineasQueViajan > topeSii

  const toggleItem = (it: ItemRow) => {
    setSelectedItems(prev => {
      const copy = { ...prev }
      if (copy[it.id] !== undefined) {
        delete copy[it.id]
      } else {
        // El disponible se precarga como STRING (el estado es texto).
        copy[it.id] = String(it.qty_disponible)
      }
      return copy
    })
  }

  // Guarda el TEXTO tal cual se teclea: nada de clamps por tecla (ver el
  // comentario de selectedItems — el clamp inmediato se comía el punto decimal).
  const updateQty = (id: number, value: string) => {
    setSelectedItems(prev => ({ ...prev, [id]: value }))
  }

  // En blur, si lo tipeado es un número LIMPIO fuera de rango, se normaliza al
  // rango legal (mín 1, tope disponible). Lo ilegible se deja tal cual: la
  // validación del envío lo nombra — corregirlo en silencio escondería el typo.
  const normalizarQtyEnBlur = (it: ItemRow) => {
    setSelectedItems(prev => {
      const raw = (prev[it.id] ?? '').trim()
      const v = Number(raw)
      if (raw === '' || !Number.isFinite(v)) return prev
      // Mínimo legal de la LÍNEA: 1, salvo que el propio disponible sea menor —
      // un remanente fraccionario (despachados 2 de 2.5 m → quedan 0.5) es válido
      // para el backend (solo exige > 0) y exigir 1 lo dejaba infacturable aquí.
      const minLegal = Math.min(1, it.qty_disponible)
      const legal = Math.min(Math.max(v, minLegal), it.qty_disponible)
      if (legal === v) return prev
      return { ...prev, [it.id]: String(legal) }
    })
  }

  const enfocarCantidad = (id: number) => {
    // El input de cantidad recién se monta al marcar la línea: se espera al
    // próximo frame. select() = pisar la cantidad precargada es CONSCIENTE
    // (lo primero que teclee el operador la reemplaza entera).
    requestAnimationFrame(() => {
      const el = qtyRefs.current[id]
      if (el) {
        el.focus()
        el.select()
      }
    })
  }

  // Handlers de Enter LOCALES al buscador y a los inputs de cantidad (onKeyDown).
  // JAMÁS un listener global del modal: el Enter del resto de los campos (bulto,
  // destinatario, observaciones) debe seguir inerte — acá no hay <form> a propósito.
  const onBuscadorKeyDown = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Escape') {
      // PRIMERA capa de Esc: limpiar la búsqueda. Esc JAMÁS cierra el modal.
      e.preventDefault()
      e.stopPropagation()
      setBusqueda('')
      return
    }
    if (e.key !== 'Enter') return
    e.preventDefault()
    // Enter solo actúa con UNA coincidencia. Con varias (líneas partidas que
    // comparten n° de parte) JAMÁS se auto-elige: el operador clickea la línea.
    if (!hayBusqueda || resultado.matches.length !== 1) return
    const it = resultado.matches[0].item
    const qtyActual = selectedItems[it.id]
    if (qtyActual !== undefined) {
      // Re-búsqueda de una línea ya marcada: avisar y llevar el foco a su
      // cantidad. Se muestra el TEXTO tal cual (puede estar a medio editar).
      toast(`Ya marcada con ${qtyActual.trim() || 'cantidad vacía'}`)
      enfocarCantidad(it.id)
      return
    }
    setSelectedItems(prev => ({ ...prev, [it.id]: String(it.qty_disponible) }))
    enfocarCantidad(it.id)
  }

  const onQtyKeyDown = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (e.key !== 'Enter') return
    e.preventDefault()
    // Cierra el ciclo de esta caja: limpiar la búsqueda y devolver el foco al
    // buscador, listo para teclear la siguiente caja.
    setBusqueda('')
    buscadorRef.current?.focus()
  }

  // Cierre seguro: con líneas marcadas, overlay / X / Cancelar piden confirmación.
  // window.confirm basta: el archivo no tiene un patrón propio de confirmación.
  const pedirCierre = () => {
    const n = contadores.lineasMarcadas
    // El trabajo TIPEADO también se pierde al cerrar: bulto / observaciones /
    // destinatario editados cuentan aunque no haya líneas marcadas. Contacto y
    // dirección se comparan contra lo PRECARGADO de la OC (cerrar sin tocar
    // nada no debe preguntar).
    const hayTipeo =
      bulto.trim() !== '' ||
      observaciones.trim() !== '' ||
      contacto !== (oc.contacto || '') ||
      direccion !== (oc.direccion || '')
    if (n > 0) {
      if (!window.confirm(`Tienes ${n} línea${n === 1 ? '' : 's'} marcada${n === 1 ? '' : 's'}, ¿descartar?`)) return
    } else if (hayTipeo) {
      if (!window.confirm('Tienes datos escritos sin guardar (bulto, observaciones o destinatario), ¿descartar?')) return
    }
    onClose()
  }

  // Guard SÍNCRONO anti doble-click: isPending recién llega en el re-render, y
  // dos clicks en la misma ráfaga alcanzaban a disparar dos POST (dos despachos
  // gemelos). El ref corta el segundo ANTES del re-render; se libera en error
  // (en éxito el modal se cierra y el ref muere con él).
  const enviandoRef = useRef(false)

  const createMut = useMutation({
    mutationFn: () =>
      despachosAPI.create({
        oc_cliente_id: oc.id,
        // transportista / numero_guia / numero_expedicion se omiten a propósito:
        // el backend los acepta nulos y se completan en los pasos 2 y 3 del flujo.
        contacto_destinatario: contacto || null,
        direccion_entrega: direccion || null,
        observaciones: observaciones || null,
        // Contrato backend 2026-08-25: string ≤50 o null ("" → NULL en el servidor).
        bulto_numero: bulto.trim() || null,
        // La conversión texto→número vive AQUÍ, ya validada por enviarCrear.
        items: Object.entries(selectedItems).map(([id, qty]) => ({
          item_cotizacion_id: Number(id),
          qty_despachada: Number(qty),
        })),
      }),
    onSuccess: (data: any) => {
      toast.success(`Despacho ${data.numero_despacho} creado. Ahora emite la guía SII, agrega el transportista y confirma el despacho.`)
      onCreated()
    },
    onError: (e: any) => {
      enviandoRef.current = false
      toast.error(e?.response?.data?.detail || 'Error al crear')
      // Un 400 acá casi siempre es DISPONIBLE VIEJO (otro usuario tomó el cupo):
      // sin refrescar, el modal seguía mostrando el disponible de antes y el
      // reintento rebotaba igual. Invalidar ['despachos'] trae detalle y listado
      // frescos para que el operador vea contra qué está eligiendo.
      qc.invalidateQueries({ queryKey: ['despachos'] })
    },
  })

  // Validación FINAL de cantidades al ENVIAR — las reglas de siempre: mín 1,
  // tope disponible, decimales legales (metros, kilos). Es la contraparte del
  // estado-texto: acá (y no por tecla) se rechaza lo ilegible con nombre de línea.
  const enviarCrear = () => {
    if (enviandoRef.current) return
    // Primero lo invisible: una marcada sin cupo no la ve `disponibles` y viajaría
    // igual en el payload (rebote 400 eterno y sin causa visible).
    if (marcadasSinCupo.length > 0) {
      const nombres = marcadasSinCupo
        .map(({ id, it }) => it?.numero_parte || `ítem ${id}`)
        .join(', ')
      toast.error(
        `${nombres}: ya no tienen cupo disponible (otro despacho las tomó). ` +
        'Usa el botón «Quitar de la selección» del aviso ámbar de arriba.'
      )
      return
    }
    const mala = disponibles.find((it: ItemRow) => {
      const raw = selectedItems[it.id]
      if (raw === undefined) return false
      const v = Number(raw.trim())
      // Mismo mínimo legal que el blur: un remanente fraccionario < 1 es válido.
      const minLegal = Math.min(1, it.qty_disponible)
      return raw.trim() === '' || !Number.isFinite(v) || v < minLegal || v > it.qty_disponible
    })
    if (mala) {
      toast.error(
        `${mala.numero_parte}: la cantidad debe ser un número entre ` +
        `${fmtQtyPicking(Math.min(1, mala.qty_disponible))} y ${fmtQtyPicking(mala.qty_disponible)}.`
      )
      return
    }
    enviandoRef.current = true
    createMut.mutate()
  }

  // Para el paso de resumen: lo que va y lo que QUEDA PENDIENTE (disponible que no
  // se incluyó en este despacho). Jamás la palabra "faltante" en esta UI: ya
  // significa otra cosa en la firma de la guía (unidades que no llegaron).
  const lineasQueVan = useMemo(
    () => disponibles.filter((it: ItemRow) => selectedItems[it.id] !== undefined),
    [disponibles, selectedItems]
  )
  // Unidades de ESE MISMO conjunto. El paso de resumen se cuenta sobre lo que la
  // tabla lista y no sobre `contadores` (selección completa): con una línea que
  // perdió el cupo, la cabecera decía «3 líneas, 14 unidades» contra una tabla
  // «Va en este despacho (2)», y el botón prometía unidades que el documento no
  // iba a llevar. Los contadores CRUDOS se quedan intactos donde deben estar
  // (picking, «Ocultar marcadas» y la confirmación al cerrar).
  const unidadesQueVan = useMemo(
    () => lineasQueVan.reduce((s: number, it: ItemRow) => s + (seleccionNumerica[it.id] ?? 0), 0),
    [lineasQueVan, seleccionNumerica]
  )
  const lineasPendientes = useMemo(
    () =>
      (disponibles as ItemRow[])
        // Vista numérica tolerante: un texto a medio tipear resta 0 (el envío
        // real igual lo valida antes de crear).
        .map(it => ({ it, pendiente: it.qty_disponible - (seleccionNumerica[it.id] ?? 0) }))
        // esDespachable y no `> 0` (5º punto de decisión por línea de esta
        // pantalla, ahora alineado con los otros cuatro): la resta de decimales
        // legales deja residuos binarios —2.5 − 2.5 en dos tandas da 4.16e-17— y
        // con `> 0` la banda «Queda pendiente» llegaba a imprimir
        // «4.16e-17 x 7T1997», o sea a inventar un remanente que no existe.
        .filter(x => esDespachable(x.pendiente)),
    [disponibles, seleccionNumerica]
  )

  // Gemelas del split: dos líneas con el MISMO n° de parte son píxel-idénticas.
  // Cuando 2+ líneas VISIBLES comparten el n° colapsado (7T1997 ≡ 7T-1997),
  // cada una gana su ordinal «línea i de n» por orden de aparición, para que el
  // operador sepa cuál de las dos está clickeando.
  const ordinalGemelas = useMemo(() => {
    const totales = new Map<string, number>()
    for (const m of visibles) {
      const k = colapsar(m.item.numero_parte || '')
      totales.set(k, (totales.get(k) ?? 0) + 1)
    }
    const vistos = new Map<string, number>()
    const out = new Map<number, { i: number; n: number }>()
    for (const m of visibles) {
      const k = colapsar(m.item.numero_parte || '')
      const n = totales.get(k) ?? 1
      if (n < 2) continue
      const i = (vistos.get(k) ?? 0) + 1
      vistos.set(k, i)
      out.set(m.item.id, { i, n })
    }
    return out
  }, [visibles])

  return (
    <div
      className="fixed inset-0 flex items-center justify-center z-50 p-4"
      style={{ backgroundColor: 'rgba(0,0,0,0.65)', backdropFilter: 'blur(4px)' }}
      onClick={pedirCierre}
    >
      <div
        className="max-w-3xl w-full max-h-[90vh] overflow-hidden flex flex-col rounded-2xl border shadow-2xl"
        style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}
        onClick={e => e.stopPropagation()}
      >
        <div
          className="p-4 border-b flex items-center justify-between"
          style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}
        >
          <div>
            <h2 className="text-lg font-bold" style={{ color: 'var(--text-primary)' }}>
              {mostrarResumen ? 'Revisar despacho' : 'Crear despacho'}
            </h2>
            <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
              {/* numero_oc nullable: sin el fallback la cabecera del modal en que
                  se CREA el despacho decía «OC  · CLIENTE». Misma convención
                  «OC #<id>» del modal de bultos y del resto de la pantalla. */}
              OC {oc.numero_oc || `#${oc.id}`} · {oc.cliente}
            </p>
          </div>
          <button
            onClick={pedirCierre}
            className="p-1.5 rounded-lg hover:bg-[var(--surface-300)]"
            style={{ color: 'var(--text-muted)' }}
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Aviso de líneas marcadas que PERDIERON su cupo mientras el modal estaba
            abierto (el padre refrescó el detalle tras un 400 por carrera). No se
            renderizan en la lista porque salieron de `disponibles`, así que sin
            este aviso el operador no tendría cómo verlas ni desmarcarlas.
            BANDA PROPIA bajo la cabecera, FUERA de los dos pasos: el guard que la
            necesita dispara desde «Crear despacho», que vive en el RESUMEN — con
            el aviso encerrado en el picking, el toast nombraba las partes y el
            botón para quitarlas era invisible (callejón sin salida).
            role="alert": se anuncia solo. */}
        {marcadasSinCupo.length > 0 && (
          <div
            className="px-4 py-3 border-b"
            style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}
          >
            <div
              role="alert"
              className="flex items-start justify-between gap-2 flex-wrap rounded-lg px-3 py-2 text-xs"
              style={{ background: 'rgba(245,158,11,0.12)', border: '1px solid rgba(245,158,11,0.35)' }}
            >
              <div style={{ color: 'var(--text-primary)' }}>
                <span className="font-semibold">
                  {marcadasSinCupo.length} línea{marcadasSinCupo.length === 1 ? '' : 's'} que marcaste ya no tiene{marcadasSinCupo.length === 1 ? '' : 'n'} cupo
                </span>
                {' '}(otro despacho la{marcadasSinCupo.length === 1 ? '' : 's'} tomó):{' '}
                <span className="font-mono">
                  {marcadasSinCupo.map(({ id, it }) => it?.numero_parte || `ítem ${id}`).join(', ')}
                </span>
              </div>
              <button
                type="button"
                onClick={quitarSinCupo}
                className="px-2 py-1 rounded-md font-semibold whitespace-nowrap"
                style={{ background: 'rgba(245,158,11,0.25)', color: 'var(--text-primary)' }}
              >
                Quitar de la selección
              </button>
            </div>
          </div>
        )}

        {/* Tope de 10 ítems de la vía «SII gratuito»: los 3 únicos documentos
            rechazados en la historia real de esta cuenta fueron por esto. El
            backend ya lo avisa en el «verificar» previo, pero ahí el despacho YA
            existe y dividirlo cuesta anularlo. Acá el operador todavía puede
            sacar líneas gratis. INFORMATIVO, no bloquea: el tope es de la vía de
            emisión electrónica, no del despacho (una guía en papel no lo tiene). */}
        {excedeTopeSii && (
          <div
            role="status"
            className="px-4 py-3 border-b text-xs"
            style={{ background: 'rgba(245,158,11,0.10)', borderColor: 'var(--border)', color: 'var(--text-primary)' }}
          >
            <span className="font-semibold">
              {lineasQueViajan} ítems marcados — la emisión al SII acepta hasta {topeSii} por documento.
            </span>{' '}
            <span style={{ color: 'var(--text-muted)' }}>
              Si vas a emitir la guía electrónica, arma dos despachos de {topeSii} o menos:
              dividirlo después obliga a anular este despacho.
            </span>
          </div>
        )}

        {!mostrarResumen && (
          /* Buscador de picking + contador — FUERA del scroll, siempre a la vista:
             el operador cuenta piezas con una caja en la mano y teclea su número. */
          <div
            className="px-4 py-3 border-b space-y-2"
            style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}
          >
            <div className="relative">
              <Search
                className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 pointer-events-none"
                style={{ color: 'var(--text-faint)' }}
              />
              <input
                ref={buscadorRef}
                autoFocus
                value={busqueda}
                onChange={e => setBusqueda(e.target.value)}
                onKeyDown={onBuscadorKeyDown}
                /* El placeholder es contrato: el filtro busca EXACTAMENTE esos dos campos. */
                placeholder="Buscar por N° de parte o descripción"
                className="input pl-9 pr-8"
              />
              {busqueda !== '' && (
                <button
                  onClick={() => {
                    setBusqueda('')
                    buscadorRef.current?.focus()
                  }}
                  className="absolute right-2 top-1/2 -translate-y-1/2 p-1 rounded hover:bg-[var(--surface-300)]"
                  style={{ color: 'var(--text-faint)' }}
                  title="Limpiar búsqueda (Esc)"
                >
                  <X className="w-4 h-4" />
                </button>
              )}
            </div>
            {hayBusqueda && resultado.huboColapsado && (
              <div className="text-[11px]" style={{ color: 'var(--text-muted)' }}>
                también busqué <span className="font-mono font-semibold">{resultado.queryColapsada}</span>
              </div>
            )}
            {/* Contador fijo: SIEMPRE sobre la selección completa, no lo filtrado.
                aria-live: el lector de pantalla anuncia cada marca sin mover el foco. */}
            <div className="flex items-center justify-between gap-2 flex-wrap text-xs">
              <div aria-live="polite" className="font-semibold" style={{ color: 'var(--text-primary)' }}>
                Marcadas {contadores.lineasMarcadas} de {contadores.lineasTotal} líneas
                {' · '}{fmtQtyPicking(contadores.unidadesTotales)} unidades
                {ocultas > 0 && (
                  <span className="font-normal" style={{ color: 'var(--text-muted)' }}>
                    {' '}({ocultas} marcada{ocultas === 1 ? '' : 's'} oculta{ocultas === 1 ? '' : 's'} por el filtro)
                  </span>
                )}
              </div>
              <label
                className="flex items-center gap-1.5 cursor-pointer select-none"
                style={{ color: 'var(--text-muted)' }}
                title="Con texto en la búsqueda se muestra TODO lo que coincida, marcado o no"
              >
                <input
                  type="checkbox"
                  checked={ocultarMarcadas}
                  onChange={e => setOcultarMarcadas(e.target.checked)}
                  className="w-3.5 h-3.5 accent-brand-500"
                />
                Ocultar marcadas
              </label>
            </div>
          </div>
        )}

        {mostrarResumen ? (
          /* ── Paso liviano de RESUMEN, dentro del mismo modal ── */
          <div className="p-4 space-y-4 overflow-y-auto flex-1">
            <div
              className="p-3 rounded-xl border text-sm"
              style={{ backgroundColor: 'var(--surface-200)', borderColor: 'var(--border)', color: 'var(--text-primary)' }}
            >
              {/* MISMO conjunto que la tabla de abajo (lineasQueVan): la cabecera
                  no puede decir «3 líneas» sobre una lista de 2. */}
              Vas a despachar <b>{lineasQueVan.length} línea{lineasQueVan.length === 1 ? '' : 's'}</b>,{' '}
              <b>{fmtQtyPicking(unidadesQueVan)} unidades</b>
              {bulto.trim() !== '' && <> · Bulto N° <b>{bulto.trim()}</b></>}
            </div>

            <div>
              <div
                className="text-xs uppercase tracking-wider mb-2 font-semibold"
                style={{ color: 'var(--text-faint)' }}
              >
                Va en este despacho ({lineasQueVan.length})
              </div>
              <div className="border rounded-xl overflow-hidden" style={{ borderColor: 'var(--border)' }}>
                {lineasQueVan.map((it: ItemRow) => (
                  <div
                    key={it.id}
                    className="p-2.5 flex items-center gap-3 border-b last:border-0 text-sm"
                    style={{ borderColor: 'var(--border)' }}
                  >
                    <span className="font-semibold w-14 text-right shrink-0" style={{ color: 'var(--text-primary)' }}>
                      {fmtQtyPicking(seleccionNumerica[it.id] ?? 0)} x
                    </span>
                    <span className="font-mono text-xs text-brand-500 font-semibold shrink-0">{it.numero_parte}</span>
                    <span className="truncate" style={{ color: 'var(--text-muted)' }}>{it.descripcion}</span>
                  </div>
                ))}
              </div>
            </div>

            {lineasPendientes.length > 0 && (
              /* Lo que QUEDA PENDIENTE de la OC (disponible no incluido en este
                 despacho). Nunca "faltante": esa palabra ya es de la firma de guía. */
              <div>
                <div
                  className="text-xs uppercase tracking-wider mb-2 font-semibold"
                  style={{ color: 'var(--text-faint)' }}
                >
                  Queda pendiente (no va en este despacho)
                </div>
                <div
                  className="border rounded-xl overflow-hidden"
                  style={{ borderColor: 'rgba(245, 158, 11, 0.4)', backgroundColor: 'rgba(245, 158, 11, 0.05)' }}
                >
                  {lineasPendientes.map(({ it, pendiente }) => (
                    <div
                      key={it.id}
                      className="p-2.5 flex items-center gap-3 border-b last:border-0 text-sm"
                      style={{ borderColor: 'rgba(245, 158, 11, 0.25)' }}
                    >
                      <span className="font-semibold w-14 text-right shrink-0" style={{ color: 'var(--text-primary)' }}>
                        {fmtQtyPicking(pendiente)} x
                      </span>
                      <span className="font-mono text-xs text-brand-500 font-semibold shrink-0">{it.numero_parte}</span>
                      <span className="truncate" style={{ color: 'var(--text-muted)' }}>{it.descripcion}</span>
                    </div>
                  ))}
                </div>
                <p className="text-[11px] mt-1" style={{ color: 'var(--text-muted)' }}>
                  Lo pendiente sigue disponible para un próximo despacho de esta OC.
                </p>
              </div>
            )}
          </div>
        ) : (
        <div className="p-4 space-y-4 overflow-y-auto flex-1">
          {/* Guía del flujo: este modal es solo el paso 1 de 4. */}
          <div
            className="flex items-start gap-2 p-3 rounded-xl border text-xs"
            style={{ backgroundColor: 'var(--surface-200)', borderColor: 'var(--border)', color: 'var(--text-muted)' }}
          >
            <Receipt className="w-4 h-4 shrink-0 mt-0.5 text-blue-500" />
            <span>
              <b style={{ color: 'var(--text-primary)' }}>Paso 1 de 4:</b> elige qué se despacha.
              Luego, en la fila del despacho: <b style={{ color: 'var(--text-primary)' }}>emites la guía SII</b> (el
              folio lo asigna el SII), <b style={{ color: 'var(--text-primary)' }}>agregas el transportista</b> y
              <b style={{ color: 'var(--text-primary)' }}> confirmas el despacho</b>.
            </span>
          </div>

          {/* Destinatario (la guía SII usa la ficha del cliente en Wasabil; estos
              datos son de referencia interna del despacho y vienen desde la OC). */}
          <div className="grid grid-cols-2 gap-3">
            <Input
              label="Contacto Destinatario"
              value={contacto}
              onChange={setContacto}
            />
            <Input
              label="Dirección de Entrega"
              value={direccion}
              onChange={setDireccion}
            />
          </div>
          <div>
            <label
              className="block text-xs font-semibold uppercase tracking-wider mb-1.5"
              style={{ color: 'var(--text-faint)' }}
            >
              Observaciones
            </label>
            <textarea
              value={observaciones}
              onChange={e => setObservaciones(e.target.value)}
              rows={2}
              className="input resize-none"
              placeholder="Opcional..."
            />
          </div>

          {/* Items disponibles */}
          <div>
            <div
              className="text-xs uppercase tracking-wider mb-2 font-semibold"
              style={{ color: 'var(--text-faint)' }}
            >
              Items disponibles ({hayBusqueda ? `${visibles.length} de ${disponibles.length}` : disponibles.length})
            </div>
            {visibles.length === 0 ? (
              hayBusqueda ? (
                /* CERO coincidencias con búsqueda activa: mensaje fuerte, y se
                   distingue el caso "sí está en la OC pero sin disponible". */
                <div className="text-center py-6 space-y-1">
                  <div className="text-sm font-bold" style={{ color: 'var(--text-primary)' }}>
                    {enOcPeroSinDisponible
                      ? 'Está en la OC, pero sin cantidad disponible para despachar'
                      : 'No está en esta OC'}
                  </div>
                  <div className="text-xs" style={{ color: 'var(--text-muted)' }}>
                    {enOcPeroSinDisponible
                      ? 'La línea existe, pero su disponible es 0 (ya despachada o aún no recibida en bodega).'
                      : `Ninguna línea despachable coincide con «${busqueda.trim()}».`}
                  </div>
                </div>
              ) : disponibles.length === 0 ? (
                <div className="text-center py-4" style={{ color: 'var(--text-faint)' }}>
                  No hay items disponibles para despacho
                </div>
              ) : (
                /* Sin búsqueda y con líneas: todo quedó oculto por el toggle. */
                <div className="text-center py-4 text-sm" style={{ color: 'var(--text-muted)' }}>
                  {contadores.lineasMarcadas === 1
                    ? 'La línea está marcada y oculta por «Ocultar marcadas».'
                    : `Las ${contadores.lineasMarcadas} líneas están marcadas y ocultas por «Ocultar marcadas».`}
                </div>
              )
            ) : (
              <div className="border rounded-xl overflow-hidden" style={{ borderColor: 'var(--border)' }}>
                {visibles.map((m: PickingMatch<ItemRow>) => {
                  const it = m.item
                  const selected = selectedItems[it.id] !== undefined
                  const unica = hayBusqueda && resultado.matches.length === 1
                  const gemela = ordinalGemelas.get(it.id)
                  // Iluminación: UNA coincidencia = fondo destacado + badge textual
                  // «Enter para marcar» (accesible: el color solo no le habla al
                  // lector de pantalla ni al daltónico); varias = iluminación suave
                  // (JAMÁS auto-elegir: click manual);
                  // marcada = cara verde con borde (gana sobre la iluminación).
                  const fondo = selected
                    ? 'rgba(16, 185, 129, 0.10)'
                    : hayBusqueda
                      ? unica
                        ? 'rgba(245, 158, 11, 0.18)'
                        : 'rgba(245, 158, 11, 0.06)'
                      : 'transparent'
                  return (
                    <div
                      key={it.id}
                      className="p-3 flex items-center gap-3 border-b last:border-0 transition-colors"
                      style={{
                        borderColor: 'var(--border)',
                        backgroundColor: fondo,
                        // inset y no borderLeft: el borde no debe correr el layout.
                        boxShadow: selected ? 'inset 3px 0 0 0 rgb(16, 185, 129)' : undefined,
                      }}
                    >
                      <input
                        type="checkbox"
                        checked={selected}
                        onChange={() => toggleItem(it)}
                        className="w-4 h-4 accent-brand-500"
                      />
                      <div className="flex-1 min-w-0">
                        <div className="font-mono text-xs text-brand-500 font-semibold">
                          <CampoFiltrado
                            texto={it.numero_parte}
                            query={busqueda}
                            colapsado={m.porColapsado && m.camposColapsados.includes('numero_parte')}
                          />
                          {gemela && (
                            /* Discriminante de gemelas: sin esto, dos líneas del
                               split con el mismo n° de parte son indistinguibles
                               y el operador marca "la otra" sin darse cuenta. */
                            <span
                              className="ml-1.5 font-sans font-semibold text-[10px] px-1.5 py-0.5 rounded-full bg-slate-500/15"
                              style={{ color: 'var(--text-muted)' }}
                            >
                              línea {gemela.i} de {gemela.n}
                            </span>
                          )}
                        </div>
                        <div className="text-sm truncate" style={{ color: 'var(--text-primary)' }}>
                          <CampoFiltrado
                            texto={it.descripcion}
                            query={busqueda}
                            colapsado={m.porColapsado && m.camposColapsados.includes('descripcion')}
                          />
                        </div>
                        <div className="text-xs" style={{ color: 'var(--text-muted)' }}>{it.marca}</div>
                      </div>
                      {unica && !selected && (
                        /* Aviso TEXTUAL del atajo (no solo el fondo ámbar). */
                        <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded-full bg-amber-500/15 text-amber-600 dark:text-amber-400 shrink-0">
                          Enter para marcar
                        </span>
                      )}
                      {selected && <CheckCircle2 className="w-4 h-4 shrink-0 text-emerald-500" />}
                      {selected ? (
                        <div className="flex flex-col items-end gap-0.5 shrink-0">
                          <input
                            ref={el => { qtyRefs.current[it.id] = el }}
                            type="number"
                            min={1}
                            max={it.qty_disponible}
                            value={selectedItems[it.id]}
                            /* El TEXTO viaja tal cual al estado (ver selectedItems):
                               clamps por tecla se comían el punto decimal. */
                            onChange={e => updateQty(it.id, e.target.value)}
                            onBlur={() => normalizarQtyEnBlur(it)}
                            onKeyDown={onQtyKeyDown}
                            className="input w-20 text-right py-1.5 px-2"
                          />
                          {/* Tope visible: reglas de cantidad intactas (mín 1, decimales
                              legales, tope qty_disponible) — se validan al ENVIAR. */}
                          <span className="text-[10px]" style={{ color: 'var(--text-faint)' }}>
                            máx {fmtQtyPicking(it.qty_disponible)}
                          </span>
                        </div>
                      ) : (
                        <span
                          className="text-sm w-20 text-right shrink-0"
                          style={{ color: 'var(--text-muted)' }}
                        >
                          {/* fmtQtyPicking, igual que el «máx» de la línea marcada:
                              el mismo número no puede imprimirse de dos formas
                              («0.10000000000000009» vs «0.1») en la misma fila. */}
                          /{fmtQtyPicking(it.qty_disponible)} disp.
                        </span>
                      )}
                    </div>
                  )
                })}
              </div>
            )}
          </div>

          {/* Bulto: hecho físico del EMPAQUE (no dato de transporte) — por eso vive
              en el paso 1. Enter acá es INERTE, como en todo el modal (sin <form>). */}
          <Input
            label="Bulto N° (opcional)"
            value={bulto}
            /* Array.from y no slice sobre el string: el tope de 50 se cuenta en
               caracteres reales para no partir un emoji (surrogate) en el borde. */
            onChange={(v: string) => setBulto(Array.from(v).slice(0, 50).join(''))}
            placeholder="Ej: 1, B2, Cajas 2-3"
            hint="Caja en que viaja este despacho (ej: 1, B2, Cajas 2-3)"
          />
        </div>
        )}

        <div
          className="p-4 border-t flex items-center justify-between"
          style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}
        >
          {/* En el RESUMEN el pie cuenta lo que realmente va (mismo conjunto que
              la tabla y la cabecera); en el picking se queda el conteo CRUDO de la
              selección completa, que es su invariante declarado. */}
          <div className="text-sm" style={{ color: 'var(--text-muted)' }}>
            {mostrarResumen ? (
              <>
                {lineasQueVan.length} línea{lineasQueVan.length === 1 ? '' : 's'} marcada{lineasQueVan.length === 1 ? '' : 's'}
                {' · '}{fmtQtyPicking(unidadesQueVan)} unidades
              </>
            ) : (
              <>
                {contadores.lineasMarcadas} línea{contadores.lineasMarcadas === 1 ? '' : 's'} marcada{contadores.lineasMarcadas === 1 ? '' : 's'}
                {' · '}{fmtQtyPicking(contadores.unidadesTotales)} unidades
              </>
            )}
          </div>
          <div className="flex gap-2">
            {mostrarResumen ? (
              <>
                <button onClick={() => setMostrarResumen(false)} className="btn-secondary text-sm">
                  Volver
                </button>
                {/* Con líneas sin cupo el botón se APAGA: el guard de enviarCrear
                    sigue siendo el cinturón, pero el operador dejaba de gastar
                    clics contra un toast. Se apaga con la banda ámbar YA VISIBLE
                    arriba (si no, sería un botón muerto sin causa a la vista). */}
                <button
                  onClick={enviarCrear}
                  disabled={lineasQueVan.length === 0 || marcadasSinCupo.length > 0 || createMut.isPending}
                  className="btn-primary text-sm"
                >
                  <Plus className="w-4 h-4" />
                  {createMut.isPending
                    ? 'Creando...'
                    : `Crear despacho (${fmtQtyPicking(unidadesQueVan)} unidades)`}
                </button>
              </>
            ) : (
              <>
                <button onClick={pedirCierre} className="btn-secondary text-sm">
                  Cancelar
                </button>
                <button
                  onClick={() => setMostrarResumen(true)}
                  disabled={contadores.lineasMarcadas === 0}
                  className="btn-primary text-sm"
                >
                  Revisar y crear <ChevronRight className="w-4 h-4" />
                </button>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

function Input({ label, value, onChange, placeholder, disabled, hint, type = 'text', max }: any) {
  return (
    <div>
      <label
        className="block text-xs font-semibold uppercase tracking-wider mb-1.5"
        style={{ color: 'var(--text-faint)' }}
      >
        {label}
      </label>
      <input
        type={type}
        max={max}
        value={value}
        onChange={e => onChange(e.target.value)}
        placeholder={placeholder}
        disabled={disabled}
        className={`input ${disabled ? 'opacity-60 cursor-not-allowed' : ''}`}
      />
      {hint && (
        <p className="text-[11px] mt-1" style={{ color: 'var(--text-faint)' }}>{hint}</p>
      )}
    </div>
  )
}

// ─── Modal: marcar guía firmada (foto/PDF + firma PARCIAL por ítem) ───────────
// A veces la guía se emitió por ítems que NO llegaron (perdidos en la entrega).
// El caso común (llegó todo) no gana fricción: los ítems vienen pre-tickeados
// completos y el body viaja SIN `items` (byte-igual al camino de siempre). Si
// algo no llegó, el operador destickea o baja la cantidad: eso queda como
// FALTANTE con motivo obligatorio y no se factura por esta guía.
const MOTIVO_FALTANTE_MIN = 5
const MOTIVO_FALTANTE_MAX = 300

function FirmarGuiaModal({
  despacho,
  dteFolio,
  onClose,
  onDone,
}: {
  despacho: DespachoRow
  dteFolio?: string | null
  onClose: () => void
  onDone: () => void
}) {
  // Re-firma: ya está firmada con faltante y se corrigen cantidades/motivo
  // (el backend valida contra lo ya facturado y devuelve 409 si no se puede).
  const esEdicion = !!despacho.guia_firmada
  const [file, setFile] = useState<File | null>(null)
  const [numeroGuia, setNumeroGuia] = useState(esEdicion ? (despacho.numero_guia || '') : '')
  // hoyEnChile() y no toISOString(): en Chile (UTC-3/-4) la fecha UTC ya es
  // "mañana" pasadas las ~21:00 y la firma quedaría con fecha futura. Tampoco el
  // día del navegador: la firma es un hecho del negocio, que ocurre en Chile.
  const [fecha, setFecha] = useState(
    esEdicion && despacho.fecha_firma ? despacho.fecha_firma.slice(0, 10) : hoyEnChile()
  )
  const [saving, setSaving] = useState(false)
  // Errores DENTRO del modal (validación local y 409 del backend): recuadro
  // fijo, no un toast que se esfuma a los 4 segundos.
  const [errores, setErrores] = useState<string[]>([])

  // Ítems del despacho: mismo GET (y misma cache) que el desplegable de la fila.
  // Si el backend previo no manda `items`, el modal se comporta como hoy.
  const { data: detalle, isLoading: cargandoItems, isError: errorItems } = useQuery<DespachoDetalle>({
    queryKey: ['despachos', 'detalle', despacho.id],
    queryFn: () => despachosAPI.get(despacho.id),
  })
  const items = detalle?.items
  const [motivo, setMotivo] = useState('')
  // Cantidad firmada por ítem, como TEXTO (el operador la edita a medias).
  // Se inicializa al llegar los ítems: la firmada actual (re-firma) o completa.
  const [qtys, setQtys] = useState<Record<number, string> | null>(null)
  useEffect(() => {
    if (!items || qtys !== null) return
    const base: Record<number, string> = {}
    for (const it of items) base[it.id] = String(it.qty_firmada ?? it.qty_despachada)
    setQtys(base)
    if (detalle?.faltante_motivo) setMotivo(detalle.faltante_motivo)
  }, [items, qtys, detalle])

  // Mismo candado que EditarDespachoModal (ver folioParaModal): con guía
  // electrónica emitida/en proceso, el N° es (o será) el folio del SII.
  const folioBloqueado = dteFolio !== null && dteFolio !== undefined

  const qtyDe = (it: DespachoItemDetalle): number => {
    const v = Number(qtys?.[it.id] ?? String(it.qty_firmada ?? it.qty_despachada))
    return Number.isFinite(v) ? v : 0
  }
  const totalDespachado = (items ?? []).reduce((s, it) => s + it.qty_despachada, 0)
  const totalFirmado = (items ?? []).reduce((s, it) => s + qtyDe(it), 0)
  const faltante = Math.max(totalDespachado - totalFirmado, 0)

  // Destickear = no llegó nada de la línea (qty 0); re-tickear = completa.
  const toggleItem = (it: DespachoItemDetalle) => {
    setQtys(p => ({ ...(p ?? {}), [it.id]: qtyDe(it) > 0 ? '0' : String(it.qty_despachada) }))
  }

  const submit = async () => {
    // Validación LOCAL antes de mandar nada (los errores quedan en el recuadro).
    const errs: string[] = []
    if (!esEdicion && !file) errs.push('Sube la foto o PDF de la guía firmada.')
    let itemsPayload: FirmaItemPayload[] | undefined
    let motivoPayload: string | undefined
    if (items && items.length > 0 && qtys) {
      let hayFaltante = false
      for (const it of items) {
        const raw = (qtys[it.id] ?? '').trim()
        const v = Number(raw)
        // Una línea INTACTA (v == qty_despachada) siempre es válida, aunque la
        // cantidad despachada sea fraccionaria (2.5 legado): exigirle «entero» a lo
        // que el operador NI TOCÓ bloqueaba firmar el despacho completo (revisión
        // adversarial M2). El check de entero aplica solo a valores editados.
        if (raw === '' || v < 0 || v > it.qty_despachada
            || (v !== it.qty_despachada && !Number.isInteger(v))) {
          errs.push(`${it.numero_parte}: la cantidad firmada debe ser un número entero entre 0 y ${it.qty_despachada} (o la cantidad completa).`)
          continue
        }
        if (v < it.qty_despachada) hayFaltante = true
      }
      if (errs.length === 0 && hayFaltante) {
        const m = motivo.trim()
        if (m.length < MOTIVO_FALTANTE_MIN || m.length > MOTIVO_FALTANTE_MAX) {
          errs.push(`Explica el motivo del faltante (entre ${MOTIVO_FALTANTE_MIN} y ${MOTIVO_FALTANTE_MAX} caracteres).`)
        } else {
          itemsPayload = items.map(it => ({ despacho_item_id: it.id, qty_firmada: Number(qtys[it.id]) }))
          motivoPayload = m
        }
      } else if (esEdicion) {
        // RE-firma con todo completo: el body lleva items EXPLÍCITOS igual
        // (revisión H-2). Sin ellos, «sin items = firma completa» + una precarga
        // nacida de caché vieja podía borrar un faltante real en silencio; con
        // ellos, lo que viaja es EXACTAMENTE lo que el operador tiene en pantalla.
        // El camino byte-igual queda solo para la PRIMERA firma.
        itemsPayload = items.map(it => ({ despacho_item_id: it.id, qty_firmada: Number(qtys[it.id]) }))
      }
      // Primera firma con todo completo → el body NO lleva `items` (byte-igual).
    }
    if (errs.length > 0) {
      setErrores(errs)
      return
    }
    setErrores([])
    setSaving(true)
    try {
      // En la re-firma la foto es opcional: sin archivo nuevo se conserva el actual.
      const archivo = file ? (await despachosAPI.uploadDoc(file)).filename : undefined
      await despachosAPI.firmar(despacho.id, {
        fecha_firma: fecha,
        // Con folio SII, el N° de guía no viaja (el backend además lo rechaza)
        numero_guia: folioBloqueado ? undefined : (numeroGuia || undefined),
        ...(archivo ? { archivo } : {}),
        ...(itemsPayload ? { items: itemsPayload } : {}),
        ...(motivoPayload ? { motivo_faltante: motivoPayload } : {}),
      })
      if (faltante > 0) {
        toast.success(
          `Guía firmada con ${faltante} unidad${faltante === 1 ? '' : 'es'} faltante` +
          ' — el faltante no se facturará por esta guía'
        )
      } else {
        toast.success('Guía firmada — ya se puede facturar en Contabilidad')
      }
      onDone()
    } catch (e: any) {
      // El error del backend (p. ej. el 409 "no puedes bajar de lo ya facturado")
      // se muestra ENTERO en el recuadro del modal.
      const d = e?.response?.data?.detail
      setErrores([typeof d === 'string' ? d : d ? JSON.stringify(d) : 'Error al firmar la guía'])
    } finally {
      setSaving(false)
    }
  }

  return (
    <div
      className="fixed inset-0 flex items-center justify-center z-50 p-4"
      style={{ backgroundColor: 'rgba(0,0,0,0.65)', backdropFilter: 'blur(4px)' }}
      onClick={onClose}
    >
      <div
        className="max-w-lg w-full max-h-[90vh] flex flex-col rounded-2xl border shadow-2xl"
        style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}
        onClick={e => e.stopPropagation()}
      >
        <div
          className="p-4 border-b flex items-center justify-between"
          style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}
        >
          <h2 className="text-lg font-bold" style={{ color: 'var(--text-primary)' }}>
            {esEdicion ? 'Editar firma de la guía' : 'Marcar guía firmada'}
          </h2>
          <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-[var(--surface-300)]" style={{ color: 'var(--text-muted)' }}>
            <X className="w-5 h-5" />
          </button>
        </div>
        <div className="p-4 space-y-4 overflow-y-auto">
          <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
            {esEdicion ? (
              <>Corrige lo que llegó con esta guía. Si subes una foto nueva, reemplaza a la actual;
              si no, se conserva la que ya está.</>
            ) : (
              <>Sube la <b>foto o PDF de la guía firmada</b> por el cliente. Quedará disponible en Contabilidad
              para cobrar (OC + factura + guía firmada).</>
            )}
          </p>
          <div>
            <label className="block text-xs font-semibold uppercase tracking-wider mb-1.5" style={{ color: 'var(--text-faint)' }}>
              Foto / PDF de la guía firmada{esEdicion ? ' (opcional)' : ''}
            </label>
            <label className="flex items-center gap-2 px-3 py-2 rounded-lg border cursor-pointer hover:bg-[var(--surface-200)]" style={{ borderColor: 'var(--border)', color: 'var(--text-muted)' }}>
              <Upload className="w-4 h-4" />
              <span className="text-sm truncate">
                {file ? file.name : esEdicion ? 'Reemplazar archivo… (opcional)' : 'Seleccionar archivo…'}
              </span>
              <input
                type="file"
                accept="image/*,application/pdf"
                onChange={e => setFile(e.target.files?.[0] || null)}
                className="hidden"
              />
            </label>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <Input
              label="N° Guía (opcional)"
              value={folioBloqueado && dteFolio !== 'verificando' && dteFolio !== 'en_emision' ? dteFolio : numeroGuia}
              onChange={setNumeroGuia}
              placeholder="Si falta"
              disabled={folioBloqueado}
              hint={
                dteFolio === 'verificando'
                  ? 'Verificando si este despacho tiene guía electrónica…'
                  : dteFolio === 'en_emision'
                    ? 'Guía electrónica en emisión: el N° quedará fijado por el folio del SII.'
                    : folioBloqueado
                      ? `Guía electrónica emitida al SII (folio ${dteFolio}): el N° no se edita a mano.`
                      : undefined
              }
            />
            <div>
              <label className="block text-xs font-semibold uppercase tracking-wider mb-1.5" style={{ color: 'var(--text-faint)' }}>
                Fecha de firma
              </label>
              <input type="date" value={fecha} onChange={e => setFecha(e.target.value)} className="input" />
            </div>
          </div>

          {/* ¿Qué llegó con esta guía? — firma parcial por ítem */}
          <div>
            <label className="block text-xs font-semibold uppercase tracking-wider mb-1.5" style={{ color: 'var(--text-faint)' }}>
              ¿Qué llegó con esta guía?
            </label>
            {cargandoItems ? (
              <div className="text-xs flex items-center gap-2 py-2" style={{ color: 'var(--text-muted)' }}>
                <Loader2 className="w-3.5 h-3.5 animate-spin" /> Cargando los ítems del despacho…
              </div>
            ) : errorItems || !items || items.length === 0 ? (
              <p className="text-[11px]" style={{ color: 'var(--text-faint)' }}>
                {errorItems
                  ? 'No se pudieron cargar los ítems: la guía se firmará completa (como siempre).'
                  : 'Sin detalle de ítems disponible: la guía se firmará completa (como siempre).'}
              </p>
            ) : (
              <>
                <p className="text-[11px] mb-1.5" style={{ color: 'var(--text-faint)' }}>
                  Viene todo marcado como recibido. Si algo <b>no llegó</b>, destíckalo o baja la cantidad.
                </p>
                <div className="border rounded-xl overflow-hidden" style={{ borderColor: 'var(--border)' }}>
                  {items.map((it: DespachoItemDetalle) => {
                    const v = qtys?.[it.id] ?? String(it.qty_firmada ?? it.qty_despachada)
                    const marcado = Number(v) > 0
                    return (
                      <div
                        key={it.id}
                        className="flex items-center gap-2.5 px-3 py-2 border-t first:border-t-0"
                        style={{ borderColor: 'var(--border)' }}
                      >
                        <input
                          type="checkbox"
                          checked={marcado}
                          onChange={() => toggleItem(it)}
                          className="shrink-0 w-4 h-4 accent-emerald-500 cursor-pointer"
                          title={marcado ? 'Destickear = esta línea no llegó' : 'Marcar como recibida completa'}
                        />
                        <div className="flex-1 min-w-0">
                          <div className="font-mono text-xs text-brand-500 truncate">{it.numero_parte}</div>
                          <div className="text-[11px] truncate" style={{ color: 'var(--text-muted)' }}>
                            {it.descripcion}
                            {it.marca ? ` · ${it.marca}` : ''}
                          </div>
                        </div>
                        {(it.facturado ?? 0) > 0 && (
                          <span
                            className="text-[10px] font-semibold px-1.5 py-0.5 rounded-full bg-blue-500/15 text-blue-600 dark:text-blue-400 shrink-0"
                            title="Esta línea ya entró a una factura: no se puede bajar de lo facturado"
                          >
                            facturada
                          </span>
                        )}
                        <div className="flex items-center gap-1 shrink-0">
                          <input
                            type="number"
                            min={0}
                            max={it.qty_despachada}
                            step={1}
                            value={v}
                            onChange={e => setQtys(p => ({ ...(p ?? {}), [it.id]: e.target.value }))}
                            className="input w-16 text-right py-1 text-sm"
                          />
                          <span className="text-[11px]" style={{ color: 'var(--text-faint)' }}>
                            / {it.qty_despachada}
                          </span>
                        </div>
                      </div>
                    )
                  })}
                </div>
                {faltante > 0 && (
                  <div className="mt-2 space-y-2">
                    <div className="p-3 rounded-xl border bg-amber-500/10 border-amber-500/30 text-xs flex items-start gap-1.5">
                      <AlertTriangle className="w-4 h-4 shrink-0 text-amber-500" />
                      <span style={{ color: 'var(--text-muted)' }}>
                        Vas a declarar{' '}
                        <b className="text-amber-600 dark:text-amber-400">
                          {faltante} unidad{faltante === 1 ? '' : 'es'}
                        </b>{' '}
                        como faltante de entrega: no se podrán facturar por esta guía.
                      </span>
                    </div>
                    <div>
                      <label className="block text-xs font-semibold uppercase tracking-wider mb-1.5" style={{ color: 'var(--text-faint)' }}>
                        Motivo del faltante (obligatorio)
                      </label>
                      <textarea
                        value={motivo}
                        onChange={e => setMotivo(e.target.value)}
                        rows={2}
                        maxLength={MOTIVO_FALTANTE_MAX}
                        placeholder="ej: 1 unidad perdida por el transportista — se repondrá en una venta nueva"
                        className="input resize-none"
                      />
                      <p className="text-[11px] mt-1" style={{ color: 'var(--text-faint)' }}>
                        Entre {MOTIVO_FALTANTE_MIN} y {MOTIVO_FALTANTE_MAX} caracteres.
                      </p>
                    </div>
                  </div>
                )}
              </>
            )}
          </div>

          {/* Recuadro de errores: validación local y respuestas del backend */}
          {errores.length > 0 && (
            <div className="p-3 rounded-xl border bg-red-500/10 border-red-500/30">
              <p className="text-xs font-semibold text-red-500 mb-1 flex items-center gap-1.5">
                <AlertTriangle className="w-4 h-4" /> No se pudo confirmar la firma:
              </p>
              <ul className="text-xs space-y-0.5 list-disc pl-4" style={{ color: 'var(--text-muted)' }}>
                {errores.map((er, i) => <li key={i}>{er}</li>)}
              </ul>
            </div>
          )}
        </div>
        <div className="p-4 border-t flex justify-end gap-2" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
          <button onClick={onClose} className="btn-secondary text-sm">Cancelar</button>
          <button
            onClick={submit}
            disabled={saving || (!esEdicion && !file)}
            className="btn-primary text-sm flex items-center gap-1.5"
          >
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <CheckCircle2 className="w-4 h-4" />}
            {esEdicion ? 'Guardar firma' : 'Confirmar firma'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ─── Modal: editar transportista / N° de expedición / N° de guía ──────────────
function EditarDespachoModal({
  despacho,
  dteFolio,
  onClose,
  onSaved,
}: {
  despacho: DespachoRow
  dteFolio?: string | null
  onClose: () => void
  onSaved: () => void
}) {
  const [transportista, setTransportista] = useState(despacho.transportista || '')
  const [numeroExpedicion, setNumeroExpedicion] = useState(despacho.numero_expedicion || '')
  const [numeroGuia, setNumeroGuia] = useState(despacho.numero_guia || '')
  const [fechaGuia, setFechaGuia] = useState((despacho.fecha_guia || '').slice(0, 10))
  // Rótulo del bulto (caja física). Editable siempre: corregir un rotulado no
  // depende del estado de la guía SII (no viaja al SII).
  const [bulto, setBulto] = useState(despacho.bulto_numero || '')
  // Con qué bulto ABRIÓ el modal: el payload solo lo manda si el operador lo
  // cambió respecto de esto (ver submit).
  const bultoInicial = (despacho.bulto_numero || '').trim()
  const [saving, setSaving] = useState(false)
  // Tope del selector de fecha: hoy en Chile. Una guía no se emite mañana, y el error de
  // tipeo clásico es el año. El backend valida lo mismo (no confiar sólo en el navegador).
  const hoyChile = hoyEnChile()

  // dteFolio: null = sin guía electrónica · 'verificando' = consulta en curso ·
  // 'en_emision' = guía en proceso en el SII (ver folioParaModal) · otro valor =
  // folio SII real. Cualquier valor no-null bloquea la edición manual del N°.
  const folioBloqueado = dteFolio !== null && dteFolio !== undefined

  const submit = async () => {
    setSaving(true)
    try {
      await despachosAPI.update(despacho.id, {
        transportista: transportista || null,
        numero_expedicion: numeroExpedicion || null,
        // El bulto viaja SOLO si el operador lo cambió respecto del valor con
        // que abrió el modal (que venía de una fila con hasta 30s de caché):
        // mandarlo siempre hacía que quien edita solo el transportista PISARA
        // el bulto recién puesto por otro usuario. El backend usa exclude_unset:
        // no mandar = no tocar; null explícito = borrar (el borrado sigue vivo:
        // vaciar el campo difiere del inicial y manda null).
        // Contrato backend 2026-08-25: string ≤50 o null ("" → NULL en el servidor).
        ...(bulto.trim() !== bultoInicial ? { bulto_numero: bulto.trim() || null } : {}),
        // Guía electrónica emitida (o sin verificar): el folio no se edita a mano, y su
        // fecha la manda el propio DTE 52 (no este campo).
        ...(folioBloqueado ? {} : { numero_guia: numeroGuia || null, fecha_guia: fechaGuia || null }),
      })
      toast.success('Datos del despacho actualizados')
      onSaved()
    } catch (e: any) {
      toast.error(e?.response?.data?.detail || 'Error al guardar')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div
      className="fixed inset-0 flex items-center justify-center z-50 p-4"
      style={{ backgroundColor: 'rgba(0,0,0,0.65)', backdropFilter: 'blur(4px)' }}
      onClick={onClose}
    >
      <div
        className="max-w-md w-full rounded-2xl border shadow-2xl"
        style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}
        onClick={e => e.stopPropagation()}
      >
        <div
          className="p-4 border-b flex items-center justify-between"
          style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}
        >
          <h2 className="text-lg font-bold" style={{ color: 'var(--text-primary)' }}>
            Editar {despacho.numero_despacho}
          </h2>
          <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-[var(--surface-300)]" style={{ color: 'var(--text-muted)' }}>
            <X className="w-5 h-5" />
          </button>
        </div>
        <div className="p-4 space-y-3">
          <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
            Puedes completar el transportista o el N° de expedición en cualquier momento del despacho.
            {!folioBloqueado && (
              <> Si la guía la emitiste <b>directo en el SII</b>, registra aquí su N° <b>y su fecha de emisión</b>:
              la factura electrónica la cita con esa fecha.</>
            )}
          </p>
          <Input label="Transportista" value={transportista} onChange={setTransportista} placeholder="Ej: Samex" />
          <Input label="N° Expedición (courier / Samex)" value={numeroExpedicion} onChange={setNumeroExpedicion} />
          <Input
            label="Bulto N°"
            value={bulto}
            /* Array.from y no slice sobre el string: el tope de 50 se cuenta en
               caracteres reales para no partir un emoji (surrogate) en el borde. */
            onChange={(v: string) => setBulto(Array.from(v).slice(0, 50).join(''))}
            placeholder="Ej: 1, B2, Cajas 2-3"
            hint="Caja en que viaja este despacho (ej: 1, B2, Cajas 2-3)"
          />
          <Input
            label="N° Guía"
            value={numeroGuia}
            onChange={setNumeroGuia}
            disabled={folioBloqueado}
            hint={
              dteFolio === 'verificando'
                ? 'Verificando si este despacho tiene guía electrónica… (reabre el detalle para editar)'
                : dteFolio === 'en_emision'
                  ? 'Guía electrónica en emisión: el N° quedará fijado por el folio del SII al terminar.'
                  : folioBloqueado
                    ? `Guía electrónica emitida al SII (folio ${dteFolio}): el N° no se edita a mano.`
                    : 'Solo para guía en papel. Si vas a emitir la guía SII, déjalo vacío: el folio lo pone el SII.'
            }
          />
          <Input
            label="Fecha de emisión de la guía"
            type="date"
            max={hoyChile}
            value={fechaGuia}
            onChange={setFechaGuia}
            disabled={folioBloqueado}
            hint={
              folioBloqueado
                ? 'Guía electrónica: la fecha la pone el propio documento del SII.'
                : 'Fecha en que se EMITIÓ la guía en el SII (no la de la firma del cliente). La factura la cita en su referencia a la guía, así que sin este dato no se puede facturar al SII.'
            }
          />
        </div>
        <div className="p-4 border-t flex justify-end gap-2" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
          <button onClick={onClose} className="btn-secondary text-sm">Cancelar</button>
          <button onClick={submit} disabled={saving} className="btn-primary text-sm flex items-center gap-1.5">
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <CheckCircle2 className="w-4 h-4" />} Guardar
          </button>
        </div>
      </div>
    </div>
  )
}

// ─── Modal: reparto de bultos de la OC (texto para el transportista) ──────────
// Qué guía viaja en qué caja. El texto se arma en picking.ts (armarResumenBultos)
// y se copia tal cual al mail del transportista.
function RepartoBultosModal({
  oc,
  despachos,
  onClose,
}: {
  /** El llamador pasa `detail` (OcDetail): acá solo se pintan estos 4 campos. */
  oc: Pick<OcDetail, 'id' | 'numero_oc' | 'cliente' | 'direccion'>
  despachos: DespachoRow[]
  onClose: () => void
}) {
  const [copied, setCopied] = useState(false)
  const ids = useMemo(() => despachos.map(d => d.id), [despachos])

  // Detalle de cada despacho NO anulado, en paralelo: el bulto y los ítems viven
  // en GET /despachos/{id}. Cacheado por React Query mientras no cambien los
  // despachos de la OC.
  // Bajo la raíz ['despachos'] A PROPÓSITO: crear/editar/firmar/anular ya
  // invalidan ['despachos'] en todo el archivo, y así esas invalidaciones barren
  // este caché gratis. Con raíz propia, el staleTime global de 30s dejaba servir
  // un reparto viejo — y este texto se COPIA al mail del transportista.
  const { data: detalles, isLoading, isError } = useQuery({
    queryKey: ['despachos', 'bultos-oc', oc.id, ids],
    // El tipo se ensancha con `cerrado_hoy` (campo aditivo del contrato nuevo):
    // services/api.ts todavía describe el detalle sin él.
    queryFn: (): Promise<DespachoDetalleConCerrado[]> =>
      Promise.all(ids.map(id => despachosAPI.get(id))),
  })

  // dd-mm-aaaa (mismo formato que la línea «Fecha del reparto»; fmtDate usa
  // barras). Se toma el DÍA tal como lo estampó el servidor y se invierte: nada
  // de new Date('YYYY-MM-DD'), que en Chile corre la fecha un día hacia atrás.
  const fmtDia = (s?: string | null): string | null => {
    const dia = (s || '').slice(0, 10)
    return /^\d{4}-\d{2}-\d{2}$/.test(dia) ? dia.split('-').reverse().join('-') : null
  }

  const resumen = useMemo(() => {
    if (!detalles) return null
    // Cinturón: otro usuario pudo ANULAR un despacho entre que la fila se pintó
    // y este fetch fresco (el llamador filtra, pero sobre datos con hasta 30s de
    // caché). Un despacho anulado nunca viaja. Se filtra el PAR (detalle, fila)
    // junto para no desalinear los fallbacks por índice.
    const vivos = detalles
      .map((det, i) => ({ det, fila: despachos[i] }))
      .filter(({ det }) => det.estado !== 'anulado')
    return armarResumenBultos({
      // numero_oc es NULLABLE (OCs legacy): sin el fallback, la PRIMERA línea del
      // texto que se pega en el mail decía literalmente «OC null - MINERA X».
      // La identidad se resuelve ACÁ (picking.ts recibe una identidad ya
      // resuelta, no la decide) y con el MISMO formato que el panel: «OC #123».
      numero_oc: oc.numero_oc || `#${oc.id}`,
      cliente: oc.cliente,
      direccion: oc.direccion || null,
      // Fecha de HOY en Chile (dd-mm-aaaa): el reparto se manda antes del viaje.
      // Con el día del NAVEGADOR (hoyLocal, lo que había acá), un PC con la zona
      // corrida —o el turno de noche desde otra zona— le mandaba al transportista
      // un mail fechado el día equivocado. El día del negocio es el de Chile.
      fecha: hoyEnChile().split('-').reverse().join('-'),
      despachos: vivos.map(({ det, fila }) => {
        // fecha_despacho (cierre) solo viaja en la fila del listado; el detalle
        // no la trae. Es un sello INFORMATIVO (ver `fechaSalida` más abajo): el
        // corte «hoy / ya viajó» NO se calcula con ella.
        const fechaSalidaIso = fila?.fecha_despacho ?? null
        // ¿Cerrado HOY? Lo dice el SERVIDOR y nadie más. `fecha_despacho` llega
        // en UTC y sin zona (el server corre en UTC), así que compararla contra
        // el reloj del navegador fechaba «mañana» todo lo que se cerró después
        // de las ~21:00 de Chile: la caja de hoy caía en «YA DESPACHADO — no
        // viaja en este reparto» y se descontaba de los totales, o sea que el
        // mail al transportista escondía la caja que tenía que cargar (y a la
        // mañana siguiente pasaba lo inverso).
        // El backend nuevo lo manda SIEMPRE en los dos serializers (detalle y
        // fila), con `fecha_despacho` nulo → false. Se leen los dos por si uno
        // de los dos endpoints responde desde una versión anterior.
        // POR QUÉ `?? null` Y NO `?? false`: `false` significa «ya viajó» y
        // manda la caja al histórico, o sea que un backend viejo —o el hueco de
        // un deploy a medias— ESCONDERÍA cajas en silencio, que es exactamente
        // el daño que este arreglo viene a evitar. `null` es «no sé», y el no sé
        // se resuelve mostrando (vaASalir). Un reparto con una caja de más se ve
        // y se corrige; uno con una caja de menos se descubre en el cliente.
        const cerradoHoy = det.cerrado_hoy ?? fila?.cerrado_hoy ?? null
        return {
          // Fallback a la fila del listado: un backend previo puede no mandar la
          // cabecera completa en el detalle (campos opcionales del contrato).
          numero_despacho: det.numero_despacho || fila?.numero_despacho || `#${det.id}`,
          numero_guia: det.numero_guia ?? fila?.numero_guia ?? null,
          bulto_numero: det.bulto_numero ?? fila?.bulto_numero ?? null,
          // El ESTADO es el eje del reparto (ver picking.ts/vaASalir): 'despachado'
          // ya salió, 'en_preparacion' va a salir. La firma NO decide.
          estado: det.estado ?? fila?.estado ?? null,
          cerradoHoy,
          // DEUDA MENOR CONOCIDA: este sello imprime el día CRUDO del ISO, que
          // es UTC — un cierre de las 22:00 de Chile se sella con la fecha del
          // día siguiente. Es solo texto informativo (el corte del reparto ya no
          // depende de él); arreglarlo pide que el backend mande la fecha de
          // cierre ya expresada en día de Chile, como hizo con `cerrado_hoy`.
          fechaSalida: fmtDia(fechaSalidaIso),
          fechaFirma: fmtDia(det.fecha_firma ?? fila?.fecha_firma ?? null),
          guiaFirmada: Boolean(det.guia_firmada ?? fila?.guia_firmada),
          items: (det.items ?? []).map(it => ({
            numero_parte: it.numero_parte,
            descripcion: it.descripcion,
            // SIEMPRE qty_despachada: el mail al transportista se manda ANTES del
            // viaje; qty_firmada y el faltante de entrega ocurren DESPUÉS y NO
            // participan de este resumen.
            qty: it.qty_despachada,
          })),
        }
      }),
    })
  }, [detalles, despachos, oc])

  // Patrón exacto de TextDocModal: clipboard en try/catch + toast si falla.
  const copy = async () => {
    if (!resumen) return
    try {
      await navigator.clipboard.writeText(resumen.texto)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      toast.error('No se pudo copiar')
    }
  }

  return (
    <div
      className="fixed inset-0 z-[9999] flex items-center justify-center p-4"
      style={{ backgroundColor: 'rgba(0,0,0,0.65)', backdropFilter: 'blur(4px)' }}
      onClick={onClose}
    >
      <div
        className="w-full max-w-xl max-h-[90vh] flex flex-col rounded-2xl border shadow-2xl overflow-hidden"
        style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}
        onClick={e => e.stopPropagation()}
      >
        <div
          className="p-4 border-b flex items-center justify-between"
          style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}
        >
          <div>
            <h3 className="font-bold text-sm" style={{ color: 'var(--text-primary)' }}>
              {/* Mismo fallback de identidad que el texto y que el panel. */}
              Bultos de la OC {oc.numero_oc || `#${oc.id}`}
            </h3>
            <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
              {/* «por salir»: los totales NO incluyen los despachos que ya
                  viajaron (van rotulados aparte en el texto). */}
              {resumen
                ? `${oc.cliente} · ${resumen.totalBultos} bulto${resumen.totalBultos === 1 ? '' : 's'} / ` +
                  `${resumen.totalGuias} guía${resumen.totalGuias === 1 ? '' : 's'} / ` +
                  `${fmtQtyPicking(resumen.totalUnidades)} unidades por salir`
                : oc.cliente}
            </p>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg hover:bg-[var(--surface-300)]"
            style={{ color: 'var(--text-muted)' }}
          >
            <X className="w-5 h-5" />
          </button>
        </div>
        <div className="p-4 overflow-y-auto flex-1 space-y-3">
          {resumen && resumen.yaViajaron > 0 && (
            /* Aviso NEUTRO (no es un error): esta OC ya tuvo entregas. Van en el
               texto rotuladas como «YA DESPACHADO» y NO suman a los totales —
               antes salían mezcladas con las de hoy y el transportista iba a
               buscar cajas que se entregaron hace semanas. */
            <div
              className="flex items-start gap-2 p-3 rounded-xl border text-xs"
              style={{ backgroundColor: 'var(--surface-200)', borderColor: 'var(--border)', color: 'var(--text-muted)' }}
            >
              <Truck className="w-4 h-4 shrink-0 mt-0.5" style={{ color: 'var(--text-faint)' }} />
              <span>
                {resumen.yaViajaron} despacho{resumen.yaViajaron === 1 ? '' : 's'} de esta OC ya{' '}
                {resumen.yaViajaron === 1 ? 'viajó' : 'viajaron'} ({fmtQtyPicking(resumen.unidadesYaViajaron)} unidades):
                {' '}van al final del texto como histórico y no cuentan en los totales del reparto.
              </span>
            </div>
          )}
          {resumen?.hayGuiasPendientes && (
            <div
              className="flex items-start gap-2 p-3 rounded-xl border text-xs"
              style={{
                backgroundColor: 'rgba(245, 158, 11, 0.08)',
                borderColor: 'rgba(245, 158, 11, 0.4)',
                color: 'var(--text-muted)',
              }}
            >
              <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5 text-amber-500" />
              <span>Hay despachos sin guía emitida: van como «Guía N° PENDIENTE».</span>
            </div>
          )}
          {resumen && resumen.colisionesRotulos.length > 0 && (
            /* Rótulos que solo difieren en mayúsculas: casi seguro es la MISMA
               caja rotulada dos veces. Se AVISA sin unificar (decisión de
               picking.ts: unificar en silencio escondería el error). */
            <div
              className="flex items-start gap-2 p-3 rounded-xl border text-xs"
              style={{
                backgroundColor: 'rgba(245, 158, 11, 0.08)',
                borderColor: 'rgba(245, 158, 11, 0.4)',
                color: 'var(--text-muted)',
              }}
            >
              <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5 text-amber-500" />
              <span>
                {resumen.colisionesRotulos
                  .map(g => g.map(r => `«${r}»`).join(' y '))
                  .join('; ')}{' '}
                parecen la misma caja con distinto rótulo — corrígelo en «Editar despacho».
              </span>
            </div>
          )}
          {isLoading && (
            <div className="flex items-center justify-center gap-2 py-8 text-sm" style={{ color: 'var(--text-muted)' }}>
              <Loader2 className="w-4 h-4 animate-spin" /> Cargando despachos...
            </div>
          )}
          {isError && (
            <div className="text-center py-8 text-sm text-red-500">
              No se pudo cargar el detalle de los despachos
            </div>
          )}
          {resumen && (
            <pre
              className="text-sm whitespace-pre-wrap break-words p-3 rounded-lg border select-text"
              style={{
                backgroundColor: 'var(--surface)',
                borderColor: 'var(--border)',
                color: 'var(--text-primary)',
              }}
            >
              {resumen.texto}
            </pre>
          )}
        </div>
        <div
          className="p-3 border-t flex items-center justify-end gap-2"
          style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}
        >
          <button onClick={copy} disabled={!resumen} className="btn-primary text-sm">
            {copied ? (
              <>
                <CheckCircle2 className="w-4 h-4" />
                Copiado
              </>
            ) : (
              <>
                <FileText className="w-4 h-4" />
                Copiar
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  )
}

// ─── Panel: qué hay LISTO para despachar ahora (se abre desde los KPIs) ───────
// Resumen transversal por OC de la mercadería en bodega con cupo REAL (recibido
// − tomado por despachos abiertos). UN solo GET: el backend arma los grupos y
// los ordena por urgencia — acá no se reordena ni se recalcula nada.
function ListoParaDespacharModal({
  onClose,
  onIrAOc,
}: {
  onClose: () => void
  /** Cierra el panel y lleva al operador a la card de esa OC en el listado.
   *  numeroOc puede venir null (OC legacy sin N°): el padre cae al id. */
  onIrAOc: (ocId: number, numeroOc: string | null) => void
}) {
  const { data, isLoading, isError, refetch, isRefetching } = useQuery<ListoResumenResponse>({
    queryKey: ['despachos', 'listo-resumen'],
    queryFn: despachosAPI.listoParaDespachar,
    // staleTime 0 + refetchOnMount 'always' A PROPÓSITO: una recepción cerrada
    // en Bodega ocurre en OTRA pantalla y no invalida nada de ['despachos'], y
    // el staleTime global de 30s (main.tsx) serviría datos viejos justo aquí,
    // donde el operador decide qué despachar AHORA.
    staleTime: 0,
    refetchOnMount: 'always',
  })
  const grupos = data?.grupos ?? []
  const totalUnidades = grupos.reduce((acc, g) => acc + (g.total_unidades || 0), 0)

  return (
    <div
      className="fixed inset-0 z-[9999] flex items-center justify-center p-4"
      style={{ backgroundColor: 'rgba(0,0,0,0.65)', backdropFilter: 'blur(4px)' }}
      onClick={onClose}
    >
      <div
        className="w-full max-w-2xl max-h-[90vh] flex flex-col rounded-2xl border shadow-2xl overflow-hidden"
        style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}
        onClick={e => e.stopPropagation()}
      >
        <div
          className="p-4 border-b flex items-center justify-between"
          style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}
        >
          <div className="flex items-center gap-2.5 min-w-0">
            <Zap className="w-5 h-5 text-emerald-500 shrink-0" />
            <div className="min-w-0">
              <h3 className="font-bold text-sm" style={{ color: 'var(--text-primary)' }}>
                Listo para despachar
              </h3>
              <p className="text-xs truncate" style={{ color: 'var(--text-muted)' }}>
                {data
                  ? `${grupos.length} OC${grupos.length === 1 ? '' : 's'} · ` +
                    `${fmtQtyPicking(totalUnidades)} un. por despachar` +
                    (data.hoy ? ` · al ${fmtDate(data.hoy)}` : '')
                  : 'Disponible real en bodega, descontando despachos abiertos'}
              </p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg hover:bg-[var(--surface-300)]"
            style={{ color: 'var(--text-muted)' }}
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="p-4 overflow-y-auto flex-1 space-y-3">
          {isLoading && (
            /* Esqueleto: 3 grupos fantasma con el mismo layout que los reales. */
            <>
              {[0, 1, 2].map(i => (
                <div
                  key={i}
                  className="border rounded-xl p-3 space-y-2 animate-pulse"
                  style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}
                >
                  <div className="h-4 w-2/3 rounded" style={{ backgroundColor: 'var(--surface-300)' }} />
                  <div className="h-3 w-full rounded" style={{ backgroundColor: 'var(--surface-300)' }} />
                  <div className="h-3 w-5/6 rounded" style={{ backgroundColor: 'var(--surface-300)' }} />
                </div>
              ))}
            </>
          )}

          {isError && !isLoading && (
            <div className="text-center py-10 space-y-3">
              <div className="text-sm text-red-500">
                No se pudo cargar el resumen de despachables
              </div>
              <button
                onClick={() => refetch()}
                disabled={isRefetching}
                className="btn-secondary text-sm inline-flex items-center gap-1.5"
              >
                {isRefetching && <Loader2 className="w-4 h-4 animate-spin" />} Reintentar
              </button>
            </div>
          )}

          {data && grupos.length === 0 && (
            /* Vacío POSITIVO: bodega al día no es un error. */
            <div className="text-center py-10">
              <CheckCircle2 className="w-8 h-8 mx-auto mb-2 text-emerald-500" />
              <div className="text-sm font-semibold" style={{ color: 'var(--text-primary)' }}>
                ✓ No hay mercadería en bodega pendiente de despachar
              </div>
            </div>
          )}

          {grupos.map(g => (
            <div
              key={g.oc_cliente_id}
              className="border rounded-xl overflow-hidden"
              style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}
            >
              <div className="p-3 flex items-center gap-2 flex-wrap">
                <span className="text-xs font-mono font-semibold text-brand-500 shrink-0">
                  {/* numero_oc null (OC legacy): identidad por id, no «OC-» vacío */}
                  {g.numero_oc ? `OC-${g.numero_oc}` : `OC #${g.oc_cliente_id}`}
                </span>
                <span
                  className="text-sm font-semibold truncate max-w-[16rem]"
                  style={{ color: 'var(--text-primary)' }}
                  title={g.cliente}
                >
                  {g.cliente}
                </span>
                <DiasRestantesBadge dias={g.dias_restantes_critico} label="entrega" />
                <span className="text-xs shrink-0" style={{ color: 'var(--text-muted)' }}>
                  {fmtQtyPicking(g.total_unidades)} un.
                  {g.fecha_entrega && ` · Entrega: ${fmtDate(g.fecha_entrega)}`}
                </span>
                <button
                  onClick={() => onIrAOc(g.oc_cliente_id, g.numero_oc)}
                  className="ml-auto px-2.5 py-1 text-xs font-semibold rounded-lg border hover:bg-[var(--surface-300)] inline-flex items-center gap-1 shrink-0"
                  style={{ borderColor: 'var(--border)', color: 'var(--text-muted)' }}
                >
                  Ir a la OC <ChevronRight className="w-3 h-3" />
                </button>
              </div>
              <div className="border-t" style={{ borderColor: 'var(--border)' }}>
                {g.items.map((it, i) => (
                  <div
                    key={`${it.numero_parte}-${i}`}
                    className={`px-3 py-1.5 flex items-center gap-3 text-xs ${i > 0 ? 'border-t' : ''}`}
                    style={{ borderColor: 'var(--border)' }}
                  >
                    <span className="font-mono text-brand-500 shrink-0">{it.numero_parte}</span>
                    <span
                      className="truncate flex-1"
                      style={{ color: 'var(--text-muted)' }}
                      title={it.descripcion}
                    >
                      {it.descripcion}
                    </span>
                    <span className="shrink-0 font-semibold" style={{ color: 'var(--text-primary)' }}>
                      {fmtQtyPicking(it.qty_disponible)} de {fmtQtyPicking(it.cantidad)}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

// ─── Modal: emitir guía de despacho electrónica (SII 52) vía Wasabil ──────────
// Protocolo de seguridad: PASO 1 previsualización (no toca el SII) → PASO 2 con
// el OK explícito del usuario se emite (IRREVERSIBLE) → sondeo del estado hasta
// Emitido (folio real + PDF) o Fallido (motivo del SII + reintento seguro).
const fmtCLP = (n: number) => '$' + Math.round(n || 0).toLocaleString('es-CL')

function EmitirGuiaSIIModal({
  despacho,
  onClose,
  onDone,
}: {
  despacho: DespachoRow
  onClose: () => void
  onDone: () => void
}) {
  type Fase = 'cargando' | 'preview' | 'error_carga' | 'emitiendo' | 'sondeo' | 'exito' | 'fallido' | 'pendiente'
  const [fase, setFase] = useState<Fase>('cargando')
  const [preview, setPreview] = useState<any>(null)
  const [dte, setDte] = useState<any>(null)
  const [error, setError] = useState('')
  // Tipo de traslado del SII (dispatchTypeCode): 1 venta por defecto, 5 traslado
  // interno hacia bodega propia, etc. El operador lo elige antes de emitir.
  const [tipoTraslado, setTipoTraslado] = useState(1)

  // Decide la fase según el DTE previo (única fuente: puede_reintentar del backend)
  const faseSegunDte = (d: any): Fase => {
    if (!d || d.estado === 'no_enviado') return 'preview'
    if (d.estado === 'emitido') return 'exito'
    if (d.puede_reintentar) return 'fallido'          // fallido del SII o error de envío
    if (d.uuid) return 'sondeo'                       // procesando/pendiente en Wasabil
    return 'pendiente'                                // claim en vuelo de otro intento
  }

  // PASO 1: cargar la previsualización (si ya había una emisión previa, retomarla).
  // También se usa para RESINCRONIZAR tras un error al emitir.
  const cargarPreview = () => {
    setFase('cargando')
    wasabilAPI.previewGuia(despacho.id)
      .then(({ data }) => {
        setPreview(data)
        if (data.dte) setDte(data.dte)
        setFase(faseSegunDte(data.dte))
      })
      .catch((e: any) => {
        setError(e?.response?.data?.detail || 'No se pudo cargar la previsualización')
        setFase('error_carga')
      })
  }
  useEffect(cargarPreview, [despacho.id])

  // Sondeo: el envío al SII es asíncrono (segundos a minutos)
  useEffect(() => {
    if (fase !== 'sondeo') return
    let vivo = true
    let intentos = 0
    const tick = async () => {
      if (!vivo) return
      intentos += 1
      try {
        const { data } = await wasabilAPI.estadoGuia(despacho.id)
        if (!vivo) return
        setDte(data)
        if (data.estado === 'emitido') { setFase('exito'); onDone(); return }
        if (data.puede_reintentar) { setFase('fallido'); return }
      } catch { /* error transitorio: se reintenta en el próximo tick */ }
      if (intentos >= 30) { setFase('pendiente'); return }  // ~90 s: seguir después
      window.setTimeout(tick, 3000)
    }
    tick()
    return () => { vivo = false }
  }, [fase, despacho.id])

  // PASO 2: emitir (solo tras ver la previsualización) o reintentar un fallido
  const emitir = async (reintento: boolean) => {
    setFase('emitiendo')
    setError('')
    try {
      const { data } = reintento
        ? await wasabilAPI.reintentarGuia(despacho.id, tipoTraslado)
        : await wasabilAPI.emitirGuia(despacho.id, tipoTraslado)
      setDte(data)
      if (data.estado === 'emitido') { setFase('exito'); onDone() }
      else if (data.puede_reintentar) setFase('fallido')
      else setFase('sondeo')
    } catch (e: any) {
      // 409/502/timeout: RESINCRONIZAR con el backend en vez de adivinar la fase
      // (el estado real pudo cambiar: claim en curso, fallido, incluso emitido)
      setError(e?.response?.data?.detail || 'No se pudo emitir la guía')
      cargarPreview()
    }
  }

  const receptor = preview?.receptor
  const referencia = preview?.referencias?.[0]

  return (
    <div
      className="fixed inset-0 flex items-center justify-center z-50 p-4"
      style={{ backgroundColor: 'rgba(0,0,0,0.65)', backdropFilter: 'blur(4px)' }}
      onClick={onClose}
    >
      <div
        className="max-w-2xl w-full max-h-[90vh] overflow-hidden flex flex-col rounded-2xl border shadow-2xl"
        style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}
        onClick={e => e.stopPropagation()}
      >
        <div
          className="p-4 border-b flex items-center justify-between"
          style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}
        >
          <div>
            <h2 className="text-lg font-bold flex items-center gap-2" style={{ color: 'var(--text-primary)' }}>
              <Receipt className="w-5 h-5 text-blue-500" /> Emitir guía de despacho SII
            </h2>
            <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
              {despacho.numero_despacho} · vía Wasabil (GRUPO AM SPA)
            </p>
          </div>
          <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-[var(--surface-300)]" style={{ color: 'var(--text-muted)' }}>
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="p-4 space-y-4 overflow-y-auto">
          {fase === 'cargando' && (
            <div className="py-10 text-center" style={{ color: 'var(--text-muted)' }}>
              <Loader2 className="w-6 h-6 animate-spin mx-auto mb-2" /> Armando la previsualización…
            </div>
          )}

          {(fase === 'emitiendo' || fase === 'sondeo') && (
            <div className="py-10 text-center" style={{ color: 'var(--text-muted)' }}>
              <Loader2 className="w-8 h-8 animate-spin mx-auto mb-3 text-blue-500" />
              <p className="font-semibold" style={{ color: 'var(--text-primary)' }}>
                {fase === 'emitiendo' ? 'Enviando a Wasabil…' : 'Procesando en el SII…'}
              </p>
              <p className="text-xs mt-1">El SII puede tardar de segundos a un par de minutos. No cierres esta ventana.</p>
            </div>
          )}

          {fase === 'exito' && dte && (
            <div className="py-8 text-center space-y-3">
              <CheckCircle2 className="w-10 h-10 mx-auto text-emerald-500" />
              <p className="text-lg font-bold" style={{ color: 'var(--text-primary)' }}>
                Guía emitida — Folio SII {dte.folio}
              </p>
              <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
                El folio quedó registrado en el despacho. Imprime el PDF para que viaje con la carga.
                Cierra esta ventana para agregar el transportista y confirmar el despacho.
              </p>
              {dte.pdf_url && (
                <button
                  onClick={() => window.open(dte.pdf_url, '_blank', 'noopener,noreferrer')}
                  className="btn-primary text-sm inline-flex items-center gap-1.5"
                >
                  <FileText className="w-4 h-4" /> Ver PDF de la guía
                </button>
              )}
            </div>
          )}

          {fase === 'pendiente' && (
            <div className="py-8 text-center space-y-2">
              <Clock className="w-8 h-8 mx-auto text-amber-500" />
              <p className="font-semibold" style={{ color: 'var(--text-primary)' }}>Emisión en curso</p>
              <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
                Hay una emisión en proceso para este despacho (puede ser de otra pestaña u
                otro usuario). Puedes cerrar esta ventana: el estado se actualizará al volver
                a abrir el despacho y <b>no se emitirá dos veces</b>.
              </p>
            </div>
          )}

          {fase === 'error_carga' && (
            <div className="py-8 text-center space-y-2">
              <AlertTriangle className="w-8 h-8 mx-auto text-red-500" />
              <p className="font-semibold" style={{ color: 'var(--text-primary)' }}>
                No se pudo cargar la previsualización
              </p>
              <p className="text-xs" style={{ color: 'var(--text-muted)' }}>{error}</p>
              <button onClick={cargarPreview} className="btn-secondary text-sm">Volver a intentar</button>
            </div>
          )}

          {fase === 'fallido' && (
            <div className="p-3 rounded-xl border bg-red-500/10 border-red-500/30 space-y-1">
              <p className="text-sm font-semibold text-red-500 flex items-center gap-1.5">
                <AlertTriangle className="w-4 h-4" />
                {dte?.estado === 'error_envio' ? 'La emisión no llegó a Wasabil' : 'El SII rechazó la guía'}
              </p>
              <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
                {error || dte?.error || 'Sin detalle del motivo'}
              </p>
            </div>
          )}

          {(fase === 'preview' || fase === 'fallido') && preview && (
            <>
              {preview.problemas?.length > 0 && fase === 'preview' && (
                <div className="p-3 rounded-xl border bg-red-500/10 border-red-500/30">
                  <p className="text-xs font-semibold text-red-500 mb-1">Para emitir falta resolver:</p>
                  <ul className="text-xs space-y-0.5 list-disc pl-4" style={{ color: 'var(--text-muted)' }}>
                    {preview.problemas.map((p: string, i: number) => <li key={i}>{p}</li>)}
                  </ul>
                </div>
              )}
              {preview.advertencias?.length > 0 && (
                <div className="p-3 rounded-xl border bg-amber-500/10 border-amber-500/30">
                  <ul className="text-xs space-y-0.5 list-disc pl-4" style={{ color: 'var(--text-muted)' }}>
                    {preview.advertencias.map((a: string, i: number) => <li key={i}>{a}</li>)}
                  </ul>
                </div>
              )}

              {/* Receptor (ficha real en Wasabil = lo que verá el SII) */}
              <div className="grid grid-cols-2 gap-3 text-sm">
                <div>
                  <div className="text-xs uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>
                    Receptor {receptor?.fuente === 'wasabil' ? '(ficha Wasabil)' : '(datos locales)'}
                  </div>
                  <div className="font-semibold" style={{ color: 'var(--text-primary)' }}>{receptor?.razon_social || '—'}</div>
                  <div className="text-xs" style={{ color: 'var(--text-muted)' }}>RUT {receptor?.rut || '—'}</div>
                  {receptor?.giro && <div className="text-xs" style={{ color: 'var(--text-muted)' }}>{receptor.giro}</div>}
                  {receptor?.direccion && (
                    <div className="text-xs" style={{ color: 'var(--text-muted)' }}>
                      {receptor.direccion}{receptor.comuna ? `, ${receptor.comuna}` : ''}
                    </div>
                  )}
                </div>
                <div>
                  <div className="text-xs uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>
                    Referencia (SII 801)
                  </div>
                  <div className="font-semibold" style={{ color: 'var(--text-primary)' }}>OC {referencia?.folio || '—'}</div>
                  <div className="text-xs" style={{ color: 'var(--text-muted)' }}>
                    Fecha OC: {referencia?.fecha || '—'}
                  </div>
                  <label className="block mt-2">
                    <span className="text-[11px] uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>
                      Tipo de traslado (SII)
                    </span>
                    <select
                      value={tipoTraslado}
                      onChange={(e) => setTipoTraslado(Number(e.target.value))}
                      disabled={fase !== 'preview' && fase !== 'fallido'}
                      className="w-full mt-0.5 px-2 py-1 rounded-lg text-xs border disabled:opacity-60"
                      style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)', color: 'var(--text-primary)' }}
                    >
                      {(preview.tipos_traslado ?? [{ codigo: 1, label: 'Operación constituye venta' }]).map(
                        (t: { codigo: number; label: string }) => (
                          <option key={t.codigo} value={t.codigo}>{t.label}</option>
                        ),
                      )}
                    </select>
                  </label>
                </div>
              </div>

              {/* Líneas */}
              {preview.lineas?.length > 0 && (
                <div className="border rounded-xl overflow-hidden" style={{ borderColor: 'var(--border)' }}>
                  <table className="w-full text-xs">
                    <thead>
                      <tr style={{ backgroundColor: 'var(--surface-200)', color: 'var(--text-faint)' }}>
                        <th className="p-2 text-left font-semibold uppercase tracking-wider">Ítem</th>
                        <th className="p-2 text-right font-semibold uppercase tracking-wider">Cant.</th>
                        <th className="p-2 text-right font-semibold uppercase tracking-wider">P. unit. neto</th>
                        <th className="p-2 text-right font-semibold uppercase tracking-wider">Total neto</th>
                      </tr>
                    </thead>
                    <tbody>
                      {preview.lineas.map((ln: any, i: number) => (
                        <tr key={i} className="border-t" style={{ borderColor: 'var(--border)' }}>
                          <td className="p-2">
                            <span className="font-mono font-semibold" style={{ color: 'var(--text-primary)' }}>{ln.name}</span>
                            {ln.description && (
                              <span className="block text-[11px]" style={{ color: 'var(--text-faint)' }}>{ln.description}</span>
                            )}
                          </td>
                          <td className="p-2 text-right" style={{ color: 'var(--text-primary)' }}>{ln.quantity}</td>
                          <td className="p-2 text-right" style={{ color: 'var(--text-muted)' }}>{fmtCLP(ln.price)}</td>
                          <td className="p-2 text-right font-semibold" style={{ color: 'var(--text-primary)' }}>
                            {fmtCLP(ln.price * ln.quantity)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                    <tfoot style={{ backgroundColor: 'var(--surface-200)' }}>
                      <tr className="border-t" style={{ borderColor: 'var(--border)' }}>
                        <td colSpan={3} className="p-2 text-right text-[11px] uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>Neto</td>
                        <td className="p-2 text-right font-semibold" style={{ color: 'var(--text-primary)' }}>{fmtCLP(preview.totales?.neto)}</td>
                      </tr>
                      <tr>
                        <td colSpan={3} className="p-2 text-right text-[11px] uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>IVA 19%</td>
                        <td className="p-2 text-right" style={{ color: 'var(--text-muted)' }}>{fmtCLP(preview.totales?.iva)}</td>
                      </tr>
                      <tr>
                        <td colSpan={3} className="p-2 text-right text-[11px] uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>Total</td>
                        <td className="p-2 text-right font-bold text-blue-500">{fmtCLP(preview.totales?.total)}</td>
                      </tr>
                    </tfoot>
                  </table>
                </div>
              )}

              {error && fase === 'preview' && (
                <div className="p-3 rounded-xl border bg-red-500/10 border-red-500/30 text-xs text-red-500">{error}</div>
              )}
            </>
          )}
        </div>

        {(fase === 'preview' || fase === 'fallido') && (
          <div className="p-4 border-t space-y-2" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
            <p className="text-[11px] flex items-center gap-1.5" style={{ color: 'var(--text-muted)' }}>
              <AlertTriangle className="w-3.5 h-3.5 text-amber-500 shrink-0" />
              Al confirmar, la guía se emite al SII a través de Wasabil. Esta acción es IRREVERSIBLE.
            </p>
            <div className="flex justify-end gap-2">
              <button onClick={onClose} className="btn-secondary text-sm">Cancelar</button>
              {fase === 'fallido' ? (
                <button onClick={() => emitir(true)} className="btn-primary text-sm flex items-center gap-1.5">
                  <Send className="w-4 h-4" /> Reintentar emisión
                </button>
              ) : (
                <button
                  onClick={() => emitir(false)}
                  disabled={!preview?.puede_emitir}
                  className="btn-primary text-sm flex items-center gap-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  <Send className="w-4 h-4" /> Confirmar y emitir al SII
                </button>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
