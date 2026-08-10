// Página "Facturas y Cobranzas" (MonzaParts, cuentas por cobrar): lista facturas +
// antigüedad de cartera, y concentra las acciones — EMITIR factura (desde una guía
// despachada), registrar cobranzas y gestionar factoring. Consume monzaContabilidadAPI.
import { useState, useEffect, useCallback, useRef } from "react";
import {
  Receipt, Plus, Search, AlertCircle, CheckCircle2, DollarSign,
  Loader2, RefreshCw, ChevronDown, ChevronUp, CreditCard, Landmark, X, Trash2,
  FileText, Send, AlertTriangle, Clock, HandCoins, Undo2, KeyRound,
} from "lucide-react";
import toast from "react-hot-toast";
import { useMonzaTheme } from "./MonzaLayout";
import { fmtClp, hoyLocal } from "../utils/format";
import { monzaContabilidadAPI, monzaDespachosAPI, monzaWasabilAPI } from "../services/monzaApi";
import type {
  MonzaDteFacturaInfo, MonzaDescuentoAnticipo, MonzaFacturaPayload, MonzaFacturaPreview,
} from "../services/monzaApi";
// La guía firmada se abre con monzaDespachosAPI.abrirGuiaFirmada (2026-08-06): el
// abrirDocumento de services/api.ts pega al serve de GA, que exige empresa 'mineria'
// y a un usuario Monza le respondía 403 al ver su propia guía.
// Confirmación de folio COMPARTIDA con Despachos (guía 52): una sola implementación de
// la doble digitación para las dos pantallas donde aparece el callejón "emitido sin folio".
import MonzaRegistrarFolioModal from "./MonzaRegistrarFolioModal";

// ─── Tipos ────────────────────────────────────────────────────────────────────
interface Cobranza { id: number; fecha: string | null; monto: number; medio: string; es_factoring?: boolean; banco: string | null; numero_operacion: string | null; observaciones: string | null }
interface Factoring { id: number; empresa_factoring: string | null; id_operacion: string | null; fecha_operacion: string | null; monto_adelantado: number; costo_factoring: number; retencion: number; banco: string | null; estado: string; fecha_liquidacion: string | null }
// anticipo_factura_id: la línea es un DESCUENTO de anticipo (total_neto negativo) y
// apunta a la factura de anticipo que descuenta.
interface FacturaItem { id: number; item_cotizacion_id?: number | null; despacho_item_id?: number | null; anticipo_factura_id?: number | null; numero_parte: string | null; descripcion: string | null; cantidad: number; precio_unit_neto: number; total_neto: number }
interface Factura {
  id: number; numero_factura: string | null; tipo_doc: string; es_anticipo?: boolean;
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

/** Normaliza el error del backend a texto legible: FastAPI/Pydantic devuelve `detail`
 *  como ARRAY de objetos {loc,msg,...} en los 422, y sin esto el toast muestra
 *  "[object Object]" — justo cuando el usuario más necesita entender qué pasó. */
function errMsg(e: unknown, fallback: string): string {
  const d = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (Array.isArray(d)) return d.map((x) => (x as { msg?: string })?.msg || JSON.stringify(x)).join("; ");
  if (typeof d === "string") return d;
  return fallback;
}

/** Muestra las `advertencias` que el backend devuelve al REGISTRAR una factura (vía
 *  manual): un 200 puede traer avisos que cambian lo que el usuario cree que pasó
 *  ("el descuento por anticipo deja esta factura en $0", "no se pudo mover el adelanto
 *  a esta factura"). Antes se descartaban y en pantalla quedaba una factura rara sin
 *  ninguna explicación. Duran más que un toast normal: hay que alcanzar a leerlas. */
function avisarAdvertencias(avisos?: string[]): void {
  (avisos || []).forEach(a =>
    toast(a, { duration: 9000, icon: <AlertTriangle size={16} color="#B45309" /> }));
}

/** Plazo (en días) que se PRECARGA desde la condición de pago pactada en la venta
 *  (`cotizacion.forma_pago`, que el backend publica como `cond_pago`).
 *
 *  POR QUÉ: el formulario nacía con "30" fijo y nadie leía la condición real, así que
 *  TODA factura de Monza salía a 30 días — un cliente al contado recibía 30 días de
 *  crédito en silencio y la antigüedad de cartera quedaba mal en todas.
 *
 *  DIVERGENCIA DELIBERADA con Grupo AM (FacturasPage.tsx:398-402): allá el número se
 *  busca con /(\d+)/ suelto. En Monza las condiciones vienen de PAGO_OPCIONES
 *  (constants/adelanto.ts) e incluyen "50% adelanto": con el regex suelto eso daría
 *  50 DÍAS de crédito. Acá el número solo cuenta si viene pegado a "día(s)". */
function plazoDeCondPago(cond?: string | null): string {
  const cp = (cond || "").toLowerCase();
  const dias = cp.match(/(\d+)\s*d[íi]as?/);
  if (dias) return dias[1];
  if (cp.includes("contado") || cp.includes("contra entrega")) return "0";
  return "30";  // condición no reconocida (o venta sin condición): default 30
}

/** Tope de ids por consulta que acepta GET /wasabil/facturas/estado-batch (el backend
 *  responde 400 pasado ese número). Se pide por TANDAS, no se recorta la lista. */
const TOPE_BATCH_SII = 200;

/** Encabezado de las tablas de previsualización (el color lo pone quien la usa, porque
 *  depende del tema). */
const THPrev: React.CSSProperties = {
  padding: "5px 10px", fontWeight: 600, fontSize: 10, textTransform: "uppercase", letterSpacing: 0.5,
};

/** Etiqueta del IVA con la tasa REAL que devuelve el backend (`iva_rate` como fracción:
 *  0.19 = 19%). Nunca un 19% escrito a mano: en Monza la tasa se congela por venta. */
function etiquetaIva(rate?: number | null): string {
  return rate != null
    ? `IVA ${(rate * 100).toLocaleString("es-CL", { maximumFractionDigits: 1 })}%`
    : "IVA";
}

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
// `wide` (620 px + scroll interno, igual que el modal de guías de MonzaDespachosPage)
// es obligatorio para el flujo SII: receptor + referencias + tabla de líneas +
// totales no caben en los 440 px del formulario y se cortaban.
function Modal({ title, onClose, children, wide }: { title: string; onClose: () => void; children: React.ReactNode; wide?: boolean }) {
  const s = useStyles();
  return (
    <div style={{ position: "fixed", inset: 0, zIndex: 300, display: "flex", alignItems: "center", justifyContent: "center", background: "rgba(0,0,0,0.6)", padding: 16 }} onClick={onClose}>
      <div style={{ width: "100%", maxWidth: wide ? 620 : 440, maxHeight: "90vh", overflowY: "auto", borderRadius: 14, border: s.cardBd, background: s.cardBg, boxShadow: "0 20px 50px rgba(0,0,0,0.4)" }} onClick={e => e.stopPropagation()}>
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

// ─── Emisión de factura electrónica al SII (DTE 33 vía Wasabil) ───────────────
// Mismo patrón de 2 pasos que la guía 52 (EmitirGuiaSIIModal de MonzaDespachosPage):
// PREVIEW (no toca el SII: receptor real de la ficha Wasabil + referencias 801/52 +
// líneas congeladas) → EMITIR con el OK explícito del usuario (IRREVERSIBLE) →
// sondeo hasta Emitido (folio + PDF) o Fallido (reintento seguro: el backend nunca
// emite dos veces). Se usa desde el formulario de creación (payload NUEVO: todavía
// no existe la factura) y desde la fila de una factura ya creada (facturaId).
const SONDEO_INTERVALO_MS = 3000; // el envío al SII es asíncrono (segundos a minutos)
const SONDEO_MAX_INTENTOS = 30;   // ~90 s: después pasa a 'pendiente' (seguir desde la lista)
const REF_SII_LABEL: Record<string, string> = {
  "801": "Orden de compra", "52": "Guía de despacho", "33": "Factura de anticipo",
};
type FaseFactura = "cargando" | "preview" | "emitiendo" | "sondeo" | "exito" | "fallido" | "pendiente" | "error";

interface PreviewFactura {
  puede_emitir: boolean;
  problemas?: string[];
  advertencias?: string[];
  receptor?: { razon_social?: string | null; rut?: string | null; giro?: string | null; direccion?: string | null; comuna?: string | null; ciudad?: string | null; fuente?: string };
  lineas?: Array<{ numero_parte?: string | null; descripcion?: string | null; cantidad: number; precio_unit_neto: number; total_neto: number }>;
  totales?: { neto: number; iva: number; bruto: number; iva_rate?: number };
  referencias?: Array<{ tipo: string | number; folio?: string | null; fecha?: string | null; descripcion?: string | null }>;
  sin_guia?: boolean;
  // Fase 7: la factura ES un anticipo / la factura DESCUENTA anticipos previos.
  es_anticipo?: boolean;
  descuentos?: MonzaDescuentoAnticipo[];
}

// Fase 7: los tres endpoints SII de factura (emitir / estado / reintentar) devuelven
// `advertencias` JUNTO al DTE — avisos que nacen al confirmarse el folio, cuando el
// backend aplica el adelanto que la emisión había diferido y no logra re-encauzarlo
// hacia la factura de anticipo (factoring vigente en la otra factura, cobranza ya
// conciliada con el banco, DTE sin emitir). El tipo compartido MonzaDteFacturaInfo
// (services/monzaApi.ts) todavía no declara el campo, así que se extiende acá.
type RespuestaSIIFactura = MonzaDteFacturaInfo & { advertencias?: string[] };

function EmisionFacturaSIIModal({ payload, facturaId, onDone, onVolver, onCerrar, onBusy }: {
  payload?: MonzaFacturaPayload;      // emisión NUEVA (payload de crearFactura SIN folio)
  facturaId?: number;                 // retomar / reintentar una factura ya creada
  onDone: () => void;                 // refresca la lista (la factura pudo crearse o cambiar)
  onVolver?: () => void;              // volver al formulario — SOLO antes de emitir
  onCerrar: () => void;
  onBusy?: (b: boolean) => void;      // avisa al Modal padre que NO debe cerrarse
}) {
  const s = useStyles();
  const [fase, setFase] = useState<FaseFactura>("cargando");
  const [prev, setPrev] = useState<PreviewFactura | null>(null);
  const [dte, setDte] = useState<MonzaDteFacturaInfo | null>(null);
  const [error, setError] = useState("");
  // Advertencias que llegan CON el folio (ver RespuestaSIIFactura). El backend las
  // genera UNA sola vez —en el request que graba el folio, sea el `emitir` o una
  // pasada del sondeo—, así que hay que quedarse con ellas apenas aparecen: la
  // siguiente respuesta ya viene vacía y se perderían. Se acumulan sin repetir.
  const [avisosSii, setAvisosSii] = useState<string[]>([]);
  const recordarAvisos = (data: RespuestaSIIFactura) => {
    const nuevos = data.advertencias || [];
    if (nuevos.length === 0) return;
    setAvisosSii(prev => [...prev, ...nuevos.filter(a => !prev.includes(a))]);
  };
  // PUERTA DE UNA SOLA DIRECCIÓN: apenas se dispara el POST /emitir este modal NUNCA
  // vuelve al formulario. El backend PUDO crear la factura (y hasta emitirla) aunque
  // la respuesta se perdiera; re-enviar el formulario crearía un SEGUNDO documento
  // tributario real. Por lo mismo aquí NO se resincroniza con el preview como hace el
  // modal de guías: allá el preview es por despacho.id, acá es por un payload sin id.
  const [emisionIntentada, setEmisionIntentada] = useState(false);

  // Única fuente de verdad del botón Reintentar: puede_reintentar del backend.
  const faseSegunDte = (d?: MonzaDteFacturaInfo | null): FaseFactura => {
    if (d?.estado === "emitido") return "exito";
    if (d?.puede_reintentar) return "fallido";
    if (d?.uuid) return "sondeo";
    return "pendiente";  // claim en vuelo de otro intento (otra pestaña u otro usuario)
  };

  // Carga inicial: preview de una emisión nueva, o estado real de una factura ya creada.
  useEffect(() => {
    let vivo = true;
    if (payload) {
      monzaWasabilAPI.previewFacturaSII(payload)
        .then(({ data }) => { if (vivo) { setPrev(data as PreviewFactura); setFase("preview"); } })
        .catch((e: unknown) => { if (vivo) { setError(errMsg(e, "No se pudo previsualizar la factura")); setFase("error"); } });
    } else if (facturaId) {
      monzaWasabilAPI.estadoFacturaSII(facturaId)
        .then(({ data }) => { if (vivo) { recordarAvisos(data); setDte(data); setFase(faseSegunDte(data)); if (data.estado === "emitido") onDone(); } })
        .catch((e: unknown) => { if (vivo) { setError(errMsg(e, "No se pudo consultar el estado SII")); setFase("error"); } });
    }
    return () => { vivo = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Sondeo: la emisión al SII es asíncrona. El id sale del DTE (una emisión nueva
  // recién ahí conoce su factura_id) y si no, del que abrió el modal.
  useEffect(() => {
    if (fase !== "sondeo") return;
    const id = dte?.factura_id || facturaId;
    if (!id) { setFase("pendiente"); return; }
    let vivo = true;
    let intentos = 0;
    const tick = async () => {
      if (!vivo) return;
      intentos += 1;
      try {
        const { data } = await monzaWasabilAPI.estadoFacturaSII(id);
        if (!vivo) return;
        // ANTES de decidir la fase: la pasada que confirma el folio es la que trae el
        // aviso, y es la misma que corta el sondeo con `return`.
        recordarAvisos(data);
        setDte(data);
        if (data.estado === "emitido") { setFase("exito"); onDone(); return; }
        if (data.puede_reintentar) { setFase("fallido"); onDone(); return; }
      } catch { /* error transitorio: se reintenta en el próximo tick */ }
      if (intentos >= SONDEO_MAX_INTENTOS) { setFase("pendiente"); return; }
      window.setTimeout(tick, SONDEO_INTERVALO_MS);
    };
    tick();
    return () => { vivo = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fase]);

  // Con el envío en vuelo el modal no debe cerrarse (ni con clic al fondo): el
  // documento puede estar naciendo ante el SII y cerrar daría falsa sensación de
  // "no pasó nada". El sondeo solo bloquea en la emisión NUEVA (payload): ahí el
  // usuario acaba de disparar el documento y debe ver su desenlace. Abierto desde
  // la lista para MIRAR una emisión ajena, cerrar es inocuo (el badge de la fila
  // sigue mostrando el estado) y bloquear 90 s dejaría la ventana secuestrada.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { onBusy?.(fase === "emitiendo" || (!!payload && fase === "sondeo")); }, [fase]);
  // Al cerrar SIEMPRE se refresca la lista: la factura pudo quedar creada (y con
  // folio) aunque el usuario cierre antes de que termine el sondeo.
  const cerrar = () => { onDone(); onCerrar(); };

  const procesarRespuesta = (data: RespuestaSIIFactura) => {
    recordarAvisos(data);
    setDte(data);
    if (data.estado === "emitido") { setFase("exito"); onDone(); }
    else if (data.puede_reintentar) { setFase("fallido"); onDone(); }
    else setFase("sondeo");
  };
  const emitir = async () => {
    setEmisionIntentada(true);
    setFase("emitiendo"); setError("");
    try { procesarRespuesta((await monzaWasabilAPI.emitirFacturaSII(payload!)).data); }
    catch (e: unknown) {
      // 409 (datos / emisión en curso) o 502 (sin confirmación de Wasabil): la factura
      // PUDO quedar creada. NO se vuelve al formulario ni se resincroniza el preview:
      // se refresca la LISTA, donde aparecerá como "SII en proceso" o "SII fallida" y
      // se puede reintentar desde ahí (el backend nunca emite dos veces).
      setError(errMsg(e, "No se pudo emitir la factura"));
      setFase("error"); onDone();
    }
  };
  const reintentar = async () => {
    const id = dte?.factura_id || facturaId;
    if (!id) return;
    setFase("emitiendo"); setError("");
    try { procesarRespuesta((await monzaWasabilAPI.reintentarFacturaSII(id)).data); }
    catch (e: unknown) { setError(errMsg(e, "No se pudo reintentar la emisión")); setFase("error"); onDone(); }
  };

  const receptor = prev?.receptor;
  // Tasa de IVA REAL de la venta (iva_pct congelado → config): jamás un 19% fijo.
  const ivaLabel = prev?.totales?.iva_rate != null
    ? `IVA ${(prev.totales.iva_rate * 100).toLocaleString("es-CL", { maximumFractionDigits: 1 })}%`
    : "IVA";
  // "Esta factura de anticipo no lleva referencia a guía (52)" NO es una advertencia:
  // en un anticipo es exactamente lo esperado (no ampara traslado). En la caja ÁMBAR
  // se leía como un problema. Se separa por el prefijo que pone el backend y se pinta
  // como NOTA informativa; el resto de las advertencias sigue en ámbar.
  const NOTA_ANTICIPO = "Factura de anticipo:";
  const advertenciasAmbar = (prev?.advertencias || []).filter(a => !a.startsWith(NOTA_ANTICIPO));
  const notasAnticipo = (prev?.advertencias || []).filter(a => a.startsWith(NOTA_ANTICIPO));
  const cajaRoja: React.CSSProperties = { padding: "10px 14px", borderRadius: 10, border: "1px solid rgba(239,68,68,0.35)", background: "rgba(239,68,68,0.10)" };
  const cajaAmbar: React.CSSProperties = { padding: "10px 14px", borderRadius: 10, border: "1px solid rgba(245,158,11,0.35)", background: "rgba(245,158,11,0.10)" };
  const cajaVerde: React.CSSProperties = { padding: "10px 14px", borderRadius: 10, border: "1px solid rgba(16,185,129,0.35)", background: "rgba(16,185,129,0.10)" };
  const rotulo: React.CSSProperties = { fontSize: 10, color: s.muted, textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 3 };
  const TH: React.CSSProperties = { padding: "6px 10px", fontWeight: 600, fontSize: 10, color: s.muted, textTransform: "uppercase", letterSpacing: 0.5 };

  return (
    <>
      {fase === "cargando" && (
        <div style={{ padding: "36px 0", textAlign: "center", color: s.muted, fontSize: 13 }}>
          <Loader2 size={24} className="animate-spin" style={{ margin: "0 auto 8px", display: "block" }} /> Verificando con Wasabil…
        </div>
      )}

      {(fase === "emitiendo" || fase === "sondeo") && (
        <div style={{ padding: "36px 0", textAlign: "center", color: s.muted }}>
          <Loader2 size={30} className="animate-spin" style={{ margin: "0 auto 10px", display: "block", color: "var(--monza-accent)" }} />
          <div style={{ fontWeight: 700, color: s.text, fontSize: 14 }}>
            {fase === "emitiendo" ? "Enviando a Wasabil…" : "Procesando en el SII…"}
          </div>
          <div style={{ fontSize: 11, marginTop: 4 }}>El SII puede tardar de segundos a un par de minutos. No cierres esta ventana.</div>
        </div>
      )}

      {fase === "exito" && dte && (
        <div style={{ padding: "28px 0", textAlign: "center" }}>
          <CheckCircle2 size={38} color="#10B981" style={{ margin: "0 auto 10px", display: "block" }} />
          <div style={{ fontSize: 16, fontWeight: 800, color: s.text }}>Factura emitida — Folio SII {dte.folio}</div>
          <div style={{ fontSize: 11, color: s.muted, marginTop: 6, maxWidth: 420, marginLeft: "auto", marginRight: "auto" }}>
            El folio quedó registrado en la factura y ya aparece en la lista para cobranzas y factoring.
          </div>
          {/* El documento salió bien, pero la PLATA puede no haber quedado donde
              corresponde: al confirmarse el folio el backend aplica el adelanto que la
              emisión había diferido, y si no logra re-encauzarlo hacia esta factura de
              anticipo (la otra factura está cedida a un factor, su cobranza ya está
              conciliada con el banco…), la factura nace POR COBRAR. Sin esta caja el
              dueño leía solo "Factura emitida" y nadie iba nunca a cobrarla.
              El texto va TAL CUAL viene del backend: ya explica qué pasó y qué hacer. */}
          {avisosSii.length > 0 && (
            <div style={{ ...cajaAmbar, textAlign: "left", marginTop: 14 }}>
              <div style={{ fontSize: 12, fontWeight: 700, color: "#B45309", display: "flex", alignItems: "center", gap: 6, marginBottom: 6 }}>
                <AlertTriangle size={14} style={{ flexShrink: 0 }} /> Ojo con el pago de esta factura
              </div>
              {avisosSii.map((a, i) => (
                <p key={i} style={{ fontSize: 12, lineHeight: 1.5, color: s.text, margin: i === 0 ? 0 : "8px 0 0" }}>{a}</p>
              ))}
            </div>
          )}
          <div style={{ display: "flex", gap: 8, justifyContent: "center", marginTop: 12 }}>
            {dte.pdf_url && (
              <button onClick={() => window.open(dte.pdf_url!, "_blank", "noopener,noreferrer")}
                style={{ padding: "8px 16px", background: "var(--monza-accent)", border: "none", borderRadius: 8, color: "white", cursor: "pointer", fontWeight: 700, fontSize: 13, display: "inline-flex", alignItems: "center", gap: 6, fontFamily: "inherit" }}>
                <FileText size={14} /> Ver PDF de la factura
              </button>
            )}
            <button onClick={cerrar} style={{ padding: "8px 18px", border: s.cardBd, borderRadius: 8, background: "transparent", color: s.muted, cursor: "pointer", fontSize: 13, fontFamily: "inherit" }}>Cerrar</button>
          </div>
        </div>
      )}

      {fase === "pendiente" && (
        <div style={{ padding: "28px 0", textAlign: "center" }}>
          <Clock size={30} color="#F59E0B" style={{ margin: "0 auto 8px", display: "block" }} />
          <div style={{ fontWeight: 700, color: s.text, fontSize: 14 }}>Emisión en curso</div>
          <div style={{ fontSize: 11, color: s.muted, marginTop: 6, maxWidth: 420, marginLeft: "auto", marginRight: "auto" }}>
            Hay una emisión en proceso para esta factura (puede ser de otra pestaña u otro usuario).
            Puedes cerrar: en la lista se ve como <b style={{ color: s.text }}>SII en proceso</b> y{" "}
            <b style={{ color: s.text }}>no se emitirá dos veces</b>.
          </div>
          <button onClick={cerrar} style={{ marginTop: 12, padding: "8px 18px", border: s.cardBd, borderRadius: 8, background: "transparent", color: s.muted, cursor: "pointer", fontSize: 13, fontFamily: "inherit" }}>Cerrar</button>
        </div>
      )}

      {fase === "error" && (
        <div style={{ padding: "24px 0", textAlign: "center" }}>
          <AlertTriangle size={30} color="#EF4444" style={{ margin: "0 auto 8px", display: "block" }} />
          <div style={{ fontWeight: 700, color: s.text, fontSize: 14 }}>No se pudo completar</div>
          <div style={{ fontSize: 11, color: s.muted, marginTop: 4, maxWidth: 440, marginLeft: "auto", marginRight: "auto" }}>{error}</div>
          {emisionIntentada && (
            <div style={{ fontSize: 11, color: s.muted, marginTop: 8, maxWidth: 440, marginLeft: "auto", marginRight: "auto" }}>
              Si la factura alcanzó a crearse, aparece en la lista como <b style={{ color: s.text }}>SII en proceso</b> o{" "}
              <b style={{ color: s.text }}>SII fallida</b> y puedes reintentar desde ahí (nunca se emite dos veces).
            </div>
          )}
          <div style={{ display: "flex", gap: 8, justifyContent: "center", marginTop: 12 }}>
            {/* El botón de volver DESAPARECE para siempre una vez disparado el emitir */}
            {onVolver && payload && !emisionIntentada && (
              <button onClick={onVolver} style={{ padding: "8px 18px", border: s.cardBd, borderRadius: 8, background: "transparent", color: s.muted, cursor: "pointer", fontSize: 13, fontFamily: "inherit" }}>← Volver al formulario</button>
            )}
            <button onClick={cerrar} style={{ padding: "8px 18px", border: s.cardBd, borderRadius: 8, background: "transparent", color: s.muted, cursor: "pointer", fontSize: 13, fontFamily: "inherit" }}>Cerrar</button>
          </div>
        </div>
      )}

      {fase === "fallido" && (
        <>
          <div style={cajaRoja}>
            <div style={{ fontSize: 13, fontWeight: 700, color: "#EF4444", display: "flex", alignItems: "center", gap: 6 }}>
              <AlertTriangle size={14} /> {dte?.estado === "error_envio" ? "La emisión no llegó a Wasabil" : "El SII/Wasabil rechazó la factura"}
            </div>
            <div style={{ fontSize: 11, color: s.muted, marginTop: 3 }}>{error || dte?.error || "Sin detalle del motivo"}</div>
          </div>
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
            <button onClick={cerrar} style={{ padding: "8px 18px", border: s.cardBd, borderRadius: 8, background: "transparent", color: s.muted, cursor: "pointer", fontSize: 13, fontFamily: "inherit" }}>Cerrar</button>
            <button onClick={reintentar}
              style={{ padding: "8px 20px", background: "var(--monza-accent)", border: "none", borderRadius: 8, color: "white", cursor: "pointer", fontWeight: 700, fontSize: 13, display: "inline-flex", alignItems: "center", gap: 6, fontFamily: "inherit" }}>
              <Send size={14} /> Reintentar emisión
            </button>
          </div>
        </>
      )}

      {fase === "preview" && prev && (
        <>
          {/* Qué documento se está por emitir. Sin este distintivo, reintentar la
              emisión de un anticipo abría un modal idéntico al de una factura normal. */}
          {prev.es_anticipo && (
            <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              <span style={{ background: "#DCFCE7", color: "#15803D", fontSize: 10, fontWeight: 700, padding: "2px 8px", borderRadius: 999, display: "inline-flex", alignItems: "center", gap: 4 }}>
                <HandCoins size={11} /> Factura de anticipo
              </span>
              <span style={{ fontSize: 11, color: s.muted }}>
                Respalda un adelanto del cliente. No lleva mercadería: al facturar el despacho real se descuenta sola.
              </span>
            </div>
          )}
          {(prev.problemas?.length ?? 0) > 0 && (
            <div style={cajaRoja}>
              <div style={{ fontSize: 11, fontWeight: 700, color: "#EF4444", marginBottom: 4 }}>Para emitir falta resolver:</div>
              <ul style={{ margin: 0, paddingLeft: 18, fontSize: 11, color: s.muted }}>
                {prev.problemas!.map((p, i) => <li key={i}>{p}</li>)}
              </ul>
            </div>
          )}
          {advertenciasAmbar.length > 0 && (
            <div style={cajaAmbar}>
              <ul style={{ margin: 0, paddingLeft: 18, fontSize: 11, color: s.muted }}>
                {advertenciasAmbar.map((a, i) => <li key={i}>{a}</li>)}
              </ul>
            </div>
          )}
          {notasAnticipo.map((a, i) => (
            <p key={i} style={{ fontSize: 11, color: s.muted, margin: 0 }}>{a} — es lo normal en un anticipo.</p>
          ))}

          {/* Receptor (ficha REAL en Wasabil = lo que verá el SII) + referencias del DTE */}
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, fontSize: 12 }}>
            <div>
              <div style={rotulo}>Receptor {receptor?.fuente === "wasabil" ? "(ficha Wasabil)" : "(datos locales)"}</div>
              <div style={{ fontWeight: 700, color: s.text }}>{receptor?.razon_social || "—"}</div>
              <div style={{ fontSize: 11, color: s.muted }}>RUT {receptor?.rut || "—"}</div>
              {receptor?.giro && <div style={{ fontSize: 11, color: s.muted }}>{receptor.giro}</div>}
              {receptor?.direccion && (
                <div style={{ fontSize: 11, color: s.muted }}>{receptor.direccion}{receptor.comuna ? `, ${receptor.comuna}` : ""}</div>
              )}
              <div style={{ fontSize: 10, color: s.muted, marginTop: 4 }}>
                Si falta un dato, corrígelo en la ficha del cliente (o en app.wasabil.com) y vuelve a abrir el modal.
              </div>
            </div>
            <div>
              <div style={rotulo}>Referencias del DTE</div>
              {(prev.referencias?.length ?? 0) === 0 && <div style={{ fontSize: 11, color: s.muted }}>—</div>}
              {prev.referencias?.map((r, i) => (
                <div key={i} style={{ marginBottom: 4 }}>
                  <div style={{ fontWeight: 700, color: s.text }}>
                    {REF_SII_LABEL[String(r.tipo)] || `Tipo ${r.tipo}`}: {r.folio || "—"}
                  </div>
                  {r.fecha && <div style={{ fontSize: 11, color: s.muted }}>Fecha: {fmtDate(r.fecha)}</div>}
                </div>
              ))}
              {/* Retiro en oficina (modo exclusivo de Monza: sin guía no hay referencia
                  52 y la factura ampara el traslado) NO se pinta aquí: el backend ya lo
                  manda como advertencia y se mostraba DOS veces en el mismo preview. */}
            </div>
          </div>

          {/* Descuento de anticipo: esta factura rebaja facturas de anticipo previas
              (línea negativa + referencia 33 al folio). Se pinta para que el usuario
              vea POR QUÉ el total es menor que la mercadería despachada. */}
          {(prev.descuentos?.length ?? 0) > 0 && (
            <div style={cajaVerde}>
              <div style={{ fontSize: 11, fontWeight: 700, color: "#15803D", marginBottom: 4 }}>Anticipo descontado en esta factura</div>
              {prev.descuentos!.map((d, i) => (
                <div key={i} style={{ fontSize: 11, color: s.muted }}>
                  Factura de anticipo {d.folio || `#${d.anticipo_factura_id}`}:{" "}
                  <b style={{ color: "#15803D" }}>−{fmtClp(d.monto_neto)}</b> neto
                </div>
              ))}
              <div style={{ fontSize: 10, color: s.muted, marginTop: 4 }}>
                El cliente ya pagó esa parte: por eso se rebaja del neto y el IVA se recalcula sobre el neto ya descontado.
              </div>
            </div>
          )}

          {/* Líneas congeladas + totales con la tasa de IVA REAL de la venta */}
          {(prev.lineas?.length ?? 0) > 0 && (
            <div style={{ border: s.cardBd, borderRadius: 10, overflow: "hidden" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
                <thead>
                  <tr style={{ background: s.sub }}>
                    <th style={{ ...TH, textAlign: "left" }}>Ítem</th>
                    <th style={{ ...TH, textAlign: "right" }}>Cant.</th>
                    <th style={{ ...TH, textAlign: "right" }}>P. unit. neto</th>
                    <th style={{ ...TH, textAlign: "right" }}>Total neto</th>
                  </tr>
                </thead>
                <tbody>
                  {prev.lineas!.map((ln, i) => (
                    <tr key={i} style={{ borderTop: s.cardBd }}>
                      <td style={{ padding: "6px 10px" }}>
                        <span style={{ fontWeight: 700, color: s.text, fontFamily: "monospace" }}>{ln.numero_parte || "—"}</span>
                        {ln.descripcion && <span style={{ display: "block", fontSize: 10, color: s.muted }}>{ln.descripcion}</span>}
                      </td>
                      <td style={{ padding: "6px 10px", textAlign: "right", color: s.text }}>{ln.cantidad}</td>
                      <td style={{ padding: "6px 10px", textAlign: "right", color: s.muted }}>{fmtClp(ln.precio_unit_neto)}</td>
                      <td style={{ padding: "6px 10px", textAlign: "right", fontWeight: 700, color: s.text }}>{fmtClp(ln.total_neto)}</td>
                    </tr>
                  ))}
                </tbody>
                <tfoot style={{ background: s.sub }}>
                  <tr style={{ borderTop: s.cardBd }}>
                    <td colSpan={3} style={{ ...TH, textAlign: "right" }}>Neto</td>
                    <td style={{ padding: "6px 10px", textAlign: "right", fontWeight: 700, color: s.text }}>{fmtClp(prev.totales?.neto ?? 0)}</td>
                  </tr>
                  <tr>
                    <td colSpan={3} style={{ ...TH, textAlign: "right" }}>{ivaLabel}</td>
                    <td style={{ padding: "6px 10px", textAlign: "right", color: s.muted }}>{fmtClp(prev.totales?.iva ?? 0)}</td>
                  </tr>
                  <tr>
                    <td colSpan={3} style={{ ...TH, textAlign: "right" }}>Total</td>
                    <td style={{ padding: "6px 10px", textAlign: "right", fontWeight: 800, color: "var(--monza-accent)" }}>{fmtClp(prev.totales?.bruto ?? 0)}</td>
                  </tr>
                </tfoot>
              </table>
            </div>
          )}

          <p style={{ fontSize: 11, color: s.muted, margin: 0 }}>
            El folio lo asigna el SII al emitir. Esta emisión es un <b style={{ color: s.text }}>documento tributario real</b> —
            revisa el receptor y los montos antes de confirmar.
          </p>

          <div style={{ borderTop: s.cardBd, paddingTop: 12 }}>
            <div style={{ fontSize: 11, color: s.muted, display: "flex", alignItems: "center", gap: 6, marginBottom: 8 }}>
              <AlertTriangle size={13} color="#F59E0B" style={{ flexShrink: 0 }} />
              Al confirmar, la factura se emite al SII a través de Wasabil. Esta acción es IRREVERSIBLE.
            </div>
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
              {onVolver && payload && (
                <button onClick={onVolver} style={{ padding: "8px 14px", border: s.cardBd, borderRadius: 8, background: "transparent", color: s.muted, cursor: "pointer", fontSize: 13, fontFamily: "inherit" }}>← Volver</button>
              )}
              <button onClick={cerrar} style={{ padding: "8px 18px", border: s.cardBd, borderRadius: 8, background: "transparent", color: s.muted, cursor: "pointer", fontSize: 13, fontFamily: "inherit" }}>Cancelar</button>
              <button onClick={emitir} disabled={!prev.puede_emitir}
                style={{ padding: "8px 20px", background: "var(--monza-accent)", border: "none", borderRadius: 8, color: "white", cursor: prev.puede_emitir ? "pointer" : "not-allowed", opacity: prev.puede_emitir ? 1 : 0.5, fontWeight: 700, fontSize: 13, display: "inline-flex", alignItems: "center", gap: 6, fontFamily: "inherit" }}>
                <Send size={14} /> Confirmar y emitir al SII
              </button>
            </div>
          </div>
        </>
      )}
    </>
  );
}

// ─── Modal: emitir factura (desde un despacho/guía de una cotización) ──────────
// Fila del selector de ventas. `cond_pago` es la condición PACTADA en la venta
// (cotizacion.forma_pago): el backend siempre la publicó y la pantalla la descartaba.
interface VentaOpcion {
  cotizacion_id: number; numero_cotizacion: string; cliente: string; cond_pago: string | null;
}
// Despacho facturable. `guia_firmada_archivo` es el RESPALDO (foto/PDF) de la guía
// firmada por el cliente; el backend ya lo sirve en /despachos-facturables.
interface DespachoFacturable {
  id: number; numero_despacho: string; numero_guia: string | null;
  /** Fecha de EMISIÓN de la guía ante el SII: sin ella la vía electrónica se bloquea. */
  fecha_guia?: string | null;
  /** Sin firma NO se factura (regla 2026-08-06): el selector deshabilita la opción. */
  guia_firmada: boolean; fecha_firma?: string | null;
  guia_firmada_archivo: string | null; items_count: number;
}

function CrearFacturaModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const s = useStyles();
  const inp = useInput();
  const [ventas, setVentas] = useState<VentaOpcion[]>([]);
  const [cotId, setCotId] = useState<number | "">("");
  const [despachos, setDespachos] = useState<DespachoFacturable[]>([]);
  const [despachoId, setDespachoId] = useState<number | "">("");
  const [sinGuia, setSinGuia] = useState(false);  // retiro en oficina (sin guía de despacho)
  const [folio, setFolio] = useState("");
  const [tipo, setTipo] = useState("factura");
  const [fecha, setFecha] = useState(hoyLocal());
  const [plazo, setPlazo] = useState("30");
  const [obs, setObs] = useState("");
  const [saving, setSaving] = useState(false);
  // Modo SII por DEFECTO: la vía normal es emitir el DTE 33; el registro manual
  // (folio ya emitido a mano) queda como respaldo a un link de distancia — nunca
  // se elimina (Wasabil caído, boletas, documentos emitidos por fuera).
  const [modoSii, setModoSii] = useState(true);
  const [siiPayload, setSiiPayload] = useState<MonzaFacturaPayload | null>(null);
  const [siiBusy, setSiiBusy] = useState(false);
  // A6 · PREVIEW de la vía manual (POST /contabilidad/facturas/preview): las líneas y
  // los montos los deriva el BACKEND (descuento de anticipo incluido), así que sin esto
  // se apretaba "Emitir"/"Registrar" sin haber visto qué se iba a facturar. No persiste
  // nada y sale de las MISMAS funciones que valida el POST.
  const [preview, setPreview] = useState<MonzaFacturaPreview | null>(null);
  const [loadingPrev, setLoadingPrev] = useState(false);
  const [prevError, setPrevError] = useState("");
  const [reintentoPrev, setReintentoPrev] = useState(0);  // re-consulta el preview tras un error

  useEffect(() => {
    monzaContabilidadAPI.listVentas().then(({ data }) =>
      setVentas(((data as VentaOpcion[] | null) ?? []).map(v => ({
        cotizacion_id: v.cotizacion_id, numero_cotizacion: v.numero_cotizacion,
        cliente: v.cliente, cond_pago: v.cond_pago ?? null,
      })))
    ).catch(() => {});
  }, []);

  // Condición de pago PACTADA en la venta elegida (la fuente del plazo, ver C3).
  const condPagoVenta = ventas.find(v => v.cotizacion_id === Number(cotId))?.cond_pago || "";

  useEffect(() => {
    if (!cotId) { setDespachos([]); return; }
    let vivo = true;  // ignora la respuesta atrasada de la venta anterior
    monzaContabilidadAPI.despachosFacturables(Number(cotId))
      .then(({ data }) => { if (vivo) setDespachos((data as DespachoFacturable[] | null) ?? []); })
      .catch(() => { if (vivo) setDespachos([]); });
    // C3 · PRECARGA el plazo desde la condición pactada ("Contado", "30 días contra
    // factura"…). Antes el estado nacía en "30" y nadie lo tocaba nunca.
    setPlazo(plazoDeCondPago(condPagoVenta));
    return () => { vivo = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cotId]);

  // Preview: depende SOLO de lo que cambia líneas y receptor — la venta, el modo
  // (guía elegida o retiro en oficina) y el TIPO de documento (una boleta no puede
  // descontar un anticipo: el backend lo devuelve como problema). La fecha, el plazo y
  // las observaciones NO viajan, para no re-consultar en cada tecla.
  useEffect(() => {
    if (!cotId || (!sinGuia && !despachoId)) { setPreview(null); setPrevError(""); return; }
    let vivo = true;
    setLoadingPrev(true); setPrevError("");
    monzaContabilidadAPI.previewFactura({
      cotizacion_id: Number(cotId),
      ...(sinGuia ? { sin_guia: true } : { despacho_id: Number(despachoId) }),
      tipo_doc: tipo,
    })
      .then(({ data }) => { if (vivo) setPreview(data); })
      .catch((e: unknown) => {
        // Nunca en silencio: el botón se gobierna con el preview, así que si falla hay
        // que decir por qué (antes el usuario habría visto un botón gris sin motivo).
        if (!vivo) return;
        setPreview(null);
        setPrevError(errMsg(e, "No se pudo calcular la previsualización"));
      })
      .finally(() => { if (vivo) setLoadingPrev(false); });
    return () => { vivo = false; };
  }, [cotId, despachoId, sinGuia, tipo, reintentoPrev]);

  // El FOLIO no entra en `puede_emitir` (eso habla de los DATOS de la factura): se
  // exige aparte y solo en la vía manual, donde el operador ya tiene el DTE en la mano.
  const folioFaltante = tipo === "factura" && !modoSii && !folio.trim();
  const puedeEmitir = !!preview && preview.puede_emitir
    && (preview.lineas?.length ?? 0) > 0 && !folioFaltante && !loadingPrev;
  const ivaLabelPreview = etiquetaIva(preview?.totales?.iva_rate);

  // Payload COMÚN a los dos modos y SIN folio: el modo SII lo persiste en NULL (el
  // folio lo asigna el SII) y el manual lo agrega recién al llamar a crearFactura.
  const armarPayload = (): MonzaFacturaPayload => ({
    cotizacion_id: Number(cotId),
    // Retiro en oficina → sin_guia (factura el saldo de la venta sin despacho).
    ...(sinGuia ? { sin_guia: true } : { despacho_id: Number(despachoId) }),
    tipo_doc: tipo,
    fecha_emision: fecha,
    plazo_dias: plazo === "" ? undefined : Number(plazo),
    // C3 · La condición REAL pactada en la venta; si la venta no la trae, se deriva del
    // plazo (0 días = Contado). Antes se inventaba siempre "30 días".
    condicion_pago: condPagoVenta
      || (plazo === "" ? undefined : (Number(plazo) === 0 ? "Contado" : `${plazo} días`)),
    observaciones: obs || undefined,
  });

  const submit = async () => {
    if (!cotId) { toast.error("Selecciona la venta"); return; }
    if (!sinGuia && !despachoId) { toast.error("Selecciona el despacho (o marca 'Retiro en oficina')"); return; }
    if (modoSii) {
      // El DTE 33 es SOLO factura; una boleta se registra por la vía manual.
      if (tipo !== "factura") { toast.error("La emisión electrónica es solo para facturas (DTE 33); una boleta regístrala como ya emitida"); return; }
      // El formulario CEDE el modal al flujo SII; no se llama a ninguna API todavía.
      setSiiPayload(armarPayload());
      return;
    }
    if (tipo === "factura" && !folio.trim()) { toast.error("Ingresa el folio SII de la factura"); return; }
    setSaving(true);
    try {
      const { data } = await monzaContabilidadAPI.crearFactura({ ...armarPayload(), numero_factura: folio.trim() || undefined });
      toast.success("Factura registrada");
      avisarAdvertencias(data?.advertencias);
      onDone(); onClose();
    } catch (e: unknown) { toast.error(errMsg(e, "No se pudo registrar la factura")); } finally { setSaving(false); }
  };

  // El flujo SII toma el modal completo (más ancho): preview → emitir → sondeo.
  // Mientras el envío está en vuelo el modal no se puede cerrar (ni con clic al fondo).
  if (siiPayload) return (
    <Modal title="Emitir factura electrónica (SII)" onClose={siiBusy ? () => {} : onClose} wide>
      <EmisionFacturaSIIModal
        payload={siiPayload}
        onDone={onDone}
        onVolver={() => setSiiPayload(null)}
        onCerrar={onClose}
        onBusy={setSiiBusy}
      />
    </Modal>
  );

  // `wide` (620 px): la previsualización trae una TABLA de líneas + totales, y en los
  // 440 px del formulario se corta — el mismo motivo por el que el flujo SII ya lo usa.
  return (
    <Modal title={modoSii ? "Emitir factura al SII" : "Registrar factura ya emitida"} onClose={onClose} wide>
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
            {/* Con TODAS las guías sin firmar, "Selecciona despacho…" mentía (no hay
                nada seleccionable): el placeholder lo dice y el aviso ámbar de abajo
                explica cómo destrabarlo. */}
            <option value="">{cotId ? (despachos.length ? (despachos.every(d => !d.guia_firmada) ? "Todas las guías están SIN FIRMAR (se marca en Despachos)" : "Selecciona despacho…") : "Sin despachos por facturar") : "Elige una venta primero"}</option>
            {/* «sin fecha» avisa acá que esa guía en papel no se va a poder emitir al SII:
                la referencia 52 exige la fecha de emisión. Se carga en Despachos → Editar.
                «SIN FIRMAR» va DESHABILITADA (regla 2026-08-06): el backend igual la
                rechaza — el disabled evita elegir algo que va a fallar y dice por qué. */}
            {despachos.map(d => <option key={d.id} value={d.id} disabled={!d.guia_firmada}>{d.numero_despacho}{d.numero_guia ? ` · Guía ${d.numero_guia}${d.fecha_guia ? "" : " (sin fecha)"}` : ""} ({d.items_count} ítems){d.guia_firmada ? ` · firmada${d.fecha_firma ? ` ${d.fecha_firma.slice(0, 10).split("-").reverse().join("-")}` : ""}` : " · SIN FIRMAR — márcala en Despachos"}</option>)}
          </select>
          {/* Guías bloqueadas por falta de firma: se dice ACÁ, no solo dentro del
              <option> deshabilitado (que en gris chico nadie lee). */}
          {despachos.some(d => !d.guia_firmada) && (
            <div style={{ marginTop: 6, fontSize: 11, color: "#B45309" }}>
              ⚠ {despachos.filter(d => !d.guia_firmada).length === 1 ? "Hay 1 guía que no se puede facturar" : `Hay ${despachos.filter(d => !d.guia_firmada).length} guías que no se pueden facturar`} porque
              el cliente aún no la{despachos.filter(d => !d.guia_firmada).length === 1 ? "" : "s"} firma: súbela{despachos.filter(d => !d.guia_firmada).length === 1 ? "" : "s"} en
              {" "}<b>Despachos → Marcar guía firmada</b> (foto/PDF + fecha de la firma).
            </div>
          )}
          {/* A13 · PRUEBA DE RECEPCIÓN de la entrega que se está por facturar. Con el
              gate 2026-08-06 una guía sin firmar ya NO se puede elegir; este bloque
              muestra el respaldo (foto/PDF) de la elegida. Molde: FacturasPage.tsx (GA). */}
          {despachoId !== "" && (() => {
            const d = despachos.find(x => x.id === Number(despachoId));
            if (!d) return null;
            return (
              <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "4px 12px", marginTop: 6, fontSize: 11, color: s.muted }}>
                {d.guia_firmada_archivo ? (
                  <button type="button"
                    onClick={() => { monzaDespachosAPI.abrirGuiaFirmada(d.guia_firmada_archivo!).catch(() => toast.error("No se pudo abrir la guía firmada")); }}
                    style={{ display: "inline-flex", alignItems: "center", gap: 4, padding: "3px 10px", borderRadius: 999, border: "1px solid rgba(16,185,129,0.45)", background: "rgba(16,185,129,0.12)", color: "#15803D", fontSize: 11, fontWeight: 600, cursor: "pointer", fontFamily: "inherit" }}>
                    <FileText size={11} /> Ver guía firmada{d.fecha_firma ? ` (${d.fecha_firma.slice(0, 10).split("-").reverse().join("-")})` : ""}
                  </button>
                ) : d.guia_firmada ? (
                  <span style={{ color: "#B45309" }}>⚠ Marcada como firmada, pero <b>sin foto de respaldo adjunta</b> (firmas antiguas; re-fírmala en Despachos para adjuntarla)</span>
                ) : (
                  <span style={{ color: "#B45309" }}>⚠ Esta guía <b>no está firmada</b>: no se puede facturar. Márcala en <b>Despachos → Marcar guía firmada</b>.</span>
                )}
              </div>
            );
          })()}
        </Field>
      ) : (
        <p style={{ fontSize: 12, color: s.muted, margin: 0, background: s.sub, padding: "8px 10px", borderRadius: 8 }}>
          Se facturará el <b>saldo pendiente SIN guía asociada</b> (lo vendido aún no facturado y que no está en
          ningún despacho): lo que salió o va a salir con guía de despacho se factura <b>desde su guía firmada</b>.
        </p>
      )}
      {sinGuia && modoSii && (
        <p style={{ fontSize: 11, color: "#B45309", margin: 0 }}>
          Retiro en oficina: el DTE 33 saldrá <b>sin referencia a guía de despacho</b> (solo la OC del cliente).
        </p>
      )}

      {/* A6 · QUÉ SE VA A FACTURAR (preview del backend, no persiste nada): problemas,
          advertencias, líneas y totales con la tasa de IVA REAL de la venta. El botón se
          gobierna con esto, así que un dato que falta se ve ANTES del clic. */}
      {(cotId !== "" && (sinGuia || despachoId !== "")) && (
        <div style={{ border: s.cardBd, borderRadius: 10, overflow: "hidden" }}>
          {loadingPrev ? (
            <div style={{ padding: 14, textAlign: "center", fontSize: 11, color: s.muted }}>
              <Loader2 size={13} className="animate-spin" style={{ display: "inline", verticalAlign: "-2px", marginRight: 6 }} /> Calculando…
            </div>
          ) : !preview ? (
            <div style={{ padding: 14, textAlign: "center", fontSize: 11, color: prevError ? "#B91C1C" : s.muted }}>
              <div>{prevError || "Sin previsualización"}</div>
              {prevError && (
                <button type="button" onClick={() => setReintentoPrev(n => n + 1)}
                  style={{ marginTop: 8, padding: "4px 12px", borderRadius: 8, border: s.cardBd, background: s.sub, color: s.text, fontSize: 11, fontWeight: 600, cursor: "pointer", fontFamily: "inherit" }}>
                  Reintentar
                </button>
              )}
            </div>
          ) : (
            <>
              {(preview.problemas?.length ?? 0) > 0 && (
                <div style={{ padding: "8px 12px", borderBottom: s.cardBd, background: "rgba(239,68,68,0.10)" }}>
                  <div style={{ fontSize: 11, fontWeight: 700, color: "#B91C1C", marginBottom: 3 }}>Para facturar falta resolver:</div>
                  <ul style={{ margin: 0, paddingLeft: 18, fontSize: 11, color: s.muted }}>
                    {preview.problemas.map((p, i) => <li key={i}>{p}</li>)}
                  </ul>
                </div>
              )}
              {(preview.advertencias?.length ?? 0) > 0 && (
                <div style={{ padding: "8px 12px", borderBottom: s.cardBd, background: "rgba(245,158,11,0.10)" }}>
                  <ul style={{ margin: 0, paddingLeft: 18, fontSize: 11, color: "#B45309" }}>
                    {preview.advertencias.map((a, i) => <li key={i}>{a}</li>)}
                  </ul>
                </div>
              )}
              <div style={{ padding: "8px 12px", borderBottom: s.cardBd, fontSize: 11, color: s.muted }}>
                Receptor: <b style={{ color: s.text }}>{preview.receptor?.razon_social || "—"}</b>
                {" · "}RUT <b style={{ color: s.text }}>{preview.receptor?.rut || "—"}</b>
              </div>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11 }}>
                <thead>
                  <tr style={{ background: s.sub }}>
                    <th style={{ ...THPrev, color: s.muted, textAlign: "left" }}>Ítem</th>
                    <th style={{ ...THPrev, color: s.muted, textAlign: "right" }}>Cant.</th>
                    <th style={{ ...THPrev, color: s.muted, textAlign: "right" }}>P. neto</th>
                    <th style={{ ...THPrev, color: s.muted, textAlign: "right" }}>Total</th>
                  </tr>
                </thead>
                <tbody>
                  {preview.lineas.map((ln, i) => {
                    // Línea NEGATIVA de descuento por anticipo: mismo trato que en la
                    // fila de la factura (↩ + violeta) para que no se lea como un error.
                    const esDescuento = ln.anticipo_factura_id != null || ln.total_neto < 0;
                    const col = esDescuento ? (s.dark ? "#A78BFA" : "#6D28D9") : s.text;
                    return (
                      <tr key={i} style={{ borderTop: s.cardBd }}>
                        <td style={{ padding: "5px 10px" }}>
                          <span style={{ fontWeight: 700, color: col, fontFamily: "monospace" }}>
                            {esDescuento ? "↩ " : ""}{ln.numero_parte || "—"}
                          </span>
                          {ln.descripcion && <span style={{ display: "block", fontSize: 10, color: esDescuento ? col : s.muted }}>{ln.descripcion}</span>}
                        </td>
                        <td style={{ padding: "5px 10px", textAlign: "right", color: s.text }}>{ln.cantidad}</td>
                        <td style={{ padding: "5px 10px", textAlign: "right", color: s.muted }}>{fmtClp(ln.precio_unit_neto)}</td>
                        <td style={{ padding: "5px 10px", textAlign: "right", fontWeight: 700, color: col }}>{fmtClp(ln.total_neto)}</td>
                      </tr>
                    );
                  })}
                  {preview.lineas.length === 0 && (
                    <tr style={{ borderTop: s.cardBd }}>
                      <td colSpan={4} style={{ padding: "10px", textAlign: "center", color: s.muted }}>Sin líneas por facturar en este modo</td>
                    </tr>
                  )}
                </tbody>
                <tfoot style={{ background: s.sub }}>
                  <tr style={{ borderTop: s.cardBd }}>
                    <td colSpan={3} style={{ ...THPrev, color: s.muted, textAlign: "right" }}>Neto</td>
                    <td style={{ padding: "5px 10px", textAlign: "right", fontWeight: 700, color: s.text }}>{fmtClp(preview.totales.neto)}</td>
                  </tr>
                  <tr>
                    <td colSpan={3} style={{ ...THPrev, color: s.muted, textAlign: "right" }}>{ivaLabelPreview}</td>
                    <td style={{ padding: "5px 10px", textAlign: "right", color: s.muted }}>{fmtClp(preview.totales.iva)}</td>
                  </tr>
                  <tr>
                    <td colSpan={3} style={{ ...THPrev, color: s.muted, textAlign: "right" }}>Total</td>
                    <td style={{ padding: "5px 10px", textAlign: "right", fontWeight: 800, color: "var(--monza-accent)" }}>{fmtClp(preview.totales.bruto)}</td>
                  </tr>
                </tfoot>
              </table>
            </>
          )}
        </div>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        {/* En modo SII el folio NO se digita: lo asigna el SII al emitir (el backend
            rechaza el payload si viene con folio). */}
        {modoSii ? (
          <Field label="N° Factura (folio SII)">
            <div style={{ ...inp, color: s.muted, fontSize: 13 }}>Lo asigna el SII al emitir</div>
          </Field>
        ) : (
          <Field label={`N° Factura (folio SII)${tipo === "factura" ? " *" : ""}`}>
            <input style={inp} value={folio} onChange={e => setFolio(e.target.value)} placeholder="Ej. 35" />
          </Field>
        )}
        <Field label="Tipo">
          {/* Elegir boleta cae automáticamente al registro manual: el DTE 33 es solo
              factura, y así el usuario no lee "Emitir al SII" para chocar con un 409. */}
          {/* Volver a "factura" REPONE el modo SII (que es el default): quedarse en
              manual en silencio hacía que el usuario tecleara un folio sin querer. */}
          <select style={inp} value={tipo} onChange={e => { setTipo(e.target.value); setModoSii(e.target.value === "factura"); }}>
            <option value="factura">Factura</option><option value="boleta">Boleta</option>
          </select>
        </Field>
        <Field label="Fecha emisión"><input type="date" style={inp} value={fecha} onChange={e => setFecha(e.target.value)} /></Field>
        <Field label="Plazo (días)"><input type="number" style={inp} value={plazo} onChange={e => setPlazo(e.target.value)} /></Field>
      </div>
      <Field label="Observaciones (opcional)">
        <input style={inp} value={obs} onChange={e => setObs(e.target.value)}
          placeholder={sinGuia ? "Ej. retira Juan Pérez (si lo dejas vacío: \"Retiro en oficina\")" : "Notas de la factura"} />
      </Field>
      {/* A6 · El botón lo gobierna el PREVIEW (`puede_emitir`): antes era disabled={saving}
          a secas y se podía disparar un DTE real —o registrar un folio ya consumido—
          sobre datos que el backend iba a rechazar. */}
      <button onClick={submit} disabled={saving || !puedeEmitir}
        style={{ ...btnPrimary(), opacity: (saving || !puedeEmitir) ? 0.5 : 1, cursor: (saving || !puedeEmitir) ? "not-allowed" : "pointer" }}>
        {saving ? <Loader2 className="animate-spin" size={16} /> : modoSii ? <Send size={16} /> : <Receipt size={16} />}
        {modoSii ? "Emitir factura al SII" : "Registrar factura emitida"}
      </button>
      {folioFaltante && (
        <p style={{ fontSize: 11, color: "#B45309", margin: 0, textAlign: "center" }}>
          Ingresa el folio SII de la factura ya emitida para poder registrarla.
        </p>
      )}
      {/* El registro manual NUNCA se elimina: es el respaldo cuando Wasabil está
          caído, cuando es boleta, o cuando el DTE ya se emitió por fuera. */}
      <button type="button" onClick={() => setModoSii(m => !m)}
        style={{ width: "100%", background: "none", border: "none", cursor: "pointer", color: s.muted, fontSize: 11, textDecoration: "underline", fontFamily: "inherit", padding: 0 }}>
        {modoSii ? "¿La factura ya fue emitida a mano en el SII? Regístrala con su folio"
                 : "← Volver a emitir al SII (folio automático)"}
      </button>
    </Modal>
  );
}

// ─── Modal: factura de ANTICIPO (respalda un adelanto; sin guía de despacho) ───
// ÚNICA excepción a la regla "solo se factura una guía firmada": esta factura
// respalda ante el SII un adelanto que el cliente pagó antes de que llegara la
// mercadería. Al facturar después el despacho real, el backend le descuenta este
// anticipo solo (línea negativa que referencia el folio) → el cliente no paga dos
// veces y Σ brutos de la venta sigue cuadrando con el total vendido.
// ADAPTACIÓN MONZA (a propósito, no es un olvido): aquí NO se piden RUT ni razón
// social como en Grupo AM. El schema de Monza no los acepta — el receptor sale de la
// VENTA (el Cierre de Venta ya exige el RUT) y si falta un dato el backend bloquea
// pidiendo completarlo ahí.
interface VentaAnticipo {
  cotizacion_id: number; numero_cotizacion: string; cliente: string;
  total_neto_clp: number; iva_clp: number; total_con_iva_clp: number; facturado_clp: number;
  pct_adelanto: number;
  // monto_aplicado = cuánto de ese adelanto YA está puesto en alguna factura. Si es
  // igual al monto, no queda plata libre para pagar el anticipo que se va a emitir.
  adelanto: { monto: number; monto_aplicado: number } | null;
}
// Factura de anticipo que la venta YA tiene (se lee del detalle al elegirla, ANTES del
// clic: este botón emite un documento tributario real e irreversible).
interface AnticipoPrevio { id: number; numero_factura: string | null; monto_bruto: number }
// Forma mínima de las facturas que devuelve GET /contabilidad/ventas/{id}.
type FacturaDeVenta = { id: number; numero_factura: string | null; monto_bruto: number; es_anticipo?: boolean };

// Tasa de IVA que aplica el BACKEND cuando la venta no trae una válida
// (monza_contabilidad/service.py → iva_rate_de cae a IVA_DEFAULT). Un IVA 0 NO es
// exento: la factura sale igual con esta tasa. Si el modal derivara la tasa solo de los
// totales de la venta, con una venta de IVA 0 prometería "$0 de IVA" y el backend
// registraría el 19% — por la vía manual eso deja un DTE real con el monto equivocado.
const IVA_RATE_FALLBACK = 0.19;
/** Tasa de IVA EFECTIVA de una venta: la MISMA que va a usar el backend al emitir.
 *  `porDefecto` avisa que la venta no traía IVA válido y se cayó al 19%. */
function ivaRateVenta(totalNeto: number, iva: number): { rate: number; porDefecto: boolean } {
  const derivada = totalNeto > 0 ? iva / totalNeto : 0;
  return derivada > 0 ? { rate: derivada, porDefecto: false } : { rate: IVA_RATE_FALLBACK, porDefecto: true };
}

function AnticipoFacturaModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const s = useStyles();
  const inp = useInput();
  const [ventas, setVentas] = useState<VentaAnticipo[]>([]);
  const [cotId, setCotId] = useState<number | "">("");
  const [montoNeto, setMontoNeto] = useState("");
  const [descripcion, setDescripcion] = useState("");
  const [folio, setFolio] = useState("");
  const [fecha, setFecha] = useState(hoyLocal());
  // Un anticipo se paga AL CONTADO: por defecto 0 días. Sin plazo la factura nace sin
  // fecha de vencimiento y entonces jamás pasa a "vencida", nunca sale en el filtro
  // Vencidas y no entra en el KPI "Vencido" — un anticipo impago no avisaba NUNCA.
  const [plazo, setPlazo] = useState("0");
  // Anticipos que la venta elegida YA tiene (aviso preventivo) + la puerta explícita
  // al segundo (el backend responde 409 si no viene marcada).
  const [anticipos, setAnticipos] = useState<AnticipoPrevio[]>([]);
  const [confirmarSegundo, setConfirmarSegundo] = useState(false);
  // Token de la última venta consultada: descarta la respuesta de una selección previa
  // que llegue tarde (si no, el aviso quedaría hablando de OTRA venta).
  const pedidoRef = useRef(0);
  const [saving, setSaving] = useState(false);
  // Modo SII por DEFECTO (igual que el modal de factura normal); el registro manual
  // queda a un link de distancia para cuando el DTE ya se emitió por fuera.
  const [modoSii, setModoSii] = useState(true);
  const [siiPayload, setSiiPayload] = useState<MonzaFacturaPayload | null>(null);
  const [siiBusy, setSiiBusy] = useState(false);
  // A6 · PREVIEW de la vía manual, la MISMA fuente de verdad que valida el POST: acá
  // aparecen el 409 del segundo anticipo, el cupo excedido y una ficha de cliente
  // incompleta ANTES del clic que emite un documento tributario irreversible.
  const [preview, setPreview] = useState<MonzaFacturaPreview | null>(null);
  const [prevError, setPrevError] = useState("");
  // El neto se teclea: el preview se consulta con el valor ASENTADO (~450 ms) para no
  // pedir una previsualización por cada tecla.
  const [netoDebounced, setNetoDebounced] = useState(0);

  useEffect(() => {
    monzaContabilidadAPI.listVentas().then(({ data }) =>
      setVentas((data || []).map((v: any) => ({
        cotizacion_id: v.cotizacion_id, numero_cotizacion: v.numero_cotizacion, cliente: v.cliente,
        total_neto_clp: v.total_neto_clp || 0, iva_clp: v.iva_clp || 0,
        total_con_iva_clp: v.total_con_iva_clp || 0, facturado_clp: v.facturado_clp || 0,
        pct_adelanto: v.pct_adelanto || 0, adelanto: v.adelanto || null,
      })))
    ).catch(() => {});
  }, []);

  const venta = ventas.find(v => v.cotizacion_id === cotId) || null;
  // Tasa de IVA EFECTIVA: la CONGELADA de la venta y, si esa venta viene con IVA 0, la
  // misma por defecto que usa el backend. Jamás un 0% que prometería un total distinto
  // del que se va a emitir (ver ivaRateVenta).
  const { rate: ivaRate, porDefecto: ivaPorDefecto } = venta
    ? ivaRateVenta(venta.total_neto_clp, venta.iva_clp)
    : { rate: 0, porDefecto: false };
  // Adelanto de la venta: el verificado manda; si aún no lo verifican, el informado
  // por Comercial (pct sobre el total) sirve igual como sugerencia.
  const adelantoBruto = venta
    ? Math.round(venta.adelanto?.monto || venta.total_con_iva_clp * (venta.pct_adelanto / 100))
    : 0;
  // Del adelanto verificado, lo que TODAVÍA no está puesto en ninguna factura. Si es 0,
  // la plata ya se aplicó a otra factura y este anticipo NO nace pagado solo (el backend
  // intenta re-rutearla; ver el aviso del modal).
  const adelantoAplicado = venta?.adelanto ? Math.round(venta.adelanto.monto_aplicado) : 0;
  const adelantoSinAplicar = venta?.adelanto
    ? Math.max(0, Math.round(venta.adelanto.monto) - adelantoAplicado)
    : 0;
  // Cupo del backend: un anticipo que exceda lo aún no facturado se rechaza con 409.
  const disponibleBruto = venta ? Math.max(0, Math.round(venta.total_con_iva_clp - venta.facturado_clp)) : 0;
  const neto = Number(montoNeto) || 0;
  const ivaEstimado = Math.round(neto * ivaRate);
  const brutoEstimado = neto + ivaEstimado;
  const excede = !!venta && neto > 0 && brutoEstimado > disponibleBruto;
  const ivaLabel = `IVA ${(ivaRate * 100).toLocaleString("es-CL", { maximumFractionDigits: 1 })}%`;

  // Elegir la venta sugiere el NETO desde el adelanto (editable): el usuario informa
  // el adelanto en BRUTO y la factura se emite en NETO. Y consulta si esa venta YA
  // tiene una factura de anticipo, para avisarlo ANTES del clic que emite.
  const elegirVenta = async (id: number | "") => {
    const token = ++pedidoRef.current;
    setCotId(id); setAnticipos([]); setConfirmarSegundo(false);
    const v = ventas.find(x => x.cotizacion_id === id);
    if (!v) { setMontoNeto(""); return; }
    const { rate } = ivaRateVenta(v.total_neto_clp, v.iva_clp);
    const bruto = Math.round(v.adelanto?.monto || v.total_con_iva_clp * (v.pct_adelanto / 100));
    setMontoNeto(bruto > 0 ? String(Math.round(bruto / (1 + rate))) : "");
    try {
      const { data } = await monzaContabilidadAPI.ventaDetalle(Number(id));
      if (pedidoRef.current !== token) return;   // llegó tarde: ya se eligió otra venta
      setAnticipos(((data as { facturas?: FacturaDeVenta[] })?.facturas || [])
        .filter(f => !!f.es_anticipo)
        .map(f => ({ id: f.id, numero_factura: f.numero_factura, monto_bruto: f.monto_bruto })));
    } catch { /* sin aviso previo: el backend igual bloquea el segundo anticipo con su 409 */ }
  };

  useEffect(() => {
    const t = window.setTimeout(() => setNetoDebounced(Number(montoNeto) || 0), 450);
    return () => window.clearTimeout(t);
  }, [montoNeto]);

  // A6 · Previsualiza con la MISMA fuente de verdad que la emisión. La descripción no
  // viaja (es solo el rótulo de la línea) para no re-consultar en cada tecla.
  useEffect(() => {
    if (!cotId || netoDebounced <= 0) { setPreview(null); setPrevError(""); return; }
    let vivo = true;
    monzaContabilidadAPI.previewFactura({
      cotizacion_id: Number(cotId),
      es_anticipo: true,
      monto_neto_anticipo: netoDebounced,
      tipo_doc: "factura",
      // Sin esto el preview seguiría pintando "esta venta ya tiene factura de anticipo"
      // (y el botón gris) aunque el operador ya marcó la casilla.
      ...(confirmarSegundo ? { confirmar_segundo_anticipo: true } : {}),
    })
      .then(({ data }) => { if (vivo) { setPreview(data); setPrevError(""); } })
      .catch((e: unknown) => {
        // Nunca en silencio: el botón depende del preview, así que un fallo tiene que
        // decir por qué (si no, queda un botón gris sin explicación).
        if (!vivo) return;
        setPreview(null);
        setPrevError(errMsg(e, "No se pudo calcular la previsualización"));
      });
    return () => { vivo = false; };
  }, [cotId, netoDebounced, confirmarSegundo]);

  // Solo vale el preview que corresponde al monto que hay en pantalla: mientras el
  // debounce no alcanza al campo, el anterior está desactualizado y no habilita nada.
  const previewVigente = preview && netoDebounced === neto ? preview : null;
  const ivaLabelPreview = etiquetaIva(preview?.totales?.iva_rate ?? ivaRate);
  const puedeEmitir = !!previewVigente && previewVigente.puede_emitir
    && neto > 0 && !excede && (modoSii || !!folio.trim());

  // Payload COMÚN a los dos modos y SIN folio: el modo SII lo persiste en NULL (lo
  // asigna el SII) y el manual lo agrega recién al llamar a crearFactura.
  const armarPayload = (): MonzaFacturaPayload => ({
    cotizacion_id: Number(cotId),
    es_anticipo: true,
    monto_neto_anticipo: neto,
    descripcion_anticipo: descripcion.trim() || undefined,
    // El anticipo es SIEMPRE un DTE 33: una boleta no puede respaldarlo ante el SII.
    tipo_doc: "factura",
    fecha_emision: fecha,
    // Plazo: 0 = al contado (default). Vacío = sin vencimiento (y sin alarmas).
    plazo_dias: plazo === "" ? undefined : Number(plazo),
    condicion_pago: plazo === "" ? undefined : (Number(plazo) === 0 ? "Contado" : `${plazo} días`),
    // Solo viaja marcado: sin esto el backend bloquea el 2º anticipo de la venta.
    ...(confirmarSegundo ? { confirmar_segundo_anticipo: true } : {}),
  });

  const submit = async () => {
    if (!cotId) { toast.error("Selecciona la venta"); return; }
    if (neto <= 0) { toast.error("Indica el monto NETO del anticipo (mayor a 0)"); return; }
    if (modoSii) {
      // El formulario CEDE el modal al flujo SII; no se llama a ninguna API todavía.
      setSiiPayload(armarPayload());
      return;
    }
    if (!folio.trim()) { toast.error("Ingresa el folio SII de la factura de anticipo"); return; }
    setSaving(true);
    try {
      const { data } = await monzaContabilidadAPI.crearFactura({ ...armarPayload(), numero_factura: folio.trim() });
      toast.success("Factura de anticipo registrada — al facturar el despacho real se descuenta sola");
      // Acá salen los avisos que cambian lo que el usuario cree que pasó (p. ej. que el
      // adelanto NO se pudo mover a esta factura y quedó por cobrar).
      avisarAdvertencias(data?.advertencias);
      onDone(); onClose();
      // El 409 del segundo anticipo llega tal cual desde el backend (nombra el anticipo
      // que ya existe): errMsg lo muestra sin reescribirlo.
    } catch (e: unknown) { toast.error(errMsg(e, "No se pudo registrar la factura de anticipo")); } finally { setSaving(false); }
  };

  // Flujo SII en curso: el formulario cede el modal al componente de emisión.
  if (siiPayload) return (
    <Modal title="Emitir factura de anticipo al SII" onClose={siiBusy ? () => {} : onClose} wide>
      <EmisionFacturaSIIModal
        payload={siiPayload}
        onDone={onDone}
        onVolver={() => setSiiPayload(null)}
        onCerrar={onClose}
        onBusy={setSiiBusy}
      />
    </Modal>
  );

  return (
    <Modal title="Factura de anticipo (sin guía de despacho)" onClose={onClose}>
      <p style={{ fontSize: 12, color: s.muted, margin: 0 }}>
        Respalda ante el SII un <b style={{ color: s.text }}>adelanto del cliente</b> cobrado antes del despacho:
        es la única factura que no nace de una guía firmada.
      </p>
      <p style={{ fontSize: 12, color: s.text, margin: 0, background: s.sub, padding: "8px 10px", borderRadius: 8 }}>
        Al facturar el despacho real con guía firmada, el sistema le descuenta este anticipo automáticamente para no cobrar dos veces.
      </p>
      <Field label="Venta (cotización)">
        <select style={inp} value={cotId} onChange={e => elegirVenta(e.target.value ? Number(e.target.value) : "")}>
          <option value="">Selecciona cotización…</option>
          {ventas.map(v => <option key={v.cotizacion_id} value={v.cotizacion_id}>COT {v.numero_cotizacion} — {v.cliente}</option>)}
        </select>
      </Field>
      {/* Qué va a pasar con la PLATA de esta factura. Mirar solo el monto del adelanto
          prometía "queda pagada" aunque ese adelanto ya estuviera puesto en otra
          factura: hay que leer también monto_aplicado. */}
      {venta && (
        <div style={{ background: s.sub, borderRadius: 8, padding: "8px 10px", fontSize: 12, color: s.muted }}>
          {adelantoBruto > 0 ? (
            !venta.adelanto ? (
              <>Adelanto <b style={{ color: s.text }}>informado</b> por Comercial: {venta.pct_adelanto}% ≈{" "}
                <b style={{ color: s.text }}>{fmtClp(adelantoBruto)}</b> — quedará pagada cuando Tesorería lo apruebe.</>
            ) : adelantoAplicado <= 0 ? (
              <>Adelanto <b style={{ color: s.text }}>verificado</b> de esta venta:{" "}
                <b style={{ color: s.text }}>{fmtClp(venta.adelanto.monto)}</b>, todavía sin aplicar a ninguna factura —
                esta factura de anticipo queda <b style={{ color: "#15803D" }}>pagada</b> apenas se emita.</>
            ) : (
              <>Adelanto <b style={{ color: s.text }}>verificado</b> de esta venta:{" "}
                <b style={{ color: s.text }}>{fmtClp(venta.adelanto.monto)}</b>, del que{" "}
                <b style={{ color: s.text }}>{fmtClp(adelantoAplicado)}</b> ya está aplicado a otras facturas
                {adelantoSinAplicar > 0 ? <> (libre {fmtClp(adelantoSinAplicar)})</> : null}.
                Al emitir, el sistema <b style={{ color: s.text }}>mueve ese pago a esta factura de anticipo</b> y las
                otras vuelven a quedar por cobrar. Si no lo consigue —por ejemplo si la otra factura está en factoring—,
                esta nace <b style={{ color: "#B45309" }}>por cobrar</b>: revisa su estado al terminar.</>
            )
          ) : (
            /* M2 · El texto anterior prometía que el pago "se registra como cobranza
               normal". Es FALSO y el backend cerró esa puerta a propósito (409): si un
               administrativo salda a mano la factura de anticipo con la transferencia del
               cliente, ese MISMO depósito se cuenta DOS VECES — la plata del adelanto cae
               después en otra factura de la venta, que aparece cobrada sin que nadie haya
               pagado. La única puerta es el adelanto que verifica Tesorería. */
            <>Esta venta <b style={{ color: s.text }}>no tiene un adelanto informado</b>. Puedes emitir igual la factura de
              anticipo, pero <b style={{ color: s.text }}>no se paga con una cobranza normal</b>: una factura de anticipo se
              salda SOLO con el adelanto que verifica Tesorería. Informa el % en el Cierre de Venta y registra el depósito
              en Tesorería; si no, esta factura queda <b style={{ color: "#B45309" }}>por cobrar</b>.</>
          )}
          <div style={{ marginTop: 4 }}>
            Total de la venta: <b style={{ color: s.text }}>{fmtClp(venta.total_con_iva_clp)}</b> · aún no facturado:{" "}
            <b style={{ color: s.text }}>{fmtClp(disponibleBruto)}</b>
          </div>
        </div>
      )}
      {/* Aviso ANTES del clic: el botón emite un DTE real e irreversible, y en Monza el
          adelanto es uno por venta (el backend rechaza el segundo con 409). */}
      {venta && anticipos.length > 0 && (
        <div style={{ padding: "8px 10px", borderRadius: 10, border: "1px solid rgba(245,158,11,0.35)", background: "rgba(245,158,11,0.10)", fontSize: 12, color: s.text }}>
          <b>Esta venta ya tiene factura de anticipo:</b>{" "}
          {anticipos.map(a => `N° ${a.numero_factura || `#${a.id}`} (${fmtClp(a.monto_bruto)})`).join(" · ")}
          <div style={{ fontSize: 11, color: s.muted, marginTop: 4 }}>
            En Monza el adelanto es uno por venta. Emitir otro crea un <b style={{ color: s.text }}>segundo documento
            tributario real</b> por la misma plata: si no es lo que quieres, cierra esta ventana.
          </div>
          <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", fontSize: 12, color: s.text, marginTop: 6 }}>
            <input type="checkbox" checked={confirmarSegundo} onChange={e => setConfirmarSegundo(e.target.checked)} style={{ accentColor: "var(--monza-accent)" }} />
            Sí, necesito un segundo anticipo para esta venta
          </label>
        </div>
      )}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12 }}>
        <Field label="Monto NETO del anticipo (CLP)">
          <input type="number" style={inp} value={montoNeto} onChange={e => setMontoNeto(e.target.value)} placeholder="Ej. 50000" />
        </Field>
        <Field label="Fecha emisión"><input type="date" style={inp} value={fecha} onChange={e => setFecha(e.target.value)} /></Field>
        <Field label="Plazo (días)">
          <input type="number" min={0} style={inp} value={plazo} onChange={e => setPlazo(e.target.value)} />
        </Field>
      </div>
      <p style={{ fontSize: 11, color: s.muted, margin: 0 }}>
        Plazo <b style={{ color: s.text }}>0 = al contado</b> (lo normal en un anticipo: el cliente ya pagó). Si lo dejas
        en blanco, la factura queda <b style={{ color: s.text }}>sin fecha de vencimiento</b>: nunca se marcará como
        vencida ni entrará en el KPI "Vencido".
      </p>
      <Field label="Descripción del anticipo (opcional)">
        <input style={inp} value={descripcion} onChange={e => setDescripcion(e.target.value)}
          placeholder={venta ? `Si lo dejas vacío: "Anticipo venta ${venta.numero_cotizacion}"` : "Ej. Anticipo 50% según orden de compra"} />
      </Field>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        {/* En modo SII el folio NO se digita: lo asigna el SII al emitir. */}
        {modoSii ? (
          <Field label="N° Factura (folio SII)">
            <div style={{ ...inp, color: s.muted, fontSize: 13 }}>Lo asigna el SII al emitir</div>
          </Field>
        ) : (
          <Field label="N° Factura (folio SII) *">
            <input style={inp} value={folio} onChange={e => setFolio(e.target.value)} placeholder="Ej. 42" />
          </Field>
        )}
        <Field label="Tipo">
          <div style={{ ...inp, color: s.muted, fontSize: 13 }}>Factura (DTE 33)</div>
        </Field>
      </div>
      {/* Estimación LOCAL con la MISMA tasa que usará el backend (la congelada de la
          venta, o el 19% por defecto si la venta viene con IVA 0). Se muestra mientras
          el preview no ha llegado; en cuanto llega manda el cálculo del backend (abajo),
          para no tener dos totales distintos en pantalla al mismo tiempo. */}
      {venta && neto > 0 && !previewVigente && (
        <div style={{ background: s.sub, borderRadius: 8, padding: "8px 10px", fontSize: 12, color: s.muted, display: "flex", gap: 16, flexWrap: "wrap" }}>
          <span>Neto: <b style={{ color: s.text }}>{fmtClp(neto)}</b></span>
          <span>{ivaLabel}: <b style={{ color: s.text }}>{fmtClp(ivaEstimado)}</b></span>
          <span>Total: <b style={{ color: "var(--monza-accent)" }}>{fmtClp(brutoEstimado)}</b></span>
        </div>
      )}
      {/* A6 · Lo que el BACKEND va a registrar: problemas (folio/receptor/cupo/segundo
          anticipo), advertencias y los montos definitivos. El botón se gobierna con esto. */}
      {prevError && !preview && (
        <div style={{ padding: "8px 10px", borderRadius: 8, border: "1px solid rgba(239,68,68,0.35)", background: "rgba(239,68,68,0.10)", fontSize: 11, color: "#B91C1C" }}>
          {prevError}
        </div>
      )}
      {/* Solo el preview VIGENTE se pinta: uno viejo mostraría los montos del monto
          anterior junto a la estimación local — dos totales distintos a la vez. */}
      {previewVigente && (
        <div style={{ border: s.cardBd, borderRadius: 8, padding: "8px 10px", background: s.sub, display: "flex", flexDirection: "column", gap: 6 }}>
          {previewVigente.problemas.length > 0 && (
            <div>
              <div style={{ fontSize: 11, fontWeight: 700, color: "#B91C1C", marginBottom: 3 }}>Para emitir falta resolver:</div>
              <ul style={{ margin: 0, paddingLeft: 18, fontSize: 11, color: s.muted }}>
                {previewVigente.problemas.map((p, i) => <li key={i}>{p}</li>)}
              </ul>
            </div>
          )}
          {previewVigente.advertencias.length > 0 && (
            <ul style={{ margin: 0, paddingLeft: 18, fontSize: 11, color: "#B45309" }}>
              {previewVigente.advertencias.map((a, i) => <li key={i}>{a}</li>)}
            </ul>
          )}
          <div style={{ display: "flex", gap: 16, flexWrap: "wrap", fontSize: 12, color: s.muted }}>
            <span>Neto: <b style={{ color: s.text }}>{fmtClp(previewVigente.totales.neto)}</b></span>
            <span>{ivaLabelPreview}: <b style={{ color: s.text }}>{fmtClp(previewVigente.totales.iva)}</b></span>
            <span>Total: <b style={{ color: "var(--monza-accent)" }}>{fmtClp(previewVigente.totales.bruto)}</b></span>
            <span style={{ fontSize: 10 }}>Receptor: <b style={{ color: s.text }}>{previewVigente.receptor?.razon_social || "—"}</b>{previewVigente.receptor?.rut ? ` · ${previewVigente.receptor.rut}` : ""}</span>
          </div>
        </div>
      )}
      {/* Venta con IVA 0: el sistema NO emite documentos exentos, así que la factura
          sale con IVA igual. Sin este aviso el modal decía "IVA 0% · Total $10.000" y
          el backend registraba $11.900 — por la vía manual, un DTE real con el monto
          equivocado. */}
      {venta && ivaPorDefecto && (
        <p style={{ fontSize: 11, color: "#B45309", margin: 0 }}>
          Esta venta está cargada con <b>IVA 0</b> y el sistema no emite documentos exentos: la factura de anticipo
          saldrá con <b>IVA {(IVA_RATE_FALLBACK * 100).toLocaleString("es-CL")}%</b> (los montos de arriba ya lo
          incluyen), no en $0 de IVA. Si la venta debía llevar IVA, corrígela en el Cierre de Venta antes de emitir.
        </p>
      )}
      {excede && (
        <p style={{ fontSize: 11, color: "#B45309", margin: 0 }}>
          El anticipo excede lo aún no facturado de la venta (disponible {fmtClp(disponibleBruto)}): bájalo antes de emitir.
        </p>
      )}
      <p style={{ fontSize: 11, color: s.muted, margin: 0 }}>
        El RUT y la razón social salen de la <b style={{ color: s.text }}>venta</b>. Si faltan, complétalos en el Cierre de Venta
        y vuelve a intentar.
      </p>
      {/* A6 · El botón lo gobierna el PREVIEW: emitir un anticipo es IRREVERSIBLE y antes
          se podía disparar con la ficha del cliente incompleta, sobre el cupo de la venta
          o duplicando un anticipo que ya existía (el backend lo rechazaba después). */}
      <button onClick={submit} disabled={saving || !puedeEmitir}
        style={{ ...btnPrimary(), opacity: (saving || !puedeEmitir) ? 0.5 : 1, cursor: (saving || !puedeEmitir) ? "not-allowed" : "pointer" }}>
        {saving ? <Loader2 className="animate-spin" size={16} /> : <HandCoins size={16} />}
        {modoSii ? "Emitir factura de anticipo al SII" : "Registrar factura de anticipo emitida"}
      </button>
      {!modoSii && neto > 0 && !folio.trim() && (
        <p style={{ fontSize: 11, color: "#B45309", margin: 0, textAlign: "center" }}>
          Ingresa el folio SII de la factura de anticipo ya emitida para poder registrarla.
        </p>
      )}
      <button type="button" onClick={() => setModoSii(m => !m)}
        style={{ width: "100%", background: "none", border: "none", cursor: "pointer", color: s.muted, fontSize: 11, textDecoration: "underline", fontFamily: "inherit", padding: 0 }}>
        {modoSii ? "¿La factura de anticipo ya fue emitida a mano en el SII? Regístrala con su folio"
                 : "← Volver a emitir al SII (folio automático)"}
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
    } catch (e: unknown) { toast.error(errMsg(e, "Error al registrar cobranza")); } finally { setSaving(false); }
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
function FactoringModal({ factura, dte, onClose, onDone }: { factura: Factura; dte?: MonzaDteFacturaInfo; onClose: () => void; onDone: () => void }) {
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
    } catch (e: unknown) { toast.error(errMsg(e, "Error en factoring")); } finally { setSaving(false); }
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
      {/* Zona de riesgo: se muestra SÓLO donde el backend abre la puerta, replicando su
          condición completa (`_plata_bloqueada_por_sii`): factura (no boleta), sin folio
          local, Y con un DTE que existe pero NO está emitido. Las tres importan —
          una factura sin folio y SIN DTE es una factura manual a medio registrar: ahí el
          backend responde 409 y ofrecer el botón sería ofrecer lo que no se puede hacer. */}
      {factura.factoring && !factura.numero_factura
        && (factura.tipo_doc || "factura") === "factura"
        && !!dte && dte.estado !== "emitido" && (
        <ZonaRevertirFactoring factura={factura} onDone={() => { onDone(); onClose(); }} />
      )}
    </Modal>
  );
}

// ─── Revertir una cesión al factor que quedó contra un documento inexistente ───
// Es la SALIDA del zombi: sin ella, una factura cedida al factor sin folio del SII no
// se puede liquidar, ni editar a 0, ni eliminar, y su mercadería queda con el cupo
// facturable secuestrado. La operación BORRA la fila de factoring (si sobreviviera, la
// factura seguiría siendo imborrable), así que el motivo es obligatorio y queda escrito
// en las observaciones de la factura.
function ZonaRevertirFactoring({ factura, onDone }: { factura: Factura; onDone: () => void }) {
  const s = useStyles(); const inp = useInput();
  const [abierta, setAbierta] = useState(false);
  const [motivo, setMotivo] = useState("");
  const [saving, setSaving] = useState(false);
  const submit = async () => {
    setSaving(true);
    try {
      await monzaContabilidadAPI.revertirFactoring(factura.id, motivo.trim());
      toast.success("Factoring revertido — queda la traza en la factura");
      onDone();
    } catch (e: unknown) { toast.error(errMsg(e, "No se pudo revertir")); }
    finally { setSaving(false); }
  };
  return (
    <div style={{ marginTop: 16, paddingTop: 14, borderTop: s.cardBd }}>
      {!abierta ? (
        <button onClick={() => setAbierta(true)}
          style={{ ...btnSecondary(s), color: "#B91C1C", borderColor: "#FCA5A5" }}>
          <Undo2 size={14} /> Revertir esta cesión al factor
        </button>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <p style={{ fontSize: 12, color: s.muted, margin: 0, lineHeight: 1.5 }}>
            Esta factura <b style={{ color: s.text }}>no tiene folio del SII</b>, así que la
            operación de factoring quedó contra un documento que no existe: no se puede
            liquidar ni eliminar. Revertirla <b style={{ color: "#B91C1C" }}>borra la
            operación y su abono</b>; la factura queda libre y el hecho se registra en sus
            observaciones. No toca los pagos del cliente ni el documento tributario.
          </p>
          <Field label="Motivo (obligatorio, mínimo 5 caracteres)">
            <input style={inp} value={motivo} autoFocus
              onChange={e => setMotivo(e.target.value)}
              placeholder="Ej. cesión registrada contra una emisión que el SII rechazó" />
          </Field>
          <div style={{ display: "flex", gap: 8 }}>
            <button onClick={() => { setAbierta(false); setMotivo(""); }}
              style={{ ...btnSecondary(s) }}>Cancelar</button>
            <button onClick={submit} disabled={saving || motivo.trim().length < 5}
              style={{
                ...btnPrimary(), background: "#B91C1C",
                opacity: saving || motivo.trim().length < 5 ? 0.5 : 1,
                cursor: motivo.trim().length < 5 ? "not-allowed" : "pointer",
              }}>
              {saving ? <Loader2 className="animate-spin" size={16} /> : <Undo2 size={16} />}
              Revertir
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Fila de factura (expandible) ─────────────────────────────────────────────
function FacturaRow({ f, dte, onChanged, onCobrar, onFactoring, onSii }: {
  f: Factura; dte?: MonzaDteFacturaInfo;
  onChanged: () => void; onCobrar: (f: Factura) => void; onFactoring: (f: Factura) => void; onSii: (f: Factura) => void;
}) {
  const s = useStyles();
  const [open, setOpen] = useState(false);
  const [folioModal, setFolioModal] = useState(false);
  const pago = PAGO[f.estado_pago] ?? { bg: "#F1F5F9", color: "#64748B", label: f.estado_pago };
  // Estado SII de la factura. `puede_reintentar` lo decide el BACKEND: el frontend
  // jamás recalcula la reintentabilidad por su cuenta.
  const siiEmitida = dte?.estado === "emitido";
  const siiFallida = !!dte && !siiEmitida && !!dte.puede_reintentar;
  const siiEnProceso = !!dte && !siiEmitida && !siiFallida;
  // EL CALLEJÓN: el SII aceptó el documento pero su folio nunca llegó al sistema. Es un
  // estado PERMANENTE por sí solo —el sondeo no lo repara, «Reintentar» responde 409 con
  // razón (re-emitir sería un segundo DTE real) y el N° no se puede teclear a mano—, así
  // que necesita su propia salida: registrar el folio leído en app.wasabil.com.
  const siiSinFolio = siiEmitida && !dte?.folio;
  const pct = Math.min(100, f.monto_bruto > 0 ? Math.round((f.monto_pagado / f.monto_bruto) * 100) : 0);
  // Violeta = "plata cobrada por adelantado" (el mismo tono del chip Anticipo de
  // Ventas), aclarado en oscuro para que siga legible.
  const violeta = s.dark ? "#A78BFA" : "#6D28D9";
  const liquidar = async () => {
    try { await monzaContabilidadAPI.liquidarFactoring(f.id); toast.success("Factoring liquidado"); onChanged(); }
    catch (e: unknown) { toast.error(errMsg(e, "Error")); }
  };
  const delCobranza = async (id: number) => {
    if (!confirm("¿Eliminar esta cobranza?")) return;
    try { await monzaContabilidadAPI.eliminarCobranza(f.id, id); toast.success("Cobranza eliminada"); onChanged(); }
    catch (e: unknown) { toast.error(errMsg(e, "Error")); }
  };
  const eliminar = async () => {
    // Guards SII ANTES del confirm: una factura ya emitida ante el SII no se borra
    // (se anula con nota de crédito en Wasabil), y con una emisión en curso el
    // documento puede estar naciendo con folio real justo ahora.
    if (siiEmitida) { toast.error(`Esta factura fue emitida al SII (folio ${dte?.folio || "—"}): anúlala primero en Wasabil (nota de crédito)`); return; }
    if (siiEnProceso) { toast.error("Hay una emisión SII en curso para esta factura: espera a que termine antes de eliminar"); return; }
    // `siiFallida` NO se bloquea acá a propósito (igual que Grupo AM): bajo ese único
    // badge conviven dos casos que el frontend NO puede distinguir — el intento que
    // NUNCA llegó a Wasabil (borrable: si no, la factura zombi secuestra para siempre
    // el cupo facturable de la mercadería) y el AMBIGUO/con uuid (imborrable adrede).
    // Quien los separa es el backend (_bloqueo_dte_factura en monza_contabilidad/router.py),
    // y su 409 explica al usuario exactamente por qué y cuál es la salida.
    if (!confirm("¿Eliminar esta factura? Solo si no tiene pagos ni factoring (revierte las cobranzas primero).")) return;
    try { await monzaContabilidadAPI.eliminarFactura(f.id); toast.success("Factura eliminada"); onChanged(); }
    catch (e: unknown) { toast.error(errMsg(e, "Error")); }
  };
  const td: React.CSSProperties = { padding: "12px 16px", whiteSpace: "nowrap" };
  return (
    <>
      {/* B9 · Fila expandible también por TECLADO: adentro viven acciones que sin esto
          quedaban inalcanzables (PDF SII, reintentar la emisión, cobranza, eliminar). */}
      <tr style={{ cursor: "pointer", borderBottom: s.cardBd }}
        role="button" tabIndex={0} aria-expanded={open}
        onClick={() => setOpen(o => !o)}
        onKeyDown={e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setOpen(o => !o); } }}>
        <td style={{ ...td, fontWeight: 600, color: "var(--monza-accent)" }}>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 4, flexWrap: "wrap" }}>
            {open ? <ChevronUp size={12} /> : <ChevronDown size={12} />}{f.numero_factura || `#${f.id}`}
            {f.es_anticipo && (
              <span title="Factura de anticipo: respalda un adelanto del cliente (sin guía de despacho). Se descuenta sola al facturar el despacho real."
                style={{ background: "#DCFCE7", color: "#15803D", fontSize: 10, fontWeight: 700, padding: "2px 8px", borderRadius: 999, display: "inline-flex", alignItems: "center", gap: 3 }}>
                <HandCoins size={10} /> Anticipo
              </span>
            )}
            {siiEmitida && !siiSinFolio && (
              <span title={`Emitida electrónicamente al SII (folio ${dte?.folio || "—"})`}
                style={{ background: "#DBEAFE", color: "#1D4ED8", fontSize: 10, fontWeight: 700, padding: "2px 8px", borderRadius: 999, display: "inline-flex", alignItems: "center", gap: 3 }}>
                <CheckCircle2 size={10} /> SII
              </span>
            )}
            {siiSinFolio && (
              <span title="El SII aceptó esta factura pero su FOLIO nunca llegó al sistema. Ábrela y usa «Registrar folio del SII» con el número que aparece en app.wasabil.com. No se puede volver a emitir: el documento ya existe."
                style={{ background: "#FEF3C7", color: "#B45309", fontSize: 10, fontWeight: 700, padding: "2px 8px", borderRadius: 999, display: "inline-flex", alignItems: "center", gap: 3 }}>
                <KeyRound size={10} /> SII sin folio
              </span>
            )}
            {siiEnProceso && (
              <span title="Emisión electrónica en curso"
                style={{ background: "#FEF3C7", color: "#B45309", fontSize: 10, fontWeight: 700, padding: "2px 8px", borderRadius: 999, display: "inline-flex", alignItems: "center", gap: 3 }}>
                <Loader2 size={10} className="animate-spin" /> SII en proceso
              </span>
            )}
            {siiFallida && (
              <span title={dte?.error || "La emisión electrónica falló"}
                style={{ background: "#FEE2E2", color: "#B91C1C", fontSize: 10, fontWeight: 700, padding: "2px 8px", borderRadius: 999, display: "inline-flex", alignItems: "center", gap: 3 }}>
                <AlertCircle size={10} /> SII fallida
              </span>
            )}
          </span>
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
          {/* Plazo 0 = al contado: la fecha de vencimiento es la de emisión y leerla como
              "vencimiento" confunde. Sin plazo (null) sigue saliendo "—": esa factura de
              verdad no tiene vencimiento y nunca va a avisar. */}
          {f.plazo_dias === 0 ? "al contado" : fmtDate(f.fecha_vencimiento)}
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
                      {f.items.map(it => {
                        // Línea de DESCUENTO de anticipo (negativa, con anticipo_factura_id):
                        // un "$-50.000" a secas se lee como una corrección o un error. El ↩ y
                        // el violeta dicen "esto devuelve plata YA facturada" y la separan de
                        // la mercadería — mismo trato que en Ventas — Contabilidad.
                        const esDescuento = it.anticipo_factura_id != null || it.total_neto < 0;
                        const col = esDescuento ? violeta : s.text;
                        return (
                        <tr key={it.id} style={{ borderBottom: s.cardBd }}>
                          <td style={{ padding: "6px 8px", fontWeight: 600, color: col }}>
                            {it.anticipo_factura_id != null ? "↩ " : ""}{it.numero_parte}
                          </td>
                          <td style={{ padding: "6px 8px", color: esDescuento ? violeta : s.muted, maxWidth: 160, overflow: "hidden", textOverflow: "ellipsis" }}>{it.descripcion}</td>
                          <td style={{ padding: "6px 8px", textAlign: "right", color: s.muted }}>{it.cantidad} × {fmtClp(it.precio_unit_neto)}</td>
                          <td style={{ padding: "6px 8px", textAlign: "right", fontWeight: 600, color: col }}>{fmtClp(it.total_neto)}</td>
                        </tr>
                        );
                      })}
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
                {/* Acciones de la emisión electrónica (SII) — arriba de todo porque
                    condicionan lo demás (una factura en proceso no se borra). */}
                {siiEmitida && dte?.pdf_url && (
                  <button onClick={(e) => { e.stopPropagation(); window.open(dte.pdf_url!, "_blank", "noopener,noreferrer"); }} style={btnSecondary(s)}>
                    <FileText size={14} /> PDF SII{dte.folio ? ` · folio ${dte.folio}` : ""}
                  </button>
                )}
                {(siiFallida || siiEnProceso) && (
                  <button onClick={(e) => { e.stopPropagation(); onSii(f); }} style={btnSecondary(s)}>
                    {siiFallida ? <><Send size={14} /> Reintentar emisión SII</> : <><Clock size={14} /> Ver emisión SII en curso</>}
                  </button>
                )}
                {siiSinFolio && (
                  <button onClick={(e) => { e.stopPropagation(); setFolioModal(true); }}
                    style={{ ...btnSecondary(s), color: "#B45309", borderColor: "#FCD34D" }}>
                    <KeyRound size={14} /> Registrar folio del SII
                  </button>
                )}
                {/* M4 · !es_anticipo: una factura de ANTICIPO se salda SOLO con el
                    adelanto que verifica Tesorería — el backend responde 409. Ofrecer el
                    botón terminaba en un error, o peor: empuja al administrativo a
                    saldarla a mano con la transferencia del cliente y ese MISMO depósito
                    se cuenta dos veces (el adelanto ligado cae completo en la factura del
                    despacho real). La UI no debe ofrecer lo que el backend rechaza. */}
                {f.saldo > 0 && !f.es_anticipo && <button onClick={(e) => { e.stopPropagation(); onCobrar(f); }} style={btnSecondary(s)}><CreditCard size={14} /> Registrar cobranza</button>}
                {f.saldo > 0 && f.es_anticipo && (
                  <p style={{ fontSize: 11, color: s.muted, margin: 0, lineHeight: 1.4 }}>
                    Esta factura de anticipo se salda con el <b style={{ color: s.text }}>adelanto que verifica Tesorería</b>,
                    no con una cobranza manual.
                  </p>
                )}
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
      {folioModal && (
        <MonzaRegistrarFolioModal
          sustantivo="factura" referencia={f.numero_factura || `#${f.id}`}
          onClose={() => setFolioModal(false)}
          onRegistrar={async (folio, confirmo) => {
            const r = await monzaWasabilAPI.registrarFolioFactura(f.id, folio, confirmo);
            toast.success(`Folio ${folio} registrado en la factura`);
            // El backend aplica acá el adelanto que la emisión había diferido: sus
            // advertencias son plata y no se pueden tragar en silencio.
            (r.data?.advertencias || []).forEach(a => toast(a, { icon: "⚠️", duration: 8000 }));
            onChanged();
          }} />
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
  const [modal, setModal] = useState<{ type: "crear" | "anticipo" | "cobranza" | "factoring" | "sii"; factura?: Factura } | null>(null);
  // Estado de las emisiones electrónicas de las facturas visibles — solo BD y en
  // LOTE (jamás una llamada por factura). El serializador de Contabilidad Monza no
  // inyecta campos dte_*, así que esta es la fuente de los badges SII.
  const [dtes, setDtes] = useState<Record<number, MonzaDteFacturaInfo>>({});
  const [siiBusy, setSiiBusy] = useState(false);
  // M3 · Fallo al consultar los estados SII: se AVISA en pantalla. Antes iba en un
  // try/catch mudo y, sin badges, TODAS las facturas se leían como "no emitidas".
  const [avisoSii, setAvisoSii] = useState("");

  const load = useCallback(async (search?: string, est?: string) => {
    setLoading(true); setError(""); setAvisoSii("");
    try {
      const [fRes, kRes] = await Promise.all([
        monzaContabilidadAPI.listFacturas(est, search),
        monzaContabilidadAPI.kpis(),
      ]);
      const lista: Factura[] = fRes.data.facturas || [];
      setFacturas(lista); setAging(fRes.data.antiguedad); setKpis(kRes.data);
      // M3 · Badges SII por TANDAS de TOPE_BATCH_SII (el backend rechaza más ids por
      // consulta). Antes se recortaba con slice(0, 200): con el histórico creciendo,
      // las facturas de más abajo se quedaban sin sello, sin folio y sin PDF, y en
      // pantalla parecían no emitidas al SII.
      const ids = lista.map(f => f.id);
      if (ids.length === 0) { setDtes({}); return; }
      const tandas: number[][] = [];
      for (let i = 0; i < ids.length; i += TOPE_BATCH_SII) tandas.push(ids.slice(i, i + TOPE_BATCH_SII));
      try {
        const resp = await Promise.all(tandas.map(t => monzaWasabilAPI.estadoBatchFacturas(t)));
        setDtes(resp.reduce<Record<number, MonzaDteFacturaInfo>>(
          (acc, r) => ({ ...acc, ...(r.data || {}) }), {}));
      } catch (e: unknown) {
        // La lista igual se muestra (los guards duros son del backend), pero se dice que
        // los sellos SII pueden faltar: sin este aviso el silencio MIENTE.
        setDtes({});
        setAvisoSii("No se pudo consultar el estado de emisión electrónica: los sellos «SII», "
          + "el folio y el PDF pueden no aparecer en la lista (las facturas y sus montos están "
          + `bien). ${errMsg(e, "Vuelve a intentar con el botón de refrescar.")}`);
      }
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
          {/* Vía B: la ÚNICA factura que no nace de una guía. Secundaria a propósito —
              el camino normal sigue siendo facturar el despacho. */}
          <button onClick={() => setModal({ type: "anticipo" })} style={{ display: "flex", alignItems: "center", gap: 6, padding: "9px 14px", borderRadius: 8, border: s.cardBd, background: s.cardBg, color: s.text, fontWeight: 600, fontSize: 14, cursor: "pointer", fontFamily: "inherit" }}><HandCoins size={16} /> Factura de anticipo</button>
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
      {avisoSii && (
        <div style={{ borderRadius: 10, border: "1px solid rgba(245,158,11,0.45)", background: "rgba(245,158,11,0.12)", color: "#B45309", padding: "10px 14px", fontSize: 13, display: "flex", alignItems: "flex-start", gap: 8 }}>
          <AlertTriangle size={15} style={{ flexShrink: 0, marginTop: 2 }} />
          <span>{avisoSii}</span>
        </div>
      )}
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
                  <FacturaRow key={f.id} f={f} dte={dtes[f.id]} onChanged={reload}
                    onCobrar={(fa) => setModal({ type: "cobranza", factura: fa })}
                    onFactoring={(fa) => setModal({ type: "factoring", factura: fa })}
                    onSii={(fa) => setModal({ type: "sii", factura: fa })} />
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Al cerrar SIEMPRE se recarga: en el flujo SII la factura pudo quedar creada
          (y hasta con folio) aunque el usuario cierre con la X a mitad del sondeo. */}
      {modal?.type === "crear" && <CrearFacturaModal onClose={() => { setModal(null); reload(); }} onDone={reload} />}
      {modal?.type === "anticipo" && <AnticipoFacturaModal onClose={() => { setModal(null); reload(); }} onDone={reload} />}
      {modal?.type === "cobranza" && modal.factura && <CobranzaModal factura={modal.factura} onClose={() => setModal(null)} onDone={reload} />}
      {modal?.type === "factoring" && modal.factura && <FactoringModal factura={modal.factura} dte={dtes[modal.factura.id]} onClose={() => setModal(null)} onDone={reload} />}
      {modal?.type === "sii" && modal.factura && (
        <Modal title={`Emisión SII · factura ${modal.factura.numero_factura || "#" + modal.factura.id}`}
          onClose={siiBusy ? () => {} : () => { setModal(null); setSiiBusy(false); reload(); }} wide>
          <EmisionFacturaSIIModal facturaId={modal.factura.id} onDone={reload}
            onCerrar={() => { setModal(null); setSiiBusy(false); }} onBusy={setSiiBusy} />
        </Modal>
      )}
    </div>
  );
}
