"""API del módulo Tesorería MonzaParts.

Prefijo: /api/monza/tesoreria (montado sin prefix; el router ya lo trae).
SOLO MonzaParts: candado require_empresa("automotriz") a nivel de router.

4 sub-áreas (cada una con su bloque de endpoints, en este orden):
  1. APROBACIONES  — cola de adelantos 50% informados por Comercial; Tesorería DA LA
     ORDEN (escribe monza_cont_adelanto + adelanto_verificado=1, misma regla de negocio
     que el endpoint histórico de Ventas-Contab; el cortafuego de Abastecimiento no
     cambia). Incluye sugerencia de abono en cartola si ya llegó la plata.
  2. POR PAGAR / APROBAR PAGOS — cola de compras con saldo (registradas en Compras/CxP
     con pago futuro o parcial). Tesorería DA LA ORDEN del pago: crea el Comprobante
     de Egreso con la MISMA regla de negocio que Compras (reusa `_crear_egreso` de
     monza_compras_contab: locks anti doble-pago, tope por saldo, recálculo).
  3. CONCILIACIÓN  — espejo de la Tesorería de Grupo AM sobre tablas monza_tes_*:
     cuentas, cartolas (CSV/XLSX) con anti-duplicados, movimientos, sugerencias y cruce
     1:1 exacto (±TOL):
       · cargo ↔ egreso de Compras (marca egreso.conciliado)
       · abono ↔ adelanto 50% (plus Monza) O ↔ cobranza de Facturas y Cobranzas (el
         "conciliado" de adelanto/cobranza se DERIVA del enlace).
  4. FLUJO DE CAJA — proyección NIC 7 de salidas (Compras por pagar) vs entradas
     (facturas por cobrar + adelantos por aprobar), solo lectura.
"""
from collections import Counter
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import IntegrityError

from database import get_db
from auth import get_current_user
from empresa_guard import require_empresa
from models.models import User
from monza_models import MonzaCotizacion
# Dependencias de solo lectura/escritura acotada sobre tablas de otros módulos Monza
# (documentadas): el adelanto y la cobranza viven en monza_contabilidad; el egreso en
# monza_compras_contab. La orden de pago de Tesorería reusa la regla de negocio de
# Compras (`_crear_egreso`: una sola fuente de verdad).
from monza_contabilidad.models import MonzaContAdelanto, MonzaContFacturaCliente, MonzaContCobranza, MonzaContFactoring
from monza_contabilidad.service import estado_adelanto, TOL_PAGO, TOL as TOL_CONTAB, _f as _fc, MEDIO_ADELANTO
from monza_compras_contab.models import MonzaContCompra, MonzaContEgreso, MonzaContEgresoDetalle
from monza_compras_contab.schemas import EgresoCreate
from monza_compras_contab.router import _crear_egreso
from monza_compras_contab.service import (
    serialize_egreso, parse_date_estricta, _estado_pago as _estado_pago_compra,
)

from .models import (
    MonzaTesCuentaBancaria, MonzaTesCartola, MonzaTesMovimiento, MonzaTesConciliacion,
    MonzaTesConciliacionIngreso,
)
from .schemas import CuentaIn, MovimientoIn, ConciliarIn, AprobarAdelantoIn
from .service import (
    TOL, TIPOS_MOV, BANCOS_SUGERIDOS, FLUJO_BUCKETS,
    _f, _parse_date, bucket_de, parse_cartola,
    serialize_cuenta, serialize_cartola, serialize_movimiento,
)

router = APIRouter(
    prefix="/api/monza/tesoreria",
    tags=["monza-tesoreria"],
    dependencies=[Depends(require_empresa("automotriz"))],
)

PAGE_SIZE_DEFAULT = 100
PAGE_SIZE_MAX = 500
# Tope de tamaño para la cartola subida (una cartola real pesa KBs; 10 MB es holgado
# y evita que un archivo gigante agote la memoria del parser).
MAX_CARTOLA_BYTES = 10 * 1024 * 1024
EXTENSIONES_CARTOLA = (".csv", ".xlsx")

# Estados de cotización que cuentan como VENTA. Mantener en sync con
# monza_contabilidad/router.py (ESTADOS_VENTA).
ESTADOS_VENTA = ("vendida", "despachado")


# ─── Helpers ───────────────────────────────────────────────────────────────────
def _fecha(s, campo: str):
    """Fecha estricta del payload: explícita pero no parseable → 400 (no se silencia)."""
    try:
        return parse_date_estricta(s, campo=campo)
    except ValueError as e:
        raise HTTPException(400, str(e))


def _cuenta_or_404(db, cuenta_id, *, lock=False) -> MonzaTesCuentaBancaria:
    q = db.query(MonzaTesCuentaBancaria).filter(MonzaTesCuentaBancaria.id == cuenta_id)
    c = (q.with_for_update().first() if lock else q.first())
    if not c:
        raise HTTPException(404, "Cuenta bancaria no encontrada")
    return c


def _mov_or_404(db, mov_id, *, lock=False) -> MonzaTesMovimiento:
    q = db.query(MonzaTesMovimiento).filter(MonzaTesMovimiento.id == mov_id)
    m = (q.with_for_update().first() if lock else q.first())
    if not m:
        raise HTTPException(404, "Movimiento no encontrado")
    return m


def _egreso_summaries(db, egresos: list) -> dict:
    """{egreso_id: resumen} para una LISTA de egresos en una sola query (sin N+1).
    Cada resumen trae las compras que paga ese egreso."""
    if not egresos:
        return {}
    ids = [e.id for e in egresos]
    rows = (db.query(MonzaContEgresoDetalle, MonzaContCompra)
            .join(MonzaContCompra, MonzaContCompra.id == MonzaContEgresoDetalle.compra_id)
            .filter(MonzaContEgresoDetalle.egreso_id.in_(ids)).all())
    by_egreso: dict = {}
    for d, c in rows:
        by_egreso.setdefault(d.egreso_id, []).append((d, c))
    out = {}
    for e in egresos:
        det = by_egreso.get(e.id, [])
        out[e.id] = {
            "clase": "egreso",
            "egreso_id": e.id,
            "fecha": e.fecha.isoformat() if e.fecha else None,
            "monto_total_clp": _f(e.monto_total_clp),
            "medio": e.medio, "banco": e.banco,
            "numero_operacion": e.numero_operacion, "beneficiario": e.beneficiario,
            "n_compras": len(det),
            "compras": [
                {"compra_id": c.id, "acreedor": c.acreedor, "numero_documento": c.numero_documento,
                 "monto_clp": _f(d.monto_clp), "categoria": c.categoria, "tipo_gasto": c.tipo_gasto}
                for d, c in det
            ],
        }
    return out


def _adelanto_summaries(db, adelantos: list) -> dict:
    """{adelanto_id: resumen} con su venta (cotización + cliente) en una sola query."""
    if not adelantos:
        return {}
    cot_ids = {a.cotizacion_id for a in adelantos}
    cots = {c.id: c for c in
            db.query(MonzaCotizacion).options(joinedload(MonzaCotizacion.cliente))
            .filter(MonzaCotizacion.id.in_(cot_ids)).all()}
    out = {}
    for a in adelantos:
        cot = cots.get(a.cotizacion_id)
        out[a.id] = {
            "clase": "adelanto",
            "adelanto_id": a.id,
            "cotizacion_id": a.cotizacion_id,
            "numero_cotizacion": cot.numero if cot else None,
            "cliente": (cot.cliente.nombre if cot and cot.cliente else None),
            "monto": _f(a.monto),
            "fecha_pago": a.fecha_pago.isoformat() if a.fecha_pago else None,
            "banco": a.banco, "numero_operacion": a.numero_operacion,
        }
    return out


def _cobranza_summaries(db, pares: list) -> dict:
    """{cobranza_id: resumen} para una lista de tuplas (MonzaContCobranza, MonzaContFacturaCliente)."""
    out = {}
    for c, f in pares:
        out[c.id] = {
            "clase": "cobranza",
            "cobranza_id": c.id,
            "factura_id": f.id,
            "numero_factura": f.numero_factura,
            "fecha": c.fecha.isoformat() if c.fecha else None,
            "monto": _f(c.monto),
            "medio": c.medio, "banco": c.banco,
            "numero_operacion": c.numero_operacion,
        }
    return out


def _clave_mov(fecha, tipo, monto, referencia, glosa, saldo) -> tuple:
    """Clave de identidad 'suave' de un movimiento bancario, para detectar duplicados
    al reimportar una cartola. Incluye el saldo de la línea: dos transferencias
    idénticas el mismo día se distinguen por el saldo corrido del banco."""
    return (
        str(fecha or ""), tipo, round(_f(monto), 2),
        (referencia or "").strip(), (glosa or "").strip(),
        round(_f(saldo), 2) if saldo is not None else None,
    )


def _solo_cuentas_clp(cuenta) -> None:
    """La conciliación compara montos contra CLP (egresos/adelantos/cobranzas): en
    cuentas en otra moneda los montos no son comparables → se rechaza con mensaje claro."""
    if cuenta is not None and (cuenta.moneda or "CLP") != "CLP":
        raise HTTPException(400, f"La cuenta está en {cuenta.moneda}: la conciliación automática "
                                 "solo está disponible para cuentas en CLP (fase futura)")


def _destinos_for_movs(db, movs) -> dict:
    """{mov_id: resumen del destino} para movimientos conciliados (1er enlace):
    egreso o adelanto (monza_tes_conciliacion) o cobranza (monza_tes_conciliacion_ingreso)."""
    mov_ids = [m.id for m in movs if m.conciliado]
    if not mov_ids:
        return {}
    links = (db.query(MonzaTesConciliacion)
             .filter(MonzaTesConciliacion.movimiento_id.in_(mov_ids))
             .order_by(MonzaTesConciliacion.id.asc()).all())
    first = {}
    for lk in links:
        first.setdefault(lk.movimiento_id, lk)
    eg_ids = {lk.egreso_id for lk in first.values() if lk.egreso_id}
    ad_ids = {lk.adelanto_id for lk in first.values() if lk.adelanto_id}
    emap = _egreso_summaries(
        db, db.query(MonzaContEgreso).filter(MonzaContEgreso.id.in_(eg_ids)).all()) if eg_ids else {}
    amap = _adelanto_summaries(
        db, db.query(MonzaContAdelanto).filter(MonzaContAdelanto.id.in_(ad_ids)).all()) if ad_ids else {}
    out = {}
    for mov_id, lk in first.items():
        if lk.egreso_id and lk.egreso_id in emap:
            out[mov_id] = emap[lk.egreso_id]
        elif lk.adelanto_id and lk.adelanto_id in amap:
            out[mov_id] = amap[lk.adelanto_id]
    # abonos ↔ cobranzas (tabla nueva monza_tes_conciliacion_ingreso)
    links_i = (db.query(MonzaTesConciliacionIngreso)
               .filter(MonzaTesConciliacionIngreso.movimiento_id.in_(mov_ids))
               .order_by(MonzaTesConciliacionIngreso.id.asc()).all())
    first_i = {}
    for lk in links_i:
        first_i.setdefault(lk.movimiento_id, lk.cobranza_id)
    if first_i:
        pares = (db.query(MonzaContCobranza, MonzaContFacturaCliente)
                 .join(MonzaContFacturaCliente, MonzaContFacturaCliente.id == MonzaContCobranza.factura_id)
                 .filter(MonzaContCobranza.id.in_(set(first_i.values()))).all())
        cmap = _cobranza_summaries(db, pares)
        for mov_id, cob_id in first_i.items():
            if cob_id in cmap and mov_id not in out:
                out[mov_id] = cmap[cob_id]
    return out


def _adelantos_conciliados_ids(db, adelanto_ids) -> set:
    """IDs de adelantos que YA tienen un enlace de conciliación (estado derivado)."""
    if not adelanto_ids:
        return set()
    rows = (db.query(MonzaTesConciliacion.adelanto_id)
            .filter(MonzaTesConciliacion.adelanto_id.in_(adelanto_ids)).all())
    return {r[0] for r in rows}


def _cobranzas_conciliadas_ids(db, cobranza_ids) -> set:
    """IDs de cobranzas que YA tienen enlace de conciliación (estado derivado)."""
    if not cobranza_ids:
        return set()
    rows = (db.query(MonzaTesConciliacionIngreso.cobranza_id)
            .filter(MonzaTesConciliacionIngreso.cobranza_id.in_(cobranza_ids)).all())
    return {r[0] for r in rows}


# ═══ 1. APROBACIONES (adelantos 50% → la orden la da Tesorería) ═════════════════
@router.get("/aprobaciones")
def listar_aprobaciones(db: Session = Depends(get_db),
                        current_user: User = Depends(get_current_user)):
    """Cola de aprobación de adelantos.
      · por_aprobar: ventas con pct_adelanto>0 aún SIN la orden de Tesorería, con el
        monto sugerido y, si ya llegó la plata, el abono de cartola que calza.
      · aprobadas: historial (adelanto registrado), con su estado de conciliación
        bancaria derivado del enlace en monza_tes_conciliacion."""
    cots = (db.query(MonzaCotizacion)
            .options(joinedload(MonzaCotizacion.cliente))
            .filter(MonzaCotizacion.estado.in_(ESTADOS_VENTA), MonzaCotizacion.pct_adelanto > 0)
            .order_by(MonzaCotizacion.id.desc()).all())
    adelantos = {a.cotizacion_id: a for a in
                 db.query(MonzaContAdelanto)
                 .filter(MonzaContAdelanto.cotizacion_id.in_([c.id for c in cots])).all()} if cots else {}
    conciliados = _adelantos_conciliados_ids(db, [a.id for a in adelantos.values()])

    # Abonos de cartola NO conciliados (para sugerir el match del adelanto que llega).
    abonos = (db.query(MonzaTesMovimiento)
              .filter(MonzaTesMovimiento.tipo == "abono", MonzaTesMovimiento.conciliado.is_(False))
              .order_by(MonzaTesMovimiento.fecha.desc()).all())

    por_aprobar, aprobadas = [], []
    for cot in cots:
        adel = adelantos.get(cot.id)
        base = {
            "cotizacion_id": cot.id,
            "numero_cotizacion": cot.numero,
            "cliente": cot.cliente.nombre if cot.cliente else None,
            "estado_venta": cot.estado,
            "fecha_venta": cot.fecha_venta.isoformat() if getattr(cot, "fecha_venta", None) else None,
            "pct_adelanto": int(cot.pct_adelanto or 0),
            "total_venta_clp": _f(cot.total_bruto),
            **estado_adelanto(cot, adel),
        }
        if adel is None:
            sugerido = round(_f(cot.total_bruto) * int(cot.pct_adelanto or 0) / 100.0, 0)
            # Sugerencia: abono pendiente cuyo monto calza con el sugerido (±TOL).
            match = next((m for m in abonos if abs(_f(m.monto) - sugerido) <= TOL), None)
            base["monto_sugerido_clp"] = sugerido
            base["abono_sugerido"] = ({
                "movimiento_id": match.id,
                "fecha": match.fecha.isoformat() if match.fecha else None,
                "monto": _f(match.monto), "glosa": match.glosa,
            } if match else None)
            por_aprobar.append(base)
        else:
            base["adelanto"] = {
                "adelanto_id": adel.id,
                "monto": _f(adel.monto),
                "monto_aplicado": _f(adel.monto_aplicado),
                "fecha_pago": adel.fecha_pago.isoformat() if adel.fecha_pago else None,
                "banco": adel.banco, "numero_operacion": adel.numero_operacion,
                "conciliado_banco": adel.id in conciliados,
            }
            aprobadas.append(base)
    return {"por_aprobar": por_aprobar, "aprobadas": aprobadas,
            "n_por_aprobar": len(por_aprobar)}


@router.post("/aprobaciones/{cot_id}/aprobar")
def aprobar_adelanto(cot_id: int, payload: AprobarAdelantoIn,
                     db: Session = Depends(get_db),
                     current_user: User = Depends(get_current_user)):
    """TESORERÍA da la orden: registra el adelanto recibido (monto/fecha/banco/N° op)
    y marca adelanto_verificado=1 → Abastecimiento queda destrabado (cortafuego).

    MISMA regla de negocio que POST /api/monza/contabilidad/ventas/{id}/adelanto/verificar
    (monza_contabilidad/router.py); mantener ambas en sync. Lock de la cotización:
    serializa aprobaciones concurrentes (el UNIQUE por cotización es la última defensa)."""
    cot = (db.query(MonzaCotizacion)
           .filter(MonzaCotizacion.id == cot_id)
           .with_for_update(of=MonzaCotizacion)
           .first())
    if not cot:
        raise HTTPException(404, "Venta (cotización) no encontrada")
    if cot.estado not in ESTADOS_VENTA:
        raise HTTPException(400, "La cotización debe estar vendida para aprobar el adelanto")
    if int(getattr(cot, "pct_adelanto", 0) or 0) <= 0:
        raise HTTPException(400, "Esta venta no tiene un adelanto informado por Comercial")
    if payload.monto > _fc(cot.total_bruto) + TOL_PAGO:
        raise HTTPException(400, f"El monto del adelanto no puede exceder el total de la venta ({_fc(cot.total_bruto):.0f})")
    adel = (db.query(MonzaContAdelanto)
            .filter(MonzaContAdelanto.cotizacion_id == cot_id).first())
    if adel is not None and _fc(adel.monto_aplicado) > TOL_CONTAB:
        raise HTTPException(409, f"El adelanto ya fue aplicado a una factura (aplicado {_fc(adel.monto_aplicado):.0f}); revierta esa cobranza antes de modificarlo")
    # GAP DE INTEGRIDAD: si el adelanto ya está conciliado con un abono del banco,
    # re-aprobarlo (editar el monto) dejaría el cruce bancario apuntando a otro monto.
    if adel is not None and _adelantos_conciliados_ids(db, [adel.id]):
        raise HTTPException(409, "El adelanto ya está conciliado con un abono del banco; "
                                 "desconcilie el abono en Tesorería antes de modificar el adelanto")
    if adel is None:
        adel = MonzaContAdelanto(cotizacion_id=cot_id)
        db.add(adel)
    adel.monto = payload.monto
    adel.fecha_pago = _parse_date(payload.fecha_pago) or date.today()
    adel.banco = payload.banco
    adel.numero_operacion = payload.numero_operacion
    adel.observaciones = payload.observaciones
    adel.usuario_id = getattr(current_user, "id", None)
    cot.adelanto_verificado = 1
    db.commit()
    db.refresh(cot)
    db.refresh(adel)
    return estado_adelanto(cot, adel)


# ═══ 2. POR PAGAR / APROBAR PAGOS (la orden del pago la da Tesorería) ═══════════
@router.get("/por-pagar")
def por_pagar(q: Optional[str] = None, page: int = 1, page_size: int = PAGE_SIZE_DEFAULT,
              db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Cola de aprobación de pagos: compras ACTIVAS con saldo pendiente (registradas en
    Compras con pago futuro o parcial), ordenadas por vencimiento. Desde aquí Tesorería
    aprueba y registra el pago (POST /pagos)."""
    page = max(1, int(page))
    page_size = min(max(1, int(page_size)), PAGE_SIZE_MAX)
    base = (db.query(MonzaContCompra)
            .filter(MonzaContCompra.anulado.is_(False), MonzaContCompra.saldo_clp > TOL))
    if q:
        like = f"%{q}%"
        base = base.filter(or_(
            MonzaContCompra.acreedor.ilike(like), MonzaContCompra.numero_documento.ilike(like),
            MonzaContCompra.proveedor_rut.ilike(like), MonzaContCompra.categoria.ilike(like)))
    total = base.count()
    # Vencidas y por vencer primero; sin fecha al final (NULLS LAST portable MySQL/SQLite).
    rows = (base.order_by(MonzaContCompra.fecha_vencimiento.is_(None).asc(),
                          MonzaContCompra.fecha_vencimiento.asc(), MonzaContCompra.id.asc())
                .offset((page - 1) * page_size).limit(page_size).all())
    hoy = date.today()
    buckets = {b: {"monto": 0.0, "n": 0} for b in FLUJO_BUCKETS}
    for venc, saldo in base.with_entities(MonzaContCompra.fecha_vencimiento, MonzaContCompra.saldo_clp).all():
        b = bucket_de(venc, hoy)
        buckets[b]["monto"] += _f(saldo)
        buckets[b]["n"] += 1
    for b in FLUJO_BUCKETS:
        buckets[b]["monto"] = round(buckets[b]["monto"], 0)
    return {
        "compras": [{
            "compra_id": c.id, "acreedor": c.acreedor, "proveedor_rut": c.proveedor_rut,
            "numero_documento": c.numero_documento, "categoria": c.categoria,
            "tipo_gasto": c.tipo_gasto, "condicion_pago": c.condicion_pago,
            "fecha": c.fecha.isoformat() if c.fecha else None,
            "fecha_vencimiento": c.fecha_vencimiento.isoformat() if c.fecha_vencimiento else None,
            "bucket": bucket_de(c.fecha_vencimiento, hoy),
            "monto_total_clp": _f(c.monto_total_clp),
            "monto_pagado_clp": _f(c.monto_pagado_clp),
            "saldo_clp": _f(c.saldo_clp),
            # estado EN VIVO (igual que Compras): el persistido no transiciona a
            # 'vencido' con el paso del tiempo
            "estado_pago": _estado_pago_compra(c, _f(c.monto_pagado_clp), _f(c.saldo_clp)),
        } for c in rows],
        "total": int(total), "page": page, "page_size": page_size,
        "buckets": buckets,
    }


@router.post("/pagos")
def aprobar_pago(payload: EgresoCreate, db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    """TESORERÍA da la orden: crea el Comprobante de Egreso que paga 1..N compras
    (parcial o total). MISMA regla de negocio que POST /api/monza/compras-contab/egresos
    (reusa `_crear_egreso`: locks anti doble-pago, tope por saldo, recálculo)."""
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


# ═══ 3. CONCILIACIÓN BANCARIA ═══════════════════════════════════════════════════
# ─── Cuentas bancarias ─────────────────────────────────────────────────────────
@router.get("/cuentas")
def listar_cuentas(incluir_inactivas: bool = False, db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_user)):
    q = db.query(MonzaTesCuentaBancaria)
    if not incluir_inactivas:
        q = q.filter(MonzaTesCuentaBancaria.activo.is_(True))
    cuentas = q.order_by(MonzaTesCuentaBancaria.banco.asc(), MonzaTesCuentaBancaria.id.asc()).all()
    return {"cuentas": [serialize_cuenta(c) for c in cuentas], "bancos_sugeridos": BANCOS_SUGERIDOS}


@router.post("/cuentas")
def crear_cuenta(payload: CuentaIn, db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    cuenta = MonzaTesCuentaBancaria(
        banco=payload.banco, nombre=payload.nombre, numero_cuenta=payload.numero_cuenta,
        moneda=payload.moneda, activo=payload.activo, observaciones=payload.observaciones)
    db.add(cuenta)
    db.commit()
    db.refresh(cuenta)
    return serialize_cuenta(cuenta)


@router.put("/cuentas/{cuenta_id}")
def actualizar_cuenta(cuenta_id: int, payload: CuentaIn, db: Session = Depends(get_db),
                      current_user: User = Depends(get_current_user)):
    cuenta = _cuenta_or_404(db, cuenta_id)
    cuenta.banco = payload.banco
    cuenta.nombre = payload.nombre
    cuenta.numero_cuenta = payload.numero_cuenta
    cuenta.moneda = payload.moneda
    cuenta.activo = payload.activo
    cuenta.observaciones = payload.observaciones
    db.commit()
    db.refresh(cuenta)
    return serialize_cuenta(cuenta)


@router.delete("/cuentas/{cuenta_id}")
def eliminar_cuenta(cuenta_id: int, db: Session = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    cuenta = _cuenta_or_404(db, cuenta_id)
    n = db.query(func.count(MonzaTesMovimiento.id)).filter(MonzaTesMovimiento.cuenta_id == cuenta.id).scalar()
    if n:
        raise HTTPException(409, "La cuenta tiene movimientos; desactívela en vez de borrarla")
    db.delete(cuenta)
    db.commit()
    return {"ok": True}


# ─── Cartolas (import) ─────────────────────────────────────────────────────────
@router.post("/cartolas/importar")
async def importar_cartola(
    cuenta_id: int = Form(...), nombre: Optional[str] = Form(None),
    file: UploadFile = File(...), db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # lock de la cuenta: serializa importaciones concurrentes de la misma cuenta
    # (sin esto, dos requests simultáneos con el mismo archivo duplicarían todo,
    # porque el anti-duplicados de abajo es leer-y-luego-insertar)
    cuenta = _cuenta_or_404(db, cuenta_id, lock=True)
    nombre_archivo = (file.filename or "").lower()
    if not nombre_archivo.endswith(EXTENSIONES_CARTOLA):
        raise HTTPException(400, f"Formato no soportado: use {' o '.join(EXTENSIONES_CARTOLA)}")
    # Lee a lo más MAX+1 bytes: si excede, se rechaza sin cargar el resto en memoria.
    content = await file.read(MAX_CARTOLA_BYTES + 1)
    if len(content) > MAX_CARTOLA_BYTES:
        raise HTTPException(413, f"Archivo demasiado grande (máx {MAX_CARTOLA_BYTES // (1024 * 1024)} MB)")
    try:
        parsed = parse_cartola(content, file.filename or "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    movs = parsed["movimientos"]
    if not movs:
        raise HTTPException(400, "No se encontraron movimientos en el archivo. " + "; ".join(parsed["warnings"]))
    fechas = [_parse_date(m["fecha"]) for m in movs if m.get("fecha")]

    # ANTI-DUPLICADOS: reimportar la misma cartola (doble clic / archivo repetido) NO
    # debe duplicar movimientos. Un movimiento idéntico ya existente en la cuenta
    # (misma fecha/tipo/monto/referencia/glosa/saldo) se omite y se informa.
    # Multiconjunto (Counter), no set: si el archivo trae DOS movimientos idénticos y
    # en la BD ya existe UNO, solo se omite uno — el otro es legítimo y se importa.
    existentes = Counter()
    if fechas:
        rows = (db.query(MonzaTesMovimiento)
                .filter(MonzaTesMovimiento.cuenta_id == cuenta.id,
                        MonzaTesMovimiento.fecha >= min(fechas),
                        MonzaTesMovimiento.fecha <= max(fechas)).all())
        existentes = Counter(
            _clave_mov(r.fecha.isoformat() if r.fecha else None, r.tipo, r.monto,
                       r.referencia, r.glosa, r.saldo)
            for r in rows
        )
    nuevos, n_duplicados = [], 0
    for m in movs:
        k = _clave_mov(m.get("fecha"), m["tipo"], m["monto"], m.get("referencia"),
                       m.get("glosa"), m.get("saldo"))
        if existentes.get(k, 0) > 0:
            existentes[k] -= 1
            n_duplicados += 1
            continue
        nuevos.append(m)
    if not nuevos:
        raise HTTPException(409, f"Esta cartola ya estaba importada: los {n_duplicados} "
                                 "movimientos ya existen en la cuenta")
    warnings = list(parsed["warnings"])
    if n_duplicados:
        warnings.append(f"{n_duplicados} movimiento(s) ya existían en la cuenta y se omitieron")

    cartola = MonzaTesCartola(
        cuenta_id=cuenta.id, nombre=nombre or (file.filename or "Cartola"),
        fecha_desde=min(fechas) if fechas else None, fecha_hasta=max(fechas) if fechas else None,
        archivo=file.filename, origen="archivo", n_movimientos=len(nuevos),
        usuario_id=getattr(current_user, "id", None))
    db.add(cartola)
    db.flush()
    for m in nuevos:
        db.add(MonzaTesMovimiento(
            cuenta_id=cuenta.id, cartola_id=cartola.id,
            fecha=_parse_date(m["fecha"]), glosa=m.get("glosa"), tipo=m["tipo"],
            monto=m["monto"], referencia=m.get("referencia"), saldo=m.get("saldo")))
    db.commit()
    db.refresh(cartola)
    return {"cartola": serialize_cartola(cartola), "headers_detectados": parsed["headers_detectados"],
            "warnings": warnings, "n_importados": len(nuevos), "n_duplicados": n_duplicados}


@router.get("/cartolas")
def listar_cartolas(cuenta_id: Optional[int] = None, db: Session = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    q = db.query(MonzaTesCartola)
    if cuenta_id:
        q = q.filter(MonzaTesCartola.cuenta_id == cuenta_id)
    return [serialize_cartola(c) for c in q.order_by(MonzaTesCartola.id.desc()).all()]


@router.delete("/cartolas/{cartola_id}")
def eliminar_cartola(cartola_id: int, db: Session = Depends(get_db),
                     current_user: User = Depends(get_current_user)):
    cartola = db.query(MonzaTesCartola).filter(MonzaTesCartola.id == cartola_id).first()
    if not cartola:
        raise HTTPException(404, "Cartola no encontrada")
    conc = (db.query(func.count(MonzaTesMovimiento.id))
            .filter(MonzaTesMovimiento.cartola_id == cartola.id,
                    MonzaTesMovimiento.conciliado.is_(True)).scalar())
    if conc:
        raise HTTPException(409, f"La cartola tiene {conc} movimiento(s) conciliado(s); desconcílielos antes de borrarla")
    # Solo se borran los NO conciliados: si una conciliación concurrente se coló entre
    # el chequeo de arriba y este DELETE, ese movimiento sobrevive (no queda un destino
    # conciliado huérfano) y la cartola se rechaza en el re-chequeo de abajo.
    db.query(MonzaTesMovimiento).filter(
        MonzaTesMovimiento.cartola_id == cartola.id,
        MonzaTesMovimiento.conciliado.is_(False)).delete(synchronize_session=False)
    restantes = (db.query(func.count(MonzaTesMovimiento.id))
                 .filter(MonzaTesMovimiento.cartola_id == cartola.id).scalar())
    if restantes:
        db.rollback()
        raise HTTPException(409, "Se concilió un movimiento de la cartola mientras se borraba; desconcílielo primero")
    db.delete(cartola)
    db.commit()
    return {"ok": True}


# ─── Movimientos ───────────────────────────────────────────────────────────────
@router.get("/movimientos")
def listar_movimientos(
    cuenta_id: Optional[int] = None, estado: Optional[str] = None, tipo: Optional[str] = None,
    q: Optional[str] = None, page: int = 1, page_size: int = PAGE_SIZE_DEFAULT,
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user),
):
    page = max(1, int(page))
    page_size = min(max(1, int(page_size)), PAGE_SIZE_MAX)
    base = db.query(MonzaTesMovimiento)
    if cuenta_id:
        base = base.filter(MonzaTesMovimiento.cuenta_id == cuenta_id)
    if estado == "conciliado":
        base = base.filter(MonzaTesMovimiento.conciliado.is_(True))
    elif estado == "pendiente":
        base = base.filter(MonzaTesMovimiento.conciliado.is_(False))
    if tipo:
        base = base.filter(MonzaTesMovimiento.tipo == tipo)
    if q:
        like = f"%{q}%"
        base = base.filter(or_(MonzaTesMovimiento.glosa.ilike(like),
                               MonzaTesMovimiento.referencia.ilike(like)))
    total = base.count()
    rows = (base.order_by(MonzaTesMovimiento.fecha.desc(), MonzaTesMovimiento.id.desc())
                .offset((page - 1) * page_size).limit(page_size).all())
    dmap = _destinos_for_movs(db, rows)
    return {
        "movimientos": [serialize_movimiento(m, destino=dmap.get(m.id)) for m in rows],
        "total": int(total), "page": page, "page_size": page_size,
    }


@router.post("/movimientos")
def crear_movimiento(payload: MovimientoIn, db: Session = Depends(get_db),
                     current_user: User = Depends(get_current_user)):
    _cuenta_or_404(db, payload.cuenta_id)
    if payload.cartola_id is not None:
        ca = db.query(MonzaTesCartola).filter(
            MonzaTesCartola.id == payload.cartola_id,
            MonzaTesCartola.cuenta_id == payload.cuenta_id).first()
        if not ca:
            raise HTTPException(400, "Cartola inválida para esta cuenta")
    # Fecha explícita pero no parseable → error (no se reemplaza en silencio por hoy).
    fecha = _parse_date(payload.fecha)
    if payload.fecha and fecha is None:
        raise HTTPException(400, f"Fecha inválida: '{payload.fecha}' (use AAAA-MM-DD o DD/MM/AAAA)")
    mov = MonzaTesMovimiento(
        cuenta_id=payload.cuenta_id, cartola_id=payload.cartola_id,
        fecha=fecha or date.today(), glosa=payload.glosa,
        tipo=payload.tipo, monto=payload.monto, referencia=payload.referencia, saldo=payload.saldo)
    db.add(mov)
    if payload.cartola_id is not None:
        # mantiene n_movimientos consistente con las filas reales del lote
        db.query(MonzaTesCartola).filter(MonzaTesCartola.id == payload.cartola_id).update(
            {MonzaTesCartola.n_movimientos: func.coalesce(MonzaTesCartola.n_movimientos, 0) + 1},
            synchronize_session=False)
    db.commit()
    db.refresh(mov)
    return serialize_movimiento(mov)


@router.delete("/movimientos/{mov_id}")
def eliminar_movimiento(mov_id: int, db: Session = Depends(get_db),
                        current_user: User = Depends(get_current_user)):
    mov = _mov_or_404(db, mov_id, lock=True)
    if mov.conciliado:
        raise HTTPException(409, "El movimiento está conciliado; desconcílielo antes de borrarlo")
    if mov.cartola_id is not None:
        db.query(MonzaTesCartola).filter(MonzaTesCartola.id == mov.cartola_id).update(
            {MonzaTesCartola.n_movimientos: func.coalesce(MonzaTesCartola.n_movimientos, 1) - 1},
            synchronize_session=False)
    db.delete(mov)
    db.commit()
    return {"ok": True}


# ─── Conciliación (movimiento ↔ egreso / adelanto) ─────────────────────────────
@router.get("/movimientos/{mov_id}/sugerencias")
def sugerencias(mov_id: int, db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    """Candidatos NO conciliados con monto ≈ igual, ordenados por cercanía de fecha:
      · cargo → Comprobantes de Egreso de Compras (clase 'egreso').
      · abono → Adelantos 50% verificados sin conciliar (clase 'adelanto') Y cobranzas
        (ingresos de caja de Facturas y Cobranzas) sin conciliar (clase 'cobranza')."""
    mov = _mov_or_404(db, mov_id)
    _solo_cuentas_clp(mov.cuenta)
    monto = _f(mov.monto)
    ref = mov.fecha or date.today()

    if mov.tipo == "cargo":
        egresos = (db.query(MonzaContEgreso)
                   .filter(MonzaContEgreso.conciliado.is_(False),
                           MonzaContEgreso.monto_total_clp >= monto - TOL,
                           MonzaContEgreso.monto_total_clp <= monto + TOL).all())

        def _dist_e(e):
            return abs((e.fecha - ref).days) if e.fecha else 9999

        egresos.sort(key=_dist_e)
        top = egresos[:15]
        smap = _egreso_summaries(db, top)
        return {"movimiento_id": mov.id, "monto": monto,
                "sugerencias": [{**smap[e.id], "dias_diferencia": _dist_e(e)} for e in top]}

    # abono → adelantos con monto ≈ y sin enlace previo (plus Monza)…
    adels = (db.query(MonzaContAdelanto)
             .filter(MonzaContAdelanto.monto >= monto - TOL,
                     MonzaContAdelanto.monto <= monto + TOL).all())
    ya_a = _adelantos_conciliados_ids(db, [a.id for a in adels])
    adels = [a for a in adels if a.id not in ya_a]

    def _dist_a(a):
        return abs((a.fecha_pago - ref).days) if a.fecha_pago else 9999

    amap = _adelanto_summaries(db, adels)

    # …Y cobranzas (ingresos de caja) con monto ≈ y sin enlace en la tabla nueva.
    # Se EXCLUYE medio='adelanto': esa cobranza es la APLICACIÓN contable de un
    # adelanto ya recibido (no un depósito nuevo); su plata se concilia por la vía
    # abono↔adelanto. Sin este filtro el mismo depósito sería conciliable dos veces.
    pares = (db.query(MonzaContCobranza, MonzaContFacturaCliente)
             .join(MonzaContFacturaCliente, MonzaContFacturaCliente.id == MonzaContCobranza.factura_id)
             .filter(MonzaContCobranza.medio != MEDIO_ADELANTO,
                     MonzaContCobranza.monto >= monto - TOL,
                     MonzaContCobranza.monto <= monto + TOL).all())
    ya_c = _cobranzas_conciliadas_ids(db, [c.id for c, _f_ in pares])
    pares = [(c, f) for c, f in pares if c.id not in ya_c]

    def _dist_c(par):
        c = par[0]
        return abs((c.fecha - ref).days) if c.fecha else 9999

    cmap = _cobranza_summaries(db, pares)
    cands = ([{**amap[a.id], "dias_diferencia": _dist_a(a)} for a in adels]
             + [{**cmap[c.id], "dias_diferencia": _dist_c((c, f))} for c, f in pares])
    cands.sort(key=lambda s: s["dias_diferencia"])
    return {"movimiento_id": mov.id, "monto": monto, "sugerencias": cands[:15]}


@router.post("/movimientos/{mov_id}/conciliar")
def conciliar(mov_id: int, payload: ConciliarIn, db: Session = Depends(get_db),
              current_user: User = Depends(get_current_user)):
    """Enlaza el movimiento con su destino (montos deben coincidir ±tolerancia):
      · cargo + egreso_id  → marca ambos conciliados y completa en el egreso la
        fecha/ref bancaria desde la cartola (igual que Grupo AM).
      · abono + adelanto_id → crea el enlace (el 'conciliado' del adelanto se deriva);
        marca el movimiento conciliado.
      · abono + cobranza_id → crea el enlace en monza_tes_conciliacion_ingreso (el
        'conciliado' de la cobranza se deriva); marca el movimiento conciliado."""
    mov = _mov_or_404(db, mov_id, lock=True)
    _solo_cuentas_clp(mov.cuenta)
    if mov.conciliado:
        raise HTTPException(409, "El movimiento ya está conciliado")
    # UTC timezone-aware (datetime.utcnow() está deprecado desde Python 3.12).
    now = datetime.now(timezone.utc)
    uid = getattr(current_user, "id", None)

    if payload.egreso_id:
        if mov.tipo != "cargo":
            raise HTTPException(400, "Un egreso de Compras se concilia contra un CARGO del banco")
        egreso = (db.query(MonzaContEgreso)
                  .filter(MonzaContEgreso.id == payload.egreso_id)
                  .with_for_update().first())
        if not egreso:
            raise HTTPException(404, "Egreso no encontrado")
        if egreso.conciliado:
            raise HTTPException(409, "Ese egreso ya está conciliado con otro movimiento")
        if abs(_f(mov.monto) - _f(egreso.monto_total_clp)) > TOL:
            raise HTTPException(400, f"Los montos no coinciden (movimiento {_f(mov.monto):.0f} vs egreso {_f(egreso.monto_total_clp):.0f})")
        db.add(MonzaTesConciliacion(movimiento_id=mov.id, egreso_id=egreso.id,
                                    monto_conciliado_clp=_f(mov.monto), usuario_id=uid))
        egreso.conciliado = True
        egreso.conciliado_at = now
        if mov.fecha:
            egreso.fecha_mov_bancario = mov.fecha
        if mov.referencia:
            egreso.referencia_bancaria = mov.referencia
        destino_fn = lambda: _egreso_summaries(db, [egreso])[egreso.id]  # noqa: E731
    elif payload.adelanto_id:
        if mov.tipo != "abono":
            raise HTTPException(400, "Un adelanto se concilia contra un ABONO del banco")
        adel = (db.query(MonzaContAdelanto)
                .filter(MonzaContAdelanto.id == payload.adelanto_id)
                .with_for_update().first())
        if not adel:
            raise HTTPException(404, "Adelanto no encontrado")
        if _adelantos_conciliados_ids(db, [adel.id]):
            raise HTTPException(409, "Ese adelanto ya está conciliado con otro movimiento")
        if abs(_f(mov.monto) - _f(adel.monto)) > TOL:
            raise HTTPException(400, f"Los montos no coinciden (movimiento {_f(mov.monto):.0f} vs adelanto {_f(adel.monto):.0f})")
        db.add(MonzaTesConciliacion(movimiento_id=mov.id, adelanto_id=adel.id,
                                    monto_conciliado_clp=_f(mov.monto), usuario_id=uid))
        destino_fn = lambda: _adelanto_summaries(db, [adel])[adel.id]  # noqa: E731
    else:
        if mov.tipo != "abono":
            raise HTTPException(400, "Una cobranza (ingreso de caja) se concilia contra un ABONO del banco")
        # Lock de cobranza Y factura: el recálculo de saldos en Facturas también
        # bloquea la factura, así que esto serializa contra cobranzas concurrentes.
        par = (db.query(MonzaContCobranza, MonzaContFacturaCliente)
               .join(MonzaContFacturaCliente, MonzaContFacturaCliente.id == MonzaContCobranza.factura_id)
               .filter(MonzaContCobranza.id == payload.cobranza_id)
               .with_for_update().first())
        if not par:
            raise HTTPException(404, "Cobranza no encontrada")
        cobranza, _factura = par
        if cobranza.medio == MEDIO_ADELANTO:
            raise HTTPException(400, "Esta cobranza es la aplicación de un adelanto (no un depósito nuevo); el abono del banco se concilia contra el ADELANTO en la pestaña Aprobaciones")
        if _cobranzas_conciliadas_ids(db, [cobranza.id]):
            raise HTTPException(409, "Esa cobranza ya está conciliada con otro movimiento")
        if abs(_f(mov.monto) - _f(cobranza.monto)) > TOL:
            raise HTTPException(400, f"Los montos no coinciden (movimiento {_f(mov.monto):.0f} vs cobranza {_f(cobranza.monto):.0f})")
        db.add(MonzaTesConciliacionIngreso(movimiento_id=mov.id, cobranza_id=cobranza.id,
                                           monto_conciliado_clp=_f(mov.monto), usuario_id=uid))
        destino_fn = lambda: _cobranza_summaries(db, [(cobranza, _factura)])[cobranza.id]  # noqa: E731

    mov.conciliado = True
    mov.conciliado_at = now
    mov.conciliado_usuario_id = uid
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "No se pudo conciliar (conflicto)")
    db.refresh(mov)
    return serialize_movimiento(mov, destino=destino_fn())


@router.post("/movimientos/{mov_id}/desconciliar")
def desconciliar(mov_id: int, db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    """Deshace la conciliación: libera el movimiento y su(s) destino(s) enlazado(s)."""
    mov = _mov_or_404(db, mov_id, lock=True)
    if not mov.conciliado:
        raise HTTPException(400, "El movimiento no está conciliado")
    for link in list(mov.conciliaciones):
        if link.egreso_id:
            eg = (db.query(MonzaContEgreso)
                  .filter(MonzaContEgreso.id == link.egreso_id)
                  .with_for_update().first())
            if eg:
                eg.conciliado = False
                eg.conciliado_at = None
                # Se limpia SOLO lo que vino de ESTE movimiento al conciliar (fecha/ref
                # idénticas al mov): así no queda data del cruce equivocado, pero se
                # conserva lo que el operador ingresó a mano en Compras.
                if mov.fecha and eg.fecha_mov_bancario == mov.fecha:
                    eg.fecha_mov_bancario = None
                if mov.referencia and eg.referencia_bancaria == mov.referencia:
                    eg.referencia_bancaria = None
        # adelanto: su 'conciliado' se deriva del enlace → basta borrar el enlace.
        db.delete(link)
    for link in list(mov.conciliaciones_ingreso):
        # cobranza: su 'conciliado' se deriva del enlace → basta borrar el enlace.
        db.delete(link)
    mov.conciliado = False
    mov.conciliado_at = None
    mov.conciliado_usuario_id = None
    db.commit()
    db.refresh(mov)
    return serialize_movimiento(mov)


# ─── Pendientes por conciliar (para emparejar manualmente) ─────────────────────
@router.get("/egresos-pendientes")
def egresos_pendientes(q: Optional[str] = None, page: int = 1, page_size: int = PAGE_SIZE_DEFAULT,
                       db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Comprobantes de Egreso de Compras aún NO conciliados."""
    page = max(1, int(page))
    page_size = min(max(1, int(page_size)), PAGE_SIZE_MAX)
    base = db.query(MonzaContEgreso).filter(MonzaContEgreso.conciliado.is_(False))
    if q:
        like = f"%{q}%"
        base = base.filter(or_(MonzaContEgreso.beneficiario.ilike(like),
                               MonzaContEgreso.numero_operacion.ilike(like)))
    total = base.count()
    rows = (base.order_by(MonzaContEgreso.fecha.desc(), MonzaContEgreso.id.desc())
                .offset((page - 1) * page_size).limit(page_size).all())
    smap = _egreso_summaries(db, rows)
    return {"egresos": [smap[e.id] for e in rows], "total": int(total),
            "page": page, "page_size": page_size}


@router.get("/adelantos-pendientes")
def adelantos_pendientes(db: Session = Depends(get_db),
                         current_user: User = Depends(get_current_user)):
    """Adelantos APROBADOS aún sin conciliar con un abono del banco."""
    adels = db.query(MonzaContAdelanto).order_by(MonzaContAdelanto.id.desc()).all()
    ya = _adelantos_conciliados_ids(db, [a.id for a in adels])
    pendientes = [a for a in adels if a.id not in ya]
    amap = _adelanto_summaries(db, pendientes)
    return {"adelantos": [amap[a.id] for a in pendientes], "total": len(pendientes)}


@router.get("/cobranzas-pendientes")
def cobranzas_pendientes(q: Optional[str] = None, page: int = 1, page_size: int = PAGE_SIZE_DEFAULT,
                         db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Cobranzas (ingresos de caja de Facturas y Cobranzas) aún NO conciliadas con un
    abono del banco. El 'conciliado' se deriva del enlace en monza_tes_conciliacion_ingreso."""
    page = max(1, int(page))
    page_size = min(max(1, int(page_size)), PAGE_SIZE_MAX)
    ya_conciliadas = db.query(MonzaTesConciliacionIngreso.cobranza_id).scalar_subquery()
    base = (db.query(MonzaContCobranza, MonzaContFacturaCliente)
            .join(MonzaContFacturaCliente, MonzaContFacturaCliente.id == MonzaContCobranza.factura_id)
            # medio='adelanto' se excluye: es la aplicación de un adelanto ya
            # conciliado por su propia vía, no un ingreso bancario pendiente
            .filter(MonzaContCobranza.medio != MEDIO_ADELANTO,
                    ~MonzaContCobranza.id.in_(ya_conciliadas)))
    if q:
        like = f"%{q}%"
        base = base.filter(or_(MonzaContFacturaCliente.numero_factura.ilike(like),
                               MonzaContCobranza.numero_operacion.ilike(like),
                               MonzaContCobranza.banco.ilike(like)))
    total = base.count()
    rows = (base.order_by(MonzaContCobranza.fecha.desc(), MonzaContCobranza.id.desc())
                .offset((page - 1) * page_size).limit(page_size).all())
    cmap = _cobranza_summaries(db, rows)
    return {"cobranzas": [cmap[c.id] for c, _f_ in rows], "total": int(total),
            "page": page, "page_size": page_size}


# ═══ 4. FLUJO DE CAJA (NIC 7, solo lectura) ═════════════════════════════════════
@router.get("/flujo-caja")
def flujo_caja(db: Session = Depends(get_db),
               current_user: User = Depends(get_current_user)):
    """Proyección de caja por buckets de vencimiento (vencido / 0-7 / 8-30 / 31-60 /
    61+ / sin fecha): salidas = saldos de Compras por pagar; entradas = saldos de
    facturas por cobrar + adelantos aprobados aún no aplicados. Solo lectura."""
    hoy = date.today()

    def _vacio():
        return {b: {"monto": 0.0, "n": 0} for b in FLUJO_BUCKETS}

    por_pagar = _vacio()
    for venc, saldo in (db.query(MonzaContCompra.fecha_vencimiento, MonzaContCompra.saldo_clp)
                        .filter(MonzaContCompra.anulado.is_(False), MonzaContCompra.saldo_clp > TOL).all()):
        b = bucket_de(venc, hoy)
        por_pagar[b]["monto"] += _f(saldo)
        por_pagar[b]["n"] += 1

    por_cobrar = _vacio()
    # Las facturas FACTORIZADAS se excluyen (espejo de Grupo AM): el cliente le paga
    # al factor; la caja pendiente real es la retención, que se cobra al LIQUIDAR la
    # operación (no al vencimiento de la factura) y se informa aparte.
    for venc, saldo in (db.query(MonzaContFacturaCliente.fecha_vencimiento, MonzaContFacturaCliente.saldo)
                        .filter(MonzaContFacturaCliente.saldo > TOL,
                                MonzaContFacturaCliente.estado_pago != "factorizada").all()):
        b = bucket_de(venc, hoy)
        por_cobrar[b]["monto"] += _f(saldo)
        por_cobrar[b]["n"] += 1

    ret_n, ret_monto = (db.query(func.count(MonzaContFactoring.id),
                                 func.coalesce(func.sum(MonzaContFactoring.retencion), 0))
                        .filter(MonzaContFactoring.estado == "vigente").one())

    # Adelantos por aprobar (aún no son plata "segura": se informan aparte).
    cots = (db.query(MonzaCotizacion.total_bruto, MonzaCotizacion.pct_adelanto)
            .filter(MonzaCotizacion.estado.in_(ESTADOS_VENTA), MonzaCotizacion.pct_adelanto > 0,
                    or_(MonzaCotizacion.adelanto_verificado.is_(None),
                        MonzaCotizacion.adelanto_verificado != 1)).all())
    adelantos_por_aprobar = sum(round(_f(t) * int(p or 0) / 100.0, 0) for t, p in cots)

    for d in (por_pagar, por_cobrar):
        for b in FLUJO_BUCKETS:
            d[b]["monto"] = round(d[b]["monto"], 0)
    neto = {b: round(por_cobrar[b]["monto"] - por_pagar[b]["monto"], 0) for b in FLUJO_BUCKETS}
    return {
        "buckets": FLUJO_BUCKETS,
        "por_pagar": por_pagar,
        "por_cobrar": por_cobrar,
        "neto": neto,
        "adelantos_por_aprobar": {"n": len(cots), "monto": round(adelantos_por_aprobar, 0)},
        "retenciones_factoring": {"n": int(ret_n or 0), "monto": round(_f(ret_monto), 0)},
    }


# ─── Resumen / KPIs (encabezado de la página) ──────────────────────────────────
@router.get("/resumen")
def resumen(cuenta_id: Optional[int] = None, db: Session = Depends(get_db),
            current_user: User = Depends(get_current_user)):
    """KPIs de Tesorería: aprobaciones y pagos por aprobar + estado de la conciliación
    (cargos y abonos)."""
    hoy = date.today()
    base = db.query(MonzaTesMovimiento)
    if cuenta_id:
        base = base.filter(MonzaTesMovimiento.cuenta_id == cuenta_id)
    total = base.count()
    conciliados = base.filter(MonzaTesMovimiento.conciliado.is_(True)).count()
    pend_cargo = base.filter(MonzaTesMovimiento.conciliado.is_(False),
                             MonzaTesMovimiento.tipo == "cargo").count()
    pend_abono = base.filter(MonzaTesMovimiento.conciliado.is_(False),
                             MonzaTesMovimiento.tipo == "abono").count()
    egresos_pend = (db.query(func.count(MonzaContEgreso.id))
                    .filter(MonzaContEgreso.conciliado.is_(False)).scalar())
    # Cobranzas sin conciliar (el 'conciliado' se deriva del enlace en la tabla nueva)
    ya_conciliadas = db.query(MonzaTesConciliacionIngreso.cobranza_id).scalar_subquery()
    cobranzas_pend = (db.query(func.count(MonzaContCobranza.id))
                      .filter(MonzaContCobranza.medio != MEDIO_ADELANTO,
                              ~MonzaContCobranza.id.in_(ya_conciliadas)).scalar())
    # Aprobaciones pendientes (n + monto sugerido)
    cots = (db.query(MonzaCotizacion.total_bruto, MonzaCotizacion.pct_adelanto)
            .filter(MonzaCotizacion.estado.in_(ESTADOS_VENTA), MonzaCotizacion.pct_adelanto > 0,
                    or_(MonzaCotizacion.adelanto_verificado.is_(None),
                        MonzaCotizacion.adelanto_verificado != 1)).all())
    monto_aprob = sum(round(_f(t) * int(p or 0) / 100.0, 0) for t, p in cots)
    # Pagos por aprobar (compras con saldo) + vencido, para el semáforo del encabezado.
    pp_n, pp_monto = (db.query(func.count(MonzaContCompra.id),
                               func.coalesce(func.sum(MonzaContCompra.saldo_clp), 0))
                      .filter(MonzaContCompra.anulado.is_(False),
                              MonzaContCompra.saldo_clp > TOL).one())
    vencido = (db.query(func.coalesce(func.sum(MonzaContCompra.saldo_clp), 0))
               .filter(MonzaContCompra.anulado.is_(False), MonzaContCompra.saldo_clp > TOL,
                       MonzaContCompra.fecha_vencimiento.isnot(None),
                       MonzaContCompra.fecha_vencimiento < hoy).scalar())
    return {
        "aprobaciones_pendientes": len(cots),
        "monto_aprobaciones_clp": round(monto_aprob, 0),
        "pagos_por_aprobar": int(pp_n or 0),
        "monto_por_pagar_clp": round(_f(pp_monto), 0),
        "movimientos_total": int(total),
        "movimientos_conciliados": int(conciliados),
        "cargos_pendientes": int(pend_cargo),
        "abonos_pendientes": int(pend_abono),
        "egresos_sin_conciliar": int(egresos_pend or 0),
        "cobranzas_sin_conciliar": int(cobranzas_pend or 0),
        "por_pagar_vencido_clp": round(_f(vencido), 0),
    }
