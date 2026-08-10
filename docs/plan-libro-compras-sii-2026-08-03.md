# Especificación — Libro de compras del SII → costo por venta (MachParts)

**Fecha:** 2026-08-03 · **Marca:** GRUPO AM SPA / MachParts (Wasabil id 2757, RUT 77.977.813-4)
**Veredicto:** VIABLE_CON_CAMBIOS · **Origen:** enjambre de 11 agentes (4 reconocimiento + 4 lentes contables + síntesis + 2 auditores adversariales)

> Documento de PLANIFICACIÓN. No se ha escrito ni una línea de código.
>
> **[YA NO — leer el ADDENDUM 2026-08-08 del final antes de usar este documento para decidir
> nada, y en particular su §4 y su §6.]** Las Fases 0, A1 y A2 están CONSTRUIDAS y corriendo
> en las dos marcas, y el módulo pasó después por tres rondas de revisión adversarial que
> cambiaron ~19 comportamientos. El cuerpo de abajo sigue siendo la mejor explicación de POR
> QUÉ el módulo es así; el CÓMO, en los puntos que el addendum enumera, quedó viejo.
>
> **Orden de autoridad, de mayor a menor: §6 → §4 → el resto del addendum → el cuerpo.** El
> §6 se escribió en una segunda pasada (2026-08-08) verificando el código línea por línea, y
> corrige catorce afirmaciones que el propio addendum seguía haciendo mal. Ahí está, además,
> la única forma de saber sin abrir el código en qué grupo cae cada cosa: **construida**,
> **planificada y no construida**, o **construida distinto de como está escrita acá**.
>
> **Tres trampas concretas, para el lector apurado:** (1) la Regla 5 dice «barrido completo,
> SIN ventana por fecha» y el barrido SÍ usa una ventana de 24 meses; (2) el bloque CONFIG
> del modelo de datos declara cuatro variables de configuración que **nunca se crearon**; y
> (3) la Fase B —Reglas 12 a 23— sigue sin existir, con la excepción de dos pedazos (la
> clasificación por RUT y el bloqueo de las notas de crédito) que sí corren.

---

## Resumen ejecutivo

Las dos fases son construibles y valen la pena, pero no en el orden ni con la forma en que se plantearon. Hay un bloqueo previo real: el programa hoy no tiene forma de PEDIRLE a Wasabil la lista de facturas recibidas — el camino que usa el sistema (GET /documents) responde "método no permitido", y el camino que sí funciona (POST) es justamente el que EMITE documentos, así que está prohibido tantearlo. Antes de escribir una línea de código hay que preguntarle a Wasabil cuál es la ruta correcta de listado. El segundo cambio de forma es que hoy el ERP no guarda el RUT del proveedor en ninguna parte (la tabla de proveedores ni siquiera tiene la columna, verificado), y sin RUT el informe "qué facturas existen ante el SII y no están en el sistema" va a acusar como faltantes facturas que ya están cargadas, y el operador las va a cargar de nuevo: el remedio sería peor que la enfermedad. El tercero es que un embarque hoy admite como máximo 6 líneas de gasto (una por categoría) mientras que un solo proveedor emite varias facturas por embarque — FastAir emitió tres el mismo día 22-07-2026 —, así que el vínculo factura↔gasto necesita una tabla nueva, no una columna nueva. Con esos tres arreglos, la Fase A se entrega sola y ya responde la pregunta de negocio, y la Fase B elimina el retecleo que hizo que el módulo se abandonara. Dos advertencias de alcance que conviene decir ahora: el libro del SII sólo trae proveedores CHILENOS (la factura del proveedor extranjero de los repuestos nunca va a aparecer) y no trae el IVA de la importación (el DIN), que suele ser el crédito más grande del mes. Y el "sello" en Wasabil para ver el costo por embarque desde allá es lo más frágil de todo: los tres campos personalizados de la cuenta son listas de opciones VACÍAS y nunca se usó ninguno en 24 meses, así que va detrás de un interruptor apagado y se enciende recién después de una prueba manual con usted mirando.

---

## Convergencias — hallazgos a los que llegaron lentes distintos por caminos distintos (11)

> Es lo que más pesa: no el volumen de hallazgos, sino que varias miradas independientes choquen con lo mismo.

### C1. El backend no tiene por dónde LISTAR documentos recibidos: GET /documents responde 405 y POST en esa ruta EMITE un DTE real

**Lentes:** recon:costos-hoy, recon:patrones-a-reusar, recon:api-recibidos, recon:api-metafields, contador:ciclo-de-vida (5 de 8, por caminos distintos: prueba HTTP en vivo con el token de producción, lectura del código, y el comentario fechado del propio cliente)

Es el único bloqueante ABSOLUTO de la Fase A y de la B: sin listado no hay espejo. Verificado en backend/wasabil_dte/client.py:19 y :248-253 («⚠️ PENDIENTE (2026-07-17): GET /documents responde 405 en el API real (solo acepta POST)»), y client.py:153-160 muestra que POST /documents es crear_documento, o sea EMITIR. Prohibido descubrir la ruta por prueba y error. Los datos que las 8 lentes usaron salieron por el canal MCP (canal de agente), que NO es el transporte de un job de FastAPI. Además el proyecto ya pagó este hueco: la memoria registra que el cinturón anti doble emisión «no bloqueaba NUNCA en producción — GET /documents da 405», es decir un guard que fallaba ABIERTO.

### C2. El grano no calza: el pricing es CATEGORÍA-grano (6 líneas fijas) y el SII es DOCUMENTO-grano (N facturas por categoría)

**Lentes:** recon:costos-hoy, recon:api-recibidos, recon:api-metafields, contador:costos, contador:antifraude-duplicados, contador:ciclo-de-vida (6 de 8; unos llegaron leyendo el UNIQUE de la base, otros contando facturas reales del mismo proveedor en el mismo día)

Verificado en backend/embarques_pricing/models.py:79-83 (UniqueConstraint('pricing_id','tipo', name='uq_emb_pricing_gasto_tipo')) y :88 (6 tipos fijos). Contra eso: FastAir emitió 3 documentos el 2026-07-22 (folios 1205849 y 1205943 entre ellos) y 17 facturas + 1 nota de crédito en 12 meses. Consecuencia: el vínculo NO puede ser una columna wasabil_uuid en la línea de gasto — la tercera factura no tiene dónde ir y el operador vuelve a sumar a mano, que es exactamente el problema que la Fase B viene a resolver. Obliga a una tabla puente N:1.

### C3. No existe RUT de proveedor en ninguna parte del ERP: la conciliación de Fase A no tiene llave de cruce y va a mentir el día uno

**Lentes:** recon:costos-hoy, contador:antifraude-duplicados, contador:costos, contador:tributario (4 de 8; unos leyendo el modelo, otros el information_schema real, otros el índice anti-duplicado)

Verificado por mí: backend/models/models.py:303-317 — la clase Proveedor tiene id, nombre, pais, moneda, contacto, email, telefono, sitio_web, notas, tipo, created_at, updated_at y NINGUNA columna rut (la clase Cliente en :320 sí tiene). El único RUT del sistema es cont_compra.proveedor_rut, texto libre nullable tecleado a mano, y es la primera columna del UNIQUE anti-duplicado (compras_contab/models.py:34-36). Wasabil entrega los RUT formateados ('76.513.680-6'); un humano teclea '76513680-6'. Sin RUT canónico normalizado, el reporte «existe en el SII y no en el ERP» produce falsos faltantes, y el remedio natural del operador (cargar la factura desde el documento) ES el doble conteo.

### C4. Las notas de crédito ya existen, el ERP las rechaza por diseño, y su monto viene POSITIVO con el signo en un campo aparte

**Lentes:** contador:tributario, contador:costos, contador:antifraude-duplicados, contador:ciclo-de-vida (4 de 8; unos por el ge=0 del schema, otros contando NC reales, otros comparando la ficha del documento contra el reporte agregado)

Hay 11 NC recibidas por −$14.134.010 en 12 meses (18 por −$114.463.015 en 36 meses). El ERP no puede absorberlas: compras_contab/schemas.py:61-62 (monto_neto ge=0) y embarques_pricing/router.py:844 + _validar_gastos_no_negativos. Y la trampa doble: la ficha del documento trae sent_ntotal POSITIVO con trx_sign:-1 (NC folio 4 de TRANSPORTES PYP: +45.220.000 con trx_sign -1), mientras el reporte agregado ya viene firmado. Un espejo que copie sent_ntotal crudo convierte cada NC en un CARGO: error del doble. Apenas se encienda el espejo aparecen 11 documentos que el sistema no sabe representar.

### C5. El write-back del metafield es un camino NUNCA ejercitado y probablemente cerrado tal como está la cuenta

**Lentes:** recon:costos-hoy, recon:api-metafields, contador:tributario, contador:ciclo-de-vida, contador:antifraude-duplicados (5 de 8)

Reverificado por mí en vivo (get_metafield_definitions, company 2757): las tres definiciones — Categoría, Centro de Costos, Proyecto — son type='options' con options=null, o sea listas de opciones VACÍAS. Y en 24 meses hay CERO documentos con metafield puesto (0 de 741). Además no existe herramienta para EDITAR ni BORRAR una definición: create_metafield_definition es idempotente para el mismo key+type pero «conflicts with a different type fail», así que el nombre y el tipo se eligen UNA vez y quedan para siempre. Conclusión: la promesa «sacar el costo por embarque desde Wasabil mismo» no puede ser precondición de nada; el costo por embarque se calcula en PartsControl y el sello es sólo una vista de conveniencia.

### C6. Nunca borrar y reinsertar: la PK de la línea de gasto es una LLAVE DE PLATA y su borrado desengancha la Cuenta por Pagar en silencio

**Lentes:** recon:patrones-a-reusar, recon:api-metafields, contador:antifraude-duplicados, contador:ciclo-de-vida (4 de 8)

Verificado por mí en backend/embarques_pricing/models.py:60-79: el docstring documenta que cont_compra.emb_pricing_gasto_id referencia esa PK con ON DELETE SET NULL y que borrar la fila permite volver a cargar el mismo gasto (Σ CxP duplicada: 380.800 por una factura de 190.400, reproducido en tests/test_llave_gasto_estable.py). Un job de sincronización escrito de la forma ingenua («borro los gastos del embarque y reinserto lo que dice Wasabil») dispara ese bug de forma masiva y automática. La misma disciplina se traslada al espejo: el barrido nunca emite DELETE, marca DESAPARECIDO.

### C7. La sincronización NO puede escribir montos en la línea de gasto: choca de frente con el guard que ya existe

**Lentes:** recon:costos-hoy, contador:ciclo-de-vida, contador:antifraude-duplicados (3 de 8)

_bloqueo_monto_gasto_con_cxp (embarques_pricing/router.py:398-470, invocado en el PUT :969-973 y en el cierre :1098-1101) devuelve 409 ante cualquier cambio de monto de una línea con CxP activa, con tolerancia de 1 peso, y su docstring explica por qué NO propaga: «reescribiría en silencio un pasivo que ya puede estar pagado, conciliado con el banco y en el F29». Un resync automático tiene dos destinos y los dos son malos: por el router se cae con 409 todas las noches; por SQL directo produce exactamente el pasivo falso que el guard previene, y cerrar_pricing lo CONGELA. Solución que sale de la convergencia: el espejo es una PROPUESTA y el monto viaja a la línea sólo cuando un humano aprieta «aplicar», por el MISMO PUT, con el 409 intacto.

### C8. Hay plata grande en el libro que NUNCA debe tocar el costo del inventario: factoring, intercompañía y gasto general

**Lentes:** contador:tributario, contador:costos, contador:antifraude-duplicados (3 de 8)

VECTOR CAPITAL CORREDORES DE BOLSA (76.513.680-6) es el proveedor #1 del libro: $550.857.712 en 23 facturas EXENTAS. Es gasto financiero (NIC 23 / NIC 2.17: los costos por préstamos no capitalizan salvo activo apto, y un repuesto de reventa no lo es). Si entra a la línea «Otros» de un embarque, se prorratea por CIF a TODOS los ítems (service.py:108, :144-146) y multiplica el costo unitario que alimenta el precio. Y si alguien le aplica el botón «+19%» del frontend (EmbarquesPricingPage.tsx:135) inventa ~$104,6 millones de crédito fiscal que nadie recargó. Aparte: LOPEZ HERNANDEZ INVERSIONES (78.121.316-0, MonzaParts) $45.049.221 es intercompañía, y el grueso del libro son peajes, telefonía y seguros — 131 receptores distintos en 14 meses.

### C9. Los documentos recibidos NO traen detalle de líneas ni estado de aceptación: hay dos promesas que el sistema no puede cumplir

**Lentes:** recon:api-recibidos, contador:tributario, contador:costos, contador:ciclo-de-vida (4 de 8)

Los 397 detalles de los 389 documentos dicen todos «Detalle no disponible», con has_document_xml=false y has_document_pdf=false. Por lo tanto la clasificación (¿esto capitaliza? ¿a qué embarque va?) es necesariamente HUMANA: el sistema sabe cuánto y de quién, nunca para qué. Y exchange_status es null en los 553 documentos de 24 meses: el espejo puede decir «existe ante el SII y no está en el ERP», JAMÁS «está aceptada» ni controlar el plazo de 8 días para reclamar. Diseñar un estado que dependa de la aceptación sería construir otro guard que parece vigilar y no vigila nada.

### C10. El libro llega RETROACTIVO y en bloque: cualquier sincronización incremental por fecha pierde documentos en silencio

**Lentes:** contador:tributario, contador:ciclo-de-vida, contador:antifraude-duplicados (3 de 8)

382 de los 389 documentos entraron en un solo lote (julio 2026) cubriendo fechas desde 2025-08-01; hay documentos con fecha 2025-09-23 y 2025-12-03 ingresados el 2026-07-14, o sea 7 a 10 meses tarde. Y facturas de FastAir del 2026-07-22 con updated_at del 2026-08-02 (11 días). Una ventana «últimos 60 días» los habría perdido PARA SIEMPRE. El volumen no justifica optimizar: 553 documentos en 24 meses ≈ 23 por mes, unas 6 páginas. El barrido completo nocturno es más barato que el bug.

### C11. En un documento recibido, los campos receiver_* contienen al PROVEEDOR, no a Grupo AM

**Lentes:** recon:api-recibidos, contador:ciclo-de-vida (2 de 8, uno por conteo masivo y otro por inspección de un documento con su bloque supplier)

Ninguno de los 389 documentos trae el RUT propio (77.977.813-4) en receiver_rut; en cambio receiver_rut coincide exactamente con supplier.rut cuando el supplier existe. Y en las notas de crédito el bloque supplier viene NULL, así que receiver_rut es la ÚNICA identidad del emisor justo en los documentos que restan plata. Si alguien codea «receiver = mi empresa», el módulo queda al revés y no se nota hasta producción.


---

## Reglas de negocio (26)

> **Estado real de estas 26 reglas — leer el §4 del ADDENDUM 2026-08-08 ANTES que esta
> tabla.** Lo que viene abajo es la redacción ORIGINAL del plan: dice lo que se QUERÍA, no
> lo que el código hace hoy. Tres advertencias para que nadie lea de más:
>
> · Varias reglas se construyeron **DISTINTO** de como están redactadas acá. Las tres que
>   más engañan: la 5 dice «barrido completo, SIN ventana por fecha» y el barrido SÍ
>   consulta una ventana de 24 meses hacia atrás; la 7 fija una lista de campos vigilados
>   que no es la que el código vigila; y el estado del espejo terminó teniendo dos valores
>   (el documento existe hoy ante el SII, o dejó de venir) en vez de los nueve del dibujo.
> · Varias se construyeron **A MEDIAS o NO se construyeron**: la 4 sólo en su mitad de
>   identidad (la marca de duplicado sospechoso no existe), la 11 entera (el RUT del
>   proveedor sigue siendo opcional al crear una cuenta por pagar), y de las 12 a 23 —la
>   Fase B— sólo dos pedazos.
> · Ninguna regla se **derogó**. Lo que cambió, cambió con motivo, y el motivo está escrito.
>
> El §4 del addendum clasifica cada regla en tres pilas —CONSTRUIDA / NO CONSTRUIDA /
> CONSTRUIDA DISTINTO— y el §6 desarrolla los casos que no caben en una celda de tabla.

| # | Sev. | Regla | Riesgo que evita | Cómo se prueba |
|---|---|---|---|---|
| 1 | CRITICA | El backend NUNCA descubre el endpoint de listado por prueba y error. La ruta y el verbo del listado de documentos recibidos se confirman POR ESCRITO con Wasabil y se implementan en un único archivo cliente. Está PROHIBIDO que cualquier función del módulo nuevo llame a POST /documents (es la ruta que emite un DTE real e irreversible). | Emitir un documento tributario real por accidente mientras se busca cómo listar. backend/wasabil_dte/client.py:153-160 (crear_documento = POST /documents) frente a client.py:248-253 (GET /documents = 405). | Sonda ESTRUCTURAL (patrón ya usado en wasabil_dte/tests/test_fecha_guia_papel.py:171-190): inspect.getsource() sobre TODOS los módulos nuevos, filtrando comentarios, verificando que no aparezca ninguno de crear_documento, 'POST' junto a '/documents', set_document_metafields, ni cualquier verbo de escritura. El test falla si alguien agrega una llamada de escritura. |
| 2 | CRITICA | Una lectura INCOMPLETA nunca produce conclusiones. Si Σ(ítems recorridos) != el campo `total` de la respuesta, o si alguna página falla, la corrida se ABORTA entera, se marca la sincronización como FALLIDA y no se marca a ningún documento como desaparecido ni se afirma que falta nada. **[Lo primero SÍ; lo último NO: tras una corrida fallida, la bandeja y la exportación siguen repartiendo «está / no está» sobre el espejo viejo, sin fecha de corte — §6.D.1]** | Acusar falsamente de omisión contable. Es el contrato que el repo ya tiene: client.py:238-284 devuelve (docs, completo) y el llamador no puede concluir con completo=False. Además el servidor recorta perPage en silencio (pedir 500 devolvió lo mismo que 250), así que confiar en el tamaño de página es un error. | Fake del cliente que devuelve página 1 con total=100 y luego falla en la página 2 → el reporte de faltantes devuelve estado INDETERMINADO, cero documentos marcados DESAPARECIDO, y la fila de la corrida queda en FALLIDA. Sonda de control: con el fake completo el mismo reporte SÍ afirma. |
| 3 | CRITICA | El reporte de Fase A tiene TRES respuestas, no dos: ESTÁ EN EL ERP / NO ESTÁ EN EL ERP / NO PUDE DETERMINARLO **[esto SÍ]**. La pantalla las distingue visualmente y muestra siempre «datos del SII hasta <fecha del último barrido exitoso>» **[esto NO: la pantalla muestra la ANTIGÜEDAD del último barrido exitoso («hace 6 h») y, aparte, la fecha del último INTENTO, que puede ser de una corrida fallida; y el archivo exportado no lleva fecha de corte — §6.D.2]**. | El bug más caro del proyecto: un guard con dos veredictos donde el «no pude preguntar» caía en el lado permisivo produjo 7 dobles emisiones reales (wasabil_dte/router.py:546-560, constantes VERD_SIN_EMITIDO / VERD_HAY_EMITIDO / VERD_INDETERMINADO). Acá el equivalente es que el operador cargue a mano una factura que sólo estaba atrasada. | Test con tres documentos: uno conciliado, uno realmente ausente, y uno cuya lectura quedó incompleta → la respuesta del endpoint tiene los tres en cubetas distintas y el tercero NO aparece bajo «falta en el ERP». |
| 4 | CRITICA | La identidad técnica del documento espejado es su `uuid`, con UNIQUE DURO. La llave de negocio (rut_emisor_normalizado, tipo_dte, folio) lleva índice NO único; si aparece colisión, ambas filas se marcan DUPLICADO_SOSPECHOSO y quedan bloqueadas para vincular hasta que un humano elija. **[NO CONSTRUIDO de la coma en adelante: el índice existe, pero la marca de duplicado sospechoso no está en el código y nadie busca la colisión — §6.B.3]** | Dos daños opuestos: sin UNIQUE, dos corridas concurrentes insertan el mismo documento (READ COMMITTED sin gap locks — backend/database.py:28-29 y :34 lo dejan escrito: «la unicidad va SIEMPRE en índice UNIQUE + captura de IntegrityError»); con UNIQUE duro sobre la llave de negocio, un doble ingreso del lado de Wasabil aborta el barrido entero con error 1062. Empíricamente hoy la llave natural es limpia (389 de 389 sin colisión), pero el folio SOLO colisiona 7 veces: jamás deduplicar por folio. | (a) Correr el barrido dos veces sobre el mismo payload → mismo número de filas, ningún IntegrityError propagado. (b) Sembrar dos documentos con distinto uuid y misma (rut,tipo,folio) → ambos quedan DUPLICADO_SOSPECHOSO, el barrido termina OK, y el endpoint de vincular devuelve 409 sobre cualquiera de los dos. |
| 5 | CRITICA | El barrido es COMPLETO todas las noches, sin ventana por fecha de documento **[FALSO HOY: el barrido SÍ consulta una ventana de 24 meses, y lo anterior a eso no se vuelve a leer nunca — §6.C.1]**, y es idempotente: N corridas = 1 corrida. Sólo hace UPSERT por uuid; jamás DELETE. El documento que deja de venir se marca DESAPARECIDO. | Perder para siempre los documentos que llegan con meses de retraso (verificado: fechas de 2025-09 y 2025-12 ingresadas en julio 2026), y perder los vínculos y las marcas de IGNORADO si un fallo de paginación vacía el espejo. Es la llave de plata un nivel más arriba. | (a) Correr el barrido 3 veces seguidas y comparar un volcado de la tabla: idéntico byte a byte salvo timestamps de sync. (b) Sembrar un documento VINCULADO, correr un barrido cuyo fake no lo incluye → la fila sigue existiendo, con estado DESAPARECIDO, y su vínculo intacto. (c) Sonda de poder discriminante: quitar el UPSERT y poner delete+insert → (b) debe fallar. |
| 6 | CRITICA | Los campos del documento (folio, RUT, tipo, fecha, montos, estado, signo) los manda SIEMPRE Wasabil y se sobrescriben ciegamente en cada barrido: son caché de una realidad ajena. Las DECISIONES locales (estado del espejo, vínculo, monto aplicado, CxP creada, marca de ignorado, snapshot congelado) las escribe SÓLO un humano y el barrido NO las toca jamás. Físicamente son dos zonas de columnas y ninguna función escribe en las dos. | Aplicar mal el «piso monótono» de wasabil_dte/router.py:890-961. Ahí PartsControl es el EMISOR y por eso su dato no se degrada; acá es una TERCERA copia de un documento de un tercero y no tiene voto sobre los hechos — defender un monto viejo contra el SII sería el error espejo. El espíritu del piso sobrevive, pero aplicado a la juntura: lo que nunca se degrada no es el dato del documento sino la decisión tomada sobre él. | Test: documento VINCULADO a un gasto; el barrido trae montos distintos → los campos w_* cambian, y estado_espejo, gasto_id y monto_aplicado quedan EXACTAMENTE iguales. Sonda estructural: grep sobre la función de barrido verificando que no asigna ninguna columna de la zona local. |
| 7 | CRITICA | Cuando un documento cambia DESPUÉS de que se decidió sobre él, el sistema NO repara solo: pasa a DIVERGENTE y avisa. La detección es un hash de una lista EXPLÍCITA de campos remotos (sent_nsubtotal, sent_niva, sent_nexempt, sent_ntotal, folio, document_date, sii_document_type_id, status_id, situation_id, trx_sign). Si el estado es NUEVO o IGNORADO se sobrescribe callado; si es VINCULADO o superior, DIVERGENTE. **[LISTA VIEJA: la real no vigila la «situación» y sí vigila el RUT y el NOMBRE del emisor y el acuse de recibo; y el documento IGNORADO tampoco se sobrescribe callado — §6.C.4]** | Que un costo ya prorrateado y congelado (y quizá ya usado para fijar precio) cambie de valor sin que nadie lo sepa. Hash de lista explícita y no del payload completo, para que un cambio cosmético del API no dispare divergencias falsas. | Tres casos: (a) documento NUEVO cuyo monto cambia → se actualiza y sigue NUEVO; (b) documento CONTABILIZADO cuyo monto cambia → DIVERGENTE + notificación, CxP y línea intactas; (c) documento CONTABILIZADO cuyo campo `updated_at` cambia pero ningún campo de la lista → NO divergente (sonda anti-falso-positivo). |
| 8 | ALTA | El espejo guarda EXCLUSIVAMENTE los montos `sent_*` (sent_nsubtotal, sent_niva, sent_nexempt, sent_ntotal). Los `current_*` no se guardan ni se muestran. | `current_*` es una recalculación de Wasabil con decimales (verificado: 63.262 × 0,19 = 12.019,78 = current_niva, contra sent_niva 12.020); `sent_*` es lo declarado al SII y lo que cuadra con el F29. La diferencia es siempre menor a 1 peso, o sea que caería DENTRO de TOL_CLP = 1.0 de _bloqueo_monto_gasto_con_cxp y el guard NO la atraparía: es un error que pasa silencioso. | Fake con current_niva 12019.78 / sent_niva 12020 → la fila del espejo guarda 12020 y la propuesta a la línea de gasto es 12020. Sonda estructural: ninguna consulta del módulo menciona current_. |
| 9 | CRITICA | Todo monto se guarda junto con `trx_sign` y existe UNA sola magnitud sumable: monto_efectivo = sent_ntotal × trx_sign. Ninguna consulta del módulo suma sent_ntotal crudo. | Convertir cada nota de crédito en un cargo. La ficha del documento trae el total POSITIVO con el signo aparte (NC folio 4 de TRANSPORTES PYP: 45.220.000 con trx_sign −1) mientras los reportes ya vienen firmados: mezclarlos produce un error del doble por documento. | Sembrar el libro con 2 facturas y 1 NC de fakes conocidos y verificar que el total del espejo para el período da exactamente la resta. Test de cuadratura sobre datos reales: la suma de 12 meses debe dar 1.267.460.692 (o el número que fije la Decisión 8), nunca 1.295.728.712. |
| 10 | ALTA | La identidad del proveedor del documento se toma SIEMPRE de receiver_rut / receiver_name, normalizando el RUT (sin puntos, con guion, DV en mayúscula). `supplier_id` y el bloque `supplier` son accesorios y pueden venir NULL. Se agrega una aserción defensiva: si un documento marcado received=true trae receiver_rut == 77.977.813-4, se aborta la ingesta de esa fila y se avisa. **[NO CONSTRUIDA, y además estaba mal planteada: en un documento RECIBIDO ese campo trae al PROVEEDOR, nunca al propio — lo que separa hoy los libros de las dos marcas es que cada una usa su propio token de Wasabil. §6.D.3]** | Dos bugs que sólo aparecen en producción: leer el módulo al revés (el «receptor» es el proveedor) y quedarse sin proveedor exactamente en las notas de crédito, que son las que restan plata (supplier NULL en las 3 inspeccionadas). El normalizador ya existe: backend/wasabil_dte/client.py:287-289; hay que extraerlo a un helper compartido. | Fake con una NC de supplier=null → el proveedor se resuelve igual. Fake con receiver_rut = el RUT propio → la fila no se ingresa y queda registrado el aviso. Test del normalizador: '76.513.680-6', '76513680-6' y '76513680-K' minúscula colapsan al mismo canónico. |
| 11 | CRITICA | La tabla `proveedores` gana columna `rut` canónica **[esto SÍ se hizo]**, y `cont_compra.proveedor_rut` pasa a OBLIGATORIO y normalizado para todo lo NUEVO **[esto NO: el RUT sigue siendo opcional y se guarda tal como se teclea — §6.B.2]**. No hay backfill automático de lo histórico: se repara a mano o se deja como está (regla de oro del proyecto, igual que el TC congelado). | Sin RUT canónico el cruce ERP↔SII sólo se puede hacer por nombre o por monto, y ambos fallan: en el mismo libro conviven 'Latam Airlines Group S.A.' y 'LATAM AIRLINES GROUP S.A.' con el mismo RUT. Y el UNIQUE anti-duplicado (cont_compra empresa+proveedor_rut+numero_documento_activo) NO protege con RUT NULL, porque los NULL no colisionan en MySQL — está escrito en compras_contab/models.py:31-33. | (a) Intentar crear una CxP nueva sin RUT → 422. (b) Crear una CxP con '76.513.680-6' y otra con '76513680-6', mismo folio → la segunda choca con IntegrityError traducido a 409. Sonda de poder discriminante: quitar la normalización y el caso (b) debe pasar (que es el bug). |
| 12 | CRITICA | El vínculo documento↔gasto vive en una TABLA PUENTE nueva (N documentos → 1 línea de gasto). El `monto_neto` y el `iva` de `emb_pricing_gasto` pasan a ser DERIVADOS (suma de los documentos aplicados) y de sólo lectura en pantalla cuando la línea tiene documentos. Está PROHIBIDO agregar una columna de uuid de Wasabil a emb_pricing_gasto. | Que la segunda y tercera factura del mismo proveedor y categoría se pierdan en silencio (subvalúa el costo landed) o se tecleen encima de la primera. Verificado: UniqueConstraint('pricing_id','tipo') en models.py:79-83, 6 tipos fijos, y FastAir con 3 documentos el mismo día. | Vincular 3 documentos a la línea 'almacenaje' de un mismo embarque → la línea queda con la suma de los tres, los tres siguen individualmente consultables, y el UNIQUE (pricing_id,tipo) sigue intacto (una sola fila de almacenaje). |
| 13 | CRITICA | La sincronización NUNCA escribe en `emb_pricing_gasto`. El espejo produce una PROPUESTA; el monto viaja a la línea sólo por acción humana explícita, a través del MISMO PUT del router, con `_bloqueo_monto_gasto_con_cxp` intacto. Ninguna función nueva escribe en cont_compra ni en emb_pricing_gasto por SQL directo. | Los dos únicos destinos de un resync que pisa montos: caerse con 409 todas las noches, o producir el pasivo falso que el guard existe para impedir (embarques_pricing/router.py:398-470; el caso medido fue línea 476.000 contra pasivo 190.400, con la anulación bloqueada por el pago ya registrado). | Sonda estructural: inspect.getsource() sobre el módulo nuevo verificando que no asigna EmbarquePricingGasto.monto_neto ni .iva. Test funcional: barrido con montos nuevos sobre una línea con CxP activa → la línea NO cambia, aparece un aviso, y ningún 409 se propaga a la corrida. |
| 14 | CRITICA | El mapeo de montos al aplicar es: monto_neto ← sent_nsubtotal (que YA incluye el exento) e iva ← sent_niva. Nunca sent_ntotal en monto_neto. El espejo guarda además sent_nexempt por separado para uso tributario. | Inflar el costo landed exactamente 19% y congelarlo al cerrar. Aritmética verificada en los 389 documentos: sent_ntotal = sent_nsubtotal + sent_niva, y sent_niva = 19% × (sent_nsubtotal − sent_nexempt), cero filas fuera. Y hay 8 facturas MIXTAS reales (afecto + exento en el mismo documento), como DHL folio 4800177 con exento 141.483 e IVA 3.690, donde aplicar «19% del neto» inventa crédito fiscal. El exento SÍ capitaliza (el flete internacional es exento por Art. 12 E N°2 DL 825 y es el corazón del CIF), así que va dentro del neto que prorratea: no se agrega columna nueva a emb_pricing_gasto. | Aplicar una factura afecta de neto 100.000 / IVA 19.000 y verificar que el costo landed sube 100.000, no 119.000. Aplicar la factura mixta de DHL y verificar que la línea queda con IVA 3.690 y no con 26.882. Sonda de control: forzar el mapeo a sent_ntotal y el primer caso debe fallar. |
| 15 | ALTA | Cuando la línea tiene documento aplicado, el IVA es DATO y no cálculo: se deshabilitan el botón «+19%» del frontend y el autocálculo neto×0,19 del backend para esa línea. Un documento tipo 34 (exenta) fuerza iva = 0 y bloquea cualquier edición que lo suba. | Declarar crédito fiscal por un IVA que el emisor nunca recargó (Art. 23 N°1 DL 825 da derecho sólo por el IVA RECARGADO). Hay 81 facturas exentas por $567.817.834 en 12 meses: el 43% del libro. El botón está en EmbarquesPricingPage.tsx:135 y el autocálculo en compras_contab/router.py:665. | Aplicar un documento tipo 34 a una línea → el input de IVA queda deshabilitado en 0 y un PUT que intente ponerle 19% devuelve 409. |
| 16 | CRITICA | Existe una clasificación por RUT de emisor con tres niveles: BLOQUEADO (no puede vincularse a ningún embarque con capitaliza=True — factoring, bancos, corredoras) **[construido como candado duro: bloquea clasificar ese documento como costo por venta]**, IGNORAR_AUTO (gasto general recurrente: peajes, telefonía, seguros — se archiva solo en la bandeja) **[NO archiva solo: su único efecto real es sacar a ese proveedor del cruce con el banco — §6.B.4]** y LOGÍSTICO (proveedor de embarque, aparece primero) **[no aparece primero: la bandeja se ordena por fecha, y no hay filtro por nivel]**. Los tres niveles son visibles, reversibles y con registro de quién los puso. | (a) Que $550.857.712 de factoring se prorrateen por CIF a todos los ítems de un embarque, multiplicando el costo unitario que alimenta el precio de venta (service.py:108, :144-146). (b) Que la bandeja nazca con 553 filas indistinguibles (131 receptores distintos en 14 meses, con Santander, Costanera Norte y Telefónica en el top) y el operador la abandone en dos semanas — el mismo destino que ya tuvo el módulo de gastos por engorroso. | Intentar vincular un documento de 76.513.680-6 a una línea capitalizable → 409 nombrando el motivo. Un documento de un RUT en IGNORAR_AUTO entra directo a IGNORADO sin pasar por la bandeja. Un documento de un RUT desconocido aparece en la bandeja con la marca «proveedor no clasificado». |
| 17 | ALTA | Existe un guard de magnitud: vincular a un embarque un documento cuyo neto supere un porcentaje configurable del CIF de ese embarque exige confirmación explícita con motivo, registrada con usuario. | Errores de un orden de magnitud que la lista negra no cubre (un proveedor nuevo, un tecleo de más ceros). Un almacenaje que vale más que la carga es un error, no un dato. | Vincular un documento de $550.000.000 a un embarque con CIF $20.000.000 → 409 pidiendo confirmación; con la confirmación y motivo, se registra y pasa. |
| 18 | ALTA | El acreedor y el RUT de una CxP de gasto de embarque vienen del DOCUMENTO del SII, nunca de la OC de mercadería. Mientras no haya documento vinculado, el campo se pide explícitamente y no se adivina. Se corrige de aquí en adelante, sin backfill. | Verificado por mí en compras_contab/router.py:838-844 y :860: el overlay resuelve el acreedor con OcProveedor.proveedor vía EmbarqueItem, o sea el proveedor EXTRANJERO de los repuestos, y usa setdefault (el primero que aparezca). Eso pone en cont_compra.acreedor un nombre que nunca emitió un DTE chileno, y ese campo es una de las dos patas del anti-duplicado por factura física (router.py:550-560): con el acreedor mal, el guard compara peras con manzanas, y en la conciliación esas CxP aparecen eternamente como «en el ERP y no en el SII». | Crear una CxP desde una línea con documento vinculado → acreedor y RUT coinciden exactamente con receiver_name / receiver_rut del documento, no con OcProveedor.proveedor. Sonda de control: sin documento, el prefill de acreedor viene VACÍO (no con el proveedor de la mercadería). |
| 19 | ALTA | La fecha de la CxP viene de `document_date` del documento del SII y queda de SOLO LECTURA cuando hay documento vinculado. El espejo guarda tres fechas distintas y no las deriva una de otra: fecha_emision (del SII, inmutable), fecha_primera_vista (propia del ERP) y fecha_aplicacion. | Hoy la CxP nace con fecha = HOY porque el Prefill no lleva la fecha (ComprasContabPage.tsx:61-65 y :78), aunque el dato ya estaba tecleado dos pantallas antes. Eso corre el vencimiento, el aging y el período tributario. El crédito fiscal se imputa por fecha de EMISIÓN, con dos períodos de gracia (Art. 24 inc. 3 DL 825); el costo se devenga cuando el bien se incorpora (NIC 2.10-11). Son ejes distintos y una sola fecha los colapsa. Además hoy la fecha queda editable a mano incluso después de registrada la CxP (embarques_pricing/router.py:436-438). | Aplicar un documento con document_date 2026-06-03 y crear la CxP el 2026-07-25 → la compra queda con fecha 2026-06-03 y el vencimiento se calcula desde ahí. Un PUT que intente cambiar la fecha de una línea con documento vinculado devuelve 409. |
| 20 | CRITICA | Las notas de crédito (tipo 61) y las notas de débito (56) se ESPEJAN y se muestran, pero NO se pueden vincular directamente a un embarque en esta entrega: se vinculan al documento que corrigen, resuelto por `related_to` (id interno de Wasabil) con fallback al par (document_type, folio) de references[]. El campo references[].document se IGNORA explícitamente. Hasta que exista la decisión del dueño (Decisión 5), el endpoint de vincular una NC devuelve 409 con motivo. **[El BLOQUEO sí está construido —una nota de crédito no se puede pre-llenar como compra ni clasificar como costo por venta, y en los dos casos se explica por qué—, pero el ENLACE con la factura que corrige no: esos datos del documento padre no se guardan en ninguna columna del espejo.]** | Dos daños. (a) Verificado por la lente de ciclo de vida: references[0].document es AUTORREFERENCIAL (la NC 20260700133683 se apunta a sí misma; el padre está en related_to = 20260700133675) — enlazar por ahí crea un ciclo y deja la factura original sin su crédito aplicado, o sea el costo del embarque inflado. (b) Un 409 explícito es infinitamente mejor que un ge=0 que devuelve 422 sin explicar, o que un guard que no existe: el ERP no puede representar una NC hoy (schemas.py:61-62, router.py:844) y hay 11 esperando. | Fake de NC con references[0].document = su propio id y related_to = otro → la NC queda enlazada al padre correcto, y el intento de vincularla a un embarque devuelve 409 con el motivo nombrado. Sonda de control: usar references[].document produciría un ciclo, el test debe detectarlo. |
| 21 | ALTA | Desvincular un documento se permite sólo en estado VINCULADO. Con CxP activa (CONTABILIZADO) o con el pricing cerrado (CONGELADO) devuelve 409 nombrando la salida exacta: «anule primero la CxP» o «reabra el pricing». Al desvincular SIEMPRE se encola el borrado del sello en Wasabil. | Dejar una cont_compra viva apuntando a una línea cuyo respaldo documental desapareció, y dejar un sello obsoleto en Wasabil que siga contando esa factura dentro de un embarque del que ya no forma parte — que es justamente el número que el dueño iría a mirar. Un sello obsoleto es PEOR que ninguno. Sigue el patrón de 409-con-salida-nombrada que el módulo ya usa (embarques_pricing/router.py:459-469). | Tres casos, uno por estado, verificando el código de respuesta y que el mensaje nombre la salida. Y que tras un desvínculo exitoso hay exactamente una fila BORRAR en la bandeja de salida del sello. |
| 22 | ALTA | El espejo NO cuelga del embarque: vive solo. Su FK a la línea de gasto es ON DELETE SET NULL y, además, al vincular se guardan denormalizados el pricing_id y el número de embarque. Cada barrido busca las filas con estado ≥VINCULADO y gasto_id IS NULL, las marca HUÉRFANO y encola el borrado de su sello. | emb_pricing.embarque_id es ON DELETE CASCADE: borrar un embarque se lleva el pricing y sus 6 gastos, y el SET NULL desengancha en SILENCIO (init_db.py:95-99 ya lo advierte). Sin el chequeo de huérfanos, borrar un embarque deja sellos mintiendo en Wasabil para siempre y filas del espejo que creen estar vinculadas a la nada. | Vincular, borrar el embarque, correr el barrido → la fila del espejo existe, está en HUÉRFANO, conserva el número de embarque original y tiene un BORRAR encolado. |
| 23 | ALTA | El sello del metafield en Wasabil va detrás del flag WASABIL_SELLO_HABILITADO, declarado en backend/config.py con default OFF, y se escribe por BANDEJA DE SALIDA asincrónica con reintentos y backoff. Una falla del sello NUNCA revierte el vínculo local, pero SIEMPRE queda visible: la fila pasa a FALLIDO y se cuenta en el tablero. Prohibido el try/except silencioso y prohibido llamar a Wasabil dentro del endpoint de vincular. | Dos lecciones en tensión, resueltas por clasificación: el sello NO es un guard (no protege ninguna decisión, no es irreversible, no es precondición de nada, y su ausencia no produce un solo número equivocado), así que un parpadeo de red no puede tirar a la basura el trabajo real del operador. PERO lo que hundió al cinturón anti doble emisión no fue fallar sino fallar INVISIBLEMENTE. Además: una variable en .env sin declarar en config.py tumba el backend (trampa ya documentada en la memoria del proyecto, Monza F5). | Fake del cliente que lanza WasabilError(ambiguo=True) → el endpoint devuelve 200, el vínculo está hecho, hay una fila en la bandeja con intentos=1 y proximo_intento_at futuro. Con ambiguo=False y 4xx de validación → FALLIDO inmediato, sin reintentos. Con el flag OFF → la bandeja no se llena y no hay ninguna llamada al cliente. |
| 24 | ALTA | El tablero muestra la EDAD del último barrido exitoso, calculada desde la tabla de corridas, y se pone en ROJO pasadas 48 horas. No es un flag que el barrido pone en verde al terminar. | La forma número uno en que estas integraciones se pudren: el barrido deja de correr y nadie se entera, porque una pantalla sin novedades se ve igual que una pantalla al día. Un flag que el propio barrido enciende nunca se apaga si el barrido nunca arranca; la edad, en cambio, crece sola. Un espejo que no avisa que está viejo miente. | Sembrar una última corrida exitosa de hace 72 horas sin correr nada → el endpoint del tablero devuelve estado ROJO. Sonda de control: implementarlo con un flag booleano y este test debe fallar. |
| 25 | ALTA | Todos los routers nuevos nacen con dependencies=[Depends(require_empresa("mineria"))] en la propia línea del constructor **[esto SÍ, en los cuatro routers de las dos marcas]**, y toda tabla nueva lleva columna `empresa` con server_default 'mineria' **[esto NO: ninguna de las tres tablas del libro la tiene — el aislamiento se resolvió por paquete, por tabla y por token; §6.C.3]** y motor InnoDB explícito. | El libro que se espeja es el de GRUPO AM SPA (77.977.813-4) y contiene facturas de LOPEZ HERNANDEZ INVERSIONES (MonzaParts), o sea intercompañía visible. Un usuario de la otra marca no puede verlo. Patrón exacto: backend/empresa_guard.py:17-31 y wasabil_dte/router.py:62-66. InnoDB explícito porque los locks lo requieren (wasabil_dte/models.py:62-63). | TestClient con dependency_override de un usuario empresa='automotriz' contra cada endpoint nuevo → 403 en todos. Sonda anti-deriva: recorrer las rutas del router y verificar que ninguna quedó sin la dependencia. |
| 26 | MEDIA | Cada fila del espejo guarda el JSON crudo del último barrido que la tocó, recortado a 60.000 caracteres, y cada corrida deja una fila con inicio, fin, páginas, vistos, creados, actualizados, divergentes, huérfanos, desaparecidos y el error si lo hubo. **[La bitácora real NO guarda páginas, ni divergentes, ni huérfanos — §6.C.6]** El token nunca se registra. | No poder diagnosticar un documento raro sin credenciales de Wasabil, y no poder responder «¿desde cuándo está roto?». El precedente es exacto: wasabil_dte/models.py:96-98 (payload_json / respuesta_json «Trazabilidad: qué se envió y qué respondió Wasabil») y router.py:925 (recorte a 60000). Se guarda SIEMPRE, también en el camino feliz: el bug que importa diagnosticar es el que nadie vio pasar. | Tras un barrido exitoso, toda fila tocada tiene w_payload_json no vacío y existe exactamente una fila de corrida con los contadores cuadrando contra la cantidad de documentos del fake. |

---

## Modelo de datos

RAÍZ: /Users/aldoantonioibacetavasquez/Desktop/Empresas/Grupo AM SpA/Repuestos /GRUPO AM/CONTABILIDAD/PartsControl/Parts control actual/PartsControl-main/

Paquetes nuevos, aislados, con la anatomía del repo (README.md, __init__.py, client.py, models.py, service.py, router.py, init_db.py, tests/):
  backend/sii_libro_compras/     ← Fase A (espejo, barrido, bandeja, reporte)
  backend/sii_libro_embarque/    ← Fase B (puente documento↔gasto, sello)

**[OJO — esos dos nombres NO existen en el repo.** Lo que se construyó se llama
backend/wasabil_compras/ (Grupo AM) y backend/monza_wasabil_compras/ (MonzaParts), y varios
nombres de columna de abajo tampoco son los reales. El mapa nombre-del-plan → nombre-real
está en el ADDENDUM 2026-08-08 §1.
**Y hay un dato del modelo de abajo que es falso en las TRES tablas del libro: la columna
`empresa`.** El plan la pone en cada tabla nueva («empresa VARCHAR(50) NOT NULL con valor
por defecto 'mineria'»), y NINGUNA de las tres tablas del espejo la tiene, en ninguna de las
dos marcas. La separación entre marcas se resolvió de otra forma —cada marca tiene su
propio paquete, sus propias tablas (las de MonzaParts llevan el prefijo `monza_`) y su
propio candado de marca en la puerta de entrada del API— y el porqué está en el §6.C.3.
La única tabla del módulo que sí lleva esa columna es la del matcher banco↔libro de Grupo
AM, que nació de otra especificación.]
Nota de estilo: el router de wasabil_dte tiene 2403 líneas, muy por encima del máximo de 800 de las reglas del usuario — es el archivo del que NO hay que copiar el tamaño. Partir el router por superficie (barrido / bandeja / reporte).

═══ TABLAS NUEVAS ═══

1) sii_libro_doc — el espejo del libro de compras. Una fila por documento recibido.
   ZONA REMOTA (sólo la escribe el barrido; sobrescritura ciega — Regla 6):
     id INT PK
     empresa VARCHAR(50) NOT NULL server_default 'mineria'
     w_uuid VARCHAR(64) NOT NULL              ← identidad técnica
     w_document_id VARCHAR(32)                ← id interno de Wasabil (YYYYMM+secuencia; su prefijo delata el lote de ingesta)
     w_rut_emisor VARCHAR(20) NOT NULL        ← de receiver_rut, NORMALIZADO (Regla 10)
     w_rut_emisor_fmt VARCHAR(20)             ← como lo entrega Wasabil, para mostrar
     w_nombre_emisor VARCHAR(255)             ← de receiver_name
     w_tipo_dte SMALLINT NOT NULL             ← sii_document_type_id (33/34/61/56/...)
     w_folio VARCHAR(30) NOT NULL             ← string SIEMPRE (get_documents lo da string, get_document_status entero)
     w_fecha_emision DATE NOT NULL            ← document_date
     w_neto NUMERIC(16,2)                     ← sent_nsubtotal (INCLUYE el exento)
     w_exento NUMERIC(16,2)                   ← sent_nexempt
     w_iva NUMERIC(16,2)                      ← sent_niva
     w_total NUMERIC(16,2)                    ← sent_ntotal (POSITIVO siempre)
     w_trx_sign SMALLINT NOT NULL DEFAULT 1   ← +1 factura / −1 nota de crédito (Regla 9)
     w_status_id SMALLINT NULL
     w_situacion VARCHAR(30) NULL
     w_related_to VARCHAR(32) NULL            ← padre de una NC (NUNCA references[].document — Regla 20)
     w_ref_tipo SMALLINT NULL, w_ref_folio VARCHAR(30) NULL   ← fallback del padre
     w_payment_method VARCHAR(30) NULL
     w_payload_json TEXT                      ← crudo, [:60000] (Regla 26)
     w_hash VARCHAR(64)                       ← hash de la lista explícita de campos (Regla 7)
   ZONA LOCAL (sólo la escribe un humano; el barrido NO la toca — Regla 6):
     estado_espejo VARCHAR(24) NOT NULL DEFAULT 'NUEVO'
         NUEVO | IGNORADO | VINCULADO | CONTABILIZADO | CONGELADO
         | DIVERGENTE | HUERFANO | DESAPARECIDO | DUPLICADO_SOSPECHOSO
     motivo_estado VARCHAR(255) NULL
     usuario_id INT NULL FK users ON DELETE SET NULL
     decidido_at DATETIME NULL
   OPERATIVAS:
     visto_en_ultimo_barrido BOOLEAN DEFAULT TRUE
     primera_vista_at DATETIME, ultima_sync_at DATETIME
   __table_args__:
     UniqueConstraint('w_uuid', name='uq_sii_libro_doc_uuid')                     ← DURO (Regla 4)
     Index('ix_sii_libro_doc_natural', 'empresa','w_rut_emisor','w_tipo_dte','w_folio')  ← NO único (Regla 4)
     Index('ix_sii_libro_doc_estado', 'empresa','estado_espejo')
     Index('ix_sii_libro_doc_fecha', 'empresa','w_fecha_emision')
     {"mysql_engine": "InnoDB"}
   PROPIEDAD DERIVADA (única magnitud sumable): monto_efectivo = w_total * w_trx_sign

TRANSICIONES DE estado_espejo (el barrido SÓLO puede provocar tres: ∅→NUEVO, →DIVERGENTE, →DESAPARECIDO):
  ∅ →(barrido) NUEVO
  NUEVO ↔(humano) IGNORADO
  NUEVO →(humano vincula) VINCULADO
  VINCULADO →(humano desvincula) NUEVO   [encola BORRAR del sello]
  VINCULADO →(POST /compras) CONTABILIZADO
  CONTABILIZADO →(anular CxP) VINCULADO
  CONTABILIZADO →(cerrar_pricing) CONGELADO
  CONGELADO →(reabrir_pricing) CONTABILIZADO
  ≥VINCULADO + w_hash cambió →(barrido) DIVERGENTE     [sólo sale por humano]
  ≥VINCULADO + gasto_id IS NULL →(chequeo) HUERFANO    [sólo sale por humano]
  no visto en el barrido →(barrido) DESAPARECIDO

2) sii_libro_sync_run — una fila por corrida del barrido.
   id, empresa, inicio DATETIME, fin DATETIME NULL, ok BOOLEAN NULL,
   paginas INT, total_informado INT, vistos INT, creados INT, actualizados INT,
   divergentes INT, huerfanos INT, desaparecidos INT, error TEXT NULL
   Index('ix_sii_sync_run_ok', 'empresa','ok','inicio')   ← el tablero lee de acá la EDAD (Regla 24)

3) sii_proveedor_regla — clasificación por RUT (Regla 16).
   id, empresa, rut_normalizado VARCHAR(20), nombre_ref VARCHAR(255),
   nivel VARCHAR(20)  ← BLOQUEADO | IGNORAR_AUTO | LOGISTICO
   tipo_gasto_sugerido VARCHAR(30) NULL   ← desconsolidacion|almacenaje|agencia|arancel|otros
   es_relacionado BOOLEAN DEFAULT FALSE   ← intercompañía (Decisión 4)
   motivo VARCHAR(255), usuario_id, created_at
   UniqueConstraint('empresa','rut_normalizado')
   Semilla propuesta (confirmar con el dueño — Decisión 3):
     BLOQUEADO: 76.513.680-6 VECTOR CAPITAL (factoring)
     LOGISTICO: 96.631.520-2 FastAir, 76.629.600-9 SAMEX, 76.780.738-4 BODEGAS MAQUIRENT,
                78.903.460-5 AG.AD. RICARDO CANCINO, 76.147.894-K DACHSER,
                78.958.160-6 TRANSPORTES FASTMARK, 89.862.200-2 LATAM
     es_relacionado: 78.121.316-0 LOPEZ HERNANDEZ INVERSIONES (MonzaParts)

**[OJO — la tabla 3 se construyó, pero con OTRA forma.** Se llama `sii_libro_regla_rut`
(y `monza_sii_libro_regla_rut` en MonzaParts) y guarda: el RUT del emisor ya normalizado —
sin puntos, con guion, que es la llave con la que se cruza contra el ERP—, el nivel
(BLOQUEADO / IGNORAR_AUTO / LOGISTICO), un destino sugerido por defecto, el motivo escrito
a mano, quién la creó y las fechas de creación y de última edición. Es una regla POR RUT en
todo el sistema: la unicidad es sobre el RUT solo, no sobre la pareja empresa+RUT (ver el
OJO de la columna `empresa` más arriba).
**Tres campos del dibujo NO existen:** el nombre de referencia del proveedor (se muestra el
que trae el propio documento del SII), el tipo de gasto sugerido, y —el que importa— la
marca de **parte relacionada** (intercompañía) que el dueño aprobó en la Decisión D4. Esa
marca no está construida en ninguna tabla: hoy una factura de la empresa hermana se
clasifica como cualquier otra y nada la distingue en un informe. Queda como deuda
declarada, no como algo hecho — ver §6.B.5.]

4) emb_gasto_documento — LA TABLA PUENTE (Fase B, Regla 12). N documentos → 1 línea de gasto.
   id INT PK
   empresa VARCHAR(50) NOT NULL server_default 'mineria'
   sii_libro_doc_id INT NOT NULL FK sii_libro_doc ON DELETE RESTRICT
   emb_pricing_gasto_id INT NULL FK emb_pricing_gasto ON DELETE SET NULL
   pricing_id INT NULL          ← DENORMALIZADO, sobrevive al SET NULL (Regla 22)
   embarque_numero VARCHAR(50)  ← DENORMALIZADO, idem
   monto_neto_asignado NUMERIC(16,2) NOT NULL   ← parte de ESTE embarque (Decisión 2)
   monto_iva_asignado NUMERIC(16,2) NOT NULL
   aplicado BOOLEAN DEFAULT FALSE               ← si su monto ya viajó a la línea vía el PUT
   snapshot_nro_factura VARCHAR(100) NULL       ← lo que la línea tenía ANTES (patrón Tesorería,
   snapshot_fecha_factura VARCHAR(30) NULL         tesoreria/models.py:154-159): desvincular RESTAURA
   snapshot_monto_neto NUMERIC(16,2) NULL
   snapshot_iva NUMERIC(16,2) NULL
   usuario_id INT NULL, created_at
   __table_args__:
     UniqueConstraint('sii_libro_doc_id','emb_pricing_gasto_id', name='uq_emb_gasto_doc_par')
     Index('ix_emb_gasto_doc_gasto','emb_pricing_gasto_id')
     Index('ix_emb_gasto_doc_doc','sii_libro_doc_id')
     {"mysql_engine": "InnoDB"}
   INVARIANTE bajo lock: Σ monto_neto_asignado de un documento ≤ w_neto del documento (tolerancia 1 CLP,
   la TOL_CLP de la casa). El residuo no asignado debe imputarse explícitamente a gasto del período,
   nunca quedar como diferencia muda.
   Si la Decisión 2 es "1 documento = 1 embarque": agregar UniqueConstraint('sii_libro_doc_id') y
   monto_asignado deja de ser editable (= w_neto). El resto del modelo no cambia.

5) sii_sello_outbox — bandeja de salida del metafield (Regla 23).
   id, sii_libro_doc_id FK, accion VARCHAR(10) ← PONER|BORRAR, metafield_key VARCHAR(64),
   valor VARCHAR(64), estado VARCHAR(16) ← PENDIENTE|OK|FALLIDO,
   intentos INT DEFAULT 0, ultimo_error TEXT NULL, proximo_intento_at DATETIME, created_at
   Index('ix_sii_sello_pend','estado','proximo_intento_at')

═══ ALTERS ADITIVOS SOBRE TABLAS EXISTENTES (mínimos, ninguno destructivo) ═══
  proveedores      + rut VARCHAR(20) NULL, + Index('ix_proveedores_rut','rut')   (Regla 11)
                     — verificado que hoy NO existe: models/models.py:303-317
  cont_compra      + tipo_dte SMALLINT NULL          ← código SII crudo
                   + sii_libro_doc_id INT NULL FK sii_libro_doc ON DELETE SET NULL
                   + monto_exento NUMERIC(16,2) DEFAULT 0
                   + es_relacionado BOOLEAN DEFAULT FALSE
                   **[OJO — NINGUNO de estos cuatro se agregó.** La tabla de cuentas por
                   pagar quedó exactamente como estaba: no guarda el tipo de documento del
                   SII, no guarda a qué documento del libro corresponde, no separa el monto
                   exento, y no marca las facturas de la empresa hermana. Se pueden agregar
                   cuando llegue la Fase B; hoy el vínculo entre una compra del ERP y un
                   documento del libro se calcula al vuelo comparando RUT y N° de
                   documento, no está guardado en ninguna parte. Lo ÚNICO que sí se aplicó
                   de este bloque es la columna `rut` de la tabla de proveedores, con su
                   script de migración — ver §6.C.5.]
                   NOTA: agregar tipo_dte al UNIQUE existente (empresa, proveedor_rut,
                   numero_documento_activo) es un cambio de índice, NO aditivo: va en una fase
                   propia y con el chequeo previo de duplicados vivos (el patrón de
                   embarques_pricing/init_db.py:85-99, que consulta ANTES de emitir el ALTER
                   porque con duplicados MySQL responde 1062 y tumba toda la migración).
  emb_pricing_gasto  SIN CAMBIOS. Deliberado: monto_neto ya recibe afecto+exento (Regla 14) y su
                   identidad (pricing_id, tipo) es la llave de plata que no se toca.

═══ CONFIG (backend/config.py — declarar SIEMPRE antes de tocar el .env) ═══
  SII_LIBRO_HABILITADO: bool = False
  SII_LIBRO_MESES_HISTORIA: int = 24
  WASABIL_SELLO_HABILITADO: bool = False
  WASABIL_SELLO_METAFIELD_KEY: str = ""

**[OJO — las CUATRO están ausentes. Ninguna se declaró nunca, y el módulo se construyó
igual, a propósito.** Qué hay en su lugar, una por una:
  · **SII_LIBRO_HABILITADO (interruptor del módulo): no existe y no se quiso.** El libro es
    lectura y decisión, no toca plata del ERP, así que no hay nada que apagar en una
    emergencia; el propio código lo dice en su primera línea («sin gate de feature»). El
    interruptor real de hecho es el token: sin token de Wasabil configurado, el barrido no
    corre y el módulo queda vacío pero en pie.
  · **SII_LIBRO_MESES_HISTORIA (cuántos meses se traen): no es configurable.** Los 24 meses
    son una constante escrita en el código del barrido, así que cambiarlos exige tocar el
    archivo y volver a desplegar. Y ojo con el detalle: la ventana se calcula como 24 × 31
    días, o sea unos 24 meses y medio corridos, no 24 meses de calendario.
  · **WASABIL_SELLO_HABILITADO y WASABIL_SELLO_METAFIELD_KEY: no existen porque el sello no
    existe.** Es Fase B (Regla 23) y esa fase no se construyó.
La advertencia de la Regla 23 sobre declarar la variable en config.py ANTES de escribirla en
el archivo de entorno —si no, el backend COMPLETO no arranca— sigue siendo verdad y hay que
respetarla el día que el sello se construya.]

═══ ECUACIONES DE CUADRATURA (el informe de una hoja las muestra en cero) ═══
  E1 INTEGRIDAD DEL DOCUMENTO: Σ monto_neto_asignado (todos los embarques) + no_asignado = w_neto
  E2 EL POZO:  total_gastos_capitaliza del embarque = Σ monto_neto_asignado de líneas capitaliza=True
  E3 EL PRORRATEO NO PIERDE PLATA: Σ gastos_clp de los ítems = total_gastos_capitaliza
     (exige redondeo con absorción de residuo al ítem de mayor CIF; hoy el pie suma valores ya
      redondeados — router.py:574 y :632 contra :618)
  E4 EL COSTO: Σ costo_total_clp = Σ fob_clp + Σ shipping_clp + Σ gastos_clp
  E5 CONTRA EL PASIVO: Σ (w_total × w_trx_sign) de los documentos vinculados = Σ monto_total_clp
     de las cont_compra activas ligadas
  E6 EL HUECO DE FASE A: documentos LOGISTICO del período sin asignación y sin marca de gasto
     del período → lista de excepciones. ESTE es el entregable de Fase A.

**[OJO — NINGUNA de estas seis ecuaciones existe, y el «informe de una hoja» que las
mostraría en cero tampoco.** Cinco de las seis (E1 a E5) se apoyan en la tabla puente
documento↔gasto de embarque, que es Fase B y no se construyó; E3 además exigía arreglar el
reparto de gastos entre ítems del embarque, que tampoco se tocó.
Lo que SÍ se construyó, y es otra cosa, es **una cuadratura mensual entre el libro del SII y
las cuentas por pagar del ERP**: mes a mes, cuánto suma el libro y cuánto suman las compras
registradas, con la resta que debe dar cero. Está en el tablero y pasó por su propia
corrección (§2.12: se descubrió que el lado del ERP sumaba en la moneda de cada documento,
así que una compra de 45.000 dólares aportaba «45.000 pesos», y que el lado del libro metía
notas de crédito y documentos ya marcados como ignorados contra un ERP que no puede
registrarlos — por eso la resta no daba cero nunca).
Y la línea «E6 ES el entregable de Fase A» quedó vieja: el entregable que se construyó y que
el dueño usa es **la bandeja de las tres cubetas** (está en el ERP / no está / no se puede
determinar) con su exportación a planilla, no una lista de excepciones de asignación a
embarques. Ver §6.B.1.]

---

## Fases

### FASE 0 — Desbloqueo (sin pantallas, sin datos nuevos)

**Entrega:** Tres cosas, en este orden: (1) la ruta y el verbo REALES del listado de documentos recibidos, confirmados POR ESCRITO con Wasabil e implementados en el cliente nuevo, con paginación y validación contra el campo `total`; (2) el helper compartido de normalización de RUT extraído de wasabil_dte/client.py:287-289, la columna rut en `proveedores` y la obligatoriedad de proveedor_rut normalizado en toda CxP nueva; (3) la auditoría del esquema de producción para confirmar que uq_emb_pricing_gasto_tipo EXISTE (init_db.py:213-247 lo SALTA si encuentra duplicados vivos, y el script sale con rc=0). Además, dos consultas de dimensionamiento sobre la base de Hostinger: cuántos gastos con monto>0 no tienen nro_factura y cuántas cont_compra tienen proveedor_rut vacío — ese número justifica (o no) el resto del proyecto. NADA de esto se ve en pantalla y todo es prerrequisito duro.

**[OJO — el punto (2) se entregó A MEDIAS.** Sí se construyeron el normalizador de RUT (cada
marca tiene el suyo, dentro de su propio paquete: no hay un `backend/rut.py` compartido) y la
columna `rut` en la tabla de proveedores, con su script de migración para cada marca. Lo que
**NO** se construyó es la segunda mitad, que era la que protegía de verdad: **el RUT del
proveedor sigue siendo OPCIONAL al crear una cuenta por pagar, y se guarda tal cual se
teclea, sin normalizar.** Crear hoy una compra sin RUT entra sin ningún reclamo. Lo que se
puso en su lugar es una defensa distinta, en el momento de guardar: al alta se compara el
RUT normalizado y el folio «blando» (sin ceros a la izquierda ni prefijos) contra todas las
compras vivas, y si calza se responde con un rechazo que nombra la compra que ya existe. Es
mejor que nada y ataca el mismo daño (la cuenta por pagar duplicada), pero no es lo que la
Regla 11 prometía. Detalle y consecuencias en §6.B.2.]

**Archivos:** backend/sii_libro_compras/client.py (nuevo, único lugar que habla con el API), backend/rut.py (helper compartido nuevo), backend/models/models.py:303-317 (+ columna rut), backend/compras_contab/schemas.py:58-63 y router.py:712 (RUT obligatorio y normalizado), backend/sii_libro_compras/init_db.py, deploy/audit_schema.py (correr, no modificar)

**Validación:** El cliente lista 389 documentos de 12 meses contra el API real, en modo SOLO LECTURA, y la suma cuadra con el número que fije la Decisión 8. Sonda estructural: ninguna función del paquete nuevo referencia crear_documento ni POST /documents. Tests del normalizador de RUT (Regla 11). deploy/audit_schema.py sin 'UNIQUE FALTANTE'.

### FASE A1 — Espejo y barrido (sin pantalla de decisión todavía)

**Entrega:** Tabla sii_libro_doc, tabla sii_libro_sync_run, barrido COMPLETO nocturno colgado del job de las 06:00 que ya existe **[REVERTIDO — hoy es un job PROPIO a las 05:30; el porqué, en el ADDENDUM 2026-08-08 §1]** (respetando sus dos invariantes: sesión propia y try/except propio, para que un fallo del libro no deje a Grupo AM sin el resto de las alertas), botón 'sincronizar ahora', y el tablero de cinco números: faltantes, EDAD del último barrido exitoso (rojo a las 48 h), Σ libro del SII contra Σ CxP por mes, sellos pendientes (en 0 mientras el flag esté apagado) **[CAMBIADO — ese cuarto número NO es «sellos pendientes»: el sello es Fase B y no existe. En su lugar el tablero muestra los DOCUMENTOS PENDIENTES DE DECISIÓN, o sea cuántos documentos del libro nadie ha ignorado ni clasificado todavía — que es la cola de trabajo real del operador. Los cinco números que hay hoy son: cuántos documentos caen en cada cubeta (está / no está / no se puede determinar), la edad del último barrido exitoso, la cuadratura mensual libro-contra-ERP, los pendientes de decisión, y los divergentes más los desaparecidos.]** y documentos que cambiaron después de usarse. Reglas 1 a 10, 24, 25 y 26.

**Archivos:** backend/sii_libro_compras/{models,client,service,router,init_db}.py, backend/config.py (4 variables), backend/main.py (import + include_router con prefix='/api'), backend/scheduler.py (job), frontend-src/src/sii-libro/LibroComprasPage.tsx (sólo tablero, sin acciones)

**Validación:** Suite nueva con el molde de la casa (MARK, check/run/wrapper, fakes instalados DENTRO de run(), limpieza al entrar y al salir): idempotencia de 3 corridas, lectura incompleta que no concluye, signo de la nota de crédito, sent_* contra current_*, receiver_* como proveedor, NC con supplier NULL, y la sonda estructural anti-escritura. El gate es `pytest` PELADO desde backend/ (la lista de carpetas a mano miente: da 64 de 200).

### FASE A2 — Bandeja y reporte de faltantes (el entregable que el dueño pidió)

**Entrega:** Bandeja con las tres cubetas (está / no está / no pude determinarlo), acciones IGNORAR y CLASIFICAR con un clic, reglas persistentes por RUT en tres niveles (BLOQUEADO / IGNORAR_AUTO / LOGÍSTICO) con motivo y usuario, filtro 'sólo proveedores de embarque', y el reporte exportable 'facturas del SII que no están en el ERP'. Con esto la Fase A está COMPLETA y vale sola, aunque la Fase B nunca se construya. Reglas 3, 16 y 25.

**[OJO — se entregó todo salvo el FILTRO 'sólo proveedores de embarque', que no existe.** La
bandeja se puede filtrar por cubeta, por decisión (pendiente / ignorado / clasificado), por
RUT del emisor, por «sólo los que cambiaron después de que alguien decidió» y por si el
documento sigue vivo ante el SII o dejó de venir — pero **no** por el nivel de la regla del
proveedor. El nivel SÍ viaja y se ve en cada fila (la pantalla lo pinta como etiqueta), así
que se puede leer; lo que no se puede es pedirle a la lista que muestre sólo los logísticos.
Por lo mismo, la frase de la Regla 16 «el proveedor logístico aparece primero» tampoco
ocurre: **la bandeja se ordena por fecha del documento, de la más nueva a la más vieja.**
Es una comodidad que falta, no un riesgo — ver §6.B.4.]

**Archivos:** backend/sii_libro_compras/router.py (endpoints de bandeja y reglas), backend/sii_libro_compras/models.py (sii_proveedor_regla), frontend-src/src/sii-libro/LibroComprasPage.tsx, frontend-src/src/services/api.ts, frontend-src/src/components/DashboardLayout.tsx (menú Contabilidad)

**Validación:** Sobre datos reales: la bandeja arranca con los ~553 documentos y, tras cargar las reglas por RUT, quedan menos de 30 pendientes de decisión. El documento de factoring aparece en la bandeja pero el intento de vincularlo devuelve 409. Un documento con lectura incompleta no aparece jamás bajo 'falta en el ERP'.

### FASE B1 — Puente y PROPUESTA (todavía sin escribir la línea de gasto)

**Entrega:** Tabla emb_gasto_documento, endpoint de vincular/desvincular con todos los guards (lista negra, magnitud, estado del pricing, tope Σ asignado, NC bloqueada), y la pantalla de Embarques Pricing mostrando por línea los documentos vinculados y la SUMA PROPUESTA al lado del monto tecleado, con la diferencia calculada. Nada se escribe todavía en emb_pricing_gasto. Reglas 12, 13, 17, 20, 21 y 22.

**Archivos:** backend/sii_libro_embarque/{models,service,router,init_db}.py, frontend-src/src/embarques-pricing/EmbarquesPricingPage.tsx (bloque nuevo bajo la tabla de gastos, cabeceras 359-409 intactas)

**Validación:** 3 documentos de FastAir del mismo día vinculados a la línea 'almacenaje' → una sola fila de gasto, tres documentos consultables, UNIQUE (pricing_id,tipo) intacto. Sonda estructural: el paquete no asigna EmbarquePricingGasto.monto_neto ni .iva. Guards: 409 con factoring, 409 con pricing cerrado, 409 con NC, 409 al pasarse del tope Σ.

### FASE B2 — Aplicar: el fin del retecleo

**Entrega:** Botón 'aplicar documentos a la línea' que arma el payload y lo manda por el MISMO PUT del router (con _bloqueo_monto_gasto_con_cxp intacto), guardando antes el snapshot de lo que la línea tenía para poder deshacer. El IVA queda de sólo lectura y el botón '+19%' se apaga en las líneas con documento. Y el prefill de 'Registrar como compra' pasa a llevar TODO lo que hoy falta: fecha de emisión del SII, RUT normalizado, acreedor real (del documento, no de la OC de mercadería), tipo de DTE y monto exento. Reglas 14, 15, 18 y 19.

**Archivos:** backend/sii_libro_embarque/router.py, backend/compras_contab/router.py:838-866 (el overlay deja de derivar el acreedor de OcProveedor cuando hay documento), frontend-src/src/compras-contab/ComprasContabPage.tsx:61-92 y :755-759 (Prefill completo), frontend-src/src/embarques-pricing/EmbarquesPricingPage.tsx:135 (botón 19%)

**Validación:** Recorrido completo con documentos reales espejados: aplicar una factura afecta de 100.000/19.000 → el landed sube 100.000, no 119.000. La CxP nace con la fecha del SII y el RUT del emisor, no con hoy y en blanco. Aplicar sobre una línea con CxP activa → 409, la línea no cambia, aparece el aviso. Desvincular → la línea vuelve EXACTAMENTE a lo tecleado antes (snapshot). Sonda de poder discriminante en cada uno: quitar el arreglo y el test debe fallar.

### FASE B3 — Sello en Wasabil (detrás de un interruptor apagado)

**Entrega:** Bandeja de salida sii_sello_outbox con reintentos y backoff, worker que la procesa, contador en el tablero, y el borrado del sello al desvincular. Se entrega con WASABIL_SELLO_HABILITADO=False. Se enciende recién después de que el dueño cree la definición del metafield y de una prueba MANUAL sobre UN documento, con él mirando, verificando que el valor queda y que no se pierde nada más. Regla 23.

**Archivos:** backend/sii_libro_embarque/{models,service}.py, backend/sii_libro_embarque/client.py (única función de ESCRITURA de todo el proyecto), backend/config.py

**Validación:** Con el flag OFF: cero llamadas al cliente, bandeja vacía, todo lo demás funciona igual (ésta es la prueba de que el sello no es precondición de nada). Con el flag ON y fakes: ambiguo=True reintenta, ambiguo=False con 4xx queda FALLIDO y visible, desvincular encola BORRAR. Primera escritura real: manual, un documento, con confirmación explícita del dueño en el momento.


---

## Refutación adversarial

> Dos auditores independientes intentaron demoler la especificación. Ambos concluyeron **SOBREVIVE_CON_PARCHES**: la dirección es correcta, pero hay agujeros que hay que cerrar ANTES de construir.


### Auditor contable — veredicto SOBREVIVE_CON_PARCHES · 10 agujeros

#### [CRITICA] Regla 12 (N documentos → 1 línea de gasto) es IMPOSIBLE hoy: el backend permite UNA sola CxP activa por línea de gasto, con 409 duro

**Escenario concreto:** Verificado en el libro real (report_documents, empresa 2757, RUT 96.631.520-2): FastAir emitió el mismo día 3 facturas — folio 1205849 ($75.282 bruto / $63.262 neto), folio 1205943 ($129.241 / $108.606) y folio 1205944 ($98.462 / $82.741). Es el caso exacto que la Convergencia 2 usa para justificar la tabla puente. Se vinculan las tres a la línea 'almacenaje' del embarque, la línea queda en $254.609 netos (Regla 12) y el operador va a 'Registrar como compra'. La primera CxP (folio 1205849) entra bien. La segunda muere en backend/compras_contab/router.py:643-645: `dup_g` consulta cont_compra por emb_pricing_gasto_id activo y lanza 409 «Ese gasto de embarque ya está registrado como la compra #N; anúlela antes de volver a registrarlo». La tercera, igual. Resultado: $227.703 de facturas REALES del SII no pueden convertirse nunca en Cuenta por Pagar por la vía del módulo. Las dos únicas salidas del operador son las dos que la Fase B venía a eliminar: (a) tecleárselas a mano, y entonces nacen con emb_pricing_gasto_id NULL — que es literalmente el bug documentado en compras_contab/router.py:830-834 («en MySQL los NULL no colisionan… la factura del forwarder se cargaba 2 y 3 veces»); o (b) registrar UNA CxP por $302.985 brutos citando el folio 1205849, con lo cual el pasivo dice que el folio 1205849 vale $302.985 cuando el SII dice que vale $75.282, y el pago al proveedor sale citando un folio con un monto que no existe.

**Parche:** La CxP debe colgar del DOCUMENTO, no de la línea. Agregar `cont_compra.emb_gasto_documento_id` (FK a la tabla puente, ON DELETE SET NULL) y mover el candado `dup_g` de emb_pricing_gasto_id a esa columna nueva (misma mecánica: FOR UPDATE sobre la fila del puente + chequeo de duplicado activo). emb_pricing_gasto_id se conserva como pista de costeo, pero DEJA de ser la llave del anti-duplicado. Regla nueva: una línea de gasto con documentos vinculados no se registra como CxP — se registran sus documentos, uno por uno.

#### [CRITICA] El guard de monto (_bloqueo_monto_gasto_con_cxp) solo ve la PRIMERA CxP de la línea: con Regla 12 devuelve 409 para siempre y congela el embarque

**Escenario concreto:** backend/embarques_pricing/router.py:385-393 (`_pares_gasto_cxp`) hace `reg.setdefault(int(gid), (...))` recorriendo las CxP ordenadas por id ascendente: se queda con la PRIMERA y descarta el resto en silencio. Con el caso FastAir de arriba resuelto por el parche anterior (3 CxP colgando de la misma línea vía el puente), la línea 'almacenaje' vale $254.609 netos y `_pares_gasto_cxp` devuelve el par (línea $254.609, CxP folio 1205849 $63.262). El guard compara con TOL_CLP = 1 peso, no cuadra, y lanza 409 en DOS puertas: en el PUT (router.py:969-973) — o sea el mismo PUT por el que la Regla 13 manda viajar el monto, así que 'aplicar documentos' queda muerto — y en POST /cerrar (router.py:1098-1101), con el mensaje «No se puede CERRAR el pricing…». El embarque no se puede cerrar nunca. Y las salidas que el propio 409 nombra son inaplicables: 'deje la línea con el monto de la factura ya registrada' significa borrar $191.347 de costo real, y 'revierta la CxP' no arregla nada porque al volver a cargarla vuelve a pasar. Corolario contable: la ecuación E5 del modelo de datos (Σ w_total×trx_sign de los documentos vinculados = Σ monto_total_clp de las CxP activas) es matemáticamente incompatible con la Regla 12 tal como está el código — el informe de una hoja no puede mostrarla en cero.

**Parche:** `_pares_gasto_cxp` debe SUMAR todas las CxP activas de la línea (o de los documentos del puente), no quedarse con la primera: reemplazar el `setdefault` por acumulación (Σ neto_cxp, Σ bruto_cxp, lista de ids) y que el mensaje del 409 liste todas las compras involucradas. Con eso el guard vuelve a tener sentido: línea $254.609 contra Σ CxP $254.609 = cuadra. Sonda de poder discriminante obligatoria: dejar el setdefault y el test de 3 facturas debe fallar.

#### [CRITICA] La Decisión 2 recomendada (repartir una factura entre dos embarques) no puede generar el pasivo: lo bloquea el UNIQUE global de cont_compra

**Escenario concreto:** Verificado en el libro: BODEGAS MAQUIRENT (76.780.738-4) factura exactamente $4.000.000 neto + $760.000 IVA = $4.760.000 todos los meses (folios 1314, 1317, 1323, 1329, 1337, 1344, 1350, 1357, 1363, 1368, 1371 — 11 facturas idénticas verificadas). Es el ejemplo que la propia Decisión 2 usa para recomendar la opción (a). Se reparte el folio 1371 entre el Embarque 40 ($2.500.000) y el Embarque 41 ($1.500.000). La CxP del Emb 40 entra. La del Emb 41 muere dos veces: primero en backend/compras_contab/router.py:608-614 (chequeo explícito empresa + proveedor_rut + numero_documento + no anulado → 409 «Ya existe una compra con documento 1371 para este proveedor») y, si alguien saltara ese chequeo, en el UniqueConstraint('empresa','proveedor_rut','numero_documento_activo') de compras_contab/models.py:36-37, que es GLOBAL y no por embarque. Y la Regla 11 empeora esto a propósito: hoy muchos gastos pasan porque proveedor_rut viene NULL y los NULL no colisionan en MySQL; al volverlo obligatorio y normalizado, el UNIQUE pasa a morder SIEMPRE. Las dos ramas son malas: o el ERP registra $2.500.000 de pasivo por una factura de $4.760.000 (deuda subdeclarada $2.260.000 ese mes, y el pago real de $4.760.000 no se puede imputar), o se registra la factura completa en el Emb 40 y entonces el guard del hallazgo anterior compara línea $2.500.000 contra CxP $4.000.000 y bloquea el cierre del Emb 40 para siempre.

**Parche:** Si el dueño elige (a), el reparto NO puede llegar hasta la CxP: la factura genera UNA sola CxP por su total ($4.000.000 neto), colgada del documento (parche 1), y el reparto vive exclusivamente en emb_gasto_documento.monto_neto_asignado como base de COSTEO. O sea: el pasivo es del documento, el costo es del reparto, y son dos ejes distintos. Eso exige además que el guard de monto compare Σ monto_neto_asignado de TODOS los embarques contra la CxP, no la línea de un embarque contra la CxP. Si el dueño elige (b) — 1 documento = 1 embarque — nada de esto hace falta; por eso la Decisión 2 debe responderse ANTES de escribir la Fase B, no después.

#### [ALTA] El reporte de Fase A miente en cualquiera de sus dos configuraciones posibles, porque la especificación no dice qué documento queda CONTABILIZADO

**Escenario concreto:** El estado_espejo es POR DOCUMENTO (sii_libro_doc.estado_espejo) pero la CxP es una sola por línea. Con las 3 facturas FastAir ($75.282 + $129.241 + $98.462) la especificación no define quién pasa a CONTABILIZADO, y las dos lecturas posibles rompen el entregable. Lectura A — marcar los 3 porque la línea tiene CxP: el reporte «facturas del SII que no están en el ERP» dice que las tres están, cuando solo hay pasivo por $75.282; el informe esconde $227.703 de deuda no registrada, que es exactamente la pregunta de negocio que motivó la Fase A. Lectura B — marcar solo el que quedó en cont_compra.numero_documento: los folios 1205943 y 1205944 aparecen como FALTANTES todos los meses aunque su plata YA esté dentro del costo del embarque; el operador hace lo natural (cargarlas) y produce el doble conteo — y como esas CxP nacerían por la vía manual, con emb_pricing_gasto_id NULL, ningún dedup las ve. Nótese que el reporte E6 tampoco excluye las notas de crédito: las 11 NC recibidas (−$14.134.010) no se pueden registrar en el ERP (schemas.py:61-62, monto_neto ge=0) y por lo tanto van a figurar como 'faltantes' de forma permanente e inaccionable.

**Parche:** El estado CONTABILIZADO se deriva de la existencia de una CxP ligada a ESE documento (parche 1), nunca de la línea. Mientras el parche 1 no exista, la Fase A debe declarar una cuarta cubeta explícita: «la plata está en el costo del embarque pero no hay pasivo propio» — es un estado real y hay que nombrarlo, no repartirlo entre 'está' y 'no está'. Y E6 debe excluir tipo 61/56 de la lista de faltantes y mostrarlos en una sección propia 'notas de crédito que el ERP no puede representar', con su monto.

#### [ALTA] La nota de crédito que anula un mes completo ya existe en el libro, y la salida que la especificación ofrece pasa por una puerta sin guard que BORRA el costo congelado

**Escenario concreto:** Verificado, no hipotético: BODEGAS MAQUIRENT tiene la nota de crédito folio 95 por −$4.000.000 neto / −$760.000 IVA / −$4.760.000, exactamente el monto de una factura mensual completa. Secuencia real: la factura de $4.000.000 se vincula al embarque, se prorratea por CIF a TODOS los ítems (service.py:108 y :144-146), se cierra el pricing, se vende la mercadería. Llega la NC folio 95. Regla 20 la bloquea con 409 → el embarque conserva $4.000.000 de costo por un servicio íntegramente acreditado, y ese costo ya alimentó márgenes informados. El operador sigue la salida que le nombra la Regla 21 («reabra el pricing») y ahí no hay ninguna red: `_reabrir_pricing_tx` (backend/embarques_pricing/router.py:1124-1134) son cuatro líneas que ponen estado='calculado' sin motivo, sin usuario, sin registro y sin mirar si el snapshot ya se usó. Y al volver a cerrar, `_persist_snapshot` (router.py:671-673) hace `db.query(EmbarquePricingItem).filter(...).delete()` antes de reinsertar: el costo congelado con el que se valorizó lo vendido se sobrescribe sin dejar rastro. La Decisión 5 ve el problema pero recomienda la opción (c) con umbral, y NINGUNA de las seis fases implementa ni el umbral, ni el motivo obligatorio de reapertura, ni el historial del snapshot.

**Parche:** Tres cosas, todas dentro de embarques_pricing (que hoy no aparece en la lista de archivos de ninguna fase de la Fase B): (1) `_reabrir_pricing_tx` exige motivo obligatorio y registra usuario+timestamp, y devuelve 409 si el pricing tiene CxP pagadas o ítems ya despachados; (2) `_persist_snapshot` deja de borrar: versiona (snapshot_version + vigente boolean) para que el costo con el que se vendió siga siendo consultable; (3) mientras la Decisión 5 no esté respondida, la NC vinculada a un documento cuyo embarque está CERRADO no solo se bloquea: dispara una alerta con monto en el tablero, porque un bloqueo silencioso sobre −$4.760.000 es un costo inflado que nadie va a ir a buscar.

#### [ALTA] La lista negra de la Regla 16 está redactada solo contra líneas capitaliza=True y deja abierta la única línea que representa impuesto recuperable

**Escenario concreto:** La Regla 16 dice textual: BLOQUEADO = «no puede vincularse a ningún embarque con capitaliza=True». El catálogo tiene 6 tipos fijos y `iva_importacion` es capitaliza=False (embarques_pricing/service.py:141), y el backend PISA el campo desde el catálogo (router.py:992, `fila.capitaliza = cat['capitaliza']`), así que ese tipo es capitaliza=False siempre. Consecuencia literal: una factura de VECTOR CAPITAL — por ejemplo folio 115170, $37.080.000, exenta, verificada en el libro — SÍ puede vincularse a la línea 'IVA Importación' sin violar la Regla 16. Y esa línea no es un cajón inocuo: su monto_neto se reporta como `iva_importacion` en el detalle (router.py:541 y :620), o sea el balde de impuesto RECUPERABLE del embarque. Resultado: $37.080.000 de gasto financiero exento quedan presentados como IVA de importación a recuperar. El mismo agujero, en la dirección benigna, permite mandar un gasto legítimo al cajón equivocado: FastAir folio 1205943 ($108.606 netos) vinculado a 'IVA Importación' desaparece del pozo que prorratea y el costo landed queda subvaluado en $108.606 sin un solo aviso.

**Parche:** Reescribir la Regla 16: BLOQUEADO significa que el RUT no puede vincularse a NINGUNA línea de gasto de ningún embarque, punto — sin el calificativo 'capitaliza=True'. Y agregar una regla nueva: la línea `iva_importacion` NO acepta documentos del libro de compras (el DIN no está en Wasabil, la propia especificación lo declara fuera de alcance), así que el endpoint de vincular devuelve 409 cuando el tipo destino es iva_importacion, nombrando el motivo.

#### [MEDIA] El estado CONGELADO del espejo no lo escribe nadie: ninguna fase toca cerrar_pricing ni reabrir_pricing

**Escenario concreto:** La tabla de transiciones declara CONTABILIZADO →(cerrar_pricing) CONGELADO y CONGELADO →(reabrir_pricing) CONTABILIZADO. Ambos endpoints viven en backend/embarques_pricing/router.py (:1074 y :1116) y ese archivo NO aparece en la lista de archivos de ninguna fase: B1 toca sii_libro_embarque/* y EmbarquesPricingPage; B2 toca sii_libro_embarque/router.py, compras_contab/router.py:838-866, ComprasContabPage y EmbarquesPricingPage:135. Nadie escribe CONGELADO. Entonces la Regla 21 —que promete 409 con la salida nombrada «reabra el pricing»— o no dispara nunca (si mira estado_espejo, que jamás llega a CONGELADO, y entonces se puede desvincular un documento de un embarque con el costo ya congelado, dejando el snapshot apoyado en un respaldo documental que ya no existe), o dispara leyendo pricing.estado en vivo y entonces el usuario reabre, el estado_espejo sigue diciendo CONGELADO y el bloqueo no se levanta. Las dos ramas son bugs, y la elección entre ellas no está escrita en ninguna parte.

**Parche:** Decidir explícitamente que estado_espejo NO replica el estado del pricing: CONGELADO se elimina de la máquina de estados y el guard de la Regla 21 consulta `EmbarquePricing.estado == 'cerrado'` en vivo, por join, en el momento de desvincular. Una copia local de un estado ajeno es exactamente la clase de dato que la Regla 6 prohíbe; aplicarla a la propia máquina de estados evita el problema entero.

#### [MEDIA] El invariante de la tabla puente topea el neto pero no el IVA ni el exento: un documento repartido puede generar más crédito fiscal del que existe

**Escenario concreto:** El modelo declara «INVARIANTE bajo lock: Σ monto_neto_asignado de un documento ≤ w_neto» y no dice nada de `monto_iva_asignado`, que es columna NOT NULL de la misma tabla. Con la Decisión 2 (a): la factura MAQUIRENT folio 1371 ($4.000.000 neto / $760.000 IVA) se reparte 60/40 entre dos embarques. El neto queda topeado ($2.400.000 + $1.600.000 = $4.000.000, cuadra), pero nada impide que el operador ponga $760.000 de IVA en cada embarque: Σ IVA asignado = $1.520.000 sobre un documento que recargó $760.000. Ese IVA se suma en `totales_gastos.total_iva` del pricing (router.py:619) y, apenas la Fase B2 prefille la CxP con el IVA de la línea, viaja al pasivo y de ahí a cualquier informe de crédito fiscal. Es exactamente el 'IVA que se cuela' que la Regla 15 previene por el lado del botón +19% y deja abierto por el lado del reparto.

**Parche:** Extender el invariante a los tres montos, bajo el mismo lock y con la misma TOL_CLP=1: Σ monto_neto_asignado ≤ w_neto, Σ monto_iva_asignado ≤ w_iva, Σ exento asignado ≤ w_exento. Y mejor aún: no dejar que el humano teclee el IVA asignado — derivarlo proporcionalmente del neto asignado (iva_asignado = w_iva × neto_asignado / w_neto, con el residuo al primer embarque), que es la única forma de que la suma cierre por construcción.

#### [MEDIA] La ecuación E3 se promete en cero y hoy no da cero, y ninguna fase la arregla

**Escenario concreto:** El modelo de datos lista E3 («Σ gastos_clp de los ítems = total_gastos_capitaliza») entre las ecuaciones que «el informe de una hoja muestra en cero», y en la misma línea reconoce que eso «exige redondeo con absorción de residuo al ítem de mayor CIF». Ese arreglo no está en la entrega de ninguna de las seis fases. Verificado: `_prorratear` (embarques_pricing/service.py:118-132) reparte en float sin absorción, cada fila se redondea a peso al serializar (router.py:574, `round(r['gastos_clp'], 0)`) y el pie suma las filas YA redondeadas (router.py:632). Con las 3 facturas FastAir ($254.609 capitalizables) repartidas entre 7 ítems de CIF parecido, la suma de las filas puede dar $254.607 o $254.611 contra un pozo de $254.609. Es un descuadre de 2 a 3 pesos, irrelevante en plata y letal en credibilidad: el informe que se entrega para demostrar que el módulo cuadra va a mostrar un número distinto de cero el día uno — que es el mismo mecanismo por el que el módulo de gastos ya se abandonó una vez.

**Parche:** Absorción de residuo en `_prorratear`: redondear a peso dentro de la función y asignar la diferencia (total − Σ redondeados) al ítem de mayor base, devolviendo enteros. Con eso E3 da cero por construcción y el pie deja de depender del orden de redondeo. Va en la Fase 0 o A1, antes de que exista el informe que la promete.

#### [MEDIA] `nro_factura` sigue siendo UN campo por línea y sigue siendo el N° de documento de la CxP: con varios documentos hay que elegir un folio o inventarlo

**Escenario concreto:** emb_pricing_gasto.nro_factura es String(100) único por línea (models.py:96) y el PUT lo reescribe en bloque desde el payload (router.py:993, `fila.nro_factura = (g.nro_factura if g else None)` — si el payload no trae el tipo, lo deja en NULL). La especificación declara emb_pricing_gasto SIN CAMBIOS y hace derivados monto_neto e iva, pero no dice qué pasa con nro_factura cuando la línea tiene 3 documentos. Ese campo es el que el front prefillea como numero_documento de la CxP, y es la llave del anti-duplicado por factura física (compras_contab/router.py:517-539), que compara TEXTO EXACTO. Con FastAir el operador va a escribir algo como «1205849/1205943/1205944» o «varias» — y a partir de ahí ninguna de las tres capas del anti-duplicado funciona: la REGLA 1 compara texto exacto y no reconoce '1205849'; el UNIQUE (empresa, rut, numero_documento_activo) tampoco; y la REGLA 2 (sin N°) no se activa porque el campo no está vacío. Se desarma justo el freno que evitó que las facturas del forwarder se cargaran dos y tres veces.

**Parche:** Con el parche 1 (CxP por documento) esto se resuelve solo: el numero_documento de cada CxP sale de w_folio del documento espejado, normalizado, y nro_factura de la línea pasa a ser informativo — la pantalla lo muestra como lista de folios y lo deja de solo lectura cuando hay documentos vinculados. Además, cerrar el hueco de texto libre normalizando el N° de documento (quitar prefijos tipo 'FE ', ceros a la izquierda, espacios) en el mismo helper compartido donde va el normalizador de RUT de la Fase 0.

**Lo que falta probar:**

- Aguantó el ataque, verificado en vivo: la aritmética de la Regla 14. Sobre 24 meses de libro recibido — tipo 34: subtotal 573.534.668 = exento 573.534.668, IVA 0; tipo 33: subtotal 1.264.067.590, exento 341.094, IVA 240.108.034, total 1.504.175.624. Se cumple sent_ntotal = sent_nsubtotal + sent_niva y sent_niva = 19% × (subtotal − exento) al peso. Confirmado que sent_nsubtotal INCLUYE el exento, así que mapear monto_neto ← sent_nsubtotal no pierde el flete exento (DACHSER 1.223.722, FASTMARK 1.170.164, LATAM 472.886, SKY 278.546, todas exentas y capitalizables). La Regla 14 es correcta.
- Aguantó: el riesgo de colisión de folio contra el UNIQUE de cont_compra. Medido con report_documents agrupando por (receiver, folio) sobre 24 meses: el máximo de documentos por par es 1, cero colisiones. La preocupación de la especificación sobre folios correlativos por tipo es latente, no actual — pero conviene el índice NO único y la marca DUPLICADO_SOSPECHOSO igual, porque el costo de equivocarse es abortar un barrido entero.
- NO VERIFICADO, y es lo primero que hay que medir en la base de producción antes de escribir código: cuántas líneas emb_pricing_gasto con monto_neto > 0 tienen HOY más de una factura real detrás (o sea, cuántas veces el operador ya sumó a mano). Ese número decide si el parche 1 (CxP por documento) es imprescindible o si el caso FastAir es raro. La Fase 0 ya contempla dos consultas de dimensionamiento; hay que agregar esta tercera.
- NO VERIFICADO: si BODEGAS MAQUIRENT capitaliza o no. Son 11 facturas idénticas de $4.000.000 más una nota de crédito que anula un mes entero — el patrón de un arriendo fijo de bodega, no de almacenaje de tránsito. La semilla de la especificación lo propone como LOGISTICO (capitalizable): si el dueño confirma que es arriendo, esa semilla mete ~$48 millones al año en existencias. Es la Decisión 10 y bloquea la carga de reglas por RUT de la Fase A2.
- NO VERIFICADO: qué son LB INVERSIONES SPA (76.728.045-9, 5 facturas exentas, $9.000.000) y SENNA MOTORS SPA (77.136.733-K, $8.700.000 exenta más $406.742 afecta). No están en la semilla de la Regla 16 en ningún nivel, así que nacen 'no clasificados' y son vinculables por defecto; el único freno sería el guard de magnitud de la Regla 17.
- NO VERIFICADO: el comportamiento de _bloqueo_monto_gasto_con_cxp con varias CxP por línea está deducido de la lectura de _pares_gasto_cxp (router.py:385-393), no ejecutado. Antes de aceptar el parche 2 hay que reproducirlo con un test: 3 CxP sobre una línea, verificar que hoy el guard solo ve la primera y que tras el arreglo suma las tres. Sonda de poder discriminante obligatoria.
- NO VERIFICADO: si _persist_snapshot al reabrir y recerrar cambia efectivamente el costo de ítems ya despachados y facturados, o si algún consumidor aguas abajo congela ese valor por su cuenta. El delete está confirmado (router.py:671-673); falta seguir quién más lee emb_pricing_item y si el margen informado se recalcula.
- NO VERIFICADO y sigue siendo el bloqueante absoluto: la ruta de listado de documentos recibidos. Todo lo que medí acá salió por el canal MCP, que no es el transporte de un job de FastAPI. Nada de la Fase A se puede construir hasta que Wasabil responda por escrito, y la prohibición de tantear POST /documents (client.py:153-160 = emitir un DTE real) se mantiene sin excepción.

### Auditor técnico — veredicto SOBREVIVE_CON_PARCHES · 14 agujeros

#### [CRITICA] El envelope real del listado es data.list.items — el parser heredado devuelve CERO documentos y declara la lectura COMPLETA (guard que falla ABIERTO)

**Escenario concreto:** Verificado EN VIVO con get_documents(company_id=2757, trxType='expense', perPage=3): la respuesta es {success,status,data:{list:{items:[...], total:47, lastPage:16}}}. La especificación manda reusar la anatomía del cliente existente, y ese cliente parsea con `_items(data)` (backend/wasabil_dte/client.py:192-203), que sólo busca las claves 'items' y 'data' en el PRIMER nivel del dict ya desenvuelto — acá 'items' está a DOS niveles (data.list.items). Reproducción exacta: se copia buscar_documentos (client.py:238-284) al módulo nuevo; `_items({'list':{...}})` devuelve []; enseguida `last_page = data.get('lastPage')` también da None porque lastPage vive dentro de 'list' → la rama 'Envoltorio sin señal de paginación reconocible: página única' hace `return docs, True`. Resultado: la corrida termina en VERDE, ok=True, vistos=0, y el informe de Fase A dice 'no falta ninguna factura ante el SII' sobre 389 documentos que no leyó. La Regla 2 (Σ ítems != total → abortar) NO lo atrapa: sin 'total' en el primer nivel no hay comparación que hacer, y Σ0 == (nada) pasa. Es literalmente el mismo modo de falla que ya costó 7 dobles emisiones en este repo (memoria: 'un guard que falla ABIERTO es peor que ninguno').

**Parche:** El cliente nuevo NO copia `_items`. Parsea con ruta EXPLÍCITA y falla cerrado: `data['list']['items']`, `data['list']['total']`, `data['list']['lastPage']`; cualquier clave ausente o de tipo inesperado lanza WasabilError(ambiguo=True) y la corrida queda FALLIDA. La Regla 2 se refuerza: la corrida sólo concluye si (a) 'total' vino explícito, (b) len(set(uuids)) == total, y (c) páginas_leídas == lastPage. Sonda de poder discriminante obligatoria: alimentar el fake con el envelope real anidado y quitar la ruta explícita — el test debe fallar con 0 documentos, no pasar en verde.

#### [CRITICA] El monto DERIVADO de la línea de gasto no tiene candado: el PUT full-state del pricing lo pisa en silencio y no hay control de concurrencia

**Escenario concreto:** La Regla 12 declara monto_neto/iva 'derivados y de sólo lectura EN PANTALLA'. Sólo-lectura en pantalla no es un guard. El frontend arma SIEMPRE el estado completo (frontend-src/src/embarques-pricing/EmbarquesPricingPage.tsx:160-176, `buildPayload` manda las 6 líneas con lo que tiene en memoria) y el backend hace UPSERT ciego: `fila.monto_neto = neto; fila.iva = iva; fila.nro_factura = (g.nro_factura if g else None)` (backend/embarques_pricing/router.py:990-997). No hay etag, ni updated_at, ni número de versión en el PUT. Reproducción: (1) el operador abre Embarques Pricing del embarque 7 a las 10:00 con almacenaje en $0; (2) a las 10:05, en la pantalla nueva, vincula las 3 facturas de FastAir del 2026-07-22 y aprieta 'aplicar' → la línea queda en $1.200.000 con el N° del documento y emb_gasto_documento.aplicado=True; (3) vuelve a la pestaña vieja (o es otro usuario) y cambia sólo el TC, Guardar → el PUT manda almacenaje monto_neto=0, nro_factura=null → la línea vuelve a CERO y pierde el folio. `_bloqueo_monto_gasto_con_cxp` NO se activa porque sólo juzga líneas con CxP ACTIVA (router.py:398-470, `_pares_gasto_cxp` filtra por ContCompra.emb_pricing_gasto_id) y todavía no hay CxP. El costo landed baja $1.200.000, se recalcula y se persiste el snapshot, y el puente sigue diciendo aplicado=True: la pantalla nueva y la vieja muestran plata distinta por la misma factura. Segunda variante del mismo agujero: la Fase B2 dice 'aplicar por el MISMO PUT'. Si ese endpoint arma un payload con SÓLO la línea tocada, el bucle `for cat in GASTOS_CATALOGO` de router.py:963-969 pone en 0 las otras 5 (tipo ausente → 0) — aplicar una factura de almacenaje borra agencia, arancel y desconsolidación.

**Parche:** Tres cosas, ninguna opcional: (a) guard nuevo `_bloqueo_linea_con_documentos` invocado en el MISMO punto que _bloqueo_monto_gasto_con_cxp (router.py:970) que devuelve 409 si el PUT cambia monto_neto/iva/nro_factura de una línea con emb_gasto_documento.aplicado=True, nombrando la salida ('desvincule el documento primero'); (b) control de concurrencia optimista: el PUT recibe `pricing_updated_at` y devuelve 409 si no coincide con el persistido — hoy dos pestañas se pisan sin ruido incluso sin este proyecto; (c) el 'aplicar' NUNCA arma el payload desde el cliente: lee las 6 líneas PERSISTIDAS, reemplaza sólo la tocada y llama a la función de servicio en la MISMA transacción (ver agujero de las dos transacciones).

#### [CRITICA] UNIQUE(sii_libro_doc_id, emb_pricing_gasto_id) con FK ON DELETE SET NULL: en MySQL los NULL no colisionan — el repo ya se quemó con esto

**Escenario concreto:** El modelo propuesto declara `emb_pricing_gasto_id INT NULL FK ... ON DELETE SET NULL` junto con `UniqueConstraint('sii_libro_doc_id','emb_pricing_gasto_id')`. Ese UNIQUE deja de proteger apenas la columna vale NULL, y la propia Regla 22 GARANTIZA que va a valer NULL (huérfanos). Cadena reproducible: Logística borra el embarque 7 → emb_pricing tiene ON DELETE CASCADE hacia embarques (backend/embarques_pricing/models.py:28-32) → se van sus 6 emb_pricing_gasto → las filas del puente quedan (doc=A, gasto=NULL). Ahora el mismo documento A se puede vincular a otro embarque, y otra vez, y otra: N filas (A, NULL) conviven sin chocar. Consecuencias medibles: (1) el invariante 'Σ monto_neto_asignado de un documento ≤ w_neto' suma las filas muertas → una factura de $4.000.000 ya repartida y huerfanizada bloquea para siempre su propia re-asignación; (2) si se excluyen las huérfanas del Σ para destrabarlo, el mismo documento se asigna dos veces por su total completo y el costo landed de dos embarques distintos incluye la misma plata; (3) la marca HUERFANO no puede distinguir dos huérfanos del mismo documento. Este es EXACTAMENTE el agujero que el repo documenta en backend/compras_contab/models.py:31-36 ('los NULL no colisionan en MySQL') y que costó 'la factura del forwarder se cargaba 2 y 3 veces' (compras_contab/router.py, docstring de costos_embarque).

**Parche:** Copiar la solución ya probada del repo, no inventar otra: columna generada `emb_pricing_gasto_id_activo` que vale el id sólo mientras el vínculo está vivo y NULL cuando se anuló (espejo de cont_compra.numero_documento_activo), y el UNIQUE sobre (sii_libro_doc_id, emb_pricing_gasto_id_activo). Alternativa más simple y preferible: FK ON DELETE RESTRICT + un desvínculo EXPLÍCITO obligatorio (que ya existe como endpoint), de modo que la columna nunca sea NULL con estado vivo, más una fila de bitácora para el huérfano. En ambos casos el Σ del tope debe filtrar por estado del vínculo, y hay que probarlo con el caso 'embarque borrado y documento re-vinculado dos veces'.

#### [CRITICA] visto_en_ultimo_barrido es un booleano global (mark-and-sweep): dos barridos solapados marcan DESAPARECIDOS documentos que existen

**Escenario concreto:** El modelo propone `visto_en_ultimo_barrido BOOLEAN DEFAULT TRUE` y la Regla 5 deriva DESAPARECIDO de él. Ese patrón exige que exista UN solo barrido a la vez, y la especificación crea al menos dos disparadores concurrentes: el job de las 06:00 (start_scheduler se llama desde el startup de FastAPI, backend/main.py:145-146, BackgroundScheduler en proceso — con más de un worker de uvicorn el job corre N veces en paralelo) y el botón 'sincronizar ahora' de la Fase A1. Reproducción: el operador aprieta 'sincronizar ahora' a las 06:00:03 mientras el cron ya arrancó. Barrido B pone todos los flags en False y empieza a leer 16 páginas (unos 20 s con el API real); barrido A termina primero y ejecuta su paso final 'los que quedaron en False → DESAPARECIDO' → marca DESAPARECIDOS los ~380 documentos que B todavía no re-marcó, incluidos los VINCULADOS y CONTABILIZADOS. La Regla 6 no salva nada porque DESAPARECIDO es zona local escrita por el barrido (la propia Regla 5 lo autoriza). Y el aislamiento por sesión del scheduler (backend/scheduler.py:76-88) garantiza que el fallo NO se propague ni se note: se imprime en el log y la pantalla queda mintiendo.

**Parche:** Nada de flag booleano: `ultimo_run_id INT` en la fila y DESAPARECIDO derivado como `ultimo_run_id != <id de la última corrida ok>`, calculado SOLO al final de una corrida que terminó completa (Regla 2). Además, single-flight duro: la corrida abre con un lock de fila sobre sii_libro_sync_run (o SELECT GET_LOCK de MySQL) y si no lo obtiene, el 'sincronizar ahora' devuelve 409 'ya hay un barrido corriendo'. Sonda discriminante: correr dos barridos con solapamiento forzado (thread + sleep en el fake) y verificar que cero documentos quedan DESAPARECIDO; implementarlo con el booleano debe hacer fallar ese test.

#### [CRITICA] Paginación por offset con orden MUTABLE: el barrido salta documentos y la Regla 2 lo aprueba

**Escenario concreto:** El listado ordena por defecto por `sortBy=recentStatus` (último cambio de ESTADO) — verificado en la definición de la herramienta de listado. Es una clave que cambia sola: los documentos del libro se re-tocan cuando el SII actualiza su situación (medido en vivo: el documento 20260800008929 tiene document_date 2026-07-24 y status_updated_at/updated_at 2026-08-02). Reproducción: el barrido lee la página 1 (200 de 389); mientras arma la página 2, Wasabil recibe la actualización de un documento que estaba en la página 1 → salta al tope → todo el resto se corre una posición → el documento que estaba en el borde (posición 201) pasa a la 200 y NUNCA se lee, mientras un documento de la página 1 se lee dos veces. La Regla 2 valida 'Σ(ítems recorridos) == total': recorrió 389 ítems y total dice 389 → PASA. El UPSERT por uuid absorbe el duplicado sin ruido, así que quedan 388 filas distintas y el que faltó, si ya existía, se marca DESAPARECIDO; si era nuevo, jamás entra a la bandeja y su factura queda invisible para siempre. Guard que falla abierto, otra vez.

**Parche:** (a) Fijar sortBy a una clave estable en la llamada (folio o documentDate) y, aun así, (b) cambiar el criterio de completitud de la Regla 2 a `len(set(uuids)) == total` (uuids DISTINTOS, no ítems recorridos) y `páginas == lastPage`; cualquier desajuste → corrida FALLIDA. (c) Como el volumen es ridículo (~23 documentos/mes, 553 en 24 meses), barrer por VENTANAS DE MES cerradas y contrastar el conteo de cada mes contra el reporte agregado — dos fuentes que tienen que dar el mismo número. Test: fake que reordena la lista entre la página 1 y la 2 → la corrida debe quedar FALLIDA, no en verde.

#### [CRITICA] El RUT canónico que la especificación describe NO es el que produce el helper que manda reusar, y la prohibición de backfill hace que el informe de Fase A acuse faltante casi todo el histórico

**Escenario concreto:** Dos errores encadenados. (1) La Regla 10 dice 'normalizando el RUT (sin puntos, CON guion, DV en mayúscula)' y cita como fuente backend/wasabil_dte/client.py:287-289. Leí esa función: `return rut.replace('.','').replace('-','').strip().upper()` — BORRA el guion ('78.279.030-7' → '782790307'). Si Fase 0 extrae el helper 'tal cual' (como ordena) y Fase A1 guarda w_rut_emisor con él, mientras la columna nueva de proveedores o el normalizador de cont_compra usa la forma con guion de la Regla 10, el cruce ERP↔SII no empata NUNCA y el informe declara faltantes los 389 documentos. Peor: el test que la Regla 11 propone ('76.513.680-6' y '76513680-6' colapsan al mismo canónico) PASA con ambas formas — no discrimina nada. (2) Aunque las dos formas se unifiquen, 'fuera_de_alcance' prohíbe el backfill: todas las cont_compra históricas conservan proveedor_rut tecleado a mano en formato arbitrario (o vacío: la columna es nullable, backend/compras_contab/models.py:66). El día que se enciende la bandeja, cada factura ya registrada cuyo RUT esté con puntos, sin guion, con K minúscula o en blanco aparece como 'existe ante el SII y NO está en el ERP', y el remedio natural del operador es cargarla de nuevo → doble CxP, que es exactamente lo que la Convergencia 3 dice querer evitar.

**Parche:** (a) UNA sola definición de canónico, escrita en backend/rut.py con su test de tabla, y el helper viejo pasa a delegar en él (no al revés) — con test de que ambos devuelven lo mismo. (b) El cruce NO compara columnas crudas: normaliza en la consulta las DOS puntas (expresión SQL o columna generada sobre cont_compra.proveedor_rut), así lo histórico entra sin tocarlo. (c) Si igual se quiere normalizar en disco, es un backfill de FORMATO, no de datos, y hay que correr ANTES la consulta de duplicados como hace backend/embarques_pricing/init_db.py:213-247: unificar formatos puede COLAPSAR dos filas contra el UNIQUE (empresa, proveedor_rut, numero_documento_activo) → 1062 y la migración entera se cae a la mitad.

#### [ALTA] 'INVARIANTE bajo lock: Σ monto_neto_asignado ≤ w_neto' — no hay fila que bloquear, y en READ COMMITTED no hay gap locks

**Escenario concreto:** El tope se calcula sobre filas de emb_gasto_documento que TODAVÍA NO EXISTEN, así que un SELECT ... FOR UPDATE sobre ese rango no bloquea nada: el propio repo lo tiene escrito en backend/database.py:28-29 ('Sin gap locks, la unicidad va SIEMPRE en índice UNIQUE + captura de IntegrityError, nunca en un FOR UPDATE sobre un rango vacío') y lo repite en compras_contab/router.py (el chequeo de duplicado va sin lock porque el candado es la fila REAL del gasto). Reproducción: la factura mensual de BODEGAS MAQUIRENT, neto $4.000.000. El contador, en dos pestañas, asigna $3.000.000 al embarque 12 y $3.000.000 al embarque 13 y confirma casi a la vez. Ambas transacciones leen Σ asignado = 0, ambas concluyen 3.000.000 ≤ 4.000.000, ambas insertan. Σ final = $6.000.000 sobre un documento de $4.000.000: $2.000.000 de costo inventado, prorrateado por CIF a todos los ítems de dos embarques (backend/embarques_pricing/service.py, pozo capitalizable) y congelado al cerrar.

**Parche:** Bloquear la fila PADRE, que sí existe: `SELECT ... FROM sii_libro_doc WHERE id=:doc FOR UPDATE` como PRIMER paso de vincular/desvincular/aplicar, y recién después calcular el Σ. Es el mismo patrón que crear_compra usa con la fila del gasto (compras_contab/router.py, `.with_for_update()` sobre EmbarquePricingGasto). Test de concurrencia real con dos hilos y dos sesiones (el repo ya tiene el molde: los 24 cierres concurrentes del G16), no un test secuencial que siempre pasa.

#### [ALTA] Aplicar en dos transacciones deja aplicado=True con la línea sin cambiar, y envenena el snapshot que el desvínculo restaura encima de datos reales

**Escenario concreto:** La Fase B2 dice que el monto viaja 'por el MISMO PUT del router', o sea un segundo commit separado del que escribió emb_gasto_documento (snapshot + aplicado). Reproducción: la línea 'agencia' del embarque 9 ya tiene la CxP #412 por $190.400 y el operador teclearon a mano nro_factura 'FE 1205849'. Se vincula el documento del SII de $476.000 y se aprieta 'aplicar'. Paso 1 commitea: snapshot_monto_neto=190400, snapshot_nro_factura='FE 1205849', aplicado=True. Paso 2 llama al PUT y recibe 409 de _bloqueo_monto_gasto_con_cxp (router.py:398-470, el caso está medido en su docstring: línea 476.000 / pasivo 190.400). Estado resultante: el puente dice APLICADO, la línea sigue en 190.400, y la pantalla muestra 'documento aplicado' sobre un monto que no es el del documento. Peor todavía: si después alguien corrige a mano la línea (poniéndola en 476.000 tras anular la CxP) y luego DESVINCULA, la Regla 21 restaura el snapshot → la línea vuelve a 190.400, o sea el 'deshacer' destruye trabajo real que nunca hizo esta función.

**Parche:** Una sola transacción: 'aplicar' llama a la FUNCIÓN de servicio del pricing (no a un PUT HTTP), escribe snapshot y línea en el mismo commit, y cualquier 409 hace rollback COMPLETO — el puente nunca queda en aplicado=True sin que la línea haya cambiado. Y el desvínculo restaura el snapshot SÓLO si la línea sigue idéntica a lo que aplicar escribió; si divergió, no restaura, avisa y deja decidir a un humano (mismo criterio del piso monótono: ante ambigüedad, bloquear).

#### [ALTA] La bandeja de salida del sello es una cola de acciones append-only: un reintento resucita un sello ya borrado

**Escenario concreto:** Regla 23 propone filas (accion PONER|BORRAR, intentos, backoff) y Regla 21 encola BORRAR al desvincular. No hay clave por documento ni número de secuencia, y el índice propuesto ordena por (estado, proximo_intento_at), no por creación. Reproducción: 10:00 se vincula el documento A al embarque 7 → fila PONER('7'). 10:00:02 el worker la manda, Wasabil da timeout (ambiguo=True) → intentos=1, proximo_intento_at=10:05. 10:01 el operador se da cuenta del error y desvincula → fila BORRAR. 10:01:10 el worker toma la BORRAR (su proximo_intento_at ya venció) y la ejecuta OK. 10:05 el worker reintenta la PONER pendiente y la aplica: el documento A queda sellado 'embarque 7' en Wasabil para siempre, sin ningún vínculo local que lo respalde. La consecuencia es justo el número que el dueño iría a mirar: el costo del embarque 7 visto desde Wasabil incluye una factura que en PartsControl no está — y la propia especificación dice 'un sello obsoleto es PEOR que ninguno'.

**Parche:** La bandeja no es una cola de acciones, es un RECONCILIADOR DE ESTADO DESEADO: UNIQUE por sii_libro_doc_id, una sola fila que se hace UPSERT con el valor deseado (o vacío = borrar) y un seq que se incrementa en cada cambio. El worker, justo antes de mandar, re-lee el estado local y descarta la escritura si el valor deseado ya cambió (mismo espíritu del claim en vuelo de wasabil_dte). Test: encolar PONER, fallar ambiguo, encolar BORRAR, procesar, reintentar → cero llamadas de PONER posteriores.

#### [ALTA] estado_espejo colapsa dos ejes ortogonales: DIVERGENTE pisa CONTABILIZADO y borra la memoria de que hay una CxP viva

**Escenario concreto:** La máquina de estados propuesta usa una sola columna para el ciclo de vida (VINCULADO/CONTABILIZADO/CONGELADO) y para las alarmas (DIVERGENTE/HUERFANO/DESAPARECIDO/DUPLICADO_SOSPECHOSO). Reproducción: documento A, CONTABILIZADO, con la CxP #501 activa y pagada. El barrido detecta que Wasabil cambió el neto → estado_espejo='DIVERGENTE'. La transición dice 'sólo sale por humano'; el humano abre la bandeja, ve DIVERGENTE, acepta el monto nuevo y el sistema lo devuelve a... ¿qué? No queda registro en la columna de que había una CxP. Si el 'resolver' lo deja en NUEVO o VINCULADO, el guard de la Fase B1 vuelve a permitir vincularlo/registrarlo → segunda cont_compra por la misma factura, que es el bug que este repo ya pagó dos veces (compras_contab/router.py, docstring de costos_embarque: 'la factura del forwarder se cargaba 2 y 3 veces'). Idéntico con HUERFANO. Además, mantener CONTABILIZADO como columna es una copia cacheada de un hecho que vive en otra tabla: si la CxP se anula por el módulo de Compras, nadie actualiza el espejo.

**Parche:** Separar los ejes: `estado_espejo` sólo NUEVO|IGNORADO|VINCULADO|DESAPARECIDO, más banderas independientes `divergente`, `huerfano`, `duplicado_sospechoso` (booleanos con su motivo). Y CONTABILIZADO/CONGELADO NO se guardan: se DERIVAN por consulta (existe cont_compra activa ligada / emb_pricing.estado=='cerrado'), exactamente como costos_embarque calcula compra_id en vivo en compras_contab/router.py. Test: anular la CxP desde Compras y verificar que el espejo refleja el cambio sin que nadie lo toque.

#### [ALTA] Orden de locks invertido contra crear_compra: deadlock 1213 en los endpoints nuevos, que no tienen retry

**Escenario concreto:** crear_compra toma los candados en este orden: portón del pricing del embarque → fila de emb_pricing_gasto (with_for_update) → lecturas de cont_compra, y está envuelto en un retry de 3 intentos para 1213/1205 (backend/compras_contab/router.py, `for _ in range(3)`). El módulo nuevo va a tomar sii_libro_doc (por el parche del tope) y luego tocar la línea de gasto. Reproducción: operador A aprieta 'aplicar documento' (lock: sii_libro_doc → emb_pricing → gasto) mientras operador B registra la CxP de esa misma línea desde Compras (lock: emb_pricing → gasto → …) y un tercer camino, 'desvincular', que naturalmente se escribe al revés (gasto → doc). MySQL mata una de las transacciones con 1213; el endpoint nuevo, sin retry, devuelve 500 al operador, y si la operación era de dos pasos (ver el agujero de las dos transacciones) queda a medio aplicar. Nota extra: `_con_retry_deadlock` existe pero sólo dentro de embarques_pricing/router.py.

**Parche:** Documentar y respetar UN orden global único: sii_libro_doc → emb_pricing → emb_pricing_gasto → cont_compra, en todos los caminos incluido desvincular. Envolver todos los endpoints nuevos que escriben en el mismo helper de retry por 1213/1205 que ya usan crear_compra y cerrar_pricing. Test: dos hilos ejecutando aplicar y registrar-CxP en bucle sobre la misma línea, 50 iteraciones, cero 500 y cero estados a medio aplicar.

#### [ALTA] La aserción de identidad de empresa mira al PROVEEDOR, no al libro del que salió el documento: el libro de MonzaParts puede entrar entero al espejo de MachParts

**Escenario concreto:** La Regla 10 propone abortar la fila si `receiver_rut == 77.977.813-4`. Pero en un documento RECIBIDO receiver_rut es el PROVEEDOR (la propia Convergencia 11 lo establece, y lo confirmé en vivo: en el documento 20260800008929, receiver_rut '13.021.175-5' == supplier.rut, con company_id 2757), así que esa aserción no puede detectar nunca el caso peligroso. El caso peligroso es de QUÉ empresa es el libro: el API exige company_id cuando el acceso autoriza más de una empresa, y el dueño tiene DOS (Grupo AM 2757 y MonzaParts 78.121.316-0, que además ya aparece como proveedor de Grupo AM por $45.049.221). Reproducción: en algún momento se regenera el token de Wasabil con acceso a las dos empresas (o se copia el módulo a Monza y alguien cruza las variables de entorno, trampa YA vivida en este repo según la memoria de Monza F5). El barrido llama sin company_id explícito, el API resuelve a la otra empresa o mezcla, y las facturas del libro de MonzaParts entran como filas con empresa='mineria' (server_default). El guard require_empresa('mineria') del router no protege de nada: filtra QUIÉN lee, no de dónde vinieron los datos. Resultado: el informe de Fase A dice que a MachParts le faltan por registrar facturas que son pasivos de otra sociedad, y alguien crea la CxP.

**Parche:** (a) company_id EXPLÍCITO y fijo en cada llamada del cliente, tomado de una constante del módulo, nunca del default del API. (b) Aserción por fila sobre el campo correcto: `company_id == 2757` (viene en cada documento, verificado) y `received is True`; cualquier fila que no cumpla NO se ingesta y se registra. (c) Test con un fake que mezcle un documento de company_id ajeno → cero filas ingestadas y corrida marcada con aviso.

#### [MEDIA] La tercera cubeta de la Regla 3 no es representable por documento: un documento no leído no tiene fila

**Escenario concreto:** La Regla 3 exige tres cubetas por documento y su test siembra 'un documento cuya lectura quedó incompleta'. Ese estado no existe: si la lectura falló, el documento no llegó y no hay fila que poner en la cubeta. La Regla 2, encima, aborta la corrida entera. Reproducción: la página 2 de 3 falla a las 06:00. La corrida queda FALLIDA. ¿Qué muestra la bandeja a las 09:00? Muestra el espejo del último barrido bueno (de anteayer) y su reporte dice, con total aplomo, 'no falta ninguna factura' — porque las que faltan son justamente las que no se leyeron. Es un falso negativo silencioso con cara de respuesta.

**Parche:** 'NO PUDE DETERMINARLO' es un estado del INFORME, no del documento: si la última corrida no fue exitosa, el endpoint del reporte devuelve estado INDETERMINADO, la pantalla lo muestra como banda roja con la fecha del último barrido bueno, y la exportación se DESHABILITA (no se exporta un informe que no se puede afirmar). Se conserva la cubeta por documento sólo para DUPLICADO_SOSPECHOSO y DIVERGENTE, que sí tienen fila. Borrar de la especificación el test imposible y reemplazarlo por: corrida fallida → el reporte no afirma nada y no deja exportar.

#### [MEDIA] El IVA de sólo lectura choca con reglas que el backend ya impone sola: 'sólo lectura en pantalla' vuelve a no ser un guard

**Escenario concreto:** La Regla 15 dice que con documento aplicado el IVA es dato y se deshabilita el botón +19% del frontend. Pero el backend FUERZA iva=0 para los tipos 'arancel' e 'iva_importacion' pase lo que pase (backend/embarques_pricing/router.py:960-969, `iva_exento = {'arancel','iva_importacion'}`) y también pisa `capitaliza` con el catálogo. Reproducción: se vincula a la línea 'arancel' un documento tipo 33 de la agencia con IVA $3.690 (caso real: hay 8 facturas MIXTAS en el libro, como la de DHL). El 'aplicar' manda iva=3690, el backend lo pone en 0 sin decir nada, y el puente queda con monto_iva_asignado=3690 mientras la línea dice 0: el crédito fiscal desaparece del costo y de la CxP derivada, y la ecuación de cuadratura E5 (Σ documentos == Σ CxP) va a marcar una diferencia que nadie sabrá explicar. En sentido inverso: apagar el botón +19% en el frontend no impide que el PUT reciba un IVA inventado desde cualquier cliente, porque no hay validación de servidor que compare el IVA con el del documento.

**Parche:** (a) 'Aplicar' verifica ANTES si el tipo destino es de IVA forzado a 0 y devuelve 409 explicando que ese documento no puede ir a esa línea (o exige repartirlo). (b) El guard del PUT (mismo del agujero 2) rechaza cualquier iva distinto al Σ de los documentos aplicados, con la tolerancia de 1 peso de la casa. (c) Test con la factura mixta real: la línea queda con el IVA del documento o el sistema devuelve 409 — nunca un 0 silencioso.

**Lo que falta probar:**

- La ruta y el verbo REST reales del listado de recibidos: NO VERIFICADO. Sólo pude comprobar el canal MCP (herramienta de listado, company_id 2757, respuesta {data:{list:{items,total,lastPage}}}), que NO es el transporte de un job de FastAPI. Sigue en pie que GET /documents da 405 (backend/wasabil_dte/client.py:248-253) y que POST /documents EMITE (client.py:153-160). Pero la especificación asume que la forma de la respuesta se parecerá a la de /clients, y NO se parece: si el endpoint que confirme Wasabil devuelve este envelope anidado, el parser heredado devuelve cero documentos en verde (agujero 1).
- perPage máximo real: NO VERIFICADO contra el endpoint del backend. La herramienta declara max 200; el encargo dice que pedir 500 devolvió 250. Son tres números incompatibles. Hay que medir cuál es el tope efectivo del transporte que se use, porque de eso depende cuántas páginas y cuánta exposición a la carrera de paginación.
- Si el token de producción de Grupo AM autoriza una o más empresas Wasabil: NO VERIFICADO (no llamé whoami desde el backend ni leí el .env). De eso depende si company_id es obligatorio y qué tan cerca está el agujero 12.
- Cuántos workers de uvicorn corren en Hostinger: NO VERIFICADO (deploy/ sólo tiene audit_schema.py y README). Con más de uno, el BackgroundScheduler de main.py:145-146 corre N veces en paralelo y el agujero 4 pasa de posible a garantizado todos los días a las 06:00.
- Si uq_emb_pricing_gasto_tipo existe REALMENTE en la base de producción: NO VERIFICADO (no toqué la BD). init_db.py:213-247 SALTA la creación cuando encuentra duplicados vivos y el script igual sale bien. Todo el diseño del puente asume una sola línea por (pricing_id, tipo).
- Cuántas cont_compra activas tienen proveedor_rut vacío o en formato no canónico: NO VERIFICADO. Es el número que decide si la bandeja de Fase A nace usable o nace con cientos de falsos faltantes (agujero 6).
- Comportamiento del listado bajo `sortBy` estable: NO VERIFICADO. No probé si folio/documentDate producen un orden total determinista (empates de fecha son seguros, y ahí el offset vuelve a bailar).
- Si el endpoint de escritura del metafield acepta un valor sobre un documento RECIBIDO (no emitido): NO VERIFICADO, y está prohibido probarlo. La Fase B3 entera descansa en una suposición no comprobada; el interruptor apagado por defecto es lo único que la hace aceptable.

---

## Decisiones que dependen del dueño

### D1. ¿Autoriza que le preguntemos a Wasabil por escrito cuál es la ruta REST para listar los documentos recibidos? Es una consulta de soporte, no desarrollo, y sin ella no se puede empezar.

**Opciones:** (a) Sí, se pregunta y se espera la respuesta. (b) Se avanza igual, tanteando rutas. (c) Se abandona el espejo automático y se carga el libro a mano una vez al mes.

**Recomendación:** (a), y es la única aceptable. La opción (b) está PROHIBIDA: en la ruta /documents un POST crea un documento tributario real e irreversible (backend/wasabil_dte/client.py:153-160), así que tantear puede emitirle una factura al SII por accidente. Mientras se espera la respuesta, la Fase 0 tiene trabajo suficiente (el RUT, el normalizador, la auditoría de esquema) para no perder tiempo. La opción (c) es el plan B honesto si Wasabil responde que no existe endpoint REST, pero convierte el módulo en una carga manual y hay que decirlo antes, no después.

### D2. ¿Una misma factura de proveedor puede cubrir DOS embarques distintos? (típicamente la de agencia de aduana o la de almacenaje de un mes completo)

**Opciones:** (a) Sí, hay que poder repartirla entre embarques indicando cuánto va a cada uno. (b) No, una factura pertenece a un solo embarque.

**Recomendación:** (a). Es la decisión que más cambia el diseño y por eso va primero. Si es (a), la tabla puente lleva `monto_neto_asignado` con el tope Σ ≤ total del documento y el residuo imputado explícitamente a gasto del período; y ojo: un sello en Wasabil es un valor único por documento, así que NO puede representar una factura repartida entre dos embarques — el costo por embarque visto desde Wasabil quedaría por debajo del real y nadie se enteraría. Si es (b), se agrega un UNIQUE por documento, el diseño se simplifica bastante y el sello funciona sin salvedades. Recomiendo (a) porque el libro real muestra facturas mensuales de almacenaje (BODEGAS MAQUIRENT, $4.000.000 netos todos los meses, 9 folios correlativos) que difícilmente correspondan a un solo embarque, y porque el documento NO trae detalle de líneas para partirlo solo.

### D3. ¿Qué RUT son proveedores de embarque (su factura entra al costo de la mercadería) y cuáles hay que bloquear? Confirmar en particular: VECTOR CAPITAL (76.513.680-6, $550.857.712 en 23 facturas) ¿es factoring?

**Opciones:** (a) Confirmar la lista propuesta y bloquear el factoring. (b) Ajustar la lista. (c) Dejar que el sistema decida por heurística.

**Recomendación:** (a) con su revisión de la lista. La opción (c) queda descartada: el libro no trae el detalle de las líneas de los documentos recibidos (los 397 dicen 'Detalle no disponible'), así que el sistema puede saber CUÁNTO y DE QUIÉN, nunca PARA QUÉ. El bloqueo del factoring debe ser un 409 duro, no una advertencia: son montos de un orden de magnitud distinto y se reparten por CIF entre TODOS los ítems del embarque, o sea que un solo error de vinculación multiplica el costo unitario de cada repuesto y contamina el precio de venta. Pregunta subordinada para su contador: esas facturas de VECTOR ¿documentan la cesión de sus facturas por cobrar, o sólo la comisión del factoring? Cambia si deben aparecer siquiera en la lista de 'faltantes'.

### D4. Las 12 facturas de LOPEZ HERNANDEZ INVERSIONES / MonzaParts a Grupo AM ($45.049.221) son intercompañía. ¿Entran al costo de la mercadería de MachParts?

**Opciones:** (a) Entran normal, pero marcadas como parte relacionada. (b) Entran normal, sin marca. (c) Se excluyen del costo.

**Recomendación:** (a). Tributariamente son dos sociedades chilenas independientes: la factura es válida y el IVA es crédito legítimo, así que esconderlas sería el error. Pero si llevan margen, el costo landed de MachParts incluye la utilidad de su propia empresa hermana y usted está tomando decisiones de precio sobre un costo inflado con plata que el grupo se cobra a sí mismo. La marca `es_relacionado` cuesta una columna, deja el legajo listo por si el SII pregunta por precios entre relacionados (Art. 64 del Código Tributario) y permite que los informes de rentabilidad muestren ese costo por separado. Pregunta subordinada: ¿esas facturas se emiten a costo o con margen?

### D5. Cuando llega una nota de crédito de un proveedor DESPUÉS de que el embarque se cerró y su costo ya se prorrateó (y quizá los repuestos ya se vendieron), ¿qué hace el sistema?

**Opciones:** (a) Reabre el embarque y recalcula el costo histórico. (b) Deja el embarque como está y lleva la nota de crédito como menor gasto del mes en que llegó. (c) Avisa y usted decide caso a caso, con un umbral por debajo del cual va directo a gasto sin molestar.

**Recomendación:** (c) con un umbral que usted fije (sugiero: bajo $50.000 o bajo 0,5% del costo del embarque, va directo a gasto). La (a) es lo que el sistema haría hoy y es lo peor: `reabrir_pricing` no valida NADA (embarques_pricing/router.py:1124-1132, cuatro líneas sin un solo guard) y el guardado BORRA el snapshot anterior sin dejar rastro, o sea reescribe el costo con el que ya se valorizó lo vendido y cambia márgenes de meses ya informados. La (b) es correcta cuando la mercadería ya se vendió (la norma pide ajustar hacia adelante, no reexpresar) pero es incorrecta si sigue en bodega, donde la nota de crédito debe rebajar el costo del inventario. Por eso la respuesta honesta depende del caso, y hay 11 notas de crédito esperando por −$14.134.010. Hasta que decida, el sistema las va a mostrar y BLOQUEAR, con motivo explícito.

### D6. ¿Autoriza crear en Wasabil un campo personalizado nuevo para marcar el embarque, y escribir esa marca sobre documentos tributarios ya recibidos?

**Opciones:** (a) Sí, crear un campo nuevo llamado 'Embarque' de tipo NÚMERO y escribir el id interno del embarque. (b) Sí, pero de tipo TEXTO con un código legible. (c) No: el vínculo vive sólo dentro de PartsControl.

**Recomendación:** (a), y si duda, (c) — que es de riesgo CERO y no le quita nada al costo por embarque, porque ese número se calcula en PartsControl igual. Verifiqué en vivo que los tres campos que existen (Categoría, Centro de Costos, Proyecto) son listas de opciones VACÍAS y que nunca se usó ninguno en 24 meses, así que no sirven y hay que crear uno nuevo. Prefiero NÚMERO sobre TEXTO por una razón concreta: el filtro de texto de Wasabil busca por coincidencia PARCIAL, así que preguntar por el embarque 'EMB-1' también traería EMB-10, EMB-100 y EMB-11, sumando gastos de otros embarques sin fallar ni avisar — un número da coincidencia exacta. Advertencia dura: no existe herramienta para editar ni borrar una definición de campo, así que el nombre y el tipo se eligen UNA sola vez y quedan para siempre. Y la escritura no altera el documento ante el SII, pero sigue siendo una modificación sobre un registro tributario: por eso la primera se hace a mano, sobre UN documento, con usted mirando.

### D7. El informe de Fase A, ¿es sólo para mirar, o desde ahí se debe poder registrar la compra con un clic?

**Opciones:** (a) Sólo informativo en la primera entrega; registrar sigue siendo por el camino de hoy. (b) Con botón de registrar desde el primer día.

**Recomendación:** (a). La (b) es bastante más trabajo y muchísimo más riesgo: crear una Cuenta por Pagar es tocar un pasivo real, y hoy el anti-duplicado tiene tres capas con agujeros conocidos (el UNIQUE no protege con RUT vacío porque los NULL no colisionan en MySQL; el chequeo por número compara textos exactos, así que '1205849' y 'FE 1205849' no se detectan; y el chequeo por monto sólo se activa si el tipo de documento dice exactamente 'factura'). Con la Fase A informativa usted ya tiene la respuesta a su pregunta, y el registro con un clic llega en la Fase B2 con el RUT canónico ya en su lugar, que es lo que lo hace seguro.

### D8. ¿Contra qué informe oficial cuadramos el total del libro antes de mostrarle cifras a alguien? El número que traía el encargo (~$1.070 millones) no reproduce: midiendo da $1.267.460.692, aunque la cantidad de documentos sí coincide exactamente (389).

**Opciones:** (a) Contra el F29. (b) Contra el Registro de Compras y Ventas del SII. (c) Contra el libro de la contadora.

**Recomendación:** (b) el Registro de Compras del SII, y una sola vez, antes de encender la pantalla. La diferencia no está en la población (389 documentos en ambos casos) sino en el criterio de suma: con o sin facturas exentas, neto o bruto, con o sin las notas de crédito restadas. Hay que fijar UNA definición, escribirla en el código y dejarla cuadrada. Si la primera pantalla contradice a su contadora, el módulo pierde credibilidad el día uno y no se vuelve a usar — que es exactamente lo que ya le pasó al módulo de gastos de embarque.

### D9. ¿Desde qué fecha traemos el libro, y quién puede REABRIR un embarque cerrado?

**Opciones:** Historia: (a) 24 meses, (b) todo lo que Wasabil tenga (552 documentos desde 2024). Reapertura: (c) cualquiera, como hoy; (d) con motivo obligatorio y registro de quién y cuándo.

**Recomendación:** (a) 24 meses y (d). Traer 24 meses cubre de sobra los dos períodos de gracia del crédito fiscal y es barato (unas 6 páginas por barrido); traer todo agrega 2024 casi vacío (1 documento). Sobre la reapertura: hoy la puede hacer cualquiera que entre al módulo, sin motivo ni rastro, y eso descongela un costo que ya se usó para fijar precios y calcular márgenes — es la puerta trasera del guard de la Regla 13. Motivo obligatorio y registro cuestan poco y son la diferencia entre un cierre contable y una sugerencia.

### D10. Dos preguntas de clasificación que no pude resolver con los datos y que cambian si esa plata es ACTIVO o GASTO: (1) la bodega de MAQUIRENT ($4.000.000 netos fijos al mes, 9 facturas correlativas verificadas, ~$48M al año) ¿es de tránsito antes de nacionalizar, o de distribución donde la mercadería ya recibida espera al cliente? (2) los transportistas locales (~$16,9M al año: APM, YOB, Barriga, Retornos Chile, Grúas Jorge Contador) ¿traen la carga del aeropuerto a la bodega, o la llevan de la bodega a la faena del cliente?

**Opciones:** Por cada una: (a) es costo de la mercadería (capitaliza), (b) es gasto del mes, (c) depende de la factura y hay que decidir caso a caso.

**Recomendación:** Necesito su respuesta; no la puedo deducir porque el libro no trae el detalle de las líneas. Mi lectura de la norma: el almacenaje de carga ANTES de nacionalizar capitaliza, pero el arriendo fijo de bodega donde espera mercadería ya recibida es gasto del mes (NIC 2.16(b)) — y una factura idéntica todos los meses tiene toda la pinta de ser lo segundo. Igual con el transporte: el tramo de entrada capitaliza, el de salida al cliente es gasto de venta (NIC 2.16(d)). Hoy el sistema asume que TODO capitaliza y el operador no tiene ninguna palanca para clasificar: verifiqué que el backend PISA la marca 'capitaliza' con el catálogo fijo (embarques_pricing/router.py:992, `fila.capitaliza = cat["capitaliza"]`), así que el campo es decorativo. Si la respuesta es que MAQUIRENT no capitaliza, hay ~$48M al año inflando existencias y difiriendo pérdida. Recomiendo además que la regla se fije por RUT de proveedor con excepción justificada por documento, no documento por documento: es más rápido, más consistente y deja rastro de por qué se hizo la excepción.


---

## Fuera de alcance

- IVA de importación (DIN) y su comprobante de pago: NO están en Wasabil — el libro sólo trae tipos 33, 34 y 61, todos nacionales. La línea 'IVA Importación' del pricing sigue siendo carga 100% manual y debe pedir el N° de DIN y su fecha de pago como campos propios, no reutilizar nro_factura. La pantalla tiene que declararlo para que nadie lea el silencio como cero: para un importador ése suele ser el crédito fiscal más grande del mes.
- Estado de aceptación / reclamo del DTE y control del plazo de 8 días: exchange_status es null en los 553 documentos de 24 meses, y el status_id 3 'Emitido' es el estado de emisión del EMISOR, no un acuse de recibo. El espejo dice 'existe ante el SII y no está en el ERP', jamás 'está aceptada'. Si lo que se quiere es controlar el plazo para reclamar, eso vive en el portal del SII y es otro proyecto.
- La compra IMPORTADA: el libro de compras del SII sólo tiene proveedores chilenos (389 documentos, 100% CLP, tipo de cambio 1,00). La factura del proveedor extranjero de los repuestos CAT nunca va a aparecer. Fase A cubre el 100% del gasto NACIONAL de internación y el 0% de la mercadería.
- Detalle por ítem del documento recibido y prorrateo desde Wasabil: los 397 detalles dicen 'Detalle no disponible', sin XML ni PDF. El sistema puede autocompletar la CABECERA (folio, fecha, RUT, neto, exento, IVA, total) y nada más. La clasificación es humana, siempre.
- Cálculo de crédito fiscal, imputación del IVA a las cuentas 1.4.01/1.4.02 y reporte de F29: el comentario existe en compras_contab/models.py:55 pero no hay una sola línea que lo implemente, no hay motor de asientos y cod_f29 se importa del Excel pero nunca se consulta. Es un módulo nuevo, no una fase de éste. Hay que decir explícitamente en la pantalla que el espejo NO calcula crédito fiscal y que la declaración sigue siendo del contador.
- Bases de prorrateo por tipo de gasto (arancel y agencia por CIF, desconsolidación y almacenaje por peso): es una mejora real de asignación ENTRE ítems, pero DIFERIDA. Hoy nadie consume el costo landed — el precio de venta se fija en services/pricing_service.py:140-142 con un landed ESTIMADO de parámetros globales (bodegaje por defecto $90.000 contra $4.000.000 reales al mes) y el único consumidor del snapshot fuera del módulo es un guard que mira si la fila EXISTE, no cuánto vale. Afinar la base de un número que nadie lee es optimizar un dato muerto: primero que el gasto entre desde el documento, después que el landed cerrado alimente existencias y costo de ventas, y recién ahí las bases.
- Hacer editable la marca 'capitaliza' por línea y separar 'almacenaje de tránsito' de 'bodegaje del mes': es la consecuencia directa de la Decisión 10 y toca el catálogo de 6 tipos, o sea el corazón del módulo existente. NO entra en Fases A ni B; se especifica aparte cuando el dueño responda, para no mezclar un cambio de reglas contables con una integración.
- Backfill de datos históricos: no se corrigen las CxP viejas con el acreedor equivocado ni las que nacieron con fecha de hoy. Regla de oro ya establecida en el proyecto (el TC congelado se aplicó sólo de ahí en adelante). Si el dueño quiere reparar lo viejo, es un trabajo aparte con su propia validación.
- Agregar tipo_dte al UNIQUE de cont_compra: es un cambio de índice, no aditivo, y requiere chequear duplicados vivos ANTES de emitir el ALTER (con duplicados MySQL responde 1062 y, al ir todo en una transacción, tumba el resto de la migración). Va en una fase propia con su propio guion de reparación. El riesgo es real: los folios son correlativos POR TIPO y GESIP ya tiene notas de crédito folio 1 y folio 2.
- Port a MonzaParts: esta especificación es sólo para GRUPO AM SPA / MachParts (empresa id 2757, RUT 77.977.813-4). El port se evalúa después, y hay que recordar que en los módulos maduros Monza suele ir ADELANTE de MachParts, así que no se asume que la deuda va en una sola dirección.
- Cualquier escritura en Wasabil fuera del sello del metafield (crear documentos, notas de crédito, anulaciones, vínculos con transacciones bancarias): prohibida por diseño y verificada por sonda estructural en cada corrida de tests.
- Conciliación bancaria de los documentos del libro: la integración bancaria de la cuenta está vacía (0 movimientos, integrations: []) y la vista de conciliación de Wasabil ni siquiera acepta filtrar por campo personalizado.
---

# ADDENDUM 2026-08-05 — El bloqueante quedó RESUELTO y el dueño respondió las 4 decisiones

## 1. La ruta de listado EXISTE y está documentada oficialmente

La pregunta del dueño («¿tenemos que leer la documentación?») era la correcta. Wasabil publica
su documentación completa del API en texto plano, **regenerada en cada deploy**:

> **https://app.wasabil.com/llms-full.txt** (y el índice en /llms.txt — estándar llmstxt.org)

Y ahí está lo que el reconocimiento no encontró:

### `POST /api/documents/query` — el listado que faltaba

Por eso `GET /documents` responde 405: **el listado es un POST a OTRA ruta** (`/query`,
semántica de consulta — no confundir jamás con `POST /api/documents`, que CREA un documento).

Lo que documenta, y que calza con lo que el plan necesita:

- Paginación: `page` + `perPage` (default 10, **máximo 250**) + `sortBy`
  (recentStatus | lastCreated | documentDate | folio, siempre descendente).
- Filtros: **`received: true`** (solo recibidos), `trxType: expense`, `siiDocumentTypeCodes`,
  `statusIds`, `fromDocumentDate`/`toDocumentDate`, `supplierRut`, **`metafields`**, y
  `exchangeStatus` (PENDING | ERM | RFT — el acuse de recibo de documentos recibidos SÍ es
  filtrable, aunque en los datos actuales venga null).
- **El envelope es `data.list.items`** — EXACTAMENTE lo que el auditor técnico predijo
  («el parser heredado devuelve CERO documentos y declara la lectura completa»). La Regla 2
  del plan (lectura incompleta → abortar) se implementa contra este envelope; si no trae
  total/lastPage utilizable, se recorre hasta página vacía con validación de solape.

### `POST /api/financials/transactions/bulk` — la cartola (Fase C)

Documentado con el flujo exacto que el plan pedía: `mode: check` (reporta duplicados sin
persistir) → `mode: apply`, dedup automática por (fuente + monto + fecha + descripción),
`skip_duplicates: true` por defecto, `group_name` por lote, `external_id` del banco.

### Webhooks GLOBALES por empresa

Además del `notification_url` por documento, hay webhooks a nivel empresa
(`GET/POST /api/webhooks`) que notifican **todo cambio de estado de todo documento** con el
objeto completo. Para el espejo esto es un acelerador de frescura: el barrido nocturno
completo sigue siendo la columna vertebral (Regla 5), pero el webhook baja la ventana de
desactualización de 24 h a segundos. Se evalúa en A1 como opcional.

## 2. Las respuestas del dueño (2026-08-05)

**D1 · Ruta de listado** → resuelta leyendo la documentación oficial. No hace falta el
ticket a soporte. El cliente nuevo se construye contra `llms-full.txt`.

**D2 · ¿Una factura puede cubrir varios embarques?** → «Depende: la de agencia es por
embarque, la desconsolidación también, y la factura del proveedor cubre los ítems que vienen
en los embarques. Lo que sí: las compras LOCALES pueden representar costos o gastos asignados
a más de un embarque.» → **Se confirma la tabla puente con `monto_neto_asignado`**: soporta
el caso típico 1:1 (agencia, desconsolidación) y el caso real N:M de las compras locales,
con tope Σ ≤ total del documento y residuo explícito a gasto del período.

**D3 · Vector Capital** → NO es factoring: **es la corredora de bolsa — compra de moneda**
(los $550M son compra de divisas, no un servicio). Cambia la etiqueta, no el veredicto:
sus documentos quedan **BLOQUEADOS** para vincularse a embarques (no son costo de mercadería)
con clasificación `financiero / compra de moneda`, junto a bancos y seguros.

**D4 · Bodega y transportistas** → «No lo vería solo por un proveedor, sino algo que pueda
clasificarlos bien. Transporte local ya no es costo landed, es distribución. La bodega es
donde guardamos.» → Reglas que quedan fijadas:
- **Transporte local = DISTRIBUCIÓN = gasto del período** (no capitaliza).
- **Bodega (Maquirent) = almacenaje de mercadería ya recibida = gasto del período.**
- La clasificación **no puede ser solo por RUT**: el nivel `LOGÍSTICO` por RUT es el default,
  y **cada documento se clasifica al vincular**, con el default en gasto del período. Esto
  refuerza la Regla 16 (tres niveles) con un cuarto matiz: la clasificación por documento
  manda sobre la del RUT.

## 3. Qué se construye ahora (orden actualizado)

1. **Fase 0** (sin dependencia externa ya): cliente REST nuevo `wasabil_compras/client.py`
   contra `POST /api/documents/query` (parser del envelope `data.list.items` con la Regla 2),
   helper de RUT canónico compartido, columna `rut` en `proveedores`.
2. **Fase A1**: espejo + barrido nocturno + tablero.
3. **Fase A2**: bandeja de faltantes (el entregable del dueño).
4. **B1 → B2 → B3** según el plan, con las reglas de D2/D3/D4 incorporadas.

---

# ADDENDUM 2026-08-08 — Lo que quedó CONSTRUIDO, y las 19 correcciones de las tres rondas adversariales

> **Cómo leer de acá en adelante:** donde este addendum y el cuerpo de arriba se contradigan,
> manda el addendum — está escrito mirando el código que corre, línea por línea. El cuerpo de
> arriba no se toca ni se borra: sigue siendo la mejor explicación de POR QUÉ el módulo es
> así, y hay decisiones que un lector puede recordar de la versión vieja. Cuando algo se dio
> vuelta, acá se dice qué se creía antes y por qué se cambió de opinión.
>
> **[Y donde este addendum contradiga al §6, manda el §6.]** Este addendum se escribió el
> mismo día que el módulo terminaba de moverse y quedó con catorce afirmaciones que el código
> desmiente —varias en la tabla del §4, que era justamente la que se leía para no abrir el
> código—. El §6, del final, las corrige una por una y agrega lo que faltaba escribir. Mismo
> criterio de siempre: no se borró nada, se dice qué se creía y qué resultó ser.

## §0. El documento dejó de ser un plan: hay código corriendo

La línea de la cabecera («no se ha escrito ni una línea de código») fue verdad exactamente dos
días. Hoy están construidas, con pruebas y desplegables, la **Fase 0** (el cliente que le habla
al API de Wasabil por `POST /api/documents/query`, más el normalizador de RUT y la columna `rut`
en la tabla de proveedores), la **Fase A1** (el espejo del libro, el barrido nocturno y el
tablero) y la **Fase A2** (la bandeja con sus cubetas, las reglas por RUT, la decisión por
documento, el CSV exportable y el pre-llenado del formulario de Compras). Y hay además un
módulo que este plan **no contemplaba**: el **matcher banco↔libro**, que propone cruces entre
los movimientos de la cartola del banco y los documentos del libro (su especificación vive
aparte, en `docs/spec-matcher-banco-libro-2026-08-06.md`).

**La Fase B entera sigue sin existir.** No hay tabla puente documento↔gasto de embarque, no hay
bandeja de salida del sello, y la variable de configuración del sello ni siquiera está declarada
— verificado buscando en todo el backend: cero coincidencias de `emb_gasto_documento`,
`sii_sello_outbox` y `WASABIL_SELLO_HABILITADO`. Todo lo que corrige este addendum es de Fase A.
Las Reglas 12 a 23 del plan siguen siendo un compromiso a futuro, no una descripción de algo que
funcione hoy.

**[TRES PRECISIONES 2026-08-08 · segunda pasada, verificadas contra el código:]**
1. **No es sólo la variable del sello la que falta: son las CUATRO** que el bloque CONFIG del
   modelo de datos declara. Tampoco existe el interruptor del módulo ni la configuración de
   cuántos meses de historia traer — los 24 meses son una constante en el código. Detalle en
   el recuadro del bloque CONFIG y en §6.B.6.
2. **De las Reglas 12 a 23, dos SÍ corren** y por eso el «no construidas» en bloque era
   demasiado grueso: la clasificación por RUT en tres niveles con su candado duro (Regla 16,
   entregada en la Fase A2 y así lo dice el propio cuerpo) y el bloqueo de las notas de
   crédito (Regla 20). Ninguna de las dos está completa; el corte exacto está en el §4 y en
   §6.B.4.
3. **La Fase A2 no se entregó sin excepciones:** falta el filtro «sólo proveedores de
   embarque» de su propia lista de entrega, y el nivel IGNORAR_AUTO no archiva documentos
   solo en la bandeja. Ver §6.B.4.

Después de construir, el módulo pasó por **tres rondas de revisión adversarial** (varios
revisores independientes intentando romperlo, cada hallazgo con un refutador que trataba de
demostrar que era falsa alarma). Lo que sobrevivió se arregló, y de ahí salen los 19 cambios de
comportamiento de este addendum:

| Commit | Qué trajo |
|---|---|
| `8bc0df4` | Los 17 hallazgos confirmados de la primera ronda (espejo, bandeja, matcher, arquitectura) |
| `2ce0fc9` | El CSV que ya no ejecuta fórmulas en el Excel del contador |
| `79d02dd` | Los tres lentes que faltaban: usabilidad, ciclo de vida y seguridad |

Un detalle que ordena todo lo demás y conviene tener presente al leer: el primer commit arregló
el backend y **no tocó un solo archivo de frontend**. Tres arreglos quedaron viviendo únicamente
en la respuesta del servidor, invisibles en la pantalla — entre ellos el aviso de folio parecido,
que era el único freno a una cuenta por pagar duplicada. La lección quedó anotada: *un dato que
el backend calcula y la pantalla no pinta es un arreglo que NO existe*.

## §1. Dónde vive el código de verdad (los nombres del plan no son los nombres reales)

El plan bautizó los paquetes `sii_libro_compras` y `sii_libro_embarque`. Ninguno de los dos
existe. Lo que hay es:

| Cosa | Grupo AM / MachParts | MonzaParts |
|---|---|---|
| Paquete del backend | `backend/wasabil_compras/` | `backend/monza_wasabil_compras/` |
| Tabla del espejo | `sii_libro_doc` | `monza_sii_libro_doc` |
| Bitácora de cada barrido | `sii_libro_sync_run` | `monza_sii_libro_sync_run` |
| Reglas por RUT de proveedor | `sii_libro_regla_rut` | `monza_sii_libro_regla_rut` |
| Cruces banco↔libro (matcher) | `sii_libro_match`, `sii_match_run`, `sii_match_etiqueta_mov`, `sii_match_config` | los mismos con prefijo `monza_` |
| Pantalla | `frontend-src/src/pages/LibroSiiPage.tsx` | `frontend-src/src/pages/MonzaLibroSiiPage.tsx` |
| Candado de marca | `require_empresa("mineria")` | `require_empresa("automotriz")` |

**Las dos marcas se documentan juntas en todo este addendum porque el comportamiento es
IDÉNTICO**, hasta el texto de los mensajes de error. Pero el código está escrito **dos veces, a
propósito**: cada marca es dueña de su paquete y no hay un solo `import` cruzado entre
`wasabil_compras` y `monza_wasabil_compras`. Es la regla de la casa y no es descuido: compartir
un helper significa que el día que una marca necesita cambiarlo, la otra se entera a golpes.
Cuando abajo se nombra un archivo sin prefijo, existe su gemelo `monza_` con el mismo contenido.

**Nombres de columna que el plan inventó y que no se usaron.** El plan propuso prefijar con `w_`
todo lo que viene de Wasabil (`w_folio`, `w_total`, `w_rut_emisor`…). El código guarda los
nombres tal como los entrega el API: `folio`, `sent_ntotal` (el total declarado al SII),
`sent_nsubtotal` (el neto, que incluye lo exento), `sent_niva`, `sent_nexempt`,
`receiver_rut_original` (el RUT del proveedor tal cual vino) y `rut_emisor_canonico` (ese mismo
RUT normalizado, que es la llave con la que se cruza contra el ERP). La única magnitud que se
puede sumar sigue existiendo como columna propia, `monto_efectivo` (el total con su signo: menos
en las notas de crédito), exactamente como manda la Regla 9.

**El estado del espejo tiene DOS valores, no nueve.** El plan dibujó una máquina de estados con
`NUEVO | IGNORADO | VINCULADO | CONTABILIZADO | CONGELADO | DIVERGENTE | HUERFANO | DESAPARECIDO
| DUPLICADO_SOSPECHOSO`. **Se adoptó el parche del auditor técnico** («estado_espejo colapsa dos
ejes ortogonales»): la columna `estado_espejo` sólo dice si el SII declara hoy ese documento —
`ACTIVO` o `DESAPARECIDO` — y todo lo demás vive en columnas separadas: `decision` (vacía =
pendiente, o `ignorado`, o `clasificado`), `destino` (a dónde se clasificó: `costo_venta`,
`activo_fijo` o un centro de costos `cc:…`) y `divergente` (un sí/no: el documento cambió en
Wasabil DESPUÉS de que alguien decidió sobre él). Los estados de Fase B no se guardan porque la
Fase B no existe. Esto no es un olvido: mezclar «en qué punto de su vida está el documento» con
«qué alarma tiene encendida» era lo que hacía que una alarma borrara la memoria de la decisión.

---

## §2. Las 19 correcciones, una por una

### El reloj: quién dispara el trabajo y cuándo

#### 1. El libro del SII dejó de colgar del job de alertas: ahora tiene job propio a las 05:30

**Antes.** El barrido nocturno iba adentro de `run_daily_checks`, el único disparo diario de las
alertas (06:00 hora de Santiago). Así lo declara la Fase A1 de arriba: «colgado del job de las
06:00 que ya existe».

**Ahora.** Existe `run_sii_libro_job`, registrado en el planificador con su propio horario —
05:30 America/Santiago, identificador `sii_libro` — y `run_daily_checks` quedó SOLO con las
alertas. Corre media hora antes a propósito: que el libro esté fresco cuando la oficina abre, y
a una hora distinta para que el trabajo pesado no compita con las alertas.

**Por qué.** Estaban atados dos trabajos que no comparten nada. El barrido habla por RED con
Wasabil y el matcher recorre la cartola entera: minutos de trabajo pesado colados dentro del
disparo de las alertas, que son livianas y sí o sí tienen que salir. Peor todavía, el acople se
notaba en las pruebas: la suite de alertas llama a `run_daily_checks` ocho veces, y eso
arrastraba ocho barridos y ocho corridas del matcher contra la misma base, dejando un estado que
hacía fallar suites vecinas. Ése fue el síntoma que destapó el problema. Separarlos sube un nivel
el invariante de aislamiento del módulo: ahora también entre asuntos distintos, no sólo entre
marcas. Los dos invariantes del job original se conservan intactos: **cada marca con su propia
sesión de base de datos y su propio try/except**, más los imports adentro de la función, para que
un paquete ausente o un fallo de una marca no deje a la otra sin barrido.

*Dónde mirarlo:* `backend/scheduler.py` (definición de `run_sii_libro_job`, el `add_job` con el
horario, y `run_daily_checks` ya sin el libro). El checklist de deploy también quedó corregido:
`docs/CHECKLIST-DEPLOY-2026-07-20.md` ya dice 05:30.

#### 2. El matcher banco↔libro ya no depende de un clic: corre tras cada barrido y tras cada cartola

**Antes.** Su especificación exigía que el motor corriera «tras cada barrido exitoso», pero el
único que lo llamaba era el botón manual de la pantalla. Consecuencia: los cruces automáticos
prometidos después del barrido no nacían nunca, y la re-verificación de sus reglas quedaba
esperando que un humano se acordara de apretar.

**Ahora.** Dos disparadores nuevos, además del botón:

- **Después del barrido nocturno.** `run_sii_libro_job` llama al matcher al terminar cada marca
  (`origen='post_barrido'`), y **sólo si ESTE job dejó una corrida del barrido marcada exitosa e
  iniciada después de que el job arrancó**. Esa condición no es un detalle: sin token de Wasabil
  el barrido se omite en silencio, y sin ella el matcher correría igual con una etiqueta
  mentirosa que diría «post_barrido» sin que hubiera habido barrido.
- **Al importar una cartola bancaria.** El endpoint que sube la cartola llama al matcher
  (`origen='post_cartola'`) dentro de un try aislado: la importación ya está confirmada en la
  base cuando el matcher corre, así que si el matcher falla —porque hay un barrido en curso,
  porque falta una migración, por lo que sea— **la importación no se cae**; el problema se informa
  al operador en la lista de `warnings` de la respuesta.

El botón manual sigue existiendo y su docstring lo aclara: es una corrida más, no la única.

*Dónde mirarlo:* `backend/scheduler.py` (`_matcher_post_barrido_ga` y `_matcher_post_barrido_monza`),
`backend/tesoreria/router.py` y `backend/monza_tesoreria/router.py` (el hook al final de importar
cartola), `backend/wasabil_compras/router_match.py` (el docstring del botón).

### El barrido ya no puede mentir

#### 3. Anti-solape por id: el barrido superado se retira sin escribir una sola fila

**Antes.** El único freno contra dos barridos pisándose era un guard de entrada: si había una
corrida sin terminar iniciada hace menos de 30 minutos, la nueva se rechazaba. El agujero: pasados
esos 30 minutos **con el barrido todavía vivo**, ese run viejo podía terminar y pisar con datos de
hace media hora los datos frescos de un run posterior.

**Ahora.** Se consulta dos veces —antes de abrir la escritura y otra vez justo antes del paso
destructivo— si nació una corrida **con número (id) mayor**. Si existe, este run deshace todo
(rollback), cierra su bitácora como fallida narrando el motivo, y **no escribe un solo
documento**. Es lo que en la industria se llama *fencing*: el que llegó tarde queda fuera por
número de turno, no por reloj.

**Una decisión que se dio vuelta y conviene saberlo:** el mensaje del commit `8bc0df4` anuncia
«heartbeat + fencing». Lo que quedó en el código es **sólo fencing**, y el porqué está escrito en
la propia función: un *heartbeat* (que el barrido vaya avisando «sigo vivo» cada tanto) necesita
una columna de última actualización en la tabla de corridas, esa columna no existe, y agregar
columnas a tablas vivas es la trampa que el checklist de deploy documenta (el error 1054 de
MySQL, «columna desconocida», que tumba pantallas enteras cuando el modelo declara algo que la
base no tiene). El fencing cubre el daño real del hallazgo sin tocar el esquema.

*Dónde mirarlo:* `backend/wasabil_compras/sync.py`, función `_superado_por_otro_run` y sus dos
llamadas; gemelo en `backend/monza_wasabil_compras/sync.py`.

#### 4. Cinturón de proporción: un barrido que haría desaparecer más de la mitad del espejo aborta

**Antes.** Una lista **vacía pero bien formada** —una regresión del API, un filtro que cambió de
significado— pasaba todos los controles: no había error de red, no había página incompleta, el
total informado calzaba con cero. El barrido entonces marcaba DESAPARECIDO el espejo COMPLETO y
cerraba con éxito. Resultado: un «no falta ninguna factura» falso que duraba 24 horas, hasta el
barrido siguiente.

**Ahora.** Si el barrido dejaría DESAPARECIDO a más del **50%** de los documentos activos dentro
de la ventana consultada, la corrida se declara sospechosa: aborta con éxito=falso, con un mensaje
que dice **cuántos de cuántos** y sugiere ir a mirar Wasabil antes de tocar nada, y **el espejo
queda tal como estaba**. El criterio es el mismo de la Regla 2: datos viejos honestos valen más
que datos frescos mentirosos.

Es el complemento exacto del tope de páginas que ya existía: aquél protege contra el **exceso**
(un barrido que se desboca), éste contra el **defecto** (un barrido que trae de menos y lo hace
pasar por verdad).

*Dónde mirarlo:* la constante `MAX_PROPORCION_DESAPARECIDOS = 0.5` y el bloque que la usa, en
`sync.py` de los dos paquetes.

#### 5. Envejecer no es desaparecer: sólo se marca DESAPARECIDO dentro de la ventana consultada

**Antes.** El barrido consulta una ventana de 24 meses hacia atrás, y esa ventana **se desliza
todos los días**. El paso final marcaba DESAPARECIDO a todo documento activo que no hubiera visto
— sin fijarse en que un documento de hace 24 meses y un día simplemente había salido de la
consulta. Así, cada noche se fabricaban desaparecidos falsos: el documento no dejó de existir
ante el SII, dejó de preguntarse por él.

**Ahora.** El paso final lleva la condición «fecha del documento igual o posterior al inicio de la
ventana». Un documento más viejo que la ventana **queda ACTIVO**, con su decisión y su cubeta como
siempre estuvo.

**Decisión explícita, para que nadie la re-discuta desde cero:** se eligió *excluirlos por fecha*
y NO inventar un estado nuevo tipo `FUERA_DE_VENTANA`. Un estado nuevo movería documentos de
bandeja sin que nada haya cambiado en el mundo real, que es justamente el ruido que se quería
evitar. Y un documento **sin fecha** tampoco se marca: no se puede demostrar que la ventana lo
cubría, y acusar a un documento de haber desaparecido del SII es una acusación seria — ante la
duda, se falla cerrado.

#### 6. Un documento malformado ya no congela el barrido: se espeja con monto vacío y el barrido termina con AVISO

**Antes.** Si de un documento no se podía derivar la magnitud sumable (porque el total o el signo
venían ilegibles), la excepción tumbaba la escritura ENTERA. Efecto real: **un solo documento
podrido hacía fallar el barrido todas las noches**, y el espejo se quedaba viejo para siempre —
sin que el problema fuera de los otros 500 documentos sanos.

**Ahora.** Ese documento se espeja igual, con `monto_efectivo` vacío (NULL). La marca es el vacío
mismo: para un documento sano esa columna siempre tiene valor. El barrido termina **exitoso**, y
en la bitácora queda `AVISO: N documento(s) malformado(s) espejado(s) con monto NULL — …`, visible
en el tablero.

**Dos consecuencias documentales que hay que tener presentes:**

1. **Que la bitácora tenga texto en el campo de error ya NO significa que el barrido falló.**
   Puede ser un aviso sobre un barrido exitoso. Quien lea esa columna tiene que mirar el
   indicador de éxito, no la presencia del texto.
2. **La Regla 9 sigue intacta.** Un monto vacío no suma; nunca suma mal. Preferir el vacío
   declarado sobre el cero silencioso es exactamente lo que esa regla pide.

### La bandeja: mirar, exportar y no fabricar una cuenta por pagar duplicada

#### 7. Los DESAPARECIDOS se pueden mirar y exportar (antes eran sólo un número)

**Antes.** Tanto la lista de documentos como el CSV filtraban duro por «activo». El documento
desaparecido existía únicamente como un contador en el tablero, y su «rastro intacto» —que la
Regla 5 promete— sólo se podía consultar entrando a la base de datos con SQL. El tablero anunciaba
«+3 desaparecidos del SII» y no había ninguna pantalla donde verlos.

**Ahora.** Los dos endpoints aceptan un parámetro `estado` con dos valores, `ACTIVO` (el que rige
si no se pide nada) o `DESAPARECIDO`, y responden 400 ante cualquier otro valor. Las filas
desaparecidas viajan con cubeta `N/A` y el detalle «el SII ya no declara este documento» — porque
el cruce contra el ERP, que se calcula en vivo, no tiene sentido sobre algo que el SII ya no
declara. La pantalla manda ese filtro con un botón que aparece bajo el contador.

#### 8. Folio BLANDO en el cruce: un folio parecido ya nunca cae en «NO ESTÁ»

**Antes.** El cruce comparaba el folio EXACTO como texto. `'0004071'` tecleado del PDF contra
`'4071'` guardado en el libro daba **NO ESTÁ EN EL ERP**. El operador leía eso, apretaba
«registrar» de buena fe, y nacía una segunda cuenta por pagar por la misma factura física — que
Tesorería podía pagar dos veces.

**Ahora.** Además del índice exacto se arma un índice **blando** por (RUT canónico, folio sin
ceros a la izquierda ni prefijos), **reutilizando el mismo criterio que el matcher ya validaba**
para folios sucios (`'F-1234'` → `'1234'`). Si un documento calza sólo en blando, su cubeta es
**INDETERMINADO con leyenda** — nunca «no está». La leyenda nombra las compras parecidas y el
folio propio, y dice qué hacer: revisar antes de registrar de nuevo.

Detalle técnico con consecuencia visible: la función del cruce dejó de devolver un texto y ahora
devuelve el par (cubeta, detalle), y la respuesta al frontend ganó la clave `cubeta_detalle`. Es
aditiva: un frontend viejo la ignora sin romperse.

#### 9. Esa leyenda viaja al pre-llenado y traba el botón de guardar

**Antes.** El pre-llenado devolvía sólo los datos del documento. La advertencia de folio parecido
se quedaba en la lista: el operador apretaba «registrar», el formulario se abría limpio, y el
aviso desaparecía **justo en la pantalla donde se fabrica el duplicado**.

**Ahora.** El endpoint de pre-llenado devuelve además la cubeta y su leyenda, **calculadas con el
mismo cruce que usa la bandeja** (una sola definición de la verdad: si divergieran, la lista diría
una cosa y el formulario otra). El formulario de Compras pinta un aviso ámbar con el texto del
servidor y **deja el botón «Registrar compra» deshabilitado** hasta que el operador marque la
casilla «Ya la busqué en la lista de compras y no está». Si el pre-llenado no trae esas claves, el
formulario se comporta como siempre.

Que quede claro qué es y qué no es: **esto es informativo, no un candado**. Los candados de verdad
son los del alta (punto 15).

#### 10. El pre-llenado entrega el RUT CANÓNICO, no el formateado

**Antes.** El pre-llenado devolvía el RUT bonito, con puntos: `'76.513.680-6'`. El anti-duplicado
del alta de compras compara textos, así que ese RUT con puntos **burlaba el guard** contra una
compra vieja guardada como `'76513680-6'` — aunque el folio fuera idéntico. Nacían dos cuentas por
pagar, las dos pagables.

**Ahora.** Devuelve el canónico (`'76513680-6'`), que es la llave de cruce de todo el paquete. La
pantalla puede formatearlo al mostrarlo si quiere; lo que se guarda es el canónico.

#### 11. Pre-llenar y decidir responden 409 si el documento no está ACTIVO en el espejo

**Antes.** Ni el pre-llenado ni la decisión miraban el estado del espejo. Con un identificador
viejo —una pestaña sin refrescar— se podía pre-llenar una compra desde un documento que el SII ya
no declara, o decidir sobre él; y al decidir se apagaba la marca de divergencia sobre ese
fantasma.

**Ahora.** Los dos endpoints responden **409** nombrando el estado y explicando la salida: refrescar
la bandeja, y que si el documento reaparece en un barrido revive solo. Es el mismo guard que el
confirmar del matcher ya aplicaba, extendido a los dos caminos que lo tenían abierto.

### El número que se muestra

#### 12. La cuadratura mensual pasó a pesos, viene DESGLOSADA, y el número que debe dar cero cambió de nombre

Éste es el cambio que más afecta lo que el dueño ve en pantalla, así que va con detalle.

**Antes, dos errores encadenados:**

1. **Moneda.** El lado ERP sumaba el total de cada compra **en la moneda del documento**. Una
   compra de USD 45.000 aportaba «45.000 pesos» a la suma. Y además metía compras que **jamás**
   van a estar en el libro del SII (los embarques del proveedor extranjero, los gastos sin
   documento tributario).
2. **Universos distintos.** El lado libro incluía notas de crédito y documentos marcados como
   «ignorado», contra un ERP que **no puede registrar notas de crédito** (el propio pre-llenado lo
   prohíbe con un 409). La resta **nunca podía dar cero**, y sin embargo la pantalla decía que
   debía darlo. Un número que siempre está en rojo deja de ser una alarma.

**Ahora:**

- El lado ERP suma en **pesos siempre**: `monto_total_clp` (el total de la compra convertido a
  pesos, que es la base con que trabaja Cuentas por Pagar), con el total en moneda original sólo
  como red para filas antiguas donde esa columna no esté poblada (en pesos ambas coinciden).
- El lado ERP se parte en dos: **`erp_comparable`** = lo que PODRÍA aparecer en el libro del SII
  (compras nacionales, no originadas en un embarque, en pesos y con documento tributario), y
  **`erp_fuera_libro`** = todo el resto. Lo que no se puede afirmar comparable cae en el segundo:
  la duda no infla la cuadratura.
- El lado libro expone tres números en vez de uno: **`libro_total`**, **`libro_ignorados`** (sólo
  los de signo positivo — una nota de crédito ignorada ya viaja en la columna de notas de crédito,
  restarla dos veces inventaría diferencia) y **`libro_nc`** (las notas de crédito).
- Y aparece la clave nueva **`diferencia_explicada` = libro − ignorados − notas de crédito −
  erp_comparable: ÉSA es la que debe dar $0.**

**Las claves viejas se conservan** (`libro`, `erp`, `diferencia`) para no romper una pantalla que
todavía no se haya actualizado — con la salvedad de que `erp` ahora está en pesos, que es lo que
siempre debió estar. Y el resumen agrega `cuadratura_criterio`, un texto que explica el criterio
para el tooltip del tablero: el número no viaja solo, viaja con su definición. La pantalla usa el
desglose si viene y cae a la resta bruta si habla con un backend anterior.

### Plata y seguridad de quien opera

#### 13. Enfriamiento de 2 minutos del barrido MANUAL (HTTP 429), y sólo después de un barrido exitoso

**Antes.** El anti-solape impedía dos barridos **simultáneos**, pero no dos **seguidos**. Un doble
clic en el botón —o una pestaña que reintenta sola— disparaba llamadas REALES a Wasabil en
cadena, y ese consumo lo paga la cuenta de la empresa.

**Ahora.** Si el último barrido salió bien y arrancó hace menos de **120 segundos**, el botón
responde **429** diciendo cuántos segundos faltan y por qué no vale la pena insistir (el SII no
publica documentos nuevos cada minuto: barrer de nuevo tan pronto traería exactamente lo mismo).

**Si el último barrido FALLÓ, el reintento es inmediato.** Es deliberado: si el operador acaba de
arreglar el token o volvió la red, castigarlo con dos minutos de espera sería absurdo. El
enfriamiento **no aplica** al barrido nocturno (corre una vez al día) ni al matcher (trabajo local,
sin costo contra terceros y ya serializado por su propio guard).

**[PRECISIÓN 2026-08-08 · segunda pasada]** «No aplica al nocturno» es cierto en un sentido y
falso en el otro: el nocturno nunca se bloquea a sí mismo, pero **el enfriamiento mira la
última corrida sea cual sea su origen**, así que un barrido nocturno exitoso deja el botón
manual bloqueado durante sus dos minutos. Impacto bajo —ocurre a las 05:30— pero explica un
rechazo que si no parece un bug. Ver §6.D.6.

#### 14. El CSV de faltantes ya no ejecuta fórmulas en el Excel del contador

**Antes.** El nombre del emisor, el folio y la leyenda del detalle salían crudos al CSV. Y esos
textos **los escribe un tercero**: vienen del SII, no de nuestro ERP. Excel y LibreOffice evalúan
como **fórmula** toda celda que empieza con `=`, `+`, `-` o `@`, así que un proveedor llamado
`=HYPERLINK("http://…","Ver")` —o algo peor— se ejecutaba en el computador de quien abre el
reporte. Y esa persona es justamente la que no puede defenderse: confía en que el archivo lo
emitió su propio sistema.

**Ahora.** Un helper antepone la comilla simple que las planillas entienden como «esto es texto,
no fórmula». Toca **sólo textos**: los montos los generamos nosotros y deben seguir siendo números
— la primera versión de la prueba los marcaba a ellos también y habría dejado al contador sin
poder sumar la columna, y a un monto negativo convertido en texto.

**El nombre viaja NEUTRALIZADO, no borrado.** El contador tiene que poder leer quién es el
proveedor: esconder el dato para protegerlo sería romper el reporte para arreglar la seguridad.

#### 15. Anti-duplicado BLANDO en el alta de compras (409 nuevo), y el texto del 409 reescrito

**Antes.** El alta de compras sólo tenía el chequeo **exacto** (misma empresa + mismo RUT de
proveedor + mismo número de documento). Se burlaba con ceros a la izquierda, con prefijos
(`'F-1234'`) o con el RUT formateado contra el canónico.

**Ahora.** Además del exacto, compara **RUT canonizado + folio blando** contra todas las compras
activas y responde **409 nombrando la compra que ya existe**: su número interno, su N° de documento
y su RUT. Los helpers son copia deliberada de los del libro SII — no se comparten entre paquetes.

**Y se reescribió el texto del mensaje, que es parte del arreglo.** La frase vieja terminaba con
«si es otra, corrija el N° de documento»: era **la receta exacta para fabricar el duplicado** que
el guard existe para impedir. El operador que lee medio cartel en cuatro segundos le cambiaba un
dígito al folio y nacía la segunda cuenta por pagar. Hoy el mensaje manda a ver la compra existente
**con el papel a la vista**, dice que si es la misma no se registre de nuevo, y que si de verdad es
otra hay que avisar a contabilidad antes de forzarla.

#### 16. Tesorería: 409 con cruce confirmado del libro también al DESCONCILIAR, y el mensaje nombra el camino exacto

**Antes.** Borrar un movimiento bancario y borrar una cartola sólo miraban la marca de
«conciliado» del movimiento, y **desconciliar no miraba nada del libro**. Apagar esa marca dejaba
huérfano un cruce ya confirmado: el movimiento volvía a la lista de pendientes y **la misma plata
podía explicar un documento del libro Y otra conciliación**. Borrar el movimiento dejaba, por la
regla de borrado en cascada suave, un cruce confirmado sin movimiento.

**Ahora.** El mismo guard se consulta en los **tres** caminos —eliminar cartola, eliminar
movimiento y desconciliar— y responde 409 con el **número del cruce**.

**Y el texto cambió por una razón de usabilidad que vale como lección:** decía «descártelo en la
bandeja del Libro SII», y en esa bandeja **ese cruce no aparecía**. El operador iba, no encontraba
nada y quedaba en un punto muerto. Hoy dice el camino exacto: *Libro SII → Conciliación bancaria →
pestaña «Conciliados», busque el N° X y use «Deshacer conciliación»* — y esa pestaña se construyó
en la misma ronda, con buscador por número.

**Nota del lente de ciclo de vida, importante aunque no esté en la lista:** ese guard **fallaba
abierto**. Si el paquete del libro estaba instalado pero su carga reventaba (un deploy a medias),
Tesorería concluía «no hay nada que proteger» **sin haber mirado**. Hoy distingue *ausente* (el
paquete no está: no puede haber cruces, se sigue) de *roto* (el paquete está y no carga: se
bloquea con un 503 y se pide un humano). La regla de la casa, otra vez: ante un dato que no se
pudo consultar, bloquear.

**[PRECISIÓN 2026-08-08 · segunda pasada]** Esa lección quedó escrita más ancha de lo que el
código cubre. **Queda una rama abierta:** si el paquete carga bien pero la tabla del cruce no
existe en la base —un despliegue al que le faltó crear tablas— la consulta falla y el guard
responde «no hay nada que proteger», dejando borrar o desconciliar. Es la misma familia de
falla, en otra puerta. Ver §6.D.5.

### Arquitectura

#### 17. El JSON crudo pasa a carga diferida: la bandeja dejó de arrastrar 60 KB por fila

**Antes.** El tablero, la bandeja y el CSV traían de la base **todos** los documentos activos con
la columna `raw_json` incluida —el documento completo tal como lo entregó Wasabil, hasta 60.000
caracteres por fila— que **ningún endpoint lee**.

**Ahora.** Esa columna está declarada como *deferred*: no viaja en las consultas normales, y quien
la necesite la carga cuando toca el atributo. **La Regla 26 del plan sigue vigente** (se guarda el
JSON crudo recortado a 60.000 caracteres, para poder diagnosticar un documento raro sin
credenciales de Wasabil); lo único que cambió es **cómo se carga**.

**[PRECISIÓN 2026-08-08 · segunda pasada]** «Sigue vigente» vale para la mitad del JSON crudo.
**La otra mitad de la Regla 26 —la bitácora— guarda menos contadores de los que la regla
pide:** no anota páginas recorridas, ni divergentes, ni huérfanos. Ver §6.C.6.

#### 18. Cierre del grafo de claves foráneas: sin esto el backend no arrancaba con el gate de Monza apagado

**Antes.** El paquete del libro de Monza sólo importaba los modelos de Tesorería de Monza. Con el
interruptor `MONZA_CONTAB_ENABLED` en falso, nada más registraba las tablas de egresos, adelantos
y cotizaciones de Monza, y **la creación de tablas del arranque reventaba** con «tabla referenciada
inexistente» en cualquier base fresca. Dicho en criollo: **la salida de emergencia documentada
—apagar Monza— tumbaba las DOS marcas**.

**Ahora.** Cada paquete cierra su propio grafo con imports explícitos al final del módulo: el de
Monza importa Tesorería + el núcleo Monza + Contabilidad + Compras; el de Grupo AM, por paridad
inversa, importa Tesorería + Compras + Embarques Pricing. El costo es unas tablas vacías
inofensivas cuando el gate está apagado, que es infinitamente mejor que no arrancar. Hay una
prueba que verifica el cierre importando el paquete en un intérprete limpio.

### Lo que la pantalla PROMETE

#### 19. La clasificación dice la verdad sobre lo que hoy hacen «costo por venta» y «activo fijo»

**Antes.** La etiqueta «Costo por venta» prometía implícitamente lo que promete el plan de arriba
—capitalizar al embarque vía Fase B—, y «Activo fijo» sugería que algo calcularía depreciación.
**Nadie lee todavía esos destinos**: la Fase B no existe.

**Ahora.** Las opciones de la pantalla dicen textualmente **«Costo por venta (queda marcado;
todavía no se suma al costo del embarque)»** y **«Activo fijo (queda marcado; todavía no calcula
depreciación)»**.

**Por qué no se quitaron las opciones:** la decisión sí queda auditada, con quién la tomó y
cuándo, y ese registro es la materia prima de la Fase B. Prometer el efecto en presente es lo peor
de los dos mundos: el contador cree que ya imputó y ni siquiera revisa el costo a mano. Los
candados del backend no se tocaron: un emisor BLOQUEADO no puede clasificarse como costo por venta,
y una nota de crédito tampoco — los dos siguen respondiendo 409.

**[PRECISIÓN 2026-08-08 · segunda pasada]** «Queda auditada» vale **mientras nadie la
deshaga**: volver un documento a «pendiente» borra de una vez la decisión, el destino, el
motivo, quién decidió y cuándo, sin dejar rastro de que hubo una decisión antes ni de quién la
deshizo. Ver §6.D.4.

---

## §3. Lo que NO cambió (para que nadie lo «arregle» de nuevo)

- **El barrido nunca borra.** Sigue siendo UPSERT por identificador único del documento, y el que
  deja de venir se marca DESAPARECIDO con su decisión y su rastro intactos (Regla 5).
- **La zona remota y la zona local siguen separadas** (Regla 6): el barrido pisa ciegamente lo que
  manda Wasabil y **jamás** toca la decisión humana. *(Precisión 2026-08-08: la frontera no es
  exactamente la que dibujó el plan — el barrido sí escribe si el documento sigue vivo ante el
  SII y sí enciende la alarma de divergencia. Lo que nunca toca es la decisión. Ver §6.C.2.)*
- **La red va siempre ANTES de abrir la transacción de escritura.** Ninguna llamada a Wasabil
  ocurre dentro de una transacción con candados.
- **El candado de marca sigue en la línea del constructor del router** (Regla 25):
  `require_empresa("mineria")` en Grupo AM, `require_empresa("automotriz")` en Monza. La revisión
  de seguridad lo verificó en los cuatro routers, sin hallazgos, junto con cero fuga entre marcas,
  cero inyección SQL y el token de Wasabil nunca en logs ni en respuestas.
- **La edad del último barrido exitoso sigue siendo el semáforo** (Regla 24), en rojo pasadas 48
  horas, calculada desde la bitácora y no desde un indicador que el propio barrido encienda.
- **Se dejó A PROPÓSITO sin tocar** la caducidad automática de sugerencias huérfanas del matcher:
  su máquina de estados declara «jamás borrado silencioso», y descartar es un acto humano.

## §4. Las 26 reglas del plan, hoy

> **Tabla corregida el 2026-08-08 (segunda pasada).** La primera versión de esta tabla daba
> por «Vigentes» seis reglas que el código no cumple, y por «no construidas» dos que sí
> corren. Cada fila de abajo se volvió a verificar contra el código, línea por línea.
>
> **Cómo leer la columna Estado — hay tres pilas y una mezcla:**
> · **CONSTRUIDA** — está en el código y hace lo que la regla dice.
> · **CONSTRUIDA DISTINTO** — se resolvió el mismo problema de otra manera. La regla NO se
>   derogó, pero su redacción del cuerpo quedó vieja y no hay que implementarla al pie de la
>   letra: el porqué del cambio está al lado.
> · **NO CONSTRUIDA** — es un compromiso a futuro. Nada de lo que promete ocurre hoy.
> · **PARCIAL** — una parte corre y otra no. Se dice cuál es cuál, porque confundirlas es
>   creerse protegido por un guard que no existe.
>
> Las dos marcas —Grupo AM/MachParts y MonzaParts— tienen cada una su propio paquete de
> código, duplicado a propósito. **Todo lo de esta tabla vale igual para las dos**: se
> verificó en ambas y no hay una sola diferencia de comportamiento en estas 26 reglas.

| Regla | Estado | Qué pasa de verdad |
|---|---|---|
| 1 — nunca tantear el endpoint de emisión | **CONSTRUIDA** | Resuelto por la documentación oficial (Addendum 2026-08-05): el listado es `POST /api/documents/query`, una ruta de consulta distinta de la que emite documentos |
| 2 — lectura incompleta no concluye | **PARCIAL** | **La CORRIDA sí cumple, y reforzada:** si falta una página, si el total cambia a mitad de camino o si un documento aparece dos veces, el barrido aborta entero y no marca a nadie como desaparecido; además se le sumó el cinturón de proporción (§2.4) contra la lista completa pero vacía. **Lo que NO cumple es el INFORME:** la bandeja y la exportación no miran si el último barrido salió bien — tras una corrida fallida siguen repartiendo «está / no está» sobre el espejo viejo y el archivo se descarga igual, sin fecha de corte. El único aviso es la edad del último barrido exitoso en el tablero. Ver §6.D.1 |
| 3 — tres cubetas | **CONSTRUIDA DISTINTO** | En la práctica hay cuatro valores: ESTÁ / NO ESTÁ / INDETERMINADO, más `N/A` para los desaparecidos (§2.7). Y el folio parecido degrada a INDETERMINADO, nunca a NO ESTÁ (§2.8). **Falta la segunda mitad de la regla:** la pantalla nunca dice «datos del SII hasta <fecha del último barrido exitoso>»; muestra la EDAD de ese barrido («hace 6 h») y, aparte, la fecha del último INTENTO, que puede ser la de una corrida fallida. Ver §6.D.2 |
| 4 — identidad por identificador único | **PARCIAL** | **Vigente la primera mitad:** el identificador único del documento tiene llave dura, dos barridos simultáneos no pueden duplicar una fila, y la llave de negocio (RUT + tipo + folio) lleva su índice no único. **La segunda mitad no existe:** el estado DUPLICADO_SOSPECHOSO no está en el código ni en la base, nadie detecta la colisión y el barrido escribe sin mirarla. Empíricamente hoy no hay colisiones, pero si aparecen, pasan sin ruido. Ver §6.B.3 |
| 5 — barrido completo, sin DELETE, idempotente | **CONSTRUIDA DISTINTO** | **Cuidado, la redacción del cuerpo es falsa donde más importa: SÍ hay ventana por fecha.** El barrido pregunta por los documentos de los últimos 24 meses (calculados como 24 × 31 días) y no vuelve a leer nada anterior: un documento más viejo que eso no se actualiza, no se le detecta divergencia y no puede reaparecer. Se eligió así porque el volumen no lo justificaba y la ventana cubre de sobra los plazos del crédito fiscal — pero es un límite real, no una idempotencia total. Lo que sí se cumple entero: nunca se borra nada, N corridas dan lo mismo que una, y el que deja de venir se marca DESAPARECIDO. El marcar-desaparecido quedó además acotado a la propia ventana (§2.5) y con tope de proporción (§2.4). Ver §6.C.1 |
| 6 — zona remota vs zona local | **CONSTRUIDA DISTINTO** | **La frontera se movió y conviene saber dónde quedó.** Sigue siendo verdad lo que importa: el barrido JAMÁS toca la decisión humana (qué se decidió, a dónde, con qué motivo, quién y cuándo). Pero el barrido sí escribe tres cosas que el plan había puesto del lado humano: si el documento sigue vivo ante el SII, y la alarma de divergencia con su explicación. Es correcto que las escriba —son hechos que sólo el barrido puede observar— pero el dibujo original decía otra cosa. Ver §6.C.2 |
| 7 — divergencia por lista explícita de campos | **CONSTRUIDA DISTINTO** | La idea (hash de una lista explícita, no del documento entero) se cumple, y la comparación se volvió tolerante a tipos: un monto guardado como número contra el mismo monto como texto ya no se lista como cambio. **Pero la lista NO es la del cuerpo.** No se vigila la «situación» del documento (esa columna ni siquiera existe) y sí se vigilan tres campos que el plan no nombraba: el RUT del emisor, su nombre y el estado de acuse de recibo — o sea que **un proveedor que corrige cómo se escribe su nombre dispara una divergencia**, y eso el cuerpo no lo contempla. Segunda diferencia: el plan decía que un documento IGNORADO se sobrescribe callado, y no es así — **cualquier documento sobre el que alguien ya decidió algo, incluido el ignorado, enciende la alarma.** Es más ruidoso y más honesto. Ver §6.C.4 |
| 8 — sólo los montos declarados al SII | **CONSTRUIDA** | — |
| 9 — una sola magnitud sumable | **CONSTRUIDA** | Se le agregó el caso del documento ilegible: monto vacío, nunca cero (§2.6) |
| 10 — identidad del proveedor y RUT normalizado | **PARCIAL** | La identidad se toma del emisor del documento y se normaliza; el pre-llenado entrega el RUT normalizado y no el bonito (§2.10). **No se construyó la aserción defensiva** que la regla pedía —abortar la fila si el documento viene con el RUT de la propia empresa— ni viaja el identificador de empresa en la consulta al API. Lo que separa hoy los libros de las dos marcas es que **cada marca usa su propio token de Wasabil**. Ver §6.D.3 |
| 11 — RUT obligatorio y canónico en compras nuevas | **PARCIAL** | **Construido:** la columna `rut` en la tabla de proveedores (con su script de migración por marca) y el normalizador. **NO construido, y es la mitad que protegía:** el RUT sigue siendo OPCIONAL al crear una cuenta por pagar y se guarda tal como se teclea. Una compra sin RUT entra hoy sin ningún reclamo. La defensa que sí existe es otra: al guardar se compara RUT normalizado + folio blando contra las compras vivas y se rechaza nombrando la que ya está (§2.15). Ver §6.B.2 |
| 12, 13, 14, 15, 17, 18, 19, 21, 22, 23 — Fase B | **NO construidas** | No existe la tabla puente documento↔gasto de embarque, ni el sello en Wasabil, ni su interruptor, ni el «aplicar a la línea», ni el prellenado del acreedor real, ni el guard de magnitud. Nada de lo que prometen ocurre |
| 16 — clasificación por RUT en tres niveles | **PARCIAL, y CORRIGE la tabla anterior: sí está construida** | Corre desde la Fase A2: la tabla de reglas por RUT existe, se listan y se crean/editan desde la pantalla con motivo y usuario, y el nivel BLOQUEADO es un candado duro (rechaza clasificar ese documento como costo por venta). **Dos partes no se hicieron:** el nivel IGNORAR_AUTO **no archiva nada solo** en la bandeja —su único efecto real es sacar a ese proveedor del cruce con el banco—, y el «aparece primero» no ocurre (la bandeja se ordena por fecha). Ver §6.B.4 |
| 20 — la nota de crédito no se vincula | **PARCIAL, y CORRIGE la tabla anterior: su guard sí está construido** | Una nota de crédito no se puede pre-llenar como compra ni clasificar como costo por venta: en los dos caminos el sistema responde rechazando y explicando por qué. **Lo que no existe** es la otra mitad: enlazar la nota de crédito con la factura que corrige. Esos datos del documento padre no se guardan en ninguna columna del espejo |
| 24 — edad del barrido en el tablero | **CONSTRUIDA** | Se calcula desde la bitácora de corridas, no desde un indicador que el propio barrido encienda, y se pone en rojo pasadas 48 horas |
| 25 — candado de marca y motor InnoDB | **PARCIAL** | **El candado de marca sí está**, en la línea del constructor de los cuatro routers (`mineria` en Grupo AM, `automotriz` en MonzaParts), verificado sin hallazgos, igual que el motor InnoDB explícito. **La columna `empresa` NO está en ninguna de las tres tablas del libro**, en ninguna de las dos marcas. Es una decisión, no un olvido — el aislamiento se resolvió por paquete, por tabla y por token — pero significa que la separación depende del candado del router y no de un filtro por fila. Ver §6.C.3 |
| 26 — JSON crudo + bitácora por corrida | **CONSTRUIDA DISTINTO** | El JSON crudo se sigue guardando recortado a 60.000 caracteres y ahora se carga en diferido (§2.17). Y el campo de error de la bitácora **ya no implica fallo**: puede llevar un aviso sobre una corrida exitosa (§2.6). **Pero la bitácora guarda menos de lo que la regla pide:** anota origen, inicio, fin, si salió bien, cuántos documentos declaró el API, cuántos nuevos, cuántos actualizados, cuántos desaparecidos, el error o aviso, el rango de fechas consultado y el usuario. **No guarda páginas, ni divergentes, ni huérfanos** — y no tiene columna de última actualización, que es justamente lo que impidió el «sigo vivo» del §2.3. Ver §6.C.6 |

## §5. Lo que este addendum NO dice

- **No se revisaron las Decisiones del dueño** (D1 a D10) ni el Addendum del 2026-08-05: siguen
  como estaban. **[CORREGIDO 2026-08-08 · segunda pasada]** La frase original decía que «las
  respuestas de D2/D3/D4 se incorporaron a la clasificación», y eso es cierto sólo de una de
  las tres:
  · **D3 (Vector Capital es compra de moneda, no factoring) SÍ está incorporada** como
    herramienta: existe el nivel BLOQUEADO por RUT y su candado duro. Falta confirmar contra
    la base de producción que la fila de ese RUT esté efectivamente cargada — cargarla es un
    dato, no código.
  · **D2 (una factura puede cubrir varios embarques) NO está incorporada**: exigía la tabla
    puente con el monto asignado a cada embarque, que es Fase B y no existe.
  · **D4 (las facturas de la empresa hermana entran, pero MARCADAS como parte relacionada)
    NO está incorporada**: esa marca no está construida en ninguna tabla. Hoy esas facturas
    se ven y se clasifican como cualquier otra, y ningún informe las puede separar.
- **No se tocó la sección de refutación adversarial** del plan original: sus agujeros siguen
  siendo la mejor lista de lo que hay que cuidar cuando se construya la Fase B. Varios de sus
  parches ya se aplicaron sin decirlo (la separación de ejes del estado del espejo, el parser con
  ruta explícita del envelope `data.list.items`, la marca de agua por número de corrida en vez de
  un sí/no global); los que apuntan a la Fase B siguen pendientes por definición.
  **[PRECISIÓN 2026-08-08 · segunda pasada]** Hay un parche del auditor técnico que NO se
  aplicó y que conviene no dar por cubierto: el que pedía mandar el identificador de empresa
  en cada llamada al API y verificar documento por documento que viniera de la empresa
  correcta. No se hizo ninguna de las dos cosas. Lo que impide hoy que el libro de una marca
  entre al espejo de la otra es que **cada marca usa su propio token de Wasabil**; si algún
  día se emitiera un token con acceso a las dos empresas, esa defensa desaparece sin avisar.
  Ver §6.D.3.
- **La Fase B no tiene fecha.** Mientras no exista, todo lo que la pantalla dice sobre «costo por
  venta» y «activo fijo» es una marca auditada, no un efecto contable — y así lo declara la propia
  pantalla desde §2.19.

---

# §6. SEGUNDA PASADA 2026-08-08 — qué está construido, qué no, y qué se hizo distinto

> **Por qué existe esta sección.** El addendum de arriba se escribió mirando el código, pero
> se quedó corto: una revisión adversarial posterior encontró **catorce afirmaciones falsas**
> repartidas entre el cuerpo del plan y la tabla del §4 —incluida la más engañosa de todas,
> que el barrido no usa ventana por fecha cuando sí la usa— y **diez huecos** de cosas que
> pasaban en el código y no estaban escritas en ninguna parte. Cada una se volvió a verificar
> contra el código antes de escribirla acá; dos de las catorce resultaron falsa alarma y
> están al final, en §6.E, para que nadie las «arregle» creyendo que son bugs.
>
> **Cómo usar esta sección.** Es el índice de verdad del documento. Si algo de arriba
> contradice algo de acá, manda esto. Está partida en cuatro: lo que **está construido**
> (§6.A), lo que se **planificó y no se hizo** (§6.B), lo que se hizo **distinto de lo
> planificado y por qué** (§6.C), y los **bordes que siguen abiertos** (§6.D) — algunos por
> decisión, otros porque nadie llegó todavía, y se dice cuál es cuál.
>
> **Las dos marcas.** Grupo AM/MachParts y MonzaParts tienen cada una su propio paquete de
> código, duplicado a propósito, sin una sola pieza compartida. Todo lo de esta sección se
> verificó en las dos y **no hay ninguna diferencia de comportamiento**: mismos límites,
> mismos números, mismos textos. La única diferencia real es de plomería y está dicha donde
> corresponde: cada marca habla con Wasabil usando su propio token, y MonzaParts separa sus
> datos por tabla (todas sus tablas llevan el prefijo `monza_`) en vez de por columna.

## §6.A — Lo que está CONSTRUIDO y corriendo

En las dos marcas, hoy, funciona esto:

1. **El cliente que le pide a Wasabil la lista de facturas recibidas** (Fase 0), por la ruta
   de consulta oficial. Lee todas las páginas y, si algo no calza —falta una página, el total
   cambia a mitad de camino, un documento aparece dos veces—, aborta la corrida entera en vez
   de concluir a medias.
2. **El espejo del libro y su barrido** (Fase A1): una fila por documento recibido, que se
   refresca todas las noches a las 05:30 con un trabajo programado propio, más un botón de
   «sincronizar ahora» con enfriamiento de dos minutos para no quemar llamadas pagadas.
3. **El tablero** con la edad del último barrido exitoso (rojo pasadas 48 horas), el reparto
   por cubetas, los pendientes de decisión, los divergentes y desaparecidos, y la cuadratura
   mensual entre el libro del SII y las cuentas por pagar del ERP.
4. **La bandeja** (Fase A2): cada documento cae en una de tres cubetas —está en el ERP, no
   está, o no se puede determinar— con las acciones de ignorar y clasificar en un clic, y la
   exportación a planilla del listado de faltantes.
5. **Las reglas por RUT de proveedor** en tres niveles, con motivo y usuario, y el candado
   duro del nivel BLOQUEADO.
6. **El pre-llenado del formulario de Compras** desde un documento del libro, con los
   avisos de posible duplicado, y el rechazo de las notas de crédito.
7. **El matcher banco↔libro**, que este plan no contemplaba y tiene especificación aparte
   (`docs/spec-matcher-banco-libro-2026-08-06.md`).

## §6.B — Lo que se PLANIFICÓ y NO se construyó

### B.1 · La Fase B entera, y con ella las seis ecuaciones de cuadratura

No existe la tabla que uniría un documento del SII con una línea de gasto de un embarque, ni
el «aplicar el monto a la línea», ni el sello en Wasabil, ni su interruptor. Como consecuencia
directa, **ninguna de las seis ecuaciones E1 a E6 del modelo de datos existe**, y tampoco el
«informe de una hoja que las muestra en cero». La frase del plan «E6 ES el entregable de Fase
A» quedó vieja: el entregable que se construyó y que el dueño usa es la bandeja de las tres
cubetas con su exportación.

Lo único que se parece a una cuadratura es la **comparación mensual libro-contra-ERP** del
tablero, que es otra cosa y tuvo que corregirse dos veces (§2.12).

### B.2 · Regla 11 — el RUT del proveedor sigue siendo opcional al crear una cuenta por pagar

Es la mitad que faltó y conviene entenderla, porque es la que sostenía el resto del plan.

Se construyó: la columna de RUT en la ficha de proveedores (con su script de migración en
cada marca) y el normalizador que deja todos los RUT en una sola forma. **No se construyó:**
que el RUT sea obligatorio al crear una cuenta por pagar y que se guarde normalizado. Hoy se
guarda tal como lo teclea la persona, y si no lo teclea, la compra entra igual.

**Por qué importa.** La llave con la que se cruza «esta factura del SII, ¿está en el ERP?» es
RUT + número de documento. Con el RUT en blanco o escrito de otra forma, ese cruce no empata,
y el documento aparece como faltante aunque ya esté cargado. El plan preveía exactamente ese
daño (Convergencia 3).

**Qué se puso en su lugar, y por qué alcanza para lo grave.** El daño real que se temía no era
el falso faltante, sino lo que el operador hace con él: cargar la factura de nuevo y dejar la
misma deuda dos veces. Contra eso se construyeron tres cosas: (a) el cruce de la bandeja no
compara el folio como texto exacto sino también «blando», sin ceros a la izquierda ni
prefijos, y si sólo calza así **no dice «no está», dice «no se puede determinar» con una
leyenda que nombra la compra parecida** (§2.8); (b) esa leyenda viaja al formulario y deja el
botón de guardar trabado hasta que la persona confirma que la buscó (§2.9); y (c) al guardar,
el sistema compara RUT normalizado + folio blando contra todas las compras vivas y rechaza
nombrando la que ya existe (§2.15). No es lo que la Regla 11 prometía, pero ataca el mismo
daño desde el otro lado.

### B.3 · Regla 4 — la marca de «duplicado sospechoso» no existe

La primera mitad de la regla sí está: cada documento tiene su llave dura por identificador
único, y la llave de negocio (RUT + tipo + folio) tiene su índice sin exigir unicidad,
exactamente como se pedía y por el motivo que se pedía — una unicidad dura ahí abortaría el
barrido entero si Wasabil llegara a mandar dos documentos con la misma llave.

Lo que no existe es qué hacer cuando eso pase: **el estado «duplicado sospechoso» no está en
el código ni en la base, nadie busca la colisión y el barrido escribe sin mirarla.** Hoy no
hay colisiones en los datos reales, así que el riesgo es latente y no actual; pero si
apareciera una, entraría sin ruido.

### B.4 · Regla 16 — el nivel IGNORAR_AUTO no archiva nada solo, y no hay filtro por nivel

La regla prometía tres cosas y se construyeron dos.

Construido: los tres niveles por RUT, con motivo y usuario, visibles y reversibles; y el
candado duro de BLOQUEADO, que impide clasificar como costo de mercadería un documento de un
proveedor financiero.

**No construido:**
- **IGNORAR_AUTO no archiva ningún documento por su cuenta en la bandeja.** Su único efecto
  real hoy es otro: saca a ese proveedor del cruce automático con la cartola del banco. Un
  documento de un proveedor marcado así sigue apareciendo en la bandeja esperando decisión.
  Es una promesa de comodidad incumplida, no un riesgo: nada se decide solo.
- **No hay filtro «sólo proveedores de embarque»**, ni ninguna forma de filtrar por nivel. El
  nivel se VE en cada fila, así que se puede leer, pero no se puede pedir la lista filtrada.
- **El proveedor logístico no «aparece primero»:** la bandeja se ordena por fecha del
  documento, de la más nueva a la más vieja.

### B.5 · Decisión D4 — la marca de «parte relacionada» no está construida

El dueño aprobó que las facturas de la empresa hermana entren normalmente pero **marcadas**
como intercompañía, para que un informe de rentabilidad las pueda separar y para dejar el
legajo listo si el SII pregunta por precios entre relacionados. Esa marca no existe en ninguna
tabla. Hoy esas facturas se clasifican como cualquier otra y nada las distingue.

### B.6 · Las cuatro variables de configuración del plan, y los cuatro campos nuevos de la tabla de compras

Ninguna de las cuatro variables se declaró nunca, y ninguno de los cuatro campos nuevos de la
tabla de cuentas por pagar se agregó. El detalle, con lo que hay en su lugar, está en los dos
recuadros del **Modelo de datos** (bloque CONFIG y bloque de cambios sobre `cont_compra`).

## §6.C — Lo que se construyó DISTINTO de lo planificado, y por qué

### C.1 · Regla 5 — el barrido SÍ tiene ventana por fecha: 24 meses

**Ésta es la afirmación más engañosa que tenía el documento**, porque la regla está redactada
en mayúsculas y dice lo contrario: «el barrido es COMPLETO todas las noches, SIN VENTANA POR
FECHA de documento».

**Lo que hace el código:** cada noche le pide a Wasabil los documentos con fecha desde hace 24
meses hasta hoy. Dentro de esa ventana el barrido sí es completo e idempotente, nunca borra
nada, y el que deja de venir se marca como desaparecido.

**Lo que eso significa, dicho sin adornos:** un documento con fecha anterior a la ventana **no
se vuelve a leer jamás**. No se actualiza, no se le detecta si cambió, y si estaba marcado
como desaparecido no puede revivir. Su fila del espejo queda congelada con lo último que se
supo de él.

**Por qué se hizo así y no como decía el plan.** Traer todo el histórico en cada barrido no
agregaba nada —el propio plan midió que antes de 24 meses hay casi cero documentos— y sí
agregaba páginas, tiempo y llamadas pagadas todas las noches. Los 24 meses cubren de sobra los
plazos legales del crédito fiscal. La decisión es defendible; **lo indefendible era no
escribirla**, porque un lector que confía en la regla tal como está redactada creería que el
espejo cubre todo el histórico.

**Un efecto secundario que sí se corrigió:** como la ventana se desliza todos los días, al
principio el barrido marcaba como «desaparecido del SII» a todo documento que se le salía por
vejez — inventando desapariciones que no ocurrieron. Hoy sólo se marca desaparecido dentro de
la ventana consultada, y un documento sin fecha no se marca nunca (§2.5).

**Y un detalle que se descubrió al verificar:** la ventana se calcula como 24 × 31 días, o sea
unos 24 meses y medio corridos, no 24 meses de calendario. Es más generoso que lo prometido,
así que no hace daño, pero explica por qué el borde no cae en una fecha redonda.

### C.2 · Regla 6 — la frontera entre «lo que manda Wasabil» y «lo que decide un humano» se movió

Sigue siendo verdad lo esencial, y es lo que protege el trabajo del operador: **el barrido
jamás toca la decisión humana** — qué se decidió sobre el documento, a dónde se clasificó, con
qué motivo, quién lo hizo y cuándo.

Lo que cambió: el plan había puesto del lado humano tres cosas que hoy escribe el barrido.
- **Si el documento sigue vivo ante el SII o dejó de venir.** Es un hecho del mundo, no una
  decisión, y sólo el barrido puede observarlo.
- **La alarma de «esto cambió después de que alguien decidió», con su explicación.** Idem: el
  que detecta el cambio es el barrido.

Es correcto que el barrido las escriba. Pero el modelo de datos del cuerpo las lista bajo el
rótulo «zona local, el barrido NO la toca», y el propio código conserva ese rótulo encima del
bloque donde vive la alarma. **Quien lea ese rótulo y no esta nota, va a creer que el barrido
no escribe ahí, y sí escribe.** La lectura correcta de la Regla 6 hoy es: el barrido puede
encender alarmas, nunca puede borrar ni cambiar una decisión.

### C.3 · Regla 25 — el candado de marca está; la columna `empresa` en las tablas del libro no

El candado está donde tiene que estar: en la línea del constructor de los cuatro routers, de
modo que ningún usuario de una marca puede llegar a los datos de la otra. Verificado sin
hallazgos, junto con el motor de base de datos explícito que los bloqueos requieren.

Lo que no está: **ninguna de las tres tablas del libro tiene la columna `empresa`**, en
ninguna de las dos marcas. El aislamiento se resolvió en tres capas distintas: cada marca
tiene su propio paquete de código, MonzaParts guarda sus datos en tablas propias con prefijo
`monza_`, y cada marca habla con Wasabil usando su propio token — así que a la tabla de Grupo
AM sólo pueden entrar documentos del libro de Grupo AM.

**Qué se pierde con eso.** Que la separación deja de estar en el dato y pasa a depender del
candado de la puerta. Si mañana alguien monta un endpoint nuevo y se olvida del candado, no
hay un filtro por fila que lo salve. Por eso la sonda que recorre las rutas verificando que
ninguna quedó sin candado no es opcional: es lo que reemplaza a la columna.

*(La única tabla del módulo que sí lleva esa columna es la del matcher banco↔libro de Grupo
AM, que nació de otra especificación.)*

### C.4 · Regla 7 — se vigilan otros campos, y el documento ignorado también dispara la alarma

La idea se respetó: se compara una lista **explícita** de campos, no el documento entero,
justamente para que un cambio cosmético del API no grite «cambió» sin motivo. Y se le agregó
una mejora: la comparación es tolerante a tipos, así que un monto guardado como número contra
el mismo monto escrito como texto ya no se lista como cambio (antes salían TODOS los montos
marcados como cambiados, y eso enterraba el campo que sí había cambiado).

**Dos diferencias con lo escrito en el cuerpo:**
1. **La lista no es la misma.** No se vigila la «situación» del documento —esa columna ni
   siquiera existe en el espejo— y sí se vigilan tres campos que el plan no nombraba: el RUT
   del emisor, **su nombre** y el estado de acuse de recibo. La consecuencia práctica: si un
   proveedor corrige cómo se escribe su razón social, el documento se marca como divergente
   aunque no haya cambiado un peso. No es un error —también es un cambio que alguien debería
   mirar— pero el cuerpo no lo contempla y conviene saberlo antes de asustarse.
2. **El documento IGNORADO ya no se sobrescribe callado.** El plan decía que sólo los
   documentos vinculados o más avanzados encendían la alarma. Hoy la enciende **cualquier
   documento sobre el que alguien ya tomó una decisión**, incluido el que se decidió ignorar.
   Tiene sentido: si un documento se ignoró porque valía poco y ahora vale diez veces más, esa
   decisión merece revisarse.

### C.5 · Fase 0 — el normalizador de RUT no es compartido: cada marca tiene el suyo

El plan pedía un único archivo `backend/rut.py` usado por todo el sistema. No se hizo así:
cada paquete tiene su propia copia, siguiendo la regla de la casa de no compartir código entre
marcas. El criterio es uno solo —sin puntos, con guion, dígito verificador en mayúscula, y con
validación del dígito— y se probó en ambos lados, pero son dos copias. El costo es el
conocido: el día que haya que cambiarlo, hay que cambiarlo dos veces.

La columna de RUT en la ficha de proveedores sí se agregó, con su script de migración por
marca, y hay un camino en la pantalla para completarla de a un proveedor por vez desde la
propia bandeja — que es la alternativa al arreglo masivo del histórico que el plan prohibía.

### C.6 · Regla 26 — la bitácora guarda menos contadores de los que la regla pide

Cada corrida deja su fila, y eso permite responder «¿de cuándo son estos datos?» y «¿desde
cuándo está roto?», que es lo que la regla buscaba. Pero guarda **origen, inicio, fin, si
salió bien, cuántos documentos declaró el API, cuántos nuevos, cuántos actualizados, cuántos
desaparecidos, el error o aviso, el rango de fechas consultado y el usuario**. La regla pedía
además **páginas recorridas, divergentes y huérfanos**, y esos tres no están.

Tampoco hay una columna de «última actualización» de la corrida — y ésa es exactamente la
razón por la que el «sigo vivo» del §2.3 no se pudo construir y quedó sólo el mecanismo de
turnos por número.

### C.7 · El tablero: cinco números, pero no los cinco del plan

El plan listaba como cuarto número los «sellos pendientes». El sello es Fase B y no existe, así
que ese número se reemplazó por **los documentos pendientes de decisión**, que es la cola de
trabajo real del operador. Los cinco de hoy están en §6.A punto 3.

## §6.D — Bordes conocidos que siguen ABIERTOS

> Ninguno de estos seis es una promesa incumplida del plan: son cosas que el código hace y
> que nadie había escrito. Se listan para que quien las encuentre sepa que ya se miraron, y
> se dice en cada una si están así por decisión o por pendiente.

### D.1 · Tras un barrido FALLIDO, la bandeja y la exportación siguen afirmando · PENDIENTE

La Regla 2 se cumple **dentro de la corrida**: si la lectura quedó a medias, la corrida se
marca fallida y no se marca a nadie como desaparecido. Pero **el informe no se entera**: la
bandeja sigue repartiendo «está / no está» sobre el espejo de la última vez que salió bien, y
el archivo se descarga igual, sin decir a qué fecha corresponde. El único aviso es la edad del
último barrido exitoso en el tablero, que se pone en rojo a las 48 horas.

El propio auditor técnico del plan ya había propuesto el arreglo («no pude determinarlo» es un
estado del INFORME, no del documento: si la última corrida falló, el reporte no afirma y la
exportación se deshabilita). Sigue sin aplicarse.

### D.2 · La pantalla no dice «datos del SII hasta <fecha>» · PENDIENTE

La Regla 3 lo exige y no ocurre. La pantalla muestra dos cosas distintas y ninguna es esa:
- **«Hace X horas»**, que sí se calcula desde el último barrido exitoso — o sea que la
  información existe, pero como antigüedad, no como fecha de corte.
- **«Último intento: <fecha>»**, que puede ser la fecha de una corrida **fallida**.

Y el archivo exportado no lleva fecha de corte en ninguna parte: una vez que sale del sistema,
nadie puede saber a qué momento corresponde. Es el arreglo natural que acompaña a D.1.

### D.3 · No se le dice a Wasabil de qué empresa es el libro que se pide · DECISIÓN, con riesgo residual

El plan pedía dos controles y no se construyó ninguno: mandar el identificador de empresa en
cada llamada, y verificar documento por documento que viniera de la empresa correcta. (El
segundo, además, estaba mal planteado en la Regla 10: pedía comparar contra el RUT propio, y
en un documento **recibido** ese campo trae al PROVEEDOR, nunca a uno mismo — la Convergencia
11 lo explica.)

**Lo que hoy separa los dos libros es el token:** cada marca usa su propia credencial de
Wasabil, y una credencial de una empresa sólo devuelve documentos de esa empresa. Es una
defensa real y suficiente mientras se cumpla esa condición.

**El riesgo residual, dicho claro:** el día que se genere un token con acceso a las dos
empresas —o que alguien cruce las credenciales al configurar el servidor, trampa que este
proyecto ya vivió una vez— el barrido empezaría a traer documentos de la otra sociedad **sin
que nada avise**, y el informe diría que a una marca le faltan por registrar facturas que son
deuda de la otra. El candado de marca del router no protege de esto: filtra quién lee, no de
dónde salieron los datos.

### D.4 · Deshacer una decisión no deja rastro de quién la deshizo · PENDIENTE

Volver un documento a «pendiente» borra de una vez la decisión, el destino, el motivo, quién
decidió, cuándo, la foto del documento al decidir, y apaga la alarma de divergencia. **No queda
ningún registro de que hubo una decisión antes, ni de quién la deshizo.**

Choca de frente con el argumento del §2.19, que justifica dejar visibles las opciones «costo
por venta» y «activo fijo» diciendo que «la decisión sí queda auditada, con quién la tomó y
cuándo». Queda auditada mientras nadie la deshaga.

### D.5 · El guard de Tesorería falla abierto si la tabla del cruce no está creada · DECISIÓN

El §2.16 cuenta —bien— que ese guard antes fallaba abierto y se cerró: si el paquete del libro
está instalado pero no carga, Tesorería bloquea el borrado y pide un humano, en vez de concluir
«no hay nada que proteger» sin haber mirado.

Falta acotar un caso que quedó fuera: **si el paquete carga pero la tabla del cruce no existe
en la base** —un despliegue al que le faltó correr la creación de tablas— la consulta falla y
el guard responde «no hay nada que proteger», dejando borrar o desconciliar. Es la misma
familia de falla que la lección declara, en una rama distinta.

### D.6 · El enfriamiento de dos minutos también lo dispara el barrido nocturno · PENDIENTE, menor

El §2.13 dice que el enfriamiento «no aplica al barrido nocturno», y es cierto: el nocturno
nunca se bloquea a sí mismo. Lo que no se dijo es lo otro: **el enfriamiento mira la última
corrida sea cual sea su origen**, así que el barrido nocturno exitoso de las 05:30 deja el
botón «sincronizar ahora» bloqueado durante dos minutos. Es una molestia de bajo impacto —a
las 05:32 no hay nadie en la oficina— pero explica un rechazo que de otro modo parece un bug.

## §6.E — Verificado y resultó FALSA ALARMA (que nadie lo «arregle»)

### E.1 · El aviso de un barrido que terminó bien SÍ se ve en el tablero

La revisión adversarial afirmó que el aviso de documento malformado del §2.6 («el barrido
terminó exitoso, y en la bitácora queda un AVISO…») era invisible, porque la pantalla sólo
pintaba ese texto cuando la corrida había fallado.

**Es falso, y se verificó en las dos marcas.** La pantalla tiene DOS recuadros distintos: uno
rojo cuando el barrido falló, y otro **ámbar** —con el texto «El último barrido terminó bien,
con un aviso: …»— que aparece cuando la corrida salió bien y trae texto en ese campo. El
comentario del código explica que ese segundo recuadro se agregó precisamente porque antes
sólo se pintaba el caso de falla. El §2.6 dice la verdad.

*(Matiz honesto: el mensajito emergente que aparece al apretar «sincronizar ahora» a mano sí
muestra sólo el caso de éxito sin repetir el aviso. Pero el tablero se recarga en ese mismo
instante, así que el recuadro ámbar queda a la vista.)*

### E.2 · El «si de verdad son dos documentos distintos, corrija el N°» del guard por embarque es deliberado

La revisión afirmó que la «receta exacta para fabricar el duplicado» que el §2.15 dice haber
erradicado seguía viva en otro guard del mismo archivo.

**El texto efectivamente sigue ahí, y se queda: no es un descuido, es una regla de negocio
distinta, escrita a propósito y presente igual en las dos marcas.** Son dos guards con
alcances distintos:
- El que el §2.15 reescribió es el del **alta de compras contra TODO el sistema**: ahí sí,
  invitar a cambiar el número es invitar a duplicar una deuda, y por eso el mensaje ahora
  manda a mirar la compra existente con el papel a la vista.
- El otro vive **dentro de un mismo embarque**, donde el mismo número de documento es
  necesariamente la misma factura aunque se cargue desde otra línea del pricing. Ahí «si de
  verdad son dos documentos distintos, corrija el N°» es la instrucción correcta: lo más
  probable es que alguien haya tecleado mal el folio de la segunda factura.

No se toca.
