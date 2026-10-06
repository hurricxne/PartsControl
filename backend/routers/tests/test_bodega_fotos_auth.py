"""Las fotos de recepción: QUIÉN puede — sesión, marca, confinamiento y pertenencia.

(CUÁNDO se puede subir o borrar —recepción abierta, última foto de un ítem dañado,
candado, limpieza de archivos— está en test_bodega_fotos_candado.py.)

QUÉ PROTEGE
    `uploads/bodega` no se publica como carpeta estática: las fotos se piden por id a
    `serve_foto`, con sesión de Grupo AM. Servida sin sesión, cualquiera en internet
    puede enumerar fotos de la mercadería de un cliente.

    `delete_foto` comprueba que la foto sea del ítem y de la recepción de la URL: si
    filtrara solo por `foto_id`, cualquier sesión borraría cualquier foto acertando un
    entero correlativo, y esas fotos son la evidencia que `marcar_item` exige para
    guardar un ítem dañado.

SONDAS DE PODER DISCRIMINANTE — qué hay que quitar para que cada etapa falle
    · `_paso_1`  ← quitar de `serve_foto` las DOS capas de sesión: el `require_empresa`
                    de la ruta y el `Depends(get_current_user)` del parámetro. Con una
                    sola quitada la etapa sigue verde, porque cualquiera de las dos corta
                    al anónimo.
    · `_paso_2`  ← devolver 403 en vez de 404 para un id inexistente.
    · `_paso_4`  ← quitar `if not full.startswith(_FOTOS_DIR + os.sep)` de
                    `_ruta_confinada_foto`.
                    OJO con el payload: "/uploads/bodega/../../../../etc/passwd" NO sirve
                    para esto. `os.path.basename()` lo reduce a "passwd" ANTES de la
                    comprobación de prefijo, así que resuelve dentro del directorio y da
                    404 con y sin esa línea. El que SÍ la ejercita es
                    "/uploads/bodega/..", cuyo basename es ".." y sale del directorio.
                    Por eso acá se exige **400 exacto**, no "400 o 404".
    · `_paso_5`  ← quitar `require_empresa` de `serve_foto`, de `delete_foto` o de
                    `upload_foto`: las tres se prueban por separado.
    · `_paso_6a` ← en `delete_foto`, quitar `RecepcionEmbarqueItem.recepcion_id ==
                    recepcion_id` del filtro del ítem de la foto.
    · `_paso_6b` ← en `delete_foto`, quitar el `if ri_id not in (ri.id,
                    ri.embarque_item_id)`.
                    6a y 6b varían UNA sola dimensión cada uno (la recepción; el ítem).
                    Probadas juntas, cualquiera de las dos condiciones que sobreviva
                    bloquea el caso y la otra queda sin sonda: el agujero queda justo en
                    la costura.
    · `_paso_6d` ← quitar `RecepcionEmbarqueItem.recepcion_id == recepcion_id` de
                    cualquiera de las tres búsquedas de `_resolver_ri`, o
                    `EmbarqueItem.embarque_id == rec.embarque_id` de `upload_foto`.
    · `_paso_7`  ← en `delete_foto`, reducir esa comprobación a `ri_id != ri.id`.
    · `_paso_8`  ← que `delete_foto` arme la ruta con `url_foto.lstrip("/")` en vez de
                    `_ruta_confinada_foto`.
    · `_paso_9`  ← que `_resolver_ri` ignore el `embarque_item_id` explícito (la foto se
                    va al ítem equivocado), o que `upload_foto` deje de validar la
                    pertenencia antes de insertar (revienta con un 500 por llave foránea
                    en vez de un 404 entendible).
    · `_paso_10` ← quitar `recepcion_id` o `total_items` de la respuesta del historial:
                    la tarjeta de una recepción cerrada no abre nada y sus fotos quedan
                    inalcanzables desde la pantalla.
    · `_paso_11` ← quitar `require_empresa` de `fotos_de_item`, o agregarle un DELETE.
    · `_paso_12` ← que `upload_foto` nombre el archivo con `{ri.id}_{segundos}` en vez
                    de uuid4: dos fotos del mismo ítem en el mismo segundo se pisan.

LIMPIA TODO al final: filas marcadas con MARK y los archivos temporales en disco.

Corre con:  ./venv/bin/python -m pytest routers/tests/test_bodega_fotos_auth.py -q
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from database import SessionLocal  # noqa: E402
from auth import get_current_user  # noqa: E402
from models.models import (  # noqa: E402
    Embarque, EmbarqueItem, RecepcionEmbarque, RecepcionEmbarqueItem, RecepcionItemFoto,
)
from routers.bodega import router as bodega_router, UPLOAD_DIR  # noqa: E402

# Sin guiones bajos: en SQL `_` es comodín de UN carácter dentro de LIKE, así que un
# MARK con `_` haría que el DELETE de limpieza borrara filas que solo se parecen.
MARK = "ZZTESTFOTOSAUTHZZ"
ARCHIVO = f"{MARK}-foto.jpg"     # nombre válido para la regex del endpoint

# App SIN sesión: reproduce al visitante anónimo tal cual llega de internet.
app_anonima = FastAPI()
app_anonima.include_router(bodega_router, prefix="/api")
cliente_anonimo = TestClient(app_anonima)

# App CON sesión de la marca correcta: el camino legítimo del operador de bodega.
app_mineria = FastAPI()
app_mineria.include_router(bodega_router, prefix="/api")
app_mineria.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=1, empresa="mineria", email="bodega@test.invalid")
cliente = TestClient(app_mineria)

# App con sesión de la OTRA marca: MonzaParts no tiene por qué ver fotos de Grupo AM.
app_automotriz = FastAPI()
app_automotriz.include_router(bodega_router, prefix="/api")
app_automotriz.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=2, empresa="automotriz", email="monza@test.invalid")
cliente_otra_marca = TestClient(app_automotriz)

# Las etapas se llaman `_paso_*` y NO `test_*` a propósito: `check()` acumula en
# `_fails` en vez de lanzar, así que si pytest las recolectara por separado cada una
# sería un verde incondicional. La única función recolectable es la de más abajo.
_fails: list = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _limpiar(db):
    """Borra las filas de prueba (hijos primero) y los archivos temporales."""
    db.execute(text(
        "DELETE f FROM recepcion_item_fotos f "
        "JOIN recepcion_embarque_items ri ON ri.id = f.recepcion_item_id "
        "JOIN recepciones_embarque r ON r.id = ri.recepcion_id "
        "WHERE r.observacion_general LIKE :m"), {"m": f"%{MARK}%"})
    db.execute(text(
        "DELETE ri FROM recepcion_embarque_items ri "
        "JOIN recepciones_embarque r ON r.id = ri.recepcion_id "
        "WHERE r.observacion_general LIKE :m"), {"m": f"%{MARK}%"})
    db.execute(text(
        "DELETE FROM recepciones_embarque WHERE observacion_general LIKE :m"),
        {"m": f"%{MARK}%"})
    db.commit()
    db.execute(text("DELETE FROM embarque_items WHERE embarque_id IN "
                    "(SELECT id FROM embarques WHERE numero LIKE :m)"), {"m": f"%{MARK}%"})
    db.execute(text("DELETE FROM embarques WHERE numero LIKE :m"), {"m": f"%{MARK}%"})
    db.commit()
    for nombre in (ARCHIVO, f"{MARK}-otra.jpg", f"{MARK}-noemp.jpg"):
        ruta = os.path.join(UPLOAD_DIR, nombre)
        if os.path.exists(ruta):
            os.remove(ruta)


def _escribir_jpg(nombre):
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    with open(os.path.join(UPLOAD_DIR, nombre), "wb") as f:
        # JPEG mínimo: basta con que exista y que mimetypes lo reconozca por extensión.
        f.write(b"\xff\xd8\xff\xdb" + b"\x00" * 32)


def _crear_datos(db):
    """DOS recepciones. La segunda existe solo para probar los cruces entre recepciones."""
    # Cada recepción cuelga de su PROPIO embarque, como en producción: la validación de
    # pertenencia de `upload_foto` compara contra `recepcion.embarque_id`, y con
    # `embarque_id=None` esa comparación pasa a ser `IS NULL` y su resultado depende de
    # filas ajenas a la prueba.
    emb = Embarque(numero=f"{MARK}-EMB", estado="en_bodega")
    emb_b = Embarque(numero=f"{MARK}-EMB-B", estado="en_bodega")
    db.add_all([emb, emb_b])
    db.flush()
    rec_a = RecepcionEmbarque(embarque_id=emb.id, estado="abierta", observacion_general=MARK)
    rec_b = RecepcionEmbarque(embarque_id=emb_b.id, estado="abierta",
                              observacion_general=f"{MARK} otra")
    db.add_all([rec_a, rec_b])
    db.flush()
    ei_a = EmbarqueItem(embarque_id=emb.id, item_cotizacion_id=None)
    db.add(ei_a)
    db.flush()
    ri_a = RecepcionEmbarqueItem(recepcion_id=rec_a.id, embarque_item_id=ei_a.id, qty_recibida=1)
    # Segundo ítem de la MISMA recepción A: permite aislar la condición del ri_id sin
    # cambiar también la recepción (ver `_paso_6a` / `_paso_6b`).
    ri_a2 = RecepcionEmbarqueItem(recepcion_id=rec_a.id, embarque_item_id=None, qty_recibida=1)
    ri_b = RecepcionEmbarqueItem(recepcion_id=rec_b.id, embarque_item_id=None, qty_recibida=1)
    # Ítem "no empezado": la pantalla manda el id del EmbarqueItem, no el del
    # RecepcionEmbarqueItem. Es el camino que produjo la regresión del borrado fantasma.
    emb_item = EmbarqueItem(embarque_id=emb.id, item_cotizacion_id=None)
    db.add(emb_item)
    db.flush()
    ri_c = RecepcionEmbarqueItem(recepcion_id=rec_a.id, embarque_item_id=emb_item.id,
                                 qty_recibida=1)
    db.add_all([ri_a, ri_a2, ri_b, ri_c])
    db.flush()

    _escribir_jpg(ARCHIVO)
    _escribir_jpg(f"{MARK}-otra.jpg")

    foto_ok = RecepcionItemFoto(recepcion_item_id=ri_a.id,
                                url_foto=f"/uploads/bodega/{ARCHIVO}")
    # Fila envenenada 1: basename la reduce a "passwd" → resuelve DENTRO del directorio
    # y muere por archivo inexistente. NO ejercita la comprobación de prefijo.
    foto_basename = RecepcionItemFoto(
        recepcion_item_id=ri_a.id, url_foto="/uploads/bodega/../../../../etc/passwd")
    # Fila envenenada 2: basename la deja en ".." → sale del directorio. ESTA es la que
    # ejercita `startswith(_FOTOS_DIR + os.sep)` y debe dar 400 exacto.
    foto_sale = RecepcionItemFoto(recepcion_item_id=ri_a.id, url_foto="/uploads/bodega/..")
    # Foto de la OTRA recepción, para el borrado cruzado.
    foto_b = RecepcionItemFoto(recepcion_item_id=ri_b.id,
                               url_foto=f"/uploads/bodega/{MARK}-otra.jpg")
    _escribir_jpg(f"{MARK}-noemp.jpg")
    foto_c = RecepcionItemFoto(recepcion_item_id=ri_c.id,
                               url_foto=f"/uploads/bodega/{MARK}-noemp.jpg")
    db.add_all([foto_ok, foto_basename, foto_sale, foto_b, foto_c])
    db.commit()
    return {
        "rec_a": rec_a.id, "ri_a": ri_a.id, "ri_a2": ri_a2.id,
        "rec_b": rec_b.id, "ri_b": ri_b.id,
        "emb": emb.id, "emb_item": emb_item.id, "ri_c": ri_c.id,
        "emb_item_ri_a": ei_a.id,
        "ok": foto_ok.id, "basename": foto_basename.id,
        "sale": foto_sale.id, "b": foto_b.id, "c": foto_c.id,
    }


def _paso_1_sin_sesion_no_entrega(d):
    r = cliente_anonimo.get(f"/api/bodega/fotos/{d['ok']}")
    check("sin sesión no entrega la foto", r.status_code in (401, 403), r.status_code)
    check("sin sesión no devuelve bytes de imagen",
          not r.headers.get("content-type", "").startswith("image/"),
          r.headers.get("content-type"))


def _paso_2_id_inexistente_es_404(d):
    # 404 y no 403: un 403 confirmaría que ese id existe.
    r = cliente.get("/api/bodega/fotos/999999999")
    check("id inexistente responde 404", r.status_code == 404, r.status_code)


def _paso_3_con_sesion_entrega(d):
    r = cliente.get(f"/api/bodega/fotos/{d['ok']}")
    check("con sesión entrega la foto", r.status_code == 200, r.text[:200])
    check("el content-type es de imagen",
          r.headers.get("content-type", "").startswith("image/"),
          r.headers.get("content-type"))
    cc = (r.headers.get("cache-control") or "").lower()
    check("no se cachea en proxies compartidos", "private" in cc, cc)
    # `private` NO basta: la caché del navegador se indexa por URL, no por el header
    # Authorization. En el computador compartido de bodega, un `max-age` deja que el
    # siguiente usuario vea la imagen sin volver a pasar por el candado. Exigir solo
    # "private" dejaría pasar "private, max-age=86400".
    check("y tampoco se guarda en el navegador (no-store)", "no-store" in cc, cc)
    check("y no trae un max-age que reviva la caché", "max-age" not in cc, cc)
    check("lleva nosniff",
          (r.headers.get("x-content-type-options") or "") == "nosniff",
          r.headers.get("x-content-type-options"))
    check("se sirve inline, no como descarga",
          "attachment" not in (r.headers.get("content-disposition") or ""),
          r.headers.get("content-disposition"))


def _paso_4_confinamiento(d):
    # (a) El payload clásico: basename lo neutraliza antes del prefijo. Da 404.
    r = cliente.get(f"/api/bodega/fotos/{d['basename']}")
    check("una ruta con ../ no lee fuera del directorio", r.status_code in (400, 404),
          r.status_code)
    check("y no devuelve el contenido de /etc/passwd", b"root:" not in r.content,
          r.content[:80])
    # (b) El payload que SÍ ejercita la comprobación de prefijo. 400 EXACTO: si alguien
    #     borra el `startswith`, esto pasa a 404 y la etapa falla.
    r = cliente.get(f"/api/bodega/fotos/{d['sale']}")
    check("una ruta que sale del directorio se rechaza con 400 exacto",
          r.status_code == 400, r.status_code)


def _paso_5_otra_marca_no_puede_nada(d):
    """Las rutas que ESCRIBEN fotos llevan el candado de marca, no solo la de lectura.

    Con `require_empresa` únicamente en `serve_foto`, una sesión de MonzaParts recibe 403
    al PEDIR la foto, pero su DELETE responde 200 y borra la fila y el archivo, y su POST
    responde 201 y le adjunta una foto a una recepción de Grupo AM. Esas fotos no son
    decorativas: son la evidencia que `marcar_item` exige para guardar un ítem dañado y
    el respaldo del reclamo al proveedor.
    """
    # Leer
    r = cliente_otra_marca.get(f"/api/bodega/fotos/{d['ok']}")
    check("otra marca: GET de la foto -> 403", r.status_code == 403, r.status_code)
    check("otra marca: y no recibe bytes de imagen",
          not r.headers.get("content-type", "").startswith("image/"),
          r.headers.get("content-type"))

    # Borrar
    r = cliente_otra_marca.delete(
        f"/api/bodega/recepciones/{d['rec_a']}/items/{d['ri_a']}/foto/{d['ok']}")
    check("otra marca: DELETE de la foto -> 403", r.status_code == 403, r.status_code)
    sigue = db_global.query(RecepcionItemFoto).filter(
        RecepcionItemFoto.id == d["ok"]).first()
    check("otra marca: la foto sigue existiendo tras el DELETE", sigue is not None)

    # Subir
    r = cliente_otra_marca.post(
        f"/api/bodega/recepciones/{d['rec_a']}/items/{d['ri_a']}/foto",
        files={"file": ("intruso.jpg", b"\xff\xd8\xff\xdb" + b"\x00" * 8, "image/jpeg")})
    check("otra marca: POST de foto -> 403", r.status_code == 403, r.status_code)


def _paso_6a_el_ri_id_debe_ser_de_esa_recepcion(d):
    """MISMO ri_id, recepción EQUIVOCADA.

    Solo varía la recepción. `delete_foto` parte de la foto y exige que su ítem sea de la
    recepción de la URL: sin esa condición el ítem se encuentra igual y esto borra.
    """
    r = cliente.delete(
        f"/api/bodega/recepciones/{d['rec_b']}/items/{d['ri_a']}/foto/{d['ok']}")
    check("ri_id correcto pero recepción ajena -> 404", r.status_code == 404, r.status_code)
    db_global.expire_all()
    sigue = db_global.query(RecepcionItemFoto).filter(
        RecepcionItemFoto.id == d["ok"]).first()
    check("y la foto NO se borró", sigue is not None)


def _paso_6b_la_foto_debe_ser_de_ese_item(d):
    """MISMA recepción, ítem EQUIVOCADO.

    Solo varía el ítem, y los dos son de la recepción A. `delete_foto` exige que el
    `ri_id` de la URL designe al ítem de la foto: sin esa condición esto borra.
    """
    r = cliente.delete(
        f"/api/bodega/recepciones/{d['rec_a']}/items/{d['ri_a2']}/foto/{d['ok']}")
    check("ítem hermano de la MISMA recepción -> 404", r.status_code == 404, r.status_code)
    db_global.expire_all()
    sigue = db_global.query(RecepcionItemFoto).filter(
        RecepcionItemFoto.id == d["ok"]).first()
    check("y la foto NO se borró", sigue is not None)


def _paso_6c_el_camino_legitimo_si_borra(d):
    """Contrapeso de 6a y 6b: con los ids correctos SÍ borra.

    Sin esta etapa, los dos pasos anteriores pasarían también si `delete_foto`
    devolviera 404 siempre.
    """
    r = cliente.delete(
        f"/api/bodega/recepciones/{d['rec_b']}/items/{d['ri_b']}/foto/{d['b']}")
    check("los ids correctos responden 200", r.status_code == 200, r.status_code)
    db_global.expire_all()
    ya_no = db_global.query(RecepcionItemFoto).filter(
        RecepcionItemFoto.id == d["b"]).first()
    check("la foto propia SÍ se borró", ya_no is None)
    check("y su archivo salió del disco",
          not os.path.isfile(os.path.join(UPLOAD_DIR, f"{MARK}-otra.jpg")))


def _fotos_del_item(ri_id):
    db_global.expire_all()
    return db_global.query(RecepcionItemFoto).filter(
        RecepcionItemFoto.recepcion_item_id == ri_id).count()


def _paso_6d_la_subida_no_cruza_recepciones(d):
    """Por la URL de la recepción B no se adjunta una foto a un ítem de la recepción A.

    Tres formas de nombrar el ítem ajeno, una por cada búsqueda de `_resolver_ri`: su id
    de recepción, su `embarque_item_id` explícito y su id de EmbarqueItem en la URL.
    """
    antes = _fotos_del_item(d["ri_a"])
    jpg = {"file": ("cruce.jpg", b"\xff\xd8\xff\xdb" + b"\x00" * 8, "image/jpeg")}
    base = f"/api/bodega/recepciones/{d['rec_b']}/items"
    try:
        r = cliente.post(f"{base}/{d['ri_a']}/foto", files=jpg)
        check("subida cruzada por id del ítem de recepción -> 404", r.status_code == 404,
              f"{r.status_code} {r.text[:120]}")
        r = cliente.post(f"{base}/{d['ri_a']}/foto", files=jpg,
                         params={"embarque_item_id": d["emb_item_ri_a"]})
        check("subida cruzada con embarque_item_id explícito -> 404", r.status_code == 404,
              f"{r.status_code} {r.text[:120]}")
        # Acá no se exige el código: si el entero coincidiera con el id de un ítem propio
        # de B, adjuntarle la foto a ESE ítem sería legítimo. Lo que no puede pasar es
        # que la gane el ítem de A.
        cliente.post(f"{base}/{d['emb_item_ri_a']}/foto", files=jpg)
        check("el ítem de la recepción A no gana fotos por la URL de B",
              _fotos_del_item(d["ri_a"]) == antes, _fotos_del_item(d["ri_a"]))
        nacidas = db_global.query(RecepcionEmbarqueItem).filter(
            RecepcionEmbarqueItem.recepcion_id == d["rec_b"],
            RecepcionEmbarqueItem.embarque_item_id == d["emb_item_ri_a"]).count()
        check("y en B no nace una fila para un ítem de otro embarque", nacidas == 0, nacidas)
    finally:
        # Con el código sano no hay nada que limpiar. Si una mutación dejó pasar una
        # subida, se borran su archivo y su fila para no ensuciar las etapas siguientes.
        db_global.expire_all()
        semilla = {d["ok"], d["basename"], d["sale"], d["b"], d["c"]}
        sobrantes = (
            db_global.query(RecepcionItemFoto)
            .join(RecepcionEmbarqueItem,
                  RecepcionEmbarqueItem.id == RecepcionItemFoto.recepcion_item_id)
            .filter(RecepcionEmbarqueItem.recepcion_id.in_([d["rec_a"], d["rec_b"]]))
            .all()
        )
        for foto in sobrantes:
            if foto.id in semilla:
                continue
            ruta = os.path.join(UPLOAD_DIR, os.path.basename(foto.url_foto))
            if os.path.isfile(ruta):
                os.remove(ruta)
            db_global.delete(foto)
        db_global.commit()


def _paso_7_el_id_del_embarque_item_tambien_resuelve(d):
    """Para un ítem que todavía no se empieza a recepcionar, el id que la pantalla tiene
    a mano es el del EmbarqueItem, no el del ítem de recepción.

    Si `delete_foto` solo aceptara `RecepcionEmbarqueItem.id`, ese borrado respondería
    404: la pantalla sacaría la foto de la lista («ya no existe») con la foto todavía en
    la base y en el disco, y un ítem dañado quedaría cubierto por una foto que el
    operador cree borrada.
    """
    r = cliente.delete(
        f"/api/bodega/recepciones/{d['rec_a']}/items/{d['emb_item']}/foto/{d['c']}")
    check("el id del EmbarqueItem también resuelve el ítem", r.status_code == 200,
          r.status_code)
    db_global.expire_all()
    ya_no = db_global.query(RecepcionItemFoto).filter(
        RecepcionItemFoto.id == d["c"]).first()
    check("y la foto se borró de verdad (no fue un borrado fantasma)", ya_no is None)
    check("y su archivo salió del disco",
          not os.path.isfile(os.path.join(UPLOAD_DIR, f"{MARK}-noemp.jpg")))



def _paso_8_el_borrado_tambien_confina_la_ruta(db, d):
    """El borrado usa la MISMA ruta confinada que la lectura.

    Si armara la ruta con `url_foto.lstrip("/")`, relativa al directorio de trabajo, una
    fila manipulada borraría un archivo de FUERA de uploads/bodega.

    OJO CON EL PAYLOAD: "/uploads/bodega/.." NO discrimina. Con `lstrip("/")` queda en
    "uploads/bodega/..", o sea un DIRECTORIO: `os.path.isfile` da False y no se borra
    nada con o sin el defecto. El payload que sí ejercita la diferencia apunta a un
    ARCHIVO REAL fuera de la carpeta:
        lstrip("/")            -> uploads/bodega/../TESTIGO  -> uploads/TESTIGO  (¡existe!)
        _ruta_confinada_foto   -> basename = TESTIGO         -> uploads/bodega/TESTIGO (no existe)
    """
    testigo_rel = os.path.join(UPLOAD_DIR, "..", f"{MARK}-testigo.txt")
    testigo_abs = os.path.abspath(testigo_rel)
    os.makedirs(os.path.dirname(testigo_abs), exist_ok=True)
    with open(testigo_abs, "w") as f:
        f.write("no me borres")

    foto_fuera = RecepcionItemFoto(
        recepcion_item_id=d["ri_a"],
        url_foto=f"/uploads/bodega/../{MARK}-testigo.txt")
    db.add(foto_fuera)
    db.commit()
    # El id se guarda ANTES de llamar: `delete_foto` borra la fila aunque la ruta sea
    # inválida (es lo que el usuario pidió; solo NO toca el disco), y después la
    # instancia de SQLAlchemy ya no se puede leer.
    foto_fuera_id = foto_fuera.id
    try:
        r = cliente.delete(
            f"/api/bodega/recepciones/{d['rec_a']}/items/{d['ri_a']}/foto/{foto_fuera_id}")
        check("borrar una foto que apunta fuera del directorio no revienta",
              r.status_code in (200, 400, 404), r.status_code)
        check("y NO tocó el archivo de fuera de uploads/bodega",
              os.path.isfile(testigo_abs), testigo_abs)
    finally:
        db.rollback()
        db.query(RecepcionItemFoto).filter(
            RecepcionItemFoto.id == foto_fuera_id).delete()
        db.commit()
        if os.path.exists(testigo_abs):
            os.remove(testigo_abs)


def _paso_9_la_ambiguedad_no_bloquea_y_el_dato_explicito_manda(db, d):
    """Un mismo entero puede designar DOS filas de la misma recepción — y aun así el
    bodeguero tiene que poder trabajar.

    POR QUÉ NO SE RESPONDE 409 ANTE LA AMBIGÜEDAD
        Sería peor que el defecto: el 409 solo dispararía cuando AMBAS ramas resuelven
        —justo el caso en que el id es correcto— y la pantalla no deja guardar un ítem
        dañado sin foto. El bodeguero quedaría sin poder subir la foto NI guardar, y
        «recarga» no ayuda porque para un ítem empezado los dos ids coinciden. Bloquear
        no es fallar seguro cuando deja al operador sin salida.

        La salida es que el llamador desambigüe con el dato que YA tiene
        (`embarque_item_id`), igual que `marcar_item`.

    Se prueban las dos mitades:
      · con el dato explícito, la foto va al ítem CORRECTO aunque haya colisión;
      · sin el dato explícito (cliente viejo) NO se bloquea: sube igual.
    """
    tope = max(
        db.execute(text("SELECT COALESCE(MAX(id),0) FROM embarque_items")).scalar(),
        db.execute(text("SELECT COALESCE(MAX(id),0) FROM recepcion_embarque_items")).scalar(),
    ) + 5000
    creados = False
    try:
        # EmbarqueItem con id = tope, del embarque de esta recepción.
        db.execute(text("INSERT INTO embarque_items (id, embarque_id) VALUES (:i, :e)"),
                   {"i": tope, "e": d["emb"]})
        # RecepcionEmbarqueItem con id = tope: el entero `tope` ahora designa ESTA fila
        # por `.id` Y designa un EmbarqueItem distinto por el otro espacio.
        db.execute(text("INSERT INTO recepcion_embarque_items (id, recepcion_id, qty_recibida) "
                        "VALUES (:i, :r, 1)"), {"i": tope, "r": d["rec_a"]})
        db.commit()
        creados = True

        # (a) CON el dato explícito: la foto tiene que quedar en el ítem del
        #     `embarque_item_id`, no en el que coincide por `.id`.
        r = cliente.post(
            f"/api/bodega/recepciones/{d['rec_a']}/items/{tope}/foto",
            params={"embarque_item_id": tope},
            files={"file": ("x.jpg", b"\xff\xd8\xff\xdb" + b"\x00" * 8, "image/jpeg")})
        check("con embarque_item_id explícito la subida funciona", r.status_code == 201,
              f"{r.status_code} {r.text[:140]}")
        if r.status_code == 201:
            ri_usado = r.json().get("recepcion_item_id")
            fila = db.query(RecepcionEmbarqueItem).filter(
                RecepcionEmbarqueItem.id == ri_usado).first()
            check("y la foto quedó en el ítem del embarque_item_id, no en el homónimo",
                  fila is not None and fila.embarque_item_id == tope,
                  (ri_usado, fila.embarque_item_id if fila else None))

        # (b) SIN el dato explícito (cliente viejo): NO se bloquea.
        r = cliente.post(
            f"/api/bodega/recepciones/{d['rec_a']}/items/{tope}/foto",
            files={"file": ("y.jpg", b"\xff\xd8\xff\xdb" + b"\x00" * 8, "image/jpeg")})
        check("sin el dato explícito NO se bloquea al operador (nada de 409)",
              r.status_code == 201, f"{r.status_code} {r.text[:140]}")

        # (c) Un id que no pertenece a esta recepción da 404 entendible, no un 500.
        r = cliente.post(
            f"/api/bodega/recepciones/{d['rec_a']}/items/999999999/foto",
            params={"embarque_item_id": 999999999},
            files={"file": ("z.jpg", b"\xff\xd8\xff\xdb" + b"\x00" * 8, "image/jpeg")})
        check("un ítem ajeno da 404, no un 500 por llave foránea",
              r.status_code == 404, f"{r.status_code} {r.text[:140]}")
    finally:
        db.rollback()
        if creados:
            # Los archivos de las subidas de esta etapa salen del disco junto con sus
            # filas: si no, cada corrida deja huérfanos en uploads/bodega.
            for (url,) in db.execute(text(
                    "SELECT f.url_foto FROM recepcion_item_fotos f "
                    "JOIN recepcion_embarque_items ri ON ri.id = f.recepcion_item_id "
                    "WHERE ri.embarque_item_id = :i OR ri.id = :i"), {"i": tope}).all():
                ruta = os.path.join(UPLOAD_DIR, os.path.basename(url))
                if os.path.isfile(ruta):
                    os.remove(ruta)
            db.execute(text("DELETE f FROM recepcion_item_fotos f "
                            "JOIN recepcion_embarque_items ri ON ri.id = f.recepcion_item_id "
                            "WHERE ri.embarque_item_id = :i OR ri.id = :i"), {"i": tope})
            db.execute(text("DELETE FROM recepcion_embarque_items "
                            "WHERE id = :i OR embarque_item_id = :i"), {"i": tope})
            db.execute(text("DELETE FROM embarque_items WHERE id = :i"), {"i": tope})
            db.commit()



def _paso_10_las_fotos_siguen_alcanzables_con_la_recepcion_cerrada(db, d):
    """Las fotos de una recepción CERRADA se pueden seguir consultando.

    POR QUÉ (pedido del dueño, 2026-08-20): en bodega se fotografían los ítems de alto
    valor (sobre $3.000.000 de venta) aunque lleguen completos, y varias fotos por ítem.
    Esas fotos hay que poder mirarlas después.

    El camino tiene dos tramos y los dos se prueban: el historial devuelve
    `recepcion_id` (sin él la tarjeta de una recepción cerrada no abre nada) y el
    detalle de la recepción cerrada sigue trayendo las fotos de cada ítem.
    """
    # La recepción de prueba se cierra a mano (sin pasar por cerrar_recepcion, que exige
    # reglas de negocio ajenas a esta prueba).
    db.execute(text("UPDATE recepciones_embarque SET estado = 'cerrada' WHERE id = :r"),
               {"r": d["rec_a"]})
    db.commit()
    try:
        # 1. El historial devuelve el id de la recepción: el camino existe.
        r = cliente.get("/api/bodega/embarques/historial")
        check("el historial responde 200", r.status_code == 200, r.status_code)
        filas = r.json() if r.status_code == 200 else []
        fila = next((x for x in filas if x.get("id") == d["emb"]), None)
        check("el embarque de prueba aparece en el historial", fila is not None,
              [x.get("id") for x in filas][:10])
        if fila:
            check("y trae recepcion_id (sin él la tarjeta no abre nada)",
                  fila.get("recepcion_id") == d["rec_a"], fila.get("recepcion_id"))
            check("y trae total_items (la pantalla lo pinta)",
                  isinstance(fila.get("total_items"), int), fila.get("total_items"))

        # 2. El detalle de la recepción cerrada sigue trayendo las fotos por ítem.
        r = cliente.get(f"/api/bodega/recepciones/{d['rec_a']}")
        check("la recepción cerrada se puede abrir", r.status_code == 200, r.status_code)
        if r.status_code == 200:
            datos = r.json()
            check("y viene marcada como cerrada (la pantalla la pone en solo lectura)",
                  datos.get("estado") == "cerrada", datos.get("estado"))
            con_fotos = [i for i in datos.get("items", []) if i.get("fotos")]
            check("y algún ítem conserva sus fotos", len(con_fotos) > 0,
                  [(i.get("id"), len(i.get("fotos", []))) for i in datos.get("items", [])])

        # 3. Y la imagen misma se sigue sirviendo.
        r = cliente.get(f"/api/bodega/fotos/{d['ok']}")
        check("la imagen de una recepción cerrada se sigue sirviendo",
              r.status_code == 200, r.status_code)
    finally:
        db.execute(text("UPDATE recepciones_embarque SET estado = 'abierta' WHERE id = :r"),
                   {"r": d["rec_a"]})
        db.commit()



def _paso_11_las_fotos_del_item_se_ven_desde_despachos(db, d):
    """`GET /bodega/items/{item_cotizacion_id}/fotos` — el camino desde Despachos.

    POR QUÉ (pedido del dueño, 2026-08-20): al preparar el despacho hay que poder mirar
    las fotos que bodega tomó al recibir la pieza. La cadena ya existía por llaves
    reales; faltaba el endpoint que la recorriera.

    Es de SOLO CONSULTA a propósito: no hay borrado desde acá. Esas fotos son la
    evidencia de la recepción y alimentan el reclamo al proveedor; quien las borra tiene
    que hacerlo en la pantalla donde las tomó, con su contexto a la vista.
    """
    # El ítem de cotización al que cuelga el EmbarqueItem de prueba.
    ei = db.query(EmbarqueItem).filter(EmbarqueItem.id == d["emb_item"]).first()
    if not ei or ei.item_cotizacion_id is None:
        # El dato de prueba se creó sin línea de venta: se prueba el contrato con un id
        # inexistente, que es el borde que sí importa (no puede reventar).
        r = cliente.get("/api/bodega/items/999999999/fotos")
        check("un ítem sin fotos devuelve lista vacía, no error",
              r.status_code == 200 and r.json() == [], f"{r.status_code} {r.text[:100]}")
    else:
        r = cliente.get(f"/api/bodega/items/{ei.item_cotizacion_id}/fotos")
        check("el ítem devuelve sus fotos de recepción", r.status_code == 200, r.status_code)
        ids = [f["id"] for f in (r.json() or [])]
        check("y aparece la foto del ítem no empezado", d["c"] in ids, ids)

    # La otra marca no puede verlas.
    r = cliente_otra_marca.get("/api/bodega/items/1/fotos")
    check("otra marca: el listado por ítem responde 403", r.status_code == 403,
          r.status_code)

    # Y el endpoint NO expone borrado: solo GET.
    r = cliente.delete("/api/bodega/items/1/fotos")
    check("no hay borrado por esta ruta (405)", r.status_code == 405, r.status_code)



def _paso_12_varias_fotos_del_mismo_item_no_se_pisan(db, d):
    """Subir varias fotos al MISMO ítem no puede colapsarlas en un archivo.

    POR QUÉ: con un nombre derivado del reloj (`recepcion_{ri.id}_{segundos}{ext}`),
    dos fotos del mismo ítem dentro del mismo segundo comparten archivo: tres FILAS en
    la base apuntando a UN solo archivo, tres miniaturas iguales, y al borrar una las
    otras quedan en 404. Es pérdida silenciosa de la evidencia del reclamo al proveedor,
    y varias fotos por ítem es el flujo normal. Por eso el nombre lleva un uuid4.

    Se sube por el endpoint real (no insertando filas) para ejercitar la generación del
    nombre, y las tres subidas van en la misma llamada rápida para reproducir el mismo
    segundo.
    """
    subidas = []
    for i in range(3):
        r = cliente.post(
            f"/api/bodega/recepciones/{d['rec_a']}/items/{d['ri_a']}/foto",
            params={"embarque_item_id": d["emb_item_ri_a"]} if d.get("emb_item_ri_a") else {},
            files={"file": (f"f{i}.jpg", b"\xff\xd8\xff\xdb" + bytes([i]) * 40, "image/jpeg")})
        check(f"subida {i + 1} responde 201", r.status_code == 201,
              f"{r.status_code} {r.text[:120]}")
        if r.status_code == 201:
            subidas.append(r.json())
    try:
        check("se crearon 3 filas de foto", len(subidas) == 3, len(subidas))
        # Los archivos en disco tienen que ser DISTINTOS.
        db.expire_all()
        urls = set()
        for u in subidas:
            foto = db.query(RecepcionItemFoto).filter(
                RecepcionItemFoto.id == u["id"]).first()
            if foto:
                urls.add(foto.url_foto)
        check("los 3 archivos en disco son distintos (no se pisaron)",
              len(urls) == len(subidas), urls)

        # Al borrar UNA, las otras dos siguen sirviéndose.
        if len(subidas) == 3:
            r = cliente.delete(
                f"/api/bodega/recepciones/{d['rec_a']}/items/{d['ri_a']}/foto/{subidas[0]['id']}")
            check("borrar la primera responde 200", r.status_code == 200, r.status_code)
            for u in subidas[1:]:
                r = cliente.get(f"/api/bodega/fotos/{u['id']}")
                check("las otras fotos siguen sirviéndose (no 404)",
                      r.status_code == 200, r.status_code)
    finally:
        # Borrar las filas y los archivos que quedaron.
        for u in subidas:
            foto = db.query(RecepcionItemFoto).filter(
                RecepcionItemFoto.id == u["id"]).first()
            if foto:
                nombre = os.path.basename(foto.url_foto)
                ruta = os.path.join(UPLOAD_DIR, nombre)
                if os.path.isfile(ruta):
                    os.remove(ruta)
                db.delete(foto)
        db.commit()


db_global = None


def run():
    global db_global
    db = SessionLocal()
    db_global = db
    try:
        _limpiar(db)
        d = _crear_datos(db)
        _paso_1_sin_sesion_no_entrega(d)
        _paso_2_id_inexistente_es_404(d)
        _paso_3_con_sesion_entrega(d)
        _paso_4_confinamiento(d)
        _paso_5_otra_marca_no_puede_nada(d)
        _paso_10_las_fotos_siguen_alcanzables_con_la_recepcion_cerrada(db, d)
        _paso_11_las_fotos_del_item_se_ven_desde_despachos(db, d)
        _paso_12_varias_fotos_del_mismo_item_no_se_pisan(db, d)
        _paso_6a_el_ri_id_debe_ser_de_esa_recepcion(d)
        _paso_6b_la_foto_debe_ser_de_ese_item(d)
        _paso_6c_el_camino_legitimo_si_borra(d)
        _paso_6d_la_subida_no_cruza_recepciones(d)
        _paso_7_el_id_del_embarque_item_tambien_resuelve(d)
        _paso_8_el_borrado_tambien_confina_la_ruta(db, d)
        _paso_9_la_ambiguedad_no_bloquea_y_el_dato_explicito_manda(db, d)
    finally:
        _limpiar(db)
        db.close()
        db_global = None
        print("Cleanup OK")

    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_bodega_fotos_auth():
    run()


if __name__ == "__main__":
    run()
