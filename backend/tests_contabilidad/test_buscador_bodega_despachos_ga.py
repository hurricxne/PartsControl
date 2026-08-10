"""Buscador de operador en Bodega y Despachos — MachParts / Grupo AM.

QUÉ CUBRE (contrato común de docs/spec-buscadores-bodega-despachos-2026-08-05.md):
  · GET /api/bodega/items (NUEVO): cada campo prometido por el placeholder —
    n° de parte (con pasada colapsada sin guiones EN AMBOS SENTIDOS), descripción
    Y nombre_cat (la pantalla imprime `descripcion or nombre_cat`), marca, N° de
    cotización CON y SIN prefijo COT- (la base guarda "2026-0001", la UI imprime
    "COT-2026-0001"), cliente, OC del cliente, N° de embarque, AWB (awb_numero,
    NUNCA `awb` que guarda el NOMBRE DEL ARCHIVO adjunto) y guía del proveedor
    NACIONAL (la compra nacional no pasa por embarques).
  · GET /api/despachos/oc-clientes (EXTENDIDO): pestaña y `q` en SQL, sobre
    {items,total,...}, EXISTS y no join (una OC con 3 ítems que matchean aparece
    UNA vez), búsqueda por N° de despacho / N° de guía / transportista, y los
    campos de cobertura (total_items / items_en_bodega) para el "Listo · N de M".

SONDAS DE PODER DISCRIMINANTE:
  · '5_%BUSCGA' con escape NO matchea 'DESC 50%BUSCGA'; sin escape sí lo haría
    (el '_' comodín se comería el '0' y el '%' el resto). '1009_4567' devuelve
    SOLO la parte con guion bajo LITERAL, no '1009-4567' ni '1009X4567'.
  · El nombre del ARCHIVO adjunto del AWB (columna `awb`) NO se busca: si se
    buscara, 'buscgafile9988' devolvería el embarque.
  · Anti-join: 3 ítems de la misma OC matchean y la OC sale UNA vez con total=1
    (con .join() saldría total=3 y la página perdería filas).

Sin red. Datos con MARK propio y limpieza verificada con sesión NUEVA.

Corre con:  ./venv/bin/python -m pytest tests_contabilidad/test_buscador_bodega_despachos_ga.py -q
"""
import os
import sys
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import SessionLocal, engine, Base  # noqa: E402
from auth import get_current_user  # noqa: E402
from models.models import (  # noqa: E402
    Cotizacion, ItemCotizacion, OcCliente, Embarque, EmbarqueItem,
    Despacho, DespachoItem,
)
from recepcion_nacional.models import (  # noqa: E402
    RecepcionNacional, RecepcionNacionalItem,
)
from routers.bodega import router as bodega_router  # noqa: E402
from routers.despachos import router as despachos_router  # noqa: E402

Base.metadata.create_all(bind=engine, checkfirst=True)

MARK = "test-buscga"          # va en Cotizacion.cliente → ancla de limpieza
CURRENT = {"id": None, "empresa": "mineria"}

app = FastAPI()
app.include_router(bodega_router, prefix="/api")
app.include_router(despachos_router, prefix="/api")
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=CURRENT["id"], email=f"{MARK}@test.invalid", empresa=CURRENT["empresa"])
client = TestClient(app)

_fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _buscar(q=None, **params):
    if q is not None:
        params["q"] = q
    return client.get("/api/bodega/items", params=params).json()


def _oc_list(tab="listas", q=None, **params):
    params["tab"] = tab
    if q is not None:
        params["q"] = q
    return client.get("/api/despachos/oc-clientes", params=params).json()


def _ids(resp):
    return [x["item_cotizacion_id"] for x in resp["items"]]


def _seed(db):
    """4 cotizaciones marcadas que cubren todos los caminos del OR."""
    ids = {}

    # ── COT A: internacional, con OC, embarque, despacho abierto ──
    cot_a = Cotizacion(numero="2077-0987", cliente=f"{MARK} MINERA ESCONDIDA",
                       rut_cliente="76.123.456-7")
    db.add(cot_a); db.flush()
    it_a1 = ItemCotizacion(cotizacion_id=cot_a.id, item_num=1,
                           numero_parte="9T7-1907B",
                           descripcion=f"BOMBA HIDRAULICA {MARK}",
                           marca="CAT", cantidad=10, estado_item="en_bodega")
    # descripcion VACÍA a propósito: el nombre visible viene de nombre_cat
    it_a2 = ItemCotizacion(cotizacion_id=cot_a.id, item_num=2,
                           numero_parte="60031137218", descripcion=None,
                           nombre_cat=f"FILTRO {MARK} CABINA",
                           marca="KOMATSU", cantidad=4, estado_item="embarcado")
    it_a3 = ItemCotizacion(cotizacion_id=cot_a.id, item_num=3,
                           numero_parte="1009_4567",
                           descripcion=f"PARTE UNDERSCORE {MARK}",
                           cantidad=1, estado_item="en_bodega")
    it_a4 = ItemCotizacion(cotizacion_id=cot_a.id, item_num=4,
                           numero_parte="1009-4567",
                           descripcion=f"PARTE GUION {MARK}",
                           cantidad=1, estado_item="en_bodega")
    it_a5 = ItemCotizacion(cotizacion_id=cot_a.id, item_num=5,
                           numero_parte="1009X4567",
                           descripcion=f"PARTE X {MARK}",
                           cantidad=1, estado_item="en_bodega")
    it_a6 = ItemCotizacion(cotizacion_id=cot_a.id, item_num=6,
                           numero_parte="PCT-1",
                           descripcion="DESC 50%BUSCGA",
                           cantidad=1, estado_item="en_bodega")
    it_a7 = ItemCotizacion(cotizacion_id=cot_a.id, item_num=7,
                           numero_parte="ZZBUSCGA-INV",
                           descripcion=f"FUERA DE ALCANCE {MARK}",
                           cantidad=1, estado_item="ingresado")
    db.add_all([it_a1, it_a2, it_a3, it_a4, it_a5, it_a6, it_a7]); db.flush()
    oc_a = OcCliente(cotizacion_id=cot_a.id, numero_oc="4500-BUSCGA-77",
                     fecha_oc="2026-07-01")
    db.add(oc_a); db.flush()
    # awb = NOMBRE DEL ARCHIVO adjunto (trampa documentada); awb_numero = el real
    emb = Embarque(numero="EMB-BUSCGA-70", estado="en_transito",
                   forwarder="DHL", awb="buscgafile9988.pdf",
                   awb_numero="AWB-BUSCGA-99")
    db.add(emb); db.flush()
    db.add(EmbarqueItem(embarque_id=emb.id, item_cotizacion_id=it_a2.id))
    d1 = Despacho(numero_despacho="DSP-BUSCGA-0001", oc_cliente_id=oc_a.id,
                  numero_guia="GUIA-BUSCGA-555",
                  transportista=f"TRANSPORTES {MARK}",
                  estado="en_preparacion")
    db.add(d1); db.flush()
    db.add(DespachoItem(despacho_id=d1.id, item_cotizacion_id=it_a1.id,
                        qty_despachada=2))

    # ── COT B: compra NACIONAL (sin embarque, con guía del proveedor) ──
    cot_b = Cotizacion(numero="2077-0988", cliente=f"{MARK} NACIONAL SPA")
    db.add(cot_b); db.flush()
    it_b1 = ItemCotizacion(cotizacion_id=cot_b.id, item_num=1,
                           numero_parte="NACBUSCGA-01",
                           descripcion=f"REPUESTO NACIONAL {MARK}",
                           cantidad=8, estado_item="en_bodega")
    db.add(it_b1); db.flush()
    oc_b = OcCliente(cotizacion_id=cot_b.id, numero_oc="OCN-BUSCGA-1")
    db.add(oc_b); db.flush()
    rn = RecepcionNacional(numero_guia_proveedor="GUIANAC-BUSCGA-31",
                           estado="cerrada")
    db.add(rn); db.flush()
    db.add(RecepcionNacionalItem(recepcion_id=rn.id,
                                 item_cotizacion_id=it_b1.id,
                                 qty_recibida=Decimal("5"),
                                 estado_recepcion="completo"))

    # ── COT C: 3 ítems que matchean el MISMO término (sonda anti-join) ──
    cot_c = Cotizacion(numero="2077-0990", cliente=f"{MARK} ANDINA LTDA")
    db.add(cot_c); db.flush()
    its_c = [
        ItemCotizacion(cotizacion_id=cot_c.id, item_num=i,
                       numero_parte=f"PAG-BUSCGA-{i}",
                       descripcion=f"PARTE PAGINADA {MARK} {i}",
                       cantidad=1, estado_item="en_bodega")
        for i in (1, 2, 3)
    ]
    db.add_all(its_c); db.flush()
    oc_c = OcCliente(cotizacion_id=cot_c.id, numero_oc="OCC-BUSCGA-2")
    db.add(oc_c); db.flush()

    # ── COT D: cobertura parcial (2 de 3 en bodega) + despacho CERRADO ──
    cot_d = Cotizacion(numero="2077-0989", cliente=f"{MARK} COBRE LTDA")
    db.add(cot_d); db.flush()
    it_d1 = ItemCotizacion(cotizacion_id=cot_d.id, item_num=1,
                           numero_parte="DBUSCGA-1", descripcion=f"PARTE D1 {MARK}",
                           cantidad=2, estado_item="en_bodega")
    it_d2 = ItemCotizacion(cotizacion_id=cot_d.id, item_num=2,
                           numero_parte="DBUSCGA-2", descripcion=f"PARTE D2 {MARK}",
                           cantidad=3, estado_item="en_bodega")
    it_d3 = ItemCotizacion(cotizacion_id=cot_d.id, item_num=3,
                           numero_parte="DBUSCGA-3", descripcion=f"PARTE D3 {MARK}",
                           cantidad=1, estado_item="embarcado")
    db.add_all([it_d1, it_d2, it_d3]); db.flush()
    oc_d = OcCliente(cotizacion_id=cot_d.id, numero_oc="OCD-BUSCGA-3")
    db.add(oc_d); db.flush()
    d2 = Despacho(numero_despacho="DSP-BUSCGA-0002", oc_cliente_id=oc_d.id,
                  numero_guia="GUIA-BUSCGA-777", estado="despachado")
    db.add(d2); db.flush()
    db.add(DespachoItem(despacho_id=d2.id, item_cotizacion_id=it_d1.id,
                        qty_despachada=1))

    db.commit()
    for k, obj in (("cot_a", cot_a), ("it_a1", it_a1), ("it_a2", it_a2),
                   ("it_a3", it_a3), ("it_b1", it_b1), ("oc_a", oc_a),
                   ("oc_c", oc_c), ("oc_d", oc_d)):
        ids[k] = obj.id
    return ids


def _limpiar(db):
    db.rollback()
    S = False  # synchronize_session
    cot_ids = [r[0] for r in db.query(Cotizacion.id)
               .filter(Cotizacion.cliente.like(f"{MARK}%")).all()]
    if cot_ids:
        item_ids = [r[0] for r in db.query(ItemCotizacion.id)
                    .filter(ItemCotizacion.cotizacion_id.in_(cot_ids)).all()]
        oc_ids = [r[0] for r in db.query(OcCliente.id)
                  .filter(OcCliente.cotizacion_id.in_(cot_ids)).all()]
        if oc_ids:
            desp_ids = [r[0] for r in db.query(Despacho.id)
                        .filter(Despacho.oc_cliente_id.in_(oc_ids)).all()]
            if desp_ids:
                db.query(DespachoItem).filter(
                    DespachoItem.despacho_id.in_(desp_ids)).delete(synchronize_session=S)
                db.query(Despacho).filter(
                    Despacho.id.in_(desp_ids)).delete(synchronize_session=S)
            db.query(OcCliente).filter(
                OcCliente.id.in_(oc_ids)).delete(synchronize_session=S)
        if item_ids:
            rn_ids = [r[0] for r in db.query(RecepcionNacionalItem.recepcion_id)
                      .filter(RecepcionNacionalItem.item_cotizacion_id.in_(item_ids))
                      .distinct().all()]
            db.query(RecepcionNacionalItem).filter(
                RecepcionNacionalItem.item_cotizacion_id.in_(item_ids)
            ).delete(synchronize_session=S)
            if rn_ids:
                db.query(RecepcionNacional).filter(
                    RecepcionNacional.id.in_(rn_ids)).delete(synchronize_session=S)
            emb_ids = [r[0] for r in db.query(EmbarqueItem.embarque_id)
                       .filter(EmbarqueItem.item_cotizacion_id.in_(item_ids))
                       .distinct().all()]
            db.query(EmbarqueItem).filter(
                EmbarqueItem.item_cotizacion_id.in_(item_ids)).delete(synchronize_session=S)
            if emb_ids:
                db.query(Embarque).filter(
                    Embarque.id.in_(emb_ids),
                    Embarque.numero.like("EMB-BUSCGA%"),
                ).delete(synchronize_session=S)
            db.query(ItemCotizacion).filter(
                ItemCotizacion.id.in_(item_ids)).delete(synchronize_session=S)
        db.query(Cotizacion).filter(
            Cotizacion.id.in_(cot_ids)).delete(synchronize_session=S)
    db.commit()


def _verificar_limpieza():
    """Sesión NUEVA (regla de la casa): una reutilizada serviría su snapshot."""
    db = SessionLocal()
    try:
        restos = (
            db.query(Cotizacion).filter(Cotizacion.cliente.like(f"{MARK}%")).count()
            + db.query(Embarque).filter(Embarque.numero.like("EMB-BUSCGA%")).count()
            + db.query(Despacho).filter(Despacho.numero_despacho.like("DSP-BUSCGA%")).count()
            + db.query(RecepcionNacional).filter(
                RecepcionNacional.numero_guia_proveedor.like("GUIANAC-BUSCGA%")).count()
        )
        assert restos == 0, f"quedaron {restos} filas {MARK} tras la limpieza"
    finally:
        db.close()
    print("Cleanup OK (verificado con sesión nueva)")


def run():
    db = SessionLocal()
    try:
        _limpiar(db)
        ids = _seed(db)

        # ══ 1) Candado de empresa del endpoint NUEVO ══
        CURRENT["empresa"] = "automotriz"
        r = client.get("/api/bodega/items")
        check("1a bodega/items: automotriz 403", r.status_code == 403, r.text)
        r = client.get("/api/despachos/oc-clientes", params={"tab": "listas"})
        check("1b despachos: automotriz 403 (candado del router intacto)",
              r.status_code == 403, r.text)
        CURRENT["empresa"] = "mineria"
        r = client.get("/api/bodega/items")
        check("1c bodega/items: mineria 200", r.status_code == 200, r.text[:200])

        # ══ 2) Sobre del contrato + mínimo 2 caracteres ══
        base = _buscar()
        check("2a sobre {items,total,page,page_size,q_efectivo,normalizado}",
              all(k in base for k in
                  ("items", "total", "page", "page_size", "q_efectivo", "normalizado")),
              list(base.keys()))
        corto = _buscar("7")
        check("2b con 1 carácter el filtro se ignora por completo",
              corto["total"] == base["total"] and corto["q_efectivo"] == "",
              (corto["total"], base["total"]))

        # ══ 3) Número de parte: infijo y colapsado EN AMBOS SENTIDOS ══
        r = _buscar("9T7-1907B")
        check("3a parte exacta con guiones (pasada A)",
              ids["it_a1"] in _ids(r) and r["normalizado"] is False, r)
        r = _buscar("9T71907B")
        check("3b tecleada SIN guiones encuentra la guardada CON guiones (pasada B)",
              ids["it_a1"] in _ids(r) and r["normalizado"] is True, r)
        check("3c la insignia declara el colapsado (no miente el resaltado)",
              any(m["campo"] == "numero_parte_colapsado"
                  for x in r["items"] if x["item_cotizacion_id"] == ids["it_a1"]
                  for m in x["match"]), r["items"])
        r = _buscar("600-3113-7218")
        check("3d tecleada CON guiones encuentra la guardada SIN guiones",
              ids["it_a2"] in _ids(r) and r["normalizado"] is True, r)

        # ══ 4) descripcion OR nombre_cat (lo que la pantalla imprime) ══
        r = _buscar("CABINA")
        check("4a nombre_cat con descripcion vacía SÍ aparece",
              ids["it_a2"] in _ids(r), r)
        fila = next(x for x in r["items"] if x["item_cotizacion_id"] == ids["it_a2"])
        check("4b la fila imprime el nombre visible (nombre_cat)",
              "CABINA" in fila["descripcion"], fila["descripcion"])

        # ══ 5) COT con y sin prefijo (la UI imprime COT-, la base no) ══
        con = _buscar("COT-2077-0987")
        sin = _buscar("2077-0987")
        check("5a 'COT-2077-0987' encuentra igual que '2077-0987'",
              con["total"] == sin["total"] and con["total"] > 0
              and ids["it_a1"] in _ids(con), (con["total"], sin["total"]))
        parcial = _buscar("77-0987")
        check("5b correlativo PARCIAL (infijo) también encuentra",
              ids["it_a1"] in _ids(parcial), parcial["total"])

        # ══ 6) OC cliente / cliente ══
        r = _buscar("4500-BUSCGA-77")
        check("6a por N° de OC del cliente", ids["it_a1"] in _ids(r), r["total"])
        check("6b insignia 'oc_cliente' con el valor",
              any(m["campo"] == "oc_cliente" and m["valor"] == "4500-BUSCGA-77"
                  for x in r["items"] for m in x["match"]), r["items"])
        r = _buscar("ESCONDIDA")
        check("6c por nombre de cliente", ids["it_a1"] in _ids(r), r["total"])

        # ══ 7) Embarque / AWB — y la TRAMPA del archivo adjunto ══
        r = _buscar("EMB-BUSCGA-70")
        check("7a por N° de embarque (EXISTS, ítem embarcado)",
              _ids(r) == [ids["it_a2"]], _ids(r))
        check("7b insignia 'embarque' lleva el VALOR (columna no visible)",
              any(m["campo"] == "embarque" and m["valor"] == "EMB-BUSCGA-70"
                  for x in r["items"] for m in x["match"]), r["items"])
        r = _buscar("AWB-BUSCGA-99")
        check("7c por N° de AWB (awb_numero)", _ids(r) == [ids["it_a2"]], _ids(r))
        r = _buscar("buscgafile9988")
        check("7d el NOMBRE DEL ARCHIVO adjunto (columna awb) NO se busca",
              r["total"] == 0, r["total"])

        # ══ 8) Guía del proveedor NACIONAL (sin embarque) ══
        r = _buscar("GUIANAC-BUSCGA-31")
        check("8a ítem nacional aparece por su guía de proveedor",
              _ids(r) == [ids["it_b1"]], _ids(r))
        fila = r["items"][0] if r["items"] else {}
        check("8b la fila dice guía nacional y NO inventa embarque",
              fila.get("guias_nacionales") == ["GUIANAC-BUSCGA-31"]
              and fila.get("embarques") == [], fila)
        check("8c cobertura por línea: recibido 5 de 8, disponible 5",
              fila.get("qty_recibida") == 5.0 and fila.get("qty_disponible") == 5.0,
              (fila.get("qty_recibida"), fila.get("qty_disponible")))

        # ══ 9) % y _ LITERALES (escape del LIKE) ══
        r = _buscar("1009_4567")
        check("9a '1009_4567' devuelve SOLO la parte con guion bajo literal",
              _ids(r) == [ids["it_a3"]]
              and r["items"][0]["numero_parte"] == "1009_4567", _ids(r))
        r = _buscar("50%BUSCGA")
        check("9b '%' literal se encuentra a sí mismo",
              r["total"] == 1 and r["items"][0]["numero_parte"] == "PCT-1", r["total"])
        r = _buscar("5_%BUSCGA")
        check("9c SONDA: '5_%BUSCGA' NO matchea 'DESC 50%BUSCGA' (sin escape sí)",
              r["total"] == 0, r["total"])

        # ══ 10) Espacios y multi-token (AND entre tokens, OR entre campos) ══
        r = _buscar("  2077-0987  ")
        check("10a espacios pegados no rompen la búsqueda",
              ids["it_a1"] in _ids(r), r["total"])
        r = _buscar("BOMBA 2077-0987")
        check("10b dos tokens sobre campos DISTINTOS de la misma fila",
              _ids(r) == [ids["it_a1"]], _ids(r))
        r = _buscar("BOMBA ZZNOEXISTEXYZ")
        check("10c AND real: un token sin match anula la fila", r["total"] == 0,
              r["total"])

        # ══ 11) Alcance de estados ══
        r = _buscar("ZZBUSCGA-INV")
        check("11a 'ingresado' queda FUERA del alcance del buscador",
              r["total"] == 0, r["total"])
        r = _buscar("BUSCGA", estado="embarcado")
        check("11b filtro de estado acota (todas las filas embarcadas)",
              r["total"] > 0 and all(x["estado_item"] == "embarcado" for x in r["items"]),
              [(x["numero_parte"], x["estado_item"]) for x in r["items"]])

        # ══ 12) Paginación honesta y orden determinista ══
        p1 = _buscar("PAG-BUSCGA", page_size=2, page=1)
        p2 = _buscar("PAG-BUSCGA", page_size=2, page=2)
        check("12a total dice 3 aunque la página muestre 2",
              p1["total"] == 3 and len(p1["items"]) == 2, (p1["total"], len(p1["items"])))
        check("12b la página 2 trae el resto, sin duplicar ni perder",
              len(p2["items"]) == 1 and not set(_ids(p1)) & set(_ids(p2)),
              (_ids(p1), _ids(p2)))
        check("12c orden id DESC (desempate determinista)",
              _ids(p1) == sorted(_ids(p1), reverse=True), _ids(p1))

        # ══ 13) Despachos: sobre + pestañas en SQL ══
        r = client.get("/api/despachos/oc-clientes", params={"tab": "xyz"})
        check("13a tab inválido sigue dando 400", r.status_code == 400, r.text)
        r = _oc_list("listas", "OCD-BUSCGA-3")
        check("13b sobre {items,total,...} en oc-clientes",
              all(k in r for k in ("items", "total", "page", "page_size")), list(r.keys()))
        check("13c la OC aparece en 'listas'",
              [x["numero_oc"] for x in r["items"]] == ["OCD-BUSCGA-3"], r["items"])
        card = r["items"][0] if r["items"] else {}
        check("13d cobertura para el card: 'Listo · 2 de 3' (en_bodega/total)",
              card.get("items_en_bodega") == 2 and card.get("total_items") == 3
              and card.get("estado") == "listo",
              (card.get("items_en_bodega"), card.get("total_items"), card.get("estado")))

        # ══ 14) Despachos: sonda anti-join (EXISTS, nunca .join) ══
        r = _oc_list("listas", "PAG-BUSCGA")
        check("14a una OC con 3 ítems que matchean aparece UNA vez y total=1",
              r["total"] == 1 and len(r["items"]) == 1
              and r["items"][0]["numero_oc"] == "OCC-BUSCGA-2",
              (r["total"], [x["numero_oc"] for x in r["items"]]))

        # ══ 15) Despachos: los campos NUEVOS del OR ══
        r = _oc_list("en_curso", "DSP-BUSCGA-0001")
        check("15a por N° de DESPACHO en 'en_curso'",
              [x["numero_oc"] for x in r["items"]] == ["4500-BUSCGA-77"], r["items"])
        check("15b insignia 'despacho' con el valor",
              any(m["campo"] == "despacho" and m["valor"] == "DSP-BUSCGA-0001"
                  for x in r["items"] for m in (x.get("match") or [])), r["items"])
        r = _oc_list("historial", "GUIA-BUSCGA-777")
        check("15c por N° de GUÍA en 'historial'",
              [x["numero_oc"] for x in r["items"]] == ["OCD-BUSCGA-3"], r["items"])
        r = _oc_list("listas", "9T7-1907B")
        check("15d por N° de PARTE (EXISTS a ítems)",
              any(x["numero_oc"] == "4500-BUSCGA-77" for x in r["items"]), r["items"])
        r = _oc_list("listas", "9T71907B")
        check("15e parte sin guiones también llega a la OC (pasada B)",
              any(x["numero_oc"] == "4500-BUSCGA-77" for x in r["items"])
              and r["normalizado"] is True, (r["total"], r["normalizado"]))
        r = _oc_list("listas", "AWB-BUSCGA-99")
        check("15f por AWB del embarque (4 saltos, EXISTS)",
              any(x["numero_oc"] == "4500-BUSCGA-77" for x in r["items"]), r["items"])
        con = _oc_list("listas", "COT-2077-0987")
        sin = _oc_list("listas", "2077-0987")
        check("15g COT con y sin prefijo dan lo mismo",
              con["total"] == sin["total"] and con["total"] > 0,
              (con["total"], sin["total"]))
        r = _oc_list("en_curso", f"TRANSPORTES {MARK}")
        check("15h por transportista (dos tokens AND)",
              any(x["numero_oc"] == "4500-BUSCGA-77" for x in r["items"]), r["items"])

        # ══ 16) Despachos: paginación honesta ══
        p1 = _oc_list("listas", MARK, page_size=2, page=1)
        p2 = _oc_list("listas", MARK, page_size=2, page=2)
        todas = {x["id"] for x in p1["items"]} | {x["id"] for x in p2["items"]}
        check("16a total honesto con la página recortada",
              p1["total"] == 4 and len(p1["items"]) == 2, (p1["total"], len(p1["items"])))
        check("16b páginas disjuntas que suman el total",
              len(todas) == 4 and not
              ({x["id"] for x in p1["items"]} & {x["id"] for x in p2["items"]}),
              (len(todas)))

    finally:
        _limpiar(db)
        db.close()
    _verificar_limpieza()
    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_buscador_bodega_despachos_ga():
    run()


if __name__ == "__main__":
    run()
