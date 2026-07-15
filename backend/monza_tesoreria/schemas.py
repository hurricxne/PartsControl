"""Schemas Pydantic del módulo Tesorería MonzaParts.

Montos con gt/ge (rechaza negativos en origen) y strings con tope de longitud.
"""
from typing import Optional, Literal
from pydantic import BaseModel, Field, model_validator


class CuentaIn(BaseModel):
    banco: str = Field(..., max_length=100)
    nombre: Optional[str] = Field(None, max_length=120)
    numero_cuenta: Optional[str] = Field(None, max_length=60)
    moneda: Literal["CLP", "USD", "EUR"] = "CLP"
    activo: bool = True
    observaciones: Optional[str] = Field(None, max_length=65535)


class MovimientoIn(BaseModel):
    """Alta manual de un movimiento bancario."""
    cuenta_id: int
    fecha: Optional[str] = Field(None, max_length=30)
    glosa: Optional[str] = Field(None, max_length=500)
    tipo: Literal["cargo", "abono"] = "cargo"
    monto: float = Field(..., gt=0)
    referencia: Optional[str] = Field(None, max_length=150)
    saldo: Optional[float] = None
    cartola_id: Optional[int] = None


class ConciliarIn(BaseModel):
    """Enlaza un movimiento con su destino. Exactamente UNO de los tres:
      · egreso_id   → cargo ↔ Comprobante de Egreso de Compras/CxP.
      · adelanto_id → abono ↔ Adelanto 50% verificado (plus Monza).
      · cobranza_id → abono ↔ cobranza (ingreso de caja) de Facturas y Cobranzas."""
    egreso_id: Optional[int] = Field(None, gt=0)
    adelanto_id: Optional[int] = Field(None, gt=0)
    cobranza_id: Optional[int] = Field(None, gt=0)

    @model_validator(mode="after")
    def _exactamente_uno(self):
        n = sum(1 for v in (self.egreso_id, self.adelanto_id, self.cobranza_id) if v)
        if n != 1:
            raise ValueError("Indique exactamente uno: egreso_id (cargo), adelanto_id o cobranza_id (abono)")
        return self


class AprobarAdelantoIn(BaseModel):
    """Aprobación del adelanto por TESORERÍA: el monto realmente recibido en el banco,
    con su fecha, banco y N° de operación. Da la orden que destraba a Abastecimiento."""
    monto: float = Field(..., gt=0)
    fecha_pago: Optional[str] = Field(None, max_length=30)
    banco: Optional[str] = Field(None, max_length=100)
    numero_operacion: Optional[str] = Field(None, max_length=100)
    observaciones: Optional[str] = Field(None, max_length=65535)
