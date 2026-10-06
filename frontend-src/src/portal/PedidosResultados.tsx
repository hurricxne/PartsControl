// Los resultados de «Mis pedidos», dibujados DOS veces con los mismos datos: como tabla
// (pantalla ancha: el comprador cuadra columnas contra su sistema) y como tarjetas
// (teléfono y tablet: se lee una orden a la vez, lo urgente arriba). portal.css muestra
// una sola (portal-pedidos-solo-tabla / portal-pedidos-solo-tarjetas); la otra sale
// también del árbol de accesibilidad. No pide datos: recibe la página de órdenes que
// cargó PedidosPage (OrdenesResp.items) y el `hoy` de esa misma respuesta.
//
// DECISIONES QUE NO SE VEN EN EL CÓDIGO
//   · El corte tabla/tarjetas es a 960 px (y no a los 640 px de la cabecera): las siete
//     columnas no caben antes sin barra horizontal. Bajo ese ancho mandan las tarjetas.
//   · Cada orden navega con UN enlace real, su N° de orden. En la tarjeta ese enlace se
//     estira sobre toda la tarjeta (portal-ancla-tarjeta); en la tabla no hay <tr onClick>.
//   · «Atrasado» no se decide mirando el reloj. `orden.atrasados` (lo cuenta el backend)
//     dice si hay ítems vencidos; como `proxima_fecha` es la fecha más antigua entre lo
//     que se espera, cuando hay atrasados esa fecha es la del ítem más atrasado. Acá solo se
//     cuentan los días contra el `hoy` del backend.
//   · La columna «Total neto» existe solo si la cuenta ve montos. Una orden sin precio
//     congelado trae `totales: null` y se pinta «—» (piezas.tsx::Monto), nunca $0.
//   · `coincidencias` viene solo cuando se buscó algo, y puede venir vacío: la orden calzó
//     por su propio N°, que ya está a la vista.
import { Link } from 'react-router-dom'
import { AlertTriangle, ChevronRight, FileText, Package, Truck, type LucideIcon } from 'lucide-react'
import type { Coincidencia, OrdenResumen } from './types'
import { contar, fmtAtraso, fmtCantidad, fmtFecha, fmtFechaCorta, fmtPlazo } from './formato'
import { BarraDeAvance, InsigniaDeOrden, Monto } from './piezas'
import { rutaDeItem, rutaDeOrden } from './rutas'

interface PedidosResultadosProps {
  ordenes: OrdenResumen[]
  /** Sello.hoy de la respuesta: la base de «en 3 días» y «atrasado 2 días». */
  hoy: string
  veMontos: boolean
}

export default function PedidosResultados({ ordenes, hoy, veMontos }: PedidosResultadosProps) {
  // Para el colSpan de la fila de coincidencias: seis columnas fijas más la del monto.
  const columnas = veMontos ? 7 : 6
  return (
    <>
      <div className="portal-tarjeta portal-tabla-marco portal-pedidos-solo-tabla">
        <table className="portal-tabla portal-pedidos-tabla">
          <caption className="sr-only">Pedidos: los abiertos primero y, entre ellos, el más reciente arriba</caption>
          <thead>
            <tr>
              <th scope="col">N° de orden</th>
              <th scope="col">Fecha de la OC</th>
              <th scope="col">Estado</th>
              <th scope="col">Avance</th>
              <th scope="col" className="portal-pedidos-titulo-largo">Fecha comprometida</th>
              <th scope="col" className="portal-cifra">Atrasados</th>
              {veMontos && <th scope="col" className="portal-cifra">Total neto</th>}
            </tr>
          </thead>
          {/* Un <tbody> por orden: agrupa su fila con la de «Coincide con». */}
          {ordenes.map((orden) => (
            <FilasDeOrden key={orden.id} orden={orden} hoy={hoy} veMontos={veMontos} columnas={columnas} />
          ))}
        </table>
      </div>

      <ul className="portal-pedidos-tarjetas portal-pedidos-solo-tarjetas">
        {ordenes.map((orden) => (
          <li key={orden.id}>
            <TarjetaDeOrden orden={orden} hoy={hoy} veMontos={veMontos} />
          </li>
        ))}
      </ul>
    </>
  )
}

// ─── Tabla ────────────────────────────────────────────────────────────────────
interface FilasDeOrdenProps {
  orden: OrdenResumen
  hoy: string
  veMontos: boolean
  columnas: number
}

function FilasDeOrden({ orden, hoy, veMontos, columnas }: FilasDeOrdenProps) {
  const coincidencias = orden.coincidencias ?? []
  return (
    <tbody>
      <tr data-atrasada={orden.atrasados > 0} data-coincide={coincidencias.length > 0}>
        <th scope="row">
          <Link to={rutaDeOrden(orden.id)} className="portal-enlace portal-pedidos-numero">
            <NumeroDeOrden orden={orden} />
          </Link>
          {orden.empresa && <span className="portal-pedidos-empresa">{orden.empresa}</span>}
        </th>
        <td className="portal-pedidos-entera">
          {orden.fecha_oc
            ? <time dateTime={orden.fecha_oc}>{fmtFecha(orden.fecha_oc)}</time>
            : <SinDato texto="Sin fecha" />}
        </td>
        <td><InsigniaDeOrden estado={orden.estado} /></td>
        <td className="portal-pedidos-celda-avance"><Avance orden={orden} /></td>
        <td><ProximaFecha orden={orden} hoy={hoy} /></td>
        <td className="portal-cifra">
          <span className={orden.atrasados > 0 ? 'portal-pedidos-atrasados' : 'portal-suave'}>
            {fmtCantidad(orden.atrasados)}
          </span>
        </td>
        {veMontos && (
          <td className="portal-cifra"><Monto valor={orden.totales?.neto_clp ?? null} /></td>
        )}
      </tr>
      {coincidencias.length > 0 && (
        <tr className="portal-pedidos-fila-coincide">
          <td colSpan={columnas}>
            <Coincidencias ordenId={orden.id} coincidencias={coincidencias} />
          </td>
        </tr>
      )}
    </tbody>
  )
}

// ─── Tarjeta ──────────────────────────────────────────────────────────────────
function TarjetaDeOrden({ orden, hoy, veMontos }: Omit<FilasDeOrdenProps, 'columnas'>) {
  const coincidencias = orden.coincidencias ?? []
  return (
    <article className="portal-tarjeta portal-tarjeta-enlace portal-pedidos-tarjeta" data-atrasada={orden.atrasados > 0}>
      <div className="portal-pedidos-tarjeta-cabeza">
        <div className="min-w-0">
          {/* Oculto al lector de pantalla: el enlace ya dice «Pedido 4500123». */}
          <p className="portal-versalita" aria-hidden="true">N° de orden</p>
          <h3 className="portal-pedidos-numero">
            <Link to={rutaDeOrden(orden.id)} className="portal-ancla-tarjeta">
              <NumeroDeOrden orden={orden} />
            </Link>
          </h3>
        </div>
        <ChevronRight className="portal-pedidos-flecha" aria-hidden="true" />
      </div>
      <div className="portal-pedidos-tarjeta-estado">
        <InsigniaDeOrden estado={orden.estado} />
        {orden.empresa && <span className="portal-pedidos-empresa">{orden.empresa}</span>}
      </div>

      <dl className="portal-pedidos-datos">
        <div className="portal-pedidos-dato" data-ancho="completo">
          <dt className="portal-versalita">Fecha comprometida</dt>
          <dd>
            <ProximaFecha orden={orden} hoy={hoy} />
            {orden.atrasados > 0 && (
              <p className="portal-pedidos-atrasados">{contar(orden.atrasados, 'ítem atrasado', 'ítems atrasados')}</p>
            )}
          </dd>
        </div>
        <div className="portal-pedidos-dato" data-ancho="completo">
          <dt className="portal-versalita">Avance</dt>
          <dd><Avance orden={orden} /></dd>
        </div>
        <div className="portal-pedidos-dato">
          <dt className="portal-versalita">Fecha de la OC</dt>
          <dd>
            {orden.fecha_oc
              ? <time dateTime={orden.fecha_oc} className="portal-pedidos-entera">{fmtFecha(orden.fecha_oc)}</time>
              : <SinDato texto="Sin fecha" />}
          </dd>
        </div>
        {veMontos && (
          <div className="portal-pedidos-dato">
            <dt className="portal-versalita">Total neto</dt>
            <dd><Monto valor={orden.totales?.neto_clp ?? null} /></dd>
          </div>
        )}
      </dl>

      {coincidencias.length > 0 && <Coincidencias ordenId={orden.id} coincidencias={coincidencias} />}
    </article>
  )
}

// ─── Lo que comparten la tabla y la tarjeta ───────────────────────────────────
/** El texto del enlace de una orden. «Pedido» va solo para el lector de pantalla. */
function NumeroDeOrden({ orden }: { orden: OrdenResumen }) {
  return (
    <>
      <span className="sr-only">Pedido </span>
      {/* Una orden sin N° no puede dejar un enlace sin texto: no habría dónde tocar. */}
      {orden.numero_oc || 'Sin N° de orden'}
    </>
  )
}

function SinDato({ texto }: { texto: string }) {
  return (
    <span className="portal-suave">
      <span aria-hidden="true">—</span>
      <span className="sr-only">{texto}</span>
    </span>
  )
}

/** «3 de 14 ítems» y la barra. El porcentaje llega calculado (pondera entregas parciales). */
function Avance({ orden }: { orden: OrdenResumen }) {
  const { total, entregados } = orden.items
  if (total === 0) return <span className="portal-suave">Sin ítems</span>
  return (
    <BarraDeAvance
      pct={orden.avance_pct}
      etiqueta={`${fmtCantidad(entregados)} de ${contar(total, 'ítem', 'ítems')}`}
    />
  )
}

/**
 * La fecha comprometida más antigua entre lo que se espera, con su plazo o su atraso
 * (si hay ítems atrasados, es la del más atrasado). No se usa piezas.tsx::
 * FechaComprometida porque esa habla de UN ítem y antepone «Comprometido», que acá ya
 * dice el título de la columna.
 */
function ProximaFecha({ orden, hoy }: { orden: OrdenResumen; hoy: string }) {
  const fecha = orden.proxima_fecha
  if (!fecha) {
    // Abierta y sin nada por entregar: le quedan unidades que el cliente firmó como no
    // recibidas (no se reponen por este pedido). No es «sin fecha comprometida».
    const soloNoRecibido = orden.estado !== 'entregada' && orden.items.total > 0 && orden.items.por_entregar === 0
    return (
      <span className="portal-suave">
        {orden.estado === 'entregada' && 'Sin entregas pendientes'}
        {soloNoRecibido && 'Unidades no recibidas en la entrega'}
        {orden.estado !== 'entregada' && !soloNoRecibido && 'Sin fecha comprometida'}
      </span>
    )
  }
  const dia = <time dateTime={fecha} className="portal-pedidos-dia">{fmtFechaCorta(fecha, hoy)}</time>
  if (orden.atrasados > 0) {
    return (
      <span className="portal-fecha portal-pedidos-proxima">
        {dia}{' '}
        <strong className="portal-atraso">
          <AlertTriangle aria-hidden="true" />
          {fmtAtraso(fecha, hoy)}
        </strong>
      </span>
    )
  }
  const plazo = fmtPlazo(fecha, hoy)
  return (
    <span className="portal-fecha portal-pedidos-proxima">
      {dia}
      {plazo && <>{' '}<span className="portal-plazo">{plazo}</span></>}
    </span>
  )
}

// ─── Por qué salió la orden en la búsqueda ────────────────────────────────────
interface CoincidenciasProps {
  ordenId: number
  coincidencias: Coincidencia[]
}

function Coincidencias({ ordenId, coincidencias }: CoincidenciasProps) {
  return (
    <div className="portal-pedidos-coincide">
      <p className="portal-versalita">Coincide con:</p>
      <ul className="portal-pedidos-coincide-lista">
        {coincidencias.map((c) => (
          <li key={`${c.tipo}-${c.id}`}>
            <FichaDeCoincidencia ordenId={ordenId} coincidencia={c} />
          </li>
        ))}
      </ul>
    </div>
  )
}

interface ClaseDeDocumento {
  nombre: string
  Icono: LucideIcon
}

const DOCUMENTO: Record<'guia' | 'factura', ClaseDeDocumento> = {
  guia: { nombre: 'Guía', Icono: Truck },
  factura: { nombre: 'Factura', Icono: FileText },
}
// Un tipo que esta pantalla no conoce (backend más nuevo): se nombra sin adivinar cuál es.
const DOCUMENTO_DESCONOCIDO: ClaseDeDocumento = { nombre: 'Documento', Icono: FileText }

/**
 * Un ítem lleva a su seguimiento; una guía o una factura, a la orden (ahí están sus
 * documentos). El N° de parte va tal cual y sin botón de copiar: un botón dentro de un
 * enlace no es HTML válido, y la ficha entera ya es el enlace.
 */
function FichaDeCoincidencia({ ordenId, coincidencia }: { ordenId: number; coincidencia: Coincidencia }) {
  if (coincidencia.tipo === 'item') {
    // Un ítem puede no tener N° de parte (calzó por su descripción), o ninguno de los dos.
    const sinTexto = !coincidencia.texto && !coincidencia.detalle
    return (
      <Link to={rutaDeItem(ordenId, coincidencia.id)} className="portal-pedidos-ficha">
        <Package aria-hidden="true" />
        {sinTexto ? 'Ver ítem' : <span className="sr-only">Ítem</span>}
        {coincidencia.texto && <>{' '}<span className="portal-parte">{coincidencia.texto}</span></>}
        {coincidencia.detalle && <>{' '}<span className="portal-pedidos-ficha-detalle">{coincidencia.detalle}</span></>}
      </Link>
    )
  }
  const { nombre, Icono } = DOCUMENTO[coincidencia.tipo] ?? DOCUMENTO_DESCONOCIDO
  return (
    <Link to={rutaDeOrden(ordenId)} className="portal-pedidos-ficha">
      <Icono aria-hidden="true" />
      <span>{nombre} <span className="portal-codigo">{coincidencia.texto}</span></span>
    </Link>
  )
}
