# Paridad contable MonzaParts ↔ MachParts — 2026-07-30

Commits: `20762a2` (backend) · `f54c1e3` (frontend) · `8b10129` (auditoría adversarial).
Gate al cierre: **233 verdes** (`cd backend && python -m pytest -q`, **pelado**), `tsc` y build limpios.

## El hallazgo que dio vuelta la premisa

El encargo era "que MonzaParts tenga todo lo que MachParts tiene en contabilidad". El
reconocimiento —8 lentes de solo lectura, cada afirmación con archivo:línea— encontró que **en los
módulos maduros MonzaParts va ADELANTE**: se construyó después, con las lecciones ya aprendidas y
pasando por multienjambres que MachParts nunca tuvo.

- Compras y CxP: los 16 endpoints idénticos.
- Tesorería: 22 endpoints y un `service.py` que difiere **solo en docstrings**.
- Embarques Pricing: el prorrateo es equivalente línea a línea (solo cambia `lbs`↔`kg`).

Así que el trabajo resultó **bidireccional**. Y el lado incómodo: **MachParts es la marca que ya
emite documentos tributarios reales**, así que allí los defectos cuestan plata y son irreversibles.

## Convergencias entre lentes independientes

Lo que más peso tuvo no fue el volumen, sino los hallazgos a los que llegaron lentes distintos por
caminos distintos:

1. **El gasto de embarque que en MachParts había que re-digitar** (3 lentes). Al re-tipearlo la
   compra nacía sin la llave, el anti-duplicado **nunca se disparaba**, y en MySQL los NULL no
   colisionan en el índice único → la factura del forwarder entraba 2-3 veces.
2. **MonzaParts no podía revertir un adelanto** (3 lentes), con prueba humeante: el sistema
   respondía *«Revierta el adelanto en Contabilidad/Tesorería primero»* y **ese endpoint no
   existía**. Mientras tanto `adelanto_verificado` quedaba pegado en 1 y Abastecimiento seguía
   comprando contra un 50% inexistente.
3. **El folio del anticipo sin validar en MachParts** (3 lentes): lo teclea el operador y el error
   aparecía semanas después, al emitir la factura del despacho, con el anticipo ya descontando cupo.

## Lo que ganó cada marca

**MonzaParts** (lo que pidió el dueño): anular/revertir un adelanto · preview de la factura antes de
emitir · adelanto no pactado · plazo real de la venta (toda factura salía a 30 días y el aging
quedaba mal en todas) · fecha prometida de entrega con el motor de días hábiles chilenos (sin ese
campo el semáforo decía "Sin fecha" en el 100% de las ventas y **ninguna alerta se disparaba**) ·
crédito a 60 y 90 días · adelanto con % libre o monto exacto · movimiento bancario manual · pestaña
de cuentas · retención de factoring y adelantos depositados en el flujo de caja · guía firmada
visible · moneda del proveedor autocompletada (antes la compra se contabilizaba a TC 1) · guard que
impide mover N°/fecha de OC después de que un DTE los referenció.

**MachParts** (endurecimiento que Monza ya tenía): botón para pasar un gasto de embarque a CxP ·
aviso y casilla antes de un segundo anticipo · las advertencias del backend ya no se tiran a la
basura · plazo en la factura de anticipo (nacía sin vencimiento: no vencía nunca ni entraba al KPI) ·
KPI de anticipo de factoring · retry de deadlock y orden canónico de candados en Tesorería · montos
negativos que ya no se congelan en el pricing · aviso de embarque con monedas mezcladas · TC EUR que
leía una columna inexistente y devolvía siempre 0 · y la alerta diaria de "plazo crítico", que estaba
**muerta desde siempre** porque preguntaba si una fecha tiene atributo `days`.

## La lección del cinturón anti doble emisión

Vale más que el código. Agregué un cinturón que, antes de reintentar, le pregunta a Wasabil si ya
existe un documento con esa referencia. Tenía **dos defectos de diseño míos**:

1. **Fallaba ABIERTO.** Si la consulta no respondía, se tragaba el error y seguía emitiendo. El
   rescate, ante lo mismo, fallaba cerrado. Dos guards sobre la misma fuente con lecturas opuestas.
2. **En producción no bloqueaba nunca**, porque el propio cliente documenta que `GET /documents`
   responde **405** en el API real.

**Un guard inerte es peor que ninguno: da confianza falsa.**

Cerrado con tres veredictos explícitos, y el tercero nunca se comporta como el primero:

| Veredicto | Qué significa | Qué hace |
|---|---|---|
| `SIN_EMITIDO` | consta que no hay | sigue |
| `HAY_EMITIDO` | consta que sí | 409 nombrando el folio |
| `INDETERMINADO` | **no se puede concluir** | 409 pidiendo verificación humana |

Y después el agujero **se movió** de "no pude preguntar" a la **definición** de "ya existe": el
cinturón contaba solo `status 3`, así que un documento `procesando` o con un status ilegible pasaba
como "no hay nada" y habilitaba re-emitir **con el listado sano**. Se reprodujeron **7 dobles
emisiones reales** (52 y 33, en las dos marcas) por esa sola diferencia.

> La pregunta correcta no es *«¿hay un status 3?»* sino
> **«¿puedo PROBAR que no existe ningún documento capaz de quedarse con un folio?»**.
> Solo un rechazo **confirmado y legible** autoriza re-emitir.

## Sobre las sondas: el hallazgo más repetido

Los auditores midieron el poder discriminante quitando cada arreglo. Encontraron que:

- **Varios guards nuevos no tenían ninguna sonda**: el gate quedaba verde con ellos MUERTOS.
- Un test **fijaba como correcto** que *«con el listado caído se re-emite»* — el invariante contrario.
- Los fakes **no registraban el documento que creaban**, así que el estado normal de producción tras
  un reintento (dos documentos con la misma referencia) era **estructuralmente inalcanzable** en las
  suites.
- Una sonda verificaba **leyendo el código fuente** en vez de ejercitar el comportamiento, y un
  auditor la burló.

Regla que queda: **una sonda se valida quitando el arreglo y viendo el rojo**, el fake tiene que
reproducir el estado ADVERSO (no el cómodo), y prohibido verificar por introspección de texto.

## Limitación conocida (documentada, no silenciada)

Si un DTE está **rechazado localmente pero vivo en Wasabil**, y alguien **borra la factura** y vuelve
a facturar, la factura nueva nace con otra referencia (`FACT-<id nuevo>`) y el cinturón no puede ver
el ancla huérfana de la vieja. El ancla **se conserva** con una nota que dice uuid, referencia y qué
revisar, así que queda rastro para el humano — pero la comprobación no es automática.
Cerrarlo exige que la verificación mire también las anclas huérfanas de la misma mercadería.

## Fuera de alcance — decisión del dueño

No se construyeron porque son funcionalidad nueva o cambian una regla de negocio, no paridad:

1. **Nota de crédito / anulación de factura.** No existe en **ninguna** marca. Hoy el reverso legal
   se hace en Wasabil y **el ERP nunca se entera**: la factura sigue en cartera, aging y KPIs. Es la
   ausencia más grande del núcleo contable.
2. **Retenciones, notas de crédito de proveedor y cuotas.** Una factura a 30/60/90 solo admite UN
   vencimiento → la antigüedad de cartera informa mal. En ambas marcas.
3. **IVA configurable en MachParts.** Hoy 19% cableado; cambiarlo rompería el cuadre de las ventas
   viejas (se recalcularían con la tasa nueva mientras las facturas emitidas quedaron con la vieja).
4. **Re-encauce del adelanto en MachParts** (Monza lo tiene): hoy un anticipo emitido sin ligar
   adelantos nace impagable, y la propia pantalla empuja a ese hoyo.
5. **«Retiro en oficina»** (facturar sin guía) en MachParts: portarlo relajaría la regla que protege
   el flujo minero. Pregunta de negocio.
6. **N adelantos por venta en Monza**: hoy 1 por venta, documentado como deliberado.
7. **Half-even y pesos perdidos en el prorrateo del landed** (ambas): Σ de las filas no cuadra con la
   cabecera por unos pesos, y se congela en el snapshot.
8. **Fantasmas en el pricing cerrado** (ambas): quitar o agregar ítems deja filas con costo congelado.
9. **El costo landed es un callejón sin salida** (ambas): nadie lo consume; el precio de venta se
   sigue fijando con el landed estimado.
10. **Sin saldo por cuenta bancaria ni exportación a Excel** (ambas), aunque el dato base ya se guarda.
11. **Candado de empresa entre marcas** — diferido por el dueño.
12. **Un solo TC por embarque** (ambas): la moneda del primer ítem se aplica a todo.

## Higiene que no era de código

`respaldos-bd/` contenía un **dump real de producción** (clientes, RUTs, precios, facturas) y **no
estaba en `.gitignore`**: un `git add -A` descuidado lo publicaba. Ya está bloqueado, junto con los
patrones `*.sql.gz` y `machparts_db-*.sql`.
