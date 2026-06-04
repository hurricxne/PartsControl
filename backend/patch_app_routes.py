"""Inserta las rutas MonzaParts en App.tsx (antes del catch-all path='*')."""

APP_PATH = "/var/www/machparts.bigcode.cl/frontend-src/src/App.tsx"

with open(APP_PATH, "r") as f:
    content = f.read()

# Verificar si las rutas ya existen (no solo el import)
if 'path="/monzaparts"' in content or "path='/monzaparts'" in content:
    print("· Rutas MonzaParts ya existen en App.tsx")
else:
    ROUTES = """
      {/* MonzaParts */}
      <Route
        path="/monzaparts"
        element={
          <PrivateRoute>
            <MonzaLayout />
          </PrivateRoute>
        }
      >
        <Route index element={<MonzaLeadsPage />} />
        <Route path="leads" element={<MonzaLeadsPage />} />
        <Route path="cotizador" element={<MonzaCotizadorPage />} />
        <Route path="cotizaciones" element={<MonzaCotizacionesPage />} />
        <Route path="ventas" element={<MonzaVentasPage />} />
        <Route path="configuracion" element={<MonzaConfigPage />} />
      </Route>
"""
    # Insertar antes del catch-all
    ANCHOR = '      <Route path="*" element={<Navigate to="/" replace />} />'
    if ANCHOR in content:
        content = content.replace(ANCHOR, ROUTES + "\n" + ANCHOR)
        print("✓ Rutas MonzaParts insertadas antes del catch-all")
    else:
        # Fallback: insertar antes del cierre de Routes
        idx = content.rfind("</Routes>")
        if idx != -1:
            content = content[:idx] + ROUTES + "\n      " + content[idx:]
            print("✓ Rutas MonzaParts insertadas antes de </Routes> (fallback)")
        else:
            print("✗ No se encontró dónde insertar las rutas")
            exit(1)

    with open(APP_PATH, "w") as f:
        f.write(content)

# Mostrar las líneas con monzaparts para verificar
print("\nVerificación — líneas con 'monzaparts' en App.tsx:")
for i, line in enumerate(content.splitlines(), 1):
    if "monzaparts" in line.lower() or "monza" in line.lower():
        print(f"  {i:3d}: {line}")
