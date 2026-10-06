// Una orden de compra del cliente (ruta /portal/pedidos/:ordenId). Responde «¿qué me
// falta de esta orden y cuándo llega?» con cuatro bloques: el título con el N° de OC, la
// ficha de cabecera (FichaDeOrden.tsx), los ítems (ItemsDeOrden.tsx) y las guías y
// facturas (Documentos.tsx).
//
// Consume GET /api/portal/ordenes/{id} (portalAPI.orden → OrdenDetalle). UNA consulta
// alimenta toda la pantalla: los bloques reciben sus datos por props y no piden nada.
//
// DECISIONES QUE NO SE VEN EN EL CÓDIGO
//   · La llave de la URL es el id de la orden. El N° de OC es texto libre (puede venir
//     vacío o repetirse entre empresas): se muestra, no se usa para buscar.
//   · Un :ordenId que no es un número no se consulta. El backend respondería 422 y para
//     el cliente es lo mismo que un 404: «No encontramos este pedido».
//   · Qué se pinta lo decide si HAY datos, no el estado de la consulta. Cuando un
//     «Recargar» falla, react-query marca error pero conserva la última respuesta: se
//     sigue mostrando, con un aviso. La excepción es el 404 (la orden dejó de ser de
//     esta cuenta): ahí lo guardado ya no se muestra.
//   · El <h1> existe en todos los estados (cargando, error, con datos). Por eso
//     ErrorDeCarga va con su nivel por defecto (h2); solo NoEncontrado lo reemplaza.
//   · No se pasa `retry` a la consulta: la regla de reintentos del portal la pone
//     sesion.ts::prepararConsultasDelPortal. El 401 lo resuelve services/authErrors.ts.
import { useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { portalAPI, estadoHttp, motivoDeBloqueo } from './api'
import type { OrdenDetalle } from './types'
import { claveDeConsulta, usePortal, useTituloDePestana } from './sesion'
import { Actualizado, Aviso, AvisoDeRecargaFallida, Cargando, ErrorDeCarga, Migas, NoEncontrado, SinConexion } from './piezas'
import { RUTA_DE_PEDIDOS, idDeRuta } from './rutas'
import FichaDeOrden from './FichaDeOrden'
import ItemsDeOrden from './ItemsDeOrden'
import Documentos from './Documentos'

function tituloDePestana(detalle: OrdenDetalle | undefined, noEncontrada: boolean): string {
  if (noEncontrada) return 'Pedido no encontrado'
  if (!detalle) return 'Pedido'
  const numero = detalle.orden.numero_oc.trim()
  return numero ? `OC ${numero}` : 'Orden de compra sin número'
}

export default function OrdenPage() {
  const { ordenId } = useParams<{ ordenId: string }>()
  const id = idDeRuta(ordenId)
  if (id === null) return <PedidoNoEncontrado />
  // `key`: al pasar de una orden a otra la pantalla se arma de nuevo, y el filtro de
  // ítems escrito para la anterior no se hereda.
  return <Orden key={id} ordenId={id} />
}

/** La dirección no corresponde a ninguna orden posible: ni siquiera se consulta. */
function PedidoNoEncontrado() {
  useTituloDePestana(tituloDePestana(undefined, true))
  return (
    <NoEncontrado titulo="No encontramos este pedido" volverA={RUTA_DE_PEDIDOS} volverTexto="Volver a Mis pedidos" />
  )
}

function Orden({ ordenId }: { ordenId: number }) {
  const { usuarioId, veMontos } = usePortal()
  const consulta = useQuery({
    queryKey: claveDeConsulta(usuarioId, 'orden', ordenId),
    queryFn: async () => (await portalAPI.orden(ordenId)).data,
  })
  const detalle = consulta.data
  // 404 = «no existe o no es tuya»: el backend no distingue, a propósito, y acá tampoco.
  const noEncontrada = consulta.isError && estadoHttp(consulta.error) === 404
  useTituloDePestana(tituloDePestana(detalle, noEncontrada))

  if (noEncontrada) {
    return (
      <NoEncontrado titulo="No encontramos este pedido" volverA={RUTA_DE_PEDIDOS} volverTexto="Volver a Mis pedidos" />
    )
  }

  const recargar = () => { void consulta.refetch() }
  const numero = detalle?.orden.numero_oc.trim() ?? ''
  const pasoActual = !detalle ? 'Pedido' : numero ? `OC ${numero}` : 'Orden sin número'
  const bloqueo = consulta.isError ? motivoDeBloqueo(consulta.error) : null

  return (
    <div className="portal-entrada">
      <Migas pasos={[{ texto: 'Mis pedidos', a: RUTA_DE_PEDIDOS }, { texto: pasoActual }]} />

      <header className="portal-orden-titulo">
        <div className="min-w-0">
          {detalle?.orden.empresa && <p className="portal-rotulo mb-1">{detalle.orden.empresa}</p>}
          <h1 className="text-2xl sm:text-3xl font-extrabold tracking-tight">
            Orden de compra
            {detalle && (numero ? <> <span className="portal-orden-numero">{numero}</span></> : ' sin número')}
          </h1>
        </div>
        {detalle && <Actualizado sello={detalle} onRecargar={recargar} cargando={consulta.isFetching} />}
      </header>

      {!detalle && (
        <div className="mt-6">
          {consulta.isPending && (
            consulta.fetchStatus === 'paused'
              ? <SinConexion texto="El pedido se cargará solo cuando vuelva la señal." />
              : <Cargando texto="Cargando el pedido…" />
          )}
          {consulta.isError && (
            bloqueo
              ? <Aviso tono="error">{bloqueo}</Aviso>
              : <ErrorDeCarga titulo="No pudimos cargar este pedido" onReintentar={recargar} error={consulta.error} />
          )}
        </div>
      )}

      {detalle && (
        <>
          {consulta.isRefetchError && (
            <div className="mt-4">
              <AvisoDeRecargaFallida motivo={bloqueo} />
            </div>
          )}
          <FichaDeOrden
            orden={detalle.orden}
            items={detalle.items}
            hoy={detalle.hoy}
            veMontos={veMontos}
            hayFacturas={detalle.facturas.length > 0}
          />
          <ItemsDeOrden ordenId={detalle.orden.id} items={detalle.items} hoy={detalle.hoy} veMontos={veMontos} />
          <div className="mt-10">
            <Documentos guias={detalle.guias} facturas={detalle.facturas} hoy={detalle.hoy} veMontos={veMontos} />
          </div>
        </>
      )}
    </div>
  )
}
