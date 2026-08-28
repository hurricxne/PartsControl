"""Documentos adjuntos genericos para MonzaParts (cotizacion, OC proveedor, item, etc.).

ENDURECIDO 2026-08-27 (dos hallazgos CRITICOS del equipo de testing, reproducidos):

 1. ESCAPE DE CARPETA + XSS ALMACENADO. El nombre del archivo se armaba con el campo
    `entidad` que manda el formulario (`f"{entidad}_{entidad_id}_{ts}{ext}"`), así que
    una `entidad` con "../.." resolvía FUERA de static/docs. Enviándola apuntada a
    `uploads/bodega` —directorio que main.py publica como StaticFiles— el atacante
    dejaba un .html con <script> servido por el PROPIO dominio de la aplicación, o sea
    robo de sesión de los operadores de las DOS marcas. Ahora el nombre lo genera el
    servidor (uuid4 + extensión de lista blanca) y el destino se resuelve y confina
    contra DOCS_DIR: el dato del usuario ya no participa de la ruta, ni siquiera
    indirectamente.

 2. SIN CANDADO DE EMPRESA. Una cuenta de la marca minería subía, listaba y DESCARGABA
    los adjuntos de MonzaParts. El router entero va detrás de require_empresa.

Además se cerró el huérfano en disco: el archivo se escribía ANTES del INSERT, así que
un commit fallido (p.ej. `entidad` más larga que su columna) dejaba el archivo plantado
igual. Ahora se valida todo primero y el disco se limpia si el commit falla.

El formato del nombre (32 hex + extensión) es el MISMO del upload de guía firmada
(monza_router_despachos.py) y del repositorio de Grupo AM: un solo vocabulario de
nombres en toda la casa.
"""
import mimetypes
import os
import re
import uuid
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from datetime import datetime
from typing import Optional

from database import get_db
from auth import get_current_user
from empresa_guard import require_empresa
from monza_models import MonzaDocumento

router = APIRouter(
    prefix="/api/monza/documentos",
    tags=["monza-documentos"],
    # Los adjuntos llevan facturas, guías y OC escaneadas de MonzaParts: mismo candado
    # que el resto de los routers de la marca.
    dependencies=[Depends(require_empresa("automotriz"))],
)

DOCS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "docs")

# Extensiones que un adjunto de negocio puede tener. Lista BLANCA a propósito: lo que
# no está listado no entra, en vez de intentar enumerar lo peligroso y olvidar uno.
# Fuera quedan .html/.htm/.svg/.xhtml/.js — los que un navegador ejecutaría.
_EXT_PERMITIDAS = {
    ".pdf", ".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif", ".bmp", ".tif", ".tiff",
    ".xls", ".xlsx", ".xlsm", ".csv", ".doc", ".docx", ".txt", ".zip", ".rar", ".7z",
    ".eml", ".msg", ".xml",
}
_MAX_DOC_BYTES = 20 * 1024 * 1024  # 20 MB (espejo del upload de guía firmada)

# `entidad` es un slug interno del sistema (cotizacion, oc_proveedor, item, lead), no
# texto libre del operador. Validar la FORMA en vez de enumerar el catálogo cerrado deja
# crecer el módulo sin tocar este guard, y descarta de plano cualquier separador de ruta.
# El tope de 30 es el ancho de la columna: sin este corte el commit reventaba con un 500
# en vez de un 400 que el operador entiende.
_ENTIDAD_RE = re.compile(r"^[a-z][a-z0-9_]{0,29}$")
# `categoria` NO es un slug: es la ETIQUETA que el operador elige en el selector, y las
# pantallas mandan texto legible con tildes, espacios, paréntesis y mayúsculas
# («guía de despacho», «AWB (guía aérea)», «packing list», «OC»). Una versión anterior de
# este guard la validaba con el mismo patrón que `entidad` y rechazaba casi todas: subir
# un documento quedó roto en media aplicación. No necesita forma, porque NO participa de
# la ruta del archivo — lo único que hay que respetar es el ancho de su columna.
_CATEGORIA_MAX = 40
# Formato del nombre ya guardado en la fila. Las filas nuevas SIEMPRE lo cumplen (las
# genera uuid4); se valida igual al leer porque una fila vieja o adulterada no debe
# poder dirigir un open() ni un remove() fuera del repositorio.
_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,255}$")


def _ruta_confinada(filename: str) -> str:
    """Ruta absoluta del documento dentro de DOCS_DIR, o 400 si intenta salirse.

    Dos capas, igual que `serve_doc` de despachos: formato estricto del nombre y
    resolución del path final contra el directorio. La segunda capa existe porque la
    primera es una lista de caracteres y basta una coma de más en el futuro para que
    deje pasar algo; el confinamiento es el que no se puede negociar.
    """
    if not filename or not _FILENAME_RE.match(filename):
        raise HTTPException(status_code=400, detail="Nombre de archivo inválido")
    ruta = os.path.abspath(os.path.join(DOCS_DIR, filename))
    if not ruta.startswith(os.path.abspath(DOCS_DIR) + os.sep):
        raise HTTPException(status_code=400, detail="Ruta inválida")
    return ruta


def _dict(d: MonzaDocumento) -> dict:
    return {
        "id": d.id,
        "entidad": d.entidad,
        "entidad_id": d.entidad_id,
        "categoria": d.categoria,
        "filename": d.filename,
        "original_name": d.original_name,
        "content_type": d.content_type,
        "uploaded_by": d.uploaded_by,
        "fecha": d.fecha.isoformat() if d.fecha else None,
    }


@router.get("")
def listar(
    entidad: str = Query(...),
    entidad_id: int = Query(...),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    docs = (
        db.query(MonzaDocumento)
        .filter(MonzaDocumento.entidad == entidad, MonzaDocumento.entidad_id == entidad_id)
        .order_by(MonzaDocumento.id.desc())
        .all()
    )
    return [_dict(d) for d in docs]


@router.post("/upload")
async def upload(
    entidad: str = Form(...),
    entidad_id: int = Form(...),
    categoria: str = Form("otro"),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    # ── Todo se valida ANTES de tocar el disco: un 400 no debe dejar archivos sueltos
    # (mismo criterio que el upload de la guía firmada).
    entidad = (entidad or "").strip().lower()
    if not _ENTIDAD_RE.match(entidad):
        raise HTTPException(
            status_code=400,
            detail="Tipo de documento inválido: usa un identificador simple "
                   "(letras, números y guion bajo, máximo 30 caracteres).",
        )
    categoria = (categoria or "").strip() or "otro"
    if len(categoria) > _CATEGORIA_MAX:
        # Se corta con un 400 claro en vez de dejar que el commit reviente con un 500
        # después de haber escrito el archivo (mismo criterio que el resto del endpoint).
        raise HTTPException(
            status_code=400,
            detail=f"La categoría no puede superar los {_CATEGORIA_MAX} caracteres.",
        )

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in _EXT_PERMITIDAS:
        raise HTTPException(
            status_code=400,
            detail=f"Tipo de archivo no permitido: '{ext or 'sin extensión'}'. "
                   "Acepta PDF, imágenes, Excel, Word, CSV, TXT y comprimidos.",
        )

    # El tope se decide ANTES de materializar el archivo en memoria. `file.size` lo cuenta
    # STARLETTE del lado del servidor mientras vuelca el cuerpo a su temporal, así que no
    # es un dato del cliente (que podría mentir) sino el tamaño real ya recibido. Con el
    # `read()` primero, el gasto de RAM ya estaba hecho cuando se evaluaba el límite: una
    # subida de 150 MB reservaba los 150 MB antes de responder «máximo 20 MB».
    if (file.size or 0) > _MAX_DOC_BYTES:
        raise HTTPException(status_code=400, detail="Archivo demasiado grande (máximo 20 MB)")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="El archivo llegó vacío: súbelo de nuevo")
    if len(content) > _MAX_DOC_BYTES:
        # Cinturón: si una versión de starlette no informara `size`, el corte sigue
        # existiendo aunque llegue tarde.
        raise HTTPException(status_code=400, detail="Archivo demasiado grande (máximo 20 MB)")

    # El nombre lo genera el SERVIDOR. Nada de lo que mandó el cliente entra en la ruta:
    # ni `entidad`, ni el nombre original del archivo. `original_name` conserva el nombre
    # que el operador reconoce, pero vive en la columna, no en el sistema de archivos.
    fname = f"{uuid.uuid4().hex}{ext}"
    dest = _ruta_confinada(fname)
    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(dest, "wb") as f:
        f.write(content)

    doc = MonzaDocumento(
        entidad=entidad, entidad_id=entidad_id, categoria=categoria,
        filename=fname, original_name=(file.filename or "")[:255] or None,
        content_type=(file.content_type or "")[:100] or None,
        uploaded_by=current_user.email,
    )
    db.add(doc)
    try:
        db.commit()
    except Exception:
        # Sin esto, un commit fallido dejaba el archivo plantado en disco para siempre:
        # un adjunto que la aplicación no sabe que existe y nadie puede borrar.
        db.rollback()
        try:
            os.remove(dest)
        except OSError:
            pass
        raise
    db.refresh(doc)
    return _dict(doc)


@router.get("/{doc_id}/download")
def download(doc_id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    d = db.query(MonzaDocumento).filter(MonzaDocumento.id == doc_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Documento no encontrado")
    path = _ruta_confinada(d.filename)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    # `filename=` hace que la respuesta baje como adjunto (Content-Disposition:
    # attachment) en vez de renderizarse: el navegador nunca ejecuta lo que descarga,
    # aunque el content_type guardado venga del cliente.
    return FileResponse(
        path,
        filename=d.original_name or d.filename,
        # El media type se deriva del NOMBRE que generó el servidor (uuid4 + extensión de
        # la lista blanca), no del `content_type` que mandó el cliente: ese viaja tal cual
        # a una cabecera HTTP, y un byte de control adentro deja el adjunto imposible de
        # descargar para siempre. Además repara las filas ya envenenadas sin migración.
        media_type=mimetypes.guess_type(d.filename)[0] or "application/octet-stream",
    )


@router.delete("/{doc_id}")
def borrar(doc_id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    d = db.query(MonzaDocumento).filter(MonzaDocumento.id == doc_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="No encontrado")
    path = _ruta_confinada(d.filename)
    try:
        os.remove(path)
    except OSError:
        # Solo se ignora el fallo del SISTEMA DE ARCHIVOS (el archivo ya no está, o el
        # permiso cambió): la fila se borra igual porque el documento deja de existir
        # para el negocio. Un except pelado, en cambio, se habría tragado también los
        # errores de la resolución del path.
        pass
    db.delete(d)
    db.commit()
    return {"ok": True}
