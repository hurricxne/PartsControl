// Página "Ventas — Contabilidad" (MonzaParts): lista las VENTAS agrupadas por cotización
// vendida/despachada (solo lectura) y, al expandir, muestra ítems, guías y facturas.
// Consume monzaContabilidadAPI. El alta de facturas vive en MonzaFacturasPage.
import { useState, useEffect, useCallback } from "react";
import { fmtClp } from "../utils/format";
import {
  TrendingUp, Search, DollarSign, CreditCard, CheckCircle2, AlertCircle,
  Loader2, RefreshCw, ChevronDown, ChevronUp, Receipt, Truck, Clock, X,
  Pencil, HandCoins,
} from "lucide-react";
import toast from "react-hot-toast";
import { useMonzaTheme } from "./MonzaLayout";
import { monzaContabilidadAPI, monzaCotizacionesAPI } from "../services/monzaApi";
import { useAuthStore } from "../stores/authStore";
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
  // Mercadería de la venta que aún NO llega a factura (base física del backend).
  // Distinto de por_cobrar_clp, que es el saldo de lo YA facturado. Opcional:
  // tolera respuestas del backend previo al deploy de la Fase 4.
  por_facturar_clp?: number;
  estado_cobranza: string;
  // Adelanto (50%): informado por Comercial, verificado por Contabilidad
  requiere_adelanto?: boolean;
  pct_adelanto?: number;
  estado_adelanto?: "no_aplica" | "por_verificar" | "verificado";
  // Adelanto ya aprobado por Tesorería (null mientras no exista). De acá sale el folio
  // de la factura de ANTICIPO (vía B) que respalda esa plata: el backend lo DERIVA de
  // las facturas es_anticipo de la venta, no es una columna. Se declara solo el campo
  // que esta pantalla pinta; el resto del payload no lo usa.
  adelanto?: { factura_anticipo_folio?: string | null } | null;
  // MISMO folio publicado en la RAÍZ de la fila. Se declara opcional porque hoy el
  // backend solo lo entrega DENTRO de `adelanto`, y ahí no llega cuando el anticipo se
  // emitió ANTES de que Tesorería aprobara (no existe fila de adelanto todavía). El
  // chip lo lee de acá primero para no depender del estado del adelanto.
  factura_anticipo_folio?: string | null;
}
interface GuiaRef { numero_guia: string | null; numero_despacho: string; estado: string; qty_despachada: number; guia_firmada?: boolean; despacho_id?: number; guia_firmada_archivo?: string | null }
// `cantidad` = cantidad del ítem cubierta por ESA factura (tandas parciales). El
// backend YA la envía; sin declararla, qtyPendiente daría la cantidad completa
// EN SILENCIO (TS no avisa por campo ausente en un objeto que llega por red).
interface FacturaRef { factura_id: number; numero_factura: string | null; fecha_vencimiento: string | null; plazo_dias: number | null; estado_pago: string; cantidad: number }
interface VentaItem {
  id: number; numero_parte: string; descripcion: string; marca: string;
  cantidad: number; precio_unit_venta_clp: number; total_venta_clp: number;
  estado_linea: string; guias: GuiaRef[]; facturas: FacturaRef[];
}
// Factura de la venta (payload de _serialize_factura del backend). La usa el
// desglose "Facturas de esta venta"; la MISMA fuente que ve Facturas y Cobranzas.
interface FacturaOcItem {
  id: number;
  item_cotizacion_id: number | null;
  // Distinto de null ⇒ es la línea NEGATIVA que descuenta esa factura de anticipo
  // (vía B). No es mercadería: no consume cantidad de la venta y por eso se pinta
  // aparte (↩ y en violeta) en vez de leerse como un ítem más.
  anticipo_factura_id: number | null;
  numero_parte: string | null;
  descripcion: string | null;
  cantidad: number;
  total_neto: number;
}
interface FacturaOc {
  id: number;
  numero_factura: string | null;
  // Factura de ANTICIPO (vía B): la ÚNICA que nace SIN guía de despacho. Cobra por
  // adelantado y después se descuenta sola en la factura del despacho real.
  es_anticipo: boolean;
  numero_guia: string | null;
  fecha_emision: string | null;
  plazo_dias: number | null;
  fecha_vencimiento: string | null;
  monto_neto: number;
  iva: number;
  monto_bruto: number;
  monto_pagado: number;
  saldo: number;
  estado_pago: string;
  dias_vencimiento: number | null;
  items: FacturaOcItem[];
}
interface ResumenCobranza {
  n_facturas: number;
  facturado_clp: number;
  cobrado_clp: number;
  cobrado_cliente_clp?: number;
  por_cobrar_clp: number;
  estado_cobranza: string;
  por_facturar_clp?: number;
  // Anticipo (vía B) YA facturado y aún NO descontado en las facturas del despacho
  // real, en BRUTO. Es lo que explica la diferencia entre "mercadería sin facturar" y
  // "por facturar": esa plata ya se facturó por adelantado contra lo que falta llegar.
  anticipo_por_descontar_clp?: number;
  // Mercadería físicamente sin facturar (bruto), AUTORITATIVA del backend:
  // por_facturar_clp = max(mercaderia_pendiente − anticipo_por_descontar, 0).
  // El frontend NO la reconstruye (por_facturar + anticipo sobredeclara con clamp).
  mercaderia_pendiente_clp?: number;
}
// Adelanto VIGENTE de la venta (bloque `adelanto` de estado_adelanto). Un adelanto
// anulado llega como `adelanto: null` y la venta vuelve a 'por_verificar'.
// `conciliado_banco` va opcional: hoy ese dato lo publica Tesorería en su propia cola
// (donde se cruza con la cartola) y este payload no lo trae — se pinta si llega.
interface AdelantoDetalle {
  id: number;
  estado: string;
  monto: number;
  monto_aplicado: number;
  fecha_pago: string | null;
  banco: string | null;
  numero_operacion: string | null;
  observaciones: string | null;
  fecha_verificacion: string | null;
  factura_anticipo_folio?: string | null;
  conciliado_banco?: boolean;
}
interface VentaDetalle {
  cotizacion_id: number; numero_cotizacion: string; cliente: string; rut_cliente: string;
  oc_cliente: string | null; vehiculo: string | null; cond_pago: string | null;
  total_neto_clp: number; iva_clp: number; total_con_iva_clp: number; items: VentaItem[];
  // Aditivos Fase 4 (opcionales: toleran backend previo)
  facturas?: FacturaOc[];
  resumen?: ResumenCobranza;
  // Bloque estado_adelanto del backend (va en la raíz del detalle).
  requiere_adelanto?: boolean;
  pct_adelanto?: number;
  estado_adelanto?: "no_aplica" | "por_verificar" | "verificado";
  factura_anticipo_folio?: string | null;
  adelanto?: AdelantoDetalle | null;
}
// GET /cotizaciones/{id}: de acá salen el N° y la FECHA de la OC del cliente. La fecha NO
// viaja en el payload de Contabilidad y es la que se imprime como REFERENCIA 801 del DTE,
// así que el contador tiene que poder verla y corregirla desde esta pantalla.
interface OcCotizacion {
  oc_cliente: string | null;
  oc_fecha: string | null;
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

// Badge honesto de cobranza: "Cobrada" a secas engaña cuando lo cobrado es solo el
// adelanto y parte de la venta ni siquiera se ha facturado. El sufijo lo transparenta.
function CobranzaBadge({ estado, faltaFacturar }: { estado: string; faltaFacturar?: boolean }) {
  const m = COBRANZA_BADGE[estado] ?? { bg: "#F1F5F9", color: "#64748B", label: estado };
  const label = estado === "cobrada" && faltaFacturar ? "Cobrada · falta facturar" : m.label;
  return <span style={{ background: m.bg, color: m.color, fontSize: 11, fontWeight: 600, padding: "2px 8px", borderRadius: 999, whiteSpace: "nowrap" }}>{label}</span>;
}

// El `detail` de FastAPI es un string en los errores de negocio (400/404/409) pero un
// ARREGLO de objetos en los de validación (422): pintarlo tal cual dejaba un toast
// ilegible justo donde el dato es una FECHA (el 422 más probable de esta pantalla).
const detalleError = (e: unknown, porDefecto: string): string => {
  const d = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof d === "string" && d.trim()) return d;
  if (Array.isArray(d)) {
    const msgs = d
      .map(x => (x && typeof x === "object" && "msg" in x ? String((x as { msg: unknown }).msg) : ""))
      .filter(Boolean);
    if (msgs.length) return msgs.join(" · ");
  }
  return porDefecto;
};

// Roles que pueden editar la OC (espejo de VentasContabPage de Grupo AM y del
// require_rol del PATCH /cotizaciones/{id}). Hoy `user.rol` llega undefined (el login no
// lo envía) → el botón se ve; cuando el backend propague el rol, el gate se cierra solo.
const ROLES_EDITAN_OC = ["comercial", "contabilidad", "admin"];

// ─── Modal: editar la OC del cliente (N° y FECHA) ─────────────────────────────
// La FECHA de la OC viaja como REFERENCIA 801 de cada DTE de la venta (guía 52 y factura
// 33). Si sale mal, el contador no tenía dónde arreglarla: este modal es ese lugar. El
// backend responde 409 nombrando el folio cuando ya hay un documento VIVO ante el SII
// (ahí el papel legal ya quedó emitido con esos datos y se corrige en Wasabil, no acá).
function OcClienteEditModal({ cotizacionId, numeroCotizacion, inicial, onClose, onSaved }: {
  cotizacionId: number; numeroCotizacion: string; inicial: OcCotizacion;
  onClose: () => void; onSaved: (oc: OcCotizacion) => void;
}) {
  const { dark } = useMonzaTheme();
  const muted = dark ? "#94A3B8" : "#64748B";
  const text = dark ? "#E2E8F0" : "#0f172a";
  const bd = `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}`;
  const [numero, setNumero] = useState(inicial.oc_cliente || "");
  const [fecha, setFecha] = useState((inicial.oc_fecha || "").slice(0, 10));
  const [saving, setSaving] = useState(false);
  const inp: React.CSSProperties = {
    width: "100%", padding: "8px 12px", borderRadius: 8, border: bd,
    background: dark ? "#0d1430" : "white", color: text, fontSize: 14,
    fontFamily: "inherit", boxSizing: "border-box",
  };
  const submit = async () => {
    if (!numero.trim()) { toast.error("El N° de OC del cliente es obligatorio"); return; }
    setSaving(true);
    try {
      // `estado` NO viaja: esto edita datos, no re-cierra la venta. La fecha solo se
      // manda si tiene valor — el backend descarta los campos nulos (model_dump
      // exclude_none), así que dejarla vacía NO borra la que ya está guardada.
      const { data } = await monzaCotizacionesAPI.update(cotizacionId, {
        oc_cliente: numero.trim(),
        ...(fecha ? { oc_fecha: fecha } : {}),
      });
      const resp = data as OcCotizacion;
      toast.success("OC del cliente actualizada");
      onSaved({ oc_cliente: resp?.oc_cliente ?? numero.trim(), oc_fecha: resp?.oc_fecha ?? (fecha || null) });
      onClose();
    } catch (e: unknown) {
      toast.error(detalleError(e, "No se pudo actualizar la OC del cliente"));
    } finally { setSaving(false); }
  };
  return (
    <div style={{ position: "fixed", inset: 0, zIndex: 300, display: "flex", alignItems: "center", justifyContent: "center", background: "rgba(0,0,0,0.6)", padding: 16 }} onClick={onClose}>
      <div style={{ width: "100%", maxWidth: 440, borderRadius: 14, border: bd, background: dark ? "#131b3e" : "white", boxShadow: "0 20px 50px rgba(0,0,0,0.4)" }} onClick={e => e.stopPropagation()}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "12px 18px", borderBottom: bd }}>
          <h3 style={{ margin: 0, fontSize: 14, fontWeight: 700, color: text }}>OC del cliente · COT {numeroCotizacion}</h3>
          <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", color: muted, padding: 4 }}><X size={16} /></button>
        </div>
        <div style={{ padding: 18, display: "flex", flexDirection: "column", gap: 12 }}>
          <p style={{ margin: 0, fontSize: 12, color: muted }}>
            El N° y la fecha de esta OC se imprimen como <b style={{ color: text }}>referencia 801</b> en la guía y
            en la factura electrónica. Con un documento ya emitido ante el SII el sistema no deja editarlos
            (la corrección se gestiona en Wasabil).
          </p>
          <label style={{ display: "block" }}>
            <span style={{ display: "block", fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 4, color: muted }}>N° de OC del cliente</span>
            <input style={inp} value={numero} onChange={e => setNumero(e.target.value)} placeholder="OC-12345" />
          </label>
          <label style={{ display: "block" }}>
            <span style={{ display: "block", fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 4, color: muted }}>Fecha de la OC</span>
            <input type="date" style={inp} value={fecha} onChange={e => setFecha(e.target.value)} />
          </label>
          <button onClick={submit} disabled={saving}
            style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 8, padding: "9px 14px", borderRadius: 8, border: "none", background: "var(--monza-accent)", color: "white", fontWeight: 600, fontSize: 13, cursor: "pointer", fontFamily: "inherit", width: "100%", opacity: saving ? 0.6 : 1 }}>
            {saving ? <Loader2 size={15} className="animate-spin" /> : <CheckCircle2 size={15} />} Guardar OC
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── Desglose de la venta: avance, facturas y pendiente (Fase 4, espejo G15 GA) ──
// Sobre este umbral la tabla completa de ítems parte PLEGADA (anti-muro con ventas
// de cientos de líneas); bajo el umbral queda abierta como siempre.
const UMBRAL_TABLA_PLEGADA = 10;

// Cantidad de un ítem aún sin facturar (las tandas parciales restan lo facturado).
// Clamp a 0: una sobrefacturación por corrección manual no debe dar remanente negativo.
const qtyPendiente = (item: VentaItem): number =>
  Math.max(item.cantidad - item.facturas.reduce((s, f) => s + (Number(f.cantidad) || 0), 0), 0);

// Etiquetas del estado logístico para el grupo "Por facturar": responden POR QUÉ ese
// ítem aún no se factura y dónde hay que empujar. Orden = cercanía a facturar.
// OJO: son los estado_linea REALES de Monza (los que escriben sus routers), NO los de
// GA — copiar los de GA ('pre_embarcado', 'ingresado', 'reclamo_proveedor') dejaría
// TODO en el bucket residual porque ninguna clave coincide.
const GRUPOS_PENDIENTE: { estados: string[]; label: string; dot: string; hint: string }[] = [
  { estados: ["despachado"], label: "Despachado", dot: "var(--monza-accent)", hint: "listo para facturar (ya salió)" },
  { estados: ["en_bodega"], label: "En bodega", dot: "#10B981", hint: "despachable ya" },
  { estados: ["embarcado"], label: "Embarcado", dot: "#3B82F6", hint: "en tránsito" },
  { estados: ["preparado"], label: "En preparación", dot: "#38BDF8", hint: "preparando embarque" },
  { estados: ["comprado"], label: "Comprado", dot: "#F59E0B", hint: "esperando al proveedor" },
  { estados: ["cotizado", "por_comprar"], label: "Por comprar", dot: "#94A3B8", hint: "sin compra al proveedor" },
  { estados: ["reclamo"], label: "En reclamo", dot: "#EF4444", hint: "reclamo al proveedor" },
];

function VencimientoLabel({ f }: { f: FacturaOc }) {
  const { dark } = useMonzaTheme();
  const muted = dark ? "#94A3B8" : "#64748B";
  // Saldada o sin días calculables: solo la fecha (no hay urgencia que pintar).
  if (f.saldo <= 0 || f.dias_vencimiento === null || f.dias_vencimiento === undefined) {
    return f.fecha_vencimiento ? <span style={{ color: muted }}>{fmtDate(f.fecha_vencimiento)}</span> : <span style={{ color: muted }}>—</span>;
  }
  const d = f.dias_vencimiento;
  if (d < 0) return <span style={{ color: "#DC2626", fontWeight: 600 }}>vencida hace {-d}d</span>;
  if (d === 0) return <span style={{ color: dark ? "#FBBF24" : "#B45309", fontWeight: 600 }}>vence HOY</span>;
  return <span style={{ color: muted }}>vence en {d}d</span>;
}

// Barra de avance de la plata de la venta (espejo del AvanceOc de Grupo AM,
// re-vestido al idioma Monza): ▓ cobrado · ▒ facturado sin cobrar · ░ por facturar
// (el fondo neutro). Con nota del anticipo (vía B) cuando explica la diferencia entre
// mercadería sin facturar y por facturar. SIN doble conteo: el bruto de la factura de
// anticipo ya está dentro de `facturado`, y `por_facturar` viene con ese anticipo
// restado, así que facturado + por_facturar == total de la venta y el tramo neutro de
// la barra es exactamente el "Por facturar" que dice la leyenda.
function AvanceMonza({ detalle }: { detalle: VentaDetalle }) {
  const { dark } = useMonzaTheme();
  const r = detalle.resumen;
  if (!r || r.por_facturar_clp === undefined) return null; // backend previo: sin datos, sin barra
  const muted = dark ? "#94A3B8" : "#64748B";
  const text = dark ? "#E2E8F0" : "#0f172a";
  const neutro = dark ? "#1e2a4a" : "#E2E8F0";
  const ambar = dark ? "#FBBF24" : "#B45309";
  const total = detalle.total_con_iva_clp || 0;
  // Un adelanto aprobado puede dejar cobrado > facturado: sin clamp la barra desborda.
  const cobrado = Math.min(r.cobrado_clp, r.facturado_clp);
  const factSinCobrar = Math.max(r.facturado_clp - cobrado, 0);
  const pct = (v: number) => (total > 0 ? Math.min(Math.max((v / total) * 100, 0), 100) : 0);
  const anticipo = r.anticipo_por_descontar_clp || 0;
  // Cifra AUTORITATIVA del backend; la reconstrucción es solo fallback (backend previo).
  const mercaderia = r.mercaderia_pendiente_clp ?? (r.por_facturar_clp || 0) + anticipo;
  // Con anticipo > mercadería la RESTA da negativo y no explica el titular (que viene
  // con clamp a 0): "Por facturar $0 (mercadería $59.500 − anticipo $119.000)" se lee
  // como un error de la pantalla. Ahí se nombran las dos cifras por separado y se dice
  // qué revisar.
  const anticipoExcede = anticipo > mercaderia;
  const dot = (color: string): React.CSSProperties =>
    ({ display: "inline-block", width: 8, height: 8, borderRadius: 999, background: color, marginRight: 6 });
  return (
    <div style={{ padding: "10px 18px", display: "flex", flexDirection: "column", gap: 8, borderBottom: `1px solid ${neutro}` }}>
      <div style={{ height: 8, borderRadius: 999, overflow: "hidden", display: "flex", background: neutro }}>
        <div style={{ width: `${pct(cobrado)}%`, background: "#10B981" }} title={`Cobrado ${fmtClp(cobrado)}`} />
        <div style={{ width: `${pct(factSinCobrar)}%`, background: "#3B82F6" }} title={`Facturado sin cobrar ${fmtClp(factSinCobrar)}`} />
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: "4px 20px", fontSize: 11, fontWeight: 600, color: muted }}>
        <span><span style={dot("#10B981")} />Cobrado <b style={{ color: text }}>{fmtClp(cobrado)}</b></span>
        <span><span style={dot("#3B82F6")} />Por cobrar <b style={{ color: text }}>{fmtClp(r.por_cobrar_clp)}</b></span>
        <span><span style={dot(neutro)} />Por facturar <b style={{ color: ambar }}>{fmtClp(r.por_facturar_clp)}</b></span>
        {anticipo > 0 && !anticipoExcede && (
          <span style={{ fontWeight: 400 }}>
            (mercadería sin facturar {fmtClp(mercaderia)} − anticipo por descontar {fmtClp(anticipo)})
          </span>
        )}
        {anticipo > 0 && anticipoExcede && (
          <span style={{ fontWeight: 400 }}>
            mercadería sin facturar <b style={{ color: text }}>{fmtClp(mercaderia)}</b> · anticipo ya facturado por
            descontar <b style={{ color: ambar }}>{fmtClp(anticipo)}</b> — el anticipo facturado supera la mercadería
            que falta por facturar: revisa los precios de esta venta.
          </span>
        )}
      </div>
    </div>
  );
}

// Facturas de la venta: misma fuente que Facturas y Cobranzas, con sus ítems al
// expandir. La factura de ANTICIPO (vía B) lleva chip propio y dice "sin guía" —nace
// sin guía de despacho, es la única excepción a la regla rectora— y las líneas de
// DESCUENTO (negativas, con anticipo_factura_id) van con ↩ y en violeta para que
// nadie las lea como mercadería.
function FacturasSection({ facturas }: { facturas: FacturaOc[] }) {
  const { dark } = useMonzaTheme();
  const [abiertas, setAbiertas] = useState<Record<number, boolean>>({});
  if (!facturas.length) return null;
  const muted = dark ? "#94A3B8" : "#64748B";
  const text = dark ? "#E2E8F0" : "#0f172a";
  const bd = dark ? "#1e2a4a" : "#E2E8F0";
  // Violeta = "plata que ya se cobró por adelantado" (el mismo tono del badge
  // Factoring), aclarado en oscuro para que el texto siga legible.
  const violeta = dark ? "#A78BFA" : "#6D28D9";
  return (
    <div style={{ padding: "10px 18px", borderBottom: `1px solid ${bd}` }}>
      <p style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: 0.5, color: muted, margin: "0 0 6px" }}>
        Facturas de esta venta ({facturas.length})
      </p>
      {facturas.map((f, idx) => {
        const abierta = !!abiertas[f.id];
        const conItems = f.items.length > 0;
        return (
          <div key={f.id} style={{ borderBottom: idx === facturas.length - 1 ? "none" : `1px solid ${bd}` }}>
            <button
              onClick={() => conItems && setAbiertas(p => ({ ...p, [f.id]: !p[f.id] }))}
              style={{ width: "100%", display: "flex", alignItems: "center", gap: 8, padding: "6px 0", fontSize: 12, textAlign: "left", background: "transparent", border: "none", cursor: conItems ? "pointer" : "default", fontFamily: "inherit", color: muted }}>
              <Receipt size={14} style={{ color: "var(--monza-accent)", flexShrink: 0 }} />
              <span style={{ fontFamily: "monospace", fontWeight: 700, whiteSpace: "nowrap", color: text }}>
                N° {f.numero_factura || `#${f.id}`}
              </span>
              {f.es_anticipo && (
                <span style={{ background: "#EDE9FE", color: "#6D28D9", fontSize: 10, fontWeight: 600, padding: "1px 7px", borderRadius: 999, whiteSpace: "nowrap" }}>Anticipo</span>
              )}
              {/* "guía —" en una factura de anticipo se leería como un dato faltante:
                  no falta nada, esa factura nace sin guía a propósito. */}
              <span style={{ whiteSpace: "nowrap" }}>{f.numero_guia ? <>Guía {f.numero_guia}</> : f.es_anticipo ? "sin guía" : "guía —"}</span>
              <span style={{ fontWeight: 700, whiteSpace: "nowrap", marginLeft: "auto", color: text }}>{fmtClp(f.monto_bruto)}</span>
              <span style={{ whiteSpace: "nowrap" }}><VencimientoLabel f={f} /></span>
              <Badge map={PAGO_BADGE} estado={f.estado_pago} />
              {conItems && (abierta ? <ChevronUp size={14} style={{ flexShrink: 0 }} /> : <ChevronDown size={14} style={{ flexShrink: 0 }} />)}
            </button>
            {abierta && conItems && (
              <div style={{ margin: "0 0 8px 24px", fontSize: 11, color: muted }}>
                {f.items.map(it => {
                  // Línea de descuento del anticipo: el monto negativo por sí solo se
                  // confunde con una corrección. El ↩ dice "esto devuelve plata ya
                  // facturada", y el color la separa de la mercadería de la guía.
                  const esDescuento = it.anticipo_factura_id != null || it.total_neto < 0;
                  const parte = [it.anticipo_factura_id != null ? "↩" : "", it.numero_parte].filter(Boolean).join(" ");
                  return (
                  <div key={it.id} style={{ display: "flex", alignItems: "center", gap: 8, padding: "2px 0" }}>
                    <span style={{ fontFamily: "monospace", color: esDescuento ? violeta : undefined }}>{parte || "—"}</span>
                    <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: esDescuento ? violeta : undefined }}>{it.descripcion || ""}</span>
                    <span style={{ marginLeft: "auto", whiteSpace: "nowrap" }}>{it.cantidad > 0 ? `${it.cantidad} × ` : ""}</span>
                    <span style={{ whiteSpace: "nowrap", fontWeight: 500, color: esDescuento ? violeta : text }}>{fmtClp(it.total_neto)} neto</span>
                  </div>
                  );
                })}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// Lo que AÚN no llega a factura, agrupado por estado logístico: responde
// "¿por qué no puedo facturar esto todavía y dónde empujo?". El total sale del
// backend (autoritativo); el cálculo cliente es SOLO fallback visual.
function PorFacturarSection({ detalle }: { detalle: VentaDetalle }) {
  const { dark } = useMonzaTheme();
  const [abiertos, setAbiertos] = useState<Record<string, boolean>>({});
  const r = detalle.resumen;
  const pendientes = detalle.items
    .map(it => ({ it, qty: qtyPendiente(it) }))
    .filter(x => x.qty > 0.001);   // tolerancia de floats en las cantidades
  if (!pendientes.length) return null;
  const muted = dark ? "#94A3B8" : "#64748B";
  const text = dark ? "#E2E8F0" : "#0f172a";
  const bd = dark ? "#1e2a4a" : "#E2E8F0";
  const ambar = dark ? "#FBBF24" : "#B45309";
  // Bruto aproximado con el factor de IVA EFECTIVO de la venta (cubre exentas:
  // factor 1) — jamás 1.19 fijo.
  const factorIva = detalle.total_neto_clp > 0 ? detalle.total_con_iva_clp / detalle.total_neto_clp : 1;
  const brutoDe = (x: { it: VentaItem; qty: number }) =>
    (x.it.cantidad > 0 ? (x.it.total_venta_clp * x.qty) / x.it.cantidad : 0) * factorIva;
  const totalPend = pendientes.reduce((s, x) => s + brutoDe(x), 0);
  const grupos = GRUPOS_PENDIENTE
    .map(g => ({ ...g, filas: pendientes.filter(x => g.estados.includes(x.it.estado_linea)) }))
    .filter(g => g.filas.length > 0);
  // Bucket residual: un estado_linea nuevo del backend NO debe perder ítems.
  const sinGrupo = pendientes.filter(x => !GRUPOS_PENDIENTE.some(g => g.estados.includes(x.it.estado_linea)));
  if (sinGrupo.length) grupos.push({ estados: [], label: "Otro estado", dot: "#64748B", hint: "", filas: sinGrupo });
  // Los montos POR GRUPO suman la MERCADERÍA pendiente, no el titular (que ya viene con
  // el anticipo restado). Es a propósito y por eso el titular nombra las dos cifras: el
  // anticipo es plata de la venta completa, no pertenece a ningún estado logístico —
  // prorratearlo entre los grupos inventaría un dato que nadie puede accionar.
  const anticipo = r?.anticipo_por_descontar_clp || 0;
  const mercaderia = r?.mercaderia_pendiente_clp ?? totalPend;
  // Ver AvanceMonza: con anticipo > mercadería la resta no da el titular (clamp a 0).
  const anticipoExcede = anticipo > mercaderia;
  return (
    <div style={{ padding: "10px 18px", borderBottom: `1px solid ${bd}` }}>
      <p style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: 0.5, color: muted, margin: "0 0 6px" }}>
        Por facturar · <span style={{ color: ambar }}>{fmtClp(r?.por_facturar_clp ?? totalPend)}</span>
        {anticipo > 0 && !anticipoExcede && (
          <span style={{ textTransform: "none", fontWeight: 400 }}>
            {" "}(mercadería {fmtClp(mercaderia)} − anticipo {fmtClp(anticipo)})
          </span>
        )}
        {anticipo > 0 && anticipoExcede && (
          <span style={{ textTransform: "none", fontWeight: 400 }}>
            {" "}· mercadería {fmtClp(mercaderia)} · anticipo ya facturado {fmtClp(anticipo)} — el anticipo supera la
            mercadería que falta por facturar: revisa los precios de esta venta.
          </span>
        )}
      </p>
      {grupos.map((g, idx) => {
        const abierto = !!abiertos[g.label];
        const monto = g.filas.reduce((s, x) => s + brutoDe(x), 0);
        return (
          <div key={g.label} style={{ borderBottom: idx === grupos.length - 1 ? "none" : `1px solid ${bd}` }}>
            <button onClick={() => setAbiertos(p => ({ ...p, [g.label]: !p[g.label] }))}
              style={{ width: "100%", display: "flex", alignItems: "center", gap: 8, padding: "6px 0", fontSize: 12, textAlign: "left", background: "transparent", border: "none", cursor: "pointer", fontFamily: "inherit", color: muted }}>
              <span style={{ display: "inline-block", width: 8, height: 8, borderRadius: 999, background: g.dot, flexShrink: 0 }} />
              <span style={{ fontWeight: 600, whiteSpace: "nowrap", color: text }}>{g.label}</span>
              {g.hint && <span style={{ whiteSpace: "nowrap" }}>· {g.hint}</span>}
              <span>· {g.filas.length} ítem{g.filas.length === 1 ? "" : "s"}</span>
              <span style={{ fontWeight: 700, whiteSpace: "nowrap", marginLeft: "auto", color: text }}>{fmtClp(monto)}</span>
              {abierto ? <ChevronUp size={14} style={{ flexShrink: 0 }} /> : <ChevronDown size={14} style={{ flexShrink: 0 }} />}
            </button>
            {abierto && (
              <div style={{ margin: "0 0 8px 24px", fontSize: 11, color: muted }}>
                {g.filas.map(x => (
                  <div key={x.it.id} style={{ display: "flex", alignItems: "center", gap: 8, padding: "2px 0" }}>
                    <span style={{ fontFamily: "monospace", fontWeight: 600, color: text }}>{x.it.numero_parte || "—"}</span>
                    <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{x.it.descripcion}</span>
                    <span style={{ marginLeft: "auto", whiteSpace: "nowrap" }}>
                      {x.qty < x.it.cantidad ? `quedan ${x.qty} de ${x.it.cantidad}` : `${x.qty} un.`}
                    </span>
                    <span style={{ whiteSpace: "nowrap", fontWeight: 500, color: text }}>{fmtClp(brutoDe(x))}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// Tabla completa de ítems (la de siempre), con control anti-muro: sobre el umbral
// parte PLEGADA y al abrirla trae buscador por N° de parte / descripción / marca.
function ItemsPlegables({ items }: { items: VentaItem[] }) {
  const { dark } = useMonzaTheme();
  const esGrande = items.length > UMBRAL_TABLA_PLEGADA;
  const [abierta, setAbierta] = useState(!esGrande);
  const [filtro, setFiltro] = useState("");
  const muted = dark ? "#94A3B8" : "#64748B";
  const text = dark ? "#E2E8F0" : "#0f172a";
  const bd = dark ? "#1e2a4a" : "#E2E8F0";
  const sub = dark ? "#131b3e" : "#F8FAFC";
  const fl = filtro.trim().toLowerCase();
  const visibles = !fl ? items : items.filter(it =>
    (it.numero_parte || "").toLowerCase().includes(fl) ||
    (it.descripcion || "").toLowerCase().includes(fl) ||
    (it.marca || "").toLowerCase().includes(fl)
  );
  // N° de línea de la VENTA (posición en la cotización, que es el orden con el que se
  // imprime y con el que habla el cliente). Se fija ANTES de filtrar: con el buscador
  // activo, numerar las filas visibles renombraría la línea 7 como "1".
  const nroLinea = new Map(items.map((it, i) => [it.id, i + 1]));
  return (
    <div style={{ borderBottom: `1px solid ${bd}` }}>
      <div style={{ padding: "8px 18px", display: "flex", alignItems: "center", gap: 12 }}>
        <button onClick={() => setAbierta(a => !a)}
          style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: 0.5, color: muted, background: "transparent", border: "none", cursor: "pointer", fontFamily: "inherit", padding: 0 }}>
          {abierta ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
          Ítems ({items.length})
        </button>
        {abierta && esGrande && (
          <div style={{ position: "relative", flex: 1, maxWidth: 280, marginLeft: "auto" }}>
            <Search size={14} style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)", color: muted }} />
            <input value={filtro} onChange={e => setFiltro(e.target.value)}
              placeholder="Buscar N° de parte, descripción…"
              style={{ width: "100%", fontSize: 12, borderRadius: 8, border: `1px solid ${bd}`, background: dark ? "#0d1430" : "white", color: text, padding: "6px 8px 6px 30px", fontFamily: "inherit", boxSizing: "border-box" }} />
          </div>
        )}
      </div>
      {abierta && (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", fontSize: 12, borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ background: sub, borderBottom: `1px solid ${bd}` }}>
                {["#", "N° Parte", "Descripción", "Cant.", "P. Unit", "Total", "Guía", "Factura", "Plazo / Vence", "Estado pago"].map(h => (
                  <th key={h} style={{ textAlign: "left", padding: "8px 12px", fontSize: 10, textTransform: "uppercase", letterSpacing: 0.5, color: muted, whiteSpace: "nowrap" }}
                    title={h === "#" ? "N° de línea de la venta" : undefined}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {visibles.length === 0 && (
                <tr><td colSpan={10} style={{ padding: "14px 12px", textAlign: "center", fontStyle: "italic", color: muted }}>
                  Ningún ítem coincide con "{filtro}"
                </td></tr>
              )}
              {visibles.map((item, idx) => {
                const facs = item.facturas;
                // Estado agregado multi-factura: todas iguales → esa; contiene
                // vencida → vencida; mezcla → parcial.
                const estados = facs.map(x => x.estado_pago);
                const agg = !estados.length ? null : estados.every(e => e === estados[0]) ? estados[0] : estados.includes("vencida") ? "vencida" : "parcial";
                return (
                  <tr key={item.id} style={{ background: idx % 2 ? sub : "transparent", borderBottom: `1px solid ${bd}` }}>
                    <td style={{ padding: "8px 12px", fontFamily: "ui-monospace, monospace", fontSize: 11, color: muted }}>{nroLinea.get(item.id)}</td>
                    <td style={{ padding: "8px 12px", fontWeight: 600, color: text, whiteSpace: "nowrap" }}>{item.numero_parte || "—"}</td>
                    <td style={{ padding: "8px 12px", color: text, maxWidth: 200, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={item.descripcion}>{item.descripcion}</td>
                    <td style={{ padding: "8px 12px", textAlign: "right", color: muted }}>{item.cantidad}</td>
                    <td style={{ padding: "8px 12px", textAlign: "right", color: text, whiteSpace: "nowrap" }}>{fmtClp(item.precio_unit_venta_clp)}</td>
                    <td style={{ padding: "8px 12px", textAlign: "right", fontWeight: 700, color: "var(--monza-accent)", whiteSpace: "nowrap" }}>{fmtClp(item.total_venta_clp)}</td>
                    <td style={{ padding: "8px 12px", color: muted, whiteSpace: "nowrap" }}>
                      {item.guias.length ? <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}><Truck size={12} style={{ color: "#15803D" }} />{item.guias.map(g => g.numero_guia || g.numero_despacho).join(", ")}</span> : <span style={{ fontStyle: "italic", color: muted }}>sin despachar</span>}
                    </td>
                    <td style={{ padding: "8px 12px", color: muted, whiteSpace: "nowrap" }}>
                      {facs.length ? <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}><Receipt size={12} style={{ color: "var(--monza-accent)" }} />{facs.map(x => x.numero_factura || `#${x.factura_id}`).join(", ")}</span> : <span style={{ fontStyle: "italic", color: muted }}>sin facturar</span>}
                    </td>
                    <td style={{ padding: "8px 12px", color: muted, whiteSpace: "nowrap" }}>
                      {facs.length === 1 ? <span>{facs[0].plazo_dias ? `${facs[0].plazo_dias}d · ` : ""}{fmtDate(facs[0].fecha_vencimiento)}</span>
                        : facs.length > 1 ? <span>varias</span> : "—"}
                    </td>
                    <td style={{ padding: "8px 12px" }}>{agg ? <Badge map={PAGO_BADGE} estado={agg} /> : "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ─── Adelanto: la ORDEN la da Tesorería ────────────────────────────────────────
// La verificación/aprobación del adelanto (50%) se movió a Contabilidad → Tesorería
// (pestaña Aprobaciones, POST /api/monza/tesoreria/aprobaciones/{cot}/aprobar).
// Acá la venta solo MUESTRA el estado (solo lectura), pero con TODO el detalle: sin
// saber cuánto del adelanto ya se aplicó a una factura y cuánto sigue esperando, es fácil
// volver a cobrarle al cliente el mismo depósito.
// Tolerancia en PESOS del monto aplicado (mismo criterio que Tesorería y Grupo AM): un
// adelanto repartido entre facturas puede quedar a $1 por el redondeo del IVA.
const TOL_APLICADO = 1;

function AdelantoSection({ detalle }: { detalle: VentaDetalle }) {
  const { dark } = useMonzaTheme();
  const muted = dark ? "#94A3B8" : "#64748B";
  const faint = dark ? "#64748B" : "#94A3B8";
  const text = dark ? "#E2E8F0" : "#0f172a";
  const bd = dark ? "#1e2a4a" : "#E2E8F0";
  const ambar = dark ? "#FBBF24" : "#B45309";
  const a = detalle.adelanto;
  const pct = detalle.pct_adelanto || 0;
  // Solo con un adelanto REGISTRADO: el caso "informado, esperando a Tesorería" ya lo
  // dicen el badge del encabezado y la nota al pie de la tarjeta — repetirlo sería ruido.
  if (!a) return null;
  return (
    <div style={{ padding: "10px 18px", borderTop: `1px solid ${bd}` }}>
      <p style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: 0.5, color: muted, margin: "0 0 6px", display: "flex", alignItems: "center", gap: 6 }}>
        <HandCoins size={13} /> Adelanto del cliente{pct > 0 ? ` · ${pct}%` : ""}
      </p>
      <div style={{ display: "flex", flexWrap: "wrap", gap: "4px 18px", fontSize: 12, color: muted }}>
        <span>Aprobado <b style={{ color: text }}>{fmtClp(a.monto)}</b>{a.fecha_pago ? ` · ${fmtDate(a.fecha_pago)}` : ""}</span>
        <span>
          {a.monto_aplicado >= a.monto - TOL_APLICADO
            ? <b style={{ color: "#15803D" }}>Aplicado a factura</b>
            : a.monto_aplicado > 0
              ? <>Aplicado <b style={{ color: text }}>{fmtClp(a.monto_aplicado)}</b> · queda <b style={{ color: ambar }}>{fmtClp(Math.max(a.monto - a.monto_aplicado, 0))}</b></>
              : <span style={{ color: faint }}>Se aplicará al facturar</span>}
        </span>
        {(a.banco || a.numero_operacion) && (
          <span>{a.banco || ""}{a.banco && a.numero_operacion ? " · " : ""}{a.numero_operacion ? `op ${a.numero_operacion}` : ""}</span>
        )}
        {/* Respaldo tributario de esa plata (factura de anticipo, vía B). */}
        {(a.factura_anticipo_folio || detalle.factura_anticipo_folio) && (
          <span style={{ color: "#6D28D9" }}>respaldo Factura N° {a.factura_anticipo_folio || detalle.factura_anticipo_folio}</span>
        )}
        {/* Solo si el backend lo publica (hoy vive en la cola de Tesorería). */}
        {a.conciliado_banco === true && <span style={{ color: "#15803D" }}>conciliado con el banco ✓</span>}
        {a.conciliado_banco === false && <span style={{ color: faint }}>sin conciliar con el banco</span>}
        {a.observaciones && <span style={{ color: faint }}>{a.observaciones}</span>}
      </div>
    </div>
  );
}

// ─── Tarjeta de venta (por cotización, expandible) ────────────────────────────
function VentaCard({ venta, onChanged }: { venta: VentaRow; onChanged: () => void }) {
  const { dark } = useMonzaTheme();
  const [open, setOpen] = useState(false);
  const [detalle, setDetalle] = useState<VentaDetalle | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(false);
  // OC del cliente (N° + FECHA). La FECHA no viaja en el payload de Contabilidad y es la
  // referencia 801 del DTE: se trae de la cotización, en paralelo con el detalle.
  const [oc, setOc] = useState<OcCotizacion | null>(null);
  const [editandoOc, setEditandoOc] = useState(false);
  const rol = useAuthStore(s => s.user)?.rol;
  const puedeEditarOc = !rol || ROLES_EDITAN_OC.includes(rol);

  const card = { background: dark ? "#131b3e" : "white", border: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}`, borderRadius: 14, overflow: "hidden" } as React.CSSProperties;
  const muted = dark ? "#94A3B8" : "#64748B";
  const text = dark ? "#E2E8F0" : "#0f172a";
  const sub = dark ? "#131b3e" : "#F8FAFC";

  const fetchDetalle = async () => {
    setLoading(true); setErr(false);
    // Un catch POR llamada: si la cotización falla, el detalle de la venta se muestra
    // igual (y al revés) — un solo catch dejaba la tarjeta en "no se pudo cargar".
    const [det, cot] = await Promise.all([
      monzaContabilidadAPI.ventaDetalle(venta.cotizacion_id)
        .then(({ data }) => data as VentaDetalle).catch(() => null),
      monzaCotizacionesAPI.get(venta.cotizacion_id)
        .then(({ data }) => data as OcCotizacion).catch(() => null),
    ]);
    if (det) setDetalle(det); else setErr(true);
    if (cot) setOc({ oc_cliente: cot.oc_cliente ?? null, oc_fecha: cot.oc_fecha ?? null });
    setLoading(false);
  };
  // Se recarga también si falta la OC: si esa llamada falló la vez anterior, al volver a
  // abrir la tarjeta se reintenta (si no, la fecha de la OC quedaba en "—" para siempre).
  const toggle = async () => { const next = !open; setOpen(next); if (next && (!detalle || !oc)) await fetchDetalle(); };
  const toggleFirma = async (despachoId: number, firmada: boolean) => {
    try { await monzaContabilidadAPI.marcarGuiaFirmada(despachoId, { firmada }); await fetchDetalle(); }
    catch { /* la firma es opcional: si falla, no bloquea nada */ }
  };
  // Folio de la factura de ANTICIPO (vía B) de esta venta. Se lee de la RAÍZ de la fila
  // y, si el backend todavía no la publica ahí, del adelanto. NO cuelga del estado del
  // adelanto: con el anticipo emitido antes de que Tesorería apruebe, el documento
  // existe y la pantalla no lo mencionaba nunca.
  const folioAnticipo = venta.factura_anticipo_folio || venta.adelanto?.factura_anticipo_folio || null;
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
            <CobranzaBadge estado={venta.estado_cobranza} faltaFacturar={(venta.por_facturar_clp || 0) > 0} />
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
            {/* Vía B: chip PROPIO, sin depender del estado del adelanto. El folio a la
                vista evita abrir la venta para saber si esa plata tiene documento
                tributario; anidado en el chip del adelanto, no se veía nunca cuando el
                anticipo se emitió antes de que Tesorería aprobara. */}
            {folioAnticipo && (
              <span title="Esta venta tiene factura de anticipo (respalda el adelanto ante el SII). Al facturar el despacho real se descuenta sola."
                style={{ background: "#EDE9FE", color: "#6D28D9", fontSize: 11, fontWeight: 600, padding: "2px 8px", borderRadius: 999, whiteSpace: "nowrap" }}>
                Anticipo facturado N° {folioAnticipo}
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
          {/* "Por cobrar" (azul, saldo de lo YA facturado) y "Por facturar" (ámbar,
              mercadería sin factura) son conceptos DISTINTOS — no fusionar. El ámbar
              solo aparece si hay plata pendiente de facturar: se escanea la lista
              sin expandir (espejo del contrato visual G15 de GA). */}
          <span style={{ fontSize: 12, fontWeight: 700, color: dark ? "#60A5FA" : "#1D4ED8" }}>Por cobrar {fmtClp(venta.por_cobrar_clp)}</span>
          {(venta.por_facturar_clp || 0) > 0 && (
            <span style={{ fontSize: 12, fontWeight: 700, color: dark ? "#FBBF24" : "#B45309" }}>Por facturar {fmtClp(venta.por_facturar_clp!)}</span>
          )}
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
              {/* OC del cliente: N° y FECHA a la vista + edición. Los dos se imprimen como
                  referencia 801 del DTE, así que este es el único lugar donde el contador
                  puede corregirlos (con un documento vivo el backend responde 409). */}
              <div style={{ padding: "8px 18px", display: "flex", alignItems: "center", flexWrap: "wrap", gap: "4px 14px", fontSize: 12, color: muted, borderBottom: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}`, background: sub }}>
                <span>
                  OC del cliente: <b style={{ color: (oc?.oc_cliente || detalle.oc_cliente) ? text : "#B45309" }}>
                    {oc?.oc_cliente || detalle.oc_cliente || "sin N°"}
                  </b>
                </span>
                <span>
                  Fecha OC: <b style={{ color: oc?.oc_fecha ? text : "#B45309" }}>{oc?.oc_fecha ? fmtDate(oc.oc_fecha) : "sin fecha"}</b>
                </span>
                <span style={{ fontSize: 11, color: dark ? "#64748B" : "#94A3B8" }}>· van como referencia 801 del DTE</span>
                {puedeEditarOc && (
                  <button onClick={() => setEditandoOc(true)}
                    style={{ marginLeft: "auto", display: "inline-flex", alignItems: "center", gap: 4, fontSize: 11, fontWeight: 600, color: "var(--monza-accent)", background: "none", border: "none", cursor: "pointer", fontFamily: "inherit", padding: "2px 4px" }}>
                    <Pencil size={12} /> Editar OC
                  </button>
                )}
              </div>
              {/* Ejecutivo primero: avance de la plata, facturas y lo pendiente.
                  El detalle ítem a ítem va al final, plegado si la venta es grande. */}
              <AvanceMonza detalle={detalle} />
              <FacturasSection facturas={detalle.facturas || []} />
              <PorFacturarSection detalle={detalle} />
              <ItemsPlegables items={detalle.items} />
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
              {/* Adelanto de la venta, con TODO su detalle: estado, cuánto se aplicó ya a
                  facturas, cuánto queda, el folio del respaldo tributario y si el depósito
                  está cruzado con el banco. Sin esto la tarjeta solo decía "aprobado" y no
                  se podía saber si esa plata ya se descontó de una factura. */}
              <AdelantoSection detalle={detalle} />
              <div style={{ padding: "10px 18px", display: "flex", flexWrap: "wrap", justifyContent: "flex-end", gap: 24, fontSize: 12, fontWeight: 600, background: sub, borderTop: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}` }}>
                <span style={{ color: muted }}>Neto: <b style={{ color: text }}>{fmtClp(detalle.total_neto_clp)}</b></span>
                <span style={{ color: muted }}>IVA: <b style={{ color: text }}>{fmtClp(detalle.iva_clp)}</b></span>
                <span style={{ color: muted }}>Total: <b style={{ color: "var(--monza-accent)" }}>{fmtClp(detalle.total_con_iva_clp)}</b></span>
              </div>
            </>
          )}
        </div>
      )}

      {editandoOc && (
        <OcClienteEditModal
          cotizacionId={venta.cotizacion_id}
          numeroCotizacion={venta.numero_cotizacion}
          inicial={oc ?? { oc_cliente: venta.oc_cliente, oc_fecha: null }}
          onClose={() => setEditandoOc(false)}
          onSaved={(nueva) => { setOc(nueva); onChanged(); }}
        />
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
