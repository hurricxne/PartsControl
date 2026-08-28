"""El reintento de creaciones simultáneas no puede duplicar lo que ya se guardó.

Cuatro rondas de auditoría encontraron el MISMO patrón en puertas distintas: la operación
confirmaba en la base y después fallaba en algo accesorio (escribir un log, mandar una
notificación); el bucle la reintentaba y una sola petición terminaba creando DOS leads —o
dos cotizaciones, con sus ítems y sus precios— devolviendo un único 201.

Se arregló cada puerta poniendo su log dentro de la transacción, pero eso sigue dependiendo
de que el próximo llamador ordene bien su cuerpo. El candado real vive en
`reintentar_carrera`: escucha los commits de la sesión y, si hubo aunque sea uno, propaga
el error en vez de reintentar. Estas pruebas cubren ESE candado, que es lo que protege
también a la puerta que alguien agregue mañana.
"""
import pytest
from fastapi import HTTPException
from sqlalchemy.exc import OperationalError

from database import SessionLocal
from monza_correlativos import reintentar_carrera, siguiente_secuencia
from monza_models import MonzaCliente, MonzaLead

MARK = "test-correlativos"
# Prefijo CORTO para los números de lead: `numero` es una columna angosta y el MARK
# largo la desborda (DataError 1406).
PREF = "TCOR"


def _deadlock():
    """Un OperationalError con el código 1213 que MySQL usa para el deadlock."""
    class _Orig:
        args = (1213, "Deadlock found when trying to get lock")
    err = OperationalError("stmt", "params", Exception())
    err.orig = _Orig()
    return err


def _limpiar():
    db = SessionLocal()
    db.query(MonzaLead).filter(MonzaLead.numero.like(f"{PREF}%")).delete(synchronize_session="fetch")
    db.query(MonzaCliente).filter(MonzaCliente.nombre.like(f"{MARK}%")).delete(synchronize_session="fetch")
    db.commit()
    db.close()


@pytest.fixture(autouse=True)
def limpieza():
    _limpiar()
    yield
    _limpiar()


def test_falla_antes_del_commit_se_reintenta():
    """El caso para el que el bucle existe: nada se guardó, rehacer es correcto."""
    db = SessionLocal()
    intentos = {"n": 0}

    def operacion():
        intentos["n"] += 1
        if intentos["n"] < 3:
            raise _deadlock()
        db.add(MonzaCliente(nombre=f"{MARK} A"))
        db.commit()
        return "listo"

    assert reintentar_carrera(db, operacion, que="pruebas") == "listo"
    assert intentos["n"] == 3
    db.close()

    verif = SessionLocal()
    assert verif.query(MonzaCliente).filter(MonzaCliente.nombre == f"{MARK} A").count() == 1
    verif.close()


def test_falla_DESPUES_del_commit_no_se_reintenta():
    """SONDA: con la fila ya guardada, reintentar no repara — duplica."""
    db = SessionLocal()
    intentos = {"n": 0}

    def operacion():
        intentos["n"] += 1
        db.add(MonzaCliente(nombre=f"{MARK} B"))
        db.commit()
        raise _deadlock()   # el fallo llega DESPUÉS de guardar

    with pytest.raises(OperationalError):
        reintentar_carrera(db, operacion, que="pruebas")
    assert intentos["n"] == 1, "se reintentó una operación ya confirmada"
    db.close()

    verif = SessionLocal()
    n = verif.query(MonzaCliente).filter(MonzaCliente.nombre == f"{MARK} B").count()
    verif.close()
    assert n == 1, f"la petición creó {n} filas: el reintento duplicó lo ya guardado"


def test_reintentos_agotados_dan_409_con_mensaje_del_operador():
    db = SessionLocal()

    def siempre_choca():
        raise _deadlock()

    with pytest.raises(HTTPException) as exc:
        reintentar_carrera(db, siempre_choca, vueltas=2, que="cotizaciones")
    assert exc.value.status_code == 409
    assert "cotizaciones" in exc.value.detail
    db.close()


# ─────────────── El número que devuelve el generador ───────────────

def test_la_secuencia_toma_el_MAXIMO_no_la_ultima_fila():
    """SONDA: una fila con id ALTO y número BAJO —lo que deja cualquier migración—
    hacía que el generador devolviera para siempre un número ya usado, y la creación
    quedaba muerta contra el índice único."""
    db = SessionLocal()
    cliente = MonzaCliente(nombre=f"{MARK} CLI")
    db.add(cliente)
    db.flush()
    # Se insertan EN ESTE ORDEN: el número más alto primero, así el id más alto queda
    # con el número más bajo.
    db.add(MonzaLead(numero=f"{PREF}-0009", cliente_id=cliente.id, estado="pendiente"))
    db.flush()
    db.add(MonzaLead(numero=f"{PREF}-0002", cliente_id=cliente.id, estado="pendiente"))
    db.commit()

    siguiente = siguiente_secuencia(db, MonzaLead.numero, f"{PREF}-")
    db.close()
    assert siguiente == 10, f"esperaba 10 (máximo 9 + 1), dio {siguiente}"


def test_la_secuencia_ignora_sufijos_no_numericos():
    """Un dato legado con formato raro no puede tumbar la creación."""
    db = SessionLocal()
    cliente = MonzaCliente(nombre=f"{MARK} CLI2")
    db.add(cliente)
    db.flush()
    db.add(MonzaLead(numero=f"{PREF}-ABC", cliente_id=cliente.id, estado="pendiente"))
    db.add(MonzaLead(numero=f"{PREF}-0003", cliente_id=cliente.id, estado="pendiente"))
    db.commit()
    siguiente = siguiente_secuencia(db, MonzaLead.numero, f"{PREF}-")
    db.close()
    assert siguiente == 4


def test_serie_vacia_empieza_en_uno():
    db = SessionLocal()
    assert siguiente_secuencia(db, MonzaLead.numero, f"{PREF}-V-") == 1
    db.close()
