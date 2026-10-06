"""FOTOS del Portal de Clientes — lo que el personal sube y lo que el cliente ve, con la
app completa, cuentas reales y sesiones reales.

LO QUE ESTA SUITE PROTEGE (portal/fotos.py, router_admin_fotos.py y GET /api/portal/fotos/{id})
  §A  SE GUARDA SOLO UN DERIVADO: la foto del teléfono trae el GPS de la bodega, la marca
      del equipo y a veces un comentario; el archivo que queda en disco no trae nada de
      eso, está enderezado, reducido y es un JPEG de verdad. Lo que no es una imagen
      aceptada se rechaza con SU mensaje, sea cual sea su extensión; los topes de peso
      y de píxeles cortan donde dicen (y no antes), y a lo más se procesan dos a la vez.
  §B  EL NOMBRE Y LA RUTA LOS DECIDE EL SERVIDOR: nada que no tenga la forma exacta que
      escribe `guardar` se lee del disco.
  §C  QUIÉN SUBE: solo el personal de Grupo AM, a un ítem de una venta cerrada, hasta el
      máximo por ítem (también si varias subidas llegan juntas), y nunca a un ítem en
      reclamo. Borrar quita la fila y el archivo. Ningún intento rechazado o fallido
      deja un archivo suelto en la carpeta.
  §D  QUIÉN VE: el cliente dueño de la orden (cualquier perfil: una foto no es un monto);
      otro cliente recibe 404. Mientras el ítem está en reclamo sus fotos no se muestran.
      Una foto cuyo archivo se perdió responde 404, no 500.
  §E  EN UN ÍTEM PARTIDO, las fotos de todas sus filas son del mismo ítem.
  §F  NI EL NOMBRE NI LA RUTA DEL ARCHIVO viajan en ninguna respuesta.

MUTACIONES verificadas al construir la suite (quitar → rojo → restaurar → verde):
  el vaciado de metadatos, el enderezado, la lista de formatos, el tope de peso, los
  dos topes de píxeles y el turno de dos derivados a la vez (→ §A), la validación del
  nombre (→ §B), el candado de marca, el de venta cerrada, el máximo, el recuento con la
  fila bloqueada, la lectura de un byte más que el tope, la regla de reclamo al subir,
  el borrado del archivo y la limpieza del archivo si la fila no se graba (→ §C), y la
  regla de reclamo al mostrar (→ §D).

Corre con:  ./venv/bin/python -m pytest portal/tests/test_fotos.py -q
(también:   ./venv/bin/python portal/tests/test_fotos.py)
"""
import io
import os
import sys
import threading
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from database import SessionLocal  # noqa: E402
from main import app  # noqa: E402
from models.models import Cotizacion, ItemCotizacion  # noqa: E402
from portal import fotos, router_admin_fotos  # noqa: E402
from portal.models import FotoArticulo  # noqa: E402
from portal.tests import _datos as D  # noqa: E402

MARK = "ZZPORTALFOT"  # SIN guiones bajos: en LIKE el _ es comodín
client = TestClient(app)
_fails: list = []

ETIQUETA_GPS, ETIQUETA_ORIENTACION, ETIQUETA_MARCA = 0x8825, 0x0112, 0x010F
COMENTARIO = b"bodega de Grupo AM, pasillo 3"


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _cab(email: str) -> dict:
    r = client.post("/api/auth/login", json={"email": email, "password": D.CLAVE})
    assert r.status_code == 200, f"login de {email}: {r.status_code} {r.text[:120]}"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _foto_de_telefono(ancho=3000, alto=2000, orientacion=6) -> bytes:
    """Un JPEG como el que sube un teléfono: con GPS, marca del equipo, orientación y un
    comentario. `orientacion=6` = la cámara estaba girada (hay que rotar 90° al mostrar)."""
    exif = Image.Exif()
    exif[ETIQUETA_ORIENTACION] = orientacion
    exif[ETIQUETA_MARCA] = "TelefonoDePrueba"
    gps = exif.get_ifd(ETIQUETA_GPS)
    gps[1], gps[2], gps[3], gps[4] = "S", (23.0, 39.0, 0.0), "W", (70.0, 24.0, 0.0)
    return D.imagen_jpeg(ancho, alto, exif=exif.tobytes(), comment=COMENTARIO)


def _en_otro_formato(formato: str, modo: str = "RGB", tamano=(400, 300)) -> bytes:
    salida = io.BytesIO()
    Image.new(modo, tamano, 128 if modo in ("L", "1", "P") else (10, 120, 200, 128)[: len(modo)]).save(salida, format=formato)
    return salida.getvalue()


def _con_relleno(total: int) -> bytes:
    """Un JPEG VÁLIDO llevado a `total` bytes con relleno después del fin de la imagen
    (el decodificador lo ignora): pesa lo que se pida y sigue siendo una foto."""
    jpeg = D.imagen_jpeg()
    return jpeg + b"\0" * (total - len(jpeg))


def _metadatos(contenido: bytes) -> dict:
    imagen = Image.open(io.BytesIO(contenido))
    exif = imagen.getexif()
    return {
        "formato": imagen.format, "modo": imagen.mode, "tamano": imagen.size,
        "exif": dict(exif), "gps": dict(exif.get_ifd(ETIQUETA_GPS)),
        "comentario": imagen.info.get("comment"), "perfil": imagen.info.get("icc_profile"),
    }


def _rechaza(contenido: bytes) -> str:
    try:
        fotos.derivado(contenido)
    except fotos.FotoError as error:
        return str(error)
    return ""


# ── §A y §B: funciones puras ────────────────────────────────────────────────────────────

def _paso_a_derivado():
    original = _foto_de_telefono()
    antes = _metadatos(original)
    check("§A (la sonda sirve) la foto de prueba SÍ trae GPS, marca, orientación y comentario",
          antes["gps"] and antes["exif"].get(ETIQUETA_MARCA) and antes["exif"].get(ETIQUETA_ORIENTACION) == 6
          and antes["comentario"] == COMENTARIO, antes)
    datos, ancho, alto = fotos.derivado(original)
    despues = _metadatos(datos)
    check("§A el derivado no trae EXIF, ni GPS, ni comentario, ni perfil de color",
          despues["exif"] == {} and despues["gps"] == {} and despues["comentario"] is None
          and despues["perfil"] is None, despues)
    check("§A …y el comentario no está en los bytes del archivo", COMENTARIO not in datos and b"TelefonoDePrueba" not in datos)
    check("§A es un JPEG en RGB, enderezado (3000×2000 girada → vertical) y reducido a 1600 de lado mayor",
          despues["formato"] == "JPEG" and despues["modo"] == "RGB" and despues["tamano"] == (1067, 1600)
          and (ancho, alto) == (1067, 1600), (despues["tamano"], ancho, alto))
    chica, ancho, alto = fotos.derivado(D.imagen_jpeg(640, 480))
    check("§A una foto chica no se agranda", (ancho, alto) == (640, 480), (ancho, alto))

    for nombre, contenido in (("PNG con transparencia", _en_otro_formato("PNG", "RGBA")),
                              ("PNG con paleta", _en_otro_formato("PNG", "P")),
                              ("WebP", _en_otro_formato("WEBP")), ("JPEG en escala de grises", _en_otro_formato("JPEG", "L"))):
        try:
            datos, _, _ = fotos.derivado(contenido)
            m = _metadatos(datos)
            ok = m["formato"] == "JPEG" and m["modo"] == "RGB"
        except fotos.FotoError as error:
            ok, m = False, str(error)
        check(f"§A acepta {nombre} y lo deja como JPEG RGB", ok, m)

    jpeg = D.imagen_jpeg()
    megas = fotos.PESO_MAXIMO_SUBIDA // (1024 * 1024)
    vacio, no_soportado = "El archivo está vacío.", "Formato no soportado: sube una foto JPG, PNG o WebP."
    danado, grande = "El archivo no es una imagen válida o está dañado.", "La imagen es demasiado grande."
    rechazos = {
        "un archivo vacío": (b"", vacio),
        "un GIF": (_en_otro_formato("GIF", "P"), no_soportado),
        "un BMP": (_en_otro_formato("BMP"), no_soportado),
        "un PDF con nombre de foto": (b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n", danado),
        "un HTML": (b"<html><script>alert(1)</script></html>", danado),
        "un SVG": (b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', danado),
        "un JPEG cortado por la mitad": (jpeg[: len(jpeg) // 2], danado),
        "solo la cabecera de un JPEG": (jpeg[:20], danado),
        # Fotos VÁLIDAS que pasan un tope (si el tope no cortara, se aceptarían).
        "una foto válida de un byte más que el tope de peso":
            (_con_relleno(fotos.PESO_MAXIMO_SUBIDA + 1), f"La foto pesa más de {megas} MB."),
        "un PNG de 26 megapíxeles (pesa pocos KB y al abrirse ocupa cientos de MB)":
            (_en_otro_formato("PNG", "1", (6500, 4000)), grande),
        "un JPEG de 61 megapíxeles": (_en_otro_formato("JPEG", "L", (9000, 6800)), grande),
    }
    for nombre, (contenido, esperado) in rechazos.items():
        mensaje = _rechaza(contenido)
        check(f"§A rechaza {nombre} con su mensaje para el operador", mensaje == esperado, mensaje or "se aceptó")
    for nombre, contenido in (
        ("una foto válida que pesa justo el tope", _con_relleno(fotos.PESO_MAXIMO_SUBIDA)),
        ("un JPEG de 26 megapíxeles (un teléfono actual)", _en_otro_formato("JPEG", "L", (6500, 4000))),
    ):
        check(f"§A acepta {nombre}: los topes no cortan antes de lo que dicen", _rechaza(contenido) == "",
              _rechaza(contenido))


def _paso_a_dos_a_la_vez():
    """Cada derivado puede ocupar más de 100 MB mientras se calcula: a lo más dos a la vez."""
    estado = {"en_curso": 0, "maximo": 0}
    candado, original = threading.Lock(), fotos._derivar

    def lento(contenido):
        with candado:
            estado["en_curso"] += 1
            estado["maximo"] = max(estado["maximo"], estado["en_curso"])
        time.sleep(0.15)
        with candado:
            estado["en_curso"] -= 1
        return original(contenido)

    fotos._derivar = lento
    try:
        hilos = [threading.Thread(target=fotos.derivado, args=(D.imagen_jpeg(),)) for _ in range(6)]
        for hilo in hilos:
            hilo.start()
        for hilo in hilos:
            hilo.join(20)
    finally:
        fotos._derivar = original
    check("§A seis fotos subidas a la vez se procesan de a dos", estado["maximo"] == 2, estado)


def _paso_b_nombre_y_ruta():
    datos, _, _ = fotos.derivado(D.imagen_jpeg())
    nombre = fotos.guardar(datos)
    try:
        ruta = fotos.ruta_de(nombre)
        check("§B guardar escribe <32 hex>.jpg dentro de la carpeta de fotos del portal",
              len(nombre) == 36 and nombre.endswith(".jpg") and ruta is not None
              and os.path.dirname(ruta) == fotos.DIRECTORIO, (nombre, ruta))
        for malo in (None, "", "../" + nombre, f"../portal_fotos/{nombre}", "/etc/passwd", nombre.upper(),
                     nombre.replace(".jpg", ".png"), "a" * 31 + ".jpg", nombre + "\x00.jpg", "..", "."):
            check(f"§B ruta_de({malo!r}) → None", fotos.ruta_de(malo) is None, fotos.ruta_de(malo))
    finally:
        fotos.borrar_archivo(nombre)
    check("§B borrar_archivo quita el archivo, y pedir su ruta después da None", fotos.ruta_de(nombre) is None)


# ── Siembra ─────────────────────────────────────────────────────────────────────────────

def _sembrar() -> dict:
    db = SessionLocal()
    try:
        fa, fb = D.ficha(db, MARK, "A", 76000011), D.ficha(db, MARK, "B", 76000022)
        pa, pb = D.portal(db, MARK, "a", [fa.id]), D.portal(db, MARK, "b", [fb.id])
        bodega = D.portal(db, MARK, "bod", [fa.id], perfil="bodega")
        operador = D.interno(db, MARK, "op")
        de_monza = D.interno(db, MARK, "monza", empresa="automotriz")
        _, oc, it = D.venta(db, MARK, "A1", fa, [
            dict(item_num=1, numero_parte="1F-0001", estado="en_bodega"),
            dict(item_num=2, numero_parte="1F-0002", descripcion="RODAMIENTO", cantidad=2, estado="en_bodega"),
            dict(item_num=2, numero_parte="1F-0002", descripcion="RODAMIENTO", cantidad=3, estado="comprado"),
            dict(item_num=3, numero_parte="1F-0003", estado="reclamo_proveedor"),
            dict(item_num=4, numero_parte="1F-0004", estado="en_bodega"),
            dict(item_num=5, numero_parte="1F-0005", estado="en_bodega"),  # para el tope de peso por HTTP
            dict(item_num=6, numero_parte="1F-0006", estado="en_bodega"),  # para las subidas simultáneas
        ])
        _, ocb, itb = D.venta(db, MARK, "B1", fb, [dict(numero_parte="2F-0001", estado="en_bodega")])
        # Una cotización que todavía no es venta (sin orden de compra del cliente).
        borrador = Cotizacion(numero=f"{MARK}-BORR", cliente=fa.nombre, rut_cliente=fa.rut)
        db.add(borrador)
        db.flush()
        suelto = ItemCotizacion(cotizacion_id=borrador.id, item_num=1, numero_parte="0F-0000", cantidad=1)
        db.add(suelto)
        db.commit()
        return {
            "oc": oc.id, "ocb": ocb.id, "it": [i.id for i in it], "itb": itb[0].id, "suelto": suelto.id,
            "email_a": pa.email, "email_b": pb.email, "email_bod": bodega.email,
            "email_op": operador.email, "email_monza": de_monza.email, "cot": oc.cotizacion_id,
            "cotb": ocb.cotizacion_id,
        }
    finally:
        db.close()


def _subir(cab: dict, item_id: int, contenido: bytes, nombre: str = "foto.jpg"):
    return client.post(f"/api/portal-admin/items/{item_id}/fotos", headers=cab,
                       files={"file": (nombre, contenido, "image/jpeg")})


def _archivo_de(foto_id: int):
    db = SessionLocal()
    try:
        fila = db.query(FotoArticulo).filter(FotoArticulo.id == foto_id).first()
        return fila.archivo if fila else None
    finally:
        db.close()


def _archivos_con_fila(ids: dict) -> list:
    """Los nombres de archivo que la base dice que existen, de las fotos de esta suite."""
    db = SessionLocal()
    try:
        return sorted(a for (a,) in db.query(FotoArticulo.archivo)
                      .join(ItemCotizacion, ItemCotizacion.id == FotoArticulo.item_cotizacion_id)
                      .filter(ItemCotizacion.cotizacion_id.in_([ids["cot"], ids["cotb"]])).all())
    finally:
        db.close()


def _estado_item(item_id: int, estado: str) -> None:
    db = SessionLocal()
    try:
        db.query(ItemCotizacion).filter(ItemCotizacion.id == item_id).update({"estado_item": estado})
        db.commit()
    finally:
        db.close()


# ── §C ──────────────────────────────────────────────────────────────────────────────────

def _paso_c_quien_sube(cab_op, cab_monza, cab_a, ids) -> dict:
    item = ids["it"][0]
    r = _subir(cab_op, item, _foto_de_telefono())
    check("§C el operador sube una foto → 201 con su id y sus medidas ya reducidas",
          r.status_code == 201 and r.json()["ancho"] == 1067 and r.json()["alto"] == 1600
          and set(r.json()) == {"id", "ancho", "alto", "peso_bytes", "fecha"}, f"{r.status_code} {r.text[:160]}")
    foto_id = r.json()["id"]
    en_disco = open(fotos.ruta_de(_archivo_de(foto_id)), "rb").read()
    m = _metadatos(en_disco)
    check("§C lo que quedó EN DISCO es el derivado: sin EXIF, sin GPS, sin comentario",
          m["exif"] == {} and m["gps"] == {} and m["comentario"] is None and COMENTARIO not in en_disco, m)
    check("§C el tamaño informado es el del archivo guardado", r.json()["peso_bytes"] == len(en_disco))

    lista = client.get(f"/api/portal-admin/items/{item}/fotos", headers=cab_op).json()
    check("§C la lista interna trae la foto y el máximo por ítem",
          [f["id"] for f in lista["fotos"]] == [foto_id] and lista["maximo"] == fotos.FOTOS_MAXIMAS_POR_ITEM, lista)
    r = client.get(f"/api/portal-admin/fotos/{foto_id}", headers=cab_op)
    check("§C la vista previa interna entrega el JPEG", r.status_code == 200
          and r.headers["content-type"] == "image/jpeg" and r.content == en_disco)

    for nombre, contenido in (("un GIF renombrado a .jpg", _en_otro_formato("GIF", "P")),
                              ("un PDF renombrado a .jpg", b"%PDF-1.4 no soy una foto"), ("un archivo vacío", b"")):
        r = _subir(cab_op, item, contenido)
        check(f"§C subir {nombre} → 400 con el motivo", r.status_code == 400
              and isinstance(r.json().get("detail"), str) and len(r.json()["detail"]) > 10, f"{r.status_code} {r.text[:120]}")
    r = client.post(f"/api/portal-admin/items/{item}/fotos", headers=cab_op)
    check("§C subir sin archivo → 422", r.status_code == 422, r.status_code)

    for n in range(fotos.FOTOS_MAXIMAS_POR_ITEM - 1):
        assert _subir(cab_op, item, D.imagen_jpeg(color=(n * 30, 10, 10))).status_code == 201
    r = _subir(cab_op, item, D.imagen_jpeg())
    check(f"§C la foto N° {fotos.FOTOS_MAXIMAS_POR_ITEM + 1} del mismo ítem → 409",
          r.status_code == 409 and "máximo" in r.json()["detail"], f"{r.status_code} {r.text[:120]}")

    r = _subir(cab_op, ids["it"][3], D.imagen_jpeg())
    check("§C subir a un ítem en reclamo al proveedor → 409", r.status_code == 409, f"{r.status_code} {r.text[:120]}")
    r = _subir(cab_op, ids["suelto"], D.imagen_jpeg())
    check("§C subir a un ítem de una cotización que no es venta cerrada → 404", r.status_code == 404,
          f"{r.status_code} {r.text[:120]}")
    check("§C subir a un ítem inexistente → 404", _subir(cab_op, 999999999, D.imagen_jpeg()).status_code == 404)
    check("§C …y ninguno de esos intentos dejó un archivo ni una fila",
          client.get(f"/api/portal-admin/items/{ids['it'][3]}/fotos", headers=cab_op).json()["fotos"] == [])

    for nombre, cab, esperado in (("el personal de la otra marca", cab_monza, 403), ("una cuenta del portal", cab_a, 403),
                                  ("nadie (sin sesión)", {}, 401)):
        codigos = {
            _subir(cab, ids["it"][4], D.imagen_jpeg()).status_code,
            client.get(f"/api/portal-admin/items/{item}/fotos", headers=cab).status_code,
            client.get(f"/api/portal-admin/fotos/{foto_id}", headers=cab).status_code,
            client.delete(f"/api/portal-admin/fotos/{foto_id}", headers=cab).status_code,
        }
        check(f"§C {nombre} → {esperado} en subir, listar, ver y borrar", codigos == {esperado}, codigos)
    check("§C …y la foto sigue ahí", client.get(f"/api/portal-admin/fotos/{foto_id}", headers=cab_op).status_code == 200)
    return {"foto": foto_id, "archivo": _archivo_de(foto_id)}


def _paso_c_topes_y_archivos_sueltos(cab_op, ids):
    item = ids["it"][5]
    megas = fotos.PESO_MAXIMO_SUBIDA // (1024 * 1024)
    r = _subir(cab_op, item, _con_relleno(fotos.PESO_MAXIMO_SUBIDA + 1))
    check("§C subir una foto de un byte más que el tope de peso → 400 con el motivo",
          r.status_code == 400 and r.json()["detail"] == f"La foto pesa más de {megas} MB.", f"{r.status_code} {r.text[:120]}")
    r = _subir(cab_op, item, _con_relleno(fotos.PESO_MAXIMO_SUBIDA))
    check("§C …y una que pesa justo el tope entra", r.status_code == 201, f"{r.status_code} {r.text[:120]}")

    # La fila no se pudo armar DESPUÉS de escribir el archivo (acá, porque el reloj con que
    # se fecha falla; en la vida real, un INSERT rechazado): el archivo no puede quedar.
    class RelojRoto:
        @staticmethod
        def utcnow():
            raise RuntimeError("reloj de prueba: roto")

    antes = sorted(os.listdir(fotos.DIRECTORIO))
    tolerante, original = TestClient(app, raise_server_exceptions=False), router_admin_fotos.datetime
    router_admin_fotos.datetime = RelojRoto
    try:
        r = tolerante.post(f"/api/portal-admin/items/{item}/fotos", headers=cab_op,
                           files={"file": ("foto.jpg", D.imagen_jpeg(), "image/jpeg")})
    finally:
        router_admin_fotos.datetime = original
    check("§C si la fila no se graba, el archivo recién escrito se borra (no queda suelto)",
          r.status_code == 500 and sorted(os.listdir(fotos.DIRECTORIO)) == antes,
          (r.status_code, len(os.listdir(fotos.DIRECTORIO)), len(antes)))
    check("§C después de todos los rechazos y fallos, en la carpeta hay EXACTAMENTE los archivos que la base conoce",
          sorted(os.listdir(fotos.DIRECTORIO)) == _archivos_con_fila(ids) and len(_archivos_con_fila(ids)) >= 2,
          (sorted(os.listdir(fotos.DIRECTORIO))[:3], _archivos_con_fila(ids)[:3]))


def _paso_c_subidas_simultaneas(cab_op, ids):
    """El operador elige varias fotos de una vez y el navegador las sube juntas."""
    item = ids["it"][6]
    for n in range(fotos.FOTOS_MAXIMAS_POR_ITEM - 1):
        assert _subir(cab_op, item, D.imagen_jpeg(color=(n * 30, 90, 10))).status_code == 201
    # `guardar` más lento: sin el recuento con la fila bloqueada, todas alcanzarían a
    # contar «5 de 6» antes de que alguna terminara.
    original, codigos = fotos.guardar, []

    def guardar_lento(datos):
        time.sleep(0.3)
        return original(datos)

    def subir():
        propio = TestClient(app)
        r = propio.post(f"/api/portal-admin/items/{item}/fotos", headers=cab_op,
                        files={"file": ("foto.jpg", D.imagen_jpeg(), "image/jpeg")})
        codigos.append(r.status_code)

    fotos.guardar = guardar_lento
    try:
        hilos = [threading.Thread(target=subir) for _ in range(4)]
        for hilo in hilos:
            hilo.start()
        for hilo in hilos:
            hilo.join(30)
    finally:
        fotos.guardar = original
    cuantas = len(client.get(f"/api/portal-admin/items/{item}/fotos", headers=cab_op).json()["fotos"])
    check("§C cuatro subidas simultáneas con un solo cupo libre: entra UNA y las otras tres reciben 409",
          sorted(codigos) == [201, 409, 409, 409] and cuantas == fotos.FOTOS_MAXIMAS_POR_ITEM, (sorted(codigos), cuantas))
    check("§C …y en la carpeta siguen estando exactamente los archivos que la base conoce",
          sorted(os.listdir(fotos.DIRECTORIO)) == _archivos_con_fila(ids))


def _paso_c_borrar(cab_op, cab_a, ids, foto: dict):
    r = client.delete(f"/api/portal-admin/fotos/{foto['foto']}", headers=cab_op)
    check("§C borrar → 204, sin fila y SIN archivo en disco",
          r.status_code == 204 and _archivo_de(foto["foto"]) is None and fotos.ruta_de(foto["archivo"]) is None,
          f"{r.status_code}")
    check("§C borrar otra vez → 404", client.delete(f"/api/portal-admin/fotos/{foto['foto']}", headers=cab_op).status_code == 404)
    check("§C …y el cliente ya no la ve", client.get(f"/api/portal/fotos/{foto['foto']}", headers=cab_a).status_code == 404)


# ── §D, §E, §F ──────────────────────────────────────────────────────────────────────────

def _paso_d_quien_ve(cab_op, cab_a, cab_b, cab_bod, ids, foto: dict):
    oc, item = ids["oc"], ids["it"][0]
    orden = client.get(f"/api/portal/ordenes/{oc}", headers=cab_a).json()
    linea = next(l for l in orden["items"] if l["numero_parte"] == "1F-0001")
    check("§D el cliente ve cuántas fotos tiene su ítem", linea["fotos"] == fotos.FOTOS_MAXIMAS_POR_ITEM, linea["fotos"])
    seguimiento = client.get(f"/api/portal/ordenes/{oc}/items/{item}", headers=cab_a).json()
    check("§D …y el seguimiento las lista con sus medidas y fecha",
          len(seguimiento["fotos"]) == fotos.FOTOS_MAXIMAS_POR_ITEM and seguimiento["fotos"][0]["id"] == foto["foto"]
          and set(seguimiento["fotos"][0]) == {"id", "ancho", "alto", "fecha"}, seguimiento["fotos"][:1])
    r = client.get(f"/api/portal/fotos/{foto['foto']}", headers=cab_a)
    check("§D la imagen llega como JPEG, privada y sin caché compartido",
          r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content[:3] == b"\xff\xd8\xff"
          and r.headers.get("cache-control") == "private, no-store"
          and r.headers.get("x-content-type-options") == "nosniff", dict(r.headers))
    check("§D el perfil bodega también ve las fotos (no son montos)",
          client.get(f"/api/portal/fotos/{foto['foto']}", headers=cab_bod).status_code == 200)
    check("§D otro cliente → 404", client.get(f"/api/portal/fotos/{foto['foto']}", headers=cab_b).status_code == 404)
    check("§D sin sesión → 401", client.get(f"/api/portal/fotos/{foto['foto']}").status_code == 401)
    for raro in ("0", "-1", "999999999"):
        check(f"§D foto {raro} → 404", client.get(f"/api/portal/fotos/{raro}", headers=cab_a).status_code == 404)

    _estado_item(item, "reclamo_proveedor")
    try:
        orden = client.get(f"/api/portal/ordenes/{oc}", headers=cab_a).json()
        linea = next(l for l in orden["items"] if l["numero_parte"] == "1F-0001")
        seguimiento = client.get(f"/api/portal/ordenes/{oc}/items/{item}", headers=cab_a).json()
        check("§D con el ítem en reclamo: ni el contador, ni la lista, ni la imagen",
              linea["fotos"] == 0 and seguimiento["fotos"] == []
              and client.get(f"/api/portal/fotos/{foto['foto']}", headers=cab_a).status_code == 404,
              (linea["fotos"], len(seguimiento["fotos"])))
        check("§D …pero el personal las sigue viendo (no se borraron)",
              len(client.get(f"/api/portal-admin/items/{item}/fotos", headers=cab_op).json()["fotos"])
              == fotos.FOTOS_MAXIMAS_POR_ITEM)
    finally:
        _estado_item(item, "en_bodega")
    check("§D resuelto el reclamo, vuelven a verse",
          client.get(f"/api/portal/fotos/{foto['foto']}", headers=cab_a).status_code == 200)

    # Una foto cuya fila existe pero cuyo archivo se perdió (disco restaurado sin uploads).
    r = _subir(cab_op, ids["it"][4], D.imagen_jpeg())
    perdida = r.json()["id"]
    os.remove(fotos.ruta_de(_archivo_de(perdida)))
    check("§D una foto cuyo archivo ya no está en disco → 404 (no 500)",
          client.get(f"/api/portal/fotos/{perdida}", headers=cab_a).status_code == 404
          and client.get(f"/api/portal-admin/fotos/{perdida}", headers=cab_op).status_code == 404)
    check("§D …y se puede borrar igual", client.delete(f"/api/portal-admin/fotos/{perdida}", headers=cab_op).status_code == 204)


def _paso_e_item_partido(cab_op, cab_a, ids):
    fila_1, fila_2 = ids["it"][1], ids["it"][2]
    a = _subir(cab_op, fila_1, D.imagen_jpeg()).json()["id"]
    b = _subir(cab_op, fila_2, D.imagen_jpeg()).json()["id"]
    orden = client.get(f"/api/portal/ordenes/{ids['oc']}", headers=cab_a).json()
    linea = next(l for l in orden["items"] if l["numero_parte"] == "1F-0002")
    seguimiento = client.get(f"/api/portal/ordenes/{ids['oc']}/items/{linea['id']}", headers=cab_a).json()
    check("§E las fotos subidas a dos filas del mismo ítem se ven juntas en ese ítem",
          linea["fotos"] == 2 and {f["id"] for f in seguimiento["fotos"]} == {a, b}, (linea["fotos"], seguimiento["fotos"]))


def _paso_f_el_archivo_no_viaja(cab_op, cab_a, ids, foto: dict):
    texto = "".join(r.text for r in (
        client.get(f"/api/portal/ordenes/{ids['oc']}", headers=cab_a),
        client.get(f"/api/portal/ordenes/{ids['oc']}/items/{ids['it'][0]}", headers=cab_a),
        client.get(f"/api/portal-admin/items/{ids['it'][0]}/fotos", headers=cab_op),
    ))
    check("§F ni el nombre del archivo ni la carpeta aparecen en ninguna respuesta",
          foto["archivo"] not in texto and foto["archivo"][:32] not in texto and "portal_fotos" not in texto
          and "uploads" not in texto and '"archivo"' not in texto)


def run():
    del _fails[:]
    D.limpiar(MARK)
    try:
        with D.carpetas_de_prueba():
            _paso_a_derivado()
            _paso_a_dos_a_la_vez()
            _paso_b_nombre_y_ruta()
            ids = _sembrar()
            cab_op, cab_monza = _cab(ids["email_op"]), _cab(ids["email_monza"])
            cab_a, cab_b, cab_bod = _cab(ids["email_a"]), _cab(ids["email_b"]), _cab(ids["email_bod"])
            foto = _paso_c_quien_sube(cab_op, cab_monza, cab_a, ids)
            _paso_d_quien_ve(cab_op, cab_a, cab_b, cab_bod, ids, foto)
            _paso_e_item_partido(cab_op, cab_a, ids)
            _paso_f_el_archivo_no_viaja(cab_op, cab_a, ids, foto)
            _paso_c_topes_y_archivos_sueltos(cab_op, ids)
            _paso_c_subidas_simultaneas(cab_op, ids)
            _paso_c_borrar(cab_op, cab_a, ids, foto)
        print()
        print(f"FALLARON {len(_fails)}: {_fails}" if _fails else "SUITE VERDE: fotos del portal")
        assert not _fails, _fails
    finally:
        D.limpiar(MARK)


def test_fotos_portal():
    run()


if __name__ == "__main__":
    run()
