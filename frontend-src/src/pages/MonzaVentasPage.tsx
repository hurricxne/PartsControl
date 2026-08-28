import { useState, useEffect, useCallback } from "react";
import { Link } from "react-router-dom";
import { Search, TrendingUp, ChevronDown, ChevronRight } from "lucide-react";
import { useMonzaTheme } from "./MonzaLayout";
import { monzaVentasAPI, monzaCotizacionesAPI } from "../services/monzaApi";
import MonzaDocs from "./MonzaDocs";
import toast from "react-hot-toast";

interface Venta {
  id: number; numero: string; estado: string; linea?: string; vehiculo?: string;
  oc_cliente?: string; fecha_venta?: string; fecha_entrega_est?: string;
  /** Venta a cliente particular: la "OC" es el N° de esta cotización (no la emitió el
   *  cliente). Opcional: contra un backend viejo simplemente no se muestra la marca. */
  cliente_sin_oc?: boolean;
  total_bruto: number; items_count: number; asesor?: string; fecha_creacion: string;
  cliente?: { nombre: string; rut?: string };
  lead_numero?: string; pipeline?: string;
}

// Estado del pipeline post-venta (comprada → embarcada → bodega → despachada)
const PIPELINE_CFG: Record<string, { bg: string; color: string; label: string }> = {
  cotizado:    { bg: "#F1F5F9", color: "#64748B", label: "Sin abastecer" },
  por_comprar: { bg: "#FEF3C7", color: "#B45309", label: "Por comprar" },
  comprado:    { bg: "#DBEAFE", color: "#1D4ED8", label: "Comprada" },
  en_transito: { bg: "#EDE9FE", color: "#6D28D9", label: "Embarcada" },
  por_recibir: { bg: "#E0F2FE", color: "#0369A1", label: "En recepción" },
  en_bodega:   { bg: "#DCFCE7", color: "#15803D", label: "En bodega" },
  despachado:  { bg: "#D1FAE5", color: "#047857", label: "Despachada" },
  // El backend manda 'reclamo' solo cuando TODAS las líneas de la venta están en
  // reclamo al proveedor (no llegó / dañado no utilizable). Sin esta entrada la venta
  // se quedaba sin badge; antes se pintaba "Sin abastecer", que era mentira: la
  // mercadería sí se compró, el proveedor falló y hay un reclamo abierto en Bodega.
  reclamo:     { bg: "#FEE2E2", color: "#B91C1C", label: "Reclamo al proveedor" },
};
interface KPIs { vendidas_mes: number; total_mes: number; pendientes_entrega: number; }
interface CotItem { id: number; descripcion: string; numero_parte?: string; marca?: string; cantidad: number; precio_unitario_clp?: number; subtotal_clp?: number; plazo_entrega?: string; calidad?: string; }

const LINEA_CONFIG: Record<string, { bg: string; color: string; label: string }> = {
  autos: { bg: "#DBEAFE", color: "#1D4ED8", label: "Autos" },
  maquinaria: { bg: "#FEF3C7", color: "#D97706", label: "Maquinaria" },
};
const ESTADO_COLORS: Record<string, { bg: string; color: string }> = {
  vendida: { bg: "#DCFCE7", color: "#15803D" },
  // Desde los arreglos del equipo (2026-08-21) la pestaña lista SOLO ventas cerradas
  // (vendida/despachado): antes las despachadas desaparecían de acá. enviada/propuesta
  // quedan como respaldo defensivo del chip por si el backend sirviera datos viejos.
  despachado: { bg: "#E0E7FF", color: "#4338CA" },
  enviada: { bg: "#DBEAFE", color: "#1D4ED8" },
  propuesta: { bg: "#F1F5F9", color: "#475569" },
};

function fmt(n: number) { return `$${n.toLocaleString("es-CL")}`; }
function fmtDate(d?: string) { return d ? new Date(d).toLocaleDateString("es-CL") : "Sin fecha"; }

function slaLabel(fecha?: string): { label: string; bg: string; color: string } {
  if (!fecha) return { label: "Sin fecha", bg: "#F1F5F9", color: "#94A3B8" };
  const days = Math.floor((new Date(fecha).getTime() - Date.now()) / 86400000);
  if (days < 0) return { label: `Vencido ${Math.abs(days)}d`, bg: "#FEE2E2", color: "#B91C1C" };
  if (days === 0) return { label: "Hoy", bg: "#FEF3C7", color: "#D97706" };
  if (days <= 3) return { label: `${days} día${days !== 1 ? "s" : ""}`, bg: "#FEF3C7", color: "#D97706" };
  return { label: `${days} días`, bg: "#DCFCE7", color: "#15803D" };
}

function KpiCard({ label, value, sub, accent = "var(--monza-accent)" }: { label: string; value: string | number; sub?: string; accent?: string }) {
  const { dark } = useMonzaTheme();
  return (
    <div style={{ background: dark ? "#131b3e" : "white", borderRadius: 12, border: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}`, padding: "18px 20px", flex: 1 }}>
      <div style={{ width: 28, height: 4, borderRadius: 2, background: accent, marginBottom: 12 }} />
      <div style={{ fontSize: 28, fontWeight: 800, color: dark ? "white" : "#1E293B", lineHeight: 1 }}>{value}</div>
      <div style={{ fontSize: 12, color: dark ? "#8899cc" : "#64748B", marginTop: 5 }}>{label}</div>
      {sub && <div style={{ fontSize: 11, color: dark ? "#475569" : "#94A3B8", marginTop: 3 }}>{sub}</div>}
    </div>
  );
}

export default function MonzaVentasPage() {
  const { dark } = useMonzaTheme();
  const [items, setItems] = useState<Venta[]>([]);
  const [total, setTotal] = useState(0);
  const [kpis, setKpis] = useState<KPIs | null>(null);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState("");
  const [estado, setEstado] = useState("todas");
  const [desde, setDesde] = useState("");
  const [hasta, setHasta] = useState("");
  const [updatingOC, setUpdatingOC] = useState<{ id: number; val: string } | null>(null);
  const [expanded, setExpanded] = useState<number | null>(null);
  const [details, setDetails] = useState<Record<number, { items: CotItem[]; condiciones_servicio?: string; forma_pago?: string; vehiculo?: string }>>({});
  const [loadingDetail, setLoadingDetail] = useState<number | null>(null);

  const fetchAll = useCallback(async () => {
    setLoading(true);
    try {
      const [ventasRes, kpisRes] = await Promise.all([
        monzaVentasAPI.list({ q: q || undefined, estado: estado !== "todas" ? estado : undefined, desde: desde || undefined, hasta: hasta || undefined }),
        monzaVentasAPI.kpis(),
      ]);
      setItems(ventasRes.data.items);
      setTotal(ventasRes.data.total);
      setKpis(kpisRes.data);
    } catch { toast.error("Error al cargar ventas"); }
    finally { setLoading(false); }
  }, [q, estado, desde, hasta]);

  useEffect(() => { fetchAll(); }, [fetchAll]);

  const handleSaveOC = async (id: number, oc: string) => {
    try {
      await monzaCotizacionesAPI.update(id, { oc_cliente: oc });
      toast.success("OC actualizada");
      fetchAll();
    } catch { toast.error("Error"); }
    setUpdatingOC(null);
  };

  const toggleExpand = async (id: number) => {
    if (expanded === id) { setExpanded(null); return; }
    setExpanded(id);
    if (details[id]) return;
    setLoadingDetail(id);
    try {
      const res = await monzaCotizacionesAPI.get(id);
      const d = res.data as { items: CotItem[]; condiciones_servicio?: string; forma_pago?: string; vehiculo?: string };
      setDetails((prev) => ({ ...prev, [id]: d }));
    } catch { toast.error("Error al cargar detalle"); }
    finally { setLoadingDetail(null); }
  };

  const sub = dark ? "#8899cc" : "#64748B";
  const bg = dark ? "#131b3e" : "white";
  const bd = dark ? "#1e2a4a" : "#E2E8F0";
  const txt = dark ? "white" : "#1E293B";

  return (
    <div>
      {/* Header */}
      <div style={{ marginBottom: 20 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
          <TrendingUp size={22} className="monza-ic" />
          <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: txt }}>Ventas</h1>
        </div>
        <p style={{ margin: 0, fontSize: 13, color: sub }}>
          Cotizaciones cerradas. Haz clic en "Ver" para expandir el detalle de cada cotización.
        </p>
      </div>

      {/* KPIs */}
      {kpis && (
        <div style={{ display: "flex", gap: 12, marginBottom: 20, flexWrap: "wrap" }}>
          <KpiCard label="Vendidas este mes" accent="#10B981" value={kpis.vendidas_mes} />
          <KpiCard label="Total vendido mes" accent="#F59E0B" value={fmt(kpis.total_mes)} />
          <KpiCard label="Pendientes entrega" accent="#6366F1" value={kpis.pendientes_entrega} />
        </div>
      )}

      {/* Filters */}
      <div style={{ background: bg, border: `1px solid ${bd}`, borderRadius: 10, padding: "14px 16px", marginBottom: 16 }}>
        <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <div style={{ position: "relative", flex: 1, minWidth: 280 }}>
            <Search size={14} style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)", color: "#94A3B8" }} />
            <input value={q} onChange={(e) => setQ(e.target.value)}
              placeholder="Cliente, RUT, OC, N° cotización..."
              style={{ width: "100%", padding: "8px 10px 8px 32px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 13, boxSizing: "border-box" as const, background: dark ? "#0d1321" : "white", color: txt }} />
          </div>
          {/* Solo estados de VENTA (arreglos del equipo 2026-08-21): propuestas y
              enviadas viven en la pestaña Cotizaciones, no acá. */}
          <select value={estado} onChange={(e) => setEstado(e.target.value)}
            style={{ padding: "8px 12px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 13, background: dark ? "#0d1321" : "white", color: txt }}>
            <option value="todas">Todas las ventas</option>
            <option value="vendida">Vendida</option>
            <option value="despachado">Despachada</option>
          </select>
          <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: sub }}>
            Desde: <input type="date" value={desde} onChange={(e) => setDesde(e.target.value)}
              style={{ padding: "6px 8px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 12, background: dark ? "#0d1321" : "white", color: txt, colorScheme: dark ? "dark" : "light" as const }} />
            Hasta: <input type="date" value={hasta} onChange={(e) => setHasta(e.target.value)}
              style={{ padding: "6px 8px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 12, background: dark ? "#0d1321" : "white", color: txt, colorScheme: dark ? "dark" : "light" as const }} />
          </div>
        </div>
      </div>

      {/* Table */}
      <div style={{ background: bg, border: `1px solid ${bd}`, borderRadius: 10, overflow: "hidden" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
          <thead>
            <tr style={{ background: dark ? "#0d1321" : "#F8FAFC", borderBottom: `1px solid ${bd}` }}>
              <th style={{ width: 32 }}></th>
              {["Cotización", "Cliente", "Línea", "OC Cliente", "Vendida", "Monto", "Entrega Est.", "Plazo", "Ítems", ""].map((h, i) => (
                <th key={i} style={{ padding: "10px 12px", textAlign: ["Monto"].includes(h) ? "right" : ["Ítems"].includes(h) ? "center" : "left", fontWeight: 600, fontSize: 11, color: sub, textTransform: "uppercase" as const, letterSpacing: 0.5 }}>
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading
              ? <tr><td colSpan={11} style={{ padding: 40, textAlign: "center", color: "#94A3B8" }}>Cargando...</td></tr>
              : items.length === 0
                ? <tr><td colSpan={11} style={{ padding: 40, textAlign: "center", color: "#94A3B8" }}>No hay ventas con los filtros actuales.</td></tr>
                : items.flatMap((v) => {
                  const lc = v.linea ? LINEA_CONFIG[v.linea] : null;
                  const ec = ESTADO_COLORS[v.estado] || ESTADO_COLORS.propuesta;
                  const sla = slaLabel(v.fecha_entrega_est);
                  const isExpanded = expanded === v.id;
                  const rowBg = isExpanded ? (dark ? "#0d1321" : "#F8FAFF") : bg;

                  return [
                    <tr key={v.id}
                      style={{ borderBottom: isExpanded ? "none" : `1px solid ${dark ? "#1e2a4a" : "#F1F5F9"}`, background: rowBg }}
                      onMouseEnter={(e) => { if (!isExpanded) (e.currentTarget as HTMLTableRowElement).style.background = dark ? "#1a2340" : "#FAFAFA"; }}
                      onMouseLeave={(e) => { (e.currentTarget as HTMLTableRowElement).style.background = rowBg; }}>
                      <td style={{ paddingLeft: 12, color: "#94A3B8", cursor: "pointer" }} onClick={() => toggleExpand(v.id)}>
                        {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                      </td>
                      <td style={{ padding: "10px 12px", cursor: "pointer" }} onClick={() => toggleExpand(v.id)}>
                        <div style={{ fontWeight: 600, fontSize: 12, color: txt }}>{v.numero}</div>
                        {v.lead_numero && <div style={{ fontSize: 10, color: "#94A3B8" }}>Lead {v.lead_numero}</div>}
                      </td>
                      <td style={{ padding: "10px 12px", cursor: "pointer" }} onClick={() => toggleExpand(v.id)}>
                        {/* Acceso directo a la ficha (mismo criterio que en Ventas —
                            Contabilidad): el RUT y la razón social del cliente son lo que
                            el SII exige para emitir, y corregirlos obligaba a irse a
                            Configuración a buscarlo a mano. stopPropagation: la fila
                            entera es expandible, el enlace no debe además desplegarla. */}
                        <Link to={`/monzaparts/configuracion?tab=clientes&cliente=${encodeURIComponent(v.cliente?.rut || v.cliente?.nombre || "")}`}
                          onClick={(e) => e.stopPropagation()}
                          title="Abrir la ficha del cliente (corregir RUT o razón social)"
                          style={{ fontWeight: 500, color: txt, textDecoration: "none" }}
                          onMouseEnter={(e) => { e.currentTarget.style.textDecoration = "underline"; }}
                          onMouseLeave={(e) => { e.currentTarget.style.textDecoration = "none"; }}>
                          {v.cliente?.nombre || "—"}
                        </Link>
                        {v.cliente?.rut && <div style={{ fontSize: 11, color: "#94A3B8" }}>{v.cliente.rut}</div>}
                      </td>
                      <td style={{ padding: "10px 12px", cursor: "pointer" }} onClick={() => toggleExpand(v.id)}>
                        {lc ? <span style={{ fontSize: 11, background: lc.bg, color: lc.color, padding: "3px 10px", borderRadius: 10, fontWeight: 600 }}>{lc.label}</span>
                          : <span style={{ color: "#94A3B8" }}>—</span>}
                      </td>
                      <td style={{ padding: "10px 12px", color: sub }}>
                        {updatingOC?.id === v.id ? (
                          <input autoFocus value={updatingOC.val}
                            onChange={(e) => setUpdatingOC({ id: v.id, val: e.target.value })}
                            onBlur={() => handleSaveOC(v.id, updatingOC.val)}
                            onKeyDown={(e) => { if (e.key === "Enter") handleSaveOC(v.id, updatingOC.val); if (e.key === "Escape") setUpdatingOC(null); }}
                            style={{ padding: "4px 8px", border: "1px solid var(--monza-accent)", borderRadius: 4, fontSize: 12, width: 100 }} />
                        ) : (
                          <>
                            <span onClick={(e) => { e.stopPropagation(); setUpdatingOC({ id: v.id, val: v.oc_cliente || "" }); }}
                              title={v.cliente_sin_oc
                                ? "Cliente particular: no emite OC, el respaldo es el N° de esta cotización"
                                : "Click para editar OC"}
                              style={{ cursor: "pointer", borderBottom: v.oc_cliente ? "none" : `1px dashed ${dark ? "#334155" : "#CBD5E1"}`, color: v.oc_cliente ? txt : (dark ? "#334155" : "#CBD5E1") }}>
                              {v.oc_cliente || "—"}
                            </span>
                            {/* Sin esta marca, una OC igual al N° de cotización se lee
                                como un error de digitación. Dice que fue deliberado. */}
                            {v.cliente_sin_oc && (
                              <span style={{ marginLeft: 6, fontSize: 10, color: dark ? "#8899cc" : "#64748B", border: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}`, padding: "1px 5px", borderRadius: 999, whiteSpace: "nowrap" }}>
                                particular
                              </span>
                            )}
                          </>
                        )}
                      </td>
                      <td style={{ padding: "10px 12px", fontSize: 12, cursor: "pointer" }} onClick={() => toggleExpand(v.id)}>
                        <div style={{ color: txt }}>{v.fecha_venta ? fmtDate(v.fecha_venta) : "—"}</div>
                        <div style={{ display: "flex", gap: 4, flexWrap: "wrap", marginTop: 2 }}>
                          <span style={{ fontSize: 10, background: ec.bg, color: ec.color, padding: "1px 6px", borderRadius: 6 }}>
                            {v.estado.charAt(0).toUpperCase() + v.estado.slice(1)}
                          </span>
                          {v.pipeline && PIPELINE_CFG[v.pipeline] && (
                            <span style={{ fontSize: 10, background: PIPELINE_CFG[v.pipeline].bg, color: PIPELINE_CFG[v.pipeline].color, padding: "1px 6px", borderRadius: 6, fontWeight: 600 }}>
                              {PIPELINE_CFG[v.pipeline].label}
                            </span>
                          )}
                        </div>
                      </td>
                      <td style={{ padding: "10px 12px", textAlign: "right", fontWeight: 600, color: txt, cursor: "pointer" }} onClick={() => toggleExpand(v.id)}>{fmt(v.total_bruto)}</td>
                      <td style={{ padding: "10px 12px", color: sub, fontSize: 12, cursor: "pointer" }} onClick={() => toggleExpand(v.id)}>{fmtDate(v.fecha_entrega_est)}</td>
                      <td style={{ padding: "10px 12px", cursor: "pointer" }} onClick={() => toggleExpand(v.id)}>
                        <span style={{ fontSize: 10, background: sla.bg, color: sla.color, padding: "2px 8px", borderRadius: 8, fontWeight: 600 }}>{sla.label}</span>
                      </td>
                      <td style={{ padding: "10px 12px", textAlign: "center", cursor: "pointer" }} onClick={() => toggleExpand(v.id)}>
                        <span style={{ background: dark ? "#1e2a4a" : "#F1F5F9", borderRadius: 10, padding: "2px 10px", fontSize: 12, fontWeight: 600, color: sub }}>{v.items_count}</span>
                      </td>
                      <td style={{ padding: "10px 12px" }}>
                        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                          <button onClick={() => toggleExpand(v.id)}
                            style={{ background: isExpanded ? "var(--monza-accent)" : "transparent", border: "1px solid var(--monza-accent)", borderRadius: 6, cursor: "pointer", color: isExpanded ? "white" : "var(--monza-accent)", padding: "4px 12px", fontSize: 12, fontWeight: 600 }}>
                            {loadingDetail === v.id ? "..." : isExpanded ? "Cerrar" : "Ver"}
                          </button>
                        </div>
                      </td>
                    </tr>,

                    /* Fila expandida */
                    ...(isExpanded ? [
                      <tr key={`detail-${v.id}`} style={{ borderBottom: `1px solid ${bd}` }}>
                        <td colSpan={11} style={{ padding: "0 16px 16px 48px", background: dark ? "#0a0e1f" : "#F8FAFF" }}>
                          {loadingDetail === v.id ? (
                            <div style={{ padding: "16px 0", color: "#94A3B8", fontSize: 12 }}>Cargando detalle...</div>
                          ) : details[v.id] ? (
                            <div>
                              {(details[v.id].vehiculo || details[v.id].forma_pago) && (
                                <div style={{ display: "flex", gap: 16, padding: "10px 0 8px", fontSize: 12, color: sub }}>
                                  {details[v.id].vehiculo && <span>🚗 <strong style={{ color: txt }}>{details[v.id].vehiculo}</strong></span>}
                                  {details[v.id].forma_pago && <span>💳 {details[v.id].forma_pago}</span>}
                                </div>
                              )}
                              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
                                <thead>
                                  <tr style={{ background: dark ? "#131b3e" : "#F1F5F9" }}>
                                    {["Repuesto", "N° parte", "Marca", "Qty", "Precio Unit.", "Total", "Plazo entrega"].map((h, i) => (
                                      <th key={i} style={{ padding: "6px 10px", textAlign: i >= 3 && i <= 5 ? "right" : "left", fontWeight: 600, fontSize: 10, color: sub, textTransform: "uppercase" as const, letterSpacing: 0.4 }}>{h}</th>
                                    ))}
                                  </tr>
                                </thead>
                                <tbody>
                                  {details[v.id].items.map((it) => (
                                    <tr key={it.id} style={{ borderBottom: `1px solid ${dark ? "#1e2a4a" : "#F1F5F9"}` }}>
                                      <td style={{ padding: "6px 10px", color: txt, fontWeight: 500 }}>{it.descripcion}</td>
                                      <td style={{ padding: "6px 10px", color: sub }}>{it.numero_parte || "—"}</td>
                                      <td style={{ padding: "6px 10px", color: sub }}>{it.marca || "—"}</td>
                                      <td style={{ padding: "6px 10px", textAlign: "right", color: txt }}>{it.cantidad}</td>
                                      <td style={{ padding: "6px 10px", textAlign: "right", color: sub }}>{it.precio_unitario_clp ? fmt(it.precio_unitario_clp) : "—"}</td>
                                      <td style={{ padding: "6px 10px", textAlign: "right", fontWeight: 600, color: txt }}>{it.subtotal_clp ? fmt(it.subtotal_clp) : "—"}</td>
                                      <td style={{ padding: "6px 10px", color: sub }}>{it.plazo_entrega || "—"}</td>
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                              {details[v.id].condiciones_servicio && (
                                <div style={{ marginTop: 8, padding: "8px 12px", background: dark ? "#131b3e" : "#FFFBEB", border: `1px solid ${dark ? "#2a3a5e" : "#FDE68A"}`, borderRadius: 6, fontSize: 11, color: dark ? "#d4b896" : "#78350F" }}>
                                  <strong>Nota:</strong> {details[v.id].condiciones_servicio}
                                </div>
                              )}
                              <div style={{ marginTop: 10 }}>
                                <MonzaDocs entidad="cotizacion" entidadId={v.id} categorias={["factura", "boleta", "guía de despacho", "orden de compra", "otro"]} titulo="Documentos de venta" />
                              </div>
                            </div>
                          ) : null}
                        </td>
                      </tr>
                    ] : []),
                  ];
                })
            }
          </tbody>
        </table>
        {total > 0 && (
          <div style={{ padding: "10px 16px", borderTop: `1px solid ${dark ? "#1e2a4a" : "#F1F5F9"}`, fontSize: 12, color: "#94A3B8" }}>
            Mostrando {items.length} de {total}
          </div>
        )}
      </div>

    </div>
  );
}
