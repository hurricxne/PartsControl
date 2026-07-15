// Página "Ventas — Contabilidad" (MonzaParts): lista las VENTAS agrupadas por cotización
// vendida/despachada (solo lectura) y, al expandir, muestra ítems, guías y facturas.
// Consume monzaContabilidadAPI. El alta de facturas vive en MonzaFacturasPage.
import { useState, useEffect, useCallback } from "react";
import { fmtClp } from "../utils/format";
import {
  TrendingUp, Search, DollarSign, CreditCard, CheckCircle2, AlertCircle,
  Loader2, RefreshCw, ChevronDown, ChevronUp, Receipt, Truck, Clock, X,
} from "lucide-react";
import toast from "react-hot-toast";
import { useMonzaTheme } from "./MonzaLayout";
import { monzaContabilidadAPI } from "../services/monzaApi";
import { ADELANTO_PCT_DEFECTO } from "../constants/adelanto";

// ─── Tipos ────────────────────────────────────────────────────────────────────
interface VentaRow {
  cotizacion_id: number;
  numero_cotizacion: string;
  cliente: string;
  rut_cliente: string;
  oc_cliente: string | null;
  vehiculo: string | null;
  estado: string;
  fecha_venta: string | null;
  fecha_creacion: string | null;
  cond_pago: string | null;
  total_items: number;
  total_neto_clp: number;
  iva_clp: number;
  total_con_iva_clp: number;
  n_facturas: number;
  facturado_clp: number;
  cobrado_clp: number;
  por_cobrar_clp: number;
  estado_cobranza: string;
  // Adelanto (50%): informado por Comercial, verificado por Contabilidad
  requiere_adelanto?: boolean;
  pct_adelanto?: number;
  estado_adelanto?: "no_aplica" | "por_verificar" | "verificado";
}
interface GuiaRef { numero_guia: string | null; numero_despacho: string; estado: string; qty_despachada: number; guia_firmada?: boolean; despacho_id?: number; guia_firmada_archivo?: string | null }
interface FacturaRef { factura_id: number; numero_factura: string | null; fecha_vencimiento: string | null; plazo_dias: number | null; estado_pago: string }
interface VentaItem {
  id: number; numero_parte: string; descripcion: string; marca: string;
  cantidad: number; precio_unit_venta_clp: number; total_venta_clp: number;
  estado_linea: string; guias: GuiaRef[]; facturas: FacturaRef[];
}
interface VentaDetalle {
  cotizacion_id: number; numero_cotizacion: string; cliente: string; rut_cliente: string;
  oc_cliente: string | null; vehiculo: string | null; cond_pago: string | null;
  total_neto_clp: number; iva_clp: number; total_con_iva_clp: number; items: VentaItem[];
}
interface Kpis {
  facturado_clp: number; cobrado_clp: number; cobrado_cliente_clp?: number;
  anticipo_factoring_clp?: number; por_cobrar_clp: number; vencido_clp: number; en_factoring_clp: number;
}
// Fechas puras 'YYYY-MM-DD' se parsean como fecha LOCAL: new Date('YYYY-MM-DD') las
// interpreta en UTC y en Chile (UTC-4/-3) quedarían corridas un día hacia atrás.
const fmtDate = (d?: string | null) => {
  if (!d) return "—";
  const dt = /^\d{4}-\d{2}-\d{2}$/.test(d) ? new Date(d + "T00:00:00") : new Date(d);
  return isNaN(dt.getTime()) ? d : dt.toLocaleDateString("es-CL");
};

const COBRANZA_BADGE: Record<string, { bg: string; color: string; label: string }> = {
  sin_factura: { bg: "#F1F5F9", color: "#64748B", label: "Sin factura" },
  por_cobrar:  { bg: "#DBEAFE", color: "#1D4ED8", label: "Por cobrar" },
  parcial:     { bg: "#FEF3C7", color: "#B45309", label: "Pago parcial" },
  cobrada:     { bg: "#DCFCE7", color: "#15803D", label: "Cobrada" },
  vencida:     { bg: "#FEE2E2", color: "#B91C1C", label: "Vencida" },
};
const PAGO_BADGE: Record<string, { bg: string; color: string; label: string }> = {
  por_cobrar:  { bg: "#DBEAFE", color: "#1D4ED8", label: "Por cobrar" },
  parcial:     { bg: "#FEF3C7", color: "#B45309", label: "Parcial" },
  pagada:      { bg: "#DCFCE7", color: "#15803D", label: "Pagada" },
  vencida:     { bg: "#FEE2E2", color: "#B91C1C", label: "Vencida" },
  factorizada: { bg: "#EDE9FE", color: "#6D28D9", label: "Factoring" },
};
const PERIODO_LABELS: Record<string, string> = { "": "Todo", semana: "Semana", mes: "Mes", anio: "Año" };

function Badge({ map, estado }: { map: Record<string, { bg: string; color: string; label: string }>; estado: string }) {
  const m = map[estado] ?? { bg: "#F1F5F9", color: "#64748B", label: estado };
  return <span style={{ background: m.bg, color: m.color, fontSize: 11, fontWeight: 600, padding: "2px 8px", borderRadius: 999, whiteSpace: "nowrap" }}>{m.label}</span>;
}

// ─── Adelanto: la ORDEN la da Tesorería ────────────────────────────────────────
// La verificación/aprobación del adelanto (50%) se movió a Contabilidad → Tesorería
// (pestaña Aprobaciones, POST /api/monza/tesoreria/aprobaciones/{cot}/aprobar).
// Acá la venta solo MUESTRA el estado (badge solo lectura, ver VentaCard).

// ─── Tarjeta de venta (por cotización, expandible) ────────────────────────────
function VentaCard({ venta, onChanged }: { venta: VentaRow; onChanged: () => void }) {
  const { dark } = useMonzaTheme();
  const [open, setOpen] = useState(false);
  const [detalle, setDetalle] = useState<VentaDetalle | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(false);

  const card = { background: dark ? "#131b3e" : "white", border: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}`, borderRadius: 14, overflow: "hidden" } as React.CSSProperties;
  const muted = dark ? "#94A3B8" : "#64748B";
  const text = dark ? "#E2E8F0" : "#0f172a";
  const sub = dark ? "#131b3e" : "#F8FAFC";

  const fetchDetalle = async () => {
    setLoading(true); setErr(false);
    try { const { data } = await monzaContabilidadAPI.ventaDetalle(venta.cotizacion_id); setDetalle(data); }
    catch { setErr(true); } finally { setLoading(false); }
  };
  const toggle = async () => { const next = !open; setOpen(next); if (next && !detalle) await fetchDetalle(); };
  const toggleFirma = async (despachoId: number, firmada: boolean) => {
    try { await monzaContabilidadAPI.marcarGuiaFirmada(despachoId, { firmada }); await fetchDetalle(); }
    catch { /* la firma es opcional: si falla, no bloquea nada */ }
  };
  // Guías distintas de la venta (deduplicadas por despacho) para registrar la firma.
  const guiasVenta: GuiaRef[] = detalle
    ? Array.from(new Map(detalle.items.flatMap(it => it.guias).filter(g => g.despacho_id != null).map(g => [g.despacho_id, g])).values())
    : [];

  return (
    <div style={card}>
      <button onClick={toggle} style={{ width: "100%", textAlign: "left", padding: "14px 18px", display: "flex", gap: 12, alignItems: "center", background: "transparent", border: "none", cursor: "pointer", fontFamily: "inherit" }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", marginBottom: 4 }}>
            <span style={{ fontWeight: 700, fontSize: 13, color: "var(--monza-accent)" }}>COT {venta.numero_cotizacion}</span>
            {venta.oc_cliente && <span style={{ fontSize: 10, color: muted, border: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}`, padding: "1px 6px", borderRadius: 999 }}>OC {venta.oc_cliente}</span>}
            <Badge map={COBRANZA_BADGE} estado={venta.estado_cobranza} />
            {venta.requiere_adelanto && (
              <span style={{
                background: venta.estado_adelanto === "verificado" ? "#DCFCE7" : "#FEE2E2",
                color: venta.estado_adelanto === "verificado" ? "#15803D" : "#B91C1C",
                fontSize: 11, fontWeight: 600, padding: "2px 8px", borderRadius: 999, whiteSpace: "nowrap",
              }}>
                {venta.estado_adelanto === "verificado"
                  ? `Adelanto ${venta.pct_adelanto || ADELANTO_PCT_DEFECTO}% aprobado`
                  : `Adelanto ${venta.pct_adelanto || ADELANTO_PCT_DEFECTO}% · por aprobar en Tesorería`}
              </span>
            )}
          </div>
          <p style={{ fontWeight: 600, fontSize: 14, color: text, margin: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{venta.cliente || "—"}</p>
          <p style={{ fontSize: 12, color: muted, margin: "2px 0 0" }}>
            {venta.rut_cliente && <span>{venta.rut_cliente} · </span>}
            {venta.vehiculo && <span>{venta.vehiculo} · </span>}
            {venta.cond_pago && <span>{venta.cond_pago} · </span>}
            <span>{venta.total_items} ítems · {venta.n_facturas} factura(s)</span>
          </p>
        </div>
        <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 2, flexShrink: 0 }}>
          <span style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: 1, color: muted }}>Total c/IVA</span>
          <span style={{ fontWeight: 700, fontSize: 15, color: text }}>{fmtClp(venta.total_con_iva_clp)}</span>
          <span style={{ fontSize: 12, fontWeight: 700, color: "#B45309" }}>Por cobrar {fmtClp(venta.por_cobrar_clp)}</span>
        </div>
        <span style={{ color: muted }}>{open ? <ChevronUp size={16} /> : <ChevronDown size={16} />}</span>
      </button>

      {/* La ORDEN del adelanto la da Tesorería (solo lectura acá). */}
      {venta.requiere_adelanto && venta.estado_adelanto === "por_verificar" && (
        <div style={{ padding: "10px 18px 12px", display: "flex", justifyContent: "flex-end", borderTop: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}` }}>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12, color: muted }}>
            <CheckCircle2 size={14} /> El pago del adelanto se aprueba en <b>Contabilidad → Tesorería</b>
          </span>
        </div>
      )}

      {open && (
        <div style={{ borderTop: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}` }}>
          {loading && <div style={{ display: "flex", justifyContent: "center", padding: 28 }}><Loader2 className="animate-spin" size={20} style={{ color: "var(--monza-accent)" }} /></div>}
          {!loading && err && (
            <div style={{ padding: 24, textAlign: "center", fontSize: 13, color: muted }}>
              No se pudo cargar el detalle. <button onClick={fetchDetalle} style={{ color: "var(--monza-accent)", textDecoration: "underline", background: "none", border: "none", cursor: "pointer" }}>Reintentar</button>
            </div>
          )}
          {!loading && detalle && (
            <>
              <div style={{ overflowX: "auto" }}>
                <table style={{ width: "100%", fontSize: 12, borderCollapse: "collapse" }}>
                  <thead>
                    <tr style={{ background: sub, borderBottom: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}` }}>
                      {["N° Parte", "Descripción", "Cant.", "P. Unit", "Total", "Guía", "Factura", "Estado pago"].map(h => (
                        <th key={h} style={{ textAlign: "left", padding: "8px 12px", fontSize: 10, textTransform: "uppercase", letterSpacing: 0.5, color: muted, whiteSpace: "nowrap" }}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {detalle.items.map((item, idx) => {
                      const estados = item.facturas.map(x => x.estado_pago);
                      const agg = !estados.length ? null : estados.every(e => e === estados[0]) ? estados[0] : estados.includes("vencida") ? "vencida" : "parcial";
                      return (
                        <tr key={item.id} style={{ background: idx % 2 ? sub : "transparent", borderBottom: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}` }}>
                          <td style={{ padding: "8px 12px", fontWeight: 600, color: text, whiteSpace: "nowrap" }}>{item.numero_parte || "—"}</td>
                          <td style={{ padding: "8px 12px", color: text, maxWidth: 200, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={item.descripcion}>{item.descripcion}</td>
                          <td style={{ padding: "8px 12px", textAlign: "right", color: muted }}>{item.cantidad}</td>
                          <td style={{ padding: "8px 12px", textAlign: "right", color: text, whiteSpace: "nowrap" }}>{fmtClp(item.precio_unit_venta_clp)}</td>
                          <td style={{ padding: "8px 12px", textAlign: "right", fontWeight: 700, color: "var(--monza-accent)", whiteSpace: "nowrap" }}>{fmtClp(item.total_venta_clp)}</td>
                          <td style={{ padding: "8px 12px", color: muted, whiteSpace: "nowrap" }}>
                            {item.guias.length ? <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}><Truck size={12} style={{ color: "#15803D" }} />{item.guias.map(g => g.numero_guia || g.numero_despacho).join(", ")}</span> : <span style={{ fontStyle: "italic", color: muted }}>sin despachar</span>}
                          </td>
                          <td style={{ padding: "8px 12px", color: muted, whiteSpace: "nowrap" }}>
                            {item.facturas.length ? <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}><Receipt size={12} style={{ color: "var(--monza-accent)" }} />{item.facturas.map(x => x.numero_factura || `#${x.factura_id}`).join(", ")}</span> : <span style={{ fontStyle: "italic", color: muted }}>sin facturar</span>}
                          </td>
                          <td style={{ padding: "8px 12px" }}>{agg ? <Badge map={PAGO_BADGE} estado={agg} /> : "—"}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              {guiasVenta.length > 0 && (
                <div style={{ padding: "10px 18px", display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center", borderTop: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}` }}>
                  <span style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: 0.5, color: muted }}>Guías (firma opcional):</span>
                  {guiasVenta.map(g => (
                    <button key={g.despacho_id} onClick={() => toggleFirma(g.despacho_id!, !g.guia_firmada)}
                      title={g.guia_firmada ? "Quitar marca de firmada" : "Marcar guía como firmada por el cliente"}
                      style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: 11, fontWeight: 600, padding: "3px 10px", borderRadius: 999, cursor: "pointer", fontFamily: "inherit",
                        border: `1px solid ${g.guia_firmada ? "#15803D" : (dark ? "#1e2a4a" : "#E2E8F0")}`,
                        background: g.guia_firmada ? "#DCFCE7" : "transparent",
                        color: g.guia_firmada ? "#15803D" : muted }}>
                      {g.numero_guia || g.numero_despacho} · {g.guia_firmada ? "firmada ✓" : "marcar firmada"}
                    </button>
                  ))}
                </div>
              )}
              <div style={{ padding: "10px 18px", display: "flex", flexWrap: "wrap", justifyContent: "flex-end", gap: 24, fontSize: 12, fontWeight: 600, background: sub, borderTop: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}` }}>
                <span style={{ color: muted }}>Neto: <b style={{ color: text }}>{fmtClp(detalle.total_neto_clp)}</b></span>
                <span style={{ color: muted }}>IVA: <b style={{ color: text }}>{fmtClp(detalle.iva_clp)}</b></span>
                <span style={{ color: muted }}>Total: <b style={{ color: "var(--monza-accent)" }}>{fmtClp(detalle.total_con_iva_clp)}</b></span>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}

// ─── Página ───────────────────────────────────────────────────────────────────
export default function MonzaVentasContabPage() {
  const { dark } = useMonzaTheme();
  const [ventas, setVentas] = useState<VentaRow[]>([]);
  const [kpis, setKpis] = useState<Kpis | null>(null);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState("");
  const [periodo, setPeriodo] = useState("");
  const [error, setError] = useState("");

  const muted = dark ? "#94A3B8" : "#64748B";
  const text = dark ? "#E2E8F0" : "#0f172a";
  const cardBg = dark ? "#131b3e" : "white";
  const cardBd = `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}`;

  const load = useCallback(async (search?: string, per?: string) => {
    setLoading(true); setError("");
    try {
      const [vRes, kRes] = await Promise.all([
        monzaContabilidadAPI.listVentas(search, per),
        monzaContabilidadAPI.kpis(per),
      ]);
      setVentas(vRes.data); setKpis(kRes.data);
    } catch { setError("No se pudieron cargar las ventas."); } finally { setLoading(false); }
  }, []);

  useEffect(() => { load(q || undefined, periodo || undefined); /* eslint-disable-next-line */ }, [periodo]);
  const handleSearch = (v: string) => { setQ(v); if (v.length === 0 || v.length >= 2) load(v || undefined, periodo || undefined); };

  const KPI_DEFS = kpis ? [
    { icon: DollarSign, label: "Facturado", value: fmtClp(kpis.facturado_clp), color: "var(--monza-accent)" },
    { icon: CheckCircle2, label: "Cobrado", value: fmtClp(kpis.cobrado_cliente_clp ?? kpis.cobrado_clp), color: "#15803D" },
    { icon: CreditCard, label: "Por cobrar", value: fmtClp(kpis.por_cobrar_clp), color: "#B45309" },
    { icon: AlertCircle, label: "Vencido", value: fmtClp(kpis.vencido_clp), color: "#B91C1C" },
    { icon: Clock, label: "En factoring", value: fmtClp(kpis.en_factoring_clp), color: "#6D28D9" },
    { icon: Receipt, label: "Anticipo factoring", value: fmtClp(kpis.anticipo_factoring_clp ?? 0), color: "#0369A1" },
  ] : [];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 16, flexWrap: "wrap" }}>
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 700, color: text, margin: 0 }}>Ventas — Contabilidad</h1>
          <p style={{ fontSize: 13, color: muted, margin: "4px 0 0" }}>Por cotización · despliega cada venta para ver ítems, guías y facturas</p>
        </div>
        <button onClick={() => load(q || undefined, periodo || undefined)} style={{ display: "flex", alignItems: "center", gap: 6, padding: "8px 12px", borderRadius: 8, border: cardBd, background: cardBg, color: muted, cursor: "pointer", fontFamily: "inherit" }}><RefreshCw size={16} /></button>
      </div>

      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        {Object.entries(PERIODO_LABELS).map(([key, label]) => (
          <button key={key} onClick={() => setPeriodo(key)}
            style={{ padding: "6px 16px", borderRadius: 999, fontSize: 13, fontWeight: 600, cursor: "pointer", fontFamily: "inherit",
              border: periodo === key ? "1px solid var(--monza-accent)" : cardBd,
              background: periodo === key ? "var(--monza-accent)" : cardBg,
              color: periodo === key ? "white" : muted }}>{label}</button>
        ))}
      </div>

      {kpis && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 12 }}>
          {KPI_DEFS.map(({ icon: Icon, label, value, color }) => (
            <div key={label} style={{ background: cardBg, border: cardBd, borderRadius: 14, padding: 16, display: "flex", alignItems: "center", gap: 12 }}>
              <div style={{ padding: 8, borderRadius: 10, background: dark ? "#0d1430" : "#F1F5F9", color }}><Icon size={18} /></div>
              <div style={{ minWidth: 0 }}>
                <p style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: 1, color: muted, margin: 0 }}>{label}</p>
                <p style={{ fontSize: 18, fontWeight: 700, color, margin: "2px 0 0", overflow: "hidden", textOverflow: "ellipsis" }}>{value}</p>
              </div>
            </div>
          ))}
        </div>
      )}

      <div style={{ position: "relative" }}>
        <Search size={16} style={{ position: "absolute", left: 12, top: "50%", transform: "translateY(-50%)", color: muted }} />
        <input type="text" placeholder="Buscar por cliente, cotización, OC o RUT…" value={q} onChange={e => handleSearch(e.target.value)}
          style={{ width: "100%", padding: "10px 14px 10px 36px", borderRadius: 10, border: cardBd, background: cardBg, color: text, fontSize: 14, fontFamily: "inherit", boxSizing: "border-box" }} />
      </div>

      {error && <div style={{ borderRadius: 10, border: "1px solid #FCA5A5", background: "#FEE2E2", color: "#B91C1C", padding: "10px 14px", fontSize: 13 }}>{error}</div>}
      {loading && <div style={{ display: "flex", justifyContent: "center", padding: 60 }}><Loader2 className="animate-spin" size={28} style={{ color: "var(--monza-accent)" }} /></div>}
      {!loading && !error && ventas.length === 0 && (
        <div style={{ background: cardBg, border: cardBd, borderRadius: 14, padding: 60, textAlign: "center" }}>
          <TrendingUp size={40} style={{ margin: "0 auto 12px", opacity: 0.2, color: muted }} />
          <p style={{ fontSize: 13, fontWeight: 500, color: muted, margin: 0 }}>No hay ventas{periodo ? " para el período" : ""}</p>
        </div>
      )}
      {!loading && ventas.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <p style={{ fontSize: 12, fontWeight: 600, textTransform: "uppercase", letterSpacing: 0.5, color: muted, margin: 0 }}>
            {ventas.length} {ventas.length === 1 ? "venta" : "ventas"}
          </p>
          {ventas.map(v => <VentaCard key={v.cotizacion_id} venta={v} onChanged={() => load(q || undefined, periodo || undefined)} />)}
        </div>
      )}
    </div>
  );
}
