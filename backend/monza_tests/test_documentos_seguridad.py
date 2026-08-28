"""Adjuntos de MonzaParts: la carpeta no se escapa y la marca contraria no entra.

Nace de dos hallazgos CRITICOS del equipo de testing (2026-08-27), ambos reproducidos
contra el código vivo:

  * El nombre del archivo se armaba con el campo `entidad` del formulario, así que una
    entidad con "../.." escribía FUERA de static/docs. Apuntada a `uploads/bodega`
    —que main.py publica como StaticFiles— dejaba un .html con <script> servido por el
    propio dominio de la app: robo de sesión de los operadores de las DOS marcas.
  * El router no tenía candado de empresa: una cuenta de minería subía, listaba y
    descargaba los adjuntos de MonzaParts.

Las dos primeras pruebas son SONDAS DE PODER DISCRIMINANTE: si alguien devuelve el
nombre de archivo a `f"{entidad}_..."`, `test_ataque_escape_de_carpeta` vuelve a rojo
porque comprueba el disco, no el status code.
"""
import io
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth import get_current_user
from database import SessionLocal
from monza_models import MonzaDocumento
import monza_router_documentos as mod

MARK = "test-doc-seg"
ENTIDAD = "cotizacion"
ENTIDAD_ID = 99000001


class _Usuario:
    def __init__(self, empresa="automotriz"):
        self.id = 1
        self.email = f"{MARK}@test.invalid"
        self.empresa = empresa
        self.rol = "admin"


def _app(empresa="automotriz"):
    app = FastAPI()
    app.include_router(mod.router)
    app.dependency_overrides[get_current_user] = lambda: _Usuario(empresa)
    return TestClient(app)


def _limpiar():
    db = SessionLocal()
    for d in db.query(MonzaDocumento).filter(MonzaDocumento.uploaded_by == f"{MARK}@test.invalid").all():
        try:
            os.remove(os.path.join(mod.DOCS_DIR, d.filename))
        except OSError:
            pass
        db.delete(d)
    db.commit()
    db.close()


@pytest.fixture(autouse=True)
def limpieza():
    _limpiar()
    yield
    _limpiar()


def _subir(cli, nombre="orden.pdf", entidad=ENTIDAD, contenido=b"%PDF-1.4 contenido"):
    return cli.post(
        "/api/monza/documentos/upload",
        data={"entidad": entidad, "entidad_id": ENTIDAD_ID, "categoria": "otro"},
        files={"file": (nombre, io.BytesIO(contenido), "application/pdf")},
    )


# ─────────────────────────── 1. El escape de carpeta ───────────────────────────

def test_ataque_escape_de_carpeta():
    """SONDA: el ataque del equipo de testing, verificado CONTRA EL DISCO.

    OJO CON LA EXTENSIÓN: el ataque original subía un .html, pero hoy hay DOS defensas
    encadenadas y la lista blanca de extensiones rebota el .html antes de que el nombre
    del archivo llegue a construirse. Una sonda con .html pasaría en verde incluso con
    el bug del nombre de vuelta —chocaría con la primera capa y dejaría la segunda sin
    probar—, así que acá se usa una extensión PERMITIDA: la única barrera que puede
    detener este caso es la que se está probando. Verificado por mutación: devolviendo
    `fname = f"{entidad}_..."` esta prueba se pone roja.

    Y no basta con mirar el status: lo que importaba es DÓNDE aterriza el archivo, así
    que la prueba fotografía el directorio publicado antes y después.
    """
    cli = _app()
    bodega = os.path.abspath(os.path.join(os.path.dirname(mod.DOCS_DIR), "..", "uploads", "bodega"))
    antes = set(os.listdir(bodega)) if os.path.isdir(bodega) else set()

    r = _subir(cli, nombre="inocente.pdf", entidad="../../uploads/bodega/R2A",
               contenido=b"%PDF-1.4 payload")

    assert r.status_code == 400, f"el escape de carpeta ya no debe aceptarse: {r.text}"
    despues = set(os.listdir(bodega)) if os.path.isdir(bodega) else set()
    assert antes == despues, f"quedaron archivos plantados fuera del repositorio: {despues - antes}"
    # El repositorio propio tampoco debe haber recibido nada.
    assert not any(f.startswith("..") for f in os.listdir(mod.DOCS_DIR))


def test_ataque_escape_de_carpeta_con_html():
    """El ataque TAL CUAL lo reportó el testing (.html): rebota, y por partida doble."""
    cli = _app()
    bodega = os.path.abspath(os.path.join(os.path.dirname(mod.DOCS_DIR), "..", "uploads", "bodega"))
    antes = set(os.listdir(bodega)) if os.path.isdir(bodega) else set()
    r = _subir(cli, nombre="R2A.html", entidad="../../uploads/bodega/R2A",
               contenido=b"<script>alert(document.cookie)</script>")
    assert r.status_code == 400, r.text
    despues = set(os.listdir(bodega)) if os.path.isdir(bodega) else set()
    assert antes == despues, f"quedaron archivos plantados fuera del repositorio: {despues - antes}"


def test_entidad_con_separadores_de_ruta_rechazada():
    """Cualquier forma de meter una ruta en `entidad`, no solo la del ataque original."""
    cli = _app()
    for entidad in ("../otro", "a/b", "a\\b", "..", "/etc/passwd", "COTIZACION!", "x" * 31):
        r = _subir(cli, entidad=entidad)
        assert r.status_code == 400, f"entidad '{entidad}' debió rebotar, dio {r.status_code}"


def test_el_nombre_guardado_no_contiene_nada_del_cliente():
    """El nombre lo genera el servidor: 32 hex + extensión, sin rastro de lo enviado."""
    cli = _app()
    r = _subir(cli, nombre="Factura del cliente (copia).pdf")
    assert r.status_code == 200, r.text
    fname = r.json()["filename"]
    assert fname.endswith(".pdf")
    assert len(fname) == 36 and fname[:32].isalnum(), f"nombre inesperado: {fname}"
    assert ENTIDAD not in fname and "Factura" not in fname
    # El nombre que el operador reconoce sigue existiendo, pero en la columna.
    assert r.json()["original_name"] == "Factura del cliente (copia).pdf"


# ─────────────────────────── 2. Lo que se puede subir ───────────────────────────

def test_html_y_svg_rechazados():
    """Los formatos que un navegador EJECUTA no entran, aunque ya no puedan salirse
    de la carpeta: defensa en profundidad barata."""
    cli = _app()
    for nombre in ("pagina.html", "pagina.htm", "dibujo.svg", "script.js", "sin_extension"):
        r = _subir(cli, nombre=nombre)
        assert r.status_code == 400, f"'{nombre}' debió rebotar, dio {r.status_code}"


def test_archivo_vacio_y_gigante_rechazados():
    cli = _app()
    assert _subir(cli, contenido=b"").status_code == 400
    grande = b"x" * (mod._MAX_DOC_BYTES + 1)
    assert _subir(cli, contenido=grande).status_code == 400


# ─────────────────────────── 3. El candado de empresa ───────────────────────────

def test_mineria_no_entra_por_ninguna_puerta():
    """SONDA del segundo hallazgo: las cuatro rutas rebotan a la marca contraria."""
    propio = _app()
    r = _subir(propio)
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]

    ajeno = _app(empresa="mineria")
    assert ajeno.get("/api/monza/documentos",
                     params={"entidad": ENTIDAD, "entidad_id": ENTIDAD_ID}).status_code == 403
    assert _subir(ajeno).status_code == 403
    assert ajeno.get(f"/api/monza/documentos/{doc_id}/download").status_code == 403
    assert ajeno.delete(f"/api/monza/documentos/{doc_id}").status_code == 403
    # Y sin empresa declarada tampoco: el guard no tiene default mágico.
    sin_empresa = _app(empresa=None)
    assert _subir(sin_empresa).status_code == 403


# ─────────────────────────── 4. El flujo que sí debe andar ───────────────────────────

def test_ciclo_completo_subir_listar_descargar_borrar():
    cli = _app()
    r = _subir(cli, nombre="guia escaneada.pdf", contenido=b"%PDF-1.4 real")
    assert r.status_code == 200, r.text
    doc_id, fname = r.json()["id"], r.json()["filename"]
    assert os.path.isfile(os.path.join(mod.DOCS_DIR, fname))

    listado = cli.get("/api/monza/documentos",
                      params={"entidad": ENTIDAD, "entidad_id": ENTIDAD_ID})
    assert listado.status_code == 200 and any(d["id"] == doc_id for d in listado.json())

    bajada = cli.get(f"/api/monza/documentos/{doc_id}/download")
    assert bajada.status_code == 200 and bajada.content == b"%PDF-1.4 real"
    # Baja como ADJUNTO: el navegador no renderiza lo que descarga.
    assert "attachment" in bajada.headers.get("content-disposition", "")

    assert cli.delete(f"/api/monza/documentos/{doc_id}").status_code == 200
    assert not os.path.exists(os.path.join(mod.DOCS_DIR, fname)), "el archivo quedó huérfano en disco"


def test_commit_fallido_no_deja_el_archivo_plantado(monkeypatch):
    """El archivo se escribía ANTES del INSERT: si el commit reventaba, quedaba igual.

    Se simula el commit fallido en vez de provocarlo con una entidad larga, porque esa
    puerta ya la cierra el 400 de arriba — y el huérfano debe estar cubierto para
    CUALQUIER causa de fallo, no solo la que encontró el testing.
    """
    cli = _app()
    antes = set(os.listdir(mod.DOCS_DIR)) if os.path.isdir(mod.DOCS_DIR) else set()

    from sqlalchemy.orm import Session as _S
    original = _S.commit
    monkeypatch.setattr(_S, "commit", lambda self: (_ for _ in ()).throw(RuntimeError("BD caída")))
    with pytest.raises(RuntimeError):
        _subir(cli)
    monkeypatch.setattr(_S, "commit", original)

    despues = set(os.listdir(mod.DOCS_DIR)) if os.path.isdir(mod.DOCS_DIR) else set()
    assert antes == despues, f"archivo huérfano tras el commit fallido: {despues - antes}"
