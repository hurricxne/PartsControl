# PROD: se despliega SOLO la contabilidad de MachParts. La de MonzaParts NO
# (decisión del cliente 2026-07-15). Este script neutraliza las referencias a
# los módulos Monza-contab que vienen en main.py / App.tsx / MonzaLayout.tsx,
# que en PROD no existen (romperían el import de la API y el menú de Monza).
import ast, re, sys

BASE = "/var/www/machparts.bigcode.cl"
NOTA = "PROD: contabilidad Monza NO desplegada (solo MachParts) - 2026-07-15"

# ── 1) backend/main.py: comentar imports y registros ─────────────────────────
p = f"{BASE}/backend/main.py"
s = open(p, encoding="utf-8").read()
n = 0
out = []
for ln in s.split("\n"):
    if re.search(r"monza_(contabilidad|embarques_pricing|compras_contab|tesoreria)", ln) and not ln.strip().startswith("#"):
        out.append(f"# {ln}  # {NOTA}")
        n += 1
    else:
        out.append(ln)
open(p, "w", encoding="utf-8").write("\n".join(out))
ast.parse(open(p, encoding="utf-8").read())
print(f"  main.py: {n} lineas neutralizadas, sintaxis OK")

# ── 2) frontend App.tsx: quitar imports y rutas ──────────────────────────────
p = f"{BASE}/frontend-src/src/App.tsx"
s = open(p, encoding="utf-8").read()
PAGES = ("MonzaVentasContabPage", "MonzaEmbarquesPricingPage", "MonzaComprasPage", "MonzaTesoreriaPage")
out, n = [], 0
for ln in s.split("\n"):
    if any(pg in ln for pg in PAGES):
        n += 1
        continue  # se elimina (import o <Route>)
    out.append(ln)
open(p, "w", encoding="utf-8").write("\n".join(out))
print(f"  App.tsx: {n} lineas eliminadas (imports + rutas)")

# ── 3) MonzaLayout.tsx: quitar los links del menu ────────────────────────────
p = f"{BASE}/frontend-src/src/pages/MonzaLayout.tsx"
s = open(p, encoding="utf-8").read()
RUTAS = ("/monzaparts/ventas-contab", "/monzaparts/compras-contab",
         "/monzaparts/tesoreria", "/monzaparts/embarques-pricing")
out, n = [], 0
for ln in s.split("\n"):
    if any(r in ln for r in RUTAS):
        n += 1
        continue  # entrada de nav o titulo
    out.append(ln)
open(p, "w", encoding="utf-8").write("\n".join(out))
print(f"  MonzaLayout.tsx: {n} lineas eliminadas (nav + titulos)")

# ── verificacion ─────────────────────────────────────────────────────────────
print("\n-- verificacion: no deben quedar referencias --")
for f in ("backend/main.py", "frontend-src/src/App.tsx", "frontend-src/src/pages/MonzaLayout.tsx"):
    txt = open(f"{BASE}/{f}", encoding="utf-8").read()
    activas = [l for l in txt.split("\n")
               if re.search(r"MonzaVentasContab|MonzaEmbarquesPricing|MonzaComprasPage|MonzaTesoreria|monza_(contabilidad|embarques_pricing|compras_contab|tesoreria)", l)
               and not l.strip().startswith("#")]
    print(f"  {f}: {'OK sin referencias activas' if not activas else activas}")
print("\nCURADO OK")
