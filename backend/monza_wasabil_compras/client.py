"""Cliente REST del libro de compras Monza — SOLO LECTURA contra el API real de Wasabil.

Espejo deliberado de wasabil_compras/client.py (GA): cliente PROPIO de MonzaParts, sin
importar nada de GA. La ÚNICA diferencia de fondo es el token — acá se usa
WASABIL_API_TOKEN_MONZA (la cuenta Wasabil de LOPEZ HERNANDEZ INVERSIONES SPA), el mismo
patrón "no configurado" de monza_wasabil_dte/client.py.

LA RUTA (resuelta el 2026-08-05 leyendo la doc oficial, no tanteando):
    POST {base}/documents/query   ← LISTA documentos. Verbo POST con semántica de consulta.

    GET /documents responde 405 en el API real — ese 405 mantuvo ciego al cinturón anti doble
    emisión durante semanas y bloqueó este plan hasta leer la documentación. La confusión
    histórica: POST /documents (SIN /query) CREA un documento tributario. Este módulo tiene
    PROHIBIDO tocar esa ruta, y la suite lo verifica con una sonda de comportamiento: el fake
    registra cada URL llamada y falla si alguna no termina en /documents/query.

EL CONTRATO DE LECTURA (Regla 2 del plan — fail-closed):
    «Una lectura INCOMPLETA nunca produce conclusiones.» El barrido:
      - captura `total` y `lastPage` de la primera página y recorre TODAS;
      - si una página falla → aborta el barrido ENTERO (no hay resultados parciales);
      - si Σ(ítems) != total, o un uuid se repite entre páginas (la lista se movió mientras
        se recorría: Wasabil ordena siempre DESC y un documento nuevo desplaza a los demás)
        → aborta. El barrido nocturno lo reintenta; datos viejos honestos > frescos mentirosos.
    El envelope REAL es data.list.{items,total,lastPage} — documentado en la foto
    docs/wasabil-api-llms-full-2026-08-05.txt (§ Listar documentos). El auditor adversarial
    predijo que un parser que asumiera otra forma devolvería CERO documentos declarando la
    lectura completa; por eso _extraer_lista falla RUIDOSO si falta cualquier nivel.

TOKEN: el MISMO de la emisión de MonzaParts (WASABIL_API_TOKEN_MONZA de backend/.env, ya
declarado en config.py — cero variables nuevas, cero riesgo de extra_forbidden). Sin token,
el módulo se declara no configurado y el barrido no corre: jamás un guard que "funciona"
sin poder ver.
"""
import logging
from typing import Iterable, List, Optional, Tuple

import httpx

from config import settings

logger = logging.getLogger("monza_wasabil_compras")

TIMEOUT_SEGUNDOS = 30
# Tope documentado del API (perPage máx 250). Menos round-trips por barrido.
PER_PAGE = 250
# Cinturón contra un `lastPage` absurdo o un bucle: 400 páginas × 250 = 100.000 documentos,
# dos órdenes de magnitud sobre el libro real (~400/año). Si se alcanza, algo está MAL y se
# aborta ruidoso en vez de barrer para siempre.
MAX_PAGINAS = 400


class WasabilComprasError(Exception):
    """Cualquier motivo por el que la lectura NO es concluyente (red, HTTP, envelope roto,
    conteo que no cuadra). El llamador debe ABORTAR el barrido: nunca concluir de una
    lectura a medias. Clase PROPIA del paquete Monza (no se importa la de GA)."""


class WasabilComprasNoConfigurado(WasabilComprasError):
    def __init__(self):
        super().__init__(
            "Wasabil no está configurado: falta WASABIL_API_TOKEN_MONZA en backend/.env "
            "(se genera en https://app.wasabil.com/api-tokens)")


def esta_configurado() -> bool:
    return bool((settings.WASABIL_API_TOKEN_MONZA or "").strip())


def _url_query() -> str:
    # ÚNICA ruta del módulo. Construida en un solo lugar para que la sonda de la suite
    # tenga un solo punto que vigilar.
    return settings.WASABIL_API_BASE.rstrip("/") + "/documents/query"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.WASABIL_API_TOKEN_MONZA.strip()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _post_query(body: dict, params: Optional[dict] = None) -> dict:
    """POST a /documents/query. Punto único de red — los tests lo reemplazan por un fake.

    Cualquier problema (timeout, 4xx/5xx, JSON ilegible) es WasabilComprasError: acá no hay
    «best effort». A diferencia de la EMISIÓN (donde un error tras el POST deja un documento
    quizá creado y hay que rescatarlo), una CONSULTA fallida no deja nada colgando: se aborta
    y se reintenta después, entero.
    """
    if not esta_configurado():
        raise WasabilComprasNoConfigurado()
    try:
        with httpx.Client(timeout=TIMEOUT_SEGUNDOS) as http:
            resp = http.post(_url_query(), json=body, params=params or {},
                             headers=_headers())
    except httpx.TimeoutException as e:
        raise WasabilComprasError("Wasabil no respondió a tiempo (timeout)") from e
    except httpx.HTTPError as e:
        raise WasabilComprasError(f"Error de red hablando con Wasabil: {e}") from e
    if resp.status_code != 200:
        raise WasabilComprasError(
            f"Wasabil respondió HTTP {resp.status_code} al listar documentos "
            f"(cuerpo: {resp.text[:300]})")
    try:
        return resp.json()
    except ValueError as e:
        raise WasabilComprasError("Wasabil devolvió una respuesta que no es JSON") from e


def _extraer_lista(data: dict) -> Tuple[List[dict], int, int]:
    """(items, total, lastPage) del envelope data.list — RUIDOSO ante cualquier desvío.

    La tentación es `data.get("data", {}).get("list", {}).get("items", [])`: eso convierte
    un cambio de envelope en «cero documentos, lectura completa», que es exactamente la
    mentira silenciosa que el plan prohíbe (Regla 2). Acá, si falta un nivel o un tipo no
    calza, se levanta el error con el fragmento recibido para diagnosticar.
    """
    if not isinstance(data, dict) or not data.get("success", False):
        raise WasabilComprasError(
            f"Respuesta sin success=true al listar documentos: {str(data)[:300]}")
    lista = (data.get("data") or {}).get("list")
    if not isinstance(lista, dict):
        raise WasabilComprasError(
            "El envelope no trae data.list — ¿cambió el API? Comparar contra la foto "
            f"docs/wasabil-api-llms-full-2026-08-05.txt. Recibido: {str(data)[:300]}")
    items, total, last_page = lista.get("items"), lista.get("total"), lista.get("lastPage")
    if not isinstance(items, list) or not isinstance(total, int) \
            or not isinstance(last_page, int):
        raise WasabilComprasError(
            f"data.list incompleto (items/total/lastPage): {str(lista)[:300]}")
    return items, total, last_page


def query_recibidos_pagina(page: int, *, from_date: Optional[str] = None,
                           to_date: Optional[str] = None,
                           per_page: int = PER_PAGE) -> Tuple[List[dict], int, int]:
    """UNA página de documentos RECIBIDOS de gasto. Devuelve (items, total, lastPage).

    `sortBy=documentDate`: es el orden MÁS estable de los cuatro que ofrece el API para
    recorrer sin que la lista baile — `recentStatus` y `lastCreated` cambian con cada
    documento que llega o se toca. Aun así el orden es DESC y un documento nuevo desplaza:
    de detectar el desplazamiento se encarga el barrido (uuid repetido / conteo).
    `with=supplier`: la ficha del proveedor viaja junto al documento (RUT ya formateado,
    uid estable) y evita una segunda consulta por fila.
    """
    body = {
        "received": True,          # SOLO recibidos: el libro de compras, no lo emitido
        "trxType": "expense",      # gastos (excluye p.ej. guías propias)
        "page": page,
        "perPage": per_page,
        "sortBy": "documentDate",
    }
    if from_date:
        body["fromDocumentDate"] = from_date
    if to_date:
        body["toDocumentDate"] = to_date
    data = _post_query(body, params={"with": "supplier"})
    return _extraer_lista(data)


def barrer_recibidos(*, from_date: Optional[str] = None,
                     to_date: Optional[str] = None,
                     per_page: int = PER_PAGE) -> List[dict]:
    """TODOS los documentos recibidos del rango, o WasabilComprasError. Nunca a medias.

    El contrato completo está en el docstring del módulo. En resumen: total y lastPage se
    fijan con la primera página; toda página se recorre; uuid duplicado o Σ != total ⇒ la
    lista se movió bajo los pies ⇒ abortar (el barrido nocturno reintenta).
    """
    vistos: dict = {}
    items, total, last_page = query_recibidos_pagina(
        1, from_date=from_date, to_date=to_date, per_page=per_page)
    if last_page > MAX_PAGINAS:
        raise WasabilComprasError(
            f"lastPage={last_page} excede el cinturón de {MAX_PAGINAS} páginas — revisar "
            "antes de barrer (¿per_page roto? ¿filtro perdido?)")
    _acumular(vistos, items)
    for page in range(2, last_page + 1):
        items, total_p, _lp = query_recibidos_pagina(
            page, from_date=from_date, to_date=to_date, per_page=per_page)
        if total_p != total:
            # El total cambió a mitad del barrido: llegaron/salieron documentos mientras
            # recorríamos. La foto ya no es consistente.
            raise WasabilComprasError(
                f"El total cambió durante el barrido ({total} → {total_p} en la página "
                f"{page}): la lista se movió; reintentar el barrido completo")
        _acumular(vistos, items)
    if len(vistos) != total:
        raise WasabilComprasError(
            f"Barrido inconsistente: recorrí {len(vistos)} documentos únicos y el API "
            f"declara {total}. No se concluye nada de una lectura a medias")
    return list(vistos.values())


def _acumular(vistos: dict, items: Iterable[dict]) -> None:
    for doc in items:
        uuid = doc.get("uuid")
        if not uuid:
            raise WasabilComprasError(
                f"Documento sin uuid en el listado — sin llave no hay espejo posible: "
                f"{str(doc)[:200]}")
        if uuid in vistos:
            # El mismo documento en dos páginas = la paginación se corrió mientras
            # recorríamos (orden DESC + documento nuevo). Foto inconsistente.
            raise WasabilComprasError(
                f"uuid {uuid} repetido entre páginas: la lista se movió durante el "
                "barrido; reintentar completo")
        vistos[uuid] = doc


def monto_efectivo(doc: dict) -> float:
    """LA única magnitud sumable de un documento del libro (Regla 9 del plan).

    `sent_ntotal` viene SIEMPRE POSITIVO y el signo viaja aparte en `trx_sign` (verificado
    con la NC folio 4 de TRANSPORTES PYP: +45.220.000 con trx_sign=-1). Sumar sent_ntotal
    crudo convierte cada nota de crédito en un CARGO: error del DOBLE de su monto. Toda
    suma del módulo pasa por acá.
    """
    total = doc.get("sent_ntotal")
    signo = doc.get("trx_sign")
    if total is None or signo not in (1, -1):
        raise WasabilComprasError(
            f"Documento sin sent_ntotal/trx_sign legibles — no se puede sumar sin signo "
            f"confiable: uuid={doc.get('uuid')} total={total!r} trx_sign={signo!r}")
    return float(total) * signo
