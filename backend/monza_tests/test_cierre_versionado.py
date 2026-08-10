"""El cierre de venta queda VERSIONADO, y revertir deshace lo que el cierre sumó (MonzaParts).

LO QUE REPORTÓ EL DUEÑO
    «Una cotización que fue vendida me permitió revertirla a enviada. Cuando fue revertida, volví
    a poner datos para ponerle vendida, y los sobreescribió.»

    La reversión en sí está bien y ya estaba protegida: sólo se permite si la venta NO tiene plata
    ni logística colgando (0 facturas, 0 adelantos, 0 despachos vivos). Es la salida que corrige un
    cierre por error. Lo que faltaba era la MEMORIA: el re-cierre pisaba el N° de OC, la fecha de
    OC y la fecha prometida sin dejar rastro, así que en Ventas la venta aparecía como si siempre
    hubiera tenido esos datos y nadie podía saber que hubo un cierre anterior.

EL BUG DE PLATA QUE APARECIÓ BUSCANDO «TODOS LOS CASOS POSIBLES»
    `cliente.ltv` se suma al pasar a 'despachado' y NADIE lo restaba al volver atrás. El camino es
    angosto —despachar, anular todos los despachos, revertir, re-despachar— pero al recorrerlo el
    cliente terminaba con la venta contada DOS veces en su histórico. §5 lo reproduce.

SONDAS DE PODER DISCRIMINANTE
    · §3 re-cierra con una OC DISTINTA a propósito: si la versión anterior no se guardó, el
      historial muestra la OC nueva dos veces y el check se cae.
    · §5 mide el LTV por DELTA, nunca por valor absoluto.
    · §6 verifica que revertir desde 'vendida' NO toque el LTV: un arreglo que restara siempre
      dejaría el histórico del cliente en negativo.

Sin red. Datos con MARK propio y limpieza verificada al final.

Corre con:  ./venv/bin/python -m pytest monza_tests/test_cierre_versionado.py -q
"""
import os
import sys
import uuid
from datetime import date
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import SessionLocal  # noqa: E402
from auth import get_current_user  # noqa: E402
from monza_models import (  # noqa: E402
    MonzaCliente, MonzaCotizacion, MonzaCotizacionItem, MonzaCotizacionCierre,
    MonzaDespacho, MonzaLead, MonzaLog, MonzaNotificacion,
)
from monza_router_cotizaciones import router as cotizaciones_router  # noqa: E402

MARK = "test-mzver"
LEAD_MARK = "L-MZV"          # MonzaLead.numero es String(20): prefijo corto propio
EMAIL = f"{MARK}@test.invalid"
API = "/api/monza/cotizaciones"

CURRENT = {"id": 1, "empresa": "automotriz"}
app = FastAPI()
app.include_router(cotizaciones_router)
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=CURRENT["id"], email=EMAIL, empresa=CURRENT["empresa"])
client = TestClient(app)

_fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _seed(total_bruto=119000):
    """Venta ENVIADA (el estado desde el que se puede cerrar), con lead y cliente."""
    db = SessionLocal()
    try:
        cli = MonzaCliente(nombre=f"{MARK} SpA", ltv=0)
        db.add(cli)
        db.flush()
        lead = MonzaLead(numero=f"{LEAD_MARK}-{uuid.uuid4().hex[:6].upper()}",
                         cliente_id=cli.id, estado="en_proceso")
        db.add(lead)
        db.flush()
        cot = MonzaCotizacion(
            numero=f"CMV-{uuid.uuid4().hex[:8].upper()}", cliente_id=cli.id, lead_id=lead.id,
            estado="enviada", total_bruto=total_bruto, forma_pago="contado", pct_adelanto=0)
        db.add(cot)
        db.flush()
        db.add(MonzaCotizacionItem(cotizacion_id=cot.id, descripcion="Filtro", cantidad=1,
                                   precio_unitario_clp=100000, estado_linea="cotizado"))
        db.commit()
        return cot.id, cli.id
    finally:
        db.close()


def _cierres(cot_id):
    db = SessionLocal()
    try:
        return (db.query(MonzaCotizacionCierre)
                .filter(MonzaCotizacionCierre.cotizacion_id == cot_id)
                .order_by(MonzaCotizacionCierre.version.asc()).all())
    finally:
        db.close()


def _ltv(cli_id):
    db = SessionLocal()
    try:
        c = db.query(MonzaCliente).filter(MonzaCliente.id == cli_id).first()
        return float(c.ltv or 0) if c else 0.0
    finally:
        db.close()


def _cot(cot_id):
    db = SessionLocal()
    try:
        return db.query(MonzaCotizacion).filter(MonzaCotizacion.id == cot_id).first()
    finally:
        db.close()


def _forzar_despachado(cot_id):
    """Deja la venta en 'despachado' por la vía de datos: el endpoint exige cobertura real de
    despachos cerrados, y montarla acá sería reimplementar media Fase 2. Lo que esta suite
    prueba es la REVERSIÓN, no cómo se llega a despachado."""
    db = SessionLocal()
    try:
        cot = db.query(MonzaCotizacion).filter(MonzaCotizacion.id == cot_id).first()
        cot.estado = "despachado"
        db.commit()
    finally:
        db.close()


def _limpiar():
    db = SessionLocal()
    try:
        S = "fetch"
        cot_ids = [r[0] for r in db.query(MonzaCotizacion.id)
                   .join(MonzaCliente, MonzaCliente.id == MonzaCotizacion.cliente_id)
                   .filter(MonzaCliente.nombre.like(f"{MARK}%")).all()]
        db.query(MonzaCotizacionCierre).filter(
            MonzaCotizacionCierre.cotizacion_id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaCotizacionItem).filter(
            MonzaCotizacionItem.cotizacion_id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaDespacho).filter(
            MonzaDespacho.cotizacion_id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaNotificacion).filter(
            MonzaNotificacion.entidad == "cotizacion",
            MonzaNotificacion.entidad_id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaCotizacion).filter(
            MonzaCotizacion.id.in_(cot_ids or [0])).delete(synchronize_session=S)
        db.query(MonzaLead).filter(MonzaLead.numero.like(f"{LEAD_MARK}%")).delete(
            synchronize_session=S)
        db.query(MonzaLog).filter(MonzaLog.user_email == EMAIL).delete(synchronize_session=S)
        db.query(MonzaCliente).filter(MonzaCliente.nombre.like(f"{MARK}%")).delete(
            synchronize_session=S)
        db.commit()
    finally:
        db.close()


def _verificar_limpieza():
    """Sesión NUEVA (regla de la casa): una reutilizada serviría su propio snapshot."""
    db = SessionLocal()
    try:
        restos = (db.query(MonzaCliente).filter(MonzaCliente.nombre.like(f"{MARK}%")).count()
                  + db.query(MonzaLead).filter(MonzaLead.numero.like(f"{LEAD_MARK}%")).count())
        assert restos == 0, f"quedaron {restos} filas {MARK} tras la limpieza"
    finally:
        db.close()
    print("Cleanup OK (verificado con sesión nueva)")


def run():
    _limpiar()
    try:
        cot_id, cli_id = _seed()

        # ── 1) Primer cierre: queda la versión 1 con su foto ────────────────────────
        r = client.patch(f"{API}/{cot_id}", json={
            "estado": "vendida", "oc_cliente": "OC-PRIMERA", "oc_fecha": "2026-07-01",
            "fecha_entrega_est": "2026-07-20", "pct_adelanto": 50, "forma_pago": "credito",
        })
        check("1a el cierre responde 200", r.status_code == 200, r.text[:200])
        cs = _cierres(cot_id)
        check("1b quedó registrada UNA versión", len(cs) == 1, len(cs))
        check("1c con la OC del cierre", cs and cs[0].oc_cliente == "OC-PRIMERA",
              cs[0].oc_cliente if cs else None)
        check("1d y con quién la cerró", cs and cs[0].cerrado_por_email == EMAIL,
              cs[0].cerrado_por_email if cs else None)
        check("1e la versión arranca en 1", cs and cs[0].version == 1, cs[0].version if cs else None)
        check("1f y todavía NO está revertida", cs and cs[0].revertido_at is None,
              cs[0].revertido_at if cs else None)

        # ── 2) Reversión: la versión se MARCA, no se borra ──────────────────────────
        r = client.patch(f"{API}/{cot_id}", json={
            "estado": "enviada", "motivo_reversion": "el cliente cambió el N° de su OC"})
        check("2a la reversión responde 200 (no hay plata colgando)", r.status_code == 200,
              r.text[:200])
        cs = _cierres(cot_id)
        check("2b la versión 1 SIGUE existiendo (es auditoría, no se borra)", len(cs) == 1, len(cs))
        check("2c y quedó marcada como revertida", cs[0].revertido_at is not None)
        check("2d con quién la revirtió", cs[0].revertido_por_email == EMAIL,
              cs[0].revertido_por_email)
        check("2e a qué estado volvió", cs[0].revertido_a_estado == "enviada",
              cs[0].revertido_a_estado)
        check("2f y el motivo que escribió el operador",
              "cambió el N°" in (cs[0].motivo or ""), cs[0].motivo)
        check("2g el motivo NO se guardó como campo de la cotización",
              not hasattr(_cot(cot_id), "motivo_reversion") or
              getattr(_cot(cot_id), "motivo_reversion", None) is None)

        # ── 3) Re-cierre con OTROS datos: versión nueva, la vieja intacta ───────────
        r = client.patch(f"{API}/{cot_id}", json={
            "estado": "vendida", "oc_cliente": "OC-SEGUNDA", "oc_fecha": "2026-07-05",
            "fecha_entrega_est": "2026-07-25",
        })
        check("3a el re-cierre responde 200", r.status_code == 200, r.text[:200])
        cs = _cierres(cot_id)
        check("3b ahora hay DOS versiones", len(cs) == 2, len(cs))
        check("3c la versión 1 conserva la OC ORIGINAL (esto es lo que antes se perdía)",
              cs[0].oc_cliente == "OC-PRIMERA", cs[0].oc_cliente)
        check("3d la versión 2 tiene la OC nueva", cs[1].oc_cliente == "OC-SEGUNDA",
              cs[1].oc_cliente)
        check("3e y la versión 2 está vigente (sin revertir)", cs[1].revertido_at is None)
        check("3f la venta VIVA muestra los datos nuevos",
              (_cot(cot_id).oc_cliente or "") == "OC-SEGUNDA", _cot(cot_id).oc_cliente)

        # ── 4) El detalle sirve el historial para que la pantalla avise ─────────────
        r = client.get(f"{API}/{cot_id}")
        check("4a el detalle responde 200", r.status_code == 200, r.text[:200])
        d = r.json()
        check("4b viaja el historial completo", len(d.get("cierres", [])) == 2,
              len(d.get("cierres", [])))
        check("4c y el contador de veces cerrada", d.get("veces_cerrada") == 2,
              d.get("veces_cerrada"))
        check("4d el historial viene de la más nueva a la más vieja",
              d["cierres"][0]["version"] == 2, [x["version"] for x in d["cierres"]])
        check("4e la versión revertida trae su motivo, para poder mostrarlo",
              any("cambió el N°" in (x.get("motivo") or "") for x in d["cierres"]),
              [x.get("motivo") for x in d["cierres"]])

        # ── 5) EL BUG DE PLATA: revertir desde 'despachado' deshace el LTV ──────────
        cot2_id, cli2_id = _seed(total_bruto=500000)
        client.patch(f"{API}/{cot2_id}", json={"estado": "vendida", "oc_cliente": "OC-LTV"})
        _forzar_despachado(cot2_id)
        # Se simula lo que hizo el despacho: sumar el total al histórico del cliente.
        db = SessionLocal()
        try:
            c = db.query(MonzaCliente).filter(MonzaCliente.id == cli2_id).first()
            c.ltv = (c.ltv or 0) + 500000
            db.commit()
        finally:
            db.close()
        ltv_antes = _ltv(cli2_id)
        check("5a el despacho dejó el LTV en 500.000", ltv_antes == 500000.0, ltv_antes)

        r = client.patch(f"{API}/{cot2_id}", json={
            "estado": "enviada", "motivo_reversion": "se anuló el despacho"})
        check("5b revertir desde despachado responde 200", r.status_code == 200, r.text[:200])
        ltv_despues = _ltv(cli2_id)
        check("5c SONDA: el LTV volvió a 0 (delta -500.000). Sin el arreglo quedaba inflado y "
              "un re-cierre + re-despacho lo contaba dos veces",
              ltv_despues == 0.0, (ltv_antes, ltv_despues))

        # ── 6) Revertir desde 'vendida' NO toca el LTV (nunca se sumó) ──────────────
        cot3_id, cli3_id = _seed(total_bruto=300000)
        client.patch(f"{API}/{cot3_id}", json={"estado": "vendida", "oc_cliente": "OC-NOLTV"})
        ltv_v = _ltv(cli3_id)
        client.patch(f"{API}/{cot3_id}", json={"estado": "enviada"})
        check("6 revertir una venta NO despachada deja el LTV igual (restar siempre lo dejaría "
              "negativo)", _ltv(cli3_id) == ltv_v, (ltv_v, _ltv(cli3_id)))

        # ── 7) La reversión sin motivo se permite igual (no se bloquea la salida) ───
        cs3 = _cierres(cot3_id)
        check("7 se revirtió aunque no se escribió motivo",
              cs3 and cs3[-1].revertido_at is not None and cs3[-1].motivo is None,
              (cs3[-1].revertido_at, cs3[-1].motivo) if cs3 else None)

        # ── 8) El guard viejo sigue en pie: con plata colgando no se revierte ───────
        cot4_id, _cli4 = _seed()
        client.patch(f"{API}/{cot4_id}", json={"estado": "vendida", "oc_cliente": "OC-GUARD"})
        db = SessionLocal()
        try:
            db.add(MonzaDespacho(numero=f"DSP-{MARK}-{uuid.uuid4().hex[:5]}",
                                 cotizacion_id=cot4_id, estado="en_preparacion"))
            db.commit()
        finally:
            db.close()
        r = client.patch(f"{API}/{cot4_id}", json={"estado": "enviada"})
        check("8a con un despacho vivo la reversión se BLOQUEA (409)", r.status_code == 409,
              r.status_code)
        cs4 = _cierres(cot4_id)
        check("8b y la versión NO quedó marcada como revertida",
              cs4 and cs4[-1].revertido_at is None, cs4[-1].revertido_at if cs4 else None)

    finally:
        _limpiar()
    _verificar_limpieza()
    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_cierre_versionado_monza():
    run()


if __name__ == "__main__":
    run()
