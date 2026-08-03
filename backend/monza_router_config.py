from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field, field_validator
from typing import Optional
from datetime import datetime

from database import get_db
from auth import get_current_user
from monza_models import MonzaConfig

router = APIRouter(prefix="/api/monza/config", tags=["monza-config"])


class ConfigOut(BaseModel):
    tc_usd_clp: float
    tc_eur_clp: float
    tarifa_aerea_por_kg: float
    moneda_tarifa: str
    iva_pct: float
    razon_social: str
    rut_empresa: str
    direccion: str
    giro: str
    email_empresa: str
    banco: Optional[str]
    tipo_cuenta: Optional[str]
    numero_cuenta: Optional[str]
    condiciones_default: Optional[str]
    ultima_actualizacion: Optional[datetime]
    usuario_email: Optional[str]

    class Config:
        from_attributes = True


class ConfigIn(BaseModel):
    tc_usd_clp: Optional[float] = None
    tc_eur_clp: Optional[float] = None
    tarifa_aerea_por_kg: Optional[float] = None
    moneda_tarifa: Optional[str] = None
    # Hallazgo #5 de la auditoría integral (Fases 1-6): con iva_pct = 0 la venta se
    # cerraba con IVA 0 pero la FACTURACIÓN usaba el 19 % por defecto (iva_rate_de
    # no distingue "sin dato" de "0 % explícito"), inventando impuesto en un documento
    # que se manda al SII y dejando el resto de la mercadería IMPOSIBLE de facturar.
    # Esta es la ÚNICA puerta de entrada del dato: la cotización no recibe iva_pct del
    # cliente, lo copia de la config (monza_router_cotizaciones.py:251). Cerrándola
    # aquí, ninguna venta puede nacer con tasa 0 (ni con una tasa absurda > 100).
    # NO se implementa un "0 = exento": la tubería DTE no sabe expresar exención
    # (MntExe); si el negocio la necesita, es un trabajo aparte.
    iva_pct: Optional[float] = Field(None, gt=0, le=100)
    razon_social: Optional[str] = None
    rut_empresa: Optional[str] = None
    direccion: Optional[str] = None
    giro: Optional[str] = None
    email_empresa: Optional[str] = None
    banco: Optional[str] = None
    tipo_cuenta: Optional[str] = None
    numero_cuenta: Optional[str] = None
    condiciones_default: Optional[str] = None

    # mode="before" a propósito: corre ANTES de las restricciones gt/le, así el
    # operador recibe un mensaje en castellano ("la tasa de IVA debe ser mayor a 0")
    # en vez del "Input should be greater than 0" genérico de pydantic.
    @field_validator("iva_pct", mode="before")
    @classmethod
    def _validar_iva_pct(cls, v):
        if v is None or v == "":
            return None
        try:
            n = float(v)
        except (TypeError, ValueError):
            return v  # tipo inválido: que lo reporte pydantic con su mensaje estándar
        if n <= 0:
            raise ValueError(
                "la tasa de IVA debe ser mayor a 0: una venta con 0 % se facturaría "
                "igual con el 19 % por defecto e inventaría impuesto ante el SII"
            )
        if n > 100:
            raise ValueError("la tasa de IVA no puede superar el 100 %")
        return n


def _get_or_create_config(db: Session) -> MonzaConfig:
    cfg = db.query(MonzaConfig).filter(MonzaConfig.id == 1).first()
    if not cfg:
        cfg = MonzaConfig(id=1)
        db.add(cfg)
        db.commit()
        db.refresh(cfg)
    return cfg


@router.get("", response_model=ConfigOut)
def get_config(db: Session = Depends(get_db), _=Depends(get_current_user)):
    return _get_or_create_config(db)


@router.put("", response_model=ConfigOut)
def update_config(body: ConfigIn, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    cfg = _get_or_create_config(db)
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(cfg, field, value)
    cfg.ultima_actualizacion = datetime.utcnow()
    cfg.usuario_email = current_user.email
    db.commit()
    db.refresh(cfg)
    return cfg
