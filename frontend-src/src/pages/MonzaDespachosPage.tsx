import { useState, useEffect, useCallback, useRef } from "react";
import { Search, Truck, RefreshCw, ChevronDown, ChevronRight, Upload, Download, FileText, CheckCircle } from "lucide-react";
import { monzaDespachosAPI, monzaCotizacionesAPI } from "../services/monzaApi";
import { useMonzaTheme } from "./MonzaLayout";
import toast from "react-hot-toast";

interface Despacho {
  id: number; numero: string; estado: string;
  vehiculo?: string; vin?: string; anio?: string;
  linea?: string; oc_cliente?: string;
  numero_factura?: string; tipo_documento?: string; tiene_documento: boolean;
  fecha_venta?: string; fecha_despacho?: string;
  total_bruto: number; items_count: number; asesor?: string;
  fecha_creacion: string;
  cliente?: { nombre: string; rut?: string };
  lead_numero?: string;
}
interface KPIs { total_despachados: number; despachados_mes: number; monto_mes: number; sin_documento: number; }
interface CotItem { id: number; descripcion: string; numero_parte?: string; marca?: string; cantidad: number; precio_unitario_clp?: number; subtotal_clp?: number; plazo_entrega?: string; calidad?: string; }
interface CotDetail { items: CotItem[]; forma_pago?: string; vehiculo?: string; vin?: string; anio?: string; condiciones_servicio?: string; total_neto?: number; iva_monto?: number; total_bruto?: number; }

const TIPO_DOC = ["factura", "boleta", "ticket", "guía de despacho", "otro"];
const LINEA_CONFIG: Record<string, { bg: string; color: string }> = {
  autos: { bg: "#DBEAFE", color: "#1D4ED8" },
  maquinaria: { bg: "#FEF3C7", color: "#D97706" },
};

function fmt(n: number) { return n > 0 ? `$${n.toLocaleString("es-CL")}` : "$0"; }
function fmtDate(d?: string) { return d ? new Date(d + "T00:00:00").toLocaleDateString("es-CL") : "—"; }

function KpiCard({ label, value, sub, accent }: { label: string; value: string | number; sub?: string; accent: string }) {
  const { dark } = useMonzaTheme();
  return (
    <div style={{ background: dark ? "#131b3e" : "white", borderRadius: 12, border: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}`, padding: "18px 20px", flex: 1, minWidth: 160 }}>
      <div style={{ width: 28, height: 4, borderRadius: 2, background: accent, marginBottom: 12 }} />
      <div style={{ fontSize: 26, fontWeight: 800, color: dark ? "white" : "#1E293B", lineHeight: 1 }}>{value}</div>
      <div style={{ fontSize: 12, color: dark ? "#8899cc" : "#64748B", marginTop: 5 }}>{label}</div>
      {sub && <div style={{ fontSize: 11, color: dark ? "#475569" : "#94A3B8", marginTop: 2 }}>{sub}</div>}
    </div>
  );
}

function UploadModal({ cotId, cotNumero, onClose, onUploaded }: { cotId: number; cotNumero: string; onClose: () => void; onUploaded: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [tipo, setTipo] = useState("factura");
  const [uploading, setUploading] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const handleUpload = async () => {
    if (!file) { toast.error("Selecciona un archivo"); return; }
    setUploading(true);
    try {
      await monzaDespachosAPI.uploadDocumento(cotId, file, tipo);
      toast.success("Documento subido correctamente");
      onUploaded();
      onClose();
    } catch { toast.error("Error al subir el archivo"); }
    finally { setUploading(false); }
  };

  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)", zIndex: 1100, display: "flex", alignItems: "center", justifyContent: "center", padding: 16 }}>
      <div style={{ background: "white", borderRadius: 14, width: "100%", maxWidth: 460, boxShadow: "0 24px 60px rgba(0,0,0,0.35)", overflow: "hidden" }}>
        <div style={{ background: "#1E293B", padding: "16px 20px", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <Upload size={16} color="#F59E0B" />
            <span style={{ color: "white", fontWeight: 700, fontSize: 14 }}>Subir documento — {cotNumero}</span>
          </div>
          <button onClick={onClose} style={{ background: "transparent", border: "none", color: "#94A3B8", cursor: "pointer", fontSize: 18 }}>×</button>
        </div>
        <div style={{ padding: 20 }}>
          <div style={{ marginBottom: 14 }}>
            <label style={{ fontSize: 11, fontWeight: 600, color: "#64748B", display: "block", marginBottom: 5 }}>Tipo de documento</label>
            <select value={tipo} onChange={(e) => setTipo(e.target.value)}
              style={{ width: "100%", padding: "8px 10px", border: "1px solid #E2E8F0", borderRadius: 6, fontSize: 13, background: "white", color: "#1E293B" }}>
              {TIPO_DOC.map((t) => <option key={t} value={t}>{t.charAt(0).toUpperCase() + t.slice(1)}</option>)}
            </select>
          </div>
          <div
            onClick={() => inputRef.current?.click()}
            style={{ border: "2px dashed #E2E8F0", borderRadius: 10, padding: "28px 20px", textAlign: "center", cursor: "pointer", background: file ? "#F0FDF4" : "#FAFAFA", borderColor: file ? "#16A34A" : "#E2E8F0" }}>
            <input ref={inputRef} type="file" accept=".pdf,.jpg,.jpeg,.png,.webp" style={{ display: "none" }}
              onChange={(e) => setFile(e.target.files?.[0] || null)} />
            {file ? (
              <>
                <CheckCircle size={28} color="#16A34A" style={{ margin: "0 auto 8px" }} />
                <div style={{ fontSize: 13, fontWeight: 600, color: "#15803D" }}>{file.name}</div>
                <div style={{ fontSize: 11, color: "#64748B", marginTop: 2 }}>{(file.size / 1024).toFixed(0)} KB</div>
              </>
            ) : (
              <>
                <Upload size={28} color="#94A3B8" style={{ margin: "0 auto 8px" }} />
                <div style={{ fontSize: 13, color: "#64748B" }}>Haz clic para seleccionar</div>
                <div style={{ fontSize: 11, color: "#94A3B8", marginTop: 2 }}>PDF, JPG, PNG — máx. 10 MB</div>
              </>
            )}
          </div>
          <div style={{ display: "flex", gap: 10, marginTop: 18, justifyContent: "flex-end" }}>
            <button onClick={onClose} style={{ padding: "9px 18px", border: "1px solid #E2E8F0", borderRadius: 8, background: "white", cursor: "pointer", fontSize: 13, color: "#475569" }}>Cancelar</button>
            <button onClick={handleUpload} disabled={uploading || !file}
              style={{ padding: "9px 18px", border: "none", borderRadius: 8, background: uploading || !file ? "#94A3B8" : "var(--monza-accent)", cursor: uploading || !file ? "not-allowed" : "pointer", fontSize: 13, color: "white", fontWeight: 700, display: "flex", alignItems: "center", gap: 6 }}>
              <Upload size={13} />{uploading ? "Subiendo..." : "Subir documento"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function MonzaDespachosPage() {
  const { dark } = useMonzaTheme();
  const [items, setItems] = useState<Despacho[]>([]);
  const [total, setTotal] = useState(0);
  const [kpis, setKpis] = useState<KPIs | null>(null);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState("");
  const [desde, setDesde] = useState("");
  const [hasta, setHasta] = useState("");
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [details, setDetails] = useState<Record<number, CotDetail>>({});
  const [loadingDetail, setLoadingDetail] = useState<number | null>(null);
  const [uploadModal, setUploadModal] = useState<{ id: number; numero: string } | null>(null);

  const bg = dark ? "#131b3e" : "white";
  const bd = dark ? "#1e2a4a" : "#E2E8F0";
  const txt = dark ? "white" : "#1E293B";
  const sub = dark ? "#8899cc" : "#64748B";

  const fetchAll = useCallback(async () => {
    setLoading(true);
    try {
      const [listRes, kpisRes] = await Promise.all([
        monzaDespachosAPI.list({ q: q || undefined, desde: desde || undefined, hasta: hasta || undefined }),
        monzaDespachosAPI.kpis(),
      ]);
      setItems(listRes.data.items);
      setTotal(listRes.data.total);
      setKpis(kpisRes.data);
    } catch { toast.error("Error al cargar despachos"); }
    finally { setLoading(false); }
  }, [q, desde, hasta]);

  useEffect(() => { fetchAll(); }, [fetchAll]);

  const toggleExpand = async (id: number) => {
    const next = new Set(expanded);
    if (next.has(id)) { next.delete(id); setExpanded(next); return; }
    next.add(id); setExpanded(next);
    if (!details[id]) {
      setLoadingDetail(id);
      try {
        const r = await monzaCotizacionesAPI.get(id);
        setDetails((prev) => ({ ...prev, [id]: r.data }));
      } catch { toast.error("Error al cargar detalle"); }
      finally { setLoadingDetail(null); }
    }
  };

  const handleDownload = async (cot: Despacho) => {
    try {
      const r = await monzaDespachosAPI.downloadDocumento(cot.id);
      const blob = new Blob([r.data]);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `doc_${cot.numero}`;
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch { toast.error("Error al descargar documento"); }
  };

  const CALIDAD_LABEL: Record<string, string> = { sin_calificar: "—", genuine: "Genuine", oem: "OEM", aftermarket: "Aftermarket" };

  return (
    <div>
      {uploadModal && (
        <UploadModal
          cotId={uploadModal.id}
          cotNumero={uploadModal.numero}
          onClose={() => setUploadModal(null)}
          onUploaded={() => { setUploadModal(null); fetchAll(); setDetails({}); }}
        />
      )}

      {/* Header */}
      <div style={{ marginBottom: 20 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
          <Truck size={22} className="monza-ic" />
          <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: txt }}>Histórico de Despachos</h1>
        </div>
        <p style={{ margin: 0, fontSize: 13, color: sub }}>
          Ventas finalizadas y despachadas al cliente. Aquí puedes cargar facturas, boletas y tickets de despacho.
        </p>
      </div>

      {/* KPIs */}
      {kpis && (
        <div style={{ display: "flex", gap: 12, marginBottom: 20, flexWrap: "wrap" }}>
          <KpiCard label="Total despachados" accent="var(--monza-accent)" value={kpis.total_despachados} />
          <KpiCard label="Despachados este mes" accent="#10B981" value={kpis.despachados_mes} />
          <KpiCard label="Monto despachado mes" accent="#F59E0B" value={fmt(kpis.monto_mes)} />
          <KpiCard label="Sin documento" accent="#EF4444" value={kpis.sin_documento}
            sub={kpis.sin_documento > 0 ? "Pendiente de cargar" : "✓ Todos documentados"} />
        </div>
      )}

      {/* Filters */}
      <div style={{ background: bg, border: `1px solid ${bd}`, borderRadius: 10, padding: "14px 16px", marginBottom: 16 }}>
        <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <div style={{ position: "relative", flex: 1, minWidth: 280 }}>
            <Search size={14} style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)", color: "#94A3B8" }} />
            <input value={q} onChange={(e) => setQ(e.target.value)}
              placeholder="Cliente, N° COT, N° factura, vehículo..."
              style={{ width: "100%", padding: "8px 10px 8px 32px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 13, boxSizing: "border-box" as const, background: dark ? "#0d1321" : "white", color: txt }} />
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: sub }}>
            Despacho desde:
            <input type="date" value={desde} onChange={(e) => setDesde(e.target.value)}
              style={{ padding: "6px 8px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 12, background: dark ? "#0d1321" : "white", color: txt, colorScheme: dark ? "dark" : "light" as const }} />
            hasta:
            <input type="date" value={hasta} onChange={(e) => setHasta(e.target.value)}
              style={{ padding: "6px 8px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 12, background: dark ? "#0d1321" : "white", color: txt, colorScheme: dark ? "dark" : "light" as const }} />
          </div>
          <button onClick={fetchAll} style={{ padding: "7px 10px", border: `1px solid ${bd}`, borderRadius: 6, background: bg, cursor: "pointer", color: sub }}>
            <RefreshCw size={13} />
          </button>
        </div>
      </div>

      {/* Table */}
      <div style={{ background: bg, border: `1px solid ${bd}`, borderRadius: 10, overflow: "hidden" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
          <thead>
            <tr style={{ background: dark ? "#0d1321" : "#F8FAFC", borderBottom: `1px solid ${bd}` }}>
              <th style={{ width: 32, padding: "10px 8px" }} />
              {["N° COT", "Fecha Despacho", "Cliente", "Vehículo", "N° Factura/Doc", "Monto", "Asesor", "Documento", ""].map((h, i) => (
                <th key={i} style={{ padding: "10px 12px", textAlign: h === "Monto" ? "right" : "left", fontWeight: 600, fontSize: 11, color: sub, textTransform: "uppercase" as const, letterSpacing: 0.5 }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading
              ? <tr><td colSpan={10} style={{ padding: 40, textAlign: "center", color: "#94A3B8" }}>Cargando...</td></tr>
              : items.length === 0
                ? <tr><td colSpan={10} style={{ padding: 48, textAlign: "center", color: "#94A3B8" }}>
                    <Truck size={32} color="#E2E8F0" style={{ display: "block", margin: "0 auto 8px" }} />
                    No hay despachos registrados aún.
                  </td></tr>
                : items.flatMap((d) => {
                  const lc = d.linea ? LINEA_CONFIG[d.linea] : null;
                  const isExpanded = expanded.has(d.id);
                  const det = details[d.id];

                  const mainRow = (
                    <tr key={d.id} style={{ borderBottom: isExpanded ? "none" : `1px solid ${dark ? "#1e2a4a" : "#F1F5F9"}`, background: isExpanded ? (dark ? "#0d1321" : "#FAFFFE") : bg }}>
                      <td style={{ padding: "10px 8px", textAlign: "center" }}>
                        <button onClick={() => toggleExpand(d.id)}
                          style={{ background: "transparent", border: "none", cursor: "pointer", color: "#94A3B8", padding: 2 }}>
                          {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                        </button>
                      </td>
                      <td style={{ padding: "10px 12px" }}>
                        <div style={{ fontWeight: 700, fontSize: 12, color: "var(--monza-accent)" }}>{d.numero}</div>
                        {d.lead_numero && <div style={{ fontSize: 10, color: "#94A3B8" }}>Lead {d.lead_numero}</div>}
                        {lc && d.linea && <span style={{ fontSize: 10, background: lc.bg, color: lc.color, padding: "1px 6px", borderRadius: 6, fontWeight: 600 }}>{d.linea}</span>}
                      </td>
                      <td style={{ padding: "10px 12px" }}>
                        <div style={{ fontWeight: 600, color: txt }}>{fmtDate(d.fecha_despacho)}</div>
                        {d.fecha_venta && <div style={{ fontSize: 11, color: sub }}>Vendida {fmtDate(d.fecha_venta)}</div>}
                      </td>
                      <td style={{ padding: "10px 12px" }}>
                        <div style={{ fontWeight: 500, color: txt }}>{d.cliente?.nombre || "—"}</div>
                        {d.cliente?.rut && <div style={{ fontSize: 11, color: sub }}>{d.cliente.rut}</div>}
                      </td>
                      <td style={{ padding: "10px 12px", color: sub, fontSize: 12 }}>
                        {d.vehiculo || "—"}
                        {d.vin && <div style={{ fontSize: 10, color: "#94A3B8" }}>VIN: {d.vin}</div>}
                      </td>
                      <td style={{ padding: "10px 12px" }}>
                        {d.numero_factura ? (
                          <div>
                            <div style={{ fontWeight: 600, color: txt, fontSize: 12 }}>{d.numero_factura}</div>
                            {d.tipo_documento && <div style={{ fontSize: 10, color: sub }}>{d.tipo_documento.charAt(0).toUpperCase() + d.tipo_documento.slice(1)}</div>}
                          </div>
                        ) : <span style={{ color: "#94A3B8", fontSize: 12 }}>Sin número</span>}
                      </td>
                      <td style={{ padding: "10px 12px", textAlign: "right", fontWeight: 700, color: txt }}>{fmt(d.total_bruto)}</td>
                      <td style={{ padding: "10px 12px", color: sub, fontSize: 12 }}>{d.asesor || "—"}</td>
                      <td style={{ padding: "10px 12px" }}>
                        {d.tiene_documento ? (
                          <span style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: 11, background: "#DCFCE7", color: "#15803D", padding: "3px 8px", borderRadius: 8, fontWeight: 600 }}>
                            <CheckCircle size={11} /> Con doc.
                          </span>
                        ) : (
                          <span style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: 11, background: "#FEF3C7", color: "#D97706", padding: "3px 8px", borderRadius: 8, fontWeight: 600 }}>
                            ⚠ Sin doc.
                          </span>
                        )}
                      </td>
                      <td style={{ padding: "10px 8px" }}>
                        <div style={{ display: "flex", gap: 5, alignItems: "center" }}>
                          <button onClick={() => setUploadModal({ id: d.id, numero: d.numero })}
                            title="Subir documento"
                            style={{ background: "transparent", border: `1px solid ${dark ? "#334155" : "#E2E8F0"}`, borderRadius: 6, cursor: "pointer", color: "#F59E0B", padding: "5px 7px" }}>
                            <Upload size={12} />
                          </button>
                          {d.tiene_documento && (
                            <button onClick={() => handleDownload(d)}
                              title="Descargar documento"
                              style={{ background: "transparent", border: `1px solid ${dark ? "#334155" : "#E2E8F0"}`, borderRadius: 6, cursor: "pointer", color: "#0EA5E9", padding: "5px 7px" }}>
                              <Download size={12} />
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  );

                  const expandedRow = isExpanded ? (
                    <tr key={`exp-${d.id}`} style={{ borderBottom: `1px solid ${bd}` }}>
                      <td colSpan={10} style={{ padding: 0, background: dark ? "#0a0e1f" : "#F8FFFE" }}>
                        {loadingDetail === d.id ? (
                          <div style={{ padding: "20px 28px", color: "#94A3B8", fontSize: 13 }}>Cargando detalle...</div>
                        ) : det ? (
                          <div style={{ padding: "16px 28px 20px" }}>
                            <div style={{ display: "flex", gap: 24, fontSize: 12, color: sub, marginBottom: 12, flexWrap: "wrap" }}>
                              {det.vehiculo && <span><strong style={{ color: "var(--monza-accent)" }}>Vehículo:</strong> {det.vehiculo}</span>}
                              {(det as { vin?: string }).vin && <span><strong style={{ color: "var(--monza-accent)" }}>VIN:</strong> {(det as { vin?: string }).vin}</span>}
                              {det.forma_pago && <span><strong style={{ color: "var(--monza-accent)" }}>Pago:</strong> {det.forma_pago}</span>}
                            </div>
                            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12, border: `1px solid ${bd}`, borderRadius: 8, overflow: "hidden" }}>
                              <thead>
                                <tr style={{ background: dark ? "#131b3e" : "#FDF2E9" }}>
                                  {["Repuesto", "Marca", "Calidad", "QTY", "Precio Unit.", "Total", "Plazo"].map((h) => (
                                    <th key={h} style={{ padding: "7px 10px", textAlign: ["QTY", "Precio Unit.", "Total"].includes(h) ? "right" : "left", fontWeight: 700, color: dark ? "#F5CBA7" : "#92400E", fontSize: 11 }}>{h}</th>
                                  ))}
                                </tr>
                              </thead>
                              <tbody>
                                {det.items.map((it, idx) => (
                                  <tr key={it.id} style={{ background: idx % 2 === 0 ? (dark ? "#131b3e" : "white") : (dark ? "#0d1321" : "#FAFAFA"), borderTop: `1px solid ${bd}` }}>
                                    <td style={{ padding: "7px 10px", color: txt, fontWeight: 500 }}>{it.descripcion}</td>
                                    <td style={{ padding: "7px 10px", color: sub }}>{it.marca || "—"}</td>
                                    <td style={{ padding: "7px 10px", color: sub }}>{CALIDAD_LABEL[it.calidad || ""] || it.calidad || "—"}</td>
                                    <td style={{ padding: "7px 10px", textAlign: "right", color: sub }}>{it.cantidad}</td>
                                    <td style={{ padding: "7px 10px", textAlign: "right", color: sub }}>{it.precio_unitario_clp ? fmt(it.precio_unitario_clp) : "—"}</td>
                                    <td style={{ padding: "7px 10px", textAlign: "right", fontWeight: 600, color: txt }}>{it.subtotal_clp ? fmt(it.subtotal_clp) : "—"}</td>
                                    <td style={{ padding: "7px 10px", color: sub }}>{it.plazo_entrega || "—"}</td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                            <div style={{ display: "flex", justifyContent: "space-between", marginTop: 10, gap: 16 }}>
                              {det.condiciones_servicio && (
                                <div style={{ flex: 1, background: dark ? "#1a1a2e" : "#FFFBEB", border: `1px solid ${dark ? "#2a3a5e" : "#FDE68A"}`, borderRadius: 8, padding: "8px 12px", fontSize: 12, color: dark ? "#d4b896" : "#78350F" }}>
                                  <strong>Condiciones:</strong> {det.condiciones_servicio}
                                </div>
                              )}
                              {det.total_bruto && (
                                <div style={{ textAlign: "right", fontSize: 12, color: sub, minWidth: 160 }}>
                                  {det.total_neto && <div>Neto: <strong style={{ color: txt }}>{fmt(det.total_neto)}</strong></div>}
                                  {det.iva_monto && <div>IVA: <strong style={{ color: txt }}>{fmt(det.iva_monto)}</strong></div>}
                                  <div style={{ fontSize: 14, fontWeight: 700, color: txt, marginTop: 4 }}>Total: {fmt(det.total_bruto)}</div>
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
          <div style={{ padding: "10px 16px", borderTop: `1px solid ${bd}`, fontSize: 12, color: "#94A3B8" }}>
            {total} despacho{total !== 1 ? "s" : ""} en total
          </div>
        )}
      </div>
    </div>
  );
}
