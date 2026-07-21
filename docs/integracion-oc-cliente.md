# Integración — N° OC del cliente obligatorio + edición ex-post de la OC

Rama: `feature/oc-cliente-obligatoria` (encima de `feature/facturas-emision-endurecida`).
Solo **core / empresa `mineria` (Grupo AM)**; nada de MonzaParts. Origen: paquete de
cambios revisado del handoff `occlientechanges.patch`, integrado como código de primera
clase (aplicado, adaptado al estado actual del repo y re-revisado con enjambre).

## Por qué

En *Cierre de Venta* se cerró una venta sin registrar el **N° OC del cliente** y no
había forma de agregarlo después. El registro `oc_cliente` es la base del post-venta
(Compras → Despachos → Contabilidad → guía SII → factura): una OC sin número queda
incompleta en todos los módulos (la guía electrónica, por ejemplo, EXIGE la referencia
801 con el N° y la fecha de la OC).

Dos cambios:
1. **Bloqueo duro**: no se puede cerrar la venta sin N° OC del cliente (frontend + 400 backend).
2. **Edición ex-post**: la OC (N° OC, Fecha OC, Cond. de pago, Fecha de entrega, Asesor)
   se puede corregir desde **Ventas** y **Ventas — Contabilidad**, con candado de rol
   preparado para el futuro módulo de Usuarios.

## Qué cambia

**Backend**
| Archivo | Cambio |
|---|---|
| `backend/role_guard.py` *(nuevo)* | `require_rol(*allowed)`, gemelo de `empresa_guard.py`. **Permisivo hoy** (el modelo `User` aún no tiene `rol`); su docstring lleva el checklist de activación futura. |
| `backend/routers/auth.py` | Nuevo `GET /auth/users` (usuarios activos de la misma empresa). El frontend ya lo llamaba (`authAPI.users()`) pero no existía: el selector de asesor estaba roto y `OcCliente.asesor_id` quedaba siempre NULL. |
| `backend/routers/compras.py` | (a) `crear_oc_cliente`: `400` si `numero_oc` viene vacío, e **idempotente por cotización** (si la OC ya existe devuelve la misma en vez de duplicarla — permite reintentar el cierre cuando falló el paso de avance de fase). (b) Schema `OcClienteUpdate` + `PUT /oc-cliente/{oc_id}` (patrón `if x is not None` para editar solo lo enviado; `asesor_id` distingue "no enviado" de "null explícito" vía `model_fields_set` para poder **desasignar** el asesor), candado `require_empresa("mineria")` + `require_rol("comercial","contabilidad","admin")`. (c) Guards del PUT tras la revisión enjambre: `asesor_id` debe ser un usuario activo de la misma empresa (mismo criterio que `GET /auth/users`); y si la OC ya tiene **guía SII 52 viva** (emitida o en emisión), el N° y la fecha de la OC NO se editan (409 — la referencia 801 quedó ante el SII); los demás campos sí. |
| `backend/routers/ventas.py` | `_build_venta` expone `oc_cliente_id` y `asesor_id`; el asesor de la OC prevalece sobre el creador de la cotización. |
| `backend/routers/contabilidad.py` | `detalle_venta` devuelve `asesor_id` (precarga del modal). |
| `backend/routers/tests/test_oc_cliente.py` *(nuevo)* | Checks de integración: POST sin N° → 400, POST con N° → 201, PUT persiste los 5 campos, vaciar N° → 400, asesor inexistente / de otra empresa → 400, N°/fecha con guía SII emitida → 409 (cond_pago sí se edita), guard de rol (permisivo / 403 / permitido). Recolectable por pytest y ejecutable directo. |

**Frontend**
| Archivo | Cambio |
|---|---|
| `frontend-src/src/components/OcClienteEditModal.tsx` *(nuevo)* | Modal reutilizable de edición de la OC (mismo patrón de modales del repo: backdrop con blur, cierre por clic afuera, toasts); selector de asesor vía `authAPI.users()`. Normaliza `fecha_oc` histórica no-ISO (10/06/2026 → 2026-06-10) para que el `<input type="date">` la muestre, e invalida el caché react-query de Despachos al guardar (el N°/fecha nuevos se ven al tiro). |
| `frontend-src/src/services/api.ts` | `comprasAPI.actualizarOcCliente(id, data)`. |
| `frontend-src/src/stores/authStore.ts` | `User.rol?: string` (forward-compat; hoy `undefined`). |
| `frontend-src/src/pages/CierreVentaPage.tsx` | Bloqueo duro: validación en `handleCerrar`, asterisco rojo y botón deshabilitado sin N° OC. |
| `frontend-src/src/pages/VentasPage.tsx` / `VentasContabPage.tsx` | Botón "Editar OC" + modal (aparece incluso si la OC quedó sin número, con hint "Sin N° OC registrado"); refrescan detalle y lista al guardar. |

## Decisiones

- **Bloqueo del cierre: duro.** Sin N° OC no se cierra (los datos históricos con
  `numero_oc` NULL siguen visibles y se completan con "Editar OC"; sin backfill).
- **Candado de rol: preparado pero permisivo.** Hoy no existe sistema de roles
  (`User` solo tiene `empresa`). `require_rol` y el gate del frontend
  (`ROLES_EDITAN_OC`) quedan escritos y se activan solos cuando exista `User.rol`
  — checklist completo en el docstring de `backend/role_guard.py`.

## Verificación

- **Backend**: `cd backend && ./venv/bin/python routers/tests/test_oc_cliente.py`
  (también corre bajo pytest con el resto de las suites).
- **Frontend**: `cd frontend-src && npm run build`.
- **Manual**: (1) *Cierre de Venta* sin N° OC → botón deshabilitado; con N° OC →
  cierra normal. (2) */ventas* → expandir → **Editar OC** → cambiar N° / asesor /
  fecha → Guardar → la tarjeta refleja el cambio. (3) */ventas-contab* → igual,
  dentro del detalle expandido.

## Notas de deploy

- **No requiere migración de esquema** (los 5 campos de `oc_cliente` ya existen).
- Solo core / minería; el curado de PROD de Monza no toca estos archivos.
