"""Schemas Pydantic del módulo Compras / Cuentas por Pagar MonzaParts.

Espejo de backend/compras_contab/schemas.py. Montos con ge/gt (rechaza negativos en
origen) y strings con tope de longitud (falla limpio en la API antes de tocar la BD).
"""
from typing import Optional, Literal, List
from pydantic import BaseModel, Field

TipoGasto = Literal["cogs", "gasto_operacional", "gasto_no_operacional", "otros"]
Moneda = Literal["CLP", "USD", "EUR"]
Condicion = Literal["contado", "credito"]
Medio = Literal["transferencia", "cheque", "efectivo", "tarjeta"]


class PagoInline(BaseModel):
    """Pago al momento de registrar la compra (contado o abono inmediato). Genera un
    Comprobante de Egreso con un solo detalle (esta compra)."""
    fecha: Optional[str] = Field(None, max_length=30)
    monto_clp: Optional[float] = Field(None, gt=0)   # si falta en contado → total
    medio: Medio = "transferencia"
    banco: Optional[str] = Field(None, max_length=100)
    cuenta_origen_id: Optional[int] = None
    fecha_mov_bancario: Optional[str] = Field(None, max_length=30)
    numero_operacion: Optional[str] = Field(None, max_length=100)
    observaciones: Optional[str] = Field(None, max_length=500)


class CompraCreate(BaseModel):
    """Alta de una compra/gasto. Si condicion_pago='contado' y no viene `pago`, se genera
    automáticamente un egreso por el total (sale del banco el mismo día)."""
    tipo_gasto: TipoGasto
    categoria: Optional[str] = Field(None, max_length=80)
    cuenta_contable_id: Optional[int] = None
    es_anticipo: bool = False
    origen: str = Field("MANUAL", max_length=20)
    proveedor_id: Optional[int] = None
    acreedor: Optional[str] = Field(None, max_length=255)
    proveedor_rut: Optional[str] = Field(None, max_length=50)
    fecha: Optional[str] = Field(None, max_length=30)
    referencia: Optional[str] = Field(None, max_length=120)
    descripcion: Optional[str] = Field(None, max_length=500)
    numero_documento: Optional[str] = Field(None, max_length=100)
    tipo_doc: str = Field("factura", max_length=20)
    moneda: Moneda = "CLP"
    tc: float = Field(1, gt=0)
    monto_neto: float = Field(0, ge=0)
    iva: Optional[float] = Field(None, ge=0)
    monto_total: Optional[float] = Field(None, ge=0)
    afecto_iva: bool = True
    condicion_pago: Condicion = "credito"
    plazo_dias: Optional[int] = Field(None, ge=0, le=3650)
    embarque_id: Optional[int] = None
    emb_pricing_gasto_id: Optional[int] = None
    observaciones: Optional[str] = Field(None, max_length=65535)
    pago: Optional[PagoInline] = None


class PagoIn(BaseModel):
    """Pago posterior de UNA compra (parcial o total). Genera un egreso de 1 detalle."""
    fecha: Optional[str] = Field(None, max_length=30)
    monto_clp: float = Field(..., gt=0)
    medio: Medio = "transferencia"
    banco: Optional[str] = Field(None, max_length=100)
    cuenta_origen_id: Optional[int] = None
    fecha_mov_bancario: Optional[str] = Field(None, max_length=30)
    numero_operacion: Optional[str] = Field(None, max_length=100)
    observaciones: Optional[str] = Field(None, max_length=500)


class EgresoDetalleIn(BaseModel):
    compra_id: int
    monto_clp: float = Field(..., gt=0)


class EgresoCreate(BaseModel):
    """Comprobante de Egreso CONSOLIDADO: una salida de dinero que paga VARIAS compras."""
    fecha: Optional[str] = Field(None, max_length=30)
    medio: Medio = "transferencia"
    cuenta_origen_id: Optional[int] = None
    banco: Optional[str] = Field(None, max_length=100)
    numero_operacion: Optional[str] = Field(None, max_length=100)
    beneficiario: Optional[str] = Field(None, max_length=255)
    beneficiario_rut: Optional[str] = Field(None, max_length=50)
    glosa: Optional[str] = Field(None, max_length=500)
    moneda: Moneda = "CLP"
    tc: float = Field(1, gt=0)
    fecha_mov_bancario: Optional[str] = Field(None, max_length=30)
    detalles: List[EgresoDetalleIn] = Field(..., min_length=1)


class EgresoUpdate(BaseModel):
    """Completar/editar datos de conciliación de un egreso (fecha banco / referencia)."""
    fecha_mov_bancario: Optional[str] = Field(None, max_length=30)
    referencia_bancaria: Optional[str] = Field(None, max_length=120)


class AnularIn(BaseModel):
    motivo: Optional[str] = Field(None, max_length=255)
