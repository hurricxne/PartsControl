# Integración — Emisión de guías de despacho electrónicas (SII 52) vía Wasabil

Rama: `feature/wasabil-guias-despacho` sobre el snapshot de producción (`main` local,
commit `6c0ac01`, equivalente al main de GitHub del 2026-07-16). Solo **core / empresa
`mineria` (Grupo AM)**; nada de MonzaParts.

## Qué hace

En **Logística → Despachos**, un despacho **en preparación** gana el botón
**"Emitir guía SII"**: previsualiza la guía (receptor por RUT desde la ficha de
Wasabil, líneas con los precios de la cotización, NETO/IVA/TOTAL, referencia a la
OC del cliente tipo 801 con su fecha) y, con confirmación explícita del usuario,
la **emite al SII vía el API REST de Wasabil** (el facturador tiene el certificado
digital de GRUPO AM SPA — sin portal MiPyme, sin firma manual). Al quedar
**Emitida**, el folio real se graba en `despacho.numero_guia` y quedan los links
al PDF/XML. Si el SII rechaza, se muestra el motivo y hay reintento seguro que
jamás duplica. El resto del flujo (crear despacho, confirmar, firmar, facturar)
queda intacto.

## Qué cambia (respecto del main de producción)

| Archivo | Cambio |
|---|---|
| `backend/wasabil_dte/` *(nuevo, aislado)* | Módulo completo: `client.py` (httpx → api.wasabil.com, token en .env), `service.py` (armado guía 52, nombres ≤25 SII, ref 801, IVA half-up), `models.py` (tabla NUEVA `wasabil_dte` — cero ALTER a tablas existentes), `router.py` (`/api/wasabil/despachos/{id}/preview·emitir·estado·reintentar` + `estado-batch`, candado `require_empresa("mineria")`), `init_db.py` idempotente, `tests/` (23 pruebas), `README.md` (protocolo de seguridad). |
| `backend/main.py` | +2 líneas: import + `include_router` (patrón de los módulos aislados). |
| `backend/config.py` | +2 settings: `WASABIL_API_TOKEN` / `WASABIL_API_BASE`. Sin token: preview funciona, emitir se bloquea con aviso claro. |
| `frontend-src/src/services/api.ts` | Bloque `wasabilAPI`. |
| `frontend-src/src/pages/DespachosPage.tsx` | Botón "Emitir guía SII" / "Reintentar" / "Estado guía SII", `EmitirGuiaSIIModal` (preview → confirmar → sondeo → folio + PDF), badges DTE por despacho, folio manual bloqueado si la guía es electrónica (también durante la emisión y si la consulta de DTEs falla), aviso al anular con guía emitida/en curso. Base verificada idéntica al main de prod (diff = 0) antes de aplicar. |
| `backend/routers/despachos.py` *(endurecido tras la revisión multi-agente)* | (a) Candado `require_empresa("mineria")` a nivel de router (la tabla no tiene columna empresa; Monza usa su propio módulo `/api/monza`). (b) **Guard de guía electrónica viva**: `anular_despacho` rechaza con 409 si el despacho tiene guía SII emitida o en emisión (anular acá dejaría el documento legal huérfano y la mercadería libre para emitir OTRA guía → doble emisión); `update_despacho`/`firmar_despacho` rechazan pisar `numero_guia` cuando es (o será) el folio del SII. (c) `create_despacho`: rechaza ítems repetidos, valida el disponible bajo `SELECT … FOR UPDATE` (cierra el sobredespacho por concurrencia) y reintenta si el correlativo `DSP-…` choca con otro request simultáneo. (d) `GET /oc-clientes` y `/counts` precargan en lote (antes ~3 queries por OC). |
| `backend/routers/tests/test_despachos_guards.py` *(nuevo)* | 16 checks de integración de todos los guards anteriores (empresa, sobredespacho, correlativo, anular/editar/firmar con DTE). |

## Diseño anti doble emisión (lo importante)

Emitir al SII es **irreversible**, así que el módulo fue endurecido con una revisión
multi-agente de 2 rondas (28 hallazgos corregidos). Reglas:

1. `issue=true` SOLO tras previsualización + confirmación explícita del usuario.
2. **Claim** `en_vuelo_desde` (TTL 180 s) marcado bajo `SELECT … FOR UPDATE` **sin
   red dentro del lock**; toda query con lock usa `populate_existing()` (sin eso el
   identity map de SQLAlchemy devuelve datos viejos y dos requests concurrentes
   podrían emitir doble — reproducido empíricamente).
3. Máquina de estados explícita con default-deny; errores de red clasificados
   (`WasabilError.ambiguo`: timeout/5xx mantienen el claim; 4xx/conexión-rechazada
   lo liberan).
4. El reintento **verifica en Wasabil** (por uuid o por la referencia interna
   `OC … · DSP-…`) antes de re-crear, y **aborta** si no puede verificar — incluida
   la búsqueda paginada: si la lista quedó truncada, "no lo encontré" NO prueba que
   no exista y también se aborta (502, nunca se re-crea a ciegas).
5. El folio se registra SOLO con status 3 (Emitido).
6. Guards en `routers/despachos.py`: con guía electrónica viva no se puede **anular
   el despacho** ni **pisar el folio** desde editar/firmar (la anulación del DTE se
   gestiona en Wasabil; recién ahí el despacho se puede anular y re-emitir).

## Configuración

En `backend/.env` (nunca en git):
```
WASABIL_API_TOKEN=<token de https://app.wasabil.com/api-tokens>
```

## Notas de deploy

- **Tabla nueva `wasabil_dte`**: con `AUTO_CREATE_TABLES=True` la crea el arranque.
  Si PROD corre con `AUTO_CREATE_TABLES=False`, correr una vez (desde `backend/`):
  `python -m wasabil_dte.init_db` (idempotente; también agrega columnas nuevas del
  módulo si ya existía la tabla).
- Sin migraciones sobre tablas existentes. La única escritura a una tabla existente
  es `despacho.numero_guia = folio` al quedar Emitido.
- ⚠️ **Antes de habilitar en producción**: hacer una **primera emisión real
  controlada** (despacho de prueba, tipo de traslado interno). La prueba del
  2026-07-17 (sección más abajo) YA confirmó contra el API real: `GET /clients`
  (formato ficha), `dispatch_guide{dispatch_type_code}` y `references` (los
  aceptó la creación del borrador). Lo único aún no confirmado es la forma de la
  respuesta de la EMISIÓN misma (`status_id`/folio/PDF al emitir de verdad) —
  cualquier ajuste queda contenido en `wasabil_dte/client.py` y
  `service.payload_a_rest()` (ver README del módulo).

## Verificación

- **Backend**: `cd backend && ./venv/bin/python -m pytest wasabil_dte/tests -q`
  (23 pruebas; necesita MySQL local con `DATABASE_URL`).
- **Frontend**: `cd frontend-src && npm run build` (typecheck + build).
- **Manual**: */despachos* → OC con despacho en preparación → **Emitir guía SII** →
  previsualización con receptor/líneas/totales → sin token muestra el bloqueo
  "Wasabil no está configurado"; con token (sandbox) emite y registra el folio.

## Prueba real contra el API de Wasabil (2026-07-17)

Primera conexión con token real (cuenta GRUPO AM SPA 77.977.813-4, cliente de
prueba H-E PARTS 78.279.030-7). Hallazgos y correcciones — el módulo se había
desarrollado contra un Wasabil **simulado** que no reflejaba el formato real:

1. **Formato de respuesta del API** (corregido, commit del fix): toda respuesta OK
   viene envuelta en `{success, status, data}`; los listados son `{items, total,
   lastPage}`; la ficha del cliente trae giro y dirección **anidados** en `giros[]`
   y `addresses[]`. Se centralizó en `client.py` (`_desenvolver`, `_items`,
   `_normalizar_cliente`). Con esto la previsualización lee el receptor completo.
2. **Folio de referencia (N° OC) máx. 18 caracteres** (corregido): el SII rechaza
   `references.0.folio` con más de 18. La previsualización ahora lo **bloquea con
   mensaje claro** (`FOLIO_REF_MAX` en service.py) en vez de fallar al emitir.
3. **Mensajes de validación** (corregido): los rechazos 4xx traen
   `{"validation": {campo: motivo}}`; `client._request` los aplana a texto legible.
4. **El PDF/timbre solo existe tras EMITIR**: un documento creado con `issue=false`
   queda en estado **Pendiente** (sin folio, `has_document_pdf=false`). No hay PDF
   "preliminar" — el timbre electrónico lo genera el SII al emitir. Para revisar
   antes de emitir está la **previsualización** (datos, montos, receptor, referencia).
5. **`buscar_documentos` apunta a `GET /documents` → 405** (PENDIENTE): ese endpoint
   solo acepta POST. Solo afecta el **reintento por-referencia** (emisión que falló
   sin devolver uuid): hoy ese fallback **aborta de forma segura** (nunca re-emite a
   ciegas) pero no reencuentra el documento. El flujo normal (emisión que devuelve
   uuid → sondeo por uuid) no se ve afectado. Confirmar el endpoint de listado real
   antes de depender del reintento automático.
6. **Tipo de traslado elegible** (nuevo): la guía ya no sale fija en "Operación
   constituye venta". El operador elige el `dispatchTypeCode` del SII en el modal de
   emisión (venta 1 por defecto, traslado interno 5 hacia bodega propia, consignación,
   devolución, etc. — ver `TIPOS_TRASLADO` en service.py). El backend lo valida (400
   si es inválido) en preview/emitir/reintentar; el frontend lo pobla desde el preview.

## Cómo revertir

Quitar las 2 líneas de `main.py`, los 2 settings de `config.py`, el bloque
`wasabilAPI` de `api.ts` y restaurar `DespachosPage.tsx` desde `main`
(`git checkout main -- frontend-src/src/pages/DespachosPage.tsx`). La carpeta
`backend/wasabil_dte/` y la tabla `wasabil_dte` pueden quedar (no molestan).
