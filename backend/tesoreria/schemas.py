"""Schemas Pydantic del módulo Tesorería."""
from typing import Optional, Literal
from pydantic import BaseModel, Field, model_validator


class CuentaIn(BaseModel):
    banco: str
    nombre: Optional[str] = None
    numero_cuenta: Optional[str] = None
    moneda: Literal["CLP", "USD", "EUR"] = "CLP"
    activo: bool = True
    observaciones: Optional[str] = None


class MovimientoIn(BaseModel):
    """Alta manual de un movimiento bancario."""
    cuenta_id: int
    fecha: Optional[str] = None
    glosa: Optional[str] = None
    tipo: Literal["cargo", "abono"] = "cargo"
    monto: float = Field(..., gt=0)
    referencia: Optional[str] = None
    saldo: Optional[float] = None
    cartola_id: Optional[int] = None


class ConciliarIn(BaseModel):
    """Enlaza un movimiento con su destino. Exactamente UNO de los dos:
      · egreso_id   → cargo  ↔ Comprobante de Egreso de Compras/CxP.
      · cobranza_id → abono  ↔ cobranza (ingreso de caja) de Facturas y Cobranzas."""
    egreso_id: Optional[int] = Field(None, gt=0)
    cobranza_id: Optional[int] = Field(None, gt=0)

    @model_validator(mode="after")
    def _exactamente_uno(self):
        if bool(self.egreso_id) == bool(self.cobranza_id):
            raise ValueError("Indique egreso_id (cargo) O cobranza_id (abono), exactamente uno")
        return self
