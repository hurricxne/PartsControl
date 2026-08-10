import { useState, useRef, useEffect } from 'react'
import { Outlet, NavLink, useNavigate, useLocation } from 'react-router-dom'
import {
  LayoutDashboard, LogOut, User, ChevronRight,
  Activity, Bell, Sun, Moon, Menu, PanelLeftClose, PanelLeftOpen,
  FileText, TrendingUp, Ship, DollarSign, Users, BarChart3,
  ShoppingCart, Package, Receipt, Settings, UserCog,
  FileSignature, PackageSearch, Banknote, Calculator, Building2, Anchor, Truck, Wallet, Landmark,
  BookOpenCheck,
} from 'lucide-react'
import { useAuthStore } from '../stores/authStore'
import { useTheme } from '../context/ThemeContext'
import { comprasAPI } from '../services/api'
import NotificationBell from '../components/NotificationBell'
import toast from 'react-hot-toast'

interface NavItem {
  to: string
  icon: React.ElementType
  label: string
  exact: boolean
  badgeKey?: 'compras' | 'seguimiento' | 'embarques'
}

interface NavGroup {
  label: string
  items: NavItem[]
}

const navGroups: NavGroup[] = [
  {
    label: 'Menú',
    items: [
      { to: '/', icon: LayoutDashboard, label: 'Dashboard', exact: true },
    ],
  },
  {
    label: 'Comercial',
    items: [
      { to: '/cot-formales',  icon: FileText,      label: 'Cotizaciones',   exact: false },
      { to: '/cierre-venta',  icon: FileSignature, label: 'Cierre de Venta',exact: false },
      { to: '/ventas',        icon: TrendingUp,    label: 'Ventas',         exact: false },
    ],
  },
  {
    label: 'Abastecimiento',
    items: [
      { to: '/compras',      icon: ShoppingCart,  label: 'Panel Compras', exact: false, badgeKey: 'compras'     },
      { to: '/seguimiento',  icon: PackageSearch, label: 'Seguimiento',   exact: false, badgeKey: 'seguimiento' },
      { to: '/control-tc',   icon: DollarSign,    label: 'Control TC',    exact: false },
    ],
  },
  {
    label: 'Logística',
    items: [
      { to: '/embarques',      icon: Anchor, label: 'Pre-Embarques', exact: false, badgeKey: 'embarques' },
      { to: '/embarques-list', icon: Ship,   label: 'Embarques',     exact: false },
      { to: '/bodega',         icon: Package, label: 'Bodega',       exact: false },
      { to: '/despachos',      icon: Truck,   label: 'Despachos',    exact: false },
    ],
  },
  {
    label: 'Contabilidad',
    items: [
      { to: '/ventas-contab',      icon: Banknote,    label: 'Ventas',           exact: false },
      { to: '/embarques-pricing',  icon: Calculator,  label: 'Embarques Pricing',exact: false },
      { to: '/facturas',           icon: Receipt,     label: 'Facturas y Cobranzas', exact: false },
      { to: '/compras-contab',     icon: Wallet,      label: 'Compras y Pagos', exact: false },
      { to: '/libro-sii',          icon: BookOpenCheck, label: 'Libro SII',     exact: false },
      { to: '/tesoreria',          icon: Landmark,    label: 'Tesorería',       exact: false },
    ],
  },
  {
    label: 'Gerencia',
    items: [
      { to: '/reportes', icon: BarChart3, label: 'Reportes', exact: false },
    ],
  },
  {
    label: 'Admin',
    items: [
      { to: '/clientes',      icon: Users,     label: 'Clientes',       exact: false },
      { to: '/proveedores',   icon: Building2, label: 'Proveedores',    exact: false },
      { to: '/configuracion', icon: Settings,  label: 'Configuración', exact: false },
      { to: '/usuarios',      icon: UserCog,   label: 'Usuarios',       exact: false },
    ],
  },
]

const pageLabels: Record<string, string> = {
  '/':                    'Dashboard',
  '/cotizaciones':        'Valida Cotización',
  '/cotizaciones/editor': 'Cotizador Editor',
  '/cot-formales':        'Cotizaciones',
  '/cierre-venta':        'Cierre de Venta',
  '/clientes':            'Clientes',
  '/ventas':              'Ventas',
  '/compras':             'Panel Compras',
  '/seguimiento':         'Seguimiento',
  '/control-tc':          'Control TC',
  '/embarques':           'Pre-Embarques',
  '/embarques-list':      'Embarques',
  '/bodega':              'Bodega',
  '/despachos':           'Despachos',
  '/ventas-contab':       'Ventas — Contabilidad',
  '/embarques-pricing':   'Embarques Pricing',
  '/facturas':            'Facturas y Cobranzas',
  '/compras-contab':      'Compras y Cuentas por Pagar',
  '/libro-sii':           'Libro de Compras SII',
  '/tesoreria':           'Tesorería',
  '/reportes':            'Reportes',
  '/proveedores':         'Proveedores',
  '/configuracion':       'Configuración',
  '/usuarios':            'Usuarios y Roles',
}

const MOCK_NOTIFICATIONS = [
  { id: 1, title: 'Worker local activo',    desc: 'Scraping disponible en este equipo', time: 'ahora',    dot: 'bg-green-400',  unread: true  },
  { id: 2, title: 'PartsControl v2.0',         desc: 'Sistema actualizado correctamente',  time: 'hace 1h',  dot: 'bg-brand-400',  unread: true  },
]

export default function DashboardLayout() {
  const { user, logout } = useAuthStore()
  const navigate = useNavigate()
  const location = useLocation()
  const { theme, toggleTheme } = useTheme()

  const [collapsed, setCollapsed] = useState(false)
  const [mobileOpen, setMobileOpen] = useState(false)
  const [bellOpen, setBellOpen] = useState(false)
  const [userOpen, setUserOpen] = useState(false)
  const [counts, setCounts] = useState({ compras: 0, seguimiento: 0, embarques: 0 })

  const bellRef = useRef<HTMLDivElement>(null)
  const userRef = useRef<HTMLDivElement>(null)

  // Load counts for badges
  useEffect(() => {
    const load = () => {
      comprasAPI.getCounts()
        .then(({ data }) => setCounts(data))
        .catch(() => {}) // silent fail
    }
    load()
    const interval = setInterval(load, 30000)
    return () => clearInterval(interval)
  }, [])

  // Handle dynamic routes (e.g. /cotizaciones/:id/editor)
  const dynamicLabel =
    /^\/cotizaciones\/\d+\/editor/.test(location.pathname) ? 'Cotizador Editor' : null

  const pageLabel = dynamicLabel ?? (Object.entries(pageLabels)
    .sort((a, b) => b[0].length - a[0].length)
    .find(([k]) => k === '/' ? location.pathname === '/' : location.pathname.startsWith(k))?.[1]
    ?? 'Panel de control')

  // Close dropdowns on outside click
  useEffect(() => {
    function handler(e: MouseEvent) {
      if (bellRef.current && !bellRef.current.contains(e.target as Node)) setBellOpen(false)
      if (userRef.current && !userRef.current.contains(e.target as Node)) setUserOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  const handleLogout = () => {
    logout()
    toast.success('Sesión cerrada')
    navigate('/login', { replace: true })
  }

  return (
    <div className="min-h-screen" style={{ backgroundColor: 'var(--surface)' }}>
      {/* Mobile overlay */}
      {mobileOpen && (
        <div className="fixed inset-0 bg-black/60 z-30 md:hidden" onClick={() => setMobileOpen(false)} />
      )}

      {/* Sidebar */}
      <aside className={`
        fixed h-full z-40 flex flex-col transition-all duration-300 ease-in-out
        sidebar-bg border-r
        w-64 md:translate-x-0
        ${mobileOpen ? 'translate-x-0' : '-translate-x-full'}
        ${collapsed ? 'md:w-16' : 'md:w-64'}
      `}>
        {/* Logo */}
        <div className={`border-b p-5 ${collapsed ? 'md:p-3' : ''}`} style={{ borderColor: 'var(--border)' }}>
          <div className={`flex items-center gap-3 ${collapsed ? 'md:justify-center' : ''}`}>
            <img src="/logo.png" alt="PartsControl" className="w-9 h-9 object-contain drop-shadow-sm shrink-0" />
            <div className={collapsed ? 'md:hidden' : ''}>
              <span className="font-bold text-base tracking-tight" style={{ color: 'var(--text-primary)' }}>PartsControl</span>
              <div className="flex items-center gap-1 mt-0.5">
                <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
                <span className="text-xs" style={{ color: 'var(--text-faint)' }}>En línea</span>
              </div>
            </div>
          </div>
        </div>

        {/* Nav */}
        <nav className="flex-1 p-3 overflow-y-auto space-y-4">
          {navGroups.map((group) => (
            <div key={group.label}>
              <p className={`text-[10px] font-semibold uppercase tracking-widest px-3 mb-1 mt-1 ${collapsed ? 'md:hidden' : ''}`}
                style={{ color: 'var(--text-faint)' }}>
                {group.label}
              </p>
              <div className="space-y-0.5">
                {group.items.map(({ to, icon: Icon, label, exact, badgeKey }) => {
                  const badgeCount = badgeKey ? counts[badgeKey] : 0
                  return (
                    <NavLink
                      key={to}
                      to={to}
                      end={exact}
                      title={collapsed ? label : undefined}
                      onClick={() => setMobileOpen(false)}
                      className={({ isActive }) => `
                        flex items-center gap-3 px-3 py-2.5 rounded-xl text-sm font-medium transition-all duration-200
                        ${collapsed ? 'md:justify-center md:gap-0 md:px-0' : ''}
                        ${isActive
                          ? 'bg-brand-600/15 text-brand-500 border border-brand-500/20'
                          : 'hover:bg-[var(--surface-200)]'
                        }
                      `}
                      style={({ isActive }) => isActive ? {} : { color: 'var(--text-muted)' }}
                    >
                      {({ isActive }) => (
                        <>
                          <Icon className="w-4 h-4 shrink-0" />
                          <span className={collapsed ? 'md:hidden' : ''}>{label}</span>
                          {isActive && !collapsed && !badgeCount && <ChevronRight className="w-3 h-3 ml-auto text-brand-500" />}
                          {badgeCount > 0 && !collapsed && (
                            <span className="ml-auto text-[10px] font-bold px-1.5 py-0.5 rounded-full bg-brand-500 text-white">
                              {badgeCount}
                            </span>
                          )}
                          {badgeCount > 0 && collapsed && (
                            <span className="absolute right-1 top-1 w-2 h-2 rounded-full bg-brand-500" />
                          )}
                        </>
                      )}
                    </NavLink>
                  )
                })}
              </div>
            </div>
          ))}
        </nav>

        {/* User info */}
        <div className="p-3 border-t" style={{ borderColor: 'var(--border)' }}>
          <div
            className={`flex items-center gap-3 px-3 py-2.5 rounded-xl ${collapsed ? 'md:justify-center md:px-0' : ''}`}
            style={{ backgroundColor: 'var(--surface-200)' }}
            title={collapsed ? user?.nombre : undefined}
          >
            <div className="w-8 h-8 rounded-full bg-gradient-to-br from-brand-500 to-brand-700 flex items-center justify-center shrink-0">
              <User className="w-4 h-4 text-white" />
            </div>
            <div className={`flex-1 min-w-0 ${collapsed ? 'md:hidden' : ''}`}>
              <p className="text-sm font-medium truncate" style={{ color: 'var(--text-primary)' }}>{user?.nombre}</p>
              <p className="text-xs truncate" style={{ color: 'var(--text-faint)' }}>{user?.email}</p>
            </div>
          </div>
        </div>
      </aside>

      {/* Main content — bloque normal; sidebar fixed no está en flujo flex */}
      <main className={`min-h-screen flex flex-col transition-all duration-300 ${collapsed ? 'md:ml-16' : 'md:ml-64'}`}>
        {/* Top bar */}
        <header className="h-16 border-b sticky top-0 z-20 flex items-center px-4 md:px-6 gap-3 header-bg backdrop-blur"
          style={{ borderColor: 'var(--border)' }}>
          {/* Mobile hamburger */}
          <button
            className="md:hidden rounded-lg p-1.5 transition-colors hover:bg-[var(--surface-200)]"
            style={{ color: 'var(--text-muted)' }}
            onClick={() => setMobileOpen(true)}
          >
            <Menu className="w-5 h-5" />
          </button>

          {/* Sidebar collapse toggle (desktop) */}
          <button
            onClick={() => setCollapsed(!collapsed)}
            title={collapsed ? 'Expandir sidebar' : 'Contraer sidebar'}
            className="hidden md:flex items-center justify-center w-8 h-8 rounded-lg transition-all duration-200 hover:bg-[var(--surface-200)]"
            style={{ color: 'var(--text-muted)' }}
          >
            {collapsed ? <PanelLeftOpen className="w-4 h-4" /> : <PanelLeftClose className="w-4 h-4" />}
          </button>

          {/* Breadcrumb — flex-1 + min-w-0 para no empujar acciones fuera */}
          <div className="flex items-center gap-2 text-sm flex-1 min-w-0">
            <Activity className="w-3.5 h-3.5 text-brand-500 hidden sm:block shrink-0" />
            <span className="hidden sm:inline shrink-0" style={{ color: 'var(--text-faint)' }}>Panel</span>
            <span className="hidden sm:inline shrink-0" style={{ color: 'var(--text-faint)' }}>/</span>
            <span className="font-medium truncate" style={{ color: 'var(--text-primary)' }}>{pageLabel}</span>
          </div>

          {/* Right actions — shrink-0 para no ceder espacio nunca */}
          <div className="flex items-center gap-1 shrink-0">
            {/* Theme toggle */}
            <button
              onClick={toggleTheme}
              title={theme === 'dark' ? 'Modo claro' : 'Modo oscuro'}
              className="w-9 h-9 rounded-xl flex items-center justify-center transition-all duration-200 hover:bg-[var(--surface-200)]"
              style={{ color: 'var(--text-muted)' }}
            >
              {theme === 'dark' ? <Sun className="w-4 h-4" /> : <Moon className="w-4 h-4" />}
            </button>

            {/* Notifications */}
            <NotificationBell />
            {/* User menu */}
            <div className="relative ml-1" ref={userRef}>
              <button
                onClick={() => { setUserOpen(!userOpen); setBellOpen(false) }}
                className="flex items-center gap-2 pl-2 pr-3 py-1.5 rounded-xl transition-all duration-200 hover:bg-[var(--surface-200)]"
                style={{ color: 'var(--text-muted)' }}
              >
                <div className="w-7 h-7 rounded-full bg-gradient-to-br from-brand-500 to-brand-700 flex items-center justify-center shrink-0">
                  <User className="w-3.5 h-3.5 text-white" />
                </div>
                <span className="text-sm font-medium hidden sm:block" style={{ color: 'var(--text-primary)' }}>
                  {user?.nombre?.split(' ')[0]}
                </span>
              </button>

              {userOpen && (
                <div className="absolute right-0 top-12 w-52 rounded-2xl shadow-2xl border overflow-hidden z-50"
                  style={{ backgroundColor: 'var(--surface-100)', borderColor: 'var(--border)' }}>
                  <div className="px-4 py-3 border-b" style={{ borderColor: 'var(--border)' }}>
                    <p className="text-sm font-medium" style={{ color: 'var(--text-primary)' }}>{user?.nombre}</p>
                    <p className="text-xs mt-0.5" style={{ color: 'var(--text-faint)' }}>{user?.email}</p>
                  </div>
                  <button
                    onClick={handleLogout}
                    className="flex items-center gap-3 w-full px-4 py-3 text-sm transition-colors hover:bg-red-500/10 text-red-500"
                  >
                    <LogOut className="w-4 h-4" />
                    Cerrar sesión
                  </button>
                </div>
              )}
            </div>
          </div>
        </header>

        <div className="flex-1 p-6 animate-fade-in">
          <Outlet />
        </div>
        <footer className="border-t px-6 py-3 flex flex-col sm:flex-row items-center justify-between gap-1"
          style={{ borderColor: 'var(--border)' }}>
          <span className="text-xs" style={{ color: 'var(--text-faint)' }}>
            © {new Date().getFullYear()} PartsControl.
          </span>
          <span className="text-xs" style={{ color: 'var(--text-faint)' }}>
            Actualizado y Optimizado por{' '}
            <a href="https://www.bigcode.cl" target="_blank" rel="noopener noreferrer"
              className="text-brand-400 hover:text-brand-300 transition-colors">
              Bigcode
            </a>
          </span>
        </footer>
      </main>
    </div>
  )
}
