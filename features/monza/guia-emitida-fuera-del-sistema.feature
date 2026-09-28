# language: es
Característica: Facturar un despacho cuya guía se emitió fuera del sistema
  Como usuario de MonzaParts que factura sus ventas
  Quiero declarar que revisé Wasabil cuando la guía electrónica del sistema fue rechazada
  Para facturar citando la guía que de verdad acompañó la mercadería, sin pedir soporte

  Antecedentes:
    Dado que la venta "COT-2026-000203" de "HDI Seguros" por $698.168 tiene el despacho "DSP-2026-0034"
    Y el despacho "DSP-2026-0034" está despachado con la guía firmada por el cliente
    Y el despacho tiene registrada la guía N° 86 del 23-09-2026

  Escenario: El sistema pide verificar Wasabil cuando la guía electrónica fue rechazada
    Dado que el SII rechazó sin folio la guía electrónica que el sistema intentó para "DSP-2026-0034"
    Cuando el usuario prepara la factura electrónica de "DSP-2026-0034"
    Entonces el sistema pide confirmar que en Wasabil no hay ninguna guía emitida con la referencia "DSP-2026-0034"
    Y indica que la factura citará la guía N° 86 del 23-09-2026
    Y la factura no se puede emitir mientras no se confirme

  Escenario: Con la referencia confirmada, la factura cita la guía emitida fuera del sistema
    Dado que el SII rechazó sin folio la guía electrónica que el sistema intentó para "DSP-2026-0034"
    Cuando el usuario confirma la referencia "DSP-2026-0034" y emite la factura
    Entonces la factura sale al SII citando la guía N° 86 del 23-09-2026
    Y la guía rechazada queda con el registro de quién confirmó, cuándo y con qué referencia

  Escenario: Una referencia que no coincide no autoriza la factura
    Dado que el SII rechazó sin folio la guía electrónica que el sistema intentó para "DSP-2026-0034"
    Cuando el usuario escribe la referencia "DSP-2026-0043" e intenta emitir la factura
    Entonces el sistema indica que la referencia no coincide con la del despacho
    Y no se emite ninguna factura

  Escenario: Una guía emitida en Wasabil con la misma referencia bloquea la factura sin excepción
    Dado que el SII rechazó sin folio la guía electrónica que el sistema intentó para "DSP-2026-0034"
    Y Wasabil informa una guía emitida con folio 91 y la referencia "DSP-2026-0034"
    Cuando el usuario confirma la referencia "DSP-2026-0034" e intenta emitir la factura
    Entonces el sistema bloquea la factura indicando que el folio real de la guía es el 91
    Y no se emite ninguna factura

  Escenario: Un despacho con guía en papel se factura sin verificación adicional
    Dado que el despacho "DSP-2026-0034" nunca tuvo guía electrónica
    Cuando el usuario prepara la factura electrónica de "DSP-2026-0034"
    Entonces el sistema no pide ninguna confirmación de Wasabil
    Y la factura citará la guía N° 86 del 23-09-2026

  Escenario: Una guía electrónica emitida con folio se cita sin verificación adicional
    Dado que el SII aceptó la guía electrónica de "DSP-2026-0034" con el folio 90 del 22-09-2026
    Cuando el usuario prepara la factura electrónica de "DSP-2026-0034"
    Entonces el sistema no pide ninguna confirmación de Wasabil
    Y la factura citará la guía N° 90 del 22-09-2026

  Escenario: Una guía electrónica en proceso sigue bloqueando aunque se confirme la referencia
    Dado que la guía electrónica de "DSP-2026-0034" está en proceso en el SII
    Cuando el usuario confirma la referencia "DSP-2026-0034" e intenta emitir la factura
    Entonces el sistema bloquea la factura hasta que la guía quede resuelta
    Y no se emite ninguna factura

  Escenario: El aviso del selector de guías no cambia
    Dado que el SII rechazó sin folio la guía electrónica que el sistema intentó para "DSP-2026-0034"
    Cuando el usuario revisa las guías disponibles para facturar la venta "COT-2026-000203"
    Entonces "DSP-2026-0034" aparece con el mismo aviso que antes de este cambio

  Escenario: La factura manual de Contabilidad no ofrece la confirmación de Wasabil
    Dado que el SII rechazó sin folio la guía electrónica que el sistema intentó para "DSP-2026-0034"
    Cuando el usuario registra a mano una factura ya emitida para "DSP-2026-0034"
    Entonces el sistema no pide ninguna confirmación de Wasabil
    Y la factura manual se registra igual que antes de este cambio

  Escenario: Un usuario de otra empresa no puede facturar despachos de MonzaParts
    Dado que Rodrigo Pinto ha iniciado sesión como usuario de "Grupo AM"
    Cuando él intenta confirmar la referencia "DSP-2026-0034" y emitir la factura
    Entonces el sistema rechaza la acción por falta de permisos
    Y no se emite ninguna factura

  Escenario: Sin sesión no se puede facturar
    Dado que nadie ha iniciado sesión
    Cuando se intenta confirmar la referencia "DSP-2026-0034" y emitir la factura
    Entonces el sistema exige iniciar sesión
    Y no se emite ninguna factura
