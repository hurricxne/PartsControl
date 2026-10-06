import mimetypes
import os
import re
import shutil
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from fastapi.responses import FileResponse
from sqlalchemy import func
from sqlalchemy.orm import Session
from pydantic import BaseModel

from database import get_db
from auth import get_current_user
# Candado de empresa ("mineria"): lo llevan `buscar_items` y las cuatro rutas de fotos
# (`upload_foto`, `delete_foto`, `serve_foto`, `fotos_de_item`). El resto del router no
# lo lleva: es una decisión pendiente del dueño.
from empresa_guard import require_empresa
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
# Raíz absoluta para confinar la lectura y el borrado de fotos. Se resuelve una sola vez
# al importar: si se calculara por request y alguien cambiara el directorio de trabajo,
# la comprobación de prefijo compararía contra una raíz distinta y dejaría de servir.
_FOTOS_DIR = os.path.abspath(UPLOAD_DIR)


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
        # OCP: numero = correlativo interno (OCP-2026-XXX); numero_oc = el manual del proveedor
        "ocp_numero": ocp.numero if ocp else None,
        "ocp_numero_oc": ocp.numero_oc if ocp else None,
        "ocp_proveedor": ocp.proveedor if ocp else None,
    }


# ── LIST EMBARQUES ─────────────────────────────────────────────────────────────

@router.get("/embarques")
def list_embarques_bodega(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Returns embarques pendientes de recepción física en Bodega.

    Incluye embarques en estados: en_transito, en_aduana, en_bodega, en_recepcion.
    Para que en_bodega y despachado NO aparezcan aquí cuando ya fueron
    recepcionados físicamente, se excluyen los que tienen RecepcionEmbarque
    con estado='cerrada'. La excepción es en_recepcion, que siempre aparece
    porque sigue en progreso.

    Caso de uso: Logística marca el Embarque como "en_bodega" desde
    EmbarquesPage al verlo llegar físicamente. Bodega lo ve aquí como
    pendiente de recepcionar, abre el panel de recepción, marca cada item
    (completo / dañado / faltante / etc.), y cierra. Solo entonces los items
    pasan a estado en_bodega y aparecen en Despachos.
    """
    # IDs de embarques con recepción YA cerrada → no son pendientes
    closed_emb_ids = {
        r[0]
        for r in db.query(RecepcionEmbarque.embarque_id)
        .filter(RecepcionEmbarque.estado == "cerrada")
        .distinct()
        .all()
    }

    q = db.query(Embarque).filter(
        Embarque.estado.in_(["en_transito", "en_aduana", "en_bodega", "en_recepcion"])
    )
    if closed_emb_ids:
        # Excluir los que ya tienen recepción cerrada (excepto si están en_recepcion)
        from sqlalchemy import or_, and_, not_
        q = q.filter(
            or_(
                Embarque.estado == "en_recepcion",
                not_(Embarque.id.in_(closed_emb_ids)),
            )
        )
    embarques = q.order_by(Embarque.created_at.desc()).all()
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
            "awb_numero": e.awb_numero,
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
    """Embarques ya recepcionados o despachados.

    Aparecen acá solo los embarques que tienen una RecepcionEmbarque con
    estado='cerrada' (= Bodega completó la recepción) o cuyo Embarque está
    en estado 'despachado' (auto al despachar todos los items).
    """
    closed_emb_ids = {
        r[0]
        for r in db.query(RecepcionEmbarque.embarque_id)
        .filter(RecepcionEmbarque.estado == "cerrada")
        .distinct()
        .all()
    }

    from sqlalchemy import or_

    q = db.query(Embarque)
    if closed_emb_ids:
        q = q.filter(
            or_(
                Embarque.id.in_(closed_emb_ids),
                Embarque.estado == "despachado",
            )
        )
    else:
        # Sin recepciones cerradas, solo los despachados (raro pero posible)
        q = q.filter(Embarque.estado == "despachado")

    embarques = q.order_by(Embarque.updated_at.desc()).limit(50).all()
    # `recepcion_id` y `total_items` (2026-08-20): sin ellos la pestaña Historial era una
    # tarjeta muerta — no se podía abrir la recepción cerrada para consultar sus ítems ni
    # sus FOTOS, y el conteo de ítems que pintaba la pantalla llegaba vacío. Las fotos ya
    # estaban en la base y `get_recepcion` ya las devolvía: lo único que faltaba era el id
    # para llegar hasta ellas.
    ids = [e.id for e in embarques]
    recepciones = {
        r.embarque_id: r.id
        for r in db.query(RecepcionEmbarque).filter(
            RecepcionEmbarque.embarque_id.in_(ids)).all()
    } if ids else {}
    # Conteo en UNA consulta agrupada, no una por embarque (N+1).
    conteos = {
        row[0]: row[1]
        for row in db.query(EmbarqueItem.embarque_id, func.count(EmbarqueItem.id))
        .filter(EmbarqueItem.embarque_id.in_(ids))
        .group_by(EmbarqueItem.embarque_id).all()
    } if ids else {}

    return [
        {
            "id": e.id,
            "numero": e.numero,
            "estado": e.estado,
            "forwarder": e.forwarder,
            "awb": e.awb,
            "awb_numero": e.awb_numero,
            "fecha_llegada_est": str(e.fecha_llegada_est) if e.fecha_llegada_est else None,
            "recepcion_id": recepciones.get(e.id),
            "total_items": conteos.get(e.id, 0),
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
        "awb_numero": e.awb_numero,
        "factura_comercial": e.factura_comercial,
        "packing_list": e.packing_list,
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
    if e.estado not in ("en_transito", "en_aduana", "en_bodega", "en_recepcion"):
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
            # `url`: ruta histórica del archivo, solo informativa (ver `upload_foto`).
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


def _validar_item_de_recepcion(db: Session, rec: RecepcionEmbarque, body) -> None:
    """Guards de PERTENENCIA y de COHERENCIA DEL FALTANTE (paridad inversa con
    monza_router_bodega.py:227-254, que los ganó en la Fase 2 del espejo).

    1) PERTENENCIA — el `embarque_item_id` tiene que venir en el embarque de ESTA recepción.
       Una fila espuria con un ítem ajeno infla el tope físico de OTRA OC: el cierre la ignora
       (itera los ítems del embarque), pero `_qty_recibida_utilizable` de despachos une
       EmbarqueItem ↔ RecepcionEmbarqueItem filtrando SOLO por recepción cerrada y sí la
       cuenta. Queda invisible y activa. Solo es alcanzable por API, no por la pantalla.

    2) COHERENCIA DEL FALTANTE — marcar 'faltante' con recibido >= vendido es la combinación
       más cara del módulo, y es UN CLIC: el input de BodegaPage.tsx:95 viene precargado con
       la cantidad VENDIDA, así que el bodeguero marca Faltante y no toca el número. Efecto:
       'faltante' está en _RECEPCION_UTILIZABLE (despachos.py), así que el tope queda en
       min(vendido, recibido) = vendido y la línea entera sale despachable; y en el cierre
       faltante_pendiente = 0, así que NO se crea reclamo. Las unidades que nunca llegaron
       desaparecen sin traza, el proveedor no recibe reclamo, y sale guía 52 + factura 33 por
       mercadería inexistente. Si llegó todo, el estado correcto es 'completo'.

    Adaptación respecto de Monza: acá la cantidad vendida NO está en el ítem del body — hay
    que pasar por EmbarqueItem.item_cotizacion_id, porque en Grupo AM el id que viaja es el
    del ítem del EMBARQUE, no el de la cotización.
    """
    emb_item = (
        db.query(EmbarqueItem)
        .filter(EmbarqueItem.id == body.embarque_item_id)
        .first()
    )
    if not emb_item or emb_item.embarque_id != rec.embarque_id:
        raise HTTPException(400, "El ítem no pertenece al embarque de esta recepción")

    if body.estado_recepcion == "faltante" and body.qty_recibida is not None:
        cant_vendida = (
            db.query(ItemCotizacion.cantidad)
            .filter(ItemCotizacion.id == emb_item.item_cotizacion_id)
            .scalar()
        ) or 0
        if cant_vendida and body.qty_recibida >= cant_vendida:
            raise HTTPException(
                400,
                "Con estado Faltante la cantidad recibida debe ser MENOR que la vendida "
                f"({cant_vendida:g}); si llegó todo, usa Completo",
            )


@router.patch("/recepciones/{recepcion_id}/items/{item_id}")
def marcar_item(
    recepcion_id: int,
    item_id: int,
    body: MarcarItemBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Lectura BLOQUEANTE de la recepción (paridad con monza_router_bodega.py:218-224): el
    # chequeo de `estado == "cerrada"` no protege sin lock — dos cierres simultáneos lo pasan
    # ambos y DUPLICAN los ReclamoProveedor. populate_existing porque la primera sentencia del
    # request (el SELECT de usuarios del guard de auth) ya abrió el read view.
    rec = (
        db.query(RecepcionEmbarque)
        .filter(RecepcionEmbarque.id == recepcion_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if not rec or rec.estado == "cerrada":
        raise HTTPException(400, "Recepción no encontrada o ya cerrada")

    if body.estado_recepcion not in ESTADOS_VALIDOS:
        raise HTTPException(400, f"Estado inválido: {body.estado_recepcion}")
    # qty_recibida alimenta el tope de despacho: negativa no tiene sentido físico
    if (body.qty_recibida or 0) < 0:
        raise HTTPException(400, "La cantidad recibida no puede ser negativa")
    if (body.qty_danada or 0) < 0:
        raise HTTPException(400, "La cantidad dañada no puede ser negativa")
    # Pertenencia + coherencia del 'faltante' (paridad con monza_router_bodega.py:227-254).
    _validar_item_de_recepcion(db, rec, body)

    es_danado = "danado" in body.estado_recepcion

    # La TERCERA puerta del mismo contador ambiguo (2026-08-20). El cuerpo ya trae
    # `embarque_item_id` explícito, así que ese dato MANDA: buscar primero por `item_id`
    # con `.id` hacía que, cuando los dos espacios de numeración colisionan, la cantidad
    # recibida se escribiera en la fila de OTRO ítem — sin atacante, en el flujo normal
    # del bodeguero. Misma regla que `_resolver_ri`, que documenta el problema completo.
    # (Es también el arreglo del bug «atascado al 63%» que QA ya tenía inline: con
    # embarque_item_id presente, _resolver_ri hace exactamente esa búsqueda.)
    ri = _resolver_ri(db, recepcion_id, item_id, body.embarque_item_id)

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
    # Lectura BLOQUEANTE de la recepción (paridad con monza_router_bodega.py:218-224): el
    # chequeo de `estado == "cerrada"` no protege sin lock — dos cierres simultáneos lo pasan
    # ambos y DUPLICAN los ReclamoProveedor. populate_existing porque la primera sentencia del
    # request (el SELECT de usuarios del guard de auth) ya abrió el read view.
    rec = (
        db.query(RecepcionEmbarque)
        .filter(RecepcionEmbarque.id == recepcion_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if not rec or rec.estado == "cerrada":
        raise HTTPException(400, "Recepción no encontrada o ya cerrada")

    if body.estado_recepcion not in ESTADOS_VALIDOS:
        raise HTTPException(400, f"Estado inválido: {body.estado_recepcion}")
    # qty_recibida alimenta el tope de despacho: negativa no tiene sentido físico
    if (body.qty_recibida or 0) < 0:
        raise HTTPException(400, "La cantidad recibida no puede ser negativa")
    if (body.qty_danada or 0) < 0:
        raise HTTPException(400, "La cantidad dañada no puede ser negativa")
    # Pertenencia + coherencia del 'faltante' (paridad con monza_router_bodega.py:227-254).
    _validar_item_de_recepcion(db, rec, body)

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

def _recepcion_abierta_con_lock(db: Session, recepcion_id: int) -> RecepcionEmbarque:
    """La recepción, leída con candado y todavía abierta; 400 si no existe o ya cerró.

    Es la PRIMERA sentencia de `upload_foto` y de `delete_foto`, por dos motivos:

    · ESTADO. Una recepción cerrada es de solo consulta: sus fotos respaldan los reclamos
      al proveedor que crea `cerrar_recepcion`. La pantalla esconde los botones, pero una
      pantalla que quedó abierta desde antes del cierre los sigue mostrando.
    · ORDEN. `marcar_item`, `crear_recepcion_item` y `cerrar_recepcion` empiezan tomando
      este mismo candado. Con las fotos en la misma fila de espera, «el ítem todavía no
      tiene fila» y «al ítem dañado le queda otra foto» se deciden sin que otra petición
      cambie la respuesta a mitad de camino. Sin el candado, una foto subida mientras se
      guarda el ítem crea una SEGUNDA fila del mismo ítem.

    Mismo SELECT y mismo 400 que `marcar_item`.
    """
    rec = (
        db.query(RecepcionEmbarque)
        .filter(RecepcionEmbarque.id == recepcion_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if not rec or rec.estado == "cerrada":
        raise HTTPException(400, "Recepción no encontrada o ya cerrada")
    return rec


def _resolver_ri(db: Session, recepcion_id: int, ri_id: int,
                 embarque_item_id: Optional[int] = None):
    """`ri_id` de la URL -> fila de RecepcionEmbarqueItem de ESA recepción, o None.

    El campo `id` que serializa `get_recepcion` vive en DOS espacios de numeración:
    `RecepcionEmbarqueItem.id` para un ítem ya empezado y `EmbarqueItem.id` para uno que
    no. Los dos autoincrementales van casi parejos, así que un mismo entero puede
    designar dos filas distintas de la MISMA recepción; resolver "el primero que
    aparezca" es una moneda al aire que pega la foto al ítem equivocado.

    Por eso el llamador DESAMBIGUA con el dato que ya tiene, igual que `marcar_item`, que
    recibe `embarque_item_id` explícito en el cuerpo desde siempre: si viene ese dato,
    manda y no hay que adivinar. Sin él (cliente viejo) se cae al comportamiento
    histórico —primero por `.id`, después por `embarque_item_id`— y se deja constancia en
    el log cuando los dos resuelven a filas distintas, que es el único caso ambiguo.

    OJO al tocar esto: NO conviene bloquear ante la ambigüedad. La pantalla no deja
    guardar un ítem dañado sin foto, así que un 409 al subir dejaría al bodeguero sin
    poder subir la foto ni guardar, y "recarga la recepción" no ayuda porque para un
    ítem empezado los dos ids coinciden. Desambiguar con el dato del llamador es lo que
    evita ese callejón sin salida.

    Quien llama ya tiene la recepción tomada con candado (`marcar_item` con su lectura
    bloqueante, `upload_foto` con `_recepcion_abierta_con_lock`): «no hay fila» solo es
    una respuesta firme bajo ese candado, porque quien llama inserta la fila cuando esto
    devuelve None.
    """
    # 1. El dato explícito manda. Es la ruta que usa el frontend actual.
    if embarque_item_id is not None:
        ri = db.query(RecepcionEmbarqueItem).filter(
            RecepcionEmbarqueItem.embarque_item_id == embarque_item_id,
            RecepcionEmbarqueItem.recepcion_id == recepcion_id,
        ).first()
        if ri:
            return ri
        # Sin fila todavía (ítem no empezado): que decida quien llama, con el dato bueno.
        return None

    # 2. Sin dato explícito: comportamiento histórico, con aviso si hay colisión.
    por_id = db.query(RecepcionEmbarqueItem).filter(
        RecepcionEmbarqueItem.id == ri_id,
        RecepcionEmbarqueItem.recepcion_id == recepcion_id,
    ).first()
    por_emb = db.query(RecepcionEmbarqueItem).filter(
        RecepcionEmbarqueItem.embarque_item_id == ri_id,
        RecepcionEmbarqueItem.recepcion_id == recepcion_id,
    ).first()
    if por_id and por_emb and por_id.id != por_emb.id:
        # Único caso genuinamente ambiguo. No se bloquea (el porqué está en el docstring):
        # se elige la fila encontrada por `.id` y se deja rastro para poder investigarlo.
        print(f"[bodega] ri_id {ri_id} AMBIGUO en recepcion {recepcion_id}: "
              f"por_id={por_id.id} por_embarque_item={por_emb.id}; se usa por_id. "
              f"El cliente deberia mandar embarque_item_id.", flush=True)
    return por_id or por_emb


def _descartar_archivo(ruta: str) -> None:
    """Borra el archivo de una foto que quedó sin fila en la base. Si el borrado también
    falla se anota y se sigue: el error que importa es el que trajo hasta acá."""
    try:
        os.remove(ruta)
    except FileNotFoundError:
        pass  # la escritura falló antes de crear el archivo: no hay nada que borrar
    except OSError as exc:
        print(f"[bodega] no se pudo borrar el archivo suelto {ruta}: {exc!r}", flush=True)


@router.post("/recepciones/{recepcion_id}/items/{ri_id}/foto", status_code=201,
             dependencies=[Depends(require_empresa("mineria"))])
def upload_foto(
    recepcion_id: int,
    ri_id: int,
    # Desambiguador OPCIONAL, misma convención que `marcar_item`, que lo recibe en el
    # cuerpo desde siempre. Con él no hay nada que adivinar; sin él se cae al
    # comportamiento histórico. Es aditivo: un cliente viejo sigue funcionando.
    embarque_item_id: Optional[int] = None,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # `def` y no `async def`: el candado puede tener que esperar a otra petición, y esa
    # espera es bloqueante. Dentro de un `async def` detiene el único proceso del servidor
    # para todos los usuarios; y si quien tiene el candado terminó con un error, recién lo
    # suelta el cierre de `get_db`, que necesita ese mismo proceso libre: la espera dura
    # hasta el timeout de MySQL. En un `def`, FastAPI corre la función en un hilo aparte.
    rec = _recepcion_abierta_con_lock(db, recepcion_id)
    ri = _resolver_ri(db, recepcion_id, ri_id, embarque_item_id)
    if not ri:
        # Ítem todavía no empezado: se crea su fila de recepción. El id del EmbarqueItem
        # es el explícito si vino, si no el de la URL.
        emb_item_id = embarque_item_id if embarque_item_id is not None else ri_id
        # Se valida la pertenencia ANTES de insertar: sin esto, un id que no corresponde
        # a ningún ítem de este embarque revienta con IntegrityError de MySQL, o sea un
        # 500 en producción en vez de un error entendible.
        ei = db.query(EmbarqueItem).filter(
            EmbarqueItem.id == emb_item_id,
            EmbarqueItem.embarque_id == rec.embarque_id,
        ).first()
        if not ei:
            raise HTTPException(404, "El ítem no pertenece a esta recepción")
        ri = RecepcionEmbarqueItem(
            recepcion_id=recepcion_id,
            embarque_item_id=emb_item_id,
            usuario_id=current_user.id,
        )
        db.add(ri)
        db.flush()

    ext = os.path.splitext(file.filename or "foto.jpg")[1].lower() or ".jpg"
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        raise HTTPException(400, "Solo se aceptan imágenes JPG/PNG/WEBP")

    # El uuid4 hace ÚNICO el nombre. Con un nombre derivado del reloj, dos fotos del mismo
    # ítem subidas en el mismo segundo comparten archivo: varias filas apuntan a uno solo
    # y al borrar una se pierden las otras, que son la evidencia del reclamo al proveedor.
    # El prefijo `recepcion_{ri.id}` solo sirve para reconocer el archivo al depurar.
    fname = f"recepcion_{ri.id}_{uuid.uuid4().hex}{ext}"
    fpath = os.path.join(UPLOAD_DIR, fname)
    # Archivo y fila van juntos. Un archivo sin fila no lo puede pedir ni borrar nadie
    # (`serve_foto` y `delete_foto` parten de la fila) y queda ocupando disco: si falla
    # la escritura o el commit, se borra.
    try:
        with open(fpath, "wb") as f:
            shutil.copyfileobj(file.file, f)
    except OSError as exc:
        _descartar_archivo(fpath)
        print(f"[bodega] no se pudo escribir la foto {fname}: {exc!r}", flush=True)
        raise HTTPException(500, "No se pudo guardar la foto en el servidor. "
                                 "Intenta de nuevo; si se repite, avisa a soporte.")

    # `url_foto` guarda la ruta histórica "/uploads/bodega/<archivo>" y así viaja en el
    # campo `url` de las respuestas (esta, `get_recepcion` y `list_reclamos`). Sirve para
    # ubicar el archivo en disco al depurar; por HTTP solo resuelve con
    # SERVIR_FOTOS_BODEGA_SIN_AUTH=true. La imagen se pide por id a `serve_foto`.
    foto = RecepcionItemFoto(
        recepcion_item_id=ri.id,
        url_foto=f"/uploads/bodega/{fname}",
        subida_por=current_user.id,
    )
    db.add(foto)
    try:
        db.commit()
    except Exception:
        db.rollback()
        _descartar_archivo(fpath)
        raise
    db.refresh(foto)
    return {"id": foto.id, "url": foto.url_foto, "recepcion_item_id": ri.id}


@router.delete("/recepciones/{recepcion_id}/items/{ri_id}/foto/{foto_id}",
               dependencies=[Depends(require_empresa("mineria"))])
def delete_foto(
    recepcion_id: int,
    ri_id: int,
    foto_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _recepcion_abierta_con_lock(db, recepcion_id)

    # PERTENENCIA — se resuelve desde la FOTO, no desde el `ri_id` de la URL.
    # Filtrar solo por `foto_id` dejaría que cualquier sesión borre cualquier foto
    # acertando un entero correlativo, y esas fotos son la evidencia que `marcar_item`
    # exige para guardar un ítem dañado. El `ri_id` tampoco sirve para encontrarla: es
    # ambiguo por contrato (ver `_resolver_ri`). Se parte de la foto, que sabe de qué ítem
    # cuelga; se comprueba que ese ítem sea de esta recepción; y al `ri_id` solo se le
    # pide ser consistente con el ítem bajo alguna de sus dos formas.
    foto = db.query(RecepcionItemFoto).filter(RecepcionItemFoto.id == foto_id).first()
    if not foto:
        # 404 y no `{"ok": true}`: responder «ok» sin borrar hace que la pantalla saque de
        # la lista una foto que sigue en la base y en el disco.
        raise HTTPException(404, "Foto no encontrada")

    ri = db.query(RecepcionEmbarqueItem).filter(
        RecepcionEmbarqueItem.id == foto.recepcion_item_id,
        RecepcionEmbarqueItem.recepcion_id == recepcion_id,
    ).first()
    if not ri:
        raise HTTPException(404, "Foto no encontrada")

    # El `ri_id` de la URL tiene que designar a ESE ítem, en cualquiera de sus dos formas.
    if ri_id not in (ri.id, ri.embarque_item_id):
        raise HTTPException(404, "Foto no encontrada")

    # Un ítem guardado como dañado no puede quedar sin foto: es la regla que `marcar_item`
    # exige al guardar, sostenida también al borrar (mismo predicado). Se cuenta bajo el
    # candado de la recepción: dos borrados simultáneos no se saltan la cuenta.
    if "danado" in (ri.estado_recepcion or ""):
        otras = db.query(RecepcionItemFoto).filter(
            RecepcionItemFoto.recepcion_item_id == ri.id,
            RecepcionItemFoto.id != foto.id,
        ).count()
        if otras == 0:
            raise HTTPException(
                409,
                "Un ítem guardado como dañado necesita al menos una foto: sube la de "
                "reemplazo antes de borrar esta, o cambia su estado y guarda.",
            )

    # Se borra por la misma ruta confinada que usa `serve_foto`: una ruta relativa al
    # directorio de trabajo, con una fila manipulada, borraría un archivo de fuera de
    # uploads/bodega.
    try:
        fpath = _ruta_confinada_foto(foto.url_foto)
        if os.path.isfile(fpath):
            os.remove(fpath)
    except HTTPException:
        # Ruta inválida en la columna: se borra la fila igual (es lo que el usuario
        # pidió) pero NO se toca el disco. Queda anotado para que alguien lo revise,
        # en vez de fallar en silencio.
        print(f"[bodega] foto {foto_id}: url_foto invalida, no se borro el archivo "
              f"({foto.url_foto!r})", flush=True)
    db.delete(foto)
    db.commit()
    return {"ok": True}


# ── SERVIR FOTOS (acceso autenticado) ──────────────────────────────────────────
# Las fotos se piden por ID DE BASE a `serve_foto`, que exige sesión de Grupo AM: el
# nombre del archivo no viaja en la URL. La carpeta uploads/bodega no se publica como
# estática (main.py solo la monta con SERVIR_FOTOS_BODEGA_SIN_AUTH=true, la palanca de
# rollback): servida sin sesión, cualquiera puede enumerar los archivos anteriores al
# uuid4, que llevan nombres predecibles (`recepcion_{id}_{segundos}.jpg`).
# El archivo se resuelve desde la fila y se confina dentro de _FOTOS_DIR con el mismo
# patrón de `serve_doc` (routers/despachos.py): regex estricta -> abspath -> prefijo.

def _ruta_confinada_foto(url_foto: str) -> str:
    """Nombre de archivo -> ruta absoluta dentro de _FOTOS_DIR, o 400.

    `basename` tolera los dos formatos que pueden convivir en la columna `url_foto`: el
    histórico "/uploads/bodega/x.jpg" y un eventual "x.jpg" pelado. Por eso no hace
    falta migrar la columna.

    OJO al probar esto: `basename` ya neutraliza los payloads clásicos de traversal
    ("../../etc/passwd" queda en "passwd", que resuelve DENTRO del directorio y muere
    por archivo inexistente). El único payload que ejercita la comprobación de prefijo
    es "/uploads/bodega/..", cuyo basename es ".." y sí sale de la carpeta: esa línea no
    es código muerto, y una prueba con el payload clásico pasa igual con y sin ella.
    """
    nombre = os.path.basename((url_foto or "").strip())
    if not re.match(r"^[A-Za-z0-9._-]{1,255}$", nombre):
        raise HTTPException(400, "Nombre de archivo inválido")
    full = os.path.abspath(os.path.join(_FOTOS_DIR, nombre))
    if not full.startswith(_FOTOS_DIR + os.sep):
        raise HTTPException(400, "Ruta inválida")
    return full


@router.get("/fotos/{foto_id}", dependencies=[Depends(require_empresa("mineria"))])
def serve_foto(
    foto_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Devuelve la imagen de una foto de recepción.

    CONTROL DE ACCESO — dos capas, y las dos importan:
      1. `require_empresa("mineria")` a nivel de ruta. Las fotos de recepción son
         SOLO de Grupo AM: MonzaParts tiene su propio camino
         (`monza_router_documentos.py`). `foto_id` es un entero correlativo: sin este
         candado, una sesión de la otra marca baja todas las fotos recorriendo los ids.
         Las cuatro rutas de fotos de este archivo (`upload_foto`, `delete_foto`,
         `serve_foto`, `fotos_de_item`) llevan el MISMO candado: con una sola sin él,
         la otra marca sube o borra evidencia que no puede ver.
      2. La foto tiene que colgar de una recepción viva (abajo).

    Responde 404 —no 403— cuando la foto no existe o quedó huérfana: un 403
    confirmaría que ese id existe.

    Se sirve INLINE (FileResponse sin `filename=`) porque el consumidor es un <img>,
    no una descarga.
    """
    foto = db.query(RecepcionItemFoto).filter(RecepcionItemFoto.id == foto_id).first()
    if not foto:
        raise HTTPException(404, "Foto no encontrada")

    # La foto tiene que colgar de un ítem de recepción, y ese de una recepción real.
    # Una fila huérfana (borrado a medias, restore parcial) se trata como inexistente.
    ri = db.query(RecepcionEmbarqueItem).filter(
        RecepcionEmbarqueItem.id == foto.recepcion_item_id
    ).first()
    if not ri:
        raise HTTPException(404, "Foto no encontrada")
    recepcion = db.query(RecepcionEmbarque).filter(
        RecepcionEmbarque.id == ri.recepcion_id
    ).first()
    if not recepcion:
        raise HTTPException(404, "Foto no encontrada")

    path = _ruta_confinada_foto(foto.url_foto)
    if not os.path.isfile(path):
        # La fila existe pero el archivo no está en disco. Es un estado real
        # (borrado a mano, restore parcial): el frontend pinta "la foto ya no existe".
        raise HTTPException(404, "Archivo no encontrado")

    return FileResponse(
        path,
        media_type=mimetypes.guess_type(path)[0] or "application/octet-stream",
        headers={
            # `no-store`: el contenido depende de QUIÉN pregunta, y la caché del
            # navegador se indexa por URL, no por el header Authorization. En el
            # computador compartido de bodega, con un `max-age` el siguiente usuario
            # ve la imagen sin volver a pasar por el candado.
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/items/{item_cotizacion_id}/fotos",
            dependencies=[Depends(require_empresa("mineria"))])
def fotos_de_item(
    item_cotizacion_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Fotos de RECEPCIÓN de una línea de venta, para consultarlas desde Despachos.

    POR QUÉ EXISTE (pedido del dueño, 2026-08-20)
        En bodega se fotografían los ítems de alto valor (sobre $3.000.000 de venta)
        aunque lleguen completos, y más de una foto por ítem. Al preparar el despacho hay
        que poder mirarlas: es la evidencia del estado en que llegó la pieza.

    ES DE SOLO CONSULTA. No hay borrado desde acá a propósito: son evidencia de la
    recepción, y quien las borra tiene que hacerlo en la pantalla donde las tomó,
    con su contexto a la vista. Los bytes de cada foto se piden a
    `GET /api/bodega/fotos/{id}`, que lleva su propio candado.

    LA CADENA, que ya existía por llaves reales:
        items_cotizacion -> embarque_items.item_cotizacion_id
                         -> recepcion_embarque_items.embarque_item_id
                         -> recepcion_item_fotos.recepcion_item_id
    Un mismo ítem puede haber pasado por MÁS DE UN embarque (re-embarque tras un
    reclamo), así que se devuelven las fotos de todos, ordenadas por fecha.
    """
    filas = (
        db.query(RecepcionItemFoto, RecepcionEmbarqueItem, RecepcionEmbarque)
        .join(RecepcionEmbarqueItem,
              RecepcionEmbarqueItem.id == RecepcionItemFoto.recepcion_item_id)
        .join(EmbarqueItem,
              EmbarqueItem.id == RecepcionEmbarqueItem.embarque_item_id)
        .join(RecepcionEmbarque,
              RecepcionEmbarque.id == RecepcionEmbarqueItem.recepcion_id)
        .filter(EmbarqueItem.item_cotizacion_id == item_cotizacion_id)
        .order_by(RecepcionItemFoto.fecha_subida.asc(), RecepcionItemFoto.id.asc())
        .all()
    )
    return [
        {
            "id": foto.id,
            "recepcion_id": rec.id,
            "estado_recepcion": ri.estado_recepcion,
            "observacion": ri.observacion,
            "fecha": foto.fecha_subida.isoformat() if foto.fecha_subida else None,
        }
        for foto, ri, rec in filas
    ]


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
    # Lectura BLOQUEANTE (paridad con monza_router_bodega.py:291-297): sin el lock, dos clics
    # simultáneos en "Cerrar recepción" pasan ambos el chequeo de abajo y DUPLICAN los
    # ReclamoProveedor que crea el cierre — el proveedor recibe dos reclamos por el mismo
    # faltante y el conteo de reclamos deja de cuadrar.
    rec = (
        db.query(RecepcionEmbarque)
        .filter(RecepcionEmbarque.id == recepcion_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
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

    # Recibido utilizable ACUMULADO en recepciones cerradas ANTERIORES (otros
    # embarques de la misma línea): el faltante a reclamar se calcula contra lo
    # que AÚN no llega en total — no contra la venta completa en cada recepción,
    # o una reposición / línea repartida en 2 embarques generaría reclamos
    # fantasma por mercadería que ya está en bodega.
    from routers.despachos import _qty_recibida_utilizable
    recibido_previo = _qty_recibida_utilizable(
        db, [ei.item_cotizacion_id for ei in all_emb_items if ei.item_cotizacion_id])

    # Process each item: update estado_item and create reclamos where needed
    def _crear_reclamo(item, ri, motivo, qty_afectada):
        asig = db.query(OcProveedorItem).filter(
            OcProveedorItem.item_cotizacion_id == item.id
        ).first()
        db.add(ReclamoProveedor(
            oc_proveedor_id=asig.oc_proveedor_id if asig else None,
            item_cotizacion_id=item.id,
            recepcion_item_id=ri.id if ri else None,
            motivo=motivo,
            qty_afectada=qty_afectada,
        ))

    for ei in all_emb_items:
        ri = ri_map.get(ei.id)
        item = ei.item_cotizacion
        if not item:
            continue

        if not ri or ri.estado_recepcion is None:
            # Solo se llega aquí con forzar=true (ítem sin marcar). Antes quedaba
            # atascado en 'embarcado' para siempre, invisible para Bodega y para
            # Despachos: se trata como 'no_llego' con reclamo TRAZABLE que se
            # gestiona cuando el ítem aparezca.
            item.estado_item = "reclamo_proveedor"
            _crear_reclamo(item, None, "no_llego", item.cantidad or 0)
            continue

        cant = item.cantidad or 0
        recibida = max(ri.qty_recibida or 0, 0)
        # Lo que sigue faltando de la línea considerando TODOS los embarques:
        # lo vendido − (recibido en recepciones anteriores + esta recepción)
        faltante_pendiente = max(cant - (recibido_previo.get(item.id, 0) + recibida), 0)

        if ri.estado_recepcion in ("completo", "danado_utilizable", "sobrante"):
            item.estado_item = "en_bodega"
            # 'completo' con MENOS unidades que lo vendido = llegada parcial: lo
            # recibido queda despachable (Despachos lo topea por qty_recibida) y
            # el faltante REAL se reclama aparte para que no desaparezca sin
            # traza (una reposición que completa la línea NO genera reclamo).
            if ri.estado_recepcion == "completo" and recibida > 0 and faltante_pendiente > 0:
                _crear_reclamo(item, ri, "faltante", faltante_pendiente)
        elif ri.estado_recepcion == "faltante":
            if recibida > 0:
                # Llegada PARCIAL: las unidades que SÍ llegaron quedan en bodega
                # y despachables (topeadas por qty_recibida en Despachos); solo
                # la diferencia REAL va a reclamo. Antes la línea ENTERA caía a
                # reclamo y lo recibido quedaba indespachable.
                item.estado_item = "en_bodega"
                if faltante_pendiente > 0:
                    _crear_reclamo(item, ri, "faltante", faltante_pendiente)
            else:
                item.estado_item = "reclamo_proveedor"
                _crear_reclamo(item, ri, "faltante", faltante_pendiente or cant)
        elif ri.estado_recepcion == "no_llego":
            item.estado_item = "reclamo_proveedor"
            # Lo afectado es TODO lo esperado (antes se registraba qty_recibida=0)
            _crear_reclamo(item, ri, "no_llego", cant)
        elif ri.estado_recepcion == "danado_no_utilizable":
            item.estado_item = "reclamo_proveedor"
            # Lo afectado es lo que llegó dañado (o lo esperado si no se registró)
            _crear_reclamo(item, ri, "danado_no_utilizable",
                           recibida if recibida > 0 else cant)

    rec.estado = "cerrada"
    rec.fecha_cierre = datetime.utcnow()
    if body.observacion_general:
        rec.observacion_general = body.observacion_general

    # Advance embarque to en_bodega (estado final manual; despachado se setea auto al despachar todo)
    emb = db.query(Embarque).filter(Embarque.id == rec.embarque_id).first()
    if emb:
        emb.estado = "en_bodega"

    db.commit()

    # Post-close: evaluate OC-Cliente completion and notify Ventas (rules B6/B7)
    _evaluar_ocs_cliente(rec.embarque_id, db)

    return {"ok": True}


def _evaluar_ocs_cliente(embarque_id: int, db: Session):
    """Check if any OC-Cliente is fully in bodega and notify Ventas."""
    emb_items = db.query(EmbarqueItem).filter(EmbarqueItem.embarque_id == embarque_id).all()
    cot_ids = {
        ei.item_cotizacion.cotizacion_id
        for ei in emb_items
        if ei.item_cotizacion
    }
    _notificar_ocs_cliente(cot_ids, db)


def _evaluar_ocs_cliente_por_items(item_ids: list, db: Session):
    """Misma evaluación B6/B7, pero anclada en ÍTEMS en vez de en un embarque.

    La recepción NACIONAL (`recepcion_nacional/`) no pasa por `embarque_items`: el
    proveedor chileno llega con su camión y su guía, y los ítems pasan a 'en_bodega'
    sin haber estado nunca en un embarque. Por eso esa vía no podía usar
    `_evaluar_ocs_cliente(embarque_id, ...)` y la OC quedaba lista para despacho sin
    que Ventas se enterara hasta el barrido de las 06:00 del día siguiente.

    Aditivo: la firma y el comportamiento de la vía embarque quedan intactos; las dos
    variantes solo resuelven las `cotizacion_id` afectadas y delegan en el MISMO
    `_notificar_ocs_cliente` (una sola definición de las reglas B6/B7, para que no se
    puedan desincronizar).
    """
    if not item_ids:
        return
    cot_ids = {
        r[0] for r in db.query(ItemCotizacion.cotizacion_id)
        .filter(ItemCotizacion.id.in_(list(item_ids))).all()
        if r[0] is not None
    }
    _notificar_ocs_cliente(cot_ids, db)


def _notificar_ocs_cliente(cot_ids, db: Session):
    """Reglas B6/B7 sobre las OC-Cliente de esas cotizaciones (cuerpo original)."""
    from datetime import date
    from models.models import Cotizacion, OcCliente

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
            "numero_oc_prov": ocp.numero_oc if ocp else None,
            # `url`: ruta histórica del archivo, solo informativa (ver `upload_foto`).
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


# ── BUSCADOR DE ÍTEMS («Buscar ítem», aditivo 2026-08-05) ─────────────────────
# Bodega listaba EMBARQUES, nunca ítems: para responder «¿dónde está esta parte?»
# este endpoint introduce la vista de ítems (fila = ItemCotizacion), con el
# contrato común de búsqueda (docs/spec-buscadores-bodega-despachos-2026-08-05.md).
# Los helpers de normalización viven en routers/despachos.py (helper único de la
# marca) y se importan localmente — patrón probado bodega→despachos, sin ciclo.
# El endpoint nace con candado de empresa PROPIO (a nivel de ruta): el resto del
# router queda tal cual está (decisión pendiente del dueño).

# Alcance de estados (decisión de la spec, no opción): la respuesta del buscador
# suele ser «todavía en el aire», así que se listan también los estados previos
# a bodega. Cada fila muestra el suyo.
_ESTADOS_BUSCABLES = (
    "comprado", "preparado", "pre_embarcado",
    "embarcado", "en_bodega", "reclamo_proveedor",
)


@router.get("/items", dependencies=[Depends(require_empresa("mineria"))])
def buscar_items(
    q: Optional[str] = None,
    estado: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Búsqueda de ítems para el operador de Bodega.

    Campos del OR (el placeholder del frontend es un CONTRATO con esta lista):
    n° de parte (con pasada colapsada sin guiones), descripción/nombre_cat (las
    dos: la pantalla imprime `descripcion or nombre_cat`), marca, n° de
    cotización (con y sin prefijo COT-), cliente, n° de OC del cliente, n° de
    embarque, n° de AWB (awb_numero, NUNCA `awb` que guarda el archivo adjunto)
    y n° de guía del proveedor nacional (la compra nacional no pasa por
    embarques: sin ese EXISTS quedaría invisible).

    Sobre de respuesta: {items, total, page, page_size, q_efectivo, normalizado}.
    Serialización en lote (queries fijas, nunca por ítem): PROHIBIDO _item_detail
    aquí — dispara hasta 6 queries por ítem.
    """
    from sqlalchemy import and_, exists, func, or_  # noqa: F401 (func: pasada B)
    from models.models import Cotizacion, OcCliente as OcClienteM, Despacho, DespachoItem
    from recepcion_nacional.models import RecepcionNacional, RecepcionNacionalItem
    from routers.despachos import (
        _normalizar_q, _variantes_token, _patron, _colapsar,
        _np_colapsado_sql, _matchea, _qty_recibida_utilizable, _disponibles_por_item,
    )

    estados = (
        (estado,) if estado in _ESTADOS_BUSCABLES else _ESTADOS_BUSCABLES
    )
    # LEFT JOIN a oc_cliente por el camino CANÓNICO (relación 1-1 con la
    # cotización), el mismo que muestra la fila — si el WHERE usara otro camino
    # el operador buscaría una OC y vería otra.
    base = (
        db.query(ItemCotizacion, Cotizacion, OcClienteM)
        .join(Cotizacion, Cotizacion.id == ItemCotizacion.cotizacion_id)
        .outerjoin(OcClienteM, OcClienteM.cotizacion_id == Cotizacion.id)
        .filter(ItemCotizacion.estado_item.in_(estados))
    )

    def _aplicar_q(query, tokens, con_colapsado=False):
        """AND entre tokens; OR entre campos por token. Tablas hijas con EXISTS
        correlacionado, NUNCA join (multiplicaría filas y mentiría el total)."""
        for tok in tokens:
            conds = []
            for v in _variantes_token(tok):
                pat = _patron(v)
                conds += [
                    ItemCotizacion.numero_parte.like(pat, escape="\\"),
                    ItemCotizacion.descripcion.like(pat, escape="\\"),
                    ItemCotizacion.nombre_cat.like(pat, escape="\\"),
                    ItemCotizacion.marca.like(pat, escape="\\"),
                    Cotizacion.numero.like(pat, escape="\\"),
                    Cotizacion.cliente.like(pat, escape="\\"),
                    OcClienteM.numero_oc.like(pat, escape="\\"),
                    exists().where(and_(
                        EmbarqueItem.item_cotizacion_id == ItemCotizacion.id,
                        Embarque.id == EmbarqueItem.embarque_id,
                        or_(
                            Embarque.numero.like(pat, escape="\\"),
                            Embarque.awb_numero.like(pat, escape="\\"),
                        ),
                    )),
                    exists().where(and_(
                        RecepcionNacionalItem.item_cotizacion_id == ItemCotizacion.id,
                        RecepcionNacional.id == RecepcionNacionalItem.recepcion_id,
                        RecepcionNacional.numero_guia_proveedor.like(pat, escape="\\"),
                    )),
                ]
            if con_colapsado and any(ch.isdigit() for ch in tok):
                conds.append(
                    _np_colapsado_sql(ItemCotizacion.numero_parte)
                    .like(_patron(_colapsar(tok)), escape="\\")
                )
            query = query.filter(or_(*conds))
        return query

    tokens = _normalizar_q(q)
    normalizado = False
    query = _aplicar_q(base, tokens) if tokens else base
    total = query.count()
    if tokens and total == 0 and any(any(c.isdigit() for c in t) for t in tokens):
        # Pasada B (red de seguridad, full scan reconocido): parte colapsada.
        query = _aplicar_q(base, tokens, con_colapsado=True)
        total = query.count()
        normalizado = total > 0

    # Orden determinista: sin desempate estable, OFFSET puede repetir/perder filas.
    rows = (
        query.order_by(ItemCotizacion.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    # ── Serialización EN LOTE (queries fijas sobre los ≤page_size ids) ──
    item_ids = [it.id for it, _cot, _occ in rows]
    emb_por_item: dict = {}
    guias_nac_por_item: dict = {}
    qty_desp: dict = {}
    recibidos: dict = {}
    if item_ids:
        for item_id, emb_num, emb_awb_num in (
            db.query(EmbarqueItem.item_cotizacion_id, Embarque.numero, Embarque.awb_numero)
            .join(Embarque, Embarque.id == EmbarqueItem.embarque_id)
            .filter(EmbarqueItem.item_cotizacion_id.in_(item_ids))
            .all()
        ):
            emb_por_item.setdefault(item_id, []).append(
                {"numero": emb_num, "awb_numero": emb_awb_num})
        for item_id, guia in (
            db.query(RecepcionNacionalItem.item_cotizacion_id,
                     RecepcionNacional.numero_guia_proveedor)
            .join(RecepcionNacional,
                  RecepcionNacional.id == RecepcionNacionalItem.recepcion_id)
            .filter(RecepcionNacionalItem.item_cotizacion_id.in_(item_ids))
            .all()
        ):
            if guia:
                guias_nac_por_item.setdefault(item_id, []).append(guia)
        # Cobertura por línea (misma verdad que Despachos): recibido y disponible.
        recibidos = _qty_recibida_utilizable(db, item_ids)
        for item_id, suma in (
            db.query(DespachoItem.item_cotizacion_id,
                     func.sum(DespachoItem.qty_despachada))
            .join(Despacho, Despacho.id == DespachoItem.despacho_id)
            .filter(
                DespachoItem.item_cotizacion_id.in_(item_ids),
                Despacho.estado != "anulado",
            )
            .group_by(DespachoItem.item_cotizacion_id)
            .all()
        ):
            qty_desp[item_id] = float(suma or 0)

    variantes_por_token = [_variantes_token(t) for t in tokens]
    colapsados = [_colapsar(t) for t in tokens if any(c.isdigit() for c in t)]

    # Disponible por línea DELEGADO en _disponibles_por_item (despachos.py): aquí
    # vivía la ÚLTIMA copia inline de la fórmula (gate en_bodega + max(tope−ya, 0))
    # y con esto queda ÚNICA en el sistema — detalle, listado, /counts, panel,
    # guard de creación y este buscador de picking dicen todos lo mismo. `qty_desp`
    # cumple el contrato de `qty_already` (Σ despachada de despachos no anulados;
    # los item_cotizacion_id son únicos globalmente, así que filtrar por item en
    # vez de por OC trae las mismas filas).
    #
    # NO es la salida exacta de la copia que reemplazó, y así debe ser: la
    # tolerancia (_es_despachable, 0.001) ahora vive DENTRO de
    # _disponibles_por_item, que colapsa el residuo flotante a 0 pelado. La copia
    # inline emitía el crudo, así que este buscador llegaba a imprimir
    # «1.11e-16 disponible» mientras el listado y el panel daban la línea por
    # agotada. Único cambio de comportamiento, deliberado y exigido por la sonda
    # e2b de routers/tests/test_listo_para_despachar.py («buscador de Bodega:
    # mismo 0, 6º consumidor de la fórmula»).
    disponibles = _disponibles_por_item(
        [it for it, _cot, _occ in rows], recibidos, qty_desp)

    items_out = []
    for it, cot, occ in rows:
        # float(): la fórmula compartida devuelve int 0 fuera de bodega y este
        # endpoint siempre emitió 0.0 — el contrato JSON no debe moverse.
        disponible = float(disponibles.get(it.id, 0))

        match: list = []
        if tokens:
            encontrados: dict = {}

            def _agregar(campo, valor):
                if valor and campo not in encontrados:
                    encontrados[campo] = valor

            if _matchea(it.numero_parte, variantes_por_token):
                _agregar("numero_parte", it.numero_parte)
            if _matchea(it.descripcion, variantes_por_token) or \
                    _matchea(it.nombre_cat, variantes_por_token):
                _agregar("repuesto", it.descripcion or it.nombre_cat)
            if _matchea(it.marca, variantes_por_token):
                _agregar("marca", it.marca)
            if normalizado and it.numero_parte and any(
                    c in _colapsar(it.numero_parte) for c in colapsados):
                _agregar("numero_parte_colapsado", it.numero_parte)
            if _matchea(cot.numero, variantes_por_token):
                _agregar("cotizacion", cot.numero)
            if _matchea(cot.cliente, variantes_por_token):
                _agregar("cliente", cot.cliente)
            if occ is not None and _matchea(occ.numero_oc, variantes_por_token):
                _agregar("oc_cliente", occ.numero_oc)
            for emb in emb_por_item.get(it.id, []):
                if _matchea(emb["numero"], variantes_por_token):
                    _agregar("embarque", emb["numero"])
                if _matchea(emb["awb_numero"], variantes_por_token):
                    _agregar("awb", emb["awb_numero"])
            for guia in guias_nac_por_item.get(it.id, []):
                if _matchea(guia, variantes_por_token):
                    _agregar("guia_nacional", guia)
            match = [{"campo": c, "valor": v} for c, v in encontrados.items()]

        items_out.append({
            "item_cotizacion_id": it.id,
            "numero_parte": it.numero_parte or "",
            # Lo que la pantalla imprime (mismo criterio que _item_detail):
            "descripcion": it.descripcion or it.nombre_cat or "",
            "marca": it.marca or "",
            "cantidad": float(it.cantidad or 0),
            "estado_item": it.estado_item,
            # None = sin recepción registrada (flujo antiguo / carga manual)
            "qty_recibida": (
                float(recibidos[it.id]) if it.id in recibidos else None),
            "qty_disponible": disponible,
            "cotizacion_id": cot.id,
            "numero_cotizacion": cot.numero,
            "cliente": cot.cliente,
            "numero_oc_cliente": occ.numero_oc if occ else None,
            "embarques": emb_por_item.get(it.id, []),
            # Identificador del ítem NACIONAL (no tiene N° de embarque; decirlo
            # en pantalla, no inventar un join):
            "guias_nacionales": guias_nac_por_item.get(it.id, []),
            "match": match,
        })

    return {
        "items": items_out,
        "total": total,
        "page": page,
        "page_size": page_size,
        "q_efectivo": " ".join(tokens),
        "normalizado": normalizado,
    }
