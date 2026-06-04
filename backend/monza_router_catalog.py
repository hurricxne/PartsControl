"""
Catálogo de partes MonzaParts — búsqueda y listas de precios.
Endpoint principal: GET /api/monza/catalog/search?q=...

Estrategia de búsqueda:
  - SKU exacto o prefijo  → índice B-tree idx_sku  (muy rápido)
  - Marca exacta/prefijo  → índice B-tree idx_marca (muy rápido)
  - Texto largo (desc.)   → FULLTEXT idx ft_partes  (solo si q ≥ 6 chars)
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_, text
from typing import Optional

from database import get_db
from auth import get_current_user
from monza_models import MonzaParteCatalogo, MonzaListaPrecios, MonzaProveedor

router = APIRouter(prefix="/api/monza/catalog", tags=["monza-catalog"])


# ── Listas de precios ─────────────────────────────────────────────────────────

@router.get("/listas")
def list_listas(db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Devuelve todas las listas de precios activas con su proveedor."""
    listas = (
        db.query(MonzaListaPrecios)
        .options(joinedload(MonzaListaPrecios.proveedor))
        .filter(MonzaListaPrecios.activo == True)  # noqa: E712
        .order_by(MonzaListaPrecios.code)
        .all()
    )
    return [
        {
            "id": l.id,
            "code": l.code,
            "nombre": l.nombre,
            "lead_time_dias": l.lead_time_dias,
            "proveedor": {
                "id": l.proveedor.id,
                "code": l.proveedor.code,
                "nombre": l.proveedor.nombre,
                "pais": l.proveedor.pais,
            } if l.proveedor else None,
        }
        for l in listas
    ]


# ── Búsqueda de partes ────────────────────────────────────────────────────────

@router.get("/search")
def search_parts(
    q: str = Query("", description="SKU, descripción o marca"),
    lista_id: Optional[int] = Query(None),
    calidad: Optional[str] = Query(None, description="GENUINO / OEM / AFTERMARKET"),
    marca: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """
    Busca partes en el catálogo CARAT (6.8M partes).

    Estrategia:
      - Para q con dígitos → búsqueda por SKU prefix (idx_sku, muy rápida)
      - Para q solo letras  → búsqueda por marca prefix (idx_marca, muy rápida)
      - Para q ≥ 6 letras   → también intenta FULLTEXT (descripción)
      - lista_id, calidad, marca se aplican como filtros adicionales
    """
    q_clean = q.strip()

    if len(q_clean) < 2 and not lista_id and not marca:
        return {"total": 0, "has_more": False, "page": page, "page_size": page_size, "items": []}

    offset = (page - 1) * page_size
    fetch_limit = page_size + 1  # +1 para saber si hay más

    # ── Determinar estrategia de búsqueda ────────────────────────────────────
    has_digits = any(c.isdigit() for c in q_clean)
    is_alpha   = q_clean.replace(" ", "").replace("-", "").isalpha()
    q_prefix   = f"{q_clean}%"   # para LIKE prefix (usa índice B-tree)

    # Construir filtros extra (lista_id, calidad, marca)
    extras_qs = []
    params: dict = {"fetch_limit": fetch_limit, "offset": offset}

    if lista_id:
        extras_qs.append("AND lista_id = :lista_id")
        params["lista_id"] = lista_id
    if calidad:
        extras_qs.append("AND calidad = :calidad")
        params["calidad"] = calidad.upper()
    if marca:
        extras_qs.append("AND marca LIKE :marca_filter")
        params["marca_filter"] = f"{marca}%"

    extras_sql = " ".join(extras_qs)

    # ── Estrategia 1: Prefix por SKU (si hay dígitos o q ≤ 5 chars) ──────────
    if q_clean and (has_digits or len(q_clean) <= 5):
        params["q_prefix"] = q_prefix
        sql = text(f"""
            SELECT id FROM monza_partes_catalogo
            WHERE disponible = 1
              AND sku LIKE :q_prefix
              {extras_sql}
            ORDER BY sku
            LIMIT :fetch_limit OFFSET :offset
        """)
        rows = db.execute(sql, params).fetchall()
        ids = [r[0] for r in rows[:page_size]]
        has_more = len(rows) > page_size

        if ids:
            return _build_response(db, ids, has_more, page, page_size)

    # ── Estrategia 2: Prefix por marca (si es texto) ──────────────────────────
    # Usa índice compuesto idx_marca_sku(marca,sku) → cubre filter + ORDER BY sin filesort
    if q_clean and is_alpha:
        params2 = {**params, "q_marca": q_prefix}
        sql2 = text(f"""
            SELECT id FROM monza_partes_catalogo
            WHERE marca LIKE :q_marca
              {extras_sql}
            ORDER BY marca, sku
            LIMIT :fetch_limit OFFSET :offset
        """)
        rows2 = db.execute(sql2, params2).fetchall()
        ids2 = [r[0] for r in rows2[:page_size]]
        has_more2 = len(rows2) > page_size

        if ids2:
            return _build_response(db, ids2, has_more2, page, page_size)

    # ── Estrategia 3: FULLTEXT (solo q ≥ 6 chars para evitar queries masivos) ─
    if q_clean and len(q_clean) >= 6:
        tokens = [t for t in q_clean.split() if len(t) >= 3]
        if tokens:
            ft_query = " ".join(f"+{t}*" for t in tokens)
            params3 = {**params, "ft_q": ft_query}
            sql3 = text(f"""
                SELECT id FROM monza_partes_catalogo
                WHERE disponible = 1
                  AND MATCH(sku, descripcion, marca) AGAINST(:ft_q IN BOOLEAN MODE)
                  {extras_sql}
                ORDER BY MATCH(sku, descripcion, marca) AGAINST(:ft_q IN BOOLEAN MODE) DESC
                LIMIT :fetch_limit OFFSET :offset
            """)
            try:
                rows3 = db.execute(sql3, params3).fetchall()
                ids3 = [r[0] for r in rows3[:page_size]]
                has_more3 = len(rows3) > page_size
                if ids3:
                    return _build_response(db, ids3, has_more3, page, page_size)
            except Exception:
                pass  # FULLTEXT no disponible, caer al fallback

    # ── Estrategia 4: Fallback LIKE %q% (lento pero completo) ────────────────
    if q_clean:
        q_like = f"%{q_clean}%"
        query = (
            db.query(MonzaParteCatalogo)
            .options(joinedload(MonzaParteCatalogo.lista).joinedload(MonzaListaPrecios.proveedor))
            .filter(MonzaParteCatalogo.disponible == True)  # noqa: E712
            .filter(
                or_(
                    MonzaParteCatalogo.sku.ilike(q_like),
                    MonzaParteCatalogo.marca.ilike(q_like),
                )
            )
        )
        if lista_id:
            query = query.filter(MonzaParteCatalogo.lista_id == lista_id)
        if calidad:
            query = query.filter(MonzaParteCatalogo.calidad == calidad.upper())
        if marca:
            query = query.filter(MonzaParteCatalogo.marca.ilike(f"{marca}%"))

        parts = query.order_by(MonzaParteCatalogo.sku).offset(offset).limit(page_size + 1).all()
        has_more = len(parts) > page_size
        parts = parts[:page_size]
        return {
            "total": len(parts) + (1 if has_more else 0),
            "has_more": has_more,
            "page": page,
            "page_size": page_size,
            "items": [_part_dict(p) for p in parts],
        }

    # ── Sin query, solo filtros ───────────────────────────────────────────────
    query = (
        db.query(MonzaParteCatalogo)
        .options(joinedload(MonzaParteCatalogo.lista).joinedload(MonzaListaPrecios.proveedor))
        .filter(MonzaParteCatalogo.disponible == True)  # noqa: E712
    )
    if lista_id:
        query = query.filter(MonzaParteCatalogo.lista_id == lista_id)
    if calidad:
        query = query.filter(MonzaParteCatalogo.calidad == calidad.upper())
    if marca:
        query = query.filter(MonzaParteCatalogo.marca.ilike(f"{marca}%"))

    parts = query.order_by(MonzaParteCatalogo.sku).offset(offset).limit(page_size + 1).all()
    has_more = len(parts) > page_size
    parts = parts[:page_size]
    return {
        "total": len(parts) + (1 if has_more else 0),
        "has_more": has_more,
        "page": page,
        "page_size": page_size,
        "items": [_part_dict(p) for p in parts],
    }


# ── Detalle de parte ──────────────────────────────────────────────────────────

@router.get("/{part_id}")
def get_part(part_id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Detalle completo de una parte del catálogo."""
    part = (
        db.query(MonzaParteCatalogo)
        .options(joinedload(MonzaParteCatalogo.lista).joinedload(MonzaListaPrecios.proveedor))
        .filter(MonzaParteCatalogo.id == part_id)
        .first()
    )
    if not part:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Parte no encontrada")
    return _part_dict(part)


# ── Markups del cliente ───────────────────────────────────────────────────────

@router.get("/markups/{cliente_id}")
def get_markups(cliente_id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    """Devuelve la matriz de markup para un cliente dado."""
    from monza_models import MonzaMarkup, MonzaCliente
    cliente = db.query(MonzaCliente).filter(MonzaCliente.id == cliente_id).first()
    if not cliente:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Cliente no encontrado")

    markups = db.query(MonzaMarkup).filter(MonzaMarkup.cliente_id == cliente_id).all()

    defaults = {"genuino": 0.13, "oem": 0.25, "aftermarket": 0.40}
    result = {**defaults}
    for m in markups:
        result[m.calidad] = float(m.valor)

    return {
        "cliente_id": cliente_id,
        "cliente_nombre": cliente.nombre,
        "markups": result,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_response(db: Session, ids: list[int], has_more: bool, page: int, page_size: int) -> dict:
    """Carga partes por IDs y construye la respuesta."""
    parts_map = {
        p.id: p
        for p in db.query(MonzaParteCatalogo)
        .options(joinedload(MonzaParteCatalogo.lista).joinedload(MonzaListaPrecios.proveedor))
        .filter(MonzaParteCatalogo.id.in_(ids))
        .all()
    }
    parts = [parts_map[i] for i in ids if i in parts_map]
    return {
        "total": len(ids) + (1 if has_more else 0),
        "has_more": has_more,
        "page": page,
        "page_size": page_size,
        "items": [_part_dict(p) for p in parts],
    }


def _part_dict(p: MonzaParteCatalogo) -> dict:
    lista = p.lista
    return {
        "id": p.id,
        "sku": p.sku,
        "descripcion": p.descripcion,
        "marca": p.marca,
        "calidad": p.calidad,
        "costo": float(p.costo),
        "moneda": p.moneda,
        "peso_real_kg": float(p.peso_real_kg) if p.peso_real_kg else None,
        "peso_vol_kg": float(p.peso_vol_kg) if p.peso_vol_kg else None,
        "pfand": float(p.pfand) if p.pfand else None,
        "moq": p.moq,
        "nueva_parte_sku": p.nueva_parte_sku,
        "disponible": p.disponible,
        "lista": {
            "id": lista.id,
            "code": lista.code,
            "nombre": lista.nombre,
            "lead_time_dias": lista.lead_time_dias,
            "proveedor_code": lista.proveedor.code if lista.proveedor else None,
        } if lista else None,
    }
