// Fotos de un ítem: rejilla de miniaturas y visor a pantalla completa. La usa ItemPage.
//
// Cada imagen se pide con portalAPI.foto(id) (GET /api/portal/fotos/{id}) a través de
// archivos.ts::useImagenPrivada: llega como Blob con el token de la sesión y se muestra
// desde memoria. La miniatura y el visor usan la MISMA clave (claveDeFoto), así que una
// foto se descarga una sola vez.
//
// DECISIONES QUE NO SE VEN EN EL CÓDIGO
//   · Las fotos no se piden hasta que la rejilla está por entrar en la pantalla
//     (useYaSeVio). En un teléfono quedan debajo del recorrido del ítem, y quien solo
//     vino a ver la fecha comprometida no tiene por qué bajar ~300 KB por foto con la
//     señal de una faena.
//   · El visor es un <dialog> abierto con showModal(): el navegador atrapa el foco,
//     cierra con Esc (en Android, también con «Atrás») y deja inerte el resto de la
//     página. Nada de eso se reimplementa acá.
//   · Quién manda: el estado `abierta` de GaleriaDeFotos. El <dialog> se sincroniza con
//     él en un efecto, y su evento `close` (Esc) avisa de vuelta. Por eso `cerrar` puede
//     llegar dos veces por un mismo cierre y tiene que tolerarlo.
//   · Una miniatura que no cargó igual abre el visor: la foto grande se vuelve a pedir al
//     montarse y ofrece «Reintentar». Al cerrar el visor las imágenes de las miniaturas
//     se remontan (`ronda`): las que ya están en memoria aparecen al instante y las que
//     habían fallado se piden de nuevo.
//   · Las miniaturas son todas 4:3 y recortan la foto (clase portal-foto): la rejilla no
//     salta cuando llega cada imagen. La foto completa se ve en el visor.
//   · Sin botón de descarga ni enlace a la imagen, a propósito. Y el visor NO bloquea el
//     zoom con dos dedos: es la forma de leer una placa o un número grabado en la pieza.
import { useEffect, useId, useRef, useState, type KeyboardEvent, type RefObject } from 'react'
import { ChevronLeft, ChevronRight, ImageIcon, ImageOff, Loader2, RefreshCw, X } from 'lucide-react'
import { portalAPI } from './api'
import { useImagenPrivada } from './archivos'
import { fmtFecha } from './formato'
import type { FotoPortal } from './types'

// Cuánto antes de que la rejilla entre en la pantalla se empiezan a pedir las fotos.
const MARGEN_DE_CARGA = '300px'

interface GaleriaDeFotosProps {
  fotos: FotoPortal[]
  titulo: string
}

/** Clave de una foto en la caché de archivos.ts. */
function claveDeFoto(fotoId: number): string {
  return `foto-${fotoId}`
}

/** «, tomada el 12 ago 2026», o nada si la foto no tiene fecha registrada. */
function fechaDeToma(fecha: string | null): string {
  return fecha ? `, tomada el ${fmtFecha(fecha)}` : ''
}

/**
 * true desde que el elemento entra (o está por entrar) en la pantalla; no vuelve a false.
 * En un navegador sin IntersectionObserver responde true de inmediato: se carga todo.
 */
function useYaSeVio(ref: RefObject<Element>): boolean {
  const [visto, setVisto] = useState(() => typeof IntersectionObserver === 'undefined')

  useEffect(() => {
    const elemento = ref.current
    if (visto || !elemento) return
    const observador = new IntersectionObserver(
      (entradas) => { if (entradas.some((e) => e.isIntersecting)) setVisto(true) },
      { rootMargin: MARGEN_DE_CARGA }
    )
    observador.observe(elemento)
    return () => observador.disconnect()
  }, [ref, visto])

  return visto
}

export default function GaleriaDeFotos({ fotos, titulo }: GaleriaDeFotosProps) {
  const idTitulo = useId()
  const refRejilla = useRef<HTMLUListElement>(null)
  const pedirMiniaturas = useYaSeVio(refRejilla)
  // Posición en `fotos` de la que muestra el visor; null = visor cerrado.
  const [abierta, setAbierta] = useState<number | null>(null)
  const [ronda, setRonda] = useState(0)
  // La miniatura que recibe el foco cuando el visor termine de cerrarse.
  const refVolverA = useRef<number | null>(null)

  const cerrar = () => {
    if (abierta === null) return
    refVolverA.current = abierta
    setAbierta(null)
    setRonda((r) => r + 1)
  }

  // El foco vuelve a la miniatura de la ÚLTIMA foto vista (no a la que abrió el visor):
  // quien avanzó hasta la quinta sigue desde ahí. Va en un efecto porque mientras el
  // <dialog> sigue abierto todo lo de afuera está inerte y no acepta el foco; el efecto
  // de Visor (hijo) lo cierra antes de que corra este.
  useEffect(() => {
    if (abierta !== null || refVolverA.current === null) return
    refRejilla.current
      ?.querySelector<HTMLButtonElement>(`[data-posicion="${refVolverA.current}"]`)
      ?.focus()
    refVolverA.current = null
  }, [abierta])

  return (
    <section aria-labelledby={idTitulo}>
      <h2 id={idTitulo} className="text-lg font-bold tracking-tight">{titulo}</h2>
      <p className="portal-suave mt-1 text-sm">Fotos tomadas por nuestro equipo al preparar tu despacho.</p>

      <ul ref={refRejilla} className="portal-fotos portal-galeria-rejilla mt-4">
        {fotos.map((foto, i) => (
          <li key={foto.id}>
            {/* El botón es estable y solo cambia lo de adentro: así no pierde el foco
                cuando llega la imagen ni cuando se remonta por `ronda`. */}
            <button
              type="button"
              className="portal-foto portal-galeria-miniatura"
              data-posicion={i}
              aria-haspopup="dialog"
              aria-label={`Ver foto ${i + 1} de ${fotos.length}${fechaDeToma(foto.fecha)}`}
              onClick={() => setAbierta(i)}
            >
              {pedirMiniaturas
                ? <ImagenDeMiniatura key={ronda} foto={foto} />
                : <ImageIcon className="portal-galeria-icono" aria-hidden="true" />}
            </button>
          </li>
        ))}
      </ul>

      <Visor titulo={titulo} fotos={fotos} indice={abierta} onIr={setAbierta} onCerrar={cerrar} />
    </section>
  )
}

/** Lo de adentro de una miniatura: la imagen, o por qué todavía no está. */
function ImagenDeMiniatura({ foto }: { foto: FotoPortal }) {
  const { url, estado } = useImagenPrivada(() => portalAPI.foto(foto.id), claveDeFoto(foto.id))

  if (estado === 'lista' && url) {
    // alt vacío: el nombre lo da el aria-label del botón que la contiene. width/height
    // son las medidas reales de la foto (el navegador reserva el lugar antes de pintarla).
    return <img src={url} alt="" width={foto.ancho} height={foto.alto} decoding="async" draggable={false} />
  }
  if (estado === 'error') {
    return (
      <span className="portal-galeria-fallo">
        <ImageOff className="portal-galeria-icono" aria-hidden="true" />
        No se pudo cargar
      </span>
    )
  }
  return <Loader2 className="portal-galeria-icono portal-giro" aria-hidden="true" />
}

// ─── Visor ────────────────────────────────────────────────────────────────────
interface VisorProps {
  titulo: string
  fotos: FotoPortal[]
  /** Posición en `fotos` de la que se muestra; null = cerrado. */
  indice: number | null
  onIr: (indice: number) => void
  onCerrar: () => void
}

function Visor({ titulo, fotos, indice, onIr, onCerrar }: VisorProps) {
  const refDialogo = useRef<HTMLDialogElement>(null)
  const refLienzo = useRef<HTMLDivElement>(null)
  // Sube con cada «Reintentar»: remonta FotoGrande, que es lo que vuelve a pedir la foto.
  const [intento, setIntento] = useState(0)
  // undefined también si la lista se acortó (una recarga) con el visor abierto: se cierra.
  const foto = indice !== null ? fotos[indice] : undefined
  const abierto = foto !== undefined
  const total = fotos.length

  // El <dialog> sigue al estado. Safari anterior a 15.4 no tiene showModal(): se abre
  // con el atributo `open` (cubre la pantalla igual, sin foco atrapado).
  useEffect(() => {
    const dialogo = refDialogo.current
    if (!dialogo || dialogo.open === abierto) return
    const esModal = typeof dialogo.showModal === 'function'
    if (abierto) {
      if (esModal) dialogo.showModal()
      else dialogo.setAttribute('open', '')
    } else if (esModal) {
      dialogo.close()
    } else {
      dialogo.removeAttribute('open')
    }
  }, [abierto])

  // Con el visor abierto la página de atrás no se desplaza: un arrastre sobre la foto la
  // movería sin que se note, y al cerrar se volvería a otra parte de la pantalla.
  useEffect(() => {
    if (!abierto) return
    const raiz = document.documentElement
    const previo = raiz.style.overflow
    raiz.style.overflow = 'hidden'
    return () => { raiz.style.overflow = previo }
  }, [abierto])

  const ir = (paso: number) => {
    if (indice === null) return
    const destino = indice + paso
    if (destino >= 0 && destino < total) onIr(destino)
  }

  const alTeclear = (ev: KeyboardEvent) => {
    if (ev.key !== 'ArrowLeft' && ev.key !== 'ArrowRight') return
    ev.preventDefault()
    ir(ev.key === 'ArrowLeft' ? -1 : 1)
  }

  const reintentar = () => {
    setIntento((n) => n + 1)
    // El botón «Reintentar» desaparece al remontar: el foco se queda dentro del visor.
    refLienzo.current?.focus()
  }

  const posicion = indice !== null ? indice + 1 : 0
  const hayAnterior = posicion > 1
  const haySiguiente = posicion < total

  return (
    <dialog
      ref={refDialogo}
      className="portal-visor"
      aria-label={titulo}
      onClose={onCerrar}
      onKeyDown={alTeclear}
    >
      <div className="portal-visor-barra">
        {/* Siempre montada: al pasar de una foto a otra el lector de pantalla anuncia el
            texto nuevo («Foto 3 de 5 · 12 ago 2026»). */}
        <p role="status" className="portal-galeria-cuenta">
          {foto && `Foto ${posicion} de ${total}${foto.fecha ? ` · ${fmtFecha(foto.fecha)}` : ''}`}
        </p>
        <button type="button" className="portal-boton-cabecera" onClick={onCerrar}>
          <X className="w-4 h-4" aria-hidden="true" />
          Cerrar
        </button>
      </div>

      <div ref={refLienzo} tabIndex={-1} className="portal-visor-lienzo portal-foco-silencioso">
        {foto && (
          <FotoGrande
            key={`${foto.id}-${intento}`}
            foto={foto}
            descripcion={`Foto ${posicion} de ${total}${fechaDeToma(foto.fecha)}`}
            onReintentar={reintentar}
          />
        )}
      </div>

      {total > 1 && (
        // aria-disabled y no disabled: en la primera y en la última foto el botón se
        // apaga sin soltar el foco del teclado (ir() ya ignora el paso que no existe).
        <div className="portal-galeria-pasos">
          <button
            type="button"
            className="portal-boton-cabecera portal-galeria-paso"
            aria-disabled={!hayAnterior}
            onClick={() => ir(-1)}
          >
            <ChevronLeft className="w-5 h-5" aria-hidden="true" />
            Anterior
          </button>
          <button
            type="button"
            className="portal-boton-cabecera portal-galeria-paso"
            aria-disabled={!haySiguiente}
            onClick={() => ir(1)}
          >
            Siguiente
            <ChevronRight className="w-5 h-5" aria-hidden="true" />
          </button>
        </div>
      )}
    </dialog>
  )
}

interface FotoGrandeProps {
  foto: FotoPortal
  descripcion: string
  onReintentar: () => void
}

/** La foto del visor. Pasar a una que ya está en memoria no muestra «Cargando». */
function FotoGrande({ foto, descripcion, onReintentar }: FotoGrandeProps) {
  const { url, estado } = useImagenPrivada(() => portalAPI.foto(foto.id), claveDeFoto(foto.id))

  if (estado === 'lista' && url) {
    return (
      <img
        src={url}
        alt={descripcion}
        width={foto.ancho}
        height={foto.alto}
        className="portal-galeria-foto-grande"
        draggable={false}
      />
    )
  }
  if (estado === 'error') {
    return (
      <div role="alert" className="portal-galeria-aviso">
        <ImageOff className="portal-galeria-icono" aria-hidden="true" />
        <p>No se pudo cargar la foto. Revisa tu conexión e intenta de nuevo.</p>
        <button type="button" className="portal-boton-cabecera" onClick={onReintentar}>
          <RefreshCw className="w-4 h-4" aria-hidden="true" />
          Reintentar
        </button>
      </div>
    )
  }
  return (
    <p role="status" className="portal-galeria-aviso">
      <Loader2 className="portal-galeria-icono portal-giro" aria-hidden="true" />
      Cargando foto…
    </p>
  )
}
