// Modal COMPARTIDO (MonzaParts) para registrar a mano el folio de un documento que el
// SII ya aceptó y cuyo folio nunca llegó al sistema. Lo usan las DOS pantallas donde
// aparece ese callejón: Despachos (guía 52) y Facturas (factura 33).
//
// POR QUÉ ES UN COMPONENTE Y NO UNA COPIA EN CADA PÁGINA
// Es una confirmación de seguridad sobre un documento tributario irreversible: el
// operador teclea el folio DOS veces y la segunda es la constancia de que lo leyó del
// documento real. Con dos copias, el día que una gane una validación y la otra no, la
// pantalla más débil manda. Una sola implementación, dos llamadores.
//
// El componente NO decide nada: valida la forma (numérico, dos veces igual) para no
// gastar un viaje al servidor, y el backend vuelve a validarlo TODO —incluida la
// consulta a Wasabil, que es la única que puede contradecir al operador—.
import { useState } from "react";
import { KeyRound, Loader2, X, AlertTriangle } from "lucide-react";
import toast from "react-hot-toast";
import { useMonzaTheme } from "./MonzaLayout";

/** Folio del SII: correlativo numérico. Misma regla que valida el backend. */
const FOLIO_MAX = 18;
export function folioSiiValido(folio: string): boolean {
  const f = (folio || "").trim();
  return f.length > 0 && f.length <= FOLIO_MAX && /^[0-9]+$/.test(f) && Number(f) > 0;
}

interface Props {
  /** "guía" | "factura" — sólo para los textos. */
  sustantivo: string;
  /** Identificación legible del documento (N° de despacho o de factura). */
  referencia: string;
  /** Llama al endpoint correspondiente. Recibe folio y su confirmación. */
  onRegistrar: (folio: string, confirmo: string) => Promise<void>;
  onClose: () => void;
}

export default function MonzaRegistrarFolioModal({ sustantivo, referencia, onRegistrar, onClose }: Props) {
  const { dark } = useMonzaTheme();
  const text = dark ? "#E2E8F0" : "#0f172a";
  const muted = dark ? "#94A3B8" : "#64748B";
  const cardBg = dark ? "#131b3e" : "white";
  const cardBd = `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}`;
  const inputBg = dark ? "#0d1430" : "white";
  const inp: React.CSSProperties = {
    width: "100%", padding: "8px 12px", borderRadius: 8, border: cardBd,
    background: inputBg, color: text, fontSize: 14, fontFamily: "inherit",
    boxSizing: "border-box",
  };

  const [folio, setFolio] = useState("");
  const [confirmo, setConfirmo] = useState("");
  const [saving, setSaving] = useState(false);

  const formaOk = folioSiiValido(folio);
  const coinciden = folio.trim() !== "" && folio.trim() === confirmo.trim();
  const puede = formaOk && coinciden && !saving;

  const submit = async () => {
    if (!puede) return;
    setSaving(true);
    try {
      await onRegistrar(folio.trim(), confirmo.trim());
      onClose();
    } catch (e: unknown) {
      // El mensaje del backend es el que sirve (nombra folios, contradicciones, etc.):
      // se muestra tal cual en vez de uno genérico.
      const detalle = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      toast.error(detalle || `No se pudo registrar el folio de la ${sustantivo}`);
    } finally { setSaving(false); }
  };

  return (
    <div style={{ position: "fixed", inset: 0, zIndex: 320, display: "flex", alignItems: "center", justifyContent: "center", background: "rgba(0,0,0,0.6)", padding: 16 }} onClick={onClose}>
      <div style={{ width: "100%", maxWidth: 460, borderRadius: 14, border: cardBd, background: cardBg, boxShadow: "0 20px 50px rgba(0,0,0,0.4)" }} onClick={e => e.stopPropagation()}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "12px 18px", borderBottom: cardBd }}>
          <h3 style={{ fontSize: 14, fontWeight: 600, color: text, margin: 0 }}>
            Registrar folio del SII · {referencia}
          </h3>
          <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", color: muted, padding: 4 }}><X size={16} /></button>
        </div>
        <div style={{ padding: 18, display: "flex", flexDirection: "column", gap: 14 }}>
          <div style={{ display: "flex", gap: 10, padding: 12, borderRadius: 10, background: dark ? "#3f2d0a" : "#FFFBEB", border: `1px solid ${dark ? "#78530f" : "#FDE68A"}` }}>
            <AlertTriangle size={18} style={{ color: "#B45309", flexShrink: 0, marginTop: 1 }} />
            <p style={{ fontSize: 12, color: dark ? "#FDE68A" : "#92400E", margin: 0, lineHeight: 1.5 }}>
              El SII <b>ya aceptó</b> esta {sustantivo}, pero su folio no llegó al sistema.
              Esto <b>no emite nada</b>: sólo anota el número que ya existe. Cópialo del
              documento en <b>app.wasabil.com</b> — si escribes otro, el sistema lo compara
              con lo que dice Wasabil y lo rechaza.
            </p>
          </div>

          <label style={{ display: "block" }}>
            <span style={{ display: "block", fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 4, color: muted }}>
              Folio del SII
            </span>
            <input style={inp} value={folio} autoFocus inputMode="numeric"
              onChange={e => setFolio(e.target.value)} placeholder="Ej. 137" />
          </label>
          {folio.trim() !== "" && !formaOk && (
            <p style={{ fontSize: 11, color: "#B91C1C", margin: "-8px 0 0" }}>
              El folio del SII es un número correlativo (hasta {FOLIO_MAX} dígitos).
            </p>
          )}

          <label style={{ display: "block" }}>
            <span style={{ display: "block", fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 4, color: muted }}>
              Repite el folio
            </span>
            {/* Sin pegar: repetirlo a mano es la constancia de que se leyó el documento.
                Copiar y pegar el mismo error dos veces no confirma nada. */}
            <input style={inp} value={confirmo} inputMode="numeric"
              onPaste={e => e.preventDefault()}
              onChange={e => setConfirmo(e.target.value)} placeholder="Escríbelo de nuevo" />
          </label>
          {confirmo.trim() !== "" && !coinciden && (
            <p style={{ fontSize: 11, color: "#B91C1C", margin: "-8px 0 0" }}>
              Los dos folios no coinciden.
            </p>
          )}

          <button onClick={submit} disabled={!puede}
            style={{
              width: "100%", display: "flex", alignItems: "center", justifyContent: "center",
              gap: 8, padding: "10px 14px", borderRadius: 8, border: "none",
              background: "#B45309", color: "white", fontWeight: 600, fontSize: 14,
              fontFamily: "inherit", opacity: puede ? 1 : 0.5,
              cursor: puede ? "pointer" : "not-allowed",
            }}>
            {saving ? <Loader2 className="animate-spin" size={16} /> : <KeyRound size={16} />}
            Registrar folio
          </button>
        </div>
      </div>
    </div>
  );
}
