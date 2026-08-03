# MonzaParts · Guías de despacho electrónicas al SII (Fase 5 del espejo)

**Fecha:** 2026-07-29 · **Cuenta Wasabil:** LOPEZ HERNANDEZ INVERSIONES SPA (MonzaParts),
RUT 78.121.316-0 · **Espejo de:** `backend/wasabil_dte/` de Grupo AM (el módulo que ya
emitió los folios reales 136/137 y la factura 116) · **Referencia:**
`docs/integracion-wasabil-guias.md` + `backend/wasabil_dte/README.md`.

## Qué hace

Desde Despachos Monza, un despacho **en preparación** puede emitir su **guía de despacho
electrónica (DTE 52)** al SII vía Wasabil: botón "Emitir guía SII" → modal de 2 pasos
(previsualización con problemas bloqueantes → confirmación explícita) → el **folio del
SII pisa el N° de guía** del despacho. Después sigue el ciclo normal de la Fase 2:
transportista → confirmar despacho → guía firmada.

## Arquitectura (decisión fundamentada)

Paquete **propio y aislado** `backend/monza_wasabil_dte/` — NO se parametrizó el módulo
de GA: su client ya emite documentos reales y tocarlo arriesgaba regresión; además cada
marca necesita superficie de fakes independiente en los tests (patrón de la casa: cero
imports cruzados entre espejos monza_* y GA).

| Pieza | Qué |
|---|---|
| `client.py` | Copia verbatim del client GA con el token propio (`WASABIL_API_TOKEN_MONZA`); misma taxonomía de errores ambiguo/no-ambiguo |
| `models.py` | Tabla `monza_wasabil_dte`: FK a `monza_despachos`, **UNIQUE por despacho** (anti doble emisión), claim `en_vuelo_desde` TTL 180s, folio solo con status Emitido |
| `service.py` | Formato **v3**: líneas con **precio CONGELADO** del ítem (jamás recálculo vivo), name = descripción limpia, code = N° parte, **IVA por venta** (`iva_rate_de`), referencia **801 = OC del cliente con N° y fecha** (los datos que la Fase 3 volvió obligatorios), `invoiceReference` = solo N° DSP (la lección de la guía 137: la OC no va dos veces) |
| `router.py` | `/api/monza/wasabil/despachos/...`: preview / emitir / estado / reintentar / estado-batch, candado automotriz, gate `MONZA_CONTAB_ENABLED` |
| Guards inversos | En `monza_router_despachos.py`: con guía 52 **viva** no se anula el despacho (409) ni se pisa su folio a mano (409) |

## El protocolo anti doble emisión (irrenunciable, espejo GA)

1. **Preview nunca emite** (`issue=false`); `issue=true` existe solo tras tu confirmación.
2. **El claim se commitea ANTES de hablar con Wasabil** (FOR UPDATE + `populate_existing`;
   con rollback previo para que la re-validación vea lo commiteado).
3. Fallo **ambiguo** (timeout/5xx) NO se reintenta a ciegas; fallo claro sí permite reintentar.
4. El **folio** solo se escribe cuando Wasabil confirma Emitido (status 3), una única vez.
5. **Emitir al SII es irreversible**: la emisión real siempre es un clic del usuario.

## Pruebas

`backend/monza_wasabil_dte/tests/` — 3 suites **100% con dobles** (monkeypatch del client
Monza; jamás el API real), independientes de los fakes de GA: service puro (formato v3,
cuadratura por IVA de la venta, precio congelado), integración (emisión feliz, doble clic
bloqueado, ambiguo vs claro, folio pisa N° guía) y guards de despachos (anular/pisar con
guía viva → 409).

## Despliegue

**Antes de reiniciar** (idempotente): `python -m monza_wasabil_dte.init_db`
**`backend/.env` del servidor**: `WASABIL_API_TOKEN_MONZA=...` (token de la cuenta
MonzaParts; **jamás al repo**). OJO: `config.py` ya declara la variable — sin esa
declaración, un `.env` con el token tumba el backend completo al arrancar (pydantic).
Requiere build del frontend.

## Primera emisión real (cuando quieras probarla)

Igual que hicimos con la guía 136 de GA: puedo dejarte un **borrador** en Wasabil
(sin emitir) para que lo revises, y la emisión real la haces tú desde la app con tu clic.
