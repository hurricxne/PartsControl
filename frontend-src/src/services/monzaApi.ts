import axios from "axios";
import type {
  EmbarquePricingRow, PricingDetail, PricingSavePayload,
} from "../monza-embarques-pricing/types";

const api = axios.create({ baseURL: "/api/monza" });

api.interceptors.request.use((cfg) => {
  try {
    const raw = localStorage.getItem("machparts-auth");
    const token = raw ? JSON.parse(raw)?.state?.token : null;
    if (token) cfg.headers.Authorization = `Bearer ${token}`;
  } catch {}
  return cfg;
});

// ── Config ──────────────────────────────────────────────────────────────────
export const monzaConfigAPI = {
  get: () => api.get("/config"),
  update: (data: Record<string, unknown>) => api.put("/config", data),
};

// ── Contabilidad (Ventas + Facturas/Cobranzas/Factoring) — SOLO MonzaParts ────
export const monzaContabilidadAPI = {
  // Ventas (agrupado por cotización vendida/despachada)
  listVentas: (q?: string, periodo?: string) =>
    api.get("/contabilidad/ventas", { params: { q: q || undefined, periodo: periodo || undefined } }),
  ventaDetalle: (cotId: number) => api.get(`/contabilidad/ventas/${cotId}`),
  despachosFacturables: (cotId: number) => api.get(`/contabilidad/ventas/${cotId}/despachos-facturables`),
  marcarGuiaFirmada: (despId: number, data: Record<string, unknown>) =>
    api.patch(`/contabilidad/ventas/despachos/${despId}/guia-firmada`, data),
  // Adelanto (50%): Contabilidad verifica el pago informado por Comercial
  verificarAdelanto: (cotId: number, data: Record<string, unknown>) =>
    api.post(`/contabilidad/ventas/${cotId}/adelanto/verificar`, data),
  // Facturas / cuentas por cobrar
  listFacturas: (estado?: string, q?: string) =>
    api.get("/contabilidad/facturas", { params: { estado: estado || undefined, q: q || undefined } }),
  crearFactura: (data: Record<string, unknown>) => api.post("/contabilidad/facturas", data),
  eliminarFactura: (id: number) => api.delete(`/contabilidad/facturas/${id}`),
  // Cobranzas
  registrarCobranza: (facturaId: number, data: Record<string, unknown>) =>
    api.post(`/contabilidad/facturas/${facturaId}/cobranzas`, data),
  eliminarCobranza: (facturaId: number, cobranzaId: number) =>
    api.delete(`/contabilidad/facturas/${facturaId}/cobranzas/${cobranzaId}`),
  // Factoring (por factura)
  setFactoring: (facturaId: number, data: Record<string, unknown>) =>
    api.post(`/contabilidad/facturas/${facturaId}/factoring`, data),
  liquidarFactoring: (facturaId: number) =>
    api.post(`/contabilidad/facturas/${facturaId}/factoring/liquidar`),
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
export const monzaComprasAPI = {
  list: (params?: Record<string, unknown>) => api.get("/compras-contab", { params }),
  detalle: (id: number) => api.get(`/compras-contab/${id}`),
  crear: (data: Record<string, unknown>) => api.post("/compras-contab", data),
  kpis: (params?: Record<string, unknown>) => api.get("/compras-contab/kpis", { params }),
  catalogos: () => api.get("/compras-contab/catalogos"),
  // Overlay en vivo: gastos anotados en Embarques Pricing (reflejados automáticamente)
  costosEmbarque: () => api.get("/compras-contab/costos-embarque"),
  // Pago de UNA compra (crea un egreso de 1 detalle)
  registrarPago: (id: number, data: Record<string, unknown>) =>
    api.post(`/compras-contab/${id}/pagos`, data),
  actualizarPago: (id: number, pagoId: number, data: Record<string, unknown>) =>
    api.patch(`/compras-contab/${id}/pagos/${pagoId}`, data),
  eliminarPago: (id: number, pagoId: number) =>
    api.delete(`/compras-contab/${id}/pagos/${pagoId}`),
  // Egreso consolidado: una salida de dinero paga varias compras
  crearEgresoConsolidado: (data: Record<string, unknown>) => api.post("/compras-contab/egresos", data),
  listarEgresos: (params?: Record<string, unknown>) => api.get("/compras-contab/egresos", { params }),
  actualizarEgreso: (egresoId: number, data: Record<string, unknown>) =>
    api.patch(`/compras-contab/egresos/${egresoId}`, data),
  eliminarEgreso: (egresoId: number) => api.delete(`/compras-contab/egresos/${egresoId}`),
  anular: (id: number, motivo?: string) => api.post(`/compras-contab/${id}/anular`, { motivo }),
  eliminar: (id: number) => api.delete(`/compras-contab/${id}`),
};

// ── Tesorería (aprobaciones 50% + conciliación bancaria + flujo de caja) — SOLO MonzaParts ──
export const monzaTesoreriaAPI = {
  // Aprobaciones: la ORDEN de los adelantos 50% (destraba Abastecimiento)
  aprobaciones: () => api.get("/tesoreria/aprobaciones"),
  aprobarAdelanto: (cotId: number, data: Record<string, unknown>) =>
    api.post(`/tesoreria/aprobaciones/${cotId}/aprobar`, data),
  // Conciliación bancaria
  cuentas: (incluirInactivas?: boolean) =>
    api.get("/tesoreria/cuentas", { params: incluirInactivas ? { incluir_inactivas: true } : {} }),
  crearCuenta: (data: Record<string, unknown>) => api.post("/tesoreria/cuentas", data),
  actualizarCuenta: (id: number, data: Record<string, unknown>) => api.put(`/tesoreria/cuentas/${id}`, data),
  eliminarCuenta: (id: number) => api.delete(`/tesoreria/cuentas/${id}`),
  importarCartola: (cuentaId: number, file: File, nombre?: string) => {
    const fd = new FormData();
    fd.append("cuenta_id", String(cuentaId));
    if (nombre) fd.append("nombre", nombre);
    fd.append("file", file);
    return api.post("/tesoreria/cartolas/importar", fd);
  },
  cartolas: (cuentaId?: number) =>
    api.get("/tesoreria/cartolas", { params: cuentaId ? { cuenta_id: cuentaId } : {} }),
  eliminarCartola: (id: number) => api.delete(`/tesoreria/cartolas/${id}`),
  movimientos: (params?: Record<string, unknown>) => api.get("/tesoreria/movimientos", { params }),
  crearMovimiento: (data: Record<string, unknown>) => api.post("/tesoreria/movimientos", data),
  eliminarMovimiento: (id: number) => api.delete(`/tesoreria/movimientos/${id}`),
  sugerencias: (movId: number) => api.get(`/tesoreria/movimientos/${movId}/sugerencias`),
  conciliar: (movId: number, data: Record<string, unknown>) =>
    api.post(`/tesoreria/movimientos/${movId}/conciliar`, data),
  desconciliar: (movId: number) => api.post(`/tesoreria/movimientos/${movId}/desconciliar`),
  egresosPendientes: (q?: string) =>
    api.get("/tesoreria/egresos-pendientes", { params: q ? { q } : {} }),
  adelantosPendientes: () => api.get("/tesoreria/adelantos-pendientes"),
  cobranzasPendientes: (q?: string, page = 1) =>
    api.get("/tesoreria/cobranzas-pendientes", { params: { q: q || undefined, page } }),
  // Por pagar / aprobar pagos (Tesorería da la orden → crea el Comprobante de Egreso)
  porPagar: (params?: { q?: string; page?: number; page_size?: number }) =>
    api.get("/tesoreria/por-pagar", { params }),
  aprobarPago: (data: Record<string, unknown>) => api.post("/tesoreria/pagos", data),
  // Flujo de caja + KPIs
  flujoCaja: () => api.get("/tesoreria/flujo-caja"),
  resumen: () => api.get("/tesoreria/resumen"),
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
    return api.post(`/cotizaciones/${cotId}/documento`, form, {
      headers: { "Content-Type": "multipart/form-data" },
    });
  },
  downloadDocumento: (cotId: number) =>
    api.get(`/cotizaciones/${cotId}/documento`, { responseType: "arraybuffer" }),
  // Despacho como entidad (alineación MachParts)
  listos: () => api.get("/despachos/listos"),
  crear: (data: Record<string, unknown>) => api.post("/despachos/crear", data),
  entidades: () => api.get("/despachos/entidades"),
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
    return api.post("/documentos/upload", form, { headers: { "Content-Type": "multipart/form-data" } });
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

// ── Abastecimiento (Panel Compras + Seguimiento) ──────────────────────────────
export const monzaAbastecimientoAPI = {
  kpis: () => api.get("/abastecimiento/kpis"),
  porComprar: (params?: Record<string, unknown>) => api.get("/abastecimiento/por-comprar", { params }),
  seguimiento: (params?: Record<string, unknown>) => api.get("/abastecimiento/seguimiento", { params }),
  comprar: (data: Record<string, unknown>) => api.post("/abastecimiento/comprar", data),
  listOcs: (params?: Record<string, unknown>) => api.get("/abastecimiento/ocs", { params }),
  ocItems: (id: number) => api.get(`/abastecimiento/ocs/${id}/items`),
  updateOc: (id: number, data: Record<string, unknown>) => api.patch(`/abastecimiento/ocs/${id}`, data),
  listProveedores: () => api.get("/abastecimiento/proveedores"),
  createProveedor: (data: Record<string, unknown>) => api.post("/abastecimiento/proveedores", data),
  updateProveedor: (id: number, data: Record<string, unknown>) => api.patch(`/abastecimiento/proveedores/${id}`, data),
  deleteProveedor: (id: number) => api.delete(`/abastecimiento/proveedores/${id}`),
  comprados: (params?: Record<string, unknown>) => api.get("/abastecimiento/comprados", { params }),
  preparar: (item_ids: number[]) => api.post("/abastecimiento/preparar", { item_ids }),
};

// ── Logística (Embarques, alineación MachParts) ───────────────────────────────
export const monzaLogisticaAPI = {
  kpis: () => api.get("/logistica/kpis"),
  preparados: (params?: Record<string, unknown>) => api.get("/logistica/preparados", { params }),
  crearEmbarque: (data: Record<string, unknown>) => api.post("/logistica/embarques", data),
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
  cerrarRecepcion: (recId: number) => api.post(`/bodega/recepciones/${recId}/cerrar`),
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
