"""API del módulo Compras / Cuentas por Pagar (AP).

Prefijo: /compras-contab (se monta con prefix=/api → /api/compras-contab).
Registrar compras/gastos del día a día, imputarlas a una cuenta contable (NIIF),
llevar su condición/estado de pago, y pagarlas vía Comprobantes de Egreso
(cont_egreso): UNA salida real de dinero que puede pagar VARIAS compras y luego se
concilia con el banco. Scope por empresa (Grupo AM = 'mineria').
"""
from datetime import date, timedelta
from typing import Optional, List, Tuple

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, case, and_, or_
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.exc import IntegrityError, OperationalError

from database import get_db
from auth import get_current_user
from empresa_guard import require_empresa
from models.models import (
    User, Proveedor, Embarque, EmbarqueItem, OcProveedor, OcProveedorItem,
    ItemCotizacion,
)
from embarques_pricing.models import EmbarquePricing, EmbarquePricingGasto, EmbarquePricingItem

from .models import ContCompra, ContEgreso, ContEgresoDetalle, ContPlanCuenta, ContCompraItem
from .schemas import CompraCreate, CompraItemIn, PagoIn, EgresoCreate, EgresoUpdate, AnularIn
from .service import (
    IVA_RATE, TOL, TOL_PAGO, TIPOS_GASTO, TIPO_GASTO_LABEL,
    CATEGORIAS_SUGERIDAS, MEDIOS_PAGO, ESTADOS_PAGO, CUENTA_DEFAULT_CODIGO,
    _f, _recompute_compra, _serialize_compra, serialize_egreso,
    empresa_de, parse_date_estricta, cuenta_default_codigo,
)

# Módulo SOLO MachParts (Grupo AM = 'mineria'). El filtrado por empresa ya existe en cada
# query; este guard de router además deniega (403) el acceso a usuarios de otra empresa.
router = APIRouter(
    prefix="/compras-contab",
    tags=["compras-contab"],
    dependencies=[Depends(require_empresa("mineria"))],
)

PAGE_SIZE_DEFAULT = 50
PAGE_SIZE_MAX = 200


# ─── Helpers ────────────────────────────────────────────────────────────────────
def _fecha(s, campo: str):
    try:
        return parse_date_estricta(s, campo=campo)
    except ValueError as e:
        raise HTTPException(400, str(e))


def _periodo_rango(periodo: Optional[str]):
    if not periodo:
        return None
    hoy = date.today()
    if periodo == "semana":
        return hoy - timedelta(days=7)
    if periodo == "mes":
        return date(hoy.year, hoy.month, 1)
    if periodo == "anio":
        return date(hoy.year, 1, 1)
    return None


def _filtro_estado_dinamico(query, estado: str):
    """Filtro por estado calculado EN VIVO (el estado_pago persistido no transiciona a
    'vencido' con el paso del tiempo). Replica la semántica de service._estado_pago."""
    hoy = date.today()
    s, p, v = ContCompra.saldo_clp, ContCompra.monto_pagado_clp, ContCompra.fecha_vencimiento
    vencida = and_(v.isnot(None), v < hoy)
    if estado == "anulado":
        return query.filter(ContCompra.anulado.is_(True))
    query = query.filter(ContCompra.anulado.is_(False))
    if estado == "pagado":
        return query.filter(s <= TOL)
    if estado == "vencido":
        return query.filter(s > TOL, vencida)
    if estado == "parcial":
        return query.filter(s > TOL, p > TOL, or_(v.is_(None), v >= hoy))
    if estado == "pendiente":
        return query.filter(s > TOL, p <= TOL, or_(v.is_(None), v >= hoy))
    return query.filter(ContCompra.estado_pago == estado)


def _apply_filtros(query, *, tipo=None, estado_pago=None, categoria=None,
                   periodo=None, q=None, proveedor_id=None):
    if tipo:
        query = query.filter(ContCompra.tipo_gasto == tipo)
    if estado_pago:
        query = _filtro_estado_dinamico(query, estado_pago)
    if categoria:
        query = query.filter(ContCompra.categoria == categoria)
    if proveedor_id:
        query = query.filter(ContCompra.proveedor_id == int(proveedor_id))
    desde = _periodo_rango(periodo)
    if desde:
        query = query.filter(ContCompra.fecha >= desde)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            ContCompra.acreedor.ilike(like), ContCompra.numero_documento.ilike(like),
            ContCompra.referencia.ilike(like), ContCompra.descripcion.ilike(like),
            ContCompra.proveedor_rut.ilike(like), ContCompra.categoria.ilike(like),
        ))
    return query


def _get_compra_scoped(db: Session, compra_id: int, empresa: str, *, lock: bool = False) -> ContCompra:
    q = db.query(ContCompra).filter(ContCompra.id == compra_id, ContCompra.empresa == empresa)
    if lock:
        q = q.with_for_update()
    compra = q.first()
    if not compra:
        raise HTTPException(404, "Compra no encontrada")
    return compra


def _crear_egreso(
    db: Session, *, empresa: str, detalles: List[Tuple[int, float]], usuario_id,
    fecha=None, medio="transferencia", cuenta_origen_id=None, banco=None,
    numero_operacion=None, beneficiario=None, beneficiario_rut=None, glosa=None,
    moneda="CLP", tc=1.0, fecha_mov_bancario=None,
) -> ContEgreso:
    """Crea un Comprobante de Egreso que paga 1..N compras. Bloquea cada compra,
    valida que no exceda su saldo, recalcula y devuelve el egreso. (Una compra no
    puede repetirse en el mismo egreso.)"""
    if not detalles:
        raise HTTPException(400, "El egreso no tiene detalles")
    vistas = set()
    compras = {}
    total = 0.0
    # Locks en orden GLOBAL (por id de compra): dos egresos consolidados concurrentes
    # que comparten compras las bloquean en el mismo orden → sin deadlock.
    detalles = sorted(detalles, key=lambda d: d[0])
    for compra_id, monto in detalles:
        if compra_id in vistas:
            raise HTTPException(400, f"La compra {compra_id} aparece dos veces en el egreso")
        vistas.add(compra_id)
        monto = _f(monto)
        if monto <= 0:
            raise HTTPException(400, "Cada monto del egreso debe ser mayor a 0")
        compra = (db.query(ContCompra)
                  .filter(ContCompra.id == compra_id, ContCompra.empresa == empresa)
                  .with_for_update().first())
        if not compra:
            raise HTTPException(404, f"Compra {compra_id} no encontrada")
        if compra.anulado:
            raise HTTPException(400, f"La compra {compra_id} está anulada")
        # Lectura BLOQUEANTE de los pagos ya imputados (no la relación perezosa): bajo
        # REPEATABLE READ toda lectura PLANA sirve el snapshot que abrió la primera
        # sentencia del request (el SELECT de usuarios del guard de empresa), ANTERIOR al
        # with_for_update de arriba. El lock serializa, pero el tope leía datos viejos: dos
        # egresos simultáneos a la misma compra pasaban AMBOS y sobre-pagaban al proveedor.
        pagado_actual = sum(
            _f(d.monto_clp) for d in
            db.query(ContEgresoDetalle)
              .filter(ContEgresoDetalle.compra_id == compra.id)
              .populate_existing().with_for_update().all())
        saldo = round(_f(compra.monto_total_clp) - pagado_actual, 2)
        if monto > saldo + TOL_PAGO:
            raise HTTPException(400, f"El pago a la compra {compra_id} excede su saldo ({max(saldo, 0):.0f})")
        compras[compra_id] = compra
        total += monto
    total = round(total, 2)
    tc_v = _f(tc) or 1.0
    egreso = ContEgreso(
        empresa=empresa, fecha=fecha, medio=medio or "transferencia",
        cuenta_origen_id=cuenta_origen_id, banco=banco, numero_operacion=numero_operacion,
        beneficiario=beneficiario, beneficiario_rut=beneficiario_rut, glosa=glosa,
        moneda=(moneda or "CLP"), tc=tc_v,
        monto_origen=round(total / tc_v, 2) if tc_v else total, monto_total_clp=total,
        fecha_mov_bancario=fecha_mov_bancario, usuario_id=usuario_id,
    )
    db.add(egreso)
    db.flush()
    for compra_id, monto in detalles:
        m = _f(monto)
        db.add(ContEgresoDetalle(
            egreso_id=egreso.id, compra_id=compra_id, monto_clp=m,
            tc_aplicado=tc_v, monto_origen=round(m / tc_v, 2) if tc_v else m,
        ))
    db.flush()
    for compra in compras.values():
        db.refresh(compra)
        _recompute_compra(compra)
    return egreso


def _antiguedad(db: Session, *, empresa, tipo=None, estado_pago=None, categoria=None,
                periodo=None, q=None, proveedor_id=None) -> dict:
    hoy = date.today()
    d30, d60, d90 = hoy - timedelta(days=30), hoy - timedelta(days=60), hoy - timedelta(days=90)
    f = func.coalesce(ContCompra.fecha, hoy)
    s = ContCompra.saldo_clp
    qy = db.query(
        func.coalesce(func.sum(case((f >= d30, s), else_=0)), 0),
        func.coalesce(func.sum(case((and_(f < d30, f >= d60), s), else_=0)), 0),
        func.coalesce(func.sum(case((and_(f < d60, f >= d90), s), else_=0)), 0),
        func.coalesce(func.sum(case((f < d90, s), else_=0)), 0),
    ).filter(ContCompra.empresa == empresa, ContCompra.anulado.is_(False), ContCompra.saldo_clp > TOL)
    qy = _apply_filtros(qy, tipo=tipo, estado_pago=estado_pago, categoria=categoria,
                        periodo=periodo, q=q, proveedor_id=proveedor_id)
    r = qy.one()
    return {"0_30": round(_f(r[0]), 0), "31_60": round(_f(r[1]), 0),
            "61_90": round(_f(r[2]), 0), "91_mas": round(_f(r[3]), 0)}


# ─── Listado ─────────────────────────────────────────────────────────────────
@router.get("")
def listar_compras(
    tipo: Optional[str] = None, estado_pago: Optional[str] = None, categoria: Optional[str] = None,
    periodo: Optional[str] = None, q: Optional[str] = None, proveedor_id: Optional[int] = None,
    incluir_anulados: bool = False, page: int = 1, page_size: int = PAGE_SIZE_DEFAULT,
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user),
):
    empresa = empresa_de(current_user)
    page = max(1, int(page))
    page_size = min(max(1, int(page_size)), PAGE_SIZE_MAX)
    base = db.query(ContCompra).filter(ContCompra.empresa == empresa)
    if not incluir_anulados:
        base = base.filter(ContCompra.anulado.is_(False))
    base = _apply_filtros(base, tipo=tipo, estado_pago=estado_pago, categoria=categoria,
                          periodo=periodo, q=q, proveedor_id=proveedor_id)
    total = base.count()
    rows = (
        base.options(
            selectinload(ContCompra.cuenta),
            # Detalle de costeo por ítem (compra nacional): sin esta carga ansiosa era
            # 1 query por compra al serializar sus items (N+1).
            selectinload(ContCompra.items),
            # cadena completa hasta ContEgreso.detalles: _serialize_pago usa
            # len(e.detalles) y sin esta carga ansiosa era 1 query por egreso (N+1)
            selectinload(ContCompra.egreso_detalles)
            .selectinload(ContEgresoDetalle.egreso)
            .selectinload(ContEgreso.detalles),
        )
        .order_by(ContCompra.id.desc())
        .offset((page - 1) * page_size).limit(page_size).all()
    )
    return {
        "compras": [_serialize_compra(c) for c in rows],
        "total": int(total), "page": page, "page_size": page_size,
        "antiguedad": _antiguedad(db, empresa=empresa, tipo=tipo, estado_pago=estado_pago,
                                  categoria=categoria, periodo=periodo, q=q, proveedor_id=proveedor_id),
    }


# ─── Costeo por ítem de la compra NACIONAL (cont_compra_item) ───────────────────
# Tolerancia en UNIDADES (no en CLP): TOL_PAGO=1 peso serviría de holgura de dinero,
# pero como epsilon de cantidad dejaría costear 1 unidad de más que lo recibido. Las
# cantidades son Numeric(12,4); 1e-3 absorbe el ruido de float sin abrir un hueco real.
TOL_QTY = 0.001


def _recibido_nacional(db: Session, item_ids, con_lock: bool = False) -> dict:
    """{item_cotizacion_id: Σ qty_recibida utilizable} de recepciones nacionales CERRADAS.
    Tope de la cantidad que se puede costear por ítem. Batch (import local para no
    cablear recepcion_nacional al importar este módulo).

    con_lock=True (guards del costeo): lectura BLOQUEANTE fila a fila (current
    read) sumada en Python. Bajo REPEATABLE READ una lectura normal NO vería una
    anulación de recepción commiteada después de nacer el snapshot — un costeo
    concurrente a la anulación costeaba contra un recibido ya borrado (write-skew,
    revisión G13). El lock además serializa contra anular_recepcion."""
    if not item_ids:
        return {}
    from recepcion_nacional.models import (
        RecepcionNacional, RecepcionNacionalItem, RECEPCION_UTILIZABLE,
    )
    q = (db.query(RecepcionNacionalItem.item_cotizacion_id,
                  RecepcionNacionalItem.qty_recibida)
         .join(RecepcionNacional, RecepcionNacional.id == RecepcionNacionalItem.recepcion_id)
         .filter(RecepcionNacionalItem.item_cotizacion_id.in_(item_ids),
                 RecepcionNacional.estado == "cerrada",
                 RecepcionNacionalItem.estado_recepcion.in_(RECEPCION_UTILIZABLE)))
    if con_lock:
        q = q.with_for_update()
    out: dict = {}
    for iid, qty in q.all():
        if iid is not None:
            out[iid] = out.get(iid, 0.0) + _f(qty)
    return out


def _crear_items_costeo(db: Session, compra: ContCompra, payload: CompraCreate, empresa: str) -> None:
    """Crea las líneas cont_compra_item de una compra nacional, con los guards:
      A) doble costeo internacional (ítem con emb_pricing_item) → 409.
      B) doble costeo nacional (Σ cantidad en otras compras ACTIVAS + esta) ≤ recibido.
      C) Σ cantidad costeada por ítem ≤ recibido nacional utilizable → 409.
      D) Σ líneas costeadas (CLP) ≤ neto CLP de la factura (cobertura parcial OK) → 400.
      E) cada ítem pertenece a la OC-Proveedor referenciada → 400.
    El costo por ítem = NETO en CLP (el IVA es crédito fiscal, NO capitaliza).

    Serializa el costeo igual que despachos/pagos: bloquea las filas ItemCotizacion
    costeadas con SELECT ... FOR UPDATE y RELEE lo ya costeado con lock. Sin esto, dos
    costeos concurrentes del MISMO ítem leen ambos 'ya costeado = 0', pasan el tope
    'Σ ≤ recibido' y sobre-costean (capitalizan a Existencias más unidades que las
    recibidas)."""
    ocp = None
    asignados = None  # item_cotizacion_id asignados a la OC (validación de pertenencia)
    if payload.oc_proveedor_id:
        ocp = db.query(OcProveedor).filter(OcProveedor.id == payload.oc_proveedor_id).first()
        if not ocp:
            raise HTTPException(404, "OC-Proveedor no encontrada")
        if (ocp.tipo_origen or "internacional") != "nacional":
            raise HTTPException(400, "El detalle por ítem solo aplica a una OC nacional")
        compra.oc_proveedor_id = ocp.id
        # Guard E — pertenencia: los ítems costeados deben estar asignados a ESTA OC.
        # No se puede costear un ítem de OTRA OC-Proveedor bajo esta factura (mezclaría
        # trazabilidad y burlaría el tope, que es por ítem, no por OC). Espejo del
        # control de recepcion_nacional.registrar_entrega ("no pertenece a esta OC").
        asignados = {r[0] for r in db.query(OcProveedorItem.item_cotizacion_id)
                     .filter(OcProveedorItem.oc_proveedor_id == ocp.id).all()}

    tc = _f(compra.tc) or 1.0
    neto_clp = round(_f(compra.monto_neto) * tc, 2)
    ids = [ln.item_cotizacion_id for ln in payload.items]

    # ── Serialización del costeo (patrón despachos/pagos) ──
    # Lock de las filas ItemCotizacion costeadas, en orden por id (mismo orden que usa
    # despachos.py, que lockea estas mismas filas → sin deadlock). Dos costeos
    # concurrentes del MISMO ítem se serializan aquí: el segundo espera y recién
    # entonces relee `ya_nac` viendo la fila que el primero ya commiteó.
    # populate_existing(): sin él, el identity map devolvería el snapshot viejo aunque
    # el lock haya esperado.
    # Las filas leídas bajo el lock se guardan: el snapshot de parte/descripción que se
    # congela en cont_compra_item cae al valor REAL del ítem cuando el payload no lo trae
    # (el front puede omitirlo). Antes quedaba NULL y la línea de costo nacía sin
    # identidad auditable. Espejo de monza_compras_contab/router.py:400-401.
    items_db: dict = {}
    if ids:
        items_db = {it.id: it for it in (db.query(ItemCotizacion)
                    .filter(ItemCotizacion.id.in_(ids))
                    .order_by(ItemCotizacion.id.asc())
                    .populate_existing().with_for_update().all())}

    # Guard A — ítems ya con costo internacional (embarque)
    ya_intl = {r[0] for r in db.query(EmbarquePricingItem.item_cotizacion_id)
               .filter(EmbarquePricingItem.item_cotizacion_id.in_(ids)).all()}
    # Guard B/C — cantidad ya costeada en OTRAS compras nacionales ACTIVAS. LECTURA CON
    # LOCK (with_for_update): bajo REPEATABLE READ, sin el lock el snapshot NO vería las
    # filas commiteadas por un costeo concurrente que ya cruzó el gate del ItemCotizacion
    # → sobre-costeo. Se suma en Python (FOR UPDATE + agregado SQL no combinan; espejo de
    # despachos._qty_already_dispatched con con_lock=True).
    ya_nac: dict = {}
    for iid, cant in (db.query(ContCompraItem.item_cotizacion_id, ContCompraItem.cantidad)
                      .join(ContCompra, ContCompra.id == ContCompraItem.compra_id)
                      .filter(ContCompraItem.item_cotizacion_id.in_(ids),
                              ContCompra.empresa == empresa, ContCompra.anulado.is_(False))
                      .with_for_update().all()):
        ya_nac[iid] = ya_nac.get(iid, 0.0) + _f(cant)
    # Guard C — recibido nacional utilizable (tope de cantidad costeable)
    recibido = _recibido_nacional(db, ids, con_lock=True)

    vistos, suma = set(), 0.0
    for ln in payload.items:
        iid = ln.item_cotizacion_id
        if iid in vistos:
            raise HTTPException(400, f"El ítem {iid} aparece dos veces en el detalle")
        vistos.add(iid)
        it = items_db.get(iid)
        if asignados is not None and iid not in asignados:
            raise HTTPException(
                400,
                f"El ítem {ln.numero_parte or iid} no pertenece a la OC-Proveedor "
                f"{ocp.numero or ocp.id}")
        if iid in ya_intl:
            raise HTTPException(
                409,
                f"El ítem {iid} ya tiene costo internacional (embarque); no puede "
                "costearse como nacional")
        cu = round(_f(ln.precio_unit) * tc, 2)
        ct = round(_f(ln.cantidad) * cu, 2)
        suma += ct
        acumulada = ya_nac.get(iid, 0.0) + _f(ln.cantidad)
        recib = recibido.get(iid, 0.0)
        if acumulada > recib + TOL_QTY:
            raise HTTPException(
                409,
                f"Ítem {ln.numero_parte or iid}: cantidad costeada acumulada "
                f"({acumulada:g}) supera lo recibido en bodega ({recib:g}). "
                "Registre primero la recepción nacional.")
        db.add(ContCompraItem(
            compra_id=compra.id, item_cotizacion_id=iid,
            oc_proveedor_id=compra.oc_proveedor_id, oc_proveedor_item_id=ln.oc_proveedor_item_id,
            numero_parte=ln.numero_parte or (it.numero_parte if it else None),
            descripcion=ln.descripcion or (it.descripcion if it else None),
            cantidad=_f(ln.cantidad), precio_unit=_f(ln.precio_unit),
            costo_unit_clp=cu, costo_total_clp=ct))

    # Guard D — Σ líneas ≤ neto CLP (cobertura parcial permitida; nunca superar el neto)
    if round(suma, 2) > neto_clp + 1.0:
        raise HTTPException(
            400,
            f"La suma de líneas costeadas ({suma:.0f}) supera el neto de la factura "
            f"({neto_clp:.0f})")


# ─── Anti-duplicado por FACTURA FÍSICA del proveedor (no por línea del pricing) ─
# Holgura de 1 peso al comparar brutos: el CLP se redondea a entero en todo el sistema.
TOL_BRUTO_CLP = 1.0


def _norm_txt(s) -> str:
    """Normaliza texto para comparar identidades (RUT / nombre del acreedor)."""
    return " ".join((s or "").strip().upper().split())


def _clp(x) -> str:
    """Monto en pesos con separador de miles chileno (para los mensajes de error).
    Se formatea el NÚMERO, no la frase: un .replace(',', '.') sobre el mensaje completo
    se comería las comas de la prosa."""
    return f"{_f(x):,.0f}".replace(",", ".")


def _scope_embarque(db: Session, payload: CompraCreate) -> Tuple[Optional[int], Optional[int]]:
    """(pricing_id, embarque_id) del alcance «misma factura del mismo embarque».

    Lectura SIN lock, a propósito: solo descubre ids para poder tomar los candados
    DESPUÉS y EN ORDEN (mismo criterio que tesoreria._porton_egresos_de_las_compras).
    El embarque sale del gasto cuando el payload no lo manda: el botón del overlay manda
    los dos, pero la vía manual puede traer solo uno.
    """
    if payload.emb_pricing_gasto_id:
        fila = (db.query(EmbarquePricingGasto.pricing_id, EmbarquePricing.embarque_id)
                .join(EmbarquePricing, EmbarquePricing.id == EmbarquePricingGasto.pricing_id)
                .filter(EmbarquePricingGasto.id == payload.emb_pricing_gasto_id)
                .first())
        if fila:
            return int(fila[0]), int(fila[1])
    if payload.embarque_id:
        pid = (db.query(EmbarquePricing.id)
               .filter(EmbarquePricing.embarque_id == payload.embarque_id).scalar())
        return (int(pid) if pid else None), int(payload.embarque_id)
    return None, None


def _porton_pricing_del_embarque(db: Session, pricing_id: Optional[int]) -> None:
    """PORTÓN: bloquea la CABECERA del pricing del embarque antes de decidir si esta
    factura ya está registrada.

    Por qué hace falta un candado NUEVO: el lock del gasto (más abajo) solo serializa dos
    registros del MISMO gasto. El daño que reprodujeron los re-auditores usa DOS gastos
    distintos (mover la plata de 'agencia' a 'otros'), así que ese lock no se toca nunca y
    el chequeo por factura física quedaría sin serializar: dos POST simultáneos leerían
    los dos «no hay duplicado» y nacerían dos CxP por la misma factura (con READ COMMITTED
    y sin gap locks, un FOR UPDATE sobre un rango vacío no bloquea nada — database.py:28).
    La cabecera `emb_pricing` es la fila REAL que representa «este embarque»: una por
    embarque, exactamente el alcance del chequeo.

    ORDEN DE CANDADOS: se toma ANTES que el lock del gasto porque es el mismo orden que
    usa el router del pricing (cabecera → gastos, embarques_pricing/router.py:70). Al
    revés, el PUT del pricing y este POST se cruzarían (1213). De paso, este portón
    serializa también PUT-del-pricing ↔ POST-de-la-CxP: sin él, «vaciar la línea» y
    «registrar la otra línea» podían intercalarse y burlar los dos guards a la vez.
    """
    if not pricing_id:
        return
    (db.query(EmbarquePricing)
     .filter(EmbarquePricing.id == pricing_id)
     .populate_existing().with_for_update().first())


def _bloqueo_factura_fisica(
    db: Session, *, empresa: str, embarque_id: Optional[int], numero_documento,
    proveedor_rut, acreedor, tipo_doc: str, bruto_clp: float,
) -> None:
    """FAIL CLOSED: el anti-duplicado mira la FACTURA FÍSICA, no la línea del pricing.

    EL DAÑO QUE CIERRA (re-auditoría, HALLAZGOS 1 y 3 — reproducidos con UN clic)
    ---------------------------------------------------------------------------
    El freno anterior es por LÍNEA (`emb_pricing_gasto_id`), y las 6 líneas del pricing
    son 6 llaves distintas. Mover el mismo monto de 'agencia' a 'otros' dejaba la línea
    vieja "registrada" en $0 y la nueva "no registrada" con la plata → el botón
    «Registrar como compra» reaparecía y Σ CxP = 380.800 por una factura de 190.400
    (medido en las dos marcas). Y la vía MANUAL («Nueva compra», sin la llave del gasto)
    nunca tuvo red: es la puerta ORIGINAL del bug.

    LA LLAVE DE NEGOCIO CORRECTA
    ---------------------------
    Una factura de proveedor se identifica por (acreedor, N° de documento) — dentro de un
    embarque, además, el N° de documento por sí solo ya la identifica. La línea del
    pricing NO es la factura: es el VÍNCULO para el costeo (y así se queda). El freno
    mira la factura:

      REGLA 1 · mismo embarque + mismo N° de documento = MISMA factura → 409.
        Cierra el hueco que deja `uq_cont_compra_empresa_prov_doc`: ese UNIQUE exige que
        el RUT coincida, así que la misma factura cargada una vez con RUT y otra sin RUT
        (los NULL no colisionan en MySQL) pasaba dos veces.

      REGLA 2 · factura SIN N° de documento: no tiene identidad → si ya hay otra CxP
        ACTIVA con `tipo_doc='factura'`, del MISMO acreedor y por el MISMO bruto en CLP,
        el sistema NO PUEDE saber si es la misma factura cargada dos veces (desde otra
        línea del pricing, o a mano) o dos facturas distintas → 409 pidiendo intervención
        humana: que escriba el N°. Es exactamente el estado ambiguo del hallazgo: las 6
        líneas seed nacen con `nro_factura=None` (integration.py) y el front lo
        prefill-ea tal cual, así que hoy el único freno era un campo OPCIONAL.
        La compra que ya está registrada puede tener N° o no tenerlo: da igual, porque la
        que NO tiene identidad es la nueva. En cuanto el operador escribe el N°, quien
        juzga es la REGLA 1 / el UNIQUE por (RUT, N°), que sí saben decidir.

    POR QUÉ ESTA CALIBRACIÓN Y NO OTRA (evitar el 409 falso)
      · Solo `tipo_doc='factura'`: una factura chilena SIEMPRE tiene folio, así que una
        factura sin N° es un dato incompleto y el 409 es la corrección. Boletas, recibos
        y 'sin_documento' (caja chica, dos boletas iguales el mismo día) quedan fuera.
      · Mismo bruto: dos gastos DISTINTOS del mismo acreedor en el mismo embarque
        (agencia y almacenaje) tienen montos distintos y siguen pasando sin ruido.
      · Solo compras ACTIVAS: anular sigue siendo la salida legítima para re-registrar.
      · bruto > 0: una factura de $0 no tiene plata que duplicar.

    La salida SIEMPRE existe y está nombrada en el mensaje (escribir el N°, o anular la
    compra ya registrada): un 409 que obliga a un humano a mirar es infinitamente más
    barato que un pago duplicado a un proveedor.
    """
    doc = (numero_documento or "").strip()
    rut, nombre = _norm_txt(proveedor_rut), _norm_txt(acreedor)

    # REGLA 1 — dentro de UN embarque, el mismo N° de documento es la MISMA factura
    if doc and embarque_id:
        otra = (db.query(ContCompra)
                .filter(ContCompra.empresa == empresa,
                        ContCompra.embarque_id == embarque_id,
                        ContCompra.anulado.is_(False),
                        func.trim(func.coalesce(ContCompra.numero_documento, "")) == doc)
                .order_by(ContCompra.id.asc()).first())
        if otra:
            raise HTTPException(
                409,
                f"El documento {doc} ya está registrado en este embarque como la compra "
                f"#{otra.id} ({otra.acreedor or 'sin acreedor'} · "
                f"{_clp(otra.monto_total_clp)} CLP). Dentro de un mismo embarque el "
                f"mismo N° de documento es la MISMA factura, aunque se cargue desde otra "
                f"línea del pricing o a mano: si de verdad son dos documentos distintos "
                f"corrija el N°; si es la misma factura, anule la compra #{otra.id} antes "
                f"de volver a cargarla.")

    # REGLA 2 — factura SIN N° de documento: no hay identidad → bloquear y pedir el N°
    if doc or _norm_txt(tipo_doc or "factura") != "FACTURA" or _f(bruto_clp) <= 0:
        return
    candidatas = (db.query(ContCompra)
                  .filter(ContCompra.empresa == empresa,
                          ContCompra.anulado.is_(False),
                          ContCompra.tipo_doc == "factura",
                          ContCompra.monto_total_clp >= _f(bruto_clp) - TOL_BRUTO_CLP,
                          ContCompra.monto_total_clp <= _f(bruto_clp) + TOL_BRUTO_CLP)
                  .order_by(ContCompra.id.asc()).all())
    for c in candidatas:
        c_rut, c_nom = _norm_txt(c.proveedor_rut), _norm_txt(c.acreedor)
        mismo_acreedor = (
            (bool(rut) and bool(c_rut) and rut == c_rut)
            or (bool(nombre) and bool(c_nom) and nombre == c_nom)
            # Ninguna de las dos identifica al acreedor: fail closed (mismo monto, sin
            # N°, sin nombre ni RUT → no hay NADA que las distinga).
            or not (rut or nombre or c_rut or c_nom)
        )
        if not mismo_acreedor:
            continue
        doc_otra = (c.numero_documento or "").strip()
        raise HTTPException(
            409,
            f"Esta factura viene SIN N° de documento y ya hay una compra ACTIVA del mismo "
            f"acreedor por el mismo monto ({_clp(bruto_clp)} CLP): la compra #{c.id} "
            f"({'documento ' + doc_otra if doc_otra else 'también sin N° de documento'}). "
            f"Sin el N° de factura no se puede distinguir si es la MISMA factura cargada "
            f"dos veces (la línea del gasto no sirve de llave: la misma factura se puede "
            f"registrar desde otra línea del pricing o a mano) o dos facturas distintas. "
            f"Escriba el N° de factura y vuelva a intentar; si es la misma factura, anule "
            f"la compra #{c.id}.")


# ─── Crear compra ──────────────────────────────────────────────────────────────
@router.post("")
def crear_compra(
    payload: CompraCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Los locks del costeo nacional (ItemCotizacion + rangos de cont_compra_item)
    # pueden DEADLOCKEAR entre dos compras simultáneas incluso de ítems DISTINTOS
    # (gap locks de InnoDB sobre el índice). MySQL mata una transacción (1213); se
    # reintenta completa en vez de devolver un 500 al operador — mismo patrón que
    # el retry de correlativo en create_despacho. 1205 = lock wait timeout.
    for _ in range(3):
        try:
            return _crear_compra_tx(payload, db, current_user)
        except OperationalError as e:
            db.rollback()
            args = getattr(getattr(e, "orig", None), "args", None) or []
            if not args or args[0] not in (1213, 1205):
                raise
    raise HTTPException(
        409, "Conflicto momentáneo al registrar la compra (registros simultáneos): reintente")


def _crear_compra_tx(payload: CompraCreate, db: Session, current_user: User):
    empresa = empresa_de(current_user)
    if payload.tipo_gasto not in TIPOS_GASTO:
        raise HTTPException(400, f"tipo_gasto inválido: {payload.tipo_gasto}")

    acreedor = payload.acreedor
    if payload.proveedor_id and not acreedor:
        prov = db.query(Proveedor).filter(Proveedor.id == payload.proveedor_id).first()
        if prov:
            acreedor = prov.nombre

    if payload.numero_documento:
        dup = (db.query(ContCompra).filter(
            ContCompra.empresa == empresa, ContCompra.proveedor_rut == payload.proveedor_rut,
            ContCompra.numero_documento == payload.numero_documento, ContCompra.anulado.is_(False),
        ).first())
        if dup:
            raise HTTPException(409, f"Ya existe una compra con documento {payload.numero_documento} para este proveedor")

    # PORTÓN del embarque: se toma ANTES del lock del gasto (mismo orden que el router del
    # pricing: cabecera → gastos) y serializa el chequeo por FACTURA FÍSICA de más abajo,
    # que mira VARIAS líneas de gasto y por eso el lock de UNA línea no alcanza a cubrir.
    pricing_id_scope, embarque_scope = _scope_embarque(db, payload)
    _porton_pricing_del_embarque(db, pricing_id_scope)

    # Punteros a embarque: si vienen, deben existir (trazabilidad). Dedup del overlay de
    # embarques: el mismo gasto de embarque no puede convertirse en DOS compras activas
    # (CxP duplicada). Espejo del control que ya tiene Monza.
    if payload.emb_pricing_gasto_id:
        # Lock del gasto ANTES del chequeo de duplicado: serializa dos registros
        # concurrentes del MISMO gasto (el 2° espera el commit del 1°). `models.py:102`
        # es index=True, NO unique, y los UniqueConstraint de :36-42 no incluyen esta
        # columna → el lock es la única defensa que existe.
        g = (db.query(EmbarquePricingGasto)
             .filter(EmbarquePricingGasto.id == payload.emb_pricing_gasto_id)
             .with_for_update().first())
        if not g:
            raise HTTPException(400, "emb_pricing_gasto_id no existe")
        # El chequeo de duplicado va SIN lock a propósito: el candado es la fila del
        # gasto (fila REAL y existente), y con READ COMMITTED (database.py:34) esta
        # lectura renueva la foto → el 2° request, ya serializado, ve la compra que el
        # 1° acaba de commitear. Un FOR UPDATE sobre este rango vacío no serviría de
        # nada: sin gap locks no hay qué bloquear (database.py:28-29).
        dup_g = (db.query(ContCompra).filter(
            ContCompra.empresa == empresa,
            ContCompra.emb_pricing_gasto_id == payload.emb_pricing_gasto_id,
            ContCompra.anulado.is_(False)).first())
        if dup_g:
            raise HTTPException(409, f"Ese gasto de embarque ya está registrado como la compra #{dup_g.id}; anúlela antes de volver a registrarlo")
    if payload.embarque_id:
        if not db.query(Embarque).filter(Embarque.id == payload.embarque_id).first():
            raise HTTPException(400, "embarque_id no existe")

    moneda = payload.moneda
    tc = _f(payload.tc)
    if moneda == "CLP":
        tc = 1.0
    elif tc <= 0:
        raise HTTPException(400, "Indique un tipo de cambio (TC) mayor a 0 para compras en moneda extranjera")

    neto = _f(payload.monto_neto)
    if payload.iva is not None:
        iva = _f(payload.iva)
    elif payload.afecto_iva:
        # CLP no usa decimales: el IVA auto-calculado se redondea a peso, como en el
        # documento SII (half-up: 142.5 → 143; el round() de Python es half-to-even
        # y daría 142). Monedas extranjeras a 2 decimales.
        iva = float(int(neto * IVA_RATE + 0.5)) if moneda == "CLP" else round(neto * IVA_RATE, 2)
    else:
        iva = 0.0
    total = _f(payload.monto_total) if payload.monto_total is not None else round(neto + iva, 2)
    total_clp = round(total * tc, 2)

    # Freno por FACTURA FÍSICA (no por línea del pricing): va acá porque necesita el bruto
    # en CLP ya calculado, y ya está serializado por el portón de arriba. Cubre las dos
    # puertas que el freno por línea deja abiertas: mover el monto a otra línea del gasto
    # y la vía MANUAL sin la llave del gasto (ver el docstring de _bloqueo_factura_fisica).
    _bloqueo_factura_fisica(
        db, empresa=empresa, embarque_id=embarque_scope,
        numero_documento=payload.numero_documento, proveedor_rut=payload.proveedor_rut,
        acreedor=acreedor, tipo_doc=payload.tipo_doc or "factura", bruto_clp=total_clp)

    fecha = _fecha(payload.fecha, "fecha") or date.today()
    condicion = payload.condicion_pago
    if condicion == "contado":
        fecha_venc = fecha
    elif payload.plazo_dias is not None:
        # `is not None`: plazo 0 días también debe generar vencimiento (= fecha)
        fecha_venc = fecha + timedelta(days=int(payload.plazo_dias))
    else:
        fecha_venc = None

    cuenta_id = payload.cuenta_contable_id
    if cuenta_id:
        cta = db.query(ContPlanCuenta).filter(
            ContPlanCuenta.id == cuenta_id, ContPlanCuenta.empresa == empresa,
            ContPlanCuenta.activa.is_(True)).first()
        if not cta:
            raise HTTPException(400, "Cuenta contable inválida para esta empresa")
        if not cta.imputable:
            raise HTTPException(400, f"La cuenta {cta.codigo} {cta.nombre} es de título/mayor: impute a una cuenta hoja")
        if cta.requiere_auxiliar and not (payload.proveedor_rut or "").strip():
            raise HTTPException(400, f"La cuenta {cta.codigo} {cta.nombre} exige RUT del proveedor (auxiliar)")
    else:
        cod = cuenta_default_codigo(payload.origen, payload.tipo_gasto)
        cta = (db.query(ContPlanCuenta)
               .filter(ContPlanCuenta.empresa == empresa, ContPlanCuenta.codigo == cod).first()
               if cod else None)
        cuenta_id = cta.id if cta else None

    compra = ContCompra(
        empresa=empresa, origen=(payload.origen or "MANUAL").upper(),
        tipo_gasto=payload.tipo_gasto, categoria=payload.categoria,
        cuenta_contable_id=cuenta_id, es_anticipo=payload.es_anticipo,
        proveedor_id=payload.proveedor_id, acreedor=acreedor, proveedor_rut=payload.proveedor_rut,
        fecha=fecha, referencia=payload.referencia, descripcion=payload.descripcion,
        numero_documento=payload.numero_documento, tipo_doc=payload.tipo_doc or "factura",
        moneda=moneda, tc=tc, monto_neto=neto, iva=iva, monto_total=total, monto_total_clp=total_clp,
        condicion_pago=condicion, plazo_dias=payload.plazo_dias, fecha_vencimiento=fecha_venc,
        embarque_id=payload.embarque_id, emb_pricing_gasto_id=payload.emb_pricing_gasto_id,
        factura_proveedor_id=payload.factura_proveedor_id,
        observaciones=payload.observaciones, usuario_id=getattr(current_user, "id", None),
    )
    try:
        db.add(compra)
        db.flush()
        # Detalle de costeo por ítem (compra nacional): la factura ES el costo de esos
        # repuestos. Los guards (doble costeo, Σ≤neto, Σ costeado≤recibido) validan aquí.
        if payload.items:
            _crear_items_costeo(db, compra, payload, empresa)
        uid = getattr(current_user, "id", None)
        pago = payload.pago
        # Para compras en moneda extranjera, el TC de la compra viaja al egreso/detalle
        # (tc_aplicado): sin él, la diferencia de cambio NIC 21 nace incalculable.
        moneda_pago = compra.moneda if (compra.moneda or "CLP") != "CLP" else "CLP"
        tc_pago = _f(compra.tc) if moneda_pago != "CLP" else 1.0
        if pago is None and condicion == "contado" and total_clp > 0:
            # Contado sin pago explícito: egreso por el total el mismo día.
            _crear_egreso(db, empresa=empresa, detalles=[(compra.id, total_clp)], usuario_id=uid,
                          fecha=fecha, medio="transferencia", beneficiario=acreedor,
                          beneficiario_rut=payload.proveedor_rut, fecha_mov_bancario=fecha,
                          moneda=moneda_pago, tc=tc_pago)
        elif pago is not None:
            monto_pago = _f(pago.monto_clp) if pago.monto_clp is not None else total_clp
            if monto_pago > total_clp + TOL_PAGO:
                raise HTTPException(400, f"El pago excede el total de la compra ({total_clp:.0f})")
            if monto_pago > 0:
                pfecha = _fecha(pago.fecha, "pago.fecha") or fecha
                _crear_egreso(db, empresa=empresa, detalles=[(compra.id, monto_pago)], usuario_id=uid,
                              fecha=pfecha, medio=pago.medio, banco=pago.banco,
                              cuenta_origen_id=pago.cuenta_origen_id, numero_operacion=pago.numero_operacion,
                              beneficiario=acreedor, beneficiario_rut=payload.proveedor_rut,
                              glosa=pago.observaciones,
                              fecha_mov_bancario=_fecha(pago.fecha_mov_bancario, "pago.fecha_mov_bancario") or pfecha,
                              moneda=moneda_pago, tc=tc_pago)
        db.flush()
        db.refresh(compra)
        _recompute_compra(compra)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as e:
        db.rollback()
        if "uq_cont_compra_empresa_prov_doc" in str(getattr(e, "orig", e)):
            raise HTTPException(409, "Documento de compra duplicado para este proveedor")
        raise HTTPException(409, "No se pudo guardar la compra (conflicto de integridad)")
    db.refresh(compra)
    return _serialize_compra(compra)


# ─── KPIs ──────────────────────────────────────────────────────────────────────
@router.get("/kpis")
def get_kpis(
    periodo: Optional[str] = None, tipo: Optional[str] = None,
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user),
):
    empresa = empresa_de(current_user)
    hoy = date.today()
    base = db.query(ContCompra).filter(ContCompra.empresa == empresa, ContCompra.anulado.is_(False))
    base = _apply_filtros(base, tipo=tipo, periodo=periodo)
    s = ContCompra.saldo_clp
    n, total, pagado, por_pagar, vencido = base.with_entities(
        func.count(ContCompra.id),
        func.coalesce(func.sum(ContCompra.monto_total_clp), 0),
        func.coalesce(func.sum(ContCompra.monto_pagado_clp), 0),
        func.coalesce(func.sum(case((s > TOL, s), else_=0)), 0),
        func.coalesce(func.sum(case(
            (and_(s > TOL, ContCompra.fecha_vencimiento.isnot(None), ContCompra.fecha_vencimiento < hoy), s),
            else_=0)), 0),
    ).one()
    por_tipo = {t: 0.0 for t in TIPOS_GASTO}
    for t, v in (base.with_entities(ContCompra.tipo_gasto, func.coalesce(func.sum(ContCompra.monto_total_clp), 0))
                     .group_by(ContCompra.tipo_gasto).all()):
        if t in por_tipo:
            por_tipo[t] = round(_f(v), 0)
    return {
        "n_compras": int(n or 0), "total_comprado_clp": round(_f(total), 0),
        "pagado_clp": round(_f(pagado), 0), "por_pagar_clp": round(_f(por_pagar), 0),
        "vencido_clp": round(_f(vencido), 0), "por_tipo": por_tipo,
    }


# ─── Catálogos ─────────────────────────────────────────────────────────────────
@router.get("/catalogos")
def get_catalogos(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    empresa = empresa_de(current_user)
    provs = db.query(Proveedor).order_by(Proveedor.nombre.asc()).all()
    cuentas = (db.query(ContPlanCuenta)
               .filter(ContPlanCuenta.empresa == empresa,
                       ContPlanCuenta.imputable.is_(True), ContPlanCuenta.activa.is_(True))
               .order_by(ContPlanCuenta.orden.asc()).all())
    cod_to_id = {c.codigo: c.id for c in cuentas}
    cuenta_default_por_tipo = {
        f"{origen}|{tipo}": cod_to_id.get(cod) for (origen, tipo), cod in CUENTA_DEFAULT_CODIGO.items()
    }
    return {
        "tipos_gasto": [{"value": t, "label": TIPO_GASTO_LABEL[t]} for t in TIPOS_GASTO],
        "estados_pago": ESTADOS_PAGO, "categorias_sugeridas": CATEGORIAS_SUGERIDAS,
        "medios_pago": MEDIOS_PAGO, "iva_rate": IVA_RATE,
        "proveedores": [{"id": p.id, "nombre": p.nombre, "moneda": p.moneda, "pais": p.pais} for p in provs],
        "plan_cuentas": [{"id": c.id, "codigo": c.codigo, "nombre": c.nombre, "clase": c.clase,
                          "grupo": c.grupo, "requiere_auxiliar": bool(c.requiere_auxiliar)} for c in cuentas],
        "cuenta_default_por_tipo": cuenta_default_por_tipo,
    }


# ─── Overlay de los gastos de embarque (reflejo en vivo del pricing) ───────────
@router.get("/costos-embarque")
def costos_embarque(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Gastos anotados en Embarques Pricing (emb_pricing_gasto), en vivo. Cada fila trae
    `compra_id` (!= None → ya está reflejado como CxP pagable): sin esa llave el front
    re-digitaba el gasto a mano, la compra nacía con emb_pricing_gasto_id NULL y el
    dedup de crear_compra quedaba inalcanzable (en MySQL los NULL no colisionan, ver
    models.py:32-34) → la factura del forwarder se cargaba 2 y 3 veces."""
    empresa = empresa_de(current_user)
    rows = (db.query(EmbarquePricingGasto, Embarque)
            .join(EmbarquePricing, EmbarquePricing.id == EmbarquePricingGasto.pricing_id)
            .join(Embarque, Embarque.id == EmbarquePricing.embarque_id)
            .order_by(Embarque.id.desc(), EmbarquePricingGasto.orden.asc()).all())
    emb_ids = {e.id for _g, e in rows}
    prov_by_emb: dict = {}
    if emb_ids:
        for emb_id, prov in (db.query(EmbarqueItem.embarque_id, OcProveedor.proveedor)
                             .join(OcProveedor, OcProveedor.id == EmbarqueItem.oc_proveedor_id)
                             .filter(EmbarqueItem.embarque_id.in_(emb_ids)).all()):
            prov_by_emb.setdefault(emb_id, prov)
    # Gastos ya registrados como compra ACTIVA (para marcarlos y no duplicarlos). Batch
    # por emb_pricing_gasto_id, sin N+1.
    gasto_ids = [g.id for g, _e in rows]
    registrados: dict = {}
    if gasto_ids:
        for gid, cid in (db.query(ContCompra.emb_pricing_gasto_id, ContCompra.id)
                         .filter(ContCompra.empresa == empresa,
                                 ContCompra.emb_pricing_gasto_id.in_(gasto_ids),
                                 ContCompra.anulado.is_(False)).all()):
            registrados[gid] = cid
    out = []
    for g, e in rows:
        neto, iva = _f(g.monto_neto), _f(g.iva)
        out.append({
            "id": g.id, "origen": "EMBARQUE", "embarque_id": e.id, "embarque_numero": e.numero,
            "tipo": g.tipo, "glosa": g.glosa, "acreedor": prov_by_emb.get(e.id),
            "monto_neto": neto, "iva": iva, "monto_total": round(neto + iva, 2),
            "nro_factura": g.nro_factura, "fecha_factura": g.fecha_factura,
            "banco": g.banco, "capitaliza": bool(g.capitaliza),
            "compra_id": registrados.get(g.id),   # != None → ya reflejado como compra pagable
        })
    return {"costos": out, "total_clp": round(sum(r["monto_total"] for r in out), 0), "n": len(out)}


# ─── Catálogo de OC nacionales costeables (para el detalle por ítem del front) ──
@router.get("/oc-nacionales")
def oc_nacionales(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """OCs con `tipo_origen='nacional'` y sus ítems costeables. Para cada ítem:
    cantidad (vendida), recibido (nacional utilizable), ya_costeado (Σ cont_compra_item
    activas) y disponible_costear = max(min(recibido, cantidad) − ya_costeado, 0).
    Batch, sin N+1."""
    from collections import defaultdict
    empresa = empresa_de(current_user)
    ocps = (db.query(OcProveedor)
            .filter(OcProveedor.tipo_origen == "nacional")
            .order_by(OcProveedor.id.desc()).all())
    if not ocps:
        return {"ocs": []}
    ocp_ids = [o.id for o in ocps]
    asigns = (db.query(OcProveedorItem)
              .filter(OcProveedorItem.oc_proveedor_id.in_(ocp_ids)).all())
    item_ids = [a.item_cotizacion_id for a in asigns if a.item_cotizacion_id is not None]
    items = ({it.id: it for it in db.query(ItemCotizacion)
              .filter(ItemCotizacion.id.in_(item_ids)).all()} if item_ids else {})
    recibido = _recibido_nacional(db, item_ids)
    ya_cost = ({i: _f(q) for i, q in (
        db.query(ContCompraItem.item_cotizacion_id,
                 func.coalesce(func.sum(ContCompraItem.cantidad), 0))
        .join(ContCompra, ContCompra.id == ContCompraItem.compra_id)
        .filter(ContCompraItem.item_cotizacion_id.in_(item_ids),
                ContCompra.empresa == empresa, ContCompra.anulado.is_(False))
        .group_by(ContCompraItem.item_cotizacion_id).all())} if item_ids else {})

    by_ocp = defaultdict(list)
    for a in asigns:
        by_ocp[a.oc_proveedor_id].append(a)

    out = []
    for o in ocps:
        item_list = []
        for a in by_ocp.get(o.id, []):
            it = items.get(a.item_cotizacion_id)
            if not it:
                continue
            cant = _f(it.cantidad)
            recib = recibido.get(it.id, 0.0)
            costeado = ya_cost.get(it.id, 0.0)
            item_list.append({
                "item_cotizacion_id": it.id,
                "oc_proveedor_item_id": a.id,
                "numero_parte": it.numero_parte,
                "descripcion": it.descripcion,
                "cantidad": cant,
                "recibido": recib,
                "ya_costeado": costeado,
                "disponible_costear": max(min(recib, cant) - costeado, 0.0),
            })
        out.append({
            "oc_proveedor_id": o.id,
            "numero": o.numero,
            "numero_oc": o.numero_oc,
            "proveedor": o.proveedor,
            "moneda": o.moneda,
            "items": item_list,
        })
    return {"ocs": out}


# ─── Egresos (pago consolidado / listado) ──────────────────────────────────────
@router.get("/egresos")
def listar_egresos(
    conciliado: Optional[bool] = None, q: Optional[str] = None,
    page: int = 1, page_size: int = PAGE_SIZE_DEFAULT,
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user),
):
    empresa = empresa_de(current_user)
    page = max(1, int(page))
    page_size = min(max(1, int(page_size)), PAGE_SIZE_MAX)
    base = db.query(ContEgreso).filter(ContEgreso.empresa == empresa)
    if conciliado is not None:
        base = base.filter(ContEgreso.conciliado.is_(bool(conciliado)))
    if q:
        like = f"%{q}%"
        base = base.filter(or_(ContEgreso.beneficiario.ilike(like),
                               ContEgreso.numero_operacion.ilike(like), ContEgreso.banco.ilike(like)))
    total = base.count()
    rows = (base.options(selectinload(ContEgreso.detalles), selectinload(ContEgreso.cuenta_origen))
                .order_by(ContEgreso.id.desc()).offset((page - 1) * page_size).limit(page_size).all())
    return {"egresos": [serialize_egreso(e) for e in rows], "total": int(total),
            "page": page, "page_size": page_size}


@router.post("/egresos")
def crear_egreso_consolidado(
    payload: EgresoCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Comprobante de Egreso que paga VARIAS compras en una sola salida de dinero."""
    empresa = empresa_de(current_user)
    fecha = _fecha(payload.fecha, "fecha") or date.today()
    try:
        egreso = _crear_egreso(
            db, empresa=empresa, usuario_id=getattr(current_user, "id", None),
            detalles=[(d.compra_id, d.monto_clp) for d in payload.detalles],
            fecha=fecha, medio=payload.medio, cuenta_origen_id=payload.cuenta_origen_id,
            banco=payload.banco, numero_operacion=payload.numero_operacion,
            beneficiario=payload.beneficiario, beneficiario_rut=payload.beneficiario_rut,
            glosa=payload.glosa, moneda=payload.moneda, tc=payload.tc,
            fecha_mov_bancario=_fecha(payload.fecha_mov_bancario, "fecha_mov_bancario") or fecha,
        )
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    db.refresh(egreso)
    return serialize_egreso(egreso)


@router.delete("/egresos/{egreso_id}")
def eliminar_egreso(
    egreso_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Revierte un egreso completo (libera el pago de todas las compras que pagaba).
    Rechaza si está conciliado con el banco (desconcílielo primero)."""
    empresa = empresa_de(current_user)
    egreso = (db.query(ContEgreso)
              .filter(ContEgreso.id == egreso_id, ContEgreso.empresa == empresa)
              .with_for_update().first())
    if not egreso:
        raise HTTPException(404, "Egreso no encontrado")
    if egreso.conciliado:
        raise HTTPException(409, "El egreso está conciliado con el banco; desconcílielo en Tesorería primero")
    compra_ids = [d.compra_id for d in egreso.detalles]
    db.delete(egreso)
    db.flush()
    for cid in compra_ids:
        compra = (db.query(ContCompra)
                  .filter(ContCompra.id == cid, ContCompra.empresa == empresa)
                  .with_for_update().first())
        if compra:
            db.refresh(compra)
            _recompute_compra(compra)
    db.commit()
    return {"ok": True, "compras_afectadas": compra_ids}


@router.patch("/egresos/{egreso_id}")
def actualizar_egreso(
    egreso_id: int,
    payload: EgresoUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Completa/edita datos de conciliación de un egreso (fecha en el banco / referencia).
    Lock de fila: sin él, entre el guard `conciliado` y el UPDATE queda una ventana en la
    que Tesorería puede conciliar (TOCTOU). El PATCH pasaba el guard con la lectura vieja
    y después PISABA la fecha/referencia que la cartola acababa de fijar — cartola y libro
    discrepando sin rastro. Con el lock, el guard y la escritura son atómicos."""
    empresa = empresa_de(current_user)
    egreso = (db.query(ContEgreso)
              .filter(ContEgreso.id == egreso_id, ContEgreso.empresa == empresa)
              .populate_existing().with_for_update().first())
    if not egreso:
        raise HTTPException(404, "Egreso no encontrado")
    if egreso.conciliado:
        raise HTTPException(409, "El egreso está conciliado: la cartola es la fuente de verdad; desconcílielo en Tesorería para editar estos datos")
    if payload.fecha_mov_bancario is not None:
        egreso.fecha_mov_bancario = _fecha(payload.fecha_mov_bancario, "fecha_mov_bancario")
    if payload.referencia_bancaria is not None:
        egreso.referencia_bancaria = payload.referencia_bancaria or None
    db.commit()
    db.refresh(egreso)
    return serialize_egreso(egreso)


# ─── Detalle ───────────────────────────────────────────────────────────────────
@router.get("/{compra_id}")
def detalle_compra(compra_id: int, db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_user)):
    # Carga ansiosa igual que el listado (:229-238): el serializador recorre pagos →
    # egreso → detalles (_serialize_pago usa len(e.detalles)) e items. Sin esto era
    # 1 query por pago y por ítem (N+1: ~42 queries con 20 pagos).
    empresa = empresa_de(current_user)
    compra = (
        db.query(ContCompra)
        .options(
            selectinload(ContCompra.cuenta),
            selectinload(ContCompra.items),
            selectinload(ContCompra.egreso_detalles)
            .selectinload(ContEgresoDetalle.egreso)
            .selectinload(ContEgreso.detalles),
        )
        .filter(ContCompra.id == compra_id, ContCompra.empresa == empresa)
        .first()
    )
    if not compra:
        raise HTTPException(404, "Compra no encontrada")
    return _serialize_compra(compra)


# ─── Pago de UNA compra (egreso de 1 detalle) + revertir/editar ────────────────
@router.post("/{compra_id}/pagos")
def registrar_pago(
    compra_id: int, payload: PagoIn,
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """Paga UNA compra (parcial o total): crea un egreso con un solo detalle."""
    empresa = empresa_de(current_user)
    # lock=True: el chequeo de anulado se hace sobre estado actual (no snapshot stale).
    compra = _get_compra_scoped(db, compra_id, empresa, lock=True)
    if compra.anulado:
        raise HTTPException(400, "La compra está anulada")
    pfecha = _fecha(payload.fecha, "fecha") or date.today()
    # compra en moneda extranjera → el TC viaja al egreso/detalle (NIC 21)
    moneda_pago = compra.moneda if (compra.moneda or "CLP") != "CLP" else "CLP"
    tc_pago = _f(compra.tc) if moneda_pago != "CLP" else 1.0
    try:
        _crear_egreso(
            db, empresa=empresa, usuario_id=getattr(current_user, "id", None),
            detalles=[(compra_id, payload.monto_clp)],
            fecha=pfecha, medio=payload.medio, banco=payload.banco,
            cuenta_origen_id=payload.cuenta_origen_id, numero_operacion=payload.numero_operacion,
            beneficiario=compra.acreedor, beneficiario_rut=compra.proveedor_rut,
            glosa=payload.observaciones,
            fecha_mov_bancario=_fecha(payload.fecha_mov_bancario, "fecha_mov_bancario") or pfecha,
            moneda=moneda_pago, tc=tc_pago,
        )
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    db.refresh(compra)
    return _serialize_compra(compra)


def _detalle_scoped(db, compra_id, pago_id, empresa) -> ContEgresoDetalle:
    """pago_id (lo que ve el front) = id de la asignación (detalle de egreso) de la compra."""
    d = (db.query(ContEgresoDetalle)
         .join(ContCompra, ContCompra.id == ContEgresoDetalle.compra_id)
         .filter(ContEgresoDetalle.id == pago_id, ContEgresoDetalle.compra_id == compra_id,
                 ContCompra.empresa == empresa)
         .first())
    if not d:
        raise HTTPException(404, "Pago no encontrado")
    return d


@router.patch("/{compra_id}/pagos/{pago_id}")
def actualizar_pago(
    compra_id: int, pago_id: int, payload: EgresoUpdate,
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """Edita la fecha del banco / referencia del EGRESO al que pertenece este pago.
    Lock de fila del egreso: mismo criterio que actualizar_egreso/eliminar_pago."""
    empresa = empresa_de(current_user)
    d = _detalle_scoped(db, compra_id, pago_id, empresa)
    egreso = (db.query(ContEgreso)
              .filter(ContEgreso.id == d.egreso_id, ContEgreso.empresa == empresa)
              .populate_existing().with_for_update().first())
    if not egreso:
        raise HTTPException(404, "Egreso no encontrado")
    if egreso.conciliado:
        raise HTTPException(409, "El pago está conciliado: la cartola es la fuente de verdad; desconcílielo en Tesorería para editar estos datos")
    if payload.fecha_mov_bancario is not None:
        egreso.fecha_mov_bancario = _fecha(payload.fecha_mov_bancario, "fecha_mov_bancario")
    if payload.referencia_bancaria is not None:
        egreso.referencia_bancaria = payload.referencia_bancaria or None
    db.commit()
    compra = _get_compra_scoped(db, compra_id, empresa)
    return _serialize_compra(compra)


@router.delete("/{compra_id}/pagos/{pago_id}")
def eliminar_pago(
    compra_id: int, pago_id: int,
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """Revierte el pago: borra el EGRESO completo al que pertenece (puede afectar a varias
    compras si era consolidado). Rechaza si el egreso está conciliado."""
    empresa = empresa_de(current_user)
    d = _detalle_scoped(db, compra_id, pago_id, empresa)
    egreso = (db.query(ContEgreso).filter(ContEgreso.id == d.egreso_id).with_for_update().first())
    if egreso and egreso.conciliado:
        raise HTTPException(409, "El pago está conciliado con el banco; desconcílielo en Tesorería primero")
    compra_ids = [x.compra_id for x in egreso.detalles]
    db.delete(egreso)
    db.flush()
    for cid in compra_ids:
        compra = (db.query(ContCompra)
                  .filter(ContCompra.id == cid, ContCompra.empresa == empresa)
                  .with_for_update().first())
        if compra:
            db.refresh(compra)
            _recompute_compra(compra)
    db.commit()
    return {"ok": True, "compras_afectadas": compra_ids}


# ─── Anular / eliminar ─────────────────────────────────────────────────────────
@router.post("/{compra_id}/anular")
def anular_compra(
    compra_id: int, payload: AnularIn,
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user),
):
    empresa = empresa_de(current_user)
    compra = _get_compra_scoped(db, compra_id, empresa, lock=True)
    if compra.egreso_detalles:
        raise HTTPException(409, "La compra tiene pagos; revierta los pagos antes de anular")
    compra.anulado = True
    compra.motivo_anulacion = payload.motivo
    _recompute_compra(compra)
    db.commit()
    db.refresh(compra)
    return _serialize_compra(compra)


@router.delete("/{compra_id}")
def eliminar_compra(
    compra_id: int,
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user),
):
    empresa = empresa_de(current_user)
    # lock=True: sin lock, un pago concurrente podría colarse entre el chequeo y el DELETE.
    compra = _get_compra_scoped(db, compra_id, empresa, lock=True)
    if compra.egreso_detalles:
        raise HTTPException(409, "La compra tiene pagos registrados; revierta los pagos o use Anular")
    db.delete(compra)
    db.commit()
    return {"ok": True}
