"""Módulo aislado Recepción Nacional MonzaParts (camino físico de la compra nacional).

Espejo 1:1 de backend/recepcion_nacional/ (Grupo AM) sobre tablas monza_*.
Paquete aditivo: NO modifica ninguna tabla existente. Su única conexión con el
código compartido es un UNION aditivo y direccionalmente seguro en
`monza_router_despachos.py::_qty_recibida_utilizable` (nuestro tope físico
endurecido F2, no el del programador).

Cuando un proveedor NACIONAL llega con su camión y su guía de despacho, Bodega
registra "cuánto llegó" por ítem. Ese acumulado por ítem BAJA el tope de Despachos
de "todo lo vendido" a "min(vendido, recibido)": no se puede despachar/facturar
más de lo que el proveedor entregó. A diferencia del embarque consolidado, NO
clona líneas ni fuerza reclamos: es un simple libro de recepciones sucesivas.

ADAPTACIÓN ESTRUCTURAL Monza (verificada en monza_models.py:311): NO existe tabla
OcProveedorItem — el vínculo ítem↔OC es directo vía
MonzaCotizacionItem.oc_proveedor_id, así que la pertenencia se valida por esa
columna y la tabla de líneas NO lleva oc_proveedor_item_id.

Para crear las tablas + la columna monza_oc_proveedor.tipo_origen sin cablear nada:
`python -m monza_recepcion_nacional.init_db` desde backend/.
"""
