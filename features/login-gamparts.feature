# language: es
# Rediseño del login de PartsControl (appmachparts.cl/login) con la identidad GAM Parts, igual que
# repuestos.gamparts.cl. Decisiones del cliente (2026-10-19): imagen nueva de bodega minera (StockCake),
# se mantiene el acceso a las 2 empresas y la pestaña Monzaparts solo cambia los textos.
# La lógica de ingreso no cambia: solo el diseño.
Característica: El login del sistema tiene el diseño GAM Parts y sigue dando acceso a Grupo AM y Monzaparts
  Como persona de Grupo AM o de Monzaparts
  Quiero entrar al sistema desde una pantalla con la misma identidad que gamparts.cl y repuestos.gamparts.cl
  Para reconocer el portal y elegir mi empresa como siempre

  Escenario: En escritorio se ve el diseño GAM Parts
    Dado que abro appmachparts.cl/login en un computador
    Entonces veo a la izquierda la foto de la bodega oscurecida, el logo GAM Parts by Grupo AM, el titular y 3 recuadros con borde dorado
    Y veo la franja dorada inclinada
    Y a la derecha veo un panel negro con el selector de empresa, el formulario y el botón dorado «Ingresar al sistema»
    Y las letras son Poppins y las esquinas rectas

  Escenario: Grupo AM viene marcada y su usuario entra a su panel
    Dado que abro el login
    Entonces la pestaña «Grupo AM» está marcada
    Cuando ingreso con un usuario de Grupo AM
    Entonces entro al panel principal del sistema

  Escenario: La pestaña Monzaparts cambia los textos y su usuario entra a Monzaparts
    Dado que abro el login
    Cuando elijo la pestaña «Monzaparts»
    Entonces cambian el titular, el texto, los 3 recuadros y el subtítulo a los de Monzaparts
    Y el diseño sigue siendo el de GAM Parts (negro y dorado)
    Cuando ingreso con un usuario de Monzaparts
    Entonces entro a /monzaparts/leads

  Escenario: Un usuario en la pestaña de la otra empresa recibe el aviso de siempre
    Dado que tengo una cuenta de Monzaparts
    Cuando ingreso con la pestaña «Grupo AM» marcada
    Entonces veo «Tu cuenta pertenece a Monzaparts» y no entro

  Escenario: Una cuenta del equipo BigCode entra al sistema de la pestaña elegida
    Dado que tengo una cuenta @bigcode.cl
    Cuando ingreso con la pestaña «Monzaparts» marcada
    Entonces entro a /monzaparts/leads

  Escenario: Clave errónea o campos vacíos muestran un aviso claro
    Cuando intento ingresar sin completar los campos
    Entonces veo «Completa todos los campos»
    Cuando ingreso con una clave errónea
    Entonces veo el error que devuelve el sistema y sigo en el login

  Escenario: Puedo mostrar y ocultar la clave
    Dado que escribí mi clave
    Cuando pulso el botón del ojo
    Entonces la clave se ve
    Cuando lo pulso otra vez
    Entonces la clave vuelve a ocultarse

  Escenario: En celular el formulario queda primero y los campos no hacen zoom
    Dado que abro el login en un celular
    Entonces no veo el panel de la foto sino el logo GAM Parts sobre el formulario
    Y los campos tienen letra de 16 px para que el iPhone no haga zoom
    Y no hay desplazamiento horizontal

  Escenario: Después de entrar, el sistema por dentro no cambia
    Dado que ingresé con un usuario de Grupo AM
    Entonces el sistema se ve con los mismos colores y la misma tipografía de antes del rediseño
