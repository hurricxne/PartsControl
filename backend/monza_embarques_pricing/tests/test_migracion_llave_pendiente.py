"""El init_db del pricing Monza NO puede decir «init OK.» con el candado de plata AUSENTE.

Espejo de `embarques_pricing/tests/test_migracion_llave_pendiente.py`. Mismo agujero, misma
plata: `monza_embarques_pricing/init_db.py` salta —con razón— el ALTER de
`uq_monza_emb_pricing_gasto_tipo (pricing_id, tipo)` cuando encuentra duplicados legados
(MySQL responde 1062 y, al ir todo en una transacción, se caería el resto de la migración),
pero el aviso quedaba enterrado y la ÚLTIMA línea del log seguía siendo
`[monza_embarques_pricing] init OK.` con rc=0. Sin ese UNIQUE, un delete + re-insert de la
línea de gastos desengancha `monza_cont_compra.emb_pricing_gasto_id` (FK **ON DELETE SET
NULL**), el overlay vuelve a decir «no registrado» y la misma factura del forwarder entra
DOS veces.

POR QUÉ EN MONZAPARTS EL RECUADRO PESA MÁS QUE EN GRUPO AM
----------------------------------------------------------
La red de seguridad del checklist (`deploy/audit_schema.py`, §1.e) clasifica las tablas
`monza_emb_*` como «solo con el gate» (`PREFIJOS_SOLO_CON_GATE`, audit_schema.py:77/216).
Con `MONZA_CONTAB_ENABLED=false` —que es como puede estar producción— el `UNIQUE FALTANTE`
de este módulo se degrada a **(aviso, gate apagado)** y el auditor sale **rc=0**. Eso se
mide acá abajo con el gate forzado en las dos posiciones. Conclusión operativa: con el gate
apagado el recuadro final del propio script es la ÚNICA señal fuerte de todo el deploy, y
`--exigir-completo` la única señal para una máquina.

Molde: `tesoreria/tests/test_lecturas_de_plata.py:324-373`. Se AFIRMA la precondición (la BD
trae el UNIQUE), se suelta el índice, se siembra el duplicado MARCADO con una CxP viva
colgada de la fila de id MAYOR (el caso en que «deje la de menor id» causa el daño), se
corre el script REAL —también como PROCESO, que es lo que teclea el operador— y se mide la
CONDUCTA: valor de retorno, ÚLTIMA línea de la salida y rc. Al final se resuelve el
duplicado y se exige que la 2ª corrida cree el UNIQUE (3ª idempotente): la BD queda igual.
Cero introspección de código.

Datos MARCADOS + limpieza en `finally` + verificación por DELTAS con conexión NUEVA.
No emite ni toca ningún documento tributario (MonzaParts todavía no hizo su 1ª emisión).

Corre con:  cd backend && ./venv/bin/python -m pytest monza_embarques_pricing/tests/test_migracion_llave_pendiente.py -q
(también:   ./venv/bin/python monza_embarques_pricing/tests/test_migracion_llave_pendiente.py)
"""
import io
import os
import subprocess
import sys
from contextlib import redirect_stdout

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from sqlalchemy import text  # noqa: E402

from database import SessionLocal, engine  # noqa: E402
from monza_models import MonzaEmbarque  # noqa: E402
import monza_embarques_pricing.init_db as ep_init  # noqa: E402
from monza_embarques_pricing.models import (  # noqa: E402
    MonzaEmbPricing, MonzaEmbPricingGasto,
)
from monza_compras_contab.models import MonzaContCompra  # noqa: E402

MARK = "__TEST_MZEP_PEND__"
TABLA = "monza_emb_pricing_gasto"
UQ = "uq_monza_emb_pricing_gasto_tipo"
BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
AUDITOR = os.path.abspath(os.path.join(BACKEND, "..", "deploy", "audit_schema.py"))

_fails: list = []


def check(name, cond, extra=""):
    print(("OK   " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


# ─── Lecturas con conexión NUEVA (nada de identity map) ───────────────────────
def _sql(sql: str, **par):
    with engine.connect() as conn:
        return conn.execute(text(sql), par).scalar()


def _indice_en_bd(nombre: str = UQ, tabla: str = TABLA) -> bool:
    return bool(_sql(
        "SELECT COUNT(*) FROM information_schema.statistics "
        "WHERE table_schema = DATABASE() AND table_name = :t AND index_name = :i",
        t=tabla, i=nombre))


def _columna_en_bd(tabla: str, columna: str) -> bool:
    return bool(_sql(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c",
        t=tabla, c=columna))


def _correr_script(*args) -> tuple:
    """Corre el init_db REAL como PROCESO (lo que teclea el operador en el deploy).

    Devuelve (rc, ultima_linea_no_vacia, salida_completa). Medir el proceso —y no solo
    `main()`— es lo que hace falta: el hallazgo era literalmente «la ÚLTIMA línea del log
    dice init OK. y rc=0»."""
    p = subprocess.run(
        [sys.executable, "-m", "monza_embarques_pricing.init_db", *args],
        cwd=BACKEND, capture_output=True, text=True, timeout=180,
    )
    salida = (p.stdout or "") + (p.stderr or "")
    lineas = [l for l in salida.splitlines() if l.strip()]
    return p.returncode, (lineas[-1] if lineas else ""), salida


def _auditor(gate: str) -> tuple:
    """`deploy/audit_schema.py` con el gate de Monza FORZADO por variable de entorno (pydantic
    da precedencia al entorno sobre el .env). Devuelve (rc, salida).

    Solo se mira si NOMBRA este UNIQUE y con qué severidad; no se exige «sin problemas»
    global porque la BD es compartida con otras suites en paralelo y el ruido ajeno volvería
    la sonda inestable. No lee ni imprime ningún secreto: solo fuerza esta variable."""
    entorno = dict(os.environ, MONZA_CONTAB_ENABLED=gate)
    p = subprocess.run([sys.executable, AUDITOR], cwd=BACKEND, env=entorno,
                       capture_output=True, text=True, timeout=180)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


# ─── Seed / limpieza ──────────────────────────────────────────────────────────
def _purge(db) -> None:
    """Borra TODO lo marcado en orden seguro de FK (idempotente)."""
    for c in db.query(MonzaContCompra).filter(
            MonzaContCompra.referencia.like(f"{MARK}%")).all():
        db.delete(c)
    db.flush()
    for emb in db.query(MonzaEmbarque).filter(MonzaEmbarque.numero.like(f"{MARK}%")).all():
        for pr in db.query(MonzaEmbPricing).filter(
                MonzaEmbPricing.embarque_id == emb.id).all():
            db.query(MonzaEmbPricingGasto).filter(
                MonzaEmbPricingGasto.pricing_id == pr.id).delete(synchronize_session=False)
            db.flush()
            db.delete(pr)
            db.flush()
        db.delete(emb)
        db.flush()
    db.commit()


def seed() -> dict:
    """Un embarque Monza marcado con su pricing y UNA línea de gastos 'agencia'.

    El duplicado NO se siembra acá: exige soltar el índice primero, y el índice se suelta
    dentro de run() para que la ventana sin candado en la BD compartida sea lo más corta
    posible."""
    db = SessionLocal()
    try:
        _purge(db)
        emb = MonzaEmbarque(numero=f"{MARK}-EMB", estado="en_bodega", forwarder="FASTMARK")
        db.add(emb)
        db.flush()
        pr = MonzaEmbPricing(embarque_id=emb.id, tc_tipo="manual", tc_valor=962,
                             moneda="USD", estado="borrador")
        db.add(pr)
        db.flush()
        g = MonzaEmbPricingGasto(pricing_id=pr.id, tipo="agencia", glosa="Agencia de aduana",
                                 monto_neto=160_000, iva=30_400, capitaliza=True, orden=3)
        db.add(g)
        db.commit()
        print(f"[seed] embarque={emb.id} numero={MARK}-EMB pricing={pr.id} gasto={g.id}")
        return {"embarque_id": emb.id, "numero": f"{MARK}-EMB",
                "pricing_id": pr.id, "gasto_id": g.id}
    finally:
        db.close()


def _residuos() -> int:
    """Verificación por DELTAS con CONEXIÓN NUEVA: 0 filas marcadas sobreviven."""
    with engine.connect() as conn:
        n = 0
        for sql in (
            "SELECT COUNT(*) FROM monza_embarques WHERE numero LIKE :m",
            "SELECT COUNT(*) FROM monza_cont_compra WHERE referencia LIKE :m",
            "SELECT COUNT(*) FROM monza_emb_pricing p "
            "JOIN monza_embarques e ON e.id = p.embarque_id WHERE e.numero LIKE :m",
        ):
            n += int(conn.execute(text(sql), {"m": f"{MARK}%"}).scalar() or 0)
    return n


# ─── Checks ───────────────────────────────────────────────────────────────────
def run(ctx: dict):
    # ── 0 · PRECONDICIÓN: el escenario peligroso solo existe si el UNIQUE está aplicado ──
    # Un control que no puede concluir no puede pasar en silencio: si la BD local no trae el
    # índice, esta suite NO probó el ramo de duplicados y tiene que decirlo en ROJO.
    tenia = _indice_en_bd()
    check(f"0 PRECONDICIÓN: la BD local trae {UQ} aplicado "
          "(si falla: python -m monza_embarques_pricing.init_db)", tenia)
    if not tenia:
        return

    pendientes_limpio = ep_init.main()
    check("0 con la BD sana main() devuelve LISTA VACÍA (contrato del valor de retorno)",
          pendientes_limpio == [], pendientes_limpio)
    rc, ultima, _ = _correr_script()
    check("0 y la línea de cierre del proceso DISTINGUE el caso sano "
          "(«init OK (sin migraciones pendientes)»)",
          rc == 0 and "sin migraciones pendientes" in ultima, (rc, ultima))

    # ── 1 · soltar el índice y sembrar el duplicado LEGADO ──────────────────────
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {TABLA} DROP INDEX {UQ}"))
    check("1 el índice quedó soltado (la ventana de la sonda está abierta)",
          not _indice_en_bd())

    db = SessionLocal()
    try:
        dup = MonzaEmbPricingGasto(pricing_id=ctx["pricing_id"], tipo="agencia",
                                   glosa=f"{MARK} DUPLICADA", monto_neto=160_000,
                                   iva=30_400, capitaliza=True, orden=3)
        db.add(dup)
        db.flush()
        ctx["dup_id"] = dup.id
        # La CxP viva se cuelga de la fila de id MAYOR: es el caso en que el consejo viejo
        # («deje la de menor id») CAUSA el daño que el UNIQUE previene.
        cxp = MonzaContCompra(origen="EMBARQUE", tipo_gasto="cogs",
                              acreedor=f"{MARK} FORWARDER", referencia=f"{MARK}-CxP",
                              descripcion="Agencia de aduana del embarque", moneda="CLP",
                              tc=1, monto_neto=160_000, iva=30_400, monto_total=190_400,
                              monto_total_clp=190_400, embarque_id=ctx["embarque_id"],
                              emb_pricing_gasto_id=dup.id, anulado=False)
        db.add(cxp)
        db.commit()
        ctx["cxp_id"] = cxp.id
    finally:
        db.close()
    check("1 quedan DOS líneas 'agencia' del mismo pricing (duplicado legado plantado)",
          int(_sql(f"SELECT COUNT(*) FROM {TABLA} WHERE pricing_id = :p AND tipo = 'agencia'",
                   p=ctx["pricing_id"]) or 0) == 2)
    check("1 y la CxP viva cuelga de la fila de id MAYOR",
          ctx["dup_id"] > ctx["gasto_id"]
          and int(_sql("SELECT emb_pricing_gasto_id FROM monza_cont_compra WHERE id = :i",
                       i=ctx["cxp_id"]) or 0) == ctx["dup_id"],
          (ctx["gasto_id"], ctx["dup_id"]))

    # ── 2 · el script REAL con el dato legado vivo ──────────────────────────────
    buf = io.StringIO()
    pendientes, exc = None, None
    try:
        with redirect_stdout(buf):
            pendientes = ep_init.main()
    except BaseException as e:                                # noqa: BLE001
        exc = e
    salida = buf.getvalue()
    lineas = [l for l in salida.splitlines() if l.strip()]

    check("2 con duplicados legados el init_db NO revienta (el deploy no queda a medias)",
          exc is None, f"{type(exc).__name__}: {exc}")
    check("2 informa el paso PENDIENTE en el valor de retorno (no sale en silencio)",
          isinstance(pendientes, list) and any(UQ in p for p in pendientes), pendientes)
    check("2 no creó el UNIQUE con datos que lo violan", not _indice_en_bd())

    # EL HALLAZGO, medido: la última línea del log ya no puede decir que todo salió bien.
    check("2 LA ÚLTIMA LÍNEA del log NO dice «init OK.» "
          "(era el hallazgo: quien mira el final del log daba el paso por bueno)",
          bool(lineas) and lineas[-1].strip() != "[monza_embarques_pricing] init OK.",
          lineas[-1:])
    check("2 la última línea es el BORDE del recuadro de atención",
          bool(lineas) and lineas[-1].startswith("=" * 20), lineas[-1:])
    check("2 y la línea de cierre del caso sano NO aparece",
          "sin migraciones pendientes" not in salida)
    check("2 el recuadro grita ATENCIÓN y dice cuántos pendientes hay",
          "ATENCIÓN" in salida and "PENDIENTE" in salida)
    idx_aviso = next((i for i, l in enumerate(lineas) if " ! " in l), None)
    idx_banner = next((i for i, l in enumerate(lineas) if "ATENCIÓN" in l), None)
    check("2 el recuadro va DESPUÉS del aviso en su lugar (resumen final, no reemplazo)",
          idx_aviso is not None and idx_banner is not None and idx_banner > idx_aviso,
          (idx_aviso, idx_banner))

    # El aviso tiene que ser ACCIONABLE: sin el N° de embarque el operador no encuentra la
    # fila (el pricing_id no se muestra en ninguna pantalla).
    texto = " ".join(pendientes or [])
    check("2 el pendiente NOMBRA el N° de embarque (así se encuentra en pantalla)",
          ctx["numero"] in texto, texto[:400])
    check("2 el pendiente NOMBRA las dos ids duplicadas",
          str(ctx["gasto_id"]) in texto and str(ctx["dup_id"]) in texto, texto[:400])
    check("2 el pendiente dice CUÁL NO BORRAR: la que tiene la CxP viva colgada "
          "(«la de menor id» habría desenganchado la factura del forwarder)",
          f"NO BORRE la id {ctx['dup_id']}" in texto, texto[:400])
    check("2 el pendiente dice el RIESGO en plata (la factura entra dos veces)",
          "DOS veces" in texto, texto[:400])
    check("2 el pendiente dice el REMEDIO exacto (volver a correr el init_db)",
          "python -m monza_embarques_pricing.init_db" in texto, texto[:400])
    check("2 y advierte que con el gate apagado el auditor NO corta",
          "MONZA_CONTAB_ENABLED=false" in texto, texto[:400])

    # El resto del módulo TIENE que haber quedado migrado en esa misma corrida.
    check("2 el resto de la migración corrió igual en esa misma invocación",
          "peso_origen" in salida and "desconsolidado_clp" in salida, lineas)
    check("2 y las columnas del resto siguen en la BD (nada quedó a medias)",
          _columna_en_bd("monza_emb_pricing_item", "peso_origen")
          and _columna_en_bd("monza_config", "desconsolidado_clp")
          and _columna_en_bd("monza_config", "costo_agencia_minimo_clp"))

    # ── 3 · el PROCESO: rc y última línea, que es lo que ve el operador ─────────
    rc, ultima, _ = _correr_script()
    check("3 el proceso sale rc=0 por defecto (NO aborta la cadena del deploy: las 7 "
          "migraciones Monza que siguen tumban el núcleo con el gate apagado)",
          rc == 0, rc)
    check("3 pero su ÚLTIMA línea es el recuadro, no «init OK.»",
          ultima.startswith("=" * 20), ultima)

    rc2, _, salida2 = _correr_script("--exigir-completo")
    check("3 con --exigir-completo el proceso sale rc=2 (señal para un deploy scripteado)",
          rc2 == 2, (rc2, salida2[-200:]))
    check("3 y el recuadro anuncia esa opción", "--exigir-completo" in salida2)

    # ── 4 · la red de seguridad del checklist, y su límite en MonzaParts ───────
    rc_on, salida_on = _auditor("true")
    check("4 con el gate ENCENDIDO el auditor canta el UNIQUE por nombre",
          f"UNIQUE FALTANTE {UQ}" in salida_on,
          [l for l in salida_on.splitlines() if "FALTANTE" in l][:5])
    check("4 y sale rc≠0 (ahí el auditor sí es el corte duro del deploy)", rc_on != 0, rc_on)

    rc_off, salida_off = _auditor("false")
    linea_off = next((l for l in salida_off.splitlines() if UQ in l), "")
    check("4 con el gate APAGADO el MISMO hallazgo se degrada a (aviso, gate apagado)",
          "(aviso, gate apagado)" in linea_off, linea_off or salida_off[-300:])
    check("4 …y el auditor sale rc=0: con el gate apagado el ÚNICO grito del deploy es el "
          "recuadro del script (por eso este recuadro existe)",
          rc_off == 0 and "sin problemas" in salida_off, (rc_off, salida_off[-200:]))


def _cerrar(ctx: dict):
    """Resuelve el duplicado como lo haría el operador BIEN INFORMADO (borra la fila SIN
    CxP, conserva la referenciada) y exige que la 2ª corrida cree el UNIQUE. Deja la BD
    exactamente como estaba: esto es además la prueba de idempotencia."""
    if not ctx.get("dup_id"):
        return
    # Se borra la de id MENOR, que es la que NO tiene la CxP: seguir el consejo del script.
    with engine.begin() as conn:
        conn.execute(text(f"DELETE FROM {TABLA} WHERE id = :i"), {"i": ctx["gasto_id"]})

    pendientes2 = ep_init.main()
    check("5 resuelto el duplicado, la 2ª corrida SÍ crea el UNIQUE y no deja pendientes",
          _indice_en_bd() and pendientes2 == [], pendientes2)
    check("5 y la CxP conservó su llave hacia el gasto que quedó vivo "
          "(el consejo del script no desengancha la factura)",
          int(_sql("SELECT emb_pricing_gasto_id FROM monza_cont_compra WHERE id = :i",
                   i=ctx.get("cxp_id")) or 0) == ctx["dup_id"],
          _sql("SELECT emb_pricing_gasto_id FROM monza_cont_compra WHERE id = :i",
               i=ctx.get("cxp_id")))
    check("5 3ª corrida idempotente (ya existe, sin pendientes)", ep_init.main() == [])
    rc, ultima, _ = _correr_script()
    check("5 y el proceso vuelve a cerrar con la línea del caso sano",
          rc == 0 and "sin migraciones pendientes" in ultima, (rc, ultima))
    rc_aud, salida_aud = _auditor("true")
    check("5 con el UNIQUE de vuelta el auditor ya NO lo nombra",
          f"UNIQUE FALTANTE {UQ}" not in salida_aud,
          [l for l in salida_aud.splitlines() if "FALTANTE" in l][:5])


def cleanup(ctx: dict):
    try:
        _cerrar(ctx)
    except Exception as e:                                    # noqa: BLE001
        check("5 el cierre de la sonda no puede fallar (la BD queda sin el UNIQUE)",
              False, repr(e)[:300])
    # Red de última instancia: si algo se cayó antes de _cerrar, el índice DEBE volver.
    if not _indice_en_bd():
        db = SessionLocal()
        try:
            _purge(db)
        finally:
            db.close()
        try:
            ep_init.main()
        except Exception as e:                                # noqa: BLE001
            print(f"⚠️  no se pudo restaurar {UQ}: {e}")
    db = SessionLocal()
    try:
        db.rollback()
        _purge(db)
    except Exception as e:                                    # noqa: BLE001
        db.rollback()
        print(f"⚠️  cleanup falló: {e}")
    finally:
        db.close()
    if not _indice_en_bd():
        check(f"5 la BD queda con {UQ} restaurado (no se deja el candado suelto)", False)


def test_init_db_pricing_avisa_migracion_pendiente_monza():
    """Wrapper para pytest: llama a run() DIRECTAMENTE (el candado
    tests_infra/test_suites_visibles.py exige la llamada literal)."""
    ctx = seed()
    try:
        run(ctx)
    finally:
        cleanup(ctx)
    resto = _residuos()
    print(f"[cleanup] filas MARCADAS que sobreviven: {resto}")
    assert not _fails and resto == 0, f"fallas={_fails} residuos={resto}"


if __name__ == "__main__":
    _ctx = seed()
    try:
        run(_ctx)
    finally:
        cleanup(_ctx)
    _resto = _residuos()
    print(f"[cleanup] filas MARCADAS que sobreviven: {_resto}")
    print()
    if _fails or _resto:
        print(f"❌ {len(_fails)} fallas: {_fails} · residuos={_resto}")
        sys.exit(1)
    print("✅ todo verde")
