"""Rediseña LeadDetail con bloques/cards separados y theme-aware."""

PATH = "/var/www/machparts.bigcode.cl/frontend-src/src/pages/MonzaLeadsPage.tsx"

with open(PATH) as f:
    c = f.read()

# 1. Agregar useMonzaTheme en LeadDetail
OLD_LD_START = """function LeadDetail({ lead, onRefresh }: { lead: Lead; onRefresh: () => void }) {
  const [detail, setDetail] = useState<Lead | null>(null);"""
NEW_LD_START = """function LeadDetail({ lead, onRefresh }: { lead: Lead; onRefresh: () => void }) {
  const { dark } = useMonzaTheme();
  const [detail, setDetail] = useState<Lead | null>(null);"""

if OLD_LD_START in c:
    c = c.replace(OLD_LD_START, NEW_LD_START)
    print("✓ useMonzaTheme agregado a LeadDetail")
else:
    print("! LeadDetail start not found")

# 2. Reemplazar el return completo de LeadDetail
# Identificamos el return por su inicio único
OLD_RETURN = """  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 0, borderTop: "1px solid #F1F5F9" }}>
      {/* Left panel */}
      <div style={{ padding: "20px 24px", borderRight: "1px solid #F1F5F9" }}>
        {/* Perfil del cliente */}
        {cli && (
          <section style={{ marginBottom: 20 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 10 }}>
              <span style={{ fontSize: 11, fontWeight: 700, color: "#8B1C1C", textTransform: "uppercase", letterSpacing: 0.5 }}>Perfil del cliente</span>
            </div>
            <div style={{ fontWeight: 700, fontSize: 14, color: "#1E293B" }}>{cli.nombre}</div>
            {cli.rut && <div style={{ fontSize: 12, color: "#64748B" }}>RUT {cli.rut}</div>}
            {cli.telefono && <div style={{ fontSize: 12, color: "#64748B", display: "flex", alignItems: "center", gap: 4 }}><Phone size={11} />{cli.telefono}</div>}
            {cli.email && <div style={{ fontSize: 12, color: "#64748B" }}>{cli.email}</div>}
            {cli.etiquetas && cli.etiquetas.length > 0 && (
              <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 8 }}>
                {cli.etiquetas.map((e, i) => (
                  <span key={i} style={{ fontSize: 10, padding: "2px 8px", borderRadius: 10, background: i === 0 ? "#FEF9C3" : "#F1F5F9", color: i === 0 ? "#854D0E" : "#475569", fontWeight: 600 }}>{e}</span>
                ))}
              </div>
            )}
            <div style={{ fontSize: 11, color: "#94A3B8", marginTop: 8 }}>
              Cliente desde {new Date(cli.leads_total !== undefined ? detail.fecha_creacion : detail.fecha_creacion).toLocaleDateString("es-CL")} · Leads: {cli.leads_total || 0} · LTV: {fmt(cli.ltv || 0)}
            </div>
          </section>
        )}

        {/* Próximos pasos */}
        <section style={{ marginBottom: 20 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
            <span style={{ fontSize: 11, fontWeight: 700, color: "#64748B", textTransform: "uppercase", letterSpacing: 0.5, display: "flex", alignItems: "center", gap: 4 }}>
              <Clock size={11} /> Próximos pasos
            </span>
            <button onClick={() => setShowAgendar(true)} style={{ fontSize: 11, color: "#8B1C1C", background: "transparent", border: "none", cursor: "pointer", fontWeight: 600 }}>+ Agendar</button>
          </div>
          {(detail.proximos_pasos || []).filter((p) => !p.completado).length === 0
            ? <p style={{ fontSize: 12, color: "#94A3B8", margin: 0 }}>No hay próximos pasos agendados.</p>
            : (detail.proximos_pasos || []).filter((p) => !p.completado).map((p) => (
              <div key={p.id} style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 0", fontSize: 12, borderBottom: "1px solid #F8FAFC" }}>
                <Calendar size={12} color="#8B1C1C" />
                <span style={{ fontWeight: 500, color: "#1E293B" }}>{p.tipo}</span>
                {p.cuando && <span style={{ color: "#64748B" }}>{new Date(p.cuando).toLocaleString("es-CL", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}</span>}
                <button onClick={async () => { await monzaLeadsAPI.completarPaso(lead.id, p.id); refresh(); }} style={{ marginLeft: "auto", fontSize: 10, background: "#DCFCE7", color: "#166534", border: "none", borderRadius: 4, padding: "2px 8px", cursor: "pointer" }}>✓ Completar</button>
              </div>
            ))
          }
        </section>

        {/* Nota interna */}
        <section>
          <div style={{ fontSize: 11, fontWeight: 700, color: "#64748B", textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 6 }}>Nota interna <span style={{ fontWeight: 400, textTransform: "none", color: "#94A3B8" }}>Visible solo para el equipo, no se envía al cliente.</span></div>
          <textarea value={nota} onChange={(e) => setNota(e.target.value)} placeholder="Escribe una nota interna..." rows={2}
            style={{ width: "100%", padding: "8px 10px", border: "1px solid #E2E8F0", borderRadius: 6, fontSize: 12, resize: "vertical", boxSizing: "border-box" }} />
          <button onClick={handleNota} disabled={!nota.trim() || savingNota}
            style={{ marginTop: 6, padding: "6px 14px", border: "none", borderRadius: 6, background: "#1E293B", color: "white", fontSize: 12, cursor: "pointer", opacity: !nota.trim() ? 0.5 : 1 }}>
            {savingNota ? "Guardando..." : "Guardar nota"}
          </button>
        </section>

        {/* Acciones */}
        <div style={{ marginTop: 16, display: "flex", gap: 8 }}>
          <button onClick={() => handleEstado("vendido")} style={{ fontSize: 11, padding: "5px 12px", border: "1px solid #DCFCE7", borderRadius: 6, background: "#F0FFF4", color: "#166534", cursor: "pointer" }}>✓ Marcar vendido</button>
          <button onClick={() => handleEstado("rechazado")} style={{ fontSize: 11, padding: "5px 12px", border: "1px solid #FEE2E2", borderRadius: 6, background: "#FFF5F5", color: "#991B1B", cursor: "pointer" }}>✗ Rechazar</button>
        </div>
      </div>

      {/* Right panel */}
      <div style={{ padding: "20px 24px" }}>
        {/* Items */}
        <div style={{ marginBottom: 20 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
            <span style={{ fontSize: 11, fontWeight: 700, color: "#64748B", textTransform: "uppercase", letterSpacing: 0.5 }}>
              Ítems <span style={{ background: "#F1F5F9", color: "#475569", borderRadius: 10, padding: "1px 7px", fontWeight: 600 }}>{(detail.items || []).length}</span>
            </span>
            <div style={{ display: "flex", gap: 8 }}>
              <button onClick={() => setShowCotizador(true)}
                style={{ fontSize: 11, display: "flex", alignItems: "center", gap: 4, padding: "5px 10px", border: "1px solid #8B1C1C", borderRadius: 6, background: "white", color: "#8B1C1C", cursor: "pointer", fontWeight: 600 }}>
                <Calculator size={11} /> Calcular todos
              </button>
              <button onClick={() => setShowAgregar(true)}
                style={{ fontSize: 11, display: "flex", alignItems: "center", gap: 4, padding: "5px 10px", border: "1px solid #E2E8F0", borderRadius: 6, background: "white", color: "#475569", cursor: "pointer" }}>
                <Plus size={11} /> Agregar repuesto
              </button>
            </div>
          </div>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
            <thead>
              <tr style={{ color: "#94A3B8", fontSize: 11, textTransform: "uppercase" }}>
                <th style={{ textAlign: "left", padding: "4px 6px", fontWeight: 600 }}>Repuesto</th>
                <th style={{ textAlign: "left", padding: "4px 6px", fontWeight: 600 }}>N° parte</th>
                <th style={{ textAlign: "left", padding: "4px 6px", fontWeight: 600 }}>Marca</th>
                <th style={{ textAlign: "center", padding: "4px 6px", fontWeight: 600 }}>QTY</th>
                <th style={{ textAlign: "left", padding: "4px 6px", fontWeight: 600 }}>Calidad</th>
                <th style={{ textAlign: "right", padding: "4px 6px", fontWeight: 600 }}>Precio</th>
                <th style={{ width: 40 }}></th>
              </tr>
            </thead>
            <tbody>
              {(detail.items || []).map((it) => (
                <tr key={it.id} style={{ borderBottom: "1px solid #F8FAFC" }}>
                  <td style={{ padding: "6px 6px", color: "#1E293B", fontWeight: 500 }}>{it.descripcion}</td>
                  <td style={{ padding: "6px 6px", color: "#64748B", fontSize: 11 }}>{it.numero_parte || "—"}</td>
                  <td style={{ padding: "6px 6px", color: "#64748B" }}>{it.marca || "—"}</td>
                  <td style={{ padding: "6px 6px", textAlign: "center" }}>{it.cantidad}</td>
                  <td style={{ padding: "6px 6px" }}>
                    <span style={{ fontSize: 10, padding: "2px 7px", borderRadius: 8, background: it.calidad === "sin_calificar" ? "#F1F5F9" : "#EFF6FF", color: it.calidad === "sin_calificar" ? "#94A3B8" : "#1D4ED8" }}>
                      {CALIDAD_LABEL[it.calidad] || it.calidad}
                    </span>
                  </td>
                  <td style={{ padding: "6px 6px", textAlign: "right", color: it.precio_clp ? "#1E293B" : "#94A3B8", fontWeight: it.precio_clp ? 600 : 400 }}>
                    {it.precio_clp ? fmt(it.precio_clp) : <span style={{ fontSize: 10, background: "#FEF3C7", color: "#D97706", padding: "2px 6px", borderRadius: 4 }}>Sin precio</span>}
                  </td>
                  <td style={{ padding: "6px 6px", textAlign: "center" }}>
                    <button onClick={async () => { await monzaLeadsAPI.deleteItem(lead.id, it.id); refresh(); }} style={{ background: "transparent", border: "none", cursor: "pointer", color: "#EF4444", opacity: 0.6 }}>
                      <Trash2 size={12} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {(detail.items || []).length > 0 && (
            <div style={{ textAlign: "right", marginTop: 6, fontSize: 12, color: "#64748B" }}>
              {(detail.items || []).filter((it) => it.precio_clp).length} ítem(s) seleccionado(s) · Total{" "}
              <strong style={{ color: "#1E293B" }}>{fmt(detail.total_estimado)}</strong>
            </div>
          )}
        </div>

        {/* Actividad */}
        <div>
          <span style={{ fontSize: 11, fontWeight: 700, color: "#64748B", textTransform: "uppercase", letterSpacing: 0.5, display: "flex", alignItems: "center", gap: 4, marginBottom: 8 }}>
            Actividad <span style={{ background: "#F1F5F9", color: "#475569", borderRadius: 10, padding: "1px 7px", fontWeight: 600 }}>{(detail.actividades || []).length}</span>
          </span>
          <div style={{ maxHeight: 180, overflowY: "auto" }}>
            {(detail.actividades || []).slice(0, 10).map((a) => (
              <div key={a.id} style={{ display: "flex", gap: 8, fontSize: 12, padding: "6px 0", borderBottom: "1px solid #F8FAFC" }}>
                <div style={{ width: 24, height: 24, borderRadius: "50%", background: "#F1F5F9", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
                  {a.tipo === "lead_creado" ? "🟢" : a.tipo === "nota" ? "📝" : a.tipo === "cotizacion" ? "📄" : "📞"}
                </div>
                <div style={{ flex: 1 }}>
                  <div style={{ color: "#1E293B" }}>{a.descripcion}</div>
                  <div style={{ color: "#94A3B8", fontSize: 11 }}>{timeSince(a.fecha)} · {a.usuario}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Modals */}
      {showAgregar && <AgregarItemModal leadId={lead.id} onClose={() => setShowAgregar(false)} onAdded={refresh} />}
      {showAgendar && <AgendarModal leadId={lead.id} onClose={() => setShowAgendar(false)} onDone={refresh} />}
      {showCotizador && (
        <MonzaCotizadorModal
          leadId={lead.id}
          leadNumero={detail.numero}
          clienteNombre={detail.cliente?.nombre || ""}
          vehiculo={detail.vehiculo}
          items={(detail.items || []) as any}
          onClose={() => setShowCotizador(false)}
          onApplied={refresh}
        />
      )}
    </div>
  );"""

NEW_RETURN = """  // ── Helpers de tema ──────────────────────────────────────────────────────
  const card  = { background: dark ? "#131b3e" : "white", border: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}`, borderRadius: 12, overflow: "hidden" as const };
  const cHead = { padding: "10px 14px", borderBottom: `1px solid ${dark ? "#1e2a4a" : "#F1F5F9"}`, background: dark ? "#0d1321" : "#F8FAFC", display: "flex", alignItems: "center", justifyContent: "space-between" };
  const cBody = { padding: "14px 16px" };
  const title = { fontSize: 11, fontWeight: 700 as const, color: dark ? "#8899cc" : "#64748B", textTransform: "uppercase" as const, letterSpacing: 0.5, display: "flex", alignItems: "center", gap: 4 };
  const txt   = dark ? "white"   : "#1E293B";
  const sub   = dark ? "#8899cc" : "#64748B";
  const rowBd = dark ? "#1e2a4a" : "#F8FAFC";

  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14, padding: 16, background: dark ? "#0a0e1f" : "#F0F4F8", borderTop: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}` }}>

      {/* ── Columna izquierda ── */}
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>

        {/* Perfil del cliente */}
        {cli && (
          <div style={card}>
            <div style={cHead}>
              <span style={title}>👤 Perfil del cliente</span>
            </div>
            <div style={cBody}>
              <div style={{ fontWeight: 700, fontSize: 15, color: txt, marginBottom: 2 }}>{cli.nombre}</div>
              {cli.rut && <div style={{ fontSize: 12, color: sub }}>RUT {cli.rut}</div>}
              <div style={{ display: "flex", flexDirection: "column", gap: 3, marginTop: 6 }}>
                {cli.telefono && <div style={{ fontSize: 12, color: sub, display: "flex", alignItems: "center", gap: 5 }}><Phone size={11} />{cli.telefono}</div>}
                {cli.email && <div style={{ fontSize: 12, color: sub }}>{cli.email}</div>}
              </div>
              {cli.etiquetas && cli.etiquetas.length > 0 && (
                <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 8 }}>
                  {cli.etiquetas.map((e, i) => (
                    <span key={i} style={{ fontSize: 10, padding: "2px 8px", borderRadius: 10, background: i === 0 ? "#FEF9C3" : (dark ? "#1e2a4a" : "#F1F5F9"), color: i === 0 ? "#854D0E" : (dark ? "#8899cc" : "#475569"), fontWeight: 600 }}>{e}</span>
                  ))}
                </div>
              )}
              <div style={{ display: "flex", gap: 12, marginTop: 10, paddingTop: 10, borderTop: `1px solid ${dark ? "#1e2a4a" : "#F1F5F9"}`, fontSize: 11 }}>
                <span style={{ color: sub }}>Leads: <strong style={{ color: txt }}>{cli.leads_total || 0}</strong></span>
                <span style={{ color: sub }}>Vendidos: <strong style={{ color: txt }}>{cli.vendidos_total || 0}</strong></span>
                <span style={{ color: sub }}>LTV: <strong style={{ color: txt }}>{fmt(cli.ltv || 0)}</strong></span>
              </div>
            </div>
          </div>
        )}

        {/* Próximos pasos */}
        <div style={card}>
          <div style={cHead}>
            <span style={title}>
              <Clock size={11} /> Próximos pasos
              {(detail.proximos_pasos || []).filter((p) => !p.completado).length > 0 && (
                <span style={{ background: "#FEF3C7", color: "#D97706", borderRadius: 10, padding: "1px 6px" }}>
                  {(detail.proximos_pasos || []).filter((p) => !p.completado).length}
                </span>
              )}
            </span>
            <button onClick={() => setShowAgendar(true)} style={{ fontSize: 11, color: "#8B1C1C", background: "transparent", border: "1px solid #8B1C1C", borderRadius: 6, cursor: "pointer", fontWeight: 600, padding: "3px 10px" }}>+ Agendar</button>
          </div>
          <div style={cBody}>
            {(detail.proximos_pasos || []).filter((p) => !p.completado).length === 0
              ? <p style={{ fontSize: 12, color: dark ? "#475569" : "#94A3B8", margin: 0 }}>No hay próximos pasos agendados.</p>
              : (detail.proximos_pasos || []).filter((p) => !p.completado).map((p) => (
                <div key={p.id} style={{ display: "flex", alignItems: "center", gap: 8, padding: "7px 0", fontSize: 12, borderBottom: `1px solid ${rowBd}` }}>
                  <Calendar size={12} color="#8B1C1C" />
                  <span style={{ fontWeight: 600, color: txt }}>{p.tipo}</span>
                  {p.cuando && <span style={{ color: sub, fontSize: 11 }}>{new Date(p.cuando).toLocaleString("es-CL", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}</span>}
                  <button onClick={async () => { await monzaLeadsAPI.completarPaso(lead.id, p.id); refresh(); }} style={{ marginLeft: "auto", fontSize: 10, background: "#DCFCE7", color: "#166534", border: "none", borderRadius: 6, padding: "3px 10px", cursor: "pointer", fontWeight: 600 }}>✓ Completar</button>
                </div>
              ))
            }
          </div>
        </div>

        {/* Nota interna + Acciones estado */}
        <div style={card}>
          <div style={cHead}>
            <span style={title}>📝 Nota interna</span>
            <span style={{ fontSize: 10, color: dark ? "#475569" : "#94A3B8" }}>Solo visible para el equipo</span>
          </div>
          <div style={cBody}>
            <textarea value={nota} onChange={(e) => setNota(e.target.value)} placeholder="Escribe una nota interna..." rows={2}
              style={{ width: "100%", padding: "8px 10px", border: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}`, borderRadius: 6, fontSize: 12, resize: "vertical", boxSizing: "border-box", background: dark ? "#0d1321" : "white", color: txt }} />
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 10 }}>
              <button onClick={handleNota} disabled={!nota.trim() || savingNota}
                style={{ padding: "6px 14px", border: "none", borderRadius: 6, background: "#1E293B", color: "white", fontSize: 12, cursor: "pointer", opacity: !nota.trim() ? 0.5 : 1 }}>
                {savingNota ? "Guardando..." : "Guardar nota"}
              </button>
              <div style={{ display: "flex", gap: 6 }}>
                <button onClick={() => handleEstado("vendido")} style={{ fontSize: 11, padding: "5px 12px", border: "1px solid #DCFCE7", borderRadius: 6, background: "#F0FFF4", color: "#166534", cursor: "pointer", fontWeight: 600 }}>✓ Vendido</button>
                <button onClick={() => handleEstado("rechazado")} style={{ fontSize: 11, padding: "5px 12px", border: "1px solid #FEE2E2", borderRadius: 6, background: "#FFF5F5", color: "#991B1B", cursor: "pointer", fontWeight: 600 }}>✗ Rechazar</button>
              </div>
            </div>
          </div>
        </div>

      </div>

      {/* ── Columna derecha ── */}
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>

        {/* Ítems */}
        <div style={card}>
          <div style={cHead}>
            <span style={title}>
              📦 Ítems
              <span style={{ background: dark ? "#1e2a4a" : "#F1F5F9", color: dark ? "#e2e8f0" : "#475569", borderRadius: 10, padding: "1px 7px" }}>{(detail.items || []).length}</span>
            </span>
            <div style={{ display: "flex", gap: 6 }}>
              <button onClick={() => setShowCotizador(true)}
                style={{ fontSize: 11, display: "flex", alignItems: "center", gap: 4, padding: "5px 10px", border: "1px solid #8B1C1C", borderRadius: 6, background: "transparent", color: "#8B1C1C", cursor: "pointer", fontWeight: 600 }}>
                <Calculator size={11} /> Calcular todos
              </button>
              <button onClick={() => setShowAgregar(true)}
                style={{ fontSize: 11, display: "flex", alignItems: "center", gap: 4, padding: "5px 10px", border: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}`, borderRadius: 6, background: "transparent", color: sub, cursor: "pointer" }}>
                <Plus size={11} /> Agregar repuesto
              </button>
            </div>
          </div>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
            <thead>
              <tr style={{ background: dark ? "#0d1321" : "#F8FAFC" }}>
                {["Repuesto","N° parte","Marca","QTY","Calidad","Precio",""].map((h, i) => (
                  <th key={i} style={{ textAlign: i === 3 ? "center" : i === 5 ? "right" : "left", padding: "7px 10px", fontWeight: 600, fontSize: 10, color: sub, textTransform: "uppercase", letterSpacing: 0.4 }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {(detail.items || []).length === 0 ? (
                <tr><td colSpan={7} style={{ padding: "20px 10px", textAlign: "center", color: dark ? "#475569" : "#94A3B8", fontSize: 12 }}>Sin repuestos — agrega el primero</td></tr>
              ) : (detail.items || []).map((it) => (
                <tr key={it.id} style={{ borderBottom: `1px solid ${rowBd}` }}>
                  <td style={{ padding: "7px 10px", color: txt, fontWeight: 500 }}>{it.descripcion}</td>
                  <td style={{ padding: "7px 10px", color: sub, fontSize: 11 }}>{it.numero_parte || "—"}</td>
                  <td style={{ padding: "7px 10px", color: sub }}>{it.marca || "—"}</td>
                  <td style={{ padding: "7px 10px", textAlign: "center", color: txt }}>{it.cantidad}</td>
                  <td style={{ padding: "7px 10px" }}>
                    <span style={{ fontSize: 10, padding: "2px 7px", borderRadius: 8, background: it.calidad === "sin_calificar" ? (dark ? "#1e2a4a" : "#F1F5F9") : "#EFF6FF", color: it.calidad === "sin_calificar" ? (dark ? "#8899cc" : "#94A3B8") : "#1D4ED8" }}>
                      {CALIDAD_LABEL[it.calidad] || it.calidad}
                    </span>
                  </td>
                  <td style={{ padding: "7px 10px", textAlign: "right", color: it.precio_clp ? txt : "#94A3B8", fontWeight: it.precio_clp ? 600 : 400 }}>
                    {it.precio_clp ? fmt(it.precio_clp) : <span style={{ fontSize: 10, background: "#FEF3C7", color: "#D97706", padding: "2px 6px", borderRadius: 4 }}>Sin precio</span>}
                  </td>
                  <td style={{ padding: "7px 10px", textAlign: "center" }}>
                    <button onClick={async () => { await monzaLeadsAPI.deleteItem(lead.id, it.id); refresh(); }} style={{ background: "transparent", border: "none", cursor: "pointer", color: "#EF4444", opacity: 0.6 }}>
                      <Trash2 size={12} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {(detail.items || []).length > 0 && (
            <div style={{ padding: "8px 10px", borderTop: `1px solid ${rowBd}`, textAlign: "right", fontSize: 12, color: sub }}>
              {(detail.items || []).filter((it) => it.precio_clp).length} ítem(s) con precio · Total{" "}
              <strong style={{ color: txt }}>{fmt(detail.total_estimado)}</strong>
            </div>
          )}
        </div>

        {/* Actividad */}
        <div style={card}>
          <div style={cHead}>
            <span style={title}>
              📋 Actividad
              <span style={{ background: dark ? "#1e2a4a" : "#F1F5F9", color: dark ? "#e2e8f0" : "#475569", borderRadius: 10, padding: "1px 7px" }}>{(detail.actividades || []).length}</span>
            </span>
          </div>
          <div style={{ padding: "10px 14px", maxHeight: 220, overflowY: "auto" }}>
            {(detail.actividades || []).length === 0
              ? <p style={{ fontSize: 12, color: dark ? "#475569" : "#94A3B8", margin: 0 }}>Sin actividad registrada.</p>
              : (detail.actividades || []).slice(0, 12).map((a) => (
                <div key={a.id} style={{ display: "flex", gap: 10, fontSize: 12, padding: "7px 0", borderBottom: `1px solid ${rowBd}` }}>
                  <div style={{ width: 26, height: 26, borderRadius: "50%", background: dark ? "#1e2a4a" : "#F1F5F9", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0, fontSize: 13 }}>
                    {a.tipo === "lead_creado" ? "🟢" : a.tipo === "nota" ? "📝" : a.tipo === "cotizacion" ? "📄" : "📞"}
                  </div>
                  <div style={{ flex: 1 }}>
                    <div style={{ color: txt, lineHeight: 1.4 }}>{a.descripcion}</div>
                    <div style={{ color: dark ? "#475569" : "#94A3B8", fontSize: 11, marginTop: 2 }}>{timeSince(a.fecha)} · {a.usuario}</div>
                  </div>
                </div>
              ))
            }
          </div>
        </div>

      </div>

      {/* Modals */}
      {showAgregar && <AgregarItemModal leadId={lead.id} onClose={() => setShowAgregar(false)} onAdded={refresh} />}
      {showAgendar && <AgendarModal leadId={lead.id} onClose={() => setShowAgendar(false)} onDone={refresh} />}
      {showCotizador && (
        <MonzaCotizadorModal
          leadId={lead.id}
          leadNumero={detail.numero}
          clienteNombre={detail.cliente?.nombre || ""}
          vehiculo={detail.vehiculo}
          items={(detail.items || []) as any}
          onClose={() => setShowCotizador(false)}
          onApplied={refresh}
        />
      )}
    </div>
  );"""

if OLD_RETURN in c:
    c = c.replace(OLD_RETURN, NEW_RETURN)
    print("✓ Return de LeadDetail reemplazado con diseño de cards")
else:
    print("! Return no encontrado — verificar")

with open(PATH, "w") as f:
    f.write(c)
print("✅ Guardado")
