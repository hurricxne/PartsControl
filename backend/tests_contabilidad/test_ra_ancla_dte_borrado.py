"""SONDA del ANCLA anti doble emisión cuando se ELIMINA una factura (Grupo AM).

Hallazgo ALTO-3 de la re-refutación: `eliminar_factura` calculaba
`ambiguo_sin_resolver = uuid is None and en_vuelo_desde is not None` y borraba la fila
`wasabil_dte` en cualquier otro caso, mientras el comentario de al lado prometía el
invariante contrario («sólo se borra el ancla cuando consta que el documento NO existe en
Wasabil: uuid IS NULL Y en_vuelo_desde IS NULL»). Con uuid conocido y status 4 el ancla se
destruía: `anclas_restantes = 0`.

POR QUÉ IMPORTA (la premisa es la misma que adopta el cinturón del reintento): `uuid` es
el identificador que Wasabil asigna AL CREAR el documento, así que si hay uuid el
documento EXISTE allá; el `status 4` local sólo dice qué respondió el SII la última vez
que preguntamos, y esa foto puede quedar obsoleta. Si además se borra la fila, desaparece
la ÚNICA llave hacia ese documento: la factura nueva por la misma mercadería nace con
otro id y por lo tanto con otra referencia (FACT-<id nuevo>), así que ni el rescate ni el
cinturón pueden encontrar el viejo, y la única defensa que queda es el tope de cantidad.

EL INVARIANTE QUE FIJA ESTA SONDA (las dos mitades, porque una sola miente):
  · la FACTURA de un rechazo se borra igual (bloquearla dejaría la mercadería
    imposible de facturar para siempre — «Reintentar» reenvía el MISMO payload), y
  · el ANCLA no se destruye si hay uuid: se conserva DESLIGADA (factura_id NULL) con la
    nota que dice dónde está el documento.
  · y si el estado local NO permite concluir qué hay en Wasabil, el borrado FALLA CERRADO.

Se mide por el ID de la fila, nunca por `factura_id == <id>`: esa condición se cumple
igual cuando la fila se conserva desligada, así que no distingue destruir de conservar
(era el agujero del check #19 de monza_contabilidad/tests/test_aud_guards.py).

NO emite ni crea documentos reales: cada estado del SII se simula ESCRIBIENDO la fila
`wasabil_dte` a mano, que es justo lo que lee el guard. Cero llamadas a Wasabil/SII.
Datos MARCADOS con __TEST_ANCLA__ (incluidos los uuid), limpieza verificada por DELTA —
la limpieza borra también las filas HUÉRFANAS, que por definición ya no cuelgan de la
factura.

Corre con:  ./venv/bin/python -m pytest tests_contabilidad/test_ra_ancla_dte_borrado.py -q
(también:   ./venv/bin/python tests_contabilidad/test_ra_ancla_dte_borrado.py)
"""
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from database import Base, engine, SessionLocal, get_db  # noqa: E402
from auth import get_current_user  # noqa: E402
from models.models import (  # noqa: E402
    Cotizacion, ItemCotizacion, OcCliente, Despacho, DespachoItem,
    ContFacturaCliente, ContFacturaClienteItem, ContCobranza, ContFactoring, ContAdelanto,
)
from wasabil_dte.models import (  # noqa: E402
    WasabilDte, STATUS_PROCESANDO, STATUS_PENDIENTE, STATUS_EMITIDO, STATUS_FALLIDO,
    CLAIM_TTL_SEGUNDOS,
)
import routers.contabilidad as cont  # noqa: E402

MARK = "__TEST_ANCLA__"
CURRENT = {"empresa": "mineria", "id": None}

Base.metadata.create_all(bind=engine, checkfirst=True)

app = FastAPI()
app.include_router(cont.router, prefix="/api")


def _current_user_realista(db: Session = Depends(get_db)):
    """Auth REALISTA: la lectura en la MISMA sesión del request abre la transacción antes
    de cualquier with_for_update(), igual que auth.get_current_user en producción."""
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=CURRENT["id"], empresa=CURRENT["empresa"])


app.dependency_overrides[get_current_user] = _current_user_realista
client = TestClient(app)

PRECIOS = {}
_orig_precios = cont._precios_de_cotizacion


def _fake_precios(db, cot_id, cfg_dict, items_db=None):
    """Precios FIJOS: el motor real depende del dólar del día y haría flaky la suite."""
    items = items_db if items_db is not None else (
        db.query(ItemCotizacion).filter(ItemCotizacion.cotizacion_id == cot_id).all())
    pmap = {i.id: {"id": i.id, "precio_venta_clp": PRECIOS.get(i.id, 0.0)} for i in items}
    neto = sum(cont._total_linea(PRECIOS.get(i.id, 0.0), float(i.cantidad or 0)) for i in items)
    totales = {"subtotal_neto_clp": neto, "iva_clp": cont._iva_clp(neto),
               "total_con_iva_clp": neto + cont._iva_clp(neto)}
    return items, pmap, totales


_fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _crear_venta(db, sufijo, *, precio=10000.0, cantidad=10):
    """Venta de 1 ítem con guía despachada y FIRMADA (neto 100.000 / bruto 119.000)."""
    cot = Cotizacion(numero=f"{MARK}-COT-{sufijo}", cliente=f"{MARK} HEPI",
                     rut_cliente="78.279.030-7")
    db.add(cot); db.flush()
    it = ItemCotizacion(cotizacion_id=cot.id, item_num=1, numero_parte="1R-0716",
                        descripcion="Filtro", cantidad=cantidad, estado_item="en_bodega")
    db.add(it); db.flush()
    oc = OcCliente(cotizacion_id=cot.id, numero_oc=f"{MARK}-OC-{sufijo}", fecha_oc="2026-07-01")
    db.add(oc); db.flush()
    desp = Despacho(numero_despacho=f"{MARK}-DSP-{oc.id}", oc_cliente_id=oc.id,
                    estado="despachado", guia_firmada=1, numero_guia=f"G-{sufijo}")
    db.add(desp); db.flush()
    db.add(DespachoItem(despacho_id=desp.id, item_cotizacion_id=it.id, qty_despachada=cantidad))
    db.commit()
    PRECIOS.clear()
    PRECIOS.update({it.id: precio})
    return cot, oc, desp, it


def _facturar(db, oc, desp, sufijo):
    """Factura del despacho por HTTP (el camino real) y sin folio local, como la deja la
    ventana de la emisión electrónica."""
    r = client.post("/api/contabilidad/facturas",
                    json={"oc_cliente_id": oc.id, "despacho_id": desp.id,
                          "numero_factura": f"{MARK}-F{sufijo}"})
    assert r.status_code == 200, r.text
    fid = r.json()["id"]
    f = db.query(ContFacturaCliente).filter(ContFacturaCliente.id == fid).first()
    f.numero_factura = None
    db.commit()
    return fid


def _sembrar_dte(db, factura_id, *, status_id, uuid=None, folio=None, en_vuelo=None,
                 error=None):
    """Estado del SII SIMULADO: la fila `wasabil_dte` escrita a mano (jamás se llama a
    Wasabil). Devuelve el ID de la fila — la única forma de saber después si sobrevivió."""
    dte = WasabilDte(tipo_dte=33, factura_id=factura_id, empresa="mineria",
                     status_id=status_id, uuid=uuid, folio=folio, en_vuelo_desde=en_vuelo,
                     error=error)
    db.add(dte)
    db.commit()
    return dte.id


def _fila(db, dte_id):
    db.expire_all()
    return db.query(WasabilDte).filter(WasabilDte.id == dte_id).first()


def _existe_factura(db, factura_id) -> bool:
    return db.query(ContFacturaCliente).filter(
        ContFacturaCliente.id == factura_id).count() == 1


def _conteos(db):
    """Foto de las tablas de plata + anclas, para verificar la limpieza por DELTA."""
    return {t: db.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
            for t in ("cont_factura_cliente", "cont_factura_cliente_item", "cont_cobranza",
                      "cont_factoring", "cont_adelanto", "wasabil_dte")}


def _limpiar(db):
    db.rollback()
    # Las anclas HUÉRFANAS (factura_id NULL) son el punto de esta suite: se limpian por su
    # uuid MARCADO, porque ya no cuelgan de ninguna factura.
    db.query(WasabilDte).filter(WasabilDte.uuid.like(f"{MARK}%")).delete(
        synchronize_session=False)
    cots = db.query(Cotizacion).filter(Cotizacion.numero.like(f"{MARK}%")).all()
    oc_ids = [oc.id for oc in db.query(OcCliente)
              .filter(OcCliente.cotizacion_id.in_([c.id for c in cots])).all()] if cots else []
    if oc_ids:
        fac_ids = [f.id for f in db.query(ContFacturaCliente)
                   .filter(ContFacturaCliente.oc_cliente_id.in_(oc_ids)).all()]
        desp_ids = [d.id for d in db.query(Despacho)
                    .filter(Despacho.oc_cliente_id.in_(oc_ids)).all()]
        adel_ids = [a.id for a in db.query(ContAdelanto)
                    .filter(ContAdelanto.oc_cliente_id.in_(oc_ids)).all()]
        if adel_ids:
            db.query(ContAdelanto).filter(ContAdelanto.id.in_(adel_ids)).update(
                {"factura_anticipo_id": None}, synchronize_session=False)
        if fac_ids:
            db.query(WasabilDte).filter(
                WasabilDte.factura_id.in_(fac_ids)).delete(synchronize_session=False)
            db.query(ContFactoring).filter(
                ContFactoring.factura_id.in_(fac_ids)).delete(synchronize_session=False)
            db.query(ContCobranza).filter(
                ContCobranza.factura_id.in_(fac_ids)).delete(synchronize_session=False)
            db.query(ContFacturaClienteItem).filter(
                ContFacturaClienteItem.factura_id.in_(fac_ids)).delete(synchronize_session=False)
        if adel_ids:
            db.query(ContAdelanto).filter(
                ContAdelanto.id.in_(adel_ids)).delete(synchronize_session=False)
        if fac_ids:
            db.query(ContFacturaCliente).filter(
                ContFacturaCliente.id.in_(fac_ids)).delete(synchronize_session=False)
        if desp_ids:
            db.query(WasabilDte).filter(
                WasabilDte.despacho_id.in_(desp_ids)).delete(synchronize_session=False)
            db.query(DespachoItem).filter(
                DespachoItem.despacho_id.in_(desp_ids)).delete(synchronize_session=False)
            db.query(Despacho).filter(Despacho.id.in_(desp_ids)).delete(synchronize_session=False)
        db.query(OcCliente).filter(OcCliente.id.in_(oc_ids)).delete(synchronize_session=False)
    for cot in cots:
        db.query(ItemCotizacion).filter(
            ItemCotizacion.cotizacion_id == cot.id).delete(synchronize_session=False)
    if cots:
        db.query(Cotizacion).filter(
            Cotizacion.id.in_([c.id for c in cots])).delete(synchronize_session=False)
    db.commit()


def run():
    cont._precios_de_cotizacion = _fake_precios
    db = SessionLocal()
    _limpiar(db)
    antes = _conteos(db)
    try:
        CURRENT["empresa"] = "mineria"

        # ═══ A · EL HALLAZGO: uuid conocido + rechazo confirmado ═══
        # El documento EXISTE en Wasabil (uuid) y el SII lo rechazó la última vez que
        # preguntamos. La factura se borra; el ancla NO.
        cot, oc, desp, it = _crear_venta(db, "A")
        fa = _facturar(db, oc, desp, "A")
        uuid_a = f"{MARK}-u-rechazado"
        dte_a = _sembrar_dte(db, fa, status_id=STATUS_FALLIDO, uuid=uuid_a,
                             error="RUT receptor inválido (respuesta del SII)")
        CURRENT["id"] = 7
        r = client.delete(f"/api/contabilidad/facturas/{fa}")
        check("A1 · la factura de un rechazo SE BORRA (200): bloquearla dejaría la "
              "mercadería imposible de facturar para siempre",
              r.status_code == 200 and not _existe_factura(db, fa),
              {"status": r.status_code, "body": r.text, "factura_viva": _existe_factura(db, fa)})
        fila = _fila(db, dte_a)
        check("A2 · con uuid CONOCIDO el borrado de la factura NO destruye el ancla: la "
              "fila sobrevive DESLIGADA (factura_id NULL) y con su uuid intacto",
              fila is not None and fila.factura_id is None and fila.uuid == uuid_a,
              None if fila is None else {"factura_id": fila.factura_id, "uuid": fila.uuid})
        check("A3 · la nota deja al humano todo lo necesario para cerrarlo en Wasabil "
              "(referencia FACT-<id>, uuid, factura borrada, usuario) y CONSERVA el "
              "error previo del SII",
              fila is not None and "ANCLA CONSERVADA" in (fila.error or "")
              and f"FACT-{fa}" in (fila.error or "")
              and uuid_a in (fila.error or "")
              and "usuario 7" in (fila.error or "")
              and "RUT receptor inválido" in (fila.error or ""),
              None if fila is None else fila.error)
        CURRENT["id"] = None
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": oc.id, "despacho_id": desp.id,
                              "numero_factura": f"{MARK}-FA2"})
        fa2 = r.json().get("id") if r.status_code == 200 else None
        check("A4 · la otra mitad del invariante: la mercadería vuelve a ser facturable "
              "(el ancla conservada NO secuestra el cupo)",
              r.status_code == 200 and r.json()["monto_bruto"] == 119000.0, r.text)
        # El único `uq_wasabil_dte_factura` no se traga los NULL en MySQL: la factura
        # nueva puede tener su PROPIA fila con el ancla huérfana ahí al lado.
        dte_a2 = _sembrar_dte(db, fa2, status_id=None, uuid=f"{MARK}-u-nueva")
        check("A5 · el ancla huérfana no bloquea el ancla de la factura NUEVA "
              "(los NULL no colisionan en el único de factura_id)",
              _fila(db, dte_a2) is not None and _fila(db, dte_a) is not None)
        _limpiar(db)

        # ═══ B · HERMANO: sin uuid no hay llave que perder ═══
        cot, oc, desp, it = _crear_venta(db, "B")
        fb = _facturar(db, oc, desp, "B")
        dte_b = _sembrar_dte(db, fb, status_id=STATUS_FALLIDO, uuid=None,
                             error="connection refused")
        r = client.delete(f"/api/contabilidad/facturas/{fb}")
        check("B1 · fallo CONFIRMADO sin uuid (el documento nunca nació): 200 y el ancla "
              "SÍ se limpia con la factura",
              r.status_code == 200 and not _existe_factura(db, fb) and _fila(db, dte_b) is None,
              {"status": r.status_code, "body": r.text,
               "fila_viva": _fila(db, dte_b) is not None})
        _limpiar(db)

        # ═══ C · FAIL-CLOSED: hay documento y no consta en qué quedó ═══
        cot, oc, desp, it = _crear_venta(db, "C")
        fc = _facturar(db, oc, desp, "C")
        uuid_c = f"{MARK}-u-incognita"
        dte_c = _sembrar_dte(db, fc, status_id=None, uuid=uuid_c)
        r = client.delete(f"/api/contabilidad/facturas/{fc}")
        check("C1 · uuid con estado DESCONOCIDO → 409 (no puede concluir, falla CERRADO) "
              "y el 409 NOMBRA el identificador que el humano tiene que revisar",
              r.status_code == 409 and uuid_c in r.json().get("detail", ""),
              {"status": r.status_code, "body": r.text})
        check("C2 · …y no destruyó nada: la factura sigue viva y el ancla intacta con su "
              "factura_id",
              _existe_factura(db, fc) and _fila(db, dte_c) is not None
              and _fila(db, dte_c).factura_id == fc,
              {"factura": _existe_factura(db, fc),
               "fila": None if _fila(db, dte_c) is None else _fila(db, dte_c).factura_id})
        _limpiar(db)

        # ═══ D · claim VENCIDO con uuid: el ancla tampoco se destruye ═══
        # Antes del arreglo este estado caía en el `db.delete` (uuid no es None, así que
        # `ambiguo_sin_resolver` daba False): se perdía la llave de un documento que
        # PUDO nacer con folio real en el intento cuya respuesta se perdió.
        cot, oc, desp, it = _crear_venta(db, "D")
        fd = _facturar(db, oc, desp, "D")
        vencido = datetime.utcnow() - timedelta(seconds=CLAIM_TTL_SEGUNDOS + 60)
        dte_d = _sembrar_dte(db, fd, status_id=STATUS_FALLIDO, uuid=f"{MARK}-u-vencido",
                             en_vuelo=vencido)
        r = client.delete(f"/api/contabilidad/facturas/{fd}")
        check("D1 · rechazo con claim VENCIDO (respuesta perdida) → 409 y el ancla queda "
              "entera: la salida es «Reintentar», que consulta a Wasabil",
              r.status_code == 409 and _existe_factura(db, fd)
              and _fila(db, dte_d) is not None and _fila(db, dte_d).factura_id == fd,
              {"status": r.status_code, "body": r.text})
        _limpiar(db)

        # ═══ E · los bloqueos que YA existían siguen en pie (un guard demasiado ancho es
        #         tan malo como uno ausente, así que E4 prueba el legítimo) ═══
        cot, oc, desp, it = _crear_venta(db, "E")
        fe = _facturar(db, oc, desp, "E")
        dte_e = _sembrar_dte(db, fe, status_id=STATUS_EMITIDO, uuid=f"{MARK}-u-emitido",
                             folio="999428777")
        r = client.delete(f"/api/contabilidad/facturas/{fe}")
        check("E1 · DTE EMITIDO → 409 con el folio y la nota de crédito, factura y ancla "
              "intactas",
              r.status_code == 409 and "999428777" in r.json().get("detail", "")
              and _existe_factura(db, fe) and _fila(db, dte_e) is not None, r.text)
        db.query(WasabilDte).filter(WasabilDte.id == dte_e).update(
            {"status_id": STATUS_PROCESANDO, "folio": None}, synchronize_session=False)
        db.commit()
        r = client.delete(f"/api/contabilidad/facturas/{fe}")
        check("E2 · DTE PROCESANDO → 409 y el ancla sigue ahí",
              r.status_code == 409 and _fila(db, dte_e) is not None, r.text)
        # Estado 'pendiente' SIN uuid: dice que el documento está vivo y no hay con qué
        # verificarlo. Antes se borraba el ancla (el `in (2, 6)` exigía uuid).
        db.query(WasabilDte).filter(WasabilDte.id == dte_e).update(
            {"status_id": STATUS_PENDIENTE, "uuid": None}, synchronize_session=False)
        db.commit()
        r = client.delete(f"/api/contabilidad/facturas/{fe}")
        check("E3 · estado PENDIENTE sin uuid → 409 (no se concluye) y el ancla sigue ahí",
              r.status_code == 409 and _fila(db, dte_e) is not None, r.text)
        # Claim VIGENTE (hay un request emitiendo ahora mismo)
        db.query(WasabilDte).filter(WasabilDte.id == dte_e).update(
            {"status_id": None, "uuid": None, "en_vuelo_desde": datetime.utcnow()},
            synchronize_session=False)
        db.commit()
        r = client.delete(f"/api/contabilidad/facturas/{fe}")
        check("E4 · claim VIGENTE → 409 y el ancla sigue ahí",
              r.status_code == 409 and _fila(db, dte_e) is not None, r.text)
        # …y el legítimo de al lado: SIN fila DTE la factura se borra como siempre.
        db.query(WasabilDte).filter(WasabilDte.id == dte_e).delete(synchronize_session=False)
        db.commit()
        r = client.delete(f"/api/contabilidad/facturas/{fe}")
        check("E5 · factura SIN emisión electrónica: se borra como siempre (200) — el "
              "guard no se pasó de ancho",
              r.status_code == 200 and not _existe_factura(db, fe), r.text)

    finally:
        _limpiar(db)
        despues = _conteos(db)
        check("limpieza · tablas de plata y anclas como estaban (delta 0, incluidas las "
              "huérfanas)", antes == despues, {"antes": antes, "despues": despues})
        db.close()
        cont._precios_de_cotizacion = _orig_precios
        CURRENT["id"] = None
        print("Cleanup OK")

    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_ra_ancla_dte_borrado():
    run()


if __name__ == "__main__":
    run()
