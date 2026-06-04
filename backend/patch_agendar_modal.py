"""Mejora UI del AgendarModal: datetime con contraste, layout más amplio, theme-aware."""

PATH = "/var/www/machparts.bigcode.cl/frontend-src/src/pages/MonzaLeadsPage.tsx"

with open(PATH) as f:
    c = f.read()

OLD = '''// ── Agendar Modal ──────────────────────────────────────────────────────────
function AgendarModal({ leadId, onClose, onDone }: { leadId: number; onClose: () => void; onDone: () => void }) {
  const [tipo, setTipo] = useState("Llamada");
  const [cuando, setCuando] = useState("");
  const [saving, setSaving] = useState(false);
  const submit = async () => {
    setSaving(true);
    try {
      await monzaLeadsAPI.agendarPaso(leadId, { tipo: tipo.toLowerCase(), cuando: cuando || undefined });
      toast.success("Próximo paso agendado");
      onDone();
      onClose();
    } catch { toast.error("Error al agendar"); }
    finally { setSaving(false); }
  };
  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.4)", zIndex: 1002, display: "flex", alignItems: "center", justifyContent: "center", padding: 16 }}>
      <div style={{ background: "white", borderRadius: 12, width: "100%", maxWidth: 400, boxShadow: "0 20px 60px rgba(0,0,0,0.3)" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "14px 18px", borderBottom: "1px solid #F1F5F9" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}><Calendar size={16} color="#8B1C1C" /><h3 style={{ margin: 0, fontSize: 15, fontWeight: 700 }}>Agendar próximo paso</h3></div>
          <button onClick={onClose} style={{ background: "transparent", border: "none", cursor: "pointer", color: "#94A3B8" }}><X size={16} /></button>
        </div>
        <div style={{ padding: 18 }}>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 14 }}>
            <div>
              <label style={{ fontSize: 11, fontWeight: 600, color: "#64748B", display: "block", marginBottom: 4 }}>Tipo</label>
              <select value={tipo} onChange={(e) => setTipo(e.target.value)} style={{ width: "100%", padding: "8px 10px", border: "1px solid #D1D5DB", borderRadius: 6, fontSize: 13, background: "white", color: "#1E293B" }}>
                {["Llamada","WhatsApp","Email","Visita"].map((o) => <option key={o} style={{ background: "white", color: "#1E293B" }}>{o}</option>)}
              </select>
            </div>
            <div>
              <label style={{ fontSize: 11, fontWeight: 600, color: "#64748B", display: "block", marginBottom: 4 }}>Cuándo</label>
              <input type="datetime-local" value={cuando} onChange={(e) => setCuando(e.target.value)} style={{ width: "100%", padding: "8px 10px", border: "1px solid #E2E8F0", borderRadius: 6, fontSize: 13, boxSizing: "border-box" }} />
            </div>
          </div>
          <div style={{ display: "flex", gap: 10, justifyContent: "flex-end" }}>
            <button onClick={onClose} style={{ padding: "9px 16px", border: "1px solid #E2E8F0", borderRadius: 8, background: "white", cursor: "pointer", fontSize: 13, color: "#475569" }}>Cancelar</button>
            <button onClick={submit} disabled={saving} style={{ padding: "9px 16px", border: "none", borderRadius: 8, background: "#8B1C1C", cursor: "pointer", fontSize: 13, color: "white", fontWeight: 600, opacity: saving ? 0.7 : 1 }}>
              {saving ? "Agendando..." : "Agendar"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}'''

NEW = '''// ── Agendar Modal ──────────────────────────────────────────────────────────
const TIPO_ICONS: Record<string, string> = { Llamada: "📞", WhatsApp: "💬", Email: "✉️", Visita: "🤝" };

function AgendarModal({ leadId, onClose, onDone }: { leadId: number; onClose: () => void; onDone: () => void }) {
  const { dark } = useMonzaTheme();
  const [tipo, setTipo] = useState("Llamada");
  const [cuando, setCuando] = useState("");
  const [saving, setSaving] = useState(false);

  const bg    = dark ? "#131b3e" : "white";
  const bd    = dark ? "#1e2a4a" : "#E2E8F0";
  const text  = dark ? "white"   : "#1E293B";
  const sub   = dark ? "#8899cc" : "#64748B";
  const inpSt = { width: "100%", padding: "9px 11px", border: `1px solid ${dark ? "#1e2a4a" : "#D1D5DB"}`, borderRadius: 8, fontSize: 13, background: dark ? "#0d1321" : "white", color: text, boxSizing: "border-box" as const, outline: "none" };

  const submit = async () => {
    if (!cuando) { toast.error("Selecciona fecha y hora"); return; }
    setSaving(true);
    try {
      await monzaLeadsAPI.agendarPaso(leadId, { tipo: tipo.toLowerCase(), cuando });
      toast.success("Próximo paso agendado");
      onDone(); onClose();
    } catch { toast.error("Error al agendar"); }
    finally { setSaving(false); }
  };

  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)", zIndex: 1002, display: "flex", alignItems: "center", justifyContent: "center", padding: 16 }}>
      <div style={{ background: bg, borderRadius: 14, width: "100%", maxWidth: 420, boxShadow: "0 24px 64px rgba(0,0,0,0.35)", border: `1px solid ${bd}` }}>
        {/* Header */}
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "16px 20px", borderBottom: `1px solid ${bd}` }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <div style={{ background: "#8B1C1C22", borderRadius: 8, padding: "6px 8px" }}>
              <Calendar size={16} color="#8B1C1C" />
            </div>
            <h3 style={{ margin: 0, fontSize: 15, fontWeight: 700, color: text }}>Agendar próximo paso</h3>
          </div>
          <button onClick={onClose} style={{ background: "transparent", border: "none", cursor: "pointer", color: sub, lineHeight: 1 }}><X size={16} /></button>
        </div>

        {/* Body */}
        <div style={{ padding: "20px 20px" }}>
          {/* Tipo — botones visuales */}
          <div style={{ marginBottom: 16 }}>
            <label style={{ fontSize: 11, fontWeight: 600, color: sub, display: "block", marginBottom: 8, textTransform: "uppercase", letterSpacing: 0.5 }}>Tipo de contacto</label>
            <div style={{ display: "flex", gap: 8 }}>
              {["Llamada", "WhatsApp", "Email", "Visita"].map((t) => (
                <button
                  key={t}
                  onClick={() => setTipo(t)}
                  style={{
                    flex: 1, padding: "10px 6px", borderRadius: 8, border: `2px solid ${tipo === t ? "#8B1C1C" : bd}`,
                    background: tipo === t ? "#8B1C1C15" : (dark ? "#0d1321" : "#F8FAFC"),
                    color: tipo === t ? "#8B1C1C" : sub,
                    cursor: "pointer", fontSize: 11, fontWeight: tipo === t ? 700 : 400,
                    display: "flex", flexDirection: "column", alignItems: "center", gap: 4,
                    transition: "all 0.15s",
                  }}
                >
                  <span style={{ fontSize: 18 }}>{TIPO_ICONS[t]}</span>
                  {t}
                </button>
              ))}
            </div>
          </div>

          {/* Fecha y hora */}
          <div style={{ marginBottom: 20 }}>
            <label style={{ fontSize: 11, fontWeight: 600, color: sub, display: "block", marginBottom: 8, textTransform: "uppercase", letterSpacing: 0.5 }}>Fecha y hora</label>
            <input
              type="datetime-local"
              value={cuando}
              onChange={(e) => setCuando(e.target.value)}
              style={{ ...inpSt, colorScheme: dark ? "dark" : "light" }}
            />
          </div>

          {/* Acciones */}
          <div style={{ display: "flex", gap: 10, justifyContent: "flex-end" }}>
            <button onClick={onClose} style={{ padding: "10px 18px", border: `1px solid ${bd}`, borderRadius: 8, background: "transparent", cursor: "pointer", fontSize: 13, color: sub, fontWeight: 500 }}>
              Cancelar
            </button>
            <button onClick={submit} disabled={saving} style={{ padding: "10px 18px", border: "none", borderRadius: 8, background: "#8B1C1C", cursor: saving ? "not-allowed" : "pointer", fontSize: 13, color: "white", fontWeight: 600, opacity: saving ? 0.7 : 1, display: "flex", alignItems: "center", gap: 6 }}>
              <Calendar size={14} />{saving ? "Agendando..." : "Agendar"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}'''

if OLD in c:
    c = c.replace(OLD, NEW)
    print("✓ AgendarModal reemplazado con nuevo diseño")
else:
    print("! AgendarModal no encontrado con texto exacto")

with open(PATH, "w") as f:
    f.write(c)
print("✅ Guardado")
