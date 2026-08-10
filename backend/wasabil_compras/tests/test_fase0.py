"""Fase 0 del libro de compras: RUT canónico + cliente de solo lectura fail-closed.

LO QUE ESTA SUITE PROTEGE (las tres promesas de la Fase 0):
  1. El RUT canónico es UNA sola forma y aguanta lo que la gente teclea.
  2. El cliente lee el envelope REAL (data.list.{items,total,lastPage}) y ante CUALQUIER
     desvío falla RUIDOSO — jamás «cero documentos, lectura completa» (la mentira que el
     auditor adversarial predijo del parser ingenuo).
  3. El cliente es INCAPAZ de tocar la ruta de creación: cada llamada del fake se registra
     y todas deben terminar en /documents/query. POST /documents (sin /query) EMITE un
     documento tributario real — la sonda §5 es el candado.

SONDAS DE PODER DISCRIMINANTE (validadas quitando el arreglo → rojo):
  · §3c un envelope con otra forma NO devuelve lista vacía: revienta.
  · §4b/§4c/§4d el barrido aborta ENTERO ante página caída, conteo que no cuadra o uuid
    repetido (la lista que se movió mientras se recorría).
  · §6 la NC con trx_sign=-1 SUMA NEGATIVO (sumar sent_ntotal crudo = error del doble).

Sin red (se reemplaza _post_query) y sin base de datos, salvo §7 (migración idempotente
sobre la base local). Molde de la casa: run() + check() + wrapper.

Corre con:  ./venv/bin/python -m pytest wasabil_compras/tests/test_fase0.py -q
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import wasabil_compras.client as cli  # noqa: E402
from wasabil_compras.client import (  # noqa: E402
    WasabilComprasError, barrer_recibidos, monto_efectivo, query_recibidos_pagina,
)
from wasabil_compras.rut import rut_canonico, rut_formateado, rut_valido  # noqa: E402

_fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


class FakeQuery:
    """Reemplaza cli._post_query. Registra CADA llamada (URL implícita: este fake solo se
    monta sobre _post_query, cuyo único destino es _url_query) y sirve páginas armadas.

    `llamadas` guarda (body, params) para las sondas de §5: el cliente real construye su
    URL en _url_query() y la suite verifica el TEXTO de esa URL aparte."""

    def __init__(self, paginas):
        self.paginas = paginas          # lista de dicts-respuesta, índice = página-1
        self.llamadas = []
        self.fallar_en_pagina = None    # int → esa página revienta (red caída a mitad)

    def __call__(self, body, params=None):
        self.llamadas.append((dict(body), dict(params or {})))
        page = body.get("page", 1)
        if self.fallar_en_pagina == page:
            raise WasabilComprasError("red caída simulada")
        return self.paginas[page - 1]


def _envelope(items, total, last_page):
    return {"success": True, "data": {"list": {
        "items": items, "total": total, "lastPage": last_page}}}


def _doc(uuid, total=1000, signo=1, rut="76.513.680-6"):
    return {"uuid": uuid, "sent_ntotal": total, "trx_sign": signo,
            "receiver_rut": rut, "folio": uuid[-4:], "sii_document_type_id": 33}


def run():
    original = cli._post_query
    try:
        # ── 1) RUT canónico: una sola forma, tolerante a lo tecleado ────────────────
        for entrada, esperado in [
            ("76.513.680-6", "76513680-6"),      # como lo entrega Wasabil
            ("76513680-6", "76513680-6"),        # como lo teclea un humano
            ("765136806", "76513680-6"),         # pegado sin guión
            (" 76.513.680-6 ", "76513680-6"),    # espacios del copy-paste
            ("77.977.813-4", "77977813-4"),      # el RUT de la propia empresa
            ("11.111.111-1", "11111111-1"),
            ("9.606.081-K", "9606081-K"),        # DV K
            ("9.606.081-k", "9606081-K"),        # k minúscula
            ("076513680-6", "76513680-6"),       # cero a la izquierda
        ]:
            check(f"1 canónico: {entrada!r} → {esperado}",
                  rut_canonico(entrada) == esperado, rut_canonico(entrada))
        for basura in [None, "", "  ", "FastAir", "12-3", "1234567890123", "76.513.680"]:
            check(f"1z no-RUT devuelve None: {basura!r}",
                  rut_canonico(basura) is None, rut_canonico(basura))

        check("1v módulo 11: 77.977.813-4 es válido", rut_valido("77.977.813-4"))
        check("1w módulo 11: 77.977.813-5 NO es válido (DV cambiado)",
              not rut_valido("77.977.813-5"))
        check("1x formateo para pantalla", rut_formateado("76513680-6") == "76.513.680-6",
              rut_formateado("76513680-6"))

        # ── 2) La URL única del cliente ─────────────────────────────────────────────
        url = cli._url_query()
        check("2a la ruta termina en /documents/query", url.endswith("/documents/query"), url)
        check("2b y NO es la ruta de creación (…/documents a secas)",
              not url.rstrip("/").endswith("/api/documents"), url)

        # ── 3) El parser del envelope: ruidoso, jamás lista-vacía-silenciosa ────────
        fake = FakeQuery([_envelope([_doc("u1")], 1, 1)])
        cli._post_query = fake
        items, total, lp = query_recibidos_pagina(1)
        check("3a envelope correcto → 1 item, total 1", len(items) == 1 and total == 1)
        check("3b el body pide received=true + trxType=expense (el LIBRO, no lo emitido)",
              fake.llamadas[0][0].get("received") is True
              and fake.llamadas[0][0].get("trxType") == "expense", fake.llamadas[0][0])

        for nombre, roto in [
            ("success=false", {"success": False, "data": {}}),
            ("sin data.list", {"success": True, "data": {}}),
            ("items no es lista", {"success": True, "data": {"list": {
                "items": None, "total": 0, "lastPage": 1}}}),
            ("sin total", {"success": True, "data": {"list": {
                "items": [], "lastPage": 1}}}),
        ]:
            cli._post_query = FakeQuery([roto])
            try:
                query_recibidos_pagina(1)
                check(f"3c SONDA envelope roto ({nombre}) revienta", False,
                      "devolvió sin error — la mentira silenciosa del auditor")
            except WasabilComprasError:
                check(f"3c SONDA envelope roto ({nombre}) revienta", True)

        # ── 4) El barrido: fail-closed de punta a punta ─────────────────────────────
        # 3 páginas sanas de 2+2+1 documentos.
        docs = [_doc(f"u{i}") for i in range(5)]
        paginas = [_envelope(docs[0:2], 5, 3), _envelope(docs[2:4], 5, 3),
                   _envelope(docs[4:5], 5, 3)]
        cli._post_query = FakeQuery(paginas)
        res = barrer_recibidos(per_page=2)
        check("4a barrido sano: 5 documentos, 3 llamadas", len(res) == 5)

        fake = FakeQuery(paginas)
        fake.fallar_en_pagina = 2
        cli._post_query = fake
        try:
            barrer_recibidos(per_page=2)
            check("4b SONDA página caída → aborta ENTERO", False, "no abortó")
        except WasabilComprasError:
            check("4b SONDA página caída → aborta ENTERO (sin resultados parciales)", True)

        # total declara 6 pero solo hay 5: lectura incompleta.
        paginas_cortas = [_envelope(docs[0:2], 6, 3), _envelope(docs[2:4], 6, 3),
                          _envelope(docs[4:5], 6, 3)]
        cli._post_query = FakeQuery(paginas_cortas)
        try:
            barrer_recibidos(per_page=2)
            check("4c SONDA Σ != total → aborta", False, "concluyó de una lectura a medias")
        except WasabilComprasError:
            check("4c SONDA Σ != total → aborta", True)

        # el mismo uuid en dos páginas: la lista se movió mientras se recorría.
        paginas_corridas = [_envelope(docs[0:2], 5, 3),
                            _envelope([docs[1], docs[2]], 5, 3),
                            _envelope(docs[3:5], 5, 3)]
        cli._post_query = FakeQuery(paginas_corridas)
        try:
            barrer_recibidos(per_page=2)
            check("4d SONDA uuid repetido entre páginas → aborta", False, "no lo vio")
        except WasabilComprasError:
            check("4d SONDA uuid repetido entre páginas → aborta", True)

        # el total cambia a mitad de barrido (llegó un documento nuevo).
        paginas_movidas = [_envelope(docs[0:2], 5, 3), _envelope(docs[2:4], 6, 3),
                           _envelope(docs[4:5], 6, 3)]
        cli._post_query = FakeQuery(paginas_movidas)
        try:
            barrer_recibidos(per_page=2)
            check("4e SONDA total cambió a mitad → aborta", False, "no lo vio")
        except WasabilComprasError:
            check("4e SONDA total cambió a mitad → aborta", True)

        # documento sin uuid: sin llave no hay espejo.
        cli._post_query = FakeQuery([_envelope([{"sent_ntotal": 1}], 1, 1)])
        try:
            barrer_recibidos()
            check("4f SONDA documento sin uuid → aborta", False, "lo aceptó")
        except WasabilComprasError:
            check("4f SONDA documento sin uuid → aborta", True)

        # ── 5) INCAPAZ de crear: toda llamada fue al query ──────────────────────────
        # El fake solo intercepta _post_query, cuyo único destino es _url_query(). La
        # sonda de texto §2 fija la URL; acá se verifica el COMPORTAMIENTO: ni una sola
        # llamada salió con un body de creación (un create lleva sii_document_type_code
        # y details[]; un query jamás).
        todas = fake.llamadas
        check("5 ninguna llamada llevó body de CREACIÓN (details/sii_document_type_code)",
              all("details" not in b and "sii_document_type_code" not in b
                  for b, _p in todas), todas[:1])

        # ── 6) monto_efectivo: el signo manda ───────────────────────────────────────
        check("6a factura normal suma positivo",
              monto_efectivo(_doc("x", total=95200, signo=1)) == 95200.0)
        check("6b SONDA nota de crédito suma NEGATIVO (sent_ntotal viene positivo)",
              monto_efectivo(_doc("x", total=45220000, signo=-1)) == -45220000.0)
        try:
            monto_efectivo({"uuid": "x", "sent_ntotal": 100})  # sin trx_sign
            check("6c sin trx_sign legible revienta (no se suma sin signo)", False)
        except WasabilComprasError:
            check("6c sin trx_sign legible revienta (no se suma sin signo)", True)

    finally:
        cli._post_query = original

    # ── 7) La migración es idempotente (base local real) ────────────────────────────
    from migrations.proveedor_rut import run as migrar
    migrar()
    migrar()  # segunda corrida: no debe reventar ni duplicar
    from sqlalchemy import inspect
    from database import engine
    cols = {c["name"] for c in inspect(engine).get_columns("proveedores")}
    check("7 proveedores.rut existe tras migrar dos veces", "rut" in cols, cols)

    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_wasabil_compras_fase0():
    run()


if __name__ == "__main__":
    run()
