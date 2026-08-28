"""FIRMA PARCIAL de la guía de despacho (Grupo AM / MachParts) — suite de contrato.

LO QUE ESTA SUITE PROTEGE (POST /despachos/{id}/firmar + gates de contabilidad):
  §A  DEUDA HISTÓRICA: el gate negativo de la firma que GA nunca tuvo — un despacho
      cerrado SIN firmar no es facturable (selector vacío, derivación 409, ítems
      explícitos 409). Inspiración de forma: monza test_guia_firmada_gate.py.
  §B  Firma completa (sin `items`) = byte-igual a hoy: mismas 4 llaves de respuesta,
      qty_firmada NULL en todas las líneas, sin motivo; GET del despacho con
      faltante_total 0 y facturado 0.
  §C  Firma PARCIAL 2 de 3: motivo obligatorio (5-300) ⇔ hay faltante; la factura
      DERIVADA trae 2; el faltante NO es facturable ni por derivación ni por ítems
      explícitos (exceso → rechazo del tope); el selector deja de ofrecer la guía
      con 2 firmadas y 2 facturadas; por_facturar NO miente (vendido 3, facturado 2,
      faltante declarado 1 → por_facturar 0 con faltante_declarado 1); notificación
      a contabilidad con el motivo; RE-FIRMA editable (subir 2→3 OK y por_facturar
      recupera la unidad; bajar bajo lo facturado → 409 nombrando la línea).
  §D  Guards del payload: Σ firmada 0 → 400 («una guía firmada donde no llegó nada
      no es una firma»); qty > despachada → 400; despacho_item ajeno → 404; id
      repetido → 400; motivo sin faltante → 400.
  §E  CARRERA: firmar vs firmar y firmar vs crear_factura (hilos con FOTO PLANA de
      ids, jamás objetos ORM), sin estados imposibles: al final SIEMPRE
      facturado(di) ≤ firmada efectiva(di).

MUTACIONES verificadas al construir la suite (quitar → rojo → restaurar → verde,
sha256 idéntico; evidencia en el reporte del encargo): el coalesce del gate de
facturación (_firmada_efectiva vuelto a qty_despachada pura → caen las sondas del
faltante facturable de §C) y el guard de re-firma bajo lo facturado (→ cae c11).

El guard de re-firma tiene DOS capas (ver firmar_despacho): la 1ª compara por
despacho_item_id; la 2ª, por ÍTEM FÍSICO contra TODO lo facturado de la venta —
esa es la que ve las líneas con despacho_item_id NULL (facturación por ítems
sueltos sin declarar guía, alcanzable hoy). §G la pinza creando exactamente ese
caso. Secciones: §A payload/guards · §B byte-igual histórico · §C parcial+gate
(motivo⇔faltante incluido) · §D selector · §E carreras · §F hallazgos de la
revisión adversarial (archivo conservado, regla cerrada del cupo, detalle==listado)
· §G la 2ª capa del guard.

Datos MARCADOS (sin guiones bajos: en LIKE el _ es comodín) y limpieza verificada
con SESIÓN NUEVA. Requiere la BD local (igual que las demás suites GA).

Corre con:  ./venv/bin/python -m pytest routers/tests/test_firma_parcial.py -q
(también:   ./venv/bin/python routers/tests/test_firma_parcial.py)
"""
import os
import sys
import threading
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import FastAPI, Depends  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from database import Base, engine, SessionLocal, get_db  # noqa: E402
from auth import get_current_user  # noqa: E402
from models.models import (  # noqa: E402
    Cotizacion, ItemCotizacion, OcCliente, Despacho, DespachoItem,
    ContFacturaCliente, ContFacturaClienteItem, ContCobranza, ContFactoring,
    Notificacion, User,
)
# PIN del módulo raíz `notificaciones` (mismo patrón que test_asignacion_parcial.py):
# con routers/ en sys.path, el ROUTER homónimo routers/notificaciones.py puede sombrear
# al módulo raíz que trae crear_notificacion (firmar_despacho lo usa). Importarlo acá,
# con backend/ al frente del path, deja la resolución correcta cacheada en sys.modules.
import notificaciones as _notif_raiz  # noqa: E402
assert hasattr(_notif_raiz, "crear_notificacion"), (
    f"el módulo notificaciones resolvió a {_notif_raiz.__file__} (sombra del router)")

import routers.contabilidad as cont  # noqa: E402
from routers.despachos import router as despachos_router  # noqa: E402

MARK = "ZZFIRMAPARC"  # SIN guiones bajos: en LIKE el _ es comodín
CURRENT = {"empresa": "mineria", "id": None}

Base.metadata.create_all(bind=engine, checkfirst=True)

app = FastAPI()
app.include_router(despachos_router, prefix="/api")
app.include_router(cont.router, prefix="/api")


# Auth REALISTA (patrón test_facturas_emision.py): una lectura en la MISMA sesión del
# request abre el read view de MySQL ANTES del primer with_for_update(), igual que en
# producción — condición necesaria para que las carreras sean visibles.
def _current_user_realista(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=CURRENT["id"], empresa=CURRENT["empresa"])


app.dependency_overrides[get_current_user] = _current_user_realista
client = TestClient(app)

# Precios deterministas: el pricing engine tiene sus propios tests; aquí se fuerza
# precio_venta_clp/total_venta_clp para poder afirmar montos exactos de por_facturar.
PRECIOS = {}
_orig_precios = cont._precios_de_cotizacion


def _fake_precios(db, cot_id, cfg_dict, items_db=None):
    items = items_db if items_db is not None else (
        db.query(ItemCotizacion).filter(ItemCotizacion.cotizacion_id == cot_id).all())
    pmap = {}
    neto = 0.0
    for i in items:
        p = PRECIOS.get(i.id, 0.0)
        tot = cont._total_linea(p, float(i.cantidad or 0))
        pmap[i.id] = {"id": i.id, "precio_venta_clp": p, "total_venta_clp": tot}
        neto += tot
    totales = {"subtotal_neto_clp": neto, "iva_clp": cont._iva_clp(neto),
               "total_con_iva_clp": neto + cont._iva_clp(neto)}
    return items, pmap, totales


_fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


# ── Fábricas de datos MARCADOS ────────────────────────────────────────────────────

def _venta(db, sufijo, qty=3.0, precio=100000.0):
    """Cotización + OC + 1 ítem + despacho CERRADO (sin firmar) con toda la qty."""
    cot = Cotizacion(numero=f"{MARK}-{sufijo}", cliente=f"{MARK} Cliente",
                     rut_cliente="78.279.030-7")
    db.add(cot); db.flush()
    it = ItemCotizacion(cotizacion_id=cot.id, item_num=1,
                        numero_parte=f"{MARK}-P{sufijo}",
                        descripcion=f"{MARK} pieza {sufijo}", cantidad=qty,
                        estado_item="despachado")
    db.add(it); db.flush()
    oc = OcCliente(cotizacion_id=cot.id, numero_oc=f"{MARK}-OC-{sufijo}",
                   fecha_oc="2026-08-01")
    db.add(oc); db.flush()
    d = Despacho(numero_despacho=f"{MARK}-DSP-{sufijo}", oc_cliente_id=oc.id,
                 estado="despachado", numero_guia=f"{MARK}-G-{sufijo}")
    db.add(d); db.flush()
    di = DespachoItem(despacho_id=d.id, item_cotizacion_id=it.id, qty_despachada=qty)
    db.add(di)
    db.commit()
    for obj in (cot, it, oc, d, di):
        db.refresh(obj)
    PRECIOS[it.id] = precio
    return SimpleNamespace(cot_id=cot.id, item_id=it.id, oc_id=oc.id,
                           desp_id=d.id, di_id=di.id, qty=qty, precio=precio)


def _firmar(desp_id, archivo, items=None, motivo=None):
    body = {"archivo": archivo}
    if items is not None:
        body["items"] = items
    if motivo is not None:
        body["motivo_faltante"] = motivo
    return client.post(f"/api/despachos/{desp_id}/firmar", json=body)


def _despacho_fresco(desp_id):
    """Relee despacho + líneas con SESIÓN NUEVA (nada de identity map viejo)."""
    db = SessionLocal()
    try:
        d = db.query(Despacho).filter(Despacho.id == desp_id).first()
        items = list(d.items) if d else []
        return d, items
    finally:
        db.close()


def _facturado_por_di(oc_id):
    db = SessionLocal()
    try:
        return cont._qty_facturada_por_despacho_item(db, oc_id)
    finally:
        db.close()


def _en_paralelo(fn, n=2):
    barrera = threading.Barrier(n)
    salida = [None] * n

    def _worker(i):
        barrera.wait()
        try:
            salida[i] = fn(i)
        except Exception as e:  # noqa: BLE001
            salida[i] = ("EXC", repr(e))

    hilos = [threading.Thread(target=_worker, args=(i,)) for i in range(n)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    return salida


# ── Limpieza ─────────────────────────────────────────────────────────────────────

def _limpiar():
    db = SessionLocal()
    try:
        db.rollback()
        cot_ids = [c.id for c in db.query(Cotizacion)
                   .filter(Cotizacion.numero.like(f"{MARK}%")).all()]
        oc_ids = [o.id for o in db.query(OcCliente)
                  .filter(OcCliente.cotizacion_id.in_(cot_ids or [0])).all()]
        if oc_ids:
            fac_ids = [f.id for f in db.query(ContFacturaCliente)
                       .filter(ContFacturaCliente.oc_cliente_id.in_(oc_ids)).all()]
            desp_ids = [d.id for d in db.query(Despacho)
                        .filter(Despacho.oc_cliente_id.in_(oc_ids)).all()]
            if fac_ids:
                db.query(ContCobranza).filter(
                    ContCobranza.factura_id.in_(fac_ids)).delete(synchronize_session=False)
                db.query(ContFactoring).filter(
                    ContFactoring.factura_id.in_(fac_ids)).delete(synchronize_session=False)
                db.query(ContFacturaClienteItem).filter(
                    ContFacturaClienteItem.factura_id.in_(fac_ids)).delete(synchronize_session=False)
                db.query(ContFacturaCliente).filter(
                    ContFacturaCliente.id.in_(fac_ids)).delete(synchronize_session=False)
            if desp_ids:
                db.query(Notificacion).filter(
                    Notificacion.entidad_tipo == "despacho",
                    Notificacion.entidad_id.in_(desp_ids)).delete(synchronize_session=False)
                db.query(DespachoItem).filter(
                    DespachoItem.despacho_id.in_(desp_ids)).delete(synchronize_session=False)
                db.query(Despacho).filter(
                    Despacho.id.in_(desp_ids)).delete(synchronize_session=False)
            db.query(OcCliente).filter(OcCliente.id.in_(oc_ids)).delete(synchronize_session=False)
        if cot_ids:
            db.query(ItemCotizacion).filter(
                ItemCotizacion.cotizacion_id.in_(cot_ids)).delete(synchronize_session=False)
            db.query(Cotizacion).filter(Cotizacion.id.in_(cot_ids)).delete(synchronize_session=False)
        db.query(User).filter(User.email.like(f"{MARK}%")).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _verificar_limpieza():
    db = SessionLocal()
    try:
        restos = (
            db.query(Cotizacion).filter(Cotizacion.numero.like(f"{MARK}%")).count()
            + db.query(Despacho).filter(Despacho.numero_despacho.like(f"{MARK}%")).count()
            + db.query(ContFacturaCliente)
              .filter(ContFacturaCliente.numero_factura.like(f"{MARK}%")).count()
        )
        assert restos == 0, f"limpieza incompleta: quedan {restos} filas {MARK}"
        print("Limpieza verificada con sesión nueva: 0 restos")
    finally:
        db.close()


# ── Suite ────────────────────────────────────────────────────────────────────────

def run():
    cont._precios_de_cotizacion = _fake_precios
    _limpiar()
    # firmar exige el archivo de la guía firmada en uploads/docs: crear uno dummy
    docs_dir = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "..", "uploads", "docs"))
    os.makedirs(docs_dir, exist_ok=True)
    archivo = f"{MARK}-guia.pdf"
    with open(os.path.join(docs_dir, archivo), "wb") as fh:
        fh.write(b"%PDF-1.4 dummy")
    db = SessionLocal()
    try:
        CURRENT["empresa"] = "mineria"
        u = User(email=f"{MARK}@test.cl", nombre=f"{MARK} user",
                 hashed_password="x", empresa="mineria")
        db.add(u); db.commit(); db.refresh(u)
        CURRENT["id"] = u.id

        # ── §A · DEUDA HISTÓRICA: sin firmar → NO facturable ─────────────────────
        A = _venta(db, "A", qty=3.0)
        r = client.get(f"/api/contabilidad/ventas/{A.oc_id}/despachos-facturables")
        check("A1 selector: despacho cerrado SIN firmar no se ofrece",
              r.status_code == 200 and r.json() == [], r.text[:200])
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": A.oc_id, "despacho_id": A.desp_id,
                              "numero_factura": f"{MARK}-A1"})
        check("A2 derivación de guía sin firmar → 409 'FIRMADA'",
              r.status_code == 409 and "FIRMADA" in r.text, (r.status_code, r.text[:200]))
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": A.oc_id, "numero_factura": f"{MARK}-A2",
                              "items": [{"item_cotizacion_id": A.item_id, "cantidad": 1}]})
        check("A3 ítems explícitos sin guía firmada → 409 'no ha sido despachado'",
              r.status_code == 409 and "no ha sido despachado" in r.text,
              (r.status_code, r.text[:200]))

        # ── §B · Firma COMPLETA (sin items) = byte-igual a hoy ───────────────────
        B = _venta(db, "B", qty=2.0)
        r = _firmar(B.desp_id, archivo)
        check("B1 firma completa → 200", r.status_code == 200, r.text[:200])
        check("B2 respuesta byte-igual (las 4 llaves históricas, ni una más)",
              set(r.json().keys()) == {"ok", "guia_firmada", "numero_guia",
                                       "guia_firmada_archivo"}, r.json())
        d_b, items_b = _despacho_fresco(B.desp_id)
        check("B3 sesión nueva: qty_firmada NULL (completo) y sin motivo",
              d_b.guia_firmada == 1 and d_b.faltante_motivo is None
              and all(di.qty_firmada is None for di in items_b),
              (d_b.faltante_motivo, [di.qty_firmada for di in items_b]))
        r = client.get(f"/api/despachos/{B.desp_id}")
        j = r.json()
        check("B4 GET despacho: faltante_total 0, item qty_firmada null y facturado 0",
              r.status_code == 200 and j["faltante_total"] == 0
              and j["faltante_motivo"] is None
              and j["items"][0]["qty_firmada"] is None
              and j["items"][0]["facturado"] == 0, j)

        # ── §C · Firma PARCIAL 2 de 3 + facturación + por_facturar + re-firma ───
        C = _venta(db, "C", qty=3.0, precio=100000.0)
        parcial = [{"despacho_item_id": C.di_id, "qty_firmada": 2}]
        r = _firmar(C.desp_id, archivo, items=parcial)
        check("c1 faltante sin motivo → 400", r.status_code == 400
              and "motivo" in r.text.lower(), (r.status_code, r.text[:200]))
        r = _firmar(C.desp_id, archivo, items=parcial, motivo="cort")
        check("c2a motivo de 4 chars → 400", r.status_code == 400, r.text[:200])
        r = _firmar(C.desp_id, archivo, items=parcial, motivo="x" * 301)
        check("c2b motivo de 301 chars → 400", r.status_code == 400, r.text[:200])
        d_c, _ = _despacho_fresco(C.desp_id)
        check("c2c y la guía sigue SIN firmar (los rechazos no dejan mitades)",
              d_c.guia_firmada in (0, None), d_c.guia_firmada)

        motivo = "caja perdida por el courier en la entrega"
        r = _firmar(C.desp_id, archivo, items=parcial, motivo=motivo)
        check("c3 firma parcial 2 de 3 → 200 con faltante_total 1",
              r.status_code == 200 and r.json().get("faltante_total") == 1
              and r.json().get("faltante_motivo") == motivo, r.text[:300])
        d_c, items_c = _despacho_fresco(C.desp_id)
        check("c3b sesión nueva: qty_firmada 2.0 y motivo en cabecera",
              d_c.guia_firmada == 1 and items_c[0].qty_firmada == 2.0
              and d_c.faltante_motivo == motivo,
              (items_c[0].qty_firmada, d_c.faltante_motivo))
        db_n = SessionLocal()
        try:
            notif = (db_n.query(Notificacion)
                     .filter(Notificacion.entidad_tipo == "despacho",
                             Notificacion.entidad_id == C.desp_id).first())
        finally:
            db_n.close()
        check("c4 notificación a contabilidad: «faltante de 1 unidad(es) — motivo»",
              notif is not None and notif.destinatario_rol == "contabilidad"
              and "faltante de 1" in (notif.mensaje or "")
              and motivo in (notif.mensaje or ""),
              (getattr(notif, "destinatario_rol", None), getattr(notif, "mensaje", None)))
        r = _firmar(C.desp_id, archivo,
                    items=[{"despacho_item_id": C.di_id, "qty_firmada": 3}],
                    motivo="un motivo sin faltante que no corresponde")
        check("c5 motivo SIN faltante → 400 (el ⇔ va en las dos direcciones)",
              r.status_code == 400 and "No hay faltante" in r.text,
              (r.status_code, r.text[:200]))

        # GET del despacho: la vía nueva del contrato (qty_firmada + faltante)
        r = client.get(f"/api/despachos/{C.desp_id}")
        j = r.json()
        check("c5b GET despacho parcial: qty_firmada 2, faltante_total 1, motivo",
              j["items"][0]["qty_firmada"] == 2.0 and j["faltante_total"] == 1
              and j["faltante_motivo"] == motivo, j)

        # Facturación: la derivada trae LO FIRMADO (2), jamás lo despachado (3)
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": C.oc_id, "despacho_id": C.desp_id,
                              "numero_factura": f"{MARK}-C1", "plazo_dias": 30})
        check("c6 factura derivada → 200 con UNA línea de cantidad 2 (no 3)",
              r.status_code == 200 and len(r.json()["items"]) == 1
              and float(r.json()["items"][0]["cantidad"]) == 2.0, r.text[:300])
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": C.oc_id, "numero_factura": f"{MARK}-C2",
                              "items": [{"item_cotizacion_id": C.item_id,
                                         "despacho_item_id": C.di_id, "cantidad": 1}]})
        check("c7a el faltante NO se factura por ítems explícitos (exceso → 409 tope)",
              r.status_code == 409 and "excede" in r.text, (r.status_code, r.text[:200]))
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": C.oc_id, "despacho_id": C.desp_id,
                              "numero_factura": f"{MARK}-C3"})
        check("c7b ni por una segunda derivación (guía ya facturada por completo)",
              r.status_code == 409 and "facturado por completo" in r.text,
              (r.status_code, r.text[:200]))
        r = client.get(f"/api/contabilidad/ventas/{C.oc_id}/despachos-facturables")
        check("c8 selector: guía con 2 firmadas y 2 facturadas DESAPARECE",
              r.status_code == 200 and r.json() == [], r.text[:200])

        # por_facturar honesto: vendido 3, facturado 2, faltante declarado 1 → 0
        r = client.get(f"/api/contabilidad/ventas/{C.oc_id}")
        j = r.json()
        res = j["resumen"]
        check("c9 por_facturar 0 con faltante_declarado 1 (el número no miente)",
              r.status_code == 200 and res.get("por_facturar_clp") == 0
              and res.get("faltante_declarado") == 1
              and res.get("mercaderia_pendiente_clp") == 0, res)
        guia_item = j["items"][0]["guias"][0]
        check("c9b guias_por_item trae qty_firmada 2 (contrato de la pantalla)",
              guia_item.get("qty_firmada") == 2.0 and guia_item["qty_despachada"] == 3.0,
              guia_item)

        # RE-FIRMA: subir 2→3 (el courier la encontró) recupera la unidad
        r = _firmar(C.desp_id, archivo,
                    items=[{"despacho_item_id": C.di_id, "qty_firmada": 3}])
        check("c10 re-firma 2→3 → 200 (sin motivo: ya no hay faltante)",
              r.status_code == 200 and r.json().get("faltante_total") == 0, r.text[:200])
        d_c, items_c = _despacho_fresco(C.desp_id)
        check("c10b sesión nueva: qty_firmada vuelve a NULL (completo) y motivo limpio",
              items_c[0].qty_firmada is None and d_c.faltante_motivo is None,
              (items_c[0].qty_firmada, d_c.faltante_motivo))
        r = client.get(f"/api/contabilidad/ventas/{C.oc_id}")
        res = r.json()["resumen"]
        bruto_unidad = round(100000 * 1.19)  # 1 unidad × $100.000 + IVA
        check("c10c por_facturar recupera la unidad (≈ $119.000, faltante 0)",
              res.get("faltante_declarado") == 0
              and abs(res.get("por_facturar_clp", 0) - bruto_unidad) <= 2, res)
        r = _firmar(C.desp_id, archivo,
                    items=[{"despacho_item_id": C.di_id, "qty_firmada": 1}],
                    motivo="intento de bajar bajo lo ya facturado")
        check("c11 re-firma bajo lo facturado (1 < 2) → 409 nombrando la línea",
              r.status_code == 409 and f"{MARK}-PC" in r.text,
              (r.status_code, r.text[:250]))
        _, items_c = _despacho_fresco(C.desp_id)
        check("c11b y la firma no se movió (sigue completa)",
              items_c[0].qty_firmada is None, items_c[0].qty_firmada)

        # ── §D · Guards del payload ──────────────────────────────────────────────
        D = _venta(db, "D", qty=3.0)
        r = _firmar(D.desp_id, archivo,
                    items=[{"despacho_item_id": D.di_id, "qty_firmada": 0}],
                    motivo="no llegó absolutamente nada al cliente")
        check("d1 Σ firmada 0 → 400 («no es una firma»)",
              r.status_code == 400 and "no es una firma" in r.text,
              (r.status_code, r.text[:200]))
        r = _firmar(D.desp_id, archivo,
                    items=[{"despacho_item_id": D.di_id, "qty_firmada": 4}])
        check("d2 qty_firmada > despachada → 400",
              r.status_code == 400 and "entre 0 y lo" in r.text,
              (r.status_code, r.text[:200]))
        r = _firmar(D.desp_id, archivo,
                    items=[{"despacho_item_id": B.di_id, "qty_firmada": 1}])
        check("d3 despacho_item de OTRA guía → 404 explícito",
              r.status_code == 404 and "no pertenece" in r.text,
              (r.status_code, r.text[:200]))
        r = _firmar(D.desp_id, archivo,
                    items=[{"despacho_item_id": D.di_id, "qty_firmada": 1},
                           {"despacho_item_id": D.di_id, "qty_firmada": 3}])
        check("d4 id repetido en el payload → 400",
              r.status_code == 400 and "repetido" in r.text,
              (r.status_code, r.text[:200]))
        d_d, items_d = _despacho_fresco(D.desp_id)
        check("d5 tras los 4 rechazos la guía sigue sin firmar y sin restos",
              d_d.guia_firmada in (0, None) and items_d[0].qty_firmada is None
              and d_d.faltante_motivo is None,
              (d_d.guia_firmada, items_d[0].qty_firmada))

        # ── §E · CARRERAS (FOTO PLANA de ids: jamás objetos ORM en hilos) ────────
        # E1: firmar vs firmar (2 vs 3 de 3) — serializadas por el lock de la OC.
        E1 = _venta(db, "E1", qty=3.0)
        e1_desp_id = int(E1.desp_id)
        e1_di_id = int(E1.di_id)

        def _tiro_firma(i):
            qty = 2 if i == 0 else 3
            body = {"archivo": archivo,
                    "items": [{"despacho_item_id": e1_di_id, "qty_firmada": qty}]}
            if qty < 3:
                body["motivo_faltante"] = "carrera: una caja quedó en el courier"
            resp = client.post(f"/api/despachos/{e1_desp_id}/firmar", json=body)
            return resp.status_code

        salida = _en_paralelo(_tiro_firma, n=2)
        d_e1, items_e1 = _despacho_fresco(e1_desp_id)
        qf_e1 = items_e1[0].qty_firmada
        coherente = (qf_e1 == 2.0 and d_e1.faltante_motivo) or (
            qf_e1 is None and d_e1.faltante_motivo is None)
        check(f"e1 firmar vs firmar sin 500 y estado final coherente: {salida}",
              all(s in (200, 409) for s in salida) and d_e1.guia_firmada == 1
              and coherente, (salida, qf_e1, d_e1.faltante_motivo))

        # E2: firmar (re-firma a la baja) vs crear_factura — el invariante es que
        # JAMÁS quede facturado(di) > firmada efectiva(di).
        E2 = _venta(db, "E2", qty=3.0)
        r = _firmar(E2.desp_id, archivo)
        check("e2-setup guía E2 firmada completa", r.status_code == 200, r.text[:200])
        e2_desp_id = int(E2.desp_id)
        e2_di_id = int(E2.di_id)
        e2_oc_id = int(E2.oc_id)

        def _tiro_mixto(i):
            if i == 0:
                resp = client.post(
                    f"/api/despachos/{e2_desp_id}/firmar",
                    json={"archivo": archivo,
                          "items": [{"despacho_item_id": e2_di_id, "qty_firmada": 1}],
                          "motivo_faltante": "carrera: solo llegó una unidad"})
            else:
                resp = client.post(
                    "/api/contabilidad/facturas",
                    json={"oc_cliente_id": e2_oc_id, "despacho_id": e2_desp_id,
                          "numero_factura": f"{MARK}-E2", "plazo_dias": 30})
            return resp.status_code

        salida = _en_paralelo(_tiro_mixto, n=2)
        _, items_e2 = _despacho_fresco(e2_desp_id)
        qd_e2 = float(items_e2[0].qty_despachada or 0)
        qf_e2 = items_e2[0].qty_firmada
        efectiva = qd_e2 if qf_e2 is None else float(qf_e2)
        fact_di = _facturado_por_di(e2_oc_id)
        facturado = fact_di.get(e2_di_id, 0.0)
        check(f"e2 firmar vs crear_factura sin 500: {salida}",
              all(s in (200, 409) for s in salida), salida)
        check("e2b invariante: facturado(di) ≤ firmada efectiva(di) — sin estados imposibles",
              facturado <= efectiva + 0.001, (facturado, efectiva, qf_e2, salida))

        # ══ §F · Los agujeros de la revisión adversarial, pinzados ═══════════════
        # F1 (H1): la RE-FIRMA sin archivo nuevo CONSERVA el actual — el flujo
        # «Editar firma» (courier encontró la caja / corregir motivo) no obliga a
        # re-subir la misma foto. La PRIMERA firma sin archivo sigue en 400.
        vF = _venta(db, "F1")
        r = _firmar(vF.desp_id, None)
        check("f1a PRIMERA firma sin archivo → 400 (el respaldo es obligatorio)",
              r.status_code == 400, (r.status_code, r.text[:120]))
        r = _firmar(vF.desp_id, archivo,
                    items=[{"despacho_item_id": vF.di_id, "qty_firmada": 2}],
                    motivo=f"{MARK} 1 unidad perdida por el transportista")
        check("f1b firma parcial 2 de 3 con archivo → 200", r.status_code == 200,
              r.text[:200])
        r = _firmar(vF.desp_id, None,
                    items=[{"despacho_item_id": vF.di_id, "qty_firmada": 3}])
        dF, _ = _despacho_fresco(vF.desp_id)
        check("f1c RE-firma 2→3 SIN archivo → 200 y el archivo se CONSERVA",
              r.status_code == 200 and dF.guia_firmada_archivo == archivo,
              (r.status_code, dF.guia_firmada_archivo, r.text[:150]))

        # F2 (M3a): la firma parcial NO libera cupo ni toca estados — la regla
        # cerrada del dueño (la reposición va por cotización nueva). Declarar
        # faltante 1 NO habilita un segundo despacho por esa unidad, y la línea
        # sigue 'despachado'.
        vC = _venta(db, "F2")
        r = _firmar(vC.desp_id, archivo,
                    items=[{"despacho_item_id": vC.di_id, "qty_firmada": 2}],
                    motivo=f"{MARK} perdida en la entrega, se repone aparte")
        check("f2a setup: firma parcial 2 de 3 → 200", r.status_code == 200,
              r.text[:150])
        db2 = SessionLocal()
        try:
            est = (db2.query(ItemCotizacion.estado_item)
                   .filter(ItemCotizacion.id == vC.item_id).scalar())
        finally:
            db2.close()
        check("f2b estado_item sigue 'despachado' (la firma parcial no toca estados)",
              est == "despachado", est)
        r = client.post("/api/despachos", json={
            "oc_cliente_id": vC.oc_id,
            "items": [{"item_cotizacion_id": vC.item_id, "qty": 1}]})
        check("f2c SONDA de la regla cerrada: un 2º despacho por la unidad faltante "
              "sigue RECHAZADO (cero cupo liberado, cero fantasmas)",
              r.status_code != 200, (r.status_code, r.text[:180]))

        # F3 (M3b): el LISTADO de ventas dice el mismo faltante que el DETALLE
        # (la query batcheada del listado y _faltante_declarado_por_item son dos
        # implementaciones — esta sonda las mantiene iguales).
        det = client.get(f"/api/contabilidad/ventas/{vC.oc_id}").json()
        lst = client.get("/api/contabilidad/ventas").json()
        fila = next((x for x in (lst if isinstance(lst, list) else lst.get("ventas", []))
                     if x.get("oc_cliente_id") == vC.oc_id or x.get("id") == vC.oc_id), None)
        det_falt = (det.get("resumen") or {}).get("faltante_declarado")
        lst_falt = (fila or {}).get("faltante_declarado",
                                    (fila or {}).get("resumen", {}).get("faltante_declarado")
                                    if fila else None)
        check("f3 detalle y listado declaran el MISMO faltante (1)",
              det_falt == 1 and (lst_falt == 1 or lst_falt is None and fila is None),
              (det_falt, lst_falt, bool(fila)))

        # ══ §G · La 2ª CAPA del guard de re-firma (líneas de factura SIN guía) ═══
        # crear_factura con items[] y SIN despacho_id persiste líneas con
        # despacho_item_id NULL — invisibles para la 1ª capa (que compara por
        # despacho_item_id). Sin la 2ª capa, una re-firma a la baja podía declarar
        # «faltante» sobre unidades YA facturadas (facturado > firmado, el estado
        # imposible). Esta sección crea EXACTAMENTE ese caso.
        vG = _venta(db, "G1")
        r = _firmar(vG.desp_id, archivo)
        check("g1 setup: guía firmada COMPLETA (3)", r.status_code == 200,
              r.text[:150])
        r = client.post("/api/contabilidad/facturas",
                        json={"oc_cliente_id": vG.oc_id,
                              "numero_factura": f"{MARK}-G-F1",
                              "items": [{"item_cotizacion_id": vG.item_id,
                                         "cantidad": 3}],
                              "plazo_dias": 30})
        check("g2 setup: factura por ítems SUELTOS (sin despacho_id) → 200",
              r.status_code == 200, r.text[:250])
        db2 = SessionLocal()
        try:
            nulls = (db2.query(ContFacturaClienteItem)
                     .join(ContFacturaCliente,
                           ContFacturaClienteItem.factura_id == ContFacturaCliente.id)
                     .filter(ContFacturaCliente.oc_cliente_id == vG.oc_id,
                             ContFacturaClienteItem.despacho_item_id.is_(None),
                             ContFacturaClienteItem.item_cotizacion_id.isnot(None))
                     .count())
        finally:
            db2.close()
        check("g3 la línea quedó de verdad con despacho_item_id NULL (el caso que "
              "la 1ª capa no ve)", nulls >= 1, nulls)
        r = _firmar(vG.desp_id, None,
                    items=[{"despacho_item_id": vG.di_id, "qty_firmada": 1}],
                    motivo=f"{MARK} intento de faltante sobre lo ya facturado")
        check("g4 ★ SONDA 2ª CAPA: re-firma 3→1 con 3 facturadas por líneas NULL → "
              "409 (sin la capa, quedaba el estado imposible facturado>firmado)",
              r.status_code == 409 and "facturado" in r.text,
              (r.status_code, r.text[:200]))
        check("g5 …y el 409 NOMBRA la parte (multi-ítem: el operador sabe qué "
              "factura corregir)", f"{MARK}-PG1" in r.text, r.text[:200])
        _, items_g = _despacho_fresco(vG.desp_id)
        check("g6 …y la firma quedó INTACTA (completa, sin faltante fantasma)",
              items_g[0].qty_firmada is None, items_g[0].qty_firmada)

    finally:
        try:
            os.remove(os.path.join(docs_dir, archivo))
        except OSError:
            pass
        db.close()
        _limpiar()
        cont._precios_de_cotizacion = _orig_precios
        print("Cleanup OK")
    _verificar_limpieza()
    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_firma_parcial():
    run()


if __name__ == "__main__":
    run()
