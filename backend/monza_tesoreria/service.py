"""Lógica pura del módulo Tesorería MonzaParts (autocontenida, sin FastAPI ni BD).

Incluye el parser FLEXIBLE de cartolas (CSV / Excel .xlsx) del patrón de Grupo AM
(conciliacion_bancaria/service.py): detecta columnas por nombre de encabezado (fecha,
glosa/detalle, cargo/abono o monto, referencia, saldo) y normaliza montos en formato
chileno ("1.234.567" / "1.234.567,89" / "($1.000)").
"""
import csv
import io
import unicodedata
from datetime import datetime, date
from typing import Optional

TOL = 1.0                 # tolerancia en CLP para emparejar montos
DIAS_SUGERENCIA = 7       # ventana de días para sugerir match por fecha

TIPOS_MOV = ["cargo", "abono"]   # cargo = salida (egresos); abono = entrada (adelantos/cobranzas)
BANCOS_SUGERIDOS = [
    "Santander", "Banco de Chile", "BCI", "BancoEstado", "Itaú", "Scotiabank",
    "Security", "BICE", "Banco Falabella", "Banco Ripley", "Coopeuch", "Otro",
]

# Buckets del flujo de caja proyectado (días respecto de hoy).
FLUJO_BUCKETS = ["vencido", "d0_7", "d8_30", "d31_60", "d61_mas", "sin_fecha"]


def _f(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _parse_date(s) -> Optional[date]:
    if not s:
        return None
    if isinstance(s, datetime):
        return s.date()
    if isinstance(s, date):
        return s
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(str(s)[:10], fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def bucket_de(fecha: Optional[date], hoy: Optional[date] = None) -> str:
    """Clasifica una fecha de vencimiento en su bucket del flujo de caja."""
    if not fecha:
        return "sin_fecha"
    hoy = hoy or date.today()
    dias = (fecha - hoy).days
    if dias < 0:
        return "vencido"
    if dias <= 7:
        return "d0_7"
    if dias <= 30:
        return "d8_30"
    if dias <= 60:
        return "d31_60"
    return "d61_mas"


# ─── Parser de montos en formato chileno ──────────────────────────────────────
def parse_monto_cl(s) -> Optional[float]:
    """'1.234.567' → 1234567.0 ; '1.234.567,89' → 1234567.89 ; '($1.000)' → -1000.0.
    Devuelve None si no hay número."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    txt = str(s).strip()
    if not txt:
        return None
    negativo = txt.startswith("(") and txt.endswith(")")
    # OJO: los dos replace de "espacio" NO son redundantes: el primero es espacio ASCII
    # (0x20) y el segundo es espacio duro / non-breaking space (0xA0), común como
    # separador de miles en cartolas bancarias chilenas exportadas desde Excel.
    txt = txt.replace("(", "").replace(")", "").replace("$", "").replace(" ", "").replace(" ", "")
    txt = txt.replace("CLP", "").replace("clp", "")
    if not txt or txt in ("-", "."):
        return None
    # Soporta formato chileno ('1.234.567,89') y anglosajón ('1,234,567.89'):
    # cuando hay ambos separadores, el ÚLTIMO es el decimal. Con un solo tipo de
    # separador, un grupo final de EXACTAMENTE 3 dígitos se trata como miles
    # (CL '1.234' / US '50,000'); 1-2 dígitos finales son decimales.
    if "," in txt and "." in txt:
        if txt.rfind(",") > txt.rfind("."):
            txt = txt.replace(".", "").replace(",", ".")   # chileno
        else:
            txt = txt.replace(",", "")                      # anglosajón
    elif "," in txt:
        ent, _, dec = txt.rpartition(",")
        if len(dec) == 3:
            txt = txt.replace(",", "")                      # '50,000' → miles (US)
        else:
            txt = txt.replace(",", ".")                     # '123,45' → decimal (CL)
    elif "." in txt:
        ent, _, dec = txt.rpartition(".")
        if len(dec) == 3 or txt.count(".") > 1:
            txt = txt.replace(".", "")                      # '1.234' / '1.234.567' → miles (CL)
        else:
            pass                                            # '1234.56' → decimal (US)
    try:
        val = float(txt)
    except ValueError:
        return None
    return -val if negativo else val


def _norm(s) -> str:
    """minúsculas sin acentos ni espacios extra (para comparar encabezados)."""
    t = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode("ascii")
    return t.lower().strip()


# Sinónimos de encabezados → campo canónico
_HDR = {
    "fecha": "fecha", "fecha mov": "fecha", "fecha movimiento": "fecha", "fecha transaccion": "fecha",
    "glosa": "glosa", "descripcion": "glosa", "detalle": "glosa", "transaccion": "glosa",
    "movimiento": "glosa", "concepto": "glosa",
    "cargo": "cargo", "cargos": "cargo", "giro": "cargo", "giros": "cargo", "debito": "cargo",
    "cheques y otros cargos": "cargo", "monto cargo": "cargo",
    "abono": "abono", "abonos": "abono", "deposito": "abono", "depositos": "abono", "credito": "abono",
    "monto abono": "abono",
    "monto": "monto", "importe": "monto", "valor": "monto",
    "referencia": "referencia", "n documento": "referencia", "documento": "referencia",
    "docto": "referencia", "n operacion": "referencia", "operacion": "referencia", "n doc": "referencia",
    "saldo": "saldo",
}


def _map_headers(cells: list) -> dict:
    """Dado un row de encabezados, devuelve {campo_canonico: indice_col}."""
    out = {}
    for i, c in enumerate(cells):
        key = _HDR.get(_norm(c))
        if key and key not in out:
            out[key] = i
    return out


def _filas_de_archivo(content: bytes, filename: str) -> list:
    """Devuelve la matriz de filas (lista de listas) desde CSV o XLSX."""
    name = (filename or "").lower()
    if name.endswith(".xlsx"):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        ws = wb.active
        return [list(r) for r in ws.iter_rows(values_only=True)]
    if name.endswith(".xls"):
        raise ValueError("Formato .xls antiguo no soportado: guarde la cartola como .xlsx o .csv")
    # CSV (intenta utf-8, luego latin-1; autodetecta ; o ,)
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")
    sample = text[:2048]
    delim = ";" if sample.count(";") >= sample.count(",") else ","
    return [row for row in csv.reader(io.StringIO(text), delimiter=delim)]


def parse_cartola(content: bytes, filename: str) -> dict:
    """Parsea una cartola → {movimientos:[...], headers_detectados:[...], warnings:[...]}.
    Cada movimiento: {fecha, glosa, tipo, monto, referencia, saldo}."""
    filas = _filas_de_archivo(content, filename)
    warnings = []
    # Buscar la fila de encabezados (la primera que mapee fecha + (monto|cargo|abono))
    hdr_idx, headers = None, {}
    for i, fila in enumerate(filas[:25]):
        m = _map_headers(fila)
        if "fecha" in m and ("monto" in m or "cargo" in m or "abono" in m):
            hdr_idx, headers = i, m
            break
    if hdr_idx is None:
        raise ValueError("No se reconocieron los encabezados (se espera al menos Fecha y Monto/Cargo/Abono)")

    movimientos = []
    n_sin_fecha = 0
    n_sin_monto = 0
    for fila in filas[hdr_idx + 1:]:
        if not fila or all((c is None or str(c).strip() == "") for c in fila):
            continue

        def cell(campo):
            idx = headers.get(campo)
            return fila[idx] if idx is not None and idx < len(fila) else None

        fecha = _parse_date(cell("fecha"))
        if not fecha:
            n_sin_fecha += 1
            continue  # filas de totales/encabezados intermedios

        cargo = parse_monto_cl(cell("cargo")) if "cargo" in headers else None
        abono = parse_monto_cl(cell("abono")) if "abono" in headers else None
        monto_raw = parse_monto_cl(cell("monto")) if "monto" in headers else None

        if cargo:
            tipo, monto = "cargo", abs(cargo)
        elif abono:
            tipo, monto = "abono", abs(abono)
        elif monto_raw is not None and monto_raw != 0:
            tipo = "abono" if monto_raw > 0 else "cargo"
            monto = abs(monto_raw)
        else:
            n_sin_monto += 1
            continue  # sin monto → se ignora

        movimientos.append({
            "fecha": fecha.isoformat(),
            "glosa": (str(cell("glosa")).strip() if cell("glosa") is not None else None),
            "tipo": tipo,
            "monto": round(monto, 2),
            "referencia": (str(cell("referencia")).strip() if cell("referencia") is not None else None),
            "saldo": parse_monto_cl(cell("saldo")) if "saldo" in headers else None,
        })

    if not movimientos:
        warnings.append("No se encontraron movimientos con fecha y monto válidos")
    # Transparencia: las filas descartadas se informan (normal si son totales/subtítulos,
    # pero permite detectar una cartola con formato de fecha/monto no reconocido).
    if n_sin_fecha:
        warnings.append(f"{n_sin_fecha} fila(s) sin fecha reconocible se omitieron (típicamente totales o subtítulos)")
    if n_sin_monto:
        warnings.append(f"{n_sin_monto} fila(s) con fecha pero sin monto reconocible se omitieron")
    return {
        "movimientos": movimientos,
        "headers_detectados": list(headers.keys()),
        "warnings": warnings,
    }


# ─── Serializadores ───────────────────────────────────────────────────────────
def serialize_cuenta(c) -> dict:
    return {
        "id": c.id, "banco": c.banco, "nombre": c.nombre,
        "numero_cuenta": c.numero_cuenta, "moneda": c.moneda,
        "activo": bool(c.activo), "observaciones": c.observaciones,
    }


def serialize_cartola(ca) -> dict:
    return {
        "id": ca.id, "cuenta_id": ca.cuenta_id, "nombre": ca.nombre,
        "fecha_desde": ca.fecha_desde.isoformat() if ca.fecha_desde else None,
        "fecha_hasta": ca.fecha_hasta.isoformat() if ca.fecha_hasta else None,
        "archivo": ca.archivo, "origen": ca.origen, "n_movimientos": ca.n_movimientos,
        "created_at": ca.created_at.isoformat() if ca.created_at else None,
    }


def serialize_movimiento(m, *, destino: Optional[dict] = None) -> dict:
    """`destino` = resumen del egreso o adelanto conciliado (si aplica)."""
    return {
        "id": m.id, "cuenta_id": m.cuenta_id, "cartola_id": m.cartola_id,
        "fecha": m.fecha.isoformat() if m.fecha else None,
        "glosa": m.glosa, "tipo": m.tipo, "monto": _f(m.monto),
        "referencia": m.referencia, "saldo": _f(m.saldo) if m.saldo is not None else None,
        "conciliado": bool(m.conciliado),
        "conciliado_at": m.conciliado_at.isoformat() if m.conciliado_at else None,
        "observaciones": m.observaciones,
        "destino": destino,
    }
