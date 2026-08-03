"""Tablas del módulo Tesorería (Grupo AM) — aisladas y aditivas.

Las 4 tablas conc_* vienen del antiguo módulo Conciliación Bancaria y CONSERVAN su
nombre físico (continuidad de datos en producción: renombrar tablas exigiría una
migración; el nombre del módulo cambió, los datos no). Se agrega UNA tabla nueva:

  conc_cuenta_bancaria       → cuentas bancarias de la empresa (Santander CLP, etc.).
  conc_cartola               → cada carga de cartola (lote de movimientos), auditable.
  conc_movimiento            → un movimiento del banco (cargo|abono).
  conc_conciliacion          → enlace cargo ↔ cont_egreso (Comprobante de Egreso de Compras).
  conc_conciliacion_ingreso  → NUEVA: enlace abono ↔ cont_cobranza (ingreso de caja
                               registrado en Facturas y Cobranzas) O abono ↔ cont_adelanto
                               (adelanto de cliente aprobado por Tesorería) — exactamente
                               uno. El "conciliado" se DERIVA de la existencia del enlace
                               (no se agregan columnas a cont_cobranza/cont_adelanto).

El esquema de enlaces es N:M preparado para parciales; HOY el router concilia 1:1
exacto (montos iguales ±TOL). Un egreso ya paga varias compras vía cont_egreso_detalle.
Se crean con `tesoreria/init_db.py` (standalone) o con el create_all de arranque.
"""
from sqlalchemy import (
    CheckConstraint, Column, Integer, String, Numeric, Boolean, Text, Date, DateTime,
    ForeignKey, Index, UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from database import Base


class CuentaBancaria(Base):
    """Cuenta bancaria de la empresa (catálogo). El banco como ENTIDAD permite agrupar
    los movimientos por cuenta de forma fiable (vs. un string libre)."""
    __tablename__ = "conc_cuenta_bancaria"
    __table_args__ = (
        Index("ix_conc_cuenta_empresa", "empresa"),
        {"mysql_engine": "InnoDB"},
    )
    id = Column(Integer, primary_key=True, index=True)
    empresa = Column(String(50), nullable=False, server_default="mineria")
    banco = Column(String(100), nullable=False)            # "Santander", "BCI", ...
    nombre = Column(String(120), nullable=True)            # alias, ej "Cta Cte principal"
    numero_cuenta = Column(String(60), nullable=True)
    moneda = Column(String(10), default="CLP")
    activo = Column(Boolean, default=True, index=True)
    observaciones = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    movimientos = relationship("MovimientoBancario", back_populates="cuenta",
                               cascade="all, delete-orphan")
    cartolas = relationship("Cartola", back_populates="cuenta",
                            cascade="all, delete-orphan")


class Cartola(Base):
    """Una carga de cartola (lote de movimientos) — para auditoría y borrado en bloque."""
    __tablename__ = "conc_cartola"
    __table_args__ = (
        Index("ix_conc_cartola_empresa_cuenta", "empresa", "cuenta_id"),
        {"mysql_engine": "InnoDB"},
    )
    id = Column(Integer, primary_key=True, index=True)
    empresa = Column(String(50), nullable=False, server_default="mineria")
    cuenta_id = Column(Integer, ForeignKey("conc_cuenta_bancaria.id", ondelete="CASCADE"), index=True)
    nombre = Column(String(150), nullable=True)            # ej "Cartola junio 2026"
    fecha_desde = Column(Date, nullable=True)
    fecha_hasta = Column(Date, nullable=True)
    archivo = Column(String(255), nullable=True)           # nombre del archivo cargado (si vino de archivo)
    origen = Column(String(20), default="manual")          # manual | archivo
    n_movimientos = Column(Integer, default=0)
    usuario_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    cuenta = relationship("CuentaBancaria", back_populates="cartolas")
    movimientos = relationship("MovimientoBancario", back_populates="cartola")


class MovimientoBancario(Base):
    """Un movimiento de la cartola del banco.
      · tipo='cargo' (salida) se concilia contra un Comprobante de Egreso de Compras
        (cont_egreso, vía conc_conciliacion).
      · tipo='abono' (entrada) se concilia contra una cobranza de Facturas y Cobranzas
        (cont_cobranza, vía conc_conciliacion_ingreso).
    """
    __tablename__ = "conc_movimiento"
    __table_args__ = (
        Index("ix_conc_mov_empresa_cuenta_fecha", "empresa", "cuenta_id", "fecha"),
        Index("ix_conc_mov_conciliado", "conciliado"),
        {"mysql_engine": "InnoDB"},
    )
    id = Column(Integer, primary_key=True, index=True)
    empresa = Column(String(50), nullable=False, server_default="mineria")
    cuenta_id = Column(Integer, ForeignKey("conc_cuenta_bancaria.id", ondelete="CASCADE"), index=True)
    cartola_id = Column(Integer, ForeignKey("conc_cartola.id", ondelete="SET NULL"), nullable=True, index=True)

    fecha = Column(Date, nullable=True, index=True)
    glosa = Column(String(500), nullable=True)             # descripción/detalle del banco
    tipo = Column(String(10), nullable=False, default="cargo")   # cargo (egreso) | abono (ingreso)
    monto = Column(Numeric(16, 2), default=0)              # SIEMPRE positivo; el signo lo da `tipo`
    referencia = Column(String(150), nullable=True)        # N° operación / documento de la cartola
    saldo = Column(Numeric(16, 2), nullable=True)          # saldo contable de la línea (opcional)

    # Estado de conciliación (el enlace real es N:M vía conc_conciliacion / _ingreso).
    # (el índice lo aporta ix_conc_mov_conciliado de __table_args__: sin index=True)
    conciliado = Column(Boolean, default=False)
    conciliado_at = Column(DateTime(timezone=True), nullable=True)
    conciliado_usuario_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    observaciones = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    cuenta = relationship("CuentaBancaria", back_populates="movimientos")
    cartola = relationship("Cartola", back_populates="movimientos")
    conciliaciones = relationship("Conciliacion", back_populates="movimiento",
                                  cascade="all, delete-orphan")
    conciliaciones_ingreso = relationship("ConciliacionIngreso", back_populates="movimiento",
                                          cascade="all, delete-orphan")


class Conciliacion(Base):
    """Enlace movimiento bancario (cargo) ↔ Comprobante de Egreso (cont_egreso). El esquema
    es N:M (preparado para parciales: 1 mov ↔ varios egresos / 1 egreso ↔ varios movs),
    pero HOY el router solo crea enlaces 1:1 exactos (montos iguales). Un egreso ya
    paga varias compras vía cont_egreso_detalle (ese es el caso 'un movimiento paga
    varios gastos'); los parciales banco↔egreso son fase FUTURA.

    Invariante reforzada TAMBIÉN a nivel de BD (defensa en profundidad; el router ya la
    valida con `egreso.conciliado` bajo lock):
      · UNIQUE egreso_id: un egreso solo puede estar enlazado a UN movimiento (1:1; los
        NULL no colisionan en MySQL). Espejo de monza_tes_conciliacion. Sin ella el lock
        del router era la ÚNICA defensa: cualquier camino nuevo —o una reparación en
        SQL— podía conciliar el mismo pago contra dos cargos del banco. Si algún día se
        habilitan parciales (N:M), relajarla.
    Migración aditiva e idempotente en init_db.py."""
    __tablename__ = "conc_conciliacion"
    __table_args__ = (
        # Evita doble-enlace idéntico del mismo par (defensa en profundidad; no bloquea
        # repartos legítimos a egresos/movimientos distintos si se habilita N:M).
        UniqueConstraint("movimiento_id", "egreso_id", name="uq_conc_concil_mov_egreso"),
        UniqueConstraint("egreso_id", name="uq_conc_concil_egreso"),
        Index("ix_conc_concil_mov", "movimiento_id"),
        {"mysql_engine": "InnoDB"},
    )
    id = Column(Integer, primary_key=True, index=True)
    empresa = Column(String(50), nullable=False, server_default="mineria")
    # (el índice de movimiento_id es el Index explícito de __table_args__ y el de
    #  egreso_id lo aporta su UNIQUE: sin index=True no se crean índices duplicados)
    movimiento_id = Column(Integer, ForeignKey("conc_movimiento.id", ondelete="CASCADE"))
    egreso_id = Column(Integer, ForeignKey("cont_egreso.id", ondelete="CASCADE"))
    monto_conciliado_clp = Column(Numeric(16, 2), default=0)
    usuario_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    # Snapshot del egreso ANTES de que la cartola lo enriqueciera al conciliar:
    # desconciliar RESTAURA estos valores (deshacer = volver al estado previo),
    # conservando lo que el operador ingresó a mano incluso si coincidía con lo
    # del banco (la igualdad de valores no distingue el origen del dato).
    fecha_egreso_previa = Column(Date, nullable=True)
    referencia_egreso_previa = Column(String(120), nullable=True)

    movimiento = relationship("MovimientoBancario", back_populates="conciliaciones")


class ConciliacionIngreso(Base):
    """Enlace movimiento bancario (abono) ↔ destino conciliado. Exactamente UNO de:
      · cobranza_id  (abono ↔ cobranza de Facturas y Cobranzas — cont_cobranza)
      · adelanto_id  (abono ↔ adelanto de cliente APROBADO por Tesorería — cont_adelanto;
                      la plata del adelanto se concilia por AQUÍ, y su aplicación a
                      facturas —cobranzas medio='adelanto'— queda EXCLUIDA de esta
                      conciliación: no es un depósito nuevo)
    El "conciliado" de la cobranza/adelanto se DERIVA de la existencia del enlace (no se
    agregan columnas a cont_cobranza/cont_adelanto).

    Invariantes a nivel de BD (defensa en profundidad; el router y el schema Pydantic
    ya las validan):
      · CHECK "exactamente uno": (cobranza_id XOR adelanto_id) no nulo.
      · UNIQUE cobranza_id / UNIQUE adelanto_id: conciliación 1:1 (los NULL no
        colisionan en MySQL). Si algún día se habilitan parciales (N:M), relajarlos.
    El borrado de una cobranza conciliada se BLOQUEA en Facturas y Cobranzas (409:
    desconciliar primero en Tesorería); el CASCADE es solo última defensa."""
    __tablename__ = "conc_conciliacion_ingreso"
    __table_args__ = (
        CheckConstraint(
            "(cobranza_id IS NOT NULL) + (adelanto_id IS NOT NULL) = 1",
            name="ck_conc_concil_ing_un_destino",
        ),
        UniqueConstraint("cobranza_id", name="uq_conc_concil_ing_cobranza"),
        UniqueConstraint("adelanto_id", name="uq_conc_concil_ing_adelanto"),
        Index("ix_conc_concil_ing_mov", "movimiento_id"),
        {"mysql_engine": "InnoDB"},
    )
    id = Column(Integer, primary_key=True, index=True)
    empresa = Column(String(50), nullable=False, server_default="mineria")
    # (el índice de movimiento_id es el Index explícito de __table_args__; los de
    #  cobranza_id/adelanto_id los aportan sus UNIQUE)
    movimiento_id = Column(Integer, ForeignKey("conc_movimiento.id", ondelete="CASCADE"))
    cobranza_id = Column(Integer, ForeignKey("cont_cobranza.id", ondelete="CASCADE"), nullable=True)
    adelanto_id = Column(Integer, ForeignKey("cont_adelanto.id", ondelete="CASCADE"), nullable=True)
    monto_conciliado_clp = Column(Numeric(16, 2), default=0)
    usuario_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    movimiento = relationship("MovimientoBancario", back_populates="conciliaciones_ingreso")
