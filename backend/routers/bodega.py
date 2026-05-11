import os
import shutil
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session
from pydantic import BaseModel

from database import get_db
from auth import get_current_user
from models.models import (
    User, Embarque, EmbarqueItem, ItemCotizacion, OcCliente, OcProveedor,
    OcProveedorItem, FacturaProveedor, FacturaProveedorItem,
    RecepcionEmbarque, RecepcionEmbarqueItem, RecepcionItemFoto, ReclamoProveedor,
    Notificacion,
)
from notificaciones import crear_notificacion

router = APIRouter(prefix="/bodega", tags=["bodega"])

UPLOAD_DIR = "uploads/bodega"
os.makedirs(UPLOAD_DIR, exist_ok=True)


# ── HELPERS ────────────────────────────────────────────────────────────────────

def _item_detail(ei: EmbarqueItem, db: Session) -> dict:
    """Build detail dict for an EmbarqueItem (no CLP pricing)."""
    item = ei.item_cotizacion
    if not item:
        return {"embarque_item_id": ei.id}

    # Get unit_price_usd from FacturaProveedorItem
    asig = db.query(OcProveedorItem).filter(
        OcProveedorItem.item_cotizacion_id == item.id
    ).first()
    unit_usd = None
    invoice_no = None
    if asig:
        fp_item = db.query(FacturaProveedorItem).filter(
            FacturaProveedorItem.ocp_item_id == asig.id
        ).first()
        if fp_item:
            unit_usd = float(fp_item.unit_price_usd) if fp_item.unit_price_usd else None
            fp = db.query(FacturaProveedor).filter(
                FacturaProveedor.id == fp_item.factura_id
            ).first()
            invoice_no = fp.invoice_no if fp else None

    # OC Cliente info
    occ = (
        db.query(OcCliente).filter(OcCliente.id == asig.oc_cliente_id).first()
        if asig and asig.oc_cliente_id
        else None
    )

    # OC Proveedor
    ocp = (
        db.query(OcProveedor).filter(OcProveedor.id == asig.oc_proveedor_id).first()
        if asig
        else None
    )

    qty = float(item.cantidad or 1)
    total_usd = round(unit_usd * qty, 4) if unit_usd else None
    peso_kg = round(float(item.peso_unit_lbs or 0) * 0.453592 * qty, 3)

    return {
        "embarque_item_id": ei.id,
        "item_cotizacion_id": item.id,
        "numero_parte": item.numero_parte or "",
        "descripcion": item.descripcion or item.nombre_cat or "",
        "marca": item.marca or "",
        "cantidad": qty,
        "peso_kg": peso_kg,
        "plazo_entrega_max": item.plazo_entrega_max,
        "unit_price_usd": unit_usd,
        "total_usd": total_usd,
        "numero_oc_cliente": occ.numero_oc if occ else None,
        "invoice_no": invoice_no,
        "ocp_numero": ocp.numero if ocp else None,
        "ocp_proveedor": ocp.proveedor if ocp else None,
    }


# ── LIST EMBARQUES ─────────────────────────────────────────────────────────────

@router.get("/embarques")
def list_embarques_bodega(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Returns embarques in en_transito or en_recepcion states."""
    embarques = (
        db.query(Embarque)
        .filter(Embarque.estado.in_(["en_transito", "en_recepcion"]))
        .order_by(Embarque.created_at.desc())
        .all()
    )
    result = []
    for e in embarques:
        recepcion = db.query(RecepcionEmbarque).filter(
            RecepcionEmbarque.embarque_id == e.id
        ).first()
        total_items = db.query(EmbarqueItem).filter(EmbarqueItem.embarque_id == e.id).count()
        marcados = 0
        if recepcion:
            marcados = db.query(RecepcionEmbarqueItem).filter(
                RecepcionEmbarqueItem.recepcion_id == recepcion.id,
                RecepcionEmbarqueItem.estado_recepcion.isnot(None),
            ).count()
        result.append({
            "id": e.id,
            "numero": e.numero,
            "estado": e.estado,
            "forwarder": e.forwarder,
            "awb": e.awb,
            "fecha_llegada_est": str(e.fecha_llegada_est) if e.fecha_llegada_est else None,
            "notas": e.notas,
            "total_items": total_items,
            "items_marcados": marcados,
            "recepcion_id": recepcion.id if recepcion else None,
            "recepcion_estado": recepcion.estado if recepcion else None,
        })
    return result


@router.get("/embarques/historial")
def historial_embarques(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    embarques = (
        db.query(Embarque)
        .filter(Embarque.estado == "recibido")
        .order_by(Embarque.updated_at.desc())
        .limit(50)
        .all()
    )
    return [
        {
            "id": e.id,
            "numero": e.numero,
            "estado": e.estado,
            "forwarder": e.forwarder,
            "awb": e.awb,
            "fecha_llegada_est": str(e.fecha_llegada_est) if e.fecha_llegada_est else None,
        }
        for e in embarques
    ]


@router.get("/embarques/{embarque_id}")
def get_embarque_bodega(
    embarque_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    e = db.query(Embarque).filter(Embarque.id == embarque_id).first()
    if not e:
        raise HTTPException(404, "Embarque no encontrado")

    recepcion = db.query(RecepcionEmbarque).filter(
        RecepcionEmbarque.embarque_id == e.id
    ).first()

    items_db = db.query(EmbarqueItem).filter(EmbarqueItem.embarque_id == e.id).all()
    items_out = [_item_detail(ei, db) for ei in items_db]

    return {
        "id": e.id,
        "numero": e.numero,
        "estado": e.estado,
        "forwarder": e.forwarder,
        "awb": e.awb,
        "fecha_llegada_est": str(e.fecha_llegada_est) if e.fecha_llegada_est else None,
        "notas": e.notas,
        "items": items_out,
        "recepcion": {
            "id": recepcion.id,
            "estado": recepcion.estado,
            "fecha_inicio": recepcion.fecha_inicio.isoformat() if recepcion.fecha_inicio else None,
            "fecha_cierre": recepcion.fecha_cierre.isoformat() if recepcion.fecha_cierre else None,
            "observacion_general": recepcion.observacion_general,
        } if recepcion else None,
    }


# ── INICIAR RECEPCION ──────────────────────────────────────────────────────────

@router.post("/embarques/{embarque_id}/recibir", status_code=201)
def iniciar_recepcion(
    embarque_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    e = db.query(Embarque).filter(Embarque.id == embarque_id).first()
    if not e:
        raise HTTPException(404, "Embarque no encontrado")
    if e.estado not in ("en_transito", "en_recepcion"):
        raise HTTPException(400, f"Embarque en estado '{e.estado}', no se puede iniciar recepción")

    existing = db.query(RecepcionEmbarque).filter(
        RecepcionEmbarque.embarque_id == embarque_id
    ).first()
    if existing:
        return {"recepcion_id": existing.id, "already_exists": True}

    e.estado = "en_recepcion"
    recepcion = RecepcionEmbarque(embarque_id=embarque_id, usuario_id=current_user.id)
    db.add(recepcion)
    db.commit()
    db.refresh(recepcion)
    return {"recepcion_id": recepcion.id}


# ── GET RECEPCION ──────────────────────────────────────────────────────────────

@router.get("/recepciones/{recepcion_id}")
def get_recepcion(
    recepcion_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rec = db.query(RecepcionEmbarque).filter(RecepcionEmbarque.id == recepcion_id).first()
    if not rec:
        raise HTTPException(404, "Recepción no encontrada")

    items_db = db.query(RecepcionEmbarqueItem).filter(
        RecepcionEmbarqueItem.recepcion_id == recepcion_id
    ).all()

    all_emb_items = db.query(EmbarqueItem).filter(
        EmbarqueItem.embarque_id == rec.embarque_id
    ).all()

    items_out = []
    for ri in items_db:
        fotos = db.query(RecepcionItemFoto).filter(
            RecepcionItemFoto.recepcion_item_id == ri.id
        ).all()
        ei = db.query(EmbarqueItem).filter(EmbarqueItem.id == ri.embarque_item_id).first()
        d = _item_detail(ei, db) if ei else {}
        d.update({
            "id": ri.id,
            "embarque_item_id": ei.id if ei else None,
            "recepcion_item_id": ri.id,
            "qty_recibida": ri.qty_recibida,
            "qty_danada": ri.qty_danada,
            "estado_recepcion": ri.estado_recepcion,
            "observacion": ri.observacion,
            "fotos": [{"id": f.id, "url": f.url_foto} for f in fotos],
        })
        items_out.append(d)

    # Items not yet started
    started_ids = {ri.embarque_item_id for ri in items_db}
    for ei in all_emb_items:
        if ei.id not in started_ids:
            d = _item_detail(ei, db)
            d.update({
                "id": ei.id,
                "embarque_item_id": ei.id,
                "recepcion_item_id": None,
                "qty_recibida": None,
                "qty_danada": 0,
                "estado_recepcion": None,
                "observacion": None,
                "fotos": [],
            })
            items_out.append(d)

    pendientes = sum(1 for i in items_out if i["estado_recepcion"] is None)

    emb_obj = db.query(Embarque).filter(Embarque.id == rec.embarque_id).first()
    return {
        "id": rec.id,
        "embarque_id": rec.embarque_id,
        "embarque_numero": emb_obj.numero if emb_obj else f"EMB-{rec.embarque_id}",
        "estado": rec.estado,
        "fecha_inicio": rec.fecha_inicio.isoformat() if rec.fecha_inicio else None,
        "fecha_cierre": rec.fecha_cierre.isoformat() if rec.fecha_cierre else None,
        "observacion_general": rec.observacion_general,
        "items": items_out,
        "total_items": len(items_out),
        "pendientes": pendientes,
    }


# ── MARCAR ITEM ────────────────────────────────────────────────────────────────

class MarcarItemBody(BaseModel):
    embarque_item_id: int
    qty_recibida: int
    qty_danada: Optional[int] = 0
    estado_recepcion: str
    observacion: Optional[str] = None


ESTADOS_VALIDOS = {
    "completo", "faltante", "sobrante",
    "danado_utilizable", "danado_no_utilizable", "no_llego",
}


@router.patch("/recepciones/{recepcion_id}/items/{item_id}")
def marcar_item(
    recepcion_id: int,
    item_id: int,
    body: MarcarItemBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rec = db.query(RecepcionEmbarque).filter(RecepcionEmbarque.id == recepcion_id).first()
    if not rec or rec.estado == "cerrada":
        raise HTTPException(400, "Recepción no encontrada o ya cerrada")

    if body.estado_recepcion not in ESTADOS_VALIDOS:
        raise HTTPException(400, f"Estado inválido: {body.estado_recepcion}")

    es_danado = "danado" in body.estado_recepcion

    # Try by recepcion_item PK first, then by embarque_item_id
    ri = db.query(RecepcionEmbarqueItem).filter(
        RecepcionEmbarqueItem.recepcion_id == recepcion_id,
        RecepcionEmbarqueItem.id == item_id,
    ).first()

    if not ri:
        ri = db.query(RecepcionEmbarqueItem).filter(
            RecepcionEmbarqueItem.recepcion_id == recepcion_id,
            RecepcionEmbarqueItem.embarque_item_id == body.embarque_item_id,
        ).first()

    if not ri:
        ri = RecepcionEmbarqueItem(
            recepcion_id=recepcion_id,
            embarque_item_id=body.embarque_item_id,
            usuario_id=current_user.id,
        )
        db.add(ri)
        db.flush()

    # Validate: damaged items need at least 1 photo
    if es_danado:
        foto_count = db.query(RecepcionItemFoto).filter(
            RecepcionItemFoto.recepcion_item_id == ri.id
        ).count()
        if foto_count == 0:
            raise HTTPException(422, "Ítems dañados requieren al menos una foto antes de guardar")

    ri.qty_recibida = body.qty_recibida
    ri.qty_danada = body.qty_danada or 0
    ri.estado_recepcion = body.estado_recepcion
    ri.observacion = body.observacion
    ri.usuario_id = current_user.id
    db.commit()
    db.refresh(ri)
    return {"ok": True, "recepcion_item_id": ri.id}


# ── CREATE RECEPCION ITEM (for brand new items) ────────────────────────────────

@router.post("/recepciones/{recepcion_id}/items", status_code=201)
def crear_recepcion_item(
    recepcion_id: int,
    body: MarcarItemBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rec = db.query(RecepcionEmbarque).filter(RecepcionEmbarque.id == recepcion_id).first()
    if not rec or rec.estado == "cerrada":
        raise HTTPException(400, "Recepción no encontrada o ya cerrada")

    if body.estado_recepcion not in ESTADOS_VALIDOS:
        raise HTTPException(400, f"Estado inválido: {body.estado_recepcion}")

    es_danado = "danado" in body.estado_recepcion

    ri = RecepcionEmbarqueItem(
        recepcion_id=recepcion_id,
        embarque_item_id=body.embarque_item_id,
        qty_recibida=body.qty_recibida,
        qty_danada=body.qty_danada or 0,
        estado_recepcion=body.estado_recepcion,
        observacion=body.observacion,
        usuario_id=current_user.id,
    )
    db.add(ri)
    db.flush()

    if es_danado:
        foto_count = db.query(RecepcionItemFoto).filter(
            RecepcionItemFoto.recepcion_item_id == ri.id
        ).count()
        if foto_count == 0:
            db.rollback()
            raise HTTPException(422, "Ítems dañados requieren al menos una foto")

    db.commit()
    db.refresh(ri)
    return {"ok": True, "recepcion_item_id": ri.id}


# ── UPLOAD FOTO ────────────────────────────────────────────────────────────────

@router.post("/recepciones/{recepcion_id}/items/{ri_id}/foto", status_code=201)
async def upload_foto(
    recepcion_id: int,
    ri_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ri = db.query(RecepcionEmbarqueItem).filter(
        RecepcionEmbarqueItem.id == ri_id,
        RecepcionEmbarqueItem.recepcion_id == recepcion_id,
    ).first()
    if not ri:
        # Fallback: try as embarque_item_id (for unstarted items)
        ri = db.query(RecepcionEmbarqueItem).filter(
            RecepcionEmbarqueItem.embarque_item_id == ri_id,
            RecepcionEmbarqueItem.recepcion_id == recepcion_id,
        ).first()
    if not ri:
        # Create new recepcion item record
        ri = RecepcionEmbarqueItem(
            recepcion_id=recepcion_id,
            embarque_item_id=ri_id,
            usuario_id=current_user.id,
        )
        db.add(ri)
        db.flush()

    ext = os.path.splitext(file.filename or "foto.jpg")[1].lower() or ".jpg"
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        raise HTTPException(400, "Solo se aceptan imágenes JPG/PNG/WEBP")

    fname = f"recepcion_{ri.id}_{int(datetime.now().timestamp())}{ext}"
    fpath = os.path.join(UPLOAD_DIR, fname)
    with open(fpath, "wb") as f:
        shutil.copyfileobj(file.file, f)

    foto = RecepcionItemFoto(
        recepcion_item_id=ri.id,
        url_foto=f"/uploads/bodega/{fname}",
        subida_por=current_user.id,
    )
    db.add(foto)
    db.commit()
    db.refresh(foto)
    return {"id": foto.id, "url": foto.url_foto, "recepcion_item_id": ri.id}


@router.delete("/recepciones/{recepcion_id}/items/{ri_id}/foto/{foto_id}")
def delete_foto(
    recepcion_id: int,
    ri_id: int,
    foto_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    foto = db.query(RecepcionItemFoto).filter(
        RecepcionItemFoto.id == foto_id,
    ).first()
    if foto:
        fpath = foto.url_foto.lstrip("/")
        if os.path.exists(fpath):
            os.remove(fpath)
        db.delete(foto)
        db.commit()
    return {"ok": True}


# ── CERRAR RECEPCION ───────────────────────────────────────────────────────────

class CerrarRecepcionBody(BaseModel):
    observacion_general: Optional[str] = None
    forzar: bool = False  # allow closing with pending items


@router.post("/recepciones/{recepcion_id}/cerrar")
def cerrar_recepcion(
    recepcion_id: int,
    body: CerrarRecepcionBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rec = db.query(RecepcionEmbarque).filter(RecepcionEmbarque.id == recepcion_id).first()
    if not rec:
        raise HTTPException(404, "Recepción no encontrada")
    if rec.estado == "cerrada":
        raise HTTPException(400, "La recepción ya está cerrada")

    all_emb_items = db.query(EmbarqueItem).filter(
        EmbarqueItem.embarque_id == rec.embarque_id
    ).all()

    ri_map = {
        ri.embarque_item_id: ri
        for ri in db.query(RecepcionEmbarqueItem).filter(
            RecepcionEmbarqueItem.recepcion_id == recepcion_id
        ).all()
    }

    pendientes = [
        ei for ei in all_emb_items
        if ei.id not in ri_map or ri_map[ei.id].estado_recepcion is None
    ]
    if pendientes and not body.forzar:
        raise HTTPException(
            400,
            f"Quedan {len(pendientes)} ítems sin marcar. Use forzar=true para cerrar igual.",
        )

    # Process each item: update estado_item and create reclamos where needed
    for ei in all_emb_items:
        ri = ri_map.get(ei.id)
        if not ri or ri.estado_recepcion is None:
            continue

        item = ei.item_cotizacion
        if not item:
            continue

        if ri.estado_recepcion in ("completo", "danado_utilizable", "sobrante"):
            item.estado_item = "en_bodega"
        elif ri.estado_recepcion in ("faltante", "danado_no_utilizable", "no_llego"):
            item.estado_item = "reclamo_proveedor"
            asig = db.query(OcProveedorItem).filter(
                OcProveedorItem.item_cotizacion_id == item.id
            ).first()
            reclamo = ReclamoProveedor(
                oc_proveedor_id=asig.oc_proveedor_id if asig else None,
                item_cotizacion_id=item.id,
                recepcion_item_id=ri.id,
                motivo=ri.estado_recepcion,
                qty_afectada=ri.qty_recibida or 0,
            )
            db.add(reclamo)

    rec.estado = "cerrada"
    rec.fecha_cierre = datetime.utcnow()
    if body.observacion_general:
        rec.observacion_general = body.observacion_general

    # Advance embarque to recibido
    emb = db.query(Embarque).filter(Embarque.id == rec.embarque_id).first()
    if emb:
        emb.estado = "recibido"

    db.commit()

    # Post-close: evaluate OC-Cliente completion and notify Ventas (rules B6/B7)
    _evaluar_ocs_cliente(rec.embarque_id, db)

    return {"ok": True}


def _evaluar_ocs_cliente(embarque_id: int, db: Session):
    """Check if any OC-Cliente is fully in bodega and notify Ventas."""
    from datetime import date
    from models.models import Cotizacion, OcCliente

    emb_items = db.query(EmbarqueItem).filter(EmbarqueItem.embarque_id == embarque_id).all()
    cot_ids = {
        ei.item_cotizacion.cotizacion_id
        for ei in emb_items
        if ei.item_cotizacion
    }

    for cot_id in cot_ids:
        oc = db.query(OcCliente).filter(OcCliente.cotizacion_id == cot_id).first()
        if not oc:
            continue

        all_items = db.query(ItemCotizacion).filter(
            ItemCotizacion.cotizacion_id == cot_id,
            ItemCotizacion.estado_item.in_([
                "cerrado", "comprado", "preparado", "pre_embarcado",
                "embarcado", "en_bodega", "reclamo_proveedor",
            ])
        ).all()

        en_bodega = [i for i in all_items if i.estado_item == "en_bodega"]
        todos_listos = len(en_bodega) == len(all_items) and len(all_items) > 0

        if todos_listos:
            crear_notificacion(
                db=db,
                rol="ventas",
                severidad="info",
                titulo=f"OC Cliente {oc.numero_oc} lista para despacho",
                mensaje=f"Todos los ítems están en bodega. OC: {oc.numero_oc}",
                entidad_tipo="oc_cliente",
                entidad_id=oc.id,
                link="/cierre-venta",
                regla="oc_lista_despacho",
            )
        elif en_bodega and oc.fecha_entrega:
            try:
                from datetime import date as date_type
                fe = oc.fecha_entrega if isinstance(oc.fecha_entrega, date_type) else None
                if fe:
                    dias_rest = (fe - date.today()).days
                    if dias_rest <= 3:
                        crear_notificacion(
                            db=db,
                            rol="ventas",
                            severidad="critical",
                            titulo=f"Plazo crítico: OC {oc.numero_oc}",
                            mensaje=(
                                f"Quedan {dias_rest} días al plazo y hay ítems listos "
                                f"en bodega para despacho parcial."
                            ),
                            entidad_tipo="oc_cliente",
                            entidad_id=oc.id,
                            link="/cierre-venta",
                            regla="plazo_critico_3d",
                        )
            except Exception:
                pass


# ── RECLAMOS ───────────────────────────────────────────────────────────────────

@router.get("/reclamos")
def list_reclamos(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    reclamos = (
        db.query(ReclamoProveedor)
        .order_by(ReclamoProveedor.fecha_creacion.desc())
        .all()
    )
    result = []
    for r in reclamos:
        item = db.query(ItemCotizacion).filter(ItemCotizacion.id == r.item_cotizacion_id).first() if r.item_cotizacion_id else None
        ocp = (
            db.query(OcProveedor).filter(OcProveedor.id == r.oc_proveedor_id).first()
            if r.oc_proveedor_id
            else None
        )
        ri = db.query(RecepcionEmbarqueItem).filter(RecepcionEmbarqueItem.id == r.recepcion_item_id).first() if r.recepcion_item_id else None
        fotos = (
            db.query(RecepcionItemFoto).filter(
                RecepcionItemFoto.recepcion_item_id == ri.id
            ).all()
            if ri
            else []
        )
        result.append({
            "id": r.id,
            "motivo": r.motivo,
            "qty_afectada": r.qty_afectada,
            "estado": r.estado,
            "observacion": r.observacion,
            "fecha_creacion": r.fecha_creacion.isoformat() if r.fecha_creacion else None,
            "numero_parte": item.numero_parte if item else None,
            "descripcion": item.descripcion if item else None,
            "proveedor_nombre": ocp.proveedor if ocp else None,
            "oc_proveedor_numero": ocp.numero if ocp else None,
            "fotos": [{"id": f.id, "url": f.url_foto} for f in fotos],
        })
    return result


class ActualizarReclamoBody(BaseModel):
    estado: str
    observacion: Optional[str] = None


@router.patch("/reclamos/{reclamo_id}")
def actualizar_reclamo(
    reclamo_id: int,
    body: ActualizarReclamoBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    r = db.query(ReclamoProveedor).filter(ReclamoProveedor.id == reclamo_id).first()
    if not r:
        raise HTTPException(404, "Reclamo no encontrado")

    validos = {"pendiente", "reclamado", "resuelto", "anulado"}
    if body.estado not in validos:
        raise HTTPException(400, f"Estado inválido. Use: {validos}")

    r.estado = body.estado
    if body.observacion:
        r.observacion = body.observacion
    if body.estado in ("resuelto", "anulado"):
        r.usuario_resuelve_id = current_user.id
        r.fecha_resolucion = datetime.utcnow()

    db.commit()
    return {"ok": True}


# ── VENTAS: LISTO PARA DESPACHAR ──────────────────────────────────────────────

@router.get("/ventas/listo-para-despachar")
def listo_para_despachar(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from models.models import Cotizacion, OcCliente

    ocs = db.query(OcCliente).all()
    result = []
    for oc in ocs:
        items = db.query(ItemCotizacion).filter(
            ItemCotizacion.cotizacion_id == oc.cotizacion_id,
            ItemCotizacion.estado_item.in_([
                "cerrado", "comprado", "preparado", "pre_embarcado",
                "embarcado", "en_bodega", "reclamo_proveedor",
            ])
        ).all()
        if not items:
            continue
        todos = all(i.estado_item == "en_bodega" for i in items)
        if todos:
            cot = db.query(Cotizacion).filter(Cotizacion.id == oc.cotizacion_id).first()
            result.append({
                "oc_cliente_id": oc.id,
                "numero_oc": oc.numero_oc,
                "fecha_entrega": str(oc.fecha_entrega) if oc.fecha_entrega else None,
                "cliente": cot.cliente if cot else None,
                "total_items": len(items),
                "items_en_bodega": len(items),
            })
    return result


@router.get("/ventas/alertas-plazo-critico")
def alertas_plazo_critico(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from datetime import date
    from models.models import Cotizacion, OcCliente

    hoy = date.today()
    ocs = db.query(OcCliente).filter(OcCliente.fecha_entrega.isnot(None)).all()
    result = []
    for oc in ocs:
        if not oc.fecha_entrega:
            continue
        try:
            fe = oc.fecha_entrega if hasattr(oc.fecha_entrega, 'year') else None
            if not fe:
                continue
            dias_cal = (fe - hoy).days
            # approximate business days (weekdays only, no holidays for simplicity)
            import math
            if dias_cal <= 0:
                dias_hab = dias_cal
            else:
                semanas = dias_cal // 7
                resto = dias_cal % 7
                dias_hab = semanas * 5 + min(resto, 5)
            if dias_hab > 3:
                continue
        except Exception:
            continue

        items = db.query(ItemCotizacion).filter(
            ItemCotizacion.cotizacion_id == oc.cotizacion_id,
            ItemCotizacion.estado_item.in_([
                "cerrado", "comprado", "preparado", "pre_embarcado",
                "embarcado", "en_bodega", "reclamo_proveedor",
            ])
        ).all()
        en_bodega = [i for i in items if i.estado_item == "en_bodega"]
        otros = [i for i in items if i.estado_item != "en_bodega"]
        if en_bodega and otros:
            cot = db.query(Cotizacion).filter(Cotizacion.id == oc.cotizacion_id).first()
            result.append({
                "oc_cliente_id": oc.id,
                "numero_oc": oc.numero_oc,
                "fecha_entrega": str(oc.fecha_entrega),
                "dias_habiles_restantes": dias_hab,
                "cliente": cot.cliente if cot else None,
                "items_en_bodega": len(en_bodega),
                "total_items": len(items),
            })
    return result
