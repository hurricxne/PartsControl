"""AISLAMIENTO del Portal de Clientes — la app COMPLETA, cuentas reales y sesiones reales.

LO QUE ESTA SUITE PROTEGE
  §1  CANDADO ESTRUCTURAL (estático, sin HTTP): cada ruta de la app tiene exactamente
      una de dos puertas — get_current_user (ERP, niega cuentas portal) o require_portal
      (portal, niega al personal) — salvo una lista CORTA y explícita de rutas públicas.
      Una ruta nueva sin puerta, o una ruta del portal sin require_portal, pone rojo acá
      y dice cuál es. Y get_usuario_autenticado (la dependencia que deja pasar a
      cualquier tipo de cuenta) solo puede aparecer en los archivos que la necesitan.
      Dentro del portal, toda ruta que entrega DATOS del cliente exige además que la
      cuenta ya haya reemplazado su clave temporal (require_portal_listo) y cobra el
      tope de frecuencia; las únicas dos que no, son las de identidad (/portal/yo y el
      cambio de clave), listadas a mano.
  §2  UNA SESIÓN PORTAL NO ENTRA AL ERP: con un token real de cliente se golpean TODAS
      las rutas GET internas y cada una debe responder 403 con el detalle de la cuenta
      portal. (Solo GET a propósito: si el corte se rompiera, un POST/DELETE correría de
      verdad contra la base local.)
  §3  CADA CUENTA VE LO SUYO: A ve la ficha A, B ve la B; /auth/me responde a ambas.
  §4  EL PERSONAL NO ENTRA AL PORTAL, y sigue entrando a lo suyo. El listado de
      asesores (/auth/users) no incluye cuentas portal.
  §5  FALLAR CERRADO: cuenta sin cliente, vencida por inactividad (en el guard Y en el
      login), desactivada, sin token, y un tipo_acceso desconocido que no entra a
      ninguno de los dos lados.
  §6  El login de una cuenta portal informa tipo_acceso y anota el último acceso.
  §S  ESQUEMA A MEDIAS: con la tabla del portal ausente (1146), /portal/yo y Cuentas del
      portal responden 503 con el comando de init_db y el error real queda en el log;
      cualquier otro error de la base se RELANZA (500), no se anuncia como «portal no
      instalado».
  §R  ROLLBACK: desactivar_cuentas.py apaga toda cuenta que no sea del personal (la
      portal Y la de tipo desconocido), y deja al personal activo. Se prueba con el
      UPDATE real dentro de una transacción que se revierte.

Un token basura no sirve para nada de esto (get_usuario_autenticado responde 401 antes
de llegar a ningún guard): todas las sondas usan sesiones REALES, obtenidas con
POST /api/auth/login.

MUTACIONES verificadas al construir la suite (quitar → rojo → restaurar → verde):
  corte en get_current_user (→ §2), chequeo de tipo en require_portal (→ §4 y §5 raro),
  corte por inactividad en require_portal (→ §5 guard) y en el login (→ §5 login),
  puente vacío (→ §5 sin cliente), filtro de /auth/users (→ §4 asesores), una ruta
  pública agregada a la app (→ §1) y una ruta de datos del portal agregada sin
  require_portal_listo (→ §1). Además: el 503 ante cualquier error de la base, el
  logger.exception y la clase de ruta en router.py y router_admin.py (→ §S), y el
  predicado del rollback en las dos direcciones (→ §R). Se repiten con
  `python -m portal.tests.mutaciones aislamiento` (portal/tests/mutaciones/cuentas.py).

Corre con:  ./venv/bin/python -m pytest portal/tests/test_aislamiento.py -q
(también:   ./venv/bin/python portal/tests/test_aislamiento.py)
"""
import ast
import os
import re
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import pymysql  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import OperationalError, ProgrammingError  # noqa: E402

from auth import DETALLE_CUENTA_PORTAL, get_current_user, get_usuario_autenticado  # noqa: E402
from database import SessionLocal, engine  # noqa: E402
from main import app  # noqa: E402
from models.models import User  # noqa: E402
from portal.deps import (  # noqa: E402
    DETALLE_NO_ES_PORTAL, DETALLE_SIN_CLIENTE, DETALLE_VENCIDA, RutaDelPortal, require_portal,
    require_portal_listo,
)
from portal.models import PortalAcceso, UsuarioCliente  # noqa: E402
from portal.router_ordenes import _cupo_general  # noqa: E402
from portal.service import DIAS_INACTIVIDAD_CORTE  # noqa: E402
from portal.tests import _datos as D  # noqa: E402

MARK = "ZZPORTALAISL"  # SIN guiones bajos: en LIKE el _ es comodín
BACKEND = Path(__file__).resolve().parent.parent.parent

client = TestClient(app)
# Para los casos que esperan un 500 como RESPUESTA (y no como excepción que aborta la corrida).
tolerante = TestClient(app, raise_server_exceptions=False)
_fails: list = []

# Rutas sin sesión, por diseño. Agregar una acá es una decisión de seguridad: se revisa.
PUBLICAS = {
    ("GET", "/api/health"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/monza/integraciones/nexor/leads"),  # entra con X-API-Key, no con JWT
}
# Rutas del ERP que aceptan cualquier tipo de cuenta (solo devuelven datos del propio usuario).
CUALQUIER_CUENTA = {("GET", "/api/auth/me")}
# Rutas del portal que atienden a una cuenta que TODAVÍA tiene la clave temporal: quién
# es y el cambio de clave. Todo lo demás de /api/portal/* entrega datos del cliente y
# exige require_portal_listo. Agregar una acá es una decisión de seguridad: se revisa.
DE_IDENTIDAD = {("GET", "/api/portal/yo"), ("POST", "/api/portal/yo/clave")}
# Únicos archivos que pueden nombrar get_usuario_autenticado.
ARCHIVOS_CON_LA_PUERTA_ABIERTA = {"auth.py", "routers/auth.py", "portal/deps.py"}


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _dependencias(dependant) -> set:
    """Todas las funciones de las que depende una ruta, recursivo (incluye lo que
    require_empresa/require_rol traen adentro)."""
    vistas = set()
    for sub in dependant.dependencies:
        vistas.add(sub.call)
        vistas |= _dependencias(sub)
    return vistas


def _rutas():
    for r in app.routes:
        if isinstance(r, APIRoute):
            for metodo in sorted(r.methods - {"HEAD", "OPTIONS"}):
                yield metodo, r.path, r


def _login(email: str):
    return client.post("/api/auth/login", json={"email": email, "password": D.CLAVE})


def _cab(email: str) -> dict:
    r = _login(email)
    assert r.status_code == 200, f"login de {email}: {r.status_code} {r.text[:120]}"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# ── §1 ──────────────────────────────────────────────────────────────────────────────────

def _paso_1_cada_ruta_tiene_su_puerta():
    sin_puerta, portal_mal, erp_mal = [], [], []
    sin_clave_propia, sin_cupo, identidad_mal, sin_aviso_de_esquema = [], [], [], []
    total = de_datos = 0
    for metodo, path, ruta in _rutas():
        total += 1
        deps = _dependencias(ruta.dependant)
        llave = (metodo, path)
        if path.startswith("/api/portal/"):
            if require_portal not in deps or get_current_user in deps:
                portal_mal.append(llave)
            if not isinstance(ruta, RutaDelPortal):
                sin_aviso_de_esquema.append(llave)
            if llave in DE_IDENTIDAD:
                if require_portal_listo in deps:
                    identidad_mal.append(llave)
                continue
            de_datos += 1
            if require_portal_listo not in deps:
                sin_clave_propia.append(llave)
            if _cupo_general not in deps:
                sin_cupo.append(llave)
        elif llave in PUBLICAS:
            continue
        elif llave in CUALQUIER_CUENTA:
            if get_usuario_autenticado not in deps:
                erp_mal.append(llave)
        elif get_current_user not in deps:
            sin_puerta.append(llave)
    check(f"§1 las {total} rutas se inspeccionaron (la app tiene cientos)", total > 300, total)
    check("§1 toda ruta del ERP pasa por get_current_user (o está en PUBLICAS)",
          not sin_puerta, sin_puerta[:10])
    check("§1 toda ruta /api/portal/* pasa por require_portal y NO por get_current_user",
          not portal_mal, portal_mal)
    check("§1 las rutas de CUALQUIER_CUENTA usan get_usuario_autenticado", not erp_mal, erp_mal)
    check(f"§1 las {de_datos} rutas de datos del portal exigen que la cuenta ya tenga su propia clave "
          "(require_portal_listo)", de_datos >= 8 and not sin_clave_propia, sin_clave_propia)
    check("§1 …y todas cobran el tope de frecuencia de la cuenta", not sin_cupo, sin_cupo)
    check("§1 las dos rutas de identidad NO lo exigen (con la clave temporal hay que poder cambiarla)",
          not identidad_mal, identidad_mal)
    check("§1 toda ruta /api/portal/* avisa con un 503 claro si falta una tabla del módulo (RutaDelPortal)",
          not sin_aviso_de_esquema, sin_aviso_de_esquema)
    presentes = {(m, p) for m, p, _ in _rutas()}
    check("§1 las listas PUBLICAS y DE_IDENTIDAD no tienen entradas muertas",
          PUBLICAS <= presentes and DE_IDENTIDAD <= presentes, (PUBLICAS | DE_IDENTIDAD) - presentes)


def _nombra_en_codigo(fuente: str, nombre: str) -> bool:
    """¿El archivo USA el nombre (import, referencia o atributo)? Se mira el árbol de
    sintaxis y no el texto: una mención en un docstring o un comentario no es un uso."""
    for nodo in ast.walk(ast.parse(fuente)):
        if isinstance(nodo, ast.Name) and nodo.id == nombre:
            return True
        if isinstance(nodo, ast.Attribute) and nodo.attr == nombre:
            return True
        if isinstance(nodo, ast.alias) and nombre in (nodo.name, nodo.asname):
            return True
        if isinstance(nodo, ast.FunctionDef) and nodo.name == nombre:
            return True
    return False


def _paso_1b_la_puerta_abierta_no_se_usa_en_otro_lado():
    usos = set()
    for py in BACKEND.rglob("*.py"):
        partes = py.relative_to(BACKEND).parts
        if "venv" in partes or "tests" in partes or "__pycache__" in partes:
            continue
        if _nombra_en_codigo(py.read_text(encoding="utf-8"), "get_usuario_autenticado"):
            usos.add("/".join(partes))
    check("§1 get_usuario_autenticado solo aparece donde debe",
          usos == ARCHIVOS_CON_LA_PUERTA_ABIERTA, sorted(usos ^ ARCHIVOS_CON_LA_PUERTA_ABIERTA))


# ── §2 ──────────────────────────────────────────────────────────────────────────────────

def _paso_2_una_sesion_portal_no_entra_al_erp(cab_a: dict):
    malas, n = [], 0
    for metodo, path, _ in _rutas():
        if metodo != "GET" or path.startswith("/api/portal/"):
            continue
        if (metodo, path) in PUBLICAS | CUALQUIER_CUENTA:
            continue
        n += 1
        url = re.sub(r"\{[^}]+\}", "0", path)
        r = client.get(url, headers=cab_a)
        detalle = ""
        try:
            detalle = (r.json() or {}).get("detail", "")
        except Exception:  # noqa: BLE001 — respuesta no-JSON: ya es un fallo, se reporta abajo
            pass
        if r.status_code != 403 or detalle != DETALLE_CUENTA_PORTAL:
            malas.append((path, r.status_code, str(detalle)[:60]))
    check(f"§2 se golpearon las {n} rutas GET del ERP con la sesión portal", n > 150, n)
    check("§2 TODAS responden 403 con el detalle de cuenta portal", not malas, malas[:8])
    # /api/portal-admin es ERP aunque empiece parecido: el corte también lo cubre.
    r = client.get("/api/portal-admin/cuentas", headers=cab_a)
    check("§2 /api/portal-admin también niega a la cuenta portal", r.status_code == 403, r.status_code)


# ── §3 ──────────────────────────────────────────────────────────────────────────────────

def _paso_3_cada_cuenta_ve_lo_suyo(cab_a, cab_b, ids):
    for nombre, cab, ficha_id in (("A", cab_a, ids["ficha_a"]), ("B", cab_b, ids["ficha_b"])):
        r = client.get("/api/portal/yo", headers=cab)
        check(f"§3 la cuenta {nombre} entra a /portal/yo", r.status_code == 200, r.text[:120])
        vistos = [c["id"] for c in (r.json().get("clientes") or [])] if r.status_code == 200 else None
        check(f"§3 la cuenta {nombre} ve SOLO su ficha", vistos == [ficha_id], vistos)
        r = client.get("/api/auth/me", headers=cab)
        check(f"§3 /auth/me responde a la cuenta {nombre} y dice que es portal",
              r.status_code == 200 and r.json().get("tipo_acceso") == "portal", r.text[:120])


# ── §4 ──────────────────────────────────────────────────────────────────────────────────

def _paso_4_el_personal_no_entra_al_portal(cab_int, ids):
    r = client.get("/api/portal/yo", headers=cab_int)
    check("§4 el personal recibe 403 en /portal/yo",
          r.status_code == 403 and r.json().get("detail") == DETALLE_NO_ES_PORTAL, r.text[:120])
    r = client.get("/api/auth/me", headers=cab_int)
    check("§4 el personal sigue entrando a lo suyo (/auth/me, tipo interno)",
          r.status_code == 200 and r.json().get("tipo_acceso") == "interno", r.text[:120])
    r = client.get("/api/auth/users", headers=cab_int)
    listados = {u["id"] for u in r.json()} if r.status_code == 200 else set()
    check("§4 /auth/users lista al personal", ids["interno"] in listados, r.text[:120])
    check("§4 /auth/users NO lista cuentas portal (no son asesores)",
          not ({ids["portal_a"], ids["portal_b"]} & listados), listados)


# ── §5 ──────────────────────────────────────────────────────────────────────────────────

def _paso_5_fallar_cerrado(ids):
    r = client.get("/api/portal/yo")
    check("§5 sin token → 401", r.status_code == 401, r.status_code)

    cab = _cab(ids["email_sin"])
    r = client.get("/api/portal/yo", headers=cab)
    check("§5 cuenta sin cliente → 403 (jamás 'sin filtro = ve todo')",
          r.status_code == 403 and r.json().get("detail") == DETALLE_SIN_CLIENTE, r.text[:120])

    # Vencida: la sesión se obtiene ANTES de envejecer la cuenta, para probar el guard y
    # no solo el login.
    cab = _cab(ids["email_vencida"])
    db = SessionLocal()
    try:
        viejo = D.ahora() - timedelta(days=DIAS_INACTIVIDAD_CORTE + 1)
        db.query(PortalAcceso).filter(PortalAcceso.usuario_id == ids["portal_vencida"]).update(
            {"vigente_desde": viejo, "ultimo_acceso_at": viejo})
        db.commit()
    finally:
        db.close()
    r = client.get("/api/portal/yo", headers=cab)
    check("§5 cuenta vencida, con sesión ya abierta → 403 en el guard",
          r.status_code == 403 and r.json().get("detail") == DETALLE_VENCIDA, r.text[:120])
    r = _login(ids["email_vencida"])
    check("§5 cuenta vencida → el login responde 403 con el motivo",
          r.status_code == 403 and r.json().get("detail") == DETALLE_VENCIDA, r.text[:120])

    # Tipo desconocido: no es interno ni portal → no entra a NINGÚN lado.
    cab = _cab(ids["email_raro"])
    r = client.get("/api/auth/users", headers=cab)
    check("§5 tipo_acceso desconocido → 403 en el ERP", r.status_code == 403, r.status_code)
    r = client.get("/api/portal/yo", headers=cab)
    check("§5 tipo_acceso desconocido → 403 en el portal",
          r.status_code == 403 and r.json().get("detail") == DETALLE_NO_ES_PORTAL, r.text[:120])

    # Desactivada: la sesión abierta deja de servir en el acto.
    cab = _cab(ids["email_baja"])
    db = SessionLocal()
    try:
        db.query(User).filter(User.id == ids["portal_baja"]).update({"is_active": 0})
        db.commit()
    finally:
        db.close()
    r = client.get("/api/portal/yo", headers=cab)
    check("§5 cuenta desactivada con sesión abierta → 401", r.status_code == 401, r.status_code)


# ── §6 ──────────────────────────────────────────────────────────────────────────────────

def _paso_6_login_portal(ids):
    r = _login(ids["email_a"])
    check("§6 el login informa tipo_acceso=portal",
          r.status_code == 200 and r.json()["user"].get("tipo_acceso") == "portal", r.text[:160])
    db = SessionLocal()
    try:
        acceso = db.query(PortalAcceso).filter(PortalAcceso.usuario_id == ids["portal_a"]).first()
        check("§6 el login anotó el último acceso", acceso is not None and acceso.ultimo_acceso_at is not None)
    finally:
        db.close()
    r = _login(ids["email_int"])
    check("§6 el login del personal informa tipo_acceso=interno",
          r.status_code == 200 and r.json()["user"].get("tipo_acceso") == "interno", r.text[:160])


# ── §S ──────────────────────────────────────────────────────────────────────────────────

def _detalle(r) -> str:
    """El `detail` de una respuesta, o su texto si no es JSON (un 500 del cliente tolerante)."""
    try:
        return str((r.json() or {}).get("detail", ""))
    except ValueError:
        return r.text[:120]


def _falla_de_base(clase, error_del_driver):
    """Una dependencia que falla como fallaría la base: con la tabla ausente (1146) o con
    la conexión caída (2013). Sin tocar el esquema."""
    def dependencia():
        raise clase("SELECT 1", {}, error_del_driver)
    return dependencia


def _paso_s_esquema_a_medias():
    """Deploy sin `portal.init_db`: solo «no existe la tabla / la columna» (1146/1054) se
    traduce al 503 con el comando, y lo hace la clase de ruta RutaDelPortal en los
    cuatro routers del portal. Cualquier otro error de la base se RELANZA (500 con su
    traceback): anunciar «corre init_db» ante una conexión caída mandaría al operador a
    correr una migración que no arregla nada. dependency_overrides es global a la app
    que comparten las suites: se restaura en finally."""
    sin_tabla = _falla_de_base(
        ProgrammingError, pymysql.err.ProgrammingError(1146, "Table x.portal_acceso doesn't exist"))
    app.dependency_overrides[require_portal] = sin_tabla
    try:
        with D.log_del_portal() as bitacora:
            r = tolerante.get("/api/portal/yo")
        check("§S s1 con la tabla del portal ausente, /portal/yo responde 503 con el comando de init_db",
              r.status_code == 503 and "portal.init_db" in _detalle(r), f"{r.status_code} {_detalle(r)}")
        check("§S s1 y el error real de la base queda en el log (1146)",
              "1146" in bitacora.getvalue(), bitacora.getvalue()[-200:] or "(nada en la bitácora)")
        app.dependency_overrides[require_portal] = _falla_de_base(
            OperationalError, pymysql.err.OperationalError(2013, "Lost connection to MySQL server"))
        try:
            r = client.get("/api/portal/yo")
            resultado = f"no se relanzó: respondió {r.status_code}"
        except OperationalError:
            resultado = "relanzada"
        check("§S s2 un error de la base que NO es de esquema se relanza (500 con traceback), "
              "no se anuncia como «portal no instalado»", resultado == "relanzada", resultado)
    finally:
        app.dependency_overrides.pop(require_portal, None)
    # La puerta gemela: Cuentas del portal, donde el comando le llega a quien puede correrlo.
    app.dependency_overrides[get_current_user] = sin_tabla
    try:
        r = tolerante.get("/api/portal-admin/cuentas")
        check("§S s3 el personal ve el mismo 503 con el comando en Cuentas del portal",
              r.status_code == 503 and "portal.init_db" in _detalle(r), f"{r.status_code} {_detalle(r)}")
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ── §R ──────────────────────────────────────────────────────────────────────────────────

def _paso_r_rollback(ids):
    """El script de rollback (portal/desactivar_cuentas.py) tiene que apagar TODO lo que
    el corte de auth.get_current_user mantiene fuera del ERP (lo que no es 'interno'),
    no «las que dicen 'portal'»: con el código anterior a la Fase 1, cualquier cuenta
    activa es personal, sea cual sea su tipo. Se ejecuta el UPDATE real dentro de UNA
    transacción que se revierte siempre; si el proceso muriera, MySQL la revierte al
    cerrar la conexión."""
    from portal import desactivar_cuentas as DC
    conn = engine.connect()
    tx = conn.begin()
    try:
        tipos = {tipo for tipo, _, _ in DC.resumen(conn)}
        check("§R el resumen del rollback incluye el tipo desconocido 'otro'", "otro" in tipos, tipos)
        DC.desactivar(conn)
        activos = dict(conn.execute(
            text("SELECT id, is_active FROM users WHERE id IN (:a, :raro, :interno)"),
            {"a": ids["portal_a"], "raro": ids["portal_raro"], "interno": ids["interno"]}).fetchall())
        check("§R desactiva la cuenta portal y también la de tipo desconocido",
              activos.get(ids["portal_a"]) == 0 and activos.get(ids["portal_raro"]) == 0, activos)
        check("§R y el personal sigue activo", activos.get(ids["interno"]) == 1, activos)
    finally:
        tx.rollback()
        conn.close()
    db = SessionLocal()
    try:
        check("§R (nada quedó escrito) la cuenta portal A sigue activa después del rollback",
              db.query(User.is_active).filter(User.id == ids["portal_a"]).scalar() == 1)
    finally:
        db.close()


def _sembrar() -> dict:
    db = SessionLocal()
    try:
        fa = D.ficha(db, MARK, "A", 5000011)
        fb = D.ficha(db, MARK, "B", 5000022)
        interno = D.interno(db, MARK, "int")
        pa = D.portal(db, MARK, "pa", [fa.id])
        pb = D.portal(db, MARK, "pb", [fb.id])
        sin = D.portal(db, MARK, "sin", [fa.id])
        vencida = D.portal(db, MARK, "venc", [fa.id])
        raro = D.portal(db, MARK, "raro", [fa.id])
        baja = D.portal(db, MARK, "baja", [fa.id])
        db.flush()
        db.query(UsuarioCliente).filter(UsuarioCliente.usuario_id == sin.id).delete()
        db.query(User).filter(User.id == raro.id).update({"tipo_acceso": "otro"})
        db.commit()
        return {
            "ficha_a": fa.id, "ficha_b": fb.id, "interno": interno.id,
            "portal_a": pa.id, "portal_b": pb.id, "portal_vencida": vencida.id, "portal_baja": baja.id,
            "portal_raro": raro.id,
            "email_int": interno.email, "email_a": pa.email, "email_b": pb.email,
            "email_sin": sin.email, "email_vencida": vencida.email,
            "email_raro": raro.email, "email_baja": baja.email,
        }
    finally:
        db.close()


def run():
    del _fails[:]
    D.limpiar(MARK)
    try:
        ids = _sembrar()
        _paso_1_cada_ruta_tiene_su_puerta()
        _paso_1b_la_puerta_abierta_no_se_usa_en_otro_lado()
        cab_a, cab_b, cab_int = _cab(ids["email_a"]), _cab(ids["email_b"]), _cab(ids["email_int"])
        _paso_2_una_sesion_portal_no_entra_al_erp(cab_a)
        _paso_3_cada_cuenta_ve_lo_suyo(cab_a, cab_b, ids)
        _paso_4_el_personal_no_entra_al_portal(cab_int, ids)
        _paso_5_fallar_cerrado(ids)
        _paso_6_login_portal(ids)
        _paso_s_esquema_a_medias()
        _paso_r_rollback(ids)
        print()
        print(f"FALLARON {len(_fails)}: {_fails}" if _fails else "SUITE VERDE: aislamiento del portal")
        assert not _fails, _fails
    finally:
        D.limpiar(MARK)


def test_aislamiento_portal():
    run()


if __name__ == "__main__":
    run()
