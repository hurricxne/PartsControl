"""Agrega rutas MonzaParts a App.tsx y el import del MonzaLayout."""
import re

APP_PATH = "/var/www/machparts.bigcode.cl/frontend-src/src/App.tsx"

with open(APP_PATH, "r") as f:
    content = f.read()

# 1. Agregar imports MonzaParts
IMPORTS = """
// MonzaParts
import MonzaLayout from './pages/MonzaLayout'
import MonzaLeadsPage from './pages/MonzaLeadsPage'
import MonzaCotizadorPage from './pages/MonzaCotizadorPage'
import MonzaCotizacionesPage from './pages/MonzaCotizacionesPage'
import MonzaVentasPage from './pages/MonzaVentasPage'
import MonzaConfigPage from './pages/MonzaConfigPage'
"""

if "MonzaLayout" not in content:
    # Insertar antes de la función App o del primer export
    match = re.search(r"^function App\(\)", content, re.MULTILINE)
    if not match:
        match = re.search(r"^export default", content, re.MULTILINE)
    if match:
        pos = match.start()
        content = content[:pos] + IMPORTS + "\n" + content[pos:]
        print("✓ Imports MonzaParts agregados")
    else:
        print("✗ No se encontró dónde insertar imports")
else:
    print("· Imports MonzaParts ya existen")

# 2. Agregar rutas dentro del Router/Routes
ROUTES = """
          {/* MonzaParts */}
          <Route path="/monzaparts" element={<MonzaLayout />}>
            <Route index element={<MonzaLeadsPage />} />
            <Route path="leads" element={<MonzaLeadsPage />} />
            <Route path="cotizador" element={<MonzaCotizadorPage />} />
            <Route path="cotizaciones" element={<MonzaCotizacionesPage />} />
            <Route path="ventas" element={<MonzaVentasPage />} />
            <Route path="configuracion" element={<MonzaConfigPage />} />
          </Route>
"""

if "MonzaLeadsPage" not in content:
    # Buscar el cierre de Routes (</Routes>) e insertar antes
    idx = content.rfind("</Routes>")
    if idx != -1:
        content = content[:idx] + ROUTES + "\n        " + content[idx:]
        print("✓ Rutas MonzaParts agregadas")
    else:
        print("✗ No se encontró </Routes> — agregar rutas manualmente")
else:
    print("· Rutas MonzaParts ya existen")

with open(APP_PATH, "w") as f:
    f.write(content)

print("✓ App.tsx actualizado")
