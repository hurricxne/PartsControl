"""Fase 9b · CANTIDADES PARCIALES en el pipeline de MonzaParts.

EL PROBLEMA que esta fase cura (el dueño confirmó que le pasa seguido): el proveedor
manda 6 de 10 filtros. Hasta ahora el pipeline movía la LÍNEA COMPLETA entre estados,
así que había que embarcar los 10 — y cuando Bodega recibía 6, el cierre de recepción
creaba un RECLAMO al proveedor por 4 que el proveedor simplemente no había despachado
todavía. Reclamo FANTASMA, y el remanente sin forma de esperar el próximo AWB.

Esta suite certifica las dos cosas que importan, en ese orden:

  1. EL INVARIANTE DE PLATA (secciones 1, 2 y 11). Partir una línea toca la FOTO DE
     PRECIOS CONGELADA de la venta, y los totales de la cabecera
     (cot.total_neto / iva_monto / total_bruto) NO se recalculan NUNCA más. Si el
     split no conserva Σ cantidad y Σ subtotal_clp, el sistema DUPLICA PLATA: el
     precio unitario que MonzaVentasContabPage deriva como subtotal/cantidad se
     infla, y ese número inflado sube al total_venta_clp que publica Contabilidad.
     La sección 11 demuestra que este check DETECTA la regresión (sonda con el vicio
     de Grupo AM inyectado: el clon hereda el subtotal completo → el mismo predicado
     que da verde con el código real da ROJO con el vicio).

  2. LA CURA DEL RECLAMO FANTASMA, e2e (sección 4). Preparar/embarcar 6 de 10 →
     Bodega recibe 6 sobre una línea de 6 = 'completo' sin faltante → cierre SIN
     reclamo, y el remanente esperando su AWB. Se prueba también el comportamiento
     VIEJO para contraste (línea completa de 10 → recibir 6 → sí nace el reclamo por
     4), porque un test que solo mira el caso nuevo no prueba que se curó algo.

Además: herencia de la foto y de la OC (2, 3), aislamiento del clon respecto del
embarque y del panel de Bodega (5), tope físico (6), los 7 candados duros de 409 (7),
el guard de estado y los 4 VICIOS DE GRUPO AM QUE NO SE COPIARON (8), concurrencia con
hilos REALES (9), y compatibilidad de las dos vías legadas (10).

DETALLES DEL ARNÉS QUE SON EL PUNTO (no simplificar):
· auth REALISTA (lección G13): el override consulta la BD en la MISMA sesión del
  request, como producción. Con el `lambda` seco el FOR UPDATE pasa a ser la primera
  sentencia del request, el read view nace DESPUÉS del lock y las carreras de plata se
  vuelven INVISIBLES.
· las verificaciones leen con CONEXIÓN NUEVA y SQL crudo: la sesión del propio test
  arrastra su read view y puede dar falso rojo Y falso verde.
· issue=False siempre: NADA llama al API real de Wasabil. La única fila de DTE que
  existe acá se inserta a mano en la tabla para ejercer el candado (guard 7.2).

Corre con:  ./venv/bin/python -m pytest monza_tests/test_preparar_parcial_monza.py -q
       o:   ./venv/bin/python monza_tests/test_preparar_parcial_monza.py
"""
import os
import sys
import threading
import uuid
from datetime import date
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import Depends, FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from database import SessionLocal, engine, Base, get_db  # noqa: E402
from auth import get_current_user  # noqa: E402
from monza_models import (  # noqa: E402
    MonzaCliente, MonzaCotizacion, MonzaCotizacionItem, MonzaOcProveedor,
    MonzaEmbarque, MonzaEmbarqueItem, MonzaRecepcion, MonzaRecepcionItem,
    MonzaDespacho, MonzaDespachoItem, MonzaReclamo, MonzaLog, MonzaNotificacion,
)
from monza_contabilidad.models import (  # noqa: E402
    MonzaContFacturaCliente, MonzaContFacturaClienteItem,
)
from monza_recepcion_nacional.models import (  # noqa: E402
    MonzaRecepcionNacional, MonzaRecepcionNacionalItem,
)
from monza_embarques_pricing.models import MonzaEmbPricing, MonzaEmbPricingItem  # noqa: E402
from monza_compras_contab.models import MonzaContCompra, MonzaContCompraItem  # noqa: E402
from monza_wasabil_dte.models import MonzaWasabilDte, STATUS_EMITIDO  # noqa: E402
from monza_wasabil_dte.service import TIPO_DOC_GUIA  # noqa: E402

# Helpers REUTILIZABLES bajo prueba, importados directo (patrón de
# monza_tests/test_recepcion_nacional_tope.py): el contrato de la fase vive en ellos.
import monza_router_abastecimiento as abast  # noqa: E402
from monza_router_abastecimiento import (  # noqa: E402
    router as abastecimiento_router, _clonar_item_remanente, _validar_pedidos_parciales,
    _lockear_items_para_split, _guard_duro_del_split, _partir_linea,
    _rechazar_items_nacionales,
)
from monza_router_despachos import (  # noqa: E402
    router as despachos_router, _rechazar_split_sobre_documento,
)
from monza_router_logistica import router as logistica_router  # noqa: E402
from monza_router_bodega import router as bodega_router  # noqa: E402

Base.metadata.create_all(bind=engine, checkfirst=True)

# monza_cotizaciones.numero es String(20): el MARK vive en el CLIENTE (ancla de
# limpieza, patrón de la casa) y el número usa un prefijo corto + sufijo aleatorio.
MARK = "__TEST_PP9B__"
EMAIL = f"{MARK}@test.invalid"
PRECIO = 12345.0        # elegido para que 6×p + 4×p == 10×p sea EXACTO en float
RONDAS = 5              # la carrera es probabilística: varias rondas evitan falsos verdes

app = FastAPI()
app.include_router(abastecimiento_router)
app.include_router(logistica_router)
app.include_router(bodega_router)
app.include_router(despachos_router)


# Auth REALISTA (lección G13): además de devolver el usuario hace una lectura en la
# MISMA sesión del request, igual que auth.get_current_user en producción — así el read
# view de MySQL nace ANTES de cualquier with_for_update(). Con el lambda seco las
# carreras de plata de la sección 9 son invisibles.
def _cu(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return SimpleNamespace(id=None, email=EMAIL, empresa="automotriz")


app.dependency_overrides[get_current_user] = _cu
client = TestClient(app)

_fails: list = []
# Ids de entidades que el TEST creó (directo o por endpoint) para la verificación de
# residuos: las notificaciones se cuelgan de (entidad, entidad_id) y no llevan MARK.
_VISTOS = {"cotizacion": set(), "embarque": set(), "recepcion": set(), "despacho": set()}


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {str(extra)[:300]}"))
    if not cond:
        _fails.append(name)


# ═══════════════════ Lecturas de VERIFICACIÓN: conexión nueva ═══════════════════
# La sesión del test arrastra su read view bajo REPEATABLE READ y puede dar falso rojo
# Y falso verde. Todo lo que se assertea se lee con conexión nueva y SQL crudo.

def _sql(q, **p):
    with engine.connect() as c:
        return c.execute(text(q), p).fetchall()


def _lineas(cot_id):
    """Filas de la venta con su plata, en orden id ASC, con CONEXIÓN NUEVA."""
    return [
        {"id": r[0], "cantidad": r[1], "subtotal": r[2], "precio": r[3],
         "estado": r[4], "parte": r[5], "ocp": r[6]}
        for r in _sql(
            "SELECT id,cantidad,subtotal_clp,precio_unitario_clp,estado_linea,"
            "numero_parte,oc_proveedor_id FROM monza_cotizacion_items "
            "WHERE cotizacion_id=:c ORDER BY id", c=cot_id)
    ]


def _cabecera(cot_id):
    r = _sql("SELECT total_neto,iva_monto,total_bruto FROM monza_cotizaciones "
             "WHERE id=:i", i=cot_id)[0]
    return (r[0], r[1], r[2])


def _hermanas(cot_id, parte):
    return [l for l in _lineas(cot_id) if l["parte"] == parte]


def _invariante_plata(cot_id, parte, cant_original, sub_original, cabecera_antes):
    """EL PREDICADO MÁS IMPORTANTE DE LA SUITE. Devuelve (ok, detalle).

    Se aísla en una función porque lo usan DOS llamadores: el check real (sección 1) y
    la SONDA de poder discriminante (sección 11), que inyecta el vicio de Grupo AM y
    exige que este MISMO predicado devuelva False. Sin ese segundo uso no se sabría si
    el check es complaciente."""
    hs = _hermanas(cot_id, parte)
    suma_cant = sum(h["cantidad"] or 0 for h in hs)
    suma_sub = sum(float(h["subtotal"] or 0) for h in hs)
    cab = _cabecera(cot_id)
    ok = (
        suma_cant == cant_original
        and suma_sub == float(sub_original)          # exacto, no "casi": es plata
        and cab == cabecera_antes
    )
    return ok, (f"Σcant={suma_cant}/{cant_original} Σsub={suma_sub}/{sub_original} "
                f"cabecera={cab}/{cabecera_antes} filas={hs}")


def _estado(item_id):
    r = _sql("SELECT estado_linea,cantidad FROM monza_cotizacion_items WHERE id=:i",
             i=item_id)
    return (r[0][0], r[0][1]) if r else (None, None)


def _reclamos(item_ids):
    if not item_ids:
        return []
    marcas = ",".join(str(int(i)) for i in item_ids)
    return [(r[0], r[1], r[2]) for r in _sql(
        f"SELECT item_id,motivo,qty_afectada FROM monza_reclamos "
        f"WHERE item_id IN ({marcas}) ORDER BY id")]


# ═══════════════════════════════ Siembra marcada ════════════════════════════════

def _venta(db, cantidades=(10,), estado_linea="comprado", ocp=None, precio=PRECIO,
           subtotal_none=False):
    """Cliente + cotización VENDIDA + N ítems con la FOTO DE PRECIOS completa.

    La cabecera se siembra CUADRADA con Σ subtotales (es lo que el motor de
    cotizaciones deja al cerrar la venta): así el invariante «la cabecera no se toca»
    se puede asertar contra un número real y no contra ceros."""
    suf = uuid.uuid4().hex[:6].upper()
    cli = MonzaCliente(nombre=MARK, rut="11.111.111-1")
    db.add(cli); db.flush()
    neto = 0.0 if subtotal_none else sum(c * precio for c in cantidades)
    cot = MonzaCotizacion(
        numero=f"CT-PP-{suf}", cliente_id=cli.id, estado="vendida",
        total_neto=neto, iva_monto=round(neto * 0.19, 2),
        total_bruto=round(neto * 1.19, 2), iva_pct=19, pct_adelanto=0,
    )
    db.add(cot); db.flush()
    items = []
    for n, cant in enumerate(cantidades, start=1):
        it = MonzaCotizacionItem(
            cotizacion_id=cot.id, descripcion=f"{MARK} Parte {n}",
            numero_parte=f"P-PP-{n}", marca="MONZA", procedencia="Alemania",
            calidad="original", cantidad=cant, estado_linea=estado_linea,
            # Foto de precios COMPLETA: los 6 campos unitarios + precio y subtotal
            costo=99.5, moneda="EUR", peso_kg=2.75, tc_aplicado=1050.25,
            tarifa_aerea=7.5, markup_pct=0.28,
            precio_unitario_clp=precio,
            subtotal_clp=(None if subtotal_none else cant * precio),
            plazo_entrega="15 dias", oc_proveedor_id=(ocp.id if ocp else None),
        )
        db.add(it); items.append(it)
    db.commit()
    for obj in [cot] + items:
        db.refresh(obj)
    _VISTOS["cotizacion"].add(cot.id)
    return cot, items


def _oc(db, tipo_origen="internacional"):
    suf = uuid.uuid4().hex[:6].upper()
    ocp = MonzaOcProveedor(numero=f"{MARK}-OCP-{suf}", proveedor_nombre=f"{MARK} PROV",
                           moneda="EUR", tipo_origen=tipo_origen, estado="emitida")
    db.add(ocp); db.commit(); db.refresh(ocp)
    return ocp


def _embarque_directo(db, items, estado="en_bodega"):
    """Embarque insertado a mano (para los guards; el flujo real usa el endpoint)."""
    suf = uuid.uuid4().hex[:6].upper()
    emb = MonzaEmbarque(numero=f"{MARK}-E-{suf}", estado=estado, notas=MARK)
    db.add(emb); db.flush()
    for it in items:
        db.add(MonzaEmbarqueItem(embarque_id=emb.id, item_id=it.id))
    db.commit(); db.refresh(emb)
    _VISTOS["embarque"].add(emb.id)
    return emb


# ═══════════════════════════ Llamadas al API bajo prueba ════════════════════════

def _preparar_parcial(items):
    return client.post("/api/monza/abastecimiento/items/preparar-parcial",
                       json={"items": items})


def _preparar_legado(item_ids):
    return client.post("/api/monza/abastecimiento/preparar",
                       json={"item_ids": item_ids})


def _crear_embarque(**body):
    """notas=MARK SIEMPRE: el numero lo genera el backend (EMB-YYYY-NNNN), así que
    `notas` es la única ancla de limpieza/residuos de los embarques del endpoint."""
    body.setdefault("notas", MARK)
    r = client.post("/api/monza/logistica/embarques", json=body)
    if r.status_code == 200:
        _VISTOS["embarque"].add(r.json()["id"])
    return r


def _recibir_y_cerrar(emb_id, marcas, forzar=False):
    """Bodega: abrir recepción, marcar cada ítem y cerrar. Devuelve (rid, resp_cierre)."""
    r = client.post(f"/api/monza/bodega/embarques/{emb_id}/recibir")
    assert r.status_code == 200, r.text
    rid = r.json()["recepcion_id"]
    _VISTOS["recepcion"].add(rid)
    for item_id, estado_rec, qty in marcas:
        rr = client.patch(f"/api/monza/bodega/recepciones/{rid}/items/{item_id}",
                          json={"estado_recepcion": estado_rec, "qty_recibida": qty})
        assert rr.status_code == 200, rr.text
    return rid, client.post(f"/api/monza/bodega/recepciones/{rid}/cerrar",
                            json={"forzar": forzar})


def _panel_recepcion(rid):
    r = client.get(f"/api/monza/bodega/recepciones/{rid}")
    assert r.status_code == 200, r.text
    return r.json()


def _avance(cot_id):
    r = client.get(f"/api/monza/despachos/avance/{cot_id}")
    assert r.status_code == 200, r.text
    return {i["id"]: i for i in r.json()["items"]}


def _despachar(cot_id, item_id, qty):
    r = client.post("/api/monza/despachos/crear", json={
        "cotizacion_id": cot_id, "items": [{"item_id": item_id, "qty": qty}]})
    if r.status_code == 200:
        _VISTOS["despacho"].add(r.json().get("id"))
    return r


def _en_paralelo(fn, n=2):
    """Hilos REALES sincronizados por barrera: los dos requests nacen a la vez, así
    los dos read views se abren antes de que cualquiera commitee."""
    barrera = threading.Barrier(n)
    salida = [None] * n

    def _worker(i):
        barrera.wait()
        try:
            salida[i] = fn(i)
        except Exception as e:                       # noqa: BLE001
            salida[i] = ("EXC", repr(e))

    hilos = [threading.Thread(target=_worker, args=(i,)) for i in range(n)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    return salida


# ═══════════════════════ Limpieza TOTAL en orden FK-seguro ══════════════════════

def _limpiar(db):
    db.rollback()
    S = "fetch"
    cot_ids = [r[0] for r in db.query(MonzaCotizacion.id)
               .join(MonzaCliente, MonzaCliente.id == MonzaCotizacion.cliente_id)
               .filter(MonzaCliente.nombre == MARK).all()]
    item_ids = [r[0] for r in db.query(MonzaCotizacionItem.id)
                .filter(MonzaCotizacionItem.cotizacion_id.in_(cot_ids or [0])).all()]
    dsp_ids = [r[0] for r in db.query(MonzaDespacho.id).filter(
        (MonzaDespacho.cotizacion_id.in_(cot_ids or [0]))
        | (MonzaDespacho.cliente_nombre == MARK)).all()]
    # Embarques: los sembrados a mano llevan el MARK en `numero`; los creados por el
    # ENDPOINT llevan numero autogenerado y se anclan por `notas` y por sus ítems.
    emb_ids = {r[0] for r in db.query(MonzaEmbarque.id).filter(
        (MonzaEmbarque.numero.like(f"{MARK}%")) | (MonzaEmbarque.notas == MARK)).all()}
    emb_ids |= {r[0] for r in db.query(MonzaEmbarqueItem.embarque_id)
                .filter(MonzaEmbarqueItem.item_id.in_(item_ids or [0])).all()}
    emb_ids = list(emb_ids)
    rec_ids = [r[0] for r in db.query(MonzaRecepcion.id)
               .filter(MonzaRecepcion.embarque_id.in_(emb_ids or [0])).all()]
    fac_ids = [r[0] for r in db.query(MonzaContFacturaCliente.id).filter(
        (MonzaContFacturaCliente.cotizacion_id.in_(cot_ids or [0]))
        | (MonzaContFacturaCliente.numero_factura.like(f"{MARK}%"))).all()]
    ocp_ids = [r[0] for r in db.query(MonzaOcProveedor.id)
               .filter(MonzaOcProveedor.numero.like(f"{MARK}%")).all()]
    rn_ids = {r[0] for r in db.query(MonzaRecepcionNacionalItem.recepcion_id)
              .filter(MonzaRecepcionNacionalItem.item_cotizacion_id.in_(item_ids or [0])).all()}
    rn_ids |= {r[0] for r in db.query(MonzaRecepcionNacional.id)
               .filter(MonzaRecepcionNacional.numero_guia_proveedor.like(f"{MARK}%")).all()}
    rn_ids = list(rn_ids)
    prc_ids = [r[0] for r in db.query(MonzaEmbPricing.id).filter(
        (MonzaEmbPricing.embarque_id.in_(emb_ids or [0]))
        | (MonzaEmbPricing.observaciones == MARK)).all()]
    cmp_ids = [r[0] for r in db.query(MonzaContCompra.id)
               .filter(MonzaContCompra.numero_documento.like(f"{MARK}%")).all()]
    for k, v in (("cotizacion", cot_ids), ("embarque", emb_ids),
                 ("recepcion", rec_ids), ("despacho", dsp_ids)):
        _VISTOS[k].update(v)

    # 1) Módulos satélite primero: sus FK apuntan a despachos/facturas/embarques.
    #    El DTE va ANTES que despachos y facturas (FK sin ondelete a ambos).
    db.query(MonzaWasabilDte).filter(
        (MonzaWasabilDte.despacho_id.in_(dsp_ids or [0]))
        | (MonzaWasabilDte.factura_id.in_(fac_ids or [0]))
        | (MonzaWasabilDte.payload_json == MARK)).delete(synchronize_session=S)
    db.query(MonzaContFacturaClienteItem).filter(
        MonzaContFacturaClienteItem.factura_id.in_(fac_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaContFacturaCliente).filter(
        MonzaContFacturaCliente.id.in_(fac_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaContCompraItem).filter(
        MonzaContCompraItem.compra_id.in_(cmp_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaContCompra).filter(
        MonzaContCompra.id.in_(cmp_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaEmbPricingItem).filter(
        MonzaEmbPricingItem.pricing_id.in_(prc_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaEmbPricing).filter(
        MonzaEmbPricing.id.in_(prc_ids or [0])).delete(synchronize_session=S)
    if rn_ids:
        db.query(MonzaRecepcionNacionalItem).filter(
            MonzaRecepcionNacionalItem.recepcion_id.in_(rn_ids)).delete(synchronize_session=S)
        db.query(MonzaRecepcionNacional).filter(
            MonzaRecepcionNacional.id.in_(rn_ids)).delete(synchronize_session=S)

    # 2) Notificaciones con scope por entidad (nunca producto cruzado con ids ajenos)
    for entidad, ids in (("cotizacion", cot_ids), ("recepcion", rec_ids),
                         ("despacho", dsp_ids), ("embarque", emb_ids)):
        db.query(MonzaNotificacion).filter(
            MonzaNotificacion.entidad == entidad,
            MonzaNotificacion.entidad_id.in_(ids or [0])).delete(synchronize_session=S)
    db.query(MonzaLog).filter(MonzaLog.user_email == EMAIL).delete(synchronize_session=S)

    # 3) Núcleo Monza, hijos antes que padres
    db.query(MonzaReclamo).filter(
        (MonzaReclamo.item_id.in_(item_ids or [0]))
        | (MonzaReclamo.user_email == EMAIL)).delete(synchronize_session=S)
    db.query(MonzaDespachoItem).filter(
        MonzaDespachoItem.despacho_id.in_(dsp_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaDespacho).filter(
        MonzaDespacho.id.in_(dsp_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaRecepcionItem).filter(
        MonzaRecepcionItem.recepcion_id.in_(rec_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaRecepcion).filter(
        MonzaRecepcion.id.in_(rec_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaEmbarqueItem).filter(
        MonzaEmbarqueItem.embarque_id.in_(emb_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaEmbarque).filter(
        MonzaEmbarque.id.in_(emb_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaCotizacionItem).filter(
        MonzaCotizacionItem.id.in_(item_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaCotizacion).filter(
        MonzaCotizacion.id.in_(cot_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaOcProveedor).filter(
        MonzaOcProveedor.id.in_(ocp_ids or [0])).delete(synchronize_session=S)
    db.query(MonzaCliente).filter(MonzaCliente.nombre == MARK).delete(synchronize_session=S)
    db.commit()


def _residuos():
    """CERO residuos, verificado con CONEXIÓN NUEVA. Cubre también las notificaciones,
    que no llevan MARK: se cuentan contra los ids que el test vio nacer."""
    def _n(q, **p):
        return _sql(q, **p)[0][0]

    r = {
        "clientes": _n("SELECT COUNT(*) FROM monza_clientes WHERE nombre=:m", m=MARK),
        "cotizaciones": _n("SELECT COUNT(*) FROM monza_cotizaciones WHERE numero LIKE 'CT-PP-%'"),
        "ocs": _n("SELECT COUNT(*) FROM monza_oc_proveedor WHERE numero LIKE :p",
                  p=f"{MARK}%"),
        "embarques": _n("SELECT COUNT(*) FROM monza_embarques WHERE numero LIKE :p "
                        "OR notas=:m", p=f"{MARK}%", m=MARK),
        "despachos": _n("SELECT COUNT(*) FROM monza_despachos WHERE cliente_nombre=:m",
                        m=MARK),
        "reclamos": _n("SELECT COUNT(*) FROM monza_reclamos WHERE user_email=:e", e=EMAIL),
        "logs": _n("SELECT COUNT(*) FROM monza_logs WHERE user_email=:e", e=EMAIL),
        "recep_nac": _n("SELECT COUNT(*) FROM monza_recepcion_nacional "
                        "WHERE numero_guia_proveedor LIKE :p", p=f"{MARK}%"),
        "facturas": _n("SELECT COUNT(*) FROM monza_cont_factura_cliente "
                       "WHERE numero_factura LIKE :p", p=f"{MARK}%"),
        "compras": _n("SELECT COUNT(*) FROM monza_cont_compra "
                      "WHERE numero_documento LIKE :p", p=f"{MARK}%"),
        "pricing": _n("SELECT COUNT(*) FROM monza_emb_pricing WHERE observaciones=:m",
                      m=MARK),
        "dte": _n("SELECT COUNT(*) FROM monza_wasabil_dte WHERE payload_json=:m", m=MARK),
    }
    notifs = 0
    for entidad, ids in _VISTOS.items():
        if ids:
            lista = ",".join(str(int(i)) for i in ids if i)
            if lista:
                notifs += _n(f"SELECT COUNT(*) FROM monza_notificaciones WHERE "
                             f"entidad=:e AND entidad_id IN ({lista})", e=entidad)
    r["notificaciones"] = notifs
    return r


# ═════════════════════════════════════ RUN ══════════════════════════════════════

def run():
    db = SessionLocal()
    try:
        _limpiar(db)

        # ══════════════════════════════════════════════════════════════════════
        # 1 · EL INVARIANTE DE PLATA — el check más importante de la suite.
        #     Partir 10 en 6+4 NO puede mover ni un peso: si Σ subtotal_clp crece,
        #     el precio unitario derivado (subtotal/cantidad, como lo calcula
        #     MonzaVentasContabPage) se infla y ese número sube al total que
        #     publica Contabilidad. La cabecera no se recalcula NUNCA: tiene que
        #     quedar byte-idéntica.
        # ══════════════════════════════════════════════════════════════════════
        cot, (it,) = _venta(db, (10,))
        cab_antes = _cabecera(cot.id)
        sub_antes = _lineas(cot.id)[0]["subtotal"]
        r = _preparar_parcial([{"item_id": it.id, "cantidad": 6}])
        check("1.1 preparar-parcial 6 de 10 → 200", r.status_code == 200, r.text)
        j = r.json() if r.status_code == 200 else {}
        check("1.1 contrato de respuesta: preparados=1, partidos=1",
              j.get("preparados") == 1 and j.get("partidos") == 1, j)
        rem = (j.get("remanentes") or [{}])[0]
        check("1.1 remanentes[0] = {item_id, remanente_item_id, preparado 6, pendiente 4}",
              rem.get("item_id") == it.id and rem.get("preparado") == 6
              and rem.get("pendiente") == 4 and isinstance(rem.get("remanente_item_id"), int),
              rem)

        ok, det = _invariante_plata(cot.id, "P-PP-1", 10, sub_antes, cab_antes)
        check("1.2 ★ INVARIANTE DE PLATA: Σcantidad==10, Σsubtotal==original, "
              "cabecera BYTE-IDÉNTICA", ok, det)
        hs = _hermanas(cot.id, "P-PP-1")
        check("1.3 la línea quedó partida en DOS hermanas (6 preparado + 4 comprado)",
              len(hs) == 2
              and sorted((h["cantidad"], h["estado"]) for h in hs)
              == [(4, "comprado"), (6, "preparado")], hs)
        check("1.4 precio_unitario_clp IDÉNTICO en las dos hermanas (no se prorratea)",
              all(h["precio"] == PRECIO for h in hs), hs)
        check("1.5 el precio DERIVADO (subtotal/cantidad, como MonzaVentasContabPage) "
              "no se infla en ninguna hermana",
              all(abs(float(h["subtotal"]) / h["cantidad"] - PRECIO) < 1e-6 for h in hs), hs)
        check("1.6 repr() de la cabecera intacto (ni un redondeo de ida y vuelta)",
              repr(_cabecera(cot.id)) == repr(cab_antes),
              f"{_cabecera(cot.id)} vs {cab_antes}")
        _limpiar(db)

        # Caso del subtotal NULO (dato legado / carga directa a la BD): el None se
        # PRESERVA en las dos mitades. Inventar cantidad × precio ahí le agregaría
        # monto a una venta que no lo tenía.
        cot, (it,) = _venta(db, (10,), subtotal_none=True)
        cab_antes = _cabecera(cot.id)
        r = _preparar_parcial([{"item_id": it.id, "cantidad": 6}])
        check("1.7 línea con subtotal NULL se puede partir → 200", r.status_code == 200, r.text)
        hs = _hermanas(cot.id, "P-PP-1")
        check("1.7 el NULL se preserva en las DOS mitades (no se inventa monto)",
              len(hs) == 2 and all(h["subtotal"] is None for h in hs), hs)
        check("1.7 Σcantidad sigue siendo 10 y la cabecera no se tocó",
              sum(h["cantidad"] for h in hs) == 10 and _cabecera(cot.id) == cab_antes, hs)
        _limpiar(db)

        # Guard de COHERENCIA de la foto: una línea cuyo subtotal NO cuadra con
        # cantidad × precio movería plata al partirse → 400, y se prepara completa.
        cot, (it,) = _venta(db, (10,))
        db.query(MonzaCotizacionItem).filter(MonzaCotizacionItem.id == it.id).update(
            {"subtotal_clp": 999999.0})
        db.commit()
        r = _preparar_parcial([{"item_id": it.id, "cantidad": 6}])
        check("1.8 subtotal que no cuadra con cantidad×precio → 400 (partirla movería "
              "el monto de la venta)", r.status_code == 400, r.text)
        check("1.8 y NADA se movió: sigue una sola línea de 10 en 'comprado'",
              _estado(it.id) == ("comprado", 10) and len(_lineas(cot.id)) == 1,
              _lineas(cot.id))
        db.query(MonzaCotizacionItem).filter(MonzaCotizacionItem.id == it.id).update(
            {"subtotal_clp": None, "precio_unitario_clp": None})
        db.commit()
        _limpiar(db)

        # ══════════════════════════════════════════════════════════════════════
        # 2 · HERENCIA DE LA FOTO DE PRECIOS en el clon (los 6 campos UNITARIOS se
        #     copian SIN dividir; Embarques Pricing los lee como peso/costo unitario).
        # ══════════════════════════════════════════════════════════════════════
        cot, (it,) = _venta(db, (10,))
        r = _preparar_parcial([{"item_id": it.id, "cantidad": 7}])
        assert r.status_code == 200, r.text
        clon_id = r.json()["remanentes"][0]["remanente_item_id"]
        f = _sql("SELECT costo,moneda,peso_kg,tc_aplicado,tarifa_aerea,markup_pct,"
                 "precio_unitario_clp,descripcion,numero_parte,marca,procedencia,"
                 "calidad,plazo_entrega,cotizacion_id FROM monza_cotizacion_items "
                 "WHERE id IN (:a,:b) ORDER BY id", a=it.id, b=clon_id)
        orig, clon = f[0], f[1]
        check("2.1 foto UNITARIA copiada sin dividir: costo, moneda, peso_kg, "
              "tc_aplicado, tarifa_aerea, markup_pct", tuple(clon[:6]) == tuple(orig[:6]),
              f"{orig[:6]} vs {clon[:6]}")
        check("2.2 precio_unitario_clp idéntico", clon[6] == orig[6] == PRECIO,
              f"{orig[6]} vs {clon[6]}")
        check("2.3 identidad del repuesto copiada (descripción, parte, marca, "
              "procedencia, calidad, plazo)", tuple(clon[7:13]) == tuple(orig[7:13]),
              f"{orig[7:13]} vs {clon[7:13]}")
        check("2.4 el clon vive en la MISMA cotización", clon[13] == orig[13] == cot.id,
              f"{orig[13]} vs {clon[13]}")
        _limpiar(db)

        # ══════════════════════════════════════════════════════════════════════
        # 3 · oc_proveedor_id HEREDADO. Sin esa columna el clon pierde su OC y
        #     _rechazar_items_nacionales (JOIN por ella) deja de reconocerlo como
        #     nacional: un ítem nacional se colaría al pipeline de embarque.
        # ══════════════════════════════════════════════════════════════════════
        ocp = _oc(db, "internacional")
        cot, (it,) = _venta(db, (10,), ocp=ocp)
        r = _preparar_parcial([{"item_id": it.id, "cantidad": 6}])
        assert r.status_code == 200, r.text
        clon_id = r.json()["remanentes"][0]["remanente_item_id"]
        check("3.1 el clon hereda oc_proveedor_id",
              _sql("SELECT oc_proveedor_id FROM monza_cotizacion_items WHERE id=:i",
                   i=clon_id)[0][0] == ocp.id)
        # Prueba FUNCIONAL de la herencia: con la OC marcada nacional, el guard
        # anti-embarque tiene que reconocer al CLON por esa columna.
        db.query(MonzaOcProveedor).filter(MonzaOcProveedor.id == ocp.id).update(
            {"tipo_origen": "nacional"})
        db.commit()
        db2 = SessionLocal()
        try:
            reconocido = False
            try:
                _rechazar_items_nacionales(db2, [clon_id])
            except HTTPException as e:
                reconocido = e.status_code == 400
            check("3.2 _rechazar_items_nacionales reconoce al CLON como nacional "
                  "(la herencia sirve de verdad, no es decorativa)", reconocido)
            # Contraste: sin la columna heredada el guard es CIEGO — es exactamente
            # lo que rompería no copiar oc_proveedor_id.
            db2.query(MonzaCotizacionItem).filter(
                MonzaCotizacionItem.id == clon_id).update({"oc_proveedor_id": None})
            db2.commit()
            ciego = True
            try:
                _rechazar_items_nacionales(db2, [clon_id])
            except HTTPException:
                ciego = False
            check("3.3 CONTRASTE: con oc_proveedor_id en NULL el guard queda ciego "
                  "(por eso la regla 4 del split es obligatoria)", ciego)
        finally:
            db2.close()
        _limpiar(db)

        # ══════════════════════════════════════════════════════════════════════
        # 4 · LA CURA DEL RECLAMO FANTASMA, e2e. Es el valor operativo de la fase.
        # ══════════════════════════════════════════════════════════════════════

        # 4a · Partición en ABASTECIMIENTO: preparar 6 de 10 → embarcar los 6 →
        #      Bodega recibe 6 sobre una línea de 6 → 'completo' SIN faltante.
        cot, (it,) = _venta(db, (10,))
        cab_antes, sub_antes = _cabecera(cot.id), _lineas(cot.id)[0]["subtotal"]
        r = _preparar_parcial([{"item_id": it.id, "cantidad": 6}])
        assert r.status_code == 200, r.text
        rem_id = r.json()["remanentes"][0]["remanente_item_id"]
        r = _crear_embarque(item_ids=[it.id], awb=f"{MARK}-AWB1")
        check("4a embarque de la línea partida (6) → 200", r.status_code == 200, r.text)
        emb_id = r.json()["id"]
        _, rc = _recibir_y_cerrar(emb_id, [(it.id, "completo", 6)])
        check("4a cierre de recepción OK", rc.status_code == 200, rc.text)
        check("4a ★ SIN RECLAMO FANTASMA: el cierre no creó ninguno",
              rc.json().get("reclamos") == 0 and _reclamos([it.id, rem_id]) == [],
              f"{rc.json()} {_reclamos([it.id, rem_id])}")
        check("4a la línea que viajó quedó en bodega con sus 6",
              _estado(it.id) == ("en_bodega", 6), _estado(it.id))
        check("4a el remanente sigue en 'comprado' con 4, esperando el próximo AWB",
              _estado(rem_id) == ("comprado", 4), _estado(rem_id))
        ok, det = _invariante_plata(cot.id, "P-PP-1", 10, sub_antes, cab_antes)
        check("4a la plata sigue cuadrada después de TODO el recorrido físico", ok, det)
        _limpiar(db)

        # 4b · Partición en LOGÍSTICA (el caso literal de la spec): la línea se
        #      prepara completa y el EMBARQUE lleva 6 → el remanente queda en
        #      'preparado', listo para el próximo AWB.
        cot, (it,) = _venta(db, (10,), estado_linea="preparado")
        cab_antes, sub_antes = _cabecera(cot.id), _lineas(cot.id)[0]["subtotal"]
        r = _crear_embarque(items=[{"item_id": it.id, "cantidad": 6}], awb=f"{MARK}-AWB2")
        check("4b embarque con cantidad parcial (6 de 10) → 200", r.status_code == 200, r.text)
        j = r.json()
        check("4b contrato: items=1, partidos=1, remanente pendiente 4",
              j.get("items") == 1 and j.get("partidos") == 1
              and j["remanentes"][0]["embarcado"] == 6
              and j["remanentes"][0]["pendiente"] == 4, j)
        emb1, rem_id = j["id"], j["remanentes"][0]["remanente_item_id"]
        _, rc = _recibir_y_cerrar(emb1, [(it.id, "completo", 6)])
        check("4b ★ cierre SIN reclamo y las 4 quedan en 'preparado'",
              rc.status_code == 200 and rc.json().get("reclamos") == 0
              and _reclamos([it.id, rem_id]) == []
              and _estado(rem_id) == ("preparado", 4),
              f"{rc.text[:120]} {_estado(rem_id)} {_reclamos([it.id, rem_id])}")
        # 4b-bis · el remanente viaja en el SEGUNDO AWB y se recibe sin reclamo: la
        #          venta se completa en dos entregas, que es lo que el dueño necesita.
        r2 = _crear_embarque(item_ids=[rem_id], awb=f"{MARK}-AWB3")
        check("4b-bis el remanente sí puede embarcarse en el próximo AWB",
              r2.status_code == 200, r2.text)
        _, rc2 = _recibir_y_cerrar(r2.json()["id"], [(rem_id, "completo", 4)])
        check("4b-bis segundo cierre SIN reclamo y las DOS hermanas en bodega",
              rc2.status_code == 200 and rc2.json().get("reclamos") == 0
              and _estado(it.id) == ("en_bodega", 6)
              and _estado(rem_id) == ("en_bodega", 4),
              f"{rc2.text[:120]} {_estado(it.id)} {_estado(rem_id)}")
        ok, det = _invariante_plata(cot.id, "P-PP-1", 10, sub_antes, cab_antes)
        check("4b la plata cuadra tras DOS embarques y DOS recepciones", ok, det)
        _limpiar(db)

        # 4c · CONTRASTE con el comportamiento VIEJO (línea completa de 10 al
        #      embarque, llegan 6): AHÍ SÍ nace el reclamo por 4. Sin este contraste
        #      no está probado que 4a/4b curaron algo.
        cot, (it,) = _venta(db, (10,), estado_linea="preparado")
        r = _crear_embarque(item_ids=[it.id], awb=f"{MARK}-AWB4")
        assert r.status_code == 200, r.text
        _, rc = _recibir_y_cerrar(r.json()["id"], [(it.id, "faltante", 6)])
        recl = _reclamos([it.id])
        check("4c ★ CONTRASTE (vía vieja, línea entera de 10 y llegan 6): el cierre SÍ "
              "crea el reclamo por 4",
              rc.status_code == 200 and rc.json().get("reclamos") == 1
              and [(m, q) for (_i, m, q) in recl] == [("faltante", 4)],
              f"{rc.text[:120]} {recl}")
        check("4c (vía vieja) la línea sigue siendo UNA sola, de 10",
              len(_lineas(cot.id)) == 1 and _estado(it.id) == ("en_bodega", 10),
              _lineas(cot.id))
        _limpiar(db)

        # ══════════════════════════════════════════════════════════════════════
        # 5 · El clon NO entra al MonzaEmbarqueItem de ese embarque → Bodega no lo
        #     ve en el panel de recepción (si lo viera, volvería el faltante fantasma).
        # ══════════════════════════════════════════════════════════════════════
        cot, (it,) = _venta(db, (10,), estado_linea="preparado")
        r = _crear_embarque(items=[{"item_id": it.id, "cantidad": 6}], awb=f"{MARK}-AWB5")
        assert r.status_code == 200, r.text
        emb_id, rem_id = r.json()["id"], r.json()["remanentes"][0]["remanente_item_id"]
        check("5.1 el embarque tiene UNA sola línea (la que viaja), no la hermana",
              _sql("SELECT COUNT(*) FROM monza_embarque_items WHERE embarque_id=:e",
                   e=emb_id)[0][0] == 1)
        check("5.2 y esa línea es la original, no el clon",
              _sql("SELECT item_id FROM monza_embarque_items WHERE embarque_id=:e",
                   e=emb_id)[0][0] == it.id)
        rr = client.post(f"/api/monza/bodega/embarques/{emb_id}/recibir")
        rid = rr.json()["recepcion_id"]; _VISTOS["recepcion"].add(rid)
        panel = _panel_recepcion(rid)
        ids_panel = [x["id"] for x in panel["items"]]
        check("5.3 el panel de recepción de Bodega muestra solo la línea embarcada "
              "(cantidad 6) y NO el remanente",
              ids_panel == [it.id] and panel["total"] == 1
              and panel["items"][0]["cantidad"] == 6, panel["items"])
        check("5.4 el remanente sigue invisible para Bodega (no está en el embarque)",
              rem_id not in ids_panel, ids_panel)
        _limpiar(db)

        # ══════════════════════════════════════════════════════════════════════
        # 6 · TOPE FÍSICO: Σ tope de las hermanas ≤ cantidad original. El clon nace
        #     SIN fila de recepción, así que su tope es su propia cantidad, y las
        #     reservas no se cruzan (se agrupan por item_id).
        # ══════════════════════════════════════════════════════════════════════
        cot, (it,) = _venta(db, (10,), estado_linea="preparado")
        r = _crear_embarque(items=[{"item_id": it.id, "cantidad": 6}], awb=f"{MARK}-AWB6")
        assert r.status_code == 200, r.text
        emb_id, rem_id = r.json()["id"], r.json()["remanentes"][0]["remanente_item_id"]
        _recibir_y_cerrar(emb_id, [(it.id, "completo", 6)])
        av = _avance(cot.id)
        check("6.1 la hermana en bodega ofrece 6; la que aún no viaja ofrece 0",
              av[it.id]["qty_disponible"] == 6 and av[rem_id]["qty_disponible"] == 0,
              {k: v["qty_disponible"] for k, v in av.items()})
        check("6.2 ★ Σ tope de las hermanas ≤ cantidad original (10): no se puede "
              "despachar más de lo vendido",
              av[it.id]["qty_disponible"] + av[rem_id]["qty_disponible"] <= 10)
        check("6.3 despachar 7 de una línea de 6 → 400 (sobredespacho)",
              _despachar(cot.id, it.id, 7).status_code == 400)
        check("6.4 despachar los 6 → 200", _despachar(cot.id, it.id, 6).status_code == 200)
        check("6.5 tras reservar los 6, el cupo cae a 0 (el borrador reserva)",
              _avance(cot.id)[it.id]["qty_disponible"] == 0)
        # El remanente llega en el próximo AWB: Σ tope == 10 exacto, ni una unidad más.
        r2 = _crear_embarque(item_ids=[rem_id], awb=f"{MARK}-AWB7")
        _recibir_y_cerrar(r2.json()["id"], [(rem_id, "completo", 4)])
        av = _avance(cot.id)
        check("6.6 con las dos entregas recibidas, Σ despachable+reservado == 10 exacto",
              av[it.id]["qty_disponible"] + av[rem_id]["qty_disponible"] + 6 == 10,
              {k: v["qty_disponible"] for k, v in av.items()})
        check("6.7 despachar 5 del remanente de 4 → 400",
              _despachar(cot.id, rem_id, 5).status_code == 400)
        _limpiar(db)

        # ══════════════════════════════════════════════════════════════════════
        # 7 · LOS 7 CANDADOS DUROS (409). Partir una línea que ya tocó un documento
        #     legal o una cantidad congelada es IRREVERSIBLE: no hay clamp posible,
        #     solo no hacerlo. Se verifica además que NADA se mutó tras cada 409.
        # ══════════════════════════════════════════════════════════════════════
        def _guard(nombre, sembrar, frase, estado_linea="comprado", via="preparar"):
            cot_g, (itg,) = _venta(db, (10,), estado_linea=estado_linea)
            sembrar(cot_g, itg)
            if via == "preparar":
                rg = _preparar_parcial([{"item_id": itg.id, "cantidad": 6}])
            else:
                rg = _crear_embarque(items=[{"item_id": itg.id, "cantidad": 6}])
            check(f"7.{nombre} → 409 y el mensaje nombra el bloqueo",
                  rg.status_code == 409 and frase in rg.text.lower(),
                  f"{rg.status_code} {rg.text[:200]}")
            check(f"7.{nombre} tras el 409 NADA se movió (sigue 1 línea de 10)",
                  len(_lineas(cot_g.id)) == 1 and _estado(itg.id) == (estado_linea, 10),
                  _lineas(cot_g.id))
            _limpiar(db)

        def _sembrar_despacho(cot_g, itg, estado="en_preparacion"):
            d = MonzaDespacho(numero=f"{MARK}-D-{uuid.uuid4().hex[:6]}", estado=estado,
                              cotizacion_id=cot_g.id, cliente_nombre=MARK)
            db.add(d); db.flush()
            db.add(MonzaDespachoItem(despacho_id=d.id, item_id=itg.id, qty_despachada=1))
            db.commit(); _VISTOS["despacho"].add(d.id)
            return d

        _guard("1 despacho no anulado", lambda c, i: _sembrar_despacho(c, i), "despacho")

        def _sembrar_guia(cot_g, itg):
            # El despacho va ANULADO a propósito: el folio del SII SOBREVIVE a la
            # anulación, y ese es el caso que el candado 2 tiene que atrapar (con el
            # despacho vivo ganaría el candado 1 y este nunca se probaría).
            d = _sembrar_despacho(cot_g, itg, estado="anulado")
            db.add(MonzaWasabilDte(despacho_id=d.id, tipo_dte=TIPO_DOC_GUIA,
                                   status_id=STATUS_EMITIDO, folio="999",
                                   uuid=f"fake-{uuid.uuid4().hex[:8]}",
                                   payload_json=MARK))
            db.commit()

        _guard("2 guía 52 viva (despacho ANULADO, folio vivo)", _sembrar_guia,
               "guía electrónica")

        def _sembrar_factura(cot_g, itg):
            fac = MonzaContFacturaCliente(
                cotizacion_id=cot_g.id, numero_factura=f"{MARK}F{uuid.uuid4().hex[:4]}",
                tipo_doc="factura", fecha_emision=date.today(), monto_neto=1000,
                iva=190, monto_bruto=1190, monto_pagado=0, saldo=1190)
            db.add(fac); db.flush()
            db.add(MonzaContFacturaClienteItem(
                factura_id=fac.id, item_cotizacion_id=itg.id, numero_parte=itg.numero_parte,
                cantidad=6, precio_unit_neto=PRECIO, total_neto=6 * PRECIO))
            db.commit()

        _guard("3 factura de cliente con esa línea", _sembrar_factura, "facturado")

        def _sembrar_recepcion(cot_g, itg):
            emb = _embarque_directo(db, [itg])
            rec = MonzaRecepcion(estado="abierta", embarque_id=emb.id,
                                 usuario_email=EMAIL)
            db.add(rec); db.flush(); _VISTOS["recepcion"].add(rec.id)
            db.add(MonzaRecepcionItem(recepcion_id=rec.id, item_id=itg.id,
                                      estado_recepcion="completo", qty_recibida=6))
            db.commit()

        _guard("4 recepción de embarque (abierta incluida)", _sembrar_recepcion,
               "recepción de")

        def _sembrar_recep_nacional(cot_g, itg):
            rec = MonzaRecepcionNacional(numero_guia_proveedor=f"{MARK}-G", estado="abierta")
            db.add(rec); db.flush()
            db.add(MonzaRecepcionNacionalItem(
                recepcion_id=rec.id, item_cotizacion_id=itg.id,
                numero_parte=itg.numero_parte, qty_recibida=6,
                estado_recepcion="completo"))
            db.commit()

        _guard("5 recepción nacional", _sembrar_recep_nacional, "recepción nacional")

        def _sembrar_pricing(cot_g, itg):
            emb = _embarque_directo(db, [itg])
            p = MonzaEmbPricing(embarque_id=emb.id, estado="cerrado", observaciones=MARK)
            db.add(p); db.flush()
            db.add(MonzaEmbPricingItem(pricing_id=p.id, item_cotizacion_id=itg.id,
                                       numero_parte=itg.numero_parte, cantidad=10))
            db.commit()

        _guard("6 costo de embarque congelado (Pricing)", _sembrar_pricing,
               "costo de embarque")

        def _sembrar_costo_compra(cot_g, itg):
            c = MonzaContCompra(numero_documento=f"{MARK}C{uuid.uuid4().hex[:4]}",
                                acreedor=f"{MARK} PROV", moneda="CLP", tc=1,
                                monto_neto=1000, iva=190, monto_total=1190,
                                monto_total_clp=1190, fecha=date.today())
            db.add(c); db.flush()
            db.add(MonzaContCompraItem(compra_id=c.id, item_cotizacion_id=itg.id,
                                       numero_parte=itg.numero_parte, cantidad=10,
                                       precio_unit=100, costo_unit_clp=100,
                                       costo_total_clp=1000))
            db.commit()

        _guard("7 costo de compra nacional asignado", _sembrar_costo_compra,
               "costo de compra")

        # 7.8 · El candado también protege la SEGUNDA puerta al pipeline (embarque).
        _guard("8 el candado vale también en el EMBARQUE (2ª puerta)",
               _sembrar_recepcion, "recepción de", estado_linea="preparado", via="embarque")

        # 7.9 · DESVÍO DELIBERADO del diseño: el candado protege LA PARTICIÓN, no el
        #       movimiento. Un ítem con costo congelado que se mueve COMPLETO (flujo
        #       real: Logística lo saca del embarque con quitar_item y lo re-embarca
        #       entero en el próximo AWB) sigue funcionando — si el guard se aplicara
        #       a todos los ids, ese flujo pasaría a dar 409 sin partir nada.
        cot, (it,) = _venta(db, (10,))
        _sembrar_pricing(cot, it)
        r = _preparar_parcial([{"item_id": it.id, "cantidad": None}])
        check("7.9 con costo congelado, mover la línea COMPLETA sigue permitido "
              "(el candado protege la partición, no el movimiento)",
              r.status_code == 200 and r.json()["partidos"] == 0, r.text[:200])
        check("7.9 y la línea se movió entera, sin clon",
              _estado(it.id) == ("preparado", 10) and len(_lineas(cot.id)) == 1,
              _lineas(cot.id))
        _limpiar(db)

        # ══════════════════════════════════════════════════════════════════════
        # 8 · GUARD DE ESTADO (400) y LOS 4 VICIOS DE GRUPO AM QUE NO SE COPIARON.
        #     Cada uno de estos es un 400 EXPLÍCITO donde GA corrige en silencio.
        # ══════════════════════════════════════════════════════════════════════
        cot, (a, b) = _venta(db, (10, 5))
        # vicio GA n°1: min(qty, cantidad) — un typo del operador se convertía en
        # "prepara todo" sin avisar.
        r = _preparar_parcial([{"item_id": a.id, "cantidad": 11}])
        check("8.1 qty > cantidad → 400, NO el clamp silencioso de GA",
              r.status_code == 400 and "excede" in r.text.lower(), r.text[:160])
        check("8.1 la línea quedó intacta (10, comprado)", _estado(a.id) == ("comprado", 10))
        # vicio GA n°2: `if item_data.cantidad` — 0 es falsy y caía a "toda la línea".
        r = _preparar_parcial([{"item_id": a.id, "cantidad": 0}])
        check("8.2 qty = 0 → 400, NO 'toda la línea' (el falsy de GA)",
              r.status_code == 400 and "mayor que 0" in r.text.lower(), r.text[:160])
        check("8.2 con qty=0 la línea NO se preparó", _estado(a.id) == ("comprado", 10))
        # vicio GA n°3: `if not item: continue` — ids inexistentes se saltaban callados.
        r = _preparar_parcial([{"item_id": 99999999, "cantidad": 1}])
        check("8.3 ítem inexistente → 400, NO un salto silencioso",
              r.status_code == 400 and "inexistente" in r.text.lower(), r.text[:160])
        # vicio GA n°4: id repetido burlaría la validación de cantidad (cada línea se
        # compararía sola contra el total).
        r = _preparar_parcial([{"item_id": a.id, "cantidad": 6},
                               {"item_id": a.id, "cantidad": 6}])
        check("8.4 ítem repetido en el pedido → 400 (12 unidades de una línea de 10)",
              r.status_code == 400 and "repetid" in r.text.lower(), r.text[:160])
        check("8.4 y nada se movió con el pedido repetido",
              _estado(a.id) == ("comprado", 10) and len(_lineas(cot.id)) == 2,
              _lineas(cot.id))
        r = _preparar_parcial([])
        check("8.5 pedido vacío → 400", r.status_code == 400, r.text[:120])
        # ATOMICIDAD: un pedido mixto (uno válido + uno inválido) no debe dejar la
        # mitad hecha. Es el riesgo real del loop por ítem.
        r = _preparar_parcial([{"item_id": a.id, "cantidad": 6},
                               {"item_id": b.id, "cantidad": 99}])
        check("8.6 pedido MIXTO (uno válido, uno inválido) → 400 y NADA se movió",
              r.status_code == 400 and _estado(a.id) == ("comprado", 10)
              and _estado(b.id) == ("comprado", 5) and len(_lineas(cot.id)) == 2,
              f"{r.status_code} {_lineas(cot.id)}")
        _limpiar(db)

        # Guard de ESTADO en las dos puertas: 400 con el estado de cada ítem, nunca
        # el `continue` silencioso que dejaba nacer un embarque con menos ítems.
        cot, (it,) = _venta(db, (10,), estado_linea="preparado")
        r = _preparar_parcial([{"item_id": it.id, "cantidad": 6}])
        check("8.7 preparar-parcial sobre un ítem 'preparado' → 400 (guard de estado)",
              r.status_code == 400 and "comprado" in r.text.lower(), r.text[:180])
        r = _crear_embarque(items=[{"item_id": it.id, "cantidad": 6}])
        check("8.8 el embarque acepta solo 'preparado' → ok aquí", r.status_code == 200,
              r.text[:160])
        _limpiar(db)
        cot, (it,) = _venta(db, (10,), estado_linea="comprado")
        embs_antes = _sql("SELECT COUNT(*) FROM monza_embarques")[0][0]
        r = _crear_embarque(items=[{"item_id": it.id, "cantidad": 6}])
        check("8.9 embarcar un ítem 'comprado' → 400 (antes se descartaba en silencio "
              "y el embarque nacía vacío)",
              r.status_code == 400 and "preparado" in r.text.lower(), r.text[:180])
        check("8.9 y el 400 NO dejó una cabecera de embarque a medio crear",
              _sql("SELECT COUNT(*) FROM monza_embarques")[0][0] == embs_antes)
        _limpiar(db)

        # ══════════════════════════════════════════════════════════════════════
        # 9 · CONCURRENCIA con hilos REALES. SIN el lock, dos preparaciones parciales
        #     simultáneas del mismo ítem leen cantidad=10 cada una, cada una clona su
        #     remanente y SE INVENTAN UNIDADES. Varias rondas: la carrera es
        #     probabilística y una sola ronda da falsos verdes.
        # ══════════════════════════════════════════════════════════════════════
        for r_i in range(RONDAS):
            _limpiar(db)
            cot, (it,) = _venta(db, (10,))
            cab_antes, sub_antes = _cabecera(cot.id), _lineas(cot.id)[0]["subtotal"]
            res = _en_paralelo(lambda i: _preparar_parcial(
                [{"item_id": it.id, "cantidad": 6}]).status_code, n=2)
            ok, det = _invariante_plata(cot.id, "P-PP-1", 10, sub_antes, cab_antes)
            check(f"9.{r_i} ★ dos preparar-parcial simultáneos: Σ cantidad y Σ subtotal "
                  f"CONSERVADOS (no se inventan unidades)", ok, f"http={res} {det}")
            check(f"9.{r_i} exactamente UNA preparación entra; la otra es rechazo "
                  f"explícito (no silencio)",
                  res.count(200) == 1 and all(s in (200, 400, 409) for s in res), res)
        _limpiar(db)

        # 3 hilos sobre el mismo ítem: el ganador único tiene que sostenerse.
        for r_i in range(2):
            _limpiar(db)
            cot, (it,) = _venta(db, (10,))
            cab_antes, sub_antes = _cabecera(cot.id), _lineas(cot.id)[0]["subtotal"]
            res = _en_paralelo(lambda i: _preparar_parcial(
                [{"item_id": it.id, "cantidad": 4 + i}]).status_code, n=3)
            ok, det = _invariante_plata(cot.id, "P-PP-1", 10, sub_antes, cab_antes)
            check(f"9.3h{r_i} TRES hilos: la plata se conserva y solo uno gana",
                  ok and res.count(200) == 1, f"http={res} {det}")
        _limpiar(db)

        # ══════════════════════════════════════════════════════════════════════
        # 10 · COMPATIBILIDAD: las dos vías legadas siguen funcionando IGUAL.
        # ══════════════════════════════════════════════════════════════════════
        cot, (a, b) = _venta(db, (10, 5))
        r = _preparar_legado([a.id, b.id])
        check("10.1 /preparar legado (item_ids) → 200 con {ok, preparados}",
              r.status_code == 200 and r.json() == {"ok": True, "preparados": 2}, r.text)
        check("10.1 movió las líneas COMPLETAS y no partió nada",
              _estado(a.id) == ("preparado", 10) and _estado(b.id) == ("preparado", 5)
              and len(_lineas(cot.id)) == 2, _lineas(cot.id))
        r = _crear_embarque(item_ids=[a.id, b.id], awb=f"{MARK}-AWB8")
        check("10.2 /embarques legado (item_ids) → 200, sin partir",
              r.status_code == 200 and r.json()["items"] == 2
              and r.json()["partidos"] == 0, r.text[:160])
        check("10.2 las dos líneas viajan enteras",
              _estado(a.id) == ("embarcado", 10) and _estado(b.id) == ("embarcado", 5)
              and len(_lineas(cot.id)) == 2, _lineas(cot.id))
        _limpiar(db)

        cot, (it,) = _venta(db, (10,), estado_linea="preparado")
        r = _crear_embarque(items=[{"item_id": it.id}], awb=f"{MARK}-AWB9")
        check("10.3 /embarques con items sin cantidad (None) = línea completa",
              r.status_code == 200 and r.json()["partidos"] == 0
              and _estado(it.id) == ("embarcado", 10), r.text[:160])
        _limpiar(db)

        cot, (it,) = _venta(db, (10,))
        r = _preparar_parcial([{"item_id": it.id, "cantidad": 10}])
        check("10.4 preparar-parcial con cantidad == total → no parte (partidos 0)",
              r.status_code == 200 and r.json()["partidos"] == 0
              and len(_lineas(cot.id)) == 1 and _estado(it.id) == ("preparado", 10),
              r.text[:160])
        _limpiar(db)
        cot, (it,) = _venta(db, (10,))
        r = _preparar_parcial([{"item_id": it.id}])
        check("10.5 preparar-parcial sin campo cantidad (sentinela None) → línea completa",
              r.status_code == 200 and r.json()["partidos"] == 0
              and _estado(it.id) == ("preparado", 10), r.text[:160])
        _limpiar(db)

        # Los helpers, ejercidos DIRECTO (son los reutilizables de la fase).
        cot, (it,) = _venta(db, (10,))
        db3 = SessionLocal()
        try:
            pedido = [SimpleNamespace(item_id=it.id, cantidad=6)]
            check("10.6 _validar_pedidos_parciales devuelve los ids saneados",
                  _validar_pedidos_parciales(pedido) == [it.id])
            by_id = _lockear_items_para_split(db3, [it.id], "comprado")
            check("10.6 _lockear_items_para_split devuelve {id: item} bajo lock",
                  list(by_id) == [it.id] and by_id[it.id].cantidad == 10)
            clon = _partir_linea(db3, by_id[it.id], 6, "comprado")
            check("10.6 _partir_linea deja 6 y devuelve la hermana de 4",
                  clon is not None and clon.cantidad == 4 and by_id[it.id].cantidad == 6)
            check("10.6 y la regla de oro se cumple en memoria: Σ subtotal == original",
                  float(clon.subtotal_clp) + float(by_id[it.id].subtotal_clp)
                  == 10 * PRECIO,
                  f"{clon.subtotal_clp} + {by_id[it.id].subtotal_clp}")
            check("10.6 _clonar_item_remanente es el ÚNICO dueño de la copia "
                  "(mismo objeto que usa _partir_linea)",
                  abast._clonar_item_remanente is _clonar_item_remanente)
            check("10.6 _guard_duro_del_split y _rechazar_split_sobre_documento "
                  "importables (contrato público de la fase)",
                  callable(_guard_duro_del_split)
                  and callable(_rechazar_split_sobre_documento))
            db3.rollback()
        finally:
            db3.close()
        _limpiar(db)

        # ══════════════════════════════════════════════════════════════════════
        # 11 · PODER DISCRIMINANTE. Un test que solo pasa no prueba nada: hay que
        #      demostrar que DETECTA la regresión. Se inyecta el vicio de Grupo AM
        #      (el clon hereda el subtotal COMPLETO en vez de recalcularlo) y se
        #      exige que el MISMO predicado del check 1.2 devuelva ROJO.
        # ══════════════════════════════════════════════════════════════════════
        original_clonar = abast._clonar_item_remanente

        def _clonar_vicioso(db_, it_, remanente, estado, copiar_oc=True):
            """El vicio: subtotal completo heredado (partir 10 en 6+4 dejando el
            subtotal de 10 en la hermana de 4 infla su precio derivado 2,5×).
            `copiar_oc` espeja la firma real (creció con la asignación parcial a
            OC de proveedor): un mock con firma vieja revienta con TypeError."""
            clon = original_clonar(db_, it_, remanente, estado, copiar_oc)
            clon.subtotal_clp = it_.subtotal_clp
            db_.flush()
            return clon

        abast._clonar_item_remanente = _clonar_vicioso
        try:
            cot, (it,) = _venta(db, (10,))
            cab_antes, sub_antes = _cabecera(cot.id), _lineas(cot.id)[0]["subtotal"]
            r = _preparar_parcial([{"item_id": it.id, "cantidad": 6}])
            check("11.1 la sonda corre igual (el vicio NO es un error, es plata mal "
                  "repartida: por eso hay que testearlo)", r.status_code == 200, r.text[:160])
            ok, det = _invariante_plata(cot.id, "P-PP-1", 10, sub_antes, cab_antes)
            check("11.2 ★ PODER DISCRIMINANTE: con el vicio de GA inyectado, el MISMO "
                  "predicado del check 1.2 da ROJO (la suite no es complaciente)",
                  not ok, f"el invariante NO detectó la duplicación de plata: {det}")
            hs = _hermanas(cot.id, "P-PP-1")
            suma = sum(float(h["subtotal"] or 0) for h in hs)
            check("11.3 y el daño es exactamente el esperado: Σ subtotal infla la venta "
                  f"a {10 * PRECIO + 6 * PRECIO:.0f} en vez de {10 * PRECIO:.0f}",
                  suma == 16 * PRECIO, f"Σ={suma} filas={hs}")
        finally:
            abast._clonar_item_remanente = original_clonar
            _limpiar(db)

        # Contra-prueba de la restauración: con el código REAL de vuelta, verde.
        cot, (it,) = _venta(db, (10,))
        cab_antes, sub_antes = _cabecera(cot.id), _lineas(cot.id)[0]["subtotal"]
        r = _preparar_parcial([{"item_id": it.id, "cantidad": 6}])
        ok, det = _invariante_plata(cot.id, "P-PP-1", 10, sub_antes, cab_antes)
        check("11.4 restaurado el código real, el mismo predicado vuelve a VERDE "
              "(la sonda no dejó el módulo parchado)", r.status_code == 200 and ok, det)
        _limpiar(db)

        # Sonda del LOCK: sin FOR UPDATE, dos preparaciones simultáneas del mismo ítem
        # inventan unidades. Es la justificación empírica del §4.2 de la spec (Grupo AM
        # no tiene este lock y por eso su split es inseguro).
        original_lock = abast._lockear_items_para_split

        def _lock_sin_for_update(db_, ids, estado_esperado):
            """Misma función SIN with_for_update(): el vicio de GA (compras.py:1024)."""
            items = (
                db_.query(MonzaCotizacionItem)
                .filter(MonzaCotizacionItem.id.in_(ids))
                .order_by(MonzaCotizacionItem.id.asc())
                .populate_existing()
                .all()
            )
            if len(items) != len(set(ids)):
                raise HTTPException(400, "Ítem(s) inexistente(s)")
            if [i for i in items if (i.estado_linea or "") != estado_esperado]:
                raise HTTPException(400, f"no están en estado '{estado_esperado}'")
            return {i.id: i for i in items}

        abast._lockear_items_para_split = _lock_sin_for_update
        inventadas = 0
        try:
            for r_i in range(6):
                _limpiar(db)
                cot, (it,) = _venta(db, (10,))
                cab_antes, sub_antes = _cabecera(cot.id), _lineas(cot.id)[0]["subtotal"]
                _en_paralelo(lambda i: _preparar_parcial(
                    [{"item_id": it.id, "cantidad": 6}]).status_code, n=2)
                ok, _d = _invariante_plata(cot.id, "P-PP-1", 10, sub_antes, cab_antes)
                if not ok:
                    inventadas += 1
        finally:
            abast._lockear_items_para_split = original_lock
            _limpiar(db)
        check(f"11.5 ★ PODER DISCRIMINANTE del LOCK: sin FOR UPDATE se inventan "
              f"unidades en {inventadas}/6 rondas (con el lock real: 0, sección 9)",
              inventadas > 0,
              "el arnés no logró reproducir la carrera: los checks de la sección 9 "
              "podrían ser complacientes")
        check("11.6 el módulo quedó restaurado tras las dos sondas",
              abast._lockear_items_para_split is original_lock
              and abast._clonar_item_remanente is original_clonar)

    finally:
        _limpiar(db)
        res = _residuos()
        db.close()
        print(f"Residuos: {res}")
    check("12 CERO residuos en la BD con el MARK (sesión nueva, SQL crudo)",
          all(v == 0 for v in res.values()), res)

    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_preparar_parcial_monza():
    """Wrapper pytest de una línea: sin él la suite sería INVISIBLE al gate."""
    run()


if __name__ == "__main__":
    try:
        run()
    except AssertionError as e:
        print("\n=== RESULTADO:", e, "===")
        sys.exit(1)
    sys.exit(0)
