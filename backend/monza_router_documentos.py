"""Documentos adjuntos genericos para MonzaParts (cotizacion, OC proveedor, item, etc.)."""
import os
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from datetime import datetime
from typing import Optional

from database import get_db
from auth import get_current_user
from monza_models import MonzaDocumento

router = APIRouter(prefix="/api/monza/documentos", tags=["monza-documentos"])

DOCS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "docs")


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
    os.makedirs(DOCS_DIR, exist_ok=True)
    ext = os.path.splitext(file.filename)[1] if file.filename else ""
    fname = f"{entidad}_{entidad_id}_{int(datetime.utcnow().timestamp()*1000)}{ext}"
    dest = os.path.join(DOCS_DIR, fname)
    content = await file.read()
    with open(dest, "wb") as f:
        f.write(content)
    doc = MonzaDocumento(
        entidad=entidad, entidad_id=entidad_id, categoria=categoria,
        filename=fname, original_name=file.filename, content_type=file.content_type,
        uploaded_by=current_user.email,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return _dict(doc)


@router.get("/{doc_id}/download")
def download(doc_id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    d = db.query(MonzaDocumento).filter(MonzaDocumento.id == doc_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Documento no encontrado")
    path = os.path.join(DOCS_DIR, d.filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    return FileResponse(path, filename=d.original_name or d.filename, media_type=d.content_type or "application/octet-stream")


@router.delete("/{doc_id}")
def borrar(doc_id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    d = db.query(MonzaDocumento).filter(MonzaDocumento.id == doc_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="No encontrado")
    try:
        os.remove(os.path.join(DOCS_DIR, d.filename))
    except Exception:
        pass
    db.delete(d)
    db.commit()
    return {"ok": True}
