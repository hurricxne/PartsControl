import axios from "axios";

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
