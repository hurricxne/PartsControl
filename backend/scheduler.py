from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
from datetime import date, timedelta

from database import SessionLocal
from models.models import (
    OcCliente, OcProveedor, OcProveedorItem, ItemCotizacion,
)
from notificaciones import crear_notificacion


def _business_days_between(start: date, end: date) -> int:
    """Count business days (Mon-Fri) between two dates, not including end."""
    if start >= end:
        return 0
    count = 0
    current = start
    while current < end:
        if current.weekday() < 5:  # Monday=0 … Friday=4
            count += 1
        current += timedelta(days=1)
    return count


def run_daily_checks():
    """Evaluate all alert rules and create notifications as needed."""
    db = SessionLocal()
    try:
        hoy = date.today()

        # ── Rule 2.1: OC Proveedor con plazo vencido ──────────────────────────
        asig_list = db.query(OcProveedorItem).filter(
            OcProveedorItem.plazo_dias_prov.isnot(None),
            OcProveedorItem.fecha_asignacion.isnot(None),
        ).all()

        for asig in asig_list:
            item = db.query(ItemCotizacion).filter(
                ItemCotizacion.id == asig.item_cotizacion_id
            ).first()
            if not item or item.estado_item not in ("comprado",):
                continue

            fecha_asig = (
                asig.fecha_asignacion.date()
                if hasattr(asig.fecha_asignacion, "date")
                else asig.fecha_asignacion
            )
            fecha_esperada = fecha_asig + timedelta(days=asig.plazo_dias_prov)

            if hoy > fecha_esperada:
                ocp = db.query(OcProveedor).filter(
                    OcProveedor.id == asig.oc_proveedor_id
                ).first()
                atraso = _business_days_between(fecha_esperada, hoy)
                for rol in ("abastecimiento", "logistica"):
                    crear_notificacion(
                        db=db,
                        rol=rol,
                        severidad="warning",
                        titulo=f"Plazo vencido: OCP {ocp.numero if ocp else asig.oc_proveedor_id}",
                        mensaje=(
                            f"Ítem {item.numero_parte} lleva {atraso} días de atraso"
                        ),
                        entidad_tipo="oc_proveedor",
                        entidad_id=asig.oc_proveedor_id,
                        link="/seguimiento",
                        regla="plazo_proveedor_rojo",
                    )

        # ── Rule B6/B7: OCs-Cliente listas / plazo crítico ────────────────────
        oc_list = db.query(OcCliente).all()
        for oc in oc_list:
            items = db.query(ItemCotizacion).filter(
                ItemCotizacion.cotizacion_id == oc.cotizacion_id,
                ItemCotizacion.estado_item.in_([
                    "cerrado", "comprado", "preparado", "pre_embarcado",
                    "embarcado", "en_bodega", "reclamo_proveedor",
                ])
            ).all()
            if not items:
                continue

            todos = all(i.estado_item == "en_bodega" for i in items)

            # B6: all items in bodega → notify Ventas
            if todos:
                crear_notificacion(
                    db=db,
                    rol="ventas",
                    severidad="info",
                    titulo=f"OC {oc.numero_oc} lista para despacho",
                    mensaje="Todos los ítems están en bodega.",
                    entidad_tipo="oc_cliente",
                    entidad_id=oc.id,
                    link="/cierre-venta",
                    regla="oc_lista_despacho",
                )

            # B7: deadline approaching with mixed items
            if oc.fecha_entrega:
                try:
                    fe = oc.fecha_entrega if hasattr(oc.fecha_entrega, "days") else None
                    if fe:
                        dias = (fe - hoy).days
                        en_bodega = [i for i in items if i.estado_item == "en_bodega"]
                        otros = [i for i in items if i.estado_item != "en_bodega"]
                        if dias <= 3 and en_bodega and otros:
                            crear_notificacion(
                                db=db,
                                rol="ventas",
                                severidad="critical",
                                titulo=f"Plazo crítico: OC {oc.numero_oc}",
                                mensaje=(
                                    f"Quedan {dias} días y {len(en_bodega)} ítems "
                                    f"están disponibles en bodega."
                                ),
                                entidad_tipo="oc_cliente",
                                entidad_id=oc.id,
                                link="/cierre-venta",
                                regla="plazo_critico_3d",
                            )
                except Exception:
                    pass

        print(f"[scheduler] daily checks done: {hoy}")
    except Exception as e:
        print(f"[scheduler] error: {e}")
    finally:
        db.close()


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    tz = pytz.timezone("America/Santiago")
    scheduler.add_job(
        run_daily_checks,
        CronTrigger(hour=6, minute=0, timezone=tz),
        id="daily_checks",
        replace_existing=True,
    )
    scheduler.start()
    print("[scheduler] APScheduler started — daily checks at 06:00 Santiago")
    return scheduler
