import { useState, useEffect, useCallback } from "react";
import { PackageSearch, Search, RefreshCw } from "lucide-react";
import { monzaAbastecimientoAPI } from "../services/monzaApi";
import { useMonzaTheme } from "./MonzaLayout";
import toast from "react-hot-toast";

interface SegItem {
  id: number;
  cot_numero: string;
  cliente?: string;
  vehiculo?: string;
  descripcion: string;
  numero_parte?: string;
  marca?: string;
  cantidad: number;
  plazo_entrega?: string;
  estado_linea: string;
  fecha_venta?: string;
  ocp_numero?: string;
  ocp_proveedor?: string;
  ocp_estado?: string;
  ocp_awb?: string;
  ocp_plazo_dias?: number;
}

const ESTADO_LINEA: Record<string, { bg: string; color: string; label: string }> = {
  comprado:    { bg: "#DBEAFE", color: "#1D4ED8", label: "Comprado" },
  en_transito: { bg: "#EDE9FE", color: "#6D28D9", label: "En tránsito" },
  en_bodega:   { bg: "#DCFCE7", color: "#15803D", label: "En bodega" },
};

function fmtDate(d?: string) { return d ? new Date(d).toLocaleDateString("es-CL") : "—"; }

/** Días transcurridos desde la venta y delta vs plazo prometido */
function plazoInfo(fechaVenta?: string, plazoDias?: number) {
  if (!fechaVenta || !plazoDias) return null;
  const trans = Math.floor((Date.now() - new Date(fechaVenta).getTime()) / 86400000);
  const delta = plazoDias - trans; // días restantes
  let color = "#15803D", bg = "#DCFCE7", label = `${delta}d restantes`;
  if (delta < 0) { color = "#B91C1C"; bg = "#FEE2E2"; label = `Vencido ${Math.abs(delta)}d`; }
  else if (delta <= 3) { color = "#B45309"; bg = "#FEF3C7"; label = `${delta}d restantes`; }
  return { trans, delta, color, bg, label };
}

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

export default function MonzaSeguimientoPage() {
  const { dark } = useMonzaTheme();
  const [items, setItems] = useState<SegItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState("");
  const [estado, setEstado] = useState("");

  const bg = dark ? "#131b3e" : "white";
  const bd = dark ? "#1e2a4a" : "#E2E8F0";
  const txt = dark ? "white" : "#1E293B";
  const sub = dark ? "#8899cc" : "#64748B";

  const fetchAll = useCallback(async () => {
    setLoading(true);
    try {
      const res = await monzaAbastecimientoAPI.seguimiento({ q: q || undefined, estado: estado || undefined });
      setItems(res.data);
    } catch { toast.error("Error al cargar seguimiento"); }
    finally { setLoading(false); }
  }, [q, estado]);

  useEffect(() => { fetchAll(); }, [fetchAll]);

  const counts = {
    comprado: items.filter((i) => i.estado_linea === "comprado").length,
    en_transito: items.filter((i) => i.estado_linea === "en_transito").length,
    en_bodega: items.filter((i) => i.estado_linea === "en_bodega").length,
  };

  return (
    <div>
      {/* Header */}
      <div style={{ marginBottom: 18 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
          <PackageSearch size={22} className="monza-ic" />
          <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: txt }}>Seguimiento</h1>
        </div>
        <p style={{ margin: 0, fontSize: 13, color: sub }}>
          Monitoreo de ítems ya comprados a proveedor. Controla plazos prometidos vs transcurridos.
        </p>
      </div>

      {/* KPIs */}
      <div style={{ display: "flex", gap: 10, marginBottom: 18, flexWrap: "wrap" }}>
        <KpiCard label="Comprado" value={counts.comprado} accent="#3B82F6" />
        <KpiCard label="En tránsito" value={counts.en_transito} accent="#6366F1" />
        <KpiCard label="En bodega" value={counts.en_bodega} accent="#10B981" />
      </div>

      {/* Toolbar */}
      <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 12, flexWrap: "wrap" }}>
        <div style={{ position: "relative", flex: 1, minWidth: 240 }}>
          <Search size={14} style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)", color: "#94A3B8" }} />
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Buscar ítem, N° COT..."
            style={{ width: "100%", padding: "8px 10px 8px 32px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 13, boxSizing: "border-box" as const, background: bg, color: txt }} />
        </div>
        <select value={estado} onChange={(e) => setEstado(e.target.value)}
          style={{ padding: "8px 12px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 13, background: bg, color: txt }}>
          <option value="">Todos los estados</option>
          <option value="comprado">Comprado</option>
          <option value="en_transito">En tránsito</option>
          <option value="en_bodega">En bodega</option>
        </select>
        <button onClick={fetchAll} style={{ padding: "8px 10px", border: `1px solid ${bd}`, borderRadius: 6, background: bg, cursor: "pointer", color: sub }}>
          <RefreshCw size={14} />
        </button>
      </div>

      {/* Table */}
      <div style={{ background: bg, border: `1px solid ${bd}`, borderRadius: 10, overflow: "hidden" }}>
        {loading ? (
          <div style={{ padding: 40, textAlign: "center", color: "#94A3B8" }}>Cargando...</div>
        ) : items.length === 0 ? (
          <div style={{ padding: 48, textAlign: "center", color: "#94A3B8" }}>
            <PackageSearch size={32} color="#E2E8F0" style={{ display: "block", margin: "0 auto 8px" }} />
            No hay ítems en seguimiento.
          </div>
        ) : (
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr style={{ background: dark ? "#0d1321" : "#F8FAFC", borderBottom: `1px solid ${bd}` }}>
                {["N° COT", "Cliente", "Repuesto", "Cant.", "OC Proveedor", "Proveedor", "AWB", "Plazo", "Estado"].map((h) => (
                  <th key={h} style={{ padding: "10px 12px", textAlign: h === "Cant." ? "right" : "left", fontWeight: 600, fontSize: 11, color: sub, textTransform: "uppercase" as const, letterSpacing: 0.5 }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {items.map((it) => {
                const es = ESTADO_LINEA[it.estado_linea] || { bg: "#F1F5F9", color: "#64748B", label: it.estado_linea };
                const pz = plazoInfo(it.fecha_venta, it.ocp_plazo_dias);
                return (
                  <tr key={it.id} style={{ borderBottom: `1px solid ${dark ? "#1e2a4a" : "#F1F5F9"}` }}>
                    <td style={{ padding: "9px 12px", fontWeight: 600, color: "var(--monza-accent)", fontSize: 12 }}>{it.cot_numero}</td>
                    <td style={{ padding: "9px 12px", color: txt }}>{it.cliente || "—"}</td>
                    <td style={{ padding: "9px 12px" }}>
                      <div style={{ color: txt, fontWeight: 500 }}>{it.descripcion}</div>
                      {it.numero_parte && <div style={{ fontSize: 10, color: sub }}>{it.numero_parte}{it.marca ? ` · ${it.marca}` : ""}</div>}
                    </td>
                    <td style={{ padding: "9px 12px", textAlign: "right", color: txt }}>{it.cantidad}</td>
                    <td style={{ padding: "9px 12px", color: sub, fontSize: 12 }}>{it.ocp_numero || "—"}</td>
                    <td style={{ padding: "9px 12px", color: sub, fontSize: 12 }}>{it.ocp_proveedor || "—"}</td>
                    <td style={{ padding: "9px 12px", color: sub, fontSize: 12 }}>{it.ocp_awb || "—"}</td>
                    <td style={{ padding: "9px 12px" }}>
                      {pz ? (
                        <span style={{ fontSize: 11, background: pz.bg, color: pz.color, padding: "2px 8px", borderRadius: 8, fontWeight: 600 }}>{pz.label}</span>
                      ) : (
                        <span style={{ fontSize: 12, color: sub }}>{it.ocp_plazo_dias ? `${it.ocp_plazo_dias}d` : "—"}</span>
                      )}
                    </td>
                    <td style={{ padding: "9px 12px" }}>
                      <span style={{ fontSize: 11, background: es.bg, color: es.color, padding: "3px 10px", borderRadius: 10, fontWeight: 600 }}>{es.label}</span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
