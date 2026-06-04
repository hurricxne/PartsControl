"""Agrega campo empresa al modelo User y actualiza auth.py para retornarlo."""
import re

MODELS_PATH = "/var/www/machparts.bigcode.cl/backend/models/models.py"
AUTH_PATH = "/var/www/machparts.bigcode.cl/backend/routers/auth.py"

# 1. Patch models.py — agregar empresa al User
with open(MODELS_PATH, "r") as f:
    content = f.read()

if "empresa" not in content:
    # Insertar después de is_active
    old = "    is_active = Column(Integer, default=1)"
    new = "    is_active = Column(Integer, default=1)\n    empresa = Column(String(50), nullable=False, server_default='mineria')"
    content = content.replace(old, new)
    with open(MODELS_PATH, "w") as f:
        f.write(content)
    print("✓ models.py: campo empresa agregado al User")
else:
    print("· models.py: empresa ya existe")

# 2. Patch auth.py — retornar empresa en login y me
with open(AUTH_PATH, "r") as f:
    content = f.read()

# Patch login endpoint
old_user_dict = '"user": {"id": user.id, "email": user.email, "nombre": user.nombre}'
new_user_dict = '"user": {"id": user.id, "email": user.email, "nombre": user.nombre, "empresa": user.empresa or "mineria"}'
if old_user_dict in content:
    content = content.replace(old_user_dict, new_user_dict)
    print("✓ auth.py: empresa agregada al response de /login")
else:
    print("· auth.py: /login ya retorna empresa (o estructura distinta)")

# Patch /me endpoint
old_me = 'return {"id": current_user.id, "email": current_user.email, "nombre": current_user.nombre}'
new_me = 'return {"id": current_user.id, "email": current_user.email, "nombre": current_user.nombre, "empresa": current_user.empresa or "mineria"}'
if old_me in content:
    content = content.replace(old_me, new_me)
    print("✓ auth.py: empresa agregada al response de /me")
else:
    print("· auth.py: /me ya retorna empresa (o estructura distinta)")

with open(AUTH_PATH, "w") as f:
    f.write(content)

print("✓ Parches aplicados")
