"""API del módulo Embarques Pricing (Contabilidad → costo landed).

Integración NO invasiva con Logística: lee los embarques que crea Compras
(tabla `embarques`) y superpone su propio pricing. Por eso TODO embarque creado
por Logística "aparece" automáticamente acá; el registro de pricing se crea de
forma diferida la primera vez que Contabilidad lo abre.

Prefijo: /embarques-pricing  (montado en main.py con prefix=/api → /api/embarques-pricing)
"""
from datetime import datetime
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.exc import IntegrityError, OperationalError
from pydantic import BaseModel, Field

from database import get_db
from auth import get_current_user
from empresa_guard import require_empresa
from models.models import (
    User, Embarque, EmbarqueItem, ItemCotizacion, OcProveedor, OcProveedorItem,
    FacturaProveedor, FacturaProveedorItem, ConfiguracionCotizador,
)
from .models import EmbarquePricing, EmbarquePricingGasto, EmbarquePricingItem
from .service import (
    calcular_landed, total_gastos_que_capitalizan, GASTOS_CATALOGO, IVA_RATE, _f,
)
# Creación / seed del pricing: lógica compartida (también la usa Logística para
# auto-crear el pricing al embarcar). Fuente única en integration.py.
from .integration import (
    detect_tipo as _detect_tipo,
    get_cfg as _cfg,
    ensure_pricing_for_embarque,
    tc_de_config as _tc_de_config,
    MONEDAS_CON_TC,
)

router = APIRouter(
    prefix="/embarques-pricing",
    tags=["embarques-pricing"],
    dependencies=[Depends(require_empresa("mineria"))],  # candado: solo Grupo AM
)

ESTADO_BLOQUEADO = "cerrado"


def _get_or_create_pricing(
    db: Session, embarque: Embarque, *, bloquear: bool = False
) -> EmbarquePricing:
    """Crea (si falta) o devuelve el pricing del embarque. Delega en integration.

    Nunca devuelve None hacia los endpoints: si la creación falla de forma
    irrecuperable (no por una carrera concurrente, que sí se recupera), corta con
    500 en vez de propagar un AttributeError en _compute_detail.

    `bloquear=True` — rutas de ESCRITURA (guardar / cerrar / reabrir): relee la
    cabecera con populate_existing().with_for_update(), que es la regla de la casa
    para toda decisión de plata (docs/regla-lecturas-de-plata.md). El costo landed
    que se congela ES plata: sin el lock, dos POST /cerrar simultáneos leían los dos
    `estado != 'cerrado'`, los dos recalculaban y el segundo PISABA el snapshot del
    primero (dos costos distintos congelados para el mismo embarque, sin rastro).
    Con el lock el segundo espera, relee `cerrado` y recibe el 409 de siempre.
    `populate_existing()` es imprescindible: sin él SQLAlchemy descarta la fila
    fresca porque el objeto ya está en el identity map (lo metió `ensure_...`).
    """
    pricing = ensure_pricing_for_embarque(db, embarque, commit=True)
    if pricing is None:
        raise HTTPException(500, "No se pudo crear el registro de pricing del embarque")
    if not bloquear:
        return pricing
    bloqueado = (
        db.query(EmbarquePricing)
        .filter(EmbarquePricing.id == pricing.id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if bloqueado is None:
        # Logística borró el embarque (ON DELETE CASCADE) entre el ensure y el lock.
        raise HTTPException(409, "El pricing del embarque ya no existe; recargue la pantalla")
    return bloqueado


def _con_retry_deadlock(db: Session, operacion):
    """Ejecuta `operacion()` reintentando ante deadlock / lock-timeout de InnoDB
    (1213 / 1205), igual que crear_compra y recepcion_nacional: el pricing lockea su
    cabecera y después escribe gastos y snapshot (con FK hacia embarque_items /
    items_cotizacion, que Despachos y el costeo también lockean). MySQL puede elegir
    víctima; se reintenta la transacción completa en vez de devolver un 500."""
    for _ in range(3):
        try:
            return operacion()
        except OperationalError as e:
            db.rollback()
            args = getattr(getattr(e, "orig", None), "args", None) or []
            if not args or args[0] not in (1213, 1205):
                raise
    raise HTTPException(
        409, "Conflicto momentáneo con otra edición simultánea del pricing: reintente")


def _load_embarque(db: Session, embarque_id: int):
    """Carga el embarque con sus ítems y la cotización de cada ítem en pocas
    queries (evita N+1 al recorrer embarque.items / ei.item_cotizacion)."""
    return (
        db.query(Embarque)
        .options(selectinload(Embarque.items).selectinload(EmbarqueItem.item_cotizacion))
        .filter(Embarque.id == embarque_id)
        .first()
    )


# ─── FOB por ítem: factura proveedor → cotización → 0 ─────────────────────────
def _fob_defaults(db: Session, embarque: Embarque) -> dict:
    """Mapea (item_cotizacion_id, oc_proveedor_id) → (fob_unit_default, origen).

    Se keyea por par (ítem, OC proveedor) para no tomar el precio de OTRA orden
    cuando el mismo ítem se re-compró en distintas OCs con precios distintos.
    Prioridad: precio de la factura del proveedor (FOB real) → precio de la
    cotización (estimado) → 0.
    """
    item_ids = [ei.item_cotizacion_id for ei in embarque.items if ei.item_cotizacion_id]
    if not item_ids:
        return {}

    # OcProveedorItem → id, indexado por par (ítem, OC) y con fallback por ítem.
    ocp_items = (
        db.query(OcProveedorItem)
        .filter(OcProveedorItem.item_cotizacion_id.in_(item_ids))
        .order_by(OcProveedorItem.id.asc())
        .all()
    )
    ocp_item_by_pair: dict = {}
    ocp_item_first_by_item: dict = {}
    for oi in ocp_items:
        ocp_item_by_pair.setdefault((oi.item_cotizacion_id, oi.oc_proveedor_id), oi.id)
        ocp_item_first_by_item.setdefault(oi.item_cotizacion_id, oi.id)

    # FacturaProveedorItem por ocp_item_id → unit_price_usd (la más reciente gana)
    fob_by_ocp_item: dict = {}
    ocp_item_ids = [oi.id for oi in ocp_items]
    if ocp_item_ids:
        fpis = (
            db.query(FacturaProveedorItem)
            .filter(FacturaProveedorItem.ocp_item_id.in_(ocp_item_ids))
            .order_by(FacturaProveedorItem.id.asc())
            .all()
        )
        for fpi in fpis:
            if fpi.unit_price_usd is not None:
                fob_by_ocp_item[fpi.ocp_item_id] = _f(fpi.unit_price_usd)

    out: dict = {}
    for ei in embarque.items:
        icid = ei.item_cotizacion_id
        if icid is None:
            continue
        # OcProveedorItem del MISMO embarque (par ítem+OC); fallback al 1º del ítem.
        ocp_item_id = ocp_item_by_pair.get((icid, ei.oc_proveedor_id)) or ocp_item_first_by_item.get(icid)
        if ocp_item_id and ocp_item_id in fob_by_ocp_item:
            out[(icid, ei.oc_proveedor_id)] = (fob_by_ocp_item[ocp_item_id], "factura")
            continue
        # Fallback: precio de la cotización (ítem ya cargado vía relación, sin N+1)
        item = ei.item_cotizacion
        if item and item.precio_unit_cotizacion:
            out[(icid, ei.oc_proveedor_id)] = (_f(item.precio_unit_cotizacion), "cotizacion")
        else:
            out[(icid, ei.oc_proveedor_id)] = (0.0, "manual")
    return out


# ─── Construcción de inputs y cómputo del detalle ─────────────────────────────
def _build_inputs(db: Session, embarque: Embarque, pricing: EmbarquePricing) -> List[dict]:
    """Arma los inputs por ítem mezclando defaults + overrides guardados."""
    fob_def = _fob_defaults(db, embarque)
    stored = {
        si.embarque_item_id: si
        for si in db.query(EmbarquePricingItem)
        .filter(EmbarquePricingItem.pricing_id == pricing.id)
        .all()
    }
    tc_header = _f(pricing.tc_valor)
    inputs: List[dict] = []
    for ei in embarque.items:
        item = ei.item_cotizacion
        icid = ei.item_cotizacion_id
        default_fob, default_origen = fob_def.get((icid, ei.oc_proveedor_id), (0.0, "manual"))
        s = stored.get(ei.id)

        # Override manual solo si trae un valor > 0: un "manual" en 0 (ítem que
        # nunca tuvo precio, o 0 explícito) no es un precio real y NO debe
        # bloquear el FOB de la factura del proveedor que llega después.
        if s is not None and s.fob_origen == "manual" and _f(s.fob_unit) > 0:
            fob_unit, origen = _f(s.fob_unit), "manual"
        else:
            fob_unit, origen = default_fob, default_origen

        # Peso: default de la cotización; override manual solo si trae valor > 0.
        # Espejo del FOB: un "manual" en 0 no es un peso real (una pieza física
        # pesa > 0) y NO debe pisar el peso de la cotización.
        default_peso = _f(item.peso_unit_lbs) if item else 0.0
        if s is not None and (s.peso_origen or "auto") == "manual" and _f(s.peso_unit_lbs) > 0:
            peso_unit, peso_origen = _f(s.peso_unit_lbs), "manual"
        else:
            peso_unit, peso_origen = default_peso, "auto"

        # TC del encabezado para todos los ítems. El TC por orden (FastMark
        # multi-OC) es una mejora futura: hoy un embarque usa un TC único, que
        # es el caso de Normal/Courier/Baukat. Así un cambio de TC se propaga
        # siempre (no queda "pegado" en el snapshot del ítem).
        tc_item = tc_header

        inputs.append({
            "embarque_item_id": ei.id,
            "item_cotizacion_id": icid,
            "numero_parte": (item.numero_parte if item else None) or "",
            "descripcion": (item.descripcion if item else None) or "",
            "moneda": pricing.moneda,
            "cantidad": _f(item.cantidad) if item else 0.0,
            "peso_unit_lbs": peso_unit,
            "peso_default": default_peso,
            "peso_origen": peso_origen,
            "fob_unit": fob_unit,
            "fob_default": default_fob,
            "fob_origen": origen,
            "tc_valor": tc_item,
        })
    return inputs


def _shipping_total_clp(pricing: EmbarquePricing) -> float:
    """Flete total en CLP: ME × TC si viene en moneda extranjera, o el CLP directo."""
    if pricing.flete_en_me:
        return _f(pricing.shipping_me) * _f(pricing.tc_valor)
    return _f(pricing.shipping_clp)


def _serialize_gasto(g: EmbarquePricingGasto) -> dict:
    neto, iva = _f(g.monto_neto), _f(g.iva)
    return {
        "id": g.id, "tipo": g.tipo, "glosa": g.glosa,
        "monto_neto": neto, "iva": iva, "total_bruto": neto + iva,
        "capitaliza": bool(g.capitaliza), "nro_factura": g.nro_factura,
        "fecha_factura": g.fecha_factura, "banco": g.banco, "orden": g.orden,
    }


def _advertencias(
    db: Session, embarque: Embarque, pricing: EmbarquePricing, cfg=None
) -> List[str]:
    """Avisos VISIBLES del embarque que NO bloquean el costeo (se muestran en el detalle).

    EL PROBLEMA DE FONDO: el pricing maneja UNA sola moneda para todo el embarque
    (`emb_pricing.moneda`) y UN solo TC (`_build_inputs`). La moneda se SIEMBRA una vez, con
    la de la 1ª OC de proveedor (`integration.moneda_de_embarque`), y **nunca se
    re-sincroniza**. Un FOB convertido con el TC equivocado subvalúa (o sobrevalúa) TODOS
    los ítems EN SILENCIO, y el cierre lo congela.

    Son avisos y no 409 a propósito: el embarque ya llegó físicamente y este módulo es el
    ÚNICO lugar donde se registra su costo — bloquear el guardado dejaría mercadería
    recibida sin costear, que es peor que costearla avisando. La raíz (un TC por ítem/orden)
    es deuda declarada de las dos marcas y no se toca acá.

    LOS 4 CASOS QUE SE AVISAN (el aviso original cubría SOLO el 2, que es justo el que el
    dueño dice que no pasa, y era ciego a los que sí pasan):

      1. La moneda del costeo NO es la de las OC del embarque. **Este es el caso real**:
         `OcProveedorCreate.moneda` nace 'USD' por defecto y el PATCH de la OC la deja
         editar, así que la secuencia normal es OC nace USD → el contador abre el pricing
         (nace USD/940) → se corrige la OC a EUR → el pricing sigue en USD/940 y calcula
         10 × 1000 EUR = 9.400.000 en vez de 11.000.000 (−14,5 %). Pasa con UNA sola OC, así
         que el `len(ocp_ids) <= 1: return []` de antes lo hacía invisible.
      2. Las OC del embarque no se ponen de acuerdo entre ellas (consolidado multi-moneda).
      3. El costeo está en una moneda que este módulo no sabe convertir (Config solo tiene
         TC de USD y EUR). En la BD real hay una OC en 'CLP': el FOB en pesos se
         multiplicaba por 940.
      4. El TC cargado es EL DEL DÓLAR en un embarque que se costea en otra moneda (el
         operador cambió la moneda y dejó el TC). Se compara contra Config, sin heurísticas.
    """
    avisos: List[str] = []
    moneda_pricing = (pricing.moneda or "USD").strip().upper()
    ocp_ids = sorted({ei.oc_proveedor_id for ei in embarque.items if ei.oc_proveedor_id})
    monedas = sorted({
        (m or "").strip().upper()
        for (m,) in db.query(OcProveedor.moneda).filter(OcProveedor.id.in_(ocp_ids)).all()
        if (m or "").strip()
    }) if ocp_ids else []

    # (1) el costeo usa una moneda distinta a la de las órdenes de proveedor
    ajenas = [m for m in monedas if m != moneda_pricing]
    if ajenas:
        avisos.append(
            f"El costo landed se está calculando en {moneda_pricing} con un solo tipo de "
            f"cambio, pero la(s) orden(es) de proveedor de este embarque están en "
            f"{', '.join(ajenas)}. El FOB de esos ítems va a quedar mal convertido. "
            f"Cambia la moneda del embarque a la de la orden (y su tipo de cambio), o "
            f"carga el FOB de todos los ítems ya expresado en {moneda_pricing}."
        )

    # (2) las órdenes del embarque traen monedas distintas ENTRE ELLAS
    if len(monedas) > 1:
        avisos.append(
            f"Este embarque trae órdenes de proveedor en más de una moneda "
            f"({', '.join(monedas)}), pero el costo landed se calcula todo en "
            f"{moneda_pricing} con un solo tipo de cambio. Carga el FOB de todos los ítems "
            f"en {moneda_pricing}, o pide separar el embarque por moneda."
        )

    # (3) moneda de costeo sin TC parametrizado (CLP, GBP, un typo…)
    if moneda_pricing not in MONEDAS_CON_TC:
        avisos.append(
            f"El embarque se está costeando en {moneda_pricing}, y este módulo solo sabe "
            f"convertir {' y '.join(MONEDAS_CON_TC)} (son las únicas monedas con tipo de "
            f"cambio en Configuración). Si el FOB ya viene en pesos, el tipo de cambio debe "
            f"ser 1; si no, cambia la moneda del embarque. Revisa el costo antes de cerrar."
        )

    # (4) se está usando el TC del DÓLAR en un embarque de otra moneda
    tc_usado = _f(pricing.tc_valor)
    tc_usd = _tc_de_config(cfg, "USD")
    tc_propio = _tc_de_config(cfg, moneda_pricing)
    if (moneda_pricing != "USD" and tc_usado > 0 and tc_usd > 0 and tc_propio > 0
            and abs(tc_usado - tc_usd) < 0.01 and abs(tc_propio - tc_usd) >= 0.01):
        avisos.append(
            f"El tipo de cambio cargado ({tc_usado:,.2f}) es exactamente el del DÓLAR, pero "
            f"este embarque se costea en {moneda_pricing}: el tipo de cambio de "
            f"{moneda_pricing} en Configuración es {tc_propio:,.2f}. Confirma el tipo de "
            f"cambio antes de cerrar."
        )
    return avisos


# ─── La línea del gasto y su CxP no pueden DIVERGIR (ni congelarse divergentes) ─
# Tolerancia de 1 peso: el CLP se redondea a entero en todo el módulo (y en CxP), así
# que un peso de diferencia es ruido de redondeo, no una edición.
TOL_CLP = 1.0


def _clp(x) -> str:
    """Monto en pesos con separador de miles chileno (para los mensajes de error)."""
    return f"{_f(x):,.0f}".replace(",", ".")


def _lineas_gasto(db: Session, pricing: EmbarquePricing) -> List[EmbarquePricingGasto]:
    """Las líneas de gasto TAL COMO ESTÁN EN LA BD (una por tipo, la de menor id).

    Relee con `populate_existing()` — regla de la casa para toda decisión de plata: el
    guard compara el monto PERSISTIDO contra la CxP, y el identity map podría servir un
    snapshot viejo de la fila (la trajo `ensure_pricing_for_embarque` antes del lock).
    Se llama siempre ANTES de modificar las filas o DESPUÉS de un flush, nunca con
    cambios pendientes en la sesión.
    """
    vistos: dict = {}
    for fila in (
        db.query(EmbarquePricingGasto)
        .filter(EmbarquePricingGasto.pricing_id == pricing.id)
        .order_by(EmbarquePricingGasto.id.asc())
        .populate_existing()
        .all()
    ):
        vistos.setdefault(fila.tipo, fila)
    return sorted(vistos.values(), key=lambda g: (g.orden or 0, g.id))


def _pares_gasto_cxp(db: Session, pricing: EmbarquePricing) -> List[tuple]:
    """[(gasto, compra_id, neto_cxp_clp, bruto_cxp_clp)] de las líneas que YA están
    registradas como Cuenta por Pagar ACTIVA. Una sola query (sin N+1).

    Import local de `compras_contab.models` a propósito: este módulo no depende de
    Contabilidad al cargar (misma convención que `compras_contab._recibido_nacional`
    con recepcion_nacional). El candado de empresa va explícito ('mineria'), igual que
    el `require_empresa` del router: una CxP de MonzaParts no puede opinar sobre el
    costo landed de Grupo AM.
    """
    from compras_contab.models import ContCompra

    filas = _lineas_gasto(db, pricing)
    ids = [g.id for g in filas]
    if not ids:
        return []
    reg: dict = {}
    for cid, gid, neto, tc, bruto_clp in (
        db.query(ContCompra.id, ContCompra.emb_pricing_gasto_id, ContCompra.monto_neto,
                 ContCompra.tc, ContCompra.monto_total_clp)
        .filter(ContCompra.empresa == "mineria",
                ContCompra.emb_pricing_gasto_id.in_(ids),
                ContCompra.anulado.is_(False))
        .order_by(ContCompra.id.asc())
        .all()
    ):
        tc_v = _f(tc) or 1.0
        reg.setdefault(int(gid), (int(cid), round(_f(neto) * tc_v, 2), round(_f(bruto_clp), 2)))
    return [(g, *reg[g.id]) for g in filas if g.id in reg]


def _bloqueo_monto_gasto_con_cxp(
    db: Session, pricing: EmbarquePricing, intenciones: Optional[dict] = None
) -> None:
    """FAIL CLOSED: el monto de una línea YA registrada en CxP no puede quedar divergente.

    EL DAÑO QUE CIERRA (re-auditoría, HALLAZGO 2 — y con él la mecánica del HALLAZGO 1)
    ---------------------------------------------------------------------------------
    La ronda anterior estabilizó la IDENTIDAD del gasto (la PK sobrevive al re-guardado),
    pero NO el MONTO: con la llave estable, la línea se podía editar libremente después de
    registrar la CxP y nada reconciliaba nada. El overlay seguía diciendo "En compras ✓"
    (lo calcula por id de gasto, sin mirar la plata), el costo landed se recalculaba con el
    monto NUEVO, la CxP se quedaba con el VIEJO y al cerrar la divergencia quedaba
    CONGELADA. Medido por los re-auditores: línea 476.000 / pasivo 190.400, `anular`
    bloqueado por el pago ya registrado y el 409 del propio arreglo anterior cerrando la
    última salida → callejón sin salida.
    Y es también la mecánica de la DOBLE CxP: mover el monto de 'agencia' a 'otros' dejaba
    la línea vieja "registrada" en $0 (pill verde sobre un cero) y la nueva "no registrada"
    con la plata → un clic y Σ CxP = 380.800 por una factura de 190.400.

    LA REGLA (se eligió BLOQUEAR, no avisar ni propagar)
    ---------------------------------------------------
    Propagar sería peor: reescribiría en silencio un pasivo que ya puede estar pagado,
    conciliado con el banco y en el F29. Avisar es lo que ya había (nada) y es justo lo que
    dejó el pasivo mal por meses. Así que el cambio de monto de una línea con CxP ACTIVA se
    RECHAZA con 409, y el mensaje nombra las dos únicas salidas reales: dejar la línea con
    el monto de la factura registrada, o revertir la CxP (pagos → anular) y volver a
    registrarla con el monto correcto. Un 409 que obliga a mirar es infinitamente más
    barato que un pasivo falso.

    DOS PRECISIONES QUE HACEN QUE NO SE VUELVA UN MURO
      · Solo se juzga la línea que el PUT REALMENTE cambia (`intenciones` != persistido).
        Un pricing legado ya divergente no secuestra la pantalla: se puede seguir
        editando el TC y el resto de las líneas.
      · Un cambio que CONVERGE hacia la CxP (deja la línea igual al monto registrado)
        se ACEPTA: es el camino de reparación de una divergencia legada.
    Con `intenciones=None` (POST /cerrar) se juzga el estado PERSISTIDO: cerrar congela el
    costo, así que ahí sí se exige que línea y pasivo cuadren.

    Los demás campos de la línea (N° de factura, banco, fecha) quedan editables a
    propósito: escribir el N° de la factura después de registrarla es exactamente el
    remedio que pide el 409 del anti-duplicado por factura física.
    """
    conflictos: List[str] = []
    for g, compra_id, neto_cxp, bruto_cxp in _pares_gasto_cxp(db, pricing):
        neto_act, iva_act = round(_f(g.monto_neto), 2), round(_f(g.iva), 2)
        if intenciones is None:
            neto_new, iva_new = neto_act, iva_act
        else:
            neto_new, iva_new = intenciones.get(g.tipo, (neto_act, iva_act))
            neto_new, iva_new = round(_f(neto_new), 2), round(_f(iva_new), 2)
            if (neto_new, iva_new) == (neto_act, iva_act):
                continue                      # esta línea no la toca el PUT
        bruto_new = round(neto_new + iva_new, 2)
        if (abs(neto_new - neto_cxp) <= TOL_CLP and abs(bruto_new - bruto_cxp) <= TOL_CLP):
            continue                          # queda CUADRADA con la CxP (o converge)
        conflictos.append(
            f"«{g.glosa or g.tipo}» está registrada como la compra #{compra_id} por "
            f"{_clp(bruto_cxp)} (neto {_clp(neto_cxp)}) y quedaría en {_clp(bruto_new)} "
            f"(neto {_clp(neto_new)})"
        )
    if not conflictos:
        return
    accion = ("Este guardado" if intenciones is not None else
              "No se puede CERRAR el pricing: cerrar CONGELA el costo landed y")
    raise HTTPException(
        409,
        f"{accion} dejaría el costo del embarque y las Cuentas por Pagar con montos "
        f"DISTINTOS por la misma factura: " + "; ".join(conflictos) + ". El pasivo no se "
        "corrige solo (puede estar pagado y conciliado). Salidas: deje la línea con el "
        "monto de la factura ya registrada, o revierta la CxP (borre sus pagos y anúlela) "
        "y vuelva a registrarla con el monto correcto.",
    )


def _avisos_gastos_vs_cxp(db: Session, pricing: EmbarquePricing) -> List[str]:
    """Aviso VISIBLE (no bloqueante) de las divergencias línea ↔ CxP que ya existen.

    Va en `advertencias` del detalle, así el contador VE la divergencia en la pantalla
    en vez de descubrirla cuando el cierre le devuelve un 409. No bloquea la lectura:
    los datos legados divergentes tienen que poder abrirse para poder repararse.
    """
    avisos: List[str] = []
    for g, compra_id, neto_cxp, bruto_cxp in _pares_gasto_cxp(db, pricing):
        bruto_linea = round(_f(g.monto_neto) + _f(g.iva), 2)
        if abs(bruto_linea - bruto_cxp) <= TOL_CLP and abs(round(_f(g.monto_neto), 2) - neto_cxp) <= TOL_CLP:
            continue
        avisos.append(
            f"La línea «{g.glosa or g.tipo}» ({_clp(bruto_linea)}) NO cuadra con la compra "
            f"#{compra_id} que la registra en Cuentas por Pagar ({_clp(bruto_cxp)}). El "
            f"costo del embarque y el pasivo están diciendo cosas distintas de la misma "
            f"factura: corrija la línea (dejándola en el monto de la compra) o revierta la "
            f"compra y regístrela de nuevo. No se puede cerrar el pricing así."
        )
    return avisos


def _snapshot_items(db: Session, pricing: EmbarquePricing) -> List[dict]:
    """Filas del snapshot persistido (pricing cerrado → costo CONGELADO).

    Devuelve las filas guardadas al cerrar, con la misma forma que el recálculo
    en vivo, para que el detalle cuadre siempre con el listado.
    """
    snap = (
        db.query(EmbarquePricingItem)
        .filter(EmbarquePricingItem.pricing_id == pricing.id)
        .order_by(EmbarquePricingItem.id.asc())
        .all()
    )
    return [{
        "embarque_item_id": s.embarque_item_id,
        "item_cotizacion_id": s.item_cotizacion_id,
        "numero_parte": s.numero_parte or "",
        "descripcion": s.descripcion or "",
        "moneda": s.moneda,
        "cantidad": _f(s.cantidad),
        "peso_unit_lbs": _f(s.peso_unit_lbs),
        # Cerrado no se edita: el default mostrado es el mismo peso congelado.
        "peso_default": _f(s.peso_unit_lbs),
        "peso_origen": s.peso_origen or "auto",
        "peso_total_lbs": round(_f(s.peso_total_lbs), 2),
        "fob_unit": _f(s.fob_unit),
        # Cerrado no se edita: el default mostrado es el mismo valor congelado.
        "fob_default": _f(s.fob_unit),
        "fob_origen": s.fob_origen,
        "tc_valor": _f(s.tc_valor),
        "fob_total": round(_f(s.fob_total), 2),
        "fob_clp": round(_f(s.fob_clp), 0),
        "shipping_clp": round(_f(s.shipping_clp), 0),
        "cif_clp": round(_f(s.cif_clp), 0),
        "gastos_clp": round(_f(s.gastos_clp), 0),
        "costo_total_clp": round(_f(s.costo_total_clp), 0),
        "costo_unit_clp": round(_f(s.costo_unit_clp), 0),
    } for s in snap]


def _compute_detail(db: Session, embarque: Embarque, pricing: EmbarquePricing) -> dict:
    cfg = _cfg(db)
    gastos = sorted(pricing.gastos, key=lambda g: (g.orden or 0, g.id))
    gastos_dicts = [_serialize_gasto(g) for g in gastos]
    total_cap = total_gastos_que_capitalizan(
        [{"monto_neto": g["monto_neto"], "capitaliza": g["capitaliza"]} for g in gastos_dicts]
    )
    total_iva = sum(g["iva"] for g in gastos_dicts if g["capitaliza"])
    iva_importacion = sum(g["monto_neto"] for g in gastos_dicts if g["tipo"] == "iva_importacion")

    shipping_total = _shipping_total_clp(pricing)

    # Cerrado → el detalle sale del snapshot persistido al cerrar (costo
    # CONGELADO, igual que el listado). Abierto → recálculo en vivo.
    if pricing.estado == ESTADO_BLOQUEADO:
        items_out = _snapshot_items(db, pricing)
    else:
        inputs = _build_inputs(db, embarque, pricing)
        calc_rows, _ = calcular_landed(inputs, shipping_total, total_cap)

        items_out = []
        for r in calc_rows:
            items_out.append({
                "embarque_item_id": r["embarque_item_id"],
                "item_cotizacion_id": r["item_cotizacion_id"],
                "numero_parte": r["numero_parte"],
                "descripcion": r["descripcion"],
                "moneda": r["moneda"],
                "cantidad": r["cantidad"],
                "peso_unit_lbs": r["peso_unit_lbs"],
                "peso_default": r.get("peso_default", 0.0),
                "peso_origen": r.get("peso_origen", "auto"),
                "peso_total_lbs": round(r["peso_total_lbs"], 2),
                "fob_unit": r["fob_unit"],
                "fob_default": r.get("fob_default", 0.0),
                "fob_origen": r["fob_origen"],
                "tc_valor": r["tc_valor"],
                "fob_total": round(r["fob_total"], 2),
                "fob_clp": round(r["fob_clp"], 0),
                "shipping_clp": round(r["shipping_clp"], 0),
                "cif_clp": round(r["cif_clp"], 0),
                "gastos_clp": round(r["gastos_clp"], 0),
                "costo_total_clp": round(r["costo_total_clp"], 0),
                "costo_unit_clp": round(r["costo_unit_clp"], 0),
            })

    return {
        "embarque": {
            "id": embarque.id, "numero": embarque.numero, "estado": embarque.estado,
            "forwarder": embarque.forwarder, "awb": embarque.awb,
            "awb_numero": embarque.awb_numero,
            "fecha_despacho": embarque.fecha_despacho,
            "fecha_llegada_est": embarque.fecha_llegada_est.isoformat() if embarque.fecha_llegada_est else None,
            "n_items": len(embarque.items),
            # Documentos del embarque (Logística los sube o no) → trazabilidad.
            "documentos": {
                "awb": embarque.awb,
                "factura_comercial": embarque.factura_comercial,
                "packing_list": embarque.packing_list,
                "certificado_origen": embarque.certificado_origen,
                "doc_adicional": embarque.doc_adicional,
            },
        },
        "pricing": {
            "id": pricing.id,
            # Correlativo de pricing partiendo desde 1 (== id autoincrement).
            "correlativo": pricing.id,
            "tipo_embarque": pricing.tipo_embarque,
            "tc_tipo": pricing.tc_tipo, "tc_valor": _f(pricing.tc_valor),
            # TC sugerido de Config SEGÚN LA MONEDA del embarque: USD →
            # tipo_cambio_usd, EUR → tipo_cambio_eur. Antes solo se servía para USD
            # porque Config no tenía columna EUR, así que TODO embarque Baukat/EUR
            # nacía sin sugerencia y el TC se teclaba de memoria sobre el 100% del FOB.
            "tc_config": _tc_de_config(cfg, pricing.moneda or "USD"),
            "moneda": pricing.moneda, "flete_en_me": bool(pricing.flete_en_me),
            "shipping_me": _f(pricing.shipping_me), "shipping_clp": _f(pricing.shipping_clp),
            "shipping_total_clp": round(shipping_total, 0),
            "estado": pricing.estado, "observaciones": pricing.observaciones,
            "calculado_at": pricing.calculado_at.isoformat() if pricing.calculado_at else None,
        },
        # Avisos no bloqueantes (moneda mezclada + línea de gasto que no cuadra con la
        # CxP que la registra). Lista vacía = todo en orden.
        "advertencias": _advertencias(db, embarque, pricing, cfg) + _avisos_gastos_vs_cxp(db, pricing),
        "gastos": gastos_dicts,
        "totales_gastos": {
            "total_capitaliza": round(total_cap, 0),
            "total_iva": round(total_iva, 0),
            "iva_importacion": round(iva_importacion, 0),
        },
        "items": items_out,
        # Totales sumando los valores YA redondeados de cada fila → el pie cuadra
        # columna por columna con lo que ve el contador.
        "totales": {
            "n_items": len(items_out),
            "peso_total_lbs": round(sum(r["peso_total_lbs"] for r in items_out), 2),
            "fob_total_me": round(sum(r["fob_total"] for r in items_out), 2),
            "fob_clp": sum(r["fob_clp"] for r in items_out),
            "shipping_clp": sum(r["shipping_clp"] for r in items_out),
            "cif_clp": sum(r["cif_clp"] for r in items_out),
            "gastos_clp": sum(r["gastos_clp"] for r in items_out),
            "costo_total_clp": sum(r["costo_total_clp"] for r in items_out),
        },
    }


def _validar_gastos_no_negativos(pricing: EmbarquePricing) -> None:
    """FAIL CLOSED: ningún gasto NEGATIVO puede entrar al costo landed ni congelarse.

    El `ge=0` de `GastoIn` cubre la puerta del PUT, pero NO es la única: los montos de
    Desconsolidación / Almacenaje / Agencia se siembran desde la Config del cotizador
    (`integration.seed_gastos`) y ese `ConfigUpdate` no tiene `ge=0` (no es código de este
    módulo). Con un parámetro negativo sembrado, un PUT SOLO-ENCABEZADO (que ni pasa por
    GastoIn) y después un POST /cerrar congelaban un total capitalizable NEGATIVO:
    `total_gastos_que_capitalizan` los SUMA, así que el negativo RESTA del pozo que se
    prorratea a TODOS los ítems (medido: 340.000 → −30.000 con un −500.000), y
    `cerrar_pricing` solo exigía `costo_total > 0`.

    Se valida acá, en el punto donde el costo se calcula y se PERSISTE, para que la defensa
    no dependa de por dónde entró el dato (payload, seed de Config, edición directa en BD o
    un escritor futuro). Es 400 con la línea culpable nombrada: el contador sabe qué
    corregir. Un PUT que traiga `gastos` sanos arregla la fila y pasa.
    """
    malos = [
        f"{g.glosa or g.tipo} (neto {_f(g.monto_neto):.0f} / IVA {_f(g.iva):.0f})"
        for g in sorted(pricing.gastos, key=lambda x: (x.orden or 0, x.id))
        if _f(g.monto_neto) < 0 or _f(g.iva) < 0
    ]
    if malos:
        raise HTTPException(
            400,
            "Hay gastos locales con monto NEGATIVO, que restarían del costo de todos los "
            "ítems del embarque: " + "; ".join(malos) + ". Corrija esas líneas (y el "
            "parámetro en Configuración del cotizador si viene de ahí) antes de guardar.",
        )


def _persist_snapshot(db: Session, pricing: EmbarquePricing, detail: dict) -> None:
    """Guarda el snapshot por ítem (inputs + computados) para congelar el costo."""
    db.query(EmbarquePricingItem).filter(
        EmbarquePricingItem.pricing_id == pricing.id
    ).delete()
    db.flush()  # materializa el borrado antes de re-insertar (sin colisiones en el identity map)
    for r in detail["items"]:
        db.add(EmbarquePricingItem(
            pricing_id=pricing.id,
            embarque_item_id=r["embarque_item_id"],
            item_cotizacion_id=r["item_cotizacion_id"],
            numero_parte=r["numero_parte"], descripcion=r["descripcion"], moneda=r["moneda"],
            cantidad=r["cantidad"], peso_unit_lbs=r["peso_unit_lbs"],
            peso_total_lbs=r["peso_total_lbs"], peso_origen=r.get("peso_origen", "auto"),
            fob_unit=r["fob_unit"],
            fob_origen=r["fob_origen"], tc_valor=r["tc_valor"],
            fob_total=r["fob_total"], fob_clp=r["fob_clp"], shipping_clp=r["shipping_clp"],
            cif_clp=r["cif_clp"], gastos_clp=r["gastos_clp"],
            costo_total_clp=r["costo_total_clp"], costo_unit_clp=r["costo_unit_clp"],
        ))


# ─── Endpoints ────────────────────────────────────────────────────────────────
@router.get("")
def listar_embarques_pricing(
    q: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Lista TODOS los embarques de Logística con el estado de su pricing."""
    # selectinload evita el N+1 al leer len(e.items) por cada embarque.
    embarques = (
        db.query(Embarque)
        .options(selectinload(Embarque.items))
        .order_by(Embarque.id.desc())
        .all()
    )
    pricings = {p.embarque_id: p for p in db.query(EmbarquePricing).all()}
    # Costo total por pricing: suma agrupada en SQL (no carga toda la tabla).
    costos: dict = {
        pid: _f(total)
        for pid, total in db.query(
            EmbarquePricingItem.pricing_id, func.sum(EmbarquePricingItem.costo_total_clp)
        ).group_by(EmbarquePricingItem.pricing_id).all()
    }

    out = []
    for e in embarques:
        if q:
            ql = q.lower()
            hay = " ".join([e.numero or "", e.forwarder or "", e.awb or "", e.awb_numero or ""]).lower()
            if ql not in hay:
                continue
        p = pricings.get(e.id)
        docs = [e.awb, e.factura_comercial, e.packing_list, e.certificado_origen, e.doc_adicional]
        out.append({
            "embarque_id": e.id,
            "correlativo": p.id if p else None,
            "numero": e.numero,
            "estado_logistica": e.estado,
            "forwarder": e.forwarder,
            "awb": e.awb,
            "awb_numero": e.awb_numero,
            "fecha_despacho": e.fecha_despacho,
            "n_items": len(e.items),
            "docs_count": sum(1 for d in docs if d),
            "tipo_embarque": p.tipo_embarque if p else _detect_tipo(e.forwarder, "USD"),
            "pricing_estado": p.estado if p else "sin_pricing",
            "moneda": p.moneda if p else None,
            "tc_valor": _f(p.tc_valor) if p else None,
            "costo_total_clp": round(costos.get(p.id, 0.0), 0) if p else None,
        })
    return out


# ─── Parámetros de costeo (Config del cotizador, lo que usa este módulo) ───────
# POR QUÉ VIVE ACÁ: `configuracion_cotizador.tipo_cambio_eur` es el TC con que nace TODO
# embarque Baukat/Europa, y NO tenía ni un camino de escritura en toda la app (ni en
# `_config_to_dict` ni en `ConfigUpdate` de routers/cotizador.py): era un DEFAULT 1100
# cableado en la migración que el dueño no podía actualizar cuando el euro se movía, con
# etiqueta `tc_tipo='config'`. Los 3 gastos de internación sí se editan en el cotizador,
# pero allá SIN piso (`ConfigUpdate` no tiene un solo `ge=0`) y son la 2ª puerta por la que
# entraban montos negativos al costeo. Este endpoint expone los 4 parámetros que ESTE
# módulo consume, con `ge=0` en todos, sin tocar el router del cotizador.
# Ruta de 2 segmentos: no compite con `/{embarque_id}`, que matchea uno solo.
class ParametrosCosteoIn(BaseModel):
    """Todos con piso en 0: un parámetro de costo negativo subvalúa el landed de todos los
    ítems del embarque (ver `_validar_gastos_no_negativos`)."""
    tipo_cambio_eur: Optional[float] = Field(None, ge=0)
    desconsolidado_clp: Optional[float] = Field(None, ge=0)
    bodegaje_clp: Optional[float] = Field(None, ge=0)
    costo_agencia_minimo_clp: Optional[float] = Field(None, ge=0)


def _cfg_or_create(db: Session) -> ConfiguracionCotizador:
    cfg = _cfg(db)
    if cfg is None:
        cfg = ConfiguracionCotizador(id=1)
        db.add(cfg)
        db.commit()
        db.refresh(cfg)
    return cfg


def _parametros_out(cfg: ConfiguracionCotizador) -> dict:
    return {
        # El TC USD se edita en el cotizador (es su parámetro principal); acá va solo para
        # que la pantalla pueda mostrar el par y comparar.
        "tipo_cambio_usd": _f(cfg.tipo_cambio_usd),
        "tipo_cambio_usd_editable": False,
        "tipo_cambio_eur": _f(getattr(cfg, "tipo_cambio_eur", 0)),
        "desconsolidado_clp": _f(cfg.desconsolidado_clp),
        "bodegaje_clp": _f(cfg.bodegaje_clp),
        "costo_agencia_minimo_clp": _f(cfg.costo_agencia_minimo_clp),
    }


@router.get("/config/parametros")
def leer_parametros_costeo(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """TC por moneda y gastos de internación por defecto con que NACE cada pricing."""
    return _parametros_out(_cfg_or_create(db))


@router.put("/config/parametros")
def guardar_parametros_costeo(
    payload: ParametrosCosteoIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Actualiza los parámetros de costeo. Solo toca los campos que vengan en el payload
    (los demás quedan como estaban) y NO recalcula embarques ya creados: son el DEFAULT con
    el que nace el pricing siguiente, y el TC de cada embarque se sigue editando por embarque.
    """
    cfg = _cfg_or_create(db)
    cambios = payload.model_dump(exclude_none=True)
    if not cambios:
        raise HTTPException(400, "No se recibió ningún parámetro para actualizar")
    for campo, valor in cambios.items():
        setattr(cfg, campo, valor)
    db.commit()
    db.refresh(cfg)
    return _parametros_out(cfg)


@router.get("/{embarque_id}")
def detalle_embarque_pricing(
    embarque_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    embarque = _load_embarque(db, embarque_id)
    if not embarque:
        raise HTTPException(404, "Embarque no encontrado")
    pricing = _get_or_create_pricing(db, embarque)
    return _compute_detail(db, embarque, pricing)


# ── Payloads de guardado ──
GastoTipo = Literal["desconsolidacion", "almacenaje", "agencia", "arancel", "otros", "iva_importacion"]
TipoEmbarque = Literal["normal", "courier", "baukat", "fastmark"]


# Montos SIEMPRE >= 0 y strings con tope de largo (espejo de
# monza_embarques_pricing/router.py:456-504). Sin el `ge=0` un signo menos en
# "Otros" RESTABA del total que capitaliza (service.total_gastos_que_capitalizan)
# y subvaluaba TODOS los ítems del embarque; `cerrar_pricing` solo exige
# costo_total > 0, así que ese costo corrupto quedaba CONGELADO en el snapshot.
# El `max_length` alineado a cada columna hace fallar limpio en la API (422) en vez
# de truncar en silencio en MySQL o reventar con un 500.
class GastoIn(BaseModel):
    tipo: GastoTipo
    glosa: Optional[str] = Field(None, max_length=120)
    monto_neto: float = Field(0, ge=0)
    iva: float = Field(0, ge=0)
    capitaliza: bool = True
    nro_factura: Optional[str] = Field(None, max_length=100)
    fecha_factura: Optional[str] = Field(None, max_length=30)
    banco: Optional[str] = Field(None, max_length=100)
    orden: int = 0


class ItemOverrideIn(BaseModel):
    embarque_item_id: int
    # Tri-estado: True=fijar manual · False=volver a auto · None=no tocar este
    # campo (el usuario no lo editó). Evita que editar SOLO el peso revierta un
    # FOB manual guardado (y viceversa). El backend es la autoridad del override.
    # fob_unit >= 0: un FOB negativo no existe. El 0 se acepta y el backend lo trata
    # como "sin precio real" → cae al FOB de la factura/cotización.
    fob_unit: Optional[float] = Field(None, ge=0)
    fob_manual: Optional[bool] = None
    # peso >= 0: una pieza física no pesa menos que nada (y el peso GOBIERNA el
    # prorrateo del flete: un negativo deformaría el costo de todos los ítems).
    peso_unit_lbs: Optional[float] = Field(None, ge=0)
    peso_manual: Optional[bool] = None


class PricingSaveIn(BaseModel):
    tipo_embarque: Optional[TipoEmbarque] = None
    tc_tipo: Optional[Literal["manual", "config", "florida", "baukat"]] = None
    # tc_valor >= 0: 0 = "aún sin TC" (borrador). La regla TC > 0 para calcular /
    # cerrar se valida aparte en los endpoints.
    tc_valor: Optional[float] = Field(None, ge=0)
    moneda: Optional[Literal["USD", "EUR"]] = None
    flete_en_me: Optional[bool] = None
    shipping_me: Optional[float] = Field(None, ge=0)
    shipping_clp: Optional[float] = Field(None, ge=0)
    observaciones: Optional[str] = Field(None, max_length=65535)
    gastos: Optional[List[GastoIn]] = None
    items: Optional[List[ItemOverrideIn]] = None


@router.put("/{embarque_id}")
def guardar_embarque_pricing(
    embarque_id: int,
    payload: PricingSaveIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Guarda encabezado + gastos + overrides de FOB/TC, recalcula y persiste el snapshot."""
    return _con_retry_deadlock(
        db, lambda: _guardar_embarque_pricing_tx(embarque_id, payload, db, current_user))


def _guardar_embarque_pricing_tx(
    embarque_id: int, payload: PricingSaveIn, db: Session, current_user: User
):
    embarque = _load_embarque(db, embarque_id)
    if not embarque:
        raise HTTPException(404, "Embarque no encontrado")
    pricing = _get_or_create_pricing(db, embarque, bloquear=True)
    if pricing.estado == ESTADO_BLOQUEADO:
        raise HTTPException(409, "El pricing está cerrado; reábralo para editar")

    # 1) Encabezado
    if payload.tipo_embarque is not None:
        pricing.tipo_embarque = payload.tipo_embarque
    if payload.tc_tipo is not None:
        pricing.tc_tipo = payload.tc_tipo
    if payload.tc_valor is not None:
        pricing.tc_valor = payload.tc_valor
    if payload.moneda is not None:
        pricing.moneda = payload.moneda.upper()
    if payload.flete_en_me is not None:
        pricing.flete_en_me = payload.flete_en_me
    if payload.shipping_me is not None:
        pricing.shipping_me = payload.shipping_me
    if payload.shipping_clp is not None:
        pricing.shipping_clp = payload.shipping_clp
    if payload.observaciones is not None:
        pricing.observaciones = payload.observaciones
    pricing.usuario_id = getattr(current_user, "id", None)

    # Validación: flete prepagado en moneda extranjera necesita TC para convertir.
    if pricing.flete_en_me and _f(pricing.shipping_me) > 0 and _f(pricing.tc_valor) <= 0:
        raise HTTPException(400, "Defina un TC > 0 para convertir el flete en moneda extranjera a CLP")

    # Mantener shipping_clp coherente cuando el flete viene en ME (= ME × TC).
    # CLP se redondea a entero (el peso chileno no usa decimales), igual que el
    # resto de la app y que los totales mostrados → cuadre consistente.
    if pricing.flete_en_me:
        pricing.shipping_clp = round(_f(pricing.shipping_me) * _f(pricing.tc_valor), 0)

    # 2) Gastos: SIEMPRE las 6 líneas predeterminadas canónicas. El backend es la
    #    autoridad de las reglas de negocio (no confía en el cliente):
    #      · estructura fija de 6 tipos (si el cliente manda menos, se completan en 0);
    #      · glosa, capitaliza y orden se derivan del catálogo canónico;
    #      · iva=0 forzado para Arancel e IVA Importación (son exentos);
    #      · solo se toman del cliente los montos y los datos de factura/banco.
    #
    #    UPSERT por la llave natural (pricing_id, tipo) — NO delete + re-insert.
    #    Esto NO es un detalle de implementación: la PK de emb_pricing_gasto es la llave
    #    que `cont_compra.emb_pricing_gasto_id` referencia para saber que la factura del
    #    forwarder ya está registrada como CxP, y esa FK es ON DELETE **SET NULL**. Con el
    #    borrado anterior, cada guardado del pricing (el front SIEMPRE manda `gastos`, así
    #    que basta con corregir el TC) re-insertaba las 6 filas con PK nuevas → la compra
    #    ya registrada quedaba con la llave en NULL → el overlay volvía a decir "no
    #    registrado" → dos clics y la MISMA factura del forwarder entraba dos veces
    #    (Σ CxP = 380.800 por una factura de 190.400, reproducido en
    #    tests/test_llave_gasto_estable.py). Con el upsert las PK sobreviven al
    #    re-guardado y el lock + el 409 de crear_compra siguen valiendo.
    #    Se toma la fila de MENOR id por tipo: determinista, y con el UNIQUE
    #    `uq_emb_pricing_gasto_tipo` (models.py + init_db.py) nunca hay más de una.
    #    No se borra NADA acá a propósito: un DELETE es exactamente lo que rompe la llave.
    if payload.gastos is not None:
        enviados = {g.tipo: g for g in payload.gastos}
        iva_exento = {"arancel", "iva_importacion"}
        # INTENCIONES: el monto con el que quedaría cada tipo si este PUT se aplicara
        # (mismas reglas del backend: tipo ausente → 0, IVA forzado a 0 en los exentos).
        # Se calcula ANTES de tocar las filas porque el guard de abajo compara el valor
        # NUEVO contra el PERSISTIDO y contra la CxP que ya registra esa línea.
        intenciones: dict = {}
        for cat in GASTOS_CATALOGO:
            g = enviados.get(cat["tipo"])
            intenciones[cat["tipo"]] = (
                _f(g.monto_neto) if g else 0.0,
                0.0 if cat["tipo"] in iva_exento else (_f(g.iva) if g else 0.0),
            )
        # FAIL CLOSED antes de escribir: mover/editar el monto de una línea que ya está
        # en Cuentas por Pagar dejaría el costo landed y el pasivo diciendo cosas
        # distintas de la MISMA factura (y es la mecánica de la doble CxP: vaciar
        # 'agencia' para pasar la plata a 'otros'). Ver el docstring del guard.
        _bloqueo_monto_gasto_con_cxp(db, pricing, intenciones)
        actuales: dict = {}
        for fila in (
            db.query(EmbarquePricingGasto)
            .filter(EmbarquePricingGasto.pricing_id == pricing.id)
            .order_by(EmbarquePricingGasto.id.asc())
            .all()
        ):
            actuales.setdefault(fila.tipo, fila)
        for cat in GASTOS_CATALOGO:
            g = enviados.get(cat["tipo"])
            neto, iva = intenciones[cat["tipo"]]
            fila = actuales.get(cat["tipo"])
            if fila is None:                      # tipo que aún no tenía línea → se crea
                fila = EmbarquePricingGasto(pricing_id=pricing.id, tipo=cat["tipo"])
                db.add(fila)
            fila.glosa = cat["glosa"]
            fila.monto_neto = neto
            fila.iva = iva
            fila.capitaliza = cat["capitaliza"]
            fila.nro_factura = (g.nro_factura if g else None)
            fila.fecha_factura = (g.fecha_factura if g else None)
            fila.banco = (g.banco if g else None)
            fila.orden = cat["orden"]
        db.flush()

    # 3) Overrides por ítem (FOB y/o peso manual) — guardados como input que
    #    _build_inputs lee. FOB y peso comparten la MISMA fila emb_pricing_item y
    #    son INDEPENDIENTES: un flag en None significa "el usuario no tocó ese
    #    campo" y no se altera (evita que editar el peso revierta un FOB manual).
    overrides = {o.embarque_item_id: o for o in (payload.items or [])}
    if overrides:
        # Solo se aceptan overrides de ítems que pertenecen a ESTE embarque (evita
        # contaminar el snapshot con embarque_item_id ajenos o inexistentes, que hoy
        # se guardaban como filas huérfanas sin decir nada).
        valid_ids = {ei.id for ei in embarque.items}
        invalidos = sorted(eiid for eiid in overrides if eiid not in valid_ids)
        if invalidos:
            raise HTTPException(400, f"embarque_item_id no pertenece a este embarque: {invalidos}")
        existing = {
            si.embarque_item_id: si
            for si in db.query(EmbarquePricingItem)
            .filter(EmbarquePricingItem.pricing_id == pricing.id).all()
        }
        for eiid, o in overrides.items():
            row = existing.get(eiid)
            # FOB manual válido solo con valor (evita "manual + vacío" → costo 0).
            quiere_fob = o.fob_manual is True and o.fob_unit is not None
            # Peso manual válido solo con valor > 0: un peso 0/negativo no es real
            # y debe caer al peso de la cotización.
            quiere_peso = o.peso_manual is True and o.peso_unit_lbs is not None and _f(o.peso_unit_lbs) > 0

            # Crear la fila de override una sola vez si algún campo la necesita
            # (FOB y peso comparten fila).
            if (quiere_fob or quiere_peso) and row is None:
                row = EmbarquePricingItem(pricing_id=pricing.id, embarque_item_id=eiid)
                db.add(row)

            # FOB
            if quiere_fob:
                row.fob_unit = _f(o.fob_unit)
                row.fob_origen = "manual"
            elif o.fob_manual is False and row is not None and row.fob_origen == "manual":
                # Quitar override manual → volver al FOB por defecto (factura/cotización).
                row.fob_origen = "auto"
                row.fob_unit = 0

            # Peso (espejo del FOB)
            if quiere_peso:
                row.peso_unit_lbs = _f(o.peso_unit_lbs)
                row.peso_origen = "manual"
            elif o.peso_manual is False and row is not None and (row.peso_origen or "auto") == "manual":
                # Quitar override → volver al peso de la cotización.
                row.peso_origen = "auto"
                row.peso_unit_lbs = 0
        db.flush()

    # FLUSH antes del refresh: refresh expira el objeto y lo recarga desde la DB,
    # así que un PUT solo-encabezado (sin gastos ni ítems, que ya flushean arriba)
    # perdería silenciosamente los cambios pendientes del encabezado.
    db.flush()
    db.refresh(pricing)
    # Fail closed antes de calcular: un gasto negativo (sembrado desde Config, ver el
    # docstring del guard) subvaluaría TODOS los ítems y el estado 'calculado' lo dejaría
    # publicado en el listado.
    _validar_gastos_no_negativos(pricing)
    detail = _compute_detail(db, embarque, pricing)

    # 4) Persistir snapshot + estado
    _persist_snapshot(db, pricing, detail)
    tiene_costo = _f(pricing.tc_valor) > 0 and detail["totales"].get("costo_total_clp", 0) > 0
    pricing.estado = "calculado" if tiene_costo else "borrador"
    pricing.calculado_at = datetime.utcnow() if tiene_costo else None
    db.commit()
    # El snapshot ya es consistente con `detail`; solo reflejamos el estado nuevo
    # (evita recomputar todo el detalle por segunda vez).
    detail["pricing"]["estado"] = pricing.estado
    detail["pricing"]["calculado_at"] = pricing.calculado_at.isoformat() if pricing.calculado_at else None
    return detail


@router.post("/{embarque_id}/cerrar")
def cerrar_pricing(
    embarque_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _con_retry_deadlock(db, lambda: _cerrar_pricing_tx(embarque_id, db, current_user))


def _cerrar_pricing_tx(embarque_id: int, db: Session, current_user: User):
    embarque = _load_embarque(db, embarque_id)
    if not embarque:
        raise HTTPException(404, "Embarque no encontrado")
    # Bajo lock: el 409 de "ya cerrado" es lo único que impide que dos cierres
    # simultáneos congelen dos costos distintos para el mismo embarque.
    pricing = _get_or_create_pricing(db, embarque, bloquear=True)
    # Ya cerrado → NO recalcular ni sobreescribir el costo congelado.
    if pricing.estado == ESTADO_BLOQUEADO:
        raise HTTPException(409, "El pricing ya está cerrado; reábralo antes de volver a cerrarlo")
    if _f(pricing.tc_valor) <= 0:
        raise HTTPException(400, "Defina un TC mayor a 0 antes de cerrar")
    # Fail closed: cerrar CONGELA el costo. Un gasto negativo (sembrado desde Config) se
    # quedaba congelado para siempre porque acá solo se exigía costo_total > 0.
    _validar_gastos_no_negativos(pricing)
    # Fail closed (2): tampoco se congela un costo que NO cuadra con las Cuentas por
    # Pagar que registran esas mismas facturas. Es el callejón sin salida que midieron
    # los re-auditores: la divergencia se congelaba y después ya no había cómo corregirla.
    _bloqueo_monto_gasto_con_cxp(db, pricing)
    # Asegurar snapshot al día antes de congelar
    detail = _compute_detail(db, embarque, pricing)
    # No congelar un costo vacío: debe haber al menos un costo > 0 (FOB/flete/gastos).
    if detail["totales"].get("costo_total_clp", 0) <= 0:
        raise HTTPException(400, "El costo landed es 0. Cargue FOB, flete o gastos antes de cerrar")
    _persist_snapshot(db, pricing, detail)
    pricing.estado = ESTADO_BLOQUEADO
    pricing.calculado_at = pricing.calculado_at or datetime.utcnow()
    db.commit()
    db.refresh(pricing)
    return _compute_detail(db, embarque, pricing)


@router.post("/{embarque_id}/reabrir")
def reabrir_pricing(
    embarque_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _con_retry_deadlock(db, lambda: _reabrir_pricing_tx(embarque_id, db, current_user))


def _reabrir_pricing_tx(embarque_id: int, db: Session, current_user: User):
    embarque = _load_embarque(db, embarque_id)
    if not embarque:
        raise HTTPException(404, "Embarque no encontrado")
    pricing = _get_or_create_pricing(db, embarque, bloquear=True)
    pricing.estado = "calculado" if _f(pricing.tc_valor) > 0 else "borrador"
    db.commit()
    db.refresh(pricing)
    return _compute_detail(db, embarque, pricing)
