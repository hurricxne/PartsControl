/**
 * <FotoBodega> — miniatura de una foto de recepción, servida CON sesión.
 *
 * POR QUÉ EXISTE
 *   La foto la entrega `GET /api/bodega/fotos/{id}`, que exige sesión, y un `<img>` no
 *   envía el header Authorization. Por eso la imagen se pide con axios (que sí lo
 *   agrega), se convierte en un blob local y ESE se pinta. Es el mismo camino que usa
 *   `abrirDocumento()` en services/api.ts para las guías.
 *
 * QUIÉN ES DUEÑO DEL objectURL — la parte que hay que entender para no romperlo
 *   El dueño es el CACHÉ del módulo, no el componente. Si cada componente revocara su
 *   URL al desmontarse, dejaría con la imagen rota a las otras filas que estén
 *   montadas mostrando esa misma foto. Por eso el `useEffect` NO revoca al desmontar.
 *
 *   `podar()` (que echa lo viejo cuando el caché pasa el techo) TAMPOCO puede revocar
 *   una foto que un `<img>` esté mostrando en ese momento, o se rompe en pantalla sin
 *   que el componente se entere; y no es un caso raro: una recepción cerrada nace con
 *   todas sus miniaturas a la vista. Por eso se lleva un CONTEO DE MONTAJES por foto:
 *   `podar()` solo revoca las que no están montadas en ninguna parte.
 *   Se revoca, entonces, en tres momentos: `podar()` sobre lo NO montado,
 *   `invalidarFoto()` cuando el usuario borra la foto, y `limpiarCacheFotos()` cuando
 *   cambia la sesión (la dispara la suscripción al store, no los botones de salir).
 *
 * SOBRE EL TAMAÑO — no son miniaturas
 *   El backend NO redimensiona: `upload_foto` guarda el archivo tal cual llega y
 *   `serve_foto` devuelve el original. Son fotos de celular de 3-8 MB que acá se
 *   pintan a 64×64. Por eso el techo del caché es bajo. Las fotos reducidas que ve el
 *   cliente en su portal son otras (tabla `foto_articulo`) y no pasan por acá.
 */
import { useEffect, useState, type CSSProperties } from 'react'
import { ImageOff, Loader2 } from 'lucide-react'
import api from '../services/api'
import { useAuthStore } from '../stores/authStore'

/** fotoId → objectURL. El orden de inserción hace de LRU (Map conserva el orden). */
const cache = new Map<number, string>()
/** fotoId → petición en vuelo. Tres <FotoBodega id={7}/> hacen UN solo GET. */
const enVuelo = new Map<number, Promise<string>>()
/**
 * Generación por foto. Cada petición recuerda la generación con que salió y, al llegar,
 * se descarta si ya no es la vigente. La suben `invalidarFoto` (la foto se borró) y
 * `limpiarCacheFotos` (cambió la sesión): así lo que venía viajando no vuelve al caché.
 */
const generacion = new Map<number, number>()
/** fotoId → cuántos <FotoBodega> están montados mostrándola AHORA. `podar()` no
 *  revoca una foto con conteo > 0: estaría rompiendo un `<img>` visible. */
const montadas = new Map<number, number>()

/**
 * Techo de blobs vivos. Bajo a propósito: ver "SOBRE EL TAMAÑO" arriba. Con 20 fotos
 * de 5 MB ya son 100 MB de blobs, sin contar los bitmaps decodificados. Es un OBJETIVO,
 * no un límite duro: si hay más de 20 montadas a la vez, el caché las conserva todas
 * (romperlas en pantalla es peor que un pico de memoria) y vuelve al techo en cuanto
 * se desmontan.
 */
const TOPE_CACHE = 20

function gen(id: number): number {
  return generacion.get(id) ?? 0
}

function montar(id: number) {
  montadas.set(id, (montadas.get(id) ?? 0) + 1)
}

function desmontar(id: number) {
  const n = (montadas.get(id) ?? 0) - 1
  if (n > 0) montadas.set(id, n)
  else montadas.delete(id)
  // Al soltar la última referencia, quizá el caché estaba sobre el techo esperando
  // justamente esto: se intenta podar de nuevo.
  if (n <= 0) podar()
}

/**
 * Libera una foto que dejó de existir. Llamarlo al BORRAR una foto: sin esto el blob
 * queda vivo en memoria, y una petición en vuelo la volvería a meter al caché.
 */
export function invalidarFoto(id: number) {
  const url = cache.get(id)
  if (url) URL.revokeObjectURL(url)
  cache.delete(id)
  enVuelo.delete(id)
  generacion.set(id, gen(id) + 1)
}

/**
 * Vacía el caché, incluido lo que viene viajando. No se exporta: la dispara la
 * suscripción de abajo cada vez que cambia la sesión.
 */
function limpiarCacheFotos() {
  for (const url of cache.values()) URL.revokeObjectURL(url)
  cache.clear()
  // Lo que viene viajando salió con el token anterior y nada lo cancela: se le sube la
  // generación para que `cargar` lo descarte al llegar en vez de guardarlo.
  for (const id of enVuelo.keys()) generacion.set(id, gen(id) + 1)
  enVuelo.clear()
  // `montadas` no se toca: cuenta los <FotoBodega> que hay en pantalla, y cambiar la
  // sesión no desmonta ninguno. Vaciarlo dejaría corto el conteo de los que siguen
  // montados y `podar()` podría revocar una foto que se está mostrando.
}

// El caché es de UNA sesión: los blobs se bajaron con el permiso de quien estaba
// conectado, y salir navega por la SPA sin recargar la página. Se vacía con CADA cambio
// de token, venga de donde venga: el botón de salir de cualquier cascarón, un 401, un
// inicio de sesión, o la otra pestaña que entra con otra cuenta (stores/authStore.ts
// relee el store). Colgarlo del store y no de cada botón cubre todas las salidas sin
// que ninguna tenga que acordarse de llamarlo.
useAuthStore.subscribe((estado, anterior) => {
  if (estado.token !== anterior.token) limpiarCacheFotos()
})

function podar() {
  if (cache.size <= TOPE_CACHE) return
  // Se recorre en orden de inserción (lo más viejo primero) y se echa lo primero que
  // NO esté montado. Si todo lo que sobra está en uso, se deja crecer: romper un `<img>`
  // visible es peor que retener unos MB de más hasta que se desmonte.
  for (const [id, url] of cache) {
    if (cache.size <= TOPE_CACHE) break
    if ((montadas.get(id) ?? 0) > 0) continue
    URL.revokeObjectURL(url)
    cache.delete(id)
  }
}

function cargar(id: number): Promise<string> {
  const enCache = cache.get(id)
  if (enCache) {
    // Re-insertar mueve la entrada al final = "usada recientemente" para el LRU.
    // Ojo: esto solo funciona si TODOS los lectores pasan por acá, incluidos los que
    // ya tienen la foto en caché. Si alguno corta antes (por ejemplo en el efecto del
    // componente), este "toque" no se ejecuta, el caché se vuelve FIFO y puede
    // descartar justamente la foto más usada.
    cache.delete(id)
    cache.set(id, enCache)
    return Promise.resolve(enCache)
  }
  const yaVa = enVuelo.get(id)
  if (yaVa) return yaVa

  const miGeneracion = gen(id)
  const pedido = api
    .get(`/bodega/fotos/${id}`, { responseType: 'blob' })
    .then((r) => {
      const url = URL.createObjectURL(r.data as Blob)
      if (gen(id) !== miGeneracion) {
        // La foto se borró o la sesión cambió mientras esta petición viajaba: no se
        // cachea (quedaría un blob que nadie revocaría) y se avisa al que esperaba.
        URL.revokeObjectURL(url)
        throw new Error('foto invalidada mientras se cargaba')
      }
      cache.set(id, url)
      podar()
      return url
    })
    .finally(() => {
      // Solo borra SU propia entrada: si `invalidarFoto` ya la limpió y arrancó una
      // petición nueva, borrarla a ciegas rompería la deduplicación de esa otra.
      if (enVuelo.get(id) === pedido) enVuelo.delete(id)
    })

  enVuelo.set(id, pedido)
  return pedido
}

type Props = {
  id: number
  className?: string
  style?: CSSProperties
  alt?: string
}

export default function FotoBodega({ id, className, style, alt = 'Foto de la recepción' }: Props) {
  const [url, setUrl] = useState<string | null>(() => cache.get(id) ?? null)
  const [error, setError] = useState<'ausente' | 'fallo' | null>(null)
  /** Sube a 1 cuando el <img> falla: permite UN reintento y no más (evita el bucle). */
  const [reintento, setReintento] = useState(0)

  useEffect(() => {
    let vivo = true
    // Este componente cuenta como un montaje de `id`: `podar()` no revocará su blob
    // mientras esté acá. Se registra en el mismo efecto que la carga para que el par
    // montar/desmontar sea exacto aunque el `id` cambie.
    montar(id)
    setError(null)
    // Se pasa SIEMPRE por `cargar`, incluso con la foto en caché: es lo que mantiene
    // vivo el "toque" del LRU. `cargar` resuelve al instante si ya la tiene.
    cargar(id)
      .then((u) => {
        if (vivo) setUrl(u)
      })
      .catch((e) => {
        if (!vivo) return
        // 404 = la fila existe pero el archivo no está en disco (borrado a mano,
        // restore parcial). Es un estado real y se muestra distinto de un fallo de red.
        setUrl(null)
        setError(e?.response?.status === 404 ? 'ausente' : 'fallo')
      })
    // `vivo` protege del doble montaje de React 18 en modo estricto y de que el id
    // cambie mientras la petición viaja. NO se revoca el blob acá: el dueño es el caché.
    return () => {
      vivo = false
      desmontar(id)
    }
  }, [id, reintento])

  const claseCaja = `${className ?? ''} flex items-center justify-center`

  if (error) {
    const mensaje = error === 'ausente' ? 'La foto ya no existe' : 'No se pudo cargar la foto'
    return (
      <div
        className={claseCaja}
        style={{ ...style, backgroundColor: 'var(--surface-200)' }}
        role="img"
        aria-label={mensaje}
        title={mensaje}
      >
        <ImageOff className="w-4 h-4" style={{ color: 'var(--text-faint)' }} />
      </div>
    )
  }

  if (!url) {
    return (
      <div
        className={claseCaja}
        style={{ ...style, backgroundColor: 'var(--surface-200)' }}
        role="status"
        aria-label="Cargando foto"
      >
        <Loader2 className="w-4 h-4 animate-spin" style={{ color: 'var(--text-faint)' }} />
      </div>
    )
  }

  return (
    <img
      src={url}
      alt={alt}
      className={className}
      style={style}
      onError={() => {
        // Red de seguridad: `podar()` no revoca un blob en uso, pero el navegador puede
        // soltarlo bajo presión de memoria.
        // Si pasa, se pide de nuevo, UNA sola vez (más sería un bucle).
        if (reintento > 0) {
          setError('fallo')
          return
        }
        invalidarFoto(id)
        setUrl(null)
        setReintento(1)
      }}
    />
  )
}
