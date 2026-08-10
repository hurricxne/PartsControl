import { useState, useEffect, useCallback, useRef, type ReactNode } from "react";
import { Boxes, Package, RefreshCw, X, CheckCircle, AlertTriangle, Truck, Ship, Search } from "lucide-react";
import { useSearchParams } from "react-router-dom";
import { monzaBodegaAPI } from "../services/monzaApi";
// Cliente axios base (baseURL /api): el historial de recepcionados (Fase 4) se
// llama directo aquí para no tocar monzaApi.ts en paralelo con otros constructores.
import api from "../services/api";
import { useMonzaTheme } from "./MonzaLayout";
import MonzaDocs from "./MonzaDocs";
import toast from "react-hot-toast";

// ─── Buscador de operador (spec buscadores 2026-08-05) ────────────────────────
// Helpers LOCALES de esta página (regla de la casa: helpers por página; el gemelo
// de Despachos duplica la forma a propósito — se comparte el contrato, no el código).

interface MatchInfo { campo: string; valor?: string | null }

const MATCH_LBL: Record<string, string> = {
  numero_parte: "n° parte", numero_parte_sin_guiones: "n° parte (sin guiones)",
  repuesto: "repuesto", marca: "marca", cotizacion: "cotización", oc_cliente: "OC cliente",
  vehiculo: "vehículo", vin: "VIN", cliente: "cliente", ocp: "OCP",
  embarque: "embarque", awb: "AWB", tracking: "tracking", forwarder: "forwarder",
  guia_nacional: "guía prov.",
};
// Si el campo que coincidió NO es columna visible de la tabla, la insignia lleva
// el VALOR (spec, decisión 5): sin él el operador relee la fila entera y agarra
// la caja equivocada.
const MATCH_CON_VALOR = new Set(["marca", "vehiculo", "vin", "awb", "tracking", "forwarder", "ocp"]);

/** minúsculas + sin tildes: espejo cliente de la collation _ci del servidor */
function plegar(s: string): string {
  return s.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
}

/** Normalización espejo del backend: strip + colapsar espacios; <2 chars = sin filtro */
function normalizarQ(s: string): string {
  const t = s.replace(/\s+/g, " ").trim();
  return t.length >= 2 ? t : "";
}

/** true si TODOS los tokens del término aparecen en alguno de los campos (filtro
 *  EN CLIENTE de las pestañas ya cargadas: da el conteo cruzado gratis) */
function coincideLocal(q: string, campos: Array<string | null | undefined>): boolean {
  const toks = plegar(q).split(" ").filter(Boolean).slice(0, 4);
  if (toks.length === 0) return true;
  const planos = campos.filter(Boolean).map((c) => plegar(String(c)));
  return toks.every((t) => planos.some((p) => p.includes(t)));
}

/** ¿el match del backend incluye alguno de estos campos? (decide dónde va el <mark>) */
function campoMatcheado(match: MatchInfo[] | undefined, ...campos: string[]): boolean {
  return !!match?.some((m) => campos.includes(m.campo));
}

/** <mark> SOLO sobre el fragmento que coincidió, partiendo el string en React —
 *  PROHIBIDO dangerouslySetInnerHTML (el repo tiene 0 usos: que siga así).
 *  `todo` resalta el campo COMPLETO (acierto de la pasada colapsada: no se puede
 *  mapear 6003113721 sobre 600-311-3721 carácter a carácter sin mentir). */
function Resaltar({ texto, q, todo }: { texto?: string | null; q: string; todo?: boolean }) {
  const { dark } = useMonzaTheme();
  const markStyle = { background: dark ? "rgba(245,158,11,0.35)" : "#FDE68A", color: "inherit", padding: 0, borderRadius: 2 };
  if (texto === null || texto === undefined || texto === "") return null;
  const t = String(texto);
  if (todo) return <mark style={markStyle}>{t}</mark>;
  // pliegue carácter a carácter para mapear índices al texto original
  const chars = Array.from(t);
  const plano = chars.map((c) => { const f = plegar(c); return f.length === 1 ? f : c.toLowerCase(); }).join("");
  const toks = plegar(q).split(" ").filter(Boolean).slice(0, 4).sort((a, b) => b.length - a.length);
  const rangos: Array<[number, number]> = [];
  for (const tok of toks) {
    let idx = plano.indexOf(tok);
    while (idx !== -1) {
      const fin = idx + tok.length;
      if (!rangos.some(([a, b]) => idx < b && fin > a)) rangos.push([idx, fin]);
      idx = plano.indexOf(tok, idx + 1);
    }
  }
  if (rangos.length === 0) return <>{t}</>;
  rangos.sort((a, b) => a[0] - b[0]);
  const out: ReactNode[] = [];
  let pos = 0;
  rangos.forEach(([a, b], i) => {
    if (a > pos) out.push(chars.slice(pos, a).join(""));
    out.push(<mark key={i} style={markStyle}>{chars.slice(a, b).join("")}</mark>);
    pos = b;
  });
  if (pos < chars.length) out.push(chars.slice(pos).join(""));
  return <>{out}</>;
}

/** Insignia del motivo: máx 2 + "+N" (spec, decisión 5) */
function MatchBadges({ match }: { match?: MatchInfo[] }) {
  const { dark } = useMonzaTheme();
  if (!match || match.length === 0) return null;
  const vis = match.slice(0, 2);
  const resto = match.length - vis.length;
  const st = { fontSize: 10, background: dark ? "rgba(59,130,246,0.2)" : "#DBEAFE", color: dark ? "#93c5fd" : "#1D4ED8", padding: "1px 7px", borderRadius: 8, fontWeight: 700 as const, whiteSpace: "nowrap" as const };
  return (
    <span style={{ display: "inline-flex", gap: 4, flexWrap: "wrap" }}>
      {vis.map((m, i) => (
        <span key={i} style={st}>
          {MATCH_LBL[m.campo] || m.campo}{MATCH_CON_VALOR.has(m.campo) && m.valor ? ` ${m.valor}` : ""}
        </span>
      ))}
      {resto > 0 && <span style={st}>+{resto}</span>}
    </span>
  );
}

interface EmbRecv { id: number; numero: string; estado: string; awb?: string; forwarder?: string; tracking?: string; fecha_llegada_est?: string; items_count: number; recepcion_id?: number; recepcion_abierta?: boolean; }
// ─── Historial de embarques recepcionados (Fase 4 espejo GA) ──────────────────
// El criterio del backend es la RECEPCIÓN cerrada (no el estado del embarque);
// fecha_llegada_est es texto libre en el modelo y se muestra tal cual.
interface EmbHist { id: number; numero: string; estado: string; awb?: string | null; forwarder?: string | null; tracking?: string | null; fecha_llegada_est?: string | null; items_count: number; recepcion?: { id: number; fecha_cierre?: string | null; usuario_email?: string | null } | null; match?: MatchInfo[]; }
interface RecItem { id: number; cot_numero?: string; cliente?: string; descripcion: string; numero_parte?: string; marca?: string; cantidad: number; ocp_proveedor?: string; estado_recepcion?: string; qty_recibida?: number; qty_danada?: number; observacion?: string; fotos?: number; }
interface Recepcion { id: number; embarque_numero?: string; estado: string; total: number; marcados: number; items: RecItem[]; }
// Hallazgo #10: `cantidad` es lo VENDIDO. En una llegada parcial el bodeguero leía
// "10 en bodega" + "6 reclamadas" = 16 unidades sobre una línea de 10, mientras
// Despachos ofrecía 4. El backend ahora manda además lo realmente RECIBIDO
// (qty_recibida, null si el ítem no tiene recepción registrada — dato legado) y el
// cupo aún despachable (qty_disponible, ya descontados borradores y despachos).
// Claves nuevas del buscador (spec 2026-08-05): oc_cliente / embarque / guía
// nacional para VER por qué la fila coincidió + match (insignia, lo calcula el backend)
interface BodegaItem { id: number; cot_numero?: string; cliente?: string; descripcion: string; numero_parte?: string; marca?: string; cantidad: number; ocp_proveedor?: string; qty_recibida?: number | null; qty_disponible?: number; oc_cliente?: string | null; embarque?: string | null; embarque_awb?: string | null; guia_nacional?: string | null; match?: MatchInfo[]; }
interface Reclamo { id: number; cot_numero?: string; descripcion?: string; motivo: string; qty_afectada: number; estado: string; observacion?: string; ocp_proveedor?: string; fecha_creacion?: string; }
interface KPIs { a_recibir: number; en_bodega: number; despachado: number; reclamos_pendientes: number; }

const RECEP_OPTS = [
  { v: "completo", label: "Completo", color: "#15803D" },
  { v: "sobrante", label: "Sobrante", color: "#0369A1" },
  { v: "danado_utilizable", label: "Dañado (utilizable)", color: "#B45309" },
  { v: "faltante", label: "Faltante", color: "#B91C1C" },
  { v: "danado_no_utilizable", label: "Dañado (no utiliz.)", color: "#B91C1C" },
  { v: "no_llego", label: "No llegó", color: "#6D28D9" },
];
const MOTIVO_LBL: Record<string, string> = { faltante: "Faltante", danado_no_utilizable: "Dañado no utiliz.", no_llego: "No llegó", danado_utilizable: "Dañado utiliz." };
const RECLAMO_EST: Record<string, { bg: string; color: string; label: string }> = { pendiente: { bg: "#FEF3C7", color: "#B45309", label: "Pendiente" }, reclamado: { bg: "#DBEAFE", color: "#1D4ED8", label: "Reclamado" }, resuelto: { bg: "#DCFCE7", color: "#15803D", label: "Resuelto" }, anulado: { bg: "#F1F5F9", color: "#64748B", label: "Anulado" } };

function KpiCard({ label, value, accent }: { label: string; value: number; accent: string }) {
  const { dark } = useMonzaTheme();
  return (<div style={{ background: dark ? "#131b3e" : "white", borderRadius: 12, border: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}`, padding: "14px 18px", flex: 1, minWidth: 120 }}><div style={{ width: 24, height: 3, borderRadius: 2, background: accent, marginBottom: 10 }} /><div style={{ fontSize: 24, fontWeight: 800, color: dark ? "white" : "#1E293B", lineHeight: 1 }}>{value}</div><div style={{ fontSize: 11, color: dark ? "#8899cc" : "#64748B", marginTop: 4 }}>{label}</div></div>);
}

// ── Panel de recepción (modal) ────────────────────────────────────────────────
function RecepcionPanel({ recId, onClose, onClosed }: { recId: number; onClose: () => void; onClosed: () => void }) {
  const { dark } = useMonzaTheme();
  const [rec, setRec] = useState<Recepcion | null>(null);
  const [closing, setClosing] = useState(false);
  // Cantidad recibida editable por ítem (llegadas PARCIALES): por defecto la
  // cantidad completa; si llegó menos, se ajusta antes de marcar. Lo recibido queda
  // despachable y solo el faltante real va a reclamo (Fase 2 espejo Grupo AM).
  const [qtys, setQtys] = useState<Record<number, number>>({});
  const bg = dark ? "#131b3e" : "white"; const bd = dark ? "#1e2a4a" : "#E2E8F0"; const txt = dark ? "white" : "#1E293B"; const sub = dark ? "#8899cc" : "#64748B";
  const load = useCallback(async () => { try { const r = await monzaBodegaAPI.getRecepcion(recId); setRec(r.data); } catch { toast.error("Error"); } }, [recId]);
  useEffect(() => { load(); }, [load]);

  const qtyDe = (it: RecItem) => {
    const v = qtys[it.id];
    if (v !== undefined && Number.isFinite(v) && v >= 0) return v;
    return it.qty_recibida ?? it.cantidad;
  };

  const marcar = async (it: RecItem, estado: string) => {
    // 'Faltante' exige decir CUÁNTO llegó: sin ajustar el input, el default (todo lo
    // vendido) inflaría el tope físico sin dejar reclamo. El backend además lo rechaza.
    if (estado === "faltante" && qtys[it.id] === undefined && it.qty_recibida == null) {
      toast.error("Indica cuántas unidades llegaron realmente en el campo Recibido");
      return;
    }
    const qty = estado === "no_llego" ? 0 : qtyDe(it);
    try {
      await monzaBodegaAPI.marcarItem(recId, it.id, { estado_recepcion: estado, qty_recibida: qty, qty_danada: estado.includes("danado") ? it.cantidad : 0 });
      load();
    } catch (e: unknown) {
      const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || "Error al marcar";
      toast.error(msg);
    }
  };
  const cerrar = async () => {
    const pendientes = rec ? rec.total - rec.marcados : 0;
    const forzar = pendientes > 0;
    if (forzar && !window.confirm(`Quedan ${pendientes} ítem(s) sin marcar. Si cierras igual, quedarán como reclamo "no llegó" (trazable). ¿Cerrar la recepción?`)) return;
    setClosing(true);
    try { const r = await monzaBodegaAPI.cerrarRecepcion(recId, forzar); toast.success(`Recepción cerrada · ${r.data.en_bodega} a bodega, ${r.data.reclamos} reclamo(s)`); onClosed(); }
    catch (e: unknown) { toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || "Error al cerrar"); }
    finally { setClosing(false); }
  };

  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)", zIndex: 1000, display: "flex", alignItems: "center", justifyContent: "center", padding: 16 }}>
      <div style={{ background: bg, border: `1px solid ${bd}`, borderRadius: 14, width: "100%", maxWidth: 760, maxHeight: "92vh", display: "flex", flexDirection: "column" }}>
        <div style={{ background: dark ? "#0a0e1f" : "#F8FAFC", borderBottom: `1px solid ${bd}`, padding: "14px 20px", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div><span style={{ fontWeight: 700, fontSize: 15, color: txt, display: "flex", alignItems: "center", gap: 8 }}><Package size={18} className="monza-ic" /> Recepción · {rec?.embarque_numero}</span>
            {rec && <span style={{ fontSize: 12, color: sub }}>{rec.marcados}/{rec.total} ítems marcados</span>}</div>
          <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", color: sub }}><X size={18} /></button>
        </div>
        <div style={{ padding: "14px 20px", overflowY: "auto", flex: 1 }}>
          {!rec ? <div style={{ color: sub }}>Cargando...</div> : rec.items.map((it) => (
            <div key={it.id} style={{ border: `1px solid ${bd}`, borderRadius: 10, padding: "10px 14px", marginBottom: 10 }}>
              <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
                <div><div style={{ fontWeight: 600, fontSize: 13, color: txt }}>{it.descripcion} <span style={{ color: sub, fontWeight: 400 }}>×{it.cantidad}</span></div>
                  <div style={{ fontSize: 11, color: sub }}>{[it.cot_numero, it.numero_parte, it.marca, it.cliente].filter(Boolean).join(" · ")}</div></div>
                {it.estado_recepcion && <CheckCircle size={16} color="#16A34A" />}
              </div>
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 8, alignItems: "center" }}>
                <label style={{ fontSize: 11, color: sub, display: "flex", alignItems: "center", gap: 4 }}>
                  Recibido:
                  <input
                    type="number" min={0} step={1}
                    value={qtyDe(it)}
                    onChange={(e) => setQtys((p) => ({ ...p, [it.id]: Math.max(0, Math.round(Number(e.target.value) || 0)) }))}
                    style={{ width: 58, padding: "3px 6px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 12, background: dark ? "#0d1321" : "#F8FAFC", color: txt, textAlign: "right" }}
                  />
                  <span>/ {it.cantidad}</span>
                </label>
                {RECEP_OPTS.map((o) => (
                  <button key={o.v} onClick={() => marcar(it, o.v)} style={{ padding: "4px 10px", borderRadius: 6, border: `1px solid ${it.estado_recepcion === o.v ? o.color : bd}`, background: it.estado_recepcion === o.v ? `${o.color}18` : "transparent", color: it.estado_recepcion === o.v ? o.color : sub, cursor: "pointer", fontSize: 11, fontWeight: 600 }}>{o.label}</button>
                ))}
              </div>
              {/* Fotos (obligatorias si dañado) */}
              <MonzaDocs entidad="recepcion_item" entidadId={it.id} categorias={["foto", "evidencia"]} titulo={`Fotos${it.estado_recepcion?.includes("danado") ? " (obligatoria)" : ""}`} />
            </div>
          ))}
        </div>
        <div style={{ padding: "12px 20px", borderTop: `1px solid ${bd}`, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <span style={{ fontSize: 12, color: sub }}>{rec ? `${rec.marcados}/${rec.total} marcados` : ""}</span>
          <div style={{ display: "flex", gap: 8 }}>
            <button onClick={onClose} style={{ padding: "8px 18px", border: `1px solid ${bd}`, borderRadius: 8, background: "transparent", color: sub, cursor: "pointer", fontSize: 13 }}>Cerrar ventana</button>
            <button onClick={cerrar} disabled={closing || !rec} style={{ padding: "8px 20px", background: rec && rec.marcados >= rec.total ? "#10B981" : "#F59E0B", border: "none", borderRadius: 8, color: "white", cursor: "pointer", fontWeight: 700, fontSize: 13 }}>{closing ? "Cerrando..." : rec && rec.marcados < rec.total ? "Cerrar con pendientes" : "Cerrar recepción"}</button>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function MonzaBodegaPage() {
  const { dark } = useMonzaTheme();
  // El estado del buscador vive en la URL (?q=&tab=), NO en localStorage (spec,
  // decisión 6): la app se recarga sola bajo los pies del operador (Despachos
  // hace window.location.reload()) y un término que sobrevive del turno de ayer
  // filtrando en silencio la lista de hoy es peor que no persistir nada.
  const [searchParams, setSearchParams] = useSearchParams();
  const tabURL = searchParams.get("tab");
  const [tab, setTab] = useState<"recibir" | "en_bodega" | "reclamos" | "historial">(
    tabURL === "en_bodega" || tabURL === "reclamos" || tabURL === "historial" ? tabURL : "recibir");
  const [q, setQ] = useState(() => searchParams.get("q") || "");
  const [qDeb, setQDeb] = useState(() => normalizarQ(searchParams.get("q") || ""));
  const [embs, setEmbs] = useState<EmbRecv[]>([]);
  const [enBodega, setEnBodega] = useState<BodegaItem[]>([]);
  const [enBodegaTotal, setEnBodegaTotal] = useState(0);
  const [enBodegaNorm, setEnBodegaNorm] = useState(false);
  const [pageBodega, setPageBodega] = useState(1);
  const [reclamos, setReclamos] = useState<Reclamo[]>([]);
  const [hist, setHist] = useState<EmbHist[]>([]);
  const [histTotal, setHistTotal] = useState(0);
  const [histNorm, setHistNorm] = useState(false);
  const [pageHist, setPageHist] = useState(1);
  const [kpis, setKpis] = useState<KPIs | null>(null);
  const [loading, setLoading] = useState(true);
  const [recId, setRecId] = useState<number | null>(null);
  // Guardia de secuencia (id monótono por endpoint): una respuesta fuera de orden
  // se ignora — el clearTimeout del debounce no alcanza con axios crudo.
  const seqBodega = useRef(0);
  const seqHist = useRef(0);

  const bg = dark ? "#131b3e" : "white"; const bd = dark ? "#1e2a4a" : "#E2E8F0"; const txt = dark ? "white" : "#1E293B"; const sub = dark ? "#8899cc" : "#64748B";

  // Debounce 350 ms (spec, decisión 3): este usuario escribe a tirones y con
  // guantes; Enter saltea la espera, Esc limpia (ver onKeyDown del input).
  useEffect(() => { const t = setTimeout(() => setQDeb(normalizarQ(q)), 350); return () => clearTimeout(t); }, [q]);

  // replaceState mientras se escribe: que Atrás no retroceda carácter por carácter
  useEffect(() => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      if (q) next.set("q", q); else next.delete("q");
      return next;
    }, { replace: true });
  }, [q, setSearchParams]);

  // push (no replace) al cambiar de pestaña: Atrás vuelve a la pestaña anterior
  const cambiarTab = (k: typeof tab) => {
    setTab(k);
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      if (k === "recibir") next.delete("tab"); else next.set("tab", k);
      return next;
    });
  };

  const fetchEnBodega = useCallback(async (page: number, append: boolean) => {
    const id = ++seqBodega.current;
    try {
      const r = await monzaBodegaAPI.enBodega({ q: qDeb || undefined, page, page_size: 50 });
      if (id !== seqBodega.current) return;
      const d = r.data;
      if (Array.isArray(d)) {
        // backend previo sin el sobre {items,total,...}: degrada al array pelado
        setEnBodega(d); setEnBodegaTotal(d.length); setEnBodegaNorm(false);
      } else {
        setEnBodega((prev) => (append ? [...prev, ...(d.items || [])] : (d.items || [])));
        setEnBodegaTotal(d.total ?? 0); setEnBodegaNorm(!!d.normalizado);
      }
    } catch { toast.error("Error al cargar bodega"); }
  }, [qDeb]);

  const fetchHist = useCallback(async (page: number, append: boolean) => {
    const id = ++seqHist.current;
    // Historial con catch PROPIO: si el deploy sirve un backend previo sin el
    // endpoint, la pestaña degrada a vacío sin tumbar el resto.
    try {
      const r = await api.get("/monza/bodega/embarques/historial", { params: { q: qDeb || undefined, page, page_size: 50 } });
      if (id !== seqHist.current) return;
      const d = r.data;
      if (Array.isArray(d)) { setHist(d); setHistTotal(d.length); setHistNorm(false); }
      else {
        setHist((prev) => (append ? [...prev, ...(d.items || [])] : (d.items || [])));
        setHistTotal(d.total ?? 0); setHistNorm(!!d.normalizado);
      }
    } catch { /* pestaña opcional */ }
  }, [qDeb]);

  const fetchAll = useCallback(async () => {
    setLoading(true);
    try { const [k, e, r] = await Promise.all([monzaBodegaAPI.kpis(), monzaBodegaAPI.embarques(), monzaBodegaAPI.listReclamos()]);
      setKpis(k.data); setEmbs(e.data); setReclamos(r.data); }
    catch { toast.error("Error al cargar bodega"); } finally { setLoading(false); }
  }, []);
  useEffect(() => { fetchAll(); }, [fetchAll]);
  // En bodega + Historial buscan EN EL SERVIDOR: se recargan cuando cambia el término
  useEffect(() => { setPageBodega(1); setPageHist(1); fetchEnBodega(1, false); fetchHist(1, false); }, [fetchEnBodega, fetchHist]);

  const recargar = () => { fetchAll(); setPageBodega(1); setPageHist(1); fetchEnBodega(1, false); fetchHist(1, false); };

  const recibir = async (e: EmbRecv) => {
    try { const r = await monzaBodegaAPI.recibir(e.id); setRecId(r.data.recepcion_id); } catch { toast.error("Error al abrir recepción"); }
  };
  const resolver = async (r: Reclamo, estado: string) => { try { await monzaBodegaAPI.updateReclamo(r.id, { estado }); toast.success(`Reclamo → ${estado}`); recargar(); } catch { toast.error("Error"); } };

  // "Por recibir" y "Reclamos" filtran EN CLIENTE con el mismo texto sobre lo ya
  // cargado: eso da el conteo cruzado de pestañas gratis (spec, decisión 1).
  const embsF = qDeb ? embs.filter((e) => coincideLocal(qDeb, [e.numero, e.awb, e.forwarder, e.tracking])) : embs;
  const reclamosF = qDeb ? reclamos.filter((r) => coincideLocal(qDeb, [r.cot_numero, r.descripcion, r.ocp_proveedor, r.observacion])) : reclamos;

  const tabs = [
    ["recibir", "Por recibir", qDeb ? embsF.length : embs.length],
    ["en_bodega", "En bodega", enBodegaTotal],
    ["reclamos", "Reclamos", qDeb ? reclamosF.length : reclamos.filter((r) => ["pendiente", "reclamado"].includes(r.estado)).length],
    ["historial", "Historial", histTotal],
  ] as const;

  // Conteo cruzado para el vacío de búsqueda: "Hay 1 coincidencia en En bodega ·
  // 2 en Reclamos", con salto de un clic (la conducta de mayor valor del encargo).
  const otrosConMatch = (actual: typeof tab): Array<[string, number, typeof tab]> => ([
    ["Por recibir", embsF.length, "recibir"],
    ["En bodega", enBodegaTotal, "en_bodega"],
    ["Reclamos", reclamosF.length, "reclamos"],
    ["Historial", histTotal, "historial"],
  ] as Array<[string, number, typeof tab]>).filter(([, n, k]) => k !== actual && n > 0);

  const VacioBusqueda = ({ pestana, etiqueta }: { pestana: typeof tab; etiqueta: string }) => (
    <div style={{ padding: 48, textAlign: "center", color: "#94A3B8" }}>
      <Search size={32} color="#E2E8F0" style={{ display: "block", margin: "0 auto 8px" }} />
      <div>Sin resultados para «{qDeb}» en {etiqueta}.</div>
      {otrosConMatch(pestana).length > 0 && (
        <div style={{ marginTop: 8, fontSize: 12 }}>
          Hay {otrosConMatch(pestana).map(([lbl, n], i) => (
            <span key={lbl}>
              {i > 0 && " · "}
              <button onClick={() => cambiarTab(otrosConMatch(pestana)[i][2])}
                style={{ background: "none", border: "none", cursor: "pointer", color: "var(--monza-accent)", fontWeight: 700, fontSize: 12, padding: 0 }}>
                {n} en {lbl}
              </button>
            </span>
          ))}
        </div>
      )}
    </div>
  );

  // Pie honesto: nunca truncar en silencio (spec, decisión 4)
  const PieLista = ({ mostrando, total, onMas }: { mostrando: number; total: number; onMas: () => void }) => (
    (total > mostrando || (qDeb && total > 0)) ? (
      <div style={{ padding: "10px 16px", borderTop: `1px solid ${bd}`, fontSize: 12, color: sub, display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <span>
          Mostrando {mostrando} de {total} {qDeb ? "coincidencia(s)" : "fila(s)"}
          {total > mostrando ? " — afiná la búsqueda" : ""}
          {total > 200 ? " · Demasiadas coincidencias: agregá el N° de cotización o el cliente." : ""}
        </span>
        {total > mostrando && (
          <button onClick={onMas} style={{ padding: "5px 12px", border: `1px solid ${bd}`, borderRadius: 6, background: "transparent", color: "var(--monza-accent)", cursor: "pointer", fontSize: 12, fontWeight: 700 }}>
            Ver más (+50)
          </button>
        )}
      </div>
    ) : null
  );

  // fecha_cierre llega ISO desde el backend; si viniera basura, se muestra tal
  // cual en vez de "Invalid Date" (defensivo, mismo criterio que fecha_llegada_est)
  const fmtCierre = (s?: string | null) => {
    if (!s) return "—";
    const d = new Date(s);
    return isNaN(d.getTime()) ? s : d.toLocaleString("es-CL", { dateStyle: "short", timeStyle: "short" });
  };

  return (
    <div>
      <div style={{ marginBottom: 18 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}><Boxes size={22} className="monza-ic" /><h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: txt }}>Bodega</h1></div>
        <p style={{ margin: 0, fontSize: 13, color: sub }}>Recepción física de embarques. Marca cada ítem (completo/dañado/faltante) y cierra la recepción.</p>
      </div>

      {kpis && (
        <div style={{ display: "flex", gap: 10, marginBottom: 18, flexWrap: "wrap" }}>
          <KpiCard label="Embarques a recibir" value={kpis.a_recibir} accent="#3B82F6" />
          <KpiCard label="En bodega (listos)" value={kpis.en_bodega} accent="#10B981" />
          <KpiCard label="Despachados" value={kpis.despachado} accent="#6366F1" />
          <KpiCard label="Reclamos pendientes" value={kpis.reclamos_pendientes} accent="#EF4444" />
        </div>
      )}

      {/* Caja ÚNICA arriba de las pestañas (spec, decisión 1): el operador tiene
          UN número en la mano y no sabe clasificarlo — filtra la pestaña activa
          contra el servidor y muestra el conteo en las demás. El placeholder es
          un CONTRATO: cada palabra es un campo que la consulta realmente toca. */}
      <div style={{ position: "relative", marginBottom: 12 }}>
        <Search size={14} style={{ position: "absolute", left: 12, top: "50%", transform: "translateY(-50%)", color: "#94A3B8" }} />
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") setQDeb(normalizarQ(q));     // Enter saltea la espera
            if (e.key === "Escape") { setQ(""); setQDeb(""); }  // Esc limpia
          }}
          placeholder="N° parte, repuesto, COT, OC, embarque o guía…"
          style={{ width: "100%", padding: "9px 34px", border: `1px solid ${bd}`, borderRadius: 8, fontSize: 13, boxSizing: "border-box" as const, background: bg, color: txt }}
        />
        {q && (
          <button onClick={() => { setQ(""); setQDeb(""); }} title="Limpiar búsqueda"
            style={{ position: "absolute", right: 8, top: "50%", transform: "translateY(-50%)", background: "none", border: "none", cursor: "pointer", color: sub, padding: 2, display: "flex" }}>
            <X size={14} />
          </button>
        )}
      </div>
      {/* Si el acierto vino de la pasada colapsada (sin guiones), se dice */}
      {qDeb && (enBodegaNorm || histNorm) && (
        <div style={{ fontSize: 11, color: sub, marginTop: -6, marginBottom: 10 }}>
          Buscaste {qDeb}; también busqué {qDeb.split(" ").map((t) => t.replace(/-/g, "")).join(" ")}.
        </div>
      )}

      <div style={{ display: "flex", gap: 4, marginBottom: 14, borderBottom: `1px solid ${bd}` }}>
        {tabs.map(([k, l, c]) => <button key={k} onClick={() => cambiarTab(k)} style={{ padding: "9px 16px", border: "none", background: "transparent", cursor: "pointer", fontSize: 13, fontWeight: 600, color: tab === k ? "var(--monza-accent)" : sub, borderBottom: `2px solid ${tab === k ? "var(--monza-accent)" : "transparent"}`, marginBottom: -1 }}>{l} <span style={{ fontSize: 11, background: dark ? "#1e2a4a" : "#F1F5F9", padding: "1px 7px", borderRadius: 10, marginLeft: 4 }}>{c}</span></button>)}
      </div>

      <div style={{ background: bg, border: `1px solid ${bd}`, borderRadius: 10, overflow: "hidden" }}>
        {loading ? <div style={{ padding: 40, textAlign: "center", color: "#94A3B8" }}>Cargando...</div>
        : tab === "recibir" ? (
          // Vacíos DIFERENCIADOS (spec, decisión 4): "no hay nada cargado" ≠ "no
          // coincide nada" — el genérico manda al bodeguero a la conclusión cara.
          embs.length === 0 ? <div style={{ padding: 48, textAlign: "center", color: "#94A3B8" }}><Ship size={32} color="#E2E8F0" style={{ display: "block", margin: "0 auto 8px" }} />No hay embarques por recibir.</div>
          : embsF.length === 0 ? <VacioBusqueda pestana="recibir" etiqueta="Por recibir" />
          : <div style={{ padding: 14, display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))", gap: 12 }}>
              {embsF.map((e) => (
                <div key={e.id} style={{ border: `1px solid ${bd}`, borderRadius: 12, padding: 14, borderLeft: "3px solid #3B82F6" }}>
                  <div style={{ fontWeight: 800, fontSize: 15, color: "var(--monza-accent)" }}><Resaltar texto={e.numero} q={qDeb} /></div>
                  <div style={{ fontSize: 12, color: sub, margin: "4px 0 10px" }}>{e.items_count} ítem(s) · AWB {e.awb ? <Resaltar texto={e.awb} q={qDeb} /> : "—"}{e.forwarder ? <> · <Resaltar texto={e.forwarder} q={qDeb} /></> : null}</div>
                  <button onClick={() => recibir(e)} style={{ width: "100%", padding: "8px", background: "var(--monza-accent)", border: "none", borderRadius: 8, color: "white", cursor: "pointer", fontWeight: 700, fontSize: 13, display: "flex", alignItems: "center", justifyContent: "center", gap: 6 }}>
                    <Package size={14} /> {e.recepcion_abierta ? "Continuar recepción" : "Recibir embarque"}
                  </button>
                </div>
              ))}
            </div>
        ) : tab === "en_bodega" ? (
          enBodega.length === 0 ? (
            qDeb ? <VacioBusqueda pestana="en_bodega" etiqueta="En bodega" />
            : <div style={{ padding: 48, textAlign: "center", color: "#94A3B8" }}><Package size={32} color="#E2E8F0" style={{ display: "block", margin: "0 auto 8px" }} />No hay ítems en bodega.</div>
          )
          : <>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead><tr style={{ background: dark ? "#0d1321" : "#F8FAFC", borderBottom: `1px solid ${bd}` }}>{["N° COT", "Cliente", "Repuesto", "Cant.", "OC cliente", "Embarque / Guía", "Proveedor", "Estado"].map((h) => <th key={h} style={{ padding: "10px 12px", textAlign: h === "Cant." ? "right" : "left", fontWeight: 600, fontSize: 11, color: sub, textTransform: "uppercase" as const }}>{h}</th>)}</tr></thead>
              <tbody>{enBodega.map((it) => (
                <tr key={it.id} style={{ borderBottom: `1px solid ${dark ? "#1e2a4a" : "#F1F5F9"}` }}>
                  {/* <mark> SOLO en el campo que coincidió (spec, decisión 5):
                      resaltar los mismos dígitos en toda la fila mata la señal. */}
                  <td style={{ padding: "9px 12px", fontWeight: 600, color: "var(--monza-accent)", fontSize: 12 }}>
                    {campoMatcheado(it.match, "cotizacion") ? <Resaltar texto={it.cot_numero} q={qDeb} /> : it.cot_numero}
                  </td>
                  <td style={{ padding: "9px 12px", color: txt }}>
                    {it.cliente ? (campoMatcheado(it.match, "cliente") ? <Resaltar texto={it.cliente} q={qDeb} /> : it.cliente) : "—"}
                  </td>
                  <td style={{ padding: "9px 12px" }}>
                    <div style={{ color: txt, fontWeight: 500 }}>
                      {campoMatcheado(it.match, "repuesto") ? <Resaltar texto={it.descripcion} q={qDeb} /> : it.descripcion}
                    </div>
                    {it.numero_parte && (
                      <div style={{ fontSize: 10, color: sub }}>
                        {/* acierto de la pasada colapsada → se resalta el campo COMPLETO */}
                        {campoMatcheado(it.match, "numero_parte", "numero_parte_sin_guiones")
                          ? <Resaltar texto={it.numero_parte} q={qDeb} todo={campoMatcheado(it.match, "numero_parte_sin_guiones")} />
                          : it.numero_parte}
                      </div>
                    )}
                    {qDeb && <div style={{ marginTop: 3 }}><MatchBadges match={it.match} /></div>}
                  </td>
                  {/* Hallazgo #10: si llegó MENOS de lo vendido se muestra "4 de 10"
                      (no "10" a secas) y, cuando ya no queda cupo despachable, se
                      avisa debajo para que el bodeguero no contradiga a Despachos. */}
                  <td style={{ padding: "9px 12px", textAlign: "right", color: txt }}>
                    <div>{it.qty_recibida != null && it.qty_recibida < it.cantidad ? `${it.qty_recibida} de ${it.cantidad}` : it.cantidad}</div>
                    {it.qty_disponible != null && it.qty_recibida != null && it.qty_disponible < it.qty_recibida && (
                      <div style={{ fontSize: 10, color: sub }}>{it.qty_disponible === 0 ? "0 despachables" : `${it.qty_disponible} despachables`}</div>
                    )}
                  </td>
                  <td style={{ padding: "9px 12px", color: sub, fontSize: 12 }}>
                    {it.oc_cliente ? (campoMatcheado(it.match, "oc_cliente") ? <Resaltar texto={it.oc_cliente} q={qDeb} /> : it.oc_cliente) : "—"}
                  </td>
                  {/* La compra NACIONAL no pasa por embarques: su identificador es
                      la guía del proveedor y la columna lo dice. */}
                  <td style={{ padding: "9px 12px", color: sub, fontSize: 12 }}>
                    {it.embarque
                      ? (campoMatcheado(it.match, "embarque") ? <Resaltar texto={it.embarque} q={qDeb} /> : it.embarque)
                      : it.guia_nacional
                        ? <>Guía prov. {campoMatcheado(it.match, "guia_nacional") ? <Resaltar texto={it.guia_nacional} q={qDeb} /> : it.guia_nacional} <span style={{ fontSize: 10 }}>(nacional)</span></>
                        : "—"}
                  </td>
                  <td style={{ padding: "9px 12px", color: sub, fontSize: 12 }}>
                    {it.ocp_proveedor ? (campoMatcheado(it.match, "ocp") ? <Resaltar texto={it.ocp_proveedor} q={qDeb} /> : it.ocp_proveedor) : "—"}
                  </td>
                  <td style={{ padding: "9px 12px" }}><span style={{ fontSize: 11, background: "#DCFCE7", color: "#15803D", padding: "3px 10px", borderRadius: 10, fontWeight: 600, display: "inline-flex", alignItems: "center", gap: 4 }}><CheckCircle size={11} /> En bodega</span></td>
                </tr>
              ))}</tbody>
            </table>
            <PieLista mostrando={enBodega.length} total={enBodegaTotal}
              onMas={() => { const p = pageBodega + 1; setPageBodega(p); fetchEnBodega(p, true); }} />
          </>
        ) : tab === "reclamos" ? (
          reclamos.length === 0 ? <div style={{ padding: 48, textAlign: "center", color: "#94A3B8" }}><AlertTriangle size={32} color="#E2E8F0" style={{ display: "block", margin: "0 auto 8px" }} />No hay reclamos.</div>
          : reclamosF.length === 0 ? <VacioBusqueda pestana="reclamos" etiqueta="Reclamos" />
          : <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead><tr style={{ background: dark ? "#0d1321" : "#F8FAFC", borderBottom: `1px solid ${bd}` }}>{["N° COT", "Repuesto", "Motivo", "Qty", "Proveedor", "Estado", "Acciones"].map((h) => <th key={h} style={{ padding: "10px 12px", textAlign: "left", fontWeight: 600, fontSize: 11, color: sub, textTransform: "uppercase" as const }}>{h}</th>)}</tr></thead>
              <tbody>{reclamosF.map((r) => { const es = RECLAMO_EST[r.estado] || RECLAMO_EST.pendiente; return (
                <tr key={r.id} style={{ borderBottom: `1px solid ${dark ? "#1e2a4a" : "#F1F5F9"}` }}>
                  <td style={{ padding: "9px 12px", fontWeight: 600, color: "var(--monza-accent)", fontSize: 12 }}>{r.cot_numero ? <Resaltar texto={r.cot_numero} q={qDeb} /> : "—"}</td>
                  <td style={{ padding: "9px 12px", color: txt }}><Resaltar texto={r.descripcion} q={qDeb} />{r.observacion && <div style={{ fontSize: 10, color: sub }}><Resaltar texto={r.observacion} q={qDeb} /></div>}</td>
                  <td style={{ padding: "9px 12px", color: "#B91C1C", fontSize: 11, fontWeight: 600 }}>{MOTIVO_LBL[r.motivo] || r.motivo}</td>
                  <td style={{ padding: "9px 12px", color: sub }}>{r.qty_afectada}</td>
                  <td style={{ padding: "9px 12px", color: sub, fontSize: 12 }}>{r.ocp_proveedor || "—"}</td>
                  <td style={{ padding: "9px 12px" }}><span style={{ fontSize: 11, background: es.bg, color: es.color, padding: "3px 10px", borderRadius: 10, fontWeight: 600 }}>{es.label}</span></td>
                  <td style={{ padding: "9px 12px" }}>{["pendiente", "reclamado"].includes(r.estado) && <div style={{ display: "flex", gap: 5 }}>
                    {r.estado === "pendiente" && <button onClick={() => resolver(r, "reclamado")} style={{ padding: "4px 9px", border: "1px solid #1D4ED8", borderRadius: 6, background: "transparent", color: "#1D4ED8", cursor: "pointer", fontSize: 11, fontWeight: 600 }}>Reclamar</button>}
                    <button onClick={() => resolver(r, "resuelto")} style={{ padding: "4px 9px", border: "1px solid #15803D", borderRadius: 6, background: "transparent", color: "#15803D", cursor: "pointer", fontSize: 11, fontWeight: 600 }}>Resolver</button>
                  </div>}</td>
                </tr>); })}</tbody>
            </table>
        ) : (
          /* ── Historial: embarques con recepción CERRADA (Fase 4 espejo GA) ── */
          hist.length === 0 ? (
            qDeb ? <VacioBusqueda pestana="historial" etiqueta="Historial" />
            : <div style={{ padding: 48, textAlign: "center", color: "#94A3B8" }}><Ship size={32} color="#E2E8F0" style={{ display: "block", margin: "0 auto 8px" }} />Aún no hay embarques recepcionados.</div>
          )
          : <>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead><tr style={{ background: dark ? "#0d1321" : "#F8FAFC", borderBottom: `1px solid ${bd}` }}>{["N° Embarque", "AWB", "Forwarder", "Llegada est.", "Recepción cerrada", "Recepcionó", "Ítems"].map((h) => <th key={h} style={{ padding: "10px 12px", textAlign: h === "Ítems" ? "right" : "left", fontWeight: 600, fontSize: 11, color: sub, textTransform: "uppercase" as const }}>{h}</th>)}</tr></thead>
              <tbody>{hist.map((e) => (
                <tr key={e.id} style={{ borderBottom: `1px solid ${dark ? "#1e2a4a" : "#F1F5F9"}` }}>
                  <td style={{ padding: "9px 12px", fontWeight: 700, color: "var(--monza-accent)", fontSize: 12 }}>
                    {campoMatcheado(e.match, "embarque") ? <Resaltar texto={e.numero} q={qDeb} /> : e.numero}
                    {e.tracking && <div style={{ fontSize: 10, color: sub, fontWeight: 400 }}>Track {campoMatcheado(e.match, "tracking") ? <Resaltar texto={e.tracking} q={qDeb} /> : e.tracking}</div>}
                    {qDeb && <div style={{ marginTop: 3 }}><MatchBadges match={e.match} /></div>}
                  </td>
                  <td style={{ padding: "9px 12px", color: txt, fontSize: 12 }}>{e.awb ? (campoMatcheado(e.match, "awb") ? <Resaltar texto={e.awb} q={qDeb} /> : e.awb) : "—"}</td>
                  <td style={{ padding: "9px 12px", color: sub, fontSize: 12 }}>{e.forwarder ? (campoMatcheado(e.match, "forwarder") ? <Resaltar texto={e.forwarder} q={qDeb} /> : e.forwarder) : "—"}</td>
                  {/* Texto libre en el modelo (String 30): tal cual, sin parseo */}
                  <td style={{ padding: "9px 12px", color: sub, fontSize: 12 }}>{e.fecha_llegada_est || "—"}</td>
                  <td style={{ padding: "9px 12px", color: txt, fontSize: 12, fontWeight: 600 }}>{fmtCierre(e.recepcion?.fecha_cierre)}</td>
                  <td style={{ padding: "9px 12px", color: sub, fontSize: 12 }}>{e.recepcion?.usuario_email || "—"}</td>
                  <td style={{ padding: "9px 12px", textAlign: "right", color: txt }}>{e.items_count}</td>
                </tr>
              ))}</tbody>
            </table>
            <PieLista mostrando={hist.length} total={histTotal}
              onMas={() => { const p = pageHist + 1; setPageHist(p); fetchHist(p, true); }} />
          </>
        )}
      </div>

      {recId && <RecepcionPanel recId={recId} onClose={() => { setRecId(null); recargar(); }} onClosed={() => { setRecId(null); recargar(); }} />}
    </div>
  );
}
