// Inicio del portal (ruta /portal): responde «¿cómo van mis compras?» de un vistazo.
// Consume GET /api/portal/resumen (portalAPI.tablero → `Tablero` en types.ts).
//
// LA PANTALLA NO SUMA NADA. Pedidos abiertos, ítems pendientes y atrasados, la etapa de lo
// que falta y los montos llegan calculados por backend/portal/resumen.py, con las mismas
// funciones que arman el listado y el detalle: si acá dijera 7 y al entrar a los pedidos
// se contaran 6, el portal perdería su credibilidad. Lo único que se calcula acá es el
// largo de las barras.
//
// DECISIONES QUE NO SE VEN EN EL CÓDIGO
//   · El año elegido vive en la URL (?anio=2025; sin parámetro = todos los años): se puede
//     recargar, guardar y deshacer con «Atrás». Los enlaces a «Mis pedidos» llevan ese
//     mismo año, para que la lista a la que se llega tenga los pedidos que acá se contaron.
//   · No hay un porcentaje de avance general. El backend no lo manda, y dividir ítems
//     despachados por ítems totales daría otra cifra que el avance de cada pedido
//     (ordenes.py::_avance_pct pondera las entregas parciales).
//   · `proxima_fecha` es la fecha comprometida más antigua ENTRE LO QUE SE ESPERA. Con
//     ítems atrasados esa fecha ya pasó: no es «próxima», y el rótulo lo dice (CeldaDeFecha).
//   · «Pendiente» es todo ítem no entregado; incluye los que solo tienen unidades «no
//     recibidas en la entrega», de los que ya no se espera nada (`items.por_entregar` los
//     deja fuera). Por eso la celda de la fecha pregunta por lo que se espera.
//   · `montos === null` es el perfil 'bodega': la sección de montos no se dibuja. No es $0.
//   · «Facturado» es la suma de las facturas emitidas, no un saldo: el portal no dice
//     cuánto se debe (decisión del negocio). La nota bajo los montos lo aclara.
//   · Al cambiar de año no se muestran los números del año anterior mientras llega el
//     nuevo (ver `datos` en TableroPage): a pleno sol nadie distingue un número atenuado.
import { Link, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowRight } from 'lucide-react'
import { portalAPI, motivoDeBloqueo } from './api'
import type { PortalYo } from './types'
import { claveDeConsulta, usePortal, useTituloDePestana } from './sesion'
import { contar, fmtCantidad } from './formato'
import { ANIO_MAXIMO, ANIO_MINIMO, leerEntero } from './filtrosDePedidos'
import { Actualizado, Aviso, AvisoDeRecargaFallida, Cargando, ErrorDeCarga, SelectorDeAnio, SinConexion } from './piezas'
import { rutaDePedidos } from './rutas'
import { DondeVieneLoQueFalta, FichaDeAvance, Montos, SinPedidos, type ResumenProps } from './TableroSecciones'

function leerAnio(texto: string | null): number | undefined {
  return leerEntero(texto, ANIO_MINIMO, ANIO_MAXIMO) ?? undefined
}

export default function TableroPage() {
  const { yo, usuarioId } = usePortal()
  const [parametros, setParametros] = useSearchParams()
  const anio = leerAnio(parametros.get('anio'))
  useTituloDePestana('Inicio')

  const consulta = useQuery({
    queryKey: claveDeConsulta(usuarioId, 'tablero', anio ?? 'todos'),
    queryFn: async () => (await portalAPI.tablero(anio)).data,
    // Solo para que el selector no pierda sus años mientras carga otro (ver `datos`). Con
    // el mismo candado que Mis pedidos: el resumen anterior se conserva solo si es de ESTA
    // cuenta (si la sesión cambió desde otra pestaña, ni la lista de años se hereda).
    placeholderData: (previos, consultaPrevia) =>
      consultaPrevia?.queryKey[1] === usuarioId ? previos : undefined,
  })
  // Con keepPreviousData, `consulta.data` es por un momento el resumen del año ANTERIOR.
  // De ahí solo se toma la lista de años (el backend la manda completa, sea cual sea el
  // año pedido); los números no se pintan hasta que llega la respuesta del año elegido.
  const datos = consulta.isPlaceholderData ? undefined : consulta.data
  const aniosConocidos = consulta.data?.anios ?? []
  const periodo = anio !== undefined ? `año ${anio}` : 'todos los años'
  const bloqueo = consulta.isError ? motivoDeBloqueo(consulta.error) : null
  const recargar = () => { void consulta.refetch() }

  const elegirAnio = (valor: number | null) => {
    setParametros((previos) => {
      const siguientes = new URLSearchParams(previos)
      if (valor !== null) siguientes.set('anio', String(valor))
      else siguientes.delete('anio')
      return siguientes
    })
  }

  return (
    <div className="portal-entrada">
      <Saludo yo={yo} />

      <div className="portal-tablero-barra mt-6">
        <SelectorDeAnio anios={aniosConocidos} anio={anio ?? null} onElegir={elegirAnio} className="portal-tablero-anio" />
        {datos && <Actualizado sello={datos} onRecargar={recargar} cargando={consulta.isFetching} />}
      </div>
      {/* Anuncia el resultado de cambiar de año sin mover el foco del selector. Siempre
          montada: una región que nace ya con su texto no se anuncia. */}
      <p role="status" className="sr-only">
        {datos ? `Resumen de ${periodo}: ${contar(datos.ordenes.total, 'pedido', 'pedidos')}` : ''}
      </p>

      <div className="mt-5">
        {!datos && !consulta.isError && (
          consulta.fetchStatus === 'paused'
            ? <SinConexion texto="Tu resumen se cargará solo cuando vuelva la señal." />
            : <Cargando texto={anio !== undefined ? `Cargando el resumen de ${anio}…` : 'Cargando tu resumen…'} />
        )}
        {!datos && consulta.isError && (
          bloqueo
            ? <Aviso tono="error">{bloqueo}</Aviso>
            : <ErrorDeCarga titulo="No pudimos cargar tu resumen" onReintentar={recargar} error={consulta.error} />
        )}
        {/* «Recargar» falló pero los datos de la carga anterior siguen ahí: se avisa que
            lo que se ve puede no estar al día, en vez de borrarlo. */}
        {datos && consulta.isError && (
          <div className="mb-5"><AvisoDeRecargaFallida motivo={bloqueo} /></div>
        )}
        {datos && <Resumen datos={datos} anio={anio} periodo={periodo} />}
      </div>
    </div>
  )
}

// ─── Cabeza de la pantalla ────────────────────────────────────────────────────
function Saludo({ yo }: { yo: PortalYo }) {
  const nombre = yo.usuario.nombre.trim()
  const empresas = yo.clientes.length
  return (
    <div>
      <p className="portal-rotulo">Resumen de tus compras</p>
      <h1 className="mt-2 text-3xl sm:text-4xl font-extrabold tracking-tight">
        {nombre ? `Hola, ${nombre}` : 'Hola'}
      </h1>
      {empresas === 1 && yo.clientes[0].nombre && (
        <p className="portal-suave mt-2 text-base font-semibold">{yo.clientes[0].nombre}</p>
      )}
      {empresas > 1 && (
        <p className="portal-suave mt-2 text-base">
          Este resumen suma los pedidos de tus {fmtCantidad(empresas)} empresas.
        </p>
      )}
    </div>
  )
}

// ─── Contenido ────────────────────────────────────────────────────────────────
function Resumen({ datos, anio, periodo }: ResumenProps) {
  if (datos.ordenes.total === 0) return <SinPedidos anio={anio} />
  const pedidosContados = contar(datos.ordenes.total, 'pedido más reciente', 'pedidos más recientes')
  return (
    <>
      {datos.truncado && (
        <div className="mb-5">
          <Aviso tono="info">
            {anio !== undefined
              ? `Este resumen considera solo tus ${pedidosContados} de ${anio}.`
              : `Mostramos tus ${pedidosContados}; elige un año para ver el resto.`}
          </Aviso>
        </div>
      )}
      <FichaDeAvance datos={datos} anio={anio} periodo={periodo} />
      <div className={datos.montos
        ? 'mt-6 grid gap-6 lg:items-start lg:grid-cols-[minmax(0,1.35fr)_minmax(0,1fr)]'
        : 'mt-6'}
      >
        <DondeVieneLoQueFalta estados={datos.pendientes_por_estado} items={datos.items} />
        {datos.montos && <Montos montos={datos.montos} />}
      </div>
      <Link to={rutaDePedidos(anio)} className="portal-boton mt-6 w-full sm:w-auto">
        Ver mis pedidos
        <ArrowRight className="w-4 h-4" aria-hidden="true" />
      </Link>
    </>
  )
}
