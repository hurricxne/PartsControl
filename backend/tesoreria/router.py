"""API del módulo Tesorería (Grupo AM).

Prefijo: /tesoreria (se monta con prefix=/api → /api/tesoreria).
SOLO MachParts (Grupo AM = 'mineria'): candado require_empresa a nivel de router.

Tesorería REVISA, APRUEBA y CONCILIA lo que otros módulos registran. 4 sub-áreas:

  1. POR PAGAR / APROBAR PAGOS — cola de compras con saldo (registradas en Compras/CxP
     con pago futuro o parcial). Tesorería DA LA ORDEN del pago: crea el Comprobante
     de Egreso con la MISMA regla de negocio que Compras (reusa `_crear_egreso` de
     compras_contab: locks anti doble-pago, tope por saldo, recálculo de estados).
  2. CONCILIACIÓN — cartolas (CSV/XLSX) y cruce 1:1 exacto (±TOL):
       · cargo  ↔ cont_egreso   (egreso de Compras; marca egreso.conciliado)
       · abono  ↔ cont_cobranza (ingreso de caja de Facturas y Cobranzas; el
         "conciliado" de la cobranza se DERIVA del enlace conc_conciliacion_ingreso)
  3. FLUJO DE CAJA — proyección NIC 7 por buckets de vencimiento: salidas (Compras por
     pagar) vs entradas (facturas por cobrar, excluyendo las factorizadas, cuya caja
     pendiente es la retención del factor). Solo lectura.
  4. CUENTAS / CARTOLAS / MOVIMIENTOS — catálogo bancario y administración de cartolas.
"""
from collections import Counter
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy import func, or_
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from database import get_db
from auth import get_current_user
from empresa_guard import require_empresa
from models.models import User, ContFacturaCliente, ContCobranza, ContFactoring
# Dependencias documentadas sobre el módulo Compras/CxP: el egreso vive allá; la orden
# de pago de Tesorería reusa su regla de negocio (una sola fuente de verdad).
from compras_contab.models import ContEgreso, ContEgresoDetalle, ContCompra
from compras_contab.schemas import EgresoCreate
from compras_contab.router import _crear_egreso
from compras_contab.service import serialize_egreso, parse_date_estricta, _estado_pago as _estado_pago_compra

from .models import CuentaBancaria, Cartola, MovimientoBancario, Conciliacion, ConciliacionIngreso
from .schemas import CuentaIn, MovimientoIn, ConciliarIn
from .service import (
    TOL, DIAS_SUGERENCIA, TIPOS_MOV, BANCOS_SUGERIDOS, FLUJO_BUCKETS,
    _f, _parse_date, empresa_de, bucket_de, parse_cartola,
    serialize_cuenta, serialize_cartola, serialize_movimiento,
)

# Módulo SOLO MachParts (Grupo AM = 'mineria'). El filtrado por empresa ya existe en cada
# query; este guard de router además deniega (403) el acceso a usuarios de otra empresa.
router = APIRouter(
    prefix="/tesoreria",
    tags=["tesoreria"],
    dependencies=[Depends(require_empresa("mineria"))],
)

PAGE_SIZE_DEFAULT = 100
PAGE_SIZE_MAX = 500
# Tope de tamaño para la cartola subida (una cartola real pesa KBs; 10 MB es holgado
# y evita que un archivo gigante agote la memoria del parser).
MAX_CARTOLA_BYTES = 10 * 1024 * 1024
EXTENSIONES_CARTOLA = (".csv", ".xlsx")


# ─── Helpers ───────────────────────────────────────────────────────────────────
def _fecha(s, campo: str):
    try:
        return parse_date_estricta(s, campo=campo)
    except ValueError as e:
        raise HTTPException(400, str(e))


def _cuenta_scoped(db, cuenta_id, empresa, *, lock=False) -> CuentaBancaria:
    q = db.query(CuentaBancaria).filter(CuentaBancaria.id == cuenta_id, CuentaBancaria.empresa == empresa)
    c = (q.with_for_update().first() if lock else q.first())
    if not c:
        raise HTTPException(404, "Cuenta bancaria no encontrada")
    return c


def _mov_scoped(db, mov_id, empresa, *, lock=False) -> MovimientoBancario:
    q = db.query(MovimientoBancario).filter(
        MovimientoBancario.id == mov_id, MovimientoBancario.empresa == empresa)
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
    rows = (db.query(ContEgresoDetalle, ContCompra)
            .join(ContCompra, ContCompra.id == ContEgresoDetalle.compra_id)
            .filter(ContEgresoDetalle.egreso_id.in_(ids)).all())
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


def _egreso_summary(db, egreso: ContEgreso) -> dict:
    """Resumen de UN egreso + las compras que paga."""
    return _egreso_summaries(db, [egreso])[egreso.id]


def _cobranza_summaries(db, pares: list) -> dict:
    """{cobranza_id: resumen} para una lista de tuplas (ContCobranza, ContFacturaCliente)."""
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
    """La conciliación compara montos contra CLP (egresos/cobranzas): en cuentas en
    otra moneda los montos no son comparables → se rechaza con mensaje claro."""
    if cuenta is not None and (cuenta.moneda or "CLP") != "CLP":
        raise HTTPException(400, f"La cuenta está en {cuenta.moneda}: la conciliación automática "
                                 "solo está disponible para cuentas en CLP (fase futura)")


def _cobranzas_conciliadas_ids(db, cobranza_ids) -> set:
    """IDs de cobranzas que YA tienen enlace de conciliación (estado derivado)."""
    if not cobranza_ids:
        return set()
    rows = (db.query(ConciliacionIngreso.cobranza_id)
            .filter(ConciliacionIngreso.cobranza_id.in_(cobranza_ids)).all())
    return {r[0] for r in rows}


def _destinos_for_movs(db, movs, empresa: str) -> dict:
    """{mov_id: resumen del destino conciliado} — egreso o cobranza (1er enlace)."""
    mov_ids = [m.id for m in movs if m.conciliado]
    if not mov_ids:
        return {}
    out = {}
    # cargos ↔ egresos
    links_e = (db.query(Conciliacion).filter(Conciliacion.movimiento_id.in_(mov_ids))
               .order_by(Conciliacion.id.asc()).all())
    first_e = {}
    for lk in links_e:
        first_e.setdefault(lk.movimiento_id, lk.egreso_id)
    if first_e:
        egresos = (db.query(ContEgreso)
                   .filter(ContEgreso.id.in_(set(first_e.values())), ContEgreso.empresa == empresa).all())
        smap = _egreso_summaries(db, egresos)
        for mov_id, eg_id in first_e.items():
            if eg_id in smap:
                out[mov_id] = smap[eg_id]
    # abonos ↔ cobranzas
    links_i = (db.query(ConciliacionIngreso).filter(ConciliacionIngreso.movimiento_id.in_(mov_ids))
               .order_by(ConciliacionIngreso.id.asc()).all())
    first_i = {}
    for lk in links_i:
        first_i.setdefault(lk.movimiento_id, lk.cobranza_id)
    if first_i:
        pares = (db.query(ContCobranza, ContFacturaCliente)
                 .join(ContFacturaCliente, ContFacturaCliente.id == ContCobranza.factura_id)
                 .filter(ContCobranza.id.in_(set(first_i.values())),
                         ContFacturaCliente.empresa == empresa).all())
        cmap = _cobranza_summaries(db, pares)
        for mov_id, cob_id in first_i.items():
            if cob_id in cmap and mov_id not in out:
                out[mov_id] = cmap[cob_id]
    return out


# ═══ 1. POR PAGAR / APROBAR PAGOS (la orden del pago la da Tesorería) ═══════════
@router.get("/por-pagar")
def por_pagar(q: Optional[str] = None, page: int = 1, page_size: int = PAGE_SIZE_DEFAULT,
              db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Cola de aprobación de pagos: compras ACTIVAS con saldo pendiente (registradas en
    Compras con pago futuro o parcial), ordenadas por vencimiento. Desde aquí Tesorería
    aprueba y registra el pago (POST /pagos)."""
    empresa = empresa_de(current_user)
    page = max(1, int(page))
    page_size = min(max(1, int(page_size)), PAGE_SIZE_MAX)
    base = (db.query(ContCompra)
            .filter(ContCompra.empresa == empresa, ContCompra.anulado.is_(False),
                    ContCompra.saldo_clp > TOL))
    if q:
        like = f"%{q}%"
        base = base.filter(or_(
            ContCompra.acreedor.ilike(like), ContCompra.numero_documento.ilike(like),
            ContCompra.proveedor_rut.ilike(like), ContCompra.categoria.ilike(like)))
    total = base.count()
    # Vencidas y por vencer primero; sin fecha al final (NULLS LAST portable MySQL/SQLite).
    rows = (base.order_by(ContCompra.fecha_vencimiento.is_(None).asc(),
                          ContCompra.fecha_vencimiento.asc(), ContCompra.id.asc())
                .offset((page - 1) * page_size).limit(page_size).all())
    hoy = date.today()
    buckets = {b: {"monto": 0.0, "n": 0} for b in FLUJO_BUCKETS}
    for venc, saldo in base.with_entities(ContCompra.fecha_vencimiento, ContCompra.saldo_clp).all():
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
    (parcial o total). MISMA regla de negocio que POST /api/compras-contab/egresos
    (reusa `_crear_egreso`: locks anti doble-pago, tope por saldo, recálculo)."""
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


# ═══ 2. CUENTAS BANCARIAS ════════════════════════════════════════════════════════
@router.get("/cuentas")
def listar_cuentas(incluir_inactivas: bool = False, db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_user)):
    empresa = empresa_de(current_user)
    q = db.query(CuentaBancaria).filter(CuentaBancaria.empresa == empresa)
    if not incluir_inactivas:
        q = q.filter(CuentaBancaria.activo.is_(True))
    cuentas = q.order_by(CuentaBancaria.banco.asc(), CuentaBancaria.id.asc()).all()
    return {"cuentas": [serialize_cuenta(c) for c in cuentas], "bancos_sugeridos": BANCOS_SUGERIDOS}


@router.post("/cuentas")
def crear_cuenta(payload: CuentaIn, db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    cuenta = CuentaBancaria(
        empresa=empresa_de(current_user), banco=payload.banco, nombre=payload.nombre,
        numero_cuenta=payload.numero_cuenta, moneda=payload.moneda, activo=payload.activo,
        observaciones=payload.observaciones)
    db.add(cuenta)
    db.commit()
    db.refresh(cuenta)
    return serialize_cuenta(cuenta)


@router.put("/cuentas/{cuenta_id}")
def actualizar_cuenta(cuenta_id: int, payload: CuentaIn, db: Session = Depends(get_db),
                      current_user: User = Depends(get_current_user)):
    cuenta = _cuenta_scoped(db, cuenta_id, empresa_de(current_user))
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
    cuenta = _cuenta_scoped(db, cuenta_id, empresa_de(current_user))
    n = db.query(func.count(MovimientoBancario.id)).filter(MovimientoBancario.cuenta_id == cuenta.id).scalar()
    if n:
        raise HTTPException(409, "La cuenta tiene movimientos; desactívela en vez de borrarla")
    db.delete(cuenta)
    db.commit()
    return {"ok": True}


# ═══ 3. CARTOLAS (import) ════════════════════════════════════════════════════════
@router.post("/cartolas/importar")
async def importar_cartola(
    cuenta_id: int = Form(...), nombre: Optional[str] = Form(None),
    file: UploadFile = File(...), db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    empresa = empresa_de(current_user)
    # lock de la cuenta: serializa importaciones concurrentes de la misma cuenta
    # (sin esto, dos requests simultáneos con el mismo archivo duplicarían todo,
    # porque el anti-duplicados de abajo es leer-y-luego-insertar)
    cuenta = _cuenta_scoped(db, cuenta_id, empresa, lock=True)
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
        rows = (db.query(MovimientoBancario)
                .filter(MovimientoBancario.cuenta_id == cuenta.id,
                        MovimientoBancario.empresa == empresa,
                        MovimientoBancario.fecha >= min(fechas),
                        MovimientoBancario.fecha <= max(fechas)).all())
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

    cartola = Cartola(
        empresa=empresa, cuenta_id=cuenta.id, nombre=nombre or (file.filename or "Cartola"),
        fecha_desde=min(fechas) if fechas else None, fecha_hasta=max(fechas) if fechas else None,
        archivo=file.filename, origen="archivo", n_movimientos=len(nuevos),
        usuario_id=getattr(current_user, "id", None))
    db.add(cartola)
    db.flush()
    for m in nuevos:
        db.add(MovimientoBancario(
            empresa=empresa, cuenta_id=cuenta.id, cartola_id=cartola.id,
            fecha=_parse_date(m["fecha"]), glosa=m.get("glosa"), tipo=m["tipo"],
            monto=m["monto"], referencia=m.get("referencia"), saldo=m.get("saldo")))
    db.commit()
    db.refresh(cartola)
    return {"cartola": serialize_cartola(cartola), "headers_detectados": parsed["headers_detectados"],
            "warnings": warnings, "n_importados": len(nuevos), "n_duplicados": n_duplicados}


@router.get("/cartolas")
def listar_cartolas(cuenta_id: Optional[int] = None, db: Session = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    q = db.query(Cartola).filter(Cartola.empresa == empresa_de(current_user))
    if cuenta_id:
        q = q.filter(Cartola.cuenta_id == cuenta_id)
    return [serialize_cartola(c) for c in q.order_by(Cartola.id.desc()).all()]


@router.delete("/cartolas/{cartola_id}")
def eliminar_cartola(cartola_id: int, db: Session = Depends(get_db),
                     current_user: User = Depends(get_current_user)):
    empresa = empresa_de(current_user)
    cartola = db.query(Cartola).filter(Cartola.id == cartola_id, Cartola.empresa == empresa).first()
    if not cartola:
        raise HTTPException(404, "Cartola no encontrada")
    conc = (db.query(func.count(MovimientoBancario.id))
            .filter(MovimientoBancario.cartola_id == cartola.id, MovimientoBancario.conciliado.is_(True)).scalar())
    if conc:
        raise HTTPException(409, f"La cartola tiene {conc} movimiento(s) conciliado(s); desconcílielos antes de borrarla")
    # Solo se borran los NO conciliados: si una conciliación concurrente se coló entre
    # el chequeo de arriba y este DELETE, ese movimiento sobrevive (no queda un egreso
    # conciliado huérfano) y la cartola se rechaza en el re-chequeo de abajo.
    db.query(MovimientoBancario).filter(
        MovimientoBancario.cartola_id == cartola.id,
        MovimientoBancario.conciliado.is_(False)).delete(synchronize_session=False)
    restantes = (db.query(func.count(MovimientoBancario.id))
                 .filter(MovimientoBancario.cartola_id == cartola.id).scalar())
    if restantes:
        db.rollback()
        raise HTTPException(409, "Se concilió un movimiento de la cartola mientras se borraba; desconcílielo primero")
    db.delete(cartola)
    db.commit()
    return {"ok": True}


# ═══ 4. MOVIMIENTOS ══════════════════════════════════════════════════════════════
@router.get("/movimientos")
def listar_movimientos(
    cuenta_id: Optional[int] = None, estado: Optional[str] = None, tipo: Optional[str] = None,
    q: Optional[str] = None, page: int = 1, page_size: int = PAGE_SIZE_DEFAULT,
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user),
):
    empresa = empresa_de(current_user)
    page = max(1, int(page))
    page_size = min(max(1, int(page_size)), PAGE_SIZE_MAX)
    base = db.query(MovimientoBancario).filter(MovimientoBancario.empresa == empresa)
    if cuenta_id:
        base = base.filter(MovimientoBancario.cuenta_id == cuenta_id)
    if estado == "conciliado":
        base = base.filter(MovimientoBancario.conciliado.is_(True))
    elif estado == "pendiente":
        base = base.filter(MovimientoBancario.conciliado.is_(False))
    if tipo:
        base = base.filter(MovimientoBancario.tipo == tipo)
    if q:
        like = f"%{q}%"
        base = base.filter(or_(MovimientoBancario.glosa.ilike(like), MovimientoBancario.referencia.ilike(like)))
    total = base.count()
    rows = (base.order_by(MovimientoBancario.fecha.desc(), MovimientoBancario.id.desc())
                .offset((page - 1) * page_size).limit(page_size).all())
    dmap = _destinos_for_movs(db, rows, empresa)
    return {
        "movimientos": [serialize_movimiento(m, destino=dmap.get(m.id)) for m in rows],
        "total": int(total), "page": page, "page_size": page_size,
    }


@router.post("/movimientos")
def crear_movimiento(payload: MovimientoIn, db: Session = Depends(get_db),
                     current_user: User = Depends(get_current_user)):
    empresa = empresa_de(current_user)
    _cuenta_scoped(db, payload.cuenta_id, empresa)
    if payload.cartola_id is not None:
        ca = db.query(Cartola).filter(
            Cartola.id == payload.cartola_id, Cartola.empresa == empresa,
            Cartola.cuenta_id == payload.cuenta_id).first()
        if not ca:
            raise HTTPException(400, "Cartola inválida para esta cuenta/empresa")
    # Fecha explícita pero no parseable → error (no se reemplaza en silencio por hoy).
    fecha = _parse_date(payload.fecha)
    if payload.fecha and fecha is None:
        raise HTTPException(400, f"Fecha inválida: '{payload.fecha}' (use AAAA-MM-DD o DD/MM/AAAA)")
    mov = MovimientoBancario(
        empresa=empresa, cuenta_id=payload.cuenta_id, cartola_id=payload.cartola_id,
        fecha=fecha or date.today(), glosa=payload.glosa,
        tipo=payload.tipo, monto=payload.monto, referencia=payload.referencia, saldo=payload.saldo)
    db.add(mov)
    if payload.cartola_id is not None:
        # mantiene n_movimientos consistente con las filas reales del lote
        db.query(Cartola).filter(Cartola.id == payload.cartola_id).update(
            {Cartola.n_movimientos: func.coalesce(Cartola.n_movimientos, 0) + 1},
            synchronize_session=False)
    db.commit()
    db.refresh(mov)
    return serialize_movimiento(mov)


@router.delete("/movimientos/{mov_id}")
def eliminar_movimiento(mov_id: int, db: Session = Depends(get_db),
                        current_user: User = Depends(get_current_user)):
    mov = _mov_scoped(db, mov_id, empresa_de(current_user), lock=True)
    if mov.conciliado:
        raise HTTPException(409, "El movimiento está conciliado; desconcílielo antes de borrarlo")
    if mov.cartola_id is not None:
        db.query(Cartola).filter(Cartola.id == mov.cartola_id).update(
            {Cartola.n_movimientos: func.coalesce(Cartola.n_movimientos, 1) - 1},
            synchronize_session=False)
    db.delete(mov)
    db.commit()
    return {"ok": True}


# ═══ 5. CONCILIACIÓN (movimiento ↔ egreso / cobranza) ════════════════════════════
@router.get("/movimientos/{mov_id}/sugerencias")
def sugerencias(mov_id: int, db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    """Candidatos NO conciliados con monto ≈ igual, ordenados por cercanía de fecha:
      · cargo → Comprobantes de Egreso de Compras.
      · abono → cobranzas (ingresos de caja) de Facturas y Cobranzas."""
    empresa = empresa_de(current_user)
    mov = _mov_scoped(db, mov_id, empresa)
    _solo_cuentas_clp(mov.cuenta)
    monto = _f(mov.monto)
    ref = mov.fecha or date.today()

    if mov.tipo == "cargo":
        egresos = (db.query(ContEgreso)
                   .filter(ContEgreso.empresa == empresa, ContEgreso.conciliado.is_(False),
                           ContEgreso.monto_total_clp >= monto - TOL,
                           ContEgreso.monto_total_clp <= monto + TOL).all())

        def _dist_e(e):
            return abs((e.fecha - ref).days) if e.fecha else 9999

        egresos.sort(key=_dist_e)
        top = egresos[:15]
        smap = _egreso_summaries(db, top)
        return {"movimiento_id": mov.id, "monto": monto,
                "sugerencias": [{**smap[e.id], "dias_diferencia": _dist_e(e)} for e in top]}

    # abono → cobranzas con monto ≈ y sin enlace previo
    pares = (db.query(ContCobranza, ContFacturaCliente)
             .join(ContFacturaCliente, ContFacturaCliente.id == ContCobranza.factura_id)
             .filter(ContFacturaCliente.empresa == empresa,
                     ContCobranza.monto >= monto - TOL,
                     ContCobranza.monto <= monto + TOL).all())
    ya = _cobranzas_conciliadas_ids(db, [c.id for c, _f_ in pares])
    pares = [(c, f) for c, f in pares if c.id not in ya]

    def _dist_c(par):
        c = par[0]
        return abs((c.fecha - ref).days) if c.fecha else 9999

    pares.sort(key=_dist_c)
    top = pares[:15]
    cmap = _cobranza_summaries(db, top)
    return {"movimiento_id": mov.id, "monto": monto,
            "sugerencias": [{**cmap[c.id], "dias_diferencia": _dist_c((c, f))} for c, f in top]}


@router.post("/movimientos/{mov_id}/conciliar")
def conciliar(mov_id: int, payload: ConciliarIn, db: Session = Depends(get_db),
              current_user: User = Depends(get_current_user)):
    """Enlaza el movimiento con su destino (montos deben coincidir ±tolerancia):
      · cargo + egreso_id   → marca ambos conciliados y completa en el egreso la
        fecha/ref bancaria desde la cartola.
      · abono + cobranza_id → crea el enlace (el 'conciliado' de la cobranza se deriva);
        marca el movimiento conciliado."""
    empresa = empresa_de(current_user)
    mov = _mov_scoped(db, mov_id, empresa, lock=True)
    _solo_cuentas_clp(mov.cuenta)
    if mov.conciliado:
        raise HTTPException(409, "El movimiento ya está conciliado")
    now = datetime.now(timezone.utc)
    uid = getattr(current_user, "id", None)

    if payload.egreso_id:
        if mov.tipo != "cargo":
            raise HTTPException(400, "Un egreso de Compras se concilia contra un CARGO del banco")
        egreso = (db.query(ContEgreso)
                  .filter(ContEgreso.id == payload.egreso_id, ContEgreso.empresa == empresa)
                  .with_for_update().first())
        if not egreso:
            raise HTTPException(404, "Egreso no encontrado")
        if egreso.conciliado:
            raise HTTPException(409, "Ese egreso ya está conciliado con otro movimiento")
        if abs(_f(mov.monto) - _f(egreso.monto_total_clp)) > TOL:
            raise HTTPException(400, f"Los montos no coinciden (movimiento {_f(mov.monto):.0f} vs egreso {_f(egreso.monto_total_clp):.0f})")
        db.add(Conciliacion(empresa=empresa, movimiento_id=mov.id, egreso_id=egreso.id,
                            monto_conciliado_clp=_f(mov.monto), usuario_id=uid))
        egreso.conciliado = True
        egreso.conciliado_at = now
        if mov.fecha:
            egreso.fecha_mov_bancario = mov.fecha
        if mov.referencia:
            egreso.referencia_bancaria = mov.referencia
        destino_fn = lambda: _egreso_summary(db, egreso)  # noqa: E731
    else:
        if mov.tipo != "abono":
            raise HTTPException(400, "Una cobranza (ingreso de caja) se concilia contra un ABONO del banco")
        # Lock de cobranza Y factura: el recálculo de saldos en Facturas también
        # bloquea la factura, así que esto serializa contra cobranzas concurrentes.
        par = (db.query(ContCobranza, ContFacturaCliente)
               .join(ContFacturaCliente, ContFacturaCliente.id == ContCobranza.factura_id)
               .filter(ContCobranza.id == payload.cobranza_id,
                       ContFacturaCliente.empresa == empresa)
               .with_for_update().first())
        if not par:
            raise HTTPException(404, "Cobranza no encontrada")
        cobranza, _factura = par
        if _cobranzas_conciliadas_ids(db, [cobranza.id]):
            raise HTTPException(409, "Esa cobranza ya está conciliada con otro movimiento")
        if abs(_f(mov.monto) - _f(cobranza.monto)) > TOL:
            raise HTTPException(400, f"Los montos no coinciden (movimiento {_f(mov.monto):.0f} vs cobranza {_f(cobranza.monto):.0f})")
        db.add(ConciliacionIngreso(empresa=empresa, movimiento_id=mov.id, cobranza_id=cobranza.id,
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
    empresa = empresa_de(current_user)
    mov = _mov_scoped(db, mov_id, empresa, lock=True)
    if not mov.conciliado:
        raise HTTPException(400, "El movimiento no está conciliado")
    for link in list(mov.conciliaciones):
        eg = (db.query(ContEgreso)
              .filter(ContEgreso.id == link.egreso_id, ContEgreso.empresa == empresa)
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


# ═══ 6. PENDIENTES POR CONCILIAR (para emparejar manualmente) ════════════════════
@router.get("/egresos-pendientes")
def egresos_pendientes(q: Optional[str] = None, page: int = 1, page_size: int = PAGE_SIZE_DEFAULT,
                       db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Comprobantes de Egreso de Compras aún NO conciliados (para emparejar a mano)."""
    empresa = empresa_de(current_user)
    page = max(1, int(page))
    page_size = min(max(1, int(page_size)), PAGE_SIZE_MAX)
    base = db.query(ContEgreso).filter(ContEgreso.empresa == empresa, ContEgreso.conciliado.is_(False))
    if q:
        like = f"%{q}%"
        base = base.filter(or_(ContEgreso.beneficiario.ilike(like), ContEgreso.numero_operacion.ilike(like)))
    total = base.count()
    rows = (base.order_by(ContEgreso.fecha.desc(), ContEgreso.id.desc())
                .offset((page - 1) * page_size).limit(page_size).all())
    smap = _egreso_summaries(db, rows)
    return {"egresos": [smap[e.id] for e in rows], "total": int(total),
            "page": page, "page_size": page_size}


@router.get("/cobranzas-pendientes")
def cobranzas_pendientes(q: Optional[str] = None, page: int = 1, page_size: int = PAGE_SIZE_DEFAULT,
                         db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Cobranzas (ingresos de caja de Facturas y Cobranzas) aún NO conciliadas con un
    abono del banco. El 'conciliado' se deriva del enlace en conc_conciliacion_ingreso."""
    empresa = empresa_de(current_user)
    page = max(1, int(page))
    page_size = min(max(1, int(page_size)), PAGE_SIZE_MAX)
    ya_conciliadas = db.query(ConciliacionIngreso.cobranza_id).scalar_subquery()
    base = (db.query(ContCobranza, ContFacturaCliente)
            .join(ContFacturaCliente, ContFacturaCliente.id == ContCobranza.factura_id)
            .filter(ContFacturaCliente.empresa == empresa,
                    ~ContCobranza.id.in_(ya_conciliadas)))
    if q:
        like = f"%{q}%"
        base = base.filter(or_(ContFacturaCliente.numero_factura.ilike(like),
                               ContCobranza.numero_operacion.ilike(like),
                               ContCobranza.banco.ilike(like)))
    total = base.count()
    rows = (base.order_by(ContCobranza.fecha.desc(), ContCobranza.id.desc())
                .offset((page - 1) * page_size).limit(page_size).all())
    cmap = _cobranza_summaries(db, rows)
    return {"cobranzas": [cmap[c.id] for c, _f_ in rows], "total": int(total),
            "page": page, "page_size": page_size}


# ═══ 7. FLUJO DE CAJA (NIC 7, solo lectura) ══════════════════════════════════════
@router.get("/flujo-caja")
def flujo_caja(db: Session = Depends(get_db),
               current_user: User = Depends(get_current_user)):
    """Proyección de caja por buckets de vencimiento (vencido / 0-7 / 8-30 / 31-60 /
    61+ / sin fecha): salidas = saldos de Compras por pagar; entradas = saldos de
    facturas por cobrar (las factorizadas se excluyen: su caja pendiente es la
    retención del factor, informada aparte). Solo lectura."""
    empresa = empresa_de(current_user)
    hoy = date.today()

    def _vacio():
        return {b: {"monto": 0.0, "n": 0} for b in FLUJO_BUCKETS}

    por_pagar = _vacio()
    for venc, saldo in (db.query(ContCompra.fecha_vencimiento, ContCompra.saldo_clp)
                        .filter(ContCompra.empresa == empresa, ContCompra.anulado.is_(False),
                                ContCompra.saldo_clp > TOL).all()):
        b = bucket_de(venc, hoy)
        por_pagar[b]["monto"] += _f(saldo)
        por_pagar[b]["n"] += 1

    por_cobrar = _vacio()
    for venc, saldo in (db.query(ContFacturaCliente.fecha_vencimiento, ContFacturaCliente.saldo)
                        .filter(ContFacturaCliente.empresa == empresa,
                                ContFacturaCliente.saldo > TOL,
                                ContFacturaCliente.estado_pago != "factorizada").all()):
        b = bucket_de(venc, hoy)
        por_cobrar[b]["monto"] += _f(saldo)
        por_cobrar[b]["n"] += 1

    # Facturas factorizadas: la caja pendiente real es la RETENCIÓN del factor
    # (se cobra al liquidar la operación), no el saldo de la factura.
    ret_n, ret_monto = (db.query(func.count(ContFactoring.id),
                                 func.coalesce(func.sum(ContFactoring.retencion), 0))
                        .join(ContFacturaCliente, ContFacturaCliente.id == ContFactoring.factura_id)
                        .filter(ContFacturaCliente.empresa == empresa,
                                ContFactoring.estado == "vigente").one())

    for d in (por_pagar, por_cobrar):
        for b in FLUJO_BUCKETS:
            d[b]["monto"] = round(d[b]["monto"], 0)
    neto = {b: round(por_cobrar[b]["monto"] - por_pagar[b]["monto"], 0) for b in FLUJO_BUCKETS}
    return {
        "buckets": FLUJO_BUCKETS,
        "por_pagar": por_pagar,
        "por_cobrar": por_cobrar,
        "neto": neto,
        "retenciones_factoring": {"n": int(ret_n or 0), "monto": round(_f(ret_monto), 0)},
    }


# ═══ 8. RESUMEN / KPIs (encabezado de la página) ═════════════════════════════════
@router.get("/resumen")
def resumen(cuenta_id: Optional[int] = None, db: Session = Depends(get_db),
            current_user: User = Depends(get_current_user)):
    """KPIs de Tesorería: pagos por aprobar + estado de la conciliación (cargos y abonos)."""
    empresa = empresa_de(current_user)
    hoy = date.today()
    base = db.query(MovimientoBancario).filter(MovimientoBancario.empresa == empresa)
    if cuenta_id:
        base = base.filter(MovimientoBancario.cuenta_id == cuenta_id)
    total = base.count()
    conciliados = base.filter(MovimientoBancario.conciliado.is_(True)).count()
    pend_cargo = base.filter(MovimientoBancario.conciliado.is_(False),
                             MovimientoBancario.tipo == "cargo").count()
    pend_abono = base.filter(MovimientoBancario.conciliado.is_(False),
                             MovimientoBancario.tipo == "abono").count()
    egresos_pend = (db.query(func.count(ContEgreso.id))
                    .filter(ContEgreso.empresa == empresa, ContEgreso.conciliado.is_(False)).scalar())
    ya_conciliadas = db.query(ConciliacionIngreso.cobranza_id).scalar_subquery()
    cobranzas_pend = (db.query(func.count(ContCobranza.id))
                      .join(ContFacturaCliente, ContFacturaCliente.id == ContCobranza.factura_id)
                      .filter(ContFacturaCliente.empresa == empresa,
                              ~ContCobranza.id.in_(ya_conciliadas)).scalar())
    # Pagos por aprobar (compras con saldo) + vencido, para el semáforo del encabezado.
    pp_n, pp_monto = (db.query(func.count(ContCompra.id),
                               func.coalesce(func.sum(ContCompra.saldo_clp), 0))
                      .filter(ContCompra.empresa == empresa, ContCompra.anulado.is_(False),
                              ContCompra.saldo_clp > TOL).one())
    vencido = (db.query(func.coalesce(func.sum(ContCompra.saldo_clp), 0))
               .filter(ContCompra.empresa == empresa, ContCompra.anulado.is_(False),
                       ContCompra.saldo_clp > TOL,
                       ContCompra.fecha_vencimiento.isnot(None),
                       ContCompra.fecha_vencimiento < hoy).scalar())
    return {
        "pagos_por_aprobar": int(pp_n or 0),
        "monto_por_pagar_clp": round(_f(pp_monto), 0),
        "por_pagar_vencido_clp": round(_f(vencido), 0),
        "movimientos_total": int(total),
        "movimientos_conciliados": int(conciliados),
        "cargos_pendientes": int(pend_cargo),
        "abonos_pendientes": int(pend_abono),
        "egresos_sin_conciliar": int(egresos_pend or 0),
        "cobranzas_sin_conciliar": int(cobranzas_pend or 0),
        # compat con tarjetas antiguas
        "movimientos_pendientes": int(pend_cargo + pend_abono),
    }
