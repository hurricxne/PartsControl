// Modal reutilizable para editar ex-post los datos de la Orden de Compra del cliente
// (OcCliente): N° OC, Fecha OC, Condiciones de pago, Fecha de entrega y Asesor.
// Se usa desde Ventas (comercial) y Ventas — Contabilidad. Consume:
//   PUT /compras/oc-cliente/{id}  (comprasAPI.actualizarOcCliente)
//   GET /auth/users               (authAPI.users, para el selector de asesor)
import { useState, useEffect } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { X, Check, Loader2 } from 'lucide-react'
import toast from 'react-hot-toast'
import { comprasAPI, authAPI } from '../services/api'

export interface OcClienteInitial {
  numero_oc: string | null
  fecha_oc: string | null
  cond_pago: string | null
  fecha_entrega: string | null
  asesor_id: number | null
}

interface Asesor {
  id: number
  nombre: string
}

const COND_PAGO_OPCIONES = [
  'Contado',
  '30 días contra factura',
  '60 días contra factura',
  '90 días contra factura',
]

// oc_cliente.fecha_oc es TEXTO libre (String(50)) y los datos históricos vienen en
// varios formatos (10/06/2026, 10-06-2026…). El <input type="date"> solo entiende
// yyyy-mm-dd: sin normalizar, una fecha legada se vería vacía y no se podría corregir.
// Mismos formatos que parsea el backend (wasabil_dte/service.parse_fecha_oc).
function fechaISO(valor: string | null): string {
  if (!valor) return ''
  const s = valor.trim().slice(0, 10)
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return s
  let m = s.match(/^(\d{4})[/.](\d{1,2})[/.](\d{1,2})$/)
  if (m) return `${m[1]}-${m[2].padStart(2, '0')}-${m[3].padStart(2, '0')}`
  m = s.match(/^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})$/)
  if (m) return `${m[3]}-${m[2].padStart(2, '0')}-${m[1].padStart(2, '0')}`
  // Año de 2 dígitos (10/06/26): también existe en datos reales (backend %d/%m/%y)
  m = s.match(/^(\d{1,2})[/.-](\d{1,2})[/.-](\d{2})$/)
  if (m) return `20${m[3]}-${m[2].padStart(2, '0')}-${m[1].padStart(2, '0')}`
  return ''
}

export default function OcClienteEditModal({
  ocId,
  initial,
  onSaved,
  onClose,
}: {
  ocId: number
  initial: OcClienteInitial
  onSaved: () => void
  onClose: () => void
}) {
  const [numeroOc, setNumeroOc] = useState(initial.numero_oc || '')
  const [fechaOc, setFechaOc] = useState(fechaISO(initial.fecha_oc))
  const [condPago, setCondPago] = useState(initial.cond_pago || '')
  const [fechaEntrega, setFechaEntrega] = useState(fechaISO(initial.fecha_entrega))
  const [asesorId, setAsesorId] = useState<number | ''>(initial.asesor_id ?? '')
  const [asesores, setAsesores] = useState<Asesor[]>([])
  const [saving, setSaving] = useState(false)
  const qc = useQueryClient()

  useEffect(() => {
    authAPI.users().then(({ data }) => setAsesores(data)).catch(() => {})
  }, [])

  // Preserva un valor histórico de condición de pago que no esté en la lista estándar.
  const condOpciones = !condPago || COND_PAGO_OPCIONES.includes(condPago)
    ? COND_PAGO_OPCIONES
    : [condPago, ...COND_PAGO_OPCIONES]

  // Las fechas solo viajan si el usuario las CAMBIÓ (undefined → el backend no
  // las toca). Así: (1) una fecha legada en formato ilegible no se borra por
  // editar otro campo; (2) una legible no-ISO no se reescribe en silencio; y
  // (3) editar condiciones/asesor no choca con el candado 409 de la guía SII
  // (que protege N° y fecha de la OC una vez emitida).
  const fechaOcInicial = fechaISO(initial.fecha_oc)
  const fechaEntregaInicial = fechaISO(initial.fecha_entrega)

  const handleSave = async () => {
    if (!numeroOc.trim()) { toast.error('El N° OC del cliente es obligatorio'); return }
    setSaving(true)
    try {
      await comprasAPI.actualizarOcCliente(ocId, {
        numero_oc: numeroOc.trim(),
        fecha_oc: fechaOc === fechaOcInicial ? undefined : fechaOc,
        cond_pago: condPago,
        fecha_entrega: fechaEntrega === fechaEntregaInicial ? undefined : fechaEntrega,
        asesor_id: asesorId === '' ? null : asesorId,
      })
      toast.success('OC actualizada')
      // Despachos cachea las OCs con react-query (staleTime 30 s): sin esto, la
      // pantalla de Despachos mostraría el N°/fecha viejos hasta el refetch.
      qc.invalidateQueries({ queryKey: ['despachos'] })
      onSaved()
      onClose()
    } catch (err: any) {
      toast.error(err.response?.data?.detail || 'Error al actualizar la OC')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ background: 'rgba(0,0,0,0.6)', backdropFilter: 'blur(4px)' }}
      onClick={e => { if (e.target === e.currentTarget) onClose() }}
    >
      <div className="w-full max-w-lg rounded-2xl border shadow-2xl p-6 space-y-5"
        style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
        <div className="flex items-center justify-between">
          <h3 className="font-semibold text-base" style={{ color: 'var(--text-primary)' }}>
            Editar Orden de Compra
          </h3>
          <button onClick={onClose} className="rounded-lg p-1 hover:bg-[var(--surface-200)] transition-colors">
            <X className="w-4 h-4" style={{ color: 'var(--text-muted)' }} />
          </button>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div className="sm:col-span-2">
            <label className="block text-xs font-semibold uppercase tracking-wider mb-1.5" style={{ color: 'var(--text-faint)' }}>
              N° OC del Cliente <span className="text-red-400">*</span>
            </label>
            <input className="input w-full" placeholder="Ej: OC-98712"
              value={numeroOc} onChange={e => setNumeroOc(e.target.value)} />
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
              <option value="">— sin definir —</option>
              {condOpciones.map(o => <option key={o}>{o}</option>)}
            </select>
          </div>
          <div>
            <label className="block text-xs font-semibold uppercase tracking-wider mb-1.5" style={{ color: 'var(--text-faint)' }}>
              Fecha prometida de entrega
            </label>
            <input type="date" className="input w-full"
              value={fechaEntrega} onChange={e => setFechaEntrega(e.target.value)} />
          </div>
          <div>
            <label className="block text-xs font-semibold uppercase tracking-wider mb-1.5" style={{ color: 'var(--text-faint)' }}>
              Asesor responsable
            </label>
            <select className="input w-full" value={asesorId}
              onChange={e => setAsesorId(e.target.value === '' ? '' : Number(e.target.value))}>
              <option value="">— sin asesor —</option>
              {asesores.map(a => <option key={a.id} value={a.id}>{a.nombre}</option>)}
            </select>
          </div>
        </div>

        <div className="flex gap-3 pt-1">
          <button onClick={onClose} className="btn-secondary flex-1">Cancelar</button>
          <button onClick={handleSave} disabled={saving}
            className="btn-primary flex-1 flex items-center justify-center gap-2">
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />}
            {saving ? 'Guardando…' : 'Guardar'}
          </button>
        </div>
      </div>
    </div>
  )
}
