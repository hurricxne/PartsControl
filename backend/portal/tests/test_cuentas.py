"""CUENTAS del Portal de Clientes — alta, claves y ciclo de vida, con la app completa.

LO QUE ESTA SUITE PROTEGE (/api/portal-admin/* y /api/portal/yo/clave)
  §A  ALTA: crear una cuenta devuelve la clave temporal UNA vez y con ella se entra de
      verdad (login real). La cuenta nace portal, con su puente y su vigencia.
  §B  JAMÁS SE CONVIERTE UNA CUENTA EXISTENTE: el correo de alguien del personal → 409,
      y esa persona sigue siendo interna y con SU clave (si no, quedaría fuera del ERP).
      Validaciones: cliente inexistente 404, sin clientes 400, perfil inválido 400,
      correo con forma inválida (lo que el login no deja escribir) 400. Carreras de
      alta: otra alta del mismo correo en vuelo → 409; la ficha eliminada a mitad del
      alta → 409 (sin hablar del correo).
  §C  LA REGLA DURA: restablecer clave / desactivar / reactivar / eliminar sobre el id
      de alguien del PERSONAL → 404 y no le cambia nada.
  §D  CLAVES: restablecer invalida la anterior; la TEMPORAL caduca (login 403 pasado su
      plazo) y el portal avisa mientras siga vigente; el listado del personal dice hasta
      cuándo sirve y si ya venció (una cuenta 'activa' con la temporal vencida no entra);
      reactivar NO renueva la temporal y lo avisa; el cliente cambia la suya (actual
      mala, corta, demasiado larga, igual a la actual o con texto que no es Unicode
      válido → 400) y el cambio de clave tiene el MISMO tope de intentos que el login
      (429), antes del bcrypt y en la MISMA llave: con el cupo agotado ahí, tampoco se
      entra por el login con la clave correcta; un cambio correcto no deja marca.
  §E  CICLO DE VIDA: desactivar corta el login; reactivar lo devuelve y además reinicia
      el plazo de inactividad de una cuenta vencida. El listado dice el estado.
  §F  QUIÉN OPERA: el personal de la OTRA marca recibe 403 en /portal-admin.
  §G  LA FICHA ES EL ANCLA (routers/clientes.py): con cuentas colgando no se puede
      borrar ni cambiarle el RUT (409); eliminada la cuenta, sí. Una cuenta que ya
      descargó documentos no se elimina (409 «desactívala») y el log dice qué tabla
      lo bloqueó.
  §H  ASESOR: una cuenta portal no es asignable como asesor de una OC, ni al cerrar la
      venta (POST) ni al editarla (PUT) — un solo helper para las dos rutas.
  §I  PERFIL: un valor de perfil desconocido en el puente NO se lee como 'procurement'
      (que ve montos): cae a 'bodega'.
  §J  ESQUEMA A MEDIAS: si falta la tabla del portal, el login de una cuenta portal
      responde 503 con el comando de init_db (no un 500 mudo); el del personal no se
      entera; un error de la base que no es de esquema sigue siendo 500.
  §K  BITÁCORA: crear, restablecer y eliminar quedan en el log del proceso (lo que va a
      `pm2 logs`) con quién lo hizo, qué cuenta y, donde el id no alcanza, el correo;
      la clave temporal jamás.
  §L  SIN REGISTRO DE VIGENCIA (solo por SQL a mano): la cuenta sigue cerrada aunque se
      le restablezca la clave; reactivar recrea el registro y trata su clave como
      temporal (hay que cambiarla).

MUTACIONES verificadas al construir la suite (quitar → rojo → restaurar → verde):
  rechazo del correo existente (→ §B), filtro por tipo en _cuenta_portal (→ §C), guard
  de borrado y de cambio de RUT de la ficha (→ §G), validación de asesor en el POST y
  filtro de tipo en el helper (→ §H), reinicio de vigencia al reactivar (→ §E), tope de
  intentos del cambio de clave y caducidad de la clave temporal (→ §D), el perfil que
  cae a procurement ante un valor desconocido (→ §I), el try/except de `verify_password`
  (→ d3b), el try/except del encode de la clave nueva en `cambiar_clave` (→ d5b), la
  llave del cambio de clave compartida con el login (→ d6c), y el try/except y el filtro
  `esquema_incompleto` de `portal/entrada.py` (→ §J). Además: la configuración del logger
  del portal y los mensajes de la bitácora (→ §K), los dos flush traducidos de
  `crear_cuenta` (→ b7, b8), la forma del correo y fullmatch (→ b9), el plazo de la clave
  temporal en el listado y en reactivar, y reactivar que revive la temporal (→ a8, d2b2,
  d2b4, d2b5), el registro recreado como definitiva (→ §L), el motivo y el try/except del
  409 de eliminar (→ g3b, g3c), `origen` del limitador y `limpiar_exito` (→ d6b2, d7b), y
  el candado del guard de RUT de routers/clientes.py (→ g2b). Todas se repiten con
  `python -m portal.tests.mutaciones cuentas` (portal/tests/mutaciones/login.py y cuentas.py).

Corre con:  ./venv/bin/python -m pytest portal/tests/test_cuentas.py -q
(también:   ./venv/bin/python portal/tests/test_cuentas.py)
"""
import contextlib
import io
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import pymysql  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event  # noqa: E402
from sqlalchemy.exc import OperationalError, ProgrammingError  # noqa: E402
from sqlalchemy.orm import Session as SesionORM  # noqa: E402

import rate_limit_login as rl  # noqa: E402
from auth import verify_password  # noqa: E402
from database import SessionLocal, engine  # noqa: E402
from main import app  # noqa: E402
from models.models import Cliente, Cotizacion, ItemCotizacion, OcCliente, User  # noqa: E402
from portal.models import DESCARGA_GUIA_PDF, PortalAcceso, PortalDescargaLog, UsuarioCliente  # noqa: E402
from portal.deps import DETALLE_CLAVE_TEMPORAL_VENCIDA, DETALLE_ESQUEMA, DETALLE_VENCIDA  # noqa: E402
from portal.service import (  # noqa: E402
    DIAS_INACTIVIDAD_CORTE, DIAS_VIGENCIA_CLAVE_TEMPORAL, LARGO_CLAVE_TEMPORAL,
)
from portal.tests import _datos as D  # noqa: E402

MARK = "ZZPORTALCTAS"  # SIN guiones bajos: en LIKE el _ es comodín
client = TestClient(app)
# Para los casos que esperan un 500 como RESPUESTA (y no como excepción que aborta la corrida).
tolerante = TestClient(app, raise_server_exceptions=False)
_fails: list = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _detalle(r) -> str:
    """El `detail` de una respuesta, o su texto si no es JSON (un 500 del cliente tolerante)."""
    try:
        return str((r.json() or {}).get("detail", ""))
    except ValueError:
        return r.text[:120]


def _login(email: str, clave: str = D.CLAVE):
    return client.post("/api/auth/login", json={"email": email, "password": clave})


def _cab(email: str, clave: str = D.CLAVE) -> dict:
    r = _login(email, clave)
    assert r.status_code == 200, f"login de {email}: {r.status_code} {r.text[:120]}"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _usuario(uid: int) -> User:
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == uid).first()
        db.expunge(u)
        return u
    finally:
        db.close()


def _sembrar() -> dict:
    db = SessionLocal()
    try:
        ficha = D.ficha(db, MARK, "Ficha", 5000033)
        interno = D.interno(db, MARK, "int")
        otra_marca = D.interno(db, MARK, "monza", empresa="automotriz")
        cot = Cotizacion(numero=f"{MARK}-COT", cliente=f"{MARK} Cliente")
        db.add(cot)
        db.flush()
        db.add(ItemCotizacion(cotizacion_id=cot.id, descripcion=f"{MARK} item", cantidad=1.0,
                              estado_item="ingresado"))
        db.commit()
        return {"ficha": ficha.id, "ficha_rut": ficha.rut, "interno": interno.id,
                "email_int": interno.email, "email_monza": otra_marca.email, "cot": cot.id}
    finally:
        db.close()


def run():
    del _fails[:]
    D.limpiar(MARK)
    # Los contadores del limitador viven en la memoria del proceso de pytest, y el de
    # intentos totales no lo borra el éxito: la suite no debe heredar ni dejar marcas.
    rl.reset()
    try:
        ids = _sembrar()
        cab = _cab(ids["email_int"])
        correo = f"{MARK.lower()}-nueva@test.invalid"

        # ══ §A — Alta ═══════════════════════════════════════════════════════════════
        with D.log_del_portal() as bitacora:
            r = client.post("/api/portal-admin/cuentas", headers=cab, json={
                "email": correo, "nombre": f"{MARK} Contacto", "cliente_ids": [ids["ficha"]],
                "perfil": "procurement"})
        check("a1 crear cuenta → 201", r.status_code == 201, r.text[:160])
        alta = r.json() or {}
        temporal = alta.get("clave_temporal") or ""
        uid = alta.get("id")
        # §K: el alta deja escrito el par id-correo y a qué clientes se dio acceso (el id
        # solo no basta para saber, meses después, a quién se le abrió la puerta).
        linea = next((l for l in bitacora.getvalue().splitlines() if "creó la cuenta portal" in l), "")
        check("k2 la bitácora del alta trae el correo creado y la ficha a la que se dio acceso",
              repr(correo) in linea and f"[{ids['ficha']}]" in linea, linea or "(nada en la bitácora)")
        check("a2 devuelve una clave temporal del largo esperado",
              len(temporal) == LARGO_CLAVE_TEMPORAL, len(temporal))
        u = _usuario(uid)
        check("a3 la cuenta nace portal, activa y de la marca mineria",
              (u.tipo_acceso, u.is_active, u.empresa) == ("portal", 1, "mineria"),
              (u.tipo_acceso, u.is_active, u.empresa))
        db = SessionLocal()
        try:
            check("a4 con su puente y su registro de vigencia",
                  db.query(UsuarioCliente).filter_by(usuario_id=uid, cliente_id=ids["ficha"]).count() == 1
                  and db.query(PortalAcceso).filter_by(usuario_id=uid).count() == 1)
        finally:
            db.close()
        r = _login(correo, temporal)
        check("a5 con la clave temporal se entra de verdad, como portal",
              r.status_code == 200 and r.json()["user"].get("tipo_acceso") == "portal", r.text[:120])
        cab_portal = {"Authorization": f"Bearer {r.json()['access_token']}"} if r.status_code == 200 else {}
        r = client.get("/api/portal/yo", headers=cab_portal)
        check("a6 el portal sabe que la cuenta sigue con la clave temporal",
              r.status_code == 200 and r.json().get("debe_cambiar_clave") is True, r.text[:160])
        r = client.get("/api/portal-admin/clientes", headers=cab)
        fila = next((c for c in (r.json() if r.status_code == 200 else []) if c["id"] == ids["ficha"]), {})
        check("a7 el listado de fichas cuenta la cuenta recién creada",
              fila.get("cuentas") == 1 and fila.get("ventas_vinculadas") == 0, fila)
        # El plazo de la clave temporal viaja con la cuenta: en la respuesta del alta (el
        # cuadro que entrega la clave dice cuándo vence) y en el listado del personal.
        r = client.get("/api/portal-admin/cuentas", headers=cab)
        fila = next((c for c in r.json().get("cuentas", []) if c["id"] == uid), {})
        check("a8 el alta y el listado dicen hasta cuándo sirve la clave temporal (y que no venció)",
              bool(alta.get("clave_temporal_vence_at")) and bool(fila.get("clave_temporal_vence_at"))
              and fila.get("clave_temporal_vencida") is False,
              (alta.get("clave_temporal_vence_at"), fila.get("clave_temporal_vence_at"), fila.get("clave_temporal_vencida")))

        # ══ §B — No se convierte una cuenta existente ═══════════════════════════════
        hash_antes = _usuario(ids["interno"]).hashed_password
        r = client.post("/api/portal-admin/cuentas", headers=cab, json={
            "email": ids["email_int"], "nombre": "x", "cliente_ids": [ids["ficha"]]})
        check("b1 el correo de alguien del personal → 409", r.status_code == 409, r.text[:160])
        u = _usuario(ids["interno"])
        check("b2 y esa persona sigue interna y con su clave",
              u.tipo_acceso == "interno" and u.hashed_password == hash_antes, u.tipo_acceso)
        r = client.post("/api/portal-admin/cuentas", headers=cab, json={
            "email": correo, "nombre": "x", "cliente_ids": [ids["ficha"]]})
        check("b3 un correo que ya es cuenta portal → 409", r.status_code == 409, r.status_code)
        base = {"email": f"{MARK.lower()}-otra@test.invalid", "nombre": "x"}
        r = client.post("/api/portal-admin/cuentas", headers=cab, json={**base, "cliente_ids": [999999999]})
        check("b4 cliente inexistente → 404", r.status_code == 404, r.status_code)
        r = client.post("/api/portal-admin/cuentas", headers=cab, json={**base, "cliente_ids": []})
        check("b5 sin clientes → 400", r.status_code == 400, r.status_code)
        r = client.post("/api/portal-admin/cuentas", headers=cab, json={
            **base, "cliente_ids": [ids["ficha"]], "perfil": "gerente"})
        check("b6 perfil fuera de la lista → 400", r.status_code == 400, r.status_code)
        # b7 — la carrera real, determinista: dos altas del mismo correo a la vez. Un
        # oyente before_flush de la sesión, registrado solo durante la petición, hace que
        # OTRA sesión inserte ese correo y confirme justo cuando la petición está por
        # insertarlo (ya pasó el chequeo de existencia). La bandera se pone ANTES de
        # actuar porque el flush de la otra sesión también dispara este oyente.
        correo_carrera = f"{MARK.lower()}-carrera@test.invalid"
        disparado: list = []

        def _otra_alta_se_adelanta(session, flush_context, instances):
            if disparado or not any(isinstance(o, User) and o.email == correo_carrera for o in session.new):
                return
            disparado.append(True)
            otra = SessionLocal()
            try:
                D.interno(otra, MARK, "carrera")
                otra.commit()
            finally:
                otra.close()

        event.listen(SesionORM, "before_flush", _otra_alta_se_adelanta)
        try:
            r = tolerante.post("/api/portal-admin/cuentas", headers=cab, json={
                "email": correo_carrera, "nombre": f"{MARK} Carrera", "cliente_ids": [ids["ficha"]]})
        finally:
            event.remove(SesionORM, "before_flush", _otra_alta_se_adelanta)
        check("b7 (la sonda sirve) la otra alta se adelantó", bool(disparado))
        check("b7 dos altas del mismo correo a la vez → 409 «ya tiene una cuenta», no un 500",
              r.status_code == 409 and "ya tiene una cuenta" in _detalle(r), f"{r.status_code} {_detalle(r)}")
        db = SessionLocal()
        try:
            uids_carrera = [u.id for u in db.query(User).filter(User.email == correo_carrera).all()]
            check("b7b queda exactamente UN usuario con ese correo, y sin puente",
                  len(uids_carrera) == 1
                  and db.query(UsuarioCliente).filter(UsuarioCliente.usuario_id.in_(uids_carrera)).count() == 0,
                  uids_carrera)
        finally:
            db.close()
        # b8 — la otra carrera: la ficha existía al revisarla y alguien la elimina antes
        # del INSERT del puente (la FK de usuario_cliente). Ficha efímera de la suite, sin
        # cuentas ni ventas; el DELETE de la otra sesión no espera porque la petición no
        # tomó ningún candado sobre la ficha (su SELECT es plano).
        db = SessionLocal()
        try:
            id_efimera = D.ficha(db, MARK, "Efimera", 5000055).id
            db.commit()
        finally:
            db.close()
        correo_efimera = f"{MARK.lower()}-efimera@test.invalid"
        del disparado[:]

        def _otro_borra_la_ficha(session, flush_context, instances):
            if disparado or not any(isinstance(o, UsuarioCliente) and o.cliente_id == id_efimera for o in session.new):
                return
            disparado.append(True)
            otra = SessionLocal()
            try:
                otra.query(Cliente).filter(Cliente.id == id_efimera).delete(synchronize_session=False)
                otra.commit()
            finally:
                otra.close()

        event.listen(SesionORM, "before_flush", _otro_borra_la_ficha)
        try:
            r = tolerante.post("/api/portal-admin/cuentas", headers=cab, json={
                "email": correo_efimera, "nombre": f"{MARK} Efimera", "cliente_ids": [id_efimera]})
        finally:
            event.remove(SesionORM, "before_flush", _otro_borra_la_ficha)
        check("b8 (la sonda sirve) la ficha se borró a mitad del alta", bool(disparado))
        check("b8 ficha eliminada a mitad del alta → 409 que NO habla del correo",
              r.status_code == 409 and "ya tiene una cuenta" not in _detalle(r), f"{r.status_code} {_detalle(r)}")
        db = SessionLocal()
        try:
            check("b8b y no queda usuario con ese correo",
                  db.query(User).filter(User.email == correo_efimera).count() == 0)
        finally:
            db.close()
        # b9 — el alta no acepta lo que el campo type=email del login no deja escribir.
        # Todos EMPIEZAN con la marca: si una corrida en rojo los creara, la limpieza por
        # marca los alcanza. a1 es el control positivo.
        m = MARK.lower()
        malos = [f"{m}-nombre <{m}-a@test.invalid>", f"{m}-a@test.invalid; {m}-b@test.invalid",
                 f"{m}-con espacio@test.invalid", f"{m}-punto@test.invalid.", f"{m}-doble@test..invalid"]
        codigos = [client.post("/api/portal-admin/cuentas", headers=cab, json={
            "email": malo, "nombre": "x", "cliente_ids": [ids["ficha"]]}).status_code for malo in malos]
        check("b9 un correo que el login no dejaría escribir → 400", codigos == [400] * len(malos), codigos)
        # ══ §C — La regla dura: el personal no se toca desde acá ════════════════════
        for accion in ("restablecer-clave", "desactivar", "reactivar"):
            r = client.post(f"/api/portal-admin/cuentas/{ids['interno']}/{accion}", headers=cab)
            check(f"c1 {accion} sobre alguien del personal → 404", r.status_code == 404, r.status_code)
        r = client.delete(f"/api/portal-admin/cuentas/{ids['interno']}", headers=cab)
        check("c2 eliminar a alguien del personal → 404", r.status_code == 404, r.status_code)
        u = _usuario(ids["interno"])
        check("c3 y esa persona quedó intacta (activa, misma clave)",
              u.is_active == 1 and u.hashed_password == hash_antes)

        # ══ §D — Claves ══════════════════════════════════════════════════════════════
        with D.log_del_portal() as bitacora:
            r = client.post(f"/api/portal-admin/cuentas/{uid}/restablecer-clave", headers=cab)
        nueva_temporal = (r.json() or {}).get("clave_temporal") or ""
        check("d1 restablecer entrega OTRA clave temporal",
              r.status_code == 200 and nueva_temporal and nueva_temporal != temporal, r.text[:120])
        check("d2 la clave anterior dejó de servir",
              not verify_password(temporal, _usuario(uid).hashed_password))
        # ══ §K — Bitácora: lo que queda en el log del proceso ════════════════════════
        # El logger 'portal' es la única bitácora de quién restableció, reactivó,
        # desactivó o eliminó una cuenta: si sus INFO se descartan, no queda rastro.
        linea = next((l for l in bitacora.getvalue().splitlines() if "restableció la clave" in l), "")
        check("k1 restablecer la clave queda en la bitácora con quién y qué cuenta",
              f"usuario {ids['interno']} restableció la clave de la cuenta {uid}" in linea,
              linea or "(nada en la bitácora)")
        check("k1b la clave temporal JAMÁS entra a la bitácora",
              bool(nueva_temporal) and nueva_temporal not in bitacora.getvalue())
        # La temporal caduca: se envejece la marca y el login la rechaza con su motivo.
        db = SessionLocal()
        try:
            db.query(PortalAcceso).filter(PortalAcceso.usuario_id == uid).update(
                {"clave_temporal_desde": D.ahora() - timedelta(days=DIAS_VIGENCIA_CLAVE_TEMPORAL + 1)})
            db.commit()
        finally:
            db.close()
        r = _login(correo, nueva_temporal)
        check("d2b una clave temporal pasada de plazo → login 403 con el motivo",
              r.status_code == 403 and r.json().get("detail") == DETALLE_CLAVE_TEMPORAL_VENCIDA,
              f"{r.status_code} {r.text[:120]}")
        # El listado tiene que decir lo mismo que el login: la cuenta sigue 'activa' (no
        # es inactividad) pero NO puede entrar, porque su clave temporal venció.
        r = client.get("/api/portal-admin/cuentas", headers=cab)
        fila = next((c for c in r.json().get("cuentas", []) if c["id"] == uid), {})
        check("d2b2 el listado la muestra activa Y con la clave temporal vencida",
              fila.get("estado") == "activa" and fila.get("clave_temporal_vencida") is True,
              (fila.get("estado"), fila.get("clave_temporal_vencida")))
        # El camino del hallazgo: una cuenta vencida por inactividad que además tiene la
        # temporal vencida. Reactivar NO renueva la temporal (reviviría una credencial que
        # viajó por WhatsApp): la respuesta lo avisa y el login sigue cerrado hasta que
        # el personal restablezca la clave.
        db = SessionLocal()
        try:
            viejo = D.ahora() - timedelta(days=DIAS_INACTIVIDAD_CORTE + 5)
            db.query(PortalAcceso).filter(PortalAcceso.usuario_id == uid).update(
                {"vigente_desde": viejo, "ultimo_acceso_at": viejo})
            db.commit()
        finally:
            db.close()
        r = _login(correo, nueva_temporal)
        check("d2b3 vencida por inactividad y con la temporal vencida → el login dice inactividad",
              r.status_code == 403 and r.json().get("detail") == DETALLE_VENCIDA, f"{r.status_code} {r.text[:120]}")
        r = client.post(f"/api/portal-admin/cuentas/{uid}/reactivar", headers=cab)
        check("d2b4 reactivar la deja activa y AVISA que la clave temporal ya venció",
              r.status_code == 200 and r.json().get("estado") == "activa"
              and r.json().get("clave_temporal_vencida") is True, r.text[:160])
        r = _login(correo, nueva_temporal)
        check("d2b5 reactivada, el login sigue cerrado por la temporal vencida (hay que restablecerla)",
              r.status_code == 403 and r.json().get("detail") == DETALLE_CLAVE_TEMPORAL_VENCIDA,
              f"{r.status_code} {r.text[:120]}")
        r = client.post(f"/api/portal-admin/cuentas/{uid}/restablecer-clave", headers=cab)
        nueva_temporal = (r.json() or {}).get("clave_temporal") or ""
        check("d2c restablecer de nuevo entrega una temporal vigente",
              _login(correo, nueva_temporal).status_code == 200)
        cab_portal = _cab(correo, nueva_temporal)
        casos = (
            ("d3 clave actual incorrecta", {"clave_actual": "no-es-esta-clave", "clave_nueva": "una-clave-nueva-2026"}),
            ("d4 clave nueva corta", {"clave_actual": nueva_temporal, "clave_nueva": "corta"}),
            ("d5 clave nueva de más de 72 bytes", {"clave_actual": nueva_temporal, "clave_nueva": "x" * 80}),
            ("d6 clave nueva igual a la actual", {"clave_actual": nueva_temporal, "clave_nueva": nueva_temporal}),
        )
        for nombre, cuerpo in casos:
            r = client.post("/api/portal/yo/clave", headers=cab_portal, json=cuerpo)
            check(f"{nombre} → 400", r.status_code == 400, f"{r.status_code} {r.text[:100]}")
        # Un surrogate suelto en el JSON ("\ud800") no es texto válido: `json=` del cliente
        # no puede codificarlo, así que el cuerpo va CRUDO. Como clave ACTUAL es una clave
        # mala más (400 con el mismo mensaje, no un 500) y gasta un cupo, como d3.
        cab_cruda = {**cab_portal, "Content-Type": "application/json"}
        r = tolerante.post("/api/portal/yo/clave", headers=cab_cruda,
                           content=b'{"clave_actual":"\\ud800","clave_nueva":"una-clave-nueva-2026"}')
        check("d3b clave actual con un surrogate → 400 «La clave actual no es correcta»",
              r.status_code == 400 and r.json().get("detail") == "La clave actual no es correcta",
              f"{r.status_code} {r.text[:100]}")
        # Como clave NUEVA se rechaza antes del limitador (no gasta cupo).
        r = tolerante.post("/api/portal/yo/clave", headers=cab_cruda, content=(
            '{"clave_actual":"' + nueva_temporal + '","clave_nueva":"\\ud800abcdefghijkl"}').encode())
        check("d5b clave nueva con un surrogate → 400", r.status_code == 400,
              f"{r.status_code} {r.text[:100]}")
        # Tope de intentos: mismo cupo que el login. Los 400 de d4-d6 y d5b no gastaron
        # cupo (se rechazan antes del limitador); d3 y d3b gastaron uno cada uno. Con
        # claves actuales malas seguidas, el limitador tiene que cortar con 429 ANTES de
        # llegar al bcrypt.
        salida = io.StringIO()
        with contextlib.redirect_stdout(salida):
            codigos = [
                client.post("/api/portal/yo/clave", headers=cab_portal, json={
                    "clave_actual": f"clave-mala-numero-{i}", "clave_nueva": "una-clave-nueva-2026"}).status_code
                for i in range(rl.MAX_FALLOS_POR_LLAVE + 1)
            ]
        check("d6b claves actuales malas en ráfaga → el limitador corta con 429",
              429 in codigos and codigos[0] == 400, codigos)
        # El cupo es el del login, pero quien lee `pm2 logs` tiene que poder distinguir
        # un cambio de clave de un intento de entrar: la línea lleva su origen.
        lineas = [l for l in salida.getvalue().splitlines() if l.startswith("[login]")]
        check("d6b2 cada línea del limitador dice que el intento vino del cambio de clave (origen=cambio-clave)",
              any("[login] intento" in l for l in lineas) and any("[login] RECHAZADO" in l for l in lineas)
              and all("origen=cambio-clave" in l for l in lineas), lineas[:2] or "(sin líneas [login])")
        # Un contador por cuenta, no dos: con el cupo agotado por el cambio de clave, el
        # LOGIN con la clave CORRECTA también responde 429, y el diagnóstico lo ve en la
        # llave de la cuenta, la misma que escribe el login.
        r = _login(correo, nueva_temporal)
        check("d6c con el cupo agotado por el cambio de clave, el login correcto → 429",
              r.status_code == 429, r.status_code)
        # Con `ip`: desde 2026-10-06 los fallos cuentan por (llave, IP) y el diagnóstico
        # muestra los de la IP que se le indica (TestClient llega como 'testclient').
        r = client.get("/api/auth/rate-limit/estado", params={"email": correo, "ip": "testclient"},
                       headers=cab)
        d = r.json() if r.status_code == 200 else {}
        check("d6c y el diagnóstico lo ve bloqueado en la llave de la cuenta",
              r.status_code == 200 and d.get("bloqueado") is True
              and d.get("llave") == rl.llave_de_cuenta(uid), (r.status_code, d))
        rl.reset()  # el contador vive en memoria del proceso: sin esto d7/d8 heredan el 429
        propia = "clave-propia-del-cliente-2026"
        r = client.post("/api/portal/yo/clave", headers=cab_portal,
                        json={"clave_actual": nueva_temporal, "clave_nueva": propia})
        check("d7 el cliente cambia su clave", r.status_code == 200, r.text[:120])
        # Antes del login de d8, que también limpiaría la marca: el cambio correcto borra
        # su propio fallo reservado (regla 1), igual que un login correcto.
        check("d7b un cambio de clave correcto no deja marca en el contador",
              rl.estado(rl.llave_de_cuenta(uid))["intentos_fallidos"] == 0, rl.estado(rl.llave_de_cuenta(uid)))
        check("d8 y entra con la nueva", _login(correo, propia).status_code == 200)
        r = client.get("/api/portal/yo", headers=_cab(correo, propia))
        check("d9 con clave propia el portal deja de pedir el cambio",
              r.status_code == 200 and r.json().get("debe_cambiar_clave") is False, r.text[:160])
        r = client.get("/api/portal-admin/cuentas", headers=cab)
        fila = next((c for c in r.json().get("cuentas", []) if c["id"] == uid), {})
        check("d9b y el listado deja de mostrar un plazo de clave temporal",
              fila.get("clave_temporal_vence_at") is None and fila.get("clave_temporal_vencida") is False,
              (fila.get("clave_temporal_vence_at"), fila.get("clave_temporal_vencida")))

        # ══ §J — Esquema a medias ════════════════════════════════════════════════════
        # Si falta la tabla del portal (deploy sin `portal.init_db`), el login de una cuenta
        # portal tiene que responder 503 con el comando exacto y no un 500 mudo: el login no
        # pasa por `RutaDelPortal`, así que lo hace `portal/entrada.py`. Sin tocar el esquema:
        # un gancho sobre el motor hace fallar, como lo haría MySQL, toda consulta a
        # portal_acceso; el login del personal no la hace y no se entera.
        def _logins_con_gancho(error_del_driver, clase):
            def gancho(conn, cursor, statement, parameters, context, executemany):
                if "portal_acceso" in statement:
                    raise clase(statement, parameters, error_del_driver)
            event.listen(engine, "before_cursor_execute", gancho)
            try:
                return (tolerante.post("/api/auth/login", json={"email": correo, "password": propia}),
                        tolerante.post("/api/auth/login", json={"email": ids["email_int"], "password": D.CLAVE}))
            finally:
                event.remove(engine, "before_cursor_execute", gancho)

        r_portal, r_interno = _logins_con_gancho(
            pymysql.err.ProgrammingError(1146, "no existe portal_acceso"), ProgrammingError)
        check("j1 sin la tabla del portal, el login de la cuenta portal → 503 con el comando de init_db",
              r_portal.status_code == 503 and r_portal.json().get("detail") == DETALLE_ESQUEMA,
              f"{r_portal.status_code} {r_portal.text[:120]}")
        check("j2 y el login del personal no se entera → 200", r_interno.status_code == 200,
              f"{r_interno.status_code} {r_interno.text[:120]}")
        r_portal, _ = _logins_con_gancho(
            pymysql.err.OperationalError(1205, "Lock wait timeout exceeded"), OperationalError)
        check("j3 un error de la base que NO es de esquema sigue siendo 500 (no se anuncia como «portal no instalado»)",
              r_portal.status_code == 500, f"{r_portal.status_code} {r_portal.text[:120]}")

        # ══ §E — Ciclo de vida ═══════════════════════════════════════════════════════
        r = client.post(f"/api/portal-admin/cuentas/{uid}/desactivar", headers=cab)
        check("e1 desactivar → estado desactivada",
              r.status_code == 200 and r.json().get("estado") == "desactivada", r.text[:120])
        check("e2 una cuenta desactivada no entra", _login(correo, propia).status_code == 401)
        db = SessionLocal()
        try:
            viejo = D.ahora() - timedelta(days=DIAS_INACTIVIDAD_CORTE + 5)
            db.query(PortalAcceso).filter(PortalAcceso.usuario_id == uid).update(
                {"vigente_desde": viejo, "ultimo_acceso_at": viejo})
            db.commit()
        finally:
            db.close()
        r = client.get("/api/portal-admin/cuentas", headers=cab)
        fila = next((c for c in r.json().get("cuentas", []) if c["id"] == uid), {})
        check("e3 el listado dice 'desactivada' (manda sobre vencida)",
              fila.get("estado") == "desactivada", fila.get("estado"))
        r = client.post(f"/api/portal-admin/cuentas/{uid}/reactivar", headers=cab)
        check("e4 reactivar una cuenta vencida la deja ACTIVA (el plazo vuelve a correr)",
              r.status_code == 200 and r.json().get("estado") == "activa", r.text[:120])
        check("e5 y vuelve a entrar", _login(correo, propia).status_code == 200)

        # ══ §I — Perfil: ante un valor desconocido, el más restrictivo ═══════════════
        db = SessionLocal()
        try:
            db.query(UsuarioCliente).filter(UsuarioCliente.usuario_id == uid).update({"perfil": "gerente"})
            db.commit()
        finally:
            db.close()
        r = client.get("/api/portal/yo", headers=_cab(correo, propia))
        check("i1 un perfil desconocido en el puente NO se lee como procurement (ve montos)",
              r.status_code == 200 and r.json().get("perfil") == "bodega", r.text[:160])

        # ══ §L — Sin registro de vigencia ════════════════════════════════════════════
        # Un estado que el módulo nunca produce (solo SQL a mano): ante él se cierra, y al
        # reactivar se recrea el registro tratando la clave que tenga como TEMPORAL (no
        # se sabe de dónde salió). Deja la cuenta con clave temporal: ninguna sección
        # posterior vuelve a entrar con ella.
        db = SessionLocal()
        try:
            db.query(PortalAcceso).filter(PortalAcceso.usuario_id == uid).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()
        r = client.post(f"/api/portal-admin/cuentas/{uid}/restablecer-clave", headers=cab)
        temporal_l = (r.json() or {}).get("clave_temporal") or ""
        codigo_login = _login(correo, temporal_l).status_code
        check("l1 sin registro, restablecer entrega una temporal pero la cuenta SIGUE cerrada (login 403)",
              r.status_code == 200 and bool(temporal_l) and codigo_login == 403, f"{r.status_code} {codigo_login}")
        r = client.post(f"/api/portal-admin/cuentas/{uid}/reactivar", headers=cab)
        check("l2 reactivar recrea el registro y trata la clave como temporal (trae su vencimiento)",
              r.status_code == 200 and bool((r.json() or {}).get("clave_temporal_vence_at")), r.text[:160])
        r = _login(correo, temporal_l)
        cab_l = {"Authorization": f"Bearer {r.json()['access_token']}"} if r.status_code == 200 else {}
        r_yo = client.get("/api/portal/yo", headers=cab_l)
        check("l3 con esa temporal entra y el portal le exige cambiarla",
              r.status_code == 200 and r_yo.status_code == 200 and r_yo.json().get("debe_cambiar_clave") is True,
              f"{r.status_code} {r_yo.status_code} {r_yo.text[:120]}")

        # ══ §F — Quién opera ═════════════════════════════════════════════════════════
        r = client.get("/api/portal-admin/cuentas", headers=_cab(ids["email_monza"]))
        check("f1 el personal de la otra marca → 403", r.status_code == 403, r.status_code)

        # ══ §H — Asesor (antes de §G, que borra la cuenta) ═══════════════════════════
        r = client.post("/api/compras/oc-cliente", headers=cab, json={
            "cotizacion_id": ids["cot"], "numero_oc": f"{MARK}-OC", "asesor_id": uid})
        check("h1 cerrar la venta con una cuenta portal como asesor → 400",
              r.status_code == 400, f"{r.status_code} {r.text[:120]}")
        db = SessionLocal()
        try:
            check("h2 y la venta NO quedó cerrada",
                  db.query(OcCliente).filter(OcCliente.cotizacion_id == ids["cot"]).count() == 0)
        finally:
            db.close()
        r = client.post("/api/compras/oc-cliente", headers=cab, json={
            "cotizacion_id": ids["cot"], "numero_oc": f"{MARK}-OC", "asesor_id": ids["interno"]})
        check("h3 con alguien del personal como asesor → 201", r.status_code == 201, r.text[:120])
        oc_id = (r.json() or {}).get("id")
        r = client.put(f"/api/compras/oc-cliente/{oc_id}", headers=cab, json={"asesor_id": uid})
        check("h4 editar la OC poniendo una cuenta portal de asesor → 400",
              r.status_code == 400, f"{r.status_code} {r.text[:120]}")

        # ══ §G — La ficha es el ancla ════════════════════════════════════════════════
        r = client.delete(f"/api/clientes/{ids['ficha']}", headers=cab)
        check("g1 borrar una ficha con cuentas del portal → 409", r.status_code == 409,
              f"{r.status_code} {r.text[:120]}")
        sentencias: list = []

        def _anotar(conn, cursor, statement, parameters, context, executemany):
            sentencias.append(statement)

        event.listen(engine, "before_cursor_execute", _anotar)
        try:
            r = client.put(f"/api/clientes/{ids['ficha']}", headers=cab,
                           json={"rut": D.rut_de_prueba(5000044)})
        finally:
            event.remove(engine, "before_cursor_execute", _anotar)
        check("g2 cambiarle el RUT a una ficha con cuentas → 409", r.status_code == 409,
              f"{r.status_code} {r.text[:120]}")
        # El guard decide con la ficha TOMADA: un alta de cuenta en vuelo (su fila de
        # usuario_cliente todavía sin confirmar) hace esperar al FOR UPDATE, y recién
        # entonces se mira si la ficha tiene cuentas. Sin candado, el guard no ve esa fila
        # y le cambia el RUT a una ficha que ya tiene cuenta.
        con_candado = next((i for i, s in enumerate(sentencias) if "FROM clientes" in s and "FOR UPDATE" in s), None)
        mira_cuentas = next((i for i, s in enumerate(sentencias) if "FROM usuario_cliente" in s), None)
        check("g2b el guard toma la ficha con candado ANTES de mirar si tiene cuentas",
              con_candado is not None and mira_cuentas is not None and con_candado < mira_cuentas,
              (con_candado, mira_cuentas))
        r = client.put(f"/api/clientes/{ids['ficha']}", headers=cab,
                       json={"rut": ids["ficha_rut"].replace(".", ""), "ciudad": "Calama"})
        check("g3 el MISMO RUT escrito sin puntos no es un cambio de RUT → 200",
              r.status_code == 200, f"{r.status_code} {r.text[:120]}")
        # g3b — una cuenta que ya descargó un documento no se puede borrar: la FK de
        # portal_descarga_log (sin ON DELETE) conserva su bitácora. El 409 dice qué hacer
        # y el log del proceso, qué tabla lo bloqueó. Es la primera sonda de ese 409.
        db = SessionLocal()
        try:
            db.add(PortalDescargaLog(usuario_id=uid, tipo=DESCARGA_GUIA_PDF, referencia_id=1, created_at=D.ahora()))
            db.commit()
        finally:
            db.close()
        with D.log_del_portal() as bitacora:
            r = tolerante.delete(f"/api/portal-admin/cuentas/{uid}", headers=cab)
        db = SessionLocal()
        try:
            entera = (db.query(User).filter(User.id == uid).count() == 1
                      and db.query(UsuarioCliente).filter_by(usuario_id=uid).count() == 1)
            db.query(PortalDescargaLog).filter(PortalDescargaLog.usuario_id == uid).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()
        check("g3b una cuenta con descargas no se borra → 409 «desactívala», y sigue entera con su puente",
              r.status_code == 409 and "desactívala" in _detalle(r) and entera, (r.status_code, _detalle(r), entera))
        check("g3c la bitácora nombra la tabla que bloqueó el borrado",
              "portal_descarga_log" in bitacora.getvalue(), bitacora.getvalue()[-200:] or "(nada en la bitácora)")
        with D.log_del_portal() as bitacora:
            r = client.delete(f"/api/portal-admin/cuentas/{uid}", headers=cab)
        check("g4 eliminar la cuenta → 204", r.status_code == 204, r.status_code)
        # §K: después del borrado no queda fila que consultar, así que la bitácora tiene
        # que decir el CORREO de la cuenta y no solo su id.
        linea = next((l for l in bitacora.getvalue().splitlines() if "eliminó la cuenta portal" in l), "")
        check("k3 la bitácora del borrado conserva el correo de la cuenta borrada",
              repr(correo) in linea, linea or "(nada en la bitácora)")
        db = SessionLocal()
        try:
            check("g5 no quedó rastro de la cuenta (usuario, puente, vigencia)",
                  db.query(User).filter(User.id == uid).count() == 0
                  and db.query(UsuarioCliente).filter_by(usuario_id=uid).count() == 0
                  and db.query(PortalAcceso).filter_by(usuario_id=uid).count() == 0)
        finally:
            db.close()
        r = client.delete(f"/api/clientes/{ids['ficha']}", headers=cab)
        check("g6 sin cuentas, la ficha se borra como siempre → 204", r.status_code == 204, r.status_code)

        print()
        print(f"FALLARON {len(_fails)}: {_fails}" if _fails else "SUITE VERDE: cuentas del portal")
        assert not _fails, _fails
    finally:
        D.limpiar(MARK)
        rl.reset()


def test_cuentas_portal():
    run()


if __name__ == "__main__":
    run()
