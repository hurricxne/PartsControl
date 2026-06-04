import { useState, useEffect, useCallback } from "react";
import { Package, Search, RefreshCw, CheckCircle, AlertTriangle, X, Boxes } from "lucide-react";
import { monzaBodegaAPI } from "../services/monzaApi";
import { useMonzaTheme } from "./MonzaLayout";
import toast from "react-hot-toast";

interface BodegaItem {
  id: number;
  cot_numero: string;
  cliente?: string;
  vehiculo?: string;
  descripcion: string;
  numero_parte?: string;
  marca?: string;
  cantidad: number;
  estado_linea: string;
  ocp_numero?: string;
  ocp_proveedor?: string;
  ocp_awb?: string;
}

interface Reclamo {
  id: number;
  cot_numero?: string;
  descripcion?: string;
  motivo: string;
  qty_afectada: number;
  estado: string;
  observacion?: string;
  ocp_numero?: string;
  ocp_proveedor?: string;
  fecha_creacion?: string;
}

interface KPIs { por_recibir: number; en_bodega: number; despachado: number; reclamos_pendientes: number; }

const MOTIVO: Record<string, { label: string; color: string }> = {
  danado:   { label: "Dañado", color: "#B91C1C" },
  faltante: { label: "Faltante", color: "#B45309" },
  no_llego: { label: "No llegó", color: "#6D28D9" },
};
const RECLAMO_ESTADO: Record<string, { bg: string; color: string; label: string }> = {
  pendiente: { bg: "#FEF3C7", color: "#B45309", label: "Pendiente" },
  reclamado: { bg: "#DBEAFE", color: "#1D4ED8", label: "Reclamado" },
  resuelto:  { bg: "#DCFCE7", color: "#15803D", label: "Resuelto" },
  anulado:   { bg: "#F1F5F9", color: "#64748B", label: "Anulado" },
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

// ── Modal confirmar recepción ─────────────────────────────────────────────────
function RecibirModal({ item, onClose, onDone }: { item: BodegaItem; onClose: () => void; onDone: () => void }) {
  const { dark } = useMonzaTheme();
  const [resultado, setResultado] = useState("ok");
  const [qty, setQty] = useState(String(item.cantidad));
  const [obs, setObs] = useState("");
  const [saving, setSaving] = useState(false);

  const bg = dark ? "#131b3e" : "white";
  const bd = dark ? "#1e2a4a" : "#E2E8F0";
  const txt = dark ? "white" : "#1E293B";
  const sub = dark ? "#8899cc" : "#64748B";

  const submit = async () => {
    setSaving(true);
    try {
      await monzaBodegaAPI.confirmar({
        item_id: item.id,
        resultado,
        qty_afectada: resultado !== "ok" ? Number(qty) : 0,
        observacion: obs || undefined,
      });
      toast.success(resultado === "ok" ? "Ítem recibido en bodega" : "Reclamo generado");
      onDone();
    } catch { toast.error("Error al confirmar"); }
    finally { setSaving(false); }
  };

  const opts = [
    { v: "ok", label: "Recibido OK", color: "#15803D" },
    { v: "danado", label: "Dañado", color: "#B91C1C" },
    { v: "faltante", label: "Faltante", color: "#B45309" },
    { v: "no_llego", label: "No llegó", color: "#6D28D9" },
  ];

  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)", zIndex: 1000, display: "flex", alignItems: "center", justifyContent: "center", padding: 16 }}>
      <div style={{ background: bg, border: `1px solid ${bd}`, borderRadius: 14, width: "100%", maxWidth: 440, boxShadow: "0 24px 60px rgba(0,0,0,0.4)" }}>
        <div style={{ background: dark ? "#0a0e1f" : "#F8FAFC", borderBottom: `1px solid ${bd}`, padding: "14px 20px", display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <Package size={18} className="monza-ic" />
            <span style={{ fontWeight: 700, fontSize: 15, color: txt }}>Confirmar recepción</span>
          </div>
          <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", color: sub, display: "flex" }}><X size={18} /></button>
        </div>

        <div style={{ padding: "16px 20px" }}>
          <div style={{ background: dark ? "#0d1321" : "#F1F5F9", borderRadius: 8, padding: "10px 14px", marginBottom: 14, fontSize: 12 }}>
            <div style={{ fontWeight: 700, color: txt }}>{item.descripcion}</div>
            <div style={{ color: sub }}>{item.cot_numero} · {item.cliente} · {item.cantidad} und · {item.ocp_proveedor || "—"}</div>
          </div>

          <label style={{ fontSize: 12, fontWeight: 600, color: sub, display: "block", marginBottom: 6 }}>Resultado de la recepción</label>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, marginBottom: 14 }}>
            {opts.map((o) => (
              <button key={o.v} onClick={() => setResultado(o.v)}
                style={{ padding: "10px", borderRadius: 8, border: `2px solid ${resultado === o.v ? o.color : bd}`, background: resultado === o.v ? `${o.color}15` : "transparent", color: resultado === o.v ? o.color : sub, cursor: "pointer", fontSize: 13, fontWeight: 600 }}>
                {o.label}
              </button>
            ))}
          </div>

          {resultado !== "ok" && (
            <>
              <label style={{ fontSize: 12, fontWeight: 600, color: sub, display: "block", marginBottom: 4 }}>Cantidad afectada</label>
              <input type="number" value={qty} onChange={(e) => setQty(e.target.value)}
                style={{ width: "100%", padding: "8px 10px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 13, boxSizing: "border-box" as const, background: dark ? "#0d1321" : "#F8FAFC", color: txt, marginBottom: 12 }} />
              <label style={{ fontSize: 12, fontWeight: 600, color: sub, display: "block", marginBottom: 4 }}>Observación</label>
              <textarea value={obs} onChange={(e) => setObs(e.target.value)} rows={2} placeholder="Detalle del problema..."
                style={{ width: "100%", padding: "8px 10px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 13, boxSizing: "border-box" as const, background: dark ? "#0d1321" : "#F8FAFC", color: txt, resize: "vertical" as const }} />
            </>
          )}
        </div>

        <div style={{ padding: "12px 20px", borderTop: `1px solid ${bd}`, display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <button onClick={onClose} style={{ padding: "8px 18px", border: `1px solid ${bd}`, borderRadius: 8, background: "transparent", color: sub, cursor: "pointer", fontSize: 13 }}>Cancelar</button>
          <button onClick={submit} disabled={saving} style={{ padding: "8px 20px", background: resultado === "ok" ? "#10B981" : "#B91C1C", border: "none", borderRadius: 8, color: "white", cursor: saving ? "wait" : "pointer", fontWeight: 700, fontSize: 13 }}>
            {saving ? "Guardando..." : resultado === "ok" ? "Confirmar OK" : "Generar reclamo"}
          </button>
        </div>
      </div>
    </div>
  );
}

export default function MonzaBodegaPage() {
  const { dark } = useMonzaTheme();
  const [tab, setTab] = useState<"por_recibir" | "en_bodega" | "reclamos">("por_recibir");
  const [porRecibir, setPorRecibir] = useState<BodegaItem[]>([]);
  const [enBodega, setEnBodega] = useState<BodegaItem[]>([]);
  const [reclamos, setReclamos] = useState<Reclamo[]>([]);
  const [kpis, setKpis] = useState<KPIs | null>(null);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState("");
  const [recibirItem, setRecibirItem] = useState<BodegaItem | null>(null);

  const bg = dark ? "#131b3e" : "white";
  const bd = dark ? "#1e2a4a" : "#E2E8F0";
  const txt = dark ? "white" : "#1E293B";
  const sub = dark ? "#8899cc" : "#64748B";

  const fetchAll = useCallback(async () => {
    setLoading(true);
    try {
      const [k, pr, eb, rec] = await Promise.all([
        monzaBodegaAPI.kpis(),
        monzaBodegaAPI.porRecibir({ q: q || undefined }),
        monzaBodegaAPI.enBodega({ q: q || undefined }),
        monzaBodegaAPI.listReclamos(),
      ]);
      setKpis(k.data);
      setPorRecibir(pr.data);
      setEnBodega(eb.data);
      setReclamos(rec.data);
    } catch { toast.error("Error al cargar bodega"); }
    finally { setLoading(false); }
  }, [q]);

  useEffect(() => { fetchAll(); }, [fetchAll]);

  const resolverReclamo = async (r: Reclamo, estado: string) => {
    try {
      await monzaBodegaAPI.updateReclamo(r.id, { estado });
      toast.success(`Reclamo → ${RECLAMO_ESTADO[estado]?.label || estado}`);
      fetchAll();
    } catch { toast.error("Error"); }
  };

  const tabs = [
    ["por_recibir", "Por recibir", porRecibir.length],
    ["en_bodega", "En bodega", enBodega.length],
    ["reclamos", "Reclamos", reclamos.filter((r) => ["pendiente", "reclamado"].includes(r.estado)).length],
  ] as const;

  return (
    <div>
      {/* Header */}
      <div style={{ marginBottom: 18 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
          <Boxes size={22} className="monza-ic" />
          <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: txt }}>Bodega</h1>
        </div>
        <p style={{ margin: 0, fontSize: 13, color: sub }}>
          Recepción física de ítems que llegan a Chile. Confirma cada ítem o genera un reclamo si llega dañado o faltante.
        </p>
      </div>

      {/* KPIs */}
      {kpis && (
        <div style={{ display: "flex", gap: 10, marginBottom: 18, flexWrap: "wrap" }}>
          <KpiCard label="Por recibir" value={kpis.por_recibir} accent="#3B82F6" />
          <KpiCard label="En bodega (listos)" value={kpis.en_bodega} accent="#10B981" />
          <KpiCard label="Despachados" value={kpis.despachado} accent="#6366F1" />
          <KpiCard label="Reclamos pendientes" value={kpis.reclamos_pendientes} accent="#EF4444" />
        </div>
      )}

      {/* Tabs */}
      <div style={{ display: "flex", gap: 4, marginBottom: 14, borderBottom: `1px solid ${bd}` }}>
        {tabs.map(([key, label, count]) => (
          <button key={key} onClick={() => setTab(key)}
            style={{ padding: "9px 16px", border: "none", background: "transparent", cursor: "pointer", fontSize: 13, fontWeight: 600,
              color: tab === key ? "var(--monza-accent)" : sub, borderBottom: `2px solid ${tab === key ? "var(--monza-accent)" : "transparent"}`, marginBottom: -1 }}>
            {label} <span style={{ fontSize: 11, background: dark ? "#1e2a4a" : "#F1F5F9", padding: "1px 7px", borderRadius: 10, marginLeft: 4 }}>{count}</span>
          </button>
        ))}
      </div>

      {/* Toolbar */}
      {tab !== "reclamos" && (
        <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 12 }}>
          <div style={{ position: "relative", flex: 1, minWidth: 240 }}>
            <Search size={14} style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)", color: "#94A3B8" }} />
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Buscar ítem, N° COT..."
              style={{ width: "100%", padding: "8px 10px 8px 32px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 13, boxSizing: "border-box" as const, background: bg, color: txt }} />
          </div>
          <button onClick={fetchAll} style={{ padding: "8px 10px", border: `1px solid ${bd}`, borderRadius: 6, background: bg, cursor: "pointer", color: sub }}>
            <RefreshCw size={14} />
          </button>
        </div>
      )}

      {/* Content */}
      <div style={{ background: bg, border: `1px solid ${bd}`, borderRadius: 10, overflow: "hidden" }}>
        {loading ? (
          <div style={{ padding: 40, textAlign: "center", color: "#94A3B8" }}>Cargando...</div>
        ) : tab === "reclamos" ? (
          reclamos.length === 0 ? (
            <div style={{ padding: 48, textAlign: "center", color: "#94A3B8" }}>
              <AlertTriangle size={32} color="#E2E8F0" style={{ display: "block", margin: "0 auto 8px" }} />
              No hay reclamos registrados.
            </div>
          ) : (
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead>
                <tr style={{ background: dark ? "#0d1321" : "#F8FAFC", borderBottom: `1px solid ${bd}` }}>
                  {["N° COT", "Repuesto", "Motivo", "Qty", "Proveedor", "Estado", "Fecha", "Acciones"].map((h) => (
                    <th key={h} style={{ padding: "10px 12px", textAlign: "left", fontWeight: 600, fontSize: 11, color: sub, textTransform: "uppercase" as const, letterSpacing: 0.5 }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {reclamos.map((r) => {
                  const m = MOTIVO[r.motivo] || { label: r.motivo, color: "#64748B" };
                  const es = RECLAMO_ESTADO[r.estado] || RECLAMO_ESTADO.pendiente;
                  return (
                    <tr key={r.id} style={{ borderBottom: `1px solid ${dark ? "#1e2a4a" : "#F1F5F9"}` }}>
                      <td style={{ padding: "9px 12px", fontWeight: 600, color: "var(--monza-accent)", fontSize: 12 }}>{r.cot_numero || "—"}</td>
                      <td style={{ padding: "9px 12px", color: txt }}>{r.descripcion || "—"}{r.observacion && <div style={{ fontSize: 10, color: sub }}>{r.observacion}</div>}</td>
                      <td style={{ padding: "9px 12px" }}><span style={{ fontSize: 11, color: m.color, fontWeight: 600 }}>{m.label}</span></td>
                      <td style={{ padding: "9px 12px", color: sub }}>{r.qty_afectada}</td>
                      <td style={{ padding: "9px 12px", color: sub, fontSize: 12 }}>{r.ocp_proveedor || "—"}</td>
                      <td style={{ padding: "9px 12px" }}><span style={{ fontSize: 11, background: es.bg, color: es.color, padding: "3px 10px", borderRadius: 10, fontWeight: 600 }}>{es.label}</span></td>
                      <td style={{ padding: "9px 12px", color: sub, fontSize: 12 }}>{fmtDate(r.fecha_creacion)}</td>
                      <td style={{ padding: "9px 12px" }}>
                        {["pendiente", "reclamado"].includes(r.estado) && (
                          <div style={{ display: "flex", gap: 5 }}>
                            {r.estado === "pendiente" && (
                              <button onClick={() => resolverReclamo(r, "reclamado")} style={{ padding: "4px 10px", border: "1px solid #1D4ED8", borderRadius: 6, background: "transparent", color: "#1D4ED8", cursor: "pointer", fontSize: 11, fontWeight: 600 }}>Reclamar</button>
                            )}
                            <button onClick={() => resolverReclamo(r, "resuelto")} style={{ padding: "4px 10px", border: "1px solid #15803D", borderRadius: 6, background: "transparent", color: "#15803D", cursor: "pointer", fontSize: 11, fontWeight: 600 }}>Resolver</button>
                            <button onClick={() => resolverReclamo(r, "anulado")} style={{ padding: "4px 10px", border: `1px solid ${bd}`, borderRadius: 6, background: "transparent", color: sub, cursor: "pointer", fontSize: 11 }}>Anular</button>
                          </div>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )
        ) : (() => {
          const list = tab === "por_recibir" ? porRecibir : enBodega;
          if (list.length === 0) {
            return (
              <div style={{ padding: 48, textAlign: "center", color: "#94A3B8" }}>
                <Package size={32} color="#E2E8F0" style={{ display: "block", margin: "0 auto 8px" }} />
                {tab === "por_recibir" ? "No hay ítems pendientes de recibir." : "No hay ítems en bodega."}
              </div>
            );
          }
          return (
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead>
                <tr style={{ background: dark ? "#0d1321" : "#F8FAFC", borderBottom: `1px solid ${bd}` }}>
                  {["N° COT", "Cliente", "Repuesto", "Cant.", "OC Prov.", "Proveedor", "AWB", tab === "por_recibir" ? "Acción" : "Estado"].map((h) => (
                    <th key={h} style={{ padding: "10px 12px", textAlign: h === "Cant." ? "right" : "left", fontWeight: 600, fontSize: 11, color: sub, textTransform: "uppercase" as const, letterSpacing: 0.5 }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {list.map((it) => (
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
                      {tab === "por_recibir" ? (
                        <button onClick={() => setRecibirItem(it)}
                          style={{ padding: "5px 14px", background: "var(--monza-accent)", border: "none", borderRadius: 6, color: "white", cursor: "pointer", fontSize: 12, fontWeight: 600, display: "flex", alignItems: "center", gap: 5 }}>
                          <CheckCircle size={12} /> Recibir
                        </button>
                      ) : (
                        <span style={{ fontSize: 11, background: "#DCFCE7", color: "#15803D", padding: "3px 10px", borderRadius: 10, fontWeight: 600, display: "inline-flex", alignItems: "center", gap: 4 }}>
                          <CheckCircle size={11} /> En bodega
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          );
        })()}
      </div>

      {recibirItem && (
        <RecibirModal
          item={recibirItem}
          onClose={() => setRecibirItem(null)}
          onDone={() => { setRecibirItem(null); fetchAll(); }}
        />
      )}
    </div>
  );
}
