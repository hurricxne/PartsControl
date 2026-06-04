import { useState, useEffect, useCallback } from "react";
import { Ship, Plane, Search, RefreshCw, Package, MapPin, Edit2, Check } from "lucide-react";
import { monzaAbastecimientoAPI } from "../services/monzaApi";
import { useMonzaTheme } from "./MonzaLayout";
import toast from "react-hot-toast";

interface OcLog {
  id: number;
  numero: string;
  proveedor_nombre?: string;
  pais?: string;
  moneda?: string;
  estado: string;
  plazo_dias?: number;
  awb?: string;
  notas?: string;
  items_count: number;
  created_at?: string;
}

const ESTADO: Record<string, { bg: string; color: string; label: string }> = {
  emitida:     { bg: "#FEF3C7", color: "#B45309", label: "Emitida" },
  en_transito: { bg: "#EDE9FE", color: "#6D28D9", label: "En tránsito" },
  llegada:     { bg: "#DBEAFE", color: "#1D4ED8", label: "Llegó a Chile" },
  recibida:    { bg: "#DCFCE7", color: "#15803D", label: "Recibida" },
  cancelada:   { bg: "#FEE2E2", color: "#B91C1C", label: "Cancelada" },
};

function fmtDate(d?: string) { return d ? new Date(d).toLocaleDateString("es-CL") : "—"; }

function KpiCard({ label, value, accent }: { label: string; value: number; accent: string }) {
  const { dark } = useMonzaTheme();
  return (
    <div style={{ background: dark ? "#131b3e" : "white", borderRadius: 12, border: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}`, padding: "14px 18px", flex: 1, minWidth: 120 }}>
      <div style={{ width: 24, height: 3, borderRadius: 2, background: accent, marginBottom: 10 }} />
      <div style={{ fontSize: 24, fontWeight: 800, color: dark ? "white" : "#1E293B", lineHeight: 1 }}>{value}</div>
      <div style={{ fontSize: 11, color: dark ? "#8899cc" : "#64748B", marginTop: 4 }}>{label}</div>
    </div>
  );
}

export default function MonzaLogisticaPage() {
  const { dark } = useMonzaTheme();
  const [ocs, setOcs] = useState<OcLog[]>([]);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState("");
  const [editAwb, setEditAwb] = useState<{ id: number; val: string } | null>(null);

  const bg = dark ? "#131b3e" : "white";
  const bd = dark ? "#1e2a4a" : "#E2E8F0";
  const txt = dark ? "white" : "#1E293B";
  const sub = dark ? "#8899cc" : "#64748B";

  const fetchAll = useCallback(async () => {
    setLoading(true);
    try {
      const res = await monzaAbastecimientoAPI.listOcs();
      // Logística gestiona las que están en proceso de importación
      const all: OcLog[] = res.data;
      setOcs(all.filter((o) => ["emitida", "en_transito", "llegada"].includes(o.estado)));
    } catch { toast.error("Error al cargar logística"); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { fetchAll(); }, [fetchAll]);

  const advance = async (oc: OcLog, estado: string) => {
    try {
      await monzaAbastecimientoAPI.updateOc(oc.id, { estado });
      toast.success(`${oc.numero} → ${ESTADO[estado]?.label || estado}`);
      fetchAll();
    } catch { toast.error("Error al actualizar"); }
  };

  const saveAwb = async (id: number, val: string) => {
    try {
      await monzaAbastecimientoAPI.updateOc(id, { awb: val });
      toast.success("AWB actualizado");
      setEditAwb(null);
      fetchAll();
    } catch { toast.error("Error"); }
  };

  const filtered = ocs.filter((o) =>
    !q || o.numero.toLowerCase().includes(q.toLowerCase()) ||
    (o.proveedor_nombre || "").toLowerCase().includes(q.toLowerCase()) ||
    (o.awb || "").toLowerCase().includes(q.toLowerCase())
  );

  const counts = {
    emitida: ocs.filter((o) => o.estado === "emitida").length,
    en_transito: ocs.filter((o) => o.estado === "en_transito").length,
    llegada: ocs.filter((o) => o.estado === "llegada").length,
  };

  return (
    <div>
      {/* Header */}
      <div style={{ marginBottom: 18 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
          <Ship size={22} className="monza-ic" />
          <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: txt }}>Logística</h1>
        </div>
        <p style={{ margin: 0, fontSize: 13, color: sub }}>
          Importación: gestiona el embarque (aéreo), AWB y llegada de las OCs a Chile. Al marcar "Llegó", los ítems pasan a Bodega para recepción.
        </p>
      </div>

      {/* KPIs */}
      <div style={{ display: "flex", gap: 10, marginBottom: 18, flexWrap: "wrap" }}>
        <KpiCard label="OC emitidas" value={counts.emitida} accent="#F59E0B" />
        <KpiCard label="En tránsito" value={counts.en_transito} accent="#6366F1" />
        <KpiCard label="Llegadas (a recibir)" value={counts.llegada} accent="#3B82F6" />
      </div>

      {/* Toolbar */}
      <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 12 }}>
        <div style={{ position: "relative", flex: 1, minWidth: 240 }}>
          <Search size={14} style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)", color: "#94A3B8" }} />
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Buscar OC, proveedor, AWB..."
            style={{ width: "100%", padding: "8px 10px 8px 32px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 13, boxSizing: "border-box" as const, background: bg, color: txt }} />
        </div>
        <button onClick={fetchAll} style={{ padding: "8px 10px", border: `1px solid ${bd}`, borderRadius: 6, background: bg, cursor: "pointer", color: sub }}>
          <RefreshCw size={14} />
        </button>
      </div>

      {/* Cards */}
      {loading ? (
        <div style={{ padding: 40, textAlign: "center", color: "#94A3B8", background: bg, border: `1px solid ${bd}`, borderRadius: 10 }}>Cargando...</div>
      ) : filtered.length === 0 ? (
        <div style={{ padding: 48, textAlign: "center", color: "#94A3B8", background: bg, border: `1px solid ${bd}`, borderRadius: 10 }}>
          <Ship size={32} color="#E2E8F0" style={{ display: "block", margin: "0 auto 8px" }} />
          No hay OCs en proceso de importación.
        </div>
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(340px, 1fr))", gap: 12 }}>
          {filtered.map((oc) => {
            const es = ESTADO[oc.estado] || ESTADO.emitida;
            return (
              <div key={oc.id} style={{ background: bg, border: `1px solid ${bd}`, borderRadius: 12, padding: 16, borderLeft: `3px solid ${es.color}` }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 10 }}>
                  <div>
                    <div style={{ fontWeight: 800, fontSize: 15, color: "var(--monza-accent)" }}>{oc.numero}</div>
                    <div style={{ fontSize: 12, color: txt, fontWeight: 500, marginTop: 2 }}>{oc.proveedor_nombre || "Sin proveedor"}</div>
                  </div>
                  <span style={{ fontSize: 11, background: es.bg, color: es.color, padding: "3px 10px", borderRadius: 10, fontWeight: 600 }}>{es.label}</span>
                </div>

                <div style={{ display: "flex", flexWrap: "wrap", gap: 12, fontSize: 12, color: sub, marginBottom: 12 }}>
                  <span style={{ display: "flex", alignItems: "center", gap: 4 }}><MapPin size={12} /> {oc.pais || "—"}</span>
                  <span style={{ display: "flex", alignItems: "center", gap: 4 }}><Package size={12} /> {oc.items_count} ítem(s)</span>
                  {oc.plazo_dias && <span>{oc.plazo_dias} días plazo</span>}
                </div>

                {/* AWB editable */}
                <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 12, fontSize: 12 }}>
                  <Plane size={13} color={sub} />
                  {editAwb?.id === oc.id ? (
                    <>
                      <input autoFocus value={editAwb.val} onChange={(e) => setEditAwb({ id: oc.id, val: e.target.value })}
                        onKeyDown={(e) => { if (e.key === "Enter") saveAwb(oc.id, editAwb.val); if (e.key === "Escape") setEditAwb(null); }}
                        placeholder="N° AWB / tracking"
                        style={{ flex: 1, padding: "4px 8px", border: "1px solid var(--monza-accent)", borderRadius: 4, fontSize: 12, background: dark ? "#0d1321" : "white", color: txt }} />
                      <button onClick={() => saveAwb(oc.id, editAwb.val)} style={{ background: "var(--monza-accent)", border: "none", borderRadius: 4, color: "white", cursor: "pointer", padding: "4px 6px", display: "flex" }}><Check size={12} /></button>
                    </>
                  ) : (
                    <span onClick={() => setEditAwb({ id: oc.id, val: oc.awb || "" })} style={{ cursor: "pointer", color: oc.awb ? txt : sub, display: "flex", alignItems: "center", gap: 4 }}>
                      {oc.awb || "Agregar AWB"} <Edit2 size={11} color={sub} />
                    </span>
                  )}
                </div>

                {/* Acciones de avance */}
                <div style={{ display: "flex", gap: 6 }}>
                  {oc.estado === "emitida" && (
                    <button onClick={() => advance(oc, "en_transito")}
                      style={{ flex: 1, padding: "7px", border: "1px solid #6366F1", borderRadius: 7, background: "transparent", color: "#6366F1", cursor: "pointer", fontSize: 12, fontWeight: 600, display: "flex", alignItems: "center", justifyContent: "center", gap: 5 }}>
                      <Plane size={12} /> Marcar embarque
                    </button>
                  )}
                  {oc.estado === "en_transito" && (
                    <button onClick={() => advance(oc, "llegada")}
                      style={{ flex: 1, padding: "7px", border: "1px solid #3B82F6", borderRadius: 7, background: "transparent", color: "#3B82F6", cursor: "pointer", fontSize: 12, fontWeight: 600, display: "flex", alignItems: "center", justifyContent: "center", gap: 5 }}>
                      <MapPin size={12} /> Llegó a Chile
                    </button>
                  )}
                  {oc.estado === "llegada" && (
                    <div style={{ flex: 1, padding: "7px", borderRadius: 7, background: dark ? "#0d1321" : "#F0FDF4", color: "#15803D", fontSize: 12, fontWeight: 600, display: "flex", alignItems: "center", justifyContent: "center", gap: 5 }}>
                      <Package size={12} /> En recepción de Bodega
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
