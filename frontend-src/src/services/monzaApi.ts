import axios from "axios";
import type {
  EmbarquePricingRow, PricingDetail, PricingSavePayload,
} from "../monza-embarques-pricing/types";

// `timeout`: sin él, una petición que nunca vuelve deja la pantalla bajo el velo de
// carga para siempre, sin cartel ni botón de reintento — el vendedor concluye que el
// sistema se colgó. 60 s es holgado para cualquier listado; las operaciones que
// legítimamente tardan más (emisión al SII, informes) piden `{ timeout: 0 }` en su
// propia llamada.
const api = axios.create({ baseURL: "/api/monza", timeout: 60_000 });

api.interceptors.request.use((cfg) => {
  try {
    const raw = localStorage.getItem("machparts-auth");
    const token = raw ? JSON.parse(raw)?.state?.token : null;
    if (token) cfg.headers.Authorization = `Bearer ${token}`;
  } catch {}
  return cfg;
});

// La sesión vencida (401) tiene que llevar al login, no confundirse con un problema de
// red: sin esto la pantalla de Leads mostraba «Revisa tu conexión y reintenta» y el botón
// Reintentar repetía la misma petición para siempre, porque el token seguía vencido.
// Es el mismo interceptor que ya tiene el cliente de MachParts (services/api.ts).
api.interceptors.response.use(
  (res) => res,
  (err) => {
    if (err.response?.status === 401) {
      try { localStorage.removeItem("machparts-auth"); } catch { /* modo privado */ }
      window.location.href = "/login";
    }
    return Promise.reject(err);
  },
);

// ── Config ──────────────────────────────────────────────────────────────────
export const monzaConfigAPI = {
  get: () => api.get("/config"),
  update: (data: Record<string, unknown>) => api.put("/config", data),
};

// Payload de una factura de cliente (POST /contabilidad/facturas). Es EL MISMO que
// consumen el preview y la emisión electrónica: el backend de Wasabil importa el
// schema FacturaCreate de Contabilidad, así que el contrato es uno solo.
// Se declara como `type` (no `interface`) a propósito: así conserva la firma de
// índice implícita y sigue siendo asignable a los `Record<string, unknown>` que ya
// usan otros modales — extender el payload no rompe ninguna llamada existente.
export type MonzaFacturaPayload = {
  cotizacion_id: number;
  // Modos EXCLUYENTES de una factura normal: desde una guía despachada
  // (despacho_id) o retiro en oficina (sin_guia).
  despacho_id?: number;
  sin_guia?: boolean;
  // Vía manual: folio del DTE ya emitido. En la vía SII NO viaja (lo asigna el SII
  // al emitir y el backend rechaza el payload si viene con folio).
  numero_factura?: string;
  tipo_doc?: string;
  fecha_emision?: string;
  condicion_pago?: string;
  plazo_dias?: number;
  observaciones?: string;
  // ── Fase 7 · factura de ANTICIPO (vía B) ──────────────────────────────────
  // Respalda ante el SII un adelanto que el cliente pagó ANTES del despacho: es la
  // ÚNICA factura que no nace de una guía firmada. `monto_neto_anticipo` es el NETO
  // (el IVA lo calcula el backend con la tasa CONGELADA de la venta, nunca 19% fijo).
  // A diferencia de Grupo AM aquí NO van rut_cliente / razon_social_cliente: el
  // receptor sale de la venta y, si falta un dato, el backend manda a completarlo ahí.
  es_anticipo?: boolean;
  monto_neto_anticipo?: number;
  descripcion_anticipo?: string;
  // Puerta EXPLÍCITA al SEGUNDO anticipo de una misma venta: en Monza el adelanto es
  // uno por venta, así que el backend rechaza con 409 (nombrando el anticipo que ya
  // existe) salvo que venga esta marca. Solo viaja cuando el usuario la confirma en el
  // modal — omitirla es lo normal y deja el bloqueo puesto.
  confirmar_segundo_anticipo?: boolean;
};

// Respuesta de POST /contabilidad/facturas: la factura serializada MÁS las
// `advertencias` que el backend acumula (p. ej. "no se pudo mover el adelanto a esta
// factura"). Antes esas advertencias solo se veían por la vía SII: por la vía manual la
// respuesta salía 200 en silencio. Se declara solo lo que la pantalla lee.
export interface MonzaFacturaCreada {
  id: number;
  numero_factura?: string | null;
  advertencias?: string[];
}

// ── Preview de la factura de la vía MANUAL (POST /contabilidad/facturas/preview) ──
// El backend deriva las líneas y CALCULA los montos (descuento de anticipo incluido),
// así que sin preview el operador registraba a ciegas una factura que YA emitió ante el
// SII: si los números no cuadraban con el papel se descubría con el folio consumido.
// No persiste ni congela nada, y sale de las MISMAS funciones que valida el POST.
/** Una línea del documento. Las líneas NEGATIVAS de descuento por anticipo vienen al
 *  FINAL, con `numero_parte: 'DESCUENTO'` y `anticipo_factura_id` puesto; van sin
 *  `item_cotizacion_id` ni `despacho_item_id` a propósito (si los llevaran contarían
 *  como mercadería facturada y romperían el tope físico). */
export interface MonzaFacturaPreviewLinea {
  item_cotizacion_id: number | null;
  despacho_item_id: number | null;
  numero_parte: string | null;
  descripcion: string | null;
  cantidad: number;
  precio_unit_neto: number;
  total_neto: number;
  anticipo_factura_id?: number;
}
/** Receptor del DTE tal como quedará. `null` en cualquiera de los dos = la ficha del
 *  cliente está incompleta, y entonces viene el problema correspondiente en `problemas`. */
export interface MonzaFacturaPreviewReceptor {
  razon_social: string | null;
  rut: string | null;
}
export interface MonzaFacturaPreview {
  /** false ⇒ hay `problemas`: el botón de registrar/emitir NO debe habilitarse. OJO: el
   *  folio NO entra en esta decisión (se valida al registrar, donde el operador lo tiene
   *  en la mano) — misma regla que Grupo AM. */
  puede_emitir: boolean;
  /** Reglas incumplidas, TODAS juntas (el preview llama con acumular=True). */
  problemas: string[];
  /** Avisos no bloqueantes que igual cambian lo que el usuario cree que pasó
   *  (p. ej. "el descuento por anticipo deja esta factura en $0"). */
  advertencias: string[];
  receptor: MonzaFacturaPreviewReceptor;
  lineas: MonzaFacturaPreviewLinea[];
  /** `iva_rate` es la tasa CONGELADA de la venta como FRACCIÓN (0.19 = 19%): se pinta
   *  desde acá, jamás con un 19% escrito a mano. Mismo contrato que el preview del SII. */
  totales: { neto: number; iva: number; bruto: number; iva_rate?: number | null };
  /** Anticipos que ESTA factura descuenta (vía B). Vacío si no hay ninguno. */
  descuentos: MonzaDescuentoAnticipo[];
  es_anticipo: boolean;
  sin_guia: boolean;
  /** Eco de la guía que quedará registrada en la factura (derivada del modo). */
  guia: { despacho_id: number | null; numero_guia: string | null };
}

// Verificación del adelanto por Contabilidad (espejo de AdelantoVerificarIn): el monto
// REALMENTE recibido, no el esperado.
export interface MonzaAdelantoVerificarPayload {
  monto: number;
  fecha_pago?: string;
  banco?: string;
  numero_operacion?: string;
  observaciones?: string;
}
/** Adelanto vivo de la venta. `estado` nunca llega 'anulado' acá: un adelanto anulado se
 *  sirve como `adelanto: null` y la venta vuelve a 'por_verificar'. */
export interface MonzaAdelantoDetalle {
  id: number;
  estado: string;
  factura_anticipo_folio: string | null;
  monto: number;
  monto_aplicado: number;
  fecha_pago: string | null;
  banco: string | null;
  numero_operacion: string | null;
  observaciones: string | null;
  fecha_verificacion: string | null;
}
/** Estado del adelanto de una venta — el contrato ÚNICO que devuelven verificar y anular
 *  (la pantalla lee uno solo). `adelanto_anulado` solo viene en la respuesta de anular:
 *  es la traza del registro que se marcó 'anulado' y que `adelanto` ya no publica. */
export interface MonzaEstadoAdelanto {
  requiere_adelanto: boolean;
  pct_adelanto: number;
  estado_adelanto: string;
  factura_anticipo_folio: string | null;
  adelanto: MonzaAdelantoDetalle | null;
  adelanto_anulado?: { id: number; estado: string; monto: number; monto_aplicado: number };
}

// Pago real del cliente (espejo de CobranzaIn). Las cobranzas de factoring NO van por
// aquí: las genera el panel de factoring.
export interface MonzaCobranzaPayload {
  fecha?: string;
  monto: number;
  /** transferencia | cheque | efectivo */
  medio?: string;
  banco?: string;
  numero_operacion?: string;
  observaciones?: string;
}
// Cesión de la factura al factor (espejo de FactoringIn). Si `retencion` no viaja, el
// backend la deriva = cupo − adelantado.
export interface MonzaFactoringPayload {
  empresa_factoring?: string;
  id_operacion?: string;
  fecha_operacion?: string;
  monto_adelantado?: number;
  costo_factoring?: number;
  retencion?: number;
  banco?: string;
  observaciones?: string;
}
// El payload MonzaGuiaFirmadaPayload y monzaContabilidadAPI.marcarGuiaFirmada se
// ELIMINARON (2026-08-06): la firma dejó de ser un toggle informativo de Contabilidad
// y ahora GATEA la facturación. Se marca en Despachos — monzaDespachosAPI.firmarEntidad
// (multipart: foto/PDF + fecha de la firma) — y Contabilidad solo la lee.

// ── Contabilidad (Ventas + Facturas/Cobranzas/Factoring) — SOLO MonzaParts ────
export const monzaContabilidadAPI = {
  // Ventas (agrupado por cotización vendida/despachada)
  listVentas: (q?: string, periodo?: string) =>
    api.get("/contabilidad/ventas", { params: { q: q || undefined, periodo: periodo || undefined } }),
  ventaDetalle: (cotId: number) => api.get(`/contabilidad/ventas/${cotId}`),
  despachosFacturables: (cotId: number) => api.get(`/contabilidad/ventas/${cotId}/despachos-facturables`),
  // Adelanto (50%): Contabilidad verifica el pago informado por Comercial
  verificarAdelanto: (cotId: number, data: MonzaAdelantoVerificarPayload) =>
    api.post<MonzaEstadoAdelanto>(`/contabilidad/ventas/${cotId}/adelanto/verificar`, data),
  // ANULA un adelanto que no prosperó (el cliente nunca depositó, o se verificó por
  // error): lo marca 'anulado' —no lo borra, queda la traza— y CIERRA de nuevo el
  // cortafuego de Abastecimiento (adelanto_verificado = 0), que solo frena la OC al
  // proveedor mientras ese flag esté en 0. Es IDEMPOTENTE: re-anular responde el estado.
  // El backend rechaza con 409 si el adelanto ya se aplicó a una factura (revierta esa
  // cobranza primero) o si está conciliado con un abono del banco (desconcílielo en
  // Tesorería). Sin esto, un adelanto verificado por error quedaba PEGADO y Abastecimiento
  // seguía comprando contra un 50% inexistente.
  anularAdelanto: (adelantoId: number) =>
    api.post<MonzaEstadoAdelanto>(`/contabilidad/adelantos/${adelantoId}/anular`),
  // Facturas / cuentas por cobrar
  listFacturas: (estado?: string, q?: string) =>
    api.get("/contabilidad/facturas", { params: { estado: estado || undefined, q: q || undefined } }),
  // PREVISUALIZA la factura de la vía MANUAL antes de registrarla: mismo payload que el
  // POST, así que lo que muestra es lo que el registro va a validar. NO persiste, NO
  // congela y NO toca el SII. El botón se gobierna con `puede_emitir` (el folio se valida
  // aparte, al registrar).
  previewFactura: (data: MonzaFacturaPayload) =>
    api.post<MonzaFacturaPreview>("/contabilidad/facturas/preview", data),
  // Registra una factura YA emitida (vía manual, con folio). Con es_anticipo:true +
  // monto_neto_anticipo registra una factura de ANTICIPO (sin guía de despacho).
  // La respuesta trae `advertencias`: hay que MOSTRARLAS (un 200 puede venir con avisos
  // que cambian lo que el usuario cree que pasó).
  crearFactura: (data: MonzaFacturaPayload) =>
    api.post<MonzaFacturaCreada>("/contabilidad/facturas", data),
  eliminarFactura: (id: number) => api.delete(`/contabilidad/facturas/${id}`),
  // Cobranzas
  registrarCobranza: (facturaId: number, data: MonzaCobranzaPayload) =>
    api.post(`/contabilidad/facturas/${facturaId}/cobranzas`, data),
  eliminarCobranza: (facturaId: number, cobranzaId: number) =>
    api.delete(`/contabilidad/facturas/${facturaId}/cobranzas/${cobranzaId}`),
  // Factoring (por factura)
  setFactoring: (facturaId: number, data: MonzaFactoringPayload) =>
    api.post(`/contabilidad/facturas/${facturaId}/factoring`, data),
  liquidarFactoring: (facturaId: number) =>
    api.post(`/contabilidad/facturas/${facturaId}/factoring/liquidar`),
  // Revierte una cesión al factor que quedó contra un documento que el SII nunca
  // conoció. El backend sólo la acepta EN ESE CASO (si la factura tiene folio responde
  // 409: la cesión es real). La fila de factoring se BORRA y la traza queda en las
  // observaciones de la factura, así que el motivo es obligatorio.
  revertirFactoring: (facturaId: number, motivo: string) =>
    api.post(`/contabilidad/facturas/${facturaId}/factoring/revertir`, { motivo }),
  kpis: (periodo?: string) => api.get("/contabilidad/kpis", { params: periodo ? { periodo } : {} }),
};

// ── Embarques Pricing (costo landed) — SOLO MonzaParts ────────────────────────
export const monzaEmbarquesPricingAPI = {
  list: (q?: string) =>
    api.get<EmbarquePricingRow[]>("/embarques-pricing", { params: q ? { q } : {} }),
  get: (embarqueId: number) =>
    api.get<PricingDetail>(`/embarques-pricing/${embarqueId}`),
  save: (embarqueId: number, data: PricingSavePayload) =>
    api.put<PricingDetail>(`/embarques-pricing/${embarqueId}`, data),
  cerrar: (embarqueId: number) =>
    api.post<PricingDetail>(`/embarques-pricing/${embarqueId}/cerrar`),
  reabrir: (embarqueId: number) =>
    api.post<PricingDetail>(`/embarques-pricing/${embarqueId}/reabrir`),
};

// ── Compras / Cuentas por Pagar (AP, NIIF/NIC 7) — SOLO MonzaParts ────────────
//
// PAYLOADS TIPADOS (espejo de monza_compras_contab/schemas.py y de la capa gemela de
// Grupo AM, compras-contab/api.ts). Antes eran `Record<string, unknown>`: un campo mal
// escrito COMPILABA, Pydantic lo descartaba en silencio y la compra se grababa con el
// dato faltante en su default — un `tc` que no llegó vale 1 y contabiliza una factura en
// USD a tipo de cambio 1. Los valores de lista (moneda, medio, tipo_gasto) se declaran
// `string` a propósito, igual que en Grupo AM: el backend es la autoridad y las pantallas
// los manejan como estado de texto libre.
export interface MonzaCompraListParams {
  tipo?: string;
  estado_pago?: string;
  categoria?: string;
  periodo?: string;
  q?: string;
  proveedor_id?: number;
  incluir_anulados?: boolean;
  page?: number;
  page_size?: number;
}
/** Pago al momento de registrar la compra (contado o abono inmediato): genera un
 *  Comprobante de Egreso de un solo detalle. Sin `monto_clp` en contado → el total. */
export interface MonzaPagoInlinePayload {
  fecha?: string;
  monto_clp?: number;
  /** transferencia | cheque | efectivo | tarjeta */
  medio?: string;
  banco?: string;
  cuenta_origen_id?: number;
  fecha_mov_bancario?: string;
  numero_operacion?: string;
  observaciones?: string;
}
/** Línea de costeo por ítem de una compra NACIONAL (la factura ES el costo de esos
 *  repuestos). ADAPTACIÓN Monza: sin `oc_proveedor_item_id` — la pertenencia ítem↔OC la
 *  valida el router contra MonzaCotizacionItem.oc_proveedor_id. */
export interface MonzaCompraItemPayload {
  item_cotizacion_id: number;
  numero_parte?: string | null;
  descripcion?: string | null;
  cantidad: number;
  /** NETO unitario en la moneda de la factura (CLP en una compra nacional). */
  precio_unit: number;
}
export interface MonzaCompraCreatePayload {
  tipo_gasto: string;
  categoria?: string;
  cuenta_contable_id?: number;
  es_anticipo?: boolean;
  /** MANUAL | EMBARQUE | NACIONAL — 'NACIONAL' elige la cuenta de Existencias (NIC 2)
   *  y habilita el detalle por ítem. */
  origen?: string;
  proveedor_id?: number;
  acreedor?: string;
  proveedor_rut?: string;
  fecha?: string;
  referencia?: string;
  descripcion?: string;
  numero_documento?: string;
  tipo_doc?: string;
  moneda?: string;
  tc?: number;
  monto_neto?: number;
  iva?: number;
  monto_total?: number;
  afecto_iva?: boolean;
  /** contado | credito. 'contado' sin `pago` genera el egreso por el total. */
  condicion_pago?: string;
  plazo_dias?: number;
  embarque_id?: number;
  /** Gasto de Embarques Pricing que esta compra REFLEJA como CxP pagable: es la llave
   *  anti-duplicado del overlay (el backend la usa para no registrarlo dos veces). */
  emb_pricing_gasto_id?: number;
  oc_proveedor_id?: number;
  items?: MonzaCompraItemPayload[];
  observaciones?: string;
  pago?: MonzaPagoInlinePayload;
}
/** Pago posterior de UNA compra (parcial o total): genera un egreso de 1 detalle. */
export interface MonzaPagoPayload {
  fecha?: string;
  monto_clp: number;
  medio?: string;
  banco?: string;
  cuenta_origen_id?: number;
  fecha_mov_bancario?: string;
  numero_operacion?: string;
  observaciones?: string;
}
export interface MonzaEgresoDetallePayload { compra_id: number; monto_clp: number }
/** Comprobante de Egreso CONSOLIDADO: una salida de dinero que paga VARIAS compras. */
export interface MonzaEgresoCreatePayload {
  fecha?: string;
  medio?: string;
  cuenta_origen_id?: number;
  banco?: string;
  numero_operacion?: string;
  beneficiario?: string;
  beneficiario_rut?: string;
  glosa?: string;
  moneda?: string;
  tc?: number;
  fecha_mov_bancario?: string;
  detalles: MonzaEgresoDetallePayload[];
}
/** Completar/editar los datos de conciliación de un egreso. */
export interface MonzaEgresoUpdatePayload {
  fecha_mov_bancario?: string;
  referencia_bancaria?: string;
}

export const monzaComprasAPI = {
  list: (params?: MonzaCompraListParams) => api.get("/compras-contab", { params }),
  detalle: (id: number) => api.get(`/compras-contab/${id}`),
  crear: (data: MonzaCompraCreatePayload) => api.post("/compras-contab", data),
  kpis: (params?: { periodo?: string; tipo?: string }) => api.get("/compras-contab/kpis", { params }),
  catalogos: () => api.get("/compras-contab/catalogos"),
  // Overlay en vivo: gastos anotados en Embarques Pricing (reflejados automáticamente)
  costosEmbarque: () => api.get("/compras-contab/costos-embarque"),
  // Pago de UNA compra (crea un egreso de 1 detalle)
  registrarPago: (id: number, data: MonzaPagoPayload) =>
    api.post(`/compras-contab/${id}/pagos`, data),
  // Edita la fecha del banco (cartola) del egreso al que pertenece este pago.
  actualizarPago: (id: number, pagoId: number, data: MonzaEgresoUpdatePayload) =>
    api.patch(`/compras-contab/${id}/pagos/${pagoId}`, data),
  eliminarPago: (id: number, pagoId: number) =>
    api.delete(`/compras-contab/${id}/pagos/${pagoId}`),
  // Egreso consolidado: una salida de dinero paga varias compras
  crearEgresoConsolidado: (data: MonzaEgresoCreatePayload) => api.post("/compras-contab/egresos", data),
  listarEgresos: (params?: { conciliado?: boolean; q?: string; page?: number; page_size?: number }) =>
    api.get("/compras-contab/egresos", { params }),
  actualizarEgreso: (egresoId: number, data: MonzaEgresoUpdatePayload) =>
    api.patch(`/compras-contab/egresos/${egresoId}`, data),
  eliminarEgreso: (egresoId: number) => api.delete(`/compras-contab/egresos/${egresoId}`),
  anular: (id: number, motivo?: string) => api.post(`/compras-contab/${id}/anular`, { motivo }),
  eliminar: (id: number) => api.delete(`/compras-contab/${id}`),
  // OC nacionales con sus ítems costeables (para el detalle por ítem del alta de una
  // compra NACIONAL). disponible_costear ya viene topeado por lo recibido en bodega
  // menos lo ya costeado (el backend es la autoridad).
  ocNacionales: () => api.get<MonzaOcNacionalesResponse>("/compras-contab/oc-nacionales"),
};

// ── Recepción Nacional (camino físico de la compra NACIONAL: camión + guía del
// proveedor, SIN embarque) — SOLO MonzaParts. Al cerrar la entrega, los
// utilizables con qty>0 pasan a en_bodega y quedan despachables, capados por lo
// recibido. ADAPTACIÓN Monza: sin oc_proveedor_item_id (el vínculo ítem↔OC es
// directo vía MonzaCotizacionItem.oc_proveedor_id, no hay tabla de asignación). ──
export interface MonzaEntregaNacionalItemPayload {
  item_cotizacion_id: number;
  qty_recibida: number;
  estado_recepcion: string;
  observacion?: string;
}
export interface MonzaRegistrarEntregaNacionalPayload {
  oc_proveedor_id: number;
  numero_guia_proveedor?: string;
  fecha?: string;
  documento?: string;
  observacion?: string;
  cerrar?: boolean;
  items: MonzaEntregaNacionalItemPayload[];
}
// Ítem 'comprado'/'en_bodega' de una OC nacional con su remanente por recibir
// (GET /recepcion-nacional/pendientes/{ocp_id}).
export interface MonzaPendienteNacionalItem {
  item_cotizacion_id: number;
  numero_parte: string | null;
  descripcion: string | null;
  estado_item: string;
  cantidad: number;
  recibido: number;
  remanente: number;
}
export interface MonzaPendientesNacionalResponse {
  oc_proveedor_id: number;
  numero: string | null;
  numero_oc: string | null;
  proveedor: string | null;
  items: MonzaPendienteNacionalItem[];
}
export const monzaRecepcionNacionalAPI = {
  // Registra una entrega (una recepción). cerrar:true → cierra y los utilizables
  // pasan a en_bodega (despachables). El backend es la autoridad del tope físico.
  registrar: (data: MonzaRegistrarEntregaNacionalPayload) =>
    api.post("/recepcion-nacional", data),
  cerrar: (id: number) => api.post(`/recepcion-nacional/${id}/cerrar`),
  anular: (id: number) => api.delete(`/recepcion-nacional/${id}`),
  // Ítems 'comprado'/'en_bodega' de una OC nacional con su remanente por recibir.
  pendientes: (ocpId: number) =>
    api.get<MonzaPendientesNacionalResponse>(`/recepcion-nacional/pendientes/${ocpId}`),
  listar: (ocpId?: number) =>
    api.get("/recepcion-nacional", { params: ocpId ? { ocp_id: ocpId } : {} }),
  detalle: (id: number) => api.get(`/recepcion-nacional/${id}`),
};

// ── Catálogo de OC nacionales costeables (GET /compras-contab/oc-nacionales) ──
export interface MonzaOcNacionalItem {
  item_cotizacion_id: number;
  numero_parte: string | null;
  descripcion: string | null;
  cantidad: number;           // cantidad vendida (asignada a la OC)
  recibido: number;           // Σ recibido nacional utilizable
  ya_costeado: number;        // Σ monza_cont_compra_item en compras activas
  disponible_costear: number; // max(min(recibido, cantidad) − ya_costeado, 0)
}
export interface MonzaOcNacional {
  oc_proveedor_id: number;
  numero: string | null;
  numero_oc: string | null;
  proveedor: string | null;
  moneda: string | null;
  items: MonzaOcNacionalItem[];
}
export interface MonzaOcNacionalesResponse { ocs: MonzaOcNacional[] }

// ── Tesorería (aprobaciones 50% + conciliación bancaria + flujo de caja) — SOLO MonzaParts ──
//
// PAYLOADS TIPADOS (espejo de monza_tesoreria/schemas.py y de la capa gemela de Grupo AM,
// tesoreria/api.ts). Antes eran `Record<string, unknown>`: un `numero_operacion` escrito
// `nro_operacion` compilaba, Pydantic lo descartaba y el movimiento quedaba sin la
// referencia con la que se cruza el banco — sin ningún error visible.
export interface MonzaCuentaPayload {
  banco: string;
  nombre?: string;
  numero_cuenta?: string;
  /** CLP | USD | EUR (la conciliación hoy solo corre sobre cuentas CLP). */
  moneda?: string;
  activo?: boolean;
  observaciones?: string;
}
/** Alta MANUAL de un movimiento bancario: el cheque, el efectivo o la comisión que no
 *  viene en la cartola. Sin esto ese movimiento no se puede conciliar nunca. */
export interface MonzaMovimientoPayload {
  cuenta_id: number;
  fecha?: string;
  glosa?: string;
  /** cargo (sale plata) | abono (entra plata). */
  tipo?: string;
  monto: number;
  referencia?: string;
  saldo?: number;
  cartola_id?: number;
}
/** Enlaza el movimiento con su destino. EXACTAMENTE UNO de los tres (el backend rechaza
 *  cero o dos): egreso_id → cargo ↔ Comprobante de Egreso; adelanto_id → abono ↔ adelanto
 *  50%; cobranza_id → abono ↔ cobranza de una factura. */
export interface MonzaConciliarPayload {
  egreso_id?: number;
  adelanto_id?: number;
  cobranza_id?: number;
}
/** Aprobación del adelanto por TESORERÍA: el monto REALMENTE recibido en el banco. Es la
 *  orden que destraba a Abastecimiento, y no exige cartola. */
export interface MonzaAprobarAdelantoPayload {
  monto: number;
  fecha_pago?: string;
  banco?: string;
  numero_operacion?: string;
  observaciones?: string;
}

/** Una ventana de vencimiento del flujo de caja. */
export interface MonzaFlujoBucket { n: number; monto: number }
/** Proyección de caja NIC 7. Los tres bloques de FUERA de los buckets no son entradas
 *  futuras y por eso van aparte:
 *   · retenciones_factoring → plata que libera el factor al LIQUIDAR, no al vencimiento.
 *   · adelantos_por_aprobar → todavía no es plata segura (Tesorería no la confirmó).
 *   · adelantos_recibidos_sin_aplicar → plata YA en la cuenta esperando su factura;
 *     explica por qué las próximas facturas nacerán con menos saldo.
 *  Los dos últimos el backend los devuelve desde siempre y la pantalla los escondía. */
export interface MonzaFlujoCaja {
  buckets: string[];
  por_pagar: Record<string, MonzaFlujoBucket>;
  por_cobrar: Record<string, MonzaFlujoBucket>;
  neto: Record<string, number>;
  retenciones_factoring: MonzaFlujoBucket;
  adelantos_por_aprobar: MonzaFlujoBucket;
  adelantos_recibidos_sin_aplicar: MonzaFlujoBucket;
}
/** KPIs del encabezado de Tesorería. Con `cuenta_id` los contadores de movimientos se
 *  acotan a esa cuenta (los de Compras/Facturas son globales por naturaleza). */
export interface MonzaResumenTesoreria {
  aprobaciones_pendientes: number;
  monto_aprobaciones_clp: number;
  pagos_por_aprobar: number;
  monto_por_pagar_clp: number;
  por_pagar_vencido_clp: number;
  movimientos_total: number;
  movimientos_conciliados: number;
  cargos_pendientes: number;
  abonos_pendientes: number;
  egresos_sin_conciliar: number;
  cobranzas_sin_conciliar: number;
  adelantos_sin_conciliar: number;
}

export const monzaTesoreriaAPI = {
  // Aprobaciones: la ORDEN de los adelantos 50% (destraba Abastecimiento)
  aprobaciones: () => api.get("/tesoreria/aprobaciones"),
  aprobarAdelanto: (cotId: number, data: MonzaAprobarAdelantoPayload) =>
    api.post(`/tesoreria/aprobaciones/${cotId}/aprobar`, data),
  // Conciliación bancaria
  cuentas: (incluirInactivas?: boolean) =>
    api.get("/tesoreria/cuentas", { params: incluirInactivas ? { incluir_inactivas: true } : {} }),
  crearCuenta: (data: MonzaCuentaPayload) => api.post("/tesoreria/cuentas", data),
  // PUT de REEMPLAZO TOTAL: lo que no viaje se pierde. Al editar hay que mandar también
  // moneda / activo / observaciones de la cuenta, no solo lo que cambió.
  actualizarCuenta: (id: number, data: MonzaCuentaPayload) => api.put(`/tesoreria/cuentas/${id}`, data),
  eliminarCuenta: (id: number) => api.delete(`/tesoreria/cuentas/${id}`),
  importarCartola: (cuentaId: number, file: File, nombre?: string) => {
    const fd = new FormData();
    fd.append("cuenta_id", String(cuentaId));
    if (nombre) fd.append("nombre", nombre);
    fd.append("file", file);
    // Subida de ARCHIVO: SIN tope de tiempo. El límite global de 60 s alcanza de sobra
    // para un listado, pero no para un archivo de 20 MB por la conexión de un taller,
    // y cortarlo deja al operador sin poder completar el trámite — en el caso de la
    // guía firmada, sin poder facturar.
    return api.post("/tesoreria/cartolas/importar", fd, { timeout: 0 });
  },
  cartolas: (cuentaId?: number) =>
    api.get("/tesoreria/cartolas", { params: cuentaId ? { cuenta_id: cuentaId } : {} }),
  eliminarCartola: (id: number) => api.delete(`/tesoreria/cartolas/${id}`),
  movimientos: (params?: { cuenta_id?: number; estado?: string; tipo?: string; q?: string; page?: number; page_size?: number }) =>
    api.get("/tesoreria/movimientos", { params }),
  crearMovimiento: (data: MonzaMovimientoPayload) => api.post("/tesoreria/movimientos", data),
  eliminarMovimiento: (id: number) => api.delete(`/tesoreria/movimientos/${id}`),
  sugerencias: (movId: number) => api.get(`/tesoreria/movimientos/${movId}/sugerencias`),
  conciliar: (movId: number, data: MonzaConciliarPayload) =>
    api.post(`/tesoreria/movimientos/${movId}/conciliar`, data),
  desconciliar: (movId: number) => api.post(`/tesoreria/movimientos/${movId}/desconciliar`),
  egresosPendientes: (q?: string) =>
    api.get("/tesoreria/egresos-pendientes", { params: q ? { q } : {} }),
  adelantosPendientes: () => api.get("/tesoreria/adelantos-pendientes"),
  cobranzasPendientes: (q?: string, page = 1) =>
    api.get("/tesoreria/cobranzas-pendientes", { params: { q: q || undefined, page } }),
  // Por pagar / aprobar pagos (Tesorería da la orden → crea el Comprobante de Egreso).
  // El payload es el MISMO Comprobante de Egreso de Compras/CxP: el backend importa
  // EgresoCreate de ese módulo, así que la regla de negocio del pago es una sola.
  porPagar: (params?: { q?: string; page?: number; page_size?: number }) =>
    api.get("/tesoreria/por-pagar", { params }),
  aprobarPago: (data: MonzaEgresoCreatePayload) => api.post("/tesoreria/pagos", data),
  // Flujo de caja + KPIs
  flujoCaja: () => api.get<MonzaFlujoCaja>("/tesoreria/flujo-caja"),
  // `cuentaId` acota los contadores de conciliación a UNA cuenta bancaria: sin él los
  // KPIs mezclan todas las cuentas y no dicen nada de la que se está cuadrando.
  resumen: (cuentaId?: number) =>
    api.get<MonzaResumenTesoreria>("/tesoreria/resumen", { params: { cuenta_id: cuentaId } }),
};

// ── Leads ───────────────────────────────────────────────────────────────────
export const monzaLeadsAPI = {
  kpis: () => api.get("/leads/kpis"),
  list: (params: Record<string, unknown>) => api.get("/leads", { params }),
  get: (id: number) => api.get(`/leads/${id}`),
  create: (data: Record<string, unknown>) => api.post("/leads", data),
  update: (id: number, data: Record<string, unknown>) => api.patch(`/leads/${id}`, data),

  addItem: (leadId: number, data: Record<string, unknown>) =>
    api.post(`/leads/${leadId}/items`, data),
  updateItem: (leadId: number, itemId: number, data: Record<string, unknown>) =>
    api.put(`/leads/${leadId}/items/${itemId}`, data),
  deleteItem: (leadId: number, itemId: number) =>
    api.delete(`/leads/${leadId}/items/${itemId}`),

  agendarPaso: (leadId: number, data: Record<string, unknown>) =>
    api.post(`/leads/${leadId}/proximos-pasos`, data),
  completarPaso: (leadId: number, pasoId: number) =>
    api.patch(`/leads/${leadId}/proximos-pasos/${pasoId}/completar`),

  addActividad: (leadId: number, data: Record<string, unknown>) =>
    api.post(`/leads/${leadId}/actividades`, data),

  searchClientes: (q: string) =>
    api.get("/leads/clientes/search", { params: { q } }),
  updateCliente: (leadId: number, data: Record<string, unknown>) =>
    api.patch(`/leads/${leadId}/cliente`, data),

  listAsesores: () => api.get("/leads/asesores/list"),
  deleteLead: (id: number) => api.delete(`/leads/${id}`),
};

// ── Cotizador ────────────────────────────────────────────────────────────────
export const monzaCotizadorAPI = {
  calcular: (data: Record<string, unknown>) => api.post("/cotizador/calcular", data),
  aplicar: (data: Record<string, unknown>) => api.post("/cotizador/aplicar", data),
};

// ── Cotizaciones ─────────────────────────────────────────────────────────────
export const monzaCotizacionesAPI = {
  list: (params: Record<string, unknown>) => api.get("/cotizaciones", { params }),
  get: (id: number) => api.get(`/cotizaciones/${id}`),
  create: (data: Record<string, unknown>) => api.post("/cotizaciones", data),
  update: (id: number, data: Record<string, unknown>) =>
    api.patch(`/cotizaciones/${id}`, data),
  downloadPdf: (id: number) =>
    api.get(`/cotizaciones/${id}/pdf`, { responseType: "arraybuffer" }),
};

// ── Ventas ────────────────────────────────────────────────────────────────────
export const monzaVentasAPI = {
  list: (params: Record<string, unknown>) => api.get("/ventas", { params }),
  kpis: () => api.get("/ventas/kpis"),
};

// ── Despachos ─────────────────────────────────────────────────────────────────
export const monzaDespachosAPI = {
  list: (params: Record<string, unknown>) => api.get("/despachos", { params }),
  kpis: () => api.get("/despachos/kpis"),
  uploadDocumento: (cotId: number, file: File, tipo: string) => {
    const form = new FormData();
    form.append("file", file);
    form.append("tipo", tipo);
    // Subida de ARCHIVO: SIN tope de tiempo. El límite global de 60 s alcanza de sobra
    // para un listado, pero no para un archivo de 20 MB por la conexión de un taller,
    // y cortarlo deja al operador sin poder completar el trámite — en el caso de la
    // guía firmada, sin poder facturar.
    return api.post(`/cotizaciones/${cotId}/documento`, form, {
      headers: { "Content-Type": "multipart/form-data" },
      timeout: 0,
    });
  },
  downloadDocumento: (cotId: number) =>
    api.get(`/cotizaciones/${cotId}/documento`, { responseType: "arraybuffer" }),
  // Despacho como entidad (alineación MachParts) — ciclo de vida Fase 2:
  // crear (borrador en_preparacion) → cerrar (confirma la salida) / anular.
  listos: () => api.get("/despachos/listos"),
  // Marca la guía del despacho como FIRMADA por el cliente (obligatorio para poder
  // facturar, regla 2026-08-06). Multipart en UN request: foto/PDF + fecha de la firma
  // (+ N° de guía opcional, rechazado si pisa un folio SII). Solo despachos cerrados.
  firmarEntidad: (id: number, file: File, fechaFirma: string, numeroGuia?: string) => {
    const form = new FormData();
    form.append("file", file);
    form.append("fecha_firma", fechaFirma);
    if (numeroGuia) form.append("numero_guia", numeroGuia);
    // SIN tope de tiempo: es la foto de la guía firmada, tomada con el celular en el
    // taller y de hasta 20 MB. Cortarla a los 60 s deja al operador sin poder marcar la
    // firma — y sin firma NO se puede facturar. Este es el peor caso del tope global.
    return api.post(`/despachos/entidades/${id}/firmar`, form, {
      headers: { "Content-Type": "multipart/form-data" },
      timeout: 0,
    });
  },
  // Abre la guía firmada (u otro doc de uploads/docs) vía el serve de ESTE módulo:
  // el de GA (services/api.ts:abrirDocumento) está detrás del candado 'mineria' y a
  // un usuario Monza le respondía 403.
  abrirGuiaFirmada: async (filename: string) => {
    const res = await api.get(`/despachos/docs/${filename}`, { responseType: "blob" });
    const url = URL.createObjectURL(res.data as Blob);
    window.open(url, "_blank");
  },
  crear: (data: Record<string, unknown>) => api.post("/despachos/crear", data),
  entidades: () => api.get("/despachos/entidades"),
  getEntidad: (id: number) => api.get(`/despachos/entidades/${id}`),
  updateEntidad: (id: number, data: Record<string, unknown>) => api.put(`/despachos/entidades/${id}`, data),
  cerrarEntidad: (id: number) => api.post(`/despachos/entidades/${id}/cerrar`),
  anularEntidad: (id: number) => api.delete(`/despachos/entidades/${id}`),
};

// ── Wasabil DTE (guías de despacho electrónicas al SII) — SOLO MonzaParts ─────
// Espejo de wasabilAPI (services/api.ts) sobre el axios monzaApi (baseURL
// /api/monza): las rutas /wasabil/... resuelven a /api/monza/wasabil/...
// (montaje del backend, monza_wasabil_dte/router.py). Flujo: preview (no toca el
// SII) → emitir (con OK explícito del usuario, IRREVERSIBLE) → sondeo de estado
// hasta Emitido (folio + PDF) o Fallido (reintento seguro).
export interface MonzaDteInfo {
  id: number;
  tipo_dte?: number;
  uuid?: string | null;
  // emitido | procesando | pendiente | fallido | enviando | error_envio | no_enviado
  estado: string;
  // ÚNICA fuente de verdad del botón Reintentar (la decide el backend)
  puede_reintentar: boolean;
  folio?: string | null;
  pdf_url?: string | null;
  error?: string | null;
}
// Fase 6: el DTE de una FACTURA viaja con `factura_id` agregado FUERA del
// serializer (el backend hace `{...serialize_dte(dte), "factura_id": id}`), y es el
// id con el que el modal sondea una emisión que nació desde un payload (la factura
// local todavía no existía cuando se abrió el modal).
export interface MonzaDteFacturaInfo extends MonzaDteInfo {
  factura_id?: number;
  // Solo en el sondeo: la consulta a Wasabil falló pero el estado local sí llegó.
  error_consulta?: string;
  // Fase 7: al confirmarse el folio se aplica el adelanto que la emisión había diferido,
  // y si es una factura de ANTICIPO el backend intenta RE-ENCAUZAR hacia ella el adelanto
  // que ya estaba en otra factura. Si no puede (factoring vigente, cobranza conciliada),
  // avisa acá con el remedio. Viene SIEMPRE (lista vacía si no hay nada que avisar) y solo
  // en el request que grabó el folio: la finalización es idempotente.
  advertencias?: string[];
}
// Fase 7: cada factura de ANTICIPO que ESTA factura descuenta (línea negativa en el
// documento + referencia 33 al folio del anticipo). Viene en los DOS previews —el del
// DTE y el de la vía manual (MonzaFacturaPreview)—: es el mismo dato, calculado por el
// mismo constructor del backend.
export interface MonzaDescuentoAnticipo {
  anticipo_factura_id: number;
  folio?: string | null;
  monto_neto: number;
}
export const monzaWasabilAPI = {
  config: () => api.get("/wasabil/config"),
  previewGuia: (despachoId: number, tipoTraslado?: number) =>
    api.post(`/wasabil/despachos/${despachoId}/preview`, null,
      tipoTraslado ? { params: { tipo_traslado: tipoTraslado } } : undefined),
  // ⚠️ SIN TIEMPO LÍMITE (`timeout: 0`), a propósito y con el resto del cliente en 60 s:
  // esta llamada emite un documento tributario REAL contra el SII. Cortarla por tiempo
  // no cancela la emisión —el documento puede quedar emitido igual— y deja el estado
  // AMBIGUO, que es exactamente la condición que ya provocó dobles emisiones. Ante un
  // documento irreversible, esperar es siempre más barato que dudar.
  emitirGuia: (despachoId: number, tipoTraslado?: number) =>
    api.post(`/wasabil/despachos/${despachoId}/emitir`, null,
      { timeout: 0, ...(tipoTraslado ? { params: { tipo_traslado: tipoTraslado } } : {}) }),
  estadoGuia: (despachoId: number) => api.get(`/wasabil/despachos/${despachoId}/estado`),
  // OJO: pasar SIEMPRE tipoTraslado al reintentar — omitirlo revierte en silencio
  // al default 1 (venta) aunque el intento fallido fuera otro tipo de traslado.
  // ⚠️ SIN TIEMPO LÍMITE (`timeout: 0`), a propósito y con el resto del cliente en 60 s:
  // esta llamada emite un documento tributario REAL contra el SII. Cortarla por tiempo
  // no cancela la emisión —el documento puede quedar emitido igual— y deja el estado
  // AMBIGUO, que es exactamente la condición que ya provocó dobles emisiones. Ante un
  // documento irreversible, esperar es siempre más barato que dudar.
  reintentarGuia: (despachoId: number, tipoTraslado?: number) =>
    api.post(`/wasabil/despachos/${despachoId}/reintentar`, null,
      { timeout: 0, ...(tipoTraslado ? { params: { tipo_traslado: tipoTraslado } } : {}) }),
  // Estado en LOTE (solo BD, sin llamar a Wasabil) → { despacho_id: dte }, para
  // pintar folio/PDF/fallida en las filas sin N llamadas de red.
  estadoBatch: (despachoIds: number[]) =>
    api.get<Record<number, MonzaDteInfo>>("/wasabil/despachos/estado-batch",
      { params: { ids: despachoIds.join(",") } }),
  // SALIDA del callejón "guía EMITIDA sin folio". NO emite: sólo escribe el folio que
  // el operador leyó en app.wasabil.com. `confirmo` viaja aparte a propósito — es la
  // constancia de que lo tecleó dos veces, y el backend rechaza si no coinciden.
  registrarFolioGuia: (despachoId: number, folio: string, confirmo: string) =>
    api.post<MonzaDteInfo>(`/wasabil/despachos/${despachoId}/registrar-folio`, null,
      { timeout: 0, params: { folio, confirmo_folio: confirmo } }),

  // ── Fase 6: FACTURAS electrónicas (DTE 33) ─────────────────────────────────
  // El payload es el MISMO de monzaContabilidadAPI.crearFactura pero SIN
  // numero_factura: el folio lo asigna el SII al emitir (el backend rechaza el
  // payload si viene con folio). preview NO persiste y NO toca el SII.
  // Fase 7: con es_anticipo:true el preview devuelve además `es_anticipo` y
  // `descuentos` (los anticipos que ESTA factura descuenta).
  previewFacturaSII: (payload: MonzaFacturaPayload) =>
    api.post("/wasabil/facturas/preview", payload),
  // IRREVERSIBLE: crea la factura local sin folio + el claim, y recién ahí emite.
  // ⚠️ SIN TIEMPO LÍMITE (`timeout: 0`): emite un documento tributario REAL. Cortar
  // por tiempo no cancela la emisión y deja el estado AMBIGUO — la condición que ya
  // provocó dobles emisiones. Esperar es más barato que dudar.
  emitirFacturaSII: (payload: MonzaFacturaPayload) =>
    api.post<MonzaDteFacturaInfo>("/wasabil/facturas/emitir", payload, { timeout: 0 }),
  estadoFacturaSII: (facturaId: number) =>
    api.get<MonzaDteFacturaInfo>(`/wasabil/facturas/${facturaId}/estado`),
  // ⚠️ SIN TIEMPO LÍMITE (`timeout: 0`): emite un documento tributario REAL. Cortar
  // por tiempo no cancela la emisión y deja el estado AMBIGUO — la condición que ya
  // provocó dobles emisiones. Esperar es más barato que dudar.
  reintentarFacturaSII: (facturaId: number) =>
    api.post<MonzaDteFacturaInfo>(`/wasabil/facturas/${facturaId}/reintentar`, null, { timeout: 0 }),
  // Estado en LOTE (solo BD, sin llamar a Wasabil) → { factura_id: dte }, para
  // pintar los badges SII del listado sin N llamadas de red (el serializador de
  // Contabilidad Monza no inyecta campos dte_*).
  estadoBatchFacturas: (facturaIds: number[]) =>
    api.get<Record<number, MonzaDteFacturaInfo>>("/wasabil/facturas/estado-batch",
      { params: { ids: facturaIds.join(",") } }),
  // Gemela de registrarFolioGuia para la factura 33. Además del folio, el backend
  // completa el N° de la factura local y aplica el adelanto que la emisión había
  // diferido, así que puede devolver `advertencias` que el operador tiene que ver.
  registrarFolioFactura: (facturaId: number, folio: string, confirmo: string) =>
    api.post<MonzaDteFacturaInfo & { advertencias?: string[] }>(
      `/wasabil/facturas/${facturaId}/registrar-folio`, null,
      { timeout: 0, params: { folio, confirmo_folio: confirmo } }),
};

// ── Clientes ──────────────────────────────────────────────────────────────────
export const monzaClientesAPI = {
  list: (params?: { q?: string; activos?: boolean; page?: number; page_size?: number }) =>
    api.get("/clientes", { params }),
  get: (id: number) => api.get(`/clientes/${id}`),
  create: (data: Record<string, unknown>) => api.post("/clientes", data),
  update: (id: number, data: Record<string, unknown>) => api.patch(`/clientes/${id}`, data),
  remove: (id: number) => api.delete(`/clientes/${id}`),
};

// ── Documentos adjuntos (genérico) ────────────────────────────────────────────
export const monzaDocumentosAPI = {
  list: (entidad: string, entidad_id: number) =>
    api.get("/documentos", { params: { entidad, entidad_id } }),
  upload: (entidad: string, entidad_id: number, file: File, categoria = "otro") => {
    const form = new FormData();
    form.append("entidad", entidad);
    form.append("entidad_id", String(entidad_id));
    form.append("categoria", categoria);
    form.append("file", file);
    // Subida de ARCHIVO: SIN tope de tiempo. El límite global de 60 s alcanza de sobra
    // para un listado, pero no para un archivo de 20 MB por la conexión de un taller,
    // y cortarlo deja al operador sin poder completar el trámite — en el caso de la
    // guía firmada, sin poder facturar.
    return api.post("/documentos/upload", form,
      { headers: { "Content-Type": "multipart/form-data" }, timeout: 0 });
  },
  download: (id: number) => api.get(`/documentos/${id}/download`, { responseType: "arraybuffer" }),
  remove: (id: number) => api.delete(`/documentos/${id}`),
};

// ── Notificaciones in-app ─────────────────────────────────────────────────────
export const monzaNotificacionesAPI = {
  list: () => api.get("/notificaciones"),
  count: () => api.get("/notificaciones/count"),
  marcarLeida: (id: number) => api.patch(`/notificaciones/${id}/leida`),
  marcarTodas: () => api.post("/notificaciones/marcar-todas"),
};

// ── Logs de operaciones ───────────────────────────────────────────────────────
export const monzaLogsAPI = {
  list: (params: Record<string, unknown>) => api.get("/logs", { params }),
  summary: () => api.get("/logs/summary"),
};

// ── Tickets (soporte / solicitudes de cambio) — hilo de conversación ──────────
export const monzaTicketsAPI = {
  list: (params?: Record<string, unknown>) => api.get("/tickets", { params }),
  counts: () => api.get("/tickets/counts"),
  get: (id: number) => api.get(`/tickets/${id}`),
  crear: (data: Record<string, unknown>) => api.post("/tickets", data),
  responder: (id: number, mensaje: string) => api.post(`/tickets/${id}/respuestas`, { mensaje }),
  cambiarEstado: (id: number, estado: string) => api.patch(`/tickets/${id}`, { estado }),
};
// ── Preparación / embarque PARCIAL (Fase 9b) ──────────────────────────────────
//
// El proveedor manda 6 de 10. Antes había que mover la LÍNEA COMPLETA, así que se
// embarcaban los 10 y Bodega abría un reclamo fantasma por las 4 que el proveedor
// nunca despachó. Ahora la cantidad viaja por ítem: el backend parte la línea, lo que
// llegó sigue su camino y el remanente espera el próximo embarque.
/** Un ítem a mover, con la MISMA forma en las dos etapas (preparar y embarcar).
 *  `cantidad` ausente = TODA la línea (vía legada, sin partición). */
export interface MonzaItemQty { item_id: number; cantidad?: number }
/** Lo que el backend informa de cada línea PARTIDA. `pendiente` = unidades que
 *  quedaron esperando el próximo embarque (alimenta el aviso en pantalla). */
export interface MonzaRemanente { item_id: number; remanente_item_id: number; pendiente: number }
export interface MonzaPrepararResp {
  ok: boolean; preparados: number; partidos?: number; remanentes?: MonzaRemanente[];
}
export interface MonzaEmbarqueResp {
  ok: boolean; id: number; numero: string; items: number;
  partidos?: number; remanentes?: MonzaRemanente[];
}

/** Suma las unidades que quedaron pendientes tras un movimiento parcial. */
export function monzaTotalPendiente(rem?: MonzaRemanente[]): number {
  return (rem || []).reduce((s, r) => s + (r.pendiente || 0), 0);
}

/** Normaliza el error del backend a texto legible. Los 400/409 de la partición traen
 *  `detail` como string (cantidad inválida, estado incorrecto, documento encima), pero
 *  un 422 de Pydantic lo trae como ARRAY de objetos: sin esto el toast mostraría
 *  "[object Object]" — o peor, React reventaría al pintar un objeto. */
export function monzaErrMsg(e: unknown, fallback: string): string {
  const d = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (Array.isArray(d)) return d.map((x) => (x as { msg?: string })?.msg || JSON.stringify(x)).join("; ");
  if (typeof d === "string") return d;
  return fallback;
}

// ── Abastecimiento (Panel Compras + Seguimiento) ──────────────────────────────
export const monzaAbastecimientoAPI = {
  kpis: () => api.get("/abastecimiento/kpis"),
  porComprar: (params?: Record<string, unknown>) => api.get("/abastecimiento/por-comprar", { params }),
  // Contrato ADITIVO de agrupación por proveedor: además de las claves de siempre,
  // cada ítem gana `costo`, `moneda`, `peso_kg`, `peso_total_kg`, `fob_total` y
  // `ocp` (objeto con semáforo y completitud CALCULADOS EN EL BACKEND). Tipos en
  // src/monza-agrupacion/agrupacion.ts (MonzaClavesAgrupacion / MonzaOcp). Mientras
  // el backend no las mande, llegan undefined y la página degrada a "Sin OC".
  seguimiento: (params?: Record<string, unknown>) => api.get("/abastecimiento/seguimiento", { params }),
  // `cantidades` opcional en el body: [{item_id, cantidad}] activa la asignación
  // PARCIAL (la línea se parte y el remanente vuelve al panel, sin OC). Ausente =
  // body legado, líneas enteras. Contrato de MonzaItemQty: 0 se rechaza, ausente/
  // None = línea entera.
  comprar: (data: Record<string, unknown>) => api.post("/abastecimiento/comprar", data),
  listOcs: (params?: Record<string, unknown>) => api.get("/abastecimiento/ocs", { params }),
  ocItems: (id: number) => api.get(`/abastecimiento/ocs/${id}/items`),
  updateOc: (id: number, data: Record<string, unknown>) => api.patch(`/abastecimiento/ocs/${id}`, data),
  listProveedores: () => api.get("/abastecimiento/proveedores"),
  createProveedor: (data: Record<string, unknown>) => api.post("/abastecimiento/proveedores", data),
  updateProveedor: (id: number, data: Record<string, unknown>) => api.patch(`/abastecimiento/proveedores/${id}`, data),
  deleteProveedor: (id: number) => api.delete(`/abastecimiento/proveedores/${id}`),
  comprados: (params?: Record<string, unknown>) => api.get("/abastecimiento/comprados", { params }),
  // Dispatcher parcial-vs-legado EN UN SOLO LUGAR: preparan ítems DOS pantallas
  // (Abastecimiento y Seguimiento) y duplicar la decisión invitaba a que derivaran.
  // Si ningún ítem trae `cantidad`, el body es el `{item_ids}` de siempre contra el
  // endpoint legado (intacto); si alguno la trae, va al endpoint parcial. Sigue
  // aceptando `number[]` para no romper una llamada que solo tenga ids.
  preparar: (items: Array<number | MonzaItemQty>) => {
    const pedidos: MonzaItemQty[] = items.map((i) => (typeof i === "number" ? { item_id: i } : i));
    return pedidos.some((p) => p.cantidad !== undefined)
      ? api.post<MonzaPrepararResp>("/abastecimiento/items/preparar-parcial", { items: pedidos })
      : api.post<MonzaPrepararResp>("/abastecimiento/preparar", { item_ids: pedidos.map((p) => p.item_id) });
  },
  // BACK ORDER (caso Baukat): lo que el proveedor no va a mandar vuelve al panel de
  // compras. `cantidad` ausente = la línea completa; con cantidad, el resto sigue
  // comprado con su OC (misma partición que preparar-parcial, al revés).
  // El motivo es obligatorio: es la única transición hacia atrás del pipeline y borra
  // el vínculo con la OC del proveedor, así que sin él la línea queda sin explicación.
  devolverACompras: (items: MonzaItemQty[], motivo: string) =>
    api.post<MonzaDevolverResp>("/abastecimiento/items/devolver-a-compras", { items, motivo }),
};

/** Respuesta de devolver-a-compras: qué volvió, qué se partió y cómo quedaron las OC. */
export interface MonzaDevolverResp {
  ok: boolean;
  devueltos: number;
  partidos: number;
  remanentes: Array<{ item_id: number; devuelto: number; sigue_comprado: number }>;
  ocs: Array<{ ocp_id: number; numero: string | null; items_vivos: number }>;
}

// ── Logística (Embarques, alineación MachParts) ───────────────────────────────
export const monzaLogisticaAPI = {
  kpis: () => api.get("/logistica/kpis"),
  // Mismo contrato ADITIVO de agrupación que /abastecimiento/seguimiento: cada ítem
  // gana `costo`, `moneda`, `peso_kg`, `peso_total_kg`, `fob_total` y `ocp` (con
  // semáforo y completitud del backend). Tipos en src/monza-agrupacion/agrupacion.ts.
  preparados: (params?: Record<string, unknown>) => api.get("/logistica/preparados", { params }),
  // El body acepta `item_ids` (vía legada: línea completa) o `items: [{item_id,
  // cantidad}]` (embarque parcial). Misma URL de siempre; la respuesta informa los
  // remanentes que quedaron esperando el próximo AWB.
  crearEmbarque: (data: Record<string, unknown>) => api.post<MonzaEmbarqueResp>("/logistica/embarques", data),
  listEmbarques: (params?: Record<string, unknown>) => api.get("/logistica/embarques", { params }),
  getEmbarque: (id: number) => api.get(`/logistica/embarques/${id}`),
  updateEmbarque: (id: number, data: Record<string, unknown>) => api.patch(`/logistica/embarques/${id}`, data),
  quitarItem: (embId: number, itemId: number) => api.delete(`/logistica/embarques/${embId}/items/${itemId}`),
};

// ── Bodega (recepción física por embarque, alineación MachParts) ──────────────
export const monzaBodegaAPI = {
  kpis: () => api.get("/bodega/kpis"),
  embarques: () => api.get("/bodega/embarques"),
  recibir: (embId: number) => api.post(`/bodega/embarques/${embId}/recibir`),
  getRecepcion: (id: number) => api.get(`/bodega/recepciones/${id}`),
  marcarItem: (recId: number, itemId: number, data: Record<string, unknown>) => api.patch(`/bodega/recepciones/${recId}/items/${itemId}`, data),
  // forzar=true cierra con ítems sin marcar: quedan como reclamo "no llegó" trazable
  cerrarRecepcion: (recId: number, forzar = false) => api.post(`/bodega/recepciones/${recId}/cerrar`, { forzar }),
  enBodega: (params?: Record<string, unknown>) => api.get("/bodega/en-bodega", { params }),
  listReclamos: (params?: Record<string, unknown>) => api.get("/bodega/reclamos", { params }),
  updateReclamo: (id: number, data: Record<string, unknown>) => api.patch(`/bodega/reclamos/${id}`, data),
};

// ── Catálogo de partes ────────────────────────────────────────────────────────
export const monzaCatalogAPI = {
  /** Listas de precios disponibles (CARAT9, BMW-3D, etc.) */
  listas: () => api.get("/catalog/listas"),
  /** Búsqueda en el catálogo: mínimo 2 caracteres */
  search: (params: { q: string; lista_id?: number; calidad?: string; marca?: string; page?: number; page_size?: number }) =>
    api.get("/catalog/search", { params }),
  /** Detalle de una parte específica */
  getParte: (id: number) => api.get(`/catalog/${id}`),
  /** Matriz de markup para un cliente */
  markups: (clienteId: number) => api.get(`/catalog/markups/${clienteId}`),
};
