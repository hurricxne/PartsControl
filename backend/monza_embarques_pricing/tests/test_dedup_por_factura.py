"""MonzaParts: el anti-duplicado de la factura del forwarder es por FACTURA FÍSICA.

Espejo de `embarques_pricing/tests/test_dedup_por_factura.py` (Grupo AM) sobre las tablas
monza_*. Los re-auditores reprodujeron el MISMO daño en esta marca, con los mismos números
(Σ CxP = 380.800 por una factura de 190.400), así que el cierre va en las dos.

EL DAÑO QUE ESTA SUITE CIERRA (re-auditoría · 3 hallazgos)
---------------------------------------------------------
  H1 (ALTO, un clic) el freno era por LÍNEA (`emb_pricing_gasto_id`) y las 6 líneas del
     pricing son 6 llaves distintas: mover el monto de 'agencia' a 'otros' dejaba la línea
     vieja "registrada" en $0 (pill verde sobre un cero) y la nueva "no registrada" con la
     plata → el botón «Registrar como compra» reaparecía y la CxP se duplicaba.
  H2 (ALTO) la identidad quedó estable pero el MONTO no: la línea se podía editar después
     de registrar la CxP y la divergencia se CONGELABA al cerrar (callejón sin salida).
  H3 (MEDIO) la vía MANUAL («Nueva compra», sin la llave del gasto) nunca tuvo red.

ARQUITECTURA DEL CIERRE (no parche) — la línea del pricing sigue siendo el VÍNCULO del
costeo; el FRENO mira la factura física:
  · `monza_compras_contab/router.py::_bloqueo_factura_fisica`
      REGLA 1 — mismo embarque + mismo N° de documento = la MISMA factura → 409 (aunque el
                RUT no coincida, que es donde el UNIQUE existente no llega).
      REGLA 2 — factura SIN N° de documento: no tiene identidad. Si ya hay una CxP ACTIVA
                del mismo acreedor por el mismo bruto → 409 pidiendo el N°.
  · `monza_compras_contab/router.py::_porton_pricing_del_embarque` — candado de la CABECERA
      del pricing (una fila por embarque = el alcance del chequeo), antes del lock del gasto.
  · `monza_embarques_pricing/router.py::_bloqueo_monto_gasto_con_cxp` — el monto de una
      línea con CxP ACTIVA no puede quedar divergente: 409 en el PUT y en el CIERRE, más
      aviso visible en `advertencias`. Mata además la MECÁNICA de H1.

MÉTODO: todo por HTTP contra los routers REALES (Pricing + Compras/CxP juntos) o leyendo la
BD con conexión NUEVA. Cero introspección de código. El escenario es el ADVERSO y real: las
6 líneas nacen sin N° de factura, la CxP se registra SIN N° de documento y la divergencia
legada se planta escribiendo la fila POR FUERA de la API (como quedó en producción antes del
guard). Cada bloqueo se prueba en los dos sentidos (frena el peligroso, no frena el legítimo
de al lado). Datos MARCADOS + limpieza + verificación por deltas. Sin SII de por medio.

Corre con:  cd backend && ./venv/bin/python -m pytest monza_embarques_pricing/tests/test_dedup_por_factura.py -q
"""
import os
import sys
import threading
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from database import SessionLocal, engine, get_db  # noqa: E402
from auth import get_current_user  # noqa: E402
import monza_models as mm  # noqa: E402
from monza_embarques_pricing.models import (  # noqa: E402
    MonzaEmbPricing, MonzaEmbPricingGasto, MonzaEmbPricingItem,
)
from monza_embarques_pricing.router import router as pricing_router  # noqa: E402
from monza_compras_contab.router import router as compras_router  # noqa: E402
from monza_compras_contab.models import (  # noqa: E402
    MonzaContCompra, MonzaContEgreso, MonzaContEgresoDetalle, MonzaContCompraItem,
)

MARK = "__T_MEP_FC__"       # corto: monza_cotizaciones.numero es VARCHAR(20)
FORWARDER = f"{MARK} FWD"

NETO, IVA = 160_000.0, 30_400.0
BRUTO = NETO + IVA              # 190.400 — el Σ CxP correcto
NETO2, IVA2 = 50_000.0, 9_500.0
BRUTO2 = NETO2 + IVA2           # 59.500 — una factura DISTINTA del mismo acreedor


def _cu(db: Session = Depends(get_db)):
    """Auth realista: toca la base con la MISMA sesión del request (igual que auth.py)."""
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=None, email=f"{MARK}@test.invalid", empresa="automotriz")


app = FastAPI()
app.include_router(pricing_router)      # /api/monza/embarques-pricing (el router trae prefijo)
app.include_router(compras_router)      # /api/monza/compras-contab
app.dependency_overrides[get_current_user] = _cu
cli = TestClient(app)

_fails: list = []


def check(name, cond, extra=""):
    print(("OK   " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _url(emb_id: int) -> str:
    return f"/api/monza/embarques-pricing/{emb_id}"


def _f(x) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


# ─── Seed / limpieza ──────────────────────────────────────────────────────────
def _purge(db: Session) -> None:
    """Borra TODO lo marcado en orden seguro (idempotente)."""
    compras = db.query(MonzaContCompra).filter(
        MonzaContCompra.referencia.like(f"{MARK}%")).all()
    egreso_ids = set()
    for c in compras:
        for d in db.query(MonzaContEgresoDetalle).filter(
                MonzaContEgresoDetalle.compra_id == c.id).all():
            egreso_ids.add(d.egreso_id)
            db.delete(d)
        db.query(MonzaContCompraItem).filter(
            MonzaContCompraItem.compra_id == c.id).delete(synchronize_session=False)
        db.flush()
        db.delete(c)
        db.flush()
    for eid in egreso_ids:
        eg = db.query(MonzaContEgreso).filter(MonzaContEgreso.id == eid).first()
        if eg and not db.query(MonzaContEgresoDetalle).filter(
                MonzaContEgresoDetalle.egreso_id == eid).first():
            db.delete(eg)
    db.flush()
    for emb in db.query(mm.MonzaEmbarque).filter(
            mm.MonzaEmbarque.numero.like(f"{MARK}%")).all():
        pr = db.query(MonzaEmbPricing).filter(
            MonzaEmbPricing.embarque_id == emb.id).first()
        if pr:
            db.query(MonzaEmbPricingItem).filter(
                MonzaEmbPricingItem.pricing_id == pr.id).delete(synchronize_session=False)
            db.query(MonzaEmbPricingGasto).filter(
                MonzaEmbPricingGasto.pricing_id == pr.id).delete(synchronize_session=False)
            db.query(MonzaEmbPricing).filter(
                MonzaEmbPricing.id == pr.id).delete(synchronize_session=False)
        db.query(mm.MonzaEmbarqueItem).filter(
            mm.MonzaEmbarqueItem.embarque_id == emb.id).delete(synchronize_session=False)
        db.flush()
        db.delete(emb)
        db.flush()
    for cot in db.query(mm.MonzaCotizacion).filter(
            mm.MonzaCotizacion.numero.like(f"{MARK}%")).all():
        db.query(mm.MonzaCotizacionItem).filter(
            mm.MonzaCotizacionItem.cotizacion_id == cot.id).delete(synchronize_session=False)
        db.delete(cot)
        db.flush()
    for cliente in db.query(mm.MonzaCliente).filter(
            mm.MonzaCliente.nombre.like(f"{MARK}%")).all():
        db.delete(cliente)
        db.flush()
    db.commit()


def seed():
    """Cotización vendida con 2 ítems USD + embarque marcado."""
    db = SessionLocal()
    try:
        _purge(db)
        cli_row = mm.MonzaCliente(nombre=f"{MARK} Cli")
        db.add(cli_row)
        db.flush()
        cot = mm.MonzaCotizacion(numero=f"{MARK}-COT", cliente_id=cli_row.id,
                                 estado="vendida", iva_pct=19)
        db.add(cot)
        db.flush()
        emb = mm.MonzaEmbarque(numero=f"{MARK}-EMB", estado="en_transito", forwarder="Fastmark")
        db.add(emb)
        db.flush()
        for parte, peso in (("FC-1", 2.0), ("FC-2", 5.0)):
            it = mm.MonzaCotizacionItem(cotizacion_id=cot.id, descripcion=f"Pieza {parte}",
                                        numero_parte=parte, cantidad=1, costo=100,
                                        moneda="USD", peso_kg=peso, estado_linea="en_transito")
            db.add(it)
            db.flush()
            db.add(mm.MonzaEmbarqueItem(embarque_id=emb.id, item_id=it.id))
        db.commit()
        print(f"[seed] embarque={emb.id}")
        return emb.id
    finally:
        db.close()


def _residuos():
    with engine.connect() as conn:
        n = 0
        for sql in (
            "SELECT COUNT(*) FROM monza_embarques WHERE numero LIKE :m",
            "SELECT COUNT(*) FROM monza_cotizaciones WHERE numero LIKE :m",
            "SELECT COUNT(*) FROM monza_cont_compra WHERE referencia LIKE :m",
            "SELECT COUNT(*) FROM monza_clientes WHERE nombre LIKE :m",
        ):
            n += int(conn.execute(text(sql), {"m": f"{MARK}%"}).scalar() or 0)
    return n


# ─── Lecturas con conexión NUEVA (nada de identity map) ───────────────────────
def _gastos_en_bd(emb_id: int) -> dict:
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT g.tipo, g.id, g.monto_neto, g.iva FROM monza_emb_pricing_gasto g "
            "JOIN monza_emb_pricing p ON p.id = g.pricing_id WHERE p.embarque_id = :e"),
            {"e": emb_id}).fetchall()
    return {r[0]: (int(r[1]), float(r[2] or 0), float(r[3] or 0)) for r in rows}


def _cxp_en_bd() -> list:
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, emb_pricing_gasto_id, monto_total_clp, numero_documento "
            "FROM monza_cont_compra WHERE referencia LIKE :m AND anulado = 0 ORDER BY id"),
            {"m": f"{MARK}%"}).fetchall()
    return [(int(r[0]), (int(r[1]) if r[1] is not None else None), float(r[2] or 0), r[3])
            for r in rows]


def _suma_cxp() -> float:
    return round(sum(x[2] for x in _cxp_en_bd()), 0)


def _overlay(tipo: str) -> dict:
    """La fila del overlay de Compras tal como la ve la PANTALLA ahora mismo.
    La pantalla decide con estos tres datos: compra_id != None → pill «En compras ✓» ·
    monto_total > 0 → botón · si no → sin monto
    (frontend-src/src/pages/MonzaComprasPage.tsx:853-865)."""
    r = cli.get("/api/monza/compras-contab/costos-embarque")
    if r.status_code != 200:
        return {}
    return next((x for x in (r.json().get("costos") or [])
                 if x.get("tipo") == tipo
                 and str(x.get("embarque_numero") or "").startswith(MARK)), {}) or {}


def _boton_visible(tipo: str) -> bool:
    """True si la pantalla muestra «Registrar como compra» en esa línea (el clic del bug)."""
    fila = _overlay(tipo)
    return bool(fila) and fila.get("compra_id") is None and _f(fila.get("monto_total")) > 0


def _payload(**lineas) -> dict:
    """El payload del PUT tal como lo manda el front: SIEMPRE las 6 líneas."""
    nro = lineas.pop("nro", {}) or {}
    tc = lineas.pop("tc", 962)
    orden = ["desconsolidacion", "almacenaje", "agencia", "arancel", "otros", "iva_importacion"]
    gastos = []
    for tipo in orden:
        neto, iva = lineas.get(tipo, (0.0, 0.0))
        gastos.append({"tipo": tipo, "monto_neto": neto, "iva": iva,
                       "nro_factura": nro.get(tipo), "banco": "Banco de Chile",
                       "fecha_factura": "2026-07-30"})
    return {"tc_tipo": "manual", "tc_valor": tc, "flete_en_me": False,
            "shipping_clp": 40_000, "gastos": gastos}


def _cxp(gasto_id, *, neto=NETO, iva=IVA, doc=None, rut=None, emb_id=None,
         tipo_doc="factura") -> dict:
    """Alta de la CxP del forwarder. `numero_documento` va VACÍO por defecto: es el caso
    REAL (las 6 líneas seed nacen sin N° de factura y el front lo prefill-ea tal cual)."""
    body = {
        "tipo_gasto": "cogs", "origen": "EMBARQUE" if gasto_id else "MANUAL",
        "categoria": "Aduana/agencia", "acreedor": FORWARDER,
        "referencia": f"{MARK}-CxP", "descripcion": "Gasto de importación del embarque",
        "numero_documento": doc, "tipo_doc": tipo_doc, "proveedor_rut": rut,
        "moneda": "CLP", "tc": 1, "monto_neto": neto, "iva": iva,
        "condicion_pago": "credito", "plazo_dias": 30,
    }
    if gasto_id:
        body["emb_pricing_gasto_id"] = gasto_id
    if emb_id:
        body["embarque_id"] = emb_id
    return body


def _plantar_divergencia(emb_id: int, tipo: str, neto: float, iva: float) -> None:
    """Escribe la línea del gasto POR FUERA de la API (UPDATE directo).
    Así quedó la BD antes del guard: la divergencia legada existe y hay que poder abrirla,
    verla y repararla. Sin este atajo el 409 del cierre no se podría ejercitar nunca."""
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE monza_emb_pricing_gasto g JOIN monza_emb_pricing p ON p.id = g.pricing_id "
            "SET g.monto_neto = :n, g.iva = :i "
            "WHERE p.embarque_id = :e AND g.tipo = :t"),
            {"n": neto, "i": iva, "e": emb_id, "t": tipo})


# ─── Checks ───────────────────────────────────────────────────────────────────
def run(emb_id: int):
    # ══ 0 · el pricing nace con sus 6 líneas y la factura del forwarder en 'agencia' ══
    r = cli.get(_url(emb_id))
    check("0 el detalle abre y siembra las 6 líneas de gastos",
          r.status_code == 200 and len(r.json().get("gastos", [])) == 6,
          (r.status_code, r.text[:150]))
    r = cli.put(_url(emb_id), json=_payload(agencia=(NETO, IVA)))
    check("0 guardar el pricing con la factura de la agencia → 200", r.status_code == 200,
          r.text[:250])
    g0 = _gastos_en_bd(emb_id)
    check("0 la línea 'agencia' quedó con el neto de la factura",
          g0.get("agencia", (0, 0, 0))[1] == NETO, g0)
    id_agencia, id_otros = g0["agencia"][0], g0["otros"][0]
    id_almacenaje = g0["almacenaje"][0]
    check("0 las 6 líneas nacen SIN N° de factura (el estado real del hallazgo)",
          all(x.get("nro_factura") in (None, "") for x in
              (cli.get("/api/monza/compras-contab/costos-embarque").json().get("costos") or [])
              if str(x.get("embarque_numero") or "").startswith(MARK)), "hay N° sembrado")

    # ══ 1 · H1: la CxP del forwarder + intentar MOVER el monto a otra línea ═══════
    r = cli.post("/api/monza/compras-contab", json=_cxp(id_agencia, emb_id=emb_id))
    compra_a = (r.json() or {}).get("id")
    check("1a la CxP del gasto de embarque se registra → 200 (sin N° de documento)",
          r.status_code == 200 and isinstance(compra_a, int), r.text[:250])
    check("1b Σ CxP ACTIVA = 190.400 (una sola factura)", _suma_cxp() == BRUTO, _cxp_en_bd())

    r = cli.put(_url(emb_id), json=_payload(otros=(NETO, IVA)))
    check("1c MOVER el monto de una línea YA registrada en CxP → 409 "
          "(antes: 200, y la línea vieja quedaba 'registrada' en $0)",
          r.status_code == 409, (r.status_code, r.text[:300]))
    check("1d el 409 nombra la compra que estorba (mensaje accionable)",
          f"#{compra_a}" in r.text and "Cuentas por Pagar" in r.text, r.text[:300])
    g1 = _gastos_en_bd(emb_id)
    check("1e el PUT no escribió NADA: 'agencia' conserva la plata y 'otros' sigue en 0",
          g1["agencia"][1] == NETO and g1["otros"][1] == 0.0,
          {"agencia": g1.get("agencia"), "otros": g1.get("otros")})
    check("1f la pantalla NO ofrece el botón en 'otros' (no hay 2° clic posible)",
          not _boton_visible("otros"), _overlay("otros"))
    check("1g 'agencia' sigue con la pill 'En compras ✓' sobre su monto REAL "
          "(no sobre un cero)",
          _overlay("agencia").get("compra_id") == compra_a
          and _f(_overlay("agencia").get("monto_total")) == BRUTO, _overlay("agencia"))
    check("1h Σ CxP sigue en 190.400 (la doble CxP NO nació)",
          _suma_cxp() == BRUTO and len(_cxp_en_bd()) == 1, _cxp_en_bd())

    # ══ 2 · anti sobre-bloqueo del guard de monto ═════════════════════════════════
    r = cli.put(_url(emb_id), json=_payload(agencia=(NETO, IVA), tc=970))
    check("2a re-guardar SIN cambiar la línea registrada (corregir el TC) → 200",
          r.status_code == 200, r.text[:200])
    r = cli.put(_url(emb_id), json=_payload(agencia=(NETO, IVA), almacenaje=(NETO2, IVA2)))
    check("2b editar OTRA línea (sin CxP) mientras 'agencia' está registrada → 200",
          r.status_code == 200, r.text[:200])
    check("2c y la línea nueva quedó guardada",
          _gastos_en_bd(emb_id)["almacenaje"][1] == NETO2, _gastos_en_bd(emb_id))

    # ══ 3 · H1 por la otra puerta: MISMO monto tecleado en otra línea, sin mover ══
    r = cli.put(_url(emb_id), json=_payload(agencia=(NETO, IVA), otros=(NETO, IVA)))
    check("3a teclear el MISMO monto en otra línea (sin tocar la registrada) → 200",
          r.status_code == 200, r.text[:200])
    check("3b la pantalla SÍ ofrece el botón en 'otros' (el clic existe de verdad)",
          _boton_visible("otros"), _overlay("otros"))
    r = cli.post("/api/monza/compras-contab", json=_cxp(id_otros, emb_id=emb_id))
    check("3c registrar la MISMA factura desde 'otros' → 409 por FACTURA FÍSICA "
          "(el freno por línea la dejaba pasar: 6 líneas = 6 llaves)",
          r.status_code == 409, (r.status_code, r.text[:300]))
    check("3d el 409 pide el N° de factura (intervención humana) y nombra la compra",
          "N° de factura" in r.text and f"#{compra_a}" in r.text, r.text[:300])
    check("3e Σ CxP ACTIVA = 190.400 — NO 380.800", _suma_cxp() == BRUTO, _cxp_en_bd())

    r = cli.post("/api/monza/compras-contab",
                 json=_cxp(id_almacenaje, neto=NETO2, iva=IVA2, emb_id=emb_id))
    compra_alm = (r.json() or {}).get("id")
    check("3f otra factura del MISMO acreedor por OTRO monto → 200 (no sobre-bloquea)",
          r.status_code == 200, (r.status_code, r.text[:250]))
    check("3g Σ CxP = 190.400 + 59.500", _suma_cxp() == BRUTO + BRUTO2, _cxp_en_bd())
    check("3h anular esa segunda compra → 200",
          cli.post(f"/api/monza/compras-contab/{compra_alm}/anular",
                   json={"motivo": f"{MARK} limpieza"}).status_code == 200)

    # ══ 4 · REGLA 1: mismo N° de documento en el mismo embarque, con RUT distinto ══
    check("4a anular la CxP sin N° para volver a registrarla con N° → 200",
          cli.post(f"/api/monza/compras-contab/{compra_a}/anular",
                   json={"motivo": f"{MARK} recarga con folio"}).status_code == 200)
    r = cli.post("/api/monza/compras-contab",
                 json=_cxp(id_agencia, doc="FW-9001", rut="76.111.111-1", emb_id=emb_id))
    compra_b = (r.json() or {}).get("id")
    check("4b registrar la factura CON N° de documento → 200", r.status_code == 200,
          r.text[:250])
    r = cli.post("/api/monza/compras-contab",
                 json=_cxp(id_otros, doc="FW-9001", rut="77.222.222-2", emb_id=emb_id))
    check("4c el MISMO N° de documento en el MISMO embarque, con OTRO RUT → 409 "
          "(el UNIQUE por (RUT, N°) no llega: los RUT distintos no chocan)",
          r.status_code == 409, (r.status_code, r.text[:300]))
    check("4d Σ CxP = 190.400 tras el intento", _suma_cxp() == BRUTO, _cxp_en_bd())
    r = cli.post("/api/monza/compras-contab",
                 json=_cxp(id_otros, doc="FW-9099", rut="77.222.222-2", neto=NETO2,
                           iva=IVA2, emb_id=emb_id))
    check("4e otro N° de documento en el mismo embarque → 200 (no sobre-bloquea)",
          r.status_code == 200, (r.status_code, r.text[:250]))
    check("4f anular esa compra de control → 200",
          cli.post(f"/api/monza/compras-contab/{(r.json() or {}).get('id')}/anular",
                   json={"motivo": f"{MARK} limpieza"}).status_code == 200)

    # ══ 5 · H3: la vía MANUAL (sin la llave del gasto ni el embarque) ═════════════
    r = cli.post("/api/monza/compras-contab", json=_cxp(None))
    check("5a la MISMA factura tecleada a mano (sin gasto ni embarque, sin N°) → 409 "
          "(es la puerta ORIGINAL del bug y no tenía ninguna red)",
          r.status_code == 409, (r.status_code, r.text[:300]))
    check("5b Σ CxP = 190.400 (la vía manual no duplicó)", _suma_cxp() == BRUTO, _cxp_en_bd())
    r = cli.post("/api/monza/compras-contab", json=_cxp(None, doc="FW-7777"))
    manual_ok = (r.json() or {}).get("id")
    check("5c la misma alta manual CON N° de documento → 200 "
          "(escribir el N° es la salida que el 409 pide, y funciona)",
          r.status_code == 200, (r.status_code, r.text[:250]))
    check("5d borrar la compra manual de control → 200",
          cli.delete(f"/api/monza/compras-contab/{manual_ok}").status_code == 200)
    r = cli.post("/api/monza/compras-contab", json=_cxp(None, neto=NETO2, iva=IVA2))
    check("5e alta manual sin N° por OTRO monto → 200 (no sobre-bloquea la caja diaria)",
          r.status_code == 200, (r.status_code, r.text[:250]))
    check("5f borrarla → 200",
          cli.delete(f"/api/monza/compras-contab/{(r.json() or {}).get('id')}").status_code == 200)
    r = cli.post("/api/monza/compras-contab", json=_cxp(None, tipo_doc="recibo"))
    check("5g un RECIBO por el mismo monto (caja chica) → 200 "
          "(la regla es solo para facturas: una factura chilena SIEMPRE tiene folio)",
          r.status_code == 200, (r.status_code, r.text[:250]))
    check("5h borrar el recibo de control → 200",
          cli.delete(f"/api/monza/compras-contab/{(r.json() or {}).get('id')}").status_code == 200)

    # ══ 6 · H2: divergencia de MONTO — bloqueo, aviso, cierre y reparación ════════
    r = cli.put(_url(emb_id), json=_payload(agencia=(400_000.0, 76_000.0),
                                           nro={"agencia": "FW-9001"}))
    check("6a subir el monto de la línea ya registrada en CxP → 409 "
          "(antes: 200 y divergencia silenciosa de 285.600)",
          r.status_code == 409, (r.status_code, r.text[:300]))
    check("6b el 409 muestra los dos montos y las salidas",
          "190.400" in r.text and "476.000" in r.text and "anúlela" in r.text, r.text[:400])

    _plantar_divergencia(emb_id, "agencia", 400_000.0, 76_000.0)
    r = cli.get(_url(emb_id))
    avisos = " ".join(r.json().get("advertencias") or [])
    check("6c el detalle AVISA la divergencia en pantalla (advertencias)",
          r.status_code == 200 and f"#{compra_b}" in avisos and "476.000" in avisos,
          (r.status_code, avisos[:300]))
    r = cli.post(f"{_url(emb_id)}/cerrar")
    check("6d CERRAR con la línea divergente → 409 "
          "(antes: 200 y el costo landed quedaba congelado en 476.000 contra un pasivo "
          "de 190.400, sin salida)",
          r.status_code == 409, (r.status_code, r.text[:300]))
    r = cli.put(_url(emb_id), json=_payload(agencia=(400_000.0, 76_000.0), tc=941,
                                            nro={"agencia": "FW-9001"}))
    check("6e con la divergencia legada el pricing SIGUE editable si no se toca esa línea "
          "(el guard no secuestra la pantalla) → 200", r.status_code == 200, r.text[:250])
    r = cli.put(_url(emb_id), json=_payload(agencia=(NETO, IVA), nro={"agencia": "FW-9001"}))
    check("6f CONVERGER la línea al monto de la CxP → 200 (camino de reparación)",
          r.status_code == 200, r.text[:250])
    check("6g y la línea quedó reparada en la BD",
          _gastos_en_bd(emb_id)["agencia"][1] == NETO, _gastos_en_bd(emb_id))
    r = cli.get(_url(emb_id))
    check("6h el aviso de divergencia desapareció",
          not any("NO cuadra" in a for a in (r.json().get("advertencias") or [])),
          r.json().get("advertencias"))
    r = cli.post(f"{_url(emb_id)}/cerrar")
    check("6i ahora el cierre pasa → 200", r.status_code == 200, r.text[:250])
    check("6j y congela el gasto CUADRADO con la CxP (160.000 capitalizable)",
          _f((r.json().get("totales_gastos") or {}).get("total_capitaliza")) == NETO,
          r.json().get("totales_gastos"))
    check("6k reabrir el pricing → 200",
          cli.post(f"{_url(emb_id)}/reabrir").status_code == 200)

    # ══ 7 · el portón: dos registros SIMULTÁNEOS de la misma factura, 2 líneas ════
    check("7a anular la CxP con folio para dejar las dos líneas libres → 200",
          cli.post(f"/api/monza/compras-contab/{compra_b}/anular",
                   json={"motivo": f"{MARK} carrera"}).status_code == 200)
    r = cli.put(_url(emb_id), json=_payload(agencia=(NETO, IVA), otros=(NETO, IVA)))
    check("7b dejar el MISMO monto en 'agencia' y 'otros' → 200", r.status_code == 200,
          r.text[:200])
    codigos: list = []

    def _reg(gid):
        codigos.append(cli.post("/api/monza/compras-contab",
                                json=_cxp(gid, emb_id=emb_id)).status_code)

    t1 = threading.Thread(target=_reg, args=(id_agencia,))
    t2 = threading.Thread(target=_reg, args=(id_otros,))
    t1.start(); t2.start(); t1.join(); t2.join()
    cxp = _cxp_en_bd()
    check("7c 2 registros SIMULTÁNEOS de la misma factura desde DOS líneas distintas → "
          "1 sola CxP activa (el lock del gasto no cubre este caso: son gastos distintos)",
          len(cxp) == 1 and _suma_cxp() == BRUTO, {"codigos": sorted(codigos), "cxp": cxp})
    check("7d y uno de los dos recibió el 409 (no un 500)",
          sorted(codigos) == [200, 409], sorted(codigos))

    # ══ 8 · la salida legítima sigue abierta de punta a punta ═════════════════════
    vivo = cxp[0][0] if cxp else None
    check("8a anular la CxP → 200",
          cli.post(f"/api/monza/compras-contab/{vivo}/anular",
                   json={"motivo": f"{MARK} reclasificar"}).status_code == 200)
    r = cli.put(_url(emb_id), json=_payload(otros=(NETO, IVA)))
    check("8b sin CxP activa, mover TODO el monto a 'otros' → 200 "
          "(reclasificar es legítimo: primero se revierte la CxP)",
          r.status_code == 200, r.text[:250])
    check("8c la línea 'agencia' quedó en 0 y 'otros' con la plata",
          _gastos_en_bd(emb_id)["agencia"][1] == 0.0
          and _gastos_en_bd(emb_id)["otros"][1] == NETO, _gastos_en_bd(emb_id))
    r = cli.post("/api/monza/compras-contab", json=_cxp(id_otros, emb_id=emb_id))
    check("8d y la factura se registra desde la línea nueva → 200",
          r.status_code == 200, (r.status_code, r.text[:250]))
    check("8e Σ CxP ACTIVA = 190.400: UNA factura, UNA CxP, en la línea correcta",
          _suma_cxp() == BRUTO and len(_cxp_en_bd()) == 1, _cxp_en_bd())
    r = cli.post(f"{_url(emb_id)}/cerrar")
    check("8f el pricing cierra con el costo cuadrado → 200", r.status_code == 200,
          r.text[:250])


def cleanup():
    db = SessionLocal()
    try:
        db.rollback()
        _purge(db)
    except Exception as e:                                   # noqa: BLE001
        db.rollback()
        print(f"⚠️  cleanup falló: {e}")
    finally:
        db.close()


def test_dedup_por_factura_fisica_monza():
    """Wrapper para pytest: llama a run() DIRECTAMENTE (el candado
    tests_infra/test_suites_visibles.py exige la llamada literal)."""
    emb_id = seed()
    try:
        run(emb_id)
    finally:
        cleanup()
    resto = _residuos()
    print(f"[cleanup] filas MARCADAS que sobreviven: {resto}")
    assert not _fails and resto == 0, f"fallas={_fails} residuos={resto}"


if __name__ == "__main__":
    _emb = seed()
    try:
        run(_emb)
    finally:
        cleanup()
    _resto = _residuos()
    print(f"[cleanup] filas MARCADAS que sobreviven: {_resto}")
    print()
    if _fails or _resto:
        print(f"RESULTADO: {len(_fails)} FALLO(S) -> {_fails} · residuos={_resto}")
        sys.exit(1)
    print("RESULTADO: TODO OK")
