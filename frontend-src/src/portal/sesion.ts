// Lo que comparten TODAS las pantallas del cliente sobre su sesión: quién es
// (usePortal), cómo se nombran sus consultas en la caché (claveDeConsulta), cuándo vale
// reintentar una (prepararConsultasDelPortal) y el título de la pestaña.
//
// CACHÉ ENTRE CUENTAS. react-query guarda las respuestas en memoria, y la caché sobrevive
// al cambio de sesión (el logout navega por la SPA sin recargar; además una segunda
// pestaña puede iniciar sesión con otra cuenta y esta la adopta, ver stores/authStore.ts).
// Por eso toda clave del portal empieza con ['portal', <id del usuario>, …]: los datos de
// una cuenta no se le pintan a otra aunque queden en memoria, y cerrar sesión borra todo
// lo que empiece con 'portal' (PortalLayout.tsx::cerrarSesion).
import { useEffect } from 'react'
import { useOutletContext } from 'react-router-dom'
import type { QueryClient } from '@tanstack/react-query'
import { estadoHttp } from './api'
import type { PortalYo } from './types'

/** Primer eslabón de toda clave de consulta del portal. */
export const RAIZ_DE_CONSULTAS = 'portal'

/** Lo que PortalLayout reparte a las pantallas por el contexto del <Outlet>. */
export interface ContextoPortal {
  yo: PortalYo
  // false para el perfil 'bodega'. Es solo presentación (no dibujar columnas vacías): el
  // backend ya no manda los montos (llegan null) ni las facturas.
  veMontos: boolean
  usuarioId: number
}

/** Los datos de la sesión. Solo funciona dentro de una ruta hija de PortalLayout. */
export function usePortal(): ContextoPortal {
  return useOutletContext<ContextoPortal>()
}

/** ['portal', usuarioId, ...partes]: la queryKey de cualquier consulta del portal. */
export function claveDeConsulta(usuarioId: number, ...partes: unknown[]): unknown[] {
  return [RAIZ_DE_CONSULTAS, usuarioId, ...partes]
}

// ─── Reintentos ───────────────────────────────────────────────────────────────
const REINTENTOS_POR_FALLA_PASAJERA = 1

/**
 * ¿Vale repetir sola una consulta que falló? Solo lo que puede cambiar sin que nadie
 * haga nada: red caída o error del servidor (5xx). Un 404 («no existe o no es tuyo») o
 * un 403 (clave temporal, cuenta suspendida) responden lo mismo al segundo intento, y
 * repetirlos solo demora el mensaje que la persona necesita leer.
 */
function valeReintentar(fallos: number, error: unknown): boolean {
  const estado = estadoHttp(error)
  const esPasajera = estado === undefined || estado >= 500
  return esPasajera && fallos < REINTENTOS_POR_FALLA_PASAJERA
}

const clientesPreparados = new WeakSet<QueryClient>()

/**
 * Deja la regla de reintentos como valor por defecto de TODA consulta cuya clave empiece
 * con 'portal' (el QueryClient de main.tsx reintenta una vez cualquier error, también un
 * 404). La llama PortalLayout antes de su primera consulta; las pantallas no pasan
 * `retry` a mano. Repetir la llamada no hace nada.
 */
export function prepararConsultasDelPortal(queryClient: QueryClient): void {
  if (clientesPreparados.has(queryClient)) return
  clientesPreparados.add(queryClient)
  queryClient.setQueryDefaults([RAIZ_DE_CONSULTAS], { retry: valeReintentar })
}

// ─── Título de la pestaña ─────────────────────────────────────────────────────
// index.html titula la pestaña como el ERP («Validador de Cotizaciones CAT»); el cliente
// no tiene por qué leer eso. Cada pantalla pide su título y, al salir del portal, vuelve
// el del ERP.
//
// POR QUÉ UNA LISTA y no «guardar el anterior y reponerlo»: al desmontar un árbol React
// limpia primero al padre y después a los hijos, así que el hijo repondría el título del
// padre cuando el padre ya se fue (la pestaña del login quedaba titulada como el portal).
// Con la lista, el orden de montaje y desmontaje da lo mismo: manda el último título
// pedido por una PANTALLA; si no hay ninguno, el de fondo que pone el cascarón; si ya no
// queda nadie, vuelve el que había antes.
interface TituloPedido {
  texto: string
  deFondo: boolean
}

let titulosPedidos: readonly TituloPedido[] = []
let tituloAnterior: string | null = null

function pintarTitulo(): void {
  const dePantalla = titulosPedidos.filter((t) => !t.deFondo)
  const elegido = dePantalla[dePantalla.length - 1] ?? titulosPedidos[titulosPedidos.length - 1]
  if (elegido) {
    document.title = elegido.texto
    return
  }
  if (tituloAnterior !== null) {
    document.title = tituloAnterior
    tituloAnterior = null
  }
}

function usePedirTitulo(titulo: string, deFondo: boolean): void {
  useEffect(() => {
    if (titulosPedidos.length === 0) tituloAnterior = document.title
    const pedido: TituloPedido = { texto: `${titulo} — MachParts`, deFondo }
    titulosPedidos = [...titulosPedidos, pedido]
    pintarTitulo()
    return () => {
      titulosPedidos = titulosPedidos.filter((t) => t !== pedido)
      pintarTitulo()
    }
  }, [titulo, deFondo])
}

/** Pone la pestaña en «<titulo> — MachParts» mientras la pantalla esté montada. */
export function useTituloDePestana(titulo: string): void {
  usePedirTitulo(titulo, false)
}

/**
 * Título de fondo del cascarón: el que se ve mientras ninguna pantalla pidió el suyo
 * (cargando, cuenta bloqueada, cambio de clave temporal). Solo lo usa PortalLayout.
 */
export function useTituloDeFondo(titulo: string): void {
  usePedirTitulo(titulo, true)
}
