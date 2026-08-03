"""Helper para crear notificaciones in-app de MonzaParts.

Endurecido a la vara de `notificaciones.py` (Grupo AM, líneas 5-45): destinatario por
ROL, `severidad`, campo `regla` e IDEMPOTENCIA de 24 h.

POR QUÉ LA IDEMPOTENCIA NO ES OPCIONAL
El barrido diario de las 06:00 (`scheduler.py`) vuelve a evaluar LAS MISMAS condiciones
todos los días: una OC de proveedor atrasada 30 días generaría 30 avisos idénticos. El
resultado no es "más información", es que el dueño deja de mirar la campana — y eso es
peor que no tener alertas. La ventana de 24 h corta la repetición sin apagar el aviso:
si el problema sigue y nadie lo leyó, mañana vuelve a sonar una sola vez.

POR QUÉ LA VENTANA MIRA `regla` **O** `titulo`
Los avisos instantáneos que ya existen (Bodega, Despachos, Logística, Cotizaciones) se
crean SIN `regla`: su único ancla anti-duplicado es el TÍTULO (ver
`monza_router_bodega.py:448`). El barrido diario re-evalúa esas mismas condiciones, así
que si la ventana mirara solo `regla` el aviso instantáneo de Bodega y el del scheduler
convivirían como DOS filas del mismo evento en el panel. Mirando `regla` O `titulo` sobre
la misma entidad, ambos caminos comparten el candado.

UNA FILA POR EVENTO (diferencia deliberada con Grupo AM)
El panel de Grupo AM filtra por rol, así que allá se crea una fila por cada rol
interesado. El de MonzaParts NO filtra (`monza_router_notificaciones.py:30` devuelve
todo), así que dos filas por evento se verían DOS VECES en la campana. En Monza cada
regla notifica a UN solo rol: el que tiene que actuar.
"""
from datetime import datetime, timedelta

# El `tipo` es el badge del frontend (info/success/warning/danger); la `severidad` es la
# urgencia de negocio de Grupo AM (info/warning/critical). Cuando el llamador no declara
# severidad se deriva del tipo, para que los 8 llamadores antiguos también queden con el
# campo poblado sin tener que tocarlos.
_SEVERIDAD_POR_TIPO = {
    "info": "info",
    "success": "info",
    "warning": "warning",
    "danger": "critical",
}

# Ventana de la idempotencia. Constante con nombre: es una regla de negocio (un aviso por
# día como máximo), no un número mágico.
HORAS_IDEMPOTENCIA = 24


def crear_notif(db, titulo, mensaje=None, tipo="info", link=None, entidad=None, entidad_id=None,
                *, rol=None, severidad=None, regla=None):
    """Crea una notificacion. No rompe el flujo si falla.

    Los 7 primeros parámetros conservan su ORDEN POSICIONAL histórico: los llamadores que
    ya existen (`crear_notif(db, titulo, mensaje, tipo, link, entidad, entidad_id)`) no
    cambian. Lo nuevo va como keyword-only para que no se pueda pasar por posición y
    quedar en el campo equivocado.

    Devuelve la notificación creada, o None si la suprimió la idempotencia o si falló.
    """
    try:
        from monza_models import MonzaNotificacion

        # ── Idempotencia de 24 h ────────────────────────────────────────────────────
        # Requiere `regla` + entidad ancla. Los dos requisitos tienen razón de ser:
        #  · sin entidad no hay forma de saber si "otro aviso igual" habla del mismo
        #    hecho o de otro;
        #  · sin `regla` es un aviso instantáneo de los que ya existen (venta cerrada,
        #    despacho confirmado, embarque en tránsito): lo dispara una ACCIÓN del
        #    usuario, ocurre una sola vez por definición, y este endurecimiento no debe
        #    cambiarle el comportamiento.
        if regla and entidad and entidad_id:
            desde = datetime.utcnow() - timedelta(hours=HORAS_IDEMPOTENCIA)
            # regla O titulo: ver la cabecera del módulo (los avisos instantáneos del
            # programador no traen `regla` y su único ancla es el título).
            ancla = (MonzaNotificacion.regla == regla) | (MonzaNotificacion.titulo == titulo)
            ya_existe = db.query(MonzaNotificacion).filter(
                MonzaNotificacion.entidad == entidad,
                MonzaNotificacion.entidad_id == entidad_id,
                # `leida == 0`: la columna es Integer (0/1), no Boolean. Si el dueño ya
                # leyó el aviso queda RECONOCIDO y mañana el barrido puede volver a
                # avisar — es el mismo criterio de Grupo AM (notificaciones.py:26).
                MonzaNotificacion.leida == 0,
                MonzaNotificacion.fecha >= desde,
                ancla,
            ).first()
            if ya_existe:
                return None

        n = MonzaNotificacion(
            titulo=titulo, mensaje=mensaje, tipo=tipo,
            link=link, entidad=entidad, entidad_id=entidad_id,
            destinatario_rol=rol,
            severidad=severidad or _SEVERIDAD_POR_TIPO.get(tipo, "info"),
            regla=regla,
        )
        db.add(n)
        db.commit()
        db.refresh(n)
        return n
    except Exception as e:
        db.rollback()
        # No se re-lanza (el contrato histórico es "no rompe el flujo del que llama"),
        # pero TAMPOCO se calla: un fallo silencioso acá deja al barrido diario sin
        # avisos y sin rastro de por qué. El log del servidor guarda el motivo.
        print(f"[monza_notif] no se pudo crear la notificación {titulo!r}: "
              f"{type(e).__name__}: {e}", flush=True)
        return None
