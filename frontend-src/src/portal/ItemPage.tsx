// Seguimiento de UN ítem (ruta /portal/pedidos/:ordenId/items/:itemId). Responde lo que
// el cliente viene a preguntar: «¿en qué va ESTE repuesto y cuándo lo comprometieron?».
// Consume GET /api/portal/ordenes/{ordenId}/items/{itemId} (portalAPI.item), que trae el
// ítem, su recorrido (hitos), sus fotos y SOLO las guías y facturas de este ítem.
//
// DECISIONES QUE NO SE VEN EN EL CÓDIGO
//   · La fecha comprometida y «atrasado» vienen del backend (`fecha_prometida`,
//     `vencida`); los plazos se cuentan contra `hoy` de la respuesta. Acá no se mira el
//     reloj del dispositivo.
//   · Un hito pendiente NUNCA muestra fecha ni detalle, aunque el backend los mandara: el
//     portal no estima cuándo va a pasar algo. La única fecha futura de la pantalla es
//     la comprometida.
//   · El backend marca el paso en que va el ítem como `cumplido` y `actual` a la vez. Si
//     ese paso es el final (Despachado / Despachado y facturado) se pinta como cumplido,
//     no «en curso»: la mercadería ya salió (ver estadoDeHito).
//   · Montos: el perfil 'bodega' no tiene filas de precio (veMontos=false). A quien sí
//     ve montos, un ítem sin precio congelado (`monto` null) le muestra «—», nunca $0.
//     Son montos NETOS y lo dicen sus rótulos.
//   · Un id de la URL que no es un número no se le pregunta al backend (respondería un
//     422 con un `detail` técnico): para el cliente es un enlace roto, «no encontramos».
//   · Si falla una RECARGA se siguen mostrando los datos anteriores con un aviso, salvo
//     que la respuesta sea 404: ahí el ítem dejó de ser del cliente y ya no se muestra.
import { useId, type ReactNode } from 'react'
import { Link, useParams } from 'react-router-dom'
import { skipToken, useQuery } from '@tanstack/react-query'
import { ArrowLeft } from 'lucide-react'
import { portalAPI, estadoHttp, motivoDeBloqueo } from './api'
import type { ClaveEstado, Hito, ItemDeOrden, SeguimientoItem } from './types'
import { claveDeConsulta, usePortal, useTituloDePestana } from './sesion'
import { TOLERANCIA_DE_CANTIDAD, fmtCantidad, fmtFecha } from './formato'
import { tonoDeEstado, type Tono } from './estadosUi'
import {
  Actualizado, Aviso, AvisoDeRecargaFallida, Cargando, ErrorDeCarga, FechaComprometida, FormaDeTono, InsigniaEstado,
  CopiarNumeroDeParte, Migas, Monto, NoEncontrado, NumeroDeParte, PrecioUnitario, SinConexion,
} from './piezas'
import Documentos from './Documentos'
import GaleriaDeFotos from './GaleriaDeFotos'
import { RUTA_DE_PEDIDOS, idDeRuta, rutaDeOrden } from './rutas'

// Desde este largo el N° de parte no cabe en una línea del título a 320 px: se achica y
// se le permite partirse (portal.css, .portal-item-titulo[data-largo]).
const LARGO_QUE_NO_CABE_EN_EL_TITULO = 15

/** El id de la URL como número, o null si no es un entero positivo. */
export default function ItemPage() {
  const parametros = useParams()
  const { usuarioId, veMontos } = usePortal()
  const ordenId = idDeRuta(parametros.ordenId)
  const itemId = idDeRuta(parametros.itemId)
  const idsValidos = ordenId !== null && itemId !== null

  const consulta = useQuery({
    queryKey: claveDeConsulta(usuarioId, 'item', ordenId, itemId),
    // skipToken: con ids inválidos la consulta no sale. Los reintentos NO se configuran
    // acá: los fija sesion.ts::prepararConsultasDelPortal para todo el portal.
    queryFn: idsValidos ? async () => (await portalAPI.item(ordenId, itemId)).data : skipToken,
  })
  const datos = consulta.data
  const { refetch } = consulta

  const estado = consulta.isError ? estadoHttp(consulta.error) : undefined
  useTituloDePestana(
    datos
      ? `${datos.item.numero_parte || 'Ítem'}${datos.orden.numero_oc ? ` · OC ${datos.orden.numero_oc}` : ''}`
      : !idsValidos || estado === 404 ? 'Ítem no encontrado' : 'Seguimiento del ítem'
  )

  if (!idsValidos || estado === 404) {
    return <NoEncontrado titulo="No encontramos este ítem" volverA={RUTA_DE_PEDIDOS} volverTexto="Volver a Mis pedidos" />
  }
  // 403 con texto: el backend explica qué pasa con la cuenta y qué hacer; va tal cual.
  const motivo = consulta.isError ? motivoDeBloqueo(consulta.error) : null
  if (motivo) return <SinAcceso motivo={motivo} />

  if (!datos) {
    if (consulta.isError) {
      return <ErrorDeCarga nivel={1} titulo="No pudimos cargar este ítem" onReintentar={() => { void refetch() }} error={consulta.error} />
    }
    return consulta.fetchStatus === 'paused'
      ? <SinConexion texto="El seguimiento se cargará solo cuando vuelva la señal." />
      : <Cargando texto="Cargando el seguimiento del ítem…" />
  }
  return (
    <Seguimiento
      datos={datos}
      veMontos={veMontos}
      recargando={consulta.isFetching}
      sinActualizar={consulta.isRefetchError}
      onRecargar={() => { void refetch() }}
    />
  )
}

function SinAcceso({ motivo }: { motivo: string }) {
  return (
    <section className="portal-tarjeta portal-entrada p-5 sm:p-7 max-w-2xl">
      <h1 className="mb-4 text-xl font-bold tracking-tight">No podemos mostrar este ítem</h1>
      <Aviso tono="error">{motivo}</Aviso>
    </section>
  )
}

// ─── La pantalla con datos ────────────────────────────────────────────────────
interface SeguimientoProps {
  datos: SeguimientoItem
  veMontos: boolean
  recargando: boolean
  /** La última recarga falló: lo que se ve es de la carga anterior. */
  sinActualizar: boolean
  onRecargar: () => void
}

function Seguimiento({ datos, veMontos, recargando, sinActualizar, onRecargar }: SeguimientoProps) {
  const { orden, item, hitos, fotos, guias, facturas, hoy } = datos
  const rutaDeLaOrden = rutaDeOrden(orden.id)
  // El N° de OC es texto libre y puede venir vacío.
  const nombreDeLaOrden = orden.numero_oc ? `OC ${orden.numero_oc}` : 'Pedido'

  return (
    <div className="portal-entrada">
      <Migas
        pasos={[
          { texto: 'Mis pedidos', a: RUTA_DE_PEDIDOS },
          { texto: nombreDeLaOrden, a: rutaDeLaOrden },
          { texto: item.numero_parte || 'Ítem' },
        ]}
      />
      <Link to={rutaDeLaOrden} className="portal-enlace inline-flex items-center gap-1.5 min-h-[2.75rem]">
        <ArrowLeft className="w-4 h-4 shrink-0" aria-hidden="true" />
        {orden.numero_oc ? `Volver a la OC ${orden.numero_oc}` : 'Volver al pedido'}
      </Link>

      <div className="mt-2 flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
        <div className="min-w-0 max-w-full">
          <p className="portal-rotulo">Seguimiento del ítem</p>
          {/* El botón de copiar va JUNTO al <h1>, no adentro: dentro se colaría en el
              nombre del encabezado («1R-0716 Copiar N° de parte 1R-0716»). */}
          <div className="portal-item-titulo-grupo">
            <h1
              className="portal-item-titulo mt-1"
              data-largo={item.numero_parte.length >= LARGO_QUE_NO_CABE_EN_EL_TITULO}
            >
              <NumeroDeParte valor={item.numero_parte} conCopiar={false} />
            </h1>
            {item.numero_parte && <CopiarNumeroDeParte valor={item.numero_parte} />}
          </div>
          {item.descripcion && <p className="mt-1 text-base sm:text-lg">{item.descripcion}</p>}
        </div>
        <Actualizado sello={datos} onRecargar={onRecargar} cargando={recargando} />
      </div>

      {sinActualizar && <div className="mt-4"><AvisoDeRecargaFallida motivo={null} /></div>}

      <FichaDelItem item={item} hoy={hoy} veMontos={veMontos} />

      {/* En un teléfono el orden es el de la pregunta: dónde va, luego las fotos y los
          documentos. Desde lg, el recorrido a la izquierda y lo demás a la derecha. */}
      <div className="mt-6 grid gap-6 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)] lg:items-start">
        <Recorrido hitos={hitos} />
        <div className="space-y-6 min-w-0">
          {fotos.length > 0 ? (
            <div className="portal-tarjeta p-5 sm:p-7">
              <GaleriaDeFotos
                fotos={fotos}
                titulo={item.numero_parte ? `Fotos de ${item.numero_parte}` : 'Fotos del ítem'}
              />
            </div>
          ) : (
            // Sin título ni tarjeta: que todavía no haya fotos no es una falta.
            <p className="portal-suave text-sm">Aún no hay fotos de este ítem.</p>
          )}
          <Documentos guias={guias} facturas={facturas} hoy={hoy} veMontos={veMontos} />
        </div>
      </div>
    </div>
  )
}

// ─── Ficha ────────────────────────────────────────────────────────────────────
interface FichaDelItemProps {
  item: ItemDeOrden
  hoy: string
  veMontos: boolean
}

/**
 * A la izquierda, lo que se viene a buscar: en qué estado va y la fecha comprometida. A
 * la derecha, los datos del ítem como renglones rótulo–valor.
 */
function FichaDelItem({ item, hoy, veMontos }: FichaDelItemProps) {
  const idTitulo = useId()
  // Lo que salió en una guía y el cliente firmó como no recibido: ni entregado ni por
  // entregar (types.ts::ItemDeOrden explica las tres cantidades).
  const noRecibidas = item.cantidad - item.cantidad_entregada - item.cantidad_por_entregar
  // Las cantidades son decimales (0.1 + 0.2 no es 0.3): se compara con la misma
  // tolerancia que el backend, no contra el cero exacto.
  const hayNoRecibidas = noRecibidas > TOLERANCIA_DE_CANTIDAD
  return (
    <section className="portal-tarjeta portal-item-ficha mt-5" aria-labelledby={idTitulo}>
      <h2 id={idTitulo} className="sr-only">Estado y fecha comprometida</h2>

      <div className="portal-item-compromiso" data-tono={item.vencida ? 'atencion' : undefined}>
        <p>
          <span className="sr-only">Estado: </span>
          <InsigniaEstado estado={item.estado} />
        </p>
        <p className="portal-item-compromiso-fecha">
          <FechaComprometida
            fecha={item.fecha_prometida}
            vencida={item.vencida}
            seEspera={item.cantidad_por_entregar > 0}
            hoy={hoy}
          />
        </p>
        {item.fecha_prometida && (
          <p className="portal-suave text-sm">Es la fecha acordada al cerrar tu pedido, no una estimación.</p>
        )}
      </div>

      <dl className="portal-item-datos">
        {item.marca && <Dato rotulo="Marca">{item.marca}</Dato>}
        {item.linea !== null && <Dato rotulo="Posición en la OC">{item.linea}</Dato>}
        <Dato rotulo="Cantidad">{fmtCantidad(item.cantidad)}</Dato>
        <Dato rotulo="Entregado">
          {fmtCantidad(item.cantidad_entregada)} de {fmtCantidad(item.cantidad)}
        </Dato>
        {hayNoRecibidas && <Dato rotulo="No recibido en la entrega">{fmtCantidad(noRecibidas)}</Dato>}
        {item.partes.length > 0 && (
          <Dato rotulo="Reparto por estado">
            <ul className="portal-item-partes">
              {item.partes.map((parte) => (
                <li key={parte.estado.clave}>
                  <span>{fmtCantidad(parte.cantidad)}</span>
                  <InsigniaEstado estado={parte.estado} />
                </li>
              ))}
            </ul>
          </Dato>
        )}
        {veMontos && (
          <>
            <Dato rotulo="Precio unitario neto">
              <PrecioUnitario monto={item.monto} />
            </Dato>
            <Dato rotulo="Total neto del ítem" destacado>
              <Monto valor={item.monto?.total_clp ?? null} />
            </Dato>
          </>
        )}
      </dl>
    </section>
  )
}

function Dato({ rotulo, destacado = false, children }: { rotulo: string; destacado?: boolean; children: ReactNode }) {
  return (
    <div className="portal-item-dato" data-destacado={destacado}>
      <dt className="portal-versalita">{rotulo}</dt>
      <dd>{children}</dd>
    </div>
  )
}

// ─── Recorrido ────────────────────────────────────────────────────────────────
type EstadoDeHito = 'cumplido' | 'actual' | 'pendiente'

// Lo que un lector de pantalla dice antes de la etiqueta del hito (a la vista lo dice la
// forma del disco).
const PREFIJO_DE_HITO: Record<EstadoDeHito, string> = {
  cumplido: 'Cumplido: ',
  actual: 'En curso: ',
  pendiente: 'Pendiente: ',
}
// La forma dentro del disco. El hito pendiente no lleva ninguna: su aro punteado vacío
// (portal.css, .portal-hito-marca) ya es el «○».
const FORMA_DE_HITO: Record<EstadoDeHito, Tono | null> = {
  cumplido: 'ok',
  actual: 'curso',
  pendiente: null,
}

function estadoDeHito(hito: Hito): EstadoDeHito {
  if (!hito.cumplido && !hito.actual) return 'pendiente'
  // Tono 'ok' = la mercadería ya salió: ese paso, aunque sea el actual, está cumplido.
  if (hito.actual && tonoDeEstado(hito.clave) !== 'ok') return 'actual'
  return 'cumplido'
}

function Recorrido({ hitos }: { hitos: Hito[] }) {
  const idTitulo = useId()
  return (
    <section className="portal-tarjeta p-5 sm:p-7 min-w-0" aria-labelledby={idTitulo}>
      <h2 id={idTitulo} className="text-lg font-bold tracking-tight">Recorrido</h2>
      <ol className="portal-hitos mt-5">
        {hitos.map((hito) => <HitoDelRecorrido key={hito.clave} hito={hito} />)}
      </ol>
      <p className="portal-item-nota portal-suave text-sm">
        Las fechas del recorrido son hechos registrados. La única fecha futura es la comprometida.
      </p>
    </section>
  )
}

// Las incidencias no son pasos del recorrido (backend/portal/estados.py las agrega al
// final, como paso actual). Cada una lleva una frase que dice qué significa para el
// cliente. La de «no recibido» no promete una reposición: no se repone por este pedido.
const NOTA_DE_INCIDENCIA: Partial<Record<ClaveEstado, string>> = {
  en_gestion: 'Estamos gestionando la reposición de este ítem. Tu ejecutivo te mantendrá al tanto.',
  no_recibido: 'Según la guía firmada, estas unidades no se recibieron. Para reponerlas, contacta a tu ejecutivo de MachParts.',
}

function HitoDelRecorrido({ hito }: { hito: Hito }) {
  const estado = estadoDeHito(hito)
  // Incidencia = lo que el catálogo de tonos pinta como «atención» (estadosUi.ts): así una
  // incidencia nueva se ve igual acá y en las listas aunque todavía no tenga su frase.
  const esIncidencia = tonoDeEstado(hito.clave) === 'atencion'
  const nota = NOTA_DE_INCIDENCIA[hito.clave]
  const forma = esIncidencia ? 'atencion' : FORMA_DE_HITO[estado]
  const ocurrio = estado !== 'pendiente'

  return (
    <li
      className="portal-hito"
      data-estado={estado}
      data-tono={esIncidencia ? 'atencion' : undefined}
      aria-current={hito.actual ? 'step' : undefined}
    >
      <span className="portal-hito-marca" aria-hidden="true">
        {forma && <FormaDeTono tono={forma} />}
      </span>
      <div className="min-w-0">
        <p className="portal-hito-titulo">
          <span className="sr-only">{PREFIJO_DE_HITO[estado]}</span>
          {hito.etiqueta}
        </p>
        {ocurrio && hito.fecha && <time dateTime={hito.fecha}>{fmtFecha(hito.fecha)}</time>}
        {ocurrio && hito.detalle && <p className="portal-hito-detalle">{hito.detalle}</p>}
        {nota && <p className="portal-hito-detalle portal-item-gestion">{nota}</p>}
      </div>
    </li>
  )
}
