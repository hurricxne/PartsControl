// Página "Facturas y Cobranzas" (cuentas por cobrar): lista facturas + antigüedad de cartera,
// y concentra las acciones — EMITIR factura (desde una guía firmada), registrar cobranzas y
// gestionar factoring. Consume contabilidadAPI (/facturas, /kpis, despachos-facturables…).
import { useState, useEffect, useCallback } from 'react'
import {
  Receipt, Plus, Search, AlertCircle, CheckCircle2, DollarSign,
  Loader2, RefreshCw, ChevronDown, ChevronUp, CreditCard, Landmark, X, Trash2, FileText,
  HandCoins,
} from 'lucide-react'
import toast from 'react-hot-toast'
import { contabilidadAPI, abrirDocumento } from '../services/api'
import { fmtClp, fmtDate, hoyLocal } from '../utils/format'

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
}
interface Kpis { facturado_clp: number; cobrado_clp: number; cobrado_cliente_clp?: number; por_cobrar_clp: number; vencido_clp: number; en_factoring_clp: number }
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
function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (<label className="block"><span className="block text-[11px] font-semibold uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>{label}</span>{children}</label>)
}

// ─── Modal: emitir factura (desde un despacho/guía de una OC) ─────────────────
// Antes de emitir se PREVISUALIZA lo que se va a facturar (receptor + líneas + neto/IVA/
// total), se exige el RUT del cliente (campo por llenar si la venta no lo trae) y el folio.
interface OcOpt { oc_cliente_id: number; numero_oc: string | null; cliente: string; cond_pago: string | null }
interface PreviewLinea { numero_parte: string | null; descripcion: string | null; cantidad: number; precio_unit_neto: number; total_neto: number }
interface PreviewData {
  puede_emitir: boolean; problemas: string[]; advertencias?: string[]
  receptor: { rut: string; rut_en_venta: boolean; razon_social: string; giro?: string | null; direccion?: string | null }
  lineas: PreviewLinea[]; totales: { neto: number; iva: number; bruto: number }
  precio_de_guia?: boolean
}

function CrearFacturaModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [ocs, setOcs] = useState<OcOpt[]>([])
  const [ocId, setOcId] = useState<number | ''>('')
  const [despachos, setDespachos] = useState<{ id: number; numero_despacho: string; numero_guia: string | null; numero_expedicion: string | null; guia_firmada_archivo: string | null; items_count: number }[]>([])
  const [despachoId, setDespachoId] = useState<number | ''>('')
  const [folio, setFolio] = useState('')
  const [tipo, setTipo] = useState('factura')
  const [fecha, setFecha] = useState(hoyLocal())
  const [plazo, setPlazo] = useState('30')
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

  useEffect(() => {
    contabilidadAPI.listVentas().then(({ data }) =>
      setOcs(data.map((v: any) => ({ oc_cliente_id: v.oc_cliente_id, numero_oc: v.numero_oc, cliente: v.cliente, cond_pago: v.cond_pago })))
    ).catch(() => {})
  }, [])

  const condPagoOc = ocs.find(o => o.oc_cliente_id === Number(ocId))?.cond_pago || ''

  useEffect(() => {
    if (!ocId) { setDespachos([]); return }
    let vivo = true  // ignora respuestas atrasadas si el usuario ya cambió de OC
    contabilidadAPI.despachosFacturables(Number(ocId))
      .then(({ data }) => { if (vivo) setDespachos(data || []) })
      .catch(() => { if (vivo) setDespachos([]) })
    // Precargar el plazo desde la condición de pago pactada en la OC ("30 días", "contado"…)
    const cp = (condPagoOc || '').toLowerCase()
    const m = cp.match(/(\d+)/)
    if (m) setPlazo(m[1])
    else if (cp.includes('contado') || cp.includes('contra entrega')) setPlazo('0')
    else setPlazo('30')  // cond_pago no reconocida: default 30 (no hereda el de otra OC)
    return () => { vivo = false }
  }, [ocId])

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
  const folioFaltante = tipo === 'factura' && !folio.trim()
  const bloqueadoDatos = (preview?.problemas?.length ?? 0) > 0
  const puedeEmitir = !!preview && preview.lineas.length > 0 && !rutFaltante && !folioFaltante && !bloqueadoDatos && !loadingPrev

  const submit = async () => {
    if (!ocId || !despachoId) { toast.error('Selecciona OC y despacho'); return }
    if (folioFaltante) { toast.error('Ingresa el folio SII de la factura'); return }
    if (rutFaltante) { toast.error('Ingresa el RUT del cliente'); return }
    setSaving(true)
    try {
      await contabilidadAPI.crearFactura({
        oc_cliente_id: Number(ocId), despacho_id: Number(despachoId),
        numero_factura: folio || undefined, tipo_doc: tipo,
        fecha_emision: fecha, plazo_dias: plazo ? Number(plazo) : undefined,
        // Condición real pactada en la OC; si no hay, se deriva del plazo
        condicion_pago: condPagoOc || (plazo ? `${plazo} días` : undefined),
        rut_cliente: rut || undefined,
        razon_social_cliente: razonSocial.trim() || undefined,
      })
      toast.success('Factura emitida')
      onDone(); onClose()
    } catch (e: any) {
      toast.error(errMsg(e, 'No se pudo emitir la factura'))
    } finally { setSaving(false) }
  }

  return (
    <Modal title="Emitir factura" onClose={onClose} wide>
      <Field label="Orden de compra (venta)">
        {/* setRutDebounced('') SÍNCRONO: sin esto, el RUT del cliente de la OC anterior
            queda “en vuelo” hasta 500ms y podía filtrarse al preview de la OC nueva */}
        <select className={inputCls} style={inputStyle} value={ocId} onChange={e => { setOcId(e.target.value ? Number(e.target.value) : ''); setDespachoId(''); setPreview(null); setRut(''); setRutDebounced(''); setRazonSocial(''); setRazonDebounced('') }}>
          <option value="">Selecciona OC…</option>
          {ocs.map(o => <option key={o.oc_cliente_id} value={o.oc_cliente_id}>OC {o.numero_oc || o.oc_cliente_id} — {o.cliente}</option>)}
        </select>
      </Field>
      <Field label="Despacho / guía a facturar">
        <select className={inputCls} style={inputStyle} value={despachoId} onChange={e => { setDespachoId(e.target.value ? Number(e.target.value) : ''); setPreview(null) }} disabled={!ocId}>
          <option value="">{ocId ? (despachos.length ? 'Selecciona despacho…' : 'Sin despachos cerrados para facturar') : 'Elige una OC primero'}</option>
          {despachos.map(d => <option key={d.id} value={d.id}>{d.numero_despacho}{d.numero_guia ? ` · Guía ${d.numero_guia}` : ''} ({d.items_count} ítems)</option>)}
        </select>
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
      </Field>

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
        <Field label={`N° Factura (folio SII)${tipo === 'factura' ? ' *' : ''}`}><input className={inputCls} style={inputStyle} value={folio} onChange={e => setFolio(e.target.value)} placeholder="Ej. 35" /></Field>
        <Field label="Tipo">
          <select className={inputCls} style={inputStyle} value={tipo} onChange={e => setTipo(e.target.value)}><option value="factura">Factura</option><option value="boleta">Boleta</option></select>
        </Field>
        <Field label="Fecha emisión"><input type="date" className={inputCls} style={inputStyle} value={fecha} onChange={e => setFecha(e.target.value)} /></Field>
        <Field label="Plazo (días)"><input type="number" min={0} step={1} className={inputCls} style={inputStyle} value={plazo} onChange={e => setPlazo(e.target.value)} /></Field>
      </div>
      <button onClick={submit} disabled={saving || !puedeEmitir} className="btn-primary w-full flex items-center justify-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed">
        {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Receipt className="w-4 h-4" />} Emitir factura
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

function AnticipoFacturaModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [ocs, setOcs] = useState<OcOpt[]>([])
  const [ocId, setOcId] = useState<number | ''>('')
  const [adelantos, setAdelantos] = useState<AdelantoVenta[]>([])
  const [adelSel, setAdelSel] = useState<Set<number>>(new Set())
  const [montoNeto, setMontoNeto] = useState('')
  const [folio, setFolio] = useState('')
  const [fecha, setFecha] = useState(hoyLocal())
  const [rut, setRut] = useState('')
  const [rutDebounced, setRutDebounced] = useState('')  // RUT asentado que dispara el preview
  // Razón social: campo por llenar (solo aparece si la venta no la trae); al emitir
  // se guarda en la venta, igual que el RUT.
  const [razonSocial, setRazonSocial] = useState('')
  const [razonDebounced, setRazonDebounced] = useState('')
  const [preview, setPreview] = useState<PreviewData | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    contabilidadAPI.listVentas().then(({ data }) =>
      setOcs(data.map((v: any) => ({ oc_cliente_id: v.oc_cliente_id, numero_oc: v.numero_oc, cliente: v.cliente, cond_pago: v.cond_pago })))
    ).catch(() => {})
  }, [])

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
    contabilidadAPI.adelantosDeVenta(Number(ocId)).then(({ data }) => {
      const cand = (data || []).filter((a: AdelantoVenta) =>
        a.estado !== 'anulado' && !a.factura_anticipo_id && (a.monto_aplicado || 0) <= 1)
      setAdelantos(cand)
      if (cand.length === 1) {
        setAdelSel(new Set([cand[0].id]))
        const bruto = cand[0].estado === 'aprobado' ? cand[0].monto : cand[0].monto_esperado
        if (bruto > 0 && !montoNeto) setMontoNeto(String(Math.round(bruto / 1.19)))
      }
    }).catch(() => setAdelantos([]))
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
    }).then(({ data }) => {
      if (!vivo) return
      setPreview(data)
      if (!rut && data.receptor?.rut) setRut(prev => prev || data.receptor.rut)
    }).catch(() => { if (vivo) setPreview(null) })
    return () => { vivo = false }
  }, [ocId, montoNeto, adelSel, rutDebounced, razonDebounced])

  const toggleAdel = (id: number) => setAdelSel(prev => {
    const s = new Set(prev); s.has(id) ? s.delete(id) : s.add(id); return s
  })
  const puedeEmitir = !!preview && preview.puede_emitir && !!folio.trim() && !!rut.trim()

  const submit = async () => {
    if (!ocId) { toast.error('Selecciona la OC'); return }
    if (!montoNeto || Number(montoNeto) <= 0) { toast.error('Indica el monto neto del anticipo'); return }
    if (!folio.trim()) { toast.error('Ingresa el folio SII de la factura'); return }
    if (!rut.trim()) { toast.error('Ingresa el RUT del cliente'); return }
    setSaving(true)
    try {
      await contabilidadAPI.crearFactura({
        oc_cliente_id: Number(ocId), es_anticipo: true,
        monto_neto_anticipo: Number(montoNeto), adelanto_ids: [...adelSel],
        numero_factura: folio, tipo_doc: 'factura', fecha_emision: fecha,
        rut_cliente: rut || undefined,
        razon_social_cliente: razonSocial.trim() || undefined,
      })
      toast.success('Factura de anticipo emitida — al facturar el despacho real se descuenta sola')
      onDone(); onClose()
    } catch (e: any) { toast.error(errMsg(e, 'No se pudo emitir la factura de anticipo')) } finally { setSaving(false) }
  }

  return (
    <Modal title="Factura de anticipo (sin guía de despacho)" onClose={onClose} wide>
      <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
        Respalda ante el SII un <b>adelanto del cliente</b> antes del despacho. Cuando después factures la guía
        firmada, el sistema le <b>descuenta este anticipo automáticamente</b> para no cobrar dos veces.
      </p>
      <Field label="Orden de compra (venta)">
        {/* setRutDebounced('') SÍNCRONO: sin esto, el RUT de la OC anterior podía
            quedar "en vuelo" 500ms y filtrarse al preview de la OC nueva */}
        <select className={inputCls} style={inputStyle} value={ocId} onChange={e => { setOcId(e.target.value ? Number(e.target.value) : ''); setRut(''); setRutDebounced(''); setRazonSocial(''); setRazonDebounced(''); setMontoNeto('') }}>
          <option value="">Selecciona OC…</option>
          {ocs.map(o => <option key={o.oc_cliente_id} value={o.oc_cliente_id}>OC {o.numero_oc || o.oc_cliente_id} — {o.cliente}</option>)}
        </select>
      </Field>
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
      <div className="grid grid-cols-2 gap-3">
        <Field label="N° Factura (folio SII) *"><input className={inputCls} style={inputStyle} value={folio} onChange={e => setFolio(e.target.value)} placeholder="Ej. 42" /></Field>
        <Field label="Fecha emisión"><input type="date" className={inputCls} style={inputStyle} value={fecha} onChange={e => setFecha(e.target.value)} /></Field>
      </div>
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
        {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <HandCoins className="w-4 h-4" />} Emitir factura de anticipo
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
function FacturaRow({ f, onChanged, onCobrar, onFactoring }: { f: Factura; onChanged: () => void; onCobrar: (f: Factura) => void; onFactoring: (f: Factura) => void }) {
  const [open, setOpen] = useState(false)
  const pago = PAGO[f.estado_pago] ?? { cls: 'bg-gray-500/10 text-gray-400', label: f.estado_pago }
  const pct = Math.min(100, f.monto_bruto > 0 ? Math.round((f.monto_pagado / f.monto_bruto) * 100) : 0)
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
    if (!confirm('¿Eliminar esta factura? Solo se puede si no tiene pagos ni factoring (revierte las cobranzas primero).')) return
    try { await contabilidadAPI.eliminarFactura(f.id); toast.success('Factura eliminada'); onChanged() }
    catch (e: any) { toast.error(errMsg(e, 'Error')) }
  }
  return (
    <>
      <tr className="hover:bg-[var(--surface-200)] transition-colors cursor-pointer" onClick={() => setOpen(o => !o)}>
        <td className="px-4 py-3 font-mono font-semibold text-brand-400 whitespace-nowrap">
          <span className="inline-flex items-center gap-1">{open ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}{f.numero_factura || `#${f.id}`}</span>
          {f.es_anticipo && (
            <span className="ml-1.5 inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-full text-[10px] font-sans font-medium bg-emerald-500/10 text-emerald-500 border border-emerald-500/20">
              <HandCoins className="w-2.5 h-2.5" /> Anticipo
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
                {f.saldo > 0 && (
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
  const [facturas, setFacturas] = useState<Factura[]>([])
  const [aging, setAging] = useState<Aging | null>(null)
  const [kpis, setKpis] = useState<Kpis | null>(null)
  const [loading, setLoading] = useState(true)
  const [q, setQ] = useState('')
  const [estado, setEstado] = useState('')
  const [error, setError] = useState('')
  const [modal, setModal] = useState<{ type: 'crear' | 'anticipo' | 'cobranza' | 'factoring'; factura?: Factura } | null>(null)

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
  const reload = () => load(q || undefined, estado || undefined)
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
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
          {[
            { icon: DollarSign, label: 'Facturado', value: fmtClp(kpis.facturado_clp), color: 'text-brand-400' },
            { icon: CheckCircle2, label: 'Cobrado', value: fmtClp(kpis.cobrado_cliente_clp ?? kpis.cobrado_clp), color: 'text-emerald-500' },
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
                    onFactoring={(fa) => setModal({ type: 'factoring', factura: fa })} />
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
    </div>
  )
}
