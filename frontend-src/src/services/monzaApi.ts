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
  updateOc: (id: number, data: Record<string, unknown>) => api.patch(`/abastecimiento/ocs/${id}`, data),
  listProveedores: () => api.get("/abastecimiento/proveedores"),
  createProveedor: (data: Record<string, unknown>) => api.post("/abastecimiento/proveedores", data),
  updateProveedor: (id: number, data: Record<string, unknown>) => api.patch(`/abastecimiento/proveedores/${id}`, data),
  deleteProveedor: (id: number) => api.delete(`/abastecimiento/proveedores/${id}`),
};

// ── Bodega (recepción física + reclamos) ──────────────────────────────────────
export const monzaBodegaAPI = {
  kpis: () => api.get("/bodega/kpis"),
  porRecibir: (params?: Record<string, unknown>) => api.get("/bodega/por-recibir", { params }),
  enBodega: (params?: Record<string, unknown>) => api.get("/bodega/en-bodega", { params }),
  confirmar: (data: Record<string, unknown>) => api.post("/bodega/confirmar", data),
  listReclamos: (params?: Record<string, unknown>) => api.get("/bodega/reclamos", { params }),
  updateReclamo: (id: number, data: Record<string, unknown>) => api.patch(`/bodega/reclamos/${id}`, data),
  listoDespacho: () => api.get("/bodega/listo-despacho"),
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
