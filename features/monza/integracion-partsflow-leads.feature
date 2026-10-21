# language: es
# Contrato con el proveedor: documento «Integración PartsFlow → MonzaParts: creación de
# leads» (v1, 2026-10-21). Cada escenario es un caso de prueba de ese documento.
Característica: PartsFlow crea leads en MonzaParts desde WhatsApp
  Como agente de PartsFlow que atiende a un cliente por WhatsApp
  Quiero crear el lead en MonzaParts con un botón
  Para que ventas lo cotice sin volver a pedirle los datos al cliente

  Escenario: Un lead completo se crea con sus repuestos
    Cuando PartsFlow envía una solicitud con contacto, vehículo, dos repuestos y comentario
    Entonces MonzaParts responde 201 con el número del lead
    Y el lead queda con canal «PartsFlow», estado pendiente y sus dos repuestos con su cantidad
    Y la patente, la urgencia y el enlace a la conversación quedan en el comentario

  Escenario: Un lead mínimo se crea
    Cuando PartsFlow envía solo el identificador, el nombre y el teléfono
    Entonces MonzaParts responde 201 con el número del lead

  Escenario: La misma solicitud enviada dos veces no crea dos leads
    Dado que PartsFlow ya envió la solicitud "pf-0001"
    Cuando vuelve a enviar "pf-0001"
    Entonces MonzaParts responde 200 con el mismo número de lead y marca que es duplicado

  Escenario: Dos envíos simultáneos de la misma solicitud crean un solo lead
    Cuando PartsFlow envía "pf-0002" dos veces al mismo tiempo
    Entonces existe un solo lead para "pf-0002"

  Escenario: Un cliente que ya existe no se duplica
    Dado que en MonzaParts existe la ficha del teléfono "+56912345678"
    Cuando PartsFlow envía un lead con ese teléfono escrito en formato E.164
    Entonces el lead queda en esa ficha y la respuesta dice que el cliente ya existía

  Escenario: Un dato mal formado se rechaza indicando el campo
    Cuando PartsFlow envía el teléfono "912345678" sin código de país
    Entonces MonzaParts responde 422 indicando el campo "contacto.telefono"
    Y no se crea ningún lead

  Escenario: Un texto más largo que su máximo se rechaza
    Cuando PartsFlow envía un repuesto con una descripción de 301 caracteres
    Entonces MonzaParts responde 422 indicando el campo "repuestos[0].descripcion"

  Escenario: Sin la clave correcta no se acepta nada
    Cuando PartsFlow envía una solicitud con una clave incorrecta, sin clave o con la clave en la URL
    Entonces MonzaParts responde 401 y no guarda nada

  Escenario: Demasiadas solicitudes por minuto se frenan
    Cuando PartsFlow supera las 60 solicitudes en un minuto
    Entonces MonzaParts responde 429 e indica cuántos segundos esperar
