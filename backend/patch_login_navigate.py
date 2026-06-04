"""Actualiza LoginPage.tsx para redirigir a /monzaparts/leads cuando empresa=automotriz."""

LOGIN_PATH = "/var/www/machparts.bigcode.cl/frontend-src/src/pages/LoginPage.tsx"

with open(LOGIN_PATH, "r") as f:
    content = f.read()

old = "      navigate('/', { replace: true })"
new = "      navigate(userEmpresa === 'automotriz' ? '/monzaparts/leads' : '/', { replace: true })"

if old in content:
    content = content.replace(old, new)
    with open(LOGIN_PATH, "w") as f:
        f.write(content)
    print("✓ LoginPage.tsx: navigate actualizado para redirigir a MonzaParts")
else:
    print("! navigate no encontrado con ese texto exacto — revisar manualmente")
    # Mostrar contexto
    import re
    m = re.search(r"navigate\([^)]+\)", content)
    if m:
        start = max(0, m.start()-100)
        print("Contexto encontrado:", repr(content[start:m.end()+100]))
