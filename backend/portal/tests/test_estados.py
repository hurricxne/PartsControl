"""ESTADOS que ve el cliente — tabla de casos sobre funciones puras (sin base, sin HTTP).

LO QUE ESTA SUITE PROTEGE (portal/estados.py)
  §A  TRADUCCIÓN: cada estado interno del ítem, cruzado con el estado del embarque que lo
      trae, da el estado del cliente que corresponde. La tabla de casos de abajo ES la
      especificación: si cambia un estado del ERP, se cambia acá primero.
  §B  ANTE LO DESCONOCIDO, DE MENOS: un estado de ítem que el portal no conoce se
      muestra como el paso MENOS avanzado; un ítem embarcado con un embarque de estado
      desconocido (es un texto libre) se muestra «en tránsito», nunca «en bodega».
  §C  FACTURADO solo cambia la etiqueta de lo que ya salió: un ítem facturado por
      anticipo que todavía no sale sigue mostrando dónde va.
  §D  LÍNEA DE TIEMPO: un hito futuro NUNCA lleva fecha ni detalle; hay exactamente un
      hito «actual»; el recorrido corto no muestra tránsito ni aduana; una incidencia
      («en gestión», «no recibido») se agrega al final sin borrar lo que sí ocurrió; un
      estado desconocido no rompe nada.
  §E  NINGUNA ETIQUETA nombra al proveedor, un país, una ciudad ni una fecha estimada
      (y la sonda que lo revisa detecta de verdad esas palabras).
  §F  EL ORDEN: las incidencias van antes que todo el recorrido.
  §G  «NO RECIBIDO EN LA ENTREGA» dice el hecho y no promete nada: no es entregado, no es
      «por entregar», y su etiqueta no habla de reposición.

MUTACIONES verificadas al construir la suite (cambiar → rojo → restaurar → verde):
  el estado por defecto ante un valor desconocido (→ §B), el respaldo «en tránsito» del
  embarque desconocido (→ §B), la condición de 'facturado' (→ §C), la fecha de los
  hitos futuros, el detalle de la incidencia y el respaldo de una clave desconocida
  (→ §D), una etiqueta que dice «llegada estimada» (→ §E), la posición de «en gestión»
  (→ §F), y «por entregar» y la etiqueta de lo no recibido (→ §G).

Corre con:  ./venv/bin/python -m pytest portal/tests/test_estados.py -q
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import re  # noqa: E402

import pytest  # noqa: E402

from portal import estados as E  # noqa: E402

# (estado_item, estado_embarque, facturado) → clave que ve el cliente
TRADUCCION = [
    (("ingresado", None, False), "confirmado"),
    (("cerrado", None, False), "confirmado"),
    (("comprado", None, False), "en_compra"),
    (("preparado", None, False), "listo_origen"),
    (("pre_embarcado", None, False), "listo_origen"),
    (("embarcado", "en_bodega_proveedor", False), "listo_origen"),
    (("embarcado", "en_transito", False), "en_transito"),
    (("embarcado", "en_aduana", False), "en_aduana"),
    (("embarcado", "en_recepcion", False), "en_recepcion"),
    # El embarque llegó y Bodega todavía no cierra la recepción: el ítem sigue 'embarcado'.
    (("embarcado", "en_bodega", False), "en_recepcion"),
    (("en_bodega", "en_bodega", False), "en_bodega"),
    (("en_bodega", None, False), "en_bodega"),
    (("despachado", None, False), "despachado"),
    (("despachado", "en_bodega", True), "facturado"),
    (("reclamo_proveedor", None, False), "en_gestion"),
    (("reclamo_proveedor", "en_transito", True), "en_gestion"),
    # El estado del embarque solo importa mientras el ítem está 'embarcado'.
    (("comprado", "en_aduana", False), "en_compra"),
    (("despachado", "en_transito", False), "despachado"),
]


@pytest.mark.parametrize("entrada,esperado", TRADUCCION)
def test_a_traduccion(entrada, esperado):
    estado = E.estado_cliente(*entrada)
    assert estado.clave == esperado
    assert estado.etiqueta == E.ETIQUETAS[esperado]
    assert estado.entregado == (esperado in ("despachado", "facturado"))


@pytest.mark.parametrize("estado_item", [None, "", "estado_nuevo_del_erp", "DESPACHADO", " despachado"])
def test_b_estado_de_item_desconocido_es_el_menos_avanzado(estado_item):
    estado = E.estado_cliente(estado_item, "en_bodega", True)
    assert estado.clave == "confirmado"
    assert not estado.entregado


@pytest.mark.parametrize("estado_embarque", [None, "", "despachado", "algo_nuevo", "EN_ADUANA"])
def test_b_embarque_desconocido_es_en_transito(estado_embarque):
    assert E.estado_cliente("embarcado", estado_embarque).clave == "en_transito"


@pytest.mark.parametrize("estado_item,embarque", [
    ("cerrado", None), ("comprado", None), ("embarcado", "en_transito"), ("en_bodega", None),
    ("reclamo_proveedor", None),
])
def test_c_facturado_no_adelanta_un_item_que_no_ha_salido(estado_item, embarque):
    con, sin = E.estado_cliente(estado_item, embarque, True), E.estado_cliente(estado_item, embarque, False)
    assert con == sin
    assert not con.entregado


def _hitos(clave, fechas=None, detalles=None, con_embarque=True):
    return E.linea_de_tiempo(clave, fechas or {}, detalles or {}, con_embarque)


INCIDENCIAS = [E.EN_GESTION, E.NO_RECIBIDO]
TODAS_LAS_FECHAS = {clave: "2026-01-15" for clave in list(E.ORDEN) + INCIDENCIAS}
TODOS_LOS_DETALLES = {clave: "detalle" for clave in list(E.ORDEN) + INCIDENCIAS}


@pytest.mark.parametrize("clave", list(E.ORDEN))
def test_d_un_hito_futuro_no_lleva_fecha_ni_detalle(clave):
    hitos = _hitos(clave, TODAS_LAS_FECHAS, TODOS_LOS_DETALLES)
    futuros = [h for h in hitos if not h.cumplido]
    assert all(h.fecha is None and h.detalle is None for h in futuros), futuros
    cumplidos = [h for h in hitos if h.cumplido]
    assert all(h.fecha == "2026-01-15" for h in cumplidos)


@pytest.mark.parametrize("clave", list(E.ORDEN) + INCIDENCIAS)
@pytest.mark.parametrize("con_embarque", [True, False])
def test_d_hay_exactamente_un_hito_actual_y_es_el_estado(clave, con_embarque):
    hitos = _hitos(clave, {"en_compra": "2026-01-10"}, con_embarque=con_embarque)
    actuales = [h for h in hitos if h.actual]
    assert [h.clave for h in actuales] == [clave]
    assert actuales[0].cumplido
    # Lo cumplido va antes que lo pendiente: el recorrido no tiene huecos.
    marcas = [h.cumplido for h in hitos if h.clave not in INCIDENCIAS]
    assert marcas == sorted(marcas, reverse=True)


def test_d_compra_nacional_no_muestra_transito_ni_aduana():
    claves = [h.clave for h in _hitos("en_bodega", con_embarque=False)]
    assert claves == ["confirmado", "en_compra", "en_bodega", "despachado"]
    # Con embarque, el recorrido completo (sin 'facturado': no se anuncia una factura).
    claves = [h.clave for h in _hitos("en_bodega", con_embarque=True)]
    assert claves == ["confirmado", "en_compra", "listo_origen", "en_transito", "en_aduana",
                      "en_recepcion", "en_bodega", "despachado"]


def test_d_facturado_es_un_paso_solo_cuando_ocurrio():
    assert "facturado" not in [h.clave for h in _hitos("despachado")]
    assert "facturado" in [h.clave for h in _hitos("facturado")]
    # Facturado por adelantado y todavía en bodega: el paso existe (hay fecha) pero es futuro
    # en el recorrido, así que no muestra su fecha.
    hitos = {h.clave: h for h in _hitos("en_bodega", {"facturado": "2026-02-01"})}
    assert "facturado" in hitos and not hitos["facturado"].cumplido and hitos["facturado"].fecha is None


def test_d_en_gestion_conserva_lo_que_si_ocurrio():
    hitos = _hitos(E.EN_GESTION, {"confirmado": "2026-01-01", "en_compra": "2026-01-05",
                                  "en_transito": "2026-01-20", E.EN_GESTION: "2026-02-01"})
    por_clave = {h.clave: h for h in hitos}
    assert hitos[-1].clave == E.EN_GESTION and hitos[-1].actual and hitos[-1].fecha == "2026-02-01"
    assert por_clave["en_transito"].cumplido and por_clave["en_transito"].fecha == "2026-01-20"
    assert not por_clave["en_bodega"].cumplido and not por_clave["despachado"].cumplido
    # Sin ninguna fecha registrada, lo único cierto es que el pedido se confirmó.
    solo = {h.clave: h for h in _hitos(E.EN_GESTION)}
    assert solo["confirmado"].cumplido and not solo["en_compra"].cumplido


def test_d_no_recibido_conserva_el_recorrido_y_lleva_su_fecha_y_su_guia():
    hitos = _hitos(E.NO_RECIBIDO, {"confirmado": "2026-01-01", "despachado": "2026-03-01", E.NO_RECIBIDO: "2026-03-05"},
                   {"despachado": "Guía 7021", E.NO_RECIBIDO: "Guía 7021"}, con_embarque=False)
    assert [h.clave for h in hitos] == ["confirmado", "en_compra", "en_bodega", "despachado", E.NO_RECIBIDO]
    ultimo, despachado = hitos[-1], hitos[-2]
    assert (ultimo.actual, ultimo.cumplido, ultimo.fecha, ultimo.detalle) == (True, True, "2026-03-05", "Guía 7021")
    assert ultimo.etiqueta == E.ETIQUETAS[E.NO_RECIBIDO]
    assert despachado.cumplido and not despachado.actual and despachado.fecha == "2026-03-01"
    # La incidencia «en gestión» no lleva detalle si nadie se lo dio.
    assert _hitos(E.EN_GESTION, {E.EN_GESTION: "2026-02-01"})[-1].detalle is None


@pytest.mark.parametrize("clave", ["", "estado_que_no_existe", "DESPACHADO"])
def test_d_una_clave_desconocida_se_trata_como_el_paso_menos_avanzado(clave):
    hitos = _hitos(clave, TODAS_LAS_FECHAS, TODOS_LOS_DETALLES)
    assert [h.clave for h in hitos if h.actual] == ["confirmado"]
    assert [h.clave for h in hitos if h.cumplido] == ["confirmado"]


# Lo que una etiqueta no puede decir. Las RAÍCES calzan con cualquier palabra que
# empiece así («proveedor», «proveedores»; «estimada», «estimado»); las PALABRAS, solo
# enteras («eta» no puede vetar «etapa»).
RAICES_PROHIBIDAS = ("proveedor", "estimad", "forwarder", "miami", "china", "aprox")
PALABRAS_PROHIBIDAS = ("usa", "eta", "awb", "eeuu")
FRASES_PROHIBIDAS = ("estados unidos", "embarque n")


def _dice_algo_prohibido(etiqueta: str) -> bool:
    baja = etiqueta.lower()
    palabras = re.findall(r"[a-záéíóúüñ]+", baja)
    return (any(palabra.startswith(raiz) for palabra in palabras for raiz in RAICES_PROHIBIDAS)
            or any(palabra in PALABRAS_PROHIBIDAS for palabra in palabras)
            or any(frase in baja for frase in FRASES_PROHIBIDAS)
            or any(c.isdigit() for c in etiqueta))


@pytest.mark.parametrize("frase", [
    "Fecha estimada de llegada", "En tránsito, llegada estimada pronto", "En gestión con el proveedor.",
    "Con nuestros proveedores", "En tránsito desde Miami,", "ETA: próxima semana",
    "En tránsito (forwarder)", "Llega el 12 de mayo", "Viene de Estados Unidos", "Aprox. en dos semanas",
])
def test_e_la_sonda_detecta_lo_prohibido(frase):
    assert _dice_algo_prohibido(frase), frase


@pytest.mark.parametrize("frase", ["En una etapa posterior", "En tránsito", "Despachado y facturado"])
def test_e_la_sonda_no_veta_de_mas(frase):
    assert not _dice_algo_prohibido(frase), frase


def test_e_ninguna_etiqueta_revela_la_cadena_de_suministro():
    for clave, etiqueta in E.ETIQUETAS.items():
        assert not _dice_algo_prohibido(etiqueta), (clave, etiqueta)
    # Todo estado posible tiene etiqueta y lugar en el orden (o es la incidencia).
    for _, esperado in TRADUCCION:
        assert esperado in E.ETIQUETAS
        assert esperado in E.ORDEN or esperado == E.EN_GESTION


def test_f_las_incidencias_van_antes_que_todo_el_recorrido():
    claves = sorted(list(E.ORDEN) + [E.NO_RECIBIDO, E.EN_GESTION], key=E.posicion)
    assert claves[:2] == [E.EN_GESTION, E.NO_RECIBIDO]
    assert claves[2:] == list(E.ORDEN)


def test_g_no_recibido_dice_el_hecho_y_no_promete_nada():
    estado = E.no_recibido()
    assert (estado.clave, estado.entregado, estado.por_entregar) == (E.NO_RECIBIDO, False, False)
    assert "reposici" not in estado.etiqueta.lower() and "gesti" not in estado.etiqueta.lower()
    assert not _dice_algo_prohibido(estado.etiqueta)


@pytest.mark.parametrize("entrada,esperado", TRADUCCION)
def test_g_por_entregar_es_todo_lo_que_no_esta_entregado_ni_es_no_recibido(entrada, esperado):
    estado = E.estado_cliente(*entrada)
    assert estado.por_entregar == (esperado not in ("despachado", "facturado"))
