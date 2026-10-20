import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { Eye, EyeOff, ChevronRight, Wrench, Car, AlertCircle } from 'lucide-react'
import toast from 'react-hot-toast'
import { authAPI } from '../services/api'
import { useAuthStore, esCuentaPortal } from '../stores/authStore'
import firma from '../assets/gam/firma-oscuro.svg'
import foto from '../assets/gam/login.jpg'
import './LoginPage.gam.css'

type Empresa = 'mineria' | 'automotriz'

// El diseño es el de GAM Parts para las dos empresas: la pestaña solo cambia los textos.
const EMPRESAS = {
  mineria: {
    nombre: 'Grupo AM',
    icon: Wrench,
    titulo: ['Que tu producción', 'no se detenga.'],
    texto: 'Repuestos y componentes para minería: cotizaciones, compras, embarques, bodega y despachos en un solo sistema.',
    chips: [['Repuestos', 'genuinos, originales y OEM'], ['Cotización', 'precios validados'], ['Embarques', 'seguimiento completo']],
    sub: 'Plataforma de gestión comercial y de operaciones.',
  },
  automotriz: {
    nombre: 'Monzaparts',
    icon: Car,
    titulo: ['Tu flota', 'siempre en ruta.'],
    texto: 'Repuestos automotrices: leads, cotizaciones, ventas y post-venta, de la compra al despacho.',
    chips: [['Repuestos', 'catálogo automotriz'], ['Precios', 'comparativa de mercado'], ['Ventas', 'control y seguimiento']],
    sub: 'CRM y cotizador de repuestos automotrices.',
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
  // Aviso FIJO para el 403 del login (cuenta del portal suspendida por inactividad). No
  // va en un toast: el mensaje trae la instrucción de qué hacer ("pide a tu ejecutivo que
  // la reactive") y un toast de 4 segundos se va antes de que el cliente lo lea.
  const [avisoCuenta, setAvisoCuenta] = useState('')

  const cfg = EMPRESAS[empresa]

  useEffect(() => {
    document.documentElement.setAttribute('data-empresa', empresa)
    return () => document.documentElement.removeAttribute('data-empresa')
  }, [empresa])

  const handleLogin = async (ev: React.FormEvent) => {
    ev.preventDefault()
    if (!email || !password) { toast.error('Completa todos los campos'); return }
    setLoading(true)
    setAvisoCuenta('')
    try {
      const { data } = await authAPI.login(email, password)
      // Cuenta del Portal de Clientes: entra por este mismo login y va directo a /portal.
      // ANTES del chequeo del selector de marca: ese selector es del personal, el
      // cliente no sabe (ni tiene por qué saber) cuál pestaña le corresponde, y con la
      // pestaña equivocada quedaría rechazado con un mensaje que no entiende.
      if (esCuentaPortal(data.user)) {
        setAuth(data.access_token, data.user)
        toast.success(`Bienvenido, ${data.user.nombre}`)
        navigate('/portal', { replace: true })
        return
      }
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
      const detail = err.response?.data?.detail
      // 403 en el login = la clave ERA correcta pero la cuenta no puede entrar (portal
      // suspendido por inactividad). El backend explica el motivo y el siguiente paso:
      // se muestra tal cual y se deja en pantalla. El resto sigue como siempre.
      if (err.response?.status === 403 && typeof detail === 'string' && detail) {
        setAvisoCuenta(detail)
      } else {
        toast.error(detail || 'Error al iniciar sesión')
      }
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="gl">
      <aside className="gl-side" style={{ '--gl-foto': `url(${foto})` } as React.CSSProperties} aria-hidden="true">
        <img className="gl-firma" src={firma} alt="" />
        <div>
          <h1>{cfg.titulo[0]}<br />{cfg.titulo[1]}</h1>
          <p className="gl-texto">{cfg.texto}</p>
          <div className="gl-chips">
            {cfg.chips.map(([titulo, detalle]) => (
              <div key={titulo} className="gl-chip"><b>{titulo}</b><span>{detalle}</span></div>
            ))}
          </div>
        </div>
        <div className="gl-pie">
          <p className="gl-lema">Solución · Rapidez · Calidad</p>
          <p className="gl-marca">GAM Parts es una marca de Grupo AM SpA · gamparts.cl</p>
        </div>
        <i className="gl-canal" />
      </aside>

      <main className="gl-main">
        <div className="gl-caja">
          <img className="gl-logo-movil" src={firma} alt="GAM Parts by Grupo AM" />

          <p className="gl-ey" id="gl-empresa-t">Selecciona tu empresa</p>
          <div className="gl-sel" role="group" aria-labelledby="gl-empresa-t">
            {(Object.entries(EMPRESAS) as [Empresa, typeof EMPRESAS[Empresa]][]).map(([key, e]) => {
              const Icon = e.icon
              return (
                <button key={key} type="button" className="gl-op" aria-pressed={empresa === key} onClick={() => setEmpresa(key)}>
                  <Icon aria-hidden="true" />
                  {e.nombre}
                </button>
              )
            })}
          </div>

          <p className="gl-marca-ey"><span className="gl-punto" aria-hidden="true" />{cfg.nombre}</p>
          <h2>Iniciar sesión</h2>
          <p className="gl-sub">{cfg.sub}</p>

          <div className="gl-estado">
            <span className="gl-estado-l"><span className="gl-luz" aria-hidden="true" />Sistema</span>
            <span className="gl-estado-v">Conectado</span>
          </div>

          <form className="gl-form" onSubmit={handleLogin}>
            <label htmlFor="gl-correo">Correo</label>
            <input
              id="gl-correo"
              type="email"
              className="gl-campo"
              placeholder="usuario@empresa.cl"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoComplete="email"
              inputMode="email"
              autoCapitalize="none"
              spellCheck={false}
            />

            <label htmlFor="gl-clave">Contraseña</label>
            <div className="gl-clave">
              <input
                id="gl-clave"
                type={showPass ? 'text' : 'password'}
                className="gl-campo"
                placeholder="••••••••"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
              />
              <button
                type="button"
                className="gl-ojo"
                onClick={() => setShowPass(!showPass)}
                aria-label={showPass ? 'Ocultar contraseña' : 'Mostrar contraseña'}
                aria-pressed={showPass}
              >
                {showPass ? <EyeOff className="w-5 h-5" /> : <Eye className="w-5 h-5" />}
              </button>
            </div>

            {avisoCuenta && (
              <div role="alert" className="gl-aviso">
                <AlertCircle aria-hidden="true" />
                <span>{avisoCuenta}</span>
              </div>
            )}

            <button type="submit" className="gl-ingresar" disabled={loading}>
              {loading ? (<><span className="gl-giro" aria-hidden="true" />Ingresando…</>) : (<>Ingresar al sistema<ChevronRight aria-hidden="true" /></>)}
            </button>
          </form>
        </div>
        <p className="gl-pie-form">PartsControl v2.0 · Powered by <a href="https://bigcode.cl" target="_blank" rel="noopener noreferrer">BigCode</a></p>
      </main>
    </div>
  )
}
