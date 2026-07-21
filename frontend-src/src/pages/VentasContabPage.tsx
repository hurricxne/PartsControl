// Página "Ventas — Contabilidad": lista las VENTAS agrupadas por OC de cliente (solo lectura)
// y, al expandir cada una, muestra sus ítems, guías de despacho y facturas. Consume
// contabilidadAPI (GET /ventas, /ventas/{oc}). El alta de facturas vive en FacturasPage.
import { useState, useEffect, useCallback } from 'react'
import {
  TrendingUp, Search, DollarSign, CreditCard, CheckCircle2, AlertCircle,
  Loader2, RefreshCw, ChevronDown, ChevronUp, Receipt, Truck, Clock, Pencil,
  HandCoins, X,
} from 'lucide-react'
import toast from 'react-hot-toast'
import { contabilidadAPI } from '../services/api'
import { fmtClp, fmtDate } from '../utils/format'
import { useAuthStore } from '../stores/authStore'
import OcClienteEditModal from '../components/OcClienteEditModal'

// Roles que pueden editar la OC. Hoy `user.rol` es undefined (login no lo envía) -> visible;
// cuando el backend propague el rol, el gate se cierra solo (ver authStore/role_guard).
const ROLES_EDITAN_OC = ['comercial', 'contabilidad', 'admin']

// ─── Tipos ────────────────────────────────────────────────────────────────────
interface VentaRow {
  oc_cliente_id: number
  cotizacion_id: number
  numero_oc: string | null
  numero_cotizacion: string
  cliente: string
  rut_cliente: string
  fecha_oc: string | null
  fecha_venta: string | null
  cond_pago: string | null
  total_items: number
  total_neto_clp: number
  iva_clp: number
  total_con_iva_clp: number
  n_facturas: number
  facturado_clp: number
  cobrado_clp: number
  por_cobrar_clp: number
  // Total OC − facturado (anticipo incluido). Distinto de por_cobrar_clp, que es
  // el saldo de lo YA facturado. Opcional: tolera respuestas del backend previo.
  por_facturar_clp?: number
  estado_cobranza: string
  adelantos?: { n: number; por_aprobar: number; aprobado_clp: number; pendiente_aplicar_clp: number }
}

interface AdelantoVenta {
  id: number
  estado: 'informado' | 'aprobado' | 'anulado'
  monto_esperado: number
  pct: number | null
  monto: number
  monto_aplicado: number
  pendiente_aplicar: number
  fecha_pago: string | null
  conciliado_banco: boolean
  factura_anticipo_folio: string | null
  observaciones: string | null
}

interface GuiaRef {
  numero_guia: string | null
  numero_despacho: string
  estado: string
  qty_despachada: number
  // El backend (detalle_venta) también envía estos campos de la guía firmada / expedición:
  despacho_id?: number
  despacho_item_id?: number
  guia_firmada?: boolean
  numero_expedicion?: string | null
  guia_firmada_archivo?: string | null
}
interface FacturaRef {
  factura_id: number
  numero_factura: string | null
  fecha_vencimiento: string | null
  plazo_dias: number | null
  estado_pago: string
  cantidad: number   // cantidad del ítem cubierta por ESTA factura (tandas parciales)
}
interface VentaItem {
  id: number
  item_num: number
  numero_parte: string
  descripcion: string
  marca: string
  cantidad: number
  precio_unit_venta_clp: number
  total_venta_clp: number
  estado_item: string
  guias: GuiaRef[]
  facturas: FacturaRef[]
}
// Factura de la OC (payload de _serialize_factura del backend). La usa el
// desglose "Facturas de esta OC"; la MISMA fuente que ve Facturas y Cobranzas.
interface FacturaOcItem {
  id: number
  item_cotizacion_id: number | null
  anticipo_factura_id: number | null
  numero_parte: string | null
  descripcion: string | null
  cantidad: number
  total_neto: number
}
interface FacturaOc {
  id: number
  numero_factura: string | null
  es_anticipo: boolean
  numero_guia: string | null
  fecha_emision: string | null
  plazo_dias: number | null
  fecha_vencimiento: string | null
  monto_bruto: number
  monto_pagado: number
  saldo: number
  estado_pago: string
  dias_vencimiento: number | null
  items: FacturaOcItem[]
}
interface ResumenCobranza {
  n_facturas: number
  facturado_clp: number
  cobrado_clp: number
  por_cobrar_clp: number
  estado_cobranza: string
  por_facturar_clp?: number
  anticipo_por_descontar_clp?: number
  // Mercadería físicamente sin facturar (bruto), autoritativa del backend:
  // por_facturar_clp = max(mercaderia_pendiente − anticipo_por_descontar, 0)
  mercaderia_pendiente_clp?: number
}
interface VentaDetalle {
  oc_cliente_id: number
  cotizacion_id: number
  numero_oc: string | null
  numero_cotizacion: string
  cliente: string
  rut_cliente: string
  fecha_oc: string | null
  cond_pago: string | null
  fecha_entrega: string | null
  asesor_id?: number | null
  total_neto_clp: number
  iva_clp: number
  total_con_iva_clp: number
  items: VentaItem[]
  adelantos?: AdelantoVenta[]
  facturas?: FacturaOc[]
  resumen?: ResumenCobranza
}
interface Kpis {
  facturado_clp: number
  cobrado_clp: number
  cobrado_cliente_clp?: number
  por_cobrar_clp: number
  vencido_clp: number
  en_factoring_clp: number
}

const COBRANZA_BADGE: Record<string, { cls: string; label: string }> = {
  sin_factura: { cls: 'bg-gray-500/10 text-gray-400 border-gray-500/20', label: 'Sin factura' },
  por_cobrar:  { cls: 'bg-blue-500/10 text-blue-400 border-blue-400/20', label: 'Por cobrar' },
  parcial:     { cls: 'bg-amber-500/10 text-amber-400 border-amber-400/20', label: 'Pago parcial' },
  cobrada:     { cls: 'bg-emerald-500/10 text-emerald-500 border-emerald-500/20', label: 'Cobrada' },
  vencida:     { cls: 'bg-red-500/10 text-red-400 border-red-400/20', label: 'Vencida' },
}
function CobranzaBadge({ estado, faltaFacturar }: { estado: string; faltaFacturar?: boolean }) {
  const m = COBRANZA_BADGE[estado] ?? COBRANZA_BADGE.sin_factura
  // Badge honesto: "Cobrada" a secas engaña cuando lo cobrado es solo un anticipo
  // y el grueso de la OC ni siquiera se ha facturado. El sufijo lo transparenta.
  const label = estado === 'cobrada' && faltaFacturar ? 'Cobrada · falta facturar' : m.label
  return <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-medium border ${m.cls}`}>{label}</span>
}

const PAGO_BADGE: Record<string, { cls: string; label: string }> = {
  por_cobrar:  { cls: 'bg-blue-500/10 text-blue-400', label: 'Por cobrar' },
  parcial:     { cls: 'bg-amber-500/10 text-amber-400', label: 'Parcial' },
  pagada:      { cls: 'bg-emerald-500/10 text-emerald-500', label: 'Pagada' },
  vencida:     { cls: 'bg-red-500/10 text-red-400', label: 'Vencida' },
  factorizada: { cls: 'bg-purple-500/10 text-purple-400', label: 'Factoring' },
}

const PERIODO_LABELS: Record<string, string> = {
  '': 'Todo', semana: 'Semana', mes: 'Mes', anio: 'Año',
}

// ─── Modal: informar adelanto (para OCs ya cerradas) ──────────────────────────
function InformarAdelantoModal({ ocId, onClose, onDone }: { ocId: number; onClose: () => void; onDone: () => void }) {
  const [pct, setPct] = useState('50')
  const [monto, setMonto] = useState('')
  const [obs, setObs] = useState('')
  const [saving, setSaving] = useState(false)
  const submit = async () => {
    if (!monto && !pct) { toast.error('Indica el % o el monto esperado'); return }
    setSaving(true)
    try {
      await contabilidadAPI.informarAdelanto({
        oc_cliente_id: ocId,
        pct: monto ? undefined : (Number(pct) || undefined),
        monto_esperado: monto ? Number(monto) : undefined,
        observaciones: obs || undefined,
      })
      toast.success('Adelanto informado — queda esperando aprobación de Tesorería')
      onDone(); onClose()
    } catch (e: any) {
      const d = e?.response?.data?.detail
      toast.error(typeof d === 'string' ? d : 'No se pudo informar el adelanto')
    } finally { setSaving(false) }
  }
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div className="w-full max-w-md rounded-2xl border shadow-2xl" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }} onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between px-5 py-3 border-b" style={{ borderColor: 'var(--border)' }}>
          <h3 className="font-semibold text-sm" style={{ color: 'var(--text-primary)' }}>Informar adelanto del cliente</h3>
          <button onClick={onClose} className="p-1 rounded-lg hover:bg-white/10" style={{ color: 'var(--text-muted)' }}><X className="w-4 h-4" /></button>
        </div>
        <div className="p-5 space-y-3">
          <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
            Queda <b>informado</b>: Tesorería confirma la plata recibida en su pestaña Adelantos, y al emitir la(s)
            factura(s) el sistema lo aplica solo como pago.
          </p>
          <div className="grid grid-cols-2 gap-3">
            <label className="block"><span className="block text-[11px] font-semibold uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>% del total</span>
              <input type="number" min={1} max={100} className="w-full px-3 py-2 rounded-lg border text-sm" style={{ backgroundColor: 'var(--surface-200)', borderColor: 'var(--border)', color: 'var(--text-primary)' }} value={pct} onChange={e => setPct(e.target.value)} disabled={!!monto} />
            </label>
            <label className="block"><span className="block text-[11px] font-semibold uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>o monto exacto (c/IVA)</span>
              <input type="number" className="w-full px-3 py-2 rounded-lg border text-sm" style={{ backgroundColor: 'var(--surface-200)', borderColor: 'var(--border)', color: 'var(--text-primary)' }} value={monto} onChange={e => setMonto(e.target.value)} placeholder="opcional" />
            </label>
          </div>
          <input className="w-full px-3 py-2 rounded-lg border text-sm" style={{ backgroundColor: 'var(--surface-200)', borderColor: 'var(--border)', color: 'var(--text-primary)' }} value={obs} onChange={e => setObs(e.target.value)} placeholder="Observaciones (opcional)" />
          <button onClick={submit} disabled={saving} className="btn-primary w-full flex items-center justify-center gap-2">
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <HandCoins className="w-4 h-4" />} Informar adelanto
          </button>
        </div>
      </div>
    </div>
  )
}

// ─── Desglose de la OC: avance, facturas y pendiente ──────────────────────────
// Sobre este umbral la tabla completa de ítems parte PLEGADA (anti-muro con OCs
// de cientos de líneas); bajo el umbral queda abierta como siempre.
const UMBRAL_TABLA_PLEGADA = 10

// Cantidad de un ítem aún sin facturar (las tandas parciales restan lo facturado).
// Clamp a 0: una sobrefacturación por corrección manual no debe dar remanente negativo.
const qtyPendiente = (item: VentaItem): number =>
  Math.max(item.cantidad - item.facturas.reduce((s, f) => s + (Number(f.cantidad) || 0), 0), 0)

// Etiquetas del estado logístico para el grupo "Por facturar": responden POR QUÉ
// ese ítem aún no se factura y dónde hay que empujar. Orden = cercanía a facturar.
const GRUPOS_PENDIENTE: { estados: string[]; label: string; dot: string; hint: string }[] = [
  { estados: ['despachado'], label: 'Despachado', dot: 'bg-brand-400', hint: 'listo para facturar (guía firmada)' },
  { estados: ['en_bodega'], label: 'En bodega', dot: 'bg-emerald-500', hint: 'despachable ya' },
  { estados: ['embarcado'], label: 'Embarcado', dot: 'bg-blue-400', hint: 'en tránsito' },
  { estados: ['pre_embarcado', 'preparado'], label: 'En preparación', dot: 'bg-sky-400', hint: 'pre-embarque' },
  { estados: ['comprado'], label: 'Comprado', dot: 'bg-amber-400', hint: 'esperando al proveedor' },
  { estados: ['cerrado', 'ingresado'], label: 'Por comprar', dot: 'bg-gray-400', hint: 'venta cerrada, sin OC al proveedor' },
  { estados: ['reclamo_proveedor'], label: 'En reclamo', dot: 'bg-red-400', hint: 'reclamo al proveedor' },
]

function VencimientoLabel({ f }: { f: FacturaOc }) {
  if (f.saldo <= 0 || f.dias_vencimiento === null || f.dias_vencimiento === undefined) {
    return f.fecha_vencimiento ? <span style={{ color: 'var(--text-muted)' }}>{fmtDate(f.fecha_vencimiento)}</span> : <span>—</span>
  }
  const d = f.dias_vencimiento
  if (d < 0) return <span className="text-red-400 font-semibold">vencida hace {-d}d</span>
  if (d === 0) return <span className="text-amber-400 font-semibold">vence HOY</span>
  return <span style={{ color: 'var(--text-muted)' }}>vence en {d}d</span>
}

// Barra de avance de la plata de la OC (patrón "Estado de Avance" de Despachos):
// ▓ cobrado · ▒ facturado sin cobrar · ░ por facturar. Con nota del anticipo
// cuando explica la diferencia entre mercadería sin facturar y por facturar.
function AvanceOc({ detalle }: { detalle: VentaDetalle }) {
  const r = detalle.resumen
  if (!r || r.por_facturar_clp === undefined) return null   // backend previo: sin datos, sin barra
  const total = detalle.total_con_iva_clp || 0
  const cobrado = Math.min(r.cobrado_clp, r.facturado_clp)
  const factSinCobrar = Math.max(r.facturado_clp - cobrado, 0)
  const pct = (v: number) => (total > 0 ? Math.min(Math.max((v / total) * 100, 0), 100) : 0)
  const anticipo = r.anticipo_por_descontar_clp || 0
  return (
    <div className="px-5 py-3 space-y-2 border-b" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
      <div className="h-2 rounded-full overflow-hidden flex" style={{ backgroundColor: 'var(--surface-300)' }}>
        <div className="bg-emerald-500 h-full" style={{ width: `${pct(cobrado)}%` }} title={`Cobrado ${fmtClp(cobrado)}`} />
        <div className="bg-blue-400 h-full" style={{ width: `${pct(factSinCobrar)}%` }} title={`Facturado sin cobrar ${fmtClp(factSinCobrar)}`} />
      </div>
      <div className="flex flex-wrap gap-x-5 gap-y-1 text-[11px] font-semibold">
        <span><span className="inline-block w-2 h-2 rounded-full bg-emerald-500 mr-1.5" />Cobrado <span style={{ color: 'var(--text-primary)' }}>{fmtClp(cobrado)}</span></span>
        <span><span className="inline-block w-2 h-2 rounded-full bg-blue-400 mr-1.5" />Por cobrar <span style={{ color: 'var(--text-primary)' }}>{fmtClp(r.por_cobrar_clp)}</span></span>
        <span><span className="inline-block w-2 h-2 rounded-full mr-1.5" style={{ backgroundColor: 'var(--surface-300)' }} />Por facturar <span className="text-amber-400">{fmtClp(r.por_facturar_clp)}</span></span>
        {anticipo > 0 && (
          <span style={{ color: 'var(--text-faint)' }} className="font-normal">
            (mercadería sin facturar {fmtClp(r.mercaderia_pendiente_clp ?? (r.por_facturar_clp || 0) + anticipo)} − anticipo por descontar {fmtClp(anticipo)})
          </span>
        )}
      </div>
    </div>
  )
}

// Facturas de la OC: misma fuente que Facturas y Cobranzas, con sus ítems al expandir.
function FacturasOcSection({ facturas }: { facturas: FacturaOc[] }) {
  const [abiertas, setAbiertas] = useState<Record<number, boolean>>({})
  if (!facturas.length) return null
  return (
    <div className="px-5 py-3 border-b" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
      <p className="text-[11px] font-semibold uppercase tracking-wider mb-1.5" style={{ color: 'var(--text-faint)' }}>
        Facturas de esta OC ({facturas.length})
      </p>
      {facturas.map(f => {
        const pago = PAGO_BADGE[f.estado_pago] ?? { cls: 'bg-gray-500/10 text-gray-400', label: f.estado_pago }
        const abierta = !!abiertas[f.id]
        const conItems = f.items.length > 0
        return (
          <div key={f.id} className="border-b last:border-0" style={{ borderColor: 'var(--border)' }}>
            <button
              onClick={() => conItems && setAbiertas(p => ({ ...p, [f.id]: !p[f.id] }))}
              className={`w-full flex items-center gap-2 py-1.5 text-xs text-left ${conItems ? 'hover:bg-white/5' : 'cursor-default'}`}
            >
              <Receipt className="w-3.5 h-3.5 shrink-0 text-brand-400" />
              <span className="font-mono font-semibold whitespace-nowrap" style={{ color: 'var(--text-primary)' }}>
                N° {f.numero_factura || `#${f.id}`}
              </span>
              {f.es_anticipo && (
                <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded-full bg-purple-500/10 text-purple-400 whitespace-nowrap">Anticipo</span>
              )}
              <span className="whitespace-nowrap" style={{ color: 'var(--text-muted)' }}>
                {f.numero_guia ? <>Guía {f.numero_guia}</> : f.es_anticipo ? 'sin guía' : 'guía —'}
              </span>
              <span className="font-bold whitespace-nowrap ml-auto" style={{ color: 'var(--text-primary)' }}>{fmtClp(f.monto_bruto)}</span>
              <span className="whitespace-nowrap hidden sm:inline"><VencimientoLabel f={f} /></span>
              <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-semibold whitespace-nowrap ${pago.cls}`}>{pago.label}</span>
              {conItems && (abierta ? <ChevronUp className="w-3.5 h-3.5 shrink-0" style={{ color: 'var(--text-faint)' }} /> : <ChevronDown className="w-3.5 h-3.5 shrink-0" style={{ color: 'var(--text-faint)' }} />)}
            </button>
            {abierta && conItems && (
              <div className="ml-6 mb-2 text-[11px]">
                {f.items.map(it => (
                  <div key={it.id} className="flex items-center gap-2 py-0.5" style={{ color: it.total_neto < 0 ? undefined : 'var(--text-muted)' }}>
                    <span className="font-mono">{it.numero_parte || (it.anticipo_factura_id ? '↩' : '—')}</span>
                    <span className={`truncate ${it.total_neto < 0 ? 'text-purple-400' : ''}`}>{it.descripcion || ''}</span>
                    <span className="ml-auto whitespace-nowrap">{it.cantidad > 0 ? `${it.cantidad} × ` : ''}</span>
                    <span className={`whitespace-nowrap font-medium ${it.total_neto < 0 ? 'text-purple-400' : ''}`}>{fmtClp(it.total_neto)} neto</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

// Lo que AÚN no llega a factura, agrupado por estado logístico: responde
// "¿por qué no puedo facturar esto todavía y dónde empujo?".
function PorFacturarSection({ detalle }: { detalle: VentaDetalle }) {
  const [abiertos, setAbiertos] = useState<Record<string, boolean>>({})
  const r = detalle.resumen
  const pendientes = detalle.items
    .map(it => ({ it, qty: qtyPendiente(it) }))
    .filter(x => x.qty > 0.001)
  if (!pendientes.length) return null
  // Bruto aproximado con el factor de IVA efectivo de la OC (cubre exentos: factor 1)
  const factorIva = detalle.total_neto_clp > 0 ? detalle.total_con_iva_clp / detalle.total_neto_clp : 1
  const brutoDe = (x: { it: VentaItem; qty: number }) =>
    (x.it.cantidad > 0 ? (x.it.total_venta_clp * x.qty) / x.it.cantidad : 0) * factorIva
  const totalPend = pendientes.reduce((s, x) => s + brutoDe(x), 0)
  const grupos = GRUPOS_PENDIENTE
    .map(g => ({ ...g, filas: pendientes.filter(x => g.estados.includes(x.it.estado_item)) }))
    .filter(g => g.filas.length > 0)
  const sinGrupo = pendientes.filter(x => !GRUPOS_PENDIENTE.some(g => g.estados.includes(x.it.estado_item)))
  if (sinGrupo.length) grupos.push({ estados: [], label: 'Otro estado', dot: 'bg-gray-500', hint: '', filas: sinGrupo })
  return (
    <div className="px-5 py-3 border-b" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
      <p className="text-[11px] font-semibold uppercase tracking-wider mb-1.5" style={{ color: 'var(--text-faint)' }}>
        Por facturar · <span className="text-amber-400">{fmtClp(r?.por_facturar_clp ?? totalPend)}</span>
        {(r?.anticipo_por_descontar_clp || 0) > 0 && (
          <span className="normal-case font-normal" style={{ color: 'var(--text-faint)' }}>
            {' '}(mercadería {fmtClp(r?.mercaderia_pendiente_clp ?? totalPend)} − anticipo {fmtClp(r!.anticipo_por_descontar_clp!)})
          </span>
        )}
      </p>
      {grupos.map(g => {
        const abierto = !!abiertos[g.label]
        const monto = g.filas.reduce((s, x) => s + brutoDe(x), 0)
        return (
          <div key={g.label} className="border-b last:border-0" style={{ borderColor: 'var(--border)' }}>
            <button onClick={() => setAbiertos(p => ({ ...p, [g.label]: !p[g.label] }))}
              className="w-full flex items-center gap-2 py-1.5 text-xs text-left hover:bg-white/5">
              <span className={`inline-block w-2 h-2 rounded-full shrink-0 ${g.dot}`} />
              <span className="font-semibold whitespace-nowrap" style={{ color: 'var(--text-primary)' }}>{g.label}</span>
              {g.hint && <span className="hidden sm:inline" style={{ color: 'var(--text-faint)' }}>· {g.hint}</span>}
              <span style={{ color: 'var(--text-muted)' }}>· {g.filas.length} ítem{g.filas.length === 1 ? '' : 's'}</span>
              <span className="font-bold whitespace-nowrap ml-auto" style={{ color: 'var(--text-primary)' }}>{fmtClp(monto)}</span>
              {abierto ? <ChevronUp className="w-3.5 h-3.5 shrink-0" style={{ color: 'var(--text-faint)' }} /> : <ChevronDown className="w-3.5 h-3.5 shrink-0" style={{ color: 'var(--text-faint)' }} />}
            </button>
            {abierto && (
              <div className="ml-6 mb-2 text-[11px]" style={{ color: 'var(--text-muted)' }}>
                {g.filas.map(x => (
                  <div key={x.it.id} className="flex items-center gap-2 py-0.5">
                    <span className="font-mono font-semibold" style={{ color: 'var(--text-primary)' }}>{x.it.numero_parte || '—'}</span>
                    <span className="truncate">{x.it.descripcion}</span>
                    <span className="ml-auto whitespace-nowrap">
                      {x.qty < x.it.cantidad ? `quedan ${x.qty} de ${x.it.cantidad}` : `${x.qty} un.`}
                    </span>
                    <span className="whitespace-nowrap font-medium" style={{ color: 'var(--text-primary)' }}>{fmtClp(brutoDe(x))}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

// Tabla completa de ítems (la de siempre), con control anti-muro: sobre el umbral
// parte PLEGADA y al abrirla trae buscador por N° de parte / descripción / marca.
function ItemsPlegables({ items }: { items: VentaItem[] }) {
  const esGrande = items.length > UMBRAL_TABLA_PLEGADA
  const [abierta, setAbierta] = useState(!esGrande)
  const [filtro, setFiltro] = useState('')
  const fl = filtro.trim().toLowerCase()
  const visibles = !fl ? items : items.filter(it =>
    (it.numero_parte || '').toLowerCase().includes(fl) ||
    (it.descripcion || '').toLowerCase().includes(fl) ||
    (it.marca || '').toLowerCase().includes(fl)
  )
  return (
    <div className="border-b" style={{ borderColor: 'var(--border)' }}>
      <div className="px-5 py-2 flex items-center gap-3" style={{ backgroundColor: 'var(--surface-100)' }}>
        <button onClick={() => setAbierta(a => !a)}
          className="inline-flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider hover:text-brand-400"
          style={{ color: 'var(--text-faint)' }}>
          {abierta ? <ChevronUp className="w-3.5 h-3.5" /> : <ChevronDown className="w-3.5 h-3.5" />}
          Ítems ({items.length})
        </button>
        {abierta && esGrande && (
          <div className="relative flex-1 max-w-xs ml-auto">
            <Search className="w-3.5 h-3.5 absolute left-2.5 top-1/2 -translate-y-1/2" style={{ color: 'var(--text-faint)' }} />
            <input
              value={filtro}
              onChange={e => setFiltro(e.target.value)}
              placeholder="Buscar N° de parte, descripción…"
              className="w-full text-xs rounded-lg border pl-8 pr-2 py-1.5"
              style={{ backgroundColor: 'var(--surface-200)', borderColor: 'var(--border)', color: 'var(--text-primary)' }}
            />
          </div>
        )}
      </div>
      {abierta && (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr style={{ backgroundColor: 'var(--surface-100)', borderBottom: '1px solid var(--border)' }}>
                {['#', 'N° Parte', 'Descripción', 'Cant.', 'P. Unit', 'Total', 'Guía despacho', 'Factura', 'Plazo / Vence', 'Estado pago'].map(h => (
                  <th key={h} className="px-3 py-2 text-left font-semibold uppercase tracking-wider whitespace-nowrap" style={{ color: 'var(--text-faint)' }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {visibles.length === 0 && (
                <tr><td colSpan={10} className="px-3 py-4 text-center italic" style={{ color: 'var(--text-faint)' }}>
                  Ningún ítem coincide con "{filtro}"
                </td></tr>
              )}
              {visibles.map((item, idx) => {
                const facs = item.facturas
                // Estado agregado: pagada si todas pagadas; vencida si alguna; parcial si mezcla
                const estados = facs.map(x => x.estado_pago)
                const estadoAgg = !estados.length ? null
                  : estados.every(e => e === estados[0]) ? estados[0]
                  : estados.includes('vencida') ? 'vencida' : 'parcial'
                const pago = estadoAgg ? (PAGO_BADGE[estadoAgg] ?? { cls: 'bg-gray-500/10 text-gray-400', label: estadoAgg }) : null
                return (
                  <tr key={item.id} style={{ backgroundColor: idx % 2 === 0 ? 'transparent' : 'var(--surface-100)', borderBottom: '1px solid var(--border)' }}>
                    <td className="px-3 py-2 font-mono text-[11px]" style={{ color: 'var(--text-muted)' }}>{item.item_num}</td>
                    <td className="px-3 py-2 font-mono font-semibold whitespace-nowrap" style={{ color: 'var(--text-primary)' }}>{item.numero_parte || '—'}</td>
                    <td className="px-3 py-2 max-w-[200px] truncate" style={{ color: 'var(--text-primary)' }} title={item.descripcion}>{item.descripcion}</td>
                    <td className="px-3 py-2 text-right" style={{ color: 'var(--text-muted)' }}>{item.cantidad}</td>
                    <td className="px-3 py-2 text-right font-medium whitespace-nowrap" style={{ color: 'var(--text-primary)' }}>{fmtClp(item.precio_unit_venta_clp)}</td>
                    <td className="px-3 py-2 text-right font-bold whitespace-nowrap text-brand-400">{fmtClp(item.total_venta_clp)}</td>
                    <td className="px-3 py-2 whitespace-nowrap" style={{ color: 'var(--text-muted)' }}>
                      {item.guias.length ? (
                        <span className="inline-flex items-center gap-1">
                          <Truck className="w-3 h-3 text-emerald-400" />
                          {item.guias.map(g => g.numero_guia || g.numero_despacho).join(', ')}
                        </span>
                      ) : <span className="italic" style={{ color: 'var(--text-faint)' }}>sin despachar</span>}
                    </td>
                    <td className="px-3 py-2 whitespace-nowrap" style={{ color: 'var(--text-muted)' }}>
                      {facs.length ? <span className="inline-flex items-center gap-1"><Receipt className="w-3 h-3 text-brand-400" />{facs.map(x => x.numero_factura || `#${x.factura_id}`).join(', ')}</span>
                           : <span className="italic" style={{ color: 'var(--text-faint)' }}>sin facturar</span>}
                    </td>
                    <td className="px-3 py-2 whitespace-nowrap" style={{ color: 'var(--text-muted)' }}>
                      {facs.length === 1 ? <span>{facs[0].plazo_dias ? `${facs[0].plazo_dias}d · ` : ''}{fmtDate(facs[0].fecha_vencimiento)}</span>
                        : facs.length > 1 ? <span>varias</span> : '—'}
                    </td>
                    <td className="px-3 py-2 whitespace-nowrap">
                      {pago ? <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-semibold ${pago.cls}`}>{pago.label}</span> : '—'}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// ─── Tarjeta de venta (por OC, expandible a ítem) ─────────────────────────────
function VentaCard({ venta, onUpdated }: { venta: VentaRow; onUpdated: () => void }) {
  const [open, setOpen] = useState(false)
  const [detalle, setDetalle] = useState<VentaDetalle | null>(null)
  const [loading, setLoading] = useState(false)
  const [errDet, setErrDet] = useState(false)
  const [editing, setEditing] = useState(false)
  const [modalAdel, setModalAdel] = useState(false)
  const rol = useAuthStore(s => s.user)?.rol
  const canEdit = !rol || ROLES_EDITAN_OC.includes(rol)

  const fetchDetalle = async () => {
    setLoading(true); setErrDet(false)
    try {
      const { data } = await contabilidadAPI.ventaDetalle(venta.oc_cliente_id)
      setDetalle(data)
    } catch { setErrDet(true) } finally { setLoading(false) }
  }

  const toggle = async () => {
    const next = !open
    setOpen(next)
    if (next && !detalle) await fetchDetalle()
  }

  return (
    <div className="rounded-2xl border overflow-hidden" style={{ backgroundColor: 'var(--surface-50)', borderColor: 'var(--border)' }}>
      <button onClick={toggle} className="w-full text-left px-5 py-4 flex flex-col sm:flex-row sm:items-center gap-3 hover:bg-white/5 transition-colors">
        <div className="flex-1 min-w-0 space-y-1.5">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-mono font-bold text-sm text-brand-400">
              OC {venta.numero_oc || `#${venta.oc_cliente_id}`}
            </span>
            <span className="text-[10px] font-medium px-2 py-0.5 rounded-full border" style={{ background: 'var(--surface-200)', borderColor: 'var(--border)', color: 'var(--text-muted)' }}>
              COT-{venta.numero_cotizacion}
            </span>
            <CobranzaBadge estado={venta.estado_cobranza} faltaFacturar={(venta.por_facturar_clp || 0) > 0} />
            {venta.adelantos && venta.adelantos.n > 0 && (
              venta.adelantos.por_aprobar > 0
                ? <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium border bg-amber-500/10 text-amber-400 border-amber-400/20">
                    <HandCoins className="w-3 h-3" /> Adelanto por aprobar
                  </span>
                : <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium border bg-emerald-500/10 text-emerald-500 border-emerald-500/20">
                    <HandCoins className="w-3 h-3" /> Adelanto {fmtClp(venta.adelantos.aprobado_clp)}
                  </span>
            )}
          </div>
          <p className="font-semibold text-sm truncate" style={{ color: 'var(--text-primary)' }}>{venta.cliente || '—'}</p>
          <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
            {venta.rut_cliente && <span>{venta.rut_cliente} · </span>}
            {venta.fecha_oc && <span>OC: {fmtDate(venta.fecha_oc)} · </span>}
            {venta.cond_pago && <span>{venta.cond_pago} · </span>}
            <span>{venta.total_items} ítems · {venta.n_facturas} factura(s)</span>
          </p>
        </div>
        <div className="flex sm:flex-col items-center sm:items-end gap-4 sm:gap-1 shrink-0">
          <div className="text-right">
            <p className="text-[10px] uppercase tracking-wider font-semibold" style={{ color: 'var(--text-faint)' }}>Total c/IVA</p>
            <p className="font-bold text-base" style={{ color: 'var(--text-primary)' }}>{fmtClp(venta.total_con_iva_clp)}</p>
          </div>
          <div className="text-right">
            <p className="text-[10px] uppercase tracking-wider font-semibold" style={{ color: 'var(--text-faint)' }}>Por cobrar</p>
            <p className="font-bold text-sm text-blue-400">{fmtClp(venta.por_cobrar_clp)}</p>
          </div>
          {/* Plata de la OC que aún NO llega a factura: se ve sin expandir, para
              escanear la lista y detectar ventas con facturación pendiente. */}
          {(venta.por_facturar_clp || 0) > 0 && (
            <div className="text-right">
              <p className="text-[10px] uppercase tracking-wider font-semibold" style={{ color: 'var(--text-faint)' }}>Por facturar</p>
              <p className="font-bold text-sm text-amber-400">{fmtClp(venta.por_facturar_clp!)}</p>
            </div>
          )}
          <div className="ml-2 sm:ml-0" style={{ color: 'var(--text-muted)' }}>
            {open ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
          </div>
        </div>
      </button>

      {editing && detalle && (
        <OcClienteEditModal
          ocId={venta.oc_cliente_id}
          initial={{
            numero_oc: detalle.numero_oc,
            fecha_oc: detalle.fecha_oc,
            cond_pago: detalle.cond_pago,
            fecha_entrega: detalle.fecha_entrega,
            asesor_id: detalle.asesor_id ?? null,
          }}
          onSaved={() => { fetchDetalle(); onUpdated() }}
          onClose={() => setEditing(false)}
        />
      )}

      {open && (
        <div className="border-t" style={{ borderColor: 'var(--border)' }}>
          {loading && (
            <div className="flex items-center justify-center py-8"><Loader2 className="w-5 h-5 animate-spin text-brand-400" /></div>
          )}
          {!loading && errDet && (
            <div className="px-5 py-6 text-center text-sm" style={{ color: 'var(--text-muted)' }}>
              No se pudo cargar el detalle.
              <button onClick={fetchDetalle} className="ml-2 text-brand-400 underline">Reintentar</button>
            </div>
          )}
          {!loading && detalle && (
            <>
              {canEdit && (
                <div className="px-5 py-2 flex justify-end border-b"
                  style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-100)' }}>
                  <button onClick={() => setEditing(true)}
                    className="inline-flex items-center gap-1 text-xs text-brand-400 hover:underline">
                    <Pencil className="w-3 h-3" /> Editar OC
                  </button>
                </div>
              )}
              {/* Ejecutivo primero: avance de la plata, facturas y lo pendiente.
                  El detalle ítem a ítem va al final, plegado si la OC es grande. */}
              <AvanceOc detalle={detalle} />
              <FacturasOcSection facturas={detalle.facturas || []} />
              <PorFacturarSection detalle={detalle} />
              <ItemsPlegables items={detalle.items} />
              {/* Adelantos de la venta */}
              <div className="px-5 py-3 space-y-1.5" style={{ backgroundColor: 'var(--surface-100)', borderTop: '1px solid var(--border)' }}>
                <div className="flex items-center justify-between gap-2">
                  <p className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>
                    Adelantos del cliente
                  </p>
                  <button onClick={() => setModalAdel(true)}
                    className="inline-flex items-center gap-1 text-[11px] font-medium text-brand-400 hover:bg-brand-500/10 rounded px-2 py-1">
                    <HandCoins className="w-3 h-3" /> Informar adelanto
                  </button>
                </div>
                {(detalle.adelantos || []).length === 0 ? (
                  <p className="text-xs" style={{ color: 'var(--text-faint)' }}>Sin adelantos informados para esta venta.</p>
                ) : (detalle.adelantos || []).map(a => (
                  <div key={a.id} className="flex items-center justify-between gap-2 text-xs py-1 border-b" style={{ borderColor: 'var(--border)' }}>
                    <span style={{ color: 'var(--text-muted)' }}>
                      {a.estado === 'informado' && <>Informado · esperado <b>{fmtClp(a.monto_esperado)}</b>{a.pct ? ` (${a.pct}%)` : ''} · esperando aprobación de Tesorería</>}
                      {a.estado === 'aprobado' && <>
                        Aprobado <b style={{ color: 'var(--text-primary)' }}>{fmtClp(a.monto)}</b> ({fmtDate(a.fecha_pago)})
                        {a.factura_anticipo_folio ? <> · respaldo Factura N° {a.factura_anticipo_folio}</> : null}
                        {a.monto_aplicado >= a.monto - 1 ? <span className="text-emerald-500"> · aplicado a factura</span>
                          : a.monto_aplicado > 0 ? <> · aplicado {fmtClp(a.monto_aplicado)} (queda {fmtClp(a.pendiente_aplicar)})</> : <> · se aplicará al facturar</>}
                        {a.conciliado_banco ? <span className="text-emerald-500"> · conciliado banco</span> : null}
                      </>}
                      {a.estado === 'anulado' && <span className="line-through">Anulado · {fmtClp(a.monto_esperado || a.monto)}</span>}
                    </span>
                  </div>
                ))}
              </div>
              <div className="px-5 py-3 flex flex-wrap justify-end gap-6 text-xs font-semibold" style={{ backgroundColor: 'var(--surface-100)', borderTop: '1px solid var(--border)' }}>
                <span><span style={{ color: 'var(--text-faint)' }}>Neto: </span><span style={{ color: 'var(--text-primary)' }}>{fmtClp(detalle.total_neto_clp)}</span></span>
                <span><span style={{ color: 'var(--text-faint)' }}>IVA: </span><span style={{ color: 'var(--text-primary)' }}>{fmtClp(detalle.iva_clp)}</span></span>
                <span><span style={{ color: 'var(--text-faint)' }}>Total: </span><span className="text-brand-400 font-bold text-sm">{fmtClp(detalle.total_con_iva_clp)}</span></span>
              </div>
            </>
          )}
        </div>
      )}
      {modalAdel && (
        <InformarAdelantoModal ocId={venta.oc_cliente_id} onClose={() => setModalAdel(false)}
          onDone={() => { fetchDetalle(); onUpdated() }} />
      )}
    </div>
  )
}

// ─── Página ───────────────────────────────────────────────────────────────────
export default function VentasContabPage() {
  const [ventas, setVentas] = useState<VentaRow[]>([])
  const [kpis, setKpis] = useState<Kpis | null>(null)
  const [loading, setLoading] = useState(true)
  const [q, setQ] = useState('')
  const [periodo, setPeriodo] = useState('')
  const [error, setError] = useState('')

  const load = useCallback(async (search?: string, per?: string) => {
    setLoading(true); setError('')
    try {
      const [vRes, kRes] = await Promise.all([
        contabilidadAPI.listVentas(search, per),
        contabilidadAPI.kpis(per),
      ])
      setVentas(vRes.data)
      setKpis(kRes.data)
    } catch {
      setError('No se pudieron cargar las ventas.')
    } finally { setLoading(false) }
  }, [])

  useEffect(() => { load(q || undefined, periodo || undefined) }, [periodo])

  const handleSearch = (v: string) => {
    setQ(v)
    if (v.length === 0 || v.length >= 2) load(v || undefined, periodo || undefined)
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-2xl font-bold" style={{ color: 'var(--text-primary)' }}>Ventas — Contabilidad</h1>
          <p className="text-sm mt-1" style={{ color: 'var(--text-muted)' }}>
            Por orden de compra · despliega cada venta para ver sus ítems, guías de despacho y facturas
          </p>
        </div>
        <button onClick={() => load(q || undefined, periodo || undefined)} className="btn-secondary flex items-center gap-2 self-start">
          <RefreshCw className="w-4 h-4" />
        </button>
      </div>

      {/* Periodo */}
      <div className="flex items-center gap-2 flex-wrap">
        {Object.entries(PERIODO_LABELS).map(([key, label]) => (
          <button key={key} onClick={() => setPeriodo(key)}
            className={`px-4 py-1.5 rounded-full text-sm font-medium border transition-all ${periodo === key ? 'border-brand-500 bg-brand-500/10 text-brand-400' : 'border-transparent'}`}
            style={periodo !== key ? { backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)', color: 'var(--text-muted)' } : {}}>
            {label}
          </button>
        ))}
      </div>

      {/* KPIs */}
      {kpis && (
        <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
          {[
            { icon: <DollarSign className="w-5 h-5" />, label: 'Facturado', value: fmtClp(kpis.facturado_clp), color: 'text-brand-400' },
            { icon: <CheckCircle2 className="w-5 h-5" />, label: 'Cobrado', value: fmtClp(kpis.cobrado_cliente_clp ?? kpis.cobrado_clp), color: 'text-emerald-400' },
            { icon: <CreditCard className="w-5 h-5" />, label: 'Por cobrar', value: fmtClp(kpis.por_cobrar_clp), color: 'text-amber-400' },
            { icon: <AlertCircle className="w-5 h-5" />, label: 'Vencido', value: fmtClp(kpis.vencido_clp), color: 'text-red-400' },
            { icon: <Clock className="w-5 h-5" />, label: 'En factoring', value: fmtClp(kpis.en_factoring_clp), color: 'text-purple-400' },
          ].map(({ icon, label, value, color }) => (
            <div key={label} className="rounded-2xl border p-4 flex items-center gap-3" style={{ backgroundColor: 'var(--surface-50)', borderColor: 'var(--border)' }}>
              <div className={`p-2 rounded-xl ${color} opacity-80`} style={{ backgroundColor: 'var(--surface-200)' }}>{icon}</div>
              <div className="min-w-0">
                <p className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>{label}</p>
                <p className={`font-bold text-lg leading-tight truncate ${color}`}>{value}</p>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Search */}
      <div className="relative">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4" style={{ color: 'var(--text-faint)' }} />
        <input type="text" placeholder="Buscar por cliente, OC, cotización o RUT…" value={q}
          onChange={e => handleSearch(e.target.value)}
          className="w-full pl-10 pr-4 py-2.5 rounded-xl border text-sm focus:outline-none focus:ring-2 focus:ring-brand-500/40"
          style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)', color: 'var(--text-primary)' }} />
      </div>

      {error && <div className="rounded-xl border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-400">{error}</div>}
      {loading && <div className="flex items-center justify-center py-16"><Loader2 className="w-7 h-7 animate-spin text-brand-400" /></div>}
      {!loading && !error && ventas.length === 0 && (
        <div className="rounded-2xl border py-16 text-center" style={{ backgroundColor: 'var(--surface-50)', borderColor: 'var(--border)' }}>
          <TrendingUp className="w-10 h-10 mx-auto mb-3 opacity-20" style={{ color: 'var(--text-muted)' }} />
          <p className="text-sm font-medium" style={{ color: 'var(--text-muted)' }}>No hay ventas{periodo ? ' para el período' : ''}</p>
        </div>
      )}
      {!loading && ventas.length > 0 && (
        <div className="space-y-3">
          <p className="text-xs font-semibold uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>
            {ventas.length} {ventas.length === 1 ? 'venta' : 'ventas'}
          </p>
          {ventas.map(v => <VentaCard key={v.oc_cliente_id} venta={v} onUpdated={() => load(q || undefined, periodo || undefined)} />)}
        </div>
      )}
    </div>
  )
}
