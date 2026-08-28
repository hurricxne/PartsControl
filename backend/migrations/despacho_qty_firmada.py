"""Migración: FIRMA PARCIAL de la guía de despacho — Grupo AM / MachParts.

QUÉ AGREGA
    despacho_items.qty_firmada   FLOAT NULL  — cantidad FIRMADA como recibida por línea
    despachos.faltante_motivo    TEXT NULL   — motivo del faltante (uno por firma, cabecera)

POR QUÉ
    A veces la guía se emitió por ítems que NO llegaron al cliente (perdidos en la
    entrega). Al marcar «guía firmada», el operador ahora puede ajustar CANTIDADES por
    ítem: lo firmado va a facturación; el faltante queda REGISTRADO con motivo y NO es
    facturable por esa guía. La reposición NO vuelve por esta venta (el dueño la pide
    de nuevo en una cotización nueva): cero liberación de cupo, cero cambios de estado
    de línea, cero bodega.

    · qty_firmada NULL = firma completa (comportamiento histórico, byte-igual).
    · qty_despachada NO se toca: es la verdad física y el espejo de la guía SII.
    · La facturación se acota a coalesce(qty_firmada, qty_despachada)
      (routers/contabilidad.py).

SOLO GRUPO AM
    MonzaParts no participa en este cambio (sus tablas monza_* no se tocan).

IDEMPOTENTE
    Se puede correr las veces que sea: si la columna ya está, la deja como está.
    No toca ni un solo dato existente — todo lo firmado hasta hoy queda con
    qty_firmada NULL (= firma completa), que es exactamente lo que significaba.

EFECTO SI NO SE CORRE (severidad CRÍTICA)
    Los modelos ya declaran las columnas → cualquier consulta que lea la ENTIDAD
    completa revienta con 1054 «Unknown column»: cerrar/anular despachos, firmar,
    FACTURAR (crear_factura lee DespachoItem completo) y la emisión de guías al SII.
    Y LA TRAMPA: las bandejas de Despachos y de Bodega SOBREVIVEN (consultan columnas
    sueltas), así que el sistema PARECE sano hasta que alguien cierra o factura.

Uso (desde backend/, con el venv activo):
    python -m migrations.despacho_qty_firmada
"""
from sqlalchemy import text

from database import engine

# tabla -> {columna: definición DDL}. Solo Grupo AM / MachParts.
TABLAS = {
    "despacho_items": {"qty_firmada": "FLOAT NULL"},
    "despachos": {"faltante_motivo": "TEXT NULL"},
}


def _tabla_existe(conn, tabla: str) -> bool:
    return bool(conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = :t"
        ),
        {"t": tabla},
    ).scalar())


def _columnas_existentes(conn, tabla: str) -> set:
    rows = conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = :t"
        ),
        {"t": tabla},
    )
    return {r[0] for r in rows}


def _parchar(tabla: str, columnas: dict) -> None:
    """Agrega a UNA tabla las columnas que le falten. Conexión propia por tabla.

    Cada tabla va en su propia transacción a propósito. MySQL hace COMMIT IMPLÍCITO en
    cada DDL, así que meter las dos en un solo `engine.begin()` PARECE atómico y no lo
    es: si la segunda fallara, la primera ya quedó aplicada igual. Separarlas hace
    visible lo que de verdad pasa y deja que el fallo de una no oculte el éxito de la
    otra. (Molde: migrations/despacho_fecha_guia.py.)
    """
    with engine.begin() as conn:
        if not _tabla_existe(conn, tabla):
            print(f"[migracion] tabla {tabla} no existe en esta base — se omite")
            return
        existentes = _columnas_existentes(conn, tabla)
        for col, ddl in columnas.items():
            if col in existentes:
                print(f"[migracion] {tabla}.{col} ya existe — ok")
                continue
            conn.execute(text(f"ALTER TABLE {tabla} ADD COLUMN {col} {ddl}"))
            print(f"[migracion] {tabla}.{col} agregada")


def run() -> None:
    fallidas = []
    for tabla, columnas in TABLAS.items():
        try:
            _parchar(tabla, columnas)
        except Exception as exc:  # noqa: BLE001
            fallidas.append(tabla)
            print(f"[migracion] ERROR en {tabla}: {type(exc).__name__}: {exc}")
    if fallidas:
        raise SystemExit(
            f"[migracion] FALLÓ en: {', '.join(fallidas)}. Las demás tablas SÍ quedaron "
            "aplicadas. Corrige la causa y vuelve a correr este mismo script "
            "(es idempotente: no repite lo ya hecho). NO reinicies el backend hasta que "
            "esta migración salga limpia: sin la columna, cerrar/anular despachos, "
            "firmar, facturar y la emisión de guías al SII responden 1054 — aunque las "
            "bandejas de Despachos y Bodega se vean sanas.")
    print("[migracion] completada")


if __name__ == "__main__":
    run()
