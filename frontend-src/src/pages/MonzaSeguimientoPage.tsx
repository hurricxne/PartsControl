import { useState, useEffect, useCallback, useRef } from "react";
import { PackageSearch, Search, RefreshCw, ChevronDown, ChevronRight, Truck, X, Upload, Loader2 } from "lucide-react";
import { monzaAbastecimientoAPI, monzaRecepcionNacionalAPI, monzaDocumentosAPI, monzaTotalPendiente, monzaErrMsg } from "../services/monzaApi";
import type { MonzaPendienteNacionalItem, MonzaItemQty } from "../services/monzaApi";
import { hoyLocal } from "../utils/format";
import { useMonzaTheme } from "./MonzaLayout";
import MonzaDocs from "./MonzaDocs";
import toast from "react-hot-toast";

interface SegItem {
  id: number;
  cot_numero: string;
  cliente?: string;
  vehiculo?: string;
  descripcion: string;
  numero_parte?: string;
  marca?: string;
  cantidad: number;
  plazo_entrega?: string;
  estado_linea: string;
  fecha_venta?: string;
  oc_proveedor_id?: number | null;
  ocp_numero?: string;
  ocp_numero_oc?: string;
  ocp_proveedor?: string;
  ocp_estado?: string;
  ocp_awb?: string;
  ocp_tracking?: string;
  ocp_plazo_dias?: number;
  // 'nacional' → el ítem NO se prepara/embarca: su camino físico es el CTA
  // "Registrar entrega nacional" (camión + guía del proveedor, directo a bodega).
  tipo_origen?: string;
}

// ── Recepción nacional ────────────────────────────────────────────────────────
// Los 6 estados de recepción. Los "utilizables" (suman al tope físico en Despachos)
// exigen cantidad > 0; no_llego / dañado_no_utilizable no cuentan (quedan 'comprado').
// 'faltante' SUMA: su qty es lo que SÍ llegó bueno.
const ESTADOS_RECEPCION: { value: string; label: string; utilizable: boolean }[] = [
  { value: "completo",             label: "Completo",               utilizable: true },
  { value: "faltante",             label: "Faltante (llegó menos)", utilizable: true },
  { value: "sobrante",             label: "Sobrante (llegó más)",   utilizable: true },
  { value: "danado_utilizable",    label: "Dañado pero utilizable", utilizable: true },
  { value: "danado_no_utilizable", label: "Dañado no utilizable",   utilizable: false },
  { value: "no_llego",             label: "No llegó",               utilizable: false },
];
const ESTADO_UTILIZABLE = new Set(ESTADOS_RECEPCION.filter((e) => e.utilizable).map((e) => e.value));

// Un ítem nacional en estos estados admite registrar (otra) entrega del proveedor.
const NACIONAL_RECIBIBLE = new Set(["comprado", "en_bodega"]);

const ESTADO_LINEA: Record<string, { bg: string; color: string; label: string }> = {
  comprado:    { bg: "#DBEAFE", color: "#1D4ED8", label: "Comprado" },
  preparado:   { bg: "#FEF3C7", color: "#B45309", label: "Preparado" },
  embarcado:   { bg: "#EDE9FE", color: "#6D28D9", label: "Embarcado" },
  en_transito: { bg: "#EDE9FE", color: "#6D28D9", label: "En tránsito" },
  en_bodega:   { bg: "#DCFCE7", color: "#15803D", label: "En bodega" },
  // Hallazgo #18: la línea que cae a 'reclamo' (no llegó / dañada no utilizable)
  // desaparecía del pipeline sin explicación — solo reaparecía en Bodega → Reclamos.
  // El backend YA acepta ?estado=reclamo; faltaba poder pedirlo y pintarlo.
  reclamo:     { bg: "#FEE2E2", color: "#B91C1C", label: "Reclamo" },
};

function fmtDate(d?: string) { return d ? new Date(d).toLocaleDateString("es-CL") : "—"; }

/** Días transcurridos desde la venta y delta vs plazo prometido */
function plazoInfo(fechaVenta?: string, plazoDias?: number) {
  if (!fechaVenta || !plazoDias) return null;
  const trans = Math.floor((Date.now() - new Date(fechaVenta).getTime()) / 86400000);
  const delta = plazoDias - trans; // días restantes
  let color = "#15803D", bg = "#DCFCE7", label = `${delta}d restantes`;
  if (delta < 0) { color = "#B91C1C"; bg = "#FEE2E2"; label = `Vencido ${Math.abs(delta)}d`; }
  else if (delta <= 3) { color = "#B45309"; bg = "#FEF3C7"; label = `${delta}d restantes`; }
  return { trans, delta, color, bg, label };
}

// ── Modal Registrar entrega nacional ─────────────────────────────────────────
// El proveedor nacional llega con su camión y su guía de despacho. Se registra
// cuánto llegó de cada ítem; al enviar (cerrar:true) los utilizables con qty>0
// pasan a en_bodega y quedan despachables, capados por lo recibido (tope físico).
// Salta preparado/embarque. Fuente: GET /api/monza/recepcion-nacional/pendientes/{ocp_id}.
interface RowEntrega { incluir: boolean; qty: string; estado: string; obs: string }

function RegistrarEntregaNacionalModal({ ocpId, titulo, onClose, onSuccess }: {
  ocpId: number; titulo: string; onClose: () => void; onSuccess: () => void;
}) {
  const { dark } = useMonzaTheme();
  const [pendientes, setPendientes] = useState<MonzaPendienteNacionalItem[]>([]);
  const [rows, setRows] = useState<Record<number, RowEntrega>>({});
  const [loading, setLoading] = useState(true);
  const [numeroGuia, setNumeroGuia] = useState("");
  const [fecha, setFecha] = useState(hoyLocal());  // fecha LOCAL (toISOString es UTC: de noche daría mañana)
  const [observacion, setObservacion] = useState("");
  const [documento, setDocumento] = useState<string | null>(null);
  const [docNombre, setDocNombre] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [saving, setSaving] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const bg = dark ? "#131b3e" : "white";
  const bd = dark ? "#1e2a4a" : "#E2E8F0";
  const txt = dark ? "white" : "#1E293B";
  const sub = dark ? "#8899cc" : "#64748B";
  const IS = { padding: "7px 9px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 12, boxSizing: "border-box" as const, background: dark ? "#0d1321" : "#F8FAFC", color: txt };
  const lbl = { fontSize: 11, fontWeight: 600, color: sub, display: "block", marginBottom: 4, textTransform: "uppercase" as const, letterSpacing: 0.5 };

  useEffect(() => {
    setLoading(true);
    monzaRecepcionNacionalAPI.pendientes(ocpId)
      .then(({ data }) => {
        const items = data.items || [];
        setPendientes(items);
        // Precarga: qty = remanente (lo que falta por recibir de cada línea).
        const init: Record<number, RowEntrega> = {};
        items.forEach((it) => {
          init[it.item_cotizacion_id] = {
            incluir: it.remanente > 0,
            qty: it.remanente > 0 ? String(it.remanente) : "",
            estado: "completo",
            obs: "",
          };
        });
        setRows(init);
      })
      .catch((e: any) => toast.error(e?.response?.data?.detail || "Error al cargar ítems pendientes"))
      .finally(() => setLoading(false));
  }, [ocpId]);

  const setRow = (id: number, patch: Partial<RowEntrega>) =>
    setRows((prev) => ({ ...prev, [id]: { ...prev[id], ...patch } }));

  const onEstadoChange = (id: number, estado: string) => {
    // 'No llegó' no cuenta al tope: fuerza cantidad 0 y bloquea el input.
    if (estado === "no_llego") setRow(id, { estado, qty: "0" });
    else setRow(id, { estado });
  };

  // La guía escaneada se adjunta como documento de la OC (queda en su pestaña de
  // documentos) y su filename viaja en `documento` de la recepción (trazabilidad).
  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    try {
      const r = await monzaDocumentosAPI.upload("oc_proveedor", ocpId, file, "guía proveedor");
      setDocumento(r.data.filename);
      setDocNombre(r.data.original_name || file.name);
      toast.success("Guía adjuntada");
    } catch (err: any) {
      toast.error(err?.response?.data?.detail || "Error al subir la guía");
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const incluidos = pendientes.filter((it) => rows[it.item_cotizacion_id]?.incluir);

  const submit = async () => {
    if (incluidos.length === 0) {
      toast.error("Marca al menos un ítem que haya llegado en esta entrega");
      return;
    }
    // Validación espejo del backend: un estado utilizable exige cantidad > 0.
    for (const it of incluidos) {
      const r = rows[it.item_cotizacion_id];
      const qty = Number(r.qty);
      if (isNaN(qty) || qty < 0) {
        toast.error(`Cantidad inválida en ${it.numero_parte || it.item_cotizacion_id}`);
        return;
      }
      if (ESTADO_UTILIZABLE.has(r.estado) && qty <= 0) {
        toast.error(`${it.numero_parte || it.item_cotizacion_id}: "${ESTADOS_RECEPCION.find((e) => e.value === r.estado)?.label}" exige cantidad mayor a 0`);
        return;
      }
    }
    setSaving(true);
    try {
      await monzaRecepcionNacionalAPI.registrar({
        oc_proveedor_id: ocpId,
        numero_guia_proveedor: numeroGuia.trim() || undefined,
        fecha: fecha || undefined,
        documento: documento || undefined,
        observacion: observacion.trim() || undefined,
        cerrar: true,   // al tiro: los utilizables pasan a en_bodega (despachables)
        items: incluidos.map((it) => ({
          item_cotizacion_id: it.item_cotizacion_id,
          qty_recibida: Number(rows[it.item_cotizacion_id].qty) || 0,
          estado_recepcion: rows[it.item_cotizacion_id].estado,
          observacion: rows[it.item_cotizacion_id].obs.trim() || undefined,
        })),
      });
      toast.success("Entrega nacional registrada");
      onSuccess();
    } catch (e: any) {
      // Muestra el detail del backend (tope físico, pertenencia, estados, etc.)
      toast.error(e?.response?.data?.detail || "Error al registrar la entrega");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)", zIndex: 1000, display: "flex", alignItems: "center", justifyContent: "center", padding: 16 }}>
      <div style={{ background: bg, border: `1px solid ${bd}`, borderRadius: 14, width: "100%", maxWidth: 760, maxHeight: "90vh", display: "flex", flexDirection: "column", boxShadow: "0 24px 60px rgba(0,0,0,0.4)" }}>
        <div style={{ background: dark ? "#0a0e1f" : "#F8FAFC", borderBottom: `1px solid ${bd}`, padding: "14px 20px", display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
            <Truck size={18} className="monza-ic" />
            <div style={{ minWidth: 0 }}>
              <span style={{ fontWeight: 700, fontSize: 15, color: txt, display: "block" }}>Registrar entrega nacional</span>
              <span style={{ fontSize: 11, color: sub, display: "block", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{titulo}</span>
            </div>
          </div>
          <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", color: sub, display: "flex" }}><X size={18} /></button>
        </div>

        <div style={{ padding: "16px 20px", overflowY: "auto" }}>
          {/* Cabecera de la guía del proveedor */}
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12, marginBottom: 12 }}>
            <div>
              <label style={lbl}>N° guía del proveedor</label>
              <input value={numeroGuia} onChange={(e) => setNumeroGuia(e.target.value)} placeholder="Ej: 12345" style={{ ...IS, width: "100%" }} />
            </div>
            <div>
              <label style={lbl}>Fecha de recepción</label>
              <input type="date" value={fecha} onChange={(e) => setFecha(e.target.value)} style={{ ...IS, width: "100%" }} />
            </div>
            <div>
              <label style={lbl}>Guía escaneada (opcional)</label>
              <input ref={fileRef} type="file" style={{ display: "none" }} accept=".pdf,.jpg,.jpeg,.png,.webp" onChange={handleUpload} />
              <button type="button" onClick={() => fileRef.current?.click()} disabled={uploading}
                style={{ width: "100%", display: "flex", alignItems: "center", justifyContent: "center", gap: 6, padding: "7px 9px", border: `1px solid ${bd}`, borderRadius: 6, background: "transparent", color: sub, cursor: uploading ? "wait" : "pointer", fontSize: 12 }}>
                {uploading ? <Loader2 size={13} className="animate-spin" /> : <Upload size={13} />}
                {documento ? "Cambiar" : "Adjuntar"}
              </button>
              {docNombre && <div style={{ fontSize: 10, color: sub, marginTop: 3, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={docNombre}>📎 {docNombre}</div>}
            </div>
          </div>
          <div style={{ marginBottom: 12 }}>
            <label style={lbl}>Observación de la entrega</label>
            <input value={observacion} onChange={(e) => setObservacion(e.target.value)} placeholder="Opcional" style={{ ...IS, width: "100%" }} />
          </div>

          {/* Ítems por recibir */}
          {loading ? (
            <div style={{ padding: 30, textAlign: "center", color: "#94A3B8" }}>Cargando ítems...</div>
          ) : pendientes.length === 0 ? (
            <div style={{ padding: 24, textAlign: "center", color: "#94A3B8", fontSize: 12 }}>
              No hay ítems pendientes de recibir en esta OC nacional.
            </div>
          ) : (
            <div style={{ border: `1px solid ${bd}`, borderRadius: 10, overflow: "hidden" }}>
              <div style={{ overflowX: "auto" }}>
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
                  <thead>
                    <tr style={{ background: dark ? "#0d1321" : "#F8FAFC", borderBottom: `1px solid ${bd}` }}>
                      <th style={{ padding: "8px 10px", width: 28 }}>
                        <input type="checkbox"
                          checked={incluidos.length === pendientes.length && pendientes.length > 0}
                          onChange={(e) => {
                            const val = e.target.checked;
                            setRows((prev) => {
                              const next = { ...prev };
                              pendientes.forEach((it) => { next[it.item_cotizacion_id] = { ...next[it.item_cotizacion_id], incluir: val }; });
                              return next;
                            });
                          }}
                          style={{ accentColor: "var(--monza-accent)" }} />
                      </th>
                      {["N° Parte", "Descripción", "Vendido", "Recibido", "Remanente", "Llegó (qty)", "Estado", "Observación"].map((h) => (
                        <th key={h} style={{ padding: "8px 10px", textAlign: "left", fontWeight: 600, fontSize: 10, color: sub, textTransform: "uppercase" as const, letterSpacing: 0.5, whiteSpace: "nowrap" }}>{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {pendientes.map((it) => {
                      const r = rows[it.item_cotizacion_id] || { incluir: false, qty: "", estado: "completo", obs: "" };
                      const noLlego = r.estado === "no_llego";
                      return (
                        <tr key={it.item_cotizacion_id} style={{ borderBottom: `1px solid ${dark ? "#1e2a4a" : "#F1F5F9"}`, opacity: r.incluir ? 1 : 0.5 }}>
                          <td style={{ padding: "6px 10px", textAlign: "center" }}>
                            <input type="checkbox" checked={r.incluir}
                              onChange={() => setRow(it.item_cotizacion_id, { incluir: !r.incluir })}
                              style={{ accentColor: "var(--monza-accent)" }} />
                          </td>
                          <td style={{ padding: "6px 10px", fontWeight: 600, color: "var(--monza-accent)", whiteSpace: "nowrap" }}>{it.numero_parte || "—"}</td>
                          <td style={{ padding: "6px 10px", color: txt, maxWidth: 160, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={it.descripcion || ""}>{it.descripcion || "—"}</td>
                          <td style={{ padding: "6px 10px", textAlign: "center", color: sub }}>{it.cantidad}</td>
                          <td style={{ padding: "6px 10px", textAlign: "center", color: sub }}>{it.recibido || 0}</td>
                          <td style={{ padding: "6px 10px", textAlign: "center", fontWeight: 700, color: txt }}>{it.remanente}</td>
                          <td style={{ padding: "6px 10px" }}>
                            <input type="number" min={0} step="any" value={r.qty} disabled={!r.incluir || noLlego}
                              onChange={(e) => setRow(it.item_cotizacion_id, { qty: e.target.value })}
                              style={{ ...IS, width: 70 }} />
                          </td>
                          <td style={{ padding: "6px 10px" }}>
                            <select value={r.estado} disabled={!r.incluir}
                              onChange={(e) => onEstadoChange(it.item_cotizacion_id, e.target.value)} style={IS}>
                              {ESTADOS_RECEPCION.map((e) => <option key={e.value} value={e.value}>{e.label}</option>)}
                            </select>
                          </td>
                          <td style={{ padding: "6px 10px" }}>
                            <input value={r.obs} disabled={!r.incluir} placeholder="—"
                              onChange={(e) => setRow(it.item_cotizacion_id, { obs: e.target.value })}
                              style={{ ...IS, width: 110 }} />
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          <p style={{ margin: "10px 0 0", fontSize: 11, color: sub }}>
            Al registrar, los ítems utilizables con cantidad recibida pasan a <b>bodega</b> y quedan
            despachables (topeados por lo recibido). "No llegó" y "Dañado no utilizable" no cuentan
            y siguen pendientes.
          </p>
        </div>

        <div style={{ padding: "12px 20px", borderTop: `1px solid ${bd}`, display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <button onClick={onClose} style={{ padding: "8px 18px", border: `1px solid ${bd}`, borderRadius: 8, background: "transparent", color: sub, cursor: "pointer", fontSize: 13 }}>Cancelar</button>
          <button onClick={submit} disabled={saving || loading || incluidos.length === 0}
            style={{ padding: "8px 20px", background: "var(--monza-accent)", border: "none", borderRadius: 8, color: "white", cursor: saving ? "wait" : "pointer", fontWeight: 700, fontSize: 13, display: "flex", alignItems: "center", gap: 6, opacity: saving || loading || incluidos.length === 0 ? 0.6 : 1 }}>
            {saving ? <Loader2 size={14} className="animate-spin" /> : <Truck size={14} />}
            {saving ? "Registrando..." : `Registrar entrega (${incluidos.length})`}
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * Devolver a compras un ítem que el proveedor dejó en BACK ORDER (caso Baukat).
 *
 * Pide cantidad y motivo porque las dos cosas se pierden si no se piden acá: la
 * cantidad porque el back order suele ser PARCIAL (mandan 6 de 10), y el motivo porque
 * esta es la única transición que va hacia atrás en el pipeline y borra el vínculo con
 * la OC — sin él, meses después nadie sabe si fue back order, error o cancelación.
 */
function DevolverAComprasModal({ item, onClose, onSuccess }: {
  item: SegItem;
  onClose: () => void;
  onSuccess: () => void;
}) {
  const { dark } = useMonzaTheme();
  const [cantidad, setCantidad] = useState(item.cantidad);
  const [motivo, setMotivo] = useState("Back order del proveedor");
  const [saving, setSaving] = useState(false);
  const bd = dark ? "#1e2a4a" : "#E2E8F0"; const txt = dark ? "white" : "#1E293B";
  const sub = dark ? "#8899cc" : "#64748B"; const bg = dark ? "#131b3e" : "white";
  const inp = { width: "100%", padding: "8px 10px", border: `1px solid ${bd}`, borderRadius: 8,
    fontSize: 13, background: dark ? "#0d1321" : "#F8FAFC", color: txt, boxSizing: "border-box" as const };
  const parcial = cantidad < item.cantidad;
  const valido = cantidad >= 1 && cantidad <= item.cantidad && motivo.trim().length >= 3;

  const submit = async () => {
    setSaving(true);
    try {
      // Cantidad completa → se manda SIN `cantidad` (la línea entera, sin partirla).
      await monzaAbastecimientoAPI.devolverACompras(
        [{ item_id: item.id, ...(parcial ? { cantidad } : {}) }], motivo.trim());
      toast.success(parcial
        ? `${cantidad} de ${item.cantidad} volvieron al panel de compras`
        : "El ítem volvió al panel de compras");
      onSuccess();
    } catch (e: unknown) {
      toast.error(monzaErrMsg(e, "No se pudo devolver a compras"));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.55)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 60, padding: 16 }} onClick={onClose}>
      <div onClick={(e) => e.stopPropagation()} style={{ background: bg, border: `1px solid ${bd}`, borderRadius: 14, width: "100%", maxWidth: 460, overflow: "hidden" }}>
        <div style={{ padding: "14px 18px", borderBottom: `1px solid ${bd}`, display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <span style={{ fontWeight: 700, fontSize: 14, color: txt }}>Devolver a compras</span>
          <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", color: sub, display: "flex" }}><X size={18} /></button>
        </div>
        <div style={{ padding: "16px 18px", display: "flex", flexDirection: "column", gap: 12 }}>
          <div style={{ fontSize: 12, color: sub, lineHeight: 1.5 }}>
            <b style={{ color: txt }}>{item.descripcion}</b>{item.numero_parte ? ` · ${item.numero_parte}` : ""}
            {item.ocp_numero ? <> — comprado en la OC <b style={{ color: txt }}>{item.ocp_numero}</b></> : null}
            {item.ocp_proveedor ? ` a ${item.ocp_proveedor}` : ""}.
            <br />Vuelve al <b style={{ color: txt }}>panel de compras</b> para comprarlo de nuevo.
          </div>
          <div>
            <label style={{ fontSize: 12, fontWeight: 600, color: sub, display: "block", marginBottom: 4 }}>
              Cantidad que vuelve <span style={{ fontWeight: 400 }}>(de {item.cantidad})</span>
            </label>
            <input type="number" min={1} max={item.cantidad} step={1} value={cantidad}
              onChange={(e) => setCantidad(Math.max(1, Math.min(item.cantidad, Math.round(Number(e.target.value) || 0))))}
              style={inp} />
            {parcial && (
              <p style={{ fontSize: 11, color: "#B45309", margin: "5px 0 0" }}>
                Las otras {item.cantidad - cantidad} unidades siguen compradas en la misma OC.
              </p>
            )}
          </div>
          <div>
            <label style={{ fontSize: 12, fontWeight: 600, color: sub, display: "block", marginBottom: 4 }}>Motivo *</label>
            <input value={motivo} onChange={(e) => setMotivo(e.target.value)}
              placeholder="Ej: back order confirmado por el proveedor" style={inp} />
          </div>
        </div>
        <div style={{ padding: "12px 18px", borderTop: `1px solid ${bd}`, display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <button onClick={onClose} style={{ padding: "8px 16px", border: `1px solid ${bd}`, borderRadius: 8, background: "transparent", color: sub, cursor: "pointer", fontSize: 13 }}>Cancelar</button>
          <button onClick={submit} disabled={saving || !valido}
            style={{ padding: "8px 18px", border: "none", borderRadius: 8, background: saving || !valido ? sub : "#B45309", color: "white", cursor: saving || !valido ? "not-allowed" : "pointer", fontWeight: 700, fontSize: 13 }}>
            {saving ? "Devolviendo…" : "Devolver a compras"}
          </button>
        </div>
      </div>
    </div>
  );
}

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

export default function MonzaSeguimientoPage() {
  const { dark } = useMonzaTheme();
  const [items, setItems] = useState<SegItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState("");
  const [estado, setEstado] = useState("");
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const toggleExp = (id: number) => setExpanded((prev) => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const [selPrep, setSelPrep] = useState<Set<number>>(new Set());
  const togglePrep = (id: number) => setSelPrep((p) => { const n = new Set(p); n.has(id) ? n.delete(id) : n.add(id); return n; });
  // Modal "Registrar entrega nacional" (OC nacional: camión + guía, sin embarque)
  const [entregaOcp, setEntregaOcp] = useState<{ id: number; titulo: string } | null>(null);
  // Ítem que se va a devolver al panel de compras (back order). Guarda el ítem entero
  // porque el modal muestra su OC y su cantidad para que el operador confirme sobre
  // datos, no de memoria.
  const [devolver, setDevolver] = useState<SegItem | null>(null);
  // Cantidad a PREPARAR por ítem (envío parcial: el proveedor mandó 6 de 10). Sin
  // tocar nada vale toda la línea; si se baja, el backend parte la línea y el
  // remanente sigue en "Comprado" esperando el próximo embarque.
  const [qtyPrep, setQtyPrep] = useState<Record<number, number>>({});
  const qtyPrepDe = (it: SegItem) => qtyPrep[it.id] ?? it.cantidad;
  const preparar = async () => {
    // Solo los ítems con cantidad REBAJADA viajan con `cantidad`; si nadie tocó nada,
    // el servicio manda el pedido por la vía legada (línea completa, sin partir).
    const pedidos: MonzaItemQty[] = Array.from(selPrep).map((id) => {
      const it = items.find((c) => c.id === id);
      const q = it ? qtyPrepDe(it) : undefined;
      return it && q !== undefined && q < it.cantidad ? { item_id: id, cantidad: q } : { item_id: id };
    });
    try {
      const r = await monzaAbastecimientoAPI.preparar(pedidos);
      const pend = monzaTotalPendiente(r.data.remanentes);
      toast.success(
        `${r.data.preparados} ítem(s) → Logística (por embarcar)`
        + (pend > 0 ? ` · quedan ${pend} unidad(es) en Comprado esperando el próximo embarque` : ""),
      );
      setSelPrep(new Set()); setQtyPrep({}); fetchAll();
    } catch (e: unknown) {
      // El backend explica el motivo (cantidad inválida, estado equivocado, o la
      // línea ya tiene guía/factura encima): mostrarlo es lo único útil aquí.
      toast.error(monzaErrMsg(e, "Error al preparar"));
    }
  };

  const bg = dark ? "#131b3e" : "white";
  const bd = dark ? "#1e2a4a" : "#E2E8F0";
  const txt = dark ? "white" : "#1E293B";
  const sub = dark ? "#8899cc" : "#64748B";

  const fetchAll = useCallback(async () => {
    setLoading(true);
    try {
      const res = await monzaAbastecimientoAPI.seguimiento({ q: q || undefined, estado: estado || undefined });
      setItems(res.data);
    } catch { toast.error("Error al cargar seguimiento"); }
    finally { setLoading(false); }
  }, [q, estado]);

  useEffect(() => { fetchAll(); }, [fetchAll]);

  const counts = {
    comprado: items.filter((i) => i.estado_linea === "comprado").length,
    preparado: items.filter((i) => i.estado_linea === "preparado").length,
    embarcado: items.filter((i) => i.estado_linea === "embarcado").length,
    en_bodega: items.filter((i) => i.estado_linea === "en_bodega").length,
  };

  return (
    <div>
      {/* Header */}
      <div style={{ marginBottom: 18 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
          <PackageSearch size={22} className="monza-ic" />
          <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: txt }}>Seguimiento</h1>
        </div>
        <p style={{ margin: 0, fontSize: 13, color: sub }}>
          Monitoreo de ítems comprados. Marca los <strong>comprados</strong> con el check y pulsa <strong>"Preparar → por embarcar"</strong> para enviarlos a Logística.
          Si el proveedor envió solo una parte, baja la <strong>Cant.</strong> de esa fila: el resto sigue en <strong>Comprado</strong> esperando el próximo embarque.
        </p>
      </div>

      {/* KPIs */}
      <div style={{ display: "flex", gap: 10, marginBottom: 18, flexWrap: "wrap" }}>
        <KpiCard label="Comprado" value={counts.comprado} accent="#3B82F6" />
        <KpiCard label="Preparado" value={counts.preparado} accent="#F59E0B" />
        <KpiCard label="Embarcado" value={counts.embarcado} accent="#6366F1" />
        <KpiCard label="En bodega" value={counts.en_bodega} accent="#10B981" />
      </div>

      {/* Toolbar */}
      <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 12, flexWrap: "wrap" }}>
        <div style={{ position: "relative", flex: 1, minWidth: 240 }}>
          <Search size={14} style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)", color: "#94A3B8" }} />
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Buscar ítem, N° COT..."
            style={{ width: "100%", padding: "8px 10px 8px 32px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 13, boxSizing: "border-box" as const, background: bg, color: txt }} />
        </div>
        <select value={estado} onChange={(e) => setEstado(e.target.value)}
          style={{ padding: "8px 12px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 13, background: bg, color: txt }}>
          <option value="">Todos los estados</option>
          <option value="comprado">Comprado</option>
          <option value="preparado">Preparado</option>
          <option value="embarcado">Embarcado</option>
          <option value="en_bodega">En bodega</option>
          {/* Hallazgo #18: 'reclamo' NO va en la lista por defecto del backend (es
              estado terminal de excepción: mezclarlo infla los contadores del
              pipeline activo), pero sí debe poder consultarse a demanda. */}
          <option value="reclamo">Reclamo</option>
        </select>
        {selPrep.size > 0 && (
          <button onClick={preparar} style={{ padding: "8px 16px", background: "#F59E0B", border: "none", borderRadius: 8, color: "white", cursor: "pointer", fontWeight: 700, fontSize: 13, display: "flex", alignItems: "center", gap: 6 }}>
            <PackageSearch size={14} /> Preparar {selPrep.size} → por embarcar
          </button>
        )}
        <button onClick={fetchAll} style={{ padding: "8px 10px", border: `1px solid ${bd}`, borderRadius: 6, background: bg, cursor: "pointer", color: sub }}>
          <RefreshCw size={14} />
        </button>
      </div>

      {/* Table */}
      <div style={{ background: bg, border: `1px solid ${bd}`, borderRadius: 10, overflow: "hidden" }}>
        {loading ? (
          <div style={{ padding: 40, textAlign: "center", color: "#94A3B8" }}>Cargando...</div>
        ) : items.length === 0 ? (
          <div style={{ padding: 48, textAlign: "center", color: "#94A3B8" }}>
            <PackageSearch size={32} color="#E2E8F0" style={{ display: "block", margin: "0 auto 8px" }} />
            No hay ítems en seguimiento.
          </div>
        ) : (
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr style={{ background: dark ? "#0d1321" : "#F8FAFC", borderBottom: `1px solid ${bd}` }}>
                <th style={{ width: 30 }}></th>
                <th style={{ width: 30 }} title="Seleccionar comprados para preparar"></th>
                {["N° COT", "Cliente", "Repuesto", "Cant.", "OC Proveedor", "Proveedor", "AWB / Tracking", "Plazo", "Estado"].map((h) => (
                  <th key={h} style={{ padding: "10px 12px", textAlign: h === "Cant." ? "right" : "left", fontWeight: 600, fontSize: 11, color: sub, textTransform: "uppercase" as const, letterSpacing: 0.5 }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {items.flatMap((it) => {
                const es = ESTADO_LINEA[it.estado_linea] || { bg: "#F1F5F9", color: "#64748B", label: it.estado_linea };
                const pz = plazoInfo(it.fecha_venta, it.ocp_plazo_dias);
                const isExp = expanded.has(it.id);
                // Camino físico nacional: sin preparar/embarcar — la UI oculta el check
                // Y el backend rechaza con 400 (nunca solo la UI). El CTA abre el modal
                // de entrega mientras el ítem siga recibible (comprado/en_bodega).
                const esNacional = it.tipo_origen === "nacional" && it.oc_proveedor_id != null;
                const abrirEntrega = () => setEntregaOcp({
                  id: it.oc_proveedor_id as number,
                  titulo: `${it.ocp_numero_oc || it.ocp_numero || "OC"} · ${it.ocp_proveedor || "Proveedor"}`,
                });
                const rows = [
                  <tr key={it.id} style={{ borderBottom: isExp ? "none" : `1px solid ${dark ? "#1e2a4a" : "#F1F5F9"}`, background: selPrep.has(it.id) ? (dark ? "#1a2340" : "#FFFBEB") : "transparent" }}>
                    <td style={{ textAlign: "center", color: "#94A3B8", cursor: "pointer" }} onClick={() => toggleExp(it.id)}>
                      {isExp ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                    </td>
                    <td style={{ textAlign: "center" }}>
                      {esNacional ? (
                        NACIONAL_RECIBIBLE.has(it.estado_linea) && (
                          <button onClick={abrirEntrega} title="Registrar entrega nacional (camión + guía del proveedor)"
                            style={{ background: "none", border: "none", cursor: "pointer", color: "#15803D", display: "flex", margin: "0 auto", padding: 2 }}>
                            <Truck size={14} />
                          </button>
                        )
                      ) : it.estado_linea === "comprado" && (
                        <input type="checkbox" checked={selPrep.has(it.id)} onChange={() => togglePrep(it.id)} title="Marcar como preparado (por embarcar)" style={{ accentColor: "var(--monza-accent)", cursor: "pointer" }} />
                      )}
                    </td>
                    <td style={{ padding: "9px 12px", fontWeight: 600, color: "var(--monza-accent)", fontSize: 12 }}>{it.cot_numero}</td>
                    <td style={{ padding: "9px 12px", color: txt }}>{it.cliente || "—"}</td>
                    <td style={{ padding: "9px 12px" }}>
                      <div style={{ color: txt, fontWeight: 500 }}>{it.descripcion}</div>
                      {it.numero_parte && <div style={{ fontSize: 10, color: sub }}>{it.numero_parte}{it.marca ? ` · ${it.marca}` : ""}</div>}
                    </td>
                    {/* Cant. es editable SOLO mientras el ítem se puede preparar: así el
                        envío parcial se teclea en la misma fila donde se marca el check.
                        El <td> corta el click por si la fila gana un handler. */}
                    <td style={{ padding: "9px 12px", textAlign: "right", color: txt }} onClick={(e) => e.stopPropagation()}>
                      {!esNacional && it.estado_linea === "comprado" ? (
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
                            <span title="Preparación parcial: el resto sigue en Comprado esperando el próximo embarque"
                              style={{ fontSize: 10, fontWeight: 700, background: "#FEF3C7", color: "#B45309", padding: "1px 7px", borderRadius: 999, whiteSpace: "nowrap" }}>
                              {it.cantidad - qtyPrepDe(it)} pendiente
                            </span>
                          )}
                        </span>
                      ) : it.cantidad}
                    </td>
                    <td style={{ padding: "9px 12px", color: sub, fontSize: 12 }}>
                      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                        {it.ocp_numero_oc || it.ocp_numero || "—"}
                        {esNacional && (
                          <span title="OC nacional: camión + guía del proveedor, sin embarque"
                            style={{ fontSize: 10, fontWeight: 700, background: "#DCFCE7", color: "#15803D", padding: "1px 7px", borderRadius: 999, display: "inline-flex", alignItems: "center", gap: 3 }}>
                            <Truck size={9} /> Nacional
                          </span>
                        )}
                      </div>
                    </td>
                    <td style={{ padding: "9px 12px", color: sub, fontSize: 12 }}>{it.ocp_proveedor || "—"}</td>
                    <td style={{ padding: "9px 12px", color: sub, fontSize: 11 }}>
                      {it.ocp_awb ? <div>AWB: {it.ocp_awb}</div> : null}
                      {it.ocp_tracking ? <div>Trk: {it.ocp_tracking}</div> : null}
                      {!it.ocp_awb && !it.ocp_tracking ? "—" : null}
                    </td>
                    <td style={{ padding: "9px 12px" }}>
                      {pz ? (
                        <span style={{ fontSize: 11, background: pz.bg, color: pz.color, padding: "2px 8px", borderRadius: 8, fontWeight: 600 }}>{pz.label}</span>
                      ) : (
                        <span style={{ fontSize: 12, color: sub }}>{it.ocp_plazo_dias ? `${it.ocp_plazo_dias}d` : "—"}</span>
                      )}
                    </td>
                    <td style={{ padding: "9px 12px" }}>
                      <span style={{ fontSize: 11, background: es.bg, color: es.color, padding: "3px 10px", borderRadius: 10, fontWeight: 600 }}>{es.label}</span>
                      {esNacional && NACIONAL_RECIBIBLE.has(it.estado_linea) && (
                        <button onClick={abrirEntrega}
                          style={{ display: "block", marginTop: 4, background: "none", border: "none", cursor: "pointer", color: "#15803D", fontSize: 10, fontWeight: 700, padding: 0, textAlign: "left", fontFamily: "inherit" }}>
                          Registrar entrega nacional →
                        </button>
                      )}
                      {/* BACK ORDER: solo desde 'comprado' — una vez preparado o
                          embarcado la mercadería ya salió del proveedor y esto deja de
                          tener sentido (el backend rechaza igual). */}
                      {it.estado_linea === "comprado" && (
                        <button onClick={(e) => { e.stopPropagation(); setDevolver(it); }}
                          title="El proveedor no lo va a enviar (back order): devolver al panel de compras"
                          style={{ display: "block", marginTop: 4, background: "none", border: "none", cursor: "pointer", color: "#B45309", fontSize: 10, fontWeight: 700, padding: 0, textAlign: "left", fontFamily: "inherit" }}>
                          ← Devolver a compras
                        </button>
                      )}
                    </td>
                  </tr>,
                ];
                if (isExp) {
                  rows.push(
                    <tr key={`doc-${it.id}`} style={{ borderBottom: `1px solid ${bd}` }}>
                      <td colSpan={11} style={{ padding: "8px 16px 14px 42px", background: dark ? "#0a0e1f" : "#F8FAFF" }}>
                        <MonzaDocs entidad="item" entidadId={it.id} categorias={["foto", "certificado", "documento adicional", "otro"]} titulo="Documentos adicionales del ítem" />
                      </td>
                    </tr>
                  );
                }
                return rows;
              })}
            </tbody>
          </table>
        )}
      </div>

      {entregaOcp && (
        <RegistrarEntregaNacionalModal
          ocpId={entregaOcp.id}
          titulo={entregaOcp.titulo}
          onClose={() => setEntregaOcp(null)}
          onSuccess={() => { setEntregaOcp(null); fetchAll(); }}
        />
      )}

      {devolver && (
        <DevolverAComprasModal
          item={devolver}
          onClose={() => setDevolver(null)}
          onSuccess={() => { setDevolver(null); fetchAll(); }}
        />
      )}
    </div>
  );
}
