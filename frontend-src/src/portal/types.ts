// Tipos del JSON del backend del Portal de Clientes (espejo de backend/portal/router.py,
// router_ordenes.py, router_admin.py y router_admin_fotos.py). Dos mundos que NO se mezclan:
//   · /api/portal/*        lo que ve el CLIENTE (cuenta tipo_acceso='portal').
//   · /api/portal-admin/*  lo que opera el PERSONAL de Grupo AM (Cuentas del portal y las
//                          fotos que se publican desde Despachos).
//
// TRES CONVENCIONES DE TODO LO QUE VE EL CLIENTE
//   · Fechas: texto 'AAAA-MM-DD', ya en día de Chile. Se pintan con portal/formato.ts;
//     jamás con `new Date(texto)` (en Chile lo corre al día anterior).
//   · Montos: pesos chilenos NETOS salvo que el nombre diga otra cosa. `null` significa
//     «esta cuenta no ve montos» o «esta orden no tiene precio congelado»: se pinta un
//     guion, nunca $0.
//   · `vencida`, `hoy` y los estados los calcula el backend con el día de Chile: la
//     pantalla no compara fechas con el reloj del dispositivo.

// 'bodega' existe para el bodeguero de faena: ve qué viene en camino y NO ve montos.
export type PerfilPortal = 'procurement' | 'bodega'

// 'vencida' no es un estado guardado: el backend lo calcula (sin uso por más de
// `dias_inactividad_corte` días). La cuenta sigue activa en la base, pero no entra.
export type EstadoCuenta = 'activa' | 'vencida' | 'desactivada'

// ─── Lado cliente ─────────────────────────────────────────────────────────────
export interface ClienteDelPortal {
  id: number
  nombre: string
  rut: string
}

/** GET /portal/yo */
export interface PortalYo {
  usuario: { nombre: string; email: string }
  perfil: PerfilPortal
  clientes: ClienteDelPortal[]
  // La cuenta sigue con la clave TEMPORAL que le entregó su ejecutivo. Esa clave vence
  // (el login la rechaza pasado su plazo), así que la página pide cambiarla.
  debe_cambiar_clave: boolean
}

// ─── Lado cliente: pedidos ────────────────────────────────────────────────────
// El recorrido de un ítem, en orden (backend/portal/estados.py), y dos INCIDENCIAS que
// están fuera del recorrido:
//   · 'en_gestion'   el ítem no llegó o llegó mal y se está reponiendo. Se sigue esperando.
//   · 'no_recibido'  unidades que salieron en una guía y que el cliente, al firmarla,
//                    declaró no haber recibido. No se reponen por este pedido: no cuentan
//                    como entregadas ni como «por entregar».
export type ClaveEstado =
  | 'confirmado' | 'en_compra' | 'listo_origen' | 'en_transito' | 'en_aduana'
  | 'en_recepcion' | 'en_bodega' | 'despachado' | 'facturado' | 'en_gestion' | 'no_recibido'

/** La etiqueta viene del backend: la pantalla la pinta tal cual y decide forma/tono por `clave`. */
export interface EstadoCliente {
  clave: ClaveEstado
  etiqueta: string
}

export interface MontoLinea {
  // 2 decimales. null = las unidades del ítem tienen más de un precio (por ejemplo, dos
  // tandas facturadas a precios distintos): no hay UN unitario que mostrar; el total sí.
  unitario_clp: number | null
  total_clp: number // entero: lo facturado a su precio + el resto al precio del pedido
}

export interface TotalesOrden {
  neto_clp: number
  iva_clp: number
  total_clp: number
}

/** Un ítem (renglón) de la orden, como lo ve el cliente. */
export interface ItemDeOrden {
  id: number            // llave de la URL del seguimiento
  linea: number | null  // posición en la orden de compra
  numero_parte: string
  descripcion: string
  marca: string
  cantidad: number
  estado: EstadoCliente
  entregado: boolean
  // Solo cuando el ítem va repartido en más de un estado («6 despachadas, 4 en bodega»).
  // Vacío = todo el ítem está en `estado`.
  partes: { cantidad: number; estado: EstadoCliente }[]
  fecha_prometida: string | null // la fecha COMPROMETIDA al cierre; no es una estimación
  vencida: boolean               // todavía se espera algo y la fecha comprometida ya pasó
  // Tres cantidades: lo pedido (`cantidad`), lo que salió y no se declaró como no
  // recibido, y lo que todavía se espera bajo este pedido. Lo que falta para cuadrar las
  // tres es lo «no recibido en la entrega».
  cantidad_entregada: number
  cantidad_por_entregar: number
  monto: MontoLinea | null
  fotos: number                  // cuántas fotos tiene para ver
}

/** Por qué una orden salió en la búsqueda. */
export interface Coincidencia {
  tipo: 'item' | 'guia' | 'factura'
  id: number      // del ítem, del despacho o de la factura
  texto: string   // N° de parte o folio
  detalle: string // descripción del ítem; vacío en documentos
}

/** Cabecera de una orden: igual en el listado, en el detalle y en el tablero. */
export interface OrdenResumen {
  id: number
  numero_oc: string
  empresa: string | null       // a cuál de sus empresas pertenece; null si la cuenta tiene una sola
  fecha_oc: string | null      // fecha de la OC del cliente
  fecha_ingreso: string | null // día en que se cerró la venta
  estado: 'abierta' | 'entregada'
  // `por_entregar`: ítems de los que todavía se espera algo. Un ítem al que solo le queda
  // algo «no recibido en la entrega» no está entregado ni por entregar.
  items: { total: number; entregados: number; por_entregar: number }
  avance_pct: number           // 0-100; 100 solo si no queda nada pendiente
  // La fecha comprometida MÁS ANTIGUA entre lo que se espera. Si hay atrasados, ya pasó:
  // no es «la próxima que todavía no vence». null si no se espera nada con fecha.
  proxima_fecha: string | null
  fecha_completa: string | null // la más lejana: cuándo quedaría completa la orden
  atrasados: number
  totales: TotalesOrden | null
  coincidencias?: Coincidencia[] // solo cuando se buscó algo
}

/** Lo que acompaña a toda respuesta de datos del cliente. */
export interface Sello {
  actualizado_at: string // cuándo se armó la respuesta (UTC, con Z)
  hoy: string            // el día de hoy en Chile: base de «en 3 días» / «hace 2 días»
}

/** GET /portal/ordenes */
export interface OrdenesResp extends Sello {
  total: number
  page: number
  page_size: number
  items: OrdenResumen[]
  q_efectivo: string // las palabras que el servidor buscó de verdad ('' = no filtró)
}

export interface OrdenesParams {
  q?: string
  estado?: 'todas' | 'abiertas' | 'entregadas'
  anio?: number
  page?: number
  page_size?: number
}

/** Guía de despacho. Los archivos se bajan con portalAPI.guiaPdf / guiaFirmada. */
export interface GuiaPortal {
  id: number
  orden_id: number
  folio: string              // '' mientras el SII no asigne folio
  fecha: string | null       // fecha del documento
  despachada_el: string | null
  // Cómo la recibió el cliente: 'pendiente' = todavía no hay guía firmada; 'conforme' =
  // firmó todo lo que viajó; 'con_faltante' = firmó menos en alguna línea.
  recepcion: 'pendiente' | 'conforme' | 'con_faltante'
  fecha_recepcion: string | null // el día de la firma; null mientras esté pendiente
  // `cantidad_recibida` es null mientras la guía no esté firmada: nadie ha dicho todavía
  // qué recibió. No compararla con `cantidad` sin mirar antes que no sea null.
  items: { numero_parte: string; descripcion: string; cantidad: number; cantidad_recibida: number | null }[]
  pdf: boolean     // hay PDF de la guía electrónica para bajar
  firmada: boolean // hay copia firmada escaneada para bajar
}

/** Factura como DOCUMENTO: no hay saldo ni estado de pago, a propósito. */
export interface FacturaPortal {
  id: number
  orden_id: number
  folio: string
  fecha: string | null
  vencimiento: string | null
  neto_clp: number
  iva_clp: number
  total_clp: number
  es_anticipo: boolean
  pdf: boolean
}

/** GET /portal/ordenes/{id} */
export interface OrdenDetalle extends Sello {
  orden: OrdenResumen & { ejecutivo: { nombre: string; email: string } | null }
  items: ItemDeOrden[] // pendientes primero (lo más atrasado arriba), entregados al final
  guias: GuiaPortal[]
  facturas: FacturaPortal[] // vacío para el perfil 'bodega'
}

/** Un paso del recorrido de un ítem. Un hito futuro NUNCA trae fecha. */
export interface Hito {
  clave: ClaveEstado
  etiqueta: string
  cumplido: boolean
  actual: boolean
  fecha: string | null   // el día en que ocurrió, si quedó registrado
  detalle: string | null // p. ej. «Guía 1284»
}

export interface FotoPortal {
  id: number // la imagen se pide con portalAPI.foto(id)
  ancho: number
  alto: number
  fecha: string | null
}

/** GET /portal/ordenes/{ordenId}/items/{itemId} */
export interface SeguimientoItem extends Sello {
  orden: { id: number; numero_oc: string }
  item: ItemDeOrden
  hitos: Hito[]
  guias: GuiaPortal[]
  facturas: FacturaPortal[]
  fotos: FotoPortal[]
}

/** GET /portal/resumen */
export interface Tablero extends Sello {
  anio: number | null
  anios: number[]     // años con órdenes, del más reciente al más antiguo
  truncado: boolean   // hay más órdenes que las que entran en el resumen: acotar por año
  ordenes: { total: number; abiertas: number; entregadas: number }
  // `pendientes` = no entregados (incluye los que solo tienen algo «no recibido en la
  // entrega»); `por_entregar` = de los que todavía se espera algo.
  items: { total: number; entregados: number; pendientes: number; por_entregar: number; atrasados: number }
  pendientes_por_estado: { clave: ClaveEstado; etiqueta: string; items: number }[]
  proxima_fecha: string | null
  montos: {
    comprado_neto_clp: number
    por_entregar_neto_clp: number
    facturado_neto_clp: number
    ordenes_sin_monto: number // órdenes sin precio congelado: no entran en las sumas
  } | null
}

// ─── Lado personal (portal-admin) ─────────────────────────────────────────────
/** Una foto publicada para el cliente (pantalla Despachos). */
export interface FotoPublicada {
  id: number
  ancho: number
  alto: number
  peso_bytes: number
  fecha: string | null
}

/** GET /portal-admin/items/{itemId}/fotos */
export interface FotosPublicadas {
  maximo: number
  fotos: FotoPublicada[]
}

/** GET /portal-admin/clientes — ficha de cliente con cuántas ventas y cuentas tiene. */
export interface ClienteAdmin {
  id: number
  rut: string
  nombre: string
  ventas_vinculadas: number
  cuentas: number
}

export interface ClienteDeCuenta {
  id: number
  nombre: string
  perfil: PerfilPortal
}

/**
 * El plazo de la clave TEMPORAL de una cuenta. Viaja en el listado y en toda respuesta
 * que entrega o toca una clave. ISO en UTC sin zona, como las otras fechas; null/false =
 * el contacto ya puso su clave. Una cuenta 'activa' con la clave temporal vencida NO
 * puede entrar (el login la rechaza): `vencida` la decide el backend, no el reloj del
 * navegador.
 */
export interface PlazoDeClave {
  clave_temporal_vence_at: string | null
  clave_temporal_vencida: boolean
}

export interface CuentaPortal extends PlazoDeClave {
  id: number
  email: string
  nombre: string
  estado: EstadoCuenta
  clientes: ClienteDeCuenta[]
  // ISO en UTC SIN zona (datetime.utcnow del backend). Ver fmtFechaHoraChile.
  ultimo_acceso_at: string | null
  vigente_desde: string | null
}

/** GET /portal-admin/cuentas */
export interface CuentasResp {
  dias_inactividad_corte: number
  cuentas: CuentaPortal[]
}

/** POST /portal-admin/cuentas (201). `clave_temporal` viaja UNA sola vez. */
export interface CuentaCreada extends PlazoDeClave {
  id: number
  email: string
  clave_temporal: string
}

/** POST /portal-admin/cuentas/{id}/restablecer-clave */
export interface ClaveRestablecida extends PlazoDeClave {
  id: number
  clave_temporal: string
}

/** POST /portal-admin/cuentas/{id}/reactivar | /desactivar */
export interface EstadoCambiado {
  id: number
  estado: EstadoCuenta
}

/**
 * POST /portal-admin/cuentas/{id}/reactivar. Reactivar NO renueva la clave temporal: si
 * ya venció, la cuenta queda activa pero no entra hasta que se restablezca la clave.
 */
export interface CuentaReactivada extends EstadoCambiado, PlazoDeClave {}

/** Venta cerrada que el cliente NO va a ver en su portal porque no calzó con una ficha. */
export interface VentaHuerfana {
  cotizacion_id: number
  numero: string
  cliente: string
  rut_cliente: string
  motivo: string
}

/** Un mismo RUT en más de una ficha de cliente: no se puede elegir a cuál vincular. */
export interface RutAmbiguo {
  rut: string
  cliente_ids: number[]
}

/** GET /portal-admin/vinculacion (ensayo, aplicado=false) y POST …/aplicar (aplicado=true). */
export interface Vinculacion {
  aplicado: boolean
  ya_vinculadas: number
  vinculadas_ahora: number
  // Ventas cuyo RUT dejó de calzar con su ficha (se editó el RUT de la ficha, o quedó
  // ambigua): hoy siguen vinculadas y al aplicar quedan SIN ficha. Es una corrección,
  // no un error: el cliente dejaría de ver una venta que no es suya.
  desvinculadas_ahora: number
  huerfanas: VentaHuerfana[]
  ruts_ambiguos: RutAmbiguo[]
}
