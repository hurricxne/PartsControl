// Las guías de despacho y las facturas de un pedido, con sus descargas. La usan
// OrdenPage.tsx (los documentos de la orden) e ItemPage.tsx (los de un ítem): recibe las
// listas ya filtradas por el backend y no pide datos.
//
// Las descargas consumen portalAPI.guiaPdf, guiaFirmada y facturaPdf a través de
// archivos.ts::descargar (el archivo viaja como Blob con el token de la sesión; no hay un
// enlace que se pueda abrir sin sesión).
//
// DECISIONES QUE NO SE VEN EN EL CÓDIGO
//   · Una factura acá es un DOCUMENTO: folio, fechas y montos. No hay saldo, estado de
//     pago ni «vencida»: el vencimiento se muestra como dato y no se compara con hoy.
//   · Perfil 'bodega' (`veMontos` false): sin sección de facturas y sin botones de
//     archivo, porque el PDF de la guía trae precios. El backend ya manda `pdf` y
//     `firmada` en false y las facturas vacías; el corte de acá es el mismo, por si acaso.
//   · El folio de una guía llega '' mientras el SII no lo asigna: se dice «sin folio
//     aún» en vez de mostrar un número que después cambia.
//   · «Qué viajó» nace abierto cuando algún ítem se recibió incompleto: plegado, el aviso
//     «recibidas 2 de 4» no lo vería nadie. Mientras la guía no esté firmada no se dice
//     nada de lo recibido (`cantidad_recibida` llega null: todavía nadie lo declaró).
//   · El botón que está bajando usa aria-disabled y no disabled: un botón deshabilitado
//     suelta el foco del teclado justo mientras se espera.
//   · Es una lista y no una tabla: a 320 px una tabla con montos no cabe, y así el mismo
//     marcado sirve en todos los anchos.
import { useId, useState, type ReactNode } from 'react'
import { AlertCircle, AlertTriangle, ChevronRight, Download, FileText, Loader2, Truck } from 'lucide-react'
import { portalAPI } from './api'
import type { FacturaPortal, GuiaPortal } from './types'
import { descargar, type Pedir } from './archivos'
import { TOLERANCIA_DE_CANTIDAD, contar, fmtCantidad, fmtFechaCorta } from './formato'
import type { Tono } from './estadosUi'
import { FormaDeTono, Monto } from './piezas'

const FALLA_SIN_MOTIVO = 'No pudimos descargar el archivo. Intenta de nuevo.'

interface DocumentosProps {
  guias: GuiaPortal[]
  facturas: FacturaPortal[]
  hoy: string
  // false (perfil 'bodega'): sin sección de facturas ni botones de archivo.
  veMontos: boolean
}

export default function Documentos({ guias, facturas, hoy, veMontos }: DocumentosProps) {
  const id = useId()
  const hayGuias = guias.length > 0
  const hayFacturas = veMontos && facturas.length > 0

  if (!hayGuias && !hayFacturas) {
    return (
      <section aria-labelledby={`${id}-vacio`}>
        <h2 id={`${id}-vacio`} className="text-xl font-bold tracking-tight">Documentos</h2>
        <p className="portal-suave mt-2">Aún no hay documentos para este pedido.</p>
      </section>
    )
  }

  return (
    <div className="space-y-10">
      <section aria-labelledby={`${id}-guias`}>
        <h2 id={`${id}-guias`} className="text-xl font-bold tracking-tight">Guías de despacho</h2>
        {hayGuias ? (
          <ul className="portal-tarjeta portal-docs-lista">
            {guias.map((guia) => <Guia key={guia.id} guia={guia} hoy={hoy} conArchivos={veMontos} />)}
          </ul>
        ) : (
          <p className="portal-suave mt-2">Aún no hay guías de despacho para este pedido.</p>
        )}
      </section>

      {veMontos && (
        <section aria-labelledby={`${id}-facturas`}>
          <h2 id={`${id}-facturas`} className="text-xl font-bold tracking-tight">Facturas</h2>
          {hayFacturas ? (
            <ul className="portal-tarjeta portal-docs-lista">
              {facturas.map((factura) => <Factura key={factura.id} factura={factura} hoy={hoy} />)}
            </ul>
          ) : (
            <p className="portal-suave mt-2">Aún no hay facturas para este pedido.</p>
          )}
          <p className="portal-suave mt-3 text-sm">
            Las facturas se muestran como documentos. Para saldos y pagos, contacta a tu ejecutivo.
          </p>
        </section>
      )}
    </div>
  )
}

// ─── Guía de despacho ─────────────────────────────────────────────────────────
function Guia({ guia, hoy, conArchivos }: { guia: GuiaPortal; hoy: string; conArchivos: boolean }) {
  const nombre = guia.folio ? `la guía ${guia.folio}` : 'la guía sin folio'
  const respaldo = guia.folio || String(guia.id)
  const conPdf = conArchivos && guia.pdf
  const conFirmada = conArchivos && guia.firmada
  // La fecha del documento; si no quedó registrada, el día en que salió el despacho.
  const fecha = guia.fecha ?? guia.despachada_el

  return (
    <li className="portal-docs-doc">
      <div className="min-w-0">
        <h3 className="portal-docs-titulo">
          <Truck aria-hidden="true" />
          {guia.folio
            ? <span>Guía <span className="portal-codigo">{guia.folio}</span></span>
            : 'Guía sin folio aún'}
        </h3>
        <p className="portal-suave mt-1 text-sm">
          {fecha ? <>Del <Dia iso={fecha} hoy={hoy} /></> : 'Sin fecha registrada'}
          {guia.despachada_el && guia.despachada_el !== fecha && (
            <> · despachada el <Dia iso={guia.despachada_el} hoy={hoy} /></>
          )}
        </p>
        <p className="mt-2"><Recepcion guia={guia} hoy={hoy} /></p>
        {guia.items.length > 0 && <QueViajo items={guia.items} />}
      </div>
      {(conPdf || conFirmada) && (
        <div className="portal-docs-acciones">
          {conPdf && (
            <BotonDeDescarga
              texto="Descargar guía (PDF)"
              deQue={nombre}
              pedir={() => portalAPI.guiaPdf(guia.id)}
              nombreBase={`guia-${respaldo}`}
            />
          )}
          {conFirmada && (
            <BotonDeDescarga
              texto="Descargar guía firmada"
              deQue={nombre}
              pedir={() => portalAPI.guiaFirmada(guia.id)}
              nombreBase={`guia-firmada-${respaldo}`}
            />
          )}
        </div>
      )}
    </li>
  )
}

// Cómo recibió el cliente la guía (`guia.recepcion`). Palabra + forma, como todo estado
// del portal. Un valor que esta pantalla no conoce se lee como «pendiente»: no afirma una
// recepción que nadie firmó.
const RECEPCION: Record<GuiaPortal['recepcion'], { texto: string; tono: Tono }> = {
  pendiente: { texto: 'Pendiente de recepción conforme', tono: 'pendiente' },
  conforme: { texto: 'Recibida conforme', tono: 'ok' },
  con_faltante: { texto: 'Recibida con faltante', tono: 'atencion' },
}

function Recepcion({ guia, hoy }: { guia: GuiaPortal; hoy: string }) {
  const { texto, tono } = RECEPCION[guia.recepcion] ?? RECEPCION.pendiente
  return (
    <span className="portal-insignia" data-tono={tono}>
      <FormaDeTono tono={tono} />
      <span>
        {texto}
        {tono !== 'pendiente' && guia.fecha_recepcion && <> el <Dia iso={guia.fecha_recepcion} hoy={hoy} /></>}
      </span>
    </span>
  )
}

type ItemDeGuia = GuiaPortal['items'][number]

/** El cliente firmó menos de lo que viajó. Sin firma (`cantidad_recibida` null) no se sabe. */
function recibioMenos(it: ItemDeGuia): it is ItemDeGuia & { cantidad_recibida: number } {
  return it.cantidad_recibida !== null && it.cantidad_recibida < it.cantidad - TOLERANCIA_DE_CANTIDAD
}

function QueViajo({ items }: { items: GuiaPortal['items'] }) {
  const hayDiferencias = items.some(recibioMenos)
  return (
    <details className="portal-docs-plegable" open={hayDiferencias}>
      <summary>
        <ChevronRight className="portal-docs-flecha" aria-hidden="true" />
        Qué viajó
        <span className="portal-suave font-normal">· {contar(items.length, 'ítem', 'ítems')}</span>
      </summary>
      <ul className="portal-docs-items">
        {items.map((it, i) => (
          // El mismo repuesto puede venir dos veces en una guía: la posición desempata.
          <li key={`${i}-${it.numero_parte}`}>
            <span className="portal-docs-item-parte">
              {it.numero_parte
                ? <span className="portal-parte">{it.numero_parte}</span>
                : <span className="portal-suave">Sin N° de parte</span>}
            </span>
            <span className="portal-docs-item-descripcion">{it.descripcion}</span>
            <span className="portal-docs-item-cantidad">
              <span className="portal-versalita">Cant.</span>{' '}
              <span className="portal-cifra">{fmtCantidad(it.cantidad)}</span>
            </span>
            {recibioMenos(it) && (
              <strong className="portal-docs-alerta">
                <AlertTriangle aria-hidden="true" />
                recibidas {fmtCantidad(it.cantidad_recibida)} de {fmtCantidad(it.cantidad)}
              </strong>
            )}
          </li>
        ))}
      </ul>
    </details>
  )
}

// ─── Factura ──────────────────────────────────────────────────────────────────
function Factura({ factura, hoy }: { factura: FacturaPortal; hoy: string }) {
  return (
    <li className="portal-docs-doc">
      <div className="min-w-0">
        <h3 className="portal-docs-titulo">
          <FileText aria-hidden="true" />
          <span>Factura <span className="portal-codigo">{factura.folio || 'sin folio'}</span></span>
          {factura.es_anticipo && <span className="portal-docs-marca">Anticipo</span>}
        </h3>
        <p className="portal-suave mt-1 text-sm">
          {factura.fecha ? <>Del <Dia iso={factura.fecha} hoy={hoy} /></> : 'Sin fecha registrada'}
          {factura.vencimiento && <> · vencimiento <Dia iso={factura.vencimiento} hoy={hoy} /></>}
        </p>
        <dl className="portal-docs-montos">
          <MontoRotulado rotulo="Neto"><Monto valor={factura.neto_clp} /></MontoRotulado>
          <MontoRotulado rotulo="IVA"><Monto valor={factura.iva_clp} /></MontoRotulado>
          <MontoRotulado rotulo="Total con IVA"><Monto valor={factura.total_clp} className="font-bold" /></MontoRotulado>
        </dl>
      </div>
      {factura.pdf && (
        <div className="portal-docs-acciones">
          <BotonDeDescarga
            texto="Descargar factura (PDF)"
            deQue={`la factura ${factura.folio || 'sin folio'}`}
            pedir={() => portalAPI.facturaPdf(factura.id)}
            nombreBase={`factura-${factura.folio || factura.id}`}
          />
        </div>
      )}
    </li>
  )
}

function MontoRotulado({ rotulo, children }: { rotulo: string; children: ReactNode }) {
  return (
    <div>
      <dt className="portal-versalita">{rotulo}</dt>
      <dd>{children}</dd>
    </div>
  )
}

// ─── Piezas comunes ───────────────────────────────────────────────────────────
/** Un día 'AAAA-MM-DD' como «12 ago» (con el año si no es el de `hoy`). */
function Dia({ iso, hoy }: { iso: string; hoy: string }) {
  return <time dateTime={iso}>{fmtFechaCorta(iso, hoy)}</time>
}

interface BotonDeDescargaProps {
  texto: string
  /** Para lectores de pantalla: de qué documento es («la guía 1284»). */
  deQue: string
  pedir: Pedir
  nombreBase: string
}

function BotonDeDescarga({ texto, deQue, pedir, nombreBase }: BotonDeDescargaProps) {
  const [bajando, setBajando] = useState(false)
  const [falla, setFalla] = useState<string | null>(null)
  // Número de intento fallido: es la `key` del mensaje. Dos fallas con el mismo texto no
  // cambian el nodo y un lector de pantalla no repite el aviso; con la key se vuelve a
  // insertar y se vuelve a leer (mismo recurso que CambiarClaveForm.tsx).
  const [intento, setIntento] = useState(0)

  const bajar = async () => {
    if (bajando) return
    setBajando(true)
    setFalla(null)
    try {
      await descargar(pedir, nombreBase)
    } catch (err) {
      // archivos.ts::descargar lanza un Error cuyo mensaje ya está escrito para el cliente.
      setFalla(err instanceof Error && err.message ? err.message : FALLA_SIN_MOTIVO)
      setIntento((n) => n + 1)
    } finally {
      setBajando(false)
    }
  }

  return (
    <div className="portal-docs-descarga">
      <button
        type="button"
        className="portal-boton portal-boton-secundario portal-boton-compacto"
        aria-disabled={bajando}
        onClick={bajar}
      >
        {bajando
          ? <Loader2 className="w-4 h-4 portal-giro" aria-hidden="true" />
          : <Download className="w-4 h-4" aria-hidden="true" />}
        {bajando ? 'Descargando…' : texto}
        <span className="sr-only"> de {deQue}</span>
      </button>
      {falla && (
        <p key={intento} role="alert" className="portal-error">
          <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" aria-hidden="true" />
          <span>{falla}</span>
        </p>
      )}
    </div>
  )
}
