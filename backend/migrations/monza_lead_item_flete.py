"""Migración: el FLETE con que se calculó cada ítem del lead se GUARDA (MonzaParts).

QUÉ AGREGA
    monza_lead_items.moneda_tarifa       VARCHAR(10) — moneda del flete aéreo de la corrida (EUR | USD)
    monza_lead_items.tarifa_aerea        FLOAT       — tarifa por kg de esa corrida, en moneda_tarifa
    monza_cotizacion_items.moneda_tarifa VARCHAR(10) — moneda de la tarifa CONGELADA en la línea
                                                       emitida (tarifa_aerea ya existía; sin la
                                                       moneda, una emisión con corridas mixtas
                                                       dejaba el número ambiguo — ronda escéptica
                                                       2026-08-22, hallazgo MED1)

POR QUÉ
    Los arreglos del equipo (2026-08-21) permiten calcular la calculadora por SUBCONJUNTOS
    ("los alemanes en EUR, los americanos en USD"). El flete a nivel LEAD
    (monza_leads.moneda_tarifa/tarifa_aerea) solo puede retratar la ÚLTIMA corrida: sin la
    estampa por ítem, reabrir la calculadora sembraba el flete equivocado para las filas de
    la corrida anterior, y la cotización emitida congelaba en TODOS los ítems una tarifa que
    no produjo sus precios — el mismo defecto de reproducibilidad que monza_cotizador_parametros
    cerró para costo/moneda/peso/markup. Esta pareja es la pieza del flete que faltaba.

    La estampa se escribe SOLO en /cotizador/aplicar y SOLO en las filas que esa corrida
    recalculó (comparación server-side); el contrato completo vive en el docstring de
    MonzaLeadItem (monza_models.py).

IDEMPOTENTE
    Se puede correr las veces que sea. No toca ni un dato existente: los ítems ya creados
    quedan con NULL (= "ninguna corrida recalculó este ítem"), y todo lector cae al nivel
    superior (lead → configuración), que es el comportamiento de siempre. Sin backfill —
    inventar el flete de una corrida vieja a partir del par del lead es justo la mentira
    que esta migración viene a impedir.

Uso (desde backend/, con el venv activo):
    python -m migrations.monza_lead_item_flete
"""
from sqlalchemy import text

from database import engine

# tabla -> {columna: definición DDL}
TABLAS = {
    "monza_lead_items": {
        # Mismo tipo que monza_leads.moneda_tarifa / .tarifa_aerea (la pareja del lead).
        "moneda_tarifa": "VARCHAR(10) NULL",
        "tarifa_aerea": "FLOAT NULL",
    },
    "monza_cotizacion_items": {
        # La MONEDA de la tarifa congelada en la línea (tarifa_aerea ya existía): sin
        # ella, una emisión con corridas mixtas guardaba "4.5" sin decir 4.5 DE QUÉ.
        "moneda_tarifa": "VARCHAR(10) NULL",
    },
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

    Cada tabla va en su propia transacción a propósito: MySQL hace COMMIT IMPLÍCITO en cada
    DDL, así que agrupar tablas en un solo `engine.begin()` PARECE atómico y no lo es
    (mismo criterio que migrations/monza_cotizador_parametros.py).
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
            f"[migracion] FALLÓ en: {', '.join(fallidas)}. Corrige la causa y vuelve a "
            "correr este mismo script (es idempotente). NO reinicies el backend hasta que "
            "salga limpia: el modelo ya declara las columnas, así que sin ellas TODA "
            "consulta de leads de MonzaParts responde 1054 (Unknown column) y la pantalla "
            "de Leads se cae — el mismo radio de falla vale para el pytest local.")
    print("[migracion] completada")


if __name__ == "__main__":
    run()
