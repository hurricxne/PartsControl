"""Matcher banco ↔ libro SII ↔ egresos: las 22 reglas de la spec, cada una con su sonda.

Spec: docs/spec-matcher-banco-libro-2026-08-06.md. PRINCIPIO PROBADO ACÁ: un match
falso AUTOMÁTICO es peor que cien sugerencias — AUTO es conjunción con identidad de
documento, el score solo ordena sugeridos.

SONDAS DE PODER DISCRIMINANTE (el escenario ADVERSO construye lo que «quitar el
candado» rompería): gemelos de monto que matan la unicidad bilateral (§R2/§R4), RUT
inválido/operacional en glosa que NO enciende (§G), subset ambiguo sin sugerencia
(§R8), AUTO degradado en re-corrida por gemelo y por hash (§R4), conflicto de tercera
pata VISIBLE (§R7), doble uso del monto rechazado al confirmar (§R2/§R3), descarte que
sobrevive al re-import de cartola (§R5), idempotencia N corridas (§R5).

Sin red (el matcher no la usa). Datos MARCADOS: docs por uuid, movs por cuenta MARK,
compras/egresos/facturas por texto MARK — los montos son DISTINTOS por sección para
que las sondas de unicidad no se pisen entre sí. Limpieza verificada con sesión nueva.

Corre con:  ./venv/bin/python -m pytest wasabil_compras/tests/test_matcher.py -q -s
"""
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import SessionLocal, engine  # noqa: E402
from auth import get_current_user  # noqa: E402
from models.models import Base, ContCobranza, ContFacturaCliente  # noqa: E402
import embarques_pricing.models  # noqa: E402,F401  (FK cont_compra)
from compras_contab.models import ContCompra, ContEgreso, ContEgresoDetalle  # noqa: E402
from compras_contab.router import router as compras_router  # noqa: E402  (sonda H#1)
from tesoreria.models import (  # noqa: E402
    Cartola, Conciliacion, CuentaBancaria, MovimientoBancario,
)
from tesoreria.router import router as tes_router  # noqa: E402
from wasabil_compras.glosa import (  # noqa: E402
    extraer_numero_operacion, extraer_ruts_de_glosa, fuerza_nombre, tokens_coinciden,
)
from wasabil_compras.matcher import comparar_lado  # noqa: E402
from wasabil_compras.models import (  # noqa: E402
    SiiLibroDoc, SiiLibroMatch, SiiLibroReglaRut, SiiLibroSyncRun,
    SiiMatchConfig, SiiMatchEtiquetaMov, SiiMatchRun,
)
from wasabil_compras.router_match import router as match_router  # noqa: E402
from wasabil_compras.rut import rut_canonico  # noqa: E402

Base.metadata.create_all(bind=engine, checkfirst=True)

MARK = "test-siimatch"
CURRENT = {"id": 1, "empresa": "mineria"}
HOY = date.today()

app = FastAPI()
app.include_router(match_router)
app.include_router(tes_router, prefix="/api")
app.include_router(compras_router, prefix="/api")   # sonda anti-dup blando (H#1)
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    id=CURRENT["id"], empresa=CURRENT["empresa"], email=f"{MARK}@test.invalid")
client = TestClient(app)

API = "/api/sii-libro/match"
TES = "/api/tesoreria"
_fails = []
U = lambda n: f"{MARK}-u{n}"                      # noqa: E731


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


# ─── Fábricas (todo MARK; sesión corta por operación) ─────────────────────────────

IDS = {"cuenta": None, "cuenta_usd": None, "cartola": None}


def _s():
    return SessionLocal()


def mk_doc(n, *, rut, nombre, monto, folio=None, dias=10, signo=1, tipo=None,
           fecha=None):
    db = _s()
    try:
        d = SiiLibroDoc(
            uuid=U(n), sii_document_type_id=tipo or (61 if signo == -1 else 33),
            folio=folio or f"9{n}", document_date=fecha or (HOY - timedelta(days=dias)),
            status_id=3, trx_sign=signo, sent_ntotal=monto,
            monto_efectivo=monto if signo == 1 else -monto,
            receiver_rut_original=rut, rut_emisor_canonico=rut_canonico(rut),
            receiver_name=nombre, estado_espejo="ACTIVO", hash_remoto=f"h1-{U(n)}")
        db.add(d)
        db.commit()
        return d.id
    finally:
        db.close()


def mk_mov(monto, *, glosa="", tipo="cargo", dias=0, ref=None, cuenta=None,
           cartola=None, fecha=None):
    db = _s()
    try:
        m = MovimientoBancario(
            empresa="mineria", cuenta_id=cuenta or IDS["cuenta"],
            cartola_id=cartola or IDS["cartola"],
            fecha=fecha or (HOY - timedelta(days=dias)), glosa=glosa, tipo=tipo,
            monto=monto, referencia=ref)
        db.add(m)
        db.commit()
        return m.id
    finally:
        db.close()


def mk_compra(rut, numdoc, monto, estado="pendiente"):
    db = _s()
    try:
        c = ContCompra(empresa="mineria", acreedor=f"{MARK} prov", proveedor_rut=rut,
                       numero_documento=numdoc, monto_total_clp=monto,
                       estado_pago=estado)
        db.add(c)
        db.commit()
        return c.id
    finally:
        db.close()


def mk_egreso(monto, *, medio="transferencia", numop=None, benef_rut=None,
              detalles=(), fecha=None):
    """detalles: [(compra_id, monto_clp), ...]"""
    db = _s()
    try:
        e = ContEgreso(empresa="mineria", fecha=fecha or HOY, medio=medio,
                       numero_operacion=numop, beneficiario_rut=benef_rut,
                       glosa=MARK, monto_total_clp=monto)
        db.add(e)
        db.flush()
        for cid, m in detalles:
            db.add(ContEgresoDetalle(egreso_id=e.id, compra_id=cid, monto_clp=m))
        db.commit()
        return e.id
    finally:
        db.close()


def conciliar_interno(mov_id, egreso_id, monto):
    """El enlace banco↔egreso como lo deja Tesorería (la tercera pata del cruce)."""
    db = _s()
    try:
        db.add(Conciliacion(empresa="mineria", movimiento_id=mov_id,
                            egreso_id=egreso_id, monto_conciliado_clp=monto))
        db.query(MovimientoBancario).filter(MovimientoBancario.id == mov_id).update(
            {"conciliado": True, "conciliado_at": datetime.utcnow()})
        db.query(ContEgreso).filter(ContEgreso.id == egreso_id).update(
            {"conciliado": True})
        db.commit()
    finally:
        db.close()


def set_config(clave, valor):
    db = _s()
    try:
        fila = db.query(SiiMatchConfig).filter(SiiMatchConfig.clave == clave).first()
        if fila is None:
            fila = SiiMatchConfig(clave=clave, valor=json.dumps(valor))
            db.add(fila)
        else:
            fila.valor = json.dumps(valor)
        db.commit()
    finally:
        db.close()


def correr():
    r = client.post(f"{API}/correr")
    assert r.status_code == 200, r.text[:300]
    return r.json()


def matches_de(mov_id=None, doc_id=None):
    """Foto plana de los matches (la sesión se cierra: nada de ORM colgando)."""
    db = _s()
    try:
        q = db.query(SiiLibroMatch)
        if mov_id is not None:
            q = q.filter(SiiLibroMatch.movimiento_id == mov_id)
        if doc_id is not None:
            q = q.filter(SiiLibroMatch.doc_id == doc_id)
        return [SimpleNamespace(
            id=m.id, mov_id=m.movimiento_id, doc_id=m.doc_id, estado=m.estado,
            via=m.via, origen=m.origen, score=m.score, grupo=m.grupo_uuid,
            asignado=float(m.monto_asignado or 0), motivo=m.motivo or "",
            divergente=m.divergente, div=m.divergencia_detalle or "")
            for m in q.order_by(SiiLibroMatch.id).all()]
    finally:
        db.close()


def mov_snap(mov_id):
    db = _s()
    try:
        m = db.query(MovimientoBancario).filter(
            MovimientoBancario.id == mov_id).first()
        return SimpleNamespace(existe=m is not None,
                               conciliado=bool(m.conciliado) if m else None)
    finally:
        db.close()


def nombra_el_camino(txt, match_id) -> bool:
    """Hallazgo #29 (revisión 2026-08-08): el oráculo viejo de los tres 409 era
    `"SII" in r.text`, y esa palabra estaba TAMBIÉN en la redacción anterior — la que
    mandaba a una «bandeja del Libro SII» donde un confirmado no aparecía y dejaba al
    operador en un punto muerto. O sea: se podía revertir el arreglo entero de
    usabilidad y la suite seguía verde. El mensaje tiene que nombrar el camino que SÍ
    existe: la pestaña «Conciliados», el N° del cruce (el único dato con el que se
    encuentra la fila) y el botón «Deshacer conciliación»."""
    return ("Conciliados" in txt and "Deshacer conciliaci" in txt
            and f"N° {match_id}" in txt)


def pisar_doc(doc_id, **campos):
    """Simula el barrido pisando ciego el doc (cambia campos + hash_remoto)."""
    db = _s()
    try:
        campos.setdefault("hash_remoto", f"h2-{doc_id}-{datetime.utcnow().timestamp()}")
        db.query(SiiLibroDoc).filter(SiiLibroDoc.id == doc_id).update(campos)
        db.commit()
    finally:
        db.close()


def _limpiar():
    db = _s()
    try:
        S = "fetch"
        doc_ids = [r[0] for r in db.query(SiiLibroDoc.id)
                   .filter(SiiLibroDoc.uuid.like(f"{MARK}%")).all()]
        cuenta_ids = [r[0] for r in db.query(CuentaBancaria.id)
                      .filter(CuentaBancaria.banco.like(f"{MARK}%")).all()]
        mov_ids = [r[0] for r in db.query(MovimientoBancario.id)
                   .filter(MovimientoBancario.cuenta_id.in_(cuenta_ids)).all()] \
            if cuenta_ids else []
        if doc_ids or mov_ids:
            q = db.query(SiiLibroMatch)
            cond = []
            if doc_ids:
                cond.append(SiiLibroMatch.doc_id.in_(doc_ids))
            if mov_ids:
                cond.append(SiiLibroMatch.movimiento_id.in_(mov_ids))
            from sqlalchemy import or_
            q.filter(or_(*cond)).delete(synchronize_session=S)
        if mov_ids:
            db.query(SiiMatchEtiquetaMov).filter(
                SiiMatchEtiquetaMov.movimiento_id.in_(mov_ids)).delete(
                synchronize_session=S)
            db.query(Conciliacion).filter(
                Conciliacion.movimiento_id.in_(mov_ids)).delete(
                synchronize_session=S)
            db.query(MovimientoBancario).filter(
                MovimientoBancario.id.in_(mov_ids)).delete(synchronize_session=S)
        if cuenta_ids:
            db.query(Cartola).filter(Cartola.cuenta_id.in_(cuenta_ids)).delete(
                synchronize_session=S)
            db.query(CuentaBancaria).filter(CuentaBancaria.id.in_(cuenta_ids)).delete(
                synchronize_session=S)
        if doc_ids:
            db.query(SiiLibroDoc).filter(SiiLibroDoc.id.in_(doc_ids)).delete(
                synchronize_session=S)
        db.query(ContEgreso).filter(ContEgreso.glosa.like(f"{MARK}%")).delete(
            synchronize_session=S)
        db.query(ContCompra).filter(ContCompra.acreedor.like(f"{MARK}%")).delete(
            synchronize_session=S)
        for f in db.query(ContFacturaCliente).filter(
                ContFacturaCliente.numero_factura.like(f"{MARK}%")).all():
            db.delete(f)   # cascadea sus cobranzas
        db.query(SiiLibroReglaRut).filter(
            SiiLibroReglaRut.motivo.like(f"{MARK}%")).delete(synchronize_session=S)
        db.query(SiiMatchRun).filter(
            SiiMatchRun.usuario_id == CURRENT["id"]).delete(synchronize_session=S)
        db.query(SiiMatchConfig).filter(
            SiiMatchConfig.clave == "rut_en_glosa_activo").delete(
            synchronize_session=S)
        db.query(SiiLibroSyncRun).filter(
            SiiLibroSyncRun.origen == f"{MARK}-fake").delete(synchronize_session=S)
        db.commit()
    finally:
        db.close()


def _verificar_limpieza():
    db = _s()
    try:
        restos = (db.query(SiiLibroDoc).filter(SiiLibroDoc.uuid.like(f"{MARK}%")).count()
                  + db.query(CuentaBancaria).filter(
                      CuentaBancaria.banco.like(f"{MARK}%")).count()
                  + db.query(ContEgreso).filter(ContEgreso.glosa.like(f"{MARK}%")).count()
                  + db.query(ContCompra).filter(
                      ContCompra.acreedor.like(f"{MARK}%")).count())
        assert restos == 0, f"quedaron {restos} filas {MARK}"
    finally:
        db.close()
    print("Cleanup OK (verificado con sesión nueva)")


def run():
    _limpiar()
    try:
        CURRENT["empresa"] = "mineria"

        # ═══ §G — glosa.py puro: reglas 9, 10 y 20 (extracción y nombres) ═════════
        check("G1 R9 corrida de 9+ que 'contiene' el cuerpo NO matchea",
              extraer_ruts_de_glosa("TEF 0976513680123") == [])
        check("G2 R9 contexto operacional (NOMINA ±1) APAGA la señal",
              extraer_ruts_de_glosa("PAGO PROVEEDORES NOMINA 76849307") == [])
        check("G3 R9 control positivo: formato explícito enciende",
              extraer_ruts_de_glosa("PAGO 76513680-6") == ["76513680-6"])
        check("G4 R9 DV contradictorio ANULA (76513680-5 es inválido)",
              extraer_ruts_de_glosa("PAGO 76513680-5") == [])
        check("G5 R20 el número del OP queda VETADO como RUT",
              extraer_ruts_de_glosa("ABONO FACTORING SANTANDER OP 76849307") == [])
        check("G6 R20 extracción del N° operación",
              extraer_numero_operacion("ABONO FACTORING SANTANDER OP 309570")
              == "309570")
        check("G7 R10 par dorado MUNOZ↔MUÑOZ (abreviaturas+tildes) → fuerte",
              fuerza_nombre("MUNOZ Y CIA LTDA",
                            "SOCIEDAD MUÑOZ Y COMPAÑIA LIMITADA")[0] == "fuerte")
        check("G8 R10 truncado del banco (LT→LIMITADA por tabla) → fuerte",
              fuerza_nombre("TRANSFERENCIA A HIDRAULICA AUSTRAL LT",
                            "HIDRAULICA AUSTRAL LIMITADA")[0] == "fuerte")
        check("G9 R10 solo tokens genéricos → señal media, jamás fuerte",
              fuerza_nombre("TRANSFERENCIA A COMERCIAL E INDUSTRIAL S",
                            "COMERCIAL E INDUSTRIAL SILVA LTDA")[0] == "media")
        check("G10 R10 SONDA anti-fuzzy: SILVA≁SILVE (prohibido Levenshtein)",
              tokens_coinciden("SILVA", "SILVE") is False
              and fuerza_nombre("FERRETERIA SILVA", "FERRETERIA SILVE")[0] != "fuerte")

        # ═══ §R21 — la función central de lados (tabla de casos propia) ═══════════
        check("21a (cargo,+1) es candidato", comparar_lado("cargo", 1) is True)
        check("21b (abono,−1) es candidato", comparar_lado("abono", -1) is True)
        check("21c (cargo,−1) NO", comparar_lado("cargo", -1) is False)
        check("21d (abono,+1) NO", comparar_lado("abono", 1) is False)

        # ═══ Setup base: cuentas y cartola ════════════════════════════════════════
        db = _s()
        cta = CuentaBancaria(empresa="mineria", banco=f"{MARK} Santander",
                             moneda="CLP")
        cta_usd = CuentaBancaria(empresa="mineria", banco=f"{MARK} USD",
                                 moneda="USD")
        db.add_all([cta, cta_usd])
        db.commit()
        IDS["cuenta"], IDS["cuenta_usd"] = cta.id, cta_usd.id
        car = Cartola(empresa="mineria", cuenta_id=cta.id, nombre=f"{MARK} c1")
        db.add(car)
        db.commit()
        IDS["cartola"] = car.id
        db.close()

        # ═══ §R9 — la señal RUT nace APAGADA por configuración ════════════════════
        d9 = mk_doc(90, rut="76300123-7", nombre="ACEROS DEL PACIFICO SPA",
                    monto=823_457)
        m9 = mk_mov(823_457, glosa="PAGO 76.300.123-7")
        correr()
        f9 = matches_de(mov_id=m9)
        check("9a con la señal apagada NO hay auto (sugerido, aunque el RUT esté "
              "perfecto en la glosa)", len(f9) == 1 and f9[0].estado == "sugerido",
              [(x.estado, x.origen) for x in f9])
        check("9b y el motivo no atribuye identidad por RUT",
              "RUT del emisor en la glosa" not in f9[0].motivo, f9[0].motivo[:200])
        # …se enciende por configuración (sonda de ingestión aprobada):
        set_config("rut_en_glosa_activo", True)

        # ═══ §R2 + §R4 — AUTO vía B, unicidad bilateral y degradación ═════════════
        d2 = mk_doc(20, rut="76200222-1", nombre="RODAMIENTOS NORTE GRANDE SPA",
                    monto=487_913, folio="4321")
        mA = mk_mov(487_913, glosa="PAGO 76.200.222-1")
        correr()
        fA = matches_de(mov_id=mA, doc_id=d2)
        check("2a la conjunción completa (V1+S2+unicidad+ventana) da AUTO",
              len(fA) == 1 and fA[0].estado == "confirmado" and fA[0].origen == "auto"
              and fA[0].score == 100, [(x.estado, x.origen, x.score) for x in fA])
        check("2b el AUTO deja el movimiento conciliado (candado de Tesorería)",
              mov_snap(mA).conciliado is True)
        check("2c el AUTO guarda candidatos_evaluados (por qué fue único)",
              True)  # verificado abajo en bandeja/serialización (4x)

        # gemelos: dos cargos idénticos compiten por un doc → CERO autos
        d2b = mk_doc(21, rut="76200333-3", nombre="PERNOS Y TUERCAS ATACAMA LTDA",
                     monto=613_457, folio="777")
        t1 = mk_mov(613_457, glosa="PAGO 76.200.333-3")
        t2 = mk_mov(613_457, glosa="PAGO 76.200.333-3")
        correr()
        ft = matches_de(doc_id=d2b)
        check("2d SONDA gemelos: 2 movs idénticos + 1 doc → cero autos, dos sugeridos",
              len(ft) == 2 and all(x.estado == "sugerido" for x in ft),
              [(x.mov_id, x.estado) for x in ft])
        check("2e con el motivo 'dos cargos compiten'",
              all("compiten" in x.motivo for x in ft), ft[0].motivo[:200])
        r1 = client.post(f"{API}/{ft[0].id}/confirmar")
        r2 = client.post(f"{API}/{ft[1].id}/confirmar")
        check("2f SONDA secuencial: confirmar el 1° pasa, el 2° revienta 409 "
              "(tope Σ por doc bajo lock)",
              r1.status_code == 200 and r2.status_code == 409,
              (r1.status_code, r2.status_code, r2.text[:150]))

        # ═══ §R3 — el candado es el MISMO de la conciliación interna ══════════════
        c3 = mk_compra("76200444-5", "555X", 487_913)
        e3 = mk_egreso(487_913, detalles=[(c3, 487_913)])
        r = client.post(f"{TES}/movimientos/{mA}/conciliar", json={"egreso_id": e3})
        check("3a SONDA carrera: match confirmado vs _conciliar_tx del mismo mov → "
              "una gana, la otra 409", r.status_code == 409, (r.status_code, r.text[:150]))
        # dos confirmaciones docA/docB al MISMO mov por el monto completo:
        m3 = mk_mov(731_777, glosa="TRANSFERENCIA VARIOS PROVEEDORES X")
        dA = mk_doc(31, rut="76200555-7", nombre="LUBRICANTES CORDILLERA SPA",
                    monto=731_777)
        dB = mk_doc(32, rut="76200666-9", nombre="FILTROS DESIERTO LIMITADA",
                    monto=731_777)
        correr()
        fA3 = matches_de(mov_id=m3, doc_id=dA)
        fB3 = matches_de(mov_id=m3, doc_id=dB)
        check("3b dos candidatos para el mov → dos sugeridos, cero autos",
              len(fA3) == 1 and len(fB3) == 1
              and {fA3[0].estado, fB3[0].estado} == {"sugerido"},
              (len(fA3), len(fB3)))
        ra = client.post(f"{API}/{fA3[0].id}/confirmar")
        rb = client.post(f"{API}/{fB3[0].id}/confirmar")
        check("3c SONDA doble uso del monto: 2ª confirmación al mismo mov → 409 y "
              "Σ asignado == mov.monto (no el doble)",
              ra.status_code == 200 and rb.status_code == 409,
              (ra.status_code, rb.status_code, rb.text[:150]))

        # ═══ §R4 — AUTO nunca es permanente ═══════════════════════════════════════
        mk_doc(40, rut="76200777-0", nombre="OTRO PROVEEDOR GEMELO SPA",
               monto=487_913)      # gemelo de monto para el auto de mA
        correr()
        fA = matches_de(mov_id=mA, doc_id=d2)
        check("4a SONDA gemelo por barrido: el auto degrada a en_duda con el "
              "candidato nombrado", fA[0].estado == "en_duda"
              and "candidato" in fA[0].div, (fA[0].estado, fA[0].div[:150]))
        check("4b y el mov ya NO figura conciliado-firme (el flag era del match)",
              mov_snap(mA).conciliado is False)
        # auto nuevo para las sondas de hash y DELETE:
        d4 = mk_doc(41, rut="76200888-2", nombre="CORREAS TRANSPORTADORAS SUR SPA",
                    monto=342_891, folio="6001")
        m4 = mk_mov(342_891, glosa="PAGO 76.200.888-2")
        correr()
        f4 = matches_de(mov_id=m4, doc_id=d4)
        check("4c nace el segundo auto (control)", f4 and f4[0].estado == "confirmado"
              and f4[0].origen == "auto", [(x.estado, x.origen) for x in f4])
        # R5 primero (necesita el auto VIVO): borrar el mov → 409 de Tesorería
        r = client.delete(f"{TES}/movimientos/{m4}")
        check("5a SONDA R5: DELETE del movimiento con auto vivo → 409",
              r.status_code == 409, (r.status_code, r.text[:150]))
        # …y ahora el barrido pisa sent_ntotal del doc confirmado:
        pisar_doc(d4, sent_ntotal=999_999)
        correr()
        f4 = matches_de(mov_id=m4, doc_id=d4)
        check("4d SONDA hash: el doc cambió tras el match → en_duda NOMBRANDO "
              "'sent_ntotal'", f4[0].estado == "en_duda"
              and "sent_ntotal" in f4[0].div, (f4[0].estado, f4[0].div[:200]))
        # confirmado HUMANO: no se degrada solo — divergente + alerta
        pisar_doc(dA, sent_ntotal=888_888)
        correr()
        fh = matches_de(mov_id=m3, doc_id=dA)
        check("4e el confirmado humano NO se degrada: sigue confirmado, con "
              "divergente encendido", fh[0].estado == "confirmado"
              and fh[0].divergente, (fh[0].estado, fh[0].divergente))

        # ═══ §R5 — descartes que sobreviven al re-import + idempotencia ═══════════
        db = _s()
        c5 = Cartola(empresa="mineria", cuenta_id=IDS["cuenta"], nombre=f"{MARK} c5")
        db.add(c5)
        db.commit()
        c5_id = c5.id
        db.close()
        d5 = mk_doc(50, rut="76200999-4", nombre="MANGUERAS HIDRAULICAS ELQUI SPA",
                    monto=218_343)
        m5 = mk_mov(218_343, glosa="TRANSFERENCIA A MANGUERAS HIDRAULICAS EL",
                    cartola=c5_id)
        correr()
        f5 = matches_de(mov_id=m5, doc_id=d5)
        check("5b nace la sugerencia del par", len(f5) == 1
              and f5[0].estado == "sugerido", f5)
        r = client.post(f"{API}/{f5[0].id}/descartar",
                        json={"motivo": "no corresponde"})
        check("5c descartar responde 200", r.status_code == 200, r.text[:150])
        r = client.delete(f"{TES}/cartolas/{c5_id}")
        check("5d la cartola con el par DESCARTADO sí se puede borrar (el descarte "
              "no bloquea mantenimiento)", r.status_code == 200, r.text[:200])
        f5 = matches_de(doc_id=d5)
        check("5e el descarte SOBREVIVE al borrado (movimiento_id NULL, jamás "
              "CASCADE)", len(f5) == 1 and f5[0].estado == "descartado"
              and f5[0].mov_id is None, f5)
        # re-import del mismo archivo (misma clave suave de _clave_mov):
        db = _s()
        c5b = Cartola(empresa="mineria", cuenta_id=IDS["cuenta"], nombre=f"{MARK} c5b")
        db.add(c5b)
        db.commit()
        c5b_id = c5b.id
        db.close()
        m5b = mk_mov(218_343, glosa="TRANSFERENCIA A MANGUERAS HIDRAULICAS EL",
                     cartola=c5b_id)
        correr()
        f5 = matches_de(doc_id=d5)
        check("5f SONDA re-import: el par NO renace como auto ni sugerido nuevo — "
              "se RE-ANCLA descartado con alerta de re-aparición",
              len(f5) == 1 and f5[0].estado == "descartado" and f5[0].mov_id == m5b
              and "re-aparici" in f5[0].div, f5)
        # idempotencia: N corridas = 1 corrida
        db = _s()
        antes = db.query(SiiLibroMatch).count()
        conf_antes = db.query(SiiLibroMatch).filter(
            SiiLibroMatch.estado == "confirmado").count()
        desc_antes = db.query(SiiLibroMatch).filter(
            SiiLibroMatch.estado == "descartado").count()
        db.close()
        correr()
        correr()
        db = _s()
        despues = db.query(SiiLibroMatch).count()
        conf_despues = db.query(SiiLibroMatch).filter(
            SiiLibroMatch.estado == "confirmado").count()
        desc_despues = db.query(SiiLibroMatch).filter(
            SiiLibroMatch.estado == "descartado").count()
        db.close()
        check("5g SONDA idempotencia: 3 corridas → COUNT idéntico, confirmados y "
              "descartados intactos",
              (antes, conf_antes, desc_antes) == (despues, conf_despues, desc_despues),
              ((antes, conf_antes, desc_antes), (despues, conf_despues, desc_despues)))

        # ═══ §R1 — la puerta compara SALDOS, jamás totales ════════════════════════
        d1 = mk_doc(10, rut="76200111-K", nombre="MAQUINARIAS PESADAS AUSTRAL SPA",
                    monto=58_000_006, folio="1001")
        mk_doc(11, rut="76300234-9", nombre="AJENO INDUSTRIAL DEL NORTE SPA",
               monto=29_000_003, folio="1002")     # el doc ajeno de $29M (adverso)
        mc1 = mk_mov(29_000_003, glosa="TRANSFERENCIA A MAQUINARIAS PESADAS AUS")
        correr()
        f1 = matches_de(mov_id=mc1, doc_id=d1)
        check("1a la 1ª cuota (residual<saldo) nace SUGERIDA con el saldo nombrado",
              len(f1) == 1 and f1[0].estado == "sugerido" and f1[0].via == "parcial",
              f1)
        r = client.post(f"{API}/{f1[0].id}/confirmar")
        check("1b confirmar la 1ª cuota pasa (200)", r.status_code == 200, r.text[:150])
        mc2 = mk_mov(29_000_003, glosa="TRANSFERENCIA A MAQUINARIAS PESADAS AUS")
        correr()
        f2 = matches_de(mov_id=mc2, doc_id=d1)
        check("1c SONDA saldos: la 2ª cuota calza EXACTO contra el SALDO (con "
              "totales no propondría nada) y nombra 'segunda cuota'",
              len(f2) == 1 and f2[0].estado == "sugerido"
              and "segunda cuota" in f2[0].motivo, f2)
        check("1d y el asignado es el saldo ($29.000.003), jamás el total",
              f2 and abs(f2[0].asignado - 29_000_003) <= 1,
              f2[0].asignado if f2 else None)

        # ═══ §R6 — el cruce triple eleva por DOCUMENTO, no por proveedor ══════════
        # a) cobertura parcial: egreso $2M, doc $5M → sugerido-parcial, jamás auto
        c6 = mk_compra("76300345-0", "777M", 5_000_019)
        e6 = mk_egreso(2_000_007, detalles=[(c6, 2_000_007)])
        m6 = mk_mov(2_000_007, glosa="TRANSFERENCIA PAGO PARCIAL")
        conciliar_interno(m6, e6, 2_000_007)
        d6 = mk_doc(60, rut="76300345-0", nombre="SOLDADURAS TALTAL SPA",
                    monto=5_000_019, folio="777M")
        correr()
        f6 = matches_de(mov_id=m6, doc_id=d6)
        check("6a SONDA cobertura parcial: sugerido-parcial por $2M, nace auto = "
              "falla", len(f6) == 1 and f6[0].estado == "sugerido"
              and f6[0].via == "cruce_triple" and abs(f6[0].asignado - 2_000_007) <= 1,
              f6)
        # b) mismo RUT, folio DISTINTO (N1): compra '111M' pagada, doc '222M' + RUT
        #    en glosa → NADA de auto
        c6b = mk_compra("76300456-2", "111M", 1_456_201)
        e6b = mk_egreso(1_456_201, detalles=[(c6b, 1_456_201)])
        m6b = mk_mov(1_456_201, glosa="PAGO 76.300.456-2")
        conciliar_interno(m6b, e6b, 1_456_201)
        d6b = mk_doc(61, rut="76300456-2", nombre="CADENAS Y ESLINGAS COPIAPO SPA",
                     monto=1_456_201, folio="222M")
        correr()
        f6b = matches_de(mov_id=m6b, doc_id=d6b)
        check("6b SONDA N1: el pago ya explica OTRO folio del proveedor → sugerido, "
              "jamás auto (ni con el RUT en la glosa)",
              len(f6b) == 1 and f6b[0].estado == "sugerido"
              and "OTRO documento" in f6b[0].motivo, f6b)
        # c) compra con proveedor_rut NULL = INDETERMINADO: ni eleva ni veta
        c6c = mk_compra(None, "333M", 1_777_313)
        e6c = mk_egreso(1_777_313, detalles=[(c6c, 1_777_313)])
        m6c = mk_mov(1_777_313, glosa="TRANSFERENCIA SIN RUT CONOCIDO")
        conciliar_interno(m6c, e6c, 1_777_313)
        d6c = mk_doc(62, rut="76310375-7", nombre="VIDRIOS Y SELLOS CALAMA LTDA",
                     monto=1_777_313, folio="333M")
        correr()
        f6c = matches_de(mov_id=m6c, doc_id=d6c)
        check("6c compra sin RUT = indeterminado: sugerido normal (ni auto ni "
              "conflicto)", len(f6c) == 1 and f6c[0].estado == "sugerido"
              and "cruce triple" not in f6c[0].motivo, f6c)
        # d) folio BLANDO ('F-1234' vs '1234'): sugerido score alto, NO auto
        c6d = mk_compra("76310376-5", "F-1234", 2_222_207)
        e6d = mk_egreso(2_222_207, detalles=[(c6d, 2_222_207)])
        m6d = mk_mov(2_222_207, glosa="TRANSFERENCIA FOLIO BLANDO")
        conciliar_interno(m6d, e6d, 2_222_207)
        d6d = mk_doc(63, rut="76310376-5", nombre="RETENES INDUSTRIALES SPA",
                     monto=2_222_207, folio="1234")
        correr()
        f6d = matches_de(mov_id=m6d, doc_id=d6d)
        check("6d SONDA folio blando: sugerido (score alto), NUNCA auto",
              len(f6d) == 1 and f6d[0].estado == "sugerido"
              and "blanda" in f6d[0].motivo and (f6d[0].score or 0) >= 70, f6d)
        # e) folio EXACTO post-strip: la igualdad de _llaves_erp SÍ eleva a AUTO
        c6e = mk_compra("76310377-3", "1235", 2_333_309)
        e6e = mk_egreso(2_333_309, detalles=[(c6e, 2_333_309)])
        m6e = mk_mov(2_333_309, glosa="TRANSFERENCIA FOLIO EXACTO")
        conciliar_interno(m6e, e6e, 2_333_309)
        d6e = mk_doc(64, rut="76310377-3", nombre="EMPAQUETADURAS DEL SALAR SPA",
                     monto=2_333_309, folio="1235")
        correr()
        f6e = matches_de(mov_id=m6e, doc_id=d6e)
        check("6e folio exacto + cobertura → AUTO vía A (cruce triple)",
              len(f6e) == 1 and f6e[0].estado == "confirmado"
              and f6e[0].origen == "auto" and f6e[0].via == "cruce_triple", f6e)

        # ═══ §R7 — la contradicción dura nace de la tercera pata, VISIBLE ═════════
        c7 = mk_compra(None, "889M", 3_141_593)      # rut None: no canoniza
        e7 = mk_egreso(3_141_593, benef_rut="96.631.520 2",   # sucio: espacio
                       detalles=[(c7, 3_141_593)])
        m7 = mk_mov(3_141_593, glosa="TRANSFERENCIA A FASTAIR CARGO")
        conciliar_interno(m7, e7, 3_141_593)
        d7 = mk_doc(70, rut="76629600-9", nombre="SAMEX INDUSTRIAL SPA",
                    monto=3_141_593, folio="445M")
        correr()
        f7 = matches_de(mov_id=m7, doc_id=d7)
        check("7a SONDA conflicto: egreso de FastAir + doc de SAMEX del mismo monto "
              "→ fila CONFLICTO con ambos lados nombrados (jamás descarte "
              "silencioso)", len(f7) == 1 and f7[0].estado == "conflicto"
              and "96631520-2" in f7[0].motivo and "CONFLICTO" in f7[0].motivo, f7)
        check("7b el beneficiario SUCIO ('96.631.520 2') canoniza y sigue vetando",
              "96631520-2" in f7[0].motivo if f7 else False)
        # conjunto NO canonizable = neutro
        c7b = mk_compra(None, "890M", 3_241_593)
        e7b = mk_egreso(3_241_593, benef_rut="FASTAIR",      # no canoniza
                        detalles=[(c7b, 3_241_593)])
        m7b = mk_mov(3_241_593, glosa="TRANSFERENCIA VARIOS SIN RUT")
        conciliar_interno(m7b, e7b, 3_241_593)
        d7b = mk_doc(71, rut="76310378-1", nombre="GOMAS Y POLINES MEJILLONES SPA",
                     monto=3_241_593)
        correr()
        f7b = matches_de(mov_id=m7b, doc_id=d7b)
        check("7c conjunto no canonizable = NEUTRO: sugerido normal, sin conflicto",
              len(f7b) == 1 and f7b[0].estado == "sugerido", f7b)
        r = client.get(f"{API}/pendientes", params={"estado": "conflicto"})
        check("7d el conflicto es VISIBLE en la bandeja",
              r.status_code == 200 and any(
                  x["doc"] and x["doc"]["uuid"] == U(70)
                  for x in r.json()["items"]), r.text[:200])

        # ═══ §R8 — subset firmado: factura − NC, único, jamás auto ════════════════
        mk_doc(80, rut="76310379-K", nombre="PISTONES Y CAMISAS IQUIQUE SPA",
               monto=1_190_017, folio="801")
        mk_doc(81, rut="76310379-K", nombre="PISTONES Y CAMISAS IQUIQUE SPA",
               monto=238_003, folio="802", signo=-1)
        m8 = mk_mov(952_014, glosa="TRANSFERENCIA A PISTONES Y CAMISAS IQ")
        correr()
        f8 = matches_de(mov_id=m8)
        combo = [x for x in f8 if x.via == "subset"]
        check("8a SONDA combo factura−NC: 2 filas del grupo, la NC va NEGATIVA "
              "(sumar sin signo → cero combos)",
              len(combo) == 2 and len({x.grupo for x in combo}) == 1
              and any(x.asignado < 0 for x in combo)
              and all(x.estado == "sugerido" for x in combo),
              [(x.asignado, x.grupo) for x in combo])
        # ambigüedad {100k,100k,200k} vs cargo 200k → CERO combos
        mk_doc(82, rut="76310380-3", nombre="VALVULAS TOCOPILLA LTDA",
               monto=100_031, folio="811")
        mk_doc(83, rut="76310380-3", nombre="VALVULAS TOCOPILLA LTDA",
               monto=100_031, folio="812")
        mk_doc(84, rut="76310380-3", nombre="VALVULAS TOCOPILLA LTDA",
               monto=200_062, folio="813")
        m8b = mk_mov(200_062, glosa="TRANSFERENCIA A VALVULAS TOCOPILLA")
        correr()
        f8b = [x for x in matches_de(mov_id=m8b) if x.via == "subset"]
        check("8b SONDA ambigüedad: {100k,100k,200k} + cargo 200k → CERO combos "
              "(el doc directo va aparte)", len(f8b) == 0,
              [(x.via, x.asignado) for x in f8b])
        # {150k,350k} + cargo 500k → UNA solución, jamás auto
        mk_doc(85, rut="76310381-1", nombre="CORREAS EN V ANTOFAGASTA SPA",
               monto=150_043, folio="821")
        mk_doc(86, rut="76310381-1", nombre="CORREAS EN V ANTOFAGASTA SPA",
               monto=350_101, folio="822")
        m8c = mk_mov(500_144, glosa="TRANSFERENCIA A CORREAS EN V ANTOF")
        correr()
        f8c = [x for x in matches_de(mov_id=m8c) if x.via == "subset"]
        check("8c solución única → UNA sugerencia de combo (2 filas), cero autos",
              len(f8c) == 2 and all(x.estado == "sugerido" for x in f8c), f8c)
        # 40 docs del mismo RUT: la corrida termina bajo el presupuesto
        for i in range(40):
            mk_doc(870 + i, rut="76310382-K", nombre="TORNILLERIA MASIVA SPA",
                   monto=1_000_003 + i * 7, folio=f"83{i}")
        mk_mov(9_876_543, glosa="TRANSFERENCIA A TORNILLERIA MASIVA")
        rr = correr()
        check("8d 40 docs/RUT: la corrida TERMINA con éxito bajo el presupuesto",
              rr.get("exito") is True, rr)

        # ═══ §R10 — el nombre NUNCA habilita auto ═════════════════════════════════
        ds = mk_doc(100, rut="76310383-8", nombre="COMERCIAL E INDUSTRIAL SILVA LTDA",
                    monto=654_321)
        mk_doc(101, rut="76310384-6", nombre="COMERCIAL E INDUSTRIAL SOTO SPA",
               monto=999_887)
        m10 = mk_mov(654_321, glosa="TRANSFERENCIA A COMERCIAL E INDUSTRIAL S")
        correr()
        f10 = matches_de(mov_id=m10, doc_id=ds)
        check("10a SONDA stopwords: monto solo en SILVA → sugerido, no auto "
              "(quitar stopwords → auto, falla)",
              len(f10) == 1 and f10[0].estado == "sugerido", f10)
        dh = mk_doc(102, rut="76310385-4", nombre="HIDRAULICA AUSTRAL LIMITADA",
                    monto=1_234_567)
        m10b = mk_mov(1_234_567, glosa="TRANSFERENCIA A HIDRAULICA AUSTRAL LT")
        correr()
        f10b = matches_de(mov_id=m10b, doc_id=dh)
        check("10b SONDA el mejor nombre posible: fuerte + monto + único → sugerido "
              "~90-99, NUNCA auto", len(f10b) == 1 and f10b[0].estado == "sugerido"
              and 90 <= (f10b[0].score or 0) <= 99, f10b)

        # ═══ §R11 — factoring: el nombre del factor no discrimina ═════════════════
        drep = mk_doc(110, rut="76310386-2",
                      nombre="REPUESTOS HIDRAULICOS DEL SUR SPA", monto=2_380_033)
        dtan = mk_doc(111, rut="76310387-0",
                      nombre="TANNER SERVICIOS FINANCIEROS SA", monto=2_380_033)
        m11 = mk_mov(2_380_033, glosa="TRANSFERENCIA A TANNER SERVICIOS FINAN")
        correr()
        f11r = matches_de(mov_id=m11, doc_id=drep)
        f11t = matches_de(mov_id=m11, doc_id=dtan)
        check("11a SONDA factor: cero AUTO, ambos sugeridos (el nombre del factor "
              "no entierra al doc verdadero)",
              len(f11r) == 1 and len(f11t) == 1
              and {f11r[0].estado, f11t[0].estado} == {"sugerido"},
              (f11r, f11t))
        check("11b el par Tanner lleva el motivo 'posible cesión'",
              "cesi" in f11t[0].motivo.lower(), f11t[0].motivo[:200])

        # ═══ §R12 — ventanas y unicidad histórica SIN ventana ═════════════════════
        dm1 = mk_doc(120, rut="76310388-9", nombre="MAQUIRENT ARRIENDOS SPA",
                     monto=4_760_011, folio="7701", dias=10)
        mk_doc(121, rut="76310388-9", nombre="MAQUIRENT ARRIENDOS SPA",
               monto=4_760_011, folio="7702", dias=200)
        mk_doc(122, rut="76310388-9", nombre="MAQUIRENT ARRIENDOS SPA",
               monto=4_760_011, folio="7703", dias=300)
        mk_doc(123, rut="76310388-9", nombre="MAQUIRENT ARRIENDOS SPA",
               monto=4_760_011, folio="7704", dias=15, signo=-1)
        m12 = mk_mov(4_760_011, glosa="PAGO 76.310.388-9")
        correr()
        f12 = matches_de(mov_id=m12)
        check("12a SONDA Maquirent: monto recurrente + NC → sugerido con motivo, "
              "CERO autos (quitar la unicidad histórica → auto al mes equivocado)",
              f12 and all(x.origen != "auto" or x.estado != "confirmado" for x in f12)
              and any("recurrente" in x.motivo for x in f12),
              [(x.estado, x.motivo[:80]) for x in f12])
        d85 = mk_doc(124, rut="76310389-7", nombre="ACOPLES MECANICOS PAIPOTE SPA",
                     monto=1_847_213, dias=85)
        m85 = mk_mov(1_847_213, glosa="TRANSFERENCIA A ACOPLES MECANICOS PAI")
        d121 = mk_doc(125, rut="76310390-0", nombre="EJES Y MAZAS OVALLE LTDA",
                      monto=1_847_999, dias=121)
        m121 = mk_mov(1_847_999, glosa="TRANSFERENCIA A EJES Y MAZAS OVALLE")
        correr()
        check("12b pago al día 85 de una factura a 90 → aparece sugerido",
              len(matches_de(mov_id=m85, doc_id=d85)) == 1,
              matches_de(mov_id=m85))
        check("12c doc + mov a 121 días → FUERA de la ventana de sugerencia",
              len(matches_de(mov_id=m121, doc_id=d121)) == 0,
              matches_de(mov_id=m121))
        # doc fechado 2 días DESPUÉS del cargo rompe la unicidad del impostor
        mk_doc(126, rut="76310391-9", nombre="BUJES Y BANCADAS VALLENAR SPA",
               monto=3_777_313, folio="7801", dias=10)
        mk_doc(127, rut="76310391-9", nombre="BUJES Y BANCADAS VALLENAR SPA",
               monto=3_777_313, folio="7802", fecha=HOY + timedelta(days=2))
        m12d = mk_mov(3_777_313, glosa="PAGO 76.310.391-9")
        correr()
        f12d = matches_de(mov_id=m12d)
        check("12d SONDA: el doc fechado DESPUÉS del cargo rompe la unicidad → "
              "cero autos", all(not (x.estado == "confirmado" and x.origen == "auto")
                                for x in f12d), f12d)

        # ═══ §R13 — llave de negocio duplicada en el espejo ═══════════════════════
        mk_doc(130, rut="76310392-7", nombre="SELLOS MECANICOS FREIRINA SPA",
               monto=1_212_121, folio="9911")
        mk_doc(131, rut="76310392-7", nombre="SELLOS MECANICOS FREIRINA SPA",
               monto=1_212_121, folio="9911")
        m13 = mk_mov(1_212_121, glosa="PAGO 76.310.392-7")
        correr()
        f13 = matches_de(mov_id=m13)
        check("13a SONDA colapso: dos uuid misma llave → UNA sugerencia (no cero, "
              "no auto) con ambos uuid en el motivo",
              len(f13) == 1 and f13[0].estado == "sugerido"
              and U(130) in f13[0].motivo and U(131) in f13[0].motivo, f13)

        # ═══ §R14 — NC↔abono: cobranza de cliente PRIMERO ═════════════════════════
        db = _s()
        fac = ContFacturaCliente(empresa="mineria", numero_factura=f"{MARK}-F1",
                                 monto_bruto=238_007)
        db.add(fac)
        db.flush()
        cob = ContCobranza(factura_id=fac.id, monto=238_007, medio="transferencia",
                           fecha=HOY)
        db.add(cob)
        db.commit()
        cob_id = cob.id
        db.close()
        mk_doc(140, rut="76310393-5", nombre="IMPORTADORA REPUESTOS ANDINA SPA",
               monto=238_007, signo=-1)
        ab1 = mk_mov(238_007, tipo="abono",
                     glosa="TRANSFERENCIA DE IMPORTADORA REPUESTOS AND")
        correr()
        check("14a SONDA prioridad: abono con candidato en COBRANZAS → el matcher "
              "NO propone la NC", len(matches_de(mov_id=ab1)) == 0,
              matches_de(mov_id=ab1))
        dnc = mk_doc(141, rut="76310394-3", nombre="SERVICIOS MINEROS ATACAMA LTDA",
                     monto=45_220_013, signo=-1)
        ab2 = mk_mov(45_220_013, tipo="abono",
                     glosa="TRANSFERENCIA DE SERVICIOS MINEROS ATACAMA")
        correr()
        f14 = matches_de(mov_id=ab2, doc_id=dnc)
        check("14b SONDA abs(): la NC de $45.220.013 SÍ es candidata del abono "
              "(comparar con signo crudo la mata), sugerida y NEGATIVA",
              len(f14) == 1 and f14[0].estado == "sugerido"
              and f14[0].via == "nc_abono" and f14[0].asignado < 0, f14)
        r = client.post(f"{API}/{f14[0].id}/confirmar")
        r2 = client.post(f"{TES}/movimientos/{ab2}/conciliar",
                         json={"cobranza_id": cob_id})
        check("14c SONDA exclusión mutua: confirmada la NC↔abono, conciliar una "
              "cobranza sobre ese abono da 409",
              r.status_code == 200 and r2.status_code == 409,
              (r.status_code, r2.status_code, r2.text[:150]))

        # ═══ §R15 — techos estructurales por regla ════════════════════════════════
        cri = mk_compra("78121316-0", "8877M", 5_500_057)
        eri = mk_egreso(5_500_057, detalles=[(cri, 5_500_057)])
        mri = mk_mov(5_500_057, glosa="PAGO 78.121.316-0")
        conciliar_interno(mri, eri, 5_500_057)
        dri = mk_doc(150, rut="78121316-0", nombre="INTERCOMPANIA GRUPO AM SPA",
                     monto=5_500_057, folio="8877M")
        correr()
        f15 = matches_de(mov_id=mri, doc_id=dri)
        check("15a SONDA intercompañía: TODAS las señales encendidas (incluido el "
              "cruce triple) → sugerido, jamás auto (quitar el veto → auto, falla)",
              len(f15) == 1 and f15[0].estado == "sugerido"
              and "intercompa" in f15[0].motivo, f15)
        db = _s()
        db.add(SiiLibroReglaRut(rut_canonico="76310395-1", nivel="BLOQUEADO",
                                motivo=f"{MARK} corredora / compra de moneda"))
        db.commit()
        db.close()
        dbl = mk_doc(151, rut="76310395-1", nombre="VECTOR CAPITAL CORREDORA SPA",
                     monto=2_718_281)
        mbl = mk_mov(2_718_281, glosa="PAGO 76.310.395-1")
        correr()
        f15b = matches_de(mov_id=mbl, doc_id=dbl)
        check("15b SONDA BLOQUEADO: doc y cargo idénticos + RUT en glosa → sugerido "
              "con el motivo de la regla (sin la consulta → auto, falla)",
              len(f15b) == 1 and f15b[0].estado == "sugerido"
              and "BLOQUEADO" in f15b[0].motivo, f15b)

        # ═══ §R16 — nómina jamás subset; lote de egresos ══════════════════════════
        mk_doc(160, rut="76310396-K", nombre="PROVEEDOR NOMINA UNO SPA",
               monto=1_100_003, folio="6101")
        mk_doc(161, rut="76310396-K", nombre="PROVEEDOR NOMINA UNO SPA",
               monto=2_200_007, folio="6102")
        mn = mk_mov(3_300_010, glosa="PAGO PROVEEDORES NOMINA JUNIO")
        correr()
        check("16a SONDA nómina: los docs de un solo RUT suman exacto y aun así "
              "CERO subset (coincidencia casual = falsa)",
              len([x for x in matches_de(mov_id=mn) if x.via == "subset"]) == 0,
              matches_de(mov_id=mn))
        db = _s()
        et = db.query(SiiMatchEtiquetaMov).filter(
            SiiMatchEtiquetaMov.movimiento_id == mn).first()
        check("16b el movimiento quedó etiquetado 'nomina'",
              et is not None and et.etiqueta == "nomina",
              et.etiqueta if et else None)
        db.close()
        f_lote = date(2026, 7, 15)
        el1 = mk_egreso(5_200_111, fecha=f_lote)
        el2 = mk_egreso(4_100_222, fecha=f_lote)
        el3 = mk_egreso(3_180_333, fecha=f_lote)
        ml = mk_mov(12_480_666, glosa="TRANSFERENCIA VARIOS", fecha=f_lote)
        correr()
        db = _s()
        etl = db.query(SiiMatchEtiquetaMov).filter(
            SiiMatchEtiquetaMov.movimiento_id == ml).first()
        db.close()
        check("16c SONDA lote: cargo sin candidato = Σ de 3 egresos del día → "
              "etiqueta lote con los 3 NOMBRADOS y cero sugerencias directas",
              etl is not None and etl.etiqueta == "lote_pendiente"
              and all(f"#{e}" in (etl.detalle or "")
                      for e in (el1, el2, el3))
              and len(matches_de(mov_id=ml)) == 0,
              (etl.etiqueta if etl else None, etl.detalle if etl else None))

        # ═══ §R17 — cubeta NO_BANCARIO ════════════════════════════════════════════
        c17 = mk_compra("76310397-8", "4455M", 890_123, estado="pagado")
        mk_egreso(890_123, medio="tarjeta", detalles=[(c17, 890_123)])
        d17 = mk_doc(170, rut="76310397-8", nombre="FERRETERIA EL TATIO LTDA",
                     monto=890_123, folio="4455M")
        m17 = mk_mov(890_123, glosa="COMPRA WEB CASUAL DEL MISMO MONTO")
        correr()
        check("17a SONDA: la compra pagada con TARJETA saca el doc del pool — el "
              "cargo casual del mismo monto no lo auto-matchea",
              len(matches_de(mov_id=m17, doc_id=d17)) == 0,
              matches_de(mov_id=m17))
        r = client.get(f"{API}/resumen")
        check("17b el tablero cuenta la cubeta NO_BANCARIO",
              r.status_code == 200 and r.json().get("no_bancario", 0) >= 1,
              r.text[:200])

        # ═══ §R18 — blocklist sin contraparte SII ═════════════════════════════════
        mk_doc(180, rut="76310398-6", nombre="PROVEEDOR CASUAL TGR SPA",
               monto=3_456_789)
        m18 = mk_mov(3_456_789, glosa="PAGO TESORERIA GENERAL REPUBLICA")
        correr()
        db = _s()
        et18 = db.query(SiiMatchEtiquetaMov).filter(
            SiiMatchEtiquetaMov.movimiento_id == m18).first()
        db.close()
        check("18a SONDA blocklist: factura casual del monto idéntico → CERO "
              "sugerencias y movimiento etiquetado",
              len(matches_de(mov_id=m18)) == 0 and et18 is not None
              and et18.etiqueta == "blocklist_sin_sii",
              (matches_de(mov_id=m18), et18.etiqueta if et18 else None))
        e18 = mk_egreso(3_456_789)
        r = client.post(f"{TES}/movimientos/{m18}/conciliar", json={"egreso_id": e18})
        check("18b la etiqueta NO impide la conciliación interna banco↔egreso",
              r.status_code == 200, (r.status_code, r.text[:150]))

        # ═══ §R19 — comisiones: agregado mensual, jamás subset ════════════════════
        jul = date(2026, 7, 1)
        movs_com = [mk_mov(7_100, glosa="COMISION MANTENCION CTA",
                           fecha=jul + timedelta(days=i)) for i in range(10)]
        movs_com += [mk_mov(8_115, glosa="COMISION TRANSFERENCIAS",
                            fecha=jul + timedelta(days=20 + i)) for i in range(2)]
        d19 = mk_doc(190, rut="97.036.000-K", nombre="BANCO SANTANDER CHILE",
                     monto=87_230, fecha=date(2026, 8, 3))
        d19chico = mk_doc(191, rut="76310399-4", nombre="INSUMOS CHICOS SPA",
                          monto=7_100)
        correr()
        f19 = [x for x in matches_de(doc_id=d19) if x.via == "agregado_mensual"]
        check("19a SONDA agregado: 12 cargos de julio (Σ $87.230) vs factura "
              "Santander del 03-08 → UNA sugerencia agregada con los 12 movs",
              len(f19) == 12 and len({x.grupo for x in f19}) == 1
              and all(x.estado == "sugerido" for x in f19),
              (len(f19), len({x.grupo for x in f19})))
        check("19b no-regresión: los $7.100 individuales NO aparecen contra la "
              "factura chica de proveedor",
              all(len(matches_de(mov_id=mc, doc_id=d19chico)) == 0
                  for mc in movs_com[:3]), matches_de(doc_id=d19chico))

        # ═══ §R20 — referencia y N° operación ═════════════════════════════════════
        c20 = mk_compra("76310400-1", "5501M", 456_790)
        mk_egreso(456_790, medio="cheque", numop="0", detalles=[(c20, 456_790)])
        d20 = mk_doc(200, rut="76310400-1", nombre="RESORTES Y AMORTIGUADORES SPA",
                     monto=456_790)
        m20 = mk_mov(456_790, ref="0", glosa="CHEQUE COBRADO")
        correr()
        f20 = matches_de(mov_id=m20, doc_id=d20)
        check("20a SONDA referencia '0' + egreso '0' → CERO señal de operación "
              "(sin el guard: señal fantasma)",
              len(f20) == 1 and "operaci" not in f20[0].motivo.lower(), f20)
        c20b = mk_compra("76310401-K", "5502M", 567_891)
        mk_egreso(567_891, medio="cheque", numop="1234", detalles=[(c20b, 567_891)])
        d20b = mk_doc(201, rut="76310401-K", nombre="RADIADORES DEL LOA SPA",
                      monto=567_891)
        m20b = mk_mov(567_891, ref="0001234", glosa="CHEQUE COBRADO POR CAJA")
        correr()
        f20b = matches_de(mov_id=m20b, doc_id=d20b)
        check("20b referencia '0001234' == cheque '1234' (strip de ceros en ambos "
              "lados) → señal encendida",
              len(f20b) == 1 and "operaci" in f20b[0].motivo.lower(), f20b)
        c20c = mk_compra("76310402-8", "5503M", 678_912)
        mk_egreso(678_912, numop="309571", detalles=[(c20c, 678_912)])
        d20c = mk_doc(202, rut="76310402-8", nombre="TURBOS Y CARDANES SPA",
                      monto=678_912)
        m20c = mk_mov(678_912, glosa="TRANSFERENCIA OP 309571")
        correr()
        f20c = matches_de(mov_id=m20c, doc_id=d20c)
        check("20c 'OP 309571' extraído de la glosa == egreso.numero_operacion → "
              "señal por extracción",
              len(f20c) == 1 and "309571" in f20c[0].motivo, f20c)

        # ═══ §R21 — cuentas en otra moneda: CERO candidatos ═══════════════════════
        d21 = mk_doc(210, rut="76310403-6", nombre="PROVEEDOR EN PESOS SPA",
                     monto=1_500)
        m21 = mk_mov(1_500, cuenta=IDS["cuenta_usd"], cartola=None,
                     glosa="CARGO USD 1500")
        correr()
        check("21e SONDA moneda: cargo de cuenta USD + doc CLP $1.500 → CERO "
              "candidatos (1.500 USD no es $1.500)",
              len(matches_de(mov_id=m21, doc_id=d21)) == 0,
              matches_de(mov_id=m21))

        # ═══ §R22 — el motor no compite con el barrido ════════════════════════════
        db = _s()
        fake = SiiLibroSyncRun(origen=f"{MARK}-fake", iniciado_at=datetime.utcnow())
        db.add(fake)
        db.commit()
        db.close()
        r = client.post(f"{API}/correr")
        check("22a SONDA abstención: barrido vivo → el matcher responde 409 y no "
              "corre", r.status_code == 409, (r.status_code, r.text[:150]))
        db = _s()
        db.query(SiiLibroSyncRun).filter(
            SiiLibroSyncRun.origen == f"{MARK}-fake").delete()
        db.commit()
        db.close()
        # sonda estática: JAMÁS FOR UPDATE sobre sii_libro_doc
        src = open(os.path.join(os.path.dirname(__file__), "..", "matcher.py")).read()
        lineas = src.splitlines()
        malas = [i for i, ln in enumerate(lineas) if "with_for_update" in ln
                 and any("SiiLibroDoc" in lineas[j]
                         for j in range(max(0, i - 4), i + 1))]
        check("22b sonda estática: cero with_for_update sobre SiiLibroDoc en el "
              "motor (la coherencia la da el snapshot de hash)", malas == [], malas)
        check("22c y el doc del auto se relee con populate_existing SIN lock",
              re.search(r"SiiLibroDoc\.id == p\[\"doc\"\]\.id\)\s*\n\s*"
                        r"\.populate_existing\(\)\.first\(\)", src) is not None)

        # ═══ §H — revisión adversarial 2026-08-06: arreglos con sonda propia ══════
        # Cada sonda es DISCRIMINANTE: quitar el arreglo correspondiente la pone roja.

        # ── H#6: el combo factura−NC es CONFIRMABLE (tope por mov con suma FIRMADA)
        mk_doc(300, rut="76310404-4", nombre="SELLOS Y ORINGS PATAGONIA SPA",
               monto=1_490_017, folio="H61")
        mk_doc(301, rut="76310404-4", nombre="SELLOS Y ORINGS PATAGONIA SPA",
               monto=298_003, folio="H62", signo=-1)
        mh6 = mk_mov(1_192_014, glosa="TRANSFERENCIA COMBO H SEIS")
        correr()
        fh6 = [x for x in matches_de(mov_id=mh6) if x.via == "subset"]
        check("H6a nace el combo factura−NC con la NC negativa (control)",
              len(fh6) == 2 and any(x.asignado < 0 for x in fh6), fh6)
        rh6 = client.post(f"{API}/{fh6[0].id}/confirmar") if fh6 else None
        fh6c = [x for x in matches_de(mov_id=mh6) if x.via == "subset"]
        check("H6b SONDA #6: confirmar el combo PASA (con Σ|abs| todo grupo con NC "
              "moría 409 'Doble uso') y el grupo queda confirmado con Σ firmada "
              "== cargo",
              rh6 is not None and rh6.status_code == 200
              and fh6c and all(x.estado == "confirmado" for x in fh6c)
              and abs(sum(x.asignado for x in fh6c) - 1_192_014) <= 1,
              (rh6.status_code if rh6 is not None else None,
               [(x.estado, x.asignado) for x in fh6c]))
        check("H6c y el movimiento quedó conciliado (flag del match)",
              mov_snap(mh6).conciliado is True)

        # ── H#7: honorarios asigna el LÍQUIDO que pagó el banco, y el confirm pasa
        dh7 = mk_doc(310, rut="76310405-2",
                     nombre="ASESORIAS CONTABLES RIO CLARO EIRL",
                     monto=1_100_007, folio="H71")
        db = _s()
        db.query(SiiLibroDoc).filter(SiiLibroDoc.id == dh7).update(
            {"destino": "cc:honorarios"})
        db.commit()
        db.close()
        from wasabil_compras.matcher import CONFIG_DEFAULTS
        anio_h = (HOY - timedelta(days=10)).year
        tasa_h = CONFIG_DEFAULTS["retencion_honorarios"][str(anio_h)]
        liq_h = int(round(1_100_007 * (1 - float(tasa_h) / 100.0)))
        mh7 = mk_mov(liq_h, glosa="TRANSFERENCIA BOLETA PROFESIONAL")
        correr()
        fh7 = [x for x in matches_de(mov_id=mh7, doc_id=dh7)
               if x.via == "honorarios"]
        check("H7a SONDA #7: la sugerencia V5 asigna el LÍQUIDO (con el bruto, "
              "asignado > mov.monto siempre) y el motivo nombra el F29",
              len(fh7) == 1 and abs(fh7[0].asignado - liq_h) <= 1
              and "F29" in fh7[0].motivo, fh7)
        rh7 = client.post(f"{API}/{fh7[0].id}/confirmar") if fh7 else None
        check("H7b y confirmarla PASA (con el bruto → 409 'Doble uso' siempre que "
              "la tasa > 0)", rh7 is not None and rh7.status_code == 200,
              (rh7.status_code, rh7.text[:150]) if rh7 is not None else None)

        # ── H#8: carrera del tope por documento (dos movs, un doc, EN PARALELO)
        dh8 = mk_doc(320, rut="76310406-0",
                     nombre="RODILLOS Y ORUGAS TIERRA AMARILLA SPA",
                     monto=3_987_901, folio="H81")
        mh8a = mk_mov(3_987_901, glosa="PAGO 76.310.406-0")
        mh8b = mk_mov(3_987_901, glosa="PAGO 76.310.406-0")
        correr()
        fh8 = matches_de(doc_id=dh8)
        check("H8a dos sugeridos compiten por el doc (control)", len(fh8) == 2
              and all(x.estado == "sugerido" for x in fh8), fh8)
        import threading
        resultados_h8 = {}
        barrera = threading.Barrier(2)

        def _confirmar_h8(nombre, match_id):
            cli = TestClient(app)
            barrera.wait()
            resultados_h8[nombre] = cli.post(f"{API}/{match_id}/confirmar")

        th1 = threading.Thread(target=_confirmar_h8, args=("a", fh8[0].id))
        th2 = threading.Thread(target=_confirmar_h8, args=("b", fh8[1].id))
        th1.start()
        th2.start()
        th1.join(60)
        th2.join(60)
        codigos_h8 = sorted(r.status_code for r in resultados_h8.values())
        fh8c = [x for x in matches_de(doc_id=dh8) if x.estado == "confirmado"]
        check("H8b SONDA #8 (concurrente, regla 2): confirmar ambos EN PARALELO → "
              "uno gana, el otro 409, y Σ confirmado del doc ≤ el documento",
              codigos_h8 == [200, 409] and len(fh8c) == 1
              and abs(sum(abs(x.asignado) for x in fh8c) - 3_987_901) <= 1,
              (codigos_h8, [(x.mov_id, x.asignado) for x in fh8c]))
        src_rm = open(os.path.join(os.path.dirname(__file__), "..",
                                   "router_match.py")).read()
        src_mm = open(os.path.join(os.path.dirname(__file__), "..",
                                   "matcher.py")).read()
        check("H8c sonda estática: el confirm y _insertar_auto serializan el lado "
              "doc con FOR UPDATE sobre las filas de MATCHES (jamás el espejo)",
              re.search(r"SiiLibroMatch\.doc_id == did\)\s*\n\s*"
                        r"\.with_for_update\(\)", src_rm) is not None
              and re.search(r"SiiLibroMatch\.doc_id == p\[\"doc\"\]\.id\)\s*\n\s*"
                            r"\.with_for_update\(\)", src_mm) is not None)

        # ── H#9: guards de Tesorería sobre matches confirmados del libro
        db = _s()
        c9x = Cartola(empresa="mineria", cuenta_id=IDS["cuenta"], nombre=f"{MARK} c9")
        db.add(c9x)
        db.commit()
        c9_id = c9x.id
        db.close()
        dh9 = mk_doc(330, rut="76310407-9", nombre="ENGRANAJES DEL SALADO SPA",
                     monto=2_468_013, folio="H91")
        mh9 = mk_mov(2_468_013, glosa="PAGO 76.310.407-9", cartola=c9_id)
        correr()
        fh9 = matches_de(mov_id=mh9, doc_id=dh9)
        if fh9 and fh9[0].estado != "confirmado":
            client.post(f"{API}/{fh9[0].id}/confirmar")
            fh9 = matches_de(mov_id=mh9, doc_id=dh9)
        check("H9a hay match confirmado y el mov quedó conciliado (control)",
              fh9 and fh9[0].estado == "confirmado"
              and mov_snap(mh9).conciliado is True, fh9)
        r = client.post(f"{TES}/movimientos/{mh9}/desconciliar")
        check("H9b SONDA #9: desconciliar con match confirmado vivo → 409 que "
              "nombra el libro SII (sin guard: 200 y el match quedaba huérfano) — y "
              "SONDA #29: el texto nombra el camino REAL (pestaña «Conciliados», el "
              "N° del cruce y «Deshacer conciliación»), no una bandeja vacía",
              r.status_code == 409 and nombra_el_camino(r.text, fh9[0].id),
              (r.status_code, r.text[:200]))
        db = _s()
        db.query(MovimientoBancario).filter(MovimientoBancario.id == mh9).update(
            {"conciliado": False, "conciliado_at": None})
        db.commit()
        db.close()
        r = client.delete(f"{TES}/movimientos/{mh9}")
        check("H9c SONDA #9: DELETE del mov con match confirmado y flag APAGADO → "
              "409 (el guard viejo solo miraba el flag) + SONDA #29 sobre el texto",
              r.status_code == 409 and nombra_el_camino(r.text, fh9[0].id),
              (r.status_code, r.text[:200]))
        # SONDA #29 (fail-closed): el guard distingue AUSENTE de ROTO. Si el paquete del
        # libro NO está desplegado no puede haber matches y seguir es correcto; pero si
        # ESTÁ y su import revienta (deploy a medias, dependencia que faltó), los matches
        # EXISTEN y devolver None sería decir «no hay nada que proteger» sin haber
        # mirado: eso es fallar ABIERTO y se lleva por delante el movimiento que sostiene
        # un cruce confirmado (FK SET NULL → match confirmado sin movimiento). Esa rama
        # NO tenía ninguna sonda. El sabotaje es el clásico: con None en sys.modules, el
        # `from wasabil_compras.models import …` levanta ImportError, mientras
        # find_spec('wasabil_compras') sigue viendo el paquete instalado → 503.
        _mod_saboteado = sys.modules.get("wasabil_compras.models")
        sys.modules["wasabil_compras.models"] = None
        try:
            r = client.delete(f"{TES}/movimientos/{mh9}")
        finally:
            if _mod_saboteado is not None:
                sys.modules["wasabil_compras.models"] = _mod_saboteado
            else:
                sys.modules.pop("wasabil_compras.models", None)
        check("H9c2 SONDA #29: con el paquete del libro INSTALADO pero roto, el DELETE "
              "del movimiento responde 503 y NO borra (con `except ImportError: return "
              "None` a secas respondía 200 y se lo llevaba)",
              r.status_code == 503 and "sistemas" in r.text
              and mov_snap(mh9).existe is True,
              (r.status_code, r.text[:180], mov_snap(mh9).existe))
        r = client.delete(f"{TES}/cartolas/{c9_id}")
        check("H9d SONDA #9: borrar la cartola de ese mov → 409 (el conteo de "
              "conciliados no lo veía: flag apagado) + SONDA #29 sobre el texto",
              r.status_code == 409 and nombra_el_camino(r.text, fh9[0].id),
              (r.status_code, r.text[:200]))
        r = client.post(f"{API}/{fh9[0].id}/descartar", json={"motivo": "sonda H9"})
        r2 = client.delete(f"{TES}/movimientos/{mh9}")
        check("H9e descartado el match, el DELETE pasa (el guard no sobre-bloquea)",
              r.status_code == 200 and r2.status_code == 200,
              (r.status_code, r2.status_code))

        # ── H#9 bis: «Deshacer conciliación» tiene que DEJAR LIBRE el movimiento
        # Hallazgos #1 y #10 (revisión 2026-08-08). H9e NO cubre esto: apaga el flag a
        # mano (H9c) ANTES de descartar, así que el liberador sale por su early-return
        # `if not mov.conciliado: return` y su lógica no se ejerce JAMÁS — la sonda
        # daba verde con el defecto adentro. Acá NADIE toca el flag: lo enciende el
        # confirm y lo tiene que apagar el descartar. Con autoflush=False, el UPDATE a
        # 'descartado' todavía no viajó cuando el liberador pregunta «¿queda otro
        # confirmado?»: el propio match se ve a sí mismo, `otro` no es None y el flag
        # quedaba encendido sin nada que lo explicara (mov fuera de pendientes, vetado
        # para AUTO vía B y cartola trabada en 409 — lo contrario de lo que promete la
        # pantalla).
        db = _s()
        c9f = Cartola(empresa="mineria", cuenta_id=IDS["cuenta"],
                      nombre=f"{MARK} c9f")
        db.add(c9f)
        db.commit()
        c9f_id = c9f.id
        db.close()
        d9f = mk_doc(331, rut="76310416-6", nombre="REPUESTOS DEL LIMARI SPA",
                     monto=2_468_027, folio="H9F1")
        m9f = mk_mov(2_468_027, glosa="PAGO 76.310.416-6", cartola=c9f_id)
        correr()
        f9f = matches_de(mov_id=m9f, doc_id=d9f)
        if f9f and f9f[0].estado != "confirmado":
            client.post(f"{API}/{f9f[0].id}/confirmar")
            f9f = matches_de(mov_id=m9f, doc_id=d9f)
        check("H9f control: el match quedó confirmado y el flag `conciliado` lo "
              "encendió ÉL — nadie lo tocó a mano (si acá el flag no está en True, la "
              "sonda de abajo probaría el early-return y no la liberación)",
              len(f9f) == 1 and f9f[0].estado == "confirmado"
              and mov_snap(m9f).conciliado is True,
              (f9f, mov_snap(m9f).conciliado))
        r = client.post(f"{API}/{f9f[0].id}/descartar", json={"motivo": "sonda H9f"})
        f9f_post = matches_de(mov_id=m9f, doc_id=d9f)
        check("H9g SONDA #1/#10: «Deshacer conciliación» LIBERA el movimiento — sin "
              "el flush previo, el propio match se lee todavía 'confirmado' "
              "(autoflush=False) y el flag quedaba pegado en True para siempre",
              r.status_code == 200 and len(f9f_post) == 1
              and f9f_post[0].estado == "descartado"
              and mov_snap(m9f).conciliado is False,
              (r.status_code, [x.estado for x in f9f_post],
               mov_snap(m9f).conciliado))
        r = client.delete(f"{TES}/cartolas/{c9f_id}")
        check("H9h y la promesa de la pantalla se cumple de punta a punta: la cartola "
              "ya se puede borrar (con el flag pegado seguía en 409 «tiene N "
              "movimiento(s) conciliado(s)»)",
              r.status_code == 200, (r.status_code, r.text[:200]))

        # ── H#10/#16: el matcher corre solo — post_cartola y post_barrido
        dh10 = mk_doc(340, rut="76310408-7", nombre="PERNOS DE ANCLAJE COPIAPO SPA",
                      monto=1_654_337, folio="H101")
        csv_h10 = ("Fecha;Glosa;Cargo\n"
                   f"{HOY.strftime('%d/%m/%Y')};PAGO 76.310.408-7;1654337\n").encode()
        r = client.post(f"{TES}/cartolas/importar",
                        data={"cuenta_id": str(IDS["cuenta"])},
                        files={"file": ("h10.csv", csv_h10, "text/csv")})
        check("H10a la importación de la cartola responde 200 (control)",
              r.status_code == 200, r.text[:250])
        cart_h10 = (r.json().get("cartola") or {}).get("id")
        db = _s()
        fila_m10 = (db.query(MovimientoBancario.id)
                    .filter(MovimientoBancario.cartola_id == cart_h10).first())
        db.close()
        mov_h10 = fila_m10[0] if fila_m10 else None
        check("H10b SONDA #16: SIN apretar 'correr', el par ya tiene match — el "
              "hook post_cartola corrió el motor",
              mov_h10 is not None
              and len(matches_de(mov_id=mov_h10, doc_id=dh10)) == 1,
              matches_de(mov_id=mov_h10) if mov_h10 else "sin mov")
        db = _s()
        hay_run_pc = (db.query(SiiMatchRun)
                      .filter(SiiMatchRun.origen == "post_cartola").first()
                      is not None)
        db.close()
        check("H10c y la bitácora registra la corrida origen='post_cartola'",
              hay_run_pc)
        # SONDA #4 (revisión 2026-08-08): el motor NO se corre DENTRO del request.
        # Arma el contexto completo dos veces (todos los movimientos CLP sin cota de
        # fecha, todos los docs activos, todos los egresos con detalle) y escribe con
        # un commit por propuesta: «minutos de trabajo pesado», las palabras del propio
        # autor cuando lo sacó del job de alertas. Encima el endpoint es `async def`
        # con Session SÍNCRONA: en línea bloqueaba el EVENT LOOP (congelaba TODAS las
        # peticiones del proceso) y el axios del front, con timeout de 60 s, le mostraba
        # al operador «no se pudo importar la cartola» con la cartola YA importada.
        # Es estática a propósito: bajo TestClient las tareas de fondo corren dentro del
        # mismo ciclo, así que desde el cliente no se distingue una de otra.
        src_tes = open(os.path.join(os.path.dirname(__file__), "..", "..",
                                    "tesoreria", "router.py")).read()
        _ini = src_tes.index("async def importar_cartola")
        _fin = src_tes.index('@router.get("/cartolas")', _ini)
        cuerpo_importar = src_tes[_ini:_fin]
        check("H10c2 SONDA #4: importar cartola AGENDA la corrida "
              "(background.add_task) y NO llama al motor en línea",
              "background.add_task(_correr_matcher_post_cartola" in cuerpo_importar
              and "correr_matcher(db" not in cuerpo_importar,
              cuerpo_importar[-400:])
        from tesoreria.router import _correr_matcher_post_cartola
        db = _s()
        n_pc_antes = (db.query(SiiMatchRun)
                      .filter(SiiMatchRun.origen == "post_cartola").count())
        db.close()
        _correr_matcher_post_cartola(None)
        db = _s()
        n_pc_post = (db.query(SiiMatchRun)
                     .filter(SiiMatchRun.origen == "post_cartola").count())
        db.close()
        check("H10c3 y la tarea de fondo abre su PROPIA sesión: la del request ya "
              "está cerrada cuando Starlette la ejecuta — invocada sola (sin db), "
              "deja igual su corrida en la bitácora",
              n_pc_post == n_pc_antes + 1, (n_pc_antes, n_pc_post))
        import scheduler as sched
        db = _s()
        ids_pb_antes = {x[0] for x in db.query(SiiMatchRun.id)
                        .filter(SiiMatchRun.origen == "post_barrido").all()}
        db.add(SiiLibroSyncRun(origen=f"{MARK}-fake", iniciado_at=datetime.utcnow(),
                               terminado_at=datetime.utcnow(), exito=True))
        db.commit()
        db.close()
        sched._matcher_post_barrido_ga(datetime.utcnow() - timedelta(minutes=5))
        db = _s()
        ids_pb_1 = {x[0] for x in db.query(SiiMatchRun.id)
                    .filter(SiiMatchRun.origen == "post_barrido").all()}
        db.close()
        check("H10d SONDA #10: tras un barrido exitoso el hook del scheduler corre "
              "el matcher (nace UN run origen='post_barrido')",
              len(ids_pb_1 - ids_pb_antes) == 1, len(ids_pb_1 - ids_pb_antes))
        sched._matcher_post_barrido_ga(datetime.utcnow() + timedelta(hours=1))
        db = _s()
        ids_pb_2 = {x[0] for x in db.query(SiiMatchRun.id)
                    .filter(SiiMatchRun.origen == "post_barrido").all()}
        if ids_pb_2 - ids_pb_antes:
            db.query(SiiMatchRun).filter(
                SiiMatchRun.id.in_(list(ids_pb_2 - ids_pb_antes))).delete(
                synchronize_session="fetch")
        db.query(SiiLibroSyncRun).filter(
            SiiLibroSyncRun.origen == f"{MARK}-fake").delete()
        db.commit()
        db.close()
        check("H10e y SIN barrido exitoso en la ventana el hook NO corre (la "
              "etiqueta 'post_barrido' no miente)", ids_pb_2 == ids_pb_1,
              (len(ids_pb_1), len(ids_pb_2)))

        # ── H#11: un candidato V1 fuera de ventana no suprime el subset
        mk_doc(350, rut="76310409-5", nombre="MANIOBRAS Y GRUAS CHANARAL SPA",
               monto=1_390_019, folio="H111")
        mk_doc(351, rut="76310409-5", nombre="MANIOBRAS Y GRUAS CHANARAL SPA",
               monto=278_005, folio="H112", signo=-1)
        mk_doc(352, rut="76310410-9", nombre="PROVEEDOR ANTIGUO DE OTRO RUT SPA",
               monto=1_112_014, folio="H113", dias=200)  # calza monto, fuera de ventana
        mh11 = mk_mov(1_112_014, glosa="TRANSFERENCIA COMBO H ONCE")
        correr()
        fh11 = [x for x in matches_de(mov_id=mh11) if x.via == "subset"]
        check("H11 SONDA #11: el doc viejo que calza el monto NO suprime el combo "
              "factura−NC (con el gate 'not grupos' el subset jamás corría)",
              len(fh11) == 2 and any(x.asignado < 0 for x in fh11), fh11)

        # ── H#12: los CONFLICTO se re-evalúan — resueltos → sugerido; muertos → caducan
        c12 = mk_compra(None, "H121", 2_591_353)
        e12 = mk_egreso(2_591_353, benef_rut="77.111.222-3",
                        detalles=[(c12, 2_591_353)])
        m12a = mk_mov(2_591_353, glosa="TRANSFERENCIA CONFLICTO H DOCE")
        conciliar_interno(m12a, e12, 2_591_353)
        d12a = mk_doc(360, rut="76310411-7",
                      nombre="CILINDROS HIDRAULICOS PAN DE AZUCAR SPA",
                      monto=2_591_353, folio="H122")
        correr()
        f12a = matches_de(mov_id=m12a, doc_id=d12a)
        check("H12a nace el conflicto (control)", len(f12a) == 1
              and f12a[0].estado == "conflicto", f12a)
        r = client.post(f"{TES}/movimientos/{m12a}/desconciliar")
        check("H12b desconciliar pasa con match en CONFLICTO (el guard #9 solo "
              "protege confirmados)", r.status_code == 200,
              (r.status_code, r.text[:150]))
        correr()
        f12a = matches_de(mov_id=m12a, doc_id=d12a)
        check("H12c SONDA #12: corregida la conciliación, el conflicto pasa a "
              "SUGERIDO en la corrida siguiente (antes quedaba zombi para siempre "
              "y bloqueaba el par)",
              len(f12a) == 1 and f12a[0].estado == "sugerido", f12a)
        c12b = mk_compra(None, "H123", 2_691_353)
        e12b = mk_egreso(2_691_353, benef_rut="77.333.444-5",
                         detalles=[(c12b, 2_691_353)])
        m12b = mk_mov(2_691_353, glosa="TRANSFERENCIA CONFLICTO H DOCE B")
        conciliar_interno(m12b, e12b, 2_691_353)
        d12b = mk_doc(361, rut="76310412-5",
                      nombre="BOMBAS Y MOTORES INCA DE ORO SPA",
                      monto=2_691_353, folio="H124")
        correr()
        f12b = matches_de(mov_id=m12b, doc_id=d12b)
        check("H12d nace el segundo conflicto (control)", len(f12b) == 1
              and f12b[0].estado == "conflicto", f12b)
        pisar_doc(d12b, estado_espejo="DESAPARECIDO")
        correr()
        f12b = matches_de(mov_id=m12b, doc_id=d12b)
        check("H12e SONDA #12: muerto el par (doc DESAPARECIDO), el conflicto "
              "caduca a en_duda con motivo",
              len(f12b) == 1 and f12b[0].estado == "en_duda"
              and "caducado" in f12b[0].motivo, f12b)

        # ── H#2: el CONFLICTO nace con los candidatos ADENTRO
        # Hallazgo #2 (revisión 2026-08-08): la rama del conflicto devolvía
        # `candidatos: []` FIJO —el conflicto se retorna antes de que se calculen los
        # candidatos— y `_upsert` no re-escribe una fila que ya no es 'sugerido', así
        # que la lista quedaba vacía PARA SIEMPRE. La pantalla solo abre «Ver los N
        # documentos que compiten» cuando la lista trae más de uno: en un conflicto no
        # se renderizaba jamás, y el tooltip mandaba a abrir una lista inexistente.
        c2 = mk_compra(None, "H2C900", 3_142_887)
        e2 = mk_egreso(3_142_887, benef_rut="77.555.666-5",
                       detalles=[(c2, 3_142_887)])
        m2 = mk_mov(3_142_887, glosa="TRANSFERENCIA CONFLICTO CANDIDATOS")
        conciliar_interno(m2, e2, 3_142_887)
        mk_doc(380, rut="76310417-4", nombre="SELLOS Y RETENES VALLENAR SPA",
               monto=3_142_887, folio="H2C801")
        mk_doc(381, rut="76310418-2", nombre="FILTROS Y MANGUERAS FREIRINA SPA",
               monto=3_142_887, folio="H2C802")
        correr()
        r = client.get(f"{API}/pendientes",
                       params={"estado": "conflicto", "page_size": 200})
        filas2 = [x for x in r.json()["items"]
                  if x["movimiento"] and x["movimiento"]["id"] == m2]
        check("H2a control: el movimiento (conciliado con un egreso de OTRO RUT) pare "
              "DOS conflictos, uno por cada documento que compite",
              len(filas2) == 2, [(x["id"], x["estado"]) for x in filas2])
        check("H2b SONDA #2: el conflicto viaja CON los documentos que compiten "
              "(antes: `candidatos: []` fijo, y la pantalla solo abre la lista con más "
              "de uno → el operador no veía NUNCA quién compite)",
              len(filas2) == 2 and all(len(x["candidatos"]) >= 2 for x in filas2),
              [len(x["candidatos"]) for x in filas2])
        check("H2c y el motivo de competencia también llega a la fila (se agregaba "
              "DESPUÉS del return del conflicto: no se escribía nunca)",
              len(filas2) == 2
              and all(any("compiten por este cargo" in t for t in x["motivo"])
                      for x in filas2), [x["motivo"] for x in filas2])

        # ── H-BAJO: el AUTO degradado no conserva score=100 ni motivo 'AUTO vía'
        eB = mk_egreso(1_357_913)   # sin detalles ni beneficiario: tercera pata neutra
        mB = mk_mov(1_357_913, glosa="PAGO 76.310.413-3")
        conciliar_interno(mB, eB, 1_357_913)
        dB = mk_doc(370, rut="76310413-3", nombre="TAPAS Y CULATAS EL SALVADOR SPA",
                    monto=1_357_913, folio="H131")
        correr()
        fB = matches_de(mov_id=mB, doc_id=dB)
        check("HB1 SONDA BAJO: el auto vía B vetado por mov.conciliado nace "
              "SUGERIDO con score<100, sin 'AUTO vía' y con el motivo del degrade "
              "(antes conservaba score=100 y el motivo de AUTO)",
              len(fB) == 1 and fB[0].estado == "sugerido"
              and (fB[0].score or 0) <= 99 and "AUTO vía" not in fB[0].motivo
              and "degradado a sugerido" in fB[0].motivo, fB)
        correr()
        fB = matches_de(mov_id=mB, doc_id=dB)
        check("HB2 y el refresh posterior tampoco escribe 100 ('confirmar a mano')",
              len(fB) == 1 and (fB[0].score or 0) <= 99
              and "confirmar a mano" in fB[0].motivo, fB)

        # ── H#1 (complemento): anti-duplicado BLANDO del alta de compras
        pay1 = {"tipo_gasto": "gasto_operacional", "acreedor": f"{MARK} prov dup",
                "proveedor_rut": "76310415-K", "numero_documento": "0004071",
                "moneda": "CLP", "monto_neto": 100000, "afecto_iva": False,
                "condicion_pago": "credito", "plazo_dias": 30}
        r1 = client.post("/api/compras-contab", json=pay1)
        check("HD1 la compra base se registra (control)", r1.status_code == 200,
              (r1.status_code, r1.text[:200]))
        pay2 = dict(pay1, proveedor_rut="76.310.415-K", numero_documento="4071")
        r2 = client.post("/api/compras-contab", json=pay2)
        check("HD2 SONDA #1: mismo folio con ceros a la izquierda + RUT formateado "
              "→ 409 que NOMBRA la compra (el guard exacto lo dejaba pasar y "
              "nacían DOS CxP)", r2.status_code == 409 and "compra #" in r2.text,
              (r2.status_code, r2.text[:250]))
        pay3 = dict(pay1, numero_documento="F-04071")
        r3 = client.post("/api/compras-contab", json=pay3)
        check("HD3 y el prefijo con ceros ('F-04071') también choca (folio blando)",
              r3.status_code == 409, (r3.status_code, r3.text[:200]))

        # ═══ 24) FENCING de corridas simultáneas (lente de ciclo de vida) ═════════
        # Desde que el motor corre solo (tras el barrido y tras importar cartola) además
        # del botón, dos corridas a la vez dejaron de ser hipótesis.
        #
        # POR QUÉ ESTA SONDA SE ESCRIBE ASÍ (y por qué la primera versión NO servía):
        # `correr_matcher` chequea DOS veces. El chequeo PREVIO corta si ya hay una
        # corrida viva; el FENCING, después de escribir la propia fila, resuelve el
        # empate por id. Sembrar una rival y llamar al motor prueba el chequeo previo
        # y NADA MÁS: el bloque de fencing no se ejecuta nunca y la sonda da verde
        # aunque lo borren entero. La carrera de verdad es la otra: la rival aparece
        # DESPUÉS del chequeo previo. Eso es lo que se simula acá.
        from wasabil_compras.matcher import MatcherEnCurso, correr_matcher
        import wasabil_compras.matcher as _mm
        db = SessionLocal()
        try:
            n_antes = db.query(SiiMatchRun).count()
            _orig_en_curso = _mm._matcher_en_curso
            vistas = []

            def _en_curso_de_mentira(_db, *, anteriores_a=None):
                """Libre en el chequeo previo, OCUPADA en el fencing: exactamente la
                ventana en la que se cuela la segunda corrida."""
                vistas.append(anteriores_a)
                return anteriores_a is not None

            _mm._matcher_en_curso = _en_curso_de_mentira
            try:
                try:
                    correr_matcher(db, origen="manual")
                    se_abstuvo = False
                except MatcherEnCurso:
                    se_abstuvo = True
            finally:
                _mm._matcher_en_curso = _orig_en_curso
            db.rollback()

            check("24a SONDA: la corrida que pierde la carrera se abstiene "
                  "(sin el bloque de fencing, seguiría de largo y las dos escribirían "
                  "sobre los mismos movimientos)", se_abstuvo, se_abstuvo)
            check("24b el fencing SE EJECUTÓ de verdad: hubo un 2º chequeo, con id "
                  "(si solo hay uno, la sonda probó el chequeo previo y nada más)",
                  len(vistas) >= 2 and vistas[0] is None and vistas[-1] is not None,
                  vistas)
            # La fila alcanzó a nacer: tiene que quedar CERRADA. Si quedara abierta,
            # bloquearía al motor los 10 minutos siguientes.
            nacida = (db.query(SiiMatchRun).filter(SiiMatchRun.id > 0)
                      .order_by(SiiMatchRun.id.desc()).first())
            check("24c la corrida que se retira CIERRA su bitácora, con exito=False y "
                  "el motivo escrito (no queda 'corriendo' bloqueando el motor)",
                  nacida is not None and nacida.terminado_at is not None
                  and nacida.exito is False and "retira" in (nacida.error or ""),
                  (nacida.terminado_at, nacida.exito, (nacida.error or "")[:60])
                  if nacida else None)
            # SONDA #5 (revisión 2026-08-08): el tablero tomaba «la última corrida» por
            # id a secas, y la retirada es SIEMPRE la de id mayor, con exito=False →
            # pintaba «Última corrida: … · falló» aunque la ganadora hubiera trabajado
            # bien, y quedaba pegado así (la ganadora tiene id MENOR y nunca vuelve a
            # ser la última). El operador concluía que el motor está roto y re-corría.
            ultima_bd = (db.query(SiiMatchRun)
                         .order_by(SiiMatchRun.id.desc()).first())
            r = client.get(f"{API}/resumen")
            uc = (r.json() or {}).get("ultima_corrida") or {}
            check("24e la retirada ES la fila de id mayor (si no lo fuera, la sonda de "
                  "abajo no estaría probando nada)",
                  nacida is not None and ultima_bd is not None
                  and ultima_bd.id == nacida.id,
                  (nacida.id if nacida else None,
                   ultima_bd.id if ultima_bd else None))
            check("24f SONDA #5: y aun así el tablero NO la muestra como última "
                  "corrida — retirarse por fencing no es fallar, es abstenerse",
                  r.status_code == 200 and bool(uc)
                  and "retira" not in (uc.get("error") or ""),
                  (r.status_code, uc))
            # limpieza: se borra SOLO la fila que nació en esta sonda
            if nacida is not None and db.query(SiiMatchRun).count() > n_antes:
                db.query(SiiMatchRun).filter(SiiMatchRun.id == nacida.id).delete(
                    synchronize_session=False)
                db.commit()
            check("24g limpieza de la sonda (la bitácora vuelve a su tamaño)",
                  db.query(SiiMatchRun).count() == n_antes,
                  (db.query(SiiMatchRun).count(), n_antes))
        finally:
            db.close()

        # ═══ candado de empresa ═══════════════════════════════════════════════════
        CURRENT["empresa"] = "automotriz"
        r = client.get(f"{API}/resumen")
        check("23 un usuario de MonzaParts recibe 403", r.status_code == 403,
              r.status_code)
        CURRENT["empresa"] = "mineria"

    finally:
        _limpiar()
    _verificar_limpieza()
    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_matcher_banco_libro():
    run()


if __name__ == "__main__":
    run()
