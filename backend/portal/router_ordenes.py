"""/api/portal/* — lo que ve el CLIENTE de sus pedidos. Solo cuentas portal que ya
reemplazaron su clave temporal (deps.require_portal_listo).

  GET /portal/resumen                         tablero: comprado, por entregar, dónde viene lo que falta
  GET /portal/ordenes                         mis pedidos (buscador `q`, filtro `estado`, `anio`, paginado)
  GET /portal/ordenes/{oc_id}                 una orden: sus ítems con precio, fecha comprometida y estado; sus guías y facturas
  GET /portal/ordenes/{oc_id}/items/{item_id} seguimiento de un ítem: línea de tiempo, documentos, fotos
  GET /portal/fotos/{foto_id}                 la imagen de una foto
  GET /portal/guias/{despacho_id}/pdf         el PDF de la guía electrónica
  GET /portal/guias/{despacho_id}/firmada     la guía firmada escaneada
  GET /portal/facturas/{factura_id}/pdf       el PDF de la factura electrónica

Este archivo NO consulta la base ni decide qué ve el cliente: recibe la petición, llama a
portal/ordenes.py, seguimiento.py, resumen.py, documentos.py o descargas.py —que son los
que aplican el candado del cliente (portal/candado.py) y la regla de montos
(`scope.ve_montos`)— y traduce "no está" a 404.

POR QUÉ SIEMPRE 404 Y NUNCA 403 para una orden, un ítem, una foto o un documento que no
es del cliente: los ids son correlativos; un 403 le confirmaría a quien tantea que el id
existe y es de otro. Para el portal, lo ajeno no existe.

TOPE DE FRECUENCIA (portal/cupo.py): toda ruta de acá cobra el cupo general de la cuenta
(`_cupo_general`, declarado en el router); el tablero y las descargas cobran además el
suyo. Pasado el tope responden 429.

LAS TRES DESCARGAS siguen el mismo orden, y no es intercambiable (portal/descargas.py):
resolver el documento contra las órdenes del cliente → copiar a variables lo que hace
falta → `descargas.anotar_descarga` (que cierra la sesión de la base) → entregar.
"""
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

import rate_limit_login as rl
from database import get_db
from . import cupo, descargas, documentos, fotos, ordenes, resumen, seguimiento
from .deps import PortalScope, RutaDelPortal, require_portal_listo
from .models import DESCARGA_FACTURA_PDF, DESCARGA_GUIA_FIRMADA, DESCARGA_GUIA_PDF
from .tiempo import hoy_chile

logger = logging.getLogger("portal")


def _cupo_general(scope: PortalScope = Depends(require_portal_listo)) -> None:
    cupo.cobrar(scope.usuario_id, cupo.GENERAL)


router = APIRouter(
    prefix="/portal",
    tags=["portal"],
    route_class=RutaDelPortal,
    dependencies=[Depends(require_portal_listo), Depends(_cupo_general)],
)

NO_ENCONTRADA = "Orden no encontrada"
DOCUMENTO_NO_DISPONIBLE = (
    "No pudimos obtener el documento en este momento. Intenta de nuevo en unos minutos "
    "o pídeselo a tu ejecutivo de MachParts."
)


def _sello() -> dict:
    """Lo que acompaña a toda respuesta de datos:
      · `actualizado_at`: cuándo se armó (UTC). La pantalla lo muestra como «Actualizado
        a las …»: sin eso, ante la duda el cliente llama por teléfono.
      · `hoy`: el día de hoy en Chile, para que «en 3 días» o «hace 2 días» no se
        calculen con el reloj del teléfono."""
    return {
        "actualizado_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "hoy": hoy_chile().isoformat(),
    }


def _pdf(uuid: Optional[str], nombre: str) -> FileResponse:
    """Entrega el PDF de un DTE que YA se resolvió contra las órdenes del cliente. No
    usa la base (a esta altura la sesión está cerrada)."""
    try:
        ruta = descargas.pdf_del_dte(uuid)
    except descargas.DocumentoNoDisponible as error:
        logger.warning("portal: el PDF «%s» no está disponible: %s", nombre, error)
        raise HTTPException(502, DOCUMENTO_NO_DISPONIBLE)
    return FileResponse(ruta, media_type="application/pdf", filename=nombre, headers=descargas.CABECERAS_PRIVADAS)


@router.get("/resumen")
def ver_resumen(
    anio: Optional[int] = Query(None, ge=2000, le=2100),
    scope: PortalScope = Depends(require_portal_listo),
    db: Session = Depends(get_db),
):
    cupo.cobrar(scope.usuario_id, cupo.TABLERO)
    return {**resumen.tablero(db, scope, anio), **_sello()}


@router.get("/ordenes")
def listar_ordenes(
    q: Optional[str] = Query(None, max_length=200),
    estado: str = Query("todas"),
    anio: Optional[int] = Query(None, ge=2000, le=2100),
    page: int = Query(1, ge=1, le=10_000),
    page_size: int = Query(20, ge=1, le=ordenes.PAGINA_MAXIMA),
    scope: PortalScope = Depends(require_portal_listo),
    db: Session = Depends(get_db),
):
    if estado not in ordenes.FILTROS_DE_ESTADO:
        raise HTTPException(400, f"Filtro inválido: usa uno de {', '.join(ordenes.FILTROS_DE_ESTADO)}")
    datos = ordenes.listar(db, scope, q=q, estado=estado, anio=anio, page=page, page_size=page_size)
    return {**datos, **_sello()}


@router.get("/ordenes/{oc_id}")
def ver_orden(
    oc_id: int,
    scope: PortalScope = Depends(require_portal_listo),
    db: Session = Depends(get_db),
):
    oc = ordenes.buscar_orden(db, scope, oc_id)
    if oc is None:
        raise HTTPException(404, NO_ENCONTRADA)
    return {**ordenes.detalle(db, scope, oc), **_sello()}


@router.get("/ordenes/{oc_id}/items/{item_id}")
def ver_item(
    oc_id: int,
    item_id: int,
    scope: PortalScope = Depends(require_portal_listo),
    db: Session = Depends(get_db),
):
    oc = ordenes.buscar_orden(db, scope, oc_id)
    if oc is None:
        raise HTTPException(404, NO_ENCONTRADA)
    datos = seguimiento.de_item(db, scope, oc, item_id)
    if datos is None:
        raise HTTPException(404, "Ítem no encontrado en esta orden")
    return {**datos, **_sello()}


@router.get("/fotos/{foto_id}")
def ver_foto(
    foto_id: int,
    scope: PortalScope = Depends(require_portal_listo),
    db: Session = Depends(get_db),
):
    foto = seguimiento.foto_del_cliente(db, scope, foto_id)
    ruta = fotos.ruta_de(foto.archivo) if foto is not None else None
    if ruta is None:
        raise HTTPException(404, "Foto no encontrada")
    return FileResponse(ruta, media_type="image/jpeg", headers=descargas.CABECERAS_PRIVADAS)


@router.get("/guias/{despacho_id}/pdf")
def pdf_de_guia(
    despacho_id: int,
    request: Request,
    scope: PortalScope = Depends(require_portal_listo),
    db: Session = Depends(get_db),
):
    despacho = documentos.despacho_del_cliente(db, scope, despacho_id)
    dte, _ = documentos.descargables_de_guia(db, despacho) if despacho is not None else (None, None)
    if dte is None:
        raise HTTPException(404, "Esta guía no tiene un PDF disponible")
    uuid, nombre = dte.uuid, descargas.nombre_de_archivo("guia", dte.folio, despacho.id, "pdf")
    descargas.anotar_descarga(db, scope, DESCARGA_GUIA_PDF, despacho.id, rl.ip_cliente(request))
    return _pdf(uuid, nombre)


@router.get("/guias/{despacho_id}/firmada")
def guia_firmada(
    despacho_id: int,
    request: Request,
    scope: PortalScope = Depends(require_portal_listo),
    db: Session = Depends(get_db),
):
    despacho = documentos.despacho_del_cliente(db, scope, despacho_id)
    _, ruta = documentos.descargables_de_guia(db, despacho) if despacho is not None else (None, None)
    if ruta is None:
        raise HTTPException(404, "Esta guía no tiene una copia firmada disponible")
    extension = ruta.rsplit(".", 1)[-1].lower()
    nombre = descargas.nombre_de_archivo("guia-firmada", despacho.numero_guia, despacho.id, extension)
    descargas.anotar_descarga(db, scope, DESCARGA_GUIA_FIRMADA, despacho.id, rl.ip_cliente(request))
    return FileResponse(ruta, media_type=documentos.TIPOS_GUIA_FIRMADA[extension], filename=nombre,
                        headers=descargas.CABECERAS_PRIVADAS)


@router.get("/facturas/{factura_id}/pdf")
def pdf_de_factura(
    factura_id: int,
    request: Request,
    scope: PortalScope = Depends(require_portal_listo),
    db: Session = Depends(get_db),
):
    factura = documentos.factura_del_cliente(db, scope, factura_id)
    dte = documentos.dte_de_factura(db, factura) if factura is not None else None
    if dte is None:
        raise HTTPException(404, "Esta factura no tiene un PDF disponible")
    uuid, nombre = dte.uuid, descargas.nombre_de_archivo("factura", factura.numero_factura, factura.id, "pdf")
    descargas.anotar_descarga(db, scope, DESCARGA_FACTURA_PDF, factura.id, rl.ip_cliente(request))
    return _pdf(uuid, nombre)
