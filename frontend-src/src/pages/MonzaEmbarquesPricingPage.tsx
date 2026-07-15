// Página "Embarques — Pricing" (MonzaParts, Contabilidad): costo landed por ítem de cada
// embarque que crea Logística. Misma lógica que Grupo AM (shipping prorrateado por peso,
// gastos locales por CIF); el FOB/peso vienen del ítem de cotización y el dueño los ajusta
// a mano. Vista previa en vivo (espeja el backend) + cierre que congela el costo.
// Consume monzaEmbarquesPricingAPI.
import { useState, useEffect, useCallback, useMemo } from "react";
import {
  Calculator, Search, Loader2, RefreshCw, ChevronDown, ChevronUp, Ship,
  Lock, Unlock, Save, Info, RotateCcw, Package, Plane, Hash,
} from "lucide-react";
import toast from "react-hot-toast";
import { useMonzaTheme } from "./MonzaLayout";
import { monzaEmbarquesPricingAPI } from "../services/monzaApi";
import { calcularLanded, type LandedInput } from "../monza-embarques-pricing/compute";
import type {
  EmbarquePricingRow, PricingDetail, GastoLinea, ItemOverride, PricingSavePayload,
} from "../monza-embarques-pricing/types";

// ─── Estilos (mismo sistema que el resto de MonzaParts) ───────────────────────
function useStyles() {
  const { dark } = useMonzaTheme();
  return {
    dark,
    text: dark ? "#E2E8F0" : "#0f172a",
    muted: dark ? "#94A3B8" : "#64748B",
    faint: dark ? "#64748B" : "#94A3B8",
    cardBg: dark ? "#131b3e" : "white",
    cardBd: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}`,
    sub: dark ? "#0d1430" : "#F8FAFC",
    inputBg: dark ? "#0d1430" : "white",
  };
}
type Styles = ReturnType<typeof useStyles>;

function inputBase(s: Styles): React.CSSProperties {
  return { width: "100%", padding: "6px 10px", borderRadius: 8, border: s.cardBd, background: s.inputBg, color: s.text, fontSize: 13, fontFamily: "inherit", boxSizing: "border-box" };
}
function inputNum(s: Styles): React.CSSProperties {
  return { ...inputBase(s), textAlign: "right", fontFamily: "ui-monospace, monospace" };
}
function btnPrimary(): React.CSSProperties {
  return { display: "flex", alignItems: "center", justifyContent: "center", gap: 8, padding: "9px 16px", borderRadius: 8, border: "none", background: "var(--monza-accent)", color: "white", fontWeight: 600, fontSize: 13, cursor: "pointer", fontFamily: "inherit" };
}
function btnSecondary(s: Styles): React.CSSProperties {
  return { display: "flex", alignItems: "center", justifyContent: "center", gap: 6, padding: "9px 14px", borderRadius: 8, border: s.cardBd, background: s.sub, color: s.text, fontWeight: 600, fontSize: 13, cursor: "pointer", fontFamily: "inherit" };
}

// ─── Helpers de formato ───────────────────────────────────────────────────────
const fmtClp = (n: number | null | undefined) =>
  n == null || Number.isNaN(n) ? "—" : "$" + Math.round(n).toLocaleString("es-CL");
const fmtNum = (n: number | null | undefined, dec = 2) =>
  n == null ? "—" : Number(n).toLocaleString("es-CL", { minimumFractionDigits: 0, maximumFractionDigits: dec });

const FLETE_HINT: Record<string, string> = {
  normal: "LATAM: el flete puede pagarse en CLP en Chile, o venir prepagado por el proveedor (USD).",
  courier: "DHL: el flete puede pagarse en CLP, o venir prepagado por el proveedor (USD/EUR).",
  baukat: "Baukat: el flete viene prepagado por el proveedor en EUR (neteado del pago).",
  fastmark: "Fast Mark: el flete se paga localmente en CLP (no viene prepagado).",
};

// ─── Badges ───────────────────────────────────────────────────────────────────
const TIPO_LABEL: Record<string, string> = {
  normal: "Normal · LATAM", courier: "Courier", baukat: "Baukat · Europa", fastmark: "Fast Mark",
};
const PRICING_STYLE: Record<string, { bg: string; color: string; label: string }> = {
  sin_pricing: { bg: "rgba(148,163,184,0.15)", color: "#64748B", label: "Sin pricing" },
  borrador:    { bg: "rgba(217,119,6,0.15)",   color: "#B45309", label: "Borrador" },
  calculado:   { bg: "rgba(37,99,235,0.15)",   color: "#1D4ED8", label: "Calculado" },
  cerrado:     { bg: "rgba(21,128,61,0.15)",   color: "#15803D", label: "Cerrado" },
};
function Pill({ bg, color, label }: { bg: string; color: string; label: string }) {
  return <span style={{ background: bg, color, fontSize: 11, fontWeight: 600, padding: "3px 9px", borderRadius: 999, whiteSpace: "nowrap" }}>{label}</span>;
}

// ─── Editor de un embarque ──────────────────────────────────────────────────
function PricingEditor({ detail: initial, onSaved }: { detail: PricingDetail; onSaved: (d: PricingDetail) => void }) {
  const s = useStyles();
  const embId = initial.embarque.id;
  const [pricing, setPricing] = useState(initial.pricing);
  const [gastos, setGastos] = useState<GastoLinea[]>(initial.gastos);
  const [items, setItems] = useState(initial.items);
  const [overrides, setOverrides] = useState<Record<number, ItemOverride>>({});
  const [fobInputs, setFobInputs] = useState<Record<number, string>>({});
  const [saving, setSaving] = useState(false);

  const locked = pricing.estado === "cerrado";

  const [tcValor, setTcValor] = useState(String(pricing.tc_valor || ""));
  const [tcTipo, setTcTipo] = useState(pricing.tc_tipo || "manual");
  const [tipoEmb, setTipoEmb] = useState(pricing.tipo_embarque);
  const [moneda, setMoneda] = useState(pricing.moneda);
  const [fleteEnMe, setFleteEnMe] = useState(pricing.flete_en_me);
  const [shippingMe, setShippingMe] = useState(String(pricing.shipping_me || ""));
  const [shippingClp, setShippingClp] = useState(String(pricing.shipping_clp || ""));
  const [observaciones, setObservaciones] = useState(pricing.observaciones || "");

  const tc = Number(tcValor) || 0;
  const shippingTotal = fleteEnMe ? (Number(shippingMe) || 0) * tc : (Number(shippingClp) || 0);
  const totalGastosCap = useMemo(
    () => gastos.filter(g => g.capitaliza).reduce((acc, g) => acc + (Number(g.monto_neto) || 0), 0),
    [gastos],
  );
  const totalIva = gastos.filter(g => g.capitaliza).reduce((acc, g) => acc + (Number(g.iva) || 0), 0);
  const ivaImport = gastos.filter(g => g.tipo === "iva_importacion").reduce((acc, g) => acc + (Number(g.monto_neto) || 0), 0);

  // FOB efectivo por ítem (override manual / reset / actual del server)
  const fobEfectivo = useCallback((it: typeof items[number]) => {
    const ov = overrides[it.embarque_item_id];
    if (ov?.fob_manual === true) return ov.fob_unit ?? 0;
    if (ov?.fob_manual === false) return it.fob_default;
    return it.fob_unit;
  }, [overrides]);

  // Vista previa en vivo (espeja el backend)
  const preview = useMemo(() => {
    const inputs: LandedInput[] = items.map(it => ({
      embarque_item_id: it.embarque_item_id,
      cantidad: it.cantidad,
      peso_unit_kg: it.peso_unit_kg,
      fob_unit: fobEfectivo(it),
      tc_valor: tc,
    }));
    const { rows, totales } = calcularLanded(inputs, shippingTotal, totalGastosCap);
    const byId = new Map(rows.map(r => [r.embarque_item_id, r]));
    return { byId, totales };
  }, [items, fobEfectivo, tc, shippingTotal, totalGastosCap]);

  type GastoField = "monto_neto" | "iva" | "nro_factura" | "fecha_factura" | "banco";
  const setGasto = (idx: number, field: GastoField, val: string) => {
    const numerico = field === "monto_neto" || field === "iva";
    setGastos(gs => gs.map((g, i) => i === idx ? { ...g, [field]: numerico ? (Number(val) || 0) : val } : g));
  };
  const autoIva = (idx: number) => {
    setGastos(gs => gs.map((g, i) => i === idx ? { ...g, iva: Math.round((Number(g.monto_neto) || 0) * 0.19) } : g));
  };

  const setFob = (id: number, val: string) => {
    setFobInputs(f => ({ ...f, [id]: val }));
    setOverrides(o => ({ ...o, [id]: { embarque_item_id: id, fob_unit: Number(val) || 0, fob_manual: true } }));
  };
  const resetFob = (id: number) => {
    setFobInputs(f => { const n = { ...f }; delete n[id]; return n; });
    setOverrides(o => ({ ...o, [id]: { embarque_item_id: id, fob_manual: false } }));
  };

  const buildPayload = (): PricingSavePayload => ({
    tipo_embarque: tipoEmb,
    tc_tipo: tcTipo,
    tc_valor: tc,
    moneda,
    flete_en_me: fleteEnMe,
    shipping_me: Number(shippingMe) || 0,
    shipping_clp: Number(shippingClp) || 0,
    observaciones,
    gastos: gastos.map(g => ({
      tipo: g.tipo, glosa: g.glosa, monto_neto: Number(g.monto_neto) || 0,
      iva: Number(g.iva) || 0, capitaliza: g.capitaliza, nro_factura: g.nro_factura,
      fecha_factura: g.fecha_factura, banco: g.banco, orden: g.orden,
    })),
    items: Object.values(overrides),
  });

  const applyDetail = (d: PricingDetail) => {
    setPricing(d.pricing);
    setGastos(d.gastos);
    setItems(d.items);
    setOverrides({});
    setFobInputs({});
    setTcValor(String(d.pricing.tc_valor || ""));
    setTcTipo(d.pricing.tc_tipo || "manual");
    setTipoEmb(d.pricing.tipo_embarque);
    setMoneda(d.pricing.moneda);
    setFleteEnMe(d.pricing.flete_en_me);
    setShippingMe(String(d.pricing.shipping_me || ""));
    setShippingClp(String(d.pricing.shipping_clp || ""));
    setObservaciones(d.pricing.observaciones || "");
  };

  const handleSave = async () => {
    if (tc <= 0) { toast.error("Ingresa un Tipo de Cambio mayor a 0"); return; }
    setSaving(true);
    try {
      const { data } = await monzaEmbarquesPricingAPI.save(embId, buildPayload());
      applyDetail(data);
      toast.success("Pricing calculado y guardado");
      onSaved(data);
    } catch (e: any) { toast.error(e?.response?.data?.detail || "No se pudo guardar el pricing"); } finally { setSaving(false); }
  };
  const handleCerrar = async () => {
    if (tc <= 0) { toast.error("Define un TC antes de cerrar"); return; }
    setSaving(true);
    try {
      const { data } = await monzaEmbarquesPricingAPI.cerrar(embId);
      applyDetail(data);
      toast.success("Pricing cerrado (costo congelado)");
      onSaved(data);
    } catch (e: any) { toast.error(e?.response?.data?.detail || "No se pudo cerrar el pricing"); } finally { setSaving(false); }
  };
  const handleReabrir = async () => {
    setSaving(true);
    try {
      const { data } = await monzaEmbarquesPricingAPI.reabrir(embId);
      applyDetail(data);
      toast.success("Pricing reabierto para editar");
      onSaved(data);
    } catch (e: any) { toast.error(e?.response?.data?.detail || "No se pudo reabrir"); } finally { setSaving(false); }
  };

  const labelCss: React.CSSProperties = { display: "block", fontSize: 11, color: s.muted, marginBottom: 4 };
  const cardCss: React.CSSProperties = { borderRadius: 12, border: s.cardBd, background: s.sub, padding: 14 };
  const sectionTitle: React.CSSProperties = { fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.6, color: s.muted, marginBottom: 8 };
  const th: React.CSSProperties = { padding: "8px 10px", textAlign: "left", fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.4, color: s.muted, whiteSpace: "nowrap" };
  const tdNum: React.CSSProperties = { padding: "6px 10px", textAlign: "right", fontFamily: "ui-monospace, monospace", whiteSpace: "nowrap", fontSize: 12 };

  return (
    <div style={{ borderTop: s.cardBd, padding: "18px 20px", display: "flex", flexDirection: "column", gap: 18, background: s.cardBg }}>
      {locked && (
        <div style={{ display: "flex", alignItems: "center", gap: 8, padding: 12, borderRadius: 10, background: "rgba(21,128,61,0.1)", border: "1px solid rgba(21,128,61,0.25)" }}>
          <Lock size={15} color="#15803D" />
          <span style={{ fontSize: 12, color: "#15803D" }}>Pricing cerrado — el costo está congelado. Reábralo para editar.</span>
        </div>
      )}

      {/* Encabezado: TC + Flete */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: 14 }}>
        <div style={cardCss}>
          <p style={sectionTitle}>Tipo de cambio y moneda</p>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            <label><span style={labelCss}>Moneda FOB</span>
              <select value={moneda} disabled={locked} onChange={e => setMoneda(e.target.value)} style={inputBase(s)}>
                {["USD", "EUR"].map(m => <option key={m} value={m}>{m}</option>)}
              </select>
            </label>
            <label><span style={labelCss}>Tipo de embarque</span>
              <select value={tipoEmb} disabled={locked} onChange={e => setTipoEmb(e.target.value as typeof tipoEmb)} style={inputBase(s)}>
                {["normal", "courier", "baukat", "fastmark"].map(t => <option key={t} value={t}>{t}</option>)}
              </select>
            </label>
          </div>
          <label style={{ display: "block", marginTop: 12 }}>
            <span style={labelCss}>TC (1 {moneda} = ? CLP)</span>
            <input value={tcValor} disabled={locked} onChange={e => setTcValor(e.target.value)} placeholder="ej. 962" style={inputNum(s)} />
          </label>
        </div>

        <div style={cardCss}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, flexWrap: "wrap" }}>
            <p style={{ ...sectionTitle, marginBottom: 0 }}>Flete internacional</p>
            <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 11, color: s.muted, cursor: "pointer" }}>
              <input type="checkbox" checked={fleteEnMe} disabled={locked} onChange={e => setFleteEnMe(e.target.checked)} style={{ accentColor: "var(--monza-accent)" }} />
              Flete en {moneda} (lo paga el proveedor)
            </label>
          </div>
          <label style={{ display: "block", marginTop: 10 }}>
            {fleteEnMe ? (
              <>
                <span style={labelCss}>Shipping en {moneda} (se convierte con el TC)</span>
                <input value={shippingMe} disabled={locked} onChange={e => setShippingMe(e.target.value)} placeholder="ej. 1200" style={inputNum(s)} />
              </>
            ) : (
              <>
                <span style={labelCss}>Shipping en CLP (pagado en Chile)</span>
                <input value={shippingClp} disabled={locked} onChange={e => setShippingClp(e.target.value)} placeholder="ej. 450000" style={inputNum(s)} />
              </>
            )}
          </label>
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, paddingTop: 8, marginTop: 8, borderTop: s.cardBd }}>
            <span style={{ color: s.muted }}>Shipping total a prorratear</span>
            <span style={{ fontFamily: "ui-monospace, monospace", fontWeight: 700, color: "#B45309" }}>{fmtClp(shippingTotal)}</span>
          </div>
          <p style={{ fontSize: 10, lineHeight: 1.4, color: s.faint, marginTop: 6 }}>{FLETE_HINT[tipoEmb]}</p>
        </div>
      </div>

      {/* Gastos locales */}
      <div>
        <p style={sectionTitle}>Gastos locales (desglose tributario)</p>
        <div style={{ overflowX: "auto", borderRadius: 12, border: s.cardBd }}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ background: s.sub, borderBottom: s.cardBd }}>
                {["Concepto", "Monto Neto", "IVA", "Total Bruto", "N° Factura", "Fecha factura", "Banco", "¿Costo?"].map((h, i) => <th key={i} style={th}>{h}</th>)}
              </tr>
            </thead>
            <tbody>
              {gastos.map((g, idx) => (
                <tr key={g.tipo} style={{ borderBottom: s.cardBd, background: g.tipo === "iva_importacion" ? s.sub : "transparent" }}>
                  <td style={{ padding: "6px 10px", fontWeight: 600, color: s.text, whiteSpace: "nowrap", fontSize: 12 }}>{g.glosa}</td>
                  <td style={{ padding: "4px 8px" }}>
                    <input value={g.monto_neto || ""} disabled={locked} onChange={e => setGasto(idx, "monto_neto", e.target.value)} style={inputNum(s)} placeholder="0" />
                  </td>
                  <td style={{ padding: "4px 8px" }}>
                    {g.tipo === "iva_importacion" || g.tipo === "arancel" ? (
                      <span style={{ display: "block", textAlign: "right", paddingRight: 8, fontStyle: "italic", color: s.faint }} title="Arancel / IVA Importación: sin IVA">—</span>
                    ) : (
                      <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
                        <input value={g.iva || ""} disabled={locked} onChange={e => setGasto(idx, "iva", e.target.value)} style={inputNum(s)} placeholder="0" />
                        {!locked && <button onClick={() => autoIva(idx)} title="IVA 19%" style={{ fontSize: 10, padding: "0 4px", borderRadius: 4, border: "none", background: "none", color: "var(--monza-accent)", cursor: "pointer", flexShrink: 0 }}>19%</button>}
                      </div>
                    )}
                  </td>
                  <td style={{ ...tdNum, color: s.muted }}>{fmtClp((Number(g.monto_neto) || 0) + (Number(g.iva) || 0))}</td>
                  <td style={{ padding: "4px 8px" }}>
                    <input value={g.nro_factura || ""} disabled={locked} onChange={e => setGasto(idx, "nro_factura", e.target.value)} style={{ ...inputBase(s), width: 110 }} placeholder="—" />
                  </td>
                  <td style={{ padding: "4px 8px" }}>
                    <input type="date" value={g.fecha_factura || ""} disabled={locked} onChange={e => setGasto(idx, "fecha_factura", e.target.value)} style={{ ...inputBase(s), width: 140 }} />
                  </td>
                  <td style={{ padding: "4px 8px" }}>
                    <input value={g.banco || ""} disabled={locked} onChange={e => setGasto(idx, "banco", e.target.value)} style={{ ...inputBase(s), width: 120 }} placeholder="—" />
                  </td>
                  <td style={{ padding: "6px 10px", textAlign: "center" }}>
                    {g.capitaliza
                      ? <span style={{ fontSize: 10, color: "#15803D" }} title="Capitaliza al costo">Sí</span>
                      : <span style={{ fontSize: 10, color: s.faint }} title="Recuperable, no es costo">No</span>}
                  </td>
                </tr>
              ))}
              <tr style={{ background: s.sub }}>
                <td style={{ padding: "8px 10px", fontWeight: 700, color: s.text, fontSize: 12 }}>TOTAL GASTOS (capitalizan)</td>
                <td style={{ ...tdNum, fontWeight: 700, color: "var(--monza-accent)" }}>{fmtClp(totalGastosCap)}</td>
                <td style={{ ...tdNum, color: s.muted }}>{fmtClp(totalIva)}</td>
                <td colSpan={5} style={{ padding: "8px 10px", textAlign: "right", fontSize: 11, color: s.faint }}>
                  IVA Importación (recuperable): <span style={{ fontFamily: "ui-monospace, monospace" }}>{fmtClp(ivaImport)}</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p style={{ fontSize: 11, marginTop: 6, display: "flex", alignItems: "center", gap: 4, color: s.faint }}>
          <Info size={12} /> Solo los gastos marcados como costo prorratean el landed. El IVA y el IVA Importación son recuperables.
        </p>
      </div>

      {/* Ítems: costo landed */}
      <div>
        <p style={sectionTitle}>Costo landed por ítem</p>
        <div style={{ overflowX: "auto", borderRadius: 12, border: s.cardBd }}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ background: s.sub, borderBottom: s.cardBd }}>
                {["N° Parte", "Descripción", "Cant", "Peso kg", `FOB unit ${moneda}`, "FOB CLP", "Shipping", "CIF CLP", "Gastos", "Costo Total", "Costo Unit"].map(h => <th key={h} style={th}>{h}</th>)}
              </tr>
            </thead>
            <tbody>
              {items.map((it, idx) => {
                const p = preview.byId.get(it.embarque_item_id);
                const ov = overrides[it.embarque_item_id];
                const isManual = ov?.fob_manual === true || (ov?.fob_manual !== false && it.fob_origen === "manual");
                const origen = ov?.fob_manual === true ? "manual" : it.fob_origen;
                const origenLabel = origen === "cotizacion" ? "de cotización" : origen === "manual" ? "manual" : "sin dato";
                // key tolerante a null: en pricing cerrado los ítems vienen del
                // snapshot y su embarque_item_id puede ser null (columna nullable)
                return (
                  <tr key={`${it.embarque_item_id ?? "s"}-${idx}`} style={{ borderBottom: s.cardBd, background: idx % 2 ? s.sub : "transparent" }}>
                    <td style={{ padding: "6px 10px", fontFamily: "ui-monospace, monospace", fontWeight: 600, whiteSpace: "nowrap", color: "var(--monza-accent)", fontSize: 12 }}>{it.numero_parte || "—"}</td>
                    <td style={{ padding: "6px 10px", maxWidth: 160, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: s.text, fontSize: 12 }} title={it.descripcion}>{it.descripcion}</td>
                    <td style={{ ...tdNum, color: s.muted }}>{fmtNum(it.cantidad, 0)}</td>
                    <td style={{ ...tdNum, color: s.muted }}>{fmtNum(it.peso_unit_kg)}</td>
                    <td style={{ padding: "4px 8px", minWidth: 120 }}>
                      <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
                        <input value={fobInputs[it.embarque_item_id] ?? String(fobEfectivo(it) || "")} disabled={locked} onChange={e => setFob(it.embarque_item_id, e.target.value)} style={inputNum(s)} placeholder="0" />
                        {!locked && isManual && (
                          <button onClick={() => resetFob(it.embarque_item_id)} title="Volver al FOB de la cotización" style={{ flexShrink: 0, border: "none", background: "none", color: "var(--monza-accent)", cursor: "pointer", padding: 0 }}>
                            <RotateCcw size={14} />
                          </button>
                        )}
                      </div>
                      <span style={{ fontSize: 9, textTransform: "uppercase", letterSpacing: 0.3, color: origen === "manual" ? s.muted : s.faint }}>{origenLabel}</span>
                    </td>
                    <td style={{ ...tdNum, color: s.text }}>{fmtClp(p?.fob_clp)}</td>
                    <td style={{ ...tdNum, color: "#B45309" }}>{fmtClp(p?.shipping_clp)}</td>
                    <td style={{ ...tdNum, color: s.text }}>{fmtClp(p?.cif_clp)}</td>
                    <td style={{ ...tdNum, color: "#B45309" }}>{fmtClp(p?.gastos_clp)}</td>
                    <td style={{ ...tdNum, fontWeight: 600, color: "var(--monza-accent)" }}>{fmtClp(p?.costo_total_clp)}</td>
                    <td style={{ ...tdNum, fontWeight: 700, color: "#15803D" }}>{fmtClp(p?.costo_unit_clp)}</td>
                  </tr>
                );
              })}
            </tbody>
            <tfoot>
              <tr style={{ background: s.sub, fontWeight: 700 }}>
                <td style={{ padding: "8px 10px", color: s.text, fontSize: 12 }} colSpan={5}>TOTAL</td>
                <td style={{ ...tdNum, color: s.text }}>{fmtClp(preview.totales.fob_clp)}</td>
                <td style={{ ...tdNum, color: "#B45309" }}>{fmtClp(preview.totales.shipping_clp)}</td>
                <td style={{ ...tdNum, color: s.text }}>{fmtClp(preview.totales.cif_clp)}</td>
                <td style={{ ...tdNum, color: "#B45309" }}>{fmtClp(preview.totales.gastos_clp)}</td>
                <td style={{ ...tdNum, color: "var(--monza-accent)" }}>{fmtClp(preview.totales.costo_total_clp)}</td>
                <td />
              </tr>
            </tfoot>
          </table>
        </div>
      </div>

      {/* Observaciones + acciones */}
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <label style={{ display: "block" }}>
          <span style={labelCss}>Observaciones</span>
          <input value={observaciones} disabled={locked} onChange={e => setObservaciones(e.target.value)} style={inputBase(s)} placeholder="Notas del pricing…" />
        </label>
        <div style={{ display: "flex", alignItems: "center", gap: 10, justifyContent: "flex-end" }}>
          {locked ? (
            <button onClick={handleReabrir} disabled={saving} style={{ ...btnSecondary(s), opacity: saving ? 0.6 : 1 }}>
              {saving ? <Loader2 size={15} className="animate-spin" /> : <Unlock size={15} />} Reabrir
            </button>
          ) : (
            <>
              <button onClick={handleSave} disabled={saving} style={{ ...btnPrimary(), opacity: saving ? 0.6 : 1 }}>
                {saving ? <Loader2 size={15} className="animate-spin" /> : <Save size={15} />} Guardar y calcular
              </button>
              <button onClick={handleCerrar} disabled={saving} style={{ ...btnSecondary(s), opacity: saving ? 0.6 : 1 }}>
                <Lock size={15} /> Cerrar
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── Tarjeta de embarque (carga el detalle al expandir) ───────────────────────
function EmbarqueCard({ row, onChanged }: { row: EmbarquePricingRow; onChanged: () => void }) {
  const s = useStyles();
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<PricingDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(false);

  const fetchDetail = async () => {
    setLoading(true); setErr(false);
    try {
      const { data } = await monzaEmbarquesPricingAPI.get(row.embarque_id);
      setDetail(data);
    } catch { setErr(true); } finally { setLoading(false); }
  };
  const toggle = async () => {
    const next = !open;
    setOpen(next);
    if (next && !detail) await fetchDetail();
  };

  const pr = PRICING_STYLE[row.pricing_estado] ?? PRICING_STYLE.sin_pricing;
  return (
    <div style={{ borderRadius: 16, border: s.cardBd, overflow: "hidden", background: s.cardBg }}>
      <button onClick={toggle} style={{ width: "100%", textAlign: "left", padding: "16px 18px", display: "flex", alignItems: "center", gap: 12, background: "none", border: "none", cursor: "pointer", fontFamily: "inherit" }}>
        <div style={{ width: 36, height: 36, borderRadius: 10, background: "var(--monza-accent)", opacity: 0.92, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
          <Plane size={16} color="white" />
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", marginBottom: 5 }}>
            {row.correlativo != null && (
              <span style={{ display: "inline-flex", alignItems: "center", gap: 2, fontSize: 11, fontFamily: "ui-monospace, monospace", fontWeight: 700, padding: "2px 6px", borderRadius: 6, background: s.sub, color: s.muted }} title="Correlativo de pricing">
                <Hash size={11} />{row.correlativo}
              </span>
            )}
            <span style={{ fontFamily: "ui-monospace, monospace", fontWeight: 700, fontSize: 14, color: "var(--monza-accent)" }}>{row.numero || `EMB #${row.embarque_id}`}</span>
            <Pill bg={s.sub} color={s.muted} label={TIPO_LABEL[row.tipo_embarque] ?? row.tipo_embarque} />
            <Pill {...pr} />
          </div>
          <p style={{ fontSize: 12, color: s.muted, margin: 0 }}>
            {row.forwarder && <span>{row.forwarder} · </span>}
            {row.estado_logistica && <span>Logística: {row.estado_logistica} · </span>}
            <span>{row.n_items} ítem(s)</span>
            {row.tc_valor ? <span> · TC {fmtNum(row.tc_valor, 2)}</span> : null}
          </p>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 16, flexShrink: 0 }}>
          <div style={{ textAlign: "right" }}>
            <p style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: 0.5, fontWeight: 600, color: s.faint, margin: 0 }}>Costo landed</p>
            <p style={{ fontWeight: 700, fontSize: 16, margin: 0, color: row.costo_total_clp ? s.text : s.faint }}>{row.costo_total_clp ? fmtClp(row.costo_total_clp) : "—"}</p>
          </div>
          <div style={{ color: s.muted }}>{open ? <ChevronUp size={16} /> : <ChevronDown size={16} />}</div>
        </div>
      </button>

      {open && (
        <>
          {loading && <div style={{ display: "flex", justifyContent: "center", padding: 40 }}><Loader2 size={20} className="animate-spin" color="var(--monza-accent)" /></div>}
          {!loading && err && (
            <div style={{ padding: "24px 20px", textAlign: "center", fontSize: 13, color: s.muted, borderTop: s.cardBd }}>
              No se pudo cargar el detalle.
              <button onClick={fetchDetail} style={{ marginLeft: 8, color: "var(--monza-accent)", background: "none", border: "none", textDecoration: "underline", cursor: "pointer" }}>Reintentar</button>
            </div>
          )}
          {!loading && detail && <PricingEditor detail={detail} onSaved={(d) => { setDetail(d); onChanged(); }} />}
        </>
      )}
    </div>
  );
}

// ─── Página ─────────────────────────────────────────────────────────────────
export default function MonzaEmbarquesPricingPage() {
  const s = useStyles();
  const [rows, setRows] = useState<EmbarquePricingRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async (search?: string) => {
    setLoading(true); setError("");
    try {
      const { data } = await monzaEmbarquesPricingAPI.list(search);
      setRows(data);
    } catch { setError("No se pudieron cargar los embarques."); } finally { setLoading(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

  const handleSearch = (v: string) => {
    setQ(v);
    if (v.length === 0 || v.length >= 2) load(v || undefined);
  };

  const pendientes = rows.filter(r => r.pricing_estado === "sin_pricing" || r.pricing_estado === "borrador").length;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 22 }}>
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 16, flexWrap: "wrap" }}>
        <div style={{ minWidth: 0 }}>
          <h1 style={{ fontSize: 24, fontWeight: 700, display: "flex", alignItems: "center", gap: 8, color: s.text, margin: 0 }}>
            <Calculator size={24} color="var(--monza-accent)" /> Embarques — Pricing
          </h1>
          <p style={{ fontSize: 13, marginTop: 4, color: s.muted }}>
            Costo landed (CIF + gastos locales) por ítem · shipping prorrateado por peso, gastos por valor CIF
          </p>
        </div>
        <button onClick={() => load(q || undefined)} style={btnSecondary(s)}><RefreshCw size={15} /></button>
      </div>

      <div style={{ display: "flex", alignItems: "flex-start", gap: 10, padding: 14, borderRadius: 12, border: "1px solid var(--monza-accent)", background: s.sub }}>
        <Info size={15} color="var(--monza-accent)" style={{ flexShrink: 0, marginTop: 2 }} />
        <p style={{ fontSize: 12, lineHeight: 1.5, color: s.muted, margin: 0 }}>
          Todo embarque creado por <strong>Logística</strong> aparece acá automáticamente. Contabilidad completa el
          TC, el flete y los gastos locales; el costo landed por ítem se calcula al instante y se congela al cerrar.
        </p>
      </div>

      <div style={{ position: "relative" }}>
        <Search size={15} color={s.faint} style={{ position: "absolute", left: 12, top: "50%", transform: "translateY(-50%)" }} />
        <input type="text" placeholder="Buscar por N° de embarque, forwarder o AWB…" value={q} onChange={e => handleSearch(e.target.value)}
          style={{ ...inputBase(s), padding: "10px 16px 10px 36px", fontSize: 14 }} />
      </div>

      {error && <div style={{ borderRadius: 10, border: "1px solid rgba(185,28,28,0.25)", background: "rgba(185,28,28,0.1)", padding: "12px 16px", fontSize: 13, color: "#B91C1C" }}>{error}</div>}
      {loading && <div style={{ display: "flex", justifyContent: "center", padding: 64 }}><Loader2 size={28} className="animate-spin" color="var(--monza-accent)" /></div>}
      {!loading && !error && rows.length === 0 && (
        <div style={{ borderRadius: 16, border: s.cardBd, padding: 64, textAlign: "center", background: s.cardBg }}>
          <Ship size={40} style={{ margin: "0 auto 12px", opacity: 0.2, color: s.muted }} />
          <p style={{ fontSize: 13, fontWeight: 500, color: s.muted, margin: 0 }}>Aún no hay embarques creados por Logística</p>
        </div>
      )}
      {!loading && rows.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <p style={{ fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.5, display: "flex", alignItems: "center", gap: 8, color: s.faint, margin: 0 }}>
            <Package size={14} />
            {rows.length} embarque(s){pendientes > 0 ? ` · ${pendientes} pendiente(s) de pricing` : ""}
          </p>
          {rows.map(r => <EmbarqueCard key={r.embarque_id} row={r} onChanged={() => load(q || undefined)} />)}
        </div>
      )}
    </div>
  );
}
