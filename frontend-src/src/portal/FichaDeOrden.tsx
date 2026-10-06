// Ficha de cabecera de una orden: lo primero que se lee en OrdenPage.tsx. Arriba, en
// grande, lo que el cliente vino a saber (cuándo llega lo próximo, cuánto falta, cuánto
// suma); abajo, en chico, los datos del documento (estado, fechas, ejecutivo).
//
// No pide nada: recibe la `orden` y los `items` de GET /api/portal/ordenes/{id}.
//
// DECISIONES QUE NO SE VEN EN EL CÓDIGO
//   · Ninguna fecha se compara con el reloj del dispositivo. «en 3 días» / «hace 2 días»
//     salen de formato.ts::fmtPlazo contra el `hoy` del backend, y cuántos ítems van
//     atrasados lo cuenta el backend (`orden.atrasados`).
//   · `proxima_fecha` es la fecha comprometida MÁS ANTIGUA entre lo que todavía se
//     espera: si ese ítem ya va atrasado, la fecha es pasada, el rótulo deja de llamarla
//     «próxima» (como en el inicio) y abajo se cuentan los atrasados. La fecha es la misma
//     que muestran Mis pedidos y el inicio para esta orden.
//   · Una orden puede seguir ABIERTA sin que se espere nada: le quedan unidades «no
//     recibidas en la entrega» (el cliente las declaró al firmar la guía y no se reponen
//     por este pedido). Ahí no hay fecha que mostrar y se dice eso, no «sin fecha».
//   · «Completa el …» solo se afirma cuando TODO lo que se espera tiene fecha comprometida.
//     Con ítems sin fecha, `fecha_completa` es apenas la última fecha conocida, y se
//     rotula así: no se le promete al cliente un cierre que nadie comprometió.
//   · La celda de montos no existe para el perfil 'bodega'. Para quien sí ve montos,
//     `totales` null significa «esta orden no tiene precio congelado» (ver
//     backend/portal/precios.py): se explica con palabras, nunca con $0.
import { useId, type ReactNode } from 'react'
import { AlertTriangle } from 'lucide-react'
import type { ItemDeOrden, OrdenDetalle, TotalesOrden } from './types'
import { contar, diasEntre, fmtCantidad, fmtFecha, fmtFechaCorta, fmtPlazo } from './formato'
import { BarraDeAvance, InsigniaDeOrden, Monto } from './piezas'

type Orden = OrdenDetalle['orden']

// Forma de un correo con la que se puede armar un `mailto:`. Si no la cumple se muestra
// como texto: con una coma habría dos destinatarios, con un «%0a» se podría colar otra
// cabecera, con un «?» el programa de correo recibiría un destinatario roto.
const PARECE_CORREO = /^[A-Za-z0-9._+'-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$/

interface FichaDeOrdenProps {
  orden: Orden
  items: ItemDeOrden[]
  hoy: string
  veMontos: boolean
  /** Decide cómo termina la nota de «sin precios»: si ya hay facturas que mirar abajo. */
  hayFacturas: boolean
}

export default function FichaDeOrden({ orden, items, hoy, veMontos, hayFacturas }: FichaDeOrdenProps) {
  const id = useId()
  const { ejecutivo } = orden
  const conEjecutivo = !!ejecutivo && (ejecutivo.nombre.trim() !== '' || ejecutivo.email.trim() !== '')

  return (
    <section className="portal-tarjeta portal-orden-ficha mt-5" aria-labelledby={id}>
      <h2 id={id} className="sr-only">Resumen de la orden</h2>

      <div className="portal-orden-celdas">
        <CeldaDeEntrega orden={orden} items={items} hoy={hoy} />
        <CeldaDeAvance orden={orden} />
        {veMontos && <CeldaDeMontos totales={orden.totales} hayFacturas={hayFacturas} />}
      </div>

      <dl className="portal-orden-celdas portal-orden-datos">
        <Dato rotulo="Estado"><InsigniaDeOrden estado={orden.estado} /></Dato>
        <Dato rotulo="Fecha de la OC"><Fecha iso={orden.fecha_oc} /></Dato>
        <Dato rotulo="Ingreso del pedido"><Fecha iso={orden.fecha_ingreso} /></Dato>
        {conEjecutivo && (
          <Dato rotulo="Tu ejecutivo" ancho>
            <Ejecutivo nombre={ejecutivo.nombre} email={ejecutivo.email} numeroOc={orden.numero_oc} />
          </Dato>
        )}
      </dl>
    </section>
  )
}

// ─── Celdas principales ───────────────────────────────────────────────────────
function CeldaDeEntrega({ orden, items, hoy }: { orden: Orden; items: ItemDeOrden[]; hoy: string }) {
  if (orden.estado === 'entregada') {
    return (
      <div className="portal-orden-celda">
        <p className="portal-versalita">Entrega</p>
        <p className="portal-cifra-grande mt-1.5">Completa</p>
        <p className="portal-suave mt-1.5 text-sm">No quedan ítems por entregar.</p>
      </div>
    )
  }

  if (orden.items.por_entregar === 0) {
    const noRecibidos = orden.items.total - orden.items.entregados
    return (
      <div className="portal-orden-celda">
        <p className="portal-versalita">Entrega</p>
        <p className="mt-1.5 text-lg font-bold">Sin entregas pendientes</p>
        {noRecibidos > 0 && (
          <p className="portal-suave mt-1.5 text-sm">
            {contar(noRecibidos, 'ítem tiene', 'ítems tienen')} unidades no recibidas en la entrega.
          </p>
        )}
      </div>
    )
  }

  const { proxima_fecha: proxima, fecha_completa: ultima } = orden
  const sinFecha = items.filter((it) => it.cantidad_por_entregar > 0 && !it.fecha_prometida).length
  // «Ya pasó» se decide contra el `hoy` del backend, nunca contra el reloj del dispositivo.
  const yaPaso = proxima !== null && diasEntre(hoy, proxima) < 0
  return (
    <div className="portal-orden-celda">
      <p className="portal-versalita">{yaPaso ? 'Compromiso pendiente más antiguo' : 'Próxima fecha comprometida'}</p>
      {proxima ? (
        <>
          <p className="portal-cifra-grande mt-1.5">
            <time dateTime={proxima}>{fmtFechaCorta(proxima, hoy)}</time>
          </p>
          <p className="portal-suave mt-1">{fmtPlazo(proxima, hoy)}</p>
        </>
      ) : (
        <p className="mt-1.5 text-lg font-bold">Sin fecha comprometida</p>
      )}
      {orden.atrasados > 0 && (
        <p className="mt-2">
          <strong className="portal-atraso">
            <AlertTriangle aria-hidden="true" />
            {contar(orden.atrasados, 'ítem atrasado', 'ítems atrasados')}
          </strong>
        </p>
      )}
      {ultima && ultima !== proxima && (
        <p className="portal-suave mt-2 text-sm">
          {sinFecha === 0 ? 'Completa el ' : 'Última fecha comprometida: '}
          <time dateTime={ultima}>{fmtFecha(ultima)}</time>
        </p>
      )}
      {proxima && sinFecha > 0 && (
        <p className="portal-suave mt-1 text-sm">
          {contar(sinFecha, 'ítem pendiente sin fecha comprometida', 'ítems pendientes sin fecha comprometida')}
        </p>
      )}
    </div>
  )
}

function CeldaDeAvance({ orden }: { orden: Orden }) {
  const { total, entregados, por_entregar: porEntregar } = orden.items
  return (
    <div className="portal-orden-celda">
      <p className="portal-versalita">Por entregar</p>
      <p className="mt-1.5">
        <span className="portal-cifra-grande">{fmtCantidad(porEntregar)}</span>{' '}
        <span className="portal-suave">de {contar(total, 'ítem', 'ítems')}</span>
      </p>
      <div className="mt-3">
        <BarraDeAvance
          pct={orden.avance_pct}
          etiqueta={`${fmtCantidad(entregados)} de ${contar(total, 'ítem entregado', 'ítems entregados')}`}
        />
      </div>
    </div>
  )
}

function CeldaDeMontos({ totales, hayFacturas }: { totales: TotalesOrden | null; hayFacturas: boolean }) {
  if (!totales) {
    return (
      <div className="portal-orden-celda">
        <p className="portal-versalita">Montos</p>
        <p className="portal-suave mt-1.5 text-sm">
          Este pedido no tiene precios en el portal;{' '}
          {hayFacturas ? 'sus facturas sí aparecen abajo.' : 'sus facturas aparecerán abajo cuando se emitan.'}
        </p>
      </div>
    )
  }
  return (
    <div className="portal-orden-celda">
      <p className="portal-versalita">Total con IVA</p>
      <p className="mt-1.5"><Monto valor={totales.total_clp} className="portal-orden-total" /></p>
      <dl className="portal-orden-desglose">
        <div>
          <dt>Neto</dt>
          <dd><Monto valor={totales.neto_clp} /></dd>
        </div>
        <div>
          <dt>IVA 19 %</dt>
          <dd><Monto valor={totales.iva_clp} /></dd>
        </div>
      </dl>
    </div>
  )
}

// ─── Datos del documento ──────────────────────────────────────────────────────
/** Un par rótulo/valor de la fila de datos. `ancho`: la celda pide el doble de espacio. */
function Dato({ rotulo, ancho = false, children }: { rotulo: string; ancho?: boolean; children: ReactNode }) {
  return (
    <div className="portal-orden-celda" data-ancho={ancho}>
      <dt className="portal-versalita">{rotulo}</dt>
      <dd className="mt-1">{children}</dd>
    </div>
  )
}

function Fecha({ iso }: { iso: string | null }) {
  if (!iso) return <span className="portal-suave">Sin registrar</span>
  return <time dateTime={iso} className="font-semibold">{fmtFecha(iso)}</time>
}

function Ejecutivo({ nombre, email, numeroOc }: { nombre: string; email: string; numeroOc: string }) {
  const correo = email.trim()
  // El asunto ya dice de qué orden se trata: el ejecutivo no tiene que preguntarlo.
  const asunto = numeroOc.trim() ? `Consulta por la OC ${numeroOc.trim()}` : 'Consulta por un pedido'
  return (
    <>
      {nombre.trim() && <span className="block font-semibold">{nombre.trim()}</span>}
      {PARECE_CORREO.test(correo)
        ? (
          <a className="portal-enlace portal-orden-correo" href={`mailto:${correo}?subject=${encodeURIComponent(asunto)}`}>
            {correo}
          </a>
        )
        : correo && <span className="portal-suave">{correo}</span>}
    </>
  )
}
