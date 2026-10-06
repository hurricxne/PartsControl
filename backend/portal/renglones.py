"""Los ítems de una orden como los ve el cliente: la carga de sus datos y el armado.

QUÉ ES "UN ÍTEM" PARA EL CLIENTE (`Renglon`)
    Un renglón de su orden de compra. En la base, ese renglón puede estar partido en
    varias filas de items_cotizacion (los splits de compra parcial, preparación parcial
    y cierre de pre-embarque clonan la fila con otra cantidad). El portal las vuelve a
    juntar (`_clave_de_linea`): si pidió 14 renglones, ve 14, y cuando las unidades van
    en estados distintos se le dice cuántas hay en cada uno (`_partes_de_fila`).

ENTREGADO = SALIÓ EN UN DESPACHO CERRADO, menos lo que el cliente firmó como NO recibido.
    No exige guía firmada. Lo que el cliente declaró no haber recibido (firma parcial)
    se muestra «No recibido en la entrega»: no cuenta como entregado ni como «por
    entregar», porque el ERP no lo repone por esta venta (ver portal/estados.py). Mientras
    el dueño no decida otra cosa, la orden que tiene unidades así sigue ABIERTA.

TRES CANTIDADES de un renglón, que no hay que confundir:
    cantidad               lo que el cliente pidió
    cantidad_entregada     lo que salió y no declaró como no recibido
    cantidad_por_entregar  lo que todavía se espera bajo esta orden
  La diferencia entre la primera y las otras dos es lo «no recibido en la entrega».

`Renglon` es de uso interno: la cabecera de la orden, el tablero y el seguimiento leen
sus campos. Al navegador viaja `Renglon.publico()`, armado campo por campo; los montos
los arma portal/valor.py, y con perfil 'bodega' no sale ninguno.

CUIDADO AL LLAMAR: `cargar` NO filtra por cliente. Recibe las órdenes que ya entregó
portal/ordenes.py::base (la consulta con el candado) y carga lo de esas ventas.
"""
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Dict, List, Optional, Tuple

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from models.models import (
    Cliente, Despacho, DespachoItem, Embarque, EmbarqueItem, ItemCotizacion, OcCliente,
    ReclamoProveedor, RecepcionEmbarque, RecepcionEmbarqueItem,
)
from recepcion_nacional.models import RecepcionNacional, RecepcionNacionalItem
from . import documentos, estados, precios, valor
from .deps import PortalScope
from .models import FotoArticulo
from .visibles import ESTADO_ITEM_EN_RECLAMO, con_fotos_publicables, despacho_visible

# Vocabulario del ERP que este archivo lee.
ESTADO_ITEM_DESPACHADO = "despachado"
ESTADO_ITEM_EN_BODEGA = "en_bodega"
ESTADO_ITEM_COMPRADO = "comprado"
RECEPCION_CERRADA = "cerrada"
RECLAMO_ANULADO = "anulado"
# Estados de recepción cuyas unidades quedan utilizables en bodega (el mismo vocabulario
# que routers/despachos.py::_RECEPCION_UTILIZABLE: 'faltante' cuenta lo que SÍ llegó).
RECEPCION_UTILIZABLE = ("completo", "danado_utilizable", "sobrante", "faltante")
TOLERANCIA = valor.TOLERANCIA


def firmado_con_faltante():
    """Condición SQL sobre (Despacho, DespachoItem): la guía está firmada y de esa línea
    el cliente firmó menos de lo que viajó. qty_firmada NULL = firmó todo."""
    return and_(Despacho.guia_firmada == 1, DespachoItem.qty_firmada.isnot(None),
                DespachoItem.qty_firmada < DespachoItem.qty_despachada - TOLERANCIA)


# ── Carga en lote ───────────────────────────────────────────────────────────────────────

@dataclass
class Datos:
    """Todo lo que hace falta para armar un conjunto de órdenes, cargado con una consulta
    por tabla (y no una por orden o por ítem). Las llaves son ids de items_cotizacion."""
    items_por_cot: Dict[int, List[ItemCotizacion]] = field(default_factory=dict)
    estado_embarque: Dict[int, str] = field(default_factory=dict)  # el del embarque que lo trae
    despachado: Dict[int, float] = field(default_factory=dict)     # unidades en despachos CERRADOS
    no_recibido: Dict[int, float] = field(default_factory=dict)    # de esas, las que el cliente firmó como NO recibidas
    # Unidades recibidas en bodega. OJO: un ítem que NO está en el dict no tiene recepción
    # registrada (no es "recibí cero").
    recibido: Dict[int, float] = field(default_factory=dict)
    con_reclamo: set = field(default_factory=set)                  # tienen un reclamo al proveedor no anulado
    facturas: List[documentos.FacturaVisible] = field(default_factory=list)  # las de estas ventas
    facturado: Dict[int, documentos.Facturado] = field(default_factory=dict)
    fotos: Dict[int, int] = field(default_factory=dict)            # cuántas fotos tiene para el cliente
    # Para valorizar (portal/valor.py). Vacíos con perfil 'bodega'.
    precio_calculado: Dict[int, float] = field(default_factory=dict)  # sin el ítem = la fila no tiene monto
    en_guias: Dict[int, List[valor.EnGuia]] = field(default_factory=dict)  # en guías emitidas, aún sin facturar
    # cliente_id → nombre; solo si la cuenta representa a más de una empresa (si no, {}:
    # no hace falta decirle de cuál es cada orden).
    empresas: Dict[int, str] = field(default_factory=dict)


def cargar(db: Session, scope: PortalScope, ocs: List[OcCliente]) -> Datos:
    """Los datos de las ventas de `ocs`, que deben venir de `base`."""
    datos = Datos(empresas=_empresas(db, scope))
    cot_ids = [oc.cotizacion_id for oc in ocs]
    if not cot_ids:
        return datos
    datos.items_por_cot = {cot_id: [] for cot_id in cot_ids}
    for it in (db.query(ItemCotizacion).filter(ItemCotizacion.cotizacion_id.in_(cot_ids))
               .order_by(ItemCotizacion.id).all()):
        datos.items_por_cot[it.cotizacion_id].append(it)

    def de_estas_ventas(consulta, columna_item):
        """Acota una consulta a los ítems de estas ventas uniendo con items_cotizacion y
        filtrando por la cotización: un IN con los ids de todos los ítems serían miles."""
        return (consulta.join(ItemCotizacion, ItemCotizacion.id == columna_item)
                .filter(ItemCotizacion.cotizacion_id.in_(cot_ids)))

    # Un ítem puede figurar en más de un embarque: manda el último (id mayor).
    for item_id, estado in de_estas_ventas(
        db.query(EmbarqueItem.item_cotizacion_id, Embarque.estado)
        .join(Embarque, Embarque.id == EmbarqueItem.embarque_id), EmbarqueItem.item_cotizacion_id,
    ).order_by(EmbarqueItem.id).all():
        datos.estado_embarque[item_id] = estado

    # Las líneas de los despachos cerrados, la más nueva primero.
    lineas_de_despacho = []
    for linea_id, item_id, despacho_id, despachadas, firmadas, guia_firmada in de_estas_ventas(
        db.query(DespachoItem.id, DespachoItem.item_cotizacion_id, DespachoItem.despacho_id,
                 DespachoItem.qty_despachada, DespachoItem.qty_firmada, Despacho.guia_firmada)
        .join(Despacho, Despacho.id == DespachoItem.despacho_id), DespachoItem.item_cotizacion_id,
    ).filter(despacho_visible()).order_by(Despacho.fecha_despacho.desc(), DespachoItem.id.desc()).all():
        despachadas = float(despachadas or 0)
        datos.despachado[item_id] = datos.despachado.get(item_id, 0.0) + despachadas
        # La misma regla que firmado_con_faltante(): firmada, y firmó menos de lo que viajó.
        if guia_firmada and firmadas is not None and float(firmadas) < despachadas - TOLERANCIA:
            datos.no_recibido[item_id] = datos.no_recibido.get(item_id, 0.0) + despachadas - float(firmadas)
        lineas_de_despacho.append((linea_id, item_id, despacho_id, despachadas))

    datos.recibido = _recibido_en_bodega(db, de_estas_ventas)
    datos.con_reclamo = {
        item_id for (item_id,) in de_estas_ventas(
            db.query(ReclamoProveedor.item_cotizacion_id), ReclamoProveedor.item_cotizacion_id,
        ).filter(or_(ReclamoProveedor.estado.is_(None), ReclamoProveedor.estado != RECLAMO_ANULADO))
        .distinct().all()
    }
    datos.facturas = documentos.facturas_visibles(db, ocs)
    datos.facturado = documentos.facturado_por_item(db, datos.facturas)
    datos.fotos = dict(
        de_estas_ventas(db.query(FotoArticulo.item_cotizacion_id, func.count(FotoArticulo.id)),
                        FotoArticulo.item_cotizacion_id)
        .filter(con_fotos_publicables()).group_by(FotoArticulo.item_cotizacion_id).all()
    )
    if scope.ve_montos:
        cfg = precios.config_global(db)
        for oc in ocs:
            datos.precio_calculado.update(precios.precios_unitarios(
                oc.cotizacion, datos.items_por_cot[oc.cotizacion_id], cfg))
        datos.en_guias = valor.en_guias_sin_facturar(
            db, lineas_de_despacho, {item_id: f.lineas for item_id, f in datos.facturado.items()})
    return datos


def _recibido_en_bodega(db: Session, de_estas_ventas: Callable) -> Dict[int, float]:
    """Unidades utilizables recibidas por ítem, de recepciones CERRADAS (embarque y
    compra nacional). Misma regla que el tope físico de Despachos
    (routers/despachos.py::_qty_recibida_utilizable): solo aparecen los ítems que TIENEN
    registro de recepción."""
    recibido: Dict[int, float] = {}
    for item_id, qty in de_estas_ventas(
        db.query(EmbarqueItem.item_cotizacion_id, RecepcionEmbarqueItem.qty_recibida)
        .join(RecepcionEmbarqueItem, RecepcionEmbarqueItem.embarque_item_id == EmbarqueItem.id)
        .join(RecepcionEmbarque, RecepcionEmbarque.id == RecepcionEmbarqueItem.recepcion_id),
        EmbarqueItem.item_cotizacion_id,
    ).filter(RecepcionEmbarque.estado == RECEPCION_CERRADA,
             RecepcionEmbarqueItem.estado_recepcion.in_(RECEPCION_UTILIZABLE)).all():
        recibido[item_id] = recibido.get(item_id, 0.0) + max(float(qty or 0), 0.0)
    for item_id, qty in de_estas_ventas(
        db.query(RecepcionNacionalItem.item_cotizacion_id, RecepcionNacionalItem.qty_recibida)
        .join(RecepcionNacional, RecepcionNacional.id == RecepcionNacionalItem.recepcion_id),
        RecepcionNacionalItem.item_cotizacion_id,
    ).filter(RecepcionNacional.estado == RECEPCION_CERRADA,
             RecepcionNacionalItem.estado_recepcion.in_(RECEPCION_UTILIZABLE)).all():
        recibido[item_id] = recibido.get(item_id, 0.0) + max(float(qty or 0), 0.0)
    return recibido


def _empresas(db: Session, scope: PortalScope) -> Dict[int, str]:
    if len(scope.cliente_ids) < 2:
        return {}
    return {cliente_id: nombre or "" for cliente_id, nombre in
            db.query(Cliente.id, Cliente.nombre).filter(Cliente.id.in_(scope.cliente_ids)).all()}


# ── El renglón ──────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Parte:
    """Cuántas unidades del ítem están en un estado."""
    cantidad: float
    estado: estados.EstadoCliente


@dataclass(frozen=True)
class Renglon:
    """Un ítem de la orden como lo ve el cliente, con todo lo ya calculado. Es de uso
    interno (cabecera, tablero y seguimiento leen de acá); al navegador viaja `publico()`."""
    id: int                           # el menor id de sus filas: la llave de la URL
    item_ids: Tuple[int, ...]         # todas sus filas de items_cotizacion
    linea: Optional[int]              # posición en la orden de compra
    numero_parte: str
    descripcion: str
    marca: str
    cantidad: float
    cantidad_entregada: float
    cantidad_por_entregar: float      # lo que todavía se espera bajo esta orden
    partes: Tuple[Parte, ...]         # una por estado: incidencias primero, después el recorrido
    estado: estados.EstadoCliente     # el de la primera parte que no está entregada
    fecha_prometida: Optional[date]   # la comprometida al cierre; no es una estimación
    vencida: bool                     # la fecha pasó y todavía se espera algo
    # Montos de venta, netos (portal/valor.py). `total_clp` None = el ítem no tiene monto
    # que mostrar. `unitario_clp` None con total = sus unidades tienen más de un precio.
    unitario_clp: Optional[float]
    total_clp: Optional[int]
    por_entregar_clp: Optional[int]
    fotos: int

    @property
    def entregado(self) -> bool:
        return all(parte.estado.entregado for parte in self.partes)

    def publico(self) -> dict:
        monto = None
        if self.total_clp is not None:
            monto = {"unitario_clp": self.unitario_clp, "total_clp": self.total_clp}
        return {
            "id": self.id,
            "linea": self.linea,
            "numero_parte": self.numero_parte,
            "descripcion": self.descripcion,
            "marca": self.marca,
            "cantidad": self.cantidad,
            "estado": _estado_json(self.estado),
            "entregado": self.entregado,
            # El desglose solo cuando hay más de un estado: con uno, es el del ítem.
            "partes": ([{"cantidad": p.cantidad, "estado": _estado_json(p.estado)} for p in self.partes]
                       if len(self.partes) > 1 else []),
            "fecha_prometida": self.fecha_prometida.isoformat() if self.fecha_prometida else None,
            "vencida": self.vencida,
            "cantidad_entregada": self.cantidad_entregada,
            "cantidad_por_entregar": self.cantidad_por_entregar,
            "monto": monto,
            "fotos": self.fotos,
        }


def _estado_json(estado: estados.EstadoCliente) -> dict:
    return {"clave": estado.clave, "etiqueta": estado.etiqueta}


def _clave_de_linea(it: ItemCotizacion) -> tuple:
    """Las filas que salieron de un mismo renglón de la orden comparten todo esto (el
    clon de un split copia cada campo y solo cambia cantidad y estado). `item_num` solo
    no alcanza: en una carga desde Excel puede venir vacío o repetido. Margen y peso
    están porque de ellos depende el precio de venta: dos filas con distinto margen son
    dos precios, y un renglón tiene uno."""
    return (it.item_num, it.numero_parte or "", it.descripcion or it.nombre_cat or "",
            it.marca or "", it.precio_unit_cotizacion or 0, it.margen_pct, it.peso_unit_lbs)


def _cantidad(it: ItemCotizacion) -> float:
    """La cantidad de la fila; una cantidad vacía o negativa (dato malo) cuenta como 0."""
    return max(float(it.cantidad or 0), 0.0)


def _partes_de_fila(it: ItemCotizacion, datos: Datos) -> List[Parte]:
    """Cuántas unidades de la fila están en cada estado.

    Antes de llegar a bodega es una sola parte: toda la fila en el estado del ítem. Desde
    ahí el ERP ya no parte la fila, así que se desglosa por unidades:
      · salieron en despachos cerrados → «despachado», o «facturado» las que además ya
        figuran en una factura; menos las que el cliente firmó como NO recibidas, que
        van como «no recibido en la entrega»;
      · llegaron y siguen en bodega → «en bodega»;
      · no llegaron en una recepción ya cerrada → «en gestión» si el ítem tiene un
        reclamo al proveedor; si no, «en proceso de compra» (en la compra nacional el
        proveedor entrega por tandas y eso no es una incidencia).
    La suma de las partes es siempre la cantidad de la fila.
    """
    cantidad = _cantidad(it)
    if it.estado_item not in (ESTADO_ITEM_EN_BODEGA, ESTADO_ITEM_DESPACHADO):
        return [Parte(cantidad, estados.estado_cliente(it.estado_item, datos.estado_embarque.get(it.id)))]

    if it.estado_item == ESTADO_ITEM_DESPACHADO:
        # El ERP pasa la fila a 'despachado' cuando salió completa.
        salieron, en_bodega, faltan = cantidad, 0.0, 0.0
    else:
        salieron = min(datos.despachado.get(it.id, 0.0), cantidad)
        # Sin registro de recepción (flujo antiguo, carga manual): no se acota, llegó todo.
        llegaron = min(datos.recibido[it.id], cantidad) if it.id in datos.recibido else cantidad
        en_bodega = max(llegaron - salieron, 0.0)
        faltan = max(cantidad - max(llegaron, salieron), 0.0)
    no_recibidas = min(datos.no_recibido.get(it.id, 0.0), salieron)
    entregadas = salieron - no_recibidas
    facturado = datos.facturado.get(it.id)
    facturadas = min(facturado.cantidad, entregadas) if facturado else 0.0

    en_gestion = estados.estado_cliente(ESTADO_ITEM_EN_RECLAMO)
    por_estado = (
        (facturadas, estados.estado_cliente(ESTADO_ITEM_DESPACHADO, facturado=True)),
        (entregadas - facturadas, estados.estado_cliente(ESTADO_ITEM_DESPACHADO)),
        (en_bodega, estados.estado_cliente(ESTADO_ITEM_EN_BODEGA)),
        (faltan, en_gestion if it.id in datos.con_reclamo else estados.estado_cliente(ESTADO_ITEM_COMPRADO)),
        (no_recibidas, estados.no_recibido()),
    )
    partes = [Parte(unidades, estado) for unidades, estado in por_estado if unidades > TOLERANCIA]
    return partes or [Parte(cantidad, estados.estado_cliente(it.estado_item))]


def _renglon(filas: List[ItemCotizacion], oc: OcCliente, datos: Datos, hoy: date) -> Renglon:
    """El renglón que forman esas filas (todas las de una misma `_clave_de_linea`)."""
    por_fila = [(it, _partes_de_fila(it, datos)) for it in filas]

    unidades: Dict[str, float] = {}
    estado_de: Dict[str, estados.EstadoCliente] = {}
    for _, partes_de_la_fila in por_fila:
        for parte in partes_de_la_fila:
            unidades[parte.estado.clave] = unidades.get(parte.estado.clave, 0.0) + parte.cantidad
            estado_de[parte.estado.clave] = parte.estado
    partes = tuple(Parte(unidades[clave], estado_de[clave])
                   for clave in sorted(unidades, key=estados.posicion))
    sin_entregar = [parte for parte in partes if not parte.estado.entregado]
    # "El estado" del renglón es el de la primera parte que no está entregada: una
    # incidencia si la hay y, si no, la que va MÁS ATRÁS en el recorrido, que es por la
    # que el cliente pregunta. (`partes` ya viene en ese orden.)
    principal = (sin_entregar or partes)[0].estado

    # La fecha comprometida: la congelada al cierre; si la venta es anterior a ese
    # congelado, la fecha de entrega de la orden. Nunca un cálculo en vivo (se movería).
    # Manda la de las filas de las que todavía se espera algo.
    con_pendiente = [it for it, ps in por_fila if any(p.estado.por_entregar for p in ps)]
    fechas = [it.fecha_promesa_cliente or oc.fecha_entrega for it in (con_pendiente or filas)]
    fechas = [f for f in fechas if f]
    fecha = min(fechas) if fechas else None

    cantidad = sum(_cantidad(it) for it in filas)
    entregada = sum(parte.cantidad for parte in partes if parte.estado.entregado)
    por_entregar = sum(parte.cantidad for parte in partes if parte.estado.por_entregar)
    unitario, total, valor_por_entregar = valor.montos(
        [valor.FilaValorizable(
            cantidad=_cantidad(it), calculado=datos.precio_calculado.get(it.id),
            facturadas=datos.facturado[it.id].lineas if it.id in datos.facturado else (),
            en_guias=datos.en_guias.get(it.id, ()))
         for it in filas],
        por_entregar)
    primera = filas[0]
    return Renglon(
        id=min(it.id for it in filas),
        item_ids=tuple(it.id for it in filas),
        linea=int(primera.item_num) if primera.item_num is not None else None,
        numero_parte=primera.numero_parte or "",
        descripcion=primera.descripcion or primera.nombre_cat or "",
        marca=primera.marca or "",
        cantidad=cantidad,
        cantidad_entregada=entregada,
        cantidad_por_entregar=por_entregar,
        partes=partes,
        estado=principal,
        fecha_prometida=fecha,
        vencida=bool(fecha and por_entregar > TOLERANCIA and fecha < hoy),
        unitario_clp=unitario,
        total_clp=total,
        por_entregar_clp=valor_por_entregar,
        fotos=sum(datos.fotos.get(it.id, 0) for it in filas),
    )


def lineas(oc: OcCliente, datos: Datos, hoy: date) -> List[Renglon]:
    """Los renglones de la orden. Primero lo que no está entregado, lo más atrasado
    arriba (es por lo que el cliente abre el portal); lo entregado al final. La pantalla
    puede reordenar por `linea` para cuadrar contra la orden de compra."""
    grupos: Dict[tuple, List[ItemCotizacion]] = {}
    for it in datos.items_por_cot.get(oc.cotizacion_id, []):
        grupos.setdefault(_clave_de_linea(it), []).append(it)
    renglones = [_renglon(filas, oc, datos, hoy) for filas in grupos.values()]
    renglones.sort(key=lambda r: (r.entregado, r.fecha_prometida or date.max,
                                  r.linea if r.linea is not None else 10**9, r.id))
    return renglones
