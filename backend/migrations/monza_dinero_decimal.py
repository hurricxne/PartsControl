"""Migración: montos de MonzaParts de FLOAT a DECIMAL — MTK-2026-0005.

QUÉ CAMBIA
    11 columnas en 6 tablas pasan de FLOAT a DECIMAL:
        monza_cotizaciones       total_neto, iva_monto, total_bruto    DECIMAL(14,2)
        monza_cotizacion_items   precio_unitario_clp, subtotal_clp      DECIMAL(14,2)
                                 costo                                  DECIMAL(16,4)
        monza_cotizacion_cierre  total_bruto                            DECIMAL(14,2)
        monza_lead_items         precio_clp                             DECIMAL(14,2)
                                 costo                                  DECIMAL(16,4)
        monza_leads              total_estimado                         DECIMAL(14,2)
        monza_clientes           ltv                                    DECIMAL(14,2)
    `costo` va en la moneda de ORIGEN (EUR/USD/CLP) y lleva decimales reales del
    cálculo: por eso 4 decimales.

POR QUÉ
    El FLOAT de 4 bytes GUARDA exacto cualquier entero hasta 16.777.216, pero MariaDB lo
    ENTREGA al cliente convertido a texto con 6 cifras significativas. Toda venta sobre
    $1.000.000 se leía corrida a la decena (COT-2026-000195: guardado 1.546.983, leído
    1.546.980), y la factura —que se recalcula desde las líneas con aritmética exacta—
    se rechazaba contra su propia cabecera. Medido en PROD el 2026-09-25: 79 de las 179
    ventas sobre el millón estaban infacturables. El parche del tope (commit 7ddd451)
    destrabó la facturación; esta migración corrige la causa.

    La corrección del 23-09 ("la columna no admite el valor") estaba mal diagnosticada:
    el valor SÍ se guardaba; lo que no se podía era LEERLO. Por eso el ALTER mismo
    RECUPERA el valor exacto — MariaDB convierte desde el binario, no desde el texto.

QUÉ HACE CON LOS DATOS
    1. El ALTER recupera solo los valores exactos. Medido en PROD con CAST antes de
       migrar: 527 de 528 cabeceras quedan cuadradas con sus líneas.
    2. R1 — cabeceras sobre 16.777.216: ahí el FLOAT de 4 bytes ya no guarda enteros
       exactos (paso de 2, 4, 8...), así que el ALTER no tiene qué recuperar. Se
       recalculan desde sus líneas con la MISMA fórmula de la creación
       (monza_router_cotizaciones: neto = Σ subtotal, iva = round(neto × pct / 100)).
       CINTURÓN: solo si la diferencia cabe en ese paso de granularidad. Una cabecera
       descuadrada por cualquier otra razón se REPORTA y no se toca.
    3. R2 — fotos de cierre VIGENTES: se guardaron con el total que la app LEÍA
       (truncado). Tras el ALTER quedarían distintas del total exacto de su cabecera, y
       la reversión de un cierre resta la FOTO del LTV (_registrar_reversion): si la
       venta se despachara con el total exacto y luego se revirtiera, quedaría residuo.
       Se alinean al exacto SOLO cuando la foto es exactamente la versión truncada del
       total; si es distinta por otra razón, se reporta y no se toca.
    4. LTV — el LTV de una venta despachada se sumó con el total truncado, el mismo que
       guarda su foto. Al alinear la foto, el LTV del cliente se ajusta por el MISMO
       delta, así LTV y foto siguen cuadrando entre sí.

IDEMPOTENTE
    Se puede correr las veces que sea. Las columnas que ya son DECIMAL no se tocan, y
    una vez aplicados, R1/R2 quedan vacíos.

DRY-RUN SIN ESCRITURAS
    El plan se calcula leyendo con CAST(... AS DECIMAL), que usa el binario guardado:
    funciona igual ANTES del ALTER (con FLOAT) que después. `--dry-run` muestra el plan
    completo sobre los datos reales sin escribir nada, ni DDL ni datos.
    Al aplicar, el plan se RECALCULA después del ALTER y se compara con el de antes:
    si difieren, la fase de datos se aborta (el DDL ya quedó, y es inocuo por sí solo).

ORDEN RESPECTO DEL DEPLOY
    Indiferente. Los modelos nuevos (Numeric asdecimal=False) leen bien una columna
    FLOAT, y los viejos (Float) leen bien una DECIMAL: en ambos casos Python recibe
    float. No hay ventana en que la app se caiga.

EFECTO SI NO SE CORRE
    Los modelos nuevos funcionan igual sobre FLOAT, pero la causa sigue: toda venta
    sobre el millón sigue leyéndose corrida (PDF, tableros, LTV) y la facturación
    depende del parche del tope.

Uso (desde backend/, con el venv activo):
    python -m migrations.monza_dinero_decimal --dry-run      # plan, sin escribir
    python -m migrations.monza_dinero_decimal                # aplica
"""
import argparse
import math
import sys
from decimal import Decimal

from sqlalchemy import text

from database import engine

CLP = "DECIMAL(14,2) NULL"
COSTO = "DECIMAL(16,4) NULL"

# tabla -> {columna: definición DDL}. Solo MonzaParts: GA/MachParts ya usa double.
TABLAS = {
    "monza_cotizaciones": {"total_neto": CLP, "iva_monto": CLP, "total_bruto": CLP},
    "monza_cotizacion_items": {"precio_unitario_clp": CLP, "subtotal_clp": CLP, "costo": COSTO},
    "monza_cotizacion_cierre": {"total_bruto": CLP},
    "monza_lead_items": {"precio_clp": CLP, "costo": COSTO},
    "monza_leads": {"total_estimado": CLP},
    "monza_clientes": {"ltv": CLP},
}

# Lectura del BINARIO del FLOAT, sin pasar por su texto de 6 cifras. Sobre una columna
# que ya es DECIMAL es un no-op, así que el plan sale igual antes y después del ALTER.
EXACTO = "DECIMAL(16,4)"


def paso_float32(v) -> Decimal:
    """Granularidad del FLOAT de 4 bytes en `v`: 0 hasta 2^24 (enteros exactos); sobre
    eso el paso se duplica en cada potencia de 2 (2 hasta 2^25, 4 hasta 2^26...)."""
    v = abs(float(v or 0))
    if v < 2 ** 24:
        return Decimal(0)
    return Decimal(2 ** (math.floor(math.log2(v)) - 23))


def vista_app(v) -> Decimal:
    """Lo que el protocolo le entregaba a la app al leer un FLOAT: 6 cifras significativas."""
    return Decimal(repr(float("%.6g" % float(v))))


def _tipos(conn, tabla: str) -> dict:
    rows = conn.execute(text(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = :t"), {"t": tabla})
    return {r[0]: r[1].lower() for r in rows}


def calcular_plan(conn) -> dict:
    """Plan de datos (R1, R2, LTV) leyendo valores exactos. No escribe nada."""
    plan = {"r1": [], "r1_no": [], "r2": [], "r2_no": [], "ltv": {}}

    subtot = {}
    for cid, s in conn.execute(text(
            f"SELECT cotizacion_id, CAST(subtotal_clp AS {EXACTO}) "
            f"FROM monza_cotizacion_items WHERE subtotal_clp IS NOT NULL")):
        subtot[cid] = subtot.get(cid, Decimal(0)) + s

    nuevo_bruto = {}
    for q in conn.execute(text(
            f"SELECT id, numero, estado, CAST(total_neto AS {EXACTO}) n, "
            f"CAST(iva_monto AS {EXACTO}) i, CAST(total_bruto AS {EXACTO}) b, iva_pct "
            f"FROM monza_cotizaciones")).all():
        if q.id not in subtot:
            continue
        neto = subtot[q.id]
        # MISMA fórmula que la creación (monza_router_cotizaciones), round() de Python
        iva = Decimal(round(float(neto) * float(q.iva_pct or 19) / 100))
        bruto = neto + iva
        antes = tuple(v if v is not None else Decimal(0) for v in (q.n, q.i, q.b))
        deltas = (antes[0] - neto, antes[1] - iva, antes[2] - bruto)
        if not any(deltas):
            continue
        tol = max(paso_float32(antes[2]), paso_float32(bruto))
        fila = {"id": q.id, "numero": q.numero, "estado": q.estado,
                "antes": antes, "despues": (neto, iva, bruto)}
        if tol and all(abs(d) <= tol for d in deltas):
            plan["r1"].append(fila)
            nuevo_bruto[q.id] = bruto
        else:
            plan["r1_no"].append(fila)

    for k in conn.execute(text(
            f"SELECT k.id kid, k.cotizacion_id cid, q.numero, q.estado, q.cliente_id, "
            f"CAST(k.total_bruto AS {EXACTO}) foto, CAST(q.total_bruto AS {EXACTO}) exacto "
            f"FROM monza_cotizacion_cierre k JOIN monza_cotizaciones q ON q.id = k.cotizacion_id "
            f"WHERE k.revertido_at IS NULL AND k.total_bruto IS NOT NULL")).all():
        destino = nuevo_bruto.get(k.cid, k.exacto)
        if k.foto == destino:
            continue
        fila = {"kid": k.kid, "numero": k.numero, "estado": k.estado,
                "cliente_id": k.cliente_id, "foto": k.foto, "destino": destino}
        # la foto se tomó de lo que la app LEÍA de la cabecera de ese momento
        if k.foto == vista_app(k.exacto):
            plan["r2"].append(fila)
            if k.estado == "despachado" and k.cliente_id:
                plan["ltv"][k.cliente_id] = plan["ltv"].get(k.cliente_id, Decimal(0)) + (destino - k.foto)
        else:
            plan["r2_no"].append(fila)
    return plan


def _clave(plan: dict):
    """Firma comparable del plan, para verificar que el ALTER no lo movió."""
    return (
        sorted((f["id"], f["despues"]) for f in plan["r1"]),
        sorted((f["kid"], f["destino"]) for f in plan["r2"]),
        sorted(plan["ltv"].items()),
    )


def mostrar_plan(plan: dict) -> None:
    def m(v):
        return f"{float(v):,.0f}".replace(",", ".")

    print(f"R1 · cabeceras sobre 16.777.216 a recalcular desde sus líneas: {len(plan['r1'])}")
    for f in plan["r1"]:
        a, d = f["antes"], f["despues"]
        print(f"     {f['numero']} ({f['estado']}): neto {m(a[0])}→{m(d[0])}  "
              f"iva {m(a[1])}→{m(d[1])}  bruto {m(a[2])}→{m(d[2])}")
    print(f"     descuadradas por OTRA razón (NO se tocan): {len(plan['r1_no'])}")
    for f in plan["r1_no"]:
        a, d = f["antes"], f["despues"]
        print(f"     ! {f['numero']} ({f['estado']}): bruto guardado {m(a[2])} vs líneas {m(d[2])}")

    print(f"R2 · fotos de cierre vigentes con el total truncado → exacto: {len(plan['r2'])}")
    for f in plan["r2"]:
        print(f"     {f['numero']} ({f['estado']}): foto {m(f['foto'])} → {m(f['destino'])}")
    print(f"     fotos distintas por OTRA razón (NO se tocan): {len(plan['r2_no'])}")
    for f in plan["r2_no"]:
        print(f"     ! {f['numero']} ({f['estado']}): foto {m(f['foto'])} vs cabecera {m(f['destino'])}")

    print(f"LTV · clientes a ajustar por el mismo delta de su foto: {len(plan['ltv'])}")
    for cid, d in sorted(plan["ltv"].items()):
        print(f"     cliente {cid}: {'+' if d >= 0 else ''}{m(d)}")


def aplicar_ddl() -> None:
    """Una tabla por transacción: MySQL hace COMMIT implícito en cada DDL, así que
    agruparlas parecería atómico sin serlo (molde: migrations/despacho_qty_firmada.py)."""
    for tabla, cols in TABLAS.items():
        with engine.begin() as conn:
            tipos = _tipos(conn, tabla)
            if not tipos:
                print(f"  · {tabla}: NO EXISTE — se omite")
                continue
            faltan = {c: d for c, d in cols.items() if tipos.get(c) == "float"}
            for c in cols:
                if c not in tipos:
                    print(f"  ! {tabla}.{c}: la columna no existe — se omite")
            if not faltan:
                print(f"  · {tabla}: ya en DECIMAL")
                continue
            ddl = ", ".join(f"MODIFY COLUMN {c} {d}" for c, d in faltan.items())
            conn.execute(text(f"ALTER TABLE {tabla} {ddl}"))
            print(f"  ✓ {tabla}: {', '.join(faltan)} → DECIMAL")


def aplicar_datos(plan: dict) -> None:
    """R1 + R2 + LTV en UNA transacción: o entra todo o nada. Cada UPDATE lleva el valor
    de antes en el WHERE (guard optimista): si algo lo movió entremedio, se aborta."""
    with engine.begin() as conn:
        for f in plan["r1"]:
            n, i, b = f["despues"]
            r = conn.execute(text(
                "UPDATE monza_cotizaciones SET total_neto = :n, iva_monto = :i, total_bruto = :b "
                "WHERE id = :id AND total_bruto = :antes"),
                {"n": n, "i": i, "b": b, "id": f["id"], "antes": f["antes"][2]})
            if r.rowcount != 1:
                raise RuntimeError(f"R1: {f['numero']} cambió durante la migración — se aborta todo")
        for f in plan["r2"]:
            r = conn.execute(text(
                "UPDATE monza_cotizacion_cierre SET total_bruto = :d WHERE id = :k AND total_bruto = :foto"),
                {"d": f["destino"], "k": f["kid"], "foto": f["foto"]})
            if r.rowcount != 1:
                raise RuntimeError(f"R2: el cierre de {f['numero']} cambió durante la migración — se aborta todo")
        for cid, delta in plan["ltv"].items():
            conn.execute(text(
                "UPDATE monza_clientes SET ltv = COALESCE(ltv, 0) + :d WHERE id = :c"),
                {"d": delta, "c": cid})


def verificar() -> bool:
    """Postcondiciones: las 11 columnas en DECIMAL y el plan vacío (salvo lo reportado)."""
    ok = True
    with engine.connect() as conn:
        for tabla, cols in TABLAS.items():
            tipos = _tipos(conn, tabla)
            for c in cols:
                if tipos.get(c) != "decimal":
                    print(f"  ✗ {tabla}.{c} sigue en {tipos.get(c)}")
                    ok = False
        plan = calcular_plan(conn)
    if plan["r1"] or plan["r2"] or plan["ltv"]:
        print("  ✗ quedaron correcciones pendientes tras aplicar:")
        mostrar_plan(plan)
        ok = False
    return ok


def main() -> int:
    try:  # la salida lleva acentos y ✓/✗: que una terminal no-UTF-8 no la haga reventar
        sys.stdout.reconfigure(errors="replace")
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="muestra el plan sin escribir nada")
    args = ap.parse_args()

    with engine.connect() as conn:
        plan_antes = calcular_plan(conn)
    print("=== PLAN (valores exactos leídos con CAST, sin escribir) ===")
    mostrar_plan(plan_antes)
    if args.dry_run:
        print("\n--dry-run: no se escribió nada.")
        return 0

    print("\n=== DDL ===")
    aplicar_ddl()

    with engine.connect() as conn:
        plan = calcular_plan(conn)
    if _clave(plan) != _clave(plan_antes):
        print("\n✗ El plan cambió tras el ALTER — la fase de datos se ABORTA (el DDL quedó).")
        mostrar_plan(plan)
        return 1

    print("\n=== DATOS (R1 + R2 + LTV, una transacción) ===")
    aplicar_datos(plan)
    print(f"  ✓ {len(plan['r1'])} cabeceras, {len(plan['r2'])} fotos de cierre, "
          f"{len(plan['ltv'])} LTV")

    print("\n=== VERIFICACIÓN ===")
    if not verificar():
        return 1
    print("  ✓ 11 columnas en DECIMAL y sin correcciones pendientes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
