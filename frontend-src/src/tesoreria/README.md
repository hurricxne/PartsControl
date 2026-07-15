# Tesorería (frontend, Grupo AM)

Módulo aislado (carpeta propia, sin tocar código compartido). Evolución de la antigua
Conciliación Bancaria: Tesorería revisa, aprueba y concilia lo que otros módulos registran.

- `TesoreriaPage.tsx` — página con 5 pestañas:
  - **Conciliar**: cargos y abonos pendientes de la cartola, con sugerencias automáticas
    (cargo → egresos de Compras; abono → cobranzas de Facturas) y búsqueda manual.
  - **Por pagar**: cola de aprobación de pagos (compras con saldo, por vencimiento, con
    buckets vencido/0-7/8-30/31-60/61+). Selección múltiple → "Aprobar pago" crea el
    Comprobante de Egreso (misma regla de negocio que Compras).
  - **Flujo de caja**: proyección NIC 7 por buckets (por cobrar / por pagar / neto +
    retenciones de factoring).
  - **Movimientos**: historial con estado y destino conciliado; desconciliar/eliminar.
  - **Cuentas**: catálogo de cuentas bancarias.
- `api.ts` — cliente axios (endpoints `/api/tesoreria/*`).
- `types.ts` — tipos espejo del JSON del backend (`backend/tesoreria/`).

Ruta: `/tesoreria` (menú Contabilidad → Tesorería). La ruta antigua `/conciliacion`
redirige aquí.
