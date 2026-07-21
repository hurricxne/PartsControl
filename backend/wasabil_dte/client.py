"""Cliente HTTP del API REST de Wasabil (facturador electrónico de GRUPO AM).

Este archivo es el ÚNICO lugar del módulo que habla con Wasabil y que conoce los
nombres de campos del API REST — si Wasabil cambia algo, se corrige aquí.

- Base y token vienen de config.py (backend/.env): WASABIL_API_BASE / WASABIL_API_TOKEN.
  Sin token el módulo queda en modo "no configurado": la previsualización funciona
  (con datos locales) pero emitir devuelve un error claro.
- Auth: header `Authorization: Bearer <TOKEN>`.
- La emisión es ASÍNCRONA: POST /documents devuelve un `uuid`; el estado final
  (Emitido con folio, o Fallido) se consulta con GET /documents/{uuid}/status.

VERIFICADO contra el API real el 2026-07-17 (borrador issue:false, sin tocar el
SII): el envelope `{success, status, data}`, los listados `{items, total,
lastPage}`, la ficha del cliente anidada (`giros[]`/`addresses[]`),
`dispatch_guide {dispatch_type_code}` y `references` aceptados, y el tope de 18
caracteres del folio de referencia. PENDIENTE de la primera emisión real: la
forma exacta de la respuesta al EMITIR (`status_id`/folio/PDF) y el endpoint de
LISTADO (`GET /documents` da 405 — ver buscar_documentos). Cualquier ajuste
queda contenido en `payload_a_rest()` y estas funciones.
"""
import json
from typing import Optional

import httpx

from config import settings

TIMEOUT_SEGUNDOS = 25.0


class WasabilError(Exception):
    """Error hablando con Wasabil (red, HTTP o respuesta inesperada).

    `detalle` guarda el cuerpo crudo de la respuesta cuando existe (auditoría).
    `ambiguo` es CLAVE para el anti doble emisión: True cuando NO se puede saber si
    el documento llegó a crearse en Wasabil (timeout, 5xx, respuesta ilegible) —
    en ese caso el claim "en vuelo" se mantiene hasta expirar. False cuando es
    seguro que NO se creó nada (conexión rechazada, 4xx de validación).
    """

    def __init__(self, mensaje: str, detalle: Optional[str] = None,
                 ambiguo: bool = True, status_code: Optional[int] = None):
        super().__init__(mensaje)
        self.detalle = detalle
        self.ambiguo = ambiguo
        self.status_code = status_code


class WasabilNoConfigurado(WasabilError):
    """No hay WASABIL_API_TOKEN en backend/.env — no se puede llamar a Wasabil."""

    def __init__(self):
        super().__init__(
            "Wasabil no está configurado: falta WASABIL_API_TOKEN en backend/.env "
            "(el token se genera en https://app.wasabil.com/api-tokens)"
        )


def esta_configurado() -> bool:
    """True si hay token configurado (no valida que el token sirva)."""
    return bool((settings.WASABIL_API_TOKEN or "").strip())


def _headers() -> dict:
    if not esta_configurado():
        raise WasabilNoConfigurado()
    return {
        "Authorization": f"Bearer {settings.WASABIL_API_TOKEN.strip()}",
        "Accept": "application/json",
    }


def _url(path: str) -> str:
    return settings.WASABIL_API_BASE.rstrip("/") + path


def _request(metodo: str, path: str, json_body: Optional[dict] = None,
             params: Optional[dict] = None) -> dict:
    """Llamada HTTP con manejo de errores uniforme. Devuelve el JSON de respuesta."""
    try:
        with httpx.Client(timeout=TIMEOUT_SEGUNDOS) as http:
            resp = http.request(metodo, _url(path), headers=_headers(),
                                json=json_body, params=params)
    except httpx.TimeoutException:
        # AMBIGUO: la petición pudo llegar y el documento pudo crearse igual
        raise WasabilError("Wasabil no respondió a tiempo (timeout). "
                           "Verifica el estado del documento antes de reintentar.",
                           ambiguo=True)
    except httpx.ConnectError as e:
        # NO ambiguo: la conexión nunca se estableció, nada llegó a Wasabil
        raise WasabilError(f"No se pudo conectar con Wasabil: {e}", ambiguo=False)
    except httpx.HTTPError as e:
        raise WasabilError(f"Error de red hablando con Wasabil: {e}", ambiguo=True)

    if resp.status_code == 401:
        raise WasabilError("Wasabil rechazó el token (401). Revisa WASABIL_API_TOKEN "
                           "en backend/.env o genera uno nuevo en app.wasabil.com/api-tokens.",
                           detalle=resp.text, ambiguo=False, status_code=401)
    if resp.status_code >= 400:
        # Mensaje de validación de Wasabil si viene en el cuerpo (más útil que el código).
        # Los rechazos de validación traen {"validation": {"campo": "motivo", …}}: se
        # aplanan a texto legible para que el operador sepa QUÉ corregir.
        mensaje = None
        try:
            data = resp.json()
            mensaje = data.get("message") or data.get("error") or data.get("detail")
            val = data.get("validation")
            if not mensaje and isinstance(val, dict) and val:
                mensaje = "; ".join(f"{campo}: {motivo}" for campo, motivo in val.items())
        except Exception:
            pass
        raise WasabilError(
            f"Wasabil respondió {resp.status_code}: {mensaje or resp.text[:300]}",
            detalle=resp.text,
            # 4xx = Wasabil rechazó la petición SIN crear documento; 5xx = ambiguo
            ambiguo=resp.status_code >= 500,
            status_code=resp.status_code,
        )
    try:
        payload = resp.json()
    except json.JSONDecodeError:
        raise WasabilError("Wasabil devolvió una respuesta no-JSON",
                           detalle=resp.text[:500], ambiguo=True)
    return _desenvolver(payload)


def _desenvolver(payload):
    """El API de Wasabil envuelve las respuestas OK en `{success, status, data}`
    (confirmado contra el API real el 2026-07-17). Devuelve el contenido de `data`
    para que el resto del módulo trabaje con el objeto/listado real. Defensivo: si
    la respuesta NO trae ese envoltorio, la devuelve tal cual (un endpoint que
    responda el objeto directo sigue funcionando)."""
    if isinstance(payload, dict) and "data" in payload and (
            "success" in payload or "status" in payload):
        return payload["data"]
    return payload


# ─── Operaciones ────────────────────────────────────────────────────────────────
def _como_dict(data, contexto: str) -> dict:
    """Los endpoints de documento ÚNICO deben devolver un objeto. Un 2xx con
    `data: null` (o una forma inesperada) se convierte en WasabilError AMBIGUO —
    no se puede saber el estado real del documento — en vez de dejar que un
    `.get()` sobre None reviente con 500 en el router (espejo del guard que ya
    tiene buscar_documentos para los listados)."""
    if not isinstance(data, dict):
        raise WasabilError(f"Wasabil devolvió una respuesta inesperada al {contexto}",
                           detalle=str(data)[:200], ambiguo=True)
    return data


def crear_documento(payload_rest: dict) -> dict:
    """POST /documents — crea (y con issue=true EMITE al SII, irreversible).

    El payload ya debe venir traducido a nombres REST (service.payload_a_rest).
    Devuelve el documento con `uuid` y `status_id`.
    """
    return _como_dict(_request("POST", "/documents", json_body=payload_rest),
                      "crear el documento")


def estado_documento(uuid: str) -> dict:
    """GET /documents/{uuid}/status — estado asíncrono del documento (poll)."""
    return _como_dict(_request("GET", f"/documents/{uuid}/status"),
                      "consultar el estado del documento")


def obtener_documento(uuid: str) -> dict:
    """GET /documents/{uuid} — documento completo (folio, PDF/XML urls)."""
    return _como_dict(_request("GET", f"/documents/{uuid}"),
                      "obtener el documento")


def buscar_cliente_por_rut(rut: str) -> Optional[dict]:
    """Busca la ficha del cliente en Wasabil por RUT. None si no existe.

    Con `clientId` en la emisión, Wasabil autocompleta razón social, giro,
    dirección y comuna del receptor desde su propia ficha (fuente de verdad
    del receptor para el SII). Devuelve la ficha NORMALIZADA (giro/dirección/
    comuna/ciudad aplanados desde `giros[]`/`addresses[]`) para que el router no
    tenga que conocer la forma anidada del API.
    """
    data = _request("GET", "/clients", params={"search": rut})
    rut_norm = _normalizar_rut(rut)
    for cli in _items(data):
        if isinstance(cli, dict) and _normalizar_rut(str(cli.get("rut") or "")) == rut_norm:
            return _normalizar_cliente(cli)
    return None


def _items(data) -> list:
    """Extrae la lista de una respuesta de listado (ya desenvuelta del envoltorio).
    Formato real del API: `{items: [...], total, lastPage}`. Acepta también una
    lista directa o el envoltorio `{data: [...]}` por robustez."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for clave in ("items", "data"):
            valor = data.get(clave)
            if isinstance(valor, list):
                return valor
    return []


def _elemento_default(lista) -> Optional[dict]:
    """De una lista de sub-objetos del API (giros/direcciones) devuelve el marcado
    `default`, o el primero si ninguno lo está."""
    dicts = [x for x in (lista or []) if isinstance(x, dict)]
    if not dicts:
        return None
    for x in dicts:
        if x.get("default"):
            return x
    return dicts[0]


def _normalizar_cliente(cli: dict) -> dict:
    """Aplana la ficha del API (giros[]/addresses[] anidados) a las claves planas
    que usa el resto del módulo, conservando el resto de la ficha cruda. Toma el
    giro y la dirección marcados `default` (o el primero de cada lista)."""
    giro = _elemento_default(cli.get("giros"))
    addr = _elemento_default(cli.get("addresses"))
    return {
        **cli,
        "giro": (giro.get("name") if giro else None) or cli.get("giro"),
        "address": (addr.get("address") if addr else None) or cli.get("address"),
        "comuna": (addr.get("comuna") if addr else None) or cli.get("comuna"),
        "city": (addr.get("city") if addr else None) or cli.get("city"),
    }


# Tope de páginas al verificar duplicados: una búsqueda por referencia interna
# específica jamás debería acercarse a esto; si se alcanza, se reporta incompleto.
MAX_PAGINAS_BUSQUEDA = 10


def buscar_documentos(search: str) -> tuple:
    """Busca documentos por referencia — para verificar duplicados antes de re-crear
    (p.ej. search por la referencia interna del despacho).

    Devuelve `(documentos, completo)`. `completo=False` significa que la lista
    PUEDE estar truncada (paginación sin agotar): el llamador NO debe concluir
    "el documento no existe" y re-emitir — abortar es lo seguro. El API pagina con
    `{items, total, lastPage}` (confirmado el 2026-07-17); una lista plana sin
    metadatos se trata como resultado completo.

    ⚠️ PENDIENTE (2026-07-17): `GET /documents` responde 405 en el API real (solo
    acepta POST). Este fallback SOLO se usa en el reintento cuando una emisión falló
    SIN devolver uuid; hoy el 405 hace que el reintento ABORTE de forma segura (nunca
    re-emite a ciegas), pero no reencuentra el documento. El flujo normal usa el uuid
    de la creación, no esto. Confirmar el endpoint de listado real con Wasabil antes
    de depender del reintento por-referencia.
    """
    docs: list = []
    pagina = 1
    while pagina <= MAX_PAGINAS_BUSQUEDA:
        data = _request("GET", "/documents", params={"search": search, "page": pagina})
        if isinstance(data, list):
            # Respuesta plana, sin paginación: es todo lo que hay
            docs.extend(data)
            return docs, True
        if not isinstance(data, dict):
            # 2xx con cuerpo inesperado (null, string…): no se puede concluir nada
            raise WasabilError("Wasabil devolvió una respuesta inesperada al buscar documentos",
                               detalle=str(data)[:200], ambiguo=True)
        docs.extend(_items(data))
        # Paginación real del API: `lastPage`. Metadatos ilegibles (tipos raros)
        # NUNCA deben reventar el reintento: se reporta incompleto y el llamador
        # aborta (conservador).
        try:
            last_page = data.get("lastPage")
            if last_page is None:
                last_page = data.get("last_page")  # tolerancia snake_case
            if last_page is not None:
                if pagina >= int(last_page):
                    return docs, True
            else:
                # Envoltorio sin señal de paginación reconocible: página única
                return docs, True
        except (TypeError, ValueError, AttributeError):
            return docs, False
        pagina += 1
    return docs, False


def _normalizar_rut(rut: str) -> str:
    """Compara RUTs ignorando puntos, guión y mayúsculas: 78.279.030-7 == 782790307."""
    return rut.replace(".", "").replace("-", "").strip().upper()
