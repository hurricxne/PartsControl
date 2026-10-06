// Archivos del cliente: las fotos de sus repuestos y los PDF de guías y facturas.
// Consume los endpoints de bytes de portalAPI (foto, guiaPdf, guiaFirmada, facturaPdf).
//
// POR QUÉ NO BASTA UN <img src="/api/…"> NI UN window.open
//   Esos endpoints exigen el token de la sesión, y ni <img> ni una pestaña nueva lo
//   envían. El archivo se pide con axios (que sí lo agrega), llega como Blob y se muestra
//   o se descarga desde un «object URL» local. Los helpers del ERP
//   (services/api.ts::abrirDocumento, components/FotoBodega) no sirven: apuntan a rutas
//   internas que a una cuenta portal le responden 403.
//
// QUIÉN ES DUEÑO DE CADA OBJECT URL (lo que hay que entender para no romperlo)
//   El dueño es la caché de este módulo, no el componente que muestra la imagen. Si cada
//   componente revocara su URL al desmontarse, rompería la misma foto en otro lugar de la
//   pantalla (la miniatura y el visor comparten una). Se revoca solo en dos momentos:
//     · podar(): cuando la caché pasa su tope, y solo lo que nadie está mostrando;
//     · limpiarArchivosDelPortal(): al cerrar sesión, todo.
//
// ENTRE CUENTAS: la clave que da el llamador se guarda con el id del usuario por delante,
// y `epoca` descarta lo que venía viajando cuando se cerró la sesión. Los archivos de una
// cuenta no llegan a otra aunque la pestaña siga abierta.
import { useEffect, useRef, useState } from 'react'
import axios from 'axios'
import { useAuthStore } from '../stores/authStore'
import { detalleDe, estadoHttp } from './api'

/** Una petición de archivo: devuelve el Blob (portalAPI.foto, guiaPdf, guiaFirmada, facturaPdf). */
export type Pedir = () => Promise<{ data: Blob }>
type EstadoDeImagen = 'cargando' | 'lista' | 'error'

/** clave → object URL. El orden de inserción hace de «usada hace más tiempo primero». */
const urls = new Map<string, string>()
/** clave → petición en vuelo: la miniatura y el visor de una foto hacen UN solo GET. */
const enVuelo = new Map<string, Promise<string>>()
/** clave → cuántos componentes la están mostrando ahora. podar() no las toca. */
const montadas = new Map<string, number>()
/** object URL de descargas recientes, todavía sin revocar. */
const descargasVivas = new Set<string>()
/** Sube con cada limpiarArchivosDelPortal(): lo pedido en una época anterior se descarta. */
let epoca = 0

// La sesión puede cambiar sin pasar por «Cerrar sesión» de esta pestaña: otra pestaña
// cierra sesión o entra con otra cuenta, y stores/authStore.ts adopta esa sesión. Lo que
// hay en memoria es de la cuenta anterior y se suelta igual que al cerrar sesión acá.
let usuarioVigente: number | null = useAuthStore.getState().user?.id ?? null
useAuthStore.subscribe((estado) => {
  const usuario = estado.user?.id ?? null
  if (usuario === usuarioVigente) return
  usuarioVigente = usuario
  limpiarArchivosDelPortal()
})

// Las fotos del portal son derivados de ~300 KB (backend/portal/fotos.py las reduce a
// 1600 px), no los originales de 3-8 MB de bodega: el tope puede ser holgado.
const TOPE_DE_IMAGENES = 40
// El navegador necesita la URL viva mientras escribe el archivo en disco; revocarla
// apenas se hace clic cancela la descarga en Safari y en algunos Android.
const ESPERA_ANTES_DE_SOLTAR_MS = 60_000

function podar(): void {
  for (const [clave, url] of urls) {
    if (urls.size <= TOPE_DE_IMAGENES) return
    if ((montadas.get(clave) ?? 0) > 0) continue
    URL.revokeObjectURL(url)
    urls.delete(clave)
  }
}

function montar(clave: string): void {
  montadas.set(clave, (montadas.get(clave) ?? 0) + 1)
}

function desmontar(clave: string): void {
  const quedan = (montadas.get(clave) ?? 0) - 1
  if (quedan > 0) {
    montadas.set(clave, quedan)
    return
  }
  montadas.delete(clave)
  podar()
}

function cargar(clave: string, pedir: Pedir): Promise<string> {
  const guardada = urls.get(clave)
  if (guardada) {
    // Volver a insertarla la deja al final: «usada recién» para podar().
    urls.delete(clave)
    urls.set(clave, guardada)
    return Promise.resolve(guardada)
  }
  const yaViaja = enVuelo.get(clave)
  if (yaViaja) return yaViaja

  const miEpoca = epoca
  const pedido = pedir()
    .then(({ data }) => {
      // La sesión se cerró mientras el archivo viajaba: no se guarda (quedaría un blob
      // de la cuenta anterior que nadie revocaría).
      if (miEpoca !== epoca) throw new Error('sesión cerrada durante la carga')
      const url = URL.createObjectURL(data)
      urls.set(clave, url)
      podar()
      return url
    })
    .finally(() => {
      if (enVuelo.get(clave) === pedido) enVuelo.delete(clave)
    })
  enVuelo.set(clave, pedido)
  return pedido
}

/**
 * Una imagen privada lista para un <img src>. `clave` identifica el archivo (por ejemplo
 * `foto-${id}`) y es lo único que decide cuándo se vuelve a pedir: `pedir` puede ser una
 * función nueva en cada render. Una carga fallida no se guarda: se reintenta al volver a
 * montar el componente.
 */
export function useImagenPrivada(
  pedir: Pedir,
  clave: string
): { url: string | null; estado: EstadoDeImagen } {
  const usuarioId = useAuthStore((s) => s.user?.id ?? 0)
  const claveCompleta = `${usuarioId}:${clave}`
  const pedirVigente = useRef(pedir)
  useEffect(() => { pedirVigente.current = pedir })

  const [resultado, setResultado] = useState<{ clave: string; url: string | null; estado: EstadoDeImagen }>(
    () => ({ clave: claveCompleta, url: urls.get(claveCompleta) ?? null, estado: urls.has(claveCompleta) ? 'lista' : 'cargando' })
  )

  useEffect(() => {
    // `vigente` descarta la respuesta de un montaje ya desechado (StrictMode monta dos
    // veces en desarrollo) o de una clave anterior.
    let vigente = true
    montar(claveCompleta)
    cargar(claveCompleta, () => pedirVigente.current())
      .then((url) => { if (vigente) setResultado({ clave: claveCompleta, url, estado: 'lista' }) })
      .catch(() => { if (vigente) setResultado({ clave: claveCompleta, url: null, estado: 'error' }) })
    return () => {
      vigente = false
      desmontar(claveCompleta)
    }
  }, [claveCompleta])

  // La clave cambió y su carga todavía no responde: no se muestra la imagen anterior.
  // Si la nueva ya está en la caché se entrega de inmediato (pasar de una foto a otra
  // en el visor no parpadea).
  if (resultado.clave !== claveCompleta) {
    const guardada = urls.get(claveCompleta) ?? null
    return { url: guardada, estado: guardada ? 'lista' : 'cargando' }
  }
  return { url: resultado.url, estado: resultado.estado }
}

// ─── Descargas ────────────────────────────────────────────────────────────────
const EXTENSIONES: Record<string, string> = {
  'application/pdf': '.pdf',
  'image/jpeg': '.jpg',
  'image/png': '.png',
  'image/webp': '.webp',
}

const FALLA_DE_DESCARGA = 'No pudimos descargar el archivo. Revisa tu conexión e intenta de nuevo.'
// Respuestas cuyo `detail` el backend escribe PARA EL CLIENTE (portal/router_ordenes.py y
// portal/cupo.py): 404 «Esta guía no tiene un PDF disponible», 502 «No pudimos obtener
// el documento…», 429 «Demasiadas solicitudes seguidas. Espera un momento…», 403 cuenta
// suspendida. El resto (500, 503) puede traer texto para el equipo técnico.
const ESTADOS_CON_MENSAJE_PARA_EL_CLIENTE = [403, 404, 429, 502]

/**
 * El motivo de una descarga fallida, en palabras para el cliente. Como la petición pide
 * un Blob, el cuerpo del error TAMBIÉN llega como Blob (aunque sea JSON): hay que leerlo
 * y abrirlo para llegar al `detail`.
 */
async function mensajeDeFalla(err: unknown): Promise<string> {
  const estado = estadoHttp(err)
  if (estado === undefined || !ESTADOS_CON_MENSAJE_PARA_EL_CLIENTE.includes(estado)) return FALLA_DE_DESCARGA
  const cuerpo: unknown = axios.isAxiosError(err) ? err.response?.data : undefined
  if (!(cuerpo instanceof Blob)) return detalleDe(err) ?? FALLA_DE_DESCARGA
  try {
    const detail = (JSON.parse(await cuerpo.text()) as { detail?: unknown }).detail
    return typeof detail === 'string' && detail.trim() ? detail : FALLA_DE_DESCARGA
  } catch {
    return FALLA_DE_DESCARGA
  }
}

/** Quita lo que un sistema de archivos no acepta en un nombre («Guía 12/34» → «Guía 12-34»). */
function nombreSeguro(nombreBase: string): string {
  const limpio = nombreBase.replace(/[\\/:*?"<>|\u0000-\u001f]+/g, '-').trim()
  return limpio || 'documento'
}

/**
 * Baja un archivo y lo entrega como descarga del navegador, con el nombre `nombreBase`
 * más la extensión que corresponde a lo que llegó (.pdf, .jpg, .png). Si falla lanza un
 * Error cuyo `message` ya está escrito para mostrárselo al cliente.
 */
export async function descargar(pedir: Pedir, nombreBase: string): Promise<void> {
  const miEpoca = epoca
  let archivo: Blob
  try {
    archivo = (await pedir()).data
  } catch (err) {
    throw new Error(await mensajeDeFalla(err))
  }
  // Se cerró la sesión mientras bajaba: el archivo ya no se entrega.
  if (miEpoca !== epoca) return

  const url = URL.createObjectURL(archivo)
  descargasVivas.add(url)
  const tipo = archivo.type.split(';')[0].trim().toLowerCase()
  const enlace = document.createElement('a')
  enlace.href = url
  enlace.download = `${nombreSeguro(nombreBase)}${EXTENSIONES[tipo] ?? ''}`
  document.body.appendChild(enlace)
  enlace.click()
  enlace.remove()
  window.setTimeout(() => {
    URL.revokeObjectURL(url)
    descargasVivas.delete(url)
  }, ESPERA_ANTES_DE_SOLTAR_MS)
}

/**
 * Suelta TODO lo que este módulo tiene en memoria. Se llama al cerrar sesión: la salida
 * navega por la SPA sin recargar, y sin esto las fotos y los PDF del cliente quedarían
 * vivos en la pestaña para quien use el computador después.
 */
export function limpiarArchivosDelPortal(): void {
  for (const url of urls.values()) URL.revokeObjectURL(url)
  for (const url of descargasVivas) URL.revokeObjectURL(url)
  urls.clear()
  descargasVivas.clear()
  enVuelo.clear()
  montadas.clear()
  epoca += 1
}
