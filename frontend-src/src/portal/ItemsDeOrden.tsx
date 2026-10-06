// Los ítems de una orden: la sección «Ítems» de OrdenPage.tsx, con sus controles (orden y
// filtro) y sus dos vistas, tabla en pantalla ancha y tarjetas en teléfono y tablet.
//
// No pide nada: recibe los `items` de GET /api/portal/ordenes/{id}.
//
// DECISIONES QUE NO SE VEN EN EL CÓDIGO
//   · El orden por defecto es el que manda el backend (backend/portal/ordenes.py::lineas):
//     pendientes primero, lo más atrasado arriba, entregados al final. Acá no se vuelve
//     a ordenar por fecha: se respeta lo que llegó, y lo ya entregado va plegado.
//   · «Posición en la OC» (?orden=posicion en la URL) existe para cuadrar contra la
//     orden de compra del cliente: UNA lista corrida 1, 2, 3…, con pendientes y
//     entregados juntos (partida en dos habría que saltar de una a otra para seguir la
//     numeración). Si ningún ítem trae posición (carga desde Excel sin N° de ítem) el
//     control no se muestra.
//   · El filtro compara sin guiones, espacios, tildes ni mayúsculas, igual que el
//     buscador del backend (backend/portal/busqueda.py): «1r 0716» encuentra «1R-0716».
//     Vive en memoria y no en la URL: es una ayuda para leer, no un estado que compartir.
//   · La tabla aparece desde 64rem (1024 px), no desde 641 px como en otras pantallas:
//     con las dos columnas de montos no cabe antes sin desplazarse hacia el lado. Las
//     dos vistas se escriben siempre y portal.css muestra una (clases portal-orden-en-*).
//   · Las columnas de montos existen solo si la cuenta ve montos Y algún ítem trae
//     precio: dos columnas enteras de rayas no informan nada.
//   · La región role="status" con la cuenta de ítems está SIEMPRE montada: una región
//     que nace ya con su texto no se anuncia. El foco no se mueve al filtrar ni al ordenar.
import { useId, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { Camera, ChevronRight } from 'lucide-react'
import type { ItemDeOrden } from './types'
import { tonoDeEstado } from './estadosUi'
import { contar, fmtCantidad } from './formato'
import { CopiarNumeroDeParte, FechaComprometida, FormaDeTono, InsigniaEstado, Monto, NumeroDeParte, PrecioUnitario } from './piezas'
import { rutaDeItem } from './rutas'

const PARAMETRO_DE_ORDEN = 'orden'
const POR_POSICION = 'posicion'
// Hasta esta cantidad un repuesto se encuentra a simple vista y el campo solo estorba.
const MAXIMO_SIN_FILTRO = 8
// Un ítem sin posición va al final de la lista ordenada por posición.
const SIN_POSICION = Number.MAX_SAFE_INTEGER

/** Lo que cambia entre una vista de la lista y otra, y que tabla y tarjeta necesitan saber. */
interface Vista {
  ordenId: number
  hoy: string
  conPosicion: boolean
  conMontos: boolean
}

// ─── Filtro y orden (funciones puras) ─────────────────────────────────────────
/** «1R-0716», «1r 0716» y «1R0716» quedan iguales: minúsculas, sin tildes, solo letras y dígitos. */
function compactar(texto: string): string {
  return texto.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase().replace(/[^a-z0-9]+/g, '')
}

function palabrasDe(filtro: string): string[] {
  return filtro.split(/\s+/).map(compactar).filter(Boolean)
}

/** Los ítems que contienen TODAS las palabras, en su N° de parte, descripción o marca. */
function filtrar(items: ItemDeOrden[], palabras: string[]): ItemDeOrden[] {
  if (palabras.length === 0) return items
  return items.filter((it) => {
    const texto = compactar(`${it.numero_parte} ${it.descripcion} ${it.marca}`)
    return palabras.every((palabra) => texto.includes(palabra))
  })
}

function compararPosicion(a: ItemDeOrden, b: ItemDeOrden): number {
  return (a.linea ?? SIN_POSICION) - (b.linea ?? SIN_POSICION) || a.id - b.id
}

// ─── La sección ───────────────────────────────────────────────────────────────
interface ItemsDeOrdenProps {
  ordenId: number
  items: ItemDeOrden[]
  hoy: string
  veMontos: boolean
}

export default function ItemsDeOrden({ ordenId, items, hoy, veMontos }: ItemsDeOrdenProps) {
  const [parametros, setParametros] = useSearchParams()
  const [filtro, setFiltro] = useState('')
  const id = useId()

  const hayPosiciones = items.some((it) => it.linea !== null)
  const porPosicion = hayPosiciones && parametros.get(PARAMETRO_DE_ORDEN) === POR_POSICION
  const palabras = useMemo(() => palabrasDe(filtro), [filtro])
  const visibles = useMemo(() => filtrar(items, palabras), [items, palabras])
  const hayFiltro = palabras.length > 0
  // Por posición: una sola lista. Por fecha: pendientes a la vista, entregados plegados.
  const principales = porPosicion ? [...visibles].sort(compararPosicion) : visibles.filter((it) => !it.entregado)
  const entregados = porPosicion ? [] : visibles.filter((it) => it.entregado)
  const vista: Vista = {
    ordenId,
    hoy,
    conPosicion: hayPosiciones,
    conMontos: veMontos && items.some((it) => it.monto !== null),
  }

  const ordenar = (posicion: boolean) => {
    // `replace`: cambiar el orden no es un paso al que «Atrás» deba volver.
    setParametros((previos) => {
      const nuevos = new URLSearchParams(previos)
      if (posicion) nuevos.set(PARAMETRO_DE_ORDEN, POR_POSICION)
      else nuevos.delete(PARAMETRO_DE_ORDEN)
      return nuevos
    }, { replace: true })
  }

  return (
    <section className="mt-10" aria-labelledby={`${id}-titulo`}>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 id={`${id}-titulo`} className="text-xl font-bold tracking-tight">Ítems</h2>
        <p role="status" className="portal-suave text-sm">{resumenDe(items.length, visibles.length, hayFiltro)}</p>
      </div>

      {items.length === 0 && <p className="portal-suave mt-3">Este pedido todavía no tiene ítems cargados.</p>}

      {(hayPosiciones || items.length > MAXIMO_SIN_FILTRO) && (
        <div className="portal-orden-controles">
          {hayPosiciones && (
            <div role="group" aria-labelledby={`${id}-ordenar`} className="portal-orden-ordenar">
              <span id={`${id}-ordenar`} className="portal-versalita">Ordenar por</span>
              <button type="button" className="portal-chip" aria-pressed={!porPosicion} onClick={() => ordenar(false)}>
                Fecha comprometida
              </button>
              <button type="button" className="portal-chip" aria-pressed={porPosicion} onClick={() => ordenar(true)}>
                Posición en la OC
              </button>
            </div>
          )}
          {items.length > MAXIMO_SIN_FILTRO && (
            <div className="portal-orden-filtro">
              <label htmlFor={`${id}-filtro`} className="portal-versalita">Filtrar ítems</label>
              <input
                id={`${id}-filtro`}
                type="search"
                className="portal-campo mt-1"
                placeholder="N° de parte o descripción"
                value={filtro}
                onChange={(e) => setFiltro(e.target.value)}
                enterKeyHint="search"
                autoComplete="off"
                autoCapitalize="off"
                spellCheck={false}
              />
            </div>
          )}
        </div>
      )}

      {principales.length > 0 && (
        <ListaDeItems items={principales} vista={vista} titulo={porPosicion ? 'Ítems en el orden de la OC' : 'Ítems pendientes'} />
      )}

      {entregados.length > 0 && (
        // Abierta cuando no hay nada pendiente que mirar, o cuando el filtro encontró algo
        // acá adentro (plegada, la coincidencia quedaría escondida).
        <details className="portal-orden-entregados" open={principales.length === 0 || hayFiltro}>
          <summary>
            <ChevronRight className="portal-orden-flecha" aria-hidden="true" />
            {contar(entregados.length, 'ítem ya entregado', 'ítems ya entregados')}
          </summary>
          <ListaDeItems items={entregados} vista={vista} titulo="Ítems ya entregados" />
        </details>
      )}

      {hayFiltro && visibles.length === 0 && (
        <div className="portal-nota mt-4 p-4">
          <p>Ningún ítem de este pedido coincide con «{filtro.trim()}».</p>
          <button type="button" className="portal-boton portal-boton-secundario portal-boton-compacto mt-3" onClick={() => setFiltro('')}>
            Quitar el filtro
          </button>
        </div>
      )}
    </section>
  )
}

function resumenDe(total: number, visibles: number, hayFiltro: boolean): string {
  if (!hayFiltro) return contar(total, 'ítem', 'ítems')
  if (visibles === 0) return 'Ningún ítem coincide con el filtro'
  return `${contar(visibles, 'ítem', 'ítems')} de ${fmtCantidad(total)}`
}

// ─── Tabla y tarjetas ─────────────────────────────────────────────────────────
function ListaDeItems({ items, vista, titulo }: { items: ItemDeOrden[]; vista: Vista; titulo: string }) {
  return (
    <>
      <div className="portal-orden-en-tabla portal-tabla-marco mt-4">
        <table className="portal-tabla portal-orden-tabla">
          <caption className="sr-only">{titulo}</caption>
          <thead>
            <tr>
              {vista.conPosicion && <th scope="col" className="portal-cifra"><abbr title="Posición en la orden de compra">Pos.</abbr></th>}
              <th scope="col">N° de parte</th>
              <th scope="col" className="portal-orden-col-descripcion">Descripción</th>
              <th scope="col" className="portal-cifra"><abbr title="Cantidad">Cant.</abbr></th>
              <th scope="col" className="portal-orden-col-estado">Estado y fecha</th>
              {vista.conMontos && <th scope="col" className="portal-cifra">Unitario neto</th>}
              {vista.conMontos && <th scope="col" className="portal-cifra">Total neto</th>}
              <th scope="col">Seguimiento</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => <FilaDeItem key={item.id} item={item} vista={vista} />)}
          </tbody>
        </table>
      </div>
      <div className="portal-orden-en-tarjetas mt-4">
        <ul className="portal-orden-tarjetas" aria-label={titulo}>
          {items.map((item) => <li key={item.id}><TarjetaDeItem item={item} vista={vista} /></li>)}
        </ul>
      </div>
    </>
  )
}

function FilaDeItem({ item, vista }: { item: ItemDeOrden; vista: Vista }) {
  return (
    <tr data-atrasado={item.vencida}>
      {vista.conPosicion && <td className="portal-cifra">{item.linea ?? '—'}</td>}
      {/* aria-label: el nombre del encabezado de fila es SOLO el N° de parte; el botón de
          copiar sigue dentro (el comprador cuadra columnas contra su sistema) pero no se
          cuela en el nombre con que el lector anuncia cada celda. */}
      <th scope="row" aria-label={item.numero_parte || 'Sin N° de parte'}>
        <NumeroDeParte valor={item.numero_parte} />
      </th>
      <td className="portal-orden-col-descripcion"><Descripcion item={item} /></td>
      <td className="portal-cifra">{fmtCantidad(item.cantidad)}</td>
      <td className="portal-orden-col-estado"><EstadoDelItem item={item} hoy={vista.hoy} /></td>
      {vista.conMontos && <td className="portal-cifra"><PrecioUnitario monto={item.monto} /></td>}
      {vista.conMontos && <td className="portal-cifra"><Monto valor={item.monto?.total_clp ?? null} /></td>}
      <td>
        <div className="portal-orden-seguimiento">
          <EnlaceAlSeguimiento item={item} ordenId={vista.ordenId} />
          <IndicadorDeFotos fotos={item.fotos} />
        </div>
      </td>
    </tr>
  )
}

function TarjetaDeItem({ item, vista }: { item: ItemDeOrden; vista: Vista }) {
  return (
    <article className="portal-tarjeta portal-tarjeta-enlace portal-orden-tarjeta" data-atrasado={item.vencida}>
      <div className="portal-orden-tarjeta-cabeza">
        {/* Un <h3> para que el lector salte de ítem en ítem; el botón de copiar queda
            afuera para no colarse en su nombre. El N° de parte no se corta; si es más
            ancho que la tarjeta se desplaza adentro. */}
        <h3 className="portal-orden-parte"><NumeroDeParte valor={item.numero_parte} conCopiar={false} /></h3>
        {item.numero_parte && <CopiarNumeroDeParte valor={item.numero_parte} />}
        {vista.conPosicion && item.linea !== null && <span className="portal-versalita">Pos. {item.linea}</span>}
      </div>
      <Descripcion item={item} />
      <EstadoDelItem item={item} hoy={vista.hoy} />
      <dl className="portal-orden-cifras">
        <div>
          <dt>Cantidad</dt>
          <dd className="portal-cifra">{fmtCantidad(item.cantidad)}</dd>
        </div>
        {vista.conMontos && (
          <div>
            <dt>Unitario neto</dt>
            <dd><PrecioUnitario monto={item.monto} /></dd>
          </div>
        )}
        {vista.conMontos && (
          <div>
            <dt>Total neto</dt>
            <dd><Monto valor={item.monto?.total_clp ?? null} /></dd>
          </div>
        )}
      </dl>
      <div className="portal-orden-seguimiento">
        {/* Con portal-ancla-tarjeta el enlace cubre la tarjeta entera: se toca en cualquier parte. */}
        <EnlaceAlSeguimiento item={item} ordenId={vista.ordenId} cubreLaTarjeta />
        <IndicadorDeFotos fotos={item.fotos} />
      </div>
    </article>
  )
}

// ─── Piezas de un ítem (las comparten la fila y la tarjeta) ───────────────────
function Descripcion({ item }: { item: ItemDeOrden }) {
  return (
    <p>
      {item.descripcion || <span className="portal-suave">Sin descripción</span>}
      {item.marca && <span className="portal-suave block text-sm">{item.marca}</span>}
    </p>
  )
}

/** Estado, desglose por unidades (si el ítem va repartido) y fecha comprometida. */
function EstadoDelItem({ item, hoy }: { item: ItemDeOrden; hoy: string }) {
  return (
    <div className="portal-orden-estado">
      <InsigniaEstado estado={item.estado} />
      {item.partes.length > 0 && (
        <ul className="portal-orden-partes" aria-label="Unidades por estado">
          {item.partes.map((parte) => (
            <li key={parte.estado.clave}>
              <FormaDeTono tono={tonoDeEstado(parte.estado.clave)} />
              <span className="portal-cifra">{fmtCantidad(parte.cantidad)}</span>
              {parte.estado.etiqueta}
            </li>
          ))}
        </ul>
      )}
      <FechaComprometida
        fecha={item.fecha_prometida}
        vencida={item.vencida}
        seEspera={item.cantidad_por_entregar > 0}
        hoy={hoy}
      />
    </div>
  )
}

function IndicadorDeFotos({ fotos }: { fotos: number }) {
  if (fotos <= 0) return null
  return (
    <span className="portal-orden-fotos">
      <Camera aria-hidden="true" />
      <span aria-hidden="true">{fotos}</span>
      <span className="sr-only">{contar(fotos, 'foto', 'fotos')}</span>
    </span>
  )
}

interface EnlaceAlSeguimientoProps {
  item: ItemDeOrden
  ordenId: number
  cubreLaTarjeta?: boolean
}

function EnlaceAlSeguimiento({ item, ordenId, cubreLaTarjeta = false }: EnlaceAlSeguimientoProps) {
  return (
    <Link
      to={rutaDeItem(ordenId, item.id)}
      className={`portal-enlace portal-orden-ver${cubreLaTarjeta ? ' portal-ancla-tarjeta' : ''}`}
    >
      Ver seguimiento
      {/* Catorce enlaces «Ver seguimiento» iguales no le dicen nada a un lector de pantalla. */}
      <span className="sr-only"> de {item.numero_parte || item.descripcion || 'este ítem'}</span>
      <ChevronRight aria-hidden="true" />
    </Link>
  )
}
