import { useState, useEffect, useCallback } from "react";
import { monzaConfigAPI, monzaClientesAPI } from "../services/monzaApi";
import toast from "react-hot-toast";
import { Settings, RefreshCw, Save, Users, Plus, Search, Edit2, Trash2, X, Phone, Mail } from "lucide-react";

// ── Config types ──────────────────────────────────────────────────────────────
interface Config {
  tc_usd_clp: number; tc_eur_clp: number; tarifa_aerea_por_kg: number;
  moneda_tarifa: string; iva_pct: number; razon_social: string; rut_empresa: string;
  direccion: string; giro: string; email_empresa: string; banco: string;
  tipo_cuenta: string; numero_cuenta: string; condiciones_default: string;
  ultima_actualizacion: string; usuario_email: string;
}
const DEFAULTS: Partial<Config> = { tc_usd_clp: 900, tc_eur_clp: 1060, tarifa_aerea_por_kg: 4.5, moneda_tarifa: "EUR", iva_pct: 19 };

// ── Cliente types ─────────────────────────────────────────────────────────────
interface Cliente {
  id: number; nombre: string; rut?: string; telefono?: string; email?: string;
  vehiculos: string[]; etiquetas: string[]; ltv: number; leads_total: number;
  vendidos_total: number; is_active: boolean; fecha_creacion?: string;
}

// ── Reusable form components ──────────────────────────────────────────────────
function Field({ label, help, children }: { label: string; help?: string; children: React.ReactNode }) {
  return (
    <div>
      <label style={{ fontSize: 12, fontWeight: 600, color: "#64748B", textTransform: "uppercase", letterSpacing: 0.5 }}>{label}</label>
      {children}
      {help && <p style={{ fontSize: 11, color: "#94A3B8", marginTop: 4 }}>{help}</p>}
    </div>
  );
}
function Inp({ value, onChange, type = "text", placeholder = "" }: { value: string | number; onChange: (v: string) => void; type?: string; placeholder?: string }) {
  return (
    <input type={type} value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder}
      style={{ width: "100%", marginTop: 6, padding: "8px 12px", border: "1px solid #E2E8F0", borderRadius: 6, fontSize: 14, color: "#1E293B", background: "white", boxSizing: "border-box" }} />
  );
}

// ── Cliente Modal (crear/editar) ──────────────────────────────────────────────
function ClienteModal({ cliente, onClose, onSaved }: { cliente?: Cliente | null; onClose: () => void; onSaved: () => void }) {
  const isEdit = !!cliente;
  const [form, setForm] = useState({
    nombre: cliente?.nombre || "",
    rut: cliente?.rut || "",
    telefono: cliente?.telefono || "",
    email: cliente?.email || "",
  });
  const [saving, setSaving] = useState(false);
  const set = (f: string, v: string) => setForm(p => ({ ...p, [f]: v }));
  const inp = { width: "100%", padding: "8px 10px", border: "1px solid #D1D5DB", borderRadius: 6, fontSize: 13, boxSizing: "border-box" as const, background: "white", color: "#1E293B" };
  const lbl = { fontSize: 11, fontWeight: 600 as const, color: "#64748B", display: "block", marginBottom: 4 };

  const submit = async () => {
    if (!form.nombre.trim()) { toast.error("Nombre requerido"); return; }
    setSaving(true);
    try {
      if (isEdit && cliente) {
        await monzaClientesAPI.update(cliente.id, form);
        toast.success("Cliente actualizado");
      } else {
        await monzaClientesAPI.create(form);
        toast.success("Cliente creado");
      }
      onSaved();
      onClose();
    } catch { toast.error("Error al guardar cliente"); }
    finally { setSaving(false); }
  };

  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)", zIndex: 1100, display: "flex", alignItems: "center", justifyContent: "center", padding: 16 }}>
      <div style={{ background: "white", borderRadius: 12, width: "100%", maxWidth: 480, boxShadow: "0 20px 60px rgba(0,0,0,0.3)" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "16px 20px", borderBottom: "1px solid #F1F5F9" }}>
          <h3 style={{ margin: 0, fontSize: 15, fontWeight: 700, color: "#1E293B" }}>
            {isEdit ? "Editar cliente" : "Nuevo cliente"}
          </h3>
          <button onClick={onClose} style={{ background: "transparent", border: "none", cursor: "pointer", color: "#94A3B8" }}><X size={18} /></button>
        </div>
        <div style={{ padding: 20 }}>
          <div style={{ marginBottom: 12 }}><label style={lbl}>Nombre *</label><input style={inp} value={form.nombre} onChange={e => set("nombre", e.target.value)} placeholder="JUAN PÉREZ" autoFocus /></div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 12 }}>
            <div><label style={lbl}>Teléfono</label><input style={inp} value={form.telefono} onChange={e => set("telefono", e.target.value)} placeholder="+56 9 1234 5678" /></div>
            <div><label style={lbl}>RUT</label><input style={inp} value={form.rut} onChange={e => set("rut", e.target.value)} placeholder="12.345.678-9" /></div>
          </div>
          <div style={{ marginBottom: 20 }}><label style={lbl}>Email</label><input style={inp} type="email" value={form.email} onChange={e => set("email", e.target.value)} /></div>
          <div style={{ display: "flex", gap: 10, justifyContent: "flex-end" }}>
            <button onClick={onClose} style={{ padding: "9px 18px", border: "1px solid #E2E8F0", borderRadius: 8, background: "white", cursor: "pointer", fontSize: 13, color: "#475569" }}>Cancelar</button>
            <button onClick={submit} disabled={saving}
              style={{ padding: "9px 18px", border: "none", borderRadius: 8, background: "var(--monza-accent)", cursor: "pointer", fontSize: 13, color: "white", fontWeight: 600, opacity: saving ? 0.7 : 1 }}>
              {saving ? "Guardando..." : isEdit ? "Guardar cambios" : "Crear cliente"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Clientes Tab ──────────────────────────────────────────────────────────────
function ClientesTab() {
  const [clientes, setClientes] = useState<Cliente[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState("");
  const [page, setPage] = useState(1);
  const [showModal, setShowModal] = useState(false);
  const [editCliente, setEditCliente] = useState<Cliente | null>(null);
  const PAGE_SIZE = 30;

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const r = await monzaClientesAPI.list({ q: q || undefined, page, page_size: PAGE_SIZE });
      setClientes(r.data.items);
      setTotal(r.data.total);
    } catch { toast.error("Error al cargar clientes"); }
    finally { setLoading(false); }
  }, [q, page]);

  useEffect(() => { setPage(1); }, [q]);
  useEffect(() => { load(); }, [load]);

  const handleDelete = async (c: Cliente) => {
    if (!window.confirm(`¿Desactivar a ${c.nombre}? Mantendrá sus leads asociados.`)) return;
    try {
      await monzaClientesAPI.remove(c.id);
      toast.success("Cliente desactivado");
      load();
    } catch { toast.error("Error al desactivar"); }
  };

  const fmt = (n: number) => n > 0 ? `$${n.toLocaleString("es-CL")}` : "$0";

  return (
    <div>
      {/* Toolbar */}
      <div style={{ display: "flex", gap: 12, alignItems: "center", marginBottom: 16 }}>
        <div style={{ position: "relative", flex: 1 }}>
          <Search size={14} style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)", color: "#94A3B8" }} />
          <input value={q} onChange={e => setQ(e.target.value)}
            placeholder="Buscar por nombre, teléfono, RUT o email..."
            style={{ width: "100%", padding: "9px 10px 9px 32px", border: "1px solid #E2E8F0", borderRadius: 8, fontSize: 13, boxSizing: "border-box" }} />
        </div>
        <button onClick={() => { setEditCliente(null); setShowModal(true); }}
          style={{ display: "flex", alignItems: "center", gap: 6, padding: "9px 16px", border: "none", borderRadius: 8, background: "var(--monza-accent)", color: "white", cursor: "pointer", fontSize: 13, fontWeight: 600, whiteSpace: "nowrap" }}>
          <Plus size={15} /> Nuevo cliente
        </button>
      </div>

      {/* Stats */}
      <div style={{ fontSize: 12, color: "#64748B", marginBottom: 12 }}>
        {loading ? "Cargando..." : `${total} cliente${total !== 1 ? "s" : ""} encontrado${total !== 1 ? "s" : ""}`}
      </div>

      {/* Table */}
      <div style={{ background: "white", border: "1px solid #E2E8F0", borderRadius: 10, overflow: "hidden" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
          <thead>
            <tr style={{ background: "#F8FAFC", borderBottom: "1px solid #E2E8F0" }}>
              {["Cliente", "Contacto", "Leads", "Vendidos", "LTV", "Acciones"].map((h, i) => (
                <th key={i} style={{ padding: "10px 14px", textAlign: i >= 2 && i <= 4 ? "center" : i === 5 ? "center" : "left", fontWeight: 600, fontSize: 11, color: "#64748B", textTransform: "uppercase", letterSpacing: 0.5 }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={6} style={{ padding: 40, textAlign: "center", color: "#94A3B8" }}>Cargando...</td></tr>
            ) : clientes.length === 0 ? (
              <tr><td colSpan={6} style={{ padding: 40, textAlign: "center", color: "#94A3B8" }}>
                {q ? "No se encontraron clientes con ese criterio." : "Aún no hay clientes registrados. Crea el primero."}
              </td></tr>
            ) : clientes.map((c) => (
              <tr key={c.id} style={{ borderBottom: "1px solid #F1F5F9" }}
                onMouseEnter={e => (e.currentTarget as HTMLTableRowElement).style.background = "#FAFAFA"}
                onMouseLeave={e => (e.currentTarget as HTMLTableRowElement).style.background = "white"}>
                <td style={{ padding: "11px 14px" }}>
                  <div style={{ fontWeight: 600, color: "#1E293B" }}>{c.nombre}</div>
                  {c.rut && <div style={{ fontSize: 11, color: "#94A3B8", marginTop: 1 }}>RUT {c.rut}</div>}
                </td>
                <td style={{ padding: "11px 14px" }}>
                  <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                    {c.telefono && <span style={{ fontSize: 12, color: "#475569", display: "flex", alignItems: "center", gap: 4 }}><Phone size={10} color="#94A3B8" />{c.telefono}</span>}
                    {c.email && <span style={{ fontSize: 12, color: "#475569", display: "flex", alignItems: "center", gap: 4 }}><Mail size={10} color="#94A3B8" />{c.email}</span>}
                    {!c.telefono && !c.email && <span style={{ fontSize: 11, color: "#CBD5E1" }}>Sin contacto</span>}
                  </div>
                </td>
                <td style={{ padding: "11px 14px", textAlign: "center" }}>
                  <span style={{ background: "#F1F5F9", borderRadius: 10, padding: "2px 10px", fontSize: 12, fontWeight: 600, color: "#475569" }}>{c.leads_total}</span>
                </td>
                <td style={{ padding: "11px 14px", textAlign: "center" }}>
                  <span style={{ background: "#DCFCE7", borderRadius: 10, padding: "2px 10px", fontSize: 12, fontWeight: 600, color: "#166534" }}>{c.vendidos_total}</span>
                </td>
                <td style={{ padding: "11px 14px", textAlign: "center", fontWeight: 600, color: c.ltv > 0 ? "#1E293B" : "#CBD5E1" }}>
                  {fmt(c.ltv)}
                </td>
                <td style={{ padding: "11px 14px", textAlign: "center" }}>
                  <div style={{ display: "flex", gap: 6, justifyContent: "center" }}>
                    <button onClick={() => { setEditCliente(c); setShowModal(true); }}
                      title="Editar" style={{ background: "transparent", border: "1px solid #E2E8F0", borderRadius: 6, cursor: "pointer", color: "#475569", padding: "5px 8px", display: "flex", alignItems: "center" }}>
                      <Edit2 size={13} />
                    </button>
                    <button onClick={() => handleDelete(c)}
                      title="Desactivar" style={{ background: "transparent", border: "1px solid #FEE2E2", borderRadius: 6, cursor: "pointer", color: "#EF4444", padding: "5px 8px", display: "flex", alignItems: "center" }}>
                      <Trash2 size={13} />
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        {/* Pagination */}
        {total > PAGE_SIZE && (
          <div style={{ padding: "10px 16px", borderTop: "1px solid #F1F5F9", display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: 12, color: "#64748B" }}>
            <span>Mostrando {(page - 1) * PAGE_SIZE + 1}–{Math.min(page * PAGE_SIZE, total)} de {total}</span>
            <div style={{ display: "flex", gap: 6 }}>
              <button disabled={page === 1} onClick={() => setPage(p => p - 1)}
                style={{ padding: "4px 12px", border: "1px solid #E2E8F0", borderRadius: 6, background: page === 1 ? "#F8FAFC" : "white", cursor: page === 1 ? "not-allowed" : "pointer", color: page === 1 ? "#CBD5E1" : "#475569" }}>
                ← Anterior
              </button>
              <button disabled={page * PAGE_SIZE >= total} onClick={() => setPage(p => p + 1)}
                style={{ padding: "4px 12px", border: "1px solid #E2E8F0", borderRadius: 6, background: page * PAGE_SIZE >= total ? "#F8FAFC" : "white", cursor: page * PAGE_SIZE >= total ? "not-allowed" : "pointer", color: page * PAGE_SIZE >= total ? "#CBD5E1" : "#475569" }}>
                Siguiente →
              </button>
            </div>
          </div>
        )}
      </div>

      {showModal && (
        <ClienteModal
          cliente={editCliente}
          onClose={() => { setShowModal(false); setEditCliente(null); }}
          onSaved={load}
        />
      )}
    </div>
  );
}

// ── Main Config Page ──────────────────────────────────────────────────────────
export default function MonzaConfigPage() {
  const [tab, setTab] = useState<"general" | "clientes">("general");
  const [cfg, setCfg] = useState<Config | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  useEffect(() => { if (tab === "general") loadCfg(); }, [tab]);

  const loadCfg = async () => {
    setLoading(true);
    try { const r = await monzaConfigAPI.get(); setCfg(r.data); }
    catch { toast.error("Error al cargar configuración"); }
    finally { setLoading(false); }
  };

  const set = (field: keyof Config, value: string | number) =>
    setCfg((prev) => prev ? { ...prev, [field]: value } : prev);

  const handleSave = async () => {
    if (!cfg) return;
    setSaving(true);
    try {
      const r = await monzaConfigAPI.update({
        tc_usd_clp: Number(cfg.tc_usd_clp), tc_eur_clp: Number(cfg.tc_eur_clp),
        tarifa_aerea_por_kg: Number(cfg.tarifa_aerea_por_kg), moneda_tarifa: cfg.moneda_tarifa,
        iva_pct: Number(cfg.iva_pct), razon_social: cfg.razon_social, rut_empresa: cfg.rut_empresa,
        direccion: cfg.direccion, giro: cfg.giro, email_empresa: cfg.email_empresa,
        banco: cfg.banco, tipo_cuenta: cfg.tipo_cuenta, numero_cuenta: cfg.numero_cuenta,
        condiciones_default: cfg.condiciones_default,
      });
      setCfg(r.data);
      toast.success("Configuración guardada");
    } catch { toast.error("Error al guardar"); }
    finally { setSaving(false); }
  };

  const Card = ({ title, children }: { title: string; children: React.ReactNode }) => (
    <div style={{ background: "white", borderRadius: 12, border: "1px solid #E2E8F0", marginBottom: 24, overflow: "hidden" }}>
      <div style={{ padding: "14px 20px", borderBottom: "1px solid #F1F5F9", fontSize: 14, fontWeight: 600, color: "#1E293B" }}>{title}</div>
      <div style={{ padding: 20 }}>{children}</div>
    </div>
  );
  const Grid2 = ({ children }: { children: React.ReactNode }) => (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 20 }}>{children}</div>
  );

  const tabStyle = (active: boolean) => ({
    display: "flex", alignItems: "center", gap: 7, padding: "9px 18px",
    border: active ? "none" : "1px solid #E2E8F0",
    borderRadius: 8, cursor: "pointer", fontSize: 13, fontWeight: 600,
    background: active ? "var(--monza-accent)" : "white", color: active ? "white" : "#475569",
  } as React.CSSProperties);

  return (
    <div style={{ maxWidth: 960 }}>
      {/* Header + Tabs */}
      <div style={{ marginBottom: 24 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 16 }}>
          <Settings size={22} className="monza-ic" />
          <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: "#1E293B" }}>Configuración</h1>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button style={tabStyle(tab === "general")} onClick={() => setTab("general")}>
            <Settings size={14} /> General
          </button>
          <button style={tabStyle(tab === "clientes")} onClick={() => setTab("clientes")}>
            <Users size={14} /> Base de clientes
          </button>
        </div>
      </div>

      {/* Tab: General */}
      {tab === "general" && (
        loading ? <div style={{ textAlign: "center", padding: 60, color: "#94A3B8" }}>Cargando...</div> :
        !cfg ? null : (
          <>
            <Card title="Configuración del día">
              <Grid2>
                <Field label="TC USD → CLP" help="Cuánto vale 1 USD en pesos chilenos.">
                  <Inp type="number" value={cfg.tc_usd_clp} onChange={(v) => set("tc_usd_clp", v)} />
                </Field>
                <Field label="TC EUR → CLP" help="Cuánto vale 1 EUR en pesos chilenos.">
                  <Inp type="number" value={cfg.tc_eur_clp} onChange={(v) => set("tc_eur_clp", v)} />
                </Field>
                <Field label="Tarifa aérea por kg" help="Valor por kilo de flete aéreo desde el proveedor.">
                  <Inp type="number" value={cfg.tarifa_aerea_por_kg} onChange={(v) => set("tarifa_aerea_por_kg", v)} />
                </Field>
                <Field label="Moneda de la tarifa" help="Moneda en la que el proveedor cobra la tarifa aérea.">
                  <select value={cfg.moneda_tarifa} onChange={(e) => set("moneda_tarifa", e.target.value)}
                    style={{ width: "100%", marginTop: 6, padding: "8px 12px", border: "1px solid #E2E8F0", borderRadius: 6, fontSize: 14, color: "#1E293B", background: "white" }}>
                    <option value="EUR">EUR</option>
                    <option value="USD">USD</option>
                  </select>
                </Field>
                <Field label="IVA (%)" help="Tasa de IVA aplicada al neto. Chile: 19%.">
                  <Inp type="number" value={cfg.iva_pct} onChange={(v) => set("iva_pct", v)} />
                </Field>
              </Grid2>
              {cfg.ultima_actualizacion && (
                <p style={{ marginTop: 16, fontSize: 11, color: "#94A3B8" }}>
                  Última actualización: {new Date(cfg.ultima_actualizacion).toLocaleString("es-CL")}
                  {cfg.usuario_email ? ` por ${cfg.usuario_email}` : ""}
                </p>
              )}
            </Card>

            <Card title="Datos de la empresa (para PDF de cotizaciones)">
              <Grid2>
                <Field label="Razón Social"><Inp value={cfg.razon_social} onChange={(v) => set("razon_social", v)} /></Field>
                <Field label="RUT"><Inp value={cfg.rut_empresa} onChange={(v) => set("rut_empresa", v)} /></Field>
                <Field label="Dirección"><Inp value={cfg.direccion} onChange={(v) => set("direccion", v)} /></Field>
                <Field label="Giro"><Inp value={cfg.giro} onChange={(v) => set("giro", v)} /></Field>
                <Field label="Email"><Inp value={cfg.email_empresa} onChange={(v) => set("email_empresa", v)} /></Field>
              </Grid2>
            </Card>

            <Card title="Datos bancarios (opcional)">
              <Grid2>
                <Field label="Banco"><Inp value={cfg.banco || ""} onChange={(v) => set("banco", v)} placeholder="Banco Estado" /></Field>
                <Field label="Tipo de cuenta"><Inp value={cfg.tipo_cuenta || ""} onChange={(v) => set("tipo_cuenta", v)} placeholder="Cuenta Corriente" /></Field>
                <Field label="Número de cuenta"><Inp value={cfg.numero_cuenta || ""} onChange={(v) => set("numero_cuenta", v)} placeholder="123456789" /></Field>
              </Grid2>
            </Card>

            <Card title="Condiciones de servicio por defecto">
              <textarea value={cfg.condiciones_default || ""} onChange={(e) => set("condiciones_default", e.target.value)} rows={4}
                style={{ width: "100%", padding: "8px 12px", border: "1px solid #E2E8F0", borderRadius: 6, fontSize: 13, color: "#1E293B", resize: "vertical", boxSizing: "border-box" }} />
              <p style={{ fontSize: 11, color: "#94A3B8", marginTop: 4 }}>Se incluyen en el PDF de cada cotización (una condición por línea).</p>
            </Card>

            <Card title="Cómo se usa">
              <p style={{ fontSize: 13, color: "#475569", margin: "0 0 8px" }}>
                Estos valores se aplican automáticamente cuando se abre la <strong>calculadora de precios</strong> en cualquier lead.
              </p>
              <ul style={{ fontSize: 12, color: "#64748B", margin: 0, paddingLeft: 18, lineHeight: 1.8 }}>
                <li><strong>Fórmula del flete:</strong> peso (kg) × tarifa × TC según moneda de la tarifa</li>
                <li><strong>Precio neto:</strong> (costo_CLP + flete_CLP) × (1 + markup/100)</li>
                <li><strong>Precio bruto:</strong> neto × (1 + IVA/100)</li>
              </ul>
            </Card>

            <div style={{ display: "flex", gap: 12, justifyContent: "flex-end" }}>
              <button onClick={() => { setCfg(p => p ? { ...p, ...DEFAULTS } : p); toast("Valores restablecidos (no guardados aún)", { icon: "↺" }); }}
                style={{ display: "flex", alignItems: "center", gap: 6, padding: "10px 18px", border: "1px solid #E2E8F0", borderRadius: 8, background: "white", cursor: "pointer", fontSize: 13, color: "#475569" }}>
                <RefreshCw size={14} /> Restablecer
              </button>
              <button onClick={handleSave} disabled={saving}
                style={{ display: "flex", alignItems: "center", gap: 6, padding: "10px 20px", border: "none", borderRadius: 8, background: "var(--monza-accent)", cursor: "pointer", fontSize: 13, color: "white", fontWeight: 600, opacity: saving ? 0.7 : 1 }}>
                <Save size={14} /> {saving ? "Guardando..." : "Guardar configuración"}
              </button>
            </div>
          </>
        )
      )}

      {/* Tab: Clientes */}
      {tab === "clientes" && <ClientesTab />}
    </div>
  );
}
