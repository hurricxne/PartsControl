# Embarques Pricing (Costo Landed) — Frontend

Página real de Contabilidad → **Embarques Pricing**. Reemplaza el mockup de
`src/pages/EmbarquesPricingPage.tsx` (que queda intacto, sin usar).

## Archivos

| Archivo | Rol |
|---|---|
| `EmbarquesPricingPage.tsx` | Página: lista de embarques + editor de pricing por embarque |
| `api.ts` | Cliente API (reusa la instancia axios compartida con auth/401) |
| `compute.ts` | Cálculo landed en el cliente para **vista previa en vivo** (espeja `backend/embarques_pricing/service.py`) |
| `types.ts` | Tipos del JSON del backend |

## Flujo de usuario

1. Se listan **todos** los embarques creados por Logística (correlativo, badge de
   tipo y de estado de pricing, e indicador de documentos `n/5 docs`). El embarque
   aparece en la lista apenas Logística lo crea (estado `sin pricing`); el registro
   de pricing se crea de forma diferida la primera vez que Contabilidad lo abre.
2. Al expandir uno se ve, arriba, la franja de **Documentos del embarque** (AWB,
   factura comercial, packing list, certificado de origen, otros) con estado
   presente/faltante — solo lectura (los sube Logística).
3. Se edita TC, moneda, **tipo de flete** (CLP local o prepagado por el proveedor
   en ME, con pista por tipo de embarque) y la tabla de **gastos locales** (6
   líneas predeterminadas fijas): Monto Neto, IVA, Total Bruto, N° Factura,
   **Fecha de factura** y **Banco** por línea. El **arancel** capitaliza pero sin
   IVA. El FOB por ítem viene de la factura del proveedor y se puede ajustar a
   mano (botón ↺ para volver al valor de la factura).
4. La tabla de ítems muestra el costo landed **al instante** mientras se edita
   (shipping prorrateado por peso, gastos por CIF).
5. **Guardar y calcular** persiste; **Cerrar** congela el costo; **Reabrir** lo
   habilita de nuevo.

## Cómo deshacer

En `src/App.tsx`, cambiar el import de `EmbarquesPricingPage` de
`./embarques-pricing/EmbarquesPricingPage` de vuelta a `./pages/EmbarquesPricingPage`,
y borrar esta carpeta `src/embarques-pricing/`.

> Nota: `compute.ts` duplica a propósito la fórmula del backend (preview cliente +
> verdad en el servidor). Si cambia la fórmula en `service.py`, actualizar ambos.
