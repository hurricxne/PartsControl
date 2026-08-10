"""Tablas del espejo del libro de compras del SII — MonzaParts.

Espejo deliberado de wasabil_compras/models.py (GA) en tablas monza_sii_libro_* PROPIAS
(la separación entre marcas en MonzaParts es POR TABLA, no por columna — mismo criterio
que el resto de tablas monza_* del sistema; el candado de empresa vive en el router).

Tres tablas, tres responsabilidades:
  · monza_sii_libro_doc      — el ESPEJO: una fila por documento recibido, llave dura el uuid.
  · monza_sii_libro_sync_run — la BITÁCORA de cada barrido (sin ella, "¿de cuándo son estos
                               datos?" no tiene respuesta y el tablero no puede decir su edad).
  · monza_sii_libro_regla_rut— las REGLAS por emisor (BLOQUEADO / IGNORAR_AUTO / LOGISTICO)
                               que alimentan los defaults de la bandeja (Fase A2).

Las crea el create_all del arranque (tablas nuevas; no altera existentes) — el router de
este paquete se monta SIEMPRE (sin gate), así que sus tablas son obligatorias en toda BD.
Ningún dato de estas tablas participa de un cálculo de plata del ERP: son lectura, cruce
y decisión.
"""
import hashlib
import json
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, Column, Date, DateTime, ForeignKey, Index, Integer,
    Numeric, SmallInteger, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import deferred

from models.models import Base


# Campos del documento remoto que definen "cambió algo que importa" (Regla 7). Lista
# EXPLÍCITA y ordenada: un hash sobre "todo el JSON" cambiaría con cada campo cosmético
# nuevo del API y gritaría DIVERGENTE sin motivo.
CAMPOS_HASH = (
    "folio", "document_date", "status_id", "trx_sign",
    "sent_nsubtotal", "sent_niva", "sent_nexempt", "sent_ntotal",
    "receiver_rut", "receiver_name", "exchange_status", "sii_document_type_id",
)


def hash_contenido(doc: dict) -> str:
    """sha256 de la lista explícita de campos, con normalización estable."""
    base = {k: doc.get(k) for k in CAMPOS_HASH}
    return hashlib.sha256(
        json.dumps(base, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


class MonzaSiiLibroDoc(Base):
    """Un documento RECIBIDO del libro Monza, tal como Wasabil lo declara.

    REGLAS DE VIDA (del plan, sección modelo de datos):
      · Identidad técnica: `uuid` con UNIQUE DURO. La llave de negocio
        (rut_emisor_canonico, tipo, folio) lleva índice NO único: si aparece duplicada,
        eso es un hallazgo para el tablero, no una excepción del barrido.
      · Los campos remotos se SOBRESCRIBEN ciegamente en cada barrido (son caché de una
        verdad ajena — Regla 6). Lo que NUNCA se pisa es la decisión local.
      · Jamás DELETE: si un documento deja de venir, pasa a estado_espejo=DESAPARECIDO
        (Regla 5). Borrar filas con decisiones tomadas destruye la auditoría.
      · Si el contenido cambia DESPUÉS de una decisión (hash distinto al de decidir),
        `divergente` se enciende y la bandeja lo muestra: el sistema no repara solo
        (Regla 7).
    """
    __tablename__ = "monza_sii_libro_doc"
    __table_args__ = (
        UniqueConstraint("uuid", name="uq_monza_sii_libro_doc_uuid"),
        Index("ix_monza_sii_libro_negocio", "rut_emisor_canonico", "sii_document_type_id",
              "folio"),
        Index("ix_monza_sii_libro_fecha", "document_date"),
        {"mysql_engine": "InnoDB"},
    )

    id = Column(Integer, primary_key=True)
    uuid = Column(String(40), nullable=False)
    document = Column(BigInteger, nullable=True)          # id interno de Wasabil
    sii_document_type_id = Column(Integer, nullable=True) # 33/34/61/56/…
    folio = Column(String(30), nullable=True)
    document_date = Column(Date, nullable=True)
    status_id = Column(Integer, nullable=True)

    # ── Montos: SIEMPRE con su signo aparte (Regla 9) ────────────────────────────
    trx_sign = Column(SmallInteger, nullable=True)        # 1 cargo · -1 abono (NC)
    sent_nsubtotal = Column(Numeric(16, 2), nullable=True)
    sent_niva = Column(Numeric(16, 2), nullable=True)
    sent_nexempt = Column(Numeric(16, 2), nullable=True)
    sent_ntotal = Column(Numeric(16, 2), nullable=True)   # POSITIVO; el signo va aparte
    # La ÚNICA magnitud sumable (sent_ntotal × trx_sign), materializada para que ninguna
    # consulta del módulo re-derive la regla y se equivoque.
    monto_efectivo = Column(Numeric(16, 2), nullable=True)

    # ── Emisor ───────────────────────────────────────────────────────────────────
    receiver_rut_original = Column(String(30), nullable=True)   # tal cual vino
    rut_emisor_canonico = Column(String(20), nullable=True)     # la llave de cruce
    receiver_name = Column(String(255), nullable=True)
    supplier_uid = Column(String(40), nullable=True)

    exchange_status = Column(String(10), nullable=True)   # PENDING|ERM|RFT (acuse)
    payment_method = Column(String(20), nullable=True)

    # ── Ciclo de vida del espejo ─────────────────────────────────────────────────
    estado_espejo = Column(String(20), nullable=False, server_default="ACTIVO")
    # ACTIVO | DESAPARECIDO (dejó de venir en un barrido completo — jamás se borra)
    hash_remoto = Column(String(64), nullable=True)
    # Marca de agua anti mark-and-sweep booleano (hallazgo del auditor: dos barridos
    # solapados con un booleano global marcan DESAPARECIDO documentos vivos). Cada upsert
    # escribe el id del run que lo vio; DESAPARECIDO = ultimo_run_id < run recién
    # completado, y los runs no corren en paralelo (guard en sync.py).
    ultimo_run_id = Column(Integer, ForeignKey("monza_sii_libro_sync_run.id",
                                               ondelete="SET NULL"), nullable=True)

    # ── Decisión local (Fase A2) — LO QUE EL BARRIDO NUNCA PISA ─────────────────
    decision = Column(String(20), nullable=True)     # NULL=pendiente | ignorado | clasificado
    destino = Column(String(60), nullable=True)      # 'costo_venta' | 'cc:<clave>'
    decision_motivo = Column(String(400), nullable=True)
    decision_usuario_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"),
                                 nullable=True)
    decision_at = Column(DateTime, nullable=True)
    hash_al_decidir = Column(String(64), nullable=True)
    divergente = Column(Boolean, nullable=False, server_default="0")
    divergencia_detalle = Column(String(500), nullable=True)

    # deferred() a propósito (hallazgo confirmado 2026-08-06 «bandeja y resumen cargan
    # TODOS los docs activos con raw_json de hasta 60KB por fila que nadie usa»): esta
    # columna es ~90% del peso de cada fila y NINGÚN endpoint la lee — solo la escribe el
    # barrido. Con deferred, los .all() de /resumen, /documentos y /export.csv dejan de
    # arrastrarla desde MySQL; quien la necesite la carga lazy al acceder al atributo.
    raw_json = deferred(Column(Text, nullable=True))  # el documento completo del listado
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MonzaSiiLibroSyncRun(Base):
    """Bitácora de UN barrido. El tablero lee de acá la EDAD del último exitoso (rojo a
    las 48 h) y el operador diagnostica sin acceso a Wasabil."""
    __tablename__ = "monza_sii_libro_sync_run"
    __table_args__ = ({"mysql_engine": "InnoDB"},)

    id = Column(Integer, primary_key=True)
    origen = Column(String(20), nullable=False)      # nocturno | manual
    iniciado_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    terminado_at = Column(DateTime, nullable=True)
    exito = Column(Boolean, nullable=True)           # NULL = corriendo/abortado sin cierre
    total_api = Column(Integer, nullable=True)       # lo que el API declaró
    nuevos = Column(Integer, nullable=True)
    actualizados = Column(Integer, nullable=True)
    desaparecidos = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)
    from_date = Column(Date, nullable=True)
    to_date = Column(Date, nullable=True)
    usuario_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)


class MonzaSiiLibroReglaRut(Base):
    """Regla PERSISTENTE por RUT emisor (Fase A2): el default que la bandeja aplica.

    Tres niveles (decisión del dueño en el addendum — la clasificación POR DOCUMENTO
    manda sobre esta regla, que es solo el punto de partida):
      · BLOQUEADO     — jamás puede ir a costo de mercadería (financiero, bancos, seguros,
                        compra de moneda — p.ej. Vector Capital).
      · IGNORAR_AUTO  — documentos que no requieren decisión (se marcan ignorados solos).
      · LOGISTICO     — candidato a costo por venta O gasto: SIEMPRE se decide documento
                        a documento, con default en gasto del período.
    """
    __tablename__ = "monza_sii_libro_regla_rut"
    __table_args__ = (
        UniqueConstraint("rut_canonico", name="uq_monza_sii_libro_regla_rut"),
        {"mysql_engine": "InnoDB"},
    )

    id = Column(Integer, primary_key=True)
    rut_canonico = Column(String(20), nullable=False)
    nivel = Column(String(20), nullable=False)       # BLOQUEADO | IGNORAR_AUTO | LOGISTICO
    destino_default = Column(String(60), nullable=True)   # sugerencia ('cc:financiero'…)
    motivo = Column(String(300), nullable=True)
    usuario_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ═══════════════════════════════════════════════════════════════════════════════════
# MATCHER banco ↔ libro SII ↔ egresos — MonzaParts
# (spec docs/spec-matcher-banco-libro-2026-08-06.md; espejo de wasabil_compras/models.py)
# ═══════════════════════════════════════════════════════════════════════════════════
# Las FKs de abajo apuntan a monza_tes_movimiento (monza_tesoreria/models.py). Sin este
# import, un consumidor que importe estas tablas de forma AISLADA (un test del paquete,
# un script) muere con NoReferencedTableError según el ORDEN de imports — misma lección
# que monza_compras_contab/models.py con monza_embarques_pricing (hallazgo F8).
#
# CIERRE COMPLETO DEL GRAFO (hallazgo confirmado 2026-08-06 #13: «con
# MONZA_CONTAB_ENABLED=false el backend ENTERO no arranca»): el import de
# monza_tesoreria.models mete a Base.metadata las tablas monza_tes_conciliacion /
# _ingreso, cuyas FKs siguen hacia monza_cont_egreso (monza_compras_contab),
# monza_cont_adelanto y monza_cont_cobranza (monza_contabilidad) → monza_cotizaciones
# (monza_models). Con el gate contable APAGADO nada más registra esas tablas y el
# create_all del arranque (main.py, que importa este paquete SIEMPRE, fuera del gate)
# revienta con NoReferencedTableError en toda BD fresca: la salida de emergencia
# documentada tumbaba las DOS marcas. Cerrar el grafo acá cuesta unas tablas vacías
# inofensivas con el gate apagado — muchísimo mejor que no arrancar. La sonda §11 de
# tests/test_a1_a2.py verifica el cierre en un intérprete limpio.
import monza_tesoreria.models      # noqa: E402,F401  (registra monza_tes_movimiento y monza_tes_*)
import monza_models                # noqa: E402,F401  (registra monza_cotizaciones y el núcleo Monza)
import monza_contabilidad.models   # noqa: E402,F401  (registra monza_cont_adelanto/cobranza/factura)
import monza_compras_contab.models  # noqa: E402,F401  (registra monza_cont_egreso; arrastra monza_embarques_pricing)


class MonzaSiiLibroMatch(Base):
    """La TABLA PUENTE: un enlace (movimiento bancario Monza, documento del libro).

    PRINCIPIO RECTOR de la spec: un match falso AUTOMÁTICO es peor que cien
    sugerencias. AUTO no es un puntaje — es una CONJUNCIÓN de condiciones duras con
    identidad de documento; el score guardado es cache de pantalla, JAMÁS autorización
    (confirmar revalida TODO bajo lock).

    Ciclo de vida (máquina de estados de la spec, transiciones RESTRINGIDAS):
      · motor:  (nada)→sugerido | (nada)→confirmado(origen=auto) | (nada)→conflicto |
                sugerido→sugerido (refresh score/motivo) | sugerido→en_duda (caducado) |
                confirmado(origen=auto)→en_duda. JAMÁS toca confirmado(origen=humano)
                ni revive un descartado — el descartado OCUPA el par en el UNIQUE:
                eso ES el no-revivir.
      · humano: sugerido/en_duda/conflicto→confirmado|descartado; deshacer un
                confirmado es acción humana explícita con usuario y fecha.

    ADAPTACIÓN Monza (vs SiiLibroMatch de GA): SIN columna `empresa` — la separación
    entre marcas en MonzaParts es POR TABLA (mismo criterio que el resto de tablas
    monza_*); el candado de empresa vive en el router.

    FK a monza_tes_movimiento con ondelete=SET NULL y movimiento_id NULLABLE — la
    misma desviación RAZONADA del RESTRICT del DDL conceptual que en GA, porque las
    DOS sondas de la Regla 5 solo son satisfacibles a la vez sin tocar
    monza_tesoreria/router.py (fuera del alcance):
      · «borrar mov con match vivo = 409»: lo da el guard EXISTENTE de Tesorería
        (mov.conciliado → 409), porque confirmar/auto marca el movimiento conciliado.
      · «los descartes sobreviven al re-import de cartola»: al borrar la cartola el
        match descartado SOBREVIVE con movimiento_id NULL y se re-ancla por
        clave_mov_snapshot (los 6 campos de _clave_mov) — con RESTRICT el borrado
        reventaría en 500 y con CASCADE (prohibido) se destruiría la auditoría.
    El doc jamás se borra (pasa a DESAPARECIDO), así que su FK sí es RESTRICT.
    """
    __tablename__ = "monza_sii_libro_match"
    __table_args__ = (
        # Necesario pero NO suficiente: no impide 2 movs sobre el mismo doc — esa
        # unicidad la aplica el MOTOR bajo lock (MySQL no tiene únicos parciales).
        UniqueConstraint("movimiento_id", "doc_id", name="uq_monza_sii_match_par"),
        Index("ix_monza_sii_match_doc_estado", "doc_id", "estado"),  # tope Σ por doc
        Index("ix_monza_sii_match_mov_estado", "movimiento_id", "estado"),  # Σ por mov
        Index("ix_monza_sii_match_estado", "estado"),                # bandeja
        Index("ix_monza_sii_match_grupo", "grupo_uuid"),
        {"mysql_engine": "InnoDB"},
    )

    id = Column(Integer, primary_key=True)
    movimiento_id = Column(Integer, ForeignKey("monza_tes_movimiento.id",
                                               ondelete="SET NULL"), nullable=True)
    doc_id = Column(Integer, ForeignKey("monza_sii_libro_doc.id", ondelete="RESTRICT"),
                    nullable=False)
    # Filas nacidas juntas (subset factura−NC, agregado mensual): se confirman o
    # descartan ATÓMICAS.
    grupo_uuid = Column(String(36), nullable=True)
    # CON SIGNO: la NC compensada va NEGATIVA (un asignado>=0 implícito no la representa).
    monto_asignado = Column(Numeric(16, 2), nullable=False)
    via = Column(String(20), nullable=False)
    # directa | subset | parcial | cruce_triple | agregado_mensual | honorarios | nc_abono
    estado = Column(String(12), nullable=False)
    # sugerido | confirmado | descartado | en_duda | conflicto
    origen = Column(String(12), nullable=False)
    # auto (motor; usuario_id NULL — el motor solo degrada los suyos) | humano | wasabil
    score = Column(SmallInteger, nullable=True)      # cache de pantalla, jamás autoriza
    motivo = Column(Text, nullable=False)            # JSON: lista de razones legibles
    candidatos_evaluados = Column(Text, nullable=True)  # JSON: por qué fue único
    # Snapshot del doc al decidir: el barrido pisa ciego monto/folio/rut y el
    # `divergente` del espejo SOLO protege la decision de bandeja, no los matches.
    hash_doc_al_matchear = Column(String(64), nullable=True)
    monto_doc_al_matchear = Column(Numeric(16, 2), nullable=True)   # saldo al decidir
    # Los 6 campos de _clave_mov (monza_tesoreria/router.py): los descartes sobreviven
    # al re-import de cartola re-anclándose por esta clave suave.
    clave_mov_snapshot = Column(Text, nullable=True)  # JSON
    divergente = Column(Boolean, nullable=False, server_default="0")
    divergencia_detalle = Column(String(500), nullable=True)
    usuario_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"),
                        nullable=True)
    decidido_at = Column(DateTime, nullable=True)    # confirmación o descarte
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
                        nullable=False)


class MonzaSiiMatchRun(Base):
    """Bitácora de UNA corrida del matcher Monza (espejo de monza_sii_libro_sync_run):
    sin ella, «¿de cuándo son estas sugerencias?» no tiene respuesta."""
    __tablename__ = "monza_sii_match_run"
    __table_args__ = ({"mysql_engine": "InnoDB"},)

    id = Column(Integer, primary_key=True)
    origen = Column(String(20), nullable=False)  # post_barrido|post_cartola|nocturno|manual
    iniciado_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    terminado_at = Column(DateTime, nullable=True)
    exito = Column(Boolean, nullable=True)       # NULL = corriendo/abortado sin cierre
    sugeridos_nuevos = Column(Integer, nullable=True)
    autos_nuevos = Column(Integer, nullable=True)
    degradados_en_duda = Column(Integer, nullable=True)
    caducados = Column(Integer, nullable=True)
    conflictos = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)
    usuario_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"),
                        nullable=True)


class MonzaSiiMatchEtiquetaMov(Base):
    """Etiqueta de EXCLUSIÓN del pool para un movimiento, REVERSIBLE por el operador:
    blocklist_sin_sii (TGR/F29/remuneraciones… sin contraparte DTE) | nomina |
    comision_banco (van a la rama de agregado mensual) | lote_pendiente (el cargo
    calza con la suma de 2-6 egresos sin conciliar: primero Tesorería).

    La etiqueta NO impide la conciliación interna banco↔egreso existente (regla 18).
    FK con CASCADE — desviación razonada del RESTRICT del DDL conceptual: la etiqueta
    es derivada y reversible (no una decisión con auditoría que proteger), y con
    RESTRICT cada borrado legítimo de un movimiento blocklisteado reventaría en 500
    en un router (monza_tesoreria) que este módulo no puede tocar.
    """
    __tablename__ = "monza_sii_match_etiqueta_mov"
    __table_args__ = (
        UniqueConstraint("movimiento_id", name="uq_monza_sii_etiq_mov"),
        {"mysql_engine": "InnoDB"},
    )

    id = Column(Integer, primary_key=True)
    movimiento_id = Column(Integer, ForeignKey("monza_tes_movimiento.id",
                                               ondelete="CASCADE"), nullable=False)
    etiqueta = Column(String(30), nullable=False)
    detalle = Column(String(300), nullable=True)
    revertida = Column(Boolean, nullable=False, server_default="0")
    usuario_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"),
                        nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class MonzaSiiMatchConfig(Base):
    """Configuración mantenible SIN redeploy (spec, sección tabla puente): lista de
    factors, stopwords extra, abreviaturas extra, tabla anual de retención de
    honorarios, RUTs relacionados/banco, y el interruptor 'rut_en_glosa_activo' que
    nace APAGADO hasta la sonda de ingestión (≥3 cartolas reales con RUT). Los
    defaults viven en matcher.py; una fila acá los pisa por clave."""
    __tablename__ = "monza_sii_match_config"
    __table_args__ = (
        UniqueConstraint("clave", name="uq_monza_sii_match_config_clave"),
        {"mysql_engine": "InnoDB"},
    )

    id = Column(Integer, primary_key=True)
    clave = Column(String(60), nullable=False)
    valor = Column(Text, nullable=False)             # JSON
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
                        nullable=False)
