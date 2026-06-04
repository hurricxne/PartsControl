from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import desc
from typing import Optional
from datetime import datetime

from database import get_db
from auth import get_current_user
from monza_models import MonzaLog

router = APIRouter(prefix="/api/monza/logs", tags=["monza-logs"])


@router.get("")
def list_logs(
    accion: Optional[str] = Query(None),
    entidad: Optional[str] = Query(None),
    user_email: Optional[str] = Query(None),
    desde: Optional[str] = Query(None),
    hasta: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    q = db.query(MonzaLog)

    if accion:
        q = q.filter(MonzaLog.accion == accion.upper())
    if entidad:
        q = q.filter(MonzaLog.entidad == entidad.lower())
    if user_email:
        q = q.filter(MonzaLog.user_email.ilike(f"%{user_email}%"))
    if desde:
        try:
            q = q.filter(MonzaLog.fecha >= datetime.fromisoformat(desde))
        except Exception:
            pass
    if hasta:
        try:
            q = q.filter(MonzaLog.fecha <= datetime.fromisoformat(hasta))
        except Exception:
            pass

    total = q.count()
    items = (
        q.order_by(desc(MonzaLog.fecha))
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "id": lg.id,
                "user_email": lg.user_email,
                "accion": lg.accion,
                "entidad": lg.entidad,
                "entidad_id": lg.entidad_id,
                "entidad_ref": lg.entidad_ref,
                "detalle": lg.detalle,
                "ip": lg.ip,
                "fecha": lg.fecha.isoformat(),
            }
            for lg in items
        ],
    }


@router.get("/summary")
def logs_summary(db: Session = Depends(get_db), _=Depends(get_current_user)):
    from sqlalchemy import func
    from datetime import date

    hoy = date.today()

    total = db.query(func.count(MonzaLog.id)).scalar() or 0
    hoy_count = db.query(func.count(MonzaLog.id)).filter(
        func.date(MonzaLog.fecha) == hoy
    ).scalar() or 0

    by_accion = (
        db.query(MonzaLog.accion, func.count(MonzaLog.id))
        .group_by(MonzaLog.accion)
        .all()
    )
    by_entidad = (
        db.query(MonzaLog.entidad, func.count(MonzaLog.id))
        .group_by(MonzaLog.entidad)
        .all()
    )

    return {
        "total": total,
        "hoy": hoy_count,
        "by_accion": {a: c for a, c in by_accion},
        "by_entidad": {e: c for e, c in by_entidad},
    }
