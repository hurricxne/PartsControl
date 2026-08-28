"""API del módulo Tesorería MonzaParts.

Prefijo: /api/monza/tesoreria (montado sin prefix; el router ya lo trae).
SOLO MonzaParts: candado require_empresa("automotriz") a nivel de router.

4 sub-áreas (cada una con su bloque de endpoints, en este orden):
  1. APROBACIONES  — cola de adelantos 50% informados por Comercial; Tesorería DA LA
     ORDEN (escribe monza_cont_adelanto + adelanto_verificado=1, misma regla de negocio
     que el endpoint histórico de Ventas-Contab; el cortafuego de Abastecimiento no
     cambia) y APLICA el adelanto de inmediato a las facturas ya emitidas de la venta
     (espejo GA; si la factura viene después, la aplica crear_factura de contabilidad).
     Incluye sugerencia de abono en cartola si ya llegó la plata.
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
import logging
from collections import Counter
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import (
    APIRouter, BackgroundTasks, Depends, HTTPException, UploadFile, File, Form,
)
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
from monza_contabilidad.service import (
    # TOL_PAGO (holgura del tope) y TOL (tolerancia de plata) ya NO se importan acá: los
    # dos guards que los usaban se mudaron a validar_venta_para_adelanto /
    # validar_adelanto_editable, unas líneas más abajo. Dejarlos importados sin usar haría
    # creer que este módulo todavía decide topes de adelanto por su cuenta.
    estado_adelanto, _f as _fc, MEDIO_ADELANTO,
    _recompute_factura,
    # Estado del adelanto: los predicados y la transición viven en Contabilidad (una sola
    # fuente de verdad, igual que el resto de la regla del adelanto). Acá se LEE el estado
    # y se REACTIVA la fila anulada que Tesorería vuelve a aprobar.
    ADEL_ANULADO, ADEL_APROBADO, adelanto_activo, reactivar_adelanto,
)
# Reglas de REGISTRO del adelanto (precondiciones de la venta/monto y los 2 candados de
# plata de la fila): una sola fuente de verdad compartida con el gemelo de Contabilidad.
# Aprobar en Tesorería y verificar en Contabilidad son la MISMA regla de negocio sobre la
# MISMA fila; tenerla dos veces es la deuda M5 y ya costó un arreglo a medias (M2: el 400
# del adelanto no pactado se quitó allá y quedó vivo acá). Import del ROUTER de otro módulo
# Monza: mismo patrón que `_crear_egreso` de monza_compras_contab, unas líneas más abajo.
from monza_contabilidad.router import (
    validar_venta_para_adelanto, validar_adelanto_editable,
)
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

logger = logging.getLogger(__name__)

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


def _correr_matcher_post_cartola(usuario_id: Optional[int]) -> None:
    """Corrida del matcher banco↔libro DESPUÉS de responder la importación de cartola.

    Hallazgo #4 (revisión 2026-08-08): esto corría EN LÍNEA dentro del POST. El motor
    arma el contexto completo DOS veces (todos los movimientos CLP sin cota de fecha,
    todos los docs activos, todos los egresos con detalle), re-verifica confirmados,
    caduca zombis y escribe propuestas con un commit por propuesta — «minutos de trabajo
    pesado», palabras del propio autor cuando lo sacó del job de alertas por la misma
    razón. El operador pagaba esa espera mirando el spinner (en Monza, sin timeout de
    cliente, girando indefinidamente). Peor: el endpoint es `async def` con Session
    SÍNCRONA, así que la corrida bloqueaba el EVENT LOOP — no demoraba solo al que
    importaba, congelaba todas las peticiones del proceso. Y el costo crece para
    siempre: la tabla de movimientos solo engorda.

    Como BackgroundTask, Starlette la corre en el threadpool DESPUÉS de mandar la
    respuesta: el operador ve su cartola al instante y el motor trabaja igual. Sesión
    PROPIA (molde `scheduler._matcher_post_barrido_monza`): la del request ya está
    cerrada cuando esto corre. Todo atrapado adentro: un fallo del motor JAMÁS toca una
    importación ya commiteada, y queda en el log y en la bitácora de corridas que el
    tablero del Libro SII muestra.
    """
    try:
        from database import SessionLocal
        from monza_wasabil_compras.matcher import correr_matcher
        db = SessionLocal()
        try:
            correr_matcher(db, origen="post_cartola", usuario_id=usuario_id)
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001 — incluye MatcherEnCurso e ImportError
        logger.warning("El matcher del libro SII no corrió tras la importación de "
                       "cartola: %s: %s", type(e).__name__, e)

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


def _con_retry_deadlock(db: Session, operacion):
    """Ejecuta `operacion()` reintentando ante deadlock / lock-timeout de InnoDB
    (1213 / 1205).

    HALLAZGO #1 de la auditoría integral: conciliar una cobranza y registrar/eliminar
    otra cobranza de la MISMA factura tocan las mismas filas (factura + cobranzas) y
    MySQL puede elegir una víctima. Sin esta red el operador recibe un 500 en una
    operación diaria. Mismo patrón (y mismo texto de 409) que
    monza_recepcion_nacional/router.py: se reintenta la transacción COMPLETA tras
    db.rollback() y, si a la tercera sigue el conflicto, se responde 409 accionable.
    Es la CAPA 2 del arreglo: la CAPA 1 (orden de locks factura → cobranza en
    `_conciliar_tx`) es la que quita la causa raíz; esto es la red de seguridad.

    DEUDA CONOCIDA: este helper ya existe idéntico en monza_recepcion_nacional (y en
    los módulos que reintentan cierres de despacho). Corresponde consolidarlo en un
    módulo compartido, pero esa extracción toca varios paquetes a la vez y se deja
    para el commit de consolidación, no para el arreglo del hallazgo."""
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


def _match_libro_confirmado(db, mov_ids):
    """Hallazgo #9 (revisión matcher 2026-08-06): un match confirmado del libro SII
    'vive' sobre el flag mov.conciliado, pero desconciliar/eliminar solo miraban el
    flag — apagarlo dejaba el match confirmado huérfano: el mov reaparecía en
    pendientes y la MISMA plata podía explicar un doc del libro Y otra conciliación,
    o el DELETE dejaba (FK SET NULL) un match confirmado sin movimiento, rompiendo
    el '409 con match vivo' de la regla 5. Devuelve el id del primer match
    CONFIRMADO vivo de esos movimientos, o None. Espejo de tesoreria/router.py (GA).

    ImportError → None (paquete del libro no desplegado: no puede existir un match).
    ProgrammingError (tabla sin migrar) → None por la MISMA razón: sin tabla no hay
    matches que proteger — no es un fail-open."""
    if not mov_ids:
        return None
    try:
        from monza_wasabil_compras.models import MonzaSiiLibroMatch
    except ImportError:
        # AUSENTE vs ROTO — la distinción es todo (lente de ciclo de vida 2026-08-08).
        # Si el paquete no está desplegado, no puede haber matches y devolver None es
        # correcto. Pero si el paquete SÍ está y su import revienta (un deploy a medias,
        # una dependencia que faltó), los matches EXISTEN en la base y este guard estaría
        # diciendo «no hay nada que proteger» sin haber mirado: eso es fallar ABIERTO, y
        # un guard que falla abierto es peor que no tener guard — deja borrar el
        # movimiento que sostiene un match confirmado y rompe la cadena entera.
        # Ante la duda se BLOQUEA y se pide un humano, nunca se adivina.
        import importlib.util
        try:
            presente = importlib.util.find_spec("monza_wasabil_compras") is not None
        except Exception:            # noqa: BLE001 — si ni eso se puede saber, se bloquea
            presente = True
        if not presente:
            return None
        raise HTTPException(503, (
            "El módulo del Libro SII está instalado pero no se pudo cargar, así que no "
            "se puede comprobar si este movimiento sostiene una conciliación con el "
            "libro. No se borra nada hasta que alguien de sistemas lo revise."))
    from sqlalchemy.exc import ProgrammingError
    try:
        fila = (db.query(MonzaSiiLibroMatch.id)
                .filter(MonzaSiiLibroMatch.movimiento_id.in_(list(mov_ids)),
                        MonzaSiiLibroMatch.estado == "confirmado").first())
    except ProgrammingError as e:
        db.rollback()
        # AUSENTE vs ROTO otra vez, ahora del lado del esquema. Si la TABLA no existe
        # (MySQL 1146: falta correr el init_db del libro) no puede haber ni un match, y
        # devolver None es la respuesta correcta. Cualquier OTRO error de esquema —el
        # caso tipico es 1054, columna desconocida tras una migracion a medias— significa
        # que la tabla SI esta, con filas adentro, y que no pudimos mirarlas: devolver
        # None ahi seria fallar ABIERTO y dejar borrar el movimiento que sostiene un cruce
        # confirmado. Ante un dato que no se pudo leer, se BLOQUEA y se pide un humano.
        detalle = str(getattr(e, "orig", None) or e).lower()
        if "1146" in detalle or "doesn't exist" in detalle or "does not exist" in detalle:
            return None
        raise HTTPException(503, (
            "No se pudo comprobar si este movimiento sostiene una conciliacion con el "
            "libro del SII (la consulta al cruce fallo). No se borra nada hasta que "
            "alguien de sistemas lo revise."))
    return fila[0] if fila else None


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


def _sin_anulados(q):
    """Excluye de una query sobre MonzaContAdelanto los adelantos ANULADOS.

    Un adelanto anulado NO es plata recibida: no se sugiere ni se concilia contra un
    abono, no suma en el flujo de caja ni en los KPIs, y su venta VUELVE a la cola de
    aprobación (la fila no se borra — el UNIQUE deja un adelanto por venta y se REUSA).
    Mismo filtro que monza_contabilidad._adelanto_de_cot, incluido el coalesce: una fila
    legada con la columna en NULL es un adelanto verificado, jamás uno anulado (leerlo al
    revés apagaría el cortafuego de Abastecimiento de todas las ventas viejas)."""
    return q.filter(func.coalesce(MonzaContAdelanto.estado, ADEL_APROBADO) != ADEL_ANULADO)


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


def _aplicar_adelanto_a_facturas(db, cot, adel, usuario_id=None, advertencias=None) -> float:
    """Aplica RETROACTIVAMENTE el adelanto recién aprobado a las facturas YA emitidas
    de la venta (cobranza medio='adelanto'), espejo de la aplicación inmediata de
    tesoreria/router.py (GA). Si la factura viene DESPUÉS, la aplica crear_factura de
    monza_contabilidad — ambas direcciones cubiertas. Devuelve el total aplicado ahora.

    UNA SOLA FUENTE DE VERDAD: la regla de plata por factura NO se reimplementa acá; se
    delega en `monza_contabilidad._aplicar_adelanto`, igual que Grupo AM delega en
    `routers.contabilidad._aplicar_adelantos_pendientes` (tesoreria/router.py). Antes
    Tesorería tenía su propio loop —gemelo línea a línea del de Contabilidad— y los dos
    ya habían empezado a divergir: el de acá filtraba por la columna `saldo` PERSISTIDA
    (una factura con el saldo cacheado en 0 salía del reparto y el cliente seguía viendo
    deuda ya pagada) y su guard SII no miraba `tipo_doc` (una BOLETA sin folio consultaba
    el módulo DTE sin necesidad y, en un deploy a medias, respondía 503 justo en la
    aprobación que destraba Abastecimiento). Con la delegación las dos puertas quedan
    gobernadas por el mismo código y el próximo guard que se agregue allá vale acá.

    Lo que aporta este envoltorio (y por eso sigue existiendo):
      · el ORDEN de las facturas: las de ANTICIPO (vía B) PRIMERO y recién después el
        FIFO por id. La factura de anticipo es la que RECIBE la plata del adelanto (la
        del despacho real lleva la línea de DESCUENTO, no una cobranza), así que al
        saldarla en esta misma pasada el EXCEDENTE —cuando el adelanto supera el bruto
        del anticipo— queda libre para las facturas del despacho real dentro de la MISMA
        transacción. Con el orden por id a secas ese excedente quedaba atrapado y la
        deuda del cliente quedaba sobrestimada. Mantener en sync con
        monza_contabilidad.verificar_adelanto (mismo order_by).
        COALESCE: en DESC MySQL manda los NULL al FINAL, así que una fila legada con
        es_anticipo NULL perdería contra un 0 y el orden quedaría invertido (la plata
        entraría a la factura equivocada). La migración deja 0 y el ORM siempre escribe
        0, pero una tabla nacida de create_all queda sin DEFAULT: cinturón y tirantes.
      · el recálculo de saldo/estado por factura desde las cobranzas BAJO LOCK.
      · el TOTAL aplicado ahora, que el endpoint devuelve como `aplicado_ahora_clp`. Se
        mide como DELTA de `monto_aplicado` (no sumando los aplicados uno por uno): así
        también refleja el re-encauce de la vía B, que puede DEVOLVER plata de otra
        factura antes de aplicarla al anticipo.

    Reglas de plata (docs/regla-lecturas-de-plata.md), todas dentro del helper delegado:
      · Se llama con la COTIZACIÓN y el ADELANTO ya bloqueados por el caller (orden
        global de locks: cotización → factura → adelanto, el mismo de crear_factura y
        eliminar_cobranza en monza_contabilidad).
      · Cada factura se relee populate_existing + with_for_update (serializa contra
        cobranzas concurrentes) y su saldo se recalcula desde cobranzas BAJO LOCK,
        no desde el saldo persistido.
      · Cap por SALDO ACTUAL de la factura (no por monto_bruto): sobre una factura
        con cobranzas parciales previas, el cap por bruto sobre-aplicaría.
      · Facturas con factoring VIGENTE se SALTAN, y una FACTURA electrónica sin folio
        con su DTE 33 sin emitir tampoco recibe plata (guard SII).

    INVARIANTE: adel.monto_aplicado == Σ cobranzas medio='adelanto' de la venta.
    """
    # Import LOCAL: evita el ciclo contabilidad ↔ tesorería (monza_contabilidad.router
    # importa monza_tesoreria.models), mismo patrón que el resto de los cruces del módulo.
    from monza_contabilidad.router import _aplicar_adelanto, _cobranzas_bloqueadas
    antes = _fc(adel.monto_aplicado)
    facturas = (db.query(MonzaContFacturaCliente)
                .filter(MonzaContFacturaCliente.cotizacion_id == cot.id)
                .order_by(func.coalesce(MonzaContFacturaCliente.es_anticipo, 0).desc(),
                          MonzaContFacturaCliente.id.asc())
                .populate_existing().with_for_update().all())
    for factura in facturas:
        _aplicar_adelanto(db, cot, factura, usuario_id, advertencias=advertencias)
        db.flush()
        _recompute_factura(factura, cobranzas=_cobranzas_bloqueadas(db, factura.id))
    return round(_fc(adel.monto_aplicado) - antes, 2)


# ═══ 1. APROBACIONES (adelantos 50% → la orden la da Tesorería) ═════════════════
@router.get("/aprobaciones")
def listar_aprobaciones(page: int = 1, page_size: int = PAGE_SIZE_DEFAULT,
                        db: Session = Depends(get_db),
                        current_user: User = Depends(get_current_user)):
    """Cola de aprobación de adelantos.
      · por_aprobar: ventas con pct_adelanto>0 aún SIN la orden de Tesorería, con el
        monto sugerido y, si ya llegó la plata, el abono de cartola que calza.
      · aprobadas: historial (adelanto registrado), con su estado de conciliación
        bancaria derivado del enlace en monza_tes_conciliacion. Incluye los adelantos NO
        PACTADOS (pct_adelanto = 0): son plata recibida igual, y filtrarlos por % los
        dejaba registrables pero invisibles en esta pestaña.

    PAGINADO (T14): las dos colas se cortan en SQL con `page`/`page_size` — antes traía
    TODAS las ventas con adelanto informado de la historia y las serializaba en memoria,
    así que la pestaña se degradaba sin techo a medida que crece el histórico. Cada lista
    va con su total (`total_por_aprobar` / `total_aprobadas`), y `n_por_aprobar` sigue
    siendo el TOTAL de la cola (no el largo de la página): es el badge del encabezado.

    Una venta cuyo adelanto fue ANULADO vuelve a `por_aprobar`: la fila del adelanto
    sigue existiendo (Monza tiene UN adelanto por venta y anular no la borra), pero un
    adelanto anulado no es plata recibida — ver `_sin_anulados`."""
    page = max(1, int(page))
    page_size = min(max(1, int(page_size)), PAGE_SIZE_MAX)
    # Ventas con adelanto informado por Comercial, partidas en dos colas EN SQL según
    # exista o no un adelanto VIGENTE (los anulados no cuentan: la venta vuelve a la cola
    # de aprobación). Subquery de ids en vez de outerjoin: el UNIQUE por cotización
    # garantiza a lo más una fila, así que un IN/NOT IN no puede duplicar ni perder filas.
    con_adelanto = _sin_anulados(db.query(MonzaContAdelanto.cotizacion_id)).scalar_subquery()
    base_cots = (db.query(MonzaCotizacion)
                 .options(joinedload(MonzaCotizacion.cliente))
                 .filter(MonzaCotizacion.estado.in_(ESTADOS_VENTA)))
    # `por_aprobar` es una EXPECTATIVA (falta la plata), así que sigue pidiendo el % que
    # informó Comercial: sin pct_adelanto > 0 no hay monto que esperar. OJO (arreglos del
    # equipo 2026-08-21): las ventas AL CONTADO ahora llegan con pct 100 A PROPÓSITO —
    # que entren a esta cola por el total ES el comportamiento pedido (contado también
    # exige verificación de Tesorería antes de comprar). El filtro pct > 0 sigue siendo
    # correcto porque excluye solo el crédito puro; no lo "arregles" filtrando el contado.
    q_pa = base_cots.filter(MonzaCotizacion.pct_adelanto > 0,
                            ~MonzaCotizacion.id.in_(con_adelanto))
    # `aprobadas` es un HECHO (la plata ya está registrada) y por eso NO filtra por
    # pct_adelanto: el adelanto NO PACTADO (M2, pct = 0) es plata recibida de verdad y con
    # el filtro quedaba fuera de la pestaña — registrable pero invisible en la cola donde
    # se administra, mientras sí aparecía en /adelantos-pendientes para conciliar.
    q_ap = base_cots.filter(MonzaCotizacion.id.in_(con_adelanto))
    total_pa, total_ap = q_pa.count(), q_ap.count()
    off = (page - 1) * page_size
    cots_pa = q_pa.order_by(MonzaCotizacion.id.desc()).offset(off).limit(page_size).all()
    cots_ap = q_ap.order_by(MonzaCotizacion.id.desc()).offset(off).limit(page_size).all()
    cots = cots_pa + cots_ap
    ids_pa = {c.id for c in cots_pa}
    adelantos = {a.cotizacion_id: a for a in
                 _sin_anulados(db.query(MonzaContAdelanto)
                               .filter(MonzaContAdelanto.cotizacion_id.in_([c.id for c in cots]))).all()} if cots else {}
    conciliados = _adelantos_conciliados_ids(db, [a.id for a in adelantos.values()])

    # Abonos de cartola NO conciliados (para sugerir el match del adelanto que llega).
    abonos = (db.query(MonzaTesMovimiento)
              .filter(MonzaTesMovimiento.tipo == "abono", MonzaTesMovimiento.conciliado.is_(False))
              .order_by(MonzaTesMovimiento.fecha.desc()).all())

    por_aprobar, aprobadas = [], []
    for cot in cots:
        # La cola la manda la QUERY, no la existencia del objeto: una venta de `q_pa`
        # nunca tiene adelanto vigente, y así la fila anulada no se serializa como
        # "aprobada" ni entrega su monto como si fuera plata en el banco.
        adel = None if cot.id in ids_pa else adelantos.get(cot.id)
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
            # n_por_aprobar = TOTAL de la cola (badge del encabezado), no el largo de la
            # página: con una sola página coincide con el valor histórico.
            "n_por_aprobar": int(total_pa),
            "total_por_aprobar": int(total_pa), "total_aprobadas": int(total_ap),
            "page": page, "page_size": page_size}


@router.post("/aprobaciones/{cot_id}/aprobar")
def aprobar_adelanto(cot_id: int, payload: AprobarAdelantoIn,
                     db: Session = Depends(get_db),
                     current_user: User = Depends(get_current_user)):
    """TESORERÍA da la orden: registra el adelanto recibido (monto/fecha/banco/N° op),
    marca adelanto_verificado=1 → Abastecimiento queda destrabado (cortafuego) y APLICA
    el adelanto DE INMEDIATO a las facturas YA emitidas de la venta (cobranza
    medio='adelanto'; si la factura viene después, la aplica crear_factura — ambas
    direcciones cubiertas, espejo de tesoreria/router.py de Grupo AM).

    MISMA regla de negocio que POST /api/monza/contabilidad/ventas/{id}/adelanto/verificar
    (monza_contabilidad/router.py); mantener ambas en sync. Lock de la cotización:
    serializa aprobaciones concurrentes (el UNIQUE por cotización es la última defensa)."""
    cot = (db.query(MonzaCotizacion)
           .filter(MonzaCotizacion.id == cot_id)
           .with_for_update(of=MonzaCotizacion)
           .first())
    if not cot:
        raise HTTPException(404, "Venta (cotización) no encontrada")
    # M2 — ADELANTO NO PACTADO: acá había un 400 «Esta venta no tiene un adelanto informado
    # por Comercial» cuando pct_adelanto era 0. Se quitó primero en el gemelo de
    # Contabilidad y esta copia quedó viva: en MonzaParts la ORDEN del adelanto la da
    # TESORERÍA (Ventas quedó de solo lectura), o sea que el arreglo había quedado en la
    # puerta que el operador NO usa y el depósito no pactado seguía sin poder registrarse.
    # Ahora las dos puertas comparten la regla —validar_venta_para_adelanto, en
    # monza_contabilidad/router.py— así que no pueden volver a divergir: es la deuda M5
    # (dos copias de una regla de plata) cerrada hacia UNA fuente de verdad.
    validar_venta_para_adelanto(cot, payload.monto, verbo="aprobar")
    # Lectura BLOQUEANTE del adelanto (espejo GA tesoreria/router.py): sin el lock,
    # conciliar (que SÍ bloquea la fila del adelanto) puede colarse entre estos guards
    # y la escritura (TOCTOU) y los guards decidirían sobre datos viejos.
    # populate_existing: sin él, el identity map serviría una lectura previa al lock.
    # Orden de locks cotización → adelanto: conciliar solo toma movimiento → adelanto,
    # nunca la cotización, así que no introduce deadlocks nuevos.
    adel = (db.query(MonzaContAdelanto)
            .filter(MonzaContAdelanto.cotizacion_id == cot_id)
            .populate_existing().with_for_update().first())
    # Los 2 candados de plata sobre la fila (ya aplicado a una factura / ya conciliado con
    # un abono del banco) son la MISMA regla que el gemelo de Contabilidad y viven allá:
    # validar_adelanto_editable. Idéntico comportamiento observable (mismos 409, mismos
    # textos, mismo orden) — lo que cambia es que ya no hay dos copias que mantener.
    validar_adelanto_editable(db, adel)
    if adel is None:
        adel = MonzaContAdelanto(cotizacion_id=cot_id)
        db.add(adel)
    # ESTADO: aprobar (o RE-aprobar) deja el adelanto VIGENTE. Es el único camino de vuelta
    # desde 'anulado': Monza tiene UN adelanto por venta (uq_monza_cont_adelanto_cotizacion),
    # así que anular NO borra la fila y no se puede crear otra — la fila se REUSA. Sin este
    # paso el adelanto quedaría 'anulado' con plata dentro: invisible para todos los
    # caminos que filtran anulados y con adelanto_verificado=1 destrabando Abastecimiento.
    # La transición vive en monza_contabilidad.service (misma llamada que hace su gemelo
    # verificar_adelanto: los dos deben mantenerse en sync).
    if reactivar_adelanto(adel):
        logger.info("Adelanto reactivado por Tesorería (venta %s): anulado → aprobado", cot_id)
    adel.monto = payload.monto
    # Fecha estricta: explícita pero no parseable → 400 (antes caía en SILENCIO a hoy,
    # registrando el adelanto con una fecha que el operador no ingresó; espejo GA).
    adel.fecha_pago = _fecha(payload.fecha_pago, "fecha_pago") or date.today()
    adel.banco = payload.banco
    adel.numero_operacion = payload.numero_operacion
    # Solo si vino en el payload: re-aprobar (corregir monto) SIN mandar observaciones
    # no debe borrar las existentes (espejo GA tesoreria/router.py).
    if payload.observaciones is not None:
        adel.observaciones = payload.observaciones
    adel.usuario_id = getattr(current_user, "id", None)
    cot.adelanto_verificado = 1
    db.flush()
    # Aplicación RETROACTIVA a las facturas que ya existen (el lock de la cotización ya
    # está tomado; el helper bloquea factura por factura). Antes de esto, la plata de un
    # adelanto aprobado DESPUÉS de facturar nunca descontaba el saldo de la factura.
    # `advertencias`: la aplicación puede tener que RE-ENCAUZAR plata de otra factura a la
    # de anticipo y quedarse corta (factoring vigente, DTE sin emitir, cobranza ya
    # conciliada). Ese aviso es la única señal de que la factura de anticipo quedó por
    # cobrar: se devuelve al operador en vez de descartarse.
    advertencias: list = []
    aplicado_ahora = _aplicar_adelanto_a_facturas(
        db, cot, adel, usuario_id=getattr(current_user, "id", None),
        advertencias=advertencias)
    db.commit()
    db.refresh(cot)
    db.refresh(adel)
    return {**estado_adelanto(cot, adel), "aplicado_ahora_clp": round(aplicado_ahora, 2),
            "advertencias": advertencias}


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
    background: BackgroundTasks,
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
    # Hallazgos #10 y #16 (revisión matcher 2026-08-06): la spec §5 del matcher
    # banco↔libro exige que el motor corra «tras cada importación de cartola» — solo
    # existía el botón manual y las sugerencias nacían recién con un click humano.
    # Hallazgo #4 (revisión 2026-08-08): la corrida se agenda, NO se espera — el porqué
    # completo está en `_correr_matcher_post_cartola`. El operador recibe su cartola de
    # inmediato; el motor corre después, con su propia sesión.
    background.add_task(_correr_matcher_post_cartola,
                        getattr(current_user, "id", None))
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
    # Hallazgo #9 (revisión matcher 2026-08-06): el conteo de conciliados no basta —
    # un mov con match del libro SII confirmado pero flag apagado (desconciliar
    # histórico) pasaría, y el borrado dejaría matches CONFIRMADOS sin movimiento.
    mov_ids_cartola = [r[0] for r in db.query(MonzaTesMovimiento.id)
                       .filter(MonzaTesMovimiento.cartola_id == cartola.id).all()]
    match_id = _match_libro_confirmado(db, mov_ids_cartola)
    if match_id is not None:
        raise HTTPException(
            # H2: el texto viejo («descártelo en la bandeja del Libro SII») mandaba a una
            # bandeja que NO listaba los confirmados: el operador iba, no encontraba nada
            # y quedaba en un punto muerto. Ahora se nombra el camino exacto que existe.
            409, "La cartola tiene movimiento(s) con un cruce CONFIRMADO con el libro de "
                 f"compras del SII (N° {match_id}). Antes de borrarla: entre a Libro SII "
                 f"→ Conciliación bancaria → pestaña «Conciliados», busque el N° "
                 f"{match_id} y use «Deshacer conciliación».")
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
    # Hallazgo #9 (revisión matcher 2026-08-06): el flag no basta — un desconciliar
    # histórico pudo apagarlo con el match del libro SII aún confirmado, y el DELETE
    # dejaría (FK SET NULL) un match CONFIRMADO sin movimiento: exactamente el
    # 'borrar mov con match vivo = 409' que promete la regla 5.
    match_id = _match_libro_confirmado(db, [mov.id])
    if match_id is not None:
        raise HTTPException(
            # H2: el camino concreto, no una bandeja donde ese cruce no aparecía.
            409, f"El movimiento tiene el cruce N° {match_id} CONFIRMADO con el libro de "
                 "compras del SII. Antes de borrarlo: entre a Libro SII → Conciliación "
                 f"bancaria → pestaña «Conciliados», busque el N° {match_id} y use "
                 "«Deshacer conciliación».")
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

    # abono → adelantos VIGENTES con monto ≈ y sin enlace previo (plus Monza). Los
    # ANULADOS no se sugieren: la plata de un adelanto anulado no está comprometida con
    # esa venta, y cruzar el abono contra él dejaría el depósito amarrado a un registro
    # que ya no cuenta para nadie (ni para Abastecimiento ni para el flujo de caja).
    adels = (_sin_anulados(db.query(MonzaContAdelanto))
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
        'conciliado' de la cobranza se deriva); marca el movimiento conciliado.

    HALLAZGO #1 (CAPA 2): el cuerpo vive en `_conciliar_tx` para poder reintentarlo
    completo si InnoDB elige esta transacción como víctima de un deadlock (1213) con
    Facturas y Cobranzas. Mismo patrón que monza_recepcion_nacional/router.py."""
    return _con_retry_deadlock(db, lambda: _conciliar_tx(mov_id, payload, db, current_user))


def _conciliar_tx(mov_id: int, payload: ConciliarIn, db: Session, current_user: User):
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
        # Snapshot ANTES de enriquecer: mientras el egreso está conciliado la cartola
        # es la fuente de verdad (fecha/ref del banco pisan lo del egreso), pero
        # desconciliar debe poder RESTAURAR lo que el operador tenía ingresado
        # (espejo GA tesoreria/router.py).
        db.add(MonzaTesConciliacion(movimiento_id=mov.id, egreso_id=egreso.id,
                                    monto_conciliado_clp=_f(mov.monto), usuario_id=uid,
                                    fecha_egreso_previa=egreso.fecha_mov_bancario,
                                    referencia_egreso_previa=egreso.referencia_bancaria))
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
        # populate_existing: la decisión de abajo (¿está anulado? ¿coincide el monto?) es
        # una decisión de PLATA, así que se toma sobre la fila FRESCA bajo el lock — sin
        # él el identity map podría servir una lectura previa (docs/regla-lecturas-de-plata.md).
        adel = (db.query(MonzaContAdelanto)
                .filter(MonzaContAdelanto.id == payload.adelanto_id)
                .populate_existing().with_for_update().first())
        if not adel:
            raise HTTPException(404, "Adelanto no encontrado")
        # Adelanto ANULADO: su plata dejó de estar comprometida con la venta, así que no
        # es un destino válido para el abono. Se lee BAJO EL LOCK de arriba, de modo que
        # una anulación concurrente no se cuela entre el guard y el enlace.
        if not adelanto_activo(adel):
            raise HTTPException(409, "Ese adelanto está anulado; vuelva a aprobarlo en la "
                                     "pestaña Aprobaciones antes de conciliar el abono")
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
        #
        # HALLAZGO #1 (CAPA 1 — orden de locks). Antes esto era UN JOIN con
        # with_for_update(): la tabla motriz era la COBRANZA (se busca por su PK) y la
        # factura se bloqueaba DESPUÉS. Facturas y Cobranzas bloquea al revés —
        # primero la FACTURA (with_for_update(of=FacturaCliente)) y luego sus
        # cobranzas — así que las dos operaciones formaban un ciclo InnoDB y el
        # operador recibía un 500 por deadlock (1213) en una operación diaria.
        # Ahora se sigue el orden canónico de la casa (cotización → factura →
        # cobranza) en tres pasos, y como al soltar el JOIN atómico se abre una
        # ventana entre la lectura sin lock y el lock real, el paso 4 REVALIDA todo
        # con los datos frescos (no es opcional).
        #
        # 1) lectura SIN lock, solo para saber a qué factura pertenece la cobranza.
        #    Se pide la columna suelta (no la entidad) para no dejar la fila en el
        #    identity map con valores viejos que la relectura bajo lock no pisaría.
        factura_id_previa = (db.query(MonzaContCobranza.factura_id)
                             .filter(MonzaContCobranza.id == payload.cobranza_id).scalar())
        if factura_id_previa is None:
            raise HTTPException(404, "Cobranza no encontrada")
        # 2) PRIMERO la factura (mismo orden que Facturas y Cobranzas).
        _factura = (db.query(MonzaContFacturaCliente)
                    .filter(MonzaContFacturaCliente.id == factura_id_previa)
                    .with_for_update().first())
        if not _factura:
            raise HTTPException(404, "Cobranza no encontrada")
        # 3) DESPUÉS la cobranza.
        cobranza = (db.query(MonzaContCobranza)
                    .filter(MonzaContCobranza.id == payload.cobranza_id)
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
    # Hallazgo #9 (revisión matcher 2026-08-06): si el flag lo sostiene un match
    # CONFIRMADO del libro SII, desconciliar lo dejaría 'confirmado' con el mov
    # libre — la misma plata podría explicar el doc del libro Y otro egreso. El
    # camino es descartar el match en la bandeja del Libro SII (eso libera el flag
    # vía _liberar_conciliado_si_es_del_match) y recién ahí desconciliar.
    match_id = _match_libro_confirmado(db, [mov.id])
    if match_id is not None:
        raise HTTPException(
            # H2: el camino concreto, no una bandeja donde ese cruce no aparecía.
            409, f"El movimiento tiene el cruce N° {match_id} CONFIRMADO con el libro de "
                 "compras del SII. Primero entre a Libro SII → Conciliación bancaria → "
                 f"pestaña «Conciliados», busque el N° {match_id}, use «Deshacer "
                 "conciliación» y reintente.")
    for link in list(mov.conciliaciones):
        if link.egreso_id:
            eg = (db.query(MonzaContEgreso)
                  .filter(MonzaContEgreso.id == link.egreso_id)
                  .with_for_update().first())
            if eg:
                eg.conciliado = False
                eg.conciliado_at = None
                # Se RESTAURA lo que el egreso tenía ANTES del cruce (snapshot guardado
                # en el enlace al conciliar): así no queda data del cruce equivocado y
                # se conserva lo que el operador ingresó a mano en Compras, incluso si
                # coincidía con lo del banco (la igualdad de valores no distingue el
                # origen del dato). Solo se toca el campo que la conciliación pisó (mov
                # con fecha/ref); en enlaces previos a la migración el snapshot es NULL
                # y el resultado es el mismo que la limpieza histórica. Espejo GA
                # tesoreria/router.py.
                if mov.fecha:
                    eg.fecha_mov_bancario = link.fecha_egreso_previa
                if mov.referencia:
                    eg.referencia_bancaria = link.referencia_egreso_previa
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
    """Adelantos APROBADOS aún sin conciliar con un abono del banco (los ANULADOS no:
    esa plata ya no está comprometida con la venta)."""
    adels = (_sin_anulados(db.query(MonzaContAdelanto))
             .order_by(MonzaContAdelanto.id.desc()).all())
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
    # isnot(None): defensa espejo de GA — si algún día el enlace admite otro destino con
    # cobranza_id NULL, un NOT IN contra una lista con NULLs no matchea NADA en SQL
    # (vaciaría la cola de pendientes en silencio).
    ya_conciliadas = (db.query(MonzaTesConciliacionIngreso.cobranza_id)
                      .filter(MonzaTesConciliacionIngreso.cobranza_id.isnot(None))
                      .scalar_subquery())
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

    # Adelantos APROBADOS aún no aplicados a facturas: plata YA recibida (no es entrada
    # futura, por eso va APARTE de los buckets); se muestra para explicar por qué las
    # facturas siguientes nacerán con menos saldo (espejo GA tesoreria/router.py).
    # Los ANULADOS quedan fuera: un adelanto anulado no es plata en la cuenta esperando
    # su factura, y sumarlo acá inflaba el disponible del flujo de caja.
    adel_apr = _sin_anulados(db.query(MonzaContAdelanto)).all()
    apr_pend = [a for a in adel_apr if _f(a.monto) - _f(a.monto_aplicado) > TOL]
    apr_pend_monto = sum(_f(a.monto) - _f(a.monto_aplicado) for a in apr_pend)

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
        "adelantos_recibidos_sin_aplicar": {"n": len(apr_pend), "monto": round(apr_pend_monto, 0)},
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
    # Cobranzas sin conciliar (el 'conciliado' se deriva del enlace en la tabla nueva).
    # isnot(None): defensa espejo de GA — un NOT IN contra una lista con NULLs no
    # matchea NADA en SQL (vaciaría el KPI en silencio).
    ya_conciliadas = (db.query(MonzaTesConciliacionIngreso.cobranza_id)
                      .filter(MonzaTesConciliacionIngreso.cobranza_id.isnot(None))
                      .scalar_subquery())
    cobranzas_pend = (db.query(func.count(MonzaContCobranza.id))
                      .filter(MonzaContCobranza.medio != MEDIO_ADELANTO,
                              ~MonzaContCobranza.id.in_(ya_conciliadas)).scalar())
    # Adelantos aprobados (no anulados) aún SIN conciliar con un abono del banco (espejo
    # GA resumen, que lista solo los de estado 'aprobado').
    adel_ids = [r[0] for r in _sin_anulados(db.query(MonzaContAdelanto.id)).all()]
    adel_sin_conc = len(set(adel_ids) - _adelantos_conciliados_ids(db, adel_ids))
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
        "adelantos_sin_conciliar": int(adel_sin_conc),
        "por_pagar_vencido_clp": round(_f(vencido), 0),
    }
