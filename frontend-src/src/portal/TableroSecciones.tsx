// Las secciones del inicio (TableroPage.tsx): la ficha de avance, la fecha, dónde viene
// lo que falta, los montos y la pantalla sin pedidos. No piden datos: reciben el
// `Tablero` que cargó TableroPage. Las decisiones de diseño están en la cabecera de
// TableroPage.tsx.
import { useId } from 'react'
import { Link } from 'react-router-dom'
import { ArrowRight, PackageOpen } from 'lucide-react'
import type { Tablero } from './types'
import { contar, diasEntre, fmtCantidad, fmtFechaCorta, fmtPlazo } from './formato'
import { tonoDeEstado } from './estadosUi'
import { Aviso, FormaDeTono, InsigniaEstado, Monto } from './piezas'
import { rutaDePedidos } from './rutas'

// Una etapa con 1 ítem junto a otra con 300 quedaría con una barra de largo cero: se le
// deja un mínimo visible. La cifra exacta va siempre escrita al lado.
const LARGO_MINIMO_DE_BARRA = 0.02

type MontosDelTablero = NonNullable<Tablero['montos']>

export interface ResumenProps {
  datos: Tablero
  anio: number | undefined
  periodo: string
}

function textoDePedidos({ total, abiertas, entregadas }: Tablero['ordenes']): string {
  if (abiertas === 0) {
    return total === 1 ? 'Tu pedido ya está entregado.' : `Tus ${fmtCantidad(total)} pedidos ya están entregados.`
  }
  const deCuantos = `de ${contar(total, 'pedido', 'pedidos')}`
  return entregadas > 0 ? `${deCuantos} · ${contar(entregadas, 'entregado', 'entregados')}` : deCuantos
}

/**
 * Los cuatro datos del vistazo en UNA tarjeta partida por filetes, con jerarquía: los
 * pedidos abiertos mandan (cifra mayor y celda entera enlazada); el resto acompaña.
 */
export function FichaDeAvance({ datos, anio, periodo }: ResumenProps) {
  const id = useId()
  const { ordenes, items } = datos
  const hayAbiertos = ordenes.abiertas > 0
  const hayAtrasos = items.atrasados > 0
  return (
    <section className="portal-tarjeta portal-tablero-ficha" aria-labelledby={id}>
      <h2 id={id} className="sr-only">Avance de tus pedidos, {periodo}</h2>
      <dl className="portal-tablero-rejilla">
        {/* portal-tarjeta-enlace + portal-ancla-tarjeta (portal.css): toda la celda se
            puede tocar con un guante, y adentro hay un solo enlace real. */}
        <div className="portal-tablero-celda portal-tablero-principal portal-tarjeta-enlace">
          <dt className="portal-versalita">Pedidos abiertos</dt>
          <dd>
            <p className="portal-tablero-cifra-mayor">{fmtCantidad(ordenes.abiertas)}</p>
            <p className="portal-suave mt-2">{textoDePedidos(ordenes)}</p>
            <Link
              to={rutaDePedidos(anio, hayAbiertos ? 'abiertas' : undefined)}
              className="portal-enlace portal-ancla-tarjeta portal-tablero-accion"
            >
              {hayAbiertos ? 'Ver pedidos abiertos' : 'Ver mis pedidos'}
              <ArrowRight className="w-4 h-4" aria-hidden="true" />
            </Link>
          </dd>
        </div>

        <div className="portal-tablero-celda portal-tablero-pendientes">
          <dt className="portal-versalita">Ítems pendientes</dt>
          <dd>
            <p className="portal-tablero-dato">
              <span className="portal-cifra-grande">{fmtCantidad(items.pendientes)}</span>
              <span className="portal-tablero-unidad">de {contar(items.total, 'ítem', 'ítems')}</span>
            </p>
            <p className="portal-suave mt-1 text-sm">
              {items.entregados > 0
                ? contar(items.entregados, 'ya despachado', 'ya despachados')
                : 'Ninguno despachado todavía'}
            </p>
          </dd>
        </div>

        {/* El tono no es el único aviso: lo dicen la palabra «atrasados» y el triángulo. */}
        <div className="portal-tablero-celda portal-tablero-atrasos" data-tono={hayAtrasos ? 'atencion' : 'ok'}>
          <dt className="portal-versalita">Atrasos</dt>
          <dd>
            {hayAtrasos ? (
              <>
                <p className="portal-tablero-dato">
                  <FormaDeTono tono="atencion" />
                  <span className="portal-cifra-grande">{fmtCantidad(items.atrasados)}</span>
                  <span className="portal-tablero-unidad">
                    {items.atrasados === 1 ? 'ítem atrasado' : 'ítems atrasados'}
                  </span>
                </p>
                <p className="mt-1 text-sm">Su fecha comprometida ya pasó.</p>
              </>
            ) : (
              <>
                <p className="portal-tablero-dato portal-tablero-en-orden">
                  <FormaDeTono tono="ok" />
                  Sin atrasos
                </p>
                {items.pendientes > 0 && (
                  <p className="portal-suave mt-1 text-sm">Ningún ítem pendiente pasó su fecha comprometida.</p>
                )}
              </>
            )}
          </dd>
        </div>

        <CeldaDeFecha fecha={datos.proxima_fecha} hoy={datos.hoy} seEsperaAlgo={items.por_entregar > 0} />
      </dl>
    </section>
  )
}

interface CeldaDeFechaProps {
  fecha: string | null
  hoy: string
  /** Hay ítems de los que todavía se espera algo. */
  seEsperaAlgo: boolean
}

/**
 * La fecha comprometida más antigua entre lo que se espera. Si ya pasó (hay atrasos) es
 * la del compromiso más antiguo sin cumplir, y el rótulo cambia para no llamarla «próxima».
 * «Ya pasó» se decide contra el `hoy` del backend, nunca contra el reloj del dispositivo.
 */
function CeldaDeFecha({ fecha, hoy, seEsperaAlgo }: CeldaDeFechaProps) {
  const yaPaso = fecha !== null && diasEntre(hoy, fecha) < 0
  const plazo = fmtPlazo(fecha, hoy)
  return (
    <div className="portal-tablero-celda portal-tablero-fecha">
      <dt className="portal-versalita">
        {yaPaso ? 'Compromiso pendiente más antiguo' : 'Próxima fecha comprometida'}
      </dt>
      <dd>
        {fecha ? (
          <p className="portal-tablero-dato">
            <time dateTime={fecha} className="portal-cifra-grande">{fmtFechaCorta(fecha, hoy)}</time>
            {plazo && <span className="portal-tablero-unidad">{plazo}</span>}
          </p>
        ) : (
          <p className="portal-suave portal-tablero-dato">
            {seEsperaAlgo ? 'Sin fecha comprometida' : 'Sin entregas pendientes'}
          </p>
        )}
      </dd>
    </div>
  )
}

interface DondeVieneProps {
  estados: Tablero['pendientes_por_estado']
  items: Tablero['items']
}

/** Los ítems que faltan, por etapa y en el orden del recorrido (así llegan del backend). */
export function DondeVieneLoQueFalta({ estados, items }: DondeVieneProps) {
  const id = useId()
  const mayor = Math.max(1, ...estados.map((e) => e.items))
  return (
    <section className="portal-tarjeta p-5 sm:p-7 min-w-0" aria-labelledby={id}>
      <h2 id={id} className="text-lg font-bold tracking-tight">Dónde viene lo que falta</h2>
      {estados.length === 0 ? (
        <p className="portal-tablero-dato portal-tablero-en-orden mt-3">
          {items.total > 0
            ? <><FormaDeTono tono="ok" />Todo lo que pediste ya fue despachado</>
            : <span className="portal-suave">Tus pedidos todavía no tienen ítems para mostrar.</span>}
        </p>
      ) : (
        <>
          <p className="portal-suave mt-1 text-sm">
            {contar(items.pendientes, 'ítem pendiente', 'ítems pendientes')}, según la etapa en que va cada uno.
          </p>
          <ul className="portal-tablero-estados mt-3">
            {estados.map((e) => (
              <li key={e.clave} className="portal-tablero-estado">
                <div className="portal-tablero-estado-nombre"><InsigniaEstado estado={e} /></div>
                {/* Dibujo de apoyo: lo que informa es la cifra escrita al lado. */}
                <div className="portal-tablero-riel" aria-hidden="true">
                  <div
                    className="portal-tablero-relleno"
                    data-tono={tonoDeEstado(e.clave)}
                    style={{ transform: `scaleX(${Math.max(e.items / mayor, LARGO_MINIMO_DE_BARRA)})` }}
                  />
                </div>
                <span className="portal-cifra portal-tablero-estado-cifra">{contar(e.items, 'ítem', 'ítems')}</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  )
}

const MONTOS_A_MOSTRAR: { campo: keyof Omit<MontosDelTablero, 'ordenes_sin_monto'>; rotulo: string }[] = [
  { campo: 'comprado_neto_clp', rotulo: 'Comprado' },
  { campo: 'por_entregar_neto_clp', rotulo: 'Por entregar' },
  { campo: 'facturado_neto_clp', rotulo: 'Facturado' },
]

export function Montos({ montos }: { montos: MontosDelTablero }) {
  const id = useId()
  const sinPrecio = montos.ordenes_sin_monto
  return (
    <section className="portal-tarjeta p-5 sm:p-7 min-w-0" aria-labelledby={id}>
      <h2 id={id} className="text-lg font-bold tracking-tight">Montos</h2>
      <dl className="portal-tablero-montos mt-2">
        {MONTOS_A_MOSTRAR.map(({ campo, rotulo }) => (
          <div key={campo} className="portal-tablero-monto-fila">
            <dt>
              <span className="font-semibold">{rotulo}</span>
              <span className="portal-versalita block">neto, sin IVA</span>
            </dt>
            <dd><Monto valor={montos[campo]} className="portal-tablero-monto" /></dd>
          </div>
        ))}
      </dl>
      <p className="portal-suave mt-4 text-sm">
        «Facturado» es la suma de las facturas emitidas por estos pedidos. No es un saldo ni lo que se debe.
      </p>
      {sinPrecio > 0 && (
        <div className="mt-4">
          <Aviso tono="info">
            {sinPrecio === 1
              ? '1 pedido no tiene precio en el portal y no entra en estas sumas.'
              : `${fmtCantidad(sinPrecio)} pedidos no tienen precio en el portal y no entran en estas sumas.`}
          </Aviso>
        </div>
      )}
    </section>
  )
}

export function SinPedidos({ anio }: { anio: number | undefined }) {
  return (
    <section className="portal-tarjeta p-5 sm:p-7 max-w-2xl">
      <PackageOpen className="w-8 h-8 portal-suave" aria-hidden="true" />
      {anio !== undefined ? (
        <>
          <h2 className="mt-3 text-xl font-bold tracking-tight">No hay pedidos de {anio}</h2>
          <p className="portal-suave mt-2">Puede que tus pedidos sean de otro año.</p>
          {/* Sin `anio` en la dirección = todos los años. */}
          <Link to="/portal" className="portal-boton mt-5 w-full sm:w-auto">Ver todos los años</Link>
        </>
      ) : (
        <>
          <h2 className="mt-3 text-xl font-bold tracking-tight">Aún no hay pedidos a tu nombre</h2>
          <p className="portal-suave mt-2">Si esperabas ver uno, escribe a tu ejecutivo de MachParts.</p>
        </>
      )}
    </section>
  )
}
