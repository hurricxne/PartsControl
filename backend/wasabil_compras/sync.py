"""El barrido del espejo — COMPLETO todas las noches, idempotente, sin DELETE jamás.

CONTRATO (Reglas 5, 6 y 7 del plan):
  · COMPLETO y sin ventana por fecha de documento: el SII permite recibir con retraso, así
    que un incremental por fecha pierde documentos en silencio (convergencia de 3 lentes).
    24 meses hacia atrás por decisión del dueño (unas 6 páginas de 250 por barrido).
  · Idempotente: N corridas = 1 corrida. Solo UPSERT por uuid; jamás DELETE — el documento
    que deja de venir pasa a DESAPARECIDO, con su decisión y su rastro intactos.
  · Los campos remotos se sobrescriben ciegamente (caché de una verdad ajena); si el
    contenido cambió DESPUÉS de una decisión local, se enciende `divergente` y lo mira un
    humano: el sistema NO repara solo.
  · ANTI-SOLAPE: dos barridos a la vez corrompen la marca de agua de DESAPARECIDO
    (hallazgo del auditor sobre el mark-and-sweep booleano). Guard: si hay un run sin
    terminar iniciado hace menos de RUN_TIMEOUT_MIN, el nuevo barrido se rechaza. La marca
    de agua es `ultimo_run_id` por documento, no un booleano global.

CINTURONES agregados tras la revisión adversarial 2026-08-06 (hallazgos confirmados):
  · PROPORCIÓN (#3): un barrido que dejaría DESAPARECIDO a más del 50% de los activos en
    ventana aborta con exito=False — la lista vacía bien formada ya no arrasa el espejo.
  · VENTANA (#15): el sweep solo marca DESAPARECIDO dentro de la ventana consultada
    (document_date >= desde) — envejecer no es desaparecer del SII.
  · FENCING (anti-solape residual): si el guard de 30 min expiró y un run más nuevo ya
    nació, el run viejo se retira SIN escribir (no hay heartbeat porque la bitácora no
    tiene updated_at y agregar columnas a tablas vivas es la trampa 1054 del checklist).
  · MALFORMADOS: un documento sin montos legibles se espeja con monto NULL y AVISO en la
    bitácora — un doc podrido ya no congela el barrido completo.

RED Y TRANSACCIONES: toda la red (el barrido del API) ocurre ANTES de abrir la transacción
de escritura — regla de la casa: jamás red adentro de una transacción con locks.
"""
import json
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from .client import WasabilComprasError, barrer_recibidos, monto_efectivo
from .models import CAMPOS_HASH, SiiLibroDoc, SiiLibroSyncRun, hash_contenido
from .rut import rut_canonico

logger = logging.getLogger("wasabil_compras")

# Ventana del barrido: 24 meses (decisión del dueño — cubre los períodos de gracia del
# crédito fiscal). Se calcula al correr, no se congela.
MESES_VENTANA = 24
# Un run "corriendo" más viejo que esto se considera MUERTO (proceso caído sin cerrar la
# bitácora) y deja de bloquear barridos nuevos.
RUN_TIMEOUT_MIN = 30
# Cinturón de PROPORCIÓN del sweep (hallazgo confirmado 2026-08-06: «barrido exitoso con
# lista vacía marca DESAPARECIDO el espejo COMPLETO»): si un barrido dejaría DESAPARECIDO
# a MÁS de esta fracción de los documentos activos en ventana, el barrido se declara
# sospechoso (regresión del API, filtro que cambió de semántica, lista vacía bien
# formada) y ABORTA con exito=False sin escribir — mismo espíritu fail-closed de la
# Regla 2: datos viejos honestos > frescos mentirosos. MAX_PAGINAS protege el exceso;
# este cinturón protege el DEFECTO.
MAX_PROPORCION_DESAPARECIDOS = 0.5


class BarridoEnCurso(Exception):
    """Ya hay un barrido corriendo: el nuevo se rechaza (anti-solape de la marca de agua)."""


def _parse_fecha(valor) -> Optional[date]:
    if not valor:
        return None
    try:
        return date.fromisoformat(str(valor)[:10])
    except ValueError:
        return None


def _run_en_curso(db: Session) -> Optional[SiiLibroSyncRun]:
    limite = datetime.utcnow() - timedelta(minutes=RUN_TIMEOUT_MIN)
    return (db.query(SiiLibroSyncRun)
            .filter(SiiLibroSyncRun.terminado_at.is_(None),
                    SiiLibroSyncRun.iniciado_at > limite)
            .first())


def _superado_por_otro_run(db: Session, run: SiiLibroSyncRun) -> Optional[SiiLibroSyncRun]:
    """¿Nació un run MÁS NUEVO mientras este barría? (hallazgo confirmado 2026-08-06:
    «el anti-solape deja de bloquear a los 30 min aunque el barrido siga VIVO: un run
    lento puede pisar con datos viejos los frescos de un run posterior»).

    Cierre elegido: FENCING al escribir, no heartbeat — sii_libro_sync_run NO tiene
    columna updated_at y agregarla exigiría un ALTER que create_all no hace (la trampa
    1054 que narra el propio checklist de deploy). El fencing cubre el daño real del
    hallazgo: si un run con id MAYOR ya existe (vivo o terminado), los datos de red de
    ESTE run son más viejos que los del otro → este run se retira SIN escribir. Se
    consulta dos veces: antes de abrir la escritura y justo antes del sweep."""
    return (db.query(SiiLibroSyncRun)
            .filter(SiiLibroSyncRun.id > run.id)
            .order_by(SiiLibroSyncRun.id)
            .first())


def _abortar_run(db: Session, run: SiiLibroSyncRun, origen: str, motivo: str) -> None:
    """Cierra el run como fallido SIN dejar nada escrito (rollback primero)."""
    db.rollback()
    run.terminado_at = datetime.utcnow()
    run.exito = False
    run.error = motivo[:2000]
    db.commit()
    logger.error("Barrido libro SII (%s) ABORTADO: %s", origen, motivo)


def ejecutar_barrido(db: Session, *, origen: str, usuario_id: Optional[int] = None) -> SiiLibroSyncRun:
    """Corre UN barrido completo y devuelve su bitácora (exito True/False, jamás a medias).

    El run se persiste ANTES de llamar a la red: si el proceso muere a mitad, queda la
    fila con terminado_at NULL — visible en el tablero y expirable por RUN_TIMEOUT_MIN.
    """
    if _run_en_curso(db):
        raise BarridoEnCurso(
            "Ya hay un barrido del libro corriendo (o uno murió hace menos de "
            f"{RUN_TIMEOUT_MIN} min). Espera a que termine o expire.")

    hoy = date.today()
    desde = hoy - timedelta(days=MESES_VENTANA * 31)
    run = SiiLibroSyncRun(origen=origen, usuario_id=usuario_id,
                          from_date=desde, to_date=hoy)
    db.add(run)
    db.commit()  # la bitácora nace visible ANTES de la red (diagnóstico + anti-solape)

    try:
        documentos = barrer_recibidos(from_date=desde.isoformat(),
                                      to_date=hoy.isoformat())
    except WasabilComprasError as e:
        run.terminado_at = datetime.utcnow()
        run.exito = False
        run.error = str(e)[:2000]
        db.commit()
        logger.error("Barrido libro SII (%s) ABORTADO: %s", origen, e)
        return run

    # ── Red terminada: recién ahora se escribe. Upsert por uuid, fila a fila. ──────
    # FENCING pre-escritura (ver _superado_por_otro_run): si el anti-solape expiró y un
    # run más nuevo ya nació, estos datos de red son VIEJOS y no deben pisar nada.
    otro = _superado_por_otro_run(db, run)
    if otro:
        _abortar_run(db, run, origen,
                     f"anti-solape: el run {otro.id} nació mientras este ({run.id}) "
                     "seguía barriendo; este run se retira SIN escribir para no pisar "
                     "datos más frescos con datos viejos")
        return run

    nuevos = actualizados = 0
    malformados: List[str] = []
    try:
        for doc in documentos:
            es_nuevo, aviso = _upsert_doc(db, doc, run.id)
            if es_nuevo:
                nuevos += 1
            else:
                actualizados += 1
            if aviso:
                malformados.append(aviso)

        # FLUSH EXPLÍCITO antes de contar y de barrer (bug latente destapado por la
        # sonda §11 del hallazgo #4): SessionLocal corre con autoflush=False, así que
        # sin esto el sweep y los conteos ven la FOTO PREVIA a los upserts — y como
        # asignar estado_espejo='ACTIVO' sobre un doc ya ACTIVO no ensucia la columna
        # en el ORM, el DESAPARECIDO del bulk UPDATE le quedaba pegado EN LA BD a
        # documentos que este mismo barrido SÍ vio (además de inflar el contador
        # `desaparecidos` de la bitácora). Con el flush, la marca de agua
        # ultimo_run_id=run.id ya está en la transacción cuando el sweep corre.
        db.flush()

        # FENCING pre-sweep: última verificación antes del paso destructivo.
        otro = _superado_por_otro_run(db, run)
        if otro:
            _abortar_run(db, run, origen,
                         f"anti-solape: el run {otro.id} nació mientras este ({run.id}) "
                         "escribía; este run se retira SIN escribir para no pisar "
                         "datos más frescos con datos viejos")
            return run

        # CINTURÓN DE PROPORCIÓN (hallazgo confirmado: la lista vacía bien formada
        # marcaba DESAPARECIDO el espejo COMPLETO con exito=True). Se cuenta SOLO
        # dentro de la ventana consultada (ver el filtro del sweep, abajo): fuera de
        # ella el barrido no testificó nada.
        activos_ventana = (db.query(func.count(SiiLibroDoc.id))
                           .filter(SiiLibroDoc.estado_espejo == "ACTIVO",
                                   SiiLibroDoc.document_date >= desde)
                           .scalar() or 0)
        por_desaparecer = (db.query(func.count(SiiLibroDoc.id))
                           .filter(SiiLibroDoc.estado_espejo == "ACTIVO",
                                   SiiLibroDoc.document_date >= desde,
                                   (SiiLibroDoc.ultimo_run_id.is_(None))
                                   | (SiiLibroDoc.ultimo_run_id < run.id))
                           .scalar() or 0)
        if (activos_ventana and por_desaparecer
                and por_desaparecer / activos_ventana > MAX_PROPORCION_DESAPARECIDOS):
            _abortar_run(
                db, run, origen,
                f"cinturón de proporción: el API devolvió {len(documentos)} docs y este "
                f"barrido dejaría DESAPARECIDO a {por_desaparecer} de {activos_ventana} "
                f"activos en ventana (>{MAX_PROPORCION_DESAPARECIDOS:.0%}): sospechoso "
                "de regresión del API (lista vacía / filtro cambiado) — no se concluye "
                "y el espejo queda como estaba. Si la caída fuera real, verificar en "
                "Wasabil antes de tocar nada")
            return run

        # DESAPARECIDOS: activos EN VENTANA que este barrido COMPLETO no vio. La
        # condición `ultimo_run_id < run.id` (y no !=) tolera runs viejos muertos sin
        # cerrar. `document_date >= desde` (hallazgo confirmado 2026-08-06: «la ventana
        # deslizante de 24 meses fabrica DESAPARECIDOS falsos») — DECISIÓN: exclusión
        # por fecha, NO un estado nuevo: un documento más viejo que la ventana dejó de
        # CONSULTARSE, no de existir ante el SII; queda ACTIVO con su decisión y su
        # cubeta como siempre estuvo (agregar un estado FUERA_DE_VENTANA movería docs
        # de la bandeja sin que nada haya cambiado en el mundo). Un doc con fecha NULL
        # tampoco se marca: no se puede probar que la ventana lo cubría — DESAPARECIDO
        # es una acusación seria y acá se falla cerrado.
        desaparecidos = (db.query(SiiLibroDoc)
                         .filter(SiiLibroDoc.estado_espejo == "ACTIVO",
                                 SiiLibroDoc.document_date >= desde,
                                 (SiiLibroDoc.ultimo_run_id.is_(None))
                                 | (SiiLibroDoc.ultimo_run_id < run.id))
                         .update({"estado_espejo": "DESAPARECIDO"},
                                 synchronize_session=False))
        run.terminado_at = datetime.utcnow()
        run.exito = True
        run.total_api = len(documentos)
        run.nuevos, run.actualizados, run.desaparecidos = nuevos, actualizados, desaparecidos
        # Documentos malformados (hallazgo BAJO confirmado: «un solo documento malformado
        # congela el barrido COMPLETO todas las noches»): se ESPEJAN con monto NULL (ver
        # _upsert_doc) y el run queda exitoso CON AVISO en la bitácora — abortar todo por
        # un doc podrido dejaría el espejo viejo para siempre, que es peor que la duda
        # declarada. El aviso viaja en `error` (visible en el tablero) con exito=True.
        run.error = (f"AVISO: {len(malformados)} documento(s) malformado(s) espejado(s) "
                     f"con monto NULL — {'; '.join(malformados)}"[:2000]
                     if malformados else None)
        db.commit()
        logger.info("Barrido libro SII (%s) OK: %s docs (%s nuevos, %s act., %s desap., "
                    "%s malformados)", origen, len(documentos), nuevos, actualizados,
                    desaparecidos, len(malformados))
    except Exception as e:  # noqa: BLE001 — la bitácora SIEMPRE se cierra
        db.rollback()
        run.terminado_at = datetime.utcnow()
        run.exito = False
        run.error = f"escritura del espejo: {type(e).__name__}: {e}"[:2000]
        db.commit()
        logger.exception("Barrido libro SII (%s): fallo escribiendo el espejo", origen)
    return run


def _difiere(remoto, local) -> bool:
    """Comparación de UN campo remoto vs su columna del espejo, tolerante a TIPOS.

    Hallazgo BAJO confirmado 2026-08-06: la narrativa de divergencia comparaba
    str(Decimal('500000.00')) vs str(500000) y listaba TODOS los montos como cambiados
    (falsos positivos que entierran el campo que SÍ cambió). Regla: si ambos lados se
    pueden leer como número, se comparan como número; si no, como texto."""
    if remoto is None or local is None:
        return (remoto is None) != (local is None)
    try:
        return Decimal(str(remoto)) != Decimal(str(local))
    except InvalidOperation:
        return str(remoto) != str(local)


def _upsert_doc(db: Session, doc: dict, run_id: int) -> Tuple[bool, Optional[str]]:
    """Upsert de UN documento por uuid. Devuelve (es_nuevo, aviso_malformado|None).
    Nunca pisa la decisión local."""
    fila = db.query(SiiLibroDoc).filter(SiiLibroDoc.uuid == doc["uuid"]).first()
    hash_nuevo = hash_contenido(doc)
    es_nuevo = fila is None
    if es_nuevo:
        fila = SiiLibroDoc(uuid=doc["uuid"])
        db.add(fila)
    elif fila.hash_remoto != hash_nuevo and fila.decision is not None \
            and not fila.divergente:
        # Cambió DESPUÉS de decidido: se enciende la alarma con el detalle de QUÉ cambió
        # (contra la lista explícita), y la decisión se conserva — la mira un humano.
        cambios = [k for k in CAMPOS_HASH
                   if _difiere(_valor_remoto(doc, k), getattr(fila, _col_de(k), None))]
        fila.divergente = True
        fila.divergencia_detalle = (
            f"El documento cambió en Wasabil después de la decisión "
            f"({fila.decision} → {fila.destino or '-'}): campos {', '.join(cambios) or '?'}"
        )[:500]

    # Campos remotos: sobrescritura ciega (Regla 6).
    fila.document = doc.get("document")
    fila.sii_document_type_id = doc.get("sii_document_type_id")
    fila.folio = str(doc.get("folio")) if doc.get("folio") is not None else None
    fila.document_date = _parse_fecha(doc.get("document_date"))
    fila.status_id = doc.get("status_id")
    fila.trx_sign = doc.get("trx_sign")
    fila.sent_nsubtotal = doc.get("sent_nsubtotal")
    fila.sent_niva = doc.get("sent_niva")
    fila.sent_nexempt = doc.get("sent_nexempt")
    fila.sent_ntotal = doc.get("sent_ntotal")
    # Hallazgo BAJO confirmado 2026-08-06 («un solo documento malformado congela el
    # barrido COMPLETO todas las noches»): si la magnitud sumable no se puede derivar
    # (sent_ntotal/trx_sign ilegibles), el documento se ESPEJA igual con monto NULL —
    # la marca es el NULL mismo: monto_efectivo es NOT-NULL de facto para todo doc sano
    # — y el barrido sigue. El aviso sube a la bitácora del run (fail-closed matizado:
    # abortar TODO por un doc podrido dejaría el espejo viejo para siempre, y la Regla 9
    # sigue intacta — un monto NULL no suma, jamás suma mal).
    aviso = None
    try:
        fila.monto_efectivo = monto_efectivo(doc)
    except WasabilComprasError as e:
        fila.monto_efectivo = None
        aviso = f"uuid={doc.get('uuid')}: {e}"
        logger.warning("Barrido libro SII: documento malformado espejado con monto "
                       "NULL — %s", aviso)
    fila.receiver_rut_original = doc.get("receiver_rut")
    fila.rut_emisor_canonico = rut_canonico(doc.get("receiver_rut"))
    fila.receiver_name = doc.get("receiver_name")
    fila.supplier_uid = (doc.get("supplier") or {}).get("uid") if doc.get("supplier") else None
    fila.exchange_status = doc.get("exchange_status")
    fila.payment_method = doc.get("payment_method")
    fila.hash_remoto = hash_nuevo
    fila.estado_espejo = "ACTIVO"       # si estaba DESAPARECIDO y volvió, revive
    fila.ultimo_run_id = run_id
    fila.raw_json = json.dumps(doc, ensure_ascii=False, default=str)[:60000]
    return es_nuevo, aviso


def _col_de(campo_remoto: str) -> str:
    """Campo remoto → columna del espejo (para narrar la divergencia)."""
    return {
        "receiver_rut": "receiver_rut_original",
    }.get(campo_remoto, campo_remoto)


def _valor_remoto(doc: dict, campo: str):
    if campo == "document_date":
        return _parse_fecha(doc.get(campo))
    return doc.get(campo)


def barrido_nocturno() -> None:
    """Punto de enganche del scheduler — job PROPIO `run_sii_libro_job`, 05:30 Santiago
    (se separó del job de alertas de las 06:00: dos trabajos sin nada en común, ver el
    docstring de scheduler.run_sii_libro_job). Sesión PROPIA y errores PROPIOS: un fallo
    del libro jamás deja a Grupo AM sin el resto de las alertas del día (invariantes del
    job, ver scheduler.py)."""
    from database import SessionLocal
    from .client import esta_configurado
    if not esta_configurado():
        logger.info("Barrido libro SII: Wasabil sin token — se omite (no es un error)")
        return
    db = SessionLocal()
    try:
        ejecutar_barrido(db, origen="nocturno")
    except BarridoEnCurso as e:
        logger.warning("Barrido libro SII nocturno: %s", e)
    finally:
        db.close()
