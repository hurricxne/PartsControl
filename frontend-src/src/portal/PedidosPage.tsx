// «Mis pedidos» (ruta /portal/pedidos?q=&estado=&anio=&page=): responde «¿dónde está mi
// orden X / mi repuesto Y?». Consume GET /api/portal/ordenes (portalAPI.ordenes →
// `OrdenesResp`) y, solo para saber qué años ofrecer, GET /api/portal/resumen.
//
// QUIÉN HACE QUÉ
//   · filtrosDePedidos.ts    lee y escribe los filtros en la URL, que es la única fuente
//                            de verdad (por ahí llegan también el buscador de la cabecera
//                            y los enlaces del tablero).
//   · PedidosFiltros.tsx     el buscador, los filtros de estado y el selector de año.
//   · PedidosResultados.tsx  la tabla y las tarjetas.
//   · este archivo           consulta, decide qué se muestra (cargando, error, vacío,
//                            lista) y pagina.
//
// DECISIONES QUE NO SE VEN EN EL CÓDIGO
//   · Teclear REEMPLAZA la entrada del historial (si no, «Atrás» desharía la búsqueda
//     pausa por pausa). Un clic en un filtro o en una página AGREGA una entrada: «Atrás»
//     lo deshace.
//   · Mientras llega una búsqueda nueva se sigue viendo la lista anterior, para que no
//     parpadee al escribir. Como a pleno sol no se distingue una lista atenuada, además
//     lo dice con palabras: «Buscando…».
//   · Tras buscar o filtrar el foco no se mueve: el resultado lo dice la región
//     role="status", que está SIEMPRE montada (una región que nace ya con su texto no se
//     anuncia). Al cambiar de PÁGINA sí se lleva el foco al inicio de la lista: quien
//     navega con teclado quedaría, si no, al final de una lista que acaba de cambiar.
//   · Una búsqueda de un solo carácter no se manda: el servidor la ignora (calza con casi
//     todo) y responder lo mismo dos veces solo gasta señal. La pantalla lo explica.
//   · «N pedidos para “…”» usa `q_efectivo`, las palabras que el servidor buscó de verdad
//     (recorta a 4 y descarta rótulos como «OC»), no lo que quedó escrito en el campo.
//   · No hay total general ni suma de montos: el backend no los manda para el listado.
import { useCallback, useId, useMemo, useRef, type MouseEvent, type ReactNode } from 'react'
import { Link, useSearchParams, type To } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowLeft, ChevronLeft, ChevronRight, Loader2, PackageOpen, SearchX, type LucideIcon } from 'lucide-react'
import { portalAPI, motivoDeBloqueo } from './api'
import type { OrdenesResp } from './types'
import { claveDeConsulta, usePortal, useTituloDePestana } from './sesion'
import { Actualizado, Aviso, AvisoDeRecargaFallida, Cargando, ErrorDeCarga, SinConexion } from './piezas'
import {
  MINIMO_PARA_BUSCAR, TAMANO_DE_PAGINA, aBusqueda, calificarPedidos, conFiltros, describirResultado,
  hayFiltros, leerFiltros,
  type CambiosDeFiltros, type FiltroDeEstado, type FiltrosDePedidos,
} from './filtrosDePedidos'
import PedidosFiltros from './PedidosFiltros'
import PedidosResultados from './PedidosResultados'

// Los años con pedidos cambian una vez al año, y el resumen que los trae arma hasta 500
// órdenes en el servidor: no se repite cada vez que se entra a esta pantalla.
const VIGENCIA_DE_LOS_ANIOS_MS = 5 * 60_000
const SIN_ANIOS: readonly number[] = []

/** Clic izquierdo sin teclas: los demás (abrir en otra pestaña) no cambian esta pantalla. */
function esClicSimple(ev: MouseEvent): boolean {
  return ev.button === 0 && !ev.metaKey && !ev.ctrlKey && !ev.shiftKey && !ev.altKey
}

export default function PedidosPage() {
  const { usuarioId, veMontos } = usePortal()
  const [parametros, setParametros] = useSearchParams()
  const filtros = useMemo(() => leerFiltros(parametros), [parametros])
  const refCampo = useRef<HTMLInputElement>(null)
  const refResultados = useRef<HTMLElement>(null)
  const idResultados = useId()
  useTituloDePestana('Mis pedidos')

  const q = filtros.q.length >= MINIMO_PARA_BUSCAR ? filtros.q : ''
  const consulta = useQuery({
    queryKey: claveDeConsulta(usuarioId, 'ordenes', {
      q, estado: filtros.estado, anio: filtros.anio, page: filtros.page,
    }),
    queryFn: async () => (await portalAPI.ordenes({
      q: q || undefined,
      estado: filtros.estado,
      anio: filtros.anio ?? undefined,
      page: filtros.page,
      page_size: TAMANO_DE_PAGINA,
    })).data,
    // Es keepPreviousData con un candado: la lista anterior se conserva solo si es de ESTA
    // cuenta. Si la sesión cambia con la pantalla abierta (otra pestaña entra con otra
    // cuenta), la lista de la cuenta anterior no se le muestra a la nueva ni un instante.
    placeholderData: (previos, consultaPrevia) =>
      consultaPrevia?.queryKey[1] === usuarioId ? previos : undefined,
  })
  // Misma clave que el resumen sin año del tablero: si ya se pasó por ahí, no se pide de
  // nuevo. Si esta consulta falla, el selector de año simplemente no aparece; la lista no
  // depende de ella.
  const consultaAnios = useQuery({
    queryKey: claveDeConsulta(usuarioId, 'tablero', 'todos'),
    queryFn: async () => (await portalAPI.tablero()).data,
    select: (tablero) => tablero.anios,
    staleTime: VIGENCIA_DE_LOS_ANIOS_MS,
  })

  const escribir = useCallback((cambios: CambiosDeFiltros, reemplazar = false) => {
    setParametros((previos) => conFiltros(previos, cambios), { replace: reemplazar })
  }, [setParametros])
  const buscar = useCallback((texto: string) => escribir({ q: texto }, true), [escribir])
  const elegirEstado = (estado: FiltroDeEstado) => {
    // Tocar el filtro ya elegido no hace nada (tampoco devuelve a la página 1).
    if (estado !== filtros.estado) escribir({ estado })
  }
  const recargar = () => { void consulta.refetch() }

  // Los botones de «sin resultados» desaparecen al usarlos: el foco se deja en un lugar
  // con sentido en vez de perderse en la página.
  const limpiarBusqueda = () => {
    buscar('')
    refCampo.current?.focus()
  }
  const quitarFiltros = () => {
    escribir({ estado: 'todas', anio: null })
    refResultados.current?.focus({ preventScroll: true })
  }
  const subirALaLista = (ev: MouseEvent) => {
    const lista = refResultados.current
    if (!lista || !esClicSimple(ev)) return
    lista.focus({ preventScroll: true })
    // index.css (del ERP) pone `scroll-behavior: smooth` en toda la página: a quien pidió
    // menos movimiento hay que decirle 'instant' de forma explícita.
    const sinMovimiento = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    lista.scrollIntoView({ block: 'start', behavior: sinMovimiento ? 'instant' : 'smooth' })
  }
  const haciaPagina = (page: number): To => ({ search: aBusqueda(conFiltros(parametros, { page })) })

  const datos = consulta.data
  // Se está viendo la lista ANTERIOR mientras llega la que corresponde a la URL.
  const enEspera = consulta.isPlaceholderData
  // react-query no sale a la red sin conexión: la consulta queda en pausa y sigue sola.
  const sinConexion = consulta.fetchStatus === 'paused'
  const bloqueo = consulta.isError ? motivoDeBloqueo(consulta.error) : null
  const paginas = datos && datos.page_size > 0 ? Math.max(1, Math.ceil(datos.total / datos.page_size)) : 1

  let anuncio = ''
  if (datos && enEspera) {
    anuncio = sinConexion ? 'Sin conexión. La búsqueda seguirá sola cuando vuelva la señal.' : 'Buscando…'
  } else if (datos) {
    anuncio = describirResultado(datos.total, filtros, datos.q_efectivo, paginas)
  }
  // Hay texto en el campo pero el servidor no buscó nada: fue muy corto para buscar.
  const faltanLetras = !!datos && !enEspera && filtros.q !== '' && datos.q_efectivo === ''

  return (
    <div className="portal-entrada">
      <p className="portal-rotulo">Órdenes de compra</p>
      <h1 className="mt-2 text-3xl sm:text-4xl font-extrabold tracking-tight">Mis pedidos</h1>

      <div className="mt-6">
        <PedidosFiltros
          filtros={filtros}
          anios={consultaAnios.data ?? SIN_ANIOS}
          refCampo={refCampo}
          onBuscar={buscar}
          onEstado={elegirEstado}
          onAnio={(anio) => escribir({ anio })}
        />
      </div>

      <section
        ref={refResultados}
        tabIndex={-1}
        aria-labelledby={idResultados}
        className="portal-foco-silencioso portal-pedidos-resultados"
      >
        <h2 id={idResultados} className="sr-only">Resultados</h2>
        <div className="portal-pedidos-barra">
          <p role="status" className="portal-pedidos-conteo">
            {enEspera && !sinConexion && <Loader2 className="portal-giro" aria-hidden="true" />}
            {anuncio}
            {faltanLetras && (
              <span className="portal-pedidos-nota"> Para buscar, escribe al menos {MINIMO_PARA_BUSCAR} caracteres.</span>
            )}
          </p>
          {datos && <Actualizado sello={datos} onRecargar={recargar} cargando={consulta.isFetching} />}
        </div>

        {!datos && consulta.isPending && (
          sinConexion
            ? <SinConexion texto="Tus pedidos se cargarán solos cuando vuelva la señal." />
            : <Cargando texto="Cargando tus pedidos…" />
        )}
        {!datos && consulta.isError && (
          // El 403 trae un texto escrito para el cliente (la cuenta dejó de poder usar el
          // portal): se muestra tal cual. El resto lleva mensaje propio: el `detail` de un
          // 503 son instrucciones para el equipo técnico.
          bloqueo
            ? <Aviso tono="error">{bloqueo}</Aviso>
            : <ErrorDeCarga titulo="No pudimos cargar tus pedidos" onReintentar={recargar} error={consulta.error} />
        )}
        {datos && consulta.isError && (
          // «Recargar» falló pero la lista de la carga anterior sigue ahí: se avisa que
          // puede no estar al día, en vez de borrarla.
          <div className="mb-4"><AvisoDeRecargaFallida motivo={bloqueo} /></div>
        )}
        {datos && (
          <div className="portal-pedidos-lista" aria-busy={enEspera}>
            {datos.items.length > 0
              ? <PedidosResultados ordenes={datos.items} hoy={datos.hoy} veMontos={veMontos} />
              : (
                <SinPedidos
                  datos={datos}
                  filtros={filtros}
                  primeraPagina={haciaPagina(1)}
                  onIrAPagina={subirALaLista}
                  onLimpiarBusqueda={limpiarBusqueda}
                  onQuitarFiltros={quitarFiltros}
                />
              )}
            {paginas > 1 && datos.items.length > 0 && (
              <Paginacion pagina={datos.page} paginas={paginas} haciaPagina={haciaPagina} onIrAPagina={subirALaLista} />
            )}
          </div>
        )}
      </section>
    </div>
  )
}

// ─── Paginación ───────────────────────────────────────────────────────────────
interface PaginacionProps {
  pagina: number
  paginas: number
  haciaPagina: (pagina: number) => To
  onIrAPagina: (ev: MouseEvent) => void
}

/** «Anterior / Siguiente» son enlaces reales (?page=N): se pueden abrir en otra pestaña. */
function Paginacion({ pagina, paginas, haciaPagina, onIrAPagina }: PaginacionProps) {
  return (
    <nav aria-label="Páginas de pedidos" className="portal-pedidos-paginas">
      <EnlaceDePagina a={pagina > 1 ? haciaPagina(pagina - 1) : null} onClick={onIrAPagina}>
        <ChevronLeft className="w-4 h-4" aria-hidden="true" />
        Anterior
      </EnlaceDePagina>
      <p className="portal-pedidos-pagina">Página {pagina} de {paginas}</p>
      <EnlaceDePagina a={pagina < paginas ? haciaPagina(pagina + 1) : null} onClick={onIrAPagina}>
        Siguiente
        <ChevronRight className="w-4 h-4" aria-hidden="true" />
      </EnlaceDePagina>
    </nav>
  )
}

interface EnlaceDePaginaProps {
  /** null = no hay página en esa dirección. */
  a: To | null
  onClick: (ev: MouseEvent) => void
  children: ReactNode
}

function EnlaceDePagina({ a, onClick, children }: EnlaceDePaginaProps) {
  const clases = 'portal-boton portal-boton-secundario'
  // Sin destino se deja el enlace en su lugar, deshabilitado: si se quitara, el otro
  // botón cambiaría de posición entre la primera página y las demás. Un <a> sin href no
  // recibe el foco; role="link" es lo que permite decirle «deshabilitado».
  if (!a) return <a role="link" aria-disabled="true" className={clases}>{children}</a>
  return <Link to={a} className={clases} onClick={onClick}>{children}</Link>
}

// ─── Sin resultados ───────────────────────────────────────────────────────────
interface SinPedidosProps {
  datos: OrdenesResp
  filtros: FiltrosDePedidos
  primeraPagina: To
  onIrAPagina: (ev: MouseEvent) => void
  onLimpiarBusqueda: () => void
  onQuitarFiltros: () => void
}

/**
 * Una lista vacía tiene cuatro causas y cada una pide otra cosa de la persona: la página
 * pedida no existe, la búsqueda no calzó, el filtro no calzó, o la cuenta no tiene
 * pedidos. Se revisan en ese orden.
 */
function SinPedidos({ datos, filtros, primeraPagina, onIrAPagina, onLimpiarBusqueda, onQuitarFiltros }: SinPedidosProps) {
  const filtrando = hayFiltros(filtros)

  // Hay pedidos, pero no en esta página (un ?page= escrito a mano o un enlace antiguo).
  if (datos.total > 0) {
    return (
      <Vacio Icono={SearchX} titulo="Esta página no tiene pedidos">
        <p className="portal-suave mt-2">La lista es más corta que la página que se pidió.</p>
        <Link to={primeraPagina} onClick={onIrAPagina} className="portal-boton mt-5 w-full sm:w-auto">
          <ArrowLeft className="w-4 h-4" aria-hidden="true" />
          Ir a la primera página
        </Link>
      </Vacio>
    )
  }
  if (datos.q_efectivo) {
    return (
      <Vacio Icono={SearchX} titulo={`No encontramos pedidos para “${datos.q_efectivo}”`}>
        <p className="portal-suave mt-2">
          Revisa el número o escríbelo sin guiones ni espacios. También puedes buscar por una parte del
          N° de orden, del N° de parte, de la descripción o del folio de una guía.
        </p>
        {filtrando && (
          <p className="portal-suave mt-2">
            Se buscó solo entre los {calificarPedidos(0, filtros)}.
          </p>
        )}
        <div className="mt-5 flex flex-col sm:flex-row gap-3">
          {filtrando && (
            <button type="button" onClick={onQuitarFiltros} className="portal-boton">
              Buscar en todos los pedidos
            </button>
          )}
          <button
            type="button"
            onClick={onLimpiarBusqueda}
            className={filtrando ? 'portal-boton portal-boton-secundario' : 'portal-boton'}
          >
            Limpiar búsqueda
          </button>
        </div>
      </Vacio>
    )
  }
  if (filtrando) {
    return (
      <Vacio Icono={SearchX} titulo={`No hay ${calificarPedidos(0, filtros)}`}>
        <p className="portal-suave mt-2">Ningún pedido calza con los filtros elegidos.</p>
        <button type="button" onClick={onQuitarFiltros} className="portal-boton mt-5 w-full sm:w-auto">
          Ver todos los pedidos
        </button>
      </Vacio>
    )
  }
  return (
    <Vacio Icono={PackageOpen} titulo="Todavía no hay pedidos en tu cuenta">
      <p className="portal-suave mt-2">
        Cuando MachParts registre una orden de compra tuya, aparecerá acá. Si esperabas ver una,
        avisa a tu ejecutivo de MachParts.
      </p>
    </Vacio>
  )
}

function Vacio({ Icono, titulo, children }: { Icono: LucideIcon; titulo: string; children: ReactNode }) {
  return (
    <div className="portal-tarjeta p-5 sm:p-7 max-w-2xl">
      <Icono className="w-8 h-8 portal-suave" aria-hidden="true" />
      <h3 className="mt-3 text-xl font-bold tracking-tight">{titulo}</h3>
      {children}
    </div>
  )
}
