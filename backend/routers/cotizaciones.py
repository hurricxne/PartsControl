import os
import asyncio
import uuid
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, BackgroundTasks
from pydantic import BaseModel
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from typing import List
from database import get_db
from models.models import Cotizacion, ItemCotizacion, PartsCache, EstadoCotizacion, User
from auth import get_current_user
from services.excel_service import parse_excel_cotizacion, generate_result_excel
from services.scraper import scrape_parts_batch
import aiofiles

router = APIRouter(prefix="/cotizaciones", tags=["cotizaciones"])


def _next_cot_numero(db) -> str:
    """Generate next correlative: YYYY-NNNN (e.g. 2026-0001)."""
    import re
    from datetime import datetime
    year = str(datetime.now().year)
    pattern = re.compile(rf"^{re.escape(year)}-(\d+)$")
    all_nums = db.query(Cotizacion.numero).all()
    max_seq = 0
    for (num,) in all_nums:
        m = pattern.match(str(num or ""))
        if m:
            max_seq = max(max_seq, int(m.group(1)))
    return f"{year}-{max_seq + 1:04d}"


UPLOAD_DIR = "uploads"
RESULTS_DIR = "results"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(RESULTS_DIR, exist_ok=True)


async def process_cotizacion(cotizacion_id: int, filepath: str, db_url: str):
    """Tarea en background: scrapea partes y actualiza la BD."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from config import settings

    engine = create_engine(db_url, pool_pre_ping=True)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()

    try:
        cotizacion = db.query(Cotizacion).filter(Cotizacion.id == cotizacion_id).first()
        if not cotizacion:
            return

        cotizacion.estado = EstadoCotizacion.PROCESANDO
        db.commit()

        # Obtener items
        items = db.query(ItemCotizacion).filter(ItemCotizacion.cotizacion_id == cotizacion_id).all()
        part_numbers = [item.numero_parte for item in items]

        # Verificar cache
        cached = {}
        for np in part_numbers:
            cache_entry = db.query(PartsCache).filter(PartsCache.numero_parte == np).first()
            if cache_entry:
                cached[np] = cache_entry

        to_scrape = [np for np in part_numbers if np not in cached]

        # Scraping
        scraped_results = {}
        if to_scrape:
            results = await scrape_parts_batch(to_scrape)
            for r in results:
                np = r["numero_parte"]
                scraped_results[np] = r
                # Guardar en cache
                cache_entry = db.query(PartsCache).filter(PartsCache.numero_parte == np).first()
                if not cache_entry:
                    cache_entry = PartsCache(numero_parte=np)
                    db.add(cache_entry)
                cache_entry.nombre_cat = r.get("nombre_cat")
                cache_entry.precio_cat = r.get("precio_cat")
                cache_entry.moneda_cat = r.get("moneda_cat", "CLP")
                cache_entry.retiro_estimado = r.get("retiro_estimado")
                cache_entry.url_cat = r.get("url_cat")
                cache_entry.imagen_url = r.get("imagen_url")
                cache_entry.encontrado = r.get("encontrado", 0)
                db.commit()

        # Actualizar items con resultados
        items_data_for_excel = []
        encontrados = 0
        for item in items:
            np = item.numero_parte
            if np in cached:
                c = cached[np]
                item.nombre_cat = c.nombre_cat
                item.precio_cat = c.precio_cat
                item.moneda_cat = c.moneda_cat
                item.retiro_estimado = c.retiro_estimado
                item.url_cat = c.url_cat
                item.imagen_url = c.imagen_url
                item.encontrado = c.encontrado
            elif np in scraped_results:
                r = scraped_results[np]
                item.nombre_cat = r.get("nombre_cat")
                item.precio_cat = r.get("precio_cat")
                item.moneda_cat = r.get("moneda_cat", "CLP")
                item.retiro_estimado = r.get("retiro_estimado")
                item.url_cat = r.get("url_cat")
                item.imagen_url = r.get("imagen_url")
                item.encontrado = r.get("encontrado", 0)
                item.scraping_error = r.get("error")

            if item.encontrado:
                encontrados += 1

            cotizacion.items_procesados += 1
            db.commit()

            items_data_for_excel.append({
                "numero_parte": item.numero_parte,
                "nombre_cat": item.nombre_cat,
                "precio_cat": item.precio_cat,
                "moneda_cat": item.moneda_cat,
                "retiro_estimado": item.retiro_estimado,
                "url_cat": item.url_cat,
                "imagen_url": item.imagen_url,
                "encontrado": item.encontrado,
            })

        # Generar Excel resultado
        result_filename = f"resultado_{cotizacion_id}_{uuid.uuid4().hex[:8]}.xlsx"
        result_path = os.path.join(RESULTS_DIR, result_filename)
        generate_result_excel(filepath, items_data_for_excel, result_path)

        cotizacion.archivo_resultado = result_filename
        cotizacion.items_encontrados = encontrados
        cotizacion.estado = EstadoCotizacion.COMPLETADO
        db.commit()

    except Exception as e:
        # Scraping/processing failed but cotizacion and items are valid — keep as COMPLETADO
        try:
            cotizacion = db.query(Cotizacion).filter(Cotizacion.id == cotizacion_id).first()
            if cotizacion:
                cotizacion.estado = EstadoCotizacion.COMPLETADO
                cotizacion.error_msg = str(e)[:500]
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


@router.post("/upload", status_code=202)
async def upload_cotizacion(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Sube un Excel de cotización y dispara el procesamiento en background."""
    if not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="Solo se aceptan archivos .xlsx o .xls")

    # Guardar archivo
    safe_name = f"{uuid.uuid4().hex}_{file.filename}"
    filepath = os.path.join(UPLOAD_DIR, safe_name)
    async with aiofiles.open(filepath, "wb") as f:
        content = await file.read()
        await f.write(content)

    # Parsear Excel
    try:
        header_info, items = parse_excel_cotizacion(filepath)
    except Exception as e:
        os.remove(filepath)
        raise HTTPException(status_code=422, detail=f"Error al leer el Excel: {str(e)}")

    # Crear cotización en BD
    auto_numero = _next_cot_numero(db)
    cotizacion = Cotizacion(
        numero=header_info.get("numero") or auto_numero,
        cliente=header_info.get("cliente"),
        rut_cliente=header_info.get("rut_cliente"),
        contacto_cliente=header_info.get("contacto_cliente"),
        email_cliente=header_info.get("email_cliente"),
        telefono_cliente=header_info.get("telefono_cliente"),
        direccion_cliente=header_info.get("direccion_cliente"),
        referencia=header_info.get("referencia"),
        archivo_original=safe_name,
        total_items=len(items),
        items_procesados=0,
        estado=EstadoCotizacion.PENDIENTE,
        user_id=current_user.id,
    )
    db.add(cotizacion)
    db.flush()

    # Crear items
    for item_data in items:
        item = ItemCotizacion(
            cotizacion_id=cotizacion.id,
            **item_data
        )
        db.add(item)

    db.commit()
    db.refresh(cotizacion)

    # Disparar procesamiento en background
    from config import settings
    background_tasks.add_task(
        asyncio.run,
        process_cotizacion(cotizacion.id, filepath, settings.DATABASE_URL)
    )

    return {
        "id": cotizacion.id,
        "numero": cotizacion.numero,
        "cliente": cotizacion.cliente,
        "total_items": cotizacion.total_items,
        "estado": cotizacion.estado,
        "message": f"Cotización {cotizacion.numero} recibida. Procesando {len(items)} partes..."
    }


@router.get("/")
def list_cotizaciones(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    cotizaciones = (
        db.query(Cotizacion)
        .filter(Cotizacion.user_id == current_user.id)
        .order_by(Cotizacion.created_at.desc())
        .limit(50)
        .all()
    )
    return [
        {
            "id": c.id,
            "numero": c.numero,
            "cliente": c.cliente,
            "referencia": c.referencia,
            "total_items": c.total_items,
            "items_procesados": c.items_procesados,
            "items_encontrados": c.items_encontrados,
            "estado": c.estado,
            "fase_comercial": c.fase_comercial,
            "es_formal": bool(c.es_formal),
            "created_at": c.created_at,
            "tiene_resultado": bool(c.archivo_resultado),
        }
        for c in cotizaciones
    ]




@router.get("/clientes")
def listar_clientes_cotizacion(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Lista clientes únicos de cotizaciones."""
    rows = db.query(Cotizacion.cliente, Cotizacion.rut_cliente).filter(
        Cotizacion.cliente.isnot(None)
    ).distinct().all()
    return [{"cliente": r.cliente, "rut_cliente": r.rut_cliente or ""} for r in rows if r.cliente]

@router.get("/{cotizacion_id}")
def get_cotizacion(
    cotizacion_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    c = db.query(Cotizacion).filter(
        Cotizacion.id == cotizacion_id,
        Cotizacion.user_id == current_user.id
    ).first()
    if not c:
        raise HTTPException(status_code=404, detail="Cotización no encontrada")

    items = db.query(ItemCotizacion).filter(ItemCotizacion.cotizacion_id == cotizacion_id).all()
    return {
        "id": c.id,
        "numero": c.numero,
        "cliente": c.cliente,
        "referencia": c.referencia,
        "archivo_original": c.archivo_original,
        "total_items": c.total_items,
        "items_procesados": c.items_procesados,
        "items_encontrados": c.items_encontrados,
        "estado": c.estado,
        "error_msg": c.error_msg,
        "created_at": c.created_at,
        "tiene_resultado": bool(c.archivo_resultado),
        "items": [
            {
                "id": item.id,
                "item_num": item.item_num,
                "descripcion": item.descripcion,
                "numero_parte": item.numero_parte,
                "marca": item.marca,
                "cantidad": item.cantidad,
                "precio_unit_cotizacion": item.precio_unit_cotizacion,
                "nombre_cat": item.nombre_cat,
                "precio_cat": item.precio_cat,
                "moneda_cat": item.moneda_cat,
                "retiro_estimado": item.retiro_estimado,
                "url_cat": item.url_cat,
                "encontrado": item.encontrado,
                "scraping_error": item.scraping_error,
            }
            for item in items
        ],
    }


@router.get("/{cotizacion_id}/status")
def get_cotizacion_status(
    cotizacion_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    c = db.query(Cotizacion).filter(
        Cotizacion.id == cotizacion_id,
        Cotizacion.user_id == current_user.id
    ).first()
    if not c:
        raise HTTPException(status_code=404, detail="No encontrada")
    return {
        "id": c.id,
        "estado": c.estado,
        "total_items": c.total_items,
        "items_procesados": c.items_procesados,
        "items_encontrados": c.items_encontrados,
        "progreso": round((c.items_procesados / c.total_items) * 100) if c.total_items else 0,
        "error_msg": c.error_msg,
    }


@router.get("/{cotizacion_id}/download")
def download_result(
    cotizacion_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    c = db.query(Cotizacion).filter(
        Cotizacion.id == cotizacion_id,
        Cotizacion.user_id == current_user.id
    ).first()
    if not c or not c.archivo_resultado:
        raise HTTPException(status_code=404, detail="Resultado no disponible aún")

    path = os.path.join(RESULTS_DIR, c.archivo_resultado)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Archivo no encontrado")

    filename = f"COT-{c.numero}_resultado_CAT.xlsx"
    return FileResponse(path, filename=filename, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ── Endpoints adicionales ─────────────────────────────────────────────────────



@router.patch("/{cotizacion_id}/fase")
def actualizar_fase(
    cotizacion_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Actualiza la fase comercial de una cotización."""
    from pydantic import BaseModel
    cot = db.query(Cotizacion).filter(Cotizacion.id == cotizacion_id).first()
    if not cot:
        raise HTTPException(404, "Cotización no encontrada")
    fase = body.get("fase")
    if not fase:
        raise HTTPException(400, "Falta el campo 'fase'")
    cot.fase_comercial = fase
    db.commit()
    return {"ok": True, "fase": fase}


@router.post("/{cotizacion_id}/reprocesar", status_code=202)
def reprocesar_cotizacion(
    cotizacion_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Vuelve a disparar el proceso de scraping para una cotización."""
    cot = db.query(Cotizacion).filter(Cotizacion.id == cotizacion_id).first()
    if not cot:
        raise HTTPException(404, "Cotización no encontrada")
    if cot.estado in ("PROCESANDO",):
        raise HTTPException(400, "La cotización ya está en proceso")
    cot.estado = "PROCESANDO"
    cot.items_procesados = 0
    cot.items_encontrados = 0
    cot.error_msg = None
    db.commit()
    import asyncio
    background_tasks.add_task(asyncio.run, process_cotizacion(cotizacion_id))
    return {"ok": True, "message": "Reprocesamiento iniciado"}


@router.delete("/{cotizacion_id}", status_code=204)
def eliminar_cotizacion(
    cotizacion_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Elimina una cotización y sus items."""
    cot = db.query(Cotizacion).filter(Cotizacion.id == cotizacion_id).first()
    if not cot:
        raise HTTPException(404, "Cotización no encontrada")
    # Delete items first
    db.query(ItemCotizacion).filter(ItemCotizacion.cotizacion_id == cotizacion_id).delete()
    db.delete(cot)
    db.commit()
    return None


class ManualItemRequest(BaseModel):
    numero_parte: str
    descripcion: Optional[str] = None
    marca: Optional[str] = None
    cantidad: float = 1
    precio_unit_cotizacion: Optional[float] = None
    peso_unit_lbs: Optional[float] = None
    plazo_entrega_min: Optional[int] = None
    plazo_entrega_max: Optional[int] = None


class ManualCotizacionRequest(BaseModel):
    cliente: Optional[str] = None
    referencia: Optional[str] = None
    rut_cliente: Optional[str] = None
    contacto_cliente: Optional[str] = None
    email_cliente: Optional[str] = None
    terminos_condiciones: Optional[str] = None
    items: List[ManualItemRequest]


@router.post("/manual", status_code=201)
def crear_cotizacion_manual(
    body: ManualCotizacionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Crea una cotización manualmente sin Excel."""
    from datetime import datetime
    # Generate numero
    numero = _next_cot_numero(db)

    cot = Cotizacion(
        numero=numero,
        cliente=body.cliente,
        referencia=body.referencia,
        rut_cliente=body.rut_cliente,
        contacto_cliente=body.contacto_cliente,
        email_cliente=body.email_cliente,
        terminos_condiciones=body.terminos_condiciones,
        total_items=len(body.items),
        items_procesados=len(body.items),
        items_encontrados=0,
        estado="COMPLETADO",
        user_id=current_user.id,
    )
    db.add(cot)
    db.flush()

    for i, it in enumerate(body.items, 1):
        item = ItemCotizacion(
            cotizacion_id=cot.id,
            item_num=i,
            numero_parte=it.numero_parte,
            descripcion=it.descripcion,
            marca=it.marca,
            cantidad=it.cantidad,
            precio_unit_cotizacion=it.precio_unit_cotizacion,
            total_cotizacion=(it.precio_unit_cotizacion or 0) * it.cantidad if it.precio_unit_cotizacion else None,
            peso_unit_lbs=it.peso_unit_lbs,
            plazo_entrega_min=it.plazo_entrega_min,
            plazo_entrega_max=it.plazo_entrega_max,
            estado_item="ingresado",
            encontrado=0,
        )
        db.add(item)

    db.commit()
    db.refresh(cot)
    return {"id": cot.id, "numero": cot.numero}
