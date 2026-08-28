from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import desc
from typing import Optional
from datetime import datetime, timedelta

from database import get_db
from auth import get_current_user
from empresa_guard import require_empresa
from monza_fechas import dia_chile_utc, hoy_chile, rango_utc
from monza_models import MonzaLog

router = APIRouter(prefix="/api/monza/logs", tags=["monza-logs"],
    # Candado de empresa (hallazgo del equipo de testing 2026-08-27).
    # La bitácora es el mapa completo de la operación comercial de la marca: quién hizo
    # qué, sobre qué documento y con qué detalle en texto libre.
    dependencies=[Depends(require_empresa("automotriz"))],
)


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
    # Días de Chile → rango semiabierto en UTC (monza_fechas). Acá el `try/except
    # pass` era el peor de la familia: una fecha inválida IGNORABA el filtro en
    # silencio, y esta es la pantalla de AUDITORÍA — el operador creía estar viendo
    # un rango acotado y estaba viendo todo. Ahora falla cerrado con 422.
    desde_utc, hasta_utc = rango_utc(desde, hasta)
    if desde_utc:
        q = q.filter(MonzaLog.fecha >= desde_utc)
    if hasta_utc:
        q = q.filter(MonzaLog.fecha < hasta_utc)

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

    # El «hoy» de la tarjeta tiene que ser el MISMO que el del filtro de fecha de esta
    # pantalla (día de Chile): con date.today() del servidor en UTC, el resumen contaba
    # un día distinto del que el filtro de abajo mostraba.
    hoy = hoy_chile()

    total = db.query(func.count(MonzaLog.id)).scalar() or 0
    # `func.date(col) == hoy` comparaba el día en UTC contra el día de Chile, así que
    # seguía contando el día equivocado pese a que `hoy` ya era el correcto: un
    # movimiento de ayer a las 21:30 de Chile caía dentro de «Hoy». Se cuenta con el
    # MISMO rango semiabierto que usa el filtro de esta pantalla — tarjeta y tabla no
    # pueden decir cosas distintas.
    ini, fin = dia_chile_utc(hoy), dia_chile_utc(hoy + timedelta(days=1))
    hoy_count = db.query(func.count(MonzaLog.id)).filter(
        MonzaLog.fecha >= ini, MonzaLog.fecha < fin
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
