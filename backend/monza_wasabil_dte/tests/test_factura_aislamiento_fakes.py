"""Fase 6 — AISLAMIENTO de los fakes de Wasabil entre MonzaParts y Grupo AM.

Las dos marcas tienen su PROPIO módulo client (`monza_wasabil_dte.client` y
`wasabil_dte.client`) con su propio token en backend/.env (WASABIL_API_TOKEN_MONZA vs
WASABIL_API_TOKEN). Esa duplicación deliberada es también la superficie de monkeypatch
de los tests: cuando pytest corre la batería COMPLETA en un solo proceso, las suites de
Monza y las de Grupo AM instalan sus fakes a nivel de módulo y no deben pisarse.

Si se pisaran, el daño no sería un test rojo sino algo peor: una suite de Monza podría
terminar hablando con el client de GA — el que tiene el token de PRODUCCIÓN de Grupo AM
— y emitir documentos tributarios REALES. Por eso esto se prueba explícitamente.

Esta suite NO toca la BD, NO hace red y NO lee NINGÚN token: solo compara la IDENTIDAD
de las funciones de cada módulo (`is`) y el nombre de la variable de entorno.

Corre con:  ./venv/bin/python -m pytest monza_wasabil_dte/tests/test_factura_aislamiento_fakes.py -q
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import wasabil_dte.client as ga_client            # noqa: E402
import monza_wasabil_dte.client as monza_client   # noqa: E402
from monza_wasabil_dte.tests.factura_harness import Checker, FakeWasabil  # noqa: E402

check = Checker()

# Las cuatro funciones que TODOS los fakes de ambas marcas pisan.
SUPERFICIE = ("crear_documento", "estado_documento", "obtener_documento",
              "buscar_cliente_por_rut", "buscar_documentos", "esta_configurado")


def _snapshot(mod):
    return {n: getattr(mod, n) for n in SUPERFICIE}


def _restaurar(mod, snap):
    for n, f in snap.items():
        setattr(mod, n, f)


def run():
    check("los dos clients son módulos DISTINTOS", ga_client is not monza_client)
    check("cada marca lee su PROPIO token (nombres distintos en backend/.env; "
          "el VALOR no se lee nunca)",
          "WASABIL_API_TOKEN_MONZA" in (monza_client.__doc__ or "")
          and "WASABIL_API_TOKEN_MONZA" not in (ga_client.__doc__ or ""))

    # Los helpers PRIVADOS no los pisa ningún fake: son la prueba de que cada módulo
    # tiene su propia implementación real detrás (con su propio token).
    check("los transportes reales de cada marca son objetos distintos",
          ga_client._request is not monza_client._request
          and ga_client._headers is not monza_client._headers)

    ga_original = _snapshot(ga_client)
    monza_original = _snapshot(monza_client)
    # OJO: NO se exige un punto de partida "limpio". pytest importa TODOS los módulos
    # de test durante la colección, así que las suites de Grupo AM ya pueden haber
    # instalado sus fakes antes de que esta corra. Lo que se verifica es el invariante
    # que de verdad importa y que vale con o sin fakes previos: NINGUNA función está
    # compartida entre los dos clients.
    check("ninguna función de la superficie está COMPARTIDA entre ambos clients",
          all(getattr(ga_client, n) is not getattr(monza_client, n) for n in SUPERFICIE),
          [n for n in SUPERFICIE if getattr(ga_client, n) is getattr(monza_client, n)])

    try:
        # ── 1. Instalar un fake MONZA no toca a Grupo AM ─────────────────────────
        fake = FakeWasabil("__AISLAMIENTO__")
        fake.install()
        check("fake de Monza instalado: el client Monza quedó pisado",
              all(getattr(monza_client, n) is not monza_original[n] for n in SUPERFICIE))
        check("fake de Monza NO tocó NINGUNA función del client de Grupo AM",
              all(getattr(ga_client, n) is ga_original[n] for n in SUPERFICIE),
              [n for n in SUPERFICIE if getattr(ga_client, n) is not ga_original[n]])

        # ── 2. Y al revés: un fake de Grupo AM no toca a Monza ───────────────────
        monza_con_fake = _snapshot(monza_client)
        centinela = lambda *a, **k: {"uuid": "GA-CENTINELA"}  # noqa: E731
        for n in SUPERFICIE:
            setattr(ga_client, n, centinela)
        check("fake de Grupo AM NO tocó NINGUNA función del client de Monza",
              all(getattr(monza_client, n) is monza_con_fake[n] for n in SUPERFICIE),
              [n for n in SUPERFICIE if getattr(monza_client, n) is not monza_con_fake[n]])
        check("el fake de Monza sigue respondiendo lo suyo (no el centinela de GA)",
              monza_client.crear_documento({"x": 1})["uuid"].startswith("uuid-f"),
              monza_client.crear_documento({"x": 1}))
    finally:
        # Restauración EXACTA: si esta suite corre antes que las de Grupo AM, dejarles
        # el client con un centinela las haría fallar de forma incomprensible.
        _restaurar(ga_client, ga_original)
        _restaurar(monza_client, monza_original)

    check("al terminar, AMBOS clients quedan restaurados a sus funciones reales",
          all(getattr(ga_client, n) is ga_original[n] for n in SUPERFICIE)
          and all(getattr(monza_client, n) is monza_original[n] for n in SUPERFICIE))

    check.finish()


def test_monza_factura_aislamiento_fakes():
    run()


if __name__ == "__main__":
    run()
