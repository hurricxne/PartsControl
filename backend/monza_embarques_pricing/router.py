"""API del módulo Embarques Pricing MonzaParts (Contabilidad → costo landed).

SOLO MonzaParts: candado require_empresa("automotriz"). Espejo del módulo de Grupo AM
(backend/embarques_pricing/router.py), apuntando a las tablas monza_*.

Integración NO invasiva con Logística: lee los embarques que crea Logística
(monza_embarques) y superpone su pricing. Por eso TODO embarque "aparece" acá; el
registro de pricing se crea diferido la primera vez que Contabilidad lo abre.

FOB por ítem:  DEFAULT = costo del ítem de cotización (estimado), editable a mano. Al
teclearlo, Contabilidad MARCA de dónde salió el número: `factura` (precio real de la
factura del proveedor) o `manual` (corrección a mano). Los dos son "el humano tecleó un
número" y pisan igual al costo de la cotización; la marca solo dice si el costo landed
está calculado con el precio REAL pagado o con el estimado. Monza no tiene tabla de
facturas de proveedor, así que el FOB real entra ACÁ, al costear el embarque.
Peso por ítem: DEFAULT = peso_kg del ítem de cotización, TAMBIÉN editable a mano. El peso
GOBIERNA el prorrateo del flete, así que un peso mal cargado deforma el costo landed de
todos los ítems: Contabilidad lo corrige y el flete se re-prorratea (Σ shipping intacta).
FOB y peso son overrides INDEPENDIENTES (tri-estado: fijar / volver a auto / no tocar).

Prefijo: /api/monza/embarques-pricing (montado sin prefix; el router ya lo trae).
"""
from datetime import datetime
from typing import List, Literal, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

from database import get_db
from auth import get_current_user
from empresa_guard import require_empresa
from models.models import User
from monza_models import (
    MonzaEmbarque, MonzaEmbarqueItem, MonzaCotizacionItem, MonzaDocumento, MonzaConfig,
)
from .models import MonzaEmbPricing, MonzaEmbPricingGasto, MonzaEmbPricingItem
from .service import calcular_landed, total_gastos_que_capitalizan, GASTOS_CATALOGO, _f
from .integration import (
    detect_tipo as _detect_tipo, ensure_pricing_for_embarque, get_cfg, tc_de_config,
    MONEDAS_CON_TC,
)

router = APIRouter(
    prefix="/api/monza/embarques-pricing",
    tags=["monza-embarques-pricing"],
    dependencies=[Depends(require_empresa("automotriz"))],
)

ESTADO_BLOQUEADO = "cerrado"

# Orígenes de FOB en los que el NÚMERO lo tecleó una persona (no sale de la cotización):
#   'factura' = precio real de la factura del proveedor · 'manual' = corrección a mano.
# Los dos pisan por igual al costo de la cotización (la precedencia NO depende de la
# marca); la marca existe para SABER si el costo landed usa el precio real o el estimado.
FOB_ORIGEN_TECLEADO = ("manual", "factura")


def _get_or_create_pricing(
    db: Session, embarque: MonzaEmbarque, *, bloquear: bool = False
) -> MonzaEmbPricing:
    """Crea (si falta) o devuelve el pricing del embarque. Nunca None hacia el endpoint.

    `bloquear=True` — rutas de ESCRITURA (guardar / cerrar / reabrir): relee la cabecera
    con populate_existing().with_for_update(), la regla de la casa para toda decisión de
    plata (docs/regla-lecturas-de-plata.md). El costo landed que se congela ES plata:
    sin el lock, dos POST /cerrar simultáneos leían los dos `estado != 'cerrado'`,
    recalculaban los dos y el segundo PISABA el snapshot del primero (dos costos
    distintos congelados para el mismo embarque, sin rastro). Con el lock el segundo
    espera, relee 'cerrado' y recibe el 409 de siempre. `populate_existing()` es
    imprescindible: sin él SQLAlchemy DESCARTA la fila fresca porque el objeto ya está
    en el identity map (lo metió ensure_pricing_for_embarque).
    """
    pricing = ensure_pricing_for_embarque(db, embarque, commit=True)
    if pricing is None:
        raise HTTPException(500, "No se pudo crear el registro de pricing del embarque")
    if not bloquear:
        return pricing
    bloqueado = (
        db.query(MonzaEmbPricing)
        .filter(MonzaEmbPricing.id == pricing.id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if bloqueado is None:
        # Logística borró el embarque entre el ensure y el lock.
        raise HTTPException(409, "El pricing del embarque ya no existe; recargue la pantalla")
    return bloqueado


def _con_retry_deadlock(db: Session, operacion):
    """Ejecuta `operacion()` reintentando ante deadlock / lock-timeout de InnoDB
    (1213 / 1205): el pricing lockea su cabecera y después escribe gastos y snapshot
    (con FK hacia monza_embarque_items, que Despachos y el costeo también lockean).
    MySQL puede elegir víctima; se reintenta la transacción completa en vez de
    devolverle un 500 al contador (mismo patrón que monza_recepcion_nacional)."""
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
        409, "Conflicto momentáneo con otra edición simultánea del pricing: reintente")


def _embarque_or_404(db: Session, embarque_id: int) -> MonzaEmbarque:
    emb = db.query(MonzaEmbarque).filter(MonzaEmbarque.id == embarque_id).first()
    if not emb:
        raise HTTPException(404, "Embarque no encontrado")
    return emb


def _embarque_items(db: Session, embarque_id: int) -> List[Tuple[MonzaEmbarqueItem, Optional[MonzaCotizacionItem]]]:
    """(MonzaEmbarqueItem, MonzaCotizacionItem|None) del embarque, en 2 queries (sin N+1)."""
    eis = (
        db.query(MonzaEmbarqueItem)
        .filter(MonzaEmbarqueItem.embarque_id == embarque_id)
        .order_by(MonzaEmbarqueItem.id.asc())
        .all()
    )
    item_ids = [ei.item_id for ei in eis if ei.item_id]
    cot_by_id = {}
    if item_ids:
        cot_by_id = {
            it.id: it for it in
            db.query(MonzaCotizacionItem).filter(MonzaCotizacionItem.id.in_(item_ids)).all()
        }
    return [(ei, cot_by_id.get(ei.item_id)) for ei in eis]


# ─── FOB por ítem: costo de la cotización → 0 (manual) ────────────────────────
def _fob_defaults(pairs) -> dict:
    """item_cotizacion_id → (fob_unit_default, origen). MonzaParts: el FOB estimado es el
    costo del ítem de cotización; si no hay, 0 con origen "auto" (sin dato, a cargar a
    mano). "manual"/"factura" se reservan para overrides reales del usuario (así el front
    muestra el botón de "volver al FOB de la cotización" solo cuando hay algo que
    revertir). Monza NO tiene facturas de proveedor en base: el FOB real no se puede leer
    de ninguna parte, lo carga Contabilidad al costear (y lo marca como 'factura')."""
    out: dict = {}
    for _ei, cot in pairs:
        if cot is None:
            continue
        if _f(cot.costo) > 0:
            out[cot.id] = (_f(cot.costo), "cotizacion")
        else:
            out[cot.id] = (0.0, "auto")
    return out


def _build_inputs(db: Session, pricing: MonzaEmbPricing, pairs) -> List[dict]:
    """Arma los inputs por ítem mezclando defaults (cotización) + overrides guardados (FOB y peso)."""
    fob_def = _fob_defaults(pairs)
    stored = {
        si.embarque_item_id: si
        for si in db.query(MonzaEmbPricingItem)
        .filter(MonzaEmbPricingItem.pricing_id == pricing.id).all()
    }
    tc_header = _f(pricing.tc_valor)
    inputs: List[dict] = []
    for ei, cot in pairs:
        icid = cot.id if cot else None
        default_fob, default_origen = fob_def.get(icid, (0.0, "manual"))
        s = stored.get(ei.id)
        # Override tecleado ('manual' o 'factura') solo si trae un valor > 0: un
        # override en 0 (ítem que nunca tuvo precio, o 0 explícito) no es un precio
        # real y NO debe bloquear el FOB default de la cotización que llega/cambia
        # después. La marca de origen se conserva tal cual vino guardada: 'factura'
        # y 'manual' tienen la MISMA precedencia, solo se distinguen para saber si el
        # costo landed se calculó con el precio real del proveedor o con el estimado.
        if s is not None and (s.fob_origen or "") in FOB_ORIGEN_TECLEADO and _f(s.fob_unit) > 0:
            fob_unit, origen = _f(s.fob_unit), s.fob_origen
        else:
            fob_unit, origen = default_fob, default_origen

        # Peso: default de la cotización (peso_kg); override manual solo si trae un
        # valor > 0. Espejo del FOB: un "manual" en 0 no es un peso real (una pieza
        # física pesa > 0) y NO debe pisar el peso de la cotización.
        default_peso = _f(cot.peso_kg) if cot else 0.0
        if s is not None and (s.peso_origen or "auto") == "manual" and _f(s.peso_unit_kg) > 0:
            peso_unit, peso_origen = _f(s.peso_unit_kg), "manual"
        else:
            peso_unit, peso_origen = default_peso, "auto"

        inputs.append({
            "embarque_item_id": ei.id,
            "item_cotizacion_id": icid,
            "numero_parte": (cot.numero_parte if cot else None) or "",
            "descripcion": (cot.descripcion if cot else None) or "",
            "moneda": pricing.moneda,
            "cantidad": _f(cot.cantidad) if cot else 0.0,
            "peso_unit": peso_unit,
            "peso_default": default_peso,
            "peso_origen": peso_origen,
            "fob_unit": fob_unit,
            "fob_default": default_fob,
            "fob_origen": origen,
            "tc_valor": tc_header,
        })
    return inputs


def _shipping_total_clp(pricing: MonzaEmbPricing) -> float:
    """Flete total en CLP: ME × TC si viene en moneda extranjera, o el CLP directo."""
    if pricing.flete_en_me:
        return _f(pricing.shipping_me) * _f(pricing.tc_valor)
    return _f(pricing.shipping_clp)


def _serialize_gasto(g: MonzaEmbPricingGasto) -> dict:
    neto, iva = _f(g.monto_neto), _f(g.iva)
    return {
        "id": g.id, "tipo": g.tipo, "glosa": g.glosa,
        "monto_neto": neto, "iva": iva, "total_bruto": neto + iva,
        "capitaliza": bool(g.capitaliza), "nro_factura": g.nro_factura,
        "fecha_factura": g.fecha_factura, "banco": g.banco, "orden": g.orden,
    }


def _advertencias(pairs, pricing: MonzaEmbPricing, cfg=None) -> List[str]:
    """Avisos VISIBLES del embarque que NO bloquean el costeo (se muestran en el detalle).

    EL PROBLEMA DE FONDO: el pricing maneja UNA sola moneda para todo el embarque
    (`monza_emb_pricing.moneda`) y UN solo TC. La moneda se SIEMBRA una vez, con la del
    PRIMER ítem (`integration.moneda_de_embarque`), y **nunca se re-sincroniza**. Un FOB
    convertido con el TC equivocado deforma el costo de TODOS los ítems EN SILENCIO, y el
    cierre lo congela.

    Son avisos y no 409 a propósito: el embarque ya llegó físicamente y este módulo es el
    ÚNICO lugar donde se registra su costo — bloquear el guardado dejaría mercadería recibida
    sin costear, que es peor que costearla avisando.

    LOS 4 CASOS QUE SE AVISAN (el aviso original cubría SOLO el 2, que es justo el que el
    dueño dice que no pasa, y era ciego a los que sí pasan):
      1. La moneda del costeo NO es la de los ítems del embarque. **Este es el caso real**:
         el ítem se edita después de que el pricing nació, y el pricing se queda con la
         moneda y el TC viejos. Pasa con UN SOLO ítem, así que el `len(monedas) <= 1` de
         antes lo hacía invisible.
      2. Los ítems del embarque no se ponen de acuerdo entre ellos (consolidado multi-moneda).
      3. El costeo está en una moneda que este módulo no sabe convertir (MonzaConfig solo
         tiene TC de USD y EUR): un FOB en pesos multiplicado por el TC del dólar.
      4. El TC cargado es EL DEL DÓLAR en un embarque que se costea en otra moneda (se cambió
         la moneda y quedó el TC). Se compara contra Config, sin heurísticas.
    """
    avisos: List[str] = []
    moneda_pricing = (pricing.moneda or "USD").strip().upper()
    monedas = sorted({
        (cot.moneda or "").strip().upper()
        for _ei, cot in pairs
        if cot is not None and (cot.moneda or "").strip()
    })

    # (1) el costeo usa una moneda distinta a la de los ítems
    ajenas = [m for m in monedas if m != moneda_pricing]
    if ajenas:
        avisos.append(
            f"El costo landed se está calculando en {moneda_pricing} con un solo tipo de "
            f"cambio, pero los ítems de este embarque están en {', '.join(ajenas)}. El FOB de "
            f"esos ítems va a quedar mal convertido. Cambia la moneda del embarque a la del "
            f"ítem (y su tipo de cambio), o carga el FOB ya expresado en {moneda_pricing}."
        )

    # (2) los ítems traen monedas distintas ENTRE ELLOS
    if len(monedas) > 1:
        avisos.append(
            f"Este embarque trae ítems en más de una moneda ({', '.join(monedas)}), pero el "
            f"costo landed se calcula todo en {moneda_pricing} con un solo tipo de cambio. "
            f"Carga el FOB de todos los ítems en {moneda_pricing}, o pide separar el embarque "
            f"por moneda."
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
    tc_usd = tc_de_config(cfg, "USD")
    tc_propio = tc_de_config(cfg, moneda_pricing)
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
# Espejo exacto de embarques_pricing/router.py (Grupo AM). Tolerancia de 1 peso: el CLP
# se redondea a entero en todo el módulo (y en CxP) → 1 peso es ruido, no una edición.
TOL_CLP = 1.0


def _clp(x) -> str:
    """Monto en pesos con separador de miles chileno (para los mensajes de error)."""
    return f"{_f(x):,.0f}".replace(",", ".")


def _lineas_gasto(db: Session, pricing: MonzaEmbPricing) -> List[MonzaEmbPricingGasto]:
    """Las líneas de gasto TAL COMO ESTÁN EN LA BD (una por tipo, la de menor id).

    Relee con `populate_existing()` — regla de la casa para toda decisión de plata: el
    guard compara el monto PERSISTIDO contra la CxP, y el identity map podría servir un
    snapshot viejo de la fila (la trajo `ensure_pricing_for_embarque` antes del lock).
    Se llama siempre ANTES de modificar las filas o DESPUÉS de un flush, nunca con
    cambios pendientes en la sesión.
    """
    vistos: dict = {}
    for fila in (
        db.query(MonzaEmbPricingGasto)
        .filter(MonzaEmbPricingGasto.pricing_id == pricing.id)
        .order_by(MonzaEmbPricingGasto.id.asc())
        .populate_existing()
        .all()
    ):
        vistos.setdefault(fila.tipo, fila)
    return sorted(vistos.values(), key=lambda g: (g.orden or 0, g.id))


def _pares_gasto_cxp(db: Session, pricing: MonzaEmbPricing) -> List[tuple]:
    """[(gasto, compra_id, neto_cxp_clp, bruto_cxp_clp)] de las líneas que YA están
    registradas como Cuenta por Pagar ACTIVA de MonzaParts. Una sola query (sin N+1).

    Import local de `monza_compras_contab.models` a propósito: este módulo no depende de
    Contabilidad al cargar, y los dos viven detrás del MISMO flag (main.py:95) así que
    cuando este router está montado la tabla existe. Las tablas monza_* son de UNA marca:
    no hace falta candado de empresa extra (el router ya exige 'automotriz').
    """
    from monza_compras_contab.models import MonzaContCompra

    filas = _lineas_gasto(db, pricing)
    ids = [g.id for g in filas]
    if not ids:
        return []
    reg: dict = {}
    for cid, gid, neto, tc, bruto_clp in (
        db.query(MonzaContCompra.id, MonzaContCompra.emb_pricing_gasto_id,
                 MonzaContCompra.monto_neto, MonzaContCompra.tc,
                 MonzaContCompra.monto_total_clp)
        .filter(MonzaContCompra.emb_pricing_gasto_id.in_(ids),
                MonzaContCompra.anulado.is_(False))
        .order_by(MonzaContCompra.id.asc())
        .all()
    ):
        tc_v = _f(tc) or 1.0
        reg.setdefault(int(gid), (int(cid), round(_f(neto) * tc_v, 2), round(_f(bruto_clp), 2)))
    return [(g, *reg[g.id]) for g in filas if g.id in reg]


def _bloqueo_monto_gasto_con_cxp(
    db: Session, pricing: MonzaEmbPricing, intenciones: Optional[dict] = None
) -> None:
    """FAIL CLOSED: el monto de una línea YA registrada en CxP no puede quedar divergente.

    EL DAÑO QUE CIERRA (re-auditoría, HALLAZGO 2 — y con él la mecánica del HALLAZGO 1)
    ---------------------------------------------------------------------------------
    La ronda anterior estabilizó la IDENTIDAD del gasto (la PK sobrevive al re-guardado),
    pero NO el MONTO: con la llave estable, la línea se podía editar libremente después de
    registrar la CxP y nada reconciliaba nada. El overlay seguía diciendo "En compras ✓"
    (lo calcula por id de gasto, sin mirar la plata), el costo landed se recalculaba con el
    monto NUEVO, la CxP se quedaba con el VIEJO y al cerrar la divergencia quedaba
    CONGELADA — reproducido por los re-auditores en las DOS marcas con los mismos números.
    Y es también la mecánica de la DOBLE CxP: mover el monto de 'agencia' a 'otros' dejaba
    la línea vieja "registrada" en $0 (pill verde sobre un cero) y la nueva "no registrada"
    con la plata → un clic y Σ CxP = 380.800 por una factura de 190.400.

    LA REGLA (se eligió BLOQUEAR, no avisar ni propagar)
    ---------------------------------------------------
    Propagar reescribiría en silencio un pasivo que puede estar pagado y conciliado con el
    banco. Avisar es lo que ya había (nada). Así que el cambio de monto de una línea con
    CxP ACTIVA se RECHAZA con 409 y el mensaje nombra las dos salidas reales.

    DOS PRECISIONES QUE HACEN QUE NO SE VUELVA UN MURO
      · Solo se juzga la línea que el PUT REALMENTE cambia (un pricing legado ya
        divergente no secuestra la pantalla).
      · Un cambio que CONVERGE hacia la CxP se ACEPTA (camino de reparación).
    Con `intenciones=None` (POST /cerrar) se juzga el estado PERSISTIDO: cerrar congela.

    Los demás campos (N° de factura, banco, fecha) quedan editables a propósito: escribir
    el N° de la factura es el remedio que pide el anti-duplicado por factura física.
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


def _avisos_gastos_vs_cxp(db: Session, pricing: MonzaEmbPricing) -> List[str]:
    """Aviso VISIBLE (no bloqueante) de las divergencias línea ↔ CxP que ya existen.

    Va en `advertencias` del detalle, así el contador VE la divergencia en la pantalla en
    vez de descubrirla cuando el cierre le devuelve un 409. No bloquea la lectura: los
    datos legados divergentes tienen que poder abrirse para poder repararse.
    """
    avisos: List[str] = []
    for g, compra_id, neto_cxp, bruto_cxp in _pares_gasto_cxp(db, pricing):
        bruto_linea = round(_f(g.monto_neto) + _f(g.iva), 2)
        if (abs(bruto_linea - bruto_cxp) <= TOL_CLP
                and abs(round(_f(g.monto_neto), 2) - neto_cxp) <= TOL_CLP):
            continue
        avisos.append(
            f"La línea «{g.glosa or g.tipo}» ({_clp(bruto_linea)}) NO cuadra con la compra "
            f"#{compra_id} que la registra en Cuentas por Pagar ({_clp(bruto_cxp)}). El "
            f"costo del embarque y el pasivo están diciendo cosas distintas de la misma "
            f"factura: corrija la línea (dejándola en el monto de la compra) o revierta la "
            f"compra y regístrela de nuevo. No se puede cerrar el pricing así."
        )
    return avisos


def _snapshot_items(db: Session, pricing: MonzaEmbPricing) -> List[dict]:
    """Filas del snapshot persistido (pricing cerrado → costo CONGELADO).

    Devuelve las filas guardadas al cerrar, con la misma forma que el recálculo
    en vivo, para que el detalle cuadre siempre con el listado.
    """
    snap = (
        db.query(MonzaEmbPricingItem)
        .filter(MonzaEmbPricingItem.pricing_id == pricing.id)
        .order_by(MonzaEmbPricingItem.id.asc())
        .all()
    )
    return [{
        "embarque_item_id": s.embarque_item_id,
        "item_cotizacion_id": s.item_cotizacion_id,
        "numero_parte": s.numero_parte or "",
        "descripcion": s.descripcion or "",
        "moneda": s.moneda,
        "cantidad": _f(s.cantidad),
        "peso_unit_kg": _f(s.peso_unit_kg),
        # Cerrado no se edita: el default mostrado es el mismo peso congelado.
        "peso_default": _f(s.peso_unit_kg),
        "peso_origen": s.peso_origen or "auto",
        "peso_total_kg": round(_f(s.peso_total_kg), 2),
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


def _compute_detail(db: Session, embarque: MonzaEmbarque, pricing: MonzaEmbPricing) -> dict:
    gastos = sorted(pricing.gastos, key=lambda g: (g.orden or 0, g.id))
    gastos_dicts = [_serialize_gasto(g) for g in gastos]
    total_cap = total_gastos_que_capitalizan(
        [{"monto_neto": g["monto_neto"], "capitaliza": g["capitaliza"]} for g in gastos_dicts]
    )
    total_iva = sum(g["iva"] for g in gastos_dicts if g["capitaliza"])
    iva_importacion = sum(g["monto_neto"] for g in gastos_dicts if g["tipo"] == "iva_importacion")

    shipping_total = _shipping_total_clp(pricing)
    pairs = _embarque_items(db, embarque.id)
    cfg = get_cfg(db)      # una sola lectura: la usan tc_config y las advertencias

    # Cerrado → el detalle sale del snapshot persistido al cerrar (costo
    # CONGELADO, igual que el listado). Abierto → recálculo en vivo.
    if pricing.estado == ESTADO_BLOQUEADO:
        items_out = _snapshot_items(db, pricing)
    else:
        inputs = _build_inputs(db, pricing, pairs)
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
                "peso_unit_kg": r["peso_unit"],
                "peso_default": r.get("peso_default", 0.0),
                "peso_origen": r.get("peso_origen", "auto"),
                "peso_total_kg": round(r["peso_total"], 2),
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

    # Documentos adjuntos del embarque (Logística los sube o no) → trazabilidad,
    # espejo del bloque "documentos" de GA. En Monza los adjuntos viven en la tabla
    # genérica monza_documentos (entidad="embarque"), con categorías abiertas.
    documentos = [
        {
            "id": d.id, "categoria": d.categoria, "original_name": d.original_name,
            "fecha": d.fecha.isoformat() if d.fecha else None,
        }
        for d in db.query(MonzaDocumento)
        .filter(MonzaDocumento.entidad == "embarque", MonzaDocumento.entidad_id == embarque.id)
        .order_by(MonzaDocumento.id.asc())
        .all()
    ]

    return {
        "embarque": {
            "id": embarque.id, "numero": embarque.numero, "estado": embarque.estado,
            "forwarder": embarque.forwarder, "awb": embarque.awb, "tracking": embarque.tracking,
            "fecha_despacho": embarque.fecha_despacho, "fecha_llegada_est": embarque.fecha_llegada_est,
            "n_items": len(pairs),
            "documentos": documentos,
        },
        "pricing": {
            "id": pricing.id,
            "correlativo": pricing.id,
            "tipo_embarque": pricing.tipo_embarque,
            "tc_tipo": pricing.tc_tipo, "tc_valor": _f(pricing.tc_valor),
            # TC vigente de MonzaConfig como SUGERENCIA (la UI muestra "sugerido: X").
            # Solo USD y EUR tienen TC parametrizado; cualquier otra moneda devuelve 0
            # (fail closed) y la advertencia (3) lo explica en pantalla.
            "tc_config": tc_de_config(cfg, pricing.moneda or "USD"),
            "moneda": pricing.moneda, "flete_en_me": bool(pricing.flete_en_me),
            "shipping_me": _f(pricing.shipping_me), "shipping_clp": _f(pricing.shipping_clp),
            "shipping_total_clp": round(shipping_total, 0),
            "estado": pricing.estado, "observaciones": pricing.observaciones,
            "calculado_at": pricing.calculado_at.isoformat() if pricing.calculado_at else None,
        },
        # Avisos no bloqueantes (moneda/TC divergentes + línea de gasto que no cuadra con
        # la CxP que la registra). Lista vacía = todo en orden.
        "advertencias": _advertencias(pairs, pricing, cfg) + _avisos_gastos_vs_cxp(db, pricing),
        "gastos": gastos_dicts,
        "totales_gastos": {
            "total_capitaliza": round(total_cap, 0),
            "total_iva": round(total_iva, 0),
            "iva_importacion": round(iva_importacion, 0),
        },
        "items": items_out,
        "totales": {
            "n_items": len(items_out),
            "peso_total_kg": round(sum(r["peso_total_kg"] for r in items_out), 2),
            "fob_total_me": round(sum(r["fob_total"] for r in items_out), 2),
            "fob_clp": sum(r["fob_clp"] for r in items_out),
            "shipping_clp": sum(r["shipping_clp"] for r in items_out),
            "cif_clp": sum(r["cif_clp"] for r in items_out),
            "gastos_clp": sum(r["gastos_clp"] for r in items_out),
            "costo_total_clp": sum(r["costo_total_clp"] for r in items_out),
        },
    }


def _validar_gastos_no_negativos(pricing: MonzaEmbPricing) -> None:
    """FAIL CLOSED: ningún gasto NEGATIVO puede entrar al costo landed ni congelarse.

    El `ge=0` de `GastoIn` cubre la puerta del PUT, pero NO es la única: los montos de
    Desconsolidación / Almacenaje / Agencia se siembran desde MonzaConfig
    (`integration.seed_gastos`) y su editor no valida signo. Con un parámetro negativo
    sembrado, un PUT SOLO-ENCABEZADO (que ni pasa por GastoIn) y después un POST /cerrar
    congelaban un total capitalizable NEGATIVO: `total_gastos_que_capitalizan` los SUMA, así
    que el negativo RESTA del pozo que se prorratea a TODOS los ítems, y `cerrar_pricing`
    solo exigía `costo_total > 0`.

    Se valida acá, en el punto donde el costo se calcula y se PERSISTE, para que la defensa
    no dependa de por dónde entró el dato (payload, seed de Config, edición directa en BD o un
    escritor futuro). Es 400 con la línea culpable nombrada.
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
            "parámetro en Configuración de MonzaParts si viene de ahí) antes de guardar.",
        )


def _persist_snapshot(db: Session, pricing: MonzaEmbPricing, detail: dict) -> None:
    """Guarda el snapshot por ítem (inputs + computados) para congelar el costo."""
    db.query(MonzaEmbPricingItem).filter(MonzaEmbPricingItem.pricing_id == pricing.id).delete()
    db.flush()  # materializa el borrado antes de re-insertar (sin colisiones en el identity map)
    for r in detail["items"]:
        db.add(MonzaEmbPricingItem(
            pricing_id=pricing.id,
            embarque_item_id=r["embarque_item_id"],
            item_cotizacion_id=r["item_cotizacion_id"],
            numero_parte=r["numero_parte"], descripcion=r["descripcion"], moneda=r["moneda"],
            cantidad=r["cantidad"], peso_unit_kg=r["peso_unit_kg"],
            peso_total_kg=r["peso_total_kg"], peso_origen=r.get("peso_origen", "auto"),
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
    embarques = db.query(MonzaEmbarque).order_by(MonzaEmbarque.id.desc()).all()
    pricings = {p.embarque_id: p for p in db.query(MonzaEmbPricing).all()}
    # Conteo de ítems por embarque (1 query agrupada).
    n_items = {
        eid: int(n) for eid, n in
        db.query(MonzaEmbarqueItem.embarque_id, func.count(MonzaEmbarqueItem.id))
        .group_by(MonzaEmbarqueItem.embarque_id).all()
    }
    # Costo total por pricing (1 query agrupada).
    costos = {
        pid: _f(total) for pid, total in
        db.query(MonzaEmbPricingItem.pricing_id, func.sum(MonzaEmbPricingItem.costo_total_clp))
        .group_by(MonzaEmbPricingItem.pricing_id).all()
    }
    # Documentos adjuntos por embarque (1 query agrupada, sin N+1): espejo del
    # docs_count de GA, pero contando filas de monza_documentos (categorías abiertas).
    docs_count = {
        eid: int(n) for eid, n in
        db.query(MonzaDocumento.entidad_id, func.count(MonzaDocumento.id))
        .filter(MonzaDocumento.entidad == "embarque")
        .group_by(MonzaDocumento.entidad_id).all()
    }
    out = []
    for e in embarques:
        if q:
            ql = q.lower()
            # `tracking` entra al buscador (Fase 9): es el N° con el que el forwarder
            # identifica la carga, y era el único identificador del embarque por el que NO
            # se podía buscar — el dueño lo tiene a mano en el correo del forwarder y hasta
            # ahora tenía que ir a Logística a traducirlo a N° de embarque para costear.
            # Monza no necesita la columna `awb_numero` que sí necesitó Grupo AM: acá `awb`
            # ya es texto libre (el número) y los adjuntos viven aparte en monza_documentos.
            hay = " ".join([e.numero or "", e.forwarder or "", e.awb or "",
                            e.tracking or ""]).lower()
            if ql not in hay:
                continue
        p = pricings.get(e.id)
        out.append({
            "embarque_id": e.id,
            "correlativo": p.id if p else None,
            "numero": e.numero,
            "estado_logistica": e.estado,
            "forwarder": e.forwarder,
            "awb": e.awb,
            # Se expone junto al AWB para que la fila muestre el mismo identificador por el
            # que se acaba de buscar (si no, uno busca por tracking y no ve por qué calzó).
            "tracking": e.tracking,
            "fecha_despacho": e.fecha_despacho,
            "n_items": n_items.get(e.id, 0),
            "docs_count": docs_count.get(e.id, 0),
            "tipo_embarque": p.tipo_embarque if p else _detect_tipo(e.forwarder, "USD"),
            "pricing_estado": p.estado if p else "sin_pricing",
            "moneda": p.moneda if p else None,
            "tc_valor": _f(p.tc_valor) if p else None,
            "costo_total_clp": round(costos.get(p.id, 0.0), 0) if p else None,
        })
    return out


# ─── Parámetros de costeo (MonzaConfig, lo que usa este módulo) ────────────────
# POR QUÉ VIVE ACÁ: `seed_gastos` precarga las 3 líneas afectas del pricing desde
# `MonzaConfig.{desconsolidado_clp, bodegaje_clp, costo_agencia_minimo_clp}`, columnas que se
# crearon con DEFAULT 0 y que **ningún endpoint podía escribir**: `monza_router_config.ConfigIn`
# no las declara y su PUT solo hace setattr de los campos del schema, así que se quedaban en 0
# PARA SIEMPRE y la precarga producía exactamente lo mismo que antes (el costo landed se
# congelaba sin gastos de internación). Este endpoint las hace cargables desde la propia
# pantalla de Contabilidad, con `ge=0` (un parámetro negativo subvalúa el landed de todos los
# ítems). El TC EUR va incluido para que la pantalla sea la misma en las dos marcas.
# Ruta de 2 segmentos: no compite con `/{embarque_id}`, que matchea uno solo.
class ParametrosCosteoIn(BaseModel):
    """Todos con piso en 0 (ver `_validar_gastos_no_negativos`)."""
    tc_eur_clp: Optional[float] = Field(None, ge=0)
    desconsolidado_clp: Optional[float] = Field(None, ge=0)
    bodegaje_clp: Optional[float] = Field(None, ge=0)
    costo_agencia_minimo_clp: Optional[float] = Field(None, ge=0)


def _cfg_or_create(db: Session) -> MonzaConfig:
    cfg = get_cfg(db)
    if cfg is None:
        cfg = MonzaConfig(id=1)
        db.add(cfg)
        db.commit()
        db.refresh(cfg)
    return cfg


def _parametros_out(cfg: MonzaConfig) -> dict:
    return {
        # El TC USD se edita en Configuración de MonzaParts (es su parámetro principal); acá
        # va solo para que la pantalla pueda mostrar el par y comparar.
        "tc_usd_clp": _f(cfg.tc_usd_clp),
        "tc_usd_clp_editable": False,
        "tc_eur_clp": _f(cfg.tc_eur_clp),
        "desconsolidado_clp": _f(getattr(cfg, "desconsolidado_clp", 0)),
        "bodegaje_clp": _f(getattr(cfg, "bodegaje_clp", 0)),
        "costo_agencia_minimo_clp": _f(getattr(cfg, "costo_agencia_minimo_clp", 0)),
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
    """Actualiza los parámetros de costeo. Solo toca los campos que vengan en el payload y NO
    recalcula embarques ya creados: son el DEFAULT con el que nace el pricing siguiente, y los
    montos siguen siendo editables por embarque."""
    cfg = _cfg_or_create(db)
    cambios = payload.model_dump(exclude_none=True)
    if not cambios:
        raise HTTPException(400, "No se recibió ningún parámetro para actualizar")
    for campo, valor in cambios.items():
        setattr(cfg, campo, valor)
    cfg.ultima_actualizacion = datetime.utcnow()
    cfg.usuario_email = getattr(current_user, "email", None)
    db.commit()
    db.refresh(cfg)
    return _parametros_out(cfg)


@router.get("/{embarque_id}")
def detalle_embarque_pricing(
    embarque_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    embarque = _embarque_or_404(db, embarque_id)
    pricing = _get_or_create_pricing(db, embarque)
    return _compute_detail(db, embarque, pricing)


# ── Payloads ──
GastoTipo = Literal["desconsolidacion", "almacenaje", "agencia", "arancel", "otros", "iva_importacion"]
TipoEmbarque = Literal["normal", "courier", "baukat", "fastmark"]


# Montos siempre >= 0 (un negativo corrompería el costo landed); strings con tope de
# longitud para fallar limpio en la API antes de tocar la BD.
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
    """Override por ítem de FOB y/o peso. Los dos flags son TRI-ESTADO:

    True = fijar manual · False = volver a auto · None = no tocar este campo (el
    usuario no lo editó). Sin el None, editar SOLO el peso revertiría en silencio un
    FOB manual guardado (y viceversa): comparten la misma fila monza_emb_pricing_item.
    El backend es la autoridad del override.
    """
    embarque_item_id: int
    fob_unit: Optional[float] = Field(None, ge=0)
    fob_manual: Optional[bool] = None
    # ¿De dónde salió el FOB tecleado? True = de la FACTURA REAL del proveedor
    # (fob_origen='factura'), False/ausente = corrección a mano (fob_origen='manual').
    # Solo se lee cuando fob_manual is True (fijar valor); en el reseteo no aplica.
    # Ausente = 'manual' a propósito: es el comportamiento histórico y el conservador
    # (nunca se afirma que un número viene de una factura si el cliente no lo dijo).
    fob_es_factura: Optional[bool] = None
    # Peso >= 0: un peso negativo no existe. El 0 se acepta en la API y el backend lo
    # trata como "sin peso real" → cae al peso de la cotización (igual que el FOB).
    peso_unit_kg: Optional[float] = Field(None, ge=0)
    peso_manual: Optional[bool] = None


class PricingSaveIn(BaseModel):
    tipo_embarque: Optional[TipoEmbarque] = None
    tc_tipo: Optional[Literal["manual", "config"]] = None
    # tc_valor >= 0: 0 = "aún sin TC" (borrador). La regla TC > 0 para calcular/cerrar
    # se valida aparte en los endpoints.
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
    """Guarda encabezado + gastos + overrides de FOB/peso, recalcula y persiste el snapshot."""
    return _con_retry_deadlock(
        db, lambda: _guardar_embarque_pricing_tx(embarque_id, payload, db, current_user))


def _guardar_embarque_pricing_tx(
    embarque_id: int, payload: PricingSaveIn, db: Session, current_user: User
):
    embarque = _embarque_or_404(db, embarque_id)
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

    if pricing.flete_en_me and _f(pricing.shipping_me) > 0 and _f(pricing.tc_valor) <= 0:
        raise HTTPException(400, "Defina un TC > 0 para convertir el flete en moneda extranjera a CLP")
    if pricing.flete_en_me:
        pricing.shipping_clp = round(_f(pricing.shipping_me) * _f(pricing.tc_valor), 0)

    # 2) Gastos: SIEMPRE las 6 líneas canónicas (el backend es la autoridad).
    #    UPSERT por la llave natural (pricing_id, tipo) — NO delete + re-insert.
    #    Esto NO es un detalle de implementación: la PK de monza_emb_pricing_gasto es la
    #    llave que `monza_cont_compra.emb_pricing_gasto_id` referencia para saber que la
    #    factura del forwarder ya está registrada como CxP, y esa FK es ON DELETE **SET
    #    NULL**. Con el borrado anterior, cada guardado del pricing (el front SIEMPRE manda
    #    `gastos`, así que basta con corregir el TC) re-insertaba las 6 filas con PK nuevas
    #    → la compra ya registrada quedaba con la llave en NULL → el botón «Registrar como
    #    compra» reaparecía en pantalla → dos clics y la MISMA factura del forwarder entraba
    #    dos veces (Σ CxP duplicada, reproducido en tests/test_llave_gasto_estable.py).
    #    Con el upsert las PK sobreviven al re-guardado y el lock + el 409 de crear_compra
    #    siguen valiendo. Se toma la fila de MENOR id por tipo: determinista, y con el
    #    UNIQUE `uq_monza_emb_pricing_gasto_tipo` (models.py + init_db.py) nunca hay más de
    #    una. No se borra NADA acá a propósito: un DELETE es lo que rompe la llave.
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
            db.query(MonzaEmbPricingGasto)
            .filter(MonzaEmbPricingGasto.pricing_id == pricing.id)
            .order_by(MonzaEmbPricingGasto.id.asc())
            .all()
        ):
            actuales.setdefault(fila.tipo, fila)
        for cat in GASTOS_CATALOGO:
            g = enviados.get(cat["tipo"])
            neto, iva = intenciones[cat["tipo"]]
            fila = actuales.get(cat["tipo"])
            if fila is None:                      # tipo que aún no tenía línea → se crea
                fila = MonzaEmbPricingGasto(pricing_id=pricing.id, tipo=cat["tipo"])
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
    #    _build_inputs lee. FOB y peso comparten la MISMA fila monza_emb_pricing_item y
    #    son INDEPENDIENTES: un flag en None significa "el usuario no tocó ese campo" y
    #    no se altera (evita que editar el peso revierta un FOB manual, y al revés).
    overrides = {o.embarque_item_id: o for o in (payload.items or [])}
    if overrides:
        # Solo se aceptan overrides de ítems que pertenecen a ESTE embarque (evita
        # contaminar el snapshot con embarque_item_id ajenos o inexistentes).
        valid_ids = {ei.id for ei, _ in _embarque_items(db, embarque.id)}
        invalidos = sorted(eiid for eiid in overrides if eiid not in valid_ids)
        if invalidos:
            raise HTTPException(400, f"embarque_item_id no pertenece a este embarque: {invalidos}")
        existing = {
            si.embarque_item_id: si
            for si in db.query(MonzaEmbPricingItem)
            .filter(MonzaEmbPricingItem.pricing_id == pricing.id).all()
        }
        for eiid, o in overrides.items():
            row = existing.get(eiid)
            # FOB manual válido solo con valor (evita "manual + vacío" → costo 0).
            quiere_fob = o.fob_manual is True and o.fob_unit is not None
            # Peso manual válido solo con valor > 0: un peso 0/negativo no es real
            # y debe caer al peso de la cotización.
            quiere_peso = (
                o.peso_manual is True and o.peso_unit_kg is not None and _f(o.peso_unit_kg) > 0
            )

            # Crear la fila de override una sola vez si algún campo la necesita
            # (FOB y peso comparten fila).
            if (quiere_fob or quiere_peso) and row is None:
                row = MonzaEmbPricingItem(pricing_id=pricing.id, embarque_item_id=eiid)
                db.add(row)

            # FOB
            if quiere_fob:
                row.fob_unit = _f(o.fob_unit)
                # De dónde salió el número: factura real del proveedor vs corrección a
                # mano. Ausente → 'manual' (histórico + conservador: no se le atribuye
                # a una factura un número que el cliente no marcó como tal).
                row.fob_origen = "factura" if o.fob_es_factura is True else "manual"
            elif o.fob_manual is False and row is not None and (row.fob_origen or "") in FOB_ORIGEN_TECLEADO:
                # Quitar el override tecleado (sea 'manual' o 'factura') → volver al FOB
                # default (costo de la cotización, o "sin dato" si la cotización no trae).
                row.fob_origen = "auto"
                row.fob_unit = 0

            # Peso (espejo del FOB)
            if quiere_peso:
                row.peso_unit_kg = _f(o.peso_unit_kg)
                row.peso_origen = "manual"
            elif o.peso_manual is False and row is not None and (row.peso_origen or "auto") == "manual":
                # Quitar override → volver al peso_kg de la cotización.
                row.peso_origen = "auto"
                row.peso_unit_kg = 0
        db.flush()

    # FLUSH antes del refresh: refresh expira el objeto y lo recarga desde la DB,
    # así que un PUT solo-encabezado (sin gastos ni ítems, que ya flushean arriba)
    # perdería silenciosamente los cambios pendientes del encabezado.
    db.flush()
    db.refresh(pricing)
    # Fail closed antes de calcular: un gasto negativo (sembrado desde MonzaConfig, ver el
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
    embarque = _embarque_or_404(db, embarque_id)
    # Bajo lock: el 409 de "ya cerrado" es lo único que impide que dos cierres
    # simultáneos congelen dos costos distintos para el mismo embarque.
    pricing = _get_or_create_pricing(db, embarque, bloquear=True)
    # Ya cerrado → NO recalcular ni sobreescribir el costo congelado.
    if pricing.estado == ESTADO_BLOQUEADO:
        raise HTTPException(409, "El pricing ya está cerrado; reábralo antes de volver a cerrarlo")
    if _f(pricing.tc_valor) <= 0:
        raise HTTPException(400, "Defina un TC mayor a 0 antes de cerrar")
    # Fail closed: cerrar CONGELA el costo. Un gasto negativo (sembrado desde MonzaConfig) se
    # quedaba congelado para siempre porque acá solo se exigía costo_total > 0.
    _validar_gastos_no_negativos(pricing)
    # Fail closed (2): tampoco se congela un costo que NO cuadra con las Cuentas por
    # Pagar que registran esas mismas facturas. Es el callejón sin salida que midieron
    # los re-auditores: la divergencia se congelaba y después ya no había cómo corregirla.
    _bloqueo_monto_gasto_con_cxp(db, pricing)
    detail = _compute_detail(db, embarque, pricing)
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
    embarque = _embarque_or_404(db, embarque_id)
    pricing = _get_or_create_pricing(db, embarque, bloquear=True)
    pricing.estado = "calculado" if _f(pricing.tc_valor) > 0 else "borrador"
    db.commit()
    db.refresh(pricing)
    return _compute_detail(db, embarque, pricing)
