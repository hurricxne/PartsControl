"""Registra los routers MonzaParts en main.py del servidor."""
import re

MAIN_PATH = "/var/www/machparts.bigcode.cl/backend/main.py"

with open(MAIN_PATH, "r") as f:
    content = f.read()

# 1. Agregar imports de routers MonzaParts
IMPORTS_BLOCK = """from monza_router_config import router as monza_config_router
from monza_router_leads import router as monza_leads_router
from monza_router_cotizador import router as monza_cotizador_router
from monza_router_cotizaciones import router as monza_cotizaciones_router
from monza_router_ventas import router as monza_ventas_router
"""

if "monza_router_config" not in content:
    # Insertar antes de la primera línea "from routers"
    first_router_import = re.search(r"^from routers\.", content, re.MULTILINE)
    if first_router_import:
        pos = first_router_import.start()
        content = content[:pos] + IMPORTS_BLOCK + content[pos:]
        print("✓ Imports agregados")
    else:
        print("✗ No se encontró 'from routers.' en main.py — agregar imports manualmente")
else:
    print("· Imports ya existen, no se modifican")

# 2. Agregar include_router calls
INCLUDE_BLOCK = """app.include_router(monza_config_router)
app.include_router(monza_leads_router)
app.include_router(monza_cotizador_router)
app.include_router(monza_cotizaciones_router)
app.include_router(monza_ventas_router)
"""

if "monza_config_router" not in content:
    # Insertar antes del último include_router o antes de @app.get("/api/health")
    health_match = re.search(r'@app\.get\("/api/health"\)', content)
    if health_match:
        pos = health_match.start()
        content = content[:pos] + INCLUDE_BLOCK + "\n" + content[pos:]
        print("✓ include_router calls agregados")
    else:
        # Insertar al final del bloque de include_router
        last_include = None
        for m in re.finditer(r"app\.include_router\(", content):
            last_include = m
        if last_include:
            end_of_line = content.index("\n", last_include.start()) + 1
            content = content[:end_of_line] + INCLUDE_BLOCK + content[end_of_line:]
            print("✓ include_router calls agregados (al final del bloque)")
        else:
            print("✗ No se encontró dónde insertar — agregar manualmente")
else:
    print("· include_router calls ya existen")

with open(MAIN_PATH, "w") as f:
    f.write(content)

print("✓ main.py actualizado")
