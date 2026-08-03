"""Schemas (Pydantic) del módulo Contabilidad MonzaParts. Validan la entrada en el
borde del sistema (mismo contrato que el módulo de Grupo AM)."""
from typing import Optional, List

from pydantic import BaseModel, Field


class FacturaItemIn(BaseModel):
    """Una línea a facturar. cantidad/precio son opcionales: si faltan se toman de lo
    despachado y del precio neto del ítem. despacho_item_id liga la línea a una guía."""
    item_cotizacion_id: int
    despacho_item_id: Optional[int] = None
    cantidad: Optional[float] = Field(None, gt=0)
    # gt=0 (espejo GA contabilidad.py): con ge=0 una línea en $0 generaba una factura
    # en $0 que nacía 'pagada'. Sin precio explícito (None) se usa el del ítem.
    precio_unit_neto: Optional[float] = Field(None, gt=0)


class FacturaCreate(BaseModel):
    """Emitir una factura. Tres modos:
      - `despacho_id`: deriva las líneas de esa guía despachada (tope por lo despachado).
      - `sin_guia=True` (retiro en oficina): factura el saldo pendiente de la cotización,
        SIN requerir despacho (tope por lo VENDIDO − ya facturado). EXCLUYENTE: no se
        combina con despacho_id ni items.
      - `items` explícitos (tope por lo despachado).
      - `es_anticipo=True` (vía B): factura de ANTICIPO, la ÚNICA que NO nace de una
        guía; el tope es el total de la venta aún no facturado.
    numero_factura es el folio SII (único).

    Este mismo schema lo usa la emisión electrónica (monza_wasabil_dte/router.py lo
    importa de aquí), así que los campos nuevos quedan disponibles en preview y emisión
    sin duplicar contrato."""
    cotizacion_id: int
    despacho_id: Optional[int] = None
    sin_guia: bool = False
    numero_factura: Optional[str] = None
    tipo_doc: str = "factura"
    fecha_emision: Optional[str] = None
    condicion_pago: Optional[str] = None
    plazo_dias: Optional[int] = Field(None, ge=0, le=3650)
    items: Optional[List[FacturaItemIn]] = None
    observaciones: Optional[str] = None
    # Factura de ANTICIPO (Fase 7, vía B): respalda ante el SII un adelanto cobrado antes
    # de que llegara la mercadería. `monto_neto_anticipo` es el NETO (el IVA lo calcula el
    # backend con la tasa CONGELADA de la venta, no con un 19% fijo).
    # A diferencia de Grupo AM aquí NO hay `adelanto_ids`: Monza tiene UN adelanto por
    # venta (uq_monza_cont_adelanto_cotizacion) y el vínculo adelanto↔factura de anticipo
    # se DERIVA de las facturas es_anticipo=1 de la venta — cero estado que se desincronice.
    es_anticipo: bool = False
    monto_neto_anticipo: Optional[float] = Field(None, gt=0)
    descripcion_anticipo: Optional[str] = None
    # Puerta EXPLÍCITA para un segundo anticipo en la misma venta. Por defecto el
    # constructor lo bloquea (409): emitir un DTE 33 es IRREVERSIBLE y el candado anti
    # doble emisión del módulo SII solo dura mientras el HTTP está en vuelo, así que sin
    # este guard dos clics seguidos facturaban DOS anticipos reales por UN adelanto.
    # No se elimina la posibilidad —hay negocios que pactan dos anticipos parciales—:
    # se exige decirlo a propósito. Ver _construir_factura_anticipo.
    confirmar_segundo_anticipo: bool = False


class CobranzaIn(BaseModel):
    """Pago real del cliente. medio: transferencia|cheque|efectivo (las cobranzas de
    factoring NO van por aquí; se generan desde el panel de factoring)."""
    fecha: Optional[str] = None
    monto: float = Field(..., gt=0)
    medio: str = "transferencia"
    banco: Optional[str] = None
    numero_operacion: Optional[str] = None
    observaciones: Optional[str] = None


class FactoringIn(BaseModel):
    """Cesión de la factura a un factor. monto_adelantado <= cupo (bruto - pagos
    reales); si retencion no viene, se deriva = cupo - adelanto."""
    empresa_factoring: Optional[str] = None
    id_operacion: Optional[str] = None
    fecha_operacion: Optional[str] = None
    monto_adelantado: float = Field(0, ge=0)
    costo_factoring: float = Field(0, ge=0)
    retencion: Optional[float] = Field(None, ge=0)
    banco: Optional[str] = None
    observaciones: Optional[str] = None


class RevertirFactoringIn(BaseModel):
    """Reversión de una cesión al factor que quedó contra un documento que el SII nunca
    conoció. El motivo es OBLIGATORIO y no es burocracia: la operación de factoring
    DESAPARECE (la fila se borra, ver `revertir_factoring`), así que este texto es lo único
    que después explica por qué. Queda en `factura.observaciones` y en el log del servidor.

    `min_length` no alcanza: ' ' pasa el validador de Pydantic. El router exige 5
    caracteres REALES tras `strip()`."""
    motivo: str = Field(..., min_length=5, max_length=500)


class GuiaFirmadaIn(BaseModel):
    """Marca/registra (opcional) si la guía de un despacho fue firmada por el cliente.
    No es requisito para facturar; es informativo y registrable caso a caso."""
    firmada: bool = True
    archivo: Optional[str] = Field(None, max_length=255)


class AdelantoVerificarIn(BaseModel):
    """Verificación del adelanto por Contabilidad: el monto realmente recibido, con su
    fecha, banco y N° de operación. Confirma el adelanto que Comercial informó al cerrar."""
    monto: float = Field(..., gt=0)
    fecha_pago: Optional[str] = None
    banco: Optional[str] = None
    numero_operacion: Optional[str] = None
    observaciones: Optional[str] = None
