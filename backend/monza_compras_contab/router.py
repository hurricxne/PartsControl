"""API del módulo Compras / Cuentas por Pagar MonzaParts (AP).

Prefijo: /api/monza/compras-contab (montado sin prefix; el router ya lo trae).
SOLO MonzaParts: candado require_empresa("automotriz") a nivel de router.

Espejo del módulo de Grupo AM (backend/compras_contab/router.py) sobre tablas monza_*:
registrar compras/gastos, imputarlas a una cuenta NIIF, llevar condición/estado de pago
(al crear se puede pagar de inmediato o dejar a crédito), y pagarlas vía Comprobantes de
Egreso (una salida real de dinero puede pagar varias compras — flujo NIC 7).

Los COSTOS DE EMBARQUE no se digitan de nuevo: se anotan en Embarques Pricing
(monza_emb_pricing_gasto) y acá se ven reflejados automáticamente (overlay solo lectura,
endpoint /costos-embarque).
"""
from datetime import date, timedelta
from typing import Optional, List, Tuple

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, case, and_, or_
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.exc import IntegrityError

from database import get_db
from auth import get_current_user
from empresa_guard import require_empresa
from models.models import User
from monza_models import (
    MonzaProveedor, MonzaEmbarque, MonzaEmbarqueItem, MonzaCotizacionItem, MonzaOcProveedor,
)
from monza_embarques_pricing.models import MonzaEmbPricing, MonzaEmbPricingGasto

from .models import MonzaContCompra, MonzaContEgreso, MonzaContEgresoDetalle, MonzaContPlanCuenta
from .schemas import CompraCreate, PagoIn, EgresoCreate, EgresoUpdate, AnularIn
from .service import (
    IVA_RATE, TOL, TOL_PAGO, TIPOS_GASTO, TIPO_GASTO_LABEL,
    CATEGORIAS_SUGERIDAS, MEDIOS_PAGO, ESTADOS_PAGO, CUENTA_DEFAULT_CODIGO,
    _f, _recompute_compra, _serialize_compra, serialize_egreso,
    parse_date_estricta, cuenta_default_codigo,
)

router = APIRouter(
    prefix="/api/monza/compras-contab",
    tags=["monza-compras-contab"],
    dependencies=[Depends(require_empresa("automotriz"))],
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
    s, p, v = MonzaContCompra.saldo_clp, MonzaContCompra.monto_pagado_clp, MonzaContCompra.fecha_vencimiento
    vencida = and_(v.isnot(None), v < hoy)
    if estado == "anulado":
        return query.filter(MonzaContCompra.anulado.is_(True))
    query = query.filter(MonzaContCompra.anulado.is_(False))
    if estado == "pagado":
        return query.filter(s <= TOL)
    if estado == "vencido":
        return query.filter(s > TOL, vencida)
    if estado == "parcial":
        return query.filter(s > TOL, p > TOL, or_(v.is_(None), v >= hoy))
    if estado == "pendiente":
        return query.filter(s > TOL, p <= TOL, or_(v.is_(None), v >= hoy))
    return query.filter(MonzaContCompra.estado_pago == estado)


def _apply_filtros(query, *, tipo=None, estado_pago=None, categoria=None,
                   periodo=None, q=None, proveedor_id=None):
    if tipo:
        query = query.filter(MonzaContCompra.tipo_gasto == tipo)
    if estado_pago:
        query = _filtro_estado_dinamico(query, estado_pago)
    if categoria:
        query = query.filter(MonzaContCompra.categoria == categoria)
    if proveedor_id:
        query = query.filter(MonzaContCompra.proveedor_id == int(proveedor_id))
    desde = _periodo_rango(periodo)
    if desde:
        query = query.filter(MonzaContCompra.fecha >= desde)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            MonzaContCompra.acreedor.ilike(like), MonzaContCompra.numero_documento.ilike(like),
            MonzaContCompra.referencia.ilike(like), MonzaContCompra.descripcion.ilike(like),
            MonzaContCompra.proveedor_rut.ilike(like), MonzaContCompra.categoria.ilike(like),
        ))
    return query


def _get_compra(db: Session, compra_id: int, *, lock: bool = False) -> MonzaContCompra:
    q = db.query(MonzaContCompra).filter(MonzaContCompra.id == compra_id)
    if lock:
        q = q.with_for_update()
    compra = q.first()
    if not compra:
        raise HTTPException(404, "Compra no encontrada")
    return compra


def _crear_egreso(
    db: Session, *, detalles: List[Tuple[int, float]], usuario_id,
    fecha=None, medio="transferencia", cuenta_origen_id=None, banco=None,
    numero_operacion=None, beneficiario=None, beneficiario_rut=None, glosa=None,
    moneda="CLP", tc=1.0, fecha_mov_bancario=None,
) -> MonzaContEgreso:
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
        compra = (db.query(MonzaContCompra)
                  .filter(MonzaContCompra.id == compra_id)
                  .with_for_update().first())
        if not compra:
            raise HTTPException(404, f"Compra {compra_id} no encontrada")
        if compra.anulado:
            raise HTTPException(400, f"La compra {compra_id} está anulada")
        # Lectura BLOQUEANTE de los pagos ya imputados (espejo del fix de compras_contab):
        # la relación perezosa es una lectura PLANA y bajo REPEATABLE READ sirve el
        # snapshot abierto por la primera sentencia del request, ANTERIOR al lock. Dos
        # egresos simultáneos a la misma compra pasaban AMBOS el tope y sobre-pagaban.
        pagado_actual = sum(
            _f(d.monto_clp) for d in
            db.query(MonzaContEgresoDetalle)
              .filter(MonzaContEgresoDetalle.compra_id == compra.id)
              .populate_existing().with_for_update().all())
        saldo = round(_f(compra.monto_total_clp) - pagado_actual, 2)
        if monto > saldo + TOL_PAGO:
            raise HTTPException(400, f"El pago a la compra {compra_id} excede su saldo ({max(saldo, 0):.0f})")
        compras[compra_id] = compra
        total += monto
    total = round(total, 2)
    tc_v = _f(tc) or 1.0
    egreso = MonzaContEgreso(
        fecha=fecha, medio=medio or "transferencia",
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
        db.add(MonzaContEgresoDetalle(
            egreso_id=egreso.id, compra_id=compra_id, monto_clp=m,
            tc_aplicado=tc_v, monto_origen=round(m / tc_v, 2) if tc_v else m,
        ))
    db.flush()
    for compra in compras.values():
        db.refresh(compra)
        _recompute_compra(compra)
    return egreso


def _antiguedad(db: Session, *, tipo=None, estado_pago=None, categoria=None,
                periodo=None, q=None, proveedor_id=None) -> dict:
    hoy = date.today()
    d30, d60, d90 = hoy - timedelta(days=30), hoy - timedelta(days=60), hoy - timedelta(days=90)
    f = func.coalesce(MonzaContCompra.fecha, hoy)
    s = MonzaContCompra.saldo_clp
    qy = db.query(
        func.coalesce(func.sum(case((f >= d30, s), else_=0)), 0),
        func.coalesce(func.sum(case((and_(f < d30, f >= d60), s), else_=0)), 0),
        func.coalesce(func.sum(case((and_(f < d60, f >= d90), s), else_=0)), 0),
        func.coalesce(func.sum(case((f < d90, s), else_=0)), 0),
    ).filter(MonzaContCompra.anulado.is_(False), MonzaContCompra.saldo_clp > TOL)
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
    page = max(1, int(page))
    page_size = min(max(1, int(page_size)), PAGE_SIZE_MAX)
    base = db.query(MonzaContCompra)
    if not incluir_anulados:
        base = base.filter(MonzaContCompra.anulado.is_(False))
    base = _apply_filtros(base, tipo=tipo, estado_pago=estado_pago, categoria=categoria,
                          periodo=periodo, q=q, proveedor_id=proveedor_id)
    total = base.count()
    rows = (
        base.options(
            selectinload(MonzaContCompra.cuenta),
            # cadena completa hasta MonzaContEgreso.detalles: _serialize_pago usa
            # len(e.detalles) y sin esta carga ansiosa era 1 query por egreso (N+1)
            selectinload(MonzaContCompra.egreso_detalles)
            .selectinload(MonzaContEgresoDetalle.egreso)
            .selectinload(MonzaContEgreso.detalles),
        )
        .order_by(MonzaContCompra.id.desc())
        .offset((page - 1) * page_size).limit(page_size).all()
    )
    return {
        "compras": [_serialize_compra(c) for c in rows],
        "total": int(total), "page": page, "page_size": page_size,
        "antiguedad": _antiguedad(db, tipo=tipo, estado_pago=estado_pago,
                                  categoria=categoria, periodo=periodo, q=q, proveedor_id=proveedor_id),
    }


# ─── Crear compra (con opción de pagarla al tiro) ──────────────────────────────
@router.post("")
def crear_compra(
    payload: CompraCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if payload.tipo_gasto not in TIPOS_GASTO:
        raise HTTPException(400, f"tipo_gasto inválido: {payload.tipo_gasto}")

    acreedor = payload.acreedor
    if payload.proveedor_id and not acreedor:
        prov = db.query(MonzaProveedor).filter(MonzaProveedor.id == payload.proveedor_id).first()
        if prov:
            acreedor = prov.nombre

    if payload.numero_documento:
        dup = (db.query(MonzaContCompra).filter(
            MonzaContCompra.proveedor_rut == payload.proveedor_rut,
            MonzaContCompra.numero_documento == payload.numero_documento,
            MonzaContCompra.anulado.is_(False),
        ).first())
        if dup:
            raise HTTPException(409, f"Ya existe una compra con documento {payload.numero_documento} para este proveedor")

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
        cta = db.query(MonzaContPlanCuenta).filter(
            MonzaContPlanCuenta.id == cuenta_id, MonzaContPlanCuenta.activa.is_(True)).first()
        if not cta:
            raise HTTPException(400, "Cuenta contable inválida")
        if not cta.imputable:
            raise HTTPException(400, f"La cuenta {cta.codigo} {cta.nombre} es de título/mayor: impute a una cuenta hoja")
        if cta.requiere_auxiliar and not (payload.proveedor_rut or "").strip():
            raise HTTPException(400, f"La cuenta {cta.codigo} {cta.nombre} exige RUT del proveedor (auxiliar)")
    else:
        cod = cuenta_default_codigo(payload.origen, payload.tipo_gasto)
        cta = (db.query(MonzaContPlanCuenta)
               .filter(MonzaContPlanCuenta.codigo == cod).first() if cod else None)
        cuenta_id = cta.id if cta else None

    # Punteros a embarque: si vienen, deben existir y ser coherentes (trazabilidad).
    if payload.emb_pricing_gasto_id:
        # Lock del gasto: serializa dos registros concurrentes del MISMO gasto (el 2°
        # espera el commit del 1° y su chequeo de duplicado ve la compra ya creada → 409).
        g = (db.query(MonzaEmbPricingGasto)
             .filter(MonzaEmbPricingGasto.id == payload.emb_pricing_gasto_id)
             .with_for_update().first())
        if not g:
            raise HTTPException(400, "emb_pricing_gasto_id no existe")
        dup_g = db.query(MonzaContCompra).filter(
            MonzaContCompra.emb_pricing_gasto_id == payload.emb_pricing_gasto_id,
            MonzaContCompra.anulado.is_(False)).first()
        if dup_g:
            raise HTTPException(409, "Ese gasto de embarque ya está registrado como compra")
    if payload.embarque_id:
        if not db.query(MonzaEmbarque).filter(MonzaEmbarque.id == payload.embarque_id).first():
            raise HTTPException(400, "embarque_id no existe")

    compra = MonzaContCompra(
        origen=(payload.origen or "MANUAL").upper(),
        tipo_gasto=payload.tipo_gasto, categoria=payload.categoria,
        cuenta_contable_id=cuenta_id, es_anticipo=payload.es_anticipo,
        proveedor_id=payload.proveedor_id, acreedor=acreedor, proveedor_rut=payload.proveedor_rut,
        fecha=fecha, referencia=payload.referencia, descripcion=payload.descripcion,
        numero_documento=payload.numero_documento, tipo_doc=payload.tipo_doc or "factura",
        moneda=moneda, tc=tc, monto_neto=neto, iva=iva, monto_total=total, monto_total_clp=total_clp,
        condicion_pago=condicion, plazo_dias=payload.plazo_dias, fecha_vencimiento=fecha_venc,
        embarque_id=payload.embarque_id, emb_pricing_gasto_id=payload.emb_pricing_gasto_id,
        observaciones=payload.observaciones, usuario_id=getattr(current_user, "id", None),
    )
    try:
        db.add(compra)
        db.flush()
        uid = getattr(current_user, "id", None)
        pago = payload.pago
        # Para compras en moneda extranjera, el TC de la compra viaja al egreso/detalle
        # (tc_aplicado): sin él, la diferencia de cambio NIC 21 nace incalculable.
        moneda_pago = compra.moneda if (compra.moneda or "CLP") != "CLP" else "CLP"
        tc_pago = _f(compra.tc) if moneda_pago != "CLP" else 1.0
        if pago is None and condicion == "contado" and total_clp > 0:
            # Contado sin pago explícito: egreso por el total el mismo día.
            _crear_egreso(db, detalles=[(compra.id, total_clp)], usuario_id=uid,
                          fecha=fecha, medio="transferencia", beneficiario=acreedor,
                          beneficiario_rut=payload.proveedor_rut, fecha_mov_bancario=fecha,
                          moneda=moneda_pago, tc=tc_pago)
        elif pago is not None:
            monto_pago = _f(pago.monto_clp) if pago.monto_clp is not None else total_clp
            if monto_pago > total_clp + TOL_PAGO:
                raise HTTPException(400, f"El pago excede el total de la compra ({total_clp:.0f})")
            if monto_pago > 0:
                pfecha = _fecha(pago.fecha, "pago.fecha") or fecha
                _crear_egreso(db, detalles=[(compra.id, monto_pago)], usuario_id=uid,
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
        if "uq_monza_cont_compra_prov_doc" in str(getattr(e, "orig", e)):
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
    hoy = date.today()
    base = db.query(MonzaContCompra).filter(MonzaContCompra.anulado.is_(False))
    base = _apply_filtros(base, tipo=tipo, periodo=periodo)
    s = MonzaContCompra.saldo_clp
    n, total, pagado, por_pagar, vencido = base.with_entities(
        func.count(MonzaContCompra.id),
        func.coalesce(func.sum(MonzaContCompra.monto_total_clp), 0),
        func.coalesce(func.sum(MonzaContCompra.monto_pagado_clp), 0),
        func.coalesce(func.sum(case((s > TOL, s), else_=0)), 0),
        func.coalesce(func.sum(case(
            (and_(s > TOL, MonzaContCompra.fecha_vencimiento.isnot(None), MonzaContCompra.fecha_vencimiento < hoy), s),
            else_=0)), 0),
    ).one()
    por_tipo = {t: 0.0 for t in TIPOS_GASTO}
    for t, v in (base.with_entities(MonzaContCompra.tipo_gasto, func.coalesce(func.sum(MonzaContCompra.monto_total_clp), 0))
                     .group_by(MonzaContCompra.tipo_gasto).all()):
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
    provs = (db.query(MonzaProveedor)
             .filter(MonzaProveedor.activo.is_(True))
             .order_by(MonzaProveedor.nombre.asc()).all())
    cuentas = (db.query(MonzaContPlanCuenta)
               .filter(MonzaContPlanCuenta.imputable.is_(True), MonzaContPlanCuenta.activa.is_(True))
               .order_by(MonzaContPlanCuenta.orden.asc()).all())
    cod_to_id = {c.codigo: c.id for c in cuentas}
    cuenta_default_por_tipo = {
        f"{origen}|{tipo}": cod_to_id.get(cod) for (origen, tipo), cod in CUENTA_DEFAULT_CODIGO.items()
    }
    return {
        "tipos_gasto": [{"value": t, "label": TIPO_GASTO_LABEL[t]} for t in TIPOS_GASTO],
        "estados_pago": ESTADOS_PAGO, "categorias_sugeridas": CATEGORIAS_SUGERIDAS,
        "medios_pago": MEDIOS_PAGO, "iva_rate": IVA_RATE,
        "proveedores": [{"id": p.id, "nombre": p.nombre, "moneda": None, "pais": p.pais} for p in provs],
        "plan_cuentas": [{"id": c.id, "codigo": c.codigo, "nombre": c.nombre, "clase": c.clase,
                          "grupo": c.grupo, "requiere_auxiliar": bool(c.requiere_auxiliar)} for c in cuentas],
        "cuenta_default_por_tipo": cuenta_default_por_tipo,
    }


# ─── Overlay solo lectura: gastos de embarque (se reflejan automáticamente) ────
@router.get("/costos-embarque")
def costos_embarque(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Gastos anotados en Embarques Pricing (monza_emb_pricing_gasto), en vivo.
    No se digitan de nuevo: acá se ven reflejados como costos por ventas (cogs)."""
    rows = (db.query(MonzaEmbPricingGasto, MonzaEmbarque)
            .join(MonzaEmbPricing, MonzaEmbPricing.id == MonzaEmbPricingGasto.pricing_id)
            .join(MonzaEmbarque, MonzaEmbarque.id == MonzaEmbPricing.embarque_id)
            .order_by(MonzaEmbarque.id.desc(), MonzaEmbPricingGasto.orden.asc()).all())
    emb_ids = {e.id for _g, e in rows}
    # Proveedor del embarque: vía sus ítems → OC proveedor (1ª que aparezca).
    prov_by_emb: dict = {}
    if emb_ids:
        for emb_id, prov_nombre in (
            db.query(MonzaEmbarqueItem.embarque_id, MonzaOcProveedor.proveedor_nombre)
            .join(MonzaCotizacionItem, MonzaCotizacionItem.id == MonzaEmbarqueItem.item_id)
            .join(MonzaOcProveedor, MonzaOcProveedor.id == MonzaCotizacionItem.oc_proveedor_id)
            .filter(MonzaEmbarqueItem.embarque_id.in_(emb_ids)).all()
        ):
            prov_by_emb.setdefault(emb_id, prov_nombre)
    # Gastos ya registrados como compra (para marcarlos y no duplicar).
    gasto_ids = [g.id for g, _e in rows]
    registrados = {}
    if gasto_ids:
        for gid, cid in (db.query(MonzaContCompra.emb_pricing_gasto_id, MonzaContCompra.id)
                         .filter(MonzaContCompra.emb_pricing_gasto_id.in_(gasto_ids),
                                 MonzaContCompra.anulado.is_(False)).all()):
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


# ─── Egresos (pago consolidado / listado) ──────────────────────────────────────
@router.get("/egresos")
def listar_egresos(
    conciliado: Optional[bool] = None, q: Optional[str] = None,
    page: int = 1, page_size: int = PAGE_SIZE_DEFAULT,
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user),
):
    page = max(1, int(page))
    page_size = min(max(1, int(page_size)), PAGE_SIZE_MAX)
    base = db.query(MonzaContEgreso)
    if conciliado is not None:
        base = base.filter(MonzaContEgreso.conciliado.is_(bool(conciliado)))
    if q:
        like = f"%{q}%"
        base = base.filter(or_(MonzaContEgreso.beneficiario.ilike(like),
                               MonzaContEgreso.numero_operacion.ilike(like), MonzaContEgreso.banco.ilike(like)))
    total = base.count()
    rows = (base.options(selectinload(MonzaContEgreso.detalles), selectinload(MonzaContEgreso.cuenta_origen))
                .order_by(MonzaContEgreso.id.desc()).offset((page - 1) * page_size).limit(page_size).all())
    return {"egresos": [serialize_egreso(e) for e in rows], "total": int(total),
            "page": page, "page_size": page_size}


@router.post("/egresos")
def crear_egreso_consolidado(
    payload: EgresoCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Comprobante de Egreso que paga VARIAS compras en una sola salida de dinero."""
    fecha = _fecha(payload.fecha, "fecha") or date.today()
    try:
        egreso = _crear_egreso(
            db, usuario_id=getattr(current_user, "id", None),
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
    Rechaza si está conciliado con el banco."""
    egreso = (db.query(MonzaContEgreso)
              .filter(MonzaContEgreso.id == egreso_id)
              .with_for_update().first())
    if not egreso:
        raise HTTPException(404, "Egreso no encontrado")
    if egreso.conciliado:
        raise HTTPException(409, "El egreso está conciliado con el banco; desconcílielo en Tesorería primero")
    compra_ids = [d.compra_id for d in egreso.detalles]
    db.delete(egreso)
    db.flush()
    for cid in compra_ids:
        compra = (db.query(MonzaContCompra)
                  .filter(MonzaContCompra.id == cid)
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
    Lock de fila: serializa ediciones concurrentes (evita lost-update en conciliación)."""
    egreso = (db.query(MonzaContEgreso)
              .filter(MonzaContEgreso.id == egreso_id)
              .with_for_update().first())
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
    # Carga ansiosa: el serializador recorre pagos → egreso (evita N+1 en lectura).
    compra = (
        db.query(MonzaContCompra)
        .options(
            selectinload(MonzaContCompra.cuenta),
            selectinload(MonzaContCompra.egreso_detalles).selectinload(MonzaContEgresoDetalle.egreso),
        )
        .filter(MonzaContCompra.id == compra_id)
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
    # lock=True: el chequeo de anulado se hace sobre estado actual (no snapshot stale);
    # _crear_egreso re-verifica bajo su propio lock, pero así el patrón es consistente.
    compra = _get_compra(db, compra_id, lock=True)
    if compra.anulado:
        raise HTTPException(400, "La compra está anulada")
    pfecha = _fecha(payload.fecha, "fecha") or date.today()
    # compra en moneda extranjera → el TC viaja al egreso/detalle (NIC 21)
    moneda_pago = compra.moneda if (compra.moneda or "CLP") != "CLP" else "CLP"
    tc_pago = _f(compra.tc) if moneda_pago != "CLP" else 1.0
    try:
        _crear_egreso(
            db, usuario_id=getattr(current_user, "id", None),
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


def _detalle_de_compra(db, compra_id, pago_id) -> MonzaContEgresoDetalle:
    """pago_id (lo que ve el front) = id de la asignación (detalle de egreso) de la compra."""
    d = (db.query(MonzaContEgresoDetalle)
         .filter(MonzaContEgresoDetalle.id == pago_id, MonzaContEgresoDetalle.compra_id == compra_id)
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
    d = _detalle_de_compra(db, compra_id, pago_id)
    egreso = (db.query(MonzaContEgreso)
              .filter(MonzaContEgreso.id == d.egreso_id)
              .with_for_update().first())
    if not egreso:
        raise HTTPException(404, "Egreso no encontrado")
    if egreso.conciliado:
        raise HTTPException(409, "El pago está conciliado: la cartola es la fuente de verdad; desconcílielo en Tesorería para editar estos datos")
    if payload.fecha_mov_bancario is not None:
        egreso.fecha_mov_bancario = _fecha(payload.fecha_mov_bancario, "fecha_mov_bancario")
    if payload.referencia_bancaria is not None:
        egreso.referencia_bancaria = payload.referencia_bancaria or None
    db.commit()
    return _serialize_compra(_get_compra(db, compra_id))


@router.delete("/{compra_id}/pagos/{pago_id}")
def eliminar_pago(
    compra_id: int, pago_id: int,
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user),
):
    """Revierte el pago: borra el EGRESO completo al que pertenece (puede afectar a varias
    compras si era consolidado). Rechaza si el egreso está conciliado."""
    d = _detalle_de_compra(db, compra_id, pago_id)
    egreso = db.query(MonzaContEgreso).filter(MonzaContEgreso.id == d.egreso_id).with_for_update().first()
    if egreso and egreso.conciliado:
        raise HTTPException(409, "El pago está conciliado con el banco; desconcílielo en Tesorería primero")
    compra_ids = [x.compra_id for x in egreso.detalles]
    db.delete(egreso)
    db.flush()
    for cid in compra_ids:
        compra = (db.query(MonzaContCompra)
                  .filter(MonzaContCompra.id == cid)
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
    compra = _get_compra(db, compra_id, lock=True)
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
    # lock=True: el chequeo "sin pagos" debe ser sobre estado actual — sin lock, un pago
    # concurrente podría colarse entre el chequeo y el DELETE (y el cascade lo huerfanaría).
    compra = _get_compra(db, compra_id, lock=True)
    if compra.egreso_detalles:
        raise HTTPException(409, "La compra tiene pagos registrados; revierta los pagos o use Anular")
    db.delete(compra)
    db.commit()
    return {"ok": True}
