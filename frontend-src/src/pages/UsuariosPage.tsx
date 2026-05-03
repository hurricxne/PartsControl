import { UserCog, Plus, Search, Shield, CheckCircle2, XCircle, Mail } from 'lucide-react'

const ROLES: Record<string, { label: string; cls: string; modulos: string }> = {
  admin:         { label: 'Admin',          cls: 'bg-red-500/10 text-red-400 border-red-400/20',           modulos: 'Todo — acceso total + configuración + usuarios' },
  gerencia:      { label: 'Gerencia',       cls: 'bg-purple-500/10 text-purple-400 border-purple-400/20',  modulos: 'Todos los dashboards — solo lectura' },
  comercial:     { label: 'Comercial',      cls: 'bg-brand-500/10 text-brand-400 border-brand-400/20',     modulos: 'Clientes, Cotizaciones, Cierre de Venta, Ventas' },
  abastecimiento:{ label: 'Abastecimiento', cls: 'bg-emerald-500/10 text-emerald-500 border-emerald-500/20',modulos: 'Panel Compras, Seguimiento, Control TC (solo lectura)' },
  logistica:     { label: 'Logística',      cls: 'bg-amber-500/10 text-amber-400 border-amber-400/20',     modulos: 'Embarques, Bodega (sin precios)' },
  contabilidad:  { label: 'Contabilidad',   cls: 'bg-blue-500/10 text-blue-400 border-blue-400/20',        modulos: 'Ventas Contab., Embarques Pricing, Facturas, Control TC' },
}

const DEMO_USERS = [
  { nombre: 'Aldo Ibaceta',     email: 'aldo@grupoam.cl',      rol: 'admin',          activo: true,  ultimo: '23-03-2026' },
  { nombre: 'Ricardo Castillo', email: 'ricardo@grupoam.cl',   rol: 'comercial',      activo: true,  ultimo: '23-03-2026' },
  { nombre: 'Francisco Lopez',  email: 'francisco@grupoam.cl', rol: 'comercial',      activo: true,  ultimo: '22-03-2026' },
  { nombre: 'María González',   email: 'mgonzalez@grupoam.cl', rol: 'contabilidad',   activo: true,  ultimo: '21-03-2026' },
  { nombre: 'Pedro Saavedra',   email: 'psaavedra@grupoam.cl', rol: 'logistica',      activo: true,  ultimo: '20-03-2026' },
  { nombre: 'Camila Reyes',     email: 'creyes@grupoam.cl',    rol: 'abastecimiento', activo: false, ultimo: '10-02-2026' },
]

export default function UsuariosPage() {
  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-2xl font-bold" style={{ color: 'var(--text-primary)' }}>Usuarios y Roles</h1>
          <p className="text-sm mt-1" style={{ color: 'var(--text-muted)' }}>
            Gestión de accesos — 6 roles · Control por módulo
          </p>
        </div>
        <button className="btn-primary flex items-center justify-center gap-2 w-full sm:w-auto shrink-0">
          <Plus className="w-4 h-4" /> Invitar Usuario
        </button>
      </div>

      {/* Roles reference */}
      <div className="rounded-2xl border overflow-hidden" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
        <div className="px-4 py-3 border-b" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
          <h2 className="font-semibold text-sm flex items-center gap-2" style={{ color: 'var(--text-primary)' }}>
            <Shield className="w-4 h-4 text-brand-400" /> Roles del sistema
          </h2>
        </div>
        <div className="divide-y" style={{ borderColor: 'var(--border)' }}>
          {Object.entries(ROLES).map(([key, r]) => (
            <div key={key} className="flex flex-col sm:flex-row sm:items-center gap-3 px-4 py-3">
              <span className={`inline-flex items-center px-2.5 py-1 rounded-full text-xs font-bold border shrink-0 w-fit ${r.cls}`}>
                {r.label}
              </span>
              <p className="text-xs" style={{ color: 'var(--text-muted)' }}>{r.modulos}</p>
            </div>
          ))}
        </div>
      </div>

      {/* Search */}
      <div className="relative max-w-sm">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4" style={{ color: 'var(--text-faint)' }} />
        <input className="input pl-9 w-full" placeholder="Buscar por nombre o email…" />
      </div>

      {/* Users table */}
      <div className="rounded-2xl border overflow-hidden" style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b" style={{ borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }}>
                {['Usuario','Email','Rol','Estado','Último acceso','Acciones'].map(h => (
                  <th key={h} className="text-left px-4 py-3 text-xs font-semibold uppercase tracking-wider whitespace-nowrap" style={{ color: 'var(--text-faint)' }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y" style={{ borderColor: 'var(--border)' }}>
              {DEMO_USERS.map(u => {
                const role = ROLES[u.rol]
                return (
                  <tr key={u.email} className="hover:bg-[var(--surface-200)] transition-colors">
                    <td className="px-4 py-3 font-medium" style={{ color: 'var(--text-primary)' }}>
                      <div className="flex items-center gap-3">
                        <div className="w-8 h-8 rounded-full bg-brand-500/10 flex items-center justify-center shrink-0">
                          <span className="text-xs font-bold text-brand-400">{u.nombre.split(' ').map(n=>n[0]).join('').slice(0,2)}</span>
                        </div>
                        {u.nombre}
                      </div>
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-1.5 text-xs" style={{ color: 'var(--text-muted)' }}>
                        <Mail className="w-3.5 h-3.5" /> {u.email}
                      </div>
                    </td>
                    <td className="px-4 py-3">
                      <span className={`inline-flex items-center px-2.5 py-1 rounded-full text-xs font-bold border ${role.cls}`}>
                        {role.label}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      {u.activo
                        ? <span className="inline-flex items-center gap-1 text-xs text-emerald-500"><CheckCircle2 className="w-3.5 h-3.5" />Activo</span>
                        : <span className="inline-flex items-center gap-1 text-xs text-gray-400"><XCircle className="w-3.5 h-3.5" />Inactivo</span>
                      }
                    </td>
                    <td className="px-4 py-3 text-xs" style={{ color: 'var(--text-muted)' }}>{u.ultimo}</td>
                    <td className="px-4 py-3">
                      <div className="flex gap-2">
                        <button className="btn-secondary text-xs px-3 py-1">Editar</button>
                        <button className="text-xs px-3 py-1 rounded-lg text-red-400 hover:bg-red-500/10 transition-colors border border-red-400/20">
                          {u.activo ? 'Desactivar' : 'Activar'}
                        </button>
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </div>

      <div className="flex items-center gap-2 p-3 rounded-xl bg-amber-500/10 border border-amber-500/20">
        <UserCog className="w-4 h-4 text-amber-400 shrink-0" />
        <p className="text-xs text-amber-400">Módulo en desarrollo — datos de demostración. El control de permisos por módulo, log de auditoría y autenticación por área estarán disponibles en la siguiente versión.</p>
      </div>
    </div>
  )
}
