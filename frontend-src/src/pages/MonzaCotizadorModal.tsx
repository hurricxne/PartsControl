import { useState, useEffect } from "react";
import { X, Plus, Trash2, Calculator, Lock } from "lucide-react";
import { monzaConfigAPI, monzaCotizadorAPI, monzaLeadsAPI } from "../services/monzaApi";
import toast from "react-hot-toast";

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
  tarifa_aerea_por_kg: number;
  moneda_tarifa: string;
  iva_pct: number;
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

function calcularPrecio(costo: number, moneda: string, peso_kg: number, markup_pct: number, cfg: Config) {
  const tc_item   = moneda === "EUR" ? cfg.tc_eur_clp : moneda === "USD" ? cfg.tc_usd_clp : 1;
  const tc_tarifa = cfg.moneda_tarifa === "EUR" ? cfg.tc_eur_clp : cfg.tc_usd_clp;
  const costo_clp = costo * tc_item;
  const flete_clp = peso_kg * cfg.tarifa_aerea_por_kg * tc_tarifa;
  const precio_neto  = (costo_clp + flete_clp) * (1 + markup_pct / 100);
  const precio_bruto = precio_neto * (1 + cfg.iva_pct / 100);
  return { precio_neto: Math.round(precio_neto), precio_bruto: Math.round(precio_bruto) };
}

function fmt(n: number) { return n > 0 ? `$${n.toLocaleString("es-CL")}` : "—"; }
function fmtShort(n: number) { return n >= 1_000_000 ? `$${(n / 1_000_000).toFixed(1)}M` : n >= 1000 ? `$${Math.round(n / 1000)}k` : fmt(n); }

const IS = (extra?: object) => ({
  padding: "5px 8px",
  border: "1px solid #E2E8F0",
  borderRadius: 5,
  fontSize: 12,
  background: "white",
  color: "#1E293B",
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
  onClose: () => void;
  onApplied: () => void;
}

export default function MonzaCotizadorModal({ leadId, leadNumero, clienteNombre, vehiculo, items, onClose, onApplied }: Props) {
  const [cfg, setCfg] = useState<Config | null>(null);
  const [itemsCalculo, setItemsCalculo] = useState<ItemCalculo[]>([]);
  const [applying, setApplying] = useState(false);

  useEffect(() => {
    monzaConfigAPI.get().then((r) => {
      const config = r.data as Config;
      setCfg(config);
      setItemsCalculo(items.map((it) => {
        const calidad = it.calidad
          ? (it.calidad.charAt(0).toUpperCase() + it.calidad.slice(1).toLowerCase()).replace("genuine", "Genuine").replace("oem", "OEM").replace("aftermarket", "Aftermarket")
          : "Genuine";

        if (it.precio_clp && it.precio_clp > 0) {
          // Item ya tiene precio → pre-cargarlo directamente en CLP
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

  const updateCalidad = (itemIdx: number, calIdx: number, field: keyof CalidadRow, value: string) => {
    setItemsCalculo((prev) => {
      const next = [...prev];
      next[itemIdx] = { ...next[itemIdx], calidades: [...next[itemIdx].calidades] };
      const updated = { ...next[itemIdx].calidades[calIdx], [field]: value, preexistente: false };
      next[itemIdx].calidades[calIdx] = updated;
      if (cfg && ["costo", "moneda", "markup_pct"].includes(field)) {
        const cal = next[itemIdx].calidades[calIdx];
        const { precio_neto, precio_bruto } = calcularPrecio(
          Number(cal.costo), cal.moneda, Number(next[itemIdx].peso_kg), Number(cal.markup_pct), cfg
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
          if (!cfg || cal.preexistente) return cal;
          const { precio_neto, precio_bruto } = calcularPrecio(Number(cal.costo), cal.moneda, Number(val), Number(cal.markup_pct), cfg);
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
      await monzaCotizadorAPI.aplicar({ lead_id: leadId, items: payload });
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
      <div style={{ background: "white", borderRadius: 14, width: "100%", maxWidth: 1080, maxHeight: "92vh", display: "flex", flexDirection: "column", boxShadow: "0 24px 64px rgba(0,0,0,0.35)" }}>

        {/* ── Header ─────────────────────────────────────────────────────────── */}
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "16px 22px", borderBottom: "1px solid #E2E8F0" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <div style={{ background: "var(--monza-accent)", borderRadius: 8, padding: 7, display: "flex" }}>
              <Calculator size={17} color="white" />
            </div>
            <div>
              <h2 style={{ margin: 0, fontSize: 16, fontWeight: 700, color: "#1E293B" }}>Calculadora de precios</h2>
              <p style={{ margin: 0, fontSize: 12, color: "#64748B" }}>{leadNumero} · {clienteNombre}{vehiculo ? ` · ${vehiculo}` : ""}</p>
            </div>
          </div>
          <button onClick={onClose} style={{ background: "#F1F5F9", border: "none", cursor: "pointer", color: "#64748B", borderRadius: 8, padding: 7, display: "flex" }}><X size={16} /></button>
        </div>

        {/* ── Config strip ───────────────────────────────────────────────────── */}
        <div style={{ padding: "8px 22px", background: "#F8FAFC", borderBottom: "1px solid #E2E8F0", fontSize: 12, display: "flex", gap: 20, flexWrap: "wrap" }}>
          {[
            { label: "USD→CLP", val: `$${cfg.tc_usd_clp}` },
            { label: "EUR→CLP", val: `$${cfg.tc_eur_clp}` },
            { label: "Flete aéreo", val: `${cfg.tarifa_aerea_por_kg} ${cfg.moneda_tarifa}/kg` },
            { label: "IVA", val: `${cfg.iva_pct}%` },
          ].map(({ label, val }) => (
            <span key={label} style={{ color: "#64748B" }}>
              {label}: <strong style={{ color: "#1E293B" }}>{val}</strong>
            </span>
          ))}
          <span style={{ marginLeft: "auto", fontSize: 11, color: "#94A3B8" }}>
            Para OEM/Aftermarket ingresa el costo a mano · Genuine se puede buscar en catálogo
          </span>
        </div>

        {/* ── Items ──────────────────────────────────────────────────────────── */}
        <div style={{ overflowY: "auto", flex: 1, padding: "18px 22px", display: "flex", flexDirection: "column", gap: 14 }}>
          {itemsCalculo.map((ic, itemIdx) => {
            const hasExisting = ic.item.precio_clp && ic.item.precio_clp > 0;

            return (
              <div key={ic.item.id} style={{
                border: `1px solid ${ic.selected ? "#CBD5E1" : "#E2E8F0"}`,
                borderRadius: 10,
                overflow: "hidden",
                opacity: ic.selected ? 1 : 0.5,
              }}>
                {/* Item header */}
                <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "10px 16px", background: ic.selected ? "#F8FAFC" : "#FAFAFA", borderBottom: ic.selected ? "1px solid #E2E8F0" : "none" }}>
                  <input type="checkbox" checked={ic.selected} onChange={() => toggleItem(itemIdx)}
                    style={{ width: 15, height: 15, accentColor: "var(--monza-accent)", cursor: "pointer" }} />

                  <div style={{ flex: 1, display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
                    <span style={{ fontWeight: 700, fontSize: 13, color: "#1E293B" }}>{ic.item.descripcion.toUpperCase()}</span>
                    <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                      <label style={{ fontSize: 10, color: "#94A3B8", fontWeight: 600 }}>N° parte</label>
                      <input value={ic.numero_parte} onChange={(e) => updateNumeroParte(itemIdx, e.target.value)}
                        placeholder="—"
                        style={{ width: 130, padding: "3px 7px", border: "1px solid #E2E8F0", borderRadius: 5, fontSize: 11, fontFamily: "monospace", background: "white", color: "#1E293B" }} />
                    </span>
                    <span style={{ fontSize: 11, color: "#94A3B8" }}>× {ic.item.cantidad}</span>

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
                    <label style={{ fontSize: 11, color: "#94A3B8" }}>Peso vol. kg:</label>
                    <input
                      type="number" value={ic.peso_kg}
                      onChange={(e) => updatePeso(itemIdx, e.target.value)}
                      style={{ width: 60, padding: "4px 6px", border: "1px solid #E2E8F0", borderRadius: 5, fontSize: 12, textAlign: "right", background: "white", color: "#1E293B" }}
                    />
                  </div>
                </div>

                {/* Detalle flete aéreo Europa */}
                {ic.selected && Number(ic.peso_kg) > 0 && (() => {
                  const tcTarifa = cfg.moneda_tarifa === "EUR" ? cfg.tc_eur_clp : cfg.tc_usd_clp;
                  const fleteMon = Number(ic.peso_kg) * cfg.tarifa_aerea_por_kg;
                  const fleteClp = Math.round(fleteMon * tcTarifa);
                  return (
                    <div style={{ padding: "6px 16px", background: "#EFF6FF", borderBottom: "1px solid #DBEAFE", fontSize: 11, color: "#1D4ED8", display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                      <span style={{ fontWeight: 700 }}>✈ Flete aéreo:</span>
                      <span>{ic.peso_kg} kg vol × {cfg.tarifa_aerea_por_kg} {cfg.moneda_tarifa}/kg = <strong>{fleteMon.toLocaleString("es-CL")} {cfg.moneda_tarifa}</strong></span>
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
                        <div key={h} style={{ fontSize: 10, fontWeight: 700, color: "#94A3B8", letterSpacing: 0.5, textAlign: ["COSTO", "MARGEN %", "PRECIO CLP"].includes(h) ? "right" : "left" }}>
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
                          border: `1px solid ${hasPrice ? "#BBF7D0" : "#E2E8F0"}`,
                          borderLeft: `3px solid ${cs.dot}`,
                          alignItems: "center",
                        }}>
                          {/* Calidad */}
                          <select value={cal.calidad} onChange={(e) => updateCalidad(itemIdx, calIdx, "calidad", e.target.value)}
                            style={{ ...IS({ background: cs.bg, color: cs.color, fontWeight: 600, borderColor: cs.dot }) }}>
                            {CALIDAD_OPTS.map((o) => <option key={o}>{o}</option>)}
                          </select>

                          {/* Marca */}
                          <input value={cal.marca} onChange={(e) => updateCalidad(itemIdx, calIdx, "marca", e.target.value)}
                            placeholder="Marca" style={IS()} />

                          {/* Procedencia */}
                          <input value={cal.procedencia} onChange={(e) => updateCalidad(itemIdx, calIdx, "procedencia", e.target.value)}
                            placeholder="País / origen" style={IS({ fontSize: 11 })} />

                          {/* Costo */}
                          <input type="number" value={cal.costo} onChange={(e) => updateCalidad(itemIdx, calIdx, "costo", e.target.value)}
                            style={IS({ textAlign: "right", fontFamily: "monospace" })} />

                          {/* Moneda */}
                          <select value={cal.moneda} onChange={(e) => updateCalidad(itemIdx, calIdx, "moneda", e.target.value)}
                            style={IS()}>
                            <option>EUR</option>
                            <option>USD</option>
                            <option>CLP</option>
                          </select>

                          {/* Markup */}
                          <div style={{ position: "relative" }}>
                            <input type="number" value={cal.markup_pct} onChange={(e) => updateCalidad(itemIdx, calIdx, "markup_pct", e.target.value)}
                              style={{ ...IS({ textAlign: "right", fontFamily: "monospace", paddingRight: 18 }) }} />
                            <span style={{ position: "absolute", right: 6, top: "50%", transform: "translateY(-50%)", fontSize: 10, color: "#94A3B8" }}>%</span>
                          </div>

                          {/* PRECIO CLP — result */}
                          <div style={{
                            textAlign: "right",
                            fontWeight: 800,
                            fontSize: hasPrice ? 14 : 12,
                            color: hasPrice ? "#15803D" : "#94A3B8",
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
                            style={IS({ fontSize: 11 })} />

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
                      style={{ marginTop: 4, display: "inline-flex", alignItems: "center", gap: 5, background: "transparent", border: "1px dashed #CBD5E1", borderRadius: 6, cursor: "pointer", color: "var(--monza-accent)", fontSize: 12, fontWeight: 600, padding: "4px 12px" }}>
                      <Plus size={12} /> Agregar calidad
                    </button>
                  </div>
                )}
              </div>
            );
          })}
        </div>

        {/* ── Footer ─────────────────────────────────────────────────────────── */}
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "14px 22px", borderTop: "1px solid #E2E8F0", background: "#F8FAFC" }}>
          <div style={{ fontSize: 13, color: "#64748B", display: "flex", alignItems: "center", gap: 16 }}>
            <span><strong style={{ color: "#1E293B" }}>{selectedItems.length}</strong> ítem(s)</span>
            <span style={{ width: 1, height: 16, background: "#E2E8F0", display: "inline-block" }} />
            <span><strong style={{ color: "#1E293B" }}>{itemsConPrecio}</strong> con precio</span>
            {totalNeto > 0 && (
              <>
                <span style={{ width: 1, height: 16, background: "#E2E8F0", display: "inline-block" }} />
                <span>Total estimado: <strong style={{ color: "#15803D", fontSize: 14 }}>{fmt(totalNeto)}</strong>
                  <span style={{ fontSize: 11, color: "#94A3B8" }}> neto · {fmt(Math.round(totalNeto * (1 + cfg.iva_pct / 100)))} c/IVA</span>
                </span>
              </>
            )}
          </div>
          <div style={{ display: "flex", gap: 10 }}>
            <button onClick={onClose}
              style={{ padding: "9px 20px", border: "1px solid #E2E8F0", borderRadius: 8, background: "white", cursor: "pointer", fontSize: 13, color: "#475569", fontWeight: 500 }}>
              Cancelar
            </button>
            <button onClick={handleAplicar} disabled={applying || itemsConPrecio === 0}
              style={{ padding: "9px 22px", border: "none", borderRadius: 8, background: applying || itemsConPrecio === 0 ? "#94A3B8" : "var(--monza-accent)", cursor: applying || itemsConPrecio === 0 ? "not-allowed" : "pointer", fontSize: 13, color: "white", fontWeight: 700 }}>
              {applying ? "Aplicando..." : "Aplicar precios"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
