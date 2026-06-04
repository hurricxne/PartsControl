import { useState, useRef } from "react";
import { UserCog, Camera, Trash2, Type, Check, Palette, Droplet } from "lucide-react";
import { useMonzaTheme, MONZA_FONTS, MONZA_ACCENTS } from "./MonzaLayout";
import { useAuthStore } from "../stores/authStore";
import toast from "react-hot-toast";

export default function MonzaPerfilPage() {
  const { dark, font, avatar, displayName, accent, setFont, setAvatar, setDisplayName, setAccent } = useMonzaTheme();
  const user = useAuthStore((s) => s.user);
  const fileRef = useRef<HTMLInputElement>(null);
  const [nameInput, setNameInput] = useState(displayName || user?.nombre || "");

  const bg = dark ? "#131b3e" : "white";
  const bd = dark ? "#1e2a4a" : "#E2E8F0";
  const txt = dark ? "white" : "#1E293B";
  const sub = dark ? "#8899cc" : "#64748B";

  const initials = (nameInput || "A").split(" ").map((w) => w[0]).join("").slice(0, 2).toUpperCase();

  const onPickFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    if (file.size > 2 * 1024 * 1024) { toast.error("La imagen no puede superar 2 MB"); return; }
    const reader = new FileReader();
    reader.onload = () => {
      // Redimensionar a máx 256px para no llenar localStorage
      const img = new Image();
      img.onload = () => {
        const max = 256;
        const scale = Math.min(1, max / Math.max(img.width, img.height));
        const w = Math.round(img.width * scale);
        const h = Math.round(img.height * scale);
        const canvas = document.createElement("canvas");
        canvas.width = w; canvas.height = h;
        const ctx = canvas.getContext("2d");
        if (ctx) {
          ctx.drawImage(img, 0, 0, w, h);
          const dataUrl = canvas.toDataURL("image/jpeg", 0.85);
          setAvatar(dataUrl);
          toast.success("Imagen de perfil actualizada");
        }
      };
      img.src = reader.result as string;
    };
    reader.readAsDataURL(file);
  };

  const saveName = () => {
    setDisplayName(nameInput.trim());
    toast.success("Nombre actualizado");
  };

  const Card = ({ icon: Icon, title, desc, children }: { icon: React.ElementType; title: string; desc: string; children: React.ReactNode }) => (
    <div style={{ background: bg, border: `1px solid ${bd}`, borderRadius: 12, padding: "20px 22px", marginBottom: 16 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
        <Icon size={17} className="monza-ic" />
        <h2 style={{ margin: 0, fontSize: 15, fontWeight: 700, color: txt }}>{title}</h2>
      </div>
      <p style={{ margin: "0 0 16px", fontSize: 12, color: sub }}>{desc}</p>
      {children}
    </div>
  );

  return (
    <div style={{ maxWidth: 720, margin: "0 auto" }}>
      {/* Header */}
      <div style={{ marginBottom: 20 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
          <UserCog size={22} className="monza-ic" />
          <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: txt }}>Mi Perfil</h1>
        </div>
        <p style={{ margin: 0, fontSize: 13, color: sub }}>
          Personaliza tu cuenta: imagen, nombre visible y la tipografía de todo el sistema.
        </p>
      </div>

      {/* Avatar */}
      <Card icon={Camera} title="Imagen de perfil" desc="Se muestra en la barra superior. JPG o PNG, máx 2 MB (se ajusta automáticamente).">
        <div style={{ display: "flex", alignItems: "center", gap: 20 }}>
          {avatar ? (
            <img src={avatar} alt="" style={{ width: 80, height: 80, borderRadius: "50%", objectFit: "cover", border: `2px solid ${bd}` }} />
          ) : (
            <div style={{ width: 80, height: 80, borderRadius: "50%", background: "var(--monza-accent)", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 28, fontWeight: 700, color: "white" }}>
              {initials}
            </div>
          )}
          <div style={{ display: "flex", gap: 8 }}>
            <input ref={fileRef} type="file" accept="image/png,image/jpeg,image/webp" style={{ display: "none" }} onChange={onPickFile} />
            <button onClick={() => fileRef.current?.click()}
              style={{ padding: "8px 16px", background: "var(--monza-accent)", border: "none", borderRadius: 8, color: "white", cursor: "pointer", fontWeight: 600, fontSize: 13, display: "flex", alignItems: "center", gap: 6 }}>
              <Camera size={14} /> Subir imagen
            </button>
            {avatar && (
              <button onClick={() => { setAvatar(""); toast.success("Imagen eliminada"); }}
                style={{ padding: "8px 14px", background: "transparent", border: `1px solid ${bd}`, borderRadius: 8, color: "#EF4444", cursor: "pointer", fontWeight: 600, fontSize: 13, display: "flex", alignItems: "center", gap: 6 }}>
                <Trash2 size={14} /> Quitar
              </button>
            )}
          </div>
        </div>
      </Card>

      {/* Nombre visible */}
      <Card icon={UserCog} title="Nombre visible" desc="Cómo aparece tu nombre en la interfaz (no cambia el de inicio de sesión).">
        <div style={{ display: "flex", gap: 8 }}>
          <input value={nameInput} onChange={(e) => setNameInput(e.target.value)}
            placeholder={user?.nombre || "Tu nombre"}
            style={{ flex: 1, padding: "9px 12px", border: `1px solid ${bd}`, borderRadius: 8, fontSize: 13, background: dark ? "#0d1321" : "#F8FAFC", color: txt, boxSizing: "border-box" }} />
          <button onClick={saveName}
            style={{ padding: "9px 18px", background: "var(--monza-accent)", border: "none", borderRadius: 8, color: "white", cursor: "pointer", fontWeight: 600, fontSize: 13, display: "flex", alignItems: "center", gap: 6 }}>
            <Check size={14} /> Guardar
          </button>
        </div>
        <div style={{ fontSize: 11, color: sub, marginTop: 8 }}>Email de la cuenta: <strong style={{ color: txt }}>{user?.email || "—"}</strong></div>
      </Card>

      {/* Tipografía */}
      <Card icon={Type} title="Tipografía del sistema" desc="Cambia la fuente de toda la interfaz de MonzaParts. Se aplica al instante.">
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))", gap: 10 }}>
          {MONZA_FONTS.map((f) => {
            const active = font === f.stack;
            return (
              <button key={f.id} onClick={() => { setFont(f.stack); toast.success(`Fuente: ${f.label}`); }}
                style={{
                  textAlign: "left", padding: "12px 14px", borderRadius: 10, cursor: "pointer",
                  border: `2px solid ${active ? "var(--monza-accent)" : bd}`,
                  background: active ? (dark ? "#1a1015" : "#FFF7F7") : (dark ? "#0d1321" : "#F8FAFC"),
                }}>
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 6 }}>
                  <span style={{ fontSize: 12, fontWeight: 700, color: active ? "var(--monza-accent)" : sub }}>{f.label}</span>
                  {active && <Check size={14} className="monza-ic" />}
                </div>
                <div style={{ fontFamily: f.stack, fontSize: 16, color: txt, lineHeight: 1.3 }}>
                  MonzaParts 123
                </div>
                <div style={{ fontFamily: f.stack, fontSize: 11, color: sub }}>
                  Repuestos AaBbCc
                </div>
              </button>
            );
          })}
        </div>
      </Card>

      {/* Color de acento */}
      <Card icon={Droplet} title="Color de acento" desc="Color principal de botones, encabezados, badges e iconos en todo el sistema.">
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(150px, 1fr))", gap: 10 }}>
          {MONZA_ACCENTS.map((a) => {
            const active = accent === a.id;
            return (
              <button key={a.id} onClick={() => { setAccent(a.id); toast.success(`Acento: ${a.label}`); }}
                style={{
                  display: "flex", alignItems: "center", gap: 10, padding: "10px 12px", borderRadius: 10, cursor: "pointer",
                  border: `2px solid ${active ? a.hex : bd}`,
                  background: active ? `${a.hex}14` : (dark ? "#0d1321" : "#F8FAFC"),
                }}>
                <span style={{ width: 26, height: 26, borderRadius: "50%", background: a.hex, flexShrink: 0, boxShadow: active ? `0 0 0 3px ${a.hex}40` : "none", display: "flex", alignItems: "center", justifyContent: "center" }}>
                  {active && <Check size={14} color="white" />}
                </span>
                <span style={{ fontSize: 13, fontWeight: active ? 700 : 500, color: active ? a.hex : txt }}>{a.label}</span>
              </button>
            );
          })}
        </div>
      </Card>

      {/* Tema (atajo informativo) */}
      <Card icon={Palette} title="Tema de color" desc="El modo claro/oscuro se cambia con el botón ☀️/🌙 de la barra superior.">
        <div style={{ fontSize: 13, color: sub }}>
          Tema actual: <strong style={{ color: txt }}>{dark ? "Oscuro 🌙" : "Claro ☀️"}</strong>
        </div>
      </Card>
    </div>
  );
}
