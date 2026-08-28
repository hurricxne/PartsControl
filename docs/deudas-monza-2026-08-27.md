# Deudas registradas de MonzaParts — cierre del 2026-08-27

Lo que **no** se arregló en la jornada de siete rondas de auditoría, con su análisis y el
motivo de la decisión. Cada una está reproducida: no son sospechas.

---

## 1. Dos fichas del mismo cliente si se crean en el MISMO instante

**Qué pasa.** El dedupe es «buscar y, si no aparece, crear». Cuatro creaciones simultáneas
del mismo RUT miran las cuatro antes de que ninguna haya insertado, no encuentran nada, y
crean dos fichas del mismo contribuyente. Reproducido 3 de 3 veces con 4 peticiones
disparadas en el mismo milisegundo. Ocurre también por el webhook de Nexor cuando reintenta
por timeout mientras la primera entrega sigue corriendo.

**Por qué no se arregló.** Se intentó, y el intento se revirtió a propósito. Un candado de
aplicación (`GET_LOCK`) serializa la decisión, pero la ficha nueva **solo se hace visible
al confirmar la transacción**, que ocurre mucho después de soltar el candado: la segunda
petición entra, no ve nada y crea la ficha igual. Se midió: seguían saliendo 2 fichas.
Dejarlo puesto habría sido peor que no tenerlo — un guard que aparenta proteger y no
protege es exactamente lo que esta casa evita.

Un índice único tampoco sirve tal cual: `monza_clientes.rut` admite vacío (hay fichas
legítimas sin RUT), así que el único rechazaría la segunda ficha sin RUT que se cree.

**Qué haría falta.** Sostener el candado hasta después del commit, lo que significa
reestructurar la transacción de creación del lead — o un índice único sobre una columna
nueva de RUT canónico que admita NULL (NULL no colisiona en MySQL) más su migración de
backfill. Ninguna de las dos es un cambio pequeño.

**Cuánto duele.** La consecuencia es una ficha duplicada: el LTV y el historial del cliente
quedan partidos en dos. Es molesto y **reparable a mano**, y no hay plata mal calculada ni
documento tributario incorrecto. La ventana es de milisegundos y exige que dos personas
creen el mismo cliente nuevo exactamente a la vez.

---

## 2. `auth.py` devuelve 500 en vez de 401 ante un token malformado

`get_current_user` hace `int(sub)` sin protección, así que una sesión con un token
adulterado produce un error de servidor en lugar de «no autorizado». Se detectó en vivo.
**No se tocó** porque `auth.py` es código compartido por las dos marcas y el dueño no
autorizó modificarlo en este corte.

---

## 3. Aislamiento entre marcas: CUATRO módulos abiertos, en los DOS sentidos

**PENDIENTE POR DECISIÓN DEL DUEÑO (2026-08-27).** Lo revisó, lo entendió y decidió dejarlo
para más adelante. No se toca sin su visto bueno.

Verificado ejecutando, con usuarios de una marca contra los módulos de la otra:

| Sentido | Módulo | Qué queda expuesto |
|---|---|---|
| Grupo AM → Monza | `monza_router_abastecimiento` | OC de proveedor, proveedores, costos por línea. **Y ESCRIBE**: crear/editar/**borrar** proveedor, crear OC, comprar, preparar embarque — ninguno candado |
| Monza → Grupo AM | `routers/clientes.py` | **La cartera completa de minería**: RUT, nombre, contacto, email |
| Monza → Grupo AM | `routers/cotizaciones.py` | Cotizaciones con cliente, referencia y montos |
| Monza → Grupo AM | `routers/ventas.py` | Ventas con cliente, RUT y referencias |

Bloqueados y verificados: contabilidad, despachos, bodega y compras de Grupo AM (candado
`mineria`), y las seis pantallas de Monza candadas el 2026-08-27.

**Cómo se llega.** No hay ningún enlace en el menú: una cuenta no ve botones de la otra
marca. Pero `PrivateRoute` (frontend-src/src/App.tsx:41-50) solo comprueba que la sesión
exista — NO la empresa — así que la dirección escrita a mano carga la pantalla, y como el
router del backend tampoco canda, los datos salen. No hace falta nada técnico: es escribir
una URL.

**El sentido más expuesto es Monza → Grupo AM**, no el que se sospechaba: la cartera de
clientes de la marca que factura de verdad queda visible para cualquiera del equipo Monza.

**Cómo se cierra, si se aprueba.** Cuatro líneas, una por router, del mismo tipo que las
seis puestas el 2026-08-27: `dependencies=[Depends(require_empresa("..."))]` en la
declaración del `APIRouter`, con `"mineria"` para los tres de Grupo AM y `"automotriz"`
para el de Monza. SIEMPRE el router completo, nunca endpoint por endpoint, para no dejar
mitades. Los cuatro son código del programador original.

**Un arreglo de fondo alternativo** (más grande, pero mata la clase entera): que
`PrivateRoute` compare la empresa del usuario contra el prefijo de la ruta, de modo que un
router nuevo nazca cerrado en vez de nacer abierto. Hoy el candado es opt-in y por eso se
olvida.

---

## 4. La vía de compra más usada no reintenta el correlativo

El índice único nuevo (`monza_unique_correlativos`) convirtió una corrupción silenciosa —dos
OC con el mismo número, sin error ni log— en un error visible. La rama de compra PARCIAL
tiene reintento; la vía NORMAL no, porque envolverla toca un router del programador. Es un
cambio de fallo deliberado: entre corromper la numeración y perder una operación de forma
ruidosa, se eligió lo segundo. Está anotado en el checklist de deploy.

---

## 5. La reversa del LTV no tiene puerta por API

Solo se puede revertir anulando el despacho. Si un cierre se hizo por error y el despacho ya
está cerrado, corregir el LTV requiere intervención manual en la base.

---

## 6. Trampa latente: `populate_existing()` con `autoflush=False`

`populate_existing()` DESCARTA los cambios pendientes del mismo objeto. Hoy no rompe nada
porque en `_crear_lead_tx` el `flush` ocurre antes del lock, pero **si alguien mueve ese lock
más arriba, el RUT que el operador acaba de teclear se pierde en silencio**. Queda anotado
acá porque es del tipo de cosa que no falla hasta que falla.

---

## 7. `update_cliente` reintenta con el helper compartido — y eso basta

Su bucle escrito a mano no tenía el candado `after_commit`, así que un choque de locks
justo al escribir el log habría devuelto un 409 falso por una vinculación que SÍ ocurrió.
Se cambió por el helper compartido. Queda anotado como referencia de que el reintento
NUNCA se escribe a mano en este módulo.

---

## 8. Menores de experiencia de uso

Los cuatro botones de acción de cada fila del listado (Llamada, WhatsApp, Nota, Agendar) no
hacen nada; «Completar un próximo paso» y «borrar un ítem» no avisan si el backend falla
(pero tampoco mienten: la fila sigue en su lugar); y el filtro «Míos» de un vendedor sin
asesor vinculado devuelve una lista vacía sin explicar que falta un dato maestro.
