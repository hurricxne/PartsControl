from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
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
    iva_pct: Optional[float] = None
    razon_social: Optional[str] = None
    rut_empresa: Optional[str] = None
    direccion: Optional[str] = None
    giro: Optional[str] = None
    email_empresa: Optional[str] = None
    banco: Optional[str] = None
    tipo_cuenta: Optional[str] = None
    numero_cuenta: Optional[str] = None
    condiciones_default: Optional[str] = None


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
