"""Piloto del Portal de Clientes: 2 clientes DEMO con una cuenta portal cada uno.

Uso (desde backend/, con el venv activo):
    python -m portal.seed_piloto             # crea lo que falte (idempotente)
    python -m portal.seed_piloto --limpiar   # borra las cuentas demo y las fichas demo
                                             # SIN ventas (la que tiene ventas se conserva)

QUÉ CREA
    · 2 fichas en `clientes`, marcadas con giro = MARCA_DEMO y "(DEMO)" en el nombre.
    · 2 cuentas `tipo_acceso='portal'`, una por ficha, con los correos de CUENTAS.
    Las cuentas nacen SIN clave utilizable (se les pone una aleatoria que nadie ve): la
    clave se genera desde la pantalla Cuentas del portal → «Restablecer clave», que es
    el mismo camino que se usará con un cliente real. Así no queda ninguna credencial
    escrita en un archivo.

QUÉ NO CREA
    Ventas. Para ver un pedido en el piloto se cotiza y se cierra una venta normal con
    el RUT de la ficha demo: queda vinculada sola (portal/sincronia.py al guardarla y
    el Cierre de Venta al cerrarla), que es justamente la cadena que hay que probar.

GUARDAS (por qué este script no es peligroso de correr)
    · Un correo que ya existe NO se toca. Si es una cuenta del personal, convertirla a
      portal la dejaría fuera del ERP: se avisa y se sigue con la otra.
    · Si el RUT demo ya aparece en una ficha o en una cotización que NO es demo, se
      aborta: el vínculo por RUT le mostraría ventas reales a la cuenta de prueba.
"""
import secrets
import sys

from auth import TIPO_PORTAL
from database import SessionLocal
from models.models import Cliente, Cotizacion, User
from .models import PERFIL_PROCUREMENT, PortalAcceso, UsuarioCliente
from .rut import rut_canonico, rut_con_dv
from .service import CuentaError, crear_cuenta

MARCA_DEMO = "DEMO PORTAL PILOTO"

# (cuerpo del RUT, nombre de la ficha, correo de la cuenta, nombre del contacto)
CUENTAS = (
    ("1111111", "MINERA PILOTO NORTE (DEMO)", "aibaceta@grupoamchile.cl", "Aldo Ibaceta (piloto Norte)"),
    ("2222222", "MINERA PILOTO SUR (DEMO)", "aibaceta@gruporamaq.cl", "Aldo Ibaceta (piloto Sur)"),
)


def _rut_en_uso_real(db, rut: str) -> list:
    """Dónde aparece este RUT fuera de los datos demo (fichas y cotizaciones)."""
    canon = rut_canonico(rut)
    usos = [
        f"ficha de cliente #{cid} ({nombre})"
        for cid, r, nombre, giro in db.query(Cliente.id, Cliente.rut, Cliente.nombre, Cliente.giro).all()
        if rut_canonico(r) == canon and giro != MARCA_DEMO
    ]
    usos += [
        f"cotización #{cot_id} ({numero})"
        for cot_id, r, numero in db.query(Cotizacion.id, Cotizacion.rut_cliente, Cotizacion.numero)
        .filter(Cotizacion.rut_cliente.isnot(None)).all()
        if rut_canonico(r) == canon
    ]
    return usos


def sembrar(db) -> int:
    avisos = 0
    for cuerpo, nombre, email, contacto in CUENTAS:
        rut = rut_con_dv(cuerpo)
        ficha = db.query(Cliente).filter(Cliente.rut == rut, Cliente.giro == MARCA_DEMO).first()
        if ficha is None:
            usos = _rut_en_uso_real(db, rut)
            if usos:
                print(f"[piloto] ABORTADO: el RUT demo {rut} ya está en uso real: {'; '.join(usos[:5])}")
                print("[piloto] Cambia el cuerpo del RUT en CUENTAS y vuelve a correr.")
                db.rollback()
                return 1
            ficha = Cliente(rut=rut, nombre=nombre, giro=MARCA_DEMO, contacto=contacto, email=email)
            db.add(ficha)
            db.flush()
            print(f"[piloto] + ficha demo #{ficha.id}: {nombre} · RUT {rut}")
        else:
            print(f"[piloto]   ficha demo #{ficha.id} ya existe — ok ({nombre})")

        existente = db.query(User).filter(User.email == email).first()
        if existente is not None:
            if getattr(existente, "tipo_acceso", None) == TIPO_PORTAL:
                print(f"[piloto]   cuenta portal {email} ya existe — ok")
            else:
                avisos += 1
                print(f"[piloto] AVISO: {email} ya es una cuenta INTERNA del ERP (usuario #{existente.id}).")
                print("[piloto]        No se toca. Para el piloto usa un alias, p. ej. "
                      f"{email.replace('@', '+portal@')}, creándolo en Cuentas del portal.")
            continue
        try:
            usuario, _ = crear_cuenta(
                db, email=email, nombre=contacto, cliente_ids=[ficha.id],
                perfil=PERFIL_PROCUREMENT, creado_por_id=None,
                clave=secrets.token_urlsafe(32),  # nadie la ve: se restablece desde la pantalla
            )
        except CuentaError as exc:
            avisos += 1
            print(f"[piloto] AVISO: no se pudo crear {email}: {exc.mensaje}")
            continue
        print(f"[piloto] + cuenta portal #{usuario.id}: {email} → {nombre}")
    db.commit()
    print("[piloto] Listo. Para entrar: ERP → Admin → Cuentas del portal → «Restablecer clave».")
    return 1 if avisos else 0


def limpiar(db) -> int:
    fichas = db.query(Cliente).filter(Cliente.giro == MARCA_DEMO).all()
    ficha_ids = [f.id for f in fichas]
    usuario_ids = [
        uid for (uid,) in db.query(UsuarioCliente.usuario_id)
        .filter(UsuarioCliente.cliente_id.in_(ficha_ids or [0])).distinct().all()
    ]
    # Solo cuentas portal: jamás se borra a alguien del personal desde acá.
    usuario_ids = [
        u.id for u in db.query(User)
        .filter(User.id.in_(usuario_ids or [0]), User.tipo_acceso == TIPO_PORTAL).all()
    ]
    db.query(UsuarioCliente).filter(UsuarioCliente.usuario_id.in_(usuario_ids or [0])).delete(synchronize_session=False)
    db.query(PortalAcceso).filter(PortalAcceso.usuario_id.in_(usuario_ids or [0])).delete(synchronize_session=False)
    db.query(User).filter(User.id.in_(usuario_ids or [0])).delete(synchronize_session=False)
    db.commit()
    print(f"[piloto] - {len(usuario_ids)} cuenta(s) portal demo")
    # La ficha con ventas vinculadas se CONSERVA. La base no lo impediría (la FK de
    # cotizaciones es ON DELETE SET NULL): borrarla dejaría esas ventas de prueba sueltas,
    # y además el RUT demo quedaría "en uso" por ellas, con lo que el piloto no se podría
    # volver a sembrar. Conservada, la próxima siembra la reutiliza y solo recrea cuentas.
    conservadas = 0
    for ficha in fichas:
        ventas = db.query(Cotizacion).filter(Cotizacion.cliente_id == ficha.id).count()
        if ventas:
            conservadas += 1
            print(f"[piloto]   la ficha demo #{ficha.id} ({ficha.nombre}) tiene {ventas} venta(s) "
                  "vinculada(s): se conserva.")
            continue
        db.delete(ficha)
        db.commit()
        print(f"[piloto] - ficha demo #{ficha.id} ({ficha.nombre})")
    return 1 if conservadas else 0


def main(argv: list) -> int:
    db = SessionLocal()
    try:
        return limpiar(db) if "--limpiar" in argv else sembrar(db)
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
