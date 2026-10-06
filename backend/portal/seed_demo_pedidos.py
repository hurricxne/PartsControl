"""Pedidos de DEMOSTRACIÓN para las dos cuentas del piloto, con fotos de prueba.

SOLO PARA UN AMBIENTE DE PRUEBAS (la base local o QA). No correr en producción: escribe
ventas, despachos y facturas ficticias directamente en las tablas del ERP, y aunque van
marcadas como demo, aparecen en las pantallas internas (Ventas, Despachos, Contabilidad).

Uso (desde backend/, con el venv activo; antes hay que haber corrido portal.seed_piloto):
    python -m portal.seed_demo_pedidos             # crea los pedidos demo (borra los anteriores)
    python -m portal.seed_demo_pedidos --limpiar   # los borra, con sus fotos y archivos

PARA QUÉ
    Para ver el portal funcionando sin recorrer a mano toda la cadena (cotizar, cerrar,
    comprar, embarcar, recibir, despachar, facturar, fotografiar). Deja a cada cuenta del
    piloto con pedidos en todos los estados que un cliente puede ver:

    MINERA PILOTO NORTE (DEMO)
      · una orden abierta con seis ítems: despachado y facturado (con fotos y guía
        firmada), en tránsito, en internación aduanera, despachado en parte, en proceso
        de compra y atrasado, y en gestión de reposición;
      · una orden ya entregada completa.
    MINERA PILOTO SUR (DEMO)
      · una orden recién confirmada, con un ítem listo en origen y otro en recepción.

QUÉ NO HACE
    No emite nada en el SII ni llama a Wasabil: las guías y facturas demo son filas
    locales con folio «DEMO-…», así que no ofrecen PDF electrónico (sí la guía firmada,
    que es una imagen generada acá). No prueba la cadena real: para eso se cotiza y se
    cierra una venta normal con el RUT de la ficha demo (ver portal/seed_piloto.py).

CÓMO SE RECONOCE Y SE BORRA
    Todas las cotizaciones demo se llaman «DEMOPORTAL-…». `--limpiar` borra por esa marca
    todo lo que cuelga de ellas (portal/tests/_datos.py::limpiar, la misma limpieza que
    usan las suites). Las fichas y las cuentas del piloto no se tocan.
"""
import io
import sys
from datetime import date, datetime, timedelta

from PIL import Image, ImageDraw

from database import SessionLocal
from models.models import Cliente, OcCliente
from .models import FotoArticulo
from .seed_piloto import CUENTAS, MARCA_DEMO
from .tests import _datos as fabricas
from .tiempo import hoy_chile

MARCA = "DEMOPORTAL"  # prefijo del número de cotización; sin guiones bajos (comodín de LIKE)


def _imagen_de_prueba(titulo: str, detalle: str, tono: tuple) -> bytes:
    """Una imagen que se reconoce como de prueba: fondo de color, una "pieza" dibujada y
    un rótulo. No pretende parecer una foto real."""
    imagen = Image.new("RGB", (1600, 1200), tono)
    lapiz = ImageDraw.Draw(imagen)
    claro = tuple(min(c + 70, 255) for c in tono)
    oscuro = tuple(max(c - 60, 0) for c in tono)
    lapiz.rounded_rectangle((300, 260, 1300, 860), radius=60, fill=claro, outline=oscuro, width=12)
    lapiz.ellipse((640, 400, 960, 720), fill=tono, outline=oscuro, width=12)
    lapiz.rectangle((0, 1000, 1600, 1200), fill=(20, 24, 33))
    lapiz.text((60, 1030), f"FOTO DE PRUEBA · {titulo}", fill=(255, 255, 255), font_size=64)
    lapiz.text((60, 1120), detalle, fill=(200, 205, 215), font_size=40)
    salida = io.BytesIO()
    imagen.save(salida, format="JPEG", quality=85)
    return salida.getvalue()


def _guia_firmada_de_prueba(n: int, folio: str) -> str:
    imagen = Image.new("RGB", (1000, 1400), (250, 250, 245))
    lapiz = ImageDraw.Draw(imagen)
    lapiz.text((80, 80), f"GUÍA DE DESPACHO {folio}", fill=(20, 24, 33), font_size=56)
    lapiz.text((80, 180), "DOCUMENTO DE PRUEBA — sin validez tributaria", fill=(160, 40, 40), font_size=34)
    lapiz.line((80, 1200, 520, 1200), fill=(20, 24, 33), width=4)
    lapiz.text((80, 1220), "Recibido conforme (firma de prueba)", fill=(20, 24, 33), font_size=30)
    salida = io.BytesIO()
    imagen.save(salida, format="JPEG", quality=85)
    return fabricas.archivo_de_guia_firmada(n, "jpg", salida.getvalue())


def _foto(db, item, detalle: str, tono: tuple) -> FotoArticulo:
    return fabricas.foto(db, item, _imagen_de_prueba(item.numero_parte, detalle, tono))


def _ficha_demo(db, cuerpo: str) -> Cliente:
    rut = fabricas.rut_de_prueba(int(cuerpo))
    ficha = db.query(Cliente).filter(Cliente.rut == rut, Cliente.giro == MARCA_DEMO).first()
    if ficha is None:
        raise SystemExit(f"[demo] No existe la ficha demo de RUT {rut}. Corre primero: python -m portal.seed_piloto")
    return ficha


def _fechar(db, oc: OcCliente, cuando: datetime) -> None:
    oc.created_at = cuando
    oc.cotizacion.created_at = cuando
    db.flush()


def _norte(db, ficha: Cliente, hoy: date) -> None:
    ahora = datetime.utcnow()
    # Orden abierta, cerrada hace tres semanas.
    _, oc, it = fabricas.venta(db, MARCA, "N1", ficha, [
        dict(numero_parte="1R-0716", descripcion="FILTRO DE ACEITE MOTOR", marca="CAT", cantidad=12,
             precio=18900, estado="despachado", promesa=hoy - timedelta(days=6)),
        dict(numero_parte="7T-1997", descripcion="SELLO HIDRÁULICO CILINDRO LEVANTE", marca="CAT", cantidad=4,
             precio=86420, estado="embarcado", promesa=hoy + timedelta(days=9)),
        dict(numero_parte="9X-8888", descripcion="BOMBA DE AGUA", marca="CAT", cantidad=1,
             precio=1240000, estado="embarcado", promesa=hoy + timedelta(days=5)),
        dict(numero_parte="5P-0001", descripcion="PERNO CABEZA HEXAGONAL 3/4", marca="CAT", cantidad=40,
             precio=1250, estado="en_bodega", promesa=hoy + timedelta(days=2)),
        dict(numero_parte="6V-4965", descripcion="KIT DE REPARACIÓN INYECTOR", marca="CAT", cantidad=2,
             precio=312500, estado="comprado", promesa=hoy - timedelta(days=2)),
        dict(numero_parte="2P-8090", descripcion="EMPAQUETADURA CULATA", marca="CAT", cantidad=6,
             precio=45800, estado="reclamo_proveedor", promesa=hoy + timedelta(days=12)),
    ], numero_oc="4500-118204", fecha_oc=(hoy - timedelta(days=22)).strftime("%d/%m/%Y"))
    filtro, sello, bomba, perno, _kit, _empaquetadura = it
    _fechar(db, oc, ahora - timedelta(days=21))
    fabricas.embarque(db, MARCA, "N1-transito", [sello], "en_transito")
    fabricas.embarque(db, MARCA, "N1-aduana", [bomba], "en_aduana")
    llegado = fabricas.embarque(db, MARCA, "N1-llegado", [filtro, perno], "en_bodega")
    fabricas.recepcion(db, llegado, {filtro.id: (12, "completo"), perno.id: (40, "completo")})
    fabricas.despacho(
        db, MARCA, "N1-1284", oc, [(filtro, 12), (perno, 25)], numero_guia="DEMO-1284",
        fecha=ahora - timedelta(days=4), fecha_guia=hoy - timedelta(days=4), firmada=True,
        fecha_firma=hoy - timedelta(days=3), archivo_firmado=_guia_firmada_de_prueba(1284, "DEMO-1284"))
    fabricas.factura(db, MARCA, "N1", oc, [(filtro, 12, 18900)], folio="DEMO-9001", fecha=hoy - timedelta(days=3))
    _foto(db, filtro, "Caja cerrada, 12 unidades", (176, 92, 28))
    _foto(db, filtro, "Etiqueta del fabricante", (120, 96, 60))
    _foto(db, perno, "Bolsa de 25 unidades despachadas", (70, 90, 120))

    # Orden entregada completa, de hace dos meses.
    _, oc2, it2 = fabricas.venta(db, MARCA, "N2", ficha, [
        dict(numero_parte="1R-0750", descripcion="FILTRO DE COMBUSTIBLE", marca="CAT", cantidad=24,
             precio=21400, estado="despachado", promesa=hoy - timedelta(days=45)),
        dict(numero_parte="326-1644", descripcion="FILTRO SEPARADOR DE AGUA", marca="CAT", cantidad=8,
             precio=38900, estado="despachado", promesa=hoy - timedelta(days=45)),
    ], numero_oc="4500-117390", fecha_oc=(hoy - timedelta(days=62)).strftime("%d/%m/%Y"))
    _fechar(db, oc2, ahora - timedelta(days=60))
    fabricas.despacho(
        db, MARCA, "N2-1250", oc2, [(it2[0], 24), (it2[1], 8)], numero_guia="DEMO-1250",
        fecha=ahora - timedelta(days=47), fecha_guia=hoy - timedelta(days=47), firmada=True,
        fecha_firma=hoy - timedelta(days=46), archivo_firmado=_guia_firmada_de_prueba(1250, "DEMO-1250"))
    fabricas.factura(db, MARCA, "N2", oc2, [(it2[0], 24, 21400), (it2[1], 8, 38900)], folio="DEMO-8950",
                     fecha=hoy - timedelta(days=46))
    _foto(db, it2[0], "Pallet completo antes del despacho", (60, 110, 80))


def _sur(db, ficha: Cliente, hoy: date) -> None:
    _, oc, it = fabricas.venta(db, MARCA, "S1", ficha, [
        dict(numero_parte="20Y-60-21311", descripcion="FILTRO HIDRÁULICO", marca="KOMATSU", cantidad=6,
             precio=64300, estado="cerrado", promesa=hoy + timedelta(days=25)),
        dict(numero_parte="600-185-4100", descripcion="FILTRO DE AIRE PRIMARIO", marca="KOMATSU", cantidad=3,
             precio=128700, estado="preparado", promesa=hoy + timedelta(days=18)),
        dict(numero_parte="6754-71-6140", descripcion="ELEMENTO FILTRO COMBUSTIBLE", marca="KOMATSU", cantidad=10,
             precio=27650, estado="embarcado", promesa=hoy + timedelta(days=3)),
    ], numero_oc="OC-2026-0457", fecha_oc=(hoy - timedelta(days=8)).strftime("%d/%m/%Y"))
    _fechar(db, oc, datetime.utcnow() - timedelta(days=7))
    fabricas.embarque(db, MARCA, "S1-llegado", [it[2]], "en_bodega")  # llegó; Bodega lo está recibiendo


def sembrar() -> int:
    fabricas.limpiar(MARCA)
    db = SessionLocal()
    try:
        hoy = hoy_chile()
        norte, sur = (_ficha_demo(db, cuerpo) for cuerpo, *_ in CUENTAS)
        _norte(db, norte, hoy)
        _sur(db, sur, hoy)
        db.commit()
    finally:
        db.close()
    print("[demo] Pedidos demo creados: 2 órdenes para Minera Piloto Norte y 1 para Minera Piloto Sur.")
    print("[demo] Para borrarlos:  python -m portal.seed_demo_pedidos --limpiar")
    return 0


def main(argv: list) -> int:
    if "--limpiar" in argv:
        fabricas.limpiar(MARCA)
        print("[demo] Pedidos demo borrados (con sus fotos y archivos).")
        return 0
    return sembrar()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
