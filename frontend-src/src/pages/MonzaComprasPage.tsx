// Página "Compras y Cuentas por Pagar" (MonzaParts, AP NIIF/NIC 7): registra
// compras/gastos, los clasifica por tipo de gasto e imputa a una cuenta NIIF, lleva su
// condición de pago (contado = se paga al crear / crédito = queda por pagar) y su estado,
// con KPIs + antigüedad de cartera. Pestaña "Costos de embarque": los gastos anotados en
// Embarques Pricing se reflejan acá automáticamente y se registran como compra con 1 clic
// (sin digitar de nuevo, sin duplicar). Consume monzaComprasAPI.
import { useState, useEffect, useCallback } from "react";
import {
  Wallet, Plus, Search, AlertCircle, CheckCircle2, DollarSign,
  Loader2, RefreshCw, ChevronDown, ChevronUp, CreditCard, X, Trash2, Ban, Ship, Truck,
} from "lucide-react";
import toast from "react-hot-toast";
import { useMonzaTheme } from "./MonzaLayout";
import { hoyLocal } from "../utils/format";
import { monzaComprasAPI } from "../services/monzaApi";
import type { MonzaOcNacional } from "../services/monzaApi";

// ─── Tipos (espejo del JSON del backend monza_compras_contab) ─────────────────
interface Pago {
  id: number; egreso_id: number; monto_clp: number; fecha: string | null;
  medio: string | null; banco: string | null; numero_operacion: string | null;
  conciliado: boolean; fecha_mov_bancario: string | null; n_compras: number;
}
// Línea de costeo por ítem de una compra NACIONAL (monza_cont_compra_item): la
// factura ES el costo de esos repuestos; costo por ítem = NETO en CLP (el IVA es
// crédito fiscal, no capitaliza). ADAPTACIÓN Monza: sin oc_proveedor_item_id (el
// vínculo ítem↔OC es directo, no hay tabla de asignación).
interface CompraItem {
  id: number; item_cotizacion_id: number | null;
  numero_parte: string | null; descripcion: string | null;
  cantidad: number; precio_unit: number;
  costo_unit_clp: number; costo_total_clp: number;
}
interface Compra {
  id: number; origen: string; tipo_gasto: string; tipo_gasto_label: string;
  categoria: string | null; cuenta_contable_id: number | null;
  cuenta_codigo: string | null; cuenta_nombre: string | null; es_anticipo: boolean;
  proveedor_id: number | null; acreedor: string | null; proveedor_rut: string | null;
  fecha: string | null; referencia: string | null; descripcion: string | null;
  numero_documento: string | null; tipo_doc: string; moneda: string; tc: number;
  monto_neto: number; iva: number; monto_total: number; monto_total_clp: number;
  condicion_pago: string; plazo_dias: number | null; fecha_vencimiento: string | null;
  estado_pago: string; monto_pagado_clp: number; saldo_clp: number; semaforo: string;
  dias_vencimiento: number | null; anulado: boolean; motivo_anulacion: string | null;
  embarque_id: number | null; emb_pricing_gasto_id: number | null;
  oc_proveedor_id: number | null;
  observaciones: string | null; pagos: Pago[]; items: CompraItem[];
}
interface Antiguedad { "0_30": number; "31_60": number; "61_90": number; "91_mas": number }
interface Kpis {
  n_compras: number; total_comprado_clp: number; pagado_clp: number;
  por_pagar_clp: number; vencido_clp: number; por_tipo: Record<string, number>;
}
interface PlanCuenta { id: number; codigo: string; nombre: string; clase: string | null; grupo: string | null }
interface Catalogos {
  tipos_gasto: { value: string; label: string }[];
  categorias_sugeridas: string[];
  proveedores: { id: number; nombre: string; moneda: string | null; pais: string | null }[];
  plan_cuentas: PlanCuenta[];
  cuenta_default_por_tipo: Record<string, number | null>;
}
interface CostoEmbarque {
  id: number; embarque_id: number; embarque_numero: string | null; tipo: string;
  glosa: string; acreedor: string | null; monto_neto: number; iva: number;
  monto_total: number; nro_factura: string | null; fecha_factura: string | null;
  banco: string | null; capitaliza: boolean; compra_id: number | null;
}
interface Prefill {
  origen: string; tipo_gasto: string; monto_neto: number; iva: number;
  numero_documento: string | null; referencia: string | null; acreedor: string | null;
  emb_pricing_gasto_id: number; embarque_id: number;
}

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
  return { display: "flex", alignItems: "center", justifyContent: "center", gap: 6, padding: "9px 12px", borderRadius: 8, border: s.cardBd, background: s.sub, color: s.text, fontWeight: 600, fontSize: 13, cursor: "pointer", fontFamily: "inherit" };
}
const fmtClp = (n: number | null | undefined) =>
  n == null || Number.isNaN(n) ? "—" : "$" + Math.round(n).toLocaleString("es-CL");
const fmtDate = (s: string | null | undefined) => {
  if (!s) return "—";
  const [y, m, d] = s.slice(0, 10).split("-");
  return y && m && d ? `${d}-${m}-${y}` : s;
};

const PAGO_PILL: Record<string, { bg: string; color: string; label: string }> = {
  pendiente: { bg: "rgba(37,99,235,0.12)", color: "#1D4ED8", label: "Pendiente" },
  parcial:   { bg: "rgba(217,119,6,0.14)", color: "#B45309", label: "Pago parcial" },
  pagado:    { bg: "rgba(21,128,61,0.14)", color: "#15803D", label: "Pagado" },
  vencido:   { bg: "rgba(185,28,28,0.12)", color: "#B91C1C", label: "Vencido" },
  anulado:   { bg: "rgba(148,163,184,0.15)", color: "#64748B", label: "Anulado" },
};
const TIPOS = ["", "cogs", "gasto_operacional", "gasto_no_operacional", "otros"];
const TIPO_LABEL: Record<string, string> = { "": "Todos", cogs: "Costo de venta", gasto_operacional: "Operacional", gasto_no_operacional: "No operacional", otros: "Otros" };
const ESTADOS = ["", "pendiente", "parcial", "pagado", "vencido"];
const ESTADO_LABEL: Record<string, string> = { "": "Todos", pendiente: "Pendiente", parcial: "Parcial", pagado: "Pagado", vencido: "Vencido" };
const TIPO_COLOR: Record<string, string> = { cogs: "#7C3AED", gasto_operacional: "#1D4ED8", gasto_no_operacional: "#C2410C", otros: "#64748B" };

// ─── Modal genérico + Field ────────────────────────────────────────────────────
function Modal({ title, wide, onClose, children }: { title: string; wide?: boolean; onClose: () => void; children: React.ReactNode }) {
  const s = useStyles();
  return (
    <div style={{ position: "fixed", inset: 0, zIndex: 300, display: "flex", alignItems: "center", justifyContent: "center", background: "rgba(0,0,0,0.6)", padding: 16 }} onClick={onClose}>
      <div style={{ width: "100%", maxWidth: wide ? 680 : 440, maxHeight: "90vh", overflowY: "auto", borderRadius: 14, border: s.cardBd, background: s.cardBg, boxShadow: "0 20px 50px rgba(0,0,0,0.4)" }} onClick={e => e.stopPropagation()}>
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

// ─── Modal: registrar compra (con opción de pagarla al tiro) ──────────────────
function RegistrarCompraModal({ catalogos, prefill, onClose, onDone }: {
  catalogos: Catalogos | null; prefill?: Prefill | null; onClose: () => void; onDone: () => void;
}) {
  const s = useStyles();
  const inp = inputBase(s);
  const hoy = hoyLocal();  // fecha LOCAL (toISOString es UTC: de noche daría mañana)
  const [tipoGasto, setTipoGasto] = useState(prefill?.tipo_gasto || "gasto_operacional");
  const [categoria, setCategoria] = useState(prefill ? "Gastos de importación" : "");
  const [cuentaId, setCuentaId] = useState<number | "">("");
  const [esAnticipo, setEsAnticipo] = useState(false);
  const [proveedorId, setProveedorId] = useState<number | "">("");
  const [acreedor, setAcreedor] = useState(prefill?.acreedor || "");
  const [rut, setRut] = useState("");
  const [fecha, setFecha] = useState(hoy);
  const [numDoc, setNumDoc] = useState(prefill?.numero_documento || "");
  const [tipoDoc, setTipoDoc] = useState("factura");
  const [descripcion, setDescripcion] = useState("");
  const [referencia, setReferencia] = useState(prefill?.referencia || "");
  const [moneda, setMoneda] = useState("CLP");
  const [tc, setTc] = useState("");
  const [neto, setNeto] = useState(prefill ? String(Math.round(prefill.monto_neto)) : "");
  const [afectoIva, setAfectoIva] = useState(prefill ? prefill.iva > 0 : true);
  const [condicion, setCondicion] = useState<"contado" | "credito">("credito");
  const [plazo, setPlazo] = useState("30");
  const [pagoMedio, setPagoMedio] = useState("transferencia");
  const [pagoBanco, setPagoBanco] = useState("");
  const [pagoFechaBanco, setPagoFechaBanco] = useState(hoy);
  const [saving, setSaving] = useState(false);

  // Compra NACIONAL con detalle de ítems: la factura ES el costo de esos repuestos
  // (neto CLP por ítem; el IVA es crédito fiscal, no capitaliza). El backend valida
  // que la cantidad costeada no supere lo recibido en bodega y que Σ ≤ neto.
  // Con prefill de embarque no aplica (ese flujo es el reflejo de Embarques Pricing).
  const [origenTipo, setOrigenTipo] = useState<"gasto" | "nacional">("gasto");
  const [ocNacionales, setOcNacionales] = useState<MonzaOcNacional[]>([]);
  const [ocpSel, setOcpSel] = useState<number | "">("");
  const [lineItems, setLineItems] = useState<Record<number, { incluir: boolean; cantidad: string; precio_unit: string }>>({});
  const esNacional = origenTipo === "nacional" && !prefill;
  const ocSel = ocNacionales.find(o => o.oc_proveedor_id === ocpSel) || null;

  const origen = prefill?.origen || (esNacional ? "NACIONAL" : "MANUAL");
  // Cuenta sugerida según (origen, tipo de gasto): nacional → Existencias (1.3.01)
  // vía la clave NACIONAL|cogs del backend.
  useEffect(() => {
    const def = catalogos?.cuenta_default_por_tipo?.[`${origen}|${tipoGasto}`];
    if (def) setCuentaId(def);
  }, [tipoGasto, catalogos, origen]);

  // Nacional fuerza costo de venta + CLP (el IVA no capitaliza en la compra nacional).
  useEffect(() => {
    if (esNacional) { setTipoGasto("cogs"); setMoneda("CLP"); }
  }, [esNacional]);

  // Cargar el catálogo de OC nacionales al entrar al modo nacional (una sola vez).
  useEffect(() => {
    if (esNacional && ocNacionales.length === 0) {
      monzaComprasAPI.ocNacionales()
        .then(({ data }) => setOcNacionales(data.ocs))
        .catch((e: any) => toast.error(e?.response?.data?.detail || "No se pudieron cargar las OC nacionales"));
    }
  }, [esNacional]);  // eslint-disable-line react-hooks/exhaustive-deps

  // Al elegir OC nacional: precarga ítems (cantidad = disponible a costear) y el acreedor.
  useEffect(() => {
    if (!ocSel) { setLineItems({}); return; }
    const init: Record<number, { incluir: boolean; cantidad: string; precio_unit: string }> = {};
    ocSel.items.forEach(it => {
      init[it.item_cotizacion_id] = {
        incluir: it.disponible_costear > 0,
        cantidad: it.disponible_costear > 0 ? String(it.disponible_costear) : "",
        precio_unit: "",
      };
    });
    setLineItems(init);
    if (ocSel.proveedor) setAcreedor(ocSel.proveedor);
  }, [ocpSel, ocNacionales]);  // eslint-disable-line react-hooks/exhaustive-deps
  const cuentasPorClase: Record<string, PlanCuenta[]> = {};
  for (const c of catalogos?.plan_cuentas || []) {
    const k = c.clase || "Otras";
    if (!cuentasPorClase[k]) cuentasPorClase[k] = [];
    cuentasPorClase[k].push(c);
  }

  const netoN = Number(neto) || 0;
  const ivaN = prefill ? Math.round(prefill.iva) : (afectoIva ? Math.round(netoN * 0.19) : 0);
  const totalN = netoN + ivaN;
  const tcN = moneda === "CLP" ? 1 : (Number(tc) || 0);
  const totalClp = Math.round(totalN * tcN);

  // Σ de las líneas costeadas incluidas (cantidad × costo unit neto CLP). El backend
  // permite cobertura PARCIAL (Σ < neto); solo se bloquea Σ > neto (+$1 de redondeo).
  const sumaLineas = ocSel ? ocSel.items.reduce((acc, it) => {
    const li = lineItems[it.item_cotizacion_id];
    if (!li || !li.incluir) return acc;
    return acc + (Number(li.cantidad) || 0) * (Number(li.precio_unit) || 0);
  }, 0) : 0;
  const sumaExcedeNeto = esNacional && netoN > 0 && Math.round(sumaLineas) > netoN + 1;

  // Al elegir un proveedor del catálogo se trae su nombre Y SU MONEDA: sin la moneda, un
  // proveedor en USD dejaba el formulario en CLP y la compra se contabilizaba a tipo de
  // cambio 1 (el monto en dólares entraba como si fueran pesos). No aplica en la compra
  // nacional ni con prefill de embarque, donde la moneda está fija en CLP a propósito.
  const onProveedor = (id: number | "") => {
    setProveedorId(id);
    const p = catalogos?.proveedores.find(x => x.id === id);
    if (!p) return;
    setAcreedor(p.nombre);
    if (p.moneda && !prefill && !esNacional) setMoneda(p.moneda);
  };

  const submit = async () => {
    if (!acreedor.trim()) { toast.error("Indica el proveedor/acreedor"); return; }
    // Con prefill de embarque el neto viene bloqueado y puede ser 0 (gasto solo-IVA,
    // ej. IVA importación): basta que neto+IVA > 0. Manual: neto > 0 obligatorio.
    if (prefill ? (netoN + ivaN) <= 0 : netoN <= 0) { toast.error("El monto debe ser mayor a 0"); return; }
    if (moneda !== "CLP" && tcN <= 0) { toast.error("Indica el tipo de cambio"); return; }

    // Detalle por ítem de la compra NACIONAL (validación espejo del backend).
    let items: { item_cotizacion_id: number; numero_parte?: string | null; descripcion?: string | null; cantidad: number; precio_unit: number }[] | undefined;
    if (esNacional) {
      if (!ocpSel) { toast.error("Selecciona la OC-Proveedor nacional"); return; }
      const incluidos = (ocSel?.items || []).filter(it => {
        const li = lineItems[it.item_cotizacion_id];
        return li && li.incluir && Number(li.cantidad) > 0;
      });
      if (incluidos.length === 0) { toast.error("Agrega al menos un ítem con cantidad"); return; }
      if (sumaExcedeNeto) { toast.error("La suma de líneas costeadas supera el neto de la factura"); return; }
      items = incluidos.map(it => {
        const li = lineItems[it.item_cotizacion_id];
        return {
          item_cotizacion_id: it.item_cotizacion_id,
          numero_parte: it.numero_parte,
          descripcion: it.descripcion,
          cantidad: Number(li.cantidad),
          precio_unit: Number(li.precio_unit) || 0,
        };
      });
    }

    setSaving(true);
    try {
      await monzaComprasAPI.crear({
        tipo_gasto: tipoGasto, categoria: categoria || undefined,
        cuenta_contable_id: cuentaId || undefined, es_anticipo: esAnticipo, origen,
        proveedor_id: proveedorId || undefined, acreedor: acreedor || undefined, proveedor_rut: rut || undefined,
        fecha, referencia: referencia || undefined, descripcion: descripcion || undefined,
        numero_documento: numDoc || undefined, tipo_doc: tipoDoc,
        moneda, tc: tcN || 1, monto_neto: netoN,
        ...(prefill ? { iva: ivaN } : { afecto_iva: afectoIva }),
        condicion_pago: condicion, plazo_dias: condicion === "credito" && plazo ? Number(plazo) : undefined,
        ...(prefill ? { emb_pricing_gasto_id: prefill.emb_pricing_gasto_id, embarque_id: prefill.embarque_id } : {}),
        ...(esNacional && ocpSel ? { oc_proveedor_id: Number(ocpSel), items } : {}),
        pago: condicion === "contado"
          ? { medio: pagoMedio, banco: pagoBanco || undefined, fecha, fecha_mov_bancario: pagoFechaBanco || fecha }
          : undefined,
      });
      toast.success("Compra registrada"); onDone(); onClose();
    } catch (e: any) { toast.error(e?.response?.data?.detail || "No se pudo registrar la compra"); } finally { setSaving(false); }
  };

  return (
    <Modal title={prefill ? `Registrar costo de embarque · ${prefill.referencia || ""}` : "Registrar compra / gasto"} wide onClose={onClose}>
      {prefill && (
        <p style={{ margin: 0, fontSize: 12, color: s.muted, background: s.sub, padding: "8px 10px", borderRadius: 8 }}>
          <Ship size={13} style={{ verticalAlign: "-2px", marginRight: 6, color: "var(--monza-accent)" }} />
          Datos traídos de <b>Embarques Pricing</b> (no se digitan de nuevo). Elige si lo pagas ahora o queda por pagar.
        </p>
      )}

      {/* Tipo de registro: gasto/servicio o compra nacional con detalle por ítem
          (con prefill de embarque no aplica: ese flujo ya viene armado). */}
      {!prefill && (
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8 }}>
          {([["gasto", "Gasto / servicio", Wallet], ["nacional", "Compra nacional (detalle de ítems)", Truck]] as const).map(([val, label, Icon]) => (
            <button key={val} type="button" onClick={() => setOrigenTipo(val)}
              style={{
                display: "flex", alignItems: "center", gap: 6, padding: "7px 14px", borderRadius: 999,
                fontSize: 12, fontWeight: 600, cursor: "pointer", fontFamily: "inherit",
                border: origenTipo === val ? "1px solid var(--monza-accent)" : s.cardBd,
                background: origenTipo === val ? "var(--monza-accent)" : s.sub,
                color: origenTipo === val ? "white" : s.muted,
              }}>
              <Icon size={13} /> {label}
            </button>
          ))}
        </div>
      )}

      {esNacional && (
        <div style={{ borderRadius: 10, border: s.cardBd, background: s.sub, padding: 12, display: "flex", flexDirection: "column", gap: 10 }}>
          <Field label="OC-Proveedor nacional">
            <select style={inp} value={ocpSel} onChange={e => setOcpSel(e.target.value ? Number(e.target.value) : "")}>
              <option value="">— Selecciona OC nacional —</option>
              {ocNacionales.map(o => (
                <option key={o.oc_proveedor_id} value={o.oc_proveedor_id}>
                  {(o.numero_oc || o.numero || `OCP #${o.oc_proveedor_id}`)} — {o.proveedor || "Sin proveedor"}
                </option>
              ))}
            </select>
          </Field>
          {ocNacionales.length === 0 ? (
            <p style={{ margin: 0, fontSize: 11, color: s.faint }}>
              No hay OC nacionales con ítems. Crea una en <b>Abastecimiento</b> (origen Nacional) y
              registra su entrega en <b>Seguimiento</b> antes de costear.
            </p>
          ) : ocSel && (
            <>
              <div style={{ borderRadius: 8, border: s.cardBd, overflow: "hidden" }}>
                <div style={{ overflowX: "auto" }}>
                  <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
                    <thead>
                      <tr style={{ background: s.cardBg, borderBottom: s.cardBd }}>
                        <th style={{ padding: "7px 8px", width: 28 }}></th>
                        {["N° Parte", "Descripción", "Recibido", "Disp. costear", "Cantidad", "Costo unit CLP", "Subtotal"].map(h => (
                          <th key={h} style={{ padding: "7px 8px", textAlign: "left", fontWeight: 700, fontSize: 10, textTransform: "uppercase", letterSpacing: 0.5, color: s.faint, whiteSpace: "nowrap" }}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {ocSel.items.map(it => {
                        const li = lineItems[it.item_cotizacion_id] || { incluir: false, cantidad: "", precio_unit: "" };
                        const sub = (Number(li.cantidad) || 0) * (Number(li.precio_unit) || 0);
                        const sinDisp = it.disponible_costear <= 0;
                        return (
                          <tr key={it.item_cotizacion_id} style={{ borderBottom: s.cardBd, opacity: li.incluir ? 1 : 0.5 }}>
                            <td style={{ padding: "5px 8px", textAlign: "center" }}>
                              <input type="checkbox" checked={li.incluir} disabled={sinDisp}
                                title={sinDisp ? "Registra primero la recepción nacional en Seguimiento" : undefined}
                                onChange={() => setLineItems(prev => ({ ...prev, [it.item_cotizacion_id]: { ...(prev[it.item_cotizacion_id] || { cantidad: "", precio_unit: "" }), incluir: !prev[it.item_cotizacion_id]?.incluir } }))}
                                style={{ accentColor: "var(--monza-accent)" }} />
                            </td>
                            <td style={{ padding: "5px 8px", fontFamily: "ui-monospace, monospace", fontWeight: 600, color: "var(--monza-accent)", whiteSpace: "nowrap" }}>{it.numero_parte || "—"}</td>
                            <td style={{ padding: "5px 8px", color: s.text, maxWidth: 150, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={it.descripcion || ""}>{it.descripcion || "—"}</td>
                            <td style={{ padding: "5px 8px", textAlign: "center", color: s.muted }}>{it.recibido}</td>
                            <td style={{ padding: "5px 8px", textAlign: "center", fontWeight: 700, color: sinDisp ? s.faint : s.text }}
                              title={sinDisp ? "Registra primero la recepción nacional en Seguimiento" : undefined}>{it.disponible_costear}</td>
                            <td style={{ padding: "5px 8px" }}>
                              <input type="number" min={0} step="any" style={{ ...inp, width: 70, padding: "4px 8px", fontSize: 12 }}
                                value={li.cantidad} disabled={!li.incluir}
                                onChange={e => setLineItems(prev => ({ ...prev, [it.item_cotizacion_id]: { ...(prev[it.item_cotizacion_id] || { incluir: true, precio_unit: "" }), cantidad: e.target.value } }))} />
                            </td>
                            <td style={{ padding: "5px 8px" }}>
                              <input type="number" min={0} step="any" style={{ ...inp, width: 90, padding: "4px 8px", fontSize: 12 }}
                                value={li.precio_unit} disabled={!li.incluir} placeholder="neto CLP"
                                onChange={e => setLineItems(prev => ({ ...prev, [it.item_cotizacion_id]: { ...(prev[it.item_cotizacion_id] || { incluir: true, cantidad: "" }), precio_unit: e.target.value } }))} />
                            </td>
                            <td style={{ padding: "5px 8px", textAlign: "right", fontFamily: "ui-monospace, monospace", whiteSpace: "nowrap", color: s.muted }}>{sub > 0 ? fmtClp(sub) : "—"}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </div>
              <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", justifyContent: "space-between", gap: 8, fontSize: 12 }}>
                <span style={{ color: s.muted }}>
                  Σ líneas: <b style={{ color: "var(--monza-accent)" }}>{fmtClp(sumaLineas)}</b>
                  <span style={{ margin: "0 6px" }}>·</span>
                  Neto factura: <b style={{ color: s.text }}>{fmtClp(netoN)}</b>
                </span>
                {sumaExcedeNeto ? (
                  <span style={{ color: "#B91C1C", fontWeight: 700, display: "flex", alignItems: "center", gap: 4 }}>
                    <AlertCircle size={13} /> Σ supera el neto de la factura
                  </span>
                ) : (
                  <button type="button" onClick={() => setNeto(String(Math.round(sumaLineas)))}
                    style={{ fontSize: 11, fontWeight: 700, padding: "4px 10px", borderRadius: 8, border: "1px solid var(--monza-accent)", background: "transparent", color: "var(--monza-accent)", cursor: "pointer", fontFamily: "inherit" }}>
                    Usar Σ como neto
                  </button>
                )}
              </div>
              <p style={{ margin: 0, fontSize: 11, color: s.faint }}>
                El costo por ítem es el <b>neto en CLP</b> (el IVA es crédito fiscal, no capitaliza).
                La cantidad costeada no puede superar lo recibido en bodega.
              </p>
            </>
          )}
        </div>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(230px, 1fr))", gap: 12 }}>
        {esNacional ? (
          <Field label="Tipo de gasto">
            <div style={{ ...inp, opacity: 0.7 }}>Costo de venta (nacional)</div>
          </Field>
        ) : (
        <Field label="Tipo de gasto">
          <select style={inp} value={tipoGasto} onChange={e => setTipoGasto(e.target.value)}>
            {(catalogos?.tipos_gasto || []).map(t => <option key={t.value} value={t.value}>{t.label}</option>)}
          </select>
        </Field>
        )}
        <Field label="Categoría">
          <input style={inp} list="mcc-cat" value={categoria} onChange={e => setCategoria(e.target.value)} placeholder="Ej. Flete internacional" />
          <datalist id="mcc-cat">{(catalogos?.categorias_sugeridas || []).map(c => <option key={c} value={c} />)}</datalist>
        </Field>
      </div>
      <Field label="Cuenta contable (imputación del neto)">
        <select style={inp} value={cuentaId} onChange={e => setCuentaId(e.target.value ? Number(e.target.value) : "")}>
          <option value="">— Selecciona cuenta —</option>
          {Object.entries(cuentasPorClase).map(([clase, cuentas]) => (
            <optgroup key={clase} label={clase}>
              {cuentas.map(c => <option key={c.id} value={c.id}>{c.codigo} — {c.nombre}</option>)}
            </optgroup>
          ))}
        </select>
        <p style={{ margin: "4px 0 0", fontSize: 11, color: s.faint }}>Se sugiere según el tipo de gasto; puedes cambiarla. El IVA va automático a crédito fiscal.</p>
      </Field>
      <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12, color: s.muted, cursor: "pointer" }}>
        <input type="checkbox" checked={esAnticipo} onChange={e => setEsAnticipo(e.target.checked)} style={{ accentColor: "var(--monza-accent)" }} />
        Es anticipo a proveedor extranjero (NIC 21 · no monetario)
      </label>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(230px, 1fr))", gap: 12 }}>
        <Field label="Proveedor (catálogo)">
          <select style={inp} value={proveedorId} onChange={e => onProveedor(e.target.value ? Number(e.target.value) : "")}>
            <option value="">— Sin catálogo —</option>
            {(catalogos?.proveedores || []).map(p => <option key={p.id} value={p.id}>{p.nombre}</option>)}
          </select>
        </Field>
        <Field label="Acreedor / proveedor (nombre)"><input style={inp} value={acreedor} onChange={e => setAcreedor(e.target.value)} placeholder="Nombre del proveedor" /></Field>
        <Field label="RUT proveedor"><input style={inp} value={rut} onChange={e => setRut(e.target.value)} placeholder="76.xxx.xxx-x" /></Field>
        <Field label="N° documento (factura/boleta)"><input style={inp} value={numDoc} onChange={e => setNumDoc(e.target.value)} /></Field>
        <Field label="Tipo de documento">
          <select style={inp} value={tipoDoc} onChange={e => setTipoDoc(e.target.value)}>
            <option value="factura">Factura</option><option value="boleta">Boleta</option>
            <option value="nota">Nota</option><option value="recibo">Recibo</option><option value="sin_documento">Sin documento</option>
          </select>
        </Field>
        <Field label="Fecha"><input type="date" style={inp} value={fecha} onChange={e => setFecha(e.target.value)} /></Field>
        <Field label="Referencia (ej. EMB-2026-001)"><input style={inp} value={referencia} onChange={e => setReferencia(e.target.value)} /></Field>
        <Field label="Descripción"><input style={inp} value={descripcion} onChange={e => setDescripcion(e.target.value)} placeholder="Glosa del gasto" /></Field>
      </div>

      {/* Montos */}
      <div style={{ borderRadius: 10, border: s.cardBd, background: s.sub, padding: 12 }}>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))", gap: 12 }}>
          {/* Con prefill de embarque la moneda queda fija en CLP (los gastos locales
              de Embarques Pricing siempre están en CLP; cambiarla corrompería el
              registro). La compra NACIONAL también fuerza CLP (factura chilena). */}
          <Field label="Moneda">
            <select style={inp} value={moneda} disabled={!!prefill || esNacional} onChange={e => setMoneda(e.target.value)}>
              <option value="CLP">CLP</option><option value="USD">USD</option><option value="EUR">EUR</option>
            </select>
          </Field>
          {moneda !== "CLP" && <Field label="Tipo de cambio (a CLP)"><input type="number" style={inp} value={tc} onChange={e => setTc(e.target.value)} placeholder="950" /></Field>}
          <Field label={`Monto neto (${moneda})`}><input type="number" style={inp} value={neto} onChange={e => setNeto(e.target.value)} disabled={!!prefill} /></Field>
        </div>
        {!prefill && (
          <label style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 8, fontSize: 12, color: s.muted, cursor: "pointer" }}>
            <input type="checkbox" checked={afectoIva} onChange={e => setAfectoIva(e.target.checked)} style={{ accentColor: "var(--monza-accent)" }} /> Afecto a IVA (19%)
          </label>
        )}
        <div style={{ display: "flex", flexWrap: "wrap", gap: "4px 20px", marginTop: 8, fontSize: 12, color: s.muted }}>
          <span>IVA: <b style={{ color: s.text }}>{moneda} {ivaN.toLocaleString("es-CL")}</b></span>
          <span>Total: <b style={{ color: s.text }}>{moneda} {Math.round(totalN).toLocaleString("es-CL")}</b></span>
          <span>Total en CLP: <b style={{ color: "var(--monza-accent)" }}>{fmtClp(totalClp)}</b></span>
        </div>
      </div>

      {/* Condición de pago: pagarlo ahora o dejarlo por pagar */}
      <div style={{ display: "flex", gap: 8 }}>
        {(["contado", "credito"] as const).map(c => (
          <button key={c} type="button" onClick={() => setCondicion(c)}
            style={{
              padding: "7px 14px", borderRadius: 999, fontSize: 12, fontWeight: 600, cursor: "pointer", fontFamily: "inherit",
              border: condicion === c ? "1px solid var(--monza-accent)" : s.cardBd,
              background: condicion === c ? "var(--monza-accent)" : s.sub,
              color: condicion === c ? "white" : s.muted,
            }}>
            {c === "contado" ? "Contado (pagar ahora)" : "Crédito (queda por pagar)"}
          </button>
        ))}
      </div>
      {condicion === "credito" ? (
        <Field label="Plazo (días)"><input type="number" style={inp} value={plazo} onChange={e => setPlazo(e.target.value)} /></Field>
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 12 }}>
          <Field label="Medio de pago">
            <select style={inp} value={pagoMedio} onChange={e => setPagoMedio(e.target.value)}>
              <option value="transferencia">Transferencia</option><option value="cheque">Cheque</option>
              <option value="efectivo">Efectivo</option><option value="tarjeta">Tarjeta</option>
            </select>
          </Field>
          <Field label="Banco"><input style={inp} value={pagoBanco} onChange={e => setPagoBanco(e.target.value)} /></Field>
          <Field label="Fecha en el banco"><input type="date" style={inp} value={pagoFechaBanco} onChange={e => setPagoFechaBanco(e.target.value)} /></Field>
        </div>
      )}

      <button onClick={submit} disabled={saving} style={{ ...btnPrimary(), width: "100%", opacity: saving ? 0.6 : 1 }}>
        {saving ? <Loader2 size={15} className="animate-spin" /> : <Wallet size={15} />} Registrar compra
      </button>
    </Modal>
  );
}

// ─── Modal: registrar pago de UNA compra ───────────────────────────────────────
function PagoCompraModal({ compra, onClose, onDone }: { compra: Compra; onClose: () => void; onDone: () => void }) {
  const s = useStyles();
  const inp = inputBase(s);
  const hoy = hoyLocal();  // fecha LOCAL (toISOString es UTC: de noche daría mañana)
  const [monto, setMonto] = useState(String(Math.round(compra.saldo_clp)));
  const [fecha, setFecha] = useState(hoy);
  const [fechaBanco, setFechaBanco] = useState(hoy);
  const [medio, setMedio] = useState("transferencia");
  const [banco, setBanco] = useState("");
  const [op, setOp] = useState("");
  const [saving, setSaving] = useState(false);
  const submit = async () => {
    if (!monto || Number(monto) <= 0) { toast.error("Monto inválido"); return; }
    setSaving(true);
    try {
      await monzaComprasAPI.registrarPago(compra.id, {
        monto_clp: Number(monto), fecha, fecha_mov_bancario: fechaBanco || undefined,
        medio, banco: banco || undefined, numero_operacion: op || undefined,
      });
      toast.success("Pago registrado"); onDone(); onClose();
    } catch (e: any) { toast.error(e?.response?.data?.detail || "Error al registrar el pago"); } finally { setSaving(false); }
  };
  return (
    <Modal title={`Registrar pago · ${compra.numero_documento || "#" + compra.id}`} onClose={onClose}>
      <p style={{ margin: 0, fontSize: 12, color: s.muted }}>Saldo pendiente: <b style={{ color: "#B45309" }}>{fmtClp(compra.saldo_clp)}</b></p>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(230px, 1fr))", gap: 12 }}>
        <Field label="Monto (CLP)"><input type="number" style={inp} value={monto} onChange={e => setMonto(e.target.value)} /></Field>
        <Field label="Fecha del pago"><input type="date" style={inp} value={fecha} onChange={e => setFecha(e.target.value)} /></Field>
        <Field label="Medio">
          <select style={inp} value={medio} onChange={e => setMedio(e.target.value)}>
            <option value="transferencia">Transferencia</option><option value="cheque">Cheque</option>
            <option value="efectivo">Efectivo</option><option value="tarjeta">Tarjeta</option>
          </select>
        </Field>
        <Field label="Banco"><input style={inp} value={banco} onChange={e => setBanco(e.target.value)} /></Field>
        <Field label="Fecha en el banco (cartola)"><input type="date" style={inp} value={fechaBanco} onChange={e => setFechaBanco(e.target.value)} /></Field>
        <Field label="N° operación"><input style={inp} value={op} onChange={e => setOp(e.target.value)} /></Field>
      </div>
      <p style={{ margin: 0, fontSize: 11, color: s.faint }}>La <b>fecha en el banco</b> es la del movimiento en la cartola; puedes dejarla y completarla después.</p>
      <button onClick={submit} disabled={saving} style={{ ...btnPrimary(), width: "100%", opacity: saving ? 0.6 : 1 }}>
        {saving ? <Loader2 size={15} className="animate-spin" /> : <CheckCircle2 size={15} />} Registrar pago
      </button>
    </Modal>
  );
}

// ─── Modal: pago consolidado (un movimiento paga varios gastos) ────────────────
function PagoConsolidadoModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const s = useStyles();
  const inp = inputBase(s);
  const [pendientes, setPendientes] = useState<Compra[]>([]);
  const [sel, setSel] = useState<Record<number, string>>({});
  const [fecha, setFecha] = useState(hoyLocal());
  const [medio, setMedio] = useState("transferencia");
  const [banco, setBanco] = useState("");
  const [op, setOp] = useState("");
  const [beneficiario, setBeneficiario] = useState("");
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    monzaComprasAPI.list({ page_size: 200 })
      .then(({ data }) => setPendientes((data.compras as Compra[]).filter(c => c.saldo_clp > 0 && !c.anulado)))
      // sin esto, un error de carga se vería como "no hay gastos pendientes" (engañoso)
      .catch((e: any) => toast.error(e?.response?.data?.detail || "No se pudieron cargar los gastos pendientes"));
  }, []);
  const toggle = (c: Compra) => setSel(prev => {
    const n = { ...prev };
    if (c.id in n) delete n[c.id]; else n[c.id] = String(Math.round(c.saldo_clp));
    return n;
  });
  const total = Object.values(sel).reduce((a, v) => a + (Number(v) || 0), 0);
  const submit = async () => {
    const detalles = Object.entries(sel).filter(([, v]) => Number(v) > 0).map(([id, v]) => ({ compra_id: Number(id), monto_clp: Number(v) }));
    if (!detalles.length) { toast.error("Selecciona al menos un gasto"); return; }
    setSaving(true);
    try {
      await monzaComprasAPI.crearEgresoConsolidado({
        fecha, medio, banco: banco || undefined, numero_operacion: op || undefined,
        beneficiario: beneficiario || undefined, fecha_mov_bancario: fecha, detalles,
      });
      toast.success("Egreso consolidado registrado"); onDone(); onClose();
    } catch (e: any) { toast.error(e?.response?.data?.detail || "No se pudo registrar el egreso"); } finally { setSaving(false); }
  };
  return (
    <Modal title="Pago consolidado · un movimiento paga varios gastos" wide onClose={onClose}>
      <p style={{ margin: 0, fontSize: 12, color: s.muted }}>Selecciona los gastos que pagaste en <b>una sola transferencia</b>; luego este egreso se cruzará con ese único movimiento del banco.</p>
      <div style={{ maxHeight: 260, overflowY: "auto", borderRadius: 8, border: s.cardBd }}>
        {pendientes.length === 0 ? (
          <p style={{ padding: 12, margin: 0, fontSize: 12, color: s.faint }}>No hay gastos pendientes de pago.</p>
        ) : pendientes.map(c => (
          <label key={c.id} style={{ display: "flex", alignItems: "center", gap: 8, padding: "8px 12px", borderBottom: s.cardBd, fontSize: 12, cursor: "pointer", color: s.text }}>
            <input type="checkbox" checked={c.id in sel} onChange={() => toggle(c)} style={{ accentColor: "var(--monza-accent)" }} />
            <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {c.acreedor || "—"} · <span style={{ fontFamily: "ui-monospace, monospace", color: "var(--monza-accent)" }}>{c.numero_documento || `#${c.id}`}</span> · saldo {fmtClp(c.saldo_clp)}
            </span>
            {c.id in sel && <input type="number" style={{ ...inp, width: 110, padding: "4px 8px", fontSize: 12 }} value={sel[c.id]} onClick={e => e.preventDefault()} onChange={e => setSel(prev => ({ ...prev, [c.id]: e.target.value }))} />}
          </label>
        ))}
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(230px, 1fr))", gap: 12 }}>
        <Field label="Fecha"><input type="date" style={inp} value={fecha} onChange={e => setFecha(e.target.value)} /></Field>
        <Field label="Medio">
          <select style={inp} value={medio} onChange={e => setMedio(e.target.value)}>
            <option value="transferencia">Transferencia</option><option value="cheque">Cheque</option>
            <option value="efectivo">Efectivo</option><option value="tarjeta">Tarjeta</option>
          </select>
        </Field>
        <Field label="Banco"><input style={inp} value={banco} onChange={e => setBanco(e.target.value)} /></Field>
        <Field label="N° operación"><input style={inp} value={op} onChange={e => setOp(e.target.value)} /></Field>
      </div>
      <Field label="Beneficiario"><input style={inp} value={beneficiario} onChange={e => setBeneficiario(e.target.value)} /></Field>
      <button onClick={submit} disabled={saving} style={{ ...btnPrimary(), width: "100%", opacity: saving ? 0.6 : 1 }}>
        {saving ? <Loader2 size={15} className="animate-spin" /> : <CreditCard size={15} />} Registrar egreso · {fmtClp(total)}
      </button>
    </Modal>
  );
}

// ─── Fila de compra (expandible) ───────────────────────────────────────────────
function CompraRow({ c, onChanged, onPagar }: { c: Compra; onChanged: () => void; onPagar: (c: Compra) => void }) {
  const s = useStyles();
  const [open, setOpen] = useState(false);
  const pill = PAGO_PILL[c.estado_pago] ?? { bg: "rgba(148,163,184,0.15)", color: "#64748B", label: c.estado_pago };
  const pct = Math.min(100, c.monto_total_clp > 0 ? Math.round((c.monto_pagado_clp / c.monto_total_clp) * 100) : 0);
  const td: React.CSSProperties = { padding: "12px 14px", whiteSpace: "nowrap", fontSize: 13 };
  const delPago = async (id: number) => {
    if (!confirm("¿Revertir este pago?")) return;
    try { await monzaComprasAPI.eliminarPago(c.id, id); toast.success("Pago revertido"); onChanged(); }
    catch (e: any) { toast.error(e?.response?.data?.detail || "Error"); }
  };
  const updFechaBanco = async (pagoId: number, value: string) => {
    try { await monzaComprasAPI.actualizarPago(c.id, pagoId, { fecha_mov_bancario: value }); onChanged(); }
    catch (e: any) { toast.error(e?.response?.data?.detail || "Error al guardar la fecha del banco"); }
  };
  const anular = async () => {
    const motivo = prompt("Motivo de anulación (opcional):") ?? undefined;
    if (motivo === undefined && !confirm("¿Anular esta compra?")) return;
    try { await monzaComprasAPI.anular(c.id, motivo); toast.success("Compra anulada"); onChanged(); }
    catch (e: any) { toast.error(e?.response?.data?.detail || "Error"); }
  };
  return (
    <>
      <tr style={{ cursor: "pointer", borderBottom: s.cardBd }} onClick={() => setOpen(o => !o)}>
        <td style={{ ...td, fontFamily: "ui-monospace, monospace", fontWeight: 600, color: "var(--monza-accent)" }}>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
            {open ? <ChevronUp size={12} /> : <ChevronDown size={12} />}{c.numero_documento || `#${c.id}`}
          </span>
          {c.origen === "EMBARQUE" && <span style={{ marginLeft: 4, fontSize: 10, color: "#0E7490" }}>·emb</span>}
          {c.origen === "NACIONAL" && <span style={{ marginLeft: 4, fontSize: 10, color: "#15803D" }} title="Compra nacional con detalle de ítems">·nac</span>}
        </td>
        <td style={{ ...td, fontWeight: 600, color: s.text, maxWidth: 180, overflow: "hidden", textOverflow: "ellipsis" }}>{c.acreedor || "—"}</td>
        <td style={td}>
          <span style={{ fontSize: 12, fontWeight: 600, color: TIPO_COLOR[c.tipo_gasto] || s.muted }}>{c.tipo_gasto_label}</span>
          {c.categoria && <span style={{ display: "block", fontSize: 10, color: s.faint }}>{c.categoria}</span>}
        </td>
        <td style={{ ...td, color: s.muted }}>{fmtDate(c.fecha)}</td>
        <td style={{ ...td, fontWeight: 600, color: s.text }}>
          {fmtClp(c.monto_total_clp)}
          {c.moneda !== "CLP" && <span style={{ display: "block", fontSize: 10, fontWeight: 400, color: s.faint }}>{c.moneda} {Math.round(c.monto_total).toLocaleString("es-CL")}</span>}
        </td>
        <td style={td}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <div style={{ width: 60, height: 6, borderRadius: 999, background: s.dark ? "#0d1430" : "#E2E8F0" }}>
              <div style={{ height: "100%", borderRadius: 999, background: "#15803D", width: `${pct}%` }} />
            </div>
            <span style={{ fontSize: 12, fontWeight: 600, color: "#15803D" }}>{pct}%</span>
          </div>
        </td>
        <td style={{ ...td, color: c.semaforo === "vencida" ? "#B91C1C" : s.muted, fontWeight: c.semaforo === "vencida" ? 600 : 400 }}>
          {fmtDate(c.fecha_vencimiento)}
          {c.dias_vencimiento != null && c.saldo_clp > 0 && <span style={{ marginLeft: 4, fontSize: 11 }}>({c.dias_vencimiento < 0 ? `${Math.abs(c.dias_vencimiento)}d venc.` : `${c.dias_vencimiento}d`})</span>}
        </td>
        <td style={td}><span style={{ background: pill.bg, color: pill.color, fontSize: 12, fontWeight: 600, padding: "4px 10px", borderRadius: 999 }}>{pill.label}</span></td>
      </tr>
      {open && (
        <tr>
          <td colSpan={8} style={{ padding: "0 16px 16px", background: s.sub }}>
            {/* flex-wrap en vez de 2 columnas fijas: en pantalla angosta el detalle y los
                botones se apilan (equivalente inline del `md:grid-cols-3` de Grupo AM). */}
            <div style={{ display: "flex", flexWrap: "wrap", gap: 16, paddingTop: 12 }}>
              <div style={{ flex: "2 1 320px", minWidth: 0 }}>
                <div style={{ display: "flex", flexWrap: "wrap", gap: "4px 20px", fontSize: 12, color: s.muted }}>
                  <span>Neto: <b style={{ color: s.text }}>{c.moneda} {Math.round(c.monto_neto).toLocaleString("es-CL")}</b></span>
                  <span>IVA: <b style={{ color: s.text }}>{c.moneda} {Math.round(c.iva).toLocaleString("es-CL")}</b></span>
                  <span>Total: <b style={{ color: "var(--monza-accent)" }}>{fmtClp(c.monto_total_clp)}</b></span>
                  <span>Pagado: <b style={{ color: "#15803D" }}>{fmtClp(c.monto_pagado_clp)}</b></span>
                  <span>Saldo: <b style={{ color: "#B45309" }}>{fmtClp(c.saldo_clp)}</b></span>
                </div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: "4px 20px", marginTop: 4, fontSize: 12, color: s.faint }}>
                  {c.cuenta_codigo && <span>Cuenta: <b style={{ color: s.muted }}>{c.cuenta_codigo} · {c.cuenta_nombre}</b></span>}
                  {c.es_anticipo && <span style={{ color: "#B45309" }}>Anticipo (NIC 21)</span>}
                  {c.proveedor_rut && <span>RUT: <b style={{ color: s.muted }}>{c.proveedor_rut}</b></span>}
                  {c.condicion_pago && <span>Condición: <b style={{ color: s.muted }}>{c.condicion_pago}</b></span>}
                  {c.moneda !== "CLP" && <span>TC: <b style={{ color: s.muted }}>{c.tc}</b></span>}
                  {c.referencia && <span>Ref: <b style={{ color: s.muted }}>{c.referencia}</b></span>}
                  {c.descripcion && <span>Glosa: <b style={{ color: s.muted }}>{c.descripcion}</b></span>}
                  {c.anulado && c.motivo_anulacion && <span style={{ color: "#B91C1C" }}>Anulada: {c.motivo_anulacion}</span>}
                </div>
                {/* Líneas costeadas de la compra NACIONAL: la factura ES el costo de
                    estos repuestos (neto CLP por ítem; el IVA no capitaliza). */}
                {(c.items?.length || 0) > 0 && (
                  <div style={{ marginTop: 12 }}>
                    <p style={{ margin: "0 0 4px", fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.5, color: s.faint }}>
                      Ítems costeados (neto CLP · el IVA no capitaliza)
                    </p>
                    <div style={{ borderRadius: 8, border: s.cardBd, overflow: "hidden" }}>
                      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
                        <thead>
                          <tr style={{ background: s.cardBg, borderBottom: s.cardBd }}>
                            {["N° Parte", "Descripción", "Cant.", "Unit CLP", "Total CLP"].map(h => (
                              <th key={h} style={{ padding: "6px 10px", textAlign: ["Cant.", "Unit CLP", "Total CLP"].includes(h) ? "right" : "left", fontWeight: 700, fontSize: 10, textTransform: "uppercase", letterSpacing: 0.5, color: s.faint, whiteSpace: "nowrap" }}>{h}</th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {c.items.map(it => (
                            <tr key={it.id} style={{ borderBottom: s.cardBd }}>
                              <td style={{ padding: "5px 10px", fontFamily: "ui-monospace, monospace", fontWeight: 600, color: "var(--monza-accent)", whiteSpace: "nowrap" }}>{it.numero_parte || "—"}</td>
                              <td style={{ padding: "5px 10px", color: s.text, maxWidth: 200, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={it.descripcion || ""}>{it.descripcion || "—"}</td>
                              <td style={{ padding: "5px 10px", textAlign: "right", color: s.muted }}>{it.cantidad}</td>
                              <td style={{ padding: "5px 10px", textAlign: "right", color: s.muted, fontFamily: "ui-monospace, monospace" }}>{fmtClp(it.costo_unit_clp)}</td>
                              <td style={{ padding: "5px 10px", textAlign: "right", fontWeight: 600, color: s.text, fontFamily: "ui-monospace, monospace" }}>{fmtClp(it.costo_total_clp)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}
                {c.pagos.length > 0 && (
                  <div style={{ marginTop: 12 }}>
                    <p style={{ margin: "0 0 4px", fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.5, color: s.faint }}>Pagos · la fecha en el banco se cruzará con la cartola</p>
                    {c.pagos.map(p => (
                      <div key={p.id} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, fontSize: 12, padding: "4px 0", borderBottom: s.cardBd }}>
                        <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: s.muted }}>
                          {fmtDate(p.fecha)} · {p.medio}{p.banco ? ` · ${p.banco}` : ""}{p.numero_operacion ? ` · ${p.numero_operacion}` : ""}{p.conciliado ? " · ✓conciliado" : ""}{p.n_compras > 1 ? " · (egreso de varios gastos)" : ""}
                        </span>
                        <span style={{ display: "flex", alignItems: "center", gap: 8, flexShrink: 0 }}>
                          <span style={{ display: "flex", alignItems: "center", gap: 4 }} title="Fecha del movimiento en el banco (cartola)" onClick={e => e.stopPropagation()}>
                            <span style={{ color: s.faint }}>en banco:</span>
                            <input type="date" value={p.fecha_mov_bancario || ""} disabled={c.anulado}
                              onChange={e => updFechaBanco(p.id, e.target.value)}
                              style={{ borderRadius: 6, border: s.cardBd, background: s.inputBg, color: s.text, padding: "2px 6px", fontSize: 11, fontFamily: "inherit" }} />
                          </span>
                          <span style={{ fontWeight: 700, color: "#15803D" }}>{fmtClp(p.monto_clp)}</span>
                          {!c.anulado && <button onClick={e => { e.stopPropagation(); delPago(p.id); }} style={{ color: "#B91C1C", background: "none", border: "none", cursor: "pointer", padding: 2 }} title="Revertir pago"><Trash2 size={12} /></button>}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
              <div style={{ flex: "1 1 200px", display: "flex", flexDirection: "column", gap: 8 }}>
                {!c.anulado && c.saldo_clp > 0 && (
                  <button onClick={e => { e.stopPropagation(); onPagar(c); }} style={btnSecondary(s)}><CreditCard size={14} /> Registrar pago</button>
                )}
                {!c.anulado && (
                  <button onClick={e => { e.stopPropagation(); anular(); }} style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 6, fontSize: 12, color: "#B91C1C", background: "none", border: "none", cursor: "pointer", padding: "6px 0", fontFamily: "inherit" }}>
                    <Ban size={14} /> Anular compra
                  </button>
                )}
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

// ─── Pestaña: costos de embarque (reflejo automático de Embarques Pricing) ─────
function CostosEmbarqueTab({ onRegistrar }: { onRegistrar: (p: Prefill) => void }) {
  const s = useStyles();
  const [rows, setRows] = useState<CostoEmbarque[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    monzaComprasAPI.costosEmbarque()
      .then(({ data }) => { setRows(data.costos); setTotal(data.total_clp); })
      .catch((e: any) => toast.error(e?.response?.data?.detail || "No se pudieron cargar los costos de embarque"))
      .finally(() => setLoading(false));
  }, []);
  const th: React.CSSProperties = { padding: "10px 14px", textAlign: "left", fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.5, color: s.faint, whiteSpace: "nowrap" };
  const td: React.CSSProperties = { padding: "10px 14px", whiteSpace: "nowrap", fontSize: 13 };
  if (loading) return <div style={{ display: "flex", justifyContent: "center", padding: 60 }}><Loader2 size={26} className="animate-spin" color="var(--monza-accent)" /></div>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{ borderRadius: 12, border: s.cardBd, background: s.cardBg, padding: "12px 16px", fontSize: 13, color: s.muted }}>
        <Ship size={15} style={{ verticalAlign: "-3px", marginRight: 6, color: "var(--monza-accent)" }} />
        Gastos anotados en <b style={{ color: s.text }}>Embarques Pricing</b> — se reflejan acá <b style={{ color: s.text }}>automáticamente</b> (no se digitan de nuevo).
        Total neto+IVA: <b style={{ color: "var(--monza-accent)" }}>{fmtClp(total)}</b>
      </div>
      {rows.length === 0 ? (
        <div style={{ borderRadius: 14, border: s.cardBd, background: s.cardBg, padding: 48, textAlign: "center" }}>
          <p style={{ margin: 0, fontSize: 13, color: s.muted }}>No hay gastos de embarque registrados todavía (se cargan en Contabilidad → Embarques Pricing).</p>
        </div>
      ) : (
        <div style={{ borderRadius: 14, border: s.cardBd, background: s.cardBg, overflow: "hidden" }}>
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              {/* Banco: lo anota Embarques Pricing junto al gasto y es con lo que después se
                  cuadra el cargo en la cartola — se venía declarando y no se pintaba. */}
              <thead><tr style={{ borderBottom: s.cardBd, background: s.sub }}>
                {["Embarque", "Gasto", "Proveedor", "N° factura", "Fecha", "Neto", "IVA", "Total", "Banco", "Estado"].map(h => <th key={h} style={th}>{h}</th>)}
              </tr></thead>
              <tbody>
                {rows.map(r => (
                  <tr key={r.id} style={{ borderBottom: s.cardBd }}>
                    <td style={{ ...td, fontFamily: "ui-monospace, monospace", color: "var(--monza-accent)" }}>{r.embarque_numero || r.embarque_id}</td>
                    <td style={{ ...td, color: s.text }}>{r.glosa} <span style={{ fontSize: 10, color: s.faint }}>({r.tipo})</span></td>
                    <td style={{ ...td, color: s.muted, maxWidth: 160, overflow: "hidden", textOverflow: "ellipsis" }}>{r.acreedor || "—"}</td>
                    <td style={{ ...td, color: s.muted }}>{r.nro_factura || "—"}</td>
                    <td style={{ ...td, color: s.muted }}>{fmtDate(r.fecha_factura)}</td>
                    <td style={{ ...td, color: s.text }}>{fmtClp(r.monto_neto)}</td>
                    <td style={{ ...td, color: s.muted }}>{fmtClp(r.iva)}</td>
                    <td style={{ ...td, fontWeight: 600, color: "var(--monza-accent)" }}>{fmtClp(r.monto_total)}</td>
                    <td style={{ ...td, color: s.muted, maxWidth: 120, overflow: "hidden", textOverflow: "ellipsis" }}>{r.banco || "—"}</td>
                    <td style={td}>
                      {r.compra_id ? (
                        <span style={{ background: "rgba(21,128,61,0.14)", color: "#15803D", fontSize: 11, fontWeight: 600, padding: "3px 9px", borderRadius: 999 }}>En compras ✓</span>
                      ) : r.monto_total > 0 ? (
                        <button onClick={() => onRegistrar({
                          origen: "EMBARQUE", tipo_gasto: "cogs", monto_neto: r.monto_neto, iva: r.iva,
                          numero_documento: r.nro_factura, referencia: r.embarque_numero,
                          acreedor: r.acreedor, emb_pricing_gasto_id: r.id, embarque_id: r.embarque_id,
                        })} style={{ ...btnSecondary(s), padding: "5px 10px", fontSize: 11 }}>
                          <Plus size={12} /> Registrar como compra
                        </button>
                      ) : (
                        <span style={{ fontSize: 11, color: s.faint }}>sin monto</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Página ─────────────────────────────────────────────────────────────────
export default function MonzaComprasPage() {
  const s = useStyles();
  const [tab, setTab] = useState<"compras" | "embarque">("compras");
  const [compras, setCompras] = useState<Compra[]>([]);
  const [aging, setAging] = useState<Antiguedad | null>(null);
  const [kpis, setKpis] = useState<Kpis | null>(null);
  const [catalogos, setCatalogos] = useState<Catalogos | null>(null);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState("");
  const [estado, setEstado] = useState("");
  const [tipo, setTipo] = useState("");
  const [error, setError] = useState("");
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [modal, setModal] = useState<{ type: "crear" | "pago" | "consolidado"; compra?: Compra; prefill?: Prefill } | null>(null);

  const PAGE_SIZE = 50;

  const load = useCallback(async (search?: string, est?: string, tp?: string, pg = 1) => {
    setLoading(true); setError("");
    try {
      const [lRes, kRes] = await Promise.all([
        monzaComprasAPI.list({ q: search || undefined, estado_pago: est || undefined, tipo: tp || undefined, page: pg, page_size: PAGE_SIZE }),
        monzaComprasAPI.kpis(),
      ]);
      setCompras(lRes.data.compras); setAging(lRes.data.antiguedad);
      setTotal(lRes.data.total); setPage(lRes.data.page); setKpis(kRes.data);
    } catch { setError("No se pudieron cargar las compras."); } finally { setLoading(false); }
  }, []);

  useEffect(() => {
    monzaComprasAPI.catalogos().then(({ data }) => setCatalogos(data))
      .catch((e: any) => toast.error(e?.response?.data?.detail || "No se pudieron cargar los catálogos"));
  }, []);
  useEffect(() => { load(q || undefined, estado || undefined, tipo || undefined, 1); }, [estado, tipo]);  // eslint-disable-line react-hooks/exhaustive-deps
  const reload = () => load(q || undefined, estado || undefined, tipo || undefined, page);
  const goPage = (pg: number) => load(q || undefined, estado || undefined, tipo || undefined, pg);
  const handleSearch = (v: string) => { setQ(v); if (v.length === 0 || v.length >= 2) load(v || undefined, estado || undefined, tipo || undefined, 1); };

  const chip = (active: boolean): React.CSSProperties => ({
    padding: "6px 12px", borderRadius: 999, fontSize: 12, fontWeight: 600, cursor: "pointer", fontFamily: "inherit",
    border: active ? "1px solid var(--monza-accent)" : s.cardBd,
    background: active ? "var(--monza-accent)" : s.cardBg,
    color: active ? "white" : s.muted,
  });

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 16, flexWrap: "wrap" }}>
        <div style={{ minWidth: 0 }}>
          <h1 style={{ margin: 0, fontSize: 24, fontWeight: 700, color: s.text }}>Compras y Cuentas por Pagar</h1>
          <p style={{ margin: "4px 0 0", fontSize: 13, color: s.muted }}>Registro de compras y gastos · pagar ahora o a crédito · costos de embarque automáticos</p>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <button onClick={() => setModal({ type: "crear" })} style={btnPrimary()}><Plus size={15} /> Registrar compra</button>
          <button onClick={() => setModal({ type: "consolidado" })} style={btnSecondary(s)}><CreditCard size={15} /> Pago consolidado</button>
          <button onClick={reload} style={btnSecondary(s)}><RefreshCw size={15} /></button>
        </div>
      </div>

      {/* Tabs */}
      <div style={{ display: "flex", alignItems: "center", gap: 4, borderBottom: s.cardBd }}>
        {([["compras", "Compras registradas"], ["embarque", "Costos de embarque"]] as const).map(([k, lbl]) => (
          <button key={k} onClick={() => setTab(k)}
            style={{
              padding: "9px 16px", fontSize: 13, fontWeight: 600, cursor: "pointer", fontFamily: "inherit",
              background: "none", border: "none", marginBottom: -1,
              borderBottom: tab === k ? "2px solid var(--monza-accent)" : "2px solid transparent",
              color: tab === k ? "var(--monza-accent)" : s.muted,
            }}>{lbl}</button>
        ))}
      </div>

      {tab === "embarque" ? (
        <CostosEmbarqueTab onRegistrar={(p) => setModal({ type: "crear", prefill: p })} />
      ) : (
        <>
          {/* KPIs */}
          {kpis && (
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 12 }}>
              {[
                { icon: DollarSign, label: "Total comprado", value: fmtClp(kpis.total_comprado_clp), color: "var(--monza-accent)" },
                { icon: CheckCircle2, label: "Pagado", value: fmtClp(kpis.pagado_clp), color: "#15803D" },
                { icon: CreditCard, label: "Por pagar", value: fmtClp(kpis.por_pagar_clp), color: "#B45309" },
                { icon: AlertCircle, label: "Vencido", value: fmtClp(kpis.vencido_clp), color: "#B91C1C" },
                { icon: Wallet, label: "Costo de venta", value: fmtClp(kpis.por_tipo?.cogs || 0), color: "#7C3AED" },
              ].map(k => (
                <div key={k.label} style={{ background: s.cardBg, border: s.cardBd, borderRadius: 14, padding: 14 }}>
                  <div style={{ width: 32, height: 32, borderRadius: 10, display: "flex", alignItems: "center", justifyContent: "center", marginBottom: 8, background: s.dark ? "#0d1430" : "#F1F5F9", color: k.color }}><k.icon size={16} /></div>
                  <p style={{ margin: 0, fontSize: 10, textTransform: "uppercase", letterSpacing: 0.8, color: s.faint }}>{k.label}</p>
                  <p style={{ margin: "2px 0 0", fontSize: 19, fontWeight: 700, color: k.color }}>{k.value}</p>
                </div>
              ))}
            </div>
          )}

          {/* Antigüedad */}
          {aging && (
            <div style={{ background: s.cardBg, border: s.cardBd, borderRadius: 14, padding: 16 }}>
              <h3 style={{ margin: "0 0 12px", fontSize: 13, fontWeight: 700, color: s.text }}>Antigüedad de Cartera (saldo por pagar)</h3>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))", gap: 12 }}>
                {[
                  { rango: "0–30 días", monto: aging["0_30"], color: "#15803D" },
                  { rango: "31–60 días", monto: aging["31_60"], color: "#B45309" },
                  { rango: "61–90 días", monto: aging["61_90"], color: "#C2410C" },
                  { rango: "+90 días", monto: aging["91_mas"], color: "#B91C1C" },
                ].map(r => (
                  <div key={r.rango} style={{ textAlign: "center", padding: 12, borderRadius: 10, background: s.sub }}>
                    <p style={{ margin: 0, fontSize: 12, color: s.faint }}>{r.rango}</p>
                    <p style={{ margin: "4px 0 0", fontSize: 15, fontWeight: 700, color: r.color }}>{fmtClp(r.monto)}</p>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Filtros */}
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            <div style={{ position: "relative" }}>
              <Search size={15} color={s.faint} style={{ position: "absolute", left: 12, top: "50%", transform: "translateY(-50%)" }} />
              <input placeholder="Buscar por proveedor, documento, referencia…" value={q} onChange={e => handleSearch(e.target.value)}
                style={{ ...inputBase(s), padding: "10px 16px 10px 36px" }} />
            </div>
            <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6 }}>
              {TIPOS.map(t => <button key={t} onClick={() => setTipo(t)} style={chip(tipo === t)}>{TIPO_LABEL[t]}</button>)}
              <span style={{ color: s.faint, margin: "0 4px" }}>·</span>
              {ESTADOS.map(e => <button key={e} onClick={() => setEstado(e)} style={chip(estado === e)}>{ESTADO_LABEL[e]}</button>)}
            </div>
          </div>

          {error && <div style={{ borderRadius: 10, border: "1px solid rgba(185,28,28,0.25)", background: "rgba(185,28,28,0.1)", padding: "12px 16px", fontSize: 13, color: "#B91C1C" }}>{error}</div>}
          {loading && <div style={{ display: "flex", justifyContent: "center", padding: 60 }}><Loader2 size={26} className="animate-spin" color="var(--monza-accent)" /></div>}
          {!loading && !error && compras.length === 0 && (
            <div style={{ borderRadius: 14, border: s.cardBd, background: s.cardBg, padding: 56, textAlign: "center" }}>
              <Wallet size={40} style={{ margin: "0 auto 12px", opacity: 0.2, color: s.muted }} />
              <p style={{ margin: 0, fontSize: 13, fontWeight: 600, color: s.muted }}>No hay compras registradas</p>
              <p style={{ margin: "4px 0 0", fontSize: 12, color: s.faint }}>Usa "Registrar compra" o trae un costo desde la pestaña de embarques.</p>
            </div>
          )}

          {!loading && compras.length > 0 && (
            <div style={{ borderRadius: 14, border: s.cardBd, background: s.cardBg, overflow: "hidden" }}>
              <div style={{ overflowX: "auto" }}>
                <table style={{ width: "100%", borderCollapse: "collapse" }}>
                  <thead><tr style={{ borderBottom: s.cardBd, background: s.sub }}>
                    {["Documento", "Proveedor", "Tipo", "Fecha", "Total", "Pagado", "Vencimiento", "Estado"].map(h => (
                      <th key={h} style={{ padding: "10px 14px", textAlign: "left", fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.5, color: s.faint, whiteSpace: "nowrap" }}>{h}</th>
                    ))}
                  </tr></thead>
                  <tbody>
                    {compras.map(c => <CompraRow key={c.id} c={c} onChanged={reload} onPagar={co => setModal({ type: "pago", compra: co })} />)}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* Paginador (server-side) */}
          {!loading && total > PAGE_SIZE && (
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", fontSize: 12, color: s.muted }}>
              <span>Mostrando {(page - 1) * PAGE_SIZE + 1}–{Math.min(page * PAGE_SIZE, total)} de {total}</span>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <button onClick={() => goPage(page - 1)} disabled={page <= 1} style={{ ...btnSecondary(s), opacity: page <= 1 ? 0.4 : 1, cursor: page <= 1 ? "not-allowed" : "pointer" }}>Anterior</button>
                <span>Página {page} de {Math.max(1, Math.ceil(total / PAGE_SIZE))}</span>
                <button onClick={() => goPage(page + 1)} disabled={page * PAGE_SIZE >= total} style={{ ...btnSecondary(s), opacity: page * PAGE_SIZE >= total ? 0.4 : 1, cursor: page * PAGE_SIZE >= total ? "not-allowed" : "pointer" }}>Siguiente</button>
              </div>
            </div>
          )}
        </>
      )}

      {/* Modales */}
      {modal?.type === "crear" && (
        <RegistrarCompraModal catalogos={catalogos} prefill={modal.prefill || null}
          onClose={() => setModal(null)}
          onDone={() => { reload(); if (modal.prefill) setTab("compras"); }} />
      )}
      {modal?.type === "pago" && modal.compra && <PagoCompraModal compra={modal.compra} onClose={() => setModal(null)} onDone={reload} />}
      {modal?.type === "consolidado" && <PagoConsolidadoModal onClose={() => setModal(null)} onDone={reload} />}
    </div>
  );
}
