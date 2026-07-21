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
    """Enlaza un movimiento con su destino. Exactamente UNO de los tres:
      · egreso_id   → cargo  ↔ Comprobante de Egreso de Compras/CxP.
      · cobranza_id → abono  ↔ cobranza (ingreso de caja) de Facturas y Cobranzas.
      · adelanto_id → abono  ↔ adelanto de cliente APROBADO (cont_adelanto)."""
    egreso_id: Optional[int] = Field(None, gt=0)
    cobranza_id: Optional[int] = Field(None, gt=0)
    adelanto_id: Optional[int] = Field(None, gt=0)

    @model_validator(mode="after")
    def _exactamente_uno(self):
        n = sum(1 for v in (self.egreso_id, self.cobranza_id, self.adelanto_id) if v)
        if n != 1:
            raise ValueError(
                "Indique egreso_id (cargo), cobranza_id (abono) o adelanto_id (abono), exactamente uno")
        return self


class AprobarAdelantoIn(BaseModel):
    """TESORERÍA aprueba un adelanto informado por Comercial: confirma la plata recibida
    (monto real, fecha, banco, N° operación). NO exige cartola subida: la conciliación
    con el abono del banco es un paso posterior e independiente."""
    monto: float = Field(..., gt=0)
    fecha_pago: Optional[str] = None
    banco: Optional[str] = None
    numero_operacion: Optional[str] = None
    observaciones: Optional[str] = None
