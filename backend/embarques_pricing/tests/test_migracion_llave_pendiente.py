"""El init_db del pricing NO puede decir «Listo.» con el candado de plata AUSENTE.

EL AGUJERO QUE ESTA SUITE CIERRA
--------------------------------
`embarques_pricing/init_db.py` crea `uq_emb_pricing_gasto_tipo (pricing_id, tipo)`, que es
la llave natural de la línea de gastos del embarque. Si encuentra duplicados legados NO
puede emitir el ALTER (MySQL responde 1062 y, al ir todo en una sola transacción, se caería
el resto de la migración), así que lo SALTA a propósito. Correcto. El problema era la
SEÑAL: el aviso quedaba enterrado a mitad de ~10 líneas y la ÚLTIMA línea del log seguía
siendo `[embarques_pricing] Listo.` con rc=0. Quien mira el final del log —que es lo que
hace todo el mundo en un deploy de 20 comandos— daba el paso por bueno con el candado
ausente.

Y ese candado toca plata: `cont_compra.emb_pricing_gasto_id` referencia la línea con FK
**ON DELETE SET NULL**, así que sin el UNIQUE un delete + re-insert de la línea desengancha
la CxP en silencio, el overlay vuelve a decir «no registrado» y la MISMA factura del
forwarder se carga DOS veces (Σ CxP al doble).

QUÉ SE PRUEBA, Y POR QUÉ NO ALCANZABA LO QUE YA HABÍA
-----------------------------------------------------
`test_llave_gasto_estable.py:385` solo afirma que el UNIQUE EXISTE, y su comentario dice
«si falta: correr embarques_pricing.init_db». Es decir: el camino de los duplicados legados
—el único en el que el script se salta el candado— no lo ejercía nadie. Acá se ejerce de
verdad, con el molde de `tesoreria/tests/test_lecturas_de_plata.py:324-373`:
  1) se AFIRMA la precondición (la BD local trae el UNIQUE aplicado): sin eso el escenario
     no es el peligroso y la suite se pondría verde sin haber probado nada,
  2) se suelta el índice y se siembra el duplicado MARCADO, con una CxP viva colgada de la
     fila de id MAYOR (el caso que hace daño si el operador «deja la de menor id»),
  3) se corre el script REAL —además como PROCESO, que es lo que teclea el operador— y se
     mide la CONDUCTA: el valor de retorno, la ÚLTIMA línea de la salida y el rc,
  4) se resuelve el duplicado y se exige que la 2ª corrida SÍ cree el UNIQUE (y que la 3ª
     sea idempotente). El propio arreglo deja la BD como estaba.
Nada de introspección de código: quitando el recuadro final del producto, las sondas de
abajo se ponen ROJAS por conducta.

También se verifica en vivo la red de seguridad que el checklist de deploy promete:
con el UNIQUE ausente, `deploy/audit_schema.py` canta `UNIQUE FALTANTE` y sale rc≠0.

Datos MARCADOS + limpieza en `finally` + verificación por DELTAS con conexión NUEVA.
No emite ni toca ningún documento tributario: este módulo no habla con el SII.

Corre con:  cd backend && ./venv/bin/python -m pytest embarques_pricing/tests/test_migracion_llave_pendiente.py -q
(también:   ./venv/bin/python embarques_pricing/tests/test_migracion_llave_pendiente.py)
"""
import io
import os
import subprocess
import sys
from contextlib import redirect_stdout

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from sqlalchemy import text  # noqa: E402

from database import SessionLocal, engine  # noqa: E402
from models.models import Embarque  # noqa: E402
import embarques_pricing.init_db as ep_init  # noqa: E402
from embarques_pricing.models import (  # noqa: E402
    EmbarquePricing, EmbarquePricingGasto,
)
from compras_contab.models import ContCompra  # noqa: E402

MARK = "__TEST_EP_PEND__"
TABLA = "emb_pricing_gasto"
UQ = "uq_emb_pricing_gasto_tipo"
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
    dice Listo. y rc=0»."""
    p = subprocess.run(
        [sys.executable, "-m", "embarques_pricing.init_db", *args],
        cwd=BACKEND, capture_output=True, text=True, timeout=180,
    )
    salida = (p.stdout or "") + (p.stderr or "")
    lineas = [l for l in salida.splitlines() if l.strip()]
    return p.returncode, (lineas[-1] if lineas else ""), salida


def _auditor() -> tuple:
    """`deploy/audit_schema.py` en modo normal: (rc, salida). Solo se mira si NOMBRA este
    UNIQUE; no se exige «sin problemas» global porque la BD es compartida con otras suites
    corriendo en paralelo y cualquier ruido ajeno volvería la sonda inestable."""
    p = subprocess.run([sys.executable, AUDITOR], cwd=BACKEND,
                       capture_output=True, text=True, timeout=180)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


# ─── Seed / limpieza ──────────────────────────────────────────────────────────
def _purge(db) -> None:
    """Borra TODO lo marcado en orden seguro de FK (idempotente)."""
    for c in db.query(ContCompra).filter(ContCompra.referencia.like(f"{MARK}%")).all():
        db.delete(c)
    db.flush()
    for emb in db.query(Embarque).filter(Embarque.numero.like(f"{MARK}%")).all():
        for pr in db.query(EmbarquePricing).filter(
                EmbarquePricing.embarque_id == emb.id).all():
            db.query(EmbarquePricingGasto).filter(
                EmbarquePricingGasto.pricing_id == pr.id).delete(synchronize_session=False)
            db.flush()
            db.delete(pr)
            db.flush()
        db.delete(emb)
        db.flush()
    db.commit()


def seed() -> dict:
    """Un embarque marcado con su pricing y UNA línea de gastos 'agencia'.

    El duplicado NO se siembra acá: exige soltar el índice primero, y el índice se suelta
    dentro de run() para que la ventana sin candado en la BD compartida sea lo más corta
    posible."""
    db = SessionLocal()
    try:
        _purge(db)
        emb = Embarque(numero=f"{MARK}-EMB", estado="en_bodega", forwarder="LATAM Cargo")
        db.add(emb)
        db.flush()
        pr = EmbarquePricing(embarque_id=emb.id, tc_tipo="manual", tc_valor=962,
                             moneda="USD", estado="borrador")
        db.add(pr)
        db.flush()
        g = EmbarquePricingGasto(pricing_id=pr.id, tipo="agencia", glosa="Agencia de aduana",
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
            "SELECT COUNT(*) FROM embarques WHERE numero LIKE :m",
            "SELECT COUNT(*) FROM cont_compra WHERE referencia LIKE :m",
            "SELECT COUNT(*) FROM emb_pricing p JOIN embarques e ON e.id = p.embarque_id "
            "WHERE e.numero LIKE :m",
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
          "(si falla: python -m embarques_pricing.init_db)", tenia)
    if not tenia:
        return

    pendientes_limpio = ep_init.main()
    check("0 con la BD sana main() devuelve LISTA VACÍA (contrato del valor de retorno)",
          pendientes_limpio == [], pendientes_limpio)
    rc, ultima, _ = _correr_script()
    check("0 y la línea de cierre del proceso DISTINGUE el caso sano "
          "(«Listo (sin migraciones pendientes)»)",
          rc == 0 and "sin migraciones pendientes" in ultima, (rc, ultima))

    # ── 1 · soltar el índice y sembrar el duplicado LEGADO ──────────────────────
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {TABLA} DROP INDEX {UQ}"))
    check("1 el índice quedó soltado (la ventana de la sonda está abierta)",
          not _indice_en_bd())

    db = SessionLocal()
    try:
        dup = EmbarquePricingGasto(pricing_id=ctx["pricing_id"], tipo="agencia",
                                   glosa=f"{MARK} DUPLICADA", monto_neto=160_000,
                                   iva=30_400, capitaliza=True, orden=3)
        db.add(dup)
        db.flush()
        ctx["dup_id"] = dup.id
        # La CxP viva se cuelga de la fila de id MAYOR: es el caso en que el consejo viejo
        # («deje la de menor id») CAUSA el daño que el UNIQUE previene.
        cxp = ContCompra(empresa="mineria", origen="EMBARQUE", tipo_gasto="cogs",
                         acreedor=f"{MARK} FORWARDER", referencia=f"{MARK}-CxP",
                         descripcion="Agencia de aduana del embarque", moneda="CLP", tc=1,
                         monto_neto=160_000, iva=30_400, monto_total=190_400,
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
          and int(_sql("SELECT emb_pricing_gasto_id FROM cont_compra WHERE id = :i",
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
    check("2 LA ÚLTIMA LÍNEA del log NO dice «Listo.» "
          "(era el hallazgo: quien mira el final del log daba el paso por bueno)",
          bool(lineas) and lineas[-1].strip() != "[embarques_pricing] Listo.", lineas[-1:])
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
          "python -m embarques_pricing.init_db" in texto, texto[:400])

    # El resto del módulo TIENE que haber quedado migrado en esa misma corrida.
    check("2 el resto de la migración corrió igual en esa misma invocación",
          "peso_origen" in salida and "tipo_cambio_eur" in salida, lineas)
    check("2 y las columnas del resto siguen en la BD (nada quedó a medias)",
          _columna_en_bd("emb_pricing_item", "peso_origen")
          and _columna_en_bd("configuracion_cotizador", "tipo_cambio_eur"))

    # ── 3 · el PROCESO: rc y última línea, que es lo que ve el operador ─────────
    rc, ultima, _ = _correr_script()
    check("3 el proceso sale rc=0 por defecto (NO aborta la cadena del deploy: las 8 "
          "migraciones que siguen son las que producen el 1054 en cascada)",
          rc == 0, rc)
    check("3 pero su ÚLTIMA línea es el recuadro, no «Listo.»",
          ultima.startswith("=" * 20), ultima)

    rc2, _, salida2 = _correr_script("--exigir-completo")
    check("3 con --exigir-completo el proceso sale rc=2 (señal para un deploy scripteado)",
          rc2 == 2, (rc2, salida2[-200:]))
    check("3 y el recuadro anuncia esa opción", "--exigir-completo" in salida2)

    # ── 4 · la red de seguridad del checklist, verificada en vivo ──────────────
    rc_aud, salida_aud = _auditor()
    check("4 con el UNIQUE ausente, deploy/audit_schema.py lo CANTA por nombre",
          f"UNIQUE FALTANTE {UQ}" in salida_aud,
          [l for l in salida_aud.splitlines() if "FALTANTE" in l][:5])
    check("4 y el auditor sale rc≠0 (ese sí es el corte duro del deploy)",
          rc_aud != 0, rc_aud)


def _cerrar(ctx: dict):
    """Resuelve el duplicado como lo haría el operador BIEN INFORMADO (borra la fila SIN
    CxP, conserva la referenciada) y exige que la 2ª corrida cree el UNIQUE. Deja la BD
    exactamente como estaba: esto es además la prueba de idempotencia."""
    if not ctx.get("dup_id"):
        return
    # Se borra la de id MENOR, que es la que NO tiene la CxP: seguir el consejo del script.
    _borrar = ctx["gasto_id"]
    with engine.begin() as conn:
        conn.execute(text(f"DELETE FROM {TABLA} WHERE id = :i"), {"i": _borrar})

    pendientes2 = ep_init.main()
    check("5 resuelto el duplicado, la 2ª corrida SÍ crea el UNIQUE y no deja pendientes",
          _indice_en_bd() and pendientes2 == [], pendientes2)
    check("5 y la CxP conservó su llave hacia el gasto que quedó vivo "
          "(el consejo del script no desengancha la factura)",
          int(_sql("SELECT emb_pricing_gasto_id FROM cont_compra WHERE id = :i",
                   i=ctx.get("cxp_id")) or 0) == ctx["dup_id"],
          _sql("SELECT emb_pricing_gasto_id FROM cont_compra WHERE id = :i",
               i=ctx.get("cxp_id")))
    check("5 3ª corrida idempotente (ya existe, sin pendientes)", ep_init.main() == [])
    rc, ultima, _ = _correr_script()
    check("5 y el proceso vuelve a cerrar con la línea del caso sano",
          rc == 0 and "sin migraciones pendientes" in ultima, (rc, ultima))
    rc_aud, salida_aud = _auditor()
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


def test_init_db_pricing_avisa_migracion_pendiente_ga():
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
