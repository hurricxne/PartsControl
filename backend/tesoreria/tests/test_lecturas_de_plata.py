"""Sondas de CONDUCTA de la regla 3 (`docs/regla-lecturas-de-plata.md`): toda lectura
BLOQUEANTE que decide plata va con `populate_existing()`.

Por qué hace falta probarlo y no basta con leerlo. El `with_for_update()` trae la fila
fresca de MySQL a cualquier nivel de aislamiento, pero si el objeto YA está en el identity
map de la sesión, SQLAlchemy DESCARTA lo que trajo el lock y sirve la copia vieja. El lock
serializa y aun así se decide con datos rancios. Cada sonda de acá:
  1) deja la fila en el identity map de la sesión del request (eso lo hace cualquier lectura
     previa del mismo request: un resumen, un guard, la cola de sugerencias). OJO: hay que
     MANTENER VIVA la referencia al objeto (`cache = …`), porque el identity map guarda
     referencias DÉBILES: si el recolector se lo lleva, la fila sale del mapa y el bug se
     vuelve inalcanzable — así se me fue verde la primera versión de estas sondas,
  2) la cambia desde OTRA sesión y COMMITEA (el otro operador),
  3) llama a la función REAL de Tesorería y mira la DECISIÓN (un 400/409 de montos que no
     debería salir, un dato viejo en la respuesta, o un UPDATE que no se emitió).
Nada de `inspect.getsource`: quitando el `populate_existing()` del producto, estas sondas se
ponen ROJAS por conducta.

Las 5 lecturas cubiertas (las 5 que el informe adversarial dejó abiertas):
  · `_mov_scoped(lock=True)`  → mov.monto, el lado BANCO de los tres cruces.
  · `_conciliar_tx` egreso    → monto_total_clp ±TOL y el guard `conciliado`.
  · `_conciliar_tx` adelanto  → estado + monto (la entidad del invariante monto_aplicado).
  · `_conciliar_tx` factura   → la fila del cruce; hoy su único lector corre DESPUÉS del
                                commit, así que la sonda mide lo que la regla promete: que
                                la instancia bloqueada lleve el dato fresco al decidir.
  · `desconciliar` egreso     → sin él NO se emite el UPDATE y el pago queda conciliado
                                para siempre (invisible en /egresos-pendientes).
Las 2 lecturas bloqueantes restantes del módulo (la cuenta bancaria y la OC del adelanto)
son PORTONES de serialización: no leen ni persisten montos, así que la regla no aplica.

+ La migración del UNIQUE(egreso_id) frente a DUPLICADOS LEGADOS: se prueba de verdad
  (se suelta la restricción, se siembra el caso legado marcado y se corre el init_db real):
  no debe reventar, debe NOMBRAR el egreso duplicado y no debe dejar el deploy a medias.

Datos MARCADOS + limpieza + verificación por deltas: no toca ni una fila real.
Corre con:  cd backend && ./venv/bin/python -m pytest tesoreria/tests/test_lecturas_de_plata.py -q
(también:   ./venv/bin/python tesoreria/tests/test_lecturas_de_plata.py)
"""
import os
import sys
from datetime import date
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from sqlalchemy import event, inspect as sa_inspect, text  # noqa: E402

from database import Base, engine, SessionLocal  # noqa: E402
from models.models import (  # noqa: E402
    Cotizacion, OcCliente, ContAdelanto, ContFacturaCliente, ContCobranza,
)
from compras_contab.models import ContEgreso  # noqa: E402
import tesoreria.init_db as tes_init  # noqa: E402
from tesoreria.models import (  # noqa: E402
    CuentaBancaria, MovimientoBancario, Conciliacion, ConciliacionIngreso,
)
from tesoreria.router import _conciliar_tx, desconciliar  # noqa: E402
from tesoreria.schemas import ConciliarIn  # noqa: E402

MARK = "__TEST_TES_PLATA__"
USUARIO = SimpleNamespace(id=None, empresa="mineria")

Base.metadata.create_all(bind=engine, checkfirst=True)

_fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _sql(consulta: str, **params):
    """UPDATE/SELECT en OTRA conexión, con su propio commit: es 'el otro operador'."""
    with engine.begin() as conn:
        res = conn.execute(text(consulta), params)
        return res.scalar() if res.returns_rows else None


def _sesion_request():
    """Sesión como la de un request real: la primera sentencia es la lectura del guard de
    auth (así el read view nace igual que en producción)."""
    db = SessionLocal()
    db.execute(text("SELECT 1"))
    return db


# ─── Siembra ───────────────────────────────────────────────────────────────────
def _cuenta() -> int:
    db = SessionLocal()
    try:
        c = CuentaBancaria(empresa="mineria", banco="Santander", nombre=f"{MARK} Cta",
                           moneda="CLP", activo=True)
        db.add(c)
        db.commit()
        return c.id
    finally:
        db.close()


def _mov(cuenta_id: int, sufijo: str, tipo: str, monto: float) -> int:
    db = SessionLocal()
    try:
        m = MovimientoBancario(empresa="mineria", cuenta_id=cuenta_id, fecha=date(2026, 7, 2),
                               glosa=f"{MARK} {tipo} {sufijo}", tipo=tipo, monto=monto)
        db.add(m)
        db.commit()
        return m.id
    finally:
        db.close()


def _egreso(sufijo: str, monto: float, **kw) -> int:
    db = SessionLocal()
    try:
        e = ContEgreso(empresa="mineria", fecha=date(2026, 7, 1), medio="transferencia",
                       beneficiario=f"{MARK} Prov{sufijo}", monto_total_clp=monto,
                       moneda="CLP", tc=1, monto_origen=monto,
                       glosa=f"{MARK} egreso {sufijo}", **kw)
        db.add(e)
        db.commit()
        return e.id
    finally:
        db.close()


def _factura_con_cobranza(sufijo: str, monto: float) -> tuple:
    db = SessionLocal()
    try:
        f = ContFacturaCliente(
            empresa="mineria", numero_factura=f"{MARK}-{sufijo}-VIEJO", tipo_doc="factura",
            fecha_emision=date(2026, 7, 1), fecha_vencimiento=date(2026, 7, 30),
            monto_neto=monto, iva=0, monto_bruto=monto, monto_pagado=monto, saldo=0,
            estado_pago="pagada")
        db.add(f)
        db.flush()
        c = ContCobranza(factura_id=f.id, fecha=date(2026, 7, 2), monto=monto,
                         medio="transferencia", banco="Santander",
                         numero_operacion=f"{MARK}-OP{sufijo}")
        db.add(c)
        db.commit()
        return f.id, c.id
    finally:
        db.close()


def _adelanto(sufijo: str, monto: float, estado: str) -> int:
    db = SessionLocal()
    try:
        cot = Cotizacion(numero=f"{MARK}-COT{sufijo}", cliente=f"{MARK} Cliente",
                         rut_cliente="78.279.030-7")
        db.add(cot)
        db.flush()
        oc = OcCliente(cotizacion_id=cot.id, numero_oc=f"{MARK}-OC{sufijo}",
                       fecha_oc="2026-07-01")
        db.add(oc)
        db.flush()
        a = ContAdelanto(oc_cliente_id=oc.id, empresa="mineria", estado=estado, monto=monto,
                         monto_aplicado=0, fecha_pago=date(2026, 7, 1), banco="Santander",
                         numero_operacion=f"{MARK}-AD{sufijo}",
                         observaciones=f"{MARK} adelanto {sufijo}")
        db.add(a)
        db.commit()
        return a.id
    finally:
        db.close()


def run():
    cuenta_id = _cuenta()
    # `cache` mantiene VIVA la referencia al objeto precargado en cada bloque: sin ella el
    # recolector lo saca del identity map (referencias débiles) y la sonda deja de medir.
    cache = None
    assert cache is None

    # ══ 1 · `_mov_scoped(lock=True)`: mov.monto es el lado BANCO del cruce ═══════════
    # El operador corrige el monto del movimiento (reimportó la cartola con el valor bueno)
    # mientras el tesorero concilia. Con la fila cacheada, el cruce se evalúa con el monto
    # viejo y rebota un 400 "los montos no coinciden" que no corresponde.
    eg1 = _egreso("M1", 500_000)
    mov1 = _mov(cuenta_id, "M1", "cargo", 111_111)
    db = _sesion_request()
    try:
        cache = db.query(MovimientoBancario).filter(  # ← queda en el identity map
            MovimientoBancario.id == mov1).first()
        _sql("UPDATE conc_movimiento SET monto = 500000 WHERE id = :i", i=mov1)
        salida, exc = None, None
        try:
            salida = _conciliar_tx(mov1, ConciliarIn(egreso_id=eg1), db, USUARIO)
        except BaseException as e:  # noqa: BLE001
            exc = e
        check("mov: concilia con el monto FRESCO del movimiento (no con el cacheado)",
              isinstance(salida, dict) and exc is None,
              f"{type(exc).__name__} {getattr(exc, 'status_code', '')}: {getattr(exc, 'detail', exc)}")
    finally:
        db.close()
    check("mov: el enlace guardó el monto fresco (500.000)",
          _sql("SELECT monto_conciliado_clp FROM conc_conciliacion WHERE movimiento_id = :i",
               i=mov1) == 500000,
          _sql("SELECT monto_conciliado_clp FROM conc_conciliacion WHERE movimiento_id = :i", i=mov1))

    # ══ 2 · egreso del cruce cargo↔egreso: monto_total_clp + guard `conciliado` ══════
    # Compras corrige el monto del egreso (o lo recalcula al revertir un detalle) y el
    # tesorero concilia el cargo real del banco.
    eg2 = _egreso("M2", 1_000)
    mov2 = _mov(cuenta_id, "M2", "cargo", 700_000)
    db = _sesion_request()
    try:
        cache = db.query(ContEgreso).filter(ContEgreso.id == eg2).first()  # ← identity map
        _sql("UPDATE cont_egreso SET monto_total_clp = 700000 WHERE id = :i", i=eg2)
        salida, exc = None, None
        try:
            salida = _conciliar_tx(mov2, ConciliarIn(egreso_id=eg2), db, USUARIO)
        except BaseException as e:  # noqa: BLE001
            exc = e
        check("egreso: concilia con el monto FRESCO del egreso (no con el cacheado)",
              isinstance(salida, dict) and exc is None,
              f"{type(exc).__name__} {getattr(exc, 'status_code', '')}: {getattr(exc, 'detail', exc)}")
    finally:
        db.close()

    # ══ 3 · adelanto del cruce abono↔adelanto: estado + monto ═══════════════════════
    # Tesorería aprueba el adelanto (estado informado→aprobado y monto real) en otra
    # pestaña; con la fila cacheada, conciliar el abono contesta "solo se concilian
    # adelantos APROBADOS" sobre un adelanto que YA está aprobado.
    ad3 = _adelanto("M3", 1_000, "informado")
    mov3 = _mov(cuenta_id, "M3", "abono", 950_000)
    db = _sesion_request()
    try:
        cache = db.query(ContAdelanto).filter(ContAdelanto.id == ad3).first()  # ← identity map
        _sql("UPDATE cont_adelanto SET estado = 'aprobado', monto = 950000 WHERE id = :i", i=ad3)
        salida, exc = None, None
        try:
            salida = _conciliar_tx(mov3, ConciliarIn(adelanto_id=ad3), db, USUARIO)
        except BaseException as e:  # noqa: BLE001
            exc = e
        check("adelanto: concilia con el estado y el monto FRESCOS (no con los cacheados)",
              isinstance(salida, dict) and exc is None,
              f"{type(exc).__name__} {getattr(exc, 'status_code', '')}: {getattr(exc, 'detail', exc)}")
    finally:
        db.close()

    # ══ 4 · factura del cruce abono↔cobranza ════════════════════════════════════════
    # Acá la observación NO puede ser la respuesta: el único lector de la factura (el resumen
    # del destino) corre DESPUÉS del `db.commit()`, y el commit expira toda la sesión
    # (expire_on_commit=True), así que el folio de la respuesta sale fresco con o sin el
    # arreglo. Lo que la regla 3 promete es otra cosa: que la entidad con la que la función
    # DECIDE lleve los valores frescos. Eso se mide donde importa —dentro de la llamada real,
    # al momento del commit— mirando el diccionario de atributos CARGADOS de la instancia
    # (`inspect(obj).dict`, que no emite SQL ni refresca nada). Es conducta observada del
    # objeto real, no lectura del código fuente.
    fid4, cid4 = _factura_con_cobranza("M4", 119_000)
    mov4 = _mov(cuenta_id, "M4", "abono", 119_000)
    db = _sesion_request()
    visto = {}
    try:
        cache = db.query(ContFacturaCliente).filter(  # ← identity map
            ContFacturaCliente.id == fid4).first()
        _sql("UPDATE cont_factura_cliente SET numero_factura = :n WHERE id = :i",
             n=f"{MARK}-M4-NUEVO", i=fid4)

        @event.listens_for(db, "before_commit")
        def _mirar(sesion, _cache=cache):  # noqa: ANN001
            visto["folio"] = sa_inspect(_cache).dict.get("numero_factura")

        salida, exc = None, None
        try:
            salida = _conciliar_tx(mov4, ConciliarIn(cobranza_id=cid4), db, USUARIO)
        except BaseException as e:  # noqa: BLE001
            exc = e
        check("factura: la fila bloqueada lleva el dato FRESCO al decidir (no el cacheado)",
              visto.get("folio") == f"{MARK}-M4-NUEVO",
              visto.get("folio") or f"{type(exc).__name__}: {exc}")
        check("factura: la conciliación se completó",
              isinstance(salida, dict) and salida.get("conciliado") is True,
              salida or f"{type(exc).__name__}: {exc}")
    finally:
        event.remove(db, "before_commit", _mirar)
        db.close()

    # ══ 5 · desconciliar: sin populate_existing NO se emite el UPDATE ════════════════
    # El egreso trae la referencia que ingresó el operador en Compras; al conciliar, la
    # cartola la pisa y el enlace guarda la previa. Si el request de desconciliar ya tenía
    # la foto ANTERIOR al cruce, las asignaciones no cambian nada respecto de esa copia y
    # SQLAlchemy no manda UPDATE: el pago se queda `conciliado` y con la referencia del
    # banco, sin forma de volver a emparejarlo.
    eg5 = _egreso("M5", 44_000, referencia_bancaria="REF-OPERADOR",
                  fecha_mov_bancario=date(2026, 6, 1))
    mov5 = _mov(cuenta_id, "M5", "cargo", 44_000)
    _sql("UPDATE conc_movimiento SET referencia = 'REF-BANCO' WHERE id = :i", i=mov5)
    db = _sesion_request()
    try:
        cache = db.query(ContEgreso).filter(ContEgreso.id == eg5).first()  # ← foto ANTES del cruce
        # El cruce ocurre en OTRA sesión (otro request), como en la vida real.
        otra = _sesion_request()
        try:
            _conciliar_tx(mov5, ConciliarIn(egreso_id=eg5), otra, USUARIO)
        finally:
            otra.close()
        check("desconciliar (previo): el cruce dejó el egreso conciliado con la ref del banco",
              _sql("SELECT conciliado FROM cont_egreso WHERE id = :i", i=eg5) == 1
              and _sql("SELECT referencia_bancaria FROM cont_egreso WHERE id = :i",
                       i=eg5) == "REF-BANCO")
        desconciliar(mov5, db=db, current_user=USUARIO)
    finally:
        db.close()
    check("desconciliar: el egreso queda LIBRE en la BD (se emitió el UPDATE)",
          _sql("SELECT conciliado FROM cont_egreso WHERE id = :i", i=eg5) == 0,
          _sql("SELECT conciliado FROM cont_egreso WHERE id = :i", i=eg5))
    check("desconciliar: se RESTAURÓ la referencia del operador",
          _sql("SELECT referencia_bancaria FROM cont_egreso WHERE id = :i",
               i=eg5) == "REF-OPERADOR",
          _sql("SELECT referencia_bancaria FROM cont_egreso WHERE id = :i", i=eg5))

    # ══ 6 · migración del UNIQUE(egreso_id) con DUPLICADOS LEGADOS ═══════════════════
    _sonda_migracion_duplicados(cuenta_id)


def _sonda_migracion_duplicados(cuenta_id: int):
    """Suelta el UNIQUE, siembra el caso legado (dos enlaces marcados al MISMO egreso) y
    corre el init_db REAL. Al final lo deja como estaba (la restricción la vuelve a crear
    el propio init_db, que es además la prueba de idempotencia)."""
    tenia = _constraint_en_bd("uq_conc_concil_egreso")
    check("pre: la BD local trae el UNIQUE(egreso_id) aplicado "
          "(si falla: python -m tesoreria.init_db)", tenia)
    if not tenia:
        return

    eg = _egreso("DUP", 12_345)
    m1 = _mov(cuenta_id, "DUP1", "cargo", 12_345)
    m2 = _mov(cuenta_id, "DUP2", "cargo", 12_345)
    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE conc_conciliacion DROP INDEX uq_conc_concil_egreso"))
        db = SessionLocal()
        try:
            db.add_all([
                Conciliacion(empresa="mineria", movimiento_id=m1, egreso_id=eg,
                             monto_conciliado_clp=12_345),
                Conciliacion(empresa="mineria", movimiento_id=m2, egreso_id=eg,
                             monto_conciliado_clp=12_345),
            ])
            db.commit()
        finally:
            db.close()

        pendientes, exc = None, None
        try:
            pendientes = tes_init.main()
        except BaseException as e:  # noqa: BLE001
            exc = e
        check("migración: con duplicados legados el init_db NO revienta",
              exc is None, f"{type(exc).__name__}: {exc}")
        check("migración: informa el paso PENDIENTE (no sale en silencio)",
              isinstance(pendientes, list) and any("uq_conc_concil_egreso" in p for p in pendientes),
              pendientes)
        check("migración: el aviso NOMBRA el egreso duplicado (accionable)",
              isinstance(pendientes, list) and any(str(eg) in p for p in pendientes),
              pendientes)
        check("migración: no creó el UNIQUE con datos que lo violan",
              not _constraint_en_bd("uq_conc_concil_egreso"))
        check("migración: el resto del esquema quedó migrado (no a medias)",
              _constraint_en_bd("uq_conc_concil_ing_adelanto", "conc_conciliacion_ingreso")
              and _constraint_en_bd("ck_conc_concil_ing_un_destino", "conc_conciliacion_ingreso"))
    finally:
        # Se quita el duplicado (como haría el operador desconciliando) y se vuelve a correr
        # el init_db: debe crear el UNIQUE y quedar sin pendientes.
        _sql("DELETE FROM conc_conciliacion WHERE egreso_id = :i AND movimiento_id = :m",
             i=eg, m=m2)
        pendientes2 = tes_init.main()
        check("migración: resuelto el duplicado, la 2ª corrida SÍ crea el UNIQUE",
              _constraint_en_bd("uq_conc_concil_egreso") and pendientes2 == [], pendientes2)
        check("migración: 3ª corrida idempotente (ya existe, sin pendientes)",
              tes_init.main() == [])


def _constraint_en_bd(nombre: str, tabla: str = "conc_conciliacion") -> bool:
    return bool(_sql(
        "SELECT COUNT(*) FROM information_schema.table_constraints "
        "WHERE table_schema = DATABASE() AND table_name = :t AND constraint_name = :n",
        t=tabla, n=nombre))


def _cleanup():
    db = SessionLocal()
    try:
        db.rollback()
        for o in db.query(CuentaBancaria).filter(CuentaBancaria.nombre.like(f"{MARK}%")).all():
            db.delete(o)  # cascade movimientos + cartolas + conciliaciones (+ ingreso)
        db.commit()
        for o in db.query(ContEgreso).filter(ContEgreso.beneficiario.like(f"%{MARK}%")).all():
            db.delete(o)
        db.commit()
        for f in db.query(ContFacturaCliente).filter(
                ContFacturaCliente.numero_factura.like(f"{MARK}%")).all():
            for lk in db.query(ConciliacionIngreso).filter(
                    ConciliacionIngreso.cobranza_id.in_([c.id for c in f.cobranzas] or [0])).all():
                db.delete(lk)
            db.delete(f)
        db.commit()
        cots = db.query(Cotizacion).filter(Cotizacion.numero.like(f"{MARK}%")).all()
        oc_ids = [oc.id for oc in db.query(OcCliente).filter(
            OcCliente.cotizacion_id.in_([c.id for c in cots])).all()] if cots else []
        if oc_ids:
            for a in db.query(ContAdelanto).filter(ContAdelanto.oc_cliente_id.in_(oc_ids)).all():
                db.query(ConciliacionIngreso).filter(
                    ConciliacionIngreso.adelanto_id == a.id).delete(synchronize_session=False)
                db.delete(a)
            db.query(OcCliente).filter(OcCliente.id.in_(oc_ids)).delete(synchronize_session=False)
        if cots:
            db.query(Cotizacion).filter(
                Cotizacion.id.in_([c.id for c in cots])).delete(synchronize_session=False)
        db.commit()
        resto = (db.query(CuentaBancaria).filter(CuentaBancaria.nombre.like(f"{MARK}%")).count()
                 + db.query(ContEgreso).filter(ContEgreso.beneficiario.like(f"%{MARK}%")).count()
                 + db.query(ContFacturaCliente).filter(
                     ContFacturaCliente.numero_factura.like(f"{MARK}%")).count()
                 + db.query(Cotizacion).filter(Cotizacion.numero.like(f"{MARK}%")).count()
                 + db.query(ContAdelanto).filter(
                     ContAdelanto.numero_operacion.like(f"{MARK}%")).count())
        print(f"\nCleanup OK (filas marcadas restantes: {resto})")
        if resto:
            _fails.append("cleanup dejó filas marcadas")
    finally:
        db.close()


def test_tesoreria_lecturas_de_plata():
    """Wrapper para pytest: sin él los checks correrían en el import y un fallo pasaría en
    silencio (verde falso)."""
    try:
        run()
    finally:
        _cleanup()
    assert not _fails, f"fallas: {_fails}"


if __name__ == "__main__":
    try:
        run()
    finally:
        _cleanup()
    print("\n=== RESULTADO:", "TODO OK" if not _fails else f"{len(_fails)} FALLAS: {_fails}", "===")
    sys.exit(1 if _fails else 0)
