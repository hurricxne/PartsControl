# language: es
# Endurecimiento previo al pase del Portal de Clientes (2026-10-06): hallazgos MEDIO 1 y 2
# de la revisión de seguridad de la entrega del cliente.
Característica: Nadie bloquea el ingreso de otro ni altera qué ve cada cliente del portal
  Como responsable del sistema de las dos marcas
  Quiero que el límite de intentos frene a quien adivina claves sin dejar fuera al dueño de la cuenta
  Y que solo Grupo AM maneje las fichas de cliente que deciden qué ve cada cuenta del portal

  Escenario: Un tercero que conoce mi correo no me bloquea desde otra conexión
    Dado que alguien prueba 5 claves malas con mi correo desde su propia IP
    Cuando yo inicio sesión con mi clave correcta desde la IP de mi oficina
    Entonces entro normalmente

  Escenario: Quien se equivoca 5 veces desde una IP queda en espera en esa IP
    Dado que desde una IP se prueban 5 claves malas para una cuenta
    Cuando desde esa misma IP se intenta una vez más
    Entonces el sistema responde que hay demasiados intentos y cuánto falta

  Escenario: Adivinar la clave repartiendo intentos entre muchas IPs también tiene tope
    Dado que una cuenta acumula 50 claves malas en 15 minutos desde distintas IPs
    Cuando se intenta otra vez desde una IP nueva
    Entonces el sistema responde que hay demasiados intentos

  Escenario: Entrar bien limpia los errores de mi conexión pero no el tope de la cuenta
    Dado que me equivoqué 4 veces desde mi IP y después entré con la clave correcta
    Entonces puedo volver a equivocarme hasta 5 veces desde mi IP
    Y esos 4 errores siguen contando para el tope de la cuenta

  Escenario: Un usuario de MonzaParts no puede tocar las fichas de cliente de Grupo AM
    Dado un usuario de MonzaParts con sesión iniciada
    Cuando intenta listar, crear, editar o borrar fichas en /api/clientes
    Entonces el sistema responde 403
    Y la ficha no cambia

  Escenario: Grupo AM y soporte siguen manejando sus fichas
    Dado un usuario de Grupo AM o una cuenta de soporte @bigcode.cl
    Cuando lista las fichas de cliente
    Entonces las ve como siempre
