// Piezas de presentación que comparten TODAS las pantallas del cliente. No piden datos ni
// conocen rutas del backend: reciben props y pintan. Sus estilos son las clases portal-*
// de portal.css (el color, el tema claro/oscuro y el contraste se deciden allá).
//
// DECISIONES QUE NO SE VEN EN EL CÓDIGO
//   · Un estado se dice con PALABRA + FORMA (✓ ● ○ !). El color es un refuerzo: a pleno
//     sol o impreso en blanco y negro la palabra y la forma siguen ahí.
//   · La palabra de un estado es `estado.etiqueta` (la escribe el backend). Acá solo se
//     elige el tono, con estadosUi.ts::tonoDeEstado.
//   · «Atrasado» lo decide el backend (`vencida`); la pieza solo cuenta los días contra
//     el `hoy` del backend, nunca contra el reloj del dispositivo.
//   · Un monto null es «—» con el texto «Sin monto» para lectores de pantalla. No es $0.
//   · ErrorDeCarga no muestra el `detail` del backend: un 503 trae instrucciones para el
//     equipo técnico. El mensaje para el cliente es fijo.
import { useEffect, useId, useState, type ElementType, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import {
  AlertCircle, AlertTriangle, ArrowLeft, Check, ChevronRight, Circle, Copy, Info, Loader2,
  RefreshCw, SearchX, X,
} from 'lucide-react'
import type { EstadoCliente, MontoLinea, OrdenResumen, Sello } from './types'
import { detalleDe, estadoHttp } from './api'
import { tonoDeEstado, type Tono } from './estadosUi'
import { contar, fmtActualizado, fmtAtraso, fmtFechaCorta, fmtMonto, fmtPlazo } from './formato'

// Nivel del título de una pieza que puede ser la pantalla entera o un bloque dentro de
// otra. Cada pantalla tiene UN solo <h1>: quien la arma elige.
type NivelDeTitulo = 1 | 2

// ─── Estados de carga ─────────────────────────────────────────────────────────
export function Cargando({ texto = 'Cargando…' }: { texto?: string }) {
  // La región role="status" se monta VACÍA y el texto entra un instante después: una
  // región que nace ya con su texto no se anuncia de forma fiable. A la vista el texto
  // está desde el principio (el span aria-hidden).
  const [anuncio, setAnuncio] = useState('')
  useEffect(() => {
    const reloj = window.setTimeout(() => setAnuncio(texto), 150)
    return () => window.clearTimeout(reloj)
  }, [texto])
  return (
    <p className="portal-cargando">
      <Loader2 className="portal-giro" aria-hidden="true" />
      <span aria-hidden="true">{texto}</span>
      <span role="status" className="sr-only">{anuncio}</span>
    </p>
  )
}

/** Sin conexión: react-query deja la consulta en pausa y la retoma sola cuando vuelve la señal. */
export function SinConexion({ texto }: { texto: string }) {
  return <Aviso tono="atencion" anunciar>Sin conexión. {texto}</Aviso>
}

// Respuestas cuyo `detail` el backend escribe PARA EL CLIENTE: 429 (demasiadas
// solicitudes seguidas, backend/portal/cupo.py). El 403 lo tratan las pantallas aparte
// (es un bloqueo de la cuenta, no un error de carga); el resto (500, 503) puede traer texto
// para el equipo técnico y no se muestra.
const ESTADOS_CON_MENSAJE_PARA_EL_CLIENTE = [429]

interface ErrorDeCargaProps {
  titulo: string
  onReintentar: () => void
  /** El error de la consulta: si trae un texto para el cliente, se muestra en vez del genérico. */
  error?: unknown
  /** 2 por defecto (un bloque dentro de una pantalla que ya tiene su <h1>). */
  nivel?: NivelDeTitulo
}

/** Red caída o error del servidor: mensaje propio y botón Reintentar. */
export function ErrorDeCarga({ titulo, onReintentar, error, nivel = 2 }: ErrorDeCargaProps) {
  const id = useId()
  const Titulo = nivel === 1 ? 'h1' : 'h2'
  const estado = estadoHttp(error)
  const detalle = estado !== undefined && ESTADOS_CON_MENSAJE_PARA_EL_CLIENTE.includes(estado) ? detalleDe(error) : null
  return (
    <section className="portal-tarjeta portal-entrada p-5 sm:p-7 max-w-2xl" aria-labelledby={id}>
      <Titulo id={id} className="text-xl font-bold tracking-tight mb-4">{titulo}</Titulo>
      <Aviso tono="error">
        {detalle ?? 'Revisa tu conexión e intenta de nuevo. Si el problema sigue, avisa a tu ejecutivo de MachParts.'}
      </Aviso>
      <button
        type="button"
        onClick={() => {
          onReintentar()
          // Si la recarga sale bien, esta tarjeta (y con ella el botón) se desmonta y el
          // foco caería al <body>: se lleva al contenido, como hace «Limpiar búsqueda».
          document.getElementById('portal-contenido')?.focus({ preventScroll: true })
        }}
        className="portal-boton mt-5 w-full sm:w-auto"
      >
        <RefreshCw className="w-4 h-4" aria-hidden="true" />
        Reintentar
      </button>
    </section>
  )
}

interface NoEncontradoProps {
  titulo: string
  volverA: string
  volverTexto: string
}

/**
 * Respuesta a un 404. Para el portal «no existe» y «no es tuyo» son lo mismo (el backend
 * no distingue, a propósito): acá tampoco se distingue ni se dan detalles técnicos. Es
 * la pantalla entera, así que su título es el <h1>.
 */
export function NoEncontrado({ titulo, volverA, volverTexto }: NoEncontradoProps) {
  const id = useId()
  return (
    <section className="portal-tarjeta portal-entrada p-5 sm:p-7 max-w-2xl" aria-labelledby={id}>
      <SearchX className="w-8 h-8 portal-suave" aria-hidden="true" />
      <h1 id={id} className="mt-3 text-xl font-bold tracking-tight">{titulo}</h1>
      <p className="portal-suave mt-2">
        Puede que el enlace esté incompleto o que ya no esté disponible para tu cuenta. Si crees
        que es un error, avisa a tu ejecutivo de MachParts.
      </p>
      <Link to={volverA} className="portal-boton mt-5 w-full sm:w-auto">
        <ArrowLeft className="w-4 h-4" aria-hidden="true" />
        {volverTexto}
      </Link>
    </section>
  )
}

// ─── Avisos ───────────────────────────────────────────────────────────────────
type TonoDeAviso = 'info' | 'atencion' | 'error'

const ICONO_DE_AVISO: Record<TonoDeAviso, ElementType> = {
  info: Info,
  atencion: AlertTriangle,
  error: AlertCircle,
}

/** Recuadro de aviso. Solo el de error interrumpe al lector de pantalla (role="alert"). */
interface AvisoProps {
  tono: TonoDeAviso
  /** El aviso se lee en voz alta al aparecer (role="alert"). Un error siempre; un aviso
      de atención solo cuando reemplaza o corrige lo que la persona está viendo (sin
      conexión, datos desactualizados): sin esto, con lector de pantalla es invisible. */
  anunciar?: boolean
  children: ReactNode
}

export function Aviso({ tono, anunciar = false, children }: AvisoProps) {
  const Icono = ICONO_DE_AVISO[tono]
  return (
    <div className="portal-aviso" data-tono={tono} role={tono === 'error' || anunciar ? 'alert' : undefined}>
      <Icono aria-hidden="true" />
      <div className="min-w-0">{children}</div>
    </div>
  )
}

// ─── Estado de un ítem ────────────────────────────────────────────────────────
const FORMA_DE_TONO: Record<Tono, ElementType> = {
  ok: Check,
  curso: Circle, // portal.css la rellena: ●
  pendiente: Circle, // sin relleno: ○
  atencion: AlertTriangle,
}

/** La forma que acompaña a un tono. Decorativa: la palabra va siempre al lado. */
export function FormaDeTono({ tono }: { tono: Tono }) {
  const Forma = FORMA_DE_TONO[tono]
  return <Forma className="portal-forma" data-tono={tono} aria-hidden="true" />
}

export function InsigniaEstado({ estado }: { estado: EstadoCliente }) {
  const tono = tonoDeEstado(estado.clave)
  return (
    <span className="portal-insignia" data-tono={tono}>
      <FormaDeTono tono={tono} />
      {estado.etiqueta}
    </span>
  )
}

interface FechaComprometidaProps {
  fecha: string | null
  vencida: boolean
  /** Todavía se espera algo de este ítem (`cantidad_por_entregar > 0`). */
  seEspera: boolean
  hoy: string
}

/**
 * La fecha comprometida de un ítem y, si todavía se espera algo de él, cuánto falta o
 * cuánto lleva de atraso: «Comprometido 12 ago · en 3 días» / «… · atrasado 2 días». Si
 * ya no se espera nada (entregado, o lo que resta es «no recibido en la entrega»): solo
 * la fecha, porque no hay plazo que contar.
 */
export function FechaComprometida({ fecha, vencida, seEspera, hoy }: FechaComprometidaProps) {
  if (!fecha) return <span className="portal-fecha portal-suave">Sin fecha comprometida</span>
  const dia = <time dateTime={fecha}>{fmtFechaCorta(fecha, hoy)}</time>
  if (!seEspera) return <span className="portal-fecha">Comprometido {dia}</span>
  if (vencida) {
    return (
      <span className="portal-fecha">
        Comprometido {dia}{' · '}
        <strong className="portal-atraso">
          <AlertTriangle aria-hidden="true" />
          {fmtAtraso(fecha, hoy)}
        </strong>
      </span>
    )
  }
  const plazo = fmtPlazo(fecha, hoy)
  return (
    <span className="portal-fecha">
      Comprometido {dia}
      {plazo && <>{' · '}<span className="portal-plazo">{plazo}</span></>}
    </span>
  )
}

// ─── Cifras y códigos ─────────────────────────────────────────────────────────
interface MontoProps {
  valor: number | null
  /** Para el precio unitario: muestra los centavos si los hay. */
  decimales?: boolean
  className?: string
}

export function Monto({ valor, decimales, className }: MontoProps) {
  const clases = className ? `portal-cifra ${className}` : 'portal-cifra'
  if (valor === null) {
    return (
      <span className={clases}>
        <span aria-hidden="true">—</span>
        <span className="sr-only">Sin monto</span>
      </span>
    )
  }
  return <span className={clases}>{fmtMonto(valor, { decimales })}</span>
}

/**
 * El precio unitario de un ítem. Con monto pero sin unitario, sus unidades tienen más de
 * un precio (tandas facturadas a precios distintos; ver types.ts::MontoLinea): se dice
 * con palabras, porque un guion se leería como «sin precio».
 */
export function PrecioUnitario({ monto }: { monto: MontoLinea | null }) {
  if (monto && monto.unitario_clp === null) return <span className="portal-suave">Varios precios</span>
  return <Monto valor={monto?.unitario_clp ?? null} decimales />
}

const AVISO_DE_COPIA_MS = 2000

/**
 * El N° de parte tal cual llega (sin mayúsculas forzadas ni cortes de línea). Con
 * `conCopiar` (lo normal) lleva un botón para copiarlo: el comprador lo pega en su
 * sistema, el bodeguero en un mensaje. Sin él, cuando va dentro de un <h1> o de un
 * <th>: ahí el botón se colaría en el nombre del encabezado.
 */
export function NumeroDeParte({ valor, conCopiar = true }: { valor: string; conCopiar?: boolean }) {
  if (!valor) return <span className="portal-suave">Sin N° de parte</span>
  if (!conCopiar) return <span className="portal-parte">{valor}</span>
  return <NumeroDeParteConCopia valor={valor} />
}

function NumeroDeParteConCopia({ valor }: { valor: string }) {
  return (
    <span className="portal-parte-grupo">
      <span className="portal-parte">{valor}</span>
      <CopiarNumeroDeParte valor={valor} />
    </span>
  )
}

/**
 * El botón de copiar, solo. Para ponerlo JUNTO a un N° de parte que vive dentro de un
 * <h1>, un <h3> o un <th>: adentro se colaría en el nombre del encabezado.
 */
export function CopiarNumeroDeParte({ valor }: { valor: string }) {
  const [resultado, setResultado] = useState<'copiado' | 'fallo' | null>(null)

  useEffect(() => {
    if (!resultado) return
    const reloj = window.setTimeout(() => setResultado(null), AVISO_DE_COPIA_MS)
    return () => window.clearTimeout(reloj)
  }, [resultado])

  const copiar = async () => {
    try {
      // Sin HTTPS, o en un navegador antiguo, `navigator.clipboard` no existe: cae al catch.
      await navigator.clipboard.writeText(valor)
      setResultado('copiado')
    } catch {
      setResultado('fallo')
    }
  }
  const Icono = resultado === 'copiado' ? Check : resultado === 'fallo' ? X : Copy

  return (
    <>
      <button type="button" className="portal-copiar" onClick={copiar} aria-label={`Copiar N° de parte ${valor}`}>
        <Icono aria-hidden="true" />
      </button>
      {/* Siempre montada: una región que nace ya con el texto no se anuncia. */}
      <span role="status" className="sr-only">
        {resultado === 'copiado' && 'Copiado'}
        {resultado === 'fallo' && 'No se pudo copiar'}
      </span>
    </>
  )
}

// ─── Orientación ──────────────────────────────────────────────────────────────
/** Miga de pan. El último paso es la pantalla actual: va sin enlace aunque traiga `a`. */
export function Migas({ pasos }: { pasos: { texto: string; a?: string }[] }) {
  return (
    <nav aria-label="Ruta">
      <ol className="portal-migas">
        {pasos.map((paso, i) => {
          const esUltimo = i === pasos.length - 1
          return (
            <li key={`${i}-${paso.texto}`}>
              {i > 0 && <ChevronRight className="portal-migas-separador" aria-hidden="true" />}
              {paso.a && !esUltimo
                ? <Link to={paso.a} className="portal-enlace">{paso.texto}</Link>
                : <span aria-current={esUltimo ? 'page' : undefined}>{paso.texto}</span>}
            </li>
          )
        })}
      </ol>
    </nav>
  )
}

const ESTADO_DE_ORDEN: Record<OrdenResumen['estado'], { texto: string; tono: Tono }> = {
  abierta: { texto: 'Abierta', tono: 'curso' },
  entregada: { texto: 'Entregada', tono: 'ok' },
}

/**
 * «Abierta» / «Entregada», palabra + forma. De una ORDEN el backend manda solo la clave
 * (OrdenResumen.estado), sin etiqueta; las de los ÍTEMS sí vienen del backend. Una clave
 * que esta pantalla no conoce se muestra como abierta: no promete una entrega.
 */
export function InsigniaDeOrden({ estado }: { estado: OrdenResumen['estado'] }) {
  const { texto, tono } = ESTADO_DE_ORDEN[estado] ?? ESTADO_DE_ORDEN.abierta
  return (
    <span className="portal-insignia" data-tono={tono}>
      <FormaDeTono tono={tono} />
      {texto}
    </span>
  )
}

/** «Recargar» falló pero los datos de la carga anterior siguen ahí: se avisa, no se borran. */
export function AvisoDeRecargaFallida({ motivo }: { motivo: string | null }) {
  return (
    <Aviso tono="atencion" anunciar>
      {motivo ?? 'No pudimos actualizar: estás viendo los datos de la última carga. Revisa tu conexión y toca «Recargar».'}
    </Aviso>
  )
}

interface SelectorDeAnioProps {
  /** Años en que el cliente tiene pedidos, del más reciente al más antiguo. */
  anios: readonly number[]
  anio: number | null
  onElegir: (anio: number | null) => void
  /** Con menos opciones que esto el selector no se muestra (salvo que la URL ya traiga un año). */
  minimo?: number
  className: string
}

/**
 * <select> y no botones: con muchos años sigue ocupando una fila en un teléfono. El año
 * de la URL entra en la lista aunque el backend no lo nombre (un enlace guardado a un año
 * sin pedidos, o la lista todavía no llega): sin esa opción el <select> diría «Todos los
 * años» sobre los números de ese año, y no se podría quitar desde acá.
 */
export function SelectorDeAnio({ anios, anio, onElegir, minimo = 1, className }: SelectorDeAnioProps) {
  const id = useId()
  const opciones = anio !== null && !anios.includes(anio) ? [anio, ...anios].sort((a, b) => b - a) : anios
  if (anio === null && opciones.length < minimo) return null
  return (
    <div className={className}>
      <label htmlFor={id} className="portal-etiqueta">Año</label>
      <select
        id={id}
        className="portal-campo"
        value={anio ?? ''}
        onChange={(e) => onElegir(e.target.value ? Number(e.target.value) : null)}
      >
        <option value="">Todos los años</option>
        {opciones.map((a) => <option key={a} value={a}>{a}</option>)}
      </select>
    </div>
  )
}

interface ActualizadoProps {
  sello: Sello
  onRecargar: () => void
  cargando: boolean
}

/**
 * «Actualizado a las 14:32» y el botón Recargar. Sin esto, ante la duda de si lo que ve
 * está al día, el cliente llama por teléfono.
 */
export function Actualizado({ sello, onRecargar, cargando }: ActualizadoProps) {
  return (
    <div className="portal-actualizado">
      <span role="status">{fmtActualizado(sello.actualizado_at)}</span>
      {/* aria-disabled y no disabled: un botón deshabilitado suelta el foco del teclado
          justo mientras recarga. */}
      <button
        type="button"
        className="portal-boton portal-boton-secundario portal-boton-compacto"
        aria-disabled={cargando}
        onClick={() => { if (!cargando) onRecargar() }}
      >
        <RefreshCw className={cargando ? 'w-4 h-4 portal-giro' : 'w-4 h-4'} aria-hidden="true" />
        {cargando ? 'Recargando…' : 'Recargar'}
      </button>
    </div>
  )
}

/**
 * Avance de 0 a 100. Lo que informa es el TEXTO (la etiqueta y el porcentaje, visibles);
 * la barra es un dibujo de apoyo y por eso va oculta para lectores de pantalla.
 */
export function BarraDeAvance({ pct, etiqueta }: { pct: number; etiqueta: string }) {
  const porcentaje = Number.isFinite(pct) ? Math.min(100, Math.max(0, Math.round(pct))) : 0
  return (
    <div className="portal-avance">
      <div className="portal-avance-texto">
        <span>{etiqueta}</span>
        <span className="portal-cifra">{porcentaje} %</span>
      </div>
      <div className="portal-avance-riel" aria-hidden="true">
        <div
          className="portal-avance-relleno"
          data-completa={porcentaje === 100}
          style={{ transform: `scaleX(${porcentaje / 100})` }}
        />
      </div>
    </div>
  )
}
