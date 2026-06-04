import { useState, useEffect, useCallback } from "react";
import { Search, Download, RefreshCw, FileText, ChevronDown, ChevronRight, CheckCircle } from "lucide-react";
import { monzaCotizacionesAPI } from "../services/monzaApi";
import toast from "react-hot-toast";

interface Cotizacion {
  id: number; numero: string; estado: string; tipo_cotizacion?: string;
  linea?: string; vehiculo?: string; total_bruto: number; items_count: number;
  fecha_creacion: string; fecha_venta?: string; oc_cliente?: string; asesor?: string;
  cliente?: { nombre: string; rut?: string };
  lead_numero?: string;
}
interface CotItem {
  id: number; descripcion: string; marca?: string; numero_parte?: string;
  calidad?: string; cantidad: number; precio_unitario_clp?: number; subtotal_clp?: number;
  plazo_entrega?: string;
}
interface CotDetail {
  id: number; numero: string; estado: string; vehiculo?: string; vin?: string; anio?: string;
  forma_pago?: string; condiciones_servicio?: string; tipo_cotizacion?: string;
  total_neto?: number; iva_monto?: number; total_bruto?: number;
  items: CotItem[];
  cliente?: { nombre: string; rut?: string };
}

const ESTADO_CONFIG: Record<string, { bg: string; color: string; label: string }> = {
  propuesta: { bg: "#F1F5F9", color: "#475569", label: "Propuesta" },
  enviada: { bg: "#DBEAFE", color: "#1D4ED8", label: "Enviada" },
  vendida: { bg: "#DCFCE7", color: "#15803D", label: "Vendida" },
  rechazada: { bg: "#FEE2E2", color: "#B91C1C", label: "Rechazada" },
};
const LINEA_CONFIG: Record<string, { bg: string; color: string }> = {
  autos: { bg: "#DBEAFE", color: "#1D4ED8" },
  maquinaria: { bg: "#FEF3C7", color: "#D97706" },
};

function fmt(n: number) { return n > 0 ? `$${n.toLocaleString("es-CL")}` : "$0"; }
function fmtDate(d?: string) { return d ? new Date(d).toLocaleDateString("es-CL") : "—"; }

export default function MonzaCotizacionesPage() {
  const [items, setItems] = useState<Cotizacion[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState("");
  const [estado, setEstado] = useState("todos");
  const [desde, setDesde] = useState("");
  const [hasta, setHasta] = useState("");
  const [downloading, setDownloading] = useState<number | null>(null);
  const [updatingEstado, setUpdatingEstado] = useState<number | null>(null);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [details, setDetails] = useState<Record<number, CotDetail>>({});
  const [loadingDetail, setLoadingDetail] = useState<number | null>(null);
  const [markingVendida, setMarkingVendida] = useState<number | null>(null);

  const fetchAll = useCallback(async () => {
    setLoading(true);
    try {
      const r = await monzaCotizacionesAPI.list({
        q: q || undefined,
        estado: estado !== "todos" ? estado : undefined,
        desde: desde || undefined,
        hasta: hasta || undefined,
      });
      setItems(r.data.items);
      setTotal(r.data.total);
    } catch { toast.error("Error al cargar cotizaciones"); }
    finally { setLoading(false); }
  }, [q, estado, desde, hasta]);

  useEffect(() => { fetchAll(); }, [fetchAll]);

  const handleDownload = async (cot: Cotizacion) => {
    setDownloading(cot.id);
    try {
      const r = await monzaCotizacionesAPI.downloadPdf(cot.id);
      const blob = new Blob([r.data], { type: "application/pdf" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `Cotizacion_${cot.numero}.pdf`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch { toast.error("Error al descargar PDF"); }
    finally { setDownloading(null); }
  };

  const handleEstado = async (id: number, nuevoEstado: string) => {
    setUpdatingEstado(id);
    try {
      await monzaCotizacionesAPI.update(id, { estado: nuevoEstado });
      toast.success("Estado actualizado");
      fetchAll();
    } catch { toast.error("Error al actualizar estado"); }
    finally { setUpdatingEstado(null); }
  };

  const toggleExpand = async (id: number) => {
    const next = new Set(expanded);
    if (next.has(id)) {
      next.delete(id);
      setExpanded(next);
      return;
    }
    next.add(id);
    setExpanded(next);
    if (!details[id]) {
      setLoadingDetail(id);
      try {
        const r = await monzaCotizacionesAPI.get(id);
        setDetails((prev) => ({ ...prev, [id]: r.data }));
      } catch { toast.error("Error al cargar detalle"); }
      finally { setLoadingDetail(null); }
    }
  };

  const handleMarcarVendida = async (cot: Cotizacion) => {
    if (!window.confirm(`¿Marcar la cotización ${cot.numero} como Vendida?`)) return;
    setMarkingVendida(cot.id);
    try {
      await monzaCotizacionesAPI.update(cot.id, { estado: "vendida" });
      toast.success(`Cotización ${cot.numero} marcada como Vendida`);
      fetchAll();
    } catch { toast.error("Error al marcar como vendida"); }
    finally { setMarkingVendida(null); }
  };

  const CALIDAD_LABEL: Record<string, string> = { sin_calificar: "—", genuine: "Genuine", oem: "OEM", aftermarket: "Aftermarket" };

  return (
    <div>
      {/* Header */}
      <div style={{ marginBottom: 24 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
          <FileText size={22} className="monza-ic" />
          <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: "#1E293B" }}>Registro de cotizaciones</h1>
        </div>
        <p style={{ margin: 0, fontSize: 13, color: "#64748B" }}>
          Todas las cotizaciones emitidas en el sistema, ordenadas por fecha descendente.
        </p>
      </div>

      {/* Filters */}
      <div style={{ background: "white", border: "1px solid #E2E8F0", borderRadius: 10, padding: "14px 16px", marginBottom: 16 }}>
        <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <div style={{ position: "relative", flex: 1, minWidth: 280 }}>
            <Search size={14} style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)", color: "#94A3B8" }} />
            <input value={q} onChange={(e) => setQ(e.target.value)}
              placeholder="Buscar N° COT, cliente, vehículo, N° parte..."
              style={{ width: "100%", padding: "8px 10px 8px 32px", border: "1px solid #E2E8F0", borderRadius: 6, fontSize: 13, boxSizing: "border-box" }} />
          </div>
          <select value={estado} onChange={(e) => setEstado(e.target.value)}
            style={{ padding: "8px 12px", border: "1px solid #E2E8F0", borderRadius: 6, fontSize: 13, background: "white" }}>
            <option value="todos">Todos los estados</option>
            <option value="propuesta">Propuesta</option>
            <option value="enviada">Enviada</option>
            <option value="vendida">Vendida</option>
            <option value="rechazada">Rechazada</option>
          </select>
          <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: "#64748B" }}>
            Fecha:
            <input type="date" value={desde} onChange={(e) => setDesde(e.target.value)}
              style={{ padding: "6px 8px", border: "1px solid #E2E8F0", borderRadius: 6, fontSize: 12 }} />
            <span>—</span>
            <input type="date" value={hasta} onChange={(e) => setHasta(e.target.value)}
              style={{ padding: "6px 8px", border: "1px solid #E2E8F0", borderRadius: 6, fontSize: 12 }} />
          </div>
          <button onClick={fetchAll} style={{ padding: "7px 10px", border: "1px solid #E2E8F0", borderRadius: 6, background: "white", cursor: "pointer", color: "#64748B" }}>
            <RefreshCw size={13} />
          </button>
        </div>
      </div>

      {/* Table */}
      <div style={{ background: "white", border: "1px solid #E2E8F0", borderRadius: 10, overflow: "hidden" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
          <thead>
            <tr style={{ background: "#F8FAFC", borderBottom: "1px solid #E2E8F0" }}>
              <th style={{ padding: "10px 8px", width: 32 }} />
              {["N° COT", "Fecha", "Cliente", "Vehículo", "Ítems", "Total", "Estado", "Asesor", "Acciones"].map((h) => (
                <th key={h} style={{ padding: "10px 12px", textAlign: h === "Total" ? "right" : h === "Ítems" ? "center" : h === "Acciones" ? "center" : "left", fontWeight: 600, fontSize: 11, color: "#64748B", textTransform: "uppercase", letterSpacing: 0.5 }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading
              ? <tr><td colSpan={10} style={{ padding: 40, textAlign: "center", color: "#94A3B8" }}>Cargando...</td></tr>
              : items.length === 0
                ? <tr><td colSpan={10} style={{ padding: 40, textAlign: "center", color: "#94A3B8" }}>No se encontraron cotizaciones.</td></tr>
                : items.flatMap((cot) => {
                  const ec = ESTADO_CONFIG[cot.estado] || ESTADO_CONFIG.propuesta;
                  const lc = cot.linea ? LINEA_CONFIG[cot.linea] : null;
                  const isExpanded = expanded.has(cot.id);
                  const det = details[cot.id];

                  const mainRow = (
                    <tr key={cot.id} style={{ borderBottom: isExpanded ? "none" : "1px solid #F1F5F9", background: isExpanded ? "#F8FAFC" : "white" }}>
                      <td style={{ padding: "10px 8px", textAlign: "center" }}>
                        <button onClick={() => toggleExpand(cot.id)}
                          style={{ background: "transparent", border: "none", cursor: "pointer", color: "#94A3B8", padding: 2, display: "flex", alignItems: "center" }}>
                          {isExpanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
                        </button>
                      </td>
                      <td style={{ padding: "10px 12px" }}>
                        <div style={{ fontWeight: 600, color: "#1E293B", fontSize: 12 }}>{cot.numero}</div>
                        {cot.lead_numero && <div style={{ fontSize: 10, color: "#94A3B8" }}>Lead {cot.lead_numero}</div>}
                      </td>
                      <td style={{ padding: "10px 12px", color: "#475569", fontSize: 12 }}>
                        <div>{fmtDate(cot.fecha_creacion)}</div>
                        {cot.fecha_venta && <div style={{ fontSize: 10, color: "#94A3B8" }}>Vendida {fmtDate(cot.fecha_venta)}</div>}
                      </td>
                      <td style={{ padding: "10px 12px" }}>
                        <div style={{ fontWeight: 500, color: "#1E293B" }}>{cot.cliente?.nombre || "—"}</div>
                        {cot.cliente?.rut && <div style={{ fontSize: 11, color: "#94A3B8" }}>{cot.cliente.rut}</div>}
                      </td>
                      <td style={{ padding: "10px 12px" }}>
                        <div style={{ color: "#475569" }}>{cot.vehiculo || "Sin definir"}</div>
                        {lc && cot.linea && (
                          <span style={{ fontSize: 10, background: lc.bg, color: lc.color, padding: "1px 7px", borderRadius: 8, fontWeight: 600 }}>
                            {cot.linea.charAt(0).toUpperCase() + cot.linea.slice(1)}
                          </span>
                        )}
                      </td>
                      <td style={{ padding: "10px 12px", textAlign: "center" }}>
                        <span style={{ background: "#F1F5F9", borderRadius: 10, padding: "2px 10px", fontSize: 12, fontWeight: 600, color: "#475569" }}>{cot.items_count}</span>
                      </td>
                      <td style={{ padding: "10px 12px", textAlign: "right", fontWeight: 600, color: "#1E293B" }}>{fmt(cot.total_bruto)}</td>
                      <td style={{ padding: "10px 12px" }}>
                        <select
                          value={cot.estado}
                          onChange={(e) => handleEstado(cot.id, e.target.value)}
                          disabled={updatingEstado === cot.id}
                          style={{ padding: "4px 8px", border: `1px solid ${ec.color}40`, borderRadius: 8, background: ec.bg, color: ec.color, fontSize: 12, fontWeight: 600, cursor: "pointer" }}>
                          {Object.entries(ESTADO_CONFIG).map(([v, c]) => (
                            <option key={v} value={v}>{c.label}</option>
                          ))}
                        </select>
                      </td>
                      <td style={{ padding: "10px 12px", color: "#475569", fontSize: 12 }}>{cot.asesor || "—"}</td>
                      <td style={{ padding: "10px 12px", textAlign: "center" }}>
                        <div style={{ display: "flex", gap: 6, justifyContent: "center", alignItems: "center" }}>
                          {cot.estado !== "vendida" && (
                            <button
                              onClick={() => handleMarcarVendida(cot)}
                              disabled={markingVendida === cot.id}
                              title="Marcar como vendida"
                              style={{ background: "transparent", border: "1px solid #16A34A40", borderRadius: 6, cursor: "pointer", color: "#16A34A", padding: "5px 8px", fontSize: 11, fontWeight: 600, display: "flex", alignItems: "center", gap: 4, opacity: markingVendida === cot.id ? 0.5 : 1 }}>
                              <CheckCircle size={12} /> Vendida
                            </button>
                          )}
                          <button
                            onClick={() => handleDownload(cot)}
                            disabled={downloading === cot.id}
                            title="Descargar PDF"
                            style={{ background: "transparent", border: "1px solid #E2E8F0", borderRadius: 6, cursor: "pointer", color: "#475569", padding: "5px 8px", opacity: downloading === cot.id ? 0.5 : 1 }}>
                            <Download size={13} />
                          </button>
                        </div>
                      </td>
                    </tr>
                  );

                  const expandedRow = isExpanded ? (
                    <tr key={`exp-${cot.id}`} style={{ borderBottom: "1px solid #E2E8F0" }}>
                      <td colSpan={10} style={{ padding: 0, background: "#F8FAFC" }}>
                        {loadingDetail === cot.id ? (
                          <div style={{ padding: "20px 24px", color: "#94A3B8", fontSize: 13 }}>Cargando detalle...</div>
                        ) : det ? (
                          <div style={{ padding: "16px 24px 20px" }}>
                            {/* Meta info */}
                            <div style={{ display: "flex", gap: 24, fontSize: 12, color: "#475569", marginBottom: 12, flexWrap: "wrap" }}>
                              {det.vehiculo && <span><strong style={{ color: "var(--monza-accent)" }}>Vehículo:</strong> {det.vehiculo}</span>}
                              {det.vin && <span><strong style={{ color: "var(--monza-accent)" }}>VIN:</strong> {det.vin}</span>}
                              {det.anio && <span><strong style={{ color: "var(--monza-accent)" }}>Año:</strong> {det.anio}</span>}
                              {det.forma_pago && <span><strong style={{ color: "var(--monza-accent)" }}>Pago:</strong> {det.forma_pago}</span>}
                              {det.tipo_cotizacion && <span><strong style={{ color: "var(--monza-accent)" }}>Tipo:</strong> {det.tipo_cotizacion}</span>}
                            </div>
                            {/* Items table */}
                            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12, border: "1px solid #E2E8F0", borderRadius: 8, overflow: "hidden" }}>
                              <thead>
                                <tr style={{ background: "#F5CBA7" }}>
                                  {["Repuesto", "Marca", "Calidad", "QTY", "Precio Unit.", "Total", "Plazo"].map((h) => (
                                    <th key={h} style={{ padding: "7px 10px", textAlign: ["QTY", "Precio Unit.", "Total"].includes(h) ? "right" : "left", fontWeight: 700, color: "#1E293B", fontSize: 11 }}>{h}</th>
                                  ))}
                                </tr>
                              </thead>
                              <tbody>
                                {det.items.map((it, idx) => (
                                  <tr key={it.id} style={{ background: idx % 2 === 0 ? "white" : "#FAFAFA", borderTop: "1px solid #F1F5F9" }}>
                                    <td style={{ padding: "7px 10px", color: "#1E293B", fontWeight: 500 }}>{it.descripcion}</td>
                                    <td style={{ padding: "7px 10px", color: "#475569" }}>{it.marca || "—"}</td>
                                    <td style={{ padding: "7px 10px", color: "#475569" }}>{CALIDAD_LABEL[it.calidad || ""] || it.calidad || "—"}</td>
                                    <td style={{ padding: "7px 10px", textAlign: "right", color: "#475569" }}>{it.cantidad}</td>
                                    <td style={{ padding: "7px 10px", textAlign: "right", color: "#475569" }}>{it.precio_unitario_clp ? fmt(it.precio_unitario_clp) : "—"}</td>
                                    <td style={{ padding: "7px 10px", textAlign: "right", fontWeight: 600, color: "#1E293B" }}>{it.subtotal_clp ? fmt(it.subtotal_clp) : "—"}</td>
                                    <td style={{ padding: "7px 10px", color: "#475569" }}>{it.plazo_entrega || "—"}</td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                            {/* Totals + note */}
                            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginTop: 12, gap: 16 }}>
                              {det.condiciones_servicio && (
                                <div style={{ flex: 1, background: "#FFFBEB", border: "1px solid #FDE68A", borderRadius: 8, padding: "10px 14px", fontSize: 12, color: "#78350F" }}>
                                  <strong>Condiciones:</strong> {det.condiciones_servicio}
                                </div>
                              )}
                              {(det.total_neto || det.total_bruto) && (
                                <div style={{ textAlign: "right", fontSize: 12, color: "#475569", minWidth: 180 }}>
                                  {det.total_neto && <div>Neto: <strong>{fmt(det.total_neto)}</strong></div>}
                                  {det.iva_monto && <div>IVA: <strong>{fmt(det.iva_monto)}</strong></div>}
                                  {det.total_bruto && <div style={{ fontSize: 14, fontWeight: 700, color: "#1E293B", marginTop: 4 }}>Total: {fmt(det.total_bruto)}</div>}
                                </div>
                              )}
                            </div>
                          </div>
                        ) : null}
                      </td>
                    </tr>
                  ) : null;

                  return [mainRow, expandedRow].filter(Boolean) as React.ReactElement[];
                })
            }
          </tbody>
        </table>
        {total > 0 && (
          <div style={{ padding: "10px 16px", borderTop: "1px solid #F1F5F9", fontSize: 12, color: "#94A3B8" }}>
            Mostrando {items.length} de {total}
          </div>
        )}
      </div>
    </div>
  );
}
