"""Portal de Clientes (Grupo AM / MachParts) — módulo aislado y aditivo.

Los contactos de procurement de un cliente entran por EL MISMO login que el personal,
pero con una cuenta `tipo_acceso='portal'`, y ven SOLO los pedidos de su(s) cliente(s).
Para el cliente es SOLO LECTURA: nada de lo que hace el portal escribe en las tablas del
negocio (lo único que escribe es lo suyo: su bitácora, sus fotos y la clave del usuario).

Qué vive acá (el detalle, en README.md):
  identidad      models.py, deps.py, entrada.py, service.py, rut.py (helpers de RUT),
                 sincronia.py, router.py, router_admin.py, init_db.py,
                 vincular_clientes.py, desactivar_cuentas.py
  pedidos        candado.py (de quién es una orden), visibles.py (qué documentos existen),
                 ordenes.py, renglones.py (los ítems por unidades), estados.py, tiempo.py,
                 valor.py y precios.py (los montos), busqueda.py, resumen.py, seguimiento.py
  documentos     documentos.py, dtes.py (lo que se lee de un DTE), descargas.py (los PDF
                 los baja el servidor)
  fotos          fotos.py, router_admin_fotos.py
  defensas       cupo.py (tope de frecuencia), auth.token_para (el token va atado a la
                 clave), deps.RutaDelPortal (503 si falta una tabla)
  rutas          router.py (identidad), router_ordenes.py (datos del cliente),
                 router_admin.py y router_admin_fotos.py (personal)
  pruebas        tests/ (suites, fábricas y las mutaciones con que se verificó cada una)
  este archivo   deja visible la bitácora del módulo (_hacer_visible_la_bitacora)

Tablas del núcleo de las que LEE: users, clientes, cotizaciones, items_cotizacion,
oc_cliente, despachos, despacho_items, embarques, recepciones, reclamos, compras al
proveedor, facturas y sus líneas, wasabil_dte. Tablas propias: usuario_cliente,
portal_acceso, foto_articulo, portal_descarga_log. Columnas que AGREGA al núcleo:
users.tipo_acceso y cotizaciones.cliente_id — ninguna otra.

El aislamiento tiene dos mitades y las dos son obligatorias:
  1) auth.get_current_user (la puerta de TODO el ERP) niega a las cuentas portal;
  2) este módulo entra por auth.get_usuario_autenticado + require_portal, y cada consulta
     filtra además por PortalScope.cliente_ids (doble candado, escrito una sola vez en
     candado.py).
Ver docs/fase-1-portal-identidad-2026-10-02.md y docs/fase-3-4-5-portal-pedidos-2026-10-04.md.
"""
import logging
import time


def _hacer_visible_la_bitacora() -> None:
    """Configura el logger 'portal' para que sus INFO lleguen al log del proceso.

    El backend no configura logging: un logger sin configurar hereda WARNING de la raíz
    y sus logger.info se descartan sin aviso. En este módulo esos INFO son la bitácora
    de quién creó, restableció, reactivó, desactivó o eliminó la cuenta de un cliente,
    y tienen que quedar en `pm2 logs machparts-api`.

    Se configura SOLO este logger (subir la raíz encendería además el INFO de httpx y
    de apscheduler) y acá, porque es lo primero que corre al importar cualquier archivo
    del paquete, sin depender del orden de los imports. La hora va en UTC, como las
    fechas que el backend guarda. `propagate` queda en True.

    Si algún día el backend configura logging para todos (basicConfig, --log-config),
    este manejador sobra: cada línea del portal saldría dos veces.
    """
    registro = logging.getLogger("portal")
    if not any(isinstance(m, logging.StreamHandler) for m in registro.handlers):
        formato = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%SZ")
        formato.converter = time.gmtime
        salida = logging.StreamHandler()  # stderr: donde ya salen los WARNING y los traceback
        salida.setFormatter(formato)
        registro.addHandler(salida)
    registro.setLevel(logging.INFO)


_hacer_visible_la_bitacora()
