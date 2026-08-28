from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field, field_validator
from typing import Optional
from datetime import datetime

from database import get_db
from auth import get_current_user
from empresa_guard import require_empresa
from monza_models import MonzaConfig

router = APIRouter(prefix="/api/monza/config", tags=["monza-config"],
    # Candado de empresa (hallazgo del equipo de testing 2026-08-27).
    # El TIPO DE CAMBIO de esta config es el que fija el precio de venta de MonzaParts
    # (lo copian el cotizador y el cierre de venta), y el GET entrega RUT, razón social,
    # giro y CUENTA BANCARIA de la empresa. Sin candado, una cuenta de la marca contraria
    # podía dejar el TC en 1 y toda cotización nueva salía regalada, sin que la pantalla
    # mostrara nada raro.
    dependencies=[Depends(require_empresa("automotriz"))],
)


class ConfigOut(BaseModel):
    tc_usd_clp: float
    tc_eur_clp: float
    # Tarifa aérea POR MONEDA (2026-08-08). Optional porque NULL = "no configurada":
    # la calculadora bloquea al elegir esa moneda en vez de cotizar con 0 (ver
    # _tarifa_configurada en monza_router_cotizador.py).
    tarifa_aerea_eur_por_kg: Optional[float]
    tarifa_aerea_usd_por_kg: Optional[float]
    # Legado: `moneda_tarifa` es la moneda PRESELECCIONADA en la calculadora y
    # `tarifa_aerea_por_kg` el respaldo de ESA moneda mientras la nueva no se cargue.
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
    # Tarifa aérea por moneda. gt=0: una tarifa de 0 o negativa no es un flete, y en 0
    # el precio de venta sale subvaluado sin que se note en pantalla. Para "todavía no
    # la tengo" el valor correcto es NULL (dejar el campo vacío), que la calculadora
    # trata como bloqueo explícito.
    tarifa_aerea_eur_por_kg: Optional[float] = Field(None, gt=0)
    tarifa_aerea_usd_por_kg: Optional[float] = Field(None, gt=0)
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

    # Las únicas dos monedas de flete que el sistema sabe convertir a pesos (hay TC
    # para USD y EUR, y `_tarifa_configurada` resuelve la tarifa por esas dos). Sin
    # este validador, guardar "usd" en minúscula o un "GBP" dejaba la configuración en
    # un estado que la calculadora no puede resolver y que solo aparecía al cotizar.
    @field_validator("moneda_tarifa", mode="before")
    @classmethod
    def _validar_moneda_tarifa(cls, v):
        if v is None or v == "":
            return None
        moneda = str(v).strip().upper()
        if moneda not in ("EUR", "USD"):
            raise ValueError("la moneda de la tarifa aérea debe ser EUR o USD")
        return moneda

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


# Campos donde NULL es un valor con significado propio ("no tengo tarifa en esta
# moneda"), y no la ausencia del campo en el request. Para ellos se respeta el null
# EXPLÍCITO: con `exclude_none` a secas, vaciar una tarifa cargada por error era un
# no-op silencioso y la pantalla respondía "Configuración guardada" mientras el valor
# viejo seguía cotizando (hallazgo del multienjambre 2026-08-08).
_CAMPOS_BORRABLES = ("tarifa_aerea_eur_por_kg", "tarifa_aerea_usd_por_kg")


@router.put("", response_model=ConfigOut)
def update_config(body: ConfigIn, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    cfg = _get_or_create_config(db)
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(cfg, field, value)
    # Borrado EXPLÍCITO: solo si el cliente mandó el campo (model_fields_set) y lo mandó
    # en null. Un request que no lo menciona sigue sin tocarlo.
    for field in _CAMPOS_BORRABLES:
        if field in body.model_fields_set and getattr(body, field) is None:
            setattr(cfg, field, None)
    cfg.ultima_actualizacion = datetime.utcnow()
    cfg.usuario_email = current_user.email
    db.commit()
    db.refresh(cfg)
    return cfg
