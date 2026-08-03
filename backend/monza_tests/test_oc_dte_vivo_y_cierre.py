"""Paridad contable · los DOS candados del PATCH de cotizaciones de MonzaParts.

W6 — EL N° Y LA FECHA DE LA OC SON REFERENCIA 801 DEL DTE. `CotUpdate` acepta
`oc_cliente`/`oc_fecha` y el PATCH los escribía con un `setattr` en bucle SIN NINGÚN
chequeo (grep de "wasabil" en el archivo: 0). Los dos datos viajan al SII dentro de
cada documento de la venta —`armar_guia` de la guía 52 y `armar_referencias_factura`
de la factura 33 (monza_wasabil_dte/service.py)—, así que editarlos después de emitir
deja el papel legal desincronizado del sistema, y en un despacho PARCIAL la 2ª guía
sale con una OC distinta a la 1ª. Grupo AM ya responde 409 nombrando el folio
(routers/compras.py, PUT /oc-cliente); Monza no tenía nada.

A5 — CERRAR COMO VENTA UNA COTIZACIÓN RECHAZADA O NUNCA ENVIADA. El botón de la
pantalla se pinta con `estado !== "vendida"` y los estados posibles incluyen
'propuesta' y 'rechazada': un clic mal dado en la fila equivocada cerraba la venta y
LIBERABA LAS COMPRAS al proveedor (el cierre pone las líneas en 'por_comprar'). GA solo
ofrece las OC con fase_comercial 'ingresada' + estado 'completado'.

Esta suite cubre, contra la BD real (datos MARCADOS + limpieza total + verificación de
residuos con conexión nueva):

   1 · guía 52 EMITIDA (folio) bloquea cambiar el N° de OC — 409 que NOMBRA el folio.
   2 · y bloquea cambiar la FECHA de la OC (es la otra mitad de la referencia 801).
   3 · re-enviar el MISMO N° y la MISMA fecha NO bloquea (el cierre idempotente del
       modal manda los dos campos en cada reintento) y los demás campos se editan.
   4 · factura 33 viva bloquea igual (la 801 también viaja en la factura).
   5 · direccionalidad: DTE FALLIDO, claim VENCIDO sin uuid ni en_vuelo, y un DTE de
       OTRA venta NO bloquean nada.
   6 · claim EN VUELO (sin uuid, sin folio) bloquea con "(en emisión)".
   7 · AMBIGUO (uuid NULL + en_vuelo_desde con claim VENCIDO) BLOQUEA — el caso que
       decidir por TTL dejaría pasar, siendo que el documento PUDO nacer en el SII.
   8 · A5: cerrar desde 'propuesta' y desde 'rechazada' → 409, y NADA se movió (ni
       estado, ni líneas, ni log VENDIDA, ni notificación); desde 'enviada' → 200;
       re-cierre desde 'vendida' → 200 (idempotente); desde 'despachado' → 409.
   9 · SONDAS DE PODER DISCRIMINANTE: se le QUITA el arreglo y se comprueba que el
       mismo predicado que da verde se pone ROJO (guard W6 completo, rama AMBIGUA, y
       lista de estados de A5).
  10 · CERO residuos.

CERO llamadas a Wasabil / al SII: las filas de DTE se escriben DIRECTO en la BD.

Corre con:  ./venv/bin/python -m pytest monza_tests/test_oc_dte_vivo_y_cierre.py -q
       o:   ./venv/bin/python monza_tests/test_oc_dte_vivo_y_cierre.py
"""
import os
import sys
import uuid as uuid_mod
from datetime import date, datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from database import SessionLocal, engine  # noqa: E402
from auth import get_current_user  # noqa: E402
from monza_models import (  # noqa: E402
    MonzaCliente, MonzaCotizacion, MonzaCotizacionItem, MonzaDespacho,
    MonzaLog, MonzaNotificacion,
)
from monza_contabilidad.models import MonzaContFacturaCliente  # noqa: E402
from monza_wasabil_dte.models import (  # noqa: E402
    MonzaWasabilDte, CLAIM_TTL_SEGUNDOS,
    STATUS_PENDIENTE, STATUS_PROCESANDO, STATUS_EMITIDO, STATUS_FALLIDO,
)
from monza_wasabil_dte.service import TIPO_DOC_GUIA, TIPO_DOC_FACTURA  # noqa: E402
import monza_router_cotizaciones as cot_mod  # noqa: E402
from monza_router_cotizaciones import router as cotizaciones_router  # noqa: E402

MARK = "__TEST_W6A5__"
EMAIL = f"{MARK}@test.invalid"
OC_ORIGINAL = "OC-W6-1788"
FECHA_ORIGINAL = date(2026, 7, 20)

app = FastAPI()
app.include_router(cotizaciones_router)
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=None, email=EMAIL, empresa="automotriz")
client = TestClient(app)

_fails: list = []
# Ids que el test vio nacer: las notificaciones se cuelgan de (entidad, entidad_id) y
# no llevan MARK, así que los residuos se cuentan contra estos ids.
_VISTOS = {"cotizacion": set()}


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {str(extra)[:300]}"))
    if not cond:
        _fails.append(name)


# ═══════════════ Lecturas de VERIFICACIÓN: conexión nueva, SQL crudo ═════════════
# La sesión del test arrastra su read view bajo REPEATABLE READ y puede dar falso rojo
# Y falso verde: todo lo que se assertea se lee con conexión nueva.

def _sql(q, **p):
    with engine.connect() as c:
        return c.execute(text(q), p).fetchall()


def _venta_row(cot_id):
    r = _sql("SELECT estado, oc_cliente, oc_fecha, forma_pago, pct_adelanto "
             "FROM monza_cotizaciones WHERE id=:i", i=cot_id)
    if not r:
        return None
    return {"estado": r[0][0], "oc": r[0][1], "oc_fecha": r[0][2],
            "forma_pago": r[0][3], "pct": r[0][4]}


def _estados_linea(cot_id):
    return [r[0] for r in _sql("SELECT estado_linea FROM monza_cotizacion_items "
                               "WHERE cotizacion_id=:c ORDER BY id", c=cot_id)]


def _n_logs(cot_id, accion):
    return _sql("SELECT COUNT(*) FROM monza_logs WHERE user_email=:e AND accion=:a "
                "AND entidad='cotizacion' AND entidad_id=:i",
                e=EMAIL, a=accion, i=cot_id)[0][0]


def _n_notifs(cot_id):
    return _sql("SELECT COUNT(*) FROM monza_notificaciones WHERE entidad='cotizacion' "
                "AND entidad_id=:i", i=cot_id)[0][0]


# ═══════════════════════════════ Siembra marcada ═════════════════════════════════

def _venta(db, estado="vendida", oc=OC_ORIGINAL, oc_fecha=FECHA_ORIGINAL,
           estado_linea="cotizado"):
    """Cliente + cotización + 1 línea. La OC nace poblada: el guard W6 compara contra
    el valor ACTUAL, así que sin OC previa no habría nada que desincronizar."""
    cli = MonzaCliente(nombre=MARK, rut="11.111.111-1")
    db.add(cli); db.flush()
    cot = MonzaCotizacion(
        numero=f"CT-W6-{uuid_mod.uuid4().hex[:6].upper()}", cliente_id=cli.id,
        estado=estado, oc_cliente=oc, oc_fecha=oc_fecha, forma_pago="30 días",
        total_neto=100000, iva_monto=19000, total_bruto=119000, iva_pct=19,
    )
    db.add(cot); db.flush()
    db.add(MonzaCotizacionItem(cotizacion_id=cot.id, descripcion=f"{MARK} filtro",
                               numero_parte="P-W6-1", cantidad=2,
                               precio_unitario_clp=50000, subtotal_clp=100000,
                               estado_linea=estado_linea))
    db.commit()
    _VISTOS["cotizacion"].add(cot.id)
    return cot.id


def _despacho(db, cot_id, estado="despachado"):
    d = MonzaDespacho(numero=f"{MARK[:20]}D{uuid_mod.uuid4().hex[:6]}", estado=estado,
                      cotizacion_id=cot_id, cliente_nombre=MARK)
    db.add(d); db.commit()
    return d.id


def _factura(db, cot_id):
    f = MonzaContFacturaCliente(cotizacion_id=cot_id, numero_cotizacion=f"CW6{MARK[:8]}",
                                cliente_nombre=MARK, monto_bruto=119000)
    db.add(f); db.commit()
    return f.id


def _dte(db, *, despacho_id=None, factura_id=None, tipo=TIPO_DOC_GUIA, status_id=None,
         uuid_w=None, folio=None, en_vuelo_hace_seg=None):
    """Fila de DTE escrita DIRECTO en la BD (jamás se llama a Wasabil).

    payload_json=MARK es el ancla de la verificación de residuos."""
    en_vuelo = None
    if en_vuelo_hace_seg is not None:
        en_vuelo = datetime.utcnow() - timedelta(seconds=en_vuelo_hace_seg)
    fila = MonzaWasabilDte(
        empresa="automotriz", tipo_dte=tipo, despacho_id=despacho_id,
        factura_id=factura_id, uuid=uuid_w, status_id=status_id, folio=folio,
        en_vuelo_desde=en_vuelo, payload_json=MARK,
    )
    db.add(fila); db.commit()
    return fila.id


# ═══════════════════════════ Llamadas al API bajo prueba ═════════════════════════

def _patch(cot_id, **body):
    return client.patch(f"/api/monza/cotizaciones/{cot_id}", json=body)


# ═════════════════════ Limpieza TOTAL en orden FK-seguro ═════════════════════════

def _limpiar(db):
    db.rollback()
    S = "fetch"
    cot_ids = [r[0] for r in db.query(MonzaCotizacion.id)
               .join(MonzaCliente, MonzaCliente.id == MonzaCotizacion.cliente_id)
               .filter(MonzaCliente.nombre == MARK).all()]
    _VISTOS["cotizacion"].update(cot_ids)
    dsp_ids = [r[0] for r in db.query(MonzaDespacho.id).filter(
        MonzaDespacho.cliente_nombre == MARK).all()]
    fac_ids = [r[0] for r in db.query(MonzaContFacturaCliente.id).filter(
        MonzaContFacturaCliente.cliente_nombre == MARK).all()]
    # Los DTE PRIMERO: sus FK a despachos y facturas no tienen ON DELETE.
    db.query(MonzaWasabilDte).filter(
        (MonzaWasabilDte.despacho_id.in_(dsp_ids or [0]))
        | (MonzaWasabilDte.factura_id.in_(fac_ids or [0]))
        | (MonzaWasabilDte.payload_json == MARK)).delete(synchronize_session=S)
    db.query(MonzaContFacturaCliente).filter(
        MonzaContFacturaCliente.id.in_(fac_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaDespacho).filter(
        MonzaDespacho.id.in_(dsp_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaNotificacion).filter(
        MonzaNotificacion.entidad == "cotizacion",
        MonzaNotificacion.entidad_id.in_(list(_VISTOS["cotizacion"]) or [0]),
    ).delete(synchronize_session=S)
    db.query(MonzaLog).filter(MonzaLog.user_email == EMAIL).delete(synchronize_session=S)
    db.query(MonzaCotizacionItem).filter(
        MonzaCotizacionItem.cotizacion_id.in_(cot_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaCotizacion).filter(
        MonzaCotizacion.id.in_(cot_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaCliente).filter(MonzaCliente.nombre == MARK).delete(synchronize_session=S)
    db.commit()


def _residuos():
    def _n(q, **p):
        return _sql(q, **p)[0][0]

    r = {
        "clientes": _n("SELECT COUNT(*) FROM monza_clientes WHERE nombre=:m", m=MARK),
        "cotizaciones": _n("SELECT COUNT(*) FROM monza_cotizaciones WHERE numero LIKE 'CT-W6-%'"),
        "despachos": _n("SELECT COUNT(*) FROM monza_despachos WHERE cliente_nombre=:m", m=MARK),
        "facturas": _n("SELECT COUNT(*) FROM monza_cont_factura_cliente "
                       "WHERE cliente_nombre=:m", m=MARK),
        "dte": _n("SELECT COUNT(*) FROM monza_wasabil_dte WHERE payload_json=:m", m=MARK),
        "logs": _n("SELECT COUNT(*) FROM monza_logs WHERE user_email=:e", e=EMAIL),
    }
    notifs = 0
    ids = [i for i in _VISTOS["cotizacion"] if i]
    if ids:
        lista = ",".join(str(int(i)) for i in ids)
        notifs = _n(f"SELECT COUNT(*) FROM monza_notificaciones WHERE "
                    f"entidad='cotizacion' AND entidad_id IN ({lista})")
    r["notificaciones"] = notifs
    return r


# ═══════════════════════════════════ RUN ═════════════════════════════════════════

def run():
    db = SessionLocal()
    try:
        _limpiar(db)

        # ══════════════════════════════════════════════════════════════════════
        # 1 · Guía 52 EMITIDA (con folio) → el N° de OC NO se edita.
        # ══════════════════════════════════════════════════════════════════════
        cot_id = _venta(db)
        _dte(db, despacho_id=_despacho(db, cot_id), status_id=STATUS_EMITIDO,
             uuid_w=f"u-{uuid_mod.uuid4().hex[:8]}", folio="9137")
        r = _patch(cot_id, oc_cliente="OC-CAMBIADA")
        check("1.1 guía 52 emitida → editar el N° de OC da 409", r.status_code == 409, r.text)
        check("1.1 el 409 NOMBRA el folio (el operador sabe qué documento lo bloquea)",
              "9137" in r.text, r.text[:200])
        check("1.1 el 409 explica que es la referencia 801",
              "801" in r.text, r.text[:200])
        check("1.2 ★ el dato NO se movió: la OC sigue siendo la referenciada",
              _venta_row(cot_id)["oc"] == OC_ORIGINAL, _venta_row(cot_id))

        # ══════════════════════════════════════════════════════════════════════
        # 2 · La FECHA es la otra mitad de la 801: se protege igual.
        # ══════════════════════════════════════════════════════════════════════
        r = _patch(cot_id, oc_fecha="2026-07-30")
        check("2.1 editar la FECHA de la OC da 409", r.status_code == 409, r.text)
        check("2.2 ★ la fecha sigue siendo la que se imprimió en el documento",
              str(_venta_row(cot_id)["oc_fecha"]) == str(FECHA_ORIGINAL),
              _venta_row(cot_id))
        # Cambiar los DOS a la vez tampoco pasa.
        r = _patch(cot_id, oc_cliente="OC-OTRA", oc_fecha="2026-07-30")
        check("2.3 cambiar N° y fecha en el mismo PATCH da 409", r.status_code == 409, r.text)
        v = _venta_row(cot_id)
        check("2.3 ninguno de los dos se movió",
              v["oc"] == OC_ORIGINAL and str(v["oc_fecha"]) == str(FECHA_ORIGINAL), v)

        # ══════════════════════════════════════════════════════════════════════
        # 3 · NO-OP: re-enviar el MISMO valor pasa. Es el caso REAL más frecuente —
        #     el modal de cierre manda oc_cliente y oc_fecha en CADA reintento, y el
        #     cierre es idempotente: un guard que mirara "vino el campo" en vez de
        #     "cambió el valor" trabaría la operación normal con un 409 falso.
        # ══════════════════════════════════════════════════════════════════════
        r = _patch(cot_id, oc_cliente=OC_ORIGINAL, oc_fecha=str(FECHA_ORIGINAL))
        check("3.1 ★ re-enviar el MISMO N° y la MISMA fecha → 200 (no-op, no bloquea)",
              r.status_code == 200, r.text)
        # Y los campos que el SII NO referencia se siguen pudiendo corregir.
        r = _patch(cot_id, forma_pago="60 días contra factura", pct_adelanto=50)
        check("3.2 con guía viva, los demás campos SÍ se editan (el guard es quirúrgico)",
              r.status_code == 200, r.text)
        v = _venta_row(cot_id)
        check("3.2 y el cambio se persistió",
              v["forma_pago"] == "60 días contra factura" and int(v["pct"] or 0) == 50, v)
        _limpiar(db)

        # ══════════════════════════════════════════════════════════════════════
        # 4 · Factura 33 viva: la referencia 801 también viaja en ella.
        # ══════════════════════════════════════════════════════════════════════
        cot_id = _venta(db)
        _dte(db, factura_id=_factura(db, cot_id), tipo=TIPO_DOC_FACTURA,
             status_id=STATUS_EMITIDO, uuid_w=f"u-{uuid_mod.uuid4().hex[:8]}", folio="116")
        r = _patch(cot_id, oc_cliente="OC-CAMBIADA")
        check("4.1 factura 33 emitida → editar el N° de OC da 409", r.status_code == 409, r.text)
        check("4.1 el 409 dice FACTURA (no 'guía') y nombra el folio",
              "factura" in r.text.lower() and "116" in r.text, r.text[:200])
        check("4.2 el dato no se movió", _venta_row(cot_id)["oc"] == OC_ORIGINAL)
        _limpiar(db)

        # ══════════════════════════════════════════════════════════════════════
        # 5 · DIRECCIONALIDAD (la mitad que NO debe bloquear). Sin esto, el guard
        #     podría ser un "siempre 409" y los checks de arriba no probarían nada.
        # ══════════════════════════════════════════════════════════════════════
        cot_id = _venta(db)
        _dte(db, despacho_id=_despacho(db, cot_id), status_id=STATUS_FALLIDO)
        r = _patch(cot_id, oc_cliente="OC-CORREGIDA-1")
        check("5.1 DTE FALLIDO (el SII rechazó, sin folio) NO bloquea: la OC se corrige",
              r.status_code == 200, r.text)
        check("5.1 y el cambio se guardó", _venta_row(cot_id)["oc"] == "OC-CORREGIDA-1")
        _limpiar(db)

        cot_id = _venta(db)
        _dte(db, despacho_id=_despacho(db, cot_id), status_id=STATUS_PENDIENTE,
             en_vuelo_hace_seg=CLAIM_TTL_SEGUNDOS + 60)
        # Claim VENCIDO pero en_vuelo_desde puesto = AMBIGUO → eso sí bloquea (sección 7).
        # Acá se prueba el otro borde: claim vencido y en_vuelo_desde LIMPIO (el error
        # fue CONFIRMADO, no ambiguo) → no bloquea.
        db.query(MonzaWasabilDte).filter(MonzaWasabilDte.payload_json == MARK).update(
            {"en_vuelo_desde": None})
        db.commit()
        r = _patch(cot_id, oc_cliente="OC-CORREGIDA-2")
        check("5.2 pendiente sin uuid y con el envío ya descartado (en_vuelo limpio) "
              "NO bloquea", r.status_code == 200, r.text)
        _limpiar(db)

        # DTE de OTRA venta: el guard filtra por cotizacion_id, no por marca.
        cot_ajena = _venta(db)
        _dte(db, despacho_id=_despacho(db, cot_ajena), status_id=STATUS_EMITIDO,
             uuid_w=f"u-{uuid_mod.uuid4().hex[:8]}", folio="9200")
        cot_id = _venta(db)
        r = _patch(cot_id, oc_cliente="OC-CORREGIDA-3")
        check("5.3 un DTE vivo de OTRA venta no bloquea esta (el guard filtra por venta)",
              r.status_code == 200, r.text)
        check("5.3 y la venta ajena quedó intacta",
              _venta_row(cot_ajena)["oc"] == OC_ORIGINAL)
        _limpiar(db)

        # 5.4 · Rama del DEPLOY A MEDIAS (tabla del módulo DTE inexistente, MySQL 1146):
        #       el guard se apaga solo, pero su rollback SUELTA el FOR UPDATE, así que
        #       el PATCH re-toma el lock antes de seguir. Se simula con el helper
        #       devolviendo (None, transaccion_reiniciada=True) — dropear la tabla no
        #       es una opción en una BD con documentos reales.
        cot_id = _venta(db)
        original_helper_0 = cot_mod._dte_vivo_de_la_venta
        cot_mod._dte_vivo_de_la_venta = lambda db_, cid: (None, True)
        try:
            r = _patch(cot_id, oc_cliente="OC-SIN-MODULO-DTE", forma_pago="Contado")
            v = _venta_row(cot_id)
            check("5.4 sin la tabla del módulo DTE el PATCH re-toma el lock y termina "
                  "bien (no 500, y el cambio se persiste)",
                  r.status_code == 200 and v["oc"] == "OC-SIN-MODULO-DTE"
                  and v["forma_pago"] == "Contado", f"{r.status_code} {r.text[:200]} {v}")
        finally:
            cot_mod._dte_vivo_de_la_venta = original_helper_0
        _limpiar(db)

        # ══════════════════════════════════════════════════════════════════════
        # 6 · Claim EN VUELO (sin uuid, sin folio): el POST salió hace segundos.
        # ══════════════════════════════════════════════════════════════════════
        cot_id = _venta(db)
        _dte(db, despacho_id=_despacho(db, cot_id), status_id=STATUS_PENDIENTE,
             en_vuelo_hace_seg=1)
        r = _patch(cot_id, oc_cliente="OC-CAMBIADA")
        check("6.1 emisión EN VUELO → 409", r.status_code == 409, r.text)
        check("6.1 y sin folio el mensaje dice '(en emisión)'",
              "en emisión" in r.text, r.text[:200])
        _limpiar(db)

        # Procesando en el SII (uuid, sin folio todavía).
        cot_id = _venta(db)
        _dte(db, despacho_id=_despacho(db, cot_id), status_id=STATUS_PROCESANDO,
             uuid_w=f"u-{uuid_mod.uuid4().hex[:8]}")
        r = _patch(cot_id, oc_cliente="OC-CAMBIADA")
        check("6.2 procesando en el SII (uuid, sin folio) → 409", r.status_code == 409, r.text)
        _limpiar(db)

        # ══════════════════════════════════════════════════════════════════════
        # 7 · ★ CASO AMBIGUO: uuid NULL + en_vuelo_desde puesto + claim VENCIDO.
        #     Wasabil cortó sin confirmar: el documento PUDO nacer con folio real.
        #     Decidir por TTL dejaría editar la OC de un documento vivo.
        # ══════════════════════════════════════════════════════════════════════
        cot_id = _venta(db)
        _dte(db, despacho_id=_despacho(db, cot_id), status_id=STATUS_PENDIENTE,
             en_vuelo_hace_seg=CLAIM_TTL_SEGUNDOS + 3600)
        r = _patch(cot_id, oc_cliente="OC-CAMBIADA")
        check("7.1 ★ AMBIGUO (uuid NULL, en_vuelo puesto, claim VENCIDO) → 409",
              r.status_code == 409, r.text)
        check("7.2 y la OC no se movió", _venta_row(cot_id)["oc"] == OC_ORIGINAL)
        cot_ambiguo = cot_id   # se reutiliza en la sonda 9.2

        # ══════════════════════════════════════════════════════════════════════
        # 9.1 · SONDA de la rama AMBIGUA: el helper SIN esa rama (lo que sería un
        #       port literal de GA sin `incluir_ambiguo`) deja pasar el 409.
        # ══════════════════════════════════════════════════════════════════════
        original_helper = cot_mod._dte_vivo_de_la_venta

        def _helper_sin_ambiguo(db_, cot_id_):
            """Mismo helper con la rama AMBIGUA amputada (decide por TTL)."""
            from monza_wasabil_dte.service import claim_vigente
            dte, rein = original_helper(db_, cot_id_)
            if dte is None:
                return None, rein
            vivo = (dte.status_id == STATUS_EMITIDO or claim_vigente(dte)
                    or (dte.uuid and dte.status_id in (STATUS_PROCESANDO, STATUS_PENDIENTE)))
            return (dte if vivo else None), rein

        cot_mod._dte_vivo_de_la_venta = _helper_sin_ambiguo
        try:
            r = _patch(cot_ambiguo, oc_cliente="OC-SONDA-AMBIGUO")
            check("9.1 ★ SONDA: sin la rama AMBIGUA el mismo caso pasa a 200 → la rama "
                  "TIENE poder discriminante", r.status_code == 200, r.text)
        finally:
            cot_mod._dte_vivo_de_la_venta = original_helper
        check("9.1 el módulo quedó restaurado",
              cot_mod._dte_vivo_de_la_venta is original_helper)
        # Con el helper real vuelto a su lugar, el caso vuelve a bloquearse.
        r = _patch(cot_ambiguo, oc_cliente="OC-CAMBIADA-2")
        check("9.1 restaurado el arreglo, el AMBIGUO vuelve a dar 409",
              r.status_code == 409, r.text)
        _limpiar(db)

        # ══════════════════════════════════════════════════════════════════════
        # 9.2 · SONDA del guard W6 COMPLETO: con el helper devolviendo siempre None
        #       (equivalente a no tener el guard), el setattr en bucle vuelve a pisar
        #       la OC de un documento EMITIDO — exactamente el defecto W6.
        # ══════════════════════════════════════════════════════════════════════
        cot_id = _venta(db)
        _dte(db, despacho_id=_despacho(db, cot_id), status_id=STATUS_EMITIDO,
             uuid_w=f"u-{uuid_mod.uuid4().hex[:8]}", folio="9138")
        cot_mod._dte_vivo_de_la_venta = lambda db_, cid: (None, False)
        try:
            r = _patch(cot_id, oc_cliente="OC-SONDA-SIN-GUARD", oc_fecha="2026-01-01")
            paso = r.status_code == 200 and _venta_row(cot_id)["oc"] == "OC-SONDA-SIN-GUARD"
            check("9.2 ★ SONDA: sin el guard, la OC de una guía EMITIDA (folio 9138) se "
                  "pisa con un 200 → el check 1.1/1.2 detecta la regresión", paso,
                  f"{r.status_code} {_venta_row(cot_id)}")
        finally:
            cot_mod._dte_vivo_de_la_venta = original_helper
        check("9.2 el módulo quedó restaurado",
              cot_mod._dte_vivo_de_la_venta is original_helper)
        _limpiar(db)

        # ══════════════════════════════════════════════════════════════════════
        # 8 · A5 · No se cierra como venta una cotización rechazada ni una que nunca
        #     se envió: el cierre libera las compras al proveedor.
        # ══════════════════════════════════════════════════════════════════════
        for estado_malo in ("propuesta", "rechazada"):
            cot_id = _venta(db, estado=estado_malo)
            r = _patch(cot_id, estado="vendida", oc_cliente=OC_ORIGINAL,
                       oc_fecha=str(FECHA_ORIGINAL), pct_adelanto=50)
            check(f"8.1 cerrar desde '{estado_malo}' → 409",
                  r.status_code == 409, f"{r.status_code} {r.text[:200]}")
            check(f"8.1 el 409 nombra el estado que bloquea",
                  estado_malo in r.text, r.text[:200])
            v = _venta_row(cot_id)
            check(f"8.2 ★ tras el 409 NADA se movió desde '{estado_malo}': estado, "
                  f"líneas, log VENDIDA y notificación intactos",
                  v["estado"] == estado_malo
                  and _estados_linea(cot_id) == ["cotizado"]
                  and _n_logs(cot_id, "VENDIDA") == 0
                  and _n_notifs(cot_id) == 0,
                  f"{v} {_estados_linea(cot_id)} logs={_n_logs(cot_id, 'VENDIDA')} "
                  f"notifs={_n_notifs(cot_id)}")
            check(f"8.2 y el pct_adelanto del PATCH rechazado no se colgó de la venta",
                  int(v["pct"] or 0) == 0, v)
            _limpiar(db)

        # 'enviada' es el camino LEGÍTIMO: cierra, mueve las líneas y deja su log.
        cot_id = _venta(db, estado="enviada")
        r = _patch(cot_id, estado="vendida", oc_cliente=OC_ORIGINAL,
                   oc_fecha=str(FECHA_ORIGINAL), pct_adelanto=50)
        check("8.3 cerrar desde 'enviada' → 200 (el camino normal sigue abierto)",
              r.status_code == 200, r.text)
        check("8.3 y el cierre hizo su trabajo (estado, líneas, log VENDIDA)",
              _venta_row(cot_id)["estado"] == "vendida"
              and _estados_linea(cot_id) == ["por_comprar"]
              and _n_logs(cot_id, "VENDIDA") == 1,
              f"{_venta_row(cot_id)} {_estados_linea(cot_id)}")
        # Re-cierre sobre una venta YA vendida: idempotente, no debe caer en el guard.
        r = _patch(cot_id, estado="vendida", oc_cliente=OC_ORIGINAL)
        check("8.4 ★ re-enviar 'vendida' sobre una venta cerrada → 200 (idempotencia "
              "intacta: el guard no puede romper el reintento del modal)",
              r.status_code == 200, r.text)
        check("8.4 y el log de venta no se duplicó", _n_logs(cot_id, "VENDIDA") == 1)
        _limpiar(db)

        # 'despachado' → 'vendida' sigue dando 409 por su guard propio (no lo pisamos).
        cot_id = _venta(db, estado="despachado")
        r = _patch(cot_id, estado="vendida")
        check("8.5 'despachado' → 'vendida' sigue dando 409 (guard preexistente intacto)",
              r.status_code == 409, r.text)
        check("8.5 y el mensaje sigue siendo el de despacho, no el de A5",
              "despachada" in r.text, r.text[:200])
        _limpiar(db)

        # ══════════════════════════════════════════════════════════════════════
        # 9.3 · SONDA de A5: con la lista de estados abierta, cerrar una cotización
        #       RECHAZADA vuelve a pasar y las líneas se van a 'por_comprar' (o sea,
        #       Abastecimiento queda habilitado para comprar) — el defecto original.
        # ══════════════════════════════════════════════════════════════════════
        cot_id = _venta(db, estado="rechazada")
        original_estados = cot_mod.ESTADOS_QUE_CIERRAN_VENTA
        cot_mod.ESTADOS_QUE_CIERRAN_VENTA = ("propuesta", "enviada", "rechazada", "vendida")
        try:
            r = _patch(cot_id, estado="vendida", oc_cliente=OC_ORIGINAL,
                       oc_fecha=str(FECHA_ORIGINAL))
            paso = (r.status_code == 200 and _venta_row(cot_id)["estado"] == "vendida"
                    and _estados_linea(cot_id) == ["por_comprar"])
            check("9.3 ★ SONDA: con la lista de estados abierta, una cotización "
                  "RECHAZADA se cierra y habilita las compras → el check 8.1/8.2 "
                  "detecta la regresión", paso,
                  f"{r.status_code} {_venta_row(cot_id)} {_estados_linea(cot_id)}")
        finally:
            cot_mod.ESTADOS_QUE_CIERRAN_VENTA = original_estados
        check("9.3 la constante quedó restaurada",
              cot_mod.ESTADOS_QUE_CIERRAN_VENTA is original_estados)
        _limpiar(db)

    finally:
        _limpiar(db)
        res = _residuos()
        db.close()
        print(f"Residuos: {res}")
    check("10 CERO residuos en la BD con el MARK (conexión nueva, SQL crudo)",
          all(v == 0 for v in res.values()), res)

    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_oc_dte_vivo_y_cierre():
    """Wrapper pytest de una línea: sin él la suite sería INVISIBLE al gate."""
    run()


if __name__ == "__main__":
    try:
        run()
    except AssertionError as e:
        print("\n=== RESULTADO:", e, "===")
        sys.exit(1)
    sys.exit(0)
