// Cascarón del Portal de Clientes (ruta /portal y todas sus hijas). Es lo único que ve
// el contacto de un cliente al entrar con su cuenta tipo_acceso='portal': cabecera, la
// pantalla de turno (<Outlet>) y pie.
//
// QUÉ HACE ADEMÁS DE ENMARCAR
//   · Pide GET /api/portal/yo UNA vez por sesión y lo reparte a las pantallas por el
//     contexto del Outlet (sesion.ts::usePortal). Ninguna pantalla vuelve a pedirlo.
//   · Mientras `yo.debe_cambiar_clave` sea true NO monta ninguna pantalla: muestra el
//     cambio de clave. El backend responde 403 a todas las rutas de datos hasta que la
//     clave temporal se reemplace, así que montar una pantalla solo mostraría errores.
//   · Cierra la sesión limpiando lo que queda en memoria (ver cerrarSesion).
//   · La primera vez que una cuenta portal entra en un navegador pasa a tema claro
//     (ver useTemaClaroLaPrimeraVez).
//   · Al cambiar de pantalla sube al inicio y deja el foco en <main>, para que un lector
//     de pantalla anuncie el contenido nuevo. Las pantallas no mueven el foco al montarse.
//
// QUÉ NO USA, A PROPÓSITO: DashboardLayout ni nada del cascarón interno (menú, campana de
// notificaciones, contadores de Compras). Esas piezas piden endpoints del ERP, que a una
// cuenta portal le responden 403, y le mostrarían al cliente la forma del sistema por
// dentro. Tampoco usa el logo de PartsControl: ese es el nombre de la herramienta
// interna; la marca que el cliente conoce es MachParts.
//
// LA SEGURIDAD NO ESTÁ ACÁ: el backend filtra cada respuesta por los clientes de la
// cuenta. Este archivo solo decide qué se pinta.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Outlet, useLocation, useNavigate, useNavigationType } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowRight, LogOut } from 'lucide-react'
import toast from 'react-hot-toast'
import { useAuthStore } from '../stores/authStore'
import { useTheme } from '../context/ThemeContext'
import { portalAPI, detalleDe, estadoHttp } from './api'
import type { PortalYo } from './types'
import { limpiarArchivosDelPortal } from './archivos'
import {
  claveDeConsulta, prepararConsultasDelPortal, useTituloDeFondo, RAIZ_DE_CONSULTAS,
  type ContextoPortal,
} from './sesion'
import { Aviso, Cargando, ErrorDeCarga } from './piezas'
import CabeceraPortal from './CabeceraPortal'
import CambiarClaveForm from './CambiarClaveForm'
import './portal.css'

// Marca de que este navegador ya pasó por el portal al menos una vez.
const CLAVE_TEMA_ELEGIDO = 'machparts-portal-tema-elegido'
const CUENTA_BLOQUEADA = 'Tu cuenta no puede usar el portal en este momento. Contacta a tu ejecutivo de MachParts.'

/**
 * ThemeContext arranca en oscuro cuando no hay preferencia guardada y, fuera del
 * portal, su único botón está dentro del ERP. El portal se usa a pleno sol: la PRIMERA vez que se entra en un
 * navegador se pasa a claro y se deja la marca; desde ahí manda lo que la persona elija
 * con el botón de la cabecera. La marca se escribe ANTES de cambiar el tema: así el
 * segundo montaje de StrictMode (desarrollo) no lo vuelve a cambiar.
 */
function useTemaClaroLaPrimeraVez(): void {
  const { theme, toggleTheme } = useTheme()
  useEffect(() => {
    try {
      if (localStorage.getItem(CLAVE_TEMA_ELEGIDO) !== null) return
      localStorage.setItem(CLAVE_TEMA_ELEGIDO, '1')
    } catch {
      // Sin localStorage (modo privado estricto) no hay cómo recordar la elección:
      // forzar el claro en cada entrada pelearía con el botón. Se deja como esté.
      return
    }
    if (theme === 'dark') toggleTheme()
    // Solo al montar: después de eso el tema es de la persona.
  }, [])
}

export default function PortalLayout() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { pathname } = useLocation()
  const tipoDeNavegacion = useNavigationType()
  const logout = useAuthStore((s) => s.logout)
  // PortalRoute (App.tsx) garantiza que hay usuario; el 0 solo cubre el instante en que
  // la sesión se cierra y este componente todavía no se desmonta.
  const usuarioId = useAuthStore((s) => s.user?.id ?? 0)
  const refContenido = useRef<HTMLElement>(null)
  // true desde que el backend confirma el cambio de la clave temporal hasta que la
  // persona toca «Continuar»: deja leer la confirmación antes de mostrar el portal.
  const [claveRecienCambiada, setClaveRecienCambiada] = useState(false)

  useTemaClaroLaPrimeraVez()
  useTituloDeFondo('Portal de Clientes')
  prepararConsultasDelPortal(queryClient)

  const claveYo = useMemo(() => claveDeConsulta(usuarioId, 'yo'), [usuarioId])
  const consulta = useQuery({
    queryKey: claveYo,
    queryFn: async () => (await portalAPI.yo()).data,
    // Una vez por sesión: quién es y qué empresas representa no cambia mientras navega.
    staleTime: Infinity,
    // Sin usuario no hay token que enviar (el instante entre cerrar la sesión y que este
    // componente se desmonte): no se pide nada.
    enabled: usuarioId > 0,
  })
  const yo = consulta.data
  const { refetch } = consulta

  // Al cambiar de PANTALLA (no de filtros: eso es `search` y el foco se queda donde
  // está). «Atrás» y «Adelante» conservan el desplazamiento que restaura el navegador.
  const rutaAnterior = useRef(pathname)
  useEffect(() => {
    if (rutaAnterior.current === pathname) return
    rutaAnterior.current = pathname
    // 'instant' y no el scroll-behavior:smooth global: con prefers-reduced-motion el
    // salto animado entre pantallas es justo lo que la persona pidió evitar.
    if (tipoDeNavegacion !== 'POP') window.scrollTo({ top: 0, left: 0, behavior: 'instant' })
    refContenido.current?.focus({ preventScroll: true })
  }, [pathname, tipoDeNavegacion])

  const cerrarSesion = useCallback(() => {
    logout()
    // La salida navega por la SPA sin recargar: lo que quede en memoria lo hereda quien
    // use el computador después (en faena un equipo lo comparten varias personas). Se
    // sueltan los archivos del portal y TODAS las respuestas del portal guardadas por
    // react-query.
    limpiarArchivosDelPortal()
    queryClient.removeQueries({ queryKey: [RAIZ_DE_CONSULTAS] })
    toast.success('Sesión cerrada')
    navigate('/login', { replace: true })
  }, [logout, queryClient, navigate])

  const alCambiarClave = useCallback(() => {
    setClaveRecienCambiada(true)
    // No se vuelve a pedir /portal/yo: el 200 del cambio de clave YA es la confirmación.
    // Con mala señal, un nuevo GET que falle dejaría a la persona atrapada en «cambia tu
    // clave» con la clave ya cambiada.
    queryClient.setQueryData<PortalYo>(claveYo, (previo) => previo && { ...previo, debe_cambiar_clave: false })
  }, [queryClient, claveYo])

  const contexto = useMemo<ContextoPortal | null>(
    () => yo
      ? { yo, veMontos: yo.perfil === 'procurement', usuarioId }
      : null,
    [yo, usuarioId]
  )

  const pideClave = !!yo && (yo.debe_cambiar_clave || claveRecienCambiada)
  const portalListo = contexto !== null && !pideClave

  return (
    <div className="portal-root">
      <a
        href="#portal-contenido"
        className="portal-salto"
        // Se enfoca a mano: seguir el ancla agregaría «#portal-contenido» al historial y
        // «Atrás» dejaría de salir de la pantalla al primer intento.
        onClick={(ev) => { ev.preventDefault(); refContenido.current?.focus() }}
      >
        Saltar al contenido
      </a>
      <CabeceraPortal conNavegacion={portalListo} onCerrarSesion={cerrarSesion} />

      <main
        id="portal-contenido"
        ref={refContenido}
        tabIndex={-1}
        className="portal-foco-silencioso flex-1 mx-auto w-full max-w-6xl px-4 sm:px-6 py-6 sm:py-10"
      >
        {/* Se decide por `yo` y no por el estado de la consulta: si un «recargar» falla,
            react-query marca error pero conserva los datos, y el portal sigue en pie. */}
        {portalListo && <Outlet context={contexto} />}
        {pideClave && (
          <ClaveTemporal
            cambiada={claveRecienCambiada}
            onCambiada={alCambiarClave}
            onContinuar={() => setClaveRecienCambiada(false)}
          />
        )}
        {!yo && consulta.isPending && <Cargando texto="Cargando tu información…" />}
        {!yo && consulta.isError && (
          // 403: la sesión es válida pero la cuenta no puede usar el portal (suspendida por
          // inactividad o sin cliente asignado); el backend explica el motivo y el paso
          // siguiente. El 401 no llega acá: lo resuelve services/authErrors.ts.
          estadoHttp(consulta.error) === 403
            ? <CuentaBloqueada motivo={detalleDe(consulta.error) ?? CUENTA_BLOQUEADA} onCerrarSesion={cerrarSesion} />
            : <ErrorDeCarga nivel={1} titulo="No pudimos cargar tu información" onReintentar={() => { void refetch() }} />
        )}
      </main>

      <footer className="portal-pie">
        <div className="mx-auto w-full max-w-6xl px-4 sm:px-6 py-4 text-sm">
          <p>MachParts · Grupo AM</p>
          <p className="mt-1">
            Este portal es de consulta. Para cambios en un pedido, contacta a tu ejecutivo de MachParts.
          </p>
        </div>
      </footer>
    </div>
  )
}

interface ClaveTemporalProps {
  cambiada: boolean
  onCambiada: () => void
  onContinuar: () => void
}

/**
 * La clave temporal viajó por correo o por teléfono y además VENCE: si el contacto no la
 * cambia, un día el login lo rechaza. Por eso es lo primero, antes de cualquier pantalla.
 */
function ClaveTemporal({ cambiada, onCambiada, onContinuar }: ClaveTemporalProps) {
  const refConfirmacion = useRef<HTMLHeadingElement>(null)

  // El formulario se retira al confirmar y con él se va el botón que tenía el foco: se
  // lleva al título nuevo para que un lector de pantalla lea la confirmación.
  useEffect(() => { if (cambiada) refConfirmacion.current?.focus() }, [cambiada])

  if (cambiada) {
    return (
      <section className="portal-entrada max-w-xl">
        <p className="portal-rotulo">Listo</p>
        <h1 ref={refConfirmacion} tabIndex={-1} className="portal-foco-silencioso mt-2 text-2xl sm:text-3xl font-extrabold tracking-tight">
          Tu clave quedó cambiada
        </h1>
        <p className="portal-suave mt-3 text-base">
          Úsala la próxima vez que ingreses. Si la olvidas, tu ejecutivo de MachParts puede generarte una nueva.
        </p>
        <button type="button" onClick={onContinuar} className="portal-boton mt-6 w-full sm:w-auto">
          Continuar al portal
          <ArrowRight className="w-4 h-4" aria-hidden="true" />
        </button>
      </section>
    )
  }

  return (
    <section className="portal-entrada max-w-xl">
      <p className="portal-rotulo">Primer ingreso</p>
      <h1 className="mt-2 mb-5 text-2xl sm:text-3xl font-extrabold tracking-tight">
        Antes de continuar, cambia tu clave
      </h1>
      <Aviso tono="atencion">
        Estás usando la <strong>clave temporal</strong> que te entregó tu ejecutivo. Cámbiala por una
        que solo tú conozcas: la temporal deja de funcionar a los pocos días.
      </Aviso>
      <div className="portal-tarjeta mt-6 p-5 sm:p-7">
        <CambiarClaveForm onCambiada={onCambiada} />
      </div>
    </section>
  )
}

function CuentaBloqueada({ motivo, onCerrarSesion }: { motivo: string; onCerrarSesion: () => void }) {
  return (
    <section className="portal-tarjeta portal-entrada p-5 sm:p-7 max-w-2xl">
      <h1 className="mb-4 text-xl font-bold tracking-tight">No podemos mostrar tu portal</h1>
      <Aviso tono="error">{motivo}</Aviso>
      <button type="button" onClick={onCerrarSesion} className="portal-boton mt-5 w-full sm:w-auto">
        <LogOut className="w-4 h-4" aria-hidden="true" />
        Cerrar sesión
      </button>
    </section>
  )
}
