"""Lógica pura del módulo Wasabil DTE (sin BD, sin red) — armado y validación
de la guía de despacho electrónica (SII tipo 52).

Reglas de negocio clave (aprendidas en producción, ver README):
- El tipo de traslado lo elige el operador al emitir (TIPOS_TRASLADO; default 1
  "Operación constituye venta"). La guía se valoriza SIEMPRE con IVA y el NETO es
  la cifra ancla (mismos precios que usará la factura después).
- El nombre de cada línea = la DESCRIPCIÓN limpia recortada a 25 chars (límite
  SII); el n° de parte va en `code` y la descripción completa aparte (hasta 255).
  (v2 tras la primera emisión real, folio 136: concatenar parte+descripción
  duplicaba el dato con `code` y cortaba a media palabra.)
- La guía SIEMPRE referencia la OC del cliente: tipo 801 con N° OC y FECHA de la
  OC — UNA sola vez: `invoice_reference` lleva SOLO el N° de despacho interno
  (ancla anti doble emisión; Wasabil lo imprime, y con la OC dentro salía
  referenciada dos veces).
- IVA 19% con redondeo half-up a peso (igual que el SII / formulario F-1).
- Nada de esto llama a Wasabil: el router junta los datos y este archivo los arma.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

# La fecha del DTE es la de CHILE, no la del server (en producción el VPS puede
# estar en UTC: pasadas las ~20-21h en Chile, date.today() ya sería "mañana").
TZ_CHILE = ZoneInfo("America/Santiago")

IVA_RATE = Decimal("0.19")
TIPO_DOC_GUIA = 52
TIPO_TRASLADO_VENTA = 1          # dispatchTypeCode 1 = "Operación constituye venta"
# Tipos de traslado del SII para la guía de despacho (dispatchTypeCode). El operador
# elige uno en el modal de emisión; 1 (venta) es el caso típico de Grupo AM, pero un
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
TIPO_REF_OC = "801"              # referencia SII: Orden de Compra del cliente
NOMBRE_MAX = 25                  # límite del SII para el nombre de línea
DESCRIPCION_MAX = 255            # descripción larga de línea
REASON_MAX = 90                  # límite del campo reason de una referencia
CONTACTO_MAX = 80                # receiverContact del DTE
INVOICE_REF_MAX = 200
FOLIO_REF_MAX = 18               # límite del SII para el folio de una referencia (N° OC)
MAX_LINEAS = 60                  # tope de líneas por documento en Wasabil
TOL_QTY = 0.001                  # tolerancia de cantidades (unidades)

# Formatos reales vistos en oc_cliente.fecha_oc (texto libre, String(50))
_FORMATOS_FECHA = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%Y/%m/%d",
                   "%d.%m.%Y", "%Y-%m-%dT%H:%M:%S")


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def hoy_chile() -> date:
    """Fecha actual en Chile (America/Santiago) — la fecha tributaria del DTE."""
    return datetime.now(TZ_CHILE).date()


def parse_fecha_oc(s: Optional[str]) -> Optional[date]:
    """Parseo tolerante de la fecha de la OC (viene como texto libre).
    Devuelve None si no se pudo interpretar — el preview lo reporta como bloqueante
    porque la referencia 801 debe llevar la fecha real de la OC."""
    if not s:
        return None
    texto = str(s).strip()
    if not texto:
        return None
    for fmt in _FORMATOS_FECHA:
        try:
            return datetime.strptime(texto[:19] if "T" in texto else texto, fmt).date()
        except ValueError:
            continue
    # Último intento: ISO con hora ("2026-06-10 00:00:00")
    try:
        return datetime.fromisoformat(texto).date()
    except ValueError:
        return None


def acortar_nombre(numero_parte: Optional[str], descripcion: Optional[str]) -> str:
    """Nombre de línea = la DESCRIPCIÓN limpia, recortada a 25 chars (límite SII).

    El N° de parte NO se antepone: ya viaja en el campo `code` y se imprime como
    código en la guía — concatenarlo duplicaba el dato y cortaba la descripción a
    media palabra (hallazgo de la primera emisión real, folio 136: el nombre salía
    "ROD-INF-PV351 RODILLO INF"). Sin descripción, el N° de parte es el nombre."""
    parte = (numero_parte or "").strip()
    desc = (descripcion or "").strip()
    if desc:
        return desc[:NOMBRE_MAX].rstrip()
    if parte:
        return parte[:NOMBRE_MAX].rstrip()
    return "ITEM"


def cuadratura(neto: float) -> Tuple[int, int, int]:
    """(neto, iva, total) en pesos enteros con redondeo half-up (== SII/F-1)."""
    neto_d = Decimal(str(neto)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    iva_d = (neto_d * IVA_RATE).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(neto_d), int(iva_d), int(neto_d + iva_d)


def armar_lineas(despacho_items: list, precios_por_item: dict) -> Tuple[List[dict], List[str]]:
    """Convierte los ítems del despacho en líneas del DTE.

    `despacho_items`: DespachoItem (con .item_cotizacion cargado).
    `precios_por_item`: {item_cotizacion_id: {"precio_venta_clp": ...}} — el MISMO
    cálculo de precios que usa Contabilidad al facturar (guía y factura cuadran).

    Devuelve (lineas, problemas). Cada problema es bloqueante para emitir.
    """
    lineas: List[dict] = []
    problemas: List[str] = []
    for di in despacho_items:
        it = di.item_cotizacion
        if not it:
            problemas.append(f"El ítem del despacho #{di.id} ya no existe en la cotización")
            continue
        qty = _f(di.qty_despachada)
        if qty <= TOL_QTY:
            continue  # línea vacía: no viaja nada de este ítem
        ci = precios_por_item.get(di.item_cotizacion_id) or {}
        # Redondear ANTES del guard: el guard debe evaluar el MISMO valor que se
        # persiste en la línea (un sub-centavo pasaría el guard y viajaría como $0)
        precio = round(_f(ci.get("precio_venta_clp")), 2)
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
            # externalId = despacho_item_id: identidad ÚNICA de la línea (permite que la
            # factura tome el precio congelado de ESTA guía con match 1:1, sin depender
            # de que el n° de parte sea único). Wasabil lo acepta como ref del ERP.
            "externalId": str(di.id),
            "quantity": qty,
            "price": precio,   # precio unitario NETO en CLP (ya redondeado a 2)
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
    esquema oficial de Wasabil el 2026-07-15). NO lo envía.

    `issue=False` por defecto: emitir al SII es IRREVERSIBLE y este módulo solo
    manda issue=True después de la confirmación explícita del usuario en la
    previsualización (protocolo de seguridad del README).

    `tipo_traslado` es el dispatchTypeCode del SII (ver TIPOS_TRASLADO): 1 venta por
    defecto, 5 traslado interno hacia bodega propia, etc.
    """
    if tipo_traslado not in TIPOS_TRASLADO:
        raise ValueError(f"Tipo de traslado inválido: {tipo_traslado}")
    doc = {
        "siiDocumentTypeCode": TIPO_DOC_GUIA,
        "issue": issue,
        "documentDate": (fecha_emision or hoy_chile()).isoformat(),
        "dispatchGuide": {"dispatchTypeCode": tipo_traslado},
        "details": lineas,
        # Referencia estructurada a la OC del cliente (obligatoria en Grupo AM)
        "references": [{
            "documentType": TIPO_REF_OC,
            "folio": str(numero_oc),
            "date": fecha_oc.isoformat(),
            "reason": f"Orden de compra {numero_oc}"[:REASON_MAX],
        }],
        # Referencia libre = SOLO el N° de despacho interno (ancla anti doble
        # emisión: única por despacho, permite reencontrar el documento ante un
        # reintento). La OC NO va aquí: Wasabil imprime este campo en la guía y
        # con "OC ... · DSP-..." la orden de compra salía referenciada DOS veces
        # (hallazgo de la primera emisión real, folio 136) — la referencia legal
        # a la OC es la 801 de arriba, una sola vez.
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
    (dispatch_guide/references aceptados al crear un borrador); si la primera
    emisión real revelara otro nombre, se corrige SOLO aquí. Ver nota en client.py.
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


def total_neto_lineas(lineas: List[dict]) -> float:
    """Neto del documento = Σ (precio unitario × cantidad) redondeado a peso POR LÍNEA
    con half-up — mismo criterio que crear_factura en Contabilidad y que lo que el
    usuario ve en pantalla (Math.round del frontend es half-up; el round() de Python
    es banker's y descuadraría por 1 peso en los .5)."""
    total = Decimal("0")
    for ln in lineas:
        total += Decimal(str(ln["price"] * ln["quantity"])).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP)
    return float(total)


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
    from .models import STATUS_LABEL, STATUS_FALLIDO  # import local (módulo puro)
    en_vuelo = claim_vigente(dte)
    if dte.uuid:
        estado = STATUS_LABEL.get(dte.status_id, "sin_respuesta")
    elif en_vuelo:
        estado = "enviando"
    elif dte.error:
        estado = "error_envio"
    else:
        estado = "no_enviado"
    puede_reintentar = (not en_vuelo) and (
        dte.status_id == STATUS_FALLIDO or (dte.uuid is None and bool(dte.error))
    )
    return {
        "id": dte.id,
        "tipo_dte": dte.tipo_dte,
        "despacho_id": dte.despacho_id,
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
