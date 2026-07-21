"""Módulo aislado Recepción Nacional (camino físico de la compra nacional).

Paquete aditivo: NO modifica ninguna tabla existente. Su única conexión con el
código compartido es un UNION aditivo y direccionalmente seguro en
`routers/despachos.py::_qty_recibida_utilizable` (nuestro tope físico endurecido,
no el del programador).

Cuando un proveedor NACIONAL llega con su camión y su guía de despacho, Bodega
registra "cuánto llegó" por ítem. Ese acumulado por ítem BAJA el tope de Despachos
de "todo lo vendido" a "min(vendido, recibido)": no se puede despachar/facturar
más de lo que el proveedor entregó. A diferencia del embarque consolidado, NO
clona líneas ni fuerza reclamos: es un simple libro de recepciones sucesivas.

Para crear las tablas + la columna oc_proveedor.tipo_origen sin cablear nada:
`python -m recepcion_nacional.init_db` desde backend/.
"""
