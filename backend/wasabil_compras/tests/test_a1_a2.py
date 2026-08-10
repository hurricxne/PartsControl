"""Fases A1+A2 del libro de compras: espejo idempotente + bandeja de tres cubetas.

LO QUE ESTA SUITE PROTEGE:
  A1 · El barrido es idempotente (N corridas = 1), jamás borra (DESAPARECIDO), no pisa la
       decisión local, y enciende DIVERGENTE si el documento cambió después de decidido.
  A2 · Las cubetas son TRES (está / no está / indeterminado) y el cruce usa el RUT
       canónico — un RUT tecleado distinto ('76513680-6' vs '76.513.680-6') MATCHEA.
       El guard BLOQUEADO impide colgar la comisión del banco a un embarque, la NC no va
       a costo_venta en esta fase, y el activo fijo es una salida válida (hallazgo del
       controller: Steel Ingeniería no cabe en costo/gasto).

SONDAS DE PODER DISCRIMINANTE:
  · §2c re-barrer NO duplica filas (idempotencia real, medida por conteo).
  · §2d/§2e el sweep corre con los upserts YA flusheados (autoflush=False): re-barrer
       lo mismo no deja DESAPARECIDOS falsos pegados en la BD.
  · §3b el documento que deja de venir NO se borra: queda DESAPARECIDO con su decisión.
  · §4c la divergencia se enciende SOLO tras decidir (cambio antes de decidir = normal).
  · §4e-bis la narrativa de divergencia compara como NÚMERO (sin falsos positivos
       str(Decimal) vs str(int)).
  · §5d el mismo RUT en formato distinto cae en la cubeta ESTA (sin canónico caería en
       NO_ESTA y el operador re-cargaría la factura: el doble conteo).

REVISIÓN ADVERSARIAL 2026-08-06 (hallazgos confirmados, ver el arreglo en cada módulo):
  · §10c/§12 (#1) prefill con RUT canónico + folio BLANDO → INDETERMINADO con leyenda.
  · §11 (#4) DESAPARECIDO visible: ?estado=DESAPARECIDO en bandeja y CSV; prefill y
       decisión sobre un desaparecido → 409.
  · §13 (#2/#5) cuadratura en CLP (monto_total_clp) y DESGLOSADA: erp_comparable /
       erp_fuera_libro / libro_ignorados / libro_nc / diferencia_explicada == 0.
  · §14 (#3) lista vacía → cinturón de proporción; (#15) fuera de ventana != DESAPARECIDO;
       doc malformado se espeja con monto NULL sin congelar el barrido; fencing
       anti-solape: el run superado se retira sin escribir.
  · §15 (#17) raw_json deferred; grafo de FKs cerrado (import aislado + create_all).

REVISIÓN ADVERSARIAL 2026-08-08 (la tanda que cerró los agujeros del cruce y las sondas
ciegas — cada una se verificó apagando el arreglo y viendo la suite ROJA):
  · §12c-bis (#26) la advertencia del folio parecido VIAJA al prefill con su leyenda —
       el check 10b-bis aceptaba las tres cubetas y no miraba nunca `cubeta_detalle`.
  · §12d-12f (#11) el TIPO del DTE entra al cruce: la boleta 39 con el mismo RUT y N°
       que una factura registrada ya no se declara «ESTA» (CxP que nunca nacía).
  · §12f-bis + §18a/§18b (#9, el ALTO) el RUT del ERP es texto libre y viene sucio
       ('96.631.520 2'): el canonizador estricto se rendía y con eso la bandeja lo veía
       como faltante Y el anti-duplicado del alta se apagaba → dos CxP pagables.
  · §12g-12j (#14) la duda de una compra sin RUT ni N° se ACOTA a los documentos de su
       mismo monto y solo si esa compra podría ser un DTE: una fila ya no borra la
       cubeta NO_ESTA del libro entero.
  · §13e/§13a-bis (#28) la NC IGNORADA no se resta dos veces en la cuadratura.
  · §16d/§16e (#27) el sanitizador del CSV cubre el espacio inicial y la columna FOLIO.
  · §18c-18e (#6/#12) el anti-duplicado blando mira el TIPO: la boleta N° 05500 y la
       factura N° 5500 del mismo proveedor son dos documentos, y el guard sigue vivo
       para el mismo tipo.

Sin red (fake sobre client._post_query). Datos MARCADOS y limpieza verificada con sesión
nueva. El cruce usa compras REALES de mentira en cont_compra (MARK propio), y §18 usa el
alta REAL de Compras y Pagos (el router se monta acá) porque el anti-duplicado del alta y
la advertencia de la bandeja son las dos mitades del mismo control.

Corre con:  ./venv/bin/python -m pytest wasabil_compras/tests/test_a1_a2.py -q
"""
import csv
import io
import os
import subprocess
import sys
from datetime import date, datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import SessionLocal, engine  # noqa: E402
from auth import get_current_user  # noqa: E402
from models.models import Base, Proveedor  # noqa: E402
import wasabil_compras.client as cli  # noqa: E402
import wasabil_compras.sync as sync_mod  # noqa: E402
from wasabil_compras.models import (  # noqa: E402
    SiiLibroDoc, SiiLibroMatch, SiiLibroReglaRut, SiiLibroSyncRun,
)
from wasabil_compras.router import router as sii_router  # noqa: E402
from wasabil_compras.sync import ejecutar_barrido  # noqa: E402
import embarques_pricing.models  # noqa: E402,F401  (FK cont_compra.emb_pricing_gasto_id)
from compras_contab.models import ContCompra  # noqa: E402
# El alta de compras se monta acá a propósito (§18): el anti-duplicado del alta es la
# otra mitad del cruce de la bandeja — la advertencia del libro y el 409 del alta tienen
# que hablar del mismo caso, y probarlos por separado fue lo que dejó pasar el #9.
from compras_contab.router import router as compras_router  # noqa: E402

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

Base.metadata.create_all(bind=engine, checkfirst=True)

MARK = "test-siilibro"
CURRENT = {"id": 1, "empresa": "mineria"}

app = FastAPI()
app.include_router(sii_router)
app.include_router(compras_router, prefix="/api")   # sondas §18 (anti-dup del alta)
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=CURRENT["id"], empresa=CURRENT["empresa"], email=f"{MARK}@test.invalid")
client = TestClient(app)

API = "/api/sii-libro"
_fails = []

# uuids del MARK: todos arrancan con el prefijo para poder limpiarlos con seguridad.
U = lambda n: f"{MARK}-u{n}"


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _doc(n, *, rut="76.513.680-6", nombre="FASTAIR", folio=None, total=100000,
         signo=1, fecha="2026-07-15", tipo=33):
    return {"uuid": U(n), "document": 900000 + n, "sii_document_type_id": tipo,
            "folio": folio or str(4000 + n), "document_date": fecha, "status_id": 3,
            "trx_sign": signo, "sent_nsubtotal": total, "sent_niva": round(total * 0.19),
            "sent_nexempt": 0, "sent_ntotal": round(total * 1.19),
            "receiver_rut": rut, "receiver_name": f"{MARK} {nombre}",
            "payment_method": "credito", "exchange_status": None,
            "supplier": {"uid": f"sup-{n}"}}


def _envelope(items):
    return {"success": True, "data": {"list": {
        "items": items, "total": len(items), "lastPage": 1}}}


class FakeQuery:
    def __init__(self, items):
        self.items = items

    def __call__(self, body, params=None):
        return _envelope(self.items)


def _limpiar():
    db = SessionLocal()
    try:
        S = "fetch"
        # Los CRUCES del matcher primero: la FK sii_libro_match.doc_id es ON DELETE
        # RESTRICT, así que borrar el documento con un cruce colgando revienta con
        # IntegrityError y la limpieza se cae ENTERA — dejando las filas de esta suite
        # en la BD, que después envenenan la corrida siguiente (y la del matcher, que
        # arma su contexto sobre TODOS los documentos del espejo, no solo los suyos).
        # Esta suite no crea cruces, pero el matcher sí los crea sobre CUALQUIER
        # documento activo: basta que una corrida suya se solape con una nuestra.
        ids_mark = [r[0] for r in db.query(SiiLibroDoc.id)
                    .filter(SiiLibroDoc.uuid.like(f"{MARK}%")).all()]
        if ids_mark:
            db.query(SiiLibroMatch).filter(SiiLibroMatch.doc_id.in_(ids_mark)).delete(
                synchronize_session=S)
        db.query(SiiLibroDoc).filter(SiiLibroDoc.uuid.like(f"{MARK}%")).delete(
            synchronize_session=S)
        # Los runs no llevan MARK: se limpian los que no tienen ningún doc (los de esta
        # suite siempre quedan sin docs tras borrar los MARK) creados por el usuario 1
        # de prueba o de origen manual reciente — más simple: borrar los runs sin error
        # cuyo total coincide con tamaños de esta suite es frágil; se borran TODOS los
        # runs que no estén referenciados por ningún doc vivo. Es seguro: la bitácora
        # real de producción siempre tiene docs apuntándole o errores que se conservan.
        referenciados = {r[0] for r in db.query(SiiLibroDoc.ultimo_run_id)
                         .filter(SiiLibroDoc.ultimo_run_id.isnot(None)).all()}
        q = db.query(SiiLibroSyncRun)
        if referenciados:
            q = q.filter(~SiiLibroSyncRun.id.in_(referenciados))
        q.filter(SiiLibroSyncRun.usuario_id == CURRENT["id"]).delete(synchronize_session=S)
        db.query(SiiLibroReglaRut).filter(
            SiiLibroReglaRut.motivo.like(f"{MARK}%")).delete(synchronize_session=S)
        db.query(ContCompra).filter(ContCompra.acreedor.like(f"{MARK}%")).delete(
            synchronize_session=S)
        db.query(Proveedor).filter(Proveedor.nombre.like(f"{MARK}%")).delete(
            synchronize_session=S)
        db.commit()
    finally:
        db.close()


def _verificar_limpieza():
    db = SessionLocal()
    try:
        restos = (db.query(SiiLibroDoc).filter(SiiLibroDoc.uuid.like(f"{MARK}%")).count()
                  + db.query(ContCompra).filter(ContCompra.acreedor.like(f"{MARK}%")).count()
                  + db.query(Proveedor).filter(Proveedor.nombre.like(f"{MARK}%")).count())
        assert restos == 0, f"quedaron {restos} filas {MARK}"
    finally:
        db.close()
    print("Cleanup OK (verificado con sesión nueva)")


def _fila(uuid):
    db = SessionLocal()
    try:
        return db.query(SiiLibroDoc).filter(SiiLibroDoc.uuid == uuid).first()
    finally:
        db.close()


def _barrer(items, origen="manual"):
    """Corre un barrido y devuelve una FOTO plana de la bitácora (la sesión se cierra:
    devolver el objeto ORM daría DetachedInstanceError al leerlo)."""
    db = SessionLocal()
    try:
        cli._post_query = FakeQuery(items)
        run = ejecutar_barrido(db, origen=origen, usuario_id=CURRENT["id"])
        return SimpleNamespace(exito=run.exito, error=run.error, total_api=run.total_api,
                               nuevos=run.nuevos, actualizados=run.actualizados,
                               desaparecidos=run.desaparecidos)
    finally:
        db.close()


def run():
    original = cli._post_query
    # El cinturón de proporción del sweep (#3) se DESACTIVA para el grueso de la suite:
    # esta suite corre contra la BD local, que puede traer documentos REALES del espejo
    # — cada barrido fake los dejaría "por desaparecer" y el cinturón (correctamente)
    # abortaría. La sonda §14b lo re-activa con su valor real y lo prueba a propósito.
    prop_original = sync_mod.MAX_PROPORCION_DESAPARECIDOS
    sync_mod.MAX_PROPORCION_DESAPARECIDOS = 1.0   # ratio > 1.0 es imposible → apagado
    _limpiar()
    try:
        CURRENT["empresa"] = "mineria"

        # ── 1) Primer barrido: el espejo nace ────────────────────────────────────────
        docs = [_doc(1), _doc(2, rut="77.115.331-3", nombre="RAMAQ", total=500000),
                _doc(3, rut="76.513.680-6", signo=-1, total=50000, tipo=61)]  # una NC
        run1 = _barrer(docs)
        check("1a el barrido termina con éxito", run1.exito is True, run1.error)
        check("1b 3 nuevos, 0 actualizados", (run1.nuevos, run1.actualizados) == (3, 0),
              (run1.nuevos, run1.actualizados))
        f1 = _fila(U(1))
        check("1c el RUT quedó CANÓNICO en el espejo",
              f1.rut_emisor_canonico == "76513680-6", f1.rut_emisor_canonico)
        f3 = _fila(U(3))
        check("1d la NC quedó con monto_efectivo NEGATIVO",
              float(f3.monto_efectivo) < 0, float(f3.monto_efectivo))

        # ── 2) Idempotencia: N corridas = 1 corrida ──────────────────────────────────
        run2 = _barrer(docs)
        check("2a re-barrer: 0 nuevos, 3 actualizados",
              (run2.nuevos, run2.actualizados) == (0, 3), (run2.nuevos, run2.actualizados))
        db = SessionLocal()
        n = db.query(SiiLibroDoc).filter(SiiLibroDoc.uuid.like(f"{MARK}%")).count()
        db.close()
        check("2c SONDA idempotencia: siguen siendo 3 filas, no 6", n == 3, n)
        check("2d SONDA flush del sweep (bug latente, autoflush=False): re-barrer lo "
              "MISMO no marca DESAPARECIDO a nadie del barrido",
              run2.desaparecidos == 0, run2.desaparecidos)
        check("2e y el doc re-visto sigue ACTIVO en la BD (el bulk UPDATE ya no le "
              "gana al upsert sin flushear)",
              _fila(U(1)).estado_espejo == "ACTIVO", _fila(U(1)).estado_espejo)

        # ── 3) DESAPARECIDO: jamás DELETE ────────────────────────────────────────────
        _barrer([docs[0], docs[1]])   # el doc 3 dejó de venir
        f3 = _fila(U(3))
        check("3a el doc que dejó de venir SIGUE existiendo", f3 is not None)
        check("3b SONDA: quedó DESAPARECIDO, no borrado",
              f3.estado_espejo == "DESAPARECIDO", f3.estado_espejo)
        _barrer(docs)                  # y vuelve
        f3 = _fila(U(3))
        check("3c si vuelve a venir, REVIVE a ACTIVO", f3.estado_espejo == "ACTIVO",
              f3.estado_espejo)

        # ── 4) La decisión local sobrevive al barrido, y la divergencia se enciende ──
        r = client.post(f"{API}/documentos/{_fila(U(1)).id}/decision",
                        json={"accion": "clasificar", "destino": "cc:bodegaje",
                              "motivo": f"{MARK} clasificación de prueba"})
        check("4a clasificar responde 200", r.status_code == 200, r.text[:200])
        _barrer(docs)  # mismo contenido: nada cambia
        f1 = _fila(U(1))
        check("4b el barrido NO pisó la decisión",
              f1.decision == "clasificado" and f1.destino == "cc:bodegaje",
              (f1.decision, f1.destino))
        check("4c sin cambios remotos NO hay divergencia", f1.divergente is False)
        docs_mod = [dict(docs[0], sent_ntotal=999999), docs[1], docs[2]]
        _barrer(docs_mod)
        f1 = _fila(U(1))
        check("4d SONDA: el doc cambió DESPUÉS de decidido → DIVERGENTE encendido",
              f1.divergente is True, f1.divergente)
        check("4e con el detalle de qué cambió",
              "sent_ntotal" in (f1.divergencia_detalle or ""), f1.divergencia_detalle)
        check("4e-bis SONDA divergencia FINA (bajo confirmado): SOLO el campo que "
              "cambió — sin falsos positivos por str(Decimal('…00')) vs str(int)",
              "sent_nsubtotal" not in (f1.divergencia_detalle or "")
              and "sent_niva" not in (f1.divergencia_detalle or ""),
              f1.divergencia_detalle)
        check("4f y la decisión se conserva (el sistema no repara solo)",
              f1.decision == "clasificado", f1.decision)
        _barrer(docs)  # restaurar contenido

        # ── 5) Las TRES cubetas, con el RUT canónico como llave ──────────────────────
        db = SessionLocal()
        # compra que MATCHEA el doc 2 (RAMAQ) — con el RUT EN OTRO FORMATO a propósito
        db.add(ContCompra(empresa="mineria", acreedor=f"{MARK} RAMAQ",
                          proveedor_rut="77115331-3", numero_documento=str(4002),
                          monto_total=595000))
        db.commit()
        db.close()
        r = client.get(f"{API}/documentos")
        check("5a la bandeja responde 200", r.status_code == 200, r.text[:200])
        por_uuid = {x["uuid"]: x for x in r.json()["items"] if x["uuid"].startswith(MARK)}
        check("5b el doc con compra registrada cae en ESTA",
              por_uuid.get(U(2), {}).get("cubeta") == "ESTA", por_uuid.get(U(2)))
        check("5c el doc sin compra cae en NO_ESTA",
              por_uuid.get(U(1), {}).get("cubeta") == "NO_ESTA", por_uuid.get(U(1)))
        check("5d SONDA canónico: '77115331-3' tecleado matchea al '77.115.331-3' del "
              "libro (sin canónico sería NO_ESTA y se re-cargaría: el doble conteo)",
              por_uuid.get(U(2), {}).get("cubeta") == "ESTA")

        # una compra SIN RUT enciende el INDETERMINADO para los folios que le calzan
        db = SessionLocal()
        db.add(ContCompra(empresa="mineria", acreedor=f"{MARK} sin rut",
                          proveedor_rut=None, numero_documento=str(4001),
                          monto_total=119000))
        db.commit()
        db.close()
        r = client.get(f"{API}/documentos")
        por_uuid = {x["uuid"]: x for x in r.json()["items"] if x["uuid"].startswith(MARK)}
        check("5e el folio que calza con una compra SIN RUT pasa a INDETERMINADO "
              "(no se puede concluir, y decirlo es lo honesto)",
              por_uuid.get(U(1), {}).get("cubeta") == "INDETERMINADO", por_uuid.get(U(1)))

        # ── 6) El guard BLOQUEADO y las salidas de la clasificación ──────────────────
        r = client.post(f"{API}/reglas-rut",
                        json={"rut": "76.513.680-6", "nivel": "BLOQUEADO",
                              "destino_default": "cc:financiero",
                              "motivo": f"{MARK} corredora / compra de moneda"})
        check("6a la regla BLOQUEADO se guarda (RUT canonizado)",
              r.status_code == 200 and r.json()["rut"] == "76513680-6", r.text[:200])
        # volver el doc 1 a pendiente para re-clasificarlo
        client.post(f"{API}/documentos/{_fila(U(1)).id}/decision",
                    json={"accion": "pendiente"})
        r = client.post(f"{API}/documentos/{_fila(U(1)).id}/decision",
                        json={"accion": "clasificar", "destino": "costo_venta"})
        check("6b SONDA: BLOQUEADO no puede ir a costo_venta (409 — la comisión del "
              "banco jamás colgada al embarque)", r.status_code == 409, r.text[:200])
        r = client.post(f"{API}/documentos/{_fila(U(1)).id}/decision",
                        json={"accion": "clasificar", "destino": "cc:financiero"})
        check("6c pero SÍ a un centro de costos", r.status_code == 200, r.text[:200])
        r = client.post(f"{API}/documentos/{_fila(U(3)).id}/decision",
                        json={"accion": "clasificar", "destino": "costo_venta"})
        check("6d la NOTA DE CRÉDITO no va a costo_venta en esta fase (409)",
              r.status_code == 409, r.text[:200])
        r = client.post(f"{API}/documentos/{_fila(U(3)).id}/decision",
                        json={"accion": "clasificar", "destino": "activo_fijo"})
        check("6e ACTIVO FIJO es salida válida (hallazgo del controller)",
              r.status_code == 200, r.text[:200])
        r = client.post(f"{API}/documentos/{_fila(U(2)).id}/decision",
                        json={"accion": "clasificar", "destino": "cualquier_cosa"})
        check("6f un destino inventado se rechaza (400)", r.status_code == 400, r.text[:160])

        # ── 7) Tablero y export ──────────────────────────────────────────────────────
        r = client.get(f"{API}/resumen")
        check("7a el tablero responde 200", r.status_code == 200, r.text[:200])
        rj = r.json()
        check("7b declara la edad del último barrido y las cubetas",
              "edad_horas_ultimo_exitoso" in rj and "cubetas" in rj, list(rj)[:8])
        check("7c la edad NO es crítica (acabamos de barrer)", rj["edad_critica"] is False,
              rj["edad_horas_ultimo_exitoso"])
        r = client.get(f"{API}/export.csv")
        check("7d el export responde CSV", r.status_code == 200
              and "text/csv" in r.headers.get("content-type", ""), r.headers)
        check("7e el export NO incluye los que ESTÁN (solo faltantes e indeterminados)",
              str(4002) not in r.text or "ESTA" not in r.text.split("\n")[0], r.text[:200])

        # ── 8) Candado de empresa ────────────────────────────────────────────────────
        CURRENT["empresa"] = "automotriz"
        r = client.get(f"{API}/resumen")
        check("8 un usuario de MonzaParts recibe 403", r.status_code == 403, r.status_code)
        CURRENT["empresa"] = "mineria"

        # ── 9) Asociar RUT al proveedor (el camino sin backfill) ─────────────────────
        db = SessionLocal()
        prov = Proveedor(nombre=f"{MARK} PROVEEDOR X")
        db.add(prov)
        db.commit()
        pid = prov.id
        db.close()
        r = client.post(f"{API}/proveedores/{pid}/asociar-rut",
                        params={"rut": "77.977.813-5"})
        check("9a un RUT con módulo 11 malo se rechaza", r.status_code == 400, r.text[:160])
        r = client.post(f"{API}/proveedores/{pid}/asociar-rut",
                        params={"rut": "77.977.813-4"})
        check("9b uno válido se asocia canónico", r.status_code == 200
              and r.json()["rut"] == "77977813-4", r.text[:160])
        r = client.post(f"{API}/proveedores/{pid}/asociar-rut",
                        params={"rut": "76.513.680-6"})
        check("9c y no se PISA con otro distinto (409)", r.status_code == 409, r.text[:160])

        # ── 10) Prefill de compra (botón «Registrar compra» de la bandeja) ───────────
        # RUT SINTÉTICO a propósito: ningún proveedor real del entorno lo tiene, así el
        # proveedor_id nulo/preseleccionado es determinista. El d5 lleva porción EXENTA:
        # la sonda de que el exento viaja dentro del neto (neto + IVA == total del doc).
        d4 = _doc(4, rut="78.901.230-5", nombre="PREFILL SA", total=200000, folio="7777")
        d5 = dict(_doc(5, rut="78.901.230-5", nombre="PREFILL SA", total=100000,
                       folio="7778"), sent_nexempt=50000, sent_ntotal=169000)
        _barrer(docs + [d4, d5])
        r = client.get(f"{API}/documentos/{_fila(U(4)).id}/prefill-compra")
        check("10a el prefill responde 200", r.status_code == 200, r.text[:200])
        pj = r.json()
        # Dos grupos, y la distinción importa: los CAMPOS del formulario tienen que ser
        # exactamente los de CompraCreate (si se colara uno de más, entraría al alta sin
        # que nadie lo haya validado), y aparte viajan dos claves INFORMATIVAS que la
        # pantalla usa para advertir «puede que esta factura ya esté registrada» y que
        # jamás se mandan al POST. Se listan explícitas: agregar una tercera obliga a
        # pasar por acá y decidir a qué grupo pertenece.
        CAMPOS_FORM = {"proveedor_id", "acreedor", "proveedor_rut", "numero_documento",
                       "fecha", "monto_neto", "iva", "monto_total"}
        AVISO = {"cubeta", "cubeta_detalle"}
        check("10b shape EXACTO que espera el formulario de Compras (CompraCreate), "
              "más las 2 claves de aviso y nada más",
              set(pj) == CAMPOS_FORM | AVISO, sorted(pj))
        check("10b-bis la advertencia de la bandeja VIAJA al formulario (si se queda "
              "en la lista, el operador la pierde justo donde fabrica el duplicado)",
              pj["cubeta"] in ("ESTA", "NO_ESTA", "INDETERMINADO"), pj.get("cubeta"))
        check("10c SONDA #1: el RUT del prefill viaja CANÓNICO (no formateado) — el "
              "anti-dup del alta compara strings y '78.901.230-5' con puntos lo burla",
              pj["proveedor_rut"] == "78901230-5", pj["proveedor_rut"])
        check("10d folio → numero_documento y fecha ISO",
              pj["numero_documento"] == "7777" and pj["fecha"] == "2026-07-15",
              (pj["numero_documento"], pj["fecha"]))
        check("10e montos del documento (neto 200000 · IVA 38000 · total 238000)",
              (pj["monto_neto"], pj["iva"], pj["monto_total"]) == (200000, 38000, 238000),
              pj)
        check("10f sin proveedor con ese RUT → proveedor_id nulo",
              pj["proveedor_id"] is None, pj["proveedor_id"])
        # Proveedor con el RUT en OTRO formato (con puntos, como un legado): igual se
        # preselecciona — la comparación es por canónico, no por igualdad de texto.
        db = SessionLocal()
        prov2 = Proveedor(nombre=f"{MARK} PREFILL SA", rut="78.901.230-5")
        db.add(prov2)
        db.commit()
        pid2 = prov2.id
        db.close()
        r = client.get(f"{API}/documentos/{_fila(U(4)).id}/prefill-compra")
        check("10g SONDA canónico: el proveedor con RUT en otro formato se preselecciona",
              r.json()["proveedor_id"] == pid2, r.json()["proveedor_id"])
        r = client.get(f"{API}/documentos/{_fila(U(5)).id}/prefill-compra")
        pj5 = r.json()
        check("10h SONDA exento: viaja dentro del neto y neto + IVA == total del doc",
              (pj5["monto_neto"], pj5["iva"], pj5["monto_total"]) == (150000, 19000, 169000),
              pj5)
        r = client.get(f"{API}/documentos/{_fila(U(3)).id}/prefill-compra")
        check("10i una NOTA DE CRÉDITO no se pre-llena como compra (409)",
              r.status_code == 409 and "crédito" in r.json()["detail"].lower(),
              r.text[:200])
        r = client.get(f"{API}/documentos/99999999/prefill-compra")
        check("10j doc inexistente → 404", r.status_code == 404, r.status_code)

        # ── 11) #4 DESAPARECIDO visible: se lista, se exporta, y no se decide/prefillea
        _barrer(docs + [d5])          # d4 deja de venir → DESAPARECIDO
        f4 = _fila(U(4))
        check("11a el doc quedó DESAPARECIDO (precondición)",
              f4.estado_espejo == "DESAPARECIDO", f4.estado_espejo)
        r = client.get(f"{API}/documentos",
                       params={"estado": "DESAPARECIDO", "rut": "78.901.230-5"})
        items = [x for x in r.json()["items"] if x["uuid"] == U(4)]
        check("11b SONDA #4: ?estado=DESAPARECIDO lista CUÁLES documentos desaparecieron",
              len(items) == 1, r.json()["total"])
        check("11c con cubeta 'N/A' (el SII ya no lo declara: el cruce vivo no aplica)",
              items and items[0]["cubeta"] == "N/A", items)
        r = client.get(f"{API}/documentos", params={"rut": "78.901.230-5"})
        check("11d la bandeja por defecto (ACTIVO) NO lo incluye",
              all(x["uuid"] != U(4) for x in r.json()["items"]), r.json()["total"])
        r = client.get(f"{API}/documentos", params={"estado": "CUALQUIERA"})
        check("11e un estado inventado se rechaza (400)", r.status_code == 400,
              r.status_code)
        r = client.get(f"{API}/documentos/{f4.id}/prefill-compra")
        check("11f SONDA #4: prefill de un DESAPARECIDO → 409 (no se registra un "
              "fantasma desde una pestaña vieja)", r.status_code == 409, r.text[:200])
        r = client.post(f"{API}/documentos/{f4.id}/decision",
                        json={"accion": "ignorar", "motivo": f"{MARK} no debería"})
        check("11g SONDA #4: decidir sobre un DESAPARECIDO → 409", r.status_code == 409,
              r.text[:200])
        r = client.get(f"{API}/export.csv", params={"estado": "DESAPARECIDO"})
        check("11h el CSV también exporta los desaparecidos (folio 7777 presente)",
              r.status_code == 200 and "7777" in r.text, r.text[:200])
        _barrer(docs + [d4, d5])      # d4 vuelve → revive (estado limpio para seguir)

        # ── 12) #1 folio BLANDO: ceros a la izquierda ≠ NO_ESTA ──────────────────────
        # Compra registrada a mano como '0004071' (así lo imprime el PDF del DTE); el
        # libro trae folio '4071'. Sin el arreglo: NO_ESTA → botón registrar → segunda
        # CxP pagable dos veces. Con el arreglo: INDETERMINADO con leyenda.
        d6 = _doc(6, rut="79.888.777-1", nombre="BLANDO SPA", folio="4071",
                  total=300000)
        _barrer(docs + [d4, d5, d6])
        db = SessionLocal()
        db.add(ContCompra(empresa="mineria", acreedor=f"{MARK} BLANDO SPA",
                          proveedor_rut="79.888.777-1", numero_documento="0004071",
                          monto_total=357000, monto_total_clp=357000))
        db.commit()
        db.close()
        r = client.get(f"{API}/documentos", params={"rut": "79.888.777-1"})
        fila6 = next((x for x in r.json()["items"] if x["uuid"] == U(6)), None)
        check("12a SONDA #1: folio '4071' vs compra '0004071' → INDETERMINADO, "
              "JAMÁS NO_ESTA (el falso faltante que termina en CxP duplicada)",
              fila6 and fila6["cubeta"] == "INDETERMINADO", fila6)
        check("12b con leyenda que cita el folio parecido del ERP",
              fila6 and "0004071" in (fila6["cubeta_detalle"] or ""), fila6)
        r = client.get(f"{API}/documentos", params={"page_size": 200})
        por_uuid = {x["uuid"]: x for x in r.json()["items"] if x["uuid"].startswith(MARK)}
        check("12c el match EXACTO sigue siendo ESTA (el blando no degrada lo seguro)",
              por_uuid.get(U(2), {}).get("cubeta") == "ESTA", por_uuid.get(U(2)))
        # SONDA #26 (revisión 2026-08-08): la advertencia del folio parecido tiene que
        # llegar al FORMULARIO, no quedarse en la lista. El check 10b-bis no lo probaba:
        # aceptaba cualquiera de las tres cubetas y nunca miraba `cubeta_detalle`, así
        # que devolver 'NO_ESTA'/None fijo (el formulario abriéndose limpio y diciendo
        # que la factura no está registrada) pasaba en verde. Se pide el prefill del
        # documento del folio BLANDO, que es el ÚNICO caso que genera leyenda.
        r = client.get(f"{API}/documentos/{_fila(U(6)).id}/prefill-compra")
        pj6 = r.json()
        check("12c-bis SONDA #26: el prefill del doc con folio parecido viaja con "
              "cubeta INDETERMINADO y la leyenda que cita el folio del ERP "
              "(la advertencia llega donde se fabrica el duplicado)",
              r.status_code == 200 and pj6.get("cubeta") == "INDETERMINADO"
              and "0004071" in (pj6.get("cubeta_detalle") or ""),
              (r.status_code, pj6.get("cubeta"), pj6.get("cubeta_detalle")))

        # ── 12-bis) #11 colisión de TIPO: mismo RUT y mismo N°, DTE distinto ─────────
        # Las series de folio del SII son por TIPO de documento: el mismo proveedor puede
        # emitir la factura 33 N° 5100 y la boleta 39 N° 5100, que son dos documentos y
        # dos deudas distintas. El cruce armaba la llave (RUT, folio) sin el tipo, así que
        # la boleta se declaraba «ESTA» contra la factura ya registrada, desaparecía del
        # filtro de faltantes y del CSV, y esa CxP no nacía nunca.
        d7 = _doc(7, rut="76.222.333-4", nombre="TIPOS SPA", folio="5100",
                  total=200000, tipo=39)          # BOLETA (familia distinta)
        d8 = _doc(8, rut="76.222.333-4", nombre="TIPOS SPA", folio="5100",
                  total=200000, tipo=33)          # FACTURA (misma familia que el ERP)
        # d9 + su compra son la SONDA #9 del lado bandeja (ver 12g): el RUT del ERP
        # tecleado con un espacio donde iba el guión. La MISMA fila la reusa §18 para el
        # lado alta — es el mismo dato sucio, y las dos mitades tienen que coincidir.
        d9 = _doc(9, rut="96.631.520-2", nombre="RUT SUCIO SA", folio="4071",
                  total=100000)
        _barrer(docs + [d4, d5, d6, d7, d8, d9])
        db = SessionLocal()
        db.add(ContCompra(empresa="mineria", acreedor=f"{MARK} TIPOS SPA",
                          proveedor_rut="76222333-4", numero_documento="5100",
                          tipo_doc="factura", moneda="CLP", origen="MANUAL",
                          monto_total=238000, monto_total_clp=238000))
        db.add(ContCompra(empresa="mineria", acreedor=f"{MARK} RUT SUCIO",
                          proveedor_rut="96.631.520 2", numero_documento="4071",
                          tipo_doc="factura", moneda="CLP", origen="MANUAL",
                          monto_total=119000, monto_total_clp=119000,
                          fecha=date(2026, 7, 15)))
        db.commit()
        db.close()
        r = client.get(f"{API}/documentos", params={"rut": "76.222.333-4"})
        por_uuid = {x["uuid"]: x for x in r.json()["items"] if x["uuid"].startswith(MARK)}
        f7, f8 = por_uuid.get(U(7), {}), por_uuid.get(U(8), {})
        check("12d SONDA #11: la BOLETA 39 con el mismo RUT y el mismo N° que una "
              "FACTURA registrada NO se declara ESTA (se daba por registrada una CxP "
              "que nunca nació) → INDETERMINADO",
              f7.get("cubeta") == "INDETERMINADO", f7)
        check("12e …con la contradicción escrita: nombra el tipo del ERP y el del SII",
              "factura" in (f7.get("cubeta_detalle") or "")
              and "39" in (f7.get("cubeta_detalle") or ""), f7.get("cubeta_detalle"))
        check("12f y el control: la FACTURA 33 del mismo RUT y N° SIGUE siendo ESTA "
              "(el tipo discrimina, no rompe el cruce)",
              f8.get("cubeta") == "ESTA", f8)
        r = client.get(f"{API}/documentos", params={"rut": "96.631.520-2"})
        f9 = next((x for x in r.json()["items"] if x["uuid"] == U(9)), None)
        check("12f-bis SONDA #9 (bandeja): la compra está registrada con el RUT sucio "
              "('96.631.520 2', un espacio donde iba el guión — el dato es texto libre) "
              "y el documento cae igual en ESTA. Con el canonizador estricto la compra "
              "se contaba como 'sin RUT' y su factura se veía como faltante",
              f9 and f9["cubeta"] == "ESTA", f9)

        # ── 12-ter) #14 la duda de una compra SIN LLAVE se ACOTA ─────────────────────
        # Antes bastaba UNA compra activa sin RUT y sin N° para que TODO el libro cayera
        # en INDETERMINADO: la tarjeta «No está — la deuda invisible» marcaba 0 y la
        # única pregunta que el módulo existe para responder dejaba de tener respuesta.
        # Ahora la duda alcanza SOLO a los documentos que esa compra podría ser.
        db = SessionLocal()
        # (1) caja chica: sin RUT, sin N° y con el MISMO monto que el doc U(4) — pero
        #     'sin_documento' JAMÁS puede ser un DTE del libro del SII, así que no
        #     envenena a nadie. El monto coincide a propósito: si el filtro por tipo se
        #     borrara, esta fila sola volvería INDETERMINADO al U(4) y 12g daría rojo.
        db.add(ContCompra(empresa="mineria", acreedor=f"{MARK} CAJA CHICA",
                          proveedor_rut=None, numero_documento=None,
                          tipo_doc="sin_documento", moneda="CLP", origen="MANUAL",
                          monto_total=238000, monto_total_clp=238000,
                          fecha=date(2026, 7, 15)))
        db.commit()
        db.close()
        r = client.get(f"{API}/documentos", params={"rut": "78.901.230-5"})
        por_uuid = {x["uuid"]: x for x in r.json()["items"] if x["uuid"].startswith(MARK)}
        check("12g SONDA #14: una caja chica 'sin_documento' sin RUT ni N° NO manda el "
              "libro entero a INDETERMINADO (no puede ser un DTE del SII)",
              por_uuid.get(U(4), {}).get("cubeta") == "NO_ESTA", por_uuid.get(U(4)))
        db = SessionLocal()
        # (2) una FACTURA en CLP sin RUT ni N°: esta SÍ podría ser un DTE del libro, y su
        #     monto (238.000) es el del doc U(4) — la duda es legítima y acotada.
        db.add(ContCompra(empresa="mineria", acreedor=f"{MARK} SIN LLAVE",
                          proveedor_rut=None, numero_documento=None,
                          tipo_doc="factura", moneda="CLP", origen="MANUAL",
                          monto_total=238000, monto_total_clp=238000,
                          fecha=date(2026, 7, 15)))
        db.commit()
        db.close()
        r = client.get(f"{API}/documentos", params={"rut": "78.901.230-5"})
        por_uuid = {x["uuid"]: x for x in r.json()["items"] if x["uuid"].startswith(MARK)}
        f4b, f5b = por_uuid.get(U(4), {}), por_uuid.get(U(5), {})
        check("12h la compra sin llave que SÍ podría ser un DTE vuelve INDETERMINADO "
              "al documento de SU MISMO monto", f4b.get("cubeta") == "INDETERMINADO",
              f4b)
        check("12i …con leyenda que dice CUÁNTAS son y por qué monto — y son 1, no 2: "
              "la caja chica del mismo monto sigue fuera",
              "1 compra(s)" in (f4b.get("cubeta_detalle") or "")
              and "238.000" in (f4b.get("cubeta_detalle") or ""),
              f4b.get("cubeta_detalle"))
        check("12j SONDA #14 (la que mata el acantilado): el documento de OTRO monto "
              "sigue en NO_ESTA — una fila sin llave ya no borra la cubeta entera",
              f5b.get("cubeta") == "NO_ESTA", f5b)

        # ── 13) #2/#5 cuadratura DESGLOSADA y en CLP ─────────────────────────────────
        # Mes sintético 2031-01 (imposible que la BD local tenga datos reales ahí):
        #   libro: factura 119.000 + NC −23.800 + ignorado 59.500 + NC IGNORADA −11.900
        #                                                          → Σ 142.800
        #   ERP:   compra CLP 119.000 (comparable, matchea la factura)
        #          + embarque USD 45.000 con clp 42.300.000 (fuera del libro)
        # diferencia_explicada = 142.800 − 59.500 − (−35.700) − 119.000 = 0.
        #
        # La NC IGNORADA (c4) es la SONDA #28: cae a la vez en la columna NC y en la de
        # ignorados, así que si el guard `trx_sign == 1` de los ignorados desapareciera se
        # restaría DOS veces e inventaría una diferencia fantasma. El escenario anterior
        # no podía detectarlo: el único documento ignorado era una FACTURA.
        c1 = _doc(20, rut="76.111.222-9", nombre="CUAD SA", folio="9001",
                  total=100000, fecha="2031-01-10")
        c2 = _doc(21, rut="76.111.222-9", nombre="CUAD SA", folio="9002",
                  total=20000, signo=-1, tipo=61, fecha="2031-01-12")
        c3 = _doc(22, rut="77.333.444-6", nombre="IGNORABLE LTDA", folio="9003",
                  total=50000, fecha="2031-01-15")
        c4 = _doc(23, rut="76.111.222-9", nombre="CUAD SA", folio="9004",
                  total=10000, signo=-1, tipo=61, fecha="2031-01-14")
        base13 = docs + [d4, d5, d6, d7, d8, d9, c1, c2, c3, c4]
        _barrer(base13)
        r = client.post(f"{API}/documentos/{_fila(U(22)).id}/decision",
                        json={"accion": "ignorar", "motivo": f"{MARK} sin interés"})
        check("13a ignorar el doc c3 responde 200", r.status_code == 200, r.text[:160])
        r = client.post(f"{API}/documentos/{_fila(U(23)).id}/decision",
                        json={"accion": "ignorar", "motivo": f"{MARK} NC ignorada"})
        check("13a-bis ignorar una NOTA DE CRÉDITO también responde 200 (la rama "
              "'ignorar' no la rechaza: por eso el guard de la cuadratura hace falta)",
              r.status_code == 200, r.text[:160])
        db = SessionLocal()
        db.add(ContCompra(empresa="mineria", acreedor=f"{MARK} CUAD SA",
                          proveedor_rut="76111222-9", numero_documento="9001",
                          moneda="CLP", origen="MANUAL", tipo_doc="factura",
                          monto_total=119000, monto_total_clp=119000,
                          fecha=date(2031, 1, 10)))
        db.add(ContCompra(empresa="mineria", acreedor=f"{MARK} FLORIDA ENGINES",
                          proveedor_rut=None, numero_documento="INV-2031-77",
                          moneda="USD", origen="EMBARQUE", tipo_doc="factura",
                          tc=940, monto_total=45000, monto_total_clp=42300000,
                          fecha=date(2031, 1, 20)))
        db.commit()
        db.close()
        r = client.get(f"{API}/resumen")
        rj = r.json()
        fila_mes = next((x for x in rj["cuadratura_mensual"] if x["mes"] == "2031-01"),
                        None)
        check("13b el mes sintético aparece en la cuadratura", fila_mes is not None,
              [x["mes"] for x in rj["cuadratura_mensual"]])
        fm = fila_mes or {}
        check("13c SONDA #2: el ERP suma monto_total_clp — 42.419.000, no los "
              "'164.000' de sumar USD como pesos",
              abs(fm.get("erp", 0) - 42419000) < 0.01, fm.get("erp"))
        check("13d el embarque USD queda desglosado como erp_fuera_libro",
              abs(fm.get("erp_fuera_libro", 0) - 42300000) < 0.01
              and abs(fm.get("erp_comparable", -1) - 119000) < 0.01, fm)
        check("13e SONDA #28: el libro desglosa ignorados y NC, y la NC IGNORADA NO "
              "entra en la columna de ignorados (59.500, no 47.600): ya viaja restada "
              "en la columna NC y restarla dos veces inventaría diferencia",
              abs(fm.get("libro_ignorados", 0) - 59500) < 0.01
              and abs(fm.get("libro_nc", 0) - (-35700)) < 0.01, fm)
        check("13f SONDA #5: diferencia_explicada = libro − ignorados − nc − "
              "erp_comparable = $0 (el 0 alcanzable que devuelve el poder de alarma)",
              abs(fm.get("diferencia_explicada", 99)) < 0.01, fm)
        check("13g las claves VIEJAS del tablero siguen (mes/libro/erp/diferencia)",
              {"mes", "libro", "erp", "diferencia"} <= set(fm), sorted(fm))

        # ── 14) sync endurecido: ventana (#15), proporción (#3), malformado, fencing ─
        viejo = _doc(30, rut="76.999.888-K", nombre="VIEJO SA", folio="8888",
                     total=80000, fecha="2024-01-15")   # fuera de la ventana de 24 meses
        _barrer(base13 + [viejo])
        check("14a el doc fuera de ventana entra ACTIVO al espejo",
              _fila(U(30)).estado_espejo == "ACTIVO", _fila(U(30)).estado_espejo)
        _barrer(base13)               # el barrido ya no lo trae (la ventana lo dejó atrás)
        check("14b SONDA #15: envejecer NO es desaparecer — el doc fuera de ventana "
              "sigue ACTIVO (el barrido dejó de consultarlo, no el SII de declararlo)",
              _fila(U(30)).estado_espejo == "ACTIVO", _fila(U(30)).estado_espejo)

        sync_mod.MAX_PROPORCION_DESAPARECIDOS = prop_original   # re-armar el cinturón
        rvacio = _barrer([])
        check("14c SONDA #3: la lista VACÍA bien formada ya no arrasa el espejo — "
              "exito=False con el cinturón de proporción",
              rvacio.exito is False
              and "proporci" in (rvacio.error or "").lower(), rvacio.error)
        check("14d y los documentos siguen ACTIVOS (no se concluyó nada)",
              _fila(U(1)).estado_espejo == "ACTIVO", _fila(U(1)).estado_espejo)
        sync_mod.MAX_PROPORCION_DESAPARECIDOS = 1.0             # apagar de nuevo

        mal = dict(_doc(31, rut="76.555.444-3", nombre="MALFORMADO", folio="8889",
                        total=10000, fecha="2026-07-20"))
        mal.pop("trx_sign")           # sin signo legible: monto_efectivo no se deriva
        rmal = _barrer(base13 + [mal])
        check("14e SONDA malformado: el barrido NO se congela — exito=True con AVISO "
              "en la bitácora", rmal.exito is True
              and "malformado" in (rmal.error or ""), (rmal.exito, rmal.error))
        fmal = _fila(U(31))
        check("14f el doc podrido se ESPEJA con monto NULL (la marca) y queda ACTIVO",
              fmal is not None and fmal.monto_efectivo is None
              and fmal.estado_espejo == "ACTIVO",
              (fmal and fmal.monto_efectivo, fmal and fmal.estado_espejo))

        # Fencing anti-solape (bajo confirmado): un run que expiró el guard de 30 min
        # vuelve de la red y encuentra que nació un run más nuevo → se retira SIN
        # escribir. El fake inserta el run intruso DURANTE la fase de red.
        class FakeConIntruso(FakeQuery):
            def __call__(self, body, params=None):
                if not getattr(self, "_ya", False):
                    self._ya = True
                    db2 = SessionLocal()
                    try:
                        db2.add(SiiLibroSyncRun(
                            origen="manual", usuario_id=CURRENT["id"],
                            terminado_at=datetime.utcnow(), exito=False,
                            error=f"{MARK} run intruso de la sonda de fencing"))
                        db2.commit()
                    finally:
                        db2.close()
                return _envelope(self.items)

        db = SessionLocal()
        try:
            cli._post_query = FakeConIntruso(base13 + [_doc(32, folio="8890")])
            rfen = ejecutar_barrido(db, origen="manual", usuario_id=CURRENT["id"])
            fen_exito, fen_error = rfen.exito, rfen.error
        finally:
            db.close()
        check("14g SONDA fencing: el run superado se retira con exito=False y lo narra",
              fen_exito is False and "anti-solape" in (fen_error or ""), fen_error)
        check("14h y NO escribió nada (el doc nuevo de ese barrido no existe)",
              _fila(U(32)) is None, _fila(U(32)))

        # ── 15) #13 grafo de FKs cerrado + #17 raw_json deferred ─────────────────────
        db = SessionLocal()
        f1 = db.query(SiiLibroDoc).filter(SiiLibroDoc.uuid == U(1)).first()
        check("15a SONDA #17: raw_json NO viaja en la carga por defecto (deferred)",
              "raw_json" not in f1.__dict__, sorted(f1.__dict__)[:8])
        check("15b …pero carga lazy al pedirlo explícitamente",
              (f1.raw_json or "").startswith("{"), (f1.raw_json or "")[:40])
        db.close()

        codigo = ("import wasabil_compras.models\n"
                  "from models.models import Base\n"
                  "from sqlalchemy import create_engine\n"
                  "Base.metadata.create_all(create_engine('sqlite://'))\n")
        res = subprocess.run([sys.executable, "-c", codigo], capture_output=True,
                             text=True, cwd=BACKEND_DIR, timeout=120)
        check("15c SONDA grafo FK (paridad del #13): importar wasabil_compras.models "
              "AISLADO + create_all sobre BD vacía no revienta con "
              "NoReferencedTableError", res.returncode == 0,
              (res.stderr or res.stdout)[-300:])

        # ── 16) CSV: inyección de fórmulas neutralizada (lente de seguridad) ─────────
        # El nombre del emisor lo escribe un TERCERO (viene del SII). Excel ejecuta como
        # fórmula la celda que empieza con = + - @, así que un proveedor con nombre
        # hostil corre código en el computador del contador que abre el reporte.
        # Va AL FINAL a propósito: pisa los nombres de los docs del MARK y no necesita
        # restaurarlos (la limpieza los borra).
        HOSTIL = '=HYPERLINK("http://evil.invalid";"clic")'
        # SONDA #27 (revisión 2026-08-08): la sonda vieja sembraba UNA sola columna y UNA
        # sola forma, así que dos regresiones pasaban en verde — quitarle el `.lstrip()`
        # al sanitizador (la planilla IGNORA el espacio inicial y evalúa igual) y sacarle
        # `_csv_seguro` a la columna del FOLIO, que también la escribe un tercero (viene
        # del SII y no se valida como numérica al espejarla).
        HOSTIL_ESP = " " + HOSTIL          # con espacio adelante: la forma que se colaba
        FOLIO_HOSTIL = "=1+1"              # el folio también es dato de tercero
        db = SessionLocal()
        try:
            db.query(SiiLibroDoc).filter(SiiLibroDoc.uuid.like(f"{MARK}%")).update(
                {"receiver_name": HOSTIL}, synchronize_session=False)
            db.query(SiiLibroDoc).filter(SiiLibroDoc.uuid == U(1)).update(
                {"receiver_name": HOSTIL_ESP}, synchronize_session=False)
            db.query(SiiLibroDoc).filter(SiiLibroDoc.uuid == U(31)).update(
                {"folio": FOLIO_HOSTIL}, synchronize_session=False)
            db.commit()
        finally:
            db.close()
        r = client.get(f"{API}/export.csv")
        filas = list(csv.reader(io.StringIO(r.text)))
        datos = filas[1:]                       # sin encabezado

        def _es_numero(c):
            """Un monto negativo ('-23800.0', el de una nota de crédito) empieza con '-'
            pero NO es una fórmula: la planilla lo lee como número. Neutralizarlo lo
            convertiría en texto y el contador no podría sumar la columna — el remedio
            sería peor. Por eso la sonda exige que lo peligroso sea texto que NO es
            número, que es exactamente lo que el sanitizador de producción distingue
            (solo toca strings; los montos los emitimos nosotros como float)."""
            try:
                float(c)
                return True
            except ValueError:
                return False

        # El detector MIRA LA CELDA COMO LA MIRA LA PLANILLA: con lstrip. El detector
        # viejo usaba c[:1] pelado — o sea, era tan ciego como el código roto que quería
        # atrapar y jamás habría visto una celda ' =HYPERLINK(…)'.
        peligrosas = [c for f in datos for c in f
                      if c.lstrip("\t\r\n ")[:1] in ("=", "+", "-", "@")
                      and not _es_numero(c)]
        check("16a el export sigue trayendo filas (si no, 16b sería un falso verde)",
              len(datos) > 0, len(filas))
        check("16b SONDA: NINGUNA celda del CSV empieza con = + - @ "
              "(inyección de fórmulas de Excel), ni siquiera con espacios adelante",
              peligrosas == [], peligrosas[:3])
        check("16c …y el nombre hostil viaja NEUTRALIZADO, no borrado "
              "(el contador tiene que poder leer quién es)",
              any(c == "'" + HOSTIL for f in datos for c in f),
              [c for f in datos for c in f if "HYPERLINK" in c][:2])
        check("16d SONDA #27: el nombre hostil CON ESPACIO adelante también viaja "
              "neutralizado (el espacio no salva: la planilla lo ignora y evalúa)",
              any(c == "'" + HOSTIL_ESP for f in datos for c in f),
              [c for f in datos for c in f if "HYPERLINK" in c][:3])
        check("16e SONDA #27: la columna del FOLIO también se sanea — el folio lo "
              "escribe el SII y se espeja sin validar que sea numérico",
              any(c == "'" + FOLIO_HOSTIL for f in datos for c in f),
              [c for f in datos for c in f if "1+1" in c][:3])

        # ── 17) Enfriamiento del barrido MANUAL (lente de seguridad) ────────────────
        # El anti-solape frena dos barridos simultáneos; esto frena dos SEGUIDOS, que es
        # lo que gasta cuota real de Wasabil (plata de la empresa) sin traer nada nuevo.
        db = SessionLocal()
        try:
            run_ok = SiiLibroSyncRun(origen="manual", exito=True,
                                     iniciado_at=datetime.utcnow(),
                                     usuario_id=CURRENT["id"])
            db.add(run_ok)
            db.commit()
            run_id = run_ok.id
        finally:
            db.close()
        r = client.post(f"{API}/sincronizar")
        check("17a un barrido exitoso recién hecho enfría el botón (429)",
              r.status_code == 429, (r.status_code, r.text[:140]))
        check("17b …y el 429 explica cuánto esperar y por qué (no es un error mudo)",
              "segundos" in r.text and "SII" in r.text, r.text[:160])
        db = SessionLocal()
        try:
            run = db.query(SiiLibroSyncRun).filter(SiiLibroSyncRun.id == run_id).first()
            run.exito = False
            db.commit()
        finally:
            db.close()
        r = client.post(f"{API}/sincronizar")
        check("17c SONDA: si el último barrido FALLÓ, el reintento NO se castiga "
              "(arregló el token y tiene que poder volver a intentar en el acto)",
              r.status_code != 429, (r.status_code, r.text[:140]))

        # ── 18) El anti-duplicado BLANDO del ALTA de compras (#9, #6, #12) ───────────
        # Este guard es el que impide que Tesorería pague DOS VECES la misma factura, así
        # que se prueba desde la suite del libro SII: el prefill de la bandeja es
        # justamente la vía que fabrica el duplicado, y las dos mitades tienen que
        # coincidir. Va al final porque crea compras propias (MARK) que moverían cubetas.
        pay_base = {"tipo_gasto": "gasto_operacional", "moneda": "CLP",
                    "monto_neto": 100000, "afecto_iva": False,
                    "condicion_pago": "credito", "plazo_dias": 30}

        # (a) #9 — el RUT del ERP es TEXTO LIBRE y viene sucio. La compra vieja quedó
        #     guardada '96.631.520 2' (un espacio donde iba el guión); se sembró en §12
        #     directo en la BD porque eso es lo que hay en producción, y es la MISMA fila
        #     que allá probó el lado bandeja. Ahora el operador la re-registra desde el
        #     prefill del libro, que manda el RUT canónico.
        r = client.post("/api/compras-contab", json=dict(
            pay_base, acreedor=f"{MARK} RUT SUCIO", proveedor_rut="96631520-2",
            numero_documento="4071"))
        check("18a SONDA #9: con el RUT del ERP sucio ('96.631.520 2') el alta de la "
              "MISMA factura choca igual (409 que nombra la compra). El guard exacto no "
              "la ve —el texto del RUT difiere— y el UNIQUE tampoco: si el canonizador "
              "se rinde, nacen DOS CxP activas y Tesorería paga dos veces",
              r.status_code == 409 and "compra #" in r.text,
              (r.status_code, r.text[:260]))
        db = SessionLocal()
        n_sucias = (db.query(ContCompra)
                    .filter(ContCompra.acreedor == f"{MARK} RUT SUCIO",
                            ContCompra.anulado.is_(False)).count())
        db.close()
        check("18b …y de verdad NO nació la segunda CxP (una sola compra activa)",
              n_sucias == 1, n_sucias)

        # (b) #6/#12 — el TIPO de documento discrimina: la serie de folios del SII es por
        #     tipo, así que la boleta N° 05500 y la factura N° 5500 del mismo proveedor
        #     son dos documentos distintos. Antes chocaban en un 409 sin ninguna salida
        #     (no hay flag de forzado ni endpoint para corregir el N° después).
        r = client.post("/api/compras-contab", json=dict(
            pay_base, acreedor=f"{MARK} DOS SERIES", proveedor_rut="76513680-6",
            numero_documento="5500", tipo_doc="factura"))
        check("18c control: la FACTURA N° 5500 se registra", r.status_code == 200,
              (r.status_code, r.text[:200]))
        r = client.post("/api/compras-contab", json=dict(
            pay_base, acreedor=f"{MARK} DOS SERIES", proveedor_rut="76.513.680-6",
            numero_documento="05500", tipo_doc="boleta"))
        check("18d SONDA #6/#12: la BOLETA N° 05500 del mismo proveedor SÍ se puede "
              "registrar (es otro documento y otra deuda) — antes era un 409 sin salida",
              r.status_code == 200, (r.status_code, r.text[:260]))
        r = client.post("/api/compras-contab", json=dict(
            pay_base, acreedor=f"{MARK} DOS SERIES", proveedor_rut="76.513.680-6",
            numero_documento="005500", tipo_doc="factura"))
        check("18e …y el guard SIGUE VIVO para el mismo tipo: otra FACTURA '005500' del "
              "mismo RUT choca con 409 (relajar por tipo no lo desarmó)",
              r.status_code == 409 and "compra #" in r.text,
              (r.status_code, r.text[:260]))

    finally:
        cli._post_query = original
        sync_mod.MAX_PROPORCION_DESAPARECIDOS = prop_original
        _limpiar()
    _verificar_limpieza()
    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_sii_libro_a1_a2():
    run()


if __name__ == "__main__":
    run()
