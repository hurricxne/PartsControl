"""SONDA del ANCLA anti doble emisión cuando se ELIMINA una factura (MonzaParts).

Espejo de tests_contabilidad/test_ra_ancla_dte_borrado.py (Grupo AM) — el hallazgo ALTO-3
tenía gemelo aquí: `_bloqueo_dte_factura` era algo más estricto que GA (bloqueaba
cualquier claim en vuelo) pero con el MISMO hueco del uuid: con uuid conocido y status 4
hacía `db.delete(dte)` y el ancla desaparecía.

POR QUÉ IMPORTA: `uuid` es el identificador que Wasabil asigna AL CREAR el documento, así
que si hay uuid el documento EXISTE allá; el `status 4` local sólo dice qué respondió el
SII la última vez que preguntamos, y esa foto puede quedar obsoleta (misma premisa del
cinturón anti doble emisión del reintento). Al borrarse la fila desaparece la única llave
hacia ese documento: la factura nueva por la misma mercadería nace con otro id → otra
referencia (FACT-<id nuevo>), inencontrable para el rescate y para el cinturón.

EL INVARIANTE QUE FIJA ESTA SONDA (las dos mitades):
  · la FACTURA de un rechazo se borra igual (si no, la mercadería queda imposible de
    facturar para siempre: «Reintentar» reenvía el MISMO payload), y
  · el ANCLA no se destruye si hay uuid: se conserva DESLIGADA (factura_id NULL) con la
    nota que dice dónde está el documento.
  · y si el estado local NO permite concluir qué hay en Wasabil, el borrado FALLA CERRADO.

Se mide por el ID de la fila, nunca por `factura_id == <id>`: esa condición se cumple
igual cuando la fila se conserva desligada, así que no distingue destruir de conservar
(era el agujero del check #19 de test_aud_guards.py, corregido en la misma pasada).

NO emite ni crea documentos reales: cada estado del SII se simula ESCRIBIENDO la fila
`monza_wasabil_dte` a mano, que es justo lo que lee el guard. Cero llamadas a Wasabil/SII
(MonzaParts todavía no hizo su primera emisión real: esta suite no la adelanta).
Datos MARCADOS con __T_MANCLA__ (incluidos los uuid) y limpieza verificada por DELTA,
que incluye las filas HUÉRFANAS — por definición ya no cuelgan de ninguna factura.

Corre con:
  ./venv/bin/python -m pytest monza_contabilidad/tests/test_ra_ancla_dte_borrado.py -q
  ./venv/bin/python monza_contabilidad/tests/test_ra_ancla_dte_borrado.py
"""
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from database import SessionLocal, get_db  # noqa: E402
from auth import get_current_user  # noqa: E402
import monza_models as mm  # noqa: E402
from monza_contabilidad.router import router  # noqa: E402
from monza_contabilidad.models import (  # noqa: E402
    MonzaContFacturaCliente, MonzaContCobranza, MonzaContFactoring,
    MonzaContFacturaClienteItem, MonzaContAdelanto,
)

MARK = "__T_MANCLA__"
CURRENT = {"empresa": "automotriz", "id": 1}

app = FastAPI()
app.include_router(router)


def _cu(db: Session = Depends(get_db)):
    """Auth REALISTA: la lectura en la MISMA sesión del request abre el read view antes de
    cualquier with_for_update(), igual que auth.get_current_user en producción."""
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=CURRENT["id"], empresa=CURRENT["empresa"])


app.dependency_overrides[get_current_user] = _cu
client = TestClient(app)

_fails = []
_seed = {}


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


# ── El módulo DTE es opcional en un entorno a medias: si la tabla no está migrada, la
# suite se SALTA en vez de fallar (mismo criterio que test_aud_guards).
def _dte_disponible() -> bool:
    db = SessionLocal()
    try:
        from monza_wasabil_dte.models import MonzaWasabilDte  # noqa: F401
        db.execute(text("SELECT 1 FROM monza_wasabil_dte LIMIT 1"))
        return True
    except Exception as e:  # noqa: BLE001
        print(f"SKIP | módulo monza_wasabil_dte no disponible en esta BD: {e}")
        return False
    finally:
        db.close()


def _seed_venta(db, sufijo, *, cantidad=10, precio=100000):
    """Venta vendida con 1 ítem despachado y su guía (neto 1.000.000 / bruto 1.190.000)."""
    cli = db.query(mm.MonzaCliente).filter(mm.MonzaCliente.nombre == f"{MARK} Cliente").first()
    if cli is None:
        cli = mm.MonzaCliente(nombre=f"{MARK} Cliente", rut="11.111.111-1")
        db.add(cli); db.flush()
    neto = cantidad * precio
    cot = mm.MonzaCotizacion(
        numero=f"{MARK}-C{sufijo}", cliente_id=cli.id, estado="vendida",
        total_neto=neto, iva_monto=round(neto * 0.19), total_bruto=round(neto * 1.19),
        iva_pct=19, forma_pago="credito", oc_cliente=f"OC-{sufijo}",
    )
    db.add(cot); db.flush()
    item = mm.MonzaCotizacionItem(
        cotizacion_id=cot.id, descripcion="Filtro", numero_parte=f"FP-{sufijo}",
        cantidad=cantidad, precio_unitario_clp=precio, subtotal_clp=neto,
        estado_linea="despachado",
    )
    db.add(item); db.flush()
    desp = mm.MonzaDespacho(numero=f"{MARK}-DSP-{sufijo}", cotizacion_id=cot.id,
                            estado="despachado", numero_guia=f"G-{sufijo}",
                            cliente_nombre=cli.nombre)
    db.add(desp); db.flush()
    di = mm.MonzaDespachoItem(despacho_id=desp.id, item_id=item.id, qty_despachada=cantidad)
    db.add(di); db.flush()
    db.commit()
    _seed.setdefault("cli_id", cli.id)
    return cot.id, item.id, desp.id, di.id


def _facturar(db, cot_id, item_id, di_id, sufijo, *, cantidad=10):
    """Factura por HTTP (el camino real) y sin folio local, como la deja la ventana de la
    emisión electrónica (la vía manual exige N°, así que se pone y se quita)."""
    r = client.post("/api/monza/contabilidad/facturas", json={
        "cotizacion_id": cot_id,
        "items": [{"item_cotizacion_id": item_id, "despacho_item_id": di_id,
                   "cantidad": cantidad}],
        "numero_factura": f"{MARK}-F{sufijo}", "plazo_dias": 30,
    })
    assert r.status_code == 200, r.text
    fid = r.json()["id"]
    f = db.query(MonzaContFacturaCliente).filter(MonzaContFacturaCliente.id == fid).first()
    f.numero_factura = None
    db.commit()
    return fid


def _sembrar_dte(db, factura_id, *, status_id, uuid=None, folio=None, en_vuelo=None,
                 error=None):
    """Estado del SII SIMULADO: la fila escrita a mano (jamás se llama a Wasabil).
    Devuelve el ID de la fila — la única forma de saber después si sobrevivió."""
    from monza_wasabil_dte.models import MonzaWasabilDte
    dte = MonzaWasabilDte(empresa="automotriz", tipo_dte=33, factura_id=factura_id,
                          status_id=status_id, uuid=uuid, folio=folio,
                          en_vuelo_desde=en_vuelo, error=error)
    db.add(dte)
    db.commit()
    return dte.id


def _fila(db, dte_id):
    from monza_wasabil_dte.models import MonzaWasabilDte
    db.expire_all()
    return db.query(MonzaWasabilDte).filter(MonzaWasabilDte.id == dte_id).first()


def _existe_factura(db, factura_id) -> bool:
    db.expire_all()
    return db.query(MonzaContFacturaCliente).filter(
        MonzaContFacturaCliente.id == factura_id).count() == 1


def _conteos(db):
    return {t: db.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
            for t in ("monza_cont_factura_cliente", "monza_cont_factura_cliente_item",
                      "monza_cont_cobranza", "monza_cont_factoring", "monza_cont_adelanto",
                      "monza_wasabil_dte")}


def _limpiar(db):
    from monza_wasabil_dte.models import MonzaWasabilDte
    db.rollback()
    # Las anclas HUÉRFANAS (factura_id NULL) son el punto de esta suite: se limpian por su
    # uuid MARCADO, porque ya no cuelgan de ninguna factura.
    db.query(MonzaWasabilDte).filter(MonzaWasabilDte.uuid.like(f"{MARK}%")).delete(
        synchronize_session=False)
    cots = db.query(mm.MonzaCotizacion).filter(
        mm.MonzaCotizacion.numero.like(f"{MARK}%")).all()
    cot_ids = [c.id for c in cots]
    if cot_ids:
        fac_ids = [f.id for f in db.query(MonzaContFacturaCliente).filter(
            MonzaContFacturaCliente.cotizacion_id.in_(cot_ids)).all()]
        if fac_ids:
            db.query(MonzaWasabilDte).filter(
                MonzaWasabilDte.factura_id.in_(fac_ids)).delete(synchronize_session=False)
            db.query(MonzaContCobranza).filter(
                MonzaContCobranza.factura_id.in_(fac_ids)).delete(synchronize_session=False)
            db.query(MonzaContFactoring).filter(
                MonzaContFactoring.factura_id.in_(fac_ids)).delete(synchronize_session=False)
            db.query(MonzaContFacturaClienteItem).filter(
                MonzaContFacturaClienteItem.factura_id.in_(fac_ids)).delete(
                synchronize_session=False)
            db.query(MonzaContFacturaCliente).filter(
                MonzaContFacturaCliente.id.in_(fac_ids)).delete(synchronize_session=False)
        db.query(MonzaContAdelanto).filter(
            MonzaContAdelanto.cotizacion_id.in_(cot_ids)).delete(synchronize_session=False)
        desp_ids = [d.id for d in db.query(mm.MonzaDespacho).filter(
            mm.MonzaDespacho.cotizacion_id.in_(cot_ids)).all()]
        if desp_ids:
            db.query(MonzaWasabilDte).filter(
                MonzaWasabilDte.despacho_id.in_(desp_ids)).delete(synchronize_session=False)
            db.query(mm.MonzaDespachoItem).filter(
                mm.MonzaDespachoItem.despacho_id.in_(desp_ids)).delete(synchronize_session=False)
            db.query(mm.MonzaDespacho).filter(
                mm.MonzaDespacho.id.in_(desp_ids)).delete(synchronize_session=False)
        db.query(mm.MonzaCotizacionItem).filter(
            mm.MonzaCotizacionItem.cotizacion_id.in_(cot_ids)).delete(synchronize_session=False)
        db.query(mm.MonzaCotizacion).filter(
            mm.MonzaCotizacion.id.in_(cot_ids)).delete(synchronize_session=False)
    if _seed.get("cli_id"):
        db.query(mm.MonzaCliente).filter(
            mm.MonzaCliente.id == _seed["cli_id"]).delete(synchronize_session=False)
        _seed.pop("cli_id", None)
    db.commit()


def run():
    if not _dte_disponible():
        return
    from monza_wasabil_dte.models import (
        MonzaWasabilDte, STATUS_PROCESANDO, STATUS_PENDIENTE, STATUS_EMITIDO, STATUS_FALLIDO,
    )
    from monza_wasabil_dte.models import CLAIM_TTL_SEGUNDOS
    db = SessionLocal()
    _limpiar(db)
    antes = _conteos(db)
    try:
        CURRENT["empresa"] = "automotriz"

        # ═══ A · EL HALLAZGO: uuid conocido + rechazo confirmado ═══
        cot_a, it_a, dsp_a, di_a = _seed_venta(db, "A")
        fa = _facturar(db, cot_a, it_a, di_a, "A")
        uuid_a = f"{MARK}-u-rechazado"
        dte_a = _sembrar_dte(db, fa, status_id=STATUS_FALLIDO, uuid=uuid_a,
                             error="RUT receptor inválido (respuesta del SII)")
        CURRENT["id"] = 9
        r = client.delete(f"/api/monza/contabilidad/facturas/{fa}")
        check("A1 · la factura de un rechazo SE BORRA (200): bloquearla dejaría la "
              "mercadería imposible de facturar para siempre",
              r.status_code == 200 and not _existe_factura(db, fa),
              {"status": r.status_code, "body": r.text})
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
              and "usuario 9" in (fila.error or "")
              and "RUT receptor inválido" in (fila.error or ""),
              None if fila is None else fila.error)
        CURRENT["id"] = 1
        r = client.post("/api/monza/contabilidad/facturas", json={
            "cotizacion_id": cot_a,
            "items": [{"item_cotizacion_id": it_a, "despacho_item_id": di_a, "cantidad": 10}],
            "numero_factura": f"{MARK}-FA2", "plazo_dias": 30,
        })
        fa2 = r.json().get("id") if r.status_code == 200 else None
        check("A4 · la otra mitad del invariante: la mercadería vuelve a ser facturable "
              "(el ancla conservada NO secuestra el cupo)",
              r.status_code == 200 and r.json()["monto_bruto"] == 1190000.0, r.text)
        dte_a2 = _sembrar_dte(db, fa2, status_id=None, uuid=f"{MARK}-u-nueva")
        check("A5 · el ancla huérfana no bloquea el ancla de la factura NUEVA "
              "(los NULL no colisionan en el único de factura_id)",
              _fila(db, dte_a2) is not None and _fila(db, dte_a) is not None)
        _limpiar(db)

        # ═══ B · HERMANO: sin uuid no hay llave que perder ═══
        cot_b, it_b, dsp_b, di_b = _seed_venta(db, "B")
        fb = _facturar(db, cot_b, it_b, di_b, "B")
        dte_b = _sembrar_dte(db, fb, status_id=STATUS_FALLIDO, uuid=None,
                             error="connection refused")
        r = client.delete(f"/api/monza/contabilidad/facturas/{fb}")
        check("B1 · fallo CONFIRMADO sin uuid (el documento nunca nació): 200 y el ancla "
              "SÍ se limpia con la factura",
              r.status_code == 200 and not _existe_factura(db, fb) and _fila(db, dte_b) is None,
              {"status": r.status_code, "body": r.text})
        _limpiar(db)

        # ═══ C · FAIL-CLOSED: hay documento y no consta en qué quedó ═══
        cot_c, it_c, dsp_c, di_c = _seed_venta(db, "C")
        fc = _facturar(db, cot_c, it_c, di_c, "C")
        uuid_c = f"{MARK}-u-incognita"
        dte_c = _sembrar_dte(db, fc, status_id=None, uuid=uuid_c)
        r = client.delete(f"/api/monza/contabilidad/facturas/{fc}")
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

        # ═══ D · claim VENCIDO con uuid (Monza ya bloqueaba: se fija que siga así) ═══
        cot_d, it_d, dsp_d, di_d = _seed_venta(db, "D")
        fd = _facturar(db, cot_d, it_d, di_d, "D")
        vencido = datetime.utcnow() - timedelta(seconds=CLAIM_TTL_SEGUNDOS + 60)
        dte_d = _sembrar_dte(db, fd, status_id=STATUS_FALLIDO, uuid=f"{MARK}-u-vencido",
                             en_vuelo=vencido)
        r = client.delete(f"/api/monza/contabilidad/facturas/{fd}")
        check("D1 · rechazo con claim VENCIDO (respuesta perdida) → 409 y el ancla queda "
              "entera: la salida es «Reintentar», que consulta a Wasabil",
              r.status_code == 409 and _existe_factura(db, fd)
              and _fila(db, dte_d) is not None and _fila(db, dte_d).factura_id == fd,
              {"status": r.status_code, "body": r.text})
        _limpiar(db)

        # ═══ E · los bloqueos que YA existían siguen en pie, y el legítimo no se bloquea ═══
        cot_e, it_e, dsp_e, di_e = _seed_venta(db, "E")
        fe = _facturar(db, cot_e, it_e, di_e, "E")
        dte_e = _sembrar_dte(db, fe, status_id=STATUS_EMITIDO, uuid=f"{MARK}-u-emitido",
                             folio="999428777")
        r = client.delete(f"/api/monza/contabilidad/facturas/{fe}")
        check("E1 · DTE EMITIDO → 409 que nombra el folio, factura y ancla intactas",
              r.status_code == 409 and "999428777" in r.json().get("detail", "")
              and _existe_factura(db, fe) and _fila(db, dte_e) is not None, r.text)
        db.query(MonzaWasabilDte).filter(MonzaWasabilDte.id == dte_e).update(
            {"status_id": STATUS_PROCESANDO, "folio": None}, synchronize_session=False)
        db.commit()
        r = client.delete(f"/api/monza/contabilidad/facturas/{fe}")
        check("E2 · DTE PROCESANDO → 409 y el ancla sigue ahí",
              r.status_code == 409 and _fila(db, dte_e) is not None, r.text)
        db.query(MonzaWasabilDte).filter(MonzaWasabilDte.id == dte_e).update(
            {"status_id": STATUS_PENDIENTE, "uuid": None}, synchronize_session=False)
        db.commit()
        r = client.delete(f"/api/monza/contabilidad/facturas/{fe}")
        check("E3 · estado PENDIENTE sin uuid → 409 (no se concluye) y el ancla sigue ahí",
              r.status_code == 409 and _fila(db, dte_e) is not None, r.text)
        db.query(MonzaWasabilDte).filter(MonzaWasabilDte.id == dte_e).update(
            {"status_id": None, "uuid": None, "en_vuelo_desde": datetime.utcnow()},
            synchronize_session=False)
        db.commit()
        r = client.delete(f"/api/monza/contabilidad/facturas/{fe}")
        check("E4 · claim VIGENTE → 409 y el ancla sigue ahí",
              r.status_code == 409 and _fila(db, dte_e) is not None, r.text)
        db.query(MonzaWasabilDte).filter(
            MonzaWasabilDte.id == dte_e).delete(synchronize_session=False)
        db.commit()
        r = client.delete(f"/api/monza/contabilidad/facturas/{fe}")
        check("E5 · factura SIN emisión electrónica: se borra como siempre (200) — el "
              "guard no se pasó de ancho",
              r.status_code == 200 and not _existe_factura(db, fe), r.text)

    finally:
        _limpiar(db)
        despues = _conteos(db)
        check("limpieza · tablas de plata y anclas como estaban (delta 0, incluidas las "
              "huérfanas)", antes == despues, {"antes": antes, "despues": despues})
        db.close()
        CURRENT["id"] = 1
        print("Cleanup OK")

    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_ra_ancla_dte_borrado_monza():
    run()


if __name__ == "__main__":
    run()
