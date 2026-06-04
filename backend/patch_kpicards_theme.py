"""
Actualiza KpiCard en MonzaLeadsPage y MonzaVentasPage con soporte dark/light.
Corrige colores de BigCard/MidCard/SmCard en MonzaDashboardPage para modo claro.
"""
import re

PAGES_DIR = "/var/www/machparts.bigcode.cl/frontend-src/src/pages"

# ────────────────────────────────────────────────────────────────────────────
# 1. MonzaLeadsPage — KpiCard + import
# ────────────────────────────────────────────────────────────────────────────
path = f"{PAGES_DIR}/MonzaLeadsPage.tsx"
with open(path) as f:
    c = f.read()

# Agregar import useMonzaTheme si no está
if "useMonzaTheme" not in c:
    c = c.replace(
        'import MonzaCotizadorModal from "./MonzaCotizadorModal";',
        'import MonzaCotizadorModal from "./MonzaCotizadorModal";\nimport { useMonzaTheme } from "./MonzaLayout";'
    )
    print("✓ Leads: import useMonzaTheme agregado")

# Reemplazar KpiCard
OLD_KPICARD_LEADS = '''function KpiCard({ label, value, sub }: { label: string; value: string | number; sub?: string }) {
  return (
    <div style={{ background: "white", borderRadius: 10, border: "1px solid #E2E8F0", padding: "16px 20px", minWidth: 140 }}>
      <div style={{ fontSize: 26, fontWeight: 700, color: "#1E293B" }}>{value}</div>
      <div style={{ fontSize: 12, color: "#64748B", marginTop: 2 }}>{label}</div>
      {sub && <div style={{ fontSize: 11, color: "#94A3B8", marginTop: 2 }}>{sub}</div>}
    </div>
  );
}'''

NEW_KPICARD_LEADS = '''function KpiCard({ label, value, sub, accent = "#8B1C1C" }: { label: string; value: string | number; sub?: string; accent?: string }) {
  const { dark } = useMonzaTheme();
  return (
    <div style={{
      background: dark ? "#131b3e" : "white",
      borderRadius: 12,
      border: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}`,
      padding: "18px 20px",
      minWidth: 140,
      flex: 1,
    }}>
      <div style={{ width: 28, height: 4, borderRadius: 2, background: accent, marginBottom: 12 }} />
      <div style={{ fontSize: 28, fontWeight: 800, color: dark ? "white" : "#1E293B", lineHeight: 1 }}>{value}</div>
      <div style={{ fontSize: 12, color: dark ? "#8899cc" : "#64748B", marginTop: 5 }}>{label}</div>
      {sub && <div style={{ fontSize: 11, color: dark ? "#475569" : "#94A3B8", marginTop: 3 }}>{sub}</div>}
    </div>
  );
}'''

if OLD_KPICARD_LEADS in c:
    c = c.replace(OLD_KPICARD_LEADS, NEW_KPICARD_LEADS)
    print("✓ Leads: KpiCard actualizado con tema")
else:
    print("! Leads: KpiCard no encontrado con texto exacto — verificar manualmente")

# Agregar colores de acento a las llamadas KpiCard
ACCENTS = [
    ('label="Leads nuevos del mes"', 'accent="#3B82F6"'),
    ('label="En proceso"',           'accent="#F59E0B"'),
    ('label="Vendidos"',             'accent="#10B981"'),
    ('label="Tasa de cierre"',       'accent="#6366F1"'),
    ('label="Sin contactar +3d"',    'accent="#EF4444"'),
]
for label_str, accent_str in ACCENTS:
    pattern = f'(<KpiCard {label_str}[^/]*/?>)'
    # Approach: add accent= after the label attr if not already there
    if label_str in c and accent_str not in c:
        c = c.replace(label_str, f'{label_str} {accent_str}')

with open(path, "w") as f:
    f.write(c)
print("✓ MonzaLeadsPage.tsx guardado\n")

# ────────────────────────────────────────────────────────────────────────────
# 2. MonzaVentasPage — KpiCard + import
# ────────────────────────────────────────────────────────────────────────────
path = f"{PAGES_DIR}/MonzaVentasPage.tsx"
with open(path) as f:
    c = f.read()

if "useMonzaTheme" not in c:
    c = c.replace(
        'import { Search, TrendingUp, ExternalLink } from "lucide-react";',
        'import { Search, TrendingUp, ExternalLink } from "lucide-react";\nimport { useMonzaTheme } from "./MonzaLayout";'
    )
    print("✓ Ventas: import useMonzaTheme agregado")

OLD_KPICARD_VENTAS = '''function KpiCard({ label, value, sub }: { label: string; value: string | number; sub?: string }) {
  return (
    <div style={{ background: "white", borderRadius: 10, border: "1px solid #E2E8F0", padding: "16px 20px" }}>
      <div style={{ fontSize: 24, fontWeight: 700, color: "#1E293B" }}>{value}</div>
      <div style={{ fontSize: 12, color: "#64748B", marginTop: 2 }}>{label}</div>
      {sub && <div style={{ fontSize: 11, color: "#94A3B8", marginTop: 2 }}>{sub}</div>}
    </div>
  );
}'''

NEW_KPICARD_VENTAS = '''function KpiCard({ label, value, sub, accent = "#8B1C1C" }: { label: string; value: string | number; sub?: string; accent?: string }) {
  const { dark } = useMonzaTheme();
  return (
    <div style={{
      background: dark ? "#131b3e" : "white",
      borderRadius: 12,
      border: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}`,
      padding: "18px 20px",
      flex: 1,
    }}>
      <div style={{ width: 28, height: 4, borderRadius: 2, background: accent, marginBottom: 12 }} />
      <div style={{ fontSize: 28, fontWeight: 800, color: dark ? "white" : "#1E293B", lineHeight: 1 }}>{value}</div>
      <div style={{ fontSize: 12, color: dark ? "#8899cc" : "#64748B", marginTop: 5 }}>{label}</div>
      {sub && <div style={{ fontSize: 11, color: dark ? "#475569" : "#94A3B8", marginTop: 3 }}>{sub}</div>}
    </div>
  );
}'''

if OLD_KPICARD_VENTAS in c:
    c = c.replace(OLD_KPICARD_VENTAS, NEW_KPICARD_VENTAS)
    print("✓ Ventas: KpiCard actualizado con tema")
else:
    print("! Ventas: KpiCard no encontrado — verificar manualmente")

# Agregar accents a llamadas ventas
ACCENTS_V = [
    ('label="Vendidas este mes"',    'accent="#10B981"'),
    ('label="Total vendido mes"',    'accent="#F59E0B"'),
    ('label="Pendientes entrega"',   'accent="#6366F1"'),
]
for label_str, accent_str in ACCENTS_V:
    if label_str in c and accent_str not in c:
        c = c.replace(label_str, f'{label_str} {accent_str}')

with open(path, "w") as f:
    f.write(c)
print("✓ MonzaVentasPage.tsx guardado\n")

# ────────────────────────────────────────────────────────────────────────────
# 3. MonzaDashboardPage — corregir colores de light mode en BigCard/MidCard/SmCard
# ────────────────────────────────────────────────────────────────────────────
path = f"{PAGES_DIR}/MonzaDashboardPage.tsx"
with open(path) as f:
    c = f.read()

# BigCard: dark ? "#131b3e" : "#1e2a4a"  →  dark ? "#131b3e" : "white"
c = c.replace(
    'const bg   = dark ? "#131b3e" : "#1e2a4a";',
    'const bg   = dark ? "#131b3e" : "white";'
)
c = c.replace(
    'const bdColor = dark ? "#1e2a4a" : "#253354";',
    'const bdColor = dark ? "#1e2a4a" : "#E2E8F0";'
)
# Tooltip en light mode
c = c.replace(
    'contentStyle={{ background: "#0a0e1f", border: "none", borderRadius: 6, fontSize: 11, color: "white" }}',
    'contentStyle={{ background: dark ? "#0a0e1f" : "white", border: `1px solid ${dark ? "#1e2a4a" : "#E2E8F0"}`, borderRadius: 6, fontSize: 11, color: dark ? "white" : "#1E293B" }}'
)

# MidCard
c = c.replace(
    '  const bg  = dark ? "#131b3e" : "#1e2a4a";\n  const bd  = dark ? "#1e2a4a" : "#253354";',
    '  const bg  = dark ? "#131b3e" : "white";\n  const bd  = dark ? "#1e2a4a" : "#E2E8F0";'
)
# MidCard progress track
c = c.replace(
    '      <div style={{ height: 5, background: "#1e2a4a", borderRadius: 3, overflow: "hidden" }}>',
    '      <div style={{ height: 5, background: dark ? "#1e2a4a" : "#F1F5F9", borderRadius: 3, overflow: "hidden" }}>'
)

# SmCard
c = c.replace(
    '  const bg = dark ? "#131b3e" : "#1e2a4a";\n  const bd = dark ? "#1e2a4a" : "#253354";',
    '  const bg = dark ? "#131b3e" : "white";\n  const bd = dark ? "#1e2a4a" : "#E2E8F0";'
)

with open(path, "w") as f:
    f.write(c)
print("✓ MonzaDashboardPage.tsx: colores light mode corregidos")
