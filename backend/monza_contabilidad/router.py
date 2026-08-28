"""API del módulo Contabilidad MonzaParts — Ventas + Facturas/Cobranzas/Factoring.

Prefijo: /api/monza/contabilidad (se monta sin prefix; el router ya lo trae, como el
resto de routers Monza). SOLO MonzaParts: candado require_empresa("automotriz").

Es el lado de CUENTAS POR COBRAR de MonzaParts. Espejo de routers/contabilidad.py de
Grupo AM, pero:
  - La VENTA es una MonzaCotizacion (estado 'vendida'/'despachado'); cliente y montos
    ya viven en la cotización y sus ítems (precio neto ya calculado → sin pricing_service).
  - Se factura una guía de despacho 'despachado' Y con la guía FIRMADA por el cliente
    (regla 2026-08-06, paridad MachParts): la marca la pone Despachos
    (monza_router_despachos.py: POST /entidades/{id}/firmar, foto/PDF + fecha) y acá
    _construir_factura la EXIGE. Doble tope por ÍTEM y por GUÍA contra lo ya facturado.
    El RETIRO EN OFICINA (sin_guia) solo factura mercadería que NO esté comprometida
    en despachos; la única factura que no nace de guía es la de ANTICIPO.

Endpoints (todos requieren autenticación + empresa automotriz):
  GET    /ventas                              listado de ventas (cotizaciones) + resumen cobranza
  GET    /ventas/{cot_id}                     detalle de una venta (ítems, guías, facturas)
  GET    /ventas/{cot_id}/despachos-facturables  guías despachadas aún facturables (las sin
                                              firmar viajan con guia_firmada=false y el
                                              selector las deshabilita con el motivo)
  POST   /ventas/{cot_id}/adelanto/verificar  Contabilidad verifica el adelanto informado
  POST   /adelantos/{id}/anular               anula el adelanto (deja la traza 'anulado')
  DELETE /adelantos/{id}                      ELIMINA el registro del adelanto (reversión
                                              completa: es la que destraba los 409 de
                                              Ventas, que cuentan filas — ver el endpoint)

Flujo de ADELANTO (ej. 50% personas naturales):
  Comercial cierra la venta indicando pct_adelanto (campo en MonzaCotizacion) → la venta
  queda "por_verificar" → Contabilidad verifica (POST .../adelanto/verificar: guarda
  MonzaContAdelanto con monto/fecha/banco y marca adelanto_verificado=1) → al EMITIR la
  factura, _aplicar_adelanto registra el adelanto como cobranza 'adelanto' (descuenta saldo);
  monto_aplicado evita aplicarlo dos veces y se devuelve si se revierte esa cobranza.
  Trazabilidad: la cobranza medio='adelanto' liga el dinero a la factura.
  GET    /facturas                            listado de facturas + antigüedad de cartera
  POST   /facturas/preview                    PREVISUALIZA la factura (no persiste nada)
  POST   /facturas                            EMITIR una factura (desde una guía o ítems)

Factura de ANTICIPO (Fase 7, vía B) — `es_anticipo=true` en POST /facturas:
  El cliente paga ANTES de que llegue la mercadería y se le emite una factura por ese
  anticipo, la ÚNICA que NO nace de una guía. Cuando después se factura el despacho real,
  el sistema le cuelga sola una línea NEGATIVA "DESCUENTO" que referencia el folio del
  anticipo → Σ brutos de las facturas de la venta == total de la venta y el cliente no
  paga dos veces. El pendiente por descontar NO se guarda: se DERIVA de esas líneas
  (_anticipos_pendientes_de_descuento), así que borrar la factura final devuelve el cupo
  sin código de reversión. A diferencia de Grupo AM no hay columna que ligue el adelanto
  con su factura de anticipo: Monza tiene UN adelanto por venta y el vínculo se deriva de
  las facturas es_anticipo=1 (ver la nota de models.py).
  Reglas duras de la vía B (todas con su porqué en el código):
    · UNA factura de anticipo por venta — un segundo anticipo se rechaza con 409 salvo
      `confirmar_segundo_anticipo` explícito (emitir un DTE 33 es IRREVERSIBLE).
    · La factura de anticipo NO admite cobranzas manuales: se salda con el adelanto que
      aprueba Tesorería (si no, el mismo depósito se contaría dos veces).
    · La mercadería de una venta con anticipo se FACTURA, nunca se boletea (una boleta no
      puede referenciar el folio de la factura de anticipo).
    · Si el adelanto ya había caído en otra factura de la venta, al nacer el anticipo la
      plata se RE-RUTEA hacia él (ver _reencauzar_adelanto_al_anticipo).
  DELETE /facturas/{id}                       borrado seguro (no si hay pagos/factoring)
  POST   /facturas/{id}/cobranzas             registrar un pago del cliente
  DELETE /facturas/{id}/cobranzas/{id}        revertir un pago
  POST   /facturas/{id}/factoring             ceder la factura a un factor
  POST   /facturas/{id}/factoring/liquidar    liquidar el factoring
  GET    /kpis                                indicadores de cobranza
"""
import logging
from datetime import date, timedelta
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, func
from sqlalchemy.orm import Session, selectinload, joinedload, contains_eager
from sqlalchemy.exc import IntegrityError

from database import get_db
from auth import get_current_user
from empresa_guard import require_empresa
from models.models import User
from monza_models import (
    MonzaCotizacion, MonzaCotizacionItem, MonzaDespacho, MonzaDespachoItem, MonzaConfig, MonzaCliente,
)
from .models import (
    MonzaContFacturaCliente, MonzaContFacturaClienteItem, MonzaContCobranza, MonzaContFactoring,
    MonzaContAdelanto,
)
from .schemas import (FacturaItemIn, FacturaCreate, CobranzaIn, FactoringIn,
                      RevertirFactoringIn, AdelantoVerificarIn)
from .service import (
    TOL, TOL_QTY, TOL_PAGO, MEDIO_FACT_ADELANTO, MEDIO_FACT_RETENCION, MEDIO_ADELANTO,
    ADEL_ANULADO, ADEL_APROBADO,
    _f, _parse_date, _es_medio_factoring, iva_rate_de,
    _precio2, _total_linea, _iva_clp, _hoy_chile,
    _semaforo, _recompute_factura, _serialize_factura, _resumen_cobranza, _periodo_filter,
    periodo_floor, estado_adelanto, estado_de_adelanto, reactivar_adelanto,
    rut_valido, rut_saneado, mercaderia_pendiente_bruto,
)
# Solo lectura de los enlaces de conciliación de Tesorería (los MODELS de ambos módulos
# solo importan database, así que no hay ciclo):
#   · MonzaTesConciliacionIngreso (abono ↔ cobranza): una cobranza conciliada con el
#     banco no se puede borrar sin desconciliarla primero allá.
#   · MonzaTesConciliacion (abono ↔ adelanto): un adelanto conciliado no se puede
#     re-verificar (editar monto) sin desconciliar el abono primero.
from monza_tesoreria.models import MonzaTesConciliacion, MonzaTesConciliacionIngreso

logger = logging.getLogger("monza_contabilidad")

# Estados de cotización que cuentan como "venta" facturable.
ESTADOS_VENTA = ("vendida", "despachado")

# Eager loading de las relaciones hijas de la factura (evita N+1 al serializar).
_FACTURA_EAGER = (
    selectinload(MonzaContFacturaCliente.items),
    selectinload(MonzaContFacturaCliente.cobranzas),
    selectinload(MonzaContFacturaCliente.factoring),
)

router = APIRouter(
    prefix="/api/monza/contabilidad",
    tags=["monza-contabilidad"],
    dependencies=[Depends(require_empresa("automotriz"))],
)


# ── Helpers de BD ──────────────────────────────────────────────────────────────
def _config(db: Session) -> Optional[MonzaConfig]:
    return db.query(MonzaConfig).order_by(MonzaConfig.id.asc()).first()


def _despacho_items_de_cot(db: Session, cot_id: int):
    """(MonzaDespachoItem, MonzaDespacho) de los despachos CERRADOS ('despachado') de la
    cotización. OJO: cerrado ya NO es sinónimo de facturable — desde 2026-08-06 la
    emisión exige además la guía FIRMADA (paridad MachParts; el gate vive en
    _construir_factura). Este helper sigue devolviendo TODOS los cerrados porque los
    serializadores y el desglose por-facturar (base física) los necesitan completos."""
    return (
        db.query(MonzaDespachoItem, MonzaDespacho)
        .filter(
            MonzaDespacho.id == MonzaDespachoItem.despacho_id,
            MonzaDespacho.cotizacion_id == cot_id,
            MonzaDespacho.estado == "despachado",
        )
        .all()
    )


def _qty_despachada_por_item(db: Session, cot_id: int) -> dict:
    out = {}
    for di, _d in _despacho_items_de_cot(db, cot_id):
        out[di.item_id] = out.get(di.item_id, 0.0) + _f(di.qty_despachada)
    return out


def _qty_comprometida_en_despachos_por_item(db: Session, cot_id: int) -> dict:
    """Qty por ítem COMPROMETIDA en despachos VIVOS (en_preparacion + despachado).

    Alimenta el tope del RETIRO EN OFICINA (sin_guia): esa vía solo puede facturar
    mercadería que el cliente pasa a buscar, es decir, la que NO está asociada a
    ninguna guía de despacho. Cuenta también los BORRADORES a propósito: un borrador
    es mercadería ya comprometida a salir con guía — facturarla por caja mientras
    tanto es el mismo bypass del candado de la firma que esta regla cierra (y si el
    borrador se anula, el cupo vuelve solo). Los anulados no cuentan."""
    rows = (
        db.query(MonzaDespachoItem.item_id, MonzaDespachoItem.qty_despachada)
        .join(MonzaDespacho, MonzaDespacho.id == MonzaDespachoItem.despacho_id)
        .filter(
            MonzaDespacho.cotizacion_id == cot_id,
            MonzaDespacho.estado.in_(("en_preparacion", "despachado")),
        )
        .all()
    )
    out = {}
    for iid, qty in rows:
        if iid is not None:
            out[iid] = out.get(iid, 0.0) + _f(qty)
    return out


def _qty_facturada_por_item(db: Session, cot_id: int) -> dict:
    rows = (
        db.query(MonzaContFacturaClienteItem.item_cotizacion_id, MonzaContFacturaClienteItem.cantidad)
        .join(MonzaContFacturaCliente, MonzaContFacturaCliente.id == MonzaContFacturaClienteItem.factura_id)
        .filter(MonzaContFacturaCliente.cotizacion_id == cot_id)
        .all()
    )
    out = {}
    for iid, qty in rows:
        if iid is not None:
            out[iid] = out.get(iid, 0.0) + _f(qty)
    return out


def _qty_facturada_por_despacho_item(db: Session, cot_id: int) -> dict:
    rows = (
        db.query(MonzaContFacturaClienteItem.despacho_item_id, MonzaContFacturaClienteItem.cantidad)
        .join(MonzaContFacturaCliente, MonzaContFacturaCliente.id == MonzaContFacturaClienteItem.factura_id)
        .filter(
            MonzaContFacturaCliente.cotizacion_id == cot_id,
            MonzaContFacturaClienteItem.despacho_item_id.isnot(None),
        )
        .all()
    )
    out = {}
    for did, qty in rows:
        out[did] = out.get(did, 0.0) + _f(qty)
    return out


def _qty_facturada_retiro_por_item(db: Session, cot_id: int) -> dict:
    """Qty por ítem facturada por el canal RETIRO EN OFICINA (facturas sin_guia=1).

    Es la mitad que faltaba del neteo entre canales (hallazgo HIGH del multienjambre
    2026-08-07): sin el canal persistido, el tope de la guía restaba TODO lo facturado
    (retiro incluido) mientras el retiro ya había reservado esa mercadería vía
    pendiente_guias — doble descuento, y unidades ENTREGADAS quedaban infacturables
    para siempre. Las facturas históricas (pre-columna) quedan en sin_guia=0 ("canal
    guía"): atribución conservadora — puede achicar el cupo de la guía a favor del
    retiro, y el techo global vendido−facturado garantiza que NUNCA se sobre-factura.

    Matiz honesto sobre el legado (ronda 2 de la auditoría): con facturas antiguas de
    RETIRO atribuidas al canal guía, una guía NUEVA y firmada puede quedar
    transitoriamente sin cupo por los dos canales. No es una trampa permanente —
    marcar como firmada también la guía antigua sube el firmado del ítem y libera el
    cupo—, y el 409 del modo despacho nombra exactamente esa salida en vez del
    genérico "ya fue facturado por completo"."""
    rows = (
        db.query(MonzaContFacturaClienteItem.item_cotizacion_id, MonzaContFacturaClienteItem.cantidad)
        .join(MonzaContFacturaCliente, MonzaContFacturaCliente.id == MonzaContFacturaClienteItem.factura_id)
        .filter(
            MonzaContFacturaCliente.cotizacion_id == cot_id,
            MonzaContFacturaCliente.sin_guia == 1,
        )
        .all()
    )
    out = {}
    for iid, qty in rows:
        if iid is not None:
            out[iid] = out.get(iid, 0.0) + _f(qty)
    return out


def _facturas_de_cot(db: Session, cot_id: int) -> List[MonzaContFacturaCliente]:
    return (
        db.query(MonzaContFacturaCliente)
        .options(*_FACTURA_EAGER)
        .filter(MonzaContFacturaCliente.cotizacion_id == cot_id)
        .order_by(MonzaContFacturaCliente.id.asc())
        .all()
    )


def _anticipos_pendientes_de_descuento(db: Session, cot_id: int) -> List[tuple]:
    """Facturas de ANTICIPO (vía B) de la venta con neto AÚN NO descontado en facturas
    del despacho real. Devuelve [(factura_anticipo, pendiente_neto)] en orden de emisión
    (FIFO por id). Espejo de routers/contabilidad.py._anticipos_pendientes_de_descuento.

    El pendiente NO se guarda en ninguna columna: se DERIVA de las líneas vivas —
        pendiente = monto_neto del anticipo − Σ(−total_neto) de las líneas que lo
        referencian con anticipo_factura_id.
    Por eso borrar la factura final devuelve el cupo SOLA (cascade de sus líneas), sin
    código de reversión que pueda desincronizarse con la realidad.

    Incluye A PROPÓSITO los anticipos SIN folio SII (emisión electrónica en vuelo o
    rechazada): todavía no se pueden descontar, pero tampoco se pueden ignorar —
    facturar el despacho completo mientras existe un anticipo pendiente le cobraría dos
    veces al cliente. Quien construye la factura debe BLOQUEAR (ver _construir_factura).

    LECTURAS DE PLATA: no toma locks propios y no le hacen falta. El punto de
    serialización de la venta es el lock de la COTIZACIÓN, que ya sostienen los dos
    únicos caminos capaces de mover estos datos (crear_factura y eliminar_factura, más
    la emisión electrónica que reusa crear); dos requests no pueden descontar el mismo
    pendiente. Ver docs/regla-lecturas-de-plata.md."""
    anticipos = (
        db.query(MonzaContFacturaCliente)
        .filter(MonzaContFacturaCliente.cotizacion_id == cot_id,
                MonzaContFacturaCliente.es_anticipo == 1)
        .order_by(MonzaContFacturaCliente.id.asc())
        .all()
    )
    if not anticipos:
        return []
    # UNA sola query para las líneas de descuento de TODOS los anticipos (jamás una por
    # anticipo dentro del loop: este helper corre también en el detalle de la venta).
    rows = (
        db.query(MonzaContFacturaClienteItem.anticipo_factura_id,
                 MonzaContFacturaClienteItem.total_neto)
        .filter(MonzaContFacturaClienteItem.anticipo_factura_id.in_([a.id for a in anticipos]))
        .all()
    )
    descontado: dict = {}
    for aid, tot in rows:
        descontado[aid] = descontado.get(aid, 0.0) + (-_f(tot))  # las líneas son NEGATIVAS
    out = []
    for a in anticipos:
        pend = round(_f(a.monto_neto) - descontado.get(a.id, 0.0), 2)
        if pend > TOL:
            out.append((a, pend))
    return out


def _anticipo_por_descontar_bruto(facturas, desc_por_anticipo: dict) -> float:
    """Anticipo aún NO descontado, en BRUTO, a partir de facturas ya cargadas.

    Gemela de _anticipos_pendientes_de_descuento para los AGREGADOS (detalle y listado),
    donde las facturas de la venta ya están en memoria y abrir otra query sería un N+1.
    El pendiente vive en NETO y la mercadería pendiente en BRUTO, así que se convierte
    con el factor bruto/neto DE CADA factura de anticipo (no con la tasa global: cubre
    una factura exenta con factor 1.0)."""
    total = 0.0
    for f in facturas:
        if not getattr(f, "es_anticipo", 0):
            continue
        neto_fa = _f(f.monto_neto)
        pend = neto_fa - desc_por_anticipo.get(f.id, 0.0)
        if pend > TOL:
            factor = (_f(f.monto_bruto) / neto_fa) if neto_fa > TOL else 1.0
            total += pend * factor
    return total


def _guias_vivas(db: Session, facturas) -> dict:
    """{factura_id: N° de guía ACTUAL de su despacho} en UNA sola query.

    AUDITORÍA (hallazgo MEDIUM «factura manual contra una guía 52 cuyo folio aún viene en
    camino»): `MonzaContFacturaCliente.numero_guia` se congela al emitir la factura. Si el
    despacho tenía el N° tecleado a mano y después el SII confirmó el folio real de la
    guía 52, el módulo DTE pisa `despacho.numero_guia` y la factura seguía mostrando la
    guía vieja PARA SIEMPRE. Se sirve el valor vivo (con fallback al snapshot cuando el
    despacho ya no existe, que es lo que hace GA vía relationship ORM).

    En Monza `despacho_id` es un snapshot SIN FK (models.py), así que no hay relationship
    que precargar: se resuelve con este batch lookup — una query por request, nunca N+1."""
    ids = {f.despacho_id for f in facturas if getattr(f, "despacho_id", None)}
    if not ids:
        return {}
    guias = dict(
        db.query(MonzaDespacho.id, MonzaDespacho.numero_guia)
        .filter(MonzaDespacho.id.in_(ids)).all()
    )
    return {f.id: guias[f.despacho_id] for f in facturas
            if getattr(f, "despacho_id", None) in guias}


def _guia_viva_de(db: Session, factura) -> Optional[str]:
    """Azúcar de _guias_vivas para los endpoints que serializan UNA factura."""
    return _guias_vivas(db, [factura]).get(factura.id)


def _guia_sii_en_proceso(db: Session, despacho_id: int) -> bool:
    """¿La guía 52 de este despacho tiene una emisión al SII en curso (folio en camino)?

    Solo INFORMATIVO (lo consume el selector de guías del modal «Emitir factura»): en esa
    ventana `despacho.numero_guia` todavía es el N° manual viejo y el folio real lo va a
    pisar. Reusa el criterio único del módulo DTE en vez de reimplementarlo.

    Import LOCAL (mismo patrón que _dte_factura_no_emitido) para no crear el ciclo
    contabilidad ↔ wasabil. Cualquier falla (módulo ausente, esquema sin migrar) devuelve
    False y se registra: es un AVISO en un endpoint de solo lectura — jamás debe tumbar
    el selector ni convertirse en un 500."""
    try:
        from monza_wasabil_dte.router import _guia_electronica_en_proceso
        return bool(_guia_electronica_en_proceso(db, despacho_id))
    except Exception as e:  # noqa: BLE001 — aviso best-effort, ver docstring
        logger.warning("No se pudo consultar el estado SII de la guía del despacho %s: %s",
                       despacho_id, e)
        return False


def _adelanto_de_cot(db: Session, cot_id: int, lock: bool = False,
                     incluir_anulado: bool = False) -> Optional[MonzaContAdelanto]:
    """Adelanto VIGENTE de la venta (los anulados no cuentan). Con `lock=True`, lectura
    BLOQUEANTE.

    `lock` es OBLIGATORIO en todo camino que ESCRIBA monto_aplicado: SQLAlchemy emite
    un UPDATE ciego ("SET monto_aplicado = <valor calculado>", no "SET x = x - m"), así
    que dos escritores concurrentes producen un lost update. READ COMMITTED NO lo evita:
    da lecturas frescas, no serialización. Ver docs/regla-lecturas-de-plata.md.

    `incluir_anulado=True` solo para los dos caminos que necesitan la fila ANULADA:
      · verificar_adelanto, que la REUSA (el UNIQUE deja un adelanto por venta, así que
        no se puede crear otra; ver service.reactivar_adelanto).
      · eliminar_cobranza, que devuelve el monto a monto_aplicado — el invariante
        monto_aplicado == Σ cobranzas 'adelanto' tiene que cuadrar incluso si la fila
        quedó anulada por una reparación manual en la BD.
    coalesce: una fila legada sin la columna (o con NULL) es un adelanto verificado —
    'aprobado'—, jamás uno anulado (leerlo al revés apagaría el cortafuego de
    Abastecimiento de todas las ventas viejas)."""
    q = db.query(MonzaContAdelanto).filter(MonzaContAdelanto.cotizacion_id == cot_id)
    if not incluir_anulado:
        q = q.filter(func.coalesce(MonzaContAdelanto.estado, ADEL_APROBADO) != ADEL_ANULADO)
    if lock:
        q = q.populate_existing().with_for_update()
    return q.first()


def _cobranzas_bloqueadas(db: Session, factura_id: int) -> list:
    """Cobranzas de la factura leídas BAJO LOCK — siempre la última versión commiteada.

    No usar la relación perezosa para DECIDIR un tope de plata: es una lectura plana.
    Espejo de routers/contabilidad.py._cobranzas_bloqueadas."""
    return (db.query(MonzaContCobranza)
            .filter(MonzaContCobranza.factura_id == factura_id)
            .populate_existing().with_for_update().all())


def _factoring_bloqueado(db: Session, factura_id: int) -> Optional[MonzaContFactoring]:
    """Factoring de la factura leído BAJO LOCK (mismo motivo que _cobranzas_bloqueadas:
    sin esto, un adelanto podía entrar a una factura recién cedida al factor, y el
    cupo/retención se decidía sobre el snapshot del inicio del request).
    Espejo de routers/contabilidad.py._factoring_bloqueado."""
    return (db.query(MonzaContFactoring)
            .filter(MonzaContFactoring.factura_id == factura_id)
            .populate_existing().with_for_update().first())


def _fecha_estricta(s, campo: str) -> Optional[date]:
    """Parseo ESTRICTO de una fecha explícita del payload: si viene y no parsea → 400.
    Espejo de tesoreria/router.py._fecha de GA — sin esto, una fecha mal escrita caía
    en SILENCIO a la fecha de hoy (dato de plata con fecha inventada)."""
    if s in (None, ""):
        return None
    d = _parse_date(s)
    if d is None:
        raise HTTPException(400, f"{campo}: fecha inválida (use AAAA-MM-DD)")
    return d


def _adelantos_by_cot(db: Session, cot_ids: List[int]) -> dict:
    """Adelantos VIGENTES de varias ventas en una sola query (evita N+1 en el listado).
    Filtra los ANULADOS con el mismo criterio de _adelanto_de_cot: la venta cuyo adelanto
    se anuló vuelve a mostrarse como 'por_verificar'."""
    out: dict = {}
    if cot_ids:
        for a in (db.query(MonzaContAdelanto)
                  .filter(MonzaContAdelanto.cotizacion_id.in_(cot_ids),
                          func.coalesce(MonzaContAdelanto.estado, ADEL_APROBADO) != ADEL_ANULADO)
                  .all()):
            out[a.cotizacion_id] = a
    return out


def _adelanto_id_de(db: Session, factura) -> Optional[int]:
    """Id del adelanto VIGENTE de la venta de esta factura (para marcar la cobranza
    medio='adelanto' en el serializador, ver _serialize_factura). None si la factura no
    cuelga de una venta o la venta no tiene adelanto."""
    if not getattr(factura, "cotizacion_id", None):
        return None
    adel = _adelanto_de_cot(db, factura.cotizacion_id)
    return adel.id if adel is not None else None


def _facturas_anticipo_de_cot(db: Session, cot_id: Optional[int]) -> List:
    """Facturas de ANTICIPO (es_anticipo=1) VIVAS de la venta, en orden de emisión.

    En Monza el vínculo adelanto ↔ factura de anticipo es DERIVADO: no existe
    MonzaContAdelanto.factura_anticipo_id (ver models.py) porque hay UN adelanto por venta
    (uq_monza_cont_adelanto_cotizacion), así que toda factura es_anticipo=1 de la venta
    respalda a ESE adelanto. Misma derivación que publica service.estado_adelanto en
    `factura_anticipo_folio`: una sola definición de «el anticipo de esta venta».
    No hay estado 'anulada' en las facturas (se BORRAN, ver eliminar_factura), así que
    «viva» == la fila existe.

    `coalesce(es_anticipo, 0) != 0` y no `== 1`: es un GUARD, así que ante un valor raro
    tiene que fallar CERRADO. Una fila legada con es_anticipo NULL (tabla creada por
    create_all antes del server_default, ver models.py) o con cualquier otro entero se
    trata igual — la misma lección del ORDER BY del FIFO del adelanto, donde el NULL movía
    la plata a la factura equivocada."""
    if not cot_id:
        return []
    return (db.query(MonzaContFacturaCliente)
            .filter(MonzaContFacturaCliente.cotizacion_id == cot_id,
                    func.coalesce(MonzaContFacturaCliente.es_anticipo, 0) != 0)
            .order_by(MonzaContFacturaCliente.id.asc()).all())


def _bloqueo_anticipo_del_adelanto(db: Session, cot_id: Optional[int], *, accion: str) -> None:
    """El adelanto que RESPALDA una factura de ANTICIPO no se anula ni se elimina.

    Es plata seria y el hueco era grande: los dos candados históricos (aplicado > TOL y
    conciliado con el banco) no miran el DOCUMENTO TRIBUTARIO. Con eso, la remediación que
    el propio 409 recomienda —«revierta esa cobranza antes de anularlo»— era el camino
    para romper el respaldo de una factura de anticipo YA EMITIDA: se borraba la cobranza
    (monto_aplicado volvía a 0), se anulaba el adelanto, y quedaba una factura de anticipo
    ante el SII por cobrar, el adelanto invisible (`adelanto: null`) y el depósito del
    cliente sin destino. Peor por la vía SII: el DTE 33 vuela con `aplicar_adelantos=False`
    (monza_wasabil_dte/router.py), así que durante todo el vuelo monto_aplicado es 0 y
    anular pasaba liso — al llegar el folio, _aplicar_adelantos_pendientes ya no encontraba
    el adelanto y la plata no se aplicaba NUNCA, en silencio.

    Un solo mensaje y una sola salida a propósito: eliminar la factura de anticipo. Ese
    camino EXISTE (DELETE /facturas/{id}) y es el que sabe decidir sobre el SII —
    `_bloqueo_dte_factura` rechaza con 409 explicando la nota de crédito si el folio ya
    está en el SII, y el guard de «anticipo ya descontado» rechaza si la factura final ya
    lo descontó. Duplicar aquí ese juicio (ramas «emitido» / «no emitido») sería una
    segunda fuente de verdad sobre el estado del DTE que puede quedar desalineada.

    Corre BAJO el lock de la cotización (el que ya toman anular_adelanto y
    eliminar_adelanto): crear_factura —el único que crea el anticipo— se serializa por esa
    misma fila, así que no hay ventana entre el chequeo y la anulación."""
    facturas = _facturas_anticipo_de_cot(db, cot_id)
    if not facturas:
        return
    folios = ", ".join(f"N° {f.numero_factura}" if (f.numero_factura or "").strip()
                       else f"sin folio (id {f.id})" for f in facturas)
    raise HTTPException(
        409, f"El adelanto respalda la factura de ANTICIPO {folios}: no se puede {accion} "
             f"sin dejar ese documento tributario sin respaldo. Elimine primero la factura "
             f"de anticipo (Facturas → eliminar), que valida si el folio ya está en el SII, "
             f"y vuelva a intentarlo.")


def validar_venta_para_adelanto(cot, monto: float, *, verbo: str) -> None:
    """UNA fuente de verdad para las precondiciones de REGISTRAR un adelanto.

    `verificar_adelanto` (Contabilidad) y `monza_tesoreria.aprobar_adelanto` son la MISMA
    regla de negocio sobre la MISMA fila, y la deuda M5 del reconocimiento es justo esa:
    dos copias de una regla de plata, con el arreglo puesto en una sola. Pasó de nuevo con
    M2 (el 400 de «adelanto no pactado» se quitó en Contabilidad y quedó vivo en
    Tesorería), así que la regla vive AQUÍ y los dos endpoints la llaman.

    `verbo` es lo único que cambia entre los dos textos («verificar» / «aprobar»).

    Y NO hay requisito de `pct_adelanto > 0` (M2): el cliente que deposita un adelanto que
    nadie pactó existe, y ese 400 dejaba su plata en el banco SIN DESTINO hasta que
    Comercial hiciera un PATCH a la cotización — un trámite entre áreas para registrar
    dinero ya recibido. El tope de abajo (≤ total de la venta) es la defensa real."""
    if cot.estado not in ESTADOS_VENTA:
        raise HTTPException(400, f"La cotización debe estar vendida para {verbo} el adelanto")
    if monto > _f(cot.total_bruto) + TOL_PAGO:
        raise HTTPException(
            400, f"El monto del adelanto no puede exceder el total de la venta "
                 f"({_f(cot.total_bruto):.0f})")


def validar_adelanto_editable(db: Session, adel: Optional[MonzaContAdelanto]) -> None:
    """Los 2 candados de plata para EDITAR el monto de un adelanto que ya existe
    (misma fuente de verdad para Contabilidad y Tesorería, ver validar_venta_para_adelanto).

    El llamador debe traer la fila leída BAJO LOCK (`with_for_update` + `populate_existing`):
    estos guards deciden sobre plata y una lectura plana los decidiría con la versión del
    identity map. Ver docs/regla-lecturas-de-plata.md."""
    if adel is None:
        return
    if _f(adel.monto_aplicado) > TOL:
        logger.warning("Re-verificación de adelanto bloqueada: cot=%s adelanto=%s monto_aplicado=%s",
                       adel.cotizacion_id, adel.id, _f(adel.monto_aplicado))
        raise HTTPException(
            409, f"El adelanto ya fue aplicado a una factura (aplicado "
                 f"{_f(adel.monto_aplicado):.0f}); revierta esa cobranza antes de modificarlo")
    # 'conciliado' se DERIVA de la existencia del enlace de Tesorería (no hay columna):
    # editar el monto dejaría el cruce bancario apuntando a otro monto.
    if db.query(MonzaTesConciliacion.id).filter(
            MonzaTesConciliacion.adelanto_id == adel.id).first():
        raise HTTPException(409, "El adelanto ya está conciliado con un abono del banco; "
                                 "desconcilie el abono en Tesorería antes de modificar el adelanto")


def _adelanto_ids_by_factura(db: Session, facturas) -> dict:
    """{factura_id: adelanto_id} de todo un listado en UNA sola query (jamás
    _adelanto_id_de por factura dentro del loop). Gemela de _guias_vivas."""
    cot_ids = {f.cotizacion_id for f in facturas if getattr(f, "cotizacion_id", None)}
    if not cot_ids:
        return {}
    by_cot = _adelantos_by_cot(db, list(cot_ids))
    return {f.id: by_cot[f.cotizacion_id].id for f in facturas
            if getattr(f, "cotizacion_id", None) in by_cot}


def _dte_factura_no_emitido(db: Session, factura_id: int) -> bool:
    """True si la factura tiene un DTE electrónico (factura 33) que AÚN no está emitido
    por el SII (borrador, en vuelo, procesando o rechazado).

    Import LOCAL del paquete monza_wasabil_dte (mismo patrón que
    monza_router_despachos._guia_electronica_activa): evita el ciclo de imports
    contabilidad ↔ wasabil y no carga el módulo DTE en los caminos que no lo necesitan.

    Tolerancias, deliberadamente ASIMÉTRICAS (el porqué de cada una):
      · Módulo ausente o modelo sin la columna `factura_id` (código anterior a la Fase
        de facturas electrónicas) → devuelve False: si el código no existe, no puede
        existir una factura electrónica que proteger.
      · Error de esquema en la BD (tabla/columna faltante por un deploy A MEDIAS) →
        503 explícito, NUNCA False. Aquí sí puede haber facturas electrónicas vivas, y
        apagar el guard en silencio movería plata contra un documento que el SII no
        conoce. Se falla ruidoso pidiendo correr el init_db, que es la regla de la casa.
        Tampoco se hace db.rollback() (a diferencia del guard de guías): este helper
        corre DENTRO de crear_factura, con la factura y sus líneas ya flusheadas.
    """
    from sqlalchemy.exc import ProgrammingError, OperationalError
    try:
        from monza_wasabil_dte.models import MonzaWasabilDte, STATUS_EMITIDO
    except ImportError:
        return False
    col = getattr(MonzaWasabilDte, "factura_id", None)
    if col is None:
        return False
    try:
        dte = db.query(MonzaWasabilDte).filter(col == factura_id).first()
    except (ProgrammingError, OperationalError) as e:
        logger.error("Guard SII de adelantos: esquema de monza_wasabil_dte incompleto: %s", e)
        raise HTTPException(
            503,
            "El módulo de facturas electrónicas está a medio instalar: corre "
            "backend/monza_wasabil_dte/init_db.py y reinicia el backend",
        ) from e
    return dte is not None and dte.status_id != STATUS_EMITIDO


def _referencia_ancla_factura(factura_id: int) -> str:
    """La referencia interna con la que el DTE 33 de esta factura vive en Wasabil.

    Fuente ÚNICA: `monza_wasabil_dte/router.py:_referencia_interna_factura`. Se importa
    (no se duplica el formato) para que un cambio de formato no deje esta nota citando
    una referencia que ya no existe; el literal es sólo el plan B si el módulo DTE no
    está instalado, caso en que tampoco habría anclas que conservar."""
    try:
        from monza_wasabil_dte.router import _referencia_interna_factura
        return _referencia_interna_factura(factura_id)
    except Exception:                                        # pragma: no cover
        return f"FACT-{factura_id}"


def _conservar_ancla_dte(db: Session, dte, factura_id: int, usuario_id) -> None:
    """DESLIGA el ancla de la factura que se está borrando, en vez de DESTRUIRLA.
    Espejo EXACTO de GA `routers/contabilidad.py:_conservar_ancla_dte` (mantener en sync).

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
    colisiona con el único `uq_monza_wasabil_dte_factura`) no la lista ninguna pantalla
    —todas las consultas del módulo son por factura_id o despacho_id— y satisface la FK
    RESTRICT que impide borrar la factura con el ancla apuntándola.

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
    # factura (FK RESTRICT `monza_wasabil_dte.factura_id` → 1451). El orden natural de la
    # unidad de trabajo ya lo hace, pero de eso no depende un candado anti doble emisión.
    db.flush()
    logger.warning("Ancla DTE CONSERVADA (factura Monza %s eliminada): uuid=%s status=%s ref=%s",
                   factura_id, dte.uuid, dte.status_id, ref)


def _plata_bloqueada_por_sii(db: Session, factura) -> bool:
    """True si NO debe entrar plata contra esta factura porque su documento electrónico
    todavía no existe ante el SII.

    PUERTA ÚNICA. Esta condición vivía COPIADA en tres sitios (aplicación de adelantos,
    liberación de adelantos y `registrar_cobranza`) y le faltaba justo donde más importa:
    el factoring. Sin ella se puede ceder al factor una factura que el SII nunca conoció,
    y el resultado es un zombi —la factura queda 'factorizada' (la aplicación automática de
    adelantos devuelve 0) y encima IMBORRABLE (`eliminar_factura` rechaza toda factura con
    factoring)—, secuestrando para siempre el cupo facturable de esa mercadería.

    Nombrar la condición no es cosmética: `revertir_factoring` abre EXACTAMENTE donde este
    helper dice True, así que puerta de entrada y puerta de salida no pueden desalinearse.
    Con la condición copiada, tocar una de las dos dejaba o una salida que nunca abre o —peor—
    una que borra una cesión al factor que era real.

    Sólo se consulta al módulo DTE cuando NO hay folio Y el documento es FACTURA: una
    factura con folio ya está ante el SII, y una BOLETA jamás tiene un DTE 33, así que ni
    una ni otra se exponen a un 503 gratuito en una base sin migrar."""
    return ((factura.tipo_doc or "factura") == "factura"
            and not (factura.numero_factura or "").strip()
            and _dte_factura_no_emitido(db, factura.id))


def _exigir_sii_emitido(db: Session, factura, accion: str) -> None:
    """409 accionable si la factura electrónica aún no tiene folio del SII. `accion` es la
    frase que cierra el mensaje ('registrar pagos', 'cederla al factoring', …) para que el
    operador sepa qué quedó sin hacer. La salida siempre es la misma: esperar el folio o
    usar «Reintentar» (que consulta a Wasabil). Apenas el SII confirma, la operación se
    puede repetir tal cual."""
    if _plata_bloqueada_por_sii(db, factura):
        raise HTTPException(
            409, f"Esta factura todavía no está emitida ante el SII: espera el folio (o usa "
                 f"«Reintentar») antes de {accion}")


def _bloqueo_dte_factura(db: Session, factura_id: int, usuario_id=None) -> None:
    """Guard SII del BORRADO de una factura (Fase 6, espejo de GA routers/contabilidad.py).

    LA REGLA, en una línea: el ancla se BORRA sólo cuando consta que el documento NO
    existe en Wasabil; si el documento EXISTE (hay uuid) se CONSERVA desligada de la
    factura; y si no se puede CONCLUIR qué hay allá, el borrado se BLOQUEA (fail-closed).
    Desligar o borrar el ancla es además lo que evita que el DELETE de la factura choque
    con la FK RESTRICT `fk_monza_wasabil_dte_factura_id` (1451).

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

    El porqué de cada rama, de más a menos evidente:
      · EMITIDO       → el folio está en el SII. Se anula allá (nota de crédito), no aquí.
                        Salvo que la factura local haya quedado SIN N° por colisión de
                        folio: ese 409 lleva su propio remedio (ver el `if`).
      · claim vigente → hay un request emitiendo AHORA MISMO; borrar bajo sus pies deja
                        un documento huérfano en Wasabil sin ancla local.
      · estado 2|6 (procesando/borrador) → el documento está VIVO en Wasabil (o el estado
                        local dice que lo está, sin identificador con que verificarlo). Se
                        resuelve allá; «Reintentar» sincroniza su estado.
      · RECHAZADO (4) con uuid → NO bloquea el borrado de la FACTURA (un rechazo no tiene
                        folio que perder, y bloquear dejaría la factura imborrable para
                        siempre secuestrando el cupo facturable: «Reintentar» reenvía el
                        MISMO payload, así que si la causa del rechazo está en la factura
                        el SII la rechaza otra vez) — pero SÍ conserva el ancla, que es la
                        única llave del documento que existe en Wasabil. Ver
                        `_conservar_ancla_dte` (hallazgo ALTO-3: el código borraba el
                        ancla mientras el comentario prometía lo contrario).
      · estado DESCONOCIDO con uuid → hay documento en Wasabil y no sabemos en qué quedó:
                        se falla CERRADO nombrando el uuid, porque un guard que protege un
                        documento IRREVERSIBLE y no puede concluir no debe abrir la puerta.
      · en vuelo (AMBIGUO) → la respuesta se perdió; el documento pudo nacer con folio
                        real. Borrar el ancla FACT-<id> lo volvería INADOPTABLE y
                        habilitaría un SEGUNDO DTE por la misma mercadería. Imborrable
                        A PROPÓSITO: parece un bug, no lo es (ver README del módulo).
      · fallo CONFIRMADO sin uuid (error de red/4xx, sin claim) → SÍ se borra, con su
                        ancla: consta que el documento NUNCA nació, así que no hay llave
                        que perder, y si no se borrara la factura zombi secuestraría el
                        cupo facturable de esa mercadería para siempre.

    Tolerancias idénticas a `_dte_factura_no_emitido` (módulo ausente → no hay nada que
    proteger; esquema a medias → 503 ruidoso pidiendo correr el init_db)."""
    from sqlalchemy.exc import ProgrammingError, OperationalError
    try:
        from monza_wasabil_dte.models import (
            MonzaWasabilDte, STATUS_EMITIDO, STATUS_PROCESANDO, STATUS_PENDIENTE,
            STATUS_FALLIDO,
        )
        from monza_wasabil_dte.service import claim_vigente
    except ImportError:
        return
    col = getattr(MonzaWasabilDte, "factura_id", None)
    if col is None:
        return
    try:
        # populate_existing + FOR UPDATE: datos FRESCOS bajo el lock. Sin populate_existing
        # SQLAlchemy sirve la versión del identity map y se decidiría con el estado viejo,
        # ignorando el claim que otro request acaba de commitear (regla-lecturas-de-plata).
        dte = (db.query(MonzaWasabilDte).filter(col == factura_id)
               .populate_existing().with_for_update().first())
    except (ProgrammingError, OperationalError) as e:
        logger.error("Guard SII de borrado: esquema de monza_wasabil_dte incompleto: %s", e)
        raise HTTPException(
            503,
            "El módulo de facturas electrónicas está a medio instalar: corre "
            "backend/monza_wasabil_dte/init_db.py y reinicia el backend",
        ) from e
    if dte is None:
        return
    if dte.status_id == STATUS_EMITIDO:
        # CASO NORMAL: el folio ya está escrito en la factura local → el documento vive
        # ante el SII y se anula allá.
        # CASO TRABADO (colisión de folio, hallazgo B-3): el DTE quedó EMITIDO pero la
        # factura local se quedó SIN N° porque ese folio ya estaba registrado A MANO en
        # otra factura (el UNIQUE de Monza es global). Ahí "anúlala en Wasabil" es un
        # consejo EQUIVOCADO —el documento del SII está bien; lo que falla es local— y
        # el operador queda sin salida: la factura no se borra y, si es un anticipo, la
        # factura del despacho tampoco se puede emitir (la referencia 33 exige el folio).
        # El remedio es el MISMO que ya nombra monza_wasabil_dte/router.py cuando ocurre
        # la colisión: corregir el N° de la OTRA factura y volver a consultar el estado
        # —el folio se graba solo—. Se repite aquí porque este 409 es donde el operador
        # llega cuando intenta salir del atolladero borrando. Mantener ambos textos en sync.
        folio_txt = str(dte.folio or "").strip()
        local = (db.query(MonzaContFacturaCliente.numero_factura)
                 .filter(MonzaContFacturaCliente.id == factura_id).first())
        if folio_txt and local is not None and not (local[0] or "").strip():
            otra = (db.query(MonzaContFacturaCliente)
                    .filter(MonzaContFacturaCliente.numero_factura == folio_txt).first())
            if otra is not None:
                venta = f" de la venta {otra.numero_cotizacion}" if otra.numero_cotizacion else ""
                raise HTTPException(
                    409, f"Esta factura YA está emitida ante el SII (folio {folio_txt}), "
                         f"pero quedó sin N° local porque ese folio ya estaba registrado a "
                         f"mano en la factura local #{otra.id}{venta}. No la elimines ni la "
                         f"re-emitas: corrige (o elimina) el N° de esa otra factura en "
                         f"Contabilidad → Facturas y vuelve a consultar el estado de ésta — "
                         f"el folio se graba solo.")
            raise HTTPException(
                409, f"Esta factura YA está emitida ante el SII (folio {folio_txt}) pero "
                     "todavía no tiene el N° grabado localmente: consulta su estado para "
                     "que el folio se grabe y, si de verdad hay que eliminarla, anúlala "
                     "primero en Wasabil (nota de crédito).")
        raise HTTPException(
            409, f"Esta factura tiene DTE emitido al SII (folio {dte.folio}): anúlala "
                 "primero en Wasabil (nota de crédito) y luego elimínala aquí")
    if claim_vigente(dte):
        raise HTTPException(
            409, "Esta factura tiene una emisión SII en curso: espera el resultado "
                 "(Emitida o Fallida) antes de eliminar")
    # Documento vivo en Wasabil (procesando o pendiente): resolverlo allá primero. Sin
    # exigir uuid: un estado que dice «vivo» sin identificador con que verificarlo es
    # justamente lo que NO se puede concluir, y esto protege un documento irreversible.
    if dte.status_id in (STATUS_PROCESANDO, STATUS_PENDIENTE):
        raise HTTPException(
            409, "El documento está en curso en Wasabil para esta factura: espera el "
                 "resultado (o usa «Reintentar» para sincronizar su estado) antes de eliminarla")
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
    if dte.status_id != STATUS_FALLIDO:
        # Hay documento en Wasabil (uuid) y el estado local no dice en qué quedó: FALLA
        # CERRADO. La salida existe y se nombra: consultar el estado / «Reintentar»
        # pregunta por ese uuid y deja la fila concluyente.
        raise HTTPException(
            409, "Hay un documento en Wasabil para esta factura (identificador interno "
                 f"{dte.uuid}) y aquí no consta en qué quedó ante el SII: consulta su "
                 "estado con «Reintentar» antes de eliminarla. Si Wasabil lo muestra "
                 "EMITIDO, no se elimina: se anula allá con una nota de crédito.")
    # RECHAZADO con uuid: la factura se borra (no secuestrar el cupo facturable) pero el
    # ancla NO se destruye — es la única llave del documento que existe en Wasabil.
    _conservar_ancla_dte(db, dte, factura_id, usuario_id)


def _reencauzar_adelanto_al_anticipo(db: Session, cot: MonzaCotizacion,
                                     anticipo: MonzaContFacturaCliente,
                                     adel: MonzaContAdelanto, *, necesario: float,
                                     advertencias: Optional[List[str]] = None) -> float:
    """RE-RUTEA hacia la factura de ANTICIPO el adelanto que ya cayó en otras facturas de
    la misma venta. Devuelve cuánto liberó.

    EL PORQUÉ (hallazgo A-4 del multienjambre, reproducido de forma DETERMINISTA): el
    orden natural del negocio es «Tesorería aprueba el lunes, Contabilidad emite el
    anticipo el martes». Si el lunes ya existía la factura del despacho, la plata se
    aplicó ahí; el martes `_aplicar_adelanto` lee pendiente = monto − monto_aplicado = 0
    y la factura de anticipo —el documento que certifica plata YA RECIBIDA— nace IMPAGA y
    se queda así: re-aprobar y re-verificar responden 409. No hay fuga (Σ saldos cuadra),
    pero Cobranzas persigue el documento equivocado.

    Mover esa plata es legítimo: la cobranza `medio='adelanto'` la GENERA EL SISTEMA
    (registrarla a mano ya está prohibido — ver registrar_cobranza), así que no se está
    editando nada que haya tecleado un operador.

    Reglas (todas verificadas en el repro de la fase):
      · Corre bajo el lock de la COTIZACIÓN que ya sostiene el llamador y con el ADELANTO
        ya bloqueado por _aplicar_adelanto. Tomar aquí locks de OTRAS facturas después del
        adelanto no abre un ciclo nuevo: todo camino que escribe monto_aplicado pasa antes
        por el lock de la cotización (crear_factura, verificar_adelanto, eliminar_cobranza,
        eliminar_factura y Tesorería), así que mientras se sostiene ese lock nadie más está
        moviendo estas filas; y los endpoints que sí bloquean una factura sin pasar por la
        venta (registrar_cobranza, factoring) jamás piden después la cotización ni el
        adelanto — esperan y terminan. Es el mismo patrón que ya usaba _aplicar_adelanto.
      · Se libera lo JUSTO (`necesario`), nunca de más: si el adelanto excedía al anticipo,
        el excedente debe QUEDARSE en la factura del despacho —volver a aplicarlo no es
        cosa de esta pasada, que solo aplica sobre la factura nueva—.
      · Orden LIFO (id desc): al liberar primero de la factura más nueva, lo que queda
        aplicado sigue respetando el FIFO por id con que se repartió originalmente.
      · SE SALTAN, con advertencia: factura con factoring vigente (su saldo es retención
        del factor, no deuda del cliente), factura con DTE 33 aún no emitido (el SII no
        conoce ese documento) y cobranza ya conciliada con un abono del banco (borrarla
        dejaría el movimiento bancario apuntando a nada — mismo guard de eliminar_cobranza).
      · Una cobranza se recorta PARCIALMENTE cuando alcanza y sobra: mantiene el
        invariante `adel.monto_aplicado == Σ cobranzas 'adelanto'` sin borrar plata que
        sigue bien asignada.
      · Nunca falla: si los guards impiden liberar todo, el anticipo queda impago y se
        agrega una ADVERTENCIA que dice exactamente qué hacer."""
    liberado = 0.0
    bloqueadas: List[str] = []
    # func.coalesce: una fila legada con es_anticipo NULL no debe hacerse pasar por
    # anticipo (ni quedar fuera del filtro) — ver el normalizador de init_db.
    otras = (db.query(MonzaContFacturaCliente)
             .filter(MonzaContFacturaCliente.cotizacion_id == cot.id,
                     MonzaContFacturaCliente.id != anticipo.id,
                     func.coalesce(MonzaContFacturaCliente.es_anticipo, 0) == 0)
             .order_by(MonzaContFacturaCliente.id.desc())
             .populate_existing().with_for_update().all())
    for f in otras:
        if round(necesario - liberado, 2) <= TOL:
            break
        cobs = [c for c in _cobranzas_bloqueadas(db, f.id) if c.medio == MEDIO_ADELANTO]
        if not cobs:
            continue
        fac = _factoring_bloqueado(db, f.id)
        if fac and fac.estado == "vigente":
            bloqueadas.append(f"la factura {f.numero_factura or f'#{f.id}'} está cedida a un factor")
            continue
        if _plata_bloqueada_por_sii(db, f):
            bloqueadas.append(f"la factura #{f.id} tiene una emisión al SII sin resolver")
            continue
        tocada = False
        for c in sorted(cobs, key=lambda x: x.id, reverse=True):
            falta = round(necesario - liberado, 2)
            if falta <= TOL:
                break
            if db.query(MonzaTesConciliacionIngreso.id).filter(
                    MonzaTesConciliacionIngreso.cobranza_id == c.id).first():
                bloqueadas.append(
                    f"el pago del adelanto en la factura {f.numero_factura or f'#{f.id}'} "
                    "está conciliado con el banco")
                continue
            monto = _f(c.monto)
            if monto <= falta + TOL:
                db.delete(c)
                liberado = round(liberado + monto, 2)
            else:
                c.monto = round(monto - falta, 2)
                liberado = round(liberado + falta, 2)
            tocada = True
        if tocada:
            db.flush()
            # Saldo/estado de la factura raideada, desde las cobranzas frescas BAJO LOCK.
            _recompute_factura(f, cobranzas=_cobranzas_bloqueadas(db, f.id))
    if liberado > TOL:
        # UPDATE bajo el lock del adelanto (lo tomó _aplicar_adelanto): el invariante
        # monto_aplicado == Σ cobranzas 'adelanto' se mantiene EXACTO.
        adel.monto_aplicado = round(max(_f(adel.monto_aplicado) - liberado, 0.0), 2)
        db.flush()
        logger.info("Adelanto re-ruteado a la factura de anticipo %s (venta %s): %.0f",
                    anticipo.id, cot.id, liberado)
    faltante = round(necesario - liberado, 2)
    if faltante > TOL and advertencias is not None:
        motivo = ("; ".join(dict.fromkeys(bloqueadas))
                  or "el adelanto ya estaba aplicado en otras facturas de esta venta")
        # El formato chileno se arma sobre el NÚMERO, no sobre la frase (un
        # .replace(",", ".") de la frase entera se comería las comas del texto).
        monto_txt = f"{faltante:,.0f}".replace(",", ".")
        advertencias.append(
            f"El adelanto de esta venta ya estaba aplicado en otra factura y no se pudieron "
            f"traspasar ${monto_txt} a la factura de anticipo porque {motivo}. La factura "
            "de anticipo queda por cobrar: revierte esa cobranza de adelanto en la otra "
            "factura (o liquida el factoring / resuelve la emisión al SII) y vuelve a "
            "aprobar el adelanto en Tesorería.")
    return liberado


def _aplicar_adelanto(db: Session, cot: MonzaCotizacion, factura: MonzaContFacturaCliente,
                      usuario_id=None, advertencias: Optional[List[str]] = None) -> None:
    """Aplica el adelanto VERIFICADO de la venta (si tiene saldo no aplicado) como una
    cobranza 'adelanto' sobre esta factura, hasta el monto de la factura. `monto_aplicado`
    evita aplicar dos veces y soporta facturación parcial. Se llama dentro de crear_factura,
    con la cotización ya bloqueada (serializa la aplicación concurrente), y también
    desde verificar_adelanto (aplicación RETROACTIVA a facturas ya emitidas).

    INVARIANTE: adel.monto_aplicado == suma de cobranzas 'adelanto' de las facturas de la
    venta. Si se revierte una cobranza 'adelanto' (eliminar_cobranza), se descuenta de vuelta.

    Endurecimientos (espejo del fix HIGH de tesorería GA):
      - Cap por el SALDO actual de la factura, no por su bruto: en crear_factura son
        iguales (factura recién nacida, sin cobranzas), pero al RE-aplicar sobre una
        factura con cobranzas parciales el cap por bruto sobre-aplicaría.
      - Con factoring VIGENTE no se aplica: ese saldo es retención del factor, no
        deuda del cliente (espejo del guard de GA).
      - Vía B: sobre una factura de ANTICIPO, si el adelanto ya se había aplicado a otra
        factura de la venta, se RE-RUTEA primero (ver _reencauzar_adelanto_al_anticipo).

    `advertencias` (opcional): lista donde dejar los avisos que el llamador vaya a
    devolver al operador (hoy solo la del re-ruteo incompleto). Es opcional a propósito —
    los caminos que no tienen dónde mostrarlos (la aplicación diferida del SII) siguen
    llamando igual que antes.
    """
    # lock=True es OBLIGATORIO: este camino ESCRIBE monto_aplicado (UPDATE ciego) —
    # ver la regla en _adelanto_de_cot y docs/regla-lecturas-de-plata.md. Orden global
    # de locks respetado: cotización (la trae el caller) → factura → adelanto.
    adel = _adelanto_de_cot(db, cot.id, lock=True)
    if adel is None:
        return
    fac = _factoring_bloqueado(db, factura.id)
    if fac and fac.estado == "vigente":
        return
    # Guard SII: una factura ELECTRÓNICA que todavía no está emitida (sin folio y con su
    # DTE en vuelo / borrador / rechazado) no debe recibir plata. Es el otro lado del
    # diferimiento de adelantos del modo SII: el persistidor ya se salta la aplicación
    # (aplicar_adelantos=False), pero verificar_adelanto re-aplica RETROACTIVAMENTE a
    # TODAS las facturas de la venta y Tesorería aprueba adelantos por su cuenta. Sin
    # este guard, esa plata caía en una factura fantasma: la dejaba 'pagada', amarraba
    # el adelanto a un documento que el SII no conoce y bloqueaba su borrado ("tiene
    # cobranzas"). La aplicación ocurre igual apenas el SII confirma el folio, desde
    # _finalizar_factura_emitida. Espejo de routers/contabilidad.py de Grupo AM.
    # Solo se consulta cuando NO hay folio Y el documento es una FACTURA: una factura
    # con folio (manual o ya emitida) ni siquiera toca el módulo DTE, y una BOLETA jamás
    # puede tener un DTE 33 — consultarlo la exponía a un 503 gratuito en una BD sin
    # migrar, en un camino que antes de la Fase 6 funcionaba.
    if _plata_bloqueada_por_sii(db, factura):
        return
    cobs = _cobranzas_bloqueadas(db, factura.id)
    saldo = round(max(_f(factura.monto_bruto) - sum(_f(c.monto) for c in cobs), 0.0), 2)
    pendiente = _f(adel.monto) - _f(adel.monto_aplicado)
    # Vía B: si la factura de ANTICIPO no alcanza a saldarse porque esa misma plata ya
    # cayó en otra factura de la venta (Tesorería aprobó antes de que se emitiera el
    # anticipo), se RE-RUTEA. Aquí es el único punto de aplicación del adelanto en
    # Contabilidad, así que cubre las tres puertas: la vía manual (crear_factura), la
    # retroactiva (verificar_adelanto) y la del SII (_aplicar_adelantos_pendientes, que
    # corre al confirmarse el folio). Ver el porqué completo en el helper.
    if getattr(factura, "es_anticipo", 0) and _f(adel.monto_aplicado) > TOL \
            and round(saldo - pendiente, 2) > TOL:
        _reencauzar_adelanto_al_anticipo(
            db, cot, factura, adel, necesario=round(saldo - pendiente, 2),
            advertencias=advertencias)
        pendiente = _f(adel.monto) - _f(adel.monto_aplicado)
    aplicar = round(min(pendiente, saldo), 2)
    if aplicar <= TOL:
        return
    # Glosa de la cobranza: con % pactado lo nombra; sin él (adelanto NO PACTADO, que
    # ahora se puede registrar) diría «Adelanto 0% aplicado» — un dato de plata con una
    # cifra falsa en la observación.
    pct_adel = int(getattr(cot, "pct_adelanto", 0) or 0)
    db.add(MonzaContCobranza(
        factura_id=factura.id, fecha=adel.fecha_pago or date.today(),
        monto=aplicar, medio=MEDIO_ADELANTO, banco=adel.banco,
        numero_operacion=adel.numero_operacion,
        observaciones=(f"Adelanto {pct_adel}% aplicado" if pct_adel > 0
                       else "Adelanto aplicado (no pactado al cerrar la venta)"),
        usuario_id=usuario_id,
    ))
    adel.monto_aplicado = round(_f(adel.monto_aplicado) + aplicar, 2)


def _aplicar_adelantos_pendientes(db: Session, cot: MonzaCotizacion,
                                  factura: MonzaContFacturaCliente,
                                  usuario_id=None,
                                  advertencias: Optional[List[str]] = None) -> List[str]:
    """Nombre-CONTRATO (espejo de routers/contabilidad.py de Grupo AM) para el módulo de
    facturas electrónicas: al confirmarse el folio del SII, _finalizar_factura_emitida
    aplica aquí el adelanto que la emisión había diferido.

    En Monza la venta tiene UN adelanto (no una lista de adelantos aprobados como GA),
    así que delega en _aplicar_adelanto. Existe para que el módulo DTE importe el mismo
    nombre en ambas marcas y no haya que recordar cuál se llama distinto.

    DEVUELVE las advertencias que produjo esta aplicación (lista vacía si no hubo). No es
    adorno: sobre una factura de ANTICIPO, _aplicar_adelanto puede tener que RE-RUTEAR
    plata que ya cayó en otra factura de la venta, y si los guards se lo impiden
    (factoring vigente, DTE sin emitir, cobranza conciliada con el banco) el único aviso
    de que la factura de anticipo quedó IMPAGA es esa advertencia. Por la vía manual
    crear_factura ya la devuelve; por ESTA vía —la del SII, que aplica el adelanto al
    confirmarse el folio— la lista se descartaba y el operador veía solo «Factura emitida
    · Folio SII X», sin ninguna señal de que el documento nació por cobrar.

    `advertencias` (opcional): lista del llamador a la que además se le agregan los
    avisos, para los caminos que ya venían acumulando en una. Se loguea siempre a WARNING:
    si el consumidor de arriba no las pinta, quedan al menos en la traza del servidor."""
    nuevas: List[str] = []
    _aplicar_adelanto(db, cot, factura, usuario_id, advertencias=nuevas)
    if nuevas:
        logger.warning(
            "Adelanto aplicado con advertencias a la factura %s (venta %s): %s",
            factura.id, cot.id, " · ".join(nuevas))
        if advertencias is not None:
            advertencias.extend(nuevas)
    return nuevas


# ── Ventas (agrupado por cotización vendida/despachada) ────────────────────────
@router.get("/ventas")
def listar_ventas(
    q: Optional[str] = None,
    periodo: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Lista las ventas (cotizaciones vendidas/despachadas) con su resumen de cobranza.
    `q` busca en N° cotización / cliente / RUT / OC cliente; `periodo` filtra por fecha
    de venta (semana | mes | anio)."""
    base = (
        db.query(MonzaCotizacion)
        .options(selectinload(MonzaCotizacion.items), contains_eager(MonzaCotizacion.cliente))
        .outerjoin(MonzaCliente, MonzaCliente.id == MonzaCotizacion.cliente_id)
        .filter(MonzaCotizacion.estado.in_(ESTADOS_VENTA))
    )
    if q:
        like = f"%{q}%"
        base = base.filter(or_(
            MonzaCotizacion.numero.ilike(like), MonzaCotizacion.oc_cliente.ilike(like),
            MonzaCliente.nombre.ilike(like), MonzaCliente.rut.ilike(like),
        ))
    cots = base.order_by(MonzaCotizacion.id.desc()).all()
    # Facturas de TODAS las ventas en una sola query (evita N+1), con hijos eager.
    cot_ids = [c.id for c in cots]
    fac_by_cot: dict = {}
    if cot_ids:
        for f in (
            db.query(MonzaContFacturaCliente).options(*_FACTURA_EAGER)
            .filter(MonzaContFacturaCliente.cotizacion_id.in_(cot_ids)).all()
        ):
            fac_by_cot.setdefault(f.cotizacion_id, []).append(f)
    adel_by_cot = _adelantos_by_cot(db, cot_ids)
    # POR FACTURAR (base física, Fase 4): qty facturada por (venta, ítem) en UNA query
    # para todo el listado — jamás _qty_facturada_por_item por venta dentro del loop
    # (anti-muro). cfg se lee UNA vez fuera del loop (iva_rate_de cae a él si la venta
    # no congeló iva_pct).
    # La MISMA query trae además los descuentos de anticipo ya aplicados (líneas
    # negativas con anticipo_factura_id): dos datos de la misma tabla en un solo viaje,
    # jamás una consulta por venta (espejo de routers/contabilidad.py:797-819).
    cfg = _config(db)
    qty_fact_global: dict = {}
    desc_por_anticipo: dict = {}   # factura de anticipo -> neto ya descontado
    if cot_ids:
        for cot_id_row, iid, qty, ant_id, tot_neto in (
            db.query(MonzaContFacturaCliente.cotizacion_id,
                     MonzaContFacturaClienteItem.item_cotizacion_id,
                     MonzaContFacturaClienteItem.cantidad,
                     MonzaContFacturaClienteItem.anticipo_factura_id,
                     MonzaContFacturaClienteItem.total_neto)
            .join(MonzaContFacturaClienteItem,
                  MonzaContFacturaClienteItem.factura_id == MonzaContFacturaCliente.id)
            .filter(MonzaContFacturaCliente.cotizacion_id.in_(cot_ids))
            .all()
        ):
            if iid is not None:
                qty_fact_global.setdefault(cot_id_row, {})
                qty_fact_global[cot_id_row][iid] = qty_fact_global[cot_id_row].get(iid, 0.0) + _f(qty)
            if ant_id is not None:
                desc_por_anticipo[ant_id] = desc_por_anticipo.get(ant_id, 0.0) + (-_f(tot_neto))
    result = []
    for cot in cots:
        fecha_ref = cot.fecha_venta or cot.fecha_creacion
        if not _periodo_filter(fecha_ref, periodo):
            continue
        cli = cot.cliente
        facturas = fac_by_cot.get(cot.id, [])
        mp = mercaderia_pendiente_bruto(
            cot.items, qty_fact_global.get(cot.id, {}), iva_rate_de(cot, cfg))
        # POR FACTURAR = mercadería físicamente pendiente − anticipo aún por descontar
        # (esa plata YA está facturada contra la mercadería que falta). La base sigue
        # siendo FÍSICA (regla de oro G15): jamás total_vivo − Σ brutos.
        anticipo_pend = _anticipo_por_descontar_bruto(facturas, desc_por_anticipo)
        resumen = _resumen_cobranza(facturas, por_facturar_clp=mp - anticipo_pend)
        # Publicados también en el LISTADO (Grupo AM solo los da en el detalle): la
        # pantalla de Ventas de Monza ya los lee, y el frontend NO debe reconstruir el
        # pendiente como por_facturar + anticipo (con anticipo > mercadería el clamp
        # a 0 lo sobredeclararía).
        resumen["anticipo_por_descontar_clp"] = round(anticipo_pend, 0)
        resumen["mercaderia_pendiente_clp"] = round(max(mp, 0), 0)
        result.append({
            "cotizacion_id": cot.id,
            "numero_cotizacion": cot.numero,
            "cliente": (cli.nombre if cli else "") or "",
            "rut_cliente": (cli.rut if cli else "") or "",
            "oc_cliente": cot.oc_cliente,
            "vehiculo": cot.vehiculo,
            "estado": cot.estado,
            "fecha_venta": cot.fecha_venta.isoformat() if cot.fecha_venta else None,
            "fecha_creacion": cot.fecha_creacion.isoformat() if cot.fecha_creacion else None,
            "cond_pago": cot.forma_pago,
            "total_items": len(cot.items),
            "total_neto_clp": round(_f(cot.total_neto), 0),
            "iva_clp": round(_f(cot.iva_monto), 0),
            "total_con_iva_clp": round(_f(cot.total_bruto), 0),
            **resumen,
            # facturas_venta: de ahí sale el folio de la factura de anticipo (derivado,
            # sin query extra — ya están cargadas en fac_by_cot).
            **estado_adelanto(cot, adel_by_cot.get(cot.id), facturas_venta=facturas),
        })
    return result


@router.get("/ventas/{cot_id}")
def detalle_venta(
    cot_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Detalle de una venta: por cada ítem su precio de venta neto, las guías de
    despacho (con estado/firma) y las facturas asociadas; más las facturas serializadas
    y el resumen de cobranza. 404 si la cotización no existe."""
    cot = (
        db.query(MonzaCotizacion)
        .options(selectinload(MonzaCotizacion.items), joinedload(MonzaCotizacion.cliente))
        .filter(MonzaCotizacion.id == cot_id)
        .first()
    )
    if not cot:
        raise HTTPException(404, "Venta (cotización) no encontrada")
    cli = cot.cliente

    # ítem -> guías (despachos no anulados de la cotización)
    desp_rows = (
        db.query(MonzaDespachoItem, MonzaDespacho)
        .filter(
            MonzaDespacho.id == MonzaDespachoItem.despacho_id,
            MonzaDespacho.cotizacion_id == cot.id,
            MonzaDespacho.estado != "anulado",
        )
        .all()
    )
    guias_por_item = {}
    for di, d in desp_rows:
        guias_por_item.setdefault(di.item_id, []).append({
            "despacho_item_id": di.id, "despacho_id": d.id,
            "numero_despacho": d.numero, "numero_guia": d.numero_guia,
            "estado": d.estado, "qty_despachada": _f(di.qty_despachada),
            # SOLO LECTURA aquí: la firma se marca en Despachos (con foto/PDF + fecha),
            # no desde Ventas — regla 2026-08-06, el chip de marcado se eliminó.
            "guia_firmada": bool(getattr(d, "guia_firmada", 0)),
            "fecha_firma": d.fecha_firma.isoformat() if getattr(d, "fecha_firma", None) else None,
            "guia_firmada_archivo": getattr(d, "guia_firmada_archivo", None),
        })

    # ítem -> facturas
    fac_rows = (
        db.query(MonzaContFacturaClienteItem, MonzaContFacturaCliente)
        .filter(
            MonzaContFacturaCliente.id == MonzaContFacturaClienteItem.factura_id,
            MonzaContFacturaCliente.cotizacion_id == cot.id,
        )
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
    for it in cot.items:
        items_out.append({
            "id": it.id,
            "numero_parte": it.numero_parte or "",
            "descripcion": it.descripcion or "",
            "marca": it.marca or "",
            "cantidad": _f(it.cantidad),
            "precio_unit_venta_clp": round(_f(it.precio_unitario_clp), 0),
            "total_venta_clp": round(_f(it.subtotal_clp), 0),
            "estado_linea": it.estado_linea or "cotizado",
            "guias": guias_por_item.get(it.id, []),
            "facturas": facturas_por_item.get(it.id, []),
        })

    facturas = _facturas_de_cot(db, cot.id)
    _gv = _guias_vivas(db, facturas)
    # Adelanto VIGENTE de la venta: se lee UNA vez y se usa en dos lugares —marcar la
    # cobranza medio='adelanto' de cada factura (adelanto_id) y el bloque estado_adelanto
    # del final—, en vez de consultarlo dos veces.
    _adel_venta = _adelanto_de_cot(db, cot.id)
    _adel_id = _adel_venta.id if _adel_venta is not None else None
    # POR FACTURAR con base física (regla de oro G15): qty sin facturar × precio
    # congelado del ítem — nunca total-vivo − brutos-congelados.
    mp = mercaderia_pendiente_bruto(
        cot.items, _qty_facturada_por_item(db, cot.id), iva_rate_de(cot, _config(db)))
    # Anticipo (vía B) aún NO descontado en facturas del despacho real, en BRUTO: es lo
    # que explica la diferencia entre "mercadería sin facturar" y por_facturar_clp en la
    # pantalla — esa plata ya se facturó por adelantado contra la mercadería que falta.
    anticipo_por_descontar = sum(
        pend_neto * ((_f(fa.monto_bruto) / _f(fa.monto_neto))
                     if _f(fa.monto_neto) > TOL else 1.0)
        for fa, pend_neto in _anticipos_pendientes_de_descuento(db, cot.id)
    )
    # POR FACTURAR = mercadería FÍSICAMENTE pendiente − anticipo por descontar (con
    # clamp a 0 dentro de _resumen_cobranza). REGLA DE ORO G15 intacta: la base es la
    # cantidad sin facturar × precio congelado, jamás total_vivo − Σ brutos.
    resumen = _resumen_cobranza(facturas, por_facturar_clp=mp - anticipo_por_descontar)
    resumen["anticipo_por_descontar_clp"] = round(anticipo_por_descontar, 0)
    # Cifra AUTORITATIVA de mercadería pendiente: la calcula el backend — el frontend NO
    # debe reconstruirla como por_facturar + anticipo (con anticipo > mercadería el
    # clamp la sobredeclararía). Espejo del contrato G15 de GA.
    resumen["mercaderia_pendiente_clp"] = round(max(mp, 0), 0)
    return {
        "cotizacion_id": cot.id,
        "numero_cotizacion": cot.numero,
        "cliente": (cli.nombre if cli else "") or "",
        "rut_cliente": (cli.rut if cli else "") or "",
        "oc_cliente": cot.oc_cliente,
        "vehiculo": cot.vehiculo,
        "cond_pago": cot.forma_pago,
        "fecha_entrega_est": cot.fecha_entrega_est.isoformat() if cot.fecha_entrega_est else None,
        "total_neto_clp": round(_f(cot.total_neto), 0),
        "iva_clp": round(_f(cot.iva_monto), 0),
        "total_con_iva_clp": round(_f(cot.total_bruto), 0),
        "items": items_out,
        # N° de guía VIVO (batch, 1 query): ver _guias_vivas.
        "facturas": [_serialize_factura(f, guia_viva=_gv.get(f.id), adelanto_id=_adel_id)
                     for f in facturas],
        "resumen": resumen,
        # facturas_venta: el folio del respaldo (vía B) sale DERIVADO de las facturas
        # es_anticipo=1 que ya se cargaron aquí arriba — sin query ni columna nuevas.
        **estado_adelanto(cot, _adel_venta, facturas_venta=facturas),
    }


@router.get("/ventas/{cot_id}/despachos-facturables")
def despachos_facturables(
    cot_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Guías de despacho FACTURABLES: despachos 'despachado' con saldo aún facturable.
    Alimenta el selector del modal 'Emitir factura'. Incluye el estado de firma (opcional)."""
    if not db.query(MonzaCotizacion.id).filter(MonzaCotizacion.id == cot_id).first():
        raise HTTPException(404, "Venta (cotización) no encontrada")
    fact_di = _qty_facturada_por_despacho_item(db, cot_id)
    by_desp = {}
    for di, d in _despacho_items_de_cot(db, cot_id):
        facturable = _f(di.qty_despachada) - fact_di.get(di.id, 0.0)
        e = by_desp.setdefault(d.id, {
            "id": d.id, "numero_despacho": d.numero,
            "numero_guia": d.numero_guia,
            # Fecha de EMISIÓN de la guía: la referencia 52 del DTE 33 la exige, así que
            # una guía en papel sin ella NO se puede facturar al SII. Viaja hasta acá para
            # que el selector lo avise ANTES de elegirla — quien factura no es quien la
            # carga en Bodega, y si no se ve acá el bloqueo sorprende al final.
            "fecha_guia": d.fecha_guia.isoformat() if d.fecha_guia else None,
            # AUDITORÍA (hallazgo MEDIUM de la guía 52 en vuelo): ADVERTENCIA visual, no
            # un bloqueo — el N° que se ve arriba todavía puede ser el tecleado a mano y
            # el folio real del SII está por llegar y lo va a pisar. La vía SII sí frena;
            # la manual solo avisa, para no divergir de Grupo AM.
            "guia_sii_en_proceso": _guia_sii_en_proceso(db, d.id),
            # REGLA 2026-08-06: sin firma NO se factura. La guía se lista IGUAL (con el
            # flag en falso) para que el selector la muestre deshabilitada con el motivo
            # — ocultarla mandaba al operador a buscar una guía "desaparecida".
            "guia_firmada": bool(getattr(d, "guia_firmada", 0)),
            "fecha_firma": d.fecha_firma.isoformat() if getattr(d, "fecha_firma", None) else None,
            "guia_firmada_archivo": getattr(d, "guia_firmada_archivo", None),
            "items_count": 0, "facturable": 0.0,
        })
        e["items_count"] += 1
        e["facturable"] += max(facturable, 0.0)
    return [e for e in by_desp.values() if e["facturable"] > TOL_QTY]


# El PATCH /ventas/despachos/{id}/guia-firmada se ELIMINÓ (2026-08-06): era un toggle
# sin validaciones ("informativo") y la firma ahora GATEA la facturación. Marcar la
# firma vive donde ocurre la entrega — Despachos (monza_router_despachos.py:
# POST /entidades/{id}/firmar), que exige despacho cerrado + foto/PDF + fecha, con
# des-firmar prohibido. Contabilidad la LEE (despachos-facturables, detalle de venta)
# y la EXIGE (_construir_factura); no la escribe.


@router.post("/ventas/{cot_id}/adelanto/verificar")
def verificar_adelanto(
    cot_id: int,
    payload: AdelantoVerificarIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Contabilidad VERIFICA el adelanto que Comercial informó al cerrar la venta:
    registra monto/fecha/banco/N° operación y marca la venta como adelanto verificado.
    El monto se aplicará como cobranza al emitir la(s) factura(s) de esta venta.
    Permite editar mientras no se haya aplicado a una factura."""
    # Lock de la cotización: serializa verificaciones concurrentes de la misma venta, de
    # modo que un doble envío actualice el mismo adelanto en vez de chocar con el UNIQUE.
    cot = (
        db.query(MonzaCotizacion)
        .filter(MonzaCotizacion.id == cot_id)
        .with_for_update(of=MonzaCotizacion)
        .first()
    )
    if not cot:
        raise HTTPException(404, "Venta (cotización) no encontrada")
    # Precondiciones de la venta y del monto: viven en validar_venta_para_adelanto, la
    # ÚNICA fuente de verdad que comparte con monza_tesoreria.aprobar_adelanto (deuda M5).
    # Ahí está también el porqué de que NO se exija pct_adelanto > 0 (M2, adelanto NO
    # PACTADO): sin eso, la plata de un depósito que nadie pactó quedaba en el banco sin
    # destino hasta que Comercial hiciera un PATCH a la cotización.
    validar_venta_para_adelanto(cot, payload.monto, verbo="verificar")
    # lock=True (espejo GA tesoreria/router.py aprobar_adelanto): los guards de
    # aplicado/conciliado deben decidir sobre la ÚLTIMA versión commiteada de la fila
    # — sin el lock, un conciliar concurrente en Tesorería (que sí bloquea la fila)
    # podía colarse entre la lectura y la escritura (TOCTOU). El orden
    # cotización → adelanto no crea deadlock: conciliar toma movimiento → adelanto,
    # nunca la cotización.
    # incluir_anulado=True: si el adelanto se ANULÓ y el cliente sí depositó (o la
    # anulación fue el error), esa MISMA fila se REUSA — el UNIQUE por cotización no
    # admite crear otra, así que sin esto el upsert de abajo chocaría con
    # uq_monza_cont_adelanto_cotizacion y saldría un 500. La reactivación explícita está
    # más abajo (reactivar_adelanto), justo antes de escribir el monto.
    adel = _adelanto_de_cot(db, cot_id, lock=True, incluir_anulado=True)
    # Los 2 candados de plata sobre la fila (aplicado / conciliado con el banco) también
    # son la MISMA regla que Tesorería: viven en validar_adelanto_editable (deuda M5).
    validar_adelanto_editable(db, adel)
    if adel is None:
        adel = MonzaContAdelanto(cotizacion_id=cot_id)
        db.add(adel)
    # Re-verificar un adelanto ANULADO lo devuelve a la vida (ver reactivar_adelanto):
    # sin esta línea la fila quedaría 'anulada' con plata dentro — invisible para todo lo
    # que filtra anulados— mientras cot.adelanto_verificado pasa a 1. Se deja traza: es
    # una vuelta atrás sobre una decisión de plata.
    if reactivar_adelanto(adel):
        logger.info("Adelanto %s de la venta %s REACTIVADO (estaba anulado)", adel.id, cot_id)
    adel.monto = payload.monto
    # Fecha ESTRICTA (espejo GA): una fecha explícita mal escrita responde 400 en vez
    # de caer en silencio a hoy. Sin fecha en el payload, hoy sigue siendo el default.
    adel.fecha_pago = _fecha_estricta(payload.fecha_pago, "fecha_pago") or date.today()
    adel.banco = payload.banco
    adel.numero_operacion = payload.numero_operacion
    # Solo si vino en el payload (espejo GA tesoreria/router.py): re-verificar sin
    # observaciones NO debe pisar con None lo que el operador ya había anotado.
    if payload.observaciones is not None:
        adel.observaciones = payload.observaciones
    adel.usuario_id = getattr(current_user, "id", None)
    cot.adelanto_verificado = 1
    db.flush()
    # Aplicación RETROACTIVA (espejo de tesoreria/router.py aprobar_adelanto de GA):
    # si la venta ya tiene facturas emitidas con saldo, el adelanto verificado se
    # aplica de inmediato como cobranza medio='adelanto'; si la factura viene después,
    # la aplica crear_factura — ambas direcciones cubiertas. Lock por factura:
    # serializa contra cobranzas concurrentes (orden global cotización → factura →
    # adelanto; _aplicar_adelanto capea por SALDO y salta factoring vigente).
    # Facturas de ANTICIPO (vía B) PRIMERO y recién después el FIFO por id (gemela de
    # monza_tesoreria._aplicar_adelanto_a_facturas: mantener ambas en sync). La factura
    # de anticipo es la que RECIBE la plata del adelanto —la del despacho real lleva la
    # línea de DESCUENTO, no una cobranza—, así que saldarla en esta misma pasada deja
    # el EXCEDENTE (adelanto mayor que el bruto del anticipo) libre para las facturas
    # del despacho real dentro de la MISMA transacción. Con el orden por id a secas ese
    # excedente quedaba atrapado si el anticipo se emitía después, y la deuda del
    # cliente quedaba sobrestimada hasta que alguien la aplicara a mano. El lock de la
    # cotización ya tomado serializa la transacción: cambiar el orden de los locks por
    # factura no introduce deadlocks nuevos.
    # COALESCE: en DESC MySQL manda los NULL al FINAL, así que una fila legada con
    # es_anticipo NULL (tabla creada por create_all antes del server_default) perdería
    # contra un 0 y el orden quedaría invertido — la plata entraría a la factura
    # equivocada. El normalizador de init_db deja 0 y el ORM siempre escribe 0: esto es
    # cinturón y tirantes. Mantener en sync con monza_tesoreria._aplicar_adelanto_a_facturas.
    facturas = (db.query(MonzaContFacturaCliente)
                .filter(MonzaContFacturaCliente.cotizacion_id == cot.id)
                .order_by(func.coalesce(MonzaContFacturaCliente.es_anticipo, 0).desc(),
                          MonzaContFacturaCliente.id.asc())
                .populate_existing().with_for_update().all())
    for f in facturas:
        _aplicar_adelanto(db, cot, f, getattr(current_user, "id", None))
        db.flush()
        _recompute_factura(f, cobranzas=_cobranzas_bloqueadas(db, f.id))
    db.commit()
    db.refresh(cot)
    db.refresh(adel)
    # facturas_venta: publica el folio de la factura de anticipo que respalda este
    # adelanto (DERIVADO de las facturas es_anticipo=1). Se releen en UNA query en vez
    # de reusar la lista de arriba: el commit expiró esos objetos y tocarlos dispararía
    # un refresh por fila —y un ObjectDeletedError si alguien borró una factura apenas
    # se soltó el lock.
    return estado_adelanto(cot, adel, facturas_venta=(
        db.query(MonzaContFacturaCliente)
        .filter(MonzaContFacturaCliente.cotizacion_id == cot.id)
        .order_by(MonzaContFacturaCliente.id.asc()).all()))


@router.post("/adelantos/{adelanto_id}/anular")
def anular_adelanto(
    adelanto_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """ANULA un adelanto que no prosperó: el cliente nunca depositó, o se verificó por
    error. Espejo de routers/contabilidad.py de Grupo AM.

    Es la pieza que FALTABA en Monza: los dos únicos escritores del adelanto eran
    creación/upsert y `adelanto_verificado` solo se escribía = 1, así que un adelanto
    verificado por error quedaba PEGADO — Abastecimiento seguía comprando al proveedor
    contra un 50% inexistente y el 409 de monza_router_cotizaciones.py («Revierta el
    adelanto en Contabilidad/Tesorería primero») mandaba a un lugar que no existía. La
    única salida era re-verificar con monto $1 y borrar la cobranza a mano, o tocar la BD.

    Los DOS candados son los de Grupo AM, y en el mismo orden:
      · aplicado > TOL → 409: esa plata ya está dentro de una factura como cobranza
        'adelanto'; anular dejaría `monto_aplicado` apuntando a un adelanto muerto y
        rompería el invariante monto_aplicado == Σ cobranzas 'adelanto'. Primero se
        revierte la cobranza (DELETE .../cobranzas/{id}, que devuelve el monto).
      · conciliado con el banco → 409: hay un abono de la cartola cruzado con este
        adelanto; anularlo dejaría el movimiento bancario apuntando a nada. Se
        desconcilia en Tesorería primero.
    Y un TERCERO que Grupo AM no tiene: la factura de ANTICIPO que el adelanto respalda
    ante el SII (ver _bloqueo_anticipo_del_adelanto).
    Y anular es IDEMPOTENTE (espejo GA): re-anular no es un error, responde el estado.

    Lo que NO hace: borrar la fila. Se marca 'anulado' y se conserva para trazabilidad
    (queda quién y cuánto se había verificado); los caminos que suman/aplican adelantos la
    filtran (_adelanto_de_cot / _adelantos_by_cot / estado_adelanto). Si el cliente
    después sí deposita, la MISMA fila se reactiva al re-verificar — el UNIQUE por
    cotización no admite una segunda.

    OJO con eso último: como la fila SIGUE EXISTIENDO, anular NO destraba los dos 409 de
    monza_router_cotizaciones.py que deciden con `COUNT(*)` de adelantos de la venta (bajar
    pct_adelanto y des-cerrar la venta). Para esa corrección la reversión completa es
    DELETE /adelantos/{id} (eliminar_adelanto), que borra la fila con los mismos candados."""
    # ORDEN GLOBAL DE LOCKS: cotización → adelanto (el mismo de crear_factura,
    # verificar_adelanto y monza_tesoreria.aprobar_adelanto). Se toma primero la
    # cotización porque aquí se ESCRIBE `adelanto_verificado`; al revés se abriría un
    # ciclo con esos tres.
    ref = (db.query(MonzaContAdelanto.cotizacion_id)
           .filter(MonzaContAdelanto.id == adelanto_id).first())
    if not ref:
        raise HTTPException(404, "Adelanto no encontrado")
    cot = (db.query(MonzaCotizacion)
           .filter(MonzaCotizacion.id == ref[0])
           .populate_existing().with_for_update(of=MonzaCotizacion).first())
    # populate_existing + FOR UPDATE: los guards de abajo deciden sobre PLATA
    # (monto_aplicado) y cualquier lectura plana previa de la sesión los decidiría con la
    # versión del identity map. Ver docs/regla-lecturas-de-plata.md.
    adel = (db.query(MonzaContAdelanto)
            .filter(MonzaContAdelanto.id == adelanto_id)
            .populate_existing().with_for_update().first())
    if not adel:
        raise HTTPException(404, "Adelanto no encontrado")

    def _salida() -> dict:
        """Misma forma que verificar_adelanto (la pantalla lee un solo contrato) más la
        traza del registro anulado, que estado_adelanto ya no publica: un adelanto
        anulado se sirve como `adelanto: null` para que la venta vuelva a
        'por_verificar'."""
        facturas_venta = ([] if cot is None else
                          db.query(MonzaContFacturaCliente)
                          .filter(MonzaContFacturaCliente.cotizacion_id == cot.id)
                          .order_by(MonzaContFacturaCliente.id.asc()).all())
        out = estado_adelanto(cot, adel, facturas_venta=facturas_venta)
        out["adelanto_anulado"] = {
            "id": adel.id, "estado": estado_de_adelanto(adel),
            "monto": _f(adel.monto), "monto_aplicado": _f(adel.monto_aplicado),
        }
        return out

    if estado_de_adelanto(adel) == ADEL_ANULADO:
        return _salida()  # idempotente (espejo GA): re-anular no es un error
    if _f(adel.monto_aplicado) > TOL:
        raise HTTPException(
            409, f"El adelanto ya fue aplicado a una factura (aplicado "
                 f"{_f(adel.monto_aplicado):.0f}); revierta esa cobranza antes de anularlo")
    # 'conciliado' se DERIVA de la existencia del enlace de Tesorería (no hay columna),
    # igual que en verificar_adelanto — mantener ambos textos en sync.
    if db.query(MonzaTesConciliacion.id).filter(
            MonzaTesConciliacion.adelanto_id == adel.id).first():
        raise HTTPException(409, "El adelanto está conciliado con un abono del banco; "
                                 "desconcílielo en Tesorería antes de anularlo")
    # TERCER candado (no lo tiene Grupo AM todavía): la factura de ANTICIPO que este
    # adelanto respalda ante el SII. Los dos de arriba no miran el documento tributario, así
    # que obedecer el 409 «revierta esa cobranza» era el camino para dejar un DTE de
    # anticipo sin respaldo. Ver _bloqueo_anticipo_del_adelanto.
    _bloqueo_anticipo_del_adelanto(db, ref[0], accion="anular el adelanto")
    adel.estado = ADEL_ANULADO
    # CIERRA EL CORTAFUEGO de Abastecimiento: monza_router_abastecimiento.py solo frena la
    # OC de proveedor cuando adelanto_verificado == 0. Sin esta línea el adelanto quedaba
    # anulado y la compra seguía autorizada — el agujero exacto que motivó este endpoint.
    if cot is not None:
        cot.adelanto_verificado = 0
    db.commit()
    db.refresh(adel)
    if cot is not None:
        db.refresh(cot)
    logger.info("Adelanto %s ANULADO (venta %s) por usuario %s",
                adel.id, ref[0], getattr(current_user, "id", None))
    return _salida()


@router.delete("/adelantos/{adelanto_id}")
def eliminar_adelanto(
    adelanto_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """ELIMINA el registro del adelanto (el que nunca debió existir). Es la REVERSIÓN
    completa, y sin ella los 409 de Ventas mandan a una acción imposible.

    Por qué existe además de `anular` (no es un duplicado, es el otro extremo):
      · `anular` deja la fila marcada 'anulado' para trazabilidad (espejo de Grupo AM) y
        cierra el cortafuego de Abastecimiento. Perfecto mientras la venta siga su curso.
      · Pero la fila SIGUE EXISTIENDO, y monza_router_cotizaciones.py decide con
        `COUNT(*)` de adelantos de la venta: por eso, después de anular, sus dos 409
        —«Revierta el adelanto en Contabilidad/Tesorería primero» al bajar pct_adelanto y
        «(… 1 adelanto(s) …) Anula/elimina eso primero» al des-cerrar la venta— seguían
        saliendo IDÉNTICOS para siempre. El operador obedecía la instrucción y nada
        cambiaba. Este endpoint es la mitad «elimina» que esos mensajes ya prometen: al
        borrarse la fila, el COUNT vuelve a 0 y la corrección se puede hacer de verdad.

    Es la convención de la casa para un registro de plata SIN huella contable, la misma de
    `eliminar_cobranza`, `eliminar_pago` y `eliminar_factura`: se borra, con candados que
    verifican que no queda nada apuntando a él. Los TRES son los de `anular` (aplicado a
    una factura / conciliado con el banco / respalda una factura de anticipo), y bastan
    porque el único enlace real a esta fila es monza_tes_conciliacion.adelanto_id — que
    además es ON DELETE CASCADE, así que sin el candado el motor borraría el cruce bancario
    en silencio. Se leen todos BAJO LOCK (mismo orden que `anular`).

    Idempotente por HTTP: si ya no existe, 404 (el recurso no está); ese es el contrato de
    un DELETE y lo que la pantalla necesita para no dejar el botón colgado."""
    # ORDEN GLOBAL DE LOCKS idéntico a anular_adelanto: cotización → adelanto (aquí también
    # se ESCRIBE `adelanto_verificado`). No introduce ciclos con conciliar (movimiento →
    # adelanto) ni con crear_factura (cotización → factura → adelanto).
    ref = (db.query(MonzaContAdelanto.cotizacion_id)
           .filter(MonzaContAdelanto.id == adelanto_id).first())
    if not ref:
        raise HTTPException(404, "Adelanto no encontrado")
    cot = (db.query(MonzaCotizacion)
           .filter(MonzaCotizacion.id == ref[0])
           .populate_existing().with_for_update(of=MonzaCotizacion).first())
    adel = (db.query(MonzaContAdelanto)
            .filter(MonzaContAdelanto.id == adelanto_id)
            .populate_existing().with_for_update().first())
    if not adel:
        raise HTTPException(404, "Adelanto no encontrado")
    if _f(adel.monto_aplicado) > TOL:
        raise HTTPException(
            409, f"El adelanto ya fue aplicado a una factura (aplicado "
                 f"{_f(adel.monto_aplicado):.0f}); revierta esa cobranza antes de eliminarlo")
    if db.query(MonzaTesConciliacion.id).filter(
            MonzaTesConciliacion.adelanto_id == adel.id).first():
        raise HTTPException(409, "El adelanto está conciliado con un abono del banco; "
                                 "desconcílielo en Tesorería antes de eliminarlo")
    _bloqueo_anticipo_del_adelanto(db, ref[0], accion="eliminar el adelanto")
    # Traza ANTES del borrado (después la fila no existe) y el cortafuego de Abastecimiento
    # cerrado en la MISMA transacción: si el adelanto se va, `adelanto_verificado` no puede
    # quedar en 1 (dejaría la OC de proveedor autorizada contra plata que ya no está
    # registrada). Esto además REPARA la fila que quedó 'anulado' con el flag en 1.
    traza = {"id": adel.id, "estado": estado_de_adelanto(adel),
             "monto": _f(adel.monto), "monto_aplicado": _f(adel.monto_aplicado),
             "banco": adel.banco, "numero_operacion": adel.numero_operacion}
    if cot is not None:
        cot.adelanto_verificado = 0
    db.delete(adel)
    db.commit()
    logger.info("Adelanto %s ELIMINADO (venta %s, monto %s) por usuario %s",
                traza["id"], ref[0], traza["monto"], getattr(current_user, "id", None))
    if cot is not None:
        db.refresh(cot)
    # Mismo contrato de salida que anular/verificar (la pantalla lee uno solo), con la
    # venta ya de vuelta en 'por_verificar' porque el adelanto no existe.
    facturas_venta = ([] if cot is None else
                      db.query(MonzaContFacturaCliente)
                      .filter(MonzaContFacturaCliente.cotizacion_id == cot.id)
                      .order_by(MonzaContFacturaCliente.id.asc()).all())
    return {"ok": True, **estado_adelanto(cot, None, facturas_venta=facturas_venta),
            "adelanto_eliminado": traza}


# ── Facturas / Cobranzas / Factoring ───────────────────────────────────────────
@router.get("/facturas")
def listar_facturas(
    estado: Optional[str] = None,
    q: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Lista las facturas (filtrables por estado_pago y texto `q`) y la ANTIGÜEDAD de
    cartera: saldo por cobrar en 0-30 / 31-60 / 61-90 / 91+ días desde la emisión."""
    base = db.query(MonzaContFacturaCliente).options(*_FACTURA_EAGER)
    if q:
        like = f"%{q}%"
        base = base.filter(or_(
            MonzaContFacturaCliente.cliente_nombre.ilike(like),
            MonzaContFacturaCliente.rut_cliente.ilike(like),
            MonzaContFacturaCliente.numero_factura.ilike(like),
            MonzaContFacturaCliente.numero_cotizacion.ilike(like),
        ))
    facturas = base.order_by(MonzaContFacturaCliente.id.desc()).all()
    # N° de guía VIVO de todo el listado en UNA query (jamás por factura dentro del loop).
    gv = _guias_vivas(db, facturas)
    # Adelanto de cada factura, también en UNA query (ver _adelanto_ids_by_factura).
    aid = _adelanto_ids_by_factura(db, facturas)
    out = []
    aging = {"0_30": 0.0, "31_60": 0.0, "61_90": 0.0, "91_mas": 0.0}
    hoy = date.today()
    for f in facturas:
        d = _serialize_factura(f, guia_viva=gv.get(f.id), adelanto_id=aid.get(f.id))
        # filtra por el estado EN VIVO del serializador (no el persistido, que puede
        # estar obsoleto para 'vencida')
        if estado and d["estado_pago"] != estado:
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


class _ProblemasFactura:
    """Recolector de los problemas de negocio de una factura, con DOS modalidades.

    El porqué de las dos: la vía MANUAL (POST /facturas) debe seguir respondiendo
    exactamente el mismo código HTTP y el mismo mensaje que antes de partir el endpoint
    en piezas (400 para datos faltantes/inválidos, 409 para topes y duplicados) — hay
    suites que lo verifican caso a caso. La vía ELECTRÓNICA (preview/emitir al SII)
    necesita en cambio la LISTA COMPLETA de problemas para mostrárselos al usuario
    ANTES de una emisión irreversible. Una sola implementación de las reglas, dos
    formas de salir:
      · `acumular=False` (manual): el primer problema lanza el HTTPException de siempre.
      · `acumular=True`  (SII): se juntan todos y el llamador decide qué hacer.
    """

    def __init__(self, acumular: bool = False):
        self.acumular = acumular
        self.items: List[str] = []
        # Código del PRIMER problema: es el que la vía manual habría respondido.
        self.status: Optional[int] = None

    def add(self, status: int, mensaje: str) -> None:
        if not self.acumular:
            raise HTTPException(status, mensaje)
        if self.status is None:
            self.status = status
        self.items.append(mensaje)


# ── Cortafuego de ADELANTO SIN VERIFICAR en las puertas de SALIDA (2026-08-22) ──
# El predicado es el MISMO que frena la OC de proveedor en Abastecimiento
# (monza_router_abastecimiento.py): una venta que exige adelanto (pct_adelanto > 0,
# incluido el Contado = 100%) y cuyo pago Tesorería todavía no verificó.
#
# POR QUÉ TAMBIÉN ACÁ: hasta ahora solo se frenaba la COMPRA, y el resto quedaba
# cubierto DE REBOTE por el camino físico (sin compra no hay mercadería que despachar).
# Pero mercadería que llega a bodega por otra vía —una reposición, el remanente de otra
# línea— podía salir despachada, con guía al SII y facturada, con el pago pendiente.
# Se replica el helper en cada módulo (patrón ESTADOS_VENTA de la casa) en vez de
# importarlo: acoplar los módulos aislados por un predicado de 3 líneas sale más caro
# que mantener las copias con este comentario.
def _adelanto_sin_verificar(cot) -> bool:
    return (int(getattr(cot, "pct_adelanto", 0) or 0) > 0
            and not int(getattr(cot, "adelanto_verificado", 0) or 0))


def _cargar_venta(db: Session, cotizacion_id: int, lock: bool = True) -> MonzaCotizacion:
    """Venta (cotización) facturable. Con `lock=True` la lee BLOQUEANTE: serializa la
    facturación concurrente de la misma venta (dos requests no pueden leer el mismo
    "ya facturado" y sobre-facturar). Es el PRIMER eslabón del orden global de locks
    de la casa: cotización → factura → adelanto.

    `populate_existing()` no es adorno: si la fila ya está en el identity map de la
    sesión (la emisión electrónica valida ANTES de bloquear), SQLAlchemy descartaría
    los valores frescos del SELECT ... FOR UPDATE y se decidiría con datos viejos.
    Ver docs/regla-lecturas-de-plata.md."""
    q = (
        db.query(MonzaCotizacion)
        .options(selectinload(MonzaCotizacion.items))  # evita lazy-load de items dentro del lock
        .filter(MonzaCotizacion.id == cotizacion_id)
    )
    if lock:
        q = q.populate_existing().with_for_update(of=MonzaCotizacion)
    cot = q.first()
    if not cot:
        raise HTTPException(404, "Cotización (venta) no encontrada")
    if cot.estado not in ESTADOS_VENTA:
        raise HTTPException(400, "La cotización no está vendida; no se puede facturar")
    return cot


def _validar_receptor_factura(cot: MonzaCotizacion, tipo_doc: str,
                              probs: _ProblemasFactura) -> dict:
    """Receptor válido para FACTURA (Fase 3, espejo GA): el SII rechaza una factura con
    RUT malo o sin razón social — mejor frenarla aquí que tras contabilizarla. Una
    boleta no exige RUT del receptor.

    `cot.cliente` es una RELACIÓN a MonzaCliente (no un string como en GA): la razón
    social sale de `cliente.nombre`."""
    cli = cot.cliente
    if tipo_doc == "factura":
        if not cli or not (cli.nombre or "").strip():
            probs.add(400, "La venta no tiene cliente con razón social: complétala en la ficha del cliente antes de facturar")
        elif not rut_valido(cli.rut):
            mostrado = rut_saneado(cli.rut) or "vacío"
            probs.add(400, f"RUT del cliente inválido o faltante ({mostrado}): corrígelo en la ficha del cliente antes de facturar")
    return {
        "razon_social": (cli.nombre if cli else None),
        "rut": (cli.rut if cli else None),
    }


def _construir_factura(db: Session, payload: FacturaCreate, cot: MonzaCotizacion,
                       *, acumular: bool = False) -> dict:
    """Valida y CONSTRUYE los datos de la factura sin escribir nada: receptor, líneas
    derivadas según el modo, doble tope por ítem/guía, tope Σ brutos ≤ total de la venta
    y montos congelados (neto/IVA/bruto). Devuelve lo que `_persistir_factura` necesita
    más lo que el preview muestra.

    Es la ÚNICA fuente de verdad de las reglas de facturación: la vía manual y la
    emisión electrónica al SII pasan por aquí, de modo que un arreglo en una no deja a
    la otra sobre-facturando en silencio.

    Deliberadamente NO incluye el guard del FOLIO obligatorio ni el de folio duplicado:
    la vía SII persiste a propósito con `numero_factura` NULL (el folio lo asigna el
    SII al emitir), así que esas dos validaciones viven en el endpoint manual.

    Llamar con la cotización ya bloqueada cuando el resultado vaya a persistirse: los
    topes se calculan contra lo ya facturado y ese conteo no debe moverse."""
    probs = _ProblemasFactura(acumular)
    advertencias: List[str] = []
    tipo_doc = payload.tipo_doc or "factura"

    receptor = _validar_receptor_factura(cot, tipo_doc, probs)

    # 'Retiro en oficina' (sin_guia) factura el SALDO de la venta: es EXCLUYENTE con
    # despacho e ítems explícitos (evita estados ambiguos / modos mezclados).
    # ── Cortafuego de adelanto SIN VERIFICAR — solo en el canal RETIRO ────────
    # POR QUÉ SOLO ACÁ y no en toda factura: facturar antes de cobrar es el flujo
    # NORMAL y diseñado del canal con guía (el cliente necesita la factura para
    # pagar, y Tesorería aplica el adelanto retroactivamente cuando el depósito
    # llega — ver monza_tests/test_viaje_de_la_plata). Bloquear eso sería circular.
    # El RETIRO EN OFICINA es distinto: no hay despacho ni guía que frenar porque la
    # mercadería sale del mostrador EN ESE ACTO, así que esta factura es su única
    # puerta. Sin este guard, marcar «retiro» era el bypass de los otros dos
    # cortafuegos (la lección del gate de la guía firmada: el canal sin_guia es la
    # puerta de servicio).
    if (payload.sin_guia and _adelanto_sin_verificar(cot)
            and not getattr(payload, "confirmar_retiro_sin_adelanto", False)):
        raise HTTPException(
            409,
            f"Adelanto no verificado por Tesorería en {cot.numero} "
            f"(adelanto {int(cot.pct_adelanto or 0)}%): no se entrega mercadería en "
            f"retiro con el pago pendiente. Tesorería debe registrar el pago recibido; "
            f"si el cliente acaba de pagar y hay respaldo, marca «retirar sin esperar la "
            f"verificación» para dejarlo registrado.",
        )
    modo_ambiguo = payload.sin_guia and (payload.despacho_id is not None or payload.items is not None)
    if modo_ambiguo:
        probs.add(400, "Retiro en oficina (sin guía) factura el saldo de la venta: no indique despacho ni ítems")

    items_by_id = {i.id: i for i in cot.items}

    # Despachos 'despachado' de la cotización (lo que se puede facturar)
    desp_items = _despacho_items_de_cot(db, cot.id)
    di_by_id = {di.id: di for di, _d in desp_items}
    desp_by_id = {d.id: d for _di, d in desp_items}
    desp_qty_item = _qty_despachada_por_item(db, cot.id)
    fact_qty_item = _qty_facturada_por_item(db, cot.id)
    fact_qty_di = _qty_facturada_por_despacho_item(db, cot.id)

    # ── REGLA 2026-08-06 · guía FIRMADA obligatoria (paridad MachParts) ────────
    # El flujo CON guía solo factura despachos cuya guía esté FIRMADA por el cliente
    # (guia_firmada==1, la marca Despachos con foto/PDF + fecha). Derivados:
    #   · desp_qty_item_firmada: tope agregado por ítem del flujo con guía — contar
    #     ahí un despacho SIN firmar dejaría colar sus cantidades por la vía de
    #     ítems explícitos sin despacho_item_id.
    #   · fact_retiro_item / fact_guia_item: lo facturado partido POR CANAL (la
    #     columna factura.sin_guia). El canal es lo que evita el DOBLE descuento
    #     entre guía y retiro (hallazgo HIGH del multienjambre 2026-08-07: retiro
    #     primero + guía después dejaba las unidades de la guía infacturables,
    #     porque el tope de la guía restaba también lo facturado por retiro).
    #   · pendiente_guias_item: lo COMPROMETIDO en despachos vivos que el canal
    #     guía aún no factura. Es lo que el RETIRO EN OFICINA no puede tocar: esa
    #     mercadería sale (o salió) con guía y se factura POR su guía firmada. Sin
    #     este descuento, bastaba marcar "retiro" para facturar una guía jamás
    #     firmada — el bypass exacto que la regla cierra.
    # Lo YA facturado no se re-litiga: el gate aplica hacia adelante (las facturas
    # históricas pre-candado quedan como están; los topes usan max(0, ...) para que
    # un legado sobre-facturado no descuadre el cálculo).
    desp_qty_item_firmada = {}
    for di, d in desp_items:
        if getattr(d, "guia_firmada", 0):
            desp_qty_item_firmada[di.item_id] = (
                desp_qty_item_firmada.get(di.item_id, 0.0) + _f(di.qty_despachada)
            )
    fact_retiro_item = _qty_facturada_retiro_por_item(db, cot.id)
    # Consumo atribuible al canal GUÍA = todo lo facturado que no fue retiro (líneas
    # ligadas a despacho_item + sueltas validadas contra el tope firmado).
    fact_guia_item = {
        iid: max(0.0, qty - fact_retiro_item.get(iid, 0.0))
        for iid, qty in fact_qty_item.items()
    }
    comprometida_viva = _qty_comprometida_en_despachos_por_item(db, cot.id)
    pendiente_guias_item = {
        iid: max(0.0, qty - fact_guia_item.get(iid, 0.0))
        for iid, qty in comprometida_viva.items()
    }

    # Determinar líneas a facturar
    lineas: List[FacturaItemIn] = []
    desp = None
    if modo_ambiguo:
        pass  # modo indefinido: no hay de dónde derivar (solo se llega aquí acumulando)
    elif payload.items:
        lineas = list(payload.items)
        # B8 — la GUÍA se valida SIEMPRE, también con ítems explícitos (paridad con Grupo
        # AM, routers/contabilidad.py: «La guía (despacho_id) se valida SIEMPRE»). Esta
        # cadena `elif` NUNCA miraba `payload.despacho_id` cuando venían ítems, así que un
        # despacho de OTRA venta —o inexistente, o todavía sin despachar— se aceptaba en
        # SILENCIO: el operador elegía una guía en el modal y la factura terminaba ligada
        # a la que derivan las líneas, sin que nada avisara del desajuste.
        # Mismos códigos y mensajes que el modo despacho (un solo texto por error).
        # El snapshot de guía se sigue DERIVANDO de las líneas (snap_desp_id, más abajo):
        # cambiarlo aquí movería la referencia 52 del DTE, que no es lo que este guard
        # arregla — lo que se cierra es aceptar un despacho ajeno sin decir nada.
        if payload.despacho_id is not None:
            desp_sel = db.query(MonzaDespacho).filter(
                MonzaDespacho.id == payload.despacho_id,
                MonzaDespacho.cotizacion_id == cot.id,
            ).first()
            if not desp_sel:
                probs.add(404, "Despacho no encontrado para esta venta")
            elif desp_sel.estado != "despachado":
                probs.add(400, "Solo se puede facturar una guía en estado 'despachado'")
            elif not getattr(desp_sel, "guia_firmada", 0):
                # Mismo texto que el modo despacho (un solo mensaje por regla).
                probs.add(400, "La guía de este despacho no está FIRMADA por el cliente: "
                               "márcala en Despachos (subiendo la foto/PDF firmada y la "
                               "fecha de la firma) antes de facturar")
    elif payload.sin_guia:
        # Retiro en oficina: derivar del saldo pendiente de la cotización que NO esté
        # comprometido en despachos (esa parte sale con guía y se factura POR su guía
        # firmada — ver pendiente_guias_item arriba).
        en_guias = 0.0
        for it in cot.items:
            pend_guia = pendiente_guias_item.get(it.id, 0.0)
            en_guias += pend_guia
            disp = _f(it.cantidad) - fact_qty_item.get(it.id, 0.0) - pend_guia
            if disp > TOL_QTY:
                lineas.append(FacturaItemIn(item_cotizacion_id=it.id, cantidad=round(disp, 4)))
        if not lineas:
            if not cot.items:
                probs.add(400, "La venta no tiene ítems para facturar")
            elif en_guias > TOL_QTY:
                probs.add(409, "Todo lo pendiente de esta venta está asociado a guías de "
                               "despacho: factúralo desde su guía (firmada), no como retiro "
                               "en oficina")
            else:
                probs.add(409, "Esta venta ya fue facturada por completo")
    elif payload.despacho_id:
        desp = db.query(MonzaDespacho).filter(
            MonzaDespacho.id == payload.despacho_id,
            MonzaDespacho.cotizacion_id == cot.id,
        ).first()
        if not desp:
            probs.add(404, "Despacho no encontrado para esta venta")
        elif desp.estado != "despachado":
            probs.add(400, "Solo se puede facturar una guía en estado 'despachado'")
        elif not getattr(desp, "guia_firmada", 0):
            # REGLA 2026-08-06 (espejo del guard de routers/contabilidad.py de GA):
            # SOLO se factura una guía FIRMADA — entregada y firmada por el cliente.
            # La marca la pone Despachos (foto/PDF + fecha); acá únicamente se exige.
            probs.add(400, "La guía de este despacho no está FIRMADA por el cliente: "
                           "márcala en Despachos (subiendo la foto/PDF firmada y la "
                           "fecha de la firma) antes de facturar")
        else:
            # AUDITORÍA (hallazgo LOW «facturar por la vía manual un despacho SIN N° de
            # guía no avisa nada»): paridad con Grupo AM (routers/contabilidad.py) — la
            # factura nacería con numero_guia NULL en silencio. NO bloquea (la guía en
            # papel puede registrarse después); solo deja constancia. El preview SII ya
            # propaga estas advertencias tal cual.
            if not (desp.numero_guia or "").strip():
                advertencias.append("El despacho no tiene N° de guía registrado: la factura "
                                    "quedará sin referencia de guía (complétalo en Despachos)")
            usado_deriv = {}
            # Ítems del despacho elegido
            desp_item_rows = db.query(MonzaDespachoItem).filter(
                MonzaDespachoItem.despacho_id == desp.id
            ).all()
            for di in desp_item_rows:
                disp_di = _f(di.qty_despachada) - fact_qty_di.get(di.id, 0.0)
                # Tope agregado con las MISMAS fórmulas (por canal) que la validación
                # por línea de abajo: derivar con otras colaría cantidades que la
                # propia validación rechazaría después. Dos techos:
                #   · canal guía: firmado − facturado POR el canal guía (restar lo del
                #     retiro acá era el doble descuento del hallazgo HIGH);
                #   · global: vendido − facturado TOTAL (nunca facturar de más aunque
                #     la atribución de canal del legado sea imperfecta).
                it_der = items_by_id.get(di.item_id)
                techo_global = (
                    (_f(it_der.cantidad) if it_der else 0.0)
                    - fact_qty_item.get(di.item_id, 0.0)
                )
                disp_item = min(
                    desp_qty_item_firmada.get(di.item_id, 0.0)
                    - fact_guia_item.get(di.item_id, 0.0),
                    techo_global,
                ) - usado_deriv.get(di.item_id, 0.0)
                disponible = min(disp_di, disp_item)
                if disponible > TOL_QTY:
                    lineas.append(FacturaItemIn(
                        item_cotizacion_id=di.item_id,
                        despacho_item_id=di.id,
                        cantidad=round(disponible, 4),
                    ))
                    usado_deriv[di.item_id] = usado_deriv.get(di.item_id, 0.0) + disponible
            if not lineas:
                # Un cupo NEGATIVO del canal guía (firmado − facturado_del_canal < 0)
                # solo ocurre con facturas LEGADAS: las anteriores a la regla quedaron
                # todas en sin_guia=0 ("canal guía"), así que una de retiro antigua le
                # come cupo a una guía nueva. Decirle "ya fue facturado por completo"
                # a un despacho SIN una sola línea facturada manda a buscar donde no
                # hay nada; el mensaje nombra la salida real (firmar también la guía
                # antigua devuelve el cupo, porque sube el firmado del ítem).
                legado = any(
                    desp_qty_item_firmada.get(di.item_id, 0.0)
                    - fact_guia_item.get(di.item_id, 0.0) < -TOL_QTY
                    for di in desp_item_rows
                )
                if legado:
                    probs.add(409, "Esta venta tiene facturas ANTIGUAS (anteriores a la regla de "
                                   "la guía firmada) atribuidas al canal guía que superan lo "
                                   "firmado: marca como firmada también la guía antigua de esta "
                                   "venta para liberar el cupo de esta")
                else:
                    probs.add(409, "El despacho ya fue facturado por completo")
    if not lineas and not probs.items:
        probs.add(400, "Debe indicar ítems o un despacho a facturar")

    # Validación por línea con acumuladores (guía + ítem) dentro del request
    usado_di = {}
    usado_item = {}
    validadas = []
    for ln in lineas:
        it = items_by_id.get(ln.item_cotizacion_id)
        if not it:
            probs.add(400, f"Ítem {ln.item_cotizacion_id} no pertenece a esta venta")
            continue
        cantidad = ln.cantidad if ln.cantidad is not None else _f(it.cantidad)
        if cantidad <= 0:
            probs.add(400, f"Cantidad inválida para {it.numero_parte or it.descripcion}")
            continue

        if payload.sin_guia:
            # RETIRO EN OFICINA: tope por lo VENDIDO − ya facturado − COMPROMETIDO en
            # despachos (eso sale con guía y se factura por su guía firmada). La derivación
            # de arriba ya aplicó este tope; acá se re-exige por línea (defensa en
            # profundidad: el tope no depende de quién armó la línea).
            disponible = (
                _f(it.cantidad)
                - fact_qty_item.get(ln.item_cotizacion_id, 0.0)
                - pendiente_guias_item.get(ln.item_cotizacion_id, 0.0)
                - usado_item.get(ln.item_cotizacion_id, 0.0)
            )
            if cantidad > disponible + TOL_QTY:
                probs.add(409, f"{it.numero_parte or it.descripcion}: cantidad excede lo vendido "
                               f"no facturado y sin guía asociada (disp {max(disponible,0):.0f}); "
                               "lo que está en guías de despacho se factura desde su guía firmada")
                continue
        else:
            # FLUJO CON GUÍA: tope por lo DESPACHADO CON GUÍA FIRMADA − facturado POR
            # el canal guía (restar lo del retiro era el doble descuento del hallazgo
            # HIGH), con techo global vendido − facturado total; y por GUÍA si aplica.
            # Contar despachos sin firmar dejaría colar sus cantidades por ítems
            # explícitos sin despacho_item_id (regla 2026-08-06).
            despachado_item = desp_qty_item_firmada.get(ln.item_cotizacion_id, 0.0)
            if despachado_item <= 0:
                if desp_qty_item.get(ln.item_cotizacion_id, 0.0) > 0:
                    probs.add(400, f"{it.numero_parte or it.descripcion}: su guía de despacho no "
                                   "está FIRMADA por el cliente; márcala en Despachos antes de facturar")
                else:
                    probs.add(400, f"{it.numero_parte or it.descripcion} no ha sido despachado; no se puede facturar")
                continue
            disponible = min(
                despachado_item - fact_guia_item.get(ln.item_cotizacion_id, 0.0),
                _f(it.cantidad) - fact_qty_item.get(ln.item_cotizacion_id, 0.0),
            ) - usado_item.get(ln.item_cotizacion_id, 0.0)
            if ln.despacho_item_id is not None:
                di = di_by_id.get(ln.despacho_item_id)
                if not di or di.item_id != ln.item_cotizacion_id:
                    probs.add(400, f"Guía/despacho inválido para {it.numero_parte or it.descripcion}")
                    continue
                d_de_linea = desp_by_id.get(di.despacho_id)
                if not d_de_linea or not getattr(d_de_linea, "guia_firmada", 0):
                    probs.add(400, "La guía de este despacho no está FIRMADA por el cliente: "
                                   "márcala en Despachos (subiendo la foto/PDF firmada y la "
                                   "fecha de la firma) antes de facturar")
                    continue
                disp_di = _f(di.qty_despachada) - fact_qty_di.get(di.id, 0.0) - usado_di.get(di.id, 0.0)
                disponible = min(disponible, disp_di)
            if cantidad > disponible + TOL_QTY:
                probs.add(409, f"{it.numero_parte or it.descripcion}: cantidad excede lo despachado/no facturado (disp {max(disponible,0):.0f})")
                continue
            if ln.despacho_item_id is not None:
                usado_di[ln.despacho_item_id] = usado_di.get(ln.despacho_item_id, 0.0) + cantidad
        usado_item[ln.item_cotizacion_id] = usado_item.get(ln.item_cotizacion_id, 0.0) + cantidad

        precio = ln.precio_unit_neto if ln.precio_unit_neto is not None else _f(it.precio_unitario_clp)
        if precio <= 0:  # antes '< 0': una línea en $0 se auto-marcaba 'pagada' (espejo GA)
            probs.add(400, f"{it.numero_parte or it.descripcion}: sin precio de venta, no se puede facturar en $0")
            continue
        validadas.append((it, ln, cantidad, precio))

    cfg = _config(db)
    # Tasa de IVA POR VENTA (congelada en la cotización): la 33 y la 52 tienen que
    # cuadrar, y la guía electrónica usa esta misma tasa. Un 0.19 fijo descuadraría.
    iva_rate = iva_rate_de(cot, cfg)

    # Neto/IVA calculados ANTES de persistir (half-up por línea y en el IVA, criterio
    # SII — ver _total_linea/_iva_clp en service.py) para poder validar el tope de
    # monto sin nada escrito todavía.
    neto_items = float(sum(_total_linea(p, c) for _it, _ln, c, p in validadas))
    display = [
        {
            "item_cotizacion_id": ln.item_cotizacion_id,
            "despacho_item_id": ln.despacho_item_id,
            "numero_parte": it.numero_parte,
            "descripcion": it.descripcion,
            "cantidad": c,
            "precio_unit_neto": _precio2(p),
            "total_neto": _total_linea(p, c),
        }
        for it, ln, c, p in validadas
    ]

    # ── Descuento AUTOMÁTICO por anticipos ya facturados (vía B) ───────────────
    # Cada descuento es una línea NEGATIVA que referencia el folio de la factura de
    # anticipo. Así Σ brutos de las facturas de la venta == total de la venta y el
    # cliente NO paga dos veces la misma mercadería. Aplica a los TRES modos (ítems
    # explícitos, retiro en oficina y despacho).
    descuentos: List[dict] = []
    pendientes_anticipo = _anticipos_pendientes_de_descuento(db, cot.id) if validadas and not probs.items else []
    # Anticipo SIN folio SII (emisión electrónica en vuelo o rechazada): todavía no es un
    # documento tributario. Ni se descuenta (la glosa citaría un folio inexistente y esa
    # mercadería quedaría fuera de toda factura) ni se ignora (facturar el total le
    # cobraría dos veces al cliente): se BLOQUEA hasta que esa emisión se resuelva —
    # reintentándola o eliminándola.
    # Se evalúa ANTES del reparto y ABORTA EL BLOQUE COMPLETO (hallazgo A-6): el `continue`
    # de la versión anterior seguía acumulando los descuentos de los anticipos POSTERIORES,
    # así que el preview publicaba un neto intermedio que no era ni el descontado ni el
    # sin descontar. El resultado ya es no-emitible: el preview no debe inventar cifras.
    sin_folio = [fa for fa, _p in pendientes_anticipo if not (fa.numero_factura or "").strip()]
    # Una BOLETA no puede descontar un anticipo (hallazgo A-5): el descuento se apoya en
    # una referencia al FOLIO de una factura, y una boleta no referencia facturas. Antes
    # salía una boleta con la línea «Descuento anticipo Factura N° …», imposible de
    # sostener ante el SII. La mercadería de una venta con anticipo se FACTURA.
    if pendientes_anticipo and tipo_doc != "factura":
        probs.add(400, "Esta venta tiene una factura de anticipo por descontar: la "
                       "mercadería debe emitirse como FACTURA (no boleta), que es el único "
                       "documento que puede referenciar el folio del anticipo")
    elif sin_folio:
        for fa in sin_folio:
            probs.add(409,
                      f"La factura de anticipo #{fa.id} no tiene folio del SII (su "
                      "emisión electrónica está en curso o falló): resuélvela antes "
                      "de facturar este despacho, o el anticipo no se podrá descontar")
    else:
        restante = neto_items
        for fa, pend in pendientes_anticipo:
            if restante <= TOL:
                break
            d = round(min(pend, restante), 2)
            if d <= TOL:
                continue
            folio_fa = fa.numero_factura
            descuentos.append({"anticipo_factura_id": fa.id, "folio": folio_fa,
                               "monto_neto": d})
            # La línea de descuento va SIN item_cotizacion_id ni despacho_item_id a
            # propósito: si los llevara, _qty_facturada_por_item la contaría como
            # mercadería facturada y romperían el tope físico y el "por facturar".
            display.append({
                "item_cotizacion_id": None, "despacho_item_id": None,
                "numero_parte": "DESCUENTO",
                "descripcion": f"Descuento anticipo Factura N° {folio_fa}",
                "cantidad": 1, "precio_unit_neto": -d, "total_neto": -d,
                "anticipo_factura_id": fa.id,
            })
            restante = round(restante - d, 2)
    neto = round(neto_items - sum(x["monto_neto"] for x in descuentos), 2)
    if descuentos and neto <= TOL:
        advertencias.append(
            "El descuento por anticipo deja esta factura en $0 (el anticipo ya cubría "
            "todo lo despachado): verifica que sea lo esperado antes de emitir."
        )
    # IVA sobre el neto YA DESCONTADO y con la tasa CONGELADA de la venta: si se
    # calculara sobre el neto bruto de las líneas, el cliente pagaría el IVA del
    # anticipo dos veces y Σ brutos se pasaría del total de la venta.
    iva = _iva_clp(neto, iva_rate) if neto else 0.0

    # Tope Σ brutos ≤ total venta cuando alguna línea trae PRECIO EXPLÍCITO del
    # payload: los precios derivados del ítem respetan el invariante por construcción,
    # pero un precio del payload podría inflar la factura por sobre la venta (espejo
    # de routers/contabilidad.py; en Monza es más simple: cot.total_bruto ya está
    # CONGELADO en la cotización, mismo patrón del tope de adelantos de este módulo).
    # Corre bajo el lock de la cotización, así que el Σ facturado no puede moverse.
    # El tope corre SIEMPRE que la venta tenga total (no solo con precio explícito):
    # una factura explícita inflada que consumió el total dejaba pasar a la derivada
    # siguiente en silencio (bypass por ORDEN — cierre de paridad 2026-07-28). En
    # Monza no hay falsos positivos posibles: el total Y los precios por ítem están
    # CONGELADOS, así que las derivadas suman ≤ total por construcción (± polvo
    # half-up: tolerancia de TOL_PAGO por tanda). GA usa una variante más estrecha
    # porque sus ventas pre-snapshot recalculan el total con el TC vivo.
    # OJO (vía B): `neto`/`iva` son los YA DESCONTADOS por anticipo. Tiene que ser así —
    # el bruto de la factura de anticipo ya está dentro de `facturado`, y evaluar el
    # tope con el neto SIN descontar rechazaría la factura final por un cupo que el
    # descuento acaba de devolver.
    total_venta = _f(cot.total_bruto)
    if validadas and not probs.items and total_venta > 0:
        facturas_previas = (
            db.query(MonzaContFacturaCliente)
            .filter(MonzaContFacturaCliente.cotizacion_id == cot.id).all()
        )
        facturado = sum(_f(x.monto_bruto) for x in facturas_previas)
        # DEUDA DECLARADA (hallazgo A-3): esta tolerancia ESCALA por tanda, así que en
        # teoría cada factura "paga" su propio peso de holgura. Aquí se deja como está —a
        # diferencia del tope del anticipo, que sí se aplanó— porque las líneas FÍSICAS lo
        # acotan: una factura de mercadería no puede repetirse sin cantidad despachada
        # disponible, de modo que el número de tandas es finito y conocido. Es
        # preexistente de Monza y cambiarlo rompería las tandas legítimas (cada una
        # arrastra hasta medio peso de polvo half-up del IVA).
        if facturado + neto + iva > total_venta + TOL_PAGO * (len(facturas_previas) + 1):
            disponible = max(total_venta - facturado, 0.0)
            probs.add(
                409,
                f"La factura excede el total de la venta "
                f"(disponible bruto {disponible:,.0f} de {total_venta:,.0f})".replace(",", "."),
            )

    # Snapshot de la guía: en modo despacho es directo; en modo 'items' se deriva si
    # TODAS las líneas provienen de un único despacho (queda trazable en la factura).
    # OJO: este snapshot es TRAZABILIDAD, no la referencia legal — el N° de guía puede
    # ser el tecleado a mano mientras el folio del SII todavía no llega. La referencia
    # 52 del DTE se resuelve en vivo desde el módulo de facturas electrónicas.
    snap_desp_id = desp.id if desp else None
    snap_guia = desp.numero_guia if desp else None
    if desp is None:
        desp_ids = {
            di_by_id[ln.despacho_item_id].despacho_id
            for _it, ln, _c, _p in validadas
            if ln.despacho_item_id is not None and ln.despacho_item_id in di_by_id
        }
        if len(desp_ids) == 1:
            _d = db.query(MonzaDespacho).filter(MonzaDespacho.id == next(iter(desp_ids))).first()
            if _d:
                snap_desp_id = _d.id
                snap_guia = _d.numero_guia

    return {
        "tipo_doc": tipo_doc,
        "validadas": validadas,
        # `lineas` incluye, al final, las líneas NEGATIVAS de descuento por anticipo.
        "lineas": display,
        "receptor": receptor,
        "neto": neto,
        "iva": iva,
        "bruto": neto + iva,
        "iva_rate": iva_rate,
        "desp": desp,
        "snap_desp_id": snap_desp_id,
        "snap_guia": snap_guia,
        # [{anticipo_factura_id, folio, monto_neto}] — lo persiste _persistir_factura
        # como líneas y lo usa la emisión electrónica para las referencias tipo 33.
        "descuentos": descuentos,
        "problemas": probs.items,
        "problemas_status": probs.status,
        "advertencias": advertencias,
    }


def _construir_factura_anticipo(db: Session, payload: FacturaCreate, cot: MonzaCotizacion,
                                *, acumular: bool = True) -> dict:
    """Valida y CONSTRUYE una factura de ANTICIPO (vía B) sin escribir nada. MISMA forma
    de salida que `_construir_factura`, para que preview, emisión y persistencia la
    reutilicen tal cual.

    Es la ÚNICA excepción a la regla rectora «toda factura nace de una guía firmada»:
    respalda ante el SII un adelanto que el cliente pagó ANTES de que llegara la
    mercadería. Lleva UNA sola línea "ANTICIPO" por el neto indicado, SIN
    item_cotizacion_id ni despacho_item_id — así no consume los topes físicos por
    ítem/guía: la mercadería se factura después, y esa factura descuenta este anticipo
    con una línea negativa (ver el bloque de descuentos de `_construir_factura`).

    La excepción es la GUÍA, NO la completitud del receptor: un RUT malo o sin razón
    social lo rechaza el SII igual que en una factura normal, así que se reutiliza
    `_validar_receptor_factura` sin duplicar nada. A diferencia de Grupo AM, en Monza el
    receptor NO viaja en el payload: sale de la ficha del cliente de la venta (F3 lo dejó
    obligatorio en el Cierre de Venta), y si falta, el mensaje manda a completarlo allá.

    `acumular=True` por defecto (al revés que `_construir_factura`): es la firma que
    importa monza_wasabil_dte, que necesita la LISTA COMPLETA de problemas antes de una
    emisión irreversible. La vía manual la llama igual y sale con los problemas unidos
    por " · " y el código del primero (el receptor ya salió con su propio 400 antes,
    porque crear_factura lo valida aparte).

    Llamar con la cotización ya bloqueada cuando el resultado vaya a persistirse: el
    tope se calcula contra lo ya facturado y ese conteo no debe moverse."""
    probs = _ProblemasFactura(acumular)
    advertencias: List[str] = []
    tipo_doc = payload.tipo_doc or "factura"

    # 1) Receptor (mismo trato que una factura normal — ver el docstring).
    receptor = _validar_receptor_factura(cot, tipo_doc, probs)

    # 2) Monto NETO del anticipo: lo indica el operador (el IVA lo pone el backend).
    neto = round(_f(getattr(payload, "monto_neto_anticipo", None)), 2)
    if neto <= 0:
        probs.add(400, "Indica el monto NETO del anticipo (mayor a 0)")

    # 2.b) ADVERTENCIA (no bloqueo) cuando la venta tiene mercadería COMPROMETIDA en
    # despachos vivos aún sin facturar. El multienjambre 2026-08-07 mostró que el
    # anticipo es la salida obvia del operador al que el gate de la firma le rechaza
    # la guía (400 → "factura de anticipo" → 200 por el total, sin referencia 52): un
    # «anticipo» por mercadería YA en guía no respalda un depósito, y el DTE nace
    # materialmente cuestionable. NO se bloquea porque la Fase 7 fijó a propósito que
    # la vía manual no bloquea (punto 15: anticipo por el total → final en $0 con
    # advertencia) y hay flujo legítimo tardío (adelanto aprobado con la mercadería
    # ya despachada). Endurecerlo es DECISIÓN DEL DUEÑO (pendiente registrado).
    comprometida_ant = _qty_comprometida_en_despachos_por_item(db, cot.id)
    if comprometida_ant:
        # Se descuenta SOLO lo facturado por el canal GUÍA (mismo criterio que
        # pendiente_guias_item de _construir_factura). Con el facturado TOTAL, un
        # retiro previo tapaba la cuenta y el aviso se callaba justo en el escenario
        # mixto que debía alertar: venta de 10 con 4 en guía sin firmar + 4 de retiro
        # ya facturado daba pendiente 0 y ninguna advertencia.
        fact_ant_total = _qty_facturada_por_item(db, cot.id)
        fact_ant_retiro = _qty_facturada_retiro_por_item(db, cot.id)
        pendiente_ant = sum(
            max(0.0, qty - max(0.0, fact_ant_total.get(iid, 0.0) - fact_ant_retiro.get(iid, 0.0)))
            for iid, qty in comprometida_ant.items()
        )
        if pendiente_ant > TOL_QTY:
            advertencias.append(
                "Esta venta tiene mercadería en guías de despacho aún sin facturar: una "
                "factura de ANTICIPO no la ampara ni referencia su guía. Si lo que "
                "quieres es facturar la entrega, hazlo desde la guía FIRMADA (Despachos "
                "→ Marcar guía firmada); el anticipo es solo para respaldar un depósito")

    # 3) IVA con la tasa CONGELADA DE LA VENTA (iva_rate_de), nunca un 0,19 fijo: la
    #    factura de anticipo y la del despacho real tienen que sumar exactamente el
    #    total de esa venta, y ese total se congeló con la tasa de la cotización.
    cfg = _config(db)
    iva_rate = iva_rate_de(cot, cfg)
    iva = _iva_clp(neto, iva_rate) if neto > 0 else 0.0
    bruto = round(neto + iva, 2)

    # 4) UN anticipo por venta (hallazgo A-1, CRÍTICO). Emitir un DTE 33 es IRREVERSIBLE
    #    y el candado anti doble emisión del módulo SII (_emision_33_en_vuelo_de_cot) solo
    #    dura mientras el HTTP está en vuelo: apenas responde la primera emisión, la
    #    segunda pasa libre. Sin este guard, DOS clics tranquilos emitían DOS facturas de
    #    anticipo REALES por UN mismo adelanto (reproducido con folios 9901 y 9902).
    #    Es el espejo del bloqueo que _construir_factura ya hacía en el otro sentido
    #    (anticipo sin folio frena la factura del despacho).
    #    POR QUÉ MONZA BLOQUEA Y GRUPO AM NO: en GA una OC puede tener N adelantos y cada
    #    uno se liga a su factura de anticipo por cont_adelanto.factura_anticipo_id, así
    #    que varias facturas de anticipo son el caso NORMAL. En Monza el adelanto es UNO
    #    por venta (uq_monza_cont_adelanto_cotizacion) y el vínculo con la factura de
    #    anticipo es DERIVADO: dos anticipos por la misma venta no tienen dos adelantos
    #    que los respalden. Se deja la puerta explícita (confirmar_segundo_anticipo) para
    #    el anticipo parcial pactado, en vez de arrinconar al operador.
    #    No se filtra por "no anulada" porque en Monza una factura no se anula: se ELIMINA
    #    (eliminar_factura, con sus guards) — si existe la fila, el documento está vivo.
    if not getattr(payload, "confirmar_segundo_anticipo", False):
        previo = (
            db.query(MonzaContFacturaCliente)
            .filter(MonzaContFacturaCliente.cotizacion_id == cot.id,
                    MonzaContFacturaCliente.es_anticipo == 1)
            .order_by(MonzaContFacturaCliente.id.asc()).first()
        )
        if previo is not None:
            ident = (previo.numero_factura or "").strip() or f"#{previo.id}"
            monto_txt = f"{_f(previo.monto_bruto):,.0f}".replace(",", ".")
            probs.add(
                409,
                f"Esta venta ya tiene una factura de anticipo (N° {ident}, ${monto_txt}). "
                "En Monza el adelanto es uno por venta: si de verdad necesitas un segundo "
                "anticipo, márcalo explícitamente.",
            )

    # 5) Tope: lo ya facturado (TODAS las facturas de la venta — anticipos en bruto y
    #    finales ya con su descuento) + este anticipo no puede pasarse del total de la
    #    venta. Sin esto, la vía B podría facturar dos veces la misma mercadería.
    #    Tolerancia PLANA (TOL_PAGO a secas, igual que Grupo AM). Escalarla por tanda
    #    —TOL_PAGO × (nº de facturas previas + 1)— era el hallazgo A-3: un anticipo de
    #    neto $1 tiene bruto $1, así que cada anticipo de polvo PAGABA SU PROPIA
    #    tolerancia y se colaban de a uno indefinidamente (200 anticipos de $1 llevaron
    #    Σ brutos a $119.200 sobre un total de $119.000, y esa holgura comprada se
    #    transfería después a una factura real). Aquí no hay tandas físicas que justifiquen
    #    la holgura creciente: el anticipo no consume cantidad despachada.
    total_venta = _f(cot.total_bruto)
    if neto > 0 and total_venta > 0:
        facturas_previas = (
            db.query(MonzaContFacturaCliente)
            .filter(MonzaContFacturaCliente.cotizacion_id == cot.id).all()
        )
        facturado = sum(_f(f.monto_bruto) for f in facturas_previas)
        if facturado + bruto > total_venta + TOL_PAGO:
            disponible = max(total_venta - facturado, 0.0)
            probs.add(
                409,
                f"El anticipo excede lo aún no facturado de la venta "
                f"(disponible bruto {disponible:,.0f} de {total_venta:,.0f})".replace(",", "."),
            )

    # 6) Línea ÚNICA, sin ítem físico ni guía.
    descripcion = ((getattr(payload, "descripcion_anticipo", None) or "").strip()
                   or f"Anticipo venta {cot.numero or cot.id}")
    display = [{
        "item_cotizacion_id": None, "despacho_item_id": None,
        "numero_parte": "ANTICIPO", "descripcion": descripcion,
        "cantidad": 1, "precio_unit_neto": neto, "total_neto": neto,
    }]
    return {
        "tipo_doc": tipo_doc,
        # Sin líneas físicas: `validadas` vacío es lo que hace que esta factura no toque
        # los topes por ítem/guía ni el "por facturar" de la venta.
        "validadas": [],
        "lineas": display,
        "receptor": receptor,
        "neto": neto,
        "iva": iva,
        "bruto": bruto,
        "iva_rate": iva_rate,
        # Sin despacho, por definición (la vía B es la excepción a la guía).
        "desp": None,
        "snap_desp_id": None,
        "snap_guia": None,
        "descuentos": [],
        "descripcion_anticipo": descripcion,
        "problemas": probs.items,
        "problemas_status": probs.status,
        "advertencias": advertencias,
    }


def _persistir_factura(db: Session, payload: FacturaCreate, cot: MonzaCotizacion,
                       datos: dict, *, folio: Optional[str], tipo_doc: str,
                       usuario_id=None, aplicar_adelantos: bool = True) -> MonzaContFacturaCliente:
    """Persiste la factura + sus líneas + los montos congelados a partir de `datos`
    (la salida de `_construir_factura`). NO hace commit: la transacción la cierra el
    llamador (la vía manual commitea al tiro; la emisión electrónica commitea la
    factura JUNTO con su claim anti doble emisión, antes de cualquier HTTP).

    `folio` es keyword-only y PUEDE ser None a propósito: la vía SII persiste sin folio
    porque lo asigna el SII al emitir (el UNIQUE de MySQL no colisiona entre NULLs).
    Por eso el guard de "folio obligatorio" vive en el endpoint manual y no aquí.

    `aplicar_adelantos=False` (emisión electrónica): la aplicación del adelanto como
    cobranza se DIFIERE hasta que el SII confirme el folio — una factura que el SII
    rechaza no debe haber movido plata. Requiere el lock de la cotización ya tomado por
    el llamador, igual que la vía manual."""
    # _hoy_chile (espejo GA): la fecha tributaria es la de Chile, no la del server (UTC).
    fecha_emision = _parse_date(payload.fecha_emision) or _hoy_chile()
    # `is not None`: plazo 0 días (contado) también debe generar vencimiento (= emisión)
    fecha_venc = (fecha_emision + timedelta(days=int(payload.plazo_dias))
                  if payload.plazo_dias is not None else None)
    cli = cot.cliente
    # Trazabilidad: marca el retiro en oficina si el usuario no puso observación propia.
    observaciones = payload.observaciones or ("Retiro en oficina (sin guía)" if payload.sin_guia else None)
    # La puerta de emergencia deja RASTRO en el documento: quién retiró con el adelanto
    # pendiente tiene que poder reconstruirse después sin adivinar.
    if (payload.sin_guia and getattr(payload, "confirmar_retiro_sin_adelanto", False)
            and _adelanto_sin_verificar(cot)):
        observaciones = ((observaciones + " · ") if observaciones else "") + \
            "Retiro autorizado con adelanto AÚN NO verificado por Tesorería"
    es_anticipo = bool(getattr(payload, "es_anticipo", False))

    factura = MonzaContFacturaCliente(
        cotizacion_id=cot.id,
        numero_cotizacion=cot.numero,
        cliente_nombre=(cli.nombre if cli else None),
        rut_cliente=(cli.rut if cli else None),
        # La factura de ANTICIPO nunca ampara un traslado: su despacho/guía se fuerzan a
        # NULL aunque el payload traiga un despacho_id (segundo cinturón sobre el None
        # que ya devuelve _construir_factura_anticipo). Además es lo que lee la emisión
        # electrónica para NO ponerle una referencia tipo 52.
        despacho_id=None if es_anticipo else datos["snap_desp_id"],
        numero_guia=None if es_anticipo else datos["snap_guia"],
        es_anticipo=1 if es_anticipo else 0,
        # CANAL de la factura (regla 2026-08-06): el retiro en oficina queda marcado
        # para que el neteo guía↔retiro de _construir_factura no descuente la misma
        # mercadería dos veces. Un anticipo nunca es retiro (no consume mercadería).
        sin_guia=0 if es_anticipo else (1 if payload.sin_guia else 0),
        numero_factura=folio or None,
        tipo_doc=tipo_doc,
        fecha_emision=fecha_emision, condicion_pago=payload.condicion_pago,
        plazo_dias=payload.plazo_dias, fecha_vencimiento=fecha_venc,
        observaciones=observaciones, usuario_id=usuario_id,
    )
    db.add(factura)
    db.flush()
    if es_anticipo:
        # Línea ÚNICA "ANTICIPO", sin ítem de cotización ni de despacho: no consume
        # cantidad física, así que la mercadería sigue entera por facturar.
        db.add(MonzaContFacturaClienteItem(
            factura_id=factura.id, numero_parte="ANTICIPO",
            descripcion=datos["descripcion_anticipo"],
            cantidad=1, precio_unit_neto=datos["neto"], total_neto=datos["neto"],
        ))
    else:
        for it, ln, cantidad, precio in datos["validadas"]:
            # _total_linea: precio a 2 dec ANTES de × qty y half-up a peso — el mismo
            # cálculo con que se validó el tope (neto = Σ de estas líneas).
            db.add(MonzaContFacturaClienteItem(
                factura_id=factura.id, item_cotizacion_id=ln.item_cotizacion_id,
                despacho_item_id=ln.despacho_item_id,
                numero_parte=it.numero_parte, descripcion=it.descripcion,
                cantidad=cantidad, precio_unit_neto=_precio2(precio),
                total_neto=_total_linea(precio, cantidad),
            ))
        # Líneas de DESCUENTO por anticipo ya facturado (NEGATIVAS; referencian a la
        # factura de anticipo). Van SIN item_cotizacion_id ni despacho_item_id a
        # propósito: la línea de descuento NO es mercadería. Si los llevara,
        # _qty_facturada_por_item la contaría como cantidad facturada y rompería el tope
        # físico y el "por facturar". De estas líneas se DERIVA el pendiente del
        # anticipo, así que borrar esta factura devuelve el cupo sola (cascade).
        for dsc in datos.get("descuentos", []):
            db.add(MonzaContFacturaClienteItem(
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
    if aplicar_adelantos:
        # Adelanto verificado de la venta → se aplica como cobranza en esta factura.
        # `advertencias`: sobre una factura de ANTICIPO, _aplicar_adelanto puede tener que
        # RE-RUTEAR plata que ya cayó en otra factura de la venta; si los guards se lo
        # impiden deja aquí el aviso, y crear_factura lo devuelve al operador (A-4/A-7).
        _aplicar_adelanto(db, cot, factura, usuario_id,
                          advertencias=datos.get("advertencias"))
        db.flush()
    _recompute_factura(factura)
    return factura


@router.post("/facturas/preview")
def preview_factura(
    payload: FacturaCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """PREVISUALIZA la factura de la vía MANUAL antes de registrarla: líneas derivadas,
    descuento(s) por anticipo, receptor, montos congelados (neto / IVA half-up con la tasa
    de la venta / bruto) y la lista COMPLETA de problemas y advertencias. NO persiste ni
    congela nada. Espejo de routers/contabilidad.py de Grupo AM.

    POR QUÉ (M1): en la vía manual el operador registra una factura que YA EMITIÓ ante el
    SII, y hasta ahora lo hacía A CIEGAS — los montos los calcula el backend (descuento de
    anticipo incluido), así que si no cuadraban con el papel se descubría después de
    contabilizarla, con el folio ya consumido. La vía electrónica sí tenía preview
    (monza_wasabil_dte); la manual no.

    Es CABLEADO, no lógica nueva: `_construir_factura` / `_construir_factura_anticipo` ya
    devuelven exactamente esta forma y son la única fuente de verdad de las reglas, así
    que lo que muestra el preview es lo que va a validar el POST. Se llama con
    `acumular=True` (todos los problemas juntos, no solo el primero) y SIN lock: no
    escribe nada, y los topes se re-calculan bajo el lock de la cotización al registrar.

    El FOLIO no entra en `puede_emitir` (misma decisión que Grupo AM): obligatorio, único
    y numérico-si-es-anticipo se validan al registrar, donde el operador ya lo tiene en la
    mano. `puede_emitir` habla de los DATOS de la factura."""
    cot = _cargar_venta(db, payload.cotizacion_id, lock=False)
    datos = (_construir_factura_anticipo(db, payload, cot) if payload.es_anticipo
             else _construir_factura(db, payload, cot, acumular=True))
    return {
        "puede_emitir": not datos["problemas"],
        "problemas": datos["problemas"],
        "advertencias": datos["advertencias"],
        "receptor": datos["receptor"],
        # Incluye al final las líneas NEGATIVAS de descuento por anticipo (vía B).
        "lineas": datos["lineas"],
        # iva_rate viaja al frontend para que el modal pinte el % REAL de la venta (el
        # iva_pct congelado), jamás un 19% escrito a mano. Mismo contrato que el preview
        # de monza_wasabil_dte, así que el modal puede leer los dos igual.
        "totales": {"neto": datos["neto"], "iva": datos["iva"], "bruto": datos["bruto"],
                    "iva_rate": datos.get("iva_rate")},
        "descuentos": datos.get("descuentos", []),
        "es_anticipo": bool(payload.es_anticipo),
        "sin_guia": bool(payload.sin_guia),
        # Guía que quedará registrada en la factura (derivada, ver snap_desp_id): en la
        # vía manual el operador elige la guía, y este es el eco de lo que se va a grabar.
        "guia": {"despacho_id": datos["snap_desp_id"], "numero_guia": datos["snap_guia"]},
    }


@router.post("/facturas")
def crear_factura(
    payload: FacturaCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """EMITE una factura a cliente REGISTRANDO a mano el folio del DTE ya emitido. Tres
    modos: `despacho_id` (deriva líneas de una guía 'despachado', tope por despachado),
    `sin_guia=True` (RETIRO EN OFICINA: factura el saldo de la cotización sin requerir
    despacho, tope por lo VENDIDO − ya facturado), o `items` explícitos. Reglas: folio
    único; nunca facturar más de lo permitido por el modo; congela montos (neto, IVA,
    bruto) y snapshots de cliente/guía. El control anti-doble-facturación es compartido
    (fact_qty_item cuenta TODAS las facturas, así retiro y guía no se solapan).

    Las reglas de negocio viven en `_construir_factura` / `_persistir_factura`, que la
    emisión electrónica al SII reutiliza tal cual. Lo que se queda AQUÍ y solo aquí es
    el folio: obligatorio y único para tipo 'factura', porque en esta vía el operador ya
    tiene el DTE en la mano. La vía SII persiste sin folio a propósito."""
    # Lock de la cotización: serializa la facturación concurrente de la misma venta.
    cot = _cargar_venta(db, payload.cotizacion_id, lock=True)

    tipo_doc = payload.tipo_doc or "factura"

    # La factura de ANTICIPO es un DTE 33: una boleta no puede respaldar un anticipo
    # ante el SII (ni admite las referencias 33 que después la descuentan). Va ANTES
    # del folio porque es un error de la FORMA del documento, no del dato que falta.
    if payload.es_anticipo and tipo_doc != "factura":
        raise HTTPException(400, "La factura de anticipo debe ser tipo 'factura' (no boleta)")

    # Receptor ANTES que el folio: el orden de los mensajes importa para el operador
    # (primero "arregla la ficha del cliente", después "escribe el folio"), y así lo
    # verifican las suites. _construir_factura lo revalida — es una función pura.
    _validar_receptor_factura(cot, tipo_doc, _ProblemasFactura(acumular=False))

    # Folio SII: OBLIGATORIO para tipo 'factura' (hoy se digita a mano del DTE emitido;
    # espejo GA contabilidad.py pre-Wasabil) y único. Una boleta puede quedar sin folio
    # (el UNIQUE admite NULLs). `.strip()` de paso rechaza el folio de puros espacios.
    # Se valida ANTES de derivar líneas: un reenvío con el mismo folio debe decir
    # "folio duplicado", no "esta venta ya fue facturada por completo".
    folio = (payload.numero_factura or "").strip()
    if tipo_doc == "factura" and not folio:
        raise HTTPException(400, "Ingresa el folio SII de la factura (o cámbialo a boleta)")
    # Folio NUMÉRICO cuando la factura es un ANTICIPO. La factura del despacho lo va a
    # citar en una referencia tipo 33, y el SII exige que el FolioRef sea el folio
    # correlativo —un número— del DTE referenciado. El módulo de facturas electrónicas
    # ya lo valida al armar esa referencia (monza_wasabil_dte/service.py), pero ahí es
    # TARDE y LEJOS: el folio se teclea AQUÍ, al registrar un anticipo ya emitido, y el
    # error aparecía recién semanas después, al facturar el despacho, con el anticipo ya
    # contabilizado y descontando. Reproducido con 'N/A-99', 'FAC 123', 'N/A' y '0'.
    # SOLO sobre el anticipo: una factura normal puede traer un folio legado no numérico
    # y romper su registro sería una regresión gratuita (nadie referencia esas).
    # (`isascii` además de `isdigit`: '٣'.isdigit() es True y no es un folio del SII.)
    # El tope de 18 caracteres es el mismo del SII para el folio de una referencia
    # (FOLIO_REF_MAX en monza_wasabil_dte/service.py) y de paso evita que un int() sobre
    # miles de dígitos reviente por el límite de conversión de Python.
    if payload.es_anticipo and folio and (
            len(folio) > 18 or not (folio.isascii() and folio.isdigit()) or int(folio) <= 0):
        raise HTTPException(
            400,
            f"El folio de la factura de anticipo ('{folio}') debe ser el número "
            "correlativo que le asignó el SII: la factura de la mercadería la va a "
            "referenciar (referencia tipo 33) y el SII solo acepta folios numéricos de "
            "hasta 18 dígitos. Escríbelo con dígitos, sin letras, guiones ni espacios.")
    if folio:
        dup = db.query(MonzaContFacturaCliente).filter(
            MonzaContFacturaCliente.numero_factura == folio,
        ).first()
        if dup:
            raise HTTPException(409, f"El folio {folio} ya existe")

    # acumular=False: cada regla incumplida sale con su propio código/mensaje, igual
    # que cuando este endpoint era un bloque único. La vía B (anticipo) tiene su propio
    # constructor: no deriva líneas de una guía ni toca los topes físicos.
    datos = (_construir_factura_anticipo(db, payload, cot) if payload.es_anticipo
             else _construir_factura(db, payload, cot, acumular=False))
    # Salida de los problemas, con DOS roles según la vía:
    #   · factura normal (acumular=False): la lista SIEMPRE viene vacía —cada problema
    #     ya salió como HTTPException—. Es red de seguridad para que un cambio futuro
    #     del default no deje persistir una factura con reglas incumplidas en silencio.
    #   · anticipo (acumular=True, la firma que comparte con la emisión electrónica):
    #     éste ES el canal de salida. Todos los problemas juntos con " · " y el código
    #     del primero (400 datos faltantes / 409 topes), igual que Grupo AM.
    if datos["problemas"]:
        raise HTTPException(datos["problemas_status"] or 409, " · ".join(datos["problemas"]))

    try:
        factura = _persistir_factura(
            db, payload, cot, datos, folio=folio or None, tipo_doc=tipo_doc,
            usuario_id=getattr(current_user, "id", None), aplicar_adelantos=True,
        )
        db.commit()
    except IntegrityError as e:
        db.rollback()
        orig = str(getattr(e, "orig", e))
        if "uq_monza_cont_factura_folio" in orig:
            raise HTTPException(409, "Folio de factura duplicado")
        logger.error("IntegrityError al crear factura Monza: %s", orig)
        raise HTTPException(409, "No se pudo guardar la factura (conflicto de integridad)")
    db.refresh(factura)
    # guia_viva: N° de guía ACTUAL del despacho, no el snapshot congelado (ver _guias_vivas).
    out = _serialize_factura(factura, guia_viva=_guia_viva_de(db, factura),
                             adelanto_id=_adelanto_id_de(db, factura))
    # ADVERTENCIAS (hallazgo A-7): avisos que NO bloquean pero que el operador tiene que
    # leer — «el descuento por anticipo deja esta factura en $0», «el despacho no tiene N°
    # de guía», «no se pudo re-rutear el adelanto». Hasta ahora solo salían por la vía SII
    # (el preview las muestra) y por la vía manual se perdían: en pantalla quedaba una
    # factura en $0 marcada «Pagada» sin ninguna explicación. Campo ADITIVO —siempre
    # presente, lista vacía cuando no hay nada que decir— para que el front no tenga que
    # distinguir entre "sin advertencias" y "backend viejo".
    out["advertencias"] = list(datos.get("advertencias") or [])
    return out


@router.post("/facturas/{factura_id}/cobranzas")
def registrar_cobranza(
    factura_id: int,
    payload: CobranzaIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Registra un pago real del cliente. Bloquea la factura, rechaza medios de
    factoring y el SOBRE-PAGO (recalcula el saldo desde las cobranzas reales). Si hay
    factoring vigente, exige liquidarlo antes."""
    factura = (
        db.query(MonzaContFacturaCliente)
        .options(selectinload(MonzaContFacturaCliente.factoring))
        .filter(MonzaContFacturaCliente.id == factura_id)
        .populate_existing()
        .with_for_update(of=MonzaContFacturaCliente)
        .first()
    )
    if not factura:
        raise HTTPException(404, "Factura no encontrada")
    if payload.monto <= 0:
        raise HTTPException(400, "El monto debe ser mayor a 0")
    if _es_medio_factoring(payload.medio):
        raise HTTPException(400, "Las cobranzas de factoring se gestionan desde el panel de factoring")
    # medio='adelanto' lo genera SOLO el sistema al aplicar un adelanto aprobado por
    # Tesorería: a mano descuadraría el invariante monto_aplicado == Σ cobranzas.
    if (payload.medio or "") == MEDIO_ADELANTO:
        raise HTTPException(400, "Las cobranzas de adelanto las genera el sistema al aplicar "
                                 "un adelanto aprobado por Tesorería")
    # Vía B (hallazgo A-2): una factura de ANTICIPO se salda SOLO con la aplicación del
    # adelanto. Si un administrativo la salda a mano con la transferencia del cliente, el
    # depósito se cuenta DOS VECES: Tesorería salta esta factura (filtra saldo > TOL) y la
    # plata del adelanto cae en OTRA factura de la venta, que aparece cobrada sin que
    # nadie haya pagado. Reproducido: cliente depositó $59.500 y el sistema mostró
    # facturado 119.000 / cobrado 119.000. La empresa deja de perseguir plata real.
    # Mismo espíritu que el rechazo de medio='adelanto' de arriba: la plata del adelanto
    # entra por una sola puerta. (Grupo AM tiene el mismo hueco: es deuda declarada allá.)
    if factura.es_anticipo:
        raise HTTPException(409, "Una factura de anticipo se salda con el adelanto que "
                                 "aprueba Tesorería, no con una cobranza manual: registra "
                                 "el depósito en Contabilidad → Tesorería")
    # Guard de factoring desde la lectura BLOQUEANTE (espejo GA): el selectinload sirve
    # el snapshot del inicio del request — una cesión commiteada en paralelo debe verse.
    fac_vig = _factoring_bloqueado(db, factura.id)
    if fac_vig and fac_vig.estado == "vigente":
        raise HTTPException(409, "La factura tiene un factoring vigente; liquídelo antes de registrar cobranzas")
    # Guard SII (AUDITORÍA, hallazgo MEDIUM «cobranza manual sobre una factura que el SII
    # todavía no conoce»): espejo EXACTO del que ya tiene _aplicar_adelanto. Una factura
    # ELECTRÓNICA sin folio y con su DTE en vuelo/borrador/rechazado no debe recibir
    # plata: si el SII termina rechazándola, quedaba dinero contabilizado contra un
    # documento tributario que nunca existió, la factura marcada 'pagada' y —peor— zombi
    # IMBORRABLE («revierta las cobranzas antes de eliminar»), secuestrando el cupo
    # facturable de esa mercadería. El adelanto ya estaba protegido; el pago manual no.
    # Solo se consulta cuando NO hay folio Y el documento es FACTURA: una factura con
    # folio no toca el módulo DTE y una BOLETA jamás tiene un DTE 33 (así no se expone a
    # un 503 gratuito en una BD sin migrar). Apenas el SII confirma el folio, la misma
    # cobranza se acepta.
    _exigir_sii_emitido(db, factura, "registrar pagos")
    # Tope anti sobre-pago desde la lectura BLOQUEANTE (el selectinload de cobranzas
    # emitía un SELECT plano: parece seguro y no lo es si el aislamiento cambia).
    cobs_frescas = _cobranzas_bloqueadas(db, factura.id)
    pagado_actual = sum(_f(c.monto) for c in cobs_frescas)
    saldo_actual = round(_f(factura.monto_bruto) - pagado_actual, 2)
    # La holgura de TOL_PAGO (1 CLP) es DELIBERADA: absorbe el polvo de redondeo half-up
    # de IVA/factoring. Consecuencia conocida y aceptada (auditoría 2026-07-29, hallazgo
    # LOW): con un sobrepago dentro de la holgura, la identidad Σ cobranzas == bruto −
    # saldo se desvía hasta 1 CLP en ESA factura, porque el saldo se clampea a 0 en
    # _recompute_factura. No es acumulable (el segundo peso ya sale 400) y es paridad
    # exacta con GA. NO capar el monto en silencio: alteraría lo que registró el operador
    # — el fundamento completo está en docs/regla-lecturas-de-plata.md ("Tolerancia de 1 CLP").
    if payload.monto > saldo_actual + TOL_PAGO:
        raise HTTPException(400, f"El monto excede el saldo pendiente ({max(saldo_actual, 0):.0f})")
    nueva = MonzaContCobranza(
        factura_id=factura.id, fecha=_parse_date(payload.fecha) or date.today(),
        monto=payload.monto, medio=payload.medio or "transferencia",
        banco=payload.banco, numero_operacion=payload.numero_operacion,
        observaciones=payload.observaciones, usuario_id=getattr(current_user, "id", None),
    )
    db.add(nueva)
    db.flush()
    db.refresh(factura, with_for_update=True)
    _recompute_factura(factura, cobranzas=cobs_frescas + [nueva])
    db.commit()
    db.refresh(factura)
    # guia_viva: N° de guía ACTUAL del despacho, no el snapshot congelado (ver _guias_vivas).
    return _serialize_factura(factura, guia_viva=_guia_viva_de(db, factura),
                              adelanto_id=_adelanto_id_de(db, factura))


@router.delete("/facturas/{factura_id}/cobranzas/{cobranza_id}")
def eliminar_cobranza(
    factura_id: int,
    cobranza_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Revierte un pago real (no de factoring) y recalcula saldo/estado."""
    # ORDEN GLOBAL DE LOCKS: cotización → factura → adelanto. El mismo que usan
    # crear_factura (lock de cot), verificar_adelanto y monza_tesoreria.aprobar_adelanto.
    # Antes este endpoint bloqueaba SOLO la factura, así que quedaba fuera del punto de
    # serialización de la venta: los otros escritores de monto_aplicado sostienen el lock
    # de la COTIZACIÓN y con locks disjuntos no hay exclusión mutua (auditoría 2026-07-21,
    # docs/regla-lecturas-de-plata.md).
    ref = (db.query(MonzaContFacturaCliente)
           .filter(MonzaContFacturaCliente.id == factura_id).first())
    if not ref:
        raise HTTPException(404, "Factura no encontrada")
    if ref.cotizacion_id:
        (db.query(MonzaCotizacion).filter(MonzaCotizacion.id == ref.cotizacion_id)
         .populate_existing().with_for_update().first())
    factura = (
        db.query(MonzaContFacturaCliente)
        .filter(MonzaContFacturaCliente.id == factura_id)
        .populate_existing()
        .with_for_update(of=MonzaContFacturaCliente)
        .first()
    )
    if not factura:
        raise HTTPException(404, "Factura no encontrada")
    c = db.query(MonzaContCobranza).filter(
        MonzaContCobranza.id == cobranza_id, MonzaContCobranza.factura_id == factura_id
    ).first()
    if not c:
        raise HTTPException(404, "Cobranza no encontrada")
    if _es_medio_factoring(c.medio):
        raise HTTPException(400, "Las cobranzas de factoring se revierten desde el panel de factoring")
    # Espejo del guard de registrar_cobranza: con la factura CEDIDA al factor la
    # asignación de pagos está CONGELADA — liquidar_factoring calcula la retención a
    # liberar como bruto − Σ cobranzas, así que borrar una cobranza después de la cesión
    # movería esa base y el factor liberaría un monto distinto al pactado.
    if factura.factoring and factura.factoring.estado == "vigente":
        raise HTTPException(409, "La factura tiene un factoring vigente; liquídelo antes de revertir cobranzas")
    conciliada = (db.query(MonzaTesConciliacionIngreso)
                  .filter(MonzaTesConciliacionIngreso.cobranza_id == c.id).first())
    if conciliada:
        raise HTTPException(409, "La cobranza está conciliada con el banco; desconcíliela en Tesorería primero")
    # Si la cobranza es la aplicación de un adelanto, devolver el monto a monto_aplicado
    # para mantener la invariante (permite re-aplicarlo a otra factura). BAJO LOCK: el
    # UPDATE es ciego y sin el lock dos reversiones concurrentes se pisan (lost update).
    # incluir_anulado=True: el invariante monto_aplicado == Σ cobranzas 'adelanto' tiene
    # que cuadrar SIEMPRE. Anular exige aplicado ≈ 0, así que por el endpoint esta
    # combinación no se da; pero si una reparación manual en la BD dejara una fila anulada
    # con plata aplicada, filtrarla aquí perdería la devolución en silencio.
    if c.medio == MEDIO_ADELANTO and factura.cotizacion_id:
        adel = _adelanto_de_cot(db, factura.cotizacion_id, lock=True, incluir_anulado=True)
        if adel is not None:
            adel.monto_aplicado = round(max(_f(adel.monto_aplicado) - _f(c.monto), 0.0), 2)
    # Totales desde la lectura BLOQUEANTE menos la fila borrada (no la relación perezosa)
    cobs_frescas = [x for x in _cobranzas_bloqueadas(db, factura.id) if x.id != c.id]
    db.delete(c)
    db.flush()
    db.refresh(factura, with_for_update=True)
    _recompute_factura(factura, cobranzas=cobs_frescas)
    db.commit()
    return {"ok": True}


@router.post("/facturas/{factura_id}/factoring")
def set_factoring(
    factura_id: int,
    payload: FactoringIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Crea o actualiza el factoring (1 por factura). Valida adelanto <= cupo (bruto -
    pagos reales), deriva la retención si falta, y genera SOLO la cobranza de ADELANTO.
    No editable si ya está liquidado."""
    factura = (
        db.query(MonzaContFacturaCliente)
        .options(selectinload(MonzaContFacturaCliente.cobranzas), selectinload(MonzaContFacturaCliente.factoring))
        .filter(MonzaContFacturaCliente.id == factura_id)
        .populate_existing()
        .with_for_update(of=MonzaContFacturaCliente)
        .first()
    )
    if not factura:
        raise HTTPException(404, "Factura no encontrada")
    # Una factura de ANTICIPO no es cobrable a futuro: respalda plata que el cliente YA
    # entregó. Cederla al factor vendería una acreencia inexistente (y su saldo se salda
    # con la cobranza 'adelanto', no con un pago por venir). Espejo GA contabilidad.py.
    if factura.es_anticipo:
        raise HTTPException(409, "No se puede hacer factoring de una factura de anticipo "
                                 "(respalda un adelanto ya recibido)")
    # Guard SII de la PUERTA DE ENTRADA. Ceder al factor es plata entrando contra la
    # factura, exactamente igual que una cobranza, y hasta acá era el único camino de plata
    # que no lo pedía: se podía vender al factor una acreencia que el SII nunca conoció, y
    # esa fila quedaba después imposible de deshacer (ver `revertir_factoring`, que es su
    # contrapartida y abre EXACTAMENTE donde este guard bloquea).
    _exigir_sii_emitido(db, factura, "cederla al factoring")
    # Lecturas BLOQUEANTES (espejo GA): el cupo que se cede al factor se calcula sobre
    # los pagos REALES del cliente, y la relación perezosa sirve el snapshot viejo del
    # request. Ver docs/regla-lecturas-de-plata.md.
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
        fac = MonzaContFactoring(factura_id=factura.id)
        db.add(fac)
    fac.usuario_id = getattr(current_user, "id", None)
    fac.empresa_factoring = payload.empresa_factoring
    fac.id_operacion = payload.id_operacion
    fac.fecha_operacion = _parse_date(payload.fecha_operacion) or date.today()
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
            conciliada = (db.query(MonzaTesConciliacionIngreso)
                          .filter(MonzaTesConciliacionIngreso.cobranza_id == c.id).first())
            if conciliada:
                raise HTTPException(409, "El adelanto del factoring está conciliado con el banco; desconcílielo en Tesorería antes de modificar el factoring")
            db.delete(c)
    db.flush()
    if payload.monto_adelantado > 0:
        db.add(MonzaContCobranza(
            factura_id=factura.id, fecha=fac.fecha_operacion, monto=payload.monto_adelantado,
            medio=MEDIO_FACT_ADELANTO, banco=payload.banco, numero_operacion=payload.id_operacion,
            observaciones=f"Adelanto factoring {payload.empresa_factoring or ''}".strip(),
            usuario_id=getattr(current_user, "id", None),
        ))
    db.flush()
    db.refresh(factura, with_for_update=True)
    # Totales desde la lectura BLOQUEANTE (recargada tras el flush para incluir lo
    # agregado en esta transacción): la relación perezosa serviría el snapshot viejo.
    _recompute_factura(factura, cobranzas=_cobranzas_bloqueadas(db, factura.id))
    db.commit()
    db.refresh(factura)
    # guia_viva: N° de guía ACTUAL del despacho, no el snapshot congelado (ver _guias_vivas).
    return _serialize_factura(factura, guia_viva=_guia_viva_de(db, factura),
                              adelanto_id=_adelanto_id_de(db, factura))


@router.post("/facturas/{factura_id}/factoring/liquidar")
def liquidar_factoring(
    factura_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Liquida el factoring vigente: libera el saldo pendiente REAL como retención,
    cerrando la factura en saldo 0, y marca estado 'liquidada'."""
    factura = (
        db.query(MonzaContFacturaCliente)
        .options(selectinload(MonzaContFacturaCliente.cobranzas), selectinload(MonzaContFacturaCliente.factoring))
        .filter(MonzaContFacturaCliente.id == factura_id)
        .populate_existing()
        .with_for_update(of=MonzaContFacturaCliente)
        .first()
    )
    if not factura:
        raise HTTPException(404, "Factura no encontrada")
    fac = _factoring_bloqueado(db, factura.id)
    if not fac or fac.estado != "vigente":
        raise HTTPException(400, "No hay factoring vigente para liquidar")
    # Liquidar libera el saldo como retención: es plata que se da por cobrada contra la
    # factura. Si el documento no existe ante el SII, se estaría cerrando en saldo 0 una
    # factura que tributariamente no existe. La salida de un factoring así es `revertir`.
    _exigir_sii_emitido(db, factura, "liquidar el factoring")
    # Liberar el saldo pendiente REAL desde la lectura BLOQUEANTE (espejo GA): con la
    # relación perezosa, un pago del cliente commiteado en paralelo era invisible y el
    # factor liberaba de más (o de menos) que lo pactado.
    cobs_frescas = _cobranzas_bloqueadas(db, factura.id)
    pagado_actual = sum(_f(c.monto) for c in cobs_frescas)
    liberar = round(max(_f(factura.monto_bruto) - pagado_actual, 0.0), 2)
    fac.retencion = liberar
    if liberar > TOL:
        db.add(MonzaContCobranza(
            factura_id=factura.id, fecha=date.today(), monto=liberar,
            medio=MEDIO_FACT_RETENCION, banco=fac.banco, numero_operacion=fac.id_operacion,
            observaciones="Liquidación retención factoring", usuario_id=getattr(current_user, "id", None),
        ))
    fac.estado = "liquidada"
    fac.fecha_liquidacion = date.today()
    fac.usuario_liquidacion_id = getattr(current_user, "id", None)
    db.flush()
    db.refresh(factura, with_for_update=True)
    # Totales desde la lectura BLOQUEANTE recargada (incluye la retención recién agregada)
    _recompute_factura(factura, cobranzas=_cobranzas_bloqueadas(db, factura.id))
    db.commit()
    db.refresh(factura)
    # guia_viva: N° de guía ACTUAL del despacho, no el snapshot congelado (ver _guias_vivas).
    return _serialize_factura(factura, guia_viva=_guia_viva_de(db, factura),
                              adelanto_id=_adelanto_id_de(db, factura))


@router.post("/facturas/{factura_id}/factoring/revertir")
def revertir_factoring(
    factura_id: int,
    payload: RevertirFactoringIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """CONTRAPARTIDA del guard SII del factoring: la salida —única y auditada— del caso en
    que una factura quedó cedida al factor contra un documento tributario que nunca existió.

    POR QUÉ EXISTE (el zombi imborrable). Hasta esta entrega, `set_factoring` de MonzaParts
    NO pedía folio del SII, así que se podía ceder al factor una factura que el SII nunca
    conoció. Con el guard recién puesto, esa fila queda cerrada por los CUATRO lados a la
    vez: no se puede liquidar (guard SII de `liquidar_factoring`), no se puede editar a 0
    (guard SII de `set_factoring`, que era la única forma que tenía el módulo de deshacer
    una cesión), no se puede eliminar la factura (`eliminar_factura` rechaza toda factura
    con factoring) y la aplicación automática de adelantos devuelve 0. Resultado: plata del
    factor amarrada a un documento inexistente y el cupo facturable de esa mercadería
    secuestrado PARA SIEMPRE. Cerrar la puerta de entrada sin abrir una de salida es dejar
    plata atrapada; eso no es aceptable, y es la razón de que ambas cosas vayan juntas.

    LA PUERTA ES EXACTAMENTE LA INVERSA DEL GUARD: se revierte SÓLO donde el guard bloquea
    (`_plata_bloqueada_por_sii` == True: factura, sin folio, con DTE que no está
    emitido-con-folio). Si el documento SÍ existe ante el SII, la cesión es un hecho
    financiero real y no se borra por acá: se liquida cuando el factor paga la retención, o
    se corrige volviendo a registrar el factoring (`set_factoring` es un upsert). UNA sola
    condición de apertura, derivada del mismo helper: no hay un segundo criterio capaz de
    desalinearse del guard.

    POR QUÉ BORRA LA FILA y no la marca 'revertida': mientras exista una fila en
    monza_cont_factoring, `eliminar_factura` sigue respondiendo 409 y la factura sigue siendo
    imborrable — o sea, el zombi seguiría vivo con otro nombre. Es además la convención de la
    casa para plata sin huella contable (`eliminar_cobranza` / `eliminar_factura`). El hecho
    NO se pierde: queda en `factura.observaciones` (visible en la ficha) y en el log del
    servidor, con motivo, montos, id de operación y usuario.

    QUÉ **NO** HACE: no toca las cobranzas del cliente (medio ≠ factoring; ésas se revierten
    una por una con su propio endpoint y sus propios candados) ni el DTE ni el registro
    tributario. Después de revertir, la factura queda borrable por el camino normal — que
    sigue exigiendo lo suyo: si el DTE puede existir ante el SII, `_bloqueo_dte_factura`
    sigue pidiendo intervención humana. La plata sale; el documento irreversible sigue
    necesitando un humano."""
    # Orden GLOBAL de locks de la casa Monza: cotización → factura (el mismo de
    # _finalizar_factura_emitida y eliminar_factura). Bloquear la factura primero
    # deadlockearía contra ellos.
    ref = (db.query(MonzaContFacturaCliente)
           .filter(MonzaContFacturaCliente.id == factura_id).first())
    if not ref:
        raise HTTPException(404, "Factura no encontrada")
    if ref.cotizacion_id:
        (db.query(MonzaCotizacion).filter(MonzaCotizacion.id == ref.cotizacion_id)
         .with_for_update().first())
    # populate_existing: se decide sobre `numero_factura`/`tipo_doc` (la apertura de la
    # puerta) y se reescriben totales — valores del FOR UPDATE, no del snapshot del request.
    factura = (db.query(MonzaContFacturaCliente)
               .filter(MonzaContFacturaCliente.id == factura_id)
               .populate_existing().with_for_update().first())
    if not factura:
        raise HTTPException(404, "Factura no encontrada")
    motivo = (payload.motivo or "").strip()
    if len(motivo) < 5:  # min_length de Pydantic no ve los espacios en blanco
        raise HTTPException(400, "Escribe el motivo de la reversión: queda registrado en la "
                                 "factura y es lo único que explica por qué desapareció la "
                                 "operación de factoring")
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
        conciliadas = (db.query(MonzaTesConciliacionIngreso)
                       .filter(MonzaTesConciliacionIngreso.cobranza_id.in_(
                           [c.id for c in del_cobs]))
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
    # [-60000:]: la columna es TEXT (65.535 bytes). Reversiones repetidas no pueden reventar
    # el UPDATE con un 500; si hay que cortar, se pierde lo MÁS VIEJO.
    factura.observaciones = ((previo + "\n" if previo else "") + nota)[-60000:]
    for c in del_cobs:
        db.delete(c)
    db.delete(fac)
    db.flush()
    # refresh bloqueante por FRESCURA de los montos (la nota de _cobranzas_bloqueadas). El
    # estado 'factorizada' lo decide `_recompute_factura` mirando `factura.factoring`: tras
    # el flush del DELETE, el lazy load de esa relación ya devuelve None.
    db.refresh(factura, with_for_update=True)
    _recompute_factura(factura,
                       cobranzas=[c for c in cobs if not _es_medio_factoring(c.medio)])
    db.commit()
    logger.warning("Factoring REVERTIDO factura Monza=%s %s", factura.id, traza)
    db.refresh(factura)
    return {**_serialize_factura(factura, guia_viva=_guia_viva_de(db, factura),
                                 adelanto_id=_adelanto_id_de(db, factura)),
            "factoring_revertido": traza}


@router.delete("/facturas/{factura_id}")
def eliminar_factura(
    factura_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Borrado SEGURO: se rechaza (409) si tiene factoring o cobranzas reales — primero
    hay que revertir esos pagos. El cascade borra solo las líneas.

    Fase 6: además se rechaza si la factura tiene emisión electrónica (DTE 33) viva o
    dudosa ante el SII — ver `_bloqueo_dte_factura`.
    Fase 7: y si es una factura de ANTICIPO ya descontada en otra factura."""
    # ORDEN GLOBAL DE LOCKS: cotización → factura → adelanto (el mismo de crear_factura
    # y eliminar_cobranza). El lock de la COTIZACIÓN lo pide la vía B: el guard de
    # "anticipo ya descontado" mira las líneas de OTRAS facturas de la misma venta, y el
    # único escritor de esas líneas —crear_factura— se serializa por la cotización. Con
    # locks disjuntos no hay exclusión mutua: se podía borrar el anticipo justo mientras
    # otro request le colgaba el descuento, y el conflicto salía como un IntegrityError
    # de la FK (500) en vez de este 409 explicado. Espejo de GA contabilidad.py.
    ref = (db.query(MonzaContFacturaCliente.cotizacion_id)
           .filter(MonzaContFacturaCliente.id == factura_id).first())
    if not ref:
        raise HTTPException(404, "Factura no encontrada")
    if ref[0]:
        (db.query(MonzaCotizacion).filter(MonzaCotizacion.id == ref[0])
         .populate_existing().with_for_update().first())
    factura = (
        db.query(MonzaContFacturaCliente)
        .options(selectinload(MonzaContFacturaCliente.cobranzas), selectinload(MonzaContFacturaCliente.factoring))
        .filter(MonzaContFacturaCliente.id == factura_id)
        # populate_existing: los guards deciden con los datos FRESCOS del SELECT ...
        # FOR UPDATE, no con la versión del identity map (lectura previa al lock).
        .populate_existing()
        .with_for_update(of=MonzaContFacturaCliente)
        .first()
    )
    if not factura:
        raise HTTPException(404, "Factura no encontrada")
    if factura.factoring:
        raise HTTPException(409, "La factura tiene una operación de factoring; no se puede eliminar")
    if any(not _es_medio_factoring(c.medio) for c in factura.cobranzas):
        raise HTTPException(409, "Revierta las cobranzas antes de eliminar la factura")
    # Vía B: una factura de anticipo YA DESCONTADA en la factura del despacho real no se
    # borra — dejaría ese descuento citando un folio inexistente y la venta facturada de
    # menos. Se explica aquí con un 409; la FK sin ondelete de anticipo_factura_id es el
    # segundo cinturón (a nivel de motor). Va ANTES del guard SII a propósito: ése TOCA el
    # ancla —la borra, o la desliga con un flush— y rechazar después obliga a confiar en
    # el rollback del cierre de la sesión para deshacerlo.
    # En Monza NO hay nada más que revertir: el vínculo adelanto↔factura de anticipo es
    # DERIVADO (no existe MonzaContAdelanto.factura_anticipo_id, ver models.py), así que
    # al desaparecer la factura el adelanto vuelve solo a la vía A. Grupo AM sí tiene que
    # limpiar esa columna porque allá el vínculo está guardado.
    if factura.es_anticipo:
        descontada = (db.query(MonzaContFacturaClienteItem)
                      .filter(MonzaContFacturaClienteItem.anticipo_factura_id == factura.id)
                      .first())
        if descontada:
            raise HTTPException(409, "La factura de anticipo ya fue descontada en otra "
                                     "factura; elimine primero esa factura")
    # Guard SII (Fase 6) ANTES del delete: además de proteger el documento tributario,
    # BORRA el ancla DTE cuando consta que el documento nunca nació, o la DESLIGA (sin
    # destruirla) cuando el documento existe en Wasabil. La FK
    # monza_wasabil_dte.factura_id es RESTRICT (sin ON DELETE, igual que en GA): sin ese
    # paso el DELETE de la factura revienta con IntegrityError 1451 → 500 en producción.
    _bloqueo_dte_factura(db, factura.id, getattr(current_user, "id", None))
    db.delete(factura)
    db.commit()
    return {"ok": True}


@router.get("/kpis")
def get_kpis(
    periodo: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Indicadores de cobranza (filtrables por `periodo`: semana|mes|anio sobre fecha
    de emisión)."""
    base = db.query(MonzaContFacturaCliente).options(*_FACTURA_EAGER)
    floor = periodo_floor(periodo)
    if floor is not None:
        # Pre-filtro grueso en SQL (reduce filas en RAM); _periodo_filter refina abajo.
        base = base.filter(or_(
            MonzaContFacturaCliente.fecha_emision.is_(None),
            MonzaContFacturaCliente.fecha_emision >= floor,
        ))
    facturas = [
        f for f in base.all()
        if _periodo_filter(f.fecha_emision or f.created_at, periodo)
    ]
    hoy = date.today()
    facturado = sum(_f(f.monto_bruto) for f in facturas)
    cobrado = sum(_f(f.monto_pagado) for f in facturas)
    cobrado_cliente = sum(_f(c.monto) for f in facturas for c in f.cobranzas
                          if not _es_medio_factoring(c.medio))
    por_cobrar = sum(_f(f.saldo) for f in facturas if _f(f.saldo) > TOL)
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
