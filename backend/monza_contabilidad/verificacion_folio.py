"""Verificación del FOLIO de una factura/boleta registrada A MANO contra Wasabil (MonzaParts).

POR QUÉ EXISTE (pedido del dueño, 2026-09-29)
    El funnel B2B emite la factura 33 desde la plataforma, pero el B2C vive de boletas y de
    documentos emitidos por fuera (portal SII, POS, Wasabil directo). Todos entran por el
    REGISTRO MANUAL de Contabilidad → Facturas → «Emitir factura», que hasta ahora aceptaba
    cualquier número tecleado: un folio mal digitado quedaba contabilizado, cobrado y
    conciliado contra un documento que no existe ante el SII.

    Desde este cambio todo registro manual declara el ORIGEN del documento:
      · 'wasabil' → se CONSULTA Wasabil y el folio solo entra si el documento existe, es del
        tipo elegido (33 factura / 39 boleta), está EMITIDO, no es de prueba (sandbox), su
        total coincide con lo que se va a registrar y —en factura— el receptor es el RUT del
        cliente de la venta.
      · 'externo' → documento emitido FUERA de Wasabil (no hay dónde verificarlo). Exige una
        declaración escrita que queda auditada. Igual se consulta Wasabil: si ese folio+tipo
        SÍ está allá, el operador eligió mal el origen y se le bloquea (los folios de un tipo
        son UNA sola numeración por emisor ante el SII, salga de donde salga el documento).
      · Wasabil caído / sin token → se BLOQUEA en los dos orígenes (decisión del dueño:
        ningún folio entra sin comprobar; se reintenta más tarde).

    Este módulo es lógica PURA sobre lo que devuelve Wasabil: no toca la BD ni lanza
    HTTPException. El router decide los códigos HTTP y dónde se guarda la auditoría.
"""
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from monza_rut import rut_identidad
from monza_wasabil_compras.client import (
    WasabilComprasError, buscar_emitidos_por_folio,
)

# Tipo interno → código SII. Solo lo que Monza registra por esta vía: factura afecta (33)
# y boleta afecta (39). Las exentas (34/41) no se usan en Monza; si un día hicieran
# falta, se agregan acá y en el selector del modal.
TIPO_SII = {"factura": "33", "boleta": "39"}

ORIGEN_WASABIL = "wasabil"
ORIGEN_EXTERNO = "externo"
ORIGENES = (ORIGEN_WASABIL, ORIGEN_EXTERNO)

# status_id de Wasabil: 6 Pendiente · 2 Procesando · 3 Emitido · 4 Fallido. Mismo valor
# que monza_wasabil_dte/models.py (STATUS_EMITIDO); se repite para no acoplar la
# contabilidad al módulo de emisión por una constante.
WASABIL_STATUS_EMITIDO = 3

# Holgura de monto: $1 por el redondeo half-up del IVA (el SII redondea a peso y el ERP
# también, pero con la línea como unidad). Más que eso ya no es redondeo: es otro documento.
TOL_MONTO_CLP = 1.0

# Largo mínimo REAL (tras strip) de la declaración del documento externo. Diez caracteres
# alcanzan para «Portal SII» o «POS local»; menos es un «ok» que no explica nada.
DECLARACION_MIN = 10

# Tope de dígitos del folio: el mismo del SII para un folio (FOLIO_REF_MAX en
# monza_wasabil_dte/service.py). Evita además un int() sobre miles de dígitos.
FOLIO_MAX_DIGITOS = 18


class FolioNoVerificable(Exception):
    """Wasabil no pudo responder (caído, timeout, sin token, respuesta ilegible). El
    router lo traduce a 503: no es un "no existe", es un "no sé" — y con un "no sé" no se
    registra nada."""


@dataclass(frozen=True)
class ResultadoFolio:
    ok: bool
    # verificado | externo_ok | no_existe | no_emitido | sandbox | monto_distinto |
    # rut_distinto | existe_en_wasabil
    estado: str
    mensaje: str
    # Lo que Wasabil sabe del documento (None si no se encontró): se muestra en el modal
    # y se guarda en la factura (uuid) para trazabilidad.
    documento: Optional[dict] = None
    advertencias: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "estado": self.estado, "mensaje": self.mensaje,
                "documento": self.documento, "advertencias": list(self.advertencias)}


def normalizar_folio(folio) -> int:
    """El folio como ENTERO positivo, o ValueError con un mensaje para el operador.

    Wasabil filtra el folio como int y el SII solo emite folios numéricos: '35', ' 35 ' y
    '035' son el mismo documento; 'F-35' o '35a' no son un folio del SII.
    (`isascii` además de `isdigit`: '٣'.isdigit() es True y no es un folio.)"""
    txt = str(folio or "").strip()
    if not txt:
        raise ValueError("Ingresa el folio SII del documento")
    if len(txt) > FOLIO_MAX_DIGITOS or not (txt.isascii() and txt.isdigit()) or int(txt) <= 0:
        raise ValueError(
            f"El folio ('{txt}') debe ser el número correlativo que asignó el SII: solo "
            f"dígitos, sin letras, guiones ni espacios (máximo {FOLIO_MAX_DIGITOS}).")
    return int(txt)


def _resumen_documento(doc: dict) -> dict:
    """Lo mínimo que la pantalla necesita mostrar. Montos en CLP (`sent_*` = lo enviado al
    SII en pesos; `current_*` puede venir en otra moneda)."""
    return {
        "uuid": doc.get("uuid"),
        "tipo_sii": str(doc.get("sii_document_type_id") or ""),
        "folio": str(doc.get("folio") or ""),
        "fecha": doc.get("document_date"),
        "status_id": doc.get("status_id"),
        "receptor_rut": doc.get("receiver_rut"),
        "receptor_nombre": doc.get("receiver_name"),
        "neto": doc.get("sent_nsubtotal"),
        "iva": doc.get("sent_niva"),
        "total": doc.get("sent_ntotal"),
    }


def _fmt_clp(v) -> str:
    try:
        return "$" + f"{float(v):,.0f}".replace(",", ".")
    except (TypeError, ValueError):
        return "—"


def verificar_folio(*, tipo_doc: str, folio, origen: str, bruto_esperado: float,
                    rut_esperado: Optional[str],
                    buscar: Optional[Callable[[str, int], List[dict]]] = None,
                    ) -> ResultadoFolio:
    """Decide si el folio tecleado puede registrarse. Ver el docstring del módulo.

    `buscar` es inyectable para las pruebas (Wasabil simulado); sin él se usa la consulta
    de solo lectura `buscar_emitidos_por_folio`, resuelta AL LLAMAR (no como default del
    parámetro) para que una suite pueda reemplazar el nombre del módulo con monkeypatch.

    Lanza ValueError (dato mal tecleado → 400) o FolioNoVerificable (Wasabil no
    respondió → 503). Todo lo demás vuelve como ResultadoFolio con `ok` y un mensaje."""
    if tipo_doc not in TIPO_SII:
        raise ValueError(f"Tipo de documento '{tipo_doc}' no se puede registrar por esta vía "
                         "(solo factura o boleta)")
    if origen not in ORIGENES:
        raise ValueError("Indica el origen del documento: emitido en Wasabil o fuera de Wasabil")
    n_folio = normalizar_folio(folio)
    codigo = TIPO_SII[tipo_doc]
    nombre = "la factura" if tipo_doc == "factura" else "la boleta"

    buscar = buscar or buscar_emitidos_por_folio
    try:
        docs = buscar(codigo, n_folio)
    except WasabilComprasError as e:
        raise FolioNoVerificable(
            f"No se pudo consultar Wasabil para verificar el folio {n_folio}: {e}. "
            "Reintenta en unos minutos; el documento no se registra sin verificar.") from e

    # Documentos de PRUEBA fuera: con el token de producción no deberían aparecer, pero si
    # aparecieran no pueden respaldar una venta real.
    reales = [d for d in docs if not d.get("sandbox")]

    if origen == ORIGEN_EXTERNO:
        if reales:
            return ResultadoFolio(
                False, "existe_en_wasabil",
                f"El folio {n_folio} de tipo {codigo} SÍ existe en Wasabil: elige el origen "
                "«Emitido en Wasabil» para que se verifique contra ese documento.",
                _resumen_documento(reales[0]))
        return ResultadoFolio(
            True, "externo_ok",
            f"El folio {n_folio} no está en Wasabil: se registrará como documento emitido "
            "FUERA de Wasabil, con tu declaración como respaldo.")

    if not reales:
        if docs:
            return ResultadoFolio(
                False, "sandbox",
                f"El folio {n_folio} solo existe como documento de PRUEBA (sandbox) en "
                "Wasabil: no respalda una venta real.", _resumen_documento(docs[0]))
        return ResultadoFolio(
            False, "no_existe",
            f"No existe en Wasabil {nombre} (tipo {codigo}) con folio {n_folio} emitida por "
            "MonzaParts. Revisa el número y el tipo; si el documento se emitió fuera de "
            "Wasabil, elige ese origen.")

    emitidos = [d for d in reales if d.get("status_id") == WASABIL_STATUS_EMITIDO]
    if not emitidos:
        doc = _resumen_documento(reales[0])
        return ResultadoFolio(
            False, "no_emitido",
            f"El folio {n_folio} existe en Wasabil pero NO está emitido ante el SII "
            f"(estado {doc['status_id']}): solo se registran documentos emitidos.", doc)

    doc = _resumen_documento(emitidos[0])
    total_wasabil = doc["total"]
    try:
        diferencia = abs(float(total_wasabil) - float(bruto_esperado))
    except (TypeError, ValueError):
        diferencia = None
    if diferencia is None or diferencia > TOL_MONTO_CLP:
        return ResultadoFolio(
            False, "monto_distinto",
            f"El total de {nombre} N° {n_folio} en Wasabil ({_fmt_clp(total_wasabil)}) no "
            f"coincide con lo que se va a registrar ({_fmt_clp(bruto_esperado)}). Revisa "
            "que el folio, la venta y el despacho sean los correctos.", doc)

    # RUT del receptor: solo en FACTURA. La boleta B2C normalmente va al RUT genérico
    # 66.666.666-6 o sin receptor, así que compararlo bloquearía el caso normal.
    if tipo_doc == "factura":
        esperado = rut_identidad(rut_esperado)
        recibido = rut_identidad(doc["receptor_rut"])
        if not esperado or esperado != recibido:
            return ResultadoFolio(
                False, "rut_distinto",
                f"La factura N° {n_folio} en Wasabil está emitida a "
                f"{doc['receptor_nombre'] or '—'} (RUT {doc['receptor_rut'] or '—'}), no al "
                f"cliente de esta venta (RUT {rut_esperado or '—'}).", doc)

    advertencias = []
    if len(emitidos) > 1:
        # No debería pasar (un folio por tipo y emisor), pero si Wasabil lo devuelve se
        # dice en vez de elegir uno en silencio.
        advertencias.append(f"Wasabil devolvió {len(emitidos)} documentos con el mismo "
                            "folio y tipo; se usó el primero. Revísalo en Wasabil.")
    return ResultadoFolio(
        True, "verificado",
        f"Verificado en Wasabil: {nombre} N° {n_folio} del {doc['fecha'] or '—'} por "
        f"{_fmt_clp(total_wasabil)}.", doc, advertencias)
