import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import {
  AreaChart, Area, ResponsiveContainer, Tooltip,
} from "recharts";
import {
  Users, TrendingUp, FileText, Target, Clock,
  AlertCircle, ChevronRight, Car, DollarSign, ArrowUpRight, ArrowDownRight,
} from "lucide-react";
import { monzaLeadsAPI, monzaVentasAPI } from "../services/monzaApi";
import { useAuthStore } from "../stores/authStore";
import { useMonzaTheme } from "./MonzaLayout";

interface LeadsKPI {
  nuevos_mes: number;
  en_proceso: number;
  vendidos_mes: number;
  // null = todavía no entró ningún lead este mes, así que no hay tasa que calcular.
  // Imprimirlo sin comprobar mostraba «null% tasa de cierre» en la primera pantalla que
  // ve el dueño; un 0% tampoco sirve: diría «lo estamos haciendo pésimo».
  tasa_cierre_pct: number | null;
  total_cotizado_mes: number;
  sin_contactar_3d: number;
}
interface VentasKPI {
  vendidas_mes: number;
  total_mes: number;
  pendientes_entrega: number;
}
interface LeadRow {
  id: number; numero: string; estado: string;
  vehiculo?: string; total_estimado?: number;
  fecha_creacion: string; cliente?: { nombre: string };
}

const ESTADO_STYLE: Record<string, { bg: string; color: string; label: string }> = {
  pendiente:  { bg: "rgba(100,116,139,0.15)", color: "#94A3B8", label: "Pendiente" },
  en_proceso: { bg: "rgba(59,130,246,0.15)",  color: "#60A5FA", label: "En proceso" },
  vendido:    { bg: "rgba(16,185,129,0.15)",  color: "#34D399", label: "Vendido" },
  rechazado:  { bg: "rgba(239,68,68,0.15)",   color: "#F87171", label: "Rechazado" },
};

function fmt(n: number) { return `$${Math.round(n).toLocaleString("es-CL")}`; }
function fmtDate(d: string) { return new Date(d).toLocaleDateString("es-CL", { day: "2-digit", month: "short" }); }

// Genera datos de sparkline que terminan en `current`
function spark(current: number, points = 8): { v: number }[] {
  const arr: { v: number }[] = [];
  for (let i = 0; i < points - 1; i++) {
    const base = current * (0.5 + (i / points) * 0.5);
    arr.push({ v: Math.max(0, base + (Math.random() - 0.4) * current * 0.3) });
  }
  arr.push({ v: current });
  return arr;
}

// ── Large KPI card con sparkline ────────────────────────────────────────────
function BigCard({
  label, value, sub, color, data, dark, onClick,
}: {
  label: string; value: string; sub?: string; color: string;
  data: { v: number }[]; dark: boolean; onClick?: () => void;
}) {
  const bg   = dark ? "#131b3e" : "white";
  const bdColor = dark ? "#1e2a4a" : "#E2E8F0";
  return (
    <div
      onClick={onClick}
      style={{ flex: 1, minWidth: 220, background: bg, border: `1px solid ${bdColor}`, borderRadius: 14, padding: "22px 22px 0", overflow: "hidden", cursor: onClick ? "pointer" : "default", position: "relative" }}
    >
      <div style={{ fontSize: 11, color: "#8899cc", fontWeight: 500, letterSpacing: 0.5, marginBottom: 6 }}>{label}</div>
      <div style={{ fontSize: 32, fontWeight: 800, color: "white", lineHeight: 1, marginBottom: 4 }}>{value}</div>
      {sub && (
        <div style={{ display: "flex", alignItems: "center", gap: 4, marginBottom: 12 }}>
          <ArrowUpRight size={13} color={color} />
          <span style={{ fontSize: 12, color, fontWeight: 600 }}>{sub}</span>
        </div>
      )}
      <ResponsiveContainer width="100%" height={80}>
        <AreaChart data={data} margin={{ top: 4, right: 0, left: 0, bottom: 0 }}>
          <defs>
            <linearGradient id={`g-${label.slice(0,4)}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%"  stopColor={color} stopOpacity={0.4} />
              <stop offset="95%" stopColor={color} stopOpacity={0} />
            </linearGradient>
          </defs>
          <Tooltip
            contentStyle={{ background: dark ? "#0a0e1f" : "white", border: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}`, borderRadius: 6, fontSize: 11, color: dark ? "white" : "#1E293B" }}
            formatter={(v: number) => [Math.round(v), ""]}
            labelFormatter={() => ""}
          />
          <Area type="monotone" dataKey="v" stroke={color} strokeWidth={2} fill={`url(#g-${label.slice(0,4)})`} dot={false} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

// ── Medium card con progress bar ────────────────────────────────────────────
function MidCard({
  label, value, goal, color, icon: Icon, dark, sub, onClick,
}: {
  label: string; value: number; goal: number; color: string;
  icon: React.ElementType; dark: boolean; sub?: string; onClick?: () => void;
}) {
  const bg  = dark ? "#131b3e" : "white";
  const bd  = dark ? "#1e2a4a" : "#E2E8F0";
  const pct = goal > 0 ? Math.min(100, Math.round((value / goal) * 100)) : 0;
  return (
    <div
      onClick={onClick}
      style={{ flex: 1, minWidth: 200, background: bg, border: `1px solid ${bd}`, borderRadius: 14, padding: "20px 22px", cursor: onClick ? "pointer" : "default" }}
    >
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", marginBottom: 12 }}>
        <div>
          <div style={{ fontSize: 28, fontWeight: 800, color: "white", lineHeight: 1 }}>{value}</div>
          <div style={{ fontSize: 11, color: "#8899cc", marginTop: 4 }}>{label}</div>
        </div>
        <div style={{ background: `${color}22`, borderRadius: 10, padding: 8 }}>
          <Icon size={18} color={color} />
        </div>
      </div>
      {sub && <div style={{ fontSize: 11, color: "#8899cc", marginBottom: 10 }}>{sub}</div>}
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, color: "#8899cc", marginBottom: 6 }}>
        <span>{pct}% alcanzado</span>
        <span>Meta: {goal}</span>
      </div>
      <div style={{ height: 5, background: dark ? "#1e2a4a" : "#F1F5F9", borderRadius: 3, overflow: "hidden" }}>
        <div style={{ width: `${pct}%`, height: "100%", background: color, borderRadius: 3, transition: "width 0.8s ease" }} />
      </div>
    </div>
  );
}

// ── Small stat card ─────────────────────────────────────────────────────────
function SmCard({
  label, value, icon: Icon, color, change, dark,
}: {
  label: string; value: string | number; icon: React.ElementType;
  color: string; change?: number; dark: boolean;
}) {
  const bg = dark ? "#131b3e" : "white";
  const bd = dark ? "#1e2a4a" : "#E2E8F0";
  const isPos = (change ?? 0) >= 0;
  return (
    <div style={{ flex: 1, minWidth: 160, background: bg, border: `1px solid ${bd}`, borderRadius: 14, padding: "18px 20px" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
        <div style={{ background: `${color}22`, borderRadius: 8, padding: 7 }}>
          <Icon size={16} color={color} />
        </div>
        {change !== undefined && (
          <div style={{ display: "flex", alignItems: "center", gap: 2, fontSize: 11, color: isPos ? "#34D399" : "#F87171", fontWeight: 600 }}>
            {isPos ? <ArrowUpRight size={12} /> : <ArrowDownRight size={12} />}
            {Math.abs(change)}%
          </div>
        )}
      </div>
      <div style={{ fontSize: 22, fontWeight: 800, color: "white", lineHeight: 1, marginBottom: 4 }}>{value}</div>
      <div style={{ fontSize: 11, color: "#8899cc" }}>{label}</div>
    </div>
  );
}

// ── Main component ───────────────────────────────────────────────────────────
export default function MonzaDashboardPage() {
  const navigate     = useNavigate();
  const { dark }     = useMonzaTheme();
  const user         = useAuthStore((s) => s.user);
  const nombre       = user?.nombre?.split(" ")[0] || "Asesor";

  const [lk, setLk]           = useState<LeadsKPI | null>(null);
  const [vk, setVk]           = useState<VentasKPI | null>(null);
  const [leads, setLeads]      = useState<LeadRow[]>([]);
  const [loading, setLoading]  = useState(true);

  useEffect(() => {
    Promise.all([
      monzaLeadsAPI.kpis(),
      monzaVentasAPI.kpis(),
      monzaLeadsAPI.list({ page_size: 6, page: 1 }),
    ])
      .then(([l, v, ll]) => { setLk(l.data); setVk(v.data); setLeads(ll.data.items || []); })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const hora   = new Date().getHours();
  const saludo = hora < 12 ? "Buenos días" : hora < 19 ? "Buenas tardes" : "Buenas noches";

  // Paleta según tema
  const C = {
    bg:      dark ? "rgba(13,19,33,0.6)"     : "white",
    bd:      dark ? "#1e2a4a"                : "#E2E8F0",
    text:    dark ? "white"                  : "#1E293B",
    textSub: dark ? "#8899cc"                : "#64748B",
    row:     dark ? "rgba(255,255,255,0.03)" : "#FAFAFA",
    tHead:   dark ? "#0d1321"                : "#F8FAFC",
  };

  // Sparkline data (generado una vez por render, suficiente para decoración)
  const sparkCot   = spark(lk?.total_cotizado_mes   ?? 0);
  const sparkVenta = spark(vk?.total_mes             ?? 0);
  const sparkLeads = spark(lk?.nuevos_mes            ?? 0);

  const GOAL_LEADS  = 20;
  const GOAL_VENTAS = 10;
  const GOAL_COT    = 15;

  if (loading) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        <div style={{ display: "flex", gap: 14 }}>
          {[1,2,3].map(i => <div key={i} style={{ flex: 1, height: 160, background: dark ? "#131b3e" : "#E2E8F0", borderRadius: 14 }} />)}
        </div>
        <div style={{ display: "flex", gap: 14 }}>
          {[1,2,3].map(i => <div key={i} style={{ flex: 1, height: 120, background: dark ? "#131b3e" : "#E2E8F0", borderRadius: 14 }} />)}
        </div>
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>

      {/* ── Welcome banner ── */}
      <div style={{
        background: "linear-gradient(135deg, #0a0e1f 0%, #131b3e 60%, var(--monza-accent) 100%)",
        borderRadius: 14, padding: "24px 28px",
        display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 12,
        border: "1px solid #1e2a4a",
      }}>
        <div>
          <div style={{ fontSize: 12, color: "#8899cc", marginBottom: 2 }}>{saludo},</div>
          <div style={{ fontSize: 26, fontWeight: 800, color: "white", lineHeight: 1.1 }}>{nombre}!</div>
          {lk?.sin_contactar_3d ? (
            <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 8, fontSize: 12, color: "#FCA5A5" }}>
              <AlertCircle size={13} />
              {lk.sin_contactar_3d} leads sin contactar hace más de 3 días
            </div>
          ) : (
            <div style={{ marginTop: 8, fontSize: 12, color: "#34D399" }}>✓ Todo al día — sin leads pendientes de contacto</div>
          )}
        </div>
        <div style={{ display: "flex", gap: 10 }}>
          <button onClick={() => navigate("/monzaparts/leads")}
            style={{ display: "flex", alignItems: "center", gap: 6, background: "var(--monza-accent)", color: "white", border: "none", borderRadius: 8, padding: "10px 18px", fontSize: 13, fontWeight: 600, cursor: "pointer" }}>
            <Users size={15} /> Ver Leads
          </button>
          <button onClick={() => navigate("/monzaparts/cotizaciones")}
            style={{ display: "flex", alignItems: "center", gap: 6, background: "rgba(255,255,255,0.08)", color: "#E2E8F0", border: "1px solid rgba(255,255,255,0.15)", borderRadius: 8, padding: "10px 18px", fontSize: 13, fontWeight: 600, cursor: "pointer" }}>
            <FileText size={15} /> Cotizaciones
          </button>
        </div>
      </div>

      {/* ── Fila 1: Big cards con sparklines ── */}
      <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
        <BigCard
          label="COTIZADO ESTE MES"
          value={lk ? fmt(lk.total_cotizado_mes) : "$0"}
          sub={vk ? fmt(vk.total_mes) + " vendido" : undefined}
          color="#10B981"
          data={sparkCot}
          dark={dark}
          onClick={() => navigate("/monzaparts/cotizaciones")}
        />
        <BigCard
          label="VENTAS CERRADAS"
          value={vk ? fmt(vk.total_mes) : "$0"}
          sub={vk ? `${vk.vendidas_mes} cotizaciones` : undefined}
          color="#F59E0B"
          data={sparkVenta}
          dark={dark}
          onClick={() => navigate("/monzaparts/ventas")}
        />
        <BigCard
          label="LEADS NUEVOS (MES)"
          value={String(lk?.nuevos_mes ?? 0)}
          sub={lk && lk.tasa_cierre_pct != null ? `${lk.tasa_cierre_pct}% tasa de cierre` : undefined}
          color="#3B82F6"
          data={sparkLeads}
          dark={dark}
          onClick={() => navigate("/monzaparts/leads")}
        />
      </div>

      {/* ── Fila 2: Mid cards con progress ── */}
      <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
        <MidCard
          label="Leads del mes"
          value={lk?.nuevos_mes ?? 0}
          goal={GOAL_LEADS}
          color="#3B82F6"
          icon={Users}
          dark={dark}
          sub={`${GOAL_LEADS - (lk?.nuevos_mes ?? 0)} restantes para la meta`}
          onClick={() => navigate("/monzaparts/leads")}
        />
        <MidCard
          label="Ventas cerradas"
          value={vk?.vendidas_mes ?? 0}
          goal={GOAL_VENTAS}
          color="#10B981"
          icon={TrendingUp}
          dark={dark}
          sub={`${GOAL_VENTAS - (vk?.vendidas_mes ?? 0)} restantes para la meta`}
          onClick={() => navigate("/monzaparts/ventas")}
        />
        <MidCard
          label="Cotizaciones emitidas"
          value={lk?.vendidos_mes ?? 0}
          goal={GOAL_COT}
          color="#F59E0B"
          icon={FileText}
          dark={dark}
          sub="Total del mes en curso"
          onClick={() => navigate("/monzaparts/cotizaciones")}
        />
      </div>

      {/* ── Fila 3: Small stat cards ── */}
      <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
        <SmCard label="En proceso"       value={lk?.en_proceso ?? 0}        icon={Clock}       color="#F59E0B" dark={dark} />
        <SmCard label="Tasa de cierre"   value={lk?.tasa_cierre_pct != null ? `${lk.tasa_cierre_pct}%` : "—"} icon={Target}  color="var(--monza-accent)" dark={dark} />
        <SmCard label="Sin contactar +3d" value={lk?.sin_contactar_3d ?? 0} icon={AlertCircle} color="#EF4444" dark={dark} />
        <SmCard label="Pendientes entrega" value={vk?.pendientes_entrega ?? 0} icon={Car}       color="#6366F1" dark={dark} />
      </div>

      {/* ── Fila 4: Leads recientes ── */}
      <div style={{ background: C.bg, border: `1px solid ${C.bd}`, borderRadius: 14, overflow: "hidden", backdropFilter: "blur(4px)" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "16px 20px", borderBottom: `1px solid ${C.bd}` }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <Users size={16} className="monza-ic" />
            <span style={{ fontWeight: 700, fontSize: 15, color: C.text }}>Leads recientes</span>
          </div>
          <button
            onClick={() => navigate("/monzaparts/leads")}
            style={{ display: "flex", alignItems: "center", gap: 4, background: "transparent", border: "none", color: "var(--monza-accent)", cursor: "pointer", fontSize: 12, fontWeight: 600 }}
          >
            Ver todos <ChevronRight size={14} />
          </button>
        </div>

        {leads.length === 0 ? (
          <div style={{ padding: 40, textAlign: "center" }}>
            <Users size={32} color={C.bd} style={{ marginBottom: 8 }} />
            <div style={{ fontSize: 13, color: C.textSub }}>No hay leads todavía</div>
            <button onClick={() => navigate("/monzaparts/leads")}
              style={{ marginTop: 12, background: "var(--monza-accent)", color: "white", border: "none", borderRadius: 6, padding: "8px 16px", fontSize: 12, cursor: "pointer", fontWeight: 600 }}>
              Crear primer lead
            </button>
          </div>
        ) : (
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr style={{ background: C.tHead }}>
                {["Lead", "Cliente", "Vehículo", "Estado", "Estimado", "Fecha"].map((h) => (
                  <th key={h} style={{ padding: "9px 16px", textAlign: "left", fontSize: 10, fontWeight: 600, color: C.textSub, textTransform: "uppercase", letterSpacing: 0.5 }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {leads.map((lead) => {
                const es = ESTADO_STYLE[lead.estado] || ESTADO_STYLE.pendiente;
                return (
                  <tr key={lead.id}
                    onClick={() => navigate("/monzaparts/leads")}
                    style={{ borderBottom: `1px solid ${C.bd}`, cursor: "pointer" }}
                    onMouseEnter={(e) => (e.currentTarget.style.background = C.row)}
                    onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
                  >
                    <td style={{ padding: "11px 16px", fontWeight: 600, color: C.text }}>{lead.numero}</td>
                    <td style={{ padding: "11px 16px", color: C.textSub }}>{lead.cliente?.nombre || "—"}</td>
                    <td style={{ padding: "11px 16px", color: C.textSub }}>{lead.vehiculo || "—"}</td>
                    <td style={{ padding: "11px 16px" }}>
                      <span style={{ fontSize: 11, background: es.bg, color: es.color, padding: "3px 10px", borderRadius: 8, fontWeight: 600 }}>{es.label}</span>
                    </td>
                    <td style={{ padding: "11px 16px", fontWeight: 600, color: C.text }}>
                      {lead.total_estimado ? fmt(lead.total_estimado) : "—"}
                    </td>
                    <td style={{ padding: "11px 16px", color: C.textSub, fontSize: 12 }}>{fmtDate(lead.fecha_creacion)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

    </div>
  );
}
