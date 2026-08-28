"""Calculadora de precios MonzaParts.

Fórmulas (según documento):
  flete_clp      = peso_kg × tarifa_aerea_por_kg × tc_moneda_tarifa
  costo_clp      = costo × tc_moneda_item
  precio_neto    = (costo_clp + flete_clp) × (1 + markup_pct/100)   ← por unidad, sin IVA
  precio_bruto   = precio_neto × (1 + iva_pct/100)
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List

from database import get_db
from auth import get_current_user
from empresa_guard import require_empresa
from monza_models import MonzaLead, MonzaLeadItem, MonzaConfig

# CANDADO DE EMPRESA a nivel de ROUTER (2026-08-22): el CRM de MonzaParts estaba
# abierto a cualquier usuario autenticado —incluidos los de minería— mientras
# Despachos, Bodega y el PATCH de Cotizaciones ya lo tenían desde la auditoría F6.
# Router COMPLETO (lecturas incluidas): candar solo las escrituras deja la lectura
# de los datos del cliente como puerta del costado. Ver monza_router_leads.py.
router = APIRouter(
    prefix="/api/monza/cotizador",
    tags=["monza-cotizador"],
    dependencies=[Depends(require_empresa("automotriz"))],
)


# ── Schemas ───────────────────────────────────────────────────────────────────

class CalidadIn(BaseModel):
    calidad: str          # genuine/oem/aftermarket/sin_calificar
    marca: Optional[str] = None
    procedencia: Optional[str] = None
    costo: float = 0
    moneda: str = "EUR"   # EUR/USD/CLP
    peso_kg: float = 0
    markup_pct: float = 0  # porcentaje entero, ej 28

class ItemCalculo(BaseModel):
    item_id: int
    calidades: List[CalidadIn]

class CalcularBody(BaseModel):
    lead_id: int
    items: List[ItemCalculo]
    tarifa_tipo: str = "aerea"  # por ahora solo aérea
    # Flete de ESTA cotización: una sola moneda y tarifa para todos los ítems (decisión
    # del dueño). None = usar la configuración global, que es como se comportaba antes.
    moneda_tarifa: Optional[str] = None   # EUR | USD
    tarifa_aerea: Optional[float] = None  # por kg, en moneda_tarifa

class AplicarPrecioItem(BaseModel):
    item_id: int
    calidad: str
    numero_parte: Optional[str] = None
    marca: Optional[str] = None
    procedencia: Optional[str] = None
    precio_clp: float  # precio neto por unidad (sin IVA)
    plazo_entrega: Optional[str] = None
    # PARÁMETROS del cálculo. Antes no viajaban y por eso el precio era irreproducible:
    # al reabrir la calculadora no había con qué reconstruir la pantalla y se mostraba el
    # precio final como si fuera el costo, en CLP. Opcionales para no romper a un cliente
    # viejo que no los mande (ahí se guarda NULL y la calculadora cae al comportamiento
    # anterior en vez de fallar).
    costo: Optional[float] = None
    moneda: Optional[str] = None
    peso_kg: Optional[float] = None
    markup_pct: Optional[float] = None

class AplicarBody(BaseModel):
    lead_id: int
    items: List[AplicarPrecioItem]
    moneda_tarifa: Optional[str] = None
    tarifa_aerea: Optional[float] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_config(db: Session) -> MonzaConfig:
    cfg = db.query(MonzaConfig).filter(MonzaConfig.id == 1).first()
    if not cfg:
        cfg = MonzaConfig(id=1)
        db.add(cfg)
        db.commit()
        db.refresh(cfg)
    return cfg


def _tarifa_configurada(cfg: MonzaConfig, moneda: str) -> Optional[float]:
    """Tarifa por kilo que Configuración tiene cargada PARA ESA MONEDA, o None.

    El courier cobra distinto según la moneda del contrato, así que cada una tiene su
    propio precio por kilo (`tarifa_aerea_eur_por_kg` / `tarifa_aerea_usd_por_kg`).

    Respaldo del legado: mientras la tarifa nueva de esa moneda no esté cargada, se usa
    la tarifa vieja (`tarifa_aerea_por_kg`) SOLO si el legado estaba expresado en ESA
    MISMA moneda (`cfg.moneda_tarifa`). Nunca se presta la tarifa de una moneda a la
    otra: ese préstamo es justamente el error que este cambio corrige.

    Devuelve None cuando no hay dato — "no configurada" NO es lo mismo que 0 (0 sería
    afirmar que el flete es gratis, y un flete en 0 subvalúa la venta sin que nadie se
    entere). Quien llama decide qué hacer con el None; _flete_efectivo lo convierte en
    un error accionable."""
    por_moneda = {
        "EUR": cfg.tarifa_aerea_eur_por_kg,
        "USD": cfg.tarifa_aerea_usd_por_kg,
    }
    valor = por_moneda.get(moneda)
    if valor is not None:
        return float(valor)
    if (cfg.moneda_tarifa or "EUR").upper() == moneda and cfg.tarifa_aerea_por_kg is not None:
        return float(cfg.tarifa_aerea_por_kg)
    return None


def _flete_efectivo(cfg: MonzaConfig, moneda_tarifa: Optional[str] = None,
                    tarifa_aerea: Optional[float] = None) -> tuple:
    """(moneda, tarifa_por_kg) del flete aéreo — la de la cotización si se eligió.

    FUENTE ÚNICA para que el cálculo, el guardado y la vista no puedan divergir. Hasta esta
    entrega la moneda del flete salía SIEMPRE de `monza_config` (global, EUR por defecto) y no
    había forma de elegir dólares al cotizar. Ahora se elige por cotización y se guarda en el
    lead; `None` significa "usar la global", que es exactamente el comportamiento anterior —
    así los leads previos a este cambio siguen calculando igual.

    Desde 2026-08-08 la tarifa depende de la MONEDA elegida (ver _tarifa_configurada): el
    selector de la calculadora ya no cambia solo la etiqueta, cambia el precio por kilo.
    `tarifa_aerea` explícita sigue mandando sobre todo lo demás — es la foto congelada de
    un lead ya calculado, y recalcularlo con la tarifa de hoy movería un precio ya ofrecido.

    Falla CERRADO: si la moneda pedida no tiene tarifa cargada, corta con 400 en vez de
    cotizar con 0. Un flete de 0 no se nota en la pantalla y sale dentro del precio."""
    mon = (moneda_tarifa or cfg.moneda_tarifa or "EUR").upper()
    if tarifa_aerea is not None:
        return mon, float(tarifa_aerea)
    tarifa = _tarifa_configurada(cfg, mon)
    if tarifa is None:
        raise HTTPException(
            400,
            f"No hay tarifa aérea configurada en {mon}: cárgala en Configuración → "
            f"«Tarifa aérea por kg ({mon})» antes de cotizar con esa moneda. "
            "Sin ese dato el flete quedaría en 0 y la venta saldría subvaluada.",
        )
    return mon, tarifa


def _calcular_precio(costo: float, moneda: str, peso_kg: float, markup_pct: float,
                     cfg: MonzaConfig, moneda_tarifa: Optional[str] = None,
                     tarifa_aerea: Optional[float] = None) -> dict:
    """Retorna precio neto y bruto por unidad."""
    # TC para el costo
    if moneda == "EUR":
        tc_item = cfg.tc_eur_clp
    elif moneda == "USD":
        tc_item = cfg.tc_usd_clp
    else:
        tc_item = 1.0

    # TC para la tarifa aérea (moneda de ESTA cotización, no la global)
    mon_tarifa, tarifa_kg = _flete_efectivo(cfg, moneda_tarifa, tarifa_aerea)
    if mon_tarifa == "EUR":
        tc_tarifa = cfg.tc_eur_clp
    else:
        tc_tarifa = cfg.tc_usd_clp

    costo_clp = costo * tc_item
    flete_clp = peso_kg * tarifa_kg * tc_tarifa
    precio_neto = (costo_clp + flete_clp) * (1 + markup_pct / 100)
    precio_bruto = precio_neto * (1 + cfg.iva_pct / 100)

    return {
        "costo_clp": round(costo_clp),
        "flete_clp": round(flete_clp),
        "precio_neto": round(precio_neto),
        "precio_bruto": round(precio_bruto),
        "tc_aplicado": tc_item,
        # La foto del flete viaja en la respuesta: es lo que la pantalla muestra en el
        # desglose y lo que se persiste al aplicar.
        "moneda_tarifa": mon_tarifa,
        "tarifa_aerea": tarifa_kg,
        "tc_tarifa": tc_tarifa,
    }


def _fila_recalculada(item: MonzaLeadItem, ap: "AplicarPrecioItem") -> bool:
    """¿ESTA corrida recalculó el precio de la fila? Decide si su estampa de flete se mueve.

    handleAplicar manda TODAS las filas seleccionadas, incluidas preexistentes cuyo precio
    salió de una corrida ANTERIOR con otro flete. Estampar incondicionalmente escribiría
    una foto FALSA (flete de hoy sobre un precio de ayer) que la siembra del modal después
    "conservaría" — la clase de mentira que la tarifa congelada existe para impedir.

    Se decide 100% server-side comparando el body contra lo GUARDADO (jamás con un flag del
    cliente, misma disciplina que tc_aplicado): si precio, costo, moneda, peso o margen
    difieren, la fila se recalculó en esta corrida. Tolerancia relativa chica por el
    round-trip FLOAT de MySQL. `ap.costo is None` = pantalla vieja sin parámetros: no hay
    con qué comparar y la estampa NO se toca (tri-estado, igual que el guardado de abajo).
    """
    if ap.costo is None:
        return False

    def _difiere(a, b) -> bool:
        if a is None or b is None:
            return (a is None) != (b is None)
        a, b = float(a), float(b)
        return abs(a - b) > max(1e-6, 1e-6 * max(abs(a), abs(b)))

    return (
        _difiere(ap.precio_clp, item.precio_clp)
        or _difiere(ap.costo, item.costo)
        or (ap.moneda or "").upper() != (item.moneda or "").upper()
        or _difiere(ap.peso_kg, item.peso_kg)
        or _difiere(ap.markup_pct, item.markup_pct)
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────


# ── Log helper ────────────────────────────────────────────────────────────────
def _log(db, user_email, accion, entidad, entidad_id=None, entidad_ref=None, detalle=None):
    from monza_models import MonzaLog
    lg = MonzaLog(user_email=user_email, accion=accion, entidad=entidad,
                  entidad_id=entidad_id, entidad_ref=entidad_ref, detalle=detalle)
    db.add(lg)
    db.commit()

@router.post("/calcular")
def calcular_precios(body: CalcularBody, db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Devuelve precios calculados sin persistir nada."""
    cfg = _get_config(db)

    lead = db.query(MonzaLead).filter(MonzaLead.id == body.lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead no encontrado")

    resultados = []
    total_neto = 0

    for item_calc in body.items:
        db_item = db.query(MonzaLeadItem).filter(
            MonzaLeadItem.id == item_calc.item_id,
            MonzaLeadItem.lead_id == body.lead_id,
        ).first()
        if not db_item:
            continue

        calidades_resultado = []
        for cal in item_calc.calidades:
            precios = _calcular_precio(
                costo=cal.costo,
                moneda=cal.moneda,
                peso_kg=cal.peso_kg,
                markup_pct=cal.markup_pct,
                cfg=cfg,
                moneda_tarifa=body.moneda_tarifa,
                tarifa_aerea=body.tarifa_aerea,
            )
            calidades_resultado.append({
                "calidad": cal.calidad,
                "marca": cal.marca,
                "procedencia": cal.procedencia,
                "costo": cal.costo,
                "moneda": cal.moneda,
                "peso_kg": cal.peso_kg,
                "markup_pct": cal.markup_pct,
                **precios,
            })

        # El neto para el total usa el primer precio con costo > 0
        for cr in calidades_resultado:
            if cr["precio_neto"] > 0:
                total_neto += cr["precio_neto"] * db_item.cantidad
                break

        resultados.append({
            "item_id": db_item.id,
            "descripcion": db_item.descripcion,
            "numero_parte": db_item.numero_parte,
            "cantidad": db_item.cantidad,
            "calidades": calidades_resultado,
        })

    # El config que se devuelve es el EFECTIVO de esta cotización, no el global: la
    # pantalla pinta el desglose del flete con él, y si mostrara el global diría una
    # moneda distinta de la que se usó para calcular.
    mon_tarifa, tarifa_kg = _flete_efectivo(cfg, body.moneda_tarifa, body.tarifa_aerea)
    return {
        "config": {
            "tc_usd_clp": cfg.tc_usd_clp,
            "tc_eur_clp": cfg.tc_eur_clp,
            "tarifa_aerea_por_kg": tarifa_kg,
            "moneda_tarifa": mon_tarifa,
            "iva_pct": cfg.iva_pct,
        },
        "items": resultados,
        "total_neto_estimado": round(total_neto),
    }


@router.post("/aplicar")
def aplicar_precios(body: AplicarBody, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """Aplica precios calculados a los items del lead."""
    lead = db.query(MonzaLead).filter(MonzaLead.id == body.lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead no encontrado")

    from datetime import datetime
    from monza_models import MonzaLeadActividad

    cfg = _get_config(db)
    # Flete de la cotización: se guarda en el LEAD (uno para todos sus ítems). Si el
    # cliente no lo manda se conserva lo que ya había, y si nunca hubo nada queda NULL
    # = configuración global. Nunca se pisa con None: perder la elección del operador
    # porque una pantalla vieja no mandó el campo sería el mismo bug que se está cerrando.
    if body.moneda_tarifa:
        lead.moneda_tarifa = body.moneda_tarifa.upper()
    if body.tarifa_aerea is not None:
        lead.tarifa_aerea = body.tarifa_aerea
    mon_tarifa, tarifa_kg = _flete_efectivo(cfg, lead.moneda_tarifa, lead.tarifa_aerea)

    for ap in body.items:
        item = db.query(MonzaLeadItem).filter(
            MonzaLeadItem.id == ap.item_id,
            MonzaLeadItem.lead_id == body.lead_id,
        ).first()
        if item:
            # La decisión de la estampa se toma ANTES de mutar: compara el body contra lo
            # que la fila tenía al entrar a esta corrida (después ya son iguales siempre).
            recalculada = _fila_recalculada(item, ap)
            item.precio_clp = ap.precio_clp
            item.calidad = ap.calidad
            if ap.marca:
                item.marca = ap.marca
            if ap.procedencia:
                item.procedencia = ap.procedencia
            if ap.plazo_entrega:
                item.plazo_entrega = ap.plazo_entrega
            if ap.numero_parte:
                item.numero_parte = ap.numero_parte
            # ── LO QUE ANTES SE PERDÍA ────────────────────────────────────────────
            # El precio se guardaba y sus parámetros no, así que era irreproducible.
            # `is not None` en vez de un `if` a secas: un costo 0 o un markup 0 son
            # valores legítimos que el operador puede haber puesto a propósito, y con
            # un truthy check se descartarían en silencio.
            if ap.costo is not None:
                item.costo = ap.costo
            if ap.moneda:
                item.moneda = ap.moneda.upper()
            if ap.peso_kg is not None:
                item.peso_kg = ap.peso_kg
            if ap.markup_pct is not None:
                item.markup_pct = ap.markup_pct
            # TC con que se convirtió ESTE costo: se resuelve SERVER-side desde la
            # moneda del ítem, jamás se acepta del cliente (misma disciplina que el
            # congelado de MonzaCotizacionItem en monza_router_cotizaciones.py).
            # ── Foto de la corrida, CONDICIONAL (TC + flete juntos) ───────────────
            # Solo las filas que ESTA corrida recalculó reciben el TC de hoy y el
            # flete de ESTA corrida; una preexistente que viajó sin cambios conserva
            # su foto completa (precio ↔ costo ↔ TC ↔ flete de la MISMA corrida).
            # Antes el tc_aplicado se refrescaba incondicional: si el TC global
            # cambiaba entre corridas por subconjunto, la fila sin tocar quedaba con
            # un TC que jamás produjo su precio — la misma mentira que la estampa
            # condicional cierra para el flete (hallazgo del veedor backend).
            # Contrato completo en MonzaLeadItem (monza_models.py).
            if recalculada:
                _mon = (item.moneda or "").upper()
                item.tc_aplicado = (cfg.tc_eur_clp if _mon == "EUR"
                                    else cfg.tc_usd_clp if _mon == "USD" else 1.0)
                item.moneda_tarifa = mon_tarifa
                item.tarifa_aerea = tarifa_kg

    asesor_nombre = current_user.email.split("@")[0].title()
    db.add(MonzaLeadActividad(
        lead_id=body.lead_id,
        tipo="cotizacion",
        descripcion=f"Precios calculados y aplicados por {asesor_nombre} ({len(body.items)} ítem(s))",
        usuario=asesor_nombre,
        usuario_id=current_user.id,
    ))

    lead.fecha_actualizacion = datetime.utcnow()
    db.commit()
    _log(db, current_user.email, "UPDATE", "lead",
         body.lead_id, f"lead#{body.lead_id}",
         f"Calculadora: {len(body.items)} precio(s) aplicado(s) · flete {tarifa_kg} {mon_tarifa}/kg")
    return {
        "ok": True,
        "items_actualizados": len(body.items),
        # El flete EFECTIVO que quedó guardado: la pantalla lo reusa al reabrir sin
        # tener que volver a preguntar.
        "moneda_tarifa": mon_tarifa,
        "tarifa_aerea": tarifa_kg,
    }
