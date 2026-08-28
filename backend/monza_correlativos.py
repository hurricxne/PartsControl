"""Correlativo del lead (L-AAAA-####), a prueba de creaciones simultáneas.

EL PROBLEMA QUE CIERRA
    `_gen_numero_lead` existía DUPLICADO —una copia en monza_router_leads y otra en
    monza_router_integraciones— y las dos leían el último número y le sumaban 1 sin lock
    ni reintento. Dos vendedores apretando «Nuevo lead» a la vez, o un vendedor justo
    cuando entra un lead por el webhook de Nexor (que es autónomo y usa el otro camino),
    calculaban el MISMO 'L-2026-000N'. Como `numero` es UNIQUE, la segunda inserción
    reventaba con IntegrityError sin capturar: el vendedor recibía un 500 y el lead que
    estaba tipeando se perdía.

    Que fueran dos copias es parte del problema: la carrera es JUSTO entre los dos
    caminos, así que arreglar una sola no habría servido de nada.

DÓNDE VIVE EL REINTENTO
    En `reintentar_carrera`, que envuelve la operación COMPLETA del endpoint, y no acá
    adentro. Una versión anterior lo intentó con un SAVEPOINT y no funcionaba para el
    caso más frecuente: dos leads del MISMO cliente no chocan en el UNIQUE sino en un
    DEADLOCK, que deshace la transacción entera y deja el savepoint sin existir (el
    rollback moría con «1305: SAVEPOINT does not exist» y enmascaraba el deadlock detrás
    de un 500 irrecuperable — sobrevivían 2 de 6). Una transacción muerta solo se puede
    recomponer un nivel más arriba.
"""
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from monza_fechas import hoy_chile
from monza_models import MonzaLead

def _prefijo_anio() -> str:
    """`L-AAAA-` con el año EN CURSO DE CHILE.

    El año es el de Chile y no `utcnow().year`: entre las 21:00 y la medianoche del
    31 de diciembre, UTC ya está en el año siguiente y el correlativo saltaba de golpe
    (mismo criterio que el resto de las fechas de negocio del módulo).
    """
    return f"L-{hoy_chile().year}-"


def _ultima_secuencia(db: Session, prefijo: str) -> int:
    """Mayor secuencia ya guardada para ese año (delega en `siguiente_secuencia`)."""
    return siguiente_secuencia(db, MonzaLead.numero, prefijo) - 1


def gen_numero_lead(db: Session) -> str:
    """Siguiente correlativo del año en curso de Chile, según lo YA GUARDADO."""
    prefijo = _prefijo_anio()
    return f"{prefijo}{_ultima_secuencia(db, prefijo) + 1:04d}"


def siguiente_secuencia(db: Session, columna_numero, prefijo: str) -> int:
    """Siguiente número de la serie `prefijo`, para CUALQUIER correlativo del módulo.

    Existe para que los cinco generadores de MonzaParts (leads, cotizaciones, despachos,
    OC de proveedor y embarques) compartan la misma regla, en vez de tener cinco copias
    que se corrigen de a una. Cuando eran copias, el arreglo del año chileno llegó a una
    sola durante meses, y el del MÁXIMO otra vez a una sola.

    LA REGLA: el MÁXIMO de la serie, no el número de la fila con id más alto. «El último
    insertado» asume que el id y el número crecen juntos, y basta UNA fila con id mayor y
    número menor —lo que deja cualquier migración o una carga de datos— para que el
    generador devuelva para siempre un número ya usado, y la creación quede muerta contra
    el índice único.

    Se calcula en Python y no con MAX() de SQL a propósito: el máximo de SQL sobre texto
    es alfabético, y a partir del documento 10.000 diría que «COT-2026-009999» es mayor
    que «COT-2026-010000». La consulta trae una sola columna filtrada por prefijo y la
    resuelve el índice de `numero` sin tocar la tabla.
    """
    mayor = 0
    for (numero,) in db.query(columna_numero).filter(columna_numero.like(f"{prefijo}%")).all():
        try:
            n = int((numero or "").split("-")[-1])
        except (ValueError, IndexError):
            # Un sufijo legado no numérico no debe tumbar la creación: se ignora esa fila
            # y el índice único se encarga de que no se pise nada.
            continue
        mayor = max(mayor, n)
    return mayor + 1


def agregar_lead_con_numero(db: Session, lead: MonzaLead) -> MonzaLead:
    """Inserta el lead con el siguiente correlativo libre. ÚNICA puerta de creación.

    Un solo intento a propósito: quien reintenta es el ENDPOINT (`create_lead`), y tiene
    que ser él.

    POR QUÉ EL REINTENTO NO PUEDE VIVIR ACÁ
        Un choque entre dos leads DEL MISMO CLIENTE —el caso más frecuente: el mismo
        cliente llamando y dos personas registrándolo, o el webhook de Nexor entrando
        sobre una ficha que un vendedor está atendiendo— no llega como violación del
        UNIQUE sino como DEADLOCK (1213): uno toma el lock del índice de `numero` y el
        otro el de la fila del cliente, en orden cruzado. Un deadlock deshace la
        transacción ENTERA, así que desde adentro no queda nada que salvar ni SAVEPOINT
        al que volver (la primera versión de este helper lo intentaba y moría con «1305:
        SAVEPOINT does not exist», enmascarando el deadlock detrás de un 500
        irrecuperable: sobrevivían 2 de 6 creaciones simultáneas). Recomponer una
        transacción muerta solo se puede un nivel más arriba.
    """
    lead.numero = gen_numero_lead(db)
    db.add(lead)
    db.flush()
    return lead


def reintentar_carrera(db: Session, operacion, vueltas: int = 8, que: str = "leads"):
    """Ejecuta `operacion()` reintentándola si otra creación simultánea la estorbó.

    ÚNICA implementación del reintento, y existe por una lección cara: cuando el bucle
    vivía escrito a mano dentro de un solo endpoint, la otra puerta que crea leads —el
    webhook de Nexor, la única que corre sin un operador mirando— se quedó sin ninguna
    protección. Un helper compartido no puede olvidarse en un llamador; un bucle a mano sí.

    QUÉ SE REINTENTA Y QUÉ NO
      · IntegrityError del campo `numero`: dos creaciones calcularon el mismo correlativo.
        Cualquier otra violación (una FK) se propaga: reintentarla escondería un error real.
      · OperationalError 1213 (deadlock) y 1205 (espera de lock agotada): pasa cuando las
        dos creaciones son DEL MISMO CLIENTE y se piden los locks en orden cruzado. El
        deadlock deshace la transacción entera, así que el único nivel donde se puede
        recomponer es éste — de ahí que el reintento envuelva la operación completa.

    ⚠️ EL CANDADO QUE HACE SEGURO ESTE BUCLE
        Reintentar solo es correcto mientras NADA se haya guardado todavía. Si la
        operación ya confirmó y falla DESPUÉS —al escribir un log, al mandar una
        notificación—, rehacerla no repara nada: DUPLICA lo que ya estaba guardado. Una
        sola petición terminaba creando dos leads o dos cotizaciones, devolviendo un solo
        201. Se reprodujo cuatro veces, en puertas distintas, porque cada una tenía su
        propio rastro de código post-commit y el arreglo se hacía de a una.

        Por eso el candado NO depende de que cada llamador ordene bien su cuerpo: este
        helper ESCUCHA los commits de la sesión (evento `after_commit`) mientras la
        operación corre. Si hubo aunque sea uno, el error se propaga tal cual en vez de
        reintentarse. Es una garantía estructural: sirve para las puertas de hoy y para
        la que alguien agregue mañana sin leer esto.
    """
    confirmado = {"si": False}

    def _marcar(_sesion):
        confirmado["si"] = True

    event.listen(db, "after_commit", _marcar)
    try:
        for _ in range(vueltas):
            confirmado["si"] = False
            try:
                return operacion()
            except IntegrityError as e:
                if confirmado["si"]:
                    raise
                db.rollback()
                if "numero" not in str(getattr(e, "orig", e)):
                    raise
            except OperationalError as e:
                if confirmado["si"]:
                    raise
                db.rollback()
                codigo = getattr(getattr(e, "orig", None), "args", [None])[0]
                if codigo not in (1213, 1205):
                    raise
    finally:
        event.remove(db, "after_commit", _marcar)
    raise HTTPException(
        status_code=409,
        detail=f"Otro usuario está creando {que} en este momento. Reintenta en unos segundos.",
    )
