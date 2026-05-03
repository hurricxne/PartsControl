import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Users, Plus, Search, Building2, Phone, Mail, MapPin,
  Edit2, Trash2, Save, X, AlertCircle, RefreshCw, CheckCircle,
} from 'lucide-react'
import toast from 'react-hot-toast'
import { clientesAPI } from '../services/api'

interface Cliente {
  id: number
  rut: string
  nombre: string
  contacto: string
  email: string
  telefono: string
  direccion: string
  ciudad: string
  giro: string
  created_at: string | null
}

const EMPTY_FORM: Omit<Cliente, 'id' | 'created_at'> = {
  rut: '', nombre: '', contacto: '', email: '', telefono: '', direccion: '', ciudad: '', giro: '',
}

function cleanRut(rut: string) { return rut.replace(/[^0-9kK]/g, '').toUpperCase() }
function formatRut(rut: string): string {
  const clean = cleanRut(rut)
  if (clean.length < 2) return clean
  const body = clean.slice(0, -1)
  const dv = clean.slice(-1)
  return `${body.replace(/\B(?=(\d{3})+(?!\d))/g, '.')}-${dv}`
}
function validateRut(rut: string): boolean {
  const clean = cleanRut(rut)
  if (clean.length < 2) return false
  const body = clean.slice(0, -1)
  const dv = clean.slice(-1)
  if (!/^\d+$/.test(body)) return false
  let sum = 0; let factor = 2
  for (let i = body.length - 1; i >= 0; i--) {
    sum += parseInt(body[i]) * factor
    factor = factor === 7 ? 2 : factor + 1
  }
  const expected = 11 - (sum % 11)
  const dvExpected = expected === 11 ? '0' : expected === 10 ? 'K' : String(expected)
  return dv === dvExpected
}

function Field({ label, value, onChange, placeholder, disabled, mono, error }: {
  label: string; value: string; onChange: (v: string) => void
  placeholder?: string; disabled?: boolean; mono?: boolean; error?: string
}) {
  return (
    <div>
      <label className="block text-[10px] font-semibold uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>
        {label}
      </label>
      <input
        className={`input w-full ${mono ? 'font-mono' : ''}`}
        placeholder={placeholder}
        value={value}
        onChange={e => onChange(e.target.value)}
        disabled={disabled}
        style={error ? { borderColor: '#f87171' } : undefined}
      />
      {error && <p className="text-[10px] text-red-400 mt-0.5">{error}</p>}
    </div>
  )
}

export default function ClientesPage() {
  const qc = useQueryClient()
  const [search, setSearch] = useState('')
  const [showForm, setShowForm] = useState(false)
  const [editId, setEditId] = useState<number | null>(null)
  const [form, setForm] = useState({ ...EMPTY_FORM })
  const [rutError, setRutError] = useState('')
  const [rutOk, setRutOk] = useState(false)
  const [deletingId, setDeletingId] = useState<number | null>(null)

  const { data: clientes = [], isLoading } = useQuery<Cliente[]>({
    queryKey: ['clientes'],
    queryFn: () => clientesAPI.list().then(r => r.data),
  })

  const createMutation = useMutation({
    mutationFn: (data: typeof form) => clientesAPI.upsert(data),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['clientes'] }); toast.success('Cliente guardado'); resetForm() },
    onError: (e: any) => toast.error(e?.response?.data?.detail || 'Error al guardar'),
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: number; data: typeof form }) => clientesAPI.update(id, data),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['clientes'] }); toast.success('Cliente actualizado'); resetForm() },
    onError: (e: any) => toast.error(e?.response?.data?.detail || 'Error al actualizar'),
  })

  const deleteMutation = useMutation({
    mutationFn: (id: number) => clientesAPI.remove(id),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['clientes'] }); toast.success('Cliente eliminado'); setDeletingId(null) },
    onError: () => toast.error('Error al eliminar'),
  })

  const resetForm = () => {
    setForm({ ...EMPTY_FORM }); setEditId(null); setShowForm(false)
    setRutError(''); setRutOk(false)
  }

  const openEdit = (c: Cliente) => {
    setForm({ rut: c.rut, nombre: c.nombre, contacto: c.contacto, email: c.email, telefono: c.telefono, direccion: c.direccion, ciudad: c.ciudad, giro: c.giro })
    setEditId(c.id); setShowForm(true); setRutOk(true); setRutError('')
  }

  const handleRutChange = (raw: string) => {
    setRutError(''); setRutOk(false)
    const clean = cleanRut(raw)
    const display = clean.length >= 7 ? formatRut(clean) : raw.replace(/[^0-9kK.\-]/gi, '')
    setForm(p => ({ ...p, rut: display }))
  }

  const handleRutBlur = () => {
    if (!form.rut) { setRutError(''); setRutOk(false); return }
    const clean = cleanRut(form.rut)
    if (clean.length < 2) { setRutError('RUT muy corto'); setRutOk(false); return }
    const formatted = formatRut(clean)
    setForm(p => ({ ...p, rut: formatted }))
    if (!validateRut(clean)) { setRutError('RUT inválido — verifica el dígito verificador'); setRutOk(false); return }
    setRutError(''); setRutOk(true)
  }

  const handleSubmit = () => {
    if (!form.nombre.trim()) { toast.error('El nombre es requerido'); return }
    if (form.rut && !rutOk) { toast.error('Corrige el RUT antes de guardar'); return }
    if (editId !== null) {
      updateMutation.mutate({ id: editId, data: form })
    } else {
      createMutation.mutate(form)
    }
  }

  const filtered = clientes.filter(c => {
    const q = search.toLowerCase()
    return !q || c.nombre?.toLowerCase().includes(q) || c.rut?.includes(q) ||
      c.ciudad?.toLowerCase().includes(q) || c.contacto?.toLowerCase().includes(q) ||
      c.email?.toLowerCase().includes(q)
  })

  const isSaving = createMutation.isPending || updateMutation.isPending

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold" style={{ color: 'var(--text-primary)' }}>Clientes</h1>
          <p className="text-sm mt-0.5" style={{ color: 'var(--text-muted)' }}>
            {clientes.length} cliente{clientes.length !== 1 ? 's' : ''} registrado{clientes.length !== 1 ? 's' : ''}
          </p>
        </div>
        <button
          onClick={() => { resetForm(); setShowForm(true) }}
          className="btn-primary flex items-center gap-2 shrink-0"
        >
          <Plus className="w-4 h-4" /> Nuevo Cliente
        </button>
      </div>

      {/* Formulario crear/editar */}
      {showForm && (
        <div className="card p-5 border-2" style={{ borderColor: 'var(--empresa-primary)' }}>
          <div className="flex items-center justify-between mb-4">
            <h3 className="font-semibold flex items-center gap-2" style={{ color: 'var(--text-primary)' }}>
              <Building2 className="w-4 h-4" style={{ color: 'var(--empresa-primary)' }} />
              {editId !== null ? 'Editar Cliente' : 'Nuevo Cliente'}
            </h3>
            <button onClick={resetForm} style={{ color: 'var(--text-faint)' }}><X className="w-4 h-4" /></button>
          </div>

          {/* Fila 1: RUT + Nombre */}
          <div className="grid grid-cols-12 gap-3 mb-3">
            <div className="col-span-3">
              <label className="block text-[10px] font-semibold uppercase tracking-wider mb-1" style={{ color: 'var(--text-faint)' }}>
                RUT
              </label>
              <input
                className="input w-full font-mono"
                placeholder="12.345.678-9"
                value={form.rut}
                onChange={e => handleRutChange(e.target.value)}
                onBlur={handleRutBlur}
                style={rutError ? { borderColor: '#f87171' } : rutOk ? { borderColor: '#4ade80' } : undefined}
              />
              {rutError
                ? <p className="text-[10px] text-red-400 mt-0.5 flex items-center gap-1"><AlertCircle className="w-3 h-3" />{rutError}</p>
                : rutOk
                  ? <p className="text-[10px] mt-0.5 flex items-center gap-1" style={{ color: '#4ade80' }}><CheckCircle className="w-3 h-3" /> RUT válido</p>
                  : null
              }
            </div>
            <div className="col-span-9">
              <Field label="Nombre / Razón Social *" value={form.nombre}
                onChange={v => setForm(p => ({ ...p, nombre: v }))} placeholder="Empresa Ltda." />
            </div>
          </div>

          {/* Fila 2: Contacto + Email + Teléfono */}
          <div className="grid grid-cols-3 gap-3 mb-3">
            <Field label="Contacto" value={form.contacto}
              onChange={v => setForm(p => ({ ...p, contacto: v }))} placeholder="Juan Pérez" />
            <Field label="Email" value={form.email}
              onChange={v => setForm(p => ({ ...p, email: v }))} placeholder="contacto@empresa.cl" />
            <Field label="Teléfono" value={form.telefono}
              onChange={v => setForm(p => ({ ...p, telefono: v }))} placeholder="+56 9 XXXX XXXX" />
          </div>

          {/* Fila 3: Dirección + Ciudad + Giro */}
          <div className="grid grid-cols-3 gap-3 mb-4">
            <Field label="Dirección" value={form.direccion}
              onChange={v => setForm(p => ({ ...p, direccion: v }))} placeholder="Av. Industrial 1234" />
            <Field label="Ciudad" value={form.ciudad}
              onChange={v => setForm(p => ({ ...p, ciudad: v }))} placeholder="Santiago" />
            <Field label="Giro" value={form.giro}
              onChange={v => setForm(p => ({ ...p, giro: v }))} placeholder="Comercio industrial" />
          </div>

          <div className="flex justify-end gap-3">
            <button onClick={resetForm} className="btn-secondary">Cancelar</button>
            <button onClick={handleSubmit} disabled={isSaving || !form.nombre.trim()} className="btn-primary flex items-center gap-2">
              {isSaving ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
              {editId !== null ? 'Guardar cambios' : 'Crear cliente'}
            </button>
          </div>
        </div>
      )}

      {/* Search */}
      <div className="relative max-w-sm">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4" style={{ color: 'var(--text-faint)' }} />
        <input
          className="input pl-9 w-full"
          placeholder="Buscar por nombre, RUT, ciudad…"
          value={search}
          onChange={e => setSearch(e.target.value)}
        />
      </div>

      {/* Lista */}
      {isLoading ? (
        <div className="flex items-center justify-center h-40 gap-2" style={{ color: 'var(--text-faint)' }}>
          <RefreshCw className="w-5 h-5 animate-spin" /> Cargando clientes…
        </div>
      ) : filtered.length === 0 ? (
        <div className="card p-10 text-center">
          <Users className="w-8 h-8 mx-auto mb-3" style={{ color: 'var(--text-faint)' }} />
          <p className="font-medium" style={{ color: 'var(--text-muted)' }}>
            {search ? `Sin resultados para "${search}"` : 'No hay clientes registrados aún'}
          </p>
          {!search && (
            <button onClick={() => setShowForm(true)} className="btn-primary mt-4 mx-auto">
              <Plus className="w-4 h-4" /> Agregar primer cliente
            </button>
          )}
        </div>
      ) : (
        <div className="rounded-2xl border overflow-hidden" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
          <table className="w-full text-sm">
            <thead>
              <tr style={{ backgroundColor: 'var(--surface-200)', borderBottom: '2px solid var(--border)' }}>
                {['RUT', 'Nombre / Razón Social', 'Contacto', 'Email', 'Teléfono', 'Ciudad', ''].map(h => (
                  <th key={h} className="text-left px-4 py-3 text-[10px] font-semibold uppercase tracking-wider whitespace-nowrap" style={{ color: 'var(--text-faint)' }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {filtered.map((c, idx) => (
                <tr key={c.id} style={{ borderBottom: '1px solid var(--border)', backgroundColor: idx % 2 === 0 ? 'var(--surface-100)' : 'var(--surface)' }}>
                  <td className="px-4 py-3 font-mono text-xs" style={{ color: 'var(--text-muted)' }}>{c.rut || '—'}</td>
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-2">
                      <div className="w-7 h-7 rounded-lg flex items-center justify-center shrink-0 text-[11px] font-bold text-brand-400" style={{ backgroundColor: 'var(--surface-300)' }}>
                        {(c.nombre || '?').charAt(0).toUpperCase()}
                      </div>
                      <span className="font-medium text-sm" style={{ color: 'var(--text-primary)' }}>{c.nombre}</span>
                    </div>
                  </td>
                  <td className="px-4 py-3 text-xs" style={{ color: 'var(--text-muted)' }}>
                    {c.contacto ? (
                      <div className="flex items-center gap-1"><Users className="w-3 h-3 shrink-0" />{c.contacto}</div>
                    ) : '—'}
                  </td>
                  <td className="px-4 py-3 text-xs" style={{ color: 'var(--text-muted)' }}>
                    {c.email ? (
                      <a href={`mailto:${c.email}`} className="flex items-center gap-1 hover:text-brand-400 transition-colors">
                        <Mail className="w-3 h-3 shrink-0" />{c.email}
                      </a>
                    ) : '—'}
                  </td>
                  <td className="px-4 py-3 text-xs whitespace-nowrap" style={{ color: 'var(--text-muted)' }}>
                    {c.telefono ? (
                      <div className="flex items-center gap-1"><Phone className="w-3 h-3 shrink-0" />{c.telefono}</div>
                    ) : '—'}
                  </td>
                  <td className="px-4 py-3 text-xs" style={{ color: 'var(--text-muted)' }}>
                    {c.ciudad ? (
                      <div className="flex items-center gap-1"><MapPin className="w-3 h-3 shrink-0" />{c.ciudad}</div>
                    ) : '—'}
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-1">
                      <button
                        onClick={() => openEdit(c)}
                        className="p-1.5 rounded-lg transition-colors hover:bg-[var(--surface-300)]"
                        title="Editar"
                        style={{ color: 'var(--text-faint)' }}
                      >
                        <Edit2 className="w-3.5 h-3.5" />
                      </button>
                      {deletingId === c.id ? (
                        <div className="flex items-center gap-1">
                          <button
                            onClick={() => deleteMutation.mutate(c.id)}
                            disabled={deleteMutation.isPending}
                            className="text-[10px] px-2 py-1 rounded-lg bg-red-500/20 text-red-400 hover:bg-red-500/30 transition-colors font-medium"
                          >
                            {deleteMutation.isPending ? '…' : 'Confirmar'}
                          </button>
                          <button onClick={() => setDeletingId(null)} className="text-[10px] px-2 py-1 rounded-lg transition-colors" style={{ color: 'var(--text-faint)' }}>
                            No
                          </button>
                        </div>
                      ) : (
                        <button
                          onClick={() => setDeletingId(c.id)}
                          className="p-1.5 rounded-lg transition-colors hover:bg-red-500/10 text-red-400/50 hover:text-red-400"
                          title="Eliminar"
                        >
                          <Trash2 className="w-3.5 h-3.5" />
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="px-4 py-2.5 border-t" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
            <p className="text-xs" style={{ color: 'var(--text-faint)' }}>
              {filtered.length} de {clientes.length} clientes
              {search && ` · filtro: "${search}"`}
            </p>
          </div>
        </div>
      )}
    </div>
  )
}
