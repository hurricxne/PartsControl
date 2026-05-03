import { useState, useEffect, useCallback } from 'react'
import { Building2, Plus, Search, Phone, Mail, Globe, Pencil, Trash2, X, Check, Loader2, MapPin } from 'lucide-react'
import toast from 'react-hot-toast'
import { comprasAPI } from '../services/api'

interface Proveedor {
  id: number
  nombre: string
  pais: string
  moneda: string
  contacto: string
  email: string
  telefono: string
  sitio_web: string
  notas: string
}

const EMPTY: Omit<Proveedor, 'id'> = {
  nombre: '', pais: '', moneda: 'USD',
  contacto: '', email: '', telefono: '',
  sitio_web: '', notas: '',
}

const MONEDAS = ['USD', 'EUR', 'CLP', 'GBP', 'JPY', 'CNY']

function ProveedorModal({
  initial,
  onSave,
  onClose,
}: {
  initial: Omit<Proveedor, 'id'>
  onSave: (data: Omit<Proveedor, 'id'>) => Promise<void>
  onClose: () => void
}) {
  const [form, setForm] = useState(initial)
  const [saving, setSaving] = useState(false)
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>) =>
    setForm(p => ({ ...p, [k]: e.target.value }))

  const handleSave = async () => {
    if (!form.nombre.trim()) { toast.error('El nombre es obligatorio'); return }
    setSaving(true)
    try { await onSave(form) } finally { setSaving(false) }
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
            {initial.nombre ? 'Editar proveedor' : 'Nuevo proveedor'}
          </h3>
          <button onClick={onClose} className="rounded-lg p-1 hover:bg-[var(--surface-200)] transition-colors">
            <X className="w-4 h-4" style={{ color: 'var(--text-muted)' }} />
          </button>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div className="sm:col-span-2">
            <label className="block text-xs font-semibold uppercase tracking-wider mb-1.5" style={{ color: 'var(--text-faint)' }}>
              Nombre / Razón Social <span className="text-red-400">*</span>
            </label>
            <input className="input w-full" placeholder="Ej: Florida Engine Parts" value={form.nombre} onChange={set('nombre')} />
          </div>
          <div>
            <label className="block text-xs font-semibold uppercase tracking-wider mb-1.5" style={{ color: 'var(--text-faint)' }}>País</label>
            <input className="input w-full" placeholder="Ej: USA" value={form.pais} onChange={set('pais')} />
          </div>
          <div>
            <label className="block text-xs font-semibold uppercase tracking-wider mb-1.5" style={{ color: 'var(--text-faint)' }}>Moneda preferida</label>
            <select className="input w-full" value={form.moneda} onChange={set('moneda')}>
              {MONEDAS.map(m => <option key={m}>{m}</option>)}
            </select>
          </div>
          <div>
            <label className="block text-xs font-semibold uppercase tracking-wider mb-1.5" style={{ color: 'var(--text-faint)' }}>Contacto</label>
            <input className="input w-full" placeholder="Nombre de contacto" value={form.contacto} onChange={set('contacto')} />
          </div>
          <div>
            <label className="block text-xs font-semibold uppercase tracking-wider mb-1.5" style={{ color: 'var(--text-faint)' }}>Email</label>
            <input type="email" className="input w-full" placeholder="contacto@proveedor.com" value={form.email} onChange={set('email')} />
          </div>
          <div>
            <label className="block text-xs font-semibold uppercase tracking-wider mb-1.5" style={{ color: 'var(--text-faint)' }}>Teléfono</label>
            <input className="input w-full" placeholder="+1 555 000 0000" value={form.telefono} onChange={set('telefono')} />
          </div>
          <div>
            <label className="block text-xs font-semibold uppercase tracking-wider mb-1.5" style={{ color: 'var(--text-faint)' }}>Sitio web</label>
            <input className="input w-full" placeholder="www.proveedor.com" value={form.sitio_web} onChange={set('sitio_web')} />
          </div>
          <div className="sm:col-span-2">
            <label className="block text-xs font-semibold uppercase tracking-wider mb-1.5" style={{ color: 'var(--text-faint)' }}>Notas</label>
            <textarea className="input w-full resize-none" rows={2} placeholder="Observaciones, condiciones comerciales…" value={form.notas} onChange={set('notas')} />
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

export default function ProveedoresPage() {
  const [proveedores, setProveedores] = useState<Proveedor[]>([])
  const [loading, setLoading] = useState(true)
  const [q, setQ] = useState('')
  const [modal, setModal] = useState<{ open: boolean; item: Proveedor | null }>({ open: false, item: null })
  const [deleting, setDeleting] = useState<number | null>(null)

  const load = useCallback(() => {
    setLoading(true)
    comprasAPI.listProveedores()
      .then(({ data }) => setProveedores(data))
      .catch(() => toast.error('Error al cargar proveedores'))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load() }, [load])

  const handleSave = async (data: Omit<Proveedor, 'id'>) => {
    if (modal.item) {
      await comprasAPI.updateProveedor(modal.item.id, data)
      toast.success('Proveedor actualizado')
    } else {
      await comprasAPI.createProveedor(data)
      toast.success('Proveedor creado')
    }
    setModal({ open: false, item: null })
    load()
  }

  const handleDelete = async (id: number) => {
    setDeleting(id)
    try {
      await comprasAPI.deleteProveedor(id)
      toast.success('Proveedor eliminado')
      load()
    } catch {
      toast.error('Error al eliminar')
    } finally {
      setDeleting(null)
    }
  }

  const filtered = proveedores.filter(p =>
    !q ||
    p.nombre.toLowerCase().includes(q.toLowerCase()) ||
    p.pais.toLowerCase().includes(q.toLowerCase()) ||
    p.contacto.toLowerCase().includes(q.toLowerCase())
  )

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold" style={{ color: 'var(--text-primary)' }}>Proveedores</h1>
          <p className="text-sm mt-1" style={{ color: 'var(--text-muted)' }}>
            Maestro de proveedores externos — importaciones y suministros
          </p>
        </div>
        <button
          onClick={() => setModal({ open: true, item: null })}
          className="btn-primary flex items-center justify-center gap-2 w-full sm:w-auto shrink-0"
        >
          <Plus className="w-4 h-4" /> Nuevo proveedor
        </button>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
        {[
          { label: 'Total proveedores', value: proveedores.length,                                               color: 'text-brand-400'   },
          { label: 'Con email',         value: proveedores.filter(p => p.email).length,                          color: 'text-emerald-500' },
          { label: 'Moneda USD',        value: proveedores.filter(p => p.moneda === 'USD' || !p.moneda).length,  color: 'text-amber-400'   },
        ].map(s => (
          <div key={s.label} className="rounded-2xl p-3 sm:p-4 border"
            style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
            <p className="text-[10px] uppercase tracking-widest leading-tight" style={{ color: 'var(--text-faint)' }}>{s.label}</p>
            <p className={`text-xl font-bold mt-1 ${s.color}`}>{s.value}</p>
          </div>
        ))}
      </div>

      {/* Search */}
      <div className="relative max-w-sm">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4" style={{ color: 'var(--text-faint)' }} />
        <input className="input pl-9 w-full" placeholder="Buscar por nombre, país o contacto…"
          value={q} onChange={e => setQ(e.target.value)} />
      </div>

      {/* List */}
      {loading ? (
        <div className="flex items-center justify-center py-16">
          <Loader2 className="w-6 h-6 animate-spin" style={{ color: 'var(--text-faint)' }} />
        </div>
      ) : filtered.length === 0 ? (
        <div className="card p-10 text-center space-y-3">
          <Building2 className="w-10 h-10 mx-auto" style={{ color: 'var(--text-faint)' }} />
          <p className="font-semibold" style={{ color: 'var(--text-primary)' }}>
            {proveedores.length === 0 ? 'Sin proveedores registrados' : 'Sin resultados'}
          </p>
          <p className="text-sm" style={{ color: 'var(--text-muted)' }}>
            {proveedores.length === 0
              ? 'Agrega tu primer proveedor con el botón de arriba.'
              : 'Ajusta el término de búsqueda.'}
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-4">
          {filtered.map(p => (
            <div key={p.id} className="rounded-2xl border p-4 space-y-3 hover:shadow-md transition-shadow"
              style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>

              {/* Header */}
              <div className="flex items-start gap-3">
                <div className="w-10 h-10 rounded-xl bg-brand-500/10 flex items-center justify-center shrink-0">
                  <Building2 className="w-5 h-5 text-brand-400" />
                </div>
                <div className="flex-1 min-w-0">
                  <p className="font-semibold text-sm leading-tight truncate" style={{ color: 'var(--text-primary)' }}>
                    {p.nombre}
                  </p>
                  <div className="flex items-center gap-2 mt-0.5 flex-wrap">
                    {p.pais && (
                      <span className="flex items-center gap-1 text-xs" style={{ color: 'var(--text-muted)' }}>
                        <MapPin className="w-3 h-3" /> {p.pais}
                      </span>
                    )}
                    {p.moneda && (
                      <span className="text-xs px-1.5 py-0.5 rounded-md font-mono font-semibold"
                        style={{ background: 'var(--surface-300)', color: 'var(--text-primary)' }}>
                        {p.moneda}
                      </span>
                    )}
                  </div>
                </div>
              </div>

              {/* Contact info */}
              <div className="space-y-1.5 text-xs" style={{ color: 'var(--text-muted)' }}>
                {p.contacto && (
                  <div className="flex items-center gap-2 truncate">
                    <Building2 className="w-3 h-3 shrink-0" style={{ color: 'var(--text-faint)' }} />
                    <span className="truncate">{p.contacto}</span>
                  </div>
                )}
                {p.email && (
                  <div className="flex items-center gap-2 truncate">
                    <Mail className="w-3 h-3 shrink-0" style={{ color: 'var(--text-faint)' }} />
                    <a href={`mailto:${p.email}`} className="truncate hover:text-brand-400 transition-colors">{p.email}</a>
                  </div>
                )}
                {p.telefono && (
                  <div className="flex items-center gap-2">
                    <Phone className="w-3 h-3 shrink-0" style={{ color: 'var(--text-faint)' }} />
                    <span>{p.telefono}</span>
                  </div>
                )}
                {p.sitio_web && (
                  <div className="flex items-center gap-2 truncate">
                    <Globe className="w-3 h-3 shrink-0" style={{ color: 'var(--text-faint)' }} />
                    <a href={p.sitio_web.startsWith('http') ? p.sitio_web : `https://${p.sitio_web}`}
                      target="_blank" rel="noopener noreferrer"
                      className="truncate hover:text-brand-400 transition-colors">{p.sitio_web}</a>
                  </div>
                )}
                {p.notas && (
                  <p className="text-xs mt-1 line-clamp-2 italic" style={{ color: 'var(--text-faint)' }}>{p.notas}</p>
                )}
              </div>

              {/* Actions */}
              <div className="flex gap-2 pt-1 border-t" style={{ borderColor: 'var(--border)' }}>
                <button
                  onClick={() => setModal({ open: true, item: p })}
                  className="btn-secondary text-xs flex-1 flex items-center justify-center gap-1.5 py-1.5"
                >
                  <Pencil className="w-3.5 h-3.5" /> Editar
                </button>
                <button
                  onClick={() => handleDelete(p.id)}
                  disabled={deleting === p.id}
                  className="text-xs px-3 py-1.5 rounded-xl border transition-colors flex items-center gap-1.5"
                  style={{ borderColor: 'var(--border)', color: 'var(--text-muted)' }}
                >
                  {deleting === p.id
                    ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    : <Trash2 className="w-3.5 h-3.5" />}
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {modal.open && (
        <ProveedorModal
          initial={modal.item ? { ...modal.item } : { ...EMPTY }}
          onSave={handleSave}
          onClose={() => setModal({ open: false, item: null })}
        />
      )}
    </div>
  )
}
