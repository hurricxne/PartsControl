"""El MOTOR del matcher banco ↔ libro SII ↔ egresos — MonzaParts.

Espejo DELIBERADO de wasabil_compras/matcher.py (GA): cero imports de wasabil_compras/
(regla de la casa — duplicación deliberada entre marcas). Implementa la spec
docs/spec-matcher-banco-libro-2026-08-06.md. PRINCIPIO RECTOR: un match falso
AUTOMÁTICO es peor que cien sugerencias — AUTO es una CONJUNCIÓN de condiciones duras
con identidad de DOCUMENTO; el score numérico SOLO ordena sugeridos. Ante la duda:
degradar de AUTO a SUGERIDO, y de SUGERIDO a nada.

CINCO VÍAS por la puerta de monto (SIEMPRE sobre SALDOS, jamás totales):
  V1 directa (única que puede llegar a AUTO) · V2 subset firmado mismo RUT ·
  V3 parcial con identidad · V4 agregado mensual de comisiones · V5 honorarios.
DOS vías de AUTO, ambas re-validadas bajo SELECT…FOR UPDATE del movimiento:
  A) cruce triple por DOCUMENTO (mov→conciliación→egreso→detalle→compra→rut+folio→doc
     con cobertura de saldo)  B) V1 + RUT válido en glosa + unicidad bilateral e
  histórica + ventana + cero techos (y la señal RUT nace APAGADA por configuración).

REGLAS DE CONVIVENCIA (spec §5): el motor se ABSTIENE si hay barrido del espejo vivo
(_run_en_curso — el barrido es UNA transacción larga sin retry: un deadlock 1213 le
pierde la noche) y JAMÁS toma FOR UPDATE sobre monza_sii_libro_doc — el tope por doc
se serializa con el lock del MOVIMIENTO + re-lectura fresca, y la coherencia de
contenido la da el snapshot de hash. Idempotente: N corridas = 1 corrida; jamás revive
descartados ni pisa confirmados humanos; sus propios autos se RE-VERIFICAN en cada
corrida y se degradan a en_duda con motivo.

ADAPTACIONES Monza (vs el molde GA; todo lo demás, espejo fiel):
  · Lado banco: monza_tesoreria (MonzaTesMovimiento/MonzaTesCuentaBancaria) y lado
    egresos: monza_compras_contab (MonzaContEgreso/…) — SIN columna `empresa` (la
    separación entre marcas en Monza es POR TABLA): se eliminan todos los filtros
    empresa == 'mineria' del molde.
  · monza_tes_conciliacion es XOR egreso/adelanto (en GA los adelantos viven en
    conc_conciliacion_ingreso): los enlaces a EGRESO son la tercera pata; los enlaces
    a ADELANTO son conciliaciones de INGRESO (van a ingreso_movs, regla 14).
  · El adelanto con estado NULL se lee 'aprobado' (semántica de monza_contabilidad,
    service.estado_de_adelanto): acá se incluye en el pool de exclusión NC↔abono.
  · Intercompañía por defecto: 77977813-4 (GRUPO AM visto DESDE Monza — el espejo
    del 78121316-0 que GA declara para Monza).

Imports de monza_tesoreria/monza_compras_contab/monza_contabilidad LOCALES dentro de
funciones (mismo patrón que el router hermano): este paquete no se acopla a esos
módulos al arranque. Aritmética de comparación SIEMPRE en pesos ENTEROS (round) —
regla 8 de la spec.
"""
import json
import logging
import uuid as uuid_mod
from datetime import date, datetime, timedelta
from itertools import combinations
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from .glosa import (
    clasificar_movimiento, extraer_numero_operacion, extraer_ruts_de_glosa,
    fuerza_nombre, nombre_de_factor,
)
from .models import (
    MonzaSiiLibroDoc, MonzaSiiLibroMatch, MonzaSiiLibroReglaRut, MonzaSiiMatchConfig,
    MonzaSiiMatchEtiquetaMov, MonzaSiiMatchRun,
)
from .rut import rut_canonico
from .sync import _run_en_curso

logger = logging.getLogger("monza_wasabil_compras")

# ── Tolerancias y ventanas (spec §1, §4 y regla 12) ─────────────────────────────
TOL_PESO = 1                    # tolerancia general de montos, en pesos ENTEROS
TOL_COMISION = 10               # agregado mensual: redondeos de IVA (regla 19)
VENTANA_SUG_ANTES = 45          # sugerido: doc−45d ≤ mov (asimétrica)
VENTANA_SUG_DESPUES = 120       # sugerido: mov ≤ doc+120d
VENTANA_AUTO = 90               # auto: doc ≤ mov ≤ doc+90d
VENTANA_AUTO_CHEQUE_EXTRA = 60  # +60d si el egreso conciliado es cheque
MESES_UNICIDAD_HISTORICA = 12   # S6/N3: docs abiertos + pagados de 12 meses

# ── Subset-sum (regla 8): límites duros contra la explosión combinatoria ────────
SUBSET_MAX_DOCS = 4
SUBSET_MAX_POR_RUT = 25
PRESUPUESTO_COMBINACIONES = 200_000   # por corrida; al agotarse, V2 deja de generar

# ── Lote de egresos (regla 16) ──────────────────────────────────────────────────
LOTE_MIN, LOTE_MAX = 2, 6
LOTE_MAX_EGRESOS_DIA = 12

# ── Pesos de señales (spec §2). El score es cache de pantalla: JAMÁS autoriza. ──
PESO_MONTO_EXACTO = 40          # V1: |residual − saldo| ≤ 1 (la base del sugerido)
PESO_S2_RUT_GLOSA = 45
PESO_S3_FUERTE = 40
PESO_S3_MEDIA = 20
PESO_S4_OPERACION = 25
PESO_S5_FECHA_MAX = 15
PESO_S6_UNICO = 15
PESO_FOLIO_BLANDO = 30
PESO_N1 = -30
SCORE_MAX_SUGERIDO = 99         # 100 queda reservado para la conjunción AUTO

# Techos que vetan la vía A (cruce triple). N3-recurrente, segunda cuota y factor NO
# están acá a propósito: la spec los exime cuando hay identidad documental («ese par
# solo llega a AUTO por S1»). La vía B la veta CUALQUIER techo.
VIA_A_VETOS = {"bloqueado", "relacionado", "honorarios", "duplicada", "anticipo"}

MEDIO_ADELANTO = "adelanto"     # mismo literal que monza_contabilidad (la aplicación
                                # automática del adelanto verificado usa medio='adelanto')

# ── Configuración mantenible sin redeploy (monza_sii_match_config pisa esto) ────
CONFIG_DEFAULTS = {
    # La señal RUT-en-glosa nace APAGADA: se enciende solo tras la sonda de ingestión
    # con ≥3 cartolas reales que traigan RUT (la muestra real del dueño NO lo trae).
    "rut_en_glosa_activo": False,
    # Tabla anual de retención de honorarios (V5). La tasa del encargo era la de 2024.
    "retencion_honorarios": {"2024": 13.75, "2025": 14.5, "2026": 15.25, "2027": 16.0},
    # Intercompañía (mandato del controller §3.2): techo sugerido aunque TODO calce.
    # GRUPO AM visto DESDE Monza — el espejo del 78121316-0 que GA declara para Monza.
    "ruts_relacionados": ["77977813-4"],
    # RUT del banco emisor de las comisiones (V4): Santander.
    "ruts_banco": ["97036000-K"],
    "factors_extra": [],
    "blocklist_extra": [],
    "nomina_extra": [],
    "comision_extra": [],
}


class MatcherEnCurso(Exception):
    """Hay un barrido del espejo (o otra corrida del matcher) vivo: el motor se
    abstiene — no compite por locks con la transacción larga del barrido."""


# ═══ Utilidades ══════════════════════════════════════════════════════════════════

def _p(x) -> int:
    """Pesos ENTEROS para comparar (round): toda la aritmética de la puerta de monto
    vive en enteros — un float acumula errores justo en el ±1 que decide."""
    return int(round(float(x or 0)))


def comparar_lado(tipo_mov: str, trx_sign) -> bool:
    """Regla 21 — la función CENTRAL de compatibilidad de lados. Un cargo solo puede
    explicar un documento positivo (+1) y un abono solo una NC (−1); la MAGNITUD se
    compara aparte (mov.monto siempre positivo; del doc se usa |saldo|). Jamás se
    mezcla magnitud con signo."""
    return (tipo_mov == "cargo" and trx_sign == 1) or \
           (tipo_mov == "abono" and trx_sign == -1)


def score_fecha(delta_dias: int) -> int:
    """S5: la fecha es score DECRECIENTE, jamás filtro binario dentro de la ventana.
    Máximo en [doc, doc+45], cayendo linealmente hacia ambos bordes de la ventana de
    sugerencia (doc−45 / doc+120)."""
    if 0 <= delta_dias <= 45:
        return PESO_S5_FECHA_MAX
    if delta_dias < 0:
        return max(0, round(PESO_S5_FECHA_MAX * (1 - (-delta_dias) / VENTANA_SUG_ANTES)))
    return max(0, round(PESO_S5_FECHA_MAX
                        * (1 - (delta_dias - 45) / (VENTANA_SUG_DESPUES - 45))))


def _folio_estricto(x) -> str:
    """La MISMA igualdad post-strip de _llaves_erp (router.py): la única que ELEVA."""
    return str(x).strip() if x is not None else ""


def _folio_blando(x) -> str:
    """Solo dígitos, sin ceros a la izquierda ('F-1234'→'1234'). SOLO suma score hacia
    sugerido: factura y NC del mismo proveedor comparten numeración por tipo y la
    variante blanda no las distingue (regla 9 de normalización)."""
    digitos = "".join(c for c in str(x or "") if c.isdigit())
    return digitos.lstrip("0")


def _rut_canon_sucio(x) -> Optional[str]:
    """rut_canonico tolerante al ESPACIO donde iba el guión ('96.631.520 2'): el dato
    del ERP es texto libre y la tercera pata debe canonizar lo canonizable — un RUT
    sucio que se rinde deja pasar la contradicción (sonda de la regla 7)."""
    canon = rut_canonico(x)
    if canon is None and x is not None and " " in str(x).strip():
        canon = rut_canonico(str(x).strip().replace(" ", "-"))
    return canon


def _clave_mov_de(mov) -> list:
    """Clave de identidad 'suave' del movimiento — CALCA los 6 campos de _clave_mov
    (monza_tesoreria/router.py): con ella los descartes sobreviven al re-import de
    la cartola (regla 5)."""
    return [str(mov.fecha or ""), mov.tipo, round(float(mov.monto or 0), 2),
            (mov.referencia or "").strip(), (mov.glosa or "").strip(),
            round(float(mov.saldo), 2) if mov.saldo is not None else None]


def _con_retry_deadlock(db: Session, operacion):
    """Reintento ante deadlock/lock-timeout InnoDB (1213/1205) — CALCA el helper de
    monza_tesoreria/router.py (que a la tercera responde 409): acá se reintenta la
    transacción COMPLETA tras rollback y a la tercera se desiste DEVOLVIENDO None al
    caller, que degrada el AUTO a sugerido — perder un auto es barato, perder la
    corrida no."""
    from sqlalchemy.exc import OperationalError
    for _ in range(3):
        try:
            return operacion()
        except OperationalError as e:
            db.rollback()
            args = getattr(getattr(e, "orig", None), "args", None) or []
            if not args or args[0] not in (1213, 1205):
                raise
    return None


def cargar_config(db: Session) -> dict:
    """Defaults del código pisados por las filas de monza_sii_match_config (JSON por
    clave): cambiar la lista de factors o encender rut_en_glosa NO exige deploy."""
    cfg = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
           for k, v in CONFIG_DEFAULTS.items()}
    for fila in db.query(MonzaSiiMatchConfig).all():
        try:
            cfg[fila.clave] = json.loads(fila.valor)
        except (ValueError, TypeError):
            logger.warning("monza_sii_match_config['%s']: JSON inválido, se ignora",
                           fila.clave)
    return cfg


def _campos_doc(doc: MonzaSiiLibroDoc) -> dict:
    """Snapshot NOMBRADO de los campos del hash (CAMPOS_HASH) al momento del match:
    cuando el barrido pise el doc, la re-verificación puede decir QUÉ campo cambió
    (el hash solo dice 'algo cambió')."""
    return {
        "folio": doc.folio, "document_date": str(doc.document_date or ""),
        "status_id": doc.status_id, "trx_sign": doc.trx_sign,
        "sent_nsubtotal": str(doc.sent_nsubtotal), "sent_niva": str(doc.sent_niva),
        "sent_nexempt": str(doc.sent_nexempt), "sent_ntotal": str(doc.sent_ntotal),
        "receiver_rut": doc.receiver_rut_original, "receiver_name": doc.receiver_name,
        "exchange_status": doc.exchange_status,
        "sii_document_type_id": doc.sii_document_type_id,
    }


def docs_no_bancarios(db: Session, llaves: Optional[Dict[tuple, list]] = None) -> set:
    """Regla 17: ids de docs del espejo cuya compra del ERP (mismo RUT canónico y N°
    de documento ESTRICTO) está PAGADA 100% por egresos medio efectivo/tarjeta. Esos
    documentos jamás son candidatos de un movimiento bancario (el cargo del estado de
    cuenta de la tarjeta se explica por la conciliación interna con el egreso
    tarjeta). DERIVADA por query — una columna se desincronizaría. La usa el motor
    (pool) y el tablero (contador propio).

    `llaves` opcional: {(rut, folio): [doc_ids]} precalculado; si no viene, se arma
    acá desde los docs ACTIVOS."""
    from monza_compras_contab.models import (MonzaContCompra, MonzaContEgreso,
                                             MonzaContEgresoDetalle)
    if llaves is None:
        llaves = {}
        for d in (db.query(MonzaSiiLibroDoc)
                  .filter(MonzaSiiLibroDoc.estado_espejo == "ACTIVO",
                          MonzaSiiLibroDoc.rut_emisor_canonico.isnot(None),
                          MonzaSiiLibroDoc.folio.isnot(None)).all()):
            folio = _folio_estricto(d.folio)
            if folio:
                llaves.setdefault((d.rut_emisor_canonico, folio), []).append(d.id)
    if not llaves:
        return set()
    compras = (db.query(MonzaContCompra)
               .filter(MonzaContCompra.anulado.is_(False),
                       MonzaContCompra.estado_pago == "pagado").all())
    pagadas = {}
    for c in compras:
        canon = _rut_canon_sucio(c.proveedor_rut)
        folio = _folio_estricto(c.numero_documento)
        if canon and folio and (canon, folio) in llaves:
            pagadas[(canon, folio)] = c.id
    if not pagadas:
        return set()
    medios = (db.query(MonzaContEgresoDetalle.compra_id, MonzaContEgreso.medio)
              .join(MonzaContEgreso,
                    MonzaContEgreso.id == MonzaContEgresoDetalle.egreso_id)
              .filter(MonzaContEgresoDetalle.compra_id.in_(list(pagadas.values())))
              .all())
    medios_por_compra: Dict[int, set] = {}
    for cid, medio in medios:
        medios_por_compra.setdefault(cid, set()).add(medio or "")
    fuera = set()
    for llave, cid in pagadas.items():
        ms = medios_por_compra.get(cid, set())
        if ms and ms <= {"efectivo", "tarjeta"}:
            fuera.update(llaves[llave])
    return fuera


# ═══ Contexto de la corrida (todo se lee UNA vez, sin locks) ═════════════════════

class _Contexto:
    """Foto de trabajo de la corrida. Sin locks: la escritura que decide (autos y
    topes) re-valida TODO fresco bajo el FOR UPDATE del movimiento."""

    def __init__(self):
        self.cfg: dict = {}
        self.movs_normales: list = []      # sin etiqueta: todas las vías
        self.movs_nomina: list = []        # solo cruce triple + lote (regla 16)
        self.movs_comision: list = []      # solo agregado mensual (regla 19)
        self.docs_pool: Dict[int, MonzaSiiLibroDoc] = {}
        self.saldo_doc: Dict[int, int] = {}
        self.docs_cubiertos: Dict[int, MonzaSiiLibroDoc] = {}  # saldo 0 → N2 doble pago
        self.no_bancario_ids: set = set()
        self.residual_mov: Dict[int, int] = {}
        self.by_par: Dict[tuple, MonzaSiiLibroMatch] = {}
        self.matches: List[MonzaSiiLibroMatch] = []
        self.vivos_por_doc: Dict[int, set] = {}   # doc_id → {mov_id} sugerido/en_duda
        self.conc_por_mov: Dict[int, list] = {}   # mov_id → [{egreso…, detalles:[…]}]
        self.ingreso_movs: set = set()
        self.egresos_por_op: Dict[str, list] = {}
        self.hist_rut_monto: Dict[tuple, int] = {}
        self.nc_vigentes: set = set()             # (rut, monto) con NC abierta
        self.reglas_rut: Dict[str, MonzaSiiLibroReglaRut] = {}
        self.montos_cobranza_adelanto: List[int] = []
        self.presupuesto = PRESUPUESTO_COMBINACIONES

    def residual(self, mov) -> int:
        return self.residual_mov.get(mov.id, _p(mov.monto))


def _construir_contexto(db: Session, cfg: dict) -> _Contexto:
    from monza_compras_contab.models import (MonzaContCompra, MonzaContEgreso,
                                             MonzaContEgresoDetalle)
    from monza_tesoreria.models import (MonzaTesConciliacion,
                                        MonzaTesConciliacionIngreso,
                                        MonzaTesCuentaBancaria, MonzaTesMovimiento)

    ctx = _Contexto()
    ctx.cfg = cfg
    ctx.reglas_rut = {r.rut_canonico: r for r in db.query(MonzaSiiLibroReglaRut).all()}

    # ── Matches existentes: el estado previo manda sobre la corrida ──────────────
    ctx.matches = db.query(MonzaSiiLibroMatch).all()
    for m in ctx.matches:
        if m.movimiento_id is not None:
            ctx.by_par[(m.movimiento_id, m.doc_id)] = m
        if m.estado in ("sugerido", "en_duda") and m.movimiento_id is not None:
            ctx.vivos_por_doc.setdefault(m.doc_id, set()).add(m.movimiento_id)
    confirm_doc: Dict[int, int] = {}
    confirm_mov: Dict[int, int] = {}
    for m in ctx.matches:
        if m.estado == "confirmado":
            confirm_doc[m.doc_id] = confirm_doc.get(m.doc_id, 0) + abs(_p(m.monto_asignado))
            if m.movimiento_id is not None:
                confirm_mov[m.movimiento_id] = (confirm_mov.get(m.movimiento_id, 0)
                                                + abs(_p(m.monto_asignado)))

    # ── Documentos: ACTIVOS, con saldo sobre CONFIRMADOS (regla 1) ───────────────
    docs = (db.query(MonzaSiiLibroDoc)
            .filter(MonzaSiiLibroDoc.estado_espejo == "ACTIVO",
                    MonzaSiiLibroDoc.monto_efectivo.isnot(None)).all())
    hoy = date.today()
    hace_12m = hoy - timedelta(days=MESES_UNICIDAD_HISTORICA * 31)
    llaves_nb = {}
    for d in docs:
        saldo = abs(_p(d.monto_efectivo)) - confirm_doc.get(d.id, 0)
        # Unicidad histórica S6/N3: abiertos + pagados de 12 meses, por (rut, |monto|).
        if d.rut_emisor_canonico and (saldo > 0 or (d.document_date or hoy) >= hace_12m):
            k = (d.rut_emisor_canonico, abs(_p(d.monto_efectivo)))
            ctx.hist_rut_monto[k] = ctx.hist_rut_monto.get(k, 0) + 1
            if d.trx_sign == -1 and saldo > 0:
                ctx.nc_vigentes.add(k)
        if saldo <= 0:
            ctx.docs_cubiertos[d.id] = d
            continue
        regla = ctx.reglas_rut.get(d.rut_emisor_canonico)
        if regla and regla.nivel == "IGNORAR_AUTO":
            continue                       # fuera del pool (regla 15)
        ctx.docs_pool[d.id] = d
        ctx.saldo_doc[d.id] = saldo
        folio = _folio_estricto(d.folio)
        if d.rut_emisor_canonico and folio:
            llaves_nb.setdefault((d.rut_emisor_canonico, folio), []).append(d.id)

    # ── Cubeta NO_BANCARIO (regla 17, DERIVADA por query, no columna): compras
    #    saldadas 100% por egresos efectivo/tarjeta → su doc sale del pool ─────────
    ctx.no_bancario_ids = docs_no_bancarios(db, llaves_nb)
    for did in ctx.no_bancario_ids:
        ctx.docs_pool.pop(did, None)
        ctx.saldo_doc.pop(did, None)

    # ── Movimientos: solo cuentas CLP (mismo criterio que _solo_cuentas_clp de
    #    monza_tesoreria/router.py — en otra moneda los montos no son comparables),
    #    etiquetados los sin-contraparte y con residual sobre CONFIRMADOS ───────────
    movs = (db.query(MonzaTesMovimiento)
            .join(MonzaTesCuentaBancaria,
                  MonzaTesCuentaBancaria.id == MonzaTesMovimiento.cuenta_id)
            .filter(MonzaTesMovimiento.tipo.in_(["cargo", "abono"]))
            .all())
    movs = [m for m in movs
            if (getattr(m.cuenta, "moneda", None) or "CLP") == "CLP"]
    etiquetas = {e.movimiento_id: e for e in db.query(MonzaSiiMatchEtiquetaMov).all()}
    nuevos = 0
    for m in movs:
        ctx.residual_mov[m.id] = _p(m.monto) - confirm_mov.get(m.id, 0)
        et = etiquetas.get(m.id)
        if et is None:
            clase = clasificar_movimiento(
                m.glosa, tuple(cfg.get("blocklist_extra", ())),
                tuple(cfg.get("nomina_extra", ())), tuple(cfg.get("comision_extra", ())))
            if clase:
                et = MonzaSiiMatchEtiquetaMov(
                    movimiento_id=m.id, etiqueta=clase[0],
                    detalle=f"keyword '{clase[1]}' en la glosa — "
                            "sin contraparte SII esperada" if clase[0] == "blocklist_sin_sii"
                            else f"keyword '{clase[1]}' en la glosa")
                db.add(et)
                etiquetas[m.id] = et
                nuevos += 1
        if et is not None and not et.revertida:
            if et.etiqueta in ("nomina", "lote_pendiente"):
                ctx.movs_nomina.append(m)      # solo tercera pata + lote (regla 16)
            elif et.etiqueta == "comision_banco":
                ctx.movs_comision.append(m)    # solo agregado mensual (regla 19)
            continue                           # blocklist: fuera de todo (regla 18)
        if ctx.residual_mov[m.id] >= 1:
            ctx.movs_normales.append(m)
    if nuevos:
        db.commit()   # las etiquetas son REVERSIBLES por el operador; se persisten ya

    # ── Conciliación interna (la tercera pata): mov → egreso → detalle → compra.
    #    ADAPTACIÓN Monza: monza_tes_conciliacion es XOR egreso/adelanto — el enlace a
    #    ADELANTO es una conciliación de INGRESO (en GA vive en conc_conciliacion_
    #    ingreso) y va a ingreso_movs, no a la tercera pata ────────────────────────
    mov_ids = [m.id for m in movs]
    if mov_ids:
        concs = (db.query(MonzaTesConciliacion)
                 .filter(MonzaTesConciliacion.movimiento_id.in_(mov_ids)).all())
        egreso_ids = list({c.egreso_id for c in concs if c.egreso_id is not None})
        egresos = {e.id: e for e in db.query(MonzaContEgreso)
                   .filter(MonzaContEgreso.id.in_(egreso_ids)).all()} if egreso_ids else {}
        detalles: Dict[int, list] = {}
        if egreso_ids:
            for det, compra in (db.query(MonzaContEgresoDetalle, MonzaContCompra)
                                .join(MonzaContCompra,
                                      MonzaContCompra.id == MonzaContEgresoDetalle.compra_id)
                                .filter(MonzaContEgresoDetalle.egreso_id.in_(egreso_ids)).all()):
                detalles.setdefault(det.egreso_id, []).append((det, compra))
        for c in concs:
            if c.egreso_id is None:
                # abono ↔ adelanto: dominio de ingresos (regla 14), no tercera pata.
                ctx.ingreso_movs.add(c.movimiento_id)
                continue
            e = egresos.get(c.egreso_id)
            if e is None:
                continue
            ctx.conc_por_mov.setdefault(c.movimiento_id, []).append(
                {"egreso": e, "detalles": detalles.get(e.id, [])})
        ctx.ingreso_movs |= {
            r[0] for r in db.query(MonzaTesConciliacionIngreso.movimiento_id)
            .filter(MonzaTesConciliacionIngreso.movimiento_id.in_(mov_ids)).all()}

    # ── Egresos por N° de operación (S4, regla 20): TEF vía regex de glosa; cheques
    #    vía referencia. Un N° '0'/corto es el parser fusionando campos: cero señal ──
    egresos_op = (db.query(MonzaContEgreso)
                  .filter(MonzaContEgreso.numero_operacion.isnot(None)).all())
    for e in egresos_op:
        num = str(e.numero_operacion or "").strip().lstrip("0")
        if len(num) >= 3:
            ctx.egresos_por_op.setdefault(num, []).append(e)

    # ── Prioridad de dominios NC↔abono (regla 14): cobranzas pendientes y adelantos
    #    aprobados sin conciliar — mismas queries que el router de Tesorería Monza ──
    from sqlalchemy import or_

    from monza_contabilidad.models import (MonzaContAdelanto, MonzaContCobranza,
                                           MonzaContFacturaCliente)
    pares = (db.query(MonzaContCobranza.id, MonzaContCobranza.monto)
             .join(MonzaContFacturaCliente,
                   MonzaContFacturaCliente.id == MonzaContCobranza.factura_id)
             .filter(MonzaContCobranza.medio != MEDIO_ADELANTO).all())
    ya = {r[0] for r in db.query(MonzaTesConciliacionIngreso.cobranza_id)
          .filter(MonzaTesConciliacionIngreso.cobranza_id.isnot(None)).all()}
    ctx.montos_cobranza_adelanto = [_p(m) for cid, m in pares if cid not in ya]
    # estado NULL se lee 'aprobado' (ventas anteriores a la columna — semántica de
    # monza_contabilidad.service.estado_de_adelanto): acá también cuenta, porque más
    # exclusión = menos sugerencias NC basura sobre abonos que son plata de clientes.
    adels = (db.query(MonzaContAdelanto.id, MonzaContAdelanto.monto)
             .filter(or_(MonzaContAdelanto.estado == "aprobado",
                         MonzaContAdelanto.estado.is_(None))).all())
    # El enlace del adelanto vive en monza_tes_conciliacion.adelanto_id (no en la
    # tabla de ingresos como GA).
    ya_a = {r[0] for r in db.query(MonzaTesConciliacion.adelanto_id)
            .filter(MonzaTesConciliacion.adelanto_id.isnot(None)).all()}
    ctx.montos_cobranza_adelanto += [_p(m) for aid, m in adels if aid not in ya_a]
    return ctx


# ═══ Señales por par (mov, doc) ═══════════════════════════════════════════════════

def _grupos_candidatos(ctx: _Contexto, objetivo: int, trx_sign: int) -> List[list]:
    """Candidatos V1 colapsados por llave de negocio (regla 13: el índice rut/tipo/
    folio del espejo es NO único por diseño — dos filas con la misma llave y monto son
    UN candidato para la unicidad, no dos). Sin ventana: la unicidad se rompe también
    con docs fuera de ventana o con campos NULL (regla 12)."""
    grupos: Dict[tuple, list] = {}
    for did, d in ctx.docs_pool.items():
        if d.trx_sign != trx_sign:
            continue
        if abs(ctx.saldo_doc[did] - objetivo) > TOL_PESO:
            continue
        llave = (d.rut_emisor_canonico, d.sii_document_type_id,
                 _folio_estricto(d.folio), str(d.monto_efectivo))
        # Docs con campos NULL: llave por uuid (jamás se colapsan entre sí, y SIEMPRE
        # cuentan para romper unicidad aunque no califiquen para match).
        if not llave[0] or not llave[2]:
            llave = ("uuid", d.uuid, "", "")
        grupos.setdefault(llave, []).append(d)
    return [sorted(g, key=lambda d: d.id) for g in grupos.values()]


def _tercera_pata(ctx: _Contexto, mov_id: int) -> Optional[dict]:
    """La conciliación interna del mov, digerida: RUTs canonizables {compras del
    detalle ∪ beneficiario} + folios por RUT + detalles crudos. None si el mov no
    está conciliado con ningún egreso."""
    enlaces = ctx.conc_por_mov.get(mov_id)
    if not enlaces:
        return None
    ruts, folios_por_rut, filas = set(), {}, []
    for enlace in enlaces:
        e = enlace["egreso"]
        canon_b = _rut_canon_sucio(e.beneficiario_rut)
        if canon_b:
            ruts.add(canon_b)
        for det, compra in enlace["detalles"]:
            canon = _rut_canon_sucio(compra.proveedor_rut)
            folio = _folio_estricto(compra.numero_documento)
            if canon:
                ruts.add(canon)
                if folio:
                    folios_por_rut.setdefault(canon, set()).add(folio)
            filas.append({"egreso": e, "detalle": det, "compra": compra,
                          "rut": canon, "folio": folio})
    return {"ruts": ruts, "folios_por_rut": folios_por_rut, "filas": filas,
            "medios": {enlace["egreso"].medio for enlace in enlaces}}


def _senal_s4(ctx: _Contexto, mov, doc) -> Optional[str]:
    """S4 (+25): N° de operación de la glosa (OP\\s*\\d{5,}) o referencia de CHEQUE
    contra egreso.numero_operacion, con strip de ceros en ambos lados (regla 20). La
    señal solo es de IDENTIDAD si ese egreso paga compras del RUT del doc."""
    candidatos = []
    op = extraer_numero_operacion(mov.glosa)
    if op and op.lstrip("0"):
        candidatos.append((op.lstrip("0"), None))
    ref = str(mov.referencia or "").strip()
    if ref and ref.lstrip("0") and len(ref.lstrip("0")) >= 3:
        candidatos.append((ref.lstrip("0"), "cheque"))
    for num, medio_exigido in candidatos:
        for e in ctx.egresos_por_op.get(num, []):
            if medio_exigido and e.medio != medio_exigido:
                continue
            # El egreso interesa POR SU NÚMERO aunque no esté conciliado aún: la
            # relación `detalles` (lazy) trae sus compras.
            for fila in getattr(e, "detalles", []):
                canon = _rut_canon_sucio(getattr(fila.compra, "proveedor_rut", None)
                                         if fila.compra else None)
                if canon and canon == doc.rut_emisor_canonico:
                    return (f"N° operación {num} coincide con egreso #{e.id} "
                            f"({e.medio}) del mismo RUT")
    return None


def _evaluar_par(ctx: _Contexto, mov, doc, grupo_docs, residual: int) -> Optional[dict]:
    """Todas las señales S1-S6 y techos N1-N5 para UN par que ya pasó la puerta de
    monto V1. Devuelve la propuesta (sugerido/auto/conflicto) o None (fuera de
    ventana / sin fecha)."""
    cfg = ctx.cfg
    saldo = ctx.saldo_doc[doc.id]
    motivo: List[str] = [f"monto exacto sobre el saldo (${saldo:,})".replace(",", ".")]
    score = PESO_MONTO_EXACTO
    techos: set = set()
    identidad = False

    if doc.document_date is None or mov.fecha is None:
        return None          # sin fecha no hay ventana: rompe unicidad, no matchea
    delta = (mov.fecha - doc.document_date).days
    if delta < -VENTANA_SUG_ANTES or delta > VENTANA_SUG_DESPUES:
        return None          # regla 12: fuera de la ventana de sugerencia
    if delta < 0:
        techos.add("anticipo")
        motivo.append("pago anterior a la emisión: ¿anticipo?")
    score += score_fecha(delta)

    # ── Techos estructurales N4 (cada uno con su motivo nombrado) ────────────────
    regla = ctx.reglas_rut.get(doc.rut_emisor_canonico)
    if regla and regla.nivel == "BLOQUEADO":
        techos.add("bloqueado")
        motivo.append(f"RUT BLOQUEADO por regla ({regla.motivo or 'sin motivo'}): "
                      "jamás auto — resolver por cruce con el egreso")
    if doc.rut_emisor_canonico in set(cfg.get("ruts_relacionados", [])):
        techos.add("relacionado")
        motivo.append("intercompañía (es_relacionado): techo sugerido por mandato "
                      "del controller")
    es_honorario = (doc.destino == "cc:honorarios") or \
                   (regla is not None and regla.destino_default == "cc:honorarios")
    if es_honorario:
        techos.add("honorarios")
        motivo.append("emisor clasificado honorarios: ambigüedad bruto/líquido — "
                      "jamás auto")
    factor = nombre_de_factor(mov.glosa, tuple(cfg.get("factors_extra", ())))
    if factor:
        techos.add("factor")
        motivo.append(f"posible cesión: la glosa nombra a un factor ({factor}) — "
                      "no discriminante")
    if len(grupo_docs) > 1:
        techos.add("duplicada")
        motivo.append("folio duplicado en el espejo: posible re-emisión (uuid "
                      + ", ".join(d.uuid for d in grupo_docs) + ")")
    cubierto_previo = abs(_p(doc.monto_efectivo)) - saldo
    if cubierto_previo > 0:
        techos.add("segunda_cuota")
        motivo.append(f"segunda cuota (saldo ${saldo:,})".replace(",", "."))

    # ── S6 / N3: unicidad histórica SIN ventana (regla 12, caso Maquirent) ───────
    k_hist = (doc.rut_emisor_canonico, abs(_p(doc.monto_efectivo)))
    n_hist = sum(ctx.hist_rut_monto.get((k_hist[0], k_hist[1] + d), 0)
                 for d in (-1, 0, 1)) if doc.rut_emisor_canonico else 99
    hay_nc_igual = any((k_hist[0], k_hist[1] + d) in ctx.nc_vigentes
                       for d in (-1, 0, 1)) and doc.trx_sign == 1
    if doc.rut_emisor_canonico and n_hist <= 1 and not hay_nc_igual:
        score += PESO_S6_UNICO
        motivo.append("par RUT+monto único en 12 meses")
        s6_unico = True
    else:
        s6_unico = False
        if n_hist >= 2:
            techos.add("recurrente")
            motivo.append(f"monto recurrente: {n_hist} documentos idénticos del RUT — "
                          "solo llega a auto por cruce triple")
        if hay_nc_igual:
            techos.add("recurrente")
            motivo.append("hay una NC vigente del RUT por el mismo monto")

    # ── S1: cruce triple por DOCUMENTO (la señal reina) + N1 + N5 ────────────────
    s1_full = False
    s1_parcial = False
    tp = _tercera_pata(ctx, mov.id)
    conflicto = None
    if tp:
        fila_doc = None
        for fila in tp["filas"]:
            if fila["rut"] and fila["rut"] == doc.rut_emisor_canonico:
                if fila["folio"] and fila["folio"] == _folio_estricto(doc.folio):
                    fila_doc = fila
                    break
        if fila_doc is not None:
            cobertura = abs(_p(fila_doc["detalle"].monto_clp) - saldo)
            if cobertura <= TOL_PESO:
                s1_full = True
                identidad = True
                motivo.append(
                    f"cruce triple por documento: egreso #{fila_doc['egreso'].id} → "
                    f"compra folio {fila_doc['folio']} con cobertura del saldo")
            else:
                s1_parcial = True
                identidad = True
                motivo.append(
                    f"cruce triple parcial: egreso #{fila_doc['egreso'].id} paga "
                    f"${_p(fila_doc['detalle'].monto_clp):,} de un saldo de ${saldo:,}"
                    .replace(",", "."))
        else:
            blando = _folio_blando(doc.folio)
            hay_blando = any(f["rut"] == doc.rut_emisor_canonico and blando
                             and _folio_blando(f["folio"]) == blando
                             for f in tp["filas"])
            mismo_rut = doc.rut_emisor_canonico in tp["folios_por_rut"]
            if hay_blando:
                score += PESO_FOLIO_BLANDO
                techos.add("folio_blando")
                motivo.append("folio coincide solo en variante blanda (dígitos sin "
                              "ceros): sugerido, no auto")
            elif mismo_rut:
                score += PESO_N1
                techos.add("n1_folio_distinto")
                motivo.append("el pago ya explica OTRO documento del proveedor "
                              "(mismo RUT, folio distinto)")
            elif tp["ruts"] and doc.rut_emisor_canonico \
                    and doc.rut_emisor_canonico not in tp["ruts"]:
                # N5: contradicción DURA — jamás descarte silencioso (regla 7).
                egreso0 = tp["filas"][0]["egreso"] if tp["filas"] else None
                conflicto = (
                    "CONFLICTO: el movimiento está conciliado con egreso "
                    f"#{egreso0.id if egreso0 else '?'} de RUT(s) "
                    f"{', '.join(sorted(tp['ruts']))} pero el documento es de "
                    f"{doc.rut_emisor_canonico} folio {doc.folio}: una de las dos "
                    "conciliaciones está mala")
            # Conjunto vacío/no canonizable = NEUTRO: ni eleva ni veta.

    if conflicto is not None:
        # Hallazgo #2 (revisión 2026-08-08): esta rama nacía SIEMPRE con
        # `candidatos: []` porque el conflicto se devuelve ANTES de que abajo se
        # calculen los candidatos del movimiento — y `_upsert` no vuelve a escribir una
        # fila que ya no es 'sugerido', así que ninguna corrida posterior la rellenaba.
        # La pantalla ofrece «Ver los N documentos que compiten» solo cuando la lista
        # trae más de uno: en un conflicto no aparecía NUNCA. Se calcula acá adentro
        # (el camino normal no paga nada de más) con la MISMA función y el MISMO formato
        # que el return de abajo. Se agrega también el motivo de competencia, que hoy se
        # arma después del return y por eso tampoco llegaba a la fila del conflicto.
        rivales = _grupos_candidatos(ctx, residual, doc.trx_sign)
        candidatos_conf = [{"doc_id": d.id, "uuid": d.uuid, "folio": d.folio,
                            "rut": d.rut_emisor_canonico,
                            "saldo": ctx.saldo_doc.get(d.id)}
                           for g in rivales for d in g][:20]
        motivo_conf = motivo + [conflicto]
        if len(rivales) > 1:
            motivo_conf.append(f"{len(rivales)} documentos compiten por este cargo")
        return {"mov": mov, "doc": doc, "via": "directa", "estado": "conflicto",
                "auto": False, "asignado": saldo if doc.trx_sign == 1 else -saldo,
                "score": None, "motivo": motivo_conf,
                "candidatos": candidatos_conf, "grupo": None, "hash": doc.hash_remoto,
                "saldo": saldo, "doc_campos": _campos_doc(doc)}

    # ── S2: RUT-token válido en glosa (APAGADA por configuración hasta la sonda de
    #    ingestión — regla 9) ─────────────────────────────────────────────────────
    s2 = False
    if cfg.get("rut_en_glosa_activo") and doc.rut_emisor_canonico:
        if doc.rut_emisor_canonico in extraer_ruts_de_glosa(mov.glosa):
            s2 = True
            identidad = True
            score += PESO_S2_RUT_GLOSA
            motivo.append("RUT del emisor en la glosa (válido módulo 11, contexto "
                          "no operacional)")

    # ── S3: nombre por prefijos deterministas — NUNCA habilita auto (regla 10) ───
    fuerza, _det = fuerza_nombre(mov.glosa, doc.receiver_name)
    if fuerza == "fuerte":
        # Colisión con OTRO emisor del libro → degrada a media (gemelos truncados).
        for otro in ctx.docs_pool.values():
            if otro.rut_emisor_canonico != doc.rut_emisor_canonico \
                    and otro.receiver_name \
                    and fuerza_nombre(mov.glosa, otro.receiver_name)[0] == "fuerte":
                fuerza = "media"
                motivo.append("nombre colisiona con otro emisor del libro: señal media")
                break
    if fuerza == "fuerte":
        score += PESO_S3_FUERTE
        identidad = True
        motivo.append("nombre fuerte en la glosa (≥2 tokens no genéricos por prefijo)")
    elif fuerza == "media":
        score += PESO_S3_MEDIA
        identidad = True
        motivo.append("nombre parcial en la glosa (señal media)")

    # ── S4: N° de operación / cheque ─────────────────────────────────────────────
    s4 = _senal_s4(ctx, mov, doc)
    if s4:
        score += PESO_S4_OPERACION
        identidad = True
        motivo.append(s4)

    # ── AUTO: conjunción, no puntaje (spec §3) ───────────────────────────────────
    auto = False
    auto_via = None
    candidatos_mov = _grupos_candidatos(ctx, residual, doc.trx_sign)
    unico_para_mov = len(candidatos_mov) == 1
    otros_movs = {mid for mid in ctx.vivos_por_doc.get(doc.id, set())
                  if mid != mov.id}
    rivales_del_doc = sum(
        1 for om in ctx.movs_normales
        if om.id != mov.id and om.tipo == mov.tipo
        and abs(ctx.residual(om) - saldo) <= TOL_PESO) + len(otros_movs)
    unico_para_doc = rivales_del_doc == 0

    if s1_full and not (techos & VIA_A_VETOS):
        auto, auto_via = True, "A"
    elif (s2 and not techos and s6_unico and unico_para_mov and unico_para_doc
          and doc.rut_emisor_canonico and _folio_estricto(doc.folio)
          and doc.document_date is not None):
        ventana_auto = VENTANA_AUTO
        if tp and "cheque" in tp.get("medios", set()):
            ventana_auto += VENTANA_AUTO_CHEQUE_EXTRA
        if 0 <= delta <= ventana_auto:
            auto, auto_via = True, "B"
    if not unico_para_mov and len(candidatos_mov) > 1:
        motivo.append(f"{len(candidatos_mov)} documentos compiten por este cargo")
    if rivales_del_doc:
        motivo.append("dos cargos compiten por el documento")

    candidatos = [{"doc_id": d.id, "uuid": d.uuid, "folio": d.folio,
                   "rut": d.rut_emisor_canonico, "saldo": ctx.saldo_doc.get(d.id)}
                  for g in candidatos_mov for d in g][:20]
    if auto:
        motivo.append("AUTO vía " + ("A (cruce triple con cobertura)" if auto_via == "A"
                                     else "B (RUT en glosa + unicidad bilateral e "
                                          "histórica + ventana)"))
    asignado = saldo if doc.trx_sign == 1 else -saldo
    via = "cruce_triple" if (s1_full or s1_parcial) else "directa"
    return {"mov": mov, "doc": doc, "via": via, "estado": "sugerido",
            "auto": auto, "auto_via": auto_via, "asignado": asignado,
            "score": 100 if auto else min(score, SCORE_MAX_SUGERIDO),
            "motivo": motivo, "candidatos": candidatos, "grupo": None,
            "hash": doc.hash_remoto, "saldo": saldo, "doc_campos": _campos_doc(doc)}


# ═══ Generación de propuestas por vía ═════════════════════════════════════════════

def _propuesta_base(mov, doc, ctx, via, asignado, score, motivo, grupo=None,
                    estado="sugerido"):
    return {"mov": mov, "doc": doc, "via": via, "estado": estado, "auto": False,
            "auto_via": None, "asignado": asignado, "score": score, "motivo": motivo,
            "candidatos": [], "grupo": grupo, "hash": doc.hash_remoto,
            "saldo": ctx.saldo_doc.get(doc.id, 0), "doc_campos": _campos_doc(doc)}


def _ventana_ok(mov, doc) -> Optional[int]:
    if mov.fecha is None or doc.document_date is None:
        return None
    delta = (mov.fecha - doc.document_date).days
    if delta < -VENTANA_SUG_ANTES or delta > VENTANA_SUG_DESPUES:
        return None
    return delta


def _vetos_via_a(ctx: _Contexto, mov, doc) -> List[str]:
    """Los techos que la spec pone POR ENCIMA incluso del cruce triple (regla 15 y la
    lista «JAMÁS auto en…»): BLOQUEADO, intercompañía, honorarios, llave de negocio
    duplicada en el espejo y anticipo (mov < doc). N3-recurrente y segunda cuota NO
    vetan la vía A: «ese par solo llega a AUTO por S1»."""
    vetos = []
    regla = ctx.reglas_rut.get(doc.rut_emisor_canonico)
    if regla and regla.nivel == "BLOQUEADO":
        vetos.append(f"RUT BLOQUEADO por regla ({regla.motivo or 'sin motivo'})")
    if doc.rut_emisor_canonico in set(ctx.cfg.get("ruts_relacionados", [])):
        vetos.append("intercompañía (es_relacionado)")
    if (doc.destino == "cc:honorarios") or \
            (regla is not None and regla.destino_default == "cc:honorarios"):
        vetos.append("emisor clasificado honorarios")
    llave = (doc.rut_emisor_canonico, doc.sii_document_type_id,
             _folio_estricto(doc.folio), str(doc.monto_efectivo))
    duplicados = [d for d in ctx.docs_pool.values()
                  if (d.rut_emisor_canonico, d.sii_document_type_id,
                      _folio_estricto(d.folio), str(d.monto_efectivo)) == llave]
    if len(duplicados) > 1:
        vetos.append("llave de negocio duplicada en el espejo")
    if mov.fecha is not None and doc.document_date is not None \
            and mov.fecha < doc.document_date:
        vetos.append("pago anterior a la emisión: ¿anticipo?")
    return vetos


def _cruce_triple_directo(ctx: _Contexto, mov, propuestas_mov: dict):
    """Vía A / S1 para docs que NO pasan la puerta V1: el caso típico es un egreso
    que paga VARIAS compras (mov de $10M con un detalle de $2M que cubre este doc
    entero → AUTO por documento) o la cobertura parcial (egreso de $2M contra doc de
    $5M → sugerido parcial). También corre para movs de NÓMINA: el multi-RUT existe
    SOLO por la vía del detalle del egreso (regla 16)."""
    tp = _tercera_pata(ctx, mov.id)
    if not tp:
        return
    residual = ctx.residual(mov)
    for fila in tp["filas"]:
        if not fila["rut"] or not fila["folio"]:
            continue     # rut None o folio vacío = INDETERMINADO: ni eleva ni veta
        for did, d in ctx.docs_pool.items():
            if d.rut_emisor_canonico != fila["rut"] \
                    or _folio_estricto(d.folio) != fila["folio"]:
                continue
            if (mov.id, did) in propuestas_mov:
                continue
            saldo = ctx.saldo_doc[did]
            det_monto = _p(fila["detalle"].monto_clp)
            cobertura_total = abs(det_monto - saldo) <= TOL_PESO
            if cobertura_total and abs(residual - saldo) <= TOL_PESO:
                continue     # cobertura total + puerta V1: ya lo evaluó _evaluar_par
            if cobertura_total and residual + TOL_PESO >= det_monto:
                # AUTO vía A: el detalle cubre el saldo del doc (monto_asignado =
                # detalle.monto_clp, jamás el total del doc) — salvo techos duros.
                vetos = _vetos_via_a(ctx, mov, d)
                motivo = [
                    f"cruce triple por documento: egreso #{fila['egreso'].id} → "
                    f"compra folio {fila['folio']} con cobertura del saldo "
                    f"(${det_monto:,})".replace(",", ".")]
                if vetos:
                    motivo += [f"techo estructural: {v} — sugerido, no auto"
                               for v in vetos]
                p = _propuesta_base(mov, d, ctx, "cruce_triple",
                                    det_monto if d.trx_sign == 1 else -det_monto,
                                    SCORE_MAX_SUGERIDO, motivo)
                if not vetos:
                    p["auto"] = True
                    p["auto_via"] = "A"
                    p["score"] = 100
                    p["motivo"].append("AUTO vía A (cruce triple con cobertura)")
                propuestas_mov[(mov.id, did)] = p
                continue
            asignado = min(det_monto, saldo, max(residual, 0))
            if asignado < 1:
                continue
            motivo = [
                f"cruce triple parcial por documento: egreso #{fila['egreso'].id} → "
                f"compra folio {fila['folio']}",
                f"cobertura ${asignado:,} de un saldo de ${saldo:,}".replace(",", "."),
            ]
            p = _propuesta_base(mov, d, ctx, "cruce_triple",
                                asignado if d.trx_sign == 1 else -asignado,
                                min(PESO_MONTO_EXACTO + PESO_FOLIO_BLANDO,
                                    SCORE_MAX_SUGERIDO), motivo)
            propuestas_mov[(mov.id, did)] = p


def _doble_pago(ctx: _Contexto, mov, residual, trx_sign, propuestas_mov: dict):
    """N2: el doc ya está cubierto por confirmados y llega OTRO cargo por el total →
    sugerido 'posible doble pago' (el tope Σ por doc jamás deja confirmar el doble)."""
    for did, d in ctx.docs_cubiertos.items():
        if d.trx_sign != trx_sign or (mov.id, did) in propuestas_mov:
            continue
        if abs(abs(_p(d.monto_efectivo)) - residual) > TOL_PESO:
            continue
        if _ventana_ok(mov, d) is None:
            continue
        motivo = ["posible doble pago: el documento ya está cubierto por matches "
                  "confirmados", f"folio {d.folio} de {d.rut_emisor_canonico}"]
        propuestas_mov[(mov.id, did)] = _propuesta_base(
            mov, d, ctx, "directa", 0, 60, motivo)


def _subset_firmado(ctx: _Contexto, mov, propuestas: list):
    """V2 (regla 8): S ⊆ docs abiertos del MISMO RUT, suma CON SIGNO (habilita
    factura−NC), ≥1 doc positivo, |S| ≤ 4, ≤25 docs/RUT, pesos enteros, presupuesto
    por corrida y SOLO ante solución ÚNICA. SIEMPRE sugerido."""
    residual = ctx.residual(mov)
    por_rut: Dict[str, list] = {}
    for did, d in ctx.docs_pool.items():
        if not d.rut_emisor_canonico or _ventana_ok(mov, d) is None:
            continue
        por_rut.setdefault(d.rut_emisor_canonico, []).append(d)
    soluciones = []
    for rut, docs in por_rut.items():
        docs = sorted(docs, key=lambda d: (d.document_date or date.min),
                      reverse=True)[:SUBSET_MAX_POR_RUT]
        valores = [(d, ctx.saldo_doc[d.id] * (1 if d.trx_sign == 1 else -1))
                   for d in docs]
        for k in range(2, min(SUBSET_MAX_DOCS, len(valores)) + 1):
            for combo in combinations(valores, k):
                if ctx.presupuesto <= 0:
                    return                     # presupuesto agotado: V2 se apaga
                ctx.presupuesto -= 1
                suma = sum(v for _d, v in combo)
                if abs(suma - residual) > TOL_PESO:
                    continue
                if not any(v > 0 for _d, v in combo):
                    continue                   # una NC sola jamás matchea un cargo
                soluciones.append(combo)
                if len(soluciones) > 1:
                    return                     # 2+ soluciones = combinación ambigua
    if len(soluciones) != 1:
        return
    combo = soluciones[0]
    grupo = str(uuid_mod.uuid4())
    nombres = " + ".join(
        f"{'NC ' if v < 0 else ''}folio {d.folio} (${abs(v):,})".replace(",", ".")
        for d, v in combo)
    for d, v in combo:
        motivo = [f"combinación única del RUT {d.rut_emisor_canonico}: {nombres} "
                  f"= ${ctx.residual(mov):,}".replace(",", "."),
                  "subset firmado: siempre sugerido, se confirma o descarta en bloque"]
        propuestas.append(_propuesta_base(mov, d, ctx, "subset", v, 70, motivo,
                                          grupo=grupo))


def _parciales_con_identidad(ctx: _Contexto, mov, propuestas_mov: dict):
    """V3 (spec §1): cuota o pago mayor, SOLO con una señal de identidad (S2/S3) —
    sin identidad, el silencio es preferible a la sugerencia basura. Solo CARGOS:
    el lado abono es dominio de NC↔abono (regla 14, con su prioridad de dominios)."""
    if mov.tipo != "cargo":
        return
    cfg = ctx.cfg
    residual = ctx.residual(mov)
    ruts_glosa = set(extraer_ruts_de_glosa(mov.glosa)) \
        if cfg.get("rut_en_glosa_activo") else set()
    for did, d in ctx.docs_pool.items():
        if d.trx_sign != (1 if mov.tipo == "cargo" else -1):
            continue
        if (mov.id, did) in propuestas_mov:
            continue
        saldo = ctx.saldo_doc[did]
        if abs(saldo - residual) <= TOL_PESO:
            continue                      # eso es V1
        if _ventana_ok(mov, d) is None:
            continue
        identidad = None
        if d.rut_emisor_canonico and d.rut_emisor_canonico in ruts_glosa:
            identidad = "RUT del emisor en la glosa"
        else:
            fuerza, _det = fuerza_nombre(mov.glosa, d.receiver_name)
            if fuerza in ("fuerte", "media"):
                identidad = f"nombre {fuerza} en la glosa"
        if identidad is None:
            continue
        asignado = min(residual, saldo)
        if residual < saldo:
            motivo = [f"pago parcial: cargo ${residual:,} bajo el documento (saldo "
                      f"${saldo:,}) — ¿cuota o descuento sin NC?".replace(",", "."),
                      identidad]
        else:
            motivo = [f"el cargo (${residual:,}) excede el saldo del documento "
                      f"(${saldo:,}): posible pago agrupado".replace(",", "."),
                      identidad]
        score = PESO_S3_FUERTE if "fuerte" in identidad else PESO_S3_MEDIA
        score += PESO_S2_RUT_GLOSA if "RUT" in identidad else 0
        propuestas_mov[(mov.id, did)] = _propuesta_base(
            mov, d, ctx, "parcial", asignado if d.trx_sign == 1 else -asignado,
            min(score + 20, SCORE_MAX_SUGERIDO), motivo)


def _honorarios(ctx: _Contexto, mov, propuestas_mov: dict):
    """V5 (spec §1): monto esperado DUAL — bruto (lo cubre V1 con techo honorarios) o
    bruto×(1−tasa_retención(año)) con la tabla anual parametrizada. Siempre sugerido,
    con el cálculo en el motivo. Si las BHE no entran al espejo, la vía queda muerta
    por datos, no por código."""
    tabla = ctx.cfg.get("retencion_honorarios", {})
    residual = ctx.residual(mov)
    for did, d in ctx.docs_pool.items():
        if d.trx_sign != 1 or (mov.id, did) in propuestas_mov:
            continue
        regla = ctx.reglas_rut.get(d.rut_emisor_canonico)
        es_hon = (d.destino == "cc:honorarios") or \
                 (regla is not None and regla.destino_default == "cc:honorarios")
        if not es_hon or d.document_date is None:
            continue
        tasa = tabla.get(str(d.document_date.year))
        if tasa is None or _ventana_ok(mov, d) is None:
            continue
        saldo = ctx.saldo_doc[did]
        liquido = _p(saldo * (1 - float(tasa) / 100.0))
        if abs(residual - liquido) > TOL_PESO:
            continue
        # Hallazgo #7 (revisión 2026-08-06): monto_asignado = LÍQUIDO, que es lo que
        # el banco realmente pagó. Con el BRUTO toda sugerencia V5 reventaba 409
        # 'Doble uso del monto' al confirmar (bruto > mov.monto siempre que la tasa
        # > 0): la vía existía como sugerencia pero jamás como match. El cálculo
        # queda en motivo[]; el saldo restante del doc (la retención) se explica por
        # el F29, no por la cartola.
        motivo = [f"honorarios: bruto ${saldo:,} × (1 − {tasa}% retención "
                  f"{d.document_date.year}) = ${liquido:,} ≈ cargo".replace(",", "."),
                  f"monto asignado = líquido que pagó el banco; la retención "
                  f"(${saldo - liquido:,}) se explica por el F29, no por la "
                  "cartola".replace(",", "."),
                  "honorarios: jamás auto (ambigüedad bruto/líquido)"]
        propuestas_mov[(mov.id, did)] = _propuesta_base(
            mov, d, ctx, "honorarios", liquido, 75, motivo)


def _agregado_comisiones(ctx: _Contexto, propuestas: list):
    """V4 (regla 19): Σ cargos COMISION del mes vs la factura mensual del banco
    (fecha M o M+1, ±$10 por redondeos de IVA). Jamás subset-sum: con 10-30 cargos
    ínfimos explota combinatoriamente y los montos chicos matchean cualquier cosa."""
    ruts_banco = set(ctx.cfg.get("ruts_banco", []))
    por_mes: Dict[tuple, list] = {}
    for m in ctx.movs_comision:
        if m.tipo != "cargo" or m.fecha is None or ctx.residual(m) < 1:
            continue
        por_mes.setdefault((m.fecha.year, m.fecha.month), []).append(m)
    for (anio, mes), movs in por_mes.items():
        total = sum(ctx.residual(m) for m in movs)
        candidatos = []
        for did, d in ctx.docs_pool.items():
            if d.trx_sign != 1 or d.rut_emisor_canonico not in ruts_banco \
                    or d.document_date is None:
                continue
            f = d.document_date
            en_mes = (f.year, f.month) == (anio, mes)
            sig = (anio + (1 if mes == 12 else 0), 1 if mes == 12 else mes + 1)
            if not (en_mes or (f.year, f.month) == sig):
                continue
            if abs(ctx.saldo_doc[did] - total) <= TOL_COMISION:
                candidatos.append(d)
        if len(candidatos) != 1:
            continue                  # 0 o 2+: nada (ambigüedad = silencio)
        d = candidatos[0]
        grupo = str(uuid_mod.uuid4())
        for m in movs:
            motivo = [f"agregado mensual de comisiones ({len(movs)} cargos de "
                      f"{mes:02d}-{anio}): Σ ${total:,} vs factura del banco "
                      f"${ctx.saldo_doc[d.id]:,}".replace(",", "."),
                      "comisiones bancarias: siempre sugerido, en bloque"]
            propuestas.append(_propuesta_base(m, d, ctx, "agregado_mensual",
                                              ctx.residual(m), 70, motivo,
                                              grupo=grupo))


def _nc_contra_abonos(ctx: _Contexto, mov, propuestas_mov: dict):
    """NC↔abono (regla 14): PRIORIDAD DE DOMINIOS — cobranza/adelanto primero. La NC
    solo se propone si el abono no tiene conciliación de ingreso NI candidato ±1 en
    cobranzas pendientes / adelantos aprobados; con señal de identidad; comparando
    por abs() (con signo crudo la rama nace muerta); SIEMPRE sugerido."""
    if mov.tipo != "abono" or mov.id in ctx.ingreso_movs:
        return
    residual = ctx.residual(mov)
    if any(abs(m - residual) <= TOL_PESO for m in ctx.montos_cobranza_adelanto):
        return          # abono con candidato en cobranzas/adelantos: dominio ajeno
    for did, d in ctx.docs_pool.items():
        if d.trx_sign != -1 or (mov.id, did) in propuestas_mov:
            continue
        if abs(ctx.saldo_doc[did] - residual) > TOL_PESO:
            continue
        if _ventana_ok(mov, d) is None:
            continue
        fuerza, _det = fuerza_nombre(mov.glosa, d.receiver_name)
        ruts_glosa = set(extraer_ruts_de_glosa(mov.glosa)) \
            if ctx.cfg.get("rut_en_glosa_activo") else set()
        if fuerza == "nula" and d.rut_emisor_canonico not in ruts_glosa:
            continue    # sin identidad no hay propuesta (señal de identidad exigida)
        motivo = [f"NC del proveedor por ${ctx.saldo_doc[did]:,} contra abono del "
                  f"banco (sin candidato en cobranzas)".replace(",", "."),
                  f"identidad: nombre {fuerza}" if fuerza != "nula"
                  else "identidad: RUT en glosa",
                  "NC↔abono: siempre sugerido"]
        propuestas_mov[(mov.id, did)] = _propuesta_base(
            mov, d, ctx, "nc_abono", -ctx.saldo_doc[did], 70, motivo)


def _lotes_de_egresos(db: Session, ctx: _Contexto, movs_sin_candidato: list) -> int:
    """Regla 16: cargo sin candidato cuyo monto iguala ±1 la suma de 2-6 egresos NO
    conciliados del mismo día (o el día hábil anterior, si el banco liquidó al hábil
    siguiente) → etiqueta lote_pendiente con los egresos NOMBRADOS: se resuelve
    primero en Tesorería. Multi-RUT solo existe por el detalle del egreso."""
    from monza_compras_contab.models import MonzaContEgreso
    creadas = 0
    for mov in movs_sin_candidato:
        if mov.tipo != "cargo" or mov.fecha is None:
            continue
        residual = ctx.residual(mov)
        if residual < 1:
            continue
        # día del cargo + el hábil ANTERIOR (lunes mira al viernes).
        fechas = [mov.fecha]
        previo = mov.fecha - timedelta(days=3 if mov.fecha.weekday() == 0 else 1)
        fechas.append(previo)
        egresos = (db.query(MonzaContEgreso)
                   .filter(MonzaContEgreso.conciliado.is_(False),
                           MonzaContEgreso.fecha.in_(fechas))
                   .order_by(MonzaContEgreso.id)
                   .limit(LOTE_MAX_EGRESOS_DIA * 2).all())
        if len(egresos) < LOTE_MIN:
            continue
        valores = [(e, _p(e.monto_total_clp)) for e in egresos[:LOTE_MAX_EGRESOS_DIA]]
        solucion = None
        ambiguo = False
        for k in range(LOTE_MIN, min(LOTE_MAX, len(valores)) + 1):
            for combo in combinations(valores, k):
                if ctx.presupuesto <= 0:
                    ambiguo = True
                    break
                ctx.presupuesto -= 1
                if abs(sum(v for _e, v in combo) - residual) <= TOL_PESO:
                    if solucion is not None:
                        ambiguo = True
                        break
                    solucion = combo
            if ambiguo:
                break
        if solucion is None or ambiguo:
            continue
        ids = ", ".join(f"#{e.id}" for e, _v in solucion)
        detalle = (f"posible lote: egresos {ids} suman ${residual:,} — resolver "
                   "primero en Tesorería").replace(",", ".")
        et = (db.query(MonzaSiiMatchEtiquetaMov)
              .filter(MonzaSiiMatchEtiquetaMov.movimiento_id == mov.id).first())
        if et is None:
            db.add(MonzaSiiMatchEtiquetaMov(movimiento_id=mov.id,
                                            etiqueta="lote_pendiente",
                                            detalle=detalle))
            creadas += 1
        elif not et.revertida and et.etiqueta in ("nomina", "lote_pendiente") \
                and detalle not in (et.detalle or ""):
            # idempotente: re-correr no re-apendiza el mismo lote
            et.detalle = ((et.detalle or "")[:150] + " · " + detalle)[:300]
    if creadas:
        db.commit()
    return creadas


# ═══ Escritura (upsert idempotente; autos bajo el candado del movimiento) ════════

def _insertar_auto(db: Session, p: dict) -> bool:
    """AMBAS vías de AUTO ejecutan bajo SELECT…FOR UPDATE + populate_existing del
    monza_tes_movimiento (calca el patrón _mov_scoped de GA; OJO: el _mov_or_404 de
    monza_tesoreria NO usa populate_existing — acá sí, porque es lectura de plata
    bajo lock) con REVALIDACIÓN COMPLETA adentro (saldos frescos, conciliación
    fresca, Σ topes, unicidad), envuelto en retry de deadlock. JAMÁS FOR UPDATE
    sobre monza_sii_libro_doc: el tope por doc se serializa con el lock del mov +
    re-lectura fresca."""
    from monza_tesoreria.models import MonzaTesMovimiento

    def _tx():
        mov = (db.query(MonzaTesMovimiento)
               .filter(MonzaTesMovimiento.id == p["mov"].id)
               .populate_existing().with_for_update().first())
        if mov is None:
            db.rollback()
            return False
        doc = (db.query(MonzaSiiLibroDoc).filter(MonzaSiiLibroDoc.id == p["doc"].id)
               .populate_existing().first())          # SIN lock (regla 22)
        if doc is None or doc.estado_espejo != "ACTIVO" \
                or doc.hash_remoto != p["hash"]:
            db.rollback()
            return False                              # el barrido lo pisó: no auto
        # ── Serialización del lado DOC (hallazgo #8, revisión 2026-08-06): el lock
        # del movimiento NO serializa dos escrituras de movimientos DISTINTOS sobre
        # el MISMO doc — un auto del motor y un confirm humano paralelos leían el Σ
        # del doc sin lock (ambos veían 0) y sumaban 2× el documento. FOR UPDATE
        # sobre las filas de monza_sii_libro_match (ix_monza_sii_match_doc_estado)
        # — JAMÁS sobre la tabla del espejo (regla 22 intacta: el barrido no toca
        # la tabla de matches). Orden global de locks: mov primero, doc después.
        (db.query(MonzaSiiLibroMatch.id)
         .filter(MonzaSiiLibroMatch.doc_id == p["doc"].id)
         .with_for_update().all())
        # Σ topes FRESCOS (invariantes de la regla 1, verificadas bajo el lock):
        frescos = (db.query(MonzaSiiLibroMatch)
                   .filter(MonzaSiiLibroMatch.estado == "confirmado",
                           (MonzaSiiLibroMatch.movimiento_id == mov.id)
                           | (MonzaSiiLibroMatch.doc_id == doc.id)).all())
        # Tope por MOVIMIENTO con suma FIRMADA (hallazgo #6, revisión 2026-08-06):
        # el consumo real del mov es Σ monto_asignado CON SIGNO proyectada a su lado
        # (cargo=+1 / abono=−1) — con Σ|abs| un grupo factura−NC excede siempre
        # (Σ|v| > Σv). El tope por DOC sigue en abs(): cada fila es un doc distinto
        # y la NC consume su propio |monto_efectivo|.
        signo_mov = 1 if mov.tipo == "cargo" else -1
        sum_mov = sum(_p(m.monto_asignado) for m in frescos
                      if m.movimiento_id == mov.id)
        sum_doc = sum(abs(_p(m.monto_asignado)) for m in frescos
                      if m.doc_id == doc.id)
        asignado = _p(p["asignado"])
        consumo = (sum_mov + asignado) * signo_mov
        if consumo > _p(mov.monto) + TOL_PESO or consumo < -TOL_PESO:
            db.rollback()
            return False
        if sum_doc + abs(asignado) > abs(_p(doc.monto_efectivo)) + TOL_PESO:
            db.rollback()
            return False
        if any(m.doc_id == doc.id and m.movimiento_id != mov.id for m in frescos):
            db.rollback()
            return False       # otro mov confirmó el doc entre la foto y el lock
        if p["auto_via"] == "B" and mov.conciliado:
            db.rollback()
            return False       # se concilió internamente en el intertanto: a mano
        db.add(MonzaSiiLibroMatch(
            movimiento_id=mov.id, doc_id=doc.id,
            monto_asignado=p["asignado"], via=p["via"], estado="confirmado",
            origen="auto", score=100,
            motivo=json.dumps(p["motivo"], ensure_ascii=False),
            candidatos_evaluados=json.dumps(
                {"candidatos": p["candidatos"], "doc_campos": p["doc_campos"]},
                ensure_ascii=False),
            hash_doc_al_matchear=p["hash"], monto_doc_al_matchear=p["saldo"],
            clave_mov_snapshot=json.dumps(_clave_mov_de(mov), ensure_ascii=False),
            decidido_at=datetime.utcnow()))
        if not mov.conciliado:
            # El match AUTO deja el movimiento EXPLICADO: enciende el mismo flag que
            # protege contra DELETE (409 en Tesorería) y contra una conciliación
            # interna contradictoria posterior (carrera de la regla 3). La
            # degradación lo APAGA si el flag es del match (sin enlaces internos).
            mov.conciliado = True
            mov.conciliado_at = datetime.utcnow()
        db.commit()
        return True

    try:
        return bool(_con_retry_deadlock(db, _tx))
    except Exception:                                  # noqa: BLE001
        db.rollback()
        logger.exception("matcher Monza: auto (mov %s, doc %s) abortado",
                         p["mov"].id, p["doc"].id)
        return False


def _liberar_conciliado_si_es_del_match(db: Session, mov_id: int,
                                        salvo_match_id: Optional[int] = None):
    """Apaga mov.conciliado SOLO si lo encendió un match: sin enlaces internos
    (monza_tes_conciliacion — egreso O adelanto — / monza_tes_conciliacion_ingreso)
    y sin OTRO match confirmado vivo. La conciliación interna de Tesorería jamás se
    toca desde acá."""
    from monza_tesoreria.models import (MonzaTesConciliacion,
                                        MonzaTesConciliacionIngreso,
                                        MonzaTesMovimiento)
    # Hallazgos #1 y #10 (revisión 2026-08-08): la sesión corre con autoflush=False
    # (database.py), así que el cambio de estado que el CALLER acaba de hacer EN MEMORIA
    # —«descartado» en router_match.descartar, «en_duda» en _reverificar_confirmados—
    # todavía NO viajó a la base cuando corre el SELECT de `otro`, unas líneas más
    # abajo. Sin este flush, el propio match que se está deshaciendo se lee a sí mismo
    # como 'confirmado', `otro` nunca es None y el flag JAMÁS se apaga: «Deshacer
    # conciliación» respondía 200 y dejaba el movimiento marcado como conciliado sin
    # NADA que lo explicara (fuera del filtro de pendientes, vetado para siempre para un
    # AUTO vía B y con la cartola trabada en 409, justo lo contrario de lo que promete
    # la pantalla). El parámetro `salvo_match_id` no alcanzaba para taparlo: un grupo
    # (subset factura−NC) trae VARIOS matches y el parámetro es UNO solo, y en la
    # re-verificación dos matches del mismo movimiento degradados en la misma pasada se
    # tapaban mutuamente (cada uno veía al otro todavía 'confirmado' en la base). El
    # flush pone a TODOS en la base antes de preguntar, y `salvo_match_id` queda como
    # cinturón redundante. Va antes del FOR UPDATE del movimiento: el orden global de
    # locks (matches → movimiento) no cambia.
    db.flush()
    mov = (db.query(MonzaTesMovimiento)
           .filter(MonzaTesMovimiento.id == mov_id)
           .populate_existing().with_for_update().first())
    if mov is None or not mov.conciliado:
        return
    tiene_enlace = (db.query(MonzaTesConciliacion.id)
                    .filter(MonzaTesConciliacion.movimiento_id == mov_id).first()
                    is not None
                    or db.query(MonzaTesConciliacionIngreso.id)
                    .filter(MonzaTesConciliacionIngreso.movimiento_id == mov_id)
                    .first() is not None)
    otro = (db.query(MonzaSiiLibroMatch.id)
            .filter(MonzaSiiLibroMatch.movimiento_id == mov_id,
                    MonzaSiiLibroMatch.estado == "confirmado",
                    MonzaSiiLibroMatch.id != (salvo_match_id or -1)).first())
    if not tiene_enlace and otro is None:
        mov.conciliado = False
        mov.conciliado_at = None


def _motivo_append(m: MonzaSiiLibroMatch, texto: str):
    try:
        lista = json.loads(m.motivo or "[]")
    except (ValueError, TypeError):
        lista = [str(m.motivo)]
    if texto not in lista:
        lista.append(texto)
    m.motivo = json.dumps(lista, ensure_ascii=False)


def _reanclar_descartados(db: Session, ctx: _Contexto) -> int:
    """Regla 5: un descarte cuya cartola se borró (movimiento_id NULL por SET NULL)
    se RE-ANCLA al movimiento re-importado por su clave suave, y se ALERTA la
    re-aparición — el par vuelve a estar OCUPADO en el UNIQUE: eso ES el no-revivir."""
    from monza_tesoreria.models import MonzaTesMovimiento
    sueltos = [m for m in ctx.matches
               if m.movimiento_id is None and m.estado == "descartado"
               and m.clave_mov_snapshot]
    n = 0
    for m in sueltos:
        try:
            clave = json.loads(m.clave_mov_snapshot)
        except (ValueError, TypeError):
            continue
        candidatos = (db.query(MonzaTesMovimiento)
                      .filter(MonzaTesMovimiento.tipo == clave[1]).all())
        for cand in candidatos:
            if _clave_mov_de(cand) == clave \
                    and (cand.id, m.doc_id) not in ctx.by_par:
                m.movimiento_id = cand.id
                m.divergente = True
                m.divergencia_detalle = ("re-aparición de par descartado (cartola "
                                         "re-importada): sigue descartado")[:500]
                ctx.by_par[(cand.id, m.doc_id)] = m
                n += 1
                break
    if n:
        db.commit()
    return n


def _reverificar_confirmados(db: Session, ctx: _Contexto) -> dict:
    """Regla 4: AUTO nunca es permanente — cada corrida re-verifica los vivos.
    (a) gemelo nuevo que habría calzado → en_duda con el gemelo nombrado;
    (b) hash vivo ≠ hash al matchear → en_duda con los CAMPOS cambiados;
    (c) doc DESAPARECIDO o mov conciliado en contradicción → en_duda.
    Confirmados HUMANOS no se degradan solos: divergente + alerta."""
    docs_all = {}
    ids = [m.doc_id for m in ctx.matches if m.estado == "confirmado"]
    if ids:
        docs_all = {d.id: d for d in db.query(MonzaSiiLibroDoc)
                    .filter(MonzaSiiLibroDoc.id.in_(ids)).all()}
    degradados = alertados = 0
    # Orden global de locks: por movimiento_id ascendente (la liberación del flag
    # conciliado toma FOR UPDATE del mov — dos corridas jamás se cruzan en desorden).
    confirmados = sorted([x for x in ctx.matches if x.estado == "confirmado"],
                         key=lambda m: (m.movimiento_id or 0, m.id))
    for m in confirmados:
        doc = docs_all.get(m.doc_id)
        problemas = []
        if doc is None or doc.estado_espejo != "ACTIVO":
            problemas.append("el documento ya no está ACTIVO en el espejo")
        else:
            if m.hash_doc_al_matchear and doc.hash_remoto != m.hash_doc_al_matchear:
                cambiados = []
                try:
                    antes = json.loads(m.candidatos_evaluados or "{}").get(
                        "doc_campos", {})
                except (ValueError, TypeError):
                    antes = {}
                ahora = _campos_doc(doc)
                for k, v in ahora.items():
                    if antes and str(antes.get(k)) != str(v):
                        cambiados.append(k)
                problemas.append("el documento cambió después del match: "
                                 + (", ".join(cambiados) or "contenido"))
            if m.origen == "auto" and m.via != "cruce_triple":
                # Gemelo nuevo: la unicidad era la base del auto vía B (el barrido
                # trae docs con retraso — por eso el sync es completo).
                gemelo = next(
                    (d for did, d in ctx.docs_pool.items()
                     if did != m.doc_id and d.trx_sign == (doc.trx_sign or 1)
                     and abs(ctx.saldo_doc[did] - abs(_p(m.monto_asignado)))
                     <= TOL_PESO), None)
                if gemelo is not None:
                    problemas.append(f"apareció un candidato que habría calzado: "
                                     f"uuid={gemelo.uuid}")
            tp = _tercera_pata(ctx, m.movimiento_id) if m.movimiento_id else None
            if tp and tp["ruts"] and doc.rut_emisor_canonico \
                    and doc.rut_emisor_canonico not in tp["ruts"]:
                problemas.append("el movimiento quedó conciliado con un egreso que "
                                 "apunta a otro RUT (contradicción)")
        if m.movimiento_id is None:
            problemas.append("el movimiento del match fue eliminado")
        if not problemas:
            continue
        detalle = "; ".join(problemas)[:500]
        if m.origen == "auto":
            m.estado = "en_duda"
            m.divergente = True
            m.divergencia_detalle = detalle
            _motivo_append(m, "degradado a en_duda: " + detalle)
            if m.movimiento_id is not None:
                _liberar_conciliado_si_es_del_match(db, m.movimiento_id, m.id)
            degradados += 1
        else:
            m.divergente = True
            m.divergencia_detalle = detalle
            alertados += 1
    if degradados or alertados:
        db.commit()
    return {"degradados": degradados, "alertados": alertados}


def _caducar_zombis(db: Session, ctx: _Contexto) -> int:
    """Sugerencias cuyo doc dejó de estar ACTIVO, cuyo mov desapareció o cuyo mov se
    concilió en CONTRADICCIÓN → en_duda con motivo (jamás borrado silencioso)."""
    n = 0
    ids = [m.doc_id for m in ctx.matches if m.estado == "sugerido"]
    docs_all = {d.id: d for d in db.query(MonzaSiiLibroDoc)
                .filter(MonzaSiiLibroDoc.id.in_(ids)).all()} if ids else {}
    for m in [x for x in ctx.matches if x.estado == "sugerido"]:
        doc = docs_all.get(m.doc_id)
        motivo = None
        if m.movimiento_id is None:
            motivo = "caducado: el movimiento fue eliminado"
        elif doc is None or doc.estado_espejo != "ACTIVO":
            motivo = "caducado: el documento dejó de estar ACTIVO"
        else:
            tp = _tercera_pata(ctx, m.movimiento_id) if m.movimiento_id else None
            if tp and tp["ruts"] and doc.rut_emisor_canonico \
                    and doc.rut_emisor_canonico not in tp["ruts"]:
                motivo = ("caducado: el movimiento se concilió con un egreso de "
                          "otro RUT")
        if motivo:
            m.estado = "en_duda"
            _motivo_append(m, motivo)
            n += 1
    if n:
        db.commit()
    return n


def _reevaluar_conflictos(db: Session, ctx: _Contexto) -> int:
    """Hallazgo #12 (revisión 2026-08-06): las filas CONFLICTO eran zombis — ninguna
    fase las procesaba (_caducar_zombis solo mira 'sugerido' y _upsert devuelve None
    para todo lo que no sea sugerido), así que una contradicción YA CORREGIDA en
    Tesorería seguía en la bandeja para siempre y bloqueaba el par en by_par (el
    AUTO legítimo nunca nacía). Cada corrida re-evalúa los conflictos vivos:
      · la contradicción desapareció (la tercera pata fresca ya contiene el RUT del
        doc, o el mov quedó sin conciliación interna) → conflicto→sugerido con
        motivo 'contradicción resuelta' — la corrida re-propone con señales frescas;
      · el par murió (mov eliminado / doc fuera de ACTIVO) → en_duda 'caducado',
        igual que los zombis de sugeridos.
    Transición nueva del motor: conflicto→sugerido | conflicto→en_duda(caducado) —
    complementa la máquina de estados documentada en models.py. Espejo de GA."""
    n = 0
    conflictos = [m for m in ctx.matches if m.estado == "conflicto"]
    if not conflictos:
        return 0
    docs_all = {d.id: d for d in db.query(MonzaSiiLibroDoc)
                .filter(MonzaSiiLibroDoc.id.in_(
                    [m.doc_id for m in conflictos])).all()}
    for m in conflictos:
        doc = docs_all.get(m.doc_id)
        if m.movimiento_id is None:
            m.estado = "en_duda"
            _motivo_append(m, "caducado: el movimiento fue eliminado")
            n += 1
            continue
        if doc is None or doc.estado_espejo != "ACTIVO":
            m.estado = "en_duda"
            _motivo_append(m, "caducado: el documento dejó de estar ACTIVO")
            n += 1
            continue
        tp = _tercera_pata(ctx, m.movimiento_id)
        contradiccion = bool(tp and tp["ruts"] and doc.rut_emisor_canonico
                             and doc.rut_emisor_canonico not in tp["ruts"])
        if not contradiccion:
            m.estado = "sugerido"
            _motivo_append(m, "contradicción resuelta: re-evaluado en la corrida")
            n += 1
    if n:
        db.commit()
    return n


def _escribir_propuestas(db: Session, ctx: _Contexto, propuestas: list) -> dict:
    """Upsert idempotente por (mov, doc): re-correr no duplica, no revive descartados,
    no pisa confirmados; refresca score/motivo SOLO de sugeridos (máquina de estados
    de la spec §5)."""
    sugeridos = autos = conflictos = 0
    grupos: Dict[str, list] = {}
    sueltas = []
    for p in propuestas:
        (grupos.setdefault(p["grupo"], []) if p["grupo"] else sueltas).append(p)
    # Un par puede llegar por DOS vías a la vez (ej.: parcial por nombre Y combo
    # factura−NC). El UNIQUE(mov, doc) exige elegir UNA: gana el GRUPO (vía
    # estructural con solución única explica el cargo COMPLETO), salvo que la suelta
    # sea un AUTO documental (cruce triple) — ahí sobra el combo.
    claves_auto = {(p["mov"].id, p["doc"].id) for p in sueltas if p["auto"]}
    for g in list(grupos):
        if any((p["mov"].id, p["doc"].id) in claves_auto for p in grupos[g]):
            del grupos[g]
    claves_grupo = {(p["mov"].id, p["doc"].id)
                    for filas in grupos.values() for p in filas}
    sueltas = [p for p in sueltas
               if (p["mov"].id, p["doc"].id) not in claves_grupo]

    def _upsert(p) -> Optional[str]:
        existente = ctx.by_par.get((p["mov"].id, p["doc"].id))
        if existente is not None:
            if existente.estado == "sugerido":
                existente.via = p["via"]
                existente.monto_asignado = p["asignado"]
                # BAJO (revisión 2026-08-06): un refresh con condiciones de AUTO no
                # puede escribir score=100 en una fila que SIGUE sugerida — 100 está
                # reservado a la conjunción AUTO consumada.
                existente.score = (min(_p(p["score"]), SCORE_MAX_SUGERIDO)
                                   if p["auto"] else p["score"])
                motivo = list(p["motivo"])
                if p["auto"]:
                    motivo.append("cumple condiciones de AUTO pero ya existía como "
                                  "sugerencia: confirmar a mano")
                existente.motivo = json.dumps(motivo, ensure_ascii=False)
                existente.candidatos_evaluados = json.dumps(
                    {"candidatos": p["candidatos"], "doc_campos": p["doc_campos"]},
                    ensure_ascii=False)
                if p["grupo"]:
                    existente.grupo_uuid = p["grupo"]
                return "refresh"
            return None            # confirmado/descartado/en_duda/conflicto: no tocar
        if p["estado"] == "conflicto":
            db.add(_fila_de(p, estado="conflicto"))
            return "conflicto"
        if p["auto"]:
            if _insertar_auto(db, p):
                return "auto"
            # BAJO (revisión 2026-08-06): el AUTO cuya revalidación bajo lock falló
            # caía a sugerido CONSERVANDO score=100 y el motivo 'AUTO vía …' — la
            # bandeja mostraba como AUTO consumado algo que NO pasó la conjunción.
            # Se recalcula al degradar: score de sugerido y motivo que dice qué pasó.
            p = dict(p)
            p["score"] = min(_p(p["score"]), SCORE_MAX_SUGERIDO)
            p["motivo"] = ([t for t in p["motivo"]
                            if not t.startswith("AUTO vía")]
                           + ["degradado a sugerido: la revalidación bajo lock del "
                              "AUTO falló (estado fresco distinto al de la foto) — "
                              "confirmar a mano"])
            return _insertar_sugerido(p)
        return _insertar_sugerido(p)

    def _fila_de(p, estado):
        return MonzaSiiLibroMatch(
            movimiento_id=p["mov"].id, doc_id=p["doc"].id,
            grupo_uuid=p["grupo"], monto_asignado=p["asignado"], via=p["via"],
            estado=estado, origen="auto", score=p["score"],
            motivo=json.dumps(p["motivo"], ensure_ascii=False),
            candidatos_evaluados=json.dumps(
                {"candidatos": p["candidatos"], "doc_campos": p["doc_campos"]},
                ensure_ascii=False),
            hash_doc_al_matchear=p["hash"], monto_doc_al_matchear=p["saldo"],
            clave_mov_snapshot=json.dumps(_clave_mov_de(p["mov"]),
                                          ensure_ascii=False))

    def _insertar_sugerido(p):
        db.add(_fila_de(p, estado="sugerido"))
        return "sugerido"

    for p in sueltas:
        r = _upsert(p)
        # Commit POR PROPUESTA: _insertar_auto hace rollback si su revalidación
        # falla — con adds pendientes en la sesión, ese rollback se llevaría
        # sugerencias ya generadas de otros pares.
        db.commit()
        if r == "auto":
            autos += 1
        elif r == "sugerido":
            sugeridos += 1
        elif r == "conflicto":
            conflictos += 1
    for _g, filas in grupos.items():
        # Grupo (subset / agregado): nace ENTERO o no nace — si algún par ya fue
        # descartado/confirmado, el combo no se re-propone (no revivir en bloque).
        estados = [ctx.by_par.get((p["mov"].id, p["doc"].id)) for p in filas]
        if any(e is not None and e.estado != "sugerido" for e in estados):
            continue
        for p in filas:
            r = _upsert(p)
            if r == "sugerido":
                sugeridos += 1
        db.commit()
    return {"sugeridos": sugeridos, "autos": autos, "conflictos": conflictos}


# ═══ La corrida ══════════════════════════════════════════════════════════════════

# Hallazgo #5 (revisión 2026-08-08): el texto con el que la corrida perdedora del
# fencing cierra su bitácora es DATO, no prosa — el tablero lo usa para no confundir
# «se retiró porque otra iba primero» con «el motor falló». Vive acá, junto a quien lo
# escribe, para que jamás se separen (router_match.resumen lo importa).
ERROR_FENCING = "se retira: otra corrida del matcher arrancó primero (fencing por id)"


def _matcher_en_curso(db: Session, *, anteriores_a: Optional[int] = None) -> bool:
    """¿Hay otra corrida viva? Con `anteriores_a` mira SOLO las de id menor: así se
    resuelve el empate entre dos corridas que arrancaron a la vez (gana la más vieja)."""
    limite = datetime.utcnow() - timedelta(minutes=10)
    q = (db.query(MonzaSiiMatchRun)
         .filter(MonzaSiiMatchRun.terminado_at.is_(None),
                 MonzaSiiMatchRun.iniciado_at > limite))
    if anteriores_a is not None:
        q = q.filter(MonzaSiiMatchRun.id < anteriores_a)
    return q.first() is not None


def correr_matcher(db: Session, *, origen: str = "manual",
                   usuario_id: Optional[int] = None) -> MonzaSiiMatchRun:
    """UNA corrida completa del motor. Idempotente (N corridas = 1). Se ABSTIENE si
    hay barrido del espejo vivo (regla 22) u otra corrida del matcher a medias."""
    if _run_en_curso(db):
        raise MatcherEnCurso(
            "Hay un barrido del libro SII corriendo: el matcher se abstiene para no "
            "competir con esa transacción (reintente cuando termine).")
    if _matcher_en_curso(db):
        raise MatcherEnCurso("Ya hay una corrida del matcher en curso.")
    run = MonzaSiiMatchRun(origen=origen, usuario_id=usuario_id)
    db.add(run)
    db.commit()          # la bitácora nace visible ANTES del trabajo (diagnóstico)

    # FENCING (lente de ciclo de vida 2026-08-08): los dos chequeos de arriba son
    # leer-y-después-actuar, así que dos llamadas simultáneas los pasan LAS DOS —
    # y desde que el motor corre solo (tras el barrido y tras importar cartola)
    # además del botón, la simultaneidad dejó de ser hipotética. Ya con la fila
    # escrita, el empate se resuelve por id: gana la corrida más VIEJA y la otra se
    # retira sin escribir un solo match. Sin esto, las dos trabajaban sobre los
    # mismos movimientos y la perdedora moría a mitad de camino con IntegrityError,
    # dejando su bitácora abierta y bloqueando al motor los 10 minutos siguientes.
    if _matcher_en_curso(db, anteriores_a=run.id):
        run.terminado_at = datetime.utcnow()
        # exito=False y no NULL: el invariante del modelo es «NULL = corriendo», y esta
        # corrida ya terminó. No hizo su trabajo, y el `error` aclara que se retiró por
        # la carrera, no que se cayó.
        run.exito = False
        run.error = ERROR_FENCING
        db.commit()
        raise MatcherEnCurso("Ya hay una corrida del matcher en curso.")

    try:
        cfg = cargar_config(db)
        ctx = _construir_contexto(db, cfg)
        _reanclar_descartados(db, ctx)
        reverif = _reverificar_confirmados(db, ctx)
        caducados = _caducar_zombis(db, ctx)
        _reevaluar_conflictos(db, ctx)     # hallazgo #12: los CONFLICTO no son zombis
        # tras degradar/caducar, la foto de saldos puede haber cambiado: los residuos
        # de matches degradados NO se descuentan (solo confirmados suman al tope).
        ctx = _construir_contexto(db, cfg)

        propuestas: list = []
        movs_sin_candidato: list = []
        propuestas_mov: dict = {}
        for mov in ctx.movs_normales:
            residual = ctx.residual(mov)
            if residual < 1:
                continue
            trx = 1 if mov.tipo == "cargo" else -1
            grupos = _grupos_candidatos(ctx, residual, trx)
            hubo = False
            for grupo_docs in grupos:
                doc = grupo_docs[0]
                if mov.tipo == "abono":
                    continue     # el lado NC↔abono tiene su rama con prioridad de dominios
                p = _evaluar_par(ctx, mov, doc, grupo_docs, residual)
                if p is not None:
                    propuestas_mov[(mov.id, doc.id)] = p
                    hubo = True
            _cruce_triple_directo(ctx, mov, propuestas_mov)
            _doble_pago(ctx, mov, residual, trx, propuestas_mov)
            _parciales_con_identidad(ctx, mov, propuestas_mov)
            if mov.tipo == "cargo":
                _honorarios(ctx, mov, propuestas_mov)
                # Hallazgo #11 (revisión 2026-08-06): V2 se gatea por 'no hubo
                # PROPUESTA V1', no por 'no hubo candidato de monto'. Con
                # `not grupos and not hubo`, un solo doc fuera de ventana (o sin
                # fecha) que calzara el monto llenaba `grupos` y suprimía el subset
                # COMPLETO del movimiento — el combo factura−NC de la regla 8
                # jamás nacía. La condición vieja era además degenerada (`hubo`
                # solo puede ser True con `grupos` no vacío). La unicidad para
                # AUTO sigue usando los grupos sin ventana: eso no cambia.
                if not hubo:
                    _subset_firmado(ctx, mov, propuestas)
            else:
                _nc_contra_abonos(ctx, mov, propuestas_mov)
            if not any(k[0] == mov.id for k in propuestas_mov) \
                    and not any(p["mov"].id == mov.id for p in propuestas):
                movs_sin_candidato.append(mov)
        for mov in ctx.movs_nomina:
            # Nómina: JAMÁS subset (regla 16) — solo tercera pata doc-a-doc y lote.
            _cruce_triple_directo(ctx, mov, propuestas_mov)
            if not any(k[0] == mov.id for k in propuestas_mov):
                movs_sin_candidato.append(mov)
        _agregado_comisiones(ctx, propuestas)
        propuestas = list(propuestas_mov.values()) + propuestas

        escritura = _escribir_propuestas(db, ctx, propuestas)
        _lotes_de_egresos(db, ctx, movs_sin_candidato)

        run.terminado_at = datetime.utcnow()
        run.exito = True
        run.sugeridos_nuevos = escritura["sugeridos"]
        run.autos_nuevos = escritura["autos"]
        run.conflictos = escritura["conflictos"]
        run.degradados_en_duda = reverif["degradados"]
        run.caducados = caducados
        db.commit()
        logger.info("Matcher Monza (%s) OK: %s sugeridos, %s autos, %s conflictos, "
                    "%s degradados, %s caducados", origen, escritura["sugeridos"],
                    escritura["autos"], escritura["conflictos"],
                    reverif["degradados"], caducados)
    except Exception as e:                             # noqa: BLE001
        db.rollback()
        run.terminado_at = datetime.utcnow()
        run.exito = False
        run.error = f"{type(e).__name__}: {e}"[:2000]
        db.commit()
        logger.exception("Matcher Monza (%s): corrida abortada", origen)
    return run
