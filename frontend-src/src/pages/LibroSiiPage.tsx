// Página "Libro SII" (libro de compras del SII — Grupo AM / MachParts, Fases A1+A2).
// Responde LA pregunta del dueño: «¿qué facturas de proveedor existen ante el SII y NO
// están registradas en el ERP?» — y deja clasificar cada documento (ignorar / costo por
// venta / activo fijo / centro de costos). Consume /api/sii-libro (backend/wasabil_compras).
// Tres zonas: TABLERO (cubetas + edad del barrido + cuadratura mensual), BANDEJA
// (documentos con filtros y decisión por fila) y REGLAS POR EMISOR (defaults por RUT).
import { useState, useEffect, useCallback, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  BookOpenCheck, RefreshCw, Download, Search, AlertTriangle, AlertCircle, Loader2,
  ChevronDown, ChevronUp, ChevronLeft, ChevronRight, CheckCircle2, HelpCircle, Clock,
  Shield, X, Landmark, Play, ArrowLeftRight, Wallet,
} from 'lucide-react'
import toast from 'react-hot-toast'
import api from '../services/api'
import { fmtClp, fmtDate } from '../utils/format'

// ─── API (cliente compartido con auth; endpoints de backend/wasabil_compras/router.py) ──
const siiLibroAPI = {
  resumen: () => api.get('/sii-libro/resumen'),
  // El barrido recorre el API de Wasabil completo: puede tardar más que el timeout normal.
  sincronizar: () => api.post('/sii-libro/sincronizar', null, { timeout: 300_000 }),
  documentos: (params: Record<string, unknown>) => api.get('/sii-libro/documentos', { params }),
  decidir: (id: number, data: { accion: string; destino?: string; motivo?: string }) =>
    api.post(`/sii-libro/documentos/${id}/decision`, data),
  reglas: () => api.get('/sii-libro/reglas-rut'),
  upsertRegla: (data: { rut: string; nivel: string; destino_default?: string; motivo?: string }) =>
    api.post('/sii-libro/reglas-rut', data),
  // H4: el CSV sale con el MISMO filtro que la pantalla (si estás mirando los
  // desaparecidos, exportas los desaparecidos — no otra cosa).
  exportCsv: (params: Record<string, unknown>) =>
    api.get('/sii-libro/export.csv', { params, responseType: 'blob' }),
  // Datos del documento con los nombres EXACTOS de CompraCreate: pre-llenan el
  // formulario NORMAL de Compras y Pagos (el operador revisa y guarda allá).
  prefillCompra: (id: number) => api.get(`/sii-libro/documentos/${id}/prefill-compra`),
}

// Llave de sessionStorage con la que el prefill viaja a la página de Compras y Pagos
// (ComprasContabPage la consume UNA vez al montar y la borra).
const PREFILL_COMPRA_SII_KEY = 'prefillCompraSii'

// ─── Tipos (espejo de _serializar y /resumen del router) ─────────────────────────────
// H3: el backend manda el DESGLOSE de la cuadratura (ignorados, notas de crédito, ERP
// comparable) y la diferencia que SÍ puede dar $0. Las claves nuevas van opcionales para
// que la pantalla no se caiga si habla con un backend anterior al desglose.
interface CuadraturaMes {
  mes: string; libro: number; erp: number; diferencia: number
  libro_ignorados?: number; libro_nc?: number
  erp_comparable?: number; erp_fuera_libro?: number; diferencia_explicada?: number
}
interface Resumen {
  ultimo_barrido: { exito: boolean | null; origen: string | null; terminado_at: string | null; error: string | null; total_api: number | null } | null
  edad_horas_ultimo_exitoso: number | null
  edad_critica: boolean
  documentos_activos: number
  cubetas: { ESTA: number; NO_ESTA: number; INDETERMINADO: number }
  pendientes_decision: number
  divergentes: number
  desaparecidos: number
  compras_erp_sin_llave: number
  monto_sin_fecha: number
  cuadratura_mensual: CuadraturaMes[]
  centros_sugeridos: string[]
}
interface Doc {
  id: number; uuid: string | null; tipo: number; folio: string | null; fecha: string | null
  rut: string | null; rut_formateado: string | null; emisor: string | null
  monto_efectivo: number; neto: number; iva: number; exento: number
  trx_sign: number; es_nota_credito: boolean
  exchange_status: string | null; estado_espejo: string
  // 'N/A' = fila de un documento DESAPARECIDO (H4): el SII ya no lo declara, así que el
  // cruce contra el ERP no aplica.
  cubeta: 'ESTA' | 'NO_ESTA' | 'INDETERMINADO' | 'N/A'
  // H1 (CRÍTICO): la leyenda del backend que avisa que ya hay una compra del mismo RUT
  // con el folio casi igual ('0004071' vs '4071'). Es lo ÚNICO que frena registrar dos
  // veces la misma factura y que Tesorería la pague dos veces: se pinta en la fila.
  cubeta_detalle: string | null
  decision: string | null; destino: string | null; decision_motivo: string | null
  divergente: boolean; divergencia_detalle: string | null
  regla_rut: string | null; regla_destino_default: string | null
}
interface Regla { id: number; rut: string; rut_formateado: string; nivel: string; destino_default: string | null; motivo: string | null }

// ─── Conciliación bancaria (matcher banco ↔ libro ↔ egresos; router_match.py) ────────
const matchAPI = {
  resumen: () => api.get('/sii-libro/match/resumen'),
  // La corrida recorre cartola + libro + egresos completos: puede exceder el timeout normal.
  correr: () => api.post('/sii-libro/match/correr', null, { timeout: 300_000 }),
  pendientes: (params: Record<string, unknown>) => api.get('/sii-libro/match/pendientes', { params }),
  confirmar: (id: number) => api.post(`/sii-libro/match/${id}/confirmar`),
  descartar: (id: number, motivo?: string) =>
    api.post(`/sii-libro/match/${id}/descartar`, motivo ? { motivo } : {}),
}

// Espejo de _serializar y /match/resumen del router del matcher.
interface MatchDoc {
  id: number; uuid: string | null; folio: string | null; tipo: number; fecha: string | null
  rut: string | null; rut_formateado: string | null; emisor: string | null
  monto_efectivo: number; estado_espejo: string
}
interface MatchMov {
  id: number; fecha: string | null; glosa: string | null; tipo: string | null
  monto: number; referencia: string | null; conciliado: boolean
}
// H6: los documentos que compiten por el mismo cargo del banco. El backend los manda
// desde siempre; hasta hoy la pantalla los tiraba a la basura (`unknown[]`) y el
// operador leía «3 documentos compiten» sin poder saber CUÁLES.
interface MatchCandidato {
  doc_id: number; uuid: string | null; folio: string | null
  rut: string | null; saldo: number | null
}
interface MatchItem {
  id: number; estado: string; via: string; origen: string; score: number | null
  grupo_uuid: string | null; monto_asignado: number
  motivo: string[]; candidatos: MatchCandidato[]
  divergente: boolean; divergencia_detalle: string | null; decidido_at: string | null
  doc: MatchDoc | null; movimiento: MatchMov | null
}
interface MatchResumen {
  autos_vivos: number; sugeridos: number; en_duda: number; conflictos: number
  confirmados: number; descartados: number; no_bancario: number
  pct_libro_conciliado_por_monto: number
  etiquetas: Record<string, number>; rut_en_glosa_activo: boolean
  ultima_corrida: {
    origen: string; exito: boolean | null; terminado_at: string | null; error: string | null
    sugeridos_nuevos: number | null; autos_nuevos: number | null
  } | null
}
// Pestañas de la sección → estado que sirve /match/pendientes. La pestaña "Autos"
// muestra el contador agregado de los automáticos vivos y lista los degradados a
// «en duda» (que sí piden decisión).
// H2 (CRÍTICO): "Conciliados" es la pestaña que faltaba. Tesorería frena el borrado de
// una cartola o de un movimiento diciendo «el match #412 está confirmado: deshágalo en
// el Libro SII», y hasta hoy ese match no aparecía en NINGUNA pantalla — el operador
// quedaba en un callejón sin salida (Tesorería lo mandaba acá y acá no estaba).
type MatchTab = 'sugeridos' | 'conflictos' | 'autos' | 'conciliados'
const ESTADO_TAB: Record<MatchTab, string> = {
  sugeridos: 'sugerido', conflictos: 'conflicto', autos: 'en_duda', conciliados: 'confirmado',
}

// ─── Etiquetas y estilos de dominio ──────────────────────────────────────────────────
const TIPO_DOC: Record<number, string> = {
  33: 'Factura', 34: 'Factura exenta', 39: 'Boleta', 43: 'Liquidación factura',
  46: 'Factura de compra', 56: 'Nota de débito', 61: 'Nota de crédito', 110: 'Factura export.',
}
const CUBETA: Record<string, { label: string; cls: string }> = {
  ESTA:          { label: 'Está en el ERP', cls: 'bg-emerald-500/10 text-emerald-500 border-emerald-500/20' },
  NO_ESTA:       { label: 'No está',        cls: 'bg-red-500/10 text-red-400 border-red-400/20' },
  INDETERMINADO: { label: 'Indeterminado',  cls: 'bg-amber-500/10 text-amber-500 border-amber-500/20' },
  // H4: cubeta de los documentos desaparecidos. No hay cruce que calcular — el SII dejó
  // de declararlos, así que la pregunta «¿está en el ERP?» ya no aplica.
  'N/A':         { label: 'El SII ya no lo declara', cls: 'bg-gray-500/10 text-gray-400 border-gray-400/20' },
}
const NIVEL_REGLA: Record<string, string> = {
  BLOQUEADO:    'bg-red-500/10 text-red-400 border-red-400/20',
  IGNORAR_AUTO: 'bg-gray-500/10 text-gray-400 border-gray-400/20',
  LOGISTICO:    'bg-blue-500/10 text-blue-400 border-blue-400/20',
}
const CC_LABEL: Record<string, string> = {
  'cc:financiero': 'Financiero', 'cc:distribucion': 'Distribución', 'cc:bodegaje': 'Bodegaje',
  'cc:administracion': 'Administración', 'cc:honorarios': 'Honorarios',
  'cc:intercompania': 'Intercompañía', 'cc:servicios_venta': 'Servicios de venta',
}
/** Etiqueta legible de un destino de clasificación ('cc:financiero' → "Financiero"). */
function destinoLabel(d: string | null | undefined): string {
  if (!d) return '—'
  if (d === 'costo_venta') return 'Costo por venta'
  if (d === 'activo_fijo') return 'Activo fijo'
  if (CC_LABEL[d]) return CC_LABEL[d]
  if (d.startsWith('cc:')) {
    const s = d.slice(3).replace(/_/g, ' ')
    return s.charAt(0).toUpperCase() + s.slice(1)
  }
  return d
}
/** Monto con signo tipográfico: las notas de crédito se ven NEGATIVAS (−$4.760.000). */
const fmtMonto = (n: number): string => (n < 0 ? '−' + fmtClp(-n) : fmtClp(n))
/** '765136806' → '76.513.680-6'. El backend manda el RUT canónico (sin puntos ni guión)
 *  en la lista de documentos que compiten (H6): el contador lee RUT con puntos. */
function fmtRutCanon(r: string | null): string {
  if (!r) return '—'
  return r.slice(0, -1).replace(/\B(?=(\d{3})+(?!\d))/g, '.') + '-' + r.slice(-1)
}
/** '2026-07' → 'jul 2026' (es-CL). */
function fmtMes(m: string): string {
  const [y, mo] = m.split('-').map(Number)
  if (!y || !mo) return m
  return new Date(y, mo - 1, 1).toLocaleDateString('es-CL', { month: 'short', year: 'numeric' })
}
/** Normaliza el error del backend a texto legible (mismo helper que FacturasPage: los 409
 *  del router EXPLICAN qué hacer — jamás taparlos con un mensaje genérico). */
function errMsg(e: any, fallback: string): string {
  const d = e?.response?.data?.detail
  if (Array.isArray(d)) return d.map((x: any) => x?.msg || JSON.stringify(x)).join('; ')
  if (typeof d === 'string') return d
  return fallback
}

// ─── UI compartida (mismo idioma visual que FacturasPage) ────────────────────────────
const inputCls = 'w-full px-3 py-2 rounded-lg border text-sm focus:outline-none focus:ring-2 focus:ring-brand-500/40'
const inputStyle = { backgroundColor: 'var(--surface-200)', borderColor: 'var(--border)', color: 'var(--text-primary)' } as React.CSSProperties
function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (<label className="block"><span className="block text-[11px] font-semibold uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>{label}</span>{children}</label>)
}
/** Opciones del selector de destino: los 2 fijos + los cc:* sugeridos por /resumen,
 *  más cualquier destino ya guardado que no esté en el catálogo (es ampliable).
 *
 *  Las etiquetas decían «capitaliza al embarque» y «se deprecia», en presente, como si
 *  clasificar moviera plata en alguna parte. Hoy NO la mueve: el destino se guarda (con
 *  quién y cuándo) y nada del ERP lo lee para costear — el costo del embarque y la
 *  depreciación se siguen calculando donde se calculan siempre. Prometer el efecto en
 *  presente es lo peor de los dos mundos: el contador cree que ya imputó y ni siquiera
 *  revisa el costo a mano. */
function opcionesDestino(centros: string[], extra?: (string | null)[]): { value: string; label: string }[] {
  const vals = ['costo_venta', 'activo_fijo', ...centros]
  for (const e of extra || []) if (e && !vals.includes(e)) vals.push(e)
  return vals.map(v => ({
    value: v,
    label: v === 'costo_venta' ? 'Costo por venta (queda marcado; todavía no se suma al costo del embarque)'
      : v === 'activo_fijo' ? 'Activo fijo (queda marcado; todavía no calcula depreciación)'
      : destinoLabel(v),
  }))
}

// ─── Fila de documento (expandible, con las acciones de decisión) ────────────────────
function DocRow({ doc, centros, onChanged }: { doc: Doc; centros: string[]; onChanged: () => void }) {
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [prefilling, setPrefilling] = useState(false)
  // Preselección: el destino ya decidido o, si hay regla del emisor, su default sugerido.
  const [destino, setDestino] = useState(doc.destino || doc.regla_destino_default || '')
  const [motivo, setMotivo] = useState('')
  const cub = CUBETA[doc.cubeta] ?? { label: doc.cubeta, cls: 'bg-gray-500/10 text-gray-400 border-gray-400/20' }

  // «Registrar compra»: pide el prefill al backend, lo deja en sessionStorage (no en la
  // URL: ahí no van datos de negocio) y navega al formulario NORMAL de Compras y Pagos.
  // No crea nada — el operador revisa y guarda allá, con todos los guards intactos.
  const registrarCompra = async () => {
    setPrefilling(true)
    try {
      const { data } = await siiLibroAPI.prefillCompra(doc.id)
      sessionStorage.setItem(PREFILL_COMPRA_SII_KEY, JSON.stringify(data))
      navigate('/compras-contab')
    } catch (e: any) {
      // El 409 (nota de crédito) explica el porqué: va tal cual. Solo se resetea el
      // estado en el error — en el éxito la fila se desmonta al navegar.
      toast.error(errMsg(e, 'No se pudo preparar el registro de la compra'), { duration: 9000 })
      setPrefilling(false)
    }
  }

  const decidir = async (accion: 'ignorar' | 'clasificar' | 'pendiente') => {
    if (accion === 'clasificar' && !destino) { toast.error('Elige un destino para clasificar'); return }
    setSaving(true)
    try {
      await siiLibroAPI.decidir(doc.id, {
        accion,
        destino: accion === 'clasificar' ? destino : undefined,
        motivo: motivo.trim() || undefined,
      })
      toast.success(accion === 'ignorar' ? 'Documento ignorado'
        : accion === 'clasificar' ? `Clasificado como ${destinoLabel(destino)}`
        : 'Documento devuelto a pendiente')
      setMotivo('')
      onChanged()
    } catch (e: any) {
      // Los 409 (BLOQUEADO→costo_venta, NC→costo_venta) explican qué hacer: van TAL CUAL.
      toast.error(errMsg(e, 'No se pudo guardar la decisión'), { duration: 9000 })
    } finally { setSaving(false) }
  }

  return (
    <>
      <tr className="hover:bg-[var(--surface-200)] transition-colors cursor-pointer focus:outline-none focus:ring-2 focus:ring-brand-500/40"
        role="button" tabIndex={0} aria-expanded={open}
        onClick={() => setOpen(o => !o)}
        onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setOpen(o => !o) } }}>
        <td className="px-4 py-3 whitespace-nowrap" style={{ color: 'var(--text-muted)' }}>
          <span className="inline-flex items-center gap-1">
            {open ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
            {fmtDate(doc.fecha)}
          </span>
        </td>
        <td className={`px-4 py-3 whitespace-nowrap text-xs font-medium ${doc.es_nota_credito ? 'text-red-400' : ''}`}
          style={doc.es_nota_credito ? {} : { color: 'var(--text-muted)' }}>
          {TIPO_DOC[doc.tipo] || `Doc ${doc.tipo}`} <span className="font-mono text-[10px]" style={{ color: 'var(--text-faint)' }}>({doc.tipo})</span>
        </td>
        <td className="px-4 py-3 font-mono font-semibold text-brand-400 whitespace-nowrap">{doc.folio || '—'}</td>
        <td className="px-4 py-3 max-w-[220px]">
          <p className="font-medium truncate" style={{ color: 'var(--text-primary)' }}>{doc.emisor || '—'}</p>
          <p className="text-xs font-mono" style={{ color: 'var(--text-faint)' }}>{doc.rut_formateado || doc.rut || '—'}</p>
        </td>
        <td className={`px-4 py-3 text-right font-semibold whitespace-nowrap ${doc.monto_efectivo < 0 ? 'text-red-400' : ''}`}
          style={doc.monto_efectivo < 0 ? {} : { color: 'var(--text-primary)' }}>
          {fmtMonto(doc.monto_efectivo)}
        </td>
        <td className="px-4 py-3">
          <span className={`inline-flex items-center px-2.5 py-1 rounded-full text-xs font-medium border ${cub.cls}`}>{cub.label}</span>
          {/* H1 (CRÍTICO): el aviso del folio parecido va EN LA FILA, no en un tooltip.
              Es el único freno entre el operador y registrar por segunda vez una factura
              que ya está en el ERP tecleada distinta ('0004071' vs '4071') — y que
              Tesorería la pague dos veces. Nadie pasa el mouse por 200 filas. */}
          {doc.cubeta === 'INDETERMINADO' && doc.cubeta_detalle && (
            <p className="mt-1.5 flex items-start gap-1 text-[11px] leading-snug text-amber-500 max-w-[260px] whitespace-normal">
              <AlertTriangle className="w-3 h-3 mt-0.5 shrink-0" />
              <span>Ojo: {doc.cubeta_detalle}</span>
            </p>
          )}
        </td>
        <td className="px-4 py-3 whitespace-nowrap">
          {doc.decision === null && <span className="inline-flex items-center px-2.5 py-1 rounded-full text-xs font-medium border bg-amber-500/10 text-amber-500 border-amber-500/20">Pendiente</span>}
          {doc.decision === 'ignorado' && <span className="inline-flex items-center px-2.5 py-1 rounded-full text-xs font-medium border bg-gray-500/10 text-gray-400 border-gray-400/20">Ignorado</span>}
          {doc.decision === 'clasificado' && <span className="inline-flex items-center px-2.5 py-1 rounded-full text-xs font-medium border bg-emerald-500/10 text-emerald-500 border-emerald-500/20">{destinoLabel(doc.destino)}</span>}
        </td>
        <td className="px-4 py-3 text-center">
          {doc.divergente && (
            <span title={doc.divergencia_detalle || 'El documento cambió en el SII después de decidirlo'}>
              <AlertTriangle className="w-4 h-4 text-amber-500 inline" />
            </span>
          )}
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={8} className="px-4 pb-4" style={{ backgroundColor: 'var(--surface-100)' }}>
            <div className="grid md:grid-cols-3 gap-4 pt-3" onClick={e => e.stopPropagation()}>
              {/* Detalle del documento */}
              <div className="md:col-span-2 space-y-2">
                <div className="flex flex-wrap gap-5 text-xs" style={{ color: 'var(--text-muted)' }}>
                  <span>Neto: <b style={{ color: 'var(--text-primary)' }}>{fmtMonto(doc.neto)}</b></span>
                  <span>IVA: <b style={{ color: 'var(--text-primary)' }}>{fmtMonto(doc.iva)}</b></span>
                  {doc.exento !== 0 && <span>Exento: <b style={{ color: 'var(--text-primary)' }}>{fmtMonto(doc.exento)}</b></span>}
                  <span>Efectivo: <b className={doc.monto_efectivo < 0 ? 'text-red-400' : 'text-brand-400'}>{fmtMonto(doc.monto_efectivo)}</b></span>
                </div>
                {doc.es_nota_credito && (
                  <p className="text-xs text-red-400">
                    Nota de crédito: resta del libro. No se clasifica como costo por venta en esta fase
                    (reduce el gasto del período o espera la fase de vínculo con embarques).
                  </p>
                )}
                {/* H1 (CRÍTICO): el mismo aviso, abierto y con la receta de qué hacer.
                    Mismo formato que el recuadro de divergencia de abajo. */}
                {doc.cubeta === 'INDETERMINADO' && doc.cubeta_detalle && (
                  <div className="rounded-xl border p-3 bg-amber-500/10 border-amber-500/30">
                    <p className="text-xs font-semibold text-amber-500 flex items-center gap-1.5">
                      <AlertTriangle className="w-3.5 h-3.5 shrink-0" /> Ojo: revisa antes de registrarla — puede que esta factura ya esté en el sistema
                    </p>
                    <p className="text-xs mt-1" style={{ color: 'var(--text-muted)' }}>{doc.cubeta_detalle}</p>
                    <p className="text-xs mt-1" style={{ color: 'var(--text-muted)' }}>
                      Si es la misma factura, <b style={{ color: 'var(--text-primary)' }}>no la registre de nuevo</b>: búsquela
                      en Compras y Pagos por el RUT del proveedor y, si el N° quedó mal tecleado, corríjalo allá.
                    </p>
                  </div>
                )}
                {doc.divergente && (
                  <div className="rounded-xl border p-3 bg-amber-500/10 border-amber-500/30">
                    <p className="text-xs font-semibold text-amber-500 flex items-center gap-1.5">
                      <AlertTriangle className="w-3.5 h-3.5 shrink-0" /> Cambió en el SII después de decidirlo
                    </p>
                    <p className="text-xs mt-1" style={{ color: 'var(--text-muted)' }}>{doc.divergencia_detalle || 'Sin detalle de la divergencia'}</p>
                  </div>
                )}
                {doc.decision && (
                  <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
                    Decisión actual: <b style={{ color: 'var(--text-primary)' }}>{doc.decision === 'ignorado' ? 'Ignorado' : `Clasificado → ${destinoLabel(doc.destino)}`}</b>
                    {doc.decision_motivo && <> · Motivo: <i>{doc.decision_motivo}</i></>}
                  </p>
                )}
                {doc.regla_rut && (
                  <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
                    Regla del emisor:{' '}
                    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-medium border ${NIVEL_REGLA[doc.regla_rut] || ''}`}>{doc.regla_rut}</span>
                    {doc.regla_destino_default && <> · default sugerido: <b style={{ color: 'var(--text-primary)' }}>{destinoLabel(doc.regla_destino_default)}</b></>}
                  </p>
                )}
              </div>
              {/* Acciones de decisión */}
              <div className="space-y-2">
                {/* Registrar en Compras y Pagos: la salida natural de un NO_ESTA (y de un
                    INDETERMINADO bajo responsabilidad del operador). Oculto para la NC:
                    el backend igual responde 409, esto solo evita el viaje inútil. */}
                {(doc.cubeta === 'NO_ESTA' || doc.cubeta === 'INDETERMINADO') && !doc.es_nota_credito && (
                  <button onClick={registrarCompra} disabled={prefilling || saving}
                    // H1: si el backend explicó POR QUÉ dudó (el folio parecido), ese es
                    // el motivo real — el texto genérico («compras sin llave») quedaba
                    // mintiendo justo en el caso peligroso.
                    title={doc.cubeta_detalle
                      ? doc.cubeta_detalle
                      : doc.cubeta === 'INDETERMINADO'
                      ? 'Hay compras del ERP sin llave que PODRÍAN ser este documento: registrarlo igual puede duplicar — revisa antes de guardar'
                      : 'Abre el formulario de Compras y Pagos pre-llenado con este documento; revisas y guardas allá (mismos controles de siempre)'}
                    className="btn-primary w-full flex items-center justify-center gap-2 text-xs disabled:opacity-50 disabled:cursor-not-allowed">
                    {prefilling ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Wallet className="w-3.5 h-3.5" />}
                    {doc.cubeta === 'INDETERMINADO' ? 'Registrar igualmente…' : 'Registrar compra'}
                  </button>
                )}
                <Field label="Destino de clasificación">
                  <select className={inputCls} style={inputStyle} value={destino} onChange={e => setDestino(e.target.value)}>
                    <option value="">Selecciona destino…</option>
                    {opcionesDestino(centros, [doc.destino, doc.regla_destino_default]).map(o => (
                      <option key={o.value} value={o.value}>{o.label}</option>
                    ))}
                  </select>
                </Field>
                {/* Qué hace y qué NO hace clasificar. Sin esta línea el operador cree que
                    el número ya se imputó y no revisa nada a mano. */}
                <p className="text-[11px] leading-snug" style={{ color: 'var(--text-faint)' }}>
                  Clasificar deja registrado —con quién y cuándo— qué es este documento: esa decisión
                  queda auditada y es la base del costeo automático que viene más adelante. Todavía no
                  mueve números: el costo del embarque y la depreciación se siguen calculando como hasta ahora.
                </p>
                <Field label="Motivo (opcional)">
                  <input className={inputCls} style={inputStyle} value={motivo} onChange={e => setMotivo(e.target.value)} placeholder="Por qué se decide así" maxLength={400} />
                </Field>
                <button onClick={() => decidir('clasificar')} disabled={saving}
                  className="btn-primary w-full flex items-center justify-center gap-2 text-xs disabled:opacity-50 disabled:cursor-not-allowed">
                  {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <CheckCircle2 className="w-3.5 h-3.5" />} Clasificar
                </button>
                <button onClick={() => decidir('ignorar')} disabled={saving}
                  className="btn-secondary w-full flex items-center justify-center gap-2 text-xs disabled:opacity-50 disabled:cursor-not-allowed">
                  <X className="w-3.5 h-3.5" /> Ignorar (no es del ERP)
                </button>
                {doc.decision !== null && (
                  <button onClick={() => decidir('pendiente')} disabled={saving}
                    className="w-full flex items-center justify-center gap-1.5 text-xs text-amber-500 hover:bg-amber-500/10 rounded-lg py-1.5 disabled:opacity-50">
                    <Clock className="w-3.5 h-3.5" /> Volver a pendiente
                  </button>
                )}
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  )
}

// ─── Conciliación bancaria: score + fila del matcher ─────────────────────────────────
const chipCls = 'inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-medium border'

/** Score como número + barra: verde ≥90, azul ≥75, gris <75; sin score (conflictos) → "—". */
function ScoreChip({ score }: { score: number | null }) {
  if (score == null) return <span className="text-xs" style={{ color: 'var(--text-faint)' }}>—</span>
  const txt = score >= 90 ? 'text-emerald-500' : score >= 75 ? 'text-blue-400' : 'text-gray-400'
  const bar = score >= 90 ? 'bg-emerald-500' : score >= 75 ? 'bg-blue-400' : 'bg-gray-400'
  return (
    <div className="flex items-center gap-1.5" title={`Score ${score}/100 (cache de pantalla: confirmar revalida todo en el backend)`}>
      <span className={`text-xs font-bold ${txt}`}>{score}</span>
      <div className="w-10 h-1.5 rounded-full overflow-hidden shrink-0" style={{ backgroundColor: 'var(--surface-300)' }}>
        <div className={`h-full rounded-full ${bar}`} style={{ width: `${Math.min(100, Math.max(0, score))}%` }} />
      </div>
    </div>
  )
}

/** Una fila del matcher: las DOS puntas lado a lado (movimiento | documento), el monto
 *  asignado, el score y los motivos como chips.
 *
 *  Acciones por estado: sugerido / en duda / CONFLICTO → Confirmar + Descartar;
 *  ya conciliado → Deshacer la conciliación.
 *  · H6: el conflicto también se confirma. El backend siempre lo aceptó (revalida todos
 *    los topes bajo candado); era esta pantalla la que escondía el botón y dejaba
 *    «descartar» —que entierra el par para siempre— como única salida, aunque el
 *    operador tuviera la factura en la mano y supiera perfectamente cuál era.
 *  · H2: la fila de un conciliado ofrece deshacerlo, que es lo que Tesorería pide
 *    cuando frena el borrado de una cartola o de un movimiento. */
function MatchRow({ m, actingId, onConfirmar, onDescartar }: {
  m: MatchItem
  actingId: number | null
  onConfirmar: (m: MatchItem) => void
  onDescartar: (m: MatchItem) => void
}) {
  const [verCandidatos, setVerCandidatos] = useState(false)
  const saving = actingId === m.id
  const conciliado = m.estado === 'confirmado'
  const puedeConfirmar = m.estado === 'sugerido' || m.estado === 'en_duda' || m.estado === 'conflicto'
  // H6: la lista de rivales solo tiene sentido cuando hay más de uno (en un sugerido
  // normal el único candidato es el propio documento de la fila).
  const candidatos = m.candidatos || []
  const hayCompetencia = candidatos.length > 1
  return (
    <>
    <tr className="border-b last:border-0" style={{ borderColor: 'var(--border)' }}>
      {/* Punta 1: el movimiento bancario */}
      <td className="px-4 py-3 max-w-[230px]">
        {m.movimiento ? (
          <>
            <p className="text-xs whitespace-nowrap" style={{ color: 'var(--text-muted)' }}>{fmtDate(m.movimiento.fecha)}</p>
            <p className="text-sm font-medium truncate" title={m.movimiento.glosa || undefined} style={{ color: 'var(--text-primary)' }}>{m.movimiento.glosa || '—'}</p>
            <p className="text-xs font-semibold text-brand-400">{fmtMonto(m.movimiento.monto)}</p>
          </>
        ) : (
          <p className="text-xs text-red-400">Movimiento eliminado (cartola borrada): re-importe la cartola antes de confirmar</p>
        )}
      </td>
      <td className="px-1 py-3 text-center"><ArrowLeftRight className="w-3.5 h-3.5 inline" style={{ color: 'var(--text-faint)' }} /></td>
      {/* Punta 2: el documento del libro SII */}
      <td className="px-4 py-3 max-w-[230px]">
        {m.doc ? (
          <>
            <p className="text-xs whitespace-nowrap" style={{ color: 'var(--text-muted)' }}>
              <span className="font-mono font-semibold text-brand-400">{m.doc.folio || '—'}</span>
              {' · '}{TIPO_DOC[m.doc.tipo] || `Doc ${m.doc.tipo}`}{m.doc.fecha ? ` · ${fmtDate(m.doc.fecha)}` : ''}
            </p>
            <p className="text-sm font-medium truncate" title={m.doc.emisor || undefined} style={{ color: 'var(--text-primary)' }}>{m.doc.emisor || '—'}</p>
            <p className="text-xs font-semibold" style={{ color: 'var(--text-primary)' }}>{fmtMonto(m.doc.monto_efectivo)}</p>
          </>
        ) : (
          <p className="text-xs" style={{ color: 'var(--text-faint)' }}>—</p>
        )}
      </td>
      <td className="px-4 py-3 text-right whitespace-nowrap text-sm font-semibold" style={{ color: 'var(--text-primary)' }}
        title="Monto asignado del cruce (en subsets o parciales puede ser menor al total del documento)">
        {fmtMonto(m.monto_asignado)}
      </td>
      <td className="px-4 py-3"><ScoreChip score={m.score} /></td>
      <td className="px-4 py-3">
        <div className="flex flex-wrap items-center gap-1 max-w-[300px]">
          {/* H2: el N° del match — el mismo que nombra Tesorería al frenar un borrado
              («el match #412 está confirmado»). Sin este número la pantalla no servía
              para encontrar el que hay que deshacer. */}
          {conciliado && (
            <>
              <span className={`${chipCls} bg-emerald-500/10 text-emerald-500 border-emerald-500/20`}
                title={m.origen === 'auto'
                  ? 'Lo concilió el sistema solo, porque se cumplieron todas las condiciones duras'
                  : 'Lo confirmó una persona desde esta pantalla'}>
                CONCILIADO{m.origen === 'auto' ? ' (automático)' : ''}
              </span>
              <span className={`${chipCls} bg-gray-500/10 text-gray-400 border-gray-400/20 font-mono`}
                title="Este es el número que nombra Tesorería cuando no deja borrar una cartola o un movimiento">
                N° {m.id}
              </span>
            </>
          )}
          {m.estado === 'en_duda' && (
            <span className={`${chipCls} bg-amber-500/10 text-amber-500 border-amber-500/20`}
              title="El motor la degradó (o caducó): dejó de cumplirse alguna condición — revisar y decidir a mano">EN DUDA</span>
          )}
          {m.estado === 'conflicto' && (
            <span className={`${chipCls} bg-red-500/10 text-red-400 border-red-400/20`}
              title="Este cargo del banco quedó en conflicto: el motivo de la fila dice por qué (varios documentos calzan con él, o lo que dice el libro contradice la conciliación que ya tiene en Tesorería). Si hay más de un documento compitiendo vas a ver el enlace para mirarlos. Confirma el que corresponde a la factura que tienes en la mano; si ninguno corresponde, descártalo.">CONFLICTO</span>
          )}
          {m.grupo_uuid && (
            <span className={`${chipCls} bg-blue-500/10 text-blue-400 border-blue-400/20`}
              title="Se confirma o descarta junto con sus filas hermanas (combo factura−nota de crédito o agregado mensual): el grupo es atómico">GRUPO</span>
          )}
          {m.divergente && (
            <span title={m.divergencia_detalle || 'El documento cambió después de la sugerencia'}>
              <AlertTriangle className="w-3.5 h-3.5 text-amber-500 inline" />
            </span>
          )}
          {m.motivo.map((r, i) => (
            <span key={i} className={`${chipCls} bg-gray-500/10 text-gray-400 border-gray-400/20 max-w-[240px]`} title={r}>
              <span className="min-w-0 truncate">{r}</span>
            </span>
          ))}
          {/* H6: «3 documentos compiten» sin decir cuáles no sirve de nada. */}
          {hayCompetencia && (
            <button onClick={() => setVerCandidatos(v => !v)}
              className="text-[10px] font-semibold underline underline-offset-2 text-brand-400 hover:opacity-80">
              {verCandidatos ? 'Ocultar los documentos que compiten'
                : `Ver los ${candidatos.length} documentos que compiten`}
            </button>
          )}
        </div>
      </td>
      <td className="px-4 py-3 whitespace-nowrap">
        <div className="flex items-center justify-end gap-1.5">
          {puedeConfirmar && (
            <button onClick={() => onConfirmar(m)} disabled={saving}
              className="btn-primary flex items-center gap-1 text-xs disabled:opacity-50 disabled:cursor-not-allowed">
              {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <CheckCircle2 className="w-3.5 h-3.5" />} Confirmar
            </button>
          )}
          {/* H2: en un conciliado, «descartar» ES el deshacer — y así hay que llamarlo:
              libera el movimiento en Tesorería para poder desconciliarlo o borrar la
              cartola. Con el nombre viejo nadie iba a adivinar que este era el botón. */}
          <button onClick={() => onDescartar(m)} disabled={saving}
            title={conciliado
              ? 'Deshace la conciliación: el movimiento del banco queda libre en Tesorería y el par no se vuelve a sugerir'
              : 'Entierra el par: el motor no vuelve a sugerir este cruce'}
            className="btn-secondary flex items-center gap-1 text-xs disabled:opacity-50 disabled:cursor-not-allowed">
            <X className="w-3.5 h-3.5" /> {conciliado ? 'Deshacer conciliación' : 'Descartar'}
          </button>
        </div>
      </td>
    </tr>
    {/* H6: la tabla de rivales, con lo justo para elegir: folio, RUT y lo que le queda
        sin cruzar a cada documento. */}
    {verCandidatos && hayCompetencia && (
      <tr className="border-b last:border-0" style={{ borderColor: 'var(--border)' }}>
        <td colSpan={7} className="px-4 pb-4 pt-1" style={{ backgroundColor: 'var(--surface-200)' }}>
          <p className="text-xs mb-2" style={{ color: 'var(--text-muted)' }}>
            Estos documentos del libro calzan con el mismo cargo del banco. Si tienes la factura
            a mano y sabes cuál es, confírmalo desde su propia fila: el sistema revisa de nuevo
            los montos antes de aceptarlo.
          </p>
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b" style={{ borderColor: 'var(--border)' }}>
                {['Folio', 'RUT del emisor', 'Le queda sin cruzar'].map((h, i) => (
                  <th key={h} className={`py-1.5 pr-4 font-semibold uppercase tracking-wider ${i === 2 ? 'text-right' : 'text-left'}`}
                    style={{ color: 'var(--text-faint)' }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {candidatos.map(c => (
                <tr key={c.doc_id}>
                  <td className="py-1.5 pr-4 font-mono font-semibold text-brand-400 whitespace-nowrap">
                    {c.folio || '—'}
                    {m.doc && c.doc_id === m.doc.id && (
                      <span className="ml-1.5 font-sans text-[10px] font-normal" style={{ color: 'var(--text-faint)' }}>
                        (el de esta fila)
                      </span>
                    )}
                  </td>
                  <td className="py-1.5 pr-4 font-mono whitespace-nowrap" style={{ color: 'var(--text-muted)' }}>{fmtRutCanon(c.rut)}</td>
                  <td className="py-1.5 text-right whitespace-nowrap" style={{ color: 'var(--text-primary)' }}>
                    {c.saldo == null ? '—' : fmtMonto(c.saldo)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </td>
      </tr>
    )}
    </>
  )
}

// ─── Página ──────────────────────────────────────────────────────────────────────────
const PAGE_SIZE = 50

export default function LibroSiiPage() {
  const [resumen, setResumen] = useState<Resumen | null>(null)
  const [docs, setDocs] = useState<Doc[]>([])
  const [total, setTotal] = useState(0)
  const [loadingDocs, setLoadingDocs] = useState(true)
  const [syncing, setSyncing] = useState(false)
  const [exporting, setExporting] = useState(false)
  // Filtros de la bandeja (el backend filtra; la página solo pide)
  const [cubeta, setCubeta] = useState('')
  const [decision, setDecision] = useState('')
  const [rutInput, setRutInput] = useState('')
  const [rutApplied, setRutApplied] = useState('')
  const [soloDiv, setSoloDiv] = useState(false)
  // H4: ACTIVO (lo que el SII declara hoy) o DESAPARECIDO (lo que declaraba y dejó de
  // declarar). El tablero anunciaba «+3 desaparecidos del SII» y no había ninguna
  // pantalla donde verlos: era un contador y nada más.
  const [estadoDocs, setEstadoDocs] = useState<'ACTIVO' | 'DESAPARECIDO'>('ACTIVO')
  const [page, setPage] = useState(1)
  // Conciliación bancaria (matcher)
  const [mResumen, setMResumen] = useState<MatchResumen | null>(null)
  const [matchOpen, setMatchOpen] = useState(false)
  const [matchTab, setMatchTab] = useState<MatchTab>('sugeridos')
  const [matchItems, setMatchItems] = useState<MatchItem[]>([])
  const [matchTotal, setMatchTotal] = useState(0)
  const [matchLoading, setMatchLoading] = useState(false)
  const [matchPage, setMatchPage] = useState(1)
  const [running, setRunning] = useState(false)
  const [actingId, setActingId] = useState<number | null>(null)
  const [descartando, setDescartando] = useState<MatchItem | null>(null)
  const [descarteMotivo, setDescarteMotivo] = useState('')
  // H2: confirmar deja el movimiento conciliado y es lo que después traba el borrado de
  // la cartola en Tesorería — o sea, la acción MÁS pesada de la sección era la única sin
  // pregunta previa (descartar, que es menos grave, sí abría un cuadro).
  const [confirmando, setConfirmando] = useState<MatchItem | null>(null)
  // H2: buscador por N° de match para la pestaña «Conciliados» — Tesorería frena el
  // borrado nombrando un número («match #412») y hay que poder llegar a ESA fila.
  const [matchIdInput, setMatchIdInput] = useState('')
  const [matchIdApplied, setMatchIdApplied] = useState('')
  const matchRef = useRef<HTMLDivElement | null>(null)
  // Reglas por emisor
  const [reglas, setReglas] = useState<Regla[]>([])
  const [reglasOpen, setReglasOpen] = useState(false)
  const [reglaRut, setReglaRut] = useState('')
  const [reglaNivel, setReglaNivel] = useState('LOGISTICO')
  const [reglaDestino, setReglaDestino] = useState('')
  const [reglaMotivo, setReglaMotivo] = useState('')
  const [reglaSaving, setReglaSaving] = useState(false)

  const loadResumen = useCallback(async () => {
    try { setResumen((await siiLibroAPI.resumen()).data) }
    catch (e: any) { toast.error(errMsg(e, 'No se pudo cargar el tablero del libro')) }
  }, [])
  const loadDocs = useCallback(async () => {
    setLoadingDocs(true)
    try {
      const { data } = await siiLibroAPI.documentos({
        cubeta: cubeta || undefined,
        decision: decision || undefined,
        rut: rutApplied || undefined,
        solo_divergentes: soloDiv || undefined,
        estado: estadoDocs,   // H4: ACTIVO por defecto; DESAPARECIDO cuando se pide
        page, page_size: PAGE_SIZE,
      })
      setDocs(data.items); setTotal(data.total)
    } catch (e: any) {
      // El 400 del filtro RUT ("'x' no tiene forma de RUT") explica el problema: tal cual.
      toast.error(errMsg(e, 'No se pudo cargar la bandeja'), { duration: 6000 })
    } finally { setLoadingDocs(false) }
  }, [cubeta, decision, rutApplied, soloDiv, estadoDocs, page])
  const loadReglas = useCallback(async () => {
    try { setReglas((await siiLibroAPI.reglas()).data) }
    catch { /* la sección es secundaria: la lista queda vacía y el resto de la página vive */ }
  }, [])
  const loadMatchResumen = useCallback(async () => {
    try { setMResumen((await matchAPI.resumen()).data) }
    catch { /* tarjeta y sección quedan en "—"; el resto de la página vive */ }
  }, [])
  const loadMatchPend = useCallback(async () => {
    if (!matchOpen) return   // la bandeja del matcher se carga recién al abrir la sección
    setMatchLoading(true)
    try {
      const { data } = await matchAPI.pendientes({
        estado: ESTADO_TAB[matchTab], page: matchPage, page_size: PAGE_SIZE,
        // H2: solo la pestaña «Conciliados» busca por N° (es la única donde Tesorería
        // manda con un número en la mano).
        match_id: matchTab === 'conciliados' && matchIdApplied ? matchIdApplied : undefined,
      })
      setMatchItems(data.items); setMatchTotal(data.total)
    } catch (e: any) {
      toast.error(errMsg(e, 'No se pudo cargar la bandeja del matcher'), { duration: 6000 })
    } finally { setMatchLoading(false) }
  }, [matchOpen, matchTab, matchPage, matchIdApplied])

  useEffect(() => { loadResumen(); loadReglas(); loadMatchResumen() }, [loadResumen, loadReglas, loadMatchResumen])
  useEffect(() => { loadDocs() }, [loadDocs])
  useEffect(() => { loadMatchPend() }, [loadMatchPend])
  // Una decisión mueve contadores del tablero (pendientes, cubetas): se refrescan juntos.
  const reloadAll = () => { loadDocs(); loadResumen() }
  // Confirmar/descartar/correr mueve contadores Y bandeja del matcher: juntos también.
  const reloadMatch = () => { loadMatchResumen(); loadMatchPend() }

  const sincronizar = async () => {
    setSyncing(true)
    try {
      const { data } = await siiLibroAPI.sincronizar()
      if (data.exito) {
        toast.success(`Barrido completo: ${data.total_api ?? 0} documentos en el SII · ${data.nuevos ?? 0} nuevos · ${data.actualizados ?? 0} actualizados${data.desaparecidos ? ` · ${data.desaparecidos} desaparecidos` : ''}`, { duration: 7000 })
      } else {
        toast.error(data.error || 'El barrido terminó con error', { duration: 9000 })
      }
      reloadAll()
    } catch (e: any) {
      // El 409 ("ya hay un barrido corriendo") se muestra tal cual: explica qué pasa.
      toast.error(errMsg(e, 'No se pudo sincronizar con el SII'), { duration: 9000 })
    } finally { setSyncing(false) }
  }

  const exportar = async () => {
    setExporting(true)
    try {
      const desap = estadoDocs === 'DESAPARECIDO'
      const r = await siiLibroAPI.exportCsv({ estado: estadoDocs })
      const blob = new Blob([r.data], { type: 'text/csv' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      // H4: nombre distinto por vista — el archivo de los desaparecidos no puede
      // llamarse igual que el de los faltantes.
      a.download = desap ? 'libro-sii-desaparecidos.csv' : 'libro-sii-faltantes.csv'
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (e: any) { toast.error(errMsg(e, 'No se pudo exportar el CSV')) }
    finally { setExporting(false) }
  }

  const guardarRegla = async () => {
    if (!reglaRut.trim()) { toast.error('Ingresa el RUT del emisor'); return }
    setReglaSaving(true)
    try {
      await siiLibroAPI.upsertRegla({
        rut: reglaRut.trim(), nivel: reglaNivel,
        destino_default: reglaDestino || undefined,
        motivo: reglaMotivo.trim() || undefined,
      })
      toast.success('Regla guardada')
      setReglaRut(''); setReglaDestino(''); setReglaMotivo('')
      loadReglas(); loadDocs()   // la regla cambia los defaults sugeridos de la bandeja
    } catch (e: any) { toast.error(errMsg(e, 'No se pudo guardar la regla'), { duration: 8000 }) }
    finally { setReglaSaving(false) }
  }

  // ─── Acciones de la conciliación bancaria ──────────────────────────────────────────
  const enfocarMatch = () => {
    setMatchOpen(true)
    // El scroll espera al render de la sección recién abierta.
    setTimeout(() => matchRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 60)
  }

  const correrMatcher = async () => {
    setRunning(true)
    try {
      const { data } = await matchAPI.correr()
      if (data.exito) {
        toast.success(
          `Matcher: ${data.autos_nuevos ?? 0} autos nuevos · ${data.sugeridos_nuevos ?? 0} sugeridos nuevos · ${data.conflictos ?? 0} conflictos` +
          `${data.degradados_en_duda ? ` · ${data.degradados_en_duda} degradados a en duda` : ''}` +
          `${data.caducados ? ` · ${data.caducados} caducados` : ''}`,
          { duration: 7000 })
      } else {
        toast.error(data.error || 'La corrida del matcher terminó con error', { duration: 9000 })
      }
      reloadMatch()
    } catch (e: any) {
      // El 409 («hay un barrido/corrida en curso») se muestra tal cual: explica qué pasa.
      toast.error(errMsg(e, 'No se pudo correr el matcher'), { duration: 9000 })
    } finally { setRunning(false) }
  }

  // H2: se dispara desde el cuadro de confirmación, nunca de un clic suelto.
  const confirmarMatch = async () => {
    const m = confirmando
    if (!m) return
    setActingId(m.id)
    try {
      await matchAPI.confirmar(m.id)
      toast.success(m.grupo_uuid
        ? 'Conciliado (grupo completo): el movimiento del banco queda conciliado en Tesorería'
        : 'Conciliado: el movimiento del banco queda conciliado en Tesorería')
      setConfirmando(null)
      reloadMatch()
    } catch (e: any) {
      // Los 409 del backend EXPLICAN qué pasó (saldos, doc cambiado, carrera con
      // Tesorería…): van tal cual, jamás tapados con un mensaje genérico.
      toast.error(errMsg(e, 'No se pudo confirmar el match'), { duration: 9000 })
    } finally { setActingId(null) }
  }

  const descartarMatch = async () => {
    if (!descartando) return
    const eraConciliado = descartando.estado === 'confirmado'
    setActingId(descartando.id)
    try {
      await matchAPI.descartar(descartando.id, descarteMotivo.trim() || undefined)
      // H2: el mismo endpoint hace las dos cosas, pero lo que el operador acaba de
      // hacer NO es lo mismo — y lo que sigue (desconciliar, borrar la cartola) tampoco.
      toast.success(eraConciliado
        ? 'Conciliación deshecha: el movimiento quedó libre en Tesorería (ya puedes desconciliarlo o borrar la cartola)'
        : 'Cruce descartado: el sistema no volverá a sugerir este par', { duration: 7000 })
      setDescartando(null); setDescarteMotivo('')
      reloadMatch()
    } catch (e: any) {
      toast.error(errMsg(e, 'No se pudo descartar el match'), { duration: 9000 })
    } finally { setActingId(null) }
  }

  // Filtros: cualquier cambio vuelve a la página 1
  const setFiltroCubeta = (c: string) => { setCubeta(prev => prev === c ? '' : c); setPage(1) }
  const setFiltroDecision = (d: string) => { setDecision(prev => prev === d ? '' : d); setPage(1) }
  const aplicarRut = () => { setRutApplied(rutInput.trim()); setPage(1) }
  const limpiarRut = () => { setRutInput(''); setRutApplied(''); setPage(1) }
  // H4: entrar o salir de la lista de desaparecidos. Al entrar se limpia el filtro de
  // cubeta: ahí la cubeta es siempre la misma («El SII ya no lo declara») y un filtro
  // viejo puesto dejaría la lista vacía sin que nadie entienda por qué.
  const verEstado = (e: 'ACTIVO' | 'DESAPARECIDO') => { setEstadoDocs(e); setCubeta(''); setPage(1) }

  const edadHoras = resumen?.edad_horas_ultimo_exitoso
  const edadCritica = resumen?.edad_critica ?? true
  const edadColor = edadCritica ? 'text-red-400' : (edadHoras ?? 0) >= 24 ? 'text-amber-400' : 'text-emerald-500'
  const edadDot = edadCritica ? 'bg-red-400' : (edadHoras ?? 0) >= 24 ? 'bg-amber-400' : 'bg-emerald-400'
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const matchTotalPages = Math.max(1, Math.ceil(matchTotal / PAGE_SIZE))
  const centros = resumen?.centros_sugeridos ?? []
  // H3: lo que el ERP compró y NUNCA va a aparecer en el libro del SII (importaciones,
  // otra moneda, gastos sin documento tributario), sumado en los meses que se muestran.
  const erpFueraLibro = (resumen?.cuadratura_mensual ?? [])
    .reduce((acc, c) => acc + (c.erp_fuera_libro ?? 0), 0)
  const verDesaparecidos = estadoDocs === 'DESAPARECIDO'

  const tarjetasCubeta = [
    { key: 'ESTA', icon: CheckCircle2, label: 'Está en el ERP', color: 'text-emerald-500', valor: resumen?.cubetas.ESTA ?? 0, sub: 'con compra activa que calza' },
    { key: 'NO_ESTA', icon: AlertCircle, label: 'No está', color: 'text-red-400', valor: resumen?.cubetas.NO_ESTA ?? 0, sub: 'la deuda invisible' },
    { key: 'INDETERMINADO', icon: HelpCircle, label: 'Indeterminado', color: 'text-amber-500', valor: resumen?.cubetas.INDETERMINADO ?? 0, sub: `${resumen?.compras_erp_sin_llave ?? 0} compras del ERP sin llave` },
  ]

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-2xl font-bold" style={{ color: 'var(--text-primary)' }}>Libro de Compras SII</h1>
          <p className="text-sm mt-1" style={{ color: 'var(--text-muted)' }}>
            Qué facturas de proveedor existen ante el SII y no están registradas en el ERP · clasificación por documento
          </p>
        </div>
        <div className="flex items-center gap-2 self-start">
          {/* H4: el botón exporta LO QUE SE ESTÁ MIRANDO — antes siempre mandaba los
              activos, aunque la pantalla mostrara los desaparecidos. */}
          <button onClick={exportar} disabled={exporting} className="btn-secondary flex items-center gap-2 disabled:opacity-50">
            {exporting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
            {verDesaparecidos ? 'Exportar desaparecidos (CSV)' : 'Exportar faltantes (CSV)'}
          </button>
          <button onClick={reloadAll} title="Refrescar la pantalla" className="btn-secondary flex items-center gap-2"><RefreshCw className="w-4 h-4" /></button>
        </div>
      </div>

      {/* Tablero: documentos + cubetas clicables + pendientes + divergentes */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
        <div className="rounded-2xl p-3 sm:p-4 border" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
          <div className="w-8 h-8 rounded-xl flex items-center justify-center mb-2" style={{ backgroundColor: 'var(--surface-300)' }}><BookOpenCheck className="w-4 h-4 text-brand-400" /></div>
          <p className="text-[10px] uppercase tracking-widest leading-tight" style={{ color: 'var(--text-faint)' }}>Documentos activos</p>
          <p className="text-lg sm:text-xl font-bold mt-0.5 text-brand-400">{resumen?.documentos_activos ?? '—'}</p>
        </div>
        {tarjetasCubeta.map(t => (
          <button key={t.key} onClick={() => setFiltroCubeta(t.key)}
            title={cubeta === t.key ? 'Quitar filtro' : 'Filtrar la bandeja por esta cubeta'}
            className={`rounded-2xl p-3 sm:p-4 border text-left transition-all hover:border-brand-600/40 ${cubeta === t.key ? 'ring-2 ring-brand-500/40 border-brand-500' : ''}`}
            style={{ backgroundColor: 'var(--surface-100)', borderColor: cubeta === t.key ? undefined : 'var(--border)' }}>
            <div className="w-8 h-8 rounded-xl flex items-center justify-center mb-2" style={{ backgroundColor: 'var(--surface-300)' }}><t.icon className={`w-4 h-4 ${t.color}`} /></div>
            <p className="text-[10px] uppercase tracking-widest leading-tight" style={{ color: 'var(--text-faint)' }}>{t.label}</p>
            <p className={`text-lg sm:text-xl font-bold mt-0.5 ${t.color}`}>{t.valor}</p>
            <p className="text-[10px] mt-0.5 leading-tight" style={{ color: 'var(--text-faint)' }}>{t.sub}</p>
          </button>
        ))}
        <button onClick={() => setFiltroDecision('pendiente')}
          title="Filtrar la bandeja: solo pendientes de decisión"
          className={`rounded-2xl p-3 sm:p-4 border text-left transition-all hover:border-brand-600/40 ${decision === 'pendiente' ? 'ring-2 ring-brand-500/40 border-brand-500' : ''}`}
          style={{ backgroundColor: 'var(--surface-100)', borderColor: decision === 'pendiente' ? undefined : 'var(--border)' }}>
          <div className="w-8 h-8 rounded-xl flex items-center justify-center mb-2" style={{ backgroundColor: 'var(--surface-300)' }}><Clock className="w-4 h-4 text-amber-400" /></div>
          <p className="text-[10px] uppercase tracking-widest leading-tight" style={{ color: 'var(--text-faint)' }}>Pendientes de decisión</p>
          <p className="text-lg sm:text-xl font-bold mt-0.5 text-amber-400">{resumen?.pendientes_decision ?? '—'}</p>
        </button>
        {/* H4: antes la tarjeta entera era UN botón, así que el clic sobre la sub-línea
            «+3 desaparecidos del SII» activaba el filtro de divergentes —otra cosa— y los
            desaparecidos no se podían ver en ninguna parte. Ahora son dos botones dentro
            de la misma tarjeta, cada uno a SU lista. */}
        <div className={`rounded-2xl p-3 sm:p-4 border transition-all hover:border-brand-600/40 ${soloDiv || verDesaparecidos ? 'ring-2 ring-brand-500/40 border-brand-500' : ''}`}
          style={{ backgroundColor: 'var(--surface-100)', borderColor: soloDiv || verDesaparecidos ? undefined : 'var(--border)' }}>
          <button onClick={() => { setSoloDiv(v => !v); setPage(1) }}
            title="Filtrar la bandeja: solo documentos que cambiaron en el SII después de decididos"
            className="w-full text-left">
            <div className="w-8 h-8 rounded-xl flex items-center justify-center mb-2" style={{ backgroundColor: 'var(--surface-300)' }}><AlertTriangle className="w-4 h-4 text-red-400" /></div>
            <p className="text-[10px] uppercase tracking-widest leading-tight" style={{ color: 'var(--text-faint)' }}>Divergentes</p>
            <p className="text-lg sm:text-xl font-bold mt-0.5 text-red-400">{resumen?.divergentes ?? '—'}</p>
          </button>
          {(resumen?.desaparecidos ?? 0) > 0 && (
            <button onClick={() => verEstado(verDesaparecidos ? 'ACTIVO' : 'DESAPARECIDO')}
              title="Ver la lista de documentos que el SII declaraba y dejó de declarar"
              className="mt-1 text-[10px] leading-tight text-red-400 underline underline-offset-2 text-left">
              +{resumen!.desaparecidos} desaparecidos del SII — {verDesaparecidos ? 'volver a la lista normal' : 'ver cuáles son'}
            </button>
          )}
        </div>
        {/* Conciliación bancaria: tarjeta-resumen clicable (abre/enfoca la sección) */}
        <button onClick={enfocarMatch}
          title="Abrir la conciliación bancaria (matcher banco ↔ libro ↔ egresos)"
          className={`col-span-2 sm:col-span-3 lg:col-span-6 rounded-2xl p-3 sm:p-4 border text-left transition-all hover:border-brand-600/40 ${matchOpen ? 'ring-2 ring-brand-500/40 border-brand-500' : ''}`}
          style={{ backgroundColor: 'var(--surface-100)', borderColor: matchOpen ? undefined : 'var(--border)' }}>
          <div className="flex flex-wrap items-center gap-x-8 gap-y-3">
            <div className="flex items-center gap-3 min-w-[200px]">
              <div className="w-8 h-8 rounded-xl flex items-center justify-center shrink-0" style={{ backgroundColor: 'var(--surface-300)' }}>
                <Landmark className="w-4 h-4 text-brand-400" />
              </div>
              <div>
                <p className="text-[10px] uppercase tracking-widest leading-tight" style={{ color: 'var(--text-faint)' }}>Conciliación bancaria · % del libro conciliado</p>
                <p className="text-lg sm:text-xl font-bold mt-0.5 text-brand-400">
                  {mResumen ? `${mResumen.pct_libro_conciliado_por_monto.toLocaleString('es-CL')}%` : '—'}
                </p>
              </div>
            </div>
            <div>
              <p className="text-[10px] uppercase tracking-widest leading-tight" style={{ color: 'var(--text-faint)' }}>Autos vivos</p>
              <p className="text-lg font-bold mt-0.5 text-emerald-500">{mResumen?.autos_vivos ?? '—'}</p>
            </div>
            <div>
              <p className="text-[10px] uppercase tracking-widest leading-tight" style={{ color: 'var(--text-faint)' }}>Sugeridos pendientes</p>
              <p className="text-lg font-bold mt-0.5 text-amber-400">{mResumen?.sugeridos ?? '—'}</p>
            </div>
            <div>
              <p className="text-[10px] uppercase tracking-widest leading-tight" style={{ color: 'var(--text-faint)' }}>Conflictos</p>
              <p className={`text-lg font-bold mt-0.5 ${(mResumen?.conflictos ?? 0) > 0 ? 'text-red-400' : ''}`}
                style={(mResumen?.conflictos ?? 0) > 0 ? {} : { color: 'var(--text-faint)' }}>
                {mResumen?.conflictos ?? '—'}
              </p>
            </div>
            {(mResumen?.en_duda ?? 0) > 0 && (
              <div>
                <p className="text-[10px] uppercase tracking-widest leading-tight" style={{ color: 'var(--text-faint)' }}>En duda</p>
                <p className="text-lg font-bold mt-0.5 text-amber-500">{mResumen!.en_duda}</p>
              </div>
            )}
          </div>
        </button>
      </div>

      {/* Edad del barrido + cuadratura mensual */}
      <div className="grid lg:grid-cols-3 gap-3">
        <div className="rounded-2xl border p-4 space-y-3" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
          <h3 className="font-semibold text-sm" style={{ color: 'var(--text-primary)' }}>Último barrido del SII</h3>
          <div className="flex items-center gap-2">
            <span className={`w-2.5 h-2.5 rounded-full ${edadDot} ${edadCritica ? 'animate-pulse' : ''}`} />
            <span className={`text-sm font-bold ${edadColor}`}>
              {edadHoras == null ? 'Nunca sincronizado' : `Hace ${edadHoras} h`}
            </span>
          </div>
          {edadCritica && (
            <p className="text-xs text-red-400">
              {edadHoras == null
                ? 'El espejo del libro está vacío o nunca terminó un barrido con éxito: sincroniza para poder confiar en las cubetas.'
                : 'Más de 48 horas sin barrido exitoso: las cubetas pueden estar desactualizadas.'}
            </p>
          )}
          {resumen?.ultimo_barrido && (
            <p className="text-xs" style={{ color: 'var(--text-faint)' }}>
              Último intento: {resumen.ultimo_barrido.terminado_at ? fmtDate(resumen.ultimo_barrido.terminado_at) : '—'}
              {resumen.ultimo_barrido.origen ? ` · ${resumen.ultimo_barrido.origen}` : ''}
              {resumen.ultimo_barrido.total_api != null ? ` · ${resumen.ultimo_barrido.total_api} docs en el API` : ''}
            </p>
          )}
          {resumen?.ultimo_barrido && resumen.ultimo_barrido.exito === false && (
            <div className="rounded-xl border p-2.5 bg-red-500/10 border-red-500/30 text-xs text-red-400">
              El último barrido falló: {resumen.ultimo_barrido.error || 'sin detalle'}
            </div>
          )}
          {/* El barrido puede terminar BIEN y con avisos: un documento del SII sin montos
              legibles se espeja igual (con monto en blanco) en vez de tumbar la corrida
              entera, y deja constancia. Esa constancia viaja en el mismo campo `error`,
              y antes solo se pintaba cuando la corrida había FALLADO: el aviso existía y
              nadie lo veía nunca — el mismo agujero que la leyenda del folio parecido.
              Va en ámbar, no en rojo: no es una falla, es algo que hay que mirar. */}
          {resumen?.ultimo_barrido && resumen.ultimo_barrido.exito === true
            && resumen.ultimo_barrido.error && (
            <div className="rounded-xl border p-2.5 bg-amber-500/10 border-amber-500/30 text-xs text-amber-400">
              El último barrido terminó bien, con un aviso: {resumen.ultimo_barrido.error}
            </div>
          )}
          <button onClick={sincronizar} disabled={syncing}
            className="btn-primary w-full flex items-center justify-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed">
            {syncing ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
            {syncing ? 'Sincronizando…' : 'Sincronizar ahora'}
          </button>
        </div>

        <div className="lg:col-span-2 rounded-2xl border p-4" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
          <h3 className="font-semibold text-sm mb-1" style={{ color: 'var(--text-primary)' }}>Cuadratura mensual · Σ libro SII vs Σ compras ERP</h3>
          {/* H3: la diferencia que se mostraba era «libro − ERP» a secas, y esa resta NO
              puede dar $0 nunca: el libro trae notas de crédito y documentos ignorados, y
              el ERP trae importaciones que jamás pasan por el libro del SII. La alarma
              estaba encendida todos los meses, así que dejó de ser alarma. Ahora se
              muestra la resta comparable, con las tres piezas a la vista para que el
              número se pueda abrir. */}
          <p className="text-xs mb-3" style={{ color: 'var(--text-faint)' }}>
            El número del controller: cuando la diferencia no es $0, hay facturas del SII sin registrar
            (o compras registradas sin respaldo tributario). La resta ya descuenta lo que nunca puede calzar.
          </p>
          {(resumen?.cuadratura_mensual?.length ?? 0) === 0 ? (
            <p className="text-xs py-4 text-center" style={{ color: 'var(--text-faint)' }}>Sin datos aún: sincroniza el libro primero.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b" style={{ borderColor: 'var(--border)' }}>
                    {[
                      ['Mes', ''],
                      ['Σ Libro SII', 'Todo lo que el SII declara en el mes, notas de crédito incluidas'],
                      ['(−) Ignorados y notas de crédito', 'Lo que se saca del libro para poder comparar: los documentos que marcaste «ignorar» (no son del ERP) y las notas de crédito'],
                      ['Σ ERP comparable', 'Las compras del ERP que SÍ deberían estar en el libro: nacionales, en pesos y con documento tributario'],
                      ['Diferencia', 'Σ Libro SII − ignorados y notas de crédito − Σ ERP comparable. Esta es la resta que debe dar $0: si no da, falta registrar una factura del SII (o hay una compra registrada sin respaldo tributario)'],
                    ].map(([h, ayuda], i) => (
                      <th key={h} title={ayuda || undefined}
                        className={`px-3 py-2 text-xs font-semibold uppercase tracking-wider ${i === 0 ? 'text-left' : 'text-right'}`}
                        style={{ color: 'var(--text-faint)' }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {resumen!.cuadratura_mensual.map(c => {
                    // Fallback a las claves viejas: si el backend no manda el desglose, se
                    // sigue viendo la resta bruta antes que una pantalla en blanco.
                    const menos = (c.libro_ignorados ?? 0) + (c.libro_nc ?? 0)
                    const erpComp = c.erp_comparable ?? c.erp
                    const dif = c.diferencia_explicada ?? c.diferencia
                    const cuadra = Math.abs(dif) < 1
                    const diffCls = cuadra ? 'text-emerald-500' : dif > 0 ? 'text-red-400' : 'text-amber-400'
                    return (
                      <tr key={c.mes} className="border-b last:border-0" style={{ borderColor: 'var(--border)' }}>
                        <td className="px-3 py-2 font-medium capitalize" style={{ color: 'var(--text-primary)' }}>{fmtMes(c.mes)}</td>
                        <td className="px-3 py-2 text-right" style={{ color: 'var(--text-muted)' }}>{fmtMonto(c.libro)}</td>
                        <td className="px-3 py-2 text-right" style={{ color: 'var(--text-faint)' }}>{menos === 0 ? '—' : fmtMonto(menos)}</td>
                        <td className="px-3 py-2 text-right" style={{ color: 'var(--text-muted)' }}>{fmtMonto(erpComp)}</td>
                        <td className={`px-3 py-2 text-right font-semibold ${diffCls}`}>
                          {cuadra ? 'Cuadra' : (dif > 0 ? '+' : '') + fmtMonto(dif)}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
          {/* H3: lo excluido se dice, no se esconde — si no, la resta parece arbitraria. */}
          {erpFueraLibro !== 0 && (
            <p className="text-[11px] mt-2" style={{ color: 'var(--text-faint)' }}>
              Compras del ERP que no pasan por el libro del SII (importaciones, compras en otra moneda,
              gastos sin documento tributario): {fmtMonto(erpFueraLibro)} en los meses de arriba —
              quedan fuera de la resta a propósito.
            </p>
          )}
          {(resumen?.monto_sin_fecha ?? 0) !== 0 && (
            <p className="text-[11px] mt-2" style={{ color: 'var(--text-faint)' }}>
              Excluido del cuadre por venir sin fecha: {fmtMonto(resumen!.monto_sin_fecha)} (no se esconde: no hay mes al que imputarlo).
            </p>
          )}
        </div>
      </div>

      {/* Conciliación bancaria: la cara del matcher banco ↔ libro ↔ egresos (plegable) */}
      <div ref={matchRef} className="rounded-2xl border" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
        <button onClick={() => setMatchOpen(o => !o)}
          className="w-full flex items-center justify-between px-4 py-3 text-left"
          aria-expanded={matchOpen}>
          <span className="flex items-center gap-2 font-semibold text-sm flex-wrap" style={{ color: 'var(--text-primary)' }}>
            <Landmark className="w-4 h-4 text-brand-400" /> Conciliación bancaria
            <span className="text-xs font-normal" style={{ color: 'var(--text-faint)' }}>
              · {mResumen ? `${mResumen.pct_libro_conciliado_por_monto.toLocaleString('es-CL')}% del libro conciliado por monto` : 'sin datos del matcher'}
            </span>
            {(mResumen?.conflictos ?? 0) > 0 && (
              <span className={`${chipCls} bg-red-500/10 text-red-400 border-red-400/20`}>
                {mResumen!.conflictos} conflicto{mResumen!.conflictos === 1 ? '' : 's'}
              </span>
            )}
          </span>
          {matchOpen ? <ChevronUp className="w-4 h-4" style={{ color: 'var(--text-muted)' }} /> : <ChevronDown className="w-4 h-4" style={{ color: 'var(--text-muted)' }} />}
        </button>
        {matchOpen && (
          <div className="px-4 pb-4 space-y-4 border-t pt-4" style={{ borderColor: 'var(--border)' }}>
            <div className="flex flex-col lg:flex-row lg:items-center gap-3 justify-between">
              <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
                El matcher cruza la cartola del banco con el libro del SII y los egresos de Tesorería.
                {' '}<b style={{ color: 'var(--text-primary)' }}>Confirmar</b> deja el movimiento conciliado (mismo candado de Tesorería);
                {' '}<b style={{ color: 'var(--text-primary)' }}>descartar</b> entierra el par: el motor no lo vuelve a sugerir.
                {' '}Lo ya conciliado vive en la pestaña <b style={{ color: 'var(--text-primary)' }}>Conciliados</b>, y desde ahí se deshace.
              </p>
              <div className="flex items-center gap-3 shrink-0 flex-wrap">
                {mResumen?.ultima_corrida && (
                  <span className="text-xs whitespace-nowrap" title={mResumen.ultima_corrida.error || undefined} style={{ color: 'var(--text-faint)' }}>
                    Última corrida: {mResumen.ultima_corrida.terminado_at ? fmtDate(mResumen.ultima_corrida.terminado_at) : '—'}
                    {` · ${mResumen.ultima_corrida.origen}`}
                    {mResumen.ultima_corrida.exito === false ? ' · falló' : ''}
                  </span>
                )}
                <button onClick={correrMatcher} disabled={running}
                  className="btn-primary flex items-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed">
                  {running ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
                  {running ? 'Corriendo…' : 'Correr matcher'}
                </button>
              </div>
            </div>

            {/* Pestañas: sugeridos (score desc, lo da el backend) / conflictos / autos /
                conciliados (H2: la que faltaba para poder DESHACER). */}
            <div className="flex items-center gap-1.5 flex-wrap">
              {([
                ['sugeridos', `Sugeridos${mResumen ? ` (${mResumen.sugeridos})` : ''}`],
                ['conflictos', `Conflictos${mResumen ? ` (${mResumen.conflictos})` : ''}`],
                ['autos', `Autos${mResumen ? ` (${mResumen.autos_vivos}${mResumen.en_duda > 0 ? ` · ${mResumen.en_duda} en duda` : ''})` : ''}`],
                ['conciliados', `Conciliados${mResumen ? ` (${mResumen.confirmados})` : ''}`],
              ] as [MatchTab, string][]).map(([k, l]) => (
                <button key={k} onClick={() => {
                  setMatchTab(k); setMatchPage(1)
                  // El buscador por N° solo tiene sentido en «Conciliados»: al salir se
                  // limpia para que no filtre en silencio otra pestaña.
                  if (k !== 'conciliados') { setMatchIdInput(''); setMatchIdApplied('') }
                }}
                  className={`px-3 py-1.5 rounded-full text-xs font-medium border transition-all ${matchTab === k ? 'border-brand-500 bg-brand-500/10 text-brand-400' : 'border-transparent'}`}
                  style={matchTab !== k ? { backgroundColor: 'var(--surface-200)', borderColor: 'var(--border)', color: 'var(--text-muted)' } : {}}>
                  {l}
                </button>
              ))}
            </div>

            {/* CONCILIADOS: la salida del callejón. Tesorería no deja borrar una cartola
                ni desconciliar un movimiento mientras su cruce con el libro siga vivo, y
                nombra el N° del cruce; acá se busca ese N° y se deshace. */}
            {matchTab === 'conciliados' && (
              <div className="rounded-xl border p-3 space-y-2" style={{ backgroundColor: 'var(--surface-200)', borderColor: 'var(--border)' }}>
                <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
                  Cruces ya conciliados (los que confirmó una persona y los que el sistema concilió solo).
                  Si Tesorería no te deja borrar una cartola o desconciliar un movimiento porque «el cruce
                  N° tanto está confirmado», búscalo acá por ese número y aprieta
                  {' '}<b style={{ color: 'var(--text-primary)' }}>Deshacer conciliación</b>: el movimiento del banco
                  queda libre y recién ahí Tesorería te deja seguir.
                </p>
                <div className="flex items-center gap-2 flex-wrap">
                  <input className="px-3 py-1.5 rounded-lg border text-xs w-40 focus:outline-none focus:ring-2 focus:ring-brand-500/40"
                    style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)', color: 'var(--text-primary)' }}
                    placeholder="N° del cruce…" inputMode="numeric" value={matchIdInput}
                    onChange={e => setMatchIdInput(e.target.value.replace(/\D/g, ''))}
                    onKeyDown={e => { if (e.key === 'Enter') { setMatchIdApplied(matchIdInput.trim()); setMatchPage(1) } }} />
                  <button onClick={() => { setMatchIdApplied(matchIdInput.trim()); setMatchPage(1) }}
                    className="btn-secondary flex items-center gap-1 text-xs">
                    <Search className="w-3.5 h-3.5" /> Buscar
                  </button>
                  {matchIdApplied && (
                    <button onClick={() => { setMatchIdInput(''); setMatchIdApplied(''); setMatchPage(1) }}
                      className="text-xs underline underline-offset-2" style={{ color: 'var(--text-muted)' }}>
                      Ver todos los conciliados
                    </button>
                  )}
                </div>
              </div>
            )}

            {/* AUTOS: los vivos no tienen listado en el API (ya están conciliados en
                Tesorería); acá va su agregado + los degradados a «en duda», que sí piden decisión. */}
            {matchTab === 'autos' && (
              <div className="rounded-xl border p-3 flex items-start gap-3" style={{ backgroundColor: 'var(--surface-200)', borderColor: 'var(--border)' }}>
                <span className={`${chipCls} bg-emerald-500/10 text-emerald-500 border-emerald-500/20 mt-0.5 shrink-0`}>AUTO</span>
                <div className="text-xs" style={{ color: 'var(--text-muted)' }}>
                  <p>
                    <b style={{ color: 'var(--text-primary)' }}>{mResumen?.autos_vivos ?? 0}</b> conciliación{(mResumen?.autos_vivos ?? 0) === 1 ? '' : 'es'} automática{(mResumen?.autos_vivos ?? 0) === 1 ? '' : 's'} viva{(mResumen?.autos_vivos ?? 0) === 1 ? '' : 's'}:
                    el motor confirma solo cuando TODAS las condiciones duras se cumplen.
                    <span title="Los autos no se tocan a mano: si en una corrida posterior dejan de cumplirse (documento cambiado en Wasabil, gemelo nuevo que también calzaría, contradicción con la conciliación de Tesorería), el motor los degrada solo a «en duda» y aparecen abajo para decisión humana.">
                      <HelpCircle className="w-3.5 h-3.5 inline ml-1 align-text-bottom" style={{ color: 'var(--text-faint)' }} />
                    </span>
                  </p>
                  <p className="mt-1" style={{ color: 'var(--text-faint)' }}>
                    Cada uno deja su movimiento conciliado en Tesorería, y todos están listados —con su
                    N°— en la pestaña «Conciliados», que es donde se deshacen si hiciera falta.
                    Abajo: los degradados o caducados a «en duda», con el porqué en sus motivos.
                  </p>
                </div>
              </div>
            )}

            {matchLoading && <div className="flex items-center justify-center py-12"><Loader2 className="w-7 h-7 animate-spin text-brand-400" /></div>}
            {!matchLoading && matchItems.length === 0 && (
              <div className="rounded-xl border py-10 text-center" style={{ borderColor: 'var(--border)' }}>
                <Landmark className="w-8 h-8 mx-auto mb-2 opacity-20" style={{ color: 'var(--text-muted)' }} />
                <p className="text-sm font-medium" style={{ color: 'var(--text-muted)' }}>
                  {matchTab === 'sugeridos' ? 'Sin sugerencias pendientes'
                    : matchTab === 'conflictos' ? 'Sin conflictos'
                    : matchTab === 'conciliados' ? (matchIdApplied ? `No hay ningún cruce conciliado con el N° ${matchIdApplied}` : 'Todavía no hay cruces conciliados')
                    : 'Ningún auto degradado a «en duda»'}
                </p>
                <p className="text-xs mt-1" style={{ color: 'var(--text-faint)' }}>
                  {matchTab === 'sugeridos' ? 'Usa «Correr matcher» para generar cruces nuevos.'
                    : matchTab === 'conflictos' ? 'Cuando dos candidatos compitan por el mismo movimiento, aparecen aquí.'
                    : matchTab === 'conciliados' ? (matchIdApplied ? 'Revisa el número que te mostró Tesorería, o quita el filtro para ver todos.' : 'Acá van a aparecer los cruces que se confirmen, para poder deshacerlos.')
                    : 'Los autos vivos siguen válidos: no piden decisión humana.'}
                </p>
              </div>
            )}
            {!matchLoading && matchItems.length > 0 && (
              <div className="rounded-xl border overflow-hidden" style={{ borderColor: 'var(--border)' }}>
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
                        {['Movimiento (banco)', '', 'Documento (libro SII)', 'Asignado', 'Score', 'Motivo', ''].map((h, i) => (
                          <th key={`${h}-${i}`}
                            className={`py-3 text-xs font-semibold uppercase tracking-wider whitespace-nowrap ${i === 1 ? 'px-1' : 'px-4'} ${i === 3 ? 'text-right' : 'text-left'}`}
                            style={{ color: 'var(--text-faint)' }}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {matchItems.map(m => (
                        <MatchRow key={m.id} m={m} actingId={actingId}
                          onConfirmar={mm => setConfirmando(mm)}
                          onDescartar={mm => { setDescartando(mm); setDescarteMotivo('') }} />
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="flex items-center justify-between px-4 py-3 border-t" style={{ borderColor: 'var(--border)' }}>
                  <span className="text-xs" style={{ color: 'var(--text-faint)' }}>
                    {(matchPage - 1) * PAGE_SIZE + 1}–{Math.min(matchPage * PAGE_SIZE, matchTotal)} de {matchTotal} matches
                  </span>
                  <div className="flex items-center gap-1.5">
                    <button onClick={() => setMatchPage(p => Math.max(1, p - 1))} disabled={matchPage <= 1}
                      className="btn-secondary flex items-center gap-1 text-xs disabled:opacity-40 disabled:cursor-not-allowed">
                      <ChevronLeft className="w-3.5 h-3.5" /> Anterior
                    </button>
                    <span className="text-xs px-2" style={{ color: 'var(--text-muted)' }}>{matchPage} / {matchTotalPages}</span>
                    <button onClick={() => setMatchPage(p => Math.min(matchTotalPages, p + 1))} disabled={matchPage >= matchTotalPages}
                      className="btn-secondary flex items-center gap-1 text-xs disabled:opacity-40 disabled:cursor-not-allowed">
                      Siguiente <ChevronRight className="w-3.5 h-3.5" />
                    </button>
                  </div>
                </div>
              </div>
            )}
          </div>
        )}
      </div>

      {/* H2: cuadro de confirmación. Confirmar deja el movimiento conciliado y es
          justamente lo que después traba el borrado de la cartola en Tesorería: era la
          acción más pesada de la sección y la única que se disparaba de un clic suelto,
          mientras que descartar —menos grave— sí preguntaba. */}
      {confirmando && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4" style={{ backgroundColor: 'rgba(0,0,0,0.55)' }}
          onClick={() => { if (actingId === null) setConfirmando(null) }}>
          <div className="rounded-2xl border p-4 w-full max-w-sm space-y-3" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}
            onClick={e => e.stopPropagation()}>
            <h3 className="font-semibold text-sm" style={{ color: 'var(--text-primary)' }}>Confirmar la conciliación</h3>
            <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
              El movimiento del banco queda <b style={{ color: 'var(--text-primary)' }}>conciliado</b> con
              este documento del libro, igual que si lo conciliaras en Tesorería.
              {confirmando.grupo_uuid ? ' Se confirma el grupo completo (las filas hermanas van juntas).' : ''}
            </p>
            {confirmando.estado === 'conflicto' && (
              <div className="rounded-xl border p-2.5 bg-amber-500/10 border-amber-500/30 text-xs text-amber-500">
                Ojo: hay más de un documento que calza con este mismo cargo. Al confirmar, el cargo queda
                explicado por ESTE. Si no estás seguro, cierra y mira antes la lista de los que compiten.
              </div>
            )}
            <p className="text-xs" style={{ color: 'var(--text-faint)' }}>
              Se puede deshacer después desde la pestaña «Conciliados».
            </p>
            <div className="flex items-center justify-end gap-2">
              <button onClick={() => setConfirmando(null)} disabled={actingId !== null}
                className="btn-secondary text-xs disabled:opacity-50">Cancelar</button>
              <button onClick={confirmarMatch} disabled={actingId !== null}
                className="btn-primary flex items-center gap-1.5 text-xs disabled:opacity-50 disabled:cursor-not-allowed">
                {actingId !== null ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <CheckCircle2 className="w-3.5 h-3.5" />} Conciliar
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Prompt-modal chico: descartar con motivo opcional */}
      {descartando && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4" style={{ backgroundColor: 'rgba(0,0,0,0.55)' }}
          onClick={() => { if (actingId === null) { setDescartando(null); setDescarteMotivo('') } }}>
          <div className="rounded-2xl border p-4 w-full max-w-sm space-y-3" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}
            onClick={e => e.stopPropagation()}>
            {/* H2: el mismo cuadro sirve para descartar una sugerencia y para DESHACER un
                conciliado (el backend usa el mismo camino), pero lo que pasa es distinto
                y hay que decirlo distinto. */}
            <h3 className="font-semibold text-sm" style={{ color: 'var(--text-primary)' }}>
              {descartando.estado === 'confirmado' ? 'Deshacer la conciliación' : 'Descartar el cruce'}
            </h3>
            <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
              {descartando.estado === 'confirmado'
                ? 'El movimiento del banco vuelve a quedar libre en Tesorería: recién ahí vas a poder desconciliarlo o borrar la cartola. El par no se vuelve a sugerir.'
                : 'El par queda ocupado: el sistema no vuelve a sugerir este cruce.'}
              {descartando.grupo_uuid ? ' Se aplica al grupo completo (filas hermanas incluidas).' : ''}
            </p>
            <Field label="Motivo (opcional)">
              <input className={inputCls} style={inputStyle} value={descarteMotivo}
                onChange={e => setDescarteMotivo(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') descartarMatch() }}
                placeholder="Por qué no corresponde este cruce" maxLength={400} autoFocus />
            </Field>
            <div className="flex items-center justify-end gap-2">
              <button onClick={() => { setDescartando(null); setDescarteMotivo('') }} disabled={actingId !== null}
                className="btn-secondary text-xs disabled:opacity-50">Cancelar</button>
              <button onClick={descartarMatch} disabled={actingId !== null}
                className="btn-primary flex items-center gap-1.5 text-xs disabled:opacity-50 disabled:cursor-not-allowed">
                {actingId !== null ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <X className="w-3.5 h-3.5" />}
                {descartando.estado === 'confirmado' ? 'Deshacer' : 'Descartar'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Bandeja: filtros + tabla paginada */}
      <div className="space-y-3">
        <div className="flex flex-col lg:flex-row gap-3 lg:items-center">
          <div className="relative flex-1 min-w-[220px]">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4" style={{ color: 'var(--text-faint)' }} />
            <input className="w-full pl-9 pr-16 py-2.5 rounded-xl border text-sm focus:outline-none focus:ring-2 focus:ring-brand-500/40"
              style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)', color: 'var(--text-primary)' }}
              placeholder="Filtrar por RUT del emisor (Enter para aplicar)…"
              value={rutInput}
              onChange={e => setRutInput(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') aplicarRut() }} />
            {(rutInput || rutApplied) && (
              <button onClick={limpiarRut} className="absolute right-2 top-1/2 -translate-y-1/2 p-1 rounded-lg hover:bg-[var(--surface-200)]"
                style={{ color: 'var(--text-muted)' }} title="Limpiar filtro RUT"><X className="w-4 h-4" /></button>
            )}
          </div>
          <div className="flex items-center gap-1.5 flex-wrap">
            {/* H4: en la lista de desaparecidos estos filtros no aplican (todos tienen la
                misma cubeta), así que se ocultan en vez de dejar botones que vacían la
                lista sin explicar nada. */}
            {!verDesaparecidos && [['', 'Todas'], ['ESTA', 'Está'], ['NO_ESTA', 'No está'], ['INDETERMINADO', 'Indeterminado']].map(([v, l]) => (
              <button key={v || 'todas-c'} onClick={() => { setCubeta(v); setPage(1) }}
                className={`px-3 py-1.5 rounded-full text-xs font-medium border transition-all ${cubeta === v ? 'border-brand-500 bg-brand-500/10 text-brand-400' : 'border-transparent'}`}
                style={cubeta !== v ? { backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)', color: 'var(--text-muted)' } : {}}>
                {l}
              </button>
            ))}
            {!verDesaparecidos && <span className="w-px h-5 mx-1" style={{ backgroundColor: 'var(--border)' }} />}
            {[['', 'Todas'], ['pendiente', 'Pendiente'], ['ignorado', 'Ignorado'], ['clasificado', 'Clasificado']].map(([v, l]) => (
              <button key={v || 'todas-d'} onClick={() => { setDecision(v); setPage(1) }}
                className={`px-3 py-1.5 rounded-full text-xs font-medium border transition-all ${decision === v ? 'border-brand-500 bg-brand-500/10 text-brand-400' : 'border-transparent'}`}
                style={decision !== v ? { backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)', color: 'var(--text-muted)' } : {}}>
                {l}
              </button>
            ))}
            <label className="flex items-center gap-1.5 text-xs cursor-pointer ml-1 whitespace-nowrap" style={{ color: 'var(--text-muted)' }}>
              <input type="checkbox" checked={soloDiv} onChange={e => { setSoloDiv(e.target.checked); setPage(1) }} />
              Solo divergentes
            </label>
            {/* H4: el chip que faltaba — la única forma de ver la lista que el tablero anuncia. */}
            {(resumen?.desaparecidos ?? 0) > 0 && (
              <button onClick={() => verEstado(verDesaparecidos ? 'ACTIVO' : 'DESAPARECIDO')}
                title="Documentos que el SII declaraba antes y dejó de declarar"
                className={`px-3 py-1.5 rounded-full text-xs font-medium border transition-all ${verDesaparecidos ? 'border-red-400 bg-red-500/10 text-red-400' : 'border-transparent'}`}
                style={!verDesaparecidos ? { backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)', color: 'var(--text-muted)' } : {}}>
                Desaparecidos del SII ({resumen!.desaparecidos})
              </button>
            )}
          </div>
        </div>

        {verDesaparecidos && (
          <div className="rounded-xl border p-3 bg-amber-500/10 border-amber-500/30">
            <p className="text-xs font-semibold text-amber-500 flex items-center gap-1.5">
              <AlertTriangle className="w-3.5 h-3.5 shrink-0" /> Documentos que el SII declaraba y ya no declara
            </p>
            <p className="text-xs mt-1" style={{ color: 'var(--text-muted)' }}>
              Estás viendo solo estos, con la decisión que tenían intacta. Si alguno quedó clasificado
              como costo o como activo fijo, revísalo: el SII ya no lo respalda. Para volver a la lista
              normal, aprieta otra vez «Desaparecidos del SII».
            </p>
          </div>
        )}

        {loadingDocs && <div className="flex items-center justify-center py-16"><Loader2 className="w-7 h-7 animate-spin text-brand-400" /></div>}
        {!loadingDocs && docs.length === 0 && (
          <div className="rounded-2xl border py-16 text-center" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
            <BookOpenCheck className="w-10 h-10 mx-auto mb-3 opacity-20" style={{ color: 'var(--text-muted)' }} />
            <p className="text-sm font-medium" style={{ color: 'var(--text-muted)' }}>
              {verDesaparecidos ? 'Ningún documento desaparecido con estos filtros'
                : edadHoras == null ? 'El espejo del libro está vacío' : 'No hay documentos con estos filtros'}
            </p>
            <p className="text-xs mt-1" style={{ color: 'var(--text-faint)' }}>
              {verDesaparecidos ? 'Aprieta otra vez «Desaparecidos del SII» para volver a la lista normal.'
                : edadHoras == null ? 'Usa "Sincronizar ahora" para traer el libro de compras del SII.' : 'Prueba quitando algún filtro.'}
            </p>
          </div>
        )}
        {!loadingDocs && docs.length > 0 && (
          <div className="rounded-2xl border overflow-hidden" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
                    {['Fecha', 'Tipo', 'Folio', 'Emisor', 'Monto', 'Cubeta', 'Decisión', '⚠'].map((h, i) => (
                      <th key={h} className={`px-4 py-3 text-xs font-semibold uppercase tracking-wider whitespace-nowrap ${i === 4 ? 'text-right' : i === 7 ? 'text-center' : 'text-left'}`}
                        style={{ color: 'var(--text-faint)' }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {docs.map(d => <DocRow key={d.id} doc={d} centros={centros} onChanged={reloadAll} />)}
                </tbody>
              </table>
            </div>
            <div className="flex items-center justify-between px-4 py-3 border-t" style={{ borderColor: 'var(--border)' }}>
              <span className="text-xs" style={{ color: 'var(--text-faint)' }}>
                {(page - 1) * PAGE_SIZE + 1}–{Math.min(page * PAGE_SIZE, total)} de {total} documentos
              </span>
              <div className="flex items-center gap-1.5">
                <button onClick={() => setPage(p => Math.max(1, p - 1))} disabled={page <= 1}
                  className="btn-secondary flex items-center gap-1 text-xs disabled:opacity-40 disabled:cursor-not-allowed">
                  <ChevronLeft className="w-3.5 h-3.5" /> Anterior
                </button>
                <span className="text-xs px-2" style={{ color: 'var(--text-muted)' }}>{page} / {totalPages}</span>
                <button onClick={() => setPage(p => Math.min(totalPages, p + 1))} disabled={page >= totalPages}
                  className="btn-secondary flex items-center gap-1 text-xs disabled:opacity-40 disabled:cursor-not-allowed">
                  Siguiente <ChevronRight className="w-3.5 h-3.5" />
                </button>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Reglas por emisor (plegable) */}
      <div className="rounded-2xl border" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
        <button onClick={() => setReglasOpen(o => !o)}
          className="w-full flex items-center justify-between px-4 py-3 text-left"
          aria-expanded={reglasOpen}>
          <span className="flex items-center gap-2 font-semibold text-sm" style={{ color: 'var(--text-primary)' }}>
            <Shield className="w-4 h-4 text-brand-400" /> Reglas por emisor (RUT)
            <span className="text-xs font-normal" style={{ color: 'var(--text-faint)' }}>· {reglas.length} regla{reglas.length === 1 ? '' : 's'}</span>
          </span>
          {reglasOpen ? <ChevronUp className="w-4 h-4" style={{ color: 'var(--text-muted)' }} /> : <ChevronDown className="w-4 h-4" style={{ color: 'var(--text-muted)' }} />}
        </button>
        {reglasOpen && (
          <div className="px-4 pb-4 space-y-4 border-t pt-4" style={{ borderColor: 'var(--border)' }}>
            <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
              El RUT trae el default, el documento manda: <b className="text-red-400">BLOQUEADO</b> jamás capitaliza a un embarque
              (financiero/intercompañía), <b style={{ color: 'var(--text-muted)' }}>IGNORAR_AUTO</b> se archiva solo con su centro,
              y <b className="text-blue-400">LOGISTICO</b> aparece primero y PUEDE capitalizar — el default sigue siendo gasto.
            </p>
            {reglas.length > 0 && (
              <div className="rounded-xl border overflow-hidden" style={{ borderColor: 'var(--border)' }}>
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
                        {['RUT', 'Nivel', 'Destino default', 'Motivo'].map(h => (
                          <th key={h} className="text-left px-4 py-2.5 text-xs font-semibold uppercase tracking-wider whitespace-nowrap" style={{ color: 'var(--text-faint)' }}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {reglas.map(r => (
                        <tr key={r.id} className="border-b last:border-0" style={{ borderColor: 'var(--border)' }}>
                          <td className="px-4 py-2.5 font-mono whitespace-nowrap" style={{ color: 'var(--text-primary)' }}>{r.rut_formateado || r.rut}</td>
                          <td className="px-4 py-2.5 whitespace-nowrap">
                            <span className={`inline-flex items-center px-2.5 py-1 rounded-full text-xs font-medium border ${NIVEL_REGLA[r.nivel] || 'bg-gray-500/10 text-gray-400 border-gray-400/20'}`}>{r.nivel}</span>
                          </td>
                          <td className="px-4 py-2.5 whitespace-nowrap" style={{ color: 'var(--text-muted)' }}>{destinoLabel(r.destino_default)}</td>
                          <td className="px-4 py-2.5 max-w-[280px] truncate" style={{ color: 'var(--text-faint)' }}>{r.motivo || '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
            {/* Crear / actualizar (el POST canoniza el RUT; una regla por emisor) */}
            <div className="grid sm:grid-cols-2 lg:grid-cols-5 gap-3 items-end">
              <Field label="RUT del emisor">
                <input className={inputCls} style={inputStyle} value={reglaRut} onChange={e => setReglaRut(e.target.value)} placeholder="Ej. 76.513.680-6" />
              </Field>
              <Field label="Nivel">
                <select className={inputCls} style={inputStyle} value={reglaNivel} onChange={e => setReglaNivel(e.target.value)}>
                  <option value="BLOQUEADO">BLOQUEADO</option>
                  <option value="IGNORAR_AUTO">IGNORAR_AUTO</option>
                  <option value="LOGISTICO">LOGISTICO</option>
                </select>
              </Field>
              <Field label="Destino default (opcional)">
                <select className={inputCls} style={inputStyle} value={reglaDestino} onChange={e => setReglaDestino(e.target.value)}>
                  <option value="">Sin default</option>
                  {opcionesDestino(centros, [reglaDestino]).map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
                </select>
              </Field>
              <Field label="Motivo (opcional)">
                <input className={inputCls} style={inputStyle} value={reglaMotivo} onChange={e => setReglaMotivo(e.target.value)} placeholder="Por qué esta regla" maxLength={300} />
              </Field>
              <button onClick={guardarRegla} disabled={reglaSaving}
                className="btn-primary flex items-center justify-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed">
                {reglaSaving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Shield className="w-4 h-4" />} Guardar regla
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
