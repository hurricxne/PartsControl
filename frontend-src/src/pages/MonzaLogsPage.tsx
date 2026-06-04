import { useState, useEffect, useCallback } from "react";
import { ScrollText, RefreshCw, Search, Filter } from "lucide-react";
import { monzaLogsAPI } from "../services/monzaApi";
import { useMonzaTheme } from "./MonzaLayout";
import toast from "react-hot-toast";

interface LogEntry {
  id: number;
  user_email: string | null;
  accion: string;
  entidad: string;
  entidad_id: number | null;
  entidad_ref: string | null;
  detalle: string | null;
  ip: string | null;
  fecha: string;
}

interface Summary {
  total: number;
  hoy: number;
  by_accion: Record<string, number>;
  by_entidad: Record<string, number>;
}

// ── Acción badge ──────────────────────────────────────────────────────────────
const ACCION_STYLE: Record<string, { bg: string; color: string; label: string }> = {
  CREATE:    { bg: "#DCFCE7", color: "#15803D", label: "CREAR" },
  UPDATE:    { bg: "#DBEAFE", color: "#1D4ED8", label: "EDITAR" },
  DELETE:    { bg: "#FEE2E2", color: "#B91C1C", label: "ELIMINAR" },
  UPLOAD:    { bg: "#FEF3C7", color: "#B45309", label: "SUBIR DOC" },
  VENDIDA:   { bg: "#F0FDF4", color: "#15803D", label: "VENDIDA" },
  DESPACHADO:{ bg: "#EDE9FE", color: "#6D28D9", label: "DESPACHO" },
  LOGIN:     { bg: "#F1F5F9", color: "#475569", label: "LOGIN" },
};

const ENTIDAD_LABEL: Record<string, string> = {
  lead: "Lead", cotizacion: "Cotización", cliente: "Cliente",
  item: "Ítem", config: "Config",
};

function AccionBadge({ accion }: { accion: string }) {
  const s = ACCION_STYLE[accion] || { bg: "#F1F5F9", color: "#475569", label: accion };
  return (
    <span style={{ fontSize: 10, fontWeight: 700, background: s.bg, color: s.color, padding: "3px 8px", borderRadius: 20, letterSpacing: 0.3 }}>
      {s.label}
    </span>
  );
}

function EntidadBadge({ entidad }: { entidad: string }) {
  return (
    <span style={{ fontSize: 11, color: "#64748B", background: "#F1F5F9", padding: "2px 7px", borderRadius: 5 }}>
      {ENTIDAD_LABEL[entidad] || entidad}
    </span>
  );
}

function fmtDate(d: string) {
  const dt = new Date(d);
  return dt.toLocaleDateString("es-CL", { day: "2-digit", month: "2-digit", year: "2-digit" })
    + " " + dt.toLocaleTimeString("es-CL", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function SummaryChip({ label, value, accent }: { label: string; value: number; accent: string }) {
  const { dark } = useMonzaTheme();
  return (
    <div style={{ background: dark ? "#131b3e" : "white", border: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}`, borderRadius: 10, padding: "12px 18px", minWidth: 110, textAlign: "center" }}>
      <div style={{ width: 20, height: 3, background: accent, borderRadius: 2, margin: "0 auto 8px" }} />
      <div style={{ fontSize: 22, fontWeight: 800, color: dark ? "white" : "#1E293B" }}>{value}</div>
      <div style={{ fontSize: 11, color: dark ? "#8899cc" : "#64748B", marginTop: 3 }}>{label}</div>
    </div>
  );
}

export default function MonzaLogsPage() {
  const { dark } = useMonzaTheme();
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [loading, setLoading] = useState(true);

  // Filters
  const [q, setQ] = useState("");
  const [accion, setAccion] = useState("");
  const [entidad, setEntidad] = useState("");
  const [desde, setDesde] = useState("");
  const [hasta, setHasta] = useState("");
  const [page, setPage] = useState(1);
  const PAGE_SIZE = 50;

  const bg  = dark ? "#131b3e" : "white";
  const bd  = dark ? "#1e2a4a" : "#E2E8F0";
  const txt = dark ? "white"   : "#1E293B";
  const sub = dark ? "#8899cc" : "#64748B";

  const fetchAll = useCallback(async () => {
    setLoading(true);
    try {
      const [logsRes, sumRes] = await Promise.all([
        monzaLogsAPI.list({
          accion: accion || undefined,
          entidad: entidad || undefined,
          user_email: q || undefined,
          desde: desde || undefined,
          hasta: hasta || undefined,
          page,
          page_size: PAGE_SIZE,
        }),
        monzaLogsAPI.summary(),
      ]);
      setLogs(logsRes.data.items);
      setTotal(logsRes.data.total);
      setSummary(sumRes.data);
    } catch { toast.error("Error al cargar logs"); }
    finally { setLoading(false); }
  }, [accion, entidad, q, desde, hasta, page]);

  useEffect(() => { fetchAll(); }, [fetchAll]);

  const totalPages = Math.ceil(total / PAGE_SIZE);

  return (
    <div>
      {/* Header */}
      <div style={{ marginBottom: 20 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
          <ScrollText size={22} className="monza-ic" />
          <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: txt }}>Logs de Operaciones</h1>
        </div>
        <p style={{ margin: 0, fontSize: 13, color: sub }}>
          Registro de todas las operaciones CRUD del sistema — creaciones, ediciones, eliminaciones y subidas de documentos.
        </p>
      </div>

      {/* Summary chips */}
      {summary && (
        <div style={{ display: "flex", gap: 10, marginBottom: 20, flexWrap: "wrap" }}>
          <SummaryChip label="Total registros" value={summary.total} accent="var(--monza-accent)" />
          <SummaryChip label="Hoy" value={summary.hoy} accent="#10B981" />
          {Object.entries(summary.by_accion).sort((a, b) => b[1] - a[1]).map(([ac, cnt]) => {
            const s = ACCION_STYLE[ac] || { bg: "#F1F5F9", color: "#64748B" };
            return <SummaryChip key={ac} label={(ACCION_STYLE[ac]?.label || ac)} value={cnt} accent={s.color} />;
          })}
        </div>
      )}

      {/* Filters */}
      <div style={{ background: bg, border: `1px solid ${bd}`, borderRadius: 10, padding: "12px 16px", marginBottom: 14 }}>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <Filter size={13} color={sub} style={{ flexShrink: 0 }} />

          {/* Usuario search */}
          <div style={{ position: "relative", minWidth: 200 }}>
            <Search size={12} style={{ position: "absolute", left: 8, top: "50%", transform: "translateY(-50%)", color: "#94A3B8" }} />
            <input value={q} onChange={(e) => { setQ(e.target.value); setPage(1); }}
              placeholder="Filtrar por usuario..."
              style={{ paddingLeft: 26, padding: "7px 10px 7px 26px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 12, background: dark ? "#0d1321" : "white", color: txt, width: "100%", boxSizing: "border-box" as const }} />
          </div>

          {/* Acción */}
          <select value={accion} onChange={(e) => { setAccion(e.target.value); setPage(1); }}
            style={{ padding: "7px 10px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 12, background: dark ? "#0d1321" : "white", color: txt }}>
            <option value="">Todas las acciones</option>
            <option value="CREATE">Crear</option>
            <option value="UPDATE">Editar</option>
            <option value="DELETE">Eliminar</option>
            <option value="UPLOAD">Subir doc.</option>
            <option value="VENDIDA">Vendida</option>
            <option value="DESPACHADO">Despacho</option>
          </select>

          {/* Entidad */}
          <select value={entidad} onChange={(e) => { setEntidad(e.target.value); setPage(1); }}
            style={{ padding: "7px 10px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 12, background: dark ? "#0d1321" : "white", color: txt }}>
            <option value="">Todas las entidades</option>
            <option value="lead">Lead</option>
            <option value="cotizacion">Cotización</option>
            <option value="cliente">Cliente</option>
          </select>

          {/* Fechas */}
          <div style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 12, color: sub }}>
            Desde:
            <input type="date" value={desde} onChange={(e) => { setDesde(e.target.value); setPage(1); }}
              style={{ padding: "6px 8px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 12, background: dark ? "#0d1321" : "white", color: txt, colorScheme: dark ? "dark" : "light" as const }} />
            Hasta:
            <input type="date" value={hasta} onChange={(e) => { setHasta(e.target.value); setPage(1); }}
              style={{ padding: "6px 8px", border: `1px solid ${bd}`, borderRadius: 6, fontSize: 12, background: dark ? "#0d1321" : "white", color: txt, colorScheme: dark ? "dark" : "light" as const }} />
          </div>

          <button onClick={fetchAll}
            style={{ marginLeft: "auto", padding: "7px 10px", border: `1px solid ${bd}`, borderRadius: 6, background: bg, cursor: "pointer", color: sub, display: "flex", alignItems: "center", gap: 5 }}>
            <RefreshCw size={13} />
          </button>
        </div>
      </div>

      {/* Table */}
      <div style={{ background: bg, border: `1px solid ${bd}`, borderRadius: 10, overflow: "hidden" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
          <thead>
            <tr style={{ background: dark ? "#0d1321" : "#F8FAFC", borderBottom: `1px solid ${bd}` }}>
              {["Fecha / Hora", "Usuario", "Acción", "Entidad", "Referencia", "Detalle", "IP"].map((h) => (
                <th key={h} style={{ padding: "10px 14px", textAlign: "left", fontWeight: 600, fontSize: 11, color: sub, textTransform: "uppercase" as const, letterSpacing: 0.5, whiteSpace: "nowrap" }}>
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={7} style={{ padding: 40, textAlign: "center", color: "#94A3B8" }}>Cargando...</td></tr>
            ) : logs.length === 0 ? (
              <tr><td colSpan={7} style={{ padding: 48, textAlign: "center", color: "#94A3B8" }}>
                <ScrollText size={32} color="#E2E8F0" style={{ display: "block", margin: "0 auto 8px" }} />
                No hay registros con los filtros actuales.
              </td></tr>
            ) : logs.map((lg, i) => (
              <tr key={lg.id} style={{
                borderBottom: `1px solid ${dark ? "#1e2a4a" : "#F1F5F9"}`,
                background: i % 2 === 0 ? bg : (dark ? "#0f1629" : "#FAFAFA"),
              }}>
                <td style={{ padding: "9px 14px", whiteSpace: "nowrap" }}>
                  <div style={{ fontSize: 12, fontFamily: "monospace", color: txt }}>{fmtDate(lg.fecha)}</div>
                </td>
                <td style={{ padding: "9px 14px" }}>
                  <div style={{ fontSize: 12, color: txt, fontWeight: 500 }}>
                    {lg.user_email ? lg.user_email.split("@")[0] : "—"}
                  </div>
                  {lg.user_email && (
                    <div style={{ fontSize: 10, color: sub }}>{lg.user_email}</div>
                  )}
                </td>
                <td style={{ padding: "9px 14px", whiteSpace: "nowrap" }}>
                  <AccionBadge accion={lg.accion} />
                </td>
                <td style={{ padding: "9px 14px" }}>
                  <EntidadBadge entidad={lg.entidad} />
                  {lg.entidad_id && (
                    <div style={{ fontSize: 10, color: sub, marginTop: 2 }}>#{lg.entidad_id}</div>
                  )}
                </td>
                <td style={{ padding: "9px 14px" }}>
                  <span style={{ fontSize: 12, fontWeight: 600, color: "var(--monza-accent)" }}>
                    {lg.entidad_ref || "—"}
                  </span>
                </td>
                <td style={{ padding: "9px 14px", maxWidth: 320 }}>
                  <span style={{ fontSize: 12, color: sub, display: "block", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {lg.detalle || "—"}
                  </span>
                </td>
                <td style={{ padding: "9px 14px" }}>
                  <span style={{ fontSize: 11, color: sub, fontFamily: "monospace" }}>{lg.ip || "—"}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        {/* Pagination */}
        <div style={{ padding: "10px 16px", borderTop: `1px solid ${bd}`, display: "flex", alignItems: "center", justifyContent: "space-between", fontSize: 12, color: sub }}>
          <span>
            {total > 0 ? `${(page - 1) * PAGE_SIZE + 1}–${Math.min(page * PAGE_SIZE, total)} de ${total} registros` : "0 registros"}
          </span>
          {totalPages > 1 && (
            <div style={{ display: "flex", gap: 4 }}>
              <button onClick={() => setPage((p) => Math.max(1, p - 1))} disabled={page === 1}
                style={{ padding: "4px 12px", border: `1px solid ${bd}`, borderRadius: 6, background: bg, cursor: page === 1 ? "not-allowed" : "pointer", color: page === 1 ? "#CBD5E1" : txt, fontSize: 12 }}>
                ← Ant.
              </button>
              {Array.from({ length: Math.min(5, totalPages) }, (_, i) => {
                const p = Math.max(1, Math.min(page - 2 + i, totalPages - 4 + i));
                return (
                  <button key={p} onClick={() => setPage(p)}
                    style={{ padding: "4px 10px", border: `1px solid ${p === page ? "var(--monza-accent)" : bd}`, borderRadius: 6, background: p === page ? "var(--monza-accent)" : bg, cursor: "pointer", color: p === page ? "white" : txt, fontSize: 12, fontWeight: p === page ? 700 : 400 }}>
                    {p}
                  </button>
                );
              })}
              <button onClick={() => setPage((p) => Math.min(totalPages, p + 1))} disabled={page === totalPages}
                style={{ padding: "4px 12px", border: `1px solid ${bd}`, borderRadius: 6, background: bg, cursor: page === totalPages ? "not-allowed" : "pointer", color: page === totalPages ? "#CBD5E1" : txt, fontSize: 12 }}>
                Sig. →
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
