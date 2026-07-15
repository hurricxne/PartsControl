"""Schemas Pydantic del módulo Compras / Cuentas por Pagar."""
from typing import Optional, Literal, List
from pydantic import BaseModel, Field

TipoGasto = Literal["cogs", "gasto_operacional", "gasto_no_operacional", "otros"]
Moneda = Literal["CLP", "USD", "EUR"]
Condicion = Literal["contado", "credito"]
Medio = Literal["transferencia", "cheque", "efectivo", "tarjeta"]


class PagoInline(BaseModel):
    """Pago al momento de registrar la compra (contado o abono inmediato). Genera un
    Comprobante de Egreso con un solo detalle (esta compra)."""
    fecha: Optional[str] = None
    monto_clp: Optional[float] = Field(None, gt=0)   # si falta en contado → total
    medio: Medio = "transferencia"
    banco: Optional[str] = None
    cuenta_origen_id: Optional[int] = None
    fecha_mov_bancario: Optional[str] = None
    numero_operacion: Optional[str] = None
    observaciones: Optional[str] = None


class CompraCreate(BaseModel):
    """Alta de una compra/gasto. Si condicion_pago='contado' y no viene `pago`, se genera
    automáticamente un egreso por el total (sale del banco el mismo día)."""
    tipo_gasto: TipoGasto
    categoria: Optional[str] = None
    cuenta_contable_id: Optional[int] = None
    es_anticipo: bool = False
    origen: str = "MANUAL"
    proveedor_id: Optional[int] = None
    acreedor: Optional[str] = None
    proveedor_rut: Optional[str] = None
    fecha: Optional[str] = None
    referencia: Optional[str] = None
    descripcion: Optional[str] = None
    numero_documento: Optional[str] = None
    tipo_doc: str = "factura"
    moneda: Moneda = "CLP"
    tc: float = Field(1, gt=0)
    monto_neto: float = Field(0, ge=0)
    iva: Optional[float] = Field(None, ge=0)
    monto_total: Optional[float] = Field(None, ge=0)
    afecto_iva: bool = True
    condicion_pago: Condicion = "credito"
    plazo_dias: Optional[int] = Field(None, ge=0)
    embarque_id: Optional[int] = None
    emb_pricing_gasto_id: Optional[int] = None
    factura_proveedor_id: Optional[int] = None
    observaciones: Optional[str] = None
    pago: Optional[PagoInline] = None


class PagoIn(BaseModel):
    """Pago posterior de UNA compra (parcial o total). Genera un egreso de 1 detalle."""
    fecha: Optional[str] = None
    monto_clp: float = Field(..., gt=0)
    medio: Medio = "transferencia"
    banco: Optional[str] = None
    cuenta_origen_id: Optional[int] = None
    fecha_mov_bancario: Optional[str] = None
    numero_operacion: Optional[str] = None
    observaciones: Optional[str] = None


class EgresoDetalleIn(BaseModel):
    compra_id: int
    monto_clp: float = Field(..., gt=0)


class EgresoCreate(BaseModel):
    """Comprobante de Egreso CONSOLIDADO: una salida de dinero que paga VARIAS compras."""
    fecha: Optional[str] = None
    medio: Medio = "transferencia"
    cuenta_origen_id: Optional[int] = None
    banco: Optional[str] = None
    numero_operacion: Optional[str] = None
    beneficiario: Optional[str] = None
    beneficiario_rut: Optional[str] = None
    glosa: Optional[str] = None
    moneda: Moneda = "CLP"
    tc: float = Field(1, gt=0)
    fecha_mov_bancario: Optional[str] = None
    detalles: List[EgresoDetalleIn] = Field(..., min_length=1)


class EgresoUpdate(BaseModel):
    """Completar/editar datos de conciliación de un egreso (fecha banco / referencia)."""
    fecha_mov_bancario: Optional[str] = None
    referencia_bancaria: Optional[str] = None


class AnularIn(BaseModel):
    motivo: Optional[str] = None
