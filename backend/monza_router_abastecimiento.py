"""
Abastecimiento MonzaParts - Panel de Compras + Seguimiento.
Mueve lineas de items vendidos por el pipeline REAL:
  cotizado -> por_comprar -> comprado -> preparado -> embarcado -> en_bodega
              -> despachado   (o -> reclamo, estado terminal de excepcion)
OJO (hallazgo #9 de la auditoria): 'en_transito' NO es un estado de linea que el
pipeline escriba nunca; el tramo "volando" son 'preparado' y 'embarcado'.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime

from database import get_db
from auth import get_current_user
from monza_notif import crear_notif
from monza_models import (
    MonzaCotizacion, MonzaCotizacionItem, MonzaCliente,
    MonzaProvAbast, MonzaOcProveedor,
)

# Hallazgo #13 (candado de empresa): este router queda A PROPOSITO sin
# dependencies=[Depends(require_empresa("automotriz"))]. El dueño difirió
# explícitamente el candado de los routers del programador (abastecimiento /
# cotizaciones) y las Fases 1-6 no agregaron endpoints nuevos aquí. Bodega y
# Despachos SÍ se candaron. Si el dueño lo aprueba, candar el router COMPLETO
# (nunca endpoint por endpoint, para no dejar mitades).
router = APIRouter(prefix="/api/monza/abastecimiento", tags=["monza-abastecimiento"])


def _log(db, user_email, accion, entidad, entidad_id=None, entidad_ref=None, detalle=None):
    from monza_models import MonzaLog
    db.add(MonzaLog(user_email=user_email, accion=accion, entidad=entidad,
                    entidad_id=entidad_id, entidad_ref=entidad_ref, detalle=detalle))
    db.commit()


def _gen_numero_ocp(db: Session) -> str:
    anio = datetime.utcnow().year
    last = (
        db.query(MonzaOcProveedor)
        .filter(MonzaOcProveedor.numero.like(f"OCP-{anio}-%"))
        .order_by(MonzaOcProveedor.id.desc())
        .first()
    )
    n = int(last.numero.split("-")[-1]) + 1 if last and last.numero else 1
    return f"OCP-{anio}-{n:04d}"


def _item_dict(it: MonzaCotizacionItem, cot: MonzaCotizacion) -> dict:
    return {
        "id": it.id,
        "cotizacion_id": cot.id,
        "cot_numero": cot.numero,
        "cliente": cot.cliente.nombre if cot.cliente else None,
        "vehiculo": cot.vehiculo,
        "descripcion": it.descripcion,
        "numero_parte": it.numero_parte,
        "marca": it.marca,
        "procedencia": it.procedencia,
        "calidad": it.calidad,
        "cantidad": it.cantidad,
        "costo": it.costo,
        "moneda": it.moneda,
        "precio_unitario_clp": it.precio_unitario_clp,
        "subtotal_clp": it.subtotal_clp,
        "plazo_entrega": it.plazo_entrega,
        "estado_linea": it.estado_linea or "cotizado",
        "oc_proveedor_id": it.oc_proveedor_id,
        "fecha_venta": cot.fecha_venta.isoformat() if cot.fecha_venta else None,
        # Adelanto (lo verifica Contabilidad): Abastecimiento ve si el pago está confirmado.
        "pct_adelanto": int(getattr(cot, "pct_adelanto", 0) or 0),
        "requiere_adelanto": int(getattr(cot, "pct_adelanto", 0) or 0) > 0,
        "pago_verificado": bool(getattr(cot, "adelanto_verificado", 0)),
    }


# KPIs
@router.get("/kpis")
def kpis(db: Session = Depends(get_db), _=Depends(get_current_user)):
    def count_estado(estado):
        return db.query(func.count(MonzaCotizacionItem.id)).filter(
            MonzaCotizacionItem.estado_linea == estado
        ).scalar() or 0

    return {
        "por_comprar": count_estado("por_comprar"),
        "comprado": count_estado("comprado"),
        # Hallazgo #9: el KPI "En transito" daba SIEMPRE 0 porque el pipeline nunca
        # escribe estado_linea='en_transito' (0 filas con ese valor en toda la BD).
        # La mercaderia volando vive en 'preparado' y 'embarcado' — misma semantica
        # que _STATE_BUCKETS de monza_router_despachos.py, que ya mapea esos dos
        # estados al bucket "en_transito". Con esto el jefe deja de ver 0 con
        # mercaderia en el aire.
        "en_transito": count_estado("preparado") + count_estado("embarcado"),
        "en_bodega": count_estado("en_bodega"),
        "despachado": count_estado("despachado"),
        "reclamo": count_estado("reclamo"),
        "ocs_abiertas": db.query(func.count(MonzaOcProveedor.id)).filter(
            MonzaOcProveedor.estado.in_(["emitida", "en_transito"])
        ).scalar() or 0,
    }


# Panel Compras: items por comprar
@router.get("/por-comprar")
def por_comprar(
    q: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    query = (
        db.query(MonzaCotizacionItem)
        .join(MonzaCotizacion, MonzaCotizacionItem.cotizacion_id == MonzaCotizacion.id)
        .options(joinedload(MonzaCotizacionItem.cotizacion).joinedload(MonzaCotizacion.cliente))
        .filter(MonzaCotizacionItem.estado_linea == "por_comprar")
    )
    if q:
        query = query.filter(
            (MonzaCotizacionItem.descripcion.ilike(f"%{q}%")) |
            (MonzaCotizacionItem.numero_parte.ilike(f"%{q}%")) |
            (MonzaCotizacion.numero.ilike(f"%{q}%"))
        )
    items = query.order_by(MonzaCotizacion.fecha_venta.desc(), MonzaCotizacionItem.id).all()
    return [_item_dict(it, it.cotizacion) for it in items]


# Seguimiento: items comprados / en transito / en bodega
@router.get("/seguimiento")
def seguimiento(
    estado: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    estados = [estado] if estado else ["comprado", "preparado", "embarcado", "en_bodega"]
    query = (
        db.query(MonzaCotizacionItem)
        .join(MonzaCotizacion, MonzaCotizacionItem.cotizacion_id == MonzaCotizacion.id)
        .options(joinedload(MonzaCotizacionItem.cotizacion).joinedload(MonzaCotizacion.cliente))
        .filter(MonzaCotizacionItem.estado_linea.in_(estados))
    )
    if q:
        query = query.filter(
            (MonzaCotizacionItem.descripcion.ilike(f"%{q}%")) |
            (MonzaCotizacion.numero.ilike(f"%{q}%"))
        )
    items = query.order_by(MonzaCotizacionItem.id.desc()).all()

    ocp_cache = {}
    out = []
    for it in items:
        d = _item_dict(it, it.cotizacion)
        if it.oc_proveedor_id:
            if it.oc_proveedor_id not in ocp_cache:
                ocp_cache[it.oc_proveedor_id] = db.query(MonzaOcProveedor).filter(
                    MonzaOcProveedor.id == it.oc_proveedor_id
                ).first()
            ocp = ocp_cache[it.oc_proveedor_id]
            if ocp:
                d["ocp_numero"] = ocp.numero
                d["ocp_proveedor"] = ocp.proveedor_nombre
                d["ocp_estado"] = ocp.estado
                d["ocp_awb"] = ocp.awb
                d["ocp_tracking"] = ocp.tracking
                d["ocp_numero_oc"] = ocp.numero_oc
                d["ocp_plazo_dias"] = ocp.plazo_dias
                # Origen de la OC: los nacionales muestran "Registrar entrega nacional"
                # en Seguimiento (saltan preparado/embarque). Coalescido: histórico sin
                # valor = internacional.
                d["tipo_origen"] = ocp.tipo_origen or "internacional"
        out.append(d)
    return out


class PrepararParcialItem(BaseModel):
    """Una línea con cantidad opcional — el contrato compartido por los 4 splits
    (comprar parcial, preparar-parcial, embarque y devolver-a-compras).

    Definida ACÁ (antes de ComprarBody, que la referencia) y no junto al bloque
    de preparar-parcial donde nació: FastAPI analiza el body de /comprar al
    decorar el endpoint, así que la clase debe existir antes.

    int (la columna `cantidad` es Integer: un float se redondearía en silencio en
    MySQL) y None como SENTINELA EXPLÍCITO de "toda la línea". OJO con el vicio de
    GA (compras.py:1031 usa `if item_data.cantidad`): con ese `if`, cantidad=0 es
    falsy y cae a "toda la cantidad" — preparar 0 preparaba 10. Acá 0 se RECHAZA."""
    item_id: int
    cantidad: Optional[int] = None


# Crear OC de proveedor (comprar items)
class ComprarBody(BaseModel):
    item_ids: List[int]
    proveedor_id: Optional[int] = None
    proveedor_nombre: Optional[str] = None
    pais: Optional[str] = None
    moneda: Optional[str] = "EUR"
    plazo_dias: Optional[int] = None
    numero_oc: Optional[str] = None
    awb: Optional[str] = None
    tracking: Optional[str] = None
    notas: Optional[str] = None
    # Origen de la compra: 'internacional' (embarque) o 'nacional' (camión + guía).
    # Gobierna el camino físico y la UI; el default deja el flujo internacional intacto.
    tipo_origen: Optional[str] = "internacional"
    # Asignación PARCIAL a la OC (espejo del split de asignación de Grupo AM,
    # commit 1d2a069): extensión ADITIVA — si el campo no viene, el body viejo
    # sigue byte-igual y corre el camino legado de abajo. Si viene, cada entrada
    # referencia un id de `item_ids` con su cantidad a comprar (None = línea
    # entera, mismo contrato que preparar-parcial: 0 se RECHAZA, exceso se
    # RECHAZA, jamás clamp silencioso). El remanente de una línea partida queda
    # 'por_comprar' y SIN OC — vuelve al panel, asignable a otro proveedor.
    cantidades: Optional[List[PrepararParcialItem]] = None


@router.post("/comprar")
def comprar(body: ComprarBody, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    # Vía PARCIAL (cantidades presentes): split + OC en una sola transacción con
    # locks y retry. `cantidades is None` = camino legado intacto (dos caminos:
    # el front solo manda el campo cuando el operador redujo alguna cantidad).
    if body.cantidades is not None:
        for _ in range(3):
            try:
                return _comprar_parcial_tx(db, body, current_user)
            except OperationalError as e:
                db.rollback()
                code = getattr(getattr(e, "orig", None), "args", [None])[0]
                if code not in (1213, 1205):
                    raise
        raise HTTPException(status_code=409,
                            detail="Conflicto de concurrencia al comprar: reintenta")

    if not body.item_ids:
        raise HTTPException(status_code=400, detail="Sin items")

    # Solo ítems realmente disponibles para comprar (estado 'por_comprar'); eager-load de
    # la cotización para el cortafuego (evita N+1 y permite detectar ítems sin venta).
    items = (
        db.query(MonzaCotizacionItem)
        .options(joinedload(MonzaCotizacionItem.cotizacion))
        .filter(
            MonzaCotizacionItem.id.in_(body.item_ids),
            MonzaCotizacionItem.estado_linea == "por_comprar",
        )
        .all()
    )
    # Cualquier id que no exista o no esté 'por_comprar' (ya comprado, etc.) → 400, en vez
    # de crear la OC con menos ítems silenciosamente.
    if len(items) != len(set(body.item_ids)):
        raise HTTPException(status_code=400, detail="Algunos ítems no están disponibles para comprar (no existen o ya fueron comprados)")

    # ── Cortafuego de ADELANTO ──────────────────────────────────────────────────
    # Si una venta exige adelanto (pct_adelanto > 0) NO se puede generar la OC de
    # proveedor hasta que Contabilidad verifique el pago (adelanto_verificado == 1).
    # Protege a Abastecimiento de comprar contra ventas cuyo 50% aún no se cobró.
    sin_verificar = [
        it.cotizacion for it in items
        if it.cotizacion is not None
        and int(it.cotizacion.pct_adelanto or 0) > 0
        and not int(it.cotizacion.adelanto_verificado or 0)
    ]
    if sin_verificar:
        # dedup por id manteniendo el orden
        vistos, unicas = set(), []
        for c in sin_verificar:
            if c.id not in vistos:
                vistos.add(c.id); unicas.append(c)
        nums = ", ".join(f"{c.numero} (adelanto {int(c.pct_adelanto or 0)}%)" for c in unicas)
        raise HTTPException(
            status_code=409,
            detail=f"Adelanto no verificado por Contabilidad en: {nums}. "
                   f"No se puede generar la OC de proveedor hasta confirmar el pago del adelanto.",
        )

    # Saneo del origen (espejo GA compras.py): la columna es NOT NULL y es la fuente
    # ÚNICA del camino físico — None/ausente cae al default 'internacional' para no
    # romper clientes antiguos del API, pero un valor explícito fuera del vocabulario
    # se RECHAZA en vez de corregirse en silencio (backend autoridad: 'nacionel' no
    # debe convertirse calladamente en una OC de embarque).
    tipo_origen = body.tipo_origen or "internacional"
    if tipo_origen not in ("nacional", "internacional"):
        raise HTTPException(
            status_code=400,
            detail="tipo_origen inválido: use 'nacional' o 'internacional'",
        )

    nombre = body.proveedor_nombre
    pais = body.pais
    moneda = body.moneda or "EUR"
    if body.proveedor_id:
        prov = db.query(MonzaProvAbast).filter(MonzaProvAbast.id == body.proveedor_id).first()
        if prov:
            nombre = prov.nombre
            pais = pais or prov.pais
            moneda = prov.moneda or moneda

    ocp = MonzaOcProveedor(
        numero=_gen_numero_ocp(db),
        proveedor_id=body.proveedor_id,
        proveedor_nombre=nombre,
        pais=pais,
        moneda=moneda,
        tipo_origen=tipo_origen,
        estado="emitida",
        plazo_dias=body.plazo_dias,
        numero_oc=body.numero_oc,
        awb=body.awb,
        tracking=body.tracking,
        notas=body.notas,
        asesor_email=current_user.email,
    )
    db.add(ocp)
    db.flush()

    for it in items:
        it.estado_linea = "comprado"
        it.oc_proveedor_id = ocp.id
    db.commit()
    db.refresh(ocp)

    _log(db, current_user.email, "CREATE", "oc_proveedor", ocp.id, ocp.numero,
         f"OC {ocp.numero} a {nombre or 'proveedor'} - {len(items)} item(s)")
    return {"ok": True, "ocp_id": ocp.id, "numero": ocp.numero, "items": len(items)}


# Items comprados (OC emitida, esperando preparar)
@router.get("/comprados")
def comprados(q: Optional[str] = Query(None), db: Session = Depends(get_db), _=Depends(get_current_user)):
    query = (
        db.query(MonzaCotizacionItem)
        .join(MonzaCotizacion, MonzaCotizacionItem.cotizacion_id == MonzaCotizacion.id)
        .options(joinedload(MonzaCotizacionItem.cotizacion).joinedload(MonzaCotizacion.cliente))
        .filter(MonzaCotizacionItem.estado_linea == "comprado")
    )
    if q:
        query = query.filter((MonzaCotizacionItem.descripcion.ilike(f"%{q}%")) | (MonzaCotizacion.numero.ilike(f"%{q}%")))
    items = query.order_by(MonzaCotizacionItem.id).all()
    cache = {}
    out = []
    for it in items:
        d = _item_dict(it, it.cotizacion)
        if it.oc_proveedor_id:
            if it.oc_proveedor_id not in cache:
                cache[it.oc_proveedor_id] = db.query(MonzaOcProveedor).filter(MonzaOcProveedor.id == it.oc_proveedor_id).first()
            ocp = cache[it.oc_proveedor_id]
            if ocp:
                d["ocp_numero"] = ocp.numero_oc or ocp.numero
                d["ocp_proveedor"] = ocp.proveedor_nombre
                d["ocp_plazo_dias"] = ocp.plazo_dias
                # Aquí decide el front el CTA: 'nacional' → "Registrar entrega
                # nacional" en vez de "Preparar" (espejo GA compras.py /comprados).
                d["tipo_origen"] = ocp.tipo_origen or "internacional"
        out.append(d)
    return out


def _rechazar_items_nacionales(db: Session, item_ids: list) -> None:
    """Los ítems asignados a una OC-Proveedor NACIONAL no pasan por
    preparado/embarque: su camino físico es 'Registrar entrega nacional' en
    Seguimiento. El backend es la autoridad — la UI oculta los botones, pero una
    selección mixta (o una llamada directa al API) no debe poder colarlos al
    pipeline de embarque (hallazgo del dueño probando en vivo en GA).

    Adaptación Monza: sin tabla OcProveedorItem — el vínculo ítem↔OC es directo
    vía MonzaCotizacionItem.oc_proveedor_id, así que el JOIN es de una sola arista.
    Además esta disjunción es la que hace correcto el UNION del tope físico en
    despachos (fuente embarque + fuente nacional nunca suman el MISMO ítem)."""
    if not item_ids:
        return
    nacionales = (
        db.query(MonzaCotizacionItem.numero_parte)
        .join(MonzaOcProveedor,
              MonzaOcProveedor.id == MonzaCotizacionItem.oc_proveedor_id)
        .filter(MonzaCotizacionItem.id.in_(item_ids),
                MonzaOcProveedor.tipo_origen == "nacional")
        .all()
    )
    if nacionales:
        partes = ", ".join(sorted({p or "?" for (p,) in nacionales}))
        raise HTTPException(
            status_code=400,
            detail=f"Ítem(s) de compra NACIONAL no pasan por embarque: {partes}. "
                   "Regístrelos con 'Registrar entrega nacional' en Seguimiento.",
        )


# Preparar items: comprado -> preparado (handoff a Logistica)
class PrepararBody(BaseModel):
    item_ids: List[int]


@router.post("/preparar")
def preparar(body: PrepararBody, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    # Guard anti-embarque: primera de las 2 entradas al pipeline físico Monza
    # (la otra es crear_embarque en Logística). Va ANTES de tocar estado_linea.
    _rechazar_items_nacionales(db, list(body.item_ids or []))
    items = db.query(MonzaCotizacionItem).filter(MonzaCotizacionItem.id.in_(body.item_ids), MonzaCotizacionItem.estado_linea == "comprado").all()
    for it in items:
        it.estado_linea = "preparado"
    db.commit()
    _log(db, current_user.email, "UPDATE", "item", None, None, f"{len(items)} ítem(s) preparado(s) para Logística")
    return {"ok": True, "preparados": len(items)}


# ── Preparación PARCIAL (Fase 9b: envíos parciales) ───────────────────────────
#
# EL PROBLEMA REAL (el dueño confirmó que le pasa seguido): el proveedor manda 6 de
# 10 filtros. Hasta ahora el pipeline movía la LÍNEA COMPLETA entre estados, así que
# había que embarcar los 10 — y cuando Bodega recibía 6, el cierre de recepción
# creaba un RECLAMO al proveedor por 4 que el proveedor simplemente no había
# despachado todavía. Reclamo FANTASMA, y el remanente sin forma de esperar el
# próximo AWB.
#
# La cura es PARTIR la línea: 6 pasan a 'preparado' (siguen al embarque) y nace una
# línea hermana con las 4 restantes en 'comprado', esperando su turno. Bodega recibe
# 6 sobre una línea de 6 = 'completo' sin faltante, sin reclamo.
#
# CERO MIGRACIÓN: las cantidades viajan en el body y el split ocurre en la MISMA
# transacción. Monza no tiene etapa de staging (no existe MonzaPreEmbarque), así que
# no necesita la columna `cantidad_despacho` que Grupo AM sí tiene en PreEmbarqueItem
# — tras el split la cantidad embarcada vive en MonzaCotizacionItem.cantidad, igual
# que en el EmbarqueItem de GA (que tampoco tiene columna de cantidad).

# PrepararParcialItem (el contrato por línea de este split) vive más ARRIBA,
# antes de ComprarBody: la asignación parcial de /comprar usa el mismo molde y
# FastAPI necesita la clase definida al decorar ese endpoint.

class PrepararParcialBody(BaseModel):
    items: List[PrepararParcialItem]


class DevolverACompras(BaseModel):
    """Devolver al panel de compras lo que el proveedor dejó en BACK ORDER.

    Mismo contrato que preparar-parcial (`cantidad` None = toda la línea, 0 se rechaza),
    porque es la MISMA operación de partir una línea vista al revés: acá `cantidad` es lo
    que VUELVE a comprarse, y el resto sigue su curso con la OC original.

    `motivo` es OBLIGATORIO y no es burocracia: esta es la única transición que va HACIA
    ATRÁS en el pipeline y borra el vínculo con la OC del proveedor. Sin el motivo, quien
    revise la línea meses después no tiene forma de saber si fue un back order real, un
    error de digitación o una cancelación del cliente."""
    items: List[PrepararParcialItem]
    motivo: str = Field(..., min_length=3, max_length=300)


def _clonar_item_remanente(db: Session, it: MonzaCotizacionItem, remanente: int,
                           estado: str, copiar_oc: bool = True) -> MonzaCotizacionItem:
    """LA REGLA DE ORO DEL SPLIT — el único lugar donde vive, porque es lo único de
    esta fase que puede DUPLICAR PLATA. Lo usan las dos entradas al pipeline físico
    (preparar-parcial en Abastecimiento y crear_embarque en Logística); una sola
    copia impide que las dos deriven.

    MonzaCotizacionItem lleva la FOTO DE PRECIOS CONGELADA de la cotización, y los
    totales de la cabecera (cot.total_neto / iva_monto / total_bruto) NO se
    recalculan NUNCA más después de crear la cotización. Reglas, no negociables:

      1. `precio_unitario_clp` se COPIA IDÉNTICO. Jamás se recalcula ni se prorratea.
         Si se prorrateara, MonzaVentasContabPage deriva el precio unitario como
         subtotal/cantidad y el precio del repuesto cambiaría solo.
      2. `subtotal_clp` se RECALCULA en las DOS mitades (qty × precio y remanente ×
         precio). Es el único campo de plata que se toca. Dejar el subtotal completo
         en la mitad con cantidad reducida INFLA el precio unitario derivado (partir
         10 en 6+4 dejando el subtotal de 10 lo multiplica por 1,67) y monta ese
         número inflado en el "total_venta_clp" que publica Contabilidad.
      3. Los otros 6 campos de la foto son UNITARIOS y se copian SIN DIVIDIR:
         tc_aplicado, tarifa_aerea, markup_pct, costo, moneda, peso_kg (Embarques
         Pricing los lee como "peso_unit" / costo unitario).
      4. `oc_proveedor_id` se COPIA cuando `copiar_oc=True` (default, los callers
         históricos llaman posicional y quedan intactos): es el análogo funcional
         del clon de OcProveedorItem que hace GA. Sin él el clon pierde su OC y
         _rechazar_items_nacionales (JOIN por esa columna) deja de reconocerlo como
         nacional — un ítem nacional se colaría al pipeline de embarque.
         EXCEPCIÓN (espejo GA 1d2a069): en la ASIGNACIÓN PARCIAL a OC el remanente
         todavía no es de nadie → `copiar_oc=False` lo deja SIN vínculo, porque si
         lo heredara nacería "comprado a X" y el panel jamás lo ofrecería a otro
         proveedor. Es cinturón de segunda capa: el guard del endpoint ya rebota
         (409) una línea 'por_comprar' con vínculo sucio, pero un caller directo
         del split no pasa por ese guard.
      5. La CABECERA de la cotización NO SE TOCA. Ni total_neto, ni iva_monto, ni
         total_bruto: Σ(cantidad) y Σ(subtotal_clp) de las hermanas son iguales a los
         de la línea original, así que la cabecera sigue cuadrando por construcción.

    INVARIANTE: Σ cantidad == cantidad original, Σ subtotal_clp == subtotal original,
    cabecera sin cambio alguno."""
    clon = MonzaCotizacionItem(
        cotizacion_id=it.cotizacion_id,
        descripcion=it.descripcion,
        numero_parte=it.numero_parte,
        marca=it.marca,
        procedencia=it.procedencia,
        calidad=it.calidad,
        cantidad=int(remanente),
        # Foto de precios: UNITARIOS, copiados tal cual (regla 3)
        costo=it.costo,
        moneda=it.moneda,
        peso_kg=it.peso_kg,
        tc_aplicado=it.tc_aplicado,
        tarifa_aerea=it.tarifa_aerea,
        markup_pct=it.markup_pct,
        # Precio unitario IDÉNTICO (regla 1); subtotal RECALCULADO (regla 2).
        # El None se PRESERVA: una línea sin subtotal guardado (dato legado o carga
        # directa a la BD) no gana monto por partirse — sigue en None en las dos
        # mitades, así el invariante Σ subtotal == subtotal original se cumple
        # también ahí (None == None) en vez de inventar cantidad × precio.
        precio_unitario_clp=it.precio_unitario_clp,
        subtotal_clp=(None if it.subtotal_clp is None
                      else int(remanente) * (it.precio_unitario_clp or 0)),
        plazo_entrega=it.plazo_entrega,
        estado_linea=estado,
        oc_proveedor_id=(it.oc_proveedor_id if copiar_oc else None),   # regla 4
    )
    db.add(clon)
    db.flush()
    return clon


def _validar_pedidos_parciales(pedidos: list, verbo: str = "preparar") -> list:
    """Saneo del body común a las 2 entradas (preparar-parcial y embarque). Devuelve
    la lista de ids. Cada rechazo es un 400 EXPLÍCITO: ninguno de los 4 vicios de GA
    (clamp silencioso, qty=0 → "todo", id inexistente ignorado, id repetido) llega a
    tocar la base."""
    if not pedidos:
        raise HTTPException(status_code=400, detail="Sin ítems")
    ids = [p.item_id for p in pedidos]
    # Dos líneas del mismo ítem en un mismo pedido burlarían la validación de
    # cantidad (cada una se compararía sola contra el total de la línea).
    if len(set(ids)) != len(ids):
        raise HTTPException(
            status_code=400,
            detail="Hay ítems repetidos en el pedido: consolida la cantidad en una sola línea",
        )
    for p in pedidos:
        if p.cantidad is not None and p.cantidad <= 0:
            raise HTTPException(
                status_code=400,
                # `verbo` nombra el flujo real (revisión del espejo, H4): este helper
                # lo comparten preparar-parcial, devolver-a-compras y la COMPRA
                # parcial — decirle «preparar» a quien está asignando confunde.
                detail=f"Cantidad inválida para el ítem {p.item_id}: debe ser mayor que 0 "
                       f"(omite el campo para {verbo} la línea completa)",
            )
    return ids


def _lockear_items_para_split(db: Session, ids: list, estado_esperado: str) -> dict:
    """Relectura BAJO LOCK de las líneas a partir, en orden id ASC (orden canónico de
    la casa: el mismo ancla que despachos, cierre de despacho y costeo de compras →
    sin deadlock estructural).

    EL LOCK ES OBLIGATORIO, no decorativo: sin él dos preparaciones parciales
    simultáneas del mismo ítem leen `cantidad=10` cada una, cada una clona su
    remanente y SE INVENTAN UNIDADES (Grupo AM no tiene este lock; es uno de sus
    defectos). populate_existing(): sin él el identity map devolvería el snapshot
    viejo aunque el lock haya esperado.

    Guard de ESTADO incluido: un ítem fuera de `estado_esperado` es un 400 con
    mensaje claro y NADIE se mueve — nunca un `continue` silencioso (vicio de GA:
    `if not item: continue` deja pasar ids inexistentes sin avisar)."""
    items = (
        db.query(MonzaCotizacionItem)
        .filter(MonzaCotizacionItem.id.in_(ids))
        .order_by(MonzaCotizacionItem.id.asc())
        .populate_existing()
        .with_for_update()
        .all()
    )
    if len(items) != len(set(ids)):
        faltan = sorted(set(ids) - {i.id for i in items})
        raise HTTPException(
            status_code=400,
            detail=f"Ítem(s) inexistente(s): {', '.join(str(i) for i in faltan)}",
        )
    malos = [i for i in items if (i.estado_linea or "") != estado_esperado]
    if malos:
        detalle = ", ".join(
            f"{i.numero_parte or i.descripcion} (estado: {i.estado_linea or 'sin estado'})"
            for i in malos
        )
        raise HTTPException(
            status_code=400,
            detail=f"Ítem(s) que no están en estado '{estado_esperado}': {detalle}",
        )
    return {i.id: i for i in items}


def _guard_duro_del_split(db: Session, pedidos: list, items_by_id: dict, ids: list,
                          estado_esperado: str) -> dict:
    """Llama al guard DURO (409) SOLO sobre las líneas que REALMENTE se van a partir,
    y devuelve el dict de ítems relockeado.

    DESVÍO DELIBERADO de la ubicación literal de la spec (que lo pone junto a
    _rechazar_items_nacionales, antes del lock y sobre TODOS los ids), con motivo
    concreto: para saber si una línea se parte hace falta su `cantidad`, y esa cifra
    solo es confiable BAJO LOCK. Llamarlo sobre todos los ids rechazaría con 409
    movimientos que NO parten nada y que hoy funcionan — flujo real: Logística saca
    un ítem de un embarque con `quitar_item` (vuelve a 'preparado') después de que
    Embarques Pricing ya guardó su snapshot (monza_emb_pricing_item), y re-embarcarlo
    COMPLETO en el próximo AWB pasaría a dar 409 sin que haya partición alguna. El
    guard protege LA PARTICIÓN, así que se aplica exactamente a las particiones: la
    vía legada (item_ids, sin cantidades) queda intacta y las 7 comprobaciones cubren
    el 100% de los splits.

    El RELOCK no es decorativo: el guard hace `db.rollback()` cuando la tabla de un
    módulo satélite no existe (MySQL 1146, deploy sin init_db), y un rollback SUELTA
    los locks. Sin re-tomarlos, el split correría sobre datos que otra transacción
    pudo haber cambiado. Re-lockear también re-valida el estado, así que un lock
    perdido no permite partir dos veces."""
    a_partir = [
        p.item_id for p in pedidos
        if p.cantidad is not None
        and int(p.cantidad) < int(items_by_id[p.item_id].cantidad or 0)
    ]
    if not a_partir:
        return items_by_id
    # Import LOCAL: la dirección de dependencia de la casa es
    # abastecimiento/logística/bodega → despachos (despachos no importa a ninguno).
    from monza_router_despachos import _rechazar_split_sobre_documento
    _rechazar_split_sobre_documento(db, a_partir)
    return _lockear_items_para_split(db, ids, estado_esperado)


def _partir_linea(db: Session, it: MonzaCotizacionItem, qty: Optional[int],
                  estado_remanente: str,
                  copiar_oc: bool = True) -> Optional[MonzaCotizacionItem]:
    """Aplica la regla de oro a UNA línea: deja `qty` unidades en la línea original
    y devuelve la hermana con el remanente (o None si no hubo partición).

    `qty is None` = toda la línea (compatibilidad: no parte nada).
    `qty > cantidad` = 400 EXPLÍCITO, jamás el `min(qty, cantidad)` silencioso de GA:
    un clamp callado convierte un typo del operador en "prepara todo" sin avisar."""
    cant_total = int(it.cantidad or 0)
    etiqueta = it.numero_parte or it.descripcion
    if cant_total <= 0:
        raise HTTPException(
            status_code=400,
            detail=f"La línea '{etiqueta}' no tiene cantidad válida ({it.cantidad}): "
                   "no se puede partir",
        )
    if qty is not None and qty > cant_total:
        raise HTTPException(
            status_code=400,
            detail=f"Cantidad {qty} excede lo vendido de '{etiqueta}' ({cant_total})",
        )
    if qty is None or int(qty) == cant_total:
        return None   # toda la línea: nada que partir

    # Coherencia de la foto de precios ANTES de tocar plata. El subtotal se recalcula
    # como cantidad × precio_unitario, así que si la línea guardada no cumple esa
    # identidad el split MOVERÍA dinero (Σ subtotales ≠ subtotal original). Eso es
    # inaceptable en silencio: se rechaza y la línea se prepara completa. En datos
    # reales la identidad SIEMPRE se cumple (el front manda
    # subtotal_clp = precio_clp × cantidad y el backend la recalcula igual).
    precio = it.precio_unitario_clp
    sub_actual = it.subtotal_clp
    if (precio is None or precio <= 0) and (sub_actual or 0) > 0:
        raise HTTPException(
            status_code=400,
            detail=f"La línea '{etiqueta}' tiene subtotal sin precio unitario: "
                   "no se puede partir sin alterar el monto de la venta",
        )
    esperado = cant_total * (precio or 0)
    if sub_actual is not None and abs(float(sub_actual) - esperado) > 1.0:
        raise HTTPException(
            status_code=400,
            detail=f"La línea '{etiqueta}' tiene un subtotal ({sub_actual}) que no "
                   f"cuadra con cantidad × precio unitario ({esperado}): partirla "
                   "movería el monto de la venta. Prepárala completa.",
        )

    remanente = cant_total - int(qty)
    clon = _clonar_item_remanente(db, it, remanente, estado_remanente, copiar_oc)
    it.cantidad = int(qty)
    if sub_actual is not None:   # None se preserva (ver _clonar_item_remanente)
        it.subtotal_clp = int(qty) * (precio or 0)
    return clon


# ── Asignación PARCIAL a OC de proveedor (espejo GA commit 1d2a069) ───────────
#
# El encargo del dueño: de una línea 'por_comprar' de 3 unidades, comprar 1 o 2 a
# un proveedor y que el remanente quede en el panel, asignable a otro. Es el MISMO
# split de preparar-parcial con una diferencia deliberada: el remanente NO hereda
# `oc_proveedor_id` (`copiar_oc=False`) — acá todavía no es de nadie.
#
# Adaptaciones al modelo Monza (documentadas para el espejo inverso):
# · Sin tabla de vínculo (OcProveedorItem no existe): la FK vive en la línea, así
#   que "asignar" es escribir `oc_proveedor_id` + 'comprado' sobre el trozo, y el
#   clon nace sin ninguna de las dos cosas. No hay LOCK 2 sobre vínculos como en
#   GA: el vínculo ES una columna de la fila ya lockeada.
# · El guard de restaurar_version de GA NO aplica: `_registrar_reversion`
#   (monza_router_cotizaciones.py) marca la versión REVERTIDA y ajusta el LTV,
#   pero JAMÁS repone cantidades por id — y el re-cierre recorre `cot.items`
#   VIVOS (las hermanas del split incluidas) moviendo solo estado_linea, así que
#   Σ de las hermanas cuadra por construcción. Verificado empíricamente
#   2026-08-10 sobre _registrar_reversion/_registrar_cierre y el PATCH de cierre.
# · El guard del editor de GA NO tiene equivalente: los únicos update/delete de
#   ítems en Monza (monza_router_leads.py) operan sobre MonzaLeadItem
#   (pre-cotización, sin estado de pipeline); no existe endpoint que edite o
#   borre una MonzaCotizacionItem post-venta. Verificado 2026-08-10 (grep de
#   endpoints en cotizaciones/cotizador/ventas/leads).


def _lockear_items_para_comprar(db: Session, ids: list) -> dict:
    """Relectura BAJO LOCK de las líneas a asignar, orden id ASC (ancla canónica
    de la casa, la misma de _lockear_items_para_split). Variante para el punto de
    COMPRA porque el contrato de rechazo difiere del split genérico:

    · id inexistente → 404 EXPLÍCITO (espejo GA: es un error del cliente y se le
      dice cuál; jamás el `continue` mudo ni crear la OC con menos ítems).
    · 'comprado' → 409 (espejo GA: la reasignación parcial es decisión v1 del
      panel de diseño de GA y acá tampoco existe).
    · 'por_comprar' con `oc_proveedor_id` sucio → 409 fail-closed (espejo del
      LOCK 2 de GA sobre vínculos previos): dato inconsistente no debe poder
      duplicar plata ni colar el remanente a otra OC.
    · cualquier otro estado → 400 con el mismo texto que el split genérico.

    El lock sigue siendo OBLIGATORIO: sin él dos asignaciones parciales
    simultáneas leen cantidad=3 las dos, cada una clona su remanente y SE
    INVENTAN UNIDADES (reproducido en GA por la sonda al quitar el lock).
    populate_existing(): sin él el identity map devolvería el snapshot viejo
    aunque el lock haya esperado."""
    items = (
        db.query(MonzaCotizacionItem)
        .filter(MonzaCotizacionItem.id.in_(ids))
        .order_by(MonzaCotizacionItem.id.asc())
        .populate_existing()
        .with_for_update()
        .all()
    )
    if len(items) != len(set(ids)):
        faltan = sorted(set(ids) - {i.id for i in items})
        raise HTTPException(
            status_code=404,
            detail=f"Ítem(s) inexistente(s): {', '.join(str(i) for i in faltan)}")
    comprados = [i for i in items if (i.estado_linea or "") == "comprado"]
    if comprados:
        det = ", ".join(i.numero_parte or i.descripcion or str(i.id) for i in comprados)
        raise HTTPException(
            status_code=409,
            detail=f"Ítem(s) ya comprados ({det}): la reasignación parcial no existe "
                   "todavía — mueva la línea entera o use el panel.")
    otros = [i for i in items if (i.estado_linea or "") != "por_comprar"]
    if otros:
        detalle = ", ".join(
            f"{i.numero_parte or i.descripcion} (estado: {i.estado_linea or 'sin estado'})"
            for i in otros)
        raise HTTPException(
            status_code=400,
            detail=f"Ítem(s) que no están en estado 'por_comprar': {detalle}")
    con_vinculo = [i for i in items if i.oc_proveedor_id]
    if con_vinculo:
        det = ", ".join(str(i.id) for i in con_vinculo)
        raise HTTPException(
            status_code=409,
            detail=f"Ítem(s) {det} ya tienen vínculo con un proveedor: la reasignación "
                   "parcial no existe todavía — mueva la línea entera o use el panel.")
    return {i.id: i for i in items}


def _comprar_parcial_tx(db: Session, body: ComprarBody, current_user) -> dict:
    """Transacción de la compra con cantidades parciales. UN solo commit al final
    (log incluido); cualquier rechazo intermedio deja la base intacta."""
    if not body.item_ids:
        raise HTTPException(status_code=400, detail="Sin items")
    ids_sel = list(body.item_ids)
    if len(set(ids_sel)) != len(ids_sel):
        raise HTTPException(
            status_code=400,
            detail="Hay ítems repetidos en el pedido: consolida la cantidad en una sola línea")
    # `cantidades` referencia ids de la selección; una entrada fuera de ella es un
    # error del cliente y se le dice cuál (jamás se ignora en silencio).
    mapa_qty = {}
    for c in body.cantidades:
        if c.item_id in mapa_qty:
            raise HTTPException(
                status_code=400,
                detail="Hay ítems repetidos en el pedido: consolida la cantidad en una sola línea")
        mapa_qty[c.item_id] = c.cantidad
    fuera = sorted(set(mapa_qty) - set(ids_sel))
    if fuera:
        raise HTTPException(
            status_code=400,
            detail=f"cantidades trae ítem(s) fuera de la selección: "
                   f"{', '.join(str(i) for i in fuera)}")
    # Ítem sin entrada en `cantidades` = línea entera (mismo sentinela que None).
    pedidos = [PrepararParcialItem(item_id=i, cantidad=mapa_qty.get(i)) for i in ids_sel]
    ids = _validar_pedidos_parciales(pedidos, verbo="asignar")   # 0/negativo → 400 explícito

    # Saneo del origen: idéntico al camino legado (backend autoridad).
    tipo_origen = body.tipo_origen or "internacional"
    if tipo_origen not in ("nacional", "internacional"):
        raise HTTPException(
            status_code=400,
            detail="tipo_origen inválido: use 'nacional' o 'internacional'",
        )

    # LOCK + contrato de estados (404/409/400, ver _lockear_items_para_comprar).
    items_by_id = _lockear_items_para_comprar(db, ids)

    # ── Cortafuego de ADELANTO (idéntico al camino legado, pero BAJO LOCK) ─────
    sin_verificar = [
        it.cotizacion for it in items_by_id.values()
        if it.cotizacion is not None
        and int(it.cotizacion.pct_adelanto or 0) > 0
        and not int(it.cotizacion.adelanto_verificado or 0)
    ]
    if sin_verificar:
        vistos, unicas = set(), []
        for cot in sin_verificar:
            if cot.id not in vistos:
                vistos.add(cot.id); unicas.append(cot)
        nums = ", ".join(f"{cot.numero} (adelanto {int(cot.pct_adelanto or 0)}%)" for cot in unicas)
        raise HTTPException(
            status_code=409,
            detail=f"Adelanto no verificado por Contabilidad en: {nums}. "
                   f"No se puede generar la OC de proveedor hasta confirmar el pago del adelanto.",
        )

    # Guard DURO de documentos SOLO sobre lo que realmente se parte (mismo criterio
    # que _guard_duro_del_split; inline porque el RELOCK debe re-validar el contrato
    # de COMPRA — 409 para 'comprado' — y no el 400 genérico del split). El guard
    # puede hacer rollback si falta la tabla de un módulo satélite (MySQL 1146), y
    # ese rollback SUELTA los locks: por eso se re-toman después.
    a_partir = [
        p.item_id for p in pedidos
        if p.cantidad is not None
        and int(p.cantidad) < int(items_by_id[p.item_id].cantidad or 0)
    ]
    if a_partir:
        from monza_router_despachos import _rechazar_split_sobre_documento
        _rechazar_split_sobre_documento(db, a_partir)
        items_by_id = _lockear_items_para_comprar(db, ids)

    # Proveedor + OC: idéntico al camino legado.
    nombre = body.proveedor_nombre
    pais = body.pais
    moneda = body.moneda or "EUR"
    if body.proveedor_id:
        prov = db.query(MonzaProvAbast).filter(MonzaProvAbast.id == body.proveedor_id).first()
        if prov:
            nombre = prov.nombre
            pais = pais or prov.pais
            moneda = prov.moneda or moneda

    ocp = MonzaOcProveedor(
        numero=_gen_numero_ocp(db),
        proveedor_id=body.proveedor_id,
        proveedor_nombre=nombre,
        pais=pais,
        moneda=moneda,
        tipo_origen=tipo_origen,
        estado="emitida",
        plazo_dias=body.plazo_dias,
        numero_oc=body.numero_oc,
        awb=body.awb,
        tracking=body.tracking,
        notas=body.notas,
        asesor_email=current_user.email,
    )
    db.add(ocp)
    db.flush()

    partidos = []
    for p in pedidos:
        it = items_by_id[p.item_id]
        original = int(it.cantidad or 0)
        # El remanente queda 'por_comprar' y SIN OC (copiar_oc=False): vuelve al
        # panel, asignable a otro proveedor. El split corre ANTES de escribir el
        # vínculo en la línea original, y la regla de oro protege la plata.
        clon = _partir_linea(db, it, p.cantidad, "por_comprar", copiar_oc=False)
        it.estado_linea = "comprado"
        it.oc_proveedor_id = ocp.id
        if clon is not None:
            partidos.append({
                "item_id": it.id,
                "remanente_item_id": clon.id,
                "comprado": it.cantidad,
                "pendiente": clon.cantidad,
                "original": original,
            })

    # UN solo commit con el log adentro (patrón de la casa: nada de trabajo después
    # del commit dentro de una función reintentada). Bitácora con los números del
    # split: «asignadas 1 de 3, remanente 2».
    from monza_models import MonzaLog
    det = f"OC {ocp.numero} a {nombre or 'proveedor'} - {len(pedidos)} item(s)"
    if partidos:
        det += " · " + ", ".join(
            f"asignadas {p['comprado']} de {p['original']}, remanente {p['pendiente']} "
            f"(ítem {p['item_id']} → remanente {p['remanente_item_id']})"
            for p in partidos)
    db.add(MonzaLog(user_email=current_user.email, accion="CREATE",
                    entidad="oc_proveedor", entidad_id=ocp.id, entidad_ref=ocp.numero,
                    detalle=det))
    db.commit()
    # Notificación POST-commit del split (paridad con GA — revisión del espejo, H3):
    # la bitácora MonzaLog queda dentro de la transacción; el aviso a la campana va
    # después y en try aislado, porque un fallo del aviso jamás puede deshacer una
    # compra ya commiteada (mismo criterio que devolver-a-compras).
    if partidos:
        try:
            det_notif = "; ".join(
                f"asignadas {p['comprado']} de {p['original']}, quedan {p['pendiente']}"
                for p in partidos)
            crear_notif(db, f"Asignación parcial · OC {ocp.numero}", det_notif,
                        "info", "/monzaparts/abastecimiento", "oc_proveedor", ocp.id)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] notificacion asignacion parcial monza: {e}")
    return {"ok": True, "ocp_id": ocp.id, "numero": ocp.numero,
            "items": len(pedidos), "partidos": len(partidos), "remanentes": partidos}


@router.post("/items/preparar-parcial")
def preparar_items_parcial(body: PrepararParcialBody, db: Session = Depends(get_db),
                           current_user=Depends(get_current_user)):
    """comprado → preparado, con cantidad PARCIAL opcional por ítem.

    `cantidad` ausente/None = toda la línea (compatibilidad). El endpoint legado
    `/preparar` no se toca: el front elige la vía según si hay cantidades.

    Retry 1213/1205 (regla de la casa): toma locks sobre MonzaCotizacionItem, las
    mismas filas que lockean despachos y el costeo de compras."""
    for _ in range(3):
        try:
            return _preparar_parcial_tx(db, body, current_user)
        except OperationalError as e:
            db.rollback()
            code = getattr(getattr(e, "orig", None), "args", [None])[0]
            if code not in (1213, 1205):
                raise
    raise HTTPException(status_code=409,
                        detail="Conflicto de concurrencia al preparar: reintenta")


def _preparar_parcial_tx(db: Session, body: PrepararParcialBody, current_user):
    pedidos = list(body.items or [])
    ids = _validar_pedidos_parciales(pedidos)

    # Guard anti-embarque nacional PRIMERO, igual que en /preparar, para que su
    # mensaje siga ganando sobre los demás.
    _rechazar_items_nacionales(db, ids)

    items_by_id = _lockear_items_para_split(db, ids, "comprado")
    # Guard DURO: solo sobre las líneas que REALMENTE se van a partir (ver
    # _guard_duro_del_split).
    items_by_id = _guard_duro_del_split(db, pedidos, items_by_id, ids, "comprado")

    partidos = []
    for p in pedidos:
        it = items_by_id[p.item_id]
        # El remanente vuelve a 'comprado': sigue comprado al proveedor, solo espera
        # el próximo embarque.
        clon = _partir_linea(db, it, p.cantidad, "comprado")
        it.estado_linea = "preparado"
        if clon is not None:
            partidos.append({"item_id": it.id, "remanente_item_id": clon.id,
                             "preparado": it.cantidad, "pendiente": clon.cantidad})

    # UN solo commit con el log adentro (patrón GA de despachos): nada de trabajo
    # después del commit dentro de una función reintentada — un fallo posterior
    # re-entraría la tx sobre un estado ya commiteado y devolvería un error falso.
    from monza_models import MonzaLog
    det = f"{len(pedidos)} ítem(s) preparado(s) para Logística"
    if partidos:
        det += " · " + ", ".join(
            f"parcial {p['preparado']} (quedan {p['pendiente']})" for p in partidos)
    db.add(MonzaLog(user_email=current_user.email, accion="UPDATE", entidad="item",
                    entidad_id=None, entidad_ref=None, detalle=det))
    db.commit()
    return {"ok": True, "preparados": len(pedidos), "partidos": len(partidos),
            "remanentes": partidos}


def _guard_plata_devolucion(db: Session, ids: list, items_by_id: dict) -> None:
    """Bloquea (409) devolver un ítem que YA está costeado en una compra viva.

    El caso: se registró la factura del proveedor en Cuentas por Pagar y su costo se
    repartió por ítem (monza_cont_compra_item). Si esa línea vuelve a 'por_comprar' y se
    recompra, el costo queda colgado de una unidad que ya no existe en esa OC: la compra
    seguiría diciendo que pagamos por algo que volvimos a pedir. Primero se corrige la
    compra, después se devuelve.

    Mismo criterio y mismo tono que el guard de anular recepción
    (monza_recepcion_nacional/router.py): la plata manda sobre el estado logístico.

    Fail-open SOLO si el módulo de compras no está instalado (tabla inexistente): ahí no
    hay ninguna compra que proteger. Cualquier otro error propaga."""
    from sqlalchemy.exc import ProgrammingError
    try:
        from monza_compras_contab.models import MonzaContCompra, MonzaContCompraItem
        filas = (
            db.query(MonzaContCompraItem.item_cotizacion_id, MonzaContCompra.numero_documento)
            .join(MonzaContCompra, MonzaContCompra.id == MonzaContCompraItem.compra_id)
            .filter(
                MonzaContCompraItem.item_cotizacion_id.in_(ids),
                # 'anulada' no cuenta: esa compra ya no reclama nada.
                func.coalesce(MonzaContCompra.estado_pago, "") != "anulado",
            )
            .all()
        )
    except ProgrammingError as e:
        if getattr(getattr(e, "orig", None), "args", [None])[0] == 1146:
            db.rollback()
            return
        raise
    if filas:
        detalle = ", ".join(
            f"{(items_by_id[iid].numero_parte or items_by_id[iid].descripcion)}"
            + (f" (documento {doc})" if doc else "")
            for iid, doc in filas if iid in items_by_id
        )
        raise HTTPException(
            409,
            f"No se puede devolver a compras: ya hay una compra registrada con el costo "
            f"de {detalle}. Corrige o anula esa compra en Compras y Pagos antes de "
            "devolver el ítem al panel de compras.",
        )


def _guard_recepcion_devolucion(db: Session, ids: list, items_by_id: dict) -> None:
    """Bloquea (409) devolver un ítem comprometido en una entrega nacional EN CURSO.

    Un ítem en 'comprado' no debería tener recepción cerrada —cerrarla con unidades
    utilizables lo mueve a 'en_bodega', y de ahí el guard de estado ya no deja
    devolver—, pero una entrega ABIERTA sí tiene líneas apuntando a él: devolverlo
    dejaría esa recepción a medio registrar sobre una línea que volvió a estar sin
    comprar.

    Solo cuentan las ABIERTAS (hallazgo HIGH del multienjambre 2026-08-08). Mirar
    TODAS dejaba ATRAPADO el back order nacional legítimo: una entrega ya cerrada en la
    que el proveedor no mandó nada ('no_llego') deja la línea en 'comprado' con su fila
    de recepción, que es EXACTAMENTE el caso que hay que poder devolver a compras. El
    docstring prometía "abierta" y el código bloqueaba por cualquiera.

    Fail-open solo con la tabla ausente, igual que el guard de la plata."""
    from sqlalchemy.exc import ProgrammingError
    try:
        from monza_recepcion_nacional.models import (
            MonzaRecepcionNacional, MonzaRecepcionNacionalItem,
        )
        filas = (
            db.query(MonzaRecepcionNacionalItem.item_cotizacion_id)
            .join(MonzaRecepcionNacional,
                  MonzaRecepcionNacional.id == MonzaRecepcionNacionalItem.recepcion_id)
            .filter(
                MonzaRecepcionNacionalItem.item_cotizacion_id.in_(ids),
                func.coalesce(MonzaRecepcionNacional.estado, "abierta") != "cerrada",
            )
            .all()
        )
    except ProgrammingError as e:
        if getattr(getattr(e, "orig", None), "args", [None])[0] == 1146:
            db.rollback()
            return
        raise
    except ImportError:
        return
    con_recepcion = {r[0] for r in filas if r[0] in items_by_id}
    if con_recepcion:
        detalle = ", ".join(
            (items_by_id[i].numero_parte or items_by_id[i].descripcion) for i in sorted(con_recepcion))
        raise HTTPException(
            409,
            f"No se puede devolver a compras: hay una entrega nacional EN CURSO que "
            f"incluye {detalle}. Ciérrala o anúlala en Bodega y vuelve a intentarlo.",
        )


@router.post("/items/devolver-a-compras")
def devolver_items_a_compras(body: DevolverACompras, db: Session = Depends(get_db),
                             current_user=Depends(get_current_user)):
    """comprado → por_comprar: lo que el proveedor dejó en BACK ORDER vuelve al panel
    de compras, entero o por cantidad parcial.

    El caso real (Baukat, proveedor de Alemania): se emite la OC, la línea queda
    'comprado' y en seguimiento, y días después el proveedor avisa que parte de lo
    pedido está en back order. Hasta ahora el pipeline era de una sola vía y esa
    mercadería quedaba trabada en Seguimiento, esperando algo que no iba a llegar.

    Devolver DESLIGA la línea de su OC de proveedor (`oc_proveedor_id = None`): vuelve a
    estar sin comprar, que es la verdad. La traza de qué OC venía queda en el log — si se
    conservara el vínculo, la línea aparecería en el panel de compras colgando de una OC
    vieja y `_rechazar_items_nacionales` (que hace JOIN por esa columna) la seguiría
    tratando como nacional.

    En una devolución PARCIAL el resto de la línea sigue 'comprado' con su OC intacta:
    es la misma partición de preparar-parcial vista al revés, con la REGLA DE ORO del
    split (_clonar_item_remanente) protegiendo la plata de la venta.

    Lo que NO hace, a propósito: no cancela la OC del proveedor aunque se quede sin
    líneas vivas. Nada en el módulo cancela OCs hoy, y una OC es un documento enviado al
    proveedor — cerrarla es una decisión comercial, no una consecuencia automática. La
    respuesta informa cuántas líneas le quedan para que el operador decida.

    Retry 1213/1205 (regla de la casa): toma locks sobre MonzaCotizacionItem, las mismas
    filas que lockean despachos, preparar-parcial y el costeo de compras."""
    for _ in range(3):
        try:
            return _devolver_a_compras_tx(db, body, current_user)
        except OperationalError as e:
            db.rollback()
            code = getattr(getattr(e, "orig", None), "args", [None])[0]
            if code not in (1213, 1205):
                raise
    raise HTTPException(409, "Conflicto de concurrencia al devolver a compras: reintenta")


def _devolver_a_compras_tx(db: Session, body: DevolverACompras, current_user):
    pedidos = list(body.items or [])
    ids = _validar_pedidos_parciales(pedidos)
    motivo = (body.motivo or "").strip()
    if len(motivo) < 3:
        # min_length de pydantic no alcanza: '   ' lo pasa.
        raise HTTPException(400, "Escribe el motivo de la devolución (por ejemplo: back order del proveedor)")

    # Lock + guard de estado: SOLO desde 'comprado'. Una línea ya preparada o embarcada
    # salió del proveedor y no se "devuelve a comprar" — se gestiona por reclamo.
    items_by_id = _lockear_items_para_split(db, ids, "comprado")

    # Guards de PLATA y de mercadería recibida ANTES de mutar nada (mismo orden que
    # anular recepción: primero se comprueba que nada dependa de esto, después se toca).
    _guard_plata_devolucion(db, ids, items_by_id)
    _guard_recepcion_devolucion(db, ids, items_by_id)

    # Guard de DOCUMENTOS sobre TODAS las líneas, no solo las que se parten (hallazgo
    # HIGH del multienjambre 2026-08-08). `_guard_duro_del_split` solo lo aplica a lo que
    # se parte —una devolución TOTAL no parte nada—, así que por esa vía un ítem con
    # factura 33 o guía 52 VIVA volvía a 'por_comprar' sin que nadie lo frenara: el
    # documento congeló cantidad y precio de esa línea, y desligarla de su OC deja el
    # papel legal apuntando a mercadería que el sistema dice no haber comprado.
    # Devolver es tan destructivo como partir: merece el mismo candado.
    # incluir_recepciones=False: las recepciones las evalúa el guard de arriba, que SÍ
    # distingue entregas en curso de cerradas. El del split bloquea por cualquiera —
    # correcto para partir, pero acá dejaría atrapado el back order nacional legítimo.
    # Import LOCAL: dirección de dependencia de la casa (abastecimiento → despachos).
    from monza_router_despachos import _rechazar_split_sobre_documento
    _rechazar_split_sobre_documento(db, ids, incluir_recepciones=False)
    # El guard puede haber hecho rollback (tabla de un módulo satélite ausente), que
    # SUELTA los locks: se re-toman y se re-valida el estado antes de tocar nada.
    items_by_id = _lockear_items_para_split(db, ids, "comprado")

    # Guard DURO del split, sobre las líneas que además se parten.
    items_by_id = _guard_duro_del_split(db, pedidos, items_by_id, ids, "comprado")

    devueltos, partidos, ocs_tocadas = [], [], set()
    for p in pedidos:
        it = items_by_id[p.item_id]
        ocp_id_previa = it.oc_proveedor_id
        if ocp_id_previa:
            ocs_tocadas.add(ocp_id_previa)
        # El REMANENTE sigue 'comprado' con su OC: esas unidades no están en back order.
        clon = _partir_linea(db, it, p.cantidad, "comprado")
        it.estado_linea = "por_comprar"
        it.oc_proveedor_id = None      # vuelve a estar sin comprar (ver docstring)
        devueltos.append({"item_id": it.id, "cantidad": it.cantidad,
                          "oc_proveedor_id_previa": ocp_id_previa})
        if clon is not None:
            partidos.append({"item_id": it.id, "devuelto": it.cantidad,
                             "sigue_comprado_item_id": clon.id, "sigue_comprado": clon.cantidad})
    db.flush()

    # Cuántas líneas VIVAS le quedan a cada OC tocada (informativo: la decisión de
    # cancelarla es comercial, ver docstring).
    ocs_resumen = []
    for ocp_id in sorted(ocs_tocadas):
        vivas = db.query(func.count(MonzaCotizacionItem.id)).filter(
            MonzaCotizacionItem.oc_proveedor_id == ocp_id).scalar() or 0
        ocp = db.query(MonzaOcProveedor).filter(MonzaOcProveedor.id == ocp_id).first()
        ocs_resumen.append({"ocp_id": ocp_id, "numero": ocp.numero if ocp else None,
                            "items_vivos": int(vivas)})

    # UN solo commit con el log adentro (patrón de la casa): nada de trabajo después del
    # commit dentro de una función reintentada.
    from monza_models import MonzaLog
    etiquetas = ", ".join(
        (items_by_id[p.item_id].numero_parte or items_by_id[p.item_id].descripcion)
        for p in pedidos)
    ocs_txt = ", ".join(f"{o['numero'] or o['ocp_id']} (quedan {o['items_vivos']})"
                        for o in ocs_resumen)
    detalle = (f"{len(pedidos)} ítem(s) devuelto(s) a compras: {etiquetas}"
               + (f" · desde OC {ocs_txt}" if ocs_txt else "")
               + (f" · parcial en {len(partidos)}" if partidos else "")
               + f" · motivo: {motivo}")
    db.add(MonzaLog(user_email=current_user.email, accion="DEVUELTO_A_COMPRAS",
                    entidad="item", entidad_id=None, entidad_ref=None, detalle=detalle))
    db.commit()

    crear_notif(db, f"Ítems devueltos a compras · {len(pedidos)}",
                f"{etiquetas} — {motivo}", "warning",
                "/monzaparts/abastecimiento", "item", None)
    return {"ok": True, "devueltos": len(pedidos), "partidos": len(partidos),
            "detalle": devueltos, "remanentes": partidos, "ocs": ocs_resumen}


# Listar OCs de proveedor
@router.get("/ocs")
def list_ocs(
    estado: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    query = db.query(MonzaOcProveedor)
    if estado:
        query = query.filter(MonzaOcProveedor.estado == estado)
    ocs = query.order_by(MonzaOcProveedor.id.desc()).all()
    out = []
    for ocp in ocs:
        n_items = db.query(func.count(MonzaCotizacionItem.id)).filter(
            MonzaCotizacionItem.oc_proveedor_id == ocp.id
        ).scalar() or 0
        out.append({
            "id": ocp.id,
            "numero": ocp.numero,
            "proveedor_nombre": ocp.proveedor_nombre,
            "pais": ocp.pais,
            "moneda": ocp.moneda,
            # Coalescido: histórico sin valor = internacional (badge del front).
            "tipo_origen": ocp.tipo_origen or "internacional",
            "estado": ocp.estado,
            "plazo_dias": ocp.plazo_dias,
            "numero_oc": ocp.numero_oc,
            "awb": ocp.awb,
            "tracking": ocp.tracking,
            "notas": ocp.notas,
            "asesor_email": ocp.asesor_email,
            "items_count": n_items,
            "created_at": ocp.created_at.isoformat() if ocp.created_at else None,
        })
    return out


class OcUpdateBody(BaseModel):
    estado: Optional[str] = None
    numero_oc: Optional[str] = None
    awb: Optional[str] = None
    tracking: Optional[str] = None
    plazo_dias: Optional[int] = None
    notas: Optional[str] = None


@router.patch("/ocs/{ocp_id}")
def update_oc(ocp_id: int, body: OcUpdateBody, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    ocp = db.query(MonzaOcProveedor).filter(MonzaOcProveedor.id == ocp_id).first()
    if not ocp:
        raise HTTPException(status_code=404, detail="OC no encontrada")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(ocp, field, value)
    ocp.updated_at = datetime.utcnow()

    # El estado de la OC es informativo; el pipeline de items lo manejan
    # Abastecimiento (preparar), Logistica (embarque) y Bodega (recepcion).
    db.commit()
    db.refresh(ocp)
    _log(db, current_user.email, "UPDATE", "oc_proveedor", ocp.id, ocp.numero,
         f"OC {ocp.numero} -> {ocp.estado}")
    if body.estado == "llegada":
        crear_notif(db, f"Mercadería en recepción · {ocp.numero}", f"{ocp.proveedor_nombre or 'Proveedor'} — lista para recibir en bodega", "info", "/monzaparts/bodega", "oc_proveedor", ocp.id)
    return {"ok": True}


# Items de una OC (detalle completo para Logistica)
@router.get("/ocs/{ocp_id}/items")
def oc_items(ocp_id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    items = (
        db.query(MonzaCotizacionItem)
        .join(MonzaCotizacion, MonzaCotizacionItem.cotizacion_id == MonzaCotizacion.id)
        .options(joinedload(MonzaCotizacionItem.cotizacion).joinedload(MonzaCotizacion.cliente))
        .filter(MonzaCotizacionItem.oc_proveedor_id == ocp_id)
        .all()
    )
    return [_item_dict(it, it.cotizacion) for it in items]


# Proveedores de abastecimiento CRUD
class ProveedorBody(BaseModel):
    nombre: str
    pais: Optional[str] = None
    moneda: Optional[str] = "EUR"
    contacto: Optional[str] = None
    email: Optional[str] = None
    telefono: Optional[str] = None
    notas: Optional[str] = None


@router.get("/proveedores")
def list_proveedores(db: Session = Depends(get_db), _=Depends(get_current_user)):
    provs = db.query(MonzaProvAbast).filter(MonzaProvAbast.activo == 1).order_by(MonzaProvAbast.nombre).all()
    return [
        {
            "id": p.id, "nombre": p.nombre, "pais": p.pais, "moneda": p.moneda,
            "contacto": p.contacto, "email": p.email, "telefono": p.telefono, "notas": p.notas,
        }
        for p in provs
    ]


@router.post("/proveedores")
def create_proveedor(body: ProveedorBody, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    p = MonzaProvAbast(**body.model_dump())
    db.add(p)
    db.commit()
    db.refresh(p)
    _log(db, current_user.email, "CREATE", "proveedor", p.id, p.nombre, f"Proveedor {p.nombre} creado")
    return {"ok": True, "id": p.id}


@router.patch("/proveedores/{prov_id}")
def update_proveedor(prov_id: int, body: ProveedorBody, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    p = db.query(MonzaProvAbast).filter(MonzaProvAbast.id == prov_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Proveedor no encontrado")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(p, field, value)
    db.commit()
    return {"ok": True}


@router.delete("/proveedores/{prov_id}")
def delete_proveedor(prov_id: int, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    p = db.query(MonzaProvAbast).filter(MonzaProvAbast.id == prov_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Proveedor no encontrado")
    p.activo = 0
    db.commit()
    return {"ok": True}
