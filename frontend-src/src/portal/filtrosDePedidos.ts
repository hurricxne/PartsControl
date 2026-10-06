// Los filtros del listado «Mis pedidos»: cómo se LEEN de la URL, cómo se ESCRIBEN en ella
// y cómo se DICEN en palabras. Módulo puro (sin React). Lo usan PedidosPage.tsx (que
// consulta GET /api/portal/ordenes) y PedidosFiltros.tsx (que dibuja los controles).
//
// LA URL ES LA ÚNICA FUENTE DE VERDAD
//   /portal/pedidos?q=&estado=&anio=&page= se puede recargar, guardar, mandar por correo
//   y deshacer con «Atrás». Además es por donde llegan el buscador de la cabecera
//   (CabeceraPortal.tsx navega a ?q=…) y los enlaces del tablero (?estado=abiertas&anio=…).
//   Ningún componente guarda una copia de los filtros: los lee de acá cada vez.
//
// LA URL ES DATO EXTERNO: cualquiera la escribe a mano o la pega cortada. Lo que no calza
// con lo que acepta el backend (backend/portal/router_ordenes.py::listar_ordenes) se
// IGNORA al leer. Mandarlo tal cual termina en un 400 o un 422, y la pantalla mostraría
// «No pudimos cargar tus pedidos» por un enlace mal copiado.
//
// UN VALOR POR DEFECTO NO SE ESCRIBE: sin búsqueda, «Todas», todos los años y la página 1
// dejan la dirección limpia (/portal/pedidos), que es la misma a la que apunta el menú.
import { fmtCantidad } from './formato'
import type { OrdenesParams } from './types'

export type FiltroDeEstado = NonNullable<OrdenesParams['estado']>

export interface FiltrosDePedidos {
  q: string // '' = sin búsqueda
  estado: FiltroDeEstado
  anio: number | null // null = todos los años
  page: number // desde 1
}

/** Lo que cambia de los filtros. `anio: null` quita el año; un campo ausente no se toca. */
export interface CambiosDeFiltros {
  q?: string
  estado?: FiltroDeEstado
  anio?: number | null
  page?: number
}

// Espejos de backend/portal/router_ordenes.py::listar_ordenes y de busqueda.py.
export const TAMANO_DE_PAGINA = 20
export const LARGO_MAXIMO_DE_BUSQUEDA = 200
// Con menos, el servidor no filtra (busqueda.py::normalizar): no vale la pena preguntar.
export const MINIMO_PARA_BUSCAR = 2
// Espejo de los límites del parámetro `anio` en backend/portal/router_ordenes.py: fuera de
// ellos el backend responde 422, así que un año así en la URL se ignora.
export const ANIO_MINIMO = 2000
export const ANIO_MAXIMO = 2100
const PAGINA_MAXIMA = 10_000

// Las palabras de los filtros van en femenino («órdenes») y el conteo en masculino
// («pedidos»): son los dos nombres que el cliente ya lee en la pantalla («N° de orden»,
// «Mis pedidos»).
export const FILTROS_DE_ESTADO: readonly { valor: FiltroDeEstado; texto: string }[] = [
  { valor: 'todas', texto: 'Todas' },
  { valor: 'abiertas', texto: 'Abiertas' },
  { valor: 'entregadas', texto: 'Entregadas' },
]

// ─── Leer ─────────────────────────────────────────────────────────────────────
/**
 * Lo tecleado → lo que se busca: sin espacios a los lados ni repetidos, y recortado al
 * largo que acepta el backend. Es la forma que se guarda en la URL y con la que el
 * buscador decide si lo que hay en el campo «es lo mismo» que ya se buscó.
 */
export function normalizarBusqueda(texto: string): string {
  return texto.replace(/\s+/g, ' ').trim().slice(0, LARGO_MAXIMO_DE_BUSQUEDA).trim()
}

function esFiltroDeEstado(texto: string | null): texto is FiltroDeEstado {
  return FILTROS_DE_ESTADO.some((f) => f.valor === texto)
}

/** Un entero escrito solo con dígitos y dentro del rango; cualquier otra cosa → null. */
export function leerEntero(texto: string | null, minimo: number, maximo: number): number | null {
  if (!texto || !/^\d{1,5}$/.test(texto)) return null
  const numero = Number(texto)
  return numero >= minimo && numero <= maximo ? numero : null
}

export function leerFiltros(parametros: URLSearchParams): FiltrosDePedidos {
  const estado = parametros.get('estado')
  return {
    q: normalizarBusqueda(parametros.get('q') ?? ''),
    estado: esFiltroDeEstado(estado) ? estado : 'todas',
    anio: leerEntero(parametros.get('anio'), ANIO_MINIMO, ANIO_MAXIMO),
    page: leerEntero(parametros.get('page'), 1, PAGINA_MAXIMA) ?? 1,
  }
}

// ─── Escribir ─────────────────────────────────────────────────────────────────
/**
 * Los parámetros de la URL con los cambios aplicados (devuelve una copia; no toca los
 * recibidos). Todo cambio que no diga la página vuelve a la PRIMERA: con otra búsqueda u
 * otro filtro la lista es otra, y la página 3 de la anterior puede no existir.
 */
export function conFiltros(parametros: URLSearchParams, cambios: CambiosDeFiltros): URLSearchParams {
  const siguientes = new URLSearchParams(parametros)
  const poner = (nombre: string, valor: string | null) => {
    if (valor) siguientes.set(nombre, valor)
    else siguientes.delete(nombre)
  }
  if (cambios.q !== undefined) poner('q', normalizarBusqueda(cambios.q))
  if (cambios.estado !== undefined) poner('estado', cambios.estado === 'todas' ? null : cambios.estado)
  if (cambios.anio !== undefined) poner('anio', cambios.anio === null ? null : String(cambios.anio))
  const pagina = cambios.page ?? 1
  poner('page', pagina > 1 ? String(pagina) : null)
  return siguientes
}

/** '?q=…&page=2', o '' si no queda ningún parámetro: lo que espera un <Link to={{ search }}>. */
export function aBusqueda(parametros: URLSearchParams): string {
  const texto = parametros.toString()
  return texto ? `?${texto}` : ''
}

// ─── Decir ────────────────────────────────────────────────────────────────────
const CALIFICATIVO: Record<FiltroDeEstado, { uno: string; varios: string }> = {
  todas: { uno: '', varios: '' },
  abiertas: { uno: ' abierto', varios: ' abiertos' },
  entregadas: { uno: ' entregado', varios: ' entregados' },
}

/** ¿Hay algún filtro puesto además de la búsqueda? */
export function hayFiltros(filtros: Pick<FiltrosDePedidos, 'estado' | 'anio'>): boolean {
  return filtros.estado !== 'todas' || filtros.anio !== null
}

/**
 * «pedidos abiertos de 2025», «pedido entregado», «pedidos»: el nombre de lo que se está
 * listando, con sus filtros. Sin la cantidad, para que sirva igual en «12 pedidos
 * abiertos» y en «No hay pedidos abiertos».
 */
export function calificarPedidos(cuantos: number, filtros: Pick<FiltrosDePedidos, 'estado' | 'anio'>): string {
  const uno = cuantos === 1
  const calificativo = CALIFICATIVO[filtros.estado][uno ? 'uno' : 'varios']
  const delAnio = filtros.anio !== null ? ` de ${filtros.anio}` : ''
  return `${uno ? 'pedido' : 'pedidos'}${calificativo}${delAnio}`
}

/**
 * La frase que anuncia el resultado: «12 pedidos», «3 pedidos abiertos de 2025 para
 * “1R0716” · página 2 de 4». Nombra SIEMPRE los filtros vigentes: una región
 * role="status" solo se vuelve a leer si su texto cambia, y entre «Todas» y «Abiertas»
 * la cantidad puede ser la misma.
 *
 * `qEfectivo` es OrdenesResp.q_efectivo: las palabras que el servidor buscó de verdad
 * (recorta a 4 palabras y descarta las de un carácter), no lo que hay en el campo.
 */
export function describirResultado(
  total: number,
  filtros: FiltrosDePedidos,
  qEfectivo: string,
  paginas: number,
): string {
  const partes = [`${fmtCantidad(total)} ${calificarPedidos(total, filtros)}`]
  if (qEfectivo) partes.push(` para “${qEfectivo}”`)
  if (paginas > 1) partes.push(` · página ${fmtCantidad(filtros.page)} de ${fmtCantidad(paginas)}`)
  return partes.join('')
}
