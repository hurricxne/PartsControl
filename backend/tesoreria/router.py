"""API del módulo Tesorería (Grupo AM).

Prefijo: /tesoreria (se monta con prefix=/api → /api/tesoreria).
SOLO MachParts (Grupo AM = 'mineria'): candado require_empresa a nivel de router.

Tesorería REVISA, APRUEBA y CONCILIA lo que otros módulos registran. 5 sub-áreas:

  1. POR PAGAR / APROBAR PAGOS — cola de compras con saldo (registradas en Compras/CxP
     con pago futuro o parcial). Tesorería DA LA ORDEN del pago: crea el Comprobante
     de Egreso con la MISMA regla de negocio que Compras (reusa `_crear_egreso` de
     compras_contab: locks anti doble-pago, tope por saldo, recálculo de estados).
  2. APROBACIÓN DE ADELANTOS — los adelantos de cliente que Comercial informa (Cierre
     de Venta / Ventas) llegan acá; Tesorería confirma la plata recibida (monto real,
     fecha, banco, N° operación) SIN exigir cartola, y al aprobar se APLICAN a las
     facturas existentes como cobranza medio='adelanto' (regla en routers/contabilidad:
     `_aplicar_adelantos_pendientes`, una sola fuente de verdad).
  3. CONCILIACIÓN — cartolas (CSV/XLSX) y cruce 1:1 exacto (±TOL):
       · cargo  ↔ cont_egreso   (egreso de Compras; marca egreso.conciliado)
       · abono  ↔ cont_cobranza (ingreso de caja de Facturas y Cobranzas; el
         "conciliado" de la cobranza se DERIVA del enlace conc_conciliacion_ingreso)
       · abono  ↔ cont_adelanto (adelanto APROBADO; misma derivación). Las cobranzas
         medio='adelanto' se EXCLUYEN del cruce: son la APLICACIÓN contable de un
         adelanto ya recibido, no un depósito nuevo — su plata se concilia por aquí.
  4. FLUJO DE CAJA — proyección NIC 7 por buckets de vencimiento: salidas (Compras por
     pagar) vs entradas (facturas por cobrar, excluyendo las factorizadas, cuya caja
     pendiente es la retención del factor). Los adelantos informados aún no aprobados
     se muestran APARTE (todavía no son plata segura). Solo lectura.
  5. CUENTAS / CARTOLAS / MOVIMIENTOS — catálogo bancario y administración de cartolas.
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
from models.models import (
    User, ContFacturaCliente, ContCobranza, ContFactoring, ContAdelanto, OcCliente,
)
# Dependencias documentadas sobre el módulo Compras/CxP: el egreso vive allá; la orden
# de pago de Tesorería reusa su regla de negocio (una sola fuente de verdad).
from compras_contab.models import ContEgreso, ContEgresoDetalle, ContCompra
from compras_contab.schemas import EgresoCreate
from compras_contab.router import _crear_egreso
from compras_contab.service import serialize_egreso, parse_date_estricta, _estado_pago as _estado_pago_compra
# Regla de negocio de adelantos: vive en Contabilidad (una sola fuente de verdad).
# Sin ciclo: routers.contabilidad solo importa tesoreria.models (no este router).
# NOTA (guard SII del adelanto): `_aplicar_adelantos_pendientes` consulta el DTE de la
# factura antes de dejar entrar la plata. Ese guard vive en routers/contabilidad.py y es
# el que se ejecuta cuando Tesorería aprueba: si el esquema de wasabil_dte no está
# migrado, el ImportError/ProgrammingError sube como 500 crudo por ESTA ruta. El arreglo
# (ImportError → False, ProgrammingError/OperationalError → 503 diciendo qué init_db
# correr; molde monza_contabilidad/router.py) va en routers/contabilidad.py, no acá.
from routers.contabilidad import (
    MEDIO_ADELANTO, _aplicar_adelantos_pendientes, _serialize_adelanto,
    _adelantos_conciliados_ids, _monto_comprometido_adelanto,
    _validar_tope_adelantos, _total_bruto_venta,
)

from .models import CuentaBancaria, Cartola, MovimientoBancario, Conciliacion, ConciliacionIngreso
from .schemas import CuentaIn, MovimientoIn, ConciliarIn, AprobarAdelantoIn
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
    # La cuenta no decide plata (es el portón de serialización del catálogo bancario), así
    # que acá el lock NO necesita populate_existing: no se compara ni se persiste ningún
    # monto leído de esta fila. Ver `docs/regla-lecturas-de-plata.md`.
    q = db.query(CuentaBancaria).filter(CuentaBancaria.id == cuenta_id, CuentaBancaria.empresa == empresa)
    c = (q.with_for_update().first() if lock else q.first())
    if not c:
        raise HTTPException(404, "Cuenta bancaria no encontrada")
    return c


def _mov_scoped(db, mov_id, empresa, *, lock=False) -> MovimientoBancario:
    q = db.query(MovimientoBancario).filter(
        MovimientoBancario.id == mov_id, MovimientoBancario.empresa == empresa)
    # LECTURA DE PLATA bajo lock → populate_existing() obligatorio (regla 3 de
    # docs/regla-lecturas-de-plata.md, a cualquier nivel de aislamiento: el problema es el
    # identity map de SQLAlchemy, no el read view de MySQL). `mov.monto` es el lado BANCO
    # de los tres cruces (se compara ±TOL contra egreso/cobranza/adelanto y se persiste en
    # conc_conciliacion.monto_conciliado_clp): si cualquier lectura anterior del mismo
    # request dejó la fila en el identity map, el FOR UPDATE traería la versión fresca de
    # la BD y SQLAlchemy la DESCARTARÍA, sirviendo el monto viejo.
    m = (q.populate_existing().with_for_update().first() if lock else q.first())
    if not m:
        raise HTTPException(404, "Movimiento no encontrado")
    return m


def _con_retry_deadlock(db: Session, operacion):
    """Ejecuta `operacion()` reintentando ante deadlock / lock-timeout de InnoDB
    (1213 / 1205).

    Dos pares de uso diario pueden cruzarse: (1) conciliar una cobranza contra
    registrar/eliminar otra cobranza de la MISMA factura (factura + cobranzas), y (2)
    aprobar un pago contra revertir un pago de la MISMA compra en Compras/CxP (egreso +
    detalles + compra). En ambos MySQL puede elegir una víctima. Sin esta red
    el operador recibe un 500 en una operación diaria. Mismo patrón (y mismo texto de
    409) que compras_contab, recepcion_nacional y routers/despachos.py: se reintenta la
    transacción COMPLETA tras db.rollback() y, si a la tercera sigue el conflicto, se
    responde 409 accionable. Es la CAPA 2 del arreglo: la CAPA 1 (orden de locks
    factura → cobranza en `_conciliar_tx`) es la que quita la causa raíz; esto es la red
    de seguridad. Espejo de monza_tesoreria/router.py.

    DEUDA CONOCIDA: este helper ya existe idéntico en monza_tesoreria y en los módulos
    que reintentan cierres de despacho. Corresponde consolidarlo en un módulo compartido,
    pero esa extracción toca varios paquetes a la vez y se deja para el commit de
    consolidación, no para el arreglo del hallazgo."""
    from sqlalchemy.exc import OperationalError
    for _ in range(3):
        try:
            return operacion()
        except OperationalError as e:
            db.rollback()
            args = getattr(getattr(e, "orig", None), "args", None) or []
            if not args or args[0] not in (1213, 1205):
                raise
    raise HTTPException(
        409, "Conflicto momentáneo con otra operación simultánea: reintente")


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


def _adelanto_summaries(db, adelantos: list) -> dict:
    """{adelanto_id: resumen} con la info de la venta (OC/cliente) para conciliación."""
    if not adelantos:
        return {}
    oc_ids = {a.oc_cliente_id for a in adelantos}
    ocs = {o.id: o for o in db.query(OcCliente).filter(OcCliente.id.in_(oc_ids)).all()}
    out = {}
    for a in adelantos:
        oc = ocs.get(a.oc_cliente_id)
        cot = oc.cotizacion if oc else None
        out[a.id] = {
            "clase": "adelanto",
            "adelanto_id": a.id,
            "oc_cliente_id": a.oc_cliente_id,
            "numero_oc": oc.numero_oc if oc else None,
            "cliente": (cot.cliente if cot else None) or "",
            "estado": a.estado,
            "fecha": a.fecha_pago.isoformat() if a.fecha_pago else None,
            "monto": _f(a.monto),
            "banco": a.banco,
            "numero_operacion": a.numero_operacion,
        }
    return out


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
    # abonos ↔ cobranzas / adelantos (el enlace tiene exactamente uno de los dos)
    links_i = (db.query(ConciliacionIngreso).filter(ConciliacionIngreso.movimiento_id.in_(mov_ids))
               .order_by(ConciliacionIngreso.id.asc()).all())
    first_i, first_a = {}, {}
    for lk in links_i:
        if lk.cobranza_id:
            first_i.setdefault(lk.movimiento_id, lk.cobranza_id)
        elif lk.adelanto_id:
            first_a.setdefault(lk.movimiento_id, lk.adelanto_id)
    if first_i:
        pares = (db.query(ContCobranza, ContFacturaCliente)
                 .join(ContFacturaCliente, ContFacturaCliente.id == ContCobranza.factura_id)
                 .filter(ContCobranza.id.in_(set(first_i.values())),
                         ContFacturaCliente.empresa == empresa).all())
        cmap = _cobranza_summaries(db, pares)
        for mov_id, cob_id in first_i.items():
            if cob_id in cmap and mov_id not in out:
                out[mov_id] = cmap[cob_id]
    if first_a:
        adelantos = (db.query(ContAdelanto)
                     .filter(ContAdelanto.id.in_(set(first_a.values())),
                             ContAdelanto.empresa == empresa).all())
        amap = _adelanto_summaries(db, adelantos)
        for mov_id, adel_id in first_a.items():
            if adel_id in amap and mov_id not in out:
                out[mov_id] = amap[adel_id]
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


def _porton_egresos_de_las_compras(db, compra_ids: list, empresa: str) -> list:
    """CAPA 1 del orden de locks de la familia PAGOS: bloquea PRIMERO los Comprobantes de
    Egreso que ya pagan estas compras, en orden de id.

    Por qué existe. Aprobar un pago (esta ruta → `_crear_egreso`) toma los candados en el
    orden COMPRA → DETALLES, mientras revertir un pago en Compras/CxP los toma AL REVÉS:
    `eliminar_pago` / `eliminar_egreso` hacen EGRESO → DETALLES (X del DELETE en cascada) →
    COMPRA. Con las dos operaciones sobre la MISMA compra, InnoDB cierra ciclo: el tesorero
    aprueba un pago mientras Compras revierte un pago parcial de esa compra y uno de los dos
    se lleva un 1213 (500 crudo). Tomando primero la fila del EGRESO —el mismo PRIMER
    recurso que toma el lado que revierte— las dos transacciones se serializan en el portón
    en vez de cruzarse. Es el mismo patrón con el que el candado de la OC serializa
    `aprobar_adelanto` contra `crear_factura`.

    La lectura que descubre los ids va SIN lock y pide la columna suelta, para no bloquear
    `cont_egreso_detalle` antes de la compra (eso invertiría el orden respecto del propio
    `_crear_egreso` y crearía el ciclo que se quiere evitar). Queda una ventana angosta: un
    egreso COMMITEADO entre esa lectura y el lock de los detalles no entra al portón; para
    eso está la CAPA 2 (`_con_retry_deadlock`), que lo degrada a un 409 accionable.

    DEUDA CONOCIDA (fuera de este módulo, no se toca desde acá): los otros dos caminos que
    crean egresos viven en Compras/CxP —`POST /compras-contab/egresos` y
    `POST /compras-contab/{id}/pagos`— y siguen entrando por COMPRA → DETALLES sin portón ni
    reintento, así que ese par (contra `eliminar_pago` / `eliminar_egreso`) sigue pudiendo
    dar 1213 → 500. El arreglo correcto allá es el mismo portón o invertir el orden de
    `eliminar_pago`; corresponde al dueño de `compras_contab/router.py`.
    """
    ids = [r[0] for r in db.query(ContEgresoDetalle.egreso_id)
           .join(ContCompra, ContCompra.id == ContEgresoDetalle.compra_id)
           .filter(ContEgresoDetalle.compra_id.in_(set(compra_ids)),
                   ContCompra.empresa == empresa)
           .distinct().all() if r[0]]
    if not ids:
        return []
    # Orden de id también DENTRO del portón: dos aprobaciones simultáneas con conjuntos de
    # compras que se solapan piden los mismos egresos en el mismo orden.
    # Sin populate_existing a propósito: es un PORTÓN (igual que la cuenta bancaria y la OC
    # del adelanto). De estas filas no se lee ni se persiste ningún monto —el resultado se
    # descarta—, así que la regla 3 de lecturas de plata no aplica.
    return (db.query(ContEgreso).filter(ContEgreso.id.in_(sorted(ids)))
            .order_by(ContEgreso.id.asc()).with_for_update().all())


def _aprobar_pago_tx(payload: EgresoCreate, empresa: str, fecha, db: Session,
                     current_user: User) -> ContEgreso:
    """Cuerpo REINTENTABLE de la aprobación del pago: termina en el commit, así que si
    InnoDB elige esta transacción como víctima (1213/1205) no quedó nada escrito y el
    reintento de `_con_retry_deadlock` no puede duplicar el pago al proveedor."""
    try:
        _porton_egresos_de_las_compras(db, [d.compra_id for d in payload.detalles], empresa)
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
    return egreso


@router.post("/pagos")
def aprobar_pago(payload: EgresoCreate, db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    """TESORERÍA da la orden: crea el Comprobante de Egreso que paga 1..N compras
    (parcial o total). MISMA regla de negocio que POST /api/compras-contab/egresos
    (reusa `_crear_egreso`: locks anti doble-pago, tope por saldo, recálculo).

    Las DOS capas del arreglo del deadlock de la familia PAGOS:
      · CAPA 1 — `_porton_egresos_de_las_compras` alinea el orden de candados con el lado
        que REVIERTE pagos en Compras/CxP (que empieza por la fila del egreso).
      · CAPA 2 — `_con_retry_deadlock` reintenta la transacción completa y responde 409
        accionable en vez de 500 si el conflicto igual ocurre (ventana angosta del portón,
        o un 1205 por lock-timeout).
    La serialización de la respuesta queda AFUERA del reintento a propósito: un 1213 al
    serializar no debe reejecutar una transacción ya aplicada. (Deuda conocida: `conciliar`
    todavía envuelve su serialización dentro del retry.)"""
    empresa = empresa_de(current_user)
    fecha = _fecha(payload.fecha, "fecha") or date.today()
    egreso = _con_retry_deadlock(
        db, lambda: _aprobar_pago_tx(payload, empresa, fecha, db, current_user))
    db.refresh(egreso)
    return serialize_egreso(egreso)


# ═══ 1b. APROBACIÓN DE ADELANTOS DE CLIENTE (la orden la da Tesorería) ═══════════
def _venta_info_adelanto(db, adelantos: list) -> dict:
    """{adelanto_id: info de la venta} para la cola de aprobaciones (sin N+1)."""
    if not adelantos:
        return {}
    oc_ids = {a.oc_cliente_id for a in adelantos}
    ocs = {o.id: o for o in db.query(OcCliente).filter(OcCliente.id.in_(oc_ids)).all()}
    out = {}
    for a in adelantos:
        oc = ocs.get(a.oc_cliente_id)
        cot = oc.cotizacion if oc else None
        out[a.id] = {
            "numero_oc": oc.numero_oc if oc else None,
            "numero_cotizacion": cot.numero if cot else None,
            "cliente": (cot.cliente if cot else None) or "",
            "rut_cliente": (cot.rut_cliente if cot else None) or "",
        }
    return out


@router.get("/aprobaciones")
def aprobaciones(page: int = 1, page_size: int = PAGE_SIZE_DEFAULT,
                 db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    """Cola de aprobación de adelantos de cliente:
      · por_aprobar: informados por Comercial (con monto esperado y, si calza ±TOL, el
        abono de la cartola sugerido — informativo: aprobar NO exige cartola).
      · aprobadas: adelantos ya aprobados (con conciliado_banco y aplicado derivados),
        PAGINADOS. Antes se truncaban a los 50 más nuevos sin decirlo: un adelanto
        antiguo aún sin conciliar quedaba INVISIBLE en la pantalla que debe cuadrarlo
        con el banco. Ahora se informan `aprobadas_total` / `page` / `page_size` para que
        la UI pueda pedir el resto (los nombres de las dos listas no cambian: aditivo)."""
    empresa = empresa_de(current_user)
    page = max(1, int(page))
    page_size = min(max(1, int(page_size)), PAGE_SIZE_MAX)
    informados = (db.query(ContAdelanto)
                  .filter(ContAdelanto.empresa == empresa,
                          ContAdelanto.estado == "informado")
                  .order_by(ContAdelanto.id.asc()).all())
    base_apr = (db.query(ContAdelanto)
                .filter(ContAdelanto.empresa == empresa,
                        ContAdelanto.estado == "aprobado"))
    aprobadas_total = base_apr.count()
    aprobados = (base_apr.order_by(ContAdelanto.id.desc())
                 .offset((page - 1) * page_size).limit(page_size).all())
    info = _venta_info_adelanto(db, informados + aprobados)
    conciliados = _adelantos_conciliados_ids(db, [a.id for a in aprobados])

    # Abono sugerido para cada informado: abono sin conciliar con monto ≈ esperado.
    abonos = (db.query(MovimientoBancario)
              .filter(MovimientoBancario.empresa == empresa,
                      MovimientoBancario.tipo == "abono",
                      MovimientoBancario.conciliado.is_(False)).all())

    def _abono_sugerido(monto_esperado: float):
        if monto_esperado <= 0:
            return None
        for m in abonos:
            if abs(_f(m.monto) - monto_esperado) <= TOL:
                return {"movimiento_id": m.id, "fecha": m.fecha.isoformat() if m.fecha else None,
                        "monto": _f(m.monto), "glosa": m.glosa, "referencia": m.referencia}
        return None

    return {
        "por_aprobar": [{
            **_serialize_adelanto(a),
            **info.get(a.id, {}),
            "abono_sugerido": _abono_sugerido(_f(a.monto_esperado)),
        } for a in informados],
        "aprobadas": [{
            **_serialize_adelanto(a, a.id in conciliados),
            **info.get(a.id, {}),
        } for a in aprobados],
        "por_aprobar_total": len(informados),
        "aprobadas_total": int(aprobadas_total),
        "page": page, "page_size": page_size,
    }


@router.post("/adelantos/{adelanto_id}/aprobar")
def aprobar_adelanto(adelanto_id: int, payload: AprobarAdelantoIn,
                     db: Session = Depends(get_db),
                     current_user: User = Depends(get_current_user)):
    """TESORERÍA aprueba el adelanto: confirma la plata recibida (monto real, fecha,
    banco, N° operación) SIN exigir cartola — la conciliación con el abono es un paso
    posterior. Al aprobar, el adelanto se APLICA de inmediato a las facturas existentes
    de la venta (cobranza medio='adelanto'; vía A a las del despacho real, vía B a su
    factura de anticipo y, saldado el anticipo, su excedente al despacho real).
    Re-aprobar (corregir) exige revertir antes la aplicación y la conciliación,
    espejo de Monza."""
    empresa = empresa_de(current_user)
    # Orden GLOBAL de locks: OC → adelanto → facturas (el mismo de crear_factura en
    # contabilidad). Tomar primero el adelanto invertía el orden y dos requests
    # simultáneos (aprobar aquí / emitir factura allá) podían terminar en deadlock.
    # Por eso: lectura SIN lock para ubicar la OC, lock de la OC, y recién ahí el
    # lock del adelanto re-leyendo con datos frescos.
    ref = (db.query(ContAdelanto)
           .filter(ContAdelanto.id == adelanto_id, ContAdelanto.empresa == empresa)
           .first())
    if not ref:
        raise HTTPException(404, "Adelanto no encontrado")
    # La OC es el PORTÓN de serialización del orden global de locks: no se lee ni se
    # persiste ningún monto de esta fila, así que la regla 3 (populate_existing) no aplica.
    oc = (db.query(OcCliente).filter(OcCliente.id == ref.oc_cliente_id)
          .with_for_update().first())
    if not oc or not oc.cotizacion:
        raise HTTPException(404, "Venta (OC) del adelanto no encontrada")
    # populate_existing: sin él, el identity map devolvería la lectura previa al lock
    adel = (db.query(ContAdelanto)
            .filter(ContAdelanto.id == adelanto_id, ContAdelanto.empresa == empresa)
            .populate_existing().with_for_update().first())
    if not adel:
        raise HTTPException(404, "Adelanto no encontrado")
    if adel.estado == "anulado":
        raise HTTPException(409, "El adelanto está anulado; infórmalo de nuevo desde Ventas")
    if _f(adel.monto_aplicado) > 1.0:  # TOL_PAGO de contabilidad
        raise HTTPException(409, "El adelanto ya fue aplicado a una factura; revierta esa cobranza antes de modificarlo")
    if _adelantos_conciliados_ids(db, [adel.id]):
        raise HTTPException(409, "El adelanto ya está conciliado con un abono; desconcílielo antes de modificarlo")
    _validar_tope_adelantos(db, oc, _f(payload.monto), excluir_id=adel.id)

    adel.estado = "aprobado"
    adel.monto = round(_f(payload.monto), 2)
    adel.fecha_pago = _fecha(payload.fecha_pago, "fecha_pago") or date.today()
    adel.banco = payload.banco
    adel.numero_operacion = payload.numero_operacion
    if payload.observaciones is not None:
        adel.observaciones = payload.observaciones
    adel.usuario_aprueba_id = getattr(current_user, "id", None)
    adel.fecha_aprobacion = datetime.now(timezone.utc)
    db.flush()

    # Aplicación inmediata a las facturas que ya existen (si la factura viene después,
    # la aplica crear_factura). Lock por factura: serializa contra cobranzas concurrentes.
    # Anticipos PRIMERO: al saldarse liberan el excedente del adelanto ligado para las
    # facturas del despacho real en esta misma pasada (el lock de la OC ya serializa
    # esta transacción, así que el orden de los locks por factura no arriesga deadlock).
    # COALESCE (paridad con monza_contabilidad/router.py y monza_tesoreria/router.py): en
    # DESC MySQL manda los NULL al FINAL, así que una fila legada con es_anticipo NULL
    # (tabla nacida de create_all — el modelo solo tiene default=0 de lado Python, sin
    # server_default) perdería contra un 0 y el orden quedaría INVERTIDO: la plata del
    # adelanto entraría a la factura equivocada. El normalizador de tesoreria/init_db.py
    # deja la columna NOT NULL DEFAULT 0 y el ORM siempre escribe 0: esto es cinturón y
    # tirantes.
    # populate_existing (regla de lecturas de plata, la MISMA que ya cumple el lock del
    # adelanto de más arriba): el FOR UPDATE trae la fila fresca A CUALQUIER NIVEL DE
    # AISLAMIENTO (el motor está fijado en READ COMMITTED, database.py), pero si el objeto
    # ya está en el identity map SQLAlchemy DESCARTA esos valores y sirve el snapshot
    # viejo — el problema es el identity map, no el read view de MySQL. Acá decide plata:
    # `_aplicar_adelantos_pendientes` mira
    # saldo/monto_pagado/es_anticipo de estas facturas para repartir el adelanto, así que
    # con un saldo cacheado el mismo depósito se aplicaría dos veces.
    facturas = (db.query(ContFacturaCliente)
                .filter(ContFacturaCliente.oc_cliente_id == oc.id,
                        ContFacturaCliente.empresa == empresa)
                .order_by(func.coalesce(ContFacturaCliente.es_anticipo, 0).desc(),
                          ContFacturaCliente.id.asc())
                .populate_existing().with_for_update().all())
    aplicado_total = 0.0
    for f in facturas:
        aplicado_total += _aplicar_adelantos_pendientes(
            db, oc, f, usuario_id=getattr(current_user, "id", None))
    db.commit()
    db.refresh(adel)
    return {
        **_serialize_adelanto(adel),
        **_venta_info_adelanto(db, [adel]).get(adel.id, {}),
        "aplicado_ahora_clp": round(aplicado_total, 2),
    }


@router.get("/adelantos-pendientes")
def adelantos_pendientes(db: Session = Depends(get_db),
                         current_user: User = Depends(get_current_user)):
    """Adelantos APROBADOS aún sin conciliar con un abono del banco (para emparejar a
    mano en la pestaña de conciliación)."""
    empresa = empresa_de(current_user)
    aprobados = (db.query(ContAdelanto)
                 .filter(ContAdelanto.empresa == empresa,
                         ContAdelanto.estado == "aprobado")
                 .order_by(ContAdelanto.id.desc()).all())
    conciliados = _adelantos_conciliados_ids(db, [a.id for a in aprobados])
    pendientes = [a for a in aprobados if a.id not in conciliados]
    amap = _adelanto_summaries(db, pendientes)
    return {"adelantos": [amap[a.id] for a in pendientes], "total": len(pendientes)}


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
      · abono → cobranzas (ingresos de caja) de Facturas y Cobranzas + adelantos de
        cliente APROBADOS. Se EXCLUYEN las cobranzas medio='adelanto': son la
        aplicación contable de un adelanto ya recibido (no un depósito nuevo); su
        plata se concilia por la vía abono↔adelanto."""
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

    # abono → cobranzas con monto ≈ y sin enlace previo (excluye medio='adelanto')
    pares = (db.query(ContCobranza, ContFacturaCliente)
             .join(ContFacturaCliente, ContFacturaCliente.id == ContCobranza.factura_id)
             .filter(ContFacturaCliente.empresa == empresa,
                     ContCobranza.medio != MEDIO_ADELANTO,
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
    sugs = [{**cmap[c.id], "dias_diferencia": _dist_c((c, f))} for c, f in top]

    # + adelantos APROBADOS sin conciliar con monto ≈ (clase 'adelanto')
    adelantos = (db.query(ContAdelanto)
                 .filter(ContAdelanto.empresa == empresa,
                         ContAdelanto.estado == "aprobado",
                         ContAdelanto.monto >= monto - TOL,
                         ContAdelanto.monto <= monto + TOL).all())
    ya_a = _adelantos_conciliados_ids(db, [a.id for a in adelantos])
    adelantos = [a for a in adelantos if a.id not in ya_a]

    def _dist_a(a):
        return abs((a.fecha_pago - ref).days) if a.fecha_pago else 9999

    adelantos.sort(key=_dist_a)
    top_a = adelantos[:15]
    amap = _adelanto_summaries(db, top_a)
    sugs += [{**amap[a.id], "dias_diferencia": _dist_a(a)} for a in top_a]
    sugs.sort(key=lambda s: s.get("dias_diferencia", 9999))
    return {"movimiento_id": mov.id, "monto": monto, "sugerencias": sugs[:15]}


@router.post("/movimientos/{mov_id}/conciliar")
def conciliar(mov_id: int, payload: ConciliarIn, db: Session = Depends(get_db),
              current_user: User = Depends(get_current_user)):
    """Enlaza el movimiento con su destino (montos deben coincidir ±tolerancia):
      · cargo + egreso_id   → marca ambos conciliados y completa en el egreso la
        fecha/ref bancaria desde la cartola.
      · abono + cobranza_id → crea el enlace (el 'conciliado' de la cobranza se deriva);
        marca el movimiento conciliado. Rechaza cobranzas medio='adelanto' (su plata
        se concilia por la vía abono↔adelanto; conciliarlas duplicaría el depósito).
      · abono + adelanto_id → enlace con un adelanto de cliente APROBADO (1:1).

    CAPA 2 del arreglo del deadlock: el cuerpo vive en `_conciliar_tx` para poder
    reintentarlo COMPLETO si InnoDB elige esta transacción como víctima (1213) contra
    Facturas y Cobranzas. Mismo patrón que monza_tesoreria/router.py."""
    return _con_retry_deadlock(db, lambda: _conciliar_tx(mov_id, payload, db, current_user))


def _conciliar_tx(mov_id: int, payload: ConciliarIn, db: Session, current_user: User):
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
        # populate_existing: LECTURA DE PLATA bajo lock (regla 3). Abajo se compara
        # `monto_total_clp` ±TOL contra el movimiento y se lee el guard `conciliado`; con la
        # fila cacheada por una lectura previa del request, el lock serializa pero los
        # valores serían viejos (el mismo pago se conciliaría contra dos cargos).
        egreso = (db.query(ContEgreso)
                  .filter(ContEgreso.id == payload.egreso_id, ContEgreso.empresa == empresa)
                  .populate_existing().with_for_update().first())
        if not egreso:
            raise HTTPException(404, "Egreso no encontrado")
        if egreso.conciliado:
            raise HTTPException(409, "Ese egreso ya está conciliado con otro movimiento")
        if abs(_f(mov.monto) - _f(egreso.monto_total_clp)) > TOL:
            raise HTTPException(400, f"Los montos no coinciden (movimiento {_f(mov.monto):.0f} vs egreso {_f(egreso.monto_total_clp):.0f})")
        # Snapshot ANTES de enriquecer: mientras el egreso está conciliado la cartola
        # es la fuente de verdad (fecha/ref del banco pisan lo del egreso), pero
        # desconciliar debe poder RESTAURAR lo que el operador tenía ingresado.
        db.add(Conciliacion(empresa=empresa, movimiento_id=mov.id, egreso_id=egreso.id,
                            monto_conciliado_clp=_f(mov.monto), usuario_id=uid,
                            fecha_egreso_previa=egreso.fecha_mov_bancario,
                            referencia_egreso_previa=egreso.referencia_bancaria))
        egreso.conciliado = True
        egreso.conciliado_at = now
        if mov.fecha:
            egreso.fecha_mov_bancario = mov.fecha
        if mov.referencia:
            egreso.referencia_bancaria = mov.referencia
        destino_fn = lambda: _egreso_summary(db, egreso)  # noqa: E731
    elif payload.adelanto_id:
        if mov.tipo != "abono":
            raise HTTPException(400, "Un adelanto de cliente se concilia contra un ABONO del banco")
        # populate_existing: LECTURA DE PLATA bajo lock (regla 3). Es la MISMA entidad que
        # protege el invariante `monto_aplicado == Σ cobranzas medio='adelanto'`: abajo se
        # leen `estado` y `monto` (±TOL contra el abono). Con la fila cacheada, un adelanto
        # recién aprobado/corregido por Tesorería se conciliaría contra el monto anterior.
        adel = (db.query(ContAdelanto)
                .filter(ContAdelanto.id == payload.adelanto_id,
                        ContAdelanto.empresa == empresa)
                .populate_existing().with_for_update().first())
        if not adel:
            raise HTTPException(404, "Adelanto no encontrado")
        if adel.estado != "aprobado":
            raise HTTPException(409, "Solo se concilian adelantos APROBADOS por Tesorería")
        if _adelantos_conciliados_ids(db, [adel.id]):
            raise HTTPException(409, "Ese adelanto ya está conciliado con otro movimiento")
        if abs(_f(mov.monto) - _f(adel.monto)) > TOL:
            raise HTTPException(400, f"Los montos no coinciden (movimiento {_f(mov.monto):.0f} vs adelanto {_f(adel.monto):.0f})")
        db.add(ConciliacionIngreso(empresa=empresa, movimiento_id=mov.id, adelanto_id=adel.id,
                                   monto_conciliado_clp=_f(mov.monto), usuario_id=uid))
        destino_fn = lambda: _adelanto_summaries(db, [adel])[adel.id]  # noqa: E731
    else:
        if mov.tipo != "abono":
            raise HTTPException(400, "Una cobranza (ingreso de caja) se concilia contra un ABONO del banco")
        # Lock de cobranza Y factura: el recálculo de saldos en Facturas también
        # bloquea la factura, así que esto serializa contra cobranzas concurrentes.
        #
        # CAPA 1 — ORDEN DE LOCKS. Antes esto era UN JOIN con with_for_update(): la tabla
        # motriz era la COBRANZA (se busca por su PK) y la factura se bloqueaba DESPUÉS,
        # o sea AL REVÉS que Facturas y Cobranzas, que bloquea primero la FACTURA y luego
        # sus cobranzas (routers/contabilidad.py: registrar_cobranza / eliminar_cobranza
        # con `_cobranzas_bloqueadas`). Las dos operaciones formaban un ciclo InnoDB y el
        # operador recibía un 500 por deadlock (1213) en una operación diaria — y en todo
        # este módulo no había un solo reintento.
        # Ahora se sigue el orden canónico de la casa (OC → factura → cobranza) en tres
        # pasos y, como al soltar el JOIN atómico se abre una ventana entre la lectura sin
        # lock y el lock real, el paso 4 REVALIDA con los datos frescos (no es opcional).
        #
        # 1) lectura SIN lock, solo para saber a qué factura pertenece la cobranza. Se
        #    pide la columna suelta (no la entidad) para no dejar la fila en el identity
        #    map con valores viejos que la relectura bajo lock no pisaría.
        factura_id_previa = (db.query(ContCobranza.factura_id)
                             .filter(ContCobranza.id == payload.cobranza_id).scalar())
        if factura_id_previa is None:
            raise HTTPException(404, "Cobranza no encontrada")
        # 2) PRIMERO la factura (mismo orden que Facturas y Cobranzas). El filtro por
        #    empresa es el mismo candado que traía el JOIN: una cobranza de otra marca
        #    responde 404, no se concilia. populate_existing por la regla 3 (lecturas de
        #    plata): esta fila viaja al resumen del destino que ve el tesorero (folio de la
        #    factura del cruce) y una copia cacheada le mostraría el dato viejo justo
        #    mientras Facturas y Cobranzas la está corrigiendo.
        _factura = (db.query(ContFacturaCliente)
                    .filter(ContFacturaCliente.id == factura_id_previa,
                            ContFacturaCliente.empresa == empresa)
                    .populate_existing().with_for_update().first())
        if not _factura:
            raise HTTPException(404, "Cobranza no encontrada")
        # 3) DESPUÉS la cobranza (populate_existing: sin él el identity map devolvería la
        #    lectura del paso 1 y descartaría los valores frescos que trajo el FOR UPDATE
        #    — regla de lecturas de plata).
        cobranza = (db.query(ContCobranza)
                    .filter(ContCobranza.id == payload.cobranza_id)
                    .populate_existing().with_for_update().first())
        if not cobranza:
            raise HTTPException(404, "Cobranza no encontrada")
        # 4) REVALIDACIÓN con los datos ya bloqueados: si la cobranza cambió de factura
        #    entre el paso 1 y el 3, la factura bloqueada no es la suya → se corta (los
        #    chequeos de medio / ya-conciliada / monto de abajo también leen la fila
        #    fresca, así que quedan cubiertos por el mismo lock).
        if cobranza.factura_id != _factura.id:
            raise HTTPException(409, "La cobranza cambió de factura mientras se conciliaba: reintente")
        if cobranza.medio == MEDIO_ADELANTO:
            raise HTTPException(409, "Esa cobranza es la APLICACIÓN de un adelanto (no un depósito nuevo): "
                                     "concilie el abono contra el ADELANTO correspondiente")
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
        # populate_existing (regla 3): acá no basta con que el lock serialice. Si el egreso
        # ya está en el identity map con la foto ANTERIOR al cruce (conciliado=False,
        # referencia del operador), SQLAlchemy sirve esa copia y las asignaciones de abajo
        # no cambian nada → NO emite UPDATE: el pago se queda marcado `conciliado` para
        # siempre (invisible en /egresos-pendientes) y la referencia del banco no se
        # revierte. El bug NO es del aislamiento de MySQL, es del identity map.
        eg = (db.query(ContEgreso)
              .filter(ContEgreso.id == link.egreso_id, ContEgreso.empresa == empresa)
              .populate_existing().with_for_update().first())
        if eg:
            eg.conciliado = False
            eg.conciliado_at = None
            # Se RESTAURA lo que el egreso tenía ANTES del cruce (snapshot guardado
            # en el enlace al conciliar): así no queda data del cruce equivocado y
            # se conserva lo que el operador ingresó a mano en Compras, incluso si
            # coincidía con lo del banco. Solo se toca el campo que la conciliación
            # pisó (mov con fecha/ref); en enlaces previos a la migración el snapshot
            # es NULL y el resultado es el mismo que la limpieza histórica.
            if mov.fecha:
                eg.fecha_mov_bancario = link.fecha_egreso_previa
            if mov.referencia:
                eg.referencia_bancaria = link.referencia_egreso_previa
        db.delete(link)
    for link in list(mov.conciliaciones_ingreso):
        # cobranza/adelanto: su 'conciliado' se deriva del enlace → basta borrarlo.
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
    abono del banco. El 'conciliado' se deriva del enlace en conc_conciliacion_ingreso.
    Excluye medio='adelanto' (aplicación de un adelanto, no un depósito nuevo)."""
    empresa = empresa_de(current_user)
    page = max(1, int(page))
    page_size = min(max(1, int(page_size)), PAGE_SIZE_MAX)
    ya_conciliadas = (db.query(ConciliacionIngreso.cobranza_id)
                      .filter(ConciliacionIngreso.cobranza_id.isnot(None)).scalar_subquery())
    base = (db.query(ContCobranza, ContFacturaCliente)
            .join(ContFacturaCliente, ContFacturaCliente.id == ContCobranza.factura_id)
            .filter(ContFacturaCliente.empresa == empresa,
                    ContCobranza.medio != MEDIO_ADELANTO,
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

    # Adelantos de cliente, INFORMATIVOS y APARTE de los buckets (regla Monza):
    #   · informados (por aprobar): aún NO son plata segura → no se proyectan.
    #   · aprobados no aplicados: plata YA recibida (no es entrada futura); se muestra
    #     para explicar por qué las facturas siguientes nacerán con menos saldo.
    adel_inf_n, adel_inf_monto = (
        db.query(func.count(ContAdelanto.id),
                 func.coalesce(func.sum(ContAdelanto.monto_esperado), 0))
        .filter(ContAdelanto.empresa == empresa, ContAdelanto.estado == "informado").one())
    adel_apr = (db.query(ContAdelanto)
                .filter(ContAdelanto.empresa == empresa,
                        ContAdelanto.estado == "aprobado").all())
    apr_pend = [a for a in adel_apr if _f(a.monto) - _f(a.monto_aplicado) > TOL]
    apr_pend_monto = sum(_f(a.monto) - _f(a.monto_aplicado) for a in apr_pend)

    return {
        "buckets": FLUJO_BUCKETS,
        "por_pagar": por_pagar,
        "por_cobrar": por_cobrar,
        "neto": neto,
        "retenciones_factoring": {"n": int(ret_n or 0), "monto": round(_f(ret_monto), 0)},
        "adelantos_por_aprobar": {"n": int(adel_inf_n or 0), "monto": round(_f(adel_inf_monto), 0)},
        "adelantos_recibidos_sin_aplicar": {"n": len(apr_pend), "monto": round(apr_pend_monto, 0)},
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
    # isnot(None): con la vía abono↔adelanto el enlace puede tener cobranza_id NULL, y
    # un NOT IN contra una lista con NULLs no matchea NADA en SQL (vaciaría el KPI).
    ya_conciliadas = (db.query(ConciliacionIngreso.cobranza_id)
                      .filter(ConciliacionIngreso.cobranza_id.isnot(None)).scalar_subquery())
    cobranzas_pend = (db.query(func.count(ContCobranza.id))
                      .join(ContFacturaCliente, ContFacturaCliente.id == ContCobranza.factura_id)
                      .filter(ContFacturaCliente.empresa == empresa,
                              ContCobranza.medio != MEDIO_ADELANTO,
                              ~ContCobranza.id.in_(ya_conciliadas)).scalar())
    # Adelantos de cliente: informados en cola + aprobados aún sin conciliar
    adel_inf_n, adel_inf_monto = (
        db.query(func.count(ContAdelanto.id),
                 func.coalesce(func.sum(ContAdelanto.monto_esperado), 0))
        .filter(ContAdelanto.empresa == empresa, ContAdelanto.estado == "informado").one())
    aprobados_ids = [a.id for a in db.query(ContAdelanto)
                     .filter(ContAdelanto.empresa == empresa,
                             ContAdelanto.estado == "aprobado").all()]
    adel_sin_conc = len(set(aprobados_ids) - _adelantos_conciliados_ids(db, aprobados_ids))
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
        "adelantos_por_aprobar": int(adel_inf_n or 0),
        "adelantos_por_aprobar_clp": round(_f(adel_inf_monto), 0),
        "adelantos_sin_conciliar": int(adel_sin_conc),
        "movimientos_total": int(total),
        "movimientos_conciliados": int(conciliados),
        "cargos_pendientes": int(pend_cargo),
        "abonos_pendientes": int(pend_abono),
        "egresos_sin_conciliar": int(egresos_pend or 0),
        "cobranzas_sin_conciliar": int(cobranzas_pend or 0),
        # compat con tarjetas antiguas
        "movimientos_pendientes": int(pend_cargo + pend_abono),
    }
