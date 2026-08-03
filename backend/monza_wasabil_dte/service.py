"""Lógica pura del módulo Wasabil DTE de MonzaParts (sin BD, sin red) — armado y
validación de la guía de despacho electrónica (SII tipo 52).

Espejo de wasabil_dte/service.py de Grupo AM (batalla-probado: folios reales
136/137) con las adaptaciones Monza:

- PRECIO CONGELADO: las líneas toman MonzaCotizacionItem.precio_unitario_clp
  (la foto al vender), NUNCA un recálculo vivo — mismo contrato que la factura
  Monza ya honra (si el precio se recalculara, guía y factura descuadrarían).
- IVA POR VENTA: la cuadratura recibe la tasa como PARÁMETRO (el router la
  resuelve con iva_rate_de(cotización, MonzaConfig)); en GA era constante 0.19.
- REFERENCIA 801 desde MonzaCotizacion.oc_cliente + oc_fecha — oc_fecha ya es
  columna Date (Fase 3), así que aquí NO existe parse_fecha_oc: la fecha se usa
  directa y el router bloquea si viene NULL.
- invoiceReference = SOLO el N° de despacho interno (DSP-AAAA-####), formato v2
  post-folio 136 (Wasabil imprime ese campo: con la OC adentro salía dos veces).
- name de línea = DESCRIPCIÓN limpia recortada a 25 (la parte va en `code`).
- Referencia 801 SIN reason (formato v3, folio 137: el tipo ya imprime la
  etiqueta "ORDEN DE COMPRA" y un reason la duplicaba en el papel).

Reglas heredadas intactas: la fecha del DTE es la de CHILE (hoy_chile, jamás
date.today() — el VPS en UTC emitiría con fecha de mañana pasadas las ~20-21h);
redondeo half-up con Decimal (round() nativo es banker's y descuadra $1 en los
.5); issue=False por defecto (emitir al SII es IRREVERSIBLE).

Nada de esto llama a Wasabil ni toca la BD: el router junta los datos y este
archivo los arma. Sin imports de client ni de router (pureza testeable).
"""
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

# La fecha del DTE es la de CHILE, no la del server (en producción el VPS puede
# estar en UTC: pasadas las ~20-21h en Chile, date.today() ya sería "mañana").
TZ_CHILE = ZoneInfo("America/Santiago")

TIPO_DOC_GUIA = 52
TIPO_TRASLADO_VENTA = 1          # dispatchTypeCode 1 = "Operación constituye venta"
# Tipos de traslado del SII para la guía de despacho (dispatchTypeCode). El operador
# elige uno en el modal de emisión; 1 (venta) es el caso típico de MonzaParts, pero un
# traslado hacia una bodega propia es 5 (interno) y no constituye venta ante el SII.
TIPOS_TRASLADO = {
    1: "Operación constituye venta",
    2: "Ventas por efectuar",
    3: "Consignaciones",
    4: "Entrega gratuita",
    5: "Traslados internos",
    6: "Otros traslados no venta",
    7: "Guía de devolución",
    8: "Traslado para exportación",
    9: "Venta para exportación",
}
TIPO_TRASLADO_DEFAULT = TIPO_TRASLADO_VENTA
TIPO_DOC_FACTURA = 33            # Fase 6: factura electrónica (DTE 33)
TIPO_REF_OC = "801"              # referencia SII: Orden de Compra del cliente
TIPO_REF_GUIA = "52"             # referencia SII: guía de despacho que ampara la factura
TIPO_REF_ANTICIPO = "33"         # referencia SII: factura (aquí, la de anticipo descontada)
MAX_REFERENCIAS = 5              # tope de referencias por documento en Wasabil
REASON_MAX = 90                  # límite del campo reason (RazonRef) de una referencia
NOMBRE_MAX = 25                  # límite del SII para el nombre de línea
DESCRIPCION_MAX = 255            # descripción larga de línea
CONTACTO_MAX = 80                # receiverContact del DTE
INVOICE_REF_MAX = 200
FOLIO_REF_MAX = 18               # límite del SII para el folio de una referencia (N° OC)
MAX_LINEAS = 60                  # tope de líneas por documento en Wasabil
TOL_QTY = 0.001                  # tolerancia de cantidades (unidades)
NETO_MINIMO_DTE = 1.0            # bajo $1 el neto se emite como $0 y el SII lo rechaza


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def hoy_chile() -> date:
    """Fecha actual en Chile (America/Santiago) — la fecha tributaria del DTE."""
    return datetime.now(TZ_CHILE).date()


def acortar_nombre(numero_parte: Optional[str], descripcion: Optional[str]) -> str:
    """Nombre de línea = la DESCRIPCIÓN limpia, recortada a 25 chars (límite SII).

    El N° de parte NO se antepone: ya viaja en el campo `code` y se imprime como
    código en la guía — concatenarlo duplicaba el dato y cortaba la descripción a
    media palabra (hallazgo de la primera emisión real de GA, folio 136: el nombre
    salía "ROD-INF-PV351 RODILLO INF"). Sin descripción, el N° de parte es el nombre."""
    parte = (numero_parte or "").strip()
    desc = (descripcion or "").strip()
    if desc:
        return desc[:NOMBRE_MAX].rstrip()
    if parte:
        return parte[:NOMBRE_MAX].rstrip()
    return "ITEM"


def cuadratura(neto: float, iva_rate: float) -> Tuple[int, int, int]:
    """(neto, iva, total) en pesos enteros con redondeo half-up (== SII/F-1).

    Adaptación Monza: la tasa de IVA es PARÁMETRO (fracción, ej 0.19) — cada
    venta Monza congela su iva_pct y el router la resuelve con
    iva_rate_de(cotización, MonzaConfig). En GA era la constante IVA_RATE=0.19;
    aquí una constante fija descuadraría la guía contra la factura de una venta
    con tasa congelada distinta."""
    neto_d = Decimal(str(neto)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    iva_d = (neto_d * Decimal(str(iva_rate))).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(neto_d), int(iva_d), int(neto_d + iva_d)


def armar_lineas(despacho_items: list, items_por_id: dict) -> Tuple[List[dict], List[str]]:
    """Convierte los ítems del despacho Monza en líneas del DTE.

    `despacho_items`: MonzaDespachoItem del despacho.
    `items_por_id`: {MonzaCotizacionItem.id: MonzaCotizacionItem} — JOIN manual
    por item_id (en Monza NO hay relación ORM despacho_item→cotizacion_item;
    mismo patrón que monza_router_despachos). El precio de cada línea es el
    CONGELADO precio_unitario_clp de la cotización (la foto al vender) — jamás
    un recálculo vivo: es el mismo precio que usará la factura (guía y factura
    cuadran por construcción).

    Devuelve (lineas, problemas). Cada problema es bloqueante para emitir.
    """
    lineas: List[dict] = []
    problemas: List[str] = []
    for di in despacho_items:
        it = items_por_id.get(di.item_id)
        if not it:
            problemas.append(f"El ítem del despacho #{di.id} ya no existe en la cotización")
            continue
        qty = _f(di.qty_despachada)
        if qty <= TOL_QTY:
            continue  # línea vacía: no viaja nada de este ítem
        # Redondear ANTES del guard: el guard debe evaluar el MISMO valor que se
        # persiste en la línea (un sub-centavo pasaría el guard y viajaría como $0)
        precio = round(_f(it.precio_unitario_clp), 2)
        if precio <= 0:
            problemas.append(
                f"{it.numero_parte or 'ítem ' + str(it.id)}: sin precio de venta en la "
                "cotización (la guía es 'operación constituye venta' y exige precio neto)"
            )
            continue
        lineas.append({
            "name": acortar_nombre(it.numero_parte, it.descripcion),
            "description": (it.descripcion or "").strip()[:DESCRIPCION_MAX] or None,
            "code": (it.numero_parte or "").strip() or None,
            # externalId = monza_despacho_item_id: identidad ÚNICA de la línea (permite
            # que la factura tome el precio congelado de ESTA guía con match 1:1, sin
            # depender de que el n° de parte sea único). Wasabil lo acepta como ref del ERP.
            "externalId": str(di.id),
            "quantity": qty,
            "price": precio,   # precio unitario NETO en CLP congelado (ya redondeado a 2)
        })
    if not lineas and not problemas:
        problemas.append("El despacho no tiene cantidades a despachar")
    if len(lineas) > MAX_LINEAS:
        problemas.append(f"El despacho tiene {len(lineas)} líneas y Wasabil acepta máximo "
                         f"{MAX_LINEAS} por documento (divide el despacho)")
    return lineas, problemas


def armar_guia(*, numero_oc: str, fecha_oc: date, numero_despacho: str,
               lineas: List[dict], client_id: Optional[int] = None,
               contacto: Optional[str] = None, receiver_email: Optional[str] = None,
               fecha_emision: Optional[date] = None, issue: bool = False,
               tipo_traslado: int = TIPO_TRASLADO_DEFAULT) -> dict:
    """Arma el documento guía 52 en el vocabulario del API (verificado contra el
    esquema oficial de Wasabil el 2026-07-15, emisiones reales GA 136/137). NO lo envía.

    `issue=False` por defecto: emitir al SII es IRREVERSIBLE y este módulo solo
    manda issue=True después de la confirmación explícita del usuario en la
    previsualización (protocolo de seguridad del README).

    `fecha_oc` viene DIRECTA de MonzaCotizacion.oc_fecha (columna Date, Fase 3):
    aquí no hay parseo tolerante — el router bloquea antes si viene NULL, y esta
    función lo re-exige como cinturón (un DTE 52 sin fecha en la referencia 801
    es inválido ante el SII).

    `tipo_traslado` es el dispatchTypeCode del SII (ver TIPOS_TRASLADO): 1 venta
    por defecto, 5 traslado interno hacia bodega propia, etc.
    """
    if tipo_traslado not in TIPOS_TRASLADO:
        raise ValueError(f"Tipo de traslado inválido: {tipo_traslado}")
    if not fecha_oc:
        raise ValueError("La referencia 801 exige la fecha de la OC (oc_fecha es NULL)")
    folio_oc = str(numero_oc).strip()
    if len(folio_oc) > FOLIO_REF_MAX:
        # Cinturón espejo de la validación del preview: el SII rechaza folios de
        # referencia de más de 18 chars — mejor reventar acá que emitir chueco.
        raise ValueError(
            f"El N° de OC ('{folio_oc}') tiene {len(folio_oc)} caracteres; "
            f"el SII permite máximo {FOLIO_REF_MAX} en la referencia 801")
    doc = {
        "siiDocumentTypeCode": TIPO_DOC_GUIA,
        "issue": issue,
        "documentDate": (fecha_emision or hoy_chile()).isoformat(),
        "dispatchGuide": {"dispatchTypeCode": tipo_traslado},
        "details": lineas,
        # Referencia estructurada a la OC del cliente (obligatoria: en Monza el
        # N° y la fecha de OC son datos maestros de la venta desde la Fase 3).
        # SIN `reason`: Wasabil imprime la etiqueta del tipo ("ORDEN DE COMPRA",
        # que deduce del código 801) junto al folio, así que un reason del estilo
        # "Orden de compra 1788" hacía salir la etiqueta Y el número DOS VECES en
        # el papel — hallazgo de la guía GA folio 137. El campo es opcional para
        # el SII (RazonRef) y payload_a_rest lo omite si viene vacío.
        "references": [{
            "documentType": TIPO_REF_OC,
            "folio": folio_oc,
            "date": fecha_oc.isoformat(),
        }],
        # Referencia libre = SOLO el N° de despacho interno (DSP-AAAA-####): ancla
        # anti doble emisión (única por despacho, permite reencontrar el documento
        # ante un reintento con match EXACTO). La OC NO va aquí: Wasabil imprime
        # este campo en la guía y con "OC ... · DSP-..." la orden de compra salía
        # referenciada DOS veces (hallazgo GA folio 136) — la referencia legal a
        # la OC es la 801 de arriba, una sola vez. Monza nace en formato v2/v3.
        "invoiceReference": str(numero_despacho)[:INVOICE_REF_MAX],
    }
    if client_id:
        doc["clientId"] = client_id  # Wasabil autocompleta RUT/razón/giro/dirección/comuna
    if contacto and contacto.strip():
        doc["receiverContact"] = contacto.strip()[:CONTACTO_MAX]
    if receiver_email and receiver_email.strip():
        doc["receiverEmail"] = receiver_email.strip()
        doc["sendEmail"] = True
    return doc


def payload_a_rest(doc: dict) -> dict:
    """Traduce el documento (camelCase, vocabulario del esquema) al snake_case del
    API REST (`POST /api/documents`), según el ejemplo oficial documentado.

    ÚNICO punto de traducción — confirmado contra el API real el 2026-07-17
    (dispatch_guide/references aceptados al crear un borrador); si una emisión
    real revelara otro nombre, se corrige SOLO aquí. Ver nota en client.py.
    """
    mapa = {
        "siiDocumentTypeCode": "sii_document_type_code",
        "documentDate": "document_date",
        "dispatchGuide": "dispatch_guide",
        "invoiceReference": "invoice_reference",
        "clientId": "client_id",
        "receiverContact": "receiver_contact",
        "receiverEmail": "receiver_email",
        "sendEmail": "send_email",
        "issue": "issue",
        "details": "details",
        "references": "references",
        "paymentMethod": "payment_method",
    }
    rest: dict = {}
    for k, v in doc.items():
        rest[mapa.get(k, k)] = v
    if "dispatch_guide" in rest:
        dg = rest["dispatch_guide"]
        rest["dispatch_guide"] = {"dispatch_type_code": dg["dispatchTypeCode"]}
    # Referencias también a snake_case (convención consistente en TODO el payload;
    # los details ya usan name/description/code/quantity/price, iguales en ambos).
    if "references" in rest:
        rest["references"] = [
            {"document_type": r["documentType"], "folio": r["folio"],
             **({"date": r["date"]} if r.get("date") else {}),
             **({"reason": r["reason"]} if r.get("reason") else {})}
            for r in rest["references"]
        ]
    return rest


def _peso(valor) -> float:
    """Redondeo a PESO con half-up — el criterio del SII, el de _total_linea de
    monza_contabilidad y el del frontend (Math.round). El round() nativo de Python es
    banker's (28.5 → 28) y descuadra 1 peso en los .5."""
    return float(Decimal(str(valor)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def neto_linea_dte(ln: dict) -> float:
    """Neto que el SII liquidará por UNA línea: (precio × cantidad) menos su `discount`
    porcentual, half-up a PESO.

    El peso —y no el centavo— es el dominio correcto por DOS razones que apuntan al
    mismo lado: los montos de un DTE en CLP son ENTEROS (MontoItem por línea), y es
    EXACTAMENTE el dominio en que Contabilidad calculó el neto local (_total_linea,
    monza_contabilidad/service.py). Medir en centavos hacía que el medio peso de una
    línea terminada en .5 se perdiera y el documento tributario saliera descuadrado
    contra el libro de ventas."""
    bruto = _f(ln.get("price")) * _f(ln.get("quantity"))
    return _peso(bruto * (1 - _f(ln.get("discount")) / 100.0))


def total_neto_lineas(lineas: List[dict]) -> float:
    """Neto del documento = Σ neto_linea_dte (half-up a peso POR LÍNEA) — mismo criterio
    que _total_linea de monza_contabilidad y que lo que el usuario ve en pantalla.

    Desde la Fase 7 DESCUENTA el `discount` de cada línea: ignorarlo reportaba un neto
    que no era el que iba a liquidar el SII. Para la guía 52 y para cualquier factura
    sin descuento de anticipo (ninguna línea trae `discount`) el resultado es idéntico
    al de siempre — el payload de esas facturas no cambia ni un byte."""
    return float(sum(neto_linea_dte(ln) for ln in lineas))


def claim_vigente(dte, ahora: Optional[datetime] = None) -> bool:
    """True si el claim "emisión en vuelo" sigue fresco (bloquea emitir/reintentar).

    Los timestamps del claim son UTC naive (datetime.utcnow(), como bodega.py):
    inmunes a cambios de hora local (el cambio de hora chileno movería un reloj
    local ±1h y podría "vencer" un claim recién puesto)."""
    from .models import CLAIM_TTL_SEGUNDOS  # import local para mantener el módulo puro
    if not dte or not dte.en_vuelo_desde:
        return False
    return (ahora or datetime.utcnow()) - dte.en_vuelo_desde < timedelta(seconds=CLAIM_TTL_SEGUNDOS)


def serialize_dte(dte) -> dict:
    """DTE → dict para el frontend (estado legible, folio, links, ruta de recuperación).

    Estados que ve el usuario:
      emitido | procesando | pendiente (borrador en Wasabil) | fallido (SII rechazó)
      | enviando (claim en vuelo, sin respuesta aún) | error_envio (no llegó a
      Wasabil o la respuesta se perdió y el claim ya expiró → reintentable).
    `puede_reintentar` es la ÚNICA fuente de verdad del botón Reintentar.
    """
    from .models import STATUS_LABEL, STATUS_FALLIDO, STATUS_EMITIDO  # local (módulo puro)
    en_vuelo = claim_vigente(dte)
    if dte.status_id == STATUS_EMITIDO:
        # El STATUS manda sobre la ausencia de uuid. Reproducido: un DTE con
        # status_id=3 y folio "9500" pero uuid NULL (la respuesta trajo el estado y el
        # folio, y el uuid se perdió) se pintaba "no enviado" CON BOTÓN DE REINTENTAR
        # al lado, sobre un documento que ya existe ante el SII. El reintento lo ataja
        # con un 409, pero invitar a re-emitir algo irreversible no es aceptable: si el
        # API lo dio por emitido, el operador tiene que leer "emitido".
        estado = STATUS_LABEL.get(STATUS_EMITIDO, "emitido")
    elif dte.uuid:
        estado = STATUS_LABEL.get(dte.status_id, "sin_respuesta")
    elif en_vuelo:
        estado = "enviando"
    elif dte.error:
        estado = "error_envio"
    else:
        estado = "no_enviado"
    # Sin uuid = el documento NUNCA llegó a Wasabil → reintentable en cuanto el claim
    # deja de estar vigente, HAYA o NO error registrado. Exigir `bool(dte.error)`
    # dejaba clavado el caso "el server se cayó (o se hizo deploy) entre el claim
    # y la respuesta": nadie alcanzó a escribir el error, el claim expiraba solo y
    # la UI quedaba en "SII en proceso" para siempre, sin botón de reintento
    # (fix heredado de GA — se nace con él, no se re-aprende).
    # ...salvo que el status diga EMITIDO: entonces el documento existe y no se
    # reintenta nada (mismo motivo que el estado de arriba; el 409 del endpoint deja de
    # ser la única defensa y el botón ni siquiera se ofrece).
    puede_reintentar = (not en_vuelo) and dte.status_id != STATUS_EMITIDO and (
        dte.status_id == STATUS_FALLIDO or dte.uuid is None
    )
    return {
        "id": dte.id,
        "tipo_dte": dte.tipo_dte,
        "despacho_id": dte.despacho_id,
        # factura_id (Fase 6): el modal de facturas sondea POR FACTURA, así que sin
        # este campo la respuesta de "emitir" no le dice al frontend a quién consultar
        # y el modal se queda colgado en "pendiente". En GA falta y los 4 endpoints lo
        # inyectan a mano; acá se expone en el serializador (los endpoints igual lo
        # vuelven a poner como cinturón: mismo valor, sin riesgo de divergencia).
        "factura_id": getattr(dte, "factura_id", None),
        "uuid": dte.uuid,
        "status_id": dte.status_id,
        "estado": estado,
        "en_vuelo": en_vuelo,
        "puede_reintentar": puede_reintentar,
        "folio": dte.folio,
        "pdf_url": dte.pdf_url,
        "xml_url": dte.xml_url,
        "error": dte.error,
        "monto_neto": _f(dte.monto_neto),
        "iva": _f(dte.iva),
        "monto_total": _f(dte.monto_total),
        "created_at": dte.created_at.isoformat() if dte.created_at else None,
        "updated_at": dte.updated_at.isoformat() if dte.updated_at else None,
    }


# ─── FASE B: factura electrónica (DTE 33) ─────────────────────────────────────
# Mismo estilo que la guía 52: funciones PURAS que arman el documento en el
# vocabulario del esquema (camelCase) — payload_a_rest lo traduce al REST real.
#
# La factura se emite DESDE la factura LOCAL ya persistida (líneas congeladas en
# monza_cont_factura_cliente_item): lo que se emite es EXACTAMENTE lo registrado,
# y el reintento re-arma lo mismo.
#
# Desde la FASE 7 existen las facturas de ANTICIPO (vía B): el cliente paga un
# adelanto antes de que llegue la mercadería y se le emite un DTE 33 SIN guía por
# ese monto. Al facturar el despacho real, la factura lleva una línea LOCAL
# negativa (MonzaContFacturaClienteItem.anticipo_factura_id) que descuenta ese
# anticipo para no cobrarlo dos veces. Consecuencias para este archivo:
#   · Esa línea negativa NO viaja como línea del DTE: el API real RECHAZA price<0
#     y quantity<0, y no tiene descuento a nivel de documento. Viaja como
#     `discount` PORCENTUAL por línea (aplicar_descuento_lineas), con la precisión
#     COMPLETA del float — Wasabil calcula con la precisión enviada, así que
#     redondear el % rompería la cuadratura neto DTE == neto local.
#   · La factura del despacho referencia cada anticipo descontado con una
#     referencia tipo 33 (su folio SII). La factura de ANTICIPO en sí lleva solo
#     la 801 de la venta: nunca una 52 (no ampara traslado) ni una 33.
# Otra diferencia contra la Fase B de Grupo AM (por el modelo de datos de Monza):
#   · La fecha de la OC llega como `date` directo desde MonzaCotizacion.oc_fecha
#     (columna Date desde la Fase 3): aquí no hay parseo tolerante.

def armar_referencias_factura(*, numero_oc: str, fecha_oc: Optional[date],
                              guia_folio: Optional[str] = None,
                              guia_fecha: Optional[date] = None,
                              anticipos: Optional[List[dict]] = None,
                              fecha_documento: Optional[date] = None,
                              ) -> Tuple[List[dict], List[str]]:
    """Referencias del DTE 33 de MonzaParts:

      · SIEMPRE la OC del cliente (801, con N° y fecha) — igual que la guía 52.
      · Si la factura viene de un despacho: referencia 52 con el FOLIO de la guía
        (el del SII cuando la guía es electrónica; el N° manual si no lo es). Quien
        llama resuelve ese folio EN VIVO desde la fila monza_wasabil_dte del
        despacho — jamás desde el snapshot `factura.numero_guia`, que puede tener
        el N° tecleado viejo mientras el folio real del SII todavía no llega.
      · 'Retiro en oficina' (sin_guia) no lleva 52: la factura misma es el
        documento que ampara el traslado. La factura de ANTICIPO tampoco: no hay
        mercadería trasladada que amparar.
      · Fase 7: por cada factura de ANTICIPO descontada, una referencia tipo 33 con
        su folio del SII. `anticipos`: [{"folio": str, "fecha": date|None}].

    Devuelve (referencias, problemas); cualquier problema es BLOQUEANTE (un DTE con
    una referencia inválida lo rechaza el SII, y emitirlo es irreversible).
    """
    problemas: List[str] = []
    refs: List[dict] = []

    numero_oc = (numero_oc or "").strip()
    if not numero_oc:
        problemas.append("La venta no tiene N° de OC del cliente: la factura debe "
                         "referenciarla (tipo 801) — complétalo en el Cierre de Venta")
    elif len(numero_oc) > FOLIO_REF_MAX:
        problemas.append(
            f"El N° de OC del cliente ('{numero_oc}') tiene {len(numero_oc)} caracteres; "
            f"el SII permite máximo {FOLIO_REF_MAX} en la referencia. Acorta el N° de OC en la venta.")
    if not fecha_oc:
        problemas.append("La venta no tiene FECHA de OC del cliente: la referencia 801 "
                         "lleva la fecha real de la OC — complétala en el Cierre de Venta")
    if not problemas:
        # SIN `reason`: ver la nota en armar_guia — Wasabil ya imprime la etiqueta
        # "ORDEN DE COMPRA" (la deduce del código 801) junto al folio, y repetirla en
        # el motivo la duplicaba en el papel (formato v3, hallazgo del folio 137 de GA).
        refs.append({
            "documentType": TIPO_REF_OC, "folio": numero_oc,
            "date": fecha_oc.isoformat(),
        })

    if guia_folio and str(guia_folio).strip():
        folio_g = str(guia_folio).strip()
        if len(folio_g) > FOLIO_REF_MAX:
            problemas.append(
                f"El folio de la guía ('{folio_g}') excede {FOLIO_REF_MAX} caracteres "
                "para la referencia 52 (¿guía manual con N° largo?)")
        elif not (folio_g.isascii() and folio_g.isdigit()) or int(folio_g) <= 0:
            # FolioRef NUMÉRICO, mismo criterio que la referencia 33 del anticipo de más
            # abajo. La 52 apunta a un documento tributario y el folio de un documento
            # tributario es un correlativo del SII — también en la guía en PAPEL, cuyo
            # folio autoriza el SII. Cuando la guía es electrónica el folio lo pone el SII
            # y esto nunca se activa; el que entra crudo es el N° MANUAL, que TECLEA el
            # operador: se reprodujo un 'G-TECLEADO-A-MANO' viajando como FolioRef. El SII
            # rechaza el documento y el rechazo llega con el folio propio YA CONSUMIDO, así
            # que hay que bloquear ANTES (regla rectora: mejor un 409 que una nota de
            # crédito). `isascii` además de `isdigit`: '٣'.isdigit() es True y no es folio.
            problemas.append(
                f"El N° de guía de este despacho ('{folio_g}') no es un número correlativo "
                "del SII: la referencia 52 apunta a un documento tributario y el SII exige "
                "su folio numérico. Corrige el N° de guía en Despachos (o emite la guía "
                "electrónica, que trae el folio real) antes de facturar")
        elif guia_fecha and fecha_documento and guia_fecha > fecha_documento:
            # `fecha_documento` = fecha de emisión de ESTA factura. Una referencia no puede
            # estar fechada DESPUÉS del documento que la cita: `fecha_emision` es un campo
            # libre sin tope, así que backdatando la factura se llegaba a un DTE 33 REAL
            # que dice haberse emitido ANTES que la guía que ampara. Ninguna de las dos
            # fechas es inválida sola — lo inválido es el orden, y nadie las cruzaba.
            problemas.append(
                f"La guía de despacho está fechada el {guia_fecha.isoformat()}, DESPUÉS de "
                f"la fecha de emisión de esta factura ({fecha_documento.isoformat()}): una "
                "factura no puede referenciar una guía que todavía no existía. Corrige la "
                "fecha de emisión de la factura o la fecha de la guía en Despachos.")
        else:
            # SIN `reason`: el tipo 52 ya se imprime como "GUIA DE DESPACHO" + folio.
            refs.append({
                "documentType": TIPO_REF_GUIA, "folio": folio_g,
                **({"date": guia_fecha.isoformat()} if guia_fecha else {}),
            })

    for a in (anticipos or []):
        folio_a = str(a.get("folio") or "").strip()
        if not folio_a or folio_a.startswith("#"):
            # "#<id>" es el placeholder que pone el router cuando la factura de anticipo
            # todavía NO tiene folio del SII (emisión en curso o fallida): no es
            # referenciable, y emitir sin la 33 dejaría el descuento sin respaldo legal
            # ante el SII. Se BLOQUEA — ni se ignora el anticipo ni se emite chueco.
            problemas.append(
                "Una factura de anticipo descontada no tiene folio SII registrado: "
                "complétalo antes de emitir esta factura (la referencia 33 lo exige)")
            continue
        if len(folio_a) > FOLIO_REF_MAX:
            problemas.append(f"El folio del anticipo ('{folio_a}') excede {FOLIO_REF_MAX} "
                             "caracteres para la referencia 33")
            continue
        # FolioRef NUMÉRICO: la 33 referencia un DTE, y el folio de un DTE es un número
        # correlativo del SII. En el modo "registrar una factura de anticipo ya emitida"
        # el folio lo TECLEA el operador, así que aquí entra cualquier cosa: se
        # reprodujo un 'N/A-99' viajando en la referencia. El SII rechaza el documento y
        # el rechazo llega con el folio propio YA CONSUMIDO — hay que bloquear antes.
        # (`isascii` además de `isdigit`: '٣'.isdigit() es True y no es un folio.)
        if not (folio_a.isascii() and folio_a.isdigit()) or int(folio_a) <= 0:
            problemas.append(
                f"El folio de la factura de anticipo ('{folio_a}') no es un número "
                "correlativo del SII: la referencia 33 apunta a un documento tributario y "
                "el SII exige su folio numérico. Corrige el N° de esa factura en "
                "Contabilidad → Facturas antes de emitir ésta")
            continue
        fecha_a = a.get("fecha")
        # Este reason SÍ se conserva (formato v3): explica POR QUÉ se referencia esa
        # factura — información que el tipo (33 = "FACTURA ELECTRONICA") y el folio no
        # dan. Sin repetir la palabra "Factura" ni el folio, que ya se imprimen.
        refs.append({
            "documentType": TIPO_REF_ANTICIPO, "folio": folio_a,
            **({"date": fecha_a.isoformat()} if fecha_a else {}),
            "reason": "Descuento anticipo"[:REASON_MAX],
        })

    if len(refs) > MAX_REFERENCIAS:
        # El mensaje anterior mandaba a "dividir el descuento de anticipos en más de una
        # factura": el operador NO tiene esa palanca — el descuento lo calcula el sistema
        # solo, por orden de antigüedad, sobre todos los anticipos pendientes. Con ese
        # texto la factura quedaba imposible de emitir Y de arreglar. Las dos salidas
        # REALES son facturar menos mercadería de una vez (así el descuento se agota con
        # menos anticipos) o registrar el documento por la vía manual.
        n_anticipos = sum(1 for r in refs if r["documentType"] == TIPO_REF_ANTICIPO)
        problemas.append(
            f"La factura llevaría {len(refs)} referencias (OC + guía + {n_anticipos} "
            f"facturas de anticipo) y Wasabil acepta máximo {MAX_REFERENCIAS}. El "
            "descuento de anticipos es automático y por orden de antigüedad, así que no "
            "se puede elegir cuáles entran: factura el despacho en tandas más chicas "
            "(cada factura descontará menos anticipos) o registra esta factura por la "
            "vía manual en Contabilidad → Facturas")
    return refs, problemas


def armar_lineas_factura(items: list) -> Tuple[List[dict], List[str]]:
    """Convierte las líneas PERSISTIDAS de la factura (MonzaContFacturaClienteItem)
    en `details` del DTE 33.

    La fuente es la factura local ya congelada — y como la factura y la guía leen el
    MISMO precio congelado de la venta (MonzaCotizacionItem.precio_unitario_clp), el
    33 cuadra con el 52 por construcción (en GA esto exige leer el payload de la guía;
    acá no hace falta).

    Las líneas locales de DESCUENTO por anticipo (Fase 7, total_neto NEGATIVO y
    `anticipo_factura_id` apuntando al anticipo) NO viajan como línea: el API real las
    RECHAZA (price y quantity deben ser > 0, y no existe descuento a nivel de
    documento). Se acumulan y se aplican como `discount` porcentual sobre las líneas
    positivas (aplicar_descuento_lineas), manteniendo el monto EXACTO.

    Lo que identifica a una línea de descuento es LA FK, no el signo: una línea
    negativa sin `anticipo_factura_id` es un problema BLOQUEANTE (ver abajo), porque
    rebajaría el DTE sin ninguna referencia 33 que lo respalde.

    Devuelve (lineas, problemas). Cualquier problema es bloqueante.
    """
    lineas: List[dict] = []
    problemas: List[str] = []
    descuento_neto = 0.0
    for it in items:
        qty = _f(it.cantidad)
        # Redondear ANTES del guard (mismo criterio que armar_lineas): el guard debe
        # evaluar el valor que realmente viaja, no un sub-centavo que llegaría como $0.
        precio = round(_f(it.precio_unit_neto), 2)
        # ANTES de los guards de cantidad/precio: una línea de descuento tiene precio
        # negativo y sería reportada como "precio $0" (o se colaría al DTE).
        # SOLO la FK alimenta el descuento. El criterio viejo ("FK o precio<0") tragaba
        # cualquier línea negativa suelta y la convertía en descuento: una línea
        # heredada de −$16.666 sin anticipo detrás —que ANTES de la Fase 7 BLOQUEABA la
        # emisión— pasaba a rebajar el DTE un 33 % sin ninguna referencia 33 que lo
        # respalde ante el SII. El descuento tiene que poder señalar QUÉ anticipo
        # descuenta; si no puede, no es un descuento.
        if getattr(it, "anticipo_factura_id", None) is not None:
            descuento_neto += -_f(it.total_neto)
            continue
        if precio < 0 or _f(getattr(it, "total_neto", None)) < 0:
            # Mensaje PROPIO, no el del anticipo: decirle al operador "falta el folio
            # del anticipo" lo mandaría a buscar un anticipo que no existe.
            problemas.append(
                f"{it.numero_parte or 'línea ' + str(it.id)}: la factura tiene una línea "
                "con monto NEGATIVO que no corresponde al descuento de ninguna factura "
                "de anticipo. El SII no acepta líneas negativas y sin anticipo detrás no "
                "hay nada que referenciar: corrige la factura o regístrala por la vía "
                "manual en Contabilidad → Facturas")
            continue
        if qty <= TOL_QTY:
            continue  # línea vacía: no viaja nada de este ítem
        if precio <= 0:
            # No debería ocurrir (la factura no se puede crear con líneas en $0), pero
            # el SII rechaza price<=0 y emitir es irreversible: se bloquea.
            problemas.append(
                f"{it.numero_parte or 'línea ' + str(it.id)}: precio $0 en la factura "
                "(no se puede emitir electrónicamente)")
            continue
        lineas.append({
            "name": acortar_nombre(it.numero_parte, it.descripcion),
            "description": (it.descripcion or "").strip()[:DESCRIPCION_MAX] or None,
            "code": (it.numero_parte or "").strip() or None,
            # externalId = el MISMO id de línea de despacho que usó la guía 52
            # (armar_lineas), de modo que factura y guía se pueden cruzar 1:1 ante una
            # auditoría. Sin despacho (retiro en oficina) se usa "fi-<id de la línea>".
            "externalId": str(it.despacho_item_id or f"fi-{it.id}"),
            "quantity": qty,
            "price": precio,
        })
    con_descuento = descuento_neto > 0
    if con_descuento:
        problemas.extend(aplicar_descuento_lineas(lineas, round(descuento_neto, 2)))
    if not lineas and not problemas:
        problemas.append("La factura no tiene líneas emitibles")
    if len(lineas) > MAX_LINEAS:
        problemas.append(f"La factura tiene {len(lineas)} líneas y Wasabil acepta máximo "
                         f"{MAX_LINEAS} por documento (divide la factura)")
    # Piso de $1: el neto se emite REDONDEADO, así que cualquier cosa bajo un peso
    # llega al SII como $0 y el DTE es rechazado. Se evalúa en DOS capas (aquí y en el
    # preview del router, sobre el neto ya calculado por Contabilidad): si solo
    # estuviera aquí, el preview diría "puede emitir" y el emitir moriría en 409
    # DESPUÉS de haber creado la factura local — una factura zombi consumiendo el cupo
    # facturable de la mercadería. Espejo de las dos capas de GA.
    # `not con_descuento`: con descuento el piso ya lo evaluó aplicar_descuento_lineas
    # SOBRE EL TOTAL YA DESCONTADO y con su propio mensaje. total_neto_lineas IGNORA
    # `discount`, así que usarlo aquí diría "neto OK" con el DTE realmente en $0.
    if lineas and not con_descuento and total_neto_lineas(lineas) < NETO_MINIMO_DTE:
        problemas.append(
            "El neto de la factura queda bajo $1: el SII no acepta un DTE en cero — "
            "revisa cantidades y precios o regístrala por la vía manual")
    return lineas, problemas


def aplicar_descuento_lineas(lineas: List[dict], descuento_neto: float) -> List[str]:
    """Reparte el descuento del anticipo como `discount` PORCENTUAL sobre las líneas,
    de mayor a menor total, con la precisión COMPLETA del float.

    Por qué porcentual y no una línea negativa: el API real RECHAZA price<0 y
    quantity<0, y el DTE no tiene descuento a nivel de documento (verificado por GA
    contra el API en borrador issue:false el 2026-07-20).

    Por qué NO se redondea el porcentaje: Wasabil calcula con la precisión enviada
    (verificado en borrador real: 16.6665% sobre 200.000 dio 166.667 exacto), así que
    el neto del DTE == neto local por construcción. Redondear el % rompería esa
    cuadratura y la factura saldría al SII por un monto distinto al registrado.

    Muta `lineas` (les agrega "discount"). Devuelve problemas (todos bloqueantes)."""
    problemas: List[str] = []
    restante = descuento_neto
    for ln in sorted(lineas, key=lambda x: x["price"] * x["quantity"], reverse=True):
        if restante <= TOL_QTY:
            break
        # El total de la línea se mide en PESOS half-up, el MISMO dominio en que
        # Contabilidad calculó el neto local (_total_linea). Medirlo en centavos
        # —round(price*qty, 2)— perdía el medio peso de una línea terminada en .5 al
        # consumirla al 100 % y el DTE salía $1 por debajo del libro de ventas
        # (reproducido: 3 líneas .5 con un anticipo de $40.000 → neto local 26.668,
        # neto emitido 26.667). Además, un tope en centavos podía exigir un `discount`
        # mayor a 100 % para agotar la línea, y el API rechaza eso.
        total_linea = neto_linea_dte(ln)
        if total_linea <= 0:
            continue
        d = min(restante, total_linea)
        objetivo = total_linea - d           # el neto EXACTO que debe quedarle a la línea
        bruto_linea = _f(ln["price"]) * _f(ln["quantity"])
        # El porcentaje va contra el bruto SIN REDONDEAR, que es lo que Wasabil
        # multiplica: así bruto × (1 − %/100) da el objetivo EXACTO y la cuadratura se
        # sostiene tanto si el receptor redondea por línea como si suma primero. Con
        # precios enteros —el caso normal— sale el mismo porcentaje de siempre
        # (30.000 sobre 80.000 → 37,5 %; 33.333 sobre 200.000 → 16,6665), así que el
        # payload de esas facturas no cambia. Escrito como (bruto − objetivo)/bruto y
        # no como 1 − objetivo/bruto: misma cuenta, sin arrastrar ruido de float.
        # Y NO se redondea (ver el docstring: Wasabil calcula con la precisión enviada).
        pct = (bruto_linea - objetivo) / bruto_linea * 100.0
        restante = round(restante - d, 2)
        if pct <= 0:
            # Resto SUB-PESO más chico que lo que la línea ya perdió al redondear (solo
            # ocurre con un anticipo con centavos): el porcentaje saldría NEGATIVO y el
            # API lo rechazaría. Se omite el `discount` — la línea redondea al MISMO
            # entero con o sin él, así que el documento no cambia.
            continue
        ln["discount"] = pct
    if restante > TOL_QTY:
        problemas.append(
            f"El descuento por anticipo (${descuento_neto:,.0f}) supera el total de las "
            "líneas de esta factura: no se puede emitir electrónicamente".replace(",", "."))
    # El SII no acepta un DTE en cero. Se evalúa SIEMPRE (no solo si alguna línea llegó
    # al 100%): un descuento repartido entre varias líneas puede dejar el total en
    # centavos sin que ninguna sola llegue al 100%. El umbral es UN PESO — el neto se
    # emite redondeado, así que bajo $1 llega al SII como $0.
    # Se miran DOS cifras y basta que una caiga bajo el piso:
    #   · total_doc  = lo que LIQUIDA el SII (half-up a peso por línea, como el neto local).
    #   · total_fino = el mismo total sin redondear, en centavos. Atrapa el documento que
    #     en el libro de ventas vale $0,99 y que el redondeo del DTE subiría a $1: el SII
    #     lo aceptaría, pero saldría por un monto distinto al registrado.
    # round() de la SUMA en el fino: el % arrastra error de float (un neto de $1,00 exacto
    # daba 0.9999999…) y sin redondear bloquearía una factura legítima.
    total_doc = total_neto_lineas(lineas)
    total_fino = round(sum(_f(ln["price"]) * _f(ln["quantity"]) *
                           (1 - _f(ln.get("discount")) / 100.0) for ln in lineas), 2)
    if lineas and min(total_doc, total_fino) < NETO_MINIMO_DTE:
        problemas.append(
            "El descuento del anticipo deja la factura en $0: el SII no acepta un "
            "DTE en cero — ajusta el descuento o registra la factura por la vía manual")
    return problemas


def armar_factura(*, referencia_interna: str, lineas: List[dict],
                  referencias: List[dict], client_id: Optional[int] = None,
                  fecha_emision: Optional[date] = None, issue: bool = False,
                  payment_method: Optional[str] = None) -> dict:
    """Arma el documento factura 33 en el vocabulario del esquema. NO lo envía.

    `issue=False` por defecto: emitir al SII es IRREVERSIBLE y este módulo solo manda
    issue=True tras la confirmación explícita del usuario (mismo protocolo que la 52).

    `referencia_interna` = "FACT-<id de la factura local>": el ancla anti doble
    emisión y de RECUPERACIÓN (el reintento sin uuid busca esa cadena en Wasabil con
    match EXACTO). NO lleva la OC adentro: Wasabil imprime este campo y la OC ya va
    en la referencia legal 801 — con ambas salía dos veces (aprendizaje del formato
    v2 de las guías, folio 136 de GA).
    """
    doc = {
        "siiDocumentTypeCode": TIPO_DOC_FACTURA,
        "issue": issue,
        "documentDate": (fecha_emision or hoy_chile()).isoformat(),
        "details": lineas,
        "references": referencias,
        "invoiceReference": str(referencia_interna)[:INVOICE_REF_MAX],
    }
    if client_id:
        doc["clientId"] = client_id  # Wasabil autocompleta RUT/razón/giro/dirección/comuna
    # paymentMethod es OBLIGATORIO en el esquema del 33 (verificado contra el API real
    # el 2026-07-20 en la Fase B de GA); 'credito' es el default del negocio.
    doc["paymentMethod"] = payment_method if payment_method in ("contado", "credito") else "credito"
    return doc
