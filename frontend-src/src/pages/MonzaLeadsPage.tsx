import { useState, useEffect, useCallback } from "react";
import { fmtClp } from "../utils/format";
import { Plus, Search, ChevronDown, ChevronRight, Phone, MessageCircle, FileText, Calendar, X, Tag, Clock, Edit2, Trash2, Calculator } from "lucide-react";
import { monzaLeadsAPI, monzaCatalogAPI, monzaCotizacionesAPI, monzaClientesAPI } from "../services/monzaApi";
import MonzaCotizadorModal from "./MonzaCotizadorModal";
import { useMonzaTheme } from "./MonzaLayout";
import toast from "react-hot-toast";

// ── Types ──────────────────────────────────────────────────────────────────
interface Lead {
  id: number; numero: string; estado: string; canal_origen: string;
  vehiculo?: string; marca?: string; modelo?: string; anio?: string; vin?: string; linea?: string; comentario?: string;
  total_estimado: number; items_count: number; asesor_id?: number; asesor?: string;
  proximo_paso?: { tipo: string; cuando?: string } | null;
  sin_contactar_dias: number; fecha_creacion: string; fecha_actualizacion: string;
  cliente?: { id: number; nombre: string; rut?: string; telefono?: string; email?: string; vehiculos?: unknown[]; etiquetas?: string[]; ltv?: number; leads_total?: number; vendidos_total?: number };
  items?: Item[]; actividades?: Actividad[]; proximos_pasos?: ProximoPaso[]; cotizaciones?: unknown[];
}
interface Item { id: number; descripcion: string; numero_parte?: string; marca?: string; procedencia?: string; calidad: string; cantidad: number; precio_clp?: number; plazo_entrega?: string; }
interface Actividad { id: number; tipo: string; descripcion: string; usuario: string; fecha: string; }
interface ProximoPaso { id: number; tipo: string; cuando?: string; asesor?: string; completado: boolean; }
interface KPIs { nuevos_mes: number; en_proceso: number; vendidos_mes: number; tasa_cierre_pct: number; total_cotizado_mes: number; sin_contactar_3d: number; pendientes: number; en_trabajo: number; }

// ── Utilities ──────────────────────────────────────────────────────────────
const ESTADO_COLORS: Record<string, { bg: string; color: string; label: string }> = {
  pendiente: { bg: "#FEF9C3", color: "#854D0E", label: "Pendiente" },
  en_proceso: { bg: "#DBEAFE", color: "#1E40AF", label: "En proceso" },
  cerrado: { bg: "#DCFCE7", color: "#166534", label: "Cerrado / Ganado" },
  vendido: { bg: "#DCFCE7", color: "#166534", label: "Cerrado / Ganado" },
  rechazado: { bg: "#FEE2E2", color: "#991B1B", label: "Rechazado" },
};
const CALIDAD_LABEL: Record<string, string> = { sin_calificar: "Sin calificar", genuine: "Genuine", oem: "OEM", aftermarket: "Aftermarket" };
function timeSince(dateStr: string) {
  const diff = Date.now() - new Date(dateStr).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 60) return `Hace ${mins}m`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `Hace ${hrs}h`;
  return `Hace ${Math.floor(hrs / 24)}d`;
}

// ── KPI Card ──────────────────────────────────────────────────────────────
function KpiCard({ label, value, sub, accent = "var(--monza-accent)" }: { label: string; value: string | number; sub?: string; accent?: string }) {
  const { dark } = useMonzaTheme();
  return (
    <div style={{
      background: dark ? "#131b3e" : "white",
      borderRadius: 12,
      border: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}`,
      padding: "18px 20px",
      minWidth: 140,
      flex: 1,
    }}>
      <div style={{ width: 28, height: 4, borderRadius: 2, background: accent, marginBottom: 12 }} />
      <div style={{ fontSize: 28, fontWeight: 800, color: dark ? "white" : "#1E293B", lineHeight: 1 }}>{value}</div>
      <div style={{ fontSize: 12, color: dark ? "#8899cc" : "#64748B", marginTop: 5 }}>{label}</div>
      {sub && <div style={{ fontSize: 11, color: dark ? "#475569" : "#94A3B8", marginTop: 3 }}>{sub}</div>}
    </div>
  );
}

// ── Nuevo Lead Modal ───────────────────────────────────────────────────────
interface ClienteSug { id: number; nombre: string; rut?: string; telefono?: string; email?: string }

function NuevoLeadModal({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const [form, setForm] = useState({ canal_origen: "WhatsApp", telefono: "", nombre: "", email: "", rut: "", marca: "", modelo: "", anio: "", vin: "", comentario: "" });
  const [items, setItems] = useState([{ descripcion: "", numero_parte: "", marca: "", cantidad: 1 }]);
  const [saving, setSaving] = useState(false);
  // Búsqueda cliente existente
  const [clienteQ, setClienteQ] = useState("");
  const [clienteResults, setClienteResults] = useState<ClienteSug[]>([]);
  const [clienteLoading, setClienteLoading] = useState(false);
  const [showClienteDrop, setShowClienteDrop] = useState(false);
  const [clienteSeleccionado, setClienteSeleccionado] = useState<ClienteSug | null>(null);
  const [showCrearCliente, setShowCrearCliente] = useState(false);
  const [savingCliente, setSavingCliente] = useState(false);
  const [nuevoCliente, setNuevoCliente] = useState({ nombre: "", rut: "", telefono: "", email: "" });

  const set = (f: string, v: string) => setForm((p) => ({ ...p, [f]: v }));
  const setItem = (i: number, f: string, v: string | number) => {
    setItems((p) => { const n = [...p]; n[i] = { ...n[i], [f]: v }; return n; });
  };
  const addItem = () => setItems((p) => [...p, { descripcion: "", numero_parte: "", marca: "", cantidad: 1 }]);

  // Debounced client search
  useEffect(() => {
    if (clienteQ.length < 2) { setClienteResults([]); return; }
    const t = setTimeout(async () => {
      setClienteLoading(true);
      try {
        const res = await monzaLeadsAPI.searchClientes(clienteQ);
        setClienteResults(res.data as ClienteSug[]);
      } catch { setClienteResults([]); }
      finally { setClienteLoading(false); }
    }, 300);
    return () => clearTimeout(t);
  }, [clienteQ]);

  const selectCliente = (c: ClienteSug) => {
    setClienteSeleccionado(c);
    setForm(f => ({
      ...f,
      nombre: c.nombre,
      telefono: c.telefono || f.telefono,
      email: c.email || f.email,
      rut: c.rut || f.rut,
    }));
    setClienteQ("");
    setClienteResults([]);
    setShowClienteDrop(false);
  };

  const deselectCliente = () => {
    setClienteSeleccionado(null);
    setForm(f => ({ ...f, nombre: "", telefono: "", email: "", rut: "" }));
  };

  const crearYSeleccionarCliente = async () => {
    if (!nuevoCliente.nombre.trim()) { toast.error("Nombre requerido"); return; }
    setSavingCliente(true);
    try {
      const res = await monzaClientesAPI.create(nuevoCliente);
      const c = res.data as ClienteSug;
      selectCliente(c);
      setShowCrearCliente(false);
      setNuevoCliente({ nombre: "", rut: "", telefono: "", email: "" });
      toast.success(`Cliente "${c.nombre}" creado y vinculado`);
    } catch { toast.error("Error al crear el cliente"); }
    finally { setSavingCliente(false); }
  };

  const submit = async () => {
    if (!form.telefono && !form.nombre && !clienteSeleccionado) { toast.error("Ingresa al menos teléfono o nombre"); return; }
    if (!form.marca.trim() || !form.modelo.trim() || !form.anio.trim() || !form.vin.trim()) {
      toast.error("Marca, modelo, año y VIN son obligatorios"); return;
    }
    setSaving(true);
    try {
      const payload: Record<string, unknown> = {
        canal_origen: form.canal_origen,
        vehiculo: `${form.marca} ${form.modelo}`.trim(),
        marca: form.marca,
        modelo: form.modelo,
        anio: form.anio,
        vin: form.vin,
        comentario: form.comentario,
        items: items.filter((it) => it.descripcion.trim()),
      };
      if (clienteSeleccionado) {
        payload.cliente_id = clienteSeleccionado.id;
      } else {
        payload.cliente = { nombre: form.nombre || form.telefono, telefono: form.telefono, email: form.email, rut: form.rut };
      }
      await monzaLeadsAPI.create(payload);
      toast.success("Lead creado");
      onCreated();
      onClose();
    } catch { toast.error("Error al crear lead"); }
    finally { setSaving(false); }
  };

  const inputSt = { width: "100%", padding: "8px 10px", border: "1px solid #D1D5DB", borderRadius: 6, fontSize: 13, boxSizing: "border-box" as const, background: "white", color: "#1E293B" };
  const labelSt = { fontSize: 11, fontWeight: 600, color: "#64748B", display: "block", marginBottom: 4 };

  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)", zIndex: 1000, display: "flex", alignItems: "center", justifyContent: "center", padding: 16 }}>
      <div style={{ background: "white", borderRadius: 12, width: "100%", maxWidth: 600, maxHeight: "92vh", overflowY: "auto", boxShadow: "0 20px 60px rgba(0,0,0,0.3)" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "16px 20px", borderBottom: "1px solid #F1F5F9" }}>
          <div>
            <h3 style={{ margin: 0, fontSize: 16, fontWeight: 700, color: "#1E293B" }}>Nuevo lead</h3>
            <p style={{ margin: "2px 0 0", fontSize: 12, color: "#64748B" }}>Captura el lead rápido. Basta con teléfono, nombre o email — el resto puede completarse después.</p>
          </div>
          <button onClick={onClose} style={{ background: "transparent", border: "none", cursor: "pointer", color: "#94A3B8" }}><X size={18} /></button>
        </div>
        <div style={{ padding: "20px" }}>

          {/* ── Búsqueda cliente existente ── */}
          <div style={{ marginBottom: 16, background: "#F0FFF4", border: "1px solid #BBF7D0", borderRadius: 8, padding: "10px 12px" }}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 6 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <Search size={12} color="#166534" />
                <span style={{ fontSize: 11, fontWeight: 700, color: "#166534" }}>CLIENTE</span>
              </div>
              {!clienteSeleccionado && (
                <button
                  onClick={() => { setShowCrearCliente(c => !c); setNuevoCliente({ nombre: clienteQ, rut: "", telefono: "", email: "" }); setShowClienteDrop(false); }}
                  style={{ fontSize: 11, padding: "3px 10px", border: "1px solid var(--monza-accent)", borderRadius: 6, background: showCrearCliente ? "var(--monza-accent)" : "white", color: showCrearCliente ? "white" : "var(--monza-accent)", cursor: "pointer", fontWeight: 600, display: "flex", alignItems: "center", gap: 4 }}>
                  <Plus size={11} /> Crear cliente nuevo
                </button>
              )}
            </div>
            {clienteSeleccionado ? (
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", background: "#DCFCE7", borderRadius: 6, padding: "8px 12px" }}>
                <div>
                  <div style={{ fontWeight: 700, fontSize: 13, color: "#166534" }}>{clienteSeleccionado.nombre}</div>
                  <div style={{ fontSize: 11, color: "#15803D" }}>{[clienteSeleccionado.telefono, clienteSeleccionado.email, clienteSeleccionado.rut].filter(Boolean).join(" · ")}</div>
                </div>
                <button onClick={deselectCliente} style={{ background: "transparent", border: "none", cursor: "pointer", color: "#16A34A", fontSize: 18, lineHeight: 1 }} title="Desvincular cliente">×</button>
              </div>
            ) : (
              <div style={{ position: "relative" }}>
                <input
                  style={{ ...inputSt, background: "white", fontSize: 12 }}
                  value={clienteQ}
                  onChange={e => { setClienteQ(e.target.value); setShowClienteDrop(true); setShowCrearCliente(false); }}
                  onFocus={() => setShowClienteDrop(true)}
                  placeholder="Buscar cliente existente por nombre, teléfono, RUT o email..."
                />
                {showClienteDrop && (clienteResults.length > 0 || clienteLoading || (clienteQ.length >= 2 && !clienteLoading)) && (
                  <div style={{ position: "absolute", top: "100%", left: 0, right: 0, background: "white", border: "1px solid #D1D5DB", borderRadius: 6, boxShadow: "0 8px 20px rgba(0,0,0,0.12)", zIndex: 200, maxHeight: 200, overflowY: "auto", marginTop: 2 }}>
                    {clienteLoading && <div style={{ padding: "10px 12px", fontSize: 12, color: "#94A3B8" }}>Buscando...</div>}
                    {clienteResults.map(c => (
                      <button key={c.id} onClick={() => selectCliente(c)}
                        style={{ width: "100%", textAlign: "left", padding: "9px 12px", border: "none", background: "transparent", cursor: "pointer", borderBottom: "1px solid #F1F5F9" }}
                        onMouseEnter={e => (e.currentTarget.style.background = "#F0FFF4")}
                        onMouseLeave={e => (e.currentTarget.style.background = "transparent")}>
                        <div style={{ fontSize: 13, fontWeight: 600, color: "#1E293B" }}>{c.nombre}</div>
                        <div style={{ fontSize: 11, color: "#64748B", marginTop: 1 }}>{[c.telefono, c.email, c.rut && `RUT ${c.rut}`].filter(Boolean).join(" · ")}</div>
                      </button>
                    ))}
                    {!clienteLoading && clienteResults.length === 0 && clienteQ.length >= 2 && (
                      <div style={{ padding: "10px 12px", fontSize: 12, color: "#94A3B8" }}>No se encontró ningún cliente con ese criterio.</div>
                    )}
                  </div>
                )}
              </div>
            )}
          </div>

          {/* ── Formulario crear cliente nuevo (inline) ── */}
          {showCrearCliente && (
            <div style={{ marginBottom: 16, background: "#FFF9F9", border: "1px solid #FCA5A5", borderRadius: 8, padding: "12px 14px" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
                <span style={{ fontSize: 12, fontWeight: 700, color: "#991B1B" }}>CREAR CLIENTE NUEVO</span>
                <button onClick={() => setShowCrearCliente(false)} style={{ background: "transparent", border: "none", cursor: "pointer", color: "#94A3B8" }}><X size={14} /></button>
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, marginBottom: 8 }}>
                <div><label style={labelSt}>Nombre *</label><input style={inputSt} value={nuevoCliente.nombre} onChange={e => setNuevoCliente(n => ({ ...n, nombre: e.target.value }))} placeholder="JUAN PÉREZ" /></div>
                <div><label style={labelSt}>RUT</label><input style={inputSt} value={nuevoCliente.rut} onChange={e => setNuevoCliente(n => ({ ...n, rut: e.target.value }))} placeholder="12.345.678-9" /></div>
                <div><label style={labelSt}>Teléfono</label><input style={inputSt} value={nuevoCliente.telefono} onChange={e => setNuevoCliente(n => ({ ...n, telefono: e.target.value }))} placeholder="+56 9..." /></div>
                <div><label style={labelSt}>Email</label><input style={inputSt} type="email" value={nuevoCliente.email} onChange={e => setNuevoCliente(n => ({ ...n, email: e.target.value }))} /></div>
              </div>
              <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
                <button onClick={() => setShowCrearCliente(false)} style={{ padding: "6px 14px", border: "1px solid #E2E8F0", borderRadius: 6, background: "white", cursor: "pointer", fontSize: 12, color: "#475569" }}>Cancelar</button>
                <button onClick={crearYSeleccionarCliente} disabled={savingCliente}
                  style={{ padding: "6px 14px", border: "none", borderRadius: 6, background: "var(--monza-accent)", cursor: "pointer", fontSize: 12, color: "white", fontWeight: 600, opacity: savingCliente ? 0.7 : 1 }}>
                  {savingCliente ? "Creando..." : "Crear y vincular"}
                </button>
              </div>
            </div>
          )}

          {/* Canal */}
          <div style={{ marginBottom: 16 }}>
            <label style={labelSt}>Canal de origen</label>
            <select value={form.canal_origen} onChange={(e) => set("canal_origen", e.target.value)} style={{ ...inputSt }}>
              {["WhatsApp","Llamada","Email","Web","Directo","Instagram","Referido"].map((o) => <option key={o} style={{ background: "white", color: "#1E293B" }}>{o}</option>)}
            </select>
          </div>
          {/* Cliente (datos manuales — deshabilitados si hay cliente seleccionado) */}
          <div style={{ marginBottom: 16, opacity: clienteSeleccionado ? 0.45 : 1, pointerEvents: clienteSeleccionado ? "none" : "auto" }}>
            <label style={labelSt}>
              {clienteSeleccionado ? "Datos del cliente (vinculado)" : "Datos del cliente nuevo"}{" "}
              <span style={{ color: "#94A3B8", fontWeight: 400 }}>(al menos un dato de contacto)</span>
            </label>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
              <div><label style={labelSt}>Teléfono</label><input style={inputSt} value={form.telefono} onChange={(e) => set("telefono", e.target.value)} placeholder="+56 9..." /></div>
              <div><label style={labelSt}>Nombre</label><input style={inputSt} value={form.nombre} onChange={(e) => set("nombre", e.target.value)} placeholder="JUAN PÉREZ" /></div>
              <div><label style={labelSt}>Email</label><input style={inputSt} value={form.email} onChange={(e) => set("email", e.target.value)} /></div>
              <div><label style={labelSt}>RUT</label><input style={inputSt} value={form.rut} onChange={(e) => set("rut", e.target.value)} placeholder="12.345.678-9" /></div>
            </div>
          </div>
          {/* Vehículo (obligatorio) */}
          <div style={{ marginBottom: 16 }}>
            <label style={labelSt}>Vehículo <span style={{ color: "#EF4444" }}>* obligatorio</span></label>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
              <div><label style={labelSt}>Marca *</label><input style={inputSt} value={form.marca} onChange={(e) => set("marca", e.target.value)} placeholder="AUDI" /></div>
              <div><label style={labelSt}>Modelo *</label><input style={inputSt} value={form.modelo} onChange={(e) => set("modelo", e.target.value)} placeholder="A4 2.0 TFSI" /></div>
              <div><label style={labelSt}>Año *</label><input style={inputSt} value={form.anio} onChange={(e) => set("anio", e.target.value)} placeholder="2018" /></div>
              <div><label style={labelSt}>VIN *</label><input style={inputSt} value={form.vin} onChange={(e) => set("vin", e.target.value)} placeholder="WAUZZZ..." /></div>
            </div>
          </div>
          {/* Items */}
          <div style={{ marginBottom: 16 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
              <label style={labelSt}>Repuestos solicitados</label>
              <span style={{ fontSize: 11, color: "#94A3B8" }}>{items.length} repuesto(s)</span>
            </div>
            {items.map((it, i) => (
              <div key={i} style={{ border: "1px solid #E2E8F0", borderRadius: 8, padding: "12px", marginBottom: 8 }}>
                <div style={{ fontSize: 11, fontWeight: 600, color: "var(--monza-accent)", marginBottom: 8 }}>Repuesto #{i + 1}</div>
                <div style={{ display: "grid", gridTemplateColumns: "3fr 1fr", gap: 8 }}>
                  <div><label style={labelSt}>Descripción</label><input style={inputSt} value={it.descripcion} onChange={(e) => setItem(i, "descripcion", e.target.value)} placeholder="Pastillas freno delanteras" /></div>
                  <div><label style={labelSt}>Cantidad</label><input type="number" style={inputSt} value={it.cantidad} onChange={(e) => setItem(i, "cantidad", Number(e.target.value))} min={1} /></div>
                </div>
              </div>
            ))}
            <button onClick={addItem} style={{ display: "flex", alignItems: "center", gap: 4, background: "transparent", border: "none", cursor: "pointer", color: "var(--monza-accent)", fontSize: 13, fontWeight: 500 }}>
              <Plus size={14} /> Agregar otro repuesto
            </button>
          </div>
          {/* Comentario */}
          <div style={{ marginBottom: 20 }}>
            <label style={labelSt}>Comentario</label>
            <textarea style={{ ...inputSt, resize: "vertical" }} rows={2} value={form.comentario} onChange={(e) => set("comentario", e.target.value)} />
          </div>
          <div style={{ display: "flex", gap: 10, justifyContent: "flex-end" }}>
            <button onClick={onClose} style={{ padding: "9px 18px", border: "1px solid #E2E8F0", borderRadius: 8, background: "white", cursor: "pointer", fontSize: 13, color: "#475569" }}>Cancelar</button>
            <button onClick={submit} disabled={saving} style={{ padding: "9px 18px", border: "none", borderRadius: 8, background: "var(--monza-accent)", cursor: "pointer", fontSize: 13, color: "white", fontWeight: 600, opacity: saving ? 0.7 : 1 }}>
              {saving ? "Creando..." : "Crear lead"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Agregar Item Modal (con búsqueda en catálogo CARAT) ───────────────────
interface CatalogParte { id: number; sku: string; descripcion: string; marca?: string; calidad: string; costo: number; moneda: string; lista?: { code: string } }

function AgregarItemModal({ leadId, onClose, onAdded }: { leadId: number; onClose: () => void; onAdded: () => void }) {
  const [form, setForm] = useState({ descripcion: "", numero_parte: "", marca: "", procedencia: "", calidad: "sin_calificar", cantidad: 1 });
  const [saving, setSaving] = useState(false);
  const [catalogQ, setCatalogQ] = useState("");
  const [catalogResults, setCatalogResults] = useState<CatalogParte[]>([]);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [showCatalog, setShowCatalog] = useState(false);

  const set = (f: string, v: string | number) => setForm((p) => ({ ...p, [f]: v }));

  // Búsqueda en catálogo con debounce
  useEffect(() => {
    if (catalogQ.length < 2) { setCatalogResults([]); return; }
    const t = setTimeout(async () => {
      setCatalogLoading(true);
      try {
        const res = await monzaCatalogAPI.search({ q: catalogQ, page_size: 8 } as { q: string; page_size: number });
        setCatalogResults((res.data as { items: CatalogParte[] }).items || []);
      } catch { setCatalogResults([]); }
      finally { setCatalogLoading(false); }
    }, 300);
    return () => clearTimeout(t);
  }, [catalogQ]);

  const selectCatalogParte = (p: CatalogParte) => {
    const calMap: Record<string, string> = { GENUINO: "genuine", OEM: "oem", AFTERMARKET: "aftermarket" };
    setForm(f => ({
      ...f,
      descripcion: f.descripcion.trim() || p.descripcion,
      numero_parte: p.sku,
      marca: p.marca || "",
      procedencia: p.lista?.code?.split("-")[0] || "",
      calidad: calMap[p.calidad] || "genuine",
    }));
    setCatalogQ("");
    setCatalogResults([]);
    setShowCatalog(false);
  };

  const submit = async () => {
    if (!form.descripcion.trim()) { toast.error("Descripción requerida"); return; }
    setSaving(true);
    try {
      await monzaLeadsAPI.addItem(leadId, form);
      toast.success("Repuesto agregado");
      onAdded();
      onClose();
    } catch { toast.error("Error al agregar repuesto"); }
    finally { setSaving(false); }
  };

  const inputSt = { width: "100%", padding: "8px 10px", border: "1px solid #D1D5DB", borderRadius: 6, fontSize: 13, boxSizing: "border-box" as const, background: "white", color: "#1E293B" };
  const labelSt = { fontSize: 11, fontWeight: 600, color: "#64748B", display: "block", marginBottom: 4 };

  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.4)", zIndex: 1001, display: "flex", alignItems: "center", justifyContent: "center", padding: 16 }}>
      <div style={{ background: "white", borderRadius: 12, width: "100%", maxWidth: 520, boxShadow: "0 20px 60px rgba(0,0,0,0.3)" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", padding: "16px 20px", borderBottom: "1px solid #F1F5F9" }}>
          <div><h3 style={{ margin: 0, fontSize: 15, fontWeight: 700, color: "#1E293B" }}>Agregar repuesto</h3>
            <p style={{ margin: "3px 0 0", fontSize: 12, color: "#64748B" }}>Agrega un ítem al lead. El precio se define con la calculadora.</p></div>
          <button onClick={onClose} style={{ background: "transparent", border: "none", cursor: "pointer", color: "#94A3B8" }}><X size={18} /></button>
        </div>
        <div style={{ padding: 20 }}>
          {/* Búsqueda en catálogo CARAT */}
          <div style={{ marginBottom: 16, background: "#F8FAFC", borderRadius: 8, padding: "10px 12px", border: "1px solid #E2E8F0" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
              <Search size={13} color="#64748B" />
              <span style={{ fontSize: 11, fontWeight: 600, color: "#64748B" }}>BUSCAR EN CATÁLOGO CARAT (opcional)</span>
            </div>
            <div style={{ position: "relative" }}>
              <input
                style={{ ...inputSt, background: "white", fontSize: 12 }}
                value={catalogQ}
                onChange={(e) => { setCatalogQ(e.target.value); setShowCatalog(true); }}
                placeholder="SKU, descripción o marca (ej: 30728784, FILTRO, VOLVO)..."
                onFocus={() => setShowCatalog(true)}
              />
              {showCatalog && (catalogResults.length > 0 || catalogLoading) && (
                <div style={{ position: "absolute", top: "100%", left: 0, right: 0, background: "white", border: "1px solid #D1D5DB", borderRadius: 6, boxShadow: "0 8px 20px rgba(0,0,0,0.15)", zIndex: 100, maxHeight: 200, overflowY: "auto", marginTop: 2 }}>
                  {catalogLoading && <div style={{ padding: "10px 12px", fontSize: 12, color: "#94A3B8" }}>Buscando...</div>}
                  {catalogResults.map(p => (
                    <button key={p.id} onClick={() => selectCatalogParte(p)}
                      style={{ width: "100%", textAlign: "left", padding: "8px 12px", border: "none", background: "transparent", cursor: "pointer", borderBottom: "1px solid #F1F5F9" }}
                      onMouseEnter={e => (e.currentTarget.style.background = "#F8FAFC")}
                      onMouseLeave={e => (e.currentTarget.style.background = "transparent")}>
                      <div style={{ fontSize: 12, fontWeight: 600, color: "#1E293B" }}>{p.sku} {p.marca ? `· ${p.marca}` : ""}</div>
                      <div style={{ fontSize: 11, color: "#64748B", marginTop: 1 }}>{p.descripcion.slice(0, 60)} · {p.calidad} · {p.costo} {p.moneda}</div>
                    </button>
                  ))}
                  {!catalogLoading && catalogResults.length === 0 && catalogQ.length >= 2 && (
                    <div style={{ padding: "10px 12px", fontSize: 12, color: "#94A3B8" }}>Sin resultados en catálogo</div>
                  )}
                </div>
              )}
            </div>
          </div>

          {/* Formulario manual */}
          <div style={{ marginBottom: 12 }}><label style={labelSt}>Descripción *</label><input style={inputSt} value={form.descripcion} onChange={(e) => set("descripcion", e.target.value)} placeholder="BRAZO DERECHO" autoFocus /></div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 12 }}>
            <div><label style={labelSt}>N° parte</label><input style={inputSt} value={form.numero_parte} onChange={(e) => set("numero_parte", e.target.value)} /></div>
            <div><label style={labelSt}>Marca</label><input style={inputSt} value={form.marca} onChange={(e) => set("marca", e.target.value)} /></div>
            <div><label style={labelSt}>Procedencia</label><input style={inputSt} value={form.procedencia} onChange={(e) => set("procedencia", e.target.value)} placeholder="Alemania / China / ..." /></div>
            <div><label style={labelSt}>Calidad</label>
              <select value={form.calidad} onChange={(e) => set("calidad", e.target.value)} style={{ ...inputSt, background: "white", color: "#1E293B" }}>
                <option value="sin_calificar" style={{ background: "white", color: "#1E293B" }}>Sin calificar</option>
                <option value="genuine" style={{ background: "white", color: "#1E293B" }}>Genuine</option>
                <option value="oem" style={{ background: "white", color: "#1E293B" }}>OEM</option>
                <option value="aftermarket" style={{ background: "white", color: "#1E293B" }}>Aftermarket</option>
              </select>
            </div>
          </div>
          <div style={{ marginBottom: 20 }}><label style={labelSt}>Cantidad</label><input type="number" style={{ ...inputSt, width: 120 }} value={form.cantidad} onChange={(e) => set("cantidad", Number(e.target.value))} min={1} /></div>
          <div style={{ display: "flex", gap: 10, justifyContent: "flex-end" }}>
            <button onClick={onClose} style={{ padding: "9px 18px", border: "1px solid #E2E8F0", borderRadius: 8, background: "white", cursor: "pointer", fontSize: 13, color: "#475569" }}>Cancelar</button>
            <button onClick={submit} disabled={saving} style={{ padding: "9px 18px", border: "none", borderRadius: 8, background: "var(--monza-accent)", cursor: "pointer", fontSize: 13, color: "white", fontWeight: 600, opacity: saving ? 0.7 : 1 }}>
              {saving ? "Agregando..." : "Agregar"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Emitir Cotización Modal ───────────────────────────────────────────────
function EmitirCotizacionModal({ lead, detail, onClose, onEmitted }: { lead: Lead; detail: Lead; onClose: () => void; onEmitted: () => void }) {
  const allItems = detail.items || [];
  const itemsConPrecio = allItems.filter((it) => it.precio_clp);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set(itemsConPrecio.map((it) => it.id)));
  const [formaPago, setFormaPago] = useState("30 días contra factura");
  const [tipoCotizacion, setTipoCotizacion] = useState("Importación de Repuestos");
  const [ocCliente, setOcCliente] = useState("");
  const [loading, setLoading] = useState(false);
  const [condicionesServicio, setCondicionesServicio] = useState("");
  // Auto-fill desde los datos del lead (marca/modelo/año/VIN ya capturados)
  const [marcaV, setMarcaV] = useState(() => detail.marca || (detail.vehiculo || "").split(" ")[0] || "");
  const [modeloV, setModeloV] = useState(() => detail.modelo || (detail.vehiculo || "").split(" ").slice(1).join(" ") || "");
  const [vinV, setVinV] = useState(detail.vin || "");
  const [anioV, setAnioV] = useState(detail.anio || "");

  const toggleItem = (id: number) =>
    setSelectedIds((prev) => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n; });

  const selectedItems = allItems.filter((it) => selectedIds.has(it.id));
  const totalNeto = selectedItems.reduce((s, it) => s + (it.precio_clp || 0) * it.cantidad, 0);
  const iva = Math.round(totalNeto * 0.19);
  const totalBruto = totalNeto + iva;

  const submit = async () => {
    if (selectedItems.length === 0) { toast.error("Selecciona al menos un ítem"); return; }
    if (!detail.cliente) { toast.error("El lead no tiene cliente vinculado"); return; }
    setLoading(true);
    try {
      const payload: Record<string, unknown> = {
        lead_id: lead.id,
        cliente_id: detail.cliente.id,
        vehiculo: [marcaV, modeloV].filter(Boolean).join(" ") || undefined,
        vin: vinV || undefined,
        anio: anioV || undefined,
        tipo_cotizacion: tipoCotizacion,
        forma_pago: formaPago,
        oc_cliente: ocCliente || undefined,
        condiciones_servicio: condicionesServicio || undefined,
        items: selectedItems.map((it) => ({
          descripcion: it.descripcion,
          numero_parte: it.numero_parte || undefined,
          marca: it.marca || undefined,
          procedencia: it.procedencia || undefined,
          calidad: it.calidad,
          cantidad: it.cantidad,
          precio_unitario_clp: it.precio_clp,
          subtotal_clp: (it.precio_clp || 0) * it.cantidad,
          plazo_entrega: it.plazo_entrega || undefined,
        })),
      };
      const res = await monzaCotizacionesAPI.create(payload);
      const cot = res.data as { id: number; numero: string };
      toast.success(`Cotización ${cot.numero} creada — descargando PDF...`);

      // Descargar PDF con arraybuffer
      const pdfRes = await monzaCotizacionesAPI.downloadPdf(cot.id);
      const blob = new Blob([pdfRes.data], { type: "application/pdf" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `Cotizacion_${cot.numero}.pdf`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(url), 1000);

      onEmitted();
      onClose();
    } catch { toast.error("Error al emitir la cotización"); }
    finally { setLoading(false); }
  };

  const inputSt = { width: "100%", padding: "8px 10px", border: "1px solid #D1D5DB", borderRadius: 6, fontSize: 13, boxSizing: "border-box" as const, background: "white", color: "#1E293B" };
  const labelSt = { fontSize: 11, fontWeight: 600 as const, color: "#64748B", display: "block", marginBottom: 4 };

  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.55)", zIndex: 1010, display: "flex", alignItems: "center", justifyContent: "center", padding: 16 }}>
      <div style={{ background: "white", borderRadius: 14, width: "100%", maxWidth: 640, maxHeight: "92vh", overflowY: "auto", boxShadow: "0 24px 64px rgba(0,0,0,0.35)" }}>

        {/* Header */}
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "16px 20px", borderBottom: "1px solid #F1F5F9", background: "#F0F9FF" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <div style={{ background: "#0EA5E9", borderRadius: 8, padding: "6px 8px", display: "flex" }}><FileText size={16} color="white" /></div>
            <div>
              <h3 style={{ margin: 0, fontSize: 15, fontWeight: 700, color: "#0C4A6E" }}>Emitir cotización</h3>
              <p style={{ margin: "2px 0 0", fontSize: 12, color: "#0369A1" }}>Lead {lead.numero} · {detail.cliente?.nombre || "Sin cliente"}</p>
            </div>
          </div>
          <button onClick={onClose} style={{ background: "transparent", border: "none", cursor: "pointer", color: "#94A3B8" }}><X size={18} /></button>
        </div>

        <div style={{ padding: "20px" }}>
          {/* Items */}
          <div style={{ marginBottom: 18 }}>
            <div style={{ fontSize: 11, fontWeight: 700, color: "#64748B", textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 8 }}>
              Ítems a cotizar
              {itemsConPrecio.length < allItems.length && (
                <span style={{ marginLeft: 8, fontSize: 10, color: "#F59E0B", background: "#FEF3C7", padding: "2px 8px", borderRadius: 8, fontWeight: 600 }}>
                  {allItems.length - itemsConPrecio.length} sin precio — no se pueden incluir
                </span>
              )}
            </div>
            {allItems.length === 0 ? (
              <div style={{ padding: "14px", background: "#FFF5F5", borderRadius: 8, color: "#991B1B", fontSize: 13 }}>
                No hay ítems en este lead. Agrega repuestos primero.
              </div>
            ) : (
              <div style={{ border: "1px solid #E2E8F0", borderRadius: 8, overflow: "hidden" }}>
                {allItems.map((it, idx) => {
                  const hasPrecio = !!it.precio_clp;
                  const isSelected = selectedIds.has(it.id);
                  return (
                    <div key={it.id} style={{
                      display: "flex", alignItems: "center", gap: 10, padding: "10px 12px",
                      borderBottom: idx < allItems.length - 1 ? "1px solid #F1F5F9" : "none",
                      background: !hasPrecio ? "#FAFAFA" : isSelected ? "#F0F9FF" : "white",
                      opacity: hasPrecio ? 1 : 0.5,
                    }}>
                      <input type="checkbox" checked={isSelected && hasPrecio} disabled={!hasPrecio}
                        onChange={() => hasPrecio && toggleItem(it.id)}
                        style={{ width: 15, height: 15, cursor: hasPrecio ? "pointer" : "not-allowed", accentColor: "#0EA5E9" }} />
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ fontSize: 13, fontWeight: 600, color: "#1E293B", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{it.descripcion}</div>
                        <div style={{ fontSize: 11, color: "#64748B", marginTop: 1 }}>
                          {[it.numero_parte, it.marca, it.calidad !== "sin_calificar" ? it.calidad : null].filter(Boolean).join(" · ")}
                          {" · "}Qty: {it.cantidad}
                        </div>
                      </div>
                      <div style={{ textAlign: "right", flexShrink: 0 }}>
                        {hasPrecio ? (
                          <>
                            <div style={{ fontSize: 13, fontWeight: 700, color: "#0C4A6E" }}>{fmtClp(it.precio_clp! * it.cantidad)}</div>
                            <div style={{ fontSize: 10, color: "#64748B" }}>{fmtClp(it.precio_clp!)} × {it.cantidad}</div>
                          </>
                        ) : (
                          <span style={{ fontSize: 10, background: "#FEF3C7", color: "#D97706", padding: "2px 7px", borderRadius: 6, fontWeight: 600 }}>Sin precio</span>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          {/* Totales preview */}
          {selectedItems.length > 0 && (
            <div style={{ background: "#F0F9FF", border: "1px solid #BAE6FD", borderRadius: 8, padding: "12px 16px", marginBottom: 18 }}>
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, color: "#0369A1", marginBottom: 4 }}>
                <span>Neto ({selectedItems.length} ítem{selectedItems.length !== 1 ? "s" : ""})</span>
                <strong>{fmtClp(totalNeto)}</strong>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, color: "#0369A1", marginBottom: 6 }}>
                <span>IVA (19%)</span>
                <strong>{fmtClp(iva)}</strong>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: 15, fontWeight: 800, color: "#0C4A6E", borderTop: "1px solid #BAE6FD", paddingTop: 6 }}>
                <span>Total bruto</span>
                <span>{fmtClp(totalBruto)}</span>
              </div>
            </div>
          )}

          {/* Vehículo */}
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 14 }}>
            <div>
              <label style={labelSt}>Marca</label>
              <input style={inputSt} value={marcaV} onChange={(e) => setMarcaV(e.target.value)} placeholder="TOYOTA" />
            </div>
            <div>
              <label style={labelSt}>Modelo</label>
              <input style={inputSt} value={modeloV} onChange={(e) => setModeloV(e.target.value)} placeholder="HILUX 2.4 4WD" />
            </div>
            <div>
              <label style={labelSt}>VIN (opcional)</label>
              <input style={inputSt} value={vinV} onChange={(e) => setVinV(e.target.value)} placeholder="JTFBF5J18FK..." />
            </div>
            <div>
              <label style={labelSt}>Año (opcional)</label>
              <input style={inputSt} value={anioV} onChange={(e) => setAnioV(e.target.value)} placeholder="2021" />
            </div>
          </div>

          {/* Opciones */}
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 14 }}>
            <div>
              <label style={labelSt}>Forma de pago</label>
              <select value={formaPago} onChange={(e) => setFormaPago(e.target.value)} style={{ ...inputSt }}>
                {["30 días contra factura","60 días contra factura","Pago anticipado","Transferencia inmediata","Crédito 90 días","A convenir"].map((o) => (
                  <option key={o} style={{ background: "white", color: "#1E293B" }}>{o}</option>
                ))}
              </select>
            </div>
            <div>
              <label style={labelSt}>OC Cliente (opcional)</label>
              <input style={inputSt} value={ocCliente} onChange={(e) => setOcCliente(e.target.value)} placeholder="N° orden de compra" />
            </div>
          </div>
          <div style={{ marginBottom: 14 }}>
            <label style={labelSt}>Tipo de cotización</label>
            <input style={inputSt} value={tipoCotizacion} onChange={(e) => setTipoCotizacion(e.target.value)} />
          </div>
          <div style={{ marginBottom: 20 }}>
            <label style={labelSt}>Comentario / Nota personalizada <span style={{ fontWeight: 400, color: "#94A3B8" }}>(aparece en la cotización)</span></label>
            <textarea
              style={{ ...inputSt, resize: "vertical" as const, minHeight: 64 }}
              rows={3}
              value={condicionesServicio}
              onChange={(e) => setCondicionesServicio(e.target.value)}
              placeholder="Por solicitud del cliente se presenta cotización... (opcional — reemplaza las condiciones por defecto)"
            />
          </div>

          {/* Acciones */}
          <div style={{ display: "flex", gap: 10, justifyContent: "flex-end" }}>
            <button onClick={onClose} style={{ padding: "10px 18px", border: "1px solid #E2E8F0", borderRadius: 8, background: "white", cursor: "pointer", fontSize: 13, color: "#475569", fontWeight: 500 }}>
              Cancelar
            </button>
            <button onClick={submit} disabled={loading || selectedItems.length === 0}
              style={{ padding: "10px 20px", border: "none", borderRadius: 8, background: loading || selectedItems.length === 0 ? "#94A3B8" : "#0EA5E9", cursor: loading || selectedItems.length === 0 ? "not-allowed" : "pointer", fontSize: 13, color: "white", fontWeight: 700, display: "flex", alignItems: "center", gap: 7 }}>
              <FileText size={14} />{loading ? "Generando PDF..." : `Emitir y descargar PDF (${selectedItems.length} ítem${selectedItems.length !== 1 ? "s" : ""})`}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Agendar Modal ──────────────────────────────────────────────────────────
const TIPO_ICONS: Record<string, string> = { Llamada: "📞", WhatsApp: "💬", Email: "✉️", Visita: "🤝" };

function AgendarModal({ leadId, onClose, onDone }: { leadId: number; onClose: () => void; onDone: () => void }) {
  const { dark } = useMonzaTheme();
  const [tipo, setTipo] = useState("Llamada");
  const [cuando, setCuando] = useState("");
  const [saving, setSaving] = useState(false);

  const bg    = dark ? "#131b3e" : "white";
  const bd    = dark ? "#1e2a4a" : "#E2E8F0";
  const text  = dark ? "white"   : "#1E293B";
  const sub   = dark ? "#8899cc" : "#64748B";
  const inpSt = { width: "100%", padding: "9px 11px", border: `1px solid ${dark ? "#1e2a4a" : "#D1D5DB"}`, borderRadius: 8, fontSize: 13, background: dark ? "#0d1321" : "white", color: text, boxSizing: "border-box" as const, outline: "none" };

  const submit = async () => {
    if (!cuando) { toast.error("Selecciona fecha y hora"); return; }
    setSaving(true);
    try {
      await monzaLeadsAPI.agendarPaso(leadId, { tipo: tipo.toLowerCase(), cuando });
      toast.success("Próximo paso agendado");
      onDone(); onClose();
    } catch { toast.error("Error al agendar"); }
    finally { setSaving(false); }
  };

  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)", zIndex: 1002, display: "flex", alignItems: "center", justifyContent: "center", padding: 16 }}>
      <div style={{ background: bg, borderRadius: 14, width: "100%", maxWidth: 420, boxShadow: "0 24px 64px rgba(0,0,0,0.35)", border: `1px solid ${bd}` }}>
        {/* Header */}
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "16px 20px", borderBottom: `1px solid ${bd}` }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <div style={{ background: "var(--monza-accent)22", borderRadius: 8, padding: "6px 8px" }}>
              <Calendar size={16} className="monza-ic" />
            </div>
            <h3 style={{ margin: 0, fontSize: 15, fontWeight: 700, color: text }}>Agendar próximo paso</h3>
          </div>
          <button onClick={onClose} style={{ background: "transparent", border: "none", cursor: "pointer", color: sub, lineHeight: 1 }}><X size={16} /></button>
        </div>

        {/* Body */}
        <div style={{ padding: "20px 20px" }}>
          {/* Tipo — botones visuales */}
          <div style={{ marginBottom: 16 }}>
            <label style={{ fontSize: 11, fontWeight: 600, color: sub, display: "block", marginBottom: 8, textTransform: "uppercase", letterSpacing: 0.5 }}>Tipo de contacto</label>
            <div style={{ display: "flex", gap: 8 }}>
              {["Llamada", "WhatsApp", "Email", "Visita"].map((t) => (
                <button
                  key={t}
                  onClick={() => setTipo(t)}
                  style={{
                    flex: 1, padding: "10px 6px", borderRadius: 8, border: `2px solid ${tipo === t ? "var(--monza-accent)" : bd}`,
                    background: tipo === t ? "var(--monza-accent)15" : (dark ? "#0d1321" : "#F8FAFC"),
                    color: tipo === t ? "var(--monza-accent)" : sub,
                    cursor: "pointer", fontSize: 11, fontWeight: tipo === t ? 700 : 400,
                    display: "flex", flexDirection: "column", alignItems: "center", gap: 4,
                    transition: "all 0.15s",
                  }}
                >
                  <span style={{ fontSize: 18 }}>{TIPO_ICONS[t]}</span>
                  {t}
                </button>
              ))}
            </div>
          </div>

          {/* Fecha y hora */}
          <div style={{ marginBottom: 20 }}>
            <label style={{ fontSize: 11, fontWeight: 600, color: sub, display: "block", marginBottom: 8, textTransform: "uppercase", letterSpacing: 0.5 }}>Fecha y hora</label>
            <input
              type="datetime-local"
              value={cuando}
              onChange={(e) => setCuando(e.target.value)}
              style={{ ...inpSt, colorScheme: dark ? "dark" : "light" }}
            />
          </div>

          {/* Acciones */}
          <div style={{ display: "flex", gap: 10, justifyContent: "flex-end" }}>
            <button onClick={onClose} style={{ padding: "10px 18px", border: `1px solid ${bd}`, borderRadius: 8, background: "transparent", cursor: "pointer", fontSize: 13, color: sub, fontWeight: 500 }}>
              Cancelar
            </button>
            <button onClick={submit} disabled={saving} style={{ padding: "10px 18px", border: "none", borderRadius: 8, background: "var(--monza-accent)", cursor: saving ? "not-allowed" : "pointer", fontSize: 13, color: "white", fontWeight: 600, opacity: saving ? 0.7 : 1, display: "flex", alignItems: "center", gap: 6 }}>
              <Calendar size={14} />{saving ? "Agendando..." : "Agendar"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Lead Row Detail ────────────────────────────────────────────────────────
function LeadDetail({ lead, onRefresh }: { lead: Lead; onRefresh: () => void }) {
  const { dark } = useMonzaTheme();
  const [detail, setDetail] = useState<Lead | null>(null);
  const [loading, setLoading] = useState(true);
  const [nota, setNota] = useState("");
  const [showAgregar, setShowAgregar] = useState(false);
  const [showAgendar, setShowAgendar] = useState(false);
  const [showCotizador, setShowCotizador] = useState(false);
  const [showCotizacion, setShowCotizacion] = useState(false);
  const [savingNota, setSavingNota] = useState(false);
  const [editingPlazo, setEditingPlazo] = useState<number | null>(null);
  const [plazoVal, setPlazoVal] = useState("");

  useEffect(() => {
    monzaLeadsAPI.get(lead.id).then((r) => { setDetail(r.data); setLoading(false); });
  }, [lead.id]);

  const refresh = () => monzaLeadsAPI.get(lead.id).then((r) => { setDetail(r.data); onRefresh(); });

  const handleNota = async () => {
    if (!nota.trim()) return;
    setSavingNota(true);
    try {
      await monzaLeadsAPI.addActividad(lead.id, { tipo: "nota", descripcion: nota });
      setNota("");
      refresh();
    } catch { toast.error("Error al guardar nota"); }
    finally { setSavingNota(false); }
  };

  const handleEstado = async (estado: string) => {
    await monzaLeadsAPI.update(lead.id, { estado });
    refresh();
    onRefresh();
    toast.success(`Lead marcado como ${estado}`);
  };

  const savePlazo = async (itemId: number, value: string) => {
    setEditingPlazo(null);
    try {
      await monzaLeadsAPI.updateItem(lead.id, itemId, { plazo_entrega: value || null });
      refresh();
    } catch { toast.error("Error al guardar plazo"); }
  };

  if (loading) return <div style={{ padding: 24, textAlign: "center", color: "#94A3B8", fontSize: 13 }}>Cargando detalle...</div>;
  if (!detail) return null;
  const cli = detail.cliente;

  // ── Helpers de tema ──────────────────────────────────────────────────────
  const card  = { background: dark ? "#131b3e" : "white", border: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}`, borderRadius: 12, overflow: "hidden" as const };
  const cHead = { padding: "10px 14px", borderBottom: `1px solid ${dark ? "#1e2a4a" : "#F1F5F9"}`, background: dark ? "#0d1321" : "#F8FAFC", display: "flex", alignItems: "center", justifyContent: "space-between" };
  const cBody = { padding: "14px 16px" };
  const title = { fontSize: 11, fontWeight: 700 as const, color: dark ? "#8899cc" : "#64748B", textTransform: "uppercase" as const, letterSpacing: 0.5, display: "flex", alignItems: "center", gap: 4 };
  const txt   = dark ? "white"   : "#1E293B";
  const sub   = dark ? "#8899cc" : "#64748B";
  const rowBd = dark ? "#1e2a4a" : "#F8FAFC";

  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14, padding: 16, background: dark ? "#0a0e1f" : "#F0F4F8", borderTop: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}` }}>

      {/* ── Columna izquierda ── */}
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>

        {/* Perfil del cliente */}
        {cli && (
          <div style={card}>
            <div style={cHead}>
              <span style={title}>👤 Perfil del cliente</span>
            </div>
            <div style={cBody}>
              <div style={{ fontWeight: 700, fontSize: 15, color: txt, marginBottom: 2 }}>{cli.nombre}</div>
              {cli.rut && <div style={{ fontSize: 12, color: sub }}>RUT {cli.rut}</div>}
              <div style={{ display: "flex", flexDirection: "column", gap: 3, marginTop: 6 }}>
                {cli.telefono && <div style={{ fontSize: 12, color: sub, display: "flex", alignItems: "center", gap: 5 }}><Phone size={11} />{cli.telefono}</div>}
                {cli.email && <div style={{ fontSize: 12, color: sub }}>{cli.email}</div>}
              </div>
              {detail.comentario && (
                <div style={{ marginTop: 8, padding: "8px 10px", background: dark ? "#1a2340" : "#FFFBEB", border: `1px solid ${dark ? "#2a3a5e" : "#FDE68A"}`, borderRadius: 7 }}>
                  <div style={{ fontSize: 9, fontWeight: 700, color: dark ? "#A78BFA" : "#92400E", textTransform: "uppercase" as const, letterSpacing: 0.5, marginBottom: 3 }}>💬 Comentario</div>
                  <div style={{ fontSize: 12, color: dark ? "#d4b896" : "#78350F", lineHeight: 1.5 }}>{detail.comentario}</div>
                </div>
              )}
              {cli.etiquetas && cli.etiquetas.length > 0 && (
                <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 8 }}>
                  {cli.etiquetas.map((e, i) => (
                    <span key={i} style={{ fontSize: 10, padding: "2px 8px", borderRadius: 10, background: i === 0 ? "#FEF9C3" : (dark ? "#1e2a4a" : "#F1F5F9"), color: i === 0 ? "#854D0E" : (dark ? "#8899cc" : "#475569"), fontWeight: 600 }}>{e}</span>
                  ))}
                </div>
              )}
              <div style={{ display: "flex", gap: 12, marginTop: 10, paddingTop: 10, borderTop: `1px solid ${dark ? "#1e2a4a" : "#F1F5F9"}`, fontSize: 11 }}>
                <span style={{ color: sub }}>Leads: <strong style={{ color: txt }}>{cli.leads_total || 0}</strong></span>
                <span style={{ color: sub }}>Vendidos: <strong style={{ color: txt }}>{cli.vendidos_total || 0}</strong></span>
                <span style={{ color: sub }}>LTV: <strong style={{ color: txt }}>{fmtClp(cli.ltv || 0)}</strong></span>
              </div>
            </div>
          </div>
        )}

        {/* Comentario (cuando no hay cliente vinculado) */}
        {!cli && detail.comentario && (
          <div style={{ ...card, padding: "10px 14px" }}>
            <div style={{ fontSize: 9, fontWeight: 700, color: dark ? "#A78BFA" : "#92400E", textTransform: "uppercase" as const, letterSpacing: 0.5, marginBottom: 4 }}>💬 Comentario del lead</div>
            <div style={{ fontSize: 12, color: txt, lineHeight: 1.5 }}>{detail.comentario}</div>
          </div>
        )}

        {/* Próximos pasos */}
        <div style={card}>
          <div style={cHead}>
            <span style={title}>
              <Clock size={11} /> Próximos pasos
              {(detail.proximos_pasos || []).filter((p) => !p.completado).length > 0 && (
                <span style={{ background: "#FEF3C7", color: "#D97706", borderRadius: 10, padding: "1px 6px" }}>
                  {(detail.proximos_pasos || []).filter((p) => !p.completado).length}
                </span>
              )}
            </span>
            <button onClick={() => setShowAgendar(true)} style={{ fontSize: 11, color: "var(--monza-accent)", background: "transparent", border: "1px solid var(--monza-accent)", borderRadius: 6, cursor: "pointer", fontWeight: 600, padding: "3px 10px" }}>+ Agendar</button>
          </div>
          <div style={cBody}>
            {(detail.proximos_pasos || []).filter((p) => !p.completado).length === 0
              ? <p style={{ fontSize: 12, color: dark ? "#475569" : "#94A3B8", margin: 0 }}>No hay próximos pasos agendados.</p>
              : (detail.proximos_pasos || []).filter((p) => !p.completado).map((p) => (
                <div key={p.id} style={{ display: "flex", alignItems: "center", gap: 8, padding: "7px 0", fontSize: 12, borderBottom: `1px solid ${rowBd}` }}>
                  <Calendar size={12} className="monza-ic" />
                  <span style={{ fontWeight: 600, color: txt }}>{p.tipo}</span>
                  {p.cuando && <span style={{ color: sub, fontSize: 11 }}>{new Date(p.cuando).toLocaleString("es-CL", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}</span>}
                  <button onClick={async () => { await monzaLeadsAPI.completarPaso(lead.id, p.id); refresh(); }} style={{ marginLeft: "auto", fontSize: 10, background: "#DCFCE7", color: "#166534", border: "none", borderRadius: 6, padding: "3px 10px", cursor: "pointer", fontWeight: 600 }}>✓ Completar</button>
                </div>
              ))
            }
          </div>
        </div>

        {/* Nota interna + Acciones estado */}
        <div style={card}>
          <div style={cHead}>
            <span style={title}>📝 Nota interna</span>
            <span style={{ fontSize: 10, color: dark ? "#475569" : "#94A3B8" }}>Solo visible para el equipo</span>
          </div>
          <div style={cBody}>
            <textarea value={nota} onChange={(e) => setNota(e.target.value)} placeholder="Escribe una nota interna..." rows={2}
              style={{ width: "100%", padding: "8px 10px", border: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}`, borderRadius: 6, fontSize: 12, resize: "vertical", boxSizing: "border-box", background: dark ? "#0d1321" : "white", color: txt }} />
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 10 }}>
              <button onClick={handleNota} disabled={!nota.trim() || savingNota}
                style={{ padding: "6px 14px", border: "none", borderRadius: 6, background: "#1E293B", color: "white", fontSize: 12, cursor: "pointer", opacity: !nota.trim() ? 0.5 : 1 }}>
                {savingNota ? "Guardando..." : "Guardar nota"}
              </button>
              <div style={{ display: "flex", gap: 6 }}>
                <button onClick={() => handleEstado("en_proceso")} style={{ fontSize: 11, padding: "5px 12px", border: "1px solid #DBEAFE", borderRadius: 6, background: "#EFF6FF", color: "#1D4ED8", cursor: "pointer", fontWeight: 600 }}>● En proceso</button>
                <button onClick={() => handleEstado("rechazado")} style={{ fontSize: 11, padding: "5px 12px", border: "1px solid #FEE2E2", borderRadius: 6, background: "#FFF5F5", color: "#991B1B", cursor: "pointer", fontWeight: 600 }}>✗ Rechazar</button>
                <button
                  onClick={async () => {
                    if (!window.confirm(`¿Eliminar el lead ${lead.numero}? Esta acción no se puede deshacer.`)) return;
                    try {
                      await monzaLeadsAPI.deleteLead(lead.id);
                      toast.success(`Lead ${lead.numero} eliminado`);
                      onRefresh();
                    } catch { toast.error("Error al eliminar el lead"); }
                  }}
                  style={{ fontSize: 11, padding: "5px 12px", border: "1px solid #FCA5A5", borderRadius: 6, background: "transparent", color: "#DC2626", cursor: "pointer", fontWeight: 600 }}>
                  🗑 Eliminar
                </button>
              </div>
            </div>
          </div>
        </div>

      </div>

      {/* ── Columna derecha ── */}
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>

        {/* Ítems */}
        <div style={card}>
          <div style={cHead}>
            <span style={title}>
              📦 Ítems
              <span style={{ background: dark ? "#1e2a4a" : "#F1F5F9", color: dark ? "#e2e8f0" : "#475569", borderRadius: 10, padding: "1px 7px" }}>{(detail.items || []).length}</span>
            </span>
            <div style={{ display: "flex", gap: 6 }}>
              {(detail.items || []).some((it) => it.precio_clp) && (
                <button onClick={() => setShowCotizacion(true)}
                  style={{ fontSize: 11, display: "flex", alignItems: "center", gap: 4, padding: "5px 10px", border: "1px solid #0EA5E9", borderRadius: 6, background: "#F0F9FF", color: "#0369A1", cursor: "pointer", fontWeight: 700 }}>
                  <FileText size={11} /> Emitir cotización
                </button>
              )}
              <button onClick={() => setShowCotizador(true)}
                style={{ fontSize: 11, display: "flex", alignItems: "center", gap: 4, padding: "5px 10px", border: "1px solid var(--monza-accent)", borderRadius: 6, background: "transparent", color: "var(--monza-accent)", cursor: "pointer", fontWeight: 600 }}>
                <Calculator size={11} /> Calcular todos
              </button>
              <button onClick={() => setShowAgregar(true)}
                style={{ fontSize: 11, display: "flex", alignItems: "center", gap: 4, padding: "5px 10px", border: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}`, borderRadius: 6, background: "transparent", color: sub, cursor: "pointer" }}>
                <Plus size={11} /> Agregar repuesto
              </button>
            </div>
          </div>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
            <thead>
              <tr style={{ background: dark ? "#0d1321" : "#F8FAFC" }}>
                {["Repuesto","N° parte","Marca","QTY","Calidad","Precio","Plazo de entrega",""].map((h, i) => (
                  <th key={i} style={{ textAlign: i === 3 ? "center" : i === 5 ? "right" : "left", padding: "7px 10px", fontWeight: 600, fontSize: 10, color: sub, textTransform: "uppercase", letterSpacing: 0.4 }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {(detail.items || []).length === 0 ? (
                <tr><td colSpan={8} style={{ padding: "20px 10px", textAlign: "center", color: dark ? "#475569" : "#94A3B8", fontSize: 12 }}>Sin repuestos — agrega el primero</td></tr>
              ) : (detail.items || []).map((it) => (
                <tr key={it.id} style={{ borderBottom: `1px solid ${rowBd}` }}>
                  <td style={{ padding: "7px 10px", color: txt, fontWeight: 500 }}>{it.descripcion}</td>
                  <td style={{ padding: "7px 10px", color: sub, fontSize: 11 }}>{it.numero_parte || "—"}</td>
                  <td style={{ padding: "7px 10px", color: sub }}>{it.marca || "—"}</td>
                  <td style={{ padding: "7px 10px", textAlign: "center", color: txt }}>{it.cantidad}</td>
                  <td style={{ padding: "7px 10px" }}>
                    <span style={{ fontSize: 10, padding: "2px 7px", borderRadius: 8, background: it.calidad === "sin_calificar" ? (dark ? "#1e2a4a" : "#F1F5F9") : "#EFF6FF", color: it.calidad === "sin_calificar" ? (dark ? "#8899cc" : "#94A3B8") : "#1D4ED8" }}>
                      {CALIDAD_LABEL[it.calidad] || it.calidad}
                    </span>
                  </td>
                  <td style={{ padding: "7px 10px", textAlign: "right", color: it.precio_clp ? txt : "#94A3B8", fontWeight: it.precio_clp ? 600 : 400 }}>
                    {it.precio_clp ? fmtClp(it.precio_clp) : <span style={{ fontSize: 10, background: "#FEF3C7", color: "#D97706", padding: "2px 6px", borderRadius: 4 }}>Sin precio</span>}
                  </td>
                  <td style={{ padding: "5px 8px", minWidth: 140 }}>
                    {editingPlazo === it.id ? (
                      <input
                        autoFocus
                        value={plazoVal}
                        onChange={(e) => setPlazoVal(e.target.value)}
                        onBlur={() => savePlazo(it.id, plazoVal)}
                        onKeyDown={(e) => { if (e.key === "Enter") savePlazo(it.id, plazoVal); if (e.key === "Escape") setEditingPlazo(null); }}
                        placeholder="7-10 días hábiles"
                        style={{ width: "100%", padding: "4px 6px", border: `1px solid ${dark ? "#1e2a4a" : "#CBD5E1"}`, borderRadius: 5, fontSize: 11, background: dark ? "#0d1321" : "white", color: txt, boxSizing: "border-box" as const, outline: "2px solid #E67E22" }}
                      />
                    ) : (
                      <button
                        onClick={() => { setEditingPlazo(it.id); setPlazoVal(it.plazo_entrega || ""); }}
                        title="Click para editar plazo"
                        style={{ background: "transparent", border: "none", cursor: "pointer", padding: "3px 0", width: "100%", textAlign: "left", fontSize: 11, color: it.plazo_entrega ? (dark ? "#e2e8f0" : "#334155") : (dark ? "#334155" : "#CBD5E1") }}
                      >
                        {it.plazo_entrega || <span style={{ fontStyle: "italic", color: dark ? "#334155" : "#CBD5E1" }}>— añadir plazo</span>}
                      </button>
                    )}
                  </td>
                  <td style={{ padding: "7px 10px", textAlign: "center" }}>
                    <button onClick={async () => { await monzaLeadsAPI.deleteItem(lead.id, it.id); refresh(); }} style={{ background: "transparent", border: "none", cursor: "pointer", color: "#EF4444", opacity: 0.6 }}>
                      <Trash2 size={12} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {(detail.items || []).length > 0 && (
            <div style={{ padding: "8px 10px", borderTop: `1px solid ${rowBd}`, textAlign: "right", fontSize: 12, color: sub }}>
              {(detail.items || []).filter((it) => it.precio_clp).length} ítem(s) con precio · Total{" "}
              <strong style={{ color: txt }}>{fmtClp(detail.total_estimado)}</strong>
            </div>
          )}
        </div>

        {/* Actividad */}
        <div style={card}>
          <div style={cHead}>
            <span style={title}>
              📋 Actividad
              <span style={{ background: dark ? "#1e2a4a" : "#F1F5F9", color: dark ? "#e2e8f0" : "#475569", borderRadius: 10, padding: "1px 7px" }}>{(detail.actividades || []).length}</span>
            </span>
          </div>
          <div style={{ padding: "10px 14px", maxHeight: 220, overflowY: "auto" }}>
            {(detail.actividades || []).length === 0
              ? <p style={{ fontSize: 12, color: dark ? "#475569" : "#94A3B8", margin: 0 }}>Sin actividad registrada.</p>
              : (detail.actividades || []).slice(0, 12).map((a) => (
                <div key={a.id} style={{ display: "flex", gap: 10, fontSize: 12, padding: "7px 0", borderBottom: `1px solid ${rowBd}` }}>
                  <div style={{ width: 26, height: 26, borderRadius: "50%", background: dark ? "#1e2a4a" : "#F1F5F9", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0, fontSize: 13 }}>
                    {a.tipo === "lead_creado" ? "🟢" : a.tipo === "nota" ? "📝" : a.tipo === "cotizacion" ? "📄" : "📞"}
                  </div>
                  <div style={{ flex: 1 }}>
                    <div style={{ color: txt, lineHeight: 1.4 }}>{a.descripcion}</div>
                    <div style={{ color: dark ? "#475569" : "#94A3B8", fontSize: 11, marginTop: 2 }}>{timeSince(a.fecha)} · {a.usuario}</div>
                  </div>
                </div>
              ))
            }
          </div>
        </div>

      </div>

      {/* Modals */}
      {showAgregar && <AgregarItemModal leadId={lead.id} onClose={() => setShowAgregar(false)} onAdded={refresh} />}
      {showAgendar && <AgendarModal leadId={lead.id} onClose={() => setShowAgendar(false)} onDone={refresh} />}
      {showCotizacion && detail && (
        <EmitirCotizacionModal lead={lead} detail={detail} onClose={() => setShowCotizacion(false)} onEmitted={refresh} />
      )}
      {showCotizador && (
        <MonzaCotizadorModal
          leadId={lead.id}
          leadNumero={detail.numero}
          clienteNombre={detail.cliente?.nombre || ""}
          vehiculo={detail.vehiculo}
          items={(detail.items || []) as any}
          onClose={() => setShowCotizador(false)}
          onApplied={refresh}
        />
      )}
    </div>
  );
}

// ── Main Page ─────────────────────────────────────────────────────────────
export default function MonzaLeadsPage() {
  const [leads, setLeads] = useState<Lead[]>([]);
  const [total, setTotal] = useState(0);
  const [kpis, setKpis] = useState<KPIs | null>(null);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState<number | null>(null);
  const [showNuevo, setShowNuevo] = useState(false);
  const [q, setQ] = useState("");
  const [estado, setEstado] = useState("todos");
  const [soloMios, setSoloMios] = useState(false);
  const [sinContactar, setSinContactar] = useState(false);

  const fetchAll = useCallback(async () => {
    setLoading(true);
    try {
      const [leadsRes, kpisRes] = await Promise.all([
        monzaLeadsAPI.list({ q: q || undefined, estado, solo_mios: soloMios, sin_contactar: sinContactar }),
        monzaLeadsAPI.kpis(),
      ]);
      setLeads(leadsRes.data.items);
      setTotal(leadsRes.data.total);
      setKpis(kpisRes.data);
    } catch { toast.error("Error al cargar leads"); }
    finally { setLoading(false); }
  }, [q, estado, soloMios, sinContactar]);

  useEffect(() => { fetchAll(); }, [fetchAll]);

  const toggleRow = (id: number) => setExpanded((prev) => (prev === id ? null : id));

  return (
    <div>
      {/* Header */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 20 }}>
        <div>
          <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: "#1E293B" }}>Leads · {new Date().getFullYear()}</h1>
          <p style={{ margin: "4px 0 0", fontSize: 13, color: "#64748B" }}>Pipeline comercial de MonzaParts</p>
        </div>
        <button onClick={() => setShowNuevo(true)}
          style={{ display: "flex", alignItems: "center", gap: 6, padding: "10px 18px", border: "none", borderRadius: 8, background: "var(--monza-accent)", color: "white", cursor: "pointer", fontSize: 13, fontWeight: 600 }}>
          <Plus size={16} /> Nuevo lead
        </button>
      </div>

      {/* KPIs */}
      {kpis && (
        <div style={{ display: "flex", gap: 12, marginBottom: 20, flexWrap: "wrap" }}>
          <KpiCard label="Leads nuevos del mes" accent="#3B82F6" value={kpis.nuevos_mes} />
          <KpiCard label="En proceso" accent="#F59E0B" value={kpis.en_proceso} />
          <KpiCard label="Vendidos" accent="#10B981" value={kpis.vendidos_mes} sub={`Total: ${fmtClp(kpis.total_cotizado_mes)}`} />
          <KpiCard label="Tasa de cierre" accent="#6366F1" value={`${kpis.tasa_cierre_pct}%`} />
          <KpiCard label="Sin contactar +3d" accent="#EF4444" value={kpis.sin_contactar_3d} sub={`Pendiente ${kpis.pendientes} · En trabajo ${kpis.en_trabajo}`} />
        </div>
      )}

      {/* Filters */}
      <div style={{ background: "white", border: "1px solid #E2E8F0", borderRadius: 10, padding: "14px 16px", marginBottom: 16 }}>
        <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <div style={{ position: "relative", flex: 1, minWidth: 260 }}>
            <Search size={14} style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)", color: "#94A3B8" }} />
            <input value={q} onChange={(e) => setQ(e.target.value)}
              placeholder="Buscar cliente, teléfono, VIN, N° parte, COT..."
              style={{ width: "100%", padding: "8px 10px 8px 32px", border: "1px solid #E2E8F0", borderRadius: 6, fontSize: 13, boxSizing: "border-box" }} />
          </div>
          <select value={estado} onChange={(e) => setEstado(e.target.value)}
            style={{ padding: "8px 12px", border: "1px solid #E2E8F0", borderRadius: 6, fontSize: 13, background: "white" }}>
            <option value="todos" style={{ background: "white", color: "#1E293B" }}>Todos los estados</option>
            <option value="pendiente" style={{ background: "white", color: "#1E293B" }}>Pendiente</option>
            <option value="en_proceso" style={{ background: "white", color: "#1E293B" }}>En proceso</option>
            <option value="vendido" style={{ background: "white", color: "#1E293B" }}>Vendido</option>
            <option value="rechazado" style={{ background: "white", color: "#1E293B" }}>Rechazado</option>
          </select>
          <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13, color: "#475569", cursor: "pointer" }}>
            <input type="checkbox" checked={soloMios} onChange={(e) => setSoloMios(e.target.checked)} /> Míos
          </label>
          <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13, color: "#475569", cursor: "pointer" }}>
            <input type="checkbox" checked={sinContactar} onChange={(e) => setSinContactar(e.target.checked)} /> Sin contactar +3 días
          </label>
        </div>
      </div>

      {/* Table */}
      <div style={{ background: "white", border: "1px solid #E2E8F0", borderRadius: 10, overflow: "hidden" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
          <thead>
            <tr style={{ background: "#F8FAFC", borderBottom: "1px solid #E2E8F0" }}>
              <th style={{ width: 32 }}></th>
              <th style={{ padding: "10px 12px", textAlign: "left", fontWeight: 600, fontSize: 11, color: "#64748B", textTransform: "uppercase", letterSpacing: 0.5 }}>N°</th>
              <th style={{ padding: "10px 12px", textAlign: "left", fontWeight: 600, fontSize: 11, color: "#64748B", textTransform: "uppercase", letterSpacing: 0.5 }}>Cliente</th>
              <th style={{ padding: "10px 12px", textAlign: "left", fontWeight: 600, fontSize: 11, color: "#64748B", textTransform: "uppercase", letterSpacing: 0.5 }}>Vehículo</th>
              <th style={{ padding: "10px 12px", textAlign: "center", fontWeight: 600, fontSize: 11, color: "#64748B", textTransform: "uppercase", letterSpacing: 0.5 }}>Ítems</th>
              <th style={{ padding: "10px 12px", textAlign: "left", fontWeight: 600, fontSize: 11, color: "#64748B", textTransform: "uppercase", letterSpacing: 0.5 }}>Estado</th>
              <th style={{ padding: "10px 12px", textAlign: "left", fontWeight: 600, fontSize: 11, color: "#64748B", textTransform: "uppercase", letterSpacing: 0.5 }}>Próximo paso</th>
              <th style={{ padding: "10px 12px", textAlign: "left", fontWeight: 600, fontSize: 11, color: "#64748B", textTransform: "uppercase", letterSpacing: 0.5 }}>Asesor</th>
              <th style={{ padding: "10px 12px", textAlign: "right", fontWeight: 600, fontSize: 11, color: "#64748B", textTransform: "uppercase", letterSpacing: 0.5 }}>Total</th>
              <th style={{ padding: "10px 12px", textAlign: "center", fontWeight: 600, fontSize: 11, color: "#64748B", textTransform: "uppercase", letterSpacing: 0.5 }}>Acciones</th>
            </tr>
          </thead>
          <tbody>
            {loading
              ? <tr><td colSpan={10} style={{ padding: 40, textAlign: "center", color: "#94A3B8" }}>Cargando leads...</td></tr>
              : leads.length === 0
                ? <tr><td colSpan={10} style={{ padding: 40, textAlign: "center", color: "#94A3B8" }}>No hay leads con los filtros actuales.</td></tr>
                : leads.map((lead) => (
                <>
                  <tr key={lead.id}
                    onClick={() => toggleRow(lead.id)}
                    style={{ borderBottom: "1px solid #F1F5F9", cursor: "pointer", background: expanded === lead.id ? "#FFF9F9" : "white" }}
                    onMouseEnter={(e) => { if (expanded !== lead.id) (e.currentTarget as HTMLTableRowElement).style.background = "#F8FAFC"; }}
                    onMouseLeave={(e) => { (e.currentTarget as HTMLTableRowElement).style.background = expanded === lead.id ? "#FFF9F9" : "white"; }}>
                    <td style={{ paddingLeft: 12, color: "#94A3B8" }}>
                      {expanded === lead.id ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                    </td>
                    <td style={{ padding: "10px 12px" }}>
                      <div style={{ fontSize: 12, fontWeight: 600, color: "#1E293B" }}>{lead.numero}</div>
                      <div style={{ fontSize: 10, color: "#94A3B8" }}>{lead.canal_origen}</div>
                    </td>
                    <td style={{ padding: "10px 12px" }}>
                      <div style={{ fontWeight: 600, color: "#1E293B" }}>{lead.cliente?.nombre || "—"}</div>
                      <div style={{ fontSize: 11, color: "#94A3B8" }}>{lead.cliente?.telefono || ""}</div>
                    </td>
                    <td style={{ padding: "10px 12px", color: "#475569" }}>{lead.vehiculo || "—"}</td>
                    <td style={{ padding: "10px 12px", textAlign: "center" }}>
                      <span style={{ background: "#F1F5F9", borderRadius: 10, padding: "2px 10px", fontSize: 12, fontWeight: 600, color: "#475569" }}>{lead.items_count}</span>
                    </td>
                    <td style={{ padding: "10px 12px" }}>
                      {(() => { const s = ESTADO_COLORS[lead.estado] || ESTADO_COLORS.pendiente; return (
                        <span style={{ background: s.bg, color: s.color, fontSize: 11, padding: "3px 10px", borderRadius: 10, fontWeight: 600 }}>» {s.label}</span>
                      ); })()}
                    </td>
                    <td style={{ padding: "10px 12px", fontSize: 12, color: "#64748B" }}>
                      {lead.proximo_paso ? (
                        <span>{lead.proximo_paso.tipo} {lead.proximo_paso.cuando ? "· " + new Date(lead.proximo_paso.cuando).toLocaleDateString("es-CL") : ""}</span>
                      ) : <span style={{ color: "#CBD5E1" }}>Sin paso</span>}
                    </td>
                    <td style={{ padding: "10px 12px" }}>
                      <div style={{ width: 28, height: 28, borderRadius: "50%", background: "#F1F5F9", display: "inline-flex", alignItems: "center", justifyContent: "center", fontSize: 11, fontWeight: 700, color: "var(--monza-accent)" }}>
                        {lead.asesor ? lead.asesor[0] : "?"}
                      </div>
                      <span style={{ fontSize: 11, color: "#64748B", marginLeft: 6 }}>{lead.asesor || "—"}</span>
                    </td>
                    <td style={{ padding: "10px 12px", textAlign: "right", fontWeight: 600, color: lead.total_estimado > 0 ? "#1E293B" : "#CBD5E1" }}>
                      {lead.total_estimado > 0 ? fmtClp(lead.total_estimado) : "$0"}
                    </td>
                    <td style={{ padding: "10px 12px", textAlign: "center" }} onClick={(e) => e.stopPropagation()}>
                      <div style={{ display: "flex", gap: 4, justifyContent: "center" }}>
                        <button title="Llamada" style={{ background: "transparent", border: "none", cursor: "pointer", color: "#64748B", padding: 4 }}><Phone size={14} /></button>
                        <button title="WhatsApp" style={{ background: "transparent", border: "none", cursor: "pointer", color: "#64748B", padding: 4 }}><MessageCircle size={14} /></button>
                        <button title="Nota" style={{ background: "transparent", border: "none", cursor: "pointer", color: "#64748B", padding: 4 }}><FileText size={14} /></button>
                        <button title="Agendar" style={{ background: "transparent", border: "none", cursor: "pointer", color: "#64748B", padding: 4 }}><Calendar size={14} /></button>
                      </div>
                    </td>
                  </tr>
                  {expanded === lead.id && (
                    <tr key={`${lead.id}-detail`} style={{ background: "#FFF9F9" }}>
                      <td colSpan={10} style={{ padding: 0 }}>
                        <LeadDetail lead={lead} onRefresh={fetchAll} />
                      </td>
                    </tr>
                  )}
                </>
              ))}
          </tbody>
        </table>
        {total > 0 && (
          <div style={{ padding: "10px 16px", borderTop: "1px solid #F1F5F9", fontSize: 12, color: "#94A3B8" }}>
            Mostrando 1-{leads.length} de {total}
          </div>
        )}
      </div>

      {showNuevo && <NuevoLeadModal onClose={() => setShowNuevo(false)} onCreated={fetchAll} />}
    </div>
  );
}
