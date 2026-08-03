import { useState, useEffect, useCallback } from "react";
import { Search, Download, RefreshCw, FileText, ChevronDown, ChevronRight, CheckCircle } from "lucide-react";
import { monzaCotizacionesAPI } from "../services/monzaApi";
import type { PagoOpcion } from "../constants/adelanto";
import {
  PAGO_OPCIONES, PAGO_ADELANTO_LIBRE_ID, ADELANTO_PCT_DEFECTO, formaPagoAdelanto,
} from "../constants/adelanto";
import { hoyLocal } from "../utils/format";
import toast from "react-hot-toast";

interface Cotizacion {
  id: number; numero: string; estado: string; tipo_cotizacion?: string;
  linea?: string; vehiculo?: string; total_bruto: number; items_count: number;
  fecha_creacion: string; fecha_venta?: string; oc_cliente?: string; asesor?: string;
  cliente?: { nombre: string; rut?: string };
  lead_numero?: string;
  // Hallazgos #20 y #14 de la auditoría integral Monza: la API no devolvía estos dos campos,
  // así que el modal de cierre no podía mostrar la condición vigente ni la fecha real de la OC
  // y las re-escribía a ciegas (la fecha viaja como referencia 801 al SII). Se leen como
  // OPCIONALES: si el backend todavía no los serializa, el modal cae al comportamiento anterior.
  oc_fecha?: string | null;
  pct_adelanto?: number;
  // Los tres los sirve el MISMO serializador (_cot_dict) que ya alimenta esta lista; se
  // declaran para el modal de cierre: `forma_pago` preselecciona la condición vigente,
  // `total_neto`/`iva_monto` arman el resumen previo a confirmar (M15) y
  // `fecha_entrega_est` evita pisar una fecha prometida ya acordada al re-cerrar.
  forma_pago?: string;
  total_neto?: number;
  iva_monto?: number;
  fecha_entrega_est?: string | null;
}
interface CotItem {
  id: number; descripcion: string; marca?: string; numero_parte?: string;
  calidad?: string; cantidad: number; precio_unitario_clp?: number; subtotal_clp?: number;
  plazo_entrega?: string;
}
interface CotDetail {
  id: number; numero: string; estado: string; vehiculo?: string; vin?: string; anio?: string;
  forma_pago?: string; condiciones_servicio?: string; tipo_cotizacion?: string;
  total_neto?: number; iva_monto?: number; total_bruto?: number;
  items: CotItem[];
  cliente?: { nombre: string; rut?: string };
}

// ── Días hábiles chilenos ───────────────────────────────────────────────────
// Copia LITERAL del motor del Cierre de Venta de Grupo AM (CierreVentaPage.tsx): mismos
// feriados, misma Pascua por Meeus, mismo Jueves/Viernes Santo y mismo Día de los
// Pueblos Indígenas. No es una aproximación: la fecha prometida de entrega alimenta el
// semáforo SLA de Ventas, el orden por urgencia de Despachos, las alertas diarias y los
// avisos de Bodega, y una fecha corrida por un feriado mueve los cuatro.

/** Algoritmo de Meeus para Pascua */
function easterDate(year: number): Date {
  const a = year % 19;
  const b = Math.floor(year / 100);
  const c = year % 100;
  const d = Math.floor(b / 4);
  const e = b % 4;
  const f = Math.floor((b + 8) / 25);
  const g = Math.floor((b - f + 1) / 3);
  const h = (19 * a + b - d - g + 15) % 30;
  const i = Math.floor(c / 4);
  const k = c % 4;
  const l = (32 + 2 * e + 2 * i - h - k) % 7;
  const m = Math.floor((a + 11 * h + 22 * l) / 451);
  const month = Math.floor((h + l - 7 * m + 114) / 31) - 1;
  const day = ((h + l - 7 * m + 114) % 31) + 1;
  return new Date(year, month, day);
}

/** Devuelve la fecha del n-ésimo weekday (0=Dom…6=Sáb) del mes/año */
function nthWeekday(year: number, month: number, weekday: number, n: number): Date {
  const d = new Date(year, month - 1, 1);
  let count = 0;
  while (true) {
    if (d.getDay() === weekday) { count++; if (count === n) return new Date(d); }
    d.setDate(d.getDate() + 1);
  }
}

/** Feriados legales Chile — incluye Jueves Santo, Viernes Santo y Pueblos Indígenas */
function isHoliday(date: Date): boolean {
  const mo = date.getMonth() + 1;
  const dd = date.getDate();
  const y = date.getFullYear();

  // Feriados fijos
  const fixed: [number, number][] = [
    [1, 1],   // Año Nuevo
    [5, 1],   // Día del Trabajo
    [5, 21],  // Glorias Navales
    [6, 29],  // San Pedro y San Pablo
    [7, 16],  // Virgen del Carmen
    [8, 15],  // Asunción
    [9, 18],  // Fiestas Patrias
    [9, 19],  // Día del Ejército
    [10, 12], // Encuentro Dos Mundos
    [10, 31], // Iglesias Evangélicas
    [11, 1],  // Todos los Santos
    [12, 8],  // Inmaculada Concepción
    [12, 25], // Navidad
  ];
  if (fixed.some(([fm, fd]) => fm === mo && fd === dd)) return true;

  // Pascua → Jueves Santo (−3) y Viernes Santo (−2)
  const easter = easterDate(y);
  const juevesSanto = new Date(easter); juevesSanto.setDate(easter.getDate() - 3);
  const viernesSanto = new Date(easter); viernesSanto.setDate(easter.getDate() - 2);
  if (dd === juevesSanto.getDate() && mo === juevesSanto.getMonth() + 1) return true;
  if (dd === viernesSanto.getDate() && mo === viernesSanto.getMonth() + 1) return true;

  // Día de los Pueblos Indígenas — 3er lunes de junio (desde 2021)
  if (y >= 2021) {
    const pueblos = nthWeekday(y, 6, 1, 3); // lunes=1, 3er ocurrencia, junio
    if (mo === 6 && dd === pueblos.getDate()) return true;
  }

  return false;
}

function addBusinessDays(start: Date, days: number): Date {
  const result = new Date(start);
  let count = 0;
  while (count < days) {
    result.setDate(result.getDate() + 1);
    const dow = result.getDay();
    if (dow !== 0 && dow !== 6 && !isHoliday(result)) count++;
  }
  return result;
}

/** Formatea Date a YYYY-MM-DD usando fecha LOCAL (evita desfase UTC en Chile UTC-3/−4) */
function toDateInput(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

/** Formatea 'YYYY-MM-DD' (fecha pura) a dd/mm/aaaa SIN pasar por UTC. El `fmtDate` de
 *  esta pantalla sirve para timestamps; con una fecha pura `new Date('YYYY-MM-DD')` la
 *  interpreta en UTC y en Chile mostraría el día anterior. */
function fmtFechaIso(iso: string): string {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(iso)) return iso || "—";
  const [y, m, d] = iso.split("-");
  return `${d}/${m}/${y}`;
}

// Plazo de entrega por defecto (días hábiles) cuando NINGÚN ítem de la cotización lo
// declara. Mismo número que el fallback del Cierre de Venta de Grupo AM
// (`data.config?.plazo_max_default ?? 45`); Monza no tiene ese parámetro en Config, así
// que acá el 45 es la constante. La fecha queda editable en el modal.
const PLAZO_ENTREGA_DEFECTO_DIAS = 45;
// Tope de cordura al leer el plazo. En Monza `plazo_entrega` es TEXTO LIBRE por ítem
// ("30 días", "15-20 días hábiles"), así que un "2026" tecleado por error daría una
// entrega prometida a 8 años. Grupo AM no lo necesita: allá el plazo es columna numérica.
const PLAZO_ENTREGA_TOPE_DIAS = 365;

/** Días de plazo que declara un ítem. Mismo criterio que Grupo AM: se toma el MAYOR
 *  número del texto ("15-20 días hábiles" → 20). undefined si no hay ninguno usable. */
function parsePlazoDias(s?: string): number | undefined {
  if (!s) return undefined;
  const nums = s.replace(/[^0-9]/g, " ").trim().split(/\s+/).map(Number)
    .filter((n) => n > 0 && n <= PLAZO_ENTREGA_TOPE_DIAS);
  return nums.length > 0 ? Math.max(...nums) : undefined;
}

/** Normaliza el error del backend a texto legible. Un 422 de FastAPI trae `detail` como
 *  ARREGLO de objetos —y con `oc_fecha`/`fecha_entrega_est` como `date` el 422 es un
 *  escenario real (una fecha mal tecleada)—; sin esto el toast mostraba
 *  "[object Object]". Molde de CierreVentaPage.tsx, tipado sin `any`. */
function msgError(e: unknown, fallback: string): string {
  const d = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (Array.isArray(d)) {
    const partes = d.map((x) => {
      const msg = (x as { msg?: unknown })?.msg;
      return typeof msg === "string" ? msg : JSON.stringify(x);
    });
    return partes.length > 0 ? partes.join("; ") : fallback;
  }
  if (typeof d === "string" && d.trim()) return d;
  return fallback;
}

/** Opción de pago que corresponde a la venta tal como está guardada.
 *
 *  Prioriza `forma_pago` (así un crédito a 60 días vuelve a preseleccionarse a 60 y no
 *  a 30) y cae al `pct_adelanto`, que es el dato que mueve plata. Si el % vigente no
 *  coincide con ninguna opción fija —el caso nuevo del adelanto libre— devuelve la
 *  opción de % libre: sin esto, un re-cierre bajaría un 30% pactado a 0 en silencio,
 *  que es exactamente el hallazgo #14 que ya se corrigió para el 50%. */
function opcionDeVenta(formaPago: string | undefined, pct: number): PagoOpcion {
  const porForma = PAGO_OPCIONES.find((o) => !o.pctLibre && o.forma === (formaPago || ""));
  if (porForma && porForma.pct === pct) return porForma;
  const porPct = PAGO_OPCIONES.find((o) => !o.pctLibre && o.pct === pct);
  if (porPct) return porPct;
  return PAGO_OPCIONES.find((o) => o.pctLibre) ?? PAGO_OPCIONES[0];
}

// Estados desde los que la fila ofrece el botón de cerrar la venta (hallazgo A5). El
// botón se pintaba con `estado !== "vendida"`, así que también aparecía en 'propuesta'
// (nunca se le envió al cliente) y en 'rechazada' (el cliente dijo NO) — y el cierre
// libera las compras al proveedor. El backend rechaza esos dos con 409
// (ESTADOS_QUE_CIERRAN_VENTA en monza_router_cotizaciones.py); acá el botón directamente
// no se ofrece. 'vendida' pasa el guard del backend por idempotencia pero sigue oculto,
// igual que hoy: la venta ya cerrada se corrige desde Contabilidad → Ventas.
const ESTADOS_CON_BOTON_CERRAR = new Set(["enviada"]);

/** Lo que el modal entrega al confirmar. Un objeto (y no 5 parámetros posicionales)
 *  para que agregar un campo no se pueda cablear en el orden equivocado. */
interface CierreVentaPayload {
  pct_adelanto: number;
  forma_pago: string;
  oc_cliente: string;
  oc_fecha: string;
  fecha_entrega_est: string;
}

const ESTADO_CONFIG: Record<string, { bg: string; color: string; label: string }> = {
  propuesta: { bg: "#F1F5F9", color: "#475569", label: "Propuesta" },
  enviada: { bg: "#DBEAFE", color: "#1D4ED8", label: "Enviada" },
  vendida: { bg: "#DCFCE7", color: "#15803D", label: "Vendida" },
  rechazada: { bg: "#FEE2E2", color: "#B91C1C", label: "Rechazada" },
};
const LINEA_CONFIG: Record<string, { bg: string; color: string }> = {
  autos: { bg: "#DBEAFE", color: "#1D4ED8" },
  maquinaria: { bg: "#FEF3C7", color: "#D97706" },
};

function fmt(n: number) { return n > 0 ? `$${n.toLocaleString("es-CL")}` : "$0"; }
function fmtDate(d?: string) { return d ? new Date(d).toLocaleDateString("es-CL") : "—"; }

// Condiciones de pago ofrecidas al cerrar una venta (PAGO_OPCIONES, en constants/adelanto).
// El '% adelanto' dispara la verificación de pago en Contabilidad y "pago no verificado"
// en Abastecimiento.
function CerrarVentaModal({ cot, detalle, loading, onClose, onConfirm }: {
  cot: Cotizacion;
  /** Detalle ya cargado (fila expandida). Si no viene, el modal lo pide: de ahí sale el
   *  plazo de entrega por ítem, que es lo único que la lista no trae. */
  detalle?: CotDetail;
  loading: boolean;
  onClose: () => void;
  onConfirm: (payload: CierreVentaPayload) => void;
}) {
  // Hallazgo #14: el modal arrancaba SIEMPRE en "contado", así que un re-cierre para corregir
  // otra cosa mandaba pct_adelanto = 0 y borraba en silencio el 50% vigente (y con él el
  // cortafuego de "pago no verificado" de Abastecimiento). Ahora preselecciona la condición
  // que la venta ya tiene; si el backend aún no serializa pct_adelanto, cae en "contado" como antes.
  const pctVigente = Number(cot.pct_adelanto ?? 0);
  const vigente = opcionDeVenta(cot.forma_pago, pctVigente);
  const [sel, setSel] = useState(() => vigente.id);
  // OC del cliente OBLIGATORIA (Fase 3): la referencia 801 del SII exige N° y fecha,
  // y sin OC no hay ancla comercial del cobro. El backend también la exige.
  const [oc, setOc] = useState(cot.oc_cliente || "");
  // Hallazgo #20: la fecha se inicializaba con new Date().toISOString() = HOY EN UTC, de modo que
  // (a) re-abrir el modal para corregir el N° de OC pisaba la fecha real de la OC con la de hoy
  // —y esa fecha viaja como referencia 801 al SII— y (b) de noche en Chile daba el día siguiente.
  // Se usa la fecha guardada si el backend la devuelve, y hoyLocal() (no UTC) como último recurso.
  const [ocFecha, setOcFecha] = useState(() => cot.oc_fecha || hoyLocal());
  // ── Hallazgo A2 · adelanto de % LIBRE o monto exacto en pesos ────────────────
  // Espejo bidireccional (molde de CierreVentaPage.tsx): con el monto digitado el % se
  // deriva, y con el % digitado se ven los pesos. Los 3 casos de siempre (contado,
  // 50%, crédito 30) siguen siendo radios sin nada que teclear.
  const [adelPct, setAdelPct] = useState(() =>
    vigente.pctLibre && pctVigente > 0 ? String(pctVigente) : String(ADELANTO_PCT_DEFECTO));
  const [adelMonto, setAdelMonto] = useState("");
  // ── Hallazgo A1 · fecha prometida de entrega ─────────────────────────────────
  // Monza NUNCA escribía esta fecha, así que el semáforo SLA de Ventas decía "Sin
  // fecha" en el 100% de las ventas y el orden por urgencia de Despachos, las alertas
  // diarias y los avisos de Bodega no se disparaban jamás. Se precarga con el mayor
  // plazo de los ítems en días hábiles chilenos, y queda editable.
  const [det, setDet] = useState<CotDetail | undefined>(detalle);
  const [cargandoDet, setCargandoDet] = useState(!detalle);
  const [fechaEntrega, setFechaEntrega] = useState(() => cot.fecha_entrega_est || "");
  const [fechaTocada, setFechaTocada] = useState(false);

  useEffect(() => {
    if (detalle) { setDet(detalle); setCargandoDet(false); return; }
    let vivo = true;
    setCargandoDet(true);
    monzaCotizacionesAPI.get(cot.id)
      .then((r) => { if (vivo) setDet(r.data as CotDetail); })
      .catch(() => {
        // Sin el detalle la fecha se precarga con el plazo por defecto: se avisa para que
        // nadie cierre una venta con una entrega prometida que no revisó.
        if (vivo) toast.error("No se pudo leer el plazo de los ítems: revisa la fecha prometida de entrega");
      })
      .finally(() => { if (vivo) setCargandoDet(false); });
    return () => { vivo = false; };
  }, [cot.id, detalle]);

  const plazosItems = (det?.items || [])
    .map((it) => parsePlazoDias(it.plazo_entrega))
    .filter((v): v is number => v != null);
  const hayPlazoDeItems = plazosItems.length > 0;
  const plazoDias = hayPlazoDeItems ? Math.max(...plazosItems) : PLAZO_ENTREGA_DEFECTO_DIAS;

  useEffect(() => {
    // Solo precarga: no pisa una fecha ya acordada (re-cierre) ni una tecleada a mano.
    if (fechaTocada || cot.fecha_entrega_est || cargandoDet) return;
    setFechaEntrega(toDateInput(addBusinessDays(new Date(), plazoDias)));
  }, [cargandoDet, plazoDias, fechaTocada, cot.fecha_entrega_est]);

  const opt = PAGO_OPCIONES.find((o) => o.id === sel) || PAGO_OPCIONES[0];
  const esLibre = !!opt.pctLibre;
  const ocOk = oc.trim().length > 0;

  // Base del adelanto: el total BRUTO de la venta (Monza ya lo guarda con IVA, con la
  // tasa congelada de la venta — nunca un 1,19 escrito a mano). Es la MISMA base con la
  // que Tesorería sugiere el monto a aprobar (monza_tesoreria: total_bruto × pct / 100).
  const totalBruto = Number(cot.total_bruto || 0);
  const totalNeto = Number(cot.total_neto || 0);
  const factorNeto = totalBruto > 0 && totalNeto > 0 ? totalNeto / totalBruto : null;
  const montoAdel = Number(adelMonto) || 0;
  const montoValido = adelMonto.trim() !== "" && montoAdel > 0;
  const adelantoExcede = totalBruto > 0 && montoAdel > totalBruto;
  const pctDigitado = Number(adelPct);
  const pctDesdeMonto = montoValido && totalBruto > 0 ? (montoAdel / totalBruto) * 100 : null;
  // Lo que REALMENTE se informa. `pct_adelanto` es entero en la base (0-100), así que un
  // monto exacto se redondea al entero más cercano: por eso el resumen muestra los pesos
  // resultantes — lo que se ve es lo que Tesorería va a ver.
  const pctFinal = esLibre
    ? Math.round(pctDesdeMonto ?? (Number.isFinite(pctDigitado) ? pctDigitado : 0))
    : opt.pct;
  const montoFinal = totalBruto > 0 ? Math.round(totalBruto * pctFinal / 100) : 0;
  const montoDesdePct = esLibre && !montoValido && totalBruto > 0
    && pctDigitado > 0 && pctDigitado <= 100
    ? Math.round(totalBruto * pctDigitado / 100)
    : null;

  // Se valida ACÁ (y no al recibir el 422) porque un cierre a medias deja la venta
  // cerrada con la condición equivocada: el PATCH es uno solo.
  const problemaAdelanto: string | null = (() => {
    if (!esLibre) return null;
    if (montoValido) {
      if (totalBruto <= 0) return "La cotización no tiene total calculado: informa el adelanto en %.";
      if (adelantoExcede) return "El monto del adelanto supera el total de la venta.";
      if (pctFinal < 1) return "El monto es menor al 1% del total: informa el adelanto en %.";
      return null;
    }
    if (adelMonto.trim() !== "") return "El monto del adelanto debe ser mayor a 0.";
    if (!(pctDigitado >= 1 && pctDigitado <= 100)) return "El % de adelanto debe estar entre 1 y 100.";
    return null;
  })();
  const puedeConfirmar = ocOk && !problemaAdelanto && !loading;

  // Aviso explícito cuando el re-cierre BAJA el adelanto ya acordado (hallazgo #14). Con
  // el adelanto libre a medio teclear el % todavía no significa nada: no se avisa.
  const bajaAdelanto = pctVigente > 0 && pctFinal < pctVigente && !problemaAdelanto;
  // La fecha mostrada es la CALCULADA (no una acordada antes ni una tecleada a mano):
  // solo en ese caso el "— N días hábiles" describe lo que hay en el campo.
  const fechaEsCalculada = !cot.fecha_entrega_est && !fechaTocada && !cargandoDet;

  const lbl: React.CSSProperties = { fontSize: 12, fontWeight: 600, color: "#64748B", display: "block", marginBottom: 4 };
  const inp: React.CSSProperties = { width: "100%", padding: "8px 10px", border: "1px solid #E2E8F0", borderRadius: 6, fontSize: 13, boxSizing: "border-box" };
  const fila: React.CSSProperties = { display: "flex", justifyContent: "space-between", gap: 12, padding: "2px 0" };

  return (
    <div style={{ position: "fixed", inset: 0, zIndex: 300, display: "flex", alignItems: "center", justifyContent: "center", background: "rgba(0,0,0,0.5)", padding: 16 }} onClick={onClose}>
      <div style={{ width: "100%", maxWidth: 520, maxHeight: "92vh", display: "flex", flexDirection: "column", background: "white", borderRadius: 14, border: "1px solid #E2E8F0", boxShadow: "0 20px 50px rgba(0,0,0,0.25)" }} onClick={(e) => e.stopPropagation()}>
        <div style={{ padding: "14px 18px", borderBottom: "1px solid #E2E8F0", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <h3 style={{ margin: 0, fontSize: 15, fontWeight: 600, color: "#0f172a" }}>Cerrar venta · {cot.numero}</h3>
          <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", color: "#64748B", fontSize: 20, lineHeight: 1 }}>×</button>
        </div>
        <div style={{ padding: 18, overflowY: "auto" }}>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginBottom: 12 }}>
            <div>
              <label style={{ ...lbl, color: ocOk ? "#64748B" : "#B91C1C" }}>N° OC del cliente *</label>
              <input value={oc} onChange={(e) => setOc(e.target.value)} placeholder="Obligatorio"
                style={{ ...inp, border: `1px solid ${ocOk ? "#E2E8F0" : "#FCA5A5"}` }} />
            </div>
            <div>
              <label style={lbl}>Fecha OC</label>
              <input type="date" value={ocFecha} onChange={(e) => setOcFecha(e.target.value)} style={inp} />
            </div>
          </div>

          {/* Hallazgo A1: la fecha prometida de entrega. Sin ella el SLA de Ventas, la
              urgencia de Despachos, las alertas diarias y Bodega quedan apagados. */}
          <div style={{ marginBottom: 12 }}>
            <label style={lbl}>
              Fecha prometida de entrega
              {fechaEsCalculada && (
                <span style={{ fontWeight: 400, color: "#94A3B8" }}>
                  {" "}— {plazoDias} días hábiles
                </span>
              )}
            </label>
            <input type="date" value={fechaEntrega} style={inp}
              onChange={(e) => { setFechaTocada(true); setFechaEntrega(e.target.value); }} />
            <p style={{ margin: "4px 0 0", fontSize: 11, color: "#94A3B8" }}>
              {cargandoDet
                ? "Leyendo el plazo de los ítems…"
                : fechaTocada
                  ? "Fecha fijada a mano."
                  : cot.fecha_entrega_est
                    ? "Fecha ya acordada en esta venta. Puedes ajustarla."
                    : hayPlazoDeItems
                      ? `Calculada desde el mayor plazo de los ítems (${plazoDias}d hábiles, feriados chilenos incluidos). Puedes ajustarla.`
                      : `Ningún ítem declara plazo: se usa el plazo por defecto (${plazoDias}d hábiles). Puedes ajustarla.`}
            </p>
          </div>

          <p style={{ margin: "0 0 10px", fontSize: 13, color: "#64748B" }}>
            Condición de pago acordada con el cliente:
            {/* Hallazgo #14: mostrar la condición VIGENTE para que el re-cierre no se haga a ciegas. */}
            {pctVigente > 0 && (
              <span style={{ display: "block", fontSize: 12, color: "#B45309", marginTop: 2 }}>
                Vigente: <b>{pctVigente}% de adelanto</b>
              </span>
            )}
          </p>
          {PAGO_OPCIONES.map((o) => (
            <div key={o.id}>
              <label style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 12px", borderRadius: 10, border: `1px solid ${sel === o.id ? "var(--monza-accent)" : "#E2E8F0"}`, marginBottom: 8, cursor: "pointer" }}>
                <input type="radio" name="pago" checked={sel === o.id} onChange={() => setSel(o.id)} />
                <span style={{ fontSize: 14, color: "#0f172a", fontWeight: sel === o.id ? 600 : 400 }}>{o.label}</span>
              </label>
              {/* Los dos campos del adelanto libre viven DENTRO de su opción: solo
                  aparecen cuando está elegida, así los 3 casos de siempre se ven igual. */}
              {o.id === PAGO_ADELANTO_LIBRE_ID && sel === o.id && (
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, margin: "0 0 10px", padding: "10px 12px", background: "#F8FAFC", border: "1px solid #E2E8F0", borderRadius: 10 }}>
                  <div>
                    <label style={lbl}>% del total</label>
                    {/* Con monto en pesos digitado el % se DERIVA en vivo (y el campo se
                        bloquea): el dato que manda es uno solo, nunca los dos a la vez. */}
                    <input type="number" min={1} max={100} style={inp}
                      value={montoValido
                        ? (pctDesdeMonto != null ? pctDesdeMonto.toFixed(1) : "")
                        : adelPct}
                      onChange={(e) => setAdelPct(e.target.value)}
                      disabled={montoValido} />
                    {montoValido && totalBruto > 0 && (
                      <p style={{ margin: "4px 0 0", fontSize: 11, color: adelantoExcede ? "#B91C1C" : "#94A3B8" }}>
                        {adelantoExcede
                          ? `Supera el total de la venta (${fmt(totalBruto)} c/IVA)`
                          : `del total c/IVA ${fmt(totalBruto)}`}
                      </p>
                    )}
                  </div>
                  <div>
                    <label style={lbl}>o monto exacto (CLP, con IVA)</label>
                    <input type="number" min={0} style={inp} placeholder="opcional"
                      value={adelMonto} onChange={(e) => setAdelMonto(e.target.value)} />
                    {/* Espejo del %: al digitar el porcentaje acá se ven los pesos, con la
                        MISMA fórmula con que Tesorería sugiere el monto a aprobar. */}
                    {montoDesdePct !== null && (
                      <p style={{ margin: "4px 0 0", fontSize: 11, color: "#94A3B8" }}>
                        ≈ <b style={{ color: "#1E293B" }}>{fmt(montoDesdePct)}</b> c/IVA
                        {factorNeto != null && ` (${fmt(Math.round(montoDesdePct * factorNeto))} neto)`}
                      </p>
                    )}
                    {/* El % se guarda ENTERO: si el monto exacto no cae justo, acá se ve el
                        monto que realmente quedará informado antes de confirmar. */}
                    {montoValido && !adelantoExcede && pctFinal >= 1 && montoFinal !== montoAdel && (
                      <p style={{ margin: "4px 0 0", fontSize: 11, color: "#B45309" }}>
                        Se informa como <b>{pctFinal}%</b> = {fmt(montoFinal)} c/IVA
                      </p>
                    )}
                  </div>
                </div>
              )}
            </div>
          ))}
          {problemaAdelanto && (
            <p style={{ margin: "4px 0 12px", fontSize: 12, color: "#B91C1C", background: "#FEE2E2", padding: "8px 10px", borderRadius: 8 }}>
              {problemaAdelanto}
            </p>
          )}
          {pctFinal > 0 && !problemaAdelanto && (
            <p style={{ margin: "4px 0 12px", fontSize: 12, color: "#B45309", background: "#FEF3C7", padding: "8px 10px", borderRadius: 8 }}>
              El cliente paga {pctFinal}% por adelantado
              {totalBruto > 0 && <> (<b>{fmt(montoFinal)}</b> c/IVA)</>}. Contabilidad deberá <b>verificar el pago</b>; en Abastecimiento aparecerá <b>"pago no verificado"</b> hasta entonces.
            </p>
          )}
          {/* Hallazgo #14: bajar el adelanto apaga el cortafuego que impide comprar sin anticipo,
              y antes ocurría en silencio. Si ya hay plata registrada, el backend responde 409. */}
          {bajaAdelanto && (
            <p style={{ margin: "4px 0 12px", fontSize: 12, color: "#B91C1C", background: "#FEE2E2", padding: "8px 10px", borderRadius: 8 }}>
              Estás bajando el adelanto de <b>{pctVigente}%</b> a <b>{pctFinal}%</b>: Abastecimiento podrá comprar sin que se cobre el anticipo.
              Si el adelanto ya fue verificado o cobrado, el sistema rechazará el cambio (revíertelo antes en Contabilidad/Tesorería).
            </p>
          )}

          {/* M15: resumen ANTES de confirmar. El cierre dispara las compras al proveedor,
              habilita Contabilidad y arranca el reloj de la entrega: el modal era OC,
              fecha, radios y un botón — sin un solo monto a la vista. */}
          <div style={{ marginBottom: 14, padding: "10px 12px", background: "#F8FAFC", border: "1px solid #E2E8F0", borderRadius: 10, fontSize: 12, color: "#475569" }}>
            <div style={{ fontSize: 11, fontWeight: 700, color: "#64748B", textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 6 }}>
              Resumen de la venta
            </div>
            <div style={fila}>
              <span>Cliente</span>
              <b style={{ color: "#1E293B", textAlign: "right" }}>{cot.cliente?.nombre || "—"}</b>
            </div>
            <div style={fila}>
              <span>Ítems</span>
              <b style={{ color: "#1E293B" }}>{det ? det.items.length : cot.items_count}</b>
            </div>
            <div style={fila}>
              <span>Total neto</span>
              <b style={{ color: "#1E293B" }}>{totalNeto > 0 ? fmt(totalNeto) : "—"}</b>
            </div>
            {cot.iva_monto != null && cot.iva_monto > 0 && (
              <div style={fila}>
                <span>IVA</span>
                <b style={{ color: "#1E293B" }}>{fmt(Number(cot.iva_monto))}</b>
              </div>
            )}
            <div style={{ ...fila, borderTop: "1px solid #E2E8F0", marginTop: 4, paddingTop: 6 }}>
              <span style={{ fontWeight: 600 }}>Total c/IVA</span>
              <b style={{ color: "var(--monza-accent)", fontSize: 14 }}>{fmt(totalBruto)}</b>
            </div>
            <div style={{ ...fila, marginTop: 4 }}>
              <span>Entrega prometida</span>
              <b style={{ color: fechaEntrega ? "#1E293B" : "#B91C1C" }}>
                {fechaEntrega ? fmtFechaIso(fechaEntrega) : "Sin fecha"}
              </b>
            </div>
            <div style={fila}>
              <span>Adelanto</span>
              <b style={{ color: "#1E293B" }}>
                {pctFinal > 0
                  ? `${pctFinal}%${totalBruto > 0 ? ` · ${fmt(montoFinal)}` : ""}`
                  : "Sin adelanto"}
              </b>
            </div>
            <div style={fila}>
              <span>Condición</span>
              <b style={{ color: "#1E293B", textAlign: "right" }}>
                {esLibre ? formaPagoAdelanto(pctFinal) : opt.forma}
              </b>
            </div>
          </div>

          <button
            onClick={() => onConfirm({
              pct_adelanto: pctFinal,
              forma_pago: esLibre ? formaPagoAdelanto(pctFinal) : opt.forma,
              oc_cliente: oc.trim(),
              oc_fecha: ocFecha,
              fecha_entrega_est: fechaEntrega,
            })}
            disabled={!puedeConfirmar}
            style={{ width: "100%", padding: 10, borderRadius: 8, border: "none", background: puedeConfirmar ? "var(--monza-accent)" : "#94A3B8", color: "white", fontWeight: 600, fontSize: 14, cursor: puedeConfirmar ? "pointer" : "not-allowed", opacity: loading ? 0.6 : 1 }}>
            {loading ? "Guardando…" : !ocOk ? "Falta el N° de OC del cliente" : problemaAdelanto ? "Revisa el adelanto" : "Confirmar venta"}
          </button>
        </div>
      </div>
    </div>
  );
}

export default function MonzaCotizacionesPage() {
  const [items, setItems] = useState<Cotizacion[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState("");
  const [estado, setEstado] = useState("todos");
  const [desde, setDesde] = useState("");
  const [hasta, setHasta] = useState("");
  const [downloading, setDownloading] = useState<number | null>(null);
  const [updatingEstado, setUpdatingEstado] = useState<number | null>(null);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [details, setDetails] = useState<Record<number, CotDetail>>({});
  const [loadingDetail, setLoadingDetail] = useState<number | null>(null);
  const [markingVendida, setMarkingVendida] = useState<number | null>(null);
  const [cerrarVenta, setCerrarVenta] = useState<Cotizacion | null>(null);

  const fetchAll = useCallback(async () => {
    setLoading(true);
    try {
      const r = await monzaCotizacionesAPI.list({
        q: q || undefined,
        estado: estado !== "todos" ? estado : undefined,
        desde: desde || undefined,
        hasta: hasta || undefined,
      });
      setItems(r.data.items);
      setTotal(r.data.total);
    } catch { toast.error("Error al cargar cotizaciones"); }
    finally { setLoading(false); }
  }, [q, estado, desde, hasta]);

  useEffect(() => { fetchAll(); }, [fetchAll]);

  const handleDownload = async (cot: Cotizacion) => {
    setDownloading(cot.id);
    try {
      const r = await monzaCotizacionesAPI.downloadPdf(cot.id);
      const blob = new Blob([r.data], { type: "application/pdf" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `Cotizacion_${cot.numero}.pdf`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch { toast.error("Error al descargar PDF"); }
    finally { setDownloading(null); }
  };

  const handleEstado = async (id: number, nuevoEstado: string) => {
    setUpdatingEstado(id);
    try {
      await monzaCotizacionesAPI.update(id, { estado: nuevoEstado });
      toast.success("Estado actualizado");
      fetchAll();
    } catch (e: unknown) {
      // M14: los 409 del backend (des-cierre con plata colgando, cobertura de despachos,
      // OC ya referenciada en un DTE) explican QUÉ hacer; el mensaje genérico los tapaba.
      toast.error(msgError(e, "Error al actualizar estado"), { duration: 8000 });
    }
    finally { setUpdatingEstado(null); }
  };

  const toggleExpand = async (id: number) => {
    const next = new Set(expanded);
    if (next.has(id)) {
      next.delete(id);
      setExpanded(next);
      return;
    }
    next.add(id);
    setExpanded(next);
    if (!details[id]) {
      setLoadingDetail(id);
      try {
        const r = await monzaCotizacionesAPI.get(id);
        setDetails((prev) => ({ ...prev, [id]: r.data }));
      } catch { toast.error("Error al cargar detalle"); }
      finally { setLoadingDetail(null); }
    }
  };

  // Cierra la venta con la condición de pago elegida (pct_adelanto dispara la
  // verificación en Contabilidad y el estado en Abastecimiento).
  const confirmarVenta = async (cot: Cotizacion, p: CierreVentaPayload) => {
    setMarkingVendida(cot.id);
    try {
      await monzaCotizacionesAPI.update(cot.id, {
        estado: "vendida",
        pct_adelanto: p.pct_adelanto,
        forma_pago: p.forma_pago,
        oc_cliente: p.oc_cliente,
        oc_fecha: p.oc_fecha || undefined,
        // Hallazgo A1: la fecha prometida de entrega. Es la que enciende el semáforo SLA
        // de Ventas, el orden por urgencia de Despachos, las alertas diarias del
        // scheduler y los avisos de Bodega — hasta ahora ninguna pantalla la escribía.
        fecha_entrega_est: p.fecha_entrega_est || undefined,
      });
      toast.success(`Cotización ${cot.numero} marcada como Vendida`);
      setCerrarVenta(null);
      fetchAll();
    } catch (e: unknown) {
      // M14: con oc_fecha/fecha_entrega_est como `date`, un 422 de FastAPI llega como
      // ARREGLO y el toast mostraba "[object Object]".
      toast.error(msgError(e, "Error al marcar como vendida"), { duration: 8000 });
    }
    finally { setMarkingVendida(null); }
  };

  const CALIDAD_LABEL: Record<string, string> = { sin_calificar: "—", genuine: "Genuine", oem: "OEM", aftermarket: "Aftermarket" };

  return (
    <div>
      {/* Header */}
      <div style={{ marginBottom: 24 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
          <FileText size={22} className="monza-ic" />
          <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: "#1E293B" }}>Registro de cotizaciones</h1>
        </div>
        <p style={{ margin: 0, fontSize: 13, color: "#64748B" }}>
          Todas las cotizaciones emitidas en el sistema, ordenadas por fecha descendente.
        </p>
      </div>

      {/* Filters */}
      <div style={{ background: "white", border: "1px solid #E2E8F0", borderRadius: 10, padding: "14px 16px", marginBottom: 16 }}>
        <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <div style={{ position: "relative", flex: 1, minWidth: 280 }}>
            <Search size={14} style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)", color: "#94A3B8" }} />
            <input value={q} onChange={(e) => setQ(e.target.value)}
              placeholder="Buscar N° COT, cliente, vehículo, N° parte..."
              style={{ width: "100%", padding: "8px 10px 8px 32px", border: "1px solid #E2E8F0", borderRadius: 6, fontSize: 13, boxSizing: "border-box" }} />
          </div>
          <select value={estado} onChange={(e) => setEstado(e.target.value)}
            style={{ padding: "8px 12px", border: "1px solid #E2E8F0", borderRadius: 6, fontSize: 13, background: "white" }}>
            <option value="todos">Todos los estados</option>
            <option value="propuesta">Propuesta</option>
            <option value="enviada">Enviada</option>
            <option value="vendida">Vendida</option>
            <option value="rechazada">Rechazada</option>
          </select>
          <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: "#64748B" }}>
            Fecha:
            <input type="date" value={desde} onChange={(e) => setDesde(e.target.value)}
              style={{ padding: "6px 8px", border: "1px solid #E2E8F0", borderRadius: 6, fontSize: 12 }} />
            <span>—</span>
            <input type="date" value={hasta} onChange={(e) => setHasta(e.target.value)}
              style={{ padding: "6px 8px", border: "1px solid #E2E8F0", borderRadius: 6, fontSize: 12 }} />
          </div>
          <button onClick={fetchAll} style={{ padding: "7px 10px", border: "1px solid #E2E8F0", borderRadius: 6, background: "white", cursor: "pointer", color: "#64748B" }}>
            <RefreshCw size={13} />
          </button>
        </div>
      </div>

      {/* Table */}
      <div style={{ background: "white", border: "1px solid #E2E8F0", borderRadius: 10, overflow: "hidden" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
          <thead>
            <tr style={{ background: "#F8FAFC", borderBottom: "1px solid #E2E8F0" }}>
              <th style={{ padding: "10px 8px", width: 32 }} />
              {["N° COT", "Fecha", "Cliente", "Vehículo", "Ítems", "Total", "Estado", "Asesor", "Acciones"].map((h) => (
                <th key={h} style={{ padding: "10px 12px", textAlign: h === "Total" ? "right" : h === "Ítems" ? "center" : h === "Acciones" ? "center" : "left", fontWeight: 600, fontSize: 11, color: "#64748B", textTransform: "uppercase", letterSpacing: 0.5 }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading
              ? <tr><td colSpan={10} style={{ padding: 40, textAlign: "center", color: "#94A3B8" }}>Cargando...</td></tr>
              : items.length === 0
                ? <tr><td colSpan={10} style={{ padding: 40, textAlign: "center", color: "#94A3B8" }}>No se encontraron cotizaciones.</td></tr>
                : items.flatMap((cot) => {
                  const ec = ESTADO_CONFIG[cot.estado] || ESTADO_CONFIG.propuesta;
                  const lc = cot.linea ? LINEA_CONFIG[cot.linea] : null;
                  const isExpanded = expanded.has(cot.id);
                  const det = details[cot.id];

                  const mainRow = (
                    <tr key={cot.id} style={{ borderBottom: isExpanded ? "none" : "1px solid #F1F5F9", background: isExpanded ? "#F8FAFC" : "white" }}>
                      <td style={{ padding: "10px 8px", textAlign: "center" }}>
                        <button onClick={() => toggleExpand(cot.id)}
                          style={{ background: "transparent", border: "none", cursor: "pointer", color: "#94A3B8", padding: 2, display: "flex", alignItems: "center" }}>
                          {isExpanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
                        </button>
                      </td>
                      <td style={{ padding: "10px 12px" }}>
                        <div style={{ fontWeight: 600, color: "#1E293B", fontSize: 12 }}>{cot.numero}</div>
                        {cot.lead_numero && <div style={{ fontSize: 10, color: "#94A3B8" }}>Lead {cot.lead_numero}</div>}
                      </td>
                      <td style={{ padding: "10px 12px", color: "#475569", fontSize: 12 }}>
                        <div>{fmtDate(cot.fecha_creacion)}</div>
                        {cot.fecha_venta && <div style={{ fontSize: 10, color: "#94A3B8" }}>Vendida {fmtDate(cot.fecha_venta)}</div>}
                      </td>
                      <td style={{ padding: "10px 12px" }}>
                        <div style={{ fontWeight: 500, color: "#1E293B" }}>{cot.cliente?.nombre || "—"}</div>
                        {cot.cliente?.rut && <div style={{ fontSize: 11, color: "#94A3B8" }}>{cot.cliente.rut}</div>}
                      </td>
                      <td style={{ padding: "10px 12px" }}>
                        <div style={{ color: "#475569" }}>{cot.vehiculo || "Sin definir"}</div>
                        {lc && cot.linea && (
                          <span style={{ fontSize: 10, background: lc.bg, color: lc.color, padding: "1px 7px", borderRadius: 8, fontWeight: 600 }}>
                            {cot.linea.charAt(0).toUpperCase() + cot.linea.slice(1)}
                          </span>
                        )}
                      </td>
                      <td style={{ padding: "10px 12px", textAlign: "center" }}>
                        <span style={{ background: "#F1F5F9", borderRadius: 10, padding: "2px 10px", fontSize: 12, fontWeight: 600, color: "#475569" }}>{cot.items_count}</span>
                      </td>
                      <td style={{ padding: "10px 12px", textAlign: "right", fontWeight: 600, color: "#1E293B" }}>{fmt(cot.total_bruto)}</td>
                      <td style={{ padding: "10px 12px" }}>
                        <select
                          value={cot.estado}
                          onChange={(e) => {
                            // 'vendida' NO se cierra desde el selector (hallazgo A5): el
                            // cierre exige N° de OC del cliente —referencia 801 del SII—,
                            // la condición de pago y la fecha prometida de entrega, así
                            // que se abre el mismo modal que el botón, que los pide todos.
                            // Y desde 'propuesta'/'rechazada' no se cierra en absoluto:
                            // el backend responde 409 (ESTADOS_QUE_CIERRAN_VENTA), acá se
                            // dice por qué sin gastar el viaje.
                            if (e.target.value === "vendida") {
                              if (!ESTADOS_CON_BOTON_CERRAR.has(cot.estado || "propuesta")) {
                                toast.error(
                                  "Solo una cotización ENVIADA al cliente se puede cerrar como venta: "
                                  + "el cierre libera las compras al proveedor. Márcala primero como Enviada.",
                                  { duration: 8000 });
                                return;
                              }
                              setCerrarVenta(cot);
                              return;
                            }
                            handleEstado(cot.id, e.target.value);
                          }}
                          disabled={updatingEstado === cot.id}
                          style={{ padding: "4px 8px", border: `1px solid ${ec.color}40`, borderRadius: 8, background: ec.bg, color: ec.color, fontSize: 12, fontWeight: 600, cursor: "pointer" }}>
                          {Object.entries(ESTADO_CONFIG).map(([v, c]) => (
                            <option key={v} value={v}>{c.label}</option>
                          ))}
                        </select>
                      </td>
                      <td style={{ padding: "10px 12px", color: "#475569", fontSize: 12 }}>{cot.asesor || "—"}</td>
                      <td style={{ padding: "10px 12px", textAlign: "center" }}>
                        <div style={{ display: "flex", gap: 6, justifyContent: "center", alignItems: "center" }}>
                          {/* Hallazgo A5: solo en los estados desde los que SÍ se puede
                              cerrar. Antes se pintaba con `estado !== "vendida"`, así que
                              también salía en 'propuesta' y 'rechazada' — y un clic mal
                              dado en la fila equivocada disparaba las compras al proveedor. */}
                          {ESTADOS_CON_BOTON_CERRAR.has(cot.estado || "propuesta") && (
                            <button
                              onClick={() => setCerrarVenta(cot)}
                              disabled={markingVendida === cot.id}
                              title="Cerrar la venta (N° de OC del cliente, condición de pago y fecha de entrega)"
                              style={{ background: "transparent", border: "1px solid #16A34A40", borderRadius: 6, cursor: "pointer", color: "#16A34A", padding: "5px 8px", fontSize: 11, fontWeight: 600, display: "flex", alignItems: "center", gap: 4, opacity: markingVendida === cot.id ? 0.5 : 1 }}>
                              <CheckCircle size={12} /> Vendida
                            </button>
                          )}
                          <button
                            onClick={() => handleDownload(cot)}
                            disabled={downloading === cot.id}
                            title="Descargar PDF"
                            style={{ background: "transparent", border: "1px solid #E2E8F0", borderRadius: 6, cursor: "pointer", color: "#475569", padding: "5px 8px", opacity: downloading === cot.id ? 0.5 : 1 }}>
                            <Download size={13} />
                          </button>
                        </div>
                      </td>
                    </tr>
                  );

                  const expandedRow = isExpanded ? (
                    <tr key={`exp-${cot.id}`} style={{ borderBottom: "1px solid #E2E8F0" }}>
                      <td colSpan={10} style={{ padding: 0, background: "#F8FAFC" }}>
                        {loadingDetail === cot.id ? (
                          <div style={{ padding: "20px 24px", color: "#94A3B8", fontSize: 13 }}>Cargando detalle...</div>
                        ) : det ? (
                          <div style={{ padding: "16px 24px 20px" }}>
                            {/* Meta info */}
                            <div style={{ display: "flex", gap: 24, fontSize: 12, color: "#475569", marginBottom: 12, flexWrap: "wrap" }}>
                              {det.vehiculo && <span><strong style={{ color: "var(--monza-accent)" }}>Vehículo:</strong> {det.vehiculo}</span>}
                              {det.vin && <span><strong style={{ color: "var(--monza-accent)" }}>VIN:</strong> {det.vin}</span>}
                              {det.anio && <span><strong style={{ color: "var(--monza-accent)" }}>Año:</strong> {det.anio}</span>}
                              {det.forma_pago && <span><strong style={{ color: "var(--monza-accent)" }}>Pago:</strong> {det.forma_pago}</span>}
                              {det.tipo_cotizacion && <span><strong style={{ color: "var(--monza-accent)" }}>Tipo:</strong> {det.tipo_cotizacion}</span>}
                            </div>
                            {/* Items table */}
                            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12, border: "1px solid #E2E8F0", borderRadius: 8, overflow: "hidden" }}>
                              <thead>
                                <tr style={{ background: "#F5CBA7" }}>
                                  {["Repuesto", "Marca", "Calidad", "QTY", "Precio Unit.", "Total", "Plazo"].map((h) => (
                                    <th key={h} style={{ padding: "7px 10px", textAlign: ["QTY", "Precio Unit.", "Total"].includes(h) ? "right" : "left", fontWeight: 700, color: "#1E293B", fontSize: 11 }}>{h}</th>
                                  ))}
                                </tr>
                              </thead>
                              <tbody>
                                {det.items.map((it, idx) => (
                                  <tr key={it.id} style={{ background: idx % 2 === 0 ? "white" : "#FAFAFA", borderTop: "1px solid #F1F5F9" }}>
                                    <td style={{ padding: "7px 10px", color: "#1E293B", fontWeight: 500 }}>{it.descripcion}</td>
                                    <td style={{ padding: "7px 10px", color: "#475569" }}>{it.marca || "—"}</td>
                                    <td style={{ padding: "7px 10px", color: "#475569" }}>{CALIDAD_LABEL[it.calidad || ""] || it.calidad || "—"}</td>
                                    <td style={{ padding: "7px 10px", textAlign: "right", color: "#475569" }}>{it.cantidad}</td>
                                    <td style={{ padding: "7px 10px", textAlign: "right", color: "#475569" }}>{it.precio_unitario_clp ? fmt(it.precio_unitario_clp) : "—"}</td>
                                    <td style={{ padding: "7px 10px", textAlign: "right", fontWeight: 600, color: "#1E293B" }}>{it.subtotal_clp ? fmt(it.subtotal_clp) : "—"}</td>
                                    <td style={{ padding: "7px 10px", color: "#475569" }}>{it.plazo_entrega || "—"}</td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                            {/* Totals + note */}
                            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginTop: 12, gap: 16 }}>
                              {det.condiciones_servicio && (
                                <div style={{ flex: 1, background: "#FFFBEB", border: "1px solid #FDE68A", borderRadius: 8, padding: "10px 14px", fontSize: 12, color: "#78350F" }}>
                                  <strong>Condiciones:</strong> {det.condiciones_servicio}
                                </div>
                              )}
                              {(det.total_neto || det.total_bruto) && (
                                <div style={{ textAlign: "right", fontSize: 12, color: "#475569", minWidth: 180 }}>
                                  {det.total_neto && <div>Neto: <strong>{fmt(det.total_neto)}</strong></div>}
                                  {det.iva_monto && <div>IVA: <strong>{fmt(det.iva_monto)}</strong></div>}
                                  {det.total_bruto && <div style={{ fontSize: 14, fontWeight: 700, color: "#1E293B", marginTop: 4 }}>Total: {fmt(det.total_bruto)}</div>}
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
          <div style={{ padding: "10px 16px", borderTop: "1px solid #F1F5F9", fontSize: 12, color: "#94A3B8" }}>
            Mostrando {items.length} de {total}
          </div>
        )}
      </div>
      {cerrarVenta && (
        <CerrarVentaModal
          cot={cerrarVenta}
          // Si la fila ya está expandida el detalle está en memoria: se reusa y el modal
          // no pide de nuevo lo mismo.
          detalle={details[cerrarVenta.id]}
          loading={markingVendida === cerrarVenta.id}
          onClose={() => setCerrarVenta(null)}
          onConfirm={(payload) => confirmarVenta(cerrarVenta, payload)}
        />
      )}
    </div>
  );
}
