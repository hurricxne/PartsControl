// Una foto publicada para el cliente: la miniatura, su botón «Quitar» y la confirmación
// en línea. La usa FotosParaCliente.tsx (pestaña «Foto despacho» de la pantalla INTERNA
// Despachos). Consume portalAdminAPI.fotoPublicada (la imagen) y borrarFoto.
//
// DECISIONES QUE NO SE VEN EN EL CÓDIGO
//   · La imagen no se pinta con <img src="/api/…">: el endpoint exige el token de la
//     sesión y un <img> no lo envía. Se pide con axios como Blob y se muestra desde un
//     object URL local.
//   · No se usa components/FotoBodega: pide a /bodega/fotos (las fotos de recepción) y
//     guarda los blobs en una caché de módulo. Acá el dueño del object URL es la propia
//     tarjeta (useFotoPublicada lo revoca al desmontarse): son a lo más `maximo`
//     derivados de ~300 KB y duran lo que dura la pestaña abierta.
//   · La miniatura usa object-contain (la foto ENTERA, sin recortar): mirarla acá es la
//     única revisión que existe, y una persona o una etiqueta ajena en el borde tiene
//     que verse.
//   · La confirmación va en línea y no en un cuadro aparte: la tarjeta ya vive dentro
//     de una ventana modal (FotosItemModal).
import { useEffect, useId, useRef, useState } from 'react'
import { ImageOff, Loader2, RotateCcw, Trash2 } from 'lucide-react'
import { portalAdminAPI, estadoHttp } from './api'
import type { FotoPublicada } from './types'
import { fmtFecha } from './formato'
import { TONO_ERROR } from './tonos'

const FALLA_AL_QUITAR = 'No se pudo quitar la foto. Revisa tu conexión e intenta de nuevo.'

// min-h de 44 px: la pestaña también se usa desde el teléfono, al momento de despachar.
const botonDeTextoCls =
  'inline-flex items-center gap-1 rounded-lg px-2 min-h-[2.75rem] text-xs font-medium transition-colors disabled:opacity-40 disabled:cursor-not-allowed'
const botonDeConfirmacionCls =
  'w-full flex items-center justify-center gap-1.5 rounded-xl px-3 min-h-[2.75rem] text-xs font-semibold transition-colors disabled:opacity-50 disabled:cursor-not-allowed'
const cajaCls = 'w-full aspect-square rounded-xl border'
const estiloDeCaja = { borderColor: 'var(--border)', backgroundColor: 'var(--surface-200)' }

type Imagen =
  | { estado: 'cargando' }
  | { estado: 'lista'; url: string }
  // `ausente`: la fila existe pero el archivo ya no está en disco (404): reintentar no sirve.
  | { estado: 'error'; ausente: boolean }

/** La imagen de una foto publicada, lista para un <img src>. Dueño de su object URL. */
function useFotoPublicada(fotoId: number): { imagen: Imagen; reintentar: () => void } {
  const [imagen, setImagen] = useState<Imagen>({ estado: 'cargando' })
  const [vuelta, setVuelta] = useState(0)

  useEffect(() => {
    // `vigente` descarta la respuesta de un montaje ya desechado (StrictMode monta dos
    // veces en desarrollo): sin él quedaría un object URL que nadie revoca.
    let vigente = true
    let url: string | null = null
    setImagen({ estado: 'cargando' })
    portalAdminAPI.fotoPublicada(fotoId)
      .then(({ data }) => {
        if (!vigente) return
        url = URL.createObjectURL(data)
        setImagen({ estado: 'lista', url })
      })
      .catch((err: unknown) => {
        if (vigente) setImagen({ estado: 'error', ausente: estadoHttp(err) === 404 })
      })
    return () => {
      vigente = false
      if (url) URL.revokeObjectURL(url)
    }
  }, [fotoId, vuelta])

  return { imagen, reintentar: () => setVuelta((n) => n + 1) }
}

interface CajaDeFotoProps {
  imagen: Imagen
  alt: string
  onReintentar: () => void
}

/** El cuadrado de la miniatura en sus tres estados: cargando, la foto, o por qué no se ve. */
function CajaDeFoto({ imagen, alt, onReintentar }: CajaDeFotoProps) {
  if (imagen.estado === 'lista') {
    return <img src={imagen.url} alt={alt} className={`${cajaCls} object-contain`} style={estiloDeCaja} />
  }
  if (imagen.estado === 'cargando') {
    return (
      <div role="status" aria-label="Cargando foto" className={`${cajaCls} flex items-center justify-center`} style={estiloDeCaja}>
        <Loader2 className="w-5 h-5 animate-spin" style={{ color: 'var(--text-muted)' }} aria-hidden="true" />
      </div>
    )
  }
  return (
    <div className={`${cajaCls} flex flex-col items-center justify-center gap-1 p-2 text-center`} style={estiloDeCaja}>
      <ImageOff className="w-5 h-5" style={{ color: 'var(--text-muted)' }} aria-hidden="true" />
      <p className="text-[11px] leading-tight" style={{ color: 'var(--text-primary)' }}>
        {imagen.ausente ? 'El archivo de esta foto ya no existe.' : 'No se pudo cargar la foto.'}
      </p>
      {!imagen.ausente && (
        <button type="button" onClick={onReintentar} className={`${botonDeTextoCls} hover:bg-[var(--surface-300)]`}
          style={{ color: 'var(--text-primary)' }}>
          <RotateCcw className="w-3.5 h-3.5" aria-hidden="true" /> Reintentar
        </button>
      )}
    </div>
  )
}

interface TarjetaDeFotoPublicadaProps {
  foto: FotoPublicada
  /** «foto 2 de 3 de 1R-0716»: completa el texto alternativo y el nombre del botón. */
  rotulo: string
  /** Hay una subida en curso: no se quita nada hasta que termine. */
  bloqueada: boolean
  /** El backend confirmó el borrado: el padre recarga la lista. */
  onQuitada: () => void
}

type Paso = 'reposo' | 'confirmando' | 'quitando'

export default function TarjetaDeFotoPublicada({ foto, rotulo, bloqueada, onQuitada }: TarjetaDeFotoPublicadaProps) {
  const { imagen, reintentar } = useFotoPublicada(foto.id)
  const [paso, setPaso] = useState<Paso>('reposo')
  const [error, setError] = useState<string | null>(null)
  const idPregunta = useId()
  const refQuitar = useRef<HTMLButtonElement>(null)
  const refCancelar = useRef<HTMLButtonElement>(null)
  const pasoAnterior = useRef<Paso>('reposo')
  const montada = useRef(false)

  useEffect(() => {
    montada.current = true
    return () => { montada.current = false }
  }, [])

  // El foco acompaña a la pregunta. Nace en «Cancelar» (un Enter por reflejo no debe
  // borrar) y, si la foto no se quitó, vuelve a «Quitar»: los dos bloques se reemplazan
  // entre sí y sin esto el foco caería al principio de la página.
  useEffect(() => {
    if (paso === 'confirmando') refCancelar.current?.focus()
    else if (paso === 'reposo' && pasoAnterior.current !== 'reposo') refQuitar.current?.focus()
    pasoAnterior.current = paso
  }, [paso])

  const quitar = async () => {
    setPaso('quitando')
    setError(null)
    try {
      await portalAdminAPI.borrarFoto(foto.id)
    } catch (err) {
      // 404 = otra persona ya la quitó: el resultado es el que se pedía.
      if (estadoHttp(err) !== 404) {
        if (montada.current) {
          setError(FALLA_AL_QUITAR)
          setPaso('reposo')
        }
        return
      }
    }
    // La tarjeta queda en «Quitando…» hasta que la lista recargada la retire.
    if (montada.current) onQuitada()
  }

  const alTeclear = (e: React.KeyboardEvent) => {
    if (e.key !== 'Escape') return
    // FotosItemModal cierra la ventana con Escape (escucha en window). Con la pregunta
    // abierta, Escape retira solo la pregunta.
    e.stopPropagation()
    if (paso === 'confirmando') setPaso('reposo')
  }

  return (
    <li className="space-y-1.5 min-w-0">
      <CajaDeFoto imagen={imagen} alt={`Para el cliente: ${rotulo}`} onReintentar={reintentar} />
      {paso === 'reposo' ? (
        <div className="flex flex-wrap items-center justify-between gap-x-2">
          <p className="text-[11px] whitespace-nowrap" style={{ color: 'var(--text-muted)' }}>
            {foto.fecha ? fmtFecha(foto.fecha) : ''}
          </p>
          <button ref={refQuitar} type="button" disabled={bloqueada} onClick={() => setPaso('confirmando')}
            aria-label={`Quitar la ${rotulo}`} className={`${botonDeTextoCls} ${TONO_ERROR} hover:bg-red-500/10`}>
            <Trash2 className="w-3.5 h-3.5" aria-hidden="true" /> Quitar
          </button>
        </div>
      ) : (
        <div role="group" aria-labelledby={idPregunta} onKeyDown={alTeclear} className="space-y-1.5">
          <p id={idPregunta} className="text-xs font-medium leading-snug" style={{ color: 'var(--text-primary)' }}>
            ¿Quitar esta foto del portal del cliente?
          </p>
          <button type="button" onClick={quitar} disabled={paso === 'quitando'}
            className={`${botonDeConfirmacionCls} text-white bg-red-600 hover:bg-red-700`}>
            {paso === 'quitando' && <Loader2 className="w-3.5 h-3.5 animate-spin" aria-hidden="true" />}
            {paso === 'quitando' ? 'Quitando…' : 'Sí, quitar'}
          </button>
          <button ref={refCancelar} type="button" onClick={() => setPaso('reposo')} disabled={paso === 'quitando'}
            className={`${botonDeConfirmacionCls} border hover:bg-[var(--surface-300)]`}
            style={{ backgroundColor: 'var(--surface-200)', borderColor: 'var(--border)', color: 'var(--text-primary)' }}>
            Cancelar
          </button>
        </div>
      )}
      {error && <p role="alert" className={`text-xs leading-snug ${TONO_ERROR}`}>{error}</p>}
    </li>
  )
}
