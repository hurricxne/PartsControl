// Página "Tesorería" (MonzaParts): el manejo del dinero real del banco en 5 pestañas.
//   1. APROBACIONES — los adelantos 50% informados por Comercial llegan acá; Tesorería
//      DA LA ORDEN (aprueba con monto/fecha/banco) → Abastecimiento queda destrabado.
//      Si la cartola ya está cargada, muestra el abono del banco que calza (sugerido).
//   2. POR PAGAR — cola de compras con saldo (buckets de vencimiento); Tesorería aprueba
//      el pago (selección múltiple → Comprobante de Egreso vía POST /tesoreria/pagos).
//   3. CONCILIACIÓN — importar cartolas (CSV/XLSX), agregar el movimiento que NO viene en
//      la cartola (cheque, efectivo, comisión) y cruzar cargos ↔ egresos de Compras /
//      abonos ↔ adelantos y cobranzas, con sugerencias.
//   4. FLUJO DE CAJA — por pagar vs por cobrar por ventana de vencimiento (NIC 7), más
//      los tres bloques que van FUERA de los buckets (retención del factor y adelantos).
//   5. CUENTAS — alta, edición (banco, alias, N° de cuenta, MONEDA) y activación de las
//      cuentas bancarias. Una moneda mal tipeada bloquea toda la conciliación automática
//      (el backend solo concilia cuentas CLP), así que tiene que poder corregirse.
// Consume monzaTesoreriaAPI.
import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import {
  Landmark, Search, Loader2, RefreshCw, CheckCircle2, X, Trash2, Upload,
  Plus, Link2, Unlink, AlertCircle, CreditCard, PiggyBank, CalendarClock, ShieldCheck,
  Banknote, Receipt, Building2, Pencil,
} from "lucide-react";
import toast from "react-hot-toast";
import { useMonzaTheme } from "./MonzaLayout";
import { hoyLocal } from "../utils/format";
import { monzaTesoreriaAPI } from "../services/monzaApi";
import type { MonzaFlujoCaja } from "../services/monzaApi";

// ─── Tipos (espejo del JSON del backend monza_tesoreria) ──────────────────────
interface Aprobacion {
  cotizacion_id: number; numero_cotizacion: string | null; cliente: string | null;
  estado_venta: string; fecha_venta: string | null; pct_adelanto: number;
  total_venta_clp: number; estado_adelanto: string;
  monto_sugerido_clp?: number;
  // Folio de la factura de ANTICIPO (vía B) que respalda esta plata ante el SII. El
  // backend lo publica DERIVADO de las facturas es_anticipo de la venta; se declara
  // opcional porque en esta cola llega solo cuando el llamador pasó las facturas.
  factura_anticipo_folio?: string | null;
  abono_sugerido?: { movimiento_id: number; fecha: string | null; monto: number; glosa: string | null } | null;
  adelanto?: {
    adelanto_id: number; monto: number; monto_aplicado: number; fecha_pago: string | null;
    banco: string | null; numero_operacion: string | null; conciliado_banco: boolean;
  };
}
// GET /tesoreria/aprobaciones. Los totales son de la COLA completa (la respuesta viene
// paginada): con ellos el encabezado dice cuántos hay de verdad, no cuántos se bajaron.
interface AprobacionesResp {
  por_aprobar: Aprobacion[]; aprobadas: Aprobacion[];
  total_por_aprobar?: number; total_aprobadas?: number;
}
interface Cuenta { id: number; banco: string; nombre: string | null; numero_cuenta: string | null; moneda: string; activo: boolean; observaciones?: string | null }
// Resumen del destino conciliado de un movimiento (egreso de Compras, adelanto 50%
// o cobranza de Facturas). Campos opcionales según `clase` — contrato en runtime.
interface Destino {
  clase: "egreso" | "adelanto" | "cobranza";
  egreso_id?: number; beneficiario?: string | null; n_compras?: number;
  adelanto_id?: number; cotizacion_id?: number; numero_cotizacion?: string | null;
  cobranza_id?: number; factura_id?: number; numero_factura?: string | null;
  cliente?: string | null; monto?: number; monto_total_clp?: number;
}
interface Movimiento {
  id: number; cuenta_id: number; cartola_id: number | null; fecha: string | null;
  glosa: string | null; tipo: string; monto: number; referencia: string | null;
  saldo: number | null; conciliado: boolean; destino: Destino | null;
}
interface Sugerencia {
  clase: string; egreso_id?: number; adelanto_id?: number; fecha?: string | null;
  fecha_pago?: string | null; monto_total_clp?: number; monto?: number;
  beneficiario?: string | null; cliente?: string | null; numero_cotizacion?: string | null;
  cobranza_id?: number; factura_id?: number; numero_factura?: string | null; medio?: string | null;
  numero_operacion?: string | null; n_compras?: number; dias_diferencia?: number;
  compras?: {
    compra_id: number; acreedor: string | null; numero_documento: string | null;
    monto_clp: number; categoria: string | null; tipo_gasto: string;
  }[];
}
// Misma lista que expone el backend (GET /tesoreria/cuentas → bancos_sugeridos).
const BANCOS_SUGERIDOS = [
  "Santander", "Banco de Chile", "BCI", "BancoEstado", "Itaú", "Scotiabank",
  "Security", "BICE", "Banco Falabella", "Banco Ripley", "Coopeuch", "Otro",
];
// El flujo de caja usa el tipo de la capa API (MonzaFlujoCaja): ahí están declarados los
// TRES bloques de fuera de los buckets. La copia local de esta pantalla solo declaraba
// `adelantos_por_aprobar` y por eso escondía la retención del factor y los adelantos ya
// recibidos sin aplicar, que el backend devuelve desde siempre.
interface Resumen {
  aprobaciones_pendientes: number; monto_aprobaciones_clp: number;
  movimientos_total: number; movimientos_conciliados: number;
  cargos_pendientes: number; abonos_pendientes: number;
  egresos_sin_conciliar: number; por_pagar_vencido_clp: number;
  // Nuevos (opcionales: toleran un backend aún sin actualizar)
  cobranzas_sin_conciliar?: number; pagos_por_aprobar?: number; monto_por_pagar_clp?: number;
  adelantos_sin_conciliar?: number;
}

// ─── Por pagar (aprobación de pagos de Compras) ───────────────────────────────
interface CompraPorPagar {
  compra_id: number; acreedor: string | null; proveedor_rut: string | null;
  numero_documento: string | null; categoria: string | null; tipo_gasto: string;
  condicion_pago: string; fecha: string | null; fecha_vencimiento: string | null;
  bucket: string; monto_total_clp: number; monto_pagado_clp: number;
  saldo_clp: number; estado_pago: string;
}
interface PorPagarResp {
  compras: CompraPorPagar[]; total: number;
  buckets: Record<string, { monto: number; n: number }>;
}
const PP_BUCKETS = ["vencido", "d0_7", "d8_30", "d31_60", "d61_mas", "sin_fecha"];

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
  return { width: "100%", padding: "8px 12px", borderRadius: 8, border: s.cardBd, background: s.inputBg, color: s.text, fontSize: 14, fontFamily: "inherit", boxSizing: "border-box" };
}
function btnPrimary(): React.CSSProperties {
  return { display: "flex", alignItems: "center", justifyContent: "center", gap: 8, padding: "9px 14px", borderRadius: 8, border: "none", background: "var(--monza-accent)", color: "white", fontWeight: 600, fontSize: 13, cursor: "pointer", fontFamily: "inherit" };
}
function btnSecondary(s: Styles): React.CSSProperties {
  return { display: "flex", alignItems: "center", justifyContent: "center", gap: 6, padding: "8px 12px", borderRadius: 8, border: s.cardBd, background: s.sub, color: s.text, fontWeight: 600, fontSize: 12, cursor: "pointer", fontFamily: "inherit" };
}
const fmtClp = (n: number | null | undefined) =>
  n == null || Number.isNaN(n) ? "—" : "$" + Math.round(n).toLocaleString("es-CL");
const fmtDate = (sd: string | null | undefined) => {
  if (!sd) return "—";
  const [y, m, d] = sd.slice(0, 10).split("-");
  return y && m && d ? `${d}-${m}-${y}` : sd;
};

// ─── Modal genérico + Field ────────────────────────────────────────────────────
function Modal({ title, wide, onClose, children }: { title: string; wide?: boolean; onClose: () => void; children: React.ReactNode }) {
  const s = useStyles();
  return (
    <div style={{ position: "fixed", inset: 0, zIndex: 300, display: "flex", alignItems: "center", justifyContent: "center", background: "rgba(0,0,0,0.6)", padding: 16 }} onClick={onClose}>
      <div style={{ width: "100%", maxWidth: wide ? 640 : 440, maxHeight: "90vh", overflowY: "auto", borderRadius: 14, border: s.cardBd, background: s.cardBg, boxShadow: "0 20px 50px rgba(0,0,0,0.4)" }} onClick={e => e.stopPropagation()}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "12px 18px", borderBottom: s.cardBd, position: "sticky", top: 0, background: s.cardBg, zIndex: 1 }}>
          <h3 style={{ margin: 0, fontSize: 14, fontWeight: 700, color: s.text }}>{title}</h3>
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

/** Devuelve SOLO el `data` de una llamada, y el `fallback` si esa llamada falla — para
 *  que un `Promise.all` de búsquedas independientes no se caiga entero por una sola.
 *  `queSe` nombra lo que no se pudo traer en el aviso al usuario (fallar en silencio
 *  haría creer que no hay coincidencias). El toast va con `id` fijo: esto corre en un
 *  typeahead y sin el id se apilaría un aviso por cada tecla. */
async function soloDatos<T>(p: Promise<{ data: T }>, fallback: T, queSe: string): Promise<T> {
  try {
    return (await p).data;
  } catch {
    toast.error(`No se pudieron buscar ${queSe} (la otra búsqueda sí se hizo)`, { id: `tes-buscar-${queSe}` });
    return fallback;
  }
}

// Tolerancia en PESOS del monto aplicado (mismo criterio que Grupo AM): un adelanto
// repartido entre varias facturas puede quedar a $1 por el redondeo del IVA y eso ya es
// "aplicado del todo", no un remanente por cobrar.
const TOL_APLICADO = 1;

// Estado de APLICACIÓN del adelanto a las facturas de la venta. El monto aplicado a
// secas no dice si la plata ya se consumió, va a medias o sigue esperando su factura;
// sin esa lectura es fácil volver a cobrarle al cliente el mismo depósito.
function AplicacionAdelanto({ a }: { a: Aprobacion }) {
  const s = useStyles();
  const ad = a.adelanto;
  if (!ad || ad.monto <= 0) return <span style={{ color: s.faint }}>—</span>;
  if (ad.monto_aplicado >= ad.monto - TOL_APLICADO)
    return <span style={{ color: "#15803D", fontWeight: 600 }}>Aplicado a factura</span>;
  if (ad.monto_aplicado > 0)
    return (
      <span style={{ color: "#B45309" }}>
        {fmtClp(ad.monto_aplicado)} · queda {fmtClp(Math.max(ad.monto - ad.monto_aplicado, 0))}
      </span>
    );
  return <span style={{ color: s.faint }}>Esperando factura</span>;
}

// ═══ PESTAÑA 1: APROBACIONES ═════════════════════════════════════════════════
function AprobarModal({ apro, onClose, onDone }: { apro: Aprobacion; onClose: () => void; onDone: () => void }) {
  const s = useStyles();
  const inp = inputBase(s);
  const sugerido = Math.round(apro.monto_sugerido_clp || 0);
  const [monto, setMonto] = useState(String(apro.abono_sugerido ? Math.round(apro.abono_sugerido.monto) : sugerido));
  // hoyLocal(): toISOString() es UTC y de noche en Chile daría el día siguiente.
  const [fecha, setFecha] = useState(apro.abono_sugerido?.fecha || hoyLocal());
  const [banco, setBanco] = useState("");
  const [op, setOp] = useState("");
  const [obs, setObs] = useState("");
  const [saving, setSaving] = useState(false);
  const submit = async () => {
    if (!monto || Number(monto) <= 0) { toast.error("Monto inválido"); return; }
    setSaving(true);
    try {
      await monzaTesoreriaAPI.aprobarAdelanto(apro.cotizacion_id, {
        monto: Number(monto), fecha_pago: fecha || undefined, banco: banco || undefined,
        numero_operacion: op || undefined, observaciones: obs || undefined,
      });
      toast.success("Adelanto aprobado — Abastecimiento destrabado"); onDone(); onClose();
    } catch (e: any) { toast.error(e?.response?.data?.detail || "No se pudo aprobar"); } finally { setSaving(false); }
  };
  return (
    <Modal title={`Aprobar adelanto · COT ${apro.numero_cotizacion}`} onClose={onClose}>
      <p style={{ margin: 0, fontSize: 12, color: s.muted }}>
        {apro.cliente || "—"} · Adelanto {apro.pct_adelanto}% — sugerido <b style={{ color: s.text }}>{fmtClp(sugerido)}</b> sobre el total {fmtClp(apro.total_venta_clp)}.
      </p>
      {apro.abono_sugerido && (
        <p style={{ margin: 0, fontSize: 12, color: "#15803D", background: "rgba(21,128,61,0.1)", padding: "8px 10px", borderRadius: 8 }}>
          <PiggyBank size={13} style={{ verticalAlign: "-2px", marginRight: 6 }} />
          Abono que calza en cartola: <b>{fmtClp(apro.abono_sugerido.monto)}</b> el {fmtDate(apro.abono_sugerido.fecha)}{apro.abono_sugerido.glosa ? ` · ${apro.abono_sugerido.glosa}` : ""}
        </p>
      )}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        <Field label="Monto recibido (CLP)"><input type="number" style={inp} value={monto} onChange={e => setMonto(e.target.value)} /></Field>
        <Field label="Fecha del pago"><input type="date" style={inp} value={fecha} onChange={e => setFecha(e.target.value)} /></Field>
        <Field label="Banco">
          <input style={inp} list="tes-bancos" value={banco} onChange={e => setBanco(e.target.value)} placeholder="Santander, BCI…" />
          <datalist id="tes-bancos">{BANCOS_SUGERIDOS.map(b => <option key={b} value={b} />)}</datalist>
        </Field>
        <Field label="N° operación"><input style={inp} value={op} onChange={e => setOp(e.target.value)} /></Field>
      </div>
      <Field label="Observaciones"><input style={inp} value={obs} onChange={e => setObs(e.target.value)} /></Field>
      <p style={{ margin: 0, fontSize: 11, color: s.faint }}>Al aprobar, Abastecimiento queda autorizado a comprar los ítems de esta venta (cortafuego del 50%).</p>
      <button onClick={submit} disabled={saving} style={{ ...btnPrimary(), width: "100%", opacity: saving ? 0.6 : 1 }}>
        {saving ? <Loader2 size={15} className="animate-spin" /> : <ShieldCheck size={15} />} Aprobar y dar la orden
      </button>
    </Modal>
  );
}

function AprobacionesTab({ onChanged }: { onChanged: () => void }) {
  const s = useStyles();
  const [porAprobar, setPorAprobar] = useState<Aprobacion[]>([]);
  const [aprobadas, setAprobadas] = useState<Aprobacion[]>([]);
  const [totales, setTotales] = useState<{ pa?: number; ap?: number }>({});
  const [loading, setLoading] = useState(true);
  const [modal, setModal] = useState<Aprobacion | null>(null);
  const load = useCallback(() => {
    setLoading(true);
    monzaTesoreriaAPI.aprobaciones()
      .then(({ data }: { data: AprobacionesResp }) => {
        setPorAprobar(data.por_aprobar || []);
        setAprobadas(data.aprobadas || []);
        setTotales({ pa: data.total_por_aprobar, ap: data.total_aprobadas });
      })
      .catch((e: any) => toast.error(e?.response?.data?.detail || "No se pudieron cargar las aprobaciones"))
      .finally(() => setLoading(false));
  }, []);
  useEffect(() => { load(); }, [load]);
  // Refresco a mano: la plata entra al banco mientras la pestaña está abierta y sin esto
  // el tesorero tiene que salir y volver para ver el abono que acaba de calzar.
  const refrescar = () => { load(); onChanged(); };

  const th: React.CSSProperties = { padding: "10px 14px", textAlign: "left", fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.5, color: s.faint, whiteSpace: "nowrap" };
  const td: React.CSSProperties = { padding: "10px 14px", whiteSpace: "nowrap", fontSize: 13 };
  if (loading) return <div style={{ display: "flex", justifyContent: "center", padding: 60 }}><Loader2 size={26} className="animate-spin" color="var(--monza-accent)" /></div>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div style={{ display: "flex", alignItems: "flex-start", gap: 10 }}>
        <div style={{ flex: 1, borderRadius: 12, border: s.cardBd, background: s.cardBg, padding: "12px 16px", fontSize: 13, color: s.muted }}>
          <ShieldCheck size={15} style={{ verticalAlign: "-3px", marginRight: 6, color: "var(--monza-accent)" }} />
          Los adelantos que Comercial informa al cerrar una venta llegan acá. <b style={{ color: s.text }}>Tesorería da la orden</b>: al aprobar, Abastecimiento queda autorizado a comprar. Ventas lo ve solo lectura.
        </div>
        <button onClick={refrescar} style={{ ...btnSecondary(s), flexShrink: 0 }} title="Volver a consultar">
          <RefreshCw size={14} />
        </button>
      </div>

      {/* Por aprobar */}
      <div style={{ borderRadius: 14, border: s.cardBd, background: s.cardBg, overflow: "hidden" }}>
        <div style={{ padding: "12px 16px", borderBottom: s.cardBd, display: "flex", alignItems: "center", gap: 8 }}>
          <AlertCircle size={15} color="#B45309" />
          <b style={{ fontSize: 13, color: s.text }}>Por aprobar ({totales.pa ?? porAprobar.length})</b>
        </div>
        {porAprobar.length === 0 ? (
          <p style={{ padding: 20, margin: 0, fontSize: 13, color: s.muted, textAlign: "center" }}>No hay adelantos pendientes de aprobación.</p>
        ) : (
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead><tr style={{ background: s.sub, borderBottom: s.cardBd }}>
                {["Venta", "Cliente", "Fecha venta", "Total venta", "% Adel.", "Monto sugerido", "Abono en cartola", ""].map(h => <th key={h} style={th}>{h}</th>)}
              </tr></thead>
              <tbody>
                {porAprobar.map(a => (
                  <tr key={a.cotizacion_id} style={{ borderBottom: s.cardBd }}>
                    <td style={{ ...td, fontFamily: "ui-monospace, monospace", fontWeight: 600, color: "var(--monza-accent)" }}>COT {a.numero_cotizacion}</td>
                    <td style={{ ...td, color: s.text, maxWidth: 180, overflow: "hidden", textOverflow: "ellipsis" }}>{a.cliente || "—"}</td>
                    <td style={{ ...td, color: s.muted }}>{fmtDate(a.fecha_venta)}</td>
                    <td style={{ ...td, color: s.text, fontWeight: 600 }}>{fmtClp(a.total_venta_clp)}</td>
                    <td style={{ ...td, color: s.muted }}>{a.pct_adelanto}%</td>
                    <td style={{ ...td, fontWeight: 700, color: "#B45309" }}>{fmtClp(a.monto_sugerido_clp)}</td>
                    <td style={td}>
                      {a.abono_sugerido ? (
                        <span style={{ background: "rgba(21,128,61,0.14)", color: "#15803D", fontSize: 11, fontWeight: 600, padding: "3px 9px", borderRadius: 999 }}>
                          ✓ {fmtClp(a.abono_sugerido.monto)} · {fmtDate(a.abono_sugerido.fecha)}
                        </span>
                      ) : (
                        <span style={{ fontSize: 11, color: s.faint }}>sin cruce aún</span>
                      )}
                    </td>
                    <td style={td}>
                      <button onClick={() => setModal(a)} style={{ ...btnPrimary(), padding: "6px 12px", fontSize: 12 }}>
                        <ShieldCheck size={13} /> Aprobar
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Aprobadas */}
      <div style={{ borderRadius: 14, border: s.cardBd, background: s.cardBg, overflow: "hidden" }}>
        <div style={{ padding: "12px 16px", borderBottom: s.cardBd, display: "flex", alignItems: "center", gap: 8 }}>
          <CheckCircle2 size={15} color="#15803D" />
          <b style={{ fontSize: 13, color: s.text }}>Aprobadas ({totales.ap ?? aprobadas.length})</b>
        </div>
        {aprobadas.length === 0 ? (
          <p style={{ padding: 20, margin: 0, fontSize: 13, color: s.muted, textAlign: "center" }}>Aún no hay adelantos aprobados.</p>
        ) : (
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead><tr style={{ background: s.sub, borderBottom: s.cardBd }}>
                {["Venta", "Cliente", "Monto aprobado", "Fecha pago", "Banco", "N° operación", "Aplicación a facturas", "Cartola"].map(h => <th key={h} style={th}>{h}</th>)}
              </tr></thead>
              <tbody>
                {aprobadas.map(a => (
                  <tr key={a.cotizacion_id} style={{ borderBottom: s.cardBd }}>
                    <td style={{ ...td, fontFamily: "ui-monospace, monospace", fontWeight: 600, color: "var(--monza-accent)" }}>
                      COT {a.numero_cotizacion}
                      {/* Respaldo tributario del adelanto (factura de anticipo, vía B): sin
                          verlo acá no se sabe si esa plata tiene documento ante el SII. */}
                      {a.factura_anticipo_folio && (
                        <span style={{ display: "block", fontFamily: "inherit", fontWeight: 400, fontSize: 11, color: s.faint }}>
                          respaldo Factura N° {a.factura_anticipo_folio}
                        </span>
                      )}
                    </td>
                    <td style={{ ...td, color: s.text, maxWidth: 180, overflow: "hidden", textOverflow: "ellipsis" }}>{a.cliente || "—"}</td>
                    <td style={{ ...td, fontWeight: 700, color: "#15803D" }}>{fmtClp(a.adelanto?.monto)}</td>
                    <td style={{ ...td, color: s.muted }}>{fmtDate(a.adelanto?.fecha_pago)}</td>
                    <td style={{ ...td, color: s.muted }}>{a.adelanto?.banco || "—"}</td>
                    <td style={{ ...td, color: s.muted }}>{a.adelanto?.numero_operacion || "—"}</td>
                    {/* El monto aplicado a secas no dice si el adelanto ya se consumió, va a
                        medias o sigue esperando su factura — y no saberlo es la fuente
                        clásica de cobrarle al cliente dos veces la misma plata. */}
                    <td style={{ ...td, color: s.muted }}><AplicacionAdelanto a={a} /></td>
                    <td style={td}>
                      {a.adelanto?.conciliado_banco
                        ? <span style={{ background: "rgba(21,128,61,0.14)", color: "#15803D", fontSize: 11, fontWeight: 600, padding: "3px 9px", borderRadius: 999 }}>Conciliado ✓</span>
                        : <span style={{ fontSize: 11, color: s.faint }}>sin conciliar</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {modal && <AprobarModal apro={modal} onClose={() => setModal(null)} onDone={() => { load(); onChanged(); }} />}
    </div>
  );
}

// ═══ PESTAÑA 2: CONCILIACIÓN ═════════════════════════════════════════════════
function SugerenciasModal({ mov, onClose, onDone }: { mov: Movimiento; onClose: () => void; onDone: () => void }) {
  const s = useStyles();
  const [sugs, setSugs] = useState<Sugerencia[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [q, setQ] = useState("");
  const [manual, setManual] = useState<Sugerencia[] | null>(null);
  const seqRef = useRef(0);  // descarta respuestas fuera de orden del typeahead
  const esCargo = mov.tipo === "cargo";
  useEffect(() => {
    monzaTesoreriaAPI.sugerencias(mov.id)
      .then(({ data }) => setSugs(data.sugerencias))
      // p.ej. 400 del backend — sin el detail se vería como "sin coincidencias"
      .catch((e: any) => toast.error(e?.response?.data?.detail || "No se pudieron cargar sugerencias"))
      .finally(() => setLoading(false));
  }, [mov.id]);
  const conciliarCon = async (sug: Sugerencia) => {
    setSaving(true);
    try {
      await monzaTesoreriaAPI.conciliar(mov.id,
        sug.clase === "egreso" ? { egreso_id: sug.egreso_id }
          : sug.clase === "cobranza" ? { cobranza_id: sug.cobranza_id }
            : { adelanto_id: sug.adelanto_id });
      toast.success("Conciliado"); onDone(); onClose();
    } catch (e: any) { toast.error(e?.response?.data?.detail || "No se pudo conciliar"); } finally { setSaving(false); }
  };
  // Búsqueda manual: cargos → egresos de Compras (?q=); abonos → cobranzas de
  // Facturas (?q=) + adelantos aprobados (el endpoint no recibe q: se filtran acá).
  const buscar = async (v: string) => {
    setQ(v);
    const my = ++seqRef.current;
    if (v.length < 2) { setManual(null); return; }
    try {
      if (esCargo) {
        const { data } = await monzaTesoreriaAPI.egresosPendientes(v);
        if (my === seqRef.current) setManual(data.egresos || []);
      } else {
        // Un catch POR LLAMADA: con el catch único de abajo, un hipo del endpoint de
        // adelantos borraba también las cobranzas que sí calzaban y el tesorero leía
        // "sin resultados" sobre un cruce que existe.
        const [cobs, adels] = await Promise.all([
          soloDatos<{ cobranzas?: Sugerencia[] }>(monzaTesoreriaAPI.cobranzasPendientes(v), {}, "cobranzas"),
          soloDatos<{ adelantos?: Sugerencia[] }>(monzaTesoreriaAPI.adelantosPendientes(), {}, "adelantos"),
        ]);
        if (my === seqRef.current) {
          const needle = v.toLowerCase();
          const adelantos = (adels.adelantos || []).filter(a =>
            `${a.numero_cotizacion || ""} ${a.cliente || ""} ${a.numero_operacion || ""}`.toLowerCase().includes(needle));
          setManual([...(cobs.cobranzas || []), ...adelantos]);
        }
      }
    } catch { if (my === seqRef.current) setManual([]); }
  };
  const card = (g: Sugerencia, key: string) => (
    <div key={key} style={{ border: s.cardBd, borderRadius: 10, padding: 12, display: "flex", alignItems: "center", gap: 12 }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        {g.clase === "egreso" ? (
          <>
            <p style={{ margin: 0, fontSize: 13, fontWeight: 600, color: s.text }}>
              Egreso #{g.egreso_id} · {fmtClp(g.monto_total_clp)} · {fmtDate(g.fecha)}
              {g.dias_diferencia != null && <span style={{ fontWeight: 400, color: s.faint }}> · {g.dias_diferencia}d de diferencia</span>}
            </p>
            <p style={{ margin: "2px 0 0", fontSize: 12, color: s.muted, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {g.beneficiario || "—"}{g.numero_operacion ? ` · ${g.numero_operacion}` : ""} · paga {g.n_compras} compra(s): {(g.compras || []).map(c => c.acreedor || c.numero_documento).filter(Boolean).slice(0, 3).join(", ")}
            </p>
          </>
        ) : g.clase === "cobranza" ? (
          <>
            <p style={{ margin: 0, fontSize: 13, fontWeight: 600, color: s.text }}>
              Factura {g.numero_factura || g.factura_id} · <span style={{ color: "#15803D" }}>{fmtClp(g.monto)}</span> · {fmtDate(g.fecha)}
              {g.dias_diferencia != null && <span style={{ fontWeight: 400, color: s.faint }}> · {g.dias_diferencia}d de diferencia</span>}
            </p>
            <p style={{ margin: "2px 0 0", fontSize: 12, color: s.muted }}>Cobranza{g.medio ? ` · ${g.medio}` : ""}{g.numero_operacion ? ` · op ${g.numero_operacion}` : ""}</p>
          </>
        ) : (
          <>
            <p style={{ margin: 0, fontSize: 13, fontWeight: 600, color: s.text }}>
              Adelanto COT {g.numero_cotizacion} · {fmtClp(g.monto)} · {fmtDate(g.fecha_pago)}
              {g.dias_diferencia != null && <span style={{ fontWeight: 400, color: s.faint }}> · {g.dias_diferencia}d de diferencia</span>}
            </p>
            <p style={{ margin: "2px 0 0", fontSize: 12, color: s.muted }}>{g.cliente || "—"}{g.numero_operacion ? ` · ${g.numero_operacion}` : ""}</p>
          </>
        )}
      </div>
      <button onClick={() => conciliarCon(g)} disabled={saving} style={{ ...btnPrimary(), padding: "7px 12px", fontSize: 12, opacity: saving ? 0.6 : 1 }}>
        <Link2 size={13} /> Conciliar
      </button>
    </div>
  );
  return (
    <Modal title={`Conciliar ${mov.tipo} · ${fmtClp(mov.monto)} · ${fmtDate(mov.fecha)}`} wide onClose={onClose}>
      <p style={{ margin: 0, fontSize: 12, color: s.muted }}>
        {mov.glosa || "(sin glosa)"} — candidatos con el mismo monto (±$1), ordenados por cercanía de fecha:
        {esCargo ? " Comprobantes de Egreso de Compras." : " Adelantos 50% aprobados y cobranzas de Facturas."}
      </p>
      {loading ? (
        <div style={{ display: "flex", justifyContent: "center", padding: 30 }}><Loader2 size={22} className="animate-spin" color="var(--monza-accent)" /></div>
      ) : sugs.length === 0 ? (
        <p style={{ margin: 0, padding: 16, fontSize: 13, color: s.muted, textAlign: "center", background: s.sub, borderRadius: 10 }}>
          No hay candidatos con ese monto. {esCargo ? "Busca el egreso abajo o registra el pago en Compras primero." : "Busca abajo la cobranza o el adelanto, o regístralos primero."}
        </p>
      ) : sugs.map((g, i) => card(g, "s" + i))}
      {!loading && (
        <>
          <div style={{ position: "relative" }}>
            <Search size={14} color={s.faint} style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)" }} />
            <input value={q} onChange={e => buscar(e.target.value)}
              placeholder={esCargo ? "Buscar egreso por beneficiario u operación…" : "Buscar cobranza (N° factura, operación, banco) o adelanto (COT, cliente)…"}
              style={{ ...inputBase(s), padding: "8px 12px 8px 32px", fontSize: 13 }} />
          </div>
          {manual && manual.map((g, i) => card(g, "m" + i))}
          {manual && manual.length === 0 && <p style={{ margin: 0, fontSize: 12, color: s.faint }}>Sin resultados.</p>}
        </>
      )}
    </Modal>
  );
}

const MONEDAS_CUENTA = ["CLP", "USD", "EUR"];

function CuentaModal({ cuenta, onClose, onDone }: { cuenta: Cuenta | null; onClose: () => void; onDone: () => void }) {
  const s = useStyles();
  const inp = inputBase(s);
  const [banco, setBanco] = useState(cuenta?.banco || "");
  const [nombre, setNombre] = useState(cuenta?.nombre || "");
  const [numero, setNumero] = useState(cuenta?.numero_cuenta || "");
  // MONEDA y ACTIVA son editables: la conciliación automática solo corre sobre cuentas
  // en CLP, así que una moneda mal tipeada la bloquea entera y hay que poder corregirla;
  // y una cuenta que se cerró en el banco se desactiva (borrarla no se puede si tiene
  // movimientos — el backend responde 409 y manda a desactivarla).
  const [moneda, setMoneda] = useState(cuenta?.moneda || "CLP");
  const [activo, setActivo] = useState(cuenta?.activo ?? true);
  const [saving, setSaving] = useState(false);
  const submit = async () => {
    if (!banco.trim()) { toast.error("Indica el banco"); return; }
    setSaving(true);
    try {
      // El PUT del backend es de reemplazo total: viaja TODO el registro (las
      // observaciones se conservan porque acá no se editan).
      const data = {
        banco, nombre: nombre || undefined, numero_cuenta: numero || undefined,
        moneda, activo, observaciones: cuenta?.observaciones ?? undefined,
      };
      if (cuenta) await monzaTesoreriaAPI.actualizarCuenta(cuenta.id, data);
      else await monzaTesoreriaAPI.crearCuenta(data);
      toast.success(cuenta ? "Cuenta actualizada" : "Cuenta creada"); onDone(); onClose();
    } catch (e: any) { toast.error(e?.response?.data?.detail || "Error"); } finally { setSaving(false); }
  };
  return (
    <Modal title={cuenta ? "Editar cuenta bancaria" : "Nueva cuenta bancaria"} onClose={onClose}>
      <Field label="Banco">
        <input style={inp} list="tes-cuenta-bancos" value={banco} onChange={e => setBanco(e.target.value)} placeholder="Santander, BCI…" />
        <datalist id="tes-cuenta-bancos">{BANCOS_SUGERIDOS.map(b => <option key={b} value={b} />)}</datalist>
      </Field>
      <Field label="Alias (opcional)"><input style={inp} value={nombre} onChange={e => setNombre(e.target.value)} placeholder="Cta corriente principal" /></Field>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 12 }}>
        <Field label="N° de cuenta (opcional)"><input style={inp} value={numero} onChange={e => setNumero(e.target.value)} /></Field>
        <Field label="Moneda">
          <select style={inp} value={moneda} onChange={e => setMoneda(e.target.value)}>
            {MONEDAS_CUENTA.map(m => <option key={m} value={m}>{m}</option>)}
          </select>
        </Field>
      </div>
      {moneda !== "CLP" && (
        <p style={{ margin: 0, fontSize: 11, color: "#B45309" }}>
          La conciliación automática compara montos en CLP: en una cuenta {moneda} queda bloqueada.
        </p>
      )}
      <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12, color: s.muted, cursor: "pointer" }}>
        <input type="checkbox" checked={activo} onChange={e => setActivo(e.target.checked)} style={{ accentColor: "var(--monza-accent)" }} />
        Cuenta activa (las inactivas no aparecen en el selector de conciliación)
      </label>
      <button onClick={submit} disabled={saving} style={{ ...btnPrimary(), width: "100%", opacity: saving ? 0.6 : 1 }}>
        {saving ? <Loader2 size={15} className="animate-spin" /> : <Building2 size={15} />} Guardar cuenta
      </button>
    </Modal>
  );
}

// ─── Modal: importar cartola (nombre del lote + confirmación explícita) ────────
// Antes la cartola se subía SOLA al elegir el archivo: no había forma de nombrar el lote
// (el backend acepta `nombre`) ni de arrepentirse tras un clic equivocado.
function ImportCartolaModal({ cuentaId, onClose, onDone }: { cuentaId: number; onClose: () => void; onDone: () => void }) {
  const s = useStyles();
  const inp = inputBase(s);
  const [file, setFile] = useState<File | null>(null);
  const [nombre, setNombre] = useState("");
  const [saving, setSaving] = useState(false);
  const submit = async () => {
    if (!file) { toast.error("Selecciona un archivo CSV o Excel"); return; }
    setSaving(true);
    try {
      const { data } = await monzaTesoreriaAPI.importarCartola(cuentaId, file, nombre || undefined);
      toast.success(`Cartola importada: ${data.n_importados} movimiento(s)`);
      // Anti-duplicados del backend: avisa cuántas filas ya existían y por qué
      if (data.warnings?.length) toast(data.warnings.join("; "));
      onDone(); onClose();
    } catch (e: any) { toast.error(e?.response?.data?.detail || "No se pudo importar la cartola"); }
    finally { setSaving(false); }
  };
  return (
    <Modal title="Importar cartola" onClose={onClose}>
      <p style={{ margin: 0, fontSize: 12, color: s.muted }}>
        Sube la cartola del banco en <b>CSV</b> o <b>Excel (.xlsx)</b>. Se detectan las columnas
        Fecha, Detalle/Glosa, Cargo/Abono (o Monto), Referencia y Saldo. Las filas que ya estén
        cargadas no se duplican.
      </p>
      <Field label="Archivo (CSV / .xlsx)">
        <input type="file" accept=".csv,.xlsx" style={inp} onChange={e => setFile(e.target.files?.[0] || null)} />
      </Field>
      <Field label="Nombre del lote (opcional)">
        <input style={inp} value={nombre} onChange={e => setNombre(e.target.value)} placeholder="Cartola julio 2026" />
      </Field>
      <button onClick={submit} disabled={saving || !file} style={{ ...btnPrimary(), width: "100%", opacity: saving || !file ? 0.6 : 1 }}>
        {saving ? <Loader2 size={15} className="animate-spin" /> : <Upload size={15} />} Importar
      </button>
    </Modal>
  );
}

// ─── Modal: movimiento bancario MANUAL ────────────────────────────────────────
// El cheque, el efectivo o la comisión que NO viene en la cartola: sin poder darlo de
// alta a mano ese movimiento no se puede conciliar nunca (endpoint POST
// /tesoreria/movimientos, que ya existía y ninguna pantalla llamaba).
function MovManualModal({ cuentaId, onClose, onDone }: { cuentaId: number; onClose: () => void; onDone: () => void }) {
  const s = useStyles();
  const inp = inputBase(s);
  // hoyLocal(): toISOString() es UTC y de noche en Chile daría el día siguiente.
  const [fecha, setFecha] = useState(hoyLocal());
  const [glosa, setGlosa] = useState("");
  const [tipo, setTipo] = useState("cargo");
  const [monto, setMonto] = useState("");
  const [referencia, setReferencia] = useState("");
  const [saving, setSaving] = useState(false);
  const submit = async () => {
    if (!monto || Number(monto) <= 0) { toast.error("Monto inválido"); return; }
    setSaving(true);
    try {
      await monzaTesoreriaAPI.crearMovimiento({
        cuenta_id: cuentaId, fecha, glosa: glosa || undefined, tipo,
        monto: Number(monto), referencia: referencia || undefined,
      });
      toast.success("Movimiento agregado"); onDone(); onClose();
    } catch (e: any) { toast.error(e?.response?.data?.detail || "No se pudo agregar el movimiento"); }
    finally { setSaving(false); }
  };
  return (
    <Modal title="Movimiento manual" onClose={onClose}>
      <p style={{ margin: 0, fontSize: 12, color: s.muted }}>
        Para lo que <b>no viene en la cartola</b>: un cheque, un pago en efectivo o una comisión del
        banco. Queda igual que un movimiento importado y se puede conciliar.
      </p>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 12 }}>
        <Field label="Fecha"><input type="date" style={inp} value={fecha} onChange={e => setFecha(e.target.value)} /></Field>
        <Field label="Tipo">
          <select style={inp} value={tipo} onChange={e => setTipo(e.target.value)}>
            <option value="cargo">Cargo (sale plata)</option>
            <option value="abono">Abono (entra plata)</option>
          </select>
        </Field>
        <Field label="Monto (CLP)"><input type="number" style={inp} value={monto} onChange={e => setMonto(e.target.value)} /></Field>
        <Field label="Referencia"><input style={inp} value={referencia} onChange={e => setReferencia(e.target.value)} /></Field>
      </div>
      <Field label="Glosa"><input style={inp} value={glosa} onChange={e => setGlosa(e.target.value)} placeholder="Cheque N° 123 · comisión mantención…" /></Field>
      <button onClick={submit} disabled={saving} style={{ ...btnPrimary(), width: "100%", opacity: saving ? 0.6 : 1 }}>
        {saving ? <Loader2 size={15} className="animate-spin" /> : <Plus size={15} />} Agregar movimiento
      </button>
    </Modal>
  );
}

function ConciliacionTab({ onChanged, onCuenta }: { onChanged: () => void; onCuenta: (id: number | "", etiqueta: string) => void }) {
  const s = useStyles();
  const [cuentas, setCuentas] = useState<Cuenta[]>([]);
  const [cuentaSel, setCuentaSel] = useState<number | "">("");
  const [movs, setMovs] = useState<Movimiento[]>([]);
  const [total, setTotal] = useState(0);
  const [estado, setEstado] = useState("");
  // Filtro cargo/abono: el backend lo acepta desde siempre (`tipo`). Cuadrar el banco es
  // mirar una columna a la vez — todos los cargos, después todos los abonos.
  const [tipo, setTipo] = useState("");
  const [q, setQ] = useState("");
  const [loading, setLoading] = useState(true);
  const [modal, setModal] = useState<
    | { type: "sugerencias"; mov: Movimiento }
    | { type: "cuenta"; cuenta: Cuenta | null }
    | { type: "import"; cuentaId: number }
    | { type: "manual"; cuentaId: number }
    | null>(null);

  const loadCuentas = useCallback(() => {
    monzaTesoreriaAPI.cuentas().then(({ data }) => {
      setCuentas(data.cuentas);
      setCuentaSel(prev => prev || (data.cuentas[0]?.id ?? ""));
    }).catch((e: any) => {
      // sin esto, un backend caído se vería como "no tienes cuentas" (engañoso)
      toast.error(e?.response?.data?.detail || "No se pudieron cargar las cuentas bancarias");
    });
  }, []);
  const loadMovs = useCallback((cId: number | "", est: string, tp: string, search: string) => {
    setLoading(true);
    monzaTesoreriaAPI.movimientos({
      cuenta_id: cId || undefined, estado: est || undefined, tipo: tp || undefined,
      q: search || undefined, page_size: 200,
    }).then(({ data }) => { setMovs(data.movimientos); setTotal(data.total); })
      .catch((e: any) => toast.error(e?.response?.data?.detail || "No se pudieron cargar los movimientos"))
      .finally(() => setLoading(false));
  }, []);
  useEffect(() => { loadCuentas(); }, [loadCuentas]);
  useEffect(() => { loadMovs(cuentaSel, estado, tipo, q); }, [cuentaSel, estado, tipo]);  // eslint-disable-line react-hooks/exhaustive-deps
  // Los KPIs del encabezado se acotan a la cuenta que se está cuadrando (sin esto mezclan
  // todas las cuentas y no dicen nada de esta). Viaja también la etiqueta: un contador
  // acotado en silencio es peor que uno global — el encabezado dice de qué cuenta habla.
  useEffect(() => {
    const c = cuentas.find(x => x.id === cuentaSel);
    onCuenta(cuentaSel, c ? `${c.banco}${c.nombre ? " · " + c.nombre : ""}` : "");
  }, [cuentaSel, cuentas]);  // eslint-disable-line react-hooks/exhaustive-deps
  const reload = () => { loadMovs(cuentaSel, estado, tipo, q); onChanged(); };

  const desconciliar = async (m: Movimiento) => {
    if (!confirm("¿Deshacer esta conciliación?")) return;
    try { await monzaTesoreriaAPI.desconciliar(m.id); toast.success("Desconciliado"); reload(); }
    catch (e: any) { toast.error(e?.response?.data?.detail || "Error"); }
  };
  const borrarMov = async (m: Movimiento) => {
    if (!confirm("¿Borrar este movimiento?")) return;
    try { await monzaTesoreriaAPI.eliminarMovimiento(m.id); toast.success("Movimiento borrado"); reload(); }
    catch (e: any) { toast.error(e?.response?.data?.detail || "Error"); }
  };

  const chip = (active: boolean): React.CSSProperties => ({
    padding: "6px 12px", borderRadius: 999, fontSize: 12, fontWeight: 600, cursor: "pointer", fontFamily: "inherit",
    border: active ? "1px solid var(--monza-accent)" : s.cardBd,
    background: active ? "var(--monza-accent)" : s.cardBg,
    color: active ? "white" : s.muted,
  });
  const th: React.CSSProperties = { padding: "10px 14px", textAlign: "left", fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.5, color: s.faint, whiteSpace: "nowrap" };
  const td: React.CSSProperties = { padding: "8px 14px", whiteSpace: "nowrap", fontSize: 13 };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      {/* Cuenta + acciones */}
      <div style={{ display: "flex", alignItems: "flex-end", gap: 10, flexWrap: "wrap" }}>
        <div style={{ minWidth: 220 }}>
          <Field label="Cuenta bancaria">
            <select style={inputBase(s)} value={cuentaSel} onChange={e => setCuentaSel(e.target.value ? Number(e.target.value) : "")}>
              {cuentas.length === 0 && <option value="">— crea una cuenta —</option>}
              {cuentas.map(c => <option key={c.id} value={c.id}>{c.banco}{c.nombre ? ` · ${c.nombre}` : ""}</option>)}
            </select>
          </Field>
        </div>
        <button onClick={() => setModal({ type: "cuenta", cuenta: null })} style={btnSecondary(s)}><Plus size={14} /> Cuenta</button>
        <button
          onClick={() => cuentaSel
            ? setModal({ type: "import", cuentaId: Number(cuentaSel) })
            : toast.error("Crea/selecciona una cuenta bancaria primero")}
          style={btnPrimary()}>
          <Upload size={15} /> Importar cartola (CSV/XLSX)
        </button>
        {/* Alta MANUAL: lo que no viene en la cartola (cheque, efectivo, comisión). */}
        <button
          onClick={() => cuentaSel
            ? setModal({ type: "manual", cuentaId: Number(cuentaSel) })
            : toast.error("Crea/selecciona una cuenta bancaria primero")}
          style={btnSecondary(s)} title="Agregar un movimiento que no viene en la cartola">
          <Plus size={14} /> Movimiento manual
        </button>
        <button onClick={reload} style={btnSecondary(s)}><RefreshCw size={14} /></button>
      </div>

      {/* Filtros */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        {[["", "Todos"], ["pendiente", "Pendientes"], ["conciliado", "Conciliados"]].map(([v, l]) => (
          <button key={v} onClick={() => setEstado(v)} style={chip(estado === v)}>{l}</button>
        ))}
        <span style={{ color: s.faint }}>·</span>
        {[["", "Cargo y abono"], ["cargo", "Cargos"], ["abono", "Abonos"]].map(([v, l]) => (
          <button key={v || "ambos"} onClick={() => setTipo(v)} style={chip(tipo === v)}>{l}</button>
        ))}
        <div style={{ position: "relative", flex: 1, minWidth: 200 }}>
          <Search size={14} color={s.faint} style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)" }} />
          <input placeholder="Buscar glosa/referencia…" value={q}
            onChange={e => { setQ(e.target.value); if (e.target.value.length === 0 || e.target.value.length >= 2) loadMovs(cuentaSel, estado, tipo, e.target.value); }}
            style={{ ...inputBase(s), padding: "8px 12px 8px 32px", fontSize: 13 }} />
        </div>
        <span style={{ fontSize: 12, color: s.faint }}>{total} movimiento(s)</span>
      </div>

      {/* Movimientos */}
      {loading ? (
        <div style={{ display: "flex", justifyContent: "center", padding: 50 }}><Loader2 size={26} className="animate-spin" color="var(--monza-accent)" /></div>
      ) : movs.length === 0 ? (
        <div style={{ borderRadius: 14, border: s.cardBd, background: s.cardBg, padding: 48, textAlign: "center" }}>
          <Landmark size={40} style={{ margin: "0 auto 12px", opacity: 0.2, color: s.muted }} />
          <p style={{ margin: 0, fontSize: 13, fontWeight: 600, color: s.muted }}>No hay movimientos</p>
          <p style={{ margin: "4px 0 0", fontSize: 12, color: s.faint }}>Importa una cartola del banco para empezar a conciliar.</p>
        </div>
      ) : (
        <div style={{ borderRadius: 14, border: s.cardBd, background: s.cardBg, overflow: "hidden" }}>
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead><tr style={{ background: s.sub, borderBottom: s.cardBd }}>
                {["Fecha", "Glosa", "Tipo", "Monto", "Referencia", "Estado / destino", ""].map(h => <th key={h} style={th}>{h}</th>)}
              </tr></thead>
              <tbody>
                {movs.map(m => (
                  <tr key={m.id} style={{ borderBottom: s.cardBd }}>
                    <td style={{ ...td, color: s.muted }}>{fmtDate(m.fecha)}</td>
                    <td style={{ ...td, color: s.text, maxWidth: 240, overflow: "hidden", textOverflow: "ellipsis" }} title={m.glosa || ""}>{m.glosa || "—"}</td>
                    <td style={td}>
                      <span style={{
                        background: m.tipo === "cargo" ? "rgba(185,28,28,0.1)" : "rgba(21,128,61,0.12)",
                        color: m.tipo === "cargo" ? "#B91C1C" : "#15803D",
                        fontSize: 11, fontWeight: 600, padding: "2px 8px", borderRadius: 999,
                      }}>{m.tipo === "cargo" ? "Cargo" : "Abono"}</span>
                    </td>
                    <td style={{ ...td, fontWeight: 700, color: m.tipo === "cargo" ? "#B91C1C" : "#15803D" }}>
                      {m.tipo === "cargo" ? "−" : "+"}{fmtClp(m.monto)}
                    </td>
                    <td style={{ ...td, color: s.muted }}>{m.referencia || "—"}</td>
                    <td style={{ ...td, maxWidth: 260, overflow: "hidden", textOverflow: "ellipsis" }}>
                      {m.conciliado ? (
                        <span style={{ fontSize: 12, color: "#15803D" }}>
                          ✓ {m.destino?.clase === "adelanto"
                            ? `Adelanto COT ${m.destino.numero_cotizacion || ""}`
                            : m.destino?.clase === "cobranza"
                              ? `Cobranza Fact. ${m.destino.numero_factura || m.destino.factura_id || ""}`
                              : m.destino?.clase === "egreso"
                                ? `Egreso #${m.destino.egreso_id} · ${m.destino.beneficiario || ""}`
                                : "Conciliado"}
                        </span>
                      ) : (
                        <span style={{ fontSize: 12, color: s.faint }}>Pendiente</span>
                      )}
                    </td>
                    <td style={{ ...td, textAlign: "right" }}>
                      {m.conciliado ? (
                        <button onClick={() => desconciliar(m)} style={{ ...btnSecondary(s), padding: "5px 10px", fontSize: 11 }} title="Deshacer conciliación">
                          <Unlink size={12} /> Desconciliar
                        </button>
                      ) : (
                        <span style={{ display: "inline-flex", gap: 6 }}>
                          <button onClick={() => setModal({ type: "sugerencias", mov: m })} style={{ ...btnPrimary(), padding: "5px 10px", fontSize: 11 }}>
                            <Link2 size={12} /> Conciliar
                          </button>
                          <button onClick={() => borrarMov(m)} style={{ background: "none", border: "none", color: "#B91C1C", cursor: "pointer", padding: 4 }} title="Borrar movimiento">
                            <Trash2 size={13} />
                          </button>
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {modal?.type === "sugerencias" && <SugerenciasModal mov={modal.mov} onClose={() => setModal(null)} onDone={reload} />}
      {modal?.type === "cuenta" && <CuentaModal cuenta={modal.cuenta} onClose={() => setModal(null)} onDone={loadCuentas} />}
      {modal?.type === "import" && <ImportCartolaModal cuentaId={modal.cuentaId} onClose={() => setModal(null)} onDone={reload} />}
      {modal?.type === "manual" && <MovManualModal cuentaId={modal.cuentaId} onClose={() => setModal(null)} onDone={reload} />}
    </div>
  );
}

// ═══ PESTAÑA: CUENTAS (alta, edición y activación de las cuentas del banco) ════
// Sin esta pestaña el CuentaModal solo se abría en modo "nueva": la moneda o el N° de
// cuenta mal tipeados no se corregían nunca, y una cuenta en USD/EUR deja la conciliación
// automática bloqueada (el backend solo compara montos en CLP).
function CuentasTab() {
  const s = useStyles();
  const [cuentas, setCuentas] = useState<Cuenta[]>([]);
  const [loading, setLoading] = useState(true);
  const [modal, setModal] = useState<{ cuenta: Cuenta | null } | null>(null);
  const load = useCallback(() => {
    setLoading(true);
    // incluir_inactivas: acá SÍ se ven las desactivadas (es la pantalla donde se reactivan).
    monzaTesoreriaAPI.cuentas(true)
      .then(({ data }) => setCuentas(data.cuentas))
      .catch((e: any) => toast.error(e?.response?.data?.detail || "No se pudieron cargar las cuentas bancarias"))
      .finally(() => setLoading(false));
  }, []);
  useEffect(() => { load(); }, [load]);
  const borrar = async (c: Cuenta) => {
    if (!confirm(`¿Borrar la cuenta ${c.banco}${c.nombre ? " · " + c.nombre : ""}?`)) return;
    try { await monzaTesoreriaAPI.eliminarCuenta(c.id); toast.success("Cuenta borrada"); load(); }
    // El backend responde 409 si la cuenta tiene movimientos (hay que desactivarla).
    catch (e: any) { toast.error(e?.response?.data?.detail || "No se pudo borrar la cuenta"); }
  };

  const th: React.CSSProperties = { padding: "10px 14px", textAlign: "left", fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.5, color: s.faint, whiteSpace: "nowrap" };
  const td: React.CSSProperties = { padding: "10px 14px", whiteSpace: "nowrap", fontSize: 13 };
  if (loading) return <div style={{ display: "flex", justifyContent: "center", padding: 60 }}><Loader2 size={26} className="animate-spin" color="var(--monza-accent)" /></div>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10, flexWrap: "wrap" }}>
        <p style={{ margin: 0, fontSize: 13, color: s.muted }}>
          Las cuentas del banco contra las que se concilia. La <b style={{ color: s.text }}>moneda</b> manda:
          la conciliación automática solo corre en CLP.
        </p>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <button onClick={() => setModal({ cuenta: null })} style={btnPrimary()}><Plus size={15} /> Nueva cuenta</button>
          <button onClick={load} style={btnSecondary(s)}><RefreshCw size={14} /></button>
        </div>
      </div>
      {cuentas.length === 0 ? (
        <div style={{ borderRadius: 14, border: s.cardBd, background: s.cardBg, padding: 48, textAlign: "center" }}>
          <Landmark size={40} style={{ margin: "0 auto 12px", opacity: 0.2, color: s.muted }} />
          <p style={{ margin: 0, fontSize: 13, fontWeight: 600, color: s.muted }}>Todavía no hay cuentas bancarias</p>
          <p style={{ margin: "4px 0 0", fontSize: 12, color: s.faint }}>Crea la primera para poder importar cartolas y conciliar.</p>
        </div>
      ) : (
        <div style={{ borderRadius: 14, border: s.cardBd, background: s.cardBg, overflow: "hidden" }}>
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead><tr style={{ background: s.sub, borderBottom: s.cardBd }}>
                {["Banco", "Alias", "N° cuenta", "Moneda", "Estado", ""].map(h => <th key={h} style={th}>{h}</th>)}
              </tr></thead>
              <tbody>
                {cuentas.map(c => (
                  <tr key={c.id} style={{ borderBottom: s.cardBd }}>
                    <td style={{ ...td, fontWeight: 600, color: s.text }}>{c.banco}</td>
                    <td style={{ ...td, color: s.muted }}>{c.nombre || "—"}</td>
                    <td style={{ ...td, fontFamily: "ui-monospace, monospace", fontSize: 12, color: s.muted }}>{c.numero_cuenta || "—"}</td>
                    <td style={td}>
                      <span style={{
                        background: (c.moneda || "CLP") === "CLP" ? "rgba(21,128,61,0.14)" : "rgba(217,119,6,0.14)",
                        color: (c.moneda || "CLP") === "CLP" ? "#15803D" : "#B45309",
                        fontSize: 11, fontWeight: 600, padding: "3px 9px", borderRadius: 999,
                      }} title={(c.moneda || "CLP") === "CLP" ? undefined : "La conciliación automática solo corre sobre cuentas CLP"}>
                        {c.moneda || "CLP"}
                      </span>
                    </td>
                    <td style={td}>
                      {c.activo
                        ? <span style={{ fontSize: 12, color: "#15803D" }}>Activa</span>
                        : <span style={{ fontSize: 12, color: s.faint }}>Inactiva</span>}
                    </td>
                    <td style={{ ...td, textAlign: "right" }}>
                      <span style={{ display: "inline-flex", gap: 6 }}>
                        <button onClick={() => setModal({ cuenta: c })} style={{ ...btnSecondary(s), padding: "5px 10px", fontSize: 11 }}>
                          <Pencil size={12} /> Editar
                        </button>
                        <button onClick={() => borrar(c)} style={{ background: "none", border: "none", color: "#B91C1C", cursor: "pointer", padding: 4 }} title="Borrar cuenta (solo si no tiene movimientos)">
                          <Trash2 size={13} />
                        </button>
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
      {modal && <CuentaModal cuenta={modal.cuenta} onClose={() => setModal(null)} onDone={load} />}
    </div>
  );
}

// ═══ PESTAÑA: POR PAGAR (Tesorería aprueba el pago → Comprobante de Egreso) ═══
const BUCKET_LABEL: Record<string, string> = {
  vencido: "Vencido", d0_7: "0–7 días", d8_30: "8–30 días",
  d31_60: "31–60 días", d61_mas: "61+ días", sin_fecha: "Sin fecha",
};

function PagoModal({ compras, onClose, onDone }: { compras: CompraPorPagar[]; onClose: () => void; onDone: () => void }) {
  const s = useStyles();
  const inp = inputBase(s);
  const [fecha, setFecha] = useState(hoyLocal());
  const [medio, setMedio] = useState("transferencia");
  const [banco, setBanco] = useState("");
  const [op, setOp] = useState("");
  // Beneficiario auto si todas las compras son del mismo acreedor
  const acreedores = useMemo(() => [...new Set(compras.map(c => c.acreedor).filter(Boolean))], [compras]);
  const [beneficiario, setBeneficiario] = useState(acreedores.length === 1 ? (acreedores[0] as string) : "");
  const [glosa, setGlosa] = useState("");
  // Monto editable por compra, con default = saldo
  const [montos, setMontos] = useState<Record<number, string>>(
    () => Object.fromEntries(compras.map(c => [c.compra_id, String(Math.round(c.saldo_clp))])));
  const [saving, setSaving] = useState(false);
  const total = compras.reduce((sum, c) => sum + (Number(montos[c.compra_id]) || 0), 0);

  const submit = async () => {
    for (const c of compras) {
      const m = Number(montos[c.compra_id]);
      if (!m || m <= 0) { toast.error(`Monto inválido para ${c.acreedor || "compra " + c.compra_id}`); return; }
      if (m > c.saldo_clp + 1) { toast.error(`El pago a ${c.acreedor || "compra " + c.compra_id} excede su saldo (${fmtClp(c.saldo_clp)})`); return; }
    }
    setSaving(true);
    try {
      await monzaTesoreriaAPI.aprobarPago({
        fecha, medio, banco: banco || undefined, numero_operacion: op || undefined,
        beneficiario: beneficiario || undefined, glosa: glosa || undefined,
        detalles: compras.map(c => ({ compra_id: c.compra_id, monto_clp: Number(montos[c.compra_id]) })),
      });
      toast.success("Pago aprobado y registrado (Comprobante de Egreso)");
      onDone(); onClose();
    } catch (e: any) { toast.error(e?.response?.data?.detail || "No se pudo registrar el pago"); } finally { setSaving(false); }
  };
  return (
    <Modal title={`Aprobar pago · ${compras.length} compra${compras.length !== 1 ? "s" : ""}`} wide onClose={onClose}>
      <div style={{ borderRadius: 10, border: s.cardBd }}>
        {compras.map((c, i) => (
          <div key={c.compra_id} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, padding: "8px 12px", borderBottom: i < compras.length - 1 ? s.cardBd : "none", fontSize: 12 }}>
            <div style={{ minWidth: 0 }}>
              <p style={{ margin: 0, fontWeight: 600, color: s.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {c.acreedor || "—"}{c.numero_documento ? ` · doc ${c.numero_documento}` : ""}
              </p>
              <p style={{ margin: "1px 0 0", color: s.faint }}>
                vence {c.fecha_vencimiento ? fmtDate(c.fecha_vencimiento) : "sin fecha"} · saldo {fmtClp(c.saldo_clp)}
              </p>
            </div>
            <input type="number" style={{ ...inp, width: 120, textAlign: "right", flexShrink: 0 }}
              value={montos[c.compra_id] ?? ""} onChange={e => setMontos(prev => ({ ...prev, [c.compra_id]: e.target.value }))} />
          </div>
        ))}
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        <Field label="Fecha del pago"><input type="date" style={inp} value={fecha} onChange={e => setFecha(e.target.value)} /></Field>
        <Field label="Medio">
          <select style={inp} value={medio} onChange={e => setMedio(e.target.value)}>
            <option value="transferencia">Transferencia</option><option value="cheque">Cheque</option>
            <option value="efectivo">Efectivo</option><option value="tarjeta">Tarjeta</option>
          </select>
        </Field>
        <Field label="Banco">
          <input style={inp} list="tes-pago-bancos" value={banco} onChange={e => setBanco(e.target.value)} />
          <datalist id="tes-pago-bancos">{BANCOS_SUGERIDOS.map(b => <option key={b} value={b} />)}</datalist>
        </Field>
        <Field label="N° operación"><input style={inp} value={op} onChange={e => setOp(e.target.value)} /></Field>
      </div>
      <Field label="Beneficiario">
        <input style={inp} value={beneficiario} onChange={e => setBeneficiario(e.target.value)}
          placeholder={acreedores.length > 1 ? "Varios acreedores (opcional)" : ""} />
      </Field>
      <Field label="Glosa"><input style={inp} value={glosa} onChange={e => setGlosa(e.target.value)} /></Field>
      <button onClick={submit} disabled={saving} style={{ ...btnPrimary(), width: "100%", opacity: saving ? 0.6 : 1 }}>
        {saving ? <Loader2 size={15} className="animate-spin" /> : <Banknote size={15} />} Aprobar y registrar pago · {fmtClp(total)}
      </button>
    </Modal>
  );
}

function PorPagarTab({ onChanged }: { onChanged: () => void }) {
  const s = useStyles();
  const [data, setData] = useState<PorPagarResp | null>(null);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState("");
  // Map id→compra (no Set): lo seleccionado sobrevive aunque el buscador lo saque
  // de la vista, y el modal de pago recibe SIEMPRE todo lo marcado.
  const [seleccion, setSeleccion] = useState<Map<number, CompraPorPagar>>(new Map());
  const [showPago, setShowPago] = useState(false);
  const load = useCallback((search: string) => {
    setLoading(true);
    monzaTesoreriaAPI.porPagar({ q: search || undefined, page_size: 300 })
      .then(({ data }) => setData(data))
      .catch((e: any) => { setData(null); toast.error(e?.response?.data?.detail || "No se pudo cargar Por pagar"); })
      .finally(() => setLoading(false));
  }, []);
  useEffect(() => { load(""); }, [load]);
  const toggleSel = (c: CompraPorPagar) => setSeleccion(prev => {
    const next = new Map(prev);
    if (next.has(c.compra_id)) next.delete(c.compra_id); else next.set(c.compra_id, c);
    return next;
  });
  const seleccionadas = [...seleccion.values()];
  const totalSel = seleccionadas.reduce((a, c) => a + c.saldo_clp, 0);

  const th: React.CSSProperties = { padding: "10px 14px", textAlign: "left", fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.5, color: s.faint, whiteSpace: "nowrap" };
  const td: React.CSSProperties = { padding: "10px 14px", whiteSpace: "nowrap", fontSize: 13 };
  const pillEstado = (estado: string): React.CSSProperties => ({
    fontSize: 11, fontWeight: 600, padding: "3px 9px", borderRadius: 999,
    background: estado === "vencido" ? "rgba(185,28,28,0.12)" : estado === "parcial" ? "rgba(217,119,6,0.14)" : "rgba(148,163,184,0.15)",
    color: estado === "vencido" ? "#B91C1C" : estado === "parcial" ? "#B45309" : s.muted,
  });

  if (loading && !data) return <div style={{ display: "flex", justifyContent: "center", padding: 60 }}><Loader2 size={26} className="animate-spin" color="var(--monza-accent)" /></div>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      {/* Buscador + chips por bucket de vencimiento */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <div style={{ position: "relative", flex: 1, minWidth: 220 }}>
          <Search size={14} color={s.faint} style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)" }} />
          <input placeholder="Buscar por acreedor, documento, RUT o categoría…" value={q}
            onChange={e => { setQ(e.target.value); if (e.target.value.length === 0 || e.target.value.length >= 2) load(e.target.value); }}
            style={{ ...inputBase(s), padding: "8px 12px 8px 32px", fontSize: 13 }} />
        </div>
        {data && PP_BUCKETS.map(b => (data.buckets[b]?.n || 0) > 0 && (
          <span key={b} style={{
            padding: "5px 10px", borderRadius: 999, fontSize: 11, fontWeight: 600,
            border: b === "vencido" ? "1px solid rgba(185,28,28,0.3)" : s.cardBd,
            background: b === "vencido" ? "rgba(185,28,28,0.1)" : s.cardBg,
            color: b === "vencido" ? "#B91C1C" : s.muted,
          }}>
            {BUCKET_LABEL[b] || b}: {fmtClp(data.buckets[b].monto)} ({data.buckets[b].n})
          </span>
        ))}
        <button onClick={() => load(q)} style={btnSecondary(s)}><RefreshCw size={14} /></button>
      </div>

      {!data || data.compras.length === 0 ? (
        <div style={{ borderRadius: 14, border: s.cardBd, background: s.cardBg, padding: 48, textAlign: "center" }}>
          <CheckCircle2 size={36} style={{ margin: "0 auto 10px", color: "rgba(21,128,61,0.4)" }} />
          <p style={{ margin: 0, fontSize: 13, color: s.muted }}>No hay compras con saldo pendiente. Todo pagado.</p>
        </div>
      ) : (
        <div style={{ borderRadius: 14, border: s.cardBd, background: s.cardBg, overflow: "hidden" }}>
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead><tr style={{ background: s.sub, borderBottom: s.cardBd }}>
                {["", "Acreedor", "Documento", "Vence", "Total", "Pagado", "Saldo", "Estado"].map((h, i) => <th key={i} style={th}>{h}</th>)}
              </tr></thead>
              <tbody>
                {data.compras.map(c => (
                  <tr key={c.compra_id} style={{ borderBottom: s.cardBd, cursor: "pointer" }} onClick={() => toggleSel(c)}>
                    <td style={td}><input type="checkbox" checked={seleccion.has(c.compra_id)} onChange={() => toggleSel(c)} onClick={e => e.stopPropagation()} style={{ accentColor: "var(--monza-accent)" }} /></td>
                    <td style={{ ...td, maxWidth: 220 }}>
                      <span style={{ display: "block", fontWeight: 600, color: s.text, overflow: "hidden", textOverflow: "ellipsis" }}>{c.acreedor || "—"}</span>
                      <span style={{ display: "block", fontSize: 11, color: s.faint, overflow: "hidden", textOverflow: "ellipsis" }}>{c.categoria || c.tipo_gasto}</span>
                    </td>
                    <td style={{ ...td, fontFamily: "ui-monospace, monospace", fontSize: 12, color: s.muted }}>{c.numero_documento || "—"}</td>
                    <td style={{ ...td, color: c.bucket === "vencido" ? "#B91C1C" : s.muted, fontWeight: c.bucket === "vencido" ? 600 : 400 }}>
                      {c.fecha_vencimiento ? fmtDate(c.fecha_vencimiento) : "—"}
                    </td>
                    <td style={{ ...td, color: s.muted }}>{fmtClp(c.monto_total_clp)}</td>
                    <td style={{ ...td, color: s.muted }}>{fmtClp(c.monto_pagado_clp)}</td>
                    <td style={{ ...td, fontWeight: 700, color: s.text }}>{fmtClp(c.saldo_clp)}</td>
                    <td style={td}><span style={pillEstado(c.estado_pago)}>{c.estado_pago}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Barra flotante de selección */}
      {seleccion.size > 0 && (
        <div style={{ position: "sticky", bottom: 12, display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, borderRadius: 14, border: s.cardBd, background: s.cardBg, padding: "12px 16px", boxShadow: "0 10px 30px rgba(0,0,0,0.25)" }}>
          <p style={{ margin: 0, fontSize: 13, color: s.text }}>
            <b>{seleccion.size}</b> compra{seleccion.size !== 1 ? "s" : ""} seleccionada{seleccion.size !== 1 ? "s" : ""} · saldo {fmtClp(totalSel)}
          </p>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <button onClick={() => setSeleccion(new Map())} style={{ ...btnSecondary(s), padding: "6px 12px", fontSize: 12 }}>Limpiar</button>
            <button onClick={() => setShowPago(true)} style={{ ...btnPrimary(), padding: "7px 14px", fontSize: 12 }}><Banknote size={14} /> Aprobar pago</button>
          </div>
        </div>
      )}

      {showPago && seleccionadas.length > 0 && (
        <PagoModal compras={seleccionadas} onClose={() => setShowPago(false)}
          onDone={() => { setSeleccion(new Map()); load(q); onChanged(); }} />
      )}
    </div>
  );
}

// ═══ PESTAÑA 4: FLUJO DE CAJA ════════════════════════════════════════════════

function FlujoCajaTab() {
  const s = useStyles();
  const [fc, setFc] = useState<MonzaFlujoCaja | null>(null);
  const [loading, setLoading] = useState(true);
  const load = useCallback(() => {
    setLoading(true);
    monzaTesoreriaAPI.flujoCaja().then(({ data }) => setFc(data))
      .catch((e: any) => toast.error(e?.response?.data?.detail || "No se pudo cargar el flujo de caja"))
      .finally(() => setLoading(false));
  }, []);
  useEffect(() => { load(); }, [load]);
  if (loading) return <div style={{ display: "flex", justifyContent: "center", padding: 60 }}><Loader2 size={26} className="animate-spin" color="var(--monza-accent)" /></div>;
  if (!fc) return null;
  const th: React.CSSProperties = { padding: "10px 14px", textAlign: "left", fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.5, color: s.faint, whiteSpace: "nowrap" };
  const td: React.CSSProperties = { padding: "10px 14px", whiteSpace: "nowrap", fontSize: 13, textAlign: "right" };
  // Los TRES bloques de FUERA de los buckets (el backend los devuelve desde siempre; el
  // fallback es un cinturón: un despliegue a medias no debe tumbar la pestaña entera).
  const cero = { n: 0, monto: 0 };
  const factoring = fc.retenciones_factoring ?? cero;
  const adelPorAprobar = fc.adelantos_por_aprobar ?? cero;
  const adelSinAplicar = fc.adelantos_recibidos_sin_aplicar ?? cero;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <div style={{ display: "flex", alignItems: "flex-start", gap: 10 }}>
        <div style={{ flex: 1, borderRadius: 12, border: s.cardBd, background: s.cardBg, padding: "12px 16px", fontSize: 13, color: s.muted }}>
          <CalendarClock size={15} style={{ verticalAlign: "-3px", marginRight: 6, color: "var(--monza-accent)" }} />
          Proyección de caja por ventana de vencimiento: lo que hay que <b style={{ color: "#B91C1C" }}>pagar</b> (Compras) vs lo que va a <b style={{ color: "#15803D" }}>entrar</b> (facturas por cobrar).
        </div>
        <button onClick={load} style={{ ...btnSecondary(s), flexShrink: 0 }} title="Volver a calcular"><RefreshCw size={14} /></button>
      </div>
      <div style={{ borderRadius: 14, border: s.cardBd, background: s.cardBg, overflow: "hidden" }}>
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead><tr style={{ background: s.sub, borderBottom: s.cardBd }}>
              <th style={th}>Ventana</th>
              <th style={{ ...th, textAlign: "right" }}>Por pagar (Compras)</th>
              <th style={{ ...th, textAlign: "right" }}>Por cobrar (Facturas)</th>
              <th style={{ ...th, textAlign: "right" }}>Neto</th>
            </tr></thead>
            <tbody>
              {fc.buckets.map(b => (
                <tr key={b} style={{ borderBottom: s.cardBd, background: b === "vencido" ? "rgba(185,28,28,0.05)" : "transparent" }}>
                  <td style={{ padding: "10px 14px", fontSize: 13, fontWeight: 600, color: b === "vencido" ? "#B91C1C" : s.text }}>{BUCKET_LABEL[b] || b}</td>
                  <td style={{ ...td, color: "#B91C1C" }}>{fmtClp(fc.por_pagar[b]?.monto)} <span style={{ color: s.faint, fontSize: 11 }}>({fc.por_pagar[b]?.n})</span></td>
                  <td style={{ ...td, color: "#15803D" }}>{fmtClp(fc.por_cobrar[b]?.monto)} <span style={{ color: s.faint, fontSize: 11 }}>({fc.por_cobrar[b]?.n})</span></td>
                  <td style={{ ...td, fontWeight: 700, color: (fc.neto[b] || 0) >= 0 ? "#15803D" : "#B91C1C" }}>{fmtClp(fc.neto[b])}</td>
                </tr>
              ))}
              <tr style={{ background: s.sub, fontWeight: 700 }}>
                <td style={{ padding: "10px 14px", fontSize: 13, color: s.text }}>TOTAL</td>
                <td style={{ ...td, color: "#B91C1C" }}>{fmtClp(fc.buckets.reduce((a, b) => a + (fc.por_pagar[b]?.monto || 0), 0))}</td>
                <td style={{ ...td, color: "#15803D" }}>{fmtClp(fc.buckets.reduce((a, b) => a + (fc.por_cobrar[b]?.monto || 0), 0))}</td>
                <td style={{ ...td, color: s.text }}>{fmtClp(fc.buckets.reduce((a, b) => a + (fc.neto[b] || 0), 0))}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
      {/* Los tres bloques que NO son entradas futuras y por eso van FUERA de los buckets.
          La pantalla los escondía aunque el backend los devuelve: sin la retención del
          factor, la caja de una factura factorizada desaparecía del tablero (no está en
          "por cobrar" porque el cliente le paga al factor), y sin los adelantos ya
          recibidos no se explica por qué las próximas facturas nacen con menos saldo. */}
      <div style={{ borderRadius: 12, border: s.cardBd, background: s.cardBg, padding: "12px 16px", display: "flex", flexDirection: "column", gap: 6, fontSize: 12, color: s.muted }}>
        <span style={{ fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.5, color: s.faint }}>
          Fuera de los buckets
        </span>
        <span>
          <Receipt size={12} style={{ verticalAlign: "-2px", marginRight: 6, color: "#6D28D9" }} />
          Las facturas <b style={{ color: s.text }}>factorizadas</b> no están en "por cobrar" (el cliente le paga al factor):
          su caja pendiente es la <b style={{ color: "#6D28D9" }}>retención</b> — {factoring.n} operación{factoring.n !== 1 ? "es" : ""} vigente{factoring.n !== 1 ? "s" : ""} por <b style={{ color: "#6D28D9" }}>{fmtClp(factoring.monto)}</b>, que libera el factor al liquidar.
        </span>
        {adelPorAprobar.n > 0 && (
          <span>
            <ShieldCheck size={12} style={{ verticalAlign: "-2px", marginRight: 6, color: "#B45309" }} />
            <b style={{ color: "#B45309" }}>{adelPorAprobar.n} adelanto(s) por aprobar</b> por {fmtClp(adelPorAprobar.monto)} — todavía no son caja segura (Tesorería no confirmó la plata).
          </span>
        )}
        {adelSinAplicar.n > 0 && (
          <span>
            <PiggyBank size={12} style={{ verticalAlign: "-2px", marginRight: 6, color: "#15803D" }} />
            <b style={{ color: "#15803D" }}>{adelSinAplicar.n} adelanto(s) recibidos sin aplicar</b> por {fmtClp(adelSinAplicar.monto)} — plata YA en el banco: las próximas facturas de esas ventas nacerán con ese monto descontado.
          </span>
        )}
      </div>
      <p style={{ margin: 0, fontSize: 11, color: s.faint }}>Solo lectura: se calcula en vivo desde Compras y Facturas (NIC 7 · actividades de operación).</p>
    </div>
  );
}

// ═══ PÁGINA ══════════════════════════════════════════════════════════════════
export default function MonzaTesoreriaPage() {
  const s = useStyles();
  const [tab, setTab] = useState<"aprobaciones" | "porpagar" | "conciliacion" | "flujo" | "cuentas">("aprobaciones");
  const [resumen, setResumen] = useState<Resumen | null>(null);
  // Cuenta que se está cuadrando en Conciliación: acota los contadores de movimientos de
  // los KPIs a ESA cuenta (los de Compras/Facturas son globales por naturaleza). Sin esto
  // los KPIs mezclaban todas las cuentas y no decían nada de la que se está revisando.
  const [cuentaKpi, setCuentaKpi] = useState<number | "">("");
  const [cuentaKpiLabel, setCuentaKpiLabel] = useState("");
  const loadResumen = useCallback((cuentaId: number | "") => {
    monzaTesoreriaAPI.resumen(cuentaId || undefined).then(({ data }) => setResumen(data)).catch(() => {});
  }, []);
  useEffect(() => { loadResumen(cuentaKpi); }, [loadResumen, cuentaKpi]);
  const refrescarResumen = useCallback(() => loadResumen(cuentaKpi), [loadResumen, cuentaKpi]);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 16, flexWrap: "wrap" }}>
        <div style={{ minWidth: 0 }}>
          <h1 style={{ margin: 0, fontSize: 24, fontWeight: 700, display: "flex", alignItems: "center", gap: 8, color: s.text }}>
            <Landmark size={24} color="var(--monza-accent)" /> Tesorería
          </h1>
          <p style={{ margin: "4px 0 0", fontSize: 13, color: s.muted }}>
            Aprobación de adelantos y pagos · conciliación bancaria · flujo de caja
            {/* Los contadores de movimientos/cargos/abonos quedan acotados a la cuenta que
                se está cuadrando: decirlo evita leer un KPI parcial como si fuera el total. */}
            {cuentaKpiLabel && <span style={{ color: s.faint }}> · KPIs de conciliación de <b>{cuentaKpiLabel}</b></span>}
          </p>
        </div>
      </div>

      {/* KPIs */}
      {resumen && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 12 }}>
          {[
            { icon: ShieldCheck, label: "Adelantos por aprobar", value: `${resumen.aprobaciones_pendientes} · ${fmtClp(resumen.monto_aprobaciones_clp)}`, color: "#B45309" },
            // Nuevos KPIs (solo si el backend ya los entrega)
            ...(resumen.monto_por_pagar_clp != null
              ? [{ icon: Banknote, label: "Por pagar (saldo)", value: fmtClp(resumen.monto_por_pagar_clp), color: "var(--monza-accent)" }] : []),
            { icon: Landmark, label: "Cargos sin conciliar", value: String(resumen.cargos_pendientes), color: "#B91C1C" },
            { icon: PiggyBank, label: "Abonos sin conciliar", value: String(resumen.abonos_pendientes), color: "#15803D" },
            { icon: CreditCard, label: "Egresos sin conciliar", value: String(resumen.egresos_sin_conciliar), color: "var(--monza-accent)" },
            ...(resumen.cobranzas_sin_conciliar != null
              ? [{ icon: Receipt, label: "Cobranzas sin conciliar", value: String(resumen.cobranzas_sin_conciliar), color: "#15803D" }] : []),
            ...(resumen.adelantos_sin_conciliar != null
              ? [{ icon: ShieldCheck, label: "Adelantos sin conciliar", value: String(resumen.adelantos_sin_conciliar), color: "#B45309" }] : []),
            { icon: AlertCircle, label: "Por pagar vencido", value: fmtClp(resumen.por_pagar_vencido_clp), color: "#B91C1C" },
          ].map(k => (
            <div key={k.label} style={{ background: s.cardBg, border: s.cardBd, borderRadius: 14, padding: 14 }}>
              <div style={{ width: 32, height: 32, borderRadius: 10, display: "flex", alignItems: "center", justifyContent: "center", marginBottom: 8, background: s.dark ? "#0d1430" : "#F1F5F9", color: k.color }}><k.icon size={16} /></div>
              <p style={{ margin: 0, fontSize: 10, textTransform: "uppercase", letterSpacing: 0.8, color: s.faint }}>{k.label}</p>
              <p style={{ margin: "2px 0 0", fontSize: 17, fontWeight: 700, color: k.color }}>{k.value}</p>
            </div>
          ))}
        </div>
      )}

      {/* Tabs */}
      <div style={{ display: "flex", alignItems: "center", gap: 4, borderBottom: s.cardBd, overflowX: "auto" }}>
        {([["aprobaciones", "Aprobaciones"], ["porpagar", "Por pagar"], ["conciliacion", "Conciliación bancaria"], ["flujo", "Flujo de caja"], ["cuentas", "Cuentas"]] as const).map(([k, lbl]) => (
          <button key={k} onClick={() => setTab(k)}
            style={{
              padding: "9px 16px", fontSize: 13, fontWeight: 600, cursor: "pointer", fontFamily: "inherit",
              background: "none", border: "none", marginBottom: -1, whiteSpace: "nowrap",
              borderBottom: tab === k ? "2px solid var(--monza-accent)" : "2px solid transparent",
              color: tab === k ? "var(--monza-accent)" : s.muted,
            }}>{lbl}</button>
        ))}
      </div>

      {tab === "aprobaciones" && <AprobacionesTab onChanged={refrescarResumen} />}
      {tab === "porpagar" && <PorPagarTab onChanged={refrescarResumen} />}
      {tab === "conciliacion" && (
        <ConciliacionTab onChanged={refrescarResumen}
          onCuenta={(id, etiqueta) => { setCuentaKpi(id); setCuentaKpiLabel(etiqueta); }} />
      )}
      {tab === "flujo" && <FlujoCajaTab />}
      {tab === "cuentas" && <CuentasTab />}
    </div>
  );
}
