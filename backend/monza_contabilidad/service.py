"""Lógica pura del módulo Contabilidad MonzaParts: cálculo de IVA, saldo, estado de
pago, semáforo de vencimiento y serializadores. Sin sesión de BD → testeable en
aislamiento (mismas reglas que el módulo de Grupo AM)."""
import logging
from datetime import datetime, date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List
from zoneinfo import ZoneInfo

logger = logging.getLogger("monza_contabilidad")

# ── Constantes (alineadas con el módulo de Grupo AM) ───────────────────────────
IVA_DEFAULT = 0.19    # IVA Chile si la cotización/config no traen iva_pct
TOL = 0.5             # tolerancia CLP para clasificar saldos (pagada / al_día)
TOL_QTY = 0.001       # tolerancia para comparaciones de cantidades
TOL_PAGO = 1.0        # holgura de 1 CLP en topes de pago/adelanto (redondeo)
DIAS_POR_VENCER = 7   # días para marcar 'por_vencer' en el semáforo
MEDIO_FACT_ADELANTO = "factoring_adelanto"
MEDIO_FACT_RETENCION = "factoring_retencion"
MEDIO_ADELANTO = "adelanto"  # cobranza generada al aplicar un adelanto verificado a la factura

# ── Estados del ADELANTO (espejo de ESTADOS_ADELANTO de routers/contabilidad.py) ──
# En Monza la EXISTENCIA del registro es la aprobación (lo crea Contabilidad al
# verificar o Tesorería al aprobar), así que el estado vivo normal es 'aprobado' y por
# eso es el default de la columna. 'informado' existe para que el vocabulario sea el
# MISMO que en Grupo AM (allá Comercial crea la fila antes de que llegue la plata) y
# para no cerrar la puerta si algún día Monza informa el adelanto sin verificarlo.
# 'anulado' es el que hacía falta: hasta ahora un adelanto verificado por error no se
# podía revertir de ninguna forma (monza_router_cotizaciones.py prometía «Revierta el
# adelanto en Contabilidad/Tesorería primero» sin que existiera cómo).
ADEL_INFORMADO = "informado"
ADEL_APROBADO = "aprobado"
ADEL_ANULADO = "anulado"
ESTADOS_ADELANTO = (ADEL_INFORMADO, ADEL_APROBADO, ADEL_ANULADO)


def _f(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


# ─── Dinero / fecha — criterio ÚNICO compartido con GA (routers/contabilidad.py) ──
# Que la factura 33 y la guía 52 del mismo despacho CUADREN exige redondear IGUAL:
# precio unitario a 2 decimales ANTES de multiplicar, y half-up (no el round()
# banker's de Python, que redondea 28.5 → 28) a peso en la línea y en el IVA.
def _precio2(precio) -> float:
    """Precio unitario neto redondeado a 2 decimales (base común guía/factura)."""
    return round(_f(precio), 2)


def _total_linea(precio, cantidad) -> float:
    """Total neto de una línea: precio(2 dec) × cantidad, half-up a peso (== guía/SII)."""
    p2 = _precio2(precio)
    return float(Decimal(str(p2 * cantidad)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _iva_clp(neto, iva_rate) -> float:
    """IVA a peso con half-up (== SII). A diferencia de GA la tasa es parámetro:
    Monza la resuelve por venta con iva_rate_de (fracción, ej 0.19)."""
    return float(Decimal(str(_f(neto) * _f(iva_rate))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _hoy_chile() -> date:
    """Fecha de hoy en Chile (America/Santiago) — la fecha tributaria del documento.
    El server en producción corre en UTC: pasadas las ~20-21h en Chile, date.today()
    ya sería 'mañana'. Espejo de routers/contabilidad.py._hoy_chile."""
    return datetime.now(ZoneInfo("America/Santiago")).date()


def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    if isinstance(s, date):
        return s
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(s)[:10], fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def _es_medio_factoring(medio: Optional[str]) -> bool:
    return bool(medio and medio.startswith("factoring"))


# ── Estado del adelanto: lectura y transición (puras, sin sesión) ──────────────
def estado_de_adelanto(adelanto) -> str:
    """Estado del adelanto tolerando la fila LEGADA sin la columna (o con NULL): las
    tablas monza_cont_adelanto creadas antes de esta columna representaban un adelanto
    ya verificado, así que la ausencia de dato significa 'aprobado' — nunca 'anulado'
    (leerlo al revés apagaría el candado de Abastecimiento de todas las ventas viejas).
    El init_db normaliza esas filas; esto es cinturón y tirantes.
    Sin adelanto devuelve 'anulado' — el estado que NO cuenta como plata comprometida."""
    if adelanto is None:
        return ADEL_ANULADO
    return getattr(adelanto, "estado", None) or ADEL_APROBADO


def adelanto_activo(adelanto) -> bool:
    """¿El adelanto CUENTA como plata comprometida del cliente? (todo lo que no está
    anulado). Es el predicado único: quien suma, aplica o muestra un adelanto lo usa,
    en vez de repetir la comparación de strings."""
    return adelanto is not None and estado_de_adelanto(adelanto) != ADEL_ANULADO


def reactivar_adelanto(adelanto) -> bool:
    """Devuelve a 'aprobado' un adelanto ANULADO que se vuelve a registrar (el cliente sí
    depositó, o la anulación fue el error). Devuelve True si hubo transición.

    Existe porque en Monza el adelanto es UNO por venta (uq_monza_cont_adelanto_cotizacion):
    el registro anulado NO se puede reemplazar por otro, se REUSA. Todo camino que
    re-registre la plata del adelanto —verificar en Contabilidad y aprobar en Tesorería,
    que son gemelos y deben mantenerse en sync— tiene que llamar a esto ANTES de escribir
    el monto; si no, la fila queda 'anulada' con plata dentro y los caminos que filtran
    anulados no la ven (adelanto invisible con adelanto_verificado=1)."""
    if adelanto is not None and estado_de_adelanto(adelanto) == ADEL_ANULADO:
        adelanto.estado = ADEL_APROBADO
        return True
    return False


# ── RUT chileno (Fase 3 espejo GA: routers/contabilidad.py) ────────────────────
def rut_saneado(rut: Optional[str]) -> str:
    return (rut or "").replace(".", "").replace(" ", "").strip().upper()


def rut_valido(rut: Optional[str]) -> bool:
    """Valida RUT chileno (cuerpo + dígito verificador, módulo 11). Acepta con o sin
    puntos/guión. Una FACTURA con RUT inválido la rechaza el SII: mejor frenarla aquí
    que descubrirlo al emitir (o peor, tras contabilizarla)."""
    r = rut_saneado(rut)
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


def iva_rate_de(cot, cfg) -> float:
    """Tasa de IVA (fracción, ej 0.19). MonzaConfig/MonzaCotizacion guardan iva_pct
    como porcentaje (ej 19); se normaliza a fracción. Cae a IVA_DEFAULT si no hay dato.

    AUDITORÍA (hallazgo MEDIUM «venta con IVA 0 % facturada con 19 %», punto 3 del fix):
    el resultado NO cambia — un 0 sigue cayendo a IVA_DEFAULT —, pero la traza deja de
    ser ambigua. Antes un solo warning genérico cubría dos casos MUY distintos:
      · SIN DATO (None en la venta y en la config) → falta configurar MonzaConfig.
      · DATO INVÁLIDO (0 o negativo) → la VENTA está mal: se cerró con iva_pct = 0 y su
        total_bruto no lleva IVA, mientras la factura sí lo llevará. Ese descuadre deja
        mercadería imposible de facturar (tope Σ brutos ≤ total de la venta), así que
        hay que poder identificar CUÁL venta reparar: por eso se loguea su id.
    Un 0 NO significa «exento»: la tubería DTE no sabe expresar exención (el documento
    real saldría igual con 19 %). Si el negocio necesita ventas exentas, es un trabajo
    aparte (MntExe en el payload de Wasabil), jamás una tasa 0. El camino de entrada
    (PUT /api/monza/config con iva_pct = 0) se cierra en monza_router_config.py."""
    raw_cot = getattr(cot, "iva_pct", None) if cot is not None else None
    raw = raw_cot
    # Cadena de resolución INTACTA (`not raw`): un iva_pct 0/None en la venta sigue
    # cayendo a MonzaConfig, tal como antes. Lo único que cambia es el diagnóstico.
    if not raw and cfg is not None:
        raw = getattr(cfg, "iva_pct", None)
    raw_num = _f(raw)
    if raw_num <= 0:
        if raw is None:
            logger.warning(
                "IVA SIN DATO en la venta ni en MonzaConfig (cotización %s); usando "
                "IVA_DEFAULT=%.0f%%. Revisa MonzaConfig.iva_pct.",
                getattr(cot, "id", None), IVA_DEFAULT * 100,
            )
        else:
            logger.warning(
                "IVA INVÁLIDO (%s) en la cotización %s (o en MonzaConfig): 0 o negativo "
                "NO es exento. Se factura con IVA_DEFAULT=%.0f%%, así que el total "
                "congelado de esa venta puede quedar descuadrado — repárala.",
                raw, getattr(cot, "id", None), IVA_DEFAULT * 100,
            )
        return IVA_DEFAULT
    return raw_num / 100.0 if raw_num > 1 else raw_num


def _semaforo(fecha_venc: Optional[date], saldo: float) -> str:
    """al_dia (saldada) | sin_fecha | vencida | por_vencer (<= DIAS_POR_VENCER) | vigente."""
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


def _estado_pago(factura, pagado: float, saldo: float) -> str:
    """El saldo manda: saldada → 'pagada'. Con saldo y factoring vigente → 'factorizada'."""
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


def _recompute_factura(factura, cobranzas=None) -> None:
    """Recalcula monto_pagado / saldo / estado_pago desde las cobranzas reales.

    `cobranzas`: lista ya leída BAJO LOCK — obligatoria en los endpoints que escriben
    plata. La relación perezosa `factura.cobranzas` es una lectura PLANA y, si alguna
    vez el engine dejara de correr en READ COMMITTED, serviría el snapshot abierto al
    inicio del request (anterior a cualquier lock) y persistiría totales derivados de
    datos viejos. Espejo de routers/contabilidad.py (auditoría 2026-07-21; el
    fundamento está en docs/regla-lecturas-de-plata.md)."""
    bruto = _f(factura.monto_bruto)
    pagado = sum(_f(c.monto) for c in (factura.cobranzas if cobranzas is None else cobranzas))
    saldo = round(max(bruto - pagado, 0.0), 2)   # nunca negativo persistido
    factura.monto_pagado = round(pagado, 2)
    factura.saldo = saldo
    factura.estado_pago = _estado_pago(factura, pagado, saldo)


def _serialize_factura(factura, guia_viva=None, adelanto_id=None) -> dict:
    """Serializa la factura. `guia_viva` es el N° de guía ACTUAL del despacho origen
    (lo precarga el router en LOTE; None = no se precargó o el despacho ya no existe).

    `adelanto_id` (opcional): id del adelanto de la VENTA, para marcar de qué adelanto
    viene la cobranza medio='adelanto'. En Monza la cobranza NO lleva la columna
    (models.py: el adelanto es UNO por venta y el vínculo se DERIVA), así que lo aporta
    el router — que ya conoce el adelanto de la venta— en vez de abrirse una query por
    cobranza. Espejo del campo `adelanto_id` que Grupo AM sí guarda en la columna.

    AUDITORÍA (hallazgo MEDIUM «factura manual contra una guía 52 cuyo folio aún viene
    en camino»): `factura.numero_guia` es un SNAPSHOT tomado al emitir la factura. Si en
    ese momento el despacho tenía el N° tecleado a mano y después el SII confirmó el
    folio real de la guía 52, el módulo DTE pisa `despacho.numero_guia` con el folio y la
    factura se quedaba mostrando para siempre una guía que ya no existe. Sirviendo el
    valor VIVO del despacho (con fallback al snapshot para despachos borrados) Facturas y
    Despachos vuelven a decir lo mismo, sin migración y sin tocar la escritura. Es además
    la paridad con Grupo AM, que resuelve lo mismo por relationship ORM
    (`factura.despacho.numero_guia if factura.despacho else None`)."""
    bruto = _f(factura.monto_bruto)
    pagado = _f(factura.monto_pagado)
    saldo = _f(factura.saldo)
    fac = factura.factoring
    return {
        "id": factura.id,
        "numero_factura": factura.numero_factura,
        "tipo_doc": factura.tipo_doc,
        # Factura de ANTICIPO (vía B): la UI la distingue con badge y sin guía. Va como
        # bool y no como 0/1 para que el front no tenga que interpretar el entero.
        "es_anticipo": bool(factura.es_anticipo),
        "cotizacion_id": factura.cotizacion_id,
        "numero_cotizacion": factura.numero_cotizacion,
        "cliente": factura.cliente_nombre or "",
        "rut_cliente": factura.rut_cliente or "",
        "despacho_id": factura.despacho_id,
        # N° de guía VIVO del despacho; el snapshot congelado solo como respaldo.
        "numero_guia": guia_viva if guia_viva is not None else factura.numero_guia,
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
                # Distinto de None ⇒ es la línea NEGATIVA que descuenta esa factura de
                # anticipo (la UI la marca con ↩ en vez de tratarla como un ítem más).
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
                # es_adelanto/adelanto_id (espejo de routers/contabilidad.py): la UI
                # infería «esto es la aplicación del adelanto» comparando el string del
                # medio. Publicarlo explícito la desacopla del vocabulario interno (y es
                # el campo que hará falta el día que una venta admita N adelantos).
                "es_adelanto": c.medio == MEDIO_ADELANTO,
                "adelanto_id": adelanto_id if c.medio == MEDIO_ADELANTO else None,
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


def mercaderia_pendiente_bruto(items, qty_fact: dict, iva_rate: float) -> float:
    """Bruto (c/IVA) de la mercadería FÍSICAMENTE sin facturar de una venta.

    REGLA DE ORO (Fase 4, espejo de _mercaderia_pendiente_bruto de GA): base FÍSICA —
    qty pendiente = cantidad − Σ facturada (clamp a 0 por correcciones manuales) ×
    precio CONGELADO del ítem. JAMÁS total_bruto − Σ facturas: el polvo de redondeo
    de IVA por tandas deja $1-3 fantasma; con base física una venta 100% facturada
    da 0 POR CONSTRUCCIÓN (hallazgo HIGH del enjambre G15 de Grupo AM).
    En Monza es MÁS simple que en GA: los precios ya están congelados por ítem
    (precio_unitario_clp) — no hay pricing vivo ni prorrateo."""
    pend_neto = 0.0
    for it in items:
        cant = _f(it.cantidad)
        if cant <= 0:
            continue
        qty_pend = max(cant - _f(qty_fact.get(it.id, 0.0)), 0.0)
        if qty_pend <= 0:
            continue
        if it.precio_unitario_clp is None:
            # No ocultar $0 silenciosos: la línea pesa 0 en el pendiente pero queda traza.
            logger.warning(
                "mercaderia_pendiente: ítem %s de la venta %s sin precio_unitario_clp — "
                "aporta $0 al pendiente por facturar.", it.id, it.cotizacion_id,
            )
            continue
        pend_neto += qty_pend * _precio2(it.precio_unitario_clp)
    return pend_neto * (1.0 + _f(iva_rate))


def _resumen_cobranza(facturas: List, por_facturar_clp: Optional[float] = None) -> dict:
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
    # POR FACTURAR con base física (viene de mercaderia_pendiente_bruto): se agrega
    # solo si el caller lo calculó — mantiene el contrato viejo intacto para los
    # consumidores que no lo piden (espejo del patrón de GA _resumen_cobranza).
    if por_facturar_clp is not None:
        out["por_facturar_clp"] = round(max(_f(por_facturar_clp), 0), 0)
    return out


def estado_adelanto(cot, adelanto, *, facturas_venta=None) -> dict:
    """Estado del adelanto de una venta, para la UI y para Abastecimiento.

    Reglas (simples y debuggeables):
      - requiere_adelanto = cotización.pct_adelanto > 0 (lo informa Comercial al cerrar).
      - 'verificado'  = existe un registro MonzaContAdelanto (Contabilidad lo verificó).
      - 'por_verificar' = requiere pero aún sin registro.
      - 'no_aplica'   = no requiere adelanto.

    `facturas_venta` (opcional, keyword-only): las facturas de ESA venta ya cargadas por
    el llamador. Sirve para publicar `factura_anticipo_folio`, el trazo «respaldo Factura
    N° X» de la vía B. Es un dato DERIVADO —se filtran las `es_anticipo` y se leen sus
    folios— y no una columna nueva: Monza tiene UN adelanto por venta
    (uq_monza_cont_adelanto_cotizacion), así que el vínculo adelanto↔factura de anticipo
    no necesita estado propio que pueda quedar obsoleto (Grupo AM sí lo guarda porque
    tiene N adelantos por OC). Se recibe como parámetro para no abrir una query aquí:
    esta función es pura y la usan listados donde una query por fila sería un N+1.

    FORMATO ELEGIDO: string. Con varias facturas de anticipo (posible: nada impide dos
    anticipos parciales) los folios van separados por ", " en orden de emisión; None si
    no hay ninguna o si el llamador no pasó las facturas. Un string se pinta directo en
    la UI («respaldo Factura N° 1234, 1240») sin que el front tenga que decidir cómo
    juntar una lista, y es lo que ya consume la pantalla de Grupo AM.

    SE PUBLICA EN DOS LUGARES a propósito: en la RAÍZ y dentro de `adelanto`. El de
    adentro es el trazo del adelanto («este depósito está respaldado por la factura N° X»)
    y desaparece con él: `adelanto` es None mientras Tesorería no haya aprobado. Pero el
    caso NORMAL de la vía B es justamente ése —el anticipo se emite ANTES de que la plata
    llegue al banco—, así que el folio existía y la pantalla nunca lo mencionaba (y con
    `pct_adelanto = 0` tampoco). El de la RAÍZ es el hecho de la VENTA («esta venta tiene
    factura de anticipo N° X»), independiente del estado del adelanto, y es el que hace
    que el chip «Anticipo facturado N° X» se pinte siempre. Sigue siendo DERIVADO y sin
    query extra: el llamador ya trae las facturas de la venta cargadas."""
    # Un adelanto ANULADO no cuenta como verificado: la venta vuelve a 'por_verificar' y
    # el cortafuego de Abastecimiento (monza_router_abastecimiento.py, que solo frena con
    # adelanto_verificado == 0) se cierra de nuevo. Se neutraliza AQUÍ, en la función
    # pura, además de filtrarlo en las queries del router: los llamadores de OTROS
    # módulos traen el adelanto de sus propias consultas (monza_tesoreria) y esta es la
    # única puerta por la que pasan todos.
    if not adelanto_activo(adelanto):
        adelanto = None
    folios_anticipo = None
    if facturas_venta:
        folios = [str(f.numero_factura).strip() for f in facturas_venta
                  if getattr(f, "es_anticipo", 0) and (f.numero_factura or "").strip()]
        folios_anticipo = ", ".join(folios) or None
    pct = int(getattr(cot, "pct_adelanto", 0) or 0)
    verificado = adelanto is not None
    # Si ya existe un registro verificado, el adelanto sigue aplicando aunque luego cambien
    # el pct (defensa ante inconsistencias); el estado lo manda la existencia del registro.
    requiere = pct > 0 or verificado
    if verificado:
        estado = "verificado"
    elif requiere:
        estado = "por_verificar"
    else:
        estado = "no_aplica"
    return {
        "requiere_adelanto": requiere,
        "pct_adelanto": pct,
        "estado_adelanto": estado,
        # RAÍZ (ver el docstring): el hecho de la VENTA, vivo aunque `adelanto` sea None
        # —el caso normal de la vía B, con el anticipo emitido antes de que Tesorería
        # apruebe— y aunque pct_adelanto sea 0. None si el llamador no pasó las facturas.
        "factura_anticipo_folio": folios_anticipo,
        "adelanto": None if adelanto is None else {
            "id": adelanto.id,
            # Estado de la máquina informado|aprobado|anulado (aquí nunca 'anulado': un
            # anulado se sirve como `adelanto: null`). Lo pinta la pantalla y lo necesita
            # el botón «Anular» para no ofrecerse dos veces.
            "estado": estado_de_adelanto(adelanto),
            # DERIVADO de las facturas es_anticipo=1 de la venta (ver el docstring):
            # None cuando la vía B no se usó (o el llamador no pasó las facturas).
            "factura_anticipo_folio": folios_anticipo,
            "monto": _f(adelanto.monto),
            "monto_aplicado": _f(adelanto.monto_aplicado),
            "fecha_pago": adelanto.fecha_pago.isoformat() if adelanto.fecha_pago else None,
            "banco": adelanto.banco,
            "numero_operacion": adelanto.numero_operacion,
            "observaciones": adelanto.observaciones,
            "fecha_verificacion": adelanto.fecha_verificacion.isoformat() if adelanto.fecha_verificacion else None,
        },
    }


def _periodo_filter(fecha, periodo: Optional[str]) -> bool:
    if not periodo or not fecha:
        return True
    hoy = date.today()
    d = fecha.date() if hasattr(fecha, "date") else fecha
    if periodo == "semana":
        return (hoy - d).days <= 7
    if periodo == "mes":
        return d.year == hoy.year and d.month == hoy.month
    if periodo == "anio":
        return d.year == hoy.year
    return True


def periodo_floor(periodo: Optional[str]) -> Optional[date]:
    """Cota inferior (fecha) para un período, usable como pre-filtro en SQL antes del
    filtro fino _periodo_filter. None si no aplica."""
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
