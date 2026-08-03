import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { Eye, EyeOff, ChevronRight, Wrench, Car } from 'lucide-react'
import toast from 'react-hot-toast'
import { authAPI } from '../services/api'
import { useAuthStore } from '../stores/authStore'

type Empresa = 'mineria' | 'automotriz'

const EMPRESAS = {
  mineria: {
    nombre: 'Grupo AM',
    descripcion: 'Maquinarias y motores para minería',
    tagline: 'Soluciones CAT para operaciones mineras',
    color: '#ea6c0d',
    colorHover: '#c45c0a',
    colorLight: 'rgba(234,108,13,0.15)',
    colorBorder: 'rgba(234,108,13,0.4)',
    gradient: 'linear-gradient(135deg, #431404 0%, #7c2d0c 40%, #ea6c0d 100%)',
    icon: Wrench,
    features: [
      { icon: '⛏️', title: 'Partes de motor', desc: 'Componentes originales CAT minería' },
      { icon: '🔧', title: 'Cotización inteligente', desc: 'Precios validados automáticamente' },
      { icon: '📦', title: 'Gestión de embarques', desc: 'Seguimiento completo de pedidos' },
    ],
  },
  automotriz: {
    nombre: 'Monzaparts',
    descripcion: 'Repuestos automotrices',
    tagline: 'Soluciones completas para flota automotriz',
    color: '#1550d4',
    colorHover: '#0e3faf',
    colorLight: 'rgba(21,80,212,0.15)',
    colorBorder: 'rgba(21,80,212,0.4)',
    gradient: 'linear-gradient(135deg, #031f6a 0%, #0e3faf 40%, #1a5cf0 100%)',
    icon: Car,
    features: [
      { icon: '🚗', title: 'Repuestos originales', desc: 'Catálogo automotriz completo' },
      { icon: '💰', title: 'Precios competitivos', desc: 'Comparativa automática de mercado' },
      { icon: '📊', title: 'Control de ventas', desc: 'Reportes y seguimiento comercial' },
    ],
  },
} as const

export default function LoginPage() {
  const navigate = useNavigate()
  const setAuth = useAuthStore((s) => s.setAuth)
  const [empresa, setEmpresa] = useState<Empresa>('mineria')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [showPass, setShowPass] = useState(false)
  const [loading, setLoading] = useState(false)

  const cfg = EMPRESAS[empresa]

  useEffect(() => {
    document.documentElement.setAttribute('data-empresa', empresa)
    return () => document.documentElement.removeAttribute('data-empresa')
  }, [empresa])

  const handleLogin = async (ev: React.FormEvent) => {
    ev.preventDefault()
    if (!email || !password) { toast.error('Completa todos los campos'); return }
    setLoading(true)
    try {
      const { data } = await authAPI.login(email, password)
      const userEmpresa = data.user.empresa || 'mineria'
      // Cuentas del equipo Bigcode (@bigcode.cl, ej. soporte) entran a AMBOS sistemas:
      // se les respeta la pestaña elegida en vez de exigir que coincida con su empresa.
      const esBigcode = (data.user.email || '').toLowerCase().endsWith('@bigcode.cl')
      if (!esBigcode && userEmpresa !== empresa) {
        toast.error(`Tu cuenta pertenece a ${EMPRESAS[userEmpresa as Empresa]?.nombre || userEmpresa}`)
        setLoading(false)
        return
      }
      setAuth(data.access_token, data.user)
      toast.success(`Bienvenido, ${data.user.nombre}`)
      const destino = esBigcode ? empresa : userEmpresa
      navigate(destino === 'automotriz' ? '/monzaparts/leads' : '/', { replace: true })
    } catch (err: any) {
      toast.error(err.response?.data?.detail || 'Error al iniciar sesión')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex">

      {/* ── Panel izquierdo — branding empresa (reactivo al selector) ── */}
      <div
        className="hidden lg:flex lg:w-[42%] relative overflow-hidden flex-col items-center justify-center p-12 transition-all duration-500"
        style={{ background: cfg.gradient }}
      >
        <div className="absolute -top-32 -left-32 w-80 h-80 bg-white/5 rounded-full blur-3xl" />
        <div
          className="absolute -bottom-24 -right-24 w-72 h-72 rounded-full blur-3xl transition-colors duration-500"
          style={{ backgroundColor: `${cfg.color}30` }}
        />

        <div className="relative z-10 text-center">
          <div className="mb-8 flex justify-center">
            <div className="w-48 h-48 flex items-center justify-center">
              <img src="/logo.png" alt="PartsControl" className="w-48 h-48 object-contain drop-shadow-lg" />
            </div>
          </div>
          <h1 className="text-4xl font-extrabold text-white mb-1 tracking-tight">PartsControl</h1>
          <p className="text-lg font-semibold mb-2" style={{ color: 'rgba(255,255,255,0.85)' }}>
            {cfg.nombre}
          </p>
          <p className="text-sm mb-12 text-white/60">{cfg.tagline}</p>

          <div className="space-y-3 text-left max-w-xs mx-auto">
            {cfg.features.map(f => (
              <div key={f.title} className="flex items-center gap-4 bg-white/8 backdrop-blur rounded-2xl px-4 py-3 ring-1 ring-white/10">
                <span className="text-2xl shrink-0">{f.icon}</span>
                <div>
                  <p className="text-white text-sm font-semibold">{f.title}</p>
                  <p className="text-xs text-white/60">{f.desc}</p>
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className="absolute bottom-6 left-0 right-0 flex justify-center">
          <span className="text-white/40 text-xs">Powered by BigCode · v2.0</span>
        </div>
      </div>

      {/* ── Panel derecho — formulario con selector integrado ── */}
      <div
        className="flex-1 flex flex-col items-center justify-center px-8 py-12 lg:px-16"
        style={{ backgroundColor: 'var(--surface)' }}
      >
        <div className="w-full max-w-md animate-fade-in">

          {/* Selector de empresa */}
          <div className="mb-8">
            <p className="text-xs font-semibold uppercase tracking-widest mb-3" style={{ color: 'var(--text-faint)' }}>
              Selecciona tu empresa
            </p>
            <div
              className="flex rounded-xl p-1 gap-1"
              style={{ backgroundColor: 'var(--surface-200)', border: '1px solid var(--border)' }}
            >
              {(Object.entries(EMPRESAS) as [Empresa, typeof EMPRESAS['mineria']][]).map(([key, e]) => {
                const Icon = e.icon
                const isActive = empresa === key
                return (
                  <button
                    key={key}
                    type="button"
                    onClick={() => setEmpresa(key)}
                    className="flex-1 flex items-center justify-center gap-2 py-2.5 px-3 rounded-lg text-sm font-semibold transition-all duration-200"
                    style={isActive ? {
                      backgroundColor: e.colorLight,
                      border: `1px solid ${e.colorBorder}`,
                      color: e.color,
                    } : {
                      backgroundColor: 'transparent',
                      border: '1px solid transparent',
                      color: 'var(--text-faint)',
                    }}
                  >
                    <Icon className="w-4 h-4 flex-shrink-0" />
                    <span>{e.nombre}</span>
                  </button>
                )
              })}
            </div>
          </div>

          {/* Cabecera */}
          <div className="mb-8">
            <div className="flex items-center gap-2 mb-2">
              <div className="w-2 h-2 rounded-full transition-colors duration-300" style={{ backgroundColor: cfg.color }} />
              <p className="text-sm font-semibold uppercase tracking-widest transition-colors duration-300" style={{ color: cfg.color }}>
                {cfg.nombre}
              </p>
            </div>
            <h2 className="text-3xl font-bold tracking-tight" style={{ color: 'var(--text-primary)' }}>
              Iniciar sesión
            </h2>
            <p className="mt-2 text-sm" style={{ color: 'var(--text-muted)' }}>
              Ingresa tus credenciales para acceder al sistema
            </p>
          </div>

          <div className="flex items-center gap-2 mb-6 px-4 py-2.5 rounded-xl ring-1" style={{ backgroundColor: 'var(--surface-200)' }}>
            <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse shrink-0" />
            <span className="text-xs" style={{ color: 'var(--text-muted)' }}>Sistema</span>
            <span className="text-xs font-semibold text-emerald-500 ml-auto">Conectado</span>
          </div>

          <form onSubmit={handleLogin} className="space-y-5">
            <div>
              <label className="block text-sm font-medium mb-1.5" style={{ color: 'var(--text-muted)' }}>Email</label>
              <input
                type="email"
                className="input"
                placeholder="usuario@empresa.cl"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                autoComplete="email"
              />
            </div>

            <div>
              <label className="block text-sm font-medium mb-1.5" style={{ color: 'var(--text-muted)' }}>Contraseña</label>
              <div className="relative">
                <input
                  type={showPass ? 'text' : 'password'}
                  className="input pr-11"
                  placeholder="••••••••"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  autoComplete="current-password"
                />
                <button
                  type="button"
                  onClick={() => setShowPass(!showPass)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 transition-colors"
                  style={{ color: 'var(--text-faint)' }}
                >
                  {showPass ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                </button>
              </div>
            </div>

            <button
              type="submit"
              disabled={loading}
              className="btn-primary w-full justify-center py-3 mt-2"
            >
              {loading ? (
                <>
                  <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                  Ingresando...
                </>
              ) : (
                <>
                  Ingresar al sistema
                  <ChevronRight className="w-4 h-4" />
                </>
              )}
            </button>
          </form>

          <p className="text-center text-xs mt-10" style={{ color: 'var(--text-faint)' }}>
            PartsControl v2.0 · Powered by <span className="font-medium" style={{ color: cfg.color }}>BigCode</span>
          </p>
        </div>
      </div>
    </div>
  )
}
