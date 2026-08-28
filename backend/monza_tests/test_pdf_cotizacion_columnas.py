"""El PDF de la cotización muestra Calidad y Plazo entrega (arreglo 6 del equipo, 2026-08-21).

EL PROBLEMA QUE CIERRA
    La tabla del PDF tenía 6 columnas (Repuesto/Marca/Procedencia/Cantidad/Precio/TOTAL):
    ni la calidad del repuesto ni los días de tránsito — que el equipo promete al cliente —
    salían en el documento. Además la página 2+ salía SIN cabecera (faltaba repeatRows=1)
    y el "corrimiento de índices" ingenuo de los totales partía el monto del TOTAL en dos
    líneas: por eso la tabla de totales ahora lleva colWidths PROPIOS.

CÓMO SE TESTEA
    _generar_pdf es una función PURA (solo lee atributos de cot/cfg): se ejercita con
    stubs SimpleNamespace — sin BD, sin MARK, sin limpieza — y el texto se lee con
    pdfplumber (dependencia ya declarada del backend). NO se usa pageCompression=0 +
    grep de bytes: es estado global del proceso pytest y los acentos van en cp1252
    escapado, dos trampas medidas.

SONDAS DE PODER DISCRIMINANTE
    · "Calidad"/"Plazo" en el texto: RED contra el PDF de 6 columnas.
    · "Genuine" presente Y "genuine" crudo ausente: prueba que el MAPEO corrió (la BD
      guarda minúsculas), no solo que la columna existe.
    · Cabecera repetida en la página 2 con 45 ítems: RED sin repeatRows=1.
    · "$1.469.135" en una sola línea: RED si los totales comparten col_ws de 8 columnas.

Corre con:  ./venv/bin/python -m pytest monza_tests/test_pdf_cotizacion_columnas.py -q
"""
import io
import os
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from monza_router_cotizaciones import _generar_pdf  # noqa: E402

pdfplumber = pytest.importorskip(
    "pdfplumber", reason="pdfplumber no está instalado: el texto del PDF NO se verificó")

_fails = []


def check(name, cond, extra=""):
    print(("OK  " if cond else "FAIL") + " | " + name + ("" if cond else f"  -> {extra}"))
    if not cond:
        _fails.append(name)


def _cfg():
    return SimpleNamespace(
        razon_social="MONZAPARTS TEST SPA", rut_empresa="77.111.222-3",
        direccion="Av. Prueba 123, Santiago", giro="Repuestos automotrices",
        email_empresa="test@monzaparts.invalid",
        banco=None, numero_cuenta=None, tipo_cuenta=None,
        condiciones_default="Precios validos por 5 dias.",
    )


def _item(descripcion, calidad, plazo, precio, cantidad=1):
    return SimpleNamespace(
        descripcion=descripcion, marca="BOSCH", procedencia="Alemania",
        calidad=calidad, cantidad=cantidad,
        precio_unitario_clp=precio, subtotal_clp=precio * cantidad,
        plazo_entrega=plazo,
    )


def _cot(items, total_neto, iva, total_bruto):
    return SimpleNamespace(
        numero="COT-TEST-0001", fecha_creacion=datetime(2026, 8, 21),
        vehiculo="TOYOTA HILUX 2.4", anio="2021", vin="JTFBF5J18FK000001",
        tipo_cotizacion="Importación de Repuestos", estado="propuesta",
        forma_pago="Contado", oc_cliente=None,
        condiciones_servicio="Precios validos por 5 dias.",
        asesor=None,
        cliente=SimpleNamespace(nombre="CLIENTE PDF LTDA", rut="76.999.888-7"),
        items=items,
        total_neto=total_neto, iva_monto=iva, total_bruto=total_bruto,
    )


def _texto(pdf_bytes):
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return [page.extract_text() or "" for page in pdf.pages]


def run():
    # ── 1) PDF chico: columnas, mapeo de calidad y plazo ─────────────────────────────
    items = [
        _item("Pastillas de freno delanteras", "genuine", "15-20 días hábiles", 123456),
        # calidad NULL y plazo NULL: la celda imprime "—", jamás revienta
        _item("Disco de freno", None, None, 654321),
        # 'sin_calificar' también es "—" (no es una calidad, es la ausencia de una)
        _item("Sensor ABS", "sin_calificar", "7 días", 45678),
        # valor libre: pasa capitalizado tal cual
        _item("Alternador", "remanufacturado", "10 días", 445680),
    ]
    pdf = _generar_pdf(_cot(items, 1234567, 234568, 1469135), _cfg())
    check("1a el PDF se genera", isinstance(pdf, bytes) and pdf[:4] == b"%PDF", pdf[:8])
    paginas = _texto(pdf)
    texto = "\n".join(paginas)
    check("1b cabecera 'Calidad' presente (RED contra el PDF de 6 columnas)",
          "Calidad" in texto, texto[:400])
    check("1c cabecera 'Plazo' presente (RED contra el PDF de 6 columnas)",
          "Plazo" in texto, texto[:400])
    check("1d SONDA del mapeo: 'Genuine' presente...", "Genuine" in texto, "")
    check("1e ...y el crudo 'genuine' AUSENTE (si aparece, el mapeo no corrió)",
          "genuine" not in texto, "")
    check("1f OEM abreviado no: 'Cant.' es la cabecera de cantidad", "Cant." in texto, "")
    check("1g el plazo del ítem sale ('15-20')", "15-20" in texto, "")
    check("1h el valor libre sale capitalizado ('Remanufacturado')",
          "Remanufacturado" in texto or "Remanufact" in texto, "")
    check("1i el TOTAL sale entero en una línea (colWidths propios de los totales)",
          "$1.469.135" in texto, [ln for ln in texto.splitlines() if "1.469" in ln])
    check("1j TOTAL NETO en una línea", "TOTAL NETO." in texto,
          [ln for ln in texto.splitlines() if "NETO" in ln])

    # ── 2) PDF largo: la cabecera se REPITE en la página 2 (repeatRows=1) ────────────
    muchos = [_item(f"Repuesto de prueba N° {i:02d}", "genuine", "7-10 días", 10000 + i)
              for i in range(1, 46)]
    pdf2 = _generar_pdf(_cot(muchos, 495000, 94050, 589050), _cfg())
    paginas2 = _texto(pdf2)
    check("2a con 45 ítems hay 2+ páginas", len(paginas2) >= 2, len(paginas2))
    check("2b SONDA repeatRows: 'Calidad' y 'Plazo' TAMBIÉN en la página 2",
          len(paginas2) >= 2 and "Calidad" in paginas2[1] and "Plazo" in paginas2[1],
          (paginas2[1][:300] if len(paginas2) >= 2 else "una sola página"))

    # ── 3) Cotización VIEJA (todo NULL en lo nuevo): el PDF no revienta ──────────────
    viejos = [SimpleNamespace(descripcion="Legado", marca=None, procedencia=None,
                              calidad=None, cantidad=1, precio_unitario_clp=None,
                              subtotal_clp=None, plazo_entrega=None)]
    pdf3 = _generar_pdf(_cot(viejos, 0, 0, 0), _cfg())
    check("3a el PDF de una cotización legado se genera igual",
          isinstance(pdf3, bytes) and pdf3[:4] == b"%PDF", "")

    if _fails:
        raise AssertionError(f"{len(_fails)} checks fallaron: {_fails}")
    print("\n=== TODO OK ===")


def test_pdf_cotizacion_columnas():
    run()


if __name__ == "__main__":
    run()
