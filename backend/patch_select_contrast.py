"""Corrige contraste de selects en MonzaLeadsPage: agrega background/color explícitos."""

PATH = "/var/www/machparts.bigcode.cl/frontend-src/src/pages/MonzaLeadsPage.tsx"

with open(PATH) as f:
    c = f.read()

# 1. inputSt — agregar background y color
OLD_INPUT = "  const inputSt = { width: \"100%\", padding: \"8px 10px\", border: \"1px solid #E2E8F0\", borderRadius: 6, fontSize: 13, boxSizing: \"border-box\" as const };"
NEW_INPUT = "  const inputSt = { width: \"100%\", padding: \"8px 10px\", border: \"1px solid #D1D5DB\", borderRadius: 6, fontSize: 13, boxSizing: \"border-box\" as const, background: \"white\", color: \"#1E293B\" };"

if OLD_INPUT in c:
    c = c.replace(OLD_INPUT, NEW_INPUT)
    print("✓ inputSt actualizado con background y color")
else:
    print("! inputSt no encontrado con texto exacto")

# 2. Select de calidad (en AgregarItemModal) — también sin background
OLD_CAL = 'style={{ ...inputSt }}'
NEW_CAL = 'style={{ ...inputSt, background: "white", color: "#1E293B" }}'
# Solo reemplazar la primera ocurrencia del select calidad (tiene value=form.calidad)
if 'value={form.calidad}' in c:
    c = c.replace(
        'value={form.calidad} onChange={(e) => set("calidad", e.target.value)} style={{ ...inputSt }}',
        'value={form.calidad} onChange={(e) => set("calidad", e.target.value)} style={{ ...inputSt, background: "white", color: "#1E293B" }}'
    )
    print("✓ Select calidad corregido")

# 3. Select de AgendarModal (tipo Llamada/WhatsApp...)
OLD_AGEN = 'style={{ width: "100%", padding: "8px 10px", border: "1px solid #E2E8F0", borderRadius: 6, fontSize: 13, background: "white" }}'
NEW_AGEN = 'style={{ width: "100%", padding: "8px 10px", border: "1px solid #D1D5DB", borderRadius: 6, fontSize: 13, background: "white", color: "#1E293B" }}'
if OLD_AGEN in c:
    c = c.replace(OLD_AGEN, NEW_AGEN)
    print("✓ Select AgendarModal corregido")

# 4. Select filtro de estado en la tabla principal
OLD_EST = '''          style={{
            padding: "8px 12px", border: "1px solid #E2E8F0", borderRadius: 6, fontSize: 13,
            background: "white", color: "#374151", cursor: "pointer",
          }}'''
if OLD_EST in c:
    print("· Select estado ya tiene estilos correctos")
else:
    # Buscar el select de estado en la tabla y agregar color si falta
    c = c.replace(
        'value={estado} onChange={(e) => setEstado(e.target.value)}\n          style={{\n            padding: "8px 12px", border: "1px solid #E2E8F0", borderRadius: 6, fontSize: 13,',
        'value={estado} onChange={(e) => setEstado(e.target.value)}\n          style={{\n            padding: "8px 12px", border: "1px solid #D1D5DB", borderRadius: 6, fontSize: 13,\n            background: "white", color: "#1E293B",',
    )

# 5. Agregar color a todos los <option> en el archivo para consistencia
# Esto ayuda en Firefox/Chrome donde sí se puede estilar
c = c.replace(
    '<option key={o}>{o}</option>',
    '<option key={o} style={{ background: "white", color: "#1E293B" }}>{o}</option>'
)
c = c.replace(
    '<option value="sin_calificar">Sin calificar</option>',
    '<option value="sin_calificar" style={{ background: "white", color: "#1E293B" }}>Sin calificar</option>'
)
c = c.replace(
    '<option value="genuine">Genuine</option>',
    '<option value="genuine" style={{ background: "white", color: "#1E293B" }}>Genuine</option>'
)
c = c.replace(
    '<option value="oem">OEM</option>',
    '<option value="oem" style={{ background: "white", color: "#1E293B" }}>OEM</option>'
)
c = c.replace(
    '<option value="aftermarket">Aftermarket</option>',
    '<option value="aftermarket" style={{ background: "white", color: "#1E293B" }}>Aftermarket</option>'
)
# Options del select de estado (tabla)
for opt in [
    ('value="todos"', 'Todos los estados'),
    ('value="pendiente"', 'Pendiente'),
    ('value="en_proceso"', 'En proceso'),
    ('value="vendido"', 'Vendido'),
    ('value="rechazado"', 'Rechazado'),
]:
    old_opt = f'<option {opt[0]}>{opt[1]}</option>'
    new_opt = f'<option {opt[0]} style={{{{ background: "white", color: "#1E293B" }}}}>{opt[1]}</option>'
    c = c.replace(old_opt, new_opt)

print("✓ Options con estilos de contraste")

with open(PATH, "w") as f:
    f.write(c)

print("\n✅ MonzaLeadsPage.tsx guardado")
