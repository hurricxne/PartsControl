import unicodedata

from fastapi import Request, APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, or_, and_
from sqlalchemy.exc import IntegrityError, OperationalError
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, timedelta

from database import get_db
from auth import get_current_user
from empresa_guard import require_empresa
from monza_fechas import hoy_chile, inicio_mes_utc, rango_utc
from monza_correlativos import agregar_lead_con_numero, reintentar_carrera
from monza_rut import buscar_ficha_por_rut, parece_rut, rut_identidad, rut_norm_py, rut_norm_sql
from monza_telefono import (buscar_ficha_por_telefono, telefono_identidad,
                            telefono_norm_py, telefono_norm_sql)
from monza_models import (
    MonzaLead, MonzaLeadItem, MonzaLeadActividad,
    MonzaProximoPaso, MonzaCliente, MonzaCotizacion, MonzaAsesor
)
from models.models import User

# CANDADO DE EMPRESA a nivel de ROUTER (2026-08-22, revierte el aplazamiento del dueño).
# El CRM de MonzaParts —leads, calculadora, ventas y las fichas de cliente— estaba
# abierto a cualquier usuario autenticado, incluidos los de minería, mientras Despachos,
# Bodega y el PATCH de Cotizaciones ya lo tenían candado desde la auditoría F6 sin un
# solo bloqueo reportado. Se canda el router COMPLETO (lecturas incluidas): los datos de
# un cliente Monza —su RUT receptor del DTE, su LTV, sus teléfonos— no son públicos entre
# marcas, y candar solo las escrituras deja la lectura como puerta del costado.
router = APIRouter(
    prefix="/api/monza/leads",
    tags=["monza-leads"],
    dependencies=[Depends(require_empresa("automotriz"))],
)

# Una venta CERRADA sigue siendo venta después de despacharse: sin 'despachado', los KPIs
# restaban la venta el día que salía a reparto (y la tasa de cierre bajaba sola). Espejo
# del par de monza_contabilidad/router.py (ESTADOS_VENTA) y de monza_router_ventas.py.
ESTADOS_VENTA = ("vendida", "despachado")


# ── Schemas ──────────────────────────────────────────────────────────────────

class ClienteQuick(BaseModel):
    nombre: str
    telefono: Optional[str] = None
    email: Optional[str] = None
    rut: Optional[str] = None

class ItemUpdate(BaseModel):
    descripcion: Optional[str] = None
    numero_parte: Optional[str] = None
    marca: Optional[str] = None
    procedencia: Optional[str] = None
    calidad: Optional[str] = None
    cantidad: Optional[int] = None
    plazo_entrega: Optional[str] = None

class ItemIn(BaseModel):
    descripcion: str
    numero_parte: Optional[str] = None
    marca: Optional[str] = None
    procedencia: Optional[str] = None
    calidad: str = "sin_calificar"
    cantidad: int = 1
    plazo_entrega: Optional[str] = None

class LeadCreate(BaseModel):
    canal_origen: str = "WhatsApp"
    cliente_id: Optional[int] = None
    cliente: Optional[ClienteQuick] = None  # crear cliente al vuelo
    vehiculo: Optional[str] = None
    marca: Optional[str] = None
    modelo: Optional[str] = None
    anio: Optional[str] = None
    vin: Optional[str] = None
    linea: Optional[str] = None
    comentario: Optional[str] = None
    items: List[ItemIn] = []

class LeadUpdate(BaseModel):
    estado: Optional[str] = None
    vehiculo: Optional[str] = None
    vin: Optional[str] = None
    canal_origen: Optional[str] = None
    asesor_id: Optional[int] = None
    linea: Optional[str] = None
    comentario: Optional[str] = None

class ProximoPasoIn(BaseModel):
    tipo: str  # llamada/whatsapp/email/visita
    cuando: Optional[datetime] = None
    asesor_id: Optional[int] = None

class ActividadIn(BaseModel):
    tipo: str  # nota/llamada/whatsapp/email/visita
    descripcion: str

class ClienteUpdate(BaseModel):
    nombre: Optional[str] = None
    rut: Optional[str] = None
    telefono: Optional[str] = None
    email: Optional[str] = None
    vehiculos: Optional[List[dict]] = None
    etiquetas: Optional[List[str]] = None
    # VINCULAR cliente a un lead huérfano (arreglos del equipo 2026-08-21, D4): solo se
    # acepta cuando el lead NO tiene cliente — un lead con cliente jamás se re-vincula
    # por acá (mover la historia de un cliente a otro no es "completar datos"). Con
    # exclude_none, mandar solo cliente_id no toca ningún campo de la ficha.
    cliente_id: Optional[int] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _escapar_like(termino: str) -> str:
    """Neutraliza los comodines de LIKE en lo que tecleó el operador.

    `%` y `_` son comodines de SQL: sin escaparlos, un `q` de «%» hacía que el filtro
    coincidiera con TODO —el buscador encendido devolvía el universo— y «a_c» encontraba
    «abc». El operador que teclea un `%` está buscando ese carácter, no pidiendo un
    comodín. El `\\` va primero: si se escapara al final, se duplicarían los que este
    mismo helper acaba de agregar.
    """
    return (termino or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")




def _get_asesor_id(db: Session, user_id: int, con_fallback: bool = True) -> int | None:
    """
    Busca el MonzaAsesor asociado al usuario logueado.
    Intenta match por user_id, luego por email, luego devuelve el primero activo.

    `con_fallback=False` OMITE ese último recurso. El fallback existe para ASIGNAR un
    lead nuevo (mejor un dueño aproximado que ninguno), pero para FILTRAR «mis leads»
    le mostraría a un usuario sin asesor los leads de un tercero: ahí conviene devolver
    None y que quien llama muestre una lista vacía. Los dos llamadores que asignan
    (create_lead y agendar_paso) quedan intactos con el default.
    """
    from models.models import User as _User
    asesor = db.query(MonzaAsesor).filter(MonzaAsesor.user_id == user_id).first()
    if asesor:
        return asesor.id
    # Intentar match por email del user
    user = db.query(_User).filter(_User.id == user_id).first()
    if user:
        asesor = db.query(MonzaAsesor).filter(
            MonzaAsesor.email == user.email
        ).first()
        if asesor:
            return asesor.id
    if not con_fallback:
        return None
    # Fallback: primer asesor activo
    asesor = db.query(MonzaAsesor).filter(MonzaAsesor.activo == True).first()  # noqa: E712
    return asesor.id if asesor else None


def _lead_dict(lead: MonzaLead) -> dict:
    asesor_nombre = None
    if lead.asesor:
        asesor_nombre = lead.asesor.nombre  # MonzaAsesor.nombre es el nombre real

    items_count = len(lead.items)
    # Total estimado = suma de precios con precio_clp
    total = sum(
        (it.precio_clp or 0) * it.cantidad for it in lead.items if it.precio_clp
    )

    # Próximo paso próximo
    proximos = [p for p in lead.proximos_pasos if not p.completado]
    proximo = None
    if proximos:
        prox = sorted(proximos, key=lambda p: p.cuando or datetime.max)[0]
        proximo = {
            "tipo": prox.tipo,
            "cuando": prox.cuando.isoformat() if prox.cuando else None,
        }

    # Días sin contactar. Las fechas se tratan como OPCIONALES aunque el modelo tenga
    # default: `migrate_business_data.py` inserta lo que devuelve `parse_dt`, que ante
    # una fecha ilegible devuelve None, y las filas migradas desde Postgres pueden traer
    # NULL. Como `_lead_dict` corre para CADA fila de la página, una sola fila mala
    # tumbaba la respuesta ENTERA con un 500 — y como el orden es `fecha_creacion DESC`
    # y MySQL manda los NULL al final, esas filas caen en las ÚLTIMAS páginas: la
    # pantalla andaba en la página 1 y se caía al llegar a los leads viejos. Es
    # exactamente el síntoma que reportó el dueño («solo veo los primeros»).
    delta = (datetime.utcnow() - lead.fecha_actualizacion).days if lead.fecha_actualizacion else 0

    return {
        "id": lead.id,
        "numero": lead.numero,
        "estado": lead.estado,
        "canal_origen": lead.canal_origen,
        "vehiculo": lead.vehiculo,
        "marca": lead.marca,
        "modelo": lead.modelo,
        "anio": lead.anio,
        "vin": lead.vin,
        "linea": lead.linea,
        "comentario": lead.comentario,
        "total_estimado": total,
        "items_count": items_count,
        "asesor_id": lead.asesor_id,
        "asesor": asesor_nombre,
        "proximo_paso": proximo,
        "sin_contactar_dias": delta,
        # Flete aéreo elegido para ESTA cotización (uno solo para todos sus ítems).
        # NULL = nunca se eligió y manda la configuración global, que es como se
        # comportaba antes de que la moneda del flete fuera seleccionable.
        "moneda_tarifa": lead.moneda_tarifa,
        "tarifa_aerea": lead.tarifa_aerea,
        "fecha_creacion": lead.fecha_creacion.isoformat() if lead.fecha_creacion else None,
        "fecha_actualizacion": lead.fecha_actualizacion.isoformat() if lead.fecha_actualizacion else None,
        "cliente": _cliente_dict(lead.cliente) if lead.cliente else None,
    }


def _cliente_dict(c: MonzaCliente) -> dict:
    return {
        "id": c.id,
        "nombre": c.nombre,
        "rut": c.rut,
        "telefono": c.telefono,
        "email": c.email,
        "vehiculos": c.vehiculos or [],
        "etiquetas": c.etiquetas or [],
        "ltv": c.ltv or 0,
        "leads_total": c.leads_total or 0,
        "vendidos_total": c.vendidos_total or 0,
        "fecha_creacion": c.fecha_creacion.isoformat() if c.fecha_creacion else None,
    }


def _item_dict(it: MonzaLeadItem) -> dict:
    return {
        "id": it.id,
        "descripcion": it.descripcion,
        "numero_parte": it.numero_parte,
        "marca": it.marca,
        "procedencia": it.procedencia,
        "calidad": it.calidad,
        "cantidad": it.cantidad,
        "precio_clp": it.precio_clp,
        "plazo_entrega": it.plazo_entrega,
        # Parámetros con que la calculadora obtuvo el precio. Viajan para que al
        # REABRIRLA se restaure lo que el operador puso (costo en su moneda, peso,
        # margen) en vez del camino de emergencia que mostraba el precio final como si
        # fuera el costo, en CLP. NULL en los ítems anteriores a la migración
        # monza_cotizador_parametros: ahí la pantalla cae al comportamiento viejo.
        "costo": it.costo,
        "moneda": it.moneda,
        "peso_kg": it.peso_kg,
        "markup_pct": it.markup_pct,
        "tc_aplicado": it.tc_aplicado,
        # Estampa del flete de la corrida que calculó ESTE precio (contrato en
        # MonzaLeadItem): con ella la pantalla siembra la calculadora por subconjunto
        # y detecta corridas mixtas. NULL = ítem nunca recalculado desde la estampa.
        "moneda_tarifa": it.moneda_tarifa,
        "tarifa_aerea": it.tarifa_aerea,
    }


# ── KPIs ──────────────────────────────────────────────────────────────────────


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

@router.get("/kpis")
def get_kpis(db: Session = Depends(get_db), _=Depends(get_current_user)):
    # El mes en curso es el de CHILE, no el de UTC: con el corte anterior, las ventas
    # cerradas entre las 21:00 y la medianoche del último día del mes caían en el mes
    # siguiente y el tablero del mes empezaba con números que nadie reconocía.
    inicio_mes = inicio_mes_utc()

    nuevos_mes = db.query(func.count(MonzaLead.id)).filter(
        MonzaLead.fecha_creacion >= inicio_mes
    ).scalar() or 0

    en_proceso = db.query(func.count(MonzaLead.id)).filter(
        MonzaLead.estado.in_(["pendiente", "en_proceso"])
    ).scalar() or 0

    vendidos_mes = db.query(func.count(MonzaCotizacion.id)).filter(
        MonzaCotizacion.estado.in_(ESTADOS_VENTA),
        MonzaCotizacion.fecha_venta >= inicio_mes,
    ).scalar() or 0

    # TASA DE CIERRE sobre la MISMA cohorte y la misma entidad: de los leads que entraron
    # este mes, cuántos ya se ganaron. Antes dividía VENTAS del mes (que en su mayoría
    # vienen de leads viejos — el ciclo de importación dura semanas) por LEADS NUEVOS del
    # mes, dos universos distintos: una semana sin leads nuevos y tres ventas cerradas
    # daba «300%». Y el `else 1` del denominador convertía «no hay con qué medir» en un
    # porcentaje inventado.
    ganados_del_mes = db.query(func.count(MonzaLead.id)).filter(
        MonzaLead.fecha_creacion >= inicio_mes,
        MonzaLead.estado.in_(("vendido", "cerrado")),
    ).scalar() or 0
    # Sin leads del mes no hay tasa que calcular: se devuelve None y la tarjeta muestra
    # «—». Un 0% diría «lo estamos haciendo pésimo» cuando la verdad es «todavía no hay
    # nada que medir».
    tasa = round((ganados_del_mes / nuevos_mes) * 100, 1) if nuevos_mes else None

    sin_contactar = db.query(func.count(MonzaLead.id)).filter(
        MonzaLead.estado.in_(["pendiente", "en_proceso"]),
        MonzaLead.fecha_actualizacion <= datetime.utcnow() - timedelta(days=3),
    ).scalar() or 0

    # Total cotizado del mes
    desde = inicio_mes
    total_cotizado = db.query(func.sum(MonzaCotizacion.total_bruto)).filter(
        MonzaCotizacion.fecha_creacion >= desde,
        MonzaCotizacion.estado.in_(ESTADOS_VENTA),
    ).scalar() or 0

    # La PLATA de la tarjeta «Vendidos» tiene que ser la de EXACTAMENTE las mismas ventas
    # que cuenta `vendidos_mes` — mismo corte, por `fecha_venta`. La tarjeta mostraba
    # `total_cotizado_mes`, que es otra cosa (de lo COTIZADO este mes, cuánto se vendió) y
    # corta por `fecha_creacion`: el número y su monto venían de dos universos distintos,
    # así que decía «3 vendidos · Total $476.000» cuando esas tres ventas sumaban
    # $1.904.000. `total_cotizado_mes` NO se toca: lo consume el Dashboard con su propio
    # significado.
    total_vendido = db.query(func.sum(MonzaCotizacion.total_bruto)).filter(
        MonzaCotizacion.estado.in_(ESTADOS_VENTA),
        MonzaCotizacion.fecha_venta >= inicio_mes,
    ).scalar() or 0

    pendientes = db.query(func.count(MonzaLead.id)).filter(
        MonzaLead.estado == "pendiente"
    ).scalar() or 0
    en_trabajo = db.query(func.count(MonzaLead.id)).filter(
        MonzaLead.estado == "en_proceso"
    ).scalar() or 0

    return {
        "nuevos_mes": nuevos_mes,
        "en_proceso": en_proceso,
        "vendidos_mes": vendidos_mes,
        "tasa_cierre_pct": tasa,
        "total_cotizado_mes": total_cotizado,
        # Par honesto con `vendidos_mes`: mismas ventas, mismo corte.
        "total_vendido_mes": total_vendido,
        "sin_contactar_3d": sin_contactar,
        "pendientes": pendientes,
        "en_trabajo": en_trabajo,
    }


# ── List leads ────────────────────────────────────────────────────────────────

@router.get("")
def list_leads(
    q: Optional[str] = Query(None),
    estado: Optional[str] = Query(None),
    asesor_id: Optional[int] = Query(None),
    etiqueta: Optional[str] = Query(None),
    solo_mios: bool = Query(False),
    sin_contactar: bool = Query(False),
    desde: Optional[str] = Query(None),
    hasta: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    query = (
        db.query(MonzaLead)
        .options(
            joinedload(MonzaLead.cliente),
            joinedload(MonzaLead.asesor),
            joinedload(MonzaLead.items),
            joinedload(MonzaLead.proximos_pasos),
        )
    )

    if q:
        # BUSCAR LEADS PASADOS (arreglo del equipo 2026-08-22): el placeholder de la
        # pantalla prometía «cliente, teléfono, VIN, N° parte, COT» y el filtro solo
        # miraba número/nombre/teléfono/vehículo — el vendedor que buscaba por el dato
        # que tenía a mano (el VIN del auto, la parte, el N° de cotización) recibía
        # «no hay leads» y concluía que el lead viejo se había perdido.
        #
        # Los dos últimos criterios van por EXISTS (`.any()`), no por JOIN: un lead con
        # dos ítems que calzan aparecería DOS veces con un join, y el `total` de la
        # paginación contaría filas repetidas.
        # `%` y `_` son COMODINES de LIKE: sin escaparlos, buscar «%» traía el universo
        # completo con el buscador encendido (y «_» hacía de comodín de un carácter).
        # Se escapan con `\` y se declara el ESCAPE en cada cláusula.
        patron = f"%{_escapar_like(q)}%"
        criterios = [
            MonzaLead.numero.ilike(patron, escape="\\"),
            MonzaCliente.nombre.ilike(patron, escape="\\"),
            MonzaCliente.telefono.ilike(patron, escape="\\"),
            MonzaLead.vehiculo.ilike(patron, escape="\\"),
            MonzaLead.vin.ilike(patron, escape="\\"),
            MonzaCliente.rut.ilike(patron, escape="\\"),
            # El repuesto se busca por N° de parte Y por DESCRIPCIÓN: los leads que
            # entran solos por el bridge (Trek / web) llegan con la descripción escrita
            # y `numero_parte` en NULL — para ESOS leads, que son la mayoría, buscar por
            # repuesto no encontraba nada. Las dos condiciones van DENTRO del mismo
            # EXISTS: dos subconsultas separadas darían el mismo resultado pero un JOIN
            # duplicaría filas e inflaría el `total`.
            MonzaLead.items.any(or_(
                MonzaLeadItem.numero_parte.ilike(patron, escape="\\"),
                MonzaLeadItem.descripcion.ilike(patron, escape="\\"),
            )),
            MonzaLead.cotizaciones.any(MonzaCotizacion.numero.ilike(patron, escape="\\")),
            # El bridge entierra la PATENTE en el comentario del lead: es el otro dato
            # que el vendedor tiene a mano cuando el cliente llama por su auto.
            MonzaLead.comentario.ilike(patron, escape="\\"),
        ]
        # RUT tecleado en OTRO formato que el guardado ('76000000-0' vs '76.000.000-0'):
        # se compara normalizando AMBOS lados. Solo cuando el término parece un RUT, o
        # buscar «MARIA» activaría una rama que no tiene nada que ver (ver monza_rut).
        if parece_rut(q):
            criterios.append(rut_norm_sql(MonzaCliente.rut).like(
                f"%{_escapar_like(rut_norm_py(q))}%", escape="\\"))
        # TELÉFONO tecleado en otro formato que el guardado ('+56 9 8887 7766' vs
        # '988877766'): mismo tratamiento bilateral que el RUT. El vendedor copia el
        # número desde WhatsApp y la columna cruda no calzaba, así que veía «no hay
        # leads» y concluía que el lead viejo se había perdido — el reclamo original.
        # `telefono_identidad` exige 8+ dígitos: sin ese piso, teclear «22» encendería
        # la rama y traería medio universo (ver monza_telefono).
        llave_tel = telefono_identidad(q)
        if llave_tel:
            criterios.append(telefono_norm_sql(MonzaCliente.telefono).like(
                f"%{_escapar_like(llave_tel)}%", escape="\\"))
        query = query.join(MonzaLead.cliente, isouter=True).filter(or_(*criterios))

    if estado and estado != "todos":
        # 'vendido' y 'cerrado' son el MISMO estado de negocio con dos nombres
        # históricos: el asesor marca 'vendido' a mano y el despacho escribe 'cerrado'.
        # La pantalla ya los pinta con el mismo chip «Cerrado / Ganado»; sin este par,
        # filtrar por vendido escondía justamente los leads ganados que más se
        # re-consultan ("¿qué le vendimos a este cliente?").
        if estado == "vendido":
            query = query.filter(MonzaLead.estado.in_(("vendido", "cerrado")))
        else:
            query = query.filter(MonzaLead.estado == estado)

    if asesor_id:
        query = query.filter(MonzaLead.asesor_id == asesor_id)

    if solo_mios:
        # «Míos» comparaba MonzaLead.asesor_id (id de monza_asesores) contra
        # current_user.id (id de users): DOS espacios de id distintos. El checkbox
        # devolvía los leads del asesor que por casualidad tuviera el mismo número que
        # tu usuario — o sea, escondía los tuyos y podía mostrarte los de un colega.
        # Sin fallback A PROPÓSITO: el «primer asesor activo» de _get_asesor_id sirve
        # para ASIGNAR un lead nuevo, pero acá mostraría los leads de un tercero a
        # alguien que no es asesor. Sin asesor ⇒ lista vacía (falla cerrado).
        mi_asesor = _get_asesor_id(db, current_user.id, con_fallback=False)
        if mi_asesor is None:
            return {"total": 0, "page": page, "page_size": page_size, "items": []}
        query = query.filter(MonzaLead.asesor_id == mi_asesor)

    if sin_contactar:
        cutoff = datetime.utcnow() - timedelta(days=3)
        query = query.filter(
            MonzaLead.estado.in_(["pendiente", "en_proceso"]),
            MonzaLead.fecha_actualizacion <= cutoff,
        )

    # El operador digita DÍAS DE CHILE y la columna guarda UTC: la conversión y el
    # rango SEMIABIERTO viven en monza_fechas (una sola vara para todas las pestañas).
    # Antes, `<= fromisoformat(hasta)` comparaba contra la MEDIANOCHE del día pedido y
    # escondía el día entero — «hasta hoy» no mostraba nada de hoy.
    desde_utc, hasta_utc = rango_utc(desde, hasta)
    if desde_utc:
        query = query.filter(MonzaLead.fecha_creacion >= desde_utc)
    if hasta_utc:
        query = query.filter(MonzaLead.fecha_creacion < hasta_utc)

    total = query.count()
    # Desempate por id: fecha_creacion tiene precisión de SEGUNDO y el bridge de leads
    # inserta en ráfaga, así que sin segundo criterio el orden entre filas empatadas no
    # es estable y la paginación podía repetir un lead en dos páginas o saltárselo.
    leads = (query.order_by(MonzaLead.fecha_creacion.desc(), MonzaLead.id.desc())
             .offset((page - 1) * page_size).limit(page_size).all())

    # Insignia de motivo: SOLO sobre la página ya paginada (ver _match_leads).
    motivos = _match_leads(db, leads, q) if q and q.strip() else {}

    def _con_match(l):
        d = _lead_dict(l)
        # Clave ADITIVA: los consumidores viejos la ignoran sin romperse.
        d["match"] = motivos.get(l.id, [])
        return d

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_con_match(l) for l in leads],
    }



# ── Insignia de MOTIVO del calce (spec de buscadores 2026-08-05) ────────────────────
# El buscador de leads encuentra por N° de parte, DESCRIPCIÓN del repuesto, N° de
# cotización, VIN, RUT y la patente enterrada en el comentario — y NINGUNO de esos datos
# se ve en la tabla. El vendedor recibía ocho filas de aspecto idéntico y tenía que
# abrirlas una por una para descubrir cuál era: la capacidad de búsqueda quedaba
# construida y prácticamente inusable. Esta insignia dice POR QUÉ calzó cada fila, igual
# que ya lo hace el buscador de Despachos (_match_ventas).
_ETIQUETA_MATCH = {
    "numero": "N° lead", "cliente": "cliente", "telefono": "teléfono",
    "vehiculo": "vehículo", "vin": "VIN", "rut": "RUT",
    "numero_parte": "N° parte", "repuesto": "repuesto",
    "cotizacion": "cotización", "comentario": "comentario",
}


def _plegar(texto) -> str:
    """Minúsculas y SIN acentos. La comparación tiene que doler lo mismo que el SQL.

    El filtro usa `ilike` sobre columnas con collation utf8mb4_unicode_ci, que ignora
    mayúsculas Y acentos: buscar «maria» TRAE a «María Pérez». La insignia comparaba con
    `in` sobre `.lower()`, que sí distingue tildes, así que esa fila llegaba SIN motivo —
    y una insignia que a veces no aparece es peor que ninguna, porque el vendedor deja de
    confiar en ella. En un CRM chileno los nombres con tilde y con ñ son la norma.
    """
    return "".join(c for c in unicodedata.normalize("NFD", str(texto or "").lower())
                   if not unicodedata.combining(c))


def _match_leads(db: Session, leads, q: str) -> dict:
    """{lead_id: [{campo, etiqueta, valor}]} para una PÁGINA de leads.

    Se calcula SOLO sobre la página ya paginada (≤ page_size filas) y los hijos se
    precargan EN LOTE sobre los ids de esa página — nunca una consulta por lead, que es
    el N+1 que el contrato de la casa prohíbe.

    El calce se decide en Python con las MISMAS normalizaciones que usó el SQL (RUT y
    teléfono canónicos), porque si la insignia usara otro criterio podría no explicar
    una fila que el filtro sí trajo — y una insignia que a veces no aparece es peor que
    ninguna: el vendedor deja de confiar en ella.
    """
    if not q or not leads:
        return {}
    termino = _plegar(q.strip())
    llave_rut = rut_identidad(q) or rut_norm_py(q)
    llave_tel = telefono_identidad(q)
    ids = [l.id for l in leads]

    items_por_lead: dict = {}
    for it in db.query(MonzaLeadItem).filter(MonzaLeadItem.lead_id.in_(ids)).all():
        items_por_lead.setdefault(it.lead_id, []).append(it)
    cots_por_lead: dict = {}
    from monza_models import MonzaCotizacion as _Cot
    for c in db.query(_Cot).filter(_Cot.lead_id.in_(ids)).all():
        cots_por_lead.setdefault(c.lead_id, []).append(c)

    def _calza(campo: str, valor) -> bool:
        if not valor:
            return False
        texto = _plegar(valor)
        if termino in texto:
            return True
        if campo == "rut" and llave_rut:
            return llave_rut.lower() in rut_norm_py(valor).lower()
        if campo == "telefono" and llave_tel:
            return llave_tel in telefono_norm_py(valor)
        return False

    salida: dict = {}
    for l in leads:
        cli = l.cliente
        candidatos = [
            ("numero", l.numero), ("vehiculo", l.vehiculo), ("vin", l.vin),
            ("comentario", l.comentario),
            ("cliente", cli.nombre if cli else None),
            ("telefono", cli.telefono if cli else None),
            ("rut", cli.rut if cli else None),
        ]
        for it in items_por_lead.get(l.id, []):
            candidatos += [("numero_parte", it.numero_parte), ("repuesto", it.descripcion)]
        for c in cots_por_lead.get(l.id, []):
            candidatos.append(("cotizacion", c.numero))

        vistos, motivos = set(), []
        for campo, valor in candidatos:
            if campo in vistos or not _calza(campo, valor):
                continue
            vistos.add(campo)
            motivos.append({"campo": campo,
                            "etiqueta": _ETIQUETA_MATCH.get(campo, campo),
                            "valor": str(valor)[:120]})
        if motivos:
            salida[l.id] = motivos
    return salida

# ── Create lead ───────────────────────────────────────────────────────────────

@router.post("", status_code=201)
def create_lead(body: LeadCreate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """Crea un lead, reintentando si dos creaciones simultáneas se estorban.

    El reintento envuelve la operación ENTERA y no solo el correlativo, porque el choque
    que más ocurre no es el del número: es el DEADLOCK entre dos leads DEL MISMO CLIENTE
    (uno toma el lock del índice de `numero` y el otro el de la fila del cliente, en
    orden cruzado). Un deadlock deshace la transacción completa, así que no hay nada que
    salvar desde adentro — hay que rehacerla. Patrón espejo de `crear_despacho`.

    Medido con 6 creaciones REALMENTE simultáneas del mismo cliente: sin esto sobrevivían
    2 y los otros 4 leads se perdían con un 500 mientras el vendedor los estaba tipeando.
    """
    return reintentar_carrera(db, lambda: _crear_lead_tx(body, db, current_user), que="leads")


def _crear_lead_tx(body: LeadCreate, db: Session, current_user):
    # Resolver cliente
    cliente_id = body.cliente_id
    # La pantalla avisa «se vinculó al cliente existente X» en vez de decir que creó
    # uno nuevo: sin este dato el operador no tenía forma de notar la fusión.
    cliente_reutilizado = False
    if not cliente_id and body.cliente:
        # DEDUPE, en orden de fuerza de la evidencia: primero el RUT (identifica a un
        # contribuyente), después el teléfono (un número lo comparten la recepción, el
        # gestor y el taller). El orden estaba INVERTIDO y el teléfono ganaba: con la
        # ficha equivocada elegida, el RUT que el operador acababa de teclear ni siquiera
        # se guardaba. Misma puerta y mismo orden que POST /clientes — cuando cada camino
        # decidía por su cuenta, el mismo cliente terminaba con dos fichas según por dónde
        # entrara.
        cliente = buscar_ficha_por_rut(db, MonzaCliente, body.cliente.rut)
        if not cliente and body.cliente.telefono:
            cliente = buscar_ficha_por_telefono(
                db, MonzaCliente, body.cliente.telefono, body.cliente.rut
            )
        if cliente:
            # COMPLETAR lo que falta, jamás pisar lo que ya está (regla de `create_cliente`).
            # Sin esto, entrar por teléfono a una ficha vieja tiraba a la basura el RUT y
            # el email recién tecleados: el operador los escribía y no quedaban en ninguna
            # parte, y el módulo DTE seguía pidiendo «completa el RUT del cliente».
            if rut_identidad(body.cliente.rut) and not cliente.rut:
                cliente.rut = body.cliente.rut
            if body.cliente.telefono and not cliente.telefono:
                cliente.telefono = body.cliente.telefono
            if body.cliente.email and not cliente.email:
                cliente.email = body.cliente.email
            cliente_reutilizado = True
        else:
            cliente = MonzaCliente(
                nombre=body.cliente.nombre,
                telefono=body.cliente.telefono,
                email=body.cliente.email,
                rut=body.cliente.rut,
            )
            db.add(cliente)
            db.flush()
        cliente_id = cliente.id

    lead = MonzaLead(
        cliente_id=cliente_id,
        vehiculo=(f"{body.marca} {body.modelo}".strip() if (body.marca or body.modelo) else body.vehiculo),
        marca=body.marca,
        modelo=body.modelo,
        anio=body.anio,
        vin=body.vin,
        canal_origen=body.canal_origen,
        asesor_id=_get_asesor_id(db, current_user.id),
        estado="pendiente",
        comentario=body.comentario,
        linea=body.linea,
    )
    # El correlativo se asigna acá dentro, con reintento ante creaciones simultáneas
    # (ver monza_correlativos): dos vendedores a la vez —o un vendedor y el webhook de
    # Nexor— calculaban el mismo número y el segundo se caía con un 500.
    agregar_lead_con_numero(db, lead)

    for it in body.items:
        if it.descripcion.strip():
            db.add(MonzaLeadItem(lead_id=lead.id, **it.model_dump()))

    # Actividad inicial
    asesor_nombre = current_user.email.split("@")[0].title()
    db.add(MonzaLeadActividad(
        lead_id=lead.id,
        tipo="lead_creado",
        descripcion=f"Lead creado por {asesor_nombre}",
        usuario=asesor_nombre,
        usuario_id=current_user.id,
    ))

    # Actualizar stats cliente
    if cliente_id:
        # Lock: leads_total/vendidos_total son lectura-modificación-escritura y dos
        # creaciones simultáneas del mismo cliente se pisaban el contador.
        c = (db.query(MonzaCliente).filter(MonzaCliente.id == cliente_id)
             .populate_existing().with_for_update().first())
        if c:
            c.leads_total = (c.leads_total or 0) + 1

    # El log va DENTRO de la transacción, antes del commit. Cuando iba después, quedaba
    # bajo el bucle de reintento pero FUERA de lo que el rollback deshace: un error
    # transitorio al escribirlo hacía que el reintento rehiciera la creación entera —
    # que ya estaba guardada— y la petición terminaba creando DOS leads. Es el mismo
    # criterio que ya usan crear/cerrar/anular despacho: un solo commit con el log
    # adentro. `lead.id` y `lead.numero` ya existen acá (agregar_lead_con_numero hace
    # flush), así que no hace falta esperar al commit para registrarlos.
    from monza_models import MonzaLog
    db.add(MonzaLog(user_email=current_user.email, accion="CREATE", entidad="lead",
                    entidad_id=lead.id, entidad_ref=lead.numero,
                    detalle=f"Lead {lead.numero} creado"))
    db.commit()
    db.refresh(lead)
    # `cliente_reutilizado` acompaña al lead para que la pantalla pueda decir a qué ficha
    # quedó vinculado: la fusión por teléfono es correcta cuando es el mismo cliente, pero
    # el operador tiene que ENTERARSE de que no se creó la ficha que él escribió.
    return {**_lead_dict(lead), "cliente_reutilizado": cliente_reutilizado}


# ── Get lead detail ───────────────────────────────────────────────────────────

@router.get("/{lead_id}")
def get_lead(lead_id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    lead = (
        db.query(MonzaLead)
        .options(
            joinedload(MonzaLead.cliente),
            joinedload(MonzaLead.asesor),
            joinedload(MonzaLead.items),
            joinedload(MonzaLead.actividades),
            joinedload(MonzaLead.proximos_pasos).joinedload(MonzaProximoPaso.asesor),
            joinedload(MonzaLead.cotizaciones),
        )
        .filter(MonzaLead.id == lead_id)
        .first()
    )
    if not lead:
        raise HTTPException(status_code=404, detail="Lead no encontrado")

    data = _lead_dict(lead)
    data["items"] = [_item_dict(it) for it in lead.items]
    # Las fechas se tratan como OPCIONALES, igual que en `_lead_dict`: la migración desde
    # Postgres inserta lo que devuelve `parse_dt`, que ante una fecha ilegible da None.
    # Sin esto, UNA actividad con fecha nula reventaba el detalle entero («'<' not
    # supported between datetime and NoneType») y ese lead quedaba imposible de abrir
    # para siempre: el vendedor lo veía en la tabla, hacía clic y no pasaba nada. El
    # arreglo de la LISTA no alcanzaba a este camino.
    _sin_fecha = datetime.min
    data["actividades"] = [
        {
            "id": a.id,
            "tipo": a.tipo,
            "descripcion": a.descripcion,
            "usuario": a.usuario,
            "fecha": a.fecha.isoformat() if a.fecha else None,
        }
        # Las de fecha desconocida van al final (datetime.min con reverse=True), que es
        # donde el operador espera lo más viejo.
        for a in sorted(lead.actividades, key=lambda x: x.fecha or _sin_fecha, reverse=True)
    ]
    data["proximos_pasos"] = [
        {
            "id": p.id,
            "tipo": p.tipo,
            "cuando": p.cuando.isoformat() if p.cuando else None,
            "asesor": p.asesor.nombre if p.asesor else None,
            "completado": p.completado,
        }
        for p in lead.proximos_pasos
    ]
    data["cotizaciones"] = [
        {"id": c.id, "numero": c.numero, "estado": c.estado, "total_bruto": c.total_bruto}
        for c in lead.cotizaciones
    ]
    return data


# ── Update lead ───────────────────────────────────────────────────────────────

@router.patch("/{lead_id}")
def update_lead(lead_id: int, body: LeadUpdate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    # LOCK sobre el LEAD, no solo sobre la ficha del cliente. La evidencia que decide si
    # esta venta ya se contó es `old_estado`, y vive en ESTA fila: sin bloquearla, dos
    # PATCH simultáneos con estado='vendido' leen los dos el mismo estado abierto, los
    # dos pasan el guard y la ficha termina con DOS ventas donde hubo una. El lock del
    # cliente no alcanza — bloquea donde se ESCRIBE, no donde se DECIDE. Reproducido
    # 3 de 3 veces antes de este cambio. (Su gemela `update_cliente`, 200 líneas más
    # abajo, ya lo tenía: es la misma clase de guard sobre la misma tabla.)
    lead = (db.query(MonzaLead).filter(MonzaLead.id == lead_id)
            .populate_existing().with_for_update().first())
    if not lead:
        raise HTTPException(status_code=404, detail="Lead no encontrado")

    old_estado = lead.estado
    # REABRIR un lead cuya venta ya se cerró lo devuelve al embudo: vuelve a contarse
    # como oportunidad abierta, entra en «sin contactar» y reaparece en el tablero del
    # asesor, mientras la venta sigue despachada y facturada. Los dos números que el
    # dueño mira —embudo y ventas— empiezan a contar la misma operación dos veces.
    # INVARIANTE, no par de estados: mientras el lead tenga una venta cerrada, su único
    # destino posible es 'vendido' o 'cerrado'. La versión anterior miraba solo la
    # transición DIRECTA (cerrado → en_proceso) y 'rechazado' servía de trampolín en dos
    # saltos —cerrado → rechazado → en_proceso—, que además son los DOS ÚNICOS botones de
    # estado que la pantalla muestra: el camino más corto, no uno rebuscado.
    if (body.estado and body.estado != old_estado
            and body.estado not in ("vendido", "cerrado")):
        from monza_models import MonzaCotizacion as _Cot
        venta = (db.query(_Cot)
                 .filter(_Cot.lead_id == lead_id, _Cot.estado.in_(("vendida", "despachado")))
                 .first())
        if venta:
            raise HTTPException(
                status_code=409,
                detail=f"El lead {lead.numero} tiene la venta {venta.numero} cerrada: "
                       "reabrirlo lo devolvería al embudo y quedaría contado dos veces. "
                       "Si el cliente vuelve a consultar, crea un lead nuevo.",
            )
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(lead, field, value)
    lead.fecha_actualizacion = datetime.utcnow()

    asesor_nombre = current_user.email.split("@")[0].title()
    if body.estado and body.estado != old_estado:
        db.add(MonzaLeadActividad(
            lead_id=lead.id,
            tipo="cambio_estado",
            descripcion=f"Estado cambiado a '{body.estado}' por {asesor_nombre}",
            usuario=asesor_nombre,
            usuario_id=current_user.id,
        ))
        # `vendidos_total` cuenta VENTAS, no clics: solo suma cuando el lead viene de un
        # estado ABIERTO. Desde que el cierre de la venta marca el lead solo (y el
        # despacho lo deja en 'cerrado'), el asesor que abre un lead cerrado y lo marca
        # «vendido» —lo natural, porque efectivamente se vendió— hacía que la ficha
        # contara DOS ventas donde hubo una. 'vendido' y 'cerrado' son los dos estados
        # donde esa venta YA se contó.
        if body.estado == "vendido" and (old_estado or "pendiente") not in ("vendido", "cerrado"):
            # Lock: leads_total/vendidos_total son lectura-modificación-escritura y dos
            # creaciones simultáneas del mismo cliente se pisaban el contador.
            c = (db.query(MonzaCliente).filter(MonzaCliente.id == lead.cliente_id)
                 .populate_existing().with_for_update().first())
            if c:
                c.vendidos_total = (c.vendidos_total or 0) + 1

    db.commit()
    db.refresh(lead)
    _log(db, current_user.email, "UPDATE", "lead",
         lead.id, lead.numero, f"Estado: {lead.estado}")
    return _lead_dict(lead)


# ── Items ─────────────────────────────────────────────────────────────────────

@router.post("/{lead_id}/items", status_code=201)
def add_item(lead_id: int, body: ItemIn, db: Session = Depends(get_db), _=Depends(get_current_user)):
    lead = db.query(MonzaLead).filter(MonzaLead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead no encontrado")
    item = MonzaLeadItem(lead_id=lead_id, **body.model_dump())
    db.add(item)
    lead.fecha_actualizacion = datetime.utcnow()
    db.commit()
    db.refresh(item)
    return _item_dict(item)


@router.put("/{lead_id}/items/{item_id}")
def update_item(lead_id: int, item_id: int, body: ItemUpdate, db: Session = Depends(get_db), _=Depends(get_current_user)):
    item = db.query(MonzaLeadItem).filter(
        MonzaLeadItem.id == item_id, MonzaLeadItem.lead_id == lead_id
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item no encontrado")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(item, field, value)
    db.commit()
    db.refresh(item)
    return _item_dict(item)


@router.delete("/{lead_id}/items/{item_id}", status_code=204)
def delete_item(lead_id: int, item_id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    item = db.query(MonzaLeadItem).filter(
        MonzaLeadItem.id == item_id, MonzaLeadItem.lead_id == lead_id
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item no encontrado")
    db.delete(item)
    db.commit()


# ── Próximos pasos ────────────────────────────────────────────────────────────

@router.post("/{lead_id}/proximos-pasos", status_code=201)
def agendar_paso(lead_id: int, body: ProximoPasoIn, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    lead = db.query(MonzaLead).filter(MonzaLead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead no encontrado")
    paso = MonzaProximoPaso(
        lead_id=lead_id,
        tipo=body.tipo,
        cuando=body.cuando,
        asesor_id=body.asesor_id or _get_asesor_id(db, current_user.id),
    )
    db.add(paso)
    asesor_nombre = current_user.email.split("@")[0].title()
    cuando_str = body.cuando.strftime("%d/%m/%Y %H:%M") if body.cuando else "sin fecha"
    db.add(MonzaLeadActividad(
        lead_id=lead_id,
        tipo=body.tipo,
        descripcion=f"Próximo paso agendado: {body.tipo} para {cuando_str}",
        usuario=asesor_nombre,
        usuario_id=current_user.id,
    ))
    lead.fecha_actualizacion = datetime.utcnow()
    db.commit()
    db.refresh(paso)
    return {"id": paso.id, "tipo": paso.tipo, "cuando": paso.cuando.isoformat() if paso.cuando else None}


@router.patch("/{lead_id}/proximos-pasos/{paso_id}/completar", status_code=200)
def completar_paso(lead_id: int, paso_id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    paso = db.query(MonzaProximoPaso).filter(
        MonzaProximoPaso.id == paso_id, MonzaProximoPaso.lead_id == lead_id
    ).first()
    if not paso:
        raise HTTPException(status_code=404, detail="Próximo paso no encontrado")
    paso.completado = True
    db.commit()
    return {"ok": True}


# ── Actividades / Notas ───────────────────────────────────────────────────────

@router.post("/{lead_id}/actividades", status_code=201)
def add_actividad(lead_id: int, body: ActividadIn, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    lead = db.query(MonzaLead).filter(MonzaLead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead no encontrado")
    asesor_nombre = current_user.email.split("@")[0].title()
    act = MonzaLeadActividad(
        lead_id=lead_id,
        tipo=body.tipo,
        descripcion=body.descripcion,
        usuario=asesor_nombre,
        usuario_id=current_user.id,
    )
    db.add(act)
    lead.fecha_actualizacion = datetime.utcnow()
    db.commit()
    db.refresh(act)
    return {"id": act.id, "tipo": act.tipo, "descripcion": act.descripcion, "usuario": act.usuario, "fecha": act.fecha.isoformat()}


# ── Clientes ──────────────────────────────────────────────────────────────────

@router.get("/clientes/search")
def search_clientes(q: str = Query(""), db: Session = Depends(get_db), _=Depends(get_current_user)):
    criterios = [
        MonzaCliente.nombre.ilike(f"%{q}%"),
        MonzaCliente.telefono.ilike(f"%{q}%"),
        MonzaCliente.rut.ilike(f"%{q}%"),
        MonzaCliente.email.ilike(f"%{q}%"),
    ]
    # RUT tecleado en otro formato que el guardado: se compara normalizando ambos lados
    # (monza_rut). Importa el doble acá: este buscador alimenta las pre-búsquedas que
    # evitan crear una ficha duplicada — si no encuentra, el operador crea el duplicado.
    if parece_rut(q):
        criterios.append(rut_norm_sql(MonzaCliente.rut).like(f"%{rut_norm_py(q)}%"))
    # Y el teléfono normalizado, por el mismo motivo y con el mismo peso: este buscador
    # alimenta la pre-búsqueda que EVITA crear una ficha duplicada. Si no encuentra al
    # cliente que ya existe porque el número está tipeado distinto, el operador crea el
    # duplicado — y el dedupe del POST ya no puede salvarlo si tampoco hay RUT.
    llave_tel = telefono_identidad(q)
    if llave_tel:
        criterios.append(telefono_norm_sql(MonzaCliente.telefono).like(f"%{llave_tel}%"))
    clientes = db.query(MonzaCliente).filter(or_(*criterios)).limit(20).all()
    return [_cliente_dict(c) for c in clientes]


# Etiquetas humanas para el rastro de la edición (solo los campos que se muestran).
_CLIENTE_CAMPOS_LABEL = {"nombre": "nombre", "rut": "RUT", "telefono": "teléfono", "email": "email"}


@router.patch("/{lead_id}/cliente")
def update_cliente(lead_id: int, body: ClienteUpdate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """Vincula o edita el cliente de un lead, reintentando ante un choque de locks.

    Los `FOR UPDATE` de abajo (sobre el lead y sobre la ficha) abrieron la puerta al
    deadlock 1213: dos operaciones que tocan esas mismas filas en orden distinto se
    bloquean mutuamente y MySQL mata a una. Es la regla de la casa —el mismo bucle que
    ya usan crear despacho, crear embarque y comprar— y sin ella el operador recibía un
    500 en una operación que solo hacía falta repetir.
    """
    # El helper COMPARTIDO, no un bucle a mano: este endpoint hace su commit y después
    # escribe el log (que commitea aparte), y un bucle escrito acá no tendría el candado
    # `after_commit` — un 1213 en ese punto devolvería un 409 falso por una vinculación
    # que SÍ ocurrió, o duplicaría la nota de actividad. Es la misma razón por la que el
    # reintento dejó de escribirse a mano en las otras tres puertas.
    return reintentar_carrera(
        db, lambda: _update_cliente_tx(lead_id, body, db, current_user),
        vueltas=3, que="clientes",
    )


def _update_cliente_tx(lead_id: int, body: ClienteUpdate, db: Session, current_user):
    """Edita la ficha del cliente del lead — o VINCULA una a un lead huérfano.

    OJO con el alcance: la ficha de MonzaCliente es COMPARTIDA por todos los leads,
    ventas y facturas del cliente (el RUT es el receptor del DTE 33). Por eso todo
    cambio deja rastro en la Actividad del lead y en MonzaLog — este endpoint existía
    sin UI y estrena pantalla con los arreglos del equipo 2026-08-21.

    Contrato del body: `cliente_id` presente = SOLO vincular (los campos de ficha que
    viajen junto a él se ignoran — la pantalla manda {cliente_id} a secas); sin
    `cliente_id` = editar la ficha, donde "" limpia un campo y None no lo toca.
    """
    # FOR UPDATE (regla de la casa, espejo del PATCH de cotizaciones): el guard
    # anti-re-vinculación de abajo decide sobre lead.cliente_id, y sin lock dos PATCH
    # simultáneos sobre el mismo lead huérfano pasarían ambos el guard — el segundo
    # re-vincularía la historia, que es exactamente lo que el 409 existe para impedir.
    lead = (db.query(MonzaLead)
            .filter(MonzaLead.id == lead_id)
            .populate_existing()
            .with_for_update()
            .first())
    if not lead:
        raise HTTPException(status_code=404, detail="Lead no encontrado")

    asesor_nombre = current_user.email.split("@")[0].title()

    # ── Vincular (D4): solo para leads SIN cliente ────────────────────────────────
    if body.cliente_id is not None:
        if lead.cliente_id:
            raise HTTPException(
                status_code=409,
                detail="El lead ya tiene un cliente vinculado: no se re-vincula. "
                       "Si la cotización va a otro RUT, elígelo al emitirla («Cotizar a»).",
            )
        # Sexto escritor de la ficha, y el que se había quedado sin lock: más abajo
        # incrementa `leads_total`, que es lectura-modificación-escritura igual que el
        # LTV. Sin él, dos vinculaciones simultáneas se pisaban el contador.
        c = (db.query(MonzaCliente).filter(MonzaCliente.id == body.cliente_id)
             .populate_existing().with_for_update().first())
        if not c:
            raise HTTPException(status_code=404, detail="Cliente no encontrado")
        lead.cliente_id = c.id
        lead.fecha_actualizacion = datetime.utcnow()
        # Mismo contador que create_lead: el cliente gana un lead en su historial.
        c.leads_total = (c.leads_total or 0) + 1
        db.add(MonzaLeadActividad(
            lead_id=lead.id,
            tipo="nota",
            descripcion=f"Cliente «{c.nombre}» vinculado al lead por {asesor_nombre}",
            usuario=asesor_nombre,
            usuario_id=current_user.id,
        ))
        db.commit()
        db.refresh(c)
        _log(db, current_user.email, "UPDATE", "lead", lead.id, lead.numero,
             f"Cliente #{c.id} «{c.nombre}» vinculado al lead {lead.numero}")
        return _cliente_dict(c)

    # ── Edición de la ficha ───────────────────────────────────────────────────────
    if not lead.cliente_id:
        raise HTTPException(status_code=404, detail="El lead no tiene cliente vinculado")
    c = db.query(MonzaCliente).filter(MonzaCliente.id == lead.cliente_id).first()
    # Los campos que de verdad CAMBIAN, para el rastro ("" también cuenta: limpia).
    cambios = body.model_dump(exclude_none=True, exclude={"cliente_id"})
    # El NOMBRE no se limpia: la ficha es compartida y receptora del DTE — una ficha
    # sin nombre queda ilegible en todo el sistema. Cinturón del guard que el modal ya
    # tiene ("" limpia sigue valiendo para rut/teléfono/email).
    if "nombre" in cambios and not (cambios["nombre"] or "").strip():
        raise HTTPException(status_code=422,
                            detail="El nombre del cliente no puede quedar vacío")
    tocados = [
        _CLIENTE_CAMPOS_LABEL[f] for f, v in cambios.items()
        if f in _CLIENTE_CAMPOS_LABEL and (getattr(c, f) or "") != (v or "")
    ]
    for field, value in cambios.items():
        setattr(c, field, value)
    c.fecha_actualizacion = datetime.utcnow()
    if tocados:
        db.add(MonzaLeadActividad(
            lead_id=lead.id,
            tipo="nota",
            descripcion=f"Datos del cliente actualizados por {asesor_nombre}: {', '.join(tocados)}",
            usuario=asesor_nombre,
            usuario_id=current_user.id,
        ))
    db.commit()
    db.refresh(c)
    if tocados:
        _log(db, current_user.email, "UPDATE", "cliente", c.id, c.nombre,
             f"Ficha editada desde el lead {lead.numero}: {', '.join(tocados)}")
    return _cliente_dict(c)


# ── Asesores ──────────────────────────────────────────────────────────────────

@router.get("/asesores/list")
def list_asesores(db: Session = Depends(get_db), _=Depends(get_current_user)):
    asesores = db.query(MonzaAsesor).filter(MonzaAsesor.activo == True).all()  # noqa: E712
    return [{"id": a.id, "nombre": a.nombre, "email": a.email, "slug": a.slug} for a in asesores]


@router.delete("/{lead_id}", status_code=204)
def delete_lead(lead_id: int, db: Session = Depends(get_db), _=Depends(get_current_user)):
    lead = db.query(MonzaLead).filter(MonzaLead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead no encontrado")
    from monza_models import MonzaCotizacion
    # Un lead con VENTA CERRADA no se borra: el borrado desvincula las cotizaciones, así
    # que la venta pierde para siempre de qué consulta nació y ninguna pantalla puede
    # reconstruirlo. El aviso de la pantalla solo decía «no se puede deshacer», sin
    # mencionar que había una venta colgando. Los leads SIN ventas se borran igual que
    # siempre — que es el caso real de los duplicados que esto viene a limpiar.
    vendidas = (db.query(MonzaCotizacion)
                .filter(MonzaCotizacion.lead_id == lead_id,
                        MonzaCotizacion.estado.in_(("vendida", "despachado")))
                .all())
    if vendidas:
        numeros = ", ".join(v.numero for v in vendidas[:3])
        extra = f" y {len(vendidas) - 3} más" if len(vendidas) > 3 else ""
        raise HTTPException(
            status_code=409,
            detail=f"El lead {lead.numero} no se puede eliminar: tiene la venta "
                   f"{numeros}{extra} cerrada, y borrarlo dejaría esa venta sin su origen.",
        )
    # Desasociar cotizaciones (conservarlas, solo romper el vínculo)
    db.query(MonzaCotizacion).filter(MonzaCotizacion.lead_id == lead_id).update({"lead_id": None})
    db.commit()
    _log(db, _.email if hasattr(_, "email") else str(_), "DELETE", "lead",
         lead.id, lead.numero, f"Lead {lead.numero} eliminado")
    db.delete(lead)
    db.commit()
