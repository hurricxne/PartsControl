"""Helpers y serializadores del módulo Recepción Nacional MonzaParts (autocontenido).

El backend es la autoridad: valida qué llegó y cuánto, y NUNCA confía en el
cliente para el tope físico. La transición de estado del ítem (comprado →
en_bodega) y la reversa de anulación viven en el router (necesitan bloquear
MonzaCotizacionItem), aquí solo quedan las utilidades puras.

Nota (adaptación Monza): a diferencia de GA no hay helper `empresa_de` — las
tablas monza_* son exclusivas de MonzaParts ('automotriz'), no llevan columna
empresa; el scope lo pone el candado require_empresa("automotriz") del router."""
from datetime import datetime, date
from typing import Optional


def _f(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _parse_date(s) -> Optional[date]:
    if not s:
        return None
    if isinstance(s, date):
        return s
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(s)[:10], fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def parse_date_estricta(s, *, campo: str) -> Optional[date]:
    if s is None or s == "":
        return None
    d = _parse_date(s)
    if d is None:
        raise ValueError(f"Fecha inválida en '{campo}': {s!r} (use AAAA-MM-DD)")
    return d


def _serialize_item(ri) -> dict:
    return {
        "id": ri.id,
        "item_cotizacion_id": ri.item_cotizacion_id,
        "numero_parte": ri.numero_parte,
        "descripcion": ri.descripcion,
        "qty_recibida": _f(ri.qty_recibida),
        "estado_recepcion": ri.estado_recepcion,
        "observacion": ri.observacion,
    }


def serialize_recepcion(rec) -> dict:
    return {
        "id": rec.id,
        "oc_proveedor_id": rec.oc_proveedor_id,
        "numero_guia_proveedor": rec.numero_guia_proveedor,
        "fecha": rec.fecha.isoformat() if rec.fecha else None,
        "estado": rec.estado,
        "documento": rec.documento,
        "observacion": rec.observacion,
        "usuario_id": rec.usuario_id,
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
        "fecha_cierre": rec.fecha_cierre.isoformat() if rec.fecha_cierre else None,
        "items": [_serialize_item(ri) for ri in sorted(rec.items, key=lambda x: x.id)],
    }
