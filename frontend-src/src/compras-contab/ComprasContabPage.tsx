// Página "Compras y Cuentas por Pagar": registra compras/gastos del día a día,
// los clasifica (costo de venta / operacional / no operacional / otros), lleva su
// condición de pago (contado/crédito) y su estado de pago, y muestra KPIs +
// antigüedad de cartera por pagar. Pestaña secundaria de SOLO LECTURA con los
// costos de embarque ya registrados en Embarques Pricing.
import { useState, useEffect, useCallback } from 'react'
import {
  Wallet, Plus, Search, AlertCircle, CheckCircle2, DollarSign,
  Loader2, RefreshCw, ChevronDown, ChevronUp, CreditCard, X, Trash2, Ban, Ship, Truck,
} from 'lucide-react'
import toast from 'react-hot-toast'
import { fmtClp, fmtDate, hoyLocal } from '../utils/format'
import { comprasContabAPI } from './api'
import type { CompraItemPayload } from './api'
import type { Compra, Antiguedad, Kpis, Catalogos, CostoEmbarque, PlanCuenta, OcNacional } from './types'

// ─── Mapas de presentación ──────────────────────────────────────────────────
const PAGO: Record<string, { cls: string; label: string }> = {
  pendiente:    { cls: 'bg-blue-500/10 text-blue-400 border-blue-400/20', label: 'Pendiente' },
  parcial:      { cls: 'bg-amber-500/10 text-amber-400 border-amber-400/20', label: 'Pago parcial' },
  pagado:       { cls: 'bg-emerald-500/10 text-emerald-500 border-emerald-500/20', label: 'Pagado' },
  vencido:      { cls: 'bg-red-500/10 text-red-400 border-red-400/20', label: 'Vencido' },
  anulado:      { cls: 'bg-gray-500/10 text-gray-400 border-gray-400/20', label: 'Anulado' },
}
const ESTADOS = ['', 'pendiente', 'parcial', 'pagado', 'vencido']
const ESTADO_LABEL: Record<string, string> = { '': 'Todos', pendiente: 'Pendiente', parcial: 'Parcial', pagado: 'Pagado', vencido: 'Vencido' }
const TIPOS = ['', 'cogs', 'gasto_operacional', 'gasto_no_operacional', 'otros']
const TIPO_LABEL: Record<string, string> = { '': 'Todos', cogs: 'Costo de venta', gasto_operacional: 'Operacional', gasto_no_operacional: 'No operacional', otros: 'Otros' }
const GASTO_TIPO_BADGE: Record<string, string> = {
  cogs: 'text-purple-400', gasto_operacional: 'text-brand-400',
  gasto_no_operacional: 'text-orange-400', otros: 'text-slate-400',
}

// ─── Modal genérico + helpers de formulario ─────────────────────────────────
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
const inputCls = 'w-full px-3 py-2 rounded-lg border text-sm focus:outline-none focus:ring-2 focus:ring-brand-500/40'
const inputStyle = { backgroundColor: 'var(--surface-200)', borderColor: 'var(--border)', color: 'var(--text-primary)' } as React.CSSProperties
function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (<label className="block"><span className="block text-[11px] font-semibold uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>{label}</span>{children}</label>)
}

// ─── Modal: registrar compra ────────────────────────────────────────────────
function RegistrarCompraModal({ catalogos, onClose, onDone }: { catalogos: Catalogos | null; onClose: () => void; onDone: () => void }) {
  const [tipoGasto, setTipoGasto] = useState('gasto_operacional')
  const [categoria, setCategoria] = useState('')
  const [cuentaId, setCuentaId] = useState<number | ''>('')
  const [esAnticipo, setEsAnticipo] = useState(false)
  const [proveedorId, setProveedorId] = useState<number | ''>('')
  const [acreedor, setAcreedor] = useState('')
  const [rut, setRut] = useState('')
  const [fecha, setFecha] = useState(hoyLocal())
  const [numDoc, setNumDoc] = useState('')
  const [tipoDoc, setTipoDoc] = useState('factura')
  const [descripcion, setDescripcion] = useState('')
  const [referencia, setReferencia] = useState('')
  const [moneda, setMoneda] = useState('CLP')
  const [tc, setTc] = useState('')
  const [neto, setNeto] = useState('')
  const [afectoIva, setAfectoIva] = useState(true)
  const [condicion, setCondicion] = useState<'contado' | 'credito'>('credito')
  const [plazo, setPlazo] = useState('30')
  const [pagoMedio, setPagoMedio] = useState('transferencia')
  const [pagoBanco, setPagoBanco] = useState('')
  const [pagoFechaBanco, setPagoFechaBanco] = useState(hoyLocal())
  const [saving, setSaving] = useState(false)

  // Compra NACIONAL con detalle de ítems: la factura ES el costo de esos repuestos
  // (neto CLP por ítem; el IVA es crédito fiscal, no capitaliza). El backend valida
  // que la cantidad costeada no supere lo recibido en bodega y que Σ ≤ neto.
  const [origenTipo, setOrigenTipo] = useState<'gasto' | 'nacional'>('gasto')
  const [ocNacionales, setOcNacionales] = useState<OcNacional[]>([])
  const [ocpSel, setOcpSel] = useState<number | ''>('')
  const [lineItems, setLineItems] = useState<Record<number, { incluir: boolean; cantidad: string; precio_unit: string }>>({})
  const esNacional = origenTipo === 'nacional'
  const ocSel = ocNacionales.find(o => o.oc_proveedor_id === ocpSel) || null

  // Cuenta sugerida: nacional → Existencias (1.3.01) vía NACIONAL|cogs; si no, MANUAL|tipo.
  useEffect(() => {
    const key = esNacional ? 'NACIONAL|cogs' : `MANUAL|${tipoGasto}`
    const def = catalogos?.cuenta_default_por_tipo?.[key]
    if (def) setCuentaId(def)
  }, [tipoGasto, esNacional, catalogos])

  // Nacional fuerza costo de venta + CLP (el IVA no capitaliza en la compra nacional).
  useEffect(() => {
    if (esNacional) { setTipoGasto('cogs'); setMoneda('CLP') }
  }, [esNacional])

  // Cargar OC nacionales al entrar al modo nacional (una sola vez).
  useEffect(() => {
    if (esNacional && ocNacionales.length === 0) {
      comprasContabAPI.ocNacionales()
        .then(({ data }) => setOcNacionales(data.ocs))
        .catch((e: any) => toast.error(e?.response?.data?.detail || 'No se pudieron cargar las OC nacionales'))
    }
  }, [esNacional])

  // Al elegir OC nacional: precarga ítems (cantidad = disponible a costear) y el acreedor.
  useEffect(() => {
    if (!ocSel) { setLineItems({}); return }
    const init: Record<number, { incluir: boolean; cantidad: string; precio_unit: string }> = {}
    ocSel.items.forEach(it => {
      init[it.item_cotizacion_id] = {
        incluir: it.disponible_costear > 0,
        cantidad: it.disponible_costear > 0 ? String(it.disponible_costear) : '',
        precio_unit: '',
      }
    })
    setLineItems(init)
    if (ocSel.proveedor) setAcreedor(ocSel.proveedor)
  }, [ocpSel, ocNacionales])
  const cuentasPorClase: Record<string, PlanCuenta[]> = {}
  for (const c of catalogos?.plan_cuentas || []) {
    const k = c.clase || 'Otras'
    if (!cuentasPorClase[k]) cuentasPorClase[k] = []
    cuentasPorClase[k].push(c)
  }

  const netoN = Number(neto) || 0
  const ivaN = afectoIva ? Math.round(netoN * 0.19) : 0
  const totalN = netoN + ivaN
  const tcN = moneda === 'CLP' ? 1 : (Number(tc) || 0)
  const totalClp = Math.round(totalN * tcN)

  // Σ de las líneas costeadas incluidas (cantidad × costo unit neto CLP).
  const sumaLineas = ocSel ? ocSel.items.reduce((acc, it) => {
    const li = lineItems[it.item_cotizacion_id]
    if (!li || !li.incluir) return acc
    return acc + (Number(li.cantidad) || 0) * (Number(li.precio_unit) || 0)
  }, 0) : 0
  const sumaExcedeNeto = esNacional && netoN > 0 && Math.round(sumaLineas) > netoN + 1

  const onProveedor = (id: number | '') => {
    setProveedorId(id)
    const p = catalogos?.proveedores.find(x => x.id === id)
    if (p) { setAcreedor(p.nombre); if (p.moneda) setMoneda(p.moneda) }
  }

  const submit = async () => {
    if (!acreedor.trim()) { toast.error('Indica el proveedor/acreedor'); return }
    if (netoN <= 0) { toast.error('El monto neto debe ser mayor a 0'); return }
    if (moneda !== 'CLP' && tcN <= 0) { toast.error('Indica el tipo de cambio'); return }

    // Detalle por ítem de la compra NACIONAL.
    let items: CompraItemPayload[] | undefined
    if (esNacional) {
      if (!ocpSel) { toast.error('Selecciona la OC-Proveedor nacional'); return }
      const incluidos = (ocSel?.items || []).filter(it => {
        const li = lineItems[it.item_cotizacion_id]
        return li && li.incluir && Number(li.cantidad) > 0
      })
      if (incluidos.length === 0) { toast.error('Agrega al menos un ítem con cantidad'); return }
      if (sumaExcedeNeto) { toast.error('La suma de líneas costeadas supera el neto de la factura'); return }
      items = incluidos.map(it => {
        const li = lineItems[it.item_cotizacion_id]
        return {
          item_cotizacion_id: it.item_cotizacion_id,
          oc_proveedor_item_id: it.oc_proveedor_item_id ?? undefined,
          numero_parte: it.numero_parte,
          descripcion: it.descripcion,
          cantidad: Number(li.cantidad),
          precio_unit: Number(li.precio_unit) || 0,
        }
      })
    }

    setSaving(true)
    try {
      await comprasContabAPI.crear({
        tipo_gasto: tipoGasto, categoria: categoria || undefined,
        cuenta_contable_id: cuentaId || undefined, es_anticipo: esAnticipo,
        origen: esNacional ? 'NACIONAL' : undefined,
        proveedor_id: proveedorId || undefined, acreedor: acreedor || undefined, proveedor_rut: rut || undefined,
        fecha, referencia: referencia || undefined, descripcion: descripcion || undefined,
        numero_documento: numDoc || undefined, tipo_doc: tipoDoc,
        moneda, tc: tcN, monto_neto: netoN, afecto_iva: afectoIva,
        condicion_pago: condicion, plazo_dias: condicion === 'credito' && plazo ? Number(plazo) : undefined,
        oc_proveedor_id: esNacional && ocpSel ? Number(ocpSel) : undefined,
        items,
        pago: condicion === 'contado'
          ? { medio: pagoMedio, banco: pagoBanco || undefined, fecha, fecha_mov_bancario: pagoFechaBanco || fecha }
          : undefined,
      })
      toast.success('Compra registrada'); onDone(); onClose()
    } catch (e: any) { toast.error(e?.response?.data?.detail || 'No se pudo registrar la compra') } finally { setSaving(false) }
  }

  return (
    <Modal title="Registrar compra / gasto" wide onClose={onClose}>
      {/* Tipo de registro: gasto/servicio o compra nacional con detalle por ítem */}
      <div className="flex flex-wrap items-center gap-2">
        {([['gasto', 'Gasto / servicio', Wallet], ['nacional', 'Compra nacional (detalle de ítems)', Truck]] as const).map(([val, label, Icon]) => (
          <button key={val} type="button" onClick={() => setOrigenTipo(val)}
            className={`px-3 py-1.5 rounded-full text-xs font-medium border transition-all flex items-center gap-1.5 ${origenTipo === val ? 'border-brand-500 bg-brand-500/10 text-brand-400' : 'border-transparent'}`}
            style={origenTipo !== val ? { backgroundColor: 'var(--surface-200)', borderColor: 'var(--border)', color: 'var(--text-muted)' } : {}}>
            <Icon className="w-3.5 h-3.5" /> {label}
          </button>
        ))}
      </div>

      {esNacional && (
        <div className="rounded-xl border p-3 space-y-3" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
          <Field label="OC-Proveedor nacional">
            <select className={inputCls} style={inputStyle} value={ocpSel} onChange={e => setOcpSel(e.target.value ? Number(e.target.value) : '')}>
              <option value="">— Selecciona OC nacional —</option>
              {ocNacionales.map(o => (
                <option key={o.oc_proveedor_id} value={o.oc_proveedor_id}>
                  {(o.numero_oc || o.numero || `OCP #${o.oc_proveedor_id}`)} — {o.proveedor || 'Sin proveedor'}
                </option>
              ))}
            </select>
          </Field>
          {ocNacionales.length === 0 ? (
            <p className="text-[11px]" style={{ color: 'var(--text-faint)' }}>
              No hay OC nacionales con ítems. Crea una en el Panel de Compras (origen Nacional) y registra su entrega en Seguimiento antes de costear.
            </p>
          ) : ocSel && (
            <>
              <div className="rounded-lg border overflow-hidden" style={{ borderColor: 'var(--border)' }}>
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead>
                      <tr style={{ backgroundColor: 'var(--surface-100)', borderBottom: '1px solid var(--border)' }}>
                        <th className="px-2 py-2 w-8"></th>
                        {['N° Parte', 'Descripción', 'Recibido', 'Disp. costear', 'Cantidad', 'Costo unit CLP', 'Subtotal'].map(h => (
                          <th key={h} className="px-2 py-2 text-left font-semibold uppercase tracking-wider" style={{ color: 'var(--text-faint)' }}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {ocSel.items.map(it => {
                        const li = lineItems[it.item_cotizacion_id] || { incluir: false, cantidad: '', precio_unit: '' }
                        const sub = (Number(li.cantidad) || 0) * (Number(li.precio_unit) || 0)
                        const sinDisp = it.disponible_costear <= 0
                        return (
                          <tr key={it.item_cotizacion_id} style={{ borderBottom: '1px solid var(--border)', opacity: li.incluir ? 1 : 0.5 }}>
                            <td className="px-2 py-1.5">
                              <input type="checkbox" checked={li.incluir} disabled={sinDisp}
                                onChange={() => setLineItems(prev => ({ ...prev, [it.item_cotizacion_id]: { ...(prev[it.item_cotizacion_id] || { cantidad: '', precio_unit: '' }), incluir: !prev[it.item_cotizacion_id]?.incluir } }))} />
                            </td>
                            <td className="px-2 py-1.5 font-mono text-brand-400 font-semibold whitespace-nowrap">{it.numero_parte || '—'}</td>
                            <td className="px-2 py-1.5 max-w-[150px] truncate" style={{ color: 'var(--text-primary)' }} title={it.descripcion || ''}>{it.descripcion || '—'}</td>
                            <td className="px-2 py-1.5 text-center" style={{ color: 'var(--text-muted)' }}>{it.recibido}</td>
                            <td className="px-2 py-1.5 text-center font-semibold" style={{ color: sinDisp ? 'var(--text-faint)' : 'var(--text-primary)' }} title={sinDisp ? 'Registra primero la recepción nacional en Seguimiento' : undefined}>{it.disponible_costear}</td>
                            <td className="px-2 py-1.5">
                              <input type="number" min="0" step="any" className="w-20 px-2 py-1 rounded border text-xs" style={inputStyle}
                                value={li.cantidad} disabled={!li.incluir}
                                onChange={e => setLineItems(prev => ({ ...prev, [it.item_cotizacion_id]: { ...(prev[it.item_cotizacion_id] || { incluir: true, precio_unit: '' }), cantidad: e.target.value } }))} />
                            </td>
                            <td className="px-2 py-1.5">
                              <input type="number" min="0" step="any" className="w-24 px-2 py-1 rounded border text-xs" style={inputStyle}
                                value={li.precio_unit} disabled={!li.incluir} placeholder="neto CLP"
                                onChange={e => setLineItems(prev => ({ ...prev, [it.item_cotizacion_id]: { ...(prev[it.item_cotizacion_id] || { incluir: true, cantidad: '' }), precio_unit: e.target.value } }))} />
                            </td>
                            <td className="px-2 py-1.5 text-right font-mono whitespace-nowrap" style={{ color: 'var(--text-muted)' }}>{sub > 0 ? fmtClp(sub) : '—'}</td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              </div>
              <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
                <span style={{ color: 'var(--text-muted)' }}>
                  Σ líneas: <b className="text-brand-400">{fmtClp(sumaLineas)}</b>
                  <span className="mx-1.5">·</span>
                  Neto factura: <b style={{ color: 'var(--text-primary)' }}>{fmtClp(netoN)}</b>
                </span>
                {sumaExcedeNeto ? (
                  <span className="text-red-400 font-semibold flex items-center gap-1"><AlertCircle className="w-3.5 h-3.5" /> Σ supera el neto de la factura</span>
                ) : (
                  <button type="button" onClick={() => setNeto(String(Math.round(sumaLineas)))}
                    className="text-[11px] px-2 py-1 rounded-lg border border-brand-400/40 text-brand-400 hover:bg-brand-500/10 transition-colors font-semibold">
                    Usar Σ como neto
                  </button>
                )}
              </div>
              <p className="text-[11px]" style={{ color: 'var(--text-faint)' }}>
                El costo por ítem es el <b>neto en CLP</b> (el IVA es crédito fiscal, no capitaliza). La cantidad costeada no puede superar lo recibido en bodega.
              </p>
            </>
          )}
        </div>
      )}

      <div className="grid sm:grid-cols-2 gap-3">
        {esNacional ? (
          <Field label="Tipo de gasto">
            <div className={inputCls} style={{ ...inputStyle, opacity: 0.7 }}>Costo de venta (nacional)</div>
          </Field>
        ) : (
          <Field label="Tipo de gasto">
            <select className={inputCls} style={inputStyle} value={tipoGasto} onChange={e => setTipoGasto(e.target.value)}>
              {(catalogos?.tipos_gasto || []).map(t => <option key={t.value} value={t.value}>{t.label}</option>)}
            </select>
          </Field>
        )}
        <Field label="Categoría">
          <input className={inputCls} style={inputStyle} list="cat-sugeridas" value={categoria} onChange={e => setCategoria(e.target.value)} placeholder="Ej. Flete internacional" />
          <datalist id="cat-sugeridas">{(catalogos?.categorias_sugeridas || []).map(c => <option key={c} value={c} />)}</datalist>
        </Field>
        <div className="sm:col-span-2">
          <Field label="Cuenta contable (imputación del neto)">
            <select className={inputCls} style={inputStyle} value={cuentaId} onChange={e => setCuentaId(e.target.value ? Number(e.target.value) : '')}>
              <option value="">— Selecciona cuenta —</option>
              {Object.entries(cuentasPorClase).map(([clase, cuentas]) => (
                <optgroup key={clase} label={clase}>
                  {cuentas.map(c => <option key={c.id} value={c.id}>{c.codigo} — {c.nombre}</option>)}
                </optgroup>
              ))}
            </select>
            <p className="text-[11px] mt-1" style={{ color: 'var(--text-faint)' }}>Se sugiere según el tipo de gasto; puedes cambiarla. El IVA se imputa automático (1.4.01/1.4.02).</p>
          </Field>
          <label className="flex items-center gap-2 mt-2 text-xs" style={{ color: 'var(--text-muted)' }}>
            <input type="checkbox" checked={esAnticipo} onChange={e => setEsAnticipo(e.target.checked)} /> Es anticipo a proveedor extranjero (NIC 21 · no monetario)
          </label>
        </div>
        <Field label="Proveedor (catálogo)">
          <select className={inputCls} style={inputStyle} value={proveedorId} onChange={e => onProveedor(e.target.value ? Number(e.target.value) : '')}>
            <option value="">— Sin catálogo —</option>
            {(catalogos?.proveedores || []).map(p => <option key={p.id} value={p.id}>{p.nombre}</option>)}
          </select>
        </Field>
        <Field label="Acreedor / proveedor (nombre)">
          <input className={inputCls} style={inputStyle} value={acreedor} onChange={e => setAcreedor(e.target.value)} placeholder="Nombre del proveedor" />
        </Field>
        <Field label="RUT proveedor"><input className={inputCls} style={inputStyle} value={rut} onChange={e => setRut(e.target.value)} placeholder="76.xxx.xxx-x" /></Field>
        <Field label="N° documento (factura/boleta)"><input className={inputCls} style={inputStyle} value={numDoc} onChange={e => setNumDoc(e.target.value)} /></Field>
        <Field label="Tipo de documento">
          <select className={inputCls} style={inputStyle} value={tipoDoc} onChange={e => setTipoDoc(e.target.value)}>
            <option value="factura">Factura</option><option value="boleta">Boleta</option>
            <option value="nota">Nota</option><option value="recibo">Recibo</option><option value="sin_documento">Sin documento</option>
          </select>
        </Field>
        <Field label="Fecha"><input type="date" className={inputCls} style={inputStyle} value={fecha} onChange={e => setFecha(e.target.value)} /></Field>
        <Field label="Referencia (ej. Emb 1)"><input className={inputCls} style={inputStyle} value={referencia} onChange={e => setReferencia(e.target.value)} /></Field>
        <Field label="Descripción"><input className={inputCls} style={inputStyle} value={descripcion} onChange={e => setDescripcion(e.target.value)} placeholder="Glosa del gasto" /></Field>
      </div>

      {/* Montos */}
      <div className="rounded-xl border p-3 mt-1" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
        <div className="grid sm:grid-cols-3 gap-3">
          <Field label="Moneda">
            <select className={inputCls} style={inputStyle} value={moneda} disabled={esNacional} onChange={e => setMoneda(e.target.value)}>
              <option value="CLP">CLP</option><option value="USD">USD</option><option value="EUR">EUR</option>
            </select>
          </Field>
          {moneda !== 'CLP' && <Field label="Tipo de cambio (a CLP)"><input type="number" className={inputCls} style={inputStyle} value={tc} onChange={e => setTc(e.target.value)} placeholder="950" /></Field>}
          <Field label={`Monto neto (${moneda})`}><input type="number" className={inputCls} style={inputStyle} value={neto} onChange={e => setNeto(e.target.value)} /></Field>
        </div>
        <label className="flex items-center gap-2 mt-2 text-xs" style={{ color: 'var(--text-muted)' }}>
          <input type="checkbox" checked={afectoIva} onChange={e => setAfectoIva(e.target.checked)} /> Afecto a IVA (19%)
        </label>
        <div className="flex flex-wrap gap-x-5 gap-y-1 mt-2 text-xs" style={{ color: 'var(--text-muted)' }}>
          <span>IVA: <b style={{ color: 'var(--text-primary)' }}>{moneda} {Math.round(ivaN).toLocaleString('es-CL')}</b></span>
          <span>Total: <b style={{ color: 'var(--text-primary)' }}>{moneda} {Math.round(totalN).toLocaleString('es-CL')}</b></span>
          <span>Total en CLP: <b className="text-brand-400">{fmtClp(totalClp)}</b></span>
        </div>
      </div>

      {/* Condición de pago */}
      <div className="flex items-center gap-2 mt-1">
        {(['contado', 'credito'] as const).map(c => (
          <button key={c} type="button" onClick={() => setCondicion(c)}
            className={`px-3 py-1.5 rounded-full text-xs font-medium border transition-all ${condicion === c ? 'border-brand-500 bg-brand-500/10 text-brand-400' : 'border-transparent'}`}
            style={condicion !== c ? { backgroundColor: 'var(--surface-200)', borderColor: 'var(--border)', color: 'var(--text-muted)' } : {}}>
            {c === 'contado' ? 'Contado (pago ahora)' : 'Crédito (pago después)'}
          </button>
        ))}
      </div>
      {condicion === 'credito' ? (
        <Field label="Plazo (días)"><input type="number" className={inputCls} style={inputStyle} value={plazo} onChange={e => setPlazo(e.target.value)} /></Field>
      ) : (
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
          <Field label="Medio de pago">
            <select className={inputCls} style={inputStyle} value={pagoMedio} onChange={e => setPagoMedio(e.target.value)}>
              <option value="transferencia">Transferencia</option><option value="cheque">Cheque</option>
              <option value="efectivo">Efectivo</option><option value="tarjeta">Tarjeta</option>
            </select>
          </Field>
          <Field label="Banco"><input className={inputCls} style={inputStyle} value={pagoBanco} onChange={e => setPagoBanco(e.target.value)} /></Field>
          <Field label="Fecha en el banco (cartola)"><input type="date" className={inputCls} style={inputStyle} value={pagoFechaBanco} onChange={e => setPagoFechaBanco(e.target.value)} /></Field>
        </div>
      )}

      <button onClick={submit} disabled={saving} className="btn-primary w-full flex items-center justify-center gap-2">
        {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Wallet className="w-4 h-4" />} Registrar compra
      </button>
    </Modal>
  )
}

// ─── Modal: registrar pago ──────────────────────────────────────────────────
function PagoCompraModal({ compra, onClose, onDone }: { compra: Compra; onClose: () => void; onDone: () => void }) {
  const hoy = hoyLocal()
  const [monto, setMonto] = useState(String(Math.round(compra.saldo_clp)))
  const [fecha, setFecha] = useState(hoy)
  const [fechaBanco, setFechaBanco] = useState(hoy)
  const [medio, setMedio] = useState('transferencia')
  const [banco, setBanco] = useState('')
  const [op, setOp] = useState('')
  const [saving, setSaving] = useState(false)
  const submit = async () => {
    if (!monto || Number(monto) <= 0) { toast.error('Monto inválido'); return }
    setSaving(true)
    try {
      await comprasContabAPI.registrarPago(compra.id, {
        monto_clp: Number(monto), fecha, fecha_mov_bancario: fechaBanco || undefined,
        medio, banco: banco || undefined, numero_operacion: op || undefined,
      })
      toast.success('Pago registrado'); onDone(); onClose()
    } catch (e: any) { toast.error(e?.response?.data?.detail || 'Error al registrar el pago') } finally { setSaving(false) }
  }
  return (
    <Modal title={`Registrar pago · ${compra.numero_documento || '#' + compra.id}`} onClose={onClose}>
      <p className="text-xs" style={{ color: 'var(--text-muted)' }}>Saldo pendiente: <span className="font-bold text-amber-400">{fmtClp(compra.saldo_clp)}</span></p>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Monto (CLP)"><input type="number" className={inputCls} style={inputStyle} value={monto} onChange={e => setMonto(e.target.value)} /></Field>
        <Field label="Fecha del pago"><input type="date" className={inputCls} style={inputStyle} value={fecha} onChange={e => setFecha(e.target.value)} /></Field>
        <Field label="Medio">
          <select className={inputCls} style={inputStyle} value={medio} onChange={e => setMedio(e.target.value)}>
            <option value="transferencia">Transferencia</option><option value="cheque">Cheque</option>
            <option value="efectivo">Efectivo</option><option value="tarjeta">Tarjeta</option>
          </select>
        </Field>
        <Field label="Banco"><input className={inputCls} style={inputStyle} value={banco} onChange={e => setBanco(e.target.value)} /></Field>
        <Field label="Fecha en el banco (cartola)"><input type="date" className={inputCls} style={inputStyle} value={fechaBanco} onChange={e => setFechaBanco(e.target.value)} /></Field>
        <Field label="N° operación"><input className={inputCls} style={inputStyle} value={op} onChange={e => setOp(e.target.value)} /></Field>
      </div>
      <p className="text-[11px]" style={{ color: 'var(--text-faint)' }}>La <b>fecha en el banco</b> es la del movimiento en tu cartola; con ella se cruzará este pago en Conciliación Bancaria. Puedes dejarla vacía y completarla después.</p>
      <button onClick={submit} disabled={saving} className="btn-primary w-full flex items-center justify-center gap-2">
        {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <CheckCircle2 className="w-4 h-4" />} Registrar pago
      </button>
    </Modal>
  )
}

// ─── Modal: pago consolidado (un movimiento, varios gastos) ──────────────────
function PagoConsolidadoModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [pendientes, setPendientes] = useState<Compra[]>([])
  const [sel, setSel] = useState<Record<number, string>>({})
  const [fecha, setFecha] = useState(hoyLocal())
  const [medio, setMedio] = useState('transferencia')
  const [banco, setBanco] = useState('')
  const [op, setOp] = useState('')
  const [beneficiario, setBeneficiario] = useState('')
  const [saving, setSaving] = useState(false)
  useEffect(() => {
    comprasContabAPI.list({ page_size: 200 })
      .then(({ data }) => setPendientes(data.compras.filter(c => c.saldo_clp > 0 && !c.anulado)))
      .catch(() => {})
  }, [])
  const toggle = (c: Compra) => setSel(s => {
    const n = { ...s }
    if (c.id in n) delete n[c.id]; else n[c.id] = String(Math.round(c.saldo_clp))
    return n
  })
  const total = Object.values(sel).reduce((a, v) => a + (Number(v) || 0), 0)
  const submit = async () => {
    const detalles = Object.entries(sel).filter(([, v]) => Number(v) > 0).map(([id, v]) => ({ compra_id: Number(id), monto_clp: Number(v) }))
    if (!detalles.length) { toast.error('Selecciona al menos un gasto'); return }
    setSaving(true)
    try {
      await comprasContabAPI.crearEgresoConsolidado({
        fecha, medio, banco: banco || undefined, numero_operacion: op || undefined,
        beneficiario: beneficiario || undefined, fecha_mov_bancario: fecha, detalles,
      })
      toast.success('Egreso consolidado registrado'); onDone(); onClose()
    } catch (e: any) { toast.error(e?.response?.data?.detail || 'No se pudo registrar el egreso') } finally { setSaving(false) }
  }
  return (
    <Modal title="Pago consolidado · un movimiento paga varios gastos" wide onClose={onClose}>
      <p className="text-xs" style={{ color: 'var(--text-muted)' }}>Selecciona los gastos que pagaste en <b>una sola transferencia</b>. Luego, en Conciliación Bancaria, este egreso se cruza con ese único movimiento del banco.</p>
      <div className="max-h-64 overflow-y-auto rounded-lg border" style={{ borderColor: 'var(--border)' }}>
        {pendientes.length === 0 ? (
          <p className="p-3 text-xs" style={{ color: 'var(--text-faint)' }}>No hay gastos pendientes de pago.</p>
        ) : pendientes.map(c => (
          <label key={c.id} className="flex items-center gap-2 px-3 py-2 border-b text-xs cursor-pointer" style={{ borderColor: 'var(--border)' }}>
            <input type="checkbox" checked={c.id in sel} onChange={() => toggle(c)} />
            <span className="flex-1 min-w-0 truncate" style={{ color: 'var(--text-primary)' }}>{c.acreedor || '—'} · <span className="font-mono text-brand-400">{c.numero_documento || `#${c.id}`}</span> · saldo {fmtClp(c.saldo_clp)}</span>
            {c.id in sel && <input type="number" className="w-28 px-2 py-1 rounded border text-xs" style={inputStyle} value={sel[c.id]} onClick={e => e.preventDefault()} onChange={e => setSel(s => ({ ...s, [c.id]: e.target.value }))} />}
          </label>
        ))}
      </div>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Fecha"><input type="date" className={inputCls} style={inputStyle} value={fecha} onChange={e => setFecha(e.target.value)} /></Field>
        <Field label="Medio">
          <select className={inputCls} style={inputStyle} value={medio} onChange={e => setMedio(e.target.value)}>
            <option value="transferencia">Transferencia</option><option value="cheque">Cheque</option>
            <option value="efectivo">Efectivo</option><option value="tarjeta">Tarjeta</option>
          </select>
        </Field>
        <Field label="Banco"><input className={inputCls} style={inputStyle} value={banco} onChange={e => setBanco(e.target.value)} /></Field>
        <Field label="N° operación"><input className={inputCls} style={inputStyle} value={op} onChange={e => setOp(e.target.value)} /></Field>
      </div>
      <Field label="Beneficiario"><input className={inputCls} style={inputStyle} value={beneficiario} onChange={e => setBeneficiario(e.target.value)} /></Field>
      <button onClick={submit} disabled={saving} className="btn-primary w-full flex items-center justify-center gap-2">
        {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <CreditCard className="w-4 h-4" />} Registrar egreso · {fmtClp(total)}
      </button>
    </Modal>
  )
}

// ─── Fila de compra (expandible) ────────────────────────────────────────────
function CompraRow({ c, onChanged, onPagar }: { c: Compra; onChanged: () => void; onPagar: (c: Compra) => void }) {
  const [open, setOpen] = useState(false)
  const pago = PAGO[c.estado_pago] ?? { cls: 'bg-gray-500/10 text-gray-400', label: c.estado_pago }
  const pct = Math.min(100, c.monto_total_clp > 0 ? Math.round((c.monto_pagado_clp / c.monto_total_clp) * 100) : 0)
  const delPago = async (id: number) => {
    if (!confirm('¿Revertir este pago?')) return
    try { await comprasContabAPI.eliminarPago(c.id, id); toast.success('Pago revertido'); onChanged() }
    catch (e: any) { toast.error(e?.response?.data?.detail || 'Error') }
  }
  const updFechaBanco = async (pagoId: number, value: string) => {
    try { await comprasContabAPI.actualizarPago(c.id, pagoId, { fecha_mov_bancario: value }); onChanged() }
    catch (e: any) { toast.error(e?.response?.data?.detail || 'Error al guardar la fecha del banco') }
  }
  const anular = async () => {
    const motivo = prompt('Motivo de anulación (opcional):') ?? undefined
    if (motivo === undefined && !confirm('¿Anular esta compra?')) return
    try { await comprasContabAPI.anular(c.id, motivo); toast.success('Compra anulada'); onChanged() }
    catch (e: any) { toast.error(e?.response?.data?.detail || 'Error') }
  }
  return (
    <>
      <tr className="hover:bg-[var(--surface-200)] transition-colors cursor-pointer" onClick={() => setOpen(o => !o)}>
        <td className="px-4 py-3 font-mono font-semibold text-brand-400 whitespace-nowrap">
          <span className="inline-flex items-center gap-1">{open ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}{c.numero_documento || `#${c.id}`}</span>
          {c.origen === 'EMBARQUE' && <span className="ml-1 text-[10px] text-cyan-400">·emb</span>}
        </td>
        <td className="px-4 py-3 font-medium max-w-[180px] truncate" style={{ color: 'var(--text-primary)' }}>{c.acreedor || '—'}</td>
        <td className="px-4 py-3 whitespace-nowrap"><span className={`text-xs font-medium ${GASTO_TIPO_BADGE[c.tipo_gasto] || ''}`}>{c.tipo_gasto_label}</span>{c.categoria && <span className="block text-[10px]" style={{ color: 'var(--text-faint)' }}>{c.categoria}</span>}</td>
        <td className="px-4 py-3 whitespace-nowrap" style={{ color: 'var(--text-muted)' }}>{fmtDate(c.fecha)}</td>
        <td className="px-4 py-3 font-semibold whitespace-nowrap" style={{ color: 'var(--text-primary)' }}>{fmtClp(c.monto_total_clp)}{c.moneda !== 'CLP' && <span className="block text-[10px] font-normal" style={{ color: 'var(--text-faint)' }}>{c.moneda} {Math.round(c.monto_total).toLocaleString('es-CL')}</span>}</td>
        <td className="px-4 py-3">
          <div className="flex items-center gap-2">
            <div className="w-16 h-1.5 rounded-full" style={{ backgroundColor: 'var(--surface-300)' }}><div className="h-full rounded-full bg-emerald-500" style={{ width: `${pct}%` }} /></div>
            <span className="text-xs text-emerald-500 font-medium">{pct}%</span>
          </div>
        </td>
        <td className="px-4 py-3 whitespace-nowrap">
          <span className={c.semaforo === 'vencida' ? 'text-red-400 font-medium' : ''} style={c.semaforo !== 'vencida' ? { color: 'var(--text-muted)' } : {}}>
            {fmtDate(c.fecha_vencimiento)}
            {c.dias_vencimiento != null && c.saldo_clp > 0 && <span className="ml-1 text-xs">({c.dias_vencimiento < 0 ? `${Math.abs(c.dias_vencimiento)}d venc.` : `${c.dias_vencimiento}d`})</span>}
          </span>
        </td>
        <td className="px-4 py-3"><span className={`inline-flex items-center px-2.5 py-1 rounded-full text-xs font-medium border ${pago.cls}`}>{pago.label}</span></td>
      </tr>
      {open && (
        <tr>
          <td colSpan={8} className="px-4 pb-4" style={{ backgroundColor: 'var(--surface-100)' }}>
            <div className="grid md:grid-cols-3 gap-4 pt-3">
              <div className="md:col-span-2">
                <div className="flex flex-wrap gap-x-5 gap-y-1 text-xs" style={{ color: 'var(--text-muted)' }}>
                  <span>Neto: <b style={{ color: 'var(--text-primary)' }}>{c.moneda} {Math.round(c.monto_neto).toLocaleString('es-CL')}</b></span>
                  <span>IVA: <b style={{ color: 'var(--text-primary)' }}>{c.moneda} {Math.round(c.iva).toLocaleString('es-CL')}</b></span>
                  <span>Total: <b className="text-brand-400">{fmtClp(c.monto_total_clp)}</b></span>
                  <span>Pagado: <b className="text-emerald-500">{fmtClp(c.monto_pagado_clp)}</b></span>
                  <span>Saldo: <b className="text-amber-400">{fmtClp(c.saldo_clp)}</b></span>
                </div>
                <div className="flex flex-wrap gap-x-5 gap-y-1 mt-1 text-xs" style={{ color: 'var(--text-faint)' }}>
                  {c.cuenta_codigo && <span>Cuenta: <b style={{ color: 'var(--text-muted)' }}>{c.cuenta_codigo} · {c.cuenta_nombre}</b></span>}
                  {c.es_anticipo && <span className="text-amber-500">Anticipo (NIC 21)</span>}
                  {c.proveedor_rut && <span>RUT: <b style={{ color: 'var(--text-muted)' }}>{c.proveedor_rut}</b></span>}
                  {c.condicion_pago && <span>Condición: <b style={{ color: 'var(--text-muted)' }}>{c.condicion_pago}</b></span>}
                  {c.moneda !== 'CLP' && <span>TC: <b style={{ color: 'var(--text-muted)' }}>{c.tc}</b></span>}
                  {c.referencia && <span>Ref: <b style={{ color: 'var(--text-muted)' }}>{c.referencia}</b></span>}
                  {c.descripcion && <span>Glosa: <b style={{ color: 'var(--text-muted)' }}>{c.descripcion}</b></span>}
                  {c.anulado && c.motivo_anulacion && <span className="text-red-400">Anulada: {c.motivo_anulacion}</span>}
                </div>
                {c.pagos.length > 0 && (
                  <div className="mt-3">
                    <p className="text-[11px] font-semibold uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>Pagos · la fecha en el banco se cruzará con la cartola en Conciliación Bancaria</p>
                    {c.pagos.map(p => (
                      <div key={p.id} className="flex items-center justify-between gap-2 text-xs py-1 border-b" style={{ borderColor: 'var(--border)' }}>
                        <span className="min-w-0 truncate" style={{ color: 'var(--text-muted)' }}>{fmtDate(p.fecha)} · {p.medio}{p.banco ? ` · ${p.banco}` : ''}{p.numero_operacion ? ` · ${p.numero_operacion}` : ''}{p.conciliado ? ' · ✓conciliado' : ''}{p.n_compras > 1 ? ' · (egreso de varios gastos)' : ''}</span>
                        <span className="flex items-center gap-2 shrink-0">
                          <span className="flex items-center gap-1" title="Fecha del movimiento en el banco (cartola)" onClick={(e) => e.stopPropagation()}>
                            <span style={{ color: 'var(--text-faint)' }}>en banco:</span>
                            <input type="date" value={p.fecha_mov_bancario || ''} disabled={c.anulado}
                              onChange={(e) => updFechaBanco(p.id, e.target.value)}
                              className="rounded border px-1 py-0.5 text-[11px]"
                              style={{ backgroundColor: 'var(--surface-200)', borderColor: 'var(--border)', color: 'var(--text-primary)' }} />
                          </span>
                          <span className="font-semibold text-emerald-500">{fmtClp(p.monto_clp)}</span>
                          {!c.anulado && <button onClick={(e) => { e.stopPropagation(); delPago(p.id) }} className="text-red-400 hover:bg-red-500/10 rounded p-0.5" title="Revertir pago"><Trash2 className="w-3 h-3" /></button>}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
                {c.items && c.items.length > 0 && (
                  <div className="mt-3">
                    <p className="text-[11px] font-semibold uppercase tracking-wider mb-1 flex items-center gap-1" style={{ color: 'var(--text-faint)' }}>
                      <Truck className="w-3 h-3" /> Detalle por ítem (compra nacional · costo = neto CLP)
                    </p>
                    <div className="overflow-x-auto rounded-lg border" style={{ borderColor: 'var(--border)' }}>
                      <table className="w-full text-[11px]">
                        <thead><tr style={{ backgroundColor: 'var(--surface-200)' }}>
                          {['N° Parte', 'Descripción', 'Cantidad', 'Costo unit CLP', 'Subtotal CLP'].map(h => (
                            <th key={h} className="text-left px-2 py-1.5 font-semibold uppercase tracking-wider whitespace-nowrap" style={{ color: 'var(--text-faint)' }}>{h}</th>
                          ))}
                        </tr></thead>
                        <tbody>
                          {c.items.map(it => (
                            <tr key={it.id} style={{ borderTop: '1px solid var(--border)' }}>
                              <td className="px-2 py-1.5 font-mono text-brand-400 whitespace-nowrap">{it.numero_parte || '—'}</td>
                              <td className="px-2 py-1.5 max-w-[180px] truncate" style={{ color: 'var(--text-primary)' }} title={it.descripcion || ''}>{it.descripcion || '—'}</td>
                              <td className="px-2 py-1.5" style={{ color: 'var(--text-muted)' }}>{it.cantidad}</td>
                              <td className="px-2 py-1.5 font-mono" style={{ color: 'var(--text-muted)' }}>{fmtClp(it.costo_unit_clp)}</td>
                              <td className="px-2 py-1.5 font-mono font-semibold" style={{ color: 'var(--text-primary)' }}>{fmtClp(it.costo_total_clp)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}
              </div>
              <div className="space-y-2">
                {!c.anulado && c.saldo_clp > 0 && (
                  <button onClick={(e) => { e.stopPropagation(); onPagar(c) }} className="btn-secondary w-full flex items-center justify-center gap-2 text-xs">
                    <CreditCard className="w-3.5 h-3.5" /> Registrar pago
                  </button>
                )}
                {!c.anulado && (
                  <button onClick={(e) => { e.stopPropagation(); anular() }} className="w-full flex items-center justify-center gap-1.5 text-xs text-red-400 hover:bg-red-500/10 rounded-lg py-1.5">
                    <Ban className="w-3.5 h-3.5" /> Anular compra
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

// ─── Pestaña: costos de embarque (solo lectura) ─────────────────────────────
function CostosEmbarqueTab() {
  const [rows, setRows] = useState<CostoEmbarque[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  useEffect(() => {
    comprasContabAPI.costosEmbarque().then(({ data }) => { setRows(data.costos); setTotal(data.total_clp) })
      .catch((e: any) => toast.error(e?.response?.data?.detail || 'No se pudieron cargar los costos de embarque'))
      .finally(() => setLoading(false))
  }, [])
  if (loading) return <div className="flex items-center justify-center py-16"><Loader2 className="w-7 h-7 animate-spin text-brand-400" /></div>
  return (
    <div className="space-y-3">
      <div className="rounded-xl border px-4 py-3 text-sm" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)', color: 'var(--text-muted)' }}>
        <Ship className="w-4 h-4 inline mr-1.5 text-cyan-400" /> Gastos de importación ya registrados en <b style={{ color: 'var(--text-primary)' }}>Embarques Pricing</b> (solo lectura). Total (neto+IVA): <b className="text-brand-400">{fmtClp(total)}</b>
      </div>
      {rows.length === 0 ? (
        <div className="rounded-2xl border py-12 text-center" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
          <p className="text-sm" style={{ color: 'var(--text-muted)' }}>No hay gastos de embarque registrados todavía.</p>
        </div>
      ) : (
        <div className="rounded-2xl border overflow-hidden" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead><tr className="border-b" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
                {['Embarque', 'Gasto', 'Proveedor', 'N° factura', 'Fecha', 'Neto', 'IVA', 'Total', 'Banco'].map(h => (
                  <th key={h} className="text-left px-4 py-3 text-xs font-semibold uppercase tracking-wider whitespace-nowrap" style={{ color: 'var(--text-faint)' }}>{h}</th>
                ))}
              </tr></thead>
              <tbody>
                {rows.map(r => (
                  <tr key={r.id} className="border-b" style={{ borderColor: 'var(--border)' }}>
                    <td className="px-4 py-2.5 font-mono text-brand-400 whitespace-nowrap">{r.embarque_numero || r.embarque_id}</td>
                    <td className="px-4 py-2.5" style={{ color: 'var(--text-primary)' }}>{r.glosa} <span className="text-[10px]" style={{ color: 'var(--text-faint)' }}>({r.tipo})</span></td>
                    <td className="px-4 py-2.5 max-w-[160px] truncate" style={{ color: 'var(--text-muted)' }}>{r.acreedor || '—'}</td>
                    <td className="px-4 py-2.5" style={{ color: 'var(--text-muted)' }}>{r.nro_factura || '—'}</td>
                    <td className="px-4 py-2.5 whitespace-nowrap" style={{ color: 'var(--text-muted)' }}>{fmtDate(r.fecha_factura)}</td>
                    <td className="px-4 py-2.5 whitespace-nowrap" style={{ color: 'var(--text-primary)' }}>{fmtClp(r.monto_neto)}</td>
                    <td className="px-4 py-2.5 whitespace-nowrap" style={{ color: 'var(--text-muted)' }}>{fmtClp(r.iva)}</td>
                    <td className="px-4 py-2.5 whitespace-nowrap font-semibold text-brand-400">{fmtClp(r.monto_total)}</td>
                    <td className="px-4 py-2.5" style={{ color: 'var(--text-muted)' }}>{r.banco || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}

// ─── Página ──────────────────────────────────────────────────────────────────
export default function ComprasContabPage() {
  const [tab, setTab] = useState<'compras' | 'embarque'>('compras')
  const [compras, setCompras] = useState<Compra[]>([])
  const [aging, setAging] = useState<Antiguedad | null>(null)
  const [kpis, setKpis] = useState<Kpis | null>(null)
  const [catalogos, setCatalogos] = useState<Catalogos | null>(null)
  const [loading, setLoading] = useState(true)
  const [q, setQ] = useState('')
  const [estado, setEstado] = useState('')
  const [tipo, setTipo] = useState('')
  const [error, setError] = useState('')
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const [modal, setModal] = useState<{ type: 'crear' | 'pago' | 'consolidado'; compra?: Compra } | null>(null)

  const PAGE_SIZE = 50

  const load = useCallback(async (search?: string, est?: string, tp?: string, pg = 1) => {
    setLoading(true); setError('')
    try {
      const [lRes, kRes] = await Promise.all([
        comprasContabAPI.list({ q: search || undefined, estado_pago: est || undefined, tipo: tp || undefined, page: pg, page_size: PAGE_SIZE }),
        comprasContabAPI.kpis(),
      ])
      setCompras(lRes.data.compras); setAging(lRes.data.antiguedad)
      setTotal(lRes.data.total); setPage(lRes.data.page); setKpis(kRes.data)
    } catch { setError('No se pudieron cargar las compras.') } finally { setLoading(false) }
  }, [])

  useEffect(() => {
    comprasContabAPI.catalogos().then(({ data }) => setCatalogos(data))
      .catch((e: any) => toast.error(e?.response?.data?.detail || 'No se pudieron cargar los catálogos'))
  }, [])
  useEffect(() => { load(q || undefined, estado || undefined, tipo || undefined, 1) }, [estado, tipo])
  const reload = () => load(q || undefined, estado || undefined, tipo || undefined, page)
  const goPage = (pg: number) => load(q || undefined, estado || undefined, tipo || undefined, pg)
  const handleSearch = (v: string) => { setQ(v); if (v.length === 0 || v.length >= 2) load(v || undefined, estado || undefined, tipo || undefined, 1) }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-2xl font-bold" style={{ color: 'var(--text-primary)' }}>Compras y Cuentas por Pagar</h1>
          <p className="text-sm mt-1" style={{ color: 'var(--text-muted)' }}>Registro de compras y gastos · condición de pago · antigüedad por pagar</p>
        </div>
        <div className="flex items-center gap-2 self-start">
          <button onClick={() => setModal({ type: 'crear' })} className="btn-primary flex items-center gap-2"><Plus className="w-4 h-4" /> Registrar compra</button>
          <button onClick={() => setModal({ type: 'consolidado' })} className="btn-secondary flex items-center gap-2"><CreditCard className="w-4 h-4" /> Pago consolidado</button>
          <button onClick={reload} className="btn-secondary flex items-center gap-2"><RefreshCw className="w-4 h-4" /></button>
        </div>
      </div>

      {/* Tabs */}
      <div className="flex items-center gap-2 border-b" style={{ borderColor: 'var(--border)' }}>
        {([['compras', 'Compras registradas'], ['embarque', 'Costos de embarque']] as const).map(([k, lbl]) => (
          <button key={k} onClick={() => setTab(k)}
            className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-all ${tab === k ? 'border-brand-500 text-brand-400' : 'border-transparent'}`}
            style={tab !== k ? { color: 'var(--text-muted)' } : {}}>{lbl}</button>
        ))}
      </div>

      {tab === 'embarque' ? <CostosEmbarqueTab /> : (
        <>
          {/* KPIs */}
          {kpis && (
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
              {[
                { icon: DollarSign, label: 'Total comprado', value: fmtClp(kpis.total_comprado_clp), color: 'text-brand-400' },
                { icon: CheckCircle2, label: 'Pagado', value: fmtClp(kpis.pagado_clp), color: 'text-emerald-500' },
                { icon: CreditCard, label: 'Por pagar', value: fmtClp(kpis.por_pagar_clp), color: 'text-amber-400' },
                { icon: AlertCircle, label: 'Vencido', value: fmtClp(kpis.vencido_clp), color: 'text-red-400' },
                { icon: Wallet, label: 'Costo de venta', value: fmtClp(kpis.por_tipo?.cogs || 0), color: 'text-purple-400' },
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
              <h3 className="font-semibold text-sm mb-3" style={{ color: 'var(--text-primary)' }}>Antigüedad de Cartera (saldo por pagar)</h3>
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
          <div className="flex flex-col gap-3">
            <div className="relative flex-1">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4" style={{ color: 'var(--text-faint)' }} />
              <input className="w-full pl-9 pr-4 py-2.5 rounded-xl border text-sm focus:outline-none focus:ring-2 focus:ring-brand-500/40"
                style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)', color: 'var(--text-primary)' }}
                placeholder="Buscar por proveedor, documento, referencia…" value={q} onChange={e => handleSearch(e.target.value)} />
            </div>
            <div className="flex flex-wrap items-center gap-1.5">
              {TIPOS.map(t => (
                <button key={t} onClick={() => setTipo(t)}
                  className={`px-3 py-1.5 rounded-full text-xs font-medium border transition-all ${tipo === t ? 'border-brand-500 bg-brand-500/10 text-brand-400' : 'border-transparent'}`}
                  style={tipo !== t ? { backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)', color: 'var(--text-muted)' } : {}}>{TIPO_LABEL[t]}</button>
              ))}
              <span className="mx-1" style={{ color: 'var(--text-faint)' }}>·</span>
              {ESTADOS.map(e => (
                <button key={e} onClick={() => setEstado(e)}
                  className={`px-3 py-1.5 rounded-full text-xs font-medium border transition-all ${estado === e ? 'border-brand-500 bg-brand-500/10 text-brand-400' : 'border-transparent'}`}
                  style={estado !== e ? { backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)', color: 'var(--text-muted)' } : {}}>{ESTADO_LABEL[e]}</button>
              ))}
            </div>
          </div>

          {error && <div className="rounded-xl border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-400">{error}</div>}
          {loading && <div className="flex items-center justify-center py-16"><Loader2 className="w-7 h-7 animate-spin text-brand-400" /></div>}
          {!loading && !error && compras.length === 0 && (
            <div className="rounded-2xl border py-16 text-center" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
              <Wallet className="w-10 h-10 mx-auto mb-3 opacity-20" style={{ color: 'var(--text-muted)' }} />
              <p className="text-sm font-medium" style={{ color: 'var(--text-muted)' }}>No hay compras registradas</p>
              <p className="text-xs mt-1" style={{ color: 'var(--text-faint)' }}>Usa "Registrar compra" para ingresar un gasto.</p>
            </div>
          )}

          {!loading && compras.length > 0 && (
            <div className="rounded-2xl border overflow-hidden" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead><tr className="border-b" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
                    {['Documento', 'Proveedor', 'Tipo', 'Fecha', 'Total', 'Pagado', 'Vencimiento', 'Estado'].map(h => (
                      <th key={h} className="text-left px-4 py-3 text-xs font-semibold uppercase tracking-wider whitespace-nowrap" style={{ color: 'var(--text-faint)' }}>{h}</th>
                    ))}
                  </tr></thead>
                  <tbody>
                    {compras.map(c => <CompraRow key={c.id} c={c} onChanged={reload} onPagar={(co) => setModal({ type: 'pago', compra: co })} />)}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* Paginador (server-side) */}
          {!loading && total > PAGE_SIZE && (
            <div className="flex items-center justify-between text-xs px-1" style={{ color: 'var(--text-muted)' }}>
              <span>Mostrando {(page - 1) * PAGE_SIZE + 1}–{Math.min(page * PAGE_SIZE, total)} de {total}</span>
              <div className="flex items-center gap-2">
                <button onClick={() => goPage(page - 1)} disabled={page <= 1}
                  className="btn-secondary px-3 py-1.5 text-xs disabled:opacity-40 disabled:cursor-not-allowed">Anterior</button>
                <span>Página {page} de {Math.max(1, Math.ceil(total / PAGE_SIZE))}</span>
                <button onClick={() => goPage(page + 1)} disabled={page * PAGE_SIZE >= total}
                  className="btn-secondary px-3 py-1.5 text-xs disabled:opacity-40 disabled:cursor-not-allowed">Siguiente</button>
              </div>
            </div>
          )}
        </>
      )}

      {/* Modales */}
      {modal?.type === 'crear' && <RegistrarCompraModal catalogos={catalogos} onClose={() => setModal(null)} onDone={reload} />}
      {modal?.type === 'pago' && modal.compra && <PagoCompraModal compra={modal.compra} onClose={() => setModal(null)} onDone={reload} />}
      {modal?.type === 'consolidado' && <PagoConsolidadoModal onClose={() => setModal(null)} onDone={reload} />}
    </div>
  )
}
