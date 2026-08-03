"""Schemas Pydantic del módulo Tesorería.

Montos con gt/ge (rechaza negativos en origen) y strings con tope de longitud
ALINEADO a la columna (espejo de monza_tesoreria/schemas.py). Sin el max_length un
texto más largo que la columna llega hasta el INSERT: MySQL laxo lo TRUNCA en
silencio (dos referencias distintas quedan iguales) y MySQL estricto responde 500
en vez del 422 que corresponde a un dato mal formado del cliente.
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
    fecha_pago: Optional[str] = Field(None, max_length=30)
    banco: Optional[str] = Field(None, max_length=100)
    numero_operacion: Optional[str] = Field(None, max_length=100)
    observaciones: Optional[str] = Field(None, max_length=65535)
