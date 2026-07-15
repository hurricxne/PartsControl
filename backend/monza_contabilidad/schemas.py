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
    precio_unit_neto: Optional[float] = Field(None, ge=0)


class FacturaCreate(BaseModel):
    """Emitir una factura. Tres modos:
      - `despacho_id`: deriva las líneas de esa guía despachada (tope por lo despachado).
      - `sin_guia=True` (retiro en oficina): factura el saldo pendiente de la cotización,
        SIN requerir despacho (tope por lo VENDIDO − ya facturado). EXCLUYENTE: no se
        combina con despacho_id ni items.
      - `items` explícitos (tope por lo despachado).
    numero_factura es el folio SII (único)."""
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
