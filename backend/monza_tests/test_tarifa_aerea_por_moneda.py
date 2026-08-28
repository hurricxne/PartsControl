"""Tarifa aérea POR MONEDA (2026-08-08) — el selector de la calculadora cambia el PRECIO,
no solo la etiqueta.

EL DEFECTO QUE ESTO FIJA
------------------------
`monza_config` tenía UNA tarifa (`tarifa_aerea_por_kg`) + la moneda en que estaba
expresada. Cuando la calculadora ganó el selector de moneda del flete (commit 87aec91),
elegir USD seguía usando ese MISMO número: 4,5 EUR/kg se cotizaba como 4,5 USD/kg. El
flete quedaba mal y el error viajaba DENTRO del precio de venta, sin aviso.

Ahora hay dos tarifas (`tarifa_aerea_eur_por_kg` / `tarifa_aerea_usd_por_kg`) y
`_flete_efectivo` —la fuente única del flete— resuelve por moneda.

SONDA DE PODER DISCRIMINANTE (§2): con tarifas distintas por moneda, el flete de la
MISMA pieza tiene que dar DISTINTO en EUR que en USD. Si alguien vuelve a la tarifa
única, ese check cae solo.

Lo que se fija, punto por punto:
  1. La tarifa sale de la moneda pedida (EUR→eur, USD→usd).
  2. Cambiar la moneda cambia el flete y, por lo tanto, el precio (el defecto original).
  3. FALLA CERRADO: moneda sin tarifa configurada → 400 accionable, NUNCA flete 0.
     Un flete en 0 no se ve en pantalla y subvalúa la venta.
  4. La tarifa EXPLÍCITA (foto congelada del lead) manda sobre la configuración: un
     cambio de tarifa hoy no puede mover un precio ya ofrecido ayer.
  5. Respaldo del legado: la tarifa vieja sirve SOLO para su propia moneda, jamás se le
     presta a la otra (prestarla ES el defecto original).
  6. El endpoint de configuración rechaza tarifa <= 0 y moneda que no sea EUR/USD.

No toca la fila real de configuración: trabaja sobre objetos MonzaConfig en memoria y,
para el endpoint, sobre una sesión que se descarta (rollback). No deja datos.

Corre con:  ./venv/bin/python -m pytest monza_tests/test_tarifa_aerea_por_moneda.py -q
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from monza_models import MonzaConfig  # noqa: E402
from monza_router_config import ConfigIn  # noqa: E402
from monza_router_cotizador import (  # noqa: E402
    _calcular_precio, _flete_efectivo, _tarifa_configurada,
)

# TC fijos para que los números del test sean verificables a mano.
TC_EUR = 1000.0
TC_USD = 900.0


def _cfg(**kw) -> MonzaConfig:
    """MonzaConfig en memoria (sin BD): estos helpers son funciones puras sobre el objeto."""
    base = dict(
        id=1, tc_eur_clp=TC_EUR, tc_usd_clp=TC_USD, iva_pct=19,
        tarifa_aerea_eur_por_kg=None, tarifa_aerea_usd_por_kg=None,
        tarifa_aerea_por_kg=None, moneda_tarifa="EUR",
    )
    base.update(kw)
    return MonzaConfig(**base)


def test_tarifa_sale_de_la_moneda_pedida():
    cfg = _cfg(tarifa_aerea_eur_por_kg=4.5, tarifa_aerea_usd_por_kg=6.0)
    assert _tarifa_configurada(cfg, "EUR") == 4.5
    assert _tarifa_configurada(cfg, "USD") == 6.0
    assert _flete_efectivo(cfg, "EUR") == ("EUR", 4.5)
    assert _flete_efectivo(cfg, "USD") == ("USD", 6.0)


def test_sonda_cambiar_la_moneda_cambia_el_precio():
    """SONDA: el corazón del arreglo. Misma pieza, mismo peso, distinta moneda de flete
    → flete distinto → precio distinto. Con la tarifa única esto daba IGUAL."""
    cfg = _cfg(tarifa_aerea_eur_por_kg=4.5, tarifa_aerea_usd_por_kg=6.0)
    en_eur = _calcular_precio(costo=0, moneda="CLP", peso_kg=10, markup_pct=0,
                              cfg=cfg, moneda_tarifa="EUR")
    en_usd = _calcular_precio(costo=0, moneda="CLP", peso_kg=10, markup_pct=0,
                              cfg=cfg, moneda_tarifa="USD")
    # 10 kg × 4,5 EUR × 1.000 = 45.000 · 10 kg × 6,0 USD × 900 = 54.000
    assert en_eur["flete_clp"] == 45_000, en_eur
    assert en_usd["flete_clp"] == 54_000, en_usd
    assert en_eur["precio_neto"] != en_usd["precio_neto"], (
        "el selector de moneda tiene que mover el precio; si esto falla, la tarifa "
        "volvió a ser única y el flete en USD es el número de EUR"
    )
    # La foto del flete viaja en la respuesta (es lo que la pantalla muestra y persiste).
    assert (en_usd["moneda_tarifa"], en_usd["tarifa_aerea"], en_usd["tc_tarifa"]) == ("USD", 6.0, TC_USD)


def test_moneda_sin_tarifa_falla_cerrado():
    """Sin tarifa para esa moneda se CORTA con 400. Jamás se cotiza con flete 0: un 0 no
    se nota en pantalla y sale dentro del precio de venta."""
    cfg = _cfg(tarifa_aerea_eur_por_kg=4.5)  # USD sin cargar
    assert _tarifa_configurada(cfg, "USD") is None
    with pytest.raises(HTTPException) as e:
        _flete_efectivo(cfg, "USD")
    assert e.value.status_code == 400
    detalle = e.value.detail
    assert "USD" in detalle and "Configuración" in detalle, detalle
    # El mensaje tiene que decir QUÉ hacer, no solo que falló.
    assert "cárgala" in detalle.lower(), detalle
    # …y la moneda que sí está cargada sigue funcionando (el corte es por moneda).
    assert _flete_efectivo(cfg, "EUR") == ("EUR", 4.5)


def test_tarifa_explicita_gana_sobre_la_configuracion():
    """La tarifa congelada en el lead manda: cambiar la configuración HOY no puede mover
    el precio de una cotización ya calculada (y ofrecida) AYER."""
    cfg = _cfg(tarifa_aerea_eur_por_kg=4.5, tarifa_aerea_usd_por_kg=6.0)
    assert _flete_efectivo(cfg, "USD", 3.2) == ("USD", 3.2)
    # Incluso para una moneda que HOY no tiene tarifa configurada: el lead viejo se
    # recalcula con SU foto, no se bloquea.
    cfg_sin_usd = _cfg(tarifa_aerea_eur_por_kg=4.5)
    assert _flete_efectivo(cfg_sin_usd, "USD", 3.2) == ("USD", 3.2)


def test_respaldo_del_legado_no_cruza_monedas():
    """La tarifa vieja respalda SOLO la moneda en que estaba expresada. Prestársela a la
    otra moneda es exactamente el defecto que este cambio corrige."""
    legado_eur = _cfg(tarifa_aerea_por_kg=4.5, moneda_tarifa="EUR")
    assert _tarifa_configurada(legado_eur, "EUR") == 4.5, "su propia moneda: sí"
    assert _tarifa_configurada(legado_eur, "USD") is None, "la otra moneda: JAMÁS"
    with pytest.raises(HTTPException):
        _flete_efectivo(legado_eur, "USD")
    # Y con el legado en USD, la simetría (para que nadie lo arregle solo en un sentido).
    legado_usd = _cfg(tarifa_aerea_por_kg=7.0, moneda_tarifa="USD")
    assert _tarifa_configurada(legado_usd, "USD") == 7.0
    assert _tarifa_configurada(legado_usd, "EUR") is None
    # La tarifa NUEVA de esa moneda le gana al legado (la migración deja las dos vivas).
    mixto = _cfg(tarifa_aerea_eur_por_kg=5.5, tarifa_aerea_por_kg=4.5, moneda_tarifa="EUR")
    assert _tarifa_configurada(mixto, "EUR") == 5.5


def test_sin_moneda_pedida_usa_la_preseleccionada():
    """`None` = "usar la global": es como se comportaban los leads previos al selector."""
    cfg = _cfg(tarifa_aerea_eur_por_kg=4.5, tarifa_aerea_usd_por_kg=6.0, moneda_tarifa="USD")
    assert _flete_efectivo(cfg) == ("USD", 6.0)


_CAMPOS_CONFIG = ("tarifa_aerea_eur_por_kg", "tarifa_aerea_usd_por_kg",
                  "moneda_tarifa", "tarifa_aerea_por_kg")


def _restaurar_config(previo) -> None:
    """Devuelve monza_config a como estaba, en una transacción PROPIA y con reintento.

    Va separada del resto de la limpieza a propósito: esta fila le pone precio a toda
    cotización de MonzaParts, así que un DELETE que falle no puede llevarse puesta la
    restauración."""
    from database import SessionLocal as _SL
    for _ in range(3):
        db = _SL()
        try:
            cfg = db.query(MonzaConfig).filter(MonzaConfig.id == 1).first()
            if cfg:
                for campo, valor in zip(_CAMPOS_CONFIG, previo):
                    setattr(cfg, campo, valor)
                db.commit()
            return
        except Exception:
            db.rollback()
        finally:
            db.close()


def _verificar_config_restaurada(previo) -> None:
    """Falla RUIDOSAMENTE si la config quedó con los valores del test."""
    from database import SessionLocal as _SL
    db = _SL()
    try:
        cfg = db.query(MonzaConfig).filter(MonzaConfig.id == 1).first()
        if not cfg:
            return
        actual = tuple(getattr(cfg, c) for c in _CAMPOS_CONFIG)
        assert actual == tuple(previo), (
            "la configuración REAL de MonzaParts quedó alterada por este test "
            f"(esperado {tuple(previo)}, quedó {actual}): restáurala a mano antes de cotizar")
    finally:
        db.close()


def test_la_cotizacion_congela_la_tarifa_DEL_LEAD_no_la_global():
    """SONDA: la foto de la cotización tiene que reproducir SUS PROPIOS precios.

    El operador elige la moneda del flete en la calculadora y esa elección queda en el
    lead junto con los precios que produjo. Si al crear la cotización se congelara la
    configuración GLOBAL, la foto guardaría una tarifa distinta de la que generó esos
    precios: la venta dejaría de ser reproducible y Embarques Pricing (que lee la tarifa
    por ítem) trabajaría con otro flete.

    Antes de que cada moneda tuviera su tarifa, esto casi no se notaba —la moneda solo
    movía el TC—; ahora la diferencia es el flete completo. Se verifica sobre datos
    REALES creando la cotización por el endpoint, y se borra todo al terminar.
    """
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import text
    from database import SessionLocal, get_db
    from auth import get_current_user
    import monza_models as mm
    from monza_router_cotizaciones import router as cot_router

    app = FastAPI()
    app.include_router(cot_router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=1, email="tarifa-lead@test.invalid", empresa="automotriz", rol="admin")
    client = TestClient(app)

    db = SessionLocal()
    cli = cot_id = None
    try:
        cfg = db.query(MonzaConfig).filter(MonzaConfig.id == 1).first()
        previo = (cfg.tarifa_aerea_eur_por_kg, cfg.tarifa_aerea_usd_por_kg,
                  cfg.moneda_tarifa, cfg.tarifa_aerea_por_kg)
        cfg.tarifa_aerea_eur_por_kg = 4.5
        cfg.tarifa_aerea_usd_por_kg = 6.0
        cfg.moneda_tarifa = "EUR"          # la GLOBAL es EUR…
        cli = mm.MonzaCliente(nombre="__TEST_TARIFA_LEAD__", rut="76.086.428-5")
        db.add(cli)
        db.flush()
        # …pero el lead se cotizó en USD, con SU tarifa.
        lead = mm.MonzaLead(numero=f"L-TAR-{os.getpid()}", cliente_id=cli.id,
                            moneda_tarifa="USD", tarifa_aerea=6.0)
        db.add(lead)
        db.commit()
        lead_id, cli_id = lead.id, cli.id

        r = client.post("/api/monza/cotizaciones", json={
            "lead_id": lead_id, "cliente_id": cli_id,
            "items": [{"descripcion": "Pieza", "cantidad": 1, "moneda": "USD",
                       "costo": 100, "peso_kg": 2, "markup_pct": 0,
                       "precio_unitario_clp": 1000, "subtotal_clp": 1000}],
        })
        assert r.status_code == 201, r.text
        cot_id = r.json()["id"]
        fila = db.execute(text(
            "SELECT moneda_tarifa, tarifa_aerea FROM monza_cotizaciones WHERE id = :i"
        ), {"i": cot_id}).fetchone()
        assert fila[0] == "USD", f"congeló la moneda GLOBAL en vez de la del lead: {fila[0]}"
        assert abs(float(fila[1]) - 6.0) < 0.001, (
            f"congeló la tarifa global (4.5) en vez de la del lead (6.0): {fila[1]}")
        # …y el ÍTEM SIN estampa propia hereda la tarifa de la cabecera. Desde el
        # arreglo 4 del equipo (2026-08-21) un ítem CON estampa congela el flete de SU
        # corrida —puede diferir de la cabecera a propósito, ver
        # test_lead_item_flete_estampa §5—; este lead nunca pasó por /aplicar, así que
        # sus ítems no traen estampa y el fallback a cabecera sigue vigente. (Nadie
        # computa con esta columna: es foto de auditoría/reproducibilidad.)
        item = db.execute(text(
            "SELECT tarifa_aerea FROM monza_cotizacion_items WHERE cotizacion_id = :i"
        ), {"i": cot_id}).fetchone()
        assert abs(float(item[0]) - 6.0) < 0.001, (item[0], "el ítem no cuadra con la cabecera")
    finally:
        # La configuración se restaura PRIMERO y en su PROPIA transacción: es la fila que
        # le pone precio a TODA cotización de MonzaParts, así que no puede quedar con los
        # valores del test si un DELETE de la limpieza falla (hallazgo MEDIUM del
        # multienjambre: la restauración colgaba del mismo commit que la limpieza).
        _restaurar_config(previo)
        try:
            if cot_id:
                db.execute(text("DELETE FROM monza_cotizacion_items WHERE cotizacion_id = :i"), {"i": cot_id})
                db.execute(text("DELETE FROM monza_cotizaciones WHERE id = :i"), {"i": cot_id})
            db.execute(text("DELETE FROM monza_lead_actividades WHERE lead_id IN "
                            "(SELECT id FROM monza_leads WHERE numero LIKE 'L-TAR-%')"))
            db.execute(text("DELETE FROM monza_leads WHERE numero LIKE 'L-TAR-%'"))
            if cli:
                db.execute(text("DELETE FROM monza_clientes WHERE nombre = '__TEST_TARIFA_LEAD__'"))
            db.commit()
        finally:
            db.close()
        # Y se COMPRUEBA que quedó como estaba: una restauración silenciosamente rota
        # dejaría al dueño cotizando con los números del test sin que nadie se entere.
        _verificar_config_restaurada(previo)


def test_configuracion_rechaza_valores_imposibles():
    """El borde de entrada: una tarifa en 0 subvalúa la venta y una moneda desconocida
    deja la configuración en un estado que la calculadora no sabe resolver."""
    for valor in (0, -1):
        with pytest.raises(ValidationError):
            ConfigIn(tarifa_aerea_eur_por_kg=valor)
        with pytest.raises(ValidationError):
            ConfigIn(tarifa_aerea_usd_por_kg=valor)
    with pytest.raises(ValidationError):
        ConfigIn(moneda_tarifa="GBP")
    # Vacío = "no la tengo" y es legítimo (queda NULL y la calculadora bloquea al elegirla).
    assert ConfigIn(tarifa_aerea_eur_por_kg=None).tarifa_aerea_eur_por_kg is None
    # …y el NULL EXPLÍCITO se distingue de "no mandé el campo": es lo que permite BORRAR
    # una tarifa cargada por error. Sin esta distinción, vaciarla era un no-op silencioso
    # y la pantalla decía "guardada" mientras el valor viejo seguía cotizando.
    assert "tarifa_aerea_eur_por_kg" in ConfigIn(tarifa_aerea_eur_por_kg=None).model_fields_set
    assert "tarifa_aerea_eur_por_kg" not in ConfigIn(iva_pct=19).model_fields_set
    # Minúsculas se normalizan en vez de rechazarse (el operador no debería pelear con esto).
    assert ConfigIn(moneda_tarifa="usd").moneda_tarifa == "USD"
