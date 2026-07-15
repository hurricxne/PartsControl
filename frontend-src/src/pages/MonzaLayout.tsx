import { useState, createContext, useContext, useEffect, useCallback } from "react";
import { NavLink, Outlet, useNavigate, useLocation } from "react-router-dom";
import { monzaNotificacionesAPI } from "../services/monzaApi";
import {
  Users, FileText, TrendingUp, Settings, LogOut,
  ChevronDown, Bell, LayoutDashboard, Car, Sun, Moon, Truck, ScrollText,
  ShoppingCart, PackageSearch, Ship, Boxes, UserCog, Wallet, Receipt, DollarSign, Calculator, CreditCard, Landmark,
} from "lucide-react";
import { useAuthStore } from "../stores/authStore";

// ── Theme + personalización context (consumed by child pages) ─────────────────
interface MonzaThemeValue {
  dark: boolean;
  font: string;
  avatar: string;
  displayName: string;
  accent: string;
  setFont: (f: string) => void;
  setAvatar: (a: string) => void;
  setDisplayName: (n: string) => void;
  setAccent: (id: string) => void;
}
export const MonzaThemeCtx = createContext<MonzaThemeValue>({
  dark: false, font: "", avatar: "", displayName: "", accent: "rojo",
  setFont: () => {}, setAvatar: () => {}, setDisplayName: () => {}, setAccent: () => {},
});
export const useMonzaTheme = () => useContext(MonzaThemeCtx);

// Fuentes disponibles para personalización (stacks web-safe, sin carga externa)
export const MONZA_FONTS: { id: string; label: string; stack: string }[] = [
  { id: "system",  label: "Sistema",        stack: "system-ui, -apple-system, 'Segoe UI', sans-serif" },
  { id: "amplia",  label: "Amplia",         stack: "Verdana, Geneva, Tahoma, sans-serif" },
  { id: "redonda", label: "Redondeada",     stack: "'Trebuchet MS', 'Segoe UI', sans-serif" },
  { id: "clasica", label: "Clásica (serif)",stack: "Georgia, 'Times New Roman', serif" },
  { id: "compacta",label: "Compacta",       stack: "'Arial Narrow', Arial, sans-serif" },
  { id: "mono",    label: "Monoespaciada",  stack: "'Consolas', 'Courier New', monospace" },
];

// Colores de acento conmutables (hex en minúscula para no chocar con el theming)
export const MONZA_ACCENTS: { id: string; label: string; hex: string; rgb: string }[] = [
  { id: "rojo",  label: "Rojo Monza",     hex: "#8b1c1c", rgb: "139,28,28" },
  { id: "teal",  label: "Verde azulado",  hex: "#1c8b76", rgb: "28,139,118" },
  { id: "azul",  label: "Azul",           hex: "#1c4d8b", rgb: "28,77,139" },
  { id: "ambar", label: "Ámbar",          hex: "#8b461c", rgb: "139,70,28" },
];

const NAV_HEAD = [
  { to: "/monzaparts",              label: "Dashboard",     icon: LayoutDashboard, exact: true },
  { to: "/monzaparts/leads",        label: "Leads",         icon: Users },
  { to: "/monzaparts/cotizaciones", label: "Cotizaciones",  icon: FileText },
  { to: "/monzaparts/ventas",       label: "Ventas",        icon: TrendingUp },
];

// Módulos post-venta agrupados en dropdown "Operaciones"
const NAV_OPERACIONES = [
  { to: "/monzaparts/abastecimiento", label: "Abastecimiento", icon: ShoppingCart },
  { to: "/monzaparts/seguimiento",    label: "Seguimiento",   icon: PackageSearch },
  { to: "/monzaparts/logistica",      label: "Logística",     icon: Ship },
  { to: "/monzaparts/bodega",         label: "Bodega",        icon: Boxes },
  { to: "/monzaparts/despachos",      label: "Despachos",     icon: Truck },
];

// Módulos de Contabilidad agrupados en dropdown "Contabilidad" (SOLO MonzaParts)
const NAV_CONTABILIDAD = [
  { to: "/monzaparts/ventas-contab",     label: "Ventas",            icon: DollarSign },
  { to: "/monzaparts/facturas",          label: "Facturas y cobranzas", icon: Receipt },
  { to: "/monzaparts/compras-contab",    label: "Compras y pagos",   icon: CreditCard },
  { to: "/monzaparts/tesoreria",         label: "Tesorería",         icon: Landmark },
  { to: "/monzaparts/embarques-pricing", label: "Embarques Pricing", icon: Calculator },
];

const NAV_TAIL = [
  { to: "/monzaparts/logs",         label: "Logs",          icon: ScrollText },
  { to: "/monzaparts/configuracion",label: "Configuración", icon: Settings },
];

interface MonzaNotif {
  id: number; titulo: string; mensaje?: string; tipo: string;
  link?: string; leida: boolean; fecha: string;
}
const NOTIF_TIPO: Record<string, { color: string; bg: string }> = {
  info:    { color: "#1D4ED8", bg: "#DBEAFE" },
  success: { color: "#15803D", bg: "#DCFCE7" },
  warning: { color: "#B45309", bg: "#FEF3C7" },
  danger:  { color: "#B91C1C", bg: "#FEE2E2" },
};
function notifTimeAgo(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const m = Math.floor(diff / 60000);
  if (m < 1) return "ahora";
  if (m < 60) return `hace ${m} min`;
  const h = Math.floor(m / 60);
  if (h < 24) return `hace ${h} h`;
  return `hace ${Math.floor(h / 24)} d`;
}

const BREADCRUMB: Record<string, string> = {
  "/monzaparts":                "Dashboard",
  "/monzaparts/leads":          "Leads / Pipeline",
  "/monzaparts/cotizaciones":   "Cotizaciones",
  "/monzaparts/ventas":         "Ventas",
  "/monzaparts/ventas-contab":  "Ventas — Contabilidad",
  "/monzaparts/facturas":       "Facturas y Cobranzas",
  "/monzaparts/compras-contab": "Compras y Cuentas por Pagar",
  "/monzaparts/tesoreria":      "Tesorería",
  "/monzaparts/embarques-pricing": "Embarques — Pricing",
  "/monzaparts/abastecimiento": "Abastecimiento / Compras",
  "/monzaparts/seguimiento":    "Seguimiento",
  "/monzaparts/logistica":      "Logística / Importación",
  "/monzaparts/bodega":         "Bodega / Recepción",
  "/monzaparts/despachos":      "Histórico Despachos",
  "/monzaparts/logs":           "Logs de Operaciones",
  "/monzaparts/perfil":         "Mi Perfil",
  "/monzaparts/configuracion":  "Configuración",
};

export default function MonzaLayout() {
  const navigate  = useNavigate();
  const location  = useLocation();
  const logout    = useAuthStore((s) => s.logout);
  const user      = useAuthStore((s) => s.user);
  const [userOpen, setUserOpen] = useState(false);
  const [opsOpen, setOpsOpen] = useState(false);
  const [cntOpen, setCntOpen] = useState(false);
  const [dark, setDark] = useState(() => localStorage.getItem("monza-theme") === "dark");

  // Personalización (persistida en localStorage)
  const [font, setFontState] = useState(() => localStorage.getItem("monza-font") || MONZA_FONTS[0].stack);
  const [avatar, setAvatarState] = useState(() => localStorage.getItem("monza-avatar") || "");
  const [displayName, setDisplayNameState] = useState(() => localStorage.getItem("monza-displayname") || "");

  const [accent, setAccentState] = useState(() => localStorage.getItem("monza-accent") || "rojo");
  const accentObj = MONZA_ACCENTS.find((a) => a.id === accent) || MONZA_ACCENTS[0];

  // Notificaciones
  const [notifOpen, setNotifOpen] = useState(false);
  const [notifs, setNotifs] = useState<MonzaNotif[]>([]);
  const [unread, setUnread] = useState(0);

  const fetchCount = useCallback(async () => {
    try { const r = await monzaNotificacionesAPI.count(); setUnread(r.data.unread); } catch {}
  }, []);
  const fetchNotifs = useCallback(async () => {
    try { const r = await monzaNotificacionesAPI.list(); setNotifs(r.data); } catch {}
  }, []);

  useEffect(() => {
    fetchCount();
    const t = setInterval(fetchCount, 60000);
    return () => clearInterval(t);
  }, [fetchCount]);

  const openNotifs = () => {
    const next = !notifOpen;
    setNotifOpen(next);
    if (next) fetchNotifs();
  };
  const onNotifClick = async (n: MonzaNotif) => {
    if (!n.leida) { try { await monzaNotificacionesAPI.marcarLeida(n.id); } catch {} }
    setNotifOpen(false);
    fetchCount();
    if (n.link) navigate(n.link);
  };
  const marcarTodas = async () => {
    try { await monzaNotificacionesAPI.marcarTodas(); } catch {}
    fetchNotifs(); fetchCount();
  };

  const setFont = (f: string) => { setFontState(f); localStorage.setItem("monza-font", f); };
  const setAvatar = (a: string) => { setAvatarState(a); a ? localStorage.setItem("monza-avatar", a) : localStorage.removeItem("monza-avatar"); };
  const setDisplayName = (n: string) => { setDisplayNameState(n); n ? localStorage.setItem("monza-displayname", n) : localStorage.removeItem("monza-displayname"); };
  const setAccent = (id: string) => { setAccentState(id); localStorage.setItem("monza-accent", id); };

  const opsActive = NAV_OPERACIONES.some((it) => location.pathname.startsWith(it.to));
  const cntActive = NAV_CONTABILIDAD.some((it) => location.pathname.startsWith(it.to));

  const toggleTheme = () => {
    const next = !dark;
    setDark(next);
    localStorage.setItem("monza-theme", next ? "dark" : "light");
  };

  const nombre   = displayName || user?.nombre || "Asesor";
  const initials = nombre.split(" ").map((w: string) => w[0]).join("").slice(0, 2).toUpperCase();
  const email    = user?.email || "";
  const page     = BREADCRUMB[location.pathname] || "MonzaParts";

  // ── Palette ──────────────────────────────────────────────────────────────
  const D = {
    layoutBg:       dark ? "#0d1321" : "#F0F4F8",
    header:         "#0a0e1f",
    subBg:          dark ? "#0f1629" : "white",
    subBorder:      dark ? "#1e2a4a" : "#E2E8F0",
    subText:        dark ? "#8899cc" : "#64748B",
    subTextMain:    dark ? "#e2e8f0" : "#1E293B",
    footerBg:       "#0a0e1f",
    footerBorder:   "#1e2a4a",
  };

  return (
    <MonzaThemeCtx.Provider value={{ dark, font, avatar, displayName, accent, setFont, setAvatar, setDisplayName, setAccent }}>
      <style>{`:root{--monza-accent:${accentObj.hex};--monza-accent-rgb:${accentObj.rgb};} .monza-ic{color:var(--monza-accent);}`}</style>
      <div style={{ minHeight: "100vh", display: "flex", flexDirection: "column", background: D.layoutBg, fontFamily: font, transition: "background 0.25s" }}>

        {/* ── Top header ── */}
        <header style={{ background: D.header, borderBottom: "2px solid var(--monza-accent)", position: "sticky", top: 0, zIndex: 100, boxShadow: "0 2px 12px rgba(0,0,0,0.4)" }}>
          <div style={{ maxWidth: 1400, margin: "0 auto", padding: "0 24px", display: "flex", alignItems: "center", gap: 16, height: 58 }}>

            {/* Logo */}
            <NavLink to="/monzaparts" style={{ display: "flex", alignItems: "center", gap: 10, textDecoration: "none", flexShrink: 0 }}>
              <div style={{ background: "var(--monza-accent)", borderRadius: 8, width: 34, height: 34, display: "flex", alignItems: "center", justifyContent: "center", boxShadow: "0 2px 8px rgba(var(--monza-accent-rgb),0.5)" }}>
                <Car size={18} color="white" />
              </div>
              <div>
                <div style={{ color: "white", fontWeight: 800, fontSize: 15, letterSpacing: 1, lineHeight: 1 }}>MONZAPARTS</div>
                <div style={{ color: "#64748B", fontSize: 9, letterSpacing: 1.5, lineHeight: 1 }}>REPUESTOS AUTOMOTRICES</div>
              </div>
            </NavLink>

            <div style={{ width: 1, height: 28, background: "#1e2a4a", flexShrink: 0 }} />

            {/* Nav */}
            <nav style={{ display: "flex", gap: 2, flex: 1, alignItems: "center" }}>
              {NAV_HEAD.map(({ to, label, icon: Icon, exact }) => (
                <NavLink
                  key={to}
                  to={to}
                  end={exact}
                  style={({ isActive }) => ({
                    display: "flex", alignItems: "center", gap: 6,
                    padding: "6px 14px", borderRadius: 6,
                    textDecoration: "none", fontSize: 13, whiteSpace: "nowrap",
                    fontWeight: isActive ? 600 : 400,
                    color: isActive ? "white" : "#64748B",
                    background: isActive ? "var(--monza-accent)" : "transparent",
                    transition: "all 0.15s",
                  })}
                >
                  <Icon size={14} />{label}
                </NavLink>
              ))}

              {/* Operaciones (dropdown) */}
              <div style={{ position: "relative" }}>
                <button
                  onClick={() => { setCntOpen(false); setOpsOpen((v) => !v); }}
                  style={{
                    display: "flex", alignItems: "center", gap: 6,
                    padding: "6px 14px", borderRadius: 6, border: "none", cursor: "pointer",
                    fontSize: 13, whiteSpace: "nowrap", fontFamily: "inherit",
                    fontWeight: opsActive ? 600 : 400,
                    color: opsActive || opsOpen ? "white" : "#64748B",
                    background: opsActive ? "var(--monza-accent)" : opsOpen ? "#131b3e" : "transparent",
                    transition: "all 0.15s",
                  }}
                >
                  <Boxes size={14} />Operaciones
                  <ChevronDown size={12} style={{ transform: opsOpen ? "rotate(180deg)" : "none", transition: "transform 0.15s" }} />
                </button>

                {opsOpen && (
                  <div style={{ position: "absolute", left: 0, top: "calc(100% + 6px)", background: dark ? "#131b3e" : "white", border: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}`, borderRadius: 10, boxShadow: "0 10px 30px rgba(0,0,0,0.3)", minWidth: 200, zIndex: 200, padding: 6 }}>
                    {NAV_OPERACIONES.map(({ to, label, icon: Icon }) => (
                      <NavLink
                        key={to}
                        to={to}
                        onClick={() => setOpsOpen(false)}
                        style={({ isActive }) => ({
                          display: "flex", alignItems: "center", gap: 9,
                          padding: "9px 12px", borderRadius: 6, textDecoration: "none", fontSize: 13,
                          fontWeight: isActive ? 600 : 400,
                          color: isActive ? "white" : (dark ? "#cbd5e1" : "#475569"),
                          background: isActive ? "var(--monza-accent)" : "transparent",
                          marginBottom: 1,
                        })}
                      >
                        <Icon size={15} />{label}
                      </NavLink>
                    ))}
                  </div>
                )}
              </div>

              {/* Contabilidad (dropdown) — SOLO MonzaParts */}
              <div style={{ position: "relative" }}>
                <button
                  onClick={() => { setOpsOpen(false); setCntOpen((v) => !v); }}
                  style={{
                    display: "flex", alignItems: "center", gap: 6,
                    padding: "6px 14px", borderRadius: 6, border: "none", cursor: "pointer",
                    fontSize: 13, whiteSpace: "nowrap", fontFamily: "inherit",
                    fontWeight: cntActive ? 600 : 400,
                    color: cntActive || cntOpen ? "white" : "#64748B",
                    background: cntActive ? "var(--monza-accent)" : cntOpen ? "#131b3e" : "transparent",
                    transition: "all 0.15s",
                  }}
                >
                  <Wallet size={14} />Contabilidad
                  <ChevronDown size={12} style={{ transform: cntOpen ? "rotate(180deg)" : "none", transition: "transform 0.15s" }} />
                </button>

                {cntOpen && (
                  <div style={{ position: "absolute", left: 0, top: "calc(100% + 6px)", background: dark ? "#131b3e" : "white", border: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}`, borderRadius: 10, boxShadow: "0 10px 30px rgba(0,0,0,0.3)", minWidth: 210, zIndex: 200, padding: 6 }}>
                    {NAV_CONTABILIDAD.map(({ to, label, icon: Icon }) => (
                      <NavLink
                        key={to}
                        to={to}
                        onClick={() => setCntOpen(false)}
                        style={({ isActive }) => ({
                          display: "flex", alignItems: "center", gap: 9,
                          padding: "9px 12px", borderRadius: 6, textDecoration: "none", fontSize: 13,
                          fontWeight: isActive ? 600 : 400,
                          color: isActive ? "white" : (dark ? "#cbd5e1" : "#475569"),
                          background: isActive ? "var(--monza-accent)" : "transparent",
                          marginBottom: 1,
                        })}
                      >
                        <Icon size={15} />{label}
                      </NavLink>
                    ))}
                  </div>
                )}
              </div>

              {NAV_TAIL.map(({ to, label, icon: Icon }) => (
                <NavLink
                  key={to}
                  to={to}
                  style={({ isActive }) => ({
                    display: "flex", alignItems: "center", gap: 6,
                    padding: "6px 14px", borderRadius: 6,
                    textDecoration: "none", fontSize: 13, whiteSpace: "nowrap",
                    fontWeight: isActive ? 600 : 400,
                    color: isActive ? "white" : "#64748B",
                    background: isActive ? "var(--monza-accent)" : "transparent",
                    transition: "all 0.15s",
                  })}
                >
                  <Icon size={14} />{label}
                </NavLink>
              ))}
            </nav>

            {/* Right controls */}
            <div style={{ display: "flex", alignItems: "center", gap: 8, flexShrink: 0 }}>
              {/* Theme toggle */}
              <button
                onClick={toggleTheme}
                title={dark ? "Modo claro" : "Modo oscuro"}
                style={{ background: dark ? "#1e2a4a" : "#131b3e", border: "1px solid #1e2a4a", color: dark ? "#FCD34D" : "#94A3B8", cursor: "pointer", padding: "6px 8px", borderRadius: 8, display: "flex", alignItems: "center", transition: "all 0.2s" }}
              >
                {dark ? <Sun size={16} /> : <Moon size={16} />}
              </button>

              {/* Bell */}
              <div style={{ position: "relative" }}>
                <button onClick={openNotifs}
                  style={{ background: notifOpen ? "#131b3e" : "transparent", border: "1px solid #1e2a4a", color: unread > 0 ? "#FCD34D" : "#64748B", cursor: "pointer", padding: "6px 8px", borderRadius: 8, display: "flex", alignItems: "center", position: "relative" }}>
                  <Bell size={16} />
                  {unread > 0 && (
                    <span style={{ position: "absolute", top: -5, right: -5, background: "#EF4444", color: "white", fontSize: 9, fontWeight: 700, minWidth: 16, height: 16, borderRadius: 8, display: "flex", alignItems: "center", justifyContent: "center", padding: "0 4px", border: "2px solid #0a0e1f" }}>
                      {unread > 9 ? "9+" : unread}
                    </span>
                  )}
                </button>

                {notifOpen && (
                  <div style={{ position: "absolute", right: 0, top: "calc(100% + 8px)", width: 340, maxHeight: 440, background: dark ? "#131b3e" : "white", border: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}`, borderRadius: 12, boxShadow: "0 12px 36px rgba(0,0,0,0.35)", zIndex: 200, display: "flex", flexDirection: "column", overflow: "hidden" }}>
                    <div style={{ padding: "12px 16px", borderBottom: `1px solid ${dark ? "#1e2a4a" : "#F1F5F9"}`, display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                      <span style={{ fontSize: 14, fontWeight: 700, color: dark ? "white" : "#1E293B" }}>Notificaciones</span>
                      {unread > 0 && (
                        <button onClick={marcarTodas} style={{ background: "transparent", border: "none", cursor: "pointer", fontSize: 11, color: "var(--monza-accent)", fontWeight: 600 }}>
                          Marcar todas leídas
                        </button>
                      )}
                    </div>
                    <div style={{ overflowY: "auto", flex: 1 }}>
                      {notifs.length === 0 ? (
                        <div style={{ padding: "32px 16px", textAlign: "center", color: "#94A3B8", fontSize: 13 }}>
                          <Bell size={26} color="#CBD5E1" style={{ display: "block", margin: "0 auto 8px" }} />
                          Sin notificaciones
                        </div>
                      ) : notifs.map((n) => {
                        const t = NOTIF_TIPO[n.tipo] || NOTIF_TIPO.info;
                        return (
                          <div key={n.id} onClick={() => onNotifClick(n)}
                            style={{ padding: "11px 16px", borderBottom: `1px solid ${dark ? "#1a2340" : "#F1F5F9"}`, cursor: "pointer", display: "flex", gap: 10, background: n.leida ? "transparent" : (dark ? "#0f1830" : "#FAFBFF") }}>
                            <span style={{ width: 8, height: 8, borderRadius: "50%", background: t.color, flexShrink: 0, marginTop: 5 }} />
                            <div style={{ flex: 1, minWidth: 0 }}>
                              <div style={{ fontSize: 12.5, fontWeight: n.leida ? 500 : 700, color: dark ? "#e2e8f0" : "#1E293B" }}>{n.titulo}</div>
                              {n.mensaje && <div style={{ fontSize: 11.5, color: dark ? "#8899cc" : "#64748B", marginTop: 1 }}>{n.mensaje}</div>}
                              <div style={{ fontSize: 10.5, color: "#94A3B8", marginTop: 3 }}>{notifTimeAgo(n.fecha)}</div>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                )}
              </div>

              {/* User menu */}
              <div style={{ position: "relative" }}>
                <button
                  onClick={() => setUserOpen(!userOpen)}
                  style={{ display: "flex", alignItems: "center", gap: 8, background: "#131b3e", border: "1px solid #1e2a4a", borderRadius: 8, cursor: "pointer", color: "#E2E8F0", fontSize: 13, padding: "5px 10px 5px 6px" }}
                >
                  {avatar ? (
                    <img src={avatar} alt="" style={{ width: 28, height: 28, borderRadius: "50%", objectFit: "cover", flexShrink: 0 }} />
                  ) : (
                    <div style={{ width: 28, height: 28, borderRadius: "50%", background: "var(--monza-accent)", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 11, fontWeight: 700, color: "white", flexShrink: 0 }}>
                      {initials}
                    </div>
                  )}
                  <div style={{ textAlign: "left" }}>
                    <div style={{ fontSize: 12, fontWeight: 600, lineHeight: 1.2 }}>{nombre}</div>
                    <div style={{ fontSize: 10, color: "#64748B", lineHeight: 1.2 }}>Asesor</div>
                  </div>
                  <ChevronDown size={13} color="#64748B" />
                </button>

                {userOpen && (
                  <div style={{ position: "absolute", right: 0, top: "calc(100% + 6px)", background: dark ? "#131b3e" : "white", border: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}`, borderRadius: 10, boxShadow: "0 10px 30px rgba(0,0,0,0.3)", minWidth: 200, zIndex: 200 }}>
                    <div style={{ padding: "14px 16px", borderBottom: `1px solid ${dark ? "#1e2a4a" : "#F1F5F9"}` }}>
                      {avatar ? (
                        <img src={avatar} alt="" style={{ width: 40, height: 40, borderRadius: "50%", objectFit: "cover", marginBottom: 8 }} />
                      ) : (
                        <div style={{ width: 40, height: 40, borderRadius: "50%", background: "var(--monza-accent)", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 15, fontWeight: 700, color: "white", marginBottom: 8 }}>
                          {initials}
                        </div>
                      )}
                      <div style={{ fontSize: 14, fontWeight: 700, color: dark ? "white" : "#1E293B" }}>{nombre}</div>
                      <div style={{ fontSize: 11, color: "#64748B", marginTop: 1 }}>{email}</div>
                    </div>
                    <div style={{ padding: 6 }}>
                      <button
                        onClick={() => { setUserOpen(false); navigate("/monzaparts/perfil"); }}
                        style={{ width: "100%", display: "flex", alignItems: "center", gap: 8, padding: "9px 12px", background: "transparent", border: "none", borderRadius: 6, cursor: "pointer", fontSize: 13, color: dark ? "#cbd5e1" : "#475569", textAlign: "left" }}
                      >
                        <UserCog size={14} /> Mi perfil
                      </button>
                      <button
                        onClick={() => { logout(); navigate("/login"); }}
                        style={{ width: "100%", display: "flex", alignItems: "center", gap: 8, padding: "9px 12px", background: "transparent", border: "none", borderRadius: 6, cursor: "pointer", fontSize: 13, color: "#EF4444", textAlign: "left" }}
                      >
                        <LogOut size={14} /> Cerrar sesión
                      </button>
                    </div>
                  </div>
                )}
              </div>
            </div>
          </div>
        </header>

        {/* ── Breadcrumb ── */}
        <div style={{ background: D.subBg, borderBottom: `1px solid ${D.subBorder}`, padding: "9px 24px", transition: "background 0.25s" }}>
          <div style={{ maxWidth: 1400, margin: "0 auto", display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13 }}>
              <Car size={13} className="monza-ic" />
              <span style={{ color: "var(--monza-accent)", fontWeight: 600 }}>MonzaParts</span>
              <span style={{ color: D.subText }}>/</span>
              <span style={{ color: D.subTextMain, fontWeight: 500 }}>{page}</span>
            </div>
            <div style={{ fontSize: 11, color: D.subText }}>
              {new Date().toLocaleDateString("es-CL", { weekday: "long", year: "numeric", month: "long", day: "numeric" })}
            </div>
          </div>
        </div>

        {/* ── Content ── */}
        <main style={{ flex: 1, maxWidth: 1400, width: "100%", margin: "0 auto", padding: "28px 24px", boxSizing: "border-box" }}>
          <Outlet />
        </main>

        {/* ── Footer ── */}
        <footer style={{ background: D.footerBg, borderTop: `1px solid ${D.footerBorder}`, padding: "14px 24px" }}>
          <div style={{ maxWidth: 1400, margin: "0 auto", display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <div style={{ background: "var(--monza-accent)", borderRadius: 5, width: 20, height: 20, display: "flex", alignItems: "center", justifyContent: "center" }}>
                <Car size={11} color="white" />
              </div>
              <span style={{ color: "#64748B", fontSize: 12 }}>
                <strong style={{ color: "#94A3B8" }}>MonzaParts SpA</strong> · RUT 70.121.315-0
              </span>
            </div>
            <span style={{ color: "#475569", fontSize: 11 }}>
              Powered by <strong style={{ color: "#64748B" }}>BigCode</strong> · v1.0
            </span>
          </div>
        </footer>

        {userOpen && <div style={{ position: "fixed", inset: 0, zIndex: 99 }} onClick={() => setUserOpen(false)} />}
        {opsOpen && <div style={{ position: "fixed", inset: 0, zIndex: 99 }} onClick={() => setOpsOpen(false)} />}
        {cntOpen && <div style={{ position: "fixed", inset: 0, zIndex: 99 }} onClick={() => setCntOpen(false)} />}
        {notifOpen && <div style={{ position: "fixed", inset: 0, zIndex: 99 }} onClick={() => setNotifOpen(false)} />}
      </div>
    </MonzaThemeCtx.Provider>
  );
}
