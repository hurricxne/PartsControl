"""Re-refutación · CRÍTICO-2 (MonzaParts no tenía cinturón) + MEDIO-4 + MEDIO-5.

QUÉ SE MIDIÓ Y POR QUÉ EXISTE ESTA SUITE
El informe de re-refutación reprodujo, por endpoints y con el listado de Wasabil SANO (sin
necesidad de ninguna falla), que `reintentar_guia` y su gemelo de facturas iban de «status 4
confirmado» DIRECTO a re-emitir:

    FAIL | 1c REINTENTAR con una 52 REAL ya emitida (52777) y el listado SANO
         -> {'http': 200, 'documentos_nuevos': 1, ...}
    ··· 1c Monza: DOBLE EMISIÓN 52 con issue=True — no existe el cinturón por referencia

Estaba contenido SOLO porque MonzaParts no ha hecho su primera emisión real. El cinturón
existía en Grupo AM (`_abortar_si_ya_hay_documento_emitido`) y no se había portado.

EL PRINCIPIO QUE MANDA ACÁ (y que la versión de GA violaba): un guard que protege un
documento IRREVERSIBLE y que NO PUEDE CONCLUIR debe FALLAR CERRADO. «No lo vi» no es «no
existe». El cinturón y el rescate preguntan a la MISMA fuente (`buscar_documentos`): si el
rescate falla cerrado (502) y el cinturón falla abierto, el cinturón desaparece justo cuando
más se necesita — y como `GET /documents` responde 405 en el API real, un cinturón «best
effort» no bloquearía NUNCA en producción. Un guard inerte es peor que ninguno.

Por eso los DESENLACES SON TRES, y esta suite prueba los tres por separado:
  1. consta que NO hay documento emitido  → se sigue (sin sobre-bloqueo)
  2. consta que SÍ hay                    → 409 con el folio, ABSOLUTO
  3. no se puede concluir                 → 409 pidiendo verificación humana, y la
                                            autorización explícita de una persona es la
                                            ÚNICA salida (jamás un fail open automático)

CONTROLES CON PRECONDICIÓN AFIRMADA (la lección de la refutación): el control «con el
listado caído se re-emite» de la suite de GA no probaba nada, porque en su escenario ya se
había quitado el documento emitido. Acá cada caso de «no se puede concluir» se corre DOS
veces —con el documento EMITIDO presente y sin él— y la presencia se AFIRMA antes de medir.

Cero introspección de texto para detectar conducta: todo por HTTP contra los routers reales,
y los asserts anti doble emisión van por DELTA de documentos creados. Wasabil SIMULADO por
monkeypatch del client MONZA: `issue` nunca sale del proceso, no se toca ningún documento
real del dueño (guías 136/137 y factura 116 intactas).

Corre con:  ./venv/bin/python -m pytest monza_wasabil_dte/tests/test_ra_cinturon_mz.py -q
"""
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from database import SessionLocal  # noqa: E402
from monza_models import MonzaDespacho  # noqa: E402
from monza_wasabil_dte import client as monza_client  # noqa: E402
from monza_wasabil_dte import router as mwr  # noqa: E402
from monza_wasabil_dte.models import (  # noqa: E402
    MonzaWasabilDte, STATUS_EMITIDO, STATUS_FALLIDO,
)
from monza_wasabil_dte.tests.factura_harness import (  # noqa: E402
    Checker, FakeWasabil, crear_venta, dte_de_factura, dte_guia, facturas_de,
    limpiar, montar_app, verificar_limpieza,
)

# MARK corto: MonzaCotizacion.numero es String(20) y el número es f"{MARK}-COT-{n}".
MARK = "__MWCINT__"
# id=None a propósito: `monza_wasabil_dte.usuario_id` tiene FK a users.id, y un id
# inventado hace fallar el INSERT del claim con IntegrityError — que el router traduce a
# «Ya existe una emisión para este despacho», un 409 que parecería del anti doble emisión.
CURRENT = {"empresa": "automotriz", "id": None}

GUIAS = "/api/monza/wasabil/despachos"
FACTURAS = "/api/monza/wasabil/facturas"
CONFIRMAR = "confirmo_sin_documento_emitido=true"

client = montar_app(CURRENT)
check = Checker()


class FakeCinturon(FakeWasabil):
    """Fake ADVERSO mínimo para el cinturón. Tres capacidades que el fake compartido no
    tiene y sin las cuales el daño no se ve:

      · `buscar_falla`: `buscar_documentos` LEVANTA (así se simula el `GET /documents` que
        responde 405 en el API real, o cualquier caída de red) — el desenlace «no se puede
        concluir».
      · `busquedas`: cuenta las consultas al listado, para probar por DELTA que el camino
        feliz de la PRIMERA emisión no pregunta (no se puede bricear con un 405) y que
        nunca se busca con `search=""`.
      · `docs_buscables` con VARIOS documentos de la misma referencia y folios DISTINTOS:
        el estado normal tras un reintento es el rechazado viejo + el nuevo, y si el
        cinturón eligiera «el primero» el folio ajeno pasaría desapercibido.
    """

    def __init__(self, mark: str):
        super().__init__(mark)
        self.buscar_falla = None
        self.busquedas: list = []

    def install(self):
        super().install()
        monza_client.buscar_documentos = self._buscar

    def _buscar(self, search):
        self.busquedas.append(search)
        if self.buscar_falla:
            raise self.buscar_falla
        return list(self.docs_buscables), self.busqueda_completa

    def emitido(self, *, folio, referencia, uuid=None):
        """Documento EMITIDO que Wasabil conserva con esa referencia (lo que el cinturón
        tiene que encontrar). Devuelve el dict para poder AFIRMAR la precondición."""
        doc = {"uuid": uuid or f"u-emitido-{folio}", "status_id": STATUS_EMITIDO,
               "folio": folio, "invoice_reference": referencia}
        self.docs_buscables.append(doc)
        return doc

    def reset(self):
        self.docs_buscables = []
        self.busqueda_completa = True
        self.buscar_falla = None
        self.crear_falla = None
        self.status_respuesta = 2
        self.estado_final = STATUS_EMITIDO
        self.folio_emitido = "9001"
        self.busquedas = []


fake = FakeCinturon(MARK)
fake.install()

CAIDA = monza_client.WasabilError(
    "Wasabil respondió 405 al listar documentos (GET /documents)", ambiguo=True)


def _dte_de(db, desp_id):
    """Fila DTE 52 del despacho, releída FRESCA (el test escribe por HTTP)."""
    db.rollback()  # la sesión del test tiene su propio snapshot REPEATABLE READ
    return (db.query(MonzaWasabilDte)
            .filter(MonzaWasabilDte.despacho_id == desp_id,
                    MonzaWasabilDte.tipo_dte == 52).first())


def _guia_rechazada_con_uuid(db):
    """Escenario BASE de las guías: la fila dice «el SII lo rechazó» y trae uuid, que es
    exactamente el estado desde el que el botón Reintentar re-emitía. El despacho queda EN
    PREPARACIÓN (único estado del que se emite)."""
    cot, desp, _i1, _i2 = crear_venta(db, MARK, estado_despacho="en_preparacion")
    db.refresh(desp)
    dte_guia(db, desp, status_id=STATUS_FALLIDO, uuid="u-rechazado", folio=None,
             en_vuelo_desde=None, error="rechazada por el SII")
    fake.estado_final = STATUS_FALLIDO   # estado_documento(uuid) CONFIRMA el rechazo
    return cot, desp


def _factura_rechazada_con_uuid(db):
    """Escenario BASE de las facturas: factura local creada SIN folio (el POST a Wasabil
    falló de forma NO ambigua) y su fila DTE puesta en «rechazado CON uuid»."""
    cot, desp, _i1, _i2 = crear_venta(db, MARK, numero_guia_manual="9501")
    fake.crear_falla = monza_client.WasabilError("conexión rechazada", ambiguo=False)
    client.post(f"{FACTURAS}/emitir",
                json={"cotizacion_id": cot.id, "despacho_id": desp.id})
    fake.crear_falla = None
    db.rollback()
    factura_id = facturas_de(db, cot.id)[0].id
    fila = dte_de_factura(db, factura_id)
    fila.uuid = "u-fac-rechazado"
    fila.status_id = STATUS_FALLIDO
    fila.en_vuelo_desde = None
    db.commit()
    fake.estado_final = STATUS_FALLIDO
    return cot, desp, factura_id


def run():
    db = SessionLocal()
    fake.install()   # anti-flaky: la última instalación a nivel de módulo gana
    fake.reset()
    limpiar(db, MARK)
    try:
        CURRENT["empresa"] = "automotriz"

        # ═══ G1 · CRÍTICO-2: listado SANO con una 52 REAL ya emitida ════════════════
        # La fila dice status 4 CON uuid y `estado_documento(uuid)` lo confirma… pero ese
        # uuid es el del intento RECHAZADO: Wasabil conserva OTRO documento EMITIDO con la
        # MISMA referencia (el N° de despacho). La pregunta correcta es por REFERENCIA.
        fake.reset()
        cot, desp = _guia_rechazada_con_uuid(db)
        doc_real = fake.emitido(folio="52777", referencia=desp.numero)
        check("G1-pre PRECONDICIÓN: Wasabil TIENE una 52 emitida con esta referencia",
              doc_real in fake.docs_buscables and doc_real["folio"] == "52777",
              fake.docs_buscables)
        creados = len(fake.creados)
        r = client.post(f"{GUIAS}/{desp.id}/reintentar")
        check("G1 reintentar con una 52 ya EMITIDA (listado sano): 409",
              r.status_code == 409, (r.status_code, r.text))
        check("G1-bis CERO documentos nuevos al SII (era la DOBLE EMISIÓN real)",
              len(fake.creados) == creados, len(fake.creados) - creados)
        check("G1-ter el 409 nombra el FOLIO del documento que ya existe (52777)",
              "52777" in r.text, r.text)
        d = _dte_de(db, desp.id)
        check("G1-quater el cinturón NO repara la fila (dos verdades en pugna: mira un humano)",
              d.status_id == STATUS_FALLIDO and d.uuid == "u-rechazado" and d.folio is None,
              (d.status_id, d.uuid, d.folio))
        limpiar(db, MARK)

        # ═══ G2 · NO SE PUEDE CONCLUIR **con el documento EMITIDO presente** ════════
        # La combinación que hace daño y que la suite de GA nunca probaba: listado caído
        # (405 en el API real) MIENTRAS Wasabil tiene el documento emitido. Un cinturón
        # best effort re-emite acá: es el CRÍTICO-1 completo.
        fake.reset()
        cot, desp = _guia_rechazada_con_uuid(db)
        doc_real = fake.emitido(folio="52888", referencia=desp.numero)
        fake.buscar_falla = CAIDA
        check("G2-pre PRECONDICIÓN: el documento EMITIDO existe, pero el listado no responde",
              doc_real in fake.docs_buscables and fake.buscar_falla is not None,
              (fake.docs_buscables, str(fake.buscar_falla)))
        creados = len(fake.creados)
        r = client.post(f"{GUIAS}/{desp.id}/reintentar")
        check("G2 listado caído + 52 EMITIDA: 409 (falla CERRADO)",
              r.status_code == 409, (r.status_code, r.text))
        check("G2-bis CERO documentos nuevos al SII",
              len(fake.creados) == creados, len(fake.creados) - creados)
        limpiar(db, MARK)

        # ═══ G3 · NO SE PUEDE CONCLUIR **sin documento emitido**: TAMBIÉN bloquea ═══
        # Éste es el invariante que la ronda anterior tenía al revés («best effort: con el
        # listado caído se re-emite»). Sin poder mirar, el sistema NO SABE en cuál de los
        # dos mundos está — G2 y G3 son indistinguibles desde acá. Por eso el desenlace
        # tiene que ser el mismo: bloquear y pedir que una persona mire.
        fake.reset()
        cot, desp = _guia_rechazada_con_uuid(db)
        fake.buscar_falla = CAIDA
        check("G3-pre PRECONDICIÓN: no hay ningún documento emitido sembrado",
              fake.docs_buscables == [], fake.docs_buscables)
        creados = len(fake.creados)
        r = client.post(f"{GUIAS}/{desp.id}/reintentar")
        check("G3 listado caído SIN emitidos: 409 igual (no se re-emite a ciegas)",
              r.status_code == 409, (r.status_code, r.text))
        check("G3-bis CERO documentos nuevos al SII",
              len(fake.creados) == creados, len(fake.creados) - creados)
        check("G3-ter el 409 dice qué referencia revisar en app.wasabil.com",
              desp.numero in r.text, r.text)

        # …y la ÚNICA salida es que una PERSONA se haga cargo (misma fila, mismo estado).
        log = logging.getLogger("monza_wasabil_dte")
        registros: list = []

        class _Captura(logging.Handler):
            def emit(self, record):
                registros.append(record)

        handler = _Captura()
        log.addHandler(handler)
        try:
            r = client.post(f"{GUIAS}/{desp.id}/reintentar?{CONFIRMAR}")
            check("G4 con la autorización explícita del humano SÍ re-emite (delta 1)",
                  r.status_code == 200 and len(fake.creados) == creados + 1,
                  (r.status_code, len(fake.creados) - creados, r.text))
            check("G4-bis y la autorización queda REGISTRADA (1 WARNING en el log)",
                  len(registros) == 1 and registros[0].levelno == logging.WARNING,
                  [(x.levelno, x.getMessage()) for x in registros])
        finally:
            log.removeHandler(handler)
        limpiar(db, MARK)

        # ═══ G5 · la autorización humana NO levanta el bloqueo PROBADO ══════════════
        # Desenlace 2 (consta que sí hay un emitido): el daño ya existe y re-emitir lo
        # triplica. Ninguna confirmación puede pasar por encima de eso.
        fake.reset()
        cot, desp = _guia_rechazada_con_uuid(db)
        fake.emitido(folio="52999", referencia=desp.numero)
        creados = len(fake.creados)
        r = client.post(f"{GUIAS}/{desp.id}/reintentar?{CONFIRMAR}")
        check("G5 con documento emitido PROBADO, la confirmación humana NO desbloquea: 409",
              r.status_code == 409 and len(fake.creados) == creados,
              (r.status_code, len(fake.creados) - creados, r.text))
        limpiar(db, MARK)

        # ═══ G6 · lista TRUNCADA (paginación): no vimos el emitido, pero no vimos todo ═
        fake.reset()
        cot, desp = _guia_rechazada_con_uuid(db)
        fake.busqueda_completa = False        # el emitido vive en una página que no se leyó
        check("G6-pre PRECONDICIÓN: la búsqueda se declara INCOMPLETA y vino vacía",
              fake.busqueda_completa is False and fake.docs_buscables == [],
              (fake.busqueda_completa, fake.docs_buscables))
        creados = len(fake.creados)
        r = client.post(f"{GUIAS}/{desp.id}/reintentar")
        check("G6 lista truncada: 409 y CERO documentos nuevos",
              r.status_code == 409 and len(fake.creados) == creados,
              (r.status_code, len(fake.creados) - creados, r.text))
        limpiar(db, MARK)

        # ═══ G7 · CONTROL anti sobre-bloqueo: el reintento NORMAL sigue funcionando ══
        # Desenlace 1 (búsqueda completa y sin emitidos) = el rechazo del SII de todos los
        # días: se corrige el dato y se reintenta. Si esto bloqueara, el cinturón sería
        # inútil por otro lado (nadie podría reintentar nunca).
        fake.reset()
        cot, desp = _guia_rechazada_con_uuid(db)
        creados = len(fake.creados)
        r = client.post(f"{GUIAS}/{desp.id}/reintentar")
        check("G7 CONTROL: listado sano y sin emitidos → SÍ re-emite (delta 1)",
              r.status_code == 200 and len(fake.creados) == creados + 1,
              (r.status_code, len(fake.creados) - creados, r.text))
        limpiar(db, MARK)

        # ═══ G8 · match EXACTO: una referencia PARECIDA no bloquea ni aporta folio ═══
        # Los TRES falsos positivos que hay que descartar de una, porque cada relajación
        # del match tiene su propia forma (el mutante que sobrevivió en GA era justo éste):
        #   · SUPERSTRING  `DSP-…-7` vs `DSP-…-79`     → lo agarraría un match por substring
        #   · PREFIJADA    `OC 4501 · DSP-…-7`          → lo agarraría un `endswith`
        #   · PREFIJO      `DSP-…-` (el propio recortado)
        # Con cualquiera de esas relajaciones, el documento de OTRO despacho bloquearía
        # este reintento — o peor: su folio se adoptaría como si fuera de esta venta. El
        # cinturón y el rescate comparten esta definición (`_coincide_referencia`).
        fake.reset()
        cot, desp = _guia_rechazada_con_uuid(db)
        fake.emitido(folio="52111", referencia=f"{desp.numero}9")             # SUPERSTRING
        fake.emitido(folio="52222", referencia=f"OC 4501 · {desp.numero}")    # PREFIJADA
        fake.emitido(folio="52333", referencia=desp.numero[:-1])              # PREFIJO
        creados = len(fake.creados)
        r = client.post(f"{GUIAS}/{desp.id}/reintentar")
        check("G8 referencias PARECIDAS (superstring/prefijada/prefijo) no bloquean: re-emite",
              r.status_code == 200 and len(fake.creados) == creados + 1,
              (r.status_code, len(fake.creados) - creados, r.text))
        d = _dte_de(db, desp.id)
        check("G8-bis y no se adoptó ningún folio ajeno",
              (d.folio or "") not in ("52111", "52222", "52333"), d.folio)
        limpiar(db, MARK)

        # ═══ G9 · sin ANCLA no hay pregunta posible → fail closed, y nunca search="" ══
        # Se mide a DOS niveles a propósito. Por HTTP el desenlace es el mismo con o sin
        # este guard (`_preparar_emision` también bloquea un despacho sin N° interno), así
        # que ese check documenta la conducta pero NO discrimina: el que discrimina es el
        # de la función, porque sin el guard el cinturón buscaría en Wasabil con search=""
        # — una consulta que devuelve documentos AJENOS y con la que el cinturón dejaría de
        # significar nada.
        fake.reset()
        cot, desp = _guia_rechazada_con_uuid(db)
        db.query(MonzaDespacho).filter(MonzaDespacho.id == desp.id).update(
            {"numero": None}, synchronize_session=False)
        db.commit()
        creados, busquedas = len(fake.creados), len(fake.busquedas)
        r = client.post(f"{GUIAS}/{desp.id}/reintentar")
        check("G9 despacho sin N° interno (sin ancla): 409 y CERO documentos nuevos",
              r.status_code == 409 and len(fake.creados) == creados,
              (r.status_code, len(fake.creados) - creados, r.text))
        bloqueo_sin_ancla, status_sin_ancla = False, None
        try:
            mwr._abortar_si_ya_hay_documento_emitido("   ", "guía")
        except Exception as e:      # HTTPException de FastAPI
            bloqueo_sin_ancla, status_sin_ancla = True, getattr(e, "status_code", None)
        check("G9-bis la función misma BLOQUEA (409) cuando la referencia viene vacía",
              bloqueo_sin_ancla and status_sin_ancla == 409,
              (bloqueo_sin_ancla, status_sin_ancla))
        check("G9-ter y JAMÁS se busca en Wasabil con search vacío",
              len(fake.busquedas) == busquedas, fake.busquedas[busquedas:])
        limpiar(db, MARK)

        # ═══ G10 · la PRIMERA emisión no consulta el listado (no se puede bricear) ═══
        # El cinturón vive SOLO en el reintento a propósito: en la primera emisión no hay
        # nada que re-emitir (el ancla local es única por despacho) y un cinturón acá
        # dejaría el módulo INUTILIZABLE en producción, donde el listado da 405.
        fake.reset()
        cot, desp, _i1, _i2 = crear_venta(db, MARK, estado_despacho="en_preparacion")
        db.refresh(desp)
        busquedas = len(fake.busquedas)
        r = client.post(f"{GUIAS}/{desp.id}/emitir")
        check("G10 la primera emisión funciona sin preguntar por el listado",
              r.status_code == 200 and len(fake.busquedas) == busquedas,
              (r.status_code, fake.busquedas[busquedas:], r.text))
        limpiar(db, MARK)

        # ═══ M4 · DOS emitidos con la misma referencia: bloquea Y DEJA RASTRO ════════
        # El bloqueo ya existía (el rescate aborta), pero `except WasabilError: pass` se
        # comía el motivo: la fila quedaba sin folio y SIN decir por qué, así que el humano
        # al que se le pide intervenir no tenía con qué. El aviso viaja hasta `dte.error`
        # con los DOS folios, y sobrevive al `error = None` del documento emitido.
        fake.reset()
        fake.status_respuesta = STATUS_EMITIDO   # el POST responde EMITIDO…
        fake.sin_uuid_en_post = True             # …y PELADO: sin uuid ni folio
        monza_client.crear_documento = lambda payload: (
            fake.creados.append(payload) or {"status_id": STATUS_EMITIDO})
        cot, desp, _i1, _i2 = crear_venta(db, MARK, estado_despacho="en_preparacion")
        db.refresh(desp)
        fake.emitido(folio="53001", referencia=desp.numero)
        fake.emitido(folio="53002", referencia=desp.numero)
        r = client.post(f"{GUIAS}/{desp.id}/emitir")
        d = _dte_de(db, desp.id)
        check("M4 con DOS emitidos no se elige folio (la fila queda sin folio)",
              r.status_code == 200 and (d.folio or "") == "", (r.text, d.folio))
        check("M4-bis y el error de la fila NOMBRA LOS DOS FOLIOS (rastro para el humano)",
              "53001" in (d.error or "") and "53002" in (d.error or ""), d.error)
        fake.install()   # restaura crear_documento del fake
        limpiar(db, MARK)

        # ═══ M5 · status 4 CON uuid no habilita el N° de guía tecleado a mano ═══════
        # Mismo estado que explota el CRÍTICO-2: la fila dice «rechazada» mientras Wasabil
        # puede tener una 52 EMITIDA con la misma referencia. Antes la 33 salía citando el
        # N° del papel (numérico y legítimo a primera vista). Y la SALIDA existe: cuando el
        # folio real queda registrado, la 33 lo cita.
        fake.reset()
        cot, desp, _i1, _i2 = crear_venta(db, MARK, numero_guia_manual="9345")
        dte_guia(db, desp, status_id=STATUS_FALLIDO, uuid="u-rechazado", folio=None,
                 en_vuelo_desde=None, error="rechazada por el SII")
        p = client.post(f"{FACTURAS}/preview",
                        json={"cotizacion_id": cot.id, "despacho_id": desp.id}).json()
        refs52 = [x["folio"] for x in p.get("referencias", []) if x["tipo"] == "52"]
        check("M5 rechazo CON uuid: la 33 NO cita el N° tecleado a mano y no puede emitir",
              p["puede_emitir"] is False and refs52 == [], (p["problemas"], refs52))
        creados = len(fake.creados)
        r = client.post(f"{FACTURAS}/emitir",
                        json={"cotizacion_id": cot.id, "despacho_id": desp.id})
        check("M5-bis emitir esa 33: 409 y NADA sale al SII",
              r.status_code == 409 and len(fake.creados) == creados, r.text)
        # SALIDA (lo que hace que el bloqueo no sea un callejón): con el folio real
        # registrado, la misma 33 se puede emitir y cita el folio del SII.
        db.rollback()
        fila = _dte_de(db, desp.id)
        fila.status_id = STATUS_EMITIDO
        fila.folio = "52777"
        db.commit()
        p = client.post(f"{FACTURAS}/preview",
                        json={"cotizacion_id": cot.id, "despacho_id": desp.id}).json()
        refs52 = [x["folio"] for x in p.get("referencias", []) if x["tipo"] == "52"]
        check("M5-ter con el folio real registrado la 33 se desbloquea y cita 52777",
              p["puede_emitir"] is True and refs52 == ["52777"], (p["problemas"], refs52))
        limpiar(db, MARK)

        # ═══ F1 · el gemelo de FACTURAS: dos ventas ante el SII ═════════════════════
        fake.reset()
        cot, desp, factura_id = _factura_rechazada_con_uuid(db)
        ref_fac = mwr._referencia_interna_factura(factura_id)
        doc_real = fake.emitido(folio="F900999", referencia=ref_fac)
        check("F1-pre PRECONDICIÓN: Wasabil TIENE una 33 emitida con FACT-<id>",
              doc_real in fake.docs_buscables and doc_real["invoice_reference"] == ref_fac,
              fake.docs_buscables)
        creados = len(fake.creados)
        r = client.post(f"{FACTURAS}/{factura_id}/reintentar")
        check("F1 reintentar con una 33 ya EMITIDA (listado sano): 409",
              r.status_code == 409, (r.status_code, r.text))
        check("F1-bis CERO DTE 33 nuevos al SII (eran DOS ventas por lo mismo)",
              len(fake.creados) == creados, len(fake.creados) - creados)
        check("F1-ter el 409 nombra el folio existente (F900999)",
              "F900999" in r.text, r.text)
        limpiar(db, MARK)

        # ═══ F2 · facturas: no se puede concluir CON el documento emitido presente ═══
        fake.reset()
        cot, desp, factura_id = _factura_rechazada_con_uuid(db)
        doc_real = fake.emitido(folio="F900777",
                                referencia=mwr._referencia_interna_factura(factura_id))
        fake.buscar_falla = CAIDA
        check("F2-pre PRECONDICIÓN: la 33 emitida existe y el listado no responde",
              doc_real in fake.docs_buscables and fake.buscar_falla is not None,
              (fake.docs_buscables, str(fake.buscar_falla)))
        creados = len(fake.creados)
        r = client.post(f"{FACTURAS}/{factura_id}/reintentar")
        check("F2 listado caído + 33 EMITIDA: 409 y CERO documentos nuevos",
              r.status_code == 409 and len(fake.creados) == creados,
              (r.status_code, len(fake.creados) - creados, r.text))
        limpiar(db, MARK)

        # ═══ F3 · facturas: sin poder concluir tampoco re-emite, y la salida es humana ═
        fake.reset()
        cot, desp, factura_id = _factura_rechazada_con_uuid(db)
        fake.buscar_falla = CAIDA
        creados = len(fake.creados)
        r = client.post(f"{FACTURAS}/{factura_id}/reintentar")
        check("F3 listado caído SIN emitidos: 409 igual (no se re-emite a ciegas)",
              r.status_code == 409 and len(fake.creados) == creados,
              (r.status_code, len(fake.creados) - creados, r.text))
        r = client.post(f"{FACTURAS}/{factura_id}/reintentar?{CONFIRMAR}")
        check("F3-bis con la autorización explícita del humano SÍ re-emite (delta 1)",
              r.status_code == 200 and len(fake.creados) == creados + 1,
              (r.status_code, len(fake.creados) - creados, r.text))
        limpiar(db, MARK)

        # ═══ F4 · facturas: la confirmación no levanta el bloqueo PROBADO ═══════════
        fake.reset()
        cot, desp, factura_id = _factura_rechazada_con_uuid(db)
        fake.emitido(folio="F900555",
                     referencia=mwr._referencia_interna_factura(factura_id))
        creados = len(fake.creados)
        r = client.post(f"{FACTURAS}/{factura_id}/reintentar?{CONFIRMAR}")
        check("F4 con la 33 emitida PROBADA, la confirmación NO desbloquea: 409",
              r.status_code == 409 and len(fake.creados) == creados,
              (r.status_code, len(fake.creados) - creados, r.text))
        limpiar(db, MARK)

        # ═══ F5 · CONTROL anti sobre-bloqueo del gemelo de facturas ═════════════════
        fake.reset()
        cot, desp, factura_id = _factura_rechazada_con_uuid(db)
        creados = len(fake.creados)
        r = client.post(f"{FACTURAS}/{factura_id}/reintentar")
        check("F5 CONTROL: listado sano y sin emitidos → SÍ re-emite (delta 1)",
              r.status_code == 200 and len(fake.creados) == creados + 1,
              (r.status_code, len(fake.creados) - creados, r.text))
        limpiar(db, MARK)

    finally:
        fake.reset()
        fake.install()
        limpiar(db, MARK)
        db.close()
        verificar_limpieza(MARK)

    check.finish()


def test_monza_cinturon_anti_doble_emision():
    run()


if __name__ == "__main__":
    run()
