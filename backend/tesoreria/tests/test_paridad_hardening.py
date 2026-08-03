"""Endurecimiento de paridad de Tesorería (Grupo AM) — hallazgos T9, T10 y T14.

Sondas de los tres arreglos que NO son el deadlock (ese vive en test_retry_deadlock.py):

  · T9  UNIQUE(egreso_id) en conc_conciliacion. Hasta ahora la conciliación 1:1
        cargo↔egreso la protegía SOLO el lock del router: cualquier camino nuevo —o una
        reparación hecha en SQL— podía enlazar el MISMO pago contra dos cargos del banco.
        Se prueba en la BD de verdad: dos enlaces al mismo egreso → IntegrityError.
  · T10 max_length en los schemas, alineado a la columna. Sin él un texto más largo que
        la columna llega hasta el INSERT: MySQL laxo lo TRUNCA en silencio y MySQL
        estricto responde 500 donde corresponde un 422.
  · T14 /aprobaciones paginado. Antes truncaba los aprobados a los 50 más nuevos sin
        decirlo: un adelanto antiguo aún sin conciliar quedaba INVISIBLE justo en la
        pantalla que debe cuadrarlo con el banco.

Datos MARCADOS + limpieza + verificación por DELTAS: no toca ni una fila real.
Corre con:  cd backend && ./venv/bin/python -m pytest tesoreria/tests/test_paridad_hardening.py -q
(también:   ./venv/bin/python tesoreria/tests/test_paridad_hardening.py)
"""
import os
import sys
from datetime import date
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy import UniqueConstraint  # noqa: E402

from database import Base, engine, SessionLocal, get_db  # noqa: E402
from auth import get_current_user  # noqa: E402
from models.models import Cotizacion, OcCliente, ContAdelanto  # noqa: E402
from compras_contab.models import ContEgreso  # noqa: E402
from tesoreria.models import CuentaBancaria, MovimientoBancario, Conciliacion  # noqa: E402
from tesoreria.router import router as tes_router, PAGE_SIZE_MAX  # noqa: E402

MARK = "__TEST_TES_HARD__"
CURRENT = {"empresa": "mineria", "id": None}

Base.metadata.create_all(bind=engine, checkfirst=True)

app = FastAPI()
app.include_router(tes_router, prefix="/api")


# Auth REALISTA (mismo motivo que en test_integration.py): la lectura en la sesión del
# request abre el read view de MySQL ANTES de cualquier with_for_update().
def _current_user_realista(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=CURRENT["id"], empresa=CURRENT["empresa"])


app.dependency_overrides[get_current_user] = _current_user_realista
client = TestClient(app)

_fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _constraint_en_bd(nombre: str, tabla: str = "conc_conciliacion") -> bool:
    db = SessionLocal()
    try:
        return bool(db.execute(
            text("SELECT COUNT(*) FROM information_schema.table_constraints "
                 "WHERE table_schema = DATABASE() AND table_name = :t AND constraint_name = :n"),
            {"t": tabla, "n": nombre}).scalar())
    finally:
        db.close()


def _sembrar_adelantos(db, n=3):
    """Cotización + OC + n adelantos APROBADOS marcados (mínimo indispensable: la cola
    de aprobaciones solo lee el adelanto y el encabezado de la venta)."""
    cot = Cotizacion(numero=f"{MARK}-COT", cliente=f"{MARK} Cliente", rut_cliente="78.279.030-7")
    db.add(cot)
    db.flush()
    oc = OcCliente(cotizacion_id=cot.id, numero_oc=f"{MARK}-OC", fecha_oc="2026-07-01")
    db.add(oc)
    db.flush()
    ids = []
    for i in range(n):
        a = ContAdelanto(oc_cliente_id=oc.id, empresa="mineria", estado="aprobado",
                         monto=1000 + i, monto_aplicado=0, fecha_pago=date(2026, 7, 1),
                         banco="Santander", numero_operacion=f"{MARK}-OP-{i}",
                         observaciones=f"{MARK} adelanto {i}")
        db.add(a)
        db.flush()
        ids.append(a.id)
    db.commit()
    return ids


def run():
    CURRENT["empresa"] = "mineria"

    # ══ T10 · max_length: 422 del cliente, NO 500 del servidor, y sin fila creada ══
    db = SessionLocal()
    try:
        cuentas_antes = db.query(CuentaBancaria).count()
        movs_antes = db.query(MovimientoBancario).count()
    finally:
        db.close()

    r = client.post("/api/tesoreria/cuentas",
                    json={"banco": "B" * 101, "nombre": f"{MARK} Cta", "moneda": "CLP"})
    check("cuenta con banco de 101 chars → 422 (no 500)", r.status_code == 422, r.status_code)
    r = client.post("/api/tesoreria/cuentas",
                    json={"banco": "Santander", "nombre": "N" * 121, "moneda": "CLP"})
    check("cuenta con nombre de 121 chars → 422", r.status_code == 422, r.status_code)
    r = client.post("/api/tesoreria/cuentas",
                    json={"banco": "Santander", "numero_cuenta": "9" * 61, "moneda": "CLP"})
    check("cuenta con numero_cuenta de 61 chars → 422", r.status_code == 422, r.status_code)

    # Sanity: el tope es EXACTO (100 pasa), o el max_length estaría rechazando datos buenos.
    r = client.post("/api/tesoreria/cuentas",
                    json={"banco": "B" * 100, "nombre": f"{MARK} Cta OK", "moneda": "CLP"})
    check("cuenta con banco de 100 chars (el máximo) → 200", r.status_code == 200, r.text)
    cuenta_id = r.json()["id"] if r.status_code == 200 else None

    if cuenta_id:
        r = client.post("/api/tesoreria/movimientos", json={
            "cuenta_id": cuenta_id, "fecha": "2026-07-02", "glosa": "G" * 501,
            "tipo": "abono", "monto": 1000})
        check("movimiento con glosa de 501 chars → 422", r.status_code == 422, r.status_code)
        r = client.post("/api/tesoreria/movimientos", json={
            "cuenta_id": cuenta_id, "fecha": "2026-07-02", "glosa": "ok",
            "tipo": "abono", "monto": 1000, "referencia": "R" * 151})
        check("movimiento con referencia de 151 chars → 422", r.status_code == 422, r.status_code)

    # El schema valida ANTES de que el handler busque el adelanto: con banco largo el 422
    # gana al 404, y con banco válido aparece el 404. Prueba el max_length de
    # AprobarAdelantoIn sin tocar ningún adelanto.
    r = client.post("/api/tesoreria/adelantos/999999999/aprobar",
                    json={"monto": 1000, "banco": "B" * 101})
    check("aprobar adelanto con banco de 101 chars → 422", r.status_code == 422, r.status_code)
    r = client.post("/api/tesoreria/adelantos/999999999/aprobar",
                    json={"monto": 1000, "banco": "Santander"})
    check("aprobar adelanto inexistente (banco válido) → 404", r.status_code == 404, r.status_code)

    db = SessionLocal()
    try:
        # DELTA: solo la cuenta legítima de 100 chars; los 422 no dejaron nada.
        check("los 422 no crearon filas (delta cuentas = 1, movimientos = 0)",
              db.query(CuentaBancaria).count() - cuentas_antes == 1
              and db.query(MovimientoBancario).count() - movs_antes == 0,
              (db.query(CuentaBancaria).count() - cuentas_antes,
               db.query(MovimientoBancario).count() - movs_antes))
    finally:
        db.close()

    # ══ T14 · /aprobaciones paginado de verdad ══════════════════════════════════════
    db = SessionLocal()
    try:
        adel_ids = _sembrar_adelantos(db, n=3)
    finally:
        db.close()

    r = client.get("/api/tesoreria/aprobaciones", params={"page": 1, "page_size": 1})
    check("aprobaciones 200 y conserva las 2 listas de siempre (aditivo)",
          r.status_code == 200 and {"por_aprobar", "aprobadas"}.issubset(r.json()), r.text)
    body = r.json()
    check("aprobaciones informa aprobadas_total / page / page_size",
          {"aprobadas_total", "page", "page_size"}.issubset(body), list(body))
    check("page_size=1 devuelve UNA sola aprobada", len(body["aprobadas"]) == 1, body["aprobadas"])
    check("aprobadas_total cuenta TODAS (≥ las 3 sembradas)",
          int(body.get("aprobadas_total", 0)) >= 3, body.get("aprobadas_total"))

    r2 = client.get("/api/tesoreria/aprobaciones", params={"page": 2, "page_size": 1})
    check("la página 2 trae OTRO adelanto (antes page se ignoraba y repetía la 1)",
          r2.status_code == 200 and len(r2.json()["aprobadas"]) == 1
          and r2.json()["aprobadas"][0]["id"] != body["aprobadas"][0]["id"],
          (body["aprobadas"][0]["id"], (r2.json().get("aprobadas") or [{}])[0].get("id")))

    # Recorrer las páginas alcanza los 3 sembrados: con el `limit(50)` viejo los aprobados
    # más ANTIGUOS eran inalcanzables por diseño (no había forma de pedirlos).
    vistos, page = set(), 1
    total = int(body.get("aprobadas_total", 0))
    while page <= (total // 2) + 2:
        rr = client.get("/api/tesoreria/aprobaciones", params={"page": page, "page_size": 2})
        filas = rr.json().get("aprobadas", [])
        if not filas:
            break
        vistos.update(f["id"] for f in filas)
        page += 1
    check("los 3 adelantos sembrados son alcanzables recorriendo las páginas",
          set(adel_ids).issubset(vistos), (adel_ids, len(vistos)))

    r = client.get("/api/tesoreria/aprobaciones", params={"page": 0, "page_size": 99999})
    check("page/page_size fuera de rango se acotan (page≥1, page_size≤MAX)",
          r.json()["page"] == 1 and r.json()["page_size"] == PAGE_SIZE_MAX,
          (r.json()["page"], r.json()["page_size"]))

    # ══ T9 · UNIQUE(egreso_id) en conc_conciliacion ═════════════════════════════════
    uqs = [c for c in Conciliacion.__table__.constraints
           if isinstance(c, UniqueConstraint) and [col.name for col in c.columns] == ["egreso_id"]]
    check("el modelo declara UNIQUE(egreso_id) en conc_conciliacion",
          len(uqs) == 1 and uqs[0].name == "uq_conc_concil_egreso", [u.name for u in uqs])
    # La migración en sí (idempotencia + qué pasa con duplicados legados) se prueba por
    # CONDUCTA en tesoreria/tests/test_lecturas_de_plata.py: se suelta el UNIQUE, se siembra
    # el caso legado y se corre el init_db real. Acá quedaban dos checks que leían el código
    # fuente (`inspect.getsource(tes_init)` + substrings): detectaban un rename, no un bug.
    check("la restricción está aplicada en la BD local (si falla: python -m tesoreria.init_db)",
          _constraint_en_bd("uq_conc_concil_egreso"))

    # Sonda REAL: dos enlaces distintos al MISMO egreso deben rebotar en la BD.
    db = SessionLocal()
    try:
        eg = ContEgreso(empresa="mineria", fecha=date(2026, 7, 3), medio="transferencia",
                        beneficiario=f"{MARK} ProvUQ", monto_total_clp=1234,
                        glosa=f"{MARK} egreso sonda")
        db.add(eg)
        cta = db.query(CuentaBancaria).filter(CuentaBancaria.nombre == f"{MARK} Cta OK").first()
        m1 = MovimientoBancario(empresa="mineria", cuenta_id=cta.id, fecha=date(2026, 7, 3),
                                glosa=f"{MARK} m1", tipo="cargo", monto=1234)
        m2 = MovimientoBancario(empresa="mineria", cuenta_id=cta.id, fecha=date(2026, 7, 3),
                                glosa=f"{MARK} m2", tipo="cargo", monto=1234)
        db.add_all([m1, m2])
        db.flush()
        db.add(Conciliacion(empresa="mineria", movimiento_id=m1.id, egreso_id=eg.id,
                            monto_conciliado_clp=1234))
        db.commit()
        check("primer enlace cargo↔egreso se inserta", True)
        db.add(Conciliacion(empresa="mineria", movimiento_id=m2.id, egreso_id=eg.id,
                            monto_conciliado_clp=1234))
        try:
            db.commit()
            check("segundo enlace al MISMO egreso → IntegrityError", False,
                  "la BD lo aceptó: el mismo pago quedó conciliado contra dos cargos")
        except IntegrityError:
            db.rollback()
            check("segundo enlace al MISMO egreso → IntegrityError (1:1 respaldado por la BD)", True)
    finally:
        db.close()


def _cleanup():
    db = SessionLocal()
    try:
        db.rollback()
        # Orden FK-seguro: enlaces → movimientos/cartolas → cuentas → egresos →
        # adelantos → OCs → cotizaciones.
        ctas = db.query(CuentaBancaria).filter(CuentaBancaria.nombre.like(f"{MARK}%")).all()
        for cta in ctas:
            db.delete(cta)   # cascade movimientos + cartolas + conciliaciones
        db.commit()
        for eg in db.query(ContEgreso).filter(ContEgreso.beneficiario.like(f"%{MARK}%")).all():
            db.delete(eg)
        db.commit()
        cots = db.query(Cotizacion).filter(Cotizacion.numero.like(f"{MARK}%")).all()
        oc_ids = [oc.id for oc in db.query(OcCliente)
                  .filter(OcCliente.cotizacion_id.in_([c.id for c in cots])).all()] if cots else []
        if oc_ids:
            db.query(ContAdelanto).filter(
                ContAdelanto.oc_cliente_id.in_(oc_ids)).delete(synchronize_session=False)
            db.query(OcCliente).filter(OcCliente.id.in_(oc_ids)).delete(synchronize_session=False)
        if cots:
            db.query(Cotizacion).filter(
                Cotizacion.id.in_([c.id for c in cots])).delete(synchronize_session=False)
        db.commit()
        # Verificación por DELTAS: nada marcado sobrevive.
        resto = (db.query(CuentaBancaria).filter(CuentaBancaria.nombre.like(f"{MARK}%")).count()
                 + db.query(ContEgreso).filter(ContEgreso.beneficiario.like(f"%{MARK}%")).count()
                 + db.query(Cotizacion).filter(Cotizacion.numero.like(f"{MARK}%")).count()
                 + db.query(ContAdelanto).filter(
                     ContAdelanto.numero_operacion.like(f"{MARK}%")).count())
        print(f"\nCleanup OK (filas marcadas restantes: {resto})")
        if resto:
            _fails.append("cleanup dejó filas marcadas")
    finally:
        db.close()


def test_tesoreria_paridad_hardening():
    """Wrapper para pytest: sin él los checks correrían en el import y un fallo pasaría
    en silencio (verde falso)."""
    try:
        run()
    finally:
        _cleanup()
    assert not _fails, f"fallas: {_fails}"


if __name__ == "__main__":
    try:
        run()
    finally:
        _cleanup()
    print("\n=== RESULTADO:", "TODO OK" if not _fails else f"{len(_fails)} FALLAS: {_fails}", "===")
    sys.exit(1 if _fails else 0)
