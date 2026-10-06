"""Fábricas y limpieza compartidas por las suites del portal (no es una suite).

Todo dato lleva la MARCA de la suite que lo crea (sin guiones bajos: en LIKE el _ es
comodín) y la limpieza es POR MARCA, antes de sembrar y en finally: una corrida que
muere a medio camino no rompe la siguiente.

Los pedidos se crean escribiendo las tablas directamente (no por los endpoints del ERP):
las suites del portal prueban lo que el portal LEE, y necesitan poder armar estados que
el flujo normal tarda varios pasos en producir (o no produce nunca, como un dato malo).
Cada fábrica escribe lo que escribiría el ERP en ese paso; donde el ERP guarda algo de
una forma particular (la firma de la guía es un DÍA a medianoche), la fábrica hace igual.

Los archivos (fotos, PDF, guías firmadas) van a carpetas TEMPORALES mientras dura
`carpetas_de_prueba()`: una suite no escribe en backend/uploads/.
"""
import io
import json
import logging
import os
import shutil
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, time
from typing import List, Optional

from PIL import Image

from auth import TIPO_INTERNO, get_password_hash
from database import SessionLocal
from models.models import (
    Cliente, ContFacturaCliente, ContFacturaClienteItem, Cotizacion, Despacho, DespachoItem,
    Embarque, EmbarqueItem, ItemCotizacion, Notificacion, OcCliente, OcProveedor,
    OcProveedorItem, PreEmbarque, PreEmbarqueItem, ReclamoProveedor, RecepcionEmbarque,
    RecepcionEmbarqueItem, User,
)
from portal import cupo, descargas, documentos, fotos
from portal.models import FotoArticulo, PortalAcceso, PortalDescargaLog, UsuarioCliente
from portal.rut import rut_con_dv
from portal.service import crear_cuenta, rut_canonico
from portal.tiempo import hoy_chile
from recepcion_nacional.models import RecepcionNacional, RecepcionNacionalItem
from wasabil_dte.models import STATUS_EMITIDO, WasabilDte

CLAVE = "clave-de-prueba-larga-2026"
# Los archivos que estas fábricas dejan con nombre propio (guías firmadas, PDF de DTE)
# empiezan así, para reconocerlos. Es hexadecimal a propósito: el nombre de una guía
# firmada tiene que seguir pareciendo un uuid4.
PREFIJO_ARCHIVO = "0000dade"


def rut_de_prueba(cuerpo: int) -> str:
    return rut_con_dv(str(cuerpo))


@contextmanager
def carpetas_de_prueba():
    """Mientras dura, el portal lee y escribe sus archivos (fotos, copias de PDF y guías
    firmadas) en una carpeta temporal que se borra al salir. También deja en cero los
    contadores en memoria del módulo (cupo por cuenta, fallos recientes de Wasabil)."""
    base = tempfile.mkdtemp(prefix="portal-pruebas-")
    originales = (fotos.DIRECTORIO, descargas.DIRECTORIO_PDF, documentos.DIRECTORIO_DOCS)
    fotos.DIRECTORIO = os.path.join(base, "portal_fotos")
    descargas.DIRECTORIO_PDF = os.path.join(base, "portal_docs")
    documentos.DIRECTORIO_DOCS = os.path.join(base, "docs")
    for carpeta in (fotos.DIRECTORIO, descargas.DIRECTORIO_PDF, documentos.DIRECTORIO_DOCS):
        os.makedirs(carpeta)
    cupo.reset()
    descargas.reset()
    try:
        yield base
    finally:
        fotos.DIRECTORIO, descargas.DIRECTORIO_PDF, documentos.DIRECTORIO_DOCS = originales
        shutil.rmtree(base, ignore_errors=True)
        cupo.reset()
        descargas.reset()


@contextmanager
def log_del_portal():
    """Lo que el logger 'portal' escribe en la salida del proceso mientras dura el bloque
    (lo que quedaría en `pm2 logs machparts-api`).

    Se lee del MISMO manejador que instala portal/__init__.py, desviándole la salida:
    sin esa configuración no se captura nada, que es justo lo que las sondas necesitan
    ver. No se usa redirect_stderr porque el manejador quedó atado al stderr que existía
    al importar el paquete (bajo pytest, el de la captura de la sesión). Los check() van
    fuera del bloque.
    """
    registro = logging.getLogger("portal")
    captura = io.StringIO()
    manejadores = [m for m in registro.handlers if isinstance(m, logging.StreamHandler)]
    anteriores = [m.setStream(captura) for m in manejadores]
    try:
        yield captura
    finally:
        for manejador, anterior in zip(manejadores, anteriores):
            manejador.setStream(anterior)


# ── Limpieza ────────────────────────────────────────────────────────────────────────────

def limpiar(mark: str) -> None:
    db = SessionLocal()
    try:
        correo = f"{mark.lower()}%"
        uids = [u.id for u in db.query(User).filter(User.email.like(correo)).all()]
        cot_ids = [c.id for c in db.query(Cotizacion).filter(Cotizacion.numero.like(f"{mark}%")).all()]
        db.query(Notificacion).filter(Notificacion.titulo.like(f"%{mark}%")).delete(synchronize_session=False)
        archivos = _limpiar_pedidos(db, mark, cot_ids, uids)
        db.query(OcCliente).filter(OcCliente.cotizacion_id.in_(cot_ids or [0])).delete(synchronize_session=False)
        db.query(ItemCotizacion).filter(ItemCotizacion.cotizacion_id.in_(cot_ids or [0])).delete(synchronize_session=False)
        db.query(Cotizacion).filter(Cotizacion.id.in_(cot_ids or [0])).delete(synchronize_session=False)
        ficha_ids = [c.id for c in db.query(Cliente).filter(Cliente.giro == mark).all()]
        db.query(UsuarioCliente).filter(
            UsuarioCliente.usuario_id.in_(uids or [0]) | UsuarioCliente.cliente_id.in_(ficha_ids or [0])
        ).delete(synchronize_session=False)
        db.query(PortalAcceso).filter(PortalAcceso.usuario_id.in_(uids or [0])).delete(synchronize_session=False)
        db.query(User).filter(User.id.in_(uids or [0])).delete(synchronize_session=False)
        db.query(Cliente).filter(Cliente.id.in_(ficha_ids or [0])).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
    # Los archivos, recién después del commit: si la limpieza de la base fallara, las
    # filas seguirían apuntando a archivos que existen.
    for ruta in archivos:
        if os.path.isfile(ruta):
            os.remove(ruta)


def _limpiar_pedidos(db, mark: str, cot_ids: list, uids: list) -> List[str]:
    """Borra todo lo que cuelga de las ventas marcadas (despachos, embarques, compras,
    recepciones, reclamos, facturas, DTE, fotos) y la bitácora de descargas de sus
    cuentas. Devuelve las rutas de los archivos que hay que borrar después."""
    oc_ids = [i for (i,) in db.query(OcCliente.id).filter(OcCliente.cotizacion_id.in_(cot_ids or [0])).all()]
    item_ids = [i for (i,) in db.query(ItemCotizacion.id).filter(ItemCotizacion.cotizacion_id.in_(cot_ids or [0])).all()]
    despacho_ids = [i for (i,) in db.query(Despacho.id).filter(Despacho.oc_cliente_id.in_(oc_ids or [0])).all()]
    factura_ids = [i for (i,) in db.query(ContFacturaCliente.id).filter(ContFacturaCliente.oc_cliente_id.in_(oc_ids or [0])).all()]
    embarque_ids = [i for (i,) in db.query(Embarque.id).filter(Embarque.numero.like(f"{mark}%")).all()]
    archivos: List[str] = []

    for foto_ in db.query(FotoArticulo).filter(FotoArticulo.item_cotizacion_id.in_(item_ids or [0])).all():
        archivos.append(os.path.join(fotos.DIRECTORIO, foto_.archivo))
        db.delete(foto_)
    db.query(PortalDescargaLog).filter(PortalDescargaLog.usuario_id.in_(uids or [0])).delete(synchronize_session=False)
    for dte_ in db.query(WasabilDte).filter(
            WasabilDte.despacho_id.in_(despacho_ids or [0]) | WasabilDte.factura_id.in_(factura_ids or [0])).all():
        # Solo uuid de prueba: una fila sembrada con un uuid en forma de ruta («../..») no
        # puede convertir esta limpieza en un borrado fuera de la carpeta.
        if dte_.uuid and dte_.uuid.startswith(PREFIJO_ARCHIVO):
            archivos.append(os.path.join(descargas.DIRECTORIO_PDF, f"{dte_.uuid}.pdf"))
        db.delete(dte_)
    db.flush()  # la sesión no hace autoflush: sin esto los DELETE de abajo chocan con las FK
    for despacho_ in db.query(Despacho).filter(Despacho.id.in_(despacho_ids or [0])).all():
        if (despacho_.guia_firmada_archivo or "").startswith(PREFIJO_ARCHIVO):
            archivos.append(os.path.join(documentos.DIRECTORIO_DOCS, despacho_.guia_firmada_archivo))
    db.query(ContFacturaClienteItem).filter(ContFacturaClienteItem.factura_id.in_(factura_ids or [0])).delete(synchronize_session=False)
    db.query(ContFacturaCliente).filter(ContFacturaCliente.id.in_(factura_ids or [0])).delete(synchronize_session=False)
    db.query(DespachoItem).filter(DespachoItem.despacho_id.in_(despacho_ids or [0])).delete(synchronize_session=False)
    db.query(Despacho).filter(Despacho.id.in_(despacho_ids or [0])).delete(synchronize_session=False)

    db.query(ReclamoProveedor).filter(ReclamoProveedor.item_cotizacion_id.in_(item_ids or [0])).delete(synchronize_session=False)
    recepcion_ids = [i for (i,) in db.query(RecepcionEmbarque.id).filter(RecepcionEmbarque.embarque_id.in_(embarque_ids or [0])).all()]
    db.query(RecepcionEmbarqueItem).filter(RecepcionEmbarqueItem.recepcion_id.in_(recepcion_ids or [0])).delete(synchronize_session=False)
    db.query(RecepcionEmbarque).filter(RecepcionEmbarque.id.in_(recepcion_ids or [0])).delete(synchronize_session=False)
    db.query(EmbarqueItem).filter(
        EmbarqueItem.embarque_id.in_(embarque_ids or [0]) | EmbarqueItem.item_cotizacion_id.in_(item_ids or [0])
    ).delete(synchronize_session=False)
    db.query(Embarque).filter(Embarque.id.in_(embarque_ids or [0])).delete(synchronize_session=False)

    nacional_ids = [i for (i,) in db.query(RecepcionNacional.id).filter(
        RecepcionNacional.numero_guia_proveedor.like(f"{mark}%")).all()]
    db.query(RecepcionNacionalItem).filter(RecepcionNacionalItem.recepcion_id.in_(nacional_ids or [0])).delete(synchronize_session=False)
    db.query(RecepcionNacional).filter(RecepcionNacional.id.in_(nacional_ids or [0])).delete(synchronize_session=False)
    db.query(PreEmbarqueItem).filter(PreEmbarqueItem.item_cotizacion_id.in_(item_ids or [0])).delete(synchronize_session=False)
    db.query(PreEmbarque).filter(PreEmbarque.numero.like(f"{mark}%")).delete(synchronize_session=False)
    db.query(OcProveedorItem).filter(OcProveedorItem.item_cotizacion_id.in_(item_ids or [0])).delete(synchronize_session=False)
    db.query(OcProveedor).filter(OcProveedor.numero.like(f"{mark}%")).delete(synchronize_session=False)
    return archivos


# ── Identidad ───────────────────────────────────────────────────────────────────────────

def ficha(db, mark: str, sufijo: str, cuerpo: int) -> Cliente:
    """Una ficha de cliente de prueba. Aborta si el RUT ya lo usa algo que NO es de esta
    marca: el vínculo por RUT le mostraría datos reales a la cuenta de prueba, y un
    choque de RUT haría fallar la siembra como si fuera una regresión."""
    rut = rut_de_prueba(cuerpo)
    canonico = rut_canonico(rut)
    ajenos = [c.nombre for c in db.query(Cliente).all()
              if c.giro != mark and rut_canonico(c.rut) == canonico]
    ajenos += [c.numero for c in db.query(Cotizacion).filter(
        Cotizacion.rut_cliente.isnot(None), ~Cotizacion.numero.like(f"{mark}%")).all()
        if rut_canonico(c.rut_cliente) == canonico]
    assert not ajenos, f"El RUT de prueba {rut} ya está en uso fuera de la marca {mark}: {ajenos[:3]}"
    c = Cliente(rut=rut, nombre=f"{mark} {sufijo}", giro=mark)
    db.add(c)
    db.flush()
    return c


def interno(db, mark: str, sufijo: str, empresa: str = "mineria") -> User:
    u = User(email=f"{mark.lower()}-{sufijo}@test.invalid", nombre=f"{mark} {sufijo}",
             hashed_password=get_password_hash(CLAVE), is_active=1, empresa=empresa,
             tipo_acceso=TIPO_INTERNO)
    db.add(u)
    db.flush()
    return u


def portal(db, mark: str, sufijo: str, cliente_ids: list, perfil: str = "procurement") -> User:
    """Una cuenta portal que ya eligió su clave (puede ver datos)."""
    u, _ = crear_cuenta(
        db, email=f"{mark.lower()}-{sufijo}@test.invalid", nombre=f"{mark} {sufijo}",
        cliente_ids=cliente_ids, perfil=perfil, creado_por_id=None, clave=CLAVE)
    return u


def ahora() -> datetime:
    return datetime.utcnow()


# ── Pedidos: una venta cerrada y lo que le va pasando ───────────────────────────────────

def venta(db, mark: str, sufijo: str, cliente: Optional[Cliente], renglones: List[dict], *,
          origen: str = "venta_clp", numero_oc: Optional[str] = None, fecha_oc: str = "10/06/2026",
          fecha_entrega: Optional[date] = None, asesor: Optional[User] = None,
          pricing_snapshot: Optional[str] = None, rut: Optional[str] = None):
    """Una venta cerrada: cotización + orden de compra del cliente + sus ítems.

    Cada renglón es un dict con: numero_parte, descripcion, cantidad, precio, estado,
    promesa, marca, item_num, margen, peso (todos opcionales). Devuelve
    (cotizacion, oc, [items]). El vínculo con la ficha lo pone el gancho de
    portal/sincronia.py a partir del RUT, igual que en una venta real.
    """
    cot = Cotizacion(
        numero=f"{mark}-{sufijo}", cliente=cliente.nombre if cliente else f"{mark} sin ficha",
        rut_cliente=rut if rut is not None else (cliente.rut if cliente else None),
        origen=origen, pricing_snapshot=pricing_snapshot, fase_comercial="cerrada")
    db.add(cot)
    db.flush()
    oc = OcCliente(cotizacion_id=cot.id, numero_oc=numero_oc or f"{mark}-OC-{sufijo}",
                   fecha_oc=fecha_oc, fecha_entrega=fecha_entrega,
                   asesor_id=asesor.id if asesor else None)
    db.add(oc)
    items = []
    for n, r in enumerate(renglones, start=1):
        it = ItemCotizacion(
            cotizacion_id=cot.id, item_num=r.get("item_num", n),
            numero_parte=r.get("numero_parte", f"{n}X-{1000 + n}"),
            descripcion=r.get("descripcion", f"REPUESTO {n}"), marca=r.get("marca", "CAT"),
            cantidad=r.get("cantidad", 1), precio_unit_cotizacion=r.get("precio", 1000.0),
            estado_item=r.get("estado", "cerrado"), fecha_promesa_cliente=r.get("promesa"),
            margen_pct=r.get("margen"), peso_unit_lbs=r.get("peso"))
        db.add(it)
        items.append(it)
    db.flush()
    return cot, oc, items


def compra(db, mark: str, sufijo: str, items: List[ItemCotizacion], *, origen: str = "internacional",
           fecha: Optional[datetime] = None, proveedor: str = "PROVEEDOR DE PRUEBA",
           pais: str = "PAIS DE PRUEBA") -> OcProveedor:
    """La asignación de esos ítems a una orden de compra al proveedor."""
    ocp = OcProveedor(numero=f"{mark}-OCP-{sufijo}", proveedor=proveedor, pais=pais, tipo_origen=origen)
    db.add(ocp)
    db.flush()
    for it in items:
        db.add(OcProveedorItem(oc_proveedor_id=ocp.id, item_cotizacion_id=it.id, fecha_asignacion=fecha))
    db.flush()
    return ocp


def pre_embarque(db, mark: str, sufijo: str, items: List[ItemCotizacion],
                 fecha: Optional[datetime] = None) -> PreEmbarque:
    pre = PreEmbarque(numero=f"{mark}-PRE-{sufijo}")
    db.add(pre)
    db.flush()
    for it in items:
        db.add(PreEmbarqueItem(pre_embarque_id=pre.id, item_cotizacion_id=it.id, created_at=fecha))
    db.flush()
    return pre


def embarque(db, mark: str, sufijo: str, items: List[ItemCotizacion], estado: str, *,
             forwarder: Optional[str] = None, awb_numero: Optional[str] = None,
             notas: Optional[str] = None) -> Embarque:
    emb = Embarque(numero=f"{mark}-EMB-{sufijo}", estado=estado, forwarder=forwarder,
                   awb_numero=awb_numero, notas=notas)
    db.add(emb)
    db.flush()
    for it in items:
        db.add(EmbarqueItem(embarque_id=emb.id, item_cotizacion_id=it.id))
    db.flush()
    return emb


def recepcion(db, emb: Embarque, recibido: dict, estado: str = "cerrada",
              fecha: Optional[datetime] = None) -> RecepcionEmbarque:
    """Recepción del embarque. `recibido` = {item_id: (qty_recibida, estado_recepcion)}."""
    rec = RecepcionEmbarque(embarque_id=emb.id, estado=estado,
                            fecha_cierre=(fecha or datetime.utcnow()) if estado == "cerrada" else None)
    db.add(rec)
    db.flush()
    for ei in db.query(EmbarqueItem).filter(EmbarqueItem.embarque_id == emb.id).all():
        if ei.item_cotizacion_id in recibido:
            qty, estado_rec = recibido[ei.item_cotizacion_id]
            db.add(RecepcionEmbarqueItem(recepcion_id=rec.id, embarque_item_id=ei.id,
                                         qty_recibida=qty, estado_recepcion=estado_rec))
    db.flush()
    return rec


def recepcion_nacional(db, mark: str, sufijo: str, recibido: dict, *, estado: str = "cerrada",
                       fecha: Optional[date] = None) -> RecepcionNacional:
    """Una entrega de un proveedor nacional. `recibido` = {item_id: (qty, estado_recepcion)}."""
    rec = RecepcionNacional(numero_guia_proveedor=f"{mark}-RN-{sufijo}", estado=estado,
                            fecha=fecha or hoy_chile())
    db.add(rec)
    db.flush()
    for item_id, (qty, estado_rec) in recibido.items():
        db.add(RecepcionNacionalItem(recepcion_id=rec.id, item_cotizacion_id=item_id,
                                     qty_recibida=qty, estado_recepcion=estado_rec))
    db.flush()
    return rec


def reclamo(db, item: ItemCotizacion, *, estado: str = "pendiente", motivo: str = "faltante",
            cantidad: int = 1, fecha: Optional[datetime] = None) -> ReclamoProveedor:
    r = ReclamoProveedor(item_cotizacion_id=item.id, motivo=motivo, qty_afectada=cantidad,
                         estado=estado, fecha_creacion=fecha or datetime.utcnow())
    db.add(r)
    db.flush()
    return r


def despacho(db, mark: str, sufijo: str, oc: OcCliente, cantidades: list, *,
             estado: str = "despachado", numero_guia: Optional[str] = None,
             fecha: Optional[datetime] = None, fecha_guia: Optional[date] = None,
             firmada: bool = False, fecha_firma: Optional[date] = None,
             archivo_firmado: Optional[str] = None, transportista: Optional[str] = None,
             numero_expedicion: Optional[str] = None, direccion_entrega: Optional[str] = None,
             observaciones: Optional[str] = None) -> Despacho:
    """Un despacho. `cantidades` = [(item, qty)] o [(item, qty, qty_firmada)].

    La firma se guarda como la escribe routers/despachos.py: el DÍA que tecleó el
    operador (por defecto, hoy en Chile), a medianoche y sin zona.
    """
    d = Despacho(
        numero_despacho=f"{mark}-DSP-{sufijo}", oc_cliente_id=oc.id, estado=estado,
        numero_guia=numero_guia, fecha_guia=fecha_guia,
        fecha_despacho=fecha or (datetime.utcnow() if estado == "despachado" else None),
        guia_firmada=1 if firmada else 0,
        fecha_firma=datetime.combine(fecha_firma or hoy_chile(), time.min) if firmada else None,
        guia_firmada_archivo=archivo_firmado, transportista=transportista,
        numero_expedicion=numero_expedicion, direccion_entrega=direccion_entrega,
        observaciones=observaciones)
    db.add(d)
    db.flush()
    for fila in cantidades:
        it, qty = fila[0], fila[1]
        db.add(DespachoItem(despacho_id=d.id, item_cotizacion_id=it.id, qty_despachada=qty,
                            qty_firmada=fila[2] if len(fila) > 2 else None))
    db.flush()
    return d


def linea_de_despacho(db, despacho_: Despacho, item: ItemCotizacion) -> DespachoItem:
    """La línea con que ese ítem viajó en ese despacho."""
    return (db.query(DespachoItem)
            .filter(DespachoItem.despacho_id == despacho_.id, DespachoItem.item_cotizacion_id == item.id).one())


def archivo_de_guia_firmada(n: int, extension: str = "pdf",
                            contenido: bytes = b"%PDF-1.4 guia firmada de prueba") -> str:
    """Escribe una guía firmada falsa en la carpeta de adjuntos y devuelve su nombre."""
    nombre = f"{PREFIJO_ARCHIVO}{n:024x}.{extension}"
    os.makedirs(documentos.DIRECTORIO_DOCS, exist_ok=True)
    with open(os.path.join(documentos.DIRECTORIO_DOCS, nombre), "wb") as destino:
        destino.write(contenido)
    return nombre


def factura(db, mark: str, sufijo: str, oc: OcCliente, lineas: list, *, folio: Optional[str] = "",
            empresa: str = "mineria", es_anticipo: bool = False, fecha: Optional[date] = None) -> ContFacturaCliente:
    """Una factura de la orden. `lineas` = [(item | None, cantidad, precio_unitario_neto)]
    o, para una línea que dice qué línea de guía factura (como las del flujo actual),
    [(item, cantidad, precio, despacho)]. `folio=""` usa uno propio de la marca;
    `folio=None` deja la factura SIN folio."""
    neto = sum(round(fila[1] * fila[2]) for fila in lineas)
    iva = round(neto * 0.19)
    f = ContFacturaCliente(
        empresa=empresa, oc_cliente_id=oc.id, cotizacion_id=oc.cotizacion_id,
        numero_factura=(f"{mark}-F{sufijo}" if folio == "" else folio),
        es_anticipo=1 if es_anticipo else 0, fecha_emision=fecha or date.today(),
        monto_neto=neto, iva=iva, monto_bruto=neto + iva, saldo=neto + iva)
    db.add(f)
    db.flush()
    for fila in lineas:
        it, qty, precio = fila[0], fila[1], fila[2]
        de_guia = linea_de_despacho(db, fila[3], it) if len(fila) > 3 else None
        db.add(ContFacturaClienteItem(
            factura_id=f.id, item_cotizacion_id=it.id if it is not None else None,
            despacho_item_id=de_guia.id if de_guia is not None else None,
            numero_parte=it.numero_parte if it is not None else None,
            cantidad=qty, precio_unit_neto=precio, total_neto=round(qty * precio)))
    db.flush()
    return f


def dte(db, *, despacho: Optional[Despacho] = None, factura: Optional[ContFacturaCliente] = None,
        uuid: str, folio: Optional[str], status_id: Optional[int] = STATUS_EMITIDO,
        receptor_rut: Optional[str] = None, fecha_documento: Optional[str] = None,
        respuesta_cruda: Optional[str] = None, precios: Optional[dict] = None) -> WasabilDte:
    """El DTE de un despacho (guía 52) o de una factura (33), como lo deja wasabil_dte.
    `precios` = {item_id: precio unitario} con que viajó cada línea de la guía (va en lo
    enviado, igual que lo arma wasabil_dte/service.py: externalId = id de la línea)."""
    enviado: dict = {"documentDate": fecha_documento} if fecha_documento else {}
    if precios and despacho is not None:
        enviado["details"] = [
            {"externalId": str(di.id), "external_id": str(di.id), "quantity": float(di.qty_despachada or 0),
             "price": precios[di.item_cotizacion_id]}
            for di in db.query(DespachoItem).filter(DespachoItem.despacho_id == despacho.id).order_by(DespachoItem.id)
            if di.item_cotizacion_id in precios
        ]
    fila = WasabilDte(
        empresa="mineria", tipo_dte=52 if despacho is not None else 33,
        despacho_id=despacho.id if despacho is not None else None,
        factura_id=factura.id if factura is not None else None,
        uuid=uuid, folio=folio, status_id=status_id,
        # El enlace público que Wasabil devuelve: está acá para probar que NO sale.
        pdf_url=f"https://publico.invalid/{uuid}.pdf", xml_url=f"https://publico.invalid/{uuid}.xml",
        payload_json=json.dumps(enviado),
        respuesta_json=respuesta_cruda if respuesta_cruda is not None
        else json.dumps({"receiver_rut": receptor_rut, "uuid": uuid}))
    db.add(fila)
    db.flush()
    return fila


def imagen_jpeg(ancho: int = 800, alto: int = 600, color=(200, 120, 40), **guardar) -> bytes:
    """Un JPEG de prueba. `guardar` pasa opciones a Pillow (exif=…, comment=…)."""
    salida = io.BytesIO()
    Image.new("RGB", (ancho, alto), color).save(salida, format="JPEG", **guardar)
    return salida.getvalue()


def foto(db, item: ItemCotizacion, contenido: Optional[bytes] = None) -> FotoArticulo:
    """Una foto publicada del ítem, guardada por el mismo camino que la subida real."""
    datos, ancho, alto = fotos.derivado(contenido or imagen_jpeg())
    fila = FotoArticulo(item_cotizacion_id=item.id, archivo=fotos.guardar(datos), ancho=ancho,
                        alto=alto, peso_bytes=len(datos), created_at=datetime.utcnow())
    db.add(fila)
    db.flush()
    return fila
