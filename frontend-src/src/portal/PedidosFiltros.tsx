// Los controles de «Mis pedidos»: el buscador, los filtros de estado y el selector de año.
// No consulta ni toca la URL: recibe los filtros vigentes (los que PedidosPage leyó de la
// URL con filtrosDePedidos.ts::leerFiltros) y avisa cada cambio por sus callbacks.
//
// DECISIONES QUE NO SE VEN EN EL CÓDIGO
//   · EL CAMPO TIENE DOS DUEÑOS. Lo que la persona teclea vive acá (`texto`) y llega a la
//     URL 350 ms después de la última tecla: una consulta por tecla gastaría la poca señal
//     de una faena. Pero la URL también cambia SIN que se teclee acá: el buscador de la
//     cabecera navega a ?q=…, «Atrás» repone la búsqueda anterior, «Limpiar búsqueda» la
//     borra. En esos casos el campo SIGUE a la URL. La regla para que no se pisen: mientras
//     lo tecleado, ya normalizado, sea igual a lo de la URL, manda lo tecleado. Sin ella,
//     al escribir «bomba » el espacio final se borraría solo y la palabra siguiente
//     quedaría pegada («bombaagua»).
//   · Enter (o la tecla «buscar» del teclado del teléfono) aplica al instante, sin esperar.
//   · El botón × es propio: el del navegador mide unos 16 px y con guantes no se acierta
//     (portal.css lo esconde para que no haya dos).
//   · Tras buscar o filtrar el foco NO se mueve. El resultado lo anuncia la región
//     role="status" de PedidosPage. La única excepción es ×: el botón desaparece al
//     vaciarse el campo, así que el foco pasa al campo en vez de perderse.
import { useEffect, useId, useState, type FormEvent, type RefObject } from 'react'
import { Search, X } from 'lucide-react'
import { SelectorDeAnio } from './piezas'
import {
  FILTROS_DE_ESTADO, LARGO_MAXIMO_DE_BUSQUEDA, normalizarBusqueda,
  type FiltroDeEstado, type FiltrosDePedidos,
} from './filtrosDePedidos'

const ESPERA_AL_TECLEAR_MS = 350

interface PedidosFiltrosProps {
  filtros: FiltrosDePedidos
  /** Años en que el cliente tiene pedidos, del más reciente al más antiguo. */
  anios: readonly number[]
  /** El <input> del buscador: PedidosPage le devuelve el foco tras «Limpiar búsqueda». */
  refCampo: RefObject<HTMLInputElement>
  onBuscar: (q: string) => void
  onEstado: (estado: FiltroDeEstado) => void
  onAnio: (anio: number | null) => void
}

export default function PedidosFiltros({ filtros, anios, refCampo, onBuscar, onEstado, onAnio }: PedidosFiltrosProps) {
  return (
    <div className="portal-pedidos-filtros">
      <Buscador q={filtros.q} refCampo={refCampo} onBuscar={onBuscar} />
      <div className="portal-pedidos-filtros-fila">
        <div role="group" aria-label="Filtrar por estado" className="portal-pedidos-chips">
          {FILTROS_DE_ESTADO.map((f) => (
            <button
              key={f.valor}
              type="button"
              className="portal-chip"
              aria-pressed={filtros.estado === f.valor}
              onClick={() => onEstado(f.valor)}
            >
              {f.texto}
            </button>
          ))}
        </div>
        {/* Con un solo año no hay nada que elegir (el inicio sí lo muestra: ahí rotula el periodo). */}
        <SelectorDeAnio anios={anios} anio={filtros.anio} onElegir={onAnio} minimo={2} className="portal-pedidos-anio" />
      </div>
    </div>
  )
}

interface BuscadorProps {
  /** La búsqueda vigente: la de la URL, ya normalizada. */
  q: string
  refCampo: RefObject<HTMLInputElement>
  onBuscar: (q: string) => void
}

function Buscador({ q, refCampo, onBuscar }: BuscadorProps) {
  const id = useId()
  const [texto, setTexto] = useState(q)

  // La URL cambió por fuera (cabecera, «Atrás», «Limpiar búsqueda»): el campo la sigue.
  // Si cambió porque ESTE campo la escribió, lo tecleado se queda como está.
  useEffect(() => {
    setTexto((tecleado) => (normalizarBusqueda(tecleado) === q ? tecleado : q))
  }, [q])

  // Lo tecleado pasa a la URL cuando la persona deja de escribir. Cada tecla cancela la
  // espera anterior; si la URL cambia por fuera mientras tanto, el efecto de arriba
  // repone el campo y esta espera se cancela sola (lo viejo no pisa a lo nuevo).
  useEffect(() => {
    const pedido = normalizarBusqueda(texto)
    if (pedido === q) return
    const reloj = window.setTimeout(() => onBuscar(pedido), ESPERA_AL_TECLEAR_MS)
    return () => window.clearTimeout(reloj)
  }, [texto, q, onBuscar])

  const buscarYa = (ev: FormEvent) => {
    ev.preventDefault()
    const pedido = normalizarBusqueda(texto)
    if (pedido !== q) onBuscar(pedido)
  }

  const limpiar = () => {
    setTexto('')
    if (q) onBuscar('')
    refCampo.current?.focus()
  }

  return (
    <form role="search" aria-label="Buscar en mis pedidos" className="portal-pedidos-buscador" onSubmit={buscarYa}>
      <label htmlFor={id} className="sr-only">
        Buscar por N° de orden, N° de parte, descripción o folio de guía
      </label>
      <Search className="portal-pedidos-lupa" aria-hidden="true" />
      <input
        ref={refCampo}
        id={id}
        type="search"
        className="portal-campo"
        placeholder="N° de orden, N° de parte o folio"
        value={texto}
        onChange={(e) => setTexto(e.target.value)}
        maxLength={LARGO_MAXIMO_DE_BUSQUEDA}
        enterKeyHint="search"
        autoComplete="off"
        autoCapitalize="off"
        spellCheck={false}
      />
      {texto && (
        <button type="button" className="portal-pedidos-limpiar" onClick={limpiar} aria-label="Limpiar búsqueda">
          <X aria-hidden="true" />
        </button>
      )}
    </form>
  )
}
