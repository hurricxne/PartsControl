import { useState, useEffect, useCallback } from "react";
import { ShoppingCart, Search, RefreshCw, Package, X, Truck, FileText } from "lucide-react";
import { monzaAbastecimientoAPI } from "../services/monzaApi";
import { useMonzaTheme } from "./MonzaLayout";
import toast from "react-hot-toast";

interface ItemCompra {
  id: number;
  cotizacion_id: number;
  cot_numero: string;
  cliente?: string;
  vehiculo?: string;
  descripcion: string;
  numero_parte?: string;
  marca?: string;
  procedencia?: string;
  calidad?: string;
  cantidad: number;
  costo?: number;
  moneda?: string;
  precio_unitario_clp?: number;
  subtotal_clp?: number;
  plazo_entrega?: string;
  estado_linea: string;
  fecha_venta?: string;
}

interface OcCompra {
  id: number;
  numero: string;
  proveedor_nombre?: string;
  pais?: string;
  moneda?: string;
  estado: string;
  plazo_dias?: number;
  awb?: string;
  notas?: string;
  asesor_email?: string;
  items_count: number;
  created_at?: string;
}

interface Proveedor {
  id: number; nombre: string; pais?: string; moneda?: string;
}

interface KPIs {
  por_comprar: number; comprado: number; en_transito: number;
  en_bodega: number; despachado: number; reclamo: number; ocs_abiertas: number;
}

const OC_ESTADO: Record<string, { bg: string; color: string; label: string }> = {
  emitida:     { bg: "#FEF3C7", color: "#B45309", label: "Emitida" },
  en_transito: { bg: "#DBEAFE", color: "#1D4ED8", label: "En tránsito" },
  recibida:    { bg: "#DCFCE7", color: "#15803D", label: "Recibida" },
  cancelada:   { bg: "#FEE2E2", color: "#B91C1C", label: "Cancelada" },
};

function fmt(n?: number) { return n && n > 0 ? `$${Math.round(n).toLocaleString("es-CL")}` : "—"; }
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

// ── Modal Crear OC de compra ──────────────────────────────────────────────────
function CrearOcModal({ items, proveedores, onClose, onDone }: {
  items: ItemCompra[]; proveedores: Proveedor[]; onClose: () => void; onDone: () => void;
}) {
  const { dark } = useMonzaTheme();
  const [provId, setProvId] = useState<string>("");
  const [provNombre, setProvNombre] = useState("");
  const [pais, setPais] = useState("");
  const [moneda, setMoneda] = useState("EUR");
  const [plazo, setPlazo] = useState("");
  const [awb, setAwb] = useState("");
  const [notas, setNotas] = useState("");
  const [saving, setSaving] = useState(false);

  const bg = dark ? "#131b3e" : "white";
  const bd = dark ? "#1e2a4a" : "#E2E8F0";
  const txt = dark ? "white" : "#1E293B";
  const sub = dark ? "#8899cc" : "#64748B";
  const IS = { width: "100%", padding: "8px 10px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 13, boxSizing: "border-box" as const, background: dark ? "#0d1321" : "#F8FAFC", color: txt };

  const onProvChange = (v: string) => {
    setProvId(v);
    const p = proveedores.find((x) => String(x.id) === v);
    if (p) { setProvNombre(p.nombre); setPais(p.pais || ""); setMoneda(p.moneda || "EUR"); }
  };

  const submit = async () => {
    setSaving(true);
    try {
      await monzaAbastecimientoAPI.comprar({
        item_ids: items.map((i) => i.id),
        proveedor_id: provId ? Number(provId) : undefined,
        proveedor_nombre: provNombre || undefined,
        pais: pais || undefined,
        moneda,
        plazo_dias: plazo ? Number(plazo) : undefined,
        awb: awb || undefined,
        notas: notas || undefined,
      });
      toast.success(`OC de compra creada · ${items.length} ítem(s)`);
      onDone();
    } catch { toast.error("Error al crear OC"); }
    finally { setSaving(false); }
  };

  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)", zIndex: 1000, display: "flex", alignItems: "center", justifyContent: "center", padding: 16 }}>
      <div style={{ background: bg, border: `1px solid ${bd}`, borderRadius: 14, width: "100%", maxWidth: 560, maxHeight: "90vh", display: "flex", flexDirection: "column", boxShadow: "0 24px 60px rgba(0,0,0,0.4)" }}>
        <div style={{ background: dark ? "#0a0e1f" : "#F8FAFC", borderBottom: `1px solid ${bd}`, padding: "14px 20px", display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <ShoppingCart size={18} className="monza-ic" />
            <span style={{ fontWeight: 700, fontSize: 15, color: txt }}>Crear OC de compra</span>
          </div>
          <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", color: sub, display: "flex" }}><X size={18} /></button>
        </div>

        <div style={{ padding: "16px 20px", overflowY: "auto" }}>
          {/* Ítems seleccionados */}
          <div style={{ background: dark ? "#0d1321" : "#F1F5F9", borderRadius: 8, padding: "10px 14px", marginBottom: 16 }}>
            <div style={{ fontSize: 11, fontWeight: 700, color: sub, marginBottom: 6, textTransform: "uppercase" }}>{items.length} ítem(s) a comprar</div>
            {items.map((it) => (
              <div key={it.id} style={{ fontSize: 12, color: txt, padding: "2px 0", display: "flex", justifyContent: "space-between" }}>
                <span>{it.descripcion} <span style={{ color: sub }}>· {it.cot_numero}</span></span>
                <span style={{ color: sub }}>×{it.cantidad}{it.costo ? ` · ${it.moneda} ${it.costo}` : ""}</span>
              </div>
            ))}
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            <div style={{ gridColumn: "1 / -1" }}>
              <label style={{ fontSize: 12, fontWeight: 600, color: sub, display: "block", marginBottom: 4 }}>Proveedor</label>
              <select value={provId} onChange={(e) => onProvChange(e.target.value)} style={{ ...IS, marginBottom: 8 }}>
                <option value="">— Escribir manualmente —</option>
                {proveedores.map((p) => <option key={p.id} value={p.id}>{p.nombre}{p.pais ? ` (${p.pais})` : ""}</option>)}
              </select>
              <input value={provNombre} onChange={(e) => setProvNombre(e.target.value)} placeholder="Nombre del proveedor" style={IS} />
            </div>
            <div>
              <label style={{ fontSize: 12, fontWeight: 600, color: sub, display: "block", marginBottom: 4 }}>País</label>
              <input value={pais} onChange={(e) => setPais(e.target.value)} placeholder="Alemania, USA..." style={IS} />
            </div>
            <div>
              <label style={{ fontSize: 12, fontWeight: 600, color: sub, display: "block", marginBottom: 4 }}>Moneda</label>
              <select value={moneda} onChange={(e) => setMoneda(e.target.value)} style={IS}>
                <option>EUR</option><option>USD</option><option>CLP</option>
              </select>
            </div>
            <div>
              <label style={{ fontSize: 12, fontWeight: 600, color: sub, display: "block", marginBottom: 4 }}>Plazo (días)</label>
              <input type="number" value={plazo} onChange={(e) => setPlazo(e.target.value)} placeholder="30" style={IS} />
            </div>
            <div>
              <label style={{ fontSize: 12, fontWeight: 600, color: sub, display: "block", marginBottom: 4 }}>AWB / Tracking</label>
              <input value={awb} onChange={(e) => setAwb(e.target.value)} placeholder="opcional" style={IS} />
            </div>
            <div style={{ gridColumn: "1 / -1" }}>
              <label style={{ fontSize: 12, fontWeight: 600, color: sub, display: "block", marginBottom: 4 }}>Notas</label>
              <textarea value={notas} onChange={(e) => setNotas(e.target.value)} rows={2} style={{ ...IS, resize: "vertical" as const }} />
            </div>
          </div>
        </div>

        <div style={{ padding: "12px 20px", borderTop: `1px solid ${bd}`, display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <button onClick={onClose} style={{ padding: "8px 18px", border: `1px solid ${bd}`, borderRadius: 8, background: "transparent", color: sub, cursor: "pointer", fontSize: 13 }}>Cancelar</button>
          <button onClick={submit} disabled={saving} style={{ padding: "8px 20px", background: "var(--monza-accent)", border: "none", borderRadius: 8, color: "white", cursor: saving ? "wait" : "pointer", fontWeight: 700, fontSize: 13, display: "flex", alignItems: "center", gap: 6 }}>
            <ShoppingCart size={14} /> {saving ? "Creando..." : "Crear OC"}
          </button>
        </div>
      </div>
    </div>
  );
}

export default function MonzaAbastecimientoPage() {
  const { dark } = useMonzaTheme();
  const [tab, setTab] = useState<"por_comprar" | "ocs">("por_comprar");
  const [items, setItems] = useState<ItemCompra[]>([]);
  const [ocs, setOcs] = useState<OcCompra[]>([]);
  const [proveedores, setProveedores] = useState<Proveedor[]>([]);
  const [kpis, setKpis] = useState<KPIs | null>(null);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState("");
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [showModal, setShowModal] = useState(false);

  const bg = dark ? "#131b3e" : "white";
  const bd = dark ? "#1e2a4a" : "#E2E8F0";
  const txt = dark ? "white" : "#1E293B";
  const sub = dark ? "#8899cc" : "#64748B";

  const fetchAll = useCallback(async () => {
    setLoading(true);
    try {
      const [kpisRes, itemsRes, ocsRes, provRes] = await Promise.all([
        monzaAbastecimientoAPI.kpis(),
        monzaAbastecimientoAPI.porComprar({ q: q || undefined }),
        monzaAbastecimientoAPI.listOcs(),
        monzaAbastecimientoAPI.listProveedores(),
      ]);
      setKpis(kpisRes.data);
      setItems(itemsRes.data);
      setOcs(ocsRes.data);
      setProveedores(provRes.data);
    } catch { toast.error("Error al cargar abastecimiento"); }
    finally { setLoading(false); }
  }, [q]);

  useEffect(() => { fetchAll(); }, [fetchAll]);

  const toggleSel = (id: number) => {
    setSelected((prev) => {
      const n = new Set(prev);
      n.has(id) ? n.delete(id) : n.add(id);
      return n;
    });
  };
  const toggleAll = () => {
    setSelected((prev) => prev.size === items.length ? new Set() : new Set(items.map((i) => i.id)));
  };

  const advanceOc = async (oc: OcCompra, nuevoEstado: string) => {
    try {
      await monzaAbastecimientoAPI.updateOc(oc.id, { estado: nuevoEstado });
      toast.success(`${oc.numero} → ${OC_ESTADO[nuevoEstado]?.label || nuevoEstado}`);
      fetchAll();
    } catch { toast.error("Error al actualizar OC"); }
  };

  const selItems = items.filter((i) => selected.has(i.id));

  return (
    <div>
      {/* Header */}
      <div style={{ marginBottom: 18 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
          <ShoppingCart size={22} className="monza-ic" />
          <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: txt }}>Abastecimiento</h1>
        </div>
        <p style={{ margin: 0, fontSize: 13, color: sub }}>
          Ítems vendidos pendientes de compra a proveedor. Selecciona ítems y emite una OC de compra.
        </p>
      </div>

      {/* KPIs */}
      {kpis && (
        <div style={{ display: "flex", gap: 10, marginBottom: 18, flexWrap: "wrap" }}>
          <KpiCard label="Por comprar" value={kpis.por_comprar} accent="#F59E0B" />
          <KpiCard label="Comprado" value={kpis.comprado} accent="#3B82F6" />
          <KpiCard label="En tránsito" value={kpis.en_transito} accent="#6366F1" />
          <KpiCard label="En bodega" value={kpis.en_bodega} accent="#10B981" />
          <KpiCard label="OCs abiertas" value={kpis.ocs_abiertas} accent="var(--monza-accent)" />
        </div>
      )}

      {/* Tabs */}
      <div style={{ display: "flex", gap: 4, marginBottom: 14, borderBottom: `1px solid ${bd}` }}>
        {([["por_comprar", "Por comprar", items.length], ["ocs", "OCs de compra", ocs.length]] as const).map(([key, label, count]) => (
          <button key={key} onClick={() => setTab(key)}
            style={{ padding: "9px 16px", border: "none", background: "transparent", cursor: "pointer", fontSize: 13, fontWeight: 600,
              color: tab === key ? "var(--monza-accent)" : sub, borderBottom: `2px solid ${tab === key ? "var(--monza-accent)" : "transparent"}`, marginBottom: -1 }}>
            {label} <span style={{ fontSize: 11, background: dark ? "#1e2a4a" : "#F1F5F9", padding: "1px 7px", borderRadius: 10, marginLeft: 4 }}>{count}</span>
          </button>
        ))}
      </div>

      {/* Toolbar */}
      <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 12, flexWrap: "wrap" }}>
        <div style={{ position: "relative", flex: 1, minWidth: 240 }}>
          <Search size={14} style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)", color: "#94A3B8" }} />
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Buscar ítem, parte, N° COT..."
            style={{ width: "100%", padding: "8px 10px 8px 32px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 13, boxSizing: "border-box" as const, background: bg, color: txt }} />
        </div>
        {tab === "por_comprar" && selected.size > 0 && (
          <button onClick={() => setShowModal(true)}
            style={{ padding: "8px 16px", background: "var(--monza-accent)", border: "none", borderRadius: 8, color: "white", cursor: "pointer", fontWeight: 700, fontSize: 13, display: "flex", alignItems: "center", gap: 6 }}>
            <ShoppingCart size={14} /> Comprar {selected.size} ítem(s)
          </button>
        )}
        <button onClick={fetchAll} style={{ padding: "8px 10px", border: `1px solid ${bd}`, borderRadius: 6, background: bg, cursor: "pointer", color: sub }}>
          <RefreshCw size={14} />
        </button>
      </div>

      {/* Content */}
      <div style={{ background: bg, border: `1px solid ${bd}`, borderRadius: 10, overflow: "hidden" }}>
        {loading ? (
          <div style={{ padding: 40, textAlign: "center", color: "#94A3B8" }}>Cargando...</div>
        ) : tab === "por_comprar" ? (
          items.length === 0 ? (
            <div style={{ padding: 48, textAlign: "center", color: "#94A3B8" }}>
              <Package size={32} color="#E2E8F0" style={{ display: "block", margin: "0 auto 8px" }} />
              No hay ítems pendientes de compra.
            </div>
          ) : (
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead>
                <tr style={{ background: dark ? "#0d1321" : "#F8FAFC", borderBottom: `1px solid ${bd}` }}>
                  <th style={{ width: 36, padding: "10px 8px", textAlign: "center" }}>
                    <input type="checkbox" checked={selected.size === items.length && items.length > 0} onChange={toggleAll} style={{ accentColor: "var(--monza-accent)", cursor: "pointer" }} />
                  </th>
                  {["N° COT", "Cliente", "Repuesto", "Marca/Calidad", "Cant.", "Costo", "Plazo", "Vendida"].map((h) => (
                    <th key={h} style={{ padding: "10px 12px", textAlign: ["Cant.", "Costo"].includes(h) ? "right" : "left", fontWeight: 600, fontSize: 11, color: sub, textTransform: "uppercase" as const, letterSpacing: 0.5 }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {items.map((it) => {
                  const isSel = selected.has(it.id);
                  return (
                    <tr key={it.id} style={{ borderBottom: `1px solid ${dark ? "#1e2a4a" : "#F1F5F9"}`, background: isSel ? (dark ? "#1a2340" : "#FFF7F7") : "transparent", cursor: "pointer" }}
                      onClick={() => toggleSel(it.id)}>
                      <td style={{ textAlign: "center", padding: "8px" }}>
                        <input type="checkbox" checked={isSel} onChange={() => toggleSel(it.id)} onClick={(e) => e.stopPropagation()} style={{ accentColor: "var(--monza-accent)", cursor: "pointer" }} />
                      </td>
                      <td style={{ padding: "9px 12px" }}>
                        <div style={{ fontWeight: 600, fontSize: 12, color: "var(--monza-accent)" }}>{it.cot_numero}</div>
                        {it.vehiculo && <div style={{ fontSize: 10, color: sub }}>{it.vehiculo}</div>}
                      </td>
                      <td style={{ padding: "9px 12px", color: txt }}>{it.cliente || "—"}</td>
                      <td style={{ padding: "9px 12px" }}>
                        <div style={{ color: txt, fontWeight: 500 }}>{it.descripcion}</div>
                        {it.numero_parte && <div style={{ fontSize: 10, color: sub }}>{it.numero_parte}</div>}
                      </td>
                      <td style={{ padding: "9px 12px", color: sub, fontSize: 12 }}>
                        {it.marca || "—"}{it.calidad ? ` · ${it.calidad}` : ""}
                      </td>
                      <td style={{ padding: "9px 12px", textAlign: "right", color: txt }}>{it.cantidad}</td>
                      <td style={{ padding: "9px 12px", textAlign: "right", color: sub }}>{it.costo ? `${it.moneda} ${it.costo}` : "—"}</td>
                      <td style={{ padding: "9px 12px", color: sub, fontSize: 12 }}>{it.plazo_entrega || "—"}</td>
                      <td style={{ padding: "9px 12px", color: sub, fontSize: 12 }}>{fmtDate(it.fecha_venta)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )
        ) : (
          // OCs tab
          ocs.length === 0 ? (
            <div style={{ padding: 48, textAlign: "center", color: "#94A3B8" }}>
              <FileText size={32} color="#E2E8F0" style={{ display: "block", margin: "0 auto 8px" }} />
              No hay OCs de compra emitidas.
            </div>
          ) : (
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead>
                <tr style={{ background: dark ? "#0d1321" : "#F8FAFC", borderBottom: `1px solid ${bd}` }}>
                  {["N° OC", "Proveedor", "País", "Ítems", "Plazo", "AWB", "Estado", "Acciones"].map((h) => (
                    <th key={h} style={{ padding: "10px 12px", textAlign: "left", fontWeight: 600, fontSize: 11, color: sub, textTransform: "uppercase" as const, letterSpacing: 0.5 }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {ocs.map((oc) => {
                  const es = OC_ESTADO[oc.estado] || OC_ESTADO.emitida;
                  return (
                    <tr key={oc.id} style={{ borderBottom: `1px solid ${dark ? "#1e2a4a" : "#F1F5F9"}` }}>
                      <td style={{ padding: "10px 12px", fontWeight: 700, color: "var(--monza-accent)" }}>{oc.numero}</td>
                      <td style={{ padding: "10px 12px", color: txt }}>{oc.proveedor_nombre || "—"}</td>
                      <td style={{ padding: "10px 12px", color: sub }}>{oc.pais || "—"}</td>
                      <td style={{ padding: "10px 12px", color: sub }}>{oc.items_count}</td>
                      <td style={{ padding: "10px 12px", color: sub }}>{oc.plazo_dias ? `${oc.plazo_dias} días` : "—"}</td>
                      <td style={{ padding: "10px 12px", color: sub, fontSize: 12 }}>{oc.awb || "—"}</td>
                      <td style={{ padding: "10px 12px" }}>
                        <span style={{ fontSize: 11, background: es.bg, color: es.color, padding: "3px 10px", borderRadius: 10, fontWeight: 600 }}>{es.label}</span>
                      </td>
                      <td style={{ padding: "10px 12px" }}>
                        <div style={{ display: "flex", gap: 6 }}>
                          {oc.estado === "emitida" && (
                            <button onClick={() => advanceOc(oc, "en_transito")} title="Marcar en tránsito"
                              style={{ padding: "4px 10px", border: "1px solid #3B82F6", borderRadius: 6, background: "transparent", color: "#3B82F6", cursor: "pointer", fontSize: 11, fontWeight: 600, display: "flex", alignItems: "center", gap: 4 }}>
                              <Truck size={11} /> En tránsito
                            </button>
                          )}
                          {(oc.estado === "emitida" || oc.estado === "en_transito") && (
                            <button onClick={() => advanceOc(oc, "recibida")} title="Marcar recibida (a bodega)"
                              style={{ padding: "4px 10px", border: "1px solid #10B981", borderRadius: 6, background: "transparent", color: "#10B981", cursor: "pointer", fontSize: 11, fontWeight: 600, display: "flex", alignItems: "center", gap: 4 }}>
                              <Package size={11} /> Recibida
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )
        )}
      </div>

      {showModal && (
        <CrearOcModal
          items={selItems}
          proveedores={proveedores}
          onClose={() => setShowModal(false)}
          onDone={() => { setShowModal(false); setSelected(new Set()); fetchAll(); }}
        />
      )}
    </div>
  );
}
