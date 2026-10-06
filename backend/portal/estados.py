"""El estado que ve el cliente, traducido del que ve el personal. Funciones PURAS: no
tocan la base (se prueban con una tabla de casos en portal/tests/test_estados.py).

DE DÓNDE SALE
    Son dos máquinas de estado que corren en paralelo, y el cliente ve su cruce:
      · el ÍTEM (`items_cotizacion.estado_item`): ingresado, cerrado, comprado,
        preparado, pre_embarcado, embarcado, en_bodega, despachado, reclamo_proveedor;
      · el EMBARQUE que lo trae (`embarques.estado`), un String libre y sin validación:
        en_bodega_proveedor, en_transito, en_aduana y en_bodega se eligen a mano en la
        pantalla de Embarques; 'en_recepcion' lo escribe Bodega al iniciar la recepción;
        'despachado' lo pone Despachos cuando salió todo el embarque (a esa altura
        ningún ítem suyo sigue 'embarcado', así que acá no se mira).
    Mientras el ítem está 'embarcado', el estado del embarque es el que dice DÓNDE viene.
    El desfase entre las dos máquinas es información real: un embarque 'en_bodega' con el
    ítem todavía 'embarcado' significa "llegó, lo estamos recibiendo" (el ítem pasa a
    'en_bodega' recién cuando Bodega cierra la recepción).

QUÉ NO DICE NUNCA UN ESTADO
    Ni el proveedor, ni el país o la ciudad de origen, ni el consolidador, ni una fecha
    estimada de llegada. El cliente ve DÓNDE va lo suyo y la fecha que se le comprometió;
    la ruta y los plazos del proveedor son información interna.

ANTE UN VALOR DESCONOCIDO se responde el estado MENOS avanzado ('confirmado'): el portal
puede decir de menos, nunca prometer un avance que no ocurrió.

DOS INCIDENCIAS, FUERA DEL RECORRIDO (no son pasos: se agregan al final de la línea de
tiempo, sin borrar lo que sí ocurrió)
  · 'en_gestion'   el ítem tiene un reclamo al proveedor (no vino en el envío o llegó
                   dañado). Sigue pendiente: se espera bajo esta misma orden.
  · 'no_recibido'  unidades que salieron en una guía y que el cliente, al firmarla,
                   declaró NO haber recibido. El ERP no las repone por esta venta (la
                   reposición se vende en una cotización nueva: routers/despachos.py::
                   firmar_despacho), así que el portal dice el hecho y no promete nada:
                   no cuentan como entregadas ni como «por entregar».
"""
from dataclasses import dataclass
from typing import Dict, List, Optional

# (clave, etiqueta para el cliente), en el orden del recorrido
_CATALOGO = (
    ("confirmado",   "Pedido confirmado"),
    ("en_compra",    "En proceso de compra"),
    ("listo_origen", "Listo en origen"),
    ("en_transito",  "En tránsito"),
    ("en_aduana",    "En internación aduanera"),
    ("en_recepcion", "Llegó a Chile, en recepción"),
    ("en_bodega",    "En nuestra bodega"),
    ("despachado",   "Despachado"),
    ("facturado",    "Despachado y facturado"),
)
# Fuera del recorrido lineal: no son pasos, son incidencias (ver el encabezado).
EN_GESTION = "en_gestion"
NO_RECIBIDO = "no_recibido"
_INCIDENCIAS = {
    # Neutra a propósito: el ítem llega acá tanto si no vino en el envío como si llegó
    # dañado. No nombra al proveedor ni promete una fecha nueva.
    EN_GESTION: "En gestión de reposición",
    # Dice lo que el cliente firmó, y nada más.
    NO_RECIBIDO: "No recibido en la entrega",
}

ORDEN: Dict[str, int] = {clave: i for i, (clave, _) in enumerate(_CATALOGO)}
ETIQUETAS: Dict[str, str] = {**dict(_CATALOGO), **_INCIDENCIAS}
# Las incidencias van ANTES que todo el recorrido al ordenar: es lo que más le preocupa
# al cliente y lo primero que tiene que ver.
_POSICION: Dict[str, int] = {**ORDEN, EN_GESTION: -2, NO_RECIBIDO: -1}

# Lo que el portal cuenta como "entregado": la mercadería ya salió hacia el cliente en un
# despacho CERRADO. No implica guía firmada: lo que el cliente firmó como recibido es otro
# dato (la recepción de la guía, portal/documentos.py), y las unidades que firmó como NO
# recibidas dejan de contarse acá (portal/renglones.py::_partes_de_fila).
ENTREGADOS = frozenset({"despachado", "facturado"})
# Pasos que solo existen cuando el ítem viaja en un embarque (compra internacional).
_PASOS_DE_EMBARQUE = frozenset({"listo_origen", "en_transito", "en_aduana", "en_recepcion"})

_POR_ESTADO_ITEM = {
    "ingresado": "confirmado",
    "cerrado": "confirmado",
    "comprado": "en_compra",
    "preparado": "listo_origen",
    "pre_embarcado": "listo_origen",
    "en_bodega": "en_bodega",
    "despachado": "despachado",
    "reclamo_proveedor": EN_GESTION,
}
_POR_ESTADO_EMBARQUE = {
    "en_bodega_proveedor": "listo_origen",
    "en_transito": "en_transito",
    "en_aduana": "en_aduana",
    # Bodega ya abrió la recepción, o el embarque se marcó llegado y el ítem todavía no
    # pasa a 'en_bodega' (eso ocurre al CERRAR la recepción): las dos son "llegó, lo
    # estamos recibiendo".
    "en_recepcion": "en_recepcion",
    "en_bodega": "en_recepcion",
}


def posicion(clave: str) -> int:
    """Lugar de un estado para ordenar: primero las incidencias, después el recorrido."""
    return _POSICION.get(clave, -1)


@dataclass(frozen=True)
class EstadoCliente:
    clave: str
    etiqueta: str
    entregado: bool

    @property
    def por_entregar(self) -> bool:
        """Las unidades en este estado todavía se esperan bajo esta orden. Falso para lo
        ya entregado y para lo que el cliente firmó como no recibido."""
        return not self.entregado and self.clave != NO_RECIBIDO


def no_recibido() -> EstadoCliente:
    """El estado de las unidades que el cliente firmó como NO recibidas."""
    return EstadoCliente(clave=NO_RECIBIDO, etiqueta=ETIQUETAS[NO_RECIBIDO], entregado=False)


def estado_cliente(
    estado_item: Optional[str], estado_embarque: Optional[str] = None, facturado: bool = False,
) -> EstadoCliente:
    """El estado de UN ítem (una fila de items_cotizacion) como lo ve el cliente.

    `estado_embarque` es el del embarque que trae el ítem (None si no tiene). Solo se
    mira cuando el ítem está 'embarcado'. `facturado` = el ítem ya figura en una factura;
    solo cambia la etiqueta de un ítem despachado (un ítem facturado por anticipo que
    todavía no sale sigue mostrando dónde va, que es lo que el cliente quiere saber).
    """
    if estado_item == "embarcado":
        # Embarcado con un estado de embarque desconocido o sin embarque: está en camino.
        clave = _POR_ESTADO_EMBARQUE.get(estado_embarque or "", "en_transito")
    else:
        clave = _POR_ESTADO_ITEM.get(estado_item or "", "confirmado")
    if clave == "despachado" and facturado:
        clave = "facturado"
    return EstadoCliente(clave=clave, etiqueta=ETIQUETAS[clave], entregado=clave in ENTREGADOS)


@dataclass(frozen=True)
class Hito:
    clave: str
    etiqueta: str
    cumplido: bool
    actual: bool
    fecha: Optional[str]      # ISO (AAAA-MM-DD) de cuándo OCURRIÓ; None si no quedó registrada
    detalle: Optional[str]    # p. ej. "Guía 1284" — nunca datos del proveedor


def linea_de_tiempo(
    clave_actual: str, fechas: Dict[str, Optional[str]], detalles: Dict[str, str],
    con_embarque: bool,
) -> List[Hito]:
    """Los pasos del recorrido de un ítem cuyo estado es `clave_actual`, con cuáles ya
    ocurrieron.

    · Un hito CUMPLIDO lleva su fecha real si quedó registrada; un hito FUTURO no lleva
      fecha jamás (el portal no estima cuándo llega algo).
    · `con_embarque=False` (compra nacional: no hay tránsito internacional ni aduana)
      omite esos pasos en vez de mostrarlos como "saltados".
    · Un ítem con una incidencia ('en gestión', 'no recibido') muestra el recorrido
      hasta donde llegó de verdad (lo dicen las fechas registradas) y agrega la
      incidencia como paso actual, con su fecha y su detalle si quedaron registrados.
    · Una clave que no es del catálogo se trata como 'confirmado' (ver «ANTE UN VALOR
      DESCONOCIDO» en el encabezado).
    """
    claves = [c for c, _ in _CATALOGO]
    incidencia = clave_actual if clave_actual in _INCIDENCIAS else None
    if incidencia is None and clave_actual not in ORDEN:
        clave_actual = "confirmado"
    # Si el estado actual ES uno de los pasos de embarque, se muestran aunque no se haya
    # encontrado el embarque: omitirlos dejaría la línea de tiempo sin paso actual.
    if not con_embarque and clave_actual not in _PASOS_DE_EMBARQUE:
        claves = [c for c in claves if c not in _PASOS_DE_EMBARQUE]
    # 'facturado' es un paso solo cuando ocurrió: no se le anuncia al cliente una factura.
    if clave_actual != "facturado" and not fechas.get("facturado"):
        claves = [c for c in claves if c != "facturado"]

    if incidencia:
        alcanzado = max((ORDEN[c] for c in claves if fechas.get(c)), default=ORDEN["confirmado"])
    else:
        alcanzado = ORDEN[clave_actual]

    hitos = [
        Hito(
            clave=c, etiqueta=ETIQUETAS[c],
            cumplido=ORDEN[c] <= alcanzado,
            actual=(ORDEN[c] == alcanzado and not incidencia),
            fecha=fechas.get(c) if ORDEN[c] <= alcanzado else None,
            detalle=detalles.get(c) if ORDEN[c] <= alcanzado else None,
        )
        for c in claves
    ]
    if incidencia:
        hitos.append(Hito(clave=incidencia, etiqueta=ETIQUETAS[incidencia], cumplido=True,
                          actual=True, fecha=fechas.get(incidencia), detalle=detalles.get(incidencia)))
    return hitos
