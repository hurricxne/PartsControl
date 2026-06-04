"""Agrega MonzaDashboardPage al App.tsx e inserta en la ruta index de /monzaparts."""

APP_PATH = "/var/www/machparts.bigcode.cl/frontend-src/src/App.tsx"

with open(APP_PATH, "r") as f:
    content = f.read()

# 1. Agregar import MonzaDashboardPage
if "MonzaDashboardPage" not in content:
    old = "// MonzaParts"
    new = "// MonzaParts\nimport MonzaDashboardPage from './pages/MonzaDashboardPage'"
    content = content.replace(old, new)
    print("✓ Import MonzaDashboardPage agregado")
else:
    print("· Import MonzaDashboardPage ya existe")

# 2. Cambiar <Route index element={<MonzaLeadsPage />} /> por MonzaDashboardPage
if "<Route index element={<MonzaDashboardPage" not in content:
    old = "<Route index element={<MonzaLeadsPage />} />"
    new = "<Route index element={<MonzaDashboardPage />} />"
    if old in content:
        content = content.replace(old, new)
        print("✓ Ruta index cambiada a MonzaDashboardPage")
    else:
        print("! Ruta index no encontrada")
else:
    print("· Ruta index ya usa MonzaDashboardPage")

# 3. Asegurarse de que la ruta /dashboard existe
if 'path="dashboard"' not in content:
    old = '<Route path="leads" element={<MonzaLeadsPage />} />'
    new = '<Route path="dashboard" element={<MonzaDashboardPage />} />\n        <Route path="leads" element={<MonzaLeadsPage />} />'
    if old in content:
        content = content.replace(old, new)
        print("✓ Ruta /dashboard agregada")
    else:
        print("! Ruta leads no encontrada para insertar dashboard")
else:
    print("· Ruta /dashboard ya existe")

with open(APP_PATH, "w") as f:
    f.write(content)

# Verificar
print("\nImports MonzaParts en App.tsx:")
for i, line in enumerate(content.splitlines(), 1):
    if "Monza" in line and ("import" in line or "Route" in line):
        print(f"  {i:3d}: {line.strip()}")
