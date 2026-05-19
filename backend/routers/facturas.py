"""
Router: /api/facturas
Gestión de facturas de proveedor vinculadas a OC-Proveedor.
Parse de Sales Invoice List PDF + CRUD manual de facturas e ítems.
"""
import os, re
from typing import Optional, List
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models.models import (
    FacturaProveedor, FacturaProveedorItem,
    OcProveedor, OcProveedorItem, ItemCotizacion,
    PreEmbarqueItem, User
)
from routers.auth import get_current_user

UPLOADS_DIR = os.path.join(os.path.dirname(__file__), '..', 'uploads')
os.makedirs(UPLOADS_DIR, exist_ok=True)

router = APIRouter(prefix="/facturas", tags=["facturas"])

# ─── Schemas ──────────────────────────────────────────────────────────────────

class FacturaItemIn(BaseModel):
    ocp_item_id:    Optional[int]   = None
    descripcion:    Optional[str]   = None
    qty_facturada:  float           = 0
    weight_lbs:     Optional[float] = None
    unit_price_usd: Optional[float] = None
    notas:          Optional[str]   = None

class FacturaIn(BaseModel):
    ocp_id:      int
    invoice_no:  str
    fecha:       Optional[str]   = None
    total_usd:   Optional[float] = None
    freight_usd: Optional[float] = None
    notas:       Optional[str]   = None
    items:       List[FacturaItemIn] = []

class FacturaItemUpdate(BaseModel):
    qty_facturada:  Optional[float] = None
    weight_lbs:     Optional[float] = None
    unit_price_usd: Optional[float] = None
    descripcion:    Optional[str]   = None
    notas:          Optional[str]   = None
    ocp_item_id:    Optional[int]   = None

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _calc_freight_proration(factura: FacturaProveedor):
    """Recalcula freight_prorrateado_usd para todos los ítems de la factura."""
    freight = float(factura.freight_usd or 0)
    if freight <= 0:
        for it in factura.items:
            it.freight_prorrateado_usd = None
        return
    total_peso = sum(
        float(it.weight_lbs or 0) * float(it.qty_facturada or 0)
        for it in factura.items
    )
    for it in factura.items:
        if total_peso > 0:
            it.freight_prorrateado_usd = round(
                float(it.weight_lbs or 0) * float(it.qty_facturada or 0)
                / total_peso * freight, 4
            )
        else:
            it.freight_prorrateado_usd = None

def _factura_to_dict(f: FacturaProveedor, db: Session):
    # Obtener qty del sistema para cada ítem (lo que está en el pre-embarque o OCP)
    items_out = []
    for it in f.items:
        qty_sistema = None
        if it.ocp_item_id:
            ocp_item = db.query(OcProveedorItem).filter_by(id=it.ocp_item_id).first()
            if ocp_item and ocp_item.item_cotizacion:
                qty_sistema = ocp_item.item_cotizacion.cantidad
        items_out.append({
            "id":                      it.id,
            "ocp_item_id":             it.ocp_item_id,
            "descripcion":             it.descripcion,
            "qty_facturada":           float(it.qty_facturada or 0),
            "qty_sistema":             qty_sistema,
            "weight_lbs":              float(it.weight_lbs) if it.weight_lbs else None,
            "unit_price_usd":          float(it.unit_price_usd) if it.unit_price_usd else None,
            "freight_prorrateado_usd": float(it.freight_prorrateado_usd) if it.freight_prorrateado_usd else None,
            "notas":                   it.notas,
            "match":                   (qty_sistema is None or float(it.qty_facturada or 0) == qty_sistema),
        })
    return {
        "id":          f.id,
        "ocp_id":      f.ocp_id,
        "invoice_no":  f.invoice_no,
        "fecha":       f.fecha,
        "total_usd":   float(f.total_usd) if f.total_usd else None,
        "freight_usd": float(f.freight_usd) if f.freight_usd else None,
        "archivo":     f.archivo,
        "notas":       f.notas,
        "created_at":  f.created_at.isoformat() if f.created_at else None,
        "items":       items_out,
    }

# ─── PDF Parser (Sales Invoice List) ─────────────────────────────────────────

def _parse_sales_invoice_list(pdf_bytes: bytes) -> list:
    """
    Extrae filas de un Sales Invoice List PDF (texto digital).
    Retorna lista de: {invoice_no, fecha, customer_po, total_usd}
    """
    try:
        import pdfplumber, io
    except ImportError:
        raise HTTPException(500, "pdfplumber no instalado en el servidor")

    results = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            # Intentar extraer tabla estructurada
            tables = page.extract_tables()
            for table in tables:
                for row in table:
                    if not row or len(row) < 4:
                        continue
                    # Limpiar celdas
                    cells = [str(c).strip() if c else '' for c in row]
                    # Saltar header
                    if any(h in cells[0].lower() for h in ['customer', 'date', 'invoice']):
                        continue
                    # Intentar parsear como fila de datos
                    # Formato esperado: CustomerID | CustomerName | Date | InvoiceNo | CustomerPO | Total
                    # Buscar el total (último campo con $)
                    total_str = ''
                    for c in reversed(cells):
                        if c and ('$' in c or re.match(r'[\d,]+\.\d{2}', c)):
                            total_str = c
                            break
                    try:
                        total_usd = float(re.sub(r'[^\d.]', '', total_str)) if total_str else None
                    except ValueError:
                        total_usd = None

                    # Buscar invoice_no (campo bold/numérico, generalmente col 3 o 4)
                    invoice_no = ''
                    customer_po = ''
                    fecha = ''
                    for i, c in enumerate(cells):
                        # Fecha: contiene /
                        if not fecha and re.match(r'\d{1,2}/\d{1,2}/\d{4}', c):
                            fecha = c
                        # Invoice No: número de 4-6 dígitos
                        elif not invoice_no and re.match(r'^\d{4,6}$', c):
                            invoice_no = c
                        # Customer PO: número diferente al invoice
                        elif invoice_no and not customer_po and re.match(r'^\d{3,6}$', c) and c != invoice_no:
                            customer_po = c

                    if invoice_no and customer_po:
                        results.append({
                            "invoice_no":   invoice_no,
                            "fecha":        fecha,
                            "customer_po":  customer_po,
                            "total_usd":    total_usd,
                        })

            # Si no hay tablas estructuradas, parsear texto
            if not results:
                text = page.extract_text() or ''
                for line in text.split('\n'):
                    # Buscar líneas con patrón: número fecha número número $monto
                    m = re.search(
                        r'(\d{1,2}/\d{1,2}/\d{4})\s+(\d{4,6})\s+(\d{3,6})\s+\$?([\d,]+\.\d{2})',
                        line
                    )
                    if m:
                        results.append({
                            "invoice_no":  m.group(2),
                            "fecha":       m.group(1),
                            "customer_po": m.group(3),
                            "total_usd":   float(m.group(4).replace(',', '')),
                        })
    return results


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/parse-pdf")
async def parse_pdf(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """Sube un Sales Invoice List PDF y extrae las filas de facturas."""
    content = await file.read()
    rows = _parse_sales_invoice_list(content)
    return {"parsed": rows, "count": len(rows)}


@router.post("/upload-pdf/{ocp_id}")
async def upload_pdf_factura(
    ocp_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Sube el PDF de una factura individual como adjunto (sin parsear ítems)."""
    ocp = db.query(OcProveedor).filter_by(id=ocp_id).first()
    if not ocp:
        raise HTTPException(404, "OC Proveedor no encontrada")

    filename = f"factura_{ocp_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{file.filename}"
    path = os.path.join(UPLOADS_DIR, filename)
    with open(path, 'wb') as f:
        f.write(await file.read())

    return {"filename": filename, "original": file.filename}


@router.get("")
def listar_facturas(
    ocp_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    facturas = db.query(FacturaProveedor).filter_by(ocp_id=ocp_id).order_by(FacturaProveedor.id).all()
    return [_factura_to_dict(f, db) for f in facturas]


@router.post("")
def crear_factura(
    body: FacturaIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ocp = db.query(OcProveedor).filter_by(id=body.ocp_id).first()
    if not ocp:
        raise HTTPException(404, "OC Proveedor no encontrada")

    factura = FacturaProveedor(
        ocp_id      = body.ocp_id,
        invoice_no  = body.invoice_no,
        fecha       = body.fecha,
        total_usd   = body.total_usd,
        freight_usd = body.freight_usd,
        notas       = body.notas,
    )
    db.add(factura)
    db.flush()

    for it in body.items:
        item = FacturaProveedorItem(
            factura_id     = factura.id,
            ocp_item_id    = it.ocp_item_id,
            descripcion    = it.descripcion,
            qty_facturada  = it.qty_facturada,
            weight_lbs     = it.weight_lbs,
            unit_price_usd = it.unit_price_usd,
            notas          = it.notas,
        )
        db.add(item)

    db.flush()
    # Recalcular prorrateo de freight
    db.refresh(factura)
    _calc_freight_proration(factura)
    db.commit()
    db.refresh(factura)
    return _factura_to_dict(factura, db)


@router.put("/{factura_id}")
def actualizar_factura(
    factura_id: int,
    body: FacturaIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    factura = db.query(FacturaProveedor).filter_by(id=factura_id).first()
    if not factura:
        raise HTTPException(404, "Factura no encontrada")

    factura.invoice_no  = body.invoice_no
    factura.fecha       = body.fecha
    factura.total_usd   = body.total_usd
    factura.freight_usd = body.freight_usd
    factura.notas       = body.notas

    # Reemplazar ítems
    for old in factura.items:
        db.delete(old)
    db.flush()

    for it in body.items:
        item = FacturaProveedorItem(
            factura_id     = factura.id,
            ocp_item_id    = it.ocp_item_id,
            descripcion    = it.descripcion,
            qty_facturada  = it.qty_facturada,
            weight_lbs     = it.weight_lbs,
            unit_price_usd = it.unit_price_usd,
            notas          = it.notas,
        )
        db.add(item)

    db.flush()
    db.refresh(factura)
    _calc_freight_proration(factura)
    db.commit()
    db.refresh(factura)
    return _factura_to_dict(factura, db)


@router.patch("/{factura_id}/items/{item_id}")
def actualizar_item(
    factura_id: int,
    item_id: int,
    body: FacturaItemUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = db.query(FacturaProveedorItem).filter_by(id=item_id, factura_id=factura_id).first()
    if not item:
        raise HTTPException(404, "Ítem no encontrado")

    if body.qty_facturada  is not None: item.qty_facturada  = body.qty_facturada
    if body.weight_lbs     is not None: item.weight_lbs     = body.weight_lbs
    if body.unit_price_usd is not None: item.unit_price_usd = body.unit_price_usd
    if body.descripcion    is not None: item.descripcion    = body.descripcion
    if body.notas          is not None: item.notas          = body.notas
    if body.ocp_item_id    is not None: item.ocp_item_id    = body.ocp_item_id

    db.flush()
    factura = db.query(FacturaProveedor).filter_by(id=factura_id).first()
    _calc_freight_proration(factura)
    db.commit()
    db.refresh(factura)
    return _factura_to_dict(factura, db)


@router.delete("/{factura_id}")
def eliminar_factura(
    factura_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    factura = db.query(FacturaProveedor).filter_by(id=factura_id).first()
    if not factura:
        raise HTTPException(404, "Factura no encontrada")
    db.delete(factura)
    db.commit()
    return {"ok": True}


@router.get("/validar-pre-embarque/{pre_embarque_id}")
def validar_pre_embarque(
    pre_embarque_id: int,
    invox_ocp_ids: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Verifica que todos los ítems del pre-embarque tienen qty_facturada
    coincidente con su cantidad en el sistema.

    Parametro opcional invox_ocp_ids (CSV de IDs de OcProveedor): los items
    cuya OCP esta en esta lista se consideran "registrados" aunque no haya
    FacturaProveedorItem en BD (el usuario tipeo el N° INVOX manualmente).
    El PDF de la factura puede subirse despues.

    Retorna: {ok: bool, discrepancias: [{numero_parte, qty_sistema, qty_facturada}]}
    """
    pe_items = db.query(PreEmbarqueItem).filter_by(pre_embarque_id=pre_embarque_id).all()
    discrepancias = []

    # Parsear OCP ids con invox provisto
    ocps_con_invox: set = set()
    if invox_ocp_ids:
        for s in invox_ocp_ids.split(","):
            s = s.strip()
            if s.isdigit():
                ocps_con_invox.add(int(s))

    for pe_item in pe_items:
        ic = db.query(ItemCotizacion).filter_by(id=pe_item.item_cotizacion_id).first()
        if not ic:
            continue
        qty_sistema = ic.cantidad

        # Buscar factura_proveedor_item vinculado a este item_cotizacion
        # via ocp_item → item_cotizacion
        ocp_items = db.query(OcProveedorItem).filter_by(item_cotizacion_id=ic.id).all()
        ocp_item_ids = [o.id for o in ocp_items]
        ocp_ids = {o.oc_proveedor_id for o in ocp_items if o.oc_proveedor_id}

        facturas_items = []
        if ocp_item_ids:
            facturas_items = db.query(FacturaProveedorItem).filter(
                FacturaProveedorItem.ocp_item_id.in_(ocp_item_ids)
            ).all()

        if not facturas_items:
            # Sin FacturaProveedorItem en BD: solo es discrepancia si tampoco
            # se provee N° INVOX manualmente para alguna OCP del item.
            if ocp_ids & ocps_con_invox:
                # Hay invox provisto -> considerar registrada (PDF puede subirse despues)
                continue
            # Sin factura registrada → discrepancia
            discrepancias.append({
                "numero_parte":   ic.numero_parte,
                "descripcion":    ic.descripcion or '',
                "qty_sistema":    qty_sistema,
                "qty_facturada":  None,
                "motivo":         "Sin factura registrada",
            })
        else:
            # Sumar qty_facturada de todas las facturas para este ítem
            qty_total_facturada = sum(float(fi.qty_facturada or 0) for fi in facturas_items)
            if qty_total_facturada != qty_sistema:
                discrepancias.append({
                    "numero_parte":   ic.numero_parte,
                    "descripcion":    ic.descripcion or '',
                    "qty_sistema":    qty_sistema,
                    "qty_facturada":  qty_total_facturada,
                    "motivo":         f"Facturado {qty_total_facturada} ≠ sistema {qty_sistema}",
                })

    return {"ok": len(discrepancias) == 0, "discrepancias": discrepancias}
