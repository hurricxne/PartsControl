"""Registrar a mano el folio de un documento EMITIDO que llegó sin folio (MonzaParts).

EL CALLEJÓN QUE ABRE
    `_completar_documento_emitido` falla ABIERTO a propósito: un error de CONSULTA no debe
    convertirse en el fracaso de una emisión que SÍ salió. Su precio es una fila con
    status 3 y folio NULL, y ese estado era PERMANENTE — el sondeo no lo repara solo,
    «Reintentar» responde 409 (correcto: re-emitir sería un SEGUNDO documento tributario
    REAL) y el N° manual no se puede editar (guard anti-pisado). La única salida era un
    UPDATE a mano en la base de datos.

SONDAS DE PODER DISCRIMINANTE
    Cada regla se ejercita con el estado ADVERSO, no con el cómodo:
      · §3 Wasabil responde un folio DISTINTO del tecleado (no un fake complaciente).
      · §4 Wasabil dice que NO hay ningún emitido, mientras la fila local dice que sí:
           dos verdades en pugna. Si el helper no distinguiera "contradice" de "no pude",
           esta sección pasaría igual y el guard sería inerte.
      · §5 DOS documentos EMITIDOS con la misma referencia = doble emisión ya ocurrida.
      · §6 folio ya registrado y DISTINTO: el estado en que pisar cuesta un folio real.
    Ninguna sección verifica leyendo el código fuente: todas ejercitan comportamiento.

Sin red (FakeWasabil pisa SOLO el client de Monza) y sin emitir nada: este módulo no
llama jamás a crear_documento. Datos con MARK propio y limpieza verificada al final.

Corre con:  ./venv/bin/python -m pytest monza_wasabil_dte/tests/test_registrar_folio.py -q
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from database import SessionLocal  # noqa: E402
from monza_models import MonzaDespacho  # noqa: E402
from monza_wasabil_dte import client as monza_client  # noqa: E402
from monza_wasabil_dte.models import (  # noqa: E402
    MonzaWasabilDte, STATUS_EMITIDO, STATUS_PROCESANDO, STATUS_FALLIDO,
)
from monza_wasabil_dte.router import (  # noqa: E402
    _folio_dte_valido, _folio_confirmado_por_wasabil, _referencia_interna_guia,
)
from monza_wasabil_dte.service import FOLIO_REF_MAX  # noqa: E402
from monza_wasabil_dte.tests.factura_harness import (  # noqa: E402
    Checker, FakeWasabil, crear_venta, dte_guia, limpiar, montar_app, verificar_limpieza,
)

MARK = "__MZRF__"
CURRENT = {"empresa": "automotriz", "id": None}

fake = FakeWasabil(MARK)
check = Checker()
client = montar_app(CURRENT)

BASE = "/api/monza/wasabil"


def _doc(ref, status, folio=None):
    """Documento como lo devuelve el listado de Wasabil (la forma que lee el cinturón)."""
    return {"invoice_reference": ref, "status_id": status, "folio": folio}


def _registrar(desp_id, folio, confirmo=None):
    return client.post(f"{BASE}/despachos/{desp_id}/registrar-folio",
                       params={"folio": folio,
                               "confirmo_folio": confirmo if confirmo is not None else folio})


def run():
    db = SessionLocal()
    fake.install()   # re-instalar el fake propio (la última instalación gana: anti-flaky)
    limpiar(db, MARK)
    try:
        CURRENT["empresa"] = "automotriz"

        # ── 1) El validador de folio: la regla del SII, no una aproximación ──────────
        for malo, motivo in [("", "vacío"), ("   ", "espacios"), ("abc", "letras"),
                             ("90-01", "guion"), ("0", "cero"), ("-5", "negativo"),
                             ("٣", "dígito no ASCII (isdigit() dice True)"),
                             ("9" * (FOLIO_REF_MAX + 1), f"más de {FOLIO_REF_MAX} dígitos")]:
            check(f"1 folio inválido rechazado: {motivo}",
                  not _folio_dte_valido(malo), repr(malo))
        check("1z folio válido aceptado", _folio_dte_valido("9001"), "9001")

        # ── 2) Camino feliz: Wasabil CONFIRMA el folio ───────────────────────────────
        cot, desp, _i1, _i2 = crear_venta(db, MARK, numero_guia_manual=None)
        ref = _referencia_interna_guia(db, desp.id)
        dte = dte_guia(db, desp, status_id=STATUS_EMITIDO, folio=None, uuid="uuid-r1")
        fake.docs_buscables = [_doc(ref, STATUS_EMITIDO, "9001")]
        fake.busqueda_completa = True
        creados_antes = len(fake.creados)

        r = _registrar(desp.id, "9001")
        check("2a registrar folio confirmado por Wasabil → 200", r.status_code == 200, r.text)
        check("2b el origen dice que lo confirmó la MÁQUINA, no el operador",
              "confirmado por Wasabil" in (r.json().get("registro_manual") or ""),
              r.json().get("registro_manual"))
        db.expire_all()
        dte = db.query(MonzaWasabilDte).filter(MonzaWasabilDte.id == dte.id).first()
        check("2c el folio quedó escrito en la fila DTE", (dte.folio or "") == "9001", dte.folio)
        desp_fresco = db.query(MonzaDespacho).filter(MonzaDespacho.id == desp.id).first()
        check("2d el folio llegó a despacho.numero_guia (mismo camino que la emisión)",
              (desp_fresco.numero_guia or "") == "9001", desp_fresco.numero_guia)
        check("2e NO se emitió nada (delta de documentos creados == 0)",
              len(fake.creados) == creados_antes, (creados_antes, len(fake.creados)))

        # ── 2-bis) Idempotencia: el doble clic no es un error ────────────────────────
        r = _registrar(desp.id, "9001")
        check("2f re-registrar el MISMO folio es idempotente (200)",
              r.status_code == 200, r.text)
        check("2g y lo dice explícitamente",
              "ya estaba" in (r.json().get("registro_manual") or ""),
              r.json().get("registro_manual"))

        # ── 3) Wasabil dice OTRO folio: no se elige por cuenta propia ────────────────
        cot3, desp3, _a, _b = crear_venta(db, MARK, numero_guia_manual=None)
        ref3 = _referencia_interna_guia(db, desp3.id)
        dte_guia(db, desp3, status_id=STATUS_EMITIDO, folio=None, uuid="uuid-r3")
        fake.docs_buscables = [_doc(ref3, STATUS_EMITIDO, "7777")]
        r = _registrar(desp3.id, "9002")
        check("3a Wasabil dice 7777 y el operador escribió 9002 → 409", r.status_code == 409, r.text)
        check("3b el mensaje nombra los DOS folios (con qué ir a app.wasabil.com)",
              "7777" in r.text and "9002" in r.text, r.text[:200])
        db.expire_all()
        d3 = (db.query(MonzaWasabilDte)
              .filter(MonzaWasabilDte.despacho_id == desp3.id).first())
        check("3c y NO escribió nada", d3.folio is None, d3.folio)

        # ── 4) CONTRADICCIÓN: acá consta emitido y Wasabil no tiene ninguno ──────────
        fake.docs_buscables = []          # búsqueda completa y sin emitidos
        fake.busqueda_completa = True
        r = _registrar(desp3.id, "9002")
        check("4a estado contradictorio → 409 (no se registra sobre una contradicción)",
              r.status_code == 409, r.text)
        check("4b y explica que puede haber un documento duplicado detrás",
              "duplicado" in r.text.lower(), r.text[:220])
        _fm, _motivo, contradice = _folio_confirmado_por_wasabil(ref3)
        check("4c el helper marca contradice=True (no lo confunde con «no pude concluir»)",
              contradice is True, (_fm, _motivo, contradice))

        # ── 5) DOS emitidos con la misma referencia: doble emisión ya ocurrida ───────
        fake.docs_buscables = [_doc(ref3, STATUS_EMITIDO, "7777"),
                               _doc(ref3, STATUS_EMITIDO, "8888")]
        r = _registrar(desp3.id, "7777")
        check("5a dos documentos EMITIDOS con la misma referencia → 409",
              r.status_code == 409, r.text)
        check("5b nombra los dos folios para que un humano los resuelva",
              "7777" in r.text and "8888" in r.text, r.text[:260])

        # ── 6) Nunca se pisa un folio ya registrado con otro distinto ────────────────
        fake.docs_buscables = [_doc(ref, STATUS_EMITIDO, "9001")]
        r = _registrar(desp.id, "9999")
        check("6a folio ya registrado, se intenta otro → 409", r.status_code == 409, r.text)
        db.expire_all()
        dte = db.query(MonzaWasabilDte).filter(MonzaWasabilDte.id == dte.id).first()
        check("6b el folio original SOBREVIVE intacto", (dte.folio or "") == "9001", dte.folio)

        # ── 7) Confirmación explícita y estado del documento ─────────────────────────
        cot7, desp7, _c, _d = crear_venta(db, MARK, numero_guia_manual=None)
        ref7 = _referencia_interna_guia(db, desp7.id)
        d7 = dte_guia(db, desp7, status_id=STATUS_EMITIDO, folio=None, uuid="uuid-r7")
        fake.docs_buscables = [_doc(ref7, STATUS_EMITIDO, "9007")]

        r = _registrar(desp7.id, "9007", confirmo="9008")
        check("7a confirmación distinta del folio → 400", r.status_code == 400, r.text)
        r = _registrar(desp7.id, "abc", confirmo="abc")
        check("7b folio no numérico → 400", r.status_code == 400, r.text)

        d7.status_id = STATUS_PROCESANDO
        db.commit()
        r = _registrar(desp7.id, "9007")
        check("7c documento que NO está emitido → 409 (esto no es un atajo de emisión)",
              r.status_code == 409, r.text)
        d7.status_id = STATUS_FALLIDO
        db.commit()
        r = _registrar(desp7.id, "9007")
        check("7d documento RECHAZADO → 409", r.status_code == 409, r.text)
        d7.status_id = STATUS_EMITIDO
        db.commit()

        # ── 8) Claim vigente: hay una emisión en curso AHORA MISMO ───────────────────
        from datetime import datetime
        d7.en_vuelo_desde = datetime.utcnow()
        db.commit()
        r = _registrar(desp7.id, "9007")
        check("8a con una emisión en vuelo → 409 (no se escribe bajo sus pies)",
              r.status_code == 409, r.text)
        d7.en_vuelo_desde = None
        db.commit()

        # ── 8-bis) LA CARRERA REAL: el claim aparece MIENTRAS se consulta a Wasabil ──
        # La consulta tiene red y tarda; el chequeo del endpoint ya pasó. Se reproduce el
        # estado adverso de verdad: otra sesión (= otro request) marca el claim justo
        # durante la consulta. La re-lectura BAJO LOCK es la única que puede verlo.
        def _consulta_lenta_con_claim(search):
            otra = SessionLocal()
            try:
                fila = (otra.query(MonzaWasabilDte)
                        .filter(MonzaWasabilDte.id == d7.id).first())
                fila.en_vuelo_desde = datetime.utcnow()
                otra.commit()
            finally:
                otra.close()
            return [_doc(ref7, STATUS_EMITIDO, "9007")], True

        monza_client.buscar_documentos = _consulta_lenta_con_claim
        try:
            r = _registrar(desp7.id, "9007")
            check("8b claim nacido DURANTE la consulta → 409 (lo atrapa la re-lectura bajo lock)",
                  r.status_code == 409, f"{r.status_code} {r.text[:180]}")
            db.expire_all()
            d7f = db.query(MonzaWasabilDte).filter(MonzaWasabilDte.id == d7.id).first()
            check("8c y NO se escribió el folio bajo los pies de la emisión",
                  d7f.folio is None, d7f.folio)
        finally:
            monza_client.buscar_documentos = lambda search: (
                list(fake.docs_buscables), fake.busqueda_completa)
        d7.en_vuelo_desde = None
        db.commit()

        # ── 9) «No se puede concluir»: vale la declaración del operador, con rastro ──
        # Wasabil CAÍDO. Decisión documentada: acá NO se bloquea (a diferencia del
        # cinturón que autoriza EMITIR), porque bloquear deja el callejón cerrado para
        # siempre — que es justo el problema que este endpoint resuelve.
        def _cae(_search):
            raise monza_client.WasabilError("listado caído (405)")
        original_buscar = monza_client.buscar_documentos
        monza_client.buscar_documentos = _cae
        try:
            r = _registrar(desp7.id, "9007")
            check("9a con Wasabil caído se acepta la declaración del operador (200)",
                  r.status_code == 200, r.text)
            check("9b pero el origen deja constancia de que la máquina NO lo confirmó",
                  "declarado por el operador" in (r.json().get("registro_manual") or ""),
                  r.json().get("registro_manual"))
        finally:
            monza_client.buscar_documentos = original_buscar

        # ── 10) Lista TRUNCADA: tampoco se puede concluir, mismo trato ───────────────
        cot10, desp10, _e, _f = crear_venta(db, MARK, numero_guia_manual=None)
        dte_guia(db, desp10, status_id=STATUS_EMITIDO, folio=None, uuid="uuid-r10")
        fake.docs_buscables = []
        fake.busqueda_completa = False        # paginación sin agotar
        r = _registrar(desp10.id, "9010")
        check("10a lista truncada → se acepta con declaración (no es una contradicción)",
              r.status_code == 200, r.text)
        check("10b y el motivo dice que la búsqueda quedó incompleta",
              "incompleta" in (r.json().get("registro_manual") or ""),
              r.json().get("registro_manual"))
        fake.busqueda_completa = True

        # ── 11) El despacho sin emisión electrónica no tiene nada que registrar ──────
        cot11, desp11, _g, _h = crear_venta(db, MARK, numero_guia_manual=None)
        r = _registrar(desp11.id, "9011")
        check("11 despacho sin fila DTE → 404", r.status_code == 404, r.text)

        # ── 12) Separación de marcas: esto es de Monza y no toca a MachParts ─────────
        import wasabil_dte.router as ga_router
        import monza_wasabil_dte.router as mz_router
        check("12a MonzaParts tiene su PROPIO _registrar_folio_a_mano",
              mz_router._registrar_folio_a_mano is not ga_router._registrar_folio_a_mano)
        check("12b y su propio validador de folio",
              mz_router._folio_dte_valido.__module__ == "monza_wasabil_dte.router",
              mz_router._folio_dte_valido.__module__)
        check("12c el fake de Monza NO pisó el client de Grupo AM",
              "wasabil_dte.client" in str(ga_router.wasabil.__name__),
              ga_router.wasabil.__name__)

    finally:
        limpiar(db, MARK)
        db.close()
    verificar_limpieza(MARK)
    check.finish()


def test_registrar_folio_monza():
    run()


if __name__ == "__main__":
    run()
