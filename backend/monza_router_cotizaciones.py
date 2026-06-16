import os
import io
from datetime import datetime, date
from typing import Optional, List

from fastapi import Request, APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_
from pydantic import BaseModel

from database import get_db
from auth import get_current_user
from monza_notif import crear_notif
from monza_models import (
    MonzaCotizacion, MonzaCotizacionItem, MonzaCliente,
    MonzaLead, MonzaLeadItem, MonzaLeadActividad, MonzaConfig
)

router = APIRouter(prefix="/api/monza/cotizaciones", tags=["monza-cotizaciones"])

RESULTS_DIR = "/var/www/machparts.bigcode.cl/backend/results"


# ── Schemas ───────────────────────────────────────────────────────────────────

class CotItemIn(BaseModel):
    descripcion: str
    numero_parte: Optional[str] = None
    marca: Optional[str] = None
    procedencia: Optional[str] = None
    calidad: Optional[str] = None
    cantidad: int = 1
    costo: Optional[float] = None
    moneda: str = "EUR"
    peso_kg: float = 0
    markup_pct: float = 0
    precio_unitario_clp: Optional[float] = None
    subtotal_clp: Optional[float] = None
    plazo_entrega: Optional[str] = None

class CotCreate(BaseModel):
    lead_id: Optional[int] = None
    cliente_id: int
    tipo_cotizacion: Optional[str] = "Importación de Repuestos"
    forma_pago: Optional[str] = "30 días contra factura"
    linea: Optional[str] = None
    vehiculo: Optional[str] = None
    anio: Optional[str] = None
    condiciones_servicio: Optional[str] = None
    oc_cliente: Optional[str] = None
    vin: Optional[str] = None
    items: List[CotItemIn]

class CotUpdate(BaseModel):
    estado: Optional[str] = None
    oc_cliente: Optional[str] = None
    fecha_entrega_est: Optional[date] = None
    fecha_venta: Optional[datetime] = None
    forma_pago: Optional[str] = None
    numero_factura: Optional[str] = None
    fecha_despacho: Optional[date] = None
    tipo_documento: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _gen_numero_cot(db: Session) -> str:
    anio = datetime.utcnow().year
    last = (
        db.query(MonzaCotizacion)
        .filter(MonzaCotizacion.numero.like(f"COT-{anio}-%"))
        .order_by(MonzaCotizacion.id.desc())
        .first()
    )
    n = int(last.numero.split("-")[-1]) + 1 if last else 1
    return f"COT-{anio}-{n:06d}"


def _get_config(db: Session) -> MonzaConfig:
    cfg = db.query(MonzaConfig).filter(MonzaConfig.id == 1).first()
    if not cfg:
        cfg = MonzaConfig(id=1)
        db.add(cfg); db.commit(); db.refresh(cfg)
    return cfg


def _cot_dict(c: MonzaCotizacion) -> dict:
    return {
        "id": c.id,
        "numero": c.numero,
        "estado": c.estado,
        "tipo_cotizacion": c.tipo_cotizacion,
        "forma_pago": c.forma_pago,
        "linea": c.linea,
        "vehiculo": c.vehiculo,
        "anio": c.anio,
        "vin": c.vin,
        "total_neto": c.total_neto,
        "iva_monto": c.iva_monto,
        "total_bruto": c.total_bruto,
        "fecha_venta": c.fecha_venta.isoformat() if c.fecha_venta else None,
        "fecha_entrega_est": c.fecha_entrega_est.isoformat() if c.fecha_entrega_est else None,
        "oc_cliente": c.oc_cliente,
        "asesor_id": c.asesor_id,
        "fecha_creacion": c.fecha_creacion.isoformat(),
        "cliente": {
            "id": c.cliente.id,
            "nombre": c.cliente.nombre,
            "rut": c.cliente.rut,
            "telefono": c.cliente.telefono,
            "email": c.cliente.email,
        } if c.cliente else None,
        "lead_numero": c.lead.numero if c.lead else None,
        "items_count": len(c.items),
        "fecha_despacho": c.fecha_despacho.isoformat() if c.fecha_despacho else None,
        "numero_factura": c.numero_factura,
        "tipo_documento": c.tipo_documento,
        "tiene_documento": bool(c.documento_path),
    }


def _cot_detail(c: MonzaCotizacion) -> dict:
    d = _cot_dict(c)
    d["items"] = [
        {
            "id": it.id,
            "descripcion": it.descripcion,
            "numero_parte": it.numero_parte,
            "marca": it.marca,
            "procedencia": it.procedencia,
            "calidad": it.calidad,
            "cantidad": it.cantidad,
            "costo": it.costo,
            "moneda": it.moneda,
            "peso_kg": it.peso_kg,
            "markup_pct": it.markup_pct,
            "precio_unitario_clp": it.precio_unitario_clp,
            "subtotal_clp": it.subtotal_clp,
            "plazo_entrega": it.plazo_entrega,
            "estado_linea": it.estado_linea or "cotizado",
        }
        for it in c.items
    ]
    d["condiciones_servicio"] = c.condiciones_servicio
    d["config_snapshot"] = {
        "tc_usd_clp": c.tc_usd_clp,
        "tc_eur_clp": c.tc_eur_clp,
        "tarifa_aerea": c.tarifa_aerea,
        "iva_pct": c.iva_pct,
    }
    return d


# ── List ──────────────────────────────────────────────────────────────────────


# ── Log helper ────────────────────────────────────────────────────────────────
def _log(db, user_email, accion, entidad, entidad_id=None, entidad_ref=None, detalle=None, request=None):
    from monza_models import MonzaLog
    ip = None
    if request:
        forwarded = request.headers.get("X-Forwarded-For")
        ip = forwarded.split(",")[0].strip() if forwarded else getattr(request.client, "host", None)
    lg = MonzaLog(user_email=user_email, accion=accion, entidad=entidad,
                  entidad_id=entidad_id, entidad_ref=entidad_ref, detalle=detalle, ip=ip)
    db.add(lg)
    db.commit()

@router.get("")
def list_cotizaciones(
    q: Optional[str] = Query(None),
    estado: Optional[str] = Query(None),
    asesor_id: Optional[int] = Query(None),
    desde: Optional[str] = Query(None),
    hasta: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    query = (
        db.query(MonzaCotizacion)
        .options(joinedload(MonzaCotizacion.cliente), joinedload(MonzaCotizacion.lead), joinedload(MonzaCotizacion.items))
    )
    if q:
        query = query.join(MonzaCotizacion.cliente, isouter=True).filter(
            or_(
                MonzaCotizacion.numero.ilike(f"%{q}%"),
                MonzaCliente.nombre.ilike(f"%{q}%"),
                MonzaCotizacion.vehiculo.ilike(f"%{q}%"),
            )
        )
    if estado and estado != "todos":
        query = query.filter(MonzaCotizacion.estado == estado)
    if asesor_id:
        query = query.filter(MonzaCotizacion.asesor_id == asesor_id)
    if desde:
        query = query.filter(MonzaCotizacion.fecha_creacion >= datetime.fromisoformat(desde))
    if hasta:
        query = query.filter(MonzaCotizacion.fecha_creacion <= datetime.fromisoformat(hasta))

    total = query.count()
    items = query.order_by(MonzaCotizacion.fecha_creacion.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "page": page, "page_size": page_size, "items": [_cot_dict(c) for c in items]}


# ── Create ────────────────────────────────────────────────────────────────────

@router.post("", status_code=201)
def create_cotizacion(body: CotCreate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    cfg = _get_config(db)

    cliente = db.query(MonzaCliente).filter(MonzaCliente.id == body.cliente_id).first()
    if not cliente:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")

    condiciones = body.condiciones_servicio or cfg.condiciones_default

    cot = MonzaCotizacion(
        numero=_gen_numero_cot(db),
        lead_id=body.lead_id,
        cliente_id=body.cliente_id,
        tipo_cotizacion=body.tipo_cotizacion,
        forma_pago=body.forma_pago,
        linea=body.linea,
        vehiculo=body.vehiculo,
        anio=body.anio,
        vin=body.vin,
        asesor_id=current_user.id,
        condiciones_servicio=condiciones,
        oc_cliente=body.oc_cliente,
        tc_usd_clp=cfg.tc_usd_clp,
        tc_eur_clp=cfg.tc_eur_clp,
        tarifa_aerea=cfg.tarifa_aerea_por_kg,
        iva_pct=cfg.iva_pct,
    )
    db.add(cot)
    db.flush()

    total_neto = 0
    for it in body.items:
        precio_unit = it.precio_unitario_clp or 0
        subtotal = it.subtotal_clp or (precio_unit * it.cantidad)
        total_neto += subtotal
        db.add(MonzaCotizacionItem(
            cotizacion_id=cot.id,
            descripcion=it.descripcion,
            numero_parte=it.numero_parte,
            marca=it.marca,
            procedencia=it.procedencia,
            calidad=it.calidad,
            cantidad=it.cantidad,
            costo=it.costo,
            moneda=it.moneda,
            peso_kg=it.peso_kg,
            markup_pct=it.markup_pct / 100 if it.markup_pct > 1 else it.markup_pct,
            precio_unitario_clp=precio_unit,
            subtotal_clp=subtotal,
            plazo_entrega=it.plazo_entrega,
        ))

    iva_monto = round(total_neto * cfg.iva_pct / 100)
    cot.total_neto = round(total_neto)
    cot.iva_monto = iva_monto
    cot.total_bruto = round(total_neto + iva_monto)

    # Actividad en el lead
    if body.lead_id:
        lead = db.query(MonzaLead).filter(MonzaLead.id == body.lead_id).first()
        if lead:
            asesor_nombre = current_user.email.split("@")[0].title()
            db.add(MonzaLeadActividad(
                lead_id=body.lead_id,
                tipo="cotizacion",
                descripcion=f"Cotización {cot.numero} generada por {asesor_nombre}",
                usuario=asesor_nombre,
                usuario_id=current_user.id,
            ))
            lead.total_estimado = cot.total_bruto
            lead.fecha_actualizacion = datetime.utcnow()

    db.commit()
    db.refresh(cot)
    _log(db, current_user.email, "CREATE", "cotizacion",
         cot.id, cot.numero, f"Cotización {cot.numero} emitida")
    return _cot_dict(cot)


# ── Get detail ────────────────────────────────────────────────────────────────

@router.get("/{cot_id}")
def get_cotizacion(cot_id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    cot = (
        db.query(MonzaCotizacion)
        .options(
            joinedload(MonzaCotizacion.cliente),
            joinedload(MonzaCotizacion.lead),
            joinedload(MonzaCotizacion.items),
            joinedload(MonzaCotizacion.asesor),
        )
        .filter(MonzaCotizacion.id == cot_id)
        .first()
    )
    if not cot:
        raise HTTPException(status_code=404, detail="Cotización no encontrada")
    return _cot_detail(cot)


# ── Update estado ─────────────────────────────────────────────────────────────

@router.patch("/{cot_id}")
def update_cotizacion(cot_id: int, body: CotUpdate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    cot = db.query(MonzaCotizacion).filter(MonzaCotizacion.id == cot_id).first()
    if not cot:
        raise HTTPException(status_code=404, detail="Cotización no encontrada")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(cot, field, value)
    if body.estado == "vendida" and not cot.fecha_venta:
        cot.fecha_venta = datetime.utcnow()
    if body.estado == "vendida":
        for _it in cot.items:
            if (_it.estado_linea or "cotizado") == "cotizado":
                _it.estado_linea = "por_comprar"
    if body.estado == "despachado" and not cot.fecha_despacho:
        cot.fecha_despacho = datetime.utcnow().date()
    if body.estado == "despachado":
        for _it in cot.items:
            if (_it.estado_linea or "cotizado") != "despachado":
                _it.estado_linea = "despachado"
        # Actualizar lead si existe
        if cot.lead_id:
            lead = db.query(MonzaLead).filter(MonzaLead.id == cot.lead_id).first()
            if lead:
                lead.estado = "cerrado"
                lead.fecha_actualizacion = datetime.utcnow()
                # ltv cliente
                if lead.cliente_id:
                    c = db.query(MonzaCliente).filter(MonzaCliente.id == lead.cliente_id).first()
                    if c:
                        c.ltv = (c.ltv or 0) + cot.total_bruto
    db.commit()
    db.refresh(cot)
    _action = body.estado.upper() if body.estado else "UPDATE"
    _log(db, current_user.email, _action if _action in ("VENDIDA","DESPACHADO") else "UPDATE",
         "cotizacion", cot.id, cot.numero,
         f"Estado → {cot.estado}" if body.estado else "Actualización")
    if body.estado == "vendida":
        _cli = cot.cliente.nombre if cot.cliente else ""
        crear_notif(db, f"Nueva venta · {cot.numero}", f"{_cli} — ${int(cot.total_bruto or 0):,}".replace(",", "."), "success", "/monzaparts/ventas", "cotizacion", cot.id)
    elif body.estado == "despachado":
        crear_notif(db, f"Despacho realizado · {cot.numero}", "Cotización despachada al cliente", "info", "/monzaparts/despachos", "cotizacion", cot.id)
    return _cot_dict(cot)



# ── Documento de despacho ─────────────────────────────────────────────────────

@router.post("/{cot_id}/documento")
async def upload_documento(
    cot_id: int,
    file: UploadFile = File(...),
    tipo: str = Form("factura"),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    cot = db.query(MonzaCotizacion).filter(MonzaCotizacion.id == cot_id).first()
    if not cot:
        raise HTTPException(status_code=404, detail="Cotización no encontrada")
    ext = os.path.splitext(file.filename)[1] if file.filename else ".pdf"
    filename = f"despacho_{cot_id}_{int(datetime.utcnow().timestamp())}{ext}"
    dest_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "despachos")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, filename)
    content_bytes = await file.read()
    with open(dest, "wb") as f:
        f.write(content_bytes)
    cot.documento_path = filename
    cot.tipo_documento = tipo
    db.commit()
    _log(db, _.email if hasattr(_, "email") else "sistema", "UPLOAD", "cotizacion",
         cot.id, cot.numero, f"Documento {tipo}: {filename}")
    return {"ok": True, "filename": filename}


@router.get("/{cot_id}/documento")
def get_documento(cot_id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    cot = db.query(MonzaCotizacion).filter(MonzaCotizacion.id == cot_id).first()
    if not cot or not cot.documento_path:
        raise HTTPException(status_code=404, detail="Sin documento")
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "despachos", cot.documento_path)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    from fastapi.responses import FileResponse
    return FileResponse(path, filename=cot.documento_path)

# ── PDF ───────────────────────────────────────────────────────────────────────

@router.get("/{cot_id}/pdf")
def download_pdf(cot_id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    cot = (
        db.query(MonzaCotizacion)
        .options(
            joinedload(MonzaCotizacion.cliente),
            joinedload(MonzaCotizacion.items),
            joinedload(MonzaCotizacion.asesor),
        )
        .filter(MonzaCotizacion.id == cot_id)
        .first()
    )
    if not cot:
        raise HTTPException(status_code=404, detail="Cotización no encontrada")

    cfg = _get_config(db)
    pdf_bytes = _generar_pdf(cot, cfg)

    numero_safe = cot.numero.replace("/", "-")
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="Cotizacion_{numero_safe}.pdf"'},
    )


def _generar_pdf(cot, cfg) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
        HRFlowable, Image,
    )
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT

    # ── Paleta exacta del template Excel ──────────────────────────────────────
    ORANGE   = colors.HexColor("#E67E22")
    PEACH_BG = colors.HexColor("#FDF2E9")
    AMBER_H  = colors.HexColor("#F5CBA7")
    DARK     = colors.HexColor("#1E293B")
    GRAY_ALT = colors.HexColor("#FAFAFA")
    LGRAY    = colors.HexColor("#E2E8F0")
    WHITE    = colors.white

    W      = 18.0 * cm
    MARGIN = 1.5 * cm

    # ── Helper de estilo ──────────────────────────────────────────────────────
    def ps(name, size=8, bold=False, color=None, align=TA_LEFT):
        if color is None:
            color = DARK
        return ParagraphStyle(
            name, fontSize=size,
            fontName="Helvetica-Bold" if bold else "Helvetica",
            textColor=color, alignment=align,
            leading=size + 3,
            spaceAfter=0, spaceBefore=0,
        )

    title_s   = ps("title",  14, bold=True,  color=ORANGE, align=TA_CENTER)
    lbl_o     = ps("lbl_o",   9, bold=True,  color=ORANGE)
    val_s     = ps("val",     9,              color=DARK)
    sec_lbl   = ps("sec_lbl",10, bold=True,  color=ORANGE)
    lbl_b     = ps("lbl_b",   8, bold=True,  color=DARK)
    val_b     = ps("val_b",   8,              color=DARK)
    hdr_c     = ps("hdr_c",   9, bold=True,  color=DARK, align=TA_CENTER)
    hdr_l     = ps("hdr_l",   9, bold=True,  color=DARK, align=TA_LEFT)
    hdr_r     = ps("hdr_r",   9, bold=True,  color=DARK, align=TA_RIGHT)
    it_l      = ps("it_l",    9,              color=DARK, align=TA_LEFT)
    it_c      = ps("it_c",    9,              color=DARK, align=TA_CENTER)
    it_r      = ps("it_r",    9,              color=DARK, align=TA_RIGHT)
    plazo_s   = ps("pz",      8,              color=DARK, align=TA_CENTER)
    tot_lbl   = ps("tot_lbl", 9, bold=True,  color=DARK, align=TA_RIGHT)
    tot_val   = ps("tot_val", 9,              color=DARK, align=TA_RIGHT)
    grand_lbl = ps("grand_l",10, bold=True,  color=WHITE, align=TA_RIGHT)
    grand_val = ps("grand_v",10, bold=True,  color=WHITE, align=TA_RIGHT)
    cond_s    = ps("cond",    8,              color=DARK)
    foot_s    = ps("foot",    7,              color=colors.HexColor("#94A3B8"), align=TA_CENTER)
    co_r      = ps("co_r",    8,              color=DARK, align=TA_RIGHT)
    co_rb     = ps("co_rb",   8, bold=True,  color=DARK, align=TA_RIGHT)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=MARGIN + 0.5 * cm,
    )
    story = []

    # ── HEADER: Logo izquierda + datos empresa derecha ────────────────────────
    LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "logo_grupoam.png")
    if os.path.exists(LOGO_PATH):
        logo = Image(LOGO_PATH, width=3.5 * cm, height=2.5 * cm, kind="proportional")
    else:
        logo = Paragraph("", val_s)

    co_inner = Table(
        [[Paragraph(cfg.razon_social or "", co_rb)],
         [Paragraph(cfg.rut_empresa or "", co_r)],
         [Paragraph(cfg.direccion or "", co_r)]],
        colWidths=[W - 4.0 * cm],
    )
    co_inner.setStyle(TableStyle([
        ("VALIGN",       (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING",   (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 1),
        ("LEFTPADDING",  (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))

    header_tbl = Table([[logo, co_inner]], colWidths=[4.0 * cm, W - 4.0 * cm])
    header_tbl.setStyle(TableStyle([
        ("VALIGN",       (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",  (0, 0), (0, -1), 0),
        ("RIGHTPADDING", (1, 0), (1, -1), 0),
        ("TOPPADDING",   (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 0),
    ]))
    story.append(header_tbl)
    story.append(Spacer(1, 6))

    # ── TÍTULO ────────────────────────────────────────────────────────────────
    story.append(Paragraph(f"COTIZACIÓN Nº : {cot.numero}", title_s))
    story.append(Spacer(1, 10))

    # ── METADATA (2 columnas, sin bordes, labels naranja) ─────────────────────
    fecha_str = cot.fecha_creacion.strftime("%d/%m/%Y") if cot.fecha_creacion else ""
    vehiculo  = cot.vehiculo or ""

    meta_rows = [
        [Paragraph("Tipo Cotización:", lbl_o), Paragraph(cot.tipo_cotizacion or "", val_s),
         Paragraph("Estado:", lbl_o), Paragraph((cot.estado or "propuesta").capitalize(), val_s)],
        [Paragraph("Fecha Solicitud:", lbl_o), Paragraph(fecha_str, val_s),
         Paragraph("Forma Pago:", lbl_o), Paragraph(cot.forma_pago or "", val_s)],
        [Paragraph("Marca:", lbl_o), Paragraph((vehiculo.split(" ", 1)[0] if vehiculo else ""), val_s),
         Paragraph("Modelo:", lbl_o), Paragraph((vehiculo.split(" ", 1)[1] if " " in (vehiculo or "") else ""), val_s)],
        [Paragraph("VIN:", lbl_o), Paragraph(getattr(cot, "vin", "") or "", val_s),
         Paragraph("Año:", lbl_o), Paragraph(cot.anio or "", val_s)],
        [Paragraph("Descripción:", lbl_o),
         Paragraph("Por solicitud del cliente se presenta cotización por importación de repuestos", val_s),
         "", ""],
    ]
    meta_tbl = Table(meta_rows, colWidths=[2.8 * cm, 5.5 * cm, 2.7 * cm, 7.0 * cm])
    meta_tbl.setStyle(TableStyle([
        ("FONTSIZE",     (0, 0), (-1, -1), 9),
        ("VALIGN",       (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING",   (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 2),
        ("LEFTPADDING",  (0, 0), (-1, -1), 0),
        ("SPAN",         (1, 4), (3, 4)),
    ]))
    story.append(meta_tbl)
    story.append(Spacer(1, 8))

    # ── DATOS CLIENTE + OC ────────────────────────────────────────────────────
    cli  = cot.cliente
    aten = ""
    if cot.asesor:
        aten = (getattr(cot.asesor, "email", "") or "").split("@")[0].replace(".", " ").title()

    cli_rows = [
        [Paragraph("Datos Cliente", sec_lbl), "",
         Paragraph("Datos para Orden de Compra", sec_lbl), ""],
        [Paragraph("Cliente:", lbl_b),    Paragraph(cli.nombre if cli else "", val_b),
         Paragraph("Razón social:", lbl_b), Paragraph(cfg.razon_social or "", val_b)],
        [Paragraph("Rut:", lbl_b),        Paragraph(cli.rut if cli else "", val_b),
         Paragraph("Rut:", lbl_b),          Paragraph(cfg.rut_empresa or "", val_b)],
        [Paragraph("Atención:", lbl_b),   Paragraph(aten, val_b),
         Paragraph("Dirección:", lbl_b),    Paragraph(cfg.direccion or "", val_b)],
        [Paragraph("Servicio:", lbl_b),   Paragraph(cot.tipo_cotizacion or "Importación de Repuestos", val_b),
         Paragraph("Giro:", lbl_b),         Paragraph(cfg.giro or "", val_b)],
        [Paragraph("Referencia:", lbl_b), Paragraph(cot.oc_cliente or "", val_b),
         Paragraph("Correo:", lbl_b),       Paragraph(cfg.email_empresa or "", val_b)],
    ]
    cli_tbl = Table(cli_rows, colWidths=[2.5 * cm, 6.0 * cm, 2.8 * cm, 6.7 * cm])
    cli_tbl.setStyle(TableStyle([
        ("FONTSIZE",     (0, 0), (-1, -1), 8),
        ("VALIGN",       (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING",   (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 3),
        # Header con fondo peach
        ("BACKGROUND",   (0, 0), (1, 0), PEACH_BG),
        ("BACKGROUND",   (2, 0), (3, 0), PEACH_BG),
        ("SPAN",         (0, 0), (1, 0)),
        ("SPAN",         (2, 0), (3, 0)),
        # Bordes en filas de datos
        ("BOX",          (0, 1), (1, 5), 0.5, LGRAY),
        ("INNERGRID",    (0, 1), (1, 5), 0.3, LGRAY),
        ("BOX",          (2, 1), (3, 5), 0.5, LGRAY),
        ("INNERGRID",    (2, 1), (3, 5), 0.3, LGRAY),
        ("ROWBACKGROUNDS",(0, 1), (-1, 5), [WHITE, colors.HexColor("#F8F9FA")]),
    ]))
    story.append(cli_tbl)
    story.append(Spacer(1, 8))

    # ── TABLA ÍTEMS ───────────────────────────────────────────────────────────
    # A=Repuesto | B=Marca | C=N°parte | D=Cant | E=PrecioUnit | F=TOTAL | G=Plazo
    col_ws = [5.8*cm, 2.6*cm, 1.6*cm, 2.8*cm, 2.8*cm, 2.4*cm]
    # Total = 18.0 cm ✓

    thead = [
        Paragraph("<b>Repuesto</b>",         hdr_l),
        Paragraph("<b>Marca</b>",            hdr_c),
        Paragraph("<b>Cantidad</b>",         hdr_c),
        Paragraph("<b>Precio Unit.</b>",     hdr_r),
        Paragraph("<b>TOTAL</b>",            hdr_r),
        Paragraph("<b>Plazo de entrega</b>", hdr_c),
    ]

    def fmt_clp(n):
        if not n:
            return "—"
        return "$" + f"{int(n):,}".replace(",", ".")

    irows = [thead]
    for it in cot.items:
        irows.append([
            Paragraph(it.descripcion or "", it_l),
            Paragraph(it.marca or "", it_c),
            Paragraph(str(it.cantidad), it_c),
            Paragraph(fmt_clp(it.precio_unitario_clp), it_r),
            Paragraph(fmt_clp(it.subtotal_clp), it_r),
            Paragraph(it.plazo_entrega or "—", plazo_s),
        ])

    n_data = len(irows) - 1
    row_bgs = []
    for i in range(1, n_data + 1):
        bg = WHITE if i % 2 == 1 else colors.HexColor("#F8F9FA")
        row_bgs.append(("BACKGROUND", (0, i), (-1, i), bg))

    items_tbl = Table(irows, colWidths=col_ws)
    items_tbl.setStyle(TableStyle([
        ("FONTSIZE",     (0, 0), (-1, -1), 8),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",   (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 4),
        ("LEFTPADDING",  (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        # Cabecera con fondo ámbar
        ("BACKGROUND",   (0, 0), (-1, 0), AMBER_H),
        # Líneas
        ("LINEBELOW",    (0, 0), (-1, -1), 0.3, LGRAY),
        ("BOX",          (0, 0), (-1, -1), 0.5, LGRAY),
    ] + row_bgs))
    story.append(items_tbl)

    # ── TOTALES ───────────────────────────────────────────────────────────────
    empty4 = [""] * 4
    tot_rows = [
        [*empty4, Paragraph("<b>TOTAL NETO.</b>", tot_lbl), Paragraph(fmt_clp(cot.total_neto), tot_val)],
        [*empty4, Paragraph("IVA.",               tot_lbl), Paragraph(fmt_clp(cot.iva_monto),  tot_val)],
        [*empty4, Paragraph("<b>TOTAL.</b>",       grand_lbl),Paragraph(fmt_clp(cot.total_bruto),grand_val)],
    ]
    tot_tbl = Table(tot_rows, colWidths=col_ws)
    tot_tbl.setStyle(TableStyle([
        ("FONTSIZE",     (0, 0), (-1, -1), 9),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",   (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        # Fondo naranja en la fila TOTAL (índice 2)
        ("BACKGROUND",   (4, 2), (5, 2), ORANGE),
        # Bordes en columnas E-F
        ("BOX",          (4, 0), (5, 2), 0.5, LGRAY),
        ("LINEBELOW",    (4, 0), (5, 1), 0.3, LGRAY),
    ]))
    story.append(tot_tbl)
    story.append(Spacer(1, 12))

    # ── CONDICIONES DE SERVICIO ───────────────────────────────────────────────
    story.append(Paragraph(
        "Condiciones de Servicio",
        ps("cs_hdr", 9, bold=True, color=ORANGE),
    ))
    story.append(Spacer(1, 3))
    condiciones = cot.condiciones_servicio or ""
    for linea in condiciones.split("\n"):
        if linea.strip():
            story.append(Paragraph(linea.strip(), cond_s))
    story.append(Spacer(1, 12))

    # ── DATOS BANCARIOS ───────────────────────────────────────────────────────
    if cfg.banco or cfg.numero_cuenta:
        banco_hdr = Table(
            [[Paragraph("Datos Bancarios", sec_lbl)]],
            colWidths=[W],
        )
        banco_hdr.setStyle(TableStyle([
            ("BACKGROUND",   (0, 0), (-1, -1), PEACH_BG),
            ("TOPPADDING",   (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING",(0, 0), (-1, -1), 4),
        ]))
        story.append(banco_hdr)

        banco_rows = [
            [Paragraph("Razón Social:", lbl_b), Paragraph(cfg.razon_social or "", val_b),
             Paragraph("Banco:", lbl_b),        Paragraph(cfg.banco or "", val_b)],
            [Paragraph("Rut:", lbl_b),           Paragraph(cfg.rut_empresa or "", val_b),
             Paragraph("Tipo Cuenta:", lbl_b),   Paragraph(cfg.tipo_cuenta or "", val_b)],
            [Paragraph("Nº Cuenta:", lbl_b),     Paragraph(cfg.numero_cuenta or "", val_b),
             Paragraph("Mail:", lbl_b),           Paragraph(cfg.email_empresa or "", val_b)],
        ]
        banco_tbl = Table(banco_rows, colWidths=[2.5*cm, 6.0*cm, 2.8*cm, 6.7*cm])
        banco_tbl.setStyle(TableStyle([
            ("FONTSIZE",     (0, 0), (-1, -1), 8),
            ("VALIGN",       (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING",   (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING",(0, 0), (-1, -1), 3),
            ("ROWBACKGROUNDS",(0, 0), (-1, -1), [WHITE, colors.HexColor("#F8F9FA")]),
        ]))
        story.append(banco_tbl)

    # ── FOOTER ────────────────────────────────────────────────────────────────
    story.append(Spacer(1, 12))
    story.append(HRFlowable(width=W, thickness=1, color=ORANGE, spaceAfter=4))
    story.append(Paragraph(
        f"{cfg.razon_social} | RUT: {cfg.rut_empresa} | {cfg.email_empresa}",
        foot_s,
    ))

    doc.build(story)
    return buf.getvalue()
