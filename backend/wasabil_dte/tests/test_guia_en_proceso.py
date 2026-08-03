"""Guard: la factura NO debe referenciar el N° de guía manual viejo mientras el folio
del SII de la guía electrónica todavía viene en camino.

Cubre además los dos endurecimientos que se le sumaron al guard:

  · «EMITIDA pero SIN folio» cuenta como EN PROCESO. Wasabil puede responder al POST
    con status 3 y sin folio; con el criterio viejo (status == 3 → False) la factura 33
    caía al fallback manual y salía al SII referenciando el N° de guía TECLEADO a mano —
    un folio 52 que el SII no reconoce, irreversible salvo nota de crédito.
  · `_guia_referencia_de_factura` devuelve una TERCERA componente `problema`. Antes
    devolvía (None, None) en tres caminos distintos sin reportar nada, y
    armar_referencias_factura omitía la 52 con `problemas` vacío → el DTE 33 salía REAL
    sin la referencia a la guía que ampara la mercadería.
  · SECCIÓN 9 (nueva): el N° de guía EN PAPEL con un documento electrónico VIVO en
    Wasabil. El estado `status 4 · uuid · sin claim` era el ÚNICO con uuid que dejaba
    citar el N° tecleado a mano: un rechazo confirmado del documento U no dice nada sobre
    OTRO documento con la misma referencia, y si allá hay una 52 EMITIDA la 33 sale
    citando el folio del papel en vez del real. Ahora manda el veredicto de tres estados
    (_problema_52_de_papel), con salida humana AUDITADA cuando no se puede concluir.

Sin red y sin emitir: solo funciones del router contra la BD local; `buscar_documentos` y
`obtener_documento` quedan SIMULADOS a nivel de módulo (nunca se llama al API real, ni
siquiera de lectura). Datos MARCADOS y limpieza al final.

Corre con:  ./venv/bin/python -m pytest wasabil_dte/tests/test_guia_en_proceso.py -q
"""
import os
import sys
from datetime import date, datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from sqlalchemy import text  # noqa: E402

from database import SessionLocal  # noqa: E402
from models.models import Cotizacion, OcCliente, Despacho  # noqa: E402
from wasabil_dte.models import (  # noqa: E402
    WasabilDte, STATUS_PENDIENTE, STATUS_PROCESANDO, STATUS_EMITIDO, STATUS_FALLIDO,
)
from wasabil_dte import client as wasabil_client  # noqa: E402
from wasabil_dte.router import (  # noqa: E402
    _DOC_FACTURA, _SUST_FACTURA, _estado_dte_bloquea, _guia_electronica_en_proceso,
    _guia_referencia_de_factura,
)

MARK = "__TEST_GUARD52__"

# Fecha de EMISIÓN de la guía EN PAPEL (la que cita la referencia 52 de la factura).
FECHA_GUIA_PAPEL = date(2026, 6, 18)
_fails: list = []

# Wasabil SIMULADO a nivel de módulo: `_problema_52_de_papel` consulta el listado, y esta
# suite no puede tocar el API real ni para leer. `LISTADO` es lo que "tiene" Wasabil;
# `LISTADO_FALLA`, el 405 del API real.
LISTADO: list = []
LISTADO_FALLA = None


def _buscar_fake(search):
    if LISTADO_FALLA:
        raise LISTADO_FALLA
    return [dict(d) for d in LISTADO
            if search and search in str(d.get("invoice_reference") or "")], True


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {str(extra)[:300]}"))
    if not cond:
        _fails.append(name)


def _limpiar(db):
    db.rollback()
    db.execute(text("DELETE FROM wasabil_dte WHERE despacho_id IN "
                    "(SELECT id FROM despachos WHERE numero_despacho LIKE :m)"), {"m": f"{MARK}%"})
    db.execute(text("DELETE FROM despachos WHERE numero_despacho LIKE :m"), {"m": f"{MARK}%"})
    db.execute(text("DELETE FROM oc_cliente WHERE cotizacion_id IN "
                    "(SELECT id FROM cotizaciones WHERE numero LIKE :m)"), {"m": f"{MARK}%"})
    db.execute(text("DELETE FROM cotizaciones WHERE numero LIKE :m"), {"m": f"{MARK}%"})
    db.commit()


def run():
    db = SessionLocal()
    _limpiar(db)  # por si una corrida anterior murió a medias
    # El simulacro se instala DENTRO de run() y se restaura al salir: instalarlo al
    # importar el módulo lo pisaba otra suite de la misma corrida (pytest importa todo
    # antes de ejecutar) y, peor, se filtraba a las demás.
    _orig_buscar = wasabil_client.buscar_documentos
    _orig_obtener = wasabil_client.obtener_documento
    wasabil_client.buscar_documentos = _buscar_fake
    wasabil_client.obtener_documento = lambda uuid: {"uuid": uuid}
    try:
        cot = Cotizacion(numero=f"{MARK}-C", cliente=f"{MARK}", rut_cliente="78.279.030-7")
        db.add(cot); db.flush()
        oc = OcCliente(cotizacion_id=cot.id, numero_oc=f"{MARK[-6:]}", fecha_oc="2026-07-01")
        db.add(oc); db.flush()
        desp = Despacho(numero_despacho=f"{MARK}-D", oc_cliente_id=oc.id, estado="despachado",
                        guia_firmada=1, numero_guia="G-VIEJA-777",
                        # La guía EN PAPEL trae su fecha de emisión: sin ella la referencia 52
                        # se bloquea por OTRO motivo y esta suite —que prueba el guard del N°
                        # manual frente a la guía electrónica— no llegaría a ejercitarlo.
                        # Ese bloqueo tiene su propia suite: test_fecha_guia_papel.py.
                        fecha_guia=FECHA_GUIA_PAPEL,
                        fecha_despacho=datetime.now())
        db.add(desp); db.flush(); db.commit()
        fac = SimpleNamespace(despacho_id=desp.id, despacho=desp, es_anticipo=0)

        # 1) sin guía electrónica → usa el N° manual (comportamiento legítimo)
        folio, _f, problema, _aud = _guia_referencia_de_factura(db, fac)
        check("1 sin guía electrónica: usa el N° manual y NO reporta problema",
              folio == "G-VIEJA-777" and problema is None, (folio, problema))

        # 2) guía electrónica EN PROCESO (status 2, con uuid) → NO debe dar el manual
        dte = WasabilDte(empresa="mineria", tipo_dte=52, despacho_id=desp.id,
                         status_id=STATUS_PROCESANDO, uuid="uuid-en-proceso")
        db.add(dte); db.commit()
        check("2 guía en PROCESO → se detecta", _guia_electronica_en_proceso(db, desp.id) is True)
        folio, _f, problema, _aud = _guia_referencia_de_factura(db, fac)
        check("2 guía en PROCESO → NO devuelve el N° manual viejo", folio is None, folio)
        check("2 guía en PROCESO → REPORTA el problema bloqueante (antes callaba)",
              problema is not None and "EN PROCESO" in problema, problema)

        # 3) borrador pendiente en Wasabil (status 6 con uuid) → igual bloquea
        dte.status_id = STATUS_PENDIENTE; db.commit()
        folio, _f, problema, _aud = _guia_referencia_de_factura(db, fac)
        check("3 guía PENDIENTE en Wasabil → tampoco cae al manual, y reporta",
              folio is None and problema is not None, (folio, problema))

        # 4) ya EMITIDA con folio → usa el folio del SII
        dte.status_id = STATUS_EMITIDO; dte.folio = "555"
        dte.payload_json = '{"documentDate": "2026-07-15"}'
        db.commit()
        folio, fecha, problema, _aud = _guia_referencia_de_factura(db, fac)
        check("4 guía EMITIDA → usa el folio del SII (no el manual), sin problema",
              folio == "555" and str(fecha) == "2026-07-15" and problema is None,
              (folio, fecha, problema))

        # 4b) EMITIDA pero SIN folio → cuenta como EN PROCESO: jamás el N° tecleado.
        # (El POST puede volver con status 3 y sin folio; el sondeo rescata el folio.)
        dte.folio = None; db.commit()
        check("4b emitida SIN folio → cuenta como EN PROCESO",
              _guia_electronica_en_proceso(db, desp.id) is True)
        folio, _f, problema, _aud = _guia_referencia_de_factura(db, fac)
        check("4b emitida SIN folio → NO cae al N° manual y BLOQUEA",
              folio is None and problema is not None and "EN PROCESO" in problema,
              (folio, problema))
        # …y con folio vacío (string en blanco) el guard se comporta igual
        dte.folio = "   "; db.commit()
        check("4b folio en blanco → sigue contando como EN PROCESO",
              _guia_electronica_en_proceso(db, desp.id) is True)

        # 5) guía FALLIDA (sin uuid, sin claim) → NO hay guía electrónica: el manual vale
        dte.status_id = STATUS_FALLIDO; dte.folio = None; dte.uuid = None
        dte.en_vuelo_desde = None
        db.commit()
        check("5 guía FALLIDA sin uuid → no bloquea (no existe guía electrónica)",
              _guia_electronica_en_proceso(db, desp.id) is False)
        folio, _f, problema, _aud = _guia_referencia_de_factura(db, fac)
        check("5 guía FALLIDA → vuelve a usar el N° manual, sin problema",
              folio == "G-VIEJA-777" and problema is None, (folio, problema))

        # 6) despacho SIN N° de guía y sin guía electrónica → problema bloqueante
        desp.numero_guia = None
        db.commit()
        folio, _f, problema, _aud = _guia_referencia_de_factura(db, fac)
        check("6 sin folio ni N° manual → BLOQUEA con el remedio",
              folio is None and problema is not None and "folio" in problema, (folio, problema))

        # 7) factura SIN despacho: la de ANTICIPO no lleva 52 y NO es problema; una
        #    factura normal sin guía identificable SÍ lo es (antes ambas callaban).
        ant = SimpleNamespace(despacho_id=None, despacho=None, es_anticipo=1)
        folio, _f, problema, _aud = _guia_referencia_de_factura(db, ant)
        check("7 factura de ANTICIPO sin despacho → sin 52 y sin problema",
              folio is None and problema is None, (folio, problema))
        suelta = SimpleNamespace(despacho_id=None, despacho=None, es_anticipo=0)
        folio, _f, problema, _aud = _guia_referencia_de_factura(db, suelta)
        check("7 factura normal sin despacho → BLOQUEA (mercadería sin guía que la ampare)",
              folio is None and problema is not None and "guía" in problema, (folio, problema))

        # 8) La MISMA máquina de estados sirve a guías y facturas, pero el sustantivo del
        #    mensaje cambia: el usuario que reintenta una FACTURA no puede leer un texto
        #    que habla de despachos (el 409 de _reclamar_emision_factura es una ventana de
        #    carrera, así que se verifica la función directo).
        emitido = SimpleNamespace(status_id=STATUS_EMITIDO, folio="777", uuid="u1",
                                  en_vuelo_desde=None)
        msg_guia = _estado_dte_bloquea(emitido, False)
        msg_fac = _estado_dte_bloquea(emitido, False, _SUST_FACTURA, _DOC_FACTURA)
        check("8 mensaje de GUÍA: habla del despacho y de la guía electrónica",
              "despacho" in msg_guia.lower() and "guía" in msg_guia.lower(), msg_guia)
        check("8 mensaje de FACTURA: habla de la factura y NO del despacho",
              "factura" in msg_fac.lower() and "despacho" not in msg_fac.lower(), msg_fac)
        check("8 el folio viaja en los dos mensajes (el operador sabe cuál documento es)",
              "777" in msg_guia and "777" in msg_fac, (msg_guia, msg_fac))
        # La LÓGICA no cambia con el sustantivo: mismos veredictos en los dos textos.
        for estado, para_reintento in ((STATUS_FALLIDO, True), (STATUS_FALLIDO, False)):
            d = SimpleNamespace(status_id=estado, folio=None, uuid="u1", en_vuelo_desde=None)
            check(f"8 lógica idéntica (status {estado}, reintento={para_reintento})",
                  (_estado_dte_bloquea(d, para_reintento) is None) ==
                  (_estado_dte_bloquea(d, para_reintento, _SUST_FACTURA, _DOC_FACTURA) is None))

        # ═══ 9 · N° DE GUÍA EN PAPEL con un documento electrónico en Wasabil ═══════
        # Estado: rechazo CONFIRMADO por el SII (status 4) CON uuid y sin claim. El
        # despacho no queda preso (eso sigue valiendo), pero el N° del papel sólo se cita
        # cuando CONSTA que no hay una 52 emitida con esta referencia.
        global LISTADO, LISTADO_FALLA
        desp.numero_guia = "900123"      # folio de guía en papel (numérico, legítimo)
        db.commit()
        dte.status_id = STATUS_FALLIDO
        dte.uuid = "uuid-rechazo-confirmado"
        dte.folio = None
        dte.en_vuelo_desde = None
        db.commit()
        ref = f"{MARK}-D"                # el N° de despacho es la referencia interna

        # (a) el listado responde y NO hay emitidos → el N° del papel es legítimo
        LISTADO, LISTADO_FALLA = [], None
        folio, _f, problema, _aud = _guia_referencia_de_factura(db, fac)
        check("9a listado SANO sin emitidos: el N° del papel se cita (no queda preso)",
              folio == "900123" and problema is None, (folio, problema))

        # (b) Wasabil TIENE una 52 emitida con esta referencia → BLOQUEA nombrando el folio
        LISTADO = [{"uuid": "u-otra", "status_id": STATUS_EMITIDO,
                    "invoice_reference": ref, "folio": "52777"}]
        folio, _f, problema, _aud = _guia_referencia_de_factura(db, fac)
        check("9b con una 52 EMITIDA en Wasabil: NO se cita el N° tecleado a mano",
              folio is None and problema is not None and "52777" in problema,
              (folio, problema))
        # y ninguna declaración humana levanta el caso (b)
        folio, _f, problema, _aud = _guia_referencia_de_factura(db, fac, ref)
        check("9b la declaración humana NO levanta el bloqueo cuando CONSTA el emitido",
              folio is None and problema is not None and "52777" in problema,
              (folio, problema))

        # (c) el listado NO permite concluir (405 del API real) → FALLA CERRADO
        LISTADO = []
        LISTADO_FALLA = wasabil_client.WasabilError(
            "Wasabil respondió 405 en GET /documents", ambiguo=True)
        folio, _f, problema, _aud = _guia_referencia_de_factura(db, fac)
        check("9c listado caído: NO se cita el N° del papel (fail closed)",
              folio is None and problema is not None and "900123" in problema,
              (folio, problema))
        check("9c el 409 dice QUÉ mirar (la referencia exacta) y cómo declararlo",
              problema is not None and ref in problema
              and "verificado_sin_guia_electronica" in problema, problema)
        # …y la salida humana AUDITADA lo destraba, con rastro
        folio, _f, problema, auditoria = _guia_referencia_de_factura(db, fac, ref)
        check("9c con la verificación humana EXACTA se puede facturar el papel",
              folio == "900123" and problema is None, (folio, problema))
        check("9c y queda línea de auditoría nombrando la referencia declarada",
              auditoria is not None and ref in auditoria
              and "VERIFICACIÓN HUMANA" in auditoria, auditoria)
        # una declaración que NO es la referencia exacta no sirve (teclearla es la prueba)
        for mentira in ("true", "si", ref[:-1], ref + "9", ""):
            folio, _f, problema, _aud = _guia_referencia_de_factura(db, fac, mentira)
            check(f"9c declaración inexacta '{mentira}' NO destraba",
                  folio is None and problema is not None, (mentira, folio))
        # lista TRUNCADA (sin 405): tampoco permite concluir
        LISTADO_FALLA = None
        _completa = wasabil_client.buscar_documentos
        wasabil_client.buscar_documentos = lambda search: ([], False)
        folio, _f, problema, _aud = _guia_referencia_de_factura(db, fac)
        check("9c lista TRUNCADA: tampoco se cita el N° del papel",
              folio is None and problema is not None, (folio, problema))
        wasabil_client.buscar_documentos = _completa
        # CONTROL: sin uuid (nunca nació documento electrónico) el papel vale SIEMPRE,
        # aunque el listado esté caído — el guard no bloquea guías de papel puras.
        dte.uuid = None; db.commit()
        LISTADO_FALLA = wasabil_client.WasabilError("405", ambiguo=True)
        folio, _f, problema, _aud = _guia_referencia_de_factura(db, fac)
        check("9d control: guía SIN uuid (papel puro) sigue citándose con el listado caído",
              folio == "900123" and problema is None, (folio, problema))
        LISTADO_FALLA = None
    finally:
        wasabil_client.buscar_documentos = _orig_buscar
        wasabil_client.obtener_documento = _orig_obtener
        _limpiar(db)
        db.close()

    assert not _fails, f"{len(_fails)} fallos: {_fails}"


def test_guia_en_proceso_no_cae_al_numero_manual(): run()


if __name__ == "__main__":
    run()
    print("TODO OK")
