"""VÍNCULO venta ↔ cliente (cotizaciones.cliente_id) y vigencia de la cuenta.

LO QUE ESTA SUITE PROTEGE
  §A  PARIDAD de los helpers de RUT del portal con los de routers/contabilidad.py en
      todo RUT escrito normalmente (si derivan, el portal vincularía distinto de como
      factura el ERP), y los TRES bordes donde el portal se aparta a propósito: los ceros
      a la izquierda no cambian de quién es el RUT, un "dígito" que no es ASCII no
      revienta el cálculo (en Contabilidad, '²' lanza ValueError), y la tipografía de un
      RUT pegado desde Word o un PDF (guion largo, espacio no separable, invisibles) se
      tolera. Más el oráculo del generador de RUT de prueba (rut_con_dv), con RUT fijos.
  §B  cuenta_vencida: sin registro → vencida (cierra ante lo desconocido); el plazo corre
      desde lo MÁS RECIENTE entre el último acceso y vigente_desde.
  §C  mapa_clientes: una ficha con RUT mal formado no ancla nada, y dos fichas con el
      mismo RUT escrito distinto quedan AMBIGUAS y fuera del mapa.
  §D  EL GANCHO (portal/sincronia.py) — la sonda de la fuga: una venta nace vinculada por
      su RUT aunque venga sin puntos o con k minúscula; si el RUT se corrige al de OTRO
      cliente la venta se MUEVE; si se corrige a un RUT sin ficha queda en NULL. Nunca
      queda colgando de la ficha del RUT anterior.
  §E  El Cierre de Venta vincula la venta cuya ficha se creó después de cotizar.
  §F  Crear la ficha desde la pantalla Clientes vincula las ventas que ya existían, y
      la ficha y sus vínculos se guardan en UNA transacción: si la resincronización
      falla, el cambio de RUT de la ficha NO queda aplicado.
  §G  vincular_historico y sus dos rutas (/portal-admin/vinculacion): el ensayo no
      escribe aunque después se haga commit; aplicar vincula, DESVINCULA lo que dejó de
      calzar y reporta como huérfanas solo las ventas CERRADAS, con su motivo.
  §H  Borrar una ficha desvincula sus ventas (ON DELETE SET NULL), no las bloquea; y
      borrar la ficha DUPLICADA de un RUT ambiguo le devuelve las ventas a la que queda.
  §J  EL MAPA PUEDE ESTAR VIEJO: las tres puertas que escriben cotizaciones.cliente_id
      (gancho, Cierre, histórico) confirman la ficha releyéndola con candado compartido;
      si otra persona le cambió el RUT entre el mapa y la escritura, la venta NO se
      cuelga de ella (j1–j3), y la confirmación ESPERA a quien edita la ficha (j4).
  §K  DAR ACCESO PONE AL DÍA EL VÍNCULO: crear una cuenta resincroniza todas las ventas.

MUTACIONES verificadas al construir la suite (quitar → rojo → restaurar → verde):
  gancho de sesión apagado y gancho que ignora los cambios de RUT (→ §D), histórico que
  solo llena lo vacío (→ §G desvincular), vínculo en el Cierre (→ §E), resincronización
  al crear la ficha (→ §F), ambiguos y fichas mal formadas dentro del mapa (→ §C), el
  plazo de vigencia mirando solo el último acceso (→ §B), y la validación de la forma
  del RUT de la venta (→ d8: d6 sola NO la discrimina, porque la ficha de un RUT mal
  formado tampoco está en el mapa; hace falta un RUT inválido cuyo canónico calce con
  una ficha válida — el guion corrido), el ensayo que escribe (→ g1, que hace COMMIT
  después del ensayo: con rollback no discriminaba), la ficha guardada antes de
  resincronizar (→ f4), el borrado que no resincroniza (→ h5), los ceros a la izquierda
  (→ d9) y el isascii del RUT (→ §A). Además: el factor del módulo 11 en
  digito_verificador (→ oráculo de §A), el saneado que solo quita punto y espacio (→
  variantes de §A y d10), la ficha sin confirmar en cada puerta y el gancho que confirma
  solo al cambiar de ficha (→ §J), la lectura sin candado (→ j4), el alta que no
  resincroniza (→ §K) y la columna omitida en la consulta del histórico (→ g4). Se
  repiten con `python -m portal.tests.mutaciones vinculacion` (portal/tests/mutaciones/cuentas.py).

Corre con:  ./venv/bin/python -m pytest portal/tests/test_vinculacion.py -q
(también:   ./venv/bin/python portal/tests/test_vinculacion.py)
"""
import os
import sys
from datetime import timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

import routers.contabilidad as cont  # noqa: E402
from database import SessionLocal, engine  # noqa: E402
from main import app  # noqa: E402
from models.models import Cliente, Cotizacion, ItemCotizacion  # noqa: E402
from portal import rut as R  # noqa: E402
from portal import service as S  # noqa: E402
from portal.tests import _datos as D  # noqa: E402

MARK = "ZZPORTALVINC"  # SIN guiones bajos: en LIKE el _ es comodín
client = TestClient(app)
_fails: list = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _rut_con_dv_k(desde: int) -> str:
    """Un RUT de prueba cuyo dígito verificador es K (para probar la k minúscula)."""
    cuerpo = desde
    while not D.rut_de_prueba(cuerpo).endswith("-K"):
        cuerpo += 1
    return D.rut_de_prueba(cuerpo)


def _cliente_de(cot_id: int):
    db = SessionLocal()
    try:
        return db.query(Cotizacion.cliente_id).filter(Cotizacion.id == cot_id).scalar()
    finally:
        db.close()


def _nueva_cot(sufijo: str, rut) -> int:
    db = SessionLocal()
    try:
        cot = Cotizacion(numero=f"{MARK}-{sufijo}", cliente=f"{MARK} Cliente", rut_cliente=rut)
        db.add(cot)
        db.flush()
        db.add(ItemCotizacion(cotizacion_id=cot.id, descripcion=f"{MARK} item", cantidad=1.0,
                              estado_item="ingresado"))
        db.commit()
        return cot.id
    finally:
        db.close()


def _paso_a_paridad_de_rut():
    # Son el oráculo del módulo 11: RUT FIJOS, no generados. El generador de RUT de
    # prueba (rut_con_dv) y el validador comparten digito_verificador, así que un RUT
    # generado validaría igual aunque el cálculo estuviera mal.
    muestras = [
        None, "", " ", "76.874.971-K", "76874971-k", "76874971K", "78.279.030-7", "782790307",
        "78.279.030-8", "1-9", "abc", "076.874.971-K", " 76.874.971 - K ", "11.111.111-1", "0000000-0",
    ]
    for cuerpo, esperado in (("76874971", "76.874.971-K"), ("78279030", "78.279.030-7"),
                             ("11111111", "11.111.111-1")):
        check(f"a· el generador de RUT de prueba calcula bien el dígito de {cuerpo}",
              R.rut_con_dv(cuerpo) == esperado, R.rut_con_dv(cuerpo))
    for m in muestras:
        con_ceros = cont._rut_canonico(m).startswith("0")
        iguales = (
            S.rut_saneado(m) == cont._rut_saneado(m)
            and S.rut_valido(m) == cont._rut_valido(m)
            and (con_ceros or S.rut_canonico(m) == cont._rut_canonico(m))
        )
        check(f"a· paridad con contabilidad para {m!r}", iguales,
              (S.rut_canonico(m), cont._rut_canonico(m), S.rut_valido(m), cont._rut_valido(m)))
    # Los dos bordes donde el portal se aparta a propósito.
    check("a· los ceros a la izquierda no cambian el canónico",
          S.rut_canonico("076.874.971-K") == S.rut_canonico("76.874.971-K") == "76874971K",
          S.rut_canonico("076.874.971-K"))
    try:
        cont._rut_valido("7687497²-K")
        contabilidad_revienta = False
    except ValueError:
        contabilidad_revienta = True
    check("a· (la sonda sirve) con '²' el helper de Contabilidad lanza ValueError", contabilidad_revienta)
    try:
        resultado = S.rut_valido("7687497²-K")
    except ValueError:
        resultado = "REVENTÓ"
    check("a· el del portal responde False sin reventar", resultado is False, resultado)
    check("a· dígitos de otro alfabeto no son un RUT válido para el portal",
          S.rut_valido("７６８７４９７１-K") is False)
    # Tercer borde: la tipografía de un RUT pegado desde Word, un PDF o una web. Sin
    # tolerarla, la venta cerrada queda fuera del portal en silencio con el RUT
    # viéndose correcto en pantalla. Todo va con escapes: escritos tal cual no se
    # distinguen de un guion o un espacio comunes en el editor.
    variantes = {
        "guion largo U+2013": "76.874.971–K",
        "guion de no separación U+2011": "76.874.971‑K",
        "signo menos U+2212": "76.874.971−K",
        "espacio no separable U+00A0 entre grupos": "76. 874. 971-K",
        "espacio fino U+2009 entre grupos": "76 874 971-K",
        "tabulador interno": "76.874\t.971-K",
        "ancho cero U+200B": "76.874.971​-K",
        "marca de orden de bytes U+FEFF al inicio": "﻿76.874.971-K",
        "guion suave U+00AD": "76.874.9­71-K",
    }
    for nombre, variante in variantes.items():
        check(f"a· tolera {nombre}: válido y con el mismo canónico",
              S.rut_valido(variante) and S.rut_canonico(variante) == "76874971K",
              (S.rut_valido(variante), S.rut_canonico(variante)))
    check("a· el guion largo no perdona un dígito verificador malo",
          S.rut_valido("76.874.971–5") is False)


def _paso_b_vigencia():
    ahora = D.ahora()
    dias = S.DIAS_INACTIVIDAD_CORTE

    def acc(vigente, ultimo):
        return SimpleNamespace(
            vigente_desde=ahora - timedelta(days=vigente),
            ultimo_acceso_at=None if ultimo is None else ahora - timedelta(days=ultimo))

    check("b1 sin registro de vigencia → vencida (cierra ante lo desconocido)", S.cuenta_vencida(None, ahora))
    check("b2 recién creada, nunca usada → vigente", not S.cuenta_vencida(acc(1, None), ahora))
    check("b3 creada hace mucho y nunca usada → vencida", S.cuenta_vencida(acc(dias + 1, None), ahora))
    check("b4 vieja pero usada ayer → vigente", not S.cuenta_vencida(acc(dias * 3, 1), ahora))
    check("b5 último uso fuera de plazo → vencida", S.cuenta_vencida(acc(dias * 3, dias + 1), ahora))
    check("b6 reactivada hoy aunque su último uso sea viejísimo → vigente",
          not S.cuenta_vencida(acc(0, dias * 5), ahora))
    check("b7 justo en el borde (día 60) → todavía vigente", not S.cuenta_vencida(acc(dias, None), ahora))


def run():
    del _fails[:]
    D.limpiar(MARK)
    try:
        _paso_a_paridad_de_rut()
        _paso_b_vigencia()

        rut_a = _rut_con_dv_k(5000100)          # termina en -K
        rut_b = D.rut_de_prueba(5000201)
        rut_dup = D.rut_de_prueba(5000302)
        rut_libre = D.rut_de_prueba(5000403)     # sin ficha
        rut_tardio = D.rut_de_prueba(5000504)    # su ficha se crea después
        db = SessionLocal()
        try:
            fa = Cliente(rut=rut_a, nombre=f"{MARK} A", giro=MARK)
            fb = Cliente(rut=rut_b, nombre=f"{MARK} B", giro=MARK)
            dup1 = Cliente(rut=rut_dup, nombre=f"{MARK} Dup con puntos", giro=MARK)
            dup2 = Cliente(rut=rut_dup.replace(".", ""), nombre=f"{MARK} Dup sin puntos", giro=MARK)
            mala = Cliente(rut=rut_b[:-1] + ("1" if rut_b[-1] != "1" else "2"),
                           nombre=f"{MARK} RUT malo", giro=MARK)
            interno = D.interno(db, MARK, "int")
            db.add_all([fa, fb, dup1, dup2, mala])
            db.commit()
            id_a, id_b, ids_dup, id_mala = fa.id, fb.id, sorted([dup1.id, dup2.id]), mala.id
            email_int = interno.email
            rut_malo = mala.rut

            # ══ §C — el mapa ═════════════════════════════════════════════════════════
            mapa, ambiguos = S.mapa_clientes(db)
            check("c1 la ficha válida está en el mapa", mapa.get(S.rut_canonico(rut_a)) == id_a)
            check("c2 la ficha con RUT mal formado NO ancla nada", id_mala not in mapa.values())
            check("c3 el RUT con dos fichas queda fuera del mapa",
                  S.rut_canonico(rut_dup) not in mapa, mapa.get(S.rut_canonico(rut_dup)))
            check("c4 y se reporta como ambiguo con sus dos fichas",
                  ambiguos.get(S.rut_canonico(rut_dup)) == ids_dup, ambiguos)
        finally:
            db.close()

        r = client.post("/api/auth/login", json={"email": email_int, "password": D.CLAVE})
        cab = {"Authorization": f"Bearer {r.json()['access_token']}"}

        # ══ §D — el gancho: la sonda de la fuga ══════════════════════════════════════
        sin_puntos_k_minuscula = rut_a.replace(".", "").lower()
        cot = _nueva_cot("D", sin_puntos_k_minuscula)
        check("d1 la venta nace vinculada aunque el RUT venga sin puntos y con k minúscula",
              _cliente_de(cot) == id_a, _cliente_de(cot))
        db = SessionLocal()
        try:
            db.query(Cotizacion).filter(Cotizacion.id == cot).first().rut_cliente = rut_b
            db.commit()
        finally:
            db.close()
        check("d2 corregir el RUT al de OTRO cliente MUEVE la venta (no queda en el anterior)",
              _cliente_de(cot) == id_b, _cliente_de(cot))
        db = SessionLocal()
        try:
            db.query(Cotizacion).filter(Cotizacion.id == cot).first().rut_cliente = rut_libre
            db.commit()
        finally:
            db.close()
        check("d3 corregirlo a un RUT sin ficha la DESVINCULA", _cliente_de(cot) is None, _cliente_de(cot))
        check("d4 una venta sin RUT nace sin vincular", _cliente_de(_nueva_cot("D2", None)) is None)
        check("d5 con el RUT ambiguo nace sin vincular", _cliente_de(_nueva_cot("D3", rut_dup)) is None)
        check("d6 con un RUT mal formado nace sin vincular", _cliente_de(_nueva_cot("D4", rut_malo)) is None)
        # El caso que SÍ discrimina la validación: el guion en el lugar equivocado deja un
        # RUT mal formado cuyo canónico (sin puntos ni guion) es idéntico al de la ficha A.
        # Sin validar la forma, esta venta se colgaría de A.
        canon_a = S.rut_canonico(rut_a)
        guion_corrido = f"{canon_a[:-2]}-{canon_a[-2:]}"
        check("d7 (la sonda sirve) el RUT con guion corrido es inválido pero canoniza igual que A",
              not S.rut_valido(guion_corrido) and S.rut_canonico(guion_corrido) == canon_a, guion_corrido)
        check("d8 un RUT mal formado NO se vincula aunque su canónico calce con una ficha",
              _cliente_de(_nueva_cot("D5", guion_corrido)) is None)
        check("d9 el mismo RUT con un cero a la izquierda SÍ se vincula a su ficha",
              _cliente_de(_nueva_cot("D6", "0" + rut_b)) == id_b)
        # El efecto real del tercer borde de §A, por el gancho: el RUT de B pegado desde
        # Word (guion largo U+2013 y espacio no separable U+00A0 en vez de los puntos).
        rut_b_pegado = rut_b.replace(".", " ").replace("-", "–")
        cot_pegado = _nueva_cot("D7", rut_b_pegado)
        check("d10 el RUT con guion largo y espacio no separable nace vinculado a su ficha",
              _cliente_de(cot_pegado) == id_b, _cliente_de(cot_pegado))

        # ══ §E — el Cierre vincula la ficha creada después ═══════════════════════════
        cot_e = _nueva_cot("E", rut_tardio)
        check("e0 sin ficha todavía, la venta está sin vincular", _cliente_de(cot_e) is None)
        db = SessionLocal()
        try:
            tardia = Cliente(rut=rut_tardio, nombre=f"{MARK} Tardía", giro=MARK)
            db.add(tardia)   # directo a la base: NO pasa por la pantalla Clientes
            db.commit()
            id_tardia = tardia.id
        finally:
            db.close()
        check("e1 crear la ficha por fuera de la pantalla no vincula nada solo", _cliente_de(cot_e) is None)
        r = client.post("/api/compras/oc-cliente", headers=cab,
                        json={"cotizacion_id": cot_e, "numero_oc": f"{MARK}-OC-E"})
        check("e2 el Cierre de Venta responde 201", r.status_code == 201, r.text[:120])
        check("e3 y deja la venta vinculada a su ficha", _cliente_de(cot_e) == id_tardia, _cliente_de(cot_e))

        # ══ §F — crear la ficha desde la pantalla Clientes vincula lo que ya existía ═
        rut_f = D.rut_de_prueba(5000605)
        cot_f = _nueva_cot("F", rut_f)
        r = client.post("/api/clientes/upsert", headers=cab,
                        json={"rut": rut_f, "nombre": f"{MARK} Desde pantalla", "giro": MARK})
        check("f1 la pantalla Clientes crea la ficha", r.status_code == 200, r.text[:120])
        id_f = (r.json() or {}).get("id")
        check("f2 y la venta que ya existía queda vinculada", _cliente_de(cot_f) == id_f, _cliente_de(cot_f))
        # Atomicidad: si la resincronización falla, el cambio de RUT de la ficha NO puede
        # quedar guardado (con dos commits quedaba aplicado y las ventas del RUT viejo
        # seguían colgando de la ficha).
        original = S.vincular_historico

        def _revienta(db, aplicar):
            raise RuntimeError("fallo inyectado en la resincronización")

        S.vincular_historico = _revienta
        try:
            try:
                client.put(f"/api/clientes/{id_f}", headers=cab, json={"rut": D.rut_de_prueba(5000706)})
                fallo = False
            except RuntimeError:
                fallo = True
        finally:
            S.vincular_historico = original
        db = SessionLocal()
        try:
            rut_guardado = db.query(Cliente.rut).filter(Cliente.id == id_f).scalar()
        finally:
            db.close()
        check("f3 (la sonda sirve) el fallo inyectado llegó a ocurrir", fallo)
        check("f4 con la resincronización caída, el cambio de RUT de la ficha NO quedó guardado",
              rut_guardado == rut_f, rut_guardado)
        check("f5 y su venta sigue vinculada a ella", _cliente_de(cot_f) == id_f, _cliente_de(cot_f))

        # ══ §G — vincular_historico ══════════════════════════════════════════════════
        cot_g = _nueva_cot("G", rut_libre)            # cerrada, sin ficha → huérfana
        client.post("/api/compras/oc-cliente", headers=cab,
                    json={"cotizacion_id": cot_g, "numero_oc": f"{MARK}-OC-G"})
        cot_borrador = _nueva_cot("G2", rut_libre)    # borrador sin ficha → NO es huérfana
        db = SessionLocal()
        try:
            # Vínculo OBSOLETO fabricado con un UPDATE masivo (que el gancho no ve):
            # la venta de rut_libre queda colgando de la ficha A.
            db.query(Cotizacion).filter(Cotizacion.id == cot_g).update({"cliente_id": id_a})
            db.commit()
            ensayo = S.vincular_historico(db, aplicar=False)
            # COMMIT a propósito, no rollback: si el ensayo hubiera tocado algo, acá
            # quedaría escrito y g1 lo ve. (Con rollback la sonda pasaba igual.)
            db.commit()
        finally:
            db.close()
        check("g1 el ensayo NO escribe ni aunque se haga commit después", _cliente_de(cot_g) == id_a)
        check("g2 el ensayo anuncia que hay algo por desvincular", ensayo["desvinculadas_ahora"] >= 1, ensayo)
        r = client.get("/api/portal-admin/vinculacion", headers=cab)
        check("g2b la ruta del ensayo responde lo mismo y tampoco escribe",
              r.status_code == 200 and r.json().get("aplicado") is False
              and r.json().get("desvinculadas_ahora", 0) >= 1 and _cliente_de(cot_g) == id_a, r.text[:160])
        r = client.post("/api/portal-admin/vinculacion/aplicar", headers=cab)
        check("g2c la ruta de aplicar responde 200 y dice que escribió",
              r.status_code == 200 and r.json().get("aplicado") is True, r.text[:160])
        aplicado = r.json() if r.status_code == 200 else {"huerfanas": [], "ruts_ambiguos": []}
        check("g3 aplicar DESVINCULA la venta cuyo RUT no calza con su ficha",
              _cliente_de(cot_g) is None, _cliente_de(cot_g))
        huerfanas = {h["cotizacion_id"]: h["motivo"] for h in aplicado["huerfanas"]}
        check("g4 la venta cerrada sin ficha sale como huérfana, con su motivo",
              huerfanas.get(cot_g) == S.MOTIVO_SIN_FICHA, huerfanas.get(cot_g))
        check("g5 el borrador sin ficha NO sale en el reporte", cot_borrador not in huerfanas)
        check("g6 el reporte nombra el RUT ambiguo",
              any(a["rut"] == S.rut_canonico(rut_dup) for a in aplicado["ruts_ambiguos"]))
        check("g7 lo que ya estaba bien vinculado no se toca", _cliente_de(cot_e) == id_tardia)

        # ══ §H — borrar la ficha desvincula ══════════════════════════════════════════
        r = client.delete(f"/api/clientes/{id_tardia}", headers=cab)
        check("h1 una ficha con ventas (y sin cuentas) se borra → 204", r.status_code == 204,
              f"{r.status_code} {r.text[:120]}")
        check("h2 y su venta queda sin vincular (desaparece del portal)", _cliente_de(cot_e) is None)
        cot_dup = _nueva_cot("H", rut_dup)
        check("h3 con el RUT ambiguo la venta está sin vincular", _cliente_de(cot_dup) is None)
        r = client.delete(f"/api/clientes/{ids_dup[1]}", headers=cab)
        check("h4 se borra la ficha duplicada → 204", r.status_code == 204, f"{r.status_code} {r.text[:120]}")
        check("h5 y la venta queda vinculada a la ficha que quedó (sin apretar nada más)",
              _cliente_de(cot_dup) == ids_dup[0], _cliente_de(cot_dup))

        # ══ §J — El mapa puede estar viejo ════════════════════════════════════════════
        # Las tres puertas que escriben cotizaciones.cliente_id (el gancho, el Cierre y
        # el histórico) deciden el destino con un mapa leído sin candado. Acá otra persona
        # cambia el RUT de la ficha J (otra sesión, commit) JUSTO después de que se leyó
        # el mapa: la venta no puede quedar colgada de una ficha que ya no es la de su
        # RUT, porque candado.py filtra solo por cliente_id y sus guías, facturas y fotos
        # quedarían a la vista de otro contribuyente. Una sonda por puerta, sin hilos.
        rut_j, rut_j_otro = D.rut_de_prueba(5000807), D.rut_de_prueba(5000908)
        db = SessionLocal()
        try:
            id_j = D.ficha(db, MARK, "J", 5000807).id
            db.commit()
        finally:
            db.close()

        def _poner_rut(cliente_id: int, rut: str) -> None:
            """Lo que haría otra persona guardando la ficha en ese instante (sin pasar por
            la pantalla Clientes: se quiere el cambio, no la resincronización)."""
            otra = SessionLocal()
            try:
                otra.query(Cliente).filter(Cliente.id == cliente_id).update({"rut": rut})
                otra.commit()
            finally:
                otra.close()

        def _venta_sin_vincular(sufijo: str) -> int:
            """Una venta con el RUT de J que NO está vinculada: nace mientras J tiene otro
            RUT y después J vuelve al suyo por fuera de la pantalla."""
            _poner_rut(id_j, rut_j_otro)
            cot_id = _nueva_cot(sufijo, rut_j)
            _poner_rut(id_j, rut_j)
            assert _cliente_de(cot_id) is None, "la venta tendría que haber nacido sin vincular"
            return cot_id

        trampa = {"armada": False}
        mapa_original = S.mapa_clientes

        def _mapa_y_otro_cambia_el_rut(db):
            resultado = mapa_original(db)
            if trampa["armada"]:
                trampa["armada"] = False  # una sola vez: el flush de la otra sesión no debe volver a disparar
                _poner_rut(id_j, rut_j_otro)
            return resultado

        S.mapa_clientes = _mapa_y_otro_cambia_el_rut
        try:
            cot_j1 = _venta_sin_vincular("J1")
            trampa["armada"] = True
            db = SessionLocal()
            try:
                S.vincular_historico(db, aplicar=True)
                db.commit()
            finally:
                db.close()
            check("j1 (la sonda sirve) la trampa se disparó", not trampa["armada"])
            check("j1 histórico: con el mapa viejo, la venta NO se cuelga de la ficha que cambió de RUT",
                  _cliente_de(cot_j1) is None, _cliente_de(cot_j1))

            _poner_rut(id_j, rut_j)
            trampa["armada"] = True
            cot_j2 = _nueva_cot("J2", rut_j)
            check("j2 gancho: una venta nueva nace sin vincular si la ficha cambió de RUT entre el mapa y el flush",
                  not trampa["armada"] and _cliente_de(cot_j2) is None, _cliente_de(cot_j2))

            _poner_rut(id_j, rut_j)
            cot_j2b = _nueva_cot("J2b", rut_j)
            check("j2b (precondición) una venta normal nace vinculada a J", _cliente_de(cot_j2b) == id_j)
            trampa["armada"] = True
            db = SessionLocal()
            try:
                db.query(Cotizacion).filter(Cotizacion.id == cot_j2b).first().rut_cliente = rut_j.replace(".", "")
                db.commit()
            finally:
                db.close()
            check("j2b gancho: a una venta YA vinculada que solo le reescriben el RUT también se le confirma la ficha",
                  not trampa["armada"] and _cliente_de(cot_j2b) is None, _cliente_de(cot_j2b))

            cot_j3 = _venta_sin_vincular("J3")
            trampa["armada"] = True
            r = client.post("/api/compras/oc-cliente", headers=cab,
                            json={"cotizacion_id": cot_j3, "numero_oc": f"{MARK}-OC-J3"})
            check("j3 Cierre: responde 201 y, con el mapa viejo, la venta sigue sin vincular",
                  r.status_code == 201 and not trampa["armada"] and _cliente_de(cot_j3) is None,
                  (r.status_code, _cliente_de(cot_j3)))

            # j4 — el candado mismo: la sesión A está editando la ficha (UPDATE sin
            # confirmar, toma la fila) y la confirmación de B tiene que ESPERAR. B usa una
            # conexión propia con la espera acotada a 1 s, que al final se invalida: quedó
            # con el tiempo de espera cambiado y no puede volver al pool.
            _poner_rut(id_j, rut_j)
            a = SessionLocal()
            conexion_b = engine.connect()
            conexion_b.exec_driver_sql("SET SESSION innodb_lock_wait_timeout = 1")
            conexion_b.commit()
            b = SessionLocal(bind=conexion_b)
            try:
                mapa_j, _ = mapa_original(b)
                a.query(Cliente).filter(Cliente.id == id_j).update({"rut": rut_j_otro}, synchronize_session=False)
                try:
                    S.cliente_para_rut_confirmado(b, mapa_j, rut_j, {})
                    resultado = "no esperó"
                except OperationalError as error:
                    resultado = error.orig.args[0]
                check("j4 confirmar la ficha ESPERA a quien la está editando (candado compartido): 1205",
                      resultado == 1205, resultado)
            finally:
                a.rollback()
                a.close()
                b.close()
                conexion_b.invalidate()
                conexion_b.close()
        finally:
            S.mapa_clientes = mapa_original
            _poner_rut(id_j, rut_j)

        # ══ §K — Dar acceso pone al día el vínculo ═══════════════════════════════════
        # Una ficha con cuentas ya no puede cambiar de RUT (routers/clientes.py lo
        # rechaza): crear la cuenta resincroniza todas las ventas justo antes de que
        # alguien pueda mirarlas, y de paso repara vínculos viejos de cualquier origen.
        cot_k = _nueva_cot("K", rut_libre)
        db = SessionLocal()
        try:
            # Vínculo OBSOLETO fabricado con un UPDATE masivo (que el gancho no ve).
            db.query(Cotizacion).filter(Cotizacion.id == cot_k).update({"cliente_id": id_b})
            db.commit()
        finally:
            db.close()
        r = client.post("/api/portal-admin/cuentas", headers=cab, json={
            "email": f"{MARK.lower()}-k@test.invalid", "nombre": f"{MARK} Contacto K",
            "cliente_ids": [id_b], "perfil": "procurement"})
        check("k1 crear una cuenta para la ficha B → 201", r.status_code == 201, r.text[:160])
        check("k2 y puso al día el vínculo: la venta colgada de B por un UPDATE masivo quedó en NULL",
              _cliente_de(cot_k) is None, _cliente_de(cot_k))

        print()
        print(f"FALLARON {len(_fails)}: {_fails}" if _fails else "SUITE VERDE: vínculo venta ↔ cliente")
        assert not _fails, _fails
    finally:
        D.limpiar(MARK)


def test_vinculacion_portal():
    run()


if __name__ == "__main__":
    run()
