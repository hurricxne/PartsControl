// Página "Facturas y Cobranzas" (cuentas por cobrar): lista facturas + antigüedad de cartera,
// y concentra las acciones — EMITIR factura (desde una guía firmada), registrar cobranzas y
// gestionar factoring. Consume contabilidadAPI (/facturas, /kpis, despachos-facturables…).
import { useState, useEffect, useCallback, useId } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import {
  Receipt, Plus, Search, AlertCircle, CheckCircle2, DollarSign,
  Loader2, RefreshCw, ChevronDown, ChevronUp, CreditCard, Landmark, X, Trash2, FileText,
  HandCoins,
} from 'lucide-react'
import toast from 'react-hot-toast'
import { contabilidadAPI, wasabilAPI, abrirDocumento } from '../services/api'
import { fmtClp, fmtDate, hoyLocal } from '../utils/format'
import SelectorOcFactura from '../components/SelectorOcFactura'

// ─── Tipos ────────────────────────────────────────────────────────────────────
interface Cobranza { id: number; fecha: string | null; monto: number; medio: string; es_adelanto?: boolean; banco: string | null; numero_operacion: string | null; observaciones: string | null }
interface Factoring { id: number; empresa_factoring: string | null; id_operacion: string | null; fecha_operacion: string | null; monto_adelantado: number; costo_factoring: number; retencion: number; banco: string | null; estado: string; fecha_liquidacion: string | null }
interface FacturaItem { id: number; numero_parte: string | null; descripcion: string | null; cantidad: number; precio_unit_neto: number; total_neto: number; anticipo_factura_id?: number | null }
interface Factura {
  id: number; numero_factura: string | null; tipo_doc: string; es_anticipo?: boolean
  oc_cliente_id: number | null; numero_oc: string | null; numero_guia: string | null
  numero_expedicion: string | null; guia_firmada_archivo: string | null; cliente: string; rut_cliente: string
  fecha_emision: string | null; condicion_pago: string | null; plazo_dias: number | null; fecha_vencimiento: string | null
  monto_neto: number; iva: number; monto_bruto: number; monto_pagado: number; saldo: number
  estado_pago: string; semaforo: string; dias_vencimiento: number | null; observaciones: string | null
  items: FacturaItem[]; cobranzas: Cobranza[]; factoring: Factoring | null
  // Emisión electrónica al SII vía Wasabil (solo presente si la factura tiene DTE 33)
  dte_estado?: string | null; dte_folio?: string | null; dte_pdf_url?: string | null
  dte_puede_reintentar?: boolean; dte_en_vuelo?: boolean; dte_error?: string | null
}
interface Kpis {
  facturado_clp: number; cobrado_clp: number; cobrado_cliente_clp?: number
  /** Lo que puso el FACTOR (adelanto + retención liquidada). cobrado = cliente + factor:
   *  sin esta tarjeta las cifras de arriba no cuadraban entre sí en pantalla. */
  anticipo_factoring_clp?: number
  por_cobrar_clp: number; vencido_clp: number; en_factoring_clp: number
}
interface Aging { '0_30': number; '31_60': number; '61_90': number; '91_mas': number }

const PAGO: Record<string, { cls: string; label: string }> = {
  por_cobrar:  { cls: 'bg-blue-500/10 text-blue-400 border-blue-400/20', label: 'Por cobrar' },
  parcial:     { cls: 'bg-amber-500/10 text-amber-400 border-amber-400/20', label: 'Pago parcial' },
  pagada:      { cls: 'bg-emerald-500/10 text-emerald-500 border-emerald-500/20', label: 'Pagada' },
  vencida:     { cls: 'bg-red-500/10 text-red-400 border-red-400/20', label: 'Vencida' },
  factorizada: { cls: 'bg-purple-500/10 text-purple-400 border-purple-400/20', label: 'Factoring' },
}
const ESTADOS = ['', 'por_cobrar', 'parcial', 'pagada', 'vencida', 'factorizada']
const ESTADO_LABEL: Record<string, string> = { '': 'Todas', por_cobrar: 'Por cobrar', parcial: 'Parcial', pagada: 'Pagada', vencida: 'Vencida', factorizada: 'Factoring' }

// ─── Modal genérico ───────────────────────────────────────────────────────────
function Modal({ title, onClose, children, wide }: { title: string; onClose: () => void; children: React.ReactNode; wide?: boolean }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div className={`w-full ${wide ? 'max-w-2xl' : 'max-w-md'} max-h-[90vh] overflow-y-auto rounded-2xl border shadow-2xl`} style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }} onClick={e => e.stopPropagation()}>
        <div className="sticky top-0 flex items-center justify-between px-5 py-3 border-b" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-100)' }}>
          <h3 className="font-semibold text-sm" style={{ color: 'var(--text-primary)' }}>{title}</h3>
          <button onClick={onClose} className="p-1 rounded-lg hover:bg-white/10" style={{ color: 'var(--text-muted)' }}><X className="w-4 h-4" /></button>
        </div>
        <div className="p-5 space-y-3">{children}</div>
      </div>
    </div>
  )
}
const inputCls = 'w-full px-3 py-2 rounded-lg border text-sm focus:outline-none focus:ring-2 focus:ring-brand-500/40'
const inputStyle = { backgroundColor: 'var(--surface-200)', borderColor: 'var(--border)', color: 'var(--text-primary)' } as React.CSSProperties

/** Normaliza el error del backend a texto legible (FastAPI/Pydantic 422 trae detail como
 *  array de objetos {loc,msg,...}; sin esto el toast muestra [object Object]). */
function errMsg(e: any, fallback: string): string {
  const d = e?.response?.data?.detail
  if (Array.isArray(d)) return d.map((x: any) => x?.msg || JSON.stringify(x)).join('; ')
  if (typeof d === 'string') return d
  return fallback
}
const rotuloCls = 'block text-[11px] font-semibold uppercase tracking-wider mb-1'
const rotuloStyle = { color: 'var(--text-faint)' } as React.CSSProperties

/** Rótulo + UN control único (input/select). El <label> que envuelve es correcto
 *  ACÁ y solo acá: con un único descendiente etiquetable, el clic en el rótulo
 *  enfoca ese control, que es lo que el operador espera. */
function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (<label className="block"><span className={rotuloCls} style={rotuloStyle}>{label}</span>{children}</label>)
}

/**
 * Gemelo de `Field` con <div> en vez de <label>, para los widgets COMPUESTOS
 * (selector de OC, lista de guías-radio con su ayuda y sus avisos).
 *
 * POR QUÉ existe: un <label> SIN `for` se asocia a su PRIMER descendiente
 * etiquetable y el navegador dispara un clic sintético sobre él al clickear
 * cualquier descendiente NO interactivo. Con widgets compuestos eso causaba dos
 * daños reales, verificados con clics en un navegador:
 *  · selector de OC: clic en el rótulo o en el texto «OC 4711 — H-E PARTS»
 *    activaba el botón «cambiar» → onSelect(null) → el formulario a medio
 *    llenar se vaciaba solo (ocId, guía, RUT, razón social, plazo…).
 *  · lista de guías: clic en el texto de ayuda o en el aviso «⚠ sin foto de
 *    respaldo» marcaba la PRIMERA guía aunque el operador tuviera elegida otra
 *    → el DTE 33, que es IRREVERSIBLE, citaba la guía 52 equivocada.
 * De paso desaparece el <label> anidado dentro de <label> (HTML inválido).
 *
 * La asociación accesible que el <label> daba gratis se repone en el punto de
 * uso, según lo que el widget sea de verdad:
 *  · `htmlFor`: el rótulo es un <label for> apuntando al ÚNICO control que
 *    merece el foco (la caja de búsqueda del selector). Un label CON `for` solo
 *    reenvía los clics hechos sobre sí mismo — no captura a sus vecinos.
 *  · `labelId`: el rótulo es un <span id> y el widget se declara grupo
 *    (role="radiogroup" aria-labelledby) — el caso de la lista de guías.
 */
function Campo({ label, htmlFor, labelId, children }: {
  label: string; htmlFor?: string; labelId?: string; children: React.ReactNode
}) {
  return (
    <div className="block">
      {htmlFor
        ? <label htmlFor={htmlFor} className={rotuloCls} style={rotuloStyle}>{label}</label>
        : <span id={labelId} className={rotuloCls} style={rotuloStyle}>{label}</span>}
      {children}
    </div>
  )
}

/** Forma mínima de las respuestas que traen avisos NO bloqueantes junto al 200: el
 *  registro manual de una factura (POST /contabilidad/facturas) y los tres endpoints SII
 *  de factura (emitir / estado / reintentar). Campo aditivo: si el backend no lo manda,
 *  la lista simplemente viene vacía. */
interface RespuestaConAvisos { advertencias?: string[] }

/** Muestra las `advertencias` que el backend devuelve al REGISTRAR una factura por la vía
 *  manual: un 200 puede traer avisos que cambian lo que el usuario cree que pasó ("el
 *  descuento del anticipo deja esta factura en $0", "no se pudo mover el adelanto a esta
 *  factura"). Antes la respuesta ni se desestructuraba: esos avisos se perdían y en
 *  pantalla quedaba una factura rara sin ninguna explicación. Duran más que un toast
 *  normal — hay que alcanzar a leerlas. Molde: MonzaFacturasPage. */
function avisarAdvertencias(avisos?: string[]): void {
  (avisos || []).forEach(a =>
    toast(a, { duration: 9000, icon: <AlertCircle className="w-4 h-4 text-amber-500" /> }))
}

// ─── Emisión de factura electrónica al SII (DTE 33 vía Wasabil) ───────────────
// Mismo patrón 2 pasos que la guía de despacho (EmitirGuiaSIIModal): PREVIEW SII
// (no toca el SII: receptor real de la ficha Wasabil + referencias OC/guía/anticipo
// + descuento de anticipo) → EMITIR con OK explícito → sondeo hasta Emitido
// (folio + PDF) o Fallido (reintento seguro: el backend nunca emite dos veces).
// Se usa desde los dos modales de creación (payload nuevo) y desde la fila de una
// factura ya creada (facturaId: retomar/reintentar).
const REF_SII_LABEL: Record<string, string> = { '801': 'Orden de compra', '52': 'Guía de despacho', '33': 'Factura de anticipo' }

function EmisionFacturaSII({ payload, facturaId, onDone, onVolver, onCerrar, onBusy }: {
  payload?: Record<string, any>   // emisión NUEVA: payload de crearFactura SIN numero_factura
  facturaId?: number              // retomar/reintentar una factura ya creada
  onDone: () => void              // refresca la lista (la factura pudo crearse o cambiar)
  onVolver?: () => void           // volver al formulario (solo antes de emitir)
  onCerrar: () => void
  onBusy?: (b: boolean) => void   // avisa al Modal padre que NO debe cerrarse (envío en vuelo)
}) {
  type Fase = 'cargando' | 'preview' | 'emitiendo' | 'sondeo' | 'exito' | 'fallido' | 'pendiente' | 'error'
  const [fase, setFase] = useState<Fase>('cargando')
  const [prev, setPrev] = useState<any>(null)
  const [dte, setDte] = useState<any>(null)
  const [error, setError] = useState('')
  // Advertencias que llegan CON el folio. El backend las genera UNA sola vez —en el
  // request que graba el folio, sea el `emitir` o una pasada del sondeo—, así que hay
  // que quedarse con ellas apenas aparecen: la siguiente respuesta ya viene vacía y se
  // perderían. Se acumulan sin repetir. Molde: MonzaFacturasPage.
  const [avisosSii, setAvisosSii] = useState<string[]>([])
  const recordarAvisos = (data: RespuestaConAvisos) => {
    const nuevos = data?.advertencias || []
    if (nuevos.length === 0) return
    setAvisosSii(prev => [...prev, ...nuevos.filter(a => !prev.includes(a))])
  }
  // Puerta de una sola dirección: apenas se dispara el POST /emitir, este modal NUNCA
  // vuelve al formulario. El backend PUDO crear la factura (y hasta emitirla) aunque la
  // respuesta se perdiera; re-enviar el formulario crearía un SEGUNDO documento real.
  const [emisionIntentada, setEmisionIntentada] = useState(false)

  // Única fuente de verdad del botón Reintentar: puede_reintentar del backend
  const faseSegunDte = (d: any): Fase => {
    if (d?.estado === 'emitido') return 'exito'
    if (d?.puede_reintentar) return 'fallido'
    if (d?.uuid) return 'sondeo'
    return 'pendiente'  // claim en vuelo de otro intento (otra pestaña/usuario)
  }

  // Carga inicial: preview SII (emisión nueva) o estado real (retomar factura)
  useEffect(() => {
    let vivo = true
    if (payload) {
      wasabilAPI.previewFacturaSII(payload)
        .then(({ data }) => { if (vivo) { setPrev(data); setFase('preview') } })
        .catch((e: any) => { if (vivo) { setError(errMsg(e, 'No se pudo previsualizar la factura')); setFase('error') } })
    } else if (facturaId) {
      wasabilAPI.estadoFacturaSII(facturaId)
        .then(({ data }) => { if (vivo) { recordarAvisos(data); setDte(data); setFase(faseSegunDte(data)); if (data.estado === 'emitido') onDone() } })
        .catch((e: any) => { if (vivo) { setError(errMsg(e, 'No se pudo consultar el estado SII')); setFase('error') } })
    }
    return () => { vivo = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Sondeo: la emisión al SII es asíncrona (segundos a minutos)
  useEffect(() => {
    if (fase !== 'sondeo') return
    const id = dte?.factura_id || facturaId
    if (!id) { setFase('pendiente'); return }
    let vivo = true
    let intentos = 0
    const tick = async () => {
      if (!vivo) return
      intentos += 1
      try {
        const { data } = await wasabilAPI.estadoFacturaSII(id)
        if (!vivo) return
        // ANTES de decidir la fase: la pasada que confirma el folio es la que trae el
        // aviso, y es la misma que corta el sondeo con `return`.
        recordarAvisos(data)
        setDte(data)
        if (data.estado === 'emitido') { setFase('exito'); onDone(); return }
        if (data.puede_reintentar) { setFase('fallido'); onDone(); return }
      } catch { /* error transitorio: se reintenta en el próximo tick */ }
      if (intentos >= 30) { setFase('pendiente'); return }  // ~90 s: seguir después desde la lista
      window.setTimeout(tick, 3000)
    }
    tick()
    return () => { vivo = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fase])

  // Mientras el envío está en vuelo el modal no debe cerrarse (ni con clic al fondo):
  // el documento puede estar naciendo ante el SII y cerrar da falsa sensación de "no pasó nada".
  useEffect(() => { onBusy?.(fase === 'emitiendo' || fase === 'sondeo') }, [fase])
  // Al cerrar SIEMPRE se refresca la lista: la factura pudo quedar creada (y con folio)
  // aunque el usuario cierre antes de que termine el sondeo.
  const cerrar = () => { onDone(); onCerrar() }

  const procesarRespuesta = (data: any) => {
    recordarAvisos(data)
    setDte(data)
    if (data.estado === 'emitido') { setFase('exito'); onDone() }
    else if (data.puede_reintentar) { setFase('fallido'); onDone() }
    else setFase('sondeo')
  }
  const emitir = async () => {
    setEmisionIntentada(true)
    setFase('emitiendo'); setError('')
    try { procesarRespuesta((await wasabilAPI.emitirFacturaSII(payload!)).data) }
    catch (e: any) {
      // 409 (datos/emisión en curso) o 502 (sin confirmación de Wasabil): la factura
      // PUDO quedar creada — el backend nunca emite dos veces; se refresca la lista
      // para verla con su estado real (SII en proceso / SII fallida).
      setError(errMsg(e, 'No se pudo emitir la factura'))
      setFase('error'); onDone()
    }
  }
  const reintentar = async () => {
    const id = dte?.factura_id || facturaId
    if (!id) return
    setFase('emitiendo'); setError('')
    try { procesarRespuesta((await wasabilAPI.reintentarFacturaSII(id)).data) }
    catch (e: any) { setError(errMsg(e, 'No se pudo reintentar la emisión')); setFase('error'); onDone() }
  }

  return (
    <div className="space-y-3">
      {fase === 'cargando' && (
        <div className="py-8 text-center text-sm" style={{ color: 'var(--text-muted)' }}>
          <Loader2 className="w-6 h-6 animate-spin mx-auto mb-2" /> Verificando con Wasabil…
        </div>
      )}

      {(fase === 'emitiendo' || fase === 'sondeo') && (
        <div className="py-8 text-center" style={{ color: 'var(--text-muted)' }}>
          <Loader2 className="w-8 h-8 animate-spin mx-auto mb-3 text-brand-400" />
          <p className="font-semibold text-sm" style={{ color: 'var(--text-primary)' }}>
            {fase === 'emitiendo' ? 'Enviando a Wasabil…' : 'Procesando en el SII…'}
          </p>
          <p className="text-xs mt-1">El SII puede tardar de segundos a un par de minutos. No cierres esta ventana.</p>
        </div>
      )}

      {fase === 'exito' && dte && (
        <div className="py-6 text-center space-y-3">
          <CheckCircle2 className="w-10 h-10 mx-auto text-emerald-500" />
          <p className="text-lg font-bold" style={{ color: 'var(--text-primary)' }}>Factura emitida — Folio SII {dte.folio}</p>
          <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
            El folio quedó registrado en la factura y ya aparece en la lista para cobranzas y factoring.
          </p>
          {/* El documento salió bien, pero la PLATA puede no haber quedado donde
              corresponde: al confirmarse el folio el backend aplica el adelanto que la
              emisión había diferido, y si no logra encauzarlo hacia esta factura (la otra
              está cedida a un factor, su cobranza ya está conciliada con el banco…), la
              factura nace POR COBRAR. Sin esta caja se leía solo "Factura emitida" y nadie
              iba nunca a cobrarla. El texto va TAL CUAL viene del backend. */}
          {avisosSii.length > 0 && (
            <div className="p-3 rounded-xl border bg-amber-500/10 border-amber-500/30 text-left space-y-1.5">
              <p className="text-xs font-semibold text-amber-600 dark:text-amber-400 flex items-center gap-1.5">
                <AlertCircle className="w-4 h-4 shrink-0" /> Ojo con el pago de esta factura
              </p>
              {avisosSii.map((a, i) => (
                <p key={i} className="text-xs leading-relaxed" style={{ color: 'var(--text-muted)' }}>{a}</p>
              ))}
            </div>
          )}
          {dte.pdf_url && (
            <button onClick={() => window.open(dte.pdf_url, '_blank', 'noopener,noreferrer')} className="btn-primary text-sm inline-flex items-center gap-1.5">
              <FileText className="w-4 h-4" /> Ver PDF de la factura
            </button>
          )}
          <div><button onClick={cerrar} className="btn-secondary text-sm">Cerrar</button></div>
        </div>
      )}

      {fase === 'pendiente' && (
        <div className="py-6 text-center space-y-2">
          <AlertCircle className="w-8 h-8 mx-auto text-amber-500" />
          <p className="font-semibold text-sm" style={{ color: 'var(--text-primary)' }}>Emisión en curso</p>
          <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
            Hay una emisión en proceso para esta factura (puede ser de otra pestaña u otro usuario).
            Puedes cerrar: en la lista se ve como <b>SII en proceso</b> y <b>no se emitirá dos veces</b>.
          </p>
          <button onClick={cerrar} className="btn-secondary text-sm">Cerrar</button>
        </div>
      )}

      {fase === 'fallido' && (
        <div className="space-y-3">
          <div className="p-3 rounded-xl border bg-red-500/10 border-red-500/30 space-y-1">
            <p className="text-sm font-semibold text-red-500 flex items-center gap-1.5">
              <AlertCircle className="w-4 h-4" />
              {dte?.estado === 'error_envio' ? 'La emisión no llegó a Wasabil' : 'El SII/Wasabil rechazó la factura'}
            </p>
            <p className="text-xs" style={{ color: 'var(--text-muted)' }}>{error || dte?.error || 'Sin detalle del motivo'}</p>
          </div>
          <div className="flex gap-2">
            <button onClick={reintentar} className="btn-primary flex-1 text-sm flex items-center justify-center gap-1.5">
              <RefreshCw className="w-4 h-4" /> Reintentar emisión
            </button>
            <button onClick={cerrar} className="btn-secondary text-sm">Cerrar</button>
          </div>
        </div>
      )}

      {fase === 'error' && (
        <div className="space-y-3">
          <div className="p-3 rounded-xl border bg-red-500/10 border-red-500/30 space-y-1">
            <p className="text-sm font-semibold text-red-500 flex items-center gap-1.5"><AlertCircle className="w-4 h-4" /> No se pudo completar</p>
            <p className="text-xs" style={{ color: 'var(--text-muted)' }}>{error}</p>
            <p className="text-[11px]" style={{ color: 'var(--text-faint)' }}>
              Si la factura alcanzó a crearse, aparece en la lista como <b>SII en proceso</b> o <b>SII fallida</b> y puedes reintentar desde ahí (nunca se emite dos veces).
            </p>
          </div>
          <div className="flex gap-2">
            {/* Volver SOLO si nunca se disparó el emitir (p.ej. falló el preview) */}
            {onVolver && payload && !emisionIntentada && (
              <button onClick={onVolver} className="btn-secondary flex-1 text-sm">← Volver al formulario</button>
            )}
            <button onClick={cerrar} className="btn-secondary flex-1 text-sm">Cerrar</button>
          </div>
        </div>
      )}

      {fase === 'preview' && prev && (
        <>
          {prev.problemas?.length > 0 && (
            <div className="p-3 rounded-xl border bg-red-500/10 border-red-500/30">
              <p className="text-xs font-semibold text-red-500 mb-1">Para emitir falta resolver:</p>
              <ul className="text-xs space-y-0.5 list-disc pl-4" style={{ color: 'var(--text-muted)' }}>
                {prev.problemas.map((p: string, i: number) => <li key={i}>{p}</li>)}
              </ul>
            </div>
          )}
          {prev.advertencias?.length > 0 && (
            <div className="p-3 rounded-xl border bg-amber-500/10 border-amber-500/30">
              <ul className="text-xs space-y-0.5 list-disc pl-4" style={{ color: 'var(--text-muted)' }}>
                {prev.advertencias.map((a: string, i: number) => <li key={i}>{a}</li>)}
              </ul>
            </div>
          )}

          {/* Receptor real (ficha Wasabil = lo que verá el SII) + referencias del DTE */}
          <div className="grid sm:grid-cols-2 gap-3 text-sm">
            <div className="rounded-xl border p-3" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
              <div className="text-[11px] uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>
                Receptor {prev.receptor?.fuente === 'wasabil' ? '(ficha Wasabil)' : '(datos locales)'}
              </div>
              <div className="font-semibold" style={{ color: 'var(--text-primary)' }}>{prev.receptor?.razon_social || '—'}</div>
              <div className="text-xs" style={{ color: 'var(--text-muted)' }}>RUT {prev.receptor?.rut || '—'}</div>
              {prev.receptor?.giro && <div className="text-xs" style={{ color: 'var(--text-muted)' }}>{prev.receptor.giro}</div>}
              {prev.receptor?.direccion && (
                <div className="text-xs" style={{ color: 'var(--text-muted)' }}>
                  {prev.receptor.direccion}{prev.receptor.comuna ? `, ${prev.receptor.comuna}` : ''}
                </div>
              )}
            </div>
            <div className="rounded-xl border p-3" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
              <div className="text-[11px] uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>Referencias del DTE</div>
              {(prev.referencias ?? []).map((r: any, i: number) => (
                <div key={i} className="text-xs" style={{ color: 'var(--text-muted)' }}>
                  {REF_SII_LABEL[String(r.tipo)] || `Doc ${r.tipo}`}: <b style={{ color: 'var(--text-primary)' }}>{r.folio}</b>{r.fecha ? ` · ${fmtDate(r.fecha)}` : ''}
                </div>
              ))}
              {(prev.referencias?.length ?? 0) === 0 && <div className="text-xs" style={{ color: 'var(--text-faint)' }}>Sin referencias</div>}
            </div>
          </div>

          {/* Descuento de anticipo (si la factura descuenta facturas de anticipo previas) */}
          {(prev.descuentos?.length ?? 0) > 0 && (
            <div className="rounded-xl border p-3 text-xs space-y-0.5" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
              <p className="text-[11px] font-semibold uppercase tracking-wider text-emerald-500">Anticipo descontado en esta factura</p>
              {prev.descuentos.map((d: any, i: number) => (
                <p key={i} style={{ color: 'var(--text-muted)' }}>
                  Factura de anticipo {d.folio || `#${d.anticipo_factura_id}`}: <b className="text-emerald-500">−{fmtClp(d.monto_neto)}</b> neto
                </p>
              ))}
            </div>
          )}

          {/* Líneas EXACTAS que se van a emitir (lo que imprimirá el DTE). Sin esta tabla
              el usuario confirmaba un documento tributario real viendo solo el total: un
              ítem de más, una cantidad mal cortada o el descuento del anticipo aplicado a
              la línea equivocada solo se descubrían en el PDF ya emitido al SII. */}
          {(prev.lineas?.length ?? 0) > 0 && (
            <div className="rounded-xl border overflow-hidden" style={{ borderColor: 'var(--border)' }}>
              <table className="w-full text-[11px]">
                <thead>
                  <tr style={{ backgroundColor: 'var(--surface-200)', color: 'var(--text-faint)' }}>
                    <th className="p-2 text-left font-semibold uppercase tracking-wider">Ítem</th>
                    <th className="p-2 text-right font-semibold uppercase tracking-wider">Cant.</th>
                    <th className="p-2 text-right font-semibold uppercase tracking-wider">P. unit. neto</th>
                    <th className="p-2 text-right font-semibold uppercase tracking-wider">Total neto</th>
                  </tr>
                </thead>
                <tbody>
                  {(prev.lineas as PreviewLinea[]).map((ln, i) => (
                    <tr key={i} className="border-t" style={{ borderColor: 'var(--border)' }}>
                      <td className="p-2">
                        <span className="font-mono font-semibold" style={{ color: 'var(--text-primary)' }}>{ln.numero_parte || '—'}</span>
                        {ln.descripcion && <span className="block" style={{ color: 'var(--text-faint)' }}>{ln.descripcion}</span>}
                      </td>
                      <td className="p-2 text-right" style={{ color: 'var(--text-primary)' }}>{ln.cantidad}</td>
                      <td className="p-2 text-right" style={{ color: 'var(--text-muted)' }}>{fmtClp(ln.precio_unit_neto)}</td>
                      <td className="p-2 text-right font-semibold" style={{ color: 'var(--text-primary)' }}>{fmtClp(ln.total_neto)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* Totales que se enviarán al SII */}
          {prev.totales && (
            <div className="rounded-xl border p-3 text-xs flex gap-5" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)', color: 'var(--text-muted)' }}>
              <span>Neto: <b style={{ color: 'var(--text-primary)' }}>{fmtClp(prev.totales.neto)}</b></span>
              <span>IVA 19%: <b style={{ color: 'var(--text-primary)' }}>{fmtClp(prev.totales.iva)}</b></span>
              <span>Total: <b className="text-brand-400">{fmtClp(prev.totales.bruto)}</b></span>
            </div>
          )}

          <p className="text-[11px]" style={{ color: 'var(--text-faint)' }}>
            El folio lo asigna el SII al emitir. Esta emisión es un documento tributario <b>real</b> — revisa el receptor y los montos antes de confirmar.
          </p>
          {/* Aviso explícito + salida sin emitir: el modal solo ofrecía "Volver" (y solo
              desde el formulario), así que abierto desde la lista la única salida visible
              era el botón que emite. Molde: MonzaFacturasPage. */}
          <div className="pt-3 border-t" style={{ borderColor: 'var(--border)' }}>
            <p className="text-[11px] flex items-center gap-1.5 mb-2" style={{ color: 'var(--text-muted)' }}>
              <AlertCircle className="w-3.5 h-3.5 text-amber-500 shrink-0" />
              Al confirmar, la factura se emite al SII a través de Wasabil. Esta acción es <b>IRREVERSIBLE</b>.
            </p>
            <div className="flex gap-2">
              {onVolver && <button onClick={onVolver} className="btn-secondary text-sm">← Volver</button>}
              <button onClick={cerrar} className="btn-secondary text-sm">Cancelar</button>
              <button onClick={emitir} disabled={!prev.puede_emitir}
                className="btn-primary flex-1 text-sm flex items-center justify-center gap-1.5 disabled:opacity-50 disabled:cursor-not-allowed">
                <Receipt className="w-4 h-4" /> Emitir al SII ahora
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  )
}

// ─── Modal: emitir factura (desde un despacho/guía de una OC) ─────────────────
// Antes de emitir se PREVISUALIZA lo que se va a facturar (receptor + líneas + neto/IVA/
// total), se exige el RUT del cliente (campo por llenar si la venta no lo trae) y el folio.
interface PreviewLinea { numero_parte: string | null; descripcion: string | null; cantidad: number; precio_unit_neto: number; total_neto: number }
interface PreviewData {
  puede_emitir: boolean; problemas: string[]; advertencias?: string[]
  receptor: { rut: string; rut_en_venta: boolean; razon_social: string; giro?: string | null; direccion?: string | null }
  lineas: PreviewLinea[]; totales: { neto: number; iva: number; bruto: number }
  precio_de_guia?: boolean
}

/**
 * Una guía facturable tal como la devuelve
 * GET /contabilidad/ventas/{oc}/despachos-facturables.
 *
 * `fecha` + `fuente` son ADITIVOS y tienen la MISMA forma que ya usa el selector
 * de OC (facturas/selectorOc.ts · GuiaFacturable): los deriva el backend con la
 * cascada real de «fecha de la guía facturable» — electrónica emitida al SII
 * primero, papel después. `fecha_guia` es SOLO la columna de la guía en papel y
 * por eso no sirve como fuente de verdad: una guía 52 electrónica la deja NULL
 * (el emisor escribe el folio, nunca esa columna) y toda guía electrónica se
 * pintaba «(sin fecha ⚠)» acá mientras el chip de arriba, calculado con la
 * cascada, mostraba su fecha correcta. Se conserva `fecha_guia` para degradar
 * al comportamiento de siempre si el backend todavía no manda los campos
 * nuevos (`fecha === undefined`): nada debe reventar.
 */
interface DespachoFacturable {
  id: number; numero_despacho: string; numero_guia: string | null
  /** Columna PAPEL cruda (fallback de degradación; ver `fechaDeGuia`). */
  fecha_guia: string | null
  /** Fecha de la guía por la cascada real (ISO date) o null = falta cargarla. */
  fecha?: string | null
  /** 'bloqueada' = guía electrónica trabada ante el SII: no citable en la
   *  referencia 52 — se resuelve en Despachos antes de poder facturar. */
  fuente?: 'electronica' | 'papel' | 'bloqueada' | null
  numero_expedicion: string | null; guia_firmada_archivo: string | null; items_count: number
}

/** Fecha a mostrar para una guía facturable, con degradación explícita: si el
 *  backend no manda `fecha` (campo ausente ≠ null), se cae a la columna papel,
 *  que es exactamente lo que esta pantalla hacía antes. */
function fechaDeGuia(d: DespachoFacturable): string | null {
  return d.fecha !== undefined ? d.fecha : d.fecha_guia
}

function CrearFacturaModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const uid = useId()  // ids únicos para asociar rótulos con widgets compuestos (ver Campo)
  const [ocId, setOcId] = useState<number | ''>('')
  // Condición de pago de la OC elegida: viaja en el onSelect del selector (antes
  // era un ocs.find sobre la lista completa de ventas, que ya no se carga acá).
  const [condPagoSel, setCondPagoSel] = useState('')
  const [despachos, setDespachos] = useState<DespachoFacturable[]>([])
  // Carga de las guías VISIBLE: sin estos dos estados, el .catch mudo dejaba el
  // mensaje «Sin despachos…» tanto durante la carga como tras un error de red —
  // dos mentiras distintas con el mismo texto.
  const [guiasCargando, setGuiasCargando] = useState(false)
  const [guiasError, setGuiasError] = useState(false)
  const [reintentoGuias, setReintentoGuias] = useState(0) // fuerza re-consulta tras un error
  const [despachoId, setDespachoId] = useState<number | ''>('')
  const [folio, setFolio] = useState('')
  const [tipo, setTipo] = useState('factura')
  const [fecha, setFecha] = useState(hoyLocal())
  const [plazo, setPlazo] = useState('30')
  // Observaciones de la factura: la fila expandida ya las MUESTRA, pero no había dónde
  // escribirlas (el backend siempre las aceptó en el payload).
  const [obs, setObs] = useState('')
  const [rut, setRut] = useState('')
  const [rutDebounced, setRutDebounced] = useState('')  // RUT asentado que dispara el preview
  // Razón social: campo por llenar (solo aparece si la venta no la trae); al emitir
  // se guarda en la venta, igual que el RUT.
  const [razonSocial, setRazonSocial] = useState('')
  const [razonDebounced, setRazonDebounced] = useState('')
  const [preview, setPreview] = useState<PreviewData | null>(null)
  const [loadingPrev, setLoadingPrev] = useState(false)
  const [prevError, setPrevError] = useState('')
  const [reintento, setReintento] = useState(0)  // fuerza re-consulta del preview tras un error
  const [saving, setSaving] = useState(false)
  // Modo de emisión: 'sii' (default: Wasabil emite y el SII asigna el folio) o
  // 'manual' (registrar una factura ya emitida a mano, con su folio digitado).
  const [modoSii, setModoSii] = useState(true)
  const [siiPayload, setSiiPayload] = useState<Record<string, any> | null>(null)
  const [siiBusy, setSiiBusy] = useState(false)   // envío en vuelo: el modal no se cierra

  const condPagoOc = condPagoSel

  useEffect(() => {
    if (!ocId) { setDespachos([]); setGuiasCargando(false); setGuiasError(false); return }
    let vivo = true  // ignora respuestas atrasadas si el usuario ya cambió de OC
    setGuiasCargando(true)
    setGuiasError(false)
    contabilidadAPI.despachosFacturables(Number(ocId))
      .then(({ data }) => {
        if (!vivo) return
        const ds = data || []
        setDespachos(ds)
        // UNA sola guía facturable → preseleccionada: el preview se dispara solo
        // (useEffect de despachoId) y el caso típico queda a un clic de emitir.
        if (ds.length === 1) setDespachoId(ds[0].id)
      })
      // Error VISIBLE con reintento (guiasError pinta el aviso y el botón):
      // nada de .catch mudo que se disfraza de «no hay guías».
      .catch(() => { if (vivo) { setDespachos([]); setGuiasError(true) } })
      .finally(() => { if (vivo) setGuiasCargando(false) })
    // Precargar el plazo desde la condición de pago pactada en la OC ("30 días", "contado"…)
    const cp = (condPagoOc || '').toLowerCase()
    const m = cp.match(/(\d+)/)
    if (m) setPlazo(m[1])
    else if (cp.includes('contado') || cp.includes('contra entrega')) setPlazo('0')
    else setPlazo('30')  // cond_pago no reconocida: default 30 (no hereda el de otra OC)
    return () => { vivo = false }
  }, [ocId, reintentoGuias])

  // El RUT digitado entra al preview con DEBOUNCE (~500ms): así el backend valida el
  // RUT nuevo y desbloquea la emisión (una venta sin RUT se completa AQUÍ), sin
  // re-consultar en cada tecla.
  useEffect(() => {
    const t = window.setTimeout(() => setRutDebounced(rut.trim()), 500)
    return () => window.clearTimeout(t)
  }, [rut])
  useEffect(() => {
    const t = window.setTimeout(() => setRazonDebounced(razonSocial.trim()), 500)
    return () => window.clearTimeout(t)
  }, [razonSocial])

  // Previsualiza al elegir la guía (y cuando el RUT / razón social digitados se asientan)
  useEffect(() => {
    if (!ocId || !despachoId) { setPreview(null); return }
    let vivo = true
    setLoadingPrev(true)
    setPrevError('')
    contabilidadAPI.previewFactura({
      oc_cliente_id: Number(ocId), despacho_id: Number(despachoId),
      rut_cliente: rutDebounced || undefined,
      razon_social_cliente: razonDebounced || undefined,
    })
      .then(({ data }) => {
        if (!vivo) return
        setPreview(data)
        // Prefijar el RUT desde la venta la primera vez (forma funcional: no pisa
        // lo que el usuario esté tipeando mientras la respuesta venía en vuelo)
        if (data.receptor?.rut) setRut(prev => prev || data.receptor.rut)
      })
      .catch((e: any) => {
        if (!vivo) return
        setPreview(null)
        setPrevError(errMsg(e, 'No se pudo calcular la previsualización'))
      })
      .finally(() => { if (vivo) setLoadingPrev(false) })
    return () => { vivo = false }
  }, [ocId, despachoId, rutDebounced, razonDebounced, reintento])

  const rutFaltante = !rut.trim()
  const folioFaltante = tipo === 'factura' && !modoSii && !folio.trim()
  const bloqueadoDatos = (preview?.problemas?.length ?? 0) > 0
  const puedeEmitir = !!preview && preview.lineas.length > 0 && !rutFaltante && !folioFaltante && !bloqueadoDatos && !loadingPrev

  // Payload común a ambos modos (el folio SOLO va en modo manual: en SII lo asigna el SII)
  const armarPayload = () => ({
    oc_cliente_id: Number(ocId), despacho_id: Number(despachoId),
    tipo_doc: tipo,
    fecha_emision: fecha, plazo_dias: plazo ? Number(plazo) : undefined,
    // Condición real pactada en la OC; si no hay, se deriva del plazo
    condicion_pago: condPagoOc || (plazo ? `${plazo} días` : undefined),
    observaciones: obs.trim() || undefined,
    rut_cliente: rut || undefined,
    razon_social_cliente: razonSocial.trim() || undefined,
  })

  const submit = async () => {
    if (!ocId || !despachoId) { toast.error('Selecciona OC y despacho'); return }
    if (rutFaltante) { toast.error('Ingresa el RUT del cliente'); return }
    if (modoSii) {
      if (tipo !== 'factura') { toast.error('La emisión electrónica es solo para facturas; una boleta regístrala como ya emitida'); return }
      setSiiPayload(armarPayload())  // pasa al flujo SII (preview Wasabil → emitir)
      return
    }
    if (folioFaltante) { toast.error('Ingresa el folio SII de la factura'); return }
    setSaving(true)
    try {
      const resp: RespuestaConAvisos =
        (await contabilidadAPI.crearFactura({ ...armarPayload(), numero_factura: folio || undefined })).data
      toast.success('Factura registrada')
      // Avisos NO bloqueantes del 200 (p. ej. "el descuento del anticipo deja esta
      // factura en $0"): antes se descartaban y la factura quedaba rara sin explicación.
      avisarAdvertencias(resp?.advertencias)
      onDone(); onClose()
    } catch (e: any) {
      toast.error(errMsg(e, 'No se pudo registrar la factura'))
    } finally { setSaving(false) }
  }

  // Flujo SII en curso: el formulario cede el modal al componente de emisión
  if (siiPayload) return (
    <Modal title="Emitir factura electrónica (SII)" onClose={siiBusy ? () => {} : onClose} wide>
      <EmisionFacturaSII payload={siiPayload} onDone={onDone}
        onVolver={() => setSiiPayload(null)} onCerrar={onClose} onBusy={setSiiBusy} />
    </Modal>
  )

  return (
    <Modal title="Emitir factura" onClose={onClose} wide>
      {/* Campo (div), NO Field (label): el selector es un widget COMPUESTO — con
          <label> el clic en el rótulo o en el texto de la OC disparaba «cambiar»
          y borraba el formulario a medio llenar. El rótulo apunta a la caja de
          búsqueda por htmlFor. */}
      <Campo label="Orden de compra (venta)" htmlFor={`${uid}-oc-buscar`}>
        {/* setRutDebounced('') SÍNCRONO: sin esto, el RUT del cliente de la OC anterior
            queda “en vuelo” hasta 500ms y podía filtrarse al preview de la OC nueva */}
        <SelectorOcFactura contexto="factura" value={ocId} autoFocus inputId={`${uid}-oc-buscar`}
          onSelect={o => { setOcId(o ? o.oc_cliente_id : ''); setDespachoId(''); setPreview(null); setRut(''); setRutDebounced(''); setRazonSocial(''); setRazonDebounced(''); setCondPagoSel(o?.cond_pago ?? '') }} />
      </Campo>
      {/* Campo (div), NO Field (label): TODO este bloque —filas-radio, ayuda,
          «Exp:…», el botón Reintentar de la rama de error— vivía dentro de un
          <label> cuyo primer control es el PRIMER radio; un clic en cualquiera
          de esos textos cambiaba EN SILENCIO qué guía 52 iba a citar un DTE 33
          irreversible. El grupo se declara con role="radiogroup". */}
      <Campo label="Despacho / guía a facturar" labelId={`${uid}-guias-rotulo`}>
        {!ocId ? (
          <p className="text-xs px-3 py-2 rounded-lg border" style={{ ...inputStyle, color: 'var(--text-faint)' }}>Elige una OC primero</p>
        ) : guiasCargando ? (
          <p className="text-xs px-3 py-2 rounded-lg border" style={{ ...inputStyle, color: 'var(--text-faint)' }}>
            <Loader2 className="w-3.5 h-3.5 animate-spin inline mr-1" /> Cargando guías…
          </p>
        ) : guiasError ? (
          <div className="text-xs px-3 py-2 rounded-lg border space-y-1.5" style={inputStyle}>
            <p className="text-red-500 flex items-center gap-1.5"><AlertCircle className="w-4 h-4" /> No se pudieron cargar las guías de esta OC</p>
            <button type="button" onClick={() => setReintentoGuias(n => n + 1)} className="btn-secondary text-xs">Reintentar</button>
          </div>
        ) : despachos.length === 0 ? (
          // SOLO cuando la carga terminó OK y de verdad no hay nada facturable.
          <p className="text-xs px-3 py-2 rounded-lg border" style={{ ...inputStyle, color: 'var(--text-faint)' }}>Sin despachos cerrados para facturar</p>
        ) : (
          /* Filas-radio (2-4 típicas) en vez de <select>: la guía se ve COMPLETA
             (folio, fecha, ítems) antes de elegir.
             La FECHA sale de la cascada del backend (`fecha`/`fuente`), la misma
             que alimenta el chip del selector de arriba — antes esta lista leía
             la columna papel cruda y toda guía electrónica se pintaba «(sin
             fecha ⚠)» a 40 píxeles de un chip que mostraba su fecha correcta.
             Quedan TRES avisos, con remedios distintos — y el tercero existe
             porque el genérico daba un consejo IMPOSIBLE:
              · sin fecha (papel)      → guía en papel a la que le falta cargar la
                fecha; la referencia 52 la exige, se carga en Despachos → Editar.
              · sin fecha (electrónica)→ el DTE de la guía no trajo documentDate;
                NO hay nada que cargar en Despachos y la emisión igual funciona.
              · bloqueada              → guía electrónica trabada ante el SII; NO
                se arregla tecleando una fecha, hay que resolverla en Despachos.
             Sin estos avisos el bloqueo aparecía recién al emitir. */
          <div role="radiogroup" aria-labelledby={`${uid}-guias-rotulo`}
            className="rounded-lg border overflow-hidden" style={{ borderColor: 'var(--border)' }}>
            {despachos.map(d => {
              const fecha = fechaDeGuia(d)
              return (
              // Este <label> SÍ es correcto: envuelve UN control (su radio) —
              // clickear la fila entera marca esa guía, que es lo esperado.
              <label key={d.id} className="flex items-center flex-wrap gap-x-2 gap-y-0.5 px-3 py-2 text-xs cursor-pointer border-b last:border-b-0 hover:bg-[var(--surface-200)]"
                style={{ borderColor: 'var(--border)' }}>
                <input type="radio" name="despacho-a-facturar" checked={despachoId === d.id}
                  onChange={() => { setDespachoId(d.id); setPreview(null) }} />
                <span className="font-mono font-semibold" style={{ color: 'var(--text-primary)' }}>{d.numero_despacho}</span>
                <span style={{ color: 'var(--text-muted)' }}>· Guía {d.numero_guia || '—'}</span>
                {d.fuente === 'bloqueada' ? (
                  // Mismo vocabulario que el chip del selector de OC, para que el
                  // operador reconozca el estado: es el MISMO problema.
                  <span className="text-amber-500" title="Guía bloqueada en SII — revísala en Despachos antes de facturar">
                    · ⚠ guía bloqueada en SII
                  </span>
                ) : fecha ? (
                  <span style={{ color: 'var(--text-muted)' }}
                    title={d.fuente === 'electronica'
                      ? 'Fecha de emisión de la guía electrónica ante el SII'
                      : 'Fecha de la guía en papel cargada en Despachos'}>
                    · {fmtDate(fecha)}
                    {d.fuente === 'electronica' && (
                      <span className="ml-1 text-[10px] uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>SII</span>
                    )}
                  </span>
                ) : d.fuente === 'electronica' ? (
                  /* Guía ELECTRÓNICA cuyo DTE no trajo documentDate: el backend
                     manda fecha=null con fuente='electronica'. Antes caía en la
                     rama genérica de abajo y pedía «cárgala en Despachos →
                     Editar», un remedio IMPOSIBLE: la cascada ya se resolvió por
                     la rama electrónica, así que escribir la columna papel no
                     cambia nada — y encima la emisión SÍ funciona (el emisor real
                     cae a otra fecha). Aviso SOBRIO, sin ámbar y sin instrucción:
                     un consejo que no arregla nada es peor que ningún consejo. */
                  <span style={{ color: 'var(--text-faint)' }}
                    title="La guía electrónica no informa su fecha de emisión. Se puede facturar igual: la fecha de la referencia la resuelve el emisor.">
                    · sin fecha en el documento
                    <span className="ml-1 text-[10px] uppercase tracking-wider">SII</span>
                  </span>
                ) : (
                  <span className="text-amber-500" title="La referencia 52 exige la fecha de emisión de la guía: cárgala en Despachos → Editar antes de facturar">
                    · (sin fecha ⚠)
                  </span>
                )}
                <span style={{ color: 'var(--text-faint)' }}>· {d.items_count} ítem{d.items_count === 1 ? '' : 's'}</span>
              </label>
              )
            })}
          </div>
        )}
        <p className="text-[11px] mt-1" style={{ color: 'var(--text-faint)' }}>Solo se listan guías de despacho <b>firmadas</b> (entregadas) y aún no facturadas.</p>
        {despachoId !== '' && (() => {
          const d = despachos.find(x => x.id === Number(despachoId))
          if (!d) return null
          return (
            <div className="flex items-center flex-wrap gap-x-3 gap-y-1 mt-2 text-[11px]" style={{ color: 'var(--text-muted)' }}>
              {d.numero_expedicion && <span>Exp: <b style={{ color: 'var(--text-primary)' }}>{d.numero_expedicion}</b></span>}
              {d.guia_firmada_archivo ? (
                <button type="button" onClick={() => abrirDocumento(d.guia_firmada_archivo!)}
                  className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 hover:bg-emerald-500/20">
                  <FileText className="w-3 h-3" /> Ver guía firmada
                </button>
              ) : (
                <span className="text-amber-500">⚠ Esta guía no tiene foto de respaldo adjunta</span>
              )}
            </div>
          )
        })()}
      </Campo>

      {/* Receptor + RUT (campo por llenar si la venta no lo trae) */}
      {despachoId !== '' && (
        <div className="rounded-xl border p-3 space-y-2" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
          <div className="text-[11px] uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>Receptor</div>
          <div className="text-sm font-semibold" style={{ color: 'var(--text-primary)' }}>{preview?.receptor?.razon_social || '—'}</div>
          {(preview?.receptor?.giro || preview?.receptor?.direccion) && (
            <div className="text-[11px] space-y-0.5" style={{ color: 'var(--text-muted)' }}>
              {preview?.receptor?.giro && <div>Giro: {preview.receptor.giro}</div>}
              {preview?.receptor?.direccion && <div>Dirección: {preview.receptor.direccion}</div>}
            </div>
          )}
          <Field label={`RUT del cliente${preview && !preview.receptor.rut_en_venta ? ' · falta en la venta, complétalo' : ''}`}>
            <input className={inputCls} style={inputStyle} value={rut} onChange={e => setRut(e.target.value)} placeholder="Ej. 78.279.030-7" />
          </Field>
          {preview && !preview.receptor.rut_en_venta && rut.trim() && (
            <p className="text-[11px] text-amber-500">Se guardará este RUT en la venta para dejarla completa.</p>
          )}
          {/* Razón social: campo por llenar SOLO si la venta no la trae (se guarda en la venta) */}
          {preview && (!preview.receptor.razon_social || razonSocial) && (
            <>
              <Field label="Razón social del cliente · falta en la venta, complétala">
                <input className={inputCls} style={inputStyle} value={razonSocial} onChange={e => setRazonSocial(e.target.value)} placeholder="Ej. H-E PARTS INTERNATIONAL CHILE SPA" />
              </Field>
              {razonSocial.trim() && (
                <p className="text-[11px] text-amber-500">Se guardará esta razón social en la venta para dejarla completa.</p>
              )}
            </>
          )}
        </div>
      )}

      {/* Previsualización de líneas y montos (lo que se va a facturar) */}
      {despachoId !== '' && (
        <div className="rounded-xl border overflow-hidden" style={{ borderColor: 'var(--border)' }}>
          {loadingPrev ? (
            <div className="p-4 text-center text-xs" style={{ color: 'var(--text-muted)' }}><Loader2 className="w-4 h-4 animate-spin inline mr-1" /> Calculando…</div>
          ) : !preview ? (
            <div className="p-4 text-center text-xs space-y-2" style={{ color: 'var(--text-faint)' }}>
              <div className={prevError ? 'text-red-500' : ''}>{prevError || 'Sin previsualización'}</div>
              {prevError && (
                <button type="button" onClick={() => setReintento(n => n + 1)} className="btn-secondary text-xs">
                  Reintentar
                </button>
              )}
            </div>
          ) : (
            <>
              {preview.problemas.length > 0 && (
                <div className="p-3 border-b bg-red-500/10" style={{ borderColor: 'var(--border)' }}>
                  <ul className="text-[11px] space-y-0.5 list-disc pl-4 text-red-500">
                    {preview.problemas.map((p, i) => <li key={i}>{p}</li>)}
                  </ul>
                </div>
              )}
              {(preview.advertencias?.length ?? 0) > 0 && (
                <div className="p-3 border-b bg-amber-500/10" style={{ borderColor: 'var(--border)' }}>
                  <ul className="text-[11px] space-y-0.5 list-disc pl-4 text-amber-600 dark:text-amber-400">
                    {preview.advertencias!.map((a, i) => <li key={i}>{a}</li>)}
                  </ul>
                </div>
              )}
              <table className="w-full text-[11px]">
                <thead>
                  <tr style={{ backgroundColor: 'var(--surface-200)', color: 'var(--text-faint)' }}>
                    <th className="p-2 text-left font-semibold uppercase tracking-wider">Ítem</th>
                    <th className="p-2 text-right font-semibold uppercase tracking-wider">Cant.</th>
                    <th className="p-2 text-right font-semibold uppercase tracking-wider">P. neto</th>
                    <th className="p-2 text-right font-semibold uppercase tracking-wider">Total</th>
                  </tr>
                </thead>
                <tbody>
                  {preview.lineas.map((ln, i) => (
                    <tr key={i} className="border-t" style={{ borderColor: 'var(--border)' }}>
                      <td className="p-2">
                        <span className="font-mono font-semibold" style={{ color: 'var(--text-primary)' }}>{ln.numero_parte || '—'}</span>
                        {ln.descripcion && <span className="block" style={{ color: 'var(--text-faint)' }}>{ln.descripcion}</span>}
                      </td>
                      <td className="p-2 text-right" style={{ color: 'var(--text-primary)' }}>{ln.cantidad}</td>
                      <td className="p-2 text-right" style={{ color: 'var(--text-muted)' }}>{fmtClp(ln.precio_unit_neto)}</td>
                      <td className="p-2 text-right font-semibold" style={{ color: 'var(--text-primary)' }}>{fmtClp(ln.total_neto)}</td>
                    </tr>
                  ))}
                </tbody>
                <tfoot style={{ backgroundColor: 'var(--surface-200)' }}>
                  <tr className="border-t" style={{ borderColor: 'var(--border)' }}><td colSpan={3} className="p-2 text-right uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>Neto</td><td className="p-2 text-right font-semibold" style={{ color: 'var(--text-primary)' }}>{fmtClp(preview.totales.neto)}</td></tr>
                  <tr><td colSpan={3} className="p-2 text-right uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>IVA 19%</td><td className="p-2 text-right" style={{ color: 'var(--text-muted)' }}>{fmtClp(preview.totales.iva)}</td></tr>
                  <tr><td colSpan={3} className="p-2 text-right uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>Total</td><td className="p-2 text-right font-bold text-brand-500">{fmtClp(preview.totales.bruto)}</td></tr>
                </tfoot>
              </table>
              {preview.precio_de_guia && (
                <div className="px-3 py-2 border-t text-[11px] text-emerald-600 dark:text-emerald-400" style={{ borderColor: 'var(--border)' }}>
                  ✓ Precios tomados de la guía electrónica ya emitida al SII (cuadran con la guía 52).
                </div>
              )}
            </>
          )}
        </div>
      )}

      <div className="grid grid-cols-2 gap-3">
        {modoSii ? (
          <Field label="N° Factura (folio SII)">
            <div className={inputCls} style={{ ...inputStyle, color: 'var(--text-faint)' }}>Lo asigna el SII al emitir</div>
          </Field>
        ) : (
          <Field label={`N° Factura (folio SII)${tipo === 'factura' ? ' *' : ''}`}><input className={inputCls} style={inputStyle} value={folio} onChange={e => setFolio(e.target.value)} placeholder="Ej. 35" /></Field>
        )}
        <Field label="Tipo">
          {/* Elegir BOLETA cae automáticamente al registro manual: el DTE 33 es solo
              factura. Antes el modo SII quedaba activo y el usuario recién se enteraba al
              apretar "Emitir factura al SII" (toast de rechazo). Volver a "factura" repone
              el modo SII, que es el default. Molde: MonzaFacturasPage. */}
          <select className={inputCls} style={inputStyle} value={tipo}
            onChange={e => { setTipo(e.target.value); setModoSii(e.target.value === 'factura') }}>
            <option value="factura">Factura</option><option value="boleta">Boleta</option>
          </select>
        </Field>
        <Field label="Fecha emisión"><input type="date" className={inputCls} style={inputStyle} value={fecha} onChange={e => setFecha(e.target.value)} /></Field>
        <Field label="Plazo (días)"><input type="number" min={0} step={1} className={inputCls} style={inputStyle} value={plazo} onChange={e => setPlazo(e.target.value)} /></Field>
      </div>
      <Field label="Observaciones (opcional)">
        <input className={inputCls} style={inputStyle} value={obs} onChange={e => setObs(e.target.value)}
          placeholder="Notas de la factura (se ven en la fila y en el detalle)" />
      </Field>
      <button onClick={submit} disabled={saving || !puedeEmitir} className="btn-primary w-full flex items-center justify-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed">
        {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Receipt className="w-4 h-4" />} {modoSii ? 'Emitir factura al SII' : 'Registrar factura emitida'}
      </button>
      <button type="button" onClick={() => setModoSii(m => !m)} className="w-full text-center text-[11px] underline underline-offset-2" style={{ color: 'var(--text-muted)' }}>
        {modoSii ? '¿La factura ya fue emitida a mano en el SII? Regístrala con su folio' : '← Volver a emitir al SII (folio automático)'}
      </button>
    </Modal>
  )
}

// ─── Modal: factura de ANTICIPO (respalda un adelanto; sin guía de despacho) ───
// Única excepción a la regla "solo se factura una guía firmada": esta factura se
// emite para respaldar ante el SII un adelanto del cliente. Al facturar el despacho
// real, el sistema le descuenta el anticipo automáticamente (línea negativa).
interface AdelantoVenta {
  id: number; estado: string; monto_esperado: number; pct: number | null
  monto: number; monto_aplicado: number; factura_anticipo_id: number | null
}
/** Factura de anticipo que la venta YA tiene (se lee del detalle de la OC al elegirla,
 *  ANTES del clic: este botón emite un documento tributario real e irreversible). */
interface AnticipoPrevio { id: number; numero_factura: string | null; monto_bruto: number }
/** Forma mínima de las facturas que devuelve GET /contabilidad/ventas/{oc_id}. */
type FacturaDeVenta = { id: number; numero_factura: string | null; monto_bruto: number; es_anticipo?: boolean }

function AnticipoFacturaModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const uid = useId()  // ids únicos para asociar rótulos con widgets compuestos (ver Campo)
  const [ocId, setOcId] = useState<number | ''>('')
  const [adelantos, setAdelantos] = useState<AdelantoVenta[]>([])
  const [adelSel, setAdelSel] = useState<Set<number>>(new Set())
  const [montoNeto, setMontoNeto] = useState('')
  const [folio, setFolio] = useState('')
  const [fecha, setFecha] = useState(hoyLocal())
  // Un anticipo se paga AL CONTADO: por defecto 0 días. Este payload NO mandaba plazo ni
  // condición (el de la factura normal sí), así que el backend dejaba fecha_vencimiento en
  // NULL: el anticipo nunca vencía, nunca salía en el filtro "Vencidas" y nunca entraba al
  // KPI "Vencido" — un anticipo impago no avisaba NUNCA. Molde: MonzaFacturasPage.
  const [plazo, setPlazo] = useState('0')
  // Facturas de anticipo que la OC YA tiene + la puerta EXPLÍCITA al segundo: emitir un
  // DTE 33 es irreversible y el backend responde 409 si la casilla no viene marcada.
  const [anticiposPrevios, setAnticiposPrevios] = useState<AnticipoPrevio[]>([])
  const [confirmarSegundo, setConfirmarSegundo] = useState(false)
  const [rut, setRut] = useState('')
  const [rutDebounced, setRutDebounced] = useState('')  // RUT asentado que dispara el preview
  // Razón social: campo por llenar (solo aparece si la venta no la trae); al emitir
  // se guarda en la venta, igual que el RUT.
  const [razonSocial, setRazonSocial] = useState('')
  const [razonDebounced, setRazonDebounced] = useState('')
  const [preview, setPreview] = useState<PreviewData | null>(null)
  const [prevError, setPrevError] = useState('')
  const [saving, setSaving] = useState(false)
  // Modo de emisión: 'sii' (default: Wasabil emite y el SII asigna el folio) o
  // 'manual' (registrar una factura de anticipo ya emitida a mano, con su folio).
  const [modoSii, setModoSii] = useState(true)
  const [siiPayload, setSiiPayload] = useState<Record<string, any> | null>(null)
  const [siiBusy, setSiiBusy] = useState(false)   // envío en vuelo: el modal no se cierra

  // Mismo patrón que CrearFacturaModal: el RUT / razón social digitados re-consultan
  // el preview (con debounce) — sin esto, una venta SIN esos datos quedaba
  // infacturable como anticipo porque puede_emitir nunca se recalculaba.
  useEffect(() => {
    const t = window.setTimeout(() => setRutDebounced(rut.trim()), 500)
    return () => window.clearTimeout(t)
  }, [rut])
  useEffect(() => {
    const t = window.setTimeout(() => setRazonDebounced(razonSocial.trim()), 500)
    return () => window.clearTimeout(t)
  }, [razonSocial])

  // Adelantos de la OC que se pueden respaldar (no anulados, sin otra factura de
  // anticipo, sin aplicaciones). Sugiere el monto neto desde lo informado (bruto/1.19).
  useEffect(() => {
    setAdelSel(new Set()); setAdelantos([]); setPreview(null)
    if (!ocId) return
    // `vivo`: sin este guard, la respuesta atrasada de la OC anterior poblaba los
    // adelantos (y el monto sugerido) de la OC recién elegida.
    let vivo = true
    contabilidadAPI.adelantosDeVenta(Number(ocId)).then(({ data }) => {
      if (!vivo) return
      const cand = (data || []).filter((a: AdelantoVenta) =>
        a.estado !== 'anulado' && !a.factura_anticipo_id && (a.monto_aplicado || 0) <= 1)
      setAdelantos(cand)
      if (cand.length === 1) {
        setAdelSel(new Set([cand[0].id]))
        const bruto = cand[0].estado === 'aprobado' ? cand[0].monto : cand[0].monto_esperado
        if (bruto > 0 && !montoNeto) setMontoNeto(String(Math.round(bruto / 1.19)))
      }
    }).catch(() => { if (vivo) setAdelantos([]) })
    return () => { vivo = false }
  }, [ocId])

  // Facturas de anticipo que la venta YA tiene: el aviso va ANTES del clic. El candado
  // anti doble emisión de wasabil_dte solo dura mientras el HTTP está en vuelo, así que
  // dos clics tranquilos emitían DOS facturas de anticipo REALES por el mismo adelanto.
  useEffect(() => {
    setAnticiposPrevios([]); setConfirmarSegundo(false)
    if (!ocId) return
    let vivo = true
    contabilidadAPI.ventaDetalle(Number(ocId)).then(({ data }) => {
      if (!vivo) return
      const facturas: FacturaDeVenta[] = data?.facturas || []
      setAnticiposPrevios(facturas.filter(f => !!f.es_anticipo)
        .map(f => ({ id: f.id, numero_factura: f.numero_factura, monto_bruto: f.monto_bruto })))
    }).catch(() => { /* sin aviso previo: el backend igual bloquea el 2º anticipo con su 409 */ })
    return () => { vivo = false }
  }, [ocId])

  // Previsualiza con la misma fuente de verdad que la emisión
  useEffect(() => {
    if (!ocId || !montoNeto || Number(montoNeto) <= 0) { setPreview(null); return }
    let vivo = true
    contabilidadAPI.previewFactura({
      oc_cliente_id: Number(ocId), es_anticipo: true,
      monto_neto_anticipo: Number(montoNeto), adelanto_ids: [...adelSel],
      rut_cliente: rutDebounced || undefined,
      razon_social_cliente: razonDebounced || undefined,
      // Sin esto el preview seguiría pintando "Esta venta ya tiene una factura de
      // anticipo" (y el botón gris) aunque el operador ya marcó la casilla.
      ...(confirmarSegundo ? { confirmar_segundo_anticipo: true } : {}),
    }).then(({ data }) => {
      if (!vivo) return
      setPreview(data); setPrevError('')
      if (!rut && data.receptor?.rut) setRut(prev => prev || data.receptor.rut)
    }).catch((e: any) => {
      // Antes se tragaba el error: el botón quedaba gris sin decir por qué
      if (!vivo) return
      setPreview(null)
      setPrevError(errMsg(e, 'No se pudo calcular la previsualización'))
    })
    return () => { vivo = false }
  }, [ocId, montoNeto, adelSel, rutDebounced, razonDebounced, confirmarSegundo])

  const toggleAdel = (id: number) => setAdelSel(prev => {
    const s = new Set(prev); s.has(id) ? s.delete(id) : s.add(id); return s
  })
  const puedeEmitir = !!preview && preview.puede_emitir && (modoSii || !!folio.trim()) && !!rut.trim()

  // Payload común a ambos modos (el folio SOLO va en modo manual: en SII lo asigna el SII)
  const armarPayload = () => ({
    oc_cliente_id: Number(ocId), es_anticipo: true,
    monto_neto_anticipo: Number(montoNeto), adelanto_ids: [...adelSel],
    tipo_doc: 'factura', fecha_emision: fecha,
    // Plazo: 0 = al contado (lo normal en un anticipo). Vacío = sin vencimiento (y sin
    // alarmas: no entra a "Vencidas" ni al KPI "Vencido").
    plazo_dias: plazo === '' ? undefined : Number(plazo),
    condicion_pago: plazo === '' ? undefined : (Number(plazo) === 0 ? 'Contado' : `${plazo} días`),
    rut_cliente: rut || undefined,
    razon_social_cliente: razonSocial.trim() || undefined,
    // Solo viaja marcado: sin esto el backend bloquea el 2º anticipo de la venta.
    ...(confirmarSegundo ? { confirmar_segundo_anticipo: true } : {}),
  })

  const submit = async () => {
    if (!ocId) { toast.error('Selecciona la OC'); return }
    if (!montoNeto || Number(montoNeto) <= 0) { toast.error('Indica el monto neto del anticipo'); return }
    if (!rut.trim()) { toast.error('Ingresa el RUT del cliente'); return }
    if (modoSii) {
      setSiiPayload(armarPayload())  // pasa al flujo SII (preview Wasabil → emitir)
      return
    }
    if (!folio.trim()) { toast.error('Ingresa el folio SII de la factura'); return }
    setSaving(true)
    try {
      const resp: RespuestaConAvisos =
        (await contabilidadAPI.crearFactura({ ...armarPayload(), numero_factura: folio })).data
      toast.success('Factura de anticipo emitida — al facturar el despacho real se descuenta sola')
      // Acá salen los avisos que cambian lo que el usuario cree que pasó (p. ej. que el
      // adelanto NO se pudo mover a esta factura y quedó por cobrar).
      avisarAdvertencias(resp?.advertencias)
      onDone(); onClose()
      // El 409 del segundo anticipo llega tal cual desde el backend (nombra el que ya
      // existe, con folio y monto): errMsg lo muestra sin reescribirlo.
    } catch (e: any) { toast.error(errMsg(e, 'No se pudo emitir la factura de anticipo')) } finally { setSaving(false) }
  }

  // Flujo SII en curso: el formulario cede el modal al componente de emisión
  if (siiPayload) return (
    <Modal title="Emitir factura de anticipo al SII" onClose={siiBusy ? () => {} : onClose} wide>
      <EmisionFacturaSII payload={siiPayload} onDone={onDone}
        onVolver={() => setSiiPayload(null)} onCerrar={onClose} onBusy={setSiiBusy} />
    </Modal>
  )

  return (
    <Modal title="Factura de anticipo (sin guía de despacho)" onClose={onClose} wide>
      <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
        Respalda ante el SII un <b>adelanto del cliente</b> antes del despacho. Cuando después factures la guía
        firmada, el sistema le <b>descuenta este anticipo automáticamente</b> para no cobrar dos veces.
      </p>
      {/* Campo (div), NO Field (label): mismo widget compuesto que en «Emitir
          factura» — con <label>, un clic en el rótulo o en el texto de la OC
          activaba «cambiar» y acá se perdían además el monto neto y los
          adelantos marcados. */}
      <Campo label="Orden de compra (venta)" htmlFor={`${uid}-oc-buscar`}>
        {/* setRutDebounced('') SÍNCRONO: sin esto, el RUT de la OC anterior podía
            quedar "en vuelo" 500ms y filtrarse al preview de la OC nueva.
            contexto="anticipo": lista plana SIN semáforo de guías — el anticipo se
            emite ANTES del despacho, ese semáforo acá sería un dato falso. */}
        <SelectorOcFactura contexto="anticipo" value={ocId} autoFocus inputId={`${uid}-oc-buscar`}
          onSelect={o => { setOcId(o ? o.oc_cliente_id : ''); setRut(''); setRutDebounced(''); setRazonSocial(''); setRazonDebounced(''); setMontoNeto('') }} />
      </Campo>
      {ocId !== '' && adelantos.length > 0 && (
        <div className="rounded-xl border p-3 space-y-1.5" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
          <p className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>Adelantos de esta venta que respalda</p>
          {adelantos.map(a => (
            <label key={a.id} className="flex items-center gap-2 text-xs cursor-pointer" style={{ color: 'var(--text-primary)' }}>
              <input type="checkbox" checked={adelSel.has(a.id)} onChange={() => toggleAdel(a.id)} />
              {a.estado === 'aprobado'
                ? <>Aprobado por Tesorería · <b>{fmtClp(a.monto)}</b> — al emitir, la factura quedará <b className="text-emerald-500">pagada</b> al tiro</>
                : <>Informado (espera aprobación) · esperado <b>{fmtClp(a.monto_esperado)}</b>{a.pct ? ` (${a.pct}%)` : ''} — quedará pagada cuando Tesorería apruebe</>}
            </label>
          ))}
        </div>
      )}
      {ocId !== '' && adelantos.length === 0 && (
        <p className="text-[11px]" style={{ color: 'var(--text-faint)' }}>
          Esta venta no tiene adelantos informados disponibles: puedes emitir igual la factura de anticipo, y el pago
          se registra como cobranza normal (o informa el adelanto primero desde Ventas / Cierre de Venta).
        </p>
      )}
      {/* Aviso ANTES del clic que emite: esta venta ya tiene factura(s) de anticipo. En
          Grupo AM un anticipo parcial adicional es legítimo (cada adelanto se liga a SU
          anticipo), así que no se prohíbe: se exige decirlo a propósito. */}
      {ocId !== '' && anticiposPrevios.length > 0 && (
        <div className="rounded-xl border p-3 bg-amber-500/10 border-amber-500/30 space-y-1.5">
          <p className="text-xs" style={{ color: 'var(--text-primary)' }}>
            <b>Esta venta ya tiene factura de anticipo:</b>{' '}
            {anticiposPrevios.map(a => `N° ${a.numero_factura || `#${a.id}`} (${fmtClp(a.monto_bruto)})`).join(' · ')}
          </p>
          <p className="text-[11px]" style={{ color: 'var(--text-muted)' }}>
            Emitir otra crea un <b style={{ color: 'var(--text-primary)' }}>segundo documento tributario real</b> por la
            misma plata. Si el anticipo pactado es por partes, márcalo; si no es lo que quieres, cierra esta ventana.
          </p>
          <label className="flex items-center gap-2 text-xs cursor-pointer" style={{ color: 'var(--text-primary)' }}>
            <input type="checkbox" checked={confirmarSegundo} onChange={e => setConfirmarSegundo(e.target.checked)} />
            Sí, necesito un segundo anticipo para esta venta
          </label>
        </div>
      )}
      <div className="grid grid-cols-2 gap-3">
        <Field label="Monto NETO del anticipo (CLP)"><input type="number" className={inputCls} style={inputStyle} value={montoNeto} onChange={e => setMontoNeto(e.target.value)} placeholder="Ej. 50000" /></Field>
        <Field label="RUT del cliente"><input className={inputCls} style={inputStyle} value={rut} onChange={e => setRut(e.target.value)} placeholder="Ej. 78.279.030-7" /></Field>
      </div>
      {/* Razón social: campo por llenar SOLO si la venta no la trae (se guarda en la venta) */}
      {preview && (!preview.receptor?.razon_social || razonSocial) && (
        <Field label="Razón social del cliente · falta en la venta, complétala">
          <input className={inputCls} style={inputStyle} value={razonSocial} onChange={e => setRazonSocial(e.target.value)} placeholder="Ej. H-E PARTS INTERNATIONAL CHILE SPA" />
        </Field>
      )}
      <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
        {modoSii ? (
          <Field label="N° Factura (folio SII)">
            <div className={inputCls} style={{ ...inputStyle, color: 'var(--text-faint)' }}>Lo asigna el SII al emitir</div>
          </Field>
        ) : (
          <Field label="N° Factura (folio SII) *"><input className={inputCls} style={inputStyle} value={folio} onChange={e => setFolio(e.target.value)} placeholder="Ej. 42" /></Field>
        )}
        <Field label="Fecha emisión"><input type="date" className={inputCls} style={inputStyle} value={fecha} onChange={e => setFecha(e.target.value)} /></Field>
        <Field label="Plazo (días)">
          <input type="number" min={0} step={1} className={inputCls} style={inputStyle} value={plazo} onChange={e => setPlazo(e.target.value)} />
        </Field>
      </div>
      <p className="text-[11px]" style={{ color: 'var(--text-muted)' }}>
        Plazo <b style={{ color: 'var(--text-primary)' }}>0 = al contado</b> (lo normal en un anticipo: el cliente ya pagó).
        Si lo dejas en blanco, la factura queda <b style={{ color: 'var(--text-primary)' }}>sin fecha de vencimiento</b>:
        nunca se marcará como vencida ni entrará en el KPI "Vencido".
      </p>
      {prevError && !preview && (
        <div className="rounded-xl border border-red-500/30 bg-red-500/10 p-3 text-xs text-red-500">{prevError}</div>
      )}
      {preview && (
        <div className="rounded-xl border p-3 text-xs space-y-1" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
          {preview.problemas.length > 0 && (
            <ul className="list-disc pl-4 text-red-500 space-y-0.5">{preview.problemas.map((p, i) => <li key={i}>{p}</li>)}</ul>
          )}
          <div className="flex gap-5" style={{ color: 'var(--text-muted)' }}>
            <span>Neto: <b style={{ color: 'var(--text-primary)' }}>{fmtClp(preview.totales.neto)}</b></span>
            <span>IVA 19%: <b style={{ color: 'var(--text-primary)' }}>{fmtClp(preview.totales.iva)}</b></span>
            <span>Total: <b className="text-brand-400">{fmtClp(preview.totales.bruto)}</b></span>
          </div>
        </div>
      )}
      <button onClick={submit} disabled={saving || !puedeEmitir} className="btn-primary w-full flex items-center justify-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed">
        {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <HandCoins className="w-4 h-4" />} {modoSii ? 'Emitir factura de anticipo al SII' : 'Registrar factura de anticipo emitida'}
      </button>
      <button type="button" onClick={() => setModoSii(m => !m)} className="w-full text-center text-[11px] underline underline-offset-2" style={{ color: 'var(--text-muted)' }}>
        {modoSii ? '¿La factura ya fue emitida a mano en el SII? Regístrala con su folio' : '← Volver a emitir al SII (folio automático)'}
      </button>
    </Modal>
  )
}

// ─── Modal: registrar cobranza ────────────────────────────────────────────────
function CobranzaModal({ factura, onClose, onDone }: { factura: Factura; onClose: () => void; onDone: () => void }) {
  const [monto, setMonto] = useState(String(Math.round(factura.saldo)))
  const [fecha, setFecha] = useState(hoyLocal())
  const [medio, setMedio] = useState('transferencia')
  const [banco, setBanco] = useState('')
  const [op, setOp] = useState('')
  const [saving, setSaving] = useState(false)
  const submit = async () => {
    if (!monto || Number(monto) <= 0) { toast.error('Monto inválido'); return }
    setSaving(true)
    try {
      await contabilidadAPI.registrarCobranza(factura.id, { monto: Number(monto), fecha, medio, banco: banco || undefined, numero_operacion: op || undefined })
      toast.success('Cobranza registrada'); onDone(); onClose()
    } catch (e: any) { toast.error(errMsg(e, 'Error al registrar cobranza')) } finally { setSaving(false) }
  }
  return (
    <Modal title={`Registrar cobranza · ${factura.numero_factura || '#' + factura.id}`} onClose={onClose}>
      <p className="text-xs" style={{ color: 'var(--text-muted)' }}>Saldo pendiente: <span className="font-bold text-amber-400">{fmtClp(factura.saldo)}</span></p>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Monto"><input type="number" className={inputCls} style={inputStyle} value={monto} onChange={e => setMonto(e.target.value)} /></Field>
        <Field label="Fecha"><input type="date" className={inputCls} style={inputStyle} value={fecha} onChange={e => setFecha(e.target.value)} /></Field>
        <Field label="Medio">
          <select className={inputCls} style={inputStyle} value={medio} onChange={e => setMedio(e.target.value)}>
            <option value="transferencia">Transferencia</option><option value="cheque">Cheque</option><option value="efectivo">Efectivo</option>
          </select>
        </Field>
        <Field label="Banco"><input className={inputCls} style={inputStyle} value={banco} onChange={e => setBanco(e.target.value)} /></Field>
      </div>
      <Field label="N° operación"><input className={inputCls} style={inputStyle} value={op} onChange={e => setOp(e.target.value)} /></Field>
      <button onClick={submit} disabled={saving} className="btn-primary w-full flex items-center justify-center gap-2">
        {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <CheckCircle2 className="w-4 h-4" />} Registrar pago
      </button>
    </Modal>
  )
}

// ─── Modal: factoring ─────────────────────────────────────────────────────────
function FactoringModal({ factura, onClose, onDone }: { factura: Factura; onClose: () => void; onDone: () => void }) {
  // Cupo financiable = bruto − pagos reales (no-factoring), igual que el backend
  const cobradoReal = factura.cobranzas.filter(c => !c.medio.startsWith('factoring')).reduce((s, c) => s + c.monto, 0)
  const cupo = Math.max(0, factura.monto_bruto - cobradoReal)
  const [empresa, setEmpresa] = useState(factura.factoring?.empresa_factoring || '')
  const [op, setOp] = useState(factura.factoring?.id_operacion || '')
  const [fecha, setFecha] = useState(factura.factoring?.fecha_operacion || hoyLocal())
  const [adelanto, setAdelanto] = useState(String(Math.round(factura.factoring?.monto_adelantado ?? Math.round(cupo * 0.9))))
  const [costo, setCosto] = useState(String(Math.round(factura.factoring?.costo_factoring || 0)))
  const [retencion, setRetencion] = useState(String(Math.round(factura.factoring?.retencion ?? (cupo - Math.round(cupo * 0.9)))))
  const [banco, setBanco] = useState(factura.factoring?.banco || '')
  const [saving, setSaving] = useState(false)
  const submit = async () => {
    setSaving(true)
    try {
      await contabilidadAPI.setFactoring(factura.id, {
        empresa_factoring: empresa || undefined, id_operacion: op || undefined, fecha_operacion: fecha,
        monto_adelantado: Number(adelanto) || 0, costo_factoring: Number(costo) || 0,
        retencion: retencion === '' ? undefined : Number(retencion), banco: banco || undefined,
      })
      toast.success('Factoring registrado'); onDone(); onClose()
    } catch (e: any) { toast.error(errMsg(e, 'Error en factoring')) } finally { setSaving(false) }
  }
  return (
    <Modal title={`Factoring · ${factura.numero_factura || '#' + factura.id}`} onClose={onClose}>
      <p className="text-xs" style={{ color: 'var(--text-muted)' }}>Bruto: <span className="font-bold" style={{ color: 'var(--text-primary)' }}>{fmtClp(factura.monto_bruto)}</span> · Financiable (cupo): <span className="font-bold text-purple-400">{fmtClp(cupo)}</span></p>
      <Field label="Empresa de factoring"><input className={inputCls} style={inputStyle} value={empresa} onChange={e => setEmpresa(e.target.value)} placeholder="Ej. Penta Financiero" /></Field>
      <div className="grid grid-cols-2 gap-3">
        <Field label="ID operación"><input className={inputCls} style={inputStyle} value={op} onChange={e => setOp(e.target.value)} /></Field>
        <Field label="Fecha"><input type="date" className={inputCls} style={inputStyle} value={fecha} onChange={e => setFecha(e.target.value)} /></Field>
        <Field label="Monto adelantado"><input type="number" className={inputCls} style={inputStyle} value={adelanto}
          onChange={e => { setAdelanto(e.target.value); setRetencion(String(Math.max(0, Math.round(cupo - (Number(e.target.value) || 0))))) }} /></Field>
        <Field label="Costo factoring"><input type="number" className={inputCls} style={inputStyle} value={costo} onChange={e => setCosto(e.target.value)} /></Field>
        <Field label="Retención (= cupo − adelanto)"><input type="number" className={inputCls} style={inputStyle} value={retencion} onChange={e => setRetencion(e.target.value)} /></Field>
        <Field label="Banco"><input className={inputCls} style={inputStyle} value={banco} onChange={e => setBanco(e.target.value)} /></Field>
      </div>
      <button onClick={submit} disabled={saving} className="btn-primary w-full flex items-center justify-center gap-2">
        {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Landmark className="w-4 h-4" />} Guardar factoring
      </button>
    </Modal>
  )
}

// ─── Fila de factura (expandible) ─────────────────────────────────────────────
function FacturaRow({ f, onChanged, onCobrar, onFactoring, onSii }: { f: Factura; onChanged: () => void; onCobrar: (f: Factura) => void; onFactoring: (f: Factura) => void; onSii: (f: Factura) => void }) {
  const [open, setOpen] = useState(false)
  const pago = PAGO[f.estado_pago] ?? { cls: 'bg-gray-500/10 text-gray-400', label: f.estado_pago }
  const pct = Math.min(100, f.monto_bruto > 0 ? Math.round((f.monto_pagado / f.monto_bruto) * 100) : 0)
  // Estado de la emisión electrónica (si esta factura tiene DTE 33 asociado)
  const siiEmitida = f.dte_estado === 'emitido'
  const siiFallida = !!f.dte_estado && !siiEmitida && !!f.dte_puede_reintentar
  const siiEnProceso = !!f.dte_estado && !siiEmitida && !siiFallida
  const liquidar = async () => {
    try { await contabilidadAPI.liquidarFactoring(f.id); toast.success('Factoring liquidado'); onChanged() }
    catch (e: any) { toast.error(errMsg(e, 'Error')) }
  }
  const delCobranza = async (id: number) => {
    if (!confirm('¿Eliminar esta cobranza?')) return
    try { await contabilidadAPI.eliminarCobranza(f.id, id); toast.success('Cobranza eliminada'); onChanged() }
    catch (e: any) { toast.error(errMsg(e, 'Error')) }
  }
  const eliminar = async () => {
    // Guards anticipados (el backend igual los valida): un DTE emitido al SII es un
    // documento tributario real — no se borra, se anula en Wasabil con nota de crédito.
    if (siiEmitida) { toast.error('Esta factura fue emitida al SII: anúlala primero en Wasabil (nota de crédito)'); return }
    if (siiEnProceso) { toast.error('Hay una emisión SII en curso para esta factura: espera a que termine antes de eliminar'); return }
    if (!confirm('¿Eliminar esta factura? Solo se puede si no tiene pagos ni factoring (revierte las cobranzas primero).')) return
    try { await contabilidadAPI.eliminarFactura(f.id); toast.success('Factura eliminada'); onChanged() }
    catch (e: any) { toast.error(errMsg(e, 'Error')) }
  }
  return (
    <>
      {/* Fila expandible también por teclado: dentro viven acciones que sin esto
          quedaban inalcanzables (PDF SII, Reintentar SII, eliminar). */}
      <tr className="hover:bg-[var(--surface-200)] transition-colors cursor-pointer focus:outline-none focus:ring-2 focus:ring-brand-500/40"
        role="button" tabIndex={0} aria-expanded={open}
        onClick={() => setOpen(o => !o)}
        onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setOpen(o => !o) } }}>
        <td className="px-4 py-3 font-mono font-semibold text-brand-400 whitespace-nowrap">
          <span className="inline-flex items-center gap-1">{open ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}{f.numero_factura || `#${f.id}`}</span>
          {f.es_anticipo && (
            <span className="ml-1.5 inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-full text-[10px] font-sans font-medium bg-emerald-500/10 text-emerald-500 border border-emerald-500/20">
              <HandCoins className="w-2.5 h-2.5" /> Anticipo
            </span>
          )}
          {siiEmitida && (
            <span className="ml-1.5 inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-full text-[10px] font-sans font-medium bg-blue-500/10 text-blue-500 border border-blue-500/20"
              title={`Emitida electrónicamente al SII (folio ${f.dte_folio || '—'})`}>
              <CheckCircle2 className="w-2.5 h-2.5" /> SII
            </span>
          )}
          {siiEnProceso && (
            <span className="ml-1.5 inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-full text-[10px] font-sans font-medium bg-amber-500/10 text-amber-500 border border-amber-500/20"
              title="Emisión al SII en proceso — abre la fila para ver el estado">
              <Loader2 className="w-2.5 h-2.5 animate-spin" /> SII en proceso
            </span>
          )}
          {siiFallida && (
            <span className="ml-1.5 inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-full text-[10px] font-sans font-medium bg-red-500/10 text-red-500 border border-red-500/20"
              title={f.dte_error || 'La emisión al SII falló — abre la fila para reintentar'}>
              <AlertCircle className="w-2.5 h-2.5" /> SII fallida
            </span>
          )}
        </td>
        <td className="px-4 py-3 font-medium max-w-[180px] truncate" style={{ color: 'var(--text-primary)' }}>{f.cliente}</td>
        <td className="px-4 py-3 font-mono text-xs text-brand-400">{f.numero_oc || '—'}</td>
        <td className="px-4 py-3 whitespace-nowrap" style={{ color: 'var(--text-muted)' }}>{fmtDate(f.fecha_emision)}</td>
        <td className="px-4 py-3 font-semibold whitespace-nowrap" style={{ color: 'var(--text-primary)' }}>{fmtClp(f.monto_bruto)}</td>
        <td className="px-4 py-3">
          <div className="flex items-center gap-2">
            <div className="w-16 h-1.5 rounded-full" style={{ backgroundColor: 'var(--surface-300)' }}><div className="h-full rounded-full bg-emerald-500" style={{ width: `${pct}%` }} /></div>
            <span className="text-xs text-emerald-500 font-medium">{pct}%</span>
          </div>
        </td>
        <td className="px-4 py-3 whitespace-nowrap">
          <span className={f.semaforo === 'vencida' ? 'text-red-400 font-medium' : ''} style={f.semaforo !== 'vencida' ? { color: 'var(--text-muted)' } : {}}>
            {fmtDate(f.fecha_vencimiento)}
            {f.dias_vencimiento != null && f.saldo > 0 && <span className="ml-1 text-xs">({f.dias_vencimiento < 0 ? `${Math.abs(f.dias_vencimiento)}d venc.` : `${f.dias_vencimiento}d`})</span>}
          </span>
        </td>
        <td className="px-4 py-3"><span className={`inline-flex items-center px-2.5 py-1 rounded-full text-xs font-medium border ${pago.cls}`}>{pago.label}</span></td>
      </tr>
      {open && (
        <tr>
          <td colSpan={8} className="px-4 pb-4" style={{ backgroundColor: 'var(--surface-100)' }}>
            <div className="grid md:grid-cols-3 gap-4 pt-3">
              {/* Ítems */}
              <div className="md:col-span-2">
                <p className="text-[11px] font-semibold uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>Ítems facturados</p>
                <div className="rounded-lg border overflow-hidden" style={{ borderColor: 'var(--border)' }}>
                  <table className="w-full text-xs">
                    <tbody>
                      {f.items.map(it => (
                        <tr key={it.id} style={{ borderBottom: '1px solid var(--border)' }}>
                          <td className="px-2 py-1.5 font-mono" style={{ color: 'var(--text-primary)' }}>{it.numero_parte}</td>
                          <td className="px-2 py-1.5 max-w-[160px] truncate" style={{ color: 'var(--text-muted)' }}>{it.descripcion}</td>
                          <td className="px-2 py-1.5 text-right" style={{ color: 'var(--text-muted)' }}>{it.cantidad} × {fmtClp(it.precio_unit_neto)}</td>
                          <td className="px-2 py-1.5 text-right font-semibold" style={{ color: 'var(--text-primary)' }}>{fmtClp(it.total_neto)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="flex gap-5 mt-2 text-xs" style={{ color: 'var(--text-muted)' }}>
                  <span>Neto: <b style={{ color: 'var(--text-primary)' }}>{fmtClp(f.monto_neto)}</b></span>
                  <span>IVA: <b style={{ color: 'var(--text-primary)' }}>{fmtClp(f.iva)}</b></span>
                  <span>Total: <b className="text-brand-400">{fmtClp(f.monto_bruto)}</b></span>
                  <span>Saldo: <b className="text-amber-400">{fmtClp(f.saldo)}</b></span>
                </div>
                {(f.numero_guia || f.numero_expedicion || f.condicion_pago || f.observaciones || f.guia_firmada_archivo) && (
                  <div className="flex flex-wrap items-center gap-x-5 gap-y-1 mt-1 text-xs" style={{ color: 'var(--text-faint)' }}>
                    {f.numero_guia && <span>Guía: <b style={{ color: 'var(--text-muted)' }}>{f.numero_guia}</b></span>}
                    {f.numero_expedicion && <span>Exp: <b style={{ color: 'var(--text-muted)' }}>{f.numero_expedicion}</b></span>}
                    {f.condicion_pago && <span>Condición: <b style={{ color: 'var(--text-muted)' }}>{f.condicion_pago}</b></span>}
                    {f.observaciones && <span>Obs.: <b style={{ color: 'var(--text-muted)' }}>{f.observaciones}</b></span>}
                    {f.guia_firmada_archivo && (
                      <button
                        onClick={(e) => { e.stopPropagation(); abrirDocumento(f.guia_firmada_archivo!) }}
                        className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-emerald-500/10 text-emerald-600 dark:text-emerald-400 hover:bg-emerald-500/20"
                      >
                        <FileText className="w-3 h-3" /> Ver guía firmada
                      </button>
                    )}
                  </div>
                )}
                {/* Cobranzas */}
                {f.cobranzas.length > 0 && (
                  <div className="mt-3">
                    <p className="text-[11px] font-semibold uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>Cobranzas</p>
                    {f.cobranzas.map(c => {
                      const esFact = c.medio.startsWith('factoring')
                      return (
                        <div key={c.id} className="flex items-center justify-between text-xs py-1 border-b" style={{ borderColor: 'var(--border)' }}>
                          <span style={{ color: 'var(--text-muted)' }}>{fmtDate(c.fecha)} · {c.medio.replace(/_/g, ' ')}{c.banco ? ` · ${c.banco}` : ''}{c.numero_operacion ? ` · ${c.numero_operacion}` : ''}</span>
                          <span className="flex items-center gap-2">
                            <span className={`font-semibold ${esFact ? 'text-purple-400' : 'text-emerald-500'}`}>{fmtClp(c.monto)}</span>
                            {!esFact && (
                              <button onClick={(e) => { e.stopPropagation(); delCobranza(c.id) }} className="text-red-400 hover:bg-red-500/10 rounded p-0.5" title="Eliminar cobranza"><Trash2 className="w-3 h-3" /></button>
                            )}
                          </span>
                        </div>
                      )
                    })}
                  </div>
                )}
              </div>
              {/* Acciones + factoring */}
              <div className="space-y-2">
                {siiEmitida && f.dte_pdf_url && (
                  <button onClick={(e) => { e.stopPropagation(); window.open(f.dte_pdf_url!, '_blank', 'noopener,noreferrer') }}
                    className="btn-secondary w-full flex items-center justify-center gap-2 text-xs">
                    <FileText className="w-3.5 h-3.5" /> PDF SII{f.dte_folio ? ` · folio ${f.dte_folio}` : ''}
                  </button>
                )}
                {(siiFallida || siiEnProceso) && (
                  <button onClick={(e) => { e.stopPropagation(); onSii(f) }}
                    className={`w-full flex items-center justify-center gap-2 text-xs rounded-lg py-1.5 border ${siiFallida ? 'text-red-500 border-red-500/30 hover:bg-red-500/10' : 'text-amber-500 border-amber-500/30 hover:bg-amber-500/10'}`}>
                    <RefreshCw className="w-3.5 h-3.5" /> {siiFallida ? 'Reintentar emisión SII' : 'Ver emisión SII en curso'}
                  </button>
                )}
                {/* !es_anticipo: una factura de ANTICIPO se salda SOLO con el adelanto que
                    aprueba Tesorería (el backend responde 409). Si se ofrece el botón, el
                    administrativo la salda a mano con la transferencia del cliente y ese
                    MISMO depósito se cuenta DOS VECES: el adelanto ligado se libera y su
                    plata cae completa en la factura del despacho real. La UI no debe
                    ofrecer lo que el backend rechaza. Paridad con MonzaParts. */}
                {f.saldo > 0 && !f.es_anticipo && (
                  <button onClick={(e) => { e.stopPropagation(); onCobrar(f) }} className="btn-secondary w-full flex items-center justify-center gap-2 text-xs">
                    <CreditCard className="w-3.5 h-3.5" /> Registrar cobranza
                  </button>
                )}
                <button onClick={(e) => { e.stopPropagation(); onFactoring(f) }}
                  disabled={f.factoring?.estado === 'liquidada'}
                  className="btn-secondary w-full flex items-center justify-center gap-2 text-xs disabled:opacity-40 disabled:cursor-not-allowed">
                  <Landmark className="w-3.5 h-3.5" /> {f.factoring ? (f.factoring.estado === 'liquidada' ? 'Factoring liquidado' : 'Editar factoring') : 'Factorizar'}
                </button>
                {f.factoring && (
                  <div className="rounded-lg border p-2.5 text-xs space-y-0.5" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
                    <p className="font-semibold text-purple-400">{f.factoring.empresa_factoring || 'Factoring'} <span className="font-normal" style={{ color: 'var(--text-faint)' }}>({f.factoring.estado})</span></p>
                    <p style={{ color: 'var(--text-muted)' }}>Adelanto: <b style={{ color: 'var(--text-primary)' }}>{fmtClp(f.factoring.monto_adelantado)}</b></p>
                    <p style={{ color: 'var(--text-muted)' }}>Retención: <b style={{ color: 'var(--text-primary)' }}>{fmtClp(f.factoring.retencion)}</b></p>
                    <p style={{ color: 'var(--text-muted)' }}>Costo: <b style={{ color: 'var(--text-primary)' }}>{fmtClp(f.factoring.costo_factoring)}</b></p>
                    {f.factoring.estado === 'vigente' && (
                      <button onClick={(e) => { e.stopPropagation(); liquidar() }} className="btn-secondary w-full mt-1 text-xs">Liquidar factoring</button>
                    )}
                  </div>
                )}
                <button onClick={(e) => { e.stopPropagation(); eliminar() }} className="w-full flex items-center justify-center gap-1.5 text-xs text-red-400 hover:bg-red-500/10 rounded-lg py-1.5">
                  <Trash2 className="w-3.5 h-3.5" /> Eliminar
                </button>
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  )
}

// ─── Página ───────────────────────────────────────────────────────────────────
export default function FacturasPage() {
  const qc = useQueryClient()
  const [facturas, setFacturas] = useState<Factura[]>([])
  const [aging, setAging] = useState<Aging | null>(null)
  const [kpis, setKpis] = useState<Kpis | null>(null)
  const [loading, setLoading] = useState(true)
  const [q, setQ] = useState('')
  const [estado, setEstado] = useState('')
  const [error, setError] = useState('')
  const [modal, setModal] = useState<{ type: 'crear' | 'anticipo' | 'cobranza' | 'factoring' | 'sii'; factura?: Factura } | null>(null)
  const [siiBusy, setSiiBusy] = useState(false)   // envío en vuelo: el modal no se cierra

  const load = useCallback(async (search?: string, est?: string) => {
    setLoading(true); setError('')
    try {
      const [fRes, kRes] = await Promise.all([
        contabilidadAPI.listFacturas(est, search),
        contabilidadAPI.kpis(),
      ])
      setFacturas(fRes.data.facturas)
      setAging(fRes.data.antiguedad)
      setKpis(kRes.data)
    } catch { setError('No se pudieron cargar las facturas.') } finally { setLoading(false) }
  }, [])

  useEffect(() => { load(q || undefined, estado || undefined) }, [estado])
  // Invalidación CENTRALIZADA del selector de OC: TODOS los onDone/onChanged de la
  // página pasan por reload (emitir/eliminar factura, cobranzas, factoring, SII),
  // así que este ÚNICO punto mantiene fresca la caché de /ventas/opciones — tres
  // invalidaciones dispersas en los modales se desincronizan a la primera refactorización.
  const reload = () => {
    qc.invalidateQueries({ queryKey: ['contabilidad', 'ventas-opciones'] })
    return load(q || undefined, estado || undefined)
  }
  const handleSearch = (v: string) => { setQ(v); if (v.length === 0 || v.length >= 2) load(v || undefined, estado || undefined) }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-2xl font-bold" style={{ color: 'var(--text-primary)' }}>Facturas y Cobranzas</h1>
          <p className="text-sm mt-1" style={{ color: 'var(--text-muted)' }}>Cuentas por cobrar · antigüedad de cartera · factoring por factura</p>
        </div>
        <div className="flex items-center gap-2 self-start">
          <button onClick={() => setModal({ type: 'crear' })} className="btn-primary flex items-center gap-2"><Plus className="w-4 h-4" /> Emitir factura</button>
          <button onClick={() => setModal({ type: 'anticipo' })} className="btn-secondary flex items-center gap-2"><HandCoins className="w-4 h-4" /> Factura de anticipo</button>
          <button onClick={reload} className="btn-secondary flex items-center gap-2"><RefreshCw className="w-4 h-4" /></button>
        </div>
      </div>

      {/* KPIs */}
      {kpis && (
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
          {[
            { icon: DollarSign, label: 'Facturado', value: fmtClp(kpis.facturado_clp), color: 'text-brand-400' },
            { icon: CheckCircle2, label: 'Cobrado cliente', value: fmtClp(kpis.cobrado_cliente_clp ?? kpis.cobrado_clp), color: 'text-emerald-500' },
            // Plata que puso el FACTOR, no el cliente: el backend ya la devolvía y la
            // pantalla la escondía, así que "Cobrado" no cuadraba con la cartera.
            { icon: HandCoins, label: 'Anticipo factoring', value: fmtClp(kpis.anticipo_factoring_clp ?? 0), color: 'text-purple-400' },
            { icon: CreditCard, label: 'Por cobrar', value: fmtClp(kpis.por_cobrar_clp), color: 'text-amber-400' },
            { icon: AlertCircle, label: 'Vencido', value: fmtClp(kpis.vencido_clp), color: 'text-red-400' },
            { icon: Landmark, label: 'En factoring', value: fmtClp(kpis.en_factoring_clp), color: 'text-purple-400' },
          ].map(s => (
            <div key={s.label} className="rounded-2xl p-3 sm:p-4 border" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
              <div className="w-8 h-8 rounded-xl flex items-center justify-center mb-2" style={{ backgroundColor: 'var(--surface-300)' }}><s.icon className={`w-4 h-4 ${s.color}`} /></div>
              <p className="text-[10px] uppercase tracking-widest leading-tight" style={{ color: 'var(--text-faint)' }}>{s.label}</p>
              <p className={`text-lg sm:text-xl font-bold mt-0.5 ${s.color}`}>{s.value}</p>
            </div>
          ))}
        </div>
      )}

      {/* Antigüedad */}
      {aging && (
        <div className="rounded-2xl border p-4" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
          <h3 className="font-semibold text-sm mb-3" style={{ color: 'var(--text-primary)' }}>Antigüedad de Cartera (saldo por cobrar)</h3>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            {[
              { rango: '0–30 días', monto: aging['0_30'], color: 'text-emerald-500' },
              { rango: '31–60 días', monto: aging['31_60'], color: 'text-amber-400' },
              { rango: '61–90 días', monto: aging['61_90'], color: 'text-orange-400' },
              { rango: '+90 días', monto: aging['91_mas'], color: 'text-red-400' },
            ].map(r => (
              <div key={r.rango} className="text-center p-3 rounded-xl" style={{ backgroundColor: 'var(--surface-200)' }}>
                <p className="text-xs" style={{ color: 'var(--text-faint)' }}>{r.rango}</p>
                <p className={`text-base font-bold mt-1 ${r.color}`}>{fmtClp(r.monto)}</p>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Filtros */}
      <div className="flex flex-col sm:flex-row gap-3">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4" style={{ color: 'var(--text-faint)' }} />
          <input className="w-full pl-9 pr-4 py-2.5 rounded-xl border text-sm focus:outline-none focus:ring-2 focus:ring-brand-500/40"
            style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)', color: 'var(--text-primary)' }}
            placeholder="Buscar por folio, cliente u OC…" value={q} onChange={e => handleSearch(e.target.value)} />
        </div>
        <div className="flex items-center gap-1.5 flex-wrap">
          {ESTADOS.map(e => (
            <button key={e} onClick={() => setEstado(e)}
              className={`px-3 py-1.5 rounded-full text-xs font-medium border transition-all ${estado === e ? 'border-brand-500 bg-brand-500/10 text-brand-400' : 'border-transparent'}`}
              style={estado !== e ? { backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)', color: 'var(--text-muted)' } : {}}>
              {ESTADO_LABEL[e]}
            </button>
          ))}
        </div>
      </div>

      {error && <div className="rounded-xl border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-400">{error}</div>}
      {loading && <div className="flex items-center justify-center py-16"><Loader2 className="w-7 h-7 animate-spin text-brand-400" /></div>}
      {!loading && !error && facturas.length === 0 && (
        <div className="rounded-2xl border py-16 text-center" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
          <Receipt className="w-10 h-10 mx-auto mb-3 opacity-20" style={{ color: 'var(--text-muted)' }} />
          <p className="text-sm font-medium" style={{ color: 'var(--text-muted)' }}>No hay facturas registradas</p>
          <p className="text-xs mt-1" style={{ color: 'var(--text-faint)' }}>Usa "Emitir factura" para emitir una desde un despacho.</p>
        </div>
      )}

      {/* Tabla */}
      {!loading && facturas.length > 0 && (
        <div className="rounded-2xl border overflow-hidden" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
                  {['Folio', 'Cliente', 'OC', 'Emisión', 'Total', 'Cobrado', 'Vencimiento', 'Estado'].map(h => (
                    <th key={h} className="text-left px-4 py-3 text-xs font-semibold uppercase tracking-wider whitespace-nowrap" style={{ color: 'var(--text-faint)' }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {facturas.map(f => (
                  <FacturaRow key={f.id} f={f} onChanged={reload}
                    onCobrar={(fa) => setModal({ type: 'cobranza', factura: fa })}
                    onFactoring={(fa) => setModal({ type: 'factoring', factura: fa })}
                    onSii={(fa) => setModal({ type: 'sii', factura: fa })} />
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Modales */}
      {modal?.type === 'crear' && <CrearFacturaModal onClose={() => setModal(null)} onDone={reload} />}
      {modal?.type === 'anticipo' && <AnticipoFacturaModal onClose={() => setModal(null)} onDone={reload} />}
      {modal?.type === 'cobranza' && modal.factura && <CobranzaModal factura={modal.factura} onClose={() => setModal(null)} onDone={reload} />}
      {modal?.type === 'factoring' && modal.factura && <FactoringModal factura={modal.factura} onClose={() => setModal(null)} onDone={reload} />}
      {modal?.type === 'sii' && modal.factura && (
        <Modal title={`Emisión SII · factura ${modal.factura.numero_factura || '#' + modal.factura.id}`}
          onClose={siiBusy ? () => {} : () => setModal(null)}>
          <EmisionFacturaSII facturaId={modal.factura.id} onDone={reload}
            onCerrar={() => setModal(null)} onBusy={setSiiBusy} />
        </Modal>
      )}
    </div>
  )
}
