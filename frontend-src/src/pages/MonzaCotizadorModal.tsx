import { useState, useEffect } from "react";
import { X, Plus, Trash2, Calculator, Lock, AlertTriangle } from "lucide-react";
import { monzaConfigAPI, monzaCotizadorAPI, monzaLeadsAPI } from "../services/monzaApi";
import { useMonzaTheme } from "./MonzaLayout";
import toast from "react-hot-toast";

function useStyles() {
  const { dark } = useMonzaTheme();
  return {
    dark,
    text:    dark ? "#E2E8F0" : "#0f172a",
    text2:   dark ? "#CBD5E1" : "#475569",
    muted:   dark ? "#94A3B8" : "#64748B",
    faint:   dark ? "#6B7A99" : "#94A3B8",
    cardBg:  dark ? "#131b3e" : "white",
    cardBd:  `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}`,
    bd:      dark ? "#1e2a4a" : "#E2E8F0",
    bdSoft:  dark ? "#243350" : "#CBD5E1",
    sub:     dark ? "#0d1430" : "#F8FAFC",
    inputBg: dark ? "#0d1430" : "white",
  };
}

interface LeadItem {
  id: number;
  descripcion: string;
  numero_parte?: string;
  marca?: string;
  procedencia?: string;
  calidad?: string;
  cantidad: number;
  precio_clp?: number;
  plazo_entrega?: string;
  // Parámetros con que la calculadora obtuvo precio_clp (los persiste /cotizador/aplicar
  // y los sirve el detalle del lead). NULL en ítems anteriores a la migración
  // monza_cotizador_parametros: ahí se cae al camino viejo (precio como CLP directo).
  costo?: number | null;
  moneda?: string | null;
  peso_kg?: number | null;
  markup_pct?: number | null;
  tc_aplicado?: number | null;
}

interface CalidadRow {
  calidad: string;
  marca: string;
  procedencia: string;
  costo: string;
  moneda: string;
  markup_pct: string;
  plazo_entrega: string;
  precio_neto?: number;
  precio_bruto?: number;
  /** true = pre-cargado desde precio existente (CLP directo) */
  preexistente?: boolean;
}

interface ItemCalculo {
  item: LeadItem;
  numero_parte: string;
  peso_kg: string;
  calidades: CalidadRow[];
  selected: boolean;
}

interface Config {
  tc_usd_clp: number;
  tc_eur_clp: number;
  /** Tarifa por kilo de CADA moneda (2026-08-08). null = no configurada todavía. */
  tarifa_aerea_eur_por_kg: number | null;
  tarifa_aerea_usd_por_kg: number | null;
  /** Legado: respaldo de `moneda_tarifa_legado` mientras su tarifa nueva no se cargue. */
  tarifa_aerea_por_kg: number;
  /** Moneda del flete de ESTA cotización (en `cfgEfectivo` es la elegida en pantalla). */
  moneda_tarifa: string;
  /**
   * Moneda en la que está expresada la tarifa LEGADA (`tarifa_aerea_por_kg`), tal como
   * viene de Configuración. Existe porque `cfgEfectivo` PISA `moneda_tarifa` con la
   * moneda que el operador eligió: sin este campo, la comparación del respaldo se hacía
   * contra sí misma —una tautología— y el legado se prestaba a cualquier moneda, que es
   * exactamente el defecto que este cambio vino a cerrar (hallazgo del multienjambre
   * 2026-08-08). El backend no tiene el problema porque `_tarifa_configurada` recibe el
   * config crudo.
   */
  moneda_tarifa_legado?: string;
  iva_pct: number;
}

/**
 * Tarifa por kilo que corresponde a esa moneda, o null si no está configurada.
 *
 * Espejo EXACTO de `_tarifa_configurada` (backend/monza_router_cotizador.py): las dos
 * mitades tienen que dar el mismo número o el operador vería un precio en la calculadora
 * y otro al guardar. Incluye el mismo respaldo del legado — la tarifa vieja sirve SOLO
 * para la moneda en que estaba expresada, y para eso compara contra
 * `moneda_tarifa_legado` (la de Configuración), NUNCA contra `moneda_tarifa`, que en el
 * config efectivo ya es la moneda elegida.
 *
 * null (no configurada) ≠ 0 (flete gratis): con null la pantalla avisa y bloquea, en vez
 * de cotizar sin flete y subvaluar la venta.
 */
function tarifaEfectiva(cfg: Config, moneda: string): number | null {
  const propia = moneda === "EUR" ? cfg.tarifa_aerea_eur_por_kg : cfg.tarifa_aerea_usd_por_kg;
  if (propia != null) return propia;
  const monedaLegado = (cfg.moneda_tarifa_legado || cfg.moneda_tarifa || "EUR").toUpperCase();
  if (monedaLegado === moneda && cfg.tarifa_aerea_por_kg != null) {
    return cfg.tarifa_aerea_por_kg;
  }
  return null;
}

const CALIDAD_OPTS = ["sin_calificar", "Genuine", "OEM", "Aftermarket", "Remanufacturado", "Usado"];

const CALIDAD_STYLE: Record<string, { bg: string; color: string; dot: string }> = {
  Genuine:        { bg: "#EFF6FF", color: "#1D4ED8", dot: "#3B82F6" },
  OEM:            { bg: "#FFFBEB", color: "#B45309", dot: "#F59E0B" },
  Aftermarket:    { bg: "#F0FDF4", color: "#15803D", dot: "#22C55E" },
  Remanufacturado:{ bg: "#F5F3FF", color: "#6D28D9", dot: "#8B5CF6" },
  Usado:          { bg: "#FEF2F2", color: "#B91C1C", dot: "#EF4444" },
  sin_calificar:  { bg: "#F8FAFC", color: "#64748B", dot: "#94A3B8" },
};

function getCalStyle(calidad: string) {
  return CALIDAD_STYLE[calidad] || CALIDAD_STYLE.sin_calificar;
}

/**
 * Precio unitario neto y bruto de una fila.
 *
 * `cfg` es SIEMPRE el config EFECTIVO (el global con la moneda del flete de esta
 * cotización encima, ver `cfgEfectivo`), así que la moneda del flete se lee de
 * `cfg.moneda_tarifa` y de ahí sale la tarifa por kilo que corresponde. Resolver la
 * tarifa AQUÍ —y no en cada llamador— es lo que impide que la pantalla calcule con una
 * tarifa y muestre otra: es el mismo criterio de fuente única del backend.
 *
 * Sin tarifa para esa moneda devuelve 0: la pantalla lo muestra como "—" junto al aviso
 * que explica qué falta, y el botón de aplicar queda bloqueado (el backend además lo
 * rechaza con 400 — dos cinturones, porque acá se define un precio de venta).
 */
function calcularPrecio(costo: number, moneda: string, peso_kg: number, markup_pct: number, cfg: Config) {
  const tc_item   = moneda === "EUR" ? cfg.tc_eur_clp : moneda === "USD" ? cfg.tc_usd_clp : 1;
  const monedaFlete = (cfg.moneda_tarifa || "EUR").toUpperCase();
  const tc_tarifa = monedaFlete === "EUR" ? cfg.tc_eur_clp : cfg.tc_usd_clp;
  const tarifa_kg = tarifaEfectiva(cfg, monedaFlete);
  if (tarifa_kg == null) return { precio_neto: 0, precio_bruto: 0 };
  const costo_clp = costo * tc_item;
  const flete_clp = peso_kg * tarifa_kg * tc_tarifa;
  const precio_neto  = (costo_clp + flete_clp) * (1 + markup_pct / 100);
  const precio_bruto = precio_neto * (1 + cfg.iva_pct / 100);
  return { precio_neto: Math.round(precio_neto), precio_bruto: Math.round(precio_bruto) };
}

function fmt(n: number) { return n > 0 ? `$${n.toLocaleString("es-CL")}` : "—"; }
function fmtShort(n: number) { return n >= 1_000_000 ? `$${(n / 1_000_000).toFixed(1)}M` : n >= 1000 ? `$${Math.round(n / 1000)}k` : fmt(n); }

const IS = (s: ReturnType<typeof useStyles>, extra?: object) => ({
  padding: "5px 8px",
  border: s.cardBd,
  borderRadius: 5,
  fontSize: 12,
  background: s.inputBg,
  color: s.text,
  width: "100%",
  boxSizing: "border-box" as const,
  ...extra,
});

interface Props {
  leadId: number;
  leadNumero: string;
  clienteNombre: string;
  vehiculo?: string;
  items: LeadItem[];
  /** Moneda del flete guardada en el lead (EUR/USD); null = usar la global. */
  monedaTarifaGuardada?: string | null;
  /**
   * Tarifa por kilo CONGELADA en el lead cuando se calcularon sus precios.
   * Mientras el operador no cambie de moneda, manda ésta y no la vigente en
   * Configuración: reabrir la calculadora no puede mover un precio ya ofrecido al
   * cliente. Al cambiar de moneda se pasa a la tarifa vigente de esa moneda, porque ahí
   * el operador está recalculando a propósito.
   */
  tarifaAereaGuardada?: number | null;
  onClose: () => void;
  onApplied: () => void;
}

export default function MonzaCotizadorModal({ leadId, leadNumero, clienteNombre, vehiculo, items, monedaTarifaGuardada, tarifaAereaGuardada, onClose, onApplied }: Props) {
  const s = useStyles();
  const [cfg, setCfg] = useState<Config | null>(null);
  const [itemsCalculo, setItemsCalculo] = useState<ItemCalculo[]>([]);
  const [applying, setApplying] = useState(false);
  // Moneda del flete de ESTA cotización (una para todos los ítems, decisión del dueño).
  // Parte de lo que el lead ya tiene guardado; si nunca se eligió, de la config global
  // en cuanto llegue. Al cambiar, `cfgEfectivo` recalcula el desglose en vivo.
  const [monedaFlete, setMonedaFlete] = useState<string>(monedaTarifaGuardada || "");

  const monedaElegida = (monedaFlete || cfg?.moneda_tarifa || "EUR").toUpperCase();
  // La tarifa CONGELADA del lead manda mientras la moneda siga siendo la que se usó al
  // calcular sus precios: reabrir la calculadora no puede mover un precio ya ofrecido al
  // cliente. Es la misma regla del backend (_flete_efectivo, donde la tarifa explícita
  // gana sobre la configuración). Al cambiar de moneda se pasa a la vigente, porque ahí
  // el operador está recalculando a propósito.
  const conservaLaGuardada = tarifaAereaGuardada != null
    && (monedaTarifaGuardada || "").toUpperCase() === monedaElegida;

  // Config EFECTIVO de esta cotización. TODO cálculo y TODO desglose usan éste — si
  // usaran el global, la pantalla calcularía con una moneda y diría otra. Lleva:
  //  · `moneda_tarifa` = la moneda elegida;
  //  · `moneda_tarifa_legado` = la de Configuración, contra la que `tarifaEfectiva`
  //    decide si puede usar la tarifa vieja (sin conservarla, la comparación se hacía
  //    contra el valor ya pisado —tautología— y el legado se prestaba a otra moneda);
  //  · la tarifa CONGELADA del lead inyectada en la columna de SU moneda, para que el
  //    precio calculado y el desglose mostrado salgan del MISMO número.
  const cfgEfectivo: Config | null = cfg
    ? {
      ...cfg,
      moneda_tarifa: monedaFlete || cfg.moneda_tarifa,
      moneda_tarifa_legado: cfg.moneda_tarifa,
      ...(conservaLaGuardada
        ? (monedaElegida === "EUR"
          ? { tarifa_aerea_eur_por_kg: tarifaAereaGuardada as number }
          : { tarifa_aerea_usd_por_kg: tarifaAereaGuardada as number })
        : {}),
    }
    : null;

  // Tarifa por kilo que rige esta cotización (null = no hay con qué cotizar en esta
  // moneda). Una sola variable para los tres usos —desglose, precio y lo que se congela
  // al aplicar— para que no puedan divergir.
  const tarifaFlete = cfgEfectivo ? tarifaEfectiva(cfgEfectivo, monedaElegida) : null;
  const faltaTarifa = cfgEfectivo != null && tarifaFlete == null;

  useEffect(() => {
    monzaConfigAPI.get().then((r) => {
      const config = r.data as Config;
      setCfg(config);
      setMonedaFlete((prev) => prev || config.moneda_tarifa || "EUR");
      setItemsCalculo(items.map((it) => {
        const calidad = it.calidad
          ? (it.calidad.charAt(0).toUpperCase() + it.calidad.slice(1).toLowerCase()).replace("genuine", "Genuine").replace("oem", "OEM").replace("aftermarket", "Aftermarket")
          : "Genuine";

        // EL BUG QUE ESTO CIERRA: el camino viejo metía el PRECIO FINAL en el campo del
        // costo y forzaba moneda CLP con markup 0 — «guardo en USD y al reabrir me
        // muestra CLP, y en el costo lo mismo que en el precio». Ahora, si el ítem trae
        // sus parámetros guardados (costo en su moneda, peso, markup), se RESTAURAN tal
        // cual el operador los dejó y la pantalla se reconstruye de verdad.
        if (it.precio_clp && it.precio_clp > 0 && it.costo != null && it.moneda) {
          return {
            item: it,
            numero_parte: it.numero_parte || "",
            peso_kg: (it.peso_kg ?? 0).toString(),
            selected: true,
            calidades: [{
              calidad,
              marca: it.marca || "",
              procedencia: it.procedencia || "",
              costo: it.costo.toString(),
              moneda: it.moneda,
              markup_pct: (it.markup_pct ?? 0).toString(),
              plazo_entrega: it.plazo_entrega || "",
              precio_neto: it.precio_clp,
              precio_bruto: Math.round(it.precio_clp * (1 + config.iva_pct / 100)),
              preexistente: true,
            }],
          };
        }

        if (it.precio_clp && it.precio_clp > 0) {
          // Ítem con precio pero SIN parámetros guardados (anterior a la migración
          // monza_cotizador_parametros): no hay con qué reconstruir el cálculo, así que
          // se muestra el precio como CLP directo. Es el único caso en que este camino
          // sigue siendo legítimo — apenas se re-aplique una vez, gana parámetros y entra
          // al camino de arriba.
          return {
            item: it,
            numero_parte: it.numero_parte || "",
            peso_kg: "0",
            selected: true,
            calidades: [{
              calidad,
              marca: it.marca || "",
              procedencia: it.procedencia || "",
              costo: it.precio_clp.toString(),
              moneda: "CLP",
              markup_pct: "0",
              plazo_entrega: it.plazo_entrega || "",
              precio_neto: it.precio_clp,
              precio_bruto: Math.round(it.precio_clp * (1 + config.iva_pct / 100)),
              preexistente: true,
            }],
          };
        }

        // Sin precio previo → defaults
        return {
          item: it,
          numero_parte: it.numero_parte || "",
          peso_kg: "0",
          selected: true,
          calidades: [{
            calidad,
            marca: it.marca || "",
            procedencia: it.procedencia || "Alemania",
            costo: "0",
            moneda: "EUR",
            markup_pct: "28",
            plazo_entrega: it.plazo_entrega || "",
            preexistente: false,
          }],
        };
      }));
    });
  }, []);

  // Cambiar la moneda del flete recalcula EN VIVO todas las filas con costo cargado:
  // sin esto, el selector cambiaba el desglose mostrado pero los precios quedaban con la
  // moneda anterior hasta tocar cada fila — dos verdades en la misma pantalla.
  // Usa `cfgEfectivo`, el MISMO objeto que el desglose y el guardado: armar otro acá
  // era una tercera verdad que ya se desincronizó una vez (la moneda del legado) y que
  // además ignoraría la tarifa congelada del lead.
  useEffect(() => {
    if (!cfgEfectivo || !monedaFlete) return;
    setItemsCalculo((prev) => prev.map((ic) => ({
      ...ic,
      calidades: ic.calidades.map((cal) => {
        if (cal.preexistente || !(Number(cal.costo) > 0)) return cal;
        const { precio_neto, precio_bruto } = calcularPrecio(
          Number(cal.costo), cal.moneda, Number(ic.peso_kg), Number(cal.markup_pct), cfgEfectivo);
        return { ...cal, precio_neto, precio_bruto };
      }),
    })));
    // eslint-disable-next-line react-hooks/exhaustive-deps -- cfgEfectivo se re-crea en
    // cada render (objeto derivado): depender de él dispararía el efecto en bucle. Sus
    // dos entradas reales son las de abajo.
  }, [monedaFlete, cfg]);

  const updateCalidad = (itemIdx: number, calIdx: number, field: keyof CalidadRow, value: string) => {
    setItemsCalculo((prev) => {
      const next = [...prev];
      next[itemIdx] = { ...next[itemIdx], calidades: [...next[itemIdx].calidades] };
      const updated = { ...next[itemIdx].calidades[calIdx], [field]: value, preexistente: false };
      next[itemIdx].calidades[calIdx] = updated;
      if (cfgEfectivo && ["costo", "moneda", "markup_pct"].includes(field)) {
        const cal = next[itemIdx].calidades[calIdx];
        const { precio_neto, precio_bruto } = calcularPrecio(
          Number(cal.costo), cal.moneda, Number(next[itemIdx].peso_kg), Number(cal.markup_pct), cfgEfectivo
        );
        next[itemIdx].calidades[calIdx].precio_neto  = precio_neto;
        next[itemIdx].calidades[calIdx].precio_bruto = precio_bruto;
      }
      return next;
    });
  };

  const updateNumeroParte = (itemIdx: number, val: string) => {
    setItemsCalculo((prev) => {
      const next = [...prev];
      next[itemIdx] = { ...next[itemIdx], numero_parte: val };
      return next;
    });
  };

  const updatePeso = (itemIdx: number, val: string) => {
    setItemsCalculo((prev) => {
      const next = [...prev];
      next[itemIdx] = {
        ...next[itemIdx],
        peso_kg: val,
        calidades: next[itemIdx].calidades.map((cal) => {
          if (!cfgEfectivo || cal.preexistente) return cal;
          const { precio_neto, precio_bruto } = calcularPrecio(Number(cal.costo), cal.moneda, Number(val), Number(cal.markup_pct), cfgEfectivo);
          return { ...cal, precio_neto, precio_bruto };
        }),
      };
      return next;
    });
  };

  const addCalidad = (itemIdx: number) => {
    setItemsCalculo((prev) => {
      const next = [...prev];
      next[itemIdx] = {
        ...next[itemIdx],
        calidades: [...next[itemIdx].calidades, {
          calidad: "OEM", marca: "", procedencia: "", costo: "0",
          moneda: "EUR", markup_pct: "28", plazo_entrega: "", preexistente: false,
        }],
      };
      return next;
    });
  };

  const removeCalidad = (itemIdx: number, calIdx: number) => {
    setItemsCalculo((prev) => {
      const next = [...prev];
      next[itemIdx] = { ...next[itemIdx], calidades: next[itemIdx].calidades.filter((_, i) => i !== calIdx) };
      return next;
    });
  };

  const toggleItem = (idx: number) => {
    setItemsCalculo((prev) => {
      const next = [...prev];
      next[idx] = { ...next[idx], selected: !next[idx].selected };
      return next;
    });
  };

  const selectedItems = itemsCalculo.filter((ic) => ic.selected);
  const totalNeto = selectedItems.reduce((sum, ic) => {
    const first = ic.calidades.find((c) => (c.precio_neto || 0) > 0);
    return sum + (first?.precio_neto || 0) * ic.item.cantidad;
  }, 0);
  const itemsConPrecio = selectedItems.filter((ic) => ic.calidades.some((c) => (c.precio_neto || 0) > 0)).length;

  const handleAplicar = async () => {
    if (!cfg) return;
    const selected = itemsCalculo.filter((ic) => ic.selected);

    const payload = selected.map((ic) => {
      const primera = ic.calidades.find((c) => (c.precio_neto || 0) > 0 || Number(c.costo) > 0) || ic.calidades[0];
      return {
        item_id: ic.item.id,
        calidad: primera.calidad.toLowerCase(),
        marca: primera.marca,
        procedencia: primera.procedencia,
        precio_clp: primera.precio_neto || 0,
        plazo_entrega: primera.plazo_entrega || undefined,
        numero_parte: ic.numero_parte || undefined,
        // Los PARÁMETROS del cálculo viajan con el precio: sin ellos el backend no tiene
        // qué guardar y al reabrir la calculadora se repetía el bug del costo=precio en
        // CLP. `Number(...)` porque los campos son strings de input.
        costo: Number(primera.costo) || 0,
        moneda: primera.moneda,
        peso_kg: Number(ic.peso_kg) || 0,
        markup_pct: Number(primera.markup_pct) || 0,
      };
    });

    const extras: Array<{ ic: typeof selected[0]; cal: CalidadRow }> = [];
    for (const ic of selected) {
      const firstValidIdx = ic.calidades.findIndex((c) => (c.precio_neto || 0) > 0 || Number(c.costo) > 0);
      ic.calidades.forEach((cal, idx) => {
        if (idx !== firstValidIdx && ((cal.precio_neto || 0) > 0 || Number(cal.costo) > 0)) {
          extras.push({ ic, cal });
        }
      });
    }

    setApplying(true);
    try {
      await monzaCotizadorAPI.aplicar({
        lead_id: leadId, items: payload,
        // El flete elegido para ESTA cotización (uno para todos los ítems): queda
        // guardado en el lead y las próximas aperturas parten de él.
        // La tarifa que viaja es la de la MONEDA elegida, no la del legado: es la que
        // se usó para calcular en pantalla y la que queda CONGELADA en el lead, de modo
        // que un cambio posterior en Configuración no mueva un precio ya ofrecido.
        moneda_tarifa: monedaFlete, tarifa_aerea: tarifaFlete,
      });
      for (const { ic, cal } of extras) {
        await monzaLeadsAPI.addItem(leadId, {
          descripcion: ic.item.descripcion,
          numero_parte: ic.numero_parte || ic.item.numero_parte || undefined,
          marca: cal.marca || ic.item.marca || undefined,
          procedencia: cal.procedencia || ic.item.procedencia || undefined,
          calidad: cal.calidad.toLowerCase(),
          cantidad: ic.item.cantidad,
          precio_clp: cal.precio_neto || 0,
          plazo_entrega: cal.plazo_entrega || undefined,
        });
      }
      const extraMsg = extras.length > 0 ? ` + ${extras.length} calidad(es) extra creada(s)` : "";
      toast.success(`Precios aplicados al lead${extraMsg}`);
      onApplied();
      onClose();
    } catch { toast.error("Error al aplicar precios"); }
    finally { setApplying(false); }
  };

  if (!cfg) return null;

  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.55)", zIndex: 1000, display: "flex", alignItems: "center", justifyContent: "center", padding: 16 }}>
      <div style={{ background: s.cardBg, borderRadius: 14, width: "100%", maxWidth: 1080, maxHeight: "92vh", display: "flex", flexDirection: "column", boxShadow: "0 24px 64px rgba(0,0,0,0.35)" }}>

        {/* ── Header ─────────────────────────────────────────────────────────── */}
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "16px 22px", borderBottom: s.cardBd }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <div style={{ background: "var(--monza-accent)", borderRadius: 8, padding: 7, display: "flex" }}>
              <Calculator size={17} color="white" />
            </div>
            <div>
              <h2 style={{ margin: 0, fontSize: 16, fontWeight: 700, color: s.text }}>Calculadora de precios</h2>
              <p style={{ margin: 0, fontSize: 12, color: s.muted }}>{leadNumero} · {clienteNombre}{vehiculo ? ` · ${vehiculo}` : ""}</p>
            </div>
          </div>
          <button onClick={onClose} style={{ background: s.sub, border: "none", cursor: "pointer", color: s.muted, borderRadius: 8, padding: 7, display: "flex" }}><X size={16} /></button>
        </div>

        {/* ── Config strip ───────────────────────────────────────────────────── */}
        <div style={{ padding: "8px 22px", background: s.sub, borderBottom: s.cardBd, fontSize: 12, display: "flex", gap: 20, flexWrap: "wrap" }}>
          {[
            { label: "USD→CLP", val: `$${cfg.tc_usd_clp}` },
            { label: "EUR→CLP", val: `$${cfg.tc_eur_clp}` },
            { label: "IVA", val: `${cfg.iva_pct}%` },
          ].map(({ label, val }) => (
            <span key={label} style={{ color: s.muted }}>
              {label}: <strong style={{ color: s.text }}>{val}</strong>
            </span>
          ))}
          {/* La moneda del flete es ELEGIBLE por cotización (antes: fija de la config
              global, sin forma de pedir dólares). Cambiarla recalcula el desglose en
              vivo y queda guardada en el lead al aplicar. */}
          <span style={{ color: s.muted, display: "inline-flex", alignItems: "center", gap: 6 }}>
            {/* La tarifa mostrada es la de la MONEDA elegida: cambiar el selector cambia
                el precio por kilo, no solo la etiqueta (cada moneda tiene su tarifa). */}
            Flete aéreo: <strong style={{ color: faltaTarifa ? "#B45309" : s.text }}>{tarifaFlete ?? "sin tarifa"}</strong>
            <select value={monedaFlete} onChange={(e) => setMonedaFlete(e.target.value)}
              style={{ background: s.inputBg, color: s.text, border: s.cardBd, borderRadius: 6, padding: "2px 6px", fontSize: 12, fontFamily: "inherit" }}>
              <option value="EUR">EUR</option>
              <option value="USD">USD</option>
            </select>/kg
          </span>
          <span style={{ marginLeft: "auto", fontSize: 11, color: s.faint }}>
            Para OEM/Aftermarket ingresa el costo a mano · Genuine se puede buscar en catálogo
          </span>
        </div>

        {/* Sin tarifa para la moneda elegida NO se cotiza: el flete quedaría en 0 y la
            venta saldría subvaluada sin que se note. Se avisa acá y se bloquea Aplicar
            (el backend además responde 400: el precio de venta lleva dos cinturones). */}
        {faltaTarifa && (
          <div style={{ padding: "8px 22px", background: "rgba(245,158,11,0.12)", borderBottom: s.cardBd,
            fontSize: 12, color: "#B45309", display: "flex", alignItems: "center", gap: 8 }}>
            <AlertTriangle size={14} style={{ flexShrink: 0 }} />
            <span>
              No hay <strong>tarifa aérea en {(monedaFlete || "").toUpperCase()}</strong> configurada:
              los precios no se pueden calcular con esta moneda. Cárgala en <strong>Configuración →
              Tarifa aérea por kg ({(monedaFlete || "").toUpperCase()})</strong>, o elige la otra moneda.
            </span>
          </div>
        )}

        {/* ── Items ──────────────────────────────────────────────────────────── */}
        <div style={{ overflowY: "auto", flex: 1, padding: "18px 22px", display: "flex", flexDirection: "column", gap: 14 }}>
          {itemsCalculo.map((ic, itemIdx) => {
            const hasExisting = ic.item.precio_clp && ic.item.precio_clp > 0;

            return (
              <div key={ic.item.id} style={{
                border: `1px solid ${ic.selected ? s.bdSoft : s.bd}`,
                borderRadius: 10,
                overflow: "hidden",
                opacity: ic.selected ? 1 : 0.5,
              }}>
                {/* Item header */}
                <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "10px 16px", background: s.sub, borderBottom: ic.selected ? s.cardBd : "none" }}>
                  <input type="checkbox" checked={ic.selected} onChange={() => toggleItem(itemIdx)}
                    style={{ width: 15, height: 15, accentColor: "var(--monza-accent)", cursor: "pointer" }} />

                  <div style={{ flex: 1, display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
                    <span style={{ fontWeight: 700, fontSize: 13, color: s.text }}>{ic.item.descripcion.toUpperCase()}</span>
                    <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                      <label style={{ fontSize: 10, color: s.faint, fontWeight: 600 }}>N° parte</label>
                      <input value={ic.numero_parte} onChange={(e) => updateNumeroParte(itemIdx, e.target.value)}
                        placeholder="—"
                        style={{ width: 130, padding: "3px 7px", border: s.cardBd, borderRadius: 5, fontSize: 11, fontFamily: "monospace", background: s.inputBg, color: s.text }} />
                    </span>
                    <span style={{ fontSize: 11, color: s.faint }}>× {ic.item.cantidad}</span>

                    {hasExisting ? (
                      <span style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: 11, background: "#DCFCE7", color: "#15803D", padding: "2px 9px", borderRadius: 20, fontWeight: 700, border: "1px solid #BBF7D0" }}>
                        <Lock size={9} /> precio actual {fmt(ic.item.precio_clp!)} · cargado
                      </span>
                    ) : (
                      <span style={{ fontSize: 11, color: "#F59E0B", background: "#FFFBEB", padding: "2px 9px", borderRadius: 20, border: "1px solid #FDE68A", fontWeight: 600 }}>
                        sin precio
                      </span>
                    )}
                  </div>

                  <div style={{ display: "flex", alignItems: "center", gap: 6, flexShrink: 0 }}>
                    <label style={{ fontSize: 11, color: s.faint }}>Peso vol. kg:</label>
                    <input
                      type="number" value={ic.peso_kg}
                      onChange={(e) => updatePeso(itemIdx, e.target.value)}
                      style={{ width: 60, padding: "4px 6px", border: s.cardBd, borderRadius: 5, fontSize: 12, textAlign: "right", background: s.inputBg, color: s.text }}
                    />
                  </div>
                </div>

                {/* Detalle del flete aéreo de la fila. Usa `tarifaFlete` —la tarifa de la
                    moneda ELEGIDA— para que el desglose sea exactamente la cuenta que
                    calcularPrecio metió en el precio. Con la tarifa sin configurar el
                    bloque no se pinta: el aviso de arriba ya explica qué falta. */}
                {ic.selected && Number(ic.peso_kg) > 0 && tarifaFlete != null && (() => {
                  const tcTarifa = cfgEfectivo!.moneda_tarifa === "EUR" ? cfg.tc_eur_clp : cfg.tc_usd_clp;
                  const fleteMon = Number(ic.peso_kg) * tarifaFlete;
                  const fleteClp = Math.round(fleteMon * tcTarifa);
                  return (
                    <div style={{ padding: "6px 16px", background: "#EFF6FF", borderBottom: "1px solid #DBEAFE", fontSize: 11, color: "#1D4ED8", display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                      <span style={{ fontWeight: 700 }}>✈ Flete aéreo:</span>
                      <span>{ic.peso_kg} kg vol × {tarifaFlete} {cfgEfectivo!.moneda_tarifa}/kg = <strong>{fleteMon.toLocaleString("es-CL")} {cfgEfectivo!.moneda_tarifa}</strong></span>
                      <span style={{ color: "#64748B" }}>→ ${fleteClp.toLocaleString("es-CL")} CLP (incluido en cada precio)</span>
                    </div>
                  );
                })()}

                {/* Calidad rows */}
                {ic.selected && (
                  <div style={{ padding: "10px 16px 12px" }}>
                    {/* Column headers */}
                    <div style={{ display: "grid", gridTemplateColumns: "118px 110px 108px 86px 70px 72px 120px 150px 28px", gap: 6, marginBottom: 4, padding: "0 2px" }}>
                      {["CALIDAD", "MARCA", "PROCEDENCIA", "COSTO", "MONEDA", "MARGEN %", "PRECIO CLP", "PLAZO ENTREGA", ""].map((h) => (
                        <div key={h} style={{ fontSize: 10, fontWeight: 700, color: s.faint, letterSpacing: 0.5, textAlign: ["COSTO", "MARGEN %", "PRECIO CLP"].includes(h) ? "right" : "left" }}>
                          {h}
                        </div>
                      ))}
                    </div>

                    {ic.calidades.map((cal, calIdx) => {
                      const cs = getCalStyle(cal.calidad);
                      const hasPrice = (cal.precio_neto || 0) > 0;
                      return (
                        <div key={calIdx} style={{
                          display: "grid",
                          gridTemplateColumns: "118px 110px 108px 86px 70px 72px 120px 150px 28px",
                          gap: 6,
                          marginBottom: 6,
                          padding: "8px 10px",
                          background: cs.bg,
                          borderRadius: 8,
                          border: `1px solid ${hasPrice ? "#BBF7D0" : s.bd}`,
                          borderLeft: `3px solid ${cs.dot}`,
                          alignItems: "center",
                        }}>
                          {/* Calidad */}
                          <select value={cal.calidad} onChange={(e) => updateCalidad(itemIdx, calIdx, "calidad", e.target.value)}
                            style={{ ...IS(s, { background: cs.bg, color: cs.color, fontWeight: 600, borderColor: cs.dot }) }}>
                            {CALIDAD_OPTS.map((o) => <option key={o}>{o}</option>)}
                          </select>

                          {/* Marca */}
                          <input value={cal.marca} onChange={(e) => updateCalidad(itemIdx, calIdx, "marca", e.target.value)}
                            placeholder="Marca" style={IS(s)} />

                          {/* Procedencia */}
                          <input value={cal.procedencia} onChange={(e) => updateCalidad(itemIdx, calIdx, "procedencia", e.target.value)}
                            placeholder="País / origen" style={IS(s, { fontSize: 11 })} />

                          {/* Costo */}
                          <input type="number" value={cal.costo} onChange={(e) => updateCalidad(itemIdx, calIdx, "costo", e.target.value)}
                            style={IS(s, { textAlign: "right", fontFamily: "monospace" })} />

                          {/* Moneda */}
                          <select value={cal.moneda} onChange={(e) => updateCalidad(itemIdx, calIdx, "moneda", e.target.value)}
                            style={IS(s)}>
                            <option>EUR</option>
                            <option>USD</option>
                            <option>CLP</option>
                          </select>

                          {/* Markup */}
                          <div style={{ position: "relative" }}>
                            <input type="number" value={cal.markup_pct} onChange={(e) => updateCalidad(itemIdx, calIdx, "markup_pct", e.target.value)}
                              style={{ ...IS(s, { textAlign: "right", fontFamily: "monospace", paddingRight: 18 }) }} />
                            <span style={{ position: "absolute", right: 6, top: "50%", transform: "translateY(-50%)", fontSize: 10, color: s.faint }}>%</span>
                          </div>

                          {/* PRECIO CLP — result */}
                          <div style={{
                            textAlign: "right",
                            fontWeight: 800,
                            fontSize: hasPrice ? 14 : 12,
                            color: hasPrice ? "#15803D" : s.faint,
                            fontFamily: "monospace",
                            padding: "4px 6px",
                            background: hasPrice ? "#F0FDF4" : "transparent",
                            borderRadius: 6,
                            border: hasPrice ? "1px solid #BBF7D0" : "none",
                          }}>
                            {hasPrice ? fmt(cal.precio_neto!) : "—"}
                            {hasPrice && cal.preexistente && (
                              <div style={{ fontSize: 9, fontWeight: 400, color: "#16A34A", fontFamily: "sans-serif" }}>actual</div>
                            )}
                          </div>

                          {/* Plazo */}
                          <input value={cal.plazo_entrega} onChange={(e) => updateCalidad(itemIdx, calIdx, "plazo_entrega", e.target.value)}
                            placeholder="ej: 7-10 días hábiles"
                            style={IS(s, { fontSize: 11 })} />

                          {/* Delete */}
                          <div style={{ textAlign: "center" }}>
                            {ic.calidades.length > 1 ? (
                              <button onClick={() => removeCalidad(itemIdx, calIdx)}
                                style={{ background: "transparent", border: "none", cursor: "pointer", color: "#EF4444", padding: 2, display: "flex" }}>
                                <Trash2 size={13} />
                              </button>
                            ) : null}
                          </div>
                        </div>
                      );
                    })}

                    <button onClick={() => addCalidad(itemIdx)}
                      style={{ marginTop: 4, display: "inline-flex", alignItems: "center", gap: 5, background: "transparent", border: `1px dashed ${s.bdSoft}`, borderRadius: 6, cursor: "pointer", color: "var(--monza-accent)", fontSize: 12, fontWeight: 600, padding: "4px 12px" }}>
                      <Plus size={12} /> Agregar calidad
                    </button>
                  </div>
                )}
              </div>
            );
          })}
        </div>

        {/* ── Footer ─────────────────────────────────────────────────────────── */}
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "14px 22px", borderTop: s.cardBd, background: s.sub }}>
          <div style={{ fontSize: 13, color: s.muted, display: "flex", alignItems: "center", gap: 16 }}>
            <span><strong style={{ color: s.text }}>{selectedItems.length}</strong> ítem(s)</span>
            <span style={{ width: 1, height: 16, background: s.bd, display: "inline-block" }} />
            <span><strong style={{ color: s.text }}>{itemsConPrecio}</strong> con precio</span>
            {totalNeto > 0 && (
              <>
                <span style={{ width: 1, height: 16, background: s.bd, display: "inline-block" }} />
                <span>Total estimado: <strong style={{ color: "#15803D", fontSize: 14 }}>{fmt(totalNeto)}</strong>
                  <span style={{ fontSize: 11, color: s.faint }}> neto · {fmt(Math.round(totalNeto * (1 + cfg.iva_pct / 100)))} c/IVA</span>
                </span>
              </>
            )}
          </div>
          <div style={{ display: "flex", gap: 10 }}>
            <button onClick={onClose}
              style={{ padding: "9px 20px", border: s.cardBd, borderRadius: 8, background: s.cardBg, cursor: "pointer", fontSize: 13, color: s.text2, fontWeight: 500 }}>
              Cancelar
            </button>
            {/* `faltaTarifa` bloquea igual que "sin precios": sin la tarifa de la moneda
                elegida, todo precio calculado sería un flete de 0 disfrazado. */}
            <button onClick={handleAplicar} disabled={applying || itemsConPrecio === 0 || faltaTarifa}
              title={faltaTarifa ? `Falta la tarifa aérea en ${(monedaFlete || "").toUpperCase()} (Configuración)` : undefined}
              style={{ padding: "9px 22px", border: "none", borderRadius: 8, background: applying || itemsConPrecio === 0 || faltaTarifa ? s.faint : "var(--monza-accent)", cursor: applying || itemsConPrecio === 0 || faltaTarifa ? "not-allowed" : "pointer", fontSize: 13, color: "white", fontWeight: 700 }}>
              {applying ? "Aplicando..." : "Aplicar precios"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
