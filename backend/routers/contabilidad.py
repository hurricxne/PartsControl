"""Módulo de Contabilidad — Ventas + Facturas/Cobranzas (con Factoring).

Router montado en /api/contabilidad. Es el lado de CUENTAS POR COBRAR del ERP.
Documentación completa del módulo (flujo, endpoints, mapeo con el frontend):
ver docs/ventas-contabilidad.md.

Reutiliza lo que ya existe en la app (no lo reescribe):
  - Identidad y montos de la venta: Cotizacion + OcCliente + ItemCotizacion, con el
    precio de venta calculado por `pricing_service.calcular_cotizacion` (igual que ventas.py).
  - Despachos/guías: Despacho + DespachoItem (los ítems se despachan por partes).

Agrega las tablas cont_*: factura a cliente, sus líneas, cobranzas (pagos) y factoring.

Flujo extremo a extremo:
  venta (Cotizacion + OcCliente) → despacho creado en Despachos → guía CERRADA y luego
  FIRMADA (en despachos.py: Despacho.estado=='despachado' AND guia_firmada==1) →
  recién ahí se EMITE la factura de esos ítems → cobranzas / factoring → KPIs.

REGLA RECTORA: SOLO se factura una guía de despacho FIRMADA (entregada y firmada por el
cliente), nunca por encima de la cantidad despachada ni dos veces (doble tope por ítem y
por guía, con control de cantidad ya facturada). Ver `_despacho_items_de_oc`.
ÚNICA EXCEPCIÓN: la factura de ANTICIPO (es_anticipo=1) respalda un adelanto del cliente
ante el SII y se emite SIN guía; su neto se descuenta automáticamente (línea negativa)
de las facturas del despacho real, para que Σ facturas de la OC == total de la venta.

ADELANTOS (cont_adelanto): Comercial los INFORMA (Cierre de Venta / Ventas), Tesorería
los APRUEBA (tesoreria/router.py, confirma la plata sin exigir cartola) y aquí se
APLICAN como cobranza medio='adelanto' al emitir facturas (vía A: facturas del despacho;
vía B: la factura de anticipo que los respalda). Esa cobranza NO es un depósito nuevo:
se excluye de la conciliación bancaria de ingresos (la plata se concilia abono↔adelanto).

Endpoints (todos requieren autenticación):
  GET    /ventas                              listado de ventas por OC + resumen de cobranza
  GET    /ventas/{oc}                         detalle de una venta (ítems, guías, facturas)
  GET    /ventas/{oc}/despachos-facturables   guías firmadas aún facturables (selector "Emitir factura")
  GET    /facturas                            listado de facturas + antigüedad de cartera
  POST   /facturas                            EMITIR una factura (desde una guía firmada o ítems)
  DELETE /facturas/{id}                       borrado seguro (no si hay pagos/factoring)
  POST   /facturas/{id}/cobranzas             registrar un pago del cliente
  DELETE /facturas/{id}/cobranzas/{id}        revertir un pago
  POST   /facturas/{id}/factoring             ceder la factura a un factor (adelanto/retención)
  POST   /facturas/{id}/factoring/liquidar    liquidar el factoring (cierra el saldo a 0)
  POST   /facturas/{id}/factoring/revertir    revertir la cesión (sólo si el DTE nunca existió)
  GET    /kpis                                indicadores de cobranza (ver get_kpis)
  POST   /ventas/adelantos                    informar un adelanto (Comercial; acepta cotizacion_id)
  GET    /ventas/{oc}/adelantos               adelantos de la venta (con estados derivados)
  PATCH  /adelantos/{id}                      editar lo informado (aprobados: re-aprobar en Tesorería)
  POST   /adelantos/{id}/anular               anular (no aplicado ni conciliado)
"""
import json
import logging
from datetime import datetime, date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional, List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload, joinedload
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from pydantic import BaseModel, Field

from database import get_db
from models.models import (
    Cotizacion, ItemCotizacion, OcCliente, Despacho, DespachoItem,
    ConfiguracionCotizador, User, Cliente,
    ContFacturaCliente, ContFacturaClienteItem, ContCobranza, ContFactoring,
    ContAdelanto,
)
from auth import get_current_user
from empresa_guard import require_empresa
from role_guard import require_rol
from services.pricing_service import calcular_cotizacion, config_efectivo
# Solo lectura del enlace de conciliación de Tesorería (abono ↔ cobranza): una cobranza
# conciliada con el banco no se puede borrar sin desconciliarla primero allá.
from tesoreria.models import ConciliacionIngreso

logger = logging.getLogger("contabilidad")

# Módulo SOLO MachParts (Grupo AM = 'mineria'): el guard a nivel de router deniega (403)
# a usuarios de otra empresa que intenten llegar por la API por fuera de la app.
# Y candado de ROL (role_guard, mismo uso que routers/compras.py y monza_router_cotizaciones):
# aquí se EMITEN y ELIMINAN facturas, se registran/revierten cobranzas y se ceden facturas
# a un factor — bodega, logística y abastecimiento no tienen nada que hacer acá. Se admite
# 'comercial' porque el Cierre de Venta informa los adelantos y consulta /ventas, y
# 'gerencia' por los KPIs de cobranza. El guard es PERMISIVO mientras `User.rol` no exista
# (ver role_guard.py): hoy no cambia nada operativo y candará solo al provisionar roles.
router = APIRouter(
    prefix="/contabilidad",
    tags=["contabilidad"],
    dependencies=[
        Depends(require_empresa("mineria")),
        Depends(require_rol("admin", "gerencia", "contabilidad", "comercial")),
    ],
)

IVA_RATE = 0.19      # IVA Chile (19%); mantener sincronizado con pricing_service
TOL = 0.5            # tolerancia en CLP para clasificar saldos (pagada / al_día)
TOL_QTY = 0.001      # tolerancia para comparaciones de cantidades (unidades)
TOL_PAGO = 1.0       # holgura de 1 CLP en topes de pago/adelanto (redondeo de IVA/factoring)
DIAS_POR_VENCER = 7  # días para marcar una factura como 'por_vencer' en el semáforo
MEDIO_FACT_ADELANTO = "factoring_adelanto"
MEDIO_FACT_RETENCION = "factoring_retencion"
# Aplicación AUTOMÁTICA de un adelanto de cliente aprobado por Tesorería (cont_adelanto).
# NO es un depósito nuevo: su plata se concilia por la vía abono↔adelanto en Tesorería,
# por eso estas cobranzas se EXCLUYEN de la conciliación bancaria de ingresos.
MEDIO_ADELANTO = "adelanto"
ESTADOS_ADELANTO = ("informado", "aprobado", "anulado")

# Carga ansiosa (evita N+1): los serializadores/resúmenes recorren estas relaciones.
_FACTURA_EAGER = (
    selectinload(ContFacturaCliente.items),
    selectinload(ContFacturaCliente.cobranzas),
    selectinload(ContFacturaCliente.factoring),
    joinedload(ContFacturaCliente.despacho),
    joinedload(ContFacturaCliente.oc_cliente).joinedload(OcCliente.cotizacion),
)


def _es_medio_factoring(medio: Optional[str]) -> bool:
    return bool(medio and medio.startswith("factoring"))


# ─── Helpers básicos ──────────────────────────────────────────────────────────
def _cfg_to_dict(cfg) -> dict:
    if cfg is None:
        return {}
    return {
        "tipo_cambio_usd": cfg.tipo_cambio_usd,
        "costo_shipping_usd_kg": cfg.costo_shipping_usd_kg,
        "adicionales_shipping_usd": cfg.adicionales_shipping_usd,
        "costo_agencia_pct": cfg.costo_agencia_pct,
        "costo_agencia_minimo_clp": cfg.costo_agencia_minimo_clp,
        "desconsolidado_clp": cfg.desconsolidado_clp,
        "bodegaje_clp": cfg.bodegaje_clp,
        "margen_venta_pct": cfg.margen_venta_pct,
    }


def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    if isinstance(s, date):
        return s
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def _f(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


# ─── Dinero / fecha / RUT — criterio ÚNICO compartido con la guía electrónica ──
# (wasabil_dte). Que la factura 33 y la guía 52 del mismo despacho CUADREN exige
# redondear IGUAL: precio unitario a 2 decimales ANTES de multiplicar, y half-up
# (no el round() banker's de Python) a peso en la línea y en el IVA.
def _precio2(precio) -> float:
    """Precio unitario neto redondeado a 2 decimales (base común guía/factura)."""
    return round(_f(precio), 2)


def _total_linea(precio, cantidad) -> float:
    """Total neto de una línea: precio(2 dec) × cantidad, half-up a peso (== guía/SII)."""
    p2 = _precio2(precio)
    return float(Decimal(str(p2 * cantidad)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _iva_clp(neto) -> float:
    """IVA 19% a peso con half-up (== SII / guía electrónica)."""
    return float(Decimal(str(_f(neto) * IVA_RATE)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _hoy_chile() -> date:
    """Fecha de hoy en Chile (America/Santiago) — la fecha tributaria del documento.
    El server en producción corre en UTC: pasadas las ~20-21h en Chile, date.today()
    ya sería 'mañana'. Mismo criterio que wasabil_dte.service.hoy_chile()."""
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/Santiago")).date()


def _rut_saneado(rut: Optional[str]) -> str:
    return (rut or "").replace(".", "").replace(" ", "").strip().upper()


def _rut_canonico(rut: Optional[str]) -> str:
    """Forma canónica para COMPARAR RUTs (cuerpo+DV, sin puntos ni guión): así
    '78.279.030-7' y '782790307' son el mismo RUT. Para mostrar se usa el original."""
    return _rut_saneado(rut).replace("-", "")


def _rut_valido(rut: Optional[str]) -> bool:
    """Valida RUT chileno (cuerpo + dígito verificador, módulo 11). Acepta con o sin
    puntos/guión. No bloquea por puntos: el SII/Wasabil exige un RUT bien formado."""
    r = _rut_saneado(rut)
    if "-" in r:
        cuerpo, _, dv = r.partition("-")
    else:
        cuerpo, dv = r[:-1], r[-1:]
    if not cuerpo.isdigit() or len(cuerpo) < 7 or not dv:
        return False
    suma, factor = 0, 2
    for d in reversed(cuerpo):
        suma += int(d) * factor
        factor = 2 if factor == 7 else factor + 1
    resto = 11 - (suma % 11)
    dv_calc = "0" if resto == 11 else "K" if resto == 10 else str(resto)
    return dv == dv_calc


def _precios_de_cotizacion(db: Session, cot_id: int, cfg_dict: dict, items_db=None):
    """(items_db, {item_id: dict_calc}, totales) reutilizando pricing_service.
    SOLO precio de venta (sin costo: alcance de fase posterior).
    `items_db` permite pasar los ítems ya precargados en lote (anti N+1)."""
    if items_db is None:
        items_db = (
            db.query(ItemCotizacion)
            .filter(ItemCotizacion.cotizacion_id == cot_id)
            .all()
        )
    item_dicts = [
        {
            "id": i.id,
            "cantidad": i.cantidad or 0,
            "precio_unit_cotizacion": i.precio_unit_cotizacion or 0,
            "peso_unit_lbs": i.peso_unit_lbs or 0,
            "margen_pct": i.margen_pct,
        }
        for i in items_db
    ]
    _cot = items_db[0].cotizacion if items_db else None
    _cfg = config_efectivo(getattr(_cot, "pricing_snapshot", None), cfg_dict)
    calc = calcular_cotizacion(item_dicts, {**_cfg, "origen": (_cot.origen if _cot else None) or "costo"})
    pmap = {ci["id"]: ci for ci in calc.get("items", [])}
    return items_db, pmap, calc.get("totales", {})


# ─── Helpers de integración con despachos (solo despachos 'despachado') ────────
def _despacho_items_de_oc(db: Session, oc_id: int):
    """DespachoItems facturables de la OC: despachos cerrados Y con la guía FIRMADA
    (entregada y firmada por el cliente). Solo eso es facturable."""
    return (
        db.query(DespachoItem, Despacho)
        .join(Despacho, Despacho.id == DespachoItem.despacho_id)
        .filter(
            Despacho.oc_cliente_id == oc_id,
            Despacho.estado == "despachado",
            Despacho.guia_firmada == 1,
        )
        .all()
    )


def _qty_despachada_por_item(db: Session, oc_id: int) -> dict:
    out = {}
    for di, _d in _despacho_items_de_oc(db, oc_id):
        out[di.item_cotizacion_id] = out.get(di.item_cotizacion_id, 0.0) + _f(di.qty_despachada)
    return out


def _qty_facturada_por_item(db: Session, oc_id: int) -> dict:
    rows = (
        db.query(ContFacturaClienteItem.item_cotizacion_id, ContFacturaClienteItem.cantidad)
        .join(ContFacturaCliente, ContFacturaCliente.id == ContFacturaClienteItem.factura_id)
        .filter(ContFacturaCliente.oc_cliente_id == oc_id)
        .all()
    )
    out = {}
    for iid, qty in rows:
        out[iid] = out.get(iid, 0.0) + _f(qty)
    return out


def _qty_facturada_por_despacho_item(db: Session, oc_id: int) -> dict:
    rows = (
        db.query(ContFacturaClienteItem.despacho_item_id, ContFacturaClienteItem.cantidad)
        .join(ContFacturaCliente, ContFacturaCliente.id == ContFacturaClienteItem.factura_id)
        .filter(
            ContFacturaCliente.oc_cliente_id == oc_id,
            ContFacturaClienteItem.despacho_item_id.isnot(None),
        )
        .all()
    )
    out = {}
    for did, qty in rows:
        out[did] = out.get(did, 0.0) + _f(qty)
    return out


# ─── Estado / saldo de la factura ─────────────────────────────────────────────
def _semaforo(fecha_venc: Optional[date], saldo: float) -> str:
    """Semáforo de vencimiento de una factura según su saldo y fecha de vencimiento.
    Devuelve: al_dia (saldada) | sin_fecha | vencida | por_vencer (<= DIAS_POR_VENCER) | vigente."""
    if saldo <= TOL:
        return "al_dia"
    if not fecha_venc:
        return "sin_fecha"
    dias = (fecha_venc - date.today()).days
    if dias < 0:
        return "vencida"
    if dias <= DIAS_POR_VENCER:
        return "por_vencer"
    return "vigente"


def _estado_pago(factura: ContFacturaCliente, pagado: float, saldo: float) -> str:
    """El saldo manda: si está saldada → 'pagada'. Solo con saldo pendiente y
    factoring vigente → 'factorizada'."""
    if saldo <= TOL:
        return "pagada"
    fac = factura.factoring
    if fac and fac.estado == "vigente":
        return "factorizada"
    venc = factura.fecha_vencimiento
    vencida = bool(venc and venc < date.today())
    if pagado > TOL:
        return "vencida" if vencida else "parcial"
    return "vencida" if vencida else "por_cobrar"


# NOTA sobre `db.refresh(factura, with_for_update=True)` (9 usos en este módulo):
# el `with_for_update` NO es por el candado sino por la FRESCURA. Un refresh normal es
# una lectura PLANA y, bajo REPEATABLE READ, devuelve la versión del snapshot que abrió
# la primera sentencia del request (el SELECT de usuarios del guard de empresa). Eso
# repoblaba la factura con montos VIEJOS justo antes de recalcularlos; y cuando el valor
# recalculado coincidía con ese valor viejo, SQLAlchemy no veía cambio y NO emitía el
# UPDATE — dejando en la base un monto_pagado/saldo que ninguna lectura posterior
# corrige (p.ej. saldo 0 con una sola cobranza de la mitad). La lectura bloqueante
# siempre trae la última versión commiteada.
def _cobranzas_bloqueadas(db: Session, factura_id: int) -> list:
    """Cobranzas de la factura leídas BAJO LOCK — es decir, la última versión commiteada.

    Por qué no basta `factura.cobranzas`: esa relación perezosa es una lectura PLANA, y
    bajo REPEATABLE READ toda lectura plana sirve el snapshot que abrió la PRIMERA
    sentencia del request. Esa primera sentencia es el `db.query(User)` del guard de
    empresa del router, ANTERIOR a cualquier `with_for_update()`. Resultado: el lock
    serializa bien, pero el tope calculado sobre la relación NO ve lo que la transacción
    gemela acaba de commitear (dos cobranzas simultáneas pasaban ambas). Una lectura
    bloqueante sí ve lo último. Usar en todo punto que DECIDE sobre plata.
    """
    return (db.query(ContCobranza)
            .filter(ContCobranza.factura_id == factura_id)
            .populate_existing().with_for_update().all())


def _factoring_bloqueado(db: Session, factura_id: int):
    """Factoring de la factura leído BAJO LOCK (mismo motivo que _cobranzas_bloqueadas:
    sin esto, un adelanto podía entrar a una factura recién cedida al factor)."""
    return (db.query(ContFactoring)
            .filter(ContFactoring.factura_id == factura_id)
            .populate_existing().with_for_update().first())


def _dte_emitido_ante_sii(dte) -> bool:
    """ÚNICA definición de «este documento tributario EXISTE y está IDENTIFICADO ante el
    SII»: status EMITIDO **Y** folio. Las dos condiciones, no una.

    Por qué el folio también (el bug que cerró esto): el criterio original era sólo
    `status_id != 3`, así que el estado CONTRADICTORIO «status 3 · folio NULL» —Wasabil
    dice que el SII lo aceptó pero no tenemos el correlativo— contaba como emitido y la
    plata entraba: cobranza 200 y factoring 200 contra una factura sin folio en ninguna
    parte (ni en `numero_factura`, que se escribe justamente DESDE ese folio, ni en el
    DTE). Y el módulo de al lado ya trataba ese mismo estado como NO referenciable para
    la 52/33 (wasabil_dte/router.py: `_guia_no_referenciable`, "status 3 y folio vacío"),
    o sea: dos criterios distintos para el MISMO estado, en el mismo commit.

    Regla de la casa ante un documento tributario IRREVERSIBLE: si el estado remoto es
    ambiguo o contradictorio, se BLOQUEA y se pide intervención humana. Un 409 que obliga
    a mirar es más barato que una nota de crédito. La salida del operador es «Reintentar»
    (consulta a Wasabil y adopta el folio) — apenas el folio existe, todo se acepta.

    `getattr` en `folio`: en un esquema anterior a la columna, la ausencia se lee como
    "sin folio" → bloquea. Fail CLOSED, nunca al revés."""
    from wasabil_dte.models import STATUS_EMITIDO
    return (dte.status_id == STATUS_EMITIDO
            and bool(str(getattr(dte, "folio", "") or "").strip()))


def _dte_factura_no_emitido(db: Session, factura_id: int) -> bool:
    """True si la factura tiene un DTE electrónico (factura 33) que AÚN no está emitido
    ante el SII (borrador, en vuelo, procesando, rechazado, o EMITIDO SIN FOLIO) — o sea:
    plata que NO debe entrar todavía. Única fuente de verdad del guard SII de la plata de
    este módulo; el criterio de "emitido" es _dte_emitido_ante_sii y sólo ese.

    Import LOCAL del paquete wasabil_dte (mismo patrón que _precios_congelados_guia):
    evita el ciclo de imports contabilidad ↔ wasabil y no carga el módulo DTE en los
    caminos que no lo necesitan.

    Tolerancias, deliberadamente ASIMÉTRICAS (el porqué de cada una — molde
    monza_contabilidad/router.py):
      · Módulo ausente o modelo sin la columna `factura_id` (código anterior a la Fase B)
        → False: si el código no existe, no puede existir una factura electrónica que
        proteger.
      · Error de ESQUEMA en la BD (tabla/columna faltante por un deploy A MEDIAS) → 503
        explícito, NUNCA False. Aquí sí puede haber facturas electrónicas vivas, y apagar
        el guard en silencio movería plata contra un documento que el SII no conoce. Se
        falla ruidoso pidiendo correr el init_db, que es la regla de la casa. Antes esto
        era un 500 crudo de SQLAlchemy sin pista de qué hacer.
        Sin db.rollback(): este helper corre DENTRO de crear_factura, con la factura y
        sus líneas ya flusheadas.
    """
    try:
        from wasabil_dte.models import WasabilDte
    except ImportError:
        return False
    col = getattr(WasabilDte, "factura_id", None)
    if col is None:
        return False
    try:
        dtes = db.query(WasabilDte).filter(col == factura_id).all()
    except (ProgrammingError, OperationalError) as e:
        logger.error("Guard SII de la plata: esquema de wasabil_dte incompleto: %s", e)
        raise HTTPException(
            503,
            "El módulo de facturas electrónicas está a medio instalar: corre "
            "backend/wasabil_dte/init_db.py y reinicia el backend",
        ) from e
    # `.all()` + `any(...)` en vez de `.first()`: hoy el UniqueConstraint("factura_id") de
    # wasabil_dte deja a lo más UNA fila, así que es la misma consulta; si esa llave
    # desapareciera, CUALQUIER fila no emitida bloquea la plata (fail closed) en vez de
    # decidir con la primera que devuelva la BD.
    return any(not _dte_emitido_ante_sii(d) for d in dtes)


def _plata_bloqueada_por_sii(db: Session, factura: ContFacturaCliente) -> bool:
    """True si NO debe entrar plata contra esta factura porque su documento electrónico
    todavía no existe ante el SII. PUERTA ÚNICA del guard: la usan los TRES caminos por
    los que entra plata a una factura —cobranza manual (registrar_cobranza), cesión al
    factor (set_factoring/liquidar_factoring) y aplicación de adelantos
    (_aplicar_adelantos_pendientes)—, así que agregar un cuarto camino de plata es
    agregar una línea, no re-derivar la regla.

    Por qué existe como helper y no como 4 líneas repetidas: la primera versión del guard
    vivió sólo en registrar_cobranza y el factoring —la misma plata, la función de al
    lado— quedó abierto: se podía ceder al factor una factura que el SII nunca conoció,
    dejarla 'factorizada' (con lo que la aplicación automática de adelantos devuelve 0) y
    encima IMBORRABLE (eliminar_factura rechaza toda factura con factoring), secuestrando
    para siempre el cupo facturable de esa mercadería.

    Sólo se consulta al módulo DTE cuando NO hay folio Y el documento es FACTURA: una
    factura con folio ya está ante el SII y una BOLETA jamás tiene un DTE 33, así que ni
    una ni otra se exponen a un 503 gratuito en una BD sin migrar.

    El criterio de "el documento ya existe ante el SII" NO se re-deriva acá: es
    _dte_emitido_ante_sii (status EMITIDO **y** folio), el mismo que usa el módulo que
    arma las referencias tributarias. Una sola definición, tres puertas de plata."""
    return ((factura.tipo_doc or "factura") == "factura"
            and not (factura.numero_factura or "").strip()
            and _dte_factura_no_emitido(db, factura.id))


def _exigir_sii_emitido(db: Session, factura: ContFacturaCliente, accion: str,
                        salida: Optional[str] = None) -> None:
    """409 accionable si la factura electrónica aún no tiene folio del SII. `accion` es la
    frase que cierra el mensaje ('registrar pagos', 'cederla al factoring', …) para que el
    operador sepa qué quedó sin hacer. La salida siempre es la misma: esperar el folio o
    usar «Reintentar» (que consulta a Wasabil). Apenas el SII confirma, la operación se
    acepta sin más trámite.

    `salida`: frase EXTRA para los caminos donde «Reintentar» puede no alcanzar. Existe
    porque el guard de la liquidación, sin ella, era un callejón: si el SII nunca va a
    confirmar ese documento, el operador quedaba sin ninguna acción posible (ver
    revertir_factoring). Un 409 sin salida es un ticket de soporte garantizado."""
    if _plata_bloqueada_por_sii(db, factura):
        raise HTTPException(409, "Esta factura todavía no está emitida ante el SII: "
                                 f"espera el folio (o usa «Reintentar») antes de {accion}"
                                 + (f". {salida}" if salida else ""))


def _recompute_factura(factura: ContFacturaCliente, cobranzas: Optional[list] = None) -> None:
    """Recalcula monto_pagado/saldo/estado_pago. `cobranzas`: lista ya leída BAJO LOCK
    (ver _cobranzas_bloqueadas) — obligatoria en los endpoints que escriben plata, para
    no persistir totales derivados de un snapshot viejo. Sin ella usa la relación."""
    bruto = _f(factura.monto_bruto)
    pagado = sum(_f(c.monto) for c in (factura.cobranzas if cobranzas is None else cobranzas))
    saldo = round(max(bruto - pagado, 0.0), 2)  # nunca negativo persistido
    factura.monto_pagado = round(pagado, 2)
    factura.saldo = saldo
    factura.estado_pago = _estado_pago(factura, pagado, saldo)


def _dtes_de_facturas(db: Session, facturas: list) -> dict:
    """{factura_id: WasabilDte} en UNA query (relación 1:1 factura↔DTE electrónico).
    Alimenta los campos dte_* del serializador — badge SII, PDF y bloqueos de la UI."""
    from wasabil_dte.models import WasabilDte
    ids = [f.id for f in facturas]
    if not ids:
        return {}
    return {w.factura_id: w for w in
            db.query(WasabilDte).filter(WasabilDte.factura_id.in_(ids)).all()}


def _serialize_factura(factura: ContFacturaCliente, dte=None) -> dict:
    """`dte`: fila WasabilDte de esta factura (opcional). Con ella el payload lleva
    dte_estado/dte_folio/dte_pdf_url/dte_puede_reintentar, que la UI usa para el
    badge "SII {folio}", el PDF y el bloqueo del folio manual (Fase B)."""
    bruto = _f(factura.monto_bruto)
    pagado = _f(factura.monto_pagado)
    saldo = _f(factura.saldo)
    fac = factura.factoring
    dte_info = {}
    if dte is not None:
        from wasabil_dte.service import serialize_dte
        s = serialize_dte(dte)
        dte_info = {
            "dte_estado": s.get("estado"), "dte_folio": s.get("folio"),
            "dte_pdf_url": s.get("pdf_url"),
            "dte_puede_reintentar": s.get("puede_reintentar"),
            "dte_en_vuelo": s.get("en_vuelo"), "dte_error": s.get("error"),
        }
    return {
        **dte_info,
        "id": factura.id,
        "numero_factura": factura.numero_factura,
        "tipo_doc": factura.tipo_doc,
        "es_anticipo": bool(factura.es_anticipo),
        "oc_cliente_id": factura.oc_cliente_id,
        "cotizacion_id": factura.cotizacion_id,
        "despacho_id": factura.despacho_id,
        "numero_guia": factura.despacho.numero_guia if factura.despacho else None,
        "numero_expedicion": factura.despacho.numero_expedicion if factura.despacho else None,
        "guia_firmada_archivo": factura.despacho.guia_firmada_archivo if factura.despacho else None,
        "fecha_emision": factura.fecha_emision.isoformat() if factura.fecha_emision else None,
        "condicion_pago": factura.condicion_pago,
        "plazo_dias": factura.plazo_dias,
        "fecha_vencimiento": factura.fecha_vencimiento.isoformat() if factura.fecha_vencimiento else None,
        "monto_neto": _f(factura.monto_neto),
        "iva": _f(factura.iva),
        "monto_bruto": bruto,
        "monto_pagado": pagado,
        "saldo": saldo,
        # SIEMPRE recalculado al servir: el estado persistido no transiciona a 'vencida'
        # con el paso del tiempo (solo se actualiza al escribir la factura/cobranzas).
        "estado_pago": _estado_pago(factura, pagado, saldo),
        "semaforo": _semaforo(factura.fecha_vencimiento, saldo),
        "dias_vencimiento": (factura.fecha_vencimiento - date.today()).days if factura.fecha_vencimiento else None,
        "observaciones": factura.observaciones,
        "items": [
            {
                "id": it.id,
                "item_cotizacion_id": it.item_cotizacion_id,
                "despacho_item_id": it.despacho_item_id,
                "anticipo_factura_id": it.anticipo_factura_id,
                "numero_parte": it.numero_parte,
                "descripcion": it.descripcion,
                "cantidad": _f(it.cantidad),
                "precio_unit_neto": _f(it.precio_unit_neto),
                "total_neto": _f(it.total_neto),
            }
            for it in factura.items
        ],
        "cobranzas": [
            {
                "id": c.id,
                "fecha": c.fecha.isoformat() if c.fecha else None,
                "monto": _f(c.monto),
                "medio": c.medio,
                "es_factoring": _es_medio_factoring(c.medio),
                "es_adelanto": c.medio == MEDIO_ADELANTO,
                "adelanto_id": c.adelanto_id,
                "banco": c.banco,
                "numero_operacion": c.numero_operacion,
                "observaciones": c.observaciones,
            }
            for c in factura.cobranzas
        ],
        "factoring": None if not fac else {
            "id": fac.id,
            "empresa_factoring": fac.empresa_factoring,
            "id_operacion": fac.id_operacion,
            "fecha_operacion": fac.fecha_operacion.isoformat() if fac.fecha_operacion else None,
            "monto_adelantado": _f(fac.monto_adelantado),
            "costo_factoring": _f(fac.costo_factoring),
            "retencion": _f(fac.retencion),
            "banco": fac.banco,
            "estado": fac.estado,
            "fecha_liquidacion": fac.fecha_liquidacion.isoformat() if fac.fecha_liquidacion else None,
            "usuario_id": fac.usuario_id,
            "usuario_liquidacion_id": fac.usuario_liquidacion_id,
            "observaciones": fac.observaciones,
        },
    }


def _facturas_de_oc(db: Session, oc_id: int, empresa: Optional[str] = None) -> List[ContFacturaCliente]:
    q = (
        db.query(ContFacturaCliente)
        .options(
            selectinload(ContFacturaCliente.items),
            selectinload(ContFacturaCliente.cobranzas),
            selectinload(ContFacturaCliente.factoring),
            joinedload(ContFacturaCliente.despacho),
        )
        .filter(ContFacturaCliente.oc_cliente_id == oc_id)
    )
    if empresa:  # defensa en profundidad (consistente con listar_facturas / get_kpis)
        q = q.filter(ContFacturaCliente.empresa == empresa)
    return q.order_by(ContFacturaCliente.id.asc()).all()


def _mercaderia_pendiente_bruto(items_db, pmap: dict, qty_fact: dict, totales: dict) -> float:
    """Bruto (c/IVA) de la mercadería AÚN sin facturar: Σ cantidad pendiente por ítem
    × su precio de venta vigente. La cantidad pendiente = cantidad − Σ facturada
    (las tandas parciales restan lo suyo; clamp a 0 por correcciones manuales).
    Se valúa al pricing VIGENTE a propósito: lo pendiente se facturará al precio del
    momento; lo YA facturado no participa (quedó congelado en sus facturas)."""
    neto_total = _f(totales.get("subtotal_neto_clp"))
    bruto_total = _f(totales.get("total_con_iva_clp"))
    factor_iva = (bruto_total / neto_total) if neto_total > TOL else 1.19
    pend_neto = 0.0
    for it in items_db:
        cant = _f(it.cantidad)
        if cant <= 0:
            continue
        qty_pend = max(cant - _f(qty_fact.get(it.id, 0.0)), 0.0)
        if qty_pend <= 0:
            continue
        pend_neto += _f((pmap.get(it.id) or {}).get("total_venta_clp")) * (qty_pend / cant)
    return pend_neto * factor_iva


def _resumen_cobranza(
    facturas: List[ContFacturaCliente],
    por_facturar_clp: Optional[float] = None,
) -> dict:
    facturado = sum(_f(f.monto_bruto) for f in facturas)
    cobrado = sum(_f(f.monto_pagado) for f in facturas)
    cobrado_cliente = sum(_f(c.monto) for f in facturas for c in f.cobranzas
                          if not _es_medio_factoring(c.medio))
    saldo = round(facturado - cobrado, 2)
    # estado EN VIVO (no el persistido): detecta facturas que vencieron con el tiempo
    hay_vencida = any(
        _estado_pago(f, _f(f.monto_pagado), _f(f.saldo)) == "vencida" for f in facturas
    )
    if not facturas:
        estado = "sin_factura"
    elif saldo <= TOL:
        estado = "cobrada"
    elif hay_vencida:
        estado = "vencida"
    elif cobrado > TOL:
        estado = "parcial"
    else:
        estado = "por_cobrar"
    out = {
        "n_facturas": len(facturas),
        "facturado_clp": round(facturado, 0),
        "cobrado_clp": round(cobrado, 0),
        "cobrado_cliente_clp": round(cobrado_cliente, 0),
        "por_cobrar_clp": round(max(saldo, 0), 0),
        "estado_cobranza": estado,
    }
    # "Por facturar" viene PRECALCULADO desde la mercadería físicamente sin facturar
    # (ver _mercaderia_pendiente_bruto) menos el anticipo aún por descontar. NO se
    # deriva de (total vivo − Σ brutos congelados): el total de la OC se recalcula
    # con el TC del día y las facturas quedan congeladas al emitir, así que esa
    # resta inventa "por facturar" fantasma en OCs cerradas cada vez que se mueve
    # el dólar (y el redondeo de IVA por tandas deja polvo de $1-3). Con la base
    # física, una OC totalmente facturada da 0 POR CONSTRUCCIÓN.
    if por_facturar_clp is not None:
        out["por_facturar_clp"] = round(max(_f(por_facturar_clp), 0), 0)
    return out


# ─── Adelantos de cliente (cont_adelanto) ─────────────────────────────────────
# El adelanto lo INFORMA Comercial (Cierre de Venta o Ventas), lo APRUEBA Tesorería
# (confirma la plata recibida; endpoint en tesoreria/router.py) y se APLICA aquí como
# cobranza medio='adelanto' al emitir facturas. Dos vías excluyentes por adelanto:
#   A) sin factura de anticipo → se aplica a las facturas del despacho real.
#   B) con factura de anticipo → se aplica a ESA factura; las facturas del despacho
#      real llevan línea de DESCUENTO negativa (ver _construir_factura).

def _total_bruto_venta(db: Session, cot_id: int) -> float:
    """Total con IVA de la venta (misma fuente que /ventas: pricing_service)."""
    cfg_dict = _cfg_to_dict(
        db.query(ConfiguracionCotizador).filter(ConfiguracionCotizador.id == 1).first()
    )
    _items, _pmap, totales = _precios_de_cotizacion(db, cot_id, cfg_dict)
    return _f(totales.get("total_con_iva_clp"))


def _adelantos_de_oc(db: Session, oc_id: int, con_anulados: bool = False) -> List[ContAdelanto]:
    q = db.query(ContAdelanto).filter(ContAdelanto.oc_cliente_id == oc_id)
    if not con_anulados:
        q = q.filter(ContAdelanto.estado != "anulado")
    return q.order_by(ContAdelanto.id.asc()).all()


def _adelantos_conciliados_ids(db: Session, adelanto_ids: List[int]) -> set:
    """IDs de adelantos ya conciliados con un abono del banco (enlace en Tesorería).
    El 'conciliado' se DERIVA de la existencia del enlace (igual que las cobranzas)."""
    if not adelanto_ids:
        return set()
    rows = (db.query(ConciliacionIngreso.adelanto_id)
            .filter(ConciliacionIngreso.adelanto_id.in_(adelanto_ids))
            .all())
    return {r[0] for r in rows}


def _monto_comprometido_adelanto(adel: ContAdelanto) -> float:
    """Monto que 'ocupa' el adelanto para el tope Σ adelantos ≤ total venta:
    el monto real si Tesorería ya aprobó; si no, lo informado por Comercial."""
    if adel.estado == "aprobado":
        return _f(adel.monto)
    return _f(adel.monto_esperado)


def _serialize_adelanto(adel: ContAdelanto, conciliado: bool = False) -> dict:
    monto = _f(adel.monto)
    aplicado = _f(adel.monto_aplicado)
    fa = adel.factura_anticipo
    return {
        "id": adel.id,
        "oc_cliente_id": adel.oc_cliente_id,
        "estado": adel.estado,
        "monto_esperado": _f(adel.monto_esperado),
        "pct": adel.pct,
        "monto": monto,
        "monto_aplicado": aplicado,
        "pendiente_aplicar": round(max(monto - aplicado, 0.0), 2),
        "fecha_pago": adel.fecha_pago.isoformat() if adel.fecha_pago else None,
        "banco": adel.banco,
        "numero_operacion": adel.numero_operacion,
        "conciliado_banco": conciliado,
        "factura_anticipo_id": adel.factura_anticipo_id,
        "factura_anticipo_folio": fa.numero_factura if fa else None,
        "observaciones": adel.observaciones,
        "fecha_aprobacion": adel.fecha_aprobacion.isoformat() if adel.fecha_aprobacion else None,
        "created_at": adel.created_at.isoformat() if adel.created_at else None,
    }


def _serialize_adelantos_de_oc(db: Session, oc_id: int) -> List[dict]:
    adelantos = _adelantos_de_oc(db, oc_id, con_anulados=True)
    conciliados = _adelantos_conciliados_ids(db, [a.id for a in adelantos])
    return [_serialize_adelanto(a, a.id in conciliados) for a in adelantos]


def _aplicar_adelantos_pendientes(db: Session, oc: OcCliente,
                                  factura: ContFacturaCliente,
                                  usuario_id: Optional[int] = None) -> float:
    """Aplica adelantos APROBADOS de la OC a `factura` como cobranzas medio='adelanto'.
    Llamar con la OC ya bloqueada (with_for_update) — desde crear_factura y desde la
    aprobación en Tesorería. NO hace commit (el llamador cierra la transacción).

      · factura de anticipo → SOLO adelantos ligados a ella (vía B).
      · factura normal      → adelantos sin factura de anticipo (vía A) más el
        EXCEDENTE de adelantos ligados cuyo anticipo ya quedó saldado: un adelanto
        mayor que el bruto de su anticipo es plata del cliente que debe rebajar el
        despacho real (misma regla que vía A: el resto sigue a la próxima factura).
    Cap por el SALDO actual (convive con cobranzas manuales previas); el remanente
    queda en `monto − monto_aplicado` para la siguiente factura.
    INVARIANTE: monto_aplicado == Σ cobranzas medio='adelanto' de ese adelanto.
    Devuelve el total aplicado (0.0 si no había nada que aplicar)."""
    # Mismo guard que registrar_cobranza: con factoring VIGENTE el saldo de la
    # factura es la retención del factor, NO deuda del cliente — aplicar el
    # adelanto aquí la dejaría 'pagada' y la liquidación liberaría $0. El
    # adelanto queda pendiente para la siguiente factura (o tras liquidar).
    # Lectura BLOQUEANTE (ver _factoring_bloqueado): con la relación perezosa, una
    # cesión al factor commiteada en paralelo era INVISIBLE y el adelanto entraba igual.
    _fact_vig = _factoring_bloqueado(db, factura.id)
    if _fact_vig and _fact_vig.estado == "vigente":
        return 0.0
    # Guard SII (Fase B): una factura electrónica que aún NO está emitida (claim en
    # vuelo o rechazada por el SII) no debe recibir plata — es el mismo invariante que
    # difiere los adelantos hasta el folio. Sin esto, Tesorería aprobando un adelanto
    # en esa ventana dejaba la factura fantasma "pagada", amarraba el adelanto a un
    # documento inexistente ante el SII y bloqueaba su borrado. La aplicación ocurre
    # igual, apenas el SII confirma, desde _finalizar_factura_emitida.
    # Consulta al DTE por _plata_bloqueada_por_sii —la misma puerta que usan la cobranza
    # manual y el factoring, así la regla vive en UN solo lugar— (ImportError → False,
    # esquema a medias → 503 accionable): antes el import y la query iban pelados y un
    # deploy sin el init_db de wasabil_dte reventaba en 500 crudo, sin decir qué correr.
    # Acá el guard NO es un 409: el adelanto queda pendiente y se aplica solo cuando el
    # SII confirma (desde _finalizar_factura_emitida).
    if _plata_bloqueada_por_sii(db, factura):
        return 0.0
    # populate_existing (regla 3 de docs/regla-lecturas-de-plata.md): sin él SQLAlchemy
    # devuelve los adelantos que ya están en el identity map —los metió la lectura PLANA
    # de _adelantos_de_oc / del serializador— y DESCARTA los valores frescos que trajo el
    # FOR UPDATE. Acá se decide plata con `monto_aplicado`: con el valor viejo, el mismo
    # depósito se aplicaba DOS VECES (rompiendo monto_aplicado == Σ cobranzas 'adelanto').
    q = (db.query(ContAdelanto)
         .filter(ContAdelanto.oc_cliente_id == oc.id,
                 ContAdelanto.estado == "aprobado")
         .populate_existing().with_for_update())
    if factura.es_anticipo:
        q = q.filter(ContAdelanto.factura_anticipo_id == factura.id)
    adelantos = q.order_by(ContAdelanto.id.asc()).all()
    if not adelantos:
        return 0.0
    # Para la factura normal, un adelanto ligado (vía B) solo entra por su EXCEDENTE
    # y recién cuando su factura de anticipo quedó saldada — antes de eso el saldo
    # del anticipo tiene prioridad sobre esa plata.
    anticipos_saldados: set = set()
    if not factura.es_anticipo:
        ids_anticipos = {a.factura_anticipo_id for a in adelantos if a.factura_anticipo_id}
        if ids_anticipos:
            anticipos = (db.query(ContFacturaCliente)
                         .options(selectinload(ContFacturaCliente.cobranzas))
                         .filter(ContFacturaCliente.id.in_(ids_anticipos))
                         .all())
            anticipos_saldados = {
                a.id for a in anticipos
                if round(_f(a.monto_bruto) - sum(_f(c.monto) for c in a.cobranzas), 2) <= TOL_PAGO
            }
    # Lectura BLOQUEANTE (ver _cobranzas_bloqueadas): con la relación perezosa, dos
    # aprobaciones de adelanto en paralelo veían ambas el saldo COMPLETO y aplicaban las
    # dos — la plata del cliente se consumía contra una factura ya cubierta, el excedente
    # se evaporaba y se le seguía exigiendo un saldo que ya había depositado.
    cobs_frescas = _cobranzas_bloqueadas(db, factura.id)
    pagado = sum(_f(c.monto) for c in cobs_frescas)
    saldo = round(_f(factura.monto_bruto) - pagado, 2)
    total_aplicado = 0.0
    nuevas_cobranzas = []
    for adel in adelantos:
        if saldo <= TOL_PAGO:
            break
        if (not factura.es_anticipo and adel.factura_anticipo_id
                and adel.factura_anticipo_id not in anticipos_saldados):
            continue
        pendiente = round(_f(adel.monto) - _f(adel.monto_aplicado), 2)
        aplicar = round(min(pendiente, saldo), 2)
        if aplicar <= TOL_PAGO:
            continue
        cob = ContCobranza(
            factura_id=factura.id, adelanto_id=adel.id,
            # Fecha del depósito; si Tesorería no la registró, la de CHILE (no date.today(),
            # que es UTC en el server y a fin de mes cae en el período equivocado).
            fecha=adel.fecha_pago or _hoy_chile(), monto=aplicar,
            medio=MEDIO_ADELANTO, banco=adel.banco,
            numero_operacion=adel.numero_operacion,
            observaciones="Aplicación automática de adelanto aprobado por Tesorería",
            usuario_id=usuario_id,
        )
        db.add(cob)
        nuevas_cobranzas.append(cob)
        adel.monto_aplicado = round(_f(adel.monto_aplicado) + aplicar, 2)
        saldo = round(saldo - aplicar, 2)
        total_aplicado = round(total_aplicado + aplicar, 2)
    if total_aplicado > 0:
        db.flush()
        db.refresh(factura, with_for_update=True)
        _recompute_factura(factura, cobranzas=cobs_frescas + nuevas_cobranzas)
    return total_aplicado


def _anticipos_pendientes_de_descuento(db: Session, oc_id: int, empresa: str):
    """Facturas de ANTICIPO de la OC con neto aún no descontado en facturas del despacho
    real. Devuelve [(factura_anticipo, pendiente_neto)] en orden de emisión. El pendiente
    se DERIVA: neto del anticipo − Σ(−total_neto) de las líneas de descuento que lo
    referencian (borrar una factura final restaura el pendiente solo, vía cascade).

    Incluye los anticipos SIN folio SII (emisión electrónica en vuelo o rechazada) a
    propósito: NO se pueden descontar todavía, pero tampoco se pueden ignorar — facturar
    el despacho completo mientras existe un anticipo pendiente le cobraría dos veces al
    cliente. Quien construye la factura debe BLOQUEAR (ver _construir_factura)."""
    anticipos = (
        db.query(ContFacturaCliente)
        .filter(ContFacturaCliente.oc_cliente_id == oc_id,
                ContFacturaCliente.empresa == empresa,
                ContFacturaCliente.es_anticipo == 1)
        .order_by(ContFacturaCliente.id.asc())
        .all()
    )
    if not anticipos:
        return []
    rows = (
        db.query(ContFacturaClienteItem.anticipo_factura_id, ContFacturaClienteItem.total_neto)
        .filter(ContFacturaClienteItem.anticipo_factura_id.in_([a.id for a in anticipos]))
        .all()
    )
    descontado: dict = {}
    for aid, tot in rows:
        descontado[aid] = descontado.get(aid, 0.0) + (-_f(tot))  # líneas negativas
    out = []
    for a in anticipos:
        pend = round(_f(a.monto_neto) - descontado.get(a.id, 0.0), 2)
        if pend > TOL:
            out.append((a, pend))
    return out


def _periodo_filter(created_at, periodo: Optional[str]) -> bool:
    if not periodo or not created_at:
        return True
    hoy = date.today()
    d = created_at.date() if hasattr(created_at, "date") else created_at
    if periodo == "semana":
        return (hoy - d).days <= 7
    if periodo == "mes":
        return d.year == hoy.year and d.month == hoy.month
    if periodo == "anio":
        return d.year == hoy.year
    return True


# ─── Ventas (agrupado por OC, expandible a ítem — SOLO datos de venta) ─────────
@router.get("/ventas")
def listar_ventas(
    q: Optional[str] = None,
    periodo: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Lista las ventas agregadas por OC de cliente, cada una con su resumen de cobranza.
    `q` busca en cliente/N° OC/N° cotización/RUT; `periodo` filtra por fecha de la
    cotización (semana | mes | anio)."""
    cfg_dict = _cfg_to_dict(
        db.query(ConfiguracionCotizador).filter(ConfiguracionCotizador.id == 1).first()
    )
    empresa = getattr(current_user, "empresa", None) or "mineria"
    ocs = db.query(OcCliente).options(joinedload(OcCliente.cotizacion)).all()
    # Prefetch en LOTE (anti N+1): ítems de todas las cotizaciones y facturas de todas
    # las OC en 2 queries totales, en vez de 2+ queries por cada OC del listado.
    cot_ids = {oc.cotizacion.id for oc in ocs if oc.cotizacion}
    items_by_cot: dict = {}
    if cot_ids:
        for it in db.query(ItemCotizacion).filter(ItemCotizacion.cotizacion_id.in_(cot_ids)).all():
            items_by_cot.setdefault(it.cotizacion_id, []).append(it)
    facturas_by_oc: dict = {}
    oc_ids = [oc.id for oc in ocs]
    if oc_ids:
        fs = (db.query(ContFacturaCliente)
              .options(selectinload(ContFacturaCliente.cobranzas),
                       selectinload(ContFacturaCliente.factoring))
              .filter(ContFacturaCliente.oc_cliente_id.in_(oc_ids),
                      ContFacturaCliente.empresa == empresa)
              .order_by(ContFacturaCliente.id.asc()).all())
        for f in fs:
            facturas_by_oc.setdefault(f.oc_cliente_id, []).append(f)
    # Para "por facturar" (misma base FÍSICA que el detalle) en UNA query extra:
    # las líneas de factura traen a la vez las cantidades facturadas por ítem y
    # los descuentos de anticipo ya aplicados (líneas negativas con anticipo_factura_id).
    qty_fact_by_oc: dict = {}      # oc_id -> {item_id: qty facturada}
    desc_por_anticipo: dict = {}   # factura_anticipo_id -> neto ya descontado
    if oc_ids:
        fitem_rows = (
            db.query(ContFacturaCliente.oc_cliente_id,
                     ContFacturaClienteItem.item_cotizacion_id,
                     ContFacturaClienteItem.cantidad,
                     ContFacturaClienteItem.anticipo_factura_id,
                     ContFacturaClienteItem.total_neto)
            .join(ContFacturaCliente, ContFacturaCliente.id == ContFacturaClienteItem.factura_id)
            .filter(ContFacturaCliente.oc_cliente_id.in_(oc_ids),
                    ContFacturaCliente.empresa == empresa)
            .all()
        )
        for oc_id_r, item_id, cant, ant_id, tot_neto in fitem_rows:
            if item_id:
                m = qty_fact_by_oc.setdefault(oc_id_r, {})
                m[item_id] = m.get(item_id, 0.0) + _f(cant)
            if ant_id:
                desc_por_anticipo[ant_id] = desc_por_anticipo.get(ant_id, 0.0) + (-_f(tot_neto))
    # Adelantos por OC en UNA query (badge en Ventas: informados / aprobados / por aplicar)
    adelantos_by_oc: dict = {}
    if oc_ids:
        for a in (db.query(ContAdelanto)
                  .filter(ContAdelanto.oc_cliente_id.in_(oc_ids),
                          ContAdelanto.estado != "anulado").all()):
            adelantos_by_oc.setdefault(a.oc_cliente_id, []).append(a)
    result = []
    for oc in ocs:
        cot = oc.cotizacion
        if not cot:
            continue
        if not _periodo_filter(cot.created_at, periodo):
            continue
        items_db, _pmap, totales = _precios_de_cotizacion(
            db, cot.id, cfg_dict, items_db=items_by_cot.get(cot.id, []))
        if not items_db:
            continue
        if q:
            ql = q.lower()
            hay = " ".join([cot.cliente or "", oc.numero_oc or "", cot.numero or "", cot.rut_cliente or ""]).lower()
            if ql not in hay:
                continue
        facs_oc = facturas_by_oc.get(oc.id, [])
        # Anticipos con neto aún no descontado, en bruto (mismo criterio del detalle)
        anticipo_pend = 0.0
        for f in facs_oc:
            if f.es_anticipo:
                neto_fa = _f(f.monto_neto)
                pend = neto_fa - desc_por_anticipo.get(f.id, 0.0)
                if pend > TOL:
                    factor = (_f(f.monto_bruto) / neto_fa) if neto_fa > TOL else 1.0
                    anticipo_pend += pend * factor
        mercaderia_pend = _mercaderia_pendiente_bruto(
            items_db, _pmap, qty_fact_by_oc.get(oc.id, {}), totales)
        resumen = _resumen_cobranza(
            facs_oc, por_facturar_clp=mercaderia_pend - anticipo_pend)
        adels = adelantos_by_oc.get(oc.id, [])
        resumen_adelantos = {
            "n": len(adels),
            "por_aprobar": sum(1 for a in adels if a.estado == "informado"),
            "aprobado_clp": round(sum(_f(a.monto) for a in adels if a.estado == "aprobado"), 0),
            "pendiente_aplicar_clp": round(sum(
                max(_f(a.monto) - _f(a.monto_aplicado), 0.0)
                for a in adels if a.estado == "aprobado"), 0),
        }
        result.append({
            "adelantos": resumen_adelantos,
            "oc_cliente_id": oc.id,
            "cotizacion_id": cot.id,
            "numero_oc": oc.numero_oc,
            "numero_cotizacion": cot.numero,
            "cliente": cot.cliente or "",
            "rut_cliente": cot.rut_cliente or "",
            "fecha_oc": oc.fecha_oc,
            "fecha_venta": cot.created_at.isoformat() if cot.created_at else None,
            "cond_pago": oc.cond_pago,
            "total_items": len(items_db),
            "total_neto_clp": round(_f(totales.get("subtotal_neto_clp")), 0),
            "iva_clp": round(_f(totales.get("iva_clp")), 0),
            "total_con_iva_clp": round(_f(totales.get("total_con_iva_clp")), 0),
            **resumen,
        })
    result.sort(key=lambda v: (v.get("fecha_venta") is None, v.get("fecha_venta") or ""), reverse=True)
    return result


@router.get("/ventas/{oc_id}")
def detalle_venta(
    oc_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Detalle de una venta (OC): por cada ítem, su precio de venta, las guías de despacho
    (con estado de firma y N° de expedición) y las facturas asociadas. Incluye además las
    facturas serializadas y el resumen de cobranza de la OC. 404 si la OC no existe."""
    empresa = getattr(current_user, "empresa", None) or "mineria"
    oc = db.query(OcCliente).filter(OcCliente.id == oc_id).first()
    if not oc or not oc.cotizacion:
        raise HTTPException(404, "Venta (OC) no encontrada")
    cot = oc.cotizacion
    cfg_dict = _cfg_to_dict(
        db.query(ConfiguracionCotizador).filter(ConfiguracionCotizador.id == 1).first()
    )
    items_db, pmap, totales = _precios_de_cotizacion(db, cot.id, cfg_dict)

    # ítem -> guías (despachos no anulados, para visibilidad: incluye en preparación)
    desp_rows = (
        db.query(DespachoItem, Despacho)
        .join(Despacho, Despacho.id == DespachoItem.despacho_id)
        .filter(Despacho.oc_cliente_id == oc.id, Despacho.estado != "anulado")
        .all()
    )
    guias_por_item = {}
    for di, d in desp_rows:
        guias_por_item.setdefault(di.item_cotizacion_id, []).append({
            "despacho_item_id": di.id, "despacho_id": d.id,
            "numero_despacho": d.numero_despacho, "numero_guia": d.numero_guia,
            "estado": d.estado, "qty_despachada": _f(di.qty_despachada),
            "guia_firmada": bool(d.guia_firmada),
            "numero_expedicion": d.numero_expedicion,
            "guia_firmada_archivo": d.guia_firmada_archivo,
        })

    # ítem -> facturas
    fac_rows = (
        db.query(ContFacturaClienteItem, ContFacturaCliente)
        .join(ContFacturaCliente, ContFacturaCliente.id == ContFacturaClienteItem.factura_id)
        .filter(ContFacturaCliente.oc_cliente_id == oc.id)
        .all()
    )
    facturas_por_item = {}
    for fi, f in fac_rows:
        facturas_por_item.setdefault(fi.item_cotizacion_id, []).append({
            "factura_id": f.id, "numero_factura": f.numero_factura,
            "fecha_emision": f.fecha_emision.isoformat() if f.fecha_emision else None,
            "plazo_dias": f.plazo_dias,
            "fecha_vencimiento": f.fecha_vencimiento.isoformat() if f.fecha_vencimiento else None,
            "estado_pago": f.estado_pago, "cantidad": _f(fi.cantidad),
        })

    items_out = []
    for it in items_db:
        ci = pmap.get(it.id, {})
        items_out.append({
            "id": it.id,
            "item_num": it.item_num,
            "numero_parte": it.numero_parte or "",
            "descripcion": it.descripcion or "",
            "marca": it.marca or "",
            "cantidad": _f(it.cantidad),
            "precio_unit_venta_clp": round(_f(ci.get("precio_venta_clp")), 0),
            "total_venta_clp": round(_f(ci.get("total_venta_clp")), 0),
            "estado_item": it.estado_item or "ingresado",
            "guias": guias_por_item.get(it.id, []),
            "facturas": facturas_por_item.get(it.id, []),
        })

    facturas = _facturas_de_oc(db, oc.id, empresa)
    # Anticipo aún NO descontado en facturas del despacho real, en BRUTO (con el IVA
    # de cada factura de anticipo: cubre facturas exentas con factor 1.0). Explica la
    # diferencia entre "mercadería sin facturar" y por_facturar_clp en la pantalla.
    anticipo_por_descontar = 0.0
    for fa, pend_neto in _anticipos_pendientes_de_descuento(db, oc.id, empresa):
        neto_fa = _f(fa.monto_neto)
        factor = (_f(fa.monto_bruto) / neto_fa) if neto_fa > TOL else 1.0
        anticipo_por_descontar += pend_neto * factor
    # Por facturar = mercadería físicamente pendiente − anticipo por descontar
    # (el anticipo ya es plata facturada contra esa mercadería futura)
    qty_fact = {}
    for fi, _fac in fac_rows:
        if fi.item_cotizacion_id:
            qty_fact[fi.item_cotizacion_id] = qty_fact.get(fi.item_cotizacion_id, 0.0) + _f(fi.cantidad)
    mercaderia_pend = _mercaderia_pendiente_bruto(items_db, pmap, qty_fact, totales)
    resumen = _resumen_cobranza(
        facturas, por_facturar_clp=mercaderia_pend - anticipo_por_descontar
    )
    resumen["anticipo_por_descontar_clp"] = round(anticipo_por_descontar, 0)
    # Cifra autoritativa para la UI (nota de la barra y sección "Por facturar"):
    # evita que el frontend la reconstruya como por_facturar + anticipo, que con
    # anticipo > mercadería (clamp) sobredeclararía la mercadería pendiente.
    resumen["mercaderia_pendiente_clp"] = round(max(mercaderia_pend, 0), 0)
    dtes_map = _dtes_de_facturas(db, facturas)
    return {
        "oc_cliente_id": oc.id,
        "cotizacion_id": cot.id,
        "numero_oc": oc.numero_oc,
        "asesor_id": oc.asesor_id,
        "numero_cotizacion": cot.numero,
        "cliente": cot.cliente or "",
        "rut_cliente": cot.rut_cliente or "",
        "fecha_oc": oc.fecha_oc,
        "cond_pago": oc.cond_pago,
        "fecha_entrega": oc.fecha_entrega.isoformat() if oc.fecha_entrega else None,
        "total_neto_clp": round(_f(totales.get("subtotal_neto_clp")), 0),
        "iva_clp": round(_f(totales.get("iva_clp")), 0),
        "total_con_iva_clp": round(_f(totales.get("total_con_iva_clp")), 0),
        "items": items_out,
        "facturas": [_serialize_factura(f, dte=dtes_map.get(f.id)) for f in facturas],
        "resumen": resumen,
        "adelantos": _serialize_adelantos_de_oc(db, oc.id),
    }


@router.get("/ventas/{oc_id}/despachos-facturables")
def despachos_facturables(
    oc_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Guías de despacho FACTURABLES de la OC: despachos cerrados (estado 'despachado')
    Y con la guía FIRMADA (entregada y firmada por el cliente), con saldo aún facturable.
    Alimenta el selector del modal 'Emitir factura' para que no ofrezca guías ya facturadas.
    Cada guía incluye numero_expedicion y guia_firmada_archivo (para verla antes de emitir)."""
    fact_di = _qty_facturada_por_despacho_item(db, oc_id)
    by_desp = {}
    for di, d in _despacho_items_de_oc(db, oc_id):
        facturable = _f(di.qty_despachada) - fact_di.get(di.id, 0.0)
        e = by_desp.setdefault(d.id, {
            "id": d.id, "numero_despacho": d.numero_despacho,
            "numero_guia": d.numero_guia, "numero_expedicion": d.numero_expedicion,
            # Fecha de EMISIÓN de la guía: la referencia 52 del DTE 33 la exige, así que
            # una guía en papel sin ella NO se puede facturar al SII. Viaja hasta acá para
            # que el selector lo avise ANTES de elegirla — quien factura (Contabilidad) no
            # es quien la carga (Bodega), y si no se ve acá el bloqueo sorprende al final.
            "fecha_guia": d.fecha_guia.isoformat() if d.fecha_guia else None,
            "guia_firmada_archivo": d.guia_firmada_archivo,
            "items_count": 0, "facturable": 0.0,
        })
        e["items_count"] += 1
        e["facturable"] += max(facturable, 0.0)
    # TOL_QTY (unidades), no TOL (pesos): 'facturable' es una CANTIDAD; con TOL=0.5 una
    # guía con saldo fraccional ≤0.5 unidades desaparecía del selector aunque crear_factura
    # sí la aceptaba (mismo criterio que la derivación de líneas).
    return [e for e in by_desp.values() if e["facturable"] > TOL_QTY]


# ─── Adelantos de cliente: informar / listar / editar / anular ────────────────
class AdelantoInformarIn(BaseModel):
    """Adelanto INFORMADO por Comercial. Acepta la OC directa o la cotización (el Cierre
    de Venta no siempre conoce el id de la OC recién creada). Basta monto_esperado o pct
    (si solo viene pct, el monto esperado se deriva del total de la venta)."""
    oc_cliente_id: Optional[int] = None
    cotizacion_id: Optional[int] = None
    monto_esperado: Optional[float] = Field(None, gt=0)
    pct: Optional[int] = Field(None, ge=1, le=100)
    observaciones: Optional[str] = None


class AdelantoEditarIn(BaseModel):
    monto_esperado: Optional[float] = Field(None, gt=0)
    pct: Optional[int] = Field(None, ge=1, le=100)
    observaciones: Optional[str] = None


def _resolver_oc(db: Session, oc_cliente_id: Optional[int],
                 cotizacion_id: Optional[int], lock: bool = False) -> OcCliente:
    if not oc_cliente_id and not cotizacion_id:
        raise HTTPException(400, "Indica la OC (oc_cliente_id) o la cotización (cotizacion_id)")
    q = db.query(OcCliente)
    if oc_cliente_id:
        q = q.filter(OcCliente.id == oc_cliente_id)
    else:
        # La OC más reciente de esa cotización (el Cierre de Venta la acaba de crear)
        q = q.filter(OcCliente.cotizacion_id == cotizacion_id).order_by(OcCliente.id.desc())
    if lock:
        q = q.with_for_update()
    oc = q.first()
    if not oc or not oc.cotizacion:
        raise HTTPException(404, "Venta (OC) no encontrada")
    return oc


def _validar_tope_adelantos(db: Session, oc: OcCliente, monto_nuevo: float,
                            excluir_id: Optional[int] = None) -> None:
    """Σ adelantos comprometidos (no anulados) + el nuevo ≤ total de la venta (+TOL).
    Si el total de la venta no se puede calcular (venta sin ítems), no bloquea."""
    total_venta = _total_bruto_venta(db, oc.cotizacion_id)
    if total_venta <= 0:
        return
    comprometido = sum(
        _monto_comprometido_adelanto(a)
        for a in _adelantos_de_oc(db, oc.id)
        if a.id != excluir_id
    )
    if comprometido + monto_nuevo > total_venta + TOL_PAGO:
        disponible = max(total_venta - comprometido, 0.0)
        raise HTTPException(
            400,
            f"La suma de adelantos excede el total de la venta "
            f"(disponible {disponible:,.0f} de {total_venta:,.0f})".replace(",", "."),
        )


@router.post("/ventas/adelantos")
def informar_adelanto(
    payload: AdelantoInformarIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """COMERCIAL informa que la venta tiene un adelanto (desde el Cierre de Venta o desde
    Ventas para una OC ya cerrada). Crea el registro en estado 'informado'; el pago lo
    confirma después Tesorería (aprobación) y ahí recién se aplica a facturas.
    Una OC puede tener VARIOS adelantos; el tope es el total de la venta."""
    empresa = getattr(current_user, "empresa", None) or "mineria"
    # Lock de la OC: serializa informes/aprobaciones concurrentes sobre la misma venta
    oc = _resolver_oc(db, payload.oc_cliente_id, payload.cotizacion_id, lock=True)
    if not payload.monto_esperado and not payload.pct:
        raise HTTPException(400, "Indica el monto esperado del adelanto o el porcentaje")
    monto_esperado = _f(payload.monto_esperado)
    if not monto_esperado and payload.pct:
        total_venta = _total_bruto_venta(db, oc.cotizacion_id)
        if total_venta <= 0:
            raise HTTPException(400, "No se pudo derivar el monto desde el % (venta sin total); indica el monto esperado")
        monto_esperado = round(total_venta * payload.pct / 100.0, 0)
    _validar_tope_adelantos(db, oc, monto_esperado)
    adel = ContAdelanto(
        empresa=empresa,
        oc_cliente_id=oc.id,
        estado="informado",
        monto_esperado=monto_esperado,
        pct=payload.pct,
        observaciones=payload.observaciones,
        usuario_informa_id=getattr(current_user, "id", None),
    )
    db.add(adel)
    db.commit()
    db.refresh(adel)
    return _serialize_adelanto(adel)


@router.get("/ventas/{oc_id}/adelantos")
def listar_adelantos_de_venta(
    oc_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Adelantos de la venta (incluye anulados, para trazabilidad) con estado derivado:
    conciliado_banco (enlace en Tesorería) y pendiente_aplicar (monto − aplicado)."""
    oc = db.query(OcCliente).filter(OcCliente.id == oc_id).first()
    if not oc:
        raise HTTPException(404, "Venta (OC) no encontrada")
    return _serialize_adelantos_de_oc(db, oc.id)


@router.patch("/adelantos/{adelanto_id}")
def editar_adelanto(
    adelanto_id: int,
    payload: AdelantoEditarIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Edita lo INFORMADO por Comercial (monto esperado / % / observaciones). Un adelanto
    APROBADO se corrige re-aprobándolo en Tesorería (y solo si no está aplicado ni
    conciliado); uno anulado no se edita."""
    # Orden GLOBAL de locks: OC → adelanto (el mismo de crear_factura y de la
    # aprobación en Tesorería) — tomar primero el adelanto arriesgaba deadlock.
    ref = db.query(ContAdelanto).filter(ContAdelanto.id == adelanto_id).first()
    if not ref:
        raise HTTPException(404, "Adelanto no encontrado")
    oc = db.query(OcCliente).filter(OcCliente.id == ref.oc_cliente_id).with_for_update().first()
    adel = (db.query(ContAdelanto)
            .filter(ContAdelanto.id == adelanto_id)
            .populate_existing().with_for_update().first())
    if not adel:
        raise HTTPException(404, "Adelanto no encontrado")
    if adel.estado == "anulado":
        raise HTTPException(409, "El adelanto está anulado; no se puede editar")
    if adel.estado == "aprobado" and (payload.monto_esperado or payload.pct):
        raise HTTPException(409, "El adelanto ya fue aprobado por Tesorería; corrígelo re-aprobándolo allá")
    if payload.monto_esperado is not None or payload.pct is not None:
        monto_esperado = _f(payload.monto_esperado) or _f(adel.monto_esperado)
        if payload.pct and not payload.monto_esperado:
            total_venta = _total_bruto_venta(db, oc.cotizacion_id) if oc else 0.0
            if total_venta > 0:
                monto_esperado = round(total_venta * payload.pct / 100.0, 0)
        if oc:
            _validar_tope_adelantos(db, oc, monto_esperado, excluir_id=adel.id)
        adel.monto_esperado = monto_esperado
        if payload.pct is not None:
            adel.pct = payload.pct
    if payload.observaciones is not None:
        adel.observaciones = payload.observaciones
    db.commit()
    db.refresh(adel)
    conciliado = adel.id in _adelantos_conciliados_ids(db, [adel.id])
    return _serialize_adelanto(adel, conciliado)


@router.post("/adelantos/{adelanto_id}/anular")
def anular_adelanto(
    adelanto_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Anula un adelanto que no prosperó (el cliente nunca depositó, o se informó por
    error). Bloqueado si ya se aplicó a una factura (revertir esa cobranza primero) o si
    está conciliado con el banco (desconciliar en Tesorería primero)."""
    # populate_existing: el guard de abajo decide sobre plata (monto_aplicado) y el
    # listado de la venta ya pudo cargar este adelanto con una lectura plana.
    adel = (db.query(ContAdelanto)
            .filter(ContAdelanto.id == adelanto_id)
            .populate_existing().with_for_update().first())
    if not adel:
        raise HTTPException(404, "Adelanto no encontrado")
    if adel.estado == "anulado":
        return _serialize_adelanto(adel)
    if _f(adel.monto_aplicado) > TOL:
        raise HTTPException(409, "El adelanto ya fue aplicado a una factura; revierta esa cobranza antes de anularlo")
    if adel.id in _adelantos_conciliados_ids(db, [adel.id]):
        raise HTTPException(409, "El adelanto está conciliado con un abono del banco; desconcílielo en Tesorería primero")
    adel.estado = "anulado"
    db.commit()
    db.refresh(adel)
    return _serialize_adelanto(adel)


# ─── Facturas / Cobranzas / Factoring ─────────────────────────────────────────
class FacturaItemIn(BaseModel):
    """Una línea a facturar. cantidad/precio son opcionales: si faltan, se toman de lo
    despachado y del precio de venta calculado. despacho_item_id liga la línea a una guía."""
    item_cotizacion_id: int
    despacho_item_id: Optional[int] = None
    cantidad: Optional[float] = Field(None, gt=0)
    precio_unit_neto: Optional[float] = Field(None, gt=0)


class FacturaCreate(BaseModel):
    """Emisión de una factura. Indicar 'despacho_id' (se derivan las líneas de esa guía
    firmada) O 'items' explícitos. numero_factura es el folio SII (único por empresa).
    rut_cliente: completa el RUT del cliente si la venta no lo tiene (campo por llenar
    en el modal de emisión) — se guarda en la venta para dejarla completa."""
    oc_cliente_id: int
    despacho_id: Optional[int] = None
    numero_factura: Optional[str] = None
    # Lista BLANCA (no un str libre): el folio SII es obligatorio solo para 'factura', así
    # que cualquier otro valor —un typo como 'Factura', o 'nota'— SALTEABA el folio
    # obligatorio y persistía un documento tributario sin N°. Los dos valores son los que
    # admite la columna (models.py: "factura | boleta") y los únicos que manda la pantalla.
    tipo_doc: Literal["factura", "boleta"] = "factura"
    fecha_emision: Optional[str] = None
    condicion_pago: Optional[str] = None
    # le=3650 (10 años): sin techo, `fecha_emision + timedelta(days=plazo)` reventaba con
    # OverflowError → 500 en vez de un 422 del borde.
    plazo_dias: Optional[int] = Field(None, ge=0, le=3650)
    items: Optional[List[FacturaItemIn]] = None
    observaciones: Optional[str] = None
    rut_cliente: Optional[str] = None
    # Razón social: mismo trato que el RUT — campo por llenar en el modal cuando la
    # venta no la trae; se guarda en la venta para dejarla completa.
    razon_social_cliente: Optional[str] = None
    # Factura de ANTICIPO: respalda un adelanto ante el SII, NO exige guía firmada.
    # monto_neto_anticipo es el neto de la factura (el IVA se calcula); adelanto_ids
    # liga los adelantos que respalda (vía B: su plata cae aquí como cobranza automática
    # y las facturas del despacho real llevan el descuento).
    es_anticipo: bool = False
    monto_neto_anticipo: Optional[float] = Field(None, gt=0)
    adelanto_ids: Optional[List[int]] = None
    descripcion_anticipo: Optional[str] = None
    # Puerta EXPLÍCITA para un SEGUNDO anticipo de la misma venta. Emitir un DTE 33 es
    # IRREVERSIBLE y el candado anti doble emisión solo dura mientras el HTTP está en
    # vuelo: sin esto, dos clics tranquilos emitían dos facturas de anticipo REALES por el
    # mismo adelanto. Aquí NO se prohíbe (en Grupo AM una OC puede tener varios adelantos,
    # cada uno ligado a su anticipo por cont_adelanto.factura_anticipo_id): se exige
    # decirlo a propósito. Ver _construir_factura_anticipo.
    confirmar_segundo_anticipo: bool = False


class FacturaPreview(BaseModel):
    """Datos mínimos para PREVISUALIZAR una factura antes de emitir (no persiste)."""
    oc_cliente_id: int
    despacho_id: Optional[int] = None
    items: Optional[List[FacturaItemIn]] = None
    rut_cliente: Optional[str] = None
    razon_social_cliente: Optional[str] = None
    es_anticipo: bool = False
    monto_neto_anticipo: Optional[float] = Field(None, gt=0)
    adelanto_ids: Optional[List[int]] = None
    descripcion_anticipo: Optional[str] = None
    # Mismo campo que FacturaCreate: el preview tiene que poder mostrar el aviso del
    # segundo anticipo Y desbloquearlo cuando el operador marca la casilla, o el modal
    # pintaría "no se puede emitir" sobre algo que el emitir sí acepta.
    confirmar_segundo_anticipo: bool = False


@router.get("/facturas")
def listar_facturas(
    estado: Optional[str] = None,
    q: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Lista las facturas (filtrables por estado_pago y por texto `q`) y la ANTIGÜEDAD de
    cartera: saldo por cobrar en buckets 0-30 / 31-60 / 61-90 / 91+ días desde la emisión."""
    empresa = getattr(current_user, "empresa", None) or "mineria"
    facturas = (
        db.query(ContFacturaCliente)
        .options(*_FACTURA_EAGER)
        .filter(ContFacturaCliente.empresa == empresa)
        .order_by(ContFacturaCliente.id.desc())
        .all()
    )
    # DTEs electrónicos de estas facturas en UNA query (badge SII / PDF / bloqueos
    # del frontend sin llamadas extra — relación 1:1 factura↔DTE)
    dtes_por_factura = _dtes_de_facturas(db, facturas)
    out = []
    aging = {"0_30": 0.0, "31_60": 0.0, "61_90": 0.0, "91_mas": 0.0}
    hoy = date.today()
    for f in facturas:
        oc = f.oc_cliente
        cot = oc.cotizacion if oc else None
        d = _serialize_factura(f, dte=dtes_por_factura.get(f.id))
        d["cliente"] = (cot.cliente if cot else None) or ""
        d["rut_cliente"] = (cot.rut_cliente if cot else None) or ""
        d["numero_oc"] = oc.numero_oc if oc else None
        # filtra por el estado EN VIVO del serializador (no el persistido, que puede
        # estar obsoleto para 'vencida')
        if estado and d["estado_pago"] != estado:
            continue
        if q:
            ql = q.lower()
            hay = " ".join([d["cliente"], d.get("numero_factura") or "", d.get("numero_oc") or "", d["rut_cliente"]]).lower()
            if ql not in hay:
                continue
        saldo = d["saldo"]
        if saldo > TOL and f.fecha_emision:
            dias = (hoy - f.fecha_emision).days
            if dias <= 30:
                aging["0_30"] += saldo
            elif dias <= 60:
                aging["31_60"] += saldo
            elif dias <= 90:
                aging["61_90"] += saldo
            else:
                aging["91_mas"] += saldo
        out.append(d)
    return {"facturas": out, "antiguedad": {k: round(v, 0) for k, v in aging.items()}}


def _precios_congelados_guia(db: Session, despacho_id: Optional[int]):
    """Precios unitarios CONGELADOS en la guía electrónica (52) ya emitida de este despacho.
    La factura DEBE reflejar lo enviado al SII en la guía, no un recálculo con la config del
    cotizador de HOY: si cambió el dólar entre el despacho y la facturación, recalcular
    descuadraría la factura contra la guía ya emitida.

    Devuelve (por_despacho_item, por_parte):
      - por_despacho_item {despacho_item_id: precio}: match 1:1 EXACTO por línea (clave
        primaria, vía externalId del detalle). Robusto ante n° de parte repetido.
      - por_parte {numero_parte: precio}: fallback para guías antiguas sin externalId
        (descarta n° de parte duplicados, que no se pueden mapear con certeza).
    Ambos vacíos si no hay guía electrónica emitida (→ la factura recalcula: guías manuales)."""
    if not despacho_id:
        return {}, {}
    try:
        from wasabil_dte.models import WasabilDte, STATUS_EMITIDO
    except Exception:
        return {}, {}
    try:
        dte = (
            db.query(WasabilDte)
            .filter(WasabilDte.despacho_id == despacho_id,
                    WasabilDte.status_id == STATUS_EMITIDO)
            .first()
        )
    except Exception:
        return {}, {}  # tabla ausente / BD sin migrar: cae al recálculo (no rompe la emisión)
    if not dte or not dte.payload_json:
        return {}, {}
    try:
        detalles = json.loads(dte.payload_json).get("details", [])
    except (ValueError, TypeError):
        return {}, {}
    por_di, por_parte, vistos_parte = {}, {}, set()
    for d in detalles:
        if d.get("price") is None:
            continue
        ext = d.get("externalId")
        if ext is not None:
            try:
                por_di[int(ext)] = d["price"]
            except (ValueError, TypeError):
                pass
        code = (d.get("code") or "").strip()
        if code:
            if code in vistos_parte:
                por_parte.pop(code, None)   # duplicado → no confiable por n° de parte
            else:
                vistos_parte.add(code)
                por_parte[code] = d["price"]
    return por_di, por_parte


def _ligar_lineas_a_su_guia(desp: Despacho, lineas: list, nombres: dict) -> tuple:
    """Liga al ítem de despacho de LA GUÍA declarada (`desp`) toda línea que no lo declare.
    Devuelve (líneas nuevas, problemas). NO muta el payload: cada línea resuelta es una
    COPIA (regla de inmutabilidad de la casa).

    POR QUÉ (el agujero que cierra). El tope por GUÍA sólo se aplicaba «si se indicó
    despacho_item_id», y ese campo es OPCIONAL: mandando `items` sueltos contra
    `despacho_id` = guía A se facturaban unidades que salieron en la guía B, y la factura
    quedaba persistida con `despacho_item_id = NULL`. Consecuencias medidas: la referencia
    52 del DTE 33 citaba la guía que NO trasladó esa mercadería (documento tributario
    irreversible), el cinturón del reintento de wasabil_dte —que mira justamente esos
    despacho_item_id— quedaba CIEGO, y `_qty_facturada_por_despacho_item` (que ignora los
    NULL) no descontaba el cupo de la guía.

    Regla que queda: una línea facturada BAJO una guía está LIGADA a esa guía, siempre.
    Si no se puede ligar, no se factura — nunca se adivina:
      · 1 ítem de despacho de esa parte en la guía → se adopta su id (y con eso el tope
        por guía, que antes se saltaba, empieza a aplicar).
      · 0 → la mercadería no salió en esta guía: BLOQUEA (facturar cada guía por separado).
      · >1 (mismo ítem partido en dos líneas de la MISMA guía) → ambiguo: BLOQUEA pidiendo
        el despacho_item_id explícito. Fail closed: repartir la cantidad a dedo sería
        inventar de cuál de las dos salió."""
    por_item = {}
    for di in desp.items:
        por_item.setdefault(di.item_cotizacion_id, []).append(di)
    out, problemas = [], []
    guia = (desp.numero_guia or desp.numero_despacho or "").strip() or f"#{desp.id}"
    for ln in lineas:
        if ln.despacho_item_id is not None:
            out.append(ln)
            continue
        cands = por_item.get(ln.item_cotizacion_id) or []
        nombre = nombres.get(ln.item_cotizacion_id) or f"ítem {ln.item_cotizacion_id}"
        if len(cands) == 1:
            out.append(ln.model_copy(update={"despacho_item_id": cands[0].id}))
        elif not cands:
            problemas.append(f"{nombre} no salió en la guía {guia}: no se puede facturar "
                             f"bajo esta guía (factura la guía que trasladó esa mercadería)")
        else:
            problemas.append(f"{nombre} aparece en {len(cands)} líneas de la guía {guia}: "
                             f"indica el despacho_item_id de la que estás facturando")
    return out, problemas


def _construir_factura(db: Session, payload, oc: OcCliente, cot, empresa: str) -> dict:
    """Deriva y valida las líneas + montos de una factura SIN persistir ni tomar locks.
    Fuente ÚNICA de verdad: el preview y crear_factura la usan (lo que se previsualiza es
    EXACTAMENTE lo que se emite). Las reglas de negocio se acumulan en `problemas`
    (bloqueantes); no lanza excepciones (salvo estructurales ya cubiertas por el llamador).
    Devuelve validadas=[(it, ln, cantidad, precio2, total)], líneas de display, receptor,
    neto/iva/bruto, problemas (bloqueantes) y advertencias (informativas)."""
    problemas: List[str] = []
    advertencias: List[str] = []

    # ── RUT del receptor: el SII lo exige. payload.rut_cliente permite completarlo
    #    (campo por llenar en el modal) si la venta no lo trae. ──
    rut_payload = _rut_saneado(getattr(payload, "rut_cliente", None))
    rut_venta = _rut_saneado(cot.rut_cliente)
    rut_norm = rut_payload or rut_venta
    rut_mostrar = (getattr(payload, "rut_cliente", None) or cot.rut_cliente or "").strip()
    if not rut_norm:
        problemas.append("Falta el RUT del cliente: complétalo para emitir la factura")
    elif not _rut_valido(rut_norm):
        problemas.append(f"El RUT '{rut_mostrar}' no es válido (revisa el dígito verificador)")
    elif (rut_payload and rut_venta and _rut_valido(rut_venta)
          and _rut_canonico(rut_payload) != _rut_canonico(rut_venta)):
        # Ambos RUT válidos y REALMENTE distintos (comparación canónica: con/sin guión
        # es el mismo RUT): se factura con el ingresado, avisando — la venta conserva
        # su RUT (corregirlo allá si el de la venta está malo).
        advertencias.append(
            f"El RUT ingresado ({rut_mostrar}) difiere del registrado en la venta "
            f"({cot.rut_cliente}): se facturará con el ingresado"
        )

    # ── Razón social del receptor: mismo trato que el RUT (la factura la exige).
    #    Si la venta no la trae, el modal la pide (campo por llenar) y crear_factura
    #    la guarda en la venta para dejarla completa. ──
    razon_social = ((getattr(payload, "razon_social_cliente", None) or "").strip()
                    or (cot.cliente or "").strip())
    if not razon_social:
        problemas.append("Falta la razón social del cliente en la venta: complétala para emitir")

    # ── Referencias del documento (avisos, no bloqueantes) ──
    if not (oc.numero_oc or "").strip():
        advertencias.append("La OC no tiene número: la factura quedará sin referencia de OC "
                            "(complétalo en la venta antes de emitir si el cliente lo exige)")

    cfg_dict = _cfg_to_dict(
        db.query(ConfiguracionCotizador).filter(ConfiguracionCotizador.id == 1).first()
    )
    items_db, pmap, _ = _precios_de_cotizacion(db, cot.id, cfg_dict)
    items_by_id = {i.id: i for i in items_db}
    # Precios congelados de la guía electrónica ya emitida (si la hay): la factura debe
    # cuadrar con la guía 52 enviada al SII, no recalcular con la config de hoy.
    congel_di, congel_parte = _precios_congelados_guia(db, payload.despacho_id)
    hay_congelados = bool(congel_di or congel_parte)
    n_congeladas = 0

    # Despachos DESPACHADOS de la OC (lo que se puede facturar)
    desp_items = _despacho_items_de_oc(db, oc.id)
    di_by_id = {di.id: di for di, _d in desp_items}
    desp_qty_item = _qty_despachada_por_item(db, oc.id)
    fact_qty_item = _qty_facturada_por_item(db, oc.id)
    fact_qty_di = _qty_facturada_por_despacho_item(db, oc.id)

    # La guía (despacho_id) se valida SIEMPRE: la factura no puede quedar ligada a una
    # guía ajena o sin firmar.
    desp = None
    guia_ok = False
    if payload.despacho_id:
        desp = db.query(Despacho).filter(
            Despacho.id == payload.despacho_id, Despacho.oc_cliente_id == oc.id
        ).first()
        if not desp:
            problemas.append("Despacho no encontrado para esta OC")
        elif desp.estado != "despachado" or not desp.guia_firmada:
            problemas.append("Solo se puede facturar una guía de despacho FIRMADA "
                             "(entregada y firmada por el cliente)")
        else:
            guia_ok = True
            if not (desp.numero_guia or "").strip():
                advertencias.append("El despacho no tiene N° de guía registrado: la factura "
                                    "quedará sin referencia de guía (complétalo en Despachos)")

    # Determinar líneas a facturar
    lineas: List[FacturaItemIn] = []
    if payload.items:
        lineas = payload.items
    elif guia_ok:
        # Derivar líneas acotando por guía (despacho_item) Y por ítem físico
        usado_deriv = {}
        for di in desp.items:
            disp_di = _f(di.qty_despachada) - fact_qty_di.get(di.id, 0.0)
            disp_item = (
                desp_qty_item.get(di.item_cotizacion_id, 0.0)
                - fact_qty_item.get(di.item_cotizacion_id, 0.0)
                - usado_deriv.get(di.item_cotizacion_id, 0.0)
            )
            disponible = min(disp_di, disp_item)
            if disponible > TOL_QTY:
                lineas.append(FacturaItemIn(
                    item_cotizacion_id=di.item_cotizacion_id,
                    despacho_item_id=di.id, cantidad=round(disponible, 4),
                ))
                usado_deriv[di.item_cotizacion_id] = usado_deriv.get(di.item_cotizacion_id, 0.0) + disponible
        if not lineas:
            problemas.append("El despacho ya fue facturado por completo")
    if not lineas and not problemas:
        problemas.append("Debe indicar ítems o un despacho a facturar")

    # Toda línea que se factura BAJO una guía queda ligada al ítem de despacho de ESA guía
    # (ver _ligar_lineas_a_su_guia). En el camino derivado ya vienen ligadas y esto es un
    # no-op; el que importa es el de `items` sueltos + despacho_id, por donde entraba una
    # línea de otra guía con `despacho_item_id = NULL`.
    if guia_ok and desp is not None and lineas:
        lineas, problemas_liga = _ligar_lineas_a_su_guia(
            desp, lineas, {i.id: (i.numero_parte or "").strip() for i in items_db})
        problemas.extend(problemas_liga)

    # Validación por línea con acumuladores unificados (guía + ítem) en este request
    usado_di, usado_item = {}, {}
    validadas, display = [], []
    for ln in lineas:
        it = items_by_id.get(ln.item_cotizacion_id)
        if not it:
            problemas.append(f"Ítem {ln.item_cotizacion_id} no pertenece a esta OC"); continue
        cantidad = ln.cantidad if ln.cantidad is not None else _f(it.cantidad)
        if cantidad <= 0:
            problemas.append(f"Cantidad inválida para {it.numero_parte}"); continue
        # Tope a nivel de ÍTEM (lo despachado y aún no facturado) — en TODAS las rutas
        despachado_item = desp_qty_item.get(ln.item_cotizacion_id, 0.0)
        if despachado_item <= 0:
            problemas.append(f"{it.numero_parte} no ha sido despachado; no se puede facturar"); continue
        disponible = (
            despachado_item
            - fact_qty_item.get(ln.item_cotizacion_id, 0.0)
            - usado_item.get(ln.item_cotizacion_id, 0.0)
        )
        # Tope adicional a nivel de GUÍA si se indicó despacho_item_id
        if ln.despacho_item_id is not None:
            di = di_by_id.get(ln.despacho_item_id)
            if not di or di.item_cotizacion_id != ln.item_cotizacion_id:
                problemas.append(f"Guía/despacho inválido para {it.numero_parte}"); continue
            disp_di = _f(di.qty_despachada) - fact_qty_di.get(di.id, 0.0) - usado_di.get(di.id, 0.0)
            disponible = min(disponible, disp_di)
        if cantidad > disponible + TOL_QTY:
            problemas.append(f"{it.numero_parte}: cantidad excede lo despachado/no facturado "
                             f"(disp {max(disponible,0):.0f})"); continue

        ci = pmap.get(ln.item_cotizacion_id, {})
        parte = (it.numero_parte or "").strip()
        if ln.precio_unit_neto is not None:
            precio = ln.precio_unit_neto  # precio explícito del payload manda
        elif ln.despacho_item_id is not None and ln.despacho_item_id in congel_di:
            precio = _f(congel_di[ln.despacho_item_id]); n_congeladas += 1  # match 1:1
        elif parte and parte in congel_parte:
            precio = _f(congel_parte[parte]); n_congeladas += 1  # fallback guía antigua
        else:
            precio = _f(ci.get("precio_venta_clp"))  # recálculo (guía manual / sin match)
        if precio <= 0:  # antes '< 0': una línea en $0 se auto-marcaba 'pagada'
            problemas.append(f"{it.numero_parte}: sin precio de venta, no se puede facturar en $0"); continue

        usado_item[ln.item_cotizacion_id] = usado_item.get(ln.item_cotizacion_id, 0.0) + cantidad
        if ln.despacho_item_id is not None:
            usado_di[ln.despacho_item_id] = usado_di.get(ln.despacho_item_id, 0.0) + cantidad
        p2 = _precio2(precio)
        total = _total_linea(precio, cantidad)  # redondea precio a 2 dec ANTES de ×qty (== guía)
        validadas.append((it, ln, cantidad, p2, total))
        display.append({
            "item_cotizacion_id": it.id, "numero_parte": it.numero_parte,
            "descripcion": it.descripcion, "cantidad": cantidad,
            "precio_unit_neto": p2, "total_neto": total,
        })

    # precio_de_guia: True SOLO si TODAS las líneas tomaron el precio congelado (banner
    # honesto). Si hay guía emitida pero alguna línea se recalculó (n° de parte sin match),
    # se avisa: esa parte podría no cuadrar con la guía ya enviada al SII.
    todas_congeladas = bool(validadas) and n_congeladas == len(validadas)
    if hay_congelados and validadas and n_congeladas < len(validadas):
        advertencias.append(
            "Algunas líneas no se pudieron cuadrar con el precio de la guía electrónica "
            "ya emitida y se recalcularon: revisa que el monto coincida con la guía 52."
        )
    neto_items = float(sum(t for *_r, t in validadas))

    # ── Descuento AUTOMÁTICO por anticipos facturados (facturas es_anticipo=1 de la
    #    OC con neto aún no descontado). Cada descuento es una línea NEGATIVA que
    #    referencia el folio de la factura de anticipo; así Σ brutos de las facturas
    #    de la OC nunca supera el total de la venta y el cliente no paga dos veces. ──
    descuentos = []
    if validadas and not problemas:
        restante = neto_items
        for fa, pend in _anticipos_pendientes_de_descuento(db, oc.id, empresa):
            if restante <= TOL:
                break
            d = round(min(pend, restante), 2)
            if d <= TOL:
                continue
            # Anticipo SIN folio SII (emisión electrónica en vuelo o rechazada): no es
            # un documento tributario todavía. Ni se descuenta (la glosa citaría un folio
            # falso '#<id>' y esa mercadería quedaría fuera de toda factura) ni se ignora
            # (facturar el total le cobraría dos veces al cliente): se BLOQUEA hasta que
            # esa emisión se resuelva — reintentándola o eliminándola.
            if not (fa.numero_factura or "").strip():
                problemas.append(
                    f"La factura de anticipo #{fa.id} no tiene folio del SII (su emisión "
                    "electrónica está en curso o falló): resuélvela antes de facturar este "
                    "despacho, o el anticipo no se podrá descontar")
                continue
            folio = fa.numero_factura
            descuentos.append({"anticipo_factura_id": fa.id, "folio": folio, "monto_neto": d})
            display.append({
                "item_cotizacion_id": None, "numero_parte": "DESCUENTO",
                "descripcion": f"Descuento anticipo Factura N° {folio}",
                "cantidad": 1, "precio_unit_neto": -d, "total_neto": -d,
                "anticipo_factura_id": fa.id,
            })
            restante = round(restante - d, 2)
    neto = round(neto_items - sum(d["monto_neto"] for d in descuentos), 2)
    if descuentos and neto <= TOL:
        advertencias.append(
            "El descuento por anticipo deja esta factura en $0 (el anticipo ya cubría "
            "todo lo despachado): verifica que sea lo esperado antes de emitir."
        )
    iva = _iva_clp(neto) if neto else 0.0

    # Tope Σ brutos ≤ total venta cuando alguna línea trae PRECIO EXPLÍCITO del
    # payload: los precios derivados (guía congelada / pricing) respetan el
    # invariante por construcción, pero un precio del payload podría inflar la
    # factura por sobre la venta (espejo del tope de _construir_factura_anticipo).
    # ADEMÁS (cierre de paridad 2026-07-28): si lo YA facturado consumió el total
    # completo, se bloquea CUALQUIER factura nueva — sin esto, una explícita inflada
    # que agotó el total dejaba pasar a la derivada siguiente en silencio (bypass
    # por orden). Deliberadamente NO se chequea el invariante completo en derivadas
    # con total parcial: las ventas SIN foto de TC recalculan el total con el config
    # vivo y una tanda vieja legítima podría exceder el total nuevo (falso 409).
    if validadas and not problemas:
        hay_explicito = any(ln.precio_unit_neto is not None for _it, ln, *_r in validadas)
        total_venta = _total_bruto_venta(db, cot.id)
        if total_venta > 0:
            facturas_previas = _facturas_de_oc(db, oc.id, empresa)
            facturado = sum(_f(f.monto_bruto) for f in facturas_previas)
            tope_agotado = facturado >= total_venta - TOL_PAGO * (len(facturas_previas) + 1)
            if (hay_explicito or tope_agotado) and \
                    facturado + neto + iva > total_venta + TOL_PAGO * (len(facturas_previas) + 1):
                disponible = max(total_venta - facturado, 0.0)
                problemas.append(
                    f"La factura excede el total de la venta "
                    f"(disponible bruto {disponible:,.0f} de {total_venta:,.0f})".replace(",", ".")
                )

    # Giro y dirección del receptor (solo lectura, para que el kit de emisión esté
    # completo): del maestro de Clientes por RUT, con la dirección de la venta como
    # respaldo. Si no hay giro, el preview lo muestra vacío (se completa en Clientes).
    # Match CANÓNICO en SQL (sin puntos/espacios/guión): cruza cualquier formato de
    # RUT sin cargar la tabla completa.
    giro, direccion = None, (cot.direccion_cliente or "").strip() or None
    if rut_norm:
        rut_sql = func.upper(func.replace(func.replace(func.replace(
            Cliente.rut, ".", ""), " ", ""), "-", ""))
        cli = db.query(Cliente).filter(rut_sql == _rut_canonico(rut_norm)).first()
        if cli:
            giro = (cli.giro or "").strip() or None
            direccion = (cli.direccion or "").strip() or direccion

    return {
        "desp": desp, "validadas": validadas, "lineas": display,
        "receptor": {
            "rut": rut_mostrar, "rut_normalizado": rut_norm,
            "razon_social": razon_social,
            "rut_en_venta": bool(_rut_saneado(cot.rut_cliente)),
            "giro": giro, "direccion": direccion,
        },
        "neto": neto, "iva": iva, "bruto": neto + iva,
        "descuentos": descuentos,  # [(factura de anticipo, folio, neto a descontar)]
        "precio_de_guia": todas_congeladas,  # todas las líneas cuadran con la guía 52
        "advertencias": advertencias,
        "problemas": problemas,
    }


def _construir_factura_anticipo(db: Session, payload, oc: OcCliente, cot, empresa: str) -> dict:
    """Deriva y valida una factura de ANTICIPO (misma forma de salida que
    _construir_factura, para reutilizar preview/emisión/serialización). EXCEPCIÓN a la
    regla rectora: NO exige guía de despacho firmada — respalda un adelanto del cliente
    ante el SII. Una sola línea 'Anticipo OC …' con el neto indicado; sin ítems físicos
    (no toca los topes por ítem/guía). Tope: Σ brutos de las facturas de la OC (anticipos
    incluidos) ≤ total de la venta."""
    problemas: List[str] = []
    advertencias: List[str] = []

    rut_norm = _rut_saneado(getattr(payload, "rut_cliente", None)) or _rut_saneado(cot.rut_cliente)
    rut_mostrar = (getattr(payload, "rut_cliente", None) or cot.rut_cliente or "").strip()
    if not rut_norm:
        problemas.append("Falta el RUT del cliente: complétalo para emitir la factura")
    elif not _rut_valido(rut_norm):
        problemas.append(f"El RUT '{rut_mostrar}' no es válido (revisa el dígito verificador)")
    # Mismo trato que la factura normal (_construir_factura): la única excepción
    # de la vía B es la guía firmada, no la completitud del receptor. Si la venta
    # no trae la razón social, el modal la pide y crear_factura la guarda.
    razon_social = ((getattr(payload, "razon_social_cliente", None) or "").strip()
                    or (cot.cliente or "").strip())
    if not razon_social:
        problemas.append("Falta la razón social del cliente en la venta: complétala para emitir la factura")

    neto = round(_f(getattr(payload, "monto_neto_anticipo", None)), 2)
    if neto <= 0:
        problemas.append("Indica el monto NETO del anticipo (mayor a 0)")

    # UN anticipo por venta salvo confirmación EXPLÍCITA. Emitir un DTE 33 es
    # IRREVERSIBLE y el candado anti doble emisión de wasabil_dte solo dura mientras el
    # HTTP está en vuelo: apenas responde la primera emisión, la segunda pasa libre. Sin
    # esto, dos clics tranquilos emitían DOS facturas de anticipo REALES por el mismo
    # adelanto (y el tope Σ brutos no lo frena: dos anticipos de la mitad caben perfecto).
    # Aquí NO se prohíbe —a diferencia de Monza, en Grupo AM una OC puede tener VARIOS
    # adelantos y cada uno se liga a SU anticipo por cont_adelanto.factura_anticipo_id, así
    # que el anticipo parcial pactado es un caso legítimo—: se exige decirlo a propósito
    # (confirmar_segundo_anticipo), con el folio y el monto del que ya existe a la vista.
    # Espejo del bloqueo que _construir_factura ya hacía en el otro sentido (un anticipo
    # sin folio frena la factura del despacho).
    if not getattr(payload, "confirmar_segundo_anticipo", False):
        previo = (
            db.query(ContFacturaCliente)
            .filter(ContFacturaCliente.oc_cliente_id == oc.id,
                    ContFacturaCliente.empresa == empresa,
                    ContFacturaCliente.es_anticipo == 1)
            .order_by(ContFacturaCliente.id.asc()).first()
        )
        if previo is not None:
            ident = (previo.numero_factura or "").strip() or f"#{previo.id}"
            monto_txt = f"{_f(previo.monto_bruto):,.0f}".replace(",", ".")
            problemas.append(
                f"Esta venta ya tiene una factura de anticipo (N° {ident}, ${monto_txt}). "
                "Si de verdad necesitas un segundo anticipo, márcalo explícitamente."
            )

    # Tope: lo ya facturado (todas las facturas de la OC: anticipos brutos + finales ya
    # con descuento) + este anticipo no puede superar el total de la venta.
    iva = _iva_clp(neto) if neto > 0 else 0.0
    bruto = round(neto + iva, 2)
    total_venta = _total_bruto_venta(db, cot.id)
    if neto > 0 and total_venta > 0:
        facturado = sum(_f(f.monto_bruto) for f in _facturas_de_oc(db, oc.id, empresa))
        if facturado + bruto > total_venta + TOL_PAGO:
            disponible = max(total_venta - facturado, 0.0)
            problemas.append(
                f"El anticipo excede lo aún no facturado de la venta "
                f"(disponible bruto {disponible:,.0f} de {total_venta:,.0f})".replace(",", ".")
            )

    # Adelantos que respalda (vía B): deben ser de esta OC, no anulados, sin factura de
    # anticipo previa y sin aplicaciones (cambiar de vía solo con monto_aplicado == 0).
    adelanto_ids = list(getattr(payload, "adelanto_ids", None) or [])
    adelantos = []
    if adelanto_ids:
        adelantos = (db.query(ContAdelanto)
                     .filter(ContAdelanto.id.in_(adelanto_ids)).all())
        found = {a.id for a in adelantos}
        for aid in adelanto_ids:
            if aid not in found:
                problemas.append(f"Adelanto {aid} no encontrado")
        for a in adelantos:
            if a.oc_cliente_id != oc.id:
                problemas.append(f"El adelanto {a.id} no pertenece a esta OC")
            elif a.estado == "anulado":
                problemas.append(f"El adelanto {a.id} está anulado")
            elif a.factura_anticipo_id:
                problemas.append(f"El adelanto {a.id} ya está respaldado por otra factura de anticipo")
            elif _f(a.monto_aplicado) > TOL:
                problemas.append(f"El adelanto {a.id} ya fue aplicado a una factura; revierta esa cobranza primero")

    descripcion = (getattr(payload, "descripcion_anticipo", None)
                   or f"Anticipo OC {oc.numero_oc or oc.id}").strip()
    display = [{
        "item_cotizacion_id": None, "numero_parte": "ANTICIPO",
        "descripcion": descripcion, "cantidad": 1,
        "precio_unit_neto": neto, "total_neto": neto,
    }]
    return {
        "desp": None, "validadas": [], "lineas": display,
        "receptor": {
            "rut": rut_mostrar, "rut_normalizado": rut_norm,
            "razon_social": razon_social,
            "rut_en_venta": bool(_rut_saneado(cot.rut_cliente)),
        },
        "neto": neto, "iva": iva, "bruto": bruto,
        "descuentos": [],
        "descripcion_anticipo": descripcion,
        "adelantos": adelantos,
        "precio_de_guia": False,
        "advertencias": advertencias,
        "problemas": problemas,
    }


@router.post("/facturas/preview")
def preview_factura(
    payload: FacturaPreview,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Previsualiza la factura ANTES de emitir: líneas, montos (neto/IVA half-up/total),
    receptor y problemas bloqueantes. NO persiste ni congela nada. `puede_emitir` es True
    solo si no hay problemas de datos (el folio se valida aparte, al emitir)."""
    oc = db.query(OcCliente).filter(OcCliente.id == payload.oc_cliente_id).first()
    if not oc or not oc.cotizacion:
        raise HTTPException(404, "OC Cliente no encontrada")
    empresa = getattr(current_user, "empresa", None) or "mineria"
    if payload.es_anticipo:
        datos = _construir_factura_anticipo(db, payload, oc, oc.cotizacion, empresa)
    else:
        datos = _construir_factura(db, payload, oc, oc.cotizacion, empresa)
    return {
        "puede_emitir": not datos["problemas"],
        "problemas": datos["problemas"],
        "advertencias": datos["advertencias"],
        "receptor": datos["receptor"],
        "lineas": datos["lineas"],
        "totales": {"neto": datos["neto"], "iva": datos["iva"], "bruto": datos["bruto"]},
        "precio_de_guia": datos["precio_de_guia"],
        "es_anticipo": bool(payload.es_anticipo),
        "descuentos": datos.get("descuentos", []),
    }


@router.post("/facturas")
def crear_factura(
    payload: FacturaCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """EMITE una factura a cliente. Dos modos: `payload.despacho_id` (deriva las líneas SOLO
    de una guía despachada y FIRMADA) o `payload.items` explícitos. Reglas: folio único y
    obligatorio (tipo 'factura'); RUT del cliente válido; no facturar más de lo despachado-
    y-no-facturado (doble tope por ÍTEM y por GUÍA); congela montos (neto, IVA 19%, bruto)."""
    empresa = getattr(current_user, "empresa", None) or "mineria"
    # Lock de fila de la OC: serializa la facturación concurrente de la misma venta
    # (evita que dos requests lean el mismo "ya facturado" y sobre-facturen).
    oc = (
        db.query(OcCliente)
        .filter(OcCliente.id == payload.oc_cliente_id)
        .with_for_update()
        .first()
    )
    if not oc or not oc.cotizacion:
        raise HTTPException(404, "OC Cliente no encontrada")
    cot = oc.cotizacion

    # Folio SII: obligatorio para tipo 'factura' (hoy se digita a mano del DTE emitido);
    # único por empresa.
    tipo_doc = payload.tipo_doc or "factura"
    if payload.es_anticipo and tipo_doc != "factura":
        raise HTTPException(400, "La factura de anticipo debe ser tipo 'factura' (no boleta)")
    # Una BOLETA no puede descontar un anticipo: el descuento se apoya en una referencia al
    # FOLIO de una factura y una boleta no referencia facturas. Sin este guard salía una
    # boleta con la línea «Descuento anticipo Factura N° …» —insostenible ante el SII— y
    # además ese descuento CONSUMÍA el pendiente del anticipo (se deriva de esas líneas,
    # ver _anticipos_pendientes_de_descuento): el anticipo quedaba contablemente descontado
    # contra un documento que no puede citarlo. La mercadería de una venta con anticipo se
    # FACTURA. Molde monza_contabilidad/router.py (su hallazgo A-5); acá el guard vive en
    # crear_factura porque en Grupo AM el `tipo_doc` no llega al constructor (la vía
    # electrónica ya rechaza todo lo que no sea factura, wasabil_dte/router.py).
    if tipo_doc != "factura" and not payload.es_anticipo and \
            _anticipos_pendientes_de_descuento(db, oc.id, empresa):
        raise HTTPException(
            400, "Esta venta tiene una factura de anticipo por descontar: la mercadería "
                 "debe emitirse como FACTURA (no boleta), que es el único documento que "
                 "puede referenciar el folio del anticipo")
    folio = (payload.numero_factura or "").strip()
    if tipo_doc == "factura" and not folio:
        raise HTTPException(400, "Ingresa el folio SII de la factura (o cámbialo a boleta)")
    # Folio NUMÉRICO cuando la factura es un ANTICIPO. La factura del despacho lo va a citar
    # en una referencia tipo 33, y el SII exige que el FolioRef sea el folio correlativo
    # —un número— del DTE referenciado. wasabil_dte/service.py lo valida al armar esa
    # referencia, pero ahí es TARDE y LEJOS: el folio se teclea AQUÍ, al registrar un
    # anticipo ya emitido, y el error aparecía semanas después al facturar el despacho —que
    # no se puede emitir hasta arreglarlo— con el anticipo ya descontando cupo. Reproducido
    # con 'N/A-99', 'FAC 123', 'N/A' y '0'. SOLO sobre el anticipo: una factura normal puede
    # traer un folio legado no numérico y romper su registro sería una regresión gratuita
    # (nadie referencia esas). (`isascii` además de `isdigit`: '٣'.isdigit() es True y no es
    # un folio del SII.) El tope de 18 caracteres es el del folio de una referencia del SII
    # y de paso evita que el int() sobre miles de dígitos reviente por el límite de Python.
    if payload.es_anticipo and folio and (
            len(folio) > 18 or not (folio.isascii() and folio.isdigit()) or int(folio) <= 0):
        raise HTTPException(
            400,
            f"El folio de la factura de anticipo ('{folio}') debe ser el número "
            "correlativo que le asignó el SII: la factura de la mercadería lo va a "
            "referenciar (referencia tipo 33) y el SII solo acepta folios numéricos de "
            "hasta 18 dígitos. Escríbelo con dígitos, sin letras, guiones ni espacios.")
    if folio:
        dup = db.query(ContFacturaCliente).filter(
            ContFacturaCliente.empresa == empresa,
            ContFacturaCliente.numero_factura == folio,
        ).first()
        if dup:
            raise HTTPException(409, f"El folio {folio} ya existe para esta empresa")

    if payload.es_anticipo:
        datos = _construir_factura_anticipo(db, payload, oc, cot, empresa)
    else:
        datos = _construir_factura(db, payload, oc, cot, empresa)
    if datos["problemas"]:
        raise HTTPException(409, " · ".join(datos["problemas"]))

    try:
        factura = _persistir_factura(
            db, payload, oc, cot, datos, folio=folio or None, tipo_doc=tipo_doc,
            empresa=empresa, usuario_id=getattr(current_user, "id", None),
        )
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as e:
        db.rollback()
        if "uq_cont_factura_empresa_folio" in str(getattr(e, "orig", e)):
            raise HTTPException(409, "Folio de factura duplicado para esta empresa")
        raise HTTPException(409, "No se pudo guardar la factura (conflicto de integridad)")
    db.refresh(factura, with_for_update=True)
    return _serialize_factura(factura)


def _persistir_factura(db: Session, payload, oc: OcCliente, cot, datos: dict, *,
                       folio: Optional[str], tipo_doc: str, empresa: str,
                       usuario_id: Optional[int], aplicar_adelantos: bool = True):
    """Persiste la factura + líneas + vínculos de adelantos a partir de `datos`
    (la salida de _construir_factura / _construir_factura_anticipo). NO hace
    commit: la transacción la decide el llamador (crear_factura commitea aquí
    mismo; la emisión electrónica de wasabil_dte persiste SIN folio y commitea
    junto con su claim anti doble emisión).

    `aplicar_adelantos=False` (emisión electrónica): la aplicación de adelantos
    como cobranza se DIFIERE hasta que el SII confirme el folio (status 3) — una
    factura rechazada por el SII no debe haber movido plata. Requiere el lock de
    la OC ya tomado por el llamador (igual que crear_factura)."""
    # Completar el RUT en la venta si venía vacío O INVÁLIDO (el campo por llenar del
    # modal deja la venta lista; un RUT guardado con dígito verificador malo se corrige
    # con el validado que se acaba de usar para emitir)
    if _rut_saneado(payload.rut_cliente) and (
        not _rut_saneado(cot.rut_cliente) or not _rut_valido(cot.rut_cliente)
    ):
        cot.rut_cliente = payload.rut_cliente.strip()
    # Razón social: mismo trato — el campo por llenar del modal completa la venta
    # (solo si venía vacía; una razón social ya guardada no se pisa desde aquí)
    razon_payload = (getattr(payload, "razon_social_cliente", None) or "").strip()
    if razon_payload and not (cot.cliente or "").strip():
        cot.cliente = razon_payload

    fecha_emision = _parse_date(payload.fecha_emision) or _hoy_chile()
    # `is not None`: plazo 0 días (contado) también debe generar vencimiento (= emisión)
    fecha_venc = (fecha_emision + timedelta(days=int(payload.plazo_dias))
                  if payload.plazo_dias is not None else None)

    factura = ContFacturaCliente(
        empresa=empresa,
        oc_cliente_id=oc.id, cotizacion_id=cot.id,
        despacho_id=None if payload.es_anticipo else payload.despacho_id,
        es_anticipo=1 if payload.es_anticipo else 0,
        numero_factura=folio or None, tipo_doc=tipo_doc,
        fecha_emision=fecha_emision, condicion_pago=payload.condicion_pago,
        plazo_dias=payload.plazo_dias, fecha_vencimiento=fecha_venc,
        observaciones=payload.observaciones, usuario_id=usuario_id,
    )
    db.add(factura)
    db.flush()
    if payload.es_anticipo:
        # Línea única "Anticipo OC …" (sin ítem físico ni guía)
        db.add(ContFacturaClienteItem(
            factura_id=factura.id, numero_parte="ANTICIPO",
            descripcion=datos["descripcion_anticipo"],
            cantidad=1, precio_unit_neto=datos["neto"], total_neto=datos["neto"],
        ))
        # Vínculo adelanto → factura de anticipo (vía B). Re-validado BAJO LOCK del
        # adelanto: anular/editar solo bloquean la fila del adelanto (no la OC), así
        # que el chequeo del preview pudo quedar obsoleto.
        if payload.adelanto_ids:
            # populate_existing: la re-validación decide plata (monto_aplicado) y el
            # preview de esta misma request ya cargó estos adelantos con una lectura
            # PLANA — sin él SQLAlchemy sirve esa copia del identity map y el
            # FOR UPDATE queda decorativo (regla de lecturas de plata, regla 3).
            adelantos_link = (db.query(ContAdelanto)
                              .filter(ContAdelanto.id.in_(payload.adelanto_ids))
                              .populate_existing().with_for_update().all())
            if len(adelantos_link) != len(set(payload.adelanto_ids)):
                raise HTTPException(409, "Alguno de los adelantos indicados ya no existe")
            for a in adelantos_link:
                if (a.oc_cliente_id != oc.id or a.estado == "anulado"
                        or a.factura_anticipo_id or _f(a.monto_aplicado) > TOL):
                    raise HTTPException(
                        409, f"El adelanto {a.id} cambió de estado; recarga e intenta de nuevo")
                a.factura_anticipo_id = factura.id
    else:
        for it, ln, cantidad, p2, total in datos["validadas"]:
            # despacho_item_id: con una guía declarada SIEMPRE viene (lo garantiza
            # _ligar_lineas_a_su_guia; antes podía quedar NULL y con eso el cinturón del
            # reintento de wasabil_dte —que valida las líneas contra la guía que la 52
            # cita— se quedaba sin nada que mirar, y el cupo de la guía sin descontar).
            db.add(ContFacturaClienteItem(
                factura_id=factura.id, item_cotizacion_id=ln.item_cotizacion_id,
                despacho_item_id=ln.despacho_item_id,
                numero_parte=it.numero_parte, descripcion=it.descripcion,
                cantidad=cantidad, precio_unit_neto=p2, total_neto=total,
            ))
        # Líneas de DESCUENTO por anticipo facturado (negativas, referencian a la
        # factura de anticipo — el pendiente de descontar se deriva de estas líneas)
        for dsc in datos.get("descuentos", []):
            db.add(ContFacturaClienteItem(
                factura_id=factura.id, anticipo_factura_id=dsc["anticipo_factura_id"],
                numero_parte="DESCUENTO",
                descripcion=f"Descuento anticipo Factura N° {dsc['folio']}",
                cantidad=1, precio_unit_neto=-dsc["monto_neto"],
                total_neto=-dsc["monto_neto"],
            ))
    factura.monto_neto = datos["neto"]
    factura.iva = datos["iva"]
    factura.monto_bruto = datos["bruto"]
    db.flush()
    _recompute_factura(factura)
    if aplicar_adelantos:
        # Aplicación automática de adelantos APROBADOS (cobranza medio='adelanto'):
        # a la factura de anticipo le caen SUS adelantos (vía B); a la normal, los que
        # no tienen factura de anticipo (vía A). Bajo el lock de la OC ya tomado.
        _aplicar_adelantos_pendientes(db, oc, factura, usuario_id=usuario_id)
    return factura


class CobranzaIn(BaseModel):
    """Un pago real del cliente. medio: transferencia|cheque|efectivo (las cobranzas de
    factoring NO van por aquí; se generan desde el panel de factoring)."""
    fecha: Optional[str] = None
    monto: float = Field(..., gt=0)
    medio: str = "transferencia"
    banco: Optional[str] = None
    numero_operacion: Optional[str] = None
    observaciones: Optional[str] = None


@router.post("/facturas/{factura_id}/cobranzas")
def registrar_cobranza(
    factura_id: int,
    payload: CobranzaIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Registra un pago real del cliente. Bloquea la factura (lock de fila), rechaza medios de
    factoring, la factura de ANTICIPO y el SOBRE-PAGO (recalcula el saldo desde las cobranzas
    reales). Si la factura tiene factoring vigente, exige liquidarlo antes; si es una factura
    ELECTRÓNICA que el SII todavía no confirmó (sin folio, DTE en vuelo/rechazado), tampoco
    recibe plata hasta que llegue el folio. Recalcula saldo y estado."""
    # populate_existing: regla 3 de docs/regla-lecturas-de-plata.md — es OBLIGATORIO en toda
    # lectura bloqueante, porque si la fila ya está en la sesión SQLAlchemy devuelve el
    # objeto cacheado y DESCARTA los valores frescos que trajo el FOR UPDATE (a cualquier
    # nivel de aislamiento). Importa acá porque los campos que se validan abajo
    # (es_anticipo, monto_bruto) deciden si entra plata y cuánta. Paridad con
    # monza_contabilidad/router.py:registrar_cobranza, que sí lo tenía.
    factura = (
        db.query(ContFacturaCliente)
        .filter(ContFacturaCliente.id == factura_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if not factura:
        raise HTTPException(404, "Factura no encontrada")
    if payload.monto <= 0:
        raise HTTPException(400, "El monto debe ser mayor a 0")
    if _es_medio_factoring(payload.medio):
        raise HTTPException(400, "Las cobranzas de factoring se gestionan desde el panel de factoring")
    if (payload.medio or "") == MEDIO_ADELANTO:
        raise HTTPException(400, "Las cobranzas de adelanto las genera el sistema al aplicar un adelanto aprobado por Tesorería")
    # PARIDAD CON MONZA (monza_contabilidad/router.py:registrar_cobranza): una factura de
    # ANTICIPO (vía B) se salda SOLO con la aplicación del adelanto que aprueba Tesorería.
    # Si un administrativo la salda A MANO con la transferencia del cliente, ese mismo
    # depósito se cuenta DOS VECES: _aplicar_adelantos_pendientes ve el anticipo ya sin
    # saldo y aplica 0 (corta en `saldo <= TOL_PAGO`), pero lo cuenta como SALDADO, así que
    # libera el adelanto ligado y su plata cae COMPLETA en la factura del despacho real.
    # Reproducido: el cliente depositó $59.500 y la venta quedó facturada 119.000 / cobrada
    # 119.000, con saldo 0. La empresa deja de perseguir plata que nunca entró.
    # Mismo espíritu que el rechazo de medio='adelanto' de arriba: la plata del adelanto
    # entra por UNA sola puerta (Tesorería).
    if factura.es_anticipo:
        raise HTTPException(409, "Una factura de anticipo se salda con el adelanto que aprueba "
                                 "Tesorería, no con una cobranza manual: informe el depósito "
                                 "en Tesorería y apruébelo ahí")
    # Lecturas BLOQUEANTES (no las relaciones perezosas): ver _cobranzas_bloqueadas.
    # Sin esto, dos cobranzas simultáneas por el saldo completo pasaban AMBAS el tope.
    fact_vig = _factoring_bloqueado(db, factura.id)
    if fact_vig and fact_vig.estado == "vigente":
        raise HTTPException(409, "La factura tiene un factoring vigente; liquide el factoring antes de registrar cobranzas")
    # Guard SII (paridad con monza_contabilidad/router.py:registrar_cobranza): espejo del
    # que _aplicar_adelantos_pendientes ya tenía para la plata AUTOMÁTICA. Una factura
    # ELECTRÓNICA sin folio y con su DTE en vuelo / borrador / rechazado no debe recibir
    # plata: si el SII termina rechazándola, quedaba dinero contabilizado contra un
    # documento tributario que nunca existió, la factura marcada 'pagada' y —peor— zombi
    # IMBORRABLE («revierta las cobranzas antes de eliminar»), secuestrando el cupo
    # facturable de esa mercadería. La asimetría era interna: el adelanto protegido, el
    # pago manual no. Apenas el SII confirma el folio, la misma cobranza se acepta.
    # (La regla completa —y por qué es un helper compartido con el factoring— en
    # _plata_bloqueada_por_sii.)
    _exigir_sii_emitido(db, factura, "registrar pagos")
    # Recalcular el saldo desde las cobranzas reales dentro de la transacción (no del campo cacheado)
    cobs_frescas = _cobranzas_bloqueadas(db, factura.id)
    pagado_actual = sum(_f(c.monto) for c in cobs_frescas)
    saldo_actual = round(_f(factura.monto_bruto) - pagado_actual, 2)
    if payload.monto > saldo_actual + TOL_PAGO:
        raise HTTPException(400, f"El monto excede el saldo pendiente ({max(saldo_actual, 0):.0f})")
    # Fecha de CHILE cuando el operador no la indica (no date.today(), que es UTC en el
    # server): un pago registrado pasadas las ~20:00 de Chile quedaba fechado al día
    # siguiente y a fin de mes caía en el PERÍODO CONTABLE equivocado. Mismo helper que
    # ya usaba fecha_emision (_hoy_chile).
    nueva = ContCobranza(
        factura_id=factura.id, fecha=_parse_date(payload.fecha) or _hoy_chile(),
        monto=payload.monto, medio=payload.medio or "transferencia",
        banco=payload.banco, numero_operacion=payload.numero_operacion,
        observaciones=payload.observaciones, usuario_id=getattr(current_user, "id", None),
    )
    db.add(nueva)
    db.flush()
    db.refresh(factura, with_for_update=True)
    # Totales desde la lectura BLOQUEANTE + la fila recién agregada (la relación
    # perezosa serviría el snapshot viejo y persistiría un saldo equivocado).
    _recompute_factura(factura, cobranzas=cobs_frescas + [nueva])
    db.commit()
    db.refresh(factura, with_for_update=True)
    return _serialize_factura(factura)


@router.delete("/facturas/{factura_id}/cobranzas/{cobranza_id}")
def eliminar_cobranza(
    factura_id: int,
    cobranza_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Revierte un pago real (no de factoring; esos se revierten desde el panel de factoring)
    y recalcula el saldo/estado de la factura."""
    # Orden GLOBAL de locks: OC → factura → adelanto (el mismo de aprobar_adelanto y
    # eliminar_factura). Serializar por la OC primero deja la protección contra
    # deadlocks en el ORDENAMIENTO y no en invariantes de negocio.
    ref = db.query(ContFacturaCliente).filter(ContFacturaCliente.id == factura_id).first()
    if not ref:
        raise HTTPException(404, "Factura no encontrada")
    if ref.oc_cliente_id:
        db.query(OcCliente).filter(OcCliente.id == ref.oc_cliente_id).with_for_update().first()
    # Bloquea la factura (lock de fila) ANTES de borrar, igual que registrar_cobranza,
    # para que el recálculo de saldo no compita con un pago concurrente.
    factura = (
        db.query(ContFacturaCliente)
        .filter(ContFacturaCliente.id == factura_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if not factura:
        raise HTTPException(404, "Factura no encontrada")
    c = db.query(ContCobranza).filter(
        ContCobranza.id == cobranza_id, ContCobranza.factura_id == factura_id
    ).first()
    if not c:
        raise HTTPException(404, "Cobranza no encontrada")
    if _es_medio_factoring(c.medio):
        raise HTTPException(400, "Las cobranzas de factoring se revierten desde el panel de factoring")
    # Espejo del candado de registrar_cobranza: con la factura CEDIDA la asignación
    # de pagos está congelada (la retención del factor se calculó sobre ella y
    # Tesorería la publica en flujo-caja) — revertir aquí la desfasaría.
    if factura.factoring and factura.factoring.estado == "vigente":
        raise HTTPException(409, "La factura tiene un factoring vigente; liquide el factoring antes de revertir cobranzas")
    conciliada = (db.query(ConciliacionIngreso)
                  .filter(ConciliacionIngreso.cobranza_id == c.id).first())
    if conciliada:
        raise HTTPException(409, "La cobranza está conciliada con el banco; desconcíliela en Tesorería primero")
    # Reversión de una aplicación de adelanto: devolver el monto al adelanto de origen
    # (bajo lock) para que quede disponible para otra factura. Mantiene el INVARIANTE
    # monto_aplicado == Σ cobranzas medio='adelanto' del adelanto.
    if c.medio == MEDIO_ADELANTO and c.adelanto_id:
        # populate_existing: se resta sobre `monto_aplicado`, así que el valor tiene que
        # ser el fresco del FOR UPDATE y no el que dejó una lectura plana anterior.
        adel = (db.query(ContAdelanto)
                .filter(ContAdelanto.id == c.adelanto_id)
                .populate_existing().with_for_update().first())
        if adel:
            adel.monto_aplicado = round(max(_f(adel.monto_aplicado) - _f(c.monto), 0.0), 2)
    # Totales desde la lectura BLOQUEANTE menos la fila borrada: la relación perezosa
    # servía el snapshot viejo y persistía monto_pagado/saldo equivocados que NINGUNA
    # lectura posterior corregía (la pantalla mostraba "por cobrar" el bruto completo
    # con la cobranza listada al lado, y la cartera quedaba inflada).
    cobs_frescas = [x for x in _cobranzas_bloqueadas(db, factura.id) if x.id != c.id]
    db.delete(c)
    db.flush()
    db.refresh(factura, with_for_update=True)
    _recompute_factura(factura, cobranzas=cobs_frescas)
    db.commit()
    return {"ok": True}


class FactoringIn(BaseModel):
    """Cesión de la factura a un factor. monto_adelantado <= cupo (bruto - pagos reales);
    si retencion no viene, se deriva = cupo - adelanto."""
    empresa_factoring: Optional[str] = None
    id_operacion: Optional[str] = None
    fecha_operacion: Optional[str] = None
    monto_adelantado: float = Field(0, ge=0)
    costo_factoring: float = Field(0, ge=0)
    retencion: Optional[float] = Field(None, ge=0)
    banco: Optional[str] = None
    observaciones: Optional[str] = None


@router.post("/facturas/{factura_id}/factoring")
def set_factoring(
    factura_id: int,
    payload: FactoringIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Crea o actualiza el factoring (1 por factura). Valida adelanto <= cupo (bruto - pagos
    reales), deriva la retención si falta, y genera SOLO la cobranza de ADELANTO
    (medio 'factoring_adelanto'). Registra quién lo hizo. No editable si ya está liquidado."""
    # populate_existing: el cupo que se cede al factor se calcula con `monto_bruto` y el
    # guard mira `es_anticipo` — plata pura; sin él sería la copia del identity map.
    factura = (
        db.query(ContFacturaCliente)
        .filter(ContFacturaCliente.id == factura_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if not factura:
        raise HTTPException(404, "Factura no encontrada")
    if factura.es_anticipo:
        raise HTTPException(409, "No se puede hacer factoring de una factura de anticipo (respalda un adelanto ya recibido)")
    # Guard SII — MISMA plata que registrar_cobranza, por la puerta de al lado: el
    # factoring genera una cobranza real ('factoring_adelanto') con el dinero que
    # entrega el factor. Ceder una factura cuyo DTE está en vuelo o RECHAZADO metía
    # plata contra un documento que el SII nunca conoció y dejaba la factura
    # 'factorizada' → la aplicación automática de adelantos devuelve 0 y
    # eliminar_factura la rechaza por tener factoring: zombi permanente que secuestra
    # el cupo facturable. Apenas llega el folio, la cesión se acepta igual.
    # La `salida` nombra revertir_factoring porque ESTE endpoint es también el que usaba el
    # operador para deshacer una cesión (upsert a monto 0): con el guard puesto, esa
    # maniobra ya no pasa y el caso legado necesita saber por dónde salir.
    _exigir_sii_emitido(db, factura, "cederla al factoring",
                        salida="Si ya hay una cesión registrada contra este documento y el "
                               "documento nunca llegó a existir ante el SII, revierte la "
                               "cesión al factor (queda la traza con el motivo)")
    # Lecturas BLOQUEANTES: el cupo que se cede al factor se calcula sobre los pagos
    # REALES del cliente, y la relación perezosa sirve el snapshot viejo del request.
    fac = _factoring_bloqueado(db, factura.id)
    if fac and fac.estado == "liquidada":
        raise HTTPException(400, "El factoring ya fue liquidado; no se puede modificar")

    bruto = _f(factura.monto_bruto)
    cobs_frescas = _cobranzas_bloqueadas(db, factura.id)
    pagado_no_fact = sum(_f(c.monto) for c in cobs_frescas if not _es_medio_factoring(c.medio))
    if payload.monto_adelantado < 0:
        raise HTTPException(400, "El adelanto no puede ser negativo")
    cupo = bruto - pagado_no_fact
    if payload.monto_adelantado > cupo + TOL_PAGO:
        raise HTTPException(400, f"El adelanto excede el saldo financiable ({cupo:.0f})")
    retencion = payload.retencion
    if retencion is None:
        retencion = round(max(cupo - payload.monto_adelantado, 0), 0)

    if not fac:
        fac = ContFactoring(factura_id=factura.id)
        db.add(fac)
    fac.usuario_id = getattr(current_user, "id", None)
    fac.empresa_factoring = payload.empresa_factoring
    fac.id_operacion = payload.id_operacion
    # Fecha de CHILE por defecto (ver la nota de registrar_cobranza): la fecha de la
    # operación de factoring es un dato contable, no la hora UTC del server.
    fac.fecha_operacion = _parse_date(payload.fecha_operacion) or _hoy_chile()
    fac.monto_adelantado = payload.monto_adelantado
    fac.costo_factoring = payload.costo_factoring
    fac.retencion = retencion
    fac.banco = payload.banco
    fac.observaciones = payload.observaciones
    fac.estado = "vigente"
    fac.fecha_liquidacion = None

    # Reemplazar solo la cobranza de ADELANTO (nunca la de retención liquidada).
    # Si esa cobranza ya está conciliada con un abono del banco en Tesorería, se
    # rechaza: borrarla dejaría el movimiento bancario conciliado sin destino.
    for c in list(factura.cobranzas):
        if c.medio == MEDIO_FACT_ADELANTO:
            conciliada = (db.query(ConciliacionIngreso)
                          .filter(ConciliacionIngreso.cobranza_id == c.id).first())
            if conciliada:
                raise HTTPException(409, "El adelanto del factoring está conciliado con el banco; desconcílielo en Tesorería antes de modificar el factoring")
            db.delete(c)
    db.flush()
    if payload.monto_adelantado > 0:
        db.add(ContCobranza(
            factura_id=factura.id, fecha=fac.fecha_operacion, monto=payload.monto_adelantado,
            medio=MEDIO_FACT_ADELANTO, banco=payload.banco, numero_operacion=payload.id_operacion,
            observaciones=f"Adelanto factoring {payload.empresa_factoring or ''}".strip(),
            usuario_id=getattr(current_user, "id", None),
        ))
    db.flush()
    db.refresh(factura, with_for_update=True)
    # Totales desde la lectura BLOQUEANTE (recargada tras el flush para incluir lo agregado
    # en esta transacción): la relación perezosa serviría el snapshot viejo del request.
    _recompute_factura(factura, cobranzas=_cobranzas_bloqueadas(db, factura.id))
    db.commit()
    db.refresh(factura, with_for_update=True)
    return _serialize_factura(factura)


@router.post("/facturas/{factura_id}/factoring/liquidar")
def liquidar_factoring(
    factura_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Liquida el factoring vigente: libera el saldo pendiente REAL como retención (cobranza
    'factoring_retencion'), cerrando la factura en saldo 0, y marca estado 'liquidada' con
    quién y cuándo."""
    # populate_existing: se libera contra `monto_bruto` (plata) — el valor tiene que ser
    # el del FOR UPDATE, no el de una lectura plana previa del request.
    factura = (
        db.query(ContFacturaCliente)
        .filter(ContFacturaCliente.id == factura_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if not factura:
        raise HTTPException(404, "Factura no encontrada")
    # Lectura BLOQUEANTE del factoring (_factoring_bloqueado, el mismo helper que usa
    # set_factoring): con la relación perezosa `factura.factoring` el estado venía del
    # snapshot del inicio del request, y justo debajo se ESCRIBE `fac.retencion` y se
    # cierra la factura en 0 — una cesión/liquidación commiteada en paralelo era
    # invisible y se liquidaba dos veces sobre datos viejos.
    fac = _factoring_bloqueado(db, factura.id)
    if not fac or fac.estado != "vigente":
        raise HTTPException(400, "No hay factoring vigente para liquidar")
    # Guard SII — la liquidación es la SEGUNDA entrada de plata del factoring (libera la
    # retención como cobranza 'factoring_retencion' y cierra la factura en saldo 0). Con
    # el guard puesto en set_factoring este estado ya no se puede crear, pero sí existe
    # como LEGADO (cesiones registradas antes de este candado), y cerrar en 0 una factura
    # que el SII no conoce es justo el asiento que no debe quedar. Va DESPUÉS del chequeo
    # de factoring vigente a propósito: si no hay nada que liquidar, el operador tiene que
    # leer eso y no «espera el folio». Salida: «Reintentar» hasta que llegue el folio y,
    # si ese documento NUNCA va a existir, revertir la cesión (revertir_factoring) — sin
    # esa segunda salida este guard convertía el caso legado en un callejón sin salida.
    _exigir_sii_emitido(db, factura, "liquidar el factoring",
                        salida="Si ese documento nunca llegó a existir ante el SII, "
                               "revierte la cesión al factor (queda la traza con el motivo)")
    # Liberar el saldo pendiente REAL (no un valor fijo) para cerrar exacto en 0.
    # Lectura BLOQUEANTE: con la relación perezosa, un pago del cliente commiteado en
    # paralelo era invisible y el factor liberaba de más (o de menos) que lo pactado.
    cobs_frescas = _cobranzas_bloqueadas(db, factura.id)
    pagado_actual = sum(_f(c.monto) for c in cobs_frescas)
    liberar = round(max(_f(factura.monto_bruto) - pagado_actual, 0.0), 2)
    # La retención refleja SIEMPRE lo realmente liberado por el factor en esta liquidación
    fac.retencion = liberar
    # Fecha de CHILE (ver la nota de registrar_cobranza): la cobranza de la retención y la
    # fecha de liquidación son el MISMO hecho contable y tienen que llevar la misma fecha.
    hoy = _hoy_chile()
    if liberar > TOL:
        db.add(ContCobranza(
            factura_id=factura.id, fecha=hoy, monto=liberar,
            medio=MEDIO_FACT_RETENCION, banco=fac.banco, numero_operacion=fac.id_operacion,
            observaciones="Liquidación retención factoring", usuario_id=getattr(current_user, "id", None),
        ))
    fac.estado = "liquidada"
    fac.fecha_liquidacion = hoy
    fac.usuario_liquidacion_id = getattr(current_user, "id", None)
    db.flush()
    db.refresh(factura, with_for_update=True)
    # Totales desde la lectura BLOQUEANTE recargada (incluye la retención recién agregada)
    _recompute_factura(factura, cobranzas=_cobranzas_bloqueadas(db, factura.id))
    db.commit()
    db.refresh(factura, with_for_update=True)
    return _serialize_factura(factura)


class RevertirFactoringIn(BaseModel):
    """Reversión de una cesión al factor registrada contra un documento que el SII nunca
    llegó a conocer. `motivo` es OBLIGATORIO: queda en la factura y en el log del server
    (la operación de factoring desaparece, así que la traza es lo único que queda)."""
    motivo: str = Field(..., min_length=5, max_length=400)


@router.post("/facturas/{factura_id}/factoring/revertir")
def revertir_factoring(
    factura_id: int,
    payload: RevertirFactoringIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CONTRAPARTIDA del guard SII del factoring: la salida —única y auditada— del caso
    LEGADO en que una factura quedó cedida al factor contra un documento tributario que
    nunca existió.

    POR QUÉ EXISTE (el zombi imborrable). Antes de los guards SII se podía ceder al factor
    una factura sin folio. Con los guards puestos, esa fila legada quedó cerrada por los
    TRES lados a la vez: no se puede liquidar (guard SII de liquidar_factoring), no se
    puede EDITAR a 0 (guard SII de set_factoring, que es la única forma que tenía el
    módulo de deshacer una cesión), no se puede eliminar la factura (`if factura.factoring`
    responde 409) y la aplicación automática de adelantos devuelve 0. Resultado: plata del
    factor amarrada a un documento inexistente y el cupo facturable de esa mercadería
    secuestrado PARA SIEMPRE. Mejorar la puerta de entrada sin abrir una de salida es
    dejar plata atrapada; eso no es aceptable.

    LA PUERTA ES EXACTAMENTE LA INVERSA DEL GUARD: se revierte SÓLO donde el guard
    bloquea (`_plata_bloqueada_por_sii` == True: factura, sin folio, con DTE que no está
    emitido-con-folio). Si el documento SÍ existe ante el SII, la cesión es un hecho
    financiero real y no se borra por acá: se liquida cuando el factor paga la retención,
    o se corrige volviendo a registrar el factoring (set_factoring es un upsert). Una sola
    condición de apertura, derivada del mismo helper: no hay un segundo criterio que se
    pueda desalinear.

    POR QUÉ BORRA LA FILA y no la marca 'revertida': mientras exista una fila en
    cont_factoring, `eliminar_factura` sigue respondiendo 409 y la factura sigue siendo
    imborrable — o sea, el zombi seguiría vivo con otro nombre. Es además la convención de
    la casa para plata sin huella contable (eliminar_cobranza / eliminar_factura). El hecho
    NO se pierde: queda en `factura.observaciones` (visible en la ficha) y en el log del
    server, con motivo, montos, id de operación y usuario.

    QUÉ **NO** HACE: no toca las cobranzas del cliente (medio ≠ factoring; ésas se
    revierten una por una con su propio endpoint y sus propios candados) ni el DTE ni el
    registro tributario. Después de revertir, la factura queda borrable por el camino
    normal — que sigue exigiendo lo suyo: si el DTE puede existir ante el SII (emisión
    ambigua sin confirmar, o emitida), `eliminar_factura` sigue pidiendo intervención
    humana. La plata sale; el documento irreversible sigue necesitando un humano."""
    # Orden GLOBAL de locks: OC → factura (el mismo de eliminar_factura/eliminar_cobranza;
    # sin él, esta reversión y la aprobación de adelantos de Tesorería se cruzan).
    ref = db.query(ContFacturaCliente).filter(ContFacturaCliente.id == factura_id).first()
    if not ref:
        raise HTTPException(404, "Factura no encontrada")
    if ref.oc_cliente_id:
        db.query(OcCliente).filter(OcCliente.id == ref.oc_cliente_id).with_for_update().first()
    # populate_existing: se decide sobre `numero_factura`/`tipo_doc` (la apertura de la
    # puerta) y se reescriben totales — valores del FOR UPDATE, no del snapshot del request.
    factura = (db.query(ContFacturaCliente)
               .filter(ContFacturaCliente.id == factura_id)
               .populate_existing().with_for_update().first())
    if not factura:
        raise HTTPException(404, "Factura no encontrada")
    motivo = (payload.motivo or "").strip()
    if len(motivo) < 5:  # min_length de Pydantic no ve los espacios en blanco
        raise HTTPException(400, "Escribe el motivo de la reversión: queda registrado en "
                                 "la factura y es lo único que explica por qué desapareció "
                                 "la operación de factoring")
    fac = _factoring_bloqueado(db, factura.id)
    if not fac:
        raise HTTPException(404, "Esta factura no tiene una operación de factoring registrada")
    if not _plata_bloqueada_por_sii(db, factura):
        raise HTTPException(
            409, "Esta factura SÍ está registrada ante el SII: la cesión al factor es un "
                 "hecho financiero real y no se borra desde aquí. Liquida el factoring "
                 "cuando el factor pague la retención, o corrige la operación volviendo a "
                 "registrar el factoring con los montos correctos.")
    # Cobranzas que nacieron de la cesión (adelanto del factor y, si se liquidó antes del
    # guard, su retención). Lectura BLOQUEANTE: son plata.
    cobs = _cobranzas_bloqueadas(db, factura.id)
    del_cobs = [c for c in cobs if _es_medio_factoring(c.medio)]
    # Mismo candado que set_factoring: si el abono del factor ya está conciliado con la
    # cartola en Tesorería, borrar la cobranza dejaría el movimiento bancario sin destino
    # (y el ON DELETE CASCADE del enlace se llevaría la conciliación en silencio).
    if del_cobs:
        conciliadas = (db.query(ConciliacionIngreso)
                       .filter(ConciliacionIngreso.cobranza_id.in_([c.id for c in del_cobs]))
                       .all())
        if conciliadas:
            raise HTTPException(
                409, "El abono del factoring está conciliado con el banco: desconcílielo "
                     "en Tesorería antes de revertir la cesión")
    traza = {
        "factoring_id": fac.id,
        "empresa_factoring": fac.empresa_factoring,
        "id_operacion": fac.id_operacion,
        "estado": fac.estado,
        "monto_adelantado": _f(fac.monto_adelantado),
        "retencion": _f(fac.retencion),
        "motivo": motivo,
        "cobranzas_eliminadas": [{"id": c.id, "medio": c.medio, "monto": _f(c.monto)}
                                 for c in del_cobs],
        "usuario_id": getattr(current_user, "id", None),
    }
    # Traza EN EL PRODUCTO (no sólo en el log): la ficha de la factura muestra
    # `observaciones`. Fecha de CHILE, como el resto de los hechos contables del módulo.
    nota = (f"[{_hoy_chile().isoformat()}] Factoring REVERTIDO — "
            f"{fac.empresa_factoring or 'sin factor'}"
            f"{' · op ' + fac.id_operacion if fac.id_operacion else ''}"
            f" · adelanto {_f(fac.monto_adelantado):.0f}"
            f" · estado {fac.estado} · usuario {getattr(current_user, 'id', None)}"
            f" — motivo: {motivo}")
    previo = (factura.observaciones or "").rstrip()
    # [-60000:]: la columna es TEXT (65.535 bytes). Reversiones repetidas no pueden
    # reventar el UPDATE con un 500; si hay que cortar, se pierde lo MÁS VIEJO.
    factura.observaciones = ((previo + "\n" if previo else "") + nota)[-60000:]
    for c in del_cobs:
        db.delete(c)
    db.delete(fac)
    db.flush()
    # refresh bloqueante por FRESCURA de los montos (la nota de _cobranzas_bloqueadas),
    # igual que eliminar_cobranza. `_estado_pago` lee además `factura.factoring` para
    # decidir 'factorizada': acá esa relación NO está cargada (nadie la tocó en este
    # request; el factoring se leyó por query), así que el lazy load posterior al flush
    # del DELETE ya devuelve None. MEDIDO con un mutante: una línea `db.expire(...)`
    # extra no cambiaba nada, así que se sacó en vez de dejar código sin sonda.
    db.refresh(factura, with_for_update=True)
    _recompute_factura(factura, cobranzas=[c for c in cobs if not _es_medio_factoring(c.medio)])
    db.commit()
    logger.warning("Factoring REVERTIDO factura=%s %s", factura.id, traza)
    db.refresh(factura, with_for_update=True)
    return {**_serialize_factura(factura), "factoring_revertido": traza}


def _referencia_ancla_factura(factura_id: int) -> str:
    """La referencia interna con la que el DTE 33 de esta factura vive en Wasabil.

    Fuente ÚNICA: `wasabil_dte/router.py:_referencia_interna_factura`. Se importa (no se
    duplica el formato) para que un cambio de formato no deje esta nota citando una
    referencia que ya no existe; el literal es sólo el plan B si el módulo DTE no está
    instalado, caso en que tampoco habría anclas que conservar."""
    try:
        from wasabil_dte.router import _referencia_interna_factura
        return _referencia_interna_factura(factura_id)
    except Exception:                                        # pragma: no cover
        return f"FACT-{factura_id}"


def _conservar_ancla_dte(db: Session, dte, factura_id: int, usuario_id) -> None:
    """DESLIGA el ancla de la factura que se está borrando, en vez de DESTRUIRLA.

    EL PORQUÉ (hallazgo ALTO-3 de la re-refutación): `uuid` es el identificador que
    Wasabil asigna AL CREAR el documento, así que `uuid IS NOT NULL` significa que el
    documento EXISTE allá — el `status 4` local sólo dice que la última vez que
    preguntamos el SII lo había rechazado, y esa foto puede quedar obsoleta (es la misma
    premisa que adopta el cinturón anti doble emisión del reintento: el estado remoto
    puede contradecir al local). Si además se borra la fila, desaparece la ÚNICA llave
    hacia ese documento real: la factura nueva por la misma mercadería nacerá con otro id
    y por lo tanto con otra referencia (FACT-<id nuevo>), así que ni el rescate ni el
    cinturón podrán encontrar el viejo, y la única defensa que quedaría es el tope de
    cantidad facturable.

    Conservarla no cuesta nada: la fila huérfana (factura_id NULL, que en MySQL no
    colisiona con el único `uq_wasabil_dte_factura`) no la lista ninguna pantalla —todas
    las consultas del módulo son por factura_id o despacho_id— y satisface la FK RESTRICT
    que impide borrar la factura con el ancla apuntándola.

    Lo que queda escrito en `error` no es decoración: es lo que un humano necesita para
    cerrar el caso desde Wasabil (uuid + referencia + factura borrada + quién y cuándo).
    Se PREPONE al error previo, que se conserva."""
    ref = _referencia_ancla_factura(factura_id)
    nota = (f"[{_hoy_chile().isoformat()}] ANCLA CONSERVADA — la factura local "
            f"#{factura_id} (referencia {ref}) fue ELIMINADA en PartsControl"
            f"{' por el usuario ' + str(usuario_id) if usuario_id else ''}. El documento "
            f"uuid={dte.uuid} EXISTE en Wasabil; último estado conocido aquí: rechazado "
            f"por el SII (status {dte.status_id})"
            f"{', folio ' + str(dte.folio) if dte.folio else ', sin folio'}. Si en Wasabil "
            f"figura EMITIDO, esta fila es su ÚNICO rastro: no emitas otra vez la misma "
            f"mercadería sin revisarlo (búscalo por ese uuid o por la referencia).")
    previo = (dte.error or "").rstrip()
    # [:60000]: la columna es TEXT (65.535 bytes). Si hay que cortar, se pierde lo MÁS
    # VIEJO — la nota que dice dónde está el documento va primero.
    dte.error = (nota + ("\n" + previo if previo else ""))[:60000]
    dte.factura_id = None
    # flush explícito: el UPDATE del ancla tiene que llegar ANTES del DELETE de la
    # factura (FK RESTRICT `wasabil_dte.factura_id` → 1451). El orden natural de la
    # unidad de trabajo ya lo hace, pero de eso no depende un candado anti doble emisión.
    db.flush()
    logger.warning("Ancla DTE CONSERVADA (factura %s eliminada): uuid=%s status=%s ref=%s",
                   factura_id, dte.uuid, dte.status_id, ref)


def _resolver_ancla_dte_al_eliminar(db: Session, factura_id: int, usuario_id=None) -> None:
    """Guard SII del BORRADO de una factura Y destino de su ANCLA anti doble emisión.

    LA REGLA, en una línea: el ancla se BORRA sólo cuando consta que el documento NO
    existe en Wasabil; si el documento EXISTE (hay uuid) se CONSERVA; y si no se puede
    CONCLUIR qué hay allá, el borrado se BLOQUEA (fail-closed).

    La tabla completa de estados (uuid × status × claim/en_vuelo), que es el contrato:

      | uuid | estado local        | en vuelo | ¿se concluye?          | acción            |
      |------|---------------------|----------|------------------------|-------------------|
      |  —   | (cualquiera ≠ 2/6)  |   no     | NUNCA nació documento  | borra el ancla    |
      |  —   | procesando/pendiente|   no     | NO (dice vivo sin id)  | 409               |
      |  —   | cualquiera          |   sí     | NO (ambiguo)           | 409               |
      | sí   | emitido (3)         |    ·     | existe CON folio       | 409 (nota crédito)|
      | sí   | procesando/pendiente|    ·     | existe y VIVO          | 409               |
      | sí   | cualquiera          |   sí     | NO (claim/ambiguo)     | 409               |
      | sí   | rechazado (4)       |   no     | existe, último: rechazo| CONSERVA el ancla |
      | sí   | desconocido (None…) |   no     | NO                     | 409               |

    Por qué el rechazado (4) NO bloquea el borrado: un rechazo no tiene folio que
    perder, y bloquearlo dejaría la factura imborrable PARA SIEMPRE secuestrando el cupo
    facturable de esa mercadería (el único remedio ofrecido, «Reintentar», reenvía el
    MISMO payload: si la causa del rechazo está en la factura, el SII la rechaza otra
    vez). Se borra la factura, sí — pero NO el ancla: ver `_conservar_ancla_dte`.

    Por qué el estado DESCONOCIDO con uuid sí bloquea: hay documento en Wasabil y no
    sabemos en qué quedó. Un guard que protege un documento IRREVERSIBLE y no puede
    concluir debe fallar CERRADO y decirle al humano qué revisar; la salida es consultar
    el estado / «Reintentar», que pregunta por ese uuid y deja la fila concluyente."""
    from wasabil_dte.models import (
        WasabilDte, STATUS_EMITIDO as _ST_EMITIDO, STATUS_PROCESANDO as _ST_PROCESANDO,
        STATUS_PENDIENTE as _ST_PENDIENTE, STATUS_FALLIDO as _ST_FALLIDO,
    )
    from wasabil_dte.service import claim_vigente as _claim_vigente
    # populate_existing + FOR UPDATE: datos FRESCOS bajo el lock (sin esto se decidiría
    # con la versión del identity map, ignorando el claim que otro request acaba de
    # commitear).
    dte = (db.query(WasabilDte).filter(WasabilDte.factura_id == factura_id)
           .populate_existing().with_for_update().first())
    if dte is None:
        return
    if dte.status_id == _ST_EMITIDO:
        raise HTTPException(
            409, f"Esta factura tiene DTE emitido al SII (folio {dte.folio}): "
                 "anúlala primero en Wasabil (nota de crédito) y luego elimínala aquí")
    if _claim_vigente(dte) or dte.status_id in (_ST_PROCESANDO, _ST_PENDIENTE):
        raise HTTPException(
            409, "Esta factura tiene una emisión SII en curso: espera el resultado "
                 "(Emitida o Fallida) antes de eliminar")
    if dte.en_vuelo_desde is not None:
        raise HTTPException(
            409, "No hay confirmación de Wasabil sobre esta emisión (se cortó la "
                 "comunicación): la factura PUEDE existir ya ante el SII. Usa "
                 "«Reintentar» para que el sistema lo verifique; si confirma que no se "
                 "emitió, podrás eliminarla.")
    if dte.uuid is None:
        # Consta que el documento NUNCA nació (jamás hubo respuesta con documento) y el
        # fallo fue CONFIRMADO (sin claim en vuelo): no hay llave que perder, y el ancla
        # se limpia con la factura para que la mercadería vuelva a ser facturable.
        db.delete(dte)
        return
    if dte.status_id != _ST_FALLIDO:
        raise HTTPException(
            409, "Hay un documento en Wasabil para esta factura (identificador interno "
                 f"{dte.uuid}) y aquí no consta en qué quedó ante el SII: consulta su "
                 "estado con «Reintentar» antes de eliminarla. Si Wasabil lo muestra "
                 "EMITIDO, no se elimina: se anula allá con una nota de crédito.")
    _conservar_ancla_dte(db, dte, factura_id, usuario_id)


@router.delete("/facturas/{factura_id}")
def eliminar_factura(
    factura_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Borrado SEGURO de una factura: se rechaza (409) si tiene factoring (vigente o liquidado)
    o cobranzas reales — primero hay que revertir esos pagos. El cascade borra las líneas,
    nunca pagos reales ni operaciones de factoring."""
    # Orden GLOBAL de locks: OC → adelanto/factura. Serializar por la OC primero
    # evita el deadlock con la aprobación de adelantos en Tesorería (que también
    # parte por la OC y luego toca adelantos y facturas de la misma venta).
    ref = db.query(ContFacturaCliente).filter(ContFacturaCliente.id == factura_id).first()
    if not ref:
        raise HTTPException(404, "Factura no encontrada")
    if ref.oc_cliente_id:
        db.query(OcCliente).filter(OcCliente.id == ref.oc_cliente_id).with_for_update().first()
    # Lock de fila: sin él, una cobranza registrada entre el chequeo y el DELETE se
    # borraría en cascada (registrar_cobranza también bloquea la factura).
    # populate_existing: datos frescos bajo el lock, no los de la lectura previa.
    factura = (db.query(ContFacturaCliente)
               .filter(ContFacturaCliente.id == factura_id)
               .populate_existing().with_for_update().first())
    if not factura:
        raise HTTPException(404, "Factura no encontrada")
    # Borrado seguro: no destruir pagos reales ni operaciones de factoring (vigentes o
    # liquidadas). El 409 NOMBRA las dos salidas —liquidar, o revertir la cesión cuando el
    # documento nunca existió ante el SII (revertir_factoring)—: antes era un callejón que
    # no decía qué hacer, y en el caso legado NINGUNA de las dos existía.
    if factura.factoring:
        raise HTTPException(409, "La factura tiene una operación de factoring; no se puede "
                                 "eliminar. Liquida el factoring, o —si su documento nunca "
                                 "llegó a existir ante el SII— revierte la cesión al factor "
                                 "(queda la traza con el motivo) y vuelve a eliminarla")
    if any(not _es_medio_factoring(c.medio) for c in factura.cobranzas):
        raise HTTPException(409, "Revierta las cobranzas antes de eliminar la factura")
    # Vía B: una factura de anticipo YA DESCONTADA en facturas del despacho real no se
    # borra — dejaría descuentos colgando citando un folio inexistente (y la FK de la línea
    # de descuento lo impediría igual, pero con un IntegrityError 500 en vez de este 409).
    # Va ANTES del guard SII a propósito (paridad con monza_contabilidad/router.py): ese
    # guard TOCA el ancla —la borra, o la desliga con un flush— y rechazar después obliga
    # a confiar en el rollback implícito del cierre de la sesión para deshacerlo. Confiar
    # en un efecto secundario para no perder el ancla anti doble emisión es inaceptable:
    # sin ese ancla, la misma mercadería puede emitirse DOS veces al SII.
    if factura.es_anticipo:
        descontada = (db.query(ContFacturaClienteItem)
                      .filter(ContFacturaClienteItem.anticipo_factura_id == factura.id)
                      .first())
        if descontada:
            raise HTTPException(409, "La factura de anticipo ya fue descontada en otra factura; elimine primero esa factura")
    # Guard SII (Fase B): con factura ELECTRÓNICA emitida o en emisión, el registro
    # local no se borra — el DTE existe ante el SII (anular allá primero). El ancla
    # anti doble emisión se borra SÓLO cuando consta que el documento nunca nació; si
    # hay uuid (documento existente) se CONSERVA huérfana. La tabla completa de estados,
    # con el porqué de cada rama, está en `_resolver_ancla_dte_al_eliminar`.
    _resolver_ancla_dte_al_eliminar(db, factura.id, getattr(current_user, "id", None))
    if factura.es_anticipo:
        # (El 409 de "anticipo ya descontado" se evaluó ARRIBA, antes del guard SII.)
        # Los adelantos que respaldaba vuelven a la vía A (sin factura de anticipo).
        # Sin aplicaciones que revertir: si las hubiera, el chequeo de cobranzas de
        # arriba ya bloqueó el borrado.
        db.query(ContAdelanto).filter(
            ContAdelanto.factura_anticipo_id == factura.id
        ).update({"factura_anticipo_id": None})
    db.delete(factura)  # cascade elimina líneas (sin pagos reales ni factoring)
    db.commit()
    return {"ok": True}


@router.get("/kpis")
def get_kpis(
    periodo: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Indicadores de cobranza (filtrables por `periodo`: semana|mes|anio sobre fecha_emisión).
    Glosario:
      facturado_clp          : total bruto facturado.
      cobrado_clp            : total ingresado (incluye adelanto/retención de factoring).
      cobrado_cliente_clp    : SOLO pagos reales del cliente (sin factoring).
      anticipo_factoring_clp : dinero del factor (adelanto + retención liquidada).
      por_cobrar_clp         : saldo pendiente.
      vencido_clp            : saldo vencido (excluye facturas con factoring vigente).
      en_factoring_clp       : bruto de facturas con factoring vigente."""
    empresa = getattr(current_user, "empresa", None) or "mineria"
    facturas = [
        f for f in db.query(ContFacturaCliente)
        .options(selectinload(ContFacturaCliente.cobranzas), selectinload(ContFacturaCliente.factoring))
        .filter(ContFacturaCliente.empresa == empresa)
        .all()
        if _periodo_filter(f.fecha_emision or f.created_at, periodo)
    ]
    hoy = date.today()
    facturado = sum(_f(f.monto_bruto) for f in facturas)
    cobrado = sum(_f(f.monto_pagado) for f in facturas)
    cobrado_cliente = sum(_f(c.monto) for f in facturas for c in f.cobranzas
                          if not _es_medio_factoring(c.medio))
    por_cobrar = sum(_f(f.saldo) for f in facturas if _f(f.saldo) > TOL)
    # Vencido: excluye facturas con factoring vigente (el riesgo de cobro lo tomó el factor)
    vencido = sum(_f(f.saldo) for f in facturas
                  if _f(f.saldo) > TOL and f.fecha_vencimiento and f.fecha_vencimiento < hoy
                  and not (f.factoring and f.factoring.estado == "vigente"))
    en_factoring = sum(_f(f.monto_bruto) for f in facturas
                       if f.factoring and f.factoring.estado == "vigente")
    anticipo_factoring = sum(_f(c.monto) for f in facturas for c in f.cobranzas
                             if _es_medio_factoring(c.medio))
    return {
        "n_facturas": len(facturas),
        "facturado_clp": round(facturado, 0),
        "cobrado_clp": round(cobrado, 0),
        "cobrado_cliente_clp": round(cobrado_cliente, 0),
        "anticipo_factoring_clp": round(anticipo_factoring, 0),
        "por_cobrar_clp": round(por_cobrar, 0),
        "vencido_clp": round(vencido, 0),
        "en_factoring_clp": round(en_factoring, 0),
    }
