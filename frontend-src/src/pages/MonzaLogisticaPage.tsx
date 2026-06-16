import { useState, useEffect, useCallback } from "react";
import { Ship, Plane, Search, RefreshCw, Package, ChevronDown, ChevronRight, X, Boxes } from "lucide-react";
import { monzaLogisticaAPI } from "../services/monzaApi";
import { useMonzaTheme } from "./MonzaLayout";
import MonzaDocs from "./MonzaDocs";
import toast from "react-hot-toast";

interface PrepItem { id: number; cot_numero: string; cliente?: string; descripcion: string; numero_parte?: string; marca?: string; calidad?: string; cantidad: number; }
interface Embarque {
  id: number; numero: string; estado: string; awb?: string; forwarder?: string; tracking?: string;
  fecha_despacho?: string; fecha_llegada_est?: string; notas?: string; items_count: number; created_at?: string;
  items?: PrepItem[];
}
interface KPIs { preparados: number; en_bodega_proveedor: number; en_transito: number; en_aduana: number; en_bodega: number; }

const EST: Record<string, { bg: string; color: string; label: string }> = {
  en_bodega_proveedor: { bg: "#F1F5F9", color: "#64748B", label: "En bodega proveedor" },
  en_transito:         { bg: "#EDE9FE", color: "#6D28D9", label: "En tránsito" },
  en_aduana:           { bg: "#FEF3C7", color: "#B45309", label: "En aduana" },
  en_bodega:           { bg: "#DCFCE7", color: "#15803D", label: "Llegó a bodega" },
};
const NEXT: Record<string, string> = { en_bodega_proveedor: "en_transito", en_transito: "en_aduana", en_aduana: "en_bodega" };
const NEXT_LBL: Record<string, string> = { en_transito: "Marcar embarque ✈", en_aduana: "Marcar en aduana", en_bodega: "Marcar llegada a Chile" };

function KpiCard({ label, value, accent }: { label: string; value: number; accent: string }) {
  const { dark } = useMonzaTheme();
  return (
    <div style={{ background: dark ? "#131b3e" : "white", borderRadius: 12, border: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}`, padding: "14px 18px", flex: 1, minWidth: 110 }}>
      <div style={{ width: 24, height: 3, borderRadius: 2, background: accent, marginBottom: 10 }} />
      <div style={{ fontSize: 24, fontWeight: 800, color: dark ? "white" : "#1E293B", lineHeight: 1 }}>{value}</div>
      <div style={{ fontSize: 11, color: dark ? "#8899cc" : "#64748B", marginTop: 4 }}>{label}</div>
    </div>
  );
}

function CrearEmbarqueModal({ items, onClose, onDone }: { items: PrepItem[]; onClose: () => void; onDone: () => void }) {
  const { dark } = useMonzaTheme();
  const [awb, setAwb] = useState(""); const [forwarder, setForwarder] = useState(""); const [tracking, setTracking] = useState("");
  const [fd, setFd] = useState(""); const [fll, setFll] = useState(""); const [notas, setNotas] = useState(""); const [saving, setSaving] = useState(false);
  const bg = dark ? "#131b3e" : "white"; const bd = dark ? "#1e2a4a" : "#E2E8F0"; const txt = dark ? "white" : "#1E293B"; const sub = dark ? "#8899cc" : "#64748B";
  const IS = { width: "100%", padding: "8px 10px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 13, boxSizing: "border-box" as const, background: dark ? "#0d1321" : "#F8FAFC", color: txt };
  const submit = async () => {
    setSaving(true);
    try {
      await monzaLogisticaAPI.crearEmbarque({ item_ids: items.map((i) => i.id), awb: awb || undefined, forwarder: forwarder || undefined, tracking: tracking || undefined, fecha_despacho: fd || undefined, fecha_llegada_est: fll || undefined, notas: notas || undefined });
      toast.success(`Embarque creado · ${items.length} ítem(s)`); onDone();
    } catch { toast.error("Error al crear embarque"); } finally { setSaving(false); }
  };
  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)", zIndex: 1000, display: "flex", alignItems: "center", justifyContent: "center", padding: 16 }}>
      <div style={{ background: bg, border: `1px solid ${bd}`, borderRadius: 14, width: "100%", maxWidth: 520, maxHeight: "90vh", display: "flex", flexDirection: "column" }}>
        <div style={{ background: dark ? "#0a0e1f" : "#F8FAFC", borderBottom: `1px solid ${bd}`, padding: "14px 20px", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <span style={{ fontWeight: 700, fontSize: 15, color: txt, display: "flex", alignItems: "center", gap: 8 }}><Ship size={18} className="monza-ic" /> Crear embarque</span>
          <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", color: sub }}><X size={18} /></button>
        </div>
        <div style={{ padding: "16px 20px", overflowY: "auto" }}>
          <div style={{ background: dark ? "#0d1321" : "#F1F5F9", borderRadius: 8, padding: "10px 14px", marginBottom: 14, fontSize: 12 }}>
            <div style={{ fontWeight: 700, color: sub, marginBottom: 4, textTransform: "uppercase" }}>{items.length} ítem(s) a embarcar</div>
            {items.map((it) => <div key={it.id} style={{ color: txt, padding: "1px 0" }}>{it.descripcion} <span style={{ color: sub }}>· {it.cot_numero} ×{it.cantidad}</span></div>)}
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            <div><label style={{ fontSize: 12, fontWeight: 600, color: sub, display: "block", marginBottom: 4 }}>AWB (guía aérea)</label><input value={awb} onChange={(e) => setAwb(e.target.value)} style={IS} /></div>
            <div><label style={{ fontSize: 12, fontWeight: 600, color: sub, display: "block", marginBottom: 4 }}>Forwarder</label><input value={forwarder} onChange={(e) => setForwarder(e.target.value)} style={IS} /></div>
            <div><label style={{ fontSize: 12, fontWeight: 600, color: sub, display: "block", marginBottom: 4 }}>Tracking</label><input value={tracking} onChange={(e) => setTracking(e.target.value)} style={IS} /></div>
            <div><label style={{ fontSize: 12, fontWeight: 600, color: sub, display: "block", marginBottom: 4 }}>Fecha despacho</label><input type="date" value={fd} onChange={(e) => setFd(e.target.value)} style={{ ...IS, colorScheme: dark ? "dark" : "light" as const }} /></div>
            <div style={{ gridColumn: "1 / -1" }}><label style={{ fontSize: 12, fontWeight: 600, color: sub, display: "block", marginBottom: 4 }}>Llegada estimada</label><input type="date" value={fll} onChange={(e) => setFll(e.target.value)} style={{ ...IS, colorScheme: dark ? "dark" : "light" as const }} /></div>
            <div style={{ gridColumn: "1 / -1" }}><label style={{ fontSize: 12, fontWeight: 600, color: sub, display: "block", marginBottom: 4 }}>Notas</label><textarea value={notas} onChange={(e) => setNotas(e.target.value)} rows={2} style={{ ...IS, resize: "vertical" as const }} /></div>
          </div>
        </div>
        <div style={{ padding: "12px 20px", borderTop: `1px solid ${bd}`, display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <button onClick={onClose} style={{ padding: "8px 18px", border: `1px solid ${bd}`, borderRadius: 8, background: "transparent", color: sub, cursor: "pointer", fontSize: 13 }}>Cancelar</button>
          <button onClick={submit} disabled={saving} style={{ padding: "8px 20px", background: "var(--monza-accent)", border: "none", borderRadius: 8, color: "white", cursor: "pointer", fontWeight: 700, fontSize: 13 }}>{saving ? "Creando..." : "Crear embarque"}</button>
        </div>
      </div>
    </div>
  );
}

export default function MonzaLogisticaPage() {
  const { dark } = useMonzaTheme();
  const [tab, setTab] = useState<"preparados" | "embarques">("preparados");
  const [prep, setPrep] = useState<PrepItem[]>([]);
  const [embs, setEmbs] = useState<Embarque[]>([]);
  const [kpis, setKpis] = useState<KPIs | null>(null);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState("");
  const [sel, setSel] = useState<Set<number>>(new Set());
  const [showModal, setShowModal] = useState(false);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [details, setDetails] = useState<Record<number, Embarque>>({});

  const bg = dark ? "#131b3e" : "white"; const bd = dark ? "#1e2a4a" : "#E2E8F0"; const txt = dark ? "white" : "#1E293B"; const sub = dark ? "#8899cc" : "#64748B";

  const fetchAll = useCallback(async () => {
    setLoading(true);
    try {
      const [k, p, e] = await Promise.all([monzaLogisticaAPI.kpis(), monzaLogisticaAPI.preparados({ q: q || undefined }), monzaLogisticaAPI.listEmbarques()]);
      setKpis(k.data); setPrep(p.data); setEmbs(e.data);
    } catch { toast.error("Error al cargar logística"); } finally { setLoading(false); }
  }, [q]);
  useEffect(() => { fetchAll(); }, [fetchAll]);

  const toggleSel = (id: number) => setSel((p) => { const n = new Set(p); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const advance = async (e: Embarque) => {
    const ne = NEXT[e.estado]; if (!ne) return;
    try { await monzaLogisticaAPI.updateEmbarque(e.id, { estado: ne }); toast.success(`${e.numero} → ${EST[ne].label}`); fetchAll(); } catch { toast.error("Error"); }
  };
  const toggleExp = async (id: number) => {
    setExpanded((p) => { const n = new Set(p); n.has(id) ? n.delete(id) : n.add(id); return n; });
    if (!details[id]) { try { const r = await monzaLogisticaAPI.getEmbarque(id); setDetails((p) => ({ ...p, [id]: r.data })); } catch {} }
  };

  const selItems = prep.filter((i) => sel.has(i.id));

  return (
    <div>
      <div style={{ marginBottom: 18 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
          <Ship size={22} className="monza-ic" />
          <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: txt }}>Logística</h1>
        </div>
        <p style={{ margin: 0, fontSize: 13, color: sub }}>Agrupa ítems preparados en embarques y gestiona su tránsito hasta la llegada a Chile.</p>
      </div>

      {kpis && (
        <div style={{ display: "flex", gap: 10, marginBottom: 18, flexWrap: "wrap" }}>
          <KpiCard label="Preparados" value={kpis.preparados} accent="#F59E0B" />
          <KpiCard label="En proveedor" value={kpis.en_bodega_proveedor} accent="#64748B" />
          <KpiCard label="En tránsito" value={kpis.en_transito} accent="#6366F1" />
          <KpiCard label="En aduana" value={kpis.en_aduana} accent="#B45309" />
          <KpiCard label="Llegados" value={kpis.en_bodega} accent="#10B981" />
        </div>
      )}

      <div style={{ display: "flex", gap: 4, marginBottom: 14, borderBottom: `1px solid ${bd}` }}>
        {([["preparados", "Por embarcar", prep.length], ["embarques", "Embarques", embs.length]] as const).map(([k, l, c]) => (
          <button key={k} onClick={() => setTab(k)} style={{ padding: "9px 16px", border: "none", background: "transparent", cursor: "pointer", fontSize: 13, fontWeight: 600, color: tab === k ? "var(--monza-accent)" : sub, borderBottom: `2px solid ${tab === k ? "var(--monza-accent)" : "transparent"}`, marginBottom: -1 }}>
            {l} <span style={{ fontSize: 11, background: dark ? "#1e2a4a" : "#F1F5F9", padding: "1px 7px", borderRadius: 10, marginLeft: 4 }}>{c}</span>
          </button>
        ))}
      </div>

      <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 12, flexWrap: "wrap" }}>
        <div style={{ position: "relative", flex: 1, minWidth: 240 }}>
          <Search size={14} style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)", color: "#94A3B8" }} />
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Buscar..." style={{ width: "100%", padding: "8px 10px 8px 32px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 13, boxSizing: "border-box" as const, background: bg, color: txt }} />
        </div>
        {tab === "preparados" && sel.size > 0 && (
          <button onClick={() => setShowModal(true)} style={{ padding: "8px 16px", background: "var(--monza-accent)", border: "none", borderRadius: 8, color: "white", cursor: "pointer", fontWeight: 700, fontSize: 13, display: "flex", alignItems: "center", gap: 6 }}><Ship size={14} /> Embarcar {sel.size} ítem(s)</button>
        )}
        <button onClick={fetchAll} style={{ padding: "8px 10px", border: `1px solid ${bd}`, borderRadius: 6, background: bg, cursor: "pointer", color: sub }}><RefreshCw size={14} /></button>
      </div>

      <div style={{ background: bg, border: `1px solid ${bd}`, borderRadius: 10, overflow: "hidden" }}>
        {loading ? <div style={{ padding: 40, textAlign: "center", color: "#94A3B8" }}>Cargando...</div>
        : tab === "preparados" ? (
          prep.length === 0 ? <div style={{ padding: 48, textAlign: "center", color: "#94A3B8" }}><Package size={32} color="#E2E8F0" style={{ display: "block", margin: "0 auto 8px" }} />No hay ítems preparados para embarcar.</div>
          : <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead><tr style={{ background: dark ? "#0d1321" : "#F8FAFC", borderBottom: `1px solid ${bd}` }}>
                <th style={{ width: 36, padding: "10px 8px", textAlign: "center" }}><input type="checkbox" checked={sel.size === prep.length && prep.length > 0} onChange={() => setSel(sel.size === prep.length ? new Set() : new Set(prep.map((i) => i.id)))} style={{ accentColor: "var(--monza-accent)" }} /></th>
                {["N° COT", "Cliente", "Repuesto", "Marca/Calidad", "Cant."].map((h) => <th key={h} style={{ padding: "10px 12px", textAlign: h === "Cant." ? "right" : "left", fontWeight: 600, fontSize: 11, color: sub, textTransform: "uppercase" as const }}>{h}</th>)}
              </tr></thead>
              <tbody>{prep.map((it) => (
                <tr key={it.id} onClick={() => toggleSel(it.id)} style={{ borderBottom: `1px solid ${dark ? "#1e2a4a" : "#F1F5F9"}`, background: sel.has(it.id) ? (dark ? "#1a2340" : "#F5F3FF") : "transparent", cursor: "pointer" }}>
                  <td style={{ textAlign: "center", padding: 8 }}><input type="checkbox" checked={sel.has(it.id)} onChange={() => toggleSel(it.id)} onClick={(e) => e.stopPropagation()} style={{ accentColor: "var(--monza-accent)" }} /></td>
                  <td style={{ padding: "9px 12px", fontWeight: 600, color: "var(--monza-accent)", fontSize: 12 }}>{it.cot_numero}</td>
                  <td style={{ padding: "9px 12px", color: txt }}>{it.cliente || "—"}</td>
                  <td style={{ padding: "9px 12px" }}><div style={{ color: txt, fontWeight: 500 }}>{it.descripcion}</div>{it.numero_parte && <div style={{ fontSize: 10, color: sub }}>{it.numero_parte}</div>}</td>
                  <td style={{ padding: "9px 12px", color: sub, fontSize: 12 }}>{it.marca || "—"}{it.calidad ? ` · ${it.calidad}` : ""}</td>
                  <td style={{ padding: "9px 12px", textAlign: "right", color: txt }}>{it.cantidad}</td>
                </tr>
              ))}</tbody>
            </table>
        ) : (
          embs.length === 0 ? <div style={{ padding: 48, textAlign: "center", color: "#94A3B8" }}><Ship size={32} color="#E2E8F0" style={{ display: "block", margin: "0 auto 8px" }} />No hay embarques.</div>
          : <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead><tr style={{ background: dark ? "#0d1321" : "#F8FAFC", borderBottom: `1px solid ${bd}` }}>
                <th style={{ width: 30 }}></th>
                {["N° Embarque", "Estado", "AWB / Forwarder", "Ítems", "Llegada est.", "Acción"].map((h) => <th key={h} style={{ padding: "10px 12px", textAlign: "left", fontWeight: 600, fontSize: 11, color: sub, textTransform: "uppercase" as const }}>{h}</th>)}
              </tr></thead>
              <tbody>{embs.flatMap((e) => {
                const es = EST[e.estado] || EST.en_bodega_proveedor; const isExp = expanded.has(e.id); const det = details[e.id];
                const rows = [
                  <tr key={e.id} style={{ borderBottom: isExp ? "none" : `1px solid ${dark ? "#1e2a4a" : "#F1F5F9"}` }}>
                    <td style={{ textAlign: "center", color: "#94A3B8", cursor: "pointer" }} onClick={() => toggleExp(e.id)}>{isExp ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</td>
                    <td style={{ padding: "10px 12px", fontWeight: 700, color: "var(--monza-accent)" }}>{e.numero}</td>
                    <td style={{ padding: "10px 12px" }}><span style={{ fontSize: 11, background: es.bg, color: es.color, padding: "3px 10px", borderRadius: 10, fontWeight: 600 }}>{es.label}</span></td>
                    <td style={{ padding: "10px 12px", color: sub, fontSize: 12 }}>{e.awb || "—"}{e.forwarder ? ` · ${e.forwarder}` : ""}</td>
                    <td style={{ padding: "10px 12px", color: sub }}>{e.items_count}</td>
                    <td style={{ padding: "10px 12px", color: sub, fontSize: 12 }}>{e.fecha_llegada_est || "—"}</td>
                    <td style={{ padding: "10px 12px" }}>{NEXT[e.estado] && <button onClick={() => advance(e)} style={{ padding: "4px 10px", border: "1px solid #6366F1", borderRadius: 6, background: "transparent", color: "#6366F1", cursor: "pointer", fontSize: 11, fontWeight: 600, display: "flex", alignItems: "center", gap: 4 }}><Plane size={11} /> {NEXT_LBL[NEXT[e.estado]]}</button>}</td>
                  </tr>,
                ];
                if (isExp) rows.push(
                  <tr key={`d-${e.id}`} style={{ borderBottom: `1px solid ${bd}` }}>
                    <td colSpan={7} style={{ padding: "8px 16px 14px 44px", background: dark ? "#0a0e1f" : "#F8FAFF" }}>
                      <div style={{ border: `1px solid ${bd}`, borderRadius: 8, overflow: "hidden", marginBottom: 10 }}>
                        <div style={{ background: dark ? "#0d1321" : "#F8FAFC", padding: "6px 10px", fontSize: 11, fontWeight: 700, color: sub, textTransform: "uppercase" }}>Ítems ({(det?.items || []).length})</div>
                        {(det?.items || []).map((it) => <div key={it.id} style={{ padding: "6px 10px", borderTop: `1px solid ${bd}`, fontSize: 12 }}><span style={{ color: txt, fontWeight: 500 }}>{it.descripcion}</span> <span style={{ color: sub }}>· {it.cot_numero} · {it.cliente} ×{it.cantidad}</span></div>)}
                      </div>
                      <MonzaDocs entidad="embarque" entidadId={e.id} categorias={["factura comercial", "packing list", "AWB", "certificado origen", "otro"]} titulo="Documentos del embarque" />
                    </td>
                  </tr>
                );
                return rows;
              })}</tbody>
            </table>
        )}
      </div>

      {showModal && <CrearEmbarqueModal items={selItems} onClose={() => setShowModal(false)} onDone={() => { setShowModal(false); setSel(new Set()); fetchAll(); }} />}
    </div>
  );
}
