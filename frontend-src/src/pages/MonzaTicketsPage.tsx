import { useState, useEffect, useCallback } from "react";
import { LifeBuoy, Plus, RefreshCw, Send, X, MessageSquare, Clock } from "lucide-react";
import { monzaTicketsAPI } from "../services/monzaApi";
import { useMonzaTheme } from "./MonzaLayout";
import { useAuthStore } from "../stores/authStore";
import toast from "react-hot-toast";

interface Respuesta {
  id: number;
  autor_id: number | null;
  autor_nombre: string | null;
  es_solicitante: boolean;
  mensaje: string;
  fecha_creacion: string | null;
}
interface Ticket {
  id: number;
  numero: string;
  titulo: string;
  descripcion: string;
  categoria: string;
  prioridad: string;
  estado: string;
  solicitante_id: number | null;
  solicitante_nombre: string | null;
  fecha_creacion: string | null;
  fecha_actualizacion: string | null;
  fecha_cierre: string | null;
  n_respuestas?: number;
  respuestas?: Respuesta[];
}

const CATEGORIAS = [
  { v: "bug", l: "Error / Bug" },
  { v: "mejora", l: "Mejora" },
  { v: "soporte", l: "Soporte" },
  { v: "consulta", l: "Consulta" },
];
const PRIORIDADES = [
  { v: "baja", l: "Baja" },
  { v: "media", l: "Media" },
  { v: "alta", l: "Alta" },
  { v: "urgente", l: "Urgente" },
];
const ESTADOS = [
  { v: "abierto", l: "Abierto" },
  { v: "en_progreso", l: "En progreso" },
  { v: "resuelto", l: "Resuelto" },
  { v: "cerrado", l: "Cerrado" },
];
const ESTADO_STYLE: Record<string, { bg: string; color: string }> = {
  abierto:     { bg: "rgba(59,130,246,.14)",  color: "#3B82F6" },
  en_progreso: { bg: "rgba(245,158,11,.14)",  color: "#D97706" },
  resuelto:    { bg: "rgba(16,185,129,.14)",  color: "#059669" },
  cerrado:     { bg: "rgba(148,163,184,.16)", color: "#64748B" },
};
const PRIORIDAD_COLOR: Record<string, string> = { baja: "#94A3B8", media: "#3B82F6", alta: "#D97706", urgente: "#DC2626" };
const CAT_LABEL: Record<string, string> = Object.fromEntries(CATEGORIAS.map((c) => [c.v, c.l]));

function fmt(d: string | null) {
  if (!d) return "—";
  const dt = new Date(d);
  return dt.toLocaleDateString("es-CL", { day: "2-digit", month: "2-digit", year: "2-digit" }) +
    " " + dt.toLocaleTimeString("es-CL", { hour: "2-digit", minute: "2-digit" });
}
function EstadoBadge({ estado }: { estado: string }) {
  const s = ESTADO_STYLE[estado] || ESTADO_STYLE.cerrado;
  const l = ESTADOS.find((e) => e.v === estado)?.l || estado;
  return <span style={{ fontSize: 11, fontWeight: 700, background: s.bg, color: s.color, padding: "2px 9px", borderRadius: 20 }}>{l}</span>;
}

export default function MonzaTicketsPage() {
  const { dark } = useMonzaTheme();
  const currentUser = useAuthStore((s) => s.user);
  const [tickets, setTickets] = useState<Ticket[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState("abiertos");
  const [showNuevo, setShowNuevo] = useState(false);
  const [selId, setSelId] = useState<number | null>(null);

  const bg  = dark ? "#131b3e" : "white";
  const bd  = dark ? "#1e2a4a" : "#E2E8F0";
  const txt = dark ? "white"   : "#1E293B";
  const sub = dark ? "#8899cc" : "#64748B";
  const inputBg = dark ? "#0d1321" : "white";

  const fetchAll = useCallback(async () => {
    setLoading(true);
    try {
      const estado = (tab === "abiertos" || tab === "todos") ? undefined : tab;
      const [listRes, cntRes] = await Promise.all([
        monzaTicketsAPI.list(estado ? { estado } : {}),
        monzaTicketsAPI.counts(),
      ]);
      setTickets(listRes.data);
      setCounts(cntRes.data);
    } catch { toast.error("Error al cargar tickets"); }
    finally { setLoading(false); }
  }, [tab]);

  useEffect(() => { fetchAll(); }, [fetchAll]);

  const visibles = tab === "abiertos"
    ? tickets.filter((t) => t.estado === "abierto" || t.estado === "en_progreso")
    : tickets;
  const abiertosTotal = counts.abiertos_total ?? 0;

  return (
    <div>
      {/* Header */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", flexWrap: "wrap", gap: 12, marginBottom: 18 }}>
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
            <LifeBuoy size={22} className="monza-ic" />
            <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: txt }}>Tickets de Soporte</h1>
          </div>
          <p style={{ margin: 0, fontSize: 13, color: sub, maxWidth: 640 }}>
            Registra solicitudes de cambio, errores y consultas. Cada ticket es una conversación: tú y el
            equipo pueden responder y re-responder hasta resolverlo.
          </p>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button onClick={fetchAll} title="Actualizar"
            style={{ padding: "8px 10px", border: `1px solid ${bd}`, borderRadius: 8, background: bg, cursor: "pointer", color: sub }}>
            <RefreshCw size={15} />
          </button>
          <button onClick={() => setShowNuevo(true)}
            style={{ padding: "8px 14px", border: "none", borderRadius: 8, background: "var(--monza-accent)", color: "white", cursor: "pointer", fontWeight: 600, fontSize: 13, display: "flex", alignItems: "center", gap: 6 }}>
            <Plus size={15} /> Nuevo ticket
          </button>
        </div>
      </div>

      {/* Tabs */}
      <div style={{ display: "flex", gap: 6, marginBottom: 14, flexWrap: "wrap" }}>
        {[
          { v: "abiertos", l: `Abiertos (${abiertosTotal})` },
          { v: "resuelto", l: `Resueltos (${counts.resuelto ?? 0})` },
          { v: "cerrado", l: `Cerrados (${counts.cerrado ?? 0})` },
          { v: "todos", l: "Todos" },
        ].map((t) => (
          <button key={t.v} onClick={() => setTab(t.v)}
            style={{
              padding: "7px 14px", borderRadius: 8, fontSize: 13, fontWeight: 600, cursor: "pointer",
              border: `1px solid ${tab === t.v ? "var(--monza-accent)" : bd}`,
              background: tab === t.v ? "var(--monza-accent)" : bg,
              color: tab === t.v ? "white" : sub,
            }}>
            {t.l}
          </button>
        ))}
      </div>

      {/* Tabla */}
      <div style={{ background: bg, border: `1px solid ${bd}`, borderRadius: 10, overflow: "hidden" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
          <thead>
            <tr style={{ background: dark ? "#0d1321" : "#F8FAFC", borderBottom: `1px solid ${bd}` }}>
              {["Folio", "Título", "Categoría", "Prioridad", "Estado", "Actualizado"].map((h) => (
                <th key={h} style={{ padding: "10px 14px", textAlign: "left", fontWeight: 600, fontSize: 11, color: sub, textTransform: "uppercase", letterSpacing: 0.5, whiteSpace: "nowrap" }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={6} style={{ padding: 40, textAlign: "center", color: "#94A3B8" }}>Cargando...</td></tr>
            ) : visibles.length === 0 ? (
              <tr><td colSpan={6} style={{ padding: 48, textAlign: "center", color: "#94A3B8" }}>
                <LifeBuoy size={30} color={bd} style={{ display: "block", margin: "0 auto 8px" }} />
                No hay tickets en esta vista.
              </td></tr>
            ) : visibles.map((t, i) => (
              <tr key={t.id} onClick={() => setSelId(t.id)} style={{
                borderBottom: `1px solid ${dark ? "#1e2a4a" : "#F1F5F9"}`,
                background: i % 2 === 0 ? bg : (dark ? "#0f1629" : "#FAFAFA"), cursor: "pointer",
              }}>
                <td style={{ padding: "10px 14px", fontFamily: "monospace", fontSize: 12, fontWeight: 700, color: "var(--monza-accent)", whiteSpace: "nowrap" }}>{t.numero}</td>
                <td style={{ padding: "10px 14px" }}>
                  <div style={{ fontWeight: 500, color: txt }}>{t.titulo}</div>
                  {(t.n_respuestas ?? 0) > 0 && (
                    <div style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 11, color: sub, marginTop: 2 }}>
                      <MessageSquare size={11} /> {t.n_respuestas} respuesta{(t.n_respuestas ?? 0) > 1 ? "s" : ""}
                    </div>
                  )}
                </td>
                <td style={{ padding: "10px 14px", fontSize: 12, color: sub }}>{CAT_LABEL[t.categoria] || t.categoria}</td>
                <td style={{ padding: "10px 14px" }}>
                  <span style={{ fontSize: 12, fontWeight: 700, color: PRIORIDAD_COLOR[t.prioridad] || "#94A3B8" }}>
                    {PRIORIDADES.find((p) => p.v === t.prioridad)?.l || t.prioridad}
                  </span>
                </td>
                <td style={{ padding: "10px 14px" }}><EstadoBadge estado={t.estado} /></td>
                <td style={{ padding: "10px 14px", fontSize: 12, color: sub, whiteSpace: "nowrap" }}>{fmt(t.fecha_actualizacion)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {showNuevo && (
        <NuevoModal dark={dark} onClose={() => setShowNuevo(false)}
          onCreated={(id) => { setShowNuevo(false); fetchAll(); setSelId(id); }} />
      )}
      {selId != null && (
        <DetalleModal dark={dark} id={selId} currentUserId={currentUser?.id ?? null}
          onClose={() => { setSelId(null); fetchAll(); }} />
      )}
    </div>
  );
}

function NuevoModal({ dark, onClose, onCreated }: { dark: boolean; onClose: () => void; onCreated: (id: number) => void }) {
  const [titulo, setTitulo] = useState("");
  const [descripcion, setDescripcion] = useState("");
  const [categoria, setCategoria] = useState("soporte");
  const [prioridad, setPrioridad] = useState("media");
  const [saving, setSaving] = useState(false);

  const bg  = dark ? "#131b3e" : "white";
  const bd  = dark ? "#1e2a4a" : "#E2E8F0";
  const txt = dark ? "white"   : "#1E293B";
  const sub = dark ? "#8899cc" : "#64748B";
  const inputStyle = { width: "100%", padding: "8px 10px", borderRadius: 8, border: `1px solid ${bd}`, background: dark ? "#0d1321" : "white", color: dark ? "white" : "#1E293B", fontSize: 13, boxSizing: "border-box" as const };

  const submit = async () => {
    if (!titulo.trim() || !descripcion.trim()) return;
    setSaving(true);
    try {
      const { data } = await monzaTicketsAPI.crear({ titulo, descripcion, categoria, prioridad });
      toast.success(`Ticket ${data.numero} creado`);
      onCreated(data.id);
    } catch { toast.error("No se pudo crear el ticket"); }
    finally { setSaving(false); }
  };

  return (
    <div style={{ position: "fixed", inset: 0, zIndex: 60, background: "rgba(0,0,0,.5)", display: "flex", alignItems: "center", justifyContent: "center", padding: 16 }} onClick={onClose}>
      <div style={{ width: "100%", maxWidth: 520, background: bg, border: `1px solid ${bd}`, borderRadius: 12 }} onClick={(e) => e.stopPropagation()}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "14px 18px", borderBottom: `1px solid ${bd}` }}>
          <span style={{ display: "flex", alignItems: "center", gap: 8, fontWeight: 700, color: txt }}><Plus size={16} className="monza-ic" /> Nuevo ticket</span>
          <button onClick={onClose} style={{ border: "none", background: "none", cursor: "pointer", color: sub }}><X size={18} /></button>
        </div>
        <div style={{ padding: 18, display: "flex", flexDirection: "column", gap: 14 }}>
          <div>
            <label style={{ display: "block", fontSize: 12, fontWeight: 600, color: sub, marginBottom: 5 }}>Título</label>
            <input style={inputStyle} value={titulo} onChange={(e) => setTitulo(e.target.value)} placeholder="Resumen breve de la solicitud" autoFocus />
          </div>
          <div>
            <label style={{ display: "block", fontSize: 12, fontWeight: 600, color: sub, marginBottom: 5 }}>Descripción</label>
            <textarea style={{ ...inputStyle, minHeight: 110, resize: "vertical" }} value={descripcion} onChange={(e) => setDescripcion(e.target.value)}
              placeholder="Detalla qué necesitas, en qué pantalla, y con qué datos ocurre." />
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            <div>
              <label style={{ display: "block", fontSize: 12, fontWeight: 600, color: sub, marginBottom: 5 }}>Categoría</label>
              <select style={inputStyle} value={categoria} onChange={(e) => setCategoria(e.target.value)}>
                {CATEGORIAS.map((c) => <option key={c.v} value={c.v}>{c.l}</option>)}
              </select>
            </div>
            <div>
              <label style={{ display: "block", fontSize: 12, fontWeight: 600, color: sub, marginBottom: 5 }}>Prioridad</label>
              <select style={inputStyle} value={prioridad} onChange={(e) => setPrioridad(e.target.value)}>
                {PRIORIDADES.map((p) => <option key={p.v} value={p.v}>{p.l}</option>)}
              </select>
            </div>
          </div>
        </div>
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, padding: "14px 18px", borderTop: `1px solid ${bd}` }}>
          <button onClick={onClose} style={{ padding: "8px 14px", borderRadius: 8, border: `1px solid ${bd}`, background: bg, color: sub, cursor: "pointer", fontSize: 13 }}>Cancelar</button>
          <button onClick={submit} disabled={!titulo.trim() || !descripcion.trim() || saving}
            style={{ padding: "8px 16px", borderRadius: 8, border: "none", background: "var(--monza-accent)", color: "white", cursor: "pointer", fontWeight: 600, fontSize: 13, display: "flex", alignItems: "center", gap: 6, opacity: (!titulo.trim() || !descripcion.trim() || saving) ? 0.6 : 1 }}>
            {saving ? <RefreshCw size={15} className="monza-spin" /> : <Send size={15} />} Crear ticket
          </button>
        </div>
      </div>
    </div>
  );
}

function DetalleModal({ dark, id, currentUserId, onClose }: { dark: boolean; id: number; currentUserId: number | null; onClose: () => void }) {
  const [t, setT] = useState<Ticket | null>(null);
  const [loading, setLoading] = useState(true);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const bg  = dark ? "#131b3e" : "white";
  const bd  = dark ? "#1e2a4a" : "#E2E8F0";
  const txt = dark ? "white"   : "#1E293B";
  const sub = dark ? "#8899cc" : "#64748B";

  const load = useCallback(async () => {
    try { const { data } = await monzaTicketsAPI.get(id); setT(data); }
    catch { toast.error("Error al cargar el ticket"); }
    finally { setLoading(false); }
  }, [id]);
  useEffect(() => { load(); }, [load]);

  const responder = async () => {
    if (!msg.trim()) return;
    setBusy(true);
    try { const { data } = await monzaTicketsAPI.responder(id, msg); setT(data); setMsg(""); }
    catch { toast.error("No se pudo enviar la respuesta"); }
    finally { setBusy(false); }
  };
  const cambiarEstado = async (estado: string) => {
    setBusy(true);
    try { const { data } = await monzaTicketsAPI.cambiarEstado(id, estado); setT(data); toast.success(`Marcado como ${ESTADOS.find((e) => e.v === estado)?.l || estado}`); }
    catch { toast.error("No se pudo cambiar el estado"); }
    finally { setBusy(false); }
  };

  const cerrado = t?.estado === "cerrado";

  return (
    <div style={{ position: "fixed", inset: 0, zIndex: 60, background: "rgba(0,0,0,.5)", display: "flex", alignItems: "center", justifyContent: "center", padding: 16 }} onClick={onClose}>
      <div style={{ width: "100%", maxWidth: 680, maxHeight: "90vh", background: bg, border: `1px solid ${bd}`, borderRadius: 12, display: "flex", flexDirection: "column" }} onClick={(e) => e.stopPropagation()}>
        {/* Header */}
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", padding: "14px 18px", borderBottom: `1px solid ${bd}` }}>
          <div style={{ minWidth: 0 }}>
            {loading || !t ? <span style={{ color: sub, fontFamily: "monospace", fontSize: 13 }}>Cargando...</span> : (
              <>
                <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                  <span style={{ fontFamily: "monospace", fontSize: 12, fontWeight: 700, color: "var(--monza-accent)" }}>{t.numero}</span>
                  <EstadoBadge estado={t.estado} />
                  <span style={{ fontSize: 12, fontWeight: 700, color: PRIORIDAD_COLOR[t.prioridad] }}>{PRIORIDADES.find((p) => p.v === t.prioridad)?.l}</span>
                  <span style={{ fontSize: 12, color: sub }}>· {CAT_LABEL[t.categoria] || t.categoria}</span>
                </div>
                <h2 style={{ margin: "6px 0 0", fontSize: 18, fontWeight: 700, color: txt }}>{t.titulo}</h2>
              </>
            )}
          </div>
          <button onClick={onClose} style={{ border: "none", background: "none", cursor: "pointer", color: sub }}><X size={18} /></button>
        </div>

        {/* Conversación */}
        <div style={{ flex: 1, overflowY: "auto", padding: 18, display: "flex", flexDirection: "column", gap: 12 }}>
          {t && (
            <>
              <div style={{ border: `1px solid ${bd}`, borderRadius: 10, padding: 12, background: dark ? "#0f1629" : "#F8FAFC" }}>
                <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
                  <span style={{ fontSize: 12, fontWeight: 700, color: txt }}>{t.solicitante_nombre || "Solicitante"}</span>
                  <span style={{ fontSize: 11, color: sub, display: "flex", alignItems: "center", gap: 4 }}><Clock size={11} />{fmt(t.fecha_creacion)}</span>
                </div>
                <p style={{ margin: 0, fontSize: 13, color: sub, whiteSpace: "pre-wrap" }}>{t.descripcion}</p>
              </div>

              {(t.respuestas || []).map((r) => {
                const mine = r.es_solicitante;
                return (
                  <div key={r.id} style={{
                    border: `1px solid ${mine ? bd : "rgba(59,130,246,.35)"}`, borderRadius: 10, padding: 12,
                    background: mine ? (dark ? "#0f1629" : "#F8FAFC") : "rgba(59,130,246,.08)",
                    marginLeft: mine ? 0 : 28, marginRight: mine ? 28 : 0,
                  }}>
                    <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
                      <span style={{ fontSize: 12, fontWeight: 700, color: mine ? txt : "#3B82F6" }}>
                        {r.autor_nombre || "Usuario"} {mine ? "· solicitante" : "· equipo"}
                      </span>
                      <span style={{ fontSize: 11, color: sub, display: "flex", alignItems: "center", gap: 4 }}><Clock size={11} />{fmt(r.fecha_creacion)}</span>
                    </div>
                    <p style={{ margin: 0, fontSize: 13, color: sub, whiteSpace: "pre-wrap" }}>{r.mensaje}</p>
                  </div>
                );
              })}
            </>
          )}
        </div>

        {/* Footer */}
        <div style={{ borderTop: `1px solid ${bd}`, padding: 18, display: "flex", flexDirection: "column", gap: 12 }}>
          {cerrado ? (
            <div style={{ fontSize: 13, textAlign: "center", color: sub, padding: "4px 0" }}>
              Este ticket está cerrado. Crea uno nuevo si necesitas retomar el tema.
            </div>
          ) : (
            <div style={{ display: "flex", alignItems: "flex-end", gap: 8 }}>
              <textarea value={msg} onChange={(e) => setMsg(e.target.value)} placeholder="Escribe una respuesta..."
                onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && msg.trim()) responder(); }}
                style={{ flex: 1, padding: "8px 10px", borderRadius: 8, border: `1px solid ${bd}`, background: dark ? "#0d1321" : "white", color: txt, fontSize: 13, minHeight: 42, maxHeight: 120, resize: "vertical", boxSizing: "border-box" as const }} />
              <button onClick={responder} disabled={!msg.trim() || busy}
                style={{ padding: "10px 14px", borderRadius: 8, border: "none", background: "var(--monza-accent)", color: "white", cursor: "pointer", opacity: (!msg.trim() || busy) ? 0.6 : 1 }}>
                {busy ? <RefreshCw size={15} className="monza-spin" /> : <Send size={15} />}
              </button>
            </div>
          )}
          {t && (
            <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              <span style={{ fontSize: 12, color: sub }}>Cambiar estado:</span>
              {ESTADOS.filter((e) => e.v !== t.estado).map((e) => (
                <button key={e.v} onClick={() => cambiarEstado(e.v)} disabled={busy}
                  style={{ padding: "5px 11px", borderRadius: 7, border: `1px solid ${bd}`, background: bg, color: sub, cursor: "pointer", fontSize: 12, fontWeight: 600 }}>
                  {e.l}
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
