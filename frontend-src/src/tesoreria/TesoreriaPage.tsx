// Página "Tesorería" (Grupo AM): quien revisa, aprueba y concilia lo que otros módulos
// registran. Compras registra compras (pago futuro/inmediato/parcial); Facturas y
// Cobranzas registra los ingresos de caja; Tesorería:
//   · aprueba los pagos por vencer (da la orden → crea el Comprobante de Egreso),
//   · aprueba los ADELANTOS de cliente que Comercial informa (confirma la plata
//     recibida SIN exigir cartola; al aprobar se aplican solos a las facturas),
//   · concilia la cartola del banco: cargos↔egresos y abonos↔cobranzas/adelantos,
//   · proyecta el flujo de caja (NIC 7).
// Pestañas: Conciliar · Por pagar · Adelantos · Flujo de caja · Movimientos · Cuentas.
import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import {
  Landmark, Plus, Search, Loader2, RefreshCw, ChevronDown, ChevronUp, X, Trash2,
  Upload, Link2, Unlink, CheckCircle2, AlertCircle, Wallet, Building2, Receipt,
  Banknote, CalendarClock, HandCoins,
} from 'lucide-react'
import toast from 'react-hot-toast'
import { fmtClp, fmtDate } from '../utils/format'
import { tesoreriaAPI } from './api'
import type {
  Cuenta, Movimiento, EgresoMatch, CobranzaMatch, AdelantoMatch, Destino, Resumen,
  CompraPorPagar, PorPagarResp, FlujoCaja, Bucket, BucketInfo, Adelanto, AprobacionesResp,
} from './types'

// ─── Helpers UI ────────────────────────────────────────────────────────────────
function Modal({ title, wide, onClose, children }: { title: string; wide?: boolean; onClose: () => void; children: React.ReactNode }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div className={`w-full ${wide ? 'max-w-2xl' : 'max-w-md'} rounded-2xl border shadow-2xl max-h-[90vh] overflow-y-auto`}
        style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }} onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between px-5 py-3 border-b sticky top-0" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-100)' }}>
          <h3 className="font-semibold text-sm" style={{ color: 'var(--text-primary)' }}>{title}</h3>
          <button onClick={onClose} className="p-1 rounded-lg hover:bg-white/10" style={{ color: 'var(--text-muted)' }}><X className="w-4 h-4" /></button>
        </div>
        <div className="p-5 space-y-3">{children}</div>
      </div>
    </div>
  )
}
// Fecha de HOY en horario local (toISOString() es UTC: de noche en Chile daría mañana)
const hoyISO = () => {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}
const inputCls = 'w-full px-3 py-2 rounded-lg border text-sm focus:outline-none focus:ring-2 focus:ring-brand-500/40'
const inputStyle = { backgroundColor: 'var(--surface-200)', borderColor: 'var(--border)', color: 'var(--text-primary)' } as React.CSSProperties
function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (<label className="block"><span className="block text-[11px] font-semibold uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>{label}</span>{children}</label>)
}
const TIPO_BADGE: Record<string, { cls: string; label: string }> = {
  cargo: { cls: 'bg-red-500/10 text-red-400', label: 'Cargo' },
  abono: { cls: 'bg-emerald-500/10 text-emerald-500', label: 'Abono' },
}
const BUCKET_LABEL: Record<Bucket, string> = {
  vencido: 'Vencido', d0_7: '0–7 días', d8_30: '8–30 días',
  d31_60: '31–60 días', d61_mas: '61+ días', sin_fecha: 'Sin fecha',
}
const MEDIOS_PAGO = ['transferencia', 'cheque', 'efectivo', 'tarjeta']

// Bancos sugeridos (los que ya usan las cuentas registradas) para TODOS los campos
// "Banco" de la página, no solo el de la cuenta: escribir "BCI", "Bci" y "B.C.I." en
// pagos y adelantos rompe cualquier agrupación o búsqueda posterior por banco.
function BancosDatalist({ id, bancos }: { id: string; bancos: string[] }) {
  return <datalist id={id}>{bancos.map(b => <option key={b} value={b} />)}</datalist>
}

// Totales del flujo de caja sumando TODAS las ventanas: sin ellos la pantalla obliga a
// sumar 6 columnas a mano para saber cuánto hay que pagar y cuánto va a entrar.
const sumaBuckets = (buckets: Bucket[], filas: Record<Bucket, BucketInfo>): BucketInfo =>
  buckets.reduce<BucketInfo>((acc, b) => ({
    monto: acc.monto + (filas[b]?.monto || 0),
    n: acc.n + (filas[b]?.n || 0),
  }), { monto: 0, n: 0 })
const sumaNeto = (buckets: Bucket[], neto: Record<Bucket, number>): number =>
  buckets.reduce((s, b) => s + (neto[b] || 0), 0)

// Destino de una conciliación en una línea (egreso de Compras / cobranza / adelanto).
// A nivel de módulo para poder nombrarlo también en el confirm de "Desconciliar".
const destinoTexto = (d: Destino): string => d.clase === 'egreso'
  ? `${d.beneficiario || 'egreso'} · ${d.n_compras} gasto${d.n_compras !== 1 ? 's' : ''} · ${fmtClp(d.monto_total_clp)}`
  : d.clase === 'adelanto'
    ? `Adelanto ${d.cliente || ''}${d.numero_oc ? ` · OC ${d.numero_oc}` : ''} · ${fmtClp(d.monto)}`
    : `Factura ${d.numero_factura || d.factura_id} · ${fmtClp(d.monto)}`

// Tarjeta del encabezado. Tipada aparte porque `sub` solo la usan algunas (sin el tipo
// explícito TS infiere el elemento sin `sub` y no deja leerla).
interface KpiCard {
  icon: React.ComponentType<{ className?: string }>
  label: string
  value: number
  color: string
  money: boolean
  sub?: string
}

// ─── Modal: cuenta bancaria ─────────────────────────────────────────────────────
function CuentaModal({ cuenta, bancos, onClose, onDone }: { cuenta: Cuenta | null; bancos: string[]; onClose: () => void; onDone: () => void }) {
  const [banco, setBanco] = useState(cuenta?.banco || 'Santander')
  const [nombre, setNombre] = useState(cuenta?.nombre || '')
  const [numero, setNumero] = useState(cuenta?.numero_cuenta || '')
  const [moneda, setMoneda] = useState(cuenta?.moneda || 'CLP')
  const [activo, setActivo] = useState(cuenta?.activo ?? true)
  const [saving, setSaving] = useState(false)
  const submit = async () => {
    if (!banco.trim()) { toast.error('Indica el banco'); return }
    setSaving(true)
    try {
      // se conserva observaciones (el PUT del backend es de reemplazo total)
      const data = { banco, nombre: nombre || undefined, numero_cuenta: numero || undefined, moneda, activo, observaciones: cuenta?.observaciones ?? undefined }
      if (cuenta) await tesoreriaAPI.actualizarCuenta(cuenta.id, data)
      else await tesoreriaAPI.crearCuenta(data)
      toast.success('Cuenta guardada'); onDone(); onClose()
    } catch (e: any) { toast.error(e?.response?.data?.detail || 'Error') } finally { setSaving(false) }
  }
  return (
    <Modal title={cuenta ? 'Editar cuenta' : 'Nueva cuenta bancaria'} onClose={onClose}>
      <Field label="Banco">
        <input className={inputCls} style={inputStyle} list="bancos-list" value={banco} onChange={e => setBanco(e.target.value)} />
        <datalist id="bancos-list">{bancos.map(b => <option key={b} value={b} />)}</datalist>
      </Field>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Alias / nombre"><input className={inputCls} style={inputStyle} value={nombre} onChange={e => setNombre(e.target.value)} placeholder="Cta Cte principal" /></Field>
        <Field label="N° de cuenta"><input className={inputCls} style={inputStyle} value={numero} onChange={e => setNumero(e.target.value)} /></Field>
        <Field label="Moneda">
          <select className={inputCls} style={inputStyle} value={moneda} onChange={e => setMoneda(e.target.value)}>
            <option value="CLP">CLP</option><option value="USD">USD</option><option value="EUR">EUR</option>
          </select>
        </Field>
        <label className="flex items-center gap-2 mt-6 text-xs" style={{ color: 'var(--text-muted)' }}>
          <input type="checkbox" checked={activo} onChange={e => setActivo(e.target.checked)} /> Activa
        </label>
      </div>
      <button onClick={submit} disabled={saving} className="btn-primary w-full flex items-center justify-center gap-2">
        {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Building2 className="w-4 h-4" />} Guardar cuenta
      </button>
    </Modal>
  )
}

// ─── Modal: importar cartola ────────────────────────────────────────────────────
function ImportModal({ cuentaId, onClose, onDone }: { cuentaId: number; onClose: () => void; onDone: () => void }) {
  const [file, setFile] = useState<File | null>(null)
  const [nombre, setNombre] = useState('')
  const [saving, setSaving] = useState(false)
  const submit = async () => {
    if (!file) { toast.error('Selecciona un archivo CSV o Excel'); return }
    setSaving(true)
    try {
      const { data } = await tesoreriaAPI.importarCartola(cuentaId, file, nombre || undefined)
      toast.success(`${data.n_importados} movimientos importados`)
      if (data.warnings?.length) toast(data.warnings.join('; '))
      onDone(); onClose()
    } catch (e: any) { toast.error(e?.response?.data?.detail || 'No se pudo importar la cartola') } finally { setSaving(false) }
  }
  return (
    <Modal title="Importar cartola" onClose={onClose}>
      <p className="text-xs" style={{ color: 'var(--text-muted)' }}>Sube la cartola del banco en <b>CSV</b> o <b>Excel (.xlsx)</b>. Se detectan columnas Fecha, Detalle/Glosa, Cargo/Abono (o Monto), Referencia y Saldo.</p>
      <Field label="Archivo (CSV / .xlsx)">
        <input type="file" accept=".csv,.xlsx" className={inputCls} style={inputStyle} onChange={e => setFile(e.target.files?.[0] || null)} />
      </Field>
      <Field label="Nombre (opcional)"><input className={inputCls} style={inputStyle} value={nombre} onChange={e => setNombre(e.target.value)} placeholder="Cartola junio 2026" /></Field>
      <button onClick={submit} disabled={saving} className="btn-primary w-full flex items-center justify-center gap-2">
        {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Upload className="w-4 h-4" />} Importar
      </button>
    </Modal>
  )
}

// ─── Modal: movimiento manual ───────────────────────────────────────────────────
function MovManualModal({ cuentaId, onClose, onDone }: { cuentaId: number; onClose: () => void; onDone: () => void }) {
  const [fecha, setFecha] = useState(hoyISO())
  const [glosa, setGlosa] = useState('')
  const [tipo, setTipo] = useState('cargo')
  const [monto, setMonto] = useState('')
  const [referencia, setReferencia] = useState('')
  const [saving, setSaving] = useState(false)
  const submit = async () => {
    if (!monto || Number(monto) <= 0) { toast.error('Monto inválido'); return }
    setSaving(true)
    try {
      await tesoreriaAPI.crearMovimiento({ cuenta_id: cuentaId, fecha, glosa: glosa || undefined, tipo, monto: Number(monto), referencia: referencia || undefined })
      toast.success('Movimiento agregado'); onDone(); onClose()
    } catch (e: any) { toast.error(e?.response?.data?.detail || 'Error') } finally { setSaving(false) }
  }
  return (
    <Modal title="Movimiento manual" onClose={onClose}>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Fecha"><input type="date" className={inputCls} style={inputStyle} value={fecha} onChange={e => setFecha(e.target.value)} /></Field>
        <Field label="Tipo">
          <select className={inputCls} style={inputStyle} value={tipo} onChange={e => setTipo(e.target.value)}>
            <option value="cargo">Cargo (egreso)</option><option value="abono">Abono (ingreso)</option>
          </select>
        </Field>
        <Field label="Monto (CLP)"><input type="number" className={inputCls} style={inputStyle} value={monto} onChange={e => setMonto(e.target.value)} /></Field>
        <Field label="Referencia"><input className={inputCls} style={inputStyle} value={referencia} onChange={e => setReferencia(e.target.value)} /></Field>
      </div>
      <Field label="Glosa"><input className={inputCls} style={inputStyle} value={glosa} onChange={e => setGlosa(e.target.value)} /></Field>
      <button onClick={submit} disabled={saving} className="btn-primary w-full flex items-center justify-center gap-2">
        {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />} Agregar movimiento
      </button>
    </Modal>
  )
}

// ─── Modal: aprobar pago (Tesorería da la orden) ────────────────────────────────
function PagoModal({ compras, bancos, onClose, onDone }: { compras: CompraPorPagar[]; bancos: string[]; onClose: () => void; onDone: () => void }) {
  const [fecha, setFecha] = useState(hoyISO())
  const [medio, setMedio] = useState('transferencia')
  const [banco, setBanco] = useState('')
  const [numeroOperacion, setNumeroOperacion] = useState('')
  const acreedores = useMemo(() => [...new Set(compras.map(c => c.acreedor).filter(Boolean))], [compras])
  const [beneficiario, setBeneficiario] = useState(acreedores.length === 1 ? (acreedores[0] as string) : '')
  const [glosa, setGlosa] = useState('')
  const [montos, setMontos] = useState<Record<number, string>>(
    () => Object.fromEntries(compras.map(c => [c.compra_id, String(Math.round(c.saldo_clp))])))
  const [saving, setSaving] = useState(false)
  const total = compras.reduce((s, c) => s + (Number(montos[c.compra_id]) || 0), 0)

  const submit = async () => {
    for (const c of compras) {
      const m = Number(montos[c.compra_id])
      if (!m || m <= 0) { toast.error(`Monto inválido para ${c.acreedor || 'compra ' + c.compra_id}`); return }
      if (m > c.saldo_clp + 1) { toast.error(`El pago a ${c.acreedor || 'compra ' + c.compra_id} excede su saldo (${fmtClp(c.saldo_clp)})`); return }
    }
    setSaving(true)
    try {
      await tesoreriaAPI.aprobarPago({
        fecha, medio, banco: banco || undefined, numero_operacion: numeroOperacion || undefined,
        beneficiario: beneficiario || undefined, glosa: glosa || undefined,
        detalles: compras.map(c => ({ compra_id: c.compra_id, monto_clp: Number(montos[c.compra_id]) })),
      })
      toast.success('Pago aprobado y registrado (Comprobante de Egreso)')
      onDone(); onClose()
    } catch (e: any) { toast.error(e?.response?.data?.detail || 'No se pudo registrar el pago') } finally { setSaving(false) }
  }
  return (
    <Modal title={`Aprobar pago · ${compras.length} compra${compras.length !== 1 ? 's' : ''}`} wide onClose={onClose}>
      <div className="rounded-xl border divide-y" style={{ borderColor: 'var(--border)' }}>
        {compras.map(c => (
          <div key={c.compra_id} className="flex items-center justify-between gap-3 px-3 py-2 text-xs" style={{ borderColor: 'var(--border)' }}>
            <div className="min-w-0">
              <p className="font-medium truncate" style={{ color: 'var(--text-primary)' }}>{c.acreedor || '—'}{c.numero_documento ? ` · doc ${c.numero_documento}` : ''}</p>
              <p style={{ color: 'var(--text-faint)' }}>vence {c.fecha_vencimiento ? fmtDate(c.fecha_vencimiento) : 'sin fecha'} · saldo {fmtClp(c.saldo_clp)}</p>
            </div>
            <input type="number" className="w-32 px-2 py-1.5 rounded-lg border text-sm text-right" style={inputStyle}
              value={montos[c.compra_id] ?? ''} onChange={e => setMontos(prev => ({ ...prev, [c.compra_id]: e.target.value }))} />
          </div>
        ))}
      </div>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Fecha del pago"><input type="date" className={inputCls} style={inputStyle} value={fecha} onChange={e => setFecha(e.target.value)} /></Field>
        <Field label="Medio">
          <select className={inputCls} style={inputStyle} value={medio} onChange={e => setMedio(e.target.value)}>
            {MEDIOS_PAGO.map(m => <option key={m} value={m}>{m}</option>)}
          </select>
        </Field>
        <Field label="Banco">
          <input className={inputCls} style={inputStyle} list="bancos-pago" value={banco} onChange={e => setBanco(e.target.value)} />
          <BancosDatalist id="bancos-pago" bancos={bancos} />
        </Field>
        <Field label="N° operación"><input className={inputCls} style={inputStyle} value={numeroOperacion} onChange={e => setNumeroOperacion(e.target.value)} /></Field>
      </div>
      <Field label="Beneficiario"><input className={inputCls} style={inputStyle} value={beneficiario} onChange={e => setBeneficiario(e.target.value)} placeholder={acreedores.length > 1 ? 'Varios acreedores (opcional)' : ''} /></Field>
      <Field label="Glosa"><input className={inputCls} style={inputStyle} value={glosa} onChange={e => setGlosa(e.target.value)} /></Field>
      <button onClick={submit} disabled={saving} className="btn-primary w-full flex items-center justify-center gap-2">
        {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Banknote className="w-4 h-4" />} Aprobar y registrar pago · {fmtClp(total)}
      </button>
    </Modal>
  )
}

// ─── Tarjetas de candidatos a conciliar ─────────────────────────────────────────
function EgresoCard({ e, onVincular }: { e: EgresoMatch; onVincular: () => void }) {
  const docs = (e.compras || []).map(c => c.numero_documento || c.acreedor || `compra ${c.compra_id}`).join(', ')
  return (
    <div className="flex items-center justify-between gap-2 rounded-lg border px-3 py-2 text-xs" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
      <div className="min-w-0">
        <p className="font-medium truncate" style={{ color: 'var(--text-primary)' }}>
          <span className="text-brand-400 font-semibold">{fmtClp(e.monto_total_clp)}</span> · {e.n_compras} gasto{e.n_compras !== 1 ? 's' : ''}{e.beneficiario ? ` · ${e.beneficiario}` : ''}
        </p>
        <p className="truncate" style={{ color: 'var(--text-faint)' }}>
          egreso {fmtDate(e.fecha)}{e.dias_diferencia != null ? ` · ${e.dias_diferencia}d de dif.` : ''}{e.numero_operacion ? ` · op ${e.numero_operacion}` : ''}{docs ? ` · ${docs}` : ''}
        </p>
      </div>
      <button onClick={onVincular} className="btn-primary px-2.5 py-1 text-xs flex items-center gap-1 shrink-0"><Link2 className="w-3 h-3" /> Vincular</button>
    </div>
  )
}

function CobranzaCard({ c, onVincular }: { c: CobranzaMatch; onVincular: () => void }) {
  return (
    <div className="flex items-center justify-between gap-2 rounded-lg border px-3 py-2 text-xs" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
      <div className="min-w-0">
        <p className="font-medium truncate" style={{ color: 'var(--text-primary)' }}>
          <span className="text-emerald-500 font-semibold">{fmtClp(c.monto)}</span> · Factura {c.numero_factura || c.factura_id}
        </p>
        <p className="truncate" style={{ color: 'var(--text-faint)' }}>
          cobranza {fmtDate(c.fecha)}{c.dias_diferencia != null ? ` · ${c.dias_diferencia}d de dif.` : ''}{c.medio ? ` · ${c.medio}` : ''}{c.numero_operacion ? ` · op ${c.numero_operacion}` : ''}
        </p>
      </div>
      <button onClick={onVincular} className="btn-primary px-2.5 py-1 text-xs flex items-center gap-1 shrink-0"><Link2 className="w-3 h-3" /> Vincular</button>
    </div>
  )
}

function AdelantoCard({ a, onVincular }: { a: AdelantoMatch; onVincular: () => void }) {
  return (
    <div className="flex items-center justify-between gap-2 rounded-lg border px-3 py-2 text-xs" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
      <div className="min-w-0">
        <p className="font-medium truncate" style={{ color: 'var(--text-primary)' }}>
          <span className="text-emerald-500 font-semibold">{fmtClp(a.monto)}</span> · Adelanto {a.cliente || ''}{a.numero_oc ? ` · OC ${a.numero_oc}` : ''}
        </p>
        <p className="truncate" style={{ color: 'var(--text-faint)' }}>
          pago {fmtDate(a.fecha)}{a.dias_diferencia != null ? ` · ${a.dias_diferencia}d de dif.` : ''}{a.banco ? ` · ${a.banco}` : ''}{a.numero_operacion ? ` · op ${a.numero_operacion}` : ''}
        </p>
      </div>
      <button onClick={onVincular} className="btn-primary px-2.5 py-1 text-xs flex items-center gap-1 shrink-0"><Link2 className="w-3 h-3" /> Vincular</button>
    </div>
  )
}

// Total de la venta DERIVADO de lo informado (monto esperado ÷ %). El backend arma
// monto_esperado = total bruto de la venta × pct / 100 (contabilidad.informar_adelanto),
// así que la inversa reconstruye ese total. Quien aprueba necesita verlo: sin él lee
// "esperado $3.000.000 (50%)" sin poder validar que el 50% sea de la venta correcta.
// null cuando Comercial informó un monto exacto sin % (no hay nada que derivar).
const totalVentaDerivado = (a: Adelanto): number | null => {
  const pct = Number(a.pct) || 0
  const esperado = Number(a.monto_esperado) || 0
  if (pct <= 0 || pct > 100 || esperado <= 0) return null
  return Math.round((esperado * 100) / pct)
}

// ─── Modal: aprobar adelanto (Tesorería confirma la plata recibida) ─────────────
function AprobarAdelantoModal({ adelanto, bancos, onClose, onDone }: { adelanto: Adelanto; bancos: string[]; onClose: () => void; onDone: () => void }) {
  const sugerido = adelanto.abono_sugerido
  const [monto, setMonto] = useState(String(Math.round(sugerido?.monto || adelanto.monto_esperado || 0) || ''))
  const [fecha, setFecha] = useState(sugerido?.fecha || hoyISO())
  const [banco, setBanco] = useState('')
  const [numeroOperacion, setNumeroOperacion] = useState(sugerido?.referencia || '')
  const [obs, setObs] = useState('')
  const [saving, setSaving] = useState(false)
  const submit = async () => {
    if (!monto || Number(monto) <= 0) { toast.error('Indica el monto recibido'); return }
    setSaving(true)
    try {
      const { data } = await tesoreriaAPI.aprobarAdelanto(adelanto.id, {
        monto: Number(monto), fecha_pago: fecha || undefined, banco: banco || undefined,
        numero_operacion: numeroOperacion || undefined, observaciones: obs || undefined,
      })
      toast.success(data.aplicado_ahora_clp > 0
        ? `Adelanto aprobado y aplicado a facturas (${fmtClp(data.aplicado_ahora_clp)})`
        : 'Adelanto aprobado — se aplicará solo al emitir la factura')
      onDone(); onClose()
    } catch (e: any) { toast.error(e?.response?.data?.detail || 'No se pudo aprobar el adelanto') } finally { setSaving(false) }
  }
  return (
    <Modal title={`Aprobar adelanto · ${adelanto.cliente || ''} ${adelanto.numero_oc ? `(OC ${adelanto.numero_oc})` : ''}`} onClose={onClose}>
      <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
        Confirma la plata recibida del cliente (no exige cartola: la conciliación con el abono viene después).
        {adelanto.monto_esperado > 0 && <> Comercial informó <b>{fmtClp(adelanto.monto_esperado)}</b>{adelanto.pct ? ` (${adelanto.pct}%)` : ''}.</>}
        {/* Total de la venta: sin él el % informado no se puede validar */}
        {totalVentaDerivado(adelanto) !== null && (
          <> Total de la venta <b>{fmtClp(totalVentaDerivado(adelanto)!)}</b> c/IVA.</>
        )}
      </p>
      {sugerido && (
        <div className="rounded-lg border px-3 py-2 text-xs" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
          <p className="font-medium" style={{ color: 'var(--text-primary)' }}>Abono de la cartola que calza: <span className="text-emerald-500">{fmtClp(sugerido.monto)}</span> · {fmtDate(sugerido.fecha)}</p>
          <p className="truncate" style={{ color: 'var(--text-faint)' }}>{sugerido.glosa || ''}</p>
        </div>
      )}
      <div className="grid grid-cols-2 gap-3">
        <Field label="Monto recibido (CLP)"><input type="number" className={inputCls} style={inputStyle} value={monto} onChange={e => setMonto(e.target.value)} /></Field>
        <Field label="Fecha del pago"><input type="date" className={inputCls} style={inputStyle} value={fecha} onChange={e => setFecha(e.target.value)} /></Field>
        <Field label="Banco">
          <input className={inputCls} style={inputStyle} list="bancos-adelanto" value={banco} onChange={e => setBanco(e.target.value)} />
          <BancosDatalist id="bancos-adelanto" bancos={bancos} />
        </Field>
        <Field label="N° operación"><input className={inputCls} style={inputStyle} value={numeroOperacion} onChange={e => setNumeroOperacion(e.target.value)} /></Field>
      </div>
      <Field label="Observaciones"><input className={inputCls} style={inputStyle} value={obs} onChange={e => setObs(e.target.value)} /></Field>
      <button onClick={submit} disabled={saving} className="btn-primary w-full flex items-center justify-center gap-2">
        {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <HandCoins className="w-4 h-4" />} Aprobar adelanto · {fmtClp(Number(monto) || 0)}
      </button>
    </Modal>
  )
}

// ─── Fila de movimiento a conciliar (pestaña Conciliar) ─────────────────────────
function ConciliarRow({ m, onChanged }: { m: Movimiento; onChanged: () => void }) {
  const [open, setOpen] = useState(false)
  const [sugs, setSugs] = useState<Destino[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [q, setQ] = useState('')
  const [manual, setManual] = useState<Destino[] | null>(null)
  const seqRef = useRef(0)  // descarta respuestas fuera de orden del typeahead
  const esCargo = m.tipo === 'cargo'

  const expand = async () => {
    const next = !open; setOpen(next)
    if (next && sugs === null) {
      setBusy(true)
      try { const { data } = await tesoreriaAPI.sugerencias(m.id); setSugs(data.sugerencias || []) }
      catch (e: any) {
        setSugs([])
        // p.ej. 400 "cuenta en USD: conciliación solo CLP" — sin esto se ve como "sin coincidencias"
        toast.error(e?.response?.data?.detail || 'No se pudieron cargar las sugerencias')
      } finally { setBusy(false) }
    }
  }
  const vincular = async (d: Destino) => {
    try {
      if (d.clase === 'egreso') await tesoreriaAPI.conciliarEgreso(m.id, d.egreso_id)
      else if (d.clase === 'adelanto') await tesoreriaAPI.conciliarAdelanto(m.id, d.adelanto_id)
      else await tesoreriaAPI.conciliarCobranza(m.id, d.cobranza_id)
      toast.success('Movimiento conciliado'); onChanged()
    } catch (e: any) { toast.error(e?.response?.data?.detail || 'Error al conciliar') }
  }
  const buscar = async (v: string) => {
    setQ(v)
    const my = ++seqRef.current
    if (v.length >= 2) {
      try {
        if (esCargo) { const { data } = await tesoreriaAPI.egresosPendientes(v); if (my === seqRef.current) setManual(data.egresos) }
        else {
          // abono: cobranzas (búsqueda del backend) + adelantos aprobados sin conciliar
          // (lista corta; se filtra por cliente/OC aquí mismo)
          const [cob, adel] = await Promise.all([
            tesoreriaAPI.cobranzasPendientes(v),
            tesoreriaAPI.adelantosPendientes().catch(() => ({ data: { adelantos: [], total: 0 } })),
          ])
          if (my === seqRef.current) {
            const vl = v.toLowerCase()
            const adelantos = (adel.data.adelantos || []).filter(a =>
              `${a.cliente || ''} ${a.numero_oc || ''} ${a.numero_operacion || ''}`.toLowerCase().includes(vl))
            setManual([...(cob.data.cobranzas || []), ...adelantos])
          }
        }
      } catch { if (my === seqRef.current) setManual([]) }
    } else setManual(null)
  }
  const card = (d: Destino, key: string) => d.clase === 'egreso'
    ? <EgresoCard key={key} e={d} onVincular={() => vincular(d)} />
    : d.clase === 'adelanto'
      ? <AdelantoCard key={key} a={d} onVincular={() => vincular(d)} />
      : <CobranzaCard key={key} c={d} onVincular={() => vincular(d)} />
  return (
    <div className="rounded-xl border" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-100)' }}>
      <button onClick={expand} className="w-full flex items-center justify-between gap-3 px-4 py-3 text-left">
        <div className="min-w-0 flex items-center gap-2">
          <span className={`inline-flex px-2 py-0.5 rounded-full text-[11px] font-medium shrink-0 ${TIPO_BADGE[m.tipo]?.cls || ''}`}>{TIPO_BADGE[m.tipo]?.label || m.tipo}</span>
          <div className="min-w-0">
            <p className="font-medium truncate text-sm" style={{ color: 'var(--text-primary)' }}>{m.glosa || 'Movimiento'}</p>
            <p className="text-xs" style={{ color: 'var(--text-faint)' }}>{fmtDate(m.fecha)}{m.referencia ? ` · ${m.referencia}` : ''}</p>
          </div>
        </div>
        <div className="flex items-center gap-3 shrink-0">
          <span className={`font-semibold ${esCargo ? 'text-red-400' : 'text-emerald-500'}`}>{fmtClp(m.monto)}</span>
          {open ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
        </div>
      </button>
      {open && (
        <div className="px-4 pb-4 space-y-2 border-t pt-3" style={{ borderColor: 'var(--border)' }}>
          {busy && <div className="flex justify-center py-3"><Loader2 className="w-5 h-5 animate-spin text-brand-400" /></div>}
          {!busy && sugs && sugs.length > 0 && (
            <>
              <p className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>
                {esCargo ? 'Egresos de Compras sugeridos (mismo monto)' : 'Cobranzas y adelantos sugeridos (mismo monto)'}
              </p>
              {sugs.map((d, i) => card(d, 's' + i))}
            </>
          )}
          {!busy && sugs && sugs.length === 0 && (
            <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
              {esCargo ? 'Sin egresos de igual monto. Busca uno manualmente:' : 'Sin cobranzas de igual monto. Busca una manualmente:'}
            </p>
          )}
          <div className="relative mt-1">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4" style={{ color: 'var(--text-faint)' }} />
            <input className="w-full pl-9 pr-3 py-2 rounded-lg border text-sm focus:outline-none" style={inputStyle}
              placeholder={esCargo ? 'Buscar egreso por beneficiario u operación…' : 'Buscar cobranza por N° factura, operación o banco…'}
              value={q} onChange={e => buscar(e.target.value)} />
          </div>
          {manual && manual.map((d, i) => card(d, 'm' + i))}
          {manual && manual.length === 0 && <p className="text-xs" style={{ color: 'var(--text-faint)' }}>Sin resultados.</p>}
        </div>
      )}
    </div>
  )
}

// ─── Página ──────────────────────────────────────────────────────────────────────
type Tab = 'conciliar' | 'porpagar' | 'adelantos' | 'flujo' | 'movimientos' | 'cuentas'

export default function TesoreriaPage() {
  const [cuentas, setCuentas] = useState<Cuenta[]>([])
  const [bancos, setBancos] = useState<string[]>([])
  const [cuentaId, setCuentaId] = useState<number | null>(null)
  const [tab, setTab] = useState<Tab>('conciliar')
  const [resumen, setResumen] = useState<Resumen | null>(null)
  const [loading, setLoading] = useState(true)
  const [modal, setModal] = useState<{ type: 'cuenta' | 'import' | 'manual' | 'pago' | 'aprobar-adelanto'; cuenta?: Cuenta | null; adelanto?: Adelanto } | null>(null)

  // datos por pestaña
  const [pendientes, setPendientes] = useState<Movimiento[]>([])
  const [movs, setMovs] = useState<Movimiento[]>([])
  const [filtroEstado, setFiltroEstado] = useState('')
  const [filtroTipo, setFiltroTipo] = useState('')
  const [porPagar, setPorPagar] = useState<PorPagarResp | null>(null)
  const [ppQ, setPpQ] = useState('')
  // Map id→compra (no Set): lo seleccionado sobrevive aunque el buscador lo saque
  // de la vista, y el modal de pago recibe SIEMPRE todo lo marcado.
  const [seleccion, setSeleccion] = useState<Map<number, CompraPorPagar>>(new Map())
  const [flujo, setFlujo] = useState<FlujoCaja | null>(null)
  const [aprobaciones, setAprobaciones] = useState<AprobacionesResp | null>(null)

  const loadCuentas = useCallback(async () => {
    try {
      const { data } = await tesoreriaAPI.cuentas(true)
      setCuentas(data.cuentas); setBancos(data.bancos_sugeridos)
      setCuentaId(prev => prev ?? (data.cuentas.find(c => c.activo)?.id ?? data.cuentas[0]?.id ?? null))
    } catch (e: any) {
      // sin esto, un backend caído se vería como "no tienes cuentas" (engañoso)
      toast.error(e?.response?.data?.detail || 'No se pudieron cargar las cuentas bancarias')
    } finally { setLoading(false) }
  }, [])

  const loadResumen = useCallback(async (cid: number | null) => {
    try { const { data } = await tesoreriaAPI.resumen(cid ?? undefined); setResumen(data) } catch { /* noop */ }
  }, [])

  const loadPendientes = useCallback(async (cid: number) => {
    try { const { data } = await tesoreriaAPI.movimientos({ cuenta_id: cid, estado: 'pendiente', page_size: 200 }); setPendientes(data.movimientos) }
    catch (e: any) { setPendientes([]); toast.error(e?.response?.data?.detail || 'No se pudieron cargar los movimientos pendientes') }
  }, [])

  const loadMovs = useCallback(async (cid: number, estado: string, tipo: string) => {
    try { const { data } = await tesoreriaAPI.movimientos({ cuenta_id: cid, estado: estado || undefined, tipo: tipo || undefined, page_size: 300 }); setMovs(data.movimientos) }
    catch (e: any) { setMovs([]); toast.error(e?.response?.data?.detail || 'No se pudieron cargar los movimientos') }
  }, [])

  const loadPorPagar = useCallback(async (q: string) => {
    try { const { data } = await tesoreriaAPI.porPagar({ q: q || undefined, page_size: 300 }); setPorPagar(data) }
    catch (e: any) { setPorPagar(null); toast.error(e?.response?.data?.detail || 'No se pudo cargar Por pagar') }
  }, [])

  const loadFlujo = useCallback(async () => {
    try { const { data } = await tesoreriaAPI.flujoCaja(); setFlujo(data) }
    catch (e: any) { setFlujo(null); toast.error(e?.response?.data?.detail || 'No se pudo cargar el flujo de caja') }
  }, [])

  const loadAprobaciones = useCallback(async () => {
    try { const { data } = await tesoreriaAPI.aprobaciones(); setAprobaciones(data) }
    catch (e: any) { setAprobaciones(null); toast.error(e?.response?.data?.detail || 'No se pudieron cargar los adelantos') }
  }, [])

  useEffect(() => { loadCuentas() }, [loadCuentas])
  useEffect(() => {
    loadResumen(cuentaId)
    if (tab === 'conciliar' && cuentaId != null) loadPendientes(cuentaId)
    if (tab === 'movimientos' && cuentaId != null) loadMovs(cuentaId, filtroEstado, filtroTipo)
    if (tab === 'porpagar') loadPorPagar(ppQ)
    if (tab === 'adelantos') loadAprobaciones()
    if (tab === 'flujo') loadFlujo()
  }, [cuentaId, tab, filtroEstado, filtroTipo, ppQ, loadResumen, loadPendientes, loadMovs, loadPorPagar, loadAprobaciones, loadFlujo])

  const refrescar = () => {
    loadResumen(cuentaId)
    if (tab === 'conciliar' && cuentaId != null) loadPendientes(cuentaId)
    if (tab === 'movimientos' && cuentaId != null) loadMovs(cuentaId, filtroEstado, filtroTipo)
    if (tab === 'porpagar') { loadPorPagar(ppQ); setSeleccion(new Map()) }
    if (tab === 'adelantos') loadAprobaciones()
    if (tab === 'flujo') loadFlujo()
  }

  const desconciliar = async (m: Movimiento) => {
    // Deshacer una conciliación desarma el enlace cartola↔egreso/cobranza/adelanto que
    // alguien ya revisó (y en el caso del adelanto libera el candado del banco). Antes
    // bastaba UN clic sin aviso, al lado del botón de eliminar. Se confirma como el
    // borrado, y nombrando el destino para que se vea qué se está desarmando.
    const destino = m.destino ? destinoTexto(m.destino) : null
    if (!confirm(`¿Deshacer la conciliación de este movimiento${destino ? ` con ${destino}` : ''}?`)) return
    try { await tesoreriaAPI.desconciliar(m.id); toast.success('Conciliación deshecha'); refrescar() }
    catch (e: any) { toast.error(e?.response?.data?.detail || 'Error') }
  }
  const eliminarMov = async (m: Movimiento) => {
    if (!confirm('¿Eliminar este movimiento?')) return
    try { await tesoreriaAPI.eliminarMovimiento(m.id); toast.success('Movimiento eliminado'); refrescar() }
    catch (e: any) { toast.error(e?.response?.data?.detail || 'Error') }
  }
  const toggleSel = (c: CompraPorPagar) => setSeleccion(prev => {
    const next = new Map(prev)
    if (next.has(c.compra_id)) next.delete(c.compra_id); else next.set(c.compra_id, c)
    return next
  })
  const cuentaSel = cuentas.find(c => c.id === cuentaId) || null
  const comprasSeleccionadas = [...seleccion.values()]
  const totalSeleccion = comprasSeleccionadas.reduce((s, c) => s + c.saldo_clp, 0)

  if (loading) return <div className="flex items-center justify-center py-20"><Loader2 className="w-7 h-7 animate-spin text-brand-400" /></div>

  const destinoLinea = (d: Destino) => `↔ ${destinoTexto(d)}`

  // Tarjetas del encabezado. "Adelantos por aprobar" muestra el MONTO (el backend ya lo
  // manda en adelantos_por_aprobar_clp) con el conteo debajo: "3 adelantos" no dice si la
  // cola vale $300.000 o $30.000.000, que es lo que decide si hay que aprobar hoy.
  const kpiCards: KpiCard[] = resumen ? [
    { icon: Banknote, label: 'Por pagar (saldo)', value: resumen.monto_por_pagar_clp, color: 'text-brand-400', money: true },
    { icon: CalendarClock, label: 'Vencido por pagar', value: resumen.por_pagar_vencido_clp, color: 'text-red-400', money: true },
    {
      icon: HandCoins, label: 'Adelantos por aprobar',
      value: resumen.adelantos_por_aprobar_clp ?? 0, color: 'text-emerald-500', money: true,
      sub: `${resumen.adelantos_por_aprobar} adelanto${resumen.adelantos_por_aprobar !== 1 ? 's' : ''} informado${resumen.adelantos_por_aprobar !== 1 ? 's' : ''}`,
    },
    { icon: AlertCircle, label: 'Cargos pendientes', value: resumen.cargos_pendientes, color: 'text-amber-400', money: false },
    { icon: Wallet, label: 'Abonos pendientes', value: resumen.abonos_pendientes, color: 'text-emerald-500', money: false },
    { icon: Link2, label: 'Egresos sin conciliar', value: resumen.egresos_sin_conciliar, color: 'text-brand-400', money: false },
    { icon: Receipt, label: 'Cobranzas sin conciliar', value: resumen.cobranzas_sin_conciliar, color: 'text-emerald-500', money: false },
  ] : []

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-2xl font-bold" style={{ color: 'var(--text-primary)' }}>Tesorería</h1>
          <p className="text-sm mt-1" style={{ color: 'var(--text-muted)' }}>Aprueba los pagos, concilia la cartola del banco (pagos e ingresos) y proyecta tu caja</p>
        </div>
        <div className="flex items-center gap-2 self-start">
          {cuentas.length > 0 && (
            <select className="rounded-xl border px-3 py-2 text-sm" style={inputStyle} value={cuentaId ?? ''} onChange={e => setCuentaId(Number(e.target.value))}>
              {cuentas.map(c => <option key={c.id} value={c.id}>{c.banco}{c.nombre ? ` · ${c.nombre}` : ''}{!c.activo ? ' (inactiva)' : ''}</option>)}
            </select>
          )}
          <button onClick={refrescar} className="btn-secondary flex items-center gap-2"><RefreshCw className="w-4 h-4" /></button>
        </div>
      </div>

      {/* Resumen */}
      {resumen && (
        <div className="grid grid-cols-2 lg:grid-cols-6 gap-3">
          {kpiCards.map(s => (
            <div key={s.label} className="rounded-2xl p-3 sm:p-4 border" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
              <div className="w-8 h-8 rounded-xl flex items-center justify-center mb-2" style={{ backgroundColor: 'var(--surface-300)' }}><s.icon className={`w-4 h-4 ${s.color}`} /></div>
              <p className="text-[10px] uppercase tracking-widest leading-tight" style={{ color: 'var(--text-faint)' }}>{s.label}</p>
              <p className={`text-base sm:text-lg font-bold mt-0.5 ${s.color}`}>{s.money ? fmtClp(s.value) : s.value}</p>
              {s.sub && <p className="text-[10px] leading-tight" style={{ color: 'var(--text-faint)' }}>{s.sub}</p>}
            </div>
          ))}
        </div>
      )}

      {/* Tabs */}
      <div className="flex items-center gap-2 border-b overflow-x-auto" style={{ borderColor: 'var(--border)' }}>
        {([['conciliar', 'Conciliar'], ['porpagar', 'Por pagar'], ['adelantos', 'Adelantos'], ['flujo', 'Flujo de caja'], ['movimientos', 'Movimientos'], ['cuentas', 'Cuentas']] as const).map(([k, lbl]) => (
          <button key={k} onClick={() => setTab(k)}
            className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-all whitespace-nowrap ${tab === k ? 'border-brand-500 text-brand-400' : 'border-transparent'}`}
            style={tab !== k ? { color: 'var(--text-muted)' } : {}}>{lbl}</button>
        ))}
        <div className="ml-auto flex items-center gap-2 pb-1">
          {(tab === 'conciliar' || tab === 'movimientos') && cuentaSel && (
            <button onClick={() => setModal({ type: 'import' })} className="btn-secondary flex items-center gap-2 text-xs"><Upload className="w-3.5 h-3.5" /> Importar cartola</button>
          )}
          {tab === 'movimientos' && cuentaSel && (
            <button onClick={() => setModal({ type: 'manual' })} className="btn-secondary flex items-center gap-2 text-xs"><Plus className="w-3.5 h-3.5" /> Manual</button>
          )}
          {tab === 'cuentas' && (
            <button onClick={() => setModal({ type: 'cuenta', cuenta: null })} className="btn-primary flex items-center gap-2 text-xs"><Plus className="w-3.5 h-3.5" /> Nueva cuenta</button>
          )}
        </div>
      </div>

      {/* Pestaña Conciliar */}
      {tab === 'conciliar' && (cuentas.length === 0 ? (
        <div className="rounded-2xl border py-16 text-center" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
          <Landmark className="w-10 h-10 mx-auto mb-3 opacity-20" style={{ color: 'var(--text-muted)' }} />
          <p className="text-sm font-medium" style={{ color: 'var(--text-muted)' }}>Primero crea una cuenta bancaria</p>
          <button onClick={() => setModal({ type: 'cuenta', cuenta: null })} className="btn-primary mt-4 inline-flex items-center gap-2"><Plus className="w-4 h-4" /> Nueva cuenta</button>
        </div>
      ) : (
        <div className="space-y-2">
          {pendientes.length === 0 ? (
            <div className="rounded-2xl border py-12 text-center" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
              <CheckCircle2 className="w-9 h-9 mx-auto mb-2 text-emerald-500/40" />
              <p className="text-sm" style={{ color: 'var(--text-muted)' }}>No hay movimientos pendientes de conciliar en esta cuenta.</p>
              <p className="text-xs mt-1" style={{ color: 'var(--text-faint)' }}>Importa una cartola para empezar.</p>
            </div>
          ) : pendientes.map(m => <ConciliarRow key={m.id} m={m} onChanged={refrescar} />)}
        </div>
      ))}

      {/* Pestaña Por pagar (aprobación de pagos) */}
      {tab === 'porpagar' && (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative flex-1 min-w-[220px]">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4" style={{ color: 'var(--text-faint)' }} />
              <input className="w-full pl-9 pr-3 py-2 rounded-lg border text-sm focus:outline-none" style={inputStyle}
                placeholder="Buscar por acreedor, documento, RUT o categoría…" value={ppQ} onChange={e => setPpQ(e.target.value)} />
            </div>
            {porPagar && (['vencido', 'd0_7', 'd8_30', 'd31_60', 'd61_mas', 'sin_fecha'] as Bucket[]).map(b => (
              porPagar.buckets[b]?.n > 0 && (
                <span key={b} className={`px-2.5 py-1 rounded-full text-[11px] font-medium border ${b === 'vencido' ? 'text-red-400 border-red-500/30 bg-red-500/10' : ''}`}
                  style={b !== 'vencido' ? { backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)', color: 'var(--text-muted)' } : {}}>
                  {BUCKET_LABEL[b]}: {fmtClp(porPagar.buckets[b].monto)} ({porPagar.buckets[b].n})
                </span>
              )
            ))}
          </div>
          {!porPagar || porPagar.compras.length === 0 ? (
            <div className="rounded-2xl border py-12 text-center" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
              <CheckCircle2 className="w-9 h-9 mx-auto mb-2 text-emerald-500/40" />
              <p className="text-sm" style={{ color: 'var(--text-muted)' }}>No hay compras con saldo pendiente. Todo pagado.</p>
            </div>
          ) : (
            <div className="rounded-2xl border overflow-hidden" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead><tr className="border-b" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
                    {['', 'Acreedor', 'Documento', 'Vence', 'Total', 'Pagado', 'Saldo', 'Estado'].map((h, i) => <th key={i} className="text-left px-3 py-3 text-xs font-semibold uppercase tracking-wider whitespace-nowrap" style={{ color: 'var(--text-faint)' }}>{h}</th>)}
                  </tr></thead>
                  <tbody>
                    {porPagar.compras.map(c => (
                      <tr key={c.compra_id} className="border-b cursor-pointer" style={{ borderColor: 'var(--border)' }} onClick={() => toggleSel(c)}>
                        <td className="px-3 py-2.5"><input type="checkbox" checked={seleccion.has(c.compra_id)} onChange={() => toggleSel(c)} onClick={e => e.stopPropagation()} /></td>
                        <td className="px-3 py-2.5 max-w-[220px]" style={{ color: 'var(--text-primary)' }}>
                          <span className="block truncate font-medium">{c.acreedor || '—'}</span>
                          <span className="block text-[11px] truncate" style={{ color: 'var(--text-faint)' }}>{c.categoria || c.tipo_gasto}</span>
                        </td>
                        <td className="px-3 py-2.5 whitespace-nowrap text-xs" style={{ color: 'var(--text-muted)' }}>{c.numero_documento || '—'}</td>
                        <td className="px-3 py-2.5 whitespace-nowrap">
                          {c.fecha_vencimiento
                            ? <span className={c.bucket === 'vencido' ? 'text-red-400 font-medium' : ''} style={c.bucket !== 'vencido' ? { color: 'var(--text-muted)' } : {}}>{fmtDate(c.fecha_vencimiento)}</span>
                            : <span style={{ color: 'var(--text-faint)' }}>—</span>}
                        </td>
                        <td className="px-3 py-2.5 whitespace-nowrap" style={{ color: 'var(--text-muted)' }}>{fmtClp(c.monto_total_clp)}</td>
                        <td className="px-3 py-2.5 whitespace-nowrap" style={{ color: 'var(--text-muted)' }}>{fmtClp(c.monto_pagado_clp)}</td>
                        <td className="px-3 py-2.5 whitespace-nowrap font-semibold" style={{ color: 'var(--text-primary)' }}>{fmtClp(c.saldo_clp)}</td>
                        <td className="px-3 py-2.5 whitespace-nowrap text-xs">
                          <span className={`inline-flex px-2 py-0.5 rounded-full text-[11px] font-medium ${c.estado_pago === 'vencido' ? 'bg-red-500/10 text-red-400' : c.estado_pago === 'parcial' ? 'bg-amber-500/10 text-amber-400' : 'bg-gray-500/10'}`}
                            style={!['vencido', 'parcial'].includes(c.estado_pago) ? { color: 'var(--text-muted)' } : {}}>{c.estado_pago}</span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
          {seleccion.size > 0 && (
            <div className="sticky bottom-3 flex items-center justify-between gap-3 rounded-2xl border px-4 py-3 shadow-xl" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
              <p className="text-sm" style={{ color: 'var(--text-primary)' }}>
                <b>{seleccion.size}</b> compra{seleccion.size !== 1 ? 's' : ''} seleccionada{seleccion.size !== 1 ? 's' : ''} · saldo {fmtClp(totalSeleccion)}
              </p>
              <div className="flex items-center gap-2">
                <button onClick={() => setSeleccion(new Map())} className="btn-secondary text-xs">Limpiar</button>
                <button onClick={() => setModal({ type: 'pago' })} className="btn-primary flex items-center gap-2 text-xs"><Banknote className="w-3.5 h-3.5" /> Aprobar pago</button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Pestaña Adelantos (aprobación de adelantos de cliente) */}
      {tab === 'adelantos' && (
        !aprobaciones ? (
          <div className="flex items-center justify-center py-16"><Loader2 className="w-6 h-6 animate-spin text-brand-400" /></div>
        ) : (
          <div className="space-y-5">
            <div>
              <p className="text-[11px] font-semibold uppercase tracking-wider mb-2" style={{ color: 'var(--text-faint)' }}>
                Por aprobar (informados por Comercial)
              </p>
              {aprobaciones.por_aprobar.length === 0 ? (
                <div className="rounded-2xl border py-10 text-center" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
                  <CheckCircle2 className="w-9 h-9 mx-auto mb-2 text-emerald-500/40" />
                  <p className="text-sm" style={{ color: 'var(--text-muted)' }}>No hay adelantos esperando aprobación.</p>
                  <p className="text-xs mt-1" style={{ color: 'var(--text-faint)' }}>Comercial los informa en el Cierre de Venta (o desde Ventas de Contabilidad).</p>
                </div>
              ) : (
                <div className="space-y-2">
                  {aprobaciones.por_aprobar.map(a => (
                    <div key={a.id} className="flex items-center justify-between gap-3 rounded-xl border px-4 py-3" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
                      <div className="min-w-0">
                        {/* Contexto de la VENTA: quien aprueba tiene que poder validar que el
                            % pedido corresponda. RUT para identificar al cliente sin ambigüedad. */}
                        <p className="font-medium text-sm truncate" style={{ color: 'var(--text-primary)' }}>
                          {a.cliente || '—'}{a.rut_cliente ? ` · ${a.rut_cliente}` : ''}
                          {a.numero_oc ? ` · OC ${a.numero_oc}` : ''}{a.numero_cotizacion ? ` · COT ${a.numero_cotizacion}` : ''}
                        </p>
                        {/* Sin truncate: con el total de la venta y la fecha, la línea ya no
                            cabe en una sola y el dato para validar el % quedaría cortado. */}
                        <p className="text-xs leading-relaxed" style={{ color: 'var(--text-faint)' }}>
                          Esperado: <b>{fmtClp(a.monto_esperado)}</b>{a.pct ? ` (${a.pct}%)` : ''}
                          {totalVentaDerivado(a) !== null && <> de una venta de <b>{fmtClp(totalVentaDerivado(a)!)}</b> c/IVA</>}
                          {a.created_at ? ` · informado ${fmtDate(a.created_at)}` : ''}
                          {a.abono_sugerido ? <span className="text-emerald-500"> · hay un abono en la cartola que calza ({fmtDate(a.abono_sugerido.fecha)} · {fmtClp(a.abono_sugerido.monto)})</span> : ''}
                          {a.observaciones ? ` · ${a.observaciones}` : ''}
                        </p>
                      </div>
                      <button onClick={() => setModal({ type: 'aprobar-adelanto', adelanto: a })}
                        className="btn-primary flex items-center gap-2 text-xs shrink-0"><HandCoins className="w-3.5 h-3.5" /> Aprobar</button>
                    </div>
                  ))}
                </div>
              )}
            </div>
            <div>
              <p className="text-[11px] font-semibold uppercase tracking-wider mb-2" style={{ color: 'var(--text-faint)' }}>
                Aprobadas (últimas 50)
              </p>
              {aprobaciones.aprobadas.length === 0 ? (
                <p className="text-xs px-1" style={{ color: 'var(--text-faint)' }}>Todavía no hay adelantos aprobados.</p>
              ) : (
                <div className="rounded-2xl border overflow-hidden" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
                  <div className="overflow-x-auto">
                    <table className="w-full text-sm">
                      <thead><tr className="border-b" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
                        {['Cliente / OC', 'Monto', 'Aplicado', 'Fecha pago', 'Banco / operación', 'Estado'].map(h => <th key={h} className="text-left px-3 py-3 text-xs font-semibold uppercase tracking-wider whitespace-nowrap" style={{ color: 'var(--text-faint)' }}>{h}</th>)}
                      </tr></thead>
                      <tbody>
                        {aprobaciones.aprobadas.map(a => (
                          <tr key={a.id} className="border-b" style={{ borderColor: 'var(--border)' }}>
                            <td className="px-3 py-2.5 max-w-[240px]" style={{ color: 'var(--text-primary)' }}>
                              <span className="block truncate font-medium">{a.cliente || '—'}</span>
                              <span className="block text-[11px] truncate" style={{ color: 'var(--text-faint)' }}>{a.numero_oc ? `OC ${a.numero_oc}` : ''}{a.factura_anticipo_folio ? ` · respaldo Factura N° ${a.factura_anticipo_folio}` : ''}</span>
                            </td>
                            <td className="px-3 py-2.5 whitespace-nowrap font-semibold" style={{ color: 'var(--text-primary)' }}>{fmtClp(a.monto)}</td>
                            <td className="px-3 py-2.5 whitespace-nowrap text-xs" style={{ color: 'var(--text-muted)' }}>
                              {a.monto_aplicado >= a.monto - 1
                                ? <span className="text-emerald-500">Aplicado a factura</span>
                                : a.monto_aplicado > 0 ? `${fmtClp(a.monto_aplicado)} (queda ${fmtClp(a.pendiente_aplicar)})` : 'Esperando factura'}
                            </td>
                            <td className="px-3 py-2.5 whitespace-nowrap text-xs" style={{ color: 'var(--text-muted)' }}>{fmtDate(a.fecha_pago)}</td>
                            <td className="px-3 py-2.5 whitespace-nowrap text-xs" style={{ color: 'var(--text-muted)' }}>{a.banco || '—'}{a.numero_operacion ? ` · ${a.numero_operacion}` : ''}</td>
                            <td className="px-3 py-2.5 whitespace-nowrap text-xs">
                              {a.conciliado_banco
                                ? <span className="inline-flex items-center gap-1 text-emerald-500"><CheckCircle2 className="w-3.5 h-3.5" /> Conciliado</span>
                                : <span style={{ color: 'var(--text-faint)' }}>Sin conciliar</span>}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </div>
          </div>
        )
      )}

      {/* Pestaña Flujo de caja */}
      {tab === 'flujo' && (
        !flujo ? (
          <div className="flex items-center justify-center py-16"><Loader2 className="w-6 h-6 animate-spin text-brand-400" /></div>
        ) : (
          <div className="space-y-3">
            <div className="rounded-2xl border overflow-hidden" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead><tr className="border-b" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
                    <th className="text-left px-4 py-3 text-xs font-semibold uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>Flujo (NIC 7)</th>
                    {flujo.buckets.map(b => <th key={b} className="text-right px-4 py-3 text-xs font-semibold uppercase tracking-wider whitespace-nowrap" style={{ color: b === 'vencido' ? undefined : 'var(--text-faint)' }} >{BUCKET_LABEL[b]}</th>)}
                    {/* TOTAL de todas las ventanas: la tabla obligaba a sumar 6 columnas a mano
                        (el mismo total que ya muestra la pantalla de MonzaParts). */}
                    <th className="text-right px-4 py-3 text-xs font-semibold uppercase tracking-wider whitespace-nowrap border-l" style={{ color: 'var(--text-muted)', borderColor: 'var(--border)' }}>Total</th>
                  </tr></thead>
                  <tbody>
                    <tr className="border-b" style={{ borderColor: 'var(--border)' }}>
                      <td className="px-4 py-2.5 font-medium text-emerald-500">Por cobrar (facturas)</td>
                      {flujo.buckets.map(b => <td key={b} className="px-4 py-2.5 text-right whitespace-nowrap" style={{ color: 'var(--text-primary)' }}>{fmtClp(flujo.por_cobrar[b].monto)}<span className="text-[10px] ml-1" style={{ color: 'var(--text-faint)' }}>({flujo.por_cobrar[b].n})</span></td>)}
                      <td className="px-4 py-2.5 text-right whitespace-nowrap font-bold border-l text-emerald-500" style={{ borderColor: 'var(--border)' }}>
                        {fmtClp(sumaBuckets(flujo.buckets, flujo.por_cobrar).monto)}
                        <span className="text-[10px] ml-1 font-normal" style={{ color: 'var(--text-faint)' }}>({sumaBuckets(flujo.buckets, flujo.por_cobrar).n})</span>
                      </td>
                    </tr>
                    <tr className="border-b" style={{ borderColor: 'var(--border)' }}>
                      <td className="px-4 py-2.5 font-medium text-red-400">Por pagar (compras)</td>
                      {flujo.buckets.map(b => <td key={b} className="px-4 py-2.5 text-right whitespace-nowrap" style={{ color: 'var(--text-primary)' }}>{fmtClp(flujo.por_pagar[b].monto)}<span className="text-[10px] ml-1" style={{ color: 'var(--text-faint)' }}>({flujo.por_pagar[b].n})</span></td>)}
                      <td className="px-4 py-2.5 text-right whitespace-nowrap font-bold border-l text-red-400" style={{ borderColor: 'var(--border)' }}>
                        {fmtClp(sumaBuckets(flujo.buckets, flujo.por_pagar).monto)}
                        <span className="text-[10px] ml-1 font-normal" style={{ color: 'var(--text-faint)' }}>({sumaBuckets(flujo.buckets, flujo.por_pagar).n})</span>
                      </td>
                    </tr>
                    <tr>
                      <td className="px-4 py-2.5 font-semibold" style={{ color: 'var(--text-primary)' }}>Neto</td>
                      {flujo.buckets.map(b => (
                        <td key={b} className={`px-4 py-2.5 text-right whitespace-nowrap font-semibold ${flujo.neto[b] < 0 ? 'text-red-400' : 'text-emerald-500'}`}>{fmtClp(flujo.neto[b])}</td>
                      ))}
                      <td className={`px-4 py-2.5 text-right whitespace-nowrap font-bold border-l ${sumaNeto(flujo.buckets, flujo.neto) < 0 ? 'text-red-400' : 'text-emerald-500'}`} style={{ borderColor: 'var(--border)' }}>
                        {fmtClp(sumaNeto(flujo.buckets, flujo.neto))}
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>
            <p className="text-xs px-1" style={{ color: 'var(--text-faint)' }}>
              Las facturas <b>factorizadas</b> no se incluyen en "por cobrar": su caja pendiente es la retención del factor
              ({flujo.retenciones_factoring.n} operación{flujo.retenciones_factoring.n !== 1 ? 'es' : ''} vigente{flujo.retenciones_factoring.n !== 1 ? 's' : ''} · {fmtClp(flujo.retenciones_factoring.monto)} por liquidar).
              Los adelantos ya aplicados y los pagos parciales ya están descontados de los saldos.
            </p>
            {(flujo.adelantos_por_aprobar?.n > 0 || flujo.adelantos_recibidos_sin_aplicar?.n > 0) && (
              <p className="text-xs px-1" style={{ color: 'var(--text-faint)' }}>
                <b>Adelantos de clientes</b> (aparte de los buckets):
                {flujo.adelantos_por_aprobar?.n > 0 && <> {flujo.adelantos_por_aprobar.n} por aprobar ({fmtClp(flujo.adelantos_por_aprobar.monto)}) — aún no son plata segura.</>}
                {flujo.adelantos_recibidos_sin_aplicar?.n > 0 && <> {flujo.adelantos_recibidos_sin_aplicar.n} recibido{flujo.adelantos_recibidos_sin_aplicar.n !== 1 ? 's' : ''} sin aplicar ({fmtClp(flujo.adelantos_recibidos_sin_aplicar.monto)}) — plata YA en el banco; las próximas facturas nacerán con ese monto descontado.</>}
              </p>
            )}
          </div>
        )
      )}

      {/* Pestaña Movimientos */}
      {tab === 'movimientos' && (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-1.5">
            {[['', 'Todos'], ['pendiente', 'Pendientes'], ['conciliado', 'Conciliados']].map(([k, l]) => (
              <button key={k} onClick={() => setFiltroEstado(k)} className={`px-3 py-1.5 rounded-full text-xs font-medium border transition-all ${filtroEstado === k ? 'border-brand-500 bg-brand-500/10 text-brand-400' : 'border-transparent'}`} style={filtroEstado !== k ? { backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)', color: 'var(--text-muted)' } : {}}>{l}</button>
            ))}
            <span className="mx-1" style={{ color: 'var(--text-faint)' }}>·</span>
            {[['', 'Cargo y abono'], ['cargo', 'Cargos'], ['abono', 'Abonos']].map(([k, l]) => (
              <button key={k} onClick={() => setFiltroTipo(k)} className={`px-3 py-1.5 rounded-full text-xs font-medium border transition-all ${filtroTipo === k ? 'border-brand-500 bg-brand-500/10 text-brand-400' : 'border-transparent'}`} style={filtroTipo !== k ? { backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)', color: 'var(--text-muted)' } : {}}>{l}</button>
            ))}
          </div>
          {movs.length === 0 ? (
            <div className="rounded-2xl border py-12 text-center" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
              <p className="text-sm" style={{ color: 'var(--text-muted)' }}>Sin movimientos. Importa una cartola o agrega uno manual.</p>
            </div>
          ) : (
            <div className="rounded-2xl border overflow-hidden" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead><tr className="border-b" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
                    {['Fecha', 'Glosa', 'Referencia', 'Tipo', 'Monto', 'Estado', ''].map(h => <th key={h} className="text-left px-4 py-3 text-xs font-semibold uppercase tracking-wider whitespace-nowrap" style={{ color: 'var(--text-faint)' }}>{h}</th>)}
                  </tr></thead>
                  <tbody>
                    {movs.map(m => {
                      const tb = TIPO_BADGE[m.tipo] ?? { cls: 'bg-gray-500/10 text-gray-400', label: m.tipo }
                      const esCargo = m.tipo === 'cargo'
                      return (
                        <tr key={m.id} className="border-b" style={{ borderColor: 'var(--border)' }}>
                          <td className="px-4 py-2.5 whitespace-nowrap" style={{ color: 'var(--text-muted)' }}>{fmtDate(m.fecha)}</td>
                          <td className="px-4 py-2.5 max-w-[260px]" style={{ color: 'var(--text-primary)' }}>
                            <span className="block truncate">{m.glosa || '—'}</span>
                            {m.conciliado && m.destino && <span className="block text-[11px] text-emerald-500 truncate">{destinoLinea(m.destino)}</span>}
                          </td>
                          {/* Referencia del banco (N° de operación / documento de la cartola): es
                              el dato con que el tesorero busca el movimiento en el banco, y la
                              pantalla lo guardaba sin mostrarlo nunca. */}
                          <td className="px-4 py-2.5 max-w-[160px] text-xs font-mono" style={{ color: 'var(--text-muted)' }}>
                            <span className="block truncate" title={m.referencia || undefined}>{m.referencia || '—'}</span>
                          </td>
                          <td className="px-4 py-2.5"><span className={`inline-flex px-2 py-0.5 rounded-full text-[11px] font-medium ${tb.cls}`}>{tb.label}</span></td>
                          {/* Monto CON SIGNO: la cartola mezcla cargos y abonos y ambos venían en
                              positivo, así que la columna no se podía leer ni sumar de un vistazo. */}
                          <td className={`px-4 py-2.5 whitespace-nowrap font-semibold ${esCargo ? 'text-red-400' : 'text-emerald-500'}`}>
                            {esCargo ? '−' : '+'}{fmtClp(m.monto)}
                          </td>
                          <td className="px-4 py-2.5 whitespace-nowrap">{m.conciliado
                            ? <span className="inline-flex items-center gap-1 text-emerald-500 text-xs"><CheckCircle2 className="w-3.5 h-3.5" /> Conciliado</span>
                            : <span className="text-xs" style={{ color: 'var(--text-faint)' }}>Pendiente</span>}</td>
                          <td className="px-4 py-2.5 text-right whitespace-nowrap">
                            {m.conciliado
                              ? <button onClick={() => desconciliar(m)} className="inline-flex items-center gap-1 text-xs text-amber-500 hover:bg-amber-500/10 rounded px-2 py-1"><Unlink className="w-3.5 h-3.5" /> Desconciliar</button>
                              : <button onClick={() => eliminarMov(m)} className="inline-flex items-center gap-1 text-xs text-red-400 hover:bg-red-500/10 rounded px-2 py-1"><Trash2 className="w-3.5 h-3.5" /></button>}
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Pestaña Cuentas */}
      {tab === 'cuentas' && (
        <div className="rounded-2xl border overflow-hidden" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
          <table className="w-full text-sm">
            <thead><tr className="border-b" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
              {['Banco', 'Alias', 'N° cuenta', 'Moneda', 'Estado', ''].map(h => <th key={h} className="text-left px-4 py-3 text-xs font-semibold uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>{h}</th>)}
            </tr></thead>
            <tbody>
              {cuentas.map(c => (
                <tr key={c.id} className="border-b" style={{ borderColor: 'var(--border)' }}>
                  <td className="px-4 py-2.5 font-medium" style={{ color: 'var(--text-primary)' }}>{c.banco}</td>
                  <td className="px-4 py-2.5" style={{ color: 'var(--text-muted)' }}>{c.nombre || '—'}</td>
                  <td className="px-4 py-2.5 font-mono text-xs" style={{ color: 'var(--text-muted)' }}>{c.numero_cuenta || '—'}</td>
                  <td className="px-4 py-2.5" style={{ color: 'var(--text-muted)' }}>{c.moneda}</td>
                  <td className="px-4 py-2.5">{c.activo ? <span className="text-emerald-500 text-xs">Activa</span> : <span className="text-xs" style={{ color: 'var(--text-faint)' }}>Inactiva</span>}</td>
                  <td className="px-4 py-2.5 text-right"><button onClick={() => setModal({ type: 'cuenta', cuenta: c })} className="text-xs text-brand-400 hover:bg-brand-500/10 rounded px-2 py-1">Editar</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Modales */}
      {modal?.type === 'cuenta' && <CuentaModal cuenta={modal.cuenta ?? null} bancos={bancos} onClose={() => setModal(null)} onDone={loadCuentas} />}
      {modal?.type === 'import' && cuentaId != null && <ImportModal cuentaId={cuentaId} onClose={() => setModal(null)} onDone={refrescar} />}
      {modal?.type === 'manual' && cuentaId != null && <MovManualModal cuentaId={cuentaId} onClose={() => setModal(null)} onDone={refrescar} />}
      {modal?.type === 'pago' && comprasSeleccionadas.length > 0 && <PagoModal compras={comprasSeleccionadas} bancos={bancos} onClose={() => setModal(null)} onDone={refrescar} />}
      {modal?.type === 'aprobar-adelanto' && modal.adelanto && <AprobarAdelantoModal adelanto={modal.adelanto} bancos={bancos} onClose={() => setModal(null)} onDone={refrescar} />}
    </div>
  )
}
