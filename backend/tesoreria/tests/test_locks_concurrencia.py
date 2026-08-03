"""Sondas de CONDUCTA del orden de candados de Tesorería (Grupo AM), con DOS sesiones
MySQL de verdad.

Reemplaza la sonda vieja de la CAPA 1, que verificaba el orden de locks leyendo el CÓDIGO
FUENTE (`inspect.getsource` + `find`). Esa sonda no tenía poder discriminante: un auditor
reintrodujo el ciclo InnoDB (un `SELECT ... FOR UPDATE` de la cobranza ANTES del de la
factura) dejando intactas todas las cadenas que buscaba y el gate quedó VERDE con el bug de
vuelta; y al revés, agregar un `populate_existing()` la ponía ROJA sin ningún cambio de
conducta. Acá no se lee el fuente: se ejerce la función/endpoint REAL con otra sesión
peleando por las mismas filas, y se mira si InnoDB levanta 1213.

Los DOS pares que el módulo puede cruzar (ambos de uso diario):
  · PAR COBRANZA — conciliar un abono (Tesorería) ⟂ registrar/eliminar otra cobranza de la
    MISMA factura (Facturas y Cobranzas, que bloquea FACTURA y después sus COBRANZAS).
    CAPA 1 = `_conciliar_tx` bloquea en el orden canónico FACTURA → COBRANZA.
  · PAR PAGO — aprobar un pago (Tesorería) ⟂ revertir un pago de la MISMA compra en
    Compras/CxP (`eliminar_pago` / `eliminar_egreso`, que bloquean EGRESO → DETALLES → COMPRA,
    justo al revés que `_crear_egreso`, que hace COMPRA → DETALLES).
    CAPA 1 = `_porton_egresos_de_las_compras` toma primero la fila del EGRESO (el mismo
    PRIMER recurso del lado que revierte) y las dos transacciones se serializan ahí.
Y la CAPA 2 de los dos: `_con_retry_deadlock` reintenta la transacción completa. Se prueba
inyectando el fallo en el `commit()` (que es como InnoDB aborta a la víctima) y verificando
que el reintento NO duplica plata: un solo egreso, un solo enlace.

CONTROL POSITIVO del arnés: antes de cada escenario, dos sesiones sobre las MISMAS filas
marcadas en orden INVERTIDO deben producir un 1213. Sin ese control, un verde podría
significar simplemente que el arnés nunca generó contención.

El lado que emula a otro módulo se escribe con SQL crudo porque no se puede pausar un
endpoint por dentro; el lado de Tesorería SIEMPRE es el código real. Datos MARCADOS +
limpieza + verificación por deltas: no toca ni una fila real.

Corre con:  cd backend && ./venv/bin/python -m pytest tesoreria/tests/test_locks_concurrencia.py -q
(también:   ./venv/bin/python tesoreria/tests/test_locks_concurrencia.py)
"""
import os
import sys
import threading
import time
from datetime import date
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from sqlalchemy import text  # noqa: E402

from database import Base, engine, SessionLocal  # noqa: E402
from models.models import ContFacturaCliente, ContCobranza  # noqa: E402
from compras_contab.models import ContCompra, ContEgreso, ContEgresoDetalle  # noqa: E402
from compras_contab.schemas import EgresoCreate, EgresoDetalleIn  # noqa: E402
from tesoreria.models import CuentaBancaria, MovimientoBancario, ConciliacionIngreso  # noqa: E402
from tesoreria.router import (  # noqa: E402
    aprobar_pago, conciliar, _aprobar_pago_tx, _conciliar_tx,
)
from tesoreria.schemas import ConciliarIn  # noqa: E402

MARK = "__TEST_TES_LOCK__"
USUARIO = SimpleNamespace(id=None, empresa="mineria")
# Margen para que el hilo de Tesorería alcance su punto de bloqueo antes de que el otro
# lado pida el segundo candado (es cuando InnoDB detecta el ciclo, si lo hay).
ESPERA_BLOQUEO = 1.5
# Tope de espera del hilo: si el escenario se cruza mal, InnoDB corta por
# innodb_lock_wait_timeout (50 s por defecto) y el test informa en vez de colgarse.
TIMEOUT_HILO = 70

Base.metadata.create_all(bind=engine, checkfirst=True)

_fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _codigo_mysql(e) -> int:
    """errno de MySQL de una excepción de SQLAlchemy (1213 deadlock, 1205 lock timeout)."""
    if e is None:
        return 0
    args = getattr(getattr(e, "orig", None), "args", None) or getattr(e, "args", None) or ()
    return args[0] if args and isinstance(args[0], int) else 0


def _hubo_deadlock(*errores) -> bool:
    return any(_codigo_mysql(e) in (1213, 1205) for e in errores)


def _en_hilo(fn):
    """Corre fn() en un hilo y devuelve (hilo, resultado). El resultado trae 'out' o 'exc'."""
    res = {}

    def _run():
        try:
            res["out"] = fn()
        except BaseException as e:  # noqa: BLE001  — el 1213 también viaja como excepción
            res["exc"] = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t, res


class _CommitVictima:
    """Sesión envuelta cuyo PRIMER `commit()` falla con 1213, tal como InnoDB aborta la
    transacción que elige víctima: hace rollback real (nada quedó escrito) y levanta el
    error. Sirve para ejercer la CAPA 2 sin depender de una carrera afortunada."""

    def __init__(self, real):
        self._real = real
        self.fallos = 0

    def __getattr__(self, nombre):
        return getattr(self._real, nombre)

    def commit(self):
        if self.fallos == 0:
            self.fallos += 1
            self._real.rollback()
            from sqlalchemy.exc import OperationalError
            orig = Exception()
            orig.args = (1213, "Deadlock found when trying to get lock; try restarting transaction")
            raise OperationalError("COMMIT", {}, orig)
        return self._real.commit()


# ─── Siembra ───────────────────────────────────────────────────────────────────
def _sembrar_compra_con_pago(sufijo: str, *, total: float, pagado: float) -> tuple:
    """Compra marcada + un Comprobante de Egreso que ya le pagó `pagado` (es el egreso que
    el otro lado va a intentar revertir). Devuelve (compra_id, egreso_id)."""
    db = SessionLocal()
    try:
        c = ContCompra(
            empresa="mineria", tipo_gasto="otros", condicion_pago="credito",
            acreedor=f"{MARK} Prov{sufijo}", numero_documento=f"{MARK}-{sufijo}",
            fecha=date(2026, 7, 1), moneda="CLP", tc=1,
            monto_neto=total, iva=0, monto_total=total, monto_total_clp=total,
            monto_pagado_clp=0, saldo_clp=total, estado_pago="pendiente", anulado=False)
        db.add(c)
        db.flush()
        e = ContEgreso(empresa="mineria", fecha=date(2026, 7, 1), medio="transferencia",
                       beneficiario=f"{MARK} Prov{sufijo}", monto_total_clp=pagado,
                       moneda="CLP", tc=1, monto_origen=pagado,
                       glosa=f"{MARK} pago previo {sufijo}")
        db.add(e)
        db.flush()
        db.add(ContEgresoDetalle(egreso_id=e.id, compra_id=c.id, monto_clp=pagado,
                                 tc_aplicado=1, monto_origen=pagado))
        c.monto_pagado_clp = pagado
        c.saldo_clp = round(total - pagado, 2)
        c.estado_pago = "parcial"
        db.commit()
        return c.id, e.id
    finally:
        db.close()


def _cuenta_marcada(sufijo: str) -> int:
    db = SessionLocal()
    try:
        c = CuentaBancaria(empresa="mineria", banco="Santander",
                           nombre=f"{MARK} Cta{sufijo}", moneda="CLP", activo=True)
        db.add(c)
        db.commit()
        return c.id
    finally:
        db.close()


def _sembrar_factura_cobranza_abono(sufijo: str, cuenta_id: int, monto: float) -> tuple:
    """Factura + cobranza (ingreso de caja) + abono del banco por el mismo monto.
    Devuelve (factura_id, cobranza_id, movimiento_id)."""
    db = SessionLocal()
    try:
        f = ContFacturaCliente(
            empresa="mineria", numero_factura=f"{MARK}-F{sufijo}", tipo_doc="factura",
            fecha_emision=date(2026, 7, 1), fecha_vencimiento=date(2026, 7, 30),
            monto_neto=monto, iva=0, monto_bruto=monto, monto_pagado=monto, saldo=0,
            estado_pago="pagada")
        db.add(f)
        db.flush()
        c = ContCobranza(factura_id=f.id, fecha=date(2026, 7, 2), monto=monto,
                         medio="transferencia", banco="Santander",
                         numero_operacion=f"{MARK}-OP{sufijo}")
        db.add(c)
        m = MovimientoBancario(empresa="mineria", cuenta_id=cuenta_id, fecha=date(2026, 7, 2),
                               glosa=f"{MARK} abono {sufijo}", tipo="abono", monto=monto)
        db.add(m)
        db.commit()
        return f.id, c.id, m.id
    finally:
        db.close()


def _mov_cargo(cuenta_id: int, sufijo: str, monto: float) -> int:
    db = SessionLocal()
    try:
        m = MovimientoBancario(empresa="mineria", cuenta_id=cuenta_id, fecha=date(2026, 7, 3),
                               glosa=f"{MARK} cargo {sufijo}", tipo="cargo", monto=monto)
        db.add(m)
        db.commit()
        return m.id
    finally:
        db.close()


# ─── Llamadas al código REAL de Tesorería (cada una con su sesión, como get_db) ────
def _payload_pago(compra_id: int, monto: float, sufijo: str) -> EgresoCreate:
    return EgresoCreate(
        fecha=date(2026, 7, 5).isoformat(), medio="transferencia",
        beneficiario=f"{MARK} Prov{sufijo}", glosa=f"{MARK} pago tesoreria {sufijo}",
        detalles=[EgresoDetalleIn(compra_id=compra_id, monto_clp=monto)])


def _aprobar_pago_tx_real(compra_id: int, monto: float, sufijo: str):
    """El CUERPO real del endpoint, SIN la red de la CAPA 2: así un 1213 se ve tal cual.
    Sin esto la sonda no discrimina — el reintento absorbe el deadlock y el test queda
    verde con la CAPA 1 muerta (comprobado)."""
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))  # el read view nace como en producción (guard de auth)
        egreso = _aprobar_pago_tx(_payload_pago(compra_id, monto, sufijo), "mineria",
                                  date(2026, 7, 5), db, USUARIO)
        return {"monto_total_clp": float(egreso.monto_total_clp or 0)}
    finally:
        db.close()


def _aprobar_pago_endpoint_victima(compra_id: int, monto: float, sufijo: str):
    """El endpoint COMPLETO (con CAPA 2) contra una sesión cuyo primer commit es víctima."""
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        return aprobar_pago(_payload_pago(compra_id, monto, sufijo),
                            db=_CommitVictima(db), current_user=USUARIO)
    finally:
        db.close()


def _conciliar_tx_real(mov_id: int, cobranza_id: int):
    """La FUNCIÓN real, sin la red de la CAPA 2: así un 1213 se ve tal cual (que es lo que
    esta sonda mide). El endpoint con red se ejerce aparte."""
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        return _conciliar_tx(mov_id, ConciliarIn(cobranza_id=cobranza_id), db, USUARIO)
    finally:
        db.close()


def _conciliar_endpoint_victima(mov_id: int, cobranza_id: int):
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        return conciliar(mov_id, ConciliarIn(cobranza_id=cobranza_id),
                         db=_CommitVictima(db), current_user=USUARIO)
    finally:
        db.close()


# ─── Control positivo del arnés ────────────────────────────────────────────────
def _control_positivo(tabla_a: str, id_a: int, tabla_b: str, id_b: int) -> int:
    """Dos sesiones sobre las MISMAS dos filas marcadas, en orden INVERTIDO. Devuelve el
    errno observado (debe ser 1213/1205): si no lo produce, el arnés no puede detectar un
    deadlock y cualquier verde de esta suite no significaría nada."""
    c1, c2 = engine.connect(), engine.connect()
    err = []
    try:
        c1.execute(text(f"SELECT id FROM {tabla_a} WHERE id = :i FOR UPDATE"), {"i": id_a})
        c2.execute(text(f"SELECT id FROM {tabla_b} WHERE id = :i FOR UPDATE"), {"i": id_b})

        def _c1_pide_b():
            c1.execute(text(f"SELECT id FROM {tabla_b} WHERE id = :i FOR UPDATE"), {"i": id_b})

        t, res = _en_hilo(_c1_pide_b)
        time.sleep(0.8)
        try:
            c2.execute(text(f"SELECT id FROM {tabla_a} WHERE id = :i FOR UPDATE"), {"i": id_a})
        except Exception as e:  # noqa: BLE001
            err.append(e)
        t.join(timeout=TIMEOUT_HILO)
        if res.get("exc") is not None:
            err.append(res["exc"])
        return max((_codigo_mysql(e) for e in err), default=0)
    finally:
        for c in (c1, c2):
            try:
                c.rollback()
            finally:
                c.close()


def run():
    # ══ PAR PAGO — CAPA 1 ═══════════════════════════════════════════════════════
    # Compra de 1.000.000 con un pago previo de 100.000 (el egreso que Compras revierte).
    compra_id, egreso_id = _sembrar_compra_con_pago("A1", total=1_000_000, pagado=100_000)

    codigo = _control_positivo("cont_compra", compra_id, "cont_egreso", egreso_id)
    check("CONTROL POSITIVO par pago: el arnés SÍ detecta un 1213/1205 (orden invertido)",
          codigo in (1213, 1205), codigo)

    # Lado B = compras_contab.eliminar_pago / eliminar_egreso, con su orden real de
    # candados: EGRESO → DETALLES (X del DELETE en cascada) → COMPRA. Es SQL crudo porque
    # no se puede pausar ese endpoint por dentro; los pasos son los suyos, 1 a 1.
    conn_b = engine.connect()
    err_b = None
    try:
        conn_b.execute(text("SELECT id FROM cont_egreso WHERE id = :i FOR UPDATE"),
                       {"i": egreso_id})
        conn_b.execute(text("DELETE FROM cont_egreso_detalle WHERE egreso_id = :i"),
                       {"i": egreso_id})
        # Lado A = el endpoint REAL de Tesorería aprobando otro pago de esa misma compra.
        hilo, res_a = _en_hilo(lambda: _aprobar_pago_tx_real(compra_id, 200_000, "A1"))
        time.sleep(ESPERA_BLOQUEO)
        en_vuelo = hilo.is_alive()
        try:
            conn_b.execute(text("SELECT id FROM cont_compra WHERE id = :i FOR UPDATE"),
                           {"i": compra_id})
        except Exception as e:  # noqa: BLE001
            err_b = e
    finally:
        conn_b.rollback()  # nada de lo que hizo B queda (los detalles vuelven)
        conn_b.close()
    hilo.join(timeout=TIMEOUT_HILO)
    check("par PAGO: VALIDEZ de la sonda — Tesorería estaba bloqueada cuando Compras pidió "
          "su 2º candado (si no, no hubo contención que medir)", en_vuelo)
    check("par PAGO: Tesorería NO recibe deadlock aprobando mientras Compras revierte",
          not _hubo_deadlock(res_a.get("exc")),
          f"{type(res_a.get('exc')).__name__}: {res_a.get('exc')}")
    check("par PAGO: el lado de Compras tampoco es víctima",
          not _hubo_deadlock(err_b), f"{err_b}")
    check("par PAGO: el pago se aprobó igual (200.000 al proveedor)",
          isinstance(res_a.get("out"), dict)
          and abs(float(res_a["out"].get("monto_total_clp") or 0) - 200_000) < 1,
          res_a.get("out") or res_a.get("exc"))

    db = SessionLocal()
    try:
        c = db.query(ContCompra).filter(ContCompra.id == compra_id).first()
        check("par PAGO: la compra quedó con los DOS pagos (100.000 + 200.000)",
              abs(float(c.monto_pagado_clp or 0) - 300_000) < 1, c.monto_pagado_clp)
    finally:
        db.close()

    # ══ PAR PAGO — CAPA 2: el cuerpo es reintentable y NO duplica el pago ════════
    compra2_id, _ = _sembrar_compra_con_pago("A2", total=500_000, pagado=0)
    db = SessionLocal()
    try:
        egresos_antes = (db.query(ContEgreso)
                         .filter(ContEgreso.glosa.like(f"{MARK} pago tesoreria A2%")).count())
    finally:
        db.close()
    try:
        salida = _aprobar_pago_endpoint_victima(compra2_id, 120_000, "A2")
        exc2 = None
    except BaseException as e:  # noqa: BLE001
        salida, exc2 = None, e
    check("par PAGO: un 1213 en el commit se reintenta y termina en 200 (no 500)",
          isinstance(salida, dict) and exc2 is None,
          f"{type(exc2).__name__}: {exc2}")
    db = SessionLocal()
    try:
        egresos = (db.query(ContEgreso)
                   .filter(ContEgreso.glosa.like(f"{MARK} pago tesoreria A2%")).all())
        c2 = db.query(ContCompra).filter(ContCompra.id == compra2_id).first()
        check("par PAGO: el reintento dejó UN solo egreso (no pagó dos veces)",
              len(egresos) - egresos_antes == 1, len(egresos))
        check("par PAGO: la compra quedó pagada UNA vez (120.000)",
              abs(float(c2.monto_pagado_clp or 0) - 120_000) < 1, c2.monto_pagado_clp)
    finally:
        db.close()

    # ══ PAR COBRANZA — CAPA 1 ═══════════════════════════════════════════════════
    cuenta_id = _cuenta_marcada("C")
    fid, cid, mid = _sembrar_factura_cobranza_abono("C1", cuenta_id, 119_000)

    codigo = _control_positivo("cont_factura_cliente", fid, "cont_cobranza", cid)
    check("CONTROL POSITIVO par cobranza: el arnés SÍ detecta un 1213/1205",
          codigo in (1213, 1205), codigo)

    # Lado B = Facturas y Cobranzas (registrar_cobranza / eliminar_cobranza): bloquea la
    # FACTURA y después SUS cobranzas (`_cobranzas_bloqueadas`).
    conn_b = engine.connect()
    err_b = None
    try:
        conn_b.execute(text("SELECT id FROM cont_factura_cliente WHERE id = :i FOR UPDATE"),
                       {"i": fid})
        hilo, res_a = _en_hilo(lambda: _conciliar_tx_real(mid, cid))
        time.sleep(ESPERA_BLOQUEO)
        en_vuelo = hilo.is_alive()
        try:
            conn_b.execute(text("SELECT id FROM cont_cobranza WHERE factura_id = :i FOR UPDATE"),
                           {"i": fid})
        except Exception as e:  # noqa: BLE001
            err_b = e
    finally:
        conn_b.rollback()
        conn_b.close()
    hilo.join(timeout=TIMEOUT_HILO)
    check("par COBRANZA: VALIDEZ de la sonda — Tesorería estaba bloqueada cuando Facturas "
          "pidió sus cobranzas (si no, no hubo contención que medir)", en_vuelo)
    check("par COBRANZA: Tesorería NO recibe deadlock conciliando mientras Facturas "
          "bloquea la factura y sus cobranzas",
          not _hubo_deadlock(res_a.get("exc")),
          f"{type(res_a.get('exc')).__name__}: {res_a.get('exc')}")
    check("par COBRANZA: el lado de Facturas y Cobranzas tampoco es víctima",
          not _hubo_deadlock(err_b), f"{err_b}")
    check("par COBRANZA: la conciliación se completó igual",
          isinstance(res_a.get("out"), dict) and res_a["out"].get("conciliado") is True,
          res_a.get("out") or res_a.get("exc"))
    db = SessionLocal()
    try:
        check("par COBRANZA: quedó UN enlace abono↔cobranza",
              db.query(ConciliacionIngreso).filter(
                  ConciliacionIngreso.cobranza_id == cid).count() == 1)
    finally:
        db.close()

    # ══ PAR COBRANZA — CAPA 2: el endpoint reintenta y no duplica el enlace ══════
    fid2, cid2, mid2 = _sembrar_factura_cobranza_abono("C2", cuenta_id, 77_000)
    try:
        salida = _conciliar_endpoint_victima(mid2, cid2)
        exc2 = None
    except BaseException as e:  # noqa: BLE001
        salida, exc2 = None, e
    check("par COBRANZA: un 1213 en el commit se reintenta y termina en 200 (no 500)",
          isinstance(salida, dict) and exc2 is None, f"{type(exc2).__name__}: {exc2}")
    db = SessionLocal()
    try:
        check("par COBRANZA: el reintento dejó UN solo enlace (no duplicó el depósito)",
              db.query(ConciliacionIngreso).filter(
                  ConciliacionIngreso.cobranza_id == cid2).count() == 1)
    finally:
        db.close()

    # ══ El candado de EMPRESA sigue vivo tras el desarme del JOIN (conducta, no texto) ══
    # Una cobranza de MonzaParts contra un abono de Grupo AM: 404 y CERO enlaces.
    fid3, cid3, mid3 = _sembrar_factura_cobranza_abono("C3", cuenta_id, 55_000)
    db = SessionLocal()
    try:
        f3 = db.query(ContFacturaCliente).filter(ContFacturaCliente.id == fid3).first()
        f3.empresa = "automotriz"
        db.commit()
    finally:
        db.close()
    try:
        _conciliar_tx_real(mid3, cid3)
        check("candado de empresa: cobranza de otra marca → 404", False, "concilió")
    except BaseException as e:  # noqa: BLE001
        check("candado de empresa: cobranza de otra marca → 404",
              getattr(e, "status_code", None) == 404, f"{type(e).__name__}: {e}")
    db = SessionLocal()
    try:
        check("candado de empresa: cero enlaces creados",
              db.query(ConciliacionIngreso).filter(
                  ConciliacionIngreso.cobranza_id == cid3).count() == 0)
        m3 = db.query(MovimientoBancario).filter(MovimientoBancario.id == mid3).first()
        check("candado de empresa: el movimiento quedó sin conciliar", not m3.conciliado)
    finally:
        db.close()


def _cleanup():
    db = SessionLocal()
    try:
        db.rollback()
        for o in db.query(CuentaBancaria).filter(CuentaBancaria.nombre.like(f"{MARK}%")).all():
            db.delete(o)  # cascade movimientos + cartolas + conciliaciones (+ ingreso)
        db.commit()
        for o in db.query(ContEgreso).filter(ContEgreso.beneficiario.like(f"%{MARK}%")).all():
            db.delete(o)  # cascade detalles
        db.commit()
        for o in db.query(ContCompra).filter(ContCompra.numero_documento.like(f"{MARK}%")).all():
            db.delete(o)
        db.commit()
        for f in db.query(ContFacturaCliente).filter(
                ContFacturaCliente.numero_factura.like(f"{MARK}%")).all():
            for lk in db.query(ConciliacionIngreso).filter(
                    ConciliacionIngreso.cobranza_id.in_(
                        [c.id for c in f.cobranzas] or [0])).all():
                db.delete(lk)
            db.delete(f)  # cascade items + cobranzas
        db.commit()
        resto = (db.query(CuentaBancaria).filter(CuentaBancaria.nombre.like(f"{MARK}%")).count()
                 + db.query(ContEgreso).filter(ContEgreso.beneficiario.like(f"%{MARK}%")).count()
                 + db.query(ContCompra).filter(ContCompra.numero_documento.like(f"{MARK}%")).count()
                 + db.query(ContFacturaCliente).filter(
                     ContFacturaCliente.numero_factura.like(f"{MARK}%")).count())
        print(f"\nCleanup OK (filas marcadas restantes: {resto})")
        if resto:
            _fails.append("cleanup dejó filas marcadas")
    finally:
        db.close()


def test_tesoreria_locks_concurrencia():
    """Wrapper para pytest: sin él los checks correrían en el import y un fallo pasaría en
    silencio (verde falso)."""
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
