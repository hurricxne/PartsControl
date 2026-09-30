# language: es
Característica: Registrar a mano una factura o boleta ya emitida, verificando su folio en Wasabil
  Como usuario de Contabilidad de MonzaParts
  Quiero registrar una factura o boleta emitida por fuera del flujo automático indicando su folio
  Para que la venta B2C y los documentos emitidos a mano entren al sistema sin folios inventados

  Antecedentes:
    Dado que la venta "COT-2026-000300" de "Juan Pérez" por $11.900 está cerrada
    Y el cliente de la venta tiene el RUT "76.543.210-3"

  Escenario: Una boleta emitida en Wasabil se verifica y se registra
    Dado que en Wasabil existe la boleta N° 35 emitida el 20-09-2026 por $11.900
    Cuando el usuario registra la boleta N° 35 de la venta indicando que se emitió en Wasabil
    Y verifica el folio
    Entonces el sistema muestra "Verificado en Wasabil" con la fecha y el total del documento
    Y al registrar, la boleta queda con fecha 20-09-2026 y ligada al documento de Wasabil

  Escenario: Una boleta sobre un despacho con la guía sin firmar se puede registrar
    Dado que la venta tiene el despacho "DSP-2026-0050" despachado con la guía sin firmar
    Y que en Wasabil existe la boleta N° 35 por $11.900
    Cuando el usuario registra la boleta N° 35 sobre el despacho "DSP-2026-0050"
    Entonces la boleta se registra
    Y el sistema advierte que la guía está sin firmar

  Escenario: Una factura sobre un despacho con la guía sin firmar sigue bloqueada
    Dado que la venta tiene el despacho "DSP-2026-0050" despachado con la guía sin firmar
    Cuando el usuario intenta registrar una factura sobre el despacho "DSP-2026-0050"
    Entonces el sistema indica que la guía debe estar FIRMADA antes de facturar
    Y no se registra ningún documento

  Escenario: Una factura sin guía de despacho se registra como retiro en oficina
    Dado que la venta no tiene despachos
    Y que en Wasabil existe la factura N° 120 emitida al RUT "76.543.210-3" por $11.900
    Cuando el usuario registra la factura N° 120 como retiro en oficina
    Entonces la factura se registra

  Escenario: El total del documento en Wasabil no coincide
    Dado que en Wasabil existe la boleta N° 36 por $12.500
    Cuando el usuario verifica la boleta N° 36 de la venta
    Entonces el sistema indica que el total en Wasabil ($12.500) no coincide con lo que se va a registrar ($11.900)
    Y no permite registrar la boleta

  Escenario: La factura en Wasabil está emitida a otro RUT
    Dado que en Wasabil existe la factura N° 121 emitida al RUT "11.111.111-1" por $11.900
    Cuando el usuario verifica la factura N° 121 de la venta
    Entonces el sistema indica que la factura está emitida a otro RUT
    Y no permite registrar la factura

  Escenario: El folio no existe en Wasabil
    Dado que en Wasabil no existe ninguna boleta N° 37
    Cuando el usuario verifica la boleta N° 37 indicando que se emitió en Wasabil
    Entonces el sistema indica que no existe esa boleta en Wasabil
    Y sugiere elegir "Emitido fuera de Wasabil" si corresponde

  Escenario: Un documento emitido fuera de Wasabil se registra con declaración
    Dado que en Wasabil no existe ninguna boleta N° 38
    Cuando el usuario registra la boleta N° 38 indicando que se emitió fuera de Wasabil
    Y escribe "Boleta emitida en el POS de la tienda" y confirma que el documento existe ante el SII
    Entonces la boleta se registra
    Y queda registrado quién la declaró, cuándo y con qué explicación

  Escenario: Un folio declarado como externo que sí está en Wasabil se rechaza
    Dado que en Wasabil existe la boleta N° 39
    Cuando el usuario registra la boleta N° 39 indicando que se emitió fuera de Wasabil
    Entonces el sistema indica que ese folio sí existe en Wasabil y que debe elegir "Emitido en Wasabil"

  Escenario: Si Wasabil no responde, un documento emitido en Wasabil no se registra
    Dado que Wasabil no responde
    Cuando el usuario intenta registrar la boleta N° 40 indicando que se emitió en Wasabil
    Entonces el sistema indica que no pudo consultar Wasabil y que reintente más tarde
    Y no se registra ningún documento

  Escenario: Si Wasabil no responde, un documento externo se registra marcado como no comprobado
    Dado que Wasabil no responde
    Cuando el usuario registra la boleta N° 41 indicando que se emitió fuera de Wasabil
    Y escribe "Boleta antigua emitida en el portal del SII" y confirma que el documento existe ante el SII
    Entonces la boleta se registra
    Y el sistema advierte que no se pudo comprobar que el folio no esté en Wasabil
    Y la declaración queda con una marca del sistema que dice que el folio no se comprobó contra Wasabil

  Escenario: Con la verificación apagada, la pantalla permite registrar sin consultar Wasabil
    Dado que la verificación de folios está apagada por emergencia
    Y que Wasabil no responde
    Cuando el usuario comprueba la boleta N° 42 en el modal
    Entonces el sistema indica que el folio se registrará sin verificar
    Y permite registrar la boleta

  Escenario: Una boleta y una factura pueden tener el mismo número
    Dado que ya está registrada la boleta N° 35 de otra venta
    Y que en Wasabil existe la factura N° 35 emitida al RUT "76.543.210-3" por $11.900
    Cuando el usuario registra la factura N° 35 de la venta
    Entonces la factura se registra

  Escenario: El mismo folio del mismo tipo no se registra dos veces
    Dado que ya está registrada la boleta N° 35
    Cuando el usuario intenta registrar otra boleta N° 35
    Entonces el sistema indica que ese folio ya está registrado

  Escenario: La boleta exige folio
    Cuando el usuario intenta registrar una boleta sin folio
    Entonces el sistema pide el folio SII de la boleta
