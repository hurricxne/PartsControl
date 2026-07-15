// Página "Facturas y Cobranzas" (MonzaParts, cuentas por cobrar): lista facturas +
// antigüedad de cartera, y concentra las acciones — EMITIR factura (desde una guía
// despachada), registrar cobranzas y gestionar factoring. Consume monzaContabilidadAPI.
import { useState, useEffect, useCallback } from "react";
import {
  Receipt, Plus, Search, AlertCircle, CheckCircle2, DollarSign,
  Loader2, RefreshCw, ChevronDown, ChevronUp, CreditCard, Landmark, X, Trash2,
} from "lucide-react";
import toast from "react-hot-toast";
import { useMonzaTheme } from "./MonzaLayout";
import { fmtClp, hoyLocal } from "../utils/format";
import { monzaContabilidadAPI } from "../services/monzaApi";

// ─── Tipos ────────────────────────────────────────────────────────────────────
interface Cobranza { id: number; fecha: string | null; monto: number; medio: string; es_factoring?: boolean; banco: string | null; numero_operacion: string | null; observaciones: string | null }
interface Factoring { id: number; empresa_factoring: string | null; id_operacion: string | null; fecha_operacion: string | null; monto_adelantado: number; costo_factoring: number; retencion: number; banco: string | null; estado: string; fecha_liquidacion: string | null }
interface FacturaItem { id: number; item_cotizacion_id?: number | null; despacho_item_id?: number | null; numero_parte: string | null; descripcion: string | null; cantidad: number; precio_unit_neto: number; total_neto: number }
interface Factura {
  id: number; numero_factura: string | null; tipo_doc: string;
  cotizacion_id: number | null; despacho_id?: number | null; numero_cotizacion: string | null; numero_guia: string | null;
  cliente: string; rut_cliente: string;
  fecha_emision: string | null; condicion_pago: string | null; plazo_dias: number | null; fecha_vencimiento: string | null;
  monto_neto: number; iva: number; monto_bruto: number; monto_pagado: number; saldo: number;
  estado_pago: string; semaforo: string; dias_vencimiento: number | null; observaciones: string | null;
  items: FacturaItem[]; cobranzas: Cobranza[]; factoring: Factoring | null;
}
interface Kpis { facturado_clp: number; cobrado_clp: number; cobrado_cliente_clp?: number; anticipo_factoring_clp?: number; por_cobrar_clp: number; vencido_clp: number; en_factoring_clp: number }
interface Aging { "0_30": number; "31_60": number; "61_90": number; "91_mas": number }
// Fechas puras 'YYYY-MM-DD' se parsean como fecha LOCAL: new Date('YYYY-MM-DD') las
// interpreta en UTC y en Chile (UTC-4/-3) quedarían corridas un día hacia atrás.
const fmtDate = (d?: string | null) => {
  if (!d) return "—";
  const dt = /^\d{4}-\d{2}-\d{2}$/.test(d) ? new Date(d + "T00:00:00") : new Date(d);
  return isNaN(dt.getTime()) ? d : dt.toLocaleDateString("es-CL");
};

const PAGO: Record<string, { bg: string; color: string; label: string }> = {
  por_cobrar:  { bg: "#DBEAFE", color: "#1D4ED8", label: "Por cobrar" },
  parcial:     { bg: "#FEF3C7", color: "#B45309", label: "Pago parcial" },
  pagada:      { bg: "#DCFCE7", color: "#15803D", label: "Pagada" },
  vencida:     { bg: "#FEE2E2", color: "#B91C1C", label: "Vencida" },
  factorizada: { bg: "#EDE9FE", color: "#6D28D9", label: "Factoring" },
};
const ESTADOS = ["", "por_cobrar", "parcial", "pagada", "vencida", "factorizada"];
const ESTADO_LABEL: Record<string, string> = { "": "Todas", por_cobrar: "Por cobrar", parcial: "Parcial", pagada: "Pagada", vencida: "Vencida", factorizada: "Factoring" };

// ─── helpers de estilo según theme ─────────────────────────────────────────────
function useStyles() {
  const { dark } = useMonzaTheme();
  return {
    dark,
    text: dark ? "#E2E8F0" : "#0f172a",
    muted: dark ? "#94A3B8" : "#64748B",
    cardBg: dark ? "#131b3e" : "white",
    cardBd: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}`,
    sub: dark ? "#0d1430" : "#F8FAFC",
    inputBg: dark ? "#0d1430" : "white",
  };
}

// ─── Modal genérico ───────────────────────────────────────────────────────────
function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  const s = useStyles();
  return (
    <div style={{ position: "fixed", inset: 0, zIndex: 300, display: "flex", alignItems: "center", justifyContent: "center", background: "rgba(0,0,0,0.6)", padding: 16 }} onClick={onClose}>
      <div style={{ width: "100%", maxWidth: 440, borderRadius: 14, border: s.cardBd, background: s.cardBg, boxShadow: "0 20px 50px rgba(0,0,0,0.4)" }} onClick={e => e.stopPropagation()}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "12px 18px", borderBottom: s.cardBd }}>
          <h3 style={{ fontSize: 14, fontWeight: 600, color: s.text, margin: 0 }}>{title}</h3>
          <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", color: s.muted, padding: 4 }}><X size={16} /></button>
        </div>
        <div style={{ padding: 18, display: "flex", flexDirection: "column", gap: 12 }}>{children}</div>
      </div>
    </div>
  );
}
function Field({ label, children }: { label: string; children: React.ReactNode }) {
  const s = useStyles();
  return (<label style={{ display: "block" }}><span style={{ display: "block", fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 4, color: s.muted }}>{label}</span>{children}</label>);
}
function useInput() {
  const s = useStyles();
  return { width: "100%", padding: "8px 12px", borderRadius: 8, border: s.cardBd, background: s.inputBg, color: s.text, fontSize: 14, fontFamily: "inherit", boxSizing: "border-box" } as React.CSSProperties;
}
function btnPrimary(): React.CSSProperties {
  return { width: "100%", display: "flex", alignItems: "center", justifyContent: "center", gap: 8, padding: "10px 14px", borderRadius: 8, border: "none", background: "var(--monza-accent)", color: "white", fontWeight: 600, fontSize: 14, cursor: "pointer", fontFamily: "inherit" };
}
function btnSecondary(s: ReturnType<typeof useStyles>): React.CSSProperties {
  return { width: "100%", display: "flex", alignItems: "center", justifyContent: "center", gap: 6, padding: "8px 12px", borderRadius: 8, border: s.cardBd, background: s.sub, color: s.text, fontWeight: 600, fontSize: 12, cursor: "pointer", fontFamily: "inherit" };
}

// ─── Modal: emitir factura (desde un despacho/guía de una cotización) ──────────
function CrearFacturaModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const s = useStyles();
  const inp = useInput();
  const [ventas, setVentas] = useState<{ cotizacion_id: number; numero_cotizacion: string; cliente: string }[]>([]);
  const [cotId, setCotId] = useState<number | "">("");
  const [despachos, setDespachos] = useState<{ id: number; numero_despacho: string; numero_guia: string | null; guia_firmada: boolean; items_count: number }[]>([]);
  const [despachoId, setDespachoId] = useState<number | "">("");
  const [sinGuia, setSinGuia] = useState(false);  // retiro en oficina (sin guía de despacho)
  const [folio, setFolio] = useState("");
  const [tipo, setTipo] = useState("factura");
  const [fecha, setFecha] = useState(hoyLocal());
  const [plazo, setPlazo] = useState("30");
  const [obs, setObs] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    monzaContabilidadAPI.listVentas().then(({ data }) =>
      setVentas(data.map((v: any) => ({ cotizacion_id: v.cotizacion_id, numero_cotizacion: v.numero_cotizacion, cliente: v.cliente })))
    ).catch(() => {});
  }, []);
  useEffect(() => {
    if (!cotId) { setDespachos([]); return; }
    monzaContabilidadAPI.despachosFacturables(Number(cotId)).then(({ data }) => setDespachos(data || [])).catch(() => setDespachos([]));
  }, [cotId]);

  const submit = async () => {
    if (!cotId) { toast.error("Selecciona la venta"); return; }
    if (!sinGuia && !despachoId) { toast.error("Selecciona el despacho (o marca 'Retiro en oficina')"); return; }
    setSaving(true);
    try {
      await monzaContabilidadAPI.crearFactura({
        cotizacion_id: Number(cotId),
        // Retiro en oficina → sin_guia (factura el saldo de la venta sin despacho).
        ...(sinGuia ? { sin_guia: true } : { despacho_id: Number(despachoId) }),
        numero_factura: folio || undefined, tipo_doc: tipo,
        fecha_emision: fecha, plazo_dias: plazo ? Number(plazo) : undefined,
        condicion_pago: plazo ? `${plazo} días` : undefined,
        observaciones: obs || undefined,
      });
      toast.success("Factura emitida"); onDone(); onClose();
    } catch (e: any) { toast.error(e?.response?.data?.detail || "No se pudo emitir la factura"); } finally { setSaving(false); }
  };

  return (
    <Modal title="Emitir factura" onClose={onClose}>
      <Field label="Venta (cotización)">
        <select style={inp} value={cotId} onChange={e => { setCotId(e.target.value ? Number(e.target.value) : ""); setDespachoId(""); }}>
          <option value="">Selecciona cotización…</option>
          {ventas.map(v => <option key={v.cotizacion_id} value={v.cotizacion_id}>COT {v.numero_cotizacion} — {v.cliente}</option>)}
        </select>
      </Field>
      <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", fontSize: 13, color: s.text }}>
        <input type="checkbox" checked={sinGuia} onChange={e => { setSinGuia(e.target.checked); if (e.target.checked) setDespachoId(""); }} style={{ accentColor: "var(--monza-accent)" }} />
        Retiro en oficina (sin guía de despacho)
      </label>
      {!sinGuia ? (
        <Field label="Despacho / guía a facturar">
          <select style={inp} value={despachoId} onChange={e => setDespachoId(e.target.value ? Number(e.target.value) : "")} disabled={!cotId}>
            <option value="">{cotId ? (despachos.length ? "Selecciona despacho…" : "Sin despachos por facturar") : "Elige una venta primero"}</option>
            {despachos.map(d => <option key={d.id} value={d.id}>{d.numero_despacho}{d.numero_guia ? ` · Guía ${d.numero_guia}` : ""} ({d.items_count} ítems){d.guia_firmada ? " · firmada" : ""}</option>)}
          </select>
        </Field>
      ) : (
        <p style={{ fontSize: 12, color: s.muted, margin: 0, background: s.sub, padding: "8px 10px", borderRadius: 8 }}>
          Se facturará el <b>saldo pendiente de la venta</b> (lo vendido aún no facturado), sin requerir guía de despacho.
        </p>
      )}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        <Field label="N° Factura (folio)"><input style={inp} value={folio} onChange={e => setFolio(e.target.value)} placeholder="Ej. 35" /></Field>
        <Field label="Tipo"><select style={inp} value={tipo} onChange={e => setTipo(e.target.value)}><option value="factura">Factura</option><option value="boleta">Boleta</option></select></Field>
        <Field label="Fecha emisión"><input type="date" style={inp} value={fecha} onChange={e => setFecha(e.target.value)} /></Field>
        <Field label="Plazo (días)"><input type="number" style={inp} value={plazo} onChange={e => setPlazo(e.target.value)} /></Field>
      </div>
      <Field label="Observaciones (opcional)">
        <input style={inp} value={obs} onChange={e => setObs(e.target.value)}
          placeholder={sinGuia ? "Ej. retira Juan Pérez (si lo dejas vacío: \"Retiro en oficina\")" : "Notas de la factura"} />
      </Field>
      <button onClick={submit} disabled={saving} style={{ ...btnPrimary(), opacity: saving ? 0.6 : 1 }}>
        {saving ? <Loader2 className="animate-spin" size={16} /> : <Receipt size={16} />} Emitir factura
      </button>
    </Modal>
  );
}

// ─── Modal: registrar cobranza ────────────────────────────────────────────────
function CobranzaModal({ factura, onClose, onDone }: { factura: Factura; onClose: () => void; onDone: () => void }) {
  const s = useStyles(); const inp = useInput();
  const [monto, setMonto] = useState(String(Math.round(factura.saldo)));
  const [fecha, setFecha] = useState(hoyLocal());
  const [medio, setMedio] = useState("transferencia");
  const [banco, setBanco] = useState("");
  const [op, setOp] = useState("");
  const [saving, setSaving] = useState(false);
  const submit = async () => {
    if (!monto || Number(monto) <= 0) { toast.error("Monto inválido"); return; }
    setSaving(true);
    try {
      await monzaContabilidadAPI.registrarCobranza(factura.id, { monto: Number(monto), fecha, medio, banco: banco || undefined, numero_operacion: op || undefined });
      toast.success("Cobranza registrada"); onDone(); onClose();
    } catch (e: any) { toast.error(e?.response?.data?.detail || "Error al registrar cobranza"); } finally { setSaving(false); }
  };
  return (
    <Modal title={`Registrar cobranza · ${factura.numero_factura || "#" + factura.id}`} onClose={onClose}>
      <p style={{ fontSize: 12, color: s.muted, margin: 0 }}>Saldo pendiente: <b style={{ color: "#B45309" }}>{fmtClp(factura.saldo)}</b></p>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        <Field label="Monto"><input type="number" style={inp} value={monto} onChange={e => setMonto(e.target.value)} /></Field>
        <Field label="Fecha"><input type="date" style={inp} value={fecha} onChange={e => setFecha(e.target.value)} /></Field>
        <Field label="Medio"><select style={inp} value={medio} onChange={e => setMedio(e.target.value)}><option value="transferencia">Transferencia</option><option value="cheque">Cheque</option><option value="efectivo">Efectivo</option></select></Field>
        <Field label="Banco"><input style={inp} value={banco} onChange={e => setBanco(e.target.value)} /></Field>
      </div>
      <Field label="N° operación"><input style={inp} value={op} onChange={e => setOp(e.target.value)} /></Field>
      <button onClick={submit} disabled={saving} style={{ ...btnPrimary(), opacity: saving ? 0.6 : 1 }}>
        {saving ? <Loader2 className="animate-spin" size={16} /> : <CheckCircle2 size={16} />} Registrar pago
      </button>
    </Modal>
  );
}

// ─── Modal: factoring ─────────────────────────────────────────────────────────
function FactoringModal({ factura, onClose, onDone }: { factura: Factura; onClose: () => void; onDone: () => void }) {
  const s = useStyles(); const inp = useInput();
  const cobradoReal = factura.cobranzas.filter(c => !(c.es_factoring ?? c.medio.startsWith("factoring"))).reduce((sum, c) => sum + c.monto, 0);
  const cupo = Math.max(0, factura.monto_bruto - cobradoReal);
  const [empresa, setEmpresa] = useState(factura.factoring?.empresa_factoring || "");
  const [op, setOp] = useState(factura.factoring?.id_operacion || "");
  const [fecha, setFecha] = useState(factura.factoring?.fecha_operacion || hoyLocal());
  const [adelanto, setAdelanto] = useState(String(Math.round(factura.factoring?.monto_adelantado ?? Math.round(cupo * 0.9))));
  const [costo, setCosto] = useState(String(Math.round(factura.factoring?.costo_factoring || 0)));
  const [retencion, setRetencion] = useState(String(Math.round(factura.factoring?.retencion ?? (cupo - Math.round(cupo * 0.9)))));
  const [banco, setBanco] = useState(factura.factoring?.banco || "");
  const [saving, setSaving] = useState(false);
  const submit = async () => {
    setSaving(true);
    try {
      await monzaContabilidadAPI.setFactoring(factura.id, {
        empresa_factoring: empresa || undefined, id_operacion: op || undefined, fecha_operacion: fecha,
        monto_adelantado: Number(adelanto) || 0, costo_factoring: Number(costo) || 0,
        retencion: retencion === "" ? undefined : Number(retencion), banco: banco || undefined,
      });
      toast.success("Factoring registrado"); onDone(); onClose();
    } catch (e: any) { toast.error(e?.response?.data?.detail || "Error en factoring"); } finally { setSaving(false); }
  };
  return (
    <Modal title={`Factoring · ${factura.numero_factura || "#" + factura.id}`} onClose={onClose}>
      <p style={{ fontSize: 12, color: s.muted, margin: 0 }}>Bruto: <b style={{ color: s.text }}>{fmtClp(factura.monto_bruto)}</b> · Financiable (cupo): <b style={{ color: "#6D28D9" }}>{fmtClp(cupo)}</b></p>
      <Field label="Empresa de factoring"><input style={inp} value={empresa} onChange={e => setEmpresa(e.target.value)} placeholder="Ej. Penta Financiero" /></Field>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        <Field label="ID operación"><input style={inp} value={op} onChange={e => setOp(e.target.value)} /></Field>
        <Field label="Fecha"><input type="date" style={inp} value={fecha} onChange={e => setFecha(e.target.value)} /></Field>
        <Field label="Monto adelantado"><input type="number" style={inp} value={adelanto} onChange={e => { setAdelanto(e.target.value); setRetencion(String(Math.max(0, Math.round(cupo - (Number(e.target.value) || 0))))); }} /></Field>
        <Field label="Costo factoring"><input type="number" style={inp} value={costo} onChange={e => setCosto(e.target.value)} /></Field>
        <Field label="Retención (= cupo − adelanto)"><input type="number" style={inp} value={retencion} onChange={e => setRetencion(e.target.value)} /></Field>
        <Field label="Banco"><input style={inp} value={banco} onChange={e => setBanco(e.target.value)} /></Field>
      </div>
      <button onClick={submit} disabled={saving} style={{ ...btnPrimary(), opacity: saving ? 0.6 : 1 }}>
        {saving ? <Loader2 className="animate-spin" size={16} /> : <Landmark size={16} />} Guardar factoring
      </button>
    </Modal>
  );
}

// ─── Fila de factura (expandible) ─────────────────────────────────────────────
function FacturaRow({ f, onChanged, onCobrar, onFactoring }: { f: Factura; onChanged: () => void; onCobrar: (f: Factura) => void; onFactoring: (f: Factura) => void }) {
  const s = useStyles();
  const [open, setOpen] = useState(false);
  const pago = PAGO[f.estado_pago] ?? { bg: "#F1F5F9", color: "#64748B", label: f.estado_pago };
  const pct = Math.min(100, f.monto_bruto > 0 ? Math.round((f.monto_pagado / f.monto_bruto) * 100) : 0);
  const liquidar = async () => {
    try { await monzaContabilidadAPI.liquidarFactoring(f.id); toast.success("Factoring liquidado"); onChanged(); }
    catch (e: any) { toast.error(e?.response?.data?.detail || "Error"); }
  };
  const delCobranza = async (id: number) => {
    if (!confirm("¿Eliminar esta cobranza?")) return;
    try { await monzaContabilidadAPI.eliminarCobranza(f.id, id); toast.success("Cobranza eliminada"); onChanged(); }
    catch (e: any) { toast.error(e?.response?.data?.detail || "Error"); }
  };
  const eliminar = async () => {
    if (!confirm("¿Eliminar esta factura? Solo si no tiene pagos ni factoring (revierte las cobranzas primero).")) return;
    try { await monzaContabilidadAPI.eliminarFactura(f.id); toast.success("Factura eliminada"); onChanged(); }
    catch (e: any) { toast.error(e?.response?.data?.detail || "Error"); }
  };
  const td: React.CSSProperties = { padding: "12px 16px", whiteSpace: "nowrap" };
  return (
    <>
      <tr style={{ cursor: "pointer", borderBottom: s.cardBd }} onClick={() => setOpen(o => !o)}>
        <td style={{ ...td, fontWeight: 600, color: "var(--monza-accent)" }}>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>{open ? <ChevronUp size={12} /> : <ChevronDown size={12} />}{f.numero_factura || `#${f.id}`}</span>
        </td>
        <td style={{ ...td, fontWeight: 500, color: s.text, maxWidth: 180, overflow: "hidden", textOverflow: "ellipsis" }}>{f.cliente}</td>
        <td style={{ ...td, color: "var(--monza-accent)", fontSize: 12 }}>{f.numero_cotizacion || "—"}</td>
        <td style={{ ...td, color: s.muted }}>{fmtDate(f.fecha_emision)}</td>
        <td style={{ ...td, fontWeight: 600, color: s.text }}>{fmtClp(f.monto_bruto)}</td>
        <td style={td}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <div style={{ width: 64, height: 6, borderRadius: 999, background: s.dark ? "#0d1430" : "#E2E8F0" }}><div style={{ height: "100%", borderRadius: 999, background: "#15803D", width: `${pct}%` }} /></div>
            <span style={{ fontSize: 12, color: "#15803D", fontWeight: 600 }}>{pct}%</span>
          </div>
        </td>
        <td style={{ ...td, color: f.semaforo === "vencida" ? "#B91C1C" : s.muted, fontWeight: f.semaforo === "vencida" ? 600 : 400 }}>
          {fmtDate(f.fecha_vencimiento)}
          {f.dias_vencimiento != null && f.saldo > 0 && <span style={{ marginLeft: 4, fontSize: 11 }}>({f.dias_vencimiento < 0 ? `${Math.abs(f.dias_vencimiento)}d venc.` : `${f.dias_vencimiento}d`})</span>}
        </td>
        <td style={td}><span style={{ background: pago.bg, color: pago.color, fontSize: 12, fontWeight: 600, padding: "4px 10px", borderRadius: 999 }}>{pago.label}</span></td>
      </tr>
      {open && (
        <tr>
          <td colSpan={8} style={{ padding: "0 16px 16px", background: s.sub }}>
            <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr", gap: 16, paddingTop: 12 }}>
              <div>
                <p style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: 0.5, color: s.muted, margin: "0 0 4px" }}>Ítems facturados</p>
                <div style={{ borderRadius: 8, border: s.cardBd, overflow: "hidden" }}>
                  <table style={{ width: "100%", fontSize: 12, borderCollapse: "collapse" }}>
                    <tbody>
                      {f.items.map(it => (
                        <tr key={it.id} style={{ borderBottom: s.cardBd }}>
                          <td style={{ padding: "6px 8px", fontWeight: 600, color: s.text }}>{it.numero_parte}</td>
                          <td style={{ padding: "6px 8px", color: s.muted, maxWidth: 160, overflow: "hidden", textOverflow: "ellipsis" }}>{it.descripcion}</td>
                          <td style={{ padding: "6px 8px", textAlign: "right", color: s.muted }}>{it.cantidad} × {fmtClp(it.precio_unit_neto)}</td>
                          <td style={{ padding: "6px 8px", textAlign: "right", fontWeight: 600, color: s.text }}>{fmtClp(it.total_neto)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div style={{ display: "flex", gap: 20, marginTop: 8, fontSize: 12, color: s.muted }}>
                  <span>Neto: <b style={{ color: s.text }}>{fmtClp(f.monto_neto)}</b></span>
                  <span>IVA: <b style={{ color: s.text }}>{fmtClp(f.iva)}</b></span>
                  <span>Total: <b style={{ color: "var(--monza-accent)" }}>{fmtClp(f.monto_bruto)}</b></span>
                  <span>Saldo: <b style={{ color: "#B45309" }}>{fmtClp(f.saldo)}</b></span>
                </div>
                {(f.numero_guia || f.numero_cotizacion || f.condicion_pago || f.observaciones) && (
                  <div style={{ display: "flex", flexWrap: "wrap", gap: "4px 20px", marginTop: 6, fontSize: 12, color: s.muted }}>
                    {f.numero_cotizacion && <span>Cotización: <b style={{ color: s.text }}>{f.numero_cotizacion}</b></span>}
                    {f.numero_guia && <span>Guía: <b style={{ color: s.text }}>{f.numero_guia}</b></span>}
                    {f.condicion_pago && <span>Condición: <b style={{ color: s.text }}>{f.condicion_pago}</b></span>}
                    {f.observaciones && <span>Obs.: <b style={{ color: s.text }}>{f.observaciones}</b></span>}
                  </div>
                )}
                {f.cobranzas.length > 0 && (
                  <div style={{ marginTop: 12 }}>
                    <p style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: 0.5, color: s.muted, margin: "0 0 4px" }}>Cobranzas</p>
                    {f.cobranzas.map(c => {
                      const esFact = c.es_factoring ?? c.medio.startsWith("factoring");
                      return (
                        <div key={c.id} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", fontSize: 12, padding: "4px 0", borderBottom: s.cardBd }}>
                          <span style={{ color: s.muted }}>{fmtDate(c.fecha)} · {c.medio.replace(/_/g, " ")}{c.banco ? ` · ${c.banco}` : ""}{c.numero_operacion ? ` · ${c.numero_operacion}` : ""}</span>
                          <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
                            <span style={{ fontWeight: 600, color: esFact ? "#6D28D9" : "#15803D" }}>{fmtClp(c.monto)}</span>
                            {!esFact && <button onClick={(e) => { e.stopPropagation(); delCobranza(c.id); }} style={{ color: "#B91C1C", background: "none", border: "none", cursor: "pointer", padding: 2 }} title="Eliminar cobranza"><Trash2 size={12} /></button>}
                          </span>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {f.saldo > 0 && <button onClick={(e) => { e.stopPropagation(); onCobrar(f); }} style={btnSecondary(s)}><CreditCard size={14} /> Registrar cobranza</button>}
                <button onClick={(e) => { e.stopPropagation(); onFactoring(f); }} disabled={f.factoring?.estado === "liquidada"} style={{ ...btnSecondary(s), opacity: f.factoring?.estado === "liquidada" ? 0.4 : 1, cursor: f.factoring?.estado === "liquidada" ? "not-allowed" : "pointer" }}>
                  <Landmark size={14} /> {f.factoring ? (f.factoring.estado === "liquidada" ? "Factoring liquidado" : "Editar factoring") : "Factorizar"}
                </button>
                {f.factoring && (
                  <div style={{ borderRadius: 8, border: s.cardBd, padding: 10, fontSize: 12, background: s.cardBg }}>
                    <p style={{ fontWeight: 600, color: "#6D28D9", margin: 0 }}>{f.factoring.empresa_factoring || "Factoring"} <span style={{ fontWeight: 400, color: s.muted }}>({f.factoring.estado})</span></p>
                    <p style={{ color: s.muted, margin: "2px 0 0" }}>Adelanto: <b style={{ color: s.text }}>{fmtClp(f.factoring.monto_adelantado)}</b></p>
                    <p style={{ color: s.muted, margin: "2px 0 0" }}>Retención: <b style={{ color: s.text }}>{fmtClp(f.factoring.retencion)}</b></p>
                    <p style={{ color: s.muted, margin: "2px 0 0" }}>Costo: <b style={{ color: s.text }}>{fmtClp(f.factoring.costo_factoring)}</b></p>
                    {f.factoring.estado === "vigente" && <button onClick={(e) => { e.stopPropagation(); liquidar(); }} style={{ ...btnSecondary(s), marginTop: 6 }}>Liquidar factoring</button>}
                  </div>
                )}
                <button onClick={(e) => { e.stopPropagation(); eliminar(); }} style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 6, fontSize: 12, color: "#B91C1C", background: "none", border: "none", cursor: "pointer", padding: "6px 0" }}><Trash2 size={14} /> Eliminar</button>
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

// ─── Página ───────────────────────────────────────────────────────────────────
export default function MonzaFacturasPage() {
  const s = useStyles();
  const [facturas, setFacturas] = useState<Factura[]>([]);
  const [aging, setAging] = useState<Aging | null>(null);
  const [kpis, setKpis] = useState<Kpis | null>(null);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState("");
  const [estado, setEstado] = useState("");
  const [error, setError] = useState("");
  const [modal, setModal] = useState<{ type: "crear" | "cobranza" | "factoring"; factura?: Factura } | null>(null);

  const load = useCallback(async (search?: string, est?: string) => {
    setLoading(true); setError("");
    try {
      const [fRes, kRes] = await Promise.all([
        monzaContabilidadAPI.listFacturas(est, search),
        monzaContabilidadAPI.kpis(),
      ]);
      setFacturas(fRes.data.facturas); setAging(fRes.data.antiguedad); setKpis(kRes.data);
    } catch { setError("No se pudieron cargar las facturas."); } finally { setLoading(false); }
  }, []);
  useEffect(() => { load(q || undefined, estado || undefined); /* eslint-disable-next-line */ }, [estado]);
  const reload = () => load(q || undefined, estado || undefined);
  const handleSearch = (v: string) => { setQ(v); if (v.length === 0 || v.length >= 2) load(v || undefined, estado || undefined); };

  const KPI_DEFS = kpis ? [
    { icon: DollarSign, label: "Facturado", value: fmtClp(kpis.facturado_clp), color: "var(--monza-accent)" },
    { icon: CheckCircle2, label: "Cobrado", value: fmtClp(kpis.cobrado_cliente_clp ?? kpis.cobrado_clp), color: "#15803D" },
    { icon: CreditCard, label: "Por cobrar", value: fmtClp(kpis.por_cobrar_clp), color: "#B45309" },
    { icon: AlertCircle, label: "Vencido", value: fmtClp(kpis.vencido_clp), color: "#B91C1C" },
    { icon: Landmark, label: "En factoring", value: fmtClp(kpis.en_factoring_clp), color: "#6D28D9" },
    { icon: CreditCard, label: "Anticipo factoring", value: fmtClp(kpis.anticipo_factoring_clp ?? 0), color: "#0369A1" },
  ] : [];
  const AGING_DEFS = aging ? [
    { rango: "0–30 días", monto: aging["0_30"], color: "#15803D" },
    { rango: "31–60 días", monto: aging["31_60"], color: "#B45309" },
    { rango: "61–90 días", monto: aging["61_90"], color: "#C2410C" },
    { rango: "+90 días", monto: aging["91_mas"], color: "#B91C1C" },
  ] : [];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 16, flexWrap: "wrap" }}>
        <div>
          <h1 style={{ fontSize: 22, fontWeight: 700, color: s.text, margin: 0 }}>Facturas y Cobranzas</h1>
          <p style={{ fontSize: 13, color: s.muted, margin: "4px 0 0" }}>Cuentas por cobrar · antigüedad de cartera · factoring por factura</p>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button onClick={() => setModal({ type: "crear" })} style={{ display: "flex", alignItems: "center", gap: 6, padding: "9px 14px", borderRadius: 8, border: "none", background: "var(--monza-accent)", color: "white", fontWeight: 600, fontSize: 14, cursor: "pointer", fontFamily: "inherit" }}><Plus size={16} /> Emitir factura</button>
          <button onClick={reload} style={{ display: "flex", alignItems: "center", padding: "9px 12px", borderRadius: 8, border: s.cardBd, background: s.cardBg, color: s.muted, cursor: "pointer", fontFamily: "inherit" }}><RefreshCw size={16} /></button>
        </div>
      </div>

      {kpis && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 12 }}>
          {KPI_DEFS.map(({ icon: Icon, label, value, color }) => (
            <div key={label} style={{ background: s.cardBg, border: s.cardBd, borderRadius: 14, padding: 14 }}>
              <div style={{ width: 32, height: 32, borderRadius: 10, display: "flex", alignItems: "center", justifyContent: "center", marginBottom: 8, background: s.dark ? "#0d1430" : "#F1F5F9", color }}><Icon size={16} /></div>
              <p style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: 1, color: s.muted, margin: 0 }}>{label}</p>
              <p style={{ fontSize: 19, fontWeight: 700, color, margin: "2px 0 0" }}>{value}</p>
            </div>
          ))}
        </div>
      )}

      {aging && (
        <div style={{ background: s.cardBg, border: s.cardBd, borderRadius: 14, padding: 16 }}>
          <h3 style={{ fontSize: 14, fontWeight: 600, color: s.text, margin: "0 0 12px" }}>Antigüedad de cartera (saldo por cobrar)</h3>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))", gap: 12 }}>
            {AGING_DEFS.map(r => (
              <div key={r.rango} style={{ textAlign: "center", padding: 12, borderRadius: 10, background: s.sub }}>
                <p style={{ fontSize: 12, color: s.muted, margin: 0 }}>{r.rango}</p>
                <p style={{ fontSize: 16, fontWeight: 700, color: r.color, margin: "4px 0 0" }}>{fmtClp(r.monto)}</p>
              </div>
            ))}
          </div>
        </div>
      )}

      <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
        <div style={{ position: "relative", flex: 1, minWidth: 220 }}>
          <Search size={16} style={{ position: "absolute", left: 12, top: "50%", transform: "translateY(-50%)", color: s.muted }} />
          <input style={{ width: "100%", padding: "10px 14px 10px 36px", borderRadius: 10, border: s.cardBd, background: s.cardBg, color: s.text, fontSize: 14, fontFamily: "inherit", boxSizing: "border-box" }} placeholder="Buscar por folio, cliente o cotización…" value={q} onChange={e => handleSearch(e.target.value)} />
        </div>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          {ESTADOS.map(e => (
            <button key={e} onClick={() => setEstado(e)}
              style={{ padding: "6px 14px", borderRadius: 999, fontSize: 12, fontWeight: 600, cursor: "pointer", fontFamily: "inherit",
                border: estado === e ? "1px solid var(--monza-accent)" : s.cardBd,
                background: estado === e ? "var(--monza-accent)" : s.cardBg,
                color: estado === e ? "white" : s.muted }}>{ESTADO_LABEL[e]}</button>
          ))}
        </div>
      </div>

      {error && <div style={{ borderRadius: 10, border: "1px solid #FCA5A5", background: "#FEE2E2", color: "#B91C1C", padding: "10px 14px", fontSize: 13 }}>{error}</div>}
      {loading && <div style={{ display: "flex", justifyContent: "center", padding: 60 }}><Loader2 className="animate-spin" size={28} style={{ color: "var(--monza-accent)" }} /></div>}
      {!loading && !error && facturas.length === 0 && (
        <div style={{ background: s.cardBg, border: s.cardBd, borderRadius: 14, padding: 60, textAlign: "center" }}>
          <Receipt size={40} style={{ margin: "0 auto 12px", opacity: 0.2, color: s.muted }} />
          <p style={{ fontSize: 13, fontWeight: 500, color: s.muted, margin: 0 }}>No hay facturas registradas</p>
          <p style={{ fontSize: 12, color: s.muted, margin: "4px 0 0" }}>Usa "Emitir factura" para emitir una desde un despacho.</p>
        </div>
      )}

      {!loading && facturas.length > 0 && (
        <div style={{ background: s.cardBg, border: s.cardBd, borderRadius: 14, overflow: "hidden" }}>
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", fontSize: 14, borderCollapse: "collapse" }}>
              <thead>
                <tr style={{ background: s.sub, borderBottom: s.cardBd }}>
                  {["Folio", "Cliente", "Cotización", "Emisión", "Total", "Cobrado", "Vencimiento", "Estado"].map(h => (
                    <th key={h} style={{ textAlign: "left", padding: "12px 16px", fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: 0.5, color: s.muted, whiteSpace: "nowrap" }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {facturas.map(f => (
                  <FacturaRow key={f.id} f={f} onChanged={reload}
                    onCobrar={(fa) => setModal({ type: "cobranza", factura: fa })}
                    onFactoring={(fa) => setModal({ type: "factoring", factura: fa })} />
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {modal?.type === "crear" && <CrearFacturaModal onClose={() => setModal(null)} onDone={reload} />}
      {modal?.type === "cobranza" && modal.factura && <CobranzaModal factura={modal.factura} onClose={() => setModal(null)} onDone={reload} />}
      {modal?.type === "factoring" && modal.factura && <FactoringModal factura={modal.factura} onClose={() => setModal(null)} onDone={reload} />}
    </div>
  );
}
