"""Migración: los parámetros de la calculadora de precios de MonzaParts se GUARDAN.

QUÉ AGREGA
    monza_lead_items.costo        FLOAT       — costo unitario en la MONEDA de origen
    monza_lead_items.moneda       VARCHAR(10) — EUR | USD | CLP
    monza_lead_items.peso_kg      FLOAT       — peso volumétrico usado para el flete
    monza_lead_items.markup_pct   FLOAT       — margen aplicado (entero, ej. 28)
    monza_lead_items.tc_aplicado  FLOAT       — TC con que se convirtió el costo (auditoría)
    monza_leads.moneda_tarifa     VARCHAR(10) — moneda del flete aéreo, ELEGIDA por cotización
    monza_leads.tarifa_aerea      FLOAT       — tarifa por kg vigente al cotizar

POR QUÉ
    La calculadora pedía costo, moneda, peso y markup, calculaba el precio… y `aplicar_precios`
    guardaba SOLO `precio_clp`. Los parámetros no llegaban nunca a la base: el schema
    `AplicarPrecioItem` ni siquiera los tenía. Al reabrir la calculadora no había nada que
    restaurar, así que el frontend caía en un camino de emergencia que metía el PRECIO FINAL en
    el campo del costo y forzaba moneda CLP con markup 0 — que es exactamente lo que el dueño
    reportó: «guardo en USD y al volver a abrir me muestra CLP, y en el costo lo mismo que en el
    precio».

    Ojo con la asimetría que hacía difícil de ver el problema: la COTIZACIÓN (monza_cotizacion_items)
    SÍ tiene estas columnas y las congela bien al cerrarse. El agujero estaba una etapa antes, en
    el LEAD, que es donde vive la calculadora.

LA MONEDA DEL FLETE
    Hasta ahora salía de `monza_config.moneda_tarifa` — una configuración GLOBAL, EUR por defecto,
    sin forma de elegirla al cotizar. Ahora se elige por cotización (decisión del dueño: una sola
    para todos los ítems, no una por ítem) y se guarda en el lead. `MonzaCotizacion.moneda_tarifa`
    y `.tarifa_aerea` ya existían y se llenan al crear la cotización: esta migración completa el
    eslabón que faltaba, el del lead.

IDEMPOTENTE
    Se puede correr las veces que sea. No toca ni un dato existente: los leads y sus ítems ya
    creados quedan con estas columnas en NULL y la calculadora sigue funcionando (cae al valor de
    la configuración global, igual que hoy). Sin backfill — inventar el costo de una cotización
    vieja a partir de su precio es justo el bug que se está cerrando.

Uso (desde backend/, con el venv activo):
    python -m migrations.monza_cotizador_parametros
"""
from sqlalchemy import text

from database import engine

# tabla -> {columna: definición DDL}
TABLAS = {
    "monza_lead_items": {
        "costo": "FLOAT NULL",
        "moneda": "VARCHAR(10) NULL",
        "peso_kg": "FLOAT NULL",
        "markup_pct": "FLOAT NULL",
        "tc_aplicado": "FLOAT NULL",
    },
    "monza_leads": {
        "moneda_tarifa": "VARCHAR(10) NULL",
        "tarifa_aerea": "FLOAT NULL",
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

    Cada tabla va en su propia transacción a propósito: MySQL hace COMMIT IMPLÍCITO en cada DDL,
    así que meter las dos en un solo `engine.begin()` PARECE atómico y no lo es. Separarlas hace
    visible lo que de verdad pasa (mismo criterio que migrations/despacho_fecha_guia.py).
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
            "aplicadas. Corrige la causa y vuelve a correr este mismo script (es idempotente). "
            "NO reinicies el backend hasta que salga limpia: los modelos ya declaran las "
            "columnas, así que sin ellas la pantalla de Leads de MonzaParts responde 1054.")
    print("[migracion] completada")


if __name__ == "__main__":
    run()
