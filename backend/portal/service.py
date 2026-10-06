"""Reglas del Portal de Clientes que no dependen de FastAPI (se prueban sin levantar la app).

Tres responsabilidades, y nada más:
  1) VIGENCIA de la cuenta: el corte automático por inactividad (cuenta_vencida).
  2) ALTA de cuentas portal: crear_cuenta / clave temporal.
  3) VÍNCULO cotización ↔ cliente por RUT saneado (sincronizar_cliente, mapa_clientes).
     El gancho que lo mantiene al día en cada escritura vive en portal/sincronia.py.

Los helpers de RUT viven en portal/rut.py.
"""
import logging
import re
import secrets
import string
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth import TIPO_INTERNO, TIPO_PORTAL, get_password_hash
from models.models import Cliente, Cotizacion, User
from .models import PERFILES, PortalAcceso, UsuarioCliente
# También los importan desde acá routers/clientes.py, portal/dtes.py, seed_piloto.py y las suites.
from .rut import rut_canonico, rut_saneado, rut_valido
from . import sincronia  # noqa: F401  registra el gancho que mantiene cliente_id al día

logger = logging.getLogger("portal")

# Corte automático de la cuenta por inactividad (control S11 del plan). Una cuenta de
# cliente que nadie usa es una credencial viva en el correo de alguien que quizá ya no
# trabaja ahí. No se borra ni se desactiva nada: la cuenta queda "vencida" hasta que el
# personal la reactiva desde Cuentas del portal.
DIAS_INACTIVIDAD_CORTE = 60

# La clave temporal viaja por correo o WhatsApp: no puede quedar como credencial
# definitiva. Vale estos días desde que se entrega; después el login la rechaza y hay que
# pedir otra. Mientras la cuenta siga con la temporal, el portal se lo recuerda.
DIAS_VIGENCIA_CLAVE_TEMPORAL = 7

LARGO_CLAVE_TEMPORAL = 14
LARGO_MINIMO_CLAVE = 10
# bcrypt solo mira los primeros 72 bytes; según la versión, más que eso es un error.
BYTES_MAXIMOS_CLAVE = 72
# Sin caracteres que se confunden al dictarlos o copiarlos a mano (0/O, 1/l/I).
_ALFABETO_CLAVE = "".join(
    c for c in string.ascii_letters + string.digits if c not in "0O1lI")

# La misma regla del <input type="email"> del login, más un punto obligatorio en el
# dominio: si el alta acepta algo que el login no deja escribir, la cuenta nace inservible
# con su clave ya entregada. Se aplica con fullmatch y no con ^…$: en Python `$` acepta un
# salto de línea al final.
_CORREO = re.compile(
    r"[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+")


class CuentaError(Exception):
    """Regla de negocio violada al crear/modificar una cuenta. `codigo` es el HTTP que
    el router debe devolver; el mensaje va tal cual al operador."""

    def __init__(self, codigo: int, mensaje: str):
        super().__init__(mensaje)
        self.codigo = codigo
        self.mensaje = mensaje


# ── 1) Vigencia ─────────────────────────────────────────────────────────────────────────

def cuenta_vencida(acceso: Optional[PortalAcceso], ahora: Optional[datetime] = None) -> bool:
    """¿La cuenta superó el plazo de inactividad?

    El plazo corre desde lo MÁS RECIENTE entre el último acceso y `vigente_desde` (la
    creación o la última reactivación): si solo mirara el último acceso, una cuenta
    recién reactivada seguiría vencida para siempre.

    Sin fila de portal_acceso se responde True: una cuenta portal sin su registro de
    vigencia es un estado que este módulo nunca produce, y ante lo desconocido se cierra.
    """
    if acceso is None or acceso.vigente_desde is None:
        return True
    ahora = ahora or datetime.utcnow()
    referencia = acceso.vigente_desde
    if acceso.ultimo_acceso_at and acceso.ultimo_acceso_at > referencia:
        referencia = acceso.ultimo_acceso_at
    return (ahora - referencia) > timedelta(days=DIAS_INACTIVIDAD_CORTE)


def vencimiento_clave_temporal(acceso: Optional[PortalAcceso]) -> Optional[datetime]:
    """Cuándo deja de servir la clave temporal de la cuenta; None si el contacto ya puso
    la suya (o no hay registro). Una sola fuente del plazo: la usan el login, el listado
    del personal y las respuestas que entregan una clave."""
    if acceso is None or acceso.clave_temporal_desde is None:
        return None
    return acceso.clave_temporal_desde + timedelta(days=DIAS_VIGENCIA_CLAVE_TEMPORAL)


def clave_temporal_vencida(acceso: Optional[PortalAcceso], ahora: Optional[datetime] = None) -> bool:
    """¿La cuenta sigue con una clave temporal que ya pasó su plazo de entrega?
    `clave_temporal_desde` en NULL significa que el contacto ya puso su propia clave."""
    vence = vencimiento_clave_temporal(acceso)
    return vence is not None and (ahora or datetime.utcnow()) > vence


def registrar_acceso(db: Session, usuario_id: int, ahora: Optional[datetime] = None) -> None:
    """Anota el uso de la cuenta. Lo llama el login: el token dura 8 horas, así que cada
    día de uso real pasa por acá y no hace falta escribir en cada request."""
    acceso = db.query(PortalAcceso).filter(PortalAcceso.usuario_id == usuario_id).first()
    if acceso is not None:
        acceso.ultimo_acceso_at = ahora or datetime.utcnow()


# ── 2) Cuentas ──────────────────────────────────────────────────────────────────────────

def clave_temporal() -> str:
    """Clave aleatoria para entregar UNA vez al operador. `secrets`, no `random`."""
    return "".join(secrets.choice(_ALFABETO_CLAVE) for _ in range(LARGO_CLAVE_TEMPORAL))


def crear_cuenta(
    db: Session, *, email: str, nombre: str, cliente_ids: List[int], perfil: str,
    creado_por_id: Optional[int], clave: Optional[str] = None,
) -> Tuple[User, Optional[str]]:
    """Crea una cuenta portal con su puente y su registro de vigencia.

    NO hace commit ni rollback. Tras un CuentaError quien llama deshace la transacción:
    si el rechazo vino de la base (409), la sesión no sirve hasta ese rollback.

    Devuelve (usuario, clave_temporal). Si se pasa `clave=None` se genera una temporal
    y se devuelve para mostrarla UNA vez; nunca se guarda en claro.

    Reglas (cada una con su prueba en portal/tests/test_cuentas.py):
      · El correo tiene que poder escribirse en el login (`_CORREO`).
      · El email no puede existir en `users`, sea del tipo que sea. JAMÁS se convierte
        una cuenta existente: si el correo es de alguien del personal, pasarlo a
        'portal' lo dejaría fuera del ERP en el acto.
      · Al menos un cliente, y todos tienen que existir. Una cuenta sin cliente no ve
        nada (require_portal la rechaza), así que crearla solo deja basura.
      · El perfil sale de la lista cerrada PERFILES.
      · Las dos carreras que los chequeos de arriba no cubren (otra alta del mismo correo
        en vuelo; la ficha eliminada entre el chequeo y el INSERT) las decide la base y
        se traducen a 409 donde ocurren: en cada flush, que tiene un único significado.
      · Dar acceso pone al día el vínculo venta ↔ cliente de TODAS las ventas
        (vincular_historico), en la misma transacción.
    """
    email = (email or "").strip()
    nombre = (nombre or "").strip()
    if not _CORREO.fullmatch(email):
        raise CuentaError(
            400, "El correo no es válido: escribe solo la dirección, sin nombre ni signos (ej. contacto@cliente.cl)")
    if not nombre:
        raise CuentaError(400, "El nombre es obligatorio")
    if perfil not in PERFILES:
        raise CuentaError(400, f"Perfil inválido: debe ser uno de {', '.join(PERFILES)}")
    ids = sorted({int(c) for c in (cliente_ids or [])})
    if not ids:
        raise CuentaError(400, "La cuenta necesita al menos un cliente")

    existente = db.query(User).filter(User.email == email).first()
    if existente is not None:
        tipo = getattr(existente, "tipo_acceso", TIPO_INTERNO)
        raise CuentaError(
            409,
            f"El correo {email} ya tiene una cuenta ({'del portal' if tipo == TIPO_PORTAL else 'interna del ERP'}). "
            "No se convierte una cuenta existente: usa otro correo.",
        )
    encontrados = {c.id for c in db.query(Cliente).filter(Cliente.id.in_(ids)).all()}
    faltan = [c for c in ids if c not in encontrados]
    if faltan:
        raise CuentaError(404, f"Cliente(s) inexistente(s): {faltan}")

    temporal = None if clave else clave_temporal()
    usuario = User(
        email=email,
        nombre=nombre,
        hashed_password=get_password_hash(clave or temporal),
        is_active=1,
        empresa="mineria",  # el portal es de Grupo AM; el eje que aísla es tipo_acceso
        tipo_acceso=TIPO_PORTAL,
    )
    db.add(usuario)
    try:
        db.flush()  # INSERT en users: hace falta usuario.id para el puente
    except IntegrityError as exc:
        # Dos altas del mismo correo a la vez: las dos pasan el chequeo de existencia y la
        # segunda choca acá con el UNIQUE de users.email (el único de la tabla). El error
        # real de la base queda en el log, para que el 409 no tape otra cosa.
        logger.warning("portal: alta de %r rechazada por la base: %s", email, exc.orig)
        raise CuentaError(
            409, f"El correo {email} ya tiene una cuenta: alguien la creó recién. Actualiza la lista de cuentas.",
        ) from exc
    for cid in ids:
        db.add(UsuarioCliente(usuario_id=usuario.id, cliente_id=cid, perfil=perfil))
    ahora = datetime.utcnow()
    db.add(PortalAcceso(
        usuario_id=usuario.id, vigente_desde=ahora, creado_por_id=creado_por_id,
        clave_temporal_desde=ahora if temporal else None))
    try:
        db.flush()
    except IntegrityError as exc:
        # La ficha existía al revisarla y ya no: alguien la eliminó mientras tanto (la FK
        # de usuario_cliente). El motivo exacto queda en el log.
        logger.warning("portal: alta de %r rechazada por la base: %s", email, exc.orig)
        raise CuentaError(
            409, "Los clientes elegidos cambiaron mientras se creaba la cuenta (¿se eliminó alguno?). "
                 "Actualiza la lista y vuelve a intentar.",
        ) from exc
    # Una ficha con cuentas ya no puede cambiar de RUT (routers/clientes.py lo rechaza):
    # se pone al día el vínculo de todas las ventas justo antes de que alguien pueda
    # mirarlas. Repara también vínculos viejos de cualquier origen (SQL a mano,
    # escrituras masivas).
    vincular_historico(db, aplicar=True)
    return usuario, temporal


# ── 3) Vínculo cotización ↔ cliente por RUT ─────────────────────────────────────────────

def mapa_clientes(db: Session) -> Tuple[Dict[str, int], Dict[str, List[int]]]:
    """RUT canónico → cliente_id, y aparte los RUT AMBIGUOS (dos fichas con el mismo RUT
    escrito distinto: `clientes.rut` es único como TEXTO, así que '76.874.971-K' y
    '76874971-K' conviven sin que la base se queje).

    Un RUT ambiguo NO entra al mapa: elegir una de las dos fichas al azar podría
    mostrarle las ventas al contacto equivocado. Queda para que una persona unifique.
    Tampoco entra un RUT mal formado: con un dígito verificador malo no hay certeza de
    a quién pertenece.
    """
    por_rut: Dict[str, List[int]] = {}
    for cid, rut in db.query(Cliente.id, Cliente.rut).all():
        if rut_valido(rut) and rut_canonico(rut):
            por_rut.setdefault(rut_canonico(rut), []).append(cid)
    mapa = {r: ids[0] for r, ids in por_rut.items() if len(ids) == 1}
    ambiguos = {r: sorted(ids) for r, ids in por_rut.items() if len(ids) > 1}
    return mapa, ambiguos


def cliente_para_rut(mapa: Dict[str, int], rut: Optional[str]) -> Optional[int]:
    """La ficha que le corresponde a un RUT, o None. Sin RUT, con RUT mal formado, sin
    ficha o con ficha ambigua (no está en el mapa) la respuesta es None."""
    if not rut_valido(rut):
        return None
    canon = rut_canonico(rut)
    return mapa.get(canon) if canon else None


def _rut_de_ficha_con_candado(db: Session, cliente_id: int) -> Optional[str]:
    """RUT canónico que la ficha tiene AHORA, leído con candado compartido sobre esa fila:
    el mismo que toma la FK al escribir cotizaciones.cliente_id, tomado un paso antes
    para poder mirar el RUT. Si otra transacción la está editando o borrando, espera a
    que termine; desde acá y hasta el commit nadie puede cambiarla. None si ya no existe
    o su RUT no es válido."""
    fila = (db.query(Cliente.rut).filter(Cliente.id == cliente_id)
            .with_for_update(read=True).first())
    return rut_canonico(fila[0]) if fila is not None and rut_valido(fila[0]) else None


def cliente_para_rut_confirmado(
    db: Session, mapa: Dict[str, int], rut: Optional[str], confirmadas: Dict[int, Optional[str]],
) -> Optional[int]:
    """La ficha de este RUT, CONFIRMADA.

    El mapa se lee sin candado: entre esa lectura y la escritura otra persona puede
    cambiarle el RUT a la ficha o borrarla, y la venta quedaría colgada de una ficha que
    ya no es la de su RUT (candado.py filtra solo por cliente_id: sus guías, facturas y
    fotos a la vista de otro contribuyente). Por eso toda escritura de
    cotizaciones.cliente_id hacia una ficha pasa por acá; si la ficha ya no calza, la
    venta queda sin vincular. `confirmadas` ({ficha: RUT leído}) vale mientras dure la
    transacción: el candado se suelta recién en el commit.
    """
    destino = cliente_para_rut(mapa, rut)
    if destino is None:
        return None
    if destino not in confirmadas:
        confirmadas[destino] = _rut_de_ficha_con_candado(db, destino)
    return destino if confirmadas[destino] == rut_canonico(rut) else None


def sincronizar_cliente(
    db: Session, cot: Cotizacion, mapa: Optional[Dict[str, int]] = None,
) -> Optional[int]:
    """Deja cot.cliente_id igual a la ficha de su RUT ACTUAL (o NULL). Devuelve el id.

    cotizaciones.cliente_id es un dato DERIVADO del RUT, no un vínculo que se hace una
    vez: si el RUT de una venta se corrige y el vínculo viejo se conservara, el contacto
    del cliente anterior seguiría viendo en su portal una venta que ya no es suya. Por
    eso acá se recalcula siempre, también hacia NULL. Sin ficha la venta queda sin
    vincular: para el portal no existe, que es fallar cerrado sin código extra.

    La ficha se confirma SIEMPRE que haya una, también si la venta ya colgaba de ella
    (cliente_para_rut_confirmado): si no, una venta a la que solo le reescriben el RUT
    conservaría el vínculo viejo con una ficha que cambió de RUT mientras tanto.
    """
    if mapa is None:
        mapa, _ = mapa_clientes(db)
    destino = cliente_para_rut_confirmado(db, mapa, cot.rut_cliente, {})
    if cot.cliente_id != destino:
        cot.cliente_id = destino
    return destino


MOTIVO_SIN_RUT = "sin RUT"
MOTIVO_RUT_INVALIDO = "RUT mal formado"
MOTIVO_AMBIGUO = "RUT con más de una ficha de cliente"
MOTIVO_SIN_FICHA = "sin ficha de cliente"


def _motivo_huerfana(rut: Optional[str], ambiguos: Dict[str, List[int]]) -> str:
    if not rut_saneado(rut):
        return MOTIVO_SIN_RUT
    if not rut_valido(rut):
        return MOTIVO_RUT_INVALIDO
    if rut_canonico(rut) in ambiguos:
        return MOTIVO_AMBIGUO
    return MOTIVO_SIN_FICHA


def vincular_historico(db: Session, aplicar: bool) -> dict:
    """Pone al día cotizaciones.cliente_id en TODAS las cotizaciones + reporte de
    huérfanas. NO hace commit.

    Con `aplicar=False` es un ensayo: cuenta lo que cambiaría y no escribe nada. Repasa
    también las ya vinculadas: una venta cuyo RUT dejó de calzar con su ficha (se editó
    el RUT de la ficha, o la ficha quedó ambigua) se DESVINCULA — ver sincronizar_cliente.
    Al aplicar, la ficha de lo que se va a anclar DE NUEVO se confirma con candado
    (cliente_para_rut_confirmado); en régimen normal cuesta cero consultas y el ensayo
    no toma candados.

    El reporte de huérfanas lista solo VENTAS CERRADAS (con OC del cliente): un borrador
    sin RUT no es un problema, una venta que el cliente no va a ver en su portal sí.
    """
    from models.models import OcCliente  # local: el resto del módulo no la necesita

    mapa, ambiguos = mapa_clientes(db)
    cerradas = {cid for (cid,) in db.query(OcCliente.cotizacion_id).all() if cid}

    vinculadas, desvinculadas, al_dia, huerfanas = 0, 0, 0, []
    confirmadas: Dict[int, Optional[str]] = {}
    # Solo las cinco columnas que se usan, y no la entidad completa (27 columnas, tres de
    # ellas Text) por cada cotización de la base: esta pasada corre también en cada alta.
    for cot in db.query(Cotizacion.id, Cotizacion.rut_cliente, Cotizacion.cliente_id,
                        Cotizacion.numero, Cotizacion.cliente).all():
        destino = cliente_para_rut(mapa, cot.rut_cliente)
        if aplicar and destino is not None and destino != cot.cliente_id:
            destino = cliente_para_rut_confirmado(db, mapa, cot.rut_cliente, confirmadas)
        if destino == cot.cliente_id:
            al_dia += 1 if destino is not None else 0
        elif destino is not None:
            vinculadas += 1
        else:
            desvinculadas += 1
        if aplicar and cot.cliente_id != destino:
            # UPDATE condicionado al RUT que se LEYÓ: protege el RUT de la VENTA. Si entre
            # la lectura y esta línea alguien lo corrigió (y el gancho ya la re-vinculó),
            # el WHERE no calza y no se pisa su vínculo con uno calculado sobre el RUT
            # viejo. A la FICHA la cubre cliente_para_rut_confirmado.
            mismo_rut = (Cotizacion.rut_cliente.is_(None) if cot.rut_cliente is None
                         else Cotizacion.rut_cliente == cot.rut_cliente)
            db.query(Cotizacion).filter(Cotizacion.id == cot.id, mismo_rut).update(
                {"cliente_id": destino}, synchronize_session=False)
        if destino is None and cot.id in cerradas:
            huerfanas.append({
                "cotizacion_id": cot.id,
                "numero": cot.numero or "",
                "cliente": cot.cliente or "",
                "rut_cliente": cot.rut_cliente or "",
                "motivo": _motivo_huerfana(cot.rut_cliente, ambiguos),
            })
    if aplicar:
        # Las entidades que quien llama tenga cargadas no se enteran de los UPDATE de arriba.
        db.expire_all()
        if vinculadas or desvinculadas:
            logger.info("portal: vínculo venta-cliente puesto al día: %s vinculadas, %s desvinculadas",
                        vinculadas, desvinculadas)
    return {
        "aplicado": aplicar,
        "ya_vinculadas": al_dia,
        "vinculadas_ahora": vinculadas,
        "desvinculadas_ahora": desvinculadas,
        "huerfanas": huerfanas,
        "ruts_ambiguos": [{"rut": r, "cliente_ids": ids} for r, ids in sorted(ambiguos.items())],
    }
