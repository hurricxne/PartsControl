// Contenido de la pestaña «Foto despacho» de components/FotosItemModal.tsx (pantalla
// INTERNA Despachos): las fotos de un ítem que ve el CLIENTE en su portal. Acá el
// personal las mira, las sube y las quita.
// Consume portalAdminAPI.fotosDeItem (lista y máximo) y subirFoto; cada foto, con su
// imagen y su borrado, es una TarjetaDeFotoPublicada. Reglas del backend:
// portal/router_admin_fotos.py y portal/fotos.py.
//
// SUBIR ES PUBLICAR. No hay un paso de aprobación: lo que se sube queda a la vista del
// cliente en el seguimiento de ese ítem. Por eso el aviso está fijo ANTES de los botones
// (no en un tooltip ni después de un clic) y quitar una foto pide confirmación.
//
// DECISIONES QUE NO SE VEN EN EL CÓDIGO
//   · Las fotos se suben DE A UNA y en orden. El backend cuenta las que ya hay antes de
//     aceptar cada una, y con mala señal una tanda en paralelo dejaría varias a medias.
//     La primera que falla detiene la tanda; las anteriores quedan publicadas.
//   · Los rechazos 400 (el archivo no sirve) y 409 (máximo alcanzado, ítem en reclamo al
//     proveedor) traen un texto escrito para el operador y se muestran tal cual. Los
//     demás fallos llevan mensaje propio: un 500/503 puede traer texto para el equipo
//     técnico.
//   · En teléfono hay dos botones: «Tomar foto» abre la cámara (`capture`), que entrega
//     una sola foto por vez; «Elegir de la galería» deja marcar varias ya tomadas. En un
//     computador `capture` no hace nada, así que ahí se muestra solo el segundo.
//   · Se monta una vez por ítem (FotosItemModal le pone `key`): los mensajes de una
//     subida no pasan de un ítem a otro.
//   · Estilos del ERP (Tailwind + variables de index.css), no las clases portal-*: esta
//     pantalla la usa el personal y no vive dentro de .portal-root.
import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { AlertCircle, AlertTriangle, Camera, CheckCircle2, ImageOff, ImagePlus, Loader2, RotateCcw } from 'lucide-react'
import { portalAdminAPI, detalleDe, estadoHttp } from './api'
import type { FotoPublicada } from './types'
import TarjetaDeFotoPublicada from './TarjetaDeFotoPublicada'
import { TONO_ATENCION, TONO_ERROR, TONO_OK } from './tonos'

// Los mismos tres formatos que acepta portal/fotos.py::derivado.
const FORMATOS_ACEPTADOS = 'image/jpeg,image/png,image/webp'
const FALLA_DE_CARGA = 'No se pudieron cargar las fotos. Revisa tu conexión e intenta de nuevo.'
const FALLA_DE_SUBIDA = 'No se pudo subir la foto. Revisa tu conexión e intenta de nuevo.'

// min-h de 44 px: la pestaña también se usa desde el teléfono, al momento de despachar.
const botonPrincipalCls = 'btn-primary text-sm justify-center min-h-[2.75rem]'
const botonSecundarioCls =
  'btn-secondary text-sm justify-center min-h-[2.75rem] disabled:opacity-50 disabled:cursor-not-allowed'

interface FotosParaClienteProps {
  itemCotizacionId: number
  numeroParte?: string | null
}

type Carga =
  | { estado: 'cargando' }
  | { estado: 'lista'; maximo: number; fotos: FotoPublicada[] }
  | { estado: 'error'; mensaje: string; reintentable: boolean }

/** En qué va la tanda que se está subiendo: «Subiendo 2 de 3…». */
interface Avance {
  actual: number
  total: number
}

interface Rechazo {
  mensaje: string
  // Qué archivo detuvo la tanda, cuando se eligió más de uno.
  contexto: string | null
}

function fallaDeCarga(err: unknown): Carga {
  const estado = estadoHttp(err)
  // 404 = el ítem no pertenece a una venta cerrada; 403 = la cuenta no opera el portal.
  // Los dos textos están escritos para el operador y reintentar no cambia la respuesta.
  const definitiva = estado === 404 || estado === 403
  return {
    estado: 'error',
    mensaje: (definitiva && detalleDe(err)) || FALLA_DE_CARGA,
    reintentable: !definitiva,
  }
}

function motivoDeRechazo(err: unknown): string {
  const estado = estadoHttp(err)
  return ((estado === 400 || estado === 409) && detalleDe(err)) || FALLA_DE_SUBIDA
}

function resumenDeSubida(publicadas: number, sinCupo: number, maximo: number): string {
  const partes: string[] = []
  if (publicadas > 0) {
    partes.push(`${publicadas === 1 ? 'Foto publicada' : `${publicadas} fotos publicadas`} en el portal del cliente.`)
  }
  if (sinCupo > 0) {
    partes.push(`${sinCupo === 1 ? 'Quedó 1 sin subir' : `Quedaron ${sinCupo} sin subir`}: el máximo es ${maximo} fotos por ítem.`)
  }
  return partes.join(' ')
}

/**
 * ¿El equipo se maneja con el dedo (teléfono, tablet)? Solo decide si se muestra «Tomar
 * foto». Si la detección falla no se pierde nada: en un teléfono «Elegir de la galería»
 * igual ofrece la cámara, y en un computador el botón de cámara abriría el selector de
 * archivos.
 */
function seManejaConElDedo(): boolean {
  return typeof window.matchMedia === 'function' && window.matchMedia('(pointer: coarse)').matches
}

interface ControlesDeSubidaProps {
  /** Hay una tanda subiendo: los botones esperan a que termine. */
  subiendo: boolean
  /** El ítem ya tiene `maximo` fotos: no se puede subir otra hasta quitar una. */
  lleno: boolean
  maximo: number
  onElegir: (elegidas: File[]) => void
}

/** El aviso de publicación y, debajo, los botones para tomar o elegir fotos. */
function ControlesDeSubida({ subiendo, lleno, maximo, onElegir }: ControlesDeSubidaProps) {
  const [conCamara] = useState(seManejaConElDedo)
  const id = useId()
  const idAviso = `${id}-aviso`
  const idLleno = `${id}-lleno`
  const refCamara = useRef<HTMLInputElement>(null)
  const refArchivos = useRef<HTMLInputElement>(null)
  const bloqueado = subiendo || lleno
  // Quien llega al botón con lector de pantalla oye el aviso (y el motivo del bloqueo).
  const descritoPor = lleno ? `${idAviso} ${idLleno}` : idAviso

  const alElegir = (e: React.ChangeEvent<HTMLInputElement>) => {
    const elegidas = Array.from(e.target.files ?? [])
    // Se vacía el campo: si no, volver a elegir el MISMO archivo (tras un rechazo, por
    // ejemplo) no dispara `change` y el botón parece no hacer nada.
    e.target.value = ''
    onElegir(elegidas)
  }

  return (
    <div className="space-y-3">
      <div id={idAviso} className="flex items-start gap-2.5 rounded-xl border border-amber-500/40 bg-amber-500/10 px-3 py-2.5">
        <AlertTriangle className={`w-4 h-4 mt-0.5 shrink-0 ${TONO_ATENCION}`} aria-hidden="true" />
        <p className="text-sm leading-snug" style={{ color: 'var(--text-primary)' }}>
          <strong className="font-semibold">Lo que subas acá lo ve el cliente en su portal, sin revisión previa.</strong>{' '}
          Sube solo fotos del repuesto: sin personas, sin documentos, sin etiquetas de otros clientes.
        </p>
      </div>

      {/* Los <input> van ocultos y los abre su botón: así el control visible es un botón
          de verdad (foco, deshabilitado, 44 px). */}
      <div className="flex flex-wrap items-center gap-2">
        {conCamara && (
          <>
            <button type="button" disabled={bloqueado} aria-describedby={descritoPor}
              onClick={() => refCamara.current?.click()} className={botonPrincipalCls}>
              <Camera className="w-4 h-4" aria-hidden="true" /> Tomar foto
            </button>
            <input ref={refCamara} type="file" accept={FORMATOS_ACEPTADOS} capture="environment"
              className="hidden" onChange={alElegir} />
          </>
        )}
        <button type="button" disabled={bloqueado} aria-describedby={descritoPor}
          onClick={() => refArchivos.current?.click()} className={conCamara ? botonSecundarioCls : botonPrincipalCls}>
          <ImagePlus className="w-4 h-4" aria-hidden="true" /> {conCamara ? 'Elegir de la galería' : 'Elegir fotos'}
        </button>
        <input ref={refArchivos} type="file" accept={FORMATOS_ACEPTADOS} multiple
          className="hidden" onChange={alElegir} />
      </div>

      {lleno && (
        <p id={idLleno} className={`text-xs ${TONO_ATENCION}`}>
          Este ítem ya tiene el máximo de {maximo} fotos. Quita una para subir otra.
        </p>
      )}
    </div>
  )
}

export default function FotosParaCliente({ itemCotizacionId, numeroParte }: FotosParaClienteProps) {
  const [carga, setCarga] = useState<Carga>({ estado: 'cargando' })
  const [avance, setAvance] = useState<Avance | null>(null)
  const [rechazo, setRechazo] = useState<Rechazo | null>(null)
  // Número de rechazo: es la `key` del aviso. Dos rechazos IGUALES seguidos no cambian
  // el nodo y un lector de pantalla no repetiría el segundo; con la key se vuelve a leer.
  const [intento, setIntento] = useState(0)
  // Lo último que salió bien, para la región role="status".
  const [hecho, setHecho] = useState('')

  const idTitulo = useId()
  const refTitulo = useRef<HTMLHeadingElement>(null)
  // Número de la última carga pedida: una respuesta que llega tarde (ya se pidió otra, o
  // la pestaña se cerró) no pisa a la más reciente.
  const ultimaCarga = useRef(0)
  const montado = useRef(false)

  // `conEspera` = primera carga o Reintentar. Tras subir o quitar se recarga sin
  // esconder las miniaturas que ya están a la vista.
  const cargar = useCallback(async (conEspera: boolean) => {
    ultimaCarga.current += 1
    const esta = ultimaCarga.current
    if (conEspera) setCarga({ estado: 'cargando' })
    try {
      const { data } = await portalAdminAPI.fotosDeItem(itemCotizacionId)
      if (esta === ultimaCarga.current) setCarga({ estado: 'lista', maximo: data.maximo, fotos: data.fotos })
    } catch (err) {
      if (esta === ultimaCarga.current) setCarga(fallaDeCarga(err))
    }
  }, [itemCotizacionId])

  useEffect(() => {
    montado.current = true
    void cargar(true)
    return () => {
      montado.current = false
      ultimaCarga.current += 1
    }
  }, [cargar])

  const subir = async (elegidas: File[]) => {
    if (carga.estado !== 'lista' || avance !== null) return
    // Lo que no cabe bajo el máximo no se envía: el backend lo rechazaría con 409.
    const tanda = elegidas.slice(0, Math.max(0, carga.maximo - carga.fotos.length))
    if (tanda.length === 0) return
    setRechazo(null)
    setHecho('')
    let publicadas = 0
    let detenida: Rechazo | null = null
    for (const [i, archivo] of tanda.entries()) {
      setAvance({ actual: i + 1, total: tanda.length })
      try {
        await portalAdminAPI.subirFoto(itemCotizacionId, archivo)
        publicadas += 1
      } catch (err) {
        detenida = {
          mensaje: motivoDeRechazo(err),
          contexto: tanda.length > 1 ? `La subida se detuvo en «${archivo.name}» (${i + 1} de ${tanda.length}).` : null,
        }
        break
      }
      // La pestaña se cerró a mitad de la tanda: no se sigue publicando sin nadie mirando.
      if (!montado.current) return
    }
    // La lista se recarga ANTES de soltar los botones: con la lista vieja el cupo que
    // queda estaría mal calculado. Se recarga también si hubo un rechazo, porque las
    // fotos anteriores de la tanda sí quedaron publicadas.
    await cargar(false)
    if (!montado.current) return
    setAvance(null)
    setHecho(resumenDeSubida(publicadas, detenida ? 0 : elegidas.length - tanda.length, carga.maximo))
    if (detenida) {
      setRechazo(detenida)
      setIntento((n) => n + 1)
    }
  }

  const alQuitar = () => {
    setRechazo(null)
    setHecho('Foto quitada del portal del cliente.')
    // La tarjeta que tenía el foco va a desaparecer: el foco pasa al título de la sección.
    refTitulo.current?.focus()
    void cargar(false)
  }

  return (
    <section aria-labelledby={idTitulo} className="space-y-4">
      <div className="flex items-baseline justify-between gap-3">
        {/* Enfocable por código (no con Tab): destino del foco tras quitar una foto. */}
        <h4 id={idTitulo} ref={refTitulo} tabIndex={-1} className="text-sm font-semibold outline-none"
          style={{ color: 'var(--text-primary)' }}>
          Fotos para el cliente
        </h4>
        {carga.estado === 'lista' && (
          <p className="text-xs tabular-nums whitespace-nowrap" style={{ color: 'var(--text-muted)' }}>
            {carga.fotos.length} de {carga.maximo} fotos
          </p>
        )}
      </div>

      {/* Los controles van ARRIBA de las fotos: con seis miniaturas, en un teléfono
          «Tomar foto» quedaría tres filas más abajo cada vez. */}
      <div>
        {carga.estado === 'lista' && (
          <ControlesDeSubida
            subiendo={avance !== null}
            lleno={carga.fotos.length >= carga.maximo}
            maximo={carga.maximo}
            onElegir={subir}
          />
        )}
        {/* Fuera del bloque de la lista y SIEMPRE montada: si la recarga posterior a una
            subida falla, el operador igual lee qué alcanzó a publicarse; y una región
            "polite" que nace ya con texto no se anuncia de forma fiable. El margen va en
            el párrafo de adentro para que, vacía, no ocupe lugar. */}
        <div role="status" className="text-xs">
          {avance ? (
            <p className="mt-3 flex items-center gap-1.5" style={{ color: 'var(--text-primary)' }}>
              <Loader2 className="w-3.5 h-3.5 animate-spin" aria-hidden="true" />
              {avance.total === 1 ? 'Subiendo la foto…' : `Subiendo ${avance.actual} de ${avance.total}…`}
            </p>
          ) : hecho ? (
            <p className={`mt-3 flex items-start gap-1.5 ${TONO_OK}`}>
              <CheckCircle2 className="w-3.5 h-3.5 mt-0.5 shrink-0" aria-hidden="true" /> <span>{hecho}</span>
            </p>
          ) : null}
        </div>
        {rechazo && (
          <p key={intento} role="alert" className={`mt-3 flex items-start gap-1.5 text-sm ${TONO_ERROR}`}>
            <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" aria-hidden="true" />
            <span>
              {rechazo.mensaje}
              {rechazo.contexto && <span className="block text-xs mt-0.5">{rechazo.contexto}</span>}
            </span>
          </p>
        )}
      </div>

      {carga.estado === 'cargando' && (
        <div role="status" className="flex items-center justify-center gap-2 py-12 text-sm" style={{ color: 'var(--text-muted)' }}>
          <Loader2 className="w-5 h-5 animate-spin" aria-hidden="true" /> Cargando las fotos…
        </div>
      )}

      {carga.estado === 'error' && (
        <div role="alert" className="flex flex-col items-center gap-3 py-10 text-center">
          <p className={`flex items-start gap-2 text-sm ${TONO_ERROR}`}>
            <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" aria-hidden="true" /> <span>{carga.mensaje}</span>
          </p>
          {carga.reintentable && (
            <button type="button" onClick={() => { void cargar(true) }} className={botonSecundarioCls}>
              <RotateCcw className="w-4 h-4" aria-hidden="true" /> Reintentar
            </button>
          )}
        </div>
      )}

      {carga.estado === 'lista' && carga.fotos.length === 0 && (
        <div className="flex flex-col items-center gap-2 py-8 text-center">
          <ImageOff className="w-6 h-6" style={{ color: 'var(--text-faint)' }} aria-hidden="true" />
          <p className="text-sm" style={{ color: 'var(--text-muted)' }}>Este ítem aún no tiene fotos para el cliente</p>
        </div>
      )}

      {carga.estado === 'lista' && carga.fotos.length > 0 && (
        <ul className="grid grid-cols-2 sm:grid-cols-3 gap-3">
          {carga.fotos.map((foto, i) => (
            <TarjetaDeFotoPublicada
              key={foto.id}
              foto={foto}
              rotulo={`foto ${i + 1} de ${carga.fotos.length} de ${numeroParte || 'el ítem'}`}
              bloqueada={avance !== null}
              onQuitada={alQuitar}
            />
          ))}
        </ul>
      )}
    </section>
  )
}
