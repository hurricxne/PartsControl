import { useState, useEffect, useCallback } from "react";
import { ShoppingCart, Search, RefreshCw, Package, X, Truck, FileText, ChevronDown, ChevronRight, Plus, AlertCircle } from "lucide-react";
// (Truck también marca el badge "Nacional": OC sin embarque, entrega por camión)
import { monzaAbastecimientoAPI, monzaTotalPendiente, monzaErrMsg } from "../services/monzaApi";
import type { MonzaItemQty } from "../services/monzaApi";
import { ADELANTO_PCT_DEFECTO } from "../constants/adelanto";
import { useMonzaTheme } from "./MonzaLayout";
import MonzaDocs from "./MonzaDocs";
import toast from "react-hot-toast";

interface ItemCompra {
  id: number;
  cotizacion_id: number;
  cot_numero: string;
  cliente?: string;
  vehiculo?: string;
  descripcion: string;
  numero_parte?: string;
  marca?: string;
  procedencia?: string;
  calidad?: string;
  cantidad: number;
  costo?: number;
  moneda?: string;
  precio_unitario_clp?: number;
  subtotal_clp?: number;
  plazo_entrega?: string;
  estado_linea: string;
  oc_proveedor_id?: number | null;
  fecha_venta?: string;
  // Origen de la OC del ítem: 'nacional' → NO pasa por preparar/embarque (su camino
  // es "Registrar entrega nacional" en Seguimiento). Solo viene en /comprados.
  tipo_origen?: string;
  // Adelanto (verificado por Contabilidad)
  pct_adelanto?: number;
  requiere_adelanto?: boolean;
  pago_verificado?: boolean;
}

// Cortafuego: un ítem NO se puede comprar si su venta exige adelanto y Contabilidad aún
// no verificó el pago. Bloquea solo ese ítem (los demás se compran normal).
const itemBloqueado = (it: ItemCompra) => !!(it.requiere_adelanto && !it.pago_verificado);

interface OcCompra {
  id: number;
  numero: string;
  numero_oc?: string;
  proveedor_nombre?: string;
  pais?: string;
  moneda?: string;
  // Camino físico: 'internacional' (embarque+aduana) | 'nacional' (camión + guía,
  // sin embarque). Coalescido en el backend: histórico sin valor = internacional.
  tipo_origen?: string;
  estado: string;
  plazo_dias?: number;
  awb?: string;
  tracking?: string;
  notas?: string;
  asesor_email?: string;
  items_count: number;
  created_at?: string;
}

interface Proveedor {
  id: number; nombre: string; pais?: string; moneda?: string;
}

interface KPIs {
  por_comprar: number; comprado: number; en_transito: number;
  en_bodega: number; despachado: number; reclamo: number; ocs_abiertas: number;
}

const OC_ESTADO: Record<string, { bg: string; color: string; label: string }> = {
  emitida:     { bg: "#FEF3C7", color: "#B45309", label: "Emitida" },
  en_transito: { bg: "#DBEAFE", color: "#1D4ED8", label: "En tránsito" },
  recibida:    { bg: "#DCFCE7", color: "#15803D", label: "Recibida" },
  cancelada:   { bg: "#FEE2E2", color: "#B91C1C", label: "Cancelada" },
};

function fmt(n?: number) { return n && n > 0 ? `$${Math.round(n).toLocaleString("es-CL")}` : "—"; }
function fmtDate(d?: string) { return d ? new Date(d).toLocaleDateString("es-CL") : "—"; }

function KpiCard({ label, value, accent }: { label: string; value: number; accent: string }) {
  const { dark } = useMonzaTheme();
  return (
    <div style={{ background: dark ? "#131b3e" : "white", borderRadius: 12, border: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}`, padding: "14px 18px", flex: 1, minWidth: 120 }}>
      <div style={{ width: 24, height: 3, borderRadius: 2, background: accent, marginBottom: 10 }} />
      <div style={{ fontSize: 24, fontWeight: 800, color: dark ? "white" : "#1E293B", lineHeight: 1 }}>{value}</div>
      <div style={{ fontSize: 11, color: dark ? "#8899cc" : "#64748B", marginTop: 4 }}>{label}</div>
    </div>
  );
}

// Países base (se combinan con los de proveedores existentes + agregados localmente)
const PAISES_BASE = ["Alemania", "USA", "Japón", "China", "España", "Italia", "Francia", "Reino Unido", "Corea del Sur", "Brasil", "México", "Chile"];
function loadPaises(extra: string[]): string[] {
  let custom: string[] = [];
  try { custom = JSON.parse(localStorage.getItem("monza-paises") || "[]"); } catch { /* noop */ }
  return Array.from(new Set([...PAISES_BASE, ...extra.filter(Boolean), ...custom])).sort();
}

// ── Modal Crear OC de compra ──────────────────────────────────────────────────
function CrearOcModal({ items, proveedores, onClose, onDone }: {
  items: ItemCompra[]; proveedores: Proveedor[]; onClose: () => void; onDone: () => void;
}) {
  const { dark } = useMonzaTheme();
  const [provList, setProvList] = useState<Proveedor[]>(proveedores);
  const [provId, setProvId] = useState<string>("");
  const [pais, setPais] = useState("");
  const [moneda, setMoneda] = useState("EUR");
  // Camino físico de la compra: internacional (embarque+aduana+landed) o nacional
  // (camión + guía de despacho, sin embarque). Gobierna la moneda por defecto en la
  // UI y el flujo posterior en Seguimiento (nacional salta preparado/embarque).
  // NO se deriva de país/moneda: es la fuente única del camino físico en el backend.
  const [tipoOrigen, setTipoOrigen] = useState<"internacional" | "nacional">("internacional");
  const [numeroOc, setNumeroOc] = useState("");
  const [plazo, setPlazo] = useState("");
  const [notas, setNotas] = useState("");
  const [saving, setSaving] = useState(false);

  // ── Asignación PARCIAL (espejo GA ComprasPage) ──────────────────────────────
  // El control de cantidad SOLO aparece en líneas partibles: 'por_comprar', sin
  // vínculo con una OC y cantidad > 1 (una línea con cantidad NULL/0/1 va entera
  // por el camino legado, que corre cuando el operador no toca nada — no se
  // ofrece en pantalla lo que el backend va a rechazar).
  const admiteParcial = (i: ItemCompra) =>
    i.estado_linea === "por_comprar" && !i.oc_proveedor_id && (i.cantidad ?? 0) > 1;

  // Cantidad a comprar por línea, como texto (el input vive como string y se
  // valida al guardar). Default = la cantidad COMPLETA → si el operador no toca
  // nada, el guardado usa el body LEGADO tal cual (cero cambio de comportamiento).
  const [itemQtys, setItemQtys] = useState<Record<number, string>>(() => {
    const init: Record<number, string> = {};
    items.forEach((i) => { init[i.id] = String(i.cantidad ?? ""); });
    return init;
  });

  // Rechazos (validación local o 400/404/409/422 del backend) al recuadro DENTRO
  // del modal: un 409 importante no cabe en un toast de 4 segundos, y el operador
  // necesita leerlo entero para decidir.
  const [errorAsignar, setErrorAsignar] = useState<string | null>(null);

  // Cierre bloqueado durante el guardado. A diferencia de GA no hace falta avisar
  // por una "OC creada sin ítems": acá la OC y la asignación (split incluido) son
  // UNA sola transacción en el backend — si algo rebota, la OC no nació.
  const cerrarSeguro = () => { if (saving) return; onClose(); };

  const onTipoOrigen = (v: "internacional" | "nacional") => {
    setTipoOrigen(v);
    // Nacional → Chile/CLP SOLO como default de UI (el usuario puede cambiarlos;
    // el origen jamás se deriva de la moneda).
    if (v === "nacional") { setPais("Chile"); setMoneda("CLP"); }
  };

  // Alta inline de proveedor
  const [addProv, setAddProv] = useState(false);
  const [npNombre, setNpNombre] = useState(""); const [npPais, setNpPais] = useState(""); const [npMoneda, setNpMoneda] = useState("EUR");
  const [savingProv, setSavingProv] = useState(false);

  // País como lista + alta
  const [paises, setPaises] = useState<string[]>(() => loadPaises(proveedores.map((p) => p.pais || "")));
  const [addPais, setAddPais] = useState(false); const [nuevoPais, setNuevoPais] = useState("");

  // Paso 2: documentos (AWB, tracking) sobre la OC creada
  const [ocpId, setOcpId] = useState<number | null>(null);
  const [ocpNumero, setOcpNumero] = useState("");

  const bg = dark ? "#131b3e" : "white";
  const bd = dark ? "#1e2a4a" : "#E2E8F0";
  const txt = dark ? "white" : "#1E293B";
  const sub = dark ? "#8899cc" : "#64748B";
  const IS = { width: "100%", padding: "8px 10px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 13, boxSizing: "border-box" as const, background: dark ? "#0d1321" : "#F8FAFC", color: txt };
  const lbl = { fontSize: 12, fontWeight: 600, color: sub, display: "block", marginBottom: 4 } as const;

  const onProvChange = (v: string) => {
    setProvId(v);
    const p = provList.find((x) => String(x.id) === v);
    if (p) { if (p.pais) setPais(p.pais); setMoneda(p.moneda || "EUR"); }
  };

  const guardarProveedor = async () => {
    if (!npNombre.trim()) { toast.error("Nombre requerido"); return; }
    setSavingProv(true);
    try {
      const r = await monzaAbastecimientoAPI.createProveedor({ nombre: npNombre, pais: npPais || undefined, moneda: npMoneda });
      const nuevo: Proveedor = { id: r.data.id, nombre: npNombre, pais: npPais, moneda: npMoneda };
      setProvList((l) => [...l, nuevo]);
      setProvId(String(nuevo.id)); if (npPais) setPais(npPais); setMoneda(npMoneda);
      setAddProv(false); setNpNombre(""); setNpPais("");
      toast.success(`Proveedor "${nuevo.nombre}" creado`);
    } catch { toast.error("Error al crear proveedor"); }
    finally { setSavingProv(false); }
  };

  const guardarPais = () => {
    const v = nuevoPais.trim();
    if (!v) return;
    try {
      const cur = JSON.parse(localStorage.getItem("monza-paises") || "[]");
      localStorage.setItem("monza-paises", JSON.stringify(Array.from(new Set([...cur, v]))));
    } catch { /* noop */ }
    setPaises((p) => Array.from(new Set([...p, v])).sort());
    setPais(v); setNuevoPais(""); setAddPais(false);
  };

  const submit = async () => {
    setErrorAsignar(null);
    const prov = provList.find((x) => String(x.id) === provId);

    // Validación de cantidades ANTES de tocar el backend: entero entre 1 y lo
    // disponible. El valor por defecto (la cantidad completa) siempre pasa.
    const problemas: string[] = [];
    const cantidades: Record<number, number> = {};
    for (const item of items) {
      if (!admiteParcial(item)) continue;
      const total = item.cantidad ?? 0;
      const parte = item.numero_parte || item.descripcion;
      const raw = (itemQtys[item.id] ?? "").trim();
      const qty = Number(raw);
      if (raw === "" || isNaN(qty)) {
        problemas.push(`${parte}: indica cuántas unidades asignar (entre 1 y ${total}).`);
        continue;
      }
      if (qty === total) { cantidades[item.id] = qty; continue; }  // completa: siempre válida
      if (!Number.isInteger(qty) || qty < 1 || qty > total) {
        problemas.push(`${parte}: la cantidad debe ser un número entero entre 1 y ${total} (pusiste ${raw}).`);
        continue;
      }
      cantidades[item.id] = qty;
    }
    if (problemas.length > 0) {
      setErrorAsignar(problemas.join("\n"));
      return;
    }
    // ¿Alguna línea va parcial? Si NINGUNA → body legado tal cual (el camino de
    // siempre no cambia ni un byte). El operador no necesita saber que hay dos rutas.
    const parciales = items.filter(
      (i) => admiteParcial(i) && cantidades[i.id] !== (i.cantidad ?? 0),
    );

    setSaving(true);
    try {
      const r = await monzaAbastecimientoAPI.comprar({
        item_ids: items.map((i) => i.id),
        proveedor_id: provId ? Number(provId) : undefined,
        proveedor_nombre: prov?.nombre || undefined,
        pais: pais || undefined,
        moneda,
        numero_oc: numeroOc || undefined,
        plazo_dias: plazo ? Number(plazo) : undefined,
        notas: notas || undefined,
        tipo_origen: tipoOrigen,
        // `cantidades` SOLO viaja cuando hay una parcial de verdad, y solo con las
        // líneas partidas (ausente = línea entera, sentinela del contrato; JAMÁS
        // se manda 0 — el backend lo rechaza a propósito).
        ...(parciales.length > 0
          ? { cantidades: parciales.map((i) => ({ item_id: i.id, cantidad: cantidades[i.id] })) }
          : {}),
      });
      if (parciales.length > 0) {
        // Feedback que cuenta QUÉ pasó, línea por línea partida, con los números
        // que devolvió el backend (no los que creímos mandar). Dura más que el
        // toast estándar porque hay que alcanzar a leerlo.
        const remanentes: any[] = r.data?.remanentes ?? [];
        const lineas = remanentes.map((p) => {
          const it = items.find((i) => i.id === p.item_id);
          const parte = it?.numero_parte || it?.descripcion || `ítem ${p.item_id}`;
          return `${parte}: asignaste ${p.comprado} de ${p.original} a la OC ${r.data.numero}; quedan ${p.pendiente} en el panel.`;
        });
        const enteras = (r.data?.items ?? items.length) - remanentes.length;
        if (enteras > 0) lineas.push(`Además ${enteras} línea(s) completa(s) asignada(s).`);
        toast.success(
          <span style={{ whiteSpace: "pre-line" }}>{lineas.join("\n")}</span>,
          { duration: 9000 },
        );
      } else {
        toast.success(`OC creada · ${items.length} ítem(s)`);
      }
      setOcpId(r.data.ocp_id); setOcpNumero(r.data.numero);  // pasar a paso 2 (documentos)
    } catch (e: any) {
      // El detalle del backend viene redactado para humanos (400/404/409/422): se
      // muestra ENTERO en el recuadro del modal, nunca truncado en un toast. El 422
      // de validación de Pydantic llega como lista de objetos → se aplanan los msg.
      const d = e?.response?.data?.detail;
      const msg = typeof d === "string"
        ? d
        : Array.isArray(d)
          ? d.map((x: any) => {
              // Con el `loc` el operador sabe QUÉ campo/línea falló:
              // «cantidades.0.cantidad: Input should be…» en vez del msg suelto.
              const loc = Array.isArray(x?.loc) ? x.loc.slice(1).join(".") : "";
              const msg = typeof x?.msg === "string" ? x.msg : JSON.stringify(x);
              return loc ? `${loc}: ${msg}` : msg;
            }).join("\n")
          : "No se pudo asignar. Revisa los datos e inténtalo de nuevo.";
      setErrorAsignar(msg);
    } finally { setSaving(false); }
  };

  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)", zIndex: 1000, display: "flex", alignItems: "center", justifyContent: "center", padding: 16 }}>
      <div style={{ background: bg, border: `1px solid ${bd}`, borderRadius: 14, width: "100%", maxWidth: 560, maxHeight: "90vh", display: "flex", flexDirection: "column", boxShadow: "0 24px 60px rgba(0,0,0,0.4)" }}>
        <div style={{ background: dark ? "#0a0e1f" : "#F8FAFC", borderBottom: `1px solid ${bd}`, padding: "14px 20px", display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <ShoppingCart size={18} className="monza-ic" />
            <span style={{ fontWeight: 700, fontSize: 15, color: txt }}>{ocpId ? `OC ${ocpNumero} · Documentos` : "Crear OC de compra"}</span>
          </div>
          <button onClick={cerrarSeguro} style={{ background: "none", border: "none", cursor: "pointer", color: sub, display: "flex" }}><X size={18} /></button>
        </div>

        <div style={{ padding: "16px 20px", overflowY: "auto" }}>
          {ocpId ? (
            /* ── Paso 2: documentos AWB / tracking ── */
            <div>
              <div style={{ fontSize: 12, color: sub, marginBottom: 12 }}>OC <strong style={{ color: txt }}>{ocpNumero}</strong> creada. Adjunta los documentos (AWB / guía aérea, tracking) cuando los tengas — también puedes hacerlo después desde la pestaña OCs.</div>
              <MonzaDocs entidad="oc_proveedor" entidadId={ocpId} categorias={["AWB (guía aérea)", "tracking", "factura proveedor", "packing list", "otro"]} titulo="Documentos de la OC" />
            </div>
          ) : (
          <>
          {/* Ítems seleccionados */}
          <div style={{ background: dark ? "#0d1321" : "#F1F5F9", borderRadius: 8, padding: "10px 14px", marginBottom: 16 }}>
            <div style={{ fontSize: 11, fontWeight: 700, color: sub, marginBottom: 6, textTransform: "uppercase" }}>{items.length} ítem(s) a comprar</div>
            {items.map((it) => (
              <div key={it.id} style={{ fontSize: 12, color: txt, padding: "2px 0", display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 8 }}>
                <span>{it.descripcion} <span style={{ color: sub }}>· {it.cot_numero}</span></span>
                {/* Control de cantidad: por defecto la cantidad completa; mínimo 1,
                    máximo lo disponible, enteros. Solo en líneas partibles
                    ('por_comprar', sin OC, cantidad > 1) — si la línea no admite
                    parcial, se muestra la cantidad a secas. */}
                {admiteParcial(it) ? (() => {
                  const total = it.cantidad ?? 0;
                  const q = Number((itemQtys[it.id] ?? "").trim());
                  const esParcialValida = !isNaN(q) && Number.isInteger(q) && q >= 1 && q < total;
                  return (
                    <span style={{ textAlign: "right", flexShrink: 0 }}>
                      <span style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
                        <input
                          type="number"
                          min={1}
                          max={total}
                          step={1}
                          value={itemQtys[it.id] ?? ""}
                          onChange={(e) => {
                            setItemQtys((prev) => ({ ...prev, [it.id]: e.target.value }));
                            setErrorAsignar(null);
                          }}
                          title={`Cuántas unidades asignar a esta OC (entre 1 y ${total}). El resto queda disponible en el panel.`}
                          style={{ ...IS, width: 56, padding: "3px 6px", fontSize: 12 }}
                        />
                        <span style={{ color: sub }}>de {total}</span>
                      </span>
                      {esParcialValida && (
                        <span style={{ display: "block", fontSize: 10, marginTop: 2, fontWeight: 600, color: "#F59E0B" }}>
                          Asignar {q} de {total} — quedan {total - q} en el panel
                        </span>
                      )}
                    </span>
                  );
                })() : (
                  <span style={{ color: sub }}>×{it.cantidad}{it.costo ? ` · ${it.moneda} ${it.costo}` : ""}</span>
                )}
              </div>
            ))}
          </div>

          {/* Origen de la compra: internacional (embarque) o nacional (camión + guía) */}
          <div style={{ marginBottom: 14 }}>
            <label style={lbl}>Origen de la compra</label>
            <div style={{ display: "flex", gap: 8 }}>
              {([
                ["internacional", "Internacional", "Embarque + aduana"],
                ["nacional", "Nacional", "Camión + guía, sin embarque"],
              ] as const).map(([val, label, subT]) => (
                <button key={val} type="button" onClick={() => onTipoOrigen(val)}
                  style={{
                    flex: 1, padding: "8px 10px", borderRadius: 10, textAlign: "left", cursor: "pointer", fontFamily: "inherit",
                    border: tipoOrigen === val ? "1px solid var(--monza-accent)" : `1px solid ${bd}`,
                    background: tipoOrigen === val ? (dark ? "#1a2340" : "#FFF5F5") : "transparent",
                    color: tipoOrigen === val ? "var(--monza-accent)" : sub, fontSize: 12, fontWeight: 700,
                  }}>
                  {label}
                  <span style={{ display: "block", fontSize: 10, fontWeight: 400, marginTop: 2, color: tipoOrigen === val ? "var(--monza-accent)" : sub }}>{subT}</span>
                </button>
              ))}
            </div>
            {tipoOrigen === "nacional" && (
              <p style={{ margin: "6px 0 0", fontSize: 11, color: sub }}>
                Nacional: moneda <b style={{ color: "var(--monza-accent)" }}>CLP</b> por defecto, sin AWB/forwarder.
                La entrega se registra luego en <b>Seguimiento</b> ("Registrar entrega nacional").
              </p>
            )}
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            {/* Proveedor (lista + agregar) */}
            <div style={{ gridColumn: "1 / -1" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
                <label style={lbl}>Proveedor</label>
                <button onClick={() => setAddProv((v) => !v)} style={{ background: "none", border: "none", cursor: "pointer", color: "var(--monza-accent)", fontSize: 11, fontWeight: 600, display: "flex", alignItems: "center", gap: 3 }}>
                  <Plus size={12} /> Agregar proveedor
                </button>
              </div>
              <select value={provId} onChange={(e) => onProvChange(e.target.value)} style={IS}>
                <option value="">— Seleccionar proveedor —</option>
                {provList.map((p) => <option key={p.id} value={p.id}>{p.nombre}{p.pais ? ` (${p.pais})` : ""}</option>)}
              </select>
              {addProv && (
                <div style={{ marginTop: 8, padding: "10px 12px", border: `1px dashed ${bd}`, borderRadius: 8, display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
                  <input value={npNombre} onChange={(e) => setNpNombre(e.target.value)} placeholder="Nombre proveedor *" style={{ ...IS, gridColumn: "1 / -1" }} />
                  <input value={npPais} onChange={(e) => setNpPais(e.target.value)} placeholder="País" style={IS} list="paises-list" />
                  <select value={npMoneda} onChange={(e) => setNpMoneda(e.target.value)} style={IS}><option>EUR</option><option>USD</option><option>CLP</option></select>
                  <button onClick={guardarProveedor} disabled={savingProv} style={{ gridColumn: "1 / -1", padding: "7px", background: "var(--monza-accent)", border: "none", borderRadius: 6, color: "white", cursor: "pointer", fontSize: 12, fontWeight: 700 }}>{savingProv ? "Guardando..." : "Guardar proveedor"}</button>
                </div>
              )}
            </div>

            {/* País (lista + agregar) */}
            <div>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
                <label style={lbl}>País</label>
                <button onClick={() => setAddPais((v) => !v)} style={{ background: "none", border: "none", cursor: "pointer", color: "var(--monza-accent)", fontSize: 11, fontWeight: 600, display: "flex", alignItems: "center", gap: 3 }}><Plus size={11} /> Otro</button>
              </div>
              {addPais ? (
                <div style={{ display: "flex", gap: 6 }}>
                  <input value={nuevoPais} onChange={(e) => setNuevoPais(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") guardarPais(); }} placeholder="Nuevo país" style={IS} autoFocus />
                  <button onClick={guardarPais} style={{ padding: "0 12px", background: "var(--monza-accent)", border: "none", borderRadius: 6, color: "white", cursor: "pointer", fontSize: 12, fontWeight: 700 }}>OK</button>
                </div>
              ) : (
                <select value={pais} onChange={(e) => setPais(e.target.value)} style={IS}>
                  <option value="">— Seleccionar —</option>
                  {paises.map((p) => <option key={p} value={p}>{p}</option>)}
                </select>
              )}
            </div>
            <div>
              <label style={lbl}>Moneda</label>
              <select value={moneda} onChange={(e) => setMoneda(e.target.value)} style={IS}><option>EUR</option><option>USD</option><option>CLP</option></select>
            </div>
            <div>
              <label style={lbl}>N° OC proveedor</label>
              <input value={numeroOc} onChange={(e) => setNumeroOc(e.target.value)} placeholder="PO-2026-… (opcional)" style={IS} />
            </div>
            <div>
              <label style={lbl}>Plazo entrega proveedor (días)</label>
              <input type="number" value={plazo} onChange={(e) => setPlazo(e.target.value)} placeholder="30" style={IS} />
            </div>
            <div style={{ gridColumn: "1 / -1" }}>
              <label style={lbl}>Notas</label>
              <textarea value={notas} onChange={(e) => setNotas(e.target.value)} rows={2} style={{ ...IS, resize: "vertical" as const }} />
            </div>
          </div>
          <div style={{ fontSize: 11, color: sub, marginTop: 8 }}>📎 La AWB (guía aérea) y el tracking se adjuntan como documentos en el siguiente paso.</div>

          {/* Rechazo pintado ACÁ, pegado al botón que el operador acaba de apretar,
              y se queda hasta que él lo cierre: el detalle del backend viene
              redactado para humanos y hay que poder leerlo entero (un toast de 4
              segundos no alcanza para un 409 importante). */}
          {errorAsignar && (
            <div style={{ marginTop: 12, border: "1px solid rgba(239,68,68,0.35)", background: "rgba(239,68,68,0.10)", borderRadius: 10, padding: "10px 12px", fontSize: 12 }}>
              <div style={{ fontWeight: 700, color: "#f87171", display: "flex", alignItems: "flex-start", gap: 6 }}>
                <AlertCircle size={14} style={{ marginTop: 1, flexShrink: 0 }} />
                No se pudo asignar
              </div>
              <div style={{ color: sub, whiteSpace: "pre-line", marginTop: 6 }}>{errorAsignar}</div>
              <button
                type="button"
                onClick={() => setErrorAsignar(null)}
                style={{ background: "none", border: "none", cursor: "pointer", padding: 0, marginTop: 6, fontWeight: 600, fontSize: 12, color: sub, textDecoration: "underline" }}
              >
                Ocultar este aviso
              </button>
            </div>
          )}
          <datalist id="paises-list">{paises.map((p) => <option key={p} value={p} />)}</datalist>
          </>
          )}
        </div>

        <div style={{ padding: "12px 20px", borderTop: `1px solid ${bd}`, display: "flex", justifyContent: "flex-end", gap: 8 }}>
          {ocpId ? (
            <button onClick={onDone} style={{ padding: "8px 22px", background: "#10B981", border: "none", borderRadius: 8, color: "white", cursor: "pointer", fontWeight: 700, fontSize: 13 }}>Finalizar</button>
          ) : (
            <>
              <button onClick={cerrarSeguro} disabled={saving} style={{ padding: "8px 18px", border: `1px solid ${bd}`, borderRadius: 8, background: "transparent", color: sub, cursor: saving ? "not-allowed" : "pointer", fontSize: 13, opacity: saving ? 0.5 : 1 }}>Cancelar</button>
              <button onClick={submit} disabled={saving} style={{ padding: "8px 20px", background: "var(--monza-accent)", border: "none", borderRadius: 8, color: "white", cursor: saving ? "wait" : "pointer", fontWeight: 700, fontSize: 13, display: "flex", alignItems: "center", gap: 6 }}>
                <ShoppingCart size={14} /> {saving ? "Creando..." : "Crear OC"}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

export default function MonzaAbastecimientoPage() {
  const { dark } = useMonzaTheme();
  const [tab, setTab] = useState<"por_comprar" | "comprados" | "ocs">("por_comprar");
  const [items, setItems] = useState<ItemCompra[]>([]);
  const [comprados, setComprados] = useState<ItemCompra[]>([]);
  const [selPrep, setSelPrep] = useState<Set<number>>(new Set());
  // Cantidad a PREPARAR por ítem (envío parcial: el proveedor mandó 6 de 10). Sin
  // tocar nada vale toda la línea; si se baja, el backend parte la línea y el
  // remanente vuelve a esta misma pestaña esperando el próximo embarque.
  const [qtyPrep, setQtyPrep] = useState<Record<number, number>>({});
  const [ocs, setOcs] = useState<OcCompra[]>([]);
  const [proveedores, setProveedores] = useState<Proveedor[]>([]);
  const [kpis, setKpis] = useState<KPIs | null>(null);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState("");
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [showModal, setShowModal] = useState(false);
  const [expandedOc, setExpandedOc] = useState<Set<number>>(new Set());
  const toggleOc = (id: number) => setExpandedOc((prev) => {
    const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n;
  });

  const bg = dark ? "#131b3e" : "white";
  const bd = dark ? "#1e2a4a" : "#E2E8F0";
  const txt = dark ? "white" : "#1E293B";
  const sub = dark ? "#8899cc" : "#64748B";

  const fetchAll = useCallback(async () => {
    setLoading(true);
    try {
      const [kpisRes, itemsRes, compRes, ocsRes, provRes] = await Promise.all([
        monzaAbastecimientoAPI.kpis(),
        monzaAbastecimientoAPI.porComprar({ q: q || undefined }),
        monzaAbastecimientoAPI.comprados({ q: q || undefined }),
        monzaAbastecimientoAPI.listOcs(),
        monzaAbastecimientoAPI.listProveedores(),
      ]);
      setKpis(kpisRes.data);
      setItems(itemsRes.data);
      setComprados(compRes.data);
      setOcs(ocsRes.data);
      setProveedores(provRes.data);
    } catch { toast.error("Error al cargar abastecimiento"); }
    finally { setLoading(false); }
  }, [q]);

  useEffect(() => { fetchAll(); }, [fetchAll]);

  const toggleSel = (id: number) => {
    setSelected((prev) => {
      const n = new Set(prev);
      n.has(id) ? n.delete(id) : n.add(id);
      return n;
    });
  };
  const toggleAll = () => {
    // "Seleccionar todos" omite los ítems bloqueados por adelanto no verificado.
    setSelected((prev) => {
      const comprables = items.filter((i) => !itemBloqueado(i));
      return prev.size === comprables.length && comprables.length > 0 ? new Set() : new Set(comprables.map((i) => i.id));
    });
  };

  const togglePrep = (id: number) => setSelPrep((p) => { const n = new Set(p); n.has(id) ? n.delete(id) : n.add(id); return n; });
  // Cantidad efectiva a preparar de un ítem: lo tecleado o toda la línea.
  const qtyPrepDe = (it: ItemCompra) => qtyPrep[it.id] ?? it.cantidad;
  const preparar = async () => {
    // Solo los ítems con cantidad REBAJADA viajan con `cantidad`; si nadie tocó nada,
    // el servicio manda el pedido por la vía legada (línea completa, sin partir).
    const pedidos: MonzaItemQty[] = Array.from(selPrep).map((id) => {
      const it = comprados.find((c) => c.id === id);
      const q = it ? qtyPrepDe(it) : undefined;
      return it && q !== undefined && q < it.cantidad ? { item_id: id, cantidad: q } : { item_id: id };
    });
    try {
      const r = await monzaAbastecimientoAPI.preparar(pedidos);
      const pend = monzaTotalPendiente(r.data.remanentes);
      toast.success(
        `${r.data.preparados} ítem(s) preparado(s) → Logística`
        + (pend > 0 ? ` · quedan ${pend} unidad(es) en Comprados esperando el próximo embarque` : ""),
      );
      setSelPrep(new Set()); setQtyPrep({}); fetchAll();
    } catch (e: unknown) {
      // El backend explica el motivo (cantidad inválida, estado equivocado, o la
      // línea ya tiene guía/factura encima): mostrarlo es lo único útil aquí.
      toast.error(monzaErrMsg(e, "Error al preparar"));
    }
  };

  const advanceOc = async (oc: OcCompra, nuevoEstado: string) => {
    try {
      await monzaAbastecimientoAPI.updateOc(oc.id, { estado: nuevoEstado });
      toast.success(`${oc.numero} → ${OC_ESTADO[nuevoEstado]?.label || nuevoEstado}`);
      fetchAll();
    } catch { toast.error("Error al actualizar OC"); }
  };

  const selItems = items.filter((i) => selected.has(i.id));
  const comprables = items.filter((i) => !itemBloqueado(i));  // ítems sin bloqueo de adelanto
  // Comprados preparables: los de OC NACIONAL no van a embarque (su camino es
  // "Registrar entrega nacional" en Seguimiento) — se excluyen del check global.
  const preparables = comprados.filter((i) => i.tipo_origen !== "nacional");
  // Defensa: ítems seleccionados que igual estén bloqueados (no debería ocurrir: no se pueden marcar).
  const bloqueadosSel = selItems.filter(itemBloqueado);

  return (
    <div>
      {/* Header */}
      <div style={{ marginBottom: 18 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
          <ShoppingCart size={22} className="monza-ic" />
          <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: txt }}>Abastecimiento</h1>
        </div>
        <p style={{ margin: 0, fontSize: 13, color: sub }}>
          Ítems vendidos pendientes de compra a proveedor. Selecciona ítems y emite una OC de compra.
        </p>
      </div>

      {/* KPIs */}
      {kpis && (
        <div style={{ display: "flex", gap: 10, marginBottom: 18, flexWrap: "wrap" }}>
          <KpiCard label="Por comprar" value={kpis.por_comprar} accent="#F59E0B" />
          <KpiCard label="Comprado" value={kpis.comprado} accent="#3B82F6" />
          <KpiCard label="En tránsito" value={kpis.en_transito} accent="#6366F1" />
          <KpiCard label="En bodega" value={kpis.en_bodega} accent="#10B981" />
          <KpiCard label="OCs abiertas" value={kpis.ocs_abiertas} accent="var(--monza-accent)" />
        </div>
      )}

      {/* Tabs */}
      <div style={{ display: "flex", gap: 4, marginBottom: 14, borderBottom: `1px solid ${bd}` }}>
        {([["por_comprar", "Por comprar", items.length], ["comprados", "Comprados", comprados.length], ["ocs", "OCs de compra", ocs.length]] as const).map(([key, label, count]) => (
          <button key={key} onClick={() => setTab(key)}
            style={{ padding: "9px 16px", border: "none", background: "transparent", cursor: "pointer", fontSize: 13, fontWeight: 600,
              color: tab === key ? "var(--monza-accent)" : sub, borderBottom: `2px solid ${tab === key ? "var(--monza-accent)" : "transparent"}`, marginBottom: -1 }}>
            {label} <span style={{ fontSize: 11, background: dark ? "#1e2a4a" : "#F1F5F9", padding: "1px 7px", borderRadius: 10, marginLeft: 4 }}>{count}</span>
          </button>
        ))}
      </div>

      {/* Toolbar */}
      <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 12, flexWrap: "wrap" }}>
        <div style={{ position: "relative", flex: 1, minWidth: 240 }}>
          <Search size={14} style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)", color: "#94A3B8" }} />
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Buscar ítem, parte, N° COT..."
            style={{ width: "100%", padding: "8px 10px 8px 32px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 13, boxSizing: "border-box" as const, background: bg, color: txt }} />
        </div>
        {tab === "por_comprar" && selected.size > 0 && (
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <button onClick={() => setShowModal(true)} disabled={bloqueadosSel.length > 0}
              title={bloqueadosSel.length > 0 ? "Hay ítems con adelanto no verificado por Contabilidad" : "Generar OC de proveedor"}
              style={{ padding: "8px 16px", background: "var(--monza-accent)", border: "none", borderRadius: 8, color: "white", cursor: bloqueadosSel.length > 0 ? "not-allowed" : "pointer", fontWeight: 700, fontSize: 13, display: "flex", alignItems: "center", gap: 6, opacity: bloqueadosSel.length > 0 ? 0.5 : 1 }}>
              <ShoppingCart size={14} /> Comprar {selected.size} ítem(s)
            </button>
            {bloqueadosSel.length > 0 && (
              <span style={{ fontSize: 12, color: "#B91C1C", fontWeight: 600 }}>⚠ {bloqueadosSel.length} con adelanto sin verificar</span>
            )}
          </div>
        )}
        {tab === "comprados" && selPrep.size > 0 && (
          <button onClick={preparar}
            style={{ padding: "8px 16px", background: "#10B981", border: "none", borderRadius: 8, color: "white", cursor: "pointer", fontWeight: 700, fontSize: 13, display: "flex", alignItems: "center", gap: 6 }}>
            <Package size={14} /> Preparar {selPrep.size} → Logística
          </button>
        )}
        <button onClick={fetchAll} style={{ padding: "8px 10px", border: `1px solid ${bd}`, borderRadius: 6, background: bg, cursor: "pointer", color: sub }}>
          <RefreshCw size={14} />
        </button>
      </div>

      {/* Envío parcial: la explicación va donde se teclea la cantidad, no en un manual */}
      {tab === "comprados" && comprados.length > 0 && (
        <p style={{ margin: "0 0 10px", fontSize: 11, color: sub }}>
          Si el proveedor envió solo una parte, baja la cantidad en <b style={{ color: txt }}>A preparar</b>:
          el resto se queda aquí en <b style={{ color: txt }}>Comprados</b> esperando el próximo embarque.
        </p>
      )}

      {/* Content */}
      <div style={{ background: bg, border: `1px solid ${bd}`, borderRadius: 10, overflow: "hidden" }}>
        {loading ? (
          <div style={{ padding: 40, textAlign: "center", color: "#94A3B8" }}>Cargando...</div>
        ) : tab === "comprados" ? (
          comprados.length === 0 ? (
            <div style={{ padding: 48, textAlign: "center", color: "#94A3B8" }}>
              <Package size={32} color="#E2E8F0" style={{ display: "block", margin: "0 auto 8px" }} />
              No hay ítems comprados pendientes de preparar.
            </div>
          ) : (
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead>
                <tr style={{ background: dark ? "#0d1321" : "#F8FAFC", borderBottom: `1px solid ${bd}` }}>
                  <th style={{ width: 36, padding: "10px 8px", textAlign: "center" }}>
                    <input type="checkbox" checked={preparables.length > 0 && selPrep.size === preparables.length} onChange={() => setSelPrep(selPrep.size === preparables.length ? new Set() : new Set(preparables.map((i) => i.id)))} style={{ accentColor: "var(--monza-accent)" }} />
                  </th>
                  {["N° COT", "Cliente", "Repuesto", "OC Proveedor", "Cant.", "A preparar"].map((h) => (
                    <th key={h} style={{ padding: "10px 12px", textAlign: ["Cant.", "A preparar"].includes(h) ? "right" : "left", fontWeight: 600, fontSize: 11, color: sub, textTransform: "uppercase" as const }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {comprados.map((it) => {
                  const isSel = selPrep.has(it.id);
                  // Nacional: NO se prepara/embarca — su camino es "Registrar entrega
                  // nacional" en Seguimiento (la UI oculta Y el backend rechaza 400).
                  const esNacional = it.tipo_origen === "nacional";
                  return (
                    <tr key={it.id} onClick={() => { if (!esNacional) togglePrep(it.id); }} style={{ borderBottom: `1px solid ${dark ? "#1e2a4a" : "#F1F5F9"}`, background: isSel ? (dark ? "#1a2340" : "#F0FDF4") : "transparent", cursor: esNacional ? "default" : "pointer" }}>
                      <td style={{ textAlign: "center", padding: 8 }}>
                        {esNacional ? (
                          <Truck size={13} color="#15803D" style={{ verticalAlign: "middle" }} />
                        ) : (
                          <input type="checkbox" checked={isSel} onChange={() => togglePrep(it.id)} onClick={(e) => e.stopPropagation()} style={{ accentColor: "var(--monza-accent)" }} />
                        )}
                      </td>
                      <td style={{ padding: "9px 12px", fontWeight: 600, color: "var(--monza-accent)", fontSize: 12 }}>{it.cot_numero}</td>
                      <td style={{ padding: "9px 12px", color: txt }}>{it.cliente || "—"}</td>
                      <td style={{ padding: "9px 12px" }}>
                        <div style={{ color: txt, fontWeight: 500, display: "flex", alignItems: "center", gap: 6 }}>
                          {it.descripcion}
                          {esNacional && (
                            <span title="OC nacional: la entrega se registra en Seguimiento (no pasa por embarque)"
                              style={{ fontSize: 10, fontWeight: 700, background: "#DCFCE7", color: "#15803D", padding: "1px 7px", borderRadius: 999 }}>Nacional</span>
                          )}
                        </div>
                        {it.numero_parte && <div style={{ fontSize: 10, color: sub }}>{it.numero_parte}</div>}
                      </td>
                      <td style={{ padding: "9px 12px", color: sub, fontSize: 12 }}>{(it as ItemCompra & { ocp_numero?: string; ocp_proveedor?: string }).ocp_numero || "—"}{(it as ItemCompra & { ocp_proveedor?: string }).ocp_proveedor ? ` · ${(it as ItemCompra & { ocp_proveedor?: string }).ocp_proveedor}` : ""}</td>
                      <td style={{ padding: "9px 12px", textAlign: "right", color: txt }}>{it.cantidad}</td>
                      {/* Cantidad a preparar (envío parcial). El <td> corta el click porque
                          la fila togglea el checkbox: teclear no debe deseleccionar la línea. */}
                      <td style={{ padding: "9px 12px", textAlign: "right" }} onClick={(e) => e.stopPropagation()}>
                        {esNacional ? (
                          <span style={{ color: sub }}>—</span>
                        ) : (
                          <span style={{ display: "inline-flex", alignItems: "center", justifyContent: "flex-end", gap: 6 }}>
                            <input
                              type="number" min={1} max={it.cantidad} step={1}
                              aria-label={`Cantidad a preparar de ${it.descripcion} (máximo ${it.cantidad})`}
                              value={qtyPrepDe(it)}
                              onChange={(e) => setQtyPrep((p) => ({ ...p, [it.id]: Math.max(1, Math.min(it.cantidad, Math.round(Number(e.target.value) || 0))) }))}
                              style={{ width: 58, padding: "3px 6px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 12, background: dark ? "#0d1321" : "#F8FAFC", color: txt, textAlign: "right" }}
                            />
                            <span style={{ fontSize: 11, color: sub }}>/ {it.cantidad}</span>
                            {qtyPrepDe(it) < it.cantidad && (
                              <span title="Preparación parcial: el resto se queda en Comprados esperando el próximo embarque"
                                style={{ fontSize: 10, fontWeight: 700, background: "#FEF3C7", color: "#B45309", padding: "1px 7px", borderRadius: 999, whiteSpace: "nowrap" }}>
                                {it.cantidad - qtyPrepDe(it)} pendiente
                              </span>
                            )}
                          </span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )
        ) : tab === "por_comprar" ? (
          items.length === 0 ? (
            <div style={{ padding: 48, textAlign: "center", color: "#94A3B8" }}>
              <Package size={32} color="#E2E8F0" style={{ display: "block", margin: "0 auto 8px" }} />
              No hay ítems pendientes de compra.
            </div>
          ) : (
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead>
                <tr style={{ background: dark ? "#0d1321" : "#F8FAFC", borderBottom: `1px solid ${bd}` }}>
                  <th style={{ width: 36, padding: "10px 8px", textAlign: "center" }}>
                    <input type="checkbox" checked={comprables.length > 0 && selected.size === comprables.length} onChange={toggleAll} style={{ accentColor: "var(--monza-accent)", cursor: "pointer" }} />
                  </th>
                  {["N° COT", "Cliente", "Repuesto", "Marca/Calidad", "Cant.", "Costo", "Plazo", "Vendida", "Pago"].map((h) => (
                    <th key={h} style={{ padding: "10px 12px", textAlign: ["Cant.", "Costo"].includes(h) ? "right" : "left", fontWeight: 600, fontSize: 11, color: sub, textTransform: "uppercase" as const, letterSpacing: 0.5 }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {items.map((it) => {
                  const isSel = selected.has(it.id);
                  const bloqueado = itemBloqueado(it);  // adelanto no verificado → no se puede comprar
                  const rowBg = isSel ? (dark ? "#1a2340" : "#FFF7F7")
                    : bloqueado ? (dark ? "#2a1620" : "#FEF2F2") : "transparent";
                  return (
                    <tr key={it.id} style={{ borderBottom: `1px solid ${dark ? "#1e2a4a" : "#F1F5F9"}`, background: rowBg, cursor: bloqueado ? "not-allowed" : "pointer" }}
                      onClick={() => { if (!bloqueado) toggleSel(it.id); }}>
                      <td style={{ textAlign: "center", padding: "8px" }}>
                        <input type="checkbox" checked={isSel} disabled={bloqueado}
                          title={bloqueado ? "Adelanto no verificado por Contabilidad: no se puede comprar hasta confirmar el pago" : ""}
                          onChange={() => { if (!bloqueado) toggleSel(it.id); }} onClick={(e) => e.stopPropagation()}
                          style={{ accentColor: "var(--monza-accent)", cursor: bloqueado ? "not-allowed" : "pointer" }} />
                      </td>
                      <td style={{ padding: "9px 12px" }}>
                        <div style={{ fontWeight: 600, fontSize: 12, color: "var(--monza-accent)" }}>{it.cot_numero}</div>
                        {it.vehiculo && <div style={{ fontSize: 10, color: sub }}>{it.vehiculo}</div>}
                      </td>
                      <td style={{ padding: "9px 12px", color: txt }}>{it.cliente || "—"}</td>
                      <td style={{ padding: "9px 12px" }}>
                        <div style={{ color: txt, fontWeight: 500 }}>{it.descripcion}</div>
                        {it.numero_parte && <div style={{ fontSize: 10, color: sub }}>{it.numero_parte}</div>}
                      </td>
                      <td style={{ padding: "9px 12px", color: sub, fontSize: 12 }}>
                        {it.marca || "—"}{it.calidad ? ` · ${it.calidad}` : ""}
                      </td>
                      <td style={{ padding: "9px 12px", textAlign: "right", color: txt }}>{it.cantidad}</td>
                      <td style={{ padding: "9px 12px", textAlign: "right", color: sub }}>{it.costo ? `${it.moneda} ${it.costo}` : "—"}</td>
                      <td style={{ padding: "9px 12px", color: sub, fontSize: 12 }}>{it.plazo_entrega || "—"}</td>
                      <td style={{ padding: "9px 12px", color: sub, fontSize: 12 }}>{fmtDate(it.fecha_venta)}</td>
                      <td style={{ padding: "9px 12px" }}>
                        {it.requiere_adelanto ? (
                          <span style={{ fontSize: 11, fontWeight: 600, padding: "2px 8px", borderRadius: 999, whiteSpace: "nowrap",
                            background: it.pago_verificado ? "#DCFCE7" : "#FEE2E2",
                            color: it.pago_verificado ? "#15803D" : "#B91C1C" }}>
                            {it.pago_verificado ? `Adelanto ${it.pct_adelanto || ADELANTO_PCT_DEFECTO}% verificado` : "Pago no verificado"}
                          </span>
                        ) : (
                          <span style={{ fontSize: 11, color: sub }}>—</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )
        ) : (
          // OCs tab
          ocs.length === 0 ? (
            <div style={{ padding: 48, textAlign: "center", color: "#94A3B8" }}>
              <FileText size={32} color="#E2E8F0" style={{ display: "block", margin: "0 auto 8px" }} />
              No hay OCs de compra emitidas.
            </div>
          ) : (
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead>
                <tr style={{ background: dark ? "#0d1321" : "#F8FAFC", borderBottom: `1px solid ${bd}` }}>
                  <th style={{ width: 32 }}></th>
                  {["N° OC", "Proveedor", "País", "Ítems", "Plazo entrega", "AWB / Tracking", "Estado", "Acciones"].map((h) => (
                    <th key={h} style={{ padding: "10px 12px", textAlign: "left", fontWeight: 600, fontSize: 11, color: sub, textTransform: "uppercase" as const, letterSpacing: 0.5 }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {ocs.flatMap((oc) => {
                  const es = OC_ESTADO[oc.estado] || OC_ESTADO.emitida;
                  const isExp = expandedOc.has(oc.id);
                  const rows = [
                    <tr key={oc.id} style={{ borderBottom: isExp ? "none" : `1px solid ${dark ? "#1e2a4a" : "#F1F5F9"}` }}>
                      <td style={{ textAlign: "center", color: "#94A3B8", cursor: "pointer" }} onClick={() => toggleOc(oc.id)}>
                        {isExp ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                      </td>
                      <td style={{ padding: "10px 12px" }}>
                        <div style={{ fontWeight: 700, color: "var(--monza-accent)", display: "flex", alignItems: "center", gap: 6 }}>
                          {oc.numero_oc || oc.numero}
                          {/* Badge del camino físico: nacional NO pasa por embarque */}
                          {oc.tipo_origen === "nacional" && (
                            <span title="OC nacional: camión + guía del proveedor, sin embarque"
                              style={{ fontSize: 10, fontWeight: 700, background: "#DCFCE7", color: "#15803D", padding: "1px 7px", borderRadius: 999, display: "inline-flex", alignItems: "center", gap: 3 }}>
                              <Truck size={9} /> Nacional
                            </span>
                          )}
                        </div>
                        {oc.numero_oc && <div style={{ fontSize: 10, color: "#94A3B8" }}>{oc.numero}</div>}
                      </td>
                      <td style={{ padding: "10px 12px", color: txt }}>{oc.proveedor_nombre || "—"}</td>
                      <td style={{ padding: "10px 12px", color: sub }}>{oc.pais || "—"}</td>
                      <td style={{ padding: "10px 12px", color: sub }}>{oc.items_count}</td>
                      <td style={{ padding: "10px 12px", color: sub }}>{oc.plazo_dias ? `${oc.plazo_dias} días` : "—"}</td>
                      <td style={{ padding: "10px 12px", color: sub, fontSize: 12 }}>
                        {oc.awb ? <div>AWB: {oc.awb}</div> : null}
                        {oc.tracking ? <div>Trk: {oc.tracking}</div> : null}
                        {!oc.awb && !oc.tracking ? "—" : null}
                      </td>
                      <td style={{ padding: "10px 12px" }}>
                        <span style={{ fontSize: 11, background: es.bg, color: es.color, padding: "3px 10px", borderRadius: 10, fontWeight: 600 }}>{es.label}</span>
                      </td>
                      <td style={{ padding: "10px 12px" }}>
                        {oc.estado === "emitida" && (
                          <button onClick={() => advanceOc(oc, "en_transito")} title="Enviar a Logística (en tránsito)"
                            style={{ padding: "4px 10px", border: "1px solid #6366F1", borderRadius: 6, background: "transparent", color: "#6366F1", cursor: "pointer", fontSize: 11, fontWeight: 600, display: "flex", alignItems: "center", gap: 4 }}>
                            <Truck size={11} /> A Logística
                          </button>
                        )}
                      </td>
                    </tr>,
                  ];
                  if (isExp) {
                    rows.push(
                      <tr key={`doc-${oc.id}`} style={{ borderBottom: `1px solid ${bd}` }}>
                        <td colSpan={9} style={{ padding: "8px 16px 14px 44px", background: dark ? "#0a0e1f" : "#F8FAFF" }}>
                          {oc.notas && <div style={{ fontSize: 12, color: sub, marginBottom: 8 }}><strong>Notas:</strong> {oc.notas}</div>}
                          <MonzaDocs entidad="oc_proveedor" entidadId={oc.id} categorias={["factura proveedor", "OC", "tracking", "packing list", "otro"]} titulo="Documentos de la OC" />
                        </td>
                      </tr>
                    );
                  }
                  return rows;
                })}
              </tbody>
            </table>
          )
        )}
      </div>

      {showModal && (
        <CrearOcModal
          items={selItems}
          proveedores={proveedores}
          onClose={() => setShowModal(false)}
          onDone={() => { setShowModal(false); setSelected(new Set()); fetchAll(); }}
        />
      )}
    </div>
  );
}
