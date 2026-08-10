"""API del libro de compras del SII — tablero, barrido, bandeja y decisiones (Fases A1+A2).

Prefijo /api/sii-libro. SOLO Grupo AM (candado 'mineria' a nivel de router — los datos del
libro son de la empresa que emite con el RUT 77.977.813-4). Sin gate de feature: es lectura
y decisión, no toca plata del ERP.

LA PREGUNTA QUE RESPONDE LA BANDEJA (el encargo original del dueño):
    «¿qué facturas de proveedor existen ante el SII y NO están registradas en el ERP?»

Las TRES cubetas (Regla 3 del plan — tres respuestas, no dos):
  · ESTA            — hay una compra ACTIVA con el mismo RUT canónico y el mismo N° de
                      documento.
  · NO_ESTA         — no hay match Y las compras del ERP tienen llaves con qué descartarlo.
  · INDETERMINADO   — el ERP tiene compras SIN llave (sin RUT canonizable o sin N° de
                      documento) que PODRÍAN ser ESTE documento: no se puede concluir, y
                      decirlo es más honesto que adivinar. El tablero muestra cuántas
                      compras sin llave alimentan esta cubeta.
                      «PODRÍAN ser ESTE documento» es literal desde el hallazgo #14: la
                      duda de una compra sin RUT ni N° alcanza SOLO a los documentos de
                      su mismo monto, no al libro entero. También cae acá la colisión de
                      TIPO del hallazgo #11 (mismo RUT y mismo N°, distinta familia de
                      DTE): ni «está» ni «no está», y la fila lo explica.
"""
import csv
import io
import logging
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, case, func
from sqlalchemy.orm import Session

from auth import get_current_user
from database import get_db
from empresa_guard import require_empresa
from models.models import Proveedor, User

# El MISMO criterio de folio blando del matcher (hallazgo confirmado 2026-08-06 #1:
# «cruce de cubetas por folio EXACTO: ceros a la izquierda o prefijos producen falso
# NO_ESTA y terminan en CxP duplicada»). Se REUSA el helper — dos definiciones del
# mismo criterio divergirían tarde o temprano, y este es exactamente el criterio que
# el paquete ya validó para folios sucios del ERP ('F-1234'→'1234', lstrip de ceros).
from .matcher import _folio_blando as folio_blando
# Y el MISMO canonizador TOLERANTE del matcher (hallazgo #9, revisión 2026-08-08): el
# RUT del ERP es texto libre y viene sucio ('96.631.520 2', con un espacio donde iba el
# guión). La versión estricta se rinde ante esa forma y devuelve None → la compra se
# trata como «sin RUT» y su documento del SII nunca cae en ESTA aunque esté registrado.
# Se REUSA el helper del propio paquete, por la misma razón que el folio blando.
from .matcher import _rut_canon_sucio as rut_canon_erp
from .models import SiiLibroDoc, SiiLibroReglaRut, SiiLibroSyncRun
from .rut import rut_canonico, rut_formateado
from .sync import BarridoEnCurso, ejecutar_barrido

logger = logging.getLogger("wasabil_compras")

router = APIRouter(
    prefix="/api/sii-libro",
    tags=["sii-libro"],
    dependencies=[Depends(require_empresa("mineria"))],
)

NIVELES_REGLA = {"BLOQUEADO", "IGNORAR_AUTO", "LOGISTICO"}
# Destinos de la clasificación (Fase A2). 'costo_venta' capitaliza (Fase B lo vincula al
# embarque); los 'cc:*' son centros de costos — el catálogo definitivo lo fija el informe
# del controller (docs/sentido-financiero-libro-compras-2026-08-06.md) y es AMPLIABLE:
# se valida el prefijo, no una lista cerrada, para que agregar un centro no exija deploy.
DESTINO_COSTO_VENTA = "costo_venta"
# Tercera salida (hallazgo ALTO del controller): una factura como la de STEEL INGENIERIA
# ($28M en un solo documento) no es costo de mercadería NI gasto del período — es un
# ACTIVO FIJO que se deprecia. Sin esta salida, el operador la forzaría a una de las dos
# y distorsionaría el P&L o el landed.
DESTINO_ACTIVO_FIJO = "activo_fijo"
CENTROS_SUGERIDOS = [
    "cc:financiero", "cc:distribucion", "cc:bodegaje", "cc:administracion",
    "cc:honorarios", "cc:intercompania", "cc:servicios_venta",
]
# Enfriamiento del barrido MANUAL — ver el docstring de `sincronizar_ahora`. No aplica al
# nocturno (corre una vez al día) ni al matcher (trabajo LOCAL, ya serializado por su
# propio guard y sin costo contra terceros): acá el bien que se protege es la cuota de la
# API de Wasabil, que es plata de la empresa.
COOLDOWN_MANUAL_SEG = 120


# ── Schemas ───────────────────────────────────────────────────────────────────

class DecisionIn(BaseModel):
    accion: str                       # 'ignorar' | 'clasificar' | 'pendiente' (deshacer)
    destino: Optional[str] = None     # exigido si accion=clasificar
    motivo: Optional[str] = Field(None, max_length=400)


class ReglaRutIn(BaseModel):
    rut: str
    nivel: str                        # BLOQUEADO | IGNORAR_AUTO | LOGISTICO
    destino_default: Optional[str] = Field(None, max_length=60)
    motivo: Optional[str] = Field(None, max_length=300)


# ── Cruce contra el ERP ───────────────────────────────────────────────────────

# Familia del tipo de DTE del SII, para poder compararla contra `cont_compra.tipo_doc`
# (hallazgo #11, revisión 2026-08-08). Las series de folio del SII son independientes
# POR TIPO y por emisor: el mismo proveedor puede mandarnos la factura 33 N° 12 y la
# boleta 39 N° 12, que son dos documentos distintos. Sin el tipo en la llave del cruce,
# la boleta se declaraba «ESTA» contra la factura ya registrada, salía del filtro de
# faltantes y del CSV, y esa CxP no nacía nunca.
#
# La granularidad la manda el lado POBRE: `tipo_doc` solo distingue
# factura|boleta|nota|recibo|sin_documento, así que 33 (afecta) y 34 (exenta) son ambas
# 'factura' y esa colisión NO es discriminable — se acepta a conciencia.
# Un tipo que no está en el mapa NO desmiente nada (se comporta como hoy): preferimos no
# inventar dudas sobre familias que no sabemos clasificar.
FAMILIA_SII = {
    33: "factura", 34: "factura", 46: "factura",   # afecta · exenta · factura de compra
    39: "boleta", 41: "boleta",                    # boleta afecta · exenta
    56: "nota", 61: "nota",                        # nota de débito · de crédito
}


def _puede_ser_dte_del_sii(origen, moneda, tipo_doc) -> bool:
    """¿Esta compra del ERP podría ser un documento del libro del SII?

    MISMO criterio que ya usa la cuadratura mensual para `erp_comparable` (más abajo
    en /resumen): nacional (no EMBARQUE), en CLP y con documento tributario. Una caja
    chica 'sin_documento' o la factura de un proveedor extranjero JAMÁS van a aparecer
    en el libro de compras del SII.
    """
    return (str(origen or "MANUAL").upper() != "EMBARQUE"
            and str(moneda or "CLP").upper() == "CLP"
            and str(tipo_doc or "factura").strip().lower()
            not in ("recibo", "sin_documento"))


def _llaves_erp(db: Session):
    """(matches, blandos, ruts_sin_doc, folios_sin_rut, montos_sin_llave) de las compras
    ACTIVAS de minería — el lado ERP del cruce, calculado UNA vez por request (no por
    documento).

    `matches`: (rut_canónico, N° de documento) → {tipos de documento del ERP}. El TIPO
    viaja en el valor por el hallazgo #11 (ver FAMILIA_SII).

    `blandos` (hallazgo confirmado #1): índice ADICIONAL por (rut_canónico,
    folio_blando) → {números de documento originales del ERP}. Un doc del libro cuyo
    folio calza SOLO en blando ('4071' vs '0004071' tecleado del PDF) NO puede caer en
    NO_ESTA: iría directo al botón «registrar» y nacería la CxP duplicada que Tesorería
    puede pagar dos veces. Cae en INDETERMINADO con leyenda.

    `montos_sin_llave` (hallazgo #14): {monto CLP redondeado → cuántas compras}. Ver el
    porqué del cambio de diseño junto al bucle.

    Import LOCAL de ContCompra (mismo patrón que los guards de monza_contabilidad): evita
    acoplar este paquete a compras_contab en el arranque.
    """
    from compras_contab.models import ContCompra
    filas = (db.query(ContCompra.proveedor_rut, ContCompra.numero_documento,
                      ContCompra.tipo_doc, ContCompra.origen, ContCompra.moneda,
                      ContCompra.monto_total_clp, ContCompra.monto_total)
             .filter(ContCompra.empresa == "mineria", ContCompra.anulado.is_(False))
             .all())
    ruts_sin_doc, folios_sin_rut = set(), set()
    matches: dict = {}
    blandos: dict = {}
    montos_sin_llave: dict = {}
    for rut_txt, numdoc, tipo_doc, origen, moneda, clp, total in filas:
        # Canonización TOLERANTE (hallazgo #9): con la estricta, un RUT tecleado
        # '96.631.520 2' se rendía y la compra caía en el saco de «sin llave» aunque
        # estuviera perfectamente registrada.
        canon = rut_canon_erp(rut_txt)
        doc = (str(numdoc).strip() if numdoc is not None else "")
        tipo = str(tipo_doc or "factura").strip().lower()
        if canon and doc:
            matches.setdefault((canon, doc), set()).add(tipo)
            blando = folio_blando(doc)
            if blando:
                blandos.setdefault((canon, blando), set()).add(doc)
        elif canon:
            ruts_sin_doc.add(canon)      # compra del proveedor sin N°: indeterminado por RUT
        elif doc:
            folios_sin_rut.add(doc)      # compra con N° pero sin RUT: indeterminado por folio
        elif _puede_ser_dte_del_sii(origen, moneda, tipo):
            # ── HALLAZGO #14: la duda se ACOTA, ya no se universaliza ────────────────
            # Antes bastaba UNA compra sin RUT y sin N° para que TODO el libro cayera en
            # INDETERMINADO: la tarjeta «No está — la deuda invisible» marcaba 0, y la
            # única pregunta que este módulo existe para responder dejaba de tener
            # respuesta. Un acantilado de una fila, y disparado por el caso más normal
            # que hay (una caja chica sin documento).
            #
            # DISEÑO NUEVO, en dos cortes:
            #   1) La compra tiene que PODER ser un DTE del SII (_puede_ser_dte_del_sii).
            #      Una caja chica 'sin_documento' o un embarque en USD no pueden serlo, y
            #      hoy envenenaban igual: eso no era prudencia, era ruido.
            #   2) De las que sí podrían serlo, la duda alcanza SOLO a los documentos que
            #      esa compra podría ser, y el discriminante es el MONTO (±$1). No se usa
            #      la fecha a propósito: `cont_compra.fecha` es la de registro tanto como
            #      la del documento, y un rango de fechas dejaría fuera a la compra que
            #      de verdad calza. El monto es el dato que el operador no suele errar.
            #
            # Y cuando la duda cae, ahora VIENE CON LEYENDA (ver _cubeta): antes el
            # tablero mostraba el número de compras sin llave y el operador no tenía cómo
            # relacionarlo con la fila que estaba mirando.
            monto = clp if clp is not None else total
            llave = int(round(float(monto or 0)))
            montos_sin_llave[llave] = montos_sin_llave.get(llave, 0) + 1
    return matches, blandos, ruts_sin_doc, folios_sin_rut, montos_sin_llave


def _cubeta(doc: SiiLibroDoc, matches, blandos, ruts_sin_doc, folios_sin_rut,
            montos_sin_llave):
    """→ (cubeta, detalle). `detalle` narra POR QUÉ un caso cayó en INDETERMINADO
    (hallazgos #1, #11 y #14) — la leyenda que el operador necesita para revisar en vez
    de registrar de nuevo o de dar por registrado lo que no está."""
    folio = (doc.folio or "").strip()
    canon = doc.rut_emisor_canonico
    tipos_erp = matches.get((canon, folio)) if (canon and folio) else None
    if tipos_erp:
        # Hallazgo #11: el mismo RUT y el mismo N° pueden ser DOS documentos distintos si
        # son de tipos distintos (series de folio independientes). Con familias que no
        # coinciden NO se puede afirmar «ESTA»… pero tampoco «NO_ESTA»: el alta de
        # compras identifica por (empresa, RUT, N°) y respondería 409 al botón
        # «registrar», dejando al operador sin salida. La respuesta honesta es la
        # tercera cubeta, con la contradicción escrita.
        familia = FAMILIA_SII.get(doc.sii_document_type_id)
        if familia is None or familia in tipos_erp:
            return "ESTA", None
        return "INDETERMINADO", (
            f"el ERP tiene el N° {folio} de este mismo RUT, pero registrado como "
            f"{'/'.join(sorted(tipos_erp))}, y este documento del SII es una "
            f"{familia} (tipo {doc.sii_document_type_id}): las series de folio del SII "
            "son por TIPO de documento, así que pueden ser dos documentos distintos con "
            "el mismo número — revísalo antes de darlo por registrado")
    # Hallazgo #1: match BLANDO pero no estricto (ceros a la izquierda / prefijos como
    # 'F-4071') → INDETERMINADO con leyenda, JAMÁS directo a NO_ESTA.
    if canon and folio:
        blando = folio_blando(folio)
        parecidos = blandos.get((canon, blando)) if blando else None
        if parecidos:
            return "INDETERMINADO", (
                "existe una compra del mismo RUT con folio parecido "
                f"({', '.join(repr(p) for p in sorted(parecidos))} vs '{folio}'): "
                "puede ser el mismo documento tecleado distinto — revisar antes de "
                "registrar de nuevo")
    # Hallazgo #14: compras sin RUT ni N° que PODRÍAN ser este documento — el criterio
    # es el monto, no «existe alguna en todo el ERP».
    if montos_sin_llave:
        if doc.monto_efectivo is None:
            # Sin monto legible no hay con qué descartar: se conserva la duda (es el
            # documento malformado del barrido, que ya viaja marcado).
            return "INDETERMINADO", (
                f"hay {sum(montos_sin_llave.values())} compra(s) del ERP sin RUT ni N° "
                "de documento, y este documento del SII no trae monto legible: no se "
                "puede concluir — complete el N° de esas compras")
        m = int(round(abs(float(doc.monto_efectivo))))
        cuantas = sum(montos_sin_llave.get(k, 0) for k in (m - 1, m, m + 1))
        if cuantas:
            monto_txt = f"{m:,}".replace(",", ".")
            return "INDETERMINADO", (
                f"hay {cuantas} compra(s) del ERP sin RUT ni N° de documento por este "
                f"mismo monto (${monto_txt}): podrían ser este documento — complete el "
                "RUT y el N° de esa compra para poder concluir")
    if (canon and canon in ruts_sin_doc) or (folio and folio in folios_sin_rut):
        return "INDETERMINADO", None
    if not canon or not folio:
        # El documento MISMO viene sin llave (raro: un recibido sin folio) — tampoco se
        # puede concluir.
        return "INDETERMINADO", None
    return "NO_ESTA", None


def _csv_seguro(v):
    """Neutraliza la INYECCIÓN DE FÓRMULAS del CSV (hallazgo ALTO, lente de seguridad
    2026-08-08).

    El nombre del emisor lo escribe un TERCERO (viene del SII, no de nuestro ERP). Excel
    y LibreOffice ejecutan como fórmula toda celda que empieza con '=', '+', '-' o '@',
    así que un proveedor llamado `=HYPERLINK("http://…","Ver")` — o algo peor — se
    ejecuta en el computador del contador cuando abre el reporte. El que abre el archivo
    es justamente quien no puede defenderse: confía en que el CSV lo emitió su ERP.

    Se antepone una comilla simple, que es la marca que las planillas entienden como
    «esto es texto, no fórmula». Solo toca STRINGS: los números los generamos nosotros
    (un monto negativo debe seguir siendo número, no texto).

    Duplicado a propósito en el paquete de cada marca — nunca un helper compartido entre
    wasabil_compras y monza_wasabil_compras.
    """
    if not isinstance(v, str) or not v:
        return v
    # El espacio/tab inicial no salva: la planilla lo ignora y evalúa igual.
    if v.lstrip("\t\r\n ")[:1] in ("=", "+", "-", "@"):
        return "'" + v
    return v


def _reglas_por_rut(db: Session) -> dict:
    return {r.rut_canonico: r for r in db.query(SiiLibroReglaRut).all()}


def _serializar(doc: SiiLibroDoc, cubeta: str, regla: Optional[SiiLibroReglaRut],
                cubeta_detalle: Optional[str] = None) -> dict:
    return {
        "id": doc.id,
        "uuid": doc.uuid,
        "tipo": doc.sii_document_type_id,
        "folio": doc.folio,
        "fecha": doc.document_date.isoformat() if doc.document_date else None,
        "rut": doc.rut_emisor_canonico,
        "rut_formateado": rut_formateado(doc.rut_emisor_canonico),
        "emisor": doc.receiver_name,
        "monto_efectivo": float(doc.monto_efectivo or 0),
        "neto": float(doc.sent_nsubtotal or 0),
        "iva": float(doc.sent_niva or 0),
        "exento": float(doc.sent_nexempt or 0),
        "trx_sign": doc.trx_sign,
        "es_nota_credito": doc.trx_sign == -1,
        "exchange_status": doc.exchange_status,
        "estado_espejo": doc.estado_espejo,
        "cubeta": cubeta,
        # Leyenda del INDETERMINADO por folio blando (hallazgo #1) — clave NUEVA,
        # aditiva: el frontend viejo la ignora sin romperse.
        "cubeta_detalle": cubeta_detalle,
        "decision": doc.decision,
        "destino": doc.destino,
        "decision_motivo": doc.decision_motivo,
        "divergente": doc.divergente,
        "divergencia_detalle": doc.divergencia_detalle,
        "regla_rut": regla.nivel if regla else None,
        "regla_destino_default": regla.destino_default if regla else None,
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/resumen")
def resumen(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    """El tablero de control: la primera pantalla, y la que dice si se puede confiar.

    Los cinco números del plan: faltantes, edad del último barrido exitoso (rojo a 48 h),
    Σ libro vs Σ CxP por mes, pendientes de decisión, y divergentes/desaparecidos.
    """
    ultimo_ok = (db.query(SiiLibroSyncRun)
                 .filter(SiiLibroSyncRun.exito.is_(True))
                 .order_by(SiiLibroSyncRun.id.desc()).first())
    ultimo = db.query(SiiLibroSyncRun).order_by(SiiLibroSyncRun.id.desc()).first()
    edad_horas = None
    if ultimo_ok and ultimo_ok.terminado_at:
        edad_horas = round((datetime.utcnow() - ultimo_ok.terminado_at)
                           .total_seconds() / 3600, 1)

    docs = db.query(SiiLibroDoc).filter(SiiLibroDoc.estado_espejo == "ACTIVO").all()
    matches, blandos, ruts_sd, folios_sr, montos_sl = _llaves_erp(db)
    cubetas = {"ESTA": 0, "NO_ESTA": 0, "INDETERMINADO": 0}
    pendientes = divergentes = 0
    for d in docs:
        cb, _det = _cubeta(d, matches, blandos, ruts_sd, folios_sr, montos_sl)
        cubetas[cb] += 1
        if d.decision is None:
            pendientes += 1
        if d.divergente:
            divergentes += 1

    # ── Σ libro vs Σ CxP por mes (los últimos 6): el número de cuadratura del controller.
    #
    # DESGLOSADA (hallazgos confirmados 2026-08-06 #2 y #5):
    #   · #2: el lado ERP sumaba monto_total EN LA MONEDA del documento (una compra
    #     USD 45.000 aportaba «45.000 pesos» en vez de sus 42,3M CLP) e incluía compras
    #     que JAMÁS estarán en el libro SII (embarques de proveedor extranjero, gastos
    #     sin documento). Ahora suma monto_total_clp (la base de AP) y separa
    #     erp_comparable de erp_fuera_libro en vez de esconder lo excluido.
    #   · #5: el lado libro incluía NC y docs 'ignorado' contra un ERP que no puede
    #     registrar NC (el propio prefill lo prohíbe con 409) → la diferencia NUNCA era
    #     0 y perdía poder de alarma. `diferencia_explicada` = libro − ignorados − nc −
    #     erp_comparable devuelve el 0 alcanzable sin esconder nada.
    # Las claves VIEJAS (mes/libro/erp/diferencia) se conservan — el frontend actual las
    # lee; las nuevas son aditivas.
    from compras_contab.models import ContCompra
    mes_libro = func.date_format(SiiLibroDoc.document_date, "%Y-%m")
    filas_libro = (db.query(
                       mes_libro,
                       func.sum(SiiLibroDoc.monto_efectivo),
                       # ignorados SOLO con signo + : una NC ignorada ya viaja en la
                       # columna NC — restarla dos veces inventaría diferencia.
                       func.sum(case((and_(SiiLibroDoc.decision == "ignorado",
                                           SiiLibroDoc.trx_sign == 1),
                                      SiiLibroDoc.monto_efectivo), else_=0)),
                       func.sum(case((SiiLibroDoc.trx_sign == -1,
                                      SiiLibroDoc.monto_efectivo), else_=0)))
                   .filter(SiiLibroDoc.estado_espejo == "ACTIVO")
                   .group_by(mes_libro).all())
    libro_por_mes = {m: (float(total or 0), float(ign or 0), float(nc or 0))
                     for m, total, ign, nc in filas_libro}

    # ERP en CLP SIEMPRE (monto_total_clp; coalesce a monto_total solo como red para
    # filas legadas sin la columna poblada — en CLP ambas coinciden). `comparable` =
    # lo que PODRÍA aparecer en el libro del SII: nacional (no EMBARQUE), en CLP y con
    # documento tributario. NULL en cualquier criterio cae a `fuera_libro` (no se puede
    # afirmar comparabilidad — la duda no infla la cuadratura).
    clp = func.coalesce(ContCompra.monto_total_clp, ContCompra.monto_total)
    comparable = and_(func.coalesce(ContCompra.origen, "MANUAL") != "EMBARQUE",
                      func.coalesce(ContCompra.moneda, "CLP") == "CLP",
                      func.coalesce(ContCompra.tipo_doc, "factura")
                          .notin_(("recibo", "sin_documento")))
    mes_erp = func.date_format(ContCompra.fecha, "%Y-%m")
    filas_erp = (db.query(mes_erp, func.sum(clp),
                          func.sum(case((comparable, clp), else_=0)))
                 .filter(ContCompra.empresa == "mineria",
                         ContCompra.anulado.is_(False))
                 .group_by(mes_erp).all())
    erp_por_mes = {m: (float(total or 0), float(comp or 0))
                   for m, total, comp in filas_erp}

    # Un documento o una compra SIN fecha agrupan bajo None: se excluyen del cuadre
    # mensual (no hay mes al que imputarlos) — pero no se esconden: viajan aparte.
    sin_fecha = (libro_por_mes.pop(None, (0, 0, 0))[0]
                 + erp_por_mes.pop(None, (0, 0))[0])
    meses = sorted(set(libro_por_mes) | set(erp_por_mes), reverse=True)[:6]
    cuadratura = []
    for m in meses:
        libro_total, libro_ign, libro_nc = libro_por_mes.get(m, (0.0, 0.0, 0.0))
        erp_total, erp_comp = erp_por_mes.get(m, (0.0, 0.0))
        cuadratura.append({
            "mes": m,
            "libro": libro_total,                       # clave vieja (Σ libro del mes)
            "erp": erp_total,                           # clave vieja, AHORA en CLP (#2)
            "diferencia": libro_total - erp_total,      # clave vieja, ahora comparable
            "libro_total": libro_total,
            "libro_ignorados": libro_ign,
            "libro_nc": libro_nc,
            "erp_comparable": erp_comp,
            "erp_fuera_libro": erp_total - erp_comp,
            "diferencia_explicada": libro_total - libro_ign - libro_nc - erp_comp,
        })

    return {
        "ultimo_barrido": {
            "exito": ultimo.exito if ultimo else None,
            "origen": ultimo.origen if ultimo else None,
            "terminado_at": (ultimo.terminado_at.isoformat()
                             if ultimo and ultimo.terminado_at else None),
            "error": ultimo.error if ultimo else None,
            "total_api": ultimo.total_api if ultimo else None,
        } if ultimo else None,
        "edad_horas_ultimo_exitoso": edad_horas,
        "edad_critica": edad_horas is None or edad_horas > 48,   # rojo a las 48 h
        "documentos_activos": len(docs),
        "cubetas": cubetas,
        "pendientes_decision": pendientes,
        "divergentes": divergentes,
        "desaparecidos": db.query(func.count(SiiLibroDoc.id))
                           .filter(SiiLibroDoc.estado_espejo == "DESAPARECIDO").scalar(),
        # Subtítulo de la tarjeta INDETERMINADO: compras del ERP sin llave con qué
        # cruzar — sin N° de documento, sin RUT, o sin ninguno de los dos. Tras el #14
        # las «sin ninguno de los dos» solo se cuentan si PODRÍAN ser un DTE del SII
        # (una caja chica 'sin_documento' no lo es y ya no infla este número ni la
        # cubeta).
        "compras_erp_sin_llave": (sum(montos_sl.values())
                                  + len(ruts_sd) + len(folios_sr)),
        "monto_sin_fecha": sin_fecha,
        "cuadratura_mensual": cuadratura,
        # El criterio del desglose, para el tooltip del tablero (#2/#5): el número que
        # debe dar 0 es diferencia_explicada, no la diferencia bruta.
        "cuadratura_criterio": (
            "libro y ERP en CLP. erp_comparable = compras nacionales (no EMBARQUE), "
            "en CLP y con documento tributario; el resto viaja en erp_fuera_libro. "
            "diferencia_explicada = libro − ignorados − notas de crédito − "
            "erp_comparable: es la que debe dar $0"),
        "centros_sugeridos": CENTROS_SUGERIDOS,
    }


@router.post("/sincronizar")
def sincronizar_ahora(db: Session = Depends(get_db),
                      current_user: User = Depends(get_current_user)):
    """Barrido manual. El mismo camino que el nocturno; el guard anti-solape responde 409.

    ENFRIAMIENTO (hallazgo MEDIO, lente de seguridad 2026-08-08): el anti-solape impide
    dos barridos SIMULTÁNEOS, pero no dos seguidos. Un botón con doble clic — o una
    pestaña que reintenta sola — dispara llamadas REALES a Wasabil en cadena, y ese
    consumo lo paga la cuenta de la empresa. El SII no publica documentos nuevos cada
    minuto, así que barrer de nuevo tan pronto no puede traer nada distinto.

    Solo enfría después de un barrido EXITOSO: si el último falló, el operador tiene que
    poder reintentar en el acto (arregló el token, volvió la red) sin esperar castigo.
    """
    ultimo = (db.query(SiiLibroSyncRun)
              .order_by(SiiLibroSyncRun.id.desc()).first())
    if ultimo is not None and ultimo.exito is True and ultimo.iniciado_at:
        faltan = COOLDOWN_MANUAL_SEG - (
            datetime.utcnow() - ultimo.iniciado_at).total_seconds()
        if faltan > 0:
            raise HTTPException(429, (
                f"El libro ya se sincronizó hace {int(COOLDOWN_MANUAL_SEG - faltan)} "
                f"segundos y salió bien. Espera {int(faltan) + 1} segundos: el SII no "
                "publica documentos nuevos tan seguido, así que volver a barrer ahora "
                "traería exactamente lo mismo."))
    try:
        run = ejecutar_barrido(db, origen="manual",
                               usuario_id=getattr(current_user, "id", None))
    except BarridoEnCurso as e:
        raise HTTPException(409, str(e))
    return {
        "exito": run.exito,
        "error": run.error,
        "total_api": run.total_api,
        "nuevos": run.nuevos,
        "actualizados": run.actualizados,
        "desaparecidos": run.desaparecidos,
    }


@router.get("/documentos")
def bandeja(
    cubeta: Optional[str] = Query(None, description="ESTA | NO_ESTA | INDETERMINADO"),
    decision: Optional[str] = Query(None, description="pendiente | ignorado | clasificado"),
    rut: Optional[str] = Query(None),
    solo_divergentes: bool = Query(False),
    estado: str = Query("ACTIVO", description="ACTIVO (default) | DESAPARECIDO"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """La bandeja de la Fase A2. El cruce se calcula EN VIVO contra las compras activas
    (una pasada por request, no por fila): una compra registrada hace un minuto mueve el
    documento de cubeta sin esperar ningún barrido.

    `estado=DESAPARECIDO` (hallazgo confirmado 2026-08-06 #4: «DESAPARECIDO es invisible:
    solo existe como contador») lista CUÁLES documentos dejaron de venir del SII, con sus
    decisiones — el «rastro intacto» de la Regla 5 accesible desde la app, no solo por
    SQL. Su cubeta viaja como 'N/A': el SII ya no los declara, el cruce vivo no aplica.
    """
    if estado not in ("ACTIVO", "DESAPARECIDO"):
        raise HTTPException(400, f"estado inválido '{estado}': usa ACTIVO | DESAPARECIDO")
    q = db.query(SiiLibroDoc).filter(SiiLibroDoc.estado_espejo == estado)
    if rut:
        canon = rut_canonico(rut)
        if canon is None:
            raise HTTPException(400, f"'{rut}' no tiene forma de RUT")
        q = q.filter(SiiLibroDoc.rut_emisor_canonico == canon)
    if decision == "pendiente":
        q = q.filter(SiiLibroDoc.decision.is_(None))
    elif decision in ("ignorado", "clasificado"):
        q = q.filter(SiiLibroDoc.decision == decision)
    if solo_divergentes:
        q = q.filter(SiiLibroDoc.divergente.is_(True))
    docs = q.order_by(SiiLibroDoc.document_date.desc(), SiiLibroDoc.id.desc()).all()

    matches, blandos, ruts_sd, folios_sr, montos_sl = _llaves_erp(db)
    reglas = _reglas_por_rut(db)
    filas = []
    for d in docs:
        if estado == "DESAPARECIDO":
            cb, det = "N/A", "el SII ya no declara este documento"
        else:
            cb, det = _cubeta(d, matches, blandos, ruts_sd, folios_sr, montos_sl)
        if cubeta and cb != cubeta:
            continue
        filas.append(_serializar(d, cb, reglas.get(d.rut_emisor_canonico), det))
    total = len(filas)
    inicio = (page - 1) * page_size
    return {"items": filas[inicio:inicio + page_size], "total": total,
            "page": page, "page_size": page_size}


@router.get("/documentos/{doc_id}/prefill-compra")
def prefill_compra(doc_id: int, db: Session = Depends(get_db),
                   _: User = Depends(get_current_user)):
    """Datos del documento con los NOMBRES EXACTOS de CompraCreate (compras_contab/
    schemas.py) para pre-llenar el formulario NORMAL de Compras y Pagos.

    NO crea nada: el operador revisa y guarda por el camino de siempre, con TODOS los
    guards del alta (anti-duplicado por RUT+N° incluido) intactos — solo se ahorra
    teclear lo que el sistema ya sabe. Al guardarse la compra, el cruce en vivo de la
    bandeja mueve este documento a la cubeta ESTA solo.
    """
    doc = db.query(SiiLibroDoc).filter(SiiLibroDoc.id == doc_id).first()
    if not doc:
        raise HTTPException(404, "Documento no encontrado en el espejo")
    if doc.estado_espejo != "ACTIVO":
        # Hallazgo #4: con un id viejo (pestaña sin refrescar) se podía pre-llenar una
        # compra desde un documento que el SII ya no declara — el mismo guard que
        # router_match.py ya aplicaba al confirmar un match, y acá faltaba.
        raise HTTPException(
            409, f"Este documento está {doc.estado_espejo} en el espejo: el SII ya no "
                 "lo declara. Refresca la bandeja; si reaparece en un barrido, revive "
                 "solo.")
    if doc.trx_sign == -1:
        # Misma razón que el guard de clasificación: la NC RESTA del libro — registrarla
        # como compra la sumaría a CxP y el doble error cuadraría de casualidad.
        raise HTTPException(
            409, "Este documento es una NOTA DE CRÉDITO: resta del libro y no se "
                 "registra como compra. Aplícala contra la compra original o "
                 "clasifícala en la bandeja.")

    # proveedor_id si existe un Proveedor con ese RUT (para preseleccionarlo en el form).
    # Se compara por CANÓNICO, no por igualdad de texto: un proveedor legado puede tener
    # el RUT guardado con puntos y aun así es el mismo emisor.
    proveedor_id = None
    if doc.rut_emisor_canonico:
        for pid, rut_txt in (db.query(Proveedor.id, Proveedor.rut)
                             .filter(Proveedor.rut.isnot(None)).all()):
            if rut_canonico(rut_txt) == doc.rut_emisor_canonico:
                proveedor_id = pid
                break

    # La MISMA cubeta que muestra la bandeja, calculada con el mismo cruce (una sola
    # definición de la verdad: si divergieran, la pantalla diría una cosa y el
    # formulario otra).
    matches, blandos, ruts_sd, folios_sr, montos_sl = _llaves_erp(db)
    cubeta, cubeta_detalle = _cubeta(doc, matches, blandos, ruts_sd, folios_sr,
                                     montos_sl)

    neto = float(doc.sent_nsubtotal or 0)
    exento = float(doc.sent_nexempt or 0)
    return {
        "proveedor_id": proveedor_id,
        "acreedor": doc.receiver_name,
        # CANÓNICO ('76513680-6'), NO formateado (hallazgo #1): el anti-duplicado del
        # alta de compras compara strings — '76.513.680-6' con puntos burlaba el guard
        # contra una compra vieja guardada '76513680-6' aunque el folio fuera idéntico,
        # y nacían DOS CxP pagables por la misma factura. El canónico es la llave de
        # cruce de TODO el paquete; la pantalla formatea al mostrar si quiere.
        "proveedor_rut": doc.rut_emisor_canonico,
        "numero_documento": doc.folio,
        "fecha": doc.document_date.isoformat() if doc.document_date else None,
        # El formulario solo tiene neto + IVA: la porción EXENTA viaja dentro del neto
        # (también es base sin IVA) para que neto + IVA == total del documento SII.
        "monto_neto": neto + exento,
        "iva": float(doc.sent_niva or 0),
        "monto_total": float(doc.sent_ntotal or 0),
        # LA ADVERTENCIA VIAJA CON LOS DATOS (lente de usabilidad 2026-08-08). La bandeja
        # ya sabe que existe una compra con folio PARECIDO ('0004071' vs '4071'), pero esa
        # leyenda se quedaba en la lista: el operador apretaba «registrar», el formulario
        # se abría limpio y la advertencia desaparecía justo en la pantalla donde se
        # fabrica el duplicado. Mandarla acá permite que el alta la muestre y pida
        # confirmación explícita antes de guardar. Es informativa: los guards de verdad
        # siguen siendo los del alta.
        "cubeta": cubeta,
        "cubeta_detalle": cubeta_detalle,
    }


@router.post("/documentos/{doc_id}/decision")
def decidir(doc_id: int, body: DecisionIn, db: Session = Depends(get_db),
            current_user: User = Depends(get_current_user)):
    """Decide UN documento: ignorar, clasificar (costo_venta | cc:*) o volver a pendiente.

    Guard del controller: un documento cuyo RUT está BLOQUEADO no puede clasificarse como
    costo_venta — es el candado duro contra «la comisión del banco colgada a un embarque
    para que cuadre» (y contra la compra de moneda de la corredora, que es plata grande).
    """
    doc = db.query(SiiLibroDoc).filter(SiiLibroDoc.id == doc_id).first()
    if not doc:
        raise HTTPException(404, "Documento no encontrado en el espejo")
    if doc.estado_espejo != "ACTIVO":
        # Hallazgo #4: decidir() no miraba estado_espejo — con un id de pestaña vieja
        # se decidía (y _sellar_decision apagaba `divergente`) sobre un documento que
        # el SII ya no declara. Mismo 409 que el confirm del matcher.
        raise HTTPException(
            409, f"Este documento está {doc.estado_espejo} en el espejo: el SII ya no "
                 "lo declara y no se decide sobre un fantasma. Refresca la bandeja; si "
                 "reaparece en un barrido, revive solo.")
    if body.accion == "pendiente":
        doc.decision = None
        doc.destino = None
        doc.decision_motivo = None
        doc.decision_usuario_id = None
        doc.decision_at = None
        doc.hash_al_decidir = None
        doc.divergente = False
        doc.divergencia_detalle = None
    elif body.accion == "ignorar":
        doc.decision = "ignorado"
        doc.destino = None
        _sellar_decision(doc, body, current_user)
    elif body.accion == "clasificar":
        destino = (body.destino or "").strip()
        if destino not in (DESTINO_COSTO_VENTA, DESTINO_ACTIVO_FIJO) \
                and not destino.startswith("cc:"):
            raise HTTPException(
                400, f"Destino inválido '{destino}': usa '{DESTINO_COSTO_VENTA}', "
                     f"'{DESTINO_ACTIVO_FIJO}' o 'cc:<centro>' (el catálogo sugerido "
                     "viaja en /resumen)")
        if destino == DESTINO_COSTO_VENTA:
            regla = (db.query(SiiLibroReglaRut)
                     .filter(SiiLibroReglaRut.rut_canonico == doc.rut_emisor_canonico)
                     .first())
            if regla and regla.nivel == "BLOQUEADO":
                raise HTTPException(
                    409, f"El emisor {doc.receiver_name or doc.rut_emisor_canonico} está "
                         f"BLOQUEADO para costo de mercadería"
                         f"{' (' + regla.motivo + ')' if regla.motivo else ''}: su "
                         "documento no puede capitalizar a un embarque. Clasifícalo a un "
                         "centro de costos.")
            if doc.trx_sign == -1:
                # Regla 20 del plan: la NC no se cuelga directo a un embarque en esta
                # entrega — reduce el costo de lo ya prorrateado y eso es de la Fase B.
                raise HTTPException(
                    409, "Una nota de crédito no se clasifica como costo por venta en "
                         "esta fase: redúcela contra el gasto del período o espera la "
                         "fase de vínculo con embarques.")
        doc.decision = "clasificado"
        doc.destino = destino
        _sellar_decision(doc, body, current_user)
    else:
        raise HTTPException(400, f"Acción inválida '{body.accion}'")
    db.commit()
    logger.info("Libro SII: doc %s (%s folio %s) → %s %s por usuario %s",
                doc.id, doc.rut_emisor_canonico, doc.folio, doc.decision,
                doc.destino or "", getattr(current_user, "id", None))
    return {"ok": True, "decision": doc.decision, "destino": doc.destino}


def _sellar_decision(doc: SiiLibroDoc, body: DecisionIn, user) -> None:
    doc.decision_motivo = (body.motivo or "").strip() or None
    doc.decision_usuario_id = getattr(user, "id", None)
    doc.decision_at = datetime.utcnow()
    doc.hash_al_decidir = doc.hash_remoto
    doc.divergente = False
    doc.divergencia_detalle = None


@router.get("/reglas-rut")
def listar_reglas(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return [{
        "id": r.id, "rut": r.rut_canonico, "rut_formateado": rut_formateado(r.rut_canonico),
        "nivel": r.nivel, "destino_default": r.destino_default, "motivo": r.motivo,
    } for r in db.query(SiiLibroReglaRut).order_by(SiiLibroReglaRut.id).all()]


@router.post("/reglas-rut")
def upsert_regla(body: ReglaRutIn, db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    """Crea o actualiza la regla del RUT (una por emisor). El nivel es el DEFAULT de la
    bandeja; la decisión por documento siempre manda encima (decisión del dueño)."""
    canon = rut_canonico(body.rut)
    if canon is None:
        raise HTTPException(400, f"'{body.rut}' no tiene forma de RUT")
    if body.nivel not in NIVELES_REGLA:
        raise HTTPException(400, f"Nivel inválido '{body.nivel}': usa "
                                 f"{' | '.join(sorted(NIVELES_REGLA))}")
    regla = (db.query(SiiLibroReglaRut)
             .filter(SiiLibroReglaRut.rut_canonico == canon).first())
    if not regla:
        regla = SiiLibroReglaRut(rut_canonico=canon)
        db.add(regla)
    regla.nivel = body.nivel
    regla.destino_default = (body.destino_default or "").strip() or None
    regla.motivo = (body.motivo or "").strip() or None
    regla.usuario_id = getattr(current_user, "id", None)
    db.commit()
    return {"ok": True, "rut": canon, "nivel": regla.nivel}


@router.post("/proveedores/{proveedor_id}/asociar-rut")
def asociar_rut_proveedor(proveedor_id: int, rut: str = Query(...),
                          db: Session = Depends(get_db),
                          current_user: User = Depends(get_current_user)):
    """Completa proveedores.rut desde la bandeja (el camino SIN backfill de la Fase 0):
    el operador ve el documento del SII, reconoce al proveedor y lo asocia con un clic.
    Escribe canónico validado — nunca texto libre."""
    from .rut import rut_valido
    canon = rut_canonico(rut)
    if canon is None or not rut_valido(canon):
        raise HTTPException(400, f"'{rut}' no es un RUT válido (módulo 11)")
    prov = db.query(Proveedor).filter(Proveedor.id == proveedor_id).first()
    if not prov:
        raise HTTPException(404, "Proveedor no encontrado")
    if prov.rut and prov.rut != canon:
        raise HTTPException(
            409, f"El proveedor ya tiene el RUT {rut_formateado(prov.rut)}: no se pisa "
                 f"con {rut_formateado(canon)}. Si el registrado está mal, corrígelo en "
                 "la ficha del proveedor con conocimiento de causa.")
    prov.rut = canon
    db.commit()
    logger.info("Libro SII: proveedor %s asociado al RUT %s por usuario %s",
                proveedor_id, canon, getattr(current_user, "id", None))
    return {"ok": True, "proveedor_id": proveedor_id, "rut": canon}


@router.get("/export.csv")
def exportar_faltantes(estado: str = Query("ACTIVO",
                                           description="ACTIVO (default) | DESAPARECIDO"),
                       db: Session = Depends(get_db),
                       _: User = Depends(get_current_user)):
    """El reporte exportable de la Fase A2: los documentos que NO están en el ERP (y los
    indeterminados, marcados como tales — excluirlos sería esconder la duda).

    `estado=DESAPARECIDO` (hallazgo #4): exporta los que dejaron de venir del SII, con
    cubeta 'N/A' y sus decisiones — antes solo existían como contador."""
    if estado not in ("ACTIVO", "DESAPARECIDO"):
        raise HTTPException(400, f"estado inválido '{estado}': usa ACTIVO | DESAPARECIDO")
    docs = (db.query(SiiLibroDoc).filter(SiiLibroDoc.estado_espejo == estado)
            .order_by(SiiLibroDoc.document_date.desc()).all())
    matches, blandos, ruts_sd, folios_sr, montos_sl = _llaves_erp(db)
    buf = io.StringIO()
    w = csv.writer(buf)
    # Columna `detalle` al final (aditiva): la leyenda del INDETERMINADO por folio
    # blando (#1) también viaja al reporte — el CSV es para revisar, no para adivinar.
    w.writerow(["cubeta", "tipo", "folio", "fecha", "rut", "emisor",
                "monto_efectivo", "es_nota_credito", "decision", "destino", "detalle"])
    for d in docs:
        if estado == "DESAPARECIDO":
            cb, det = "N/A", "el SII ya no declara este documento"
        else:
            cb, det = _cubeta(d, matches, blandos, ruts_sd, folios_sr, montos_sl)
            if cb == "ESTA":
                continue
        w.writerow([cb, d.sii_document_type_id, _csv_seguro(d.folio),
                    d.document_date.isoformat() if d.document_date else "",
                    rut_formateado(d.rut_emisor_canonico),
                    _csv_seguro(d.receiver_name),
                    float(d.monto_efectivo or 0), "SI" if d.trx_sign == -1 else "NO",
                    d.decision or "pendiente", d.destino or "",
                    _csv_seguro(det or "")])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition":
                 'attachment; filename="libro-sii-faltantes.csv"'})
