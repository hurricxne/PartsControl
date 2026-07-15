"""Tablas del módulo Tesorería MonzaParts — aisladas y aditivas.

Espejo de las conc_* de Grupo AM (backend/conciliacion_bancaria/models.py) en tablas
monza_tes_* propias (separación entre empresas POR TABLA, criterio Monza; el candado de
empresa vive en el router).

  monza_tes_cuenta_bancaria      → cuentas bancarias de MonzaParts (catálogo).
  monza_tes_cartola              → cada carga de cartola (lote de movimientos), auditable.
  monza_tes_movimiento           → un movimiento del banco (cargo|abono).
  monza_tes_conciliacion         → enlace movimiento ↔ egreso de Compras O ↔ adelanto 50%.
  monza_tes_conciliacion_ingreso → NUEVA (espejo de conc_conciliacion_ingreso de Grupo AM):
                                   enlace abono ↔ monza_cont_cobranza (ingreso de caja de
                                   Facturas y Cobranzas). El "conciliado" de la cobranza se
                                   DERIVA de la existencia del enlace (no se agregan
                                   columnas a monza_cont_cobranza).

El enlace es N:M preparado para parciales, pero HOY el router concilia 1:1 exacto
(montos iguales ±TOL), igual que Grupo AM:
  · cargo  ↔ monza_cont_egreso   (marca egreso.conciliado + fecha/ref bancaria)
  · abono  ↔ monza_cont_adelanto (el "conciliado" del adelanto se DERIVA de la
    existencia del enlace; no se agregan columnas a la tabla de monza_contabilidad)
  · abono  ↔ monza_cont_cobranza (el "conciliado" de la cobranza también se DERIVA
    del enlace, en monza_tes_conciliacion_ingreso)

Se crean con `monza_tesoreria/init_db.py` o con el create_all de main.py.
"""
from sqlalchemy import (
    Column, Integer, String, Numeric, Boolean, Text, Date, DateTime,
    ForeignKey, Index, UniqueConstraint, CheckConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from database import Base


class MonzaTesCuentaBancaria(Base):
    """Cuenta bancaria de MonzaParts (catálogo). El banco como ENTIDAD permite agrupar
    los movimientos por cuenta de forma fiable (vs. un string libre)."""
    __tablename__ = "monza_tes_cuenta_bancaria"
    __table_args__ = ({"mysql_engine": "InnoDB"},)

    id = Column(Integer, primary_key=True, index=True)
    banco = Column(String(100), nullable=False)            # "Santander", "BCI", ...
    nombre = Column(String(120), nullable=True)            # alias, ej "Cta Cte principal"
    numero_cuenta = Column(String(60), nullable=True)
    moneda = Column(String(10), default="CLP")
    activo = Column(Boolean, default=True, index=True)
    observaciones = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    movimientos = relationship("MonzaTesMovimiento", back_populates="cuenta",
                               cascade="all, delete-orphan")
    cartolas = relationship("MonzaTesCartola", back_populates="cuenta",
                            cascade="all, delete-orphan")


class MonzaTesCartola(Base):
    """Una carga de cartola (lote de movimientos) — para auditoría y borrado en bloque."""
    __tablename__ = "monza_tes_cartola"
    __table_args__ = (
        Index("ix_monza_tes_cartola_cuenta", "cuenta_id"),
        {"mysql_engine": "InnoDB"},
    )
    id = Column(Integer, primary_key=True, index=True)
    # (el índice de cuenta_id es el Index explícito de __table_args__; no usar index=True
    #  acá o se crean DOS índices para la misma columna)
    cuenta_id = Column(Integer, ForeignKey("monza_tes_cuenta_bancaria.id", ondelete="CASCADE"))
    nombre = Column(String(150), nullable=True)            # ej "Cartola junio 2026"
    fecha_desde = Column(Date, nullable=True)
    fecha_hasta = Column(Date, nullable=True)
    archivo = Column(String(255), nullable=True)           # nombre del archivo cargado
    origen = Column(String(20), default="manual")          # manual | archivo
    n_movimientos = Column(Integer, default=0)
    usuario_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    cuenta = relationship("MonzaTesCuentaBancaria", back_populates="cartolas")
    movimientos = relationship("MonzaTesMovimiento", back_populates="cartola")


class MonzaTesMovimiento(Base):
    """Un movimiento de la cartola del banco.
      · tipo='cargo' (salida) se concilia contra un Comprobante de Egreso de Compras.
      · tipo='abono' (entrada) se concilia contra un Adelanto 50% verificado (plus
        Monza, vía monza_tes_conciliacion) O contra una cobranza de Facturas y
        Cobranzas (vía monza_tes_conciliacion_ingreso).
    """
    __tablename__ = "monza_tes_movimiento"
    __table_args__ = (
        Index("ix_monza_tes_mov_cuenta_fecha", "cuenta_id", "fecha"),
        Index("ix_monza_tes_mov_conciliado", "conciliado"),
        {"mysql_engine": "InnoDB"},
    )
    id = Column(Integer, primary_key=True, index=True)
    cuenta_id = Column(Integer, ForeignKey("monza_tes_cuenta_bancaria.id", ondelete="CASCADE"), index=True)
    cartola_id = Column(Integer, ForeignKey("monza_tes_cartola.id", ondelete="SET NULL"), nullable=True, index=True)

    fecha = Column(Date, nullable=True, index=True)
    glosa = Column(String(500), nullable=True)             # descripción/detalle del banco
    tipo = Column(String(10), nullable=False, default="cargo")   # cargo (salida) | abono (entrada)
    monto = Column(Numeric(16, 2), default=0)              # SIEMPRE positivo; el signo lo da `tipo`
    referencia = Column(String(150), nullable=True)        # N° operación / documento de la cartola
    saldo = Column(Numeric(16, 2), nullable=True)          # saldo contable de la línea (opcional)

    # Estado de conciliación (el enlace real es N:M vía monza_tes_conciliacion).
    conciliado = Column(Boolean, default=False)
    conciliado_at = Column(DateTime(timezone=True), nullable=True)
    conciliado_usuario_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    observaciones = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    cuenta = relationship("MonzaTesCuentaBancaria", back_populates="movimientos")
    cartola = relationship("MonzaTesCartola", back_populates="movimientos")
    conciliaciones = relationship("MonzaTesConciliacion", back_populates="movimiento",
                                  cascade="all, delete-orphan")
    conciliaciones_ingreso = relationship("MonzaTesConciliacionIngreso", back_populates="movimiento",
                                          cascade="all, delete-orphan")


class MonzaTesConciliacion(Base):
    """Enlace movimiento bancario ↔ destino conciliado. Exactamente UNO de los dos:
      · egreso_id   (cargo  ↔ Comprobante de Egreso de monza_compras_contab)
      · adelanto_id (abono  ↔ Adelanto verificado de monza_contabilidad)
    El "conciliado" del adelanto se deriva de la existencia de este enlace (no se
    agregan columnas a monza_cont_adelanto).

    Invariantes reforzadas TAMBIÉN a nivel de BD (defensa en profundidad; el router y
    el schema Pydantic ya las validan):
      · CHECK "exactamente uno": (egreso_id XOR adelanto_id) no nulo.
      · UNIQUE egreso_id / UNIQUE adelanto_id: conciliación 1:1 (un destino solo puede
        estar enlazado a UN movimiento; los NULL no colisionan en MySQL). Si algún día
        se habilitan parciales (N:M), relajar estos UNIQUE."""
    __tablename__ = "monza_tes_conciliacion"
    __table_args__ = (
        CheckConstraint(
            "(egreso_id IS NOT NULL) + (adelanto_id IS NOT NULL) = 1",
            name="ck_monza_tes_concil_un_destino",
        ),
        UniqueConstraint("egreso_id", name="uq_monza_tes_concil_egreso"),
        UniqueConstraint("adelanto_id", name="uq_monza_tes_concil_adelanto"),
        Index("ix_monza_tes_concil_mov", "movimiento_id"),
        {"mysql_engine": "InnoDB"},
    )
    id = Column(Integer, primary_key=True, index=True)
    # (el índice de movimiento_id es el Index explícito de __table_args__; los de
    #  egreso_id/adelanto_id los aportan sus UNIQUE)
    movimiento_id = Column(Integer, ForeignKey("monza_tes_movimiento.id", ondelete="CASCADE"))
    egreso_id = Column(Integer, ForeignKey("monza_cont_egreso.id", ondelete="CASCADE"), nullable=True)
    adelanto_id = Column(Integer, ForeignKey("monza_cont_adelanto.id", ondelete="CASCADE"), nullable=True)
    monto_conciliado_clp = Column(Numeric(16, 2), default=0)
    usuario_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    movimiento = relationship("MonzaTesMovimiento", back_populates="conciliaciones")


class MonzaTesConciliacionIngreso(Base):
    """Enlace movimiento bancario (abono) ↔ cobranza de Facturas y Cobranzas
    (monza_cont_cobranza: el ingreso de caja que registró el operador). Espejo de
    conc_conciliacion_ingreso de Grupo AM. El "conciliado" de la cobranza se DERIVA de
    la existencia de este enlace (no se agregan columnas a monza_cont_cobranza, tabla
    de otro módulo).

    Invariantes a nivel de BD (defensa en profundidad; el router y el schema Pydantic
    ya las validan):
      · UNIQUE cobranza_id: una cobranza solo puede estar conciliada con UN movimiento
        (1:1; los NULL no aplican porque la columna es NOT NULL de facto en el uso).
    El borrado de una cobranza conciliada se BLOQUEA en Facturas y Cobranzas (409:
    desconciliar primero en Tesorería); el CASCADE es solo última defensa."""
    __tablename__ = "monza_tes_conciliacion_ingreso"
    __table_args__ = (
        UniqueConstraint("cobranza_id", name="uq_monza_tes_concil_ing_cobranza"),
        Index("ix_monza_tes_concil_ing_mov", "movimiento_id"),
        {"mysql_engine": "InnoDB"},
    )
    id = Column(Integer, primary_key=True, index=True)
    # (el índice de movimiento_id es el Index explícito de __table_args__; el de
    #  cobranza_id lo aporta su UNIQUE)
    movimiento_id = Column(Integer, ForeignKey("monza_tes_movimiento.id", ondelete="CASCADE"))
    cobranza_id = Column(Integer, ForeignKey("monza_cont_cobranza.id", ondelete="CASCADE"))
    monto_conciliado_clp = Column(Numeric(16, 2), default=0)
    usuario_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    movimiento = relationship("MonzaTesMovimiento", back_populates="conciliaciones_ingreso")
