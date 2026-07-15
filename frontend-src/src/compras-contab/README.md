# Compras y Cuentas por Pagar — Frontend

Módulo **aislado** (mismo patrón que `src/embarques-pricing/`). NO modifica ningún
archivo existente. Página de Contabilidad → **Compras y Pagos**: registra compras
y gastos del día a día, los clasifica, lleva su condición/estado de pago, muestra
KPIs y antigüedad de cartera por pagar, y tiene una pestaña de **solo lectura** con
los costos de embarque ya cargados en Embarques Pricing.

## Archivos

| Archivo | Rol |
|---|---|
| `ComprasContabPage.tsx` | Página: KPIs + antigüedad + filtros + tabla + modales |
| `api.ts` | Cliente API (reusa la instancia axios compartida con auth/401) |
| `types.ts` | Tipos del JSON del backend |

## Activar (cuando se decida; toca 2 archivos existentes)

### `src/App.tsx`

Import (junto a los demás, ~línea 23):

```tsx
import ComprasContabPage from './compras-contab/ComprasContabPage'
```

Ruta (junto a `ventas-contab` / `embarques-pricing`, ~línea 88):

```tsx
<Route path="compras-contab" element={<ComprasContabPage />} />
```

### `src/pages/DashboardLayout.tsx`

1. Agregar `Wallet` al import de `lucide-react` (línea ~3-9).
2. Item en el grupo **Contabilidad** de `navGroups` (~línea 64-66):

```tsx
{ to: '/compras-contab', icon: Wallet, label: 'Compras y Pagos', exact: false },
```

3. Título en `pageLabels` (~línea 103):

```tsx
'/compras-contab': 'Compras y Cuentas por Pagar',
```

> El backend debe estar cableado (ver `backend/compras_contab/README.md`) y las
> tablas creadas (`python -m compras_contab.init_db`).

## Cómo deshacer

Revertir las líneas agregadas en `App.tsx` y `DashboardLayout.tsx`, y borrar la
carpeta `src/compras-contab/`.
