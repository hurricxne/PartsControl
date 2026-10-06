// Cabecera del Portal de Clientes: la marca, las secciones, el buscador, el botón de tema
// y Cerrar sesión. La monta PortalLayout; no pide nada al backend.
//
// TRES ANCHOS (las reglas están en portal.css, bloque «Cabecera»):
//   · angosto (teléfono): marca + botón «Menú» en la primera fila y el buscador en la
//     segunda. Las secciones, el tema y Cerrar sesión se despliegan con «Menú»: la
//     cabecera no puede comerse media pantalla de un teléfono.
//   · medio (desde 768 px): marca + secciones arriba; buscador + acciones abajo.
//   · ancho (desde 1152 px): todo en una fila.
// El orden de los elementos en el código es el mismo en que se ven en los tres anchos,
// para que el tabulador recorra la cabecera en el orden en que se lee.
//
// EL BUSCADOR NO BUSCA: navega a /portal/pedidos?q=… y la que consulta es PedidosPage,
// que lee sus filtros de la URL. El campo muestra el `q` de la URL mientras se está en
// esa pantalla; así, si allá se borra la búsqueda, acá no queda un texto que ya no
// corresponde a lo que se ve.
import { useEffect, useId, useRef, useState } from 'react'
import { Link, NavLink, useLocation, useNavigate } from 'react-router-dom'
import { LogOut, Menu, Moon, Search, Sun, Wrench, X } from 'lucide-react'
import { useTheme } from '../context/ThemeContext'
import { LARGO_MAXIMO_DE_BUSQUEDA } from './filtrosDePedidos'
import { RUTA_DE_PEDIDOS } from './rutas'


const SECCIONES = [
  // `exacta`: sin eso «Inicio» quedaría marcada en todas las pantallas (todas empiezan
  // con /portal).
  { a: '/portal', texto: 'Inicio', exacta: true },
  { a: RUTA_DE_PEDIDOS, texto: 'Mis pedidos', exacta: false },
  { a: '/portal/cuenta', texto: 'Mi cuenta', exacta: false },
]

interface CabeceraPortalProps {
  /**
   * false mientras no hay portal que recorrer (cargando, cuenta bloqueada, clave
   * temporal): se ocultan las secciones y el buscador; el tema y Cerrar sesión siguen.
   */
  conNavegacion: boolean
  onCerrarSesion: () => void
}

export default function CabeceraPortal({ conNavegacion, onCerrarSesion }: CabeceraPortalProps) {
  const { theme, toggleTheme } = useTheme()
  const { pathname, search } = useLocation()
  const [menuAbierto, setMenuAbierto] = useState(false)
  const refBotonMenu = useRef<HTMLButtonElement>(null)
  const id = useId()
  const idSecciones = `${id}-secciones`
  const idAcciones = `${id}-acciones`

  // Elegir una sección (o buscar) cierra el menú: en un teléfono taparía la pantalla a
  // la que se acaba de llegar.
  useEffect(() => { setMenuAbierto(false) }, [pathname, search])

  const cerrarConEscape = (ev: React.KeyboardEvent) => {
    if (ev.key !== 'Escape' || !menuAbierto) return
    setMenuAbierto(false)
    refBotonMenu.current?.focus()
  }

  const temaDestino = theme === 'dark' ? 'claro' : 'oscuro'

  return (
    <header className="portal-cabecera" onKeyDown={cerrarConEscape}>
      <div
        className="portal-cabecera-rejilla mx-auto w-full max-w-6xl px-4 sm:px-6"
        data-navegacion={conNavegacion}
      >
        <Link to="/portal" className="portal-marca">
          <span className="portal-sello" aria-hidden="true">
            <Wrench className="w-5 h-5" />
          </span>
          <span className="min-w-0 leading-tight">
            <span className="portal-marca-nombre">MachParts</span>
            <span className="portal-cabecera-rotulo portal-marca-rotulo">Portal de Clientes</span>
          </span>
        </Link>

        {conNavegacion && (
          <button
            ref={refBotonMenu}
            type="button"
            className="portal-boton-cabecera portal-menu-boton"
            aria-expanded={menuAbierto}
            aria-controls={`${idSecciones} ${idAcciones}`}
            onClick={() => setMenuAbierto((abierto) => !abierto)}
          >
            {menuAbierto
              ? <X className="w-4 h-4" aria-hidden="true" />
              : <Menu className="w-4 h-4" aria-hidden="true" />}
            Menú
          </button>
        )}

        {conNavegacion && (
          <nav id={idSecciones} aria-label="Secciones del portal" className="portal-nav" data-abierto={menuAbierto}>
            {SECCIONES.map((s) => (
              // NavLink pone aria-current="page" en la sección activa; portal.css la marca
              // con un filete además del color.
              <NavLink key={s.a} to={s.a} end={s.exacta}>{s.texto}</NavLink>
            ))}
          </nav>
        )}

        {conNavegacion && <Buscador />}

        <div id={idAcciones} className="portal-acciones" data-abierto={menuAbierto}>
          <button
            type="button"
            className="portal-boton-cabecera"
            onClick={toggleTheme}
            aria-label={`Cambiar a tema ${temaDestino}`}
          >
            {theme === 'dark'
              ? <Sun className="w-4 h-4" aria-hidden="true" />
              : <Moon className="w-4 h-4" aria-hidden="true" />}
            <span className="portal-tema-texto">Tema {temaDestino}</span>
          </button>
          <button type="button" className="portal-boton-cabecera" onClick={onCerrarSesion}>
            <LogOut className="w-4 h-4" aria-hidden="true" />
            {/* A 320 px «Cerrar sesión» completo no cabe junto a la marca. */}
            <span className="sm:hidden">Salir</span>
            <span className="hidden sm:inline">Cerrar sesión</span>
          </button>
        </div>
      </div>
    </header>
  )
}

/** Lo que se está buscando según la URL: solo en Mis pedidos; en otra pantalla, nada. */
function qDeLaUrl(pathname: string, search: string): string {
  const enPedidos = pathname.replace(/\/+$/, '') === RUTA_DE_PEDIDOS
  return enPedidos ? new URLSearchParams(search).get('q') ?? '' : ''
}

// El campo sigue a la URL («Atrás», «Limpiar búsqueda» de la pantalla), salvo mientras el
// foco está EN él: pisar lo que se teclea borraría espacios a medio escribir, y volver a
// montarlo (una `key`) soltaría el foco justo después de buscar.
function Buscador() {
  const navigate = useNavigate()
  const { pathname, search } = useLocation()
  const id = useId()
  const refCampo = useRef<HTMLInputElement>(null)
  const q = qDeLaUrl(pathname, search)
  const [texto, setTexto] = useState(q)

  useEffect(() => {
    if (document.activeElement !== refCampo.current) setTexto(q)
  }, [q])

  const buscar = (ev: React.FormEvent) => {
    ev.preventDefault()
    const q = texto.trim()
    // Buscar es empezar de nuevo: no arrastra el filtro de estado, el año ni la página de
    // la búsqueda anterior (un N° de orden no puede quedar escondido por un filtro viejo).
    navigate(q ? `${RUTA_DE_PEDIDOS}?${new URLSearchParams({ q })}` : RUTA_DE_PEDIDOS)
  }

  return (
    <form role="search" className="portal-buscador" onSubmit={buscar}>
      <label htmlFor={id} className="sr-only">Buscar por N° de orden o N° de parte</label>
      <input
        ref={refCampo}
        id={id}
        type="search"
        className="portal-campo"
        placeholder="N° de orden o N° de parte"
        value={texto}
        onChange={(e) => setTexto(e.target.value)}
        maxLength={LARGO_MAXIMO_DE_BUSQUEDA}
        enterKeyHint="search"
        autoComplete="off"
        autoCapitalize="off"
        spellCheck={false}
      />
      <button type="submit" className="portal-buscador-boton" aria-label="Buscar">
        <Search className="w-5 h-5" aria-hidden="true" />
      </button>
    </form>
  )
}
