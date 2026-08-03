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
from pydantic import BaseModel
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


@router.post("/comprar")
def comprar(body: ComprarBody, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
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

class PrepararParcialItem(BaseModel):
    item_id: int
    # int (la columna `cantidad` es Integer: un float se redondearía en silencio en
    # MySQL) y None como SENTINELA EXPLÍCITO de "toda la línea". OJO con el vicio de
    # GA (compras.py:1031 usa `if item_data.cantidad`): con ese `if`, cantidad=0 es
    # falsy y cae a "toda la cantidad" — preparar 0 preparaba 10. Acá 0 se RECHAZA.
    cantidad: Optional[int] = None


class PrepararParcialBody(BaseModel):
    items: List[PrepararParcialItem]


def _clonar_item_remanente(db: Session, it: MonzaCotizacionItem, remanente: int,
                           estado: str) -> MonzaCotizacionItem:
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
      4. `oc_proveedor_id` se COPIA: es el análogo funcional del clon de
         OcProveedorItem que hace GA. Sin él el clon pierde su OC y
         _rechazar_items_nacionales (JOIN por esa columna) deja de reconocerlo como
         nacional — un ítem nacional se colaría al pipeline de embarque.
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
        oc_proveedor_id=it.oc_proveedor_id,   # regla 4
    )
    db.add(clon)
    db.flush()
    return clon


def _validar_pedidos_parciales(pedidos: list) -> list:
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
                detail=f"Cantidad inválida para el ítem {p.item_id}: debe ser mayor que 0 "
                       "(omite el campo para preparar la línea completa)",
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
                  estado_remanente: str) -> Optional[MonzaCotizacionItem]:
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
    clon = _clonar_item_remanente(db, it, remanente, estado_remanente)
    it.cantidad = int(qty)
    if sub_actual is not None:   # None se preserva (ver _clonar_item_remanente)
        it.subtotal_clp = int(qty) * (precio or 0)
    return clon


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
