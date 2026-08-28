"""La ficha del cliente se toma con lock antes de sumarle plata.

`ltv` y `vendidos_total` se leen, se modifican y se escriben. Sin lock, dos ventas del
MISMO cliente despachadas a la vez se pisan: la segunda lee el valor viejo y guarda su
suma encima de la de la otra, así que la plata desaparece de la ficha en silencio, sin
error ni rastro.

NOTA DE MÉTODO — por qué esta prueba mide el LOCK y no «el LTV final»
    La sonda obvia (cerrar N despachos a la vez por la API y comparar el total) pasa en
    verde CON y SIN el lock: el arnés de pruebas no logra abrir la ventana de carrera, así
    que no discrimina nada y daría una falsa tranquilidad. Ésta va directo al mecanismo:
    una sesión toma la ficha y no confirma; la segunda tiene que ESPERAR. Verificado por
    mutación: quitando `.with_for_update()`, la segunda pasa de largo en 4 ms en vez de
    quedarse esperando.
"""
import threading
import time

import pytest
from sqlalchemy import text

from database import SessionLocal
from monza_models import MonzaCliente, MonzaCotizacion
import monza_router_cotizaciones as mod

MARK = "test-ltv-lock"
# Segundos que la segunda sesión espera antes de rendirse. Corto a propósito: la prueba
# quiere comprobar que ESPERA, no cuánto.
_TIMEOUT_LOCK = 3


def _limpiar():
    db = SessionLocal()
    db.query(MonzaCotizacion).filter(MonzaCotizacion.numero.like(f"{MARK}%")).delete(synchronize_session="fetch")
    db.query(MonzaCliente).filter(MonzaCliente.nombre.like(f"{MARK}%")).delete(synchronize_session="fetch")
    db.commit()
    db.close()


@pytest.fixture(autouse=True)
def limpieza():
    _limpiar()
    yield
    _limpiar()


def _sembrar():
    db = SessionLocal()
    c = MonzaCliente(nombre=f"{MARK} CLI", ltv=0)
    db.add(c)
    db.flush()
    cot = MonzaCotizacion(numero=f"{MARK}-1", cliente_id=c.id, estado="despachado", total_bruto=1000)
    db.add(cot)
    db.commit()
    ids = (c.id, cot.id)
    db.close()
    return ids


def test_la_segunda_venta_espera_el_lock_de_la_ficha():
    """SONDA: con la ficha tomada por una sesión, la otra no puede escribirla."""
    _, cot_id = _sembrar()

    primera = SessionLocal()
    cot1 = primera.query(MonzaCotizacion).filter(MonzaCotizacion.id == cot_id).first()
    # Toma el lock de la ficha y NO confirma: la deja retenida.
    mod.aplicar_efectos_venta_despachada(primera, cot1)

    resultado = {}

    def segunda_sesion():
        db2 = SessionLocal()
        t0 = time.time()
        try:
            db2.execute(text(f"SET SESSION innodb_lock_wait_timeout = {_TIMEOUT_LOCK}"))
            cot2 = db2.query(MonzaCotizacion).filter(MonzaCotizacion.id == cot_id).first()
            mod.aplicar_efectos_venta_despachada(db2, cot2)
            resultado["estado"] = "paso_de_largo"
        except Exception:  # noqa: BLE001 - el timeout del lock ES el resultado esperado
            resultado["estado"] = "espero"
        finally:
            resultado["segundos"] = time.time() - t0
            db2.rollback()
            db2.close()

    hilo = threading.Thread(target=segunda_sesion)
    hilo.start()
    hilo.join(timeout=_TIMEOUT_LOCK + 10)
    primera.rollback()
    primera.close()

    assert resultado.get("estado") == "espero", (
        "la segunda sesión escribió la ficha mientras la primera la tenía tomada: "
        "el lock no está puesto y la plata se pisa en silencio"
    )
    # Y esperó de verdad, no falló por otra causa instantánea.
    assert resultado["segundos"] >= _TIMEOUT_LOCK - 0.5, resultado
