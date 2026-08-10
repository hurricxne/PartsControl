// Página "Libro SII" (libro de compras del SII — MonzaParts, Fases A1+A2).
// ESPEJO FUNCIONAL de LibroSiiPage.tsx (Grupo AM) en el idioma visual Monza (tema
// oscuro/claro con useMonzaTheme + estilos inline, como MonzaFacturasPage). Responde LA
// pregunta del dueño: «¿qué facturas de proveedor existen ante el SII y NO están
// registradas en el ERP?» — y deja clasificar cada documento (ignorar / costo por venta /
// activo fijo / centro de costos). Consume /api/monza/sii-libro (backend/
// monza_wasabil_compras). Cuatro zonas: TABLERO (cubetas + edad del barrido + cuadratura
// mensual + tarjeta de conciliación), CONCILIACIÓN BANCARIA (matcher banco↔libro↔egresos),
// BANDEJA (documentos con filtros y decisión por fila, con salto a Compras y Pagos
// pre-llenado) y REGLAS POR EMISOR (defaults por RUT).
import { useState, useEffect, useCallback, useRef } from "react";
import { useNavigate } from "react-router-dom";
import {
  BookOpenCheck, RefreshCw, Download, Search, AlertTriangle, AlertCircle, Loader2,
  ChevronDown, ChevronUp, ChevronLeft, ChevronRight, CheckCircle2, HelpCircle, Clock,
  Shield, X, Landmark, Play, ArrowLeftRight, Wallet,
} from "lucide-react";
import toast from "react-hot-toast";
import { useMonzaTheme } from "./MonzaLayout";
import { fmtClp, fmtDate } from "../utils/format";
// Cliente COMPARTIDO con auth (baseURL /api): los endpoints Monza viajan con el
// prefijo /monza/sii-libro — mismo backend candado 'automotriz'.
import api from "../services/api";

// ─── API (endpoints de backend/monza_wasabil_compras/router.py) ───────────────
const siiLibroAPI = {
  resumen: () => api.get("/monza/sii-libro/resumen"),
  // El barrido recorre el API de Wasabil completo: puede tardar más que el timeout normal.
  sincronizar: () => api.post("/monza/sii-libro/sincronizar", null, { timeout: 300_000 }),
  documentos: (params: Record<string, unknown>) => api.get("/monza/sii-libro/documentos", { params }),
  decidir: (id: number, data: { accion: string; destino?: string; motivo?: string }) =>
    api.post(`/monza/sii-libro/documentos/${id}/decision`, data),
  reglas: () => api.get("/monza/sii-libro/reglas-rut"),
  upsertRegla: (data: { rut: string; nivel: string; destino_default?: string; motivo?: string }) =>
    api.post("/monza/sii-libro/reglas-rut", data),
  // H4: el CSV sale con el MISMO filtro que la pantalla (si estás mirando los
  // desaparecidos, exportas los desaparecidos — no otra cosa).
  exportCsv: (params: Record<string, unknown>) =>
    api.get("/monza/sii-libro/export.csv", { params, responseType: "blob" }),
  // Datos del documento con los nombres EXACTOS de CompraCreate: pre-llenan el
  // formulario NORMAL de Compras y Pagos Monza (el operador revisa y guarda allá).
  prefillCompra: (id: number) => api.get(`/monza/sii-libro/documentos/${id}/prefill-compra`),
};

// Llave de sessionStorage con la que el prefill viaja a la página de Compras y Pagos
// Monza (MonzaComprasPage la consume UNA vez al montar y la borra).
const PREFILL_COMPRA_SII_KEY = "prefillCompraSiiMonza";

// ─── Tipos (espejo de _serializar y /resumen del router) ──────────────────────
// H3: el backend manda el DESGLOSE de la cuadratura (ignorados, notas de crédito, ERP
// comparable) y la diferencia que SÍ puede dar $0. Las claves nuevas van opcionales para
// que la pantalla no se caiga si habla con un backend anterior al desglose.
interface CuadraturaMes {
  mes: string; libro: number; erp: number; diferencia: number;
  libro_ignorados?: number; libro_nc?: number;
  erp_comparable?: number; erp_fuera_libro?: number; diferencia_explicada?: number;
}
interface Resumen {
  ultimo_barrido: { exito: boolean | null; origen: string | null; terminado_at: string | null; error: string | null; total_api: number | null } | null;
  edad_horas_ultimo_exitoso: number | null;
  edad_critica: boolean;
  documentos_activos: number;
  cubetas: { ESTA: number; NO_ESTA: number; INDETERMINADO: number };
  pendientes_decision: number;
  divergentes: number;
  desaparecidos: number;
  compras_erp_sin_llave: number;
  monto_sin_fecha: number;
  cuadratura_mensual: CuadraturaMes[];
  centros_sugeridos: string[];
}
interface Doc {
  id: number; uuid: string | null; tipo: number; folio: string | null; fecha: string | null;
  rut: string | null; rut_formateado: string | null; emisor: string | null;
  monto_efectivo: number; neto: number; iva: number; exento: number;
  trx_sign: number; es_nota_credito: boolean;
  exchange_status: string | null; estado_espejo: string;
  // 'N/A' = fila de un documento DESAPARECIDO (H4): el SII ya no lo declara, así que el
  // cruce contra el ERP no aplica.
  cubeta: "ESTA" | "NO_ESTA" | "INDETERMINADO" | "N/A";
  // H1 (CRÍTICO): la leyenda del backend que avisa que ya hay una compra del mismo RUT
  // con el folio casi igual ('0004071' vs '4071'). Es lo ÚNICO que frena registrar dos
  // veces la misma factura y que Tesorería la pague dos veces: se pinta en la fila.
  cubeta_detalle: string | null;
  decision: string | null; destino: string | null; decision_motivo: string | null;
  divergente: boolean; divergencia_detalle: string | null;
  regla_rut: string | null; regla_destino_default: string | null;
}
interface Regla { id: number; rut: string; rut_formateado: string; nivel: string; destino_default: string | null; motivo: string | null }

// ─── Conciliación bancaria (matcher banco ↔ libro ↔ egresos; router_match.py) ─
const matchAPI = {
  resumen: () => api.get("/monza/sii-libro/match/resumen"),
  // La corrida recorre cartola + libro + egresos completos: puede exceder el timeout normal.
  correr: () => api.post("/monza/sii-libro/match/correr", null, { timeout: 300_000 }),
  pendientes: (params: Record<string, unknown>) => api.get("/monza/sii-libro/match/pendientes", { params }),
  confirmar: (id: number) => api.post(`/monza/sii-libro/match/${id}/confirmar`),
  descartar: (id: number, motivo?: string) =>
    api.post(`/monza/sii-libro/match/${id}/descartar`, motivo ? { motivo } : {}),
};

// Espejo de _serializar y /match/resumen del router del matcher.
interface MatchDoc {
  id: number; uuid: string | null; folio: string | null; tipo: number; fecha: string | null;
  rut: string | null; rut_formateado: string | null; emisor: string | null;
  monto_efectivo: number; estado_espejo: string;
}
interface MatchMov {
  id: number; fecha: string | null; glosa: string | null; tipo: string | null;
  monto: number; referencia: string | null; conciliado: boolean;
}
// H6: los documentos que compiten por el mismo cargo del banco. El backend los manda
// desde siempre; hasta hoy la pantalla los tiraba a la basura (`unknown[]`) y el
// operador leía «3 documentos compiten» sin poder saber CUÁLES.
interface MatchCandidato {
  doc_id: number; uuid: string | null; folio: string | null;
  rut: string | null; saldo: number | null;
}
interface MatchItem {
  id: number; estado: string; via: string; origen: string; score: number | null;
  grupo_uuid: string | null; monto_asignado: number;
  motivo: string[]; candidatos: MatchCandidato[];
  divergente: boolean; divergencia_detalle: string | null; decidido_at: string | null;
  doc: MatchDoc | null; movimiento: MatchMov | null;
}
interface MatchResumen {
  autos_vivos: number; sugeridos: number; en_duda: number; conflictos: number;
  confirmados: number; descartados: number; no_bancario: number;
  pct_libro_conciliado_por_monto: number;
  etiquetas: Record<string, number>; rut_en_glosa_activo: boolean;
  ultima_corrida: {
    origen: string; exito: boolean | null; terminado_at: string | null; error: string | null;
    sugeridos_nuevos: number | null; autos_nuevos: number | null;
  } | null;
}
// Pestañas de la sección → estado que sirve /match/pendientes. La pestaña "Autos"
// muestra el contador agregado de los automáticos vivos y lista los degradados a
// «en duda» (que sí piden decisión).
// H2 (CRÍTICO): "Conciliados" es la pestaña que faltaba. Tesorería frena el borrado de
// una cartola o de un movimiento diciendo «el match #412 está confirmado: deshágalo en
// el Libro SII», y hasta hoy ese match no aparecía en NINGUNA pantalla — el operador
// quedaba en un callejón sin salida (Tesorería lo mandaba acá y acá no estaba).
type MatchTab = "sugeridos" | "conflictos" | "autos" | "conciliados";
const ESTADO_TAB: Record<MatchTab, string> = {
  sugeridos: "sugerido", conflictos: "conflicto", autos: "en_duda", conciliados: "confirmado",
};

// ─── Etiquetas y colores de dominio (paleta semántica Monza) ──────────────────
const TIPO_DOC: Record<number, string> = {
  33: "Factura", 34: "Factura exenta", 39: "Boleta", 43: "Liquidación factura",
  46: "Factura de compra", 56: "Nota de débito", 61: "Nota de crédito", 110: "Factura export.",
};
// Verde / rojo / ámbar / azul / gris: los mismos hex semánticos del resto de Monza
// (MonzaComprasPage.PAGO_PILL) — los neutros SIEMPRE salen del tema (useStyles).
const VERDE = "#15803D", ROJO = "#B91C1C", AMBAR = "#B45309", AZUL = "#1D4ED8", GRIS = "#64748B";
const CUBETA_PILL: Record<string, { bg: string; color: string; label: string }> = {
  ESTA:          { bg: "rgba(21,128,61,0.14)",  color: VERDE, label: "Está en el ERP" },
  NO_ESTA:       { bg: "rgba(185,28,28,0.12)",  color: ROJO,  label: "No está" },
  INDETERMINADO: { bg: "rgba(217,119,6,0.14)",  color: AMBAR, label: "Indeterminado" },
  // H4: cubeta de los documentos desaparecidos. No hay cruce que calcular — el SII dejó
  // de declararlos, así que la pregunta «¿está en el ERP?» ya no aplica.
  "N/A":         { bg: "rgba(148,163,184,0.15)", color: GRIS,  label: "El SII ya no lo declara" },
};
const NIVEL_PILL: Record<string, { bg: string; color: string }> = {
  BLOQUEADO:    { bg: "rgba(185,28,28,0.12)",   color: ROJO },
  IGNORAR_AUTO: { bg: "rgba(148,163,184,0.15)", color: GRIS },
  LOGISTICO:    { bg: "rgba(37,99,235,0.12)",   color: AZUL },
};
const CC_LABEL: Record<string, string> = {
  "cc:financiero": "Financiero", "cc:distribucion": "Distribución", "cc:bodegaje": "Bodegaje",
  "cc:administracion": "Administración", "cc:honorarios": "Honorarios",
  "cc:intercompania": "Intercompañía", "cc:servicios_venta": "Servicios de venta",
};
/** Etiqueta legible de un destino de clasificación ('cc:financiero' → "Financiero"). */
function destinoLabel(d: string | null | undefined): string {
  if (!d) return "—";
  if (d === "costo_venta") return "Costo por venta";
  if (d === "activo_fijo") return "Activo fijo";
  if (CC_LABEL[d]) return CC_LABEL[d];
  if (d.startsWith("cc:")) {
    const s = d.slice(3).replace(/_/g, " ");
    return s.charAt(0).toUpperCase() + s.slice(1);
  }
  return d;
}
/** Monto con signo tipográfico: las notas de crédito se ven NEGATIVAS (−$4.760.000). */
const fmtMonto = (n: number): string => (n < 0 ? "−" + fmtClp(-n) : fmtClp(n));
/** '765136806' → '76.513.680-6'. El backend manda el RUT canónico (sin puntos ni guión)
 *  en la lista de documentos que compiten (H6): el contador lee RUT con puntos. */
function fmtRutCanon(r: string | null): string {
  if (!r) return "—";
  return r.slice(0, -1).replace(/\B(?=(\d{3})+(?!\d))/g, ".") + "-" + r.slice(-1);
}
/** '2026-07' → 'jul 2026' (es-CL). */
function fmtMes(m: string): string {
  const [y, mo] = m.split("-").map(Number);
  if (!y || !mo) return m;
  return new Date(y, mo - 1, 1).toLocaleDateString("es-CL", { month: "short", year: "numeric" });
}
/** Normaliza el error del backend a texto legible (mismo helper que MonzaFacturasPage:
 *  los 409 del router EXPLICAN qué hacer — jamás taparlos con un mensaje genérico). */
function errMsg(e: unknown, fallback: string): string {
  const d = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (Array.isArray(d)) return d.map((x) => (x as { msg?: string })?.msg || JSON.stringify(x)).join("; ");
  if (typeof d === "string") return d;
  return fallback;
}

// ─── Helpers de estilo según theme (mismo sistema que el resto de MonzaParts) ─
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
  return { width: "100%", padding: "8px 12px", borderRadius: 8, border: s.cardBd, background: s.inputBg, color: s.text, fontSize: 13, fontFamily: "inherit", boxSizing: "border-box" };
}
function btnPrimary(): React.CSSProperties {
  return { display: "flex", alignItems: "center", justifyContent: "center", gap: 8, padding: "9px 14px", borderRadius: 8, border: "none", background: "var(--monza-accent)", color: "white", fontWeight: 600, fontSize: 13, cursor: "pointer", fontFamily: "inherit" };
}
function btnSecondary(s: Styles): React.CSSProperties {
  return { display: "flex", alignItems: "center", justifyContent: "center", gap: 6, padding: "9px 12px", borderRadius: 8, border: s.cardBd, background: s.sub, color: s.text, fontWeight: 600, fontSize: 13, cursor: "pointer", fontFamily: "inherit" };
}
const pill = (bg: string, color: string): React.CSSProperties =>
  ({ background: bg, color, fontSize: 11, fontWeight: 600, padding: "3px 9px", borderRadius: 999, whiteSpace: "nowrap", display: "inline-block" });
const chipMini = (bg: string, color: string): React.CSSProperties =>
  ({ background: bg, color, fontSize: 10, fontWeight: 600, padding: "2px 8px", borderRadius: 999, display: "inline-flex", alignItems: "center", maxWidth: 240 });
function Field({ label, children }: { label: string; children: React.ReactNode }) {
  const s = useStyles();
  return (<label style={{ display: "block" }}><span style={{ display: "block", fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 4, color: s.muted }}>{label}</span>{children}</label>);
}
/** Opciones del selector de destino: los 2 fijos + los cc:* sugeridos por /resumen,
 *  más cualquier destino ya guardado que no esté en el catálogo (es ampliable).
 *
 *  Las etiquetas decían «capitaliza al embarque» y «se deprecia», en presente, como si
 *  clasificar moviera plata en alguna parte. Hoy NO la mueve: el destino se guarda (con
 *  quién y cuándo) y nada del ERP lo lee para costear — el costo del embarque y la
 *  depreciación se siguen calculando donde se calculan siempre. Prometer el efecto en
 *  presente es lo peor de los dos mundos: el contador cree que ya imputó y ni siquiera
 *  revisa el costo a mano. */
function opcionesDestino(centros: string[], extra?: (string | null)[]): { value: string; label: string }[] {
  const vals = ["costo_venta", "activo_fijo", ...centros];
  for (const e of extra || []) if (e && !vals.includes(e)) vals.push(e);
  return vals.map(v => ({
    value: v,
    label: v === "costo_venta" ? "Costo por venta (queda marcado; todavía no se suma al costo del embarque)"
      : v === "activo_fijo" ? "Activo fijo (queda marcado; todavía no calcula depreciación)"
      : destinoLabel(v),
  }));
}
const TH: React.CSSProperties = { padding: "10px 14px", textAlign: "left", fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.5, whiteSpace: "nowrap" };

// ─── Fila de documento (expandible, con las acciones de decisión) ─────────────
function DocRow({ doc, centros, onChanged }: { doc: Doc; centros: string[]; onChanged: () => void }) {
  const s = useStyles();
  const navigate = useNavigate();
  const inp = inputBase(s);
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [prefilling, setPrefilling] = useState(false);
  // Preselección: el destino ya decidido o, si hay regla del emisor, su default sugerido.
  const [destino, setDestino] = useState(doc.destino || doc.regla_destino_default || "");
  const [motivo, setMotivo] = useState("");
  const cub = CUBETA_PILL[doc.cubeta] ?? { bg: "rgba(148,163,184,0.15)", color: GRIS, label: doc.cubeta };
  const td: React.CSSProperties = { padding: "11px 14px", whiteSpace: "nowrap", fontSize: 13 };

  // «Registrar compra»: pide el prefill al backend, lo deja en sessionStorage (no en la
  // URL: ahí no van datos de negocio) y navega al formulario NORMAL de Compras y Pagos
  // Monza. No crea nada — el operador revisa y guarda allá, con todos los guards intactos.
  const registrarCompra = async () => {
    setPrefilling(true);
    try {
      const { data } = await siiLibroAPI.prefillCompra(doc.id);
      sessionStorage.setItem(PREFILL_COMPRA_SII_KEY, JSON.stringify(data));
      navigate("/monzaparts/compras-contab");
    } catch (e: unknown) {
      // El 409 (nota de crédito) explica el porqué: va tal cual. Solo se resetea el
      // estado en el error — en el éxito la fila se desmonta al navegar.
      toast.error(errMsg(e, "No se pudo preparar el registro de la compra"), { duration: 9000 });
      setPrefilling(false);
    }
  };

  const decidir = async (accion: "ignorar" | "clasificar" | "pendiente") => {
    if (accion === "clasificar" && !destino) { toast.error("Elige un destino para clasificar"); return; }
    setSaving(true);
    try {
      await siiLibroAPI.decidir(doc.id, {
        accion,
        destino: accion === "clasificar" ? destino : undefined,
        motivo: motivo.trim() || undefined,
      });
      toast.success(accion === "ignorar" ? "Documento ignorado"
        : accion === "clasificar" ? `Clasificado como ${destinoLabel(destino)}`
        : "Documento devuelto a pendiente");
      setMotivo("");
      onChanged();
    } catch (e: unknown) {
      // Los 409 (BLOQUEADO→costo_venta, NC→costo_venta) explican qué hacer: van TAL CUAL.
      toast.error(errMsg(e, "No se pudo guardar la decisión"), { duration: 9000 });
    } finally { setSaving(false); }
  };

  return (
    <>
      <tr style={{ cursor: "pointer", borderBottom: s.cardBd }} onClick={() => setOpen(o => !o)}
        role="button" tabIndex={0} aria-expanded={open}
        onKeyDown={e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setOpen(o => !o); } }}>
        <td style={{ ...td, color: s.muted }}>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
            {open ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
            {fmtDate(doc.fecha)}
          </span>
        </td>
        <td style={{ ...td, fontSize: 12, fontWeight: 500, color: doc.es_nota_credito ? ROJO : s.muted }}>
          {TIPO_DOC[doc.tipo] || `Doc ${doc.tipo}`} <span style={{ fontFamily: "ui-monospace, monospace", fontSize: 10, color: s.faint }}>({doc.tipo})</span>
        </td>
        <td style={{ ...td, fontFamily: "ui-monospace, monospace", fontWeight: 600, color: "var(--monza-accent)" }}>{doc.folio || "—"}</td>
        <td style={{ ...td, whiteSpace: "normal", maxWidth: 220 }}>
          <p style={{ margin: 0, fontWeight: 600, color: s.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{doc.emisor || "—"}</p>
          <p style={{ margin: 0, fontSize: 11, fontFamily: "ui-monospace, monospace", color: s.faint }}>{doc.rut_formateado || doc.rut || "—"}</p>
        </td>
        <td style={{ ...td, textAlign: "right", fontWeight: 600, color: doc.monto_efectivo < 0 ? ROJO : s.text }}>
          {fmtMonto(doc.monto_efectivo)}
        </td>
        <td style={{ ...td, whiteSpace: "normal" }}>
          <span style={pill(cub.bg, cub.color)}>{cub.label}</span>
          {/* H1 (CRÍTICO): el aviso del folio parecido va EN LA FILA, no en un tooltip.
              Es el único freno entre el operador y registrar por segunda vez una factura
              que ya está en el ERP tecleada distinta ('0004071' vs '4071') — y que
              Tesorería la pague dos veces. Nadie pasa el mouse por 200 filas. */}
          {doc.cubeta === "INDETERMINADO" && doc.cubeta_detalle && (
            <p style={{ margin: "6px 0 0", display: "flex", alignItems: "flex-start", gap: 4, fontSize: 11, lineHeight: 1.35, color: AMBAR, maxWidth: 260 }}>
              <AlertTriangle size={12} style={{ flexShrink: 0, marginTop: 2 }} />
              <span>Ojo: {doc.cubeta_detalle}</span>
            </p>
          )}
        </td>
        <td style={td}>
          {doc.decision === null && <span style={pill("rgba(217,119,6,0.14)", AMBAR)}>Pendiente</span>}
          {doc.decision === "ignorado" && <span style={pill("rgba(148,163,184,0.15)", GRIS)}>Ignorado</span>}
          {doc.decision === "clasificado" && <span style={pill("rgba(21,128,61,0.14)", VERDE)}>{destinoLabel(doc.destino)}</span>}
        </td>
        <td style={{ ...td, textAlign: "center" }}>
          {doc.divergente && (
            <span title={doc.divergencia_detalle || "El documento cambió en el SII después de decidirlo"}>
              <AlertTriangle size={15} color={AMBAR} style={{ display: "inline" }} />
            </span>
          )}
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={8} style={{ padding: "0 16px 16px", background: s.sub }}>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 16, paddingTop: 12 }} onClick={e => e.stopPropagation()}>
              {/* Detalle del documento */}
              <div style={{ flex: "2 1 320px", minWidth: 0, display: "flex", flexDirection: "column", gap: 8 }}>
                <div style={{ display: "flex", flexWrap: "wrap", gap: "4px 20px", fontSize: 12, color: s.muted }}>
                  <span>Neto: <b style={{ color: s.text }}>{fmtMonto(doc.neto)}</b></span>
                  <span>IVA: <b style={{ color: s.text }}>{fmtMonto(doc.iva)}</b></span>
                  {doc.exento !== 0 && <span>Exento: <b style={{ color: s.text }}>{fmtMonto(doc.exento)}</b></span>}
                  <span>Efectivo: <b style={{ color: doc.monto_efectivo < 0 ? ROJO : "var(--monza-accent)" }}>{fmtMonto(doc.monto_efectivo)}</b></span>
                </div>
                {doc.es_nota_credito && (
                  <p style={{ margin: 0, fontSize: 12, color: ROJO }}>
                    Nota de crédito: resta del libro. No se clasifica como costo por venta en esta fase
                    (reduce el gasto del período o espera la fase de vínculo con embarques).
                  </p>
                )}
                {/* H1 (CRÍTICO): el mismo aviso, abierto y con la receta de qué hacer.
                    Mismo formato que el recuadro de divergencia de abajo. */}
                {doc.cubeta === "INDETERMINADO" && doc.cubeta_detalle && (
                  <div style={{ borderRadius: 10, border: "1px solid rgba(245,158,11,0.35)", background: "rgba(245,158,11,0.10)", padding: "10px 12px" }}>
                    <p style={{ margin: 0, fontSize: 12, fontWeight: 700, color: AMBAR, display: "flex", alignItems: "center", gap: 6 }}>
                      <AlertTriangle size={13} style={{ flexShrink: 0 }} /> Ojo: revisa antes de registrarla — puede que esta factura ya esté en el sistema
                    </p>
                    <p style={{ margin: "4px 0 0", fontSize: 12, color: s.muted }}>{doc.cubeta_detalle}</p>
                    <p style={{ margin: "4px 0 0", fontSize: 12, color: s.muted }}>
                      Si es la misma factura, <b style={{ color: s.text }}>no la registre de nuevo</b>: búsquela
                      en Compras y Pagos por el RUT del proveedor y, si el N° quedó mal tecleado, corríjalo allá.
                    </p>
                  </div>
                )}
                {doc.divergente && (
                  <div style={{ borderRadius: 10, border: "1px solid rgba(245,158,11,0.35)", background: "rgba(245,158,11,0.10)", padding: "10px 12px" }}>
                    <p style={{ margin: 0, fontSize: 12, fontWeight: 700, color: AMBAR, display: "flex", alignItems: "center", gap: 6 }}>
                      <AlertTriangle size={13} style={{ flexShrink: 0 }} /> Cambió en el SII después de decidirlo
                    </p>
                    <p style={{ margin: "4px 0 0", fontSize: 12, color: s.muted }}>{doc.divergencia_detalle || "Sin detalle de la divergencia"}</p>
                  </div>
                )}
                {doc.decision && (
                  <p style={{ margin: 0, fontSize: 12, color: s.muted }}>
                    Decisión actual: <b style={{ color: s.text }}>{doc.decision === "ignorado" ? "Ignorado" : `Clasificado → ${destinoLabel(doc.destino)}`}</b>
                    {doc.decision_motivo && <> · Motivo: <i>{doc.decision_motivo}</i></>}
                  </p>
                )}
                {doc.regla_rut && (
                  <p style={{ margin: 0, fontSize: 12, color: s.muted, display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
                    Regla del emisor:{" "}
                    <span style={chipMini((NIVEL_PILL[doc.regla_rut] || { bg: "rgba(148,163,184,0.15)", color: GRIS }).bg, (NIVEL_PILL[doc.regla_rut] || { color: GRIS }).color)}>{doc.regla_rut}</span>
                    {doc.regla_destino_default && <> · default sugerido: <b style={{ color: s.text }}>{destinoLabel(doc.regla_destino_default)}</b></>}
                  </p>
                )}
              </div>
              {/* Acciones de decisión */}
              <div style={{ flex: "1 1 220px", display: "flex", flexDirection: "column", gap: 8 }}>
                {/* Registrar en Compras y Pagos: la salida natural de un NO_ESTA (y de un
                    INDETERMINADO bajo responsabilidad del operador). Oculto para la NC:
                    el backend igual responde 409, esto solo evita el viaje inútil. */}
                {(doc.cubeta === "NO_ESTA" || doc.cubeta === "INDETERMINADO") && !doc.es_nota_credito && (
                  <button onClick={registrarCompra} disabled={prefilling || saving}
                    // H1: si el backend explicó POR QUÉ dudó (el folio parecido), ese es
                    // el motivo real — el texto genérico («compras sin llave») quedaba
                    // mintiendo justo en el caso peligroso.
                    title={doc.cubeta_detalle
                      ? doc.cubeta_detalle
                      : doc.cubeta === "INDETERMINADO"
                      ? "Hay compras del ERP sin llave que PODRÍAN ser este documento: registrarlo igual puede duplicar — revisa antes de guardar"
                      : "Abre el formulario de Compras y Pagos pre-llenado con este documento; revisas y guardas allá (mismos controles de siempre)"}
                    style={{ ...btnPrimary(), width: "100%", fontSize: 12, opacity: prefilling || saving ? 0.5 : 1, cursor: prefilling || saving ? "not-allowed" : "pointer" }}>
                    {prefilling ? <Loader2 size={14} className="animate-spin" /> : <Wallet size={14} />}
                    {doc.cubeta === "INDETERMINADO" ? "Registrar igualmente…" : "Registrar compra"}
                  </button>
                )}
                <Field label="Destino de clasificación">
                  <select style={inp} value={destino} onChange={e => setDestino(e.target.value)}>
                    <option value="">Selecciona destino…</option>
                    {opcionesDestino(centros, [doc.destino, doc.regla_destino_default]).map(o => (
                      <option key={o.value} value={o.value}>{o.label}</option>
                    ))}
                  </select>
                </Field>
                {/* Qué hace y qué NO hace clasificar. Sin esta línea el operador cree que
                    el número ya se imputó y no revisa nada a mano. */}
                <p style={{ margin: 0, fontSize: 11, lineHeight: 1.35, color: s.faint }}>
                  Clasificar deja registrado —con quién y cuándo— qué es este documento: esa decisión
                  queda auditada y es la base del costeo automático que viene más adelante. Todavía no
                  mueve números: el costo del embarque y la depreciación se siguen calculando como hasta ahora.
                </p>
                <Field label="Motivo (opcional)">
                  <input style={inp} value={motivo} onChange={e => setMotivo(e.target.value)} placeholder="Por qué se decide así" maxLength={400} />
                </Field>
                <button onClick={() => decidir("clasificar")} disabled={saving}
                  style={{ ...btnPrimary(), width: "100%", fontSize: 12, opacity: saving ? 0.5 : 1, cursor: saving ? "not-allowed" : "pointer" }}>
                  {saving ? <Loader2 size={14} className="animate-spin" /> : <CheckCircle2 size={14} />} Clasificar
                </button>
                <button onClick={() => decidir("ignorar")} disabled={saving}
                  style={{ ...btnSecondary(s), width: "100%", fontSize: 12, opacity: saving ? 0.5 : 1, cursor: saving ? "not-allowed" : "pointer" }}>
                  <X size={14} /> Ignorar (no es del ERP)
                </button>
                {doc.decision !== null && (
                  <button onClick={() => decidir("pendiente")} disabled={saving}
                    style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 6, width: "100%", fontSize: 12, color: AMBAR, background: "none", border: "none", cursor: "pointer", padding: "6px 0", fontFamily: "inherit", opacity: saving ? 0.5 : 1 }}>
                    <Clock size={14} /> Volver a pendiente
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

// ─── Conciliación bancaria: score + fila del matcher ──────────────────────────
/** Score como número + barra: verde ≥90, azul ≥75, gris <75; sin score (conflictos) → "—". */
function ScoreChip({ score }: { score: number | null }) {
  const s = useStyles();
  if (score == null) return <span style={{ fontSize: 12, color: s.faint }}>—</span>;
  const color = score >= 90 ? VERDE : score >= 75 ? AZUL : GRIS;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6 }} title={`Score ${score}/100 (cache de pantalla: confirmar revalida todo en el backend)`}>
      <span style={{ fontSize: 12, fontWeight: 700, color }}>{score}</span>
      <div style={{ width: 40, height: 6, borderRadius: 999, overflow: "hidden", flexShrink: 0, background: s.dark ? "#0d1430" : "#E2E8F0" }}>
        <div style={{ height: "100%", borderRadius: 999, background: color, width: `${Math.min(100, Math.max(0, score))}%` }} />
      </div>
    </div>
  );
}

/** Una fila del matcher: las DOS puntas lado a lado (movimiento | documento), el monto
 *  asignado, el score y los motivos como chips.
 *
 *  Acciones por estado: sugerido / en duda / CONFLICTO → Confirmar + Descartar;
 *  ya conciliado → Deshacer la conciliación.
 *  · H6: el conflicto también se confirma. El backend siempre lo aceptó (revalida todos
 *    los topes bajo candado); era esta pantalla la que escondía el botón y dejaba
 *    «descartar» —que entierra el par para siempre— como única salida, aunque el
 *    operador tuviera la factura en la mano y supiera perfectamente cuál era.
 *  · H2: la fila de un conciliado ofrece deshacerlo, que es lo que Tesorería pide
 *    cuando frena el borrado de una cartola o de un movimiento. */
function MatchRow({ m, actingId, onConfirmar, onDescartar }: {
  m: MatchItem;
  actingId: number | null;
  onConfirmar: (m: MatchItem) => void;
  onDescartar: (m: MatchItem) => void;
}) {
  const s = useStyles();
  const [verCandidatos, setVerCandidatos] = useState(false);
  const saving = actingId === m.id;
  const conciliado = m.estado === "confirmado";
  const puedeConfirmar = m.estado === "sugerido" || m.estado === "en_duda" || m.estado === "conflicto";
  // H6: la lista de rivales solo tiene sentido cuando hay más de uno (en un sugerido
  // normal el único candidato es el propio documento de la fila).
  const candidatos = m.candidatos || [];
  const hayCompetencia = candidatos.length > 1;
  const td: React.CSSProperties = { padding: "11px 14px", fontSize: 13 };
  return (
    <>
    <tr style={{ borderBottom: s.cardBd }}>
      {/* Punta 1: el movimiento bancario */}
      <td style={{ ...td, maxWidth: 230 }}>
        {m.movimiento ? (
          <>
            <p style={{ margin: 0, fontSize: 11, whiteSpace: "nowrap", color: s.muted }}>{fmtDate(m.movimiento.fecha)}</p>
            <p style={{ margin: 0, fontSize: 13, fontWeight: 600, color: s.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={m.movimiento.glosa || undefined}>{m.movimiento.glosa || "—"}</p>
            <p style={{ margin: 0, fontSize: 12, fontWeight: 600, color: "var(--monza-accent)" }}>{fmtMonto(m.movimiento.monto)}</p>
          </>
        ) : (
          <p style={{ margin: 0, fontSize: 12, color: ROJO }}>Movimiento eliminado (cartola borrada): re-importe la cartola antes de confirmar</p>
        )}
      </td>
      <td style={{ padding: "11px 4px", textAlign: "center" }}><ArrowLeftRight size={14} color={s.faint} style={{ display: "inline" }} /></td>
      {/* Punta 2: el documento del libro SII */}
      <td style={{ ...td, maxWidth: 230 }}>
        {m.doc ? (
          <>
            <p style={{ margin: 0, fontSize: 11, whiteSpace: "nowrap", color: s.muted }}>
              <span style={{ fontFamily: "ui-monospace, monospace", fontWeight: 600, color: "var(--monza-accent)" }}>{m.doc.folio || "—"}</span>
              {" · "}{TIPO_DOC[m.doc.tipo] || `Doc ${m.doc.tipo}`}{m.doc.fecha ? ` · ${fmtDate(m.doc.fecha)}` : ""}
            </p>
            <p style={{ margin: 0, fontSize: 13, fontWeight: 600, color: s.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={m.doc.emisor || undefined}>{m.doc.emisor || "—"}</p>
            <p style={{ margin: 0, fontSize: 12, fontWeight: 600, color: s.text }}>{fmtMonto(m.doc.monto_efectivo)}</p>
          </>
        ) : (
          <p style={{ margin: 0, fontSize: 12, color: s.faint }}>—</p>
        )}
      </td>
      <td style={{ ...td, textAlign: "right", whiteSpace: "nowrap", fontWeight: 600, color: s.text }}
        title="Monto asignado del cruce (en subsets o parciales puede ser menor al total del documento)">
        {fmtMonto(m.monto_asignado)}
      </td>
      <td style={td}><ScoreChip score={m.score} /></td>
      <td style={td}>
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 4, maxWidth: 300 }}>
          {/* H2: el N° del match — el mismo que nombra Tesorería al frenar un borrado
              («el match #412 está confirmado»). Sin este número la pantalla no servía
              para encontrar el que hay que deshacer. */}
          {conciliado && (
            <>
              <span style={chipMini("rgba(21,128,61,0.14)", VERDE)}
                title={m.origen === "auto"
                  ? "Lo concilió el sistema solo, porque se cumplieron todas las condiciones duras"
                  : "Lo confirmó una persona desde esta pantalla"}>
                CONCILIADO{m.origen === "auto" ? " (automático)" : ""}
              </span>
              <span style={{ ...chipMini("rgba(148,163,184,0.15)", GRIS), fontFamily: "ui-monospace, monospace" }}
                title="Este es el número que nombra Tesorería cuando no deja borrar una cartola o un movimiento">
                N° {m.id}
              </span>
            </>
          )}
          {m.estado === "en_duda" && (
            <span style={chipMini("rgba(217,119,6,0.14)", AMBAR)}
              title="El motor la degradó (o caducó): dejó de cumplirse alguna condición — revisar y decidir a mano">EN DUDA</span>
          )}
          {m.estado === "conflicto" && (
            <span style={chipMini("rgba(185,28,28,0.12)", ROJO)}
              title="Este cargo del banco quedó en conflicto: el motivo de la fila dice por qué (varios documentos calzan con él, o lo que dice el libro contradice la conciliación que ya tiene en Tesorería). Si hay más de un documento compitiendo vas a ver el enlace para mirarlos. Confirma el que corresponde a la factura que tienes en la mano; si ninguno corresponde, descártalo.">CONFLICTO</span>
          )}
          {m.grupo_uuid && (
            <span style={chipMini("rgba(37,99,235,0.12)", AZUL)}
              title="Se confirma o descarta junto con sus filas hermanas (combo factura−nota de crédito o agregado mensual): el grupo es atómico">GRUPO</span>
          )}
          {m.divergente && (
            <span title={m.divergencia_detalle || "El documento cambió después de la sugerencia"}>
              <AlertTriangle size={14} color={AMBAR} style={{ display: "inline" }} />
            </span>
          )}
          {m.motivo.map((r, i) => (
            <span key={i} style={chipMini("rgba(148,163,184,0.15)", GRIS)} title={r}>
              <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r}</span>
            </span>
          ))}
          {/* H6: «3 documentos compiten» sin decir cuáles no sirve de nada. */}
          {hayCompetencia && (
            <button onClick={() => setVerCandidatos(v => !v)}
              style={{ background: "none", border: "none", padding: 0, fontFamily: "inherit", fontSize: 10, fontWeight: 700, cursor: "pointer", textDecoration: "underline", textUnderlineOffset: 2, color: "var(--monza-accent)" }}>
              {verCandidatos ? "Ocultar los documentos que compiten"
                : `Ver los ${candidatos.length} documentos que compiten`}
            </button>
          )}
        </div>
      </td>
      <td style={{ ...td, whiteSpace: "nowrap" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 6 }}>
          {puedeConfirmar && (
            <button onClick={() => onConfirmar(m)} disabled={saving}
              style={{ ...btnPrimary(), padding: "6px 10px", fontSize: 12, opacity: saving ? 0.5 : 1, cursor: saving ? "not-allowed" : "pointer" }}>
              {saving ? <Loader2 size={14} className="animate-spin" /> : <CheckCircle2 size={14} />} Confirmar
            </button>
          )}
          {/* H2: en un conciliado, «descartar» ES el deshacer — y así hay que llamarlo:
              libera el movimiento en Tesorería para poder desconciliarlo o borrar la
              cartola. Con el nombre viejo nadie iba a adivinar que este era el botón. */}
          <button onClick={() => onDescartar(m)} disabled={saving}
            title={conciliado
              ? "Deshace la conciliación: el movimiento del banco queda libre en Tesorería y el par no se vuelve a sugerir"
              : "Entierra el par: el motor no vuelve a sugerir este cruce"}
            style={{ ...btnSecondary(s), padding: "6px 10px", fontSize: 12, opacity: saving ? 0.5 : 1, cursor: saving ? "not-allowed" : "pointer" }}>
            <X size={14} /> {conciliado ? "Deshacer conciliación" : "Descartar"}
          </button>
        </div>
      </td>
    </tr>
    {/* H6: la tabla de rivales, con lo justo para elegir: folio, RUT y lo que le queda
        sin cruzar a cada documento. */}
    {verCandidatos && hayCompetencia && (
      <tr style={{ borderBottom: s.cardBd }}>
        <td colSpan={7} style={{ padding: "4px 14px 16px", background: s.sub }}>
          <p style={{ margin: "0 0 8px", fontSize: 12, color: s.muted }}>
            Estos documentos del libro calzan con el mismo cargo del banco. Si tienes la factura
            a mano y sabes cuál es, confírmalo desde su propia fila: el sistema revisa de nuevo
            los montos antes de aceptarlo.
          </p>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
            <thead>
              <tr style={{ borderBottom: s.cardBd }}>
                {["Folio", "RUT del emisor", "Le queda sin cruzar"].map((h, i) => (
                  <th key={h} style={{ ...TH, padding: "6px 16px 6px 0", fontSize: 10, color: s.faint, textAlign: i === 2 ? "right" : "left" }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {candidatos.map(c => (
                <tr key={c.doc_id}>
                  <td style={{ padding: "6px 16px 6px 0", fontFamily: "ui-monospace, monospace", fontWeight: 600, whiteSpace: "nowrap", color: "var(--monza-accent)" }}>
                    {c.folio || "—"}
                    {m.doc && c.doc_id === m.doc.id && (
                      <span style={{ marginLeft: 6, fontFamily: "inherit", fontSize: 10, fontWeight: 400, color: s.faint }}>
                        (el de esta fila)
                      </span>
                    )}
                  </td>
                  <td style={{ padding: "6px 16px 6px 0", fontFamily: "ui-monospace, monospace", whiteSpace: "nowrap", color: s.muted }}>{fmtRutCanon(c.rut)}</td>
                  <td style={{ padding: "6px 0", textAlign: "right", whiteSpace: "nowrap", color: s.text }}>
                    {c.saldo == null ? "—" : fmtMonto(c.saldo)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </td>
      </tr>
    )}
    </>
  );
}

// ─── Página ───────────────────────────────────────────────────────────────────
const PAGE_SIZE = 50;

export default function MonzaLibroSiiPage() {
  const s = useStyles();
  const inp = inputBase(s);
  const [resumen, setResumen] = useState<Resumen | null>(null);
  const [docs, setDocs] = useState<Doc[]>([]);
  const [total, setTotal] = useState(0);
  const [loadingDocs, setLoadingDocs] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [exporting, setExporting] = useState(false);
  // Filtros de la bandeja (el backend filtra; la página solo pide)
  const [cubeta, setCubeta] = useState("");
  const [decision, setDecision] = useState("");
  const [rutInput, setRutInput] = useState("");
  const [rutApplied, setRutApplied] = useState("");
  const [soloDiv, setSoloDiv] = useState(false);
  // H4: ACTIVO (lo que el SII declara hoy) o DESAPARECIDO (lo que declaraba y dejó de
  // declarar). El tablero anunciaba «+3 desaparecidos del SII» y no había ninguna
  // pantalla donde verlos: era un contador y nada más.
  const [estadoDocs, setEstadoDocs] = useState<"ACTIVO" | "DESAPARECIDO">("ACTIVO");
  const [page, setPage] = useState(1);
  // Conciliación bancaria (matcher)
  const [mResumen, setMResumen] = useState<MatchResumen | null>(null);
  const [matchOpen, setMatchOpen] = useState(false);
  const [matchTab, setMatchTab] = useState<MatchTab>("sugeridos");
  const [matchItems, setMatchItems] = useState<MatchItem[]>([]);
  const [matchTotal, setMatchTotal] = useState(0);
  const [matchLoading, setMatchLoading] = useState(false);
  const [matchPage, setMatchPage] = useState(1);
  const [running, setRunning] = useState(false);
  const [actingId, setActingId] = useState<number | null>(null);
  const [descartando, setDescartando] = useState<MatchItem | null>(null);
  const [descarteMotivo, setDescarteMotivo] = useState("");
  // H2: confirmar deja el movimiento conciliado y es lo que después traba el borrado de
  // la cartola en Tesorería — o sea, la acción MÁS pesada de la sección era la única sin
  // pregunta previa (descartar, que es menos grave, sí abría un cuadro).
  const [confirmando, setConfirmando] = useState<MatchItem | null>(null);
  // H2: buscador por N° de match para la pestaña «Conciliados» — Tesorería frena el
  // borrado nombrando un número («match #412») y hay que poder llegar a ESA fila.
  const [matchIdInput, setMatchIdInput] = useState("");
  const [matchIdApplied, setMatchIdApplied] = useState("");
  const matchRef = useRef<HTMLDivElement | null>(null);
  // Reglas por emisor
  const [reglas, setReglas] = useState<Regla[]>([]);
  const [reglasOpen, setReglasOpen] = useState(false);
  const [reglaRut, setReglaRut] = useState("");
  const [reglaNivel, setReglaNivel] = useState("LOGISTICO");
  const [reglaDestino, setReglaDestino] = useState("");
  const [reglaMotivo, setReglaMotivo] = useState("");
  const [reglaSaving, setReglaSaving] = useState(false);

  const loadResumen = useCallback(async () => {
    try { setResumen((await siiLibroAPI.resumen()).data); }
    catch (e: unknown) { toast.error(errMsg(e, "No se pudo cargar el tablero del libro")); }
  }, []);
  const loadDocs = useCallback(async () => {
    setLoadingDocs(true);
    try {
      const { data } = await siiLibroAPI.documentos({
        cubeta: cubeta || undefined,
        decision: decision || undefined,
        rut: rutApplied || undefined,
        solo_divergentes: soloDiv || undefined,
        estado: estadoDocs,   // H4: ACTIVO por defecto; DESAPARECIDO cuando se pide
        page, page_size: PAGE_SIZE,
      });
      setDocs(data.items); setTotal(data.total);
    } catch (e: unknown) {
      // El 400 del filtro RUT ("'x' no tiene forma de RUT") explica el problema: tal cual.
      toast.error(errMsg(e, "No se pudo cargar la bandeja"), { duration: 6000 });
    } finally { setLoadingDocs(false); }
  }, [cubeta, decision, rutApplied, soloDiv, estadoDocs, page]);
  const loadReglas = useCallback(async () => {
    try { setReglas((await siiLibroAPI.reglas()).data); }
    catch { /* la sección es secundaria: la lista queda vacía y el resto de la página vive */ }
  }, []);
  const loadMatchResumen = useCallback(async () => {
    try { setMResumen((await matchAPI.resumen()).data); }
    catch { /* tarjeta y sección quedan en "—"; el resto de la página vive */ }
  }, []);
  const loadMatchPend = useCallback(async () => {
    if (!matchOpen) return;   // la bandeja del matcher se carga recién al abrir la sección
    setMatchLoading(true);
    try {
      const { data } = await matchAPI.pendientes({
        estado: ESTADO_TAB[matchTab], page: matchPage, page_size: PAGE_SIZE,
        // H2: solo la pestaña «Conciliados» busca por N° (es la única donde Tesorería
        // manda con un número en la mano).
        match_id: matchTab === "conciliados" && matchIdApplied ? matchIdApplied : undefined,
      });
      setMatchItems(data.items); setMatchTotal(data.total);
    } catch (e: unknown) {
      toast.error(errMsg(e, "No se pudo cargar la bandeja del matcher"), { duration: 6000 });
    } finally { setMatchLoading(false); }
  }, [matchOpen, matchTab, matchPage, matchIdApplied]);

  useEffect(() => { loadResumen(); loadReglas(); loadMatchResumen(); }, [loadResumen, loadReglas, loadMatchResumen]);
  useEffect(() => { loadDocs(); }, [loadDocs]);
  useEffect(() => { loadMatchPend(); }, [loadMatchPend]);
  // Una decisión mueve contadores del tablero (pendientes, cubetas): se refrescan juntos.
  const reloadAll = () => { loadDocs(); loadResumen(); };
  // Confirmar/descartar/correr mueve contadores Y bandeja del matcher: juntos también.
  const reloadMatch = () => { loadMatchResumen(); loadMatchPend(); };

  const sincronizar = async () => {
    setSyncing(true);
    try {
      const { data } = await siiLibroAPI.sincronizar();
      if (data.exito) {
        toast.success(`Barrido completo: ${data.total_api ?? 0} documentos en el SII · ${data.nuevos ?? 0} nuevos · ${data.actualizados ?? 0} actualizados${data.desaparecidos ? ` · ${data.desaparecidos} desaparecidos` : ""}`, { duration: 7000 });
      } else {
        toast.error(data.error || "El barrido terminó con error", { duration: 9000 });
      }
      reloadAll();
    } catch (e: unknown) {
      // El 409 ("ya hay un barrido corriendo") se muestra tal cual: explica qué pasa.
      toast.error(errMsg(e, "No se pudo sincronizar con el SII"), { duration: 9000 });
    } finally { setSyncing(false); }
  };

  const exportar = async () => {
    setExporting(true);
    try {
      const desap = estadoDocs === "DESAPARECIDO";
      const r = await siiLibroAPI.exportCsv({ estado: estadoDocs });
      const blob = new Blob([r.data], { type: "text/csv" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      // H4: nombre distinto por vista — el archivo de los desaparecidos no puede
      // llamarse igual que el de los faltantes.
      a.download = desap ? "libro-sii-monza-desaparecidos.csv" : "libro-sii-monza-faltantes.csv";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e: unknown) { toast.error(errMsg(e, "No se pudo exportar el CSV")); }
    finally { setExporting(false); }
  };

  const guardarRegla = async () => {
    if (!reglaRut.trim()) { toast.error("Ingresa el RUT del emisor"); return; }
    setReglaSaving(true);
    try {
      await siiLibroAPI.upsertRegla({
        rut: reglaRut.trim(), nivel: reglaNivel,
        destino_default: reglaDestino || undefined,
        motivo: reglaMotivo.trim() || undefined,
      });
      toast.success("Regla guardada");
      setReglaRut(""); setReglaDestino(""); setReglaMotivo("");
      loadReglas(); loadDocs();   // la regla cambia los defaults sugeridos de la bandeja
    } catch (e: unknown) { toast.error(errMsg(e, "No se pudo guardar la regla"), { duration: 8000 }); }
    finally { setReglaSaving(false); }
  };

  // ─── Acciones de la conciliación bancaria ───────────────────────────────────
  const enfocarMatch = () => {
    setMatchOpen(true);
    // El scroll espera al render de la sección recién abierta.
    setTimeout(() => matchRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }), 60);
  };

  const correrMatcher = async () => {
    setRunning(true);
    try {
      const { data } = await matchAPI.correr();
      if (data.exito) {
        toast.success(
          `Matcher: ${data.autos_nuevos ?? 0} autos nuevos · ${data.sugeridos_nuevos ?? 0} sugeridos nuevos · ${data.conflictos ?? 0} conflictos` +
          `${data.degradados_en_duda ? ` · ${data.degradados_en_duda} degradados a en duda` : ""}` +
          `${data.caducados ? ` · ${data.caducados} caducados` : ""}`,
          { duration: 7000 });
      } else {
        toast.error(data.error || "La corrida del matcher terminó con error", { duration: 9000 });
      }
      reloadMatch();
    } catch (e: unknown) {
      // El 409 («hay un barrido/corrida en curso») se muestra tal cual: explica qué pasa.
      toast.error(errMsg(e, "No se pudo correr el matcher"), { duration: 9000 });
    } finally { setRunning(false); }
  };

  // H2: se dispara desde el cuadro de confirmación, nunca de un clic suelto.
  const confirmarMatch = async () => {
    const m = confirmando;
    if (!m) return;
    setActingId(m.id);
    try {
      await matchAPI.confirmar(m.id);
      toast.success(m.grupo_uuid
        ? "Conciliado (grupo completo): el movimiento del banco queda conciliado en Tesorería"
        : "Conciliado: el movimiento del banco queda conciliado en Tesorería");
      setConfirmando(null);
      reloadMatch();
    } catch (e: unknown) {
      // Los 409 del backend EXPLICAN qué pasó (saldos, doc cambiado, carrera con
      // Tesorería…): van tal cual, jamás tapados con un mensaje genérico.
      toast.error(errMsg(e, "No se pudo confirmar el match"), { duration: 9000 });
    } finally { setActingId(null); }
  };

  const descartarMatch = async () => {
    if (!descartando) return;
    const eraConciliado = descartando.estado === "confirmado";
    setActingId(descartando.id);
    try {
      await matchAPI.descartar(descartando.id, descarteMotivo.trim() || undefined);
      // H2: el mismo endpoint hace las dos cosas, pero lo que el operador acaba de
      // hacer NO es lo mismo — y lo que sigue (desconciliar, borrar la cartola) tampoco.
      toast.success(eraConciliado
        ? "Conciliación deshecha: el movimiento quedó libre en Tesorería (ya puedes desconciliarlo o borrar la cartola)"
        : "Cruce descartado: el sistema no volverá a sugerir este par", { duration: 7000 });
      setDescartando(null); setDescarteMotivo("");
      reloadMatch();
    } catch (e: unknown) {
      toast.error(errMsg(e, "No se pudo descartar el match"), { duration: 9000 });
    } finally { setActingId(null); }
  };

  // Filtros: cualquier cambio vuelve a la página 1
  const setFiltroCubeta = (c: string) => { setCubeta(prev => prev === c ? "" : c); setPage(1); };
  const setFiltroDecision = (d: string) => { setDecision(prev => prev === d ? "" : d); setPage(1); };
  const aplicarRut = () => { setRutApplied(rutInput.trim()); setPage(1); };
  const limpiarRut = () => { setRutInput(""); setRutApplied(""); setPage(1); };
  // H4: entrar o salir de la lista de desaparecidos. Al entrar se limpia el filtro de
  // cubeta: ahí la cubeta es siempre la misma («El SII ya no lo declara») y un filtro
  // viejo puesto dejaría la lista vacía sin que nadie entienda por qué.
  const verEstado = (e: "ACTIVO" | "DESAPARECIDO") => { setEstadoDocs(e); setCubeta(""); setPage(1); };

  const edadHoras = resumen?.edad_horas_ultimo_exitoso;
  const edadCritica = resumen?.edad_critica ?? true;
  const edadColor = edadCritica ? ROJO : (edadHoras ?? 0) >= 24 ? AMBAR : VERDE;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const matchTotalPages = Math.max(1, Math.ceil(matchTotal / PAGE_SIZE));
  const centros = resumen?.centros_sugeridos ?? [];
  // H3: lo que el ERP compró y NUNCA va a aparecer en el libro del SII (importaciones,
  // otra moneda, gastos sin documento tributario), sumado en los meses que se muestran.
  const erpFueraLibro = (resumen?.cuadratura_mensual ?? [])
    .reduce((acc, c) => acc + (c.erp_fuera_libro ?? 0), 0);
  const verDesaparecidos = estadoDocs === "DESAPARECIDO";

  const chip = (active: boolean): React.CSSProperties => ({
    padding: "6px 12px", borderRadius: 999, fontSize: 12, fontWeight: 600, cursor: "pointer", fontFamily: "inherit",
    border: active ? "1px solid var(--monza-accent)" : s.cardBd,
    background: active ? "var(--monza-accent)" : s.cardBg,
    color: active ? "white" : s.muted,
  });
  const kpiCard = (active: boolean): React.CSSProperties => ({
    background: s.cardBg, borderRadius: 14, padding: 14, textAlign: "left", cursor: "pointer", fontFamily: "inherit",
    border: active ? "1px solid var(--monza-accent)" : s.cardBd,
    boxShadow: active ? "0 0 0 2px rgba(var(--monza-accent-rgb),0.25)" : "none",
  });
  const kpiIconBox: React.CSSProperties = {
    width: 32, height: 32, borderRadius: 10, display: "flex", alignItems: "center", justifyContent: "center",
    marginBottom: 8, background: s.dark ? "#0d1430" : "#F1F5F9",
  };
  const kpiLabel: React.CSSProperties = { margin: 0, fontSize: 10, textTransform: "uppercase", letterSpacing: 0.8, color: s.faint, lineHeight: 1.3 };

  const tarjetasCubeta = [
    { key: "ESTA", icon: CheckCircle2, color: VERDE, label: "Está en el ERP", valor: resumen?.cubetas.ESTA ?? 0, sub: "con compra activa que calza" },
    { key: "NO_ESTA", icon: AlertCircle, color: ROJO, label: "No está", valor: resumen?.cubetas.NO_ESTA ?? 0, sub: "la deuda invisible" },
    { key: "INDETERMINADO", icon: HelpCircle, color: AMBAR, label: "Indeterminado", valor: resumen?.cubetas.INDETERMINADO ?? 0, sub: `${resumen?.compras_erp_sin_llave ?? 0} compras del ERP sin llave` },
  ];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 16, flexWrap: "wrap" }}>
        <div style={{ minWidth: 0 }}>
          <h1 style={{ margin: 0, fontSize: 24, fontWeight: 700, color: s.text }}>Libro de Compras SII</h1>
          <p style={{ margin: "4px 0 0", fontSize: 13, color: s.muted }}>
            Qué facturas de proveedor existen ante el SII y no están registradas en el ERP · clasificación por documento
          </p>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          {/* H4: el botón exporta LO QUE SE ESTÁ MIRANDO — antes siempre mandaba los
              activos, aunque la pantalla mostrara los desaparecidos. */}
          <button onClick={exportar} disabled={exporting} style={{ ...btnSecondary(s), opacity: exporting ? 0.5 : 1 }}>
            {exporting ? <Loader2 size={15} className="animate-spin" /> : <Download size={15} />}
            {verDesaparecidos ? "Exportar desaparecidos (CSV)" : "Exportar faltantes (CSV)"}
          </button>
          <button onClick={reloadAll} title="Refrescar la pantalla" style={btnSecondary(s)}><RefreshCw size={15} /></button>
        </div>
      </div>

      {/* Tablero: documentos + cubetas clicables + pendientes + divergentes */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 12 }}>
        <div style={{ background: s.cardBg, border: s.cardBd, borderRadius: 14, padding: 14 }}>
          <div style={kpiIconBox}><BookOpenCheck size={16} color="var(--monza-accent)" /></div>
          <p style={kpiLabel}>Documentos activos</p>
          <p style={{ margin: "2px 0 0", fontSize: 19, fontWeight: 700, color: "var(--monza-accent)" }}>{resumen?.documentos_activos ?? "—"}</p>
        </div>
        {tarjetasCubeta.map(t => (
          <button key={t.key} onClick={() => setFiltroCubeta(t.key)}
            title={cubeta === t.key ? "Quitar filtro" : "Filtrar la bandeja por esta cubeta"}
            style={kpiCard(cubeta === t.key)}>
            <div style={kpiIconBox}><t.icon size={16} color={t.color} /></div>
            <p style={kpiLabel}>{t.label}</p>
            <p style={{ margin: "2px 0 0", fontSize: 19, fontWeight: 700, color: t.color }}>{t.valor}</p>
            <p style={{ margin: "2px 0 0", fontSize: 10, color: s.faint, lineHeight: 1.3 }}>{t.sub}</p>
          </button>
        ))}
        <button onClick={() => setFiltroDecision("pendiente")}
          title="Filtrar la bandeja: solo pendientes de decisión"
          style={kpiCard(decision === "pendiente")}>
          <div style={kpiIconBox}><Clock size={16} color={AMBAR} /></div>
          <p style={kpiLabel}>Pendientes de decisión</p>
          <p style={{ margin: "2px 0 0", fontSize: 19, fontWeight: 700, color: AMBAR }}>{resumen?.pendientes_decision ?? "—"}</p>
        </button>
        {/* H4: antes la tarjeta entera era UN botón, así que el clic sobre la sub-línea
            «+3 desaparecidos del SII» activaba el filtro de divergentes —otra cosa— y los
            desaparecidos no se podían ver en ninguna parte. Ahora son dos botones dentro
            de la misma tarjeta, cada uno a SU lista. */}
        <div style={{ ...kpiCard(soloDiv || verDesaparecidos), cursor: "default" }}>
          <button onClick={() => { setSoloDiv(v => !v); setPage(1); }}
            title="Filtrar la bandeja: solo documentos que cambiaron en el SII después de decididos"
            style={{ width: "100%", textAlign: "left", background: "none", border: "none", padding: 0, cursor: "pointer", fontFamily: "inherit" }}>
            <div style={kpiIconBox}><AlertTriangle size={16} color={ROJO} /></div>
            <p style={kpiLabel}>Divergentes</p>
            <p style={{ margin: "2px 0 0", fontSize: 19, fontWeight: 700, color: ROJO }}>{resumen?.divergentes ?? "—"}</p>
          </button>
          {(resumen?.desaparecidos ?? 0) > 0 && (
            <button onClick={() => verEstado(verDesaparecidos ? "ACTIVO" : "DESAPARECIDO")}
              title="Ver la lista de documentos que el SII declaraba y dejó de declarar"
              style={{ margin: "4px 0 0", padding: 0, background: "none", border: "none", textAlign: "left", fontFamily: "inherit", fontSize: 10, lineHeight: 1.3, color: ROJO, cursor: "pointer", textDecoration: "underline", textUnderlineOffset: 2 }}>
              +{resumen!.desaparecidos} desaparecidos del SII — {verDesaparecidos ? "volver a la lista normal" : "ver cuáles son"}
            </button>
          )}
        </div>
      </div>

      {/* Conciliación bancaria: tarjeta-resumen clicable (abre/enfoca la sección) */}
      <button onClick={enfocarMatch}
        title="Abrir la conciliación bancaria (matcher banco ↔ libro ↔ egresos)"
        style={{ ...kpiCard(matchOpen), width: "100%" }}>
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "12px 32px" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 12, minWidth: 200 }}>
            <div style={{ ...kpiIconBox, marginBottom: 0, flexShrink: 0 }}><Landmark size={16} color="var(--monza-accent)" /></div>
            <div>
              <p style={kpiLabel}>Conciliación bancaria · % del libro conciliado</p>
              <p style={{ margin: "2px 0 0", fontSize: 19, fontWeight: 700, color: "var(--monza-accent)" }}>
                {mResumen ? `${mResumen.pct_libro_conciliado_por_monto.toLocaleString("es-CL")}%` : "—"}
              </p>
            </div>
          </div>
          <div>
            <p style={kpiLabel}>Autos vivos</p>
            <p style={{ margin: "2px 0 0", fontSize: 16, fontWeight: 700, color: VERDE }}>{mResumen?.autos_vivos ?? "—"}</p>
          </div>
          <div>
            <p style={kpiLabel}>Sugeridos pendientes</p>
            <p style={{ margin: "2px 0 0", fontSize: 16, fontWeight: 700, color: AMBAR }}>{mResumen?.sugeridos ?? "—"}</p>
          </div>
          <div>
            <p style={kpiLabel}>Conflictos</p>
            <p style={{ margin: "2px 0 0", fontSize: 16, fontWeight: 700, color: (mResumen?.conflictos ?? 0) > 0 ? ROJO : s.faint }}>
              {mResumen?.conflictos ?? "—"}
            </p>
          </div>
          {(mResumen?.en_duda ?? 0) > 0 && (
            <div>
              <p style={kpiLabel}>En duda</p>
              <p style={{ margin: "2px 0 0", fontSize: 16, fontWeight: 700, color: AMBAR }}>{mResumen!.en_duda}</p>
            </div>
          )}
        </div>
      </button>

      {/* Edad del barrido + cuadratura mensual */}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 12 }}>
        <div style={{ flex: "1 1 260px", background: s.cardBg, border: s.cardBd, borderRadius: 14, padding: 16, display: "flex", flexDirection: "column", gap: 10 }}>
          <h3 style={{ margin: 0, fontSize: 13, fontWeight: 700, color: s.text }}>Último barrido del SII</h3>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span className={edadCritica ? "animate-pulse" : undefined}
              style={{ width: 10, height: 10, borderRadius: "50%", background: edadColor, flexShrink: 0 }} />
            <span style={{ fontSize: 13, fontWeight: 700, color: edadColor }}>
              {edadHoras == null ? "Nunca sincronizado" : `Hace ${edadHoras} h`}
            </span>
          </div>
          {edadCritica && (
            <p style={{ margin: 0, fontSize: 12, color: ROJO }}>
              {edadHoras == null
                ? "El espejo del libro está vacío o nunca terminó un barrido con éxito: sincroniza para poder confiar en las cubetas."
                : "Más de 48 horas sin barrido exitoso: las cubetas pueden estar desactualizadas."}
            </p>
          )}
          {resumen?.ultimo_barrido && (
            <p style={{ margin: 0, fontSize: 11, color: s.faint }}>
              Último intento: {resumen.ultimo_barrido.terminado_at ? fmtDate(resumen.ultimo_barrido.terminado_at) : "—"}
              {resumen.ultimo_barrido.origen ? ` · ${resumen.ultimo_barrido.origen}` : ""}
              {resumen.ultimo_barrido.total_api != null ? ` · ${resumen.ultimo_barrido.total_api} docs en el API` : ""}
            </p>
          )}
          {resumen?.ultimo_barrido && resumen.ultimo_barrido.exito === false && (
            <div style={{ borderRadius: 10, border: "1px solid rgba(185,28,28,0.25)", background: "rgba(185,28,28,0.1)", padding: "8px 10px", fontSize: 12, color: ROJO }}>
              El último barrido falló: {resumen.ultimo_barrido.error || "sin detalle"}
            </div>
          )}
          {/* El barrido puede terminar BIEN y con avisos: un documento del SII sin montos
              legibles se espeja igual (con monto en blanco) en vez de tumbar la corrida
              entera, y deja constancia. Esa constancia viaja en el mismo campo `error`,
              y antes solo se pintaba cuando la corrida había FALLADO: el aviso existía y
              nadie lo veía nunca — el mismo agujero que la leyenda del folio parecido.
              Va en ámbar, no en rojo: no es una falla, es algo que hay que mirar. */}
          {resumen?.ultimo_barrido && resumen.ultimo_barrido.exito === true
            && resumen.ultimo_barrido.error && (
            <div style={{ borderRadius: 10, border: "1px solid rgba(217,119,6,0.25)", background: "rgba(217,119,6,0.1)", padding: "8px 10px", fontSize: 12, color: AMBAR }}>
              El último barrido terminó bien, con un aviso: {resumen.ultimo_barrido.error}
            </div>
          )}
          <button onClick={sincronizar} disabled={syncing}
            style={{ ...btnPrimary(), width: "100%", marginTop: "auto", opacity: syncing ? 0.5 : 1, cursor: syncing ? "not-allowed" : "pointer" }}>
            {syncing ? <Loader2 size={15} className="animate-spin" /> : <RefreshCw size={15} />}
            {syncing ? "Sincronizando…" : "Sincronizar ahora"}
          </button>
        </div>

        <div style={{ flex: "2 1 420px", background: s.cardBg, border: s.cardBd, borderRadius: 14, padding: 16 }}>
          <h3 style={{ margin: 0, fontSize: 13, fontWeight: 700, color: s.text }}>Cuadratura mensual · Σ libro SII vs Σ compras ERP</h3>
          {/* H3: la diferencia que se mostraba era «libro − ERP» a secas, y esa resta NO
              puede dar $0 nunca: el libro trae notas de crédito y documentos ignorados, y
              el ERP trae importaciones que jamás pasan por el libro del SII. La alarma
              estaba encendida todos los meses, así que dejó de ser alarma. Ahora se
              muestra la resta comparable, con las tres piezas a la vista para que el
              número se pueda abrir. */}
          <p style={{ margin: "4px 0 12px", fontSize: 11, color: s.faint }}>
            El número del controller: cuando la diferencia no es $0, hay facturas del SII sin registrar
            (o compras registradas sin respaldo tributario). La resta ya descuenta lo que nunca puede calzar.
          </p>
          {(resumen?.cuadratura_mensual?.length ?? 0) === 0 ? (
            <p style={{ margin: 0, padding: "16px 0", textAlign: "center", fontSize: 12, color: s.faint }}>Sin datos aún: sincroniza el libro primero.</p>
          ) : (
            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                <thead>
                  <tr style={{ borderBottom: s.cardBd }}>
                    {[
                      ["Mes", ""],
                      ["Σ Libro SII", "Todo lo que el SII declara en el mes, notas de crédito incluidas"],
                      ["(−) Ignorados y notas de crédito", "Lo que se saca del libro para poder comparar: los documentos que marcaste «ignorar» (no son del ERP) y las notas de crédito"],
                      ["Σ ERP comparable", "Las compras del ERP que SÍ deberían estar en el libro: nacionales, en pesos y con documento tributario"],
                      ["Diferencia", "Σ Libro SII − ignorados y notas de crédito − Σ ERP comparable. Esta es la resta que debe dar $0: si no da, falta registrar una factura del SII (o hay una compra registrada sin respaldo tributario)"],
                    ].map(([h, ayuda], i) => (
                      <th key={h} title={ayuda || undefined}
                        style={{ ...TH, padding: "7px 10px", textAlign: i === 0 ? "left" : "right", color: s.faint }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {resumen!.cuadratura_mensual.map(c => {
                    // Fallback a las claves viejas: si el backend no manda el desglose, se
                    // sigue viendo la resta bruta antes que una pantalla en blanco.
                    const menos = (c.libro_ignorados ?? 0) + (c.libro_nc ?? 0);
                    const erpComp = c.erp_comparable ?? c.erp;
                    const dif = c.diferencia_explicada ?? c.diferencia;
                    const cuadra = Math.abs(dif) < 1;
                    const diffColor = cuadra ? VERDE : dif > 0 ? ROJO : AMBAR;
                    return (
                      <tr key={c.mes} style={{ borderBottom: s.cardBd }}>
                        <td style={{ padding: "7px 10px", fontWeight: 600, textTransform: "capitalize", color: s.text }}>{fmtMes(c.mes)}</td>
                        <td style={{ padding: "7px 10px", textAlign: "right", color: s.muted }}>{fmtMonto(c.libro)}</td>
                        <td style={{ padding: "7px 10px", textAlign: "right", color: s.faint }}>{menos === 0 ? "—" : fmtMonto(menos)}</td>
                        <td style={{ padding: "7px 10px", textAlign: "right", color: s.muted }}>{fmtMonto(erpComp)}</td>
                        <td style={{ padding: "7px 10px", textAlign: "right", fontWeight: 700, color: diffColor }}>
                          {cuadra ? "Cuadra" : (dif > 0 ? "+" : "") + fmtMonto(dif)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          {/* H3: lo excluido se dice, no se esconde — si no, la resta parece arbitraria. */}
          {erpFueraLibro !== 0 && (
            <p style={{ margin: "8px 0 0", fontSize: 11, color: s.faint }}>
              Compras del ERP que no pasan por el libro del SII (importaciones, compras en otra moneda,
              gastos sin documento tributario): {fmtMonto(erpFueraLibro)} en los meses de arriba —
              quedan fuera de la resta a propósito.
            </p>
          )}
          {(resumen?.monto_sin_fecha ?? 0) !== 0 && (
            <p style={{ margin: "8px 0 0", fontSize: 11, color: s.faint }}>
              Excluido del cuadre por venir sin fecha: {fmtMonto(resumen!.monto_sin_fecha)} (no se esconde: no hay mes al que imputarlo).
            </p>
          )}
        </div>
      </div>

      {/* Conciliación bancaria: la cara del matcher banco ↔ libro ↔ egresos (plegable) */}
      <div ref={matchRef} style={{ background: s.cardBg, border: s.cardBd, borderRadius: 14 }}>
        <button onClick={() => setMatchOpen(o => !o)} aria-expanded={matchOpen}
          style={{ width: "100%", display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, padding: "12px 16px", background: "none", border: "none", cursor: "pointer", textAlign: "left", fontFamily: "inherit" }}>
          <span style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", fontSize: 13, fontWeight: 700, color: s.text }}>
            <Landmark size={15} color="var(--monza-accent)" /> Conciliación bancaria
            <span style={{ fontSize: 12, fontWeight: 400, color: s.faint }}>
              · {mResumen ? `${mResumen.pct_libro_conciliado_por_monto.toLocaleString("es-CL")}% del libro conciliado por monto` : "sin datos del matcher"}
            </span>
            {(mResumen?.conflictos ?? 0) > 0 && (
              <span style={chipMini("rgba(185,28,28,0.12)", ROJO)}>
                {mResumen!.conflictos} conflicto{mResumen!.conflictos === 1 ? "" : "s"}
              </span>
            )}
          </span>
          {matchOpen ? <ChevronUp size={15} color={s.muted} /> : <ChevronDown size={15} color={s.muted} />}
        </button>
        {matchOpen && (
          <div style={{ padding: "0 16px 16px", borderTop: s.cardBd, display: "flex", flexDirection: "column", gap: 14, paddingTop: 14 }}>
            <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", justifyContent: "space-between", gap: 10 }}>
              <p style={{ margin: 0, fontSize: 12, color: s.muted, flex: "1 1 320px" }}>
                El matcher cruza la cartola del banco con el libro del SII y los egresos de Tesorería.
                {" "}<b style={{ color: s.text }}>Confirmar</b> deja el movimiento conciliado (mismo candado de Tesorería);
                {" "}<b style={{ color: s.text }}>descartar</b> entierra el par: el motor no lo vuelve a sugerir.
                {" "}Lo ya conciliado vive en la pestaña <b style={{ color: s.text }}>Conciliados</b>, y desde ahí se deshace.
              </p>
              <div style={{ display: "flex", alignItems: "center", gap: 10, flexShrink: 0, flexWrap: "wrap" }}>
                {mResumen?.ultima_corrida && (
                  <span style={{ fontSize: 11, whiteSpace: "nowrap", color: s.faint }} title={mResumen.ultima_corrida.error || undefined}>
                    Última corrida: {mResumen.ultima_corrida.terminado_at ? fmtDate(mResumen.ultima_corrida.terminado_at) : "—"}
                    {` · ${mResumen.ultima_corrida.origen}`}
                    {mResumen.ultima_corrida.exito === false ? " · falló" : ""}
                  </span>
                )}
                <button onClick={correrMatcher} disabled={running}
                  style={{ ...btnPrimary(), opacity: running ? 0.5 : 1, cursor: running ? "not-allowed" : "pointer" }}>
                  {running ? <Loader2 size={15} className="animate-spin" /> : <Play size={15} />}
                  {running ? "Corriendo…" : "Correr matcher"}
                </button>
              </div>
            </div>

            {/* Pestañas: sugeridos (score desc, lo da el backend) / conflictos / autos /
                conciliados (H2: la que faltaba para poder DESHACER). */}
            <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
              {([
                ["sugeridos", `Sugeridos${mResumen ? ` (${mResumen.sugeridos})` : ""}`],
                ["conflictos", `Conflictos${mResumen ? ` (${mResumen.conflictos})` : ""}`],
                ["autos", `Autos${mResumen ? ` (${mResumen.autos_vivos}${mResumen.en_duda > 0 ? ` · ${mResumen.en_duda} en duda` : ""})` : ""}`],
                ["conciliados", `Conciliados${mResumen ? ` (${mResumen.confirmados})` : ""}`],
              ] as [MatchTab, string][]).map(([k, l]) => (
                <button key={k} onClick={() => {
                  setMatchTab(k); setMatchPage(1);
                  // El buscador por N° solo tiene sentido en «Conciliados»: al salir se
                  // limpia para que no filtre en silencio otra pestaña.
                  if (k !== "conciliados") { setMatchIdInput(""); setMatchIdApplied(""); }
                }} style={chip(matchTab === k)}>{l}</button>
              ))}
            </div>

            {/* CONCILIADOS: la salida del callejón. Tesorería no deja borrar una cartola
                ni desconciliar un movimiento mientras su cruce con el libro siga vivo, y
                nombra el N° del cruce; acá se busca ese N° y se deshace. */}
            {matchTab === "conciliados" && (
              <div style={{ borderRadius: 10, border: s.cardBd, background: s.sub, padding: 12, display: "flex", flexDirection: "column", gap: 10 }}>
                <p style={{ margin: 0, fontSize: 12, color: s.muted }}>
                  Cruces ya conciliados (los que confirmó una persona y los que el sistema concilió solo).
                  Si Tesorería no te deja borrar una cartola o desconciliar un movimiento porque «el cruce
                  N° tanto está confirmado», búscalo acá por ese número y aprieta
                  {" "}<b style={{ color: s.text }}>Deshacer conciliación</b>: el movimiento del banco
                  queda libre y recién ahí Tesorería te deja seguir.
                </p>
                <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                  <input style={{ ...inp, width: 150, padding: "6px 10px", fontSize: 12, background: s.cardBg }}
                    placeholder="N° del cruce…" inputMode="numeric" value={matchIdInput}
                    onChange={e => setMatchIdInput(e.target.value.replace(/\D/g, ""))}
                    onKeyDown={e => { if (e.key === "Enter") { setMatchIdApplied(matchIdInput.trim()); setMatchPage(1); } }} />
                  <button onClick={() => { setMatchIdApplied(matchIdInput.trim()); setMatchPage(1); }}
                    style={{ ...btnSecondary(s), padding: "6px 10px", fontSize: 12 }}>
                    <Search size={14} /> Buscar
                  </button>
                  {matchIdApplied && (
                    <button onClick={() => { setMatchIdInput(""); setMatchIdApplied(""); setMatchPage(1); }}
                      style={{ background: "none", border: "none", padding: 0, fontFamily: "inherit", fontSize: 12, cursor: "pointer", textDecoration: "underline", textUnderlineOffset: 2, color: s.muted }}>
                      Ver todos los conciliados
                    </button>
                  )}
                </div>
              </div>
            )}

            {/* AUTOS: los vivos no tienen listado en el API (ya están conciliados en
                Tesorería); acá va su agregado + los degradados a «en duda», que sí piden decisión. */}
            {matchTab === "autos" && (
              <div style={{ borderRadius: 10, border: s.cardBd, background: s.sub, padding: 12, display: "flex", alignItems: "flex-start", gap: 10 }}>
                <span style={{ ...chipMini("rgba(21,128,61,0.14)", VERDE), marginTop: 2, flexShrink: 0 }}>AUTO</span>
                <div style={{ fontSize: 12, color: s.muted }}>
                  <p style={{ margin: 0 }}>
                    <b style={{ color: s.text }}>{mResumen?.autos_vivos ?? 0}</b> conciliación{(mResumen?.autos_vivos ?? 0) === 1 ? "" : "es"} automática{(mResumen?.autos_vivos ?? 0) === 1 ? "" : "s"} viva{(mResumen?.autos_vivos ?? 0) === 1 ? "" : "s"}:
                    el motor confirma solo cuando TODAS las condiciones duras se cumplen.
                    <span title="Los autos no se tocan a mano: si en una corrida posterior dejan de cumplirse (documento cambiado en Wasabil, gemelo nuevo que también calzaría, contradicción con la conciliación de Tesorería), el motor los degrada solo a «en duda» y aparecen abajo para decisión humana.">
                      <HelpCircle size={13} color={s.faint} style={{ display: "inline", marginLeft: 4, verticalAlign: "-2px" }} />
                    </span>
                  </p>
                  <p style={{ margin: "4px 0 0", color: s.faint }}>
                    Cada uno deja su movimiento conciliado en Tesorería, y todos están listados —con su
                    N°— en la pestaña «Conciliados», que es donde se deshacen si hiciera falta.
                    Abajo: los degradados o caducados a «en duda», con el porqué en sus motivos.
                  </p>
                </div>
              </div>
            )}

            {matchLoading && <div style={{ display: "flex", justifyContent: "center", padding: 44 }}><Loader2 size={26} className="animate-spin" color="var(--monza-accent)" /></div>}
            {!matchLoading && matchItems.length === 0 && (
              <div style={{ borderRadius: 10, border: s.cardBd, padding: "36px 16px", textAlign: "center" }}>
                <Landmark size={30} style={{ margin: "0 auto 8px", display: "block", opacity: 0.2, color: s.muted }} />
                <p style={{ margin: 0, fontSize: 13, fontWeight: 600, color: s.muted }}>
                  {matchTab === "sugeridos" ? "Sin sugerencias pendientes"
                    : matchTab === "conflictos" ? "Sin conflictos"
                    : matchTab === "conciliados" ? (matchIdApplied ? `No hay ningún cruce conciliado con el N° ${matchIdApplied}` : "Todavía no hay cruces conciliados")
                    : "Ningún auto degradado a «en duda»"}
                </p>
                <p style={{ margin: "4px 0 0", fontSize: 12, color: s.faint }}>
                  {matchTab === "sugeridos" ? "Usa «Correr matcher» para generar cruces nuevos."
                    : matchTab === "conflictos" ? "Cuando dos candidatos compitan por el mismo movimiento, aparecen aquí."
                    : matchTab === "conciliados" ? (matchIdApplied ? "Revisa el número que te mostró Tesorería, o quita el filtro para ver todos." : "Acá van a aparecer los cruces que se confirmen, para poder deshacerlos.")
                    : "Los autos vivos siguen válidos: no piden decisión humana."}
                </p>
              </div>
            )}
            {!matchLoading && matchItems.length > 0 && (
              <div style={{ borderRadius: 10, border: s.cardBd, overflow: "hidden" }}>
                <div style={{ overflowX: "auto" }}>
                  <table style={{ width: "100%", borderCollapse: "collapse" }}>
                    <thead>
                      <tr style={{ borderBottom: s.cardBd, background: s.sub }}>
                        {["Movimiento (banco)", "", "Documento (libro SII)", "Asignado", "Score", "Motivo", ""].map((h, i) => (
                          <th key={`${h}-${i}`}
                            style={{ ...TH, color: s.faint, padding: i === 1 ? "10px 4px" : "10px 14px", textAlign: i === 3 ? "right" : "left" }}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {matchItems.map(m => (
                        <MatchRow key={m.id} m={m} actingId={actingId}
                          onConfirmar={mm => setConfirmando(mm)}
                          onDescartar={mm => { setDescartando(mm); setDescarteMotivo(""); }} />
                      ))}
                    </tbody>
                  </table>
                </div>
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "10px 14px", borderTop: s.cardBd }}>
                  <span style={{ fontSize: 11, color: s.faint }}>
                    {(matchPage - 1) * PAGE_SIZE + 1}–{Math.min(matchPage * PAGE_SIZE, matchTotal)} de {matchTotal} matches
                  </span>
                  <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                    <button onClick={() => setMatchPage(p => Math.max(1, p - 1))} disabled={matchPage <= 1}
                      style={{ ...btnSecondary(s), padding: "5px 10px", fontSize: 12, opacity: matchPage <= 1 ? 0.4 : 1, cursor: matchPage <= 1 ? "not-allowed" : "pointer" }}>
                      <ChevronLeft size={13} /> Anterior
                    </button>
                    <span style={{ fontSize: 12, padding: "0 6px", color: s.muted }}>{matchPage} / {matchTotalPages}</span>
                    <button onClick={() => setMatchPage(p => Math.min(matchTotalPages, p + 1))} disabled={matchPage >= matchTotalPages}
                      style={{ ...btnSecondary(s), padding: "5px 10px", fontSize: 12, opacity: matchPage >= matchTotalPages ? 0.4 : 1, cursor: matchPage >= matchTotalPages ? "not-allowed" : "pointer" }}>
                      Siguiente <ChevronRight size={13} />
                    </button>
                  </div>
                </div>
              </div>
            )}
          </div>
        )}
      </div>

      {/* H2: cuadro de confirmación. Confirmar deja el movimiento conciliado y es
          justamente lo que después traba el borrado de la cartola en Tesorería: era la
          acción más pesada de la sección y la única que se disparaba de un clic suelto,
          mientras que descartar —menos grave— sí preguntaba. */}
      {confirmando && (
        <div style={{ position: "fixed", inset: 0, zIndex: 300, display: "flex", alignItems: "center", justifyContent: "center", background: "rgba(0,0,0,0.6)", padding: 16 }}
          onClick={() => { if (actingId === null) setConfirmando(null); }}>
          <div style={{ width: "100%", maxWidth: 400, borderRadius: 14, border: s.cardBd, background: s.cardBg, boxShadow: "0 20px 50px rgba(0,0,0,0.4)", padding: 18, display: "flex", flexDirection: "column", gap: 12 }}
            onClick={e => e.stopPropagation()}>
            <h3 style={{ margin: 0, fontSize: 14, fontWeight: 700, color: s.text }}>Confirmar la conciliación</h3>
            <p style={{ margin: 0, fontSize: 12, color: s.muted }}>
              El movimiento del banco queda <b style={{ color: s.text }}>conciliado</b> con
              este documento del libro, igual que si lo conciliaras en Tesorería.
              {confirmando.grupo_uuid ? " Se confirma el grupo completo (las filas hermanas van juntas)." : ""}
            </p>
            {confirmando.estado === "conflicto" && (
              <div style={{ borderRadius: 10, border: "1px solid rgba(245,158,11,0.35)", background: "rgba(245,158,11,0.10)", padding: "10px 12px", fontSize: 12, color: AMBAR }}>
                Ojo: hay más de un documento que calza con este mismo cargo. Al confirmar, el cargo queda
                explicado por ESTE. Si no estás seguro, cierra y mira antes la lista de los que compiten.
              </div>
            )}
            <p style={{ margin: 0, fontSize: 12, color: s.faint }}>
              Se puede deshacer después desde la pestaña «Conciliados».
            </p>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 8 }}>
              <button onClick={() => setConfirmando(null)} disabled={actingId !== null}
                style={{ ...btnSecondary(s), fontSize: 12, opacity: actingId !== null ? 0.5 : 1 }}>Cancelar</button>
              <button onClick={confirmarMatch} disabled={actingId !== null}
                style={{ ...btnPrimary(), fontSize: 12, opacity: actingId !== null ? 0.5 : 1, cursor: actingId !== null ? "not-allowed" : "pointer" }}>
                {actingId !== null ? <Loader2 size={14} className="animate-spin" /> : <CheckCircle2 size={14} />} Conciliar
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Prompt-modal chico: descartar con motivo opcional */}
      {descartando && (
        <div style={{ position: "fixed", inset: 0, zIndex: 300, display: "flex", alignItems: "center", justifyContent: "center", background: "rgba(0,0,0,0.6)", padding: 16 }}
          onClick={() => { if (actingId === null) { setDescartando(null); setDescarteMotivo(""); } }}>
          <div style={{ width: "100%", maxWidth: 400, borderRadius: 14, border: s.cardBd, background: s.cardBg, boxShadow: "0 20px 50px rgba(0,0,0,0.4)", padding: 18, display: "flex", flexDirection: "column", gap: 12 }}
            onClick={e => e.stopPropagation()}>
            {/* H2: el mismo cuadro sirve para descartar una sugerencia y para DESHACER un
                conciliado (el backend usa el mismo camino), pero lo que pasa es distinto
                y hay que decirlo distinto. */}
            <h3 style={{ margin: 0, fontSize: 14, fontWeight: 700, color: s.text }}>
              {descartando.estado === "confirmado" ? "Deshacer la conciliación" : "Descartar el cruce"}
            </h3>
            <p style={{ margin: 0, fontSize: 12, color: s.muted }}>
              {descartando.estado === "confirmado"
                ? "El movimiento del banco vuelve a quedar libre en Tesorería: recién ahí vas a poder desconciliarlo o borrar la cartola. El par no se vuelve a sugerir."
                : "El par queda ocupado: el sistema no vuelve a sugerir este cruce."}
              {descartando.grupo_uuid ? " Se aplica al grupo completo (filas hermanas incluidas)." : ""}
            </p>
            <Field label="Motivo (opcional)">
              <input style={inp} value={descarteMotivo}
                onChange={e => setDescarteMotivo(e.target.value)}
                onKeyDown={e => { if (e.key === "Enter") descartarMatch(); }}
                placeholder="Por qué no corresponde este cruce" maxLength={400} autoFocus />
            </Field>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 8 }}>
              <button onClick={() => { setDescartando(null); setDescarteMotivo(""); }} disabled={actingId !== null}
                style={{ ...btnSecondary(s), fontSize: 12, opacity: actingId !== null ? 0.5 : 1 }}>Cancelar</button>
              <button onClick={descartarMatch} disabled={actingId !== null}
                style={{ ...btnPrimary(), fontSize: 12, opacity: actingId !== null ? 0.5 : 1, cursor: actingId !== null ? "not-allowed" : "pointer" }}>
                {actingId !== null ? <Loader2 size={14} className="animate-spin" /> : <X size={14} />}
                {descartando.estado === "confirmado" ? "Deshacer" : "Descartar"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Bandeja: filtros + tabla paginada */}
      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 10 }}>
          <div style={{ position: "relative", flex: "1 1 240px", minWidth: 220 }}>
            <Search size={15} color={s.faint} style={{ position: "absolute", left: 12, top: "50%", transform: "translateY(-50%)" }} />
            <input style={{ ...inp, padding: "10px 40px 10px 36px", background: s.cardBg }}
              placeholder="Filtrar por RUT del emisor (Enter para aplicar)…"
              value={rutInput}
              onChange={e => setRutInput(e.target.value)}
              onKeyDown={e => { if (e.key === "Enter") aplicarRut(); }} />
            {(rutInput || rutApplied) && (
              <button onClick={limpiarRut} title="Limpiar filtro RUT"
                style={{ position: "absolute", right: 8, top: "50%", transform: "translateY(-50%)", background: "none", border: "none", cursor: "pointer", color: s.muted, padding: 4, display: "flex" }}>
                <X size={15} />
              </button>
            )}
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6 }}>
            {/* H4: en la lista de desaparecidos estos filtros no aplican (todos tienen la
                misma cubeta), así que se ocultan en vez de dejar botones que vacían la
                lista sin explicar nada. */}
            {!verDesaparecidos && [["", "Todas"], ["ESTA", "Está"], ["NO_ESTA", "No está"], ["INDETERMINADO", "Indeterminado"]].map(([v, l]) => (
              <button key={v || "todas-c"} onClick={() => { setCubeta(v); setPage(1); }} style={chip(cubeta === v)}>{l}</button>
            ))}
            {!verDesaparecidos && <span style={{ width: 1, height: 20, background: s.dark ? "#1e2a4a" : "#E2E8F0", margin: "0 4px" }} />}
            {[["", "Todas"], ["pendiente", "Pendiente"], ["ignorado", "Ignorado"], ["clasificado", "Clasificado"]].map(([v, l]) => (
              <button key={v || "todas-d"} onClick={() => { setDecision(v); setPage(1); }} style={chip(decision === v)}>{l}</button>
            ))}
            <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, cursor: "pointer", marginLeft: 4, whiteSpace: "nowrap", color: s.muted }}>
              <input type="checkbox" checked={soloDiv} onChange={e => { setSoloDiv(e.target.checked); setPage(1); }} style={{ accentColor: "var(--monza-accent)" }} />
              Solo divergentes
            </label>
            {/* H4: el chip que faltaba — la única forma de ver la lista que el tablero anuncia. */}
            {(resumen?.desaparecidos ?? 0) > 0 && (
              <button onClick={() => verEstado(verDesaparecidos ? "ACTIVO" : "DESAPARECIDO")}
                title="Documentos que el SII declaraba antes y dejó de declarar"
                style={verDesaparecidos
                  ? { ...chip(false), border: `1px solid ${ROJO}`, background: "rgba(185,28,28,0.12)", color: ROJO }
                  : chip(false)}>
                Desaparecidos del SII ({resumen!.desaparecidos})
              </button>
            )}
          </div>
        </div>

        {verDesaparecidos && (
          <div style={{ borderRadius: 10, border: "1px solid rgba(245,158,11,0.35)", background: "rgba(245,158,11,0.10)", padding: "10px 12px" }}>
            <p style={{ margin: 0, fontSize: 12, fontWeight: 700, color: AMBAR, display: "flex", alignItems: "center", gap: 6 }}>
              <AlertTriangle size={13} style={{ flexShrink: 0 }} /> Documentos que el SII declaraba y ya no declara
            </p>
            <p style={{ margin: "4px 0 0", fontSize: 12, color: s.muted }}>
              Estás viendo solo estos, con la decisión que tenían intacta. Si alguno quedó clasificado
              como costo o como activo fijo, revísalo: el SII ya no lo respalda. Para volver a la lista
              normal, aprieta otra vez «Desaparecidos del SII».
            </p>
          </div>
        )}

        {loadingDocs && <div style={{ display: "flex", justifyContent: "center", padding: 60 }}><Loader2 size={26} className="animate-spin" color="var(--monza-accent)" /></div>}
        {!loadingDocs && docs.length === 0 && (
          <div style={{ borderRadius: 14, border: s.cardBd, background: s.cardBg, padding: 56, textAlign: "center" }}>
            <BookOpenCheck size={40} style={{ margin: "0 auto 12px", display: "block", opacity: 0.2, color: s.muted }} />
            <p style={{ margin: 0, fontSize: 13, fontWeight: 600, color: s.muted }}>
              {verDesaparecidos ? "Ningún documento desaparecido con estos filtros"
                : edadHoras == null ? "El espejo del libro está vacío" : "No hay documentos con estos filtros"}
            </p>
            <p style={{ margin: "4px 0 0", fontSize: 12, color: s.faint }}>
              {verDesaparecidos ? "Aprieta otra vez «Desaparecidos del SII» para volver a la lista normal."
                : edadHoras == null ? 'Usa "Sincronizar ahora" para traer el libro de compras del SII.' : "Prueba quitando algún filtro."}
            </p>
          </div>
        )}
        {!loadingDocs && docs.length > 0 && (
          <div style={{ borderRadius: 14, border: s.cardBd, background: s.cardBg, overflow: "hidden" }}>
            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead>
                  <tr style={{ borderBottom: s.cardBd, background: s.sub }}>
                    {["Fecha", "Tipo", "Folio", "Emisor", "Monto", "Cubeta", "Decisión", "⚠"].map((h, i) => (
                      <th key={h} style={{ ...TH, color: s.faint, textAlign: i === 4 ? "right" : i === 7 ? "center" : "left" }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {docs.map(d => <DocRow key={d.id} doc={d} centros={centros} onChanged={reloadAll} />)}
                </tbody>
              </table>
            </div>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "10px 14px", borderTop: s.cardBd }}>
              <span style={{ fontSize: 11, color: s.faint }}>
                {(page - 1) * PAGE_SIZE + 1}–{Math.min(page * PAGE_SIZE, total)} de {total} documentos
              </span>
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <button onClick={() => setPage(p => Math.max(1, p - 1))} disabled={page <= 1}
                  style={{ ...btnSecondary(s), padding: "5px 10px", fontSize: 12, opacity: page <= 1 ? 0.4 : 1, cursor: page <= 1 ? "not-allowed" : "pointer" }}>
                  <ChevronLeft size={13} /> Anterior
                </button>
                <span style={{ fontSize: 12, padding: "0 6px", color: s.muted }}>{page} / {totalPages}</span>
                <button onClick={() => setPage(p => Math.min(totalPages, p + 1))} disabled={page >= totalPages}
                  style={{ ...btnSecondary(s), padding: "5px 10px", fontSize: 12, opacity: page >= totalPages ? 0.4 : 1, cursor: page >= totalPages ? "not-allowed" : "pointer" }}>
                  Siguiente <ChevronRight size={13} />
                </button>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Reglas por emisor (plegable) */}
      <div style={{ background: s.cardBg, border: s.cardBd, borderRadius: 14 }}>
        <button onClick={() => setReglasOpen(o => !o)} aria-expanded={reglasOpen}
          style={{ width: "100%", display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, padding: "12px 16px", background: "none", border: "none", cursor: "pointer", textAlign: "left", fontFamily: "inherit" }}>
          <span style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, fontWeight: 700, color: s.text }}>
            <Shield size={15} color="var(--monza-accent)" /> Reglas por emisor (RUT)
            <span style={{ fontSize: 12, fontWeight: 400, color: s.faint }}>· {reglas.length} regla{reglas.length === 1 ? "" : "s"}</span>
          </span>
          {reglasOpen ? <ChevronUp size={15} color={s.muted} /> : <ChevronDown size={15} color={s.muted} />}
        </button>
        {reglasOpen && (
          <div style={{ padding: "0 16px 16px", borderTop: s.cardBd, display: "flex", flexDirection: "column", gap: 14, paddingTop: 14 }}>
            <p style={{ margin: 0, fontSize: 12, color: s.muted }}>
              El RUT trae el default, el documento manda: <b style={{ color: ROJO }}>BLOQUEADO</b> jamás capitaliza a un embarque
              (financiero/intercompañía), <b style={{ color: s.muted }}>IGNORAR_AUTO</b> se archiva solo con su centro,
              y <b style={{ color: AZUL }}>LOGISTICO</b> aparece primero y PUEDE capitalizar — el default sigue siendo gasto.
            </p>
            {reglas.length > 0 && (
              <div style={{ borderRadius: 10, border: s.cardBd, overflow: "hidden" }}>
                <div style={{ overflowX: "auto" }}>
                  <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                    <thead>
                      <tr style={{ borderBottom: s.cardBd, background: s.sub }}>
                        {["RUT", "Nivel", "Destino default", "Motivo"].map(h => (
                          <th key={h} style={{ ...TH, padding: "9px 14px", color: s.faint }}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {reglas.map(r => {
                        const nv = NIVEL_PILL[r.nivel] || { bg: "rgba(148,163,184,0.15)", color: GRIS };
                        return (
                          <tr key={r.id} style={{ borderBottom: s.cardBd }}>
                            <td style={{ padding: "9px 14px", fontFamily: "ui-monospace, monospace", whiteSpace: "nowrap", color: s.text }}>{r.rut_formateado || r.rut}</td>
                            <td style={{ padding: "9px 14px", whiteSpace: "nowrap" }}><span style={pill(nv.bg, nv.color)}>{r.nivel}</span></td>
                            <td style={{ padding: "9px 14px", whiteSpace: "nowrap", color: s.muted }}>{destinoLabel(r.destino_default)}</td>
                            <td style={{ padding: "9px 14px", maxWidth: 280, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: s.faint }}>{r.motivo || "—"}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
            {/* Crear / actualizar (el POST canoniza el RUT; una regla por emisor) */}
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 12, alignItems: "end" }}>
              <Field label="RUT del emisor">
                <input style={inp} value={reglaRut} onChange={e => setReglaRut(e.target.value)} placeholder="Ej. 76.513.680-6" />
              </Field>
              <Field label="Nivel">
                <select style={inp} value={reglaNivel} onChange={e => setReglaNivel(e.target.value)}>
                  <option value="BLOQUEADO">BLOQUEADO</option>
                  <option value="IGNORAR_AUTO">IGNORAR_AUTO</option>
                  <option value="LOGISTICO">LOGISTICO</option>
                </select>
              </Field>
              <Field label="Destino default (opcional)">
                <select style={inp} value={reglaDestino} onChange={e => setReglaDestino(e.target.value)}>
                  <option value="">Sin default</option>
                  {opcionesDestino(centros, [reglaDestino]).map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
                </select>
              </Field>
              <Field label="Motivo (opcional)">
                <input style={inp} value={reglaMotivo} onChange={e => setReglaMotivo(e.target.value)} placeholder="Por qué esta regla" maxLength={300} />
              </Field>
              <button onClick={guardarRegla} disabled={reglaSaving}
                style={{ ...btnPrimary(), opacity: reglaSaving ? 0.5 : 1, cursor: reglaSaving ? "not-allowed" : "pointer" }}>
                {reglaSaving ? <Loader2 size={15} className="animate-spin" /> : <Shield size={15} />} Guardar regla
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
