# MonzaParts — las dos salidas de emergencia que faltaban

**Fecha:** 2026-08-02 · **Marca:** MonzaParts **únicamente** (no se tocó ni una línea de MachParts)
**Migración de base de datos:** **NINGUNA** — ver §7
**Gate al cierre:** `pytest` pelado verde · `tsc` y `npm run build` limpios

---

## 1. Las dos cosas, en una frase cada una

1. **Registrar folio emitido fuera del sistema.** El SII aceptó un documento y su folio nunca
   llegó al ERP. Ese estado era **permanente**: la guía se quedaba sin N° para siempre.
2. **Revertir el factoring de una factura.** Una cesión al factor quedó contra un documento
   tributario que nunca existió, y esa fila no se podía liquidar, ni editar, ni borrar.

Las dos existen para lo mismo: **destrabar algo que quedó atrapado, sin meter mano en la base
de datos y dejando rastro de quién lo hizo y por qué.**

---

## 2. Registrar folio del SII

### 2.1 El callejón

`_completar_documento_emitido` **falla ABIERTO a propósito**: un error de *consulta* no debe
convertirse en el fracaso de una emisión que **sí salió**. El precio de esa decisión es una fila
con `status_id = 3` (emitido) y `folio = NULL`. Y ese estado no se repara solo:

| Camino | Qué respondía | ¿Por qué está bien que responda eso? |
|---|---|---|
| Sondeo de estado | no lo reparaba | el documento remoto puede no traer folio nunca |
| «Reintentar» | 409 | re-emitir sería un **SEGUNDO** documento tributario REAL |
| Teclear el N° a mano | 409 | guard anti-pisado: el N° es el folio, no un campo libre |

Todas correctas por separado, y juntas un callejón sin salida. La única salida real era un
`UPDATE` a mano en MySQL.

### 2.2 Las cinco reglas

`monza_wasabil_dte/router.py::_registrar_folio_a_mano`. Ninguna es capaz de emitir:

1. **Sólo el callejón exacto** (`status 3` + folio vacío). Cualquier otro estado → 409. Folio
   idéntico → **idempotente** 200, para que el doble clic no sea un error.
2. **Doble digitación**: el operador repite el folio. El campo de confirmación **no acepta
   pegar** — copiar el mismo error dos veces no confirma nada.
3. **Forma de folio del SII**: correlativo numérico ASCII, hasta 18 dígitos. (`isascii()` además
   de `isdigit()`: `'٣'.isdigit()` es `True` y no es un folio.)
4. **La máquina manda cuando puede concluir** (`_folio_confirmado_por_wasabil`).
5. **Se escribe por el mismo camino que la emisión** (`_actualizar_desde_wasabil`), con sus tres
   pisos puestos, así el folio llega a `monza_despachos.numero_guia` igual que siempre.

### 2.3 Los tres veredictos de la consulta a Wasabil

| Veredicto | Qué significa | Qué hace |
|---|---|---|
| folio confirmado | consta cuál es | si no coincide con lo tecleado → **409** nombrando los dos |
| **contradice** | aquí consta emitido y allá no hay ninguno (o hay **dos**) | **409**: lo mira una persona |
| no se puede concluir | la consulta falló o la lista vino truncada | se acepta la **declaración del operador**, con rastro |

**Por qué el tercero NO bloquea, al revés que el cinturón anti doble emisión.** Son preguntas
distintas: el cinturón autoriza **crear** un documento ante el SII (irreversible), así que ante
la duda no se emite. Esto sólo **anota un número** de un documento que ya existe, y bloquear
aquí deja el callejón cerrado para siempre — que es el problema que se está resolviendo.
Además `GET /documents` responde 405 en el API real, así que un guard que bloqueara ante «no
pude preguntar» **bloquearía siempre** y la funcionalidad nacería muerta.

El riesgo residual (que el folio tecleado sea de otro documento) lo acotan las otras cuatro
reglas, y el origen queda escrito: *«declarado por el operador (Wasabil no pudo confirmarlo: …)»*
en `respuesta_json` y en el log del servidor, con usuario y fecha.

### 2.4 Endpoints

```
POST /api/monza/wasabil/despachos/{id}/registrar-folio?folio=…&confirmo_folio=…
POST /api/monza/wasabil/facturas/{id}/registrar-folio?folio=…&confirmo_folio=…
```

La gemela de facturas hace **un paso más**: llama a `_finalizar_factura_emitida` en la **misma
transacción**, que escribe `numero_factura` y aplica el **adelanto que la emisión había
diferido**. Sin eso, la factura quedaba emitida ante el SII, sin N° en el ERP, fuera de la
cartera y con el adelanto del cliente sin aplicar nunca. Sus advertencias se muestran al
operador (son plata: no se tragan en silencio).

---

## 3. Revertir el factoring

### 3.1 El zombi, y por qué había que cerrar la entrada al mismo tiempo

`set_factoring` de MonzaParts **no pedía folio del SII**. Era el único camino de plata que no lo
hacía — la cobranza manual y la aplicación de adelantos sí—, así que se podía **vender al factor
una acreencia que el SII nunca conoció**. Esa fila quedaba después cerrada por los cuatro lados:

- no se puede **liquidar** (guard SII de `liquidar_factoring`),
- no se puede **editar a 0** (guard SII de `set_factoring`, la única forma que tenía el módulo
  de deshacer una cesión),
- no se puede **eliminar la factura** (`eliminar_factura` rechaza toda factura con factoring),
- la **aplicación automática de adelantos devuelve 0**.

Plata del factor amarrada a un documento inexistente y **el cupo facturable de esa mercadería
secuestrado para siempre**.

Por eso las dos mitades van juntas: **cerrar la entrada sin abrir una salida** deja atrapadas
las filas que ya existen, y **abrir la salida sin cerrar la entrada** es un trapeador bajo una
llave abierta.

### 3.2 La decisión de arquitectura: una sola condición

La condición «este documento todavía no existe ante el SII» vivía **copiada en tres sitios** de
`monza_contabilidad/router.py`. Ahora tiene nombre — `_plata_bloqueada_por_sii` — y **la puerta
de salida abre EXACTAMENTE donde el guard bloquea**:

```
set_factoring / liquidar_factoring   →  bloquean si  _plata_bloqueada_por_sii == True
revertir_factoring                   →  abre    si   _plata_bloqueada_por_sii == True
```

No es cosmética. Con la condición copiada, tocar una de las dos dejaba **o** una salida que no
abre nunca **o** —mucho peor— una que **borra una cesión al factor que era real**. La sonda B de
la suite demuestra justo eso: al quitar el cruce, el test que protege el factoring legítimo se
pone rojo.

### 3.3 Por qué BORRA la fila y no la marca 'revertida'

Mientras exista una fila en `monza_cont_factoring`, `eliminar_factura` sigue respondiendo 409 y
la factura sigue siendo imborrable: **el zombi seguiría vivo con otro nombre**. Es además la
convención de la casa para plata sin huella contable (`eliminar_cobranza`, `eliminar_factura`).

El hecho **no se pierde**: queda en `factura.observaciones` —visible en la ficha— y en el log del
servidor, con motivo, montos, id de operación y usuario. Reversiones sucesivas **acumulan** la
nota, no la pisan.

### 3.4 Qué NO hace

No toca las **cobranzas del cliente** (ésas se revierten una por una con su propio endpoint), ni
el DTE, ni el registro tributario. Después de revertir, la factura queda borrable **por el camino
normal**, que sigue exigiendo lo suyo: si el documento puede existir ante el SII,
`_bloqueo_dte_factura` sigue pidiendo intervención humana. *La plata sale; el documento
irreversible sigue necesitando un humano.*

Además: si el abono del factor **ya está conciliado** con la cartola en Tesorería, se rechaza con
409 (borrar la cobranza dejaría el movimiento bancario sin destino, y el `ON DELETE CASCADE` del
enlace se llevaría la conciliación en silencio).

---

## 4. Cómo se cruzan las dos funciones (importante)

`_plata_bloqueada_por_sii` es **False** cuando el DTE está en `status 3`, **aunque el folio falte**
— y con razón: el documento **sí existe** ante el SII. Consecuencia práctica:

> Una factura atrapada en el callejón «emitida sin folio» **no se arregla con *revertir*** (que
> responderá 409), sino con ***registrar folio***. Y al registrarlo, `numero_factura` se llena y
> la factura sale sola de la zona donde el guard de factoring bloquea.

Las dos funciones son complementarias, no alternativas.

---

## 5. Archivos

### Backend (sólo paquetes `monza_*`)

| Archivo | Qué |
|---|---|
| `monza_wasabil_dte/router.py` | `_folio_dte_valido` · `_folio_confirmado_por_wasabil` · `_registrar_folio_a_mano` · los 2 endpoints |
| `monza_contabilidad/router.py` | `_plata_bloqueada_por_sii` + `_exigir_sii_emitido` (**nuevos**, reemplazan la condición copiada en 3 sitios) · guard en `set_factoring` y `liquidar_factoring` (**nuevo**) · `revertir_factoring` (**nuevo**) |
| `monza_contabilidad/schemas.py` | `RevertirFactoringIn` (motivo obligatorio) |

### Frontend

| Archivo | Qué |
|---|---|
| `pages/MonzaRegistrarFolioModal.tsx` | **NUEVO.** La doble digitación, **compartida** por las dos pantallas |
| `pages/MonzaDespachosPage.tsx` | badge «Guía SII sin folio» + botón · `folioParaModal` gana el estado `'sin_folio'` (ver §6) |
| `pages/MonzaFacturasPage.tsx` | badge «SII sin folio» + botón · zona de riesgo para revertir el factoring |
| `services/monzaApi.ts` | `registrarFolioGuia` · `registrarFolioFactura` · `revertirFactoring` |

**Por qué el modal es un componente y no una copia en cada página:** es una confirmación de
seguridad sobre un documento irreversible. Con dos copias, el día que una gane una validación y
la otra no, manda la pantalla más débil.

---

### 5.1 La condición de la UI replica la del backend, entera

La zona de reversión aparece sólo si se cumplen **las tres** condiciones de
`_plata_bloqueada_por_sii`: documento tipo *factura*, sin folio local, **y con un DTE que existe
pero no está emitido**. La primera versión sólo miraba las dos primeras, y en una factura sin
folio y **sin DTE** (una factura manual a medio registrar) ofrecía un botón que el backend
rechazaba con 409. Por eso `FactoringModal` recibe ahora el estado SII de la factura: *la UI no
debe ofrecer lo que el backend rechaza.*

---

## 6. Un desajuste que apareció de paso (y se cerró)

En Despachos, `folioParaModal` devolvía `null` para una guía **emitida sin folio** (ese estado no
está en `DTE_EN_PROCESO`), así que el campo «N° Guía» quedaba **editable**… y
`_rechazar_si_pisa_folio` lo rechazaba igual con un 409 que además decía *«emisión en curso»*,
que no era lo que pasaba. Ahora ese estado bloquea el campo, **dice la verdad** y apunta al botón
nuevo.

---

## 7. Deploy

**No hay migración de base de datos.** Las dos funciones trabajan sobre tablas y columnas que ya
existen (`monza_wasabil_dte`, `monza_cont_factoring`, `monza_cont_cobranza`). El deploy es el
normal: subir el código, `npm run build` del frontend y reiniciar el backend.

*(Se dice explícitamente porque en este repositorio varios scripts de esquema fallan en silencio
y la pregunta «¿qué hay que correr antes de reiniciar?» es la que más caro sale. Aquí: nada.)*

---

## 8. Efecto en el día a día

**Lo que cambia para el operador:**

- Una guía o factura atrapada sin folio ahora se marca en **ámbar** y trae su botón. Se resuelve
  en dos clics, con el número que se lee en app.wasabil.com.
- **Ceder al factor una factura sin folio del SII ahora se rechaza.** Antes se dejaba. Si esto
  aparece en la operación diaria, la salida es esperar el folio (o usar «Reintentar»), no
  saltarse el paso: es lo que impide vender una acreencia que no existe.
- Si ya hay cesiones registradas contra facturas sin folio, el modal de factoring muestra la
  **zona de reversión** para limpiarlas, pidiendo un motivo.

**Lo que NO cambia:** nada de MachParts, ninguna factura ya emitida, ningún dato existente.

---

## 9. Pruebas

```bash
cd backend && ./venv/bin/python -m pytest \
  monza_wasabil_dte/tests/test_registrar_folio.py \
  monza_contabilidad/tests/test_revertir_factoring.py -q
```

**39 checks** en la primera y **32** en la segunda, sin red y sin emitir nada.

### Sondas de poder discriminante (verificadas quitando el arreglo)

| Sonda | Qué se quitó | Qué se puso rojo |
|---|---|---|
| 1 | el guard de **contradicción** | §4: se registró un folio sobre un estado contradictorio |
| 2 | la comparación **folio de Wasabil ≠ tecleado** | §3: se escribió 9002 cuando Wasabil decía 7777 |
| 3 | el chequeo de **claim en la re-lectura bajo lock** | §8b: se escribió el folio bajo los pies de una emisión naciendo |
| A | el guard SII de **entrada** al factoring | §1: la cesión sin folio pasó con 200 |
| B | el cruce **puerta = inversa del guard** | §5: **se borró un factoring REAL** |
| C | borrar la fila → marcarla `'revertida'` | §4: la factura siguió imborrable |

La §8b merece una nota: no simula la carrera con un flag, la **provoca** — durante la consulta a
Wasabil, otra **sesión de base de datos** (= otro request) marca el claim y commitea. Es el único
punto donde la re-lectura bajo lock puede verlo, y sin ella el folio se escribe igual.

Los fakes reproducen el estado **adverso** (Wasabil caído, folio distinto, dos emitidos, lista
truncada, abono ya conciliado), no el cómodo. Ninguna sonda verifica leyendo el código fuente.

---

## 10. Fuera de alcance

- **MachParts**: no se tocó. Su `registrar-folio` ya existía; su `revertir_factoring` también.
- **Nota de crédito**: sigue sin existir en ninguna de las dos marcas. Es otra cosa y más grande.
- El **candado de empresa entre marcas** sigue diferido por decisión del dueño.
