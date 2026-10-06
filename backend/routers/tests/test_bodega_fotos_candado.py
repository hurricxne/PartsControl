"""Las fotos de recepción: CUÁNDO se pueden subir o borrar.

(QUIÉN puede —sesión, marca, pertenencia, confinamiento— está en test_bodega_fotos_auth.py.)

QUÉ PROTEGE
    · Una recepción CERRADA es de solo consulta: sus fotos respaldan los reclamos al
      proveedor. La pantalla esconde los botones, pero el que decide es el backend.
    · Un ítem guardado como dañado no queda sin foto: la regla que `marcar_item` exige al
      guardar se sostiene también al borrar.
    · Subir una foto espera el candado de la recepción, igual que `marcar_item`: si no,
      una foto subida mientras se guarda el ítem crea una SEGUNDA fila del mismo ítem. Y
      espera en un hilo, sin detener al resto del servidor.
    · Archivo y fila van juntos: si falla la escritura o el commit no queda un archivo
      que nadie puede pedir ni borrar.

SONDAS DE PODER DISCRIMINANTE — qué hay que quitar para que cada etapa falle
    · `_paso_0` ← en `_recepcion_abierta_con_lock`, invertir a `rec.estado != "cerrada"`
                   (contrapeso: sin él, los 400 de `_paso_1` pasarían con un helper que
                   rechaza siempre); o en `delete_foto`, volver siempre verdadera la
                   condición `if "danado" in (...)` (el 409 saldría para cualquier ítem).
    · `_paso_1` ← quitar `or rec.estado == "cerrada"` del helper; o en `upload_foto`
                   reemplazar el helper por una lectura plana de la recepción; o quitar
                   de `delete_foto` la llamada al helper.
    · `_paso_2` ← quitar de `delete_foto` el bloque `if "danado" in (...)`; o quitar
                   `RecepcionItemFoto.id != foto.id` de la cuenta; o quitar la condición
                   del estado (bloquea la última foto de cualquier ítem: cae el final de
                   esta etapa y `_paso_0`).
    · `_paso_3` ← quitar `.with_for_update()` del helper, o llamarlo DESPUÉS de
                   `_resolver_ri` (nacen 2 filas del ítem); o volver a `async def
                   upload_foto` (el servidor no atiende nada mientras la subida espera).
    · `_paso_4` ← quitar `_descartar_archivo(fpath)` del `except OSError` (queda el
                   archivo a medias); relanzar el OSError sin HTTPException (500 sin
                   `detail`); o quitar el try/except del `db.commit()` (queda la foto
                   completa sin fila).
    La lista ejecutable de estas mutaciones vive en portal/tests/mutaciones/bodega.py.

No escribe en uploads/bodega: durante la corrida el router apunta a una carpeta temporal.
LIMPIA por MARCA al empezar y al terminar.

Corre con:  ./venv/bin/python -m pytest routers/tests/test_bodega_fotos_candado.py -q
(con uvicorn detenido)
"""
import errno
import os
import shutil
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from database import SessionLocal, get_db  # noqa: E402
from auth import get_current_user  # noqa: E402
from models.models import (  # noqa: E402
    Embarque, EmbarqueItem, RecepcionEmbarque, RecepcionEmbarqueItem, RecepcionItemFoto,
)
from routers.bodega import router as bodega_router  # noqa: E402

# Sin guiones bajos: `_` es comodín de un carácter en LIKE.
MARK = "ZZTESTFOTOSCANDADOZZ"
JPG = b"\xff\xd8\xff\xdb" + b"\x00" * 32
# Margen para que la subida alcance el candado antes de mirar si quedó esperando.
ESPERA_BLOQUEO = 1.0
# Si algo se cruza mal, InnoDB corta por innodb_lock_wait_timeout (50 s por defecto):
# el tope es mayor para que una mutación dé rojo y no un cuelgue.
TIMEOUT_HILO = 70

app = FastAPI()
app.include_router(bodega_router, prefix="/api")
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=1, empresa="mineria", email="bodega@test.invalid")
# `raise_server_exceptions=False`: un commit que falla tiene que verse como el 500 que
# recibe la pantalla, no como una excepción dentro de la prueba.
cliente = TestClient(app, raise_server_exceptions=False)

# Las etapas se llaman `_paso_*` y NO `test_*` a propósito: `check()` acumula en
# `_fails` en vez de lanzar, así que si pytest las recolectara por separado cada una
# sería un verde incondicional. La única función recolectable es la de más abajo.
_fails: list = []
_carpeta = ""          # la carpeta temporal que hace de uploads/bodega durante la corrida


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _archivos():
    return sorted(os.listdir(_carpeta))


def _subir(c, d, ei_key, nombre="foto.jpg"):
    """POST de una foto al ítem `ei_key`, como lo manda la pantalla (id + embarque_item_id)."""
    return c.post(f"/api/bodega/recepciones/{d['rec']}/items/{d[ei_key]}/foto",
                  params={"embarque_item_id": d[ei_key]},
                  files={"file": (nombre, JPG, "image/jpeg")})


def _borrar(c, d, ri_key, foto_id):
    return c.delete(f"/api/bodega/recepciones/{d['rec']}/items/{d[ri_key]}/foto/{foto_id}")


def _en_hilo(fn):
    """Corre fn() en un hilo y devuelve (hilo, resultado). El resultado trae 'out' o 'exc'."""
    res = {}

    def _run():
        try:
            res["out"] = fn()
        except BaseException as e:  # noqa: BLE001  — un error de MySQL también viaja como excepción
            res["exc"] = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t, res


def _limpiar(db):
    """Borra las filas de prueba (hijos primero), por MARCA: nunca por ids en memoria."""
    db.rollback()
    for sql in (
        "DELETE f FROM recepcion_item_fotos f "
        "JOIN recepcion_embarque_items ri ON ri.id = f.recepcion_item_id "
        "JOIN recepciones_embarque r ON r.id = ri.recepcion_id "
        "WHERE r.observacion_general LIKE :m",
        "DELETE ri FROM recepcion_embarque_items ri "
        "JOIN recepciones_embarque r ON r.id = ri.recepcion_id "
        "WHERE r.observacion_general LIKE :m",
        "DELETE FROM recepciones_embarque WHERE observacion_general LIKE :m",
        "DELETE FROM embarque_items WHERE embarque_id IN "
        "(SELECT id FROM embarques WHERE numero LIKE :m)",
        "DELETE FROM embarques WHERE numero LIKE :m",
    ):
        db.execute(text(sql), {"m": f"%{MARK}%"})
        db.commit()


def _crear_datos(db):
    """Un embarque con su recepción abierta, cinco ítems de embarque y dos filas de
    recepción: `ri_emp` (empezada, sin estado) y `ri_dan` (guardada como dañada)."""
    emb = Embarque(numero=f"{MARK}-EMB", estado="en_recepcion")
    db.add(emb)
    db.flush()
    rec = RecepcionEmbarque(embarque_id=emb.id, estado="abierta", observacion_general=MARK)
    db.add(rec)
    items = {k: EmbarqueItem(embarque_id=emb.id, item_cotizacion_id=None)
             for k in ("ei_emp", "ei_libre", "ei_dan", "ei_carrera", "ei_falla")}
    db.add_all(items.values())
    db.flush()
    ri_emp = RecepcionEmbarqueItem(recepcion_id=rec.id, embarque_item_id=items["ei_emp"].id,
                                   qty_recibida=1)
    ri_dan = RecepcionEmbarqueItem(recepcion_id=rec.id, embarque_item_id=items["ei_dan"].id,
                                   qty_recibida=1, qty_danada=1,
                                   estado_recepcion="danado_utilizable")
    db.add_all([ri_emp, ri_dan])
    # Confirmado ANTES de llamar a un endpoint: si no, su FOR UPDATE espera a esta sesión.
    db.commit()
    d = {k: v.id for k, v in items.items()}
    d.update(rec=rec.id, ri_emp=ri_emp.id, ri_dan=ri_dan.id)
    return d


def _fotos_de(db, ri_id):
    db.rollback()
    return db.query(RecepcionItemFoto).filter(RecepcionItemFoto.recepcion_item_id == ri_id).count()


def _filas_del_item(db, d, ei_key):
    db.rollback()
    return db.query(RecepcionEmbarqueItem).filter(
        RecepcionEmbarqueItem.recepcion_id == d["rec"],
        RecepcionEmbarqueItem.embarque_item_id == d[ei_key]).all()


def _poner_estado(db, d, estado):
    db.execute(text("UPDATE recepciones_embarque SET estado = :e WHERE id = :r"),
               {"e": estado, "r": d["rec"]})
    db.commit()


def _paso_0_abierta_sube_y_borra(db, d):
    """Contrapeso: con la recepción abierta, subir y borrar funcionan. Sin esta etapa,
    los 400 y el 409 de las siguientes pasarían también con un helper o una regla que
    rechazan siempre."""
    r = _subir(cliente, d, "ei_emp")
    check("abierta: subir responde 201", r.status_code == 201, f"{r.status_code} {r.text[:120]}")
    check("y el archivo queda en la carpeta", len(_archivos()) == 1, _archivos())
    if r.status_code == 201:
        r = _borrar(cliente, d, "ri_emp", r.json()["id"])
        check("abierta: borrar una foto de un ítem no dañado responde 200",
              r.status_code == 200, f"{r.status_code} {r.text[:120]}")
        check("y el archivo sale del disco", _archivos() == [], _archivos())


def _paso_1_cerrada_es_solo_consulta(db, d):
    """Con la recepción cerrada no se sube ni se borra; se sigue consultando."""
    foto = _subir(cliente, d, "ei_emp").json()["id"]
    antes = _archivos()
    _poner_estado(db, d, "cerrada")
    try:
        r = _subir(cliente, d, "ei_emp")
        check("cerrada: subir responde 400", r.status_code == 400, f"{r.status_code} {r.text[:120]}")
        check("y el mensaje dice que está cerrada", "cerrada" in r.text, r.text[:120])
        check("no gana fotos ni archivos",
              _fotos_de(db, d["ri_emp"]) == 1 and _archivos() == antes, _archivos())
        r = _subir(cliente, d, "ei_libre")
        check("cerrada: subir a un ítem no empezado responde 400", r.status_code == 400, r.status_code)
        check("y NO nace la fila del ítem dentro de una recepción cerrada",
              _filas_del_item(db, d, "ei_libre") == [])
        r = _borrar(cliente, d, "ri_emp", foto)
        # 400 y no 404: con 404 la pantalla saca la foto de la lista («ya no existe»).
        check("cerrada: borrar responde 400", r.status_code == 400, f"{r.status_code} {r.text[:120]}")
        check("la foto sigue en la base y en el disco",
              _fotos_de(db, d["ri_emp"]) == 1 and _archivos() == antes, _archivos())
        r = cliente.get(f"/api/bodega/fotos/{foto}")
        check("y se sigue sirviendo", r.status_code == 200, r.status_code)
    finally:
        _poner_estado(db, d, "abierta")


def _paso_2_el_danado_no_queda_sin_foto(db, d):
    """La última foto de un ítem guardado como dañado no se borra (409); con otro estado
    guardado, la misma foto sí se borra."""
    f1 = _subir(cliente, d, "ei_dan").json()["id"]
    f2 = _subir(cliente, d, "ei_dan").json()["id"]
    r = _borrar(cliente, d, "ri_dan", f1)
    check("dañado con dos fotos: borrar una responde 200", r.status_code == 200, r.status_code)
    r = _borrar(cliente, d, "ri_dan", f2)
    check("dañado: borrar la ÚLTIMA responde 409", r.status_code == 409, f"{r.status_code} {r.text[:120]}")
    check("y el mensaje explica la salida", "dañado" in r.text and "reemplazo" in r.text, r.text[:200])
    check("la última foto sigue", _fotos_de(db, d["ri_dan"]) == 1 and len(_archivos()) >= 1)
    db.execute(text("UPDATE recepcion_embarque_items SET estado_recepcion = 'completo' "
                    "WHERE id = :i"), {"i": d["ri_dan"]})
    db.commit()
    r = _borrar(cliente, d, "ri_dan", f2)
    check("guardado como completo, la misma foto sí se borra (200)", r.status_code == 200, r.status_code)


def _paso_3_la_subida_espera_el_candado(db, d):
    """DOS sesiones MySQL reales. La segunda emula a `marcar_item` a mitad de transacción:
    tiene la recepción tomada y la fila del ítem insertada sin confirmar.

    «La subida sigue esperando» no discrimina por sí solo (sin candado también espera, en
    la llave foránea del INSERT): discriminan el conteo de filas y el GET atendido."""
    otra = SessionLocal()
    foto_testigo = _subir(cliente, d, "ei_emp").json()["id"]
    soltado = False
    # `with`: UN solo event loop para las dos peticiones, como en uvicorn. Sin él cada
    # petición trae su propio loop y un `async def` bloqueado no se nota.
    with TestClient(app, raise_server_exceptions=False) as c:
        try:
            otra.execute(text("SELECT id FROM recepciones_embarque WHERE id = :r FOR UPDATE"),
                         {"r": d["rec"]})
            otra.execute(text("INSERT INTO recepcion_embarque_items "
                              "(recepcion_id, embarque_item_id, qty_recibida) VALUES (:r, :e, 1)"),
                         {"r": d["rec"], "e": d["ei_carrera"]})
            hilo, res = _en_hilo(lambda: _subir(c, d, "ei_carrera"))
            time.sleep(ESPERA_BLOQUEO)
            check("la subida queda esperando mientras otro tiene la recepción", hilo.is_alive())
            h2, r2 = _en_hilo(lambda: c.get(f"/api/bodega/fotos/{foto_testigo}"))
            h2.join(5)
            check("mientras espera, el servidor sigue atendiendo otras peticiones",
                  not h2.is_alive() and getattr(r2.get("out"), "status_code", None) == 200,
                  r2.get("exc") or getattr(r2.get("out"), "status_code", "sin respuesta"))
            otra.commit()
            soltado = True
            hilo.join(TIMEOUT_HILO)
            h2.join(TIMEOUT_HILO)
            r = res.get("out")
            check("al soltarse el candado, la subida responde 201",
                  r is not None and r.status_code == 201,
                  res.get("exc") or ((r.status_code, r.text[:120]) if r is not None else "sin respuesta"))
            filas = _filas_del_item(db, d, "ei_carrera")
            check("y hay UNA sola fila del ítem (no nació una segunda)", len(filas) == 1,
                  [f.id for f in filas])
            if r is not None and r.status_code == 201 and len(filas) == 1:
                check("la foto cuelga de esa fila", r.json()["recepcion_item_id"] == filas[0].id,
                      (r.json()["recepcion_item_id"], filas[0].id))
        finally:
            if not soltado:
                otra.rollback()
            otra.close()


class _CommitQueFalla:
    """Sesión envuelta cuyo `commit()` falla como cuando se cae la conexión: deshace de
    verdad (nada queda escrito) y levanta el error."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, nombre):
        return getattr(self._real, nombre)

    def commit(self):
        self._real.rollback()
        orig = Exception()
        orig.args = (2013, "Lost connection to MySQL server during query")
        raise OperationalError("COMMIT", {}, orig)


def _paso_4_archivo_y_fila_van_juntos(db, d):
    """Si falla la escritura o el commit, no queda archivo suelto ni fila a medias."""
    antes, fotos_antes = _archivos(), _fotos_de(db, d["ri_emp"])

    # (a) Falla la ESCRITURA a mitad (disco lleno), sobre un ítem no empezado.
    def _copia_que_falla(src, dst, *a, **k):
        dst.write(b"xx")
        dst.flush()
        raise OSError(errno.ENOSPC, "No space left on device")

    with mock.patch("routers.bodega.shutil.copyfileobj", side_effect=_copia_que_falla):
        r = _subir(cliente, d, "ei_falla")
    detalle = (r.json().get("detail")
               if r.headers.get("content-type", "").startswith("application/json") else None)
    check("escritura fallida: responde 500 con un mensaje para el operador",
          r.status_code == 500 and isinstance(detalle, str)
          and detalle.startswith("No se pudo guardar la foto"), (r.status_code, r.text[:120]))
    check("y no queda el archivo a medias", _archivos() == antes, _archivos())
    check("ni la fila del ítem recién creada", _filas_del_item(db, d, "ei_falla") == [])

    # (b) Falla el COMMIT con el archivo ya escrito.
    def _get_db_que_falla():
        s = SessionLocal()
        try:
            yield _CommitQueFalla(s)
        finally:
            s.close()

    app.dependency_overrides[get_db] = _get_db_que_falla
    try:
        r = _subir(cliente, d, "ei_emp")
    finally:
        app.dependency_overrides.pop(get_db, None)
    check("commit fallido: responde 500", r.status_code == 500, r.status_code)
    check("y no queda la foto completa sin fila en el disco", _archivos() == antes, _archivos())
    check("ni una fila de foto", _fotos_de(db, d["ri_emp"]) == fotos_antes)
    # Contrapeso: la misma subida, sin el fallo, funciona.
    r = _subir(cliente, d, "ei_emp")
    check("sin el fallo, la misma subida responde 201", r.status_code == 201, r.status_code)


def run():
    global _carpeta
    _fails.clear()
    db = SessionLocal()
    _carpeta = tempfile.mkdtemp(prefix="fotos-candado-")
    try:
        _limpiar(db)
        # El router escribe y borra en la carpeta temporal: «no quedó archivo» se afirma
        # comparando su listado exacto, y una corrida que muere a mitad no ensucia
        # uploads/bodega.
        with mock.patch.multiple("routers.bodega", UPLOAD_DIR=_carpeta,
                                 _FOTOS_DIR=os.path.abspath(_carpeta)):
            d = _crear_datos(db)
            _paso_0_abierta_sube_y_borra(db, d)
            _paso_1_cerrada_es_solo_consulta(db, d)
            _paso_2_el_danado_no_queda_sin_foto(db, d)
            _paso_3_la_subida_espera_el_candado(db, d)
            _paso_4_archivo_y_fila_van_juntos(db, d)
    finally:
        _limpiar(db)
        db.close()
        shutil.rmtree(_carpeta, ignore_errors=True)
        print("Cleanup OK")

    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_bodega_fotos_candado():
    run()


if __name__ == "__main__":
    run()
