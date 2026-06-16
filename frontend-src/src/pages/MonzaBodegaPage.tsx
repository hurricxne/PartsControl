import { useState, useEffect, useCallback } from "react";
import { Boxes, Package, RefreshCw, X, CheckCircle, AlertTriangle, Truck, Ship } from "lucide-react";
import { monzaBodegaAPI } from "../services/monzaApi";
import { useMonzaTheme } from "./MonzaLayout";
import MonzaDocs from "./MonzaDocs";
import toast from "react-hot-toast";

interface EmbRecv { id: number; numero: string; estado: string; awb?: string; forwarder?: string; tracking?: string; fecha_llegada_est?: string; items_count: number; recepcion_id?: number; recepcion_abierta?: boolean; }
interface RecItem { id: number; cot_numero?: string; cliente?: string; descripcion: string; numero_parte?: string; marca?: string; cantidad: number; ocp_proveedor?: string; estado_recepcion?: string; qty_recibida?: number; qty_danada?: number; observacion?: string; fotos?: number; }
interface Recepcion { id: number; embarque_numero?: string; estado: string; total: number; marcados: number; items: RecItem[]; }
interface BodegaItem { id: number; cot_numero?: string; cliente?: string; descripcion: string; numero_parte?: string; marca?: string; cantidad: number; ocp_proveedor?: string; }
interface Reclamo { id: number; cot_numero?: string; descripcion?: string; motivo: string; qty_afectada: number; estado: string; observacion?: string; ocp_proveedor?: string; fecha_creacion?: string; }
interface KPIs { a_recibir: number; en_bodega: number; despachado: number; reclamos_pendientes: number; }

const RECEP_OPTS = [
  { v: "completo", label: "Completo", color: "#15803D" },
  { v: "sobrante", label: "Sobrante", color: "#0369A1" },
  { v: "danado_utilizable", label: "Dañado (utilizable)", color: "#B45309" },
  { v: "faltante", label: "Faltante", color: "#B91C1C" },
  { v: "danado_no_utilizable", label: "Dañado (no utiliz.)", color: "#B91C1C" },
  { v: "no_llego", label: "No llegó", color: "#6D28D9" },
];
const MOTIVO_LBL: Record<string, string> = { faltante: "Faltante", danado_no_utilizable: "Dañado no utiliz.", no_llego: "No llegó", danado_utilizable: "Dañado utiliz." };
const RECLAMO_EST: Record<string, { bg: string; color: string; label: string }> = { pendiente: { bg: "#FEF3C7", color: "#B45309", label: "Pendiente" }, reclamado: { bg: "#DBEAFE", color: "#1D4ED8", label: "Reclamado" }, resuelto: { bg: "#DCFCE7", color: "#15803D", label: "Resuelto" }, anulado: { bg: "#F1F5F9", color: "#64748B", label: "Anulado" } };

function KpiCard({ label, value, accent }: { label: string; value: number; accent: string }) {
  const { dark } = useMonzaTheme();
  return (<div style={{ background: dark ? "#131b3e" : "white", borderRadius: 12, border: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}`, padding: "14px 18px", flex: 1, minWidth: 120 }}><div style={{ width: 24, height: 3, borderRadius: 2, background: accent, marginBottom: 10 }} /><div style={{ fontSize: 24, fontWeight: 800, color: dark ? "white" : "#1E293B", lineHeight: 1 }}>{value}</div><div style={{ fontSize: 11, color: dark ? "#8899cc" : "#64748B", marginTop: 4 }}>{label}</div></div>);
}

// ── Panel de recepción (modal) ────────────────────────────────────────────────
function RecepcionPanel({ recId, onClose, onClosed }: { recId: number; onClose: () => void; onClosed: () => void }) {
  const { dark } = useMonzaTheme();
  const [rec, setRec] = useState<Recepcion | null>(null);
  const [closing, setClosing] = useState(false);
  const bg = dark ? "#131b3e" : "white"; const bd = dark ? "#1e2a4a" : "#E2E8F0"; const txt = dark ? "white" : "#1E293B"; const sub = dark ? "#8899cc" : "#64748B";
  const load = useCallback(async () => { try { const r = await monzaBodegaAPI.getRecepcion(recId); setRec(r.data); } catch { toast.error("Error"); } }, [recId]);
  useEffect(() => { load(); }, [load]);

  const marcar = async (it: RecItem, estado: string) => {
    try {
      await monzaBodegaAPI.marcarItem(recId, it.id, { estado_recepcion: estado, qty_recibida: it.cantidad, qty_danada: estado.includes("danado") ? it.cantidad : 0 });
      load();
    } catch (e: unknown) {
      const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || "Error al marcar";
      toast.error(msg);
    }
  };
  const cerrar = async () => {
    setClosing(true);
    try { const r = await monzaBodegaAPI.cerrarRecepcion(recId); toast.success(`Recepción cerrada · ${r.data.en_bodega} a bodega, ${r.data.reclamos} reclamo(s)`); onClosed(); }
    catch (e: unknown) { toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || "Error al cerrar"); }
    finally { setClosing(false); }
  };

  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)", zIndex: 1000, display: "flex", alignItems: "center", justifyContent: "center", padding: 16 }}>
      <div style={{ background: bg, border: `1px solid ${bd}`, borderRadius: 14, width: "100%", maxWidth: 760, maxHeight: "92vh", display: "flex", flexDirection: "column" }}>
        <div style={{ background: dark ? "#0a0e1f" : "#F8FAFC", borderBottom: `1px solid ${bd}`, padding: "14px 20px", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div><span style={{ fontWeight: 700, fontSize: 15, color: txt, display: "flex", alignItems: "center", gap: 8 }}><Package size={18} className="monza-ic" /> Recepción · {rec?.embarque_numero}</span>
            {rec && <span style={{ fontSize: 12, color: sub }}>{rec.marcados}/{rec.total} ítems marcados</span>}</div>
          <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", color: sub }}><X size={18} /></button>
        </div>
        <div style={{ padding: "14px 20px", overflowY: "auto", flex: 1 }}>
          {!rec ? <div style={{ color: sub }}>Cargando...</div> : rec.items.map((it) => (
            <div key={it.id} style={{ border: `1px solid ${bd}`, borderRadius: 10, padding: "10px 14px", marginBottom: 10 }}>
              <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
                <div><div style={{ fontWeight: 600, fontSize: 13, color: txt }}>{it.descripcion} <span style={{ color: sub, fontWeight: 400 }}>×{it.cantidad}</span></div>
                  <div style={{ fontSize: 11, color: sub }}>{[it.cot_numero, it.numero_parte, it.marca, it.cliente].filter(Boolean).join(" · ")}</div></div>
                {it.estado_recepcion && <CheckCircle size={16} color="#16A34A" />}
              </div>
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 8 }}>
                {RECEP_OPTS.map((o) => (
                  <button key={o.v} onClick={() => marcar(it, o.v)} style={{ padding: "4px 10px", borderRadius: 6, border: `1px solid ${it.estado_recepcion === o.v ? o.color : bd}`, background: it.estado_recepcion === o.v ? `${o.color}18` : "transparent", color: it.estado_recepcion === o.v ? o.color : sub, cursor: "pointer", fontSize: 11, fontWeight: 600 }}>{o.label}</button>
                ))}
              </div>
              {/* Fotos (obligatorias si dañado) */}
              <MonzaDocs entidad="recepcion_item" entidadId={it.id} categorias={["foto", "evidencia"]} titulo={`Fotos${it.estado_recepcion?.includes("danado") ? " (obligatoria)" : ""}`} />
            </div>
          ))}
        </div>
        <div style={{ padding: "12px 20px", borderTop: `1px solid ${bd}`, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <span style={{ fontSize: 12, color: sub }}>{rec ? `${rec.marcados}/${rec.total} marcados` : ""}</span>
          <div style={{ display: "flex", gap: 8 }}>
            <button onClick={onClose} style={{ padding: "8px 18px", border: `1px solid ${bd}`, borderRadius: 8, background: "transparent", color: sub, cursor: "pointer", fontSize: 13 }}>Cerrar ventana</button>
            <button onClick={cerrar} disabled={closing || !rec || rec.marcados < rec.total} style={{ padding: "8px 20px", background: rec && rec.marcados >= rec.total ? "#10B981" : "#94A3B8", border: "none", borderRadius: 8, color: "white", cursor: rec && rec.marcados >= rec.total ? "pointer" : "not-allowed", fontWeight: 700, fontSize: 13 }}>{closing ? "Cerrando..." : "Cerrar recepción"}</button>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function MonzaBodegaPage() {
  const { dark } = useMonzaTheme();
  const [tab, setTab] = useState<"recibir" | "en_bodega" | "reclamos">("recibir");
  const [embs, setEmbs] = useState<EmbRecv[]>([]);
  const [enBodega, setEnBodega] = useState<BodegaItem[]>([]);
  const [reclamos, setReclamos] = useState<Reclamo[]>([]);
  const [kpis, setKpis] = useState<KPIs | null>(null);
  const [loading, setLoading] = useState(true);
  const [recId, setRecId] = useState<number | null>(null);

  const bg = dark ? "#131b3e" : "white"; const bd = dark ? "#1e2a4a" : "#E2E8F0"; const txt = dark ? "white" : "#1E293B"; const sub = dark ? "#8899cc" : "#64748B";

  const fetchAll = useCallback(async () => {
    setLoading(true);
    try { const [k, e, b, r] = await Promise.all([monzaBodegaAPI.kpis(), monzaBodegaAPI.embarques(), monzaBodegaAPI.enBodega(), monzaBodegaAPI.listReclamos()]);
      setKpis(k.data); setEmbs(e.data); setEnBodega(b.data); setReclamos(r.data); }
    catch { toast.error("Error al cargar bodega"); } finally { setLoading(false); }
  }, []);
  useEffect(() => { fetchAll(); }, [fetchAll]);

  const recibir = async (e: EmbRecv) => {
    try { const r = await monzaBodegaAPI.recibir(e.id); setRecId(r.data.recepcion_id); } catch { toast.error("Error al abrir recepción"); }
  };
  const resolver = async (r: Reclamo, estado: string) => { try { await monzaBodegaAPI.updateReclamo(r.id, { estado }); toast.success(`Reclamo → ${estado}`); fetchAll(); } catch { toast.error("Error"); } };

  const tabs = [["recibir", "Por recibir", embs.length], ["en_bodega", "En bodega", enBodega.length], ["reclamos", "Reclamos", reclamos.filter((r) => ["pendiente", "reclamado"].includes(r.estado)).length]] as const;

  return (
    <div>
      <div style={{ marginBottom: 18 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}><Boxes size={22} className="monza-ic" /><h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: txt }}>Bodega</h1></div>
        <p style={{ margin: 0, fontSize: 13, color: sub }}>Recepción física de embarques. Marca cada ítem (completo/dañado/faltante) y cierra la recepción.</p>
      </div>

      {kpis && (
        <div style={{ display: "flex", gap: 10, marginBottom: 18, flexWrap: "wrap" }}>
          <KpiCard label="Embarques a recibir" value={kpis.a_recibir} accent="#3B82F6" />
          <KpiCard label="En bodega (listos)" value={kpis.en_bodega} accent="#10B981" />
          <KpiCard label="Despachados" value={kpis.despachado} accent="#6366F1" />
          <KpiCard label="Reclamos pendientes" value={kpis.reclamos_pendientes} accent="#EF4444" />
        </div>
      )}

      <div style={{ display: "flex", gap: 4, marginBottom: 14, borderBottom: `1px solid ${bd}` }}>
        {tabs.map(([k, l, c]) => <button key={k} onClick={() => setTab(k)} style={{ padding: "9px 16px", border: "none", background: "transparent", cursor: "pointer", fontSize: 13, fontWeight: 600, color: tab === k ? "var(--monza-accent)" : sub, borderBottom: `2px solid ${tab === k ? "var(--monza-accent)" : "transparent"}`, marginBottom: -1 }}>{l} <span style={{ fontSize: 11, background: dark ? "#1e2a4a" : "#F1F5F9", padding: "1px 7px", borderRadius: 10, marginLeft: 4 }}>{c}</span></button>)}
      </div>

      <div style={{ background: bg, border: `1px solid ${bd}`, borderRadius: 10, overflow: "hidden" }}>
        {loading ? <div style={{ padding: 40, textAlign: "center", color: "#94A3B8" }}>Cargando...</div>
        : tab === "recibir" ? (
          embs.length === 0 ? <div style={{ padding: 48, textAlign: "center", color: "#94A3B8" }}><Ship size={32} color="#E2E8F0" style={{ display: "block", margin: "0 auto 8px" }} />No hay embarques por recibir.</div>
          : <div style={{ padding: 14, display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))", gap: 12 }}>
              {embs.map((e) => (
                <div key={e.id} style={{ border: `1px solid ${bd}`, borderRadius: 12, padding: 14, borderLeft: "3px solid #3B82F6" }}>
                  <div style={{ fontWeight: 800, fontSize: 15, color: "var(--monza-accent)" }}>{e.numero}</div>
                  <div style={{ fontSize: 12, color: sub, margin: "4px 0 10px" }}>{e.items_count} ítem(s) · AWB {e.awb || "—"}{e.forwarder ? ` · ${e.forwarder}` : ""}</div>
                  <button onClick={() => recibir(e)} style={{ width: "100%", padding: "8px", background: "var(--monza-accent)", border: "none", borderRadius: 8, color: "white", cursor: "pointer", fontWeight: 700, fontSize: 13, display: "flex", alignItems: "center", justifyContent: "center", gap: 6 }}>
                    <Package size={14} /> {e.recepcion_abierta ? "Continuar recepción" : "Recibir embarque"}
                  </button>
                </div>
              ))}
            </div>
        ) : tab === "en_bodega" ? (
          enBodega.length === 0 ? <div style={{ padding: 48, textAlign: "center", color: "#94A3B8" }}><Package size={32} color="#E2E8F0" style={{ display: "block", margin: "0 auto 8px" }} />No hay ítems en bodega.</div>
          : <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead><tr style={{ background: dark ? "#0d1321" : "#F8FAFC", borderBottom: `1px solid ${bd}` }}>{["N° COT", "Cliente", "Repuesto", "Cant.", "Proveedor", "Estado"].map((h) => <th key={h} style={{ padding: "10px 12px", textAlign: h === "Cant." ? "right" : "left", fontWeight: 600, fontSize: 11, color: sub, textTransform: "uppercase" as const }}>{h}</th>)}</tr></thead>
              <tbody>{enBodega.map((it) => (
                <tr key={it.id} style={{ borderBottom: `1px solid ${dark ? "#1e2a4a" : "#F1F5F9"}` }}>
                  <td style={{ padding: "9px 12px", fontWeight: 600, color: "var(--monza-accent)", fontSize: 12 }}>{it.cot_numero}</td>
                  <td style={{ padding: "9px 12px", color: txt }}>{it.cliente || "—"}</td>
                  <td style={{ padding: "9px 12px" }}><div style={{ color: txt, fontWeight: 500 }}>{it.descripcion}</div>{it.numero_parte && <div style={{ fontSize: 10, color: sub }}>{it.numero_parte}</div>}</td>
                  <td style={{ padding: "9px 12px", textAlign: "right", color: txt }}>{it.cantidad}</td>
                  <td style={{ padding: "9px 12px", color: sub, fontSize: 12 }}>{it.ocp_proveedor || "—"}</td>
                  <td style={{ padding: "9px 12px" }}><span style={{ fontSize: 11, background: "#DCFCE7", color: "#15803D", padding: "3px 10px", borderRadius: 10, fontWeight: 600, display: "inline-flex", alignItems: "center", gap: 4 }}><CheckCircle size={11} /> En bodega</span></td>
                </tr>
              ))}</tbody>
            </table>
        ) : (
          reclamos.length === 0 ? <div style={{ padding: 48, textAlign: "center", color: "#94A3B8" }}><AlertTriangle size={32} color="#E2E8F0" style={{ display: "block", margin: "0 auto 8px" }} />No hay reclamos.</div>
          : <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead><tr style={{ background: dark ? "#0d1321" : "#F8FAFC", borderBottom: `1px solid ${bd}` }}>{["N° COT", "Repuesto", "Motivo", "Qty", "Proveedor", "Estado", "Acciones"].map((h) => <th key={h} style={{ padding: "10px 12px", textAlign: "left", fontWeight: 600, fontSize: 11, color: sub, textTransform: "uppercase" as const }}>{h}</th>)}</tr></thead>
              <tbody>{reclamos.map((r) => { const es = RECLAMO_EST[r.estado] || RECLAMO_EST.pendiente; return (
                <tr key={r.id} style={{ borderBottom: `1px solid ${dark ? "#1e2a4a" : "#F1F5F9"}` }}>
                  <td style={{ padding: "9px 12px", fontWeight: 600, color: "var(--monza-accent)", fontSize: 12 }}>{r.cot_numero || "—"}</td>
                  <td style={{ padding: "9px 12px", color: txt }}>{r.descripcion}{r.observacion && <div style={{ fontSize: 10, color: sub }}>{r.observacion}</div>}</td>
                  <td style={{ padding: "9px 12px", color: "#B91C1C", fontSize: 11, fontWeight: 600 }}>{MOTIVO_LBL[r.motivo] || r.motivo}</td>
                  <td style={{ padding: "9px 12px", color: sub }}>{r.qty_afectada}</td>
                  <td style={{ padding: "9px 12px", color: sub, fontSize: 12 }}>{r.ocp_proveedor || "—"}</td>
                  <td style={{ padding: "9px 12px" }}><span style={{ fontSize: 11, background: es.bg, color: es.color, padding: "3px 10px", borderRadius: 10, fontWeight: 600 }}>{es.label}</span></td>
                  <td style={{ padding: "9px 12px" }}>{["pendiente", "reclamado"].includes(r.estado) && <div style={{ display: "flex", gap: 5 }}>
                    {r.estado === "pendiente" && <button onClick={() => resolver(r, "reclamado")} style={{ padding: "4px 9px", border: "1px solid #1D4ED8", borderRadius: 6, background: "transparent", color: "#1D4ED8", cursor: "pointer", fontSize: 11, fontWeight: 600 }}>Reclamar</button>}
                    <button onClick={() => resolver(r, "resuelto")} style={{ padding: "4px 9px", border: "1px solid #15803D", borderRadius: 6, background: "transparent", color: "#15803D", cursor: "pointer", fontSize: 11, fontWeight: 600 }}>Resolver</button>
                  </div>}</td>
                </tr>); })}</tbody>
            </table>
        )}
      </div>

      {recId && <RecepcionPanel recId={recId} onClose={() => { setRecId(null); fetchAll(); }} onClosed={() => { setRecId(null); fetchAll(); }} />}
    </div>
  );
}
