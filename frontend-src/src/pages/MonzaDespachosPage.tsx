import { useState, useEffect, useCallback, useRef, type ReactNode } from "react";
import { Search, Truck, RefreshCw, ChevronDown, ChevronRight, Upload, Download, FileText, CheckCircle, X, Package, Ship, ClipboardList, Receipt, AlertTriangle, Clock, Send, Loader2, KeyRound } from "lucide-react";
import { useSearchParams } from "react-router-dom";
import { monzaDespachosAPI, monzaCotizacionesAPI, monzaWasabilAPI } from "../services/monzaApi";
import type { MonzaDteInfo } from "../services/monzaApi";
// Confirmación de folio COMPARTIDA con Facturas (DTE 33): una sola implementación de
// la doble digitación para las dos pantallas donde aparece "emitido sin folio".
import MonzaRegistrarFolioModal from "./MonzaRegistrarFolioModal";
// Cliente axios base (baseURL /api): los endpoints nuevos de avance se llaman
// directo aquí para no tocar monzaApi.ts en paralelo con otros constructores.
import api from "../services/api";
import { useMonzaTheme } from "./MonzaLayout";
import toast from "react-hot-toast";

// ─── Buscador de operador (spec buscadores 2026-08-05) ────────────────────────
// Helpers LOCALES de esta página (regla de la casa: helpers por página; el gemelo
// de Bodega duplica la forma a propósito — se comparte el contrato, no el código).

interface MatchInfo { campo: string; valor?: string | null }

const MATCH_LBL: Record<string, string> = {
  numero_parte: "n° parte", numero_parte_sin_guiones: "n° parte (sin guiones)",
  repuesto: "repuesto", marca: "marca", cotizacion: "cotización", oc_cliente: "OC cliente",
  vehiculo: "vehículo", vin: "VIN", cliente: "cliente", rut: "RUT", factura: "factura",
  ocp: "OCP", embarque: "embarque", awb: "AWB", tracking: "tracking", forwarder: "forwarder",
  despacho: "N° despacho", guia: "guía", expedicion: "expedición",
  transportista: "transportista", destinatario: "destinatario",
};
// Si el campo que coincidió NO es columna visible, la insignia lleva el VALOR
// (spec, decisión 5): "embarque EMB-2026-0007" en vez de releer la fila entera.
const MATCH_CON_VALOR = new Set([
  "numero_parte", "numero_parte_sin_guiones", "repuesto", "marca", "oc_cliente", "ocp",
  "embarque", "awb", "tracking", "forwarder", "despacho", "guia", "expedicion",
  "transportista", "destinatario",
]);

/** minúsculas + sin tildes: espejo cliente de la collation _ci del servidor */
function plegar(s: string): string {
  return s.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
}

/** Normalización espejo del backend: strip + colapsar espacios; <2 chars = sin filtro */
function normalizarQ(s: string): string {
  const t = s.replace(/\s+/g, " ").trim();
  return t.length >= 2 ? t : "";
}

/** ¿el match del backend incluye alguno de estos campos? (decide dónde va el <mark>) */
function campoMatcheado(match: MatchInfo[] | undefined, ...campos: string[]): boolean {
  return !!match?.some((m) => campos.includes(m.campo));
}

/** <mark> SOLO sobre el fragmento que coincidió, partiendo el string en React —
 *  PROHIBIDO dangerouslySetInnerHTML (el repo tiene 0 usos: que siga así). */
function Resaltar({ texto, q, todo }: { texto?: string | null; q: string; todo?: boolean }) {
  const { dark } = useMonzaTheme();
  const markStyle = { background: dark ? "rgba(245,158,11,0.35)" : "#FDE68A", color: "inherit", padding: 0, borderRadius: 2 };
  if (texto === null || texto === undefined || texto === "") return null;
  const t = String(texto);
  if (todo) return <mark style={markStyle}>{t}</mark>;
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

// ── Panel "Listos para despachar" (ítems en bodega) ───────────────────────────
interface ListoItem { id: number; descripcion: string; numero_parte?: string; cantidad: number; qty_disponible: number; }
interface ListoCot { id: number; numero: string; cliente?: string; vehiculo?: string; total_items: number; en_bodega: number; listo_completo: boolean; total_bruto: number; items: ListoItem[]; }

function DespacharModal({ cot, onClose, onDone }: { cot: ListoCot; onClose: () => void; onDone: () => void }) {
  const { dark } = useMonzaTheme();
  // El N° de guía NO se pide aquí a propósito (flujo guía-primero, espejo GA
  // G14b): la guía de despacho SII se emite en "Despachos en curso" y su folio
  // lo asigna el SII. Pedirlo acá invitaba a inventar un número que después el
  // folio real pisaría. Ver EmitirGuiaSIIModal y EditarCabeceraModal.
  const [transp, setTransp] = useState(""); const [dest, setDest] = useState(""); const [dir, setDir] = useState(""); const [obs, setObs] = useState(""); const [saving, setSaving] = useState(false);
  // Cantidad a despachar por ítem (parcial permitido): por defecto todo el
  // DISPONIBLE (min(vendido, recibido en bodega) − ya reservado). El backend
  // vuelve a validar el tope bajo lock.
  const [qtys, setQtys] = useState<Record<number, number>>(
    () => Object.fromEntries(cot.items.map((i) => [i.id, i.qty_disponible])),
  );
  const bg = dark ? "#131b3e" : "white"; const bd = dark ? "#1e2a4a" : "#E2E8F0"; const txt = dark ? "white" : "#1E293B"; const sub = dark ? "#8899cc" : "#64748B";
  const IS = { width: "100%", padding: "8px 10px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 13, boxSizing: "border-box" as const, background: dark ? "#0d1321" : "#F8FAFC", color: txt };
  const submit = async () => {
    const items = cot.items
      .map((i) => ({ item_id: i.id, qty: qtys[i.id] ?? i.qty_disponible }))
      .filter((x) => x.qty > 0);
    if (items.length === 0) { toast.error("Indica al menos una cantidad a despachar"); return; }
    setSaving(true);
    try {
      // numero_guia se omite a propósito: el folio del SII lo fija al emitir la guía
      await monzaDespachosAPI.crear({ cotizacion_id: cot.id, items, transportista: transp, destinatario: dest, direccion_entrega: dir, observaciones: obs });
      toast.success(`Despacho en preparación · ${cot.numero} — emite la guía SII y confírmalo en "Despachos en curso"`); onDone();
    } catch (e: unknown) {
      toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || "Error al crear despacho");
    } finally { setSaving(false); }
  };
  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)", zIndex: 1000, display: "flex", alignItems: "center", justifyContent: "center", padding: 16 }}>
      <div style={{ background: bg, border: `1px solid ${bd}`, borderRadius: 14, width: "100%", maxWidth: 500 }}>
        <div style={{ background: dark ? "#0a0e1f" : "#F8FAFC", borderBottom: `1px solid ${bd}`, padding: "14px 20px", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <span style={{ fontWeight: 700, fontSize: 15, color: txt, display: "flex", alignItems: "center", gap: 8 }}><Truck size={18} color="#10B981" /> Despachar {cot.numero}</span>
          <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", color: sub }}><X size={18} /></button>
        </div>
        <div style={{ padding: "16px 20px" }}>
          <div style={{ background: dark ? "#0d1321" : "#F0FDF4", border: `1px solid ${dark ? "#1e2a4a" : "#BBF7D0"}`, borderRadius: 8, padding: "10px 14px", marginBottom: 14, fontSize: 12 }}>
            <div style={{ fontWeight: 700, color: txt }}>{cot.cliente} · {cot.vehiculo || "—"}</div>
            <div style={{ color: sub }}>{cot.en_bodega} ítem(s) con cupo disponible — ajusta cantidades si el despacho es parcial</div>
          </div>
          {/* Cantidades por ítem (despacho parcial): tope = disponible real en bodega */}
          <div style={{ border: `1px solid ${bd}`, borderRadius: 8, marginBottom: 14, maxHeight: 180, overflowY: "auto" }}>
            {cot.items.map((i) => (
              <div key={i.id} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8, padding: "7px 12px", borderBottom: `1px solid ${dark ? "#1e2a4a" : "#F1F5F9"}` }}>
                <div style={{ fontSize: 12, color: txt, minWidth: 0 }}>
                  <div style={{ fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{i.descripcion}</div>
                  {i.numero_parte && <div style={{ fontSize: 10, color: sub }}>{i.numero_parte}</div>}
                </div>
                <label style={{ fontSize: 11, color: sub, display: "flex", alignItems: "center", gap: 4, flexShrink: 0 }}>
                  <input
                    type="number" min={0} max={i.qty_disponible} step={1}
                    value={qtys[i.id] ?? i.qty_disponible}
                    onChange={(e) => setQtys((p) => ({ ...p, [i.id]: Math.max(0, Math.min(i.qty_disponible, Math.round(Number(e.target.value) || 0))) }))}
                    style={{ width: 58, padding: "3px 6px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 12, background: dark ? "#0d1321" : "#F8FAFC", color: txt, textAlign: "right" }}
                  />
                  / {i.qty_disponible} disp.
                </label>
              </div>
            ))}
          </div>
          {/* Guía del flujo guía-primero: paso 1 de 4 (sin campo N° de guía) */}
          <div style={{ display: "flex", gap: 8, alignItems: "flex-start", background: dark ? "#0d1321" : "#EFF6FF", border: `1px solid ${dark ? "#1e2a4a" : "#BFDBFE"}`, borderRadius: 8, padding: "8px 12px", marginBottom: 14, fontSize: 11, color: sub }}>
            <Receipt size={14} color="#3B82F6" style={{ flexShrink: 0, marginTop: 1 }} />
            <span>
              <b style={{ color: txt }}>Paso 1 de 4:</b> elige qué se despacha. Luego, en "Despachos en curso",{" "}
              <b style={{ color: txt }}>emites la guía SII</b> (el N° de guía lo asigna el folio del SII),{" "}
              <b style={{ color: txt }}>completas el transportista</b> y <b style={{ color: txt }}>confirmas</b> la salida.
            </span>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            <div><label style={{ fontSize: 12, fontWeight: 600, color: sub, display: "block", marginBottom: 4 }}>Transportista</label><input value={transp} onChange={(e) => setTransp(e.target.value)} style={IS} /></div>
            <div><label style={{ fontSize: 12, fontWeight: 600, color: sub, display: "block", marginBottom: 4 }}>Destinatario</label><input value={dest} onChange={(e) => setDest(e.target.value)} style={IS} /></div>
            <div><label style={{ fontSize: 12, fontWeight: 600, color: sub, display: "block", marginBottom: 4 }}>Dirección</label><input value={dir} onChange={(e) => setDir(e.target.value)} style={IS} /></div>
            <div style={{ gridColumn: "1 / -1" }}><label style={{ fontSize: 12, fontWeight: 600, color: sub, display: "block", marginBottom: 4 }}>Observaciones</label><textarea value={obs} onChange={(e) => setObs(e.target.value)} rows={2} style={{ ...IS, resize: "vertical" as const }} /></div>
          </div>
        </div>
        <div style={{ padding: "12px 20px", borderTop: `1px solid ${bd}`, display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <button onClick={onClose} style={{ padding: "8px 18px", border: `1px solid ${bd}`, borderRadius: 8, background: "transparent", color: sub, cursor: "pointer", fontSize: 13 }}>Cancelar</button>
          <button onClick={submit} disabled={saving} style={{ padding: "8px 20px", background: "#10B981", border: "none", borderRadius: 8, color: "white", cursor: "pointer", fontWeight: 700, fontSize: 13 }}>{saving ? "Creando..." : "Crear despacho (en preparación)"}</button>
        </div>
      </div>
    </div>
  );
}

function ListosPanel() {
  const { dark } = useMonzaTheme();
  const [listos, setListos] = useState<ListoCot[]>([]);
  const [desp, setDesp] = useState<ListoCot | null>(null);
  const bd = dark ? "#1e2a4a" : "#E2E8F0"; const txt = dark ? "white" : "#1E293B"; const sub = dark ? "#8899cc" : "#64748B"; const bg = dark ? "#131b3e" : "white";
  const load = useCallback(async () => { try { const r = await monzaDespachosAPI.listos(); setListos(r.data); } catch {} }, []);
  useEffect(() => { load(); }, [load]);
  if (listos.length === 0) return null;
  return (
    <div style={{ background: dark ? "#0f1f17" : "#F0FDF4", border: `1px solid ${dark ? "#1e3a2e" : "#BBF7D0"}`, borderRadius: 12, padding: "14px 18px", marginBottom: 20 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
        <Package size={16} color="#15803D" />
        <span style={{ fontWeight: 700, fontSize: 14, color: dark ? "#86efac" : "#15803D" }}>Listos para despachar ({listos.length})</span>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))", gap: 10 }}>
        {listos.map((c) => (
          <div key={c.id} style={{ background: bg, border: `1px solid ${bd}`, borderRadius: 10, padding: 12 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
              <div><div style={{ fontWeight: 700, color: "var(--monza-accent)", fontSize: 13 }}>{c.numero}</div>
                <div style={{ fontSize: 12, color: txt }}>{c.cliente}</div>
                <div style={{ fontSize: 11, color: sub }}>{c.en_bodega}/{c.total_items} en bodega{!c.listo_completo ? " (parcial)" : ""}</div></div>
              <button onClick={() => setDesp(c)} style={{ padding: "5px 12px", background: "#10B981", border: "none", borderRadius: 7, color: "white", cursor: "pointer", fontSize: 12, fontWeight: 700, display: "flex", alignItems: "center", gap: 4 }}><Truck size={12} /> Despachar</button>
            </div>
          </div>
        ))}
      </div>
      {desp && <DespacharModal cot={desp} onClose={() => setDesp(null)} onDone={() => { setDesp(null); load(); window.location.reload(); }} />}
    </div>
  );
}

// ── Panel "Despachos en curso" (entidades con ciclo de vida) ──────────────────
interface DespachoEnt { id: number; numero?: string; cotizacion_numero?: string; cliente_nombre?: string; numero_guia?: string; fecha_guia?: string | null; transportista?: string; numero_expedicion?: string; destinatario?: string; direccion_entrega?: string; observaciones?: string; estado: string; items_count?: number; fecha?: string; fecha_despacho?: string; guia_firmada?: boolean; fecha_firma?: string | null; guia_firmada_archivo?: string | null; }

const DSP_EST: Record<string, { bg: string; color: string; label: string }> = {
  en_preparacion: { bg: "#FEF3C7", color: "#B45309", label: "En preparación" },
  despachado: { bg: "#DCFCE7", color: "#15803D", label: "Despachado" },
  anulado: { bg: "#F1F5F9", color: "#64748B", label: "Anulado" },
};

// Estados de un DTE realmente EN PROCESO (espejo del guard _guia_electronica_activa
// del backend Monza): claim en vuelo o documento vivo en Wasabil sin resultado final.
// OJO: 'no_enviado' (limbo: claim expirado sin respuesta) NO está — el backend
// permite anular/editar en ese caso y el frontend no debe sobre-bloquear.
const DTE_EN_PROCESO = ["enviando", "procesando", "pendiente"];

// Qué pasarle al modal que puede tocar el N° de guía (EditarCabeceraModal):
//   'verificando' = la consulta de DTEs no resolvió CON ÉXITO (bloquear por precaución)
//   folio SII     = guía electrónica emitida (bloqueado: el folio no se edita a mano)
//   'en_emision'  = guía en proceso en el SII, aún sin folio (bloqueado: llegará solo)
//   'sin_folio'   = EMITIDA ante el SII pero su folio nunca llegó (ver abajo)
//   null          = sin guía electrónica (o fallida): el N° manual se puede editar
// FAIL-CLOSED con la semántica isSuccess de GA (NO isFetched): una consulta
// fallida también debe bloquear la edición manual.
//
// 'sin_folio' cierra un desajuste real entre esta pantalla y el backend: una guía
// EMITIDA sin folio no cae en DTE_EN_PROCESO, así que esta función devolvía null, el
// campo quedaba editable… y `_rechazar_si_pisa_folio` rechazaba igual con 409 —
// diciendo además "emisión en curso", que no es lo que pasa—. Ahora se bloquea y se
// dice la verdad, con la salida al lado (el botón «Registrar folio del SII»).
function folioParaModal(dtesListos: boolean, dte?: MonzaDteInfo): string | null {
  if (!dtesListos) return "verificando";
  if (dte?.folio) return dte.folio;
  if (dte?.estado === "emitido") return "sin_folio";
  if (dte && DTE_EN_PROCESO.includes(dte.estado)) return "en_emision";
  return null;
}

function EditarCabeceraModal({ d, dteFolio, onClose, onSaved }: { d: DespachoEnt; dteFolio?: string | null; onClose: () => void; onSaved: () => void }) {
  const { dark } = useMonzaTheme();
  const [guia, setGuia] = useState(d.numero_guia || ""); const [transp, setTransp] = useState(d.transportista || "");
  const [fechaGuia, setFechaGuia] = useState((d.fecha_guia || "").slice(0, 10));
  // Tope del selector: hoy en Chile. Una guía no se emite mañana y el tipeo clásico
  // es el año. El backend valida lo mismo (no confiar sólo en el navegador).
  const hoyChile = new Date().toLocaleDateString("en-CA", { timeZone: "America/Santiago" });
  const [exped, setExped] = useState(d.numero_expedicion || ""); const [dest, setDest] = useState(d.destinatario || "");
  const [dir, setDir] = useState(d.direccion_entrega || ""); const [obs, setObs] = useState(d.observaciones || "");
  const [saving, setSaving] = useState(false);
  const bg = dark ? "#131b3e" : "white"; const bd = dark ? "#1e2a4a" : "#E2E8F0"; const txt = dark ? "white" : "#1E293B"; const sub = dark ? "#8899cc" : "#64748B";
  const IS = { width: "100%", padding: "8px 10px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 13, boxSizing: "border-box" as const, background: dark ? "#0d1321" : "#F8FAFC", color: txt };
  // Candado del folio SII (ver folioParaModal): con guía electrónica emitida o en
  // proceso, el N° es (o será) el folio del SII — cualquier valor no-null bloquea.
  const folioBloqueado = dteFolio !== null && dteFolio !== undefined;
  const save = async () => {
    setSaving(true);
    try {
      await monzaDespachosAPI.updateEntidad(d.id, {
        // Con folio SII (o en duda) el N° de guía no viaja: el backend además lo
        // rechaza con 409 si intenta pisar el folio (_rechazar_si_pisa_folio).
        // La fecha viaja junto al N°: con guía electrónica la pone el propio DTE 52.
        ...(folioBloqueado ? {} : { numero_guia: guia, fecha_guia: fechaGuia || null }),
        transportista: transp, numero_expedicion: exped, destinatario: dest, direccion_entrega: dir, observaciones: obs,
      });
      toast.success("Despacho actualizado"); onSaved();
    } catch (e: unknown) {
      toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || "Error al guardar");
    } finally { setSaving(false); }
  };
  const campos: Array<[string, string, (v: string) => void]> = [
    ["Transportista", transp, setTransp],
    ["N° Expedición (courier)", exped, setExped], ["Destinatario", dest, setDest],
  ];
  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)", zIndex: 1000, display: "flex", alignItems: "center", justifyContent: "center", padding: 16 }}>
      <div style={{ background: bg, border: `1px solid ${bd}`, borderRadius: 14, width: "100%", maxWidth: 480 }}>
        <div style={{ background: dark ? "#0a0e1f" : "#F8FAFC", borderBottom: `1px solid ${bd}`, padding: "14px 20px", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <span style={{ fontWeight: 700, fontSize: 15, color: txt }}>Editar {d.numero || `despacho #${d.id}`}</span>
          <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", color: sub }}><X size={18} /></button>
        </div>
        <div style={{ padding: "16px 20px", display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
          {/* N° Guía con candado del folio SII: si hay guía electrónica emitida o
              en proceso (o la consulta no resolvió), el input queda bloqueado. */}
          <div>
            <label style={{ fontSize: 12, fontWeight: 600, color: sub, display: "block", marginBottom: 4 }}>N° Guía despacho</label>
            <input
              value={folioBloqueado && dteFolio !== "verificando" && dteFolio !== "en_emision"
                && dteFolio !== "sin_folio" ? dteFolio! : guia}
              onChange={(e) => setGuia(e.target.value)}
              disabled={folioBloqueado}
              style={{ ...IS, opacity: folioBloqueado ? 0.6 : 1, cursor: folioBloqueado ? "not-allowed" : undefined }}
            />
            {folioBloqueado && (
              <div style={{ fontSize: 10, color: "#D97706", marginTop: 3 }}>
                {dteFolio === "verificando"
                  ? "Verificando la guía electrónica: no se puede editar el N° hasta confirmar que no hay folio SII."
                  : dteFolio === "en_emision"
                    ? "Guía electrónica en emisión: el N° quedará fijado por el folio del SII."
                    : dteFolio === "sin_folio"
                      ? "El SII ya aceptó esta guía pero su folio no llegó al sistema: no se teclea acá. Ciérra este modal y usa «Registrar folio del SII» en el despacho."
                      : `Guía electrónica emitida al SII (folio ${dteFolio}): el N° no se edita a mano.`}
              </div>
            )}
          </div>
          {/* Fecha de EMISIÓN de la guía ante el SII: sólo aplica a la guía en PAPEL
              (emitida fuera del sistema). Es la fecha que la factura cita en su
              referencia 52; sin ella la emisión de la factura queda bloqueada. */}
          <div>
            <label style={{ fontSize: 12, fontWeight: 600, color: sub, display: "block", marginBottom: 4 }}>Fecha emisión guía</label>
            <input
              type="date"
              max={hoyChile}
              value={fechaGuia}
              onChange={(e) => setFechaGuia(e.target.value)}
              disabled={folioBloqueado}
              style={{ ...IS, opacity: folioBloqueado ? 0.6 : 1, cursor: folioBloqueado ? "not-allowed" : undefined }}
            />
            <div style={{ fontSize: 10, color: folioBloqueado ? "#D97706" : sub, marginTop: 3 }}>
              {folioBloqueado
                ? "Guía electrónica: la fecha la pone el propio documento del SII."
                : "Fecha en que se EMITIÓ la guía en el SII (no la de la firma). Sin ella no se puede facturar al SII."}
            </div>
          </div>
          {campos.map(([lbl, val, set]) => (
            <div key={lbl}><label style={{ fontSize: 12, fontWeight: 600, color: sub, display: "block", marginBottom: 4 }}>{lbl}</label><input value={val} onChange={(e) => set(e.target.value)} style={IS} /></div>
          ))}
          <div style={{ gridColumn: "1 / -1" }}><label style={{ fontSize: 12, fontWeight: 600, color: sub, display: "block", marginBottom: 4 }}>Dirección de entrega</label><input value={dir} onChange={(e) => setDir(e.target.value)} style={IS} /></div>
          <div style={{ gridColumn: "1 / -1" }}><label style={{ fontSize: 12, fontWeight: 600, color: sub, display: "block", marginBottom: 4 }}>Observaciones</label><textarea value={obs} onChange={(e) => setObs(e.target.value)} rows={2} style={{ ...IS, resize: "vertical" as const }} /></div>
        </div>
        <div style={{ padding: "12px 20px", borderTop: `1px solid ${bd}`, display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <button onClick={onClose} style={{ padding: "8px 18px", border: `1px solid ${bd}`, borderRadius: 8, background: "transparent", color: sub, cursor: "pointer", fontSize: 13 }}>Cancelar</button>
          <button onClick={save} disabled={saving} style={{ padding: "8px 20px", background: "var(--monza-accent)", border: "none", borderRadius: 8, color: "white", cursor: "pointer", fontWeight: 700, fontSize: 13 }}>{saving ? "Guardando..." : "Guardar"}</button>
        </div>
      </div>
    </div>
  );
}

// ─── Modal: emitir guía de despacho electrónica (SII 52) vía Wasabil ──────────
// Espejo del EmitirGuiaSIIModal de GA (DespachosPage) con el idioma visual Monza.
// Protocolo de seguridad: PASO 1 previsualización (no toca el SII) → PASO 2 con
// el OK explícito del usuario se emite (IRREVERSIBLE) → sondeo del estado hasta
// Emitido (folio real + PDF) o Fallido (motivo del SII + reintento seguro).
const fmtCLP = (n?: number) => "$" + Math.round(n || 0).toLocaleString("es-CL");
const SONDEO_INTERVALO_MS = 3000; // el envío al SII es asíncrono (segundos a minutos)
const SONDEO_MAX_INTENTOS = 30;   // ~90 s: después pasa a 'pendiente' (seguir más tarde)

type FaseEmision = "cargando" | "preview" | "error_carga" | "emitiendo" | "sondeo" | "exito" | "fallido" | "pendiente";

interface PreviewGuia {
  puede_emitir: boolean;
  problemas?: string[];
  advertencias?: string[];
  receptor?: { razon_social?: string; rut?: string; giro?: string; direccion?: string; comuna?: string; fuente?: string };
  lineas?: Array<{ name: string; description?: string | null; code?: string | null; quantity: number; price: number }>;
  totales?: { neto: number; iva: number; total: number; iva_rate?: number };
  referencias?: Array<{ tipo: string; folio?: string | null; fecha?: string | null; descripcion?: string }>;
  tipos_traslado?: Array<{ codigo: number; label: string }>;
  dte?: MonzaDteInfo | null;
}

function EmitirGuiaSIIModal({ despacho, onClose, onDone }: { despacho: DespachoEnt; onClose: () => void; onDone: () => void }) {
  const { dark } = useMonzaTheme();
  const [fase, setFase] = useState<FaseEmision>("cargando");
  const [preview, setPreview] = useState<PreviewGuia | null>(null);
  const [dte, setDte] = useState<MonzaDteInfo | null>(null);
  const [error, setError] = useState("");
  // Tipo de traslado del SII (dispatchTypeCode): 1 venta por defecto, 5 traslado
  // interno hacia bodega propia, etc. El operador lo elige antes de emitir.
  const [tipoTraslado, setTipoTraslado] = useState(1);

  const bg = dark ? "#131b3e" : "white"; const bd = dark ? "#1e2a4a" : "#E2E8F0";
  const txt = dark ? "white" : "#1E293B"; const sub = dark ? "#8899cc" : "#64748B";

  // Decide la fase según el DTE previo (única fuente: puede_reintentar del backend)
  const faseSegunDte = (d?: MonzaDteInfo | null): FaseEmision => {
    if (!d || d.estado === "no_enviado") return "preview";
    if (d.estado === "emitido") return "exito";
    if (d.puede_reintentar) return "fallido";          // fallido del SII o error de envío
    if (d.uuid) return "sondeo";                       // procesando/pendiente en Wasabil
    return "pendiente";                                // claim en vuelo de otro intento
  };

  // PASO 1: cargar la previsualización (si ya había una emisión previa, retomarla).
  // También se usa para RESINCRONIZAR tras un error al emitir.
  const cargarPreview = () => {
    setFase("cargando");
    monzaWasabilAPI.previewGuia(despacho.id)
      .then(({ data }: { data: PreviewGuia }) => {
        setPreview(data);
        if (data.dte) setDte(data.dte);
        setFase(faseSegunDte(data.dte));
      })
      .catch((e: { response?: { data?: { detail?: string } } }) => {
        setError(e?.response?.data?.detail || "No se pudo cargar la previsualización");
        setFase("error_carga");
      });
  };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(cargarPreview, [despacho.id]);

  // Sondeo: el envío al SII es asíncrono (segundos a minutos)
  useEffect(() => {
    if (fase !== "sondeo") return;
    let vivo = true;
    let intentos = 0;
    const tick = async () => {
      if (!vivo) return;
      intentos += 1;
      try {
        const { data } = await monzaWasabilAPI.estadoGuia(despacho.id);
        if (!vivo) return;
        setDte(data);
        if (data.estado === "emitido") { setFase("exito"); onDone(); return; }
        if (data.puede_reintentar) { setFase("fallido"); return; }
      } catch { /* error transitorio: se reintenta en el próximo tick */ }
      if (intentos >= SONDEO_MAX_INTENTOS) { setFase("pendiente"); return; }
      window.setTimeout(tick, SONDEO_INTERVALO_MS);
    };
    tick();
    return () => { vivo = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fase, despacho.id]);

  // PASO 2: emitir (solo tras ver la previsualización) o reintentar un fallido.
  // tipoTraslado viaja SIEMPRE (también en el reintento: omitirlo revertiría en
  // silencio al default 1 aunque el intento fallido fuera otro tipo).
  const emitir = async (reintento: boolean) => {
    setFase("emitiendo");
    setError("");
    try {
      const { data } = reintento
        ? await monzaWasabilAPI.reintentarGuia(despacho.id, tipoTraslado)
        : await monzaWasabilAPI.emitirGuia(despacho.id, tipoTraslado);
      setDte(data);
      if (data.estado === "emitido") { setFase("exito"); onDone(); }
      else if (data.puede_reintentar) setFase("fallido");
      else setFase("sondeo");
    } catch (e: unknown) {
      // 409/502/timeout: RESINCRONIZAR con el backend en vez de adivinar la fase
      // (el estado real pudo cambiar: claim en curso, fallido, incluso emitido)
      setError((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || "No se pudo emitir la guía");
      cargarPreview();
    }
  };

  const receptor = preview?.receptor;
  const referencia = preview?.referencias?.[0];
  // Tasa de IVA REAL de la venta (iva_pct congelado → config): jamás un 19% fijo
  const ivaLabel = preview?.totales?.iva_rate != null
    ? `IVA ${(preview.totales.iva_rate * 100).toLocaleString("es-CL", { maximumFractionDigits: 1 })}%`
    : "IVA";
  const TH = { padding: "6px 10px", fontWeight: 600, fontSize: 10, color: sub, textTransform: "uppercase" as const, letterSpacing: 0.5 };

  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.65)", zIndex: 1100, display: "flex", alignItems: "center", justifyContent: "center", padding: 16 }} onClick={onClose}>
      <div style={{ background: bg, border: `1px solid ${bd}`, borderRadius: 14, width: "100%", maxWidth: 620, maxHeight: "90vh", display: "flex", flexDirection: "column", overflow: "hidden" }} onClick={(e) => e.stopPropagation()}>
        <div style={{ background: dark ? "#0a0e1f" : "#F8FAFC", borderBottom: `1px solid ${bd}`, padding: "14px 20px", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div>
            <span style={{ fontWeight: 700, fontSize: 15, color: txt, display: "flex", alignItems: "center", gap: 8 }}>
              <Receipt size={18} color="#3B82F6" /> Emitir guía de despacho SII
            </span>
            <span style={{ fontSize: 11, color: sub }}>{despacho.numero || `Despacho #${despacho.id}`} · vía Wasabil (MonzaParts)</span>
          </div>
          <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", color: sub }}><X size={18} /></button>
        </div>

        <div style={{ padding: "16px 20px", overflowY: "auto", display: "flex", flexDirection: "column", gap: 12 }}>
          {fase === "cargando" && (
            <div style={{ padding: "36px 0", textAlign: "center", color: sub, fontSize: 13 }}>
              <Loader2 size={24} className="animate-spin" style={{ margin: "0 auto 8px", display: "block" }} /> Armando la previsualización…
            </div>
          )}

          {(fase === "emitiendo" || fase === "sondeo") && (
            <div style={{ padding: "36px 0", textAlign: "center", color: sub }}>
              <Loader2 size={30} color="#3B82F6" className="animate-spin" style={{ margin: "0 auto 10px", display: "block" }} />
              <div style={{ fontWeight: 700, color: txt, fontSize: 14 }}>
                {fase === "emitiendo" ? "Enviando a Wasabil…" : "Procesando en el SII…"}
              </div>
              <div style={{ fontSize: 11, marginTop: 4 }}>El SII puede tardar de segundos a un par de minutos. No cierres esta ventana.</div>
            </div>
          )}

          {fase === "exito" && dte && (
            <div style={{ padding: "28px 0", textAlign: "center" }}>
              <CheckCircle size={38} color="#10B981" style={{ margin: "0 auto 10px", display: "block" }} />
              <div style={{ fontSize: 16, fontWeight: 800, color: txt }}>Guía emitida — Folio SII {dte.folio}</div>
              <div style={{ fontSize: 11, color: sub, marginTop: 6, maxWidth: 420, marginLeft: "auto", marginRight: "auto" }}>
                El folio quedó registrado en el despacho. Imprime el PDF para que viaje con la carga.
                Cierra esta ventana para agregar el transportista y confirmar el despacho.
              </div>
              {dte.pdf_url && (
                <button onClick={() => window.open(dte.pdf_url!, "_blank", "noopener,noreferrer")}
                  style={{ marginTop: 12, padding: "8px 16px", background: "var(--monza-accent)", border: "none", borderRadius: 8, color: "white", cursor: "pointer", fontWeight: 700, fontSize: 13, display: "inline-flex", alignItems: "center", gap: 6 }}>
                  <FileText size={14} /> Ver PDF de la guía
                </button>
              )}
            </div>
          )}

          {fase === "pendiente" && (
            <div style={{ padding: "28px 0", textAlign: "center" }}>
              <Clock size={30} color="#F59E0B" style={{ margin: "0 auto 8px", display: "block" }} />
              <div style={{ fontWeight: 700, color: txt, fontSize: 14 }}>Emisión en curso</div>
              <div style={{ fontSize: 11, color: sub, marginTop: 6, maxWidth: 420, marginLeft: "auto", marginRight: "auto" }}>
                Hay una emisión en proceso para este despacho (puede ser de otra pestaña u otro usuario).
                Puedes cerrar esta ventana: el estado se actualizará al volver a abrir el despacho y{" "}
                <b style={{ color: txt }}>no se emitirá dos veces</b>.
              </div>
            </div>
          )}

          {fase === "error_carga" && (
            <div style={{ padding: "28px 0", textAlign: "center" }}>
              <AlertTriangle size={30} color="#EF4444" style={{ margin: "0 auto 8px", display: "block" }} />
              <div style={{ fontWeight: 700, color: txt, fontSize: 14 }}>No se pudo cargar la previsualización</div>
              <div style={{ fontSize: 11, color: sub, marginTop: 4 }}>{error}</div>
              <button onClick={cargarPreview} style={{ marginTop: 10, padding: "7px 16px", border: `1px solid ${bd}`, borderRadius: 8, background: "transparent", color: sub, cursor: "pointer", fontSize: 13 }}>Volver a intentar</button>
            </div>
          )}

          {fase === "fallido" && (
            <div style={{ padding: "10px 14px", borderRadius: 10, border: "1px solid rgba(239,68,68,0.35)", background: "rgba(239,68,68,0.10)" }}>
              <div style={{ fontSize: 13, fontWeight: 700, color: "#EF4444", display: "flex", alignItems: "center", gap: 6 }}>
                <AlertTriangle size={14} /> {dte?.estado === "error_envio" ? "La emisión no llegó a Wasabil" : "El SII rechazó la guía"}
              </div>
              <div style={{ fontSize: 11, color: sub, marginTop: 3 }}>{error || dte?.error || "Sin detalle del motivo"}</div>
            </div>
          )}

          {(fase === "preview" || fase === "fallido") && preview && (
            <>
              {(preview.problemas?.length ?? 0) > 0 && fase === "preview" && (
                <div style={{ padding: "10px 14px", borderRadius: 10, border: "1px solid rgba(239,68,68,0.35)", background: "rgba(239,68,68,0.10)" }}>
                  <div style={{ fontSize: 11, fontWeight: 700, color: "#EF4444", marginBottom: 4 }}>Para emitir falta resolver:</div>
                  <ul style={{ margin: 0, paddingLeft: 18, fontSize: 11, color: sub }}>
                    {preview.problemas!.map((p, i) => <li key={i}>{p}</li>)}
                  </ul>
                </div>
              )}
              {(preview.advertencias?.length ?? 0) > 0 && (
                <div style={{ padding: "10px 14px", borderRadius: 10, border: "1px solid rgba(245,158,11,0.35)", background: "rgba(245,158,11,0.10)" }}>
                  <ul style={{ margin: 0, paddingLeft: 18, fontSize: 11, color: sub }}>
                    {preview.advertencias!.map((a, i) => <li key={i}>{a}</li>)}
                  </ul>
                </div>
              )}

              {/* Receptor (ficha real en Wasabil = lo que verá el SII) + referencia 801 */}
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, fontSize: 12 }}>
                <div>
                  <div style={{ fontSize: 10, color: "#94A3B8", textTransform: "uppercase" as const, letterSpacing: 0.5, marginBottom: 3 }}>
                    Receptor {receptor?.fuente === "wasabil" ? "(ficha Wasabil)" : "(datos locales)"}
                  </div>
                  <div style={{ fontWeight: 700, color: txt }}>{receptor?.razon_social || "—"}</div>
                  <div style={{ fontSize: 11, color: sub }}>RUT {receptor?.rut || "—"}</div>
                  {receptor?.giro && <div style={{ fontSize: 11, color: sub }}>{receptor.giro}</div>}
                  {receptor?.direccion && (
                    <div style={{ fontSize: 11, color: sub }}>{receptor.direccion}{receptor.comuna ? `, ${receptor.comuna}` : ""}</div>
                  )}
                </div>
                <div>
                  <div style={{ fontSize: 10, color: "#94A3B8", textTransform: "uppercase" as const, letterSpacing: 0.5, marginBottom: 3 }}>Referencia (SII 801)</div>
                  <div style={{ fontWeight: 700, color: txt }}>OC {referencia?.folio || "—"}</div>
                  <div style={{ fontSize: 11, color: sub }}>Fecha OC: {referencia?.fecha || "—"}</div>
                  <label style={{ display: "block", marginTop: 8 }}>
                    <span style={{ fontSize: 10, color: "#94A3B8", textTransform: "uppercase" as const, letterSpacing: 0.5 }}>Tipo de traslado (SII)</span>
                    <select
                      value={tipoTraslado}
                      onChange={(e) => setTipoTraslado(Number(e.target.value))}
                      disabled={fase !== "preview" && fase !== "fallido"}
                      style={{ width: "100%", marginTop: 2, padding: "5px 8px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 12, background: dark ? "#0d1321" : "#F8FAFC", color: txt }}
                    >
                      {(preview.tipos_traslado ?? [{ codigo: 1, label: "Operación constituye venta" }]).map((t) => (
                        <option key={t.codigo} value={t.codigo}>{t.label}</option>
                      ))}
                    </select>
                  </label>
                </div>
              </div>

              {/* Líneas + totales (neto/IVA/total con la tasa que devuelve el preview) */}
              {(preview.lineas?.length ?? 0) > 0 && (
                <div style={{ border: `1px solid ${bd}`, borderRadius: 10, overflow: "hidden" }}>
                  <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
                    <thead>
                      <tr style={{ background: dark ? "#0d1321" : "#F8FAFC" }}>
                        <th style={{ ...TH, textAlign: "left" as const }}>Ítem</th>
                        <th style={{ ...TH, textAlign: "right" as const }}>Cant.</th>
                        <th style={{ ...TH, textAlign: "right" as const }}>P. unit. neto</th>
                        <th style={{ ...TH, textAlign: "right" as const }}>Total neto</th>
                      </tr>
                    </thead>
                    <tbody>
                      {preview.lineas!.map((ln, i) => (
                        <tr key={i} style={{ borderTop: `1px solid ${dark ? "#1e2a4a" : "#F1F5F9"}` }}>
                          <td style={{ padding: "6px 10px" }}>
                            <span style={{ fontWeight: 700, color: txt, fontFamily: "monospace" }}>{ln.name}</span>
                            {ln.description && <span style={{ display: "block", fontSize: 10, color: "#94A3B8" }}>{ln.description}</span>}
                          </td>
                          <td style={{ padding: "6px 10px", textAlign: "right", color: txt }}>{ln.quantity}</td>
                          <td style={{ padding: "6px 10px", textAlign: "right", color: sub }}>{fmtCLP(ln.price)}</td>
                          <td style={{ padding: "6px 10px", textAlign: "right", fontWeight: 700, color: txt }}>{fmtCLP(ln.price * ln.quantity)}</td>
                        </tr>
                      ))}
                    </tbody>
                    <tfoot style={{ background: dark ? "#0d1321" : "#F8FAFC" }}>
                      <tr style={{ borderTop: `1px solid ${bd}` }}>
                        <td colSpan={3} style={{ ...TH, textAlign: "right" as const }}>Neto</td>
                        <td style={{ padding: "6px 10px", textAlign: "right", fontWeight: 700, color: txt }}>{fmtCLP(preview.totales?.neto)}</td>
                      </tr>
                      <tr>
                        <td colSpan={3} style={{ ...TH, textAlign: "right" as const }}>{ivaLabel}</td>
                        <td style={{ padding: "6px 10px", textAlign: "right", color: sub }}>{fmtCLP(preview.totales?.iva)}</td>
                      </tr>
                      <tr>
                        <td colSpan={3} style={{ ...TH, textAlign: "right" as const }}>Total</td>
                        <td style={{ padding: "6px 10px", textAlign: "right", fontWeight: 800, color: "#3B82F6" }}>{fmtCLP(preview.totales?.total)}</td>
                      </tr>
                    </tfoot>
                  </table>
                </div>
              )}

              {error && fase === "preview" && (
                <div style={{ padding: "10px 14px", borderRadius: 10, border: "1px solid rgba(239,68,68,0.35)", background: "rgba(239,68,68,0.10)", fontSize: 11, color: "#EF4444" }}>{error}</div>
              )}
            </>
          )}
        </div>

        {(fase === "preview" || fase === "fallido") && (
          <div style={{ padding: "12px 20px", borderTop: `1px solid ${bd}`, background: dark ? "#0a0e1f" : "#F8FAFC" }}>
            <div style={{ fontSize: 11, color: sub, display: "flex", alignItems: "center", gap: 6, marginBottom: 8 }}>
              <AlertTriangle size={13} color="#F59E0B" style={{ flexShrink: 0 }} />
              Al confirmar, la guía se emite al SII a través de Wasabil. Esta acción es IRREVERSIBLE.
            </div>
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
              <button onClick={onClose} style={{ padding: "8px 18px", border: `1px solid ${bd}`, borderRadius: 8, background: "transparent", color: sub, cursor: "pointer", fontSize: 13 }}>Cancelar</button>
              {fase === "fallido" ? (
                <button onClick={() => emitir(true)}
                  style={{ padding: "8px 20px", background: "var(--monza-accent)", border: "none", borderRadius: 8, color: "white", cursor: "pointer", fontWeight: 700, fontSize: 13, display: "inline-flex", alignItems: "center", gap: 6 }}>
                  <Send size={14} /> Reintentar emisión
                </button>
              ) : (
                <button onClick={() => emitir(false)} disabled={!preview?.puede_emitir}
                  style={{ padding: "8px 20px", background: "var(--monza-accent)", border: "none", borderRadius: 8, color: "white", cursor: preview?.puede_emitir ? "pointer" : "not-allowed", opacity: preview?.puede_emitir ? 1 : 0.5, fontWeight: 700, fontSize: 13, display: "inline-flex", alignItems: "center", gap: 6 }}>
                  <Send size={14} /> Confirmar y emitir al SII
                </button>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// Filas visibles del panel: borradores y cerrados con la guía SIN FIRMAR siempre
// completos — ambos exigen acción (confirmar / marcar la firma, sin la cual NO se
// factura, regla 2026-08-06) y no pueden desaparecer del único panel que la permite
// (hallazgo HIGH del multienjambre: el corte de 30 escondía justo las guías que
// llevaban semanas esperando la firma). Los firmados recientes rellenan el resto.
// Espejo del criterio del backend (GET /entidades trae completos los dos grupos).
function visiblesDe(list: DespachoEnt[]): DespachoEnt[] {
  const abiertos = list.filter((d) => d.estado === "en_preparacion");
  const sinFirmar = list.filter((d) => d.estado === "despachado" && !d.guia_firmada);
  const firmados = list.filter((d) => d.estado === "despachado" && d.guia_firmada);
  return [...abiertos, ...sinFirmar, ...firmados].slice(0, Math.max(30, abiertos.length + sinFirmar.length));
}

function DespachosEnCursoPanel() {
  const { dark } = useMonzaTheme();
  const [ents, setEnts] = useState<DespachoEnt[]>([]);
  const [editar, setEditar] = useState<DespachoEnt | null>(null);
  const [emitir, setEmitir] = useState<DespachoEnt | null>(null);
  const [folioDe, setFolioDe] = useState<DespachoEnt | null>(null);
  const [firmar, setFirmar] = useState<DespachoEnt | null>(null);
  // Estado de las guías electrónicas (Wasabil) de los despachos visibles — solo
  // BD, en LOTE (jamás una llamada de red por despacho por render). dtesListos es
  // el espejo del isSuccess de GA, FAIL-CLOSED: mientras la consulta no resuelva
  // CON ÉXITO (también si falla), el folio se trata como DESCONOCIDO y
  // folioParaModal devuelve 'verificando' → se bloquea la edición manual.
  const [dtes, setDtes] = useState<Record<number, MonzaDteInfo>>({});
  const [dtesListos, setDtesListos] = useState(false);
  const bd = dark ? "#1e2a4a" : "#E2E8F0"; const txt = dark ? "white" : "#1E293B"; const sub = dark ? "#8899cc" : "#64748B"; const bg = dark ? "#131b3e" : "white";
  const load = useCallback(async () => {
    try {
      const r = await monzaDespachosAPI.entidades();
      const list: DespachoEnt[] = Array.isArray(r.data) ? r.data : [];
      setEnts(list);
      // Mismos despachos que pinta la tabla (ver visiblesDe): solo esos ids viajan
      // al estado-batch (con 0 ids no hay llamada de red).
      const ids = visiblesDe(list).map((d) => d.id);
      if (ids.length === 0) { setDtes({}); setDtesListos(true); return; }
      setDtesListos(false);
      try {
        // EN TANDAS de 200: el endpoint rechaza más de 200 ids por consulta (400).
        // Desde que los cerrados SIN FIRMAR se muestran completos, la lista puede
        // pasar ese tope y la consulta entera caía → fail-closed global: TODOS los
        // badges SII desaparecían y el candado del folio quedaba pegado en
        // "Verificando…". Se parte en tandas en vez de RECORTAR los ids: con ids
        // recortados las filas sobrantes quedarían sin estado y folioParaModal las
        // trataría como "sin guía electrónica" — fail-OPEN sobre el folio del SII,
        // que es peor que no tener candado.
        const CHUNK = 200;
        const acumulado: Record<number, MonzaDteInfo> = {};
        for (let i = 0; i < ids.length; i += CHUNK) {
          const rb = await monzaWasabilAPI.estadoBatch(ids.slice(i, i + CHUNK));
          Object.assign(acumulado, rb.data || {});
        }
        setDtes(acumulado);
        setDtesListos(true);
      } catch { /* fail-closed: sin éxito el candado del folio queda puesto */ }
    } catch { /* panel opcional */ }
  }, []);
  useEffect(() => { load(); }, [load]);

  const confirmar = async (d: DespachoEnt) => {
    if (!window.confirm(`¿Confirmar el despacho ${d.numero || d.id}? La mercadería queda registrada como salida.`)) return;
    try { await monzaDespachosAPI.cerrarEntidad(d.id); toast.success(`Despacho ${d.numero || d.id} confirmado`); load(); window.location.reload(); }
    catch (e: unknown) { toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || "Error al confirmar"); }
  };
  const anular = async (d: DespachoEnt) => {
    // Pre-check con la foto local del DTE (espejo GA): el backend igual rechaza
    // con 409 (guard _guia_electronica_activa), pero acá se explica de inmediato
    // en vez de dejar intentar algo que fallará.
    const dte = dtes[d.id];
    if (dte?.estado === "emitido") {
      toast.error(`Este despacho tiene guía SII EMITIDA (folio ${dte.folio}): anúlala primero en Wasabil y luego anula el despacho.`);
      return;
    }
    if (dte && DTE_EN_PROCESO.includes(dte.estado)) {
      toast.error("Este despacho tiene una emisión de guía SII en curso: espera el resultado antes de anular.");
      return;
    }
    if (!window.confirm(`¿Anular el borrador ${d.numero || d.id}? El cupo vuelve a quedar despachable.`)) return;
    try { await monzaDespachosAPI.anularEntidad(d.id); toast.success(`Despacho ${d.numero || d.id} anulado`); load(); window.location.reload(); }
    catch (e: unknown) { toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || "Error al anular"); }
  };

  const visibles = visiblesDe(ents);
  if (visibles.length === 0) return null;
  return (
    <div style={{ background: bg, border: `1px solid ${bd}`, borderRadius: 12, padding: "14px 18px", marginBottom: 20 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
        <Truck size={16} className="monza-ic" />
        <span style={{ fontWeight: 700, fontSize: 14, color: txt }}>Despachos en curso</span>
        <span style={{ fontSize: 11, color: sub }}>· confirma el borrador para registrar la salida; con la entrega hecha, marca la guía firmada — sin eso no se puede facturar</span>
      </div>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
        <thead><tr style={{ borderBottom: `1px solid ${bd}` }}>{["N°", "Venta", "Cliente", "Ítems", "N° Guía", "Transportista", "Estado", "Guía firmada", "Acciones"].map((h) => <th key={h} style={{ padding: "7px 10px", textAlign: "left", fontWeight: 600, fontSize: 11, color: sub, textTransform: "uppercase" as const }}>{h}</th>)}</tr></thead>
        <tbody>{visibles.map((d) => {
          const es = DSP_EST[d.estado] || DSP_EST.despachado;
          const dte = dtes[d.id];
          return (
          <tr key={d.id} style={{ borderBottom: `1px solid ${dark ? "#1e2a4a" : "#F1F5F9"}` }}>
            <td style={{ padding: "7px 10px", fontWeight: 700, color: "var(--monza-accent)", fontSize: 12 }}>{d.numero || d.id}</td>
            <td style={{ padding: "7px 10px", color: txt, fontSize: 12 }}>{d.cotizacion_numero || "—"}</td>
            <td style={{ padding: "7px 10px", color: txt, fontSize: 12 }}>{d.cliente_nombre || "—"}</td>
            <td style={{ padding: "7px 10px", color: sub }}>{d.items_count ?? "—"}</td>
            {/* N° de guía + su fecha de EMISIÓN. El aviso ámbar marca SÓLO la guía EN
                PAPEL sin fecha. Se exige `dtesListos && !dte` porque al emitir la guía
                electrónica el backend copia el folio del SII a `numero_guia` y deja
                `fecha_guia` vacía a propósito (la fecha la pone el DTE): sin este filtro
                el aviso salía en TODA guía electrónica emitida, mandando al operador a
                un campo que el propio modal deshabilita. `dtesListos` evita además el
                falso positivo mientras la consulta de estados no ha resuelto. */}
            <td style={{ padding: "7px 10px", color: sub, fontSize: 12 }}>
              {d.numero_guia || "—"}
              {d.numero_guia && (d.fecha_guia
                ? <span style={{ marginLeft: 6, fontSize: 11 }}>({d.fecha_guia.slice(0, 10).split("-").reverse().join("-")})</span>
                : (dtesListos && !dte) && <span style={{ marginLeft: 6, fontSize: 11, color: "#D97706", fontWeight: 600 }} title="Falta la fecha de emisión de la guía: la factura electrónica la exige. Cárgala en Editar.">⚠ sin fecha</span>)}
            </td>
            <td style={{ padding: "7px 10px", color: sub, fontSize: 12 }}>{d.transportista || "—"}</td>
            <td style={{ padding: "7px 10px" }}>
              {/* Estado del despacho + badges de la guía electrónica (estado-batch) */}
              <div style={{ display: "flex", gap: 4, flexWrap: "wrap", alignItems: "center" }}>
                <span style={{ fontSize: 11, background: es.bg, color: es.color, padding: "3px 10px", borderRadius: 10, fontWeight: 600 }}>{es.label}</span>
                {dte?.estado === "emitido" && dte.folio && (
                  <span style={{ fontSize: 10, background: "rgba(59,130,246,0.15)", color: "#3B82F6", padding: "2px 8px", borderRadius: 10, fontWeight: 700, display: "inline-flex", alignItems: "center", gap: 3, whiteSpace: "nowrap" }}>
                    <Receipt size={10} /> Guía SII {dte.folio}
                  </span>
                )}
                {/* El callejón: el SII la aceptó y su folio no llegó. Sin este badge la
                    fila decía "Guía SII undefined" y no había forma de salir. */}
                {dte?.estado === "emitido" && !dte.folio && (
                  <span title="El SII aceptó esta guía pero su folio nunca llegó al sistema. Usa «Registrar folio del SII» con el número que aparece en app.wasabil.com. No se puede re-emitir: el documento ya existe."
                    style={{ fontSize: 10, background: "rgba(245,158,11,0.15)", color: "#B45309", padding: "2px 8px", borderRadius: 10, fontWeight: 700, display: "inline-flex", alignItems: "center", gap: 3, whiteSpace: "nowrap" }}>
                    <KeyRound size={10} /> Guía SII sin folio
                  </span>
                )}
                {dte && dte.estado !== "emitido" && (
                  <span style={{ fontSize: 10, background: "rgba(245,158,11,0.15)", color: "#D97706", padding: "2px 8px", borderRadius: 10, fontWeight: 700, display: "inline-flex", alignItems: "center", gap: 3, whiteSpace: "nowrap" }}>
                    <AlertTriangle size={10} /> {dte.puede_reintentar ? "Emisión SII fallida" : "Emisión SII en proceso"}
                  </span>
                )}
              </div>
            </td>
            {/* Guía FIRMADA por el cliente — desde 2026-08-06 es REQUISITO para poder
                facturar este despacho en Contabilidad (el gate vive en el backend;
                acá está la única puerta para marcarla). Solo aplica a confirmados:
                un borrador todavía no entregó nada que el cliente pueda firmar. */}
            <td style={{ padding: "7px 10px" }}>
              {d.estado !== "despachado" ? (
                <span style={{ color: sub, fontSize: 12 }}>—</span>
              ) : d.guia_firmada ? (
                <div style={{ display: "flex", gap: 4, alignItems: "center", flexWrap: "wrap" }}>
                  <span style={{ fontSize: 11, background: "#DCFCE7", color: "#15803D", padding: "3px 10px", borderRadius: 10, fontWeight: 700, display: "inline-flex", alignItems: "center", gap: 4, whiteSpace: "nowrap" }}>
                    <CheckCircle size={11} /> Firmada{d.fecha_firma ? ` · ${d.fecha_firma.slice(0, 10).split("-").reverse().join("-")}` : ""}
                  </span>
                  {d.guia_firmada_archivo && (
                    <button onClick={() => monzaDespachosAPI.abrirGuiaFirmada(d.guia_firmada_archivo!).catch(() => toast.error("No se pudo abrir la guía firmada"))}
                      title="Ver la foto/PDF de la guía firmada"
                      style={{ padding: "3px 7px", background: "rgba(59,130,246,0.10)", border: "none", borderRadius: 6, color: "#3B82F6", cursor: "pointer", fontSize: 10, fontWeight: 600, display: "inline-flex", alignItems: "center", gap: 3 }}>
                      <FileText size={10} /> Ver
                    </button>
                  )}
                  <button onClick={() => setFirmar(d)}
                    title="Corregir la firma (reemplaza la foto/PDF y la fecha)"
                    style={{ padding: "3px 7px", border: `1px solid ${bd}`, borderRadius: 6, background: "transparent", color: sub, cursor: "pointer", fontSize: 10, fontWeight: 600 }}>
                    Corregir
                  </button>
                </div>
              ) : (
                <button onClick={() => setFirmar(d)}
                  title="Subir la foto/PDF de la guía firmada por el cliente y registrar la fecha de la firma. Sin esto, Contabilidad no puede facturar este despacho."
                  style={{ padding: "4px 10px", background: "rgba(16,185,129,0.15)", border: "none", borderRadius: 6, color: "#059669", cursor: "pointer", fontSize: 11, fontWeight: 700, display: "inline-flex", alignItems: "center", gap: 4, whiteSpace: "nowrap" }}>
                  <CheckCircle size={11} /> Marcar guía firmada
                </button>
              )}
            </td>
            <td style={{ padding: "7px 10px" }}>
              <div style={{ display: "flex", gap: 5, flexWrap: "wrap" }}>
                {d.estado === "en_preparacion" && <>
                  {/* Paso 2 del flujo guía-primero: la guía SII se emite ANTES de
                      confirmar. Visible sin DTE o con reintento seguro habilitado. */}
                  {(!dte || dte.puede_reintentar) && (
                    <button onClick={() => setEmitir(d)}
                      title="Emitir la guía de despacho electrónica al SII vía Wasabil"
                      style={{ padding: "4px 10px", background: "rgba(59,130,246,0.15)", border: "none", borderRadius: 6, color: "#3B82F6", cursor: "pointer", fontSize: 11, fontWeight: 700, display: "inline-flex", alignItems: "center", gap: 4 }}>
                      <Receipt size={11} /> {dte ? "Reintentar guía SII" : "Emitir guía SII"}
                    </button>
                  )}
                  <button onClick={() => confirmar(d)} style={{ padding: "4px 10px", background: "#10B981", border: "none", borderRadius: 6, color: "white", cursor: "pointer", fontSize: 11, fontWeight: 700 }}>Confirmar</button>
                  <button onClick={() => anular(d)} style={{ padding: "4px 9px", border: "1px solid #B91C1C", borderRadius: 6, background: "transparent", color: "#B91C1C", cursor: "pointer", fontSize: 11, fontWeight: 600 }}>Anular</button>
                </>}
                {/* SALIDA del callejón "emitida sin folio" (fuera de en_preparacion
                    también: el despacho puede haberse confirmado igual). NO emite. */}
                {dte?.estado === "emitido" && !dte.folio && (
                  <button onClick={() => setFolioDe(d)}
                    title="Registrar el folio que el SII ya asignó a esta guía (se lee en app.wasabil.com)"
                    style={{ padding: "4px 10px", background: "rgba(245,158,11,0.15)", border: "none", borderRadius: 6, color: "#B45309", cursor: "pointer", fontSize: 11, fontWeight: 700, display: "inline-flex", alignItems: "center", gap: 4 }}>
                    <KeyRound size={11} /> Registrar folio del SII
                  </button>
                )}
                {/* Emisión viva sin reintento (visible también fuera de en_preparacion):
                    abre el modal para consultar el estado real en el SII */}
                {dte && dte.estado !== "emitido" && !dte.puede_reintentar && (
                  <button onClick={() => setEmitir(d)}
                    title="La emisión sigue en proceso: consultar el estado real en el SII"
                    style={{ padding: "4px 10px", background: "rgba(245,158,11,0.15)", border: "none", borderRadius: 6, color: "#D97706", cursor: "pointer", fontSize: 11, fontWeight: 700, display: "inline-flex", alignItems: "center", gap: 4 }}>
                    <Clock size={11} /> Estado guía SII
                  </button>
                )}
                {dte?.pdf_url && (
                  <button onClick={() => window.open(dte.pdf_url!, "_blank", "noopener,noreferrer")}
                    title="Ver el PDF de la guía electrónica (SII)"
                    style={{ padding: "4px 9px", background: "rgba(59,130,246,0.10)", border: "none", borderRadius: 6, color: "#3B82F6", cursor: "pointer", fontSize: 11, fontWeight: 600, display: "inline-flex", alignItems: "center", gap: 4 }}>
                    <FileText size={11} /> PDF SII
                  </button>
                )}
                <button onClick={() => setEditar(d)} style={{ padding: "4px 9px", border: `1px solid ${bd}`, borderRadius: 6, background: "transparent", color: sub, cursor: "pointer", fontSize: 11, fontWeight: 600 }}>Editar</button>
              </div>
            </td>
          </tr>); })}</tbody>
      </table>
      {editar && (
        <EditarCabeceraModal
          d={editar}
          // El candado del N° de guía se calcula en vivo contra el batch (fail-closed)
          dteFolio={folioParaModal(dtesListos, dtes[editar.id])}
          onClose={() => setEditar(null)}
          onSaved={() => { setEditar(null); load(); }}
        />
      )}
      {emitir && (
        <EmitirGuiaSIIModal
          despacho={emitir}
          // Re-load SIEMPRE (equivale al invalidate ['despachos','wasabil'] de GA):
          // aunque el usuario cierre a mitad del sondeo, el folio pudo llegar igual.
          onClose={() => { setEmitir(null); load(); }}
          onDone={() => { load(); }}
        />
      )}
      {folioDe && (
        <MonzaRegistrarFolioModal
          sustantivo="guía" referencia={folioDe.numero || `#${folioDe.id}`}
          onClose={() => setFolioDe(null)}
          onRegistrar={async (folio, confirmo) => {
            await monzaWasabilAPI.registrarFolioGuia(folioDe.id, folio, confirmo);
            toast.success(`Folio ${folio} registrado — ya es el N° de guía del despacho`);
            load();
          }} />
      )}
      {firmar && (
        <FirmarGuiaModal
          d={firmar}
          // Mismo candado del N° de guía que EditarCabeceraModal (fail-closed): con
          // guía electrónica el N° es (o será) el folio del SII y no se teclea a mano.
          dteFolio={folioParaModal(dtesListos, dtes[firmar.id])}
          onClose={() => setFirmar(null)}
          onDone={() => { setFirmar(null); load(); }}
        />
      )}
    </div>
  );
}

// ── Modal: marcar guía firmada (foto/PDF + fecha de la firma) ─────────────────
// Espejo del FirmarGuiaModal de GA (DespachosPage.tsx) con el multipart de Monza:
// acá el archivo y la fecha viajan JUNTOS a POST /despachos/entidades/{id}/firmar
// (Monza no tiene upload genérico propio y el de compras de GA exige 'mineria').
// La fecha de la firma es OBLIGATORIA — es el dato que ordena la cobranza después
// (OC + factura + guía firmada) — y el backend la re-valida (formato, no futura,
// no anterior a la emisión de la guía).
function FirmarGuiaModal({ d, dteFolio, onClose, onDone }: { d: DespachoEnt; dteFolio?: string | null; onClose: () => void; onDone: () => void }) {
  const { dark } = useMonzaTheme();
  const [file, setFile] = useState<File | null>(null);
  const [numeroGuia, setNumeroGuia] = useState("");
  // Hoy en CHILE y no toISOString(): en UTC-3/-4 la fecha UTC ya es "mañana" pasadas
  // las ~21:00 y la firma quedaría con fecha futura (el backend la rechazaría).
  const hoyChile = new Date().toLocaleDateString("en-CA", { timeZone: "America/Santiago" });
  const [fecha, setFecha] = useState(hoyChile);
  const [saving, setSaving] = useState(false);
  const bg = dark ? "#131b3e" : "white"; const bd = dark ? "#1e2a4a" : "#E2E8F0"; const txt = dark ? "white" : "#1E293B"; const sub = dark ? "#8899cc" : "#64748B";
  const IS = { width: "100%", padding: "8px 10px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 13, boxSizing: "border-box" as const, background: dark ? "#0d1321" : "#F8FAFC", color: txt };
  // Con guía electrónica emitida/en proceso (o consulta sin resolver), el N° es (o
  // será) el folio del SII: la firma funciona igual, pero sin tocar el N° a mano.
  const folioBloqueado = dteFolio !== null && dteFolio !== undefined;

  const submit = async () => {
    if (!file) { toast.error("Sube la foto o PDF de la guía firmada"); return; }
    if (!fecha) { toast.error("Indica la fecha en que el cliente firmó la guía"); return; }
    setSaving(true);
    try {
      await monzaDespachosAPI.firmarEntidad(d.id, file, fecha, folioBloqueado ? undefined : (numeroGuia.trim() || undefined));
      toast.success("Guía firmada — ya se puede facturar en Contabilidad");
      onDone();
    } catch (e: unknown) {
      toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || "Error al firmar la guía");
    } finally { setSaving(false); }
  };

  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)", zIndex: 1000, display: "flex", alignItems: "center", justifyContent: "center", padding: 16 }}>
      <div style={{ background: bg, border: `1px solid ${bd}`, borderRadius: 14, width: "100%", maxWidth: 460 }}>
        <div style={{ background: dark ? "#0a0e1f" : "#F8FAFC", borderBottom: `1px solid ${bd}`, padding: "14px 20px", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <span style={{ fontWeight: 700, fontSize: 15, color: txt }}>Marcar guía firmada · {d.numero || `#${d.id}`}</span>
          <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", color: sub }}><X size={18} /></button>
        </div>
        <div style={{ padding: "16px 20px", display: "flex", flexDirection: "column", gap: 12 }}>
          <div style={{ fontSize: 12, color: sub, lineHeight: 1.5 }}>
            Sube la <b style={{ color: txt }}>foto o PDF de la guía firmada</b> por el cliente y registra la{" "}
            <b style={{ color: txt }}>fecha de la firma</b>. Sin esta marca, Contabilidad no puede facturar este despacho
            (la cobranza se ordena con OC + factura + guía firmada).
          </div>
          {/* Contexto de la guía: N° y fecha de EMISIÓN (viene de antes: del DTE 52 si
              fue electrónica, o tecleada en Editar si fue en papel). NO se edita acá. */}
          <div style={{ fontSize: 12, color: sub, background: dark ? "#0d1321" : "#F8FAFC", border: `1px solid ${bd}`, borderRadius: 8, padding: "8px 12px" }}>
            Guía: <b style={{ color: txt }}>{d.numero_guia || "sin N° registrado"}</b>
            {d.fecha_guia ? <> · emitida el <b style={{ color: txt }}>{d.fecha_guia.slice(0, 10).split("-").reverse().join("-")}</b></> : null}
            {d.guia_firmada && <span style={{ color: "#D97706" }}> · ya estaba firmada: guardar REEMPLAZA la foto y la fecha</span>}
          </div>
          <div>
            <label style={{ fontSize: 12, fontWeight: 600, color: sub, display: "block", marginBottom: 4 }}>Foto / PDF de la guía firmada</label>
            <label style={{ display: "flex", alignItems: "center", gap: 8, padding: "8px 12px", border: `1px dashed ${bd}`, borderRadius: 8, cursor: "pointer", color: file ? txt : sub, fontSize: 13 }}>
              <Upload size={14} />
              <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{file ? file.name : "Seleccionar archivo…"}</span>
              <input type="file" accept="image/*,application/pdf" onChange={(e) => setFile(e.target.files?.[0] || null)} style={{ display: "none" }} />
            </label>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            <div>
              <label style={{ fontSize: 12, fontWeight: 600, color: sub, display: "block", marginBottom: 4 }}>Fecha de la firma</label>
              {/* min = fecha de emisión de la guía: nadie firma una guía que aún no
                  existía. El backend re-valida lo mismo (no confiar solo en el picker). */}
              <input type="date" min={d.fecha_guia ? d.fecha_guia.slice(0, 10) : undefined} max={hoyChile}
                value={fecha} onChange={(e) => setFecha(e.target.value)} style={IS} />
            </div>
            <div>
              <label style={{ fontSize: 12, fontWeight: 600, color: sub, display: "block", marginBottom: 4 }}>N° Guía (si falta)</label>
              <input
                value={folioBloqueado && dteFolio !== "verificando" && dteFolio !== "en_emision" && dteFolio !== "sin_folio" ? dteFolio! : numeroGuia}
                onChange={(e) => setNumeroGuia(e.target.value)}
                disabled={folioBloqueado}
                placeholder={d.numero_guia || "Opcional"}
                style={{ ...IS, opacity: folioBloqueado ? 0.6 : 1, cursor: folioBloqueado ? "not-allowed" : undefined }}
              />
              {folioBloqueado && (
                <div style={{ fontSize: 10, color: "#D97706", marginTop: 3 }}>
                  {dteFolio === "verificando" ? "Verificando la guía electrónica…"
                    : dteFolio === "en_emision" ? "Guía electrónica en emisión: el N° lo fija el folio del SII."
                      : dteFolio === "sin_folio" ? "Guía SII sin folio en el sistema: regístralo con «Registrar folio del SII»."
                        : `Guía electrónica emitida (folio ${dteFolio}): el N° no se edita a mano.`}
                </div>
              )}
            </div>
          </div>
        </div>
        <div style={{ padding: "12px 20px", borderTop: `1px solid ${bd}`, background: dark ? "#0a0e1f" : "#F8FAFC", display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <button onClick={onClose} style={{ padding: "8px 18px", border: `1px solid ${bd}`, borderRadius: 8, background: "transparent", color: sub, cursor: "pointer", fontSize: 13 }}>Cancelar</button>
          <button onClick={submit} disabled={saving || !file}
            style={{ padding: "8px 20px", background: "#10B981", border: "none", borderRadius: 8, color: "white", cursor: saving || !file ? "not-allowed" : "pointer", opacity: saving || !file ? 0.6 : 1, fontWeight: 700, fontSize: 13, display: "inline-flex", alignItems: "center", gap: 6 }}>
            {saving ? <Loader2 size={14} className="animate-spin" /> : <CheckCircle size={14} />} Confirmar firma
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── Tablero de avance por venta (Fase 4 espejo GA: G15b) ─────────────────────
// Consume GET /api/monza/despachos/avance (tabs listas | en_curso | historial) y
// el detalle /avance/{id}. Es SOLO lectura: la acción de despachar sigue viviendo
// en ListosPanel / DespachosEnCursoPanel — aquí no se duplica ninguna acción.

interface ProgresoEstados { pendiente: number; en_compras: number; en_transito: number; en_bodega: number; despachado: number; reclamo: number; }
interface AvanceCard {
  id: number; numero: string; oc_cliente?: string | null; oc_fecha?: string | null;
  fecha_venta?: string | null; fecha_entrega_est?: string | null;
  cliente?: { nombre: string; rut?: string | null } | null; vehiculo?: string | null;
  total_items: number; items_en_bodega: number; items_despachados: number; items_no_disponibles: number;
  progreso_pct: number; progreso_estados?: ProgresoEstados;
  // Hallazgo #17: 'en_espera_stock' lo devuelve el backend cuando la venta TIENE ítems
  // en bodega pero ninguno con cupo despachable (lo recibido ya se despachó y falta el
  // resto por llegar). Antes ese caso decía "Listo para despacho" y el encargado entraba
  // a una venta sin nada que despachar. La venta NO se oculta de la pestaña «Listas»:
  // esconderla taparía una venta parcialmente en bodega esperando stock, que es peor.
  estado: "listo" | "parcial" | "completado" | "pendiente" | "en_espera_stock";
  // Ítems en bodega que SÍ tienen cupo (null cuando el backend no lo precalculó, p. ej.
  // en el detalle de una venta, donde el card se serializa sin la precarga en lote).
  items_con_cupo?: number | null;
  // Días HÁBILES restantes calculados en backend (feriados chilenos incluidos)
  dias_restantes?: number | null;
  // Insignia del motivo de coincidencia (la calcula el backend sobre la página)
  match?: MatchInfo[];
}
interface AvanceItem { id: number; descripcion: string; numero_parte?: string | null; cantidad: number; estado_linea: string; bucket: keyof ProgresoEstados; qty_despachada: number; qty_disponible: number; }
interface AvanceDespacho { id: number; numero?: string | null; numero_guia?: string | null; transportista?: string | null; estado: string; fecha?: string | null; fecha_despacho?: string | null; items_count?: number; }
interface AvanceEmbarque { id: number; numero: string; estado: string; awb?: string | null; forwarder?: string | null; tracking?: string | null; fecha_llegada_est?: string | null; items_de_esta_venta: number; }
interface AvanceDetalle extends AvanceCard { items?: AvanceItem[]; despachos?: AvanceDespacho[]; embarques?: AvanceEmbarque[]; }
interface AvanceCounts { ventas_listas: number; items_listos: number; items_despachados: number; }

// Colores FIJOS por bucket del pipeline (espejo de PIPELINE_SEGMENTS de GA
// DespachosPage); 'despachado' usa el acento Monza para hablar el idioma de la casa.
const PIPELINE_SEGMENTS: { key: keyof ProgresoEstados; label: string; color: string }[] = [
  { key: "pendiente", label: "Pendiente", color: "#94a3b8" },
  { key: "en_compras", label: "En compras", color: "#f59e0b" },
  { key: "en_transito", label: "En tránsito", color: "#8b5cf6" },
  { key: "en_bodega", label: "En bodega", color: "#10b981" },
  { key: "despachado", label: "Despachado", color: "var(--monza-accent)" },
  { key: "reclamo", label: "Reclamo", color: "#ef4444" },
];
const AVANCE_EST: Record<AvanceCard["estado"], { bg: string; color: string; label: string }> = {
  listo: { bg: "rgba(16,185,129,0.15)", color: "#10B981", label: "Listo para despacho" },
  parcial: { bg: "rgba(245,158,11,0.15)", color: "#D97706", label: "Parcial" },
  completado: { bg: "rgba(59,130,246,0.15)", color: "#3B82F6", label: "Completado" },
  pendiente: { bg: "rgba(100,116,139,0.15)", color: "#64748B", label: "Pendiente bodega" },
  // Hallazgo #17: ámbar (no verde) porque hay mercadería en bodega, pero 0 despachable.
  // Sin esta clave el badge caía al fallback "Pendiente bodega" (:940), que es impreciso:
  // la mercadería SÍ llegó, lo que falta es el resto del pedido.
  en_espera_stock: { bg: "rgba(245,158,11,0.15)", color: "#D97706", label: "En espera de stock" },
};

function DiasRestantesBadge({ dias }: { dias?: number | null }) {
  // Los días llegan HÁBILES desde el backend (vara única, feriados chilenos
  // incluidos); aquí SOLO se pintan — recalcular en cliente con días corridos
  // mentiría (misma inconsistencia que GA arrastra y que no se replica).
  if (dias === null || dias === undefined) return <span style={{ fontSize: 11, color: "#94A3B8" }}>—</span>;
  let bg = "rgba(16,185,129,0.15)", color = "#10B981", label = `${dias}d hábiles`;
  if (dias < 0) { bg = "rgba(239,68,68,0.15)"; color = "#EF4444"; label = `Vencido ${Math.abs(dias)}d`; }
  else if (dias === 0) { bg = "rgba(239,68,68,0.15)"; color = "#EF4444"; label = "Vence HOY"; }
  else if (dias <= 3) { bg = "rgba(239,68,68,0.15)"; color = "#EF4444"; }
  else if (dias <= 7) { bg = "rgba(245,158,11,0.15)"; color = "#D97706"; }
  return <span style={{ fontSize: 11, background: bg, color, padding: "3px 10px", borderRadius: 10, fontWeight: 700, whiteSpace: "nowrap" }}>{label}</span>;
}

function BucketBadge({ bucket }: { bucket: keyof ProgresoEstados }) {
  const seg = PIPELINE_SEGMENTS.find((s) => s.key === bucket) || PIPELINE_SEGMENTS[0];
  return (
    <span style={{ fontSize: 11, color: seg.color, fontWeight: 700, display: "inline-flex", alignItems: "center", gap: 5, whiteSpace: "nowrap" }}>
      <span style={{ width: 8, height: 8, borderRadius: 2, background: seg.color, flexShrink: 0 }} />{seg.label}
    </span>
  );
}

function PipelineProgress({ card }: { card: AvanceCard }) {
  const { dark } = useMonzaTheme();
  const total = card.total_items;
  if (!total) return null;
  const segs = PIPELINE_SEGMENTS
    .map((s) => ({ ...s, count: card.progreso_estados?.[s.key] ?? 0 }))
    .filter((s) => s.count > 0);
  return (
    <div style={{ marginTop: 8 }}>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, marginBottom: 4 }}>
        <span style={{ color: "#94A3B8", textTransform: "uppercase" as const, letterSpacing: 0.5 }}>Avance</span>
        <span style={{ color: card.progreso_pct === 100 ? "#10B981" : (dark ? "#8899cc" : "#64748B"), fontWeight: card.progreso_pct === 100 ? 700 : 400 }}>
          {card.items_despachados}/{total} despachados · {card.progreso_pct}%
        </span>
      </div>
      {/* Barra segmentada: el ancho de cada color = ítems en ese bucket / total */}
      <div style={{ height: 8, borderRadius: 4, overflow: "hidden", display: "flex", background: dark ? "#1e2a4a" : "#E2E8F0" }}>
        {segs.map((s) => (
          <div key={s.key} title={`${s.label}: ${s.count}`} style={{ height: "100%", width: `${(s.count / total) * 100}%`, background: s.color }} />
        ))}
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: "4px 12px", fontSize: 10, color: dark ? "#8899cc" : "#64748B", marginTop: 4 }}>
        {segs.map((s) => (
          <span key={s.key} style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
            <span style={{ width: 8, height: 8, borderRadius: 2, background: s.color, flexShrink: 0 }} />
            {s.label} ({s.count})
          </span>
        ))}
      </div>
    </div>
  );
}

function AvanceDetalleView({ det }: { det: AvanceDetalle }) {
  const { dark } = useMonzaTheme();
  const bd = dark ? "#1e2a4a" : "#E2E8F0"; const txt = dark ? "white" : "#1E293B"; const sub = dark ? "#8899cc" : "#64748B";
  const items = det.items || []; const despachos = det.despachos || []; const embarques = det.embarques || [];
  const TH = { padding: "6px 10px", textAlign: "left" as const, fontWeight: 600, fontSize: 10, color: sub, textTransform: "uppercase" as const };
  return (
    <div style={{ borderTop: `1px solid ${bd}`, padding: "12px 16px", background: dark ? "#0a0e1f" : "#FCFDFF" }}>
      {/* Ítems con su bucket del pipeline + despachado/disponible */}
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
        <thead><tr style={{ borderBottom: `1px solid ${bd}` }}>
          <th style={TH}>Repuesto</th><th style={TH}>Estado</th>
          <th style={{ ...TH, textAlign: "right" as const }}>Despachado</th>
          <th style={{ ...TH, textAlign: "right" as const }}>Disponible</th>
        </tr></thead>
        <tbody>{items.map((it) => (
          <tr key={it.id} style={{ borderBottom: `1px solid ${dark ? "#1e2a4a" : "#F1F5F9"}` }}>
            <td style={{ padding: "6px 10px" }}>
              <div style={{ color: txt, fontWeight: 500 }}>{it.descripcion}</div>
              {it.numero_parte && <div style={{ fontSize: 10, color: sub }}>{it.numero_parte}</div>}
            </td>
            <td style={{ padding: "6px 10px" }}><BucketBadge bucket={it.bucket} /></td>
            <td style={{ padding: "6px 10px", textAlign: "right", color: it.qty_despachada > 0 ? txt : sub, fontWeight: it.qty_despachada > 0 ? 700 : 400 }}>{it.qty_despachada}/{it.cantidad}</td>
            <td style={{ padding: "6px 10px", textAlign: "right", color: it.qty_disponible > 0 ? "#10B981" : sub, fontWeight: it.qty_disponible > 0 ? 700 : 400 }}>{it.qty_disponible}</td>
          </tr>
        ))}</tbody>
      </table>

      {/* Despachos de la venta (borradores y cerrados; la acción vive en el panel de arriba) */}
      {despachos.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div style={{ fontSize: 11, fontWeight: 700, color: sub, textTransform: "uppercase" as const, letterSpacing: 0.5, marginBottom: 6, display: "flex", alignItems: "center", gap: 6 }}><Truck size={12} /> Despachos</div>
          {despachos.map((d) => { const es = DSP_EST[d.estado] || DSP_EST.despachado; return (
            <div key={d.id} style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", fontSize: 12, padding: "5px 0", borderBottom: `1px solid ${dark ? "#1e2a4a" : "#F1F5F9"}` }}>
              <span style={{ fontWeight: 700, color: "var(--monza-accent)" }}>{d.numero || `#${d.id}`}</span>
              <span style={{ color: sub }}>Guía {d.numero_guia || "—"}</span>
              <span style={{ color: sub }}>{d.transportista || "sin transportista"}</span>
              {typeof d.items_count === "number" && <span style={{ color: sub }}>{d.items_count} ítem(s)</span>}
              <span style={{ fontSize: 11, background: es.bg, color: es.color, padding: "2px 9px", borderRadius: 10, fontWeight: 600 }}>{es.label}</span>
              <span style={{ color: sub, fontSize: 11 }}>{d.fecha_despacho ? new Date(d.fecha_despacho).toLocaleDateString("es-CL") : d.fecha ? new Date(d.fecha).toLocaleDateString("es-CL") : ""}</span>
            </div>
          ); })}
        </div>
      )}

      {/* Embarques que trajeron ítems de ESTA venta (items_de_esta_venta del backend) */}
      {embarques.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div style={{ fontSize: 11, fontWeight: 700, color: sub, textTransform: "uppercase" as const, letterSpacing: 0.5, marginBottom: 6, display: "flex", alignItems: "center", gap: 6 }}><Ship size={12} /> Embarques de esta venta</div>
          {embarques.map((e) => (
            <div key={e.id} style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", fontSize: 12, padding: "5px 0", borderBottom: `1px solid ${dark ? "#1e2a4a" : "#F1F5F9"}` }}>
              <span style={{ fontWeight: 700, color: "var(--monza-accent)" }}>{e.numero}</span>
              <span style={{ color: sub }}>AWB {e.awb || "—"}</span>
              {e.forwarder && <span style={{ color: sub }}>{e.forwarder}</span>}
              {/* fecha_llegada_est es texto libre en el modelo: se muestra tal cual */}
              {e.fecha_llegada_est && <span style={{ color: sub, fontSize: 11 }}>Llegada est. {e.fecha_llegada_est}</span>}
              <span style={{ fontSize: 11, background: dark ? "#1e2a4a" : "#F1F5F9", color: sub, padding: "2px 9px", borderRadius: 10, fontWeight: 600 }}>{e.estado}</span>
              <span style={{ color: txt, fontWeight: 600, fontSize: 11 }}>{e.items_de_esta_venta} ítem(s) de esta venta</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function AvanceVentasPanel() {
  const { dark } = useMonzaTheme();
  // El estado vive en la URL (?tab= y ?aq= de este panel; el ?q= es del
  // histórico): la página se recarga sola (window.location.reload tras crear un
  // despacho) y sin URL se perdían la pestaña y el término (spec, decisión 6).
  const [searchParams, setSearchParams] = useSearchParams();
  const tabURL = searchParams.get("tab");
  const [tab, setTab] = useState<"listas" | "en_curso" | "historial">(
    tabURL === "en_curso" || tabURL === "historial" ? tabURL : "listas");
  const [q, setQ] = useState(() => searchParams.get("aq") || "");
  const [qDeb, setQDeb] = useState(() => normalizarQ(searchParams.get("aq") || ""));
  const [cards, setCards] = useState<AvanceCard[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  // Conteo cruzado de pestañas (viene en el sobre del backend): "dónde está esto"
  const [tabsCount, setTabsCount] = useState<{ listas: number; en_curso: number; historial: number } | null>(null);
  const [normalizado, setNormalizado] = useState(false);
  const [counts, setCounts] = useState<AvanceCounts | null>(null);
  const [loading, setLoading] = useState(true);
  const [expandido, setExpandido] = useState<number | null>(null);
  const [detalles, setDetalles] = useState<Record<number, AvanceDetalle>>({});
  const [loadingDet, setLoadingDet] = useState<number | null>(null);
  // Guardia de secuencia (id monótono): una respuesta fuera de orden se ignora
  const seqRef = useRef(0);
  const bg = dark ? "#131b3e" : "white"; const bd = dark ? "#1e2a4a" : "#E2E8F0"; const txt = dark ? "white" : "#1E293B"; const sub = dark ? "#8899cc" : "#64748B";

  // Debounce del buscador: el filtro corre en el BACKEND. 350 ms (antes 300: se
  // sube por consistencia con todas las cajas del encargo — spec, decisión 3).
  useEffect(() => { const t = setTimeout(() => setQDeb(normalizarQ(q)), 350); return () => clearTimeout(t); }, [q]);
  // replaceState mientras se escribe (que Atrás no retroceda carácter por carácter)
  useEffect(() => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      if (q) next.set("aq", q); else next.delete("aq");
      return next;
    }, { replace: true });
  }, [q, setSearchParams]);
  // push (no replace) al cambiar de pestaña
  const cambiarTab = (k: typeof tab) => {
    setTab(k); setExpandido(null);
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      if (k === "listas") next.delete("tab"); else next.set("tab", k);
      return next;
    });
  };

  const load = useCallback(async (pageN: number, append: boolean) => {
    const id = ++seqRef.current;
    if (!append) setLoading(true);
    try {
      const r = await api.get("/monza/despachos/avance", { params: { tab, q: qDeb || undefined, page: pageN, page_size: 50 } });
      if (id !== seqRef.current) return;
      const d = r.data;
      if (Array.isArray(d)) {
        // backend previo sin el sobre {items,total,...}: degrada al array pelado
        setCards(d); setTotal(d.length); setTabsCount(null); setNormalizado(false);
      } else {
        setCards((prev) => (append ? [...prev, ...(d.items || [])] : (d.items || [])));
        setTotal(d.total ?? 0); setTabsCount(d.tabs || null); setNormalizado(!!d.normalizado);
      }
    } catch { /* backend previo sin el endpoint: el tablero degrada a vacío sin tumbar la página */ }
    finally { if (id === seqRef.current) setLoading(false); }
  }, [tab, qDeb]);
  useEffect(() => { setPage(1); load(1, false); }, [load]);
  // Los KPI quedan FUERA del camino del buscador (spec): son 4 agregaciones que
  // ni dependen del texto — antes cada tecla disparaba también esta consulta.
  useEffect(() => { api.get("/monza/despachos/counts").then((r) => setCounts(r.data)).catch(() => {}); }, []);

  const toggle = async (id: number) => {
    if (expandido === id) { setExpandido(null); return; }
    setExpandido(id);
    // Detalle LAZY y cacheado: re-expandir no vuelve a pegarle al backend
    if (!detalles[id]) {
      setLoadingDet(id);
      try { const r = await api.get(`/monza/despachos/avance/${id}`); setDetalles((p) => ({ ...p, [id]: r.data })); }
      catch { toast.error("Error al cargar el detalle de avance"); }
      finally { setLoadingDet(null); }
    }
  };

  const TABS: Array<[typeof tab, string]> = [["listas", "Listas"], ["en_curso", "En curso"], ["historial", "Historial"]];
  const TAB_LBL: Record<typeof tab, string> = { listas: "Listas", en_curso: "En curso", historial: "Historial" };
  const otros = TABS.filter(([k]) => k !== tab && qDeb && (tabsCount?.[k] ?? 0) > 0);
  return (
    <div style={{ background: bg, border: `1px solid ${bd}`, borderRadius: 12, padding: "14px 18px", marginBottom: 20 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10, flexWrap: "wrap" }}>
        <ClipboardList size={16} className="monza-ic" />
        <span style={{ fontWeight: 700, fontSize: 14, color: txt }}>Avance por venta</span>
        {counts && (
          <span style={{ fontSize: 11, color: sub }}>
            · {counts.ventas_listas} venta(s) con ítems en bodega · {counts.items_listos} ítem(s) listos · {counts.items_despachados} despachados
          </span>
        )}
      </div>
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", marginBottom: 12 }}>
        <div style={{ display: "flex", gap: 4, borderBottom: `1px solid ${bd}` }}>
          {TABS.map(([k, l]) => (
            <button key={k} onClick={() => cambiarTab(k)}
              style={{ padding: "7px 14px", border: "none", background: "transparent", cursor: "pointer", fontSize: 13, fontWeight: 600, color: tab === k ? "var(--monza-accent)" : sub, borderBottom: `2px solid ${tab === k ? "var(--monza-accent)" : "transparent"}`, marginBottom: -1 }}>
              {l}{qDeb && tabsCount ? ` (${tabsCount[k]})` : ""}
            </button>
          ))}
        </div>
        <div style={{ position: "relative", flex: 1, minWidth: 220 }}>
          <Search size={13} style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)", color: "#94A3B8" }} />
          {/* El placeholder es un CONTRATO: cada palabra es un campo que la
              consulta toca (spec, decisión 7). */}
          <input value={q} onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") setQDeb(normalizarQ(q));     // Enter saltea la espera
              if (e.key === "Escape") { setQ(""); setQDeb(""); }  // Esc limpia
            }}
            placeholder="N° parte, repuesto, COT, OC, cliente, embarque, N° despacho o guía…"
            style={{ width: "100%", padding: "7px 28px 7px 30px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 12, boxSizing: "border-box" as const, background: dark ? "#0d1321" : "white", color: txt }} />
          {q && (
            <button onClick={() => { setQ(""); setQDeb(""); }} title="Limpiar búsqueda"
              style={{ position: "absolute", right: 6, top: "50%", transform: "translateY(-50%)", background: "none", border: "none", cursor: "pointer", color: sub, padding: 2, display: "flex" }}>
              <X size={13} />
            </button>
          )}
        </div>
      </div>
      {qDeb && normalizado && (
        <div style={{ fontSize: 11, color: sub, marginTop: -6, marginBottom: 10 }}>
          Buscaste {qDeb}; también busqué {qDeb.split(" ").map((t) => t.replace(/-/g, "")).join(" ")}.
        </div>
      )}
      {loading ? <div style={{ padding: 24, textAlign: "center", color: "#94A3B8", fontSize: 13 }}>Cargando avance...</div>
      : cards.length === 0 ? (
        <div style={{ padding: 24, textAlign: "center", color: "#94A3B8", fontSize: 13 }}>
          {/* Vacíos DIFERENCIADOS + conteo cruzado con salto de un clic (spec, dec. 4) */}
          {qDeb ? (
            <>
              <div>Sin resultados para «{qDeb}» en {TAB_LBL[tab]}.</div>
              {otros.length > 0 && (
                <div style={{ marginTop: 8, fontSize: 12 }}>
                  Hay {otros.map(([k, l], i) => (
                    <span key={k}>
                      {i > 0 && " · "}
                      <button onClick={() => cambiarTab(k)}
                        style={{ background: "none", border: "none", cursor: "pointer", color: "var(--monza-accent)", fontWeight: 700, fontSize: 12, padding: 0 }}>
                        {tabsCount?.[k] ?? 0} en {l}
                      </button>
                    </span>
                  ))}
                </div>
              )}
            </>
          ) : tab === "listas" ? "No hay ventas con ítems en bodega." : tab === "en_curso" ? "No hay ventas con despachos en preparación." : "No hay ventas con despachos cerrados."}
        </div>
      ) : cards.map((c) => {
        const est = AVANCE_EST[c.estado] || AVANCE_EST.pendiente;
        // Cobertura parcial (spec «Listo · 3 de 7»): que se vea que SE PUEDE
        // despachar pero NO es la orden completa. items_con_cupo ya viaja desde
        // el backend (cupos en lote, hallazgo #17) — con cupo completo o sin el
        // dato precalculado, la etiqueta clásica se conserva.
        const estLabel = c.estado === "listo" && c.items_con_cupo != null && c.total_items > 0 && c.items_con_cupo < c.total_items
          ? `Listo · ${c.items_con_cupo} de ${c.total_items} ítems`
          : est.label;
        const abierto = expandido === c.id;
        const det = detalles[c.id];
        return (
          <div key={c.id} style={{ border: `1px solid ${bd}`, borderRadius: 10, marginBottom: 8, overflow: "hidden" }}>
            <div onClick={() => toggle(c.id)} style={{ padding: "10px 14px", cursor: "pointer", background: abierto ? (dark ? "#0d1321" : "#F8FAFC") : "transparent" }}>
              <div style={{ display: "flex", justifyContent: "space-between", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0, flexWrap: "wrap" }}>
                  {abierto ? <ChevronDown size={14} color="#94A3B8" /> : <ChevronRight size={14} color="#94A3B8" />}
                  {/* <mark> SOLO en el campo que coincidió (spec, decisión 5) */}
                  <span style={{ fontWeight: 800, fontSize: 13, color: "var(--monza-accent)" }}>
                    {campoMatcheado(c.match, "cotizacion") ? <Resaltar texto={c.numero} q={qDeb} /> : c.numero}
                  </span>
                  <span style={{ fontSize: 12, color: txt, fontWeight: 600 }}>
                    {c.cliente?.nombre ? (campoMatcheado(c.match, "cliente") ? <Resaltar texto={c.cliente.nombre} q={qDeb} /> : c.cliente.nombre) : "—"}
                  </span>
                  {c.vehiculo && <span style={{ fontSize: 11, color: sub }}>{campoMatcheado(c.match, "vehiculo") ? <Resaltar texto={c.vehiculo} q={qDeb} /> : c.vehiculo}</span>}
                  {c.oc_cliente && <span style={{ fontSize: 11, color: sub }}>OC {campoMatcheado(c.match, "oc_cliente") ? <Resaltar texto={c.oc_cliente} q={qDeb} /> : c.oc_cliente}</span>}
                  {qDeb && <MatchBadges match={c.match} />}
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                  {c.fecha_entrega_est && <span style={{ fontSize: 11, color: sub }}>Entrega {fmtDate(c.fecha_entrega_est)}</span>}
                  <DiasRestantesBadge dias={c.dias_restantes} />
                  <span style={{ fontSize: 11, background: est.bg, color: est.color, padding: "3px 10px", borderRadius: 10, fontWeight: 700 }}>{estLabel}</span>
                </div>
              </div>
              <PipelineProgress card={c} />
            </div>
            {abierto && (loadingDet === c.id
              ? <div style={{ borderTop: `1px solid ${bd}`, padding: "14px 16px", color: "#94A3B8", fontSize: 12 }}>Cargando detalle...</div>
              : det ? <AvanceDetalleView det={det} /> : null)}
          </div>
        );
      })}
      {/* Pie honesto: nunca truncar en silencio (spec, decisión 4) */}
      {!loading && cards.length > 0 && total > cards.length && (
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10, flexWrap: "wrap", fontSize: 12, color: sub, paddingTop: 8 }}>
          <span>
            Mostrando {cards.length} de {total} coincidencia(s) — afiná la búsqueda
            {total > 200 ? " · Demasiadas coincidencias: agregá el N° de cotización o el cliente." : ""}
          </span>
          <button onClick={() => { const p = page + 1; setPage(p); load(p, true); }}
            style={{ padding: "5px 12px", border: `1px solid ${bd}`, borderRadius: 6, background: "transparent", color: "var(--monza-accent)", cursor: "pointer", fontSize: 12, fontWeight: 700 }}>
            Ver más (+50)
          </button>
        </div>
      )}
    </div>
  );
}

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
  // Insignia del motivo de coincidencia (la calcula el backend sobre la página)
  match?: MatchInfo[];
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
  // El término del histórico vive en la URL (?q=; el panel de avance usa ?tab= y
  // ?aq=): la página se recarga sola tras crear/confirmar un despacho y sin URL
  // el término se borraba bajo los pies del operador (spec, decisión 6).
  const [searchParams, setSearchParams] = useSearchParams();
  const [items, setItems] = useState<Despacho[]>([]);
  const [total, setTotal] = useState(0);
  const [kpis, setKpis] = useState<KPIs | null>(null);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState(() => searchParams.get("q") || "");
  const [qDeb, setQDeb] = useState(() => normalizarQ(searchParams.get("q") || ""));
  const [page, setPage] = useState(1);
  const [normalizado, setNormalizado] = useState(false);
  const [desde, setDesde] = useState("");
  const [hasta, setHasta] = useState("");
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [details, setDetails] = useState<Record<number, CotDetail>>({});
  const [loadingDetail, setLoadingDetail] = useState<number | null>(null);
  const [uploadModal, setUploadModal] = useState<{ id: number; numero: string } | null>(null);
  // Guardia de secuencia (id monótono): una respuesta fuera de orden se ignora —
  // el clearTimeout del debounce no alcanza con axios crudo.
  const seqRef = useRef(0);

  const bg = dark ? "#131b3e" : "white";
  const bd = dark ? "#1e2a4a" : "#E2E8F0";
  const txt = dark ? "white" : "#1E293B";
  const sub = dark ? "#8899cc" : "#64748B";

  // Debounce 350 ms (spec, decisión 3): antes NO había — cada tecla disparaba
  // DOS requests (listado + KPIs). Enter saltea la espera, Esc limpia.
  useEffect(() => { const t = setTimeout(() => setQDeb(normalizarQ(q)), 350); return () => clearTimeout(t); }, [q]);
  // replaceState mientras se escribe (que Atrás no retroceda carácter por carácter)
  useEffect(() => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      if (q) next.set("q", q); else next.delete("q");
      return next;
    }, { replace: true });
  }, [q, setSearchParams]);

  const fetchList = useCallback(async (pageN: number, append: boolean) => {
    const id = ++seqRef.current;
    if (!append) setLoading(true);
    try {
      const r = await monzaDespachosAPI.list({
        q: qDeb || undefined, desde: desde || undefined, hasta: hasta || undefined,
        page: pageN, page_size: 50,
      });
      if (id !== seqRef.current) return;
      setItems((prev) => (append ? [...prev, ...(r.data.items || [])] : (r.data.items || [])));
      setTotal(r.data.total ?? 0);
      setNormalizado(!!r.data.normalizado);
    } catch { toast.error("Error al cargar despachos"); }
    finally { if (id === seqRef.current) setLoading(false); }
  }, [qDeb, desde, hasta]);
  useEffect(() => { setPage(1); fetchList(1, false); }, [fetchList]);

  // KPIs FUERA del camino del buscador (spec): no dependen del texto — se cargan
  // una vez y con el botón de refresco, no en cada tecla.
  const fetchKpis = useCallback(() => { monzaDespachosAPI.kpis().then((r) => setKpis(r.data)).catch(() => {}); }, []);
  useEffect(() => { fetchKpis(); }, [fetchKpis]);

  const recargar = () => { setPage(1); fetchList(1, false); fetchKpis(); };

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
          onUploaded={() => { setUploadModal(null); recargar(); setDetails({}); }}
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

      {/* Listos para despachar (ítems en bodega) */}
      <ListosPanel />

      {/* Despachos en curso: confirmar borradores, completar transportista/guía, anular */}
      <DespachosEnCursoPanel />

      {/* Tablero de avance por venta (Fase 4): buckets del pipeline + plazo hábil */}
      <AvanceVentasPanel />

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
            {/* El placeholder es un CONTRATO: cada palabra es un campo que la
                consulta toca (spec, decisión 7). */}
            <input value={q} onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") setQDeb(normalizarQ(q));     // Enter saltea la espera
                if (e.key === "Escape") { setQ(""); setQDeb(""); }  // Esc limpia
              }}
              placeholder="N° parte, repuesto, COT, OC, cliente, embarque, N° despacho o guía…"
              style={{ width: "100%", padding: "8px 30px 8px 32px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 13, boxSizing: "border-box" as const, background: dark ? "#0d1321" : "white", color: txt }} />
            {q && (
              <button onClick={() => { setQ(""); setQDeb(""); }} title="Limpiar búsqueda"
                style={{ position: "absolute", right: 6, top: "50%", transform: "translateY(-50%)", background: "none", border: "none", cursor: "pointer", color: sub, padding: 2, display: "flex" }}>
                <X size={14} />
              </button>
            )}
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: sub }}>
            Despacho desde:
            <input type="date" value={desde} onChange={(e) => setDesde(e.target.value)}
              style={{ padding: "6px 8px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 12, background: dark ? "#0d1321" : "white", color: txt, colorScheme: dark ? "dark" : "light" as const }} />
            hasta:
            <input type="date" value={hasta} onChange={(e) => setHasta(e.target.value)}
              style={{ padding: "6px 8px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 12, background: dark ? "#0d1321" : "white", color: txt, colorScheme: dark ? "dark" : "light" as const }} />
          </div>
          <button onClick={recargar} style={{ padding: "7px 10px", border: `1px solid ${bd}`, borderRadius: 6, background: bg, cursor: "pointer", color: sub }}>
            <RefreshCw size={13} />
          </button>
        </div>
        {/* Si el acierto vino de la pasada colapsada (sin guiones), se dice */}
        {qDeb && normalizado && (
          <div style={{ fontSize: 11, color: sub, marginTop: 8 }}>
            Buscaste {qDeb}; también busqué {qDeb.split(" ").map((t) => t.replace(/-/g, "")).join(" ")}.
          </div>
        )}
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
                    {/* Vacíos DIFERENCIADOS: "no hay nada" ≠ "no coincide" (spec, dec. 4) */}
                    {qDeb ? <>
                      <Search size={32} color="#E2E8F0" style={{ display: "block", margin: "0 auto 8px" }} />
                      Sin resultados para «{qDeb}» en el histórico de despachos.
                    </> : <>
                      <Truck size={32} color="#E2E8F0" style={{ display: "block", margin: "0 auto 8px" }} />
                      No hay despachos registrados aún.
                    </>}
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
                        {/* <mark> SOLO en el campo que coincidió + insignia del
                            motivo (spec, decisión 5): "1234" puede ser COT, parte
                            u OC — sin la insignia el operador relee la fila. */}
                        <div style={{ fontWeight: 700, fontSize: 12, color: "var(--monza-accent)" }}>
                          {campoMatcheado(d.match, "cotizacion") ? <Resaltar texto={d.numero} q={qDeb} /> : d.numero}
                        </div>
                        {d.lead_numero && <div style={{ fontSize: 10, color: "#94A3B8" }}>Lead {d.lead_numero}</div>}
                        {lc && d.linea && <span style={{ fontSize: 10, background: lc.bg, color: lc.color, padding: "1px 6px", borderRadius: 6, fontWeight: 600 }}>{d.linea}</span>}
                        {qDeb && <div style={{ marginTop: 3 }}><MatchBadges match={d.match} /></div>}
                      </td>
                      <td style={{ padding: "10px 12px" }}>
                        <div style={{ fontWeight: 600, color: txt }}>{fmtDate(d.fecha_despacho)}</div>
                        {d.fecha_venta && <div style={{ fontSize: 11, color: sub }}>Vendida {fmtDate(d.fecha_venta)}</div>}
                      </td>
                      <td style={{ padding: "10px 12px" }}>
                        <div style={{ fontWeight: 500, color: txt }}>
                          {d.cliente?.nombre ? (campoMatcheado(d.match, "cliente") ? <Resaltar texto={d.cliente.nombre} q={qDeb} /> : d.cliente.nombre) : "—"}
                        </div>
                        {d.cliente?.rut && <div style={{ fontSize: 11, color: sub }}>{campoMatcheado(d.match, "rut") ? <Resaltar texto={d.cliente.rut} q={qDeb} /> : d.cliente.rut}</div>}
                      </td>
                      <td style={{ padding: "10px 12px", color: sub, fontSize: 12 }}>
                        {d.vehiculo ? (campoMatcheado(d.match, "vehiculo") ? <Resaltar texto={d.vehiculo} q={qDeb} /> : d.vehiculo) : "—"}
                        {d.vin && <div style={{ fontSize: 10, color: "#94A3B8" }}>VIN: {campoMatcheado(d.match, "vin") ? <Resaltar texto={d.vin} q={qDeb} /> : d.vin}</div>}
                      </td>
                      <td style={{ padding: "10px 12px" }}>
                        {d.numero_factura ? (
                          <div>
                            <div style={{ fontWeight: 600, color: txt, fontSize: 12 }}>
                              {campoMatcheado(d.match, "factura") ? <Resaltar texto={d.numero_factura} q={qDeb} /> : d.numero_factura}
                            </div>
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
          // Pie HONESTO (spec, decisión 4): antes decía "{total} en total"
          // mientras el backend cortaba en 25 y el frontend nunca mandaba page —
          // 25 filas bajo un cartel que decía 120. Nunca truncar en silencio.
          <div style={{ padding: "10px 16px", borderTop: `1px solid ${bd}`, fontSize: 12, color: "#94A3B8", display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
            <span>
              Mostrando {items.length} de {total} despacho{total !== 1 ? "s" : ""}
              {total > items.length && qDeb ? " — afiná la búsqueda" : ""}
              {qDeb && total > 200 ? " · Demasiadas coincidencias: agregá el N° de cotización o el cliente." : ""}
            </span>
            {total > items.length && (
              <button onClick={() => { const p = page + 1; setPage(p); fetchList(p, true); }}
                style={{ padding: "5px 12px", border: `1px solid ${bd}`, borderRadius: 6, background: "transparent", color: "var(--monza-accent)", cursor: "pointer", fontSize: 12, fontWeight: 700 }}>
                Ver más (+50)
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
