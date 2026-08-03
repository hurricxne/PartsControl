# MonzaParts · Cierre de venta y datos maestros (Fase 3 del espejo Grupo AM)

**Fecha:** 2026-07-28 · **Espejo de:** `docs/integracion-oc-cliente.md` (GA) adaptado al
modelo Monza (la cotización ES la venta: el cierre es `PATCH estado='vendida'`, no hay
entidad OC separada).

## Qué exige ahora el cierre de venta

- **N° de OC del cliente OBLIGATORIO** (backend 400 + campo marcado en el modal) y
  **fecha de la OC** (columna nueva `monza_cotizaciones.oc_fecha`): la referencia 801 del
  SII exige N° **y** fecha — sin esto la guía/factura electrónica (Fases 5/6) no puede
  armar la referencia.
- **`pct_adelanto` ya se persiste** (bug real: faltaba en el schema del PATCH y Pydantic
  lo descartaba en silencio → el flujo de adelantos nunca partía desde la UI).
- **Cierre idempotente**: re-enviar `vendida` (editar la OC ex-post, reintento del modal)
  edita campos pero NO repite efectos (fecha_venta, transición de líneas, log VENDIDA,
  notificación).
- **Sin retrocesos silenciosos**: `despachado` no vuelve a `vendida` (409); y una venta
  cerrada no vuelve a propuesta/enviada/rechazada **si tiene plata o logística colgando**
  (facturas, adelanto o despachos) — solo un cierre por error, aún sin efectos, se puede
  deshacer.
- **Transición `despachado` idempotente**: el LTV del cliente se suma UNA vez (antes se
  sumaba en cada re-PATCH) y la notificación no se repite.
- **Candados**: `require_empresa("automotriz")` + `require_rol("comercial", "contabilidad",
  "admin")` (permisivo mientras los roles no estén provisionados) + `FOR UPDATE` contra
  cierres simultáneos.

## Datos del receptor al facturar (monza_contabilidad)

- **Factura** (`tipo_doc="factura"`): exige **RUT válido** (dígito verificador módulo 11,
  helpers `rut_valido`/`rut_saneado` en `monza_contabilidad/service.py`, espejo GA) y
  **razón social** no vacía — el SII rechaza facturas con receptor malo; se frena antes.
  También exige **folio** (`numero_factura`). Una **boleta** no exige RUT ni folio.
- **Foto de precios completa**: la cotización congela `moneda_tarifa` (además de
  `tc_usd_clp`/`tc_eur_clp`/`tarifa_aerea`/`iva_pct`) y cada ítem guarda su `tc_aplicado`
  resuelto por moneda en el servidor — freeze-forward, sin backfill (mismo criterio que
  el TC congelado de GA, `docs/tc-congelado-cotizacion.md`).

## Despliegue

Antes de reiniciar el backend (idempotentes):

```bash
cd backend && python -m migrations.monza_oc_fecha_fase3
python -m migrations.monza_moneda_tarifa
```

## Pruebas

- `monza_tests/test_cierre_venta_datos_maestros.py` — OC obligatoria, pct_adelanto
  persistido, idempotencia, sin retroceso, candado de empresa, RUT al facturar (factura
  vs boleta), unidad de `rut_valido`.
- `monza_tests/test_cierre_estados_foto.py` — des-cierre con/sin plata, LTV una vez,
  foto de precios completa.
