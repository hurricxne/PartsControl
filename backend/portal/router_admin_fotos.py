"""/api/portal-admin/* — las fotos que el PERSONAL sube para que las vea el cliente.

  GET    /portal-admin/items/{item_id}/fotos   fotos de un ítem (para la pantalla Despachos)
  POST   /portal-admin/items/{item_id}/fotos   subir una foto → queda PUBLICADA para el cliente
  GET    /portal-admin/fotos/{foto_id}         la imagen (vista previa interna)
  DELETE /portal-admin/fotos/{foto_id}         borrarla (deja de verse en el portal)

Quién entra: cuentas internas de la marca 'mineria' (require_empresa en el router).

SUBIR ES PUBLICAR. No hay un paso de "aprobar": la foto que se sube acá la ve el cliente
en el seguimiento de ese ítem, apenas tenga cuenta y la venta esté vinculada a su ficha.
Por eso la pantalla lo dice antes de subir, y por eso estas fotos viven en una tabla
aparte de las de recepción en bodega (portal/models.py::FotoArticulo): acá solo entra lo
que alguien eligió mostrar.

Lo que se guarda es un derivado sin metadatos, nunca el archivo original (portal/fotos.py).
Qué ítems admiten fotos lo decide la misma regla con que se le muestran al cliente
(portal/visibles.py): mientras el ítem está en reclamo al proveedor, ni se suben ni se ven.

EL TAMAÑO DE LA SUBIDA. `fotos.PESO_MAXIMO_SUBIDA` protege la memoria de este proceso (se
lee ese largo más un byte y, si hay más, se rechaza). No evita que el archivo llegue: el
servidor recibe el cuerpo completo antes de que este código corra. El tope de lo que se
puede subir es `client_max_body_size` de nginx.
"""
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from empresa_guard import require_empresa
from models.models import ItemCotizacion, OcCliente, User
from . import fotos
from .deps import RutaDelPortal
from .descargas import CABECERAS_PRIVADAS
from .models import FotoArticulo
from .tiempo import dia_chile
from .visibles import ESTADO_ITEM_EN_RECLAMO

logger = logging.getLogger("portal")

router = APIRouter(
    prefix="/portal-admin",
    tags=["portal-admin"],
    route_class=RutaDelPortal,
    dependencies=[Depends(require_empresa("mineria"))],
)


def _item_vendido(db: Session, item_id: int, bloquear: bool = False) -> ItemCotizacion:
    """El ítem, si pertenece a una venta cerrada (con orden de compra del cliente). Con
    `bloquear`, la fila queda tomada hasta el commit (SELECT … FOR UPDATE)."""
    consulta = (
        db.query(ItemCotizacion)
        .join(OcCliente, OcCliente.cotizacion_id == ItemCotizacion.cotizacion_id)
        .filter(ItemCotizacion.id == item_id)
    )
    if bloquear:
        consulta = consulta.with_for_update()
    item = consulta.first()
    if item is None:
        raise HTTPException(404, "Ítem no encontrado en una venta cerrada")
    return item


def _foto_json(foto: FotoArticulo) -> dict:
    return {"id": foto.id, "ancho": foto.ancho, "alto": foto.alto,
            "peso_bytes": foto.peso_bytes, "fecha": dia_chile(foto.created_at)}


def _exigir_que_admita_fotos(db: Session, item: ItemCotizacion) -> None:
    if item.estado_item == ESTADO_ITEM_EN_RECLAMO:
        raise HTTPException(409, "Este ítem está en reclamo al proveedor: no se publican fotos suyas.")
    cuantas = db.query(FotoArticulo).filter(FotoArticulo.item_cotizacion_id == item.id).count()
    if cuantas >= fotos.FOTOS_MAXIMAS_POR_ITEM:
        raise HTTPException(
            409, f"Este ítem ya tiene {cuantas} fotos (el máximo es {fotos.FOTOS_MAXIMAS_POR_ITEM}). "
                 "Borra una para subir otra.")


@router.get("/items/{item_id}/fotos")
def listar_fotos(item_id: int, db: Session = Depends(get_db)):
    _item_vendido(db, item_id)
    filas = (db.query(FotoArticulo).filter(FotoArticulo.item_cotizacion_id == item_id)
             .order_by(FotoArticulo.created_at.asc(), FotoArticulo.id.asc()).all())
    return {"maximo": fotos.FOTOS_MAXIMAS_POR_ITEM, "fotos": [_foto_json(f) for f in filas]}


@router.post("/items/{item_id}/fotos", status_code=201)
def subir_foto(
    item_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Primero lo barato: si el ítem no admite fotos, no se gasta nada en procesar la imagen.
    _exigir_que_admita_fotos(db, _item_vendido(db, item_id))
    # Un byte más que el tope: alcanza para saber que se pasó sin cargarlo entero en memoria.
    contenido = file.file.read(fotos.PESO_MAXIMO_SUBIDA + 1)
    try:
        datos, ancho, alto = fotos.derivado(contenido)
    except fotos.FotoError as exc:
        raise HTTPException(400, str(exc))

    # El máximo se vuelve a contar con la fila del ítem BLOQUEADA: sin eso, varias subidas
    # simultáneas (el operador elige seis fotos de una vez) leen el mismo conteo y pasan
    # todas. El bloqueo va después de procesar la imagen para que dure milisegundos.
    db.rollback()
    _exigir_que_admita_fotos(db, _item_vendido(db, item_id, bloquear=True))
    archivo = fotos.guardar(datos)
    try:
        foto = FotoArticulo(
            item_cotizacion_id=item_id, archivo=archivo, ancho=ancho, alto=alto,
            peso_bytes=len(datos), subida_por_id=current_user.id, created_at=datetime.utcnow())
        db.add(foto)
        db.commit()
    except Exception:
        # Sin fila no hay foto: el archivo recién escrito no puede quedar suelto en disco.
        db.rollback()
        fotos.borrar_archivo(archivo)
        raise
    logger.info("portal-admin: usuario %s publicó la foto %s del ítem %s", current_user.id, foto.id, item_id)
    return _foto_json(foto)


@router.get("/fotos/{foto_id}")
def ver_foto(foto_id: int, db: Session = Depends(get_db)):
    foto = db.query(FotoArticulo).filter(FotoArticulo.id == foto_id).first()
    ruta = fotos.ruta_de(foto.archivo) if foto is not None else None
    if ruta is None:
        raise HTTPException(404, "Foto no encontrada")
    return FileResponse(ruta, media_type="image/jpeg", headers=CABECERAS_PRIVADAS)


@router.delete("/fotos/{foto_id}", status_code=204)
def borrar_foto(
    foto_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    foto = db.query(FotoArticulo).filter(FotoArticulo.id == foto_id).first()
    if foto is None:
        raise HTTPException(404, "Foto no encontrada")
    archivo, item_id = foto.archivo, foto.item_cotizacion_id
    db.delete(foto)
    db.commit()
    # Primero la fila, después el archivo: si el borrado en disco fallara, queda un
    # archivo suelto que nadie puede pedir (sin fila no hay ruta), nunca una fila rota.
    fotos.borrar_archivo(archivo)
    logger.info("portal-admin: usuario %s borró la foto %s del ítem %s", current_user.id, foto_id, item_id)
