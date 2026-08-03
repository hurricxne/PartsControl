# Regla de la casa: toda decisión sobre PLATA se relee BAJO LOCK

Auditoría 2026-07-21. Un enjambre empírico encontró cuatro fugas de dinero — todas con
la **misma causa raíz**, y ninguna la detectaban las pruebas.

## El problema en una frase

El candado serializa bien, pero **las lecturas mienten**.

## Por qué

MySQL corre en `REPEATABLE READ`. En ese nivel, InnoDB arma la "foto" de la base
(*read view*) en la **primera sentencia** de la transacción, y todas las lecturas
**planas** posteriores sirven esa foto — aunque otra transacción haya commiteado después.
Las lecturas **bloqueantes** (`SELECT ... FOR UPDATE`) no: esas siempre ven la última
versión commiteada.

En PartsControl la primera sentencia de **todo** request es un `SELECT` de usuarios: el
router se declara con `dependencies=[Depends(require_empresa("mineria"))]`, que depende de
`get_current_user`, que consulta la BD con la **misma** sesión. Es decir: cuando el
endpoint llega a su `with_for_update()`, la foto ya está tomada.

Resultado: dos requests se serializan correctamente por el lock, pero el segundo calcula
su tope sobre datos anteriores al primero.

```python
factura = db.query(ContFacturaCliente).filter(...).with_for_update().first()  # ✅ fresco
pagado = sum(_f(c.monto) for c in factura.cobranzas)   # ❌ relación perezosa = foto vieja
if payload.monto > bruto - pagado:                     # ❌ el tope miente
    raise HTTPException(400, "excede el saldo")
```

## Lo que costó (verificado con números, no en teoría)

| Fuga | Efecto |
|---|---|
| Dos cobranzas simultáneas por el saldo completo | libro con $238.000 en una factura de $119.000 |
| Dos adelantos aprobados en paralelo | se le exige al cliente $59.500 que **ya depositó** |
| Borrar una cobranza mientras entra otra | saldo persistido corrupto que **nada** corrige |
| Adelanto sobre factura ya cedida al factor | el factor libera $0 en vez de $19.000 |
| Dos egresos simultáneos a la misma compra | **sobre-pago al proveedor** (plata que sale) |

## La regla

1. **Toda cifra que decide un tope de plata se relee BAJO LOCK**, con
   `populate_existing().with_for_update()`. Nunca desde una relación perezosa
   (`factura.cobranzas`, `factura.factoring`, `compra.egreso_detalles`) ni desde un
   `selectinload` — ese emite su propio `SELECT` plano y *parece* seguro sin serlo.
   Helpers de referencia: `_cobranzas_bloqueadas` / `_factoring_bloqueado` en
   `backend/routers/contabilidad.py`.
2. **`db.refresh(objeto, with_for_update=True)`** cuando haya que recargar tras un flush.
   Un refresh normal repuebla con la foto vieja justo antes de recalcular; y si el valor
   recalculado coincide con ese valor viejo, **SQLAlchemy no emite el UPDATE** y el dato
   derivado queda corrupto para siempre. Fue el caso más difícil de ver de los cinco.
3. **`populate_existing()` es obligatorio** en toda lectura bloqueante: sin él, si la fila
   ya está en la sesión, SQLAlchemy devuelve el objeto cacheado y descarta los valores
   frescos que trajo el `FOR UPDATE` — a cualquier nivel de aislamiento.
4. **La unicidad se garantiza con índice `UNIQUE` + captura de `IntegrityError`**, nunca
   con gap locks.
5. **Los helpers se copian por módulo**, no se comparten entre módulos (los módulos de la
   casa son aislados a propósito).

## Y la regla para las PRUEBAS

Las suites sustituían `get_current_user` por un `lambda` que **no consulta la BD**. Con
ese atajo el `with_for_update()` pasa a ser la primera sentencia del request y la foto
nace *después* del candado: la clase entera de bug es **invisible**. Medido: 0 de 4 rondas
fallan con el login falso, 4 de 4 con el login real.

Todas las suites usan ahora un override que hace una lectura real en la misma sesión.
**No volver al lambda seco.** Arnés de referencia:
`backend/tests_contabilidad/test_concurrencia_plata.py` — reproduce tres carreras en 4
rondas cada una y **verifica leyendo con conexión nueva** (la sesión del propio test
también arrastra su foto y puede dar un falso rojo… o un falso verde).

## Pendiente: bajar el motor a READ COMMITTED

La mesa redonda (3 votos a 1) recomendó **además** poner el engine en `READ COMMITTED`
(`backend/database.py`, un parámetro de `create_engine`), que cierra la clase entera de
raíz — incluidos los ~11 puntos aún no endurecidos, varios en módulos espejo de Monza.
Verificado: no hay ningún camino de la casa que dependa de gap locks, y los deadlocks
*bajan*.

**Requisitos antes de aplicarlo:**

1. Confirmar en el servidor de producción: `SELECT @@global.binlog_format;` debe ser `ROW`
   o `MIXED`. Con `STATEMENT`, MySQL **rechaza** las escrituras bajo READ COMMITTED.
   (Local: `ROW` ✓.)
2. Va en **commit aparte**, para poder revertirlo solo a él.
3. Reiniciar el servicio **completo** de uvicorn (no `--reload`): el nivel se fija al abrir
   cada conexión, y las que ya están en el pool seguirían en el nivel viejo.

Los arreglos puntuales ya aplicados protegen los caminos críticos por sí solos: ese es el
punto de tener las dos capas.

## Tolerancia de 1 CLP (TOL_PAGO) — por qué la identidad Σ cobranzas == bruto − saldo puede desviarse

Auditoría integral MonzaParts 2026-07-29 (hallazgo LOW). **No es un descuadre: está a
propósito y se documenta para que una auditoría futura no lo vuelva a levantar.**

1. Los topes de plata aceptan hasta **1 CLP por sobre el saldo** para absorber el polvo de
   redondeo de IVA/factoring (half-up a peso por línea y por tanda):
   · cobranzas — `backend/monza_contabilidad/router.py` (`payload.monto > saldo_actual + TOL_PAGO`)
     y su espejo `backend/routers/contabilidad.py`;
   · factoring — el mismo `TOL_PAGO` en el cupo financiable de ambos módulos;
   · facturación — el tope Σ brutos ≤ total de la venta.
2. Por eso, cuando hay un sobrepago DENTRO de la holgura, la identidad
   `Σ cobranzas == bruto − saldo` puede desviarse **hasta 1 CLP por factura**: el saldo se
   clampea a 0 (`max(bruto − pagado, 0)`, `monza_contabilidad/service.py::_recompute_factura`)
   mientras `monto_pagado` conserva lo que el operador registró de verdad.
3. El desvío **NO es acumulable dentro de una factura** (el invariante duro
   `Σ cobranzas ≤ bruto + TOL_PAGO` se revalida BAJO LOCK en cada inserción, también por la
   vía de factoring): un segundo peso ya sale 400. Pero **sí suma 1 CLP por factura
   sobrepagada** en las lecturas agregadas (`cobrado_clp` de la venta y del KPI), así que
   una diferencia de N pesos con N facturas es **esperada**.

Si alguna vez se quisiera la identidad exacta al peso: **no** usar `min(payload.monto, saldo_actual)`
a secas (el `gt=0` de pydantic valida el payload, no el valor capado — habría que usar
`min(payload.monto, max(saldo_actual, 0.0))` y rechazar con 400 si queda en 0). Igual **no se
recomienda**: capar en silencio altera lo que registró el operador, y rompería la paridad con
Grupo AM, que tendría que cambiar en el mismo commit.
