import os
import io
from datetime import datetime, date
from typing import Optional, List

from fastapi import Request, APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_, func
from sqlalchemy.exc import IntegrityError, OperationalError
from pydantic import BaseModel, Field

from database import get_db
from auth import get_current_user
from empresa_guard import require_empresa
from role_guard import require_rol
from monza_correlativos import siguiente_secuencia, reintentar_carrera
from monza_fechas import hoy_chile, rango_utc
from monza_notif import crear_notif
from monza_models import (
    MonzaCotizacion, MonzaCotizacionItem, MonzaCliente,
    MonzaLead, MonzaLeadItem, MonzaLeadActividad, MonzaConfig,
    MonzaCotizacionCierre,
)

# CANDADO DE EMPRESA a nivel de ROUTER (2026-08-22): el PATCH ya lo tenía en su
# firma, pero el resto del router —incluida la LISTA con nombre, RUT y teléfono de
# cada cliente, y el PDF de la cotización— quedaba legible para un usuario de
# minería. Candar 4 de los 5 routers del CRM y dejar este abierto era cerrar la
# puerta y dejar la ventana. El require_rol del PATCH se conserva en su firma.
router = APIRouter(
    prefix="/api/monza/cotizaciones",
    tags=["monza-cotizaciones"],
    dependencies=[Depends(require_empresa("automotriz"))],
)

RESULTS_DIR = "/var/www/machparts.bigcode.cl/backend/results"

# Estados de cotización desde los que SÍ se puede cerrar la venta (hallazgo A5 de la
# paridad contable). Espejo conceptual del Cierre de Venta de Grupo AM, que solo lista
# las OC con fase_comercial 'ingresada' y estado 'completado' (CierreVentaPage.tsx):
# una cotización RECHAZADA por el cliente, o que nunca se le envió, NO es una venta —
# y cerrarla dispara las compras al proveedor (el cierre pone las líneas en
# 'por_comprar' y habilita Abastecimiento). Hoy el botón de la pantalla se pinta con
# `estado !== "vendida"`, así que un clic mal dado en la fila equivocada compraba.
# 'vendida' está en la lista porque el cierre es IDEMPOTENTE (re-enviar 'vendida'
# edita la OC sin repetir efectos); 'despachado' tiene su propio 409 más arriba.
ESTADOS_QUE_CIERRAN_VENTA = ("enviada", "vendida")


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
    # ID del ítem del LEAD del que nace esta línea. Es un ID, no un valor de plata: con
    # él create_cotizacion lee EN LA BASE la estampa de flete de la corrida que calculó
    # ese precio (monza_lead_items.moneda_tarifa/tarifa_aerea) — la foto sigue saliendo
    # del servidor, jamás del body. None (pantalla vieja / cotización a mano) = la
    # tarifa de cabecera, comportamiento de siempre.
    lead_item_id: Optional[int] = None

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
    oc_fecha: Optional[date] = None
    # VENTA A CLIENTE PARTICULAR (2026-08-08): un consumidor final no emite OC. Con
    # `True`, el cierre usa la COTIZACIÓN como documento de respaldo y llena
    # `oc_cliente`/`oc_fecha` con su N° y su fecha de emisión — la referencia 801 del
    # SII sigue apuntando a un documento real. `False` vuelve a exigir la OC del
    # cliente; `None` (ausente) NO toca la marca, para que un PATCH de otro campo no
    # la borre sin querer (mismo tri-estado que el resto del schema).
    cliente_sin_oc: Optional[bool] = None
    fecha_entrega_est: Optional[date] = None
    fecha_venta: Optional[datetime] = None
    forma_pago: Optional[str] = None
    # El cierre de venta SIEMPRE lo envía (el modal de la UI): sin este campo en el
    # schema, Pydantic lo DESCARTABA en silencio y el flujo de adelantos (Contabilidad
    # verifica → Tesorería ordena → Abastecimiento libera) nunca se disparaba.
    pct_adelanto: Optional[int] = Field(None, ge=0, le=100)
    numero_factura: Optional[str] = None
    fecha_despacho: Optional[date] = None
    tipo_documento: Optional[str] = None
    # Motivo de una REVERSIÓN (sacar la venta de 'vendida'/'despachado'). Opcional a
    # propósito: la reversión no se bloquea por no escribirlo — bloquearla dejaría al
    # operador sin la salida que corrige un cierre por error —, pero si lo escribe queda
    # en el historial. Ver MonzaCotizacionCierre y _registrar_reversion.
    motivo_reversion: Optional[str] = Field(None, max_length=400)


# ── Helpers ───────────────────────────────────────────────────────────────────

# Campos del body que NO son columnas de la cotización: si se les hace setattr, quedan
# como atributos sueltos del objeto ORM (silenciosos, pero basura). Se excluyen del
# volcado genérico de abajo.
_CAMPOS_NO_PERSISTENTES = {"motivo_reversion"}


def _registrar_cierre(db: Session, cot, usuario_id=None, usuario_email=None):
    """Deja la FOTO de este cierre de venta como una versión nueva.

    Una cotización cerrada puede revertirse (si no tiene plata ni logística colgando) y
    volver a cerrarse con otros datos. El re-cierre PISA el N° de OC, la fecha de OC y la
    fecha prometida, y hasta acá lo hacía sin dejar rastro: en Ventas la venta aparecía
    como si siempre hubiera tenido esos datos. Esta fila es la memoria de que hubo un
    cierre anterior y con qué se cerró.

    El número de versión se calcula BAJO EL MISMO LOCK que el cierre (el `with_for_update`
    de la cotización, más arriba en el PATCH), así que dos cierres simultáneos no pueden
    quedarse con el mismo número.
    """
    ultima = (db.query(func.max(MonzaCotizacionCierre.version))
              .filter(MonzaCotizacionCierre.cotizacion_id == cot.id).scalar()) or 0
    db.add(MonzaCotizacionCierre(
        cotizacion_id=cot.id,
        version=ultima + 1,
        oc_cliente=cot.oc_cliente,
        oc_fecha=cot.oc_fecha,
        fecha_entrega_est=cot.fecha_entrega_est,
        pct_adelanto=cot.pct_adelanto,
        forma_pago=cot.forma_pago,
        total_bruto=cot.total_bruto,
        cerrado_por_id=usuario_id,
        cerrado_por_email=usuario_email,
    ))


def _registrar_reversion(db: Session, cot, nuevo_estado: str, usuario_id=None,
                         usuario_email=None, motivo: str = None):
    """Marca como REVERTIDA la versión vigente del cierre y deshace lo que el cierre sumó.

    Nunca borra la fila: es auditoría, y el punto es justamente que quede el rastro.

    EL LTV. `c.ltv` se suma cuando la venta pasa a 'despachado' (no al cerrarse), y hasta
    acá NADIE lo restaba al volver atrás. El camino era angosto —hay que despachar, anular
    todos los despachos, revertir y re-despachar— pero al recorrerlo el cliente terminaba
    con la venta contada DOS veces en su histórico. Se resta sólo si se está saliendo de
    'despachado', que es el único estado que la sumó, y con el mismo total que se sumó (el
    de la versión, no el vivo: si el total cambió entremedio, restar el vivo dejaría un
    residuo).
    """
    vigente = (db.query(MonzaCotizacionCierre)
               .filter(MonzaCotizacionCierre.cotizacion_id == cot.id,
                       MonzaCotizacionCierre.revertido_at.is_(None))
               .order_by(MonzaCotizacionCierre.version.desc())
               .first())
    if vigente:
        vigente.revertido_at = datetime.utcnow()
        vigente.revertido_por_id = usuario_id
        vigente.revertido_por_email = usuario_email
        vigente.revertido_a_estado = nuevo_estado
        vigente.motivo = (motivo or "").strip() or None

    # El cliente del LTV es el FACTURADO (cot.cliente_id), no el del lead: con «Cotizar
    # a» (arreglos del equipo 2026-08-21) pueden divergir, y la resta tiene que salir de
    # la MISMA ficha donde la suma del despacho entró — asimétrico deja residuo que el
    # max(...,0) esconde. SIN gate por `lead_id`, igual que en la suma: la resta tiene
    # que poder salir aunque el lead ya no exista.
    if (cot.estado or "") == "despachado" and cot.cliente_id:
    # LOCK sobre la ficha: `ltv` y `vendidos_total` se leen, se modifican y se escriben,
    # y sin él dos ventas del MISMO cliente despachadas a la vez se pisan — la segunda
    # lee el valor viejo y guarda su suma encima de la otra. Reproducido: dos ventas
    # simultáneas dejaban el LTV en el monto de UNA sola. La plata desaparecía de la
    # ficha en silencio, sin error ni rastro. `populate_existing` obliga a leer lo
    # COMMITEADO y no el snapshot que la sesión ya tenía en memoria.
        c = (db.query(MonzaCliente)
                 .filter(MonzaCliente.id == cot.cliente_id)
                 .populate_existing().with_for_update().first())
        if c:
            # El total de la versión que se está revirtiendo; si no hay versión
            # registrada (venta anterior a esta entrega) se cae al total vivo, que es
            # lo mejor disponible.
            monto = (vigente.total_bruto if vigente and vigente.total_bruto is not None
                     else cot.total_bruto) or 0
            c.ltv = max((c.ltv or 0) - monto, 0)


def aplicar_efectos_venta_despachada(db: Session, cot) -> None:
    """Efectos de que una venta pase a DESPACHADA: cierra el lead y suma el LTV.

    EL BUG QUE CIERRA (2026-08-22). Estos efectos vivían inline en el PATCH
    administrativo, así que solo ocurrían por esa puerta. Pero el camino NORMAL de la
    operación es otro: cuando el encargado cierra el último despacho, `cerrar_despacho`
    voltea la venta a 'despachado' por su cuenta — y ahí no se sumaba nada. Peor: como
    la venta YA quedaba en 'despachado', un PATCH posterior calculaba
    `es_despacho_nuevo = False` y tampoco los aplicaba. Resultado: el LTV de los
    clientes —la plata que la ficha muestra y que el Portal va a publicar— casi nunca
    se sumaba.

    IDEMPOTENCIA: quien llama decide, y lo hace por la TRANSICIÓN de estado (la venta
    NO estaba en 'despachado' y ahora sí), evaluada bajo el mismo lock que escribe el
    estado. Así los dos caminos son excluyentes por construcción: el que llega segundo
    ve la venta ya despachada y no vuelve a sumar.

    ALCANCE DELIBERADO — espejo LITERAL de lo que hacía el PATCH, ni un efecto más:
      · lead → 'cerrado' (jamás 'vendido': que un automatismo marque leads vendidos es
        una decisión de negocio aparte, con su propio camino);
      · LTV al cliente FACTURADO (`cot.cliente_id`), que con «Cotizar a» puede no ser el
        del lead — la plata es de quien compró;
      · `vendidos_total` NO se toca (es del ciclo del lead, lo mueve el asesor);
      · notificaciones y logs quedan en cada llamador: los dos ya tienen los suyos y
        centralizarlos acá los duplicaría.
    SIN gate por `lead_id`: una venta despachada suma LTV tenga lead o no — el lead solo
    manda en el cierre del lead. El porqué está en el comentario de abajo, y no es un
    detalle: mientras la plata colgó del lead, borrar el lead apagaba la RESTA.
    """
    if not cot:
        return
    # DOS efectos con DOS llaves distintas, y la diferencia importa:
    #   · cerrar el LEAD necesita el lead (obvio);
    #   · sumar el LTV necesita el CLIENTE de la venta — y nada más.
    # Estaban juntos bajo `if cot.lead_id`, así que borrar el lead (que solo desvincula
    # las cotizaciones) apagaba también la RESTA: el LTV quedaba inflado para siempre y
    # ninguna pantalla podía devolverlo. La plata no puede depender de un dato del CRM
    # que la pantalla permite borrar.
    if cot.lead_id:
        lead = db.query(MonzaLead).filter(MonzaLead.id == cot.lead_id).first()
        if lead:
            lead.estado = "cerrado"
            lead.fecha_actualizacion = datetime.utcnow()
    if cot.cliente_id:
    # LOCK sobre la ficha: `ltv` y `vendidos_total` se leen, se modifican y se escriben,
    # y sin él dos ventas del MISMO cliente despachadas a la vez se pisan — la segunda
    # lee el valor viejo y guarda su suma encima de la otra. Reproducido: dos ventas
    # simultáneas dejaban el LTV en el monto de UNA sola. La plata desaparecía de la
    # ficha en silencio, sin error ni rastro. `populate_existing` obliga a leer lo
    # COMMITEADO y no el snapshot que la sesión ya tenía en memoria.
        c = (db.query(MonzaCliente)
                 .filter(MonzaCliente.id == cot.cliente_id)
                 .populate_existing().with_for_update().first())
        if c:
            c.ltv = (c.ltv or 0) + (cot.total_bruto or 0)


def revertir_efectos_venta_despachada(db: Session, cot) -> None:
    """Deshace el LTV cuando una venta SALE de 'despachado' (espejo exacto de la suma).

    Simétrico con `aplicar_efectos_venta_despachada`: misma llave (solo el cliente
    FACTURADO, sin lead) y el clamp en 0 para que un dato viejo no deje el LTV negativo.
    El lead NO se reabre: quedó cerrado y su rastro vive en la Actividad — reabrirlo
    inventaría un lead "en proceso" que nadie está trabajando.

    CUÁNDO llamarlo: SOLO en la transición efectiva 'despachado' → 'vendida', nunca en
    los barridos de reparación que recorren ventas ya en 'vendida' (restarían plata que
    jamás entró). El monto es `total_bruto` vivo, el mismo que usó la suma.
    """
    # MISMA llave que la suma (solo el cliente): si la resta pidiera además el lead,
    # una venta cuyo lead se borró sumaría y nunca devolvería.
    if not cot or not cot.cliente_id:
        return
    # LOCK sobre la ficha: `ltv` y `vendidos_total` se leen, se modifican y se escriben,
    # y sin él dos ventas del MISMO cliente despachadas a la vez se pisan — la segunda
    # lee el valor viejo y guarda su suma encima de la otra. Reproducido: dos ventas
    # simultáneas dejaban el LTV en el monto de UNA sola. La plata desaparecía de la
    # ficha en silencio, sin error ni rastro. `populate_existing` obliga a leer lo
    # COMMITEADO y no el snapshot que la sesión ya tenía en memoria.
    c = (db.query(MonzaCliente)
                 .filter(MonzaCliente.id == cot.cliente_id)
                 .populate_existing().with_for_update().first())
    if c:
        c.ltv = max((c.ltv or 0) - (cot.total_bruto or 0), 0)


def _gen_numero_cot(db: Session) -> str:
    # El año es el de CHILE: entre las 21:00 y la medianoche del 31 de diciembre, UTC
    # ya está en el año siguiente y el correlativo saltaba de golpe (misma regla que
    # monza_correlativos, donde vive la versión de referencia).
    anio = hoy_chile().year
    # MÁXIMO de la serie, no «la fila con id más alto» (ver monza_correlativos).
    n = siguiente_secuencia(db, MonzaCotizacion.numero, f"COT-{anio}-")
    return f"COT-{anio}-{n:06d}"


def _get_config(db: Session) -> MonzaConfig:
    cfg = db.query(MonzaConfig).filter(MonzaConfig.id == 1).first()
    if not cfg:
        cfg = MonzaConfig(id=1)
        db.add(cfg); db.commit(); db.refresh(cfg)
    return cfg


def _dte_vivo_de_la_venta(db: Session, cot_id: int):
    """Documento tributario electrónico VIVO de esta venta → (dte, transaccion_reiniciada).

    Vivo = emitido (folio del SII), en vuelo (claim fresco), en proceso en Wasabil, o
    AMBIGUO. Devuelve el PRIMERO que encuentre (o None) y una bandera que avisa si la
    comprobación tuvo que descartar la transacción — ver más abajo.

    POR QUÉ existe (hallazgo W6 de la paridad contable): el N° y la FECHA de la OC del
    cliente viajan al SII como REFERENCIA 801 de cada documento de la venta —
    `armar_guia` de la guía 52 y `armar_referencias_factura` de la factura 33
    (monza_wasabil_dte/service.py). Editarlos con un documento vivo dejaría el papel
    legal desincronizado del sistema, y en un despacho PARCIAL la 2ª guía saldría con
    una OC distinta a la 1ª. La anulación de un DTE se gestiona en Wasabil, no
    reescribiendo el dato local. Espejo del 409 del PUT /oc-cliente de Grupo AM
    (routers/compras.py) sobre el modelo Monza: acá la guía cuelga del DESPACHO y la
    factura de la FACTURA DE CLIENTE, y las dos llegan a la venta por su cotizacion_id
    (en Monza la cotización ES la venta: no hay OcCliente).

    El caso AMBIGUO — uuid NULL con `en_vuelo_desde` puesto y el claim ya VENCIDO por
    TTL — CUENTA como vivo, igual que el `incluir_ambiguo` de
    monza_router_despachos.py:_guia_electronica_activa: ahí Wasabil cortó la
    comunicación sin confirmar nada y el documento PUDO nacer con folio real en el SII.
    Editar la OC es tan irreversible como una segunda emisión, así que no se decide
    por TTL.

    Tabla inexistente (MySQL 1146: deploy sin correr el init_db del módulo DTE, o sin
    el de contabilidad) → esa mitad del guard se apaga sola en vez de tumbar la edición
    con un 500 (mismo criterio que _guia_electronica_activa: sin tabla JAMÁS hubo
    emisión que proteger). Las dos mitades van en try/except SEPARADOS para que la
    ausencia del módulo contable no desarme el candado de las guías. OJO: el rollback
    que exige ese except SUELTA los locks del llamador (patrón ya documentado en
    monza_router_despachos.py:_rechazar_split_sobre_documento), y por eso se devuelve
    `transaccion_reiniciada`: el llamador re-toma el FOR UPDATE antes de seguir.
    """
    from sqlalchemy.exc import ProgrammingError
    from monza_models import MonzaDespacho
    # monza_contabilidad.models solo importa sqlalchemy + database (no crea ciclo), y
    # monza_wasabil_dte.models ya lo registra siempre para que configure_mappers() no
    # reviente: importarlo acá no cambia lo que se carga, solo lo hace explícito.
    from monza_contabilidad.models import MonzaContFacturaCliente
    from monza_wasabil_dte.models import (
        MonzaWasabilDte, STATUS_EMITIDO, STATUS_PROCESANDO, STATUS_PENDIENTE,
    )
    from monza_wasabil_dte.service import TIPO_DOC_GUIA, TIPO_DOC_FACTURA, claim_vigente

    reiniciada = False

    def _opcional(fn):
        """Comprobación cuyo módulo puede no estar instalado (tabla inexistente)."""
        nonlocal reiniciada
        try:
            return fn()
        except ProgrammingError as e:
            if getattr(getattr(e, "orig", None), "args", [None])[0] == 1146:
                db.rollback()
                reiniciada = True
                return []
            raise

    def _vivo(dte) -> bool:
        if dte.status_id == STATUS_EMITIDO:
            return True
        if claim_vigente(dte):
            return True
        if dte.uuid and dte.status_id in (STATUS_PROCESANDO, STATUS_PENDIENTE):
            return True
        # AMBIGUO con el claim ya vencido: el POST salió y nadie confirmó el resultado.
        return dte.uuid is None and dte.en_vuelo_desde is not None

    # Guías 52 de la venta (vía despacho). Import LOCAL del paquete DTE: no se acopla
    # el arranque de Ventas al módulo Wasabil ni se carga en los caminos que no lo usan.
    candidatos = _opcional(lambda: (
        db.query(MonzaWasabilDte)
        .join(MonzaDespacho, MonzaDespacho.id == MonzaWasabilDte.despacho_id)
        .filter(MonzaDespacho.cotizacion_id == cot_id,
                MonzaWasabilDte.tipo_dte == TIPO_DOC_GUIA)
        .all()))
    # Facturas 33 de la venta (vía factura de cliente): la 801 también viaja en ellas.
    candidatos = list(candidatos) + list(_opcional(lambda: (
        db.query(MonzaWasabilDte)
        .join(MonzaContFacturaCliente,
              MonzaContFacturaCliente.id == MonzaWasabilDte.factura_id)
        .filter(MonzaContFacturaCliente.cotizacion_id == cot_id,
                MonzaWasabilDte.tipo_dte == TIPO_DOC_FACTURA)
        .all())))
    # El EMITIDO manda sobre el resto: si hay folio, el mensaje del 409 lo nombra.
    vivos = [d for d in candidatos if _vivo(d)]
    vivos.sort(key=lambda d: (0 if d.folio else 1, d.id))
    return (vivos[0] if vivos else None), reiniciada


def _cot_dict(c: MonzaCotizacion, db: Session = None) -> dict:
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
        # Venta a cliente PARTICULAR: sin este dato el modal de cierre no puede volver a
        # abrirse con la casilla marcada y el operador leería la OC (= N° de cotización)
        # como un error de digitación.
        "cliente_sin_oc": bool(c.cliente_sin_oc),
        # Hallazgos #14 y #20 (auditoría integral Fases 1-6): oc_fecha y pct_adelanto
        # eran WRITE-ONLY — el modal de cierre no podía leerlos, así que inicializaba
        # la fecha de OC con HOY (pisando la real, que viaja como referencia 801 al SII)
        # y el % de adelanto con el default fijo (un re-cierre lo bajaba a 0 en silencio
        # y desarmaba el cortafuego de adelanto de Abastecimiento). Aditivo: lo hereda
        # _cot_detail y no cambia ningún consumidor existente.
        "oc_fecha": c.oc_fecha.isoformat() if c.oc_fecha else None,
        "pct_adelanto": int(c.pct_adelanto or 0),
        "adelanto_verificado": bool(c.adelanto_verificado),
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


def _cot_detail(c: MonzaCotizacion, db: Session = None) -> dict:
    d = _cot_dict(c, db)
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
            # Foto por ítem (freeze-forward): TC y tarifa con que se calculó la línea.
            "tc_aplicado": it.tc_aplicado,
            "tarifa_aerea": it.tarifa_aerea,
            # Su MONEDA (NULL en líneas pre-2026-08-22 = la de la cabecera).
            "moneda_tarifa": it.moneda_tarifa,
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
        # Moneda de la tarifa congelada al crear (freeze-forward: NULL en las viejas).
        "moneda_tarifa": c.moneda_tarifa,
        "tarifa_aerea": c.tarifa_aerea,
        "iva_pct": c.iva_pct,
    }
    if db is not None:
        # Historial de cierres. Se sirve SÓLO con `db` explícito (o sea, en el detalle):
        # meterlo en el listado sería una consulta por fila, que es el N+1 clásico.
        cierres = (db.query(MonzaCotizacionCierre)
                   .filter(MonzaCotizacionCierre.cotizacion_id == c.id)
                   .order_by(MonzaCotizacionCierre.version.desc()).all())
        d["cierres"] = [{
            "version": x.version,
            "oc_cliente": x.oc_cliente,
            "oc_fecha": x.oc_fecha.isoformat() if x.oc_fecha else None,
            "fecha_entrega_est": x.fecha_entrega_est.isoformat() if x.fecha_entrega_est else None,
            "pct_adelanto": x.pct_adelanto,
            "forma_pago": x.forma_pago,
            "total_bruto": x.total_bruto,
            "cerrado_at": x.cerrado_at.isoformat() if x.cerrado_at else None,
            "cerrado_por": x.cerrado_por_email,
            "revertido_at": x.revertido_at.isoformat() if x.revertido_at else None,
            "revertido_por": x.revertido_por_email,
            "revertido_a_estado": x.revertido_a_estado,
            "motivo": x.motivo,
        } for x in cierres]
        # Atajo para la pantalla: cuántas veces se cerró esta venta. Con >= 1 y estado
        # 'vendida', el aviso de reversión puede decir desde cuándo está cerrada y con qué
        # OC, en vez de una advertencia genérica.
        d["veces_cerrada"] = len(cierres)
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
    # Días de Chile → rango semiabierto en UTC (monza_fechas): el gemelo del mismo
    # defecto que Ventas y Leads tenían — el día `hasta` quedaba fuera entero.
    desde_utc, hasta_utc = rango_utc(desde, hasta)
    if desde_utc:
        query = query.filter(MonzaCotizacion.fecha_creacion >= desde_utc)
    if hasta_utc:
        query = query.filter(MonzaCotizacion.fecha_creacion < hasta_utc)

    total = query.count()
    items = query.order_by(MonzaCotizacion.fecha_creacion.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "page": page, "page_size": page_size, "items": [_cot_dict(c) for c in items]}


# ── Create ────────────────────────────────────────────────────────────────────

@router.post("", status_code=201)
def create_cotizacion(body: CotCreate, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """Crea una cotización, reintentando si dos se crean a la vez.

    Misma enfermedad —y mismo remedio— que el correlativo de leads: `_gen_numero_cot`
    lee el último número y le suma 1 sin lock, así que dos vendedores cotizando al mismo
    tiempo (el escenario normal de una mañana) calculaban el mismo 'COT-2026-000N'.
    `numero` es UNIQUE, así que la segunda moría con un 500 sin capturar y la cotización
    se perdía ENTERA, con todos sus ítems y sus precios ya calculados — y la cotización
    es el documento que después se convierte en la venta. Medido: 2 de 4 simultáneas.

    El reintento envuelve la operación completa (no solo el número) porque un choque
    entre dos cotizaciones DEL MISMO CLIENTE llega como deadlock, no como violación del
    UNIQUE, y un deadlock deshace la transacción entera. Ver monza_correlativos.
    """
    return reintentar_carrera(db, lambda: _crear_cotizacion_tx(body, db, current_user),
                              que="cotizaciones")


def _crear_cotizacion_tx(body: CotCreate, db: Session, current_user):
    cfg = _get_config(db)

    cliente = db.query(MonzaCliente).filter(MonzaCliente.id == body.cliente_id).first()
    if not cliente:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")

    condiciones = body.condiciones_servicio or cfg.condiciones_default

    # ── Foto del FLETE: sale del LEAD, no de la configuración global ──────────
    # El operador elige la moneda del flete EN LA CALCULADORA y esa elección queda en el
    # lead (monza_leads.moneda_tarifa / .tarifa_aerea) junto con los precios que produjo.
    # Congelar acá la global significaba guardar una tarifa DISTINTA de la que generó los
    # precios de estos mismos ítems: la foto dejaba de reproducir su propia cotización.
    # Antes casi no se notaba (la moneda solo cambiaba el TC); desde que cada moneda tiene
    # SU tarifa por kilo (2026-08-08), la diferencia es el flete completo.
    # Sin lead —cotización creada a mano— se cae a la configuración, resolviendo la tarifa
    # POR MONEDA con el mismo helper que usa la calculadora (fuente única).
    from monza_router_cotizador import _tarifa_configurada  # local: evita ciclo
    lead_flete = (db.query(MonzaLead).filter(MonzaLead.id == body.lead_id).first()
                  if body.lead_id else None)
    moneda_flete = ((lead_flete.moneda_tarifa if lead_flete else None)
                    or cfg.moneda_tarifa or "EUR").upper()
    tarifa_flete = (lead_flete.tarifa_aerea if lead_flete and lead_flete.tarifa_aerea is not None
                    else _tarifa_configurada(cfg, moneda_flete))

    # ── Estampas de flete POR ÍTEM del lead (arreglos del equipo 2026-08-21) ──────
    # Con corridas por subconjunto, el par del LEAD retrata solo la ÚLTIMA corrida.
    # Cada línea congela el flete de SU corrida: la estampa del MonzaLeadItem, leída
    # por `lead_item_id` en UNA query y validando pertenencia al lead de la cotización
    # (fail-closed: un id ajeno o inexistente simplemente se ignora — jamás 500, jamás
    # una estampa de otro lead). Solo entran al dict las estampas COMPLETAS.
    estampas: dict = {}
    _ids_lead_items = [it.lead_item_id for it in body.items if it.lead_item_id]
    if body.lead_id and _ids_lead_items:
        _filas = (
            db.query(MonzaLeadItem)
            .filter(MonzaLeadItem.id.in_(_ids_lead_items),
                    MonzaLeadItem.lead_id == body.lead_id)
            .all()
        )
        estampas = {li.id: ((li.moneda_tarifa or "").upper(), float(li.tarifa_aerea))
                    for li in _filas
                    if li.moneda_tarifa and li.tarifa_aerea is not None}
    # Cabecera: si las estampas del subconjunto EMITIDO son uniformes (ignorando los
    # ítems sin estampa, misma regla que la siembra del cotizador), la cabecera retrata
    # ese par — no el del lead, que puede ser de una corrida posterior a estos precios.
    # Mixtas → cabecera del lead como siempre; cada línea queda reproducible por su
    # propia estampa y la pantalla avisa la mezcla (no la bloquea).
    _pares = set(estampas.values())
    if len(_pares) == 1:
        moneda_flete, tarifa_flete = next(iter(_pares))

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
        # Moneda y tarifa del flete congeladas junto al resto de la foto: sin ellas el
        # cálculo no es reproducible (espejo del pricing_snapshot de GA). Salen del LEAD
        # —la elección que produjo estos precios—, ver el bloque de arriba.
        moneda_tarifa=moneda_flete,
        tarifa_aerea=tarifa_flete,
        iva_pct=cfg.iva_pct,
    )
    db.add(cot)
    db.flush()

    total_neto = 0
    for it in body.items:
        precio_unit = it.precio_unitario_clp or 0
        subtotal = it.subtotal_clp or (precio_unit * it.cantidad)
        total_neto += subtotal
        # Foto por ítem (freeze-forward, espejo pricing_snapshot GA): el TC y la
        # tarifa se resuelven SERVER-side desde la config vigente según la moneda
        # del ítem — JAMÁS del body (la foto sale del servidor, no del cliente).
        _mon = (it.moneda or "").upper()
        if _mon == "EUR":
            tc_item = cfg.tc_eur_clp
        elif _mon == "USD":
            tc_item = cfg.tc_usd_clp
        else:
            tc_item = 1.0  # CLP u otra moneda sin TC: el costo ya viene en pesos
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
            tc_aplicado=tc_item,
            # El flete de la CORRIDA que calculó esta línea (estampa del ítem del lead);
            # sin estampa, la tarifa de cabecera como siempre. Es foto de auditoría y
            # reproducibilidad: hoy NADIE computa con ella (Embarques Pricing lee
            # costo/moneda/peso_kg, nunca esta columna — verificado 2026-08-21), pero la
            # API la sirve como "tarifa con que se calculó la línea" y el split de
            # Abastecimiento la clona: tiene que decir la verdad. La tarifa viaja CON su
            # moneda — en una emisión mixta el número solo no dice de qué moneda es.
            tarifa_aerea=(estampas[it.lead_item_id][1]
                          if it.lead_item_id in estampas else tarifa_flete),
            moneda_tarifa=(estampas[it.lead_item_id][0]
                           if it.lead_item_id in estampas else moneda_flete),
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

    # El log va DENTRO de la transacción, antes del commit — igual que en POST /leads.
    # Escrito después quedaba bajo el bucle de reintento pero fuera de lo que el rollback
    # deshace, y un error transitorio ahí rehacía la creación entera (ya guardada): una
    # sola petición devolvía un 201 habiendo creado DOS cotizaciones, cada una con sus
    # ítems y sus precios. `reintentar_carrera` tiene además un candado que corta el
    # reintento en cuanto detecta un commit, pero el orden correcto es esta línea.
    from monza_models import MonzaLog
    db.add(MonzaLog(user_email=current_user.email, accion="CREATE", entidad="cotizacion",
                    entidad_id=cot.id, entidad_ref=cot.numero,
                    detalle=f"Cotización {cot.numero} emitida"))
    db.commit()
    db.refresh(cot)
    return _cot_dict(cot, db)


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
    return _cot_detail(cot, db)


# ── Update estado ─────────────────────────────────────────────────────────────

# Candado de rol ADEMÁS del de empresa (espejo del PUT /oc-cliente de GA en
# routers/compras.py): permisivo mientras User.rol no exista — forward-compatible.
@router.patch(
    "/{cot_id}",
    dependencies=[Depends(require_rol("comercial", "contabilidad", "admin"))],
)
def update_cotizacion(
    cot_id: int,
    body: CotUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(require_empresa("automotriz")),
):
    """Actualiza la cotización; el CIERRE DE VENTA (estado='vendida') pasa por aquí.

    Endurecimiento Fase 3 (espejo del cierre de GA en routers/compras.py):
      · N° de OC del cliente OBLIGATORIO para cerrar (la ref 801 del SII exige N° y
        fecha; sin OC tampoco hay ancla comercial del cobro).
      · Cierre IDEMPOTENTE: re-enviar 'vendida' sobre una venta ya cerrada edita los
        campos pero NO repite efectos (fecha_venta, líneas, log VENDIDA, notificación).
      · Sin retroceso: una venta 'despachado' no vuelve a 'vendida' por este PATCH.
      · Sin DES-CIERRE: una venta cerrada (vendida/despachado) no retrocede a
        propuesta/enviada/rechazada si tiene plata o logística colgando (facturas,
        adelanto, despachos no-anulados) — 409; sin nada colgando se permite
        (corrige un cierre por error recién hecho).
      · Despacho idempotente: re-enviar 'despachado' no vuelve a sumar el LTV del
        cliente ni repite log/notificación (espejo de es_cierre_nuevo).
      · Candado de empresa: solo usuarios automotriz (es el camino que mueve plata).
      · Candado de rol (permisivo hasta provisionar User.rol): comercial/contabilidad/admin.

    Endurecimiento de la auditoría integral Fases 1-6:
      · 'despachado' exige COBERTURA REAL (hallazgo #8): cada línea no-despachada
        debe estar cubierta por despachos CERRADOS, y la venta debe venir de
        'vendida'. El estado logístico lo gobierna el cierre de despacho.
      · pct_adelanto no BAJA si hay plata de adelanto registrada (hallazgo #14) — 409.

    Endurecimiento de la paridad contable Monza ↔ MachParts:
      · W6: el N° y la FECHA de la OC NO se editan con un DTE vivo de la venta (guía 52
        o factura 33) — 409 nombrando el folio. Los dos datos son la referencia 801 del
        documento (_dte_vivo_de_la_venta). Re-enviar el MISMO valor sigue siendo no-op.
      · A5: solo se cierra como venta una cotización ENVIADA (o ya vendida, por
        idempotencia) — 409 sobre 'propuesta' y 'rechazada' (ESTADOS_QUE_CIERRAN_VENTA).
    """
    # FOR UPDATE + populate_existing (regla de la casa): el cierre muta estados de
    # línea y dispara el flujo de adelantos — dos cierres/ediciones simultáneos se
    # serializan aquí en vez de pisarse.
    cot = (
        db.query(MonzaCotizacion)
        .filter(MonzaCotizacion.id == cot_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if not cot:
        raise HTTPException(status_code=404, detail="Cotización no encontrada")

    # ── VENTA A CLIENTE PARTICULAR (2026-08-08) ───────────────────────────────
    # Un consumidor final no emite orden de compra. Con la casilla marcada, el
    # documento de respaldo pasa a ser NUESTRA cotización: su N° y su fecha de emisión
    # se graban en oc_cliente/oc_fecha, así la referencia 801 del SII sigue apuntando a
    # un documento real y verificable (sin ella la venta cerraría pero no se podría
    # emitir ni guía 52 ni factura 33 — los tres guards del módulo DTE exigen ambos).
    #
    # Va ANTES del guard W6 A PROPÓSITO: la marca REESCRIBE oc_cliente/oc_fecha, así que
    # tiene que pasar por el mismo candado que una edición manual. Si se resolviera
    # después, marcar la casilla sería la puerta trasera para cambiar la referencia 801
    # de una venta ya facturada — exactamente lo que W6 existe para impedir.
    if body.cliente_sin_oc:
        numero_cot = (cot.numero or "").strip()
        if not numero_cot:
            raise HTTPException(
                400,
                "Esta cotización no tiene número: no se puede usar como documento de "
                "respaldo de una venta a cliente particular.",
            )
        # El SII limita el folio de una referencia a 18 caracteres. El formato de la
        # casa (COT-2026-0001) entra de sobra, pero si algún día crece, mejor un 400
        # explicado acá que el rechazo del SII al emitir un documento irreversible.
        from monza_wasabil_dte.service import FOLIO_REF_MAX  # local: evita ciclo
        if len(numero_cot) > FOLIO_REF_MAX:
            raise HTTPException(
                400,
                f"El N° de esta cotización ('{numero_cot}') tiene {len(numero_cot)} "
                f"caracteres y el SII permite máximo {FOLIO_REF_MAX} en la referencia "
                "a la orden de compra: no puede usarse como respaldo de la venta.",
            )
        # La FECHA es la de emisión de la cotización, no la de hoy: folio y fecha tienen
        # que ser del MISMO documento, o la referencia 801 cita un papel que no existe.
        fecha_cot = cot.fecha_creacion.date() if cot.fecha_creacion else None
        if not fecha_cot:
            raise HTTPException(
                400,
                "Esta cotización no tiene fecha de emisión: no se puede usar como "
                "documento de respaldo (la referencia al SII la exige).",
            )
        # Se inyectan en el body para que TODO el resto del PATCH —el guard W6, la
        # validación del cierre y el versionado— vea exactamente los mismos valores que
        # se van a persistir. Sin esto habría dos verdades en la misma función.
        body.oc_cliente = numero_cot
        body.oc_fecha = fecha_cot

    # ── Hallazgo W6 (paridad contable): el N° y la FECHA de la OC son REFERENCIA 801
    # del DTE. Con un documento tributario vivo no se editan (espejo del PUT
    # /oc-cliente de GA en routers/compras.py, que responde 409 nombrando el folio).
    # Se compara contra el valor ACTUAL: re-enviar el mismo N°/fecha —lo que hace el
    # cierre idempotente cada vez que el modal se reintenta— es un no-op y NO bloquea.
    # Va primero, antes de cualquier otro guard: acá todavía no se evaluó nada, así que
    # si la comprobación tiene que descartar la transacción se re-toma el lock limpio.
    if ((body.oc_cliente is not None and (body.oc_cliente or "") != (cot.oc_cliente or ""))
            or (body.oc_fecha is not None and body.oc_fecha != cot.oc_fecha)):
        dte_vivo, _transaccion_reiniciada = _dte_vivo_de_la_venta(db, cot.id)
        if dte_vivo:
            from monza_wasabil_dte.service import TIPO_DOC_FACTURA  # local: evita ciclo
            _doc = ("factura electrónica" if dte_vivo.tipo_dte == TIPO_DOC_FACTURA
                    else "guía de despacho electrónica")
            raise HTTPException(
                409,
                f"Esta venta ya tiene {_doc} ante el SII"
                + (f" (folio {dte_vivo.folio})" if dte_vivo.folio else " (en emisión)")
                + ": el N° y la fecha de la OC del cliente quedaron referenciados en el "
                "documento (referencia 801) y no se editan. Los demás campos de la venta "
                "sí se pueden corregir; la anulación del documento se gestiona en Wasabil.",
            )
        if _transaccion_reiniciada:
            # El guard tuvo que descartar la transacción (tabla del módulo DTE
            # inexistente): el FOR UPDATE de arriba se soltó y se re-toma para que el
            # resto del PATCH siga serializado.
            cot = (
                db.query(MonzaCotizacion)
                .filter(MonzaCotizacion.id == cot_id)
                .populate_existing()
                .with_for_update()
                .first()
            )
            if not cot:
                raise HTTPException(status_code=404, detail="Cotización no encontrada")

    es_cierre_nuevo = body.estado == "vendida" and cot.estado not in ("vendida", "despachado")
    # Transición a 'despachado' IDEMPOTENTE (mismo patrón que es_cierre_nuevo): sin
    # este gate, cada re-PATCH 'despachado' volvía a sumar el total al LTV del
    # cliente (bug de plata) y repetía la notificación de despacho.
    es_despacho_nuevo = body.estado == "despachado" and cot.estado != "despachado"
    if body.estado == "vendida":
        if cot.estado == "despachado":
            raise HTTPException(409, "La venta ya está despachada: no puede volver a 'vendida'")
        # Hallazgo A5 (paridad contable): solo se cierra una cotización ENVIADA al
        # cliente (o una ya vendida, por idempotencia). 'despachado' ya salió con su
        # propio 409 justo arriba, así que acá caen 'propuesta' (nunca se envió) y
        # 'rechazada' — y cerrarlas dispara compras al proveedor.
        if (cot.estado or "propuesta") not in ESTADOS_QUE_CIERRAN_VENTA:
            raise HTTPException(
                409,
                f"La cotización está en estado '{cot.estado or 'propuesta'}': no se puede "
                f"cerrar como venta. Una cotización rechazada, o que nunca se le envió al "
                f"cliente, no es una venta — y el cierre libera las compras al proveedor. "
                f"Si el cliente la aprobó, márcala primero como 'Enviada' y vuelve a cerrar.",
            )
        oc_num = (body.oc_cliente if body.oc_cliente is not None else cot.oc_cliente) or ""
        if not oc_num.strip():
            # La salida para el consumidor final: marcar «Cliente particular» en el
            # cierre, que usa esta cotización como documento de respaldo. Se nombra en
            # el mensaje porque el operador que ve este error es justo el que no sabe
            # qué poner cuando el cliente no tiene OC.
            raise HTTPException(
                400,
                "El N° de OC del cliente es obligatorio para cerrar la venta. Si el "
                "cliente es un particular y no emite orden de compra, marca «Cliente "
                "particular (sin OC)» y se usará el N° de esta cotización.",
            )

    # ── Hallazgo #8 (auditoría integral Fases 1-6): el flip a 'despachado' era CIEGO —
    # marcaba TODAS las líneas como despachadas sin un solo despacho, rompiendo el
    # invariante de la Fase 2 (SOLO cerrar_despacho voltea la línea) y TRABANDO la
    # mercadería: con la línea en 'despachado' el POST /despachos/crear responde 400
    # "Item no está en bodega", así que esas unidades ya no se pueden despachar ni
    # emitir su guía 52, y los tableros informan despachado algo que nunca salió.
    # Ahora la ruta administrativa sigue existiendo (es idempotente y la usa el cierre
    # manual), pero exige COBERTURA REAL de despachos CERRADOS.
    if body.estado == "despachado":
        # Agravante 2: propuesta → despachado se saltaba la OC obligatoria, fecha_venta
        # y el paso por 'vendida' (que es el que suma el LTV con datos completos).
        if cot.estado not in ("vendida", "despachado"):
            raise HTTPException(409, "La venta debe estar cerrada antes de marcarse despachada")
        # Import LOCAL (estilo del repo): monza_router_despachos importa monza_models;
        # importarlo arriba acoplaría dos routers en el arranque.
        from monza_router_despachos import _qty_dispatched_closed
        cerr = _qty_dispatched_closed(db, cot.id)
        for _it in cot.items:
            # Las líneas que YA están en 'despachado' no se re-validan: el re-PATCH es
            # idempotente y su cobertura la fijó el cierre de despacho en su momento.
            if (_it.estado_linea or "cotizado") == "despachado":
                continue
            _cerrado = cerr.get(_it.id, 0)
            if _cerrado + 0.001 < (_it.cantidad or 0):
                raise HTTPException(
                    409,
                    f"No se puede marcar la venta como despachada: el ítem "
                    f"'{_it.descripcion}' no está cubierto por despachos CERRADOS "
                    f"(cerrado: {_cerrado} de {_it.cantidad}). El estado logístico lo "
                    f"gobierna el cierre de despacho.",
                )

    # Sin DES-CIERRE silencioso (espejo del espíritu GA: la OcCliente del cierre es
    # permanente y ancla la plata): una venta cerrada no retrocede a propuesta/enviada/
    # rechazada por este PATCH. EXCEPCIÓN sensata: si la venta NO tiene plata ni
    # logística colgando (0 facturas, 0 adelanto, 0 despachos no-anulados) se permite
    # el retroceso — corrige un cierre por error recién hecho. Corre bajo el FOR UPDATE
    # de arriba, así que el chequeo + cambio de estado es atómico.
    if (body.estado
            and body.estado not in ("vendida", "despachado")
            and cot.estado in ("vendida", "despachado")):
        # Imports locales: monza_contabilidad importa monza_models — importarlo arriba
        # crearía un ciclo de imports entre módulos.
        from monza_contabilidad.models import MonzaContAdelanto, MonzaContFacturaCliente
        from monza_models import MonzaDespacho
        n_fact = db.query(MonzaContFacturaCliente).filter(
            MonzaContFacturaCliente.cotizacion_id == cot.id).count()
        n_adel = db.query(MonzaContAdelanto).filter(
            MonzaContAdelanto.cotizacion_id == cot.id).count()
        n_desp = db.query(MonzaDespacho).filter(
            MonzaDespacho.cotizacion_id == cot.id,
            MonzaDespacho.estado != "anulado").count()
        if n_fact or n_adel or n_desp:
            raise HTTPException(
                409,
                f"La venta ya está cerrada (estado '{cot.estado}') y tiene plata/logística "
                f"asociada ({n_fact} factura(s), {n_adel} adelanto(s), {n_desp} despacho(s)): "
                f"no puede volver a '{body.estado}'. Anula/elimina eso primero.",
            )
        # La reversión está autorizada: se deja el RASTRO antes de tocar el estado, porque
        # `_registrar_reversion` decide sobre `cot.estado` (el de ANTES) para saber si hay
        # que deshacer el LTV. Después del volcado de campos ya sería tarde.
        _registrar_reversion(db, cot, body.estado,
                             usuario_id=getattr(current_user, "id", None),
                             usuario_email=current_user.email,
                             motivo=body.motivo_reversion)

    # Hallazgo #14 (auditoría integral Fases 1-6): como el modal manda opt.pct FIJO,
    # un re-cierre eligiendo "contado" bajaba pct_adelanto de 50 a 0 en silencio y con
    # eso el cortafuego de monza_router_abastecimiento.py:203-217 dejaba de frenar la
    # OC de proveedor (solo frena si pct_adelanto > 0 y adelanto_verificado == 0).
    # Corre bajo el FOR UPDATE de arriba, así que el chequeo + la asignación son
    # atómicos. Bajar el % SIN plata registrada se sigue permitiendo: es el caso
    # legítimo de corregir la condición de pago (implementa monza_contabilidad/README.md:40).
    if body.pct_adelanto is not None and int(body.pct_adelanto) < int(cot.pct_adelanto or 0):
        from monza_contabilidad.models import MonzaContAdelanto  # local: evita ciclo
        tiene_plata = int(cot.adelanto_verificado or 0) or db.query(MonzaContAdelanto).filter(
            MonzaContAdelanto.cotizacion_id == cot.id).count()
        if tiene_plata:
            raise HTTPException(
                409,
                f"La venta tiene un adelanto verificado: no se puede bajar la condición "
                f"de pago de {int(cot.pct_adelanto or 0)}% a {int(body.pct_adelanto)}%. "
                f"Revierta el adelanto en Contabilidad/Tesorería primero.",
            )

    # ── Contado ⇒ 100% (arreglos del equipo 2026-08-21, cinturón backend) ─────────
    # La regla vive en el frontend (constants/adelanto.ts manda pct 100 con la forma
    # "Contado"), pero un build VIEJO en caché justo después del deploy —o una llamada
    # directa al API— dejaría una venta "Contado" con pct < 100 saltándose la cola de
    # Tesorería y el cortafuego de Abastecimiento: exactamente el agujero que el pedido
    # #8 vino a cerrar. Falla CERRADO con el mensaje que destraba al operador. Match
    # EXACTO con "Contado" (el único texto que la constante escribió siempre, también
    # en las ventas viejas): un texto libre distinto no es la condición contado de la
    # casa y este guard no policía texto libre. Corre bajo el FOR UPDATE del PATCH.
    #
    # El gate cubre DOS puertas (ronda escéptica 2026-08-22, hallazgo ALT1 confirmado
    # empíricamente): (a) el CIERRE (body.estado == 'vendida'); y (b) la EDICIÓN
    # post-cierre — un PATCH sin `estado` que toque forma_pago o pct_adelanto sobre una
    # venta ya cerrada ({pct_adelanto: 0} apagaba el cortafuego con 200, y
    # {forma_pago: 'Contado'} dejaba Contado+50 sin pasar por ningún guard). El
    # invariante es de la VENTA CERRADA, no del verbo que la tocó. Bajar el pct
    # cambiando también la forma (corregir un cierre mal hecho a crédito) sigue
    # permitido: la forma efectiva deja de ser "Contado".
    _toca_condicion = body.forma_pago is not None or body.pct_adelanto is not None
    _estado_efectivo = body.estado if body.estado is not None else (cot.estado or "")
    if (body.estado == "vendida"
            or (_toca_condicion and _estado_efectivo in ("vendida", "despachado"))):
        _forma_efectiva = (body.forma_pago if body.forma_pago is not None
                           else cot.forma_pago) or ""
        _pct_efectivo = int(body.pct_adelanto if body.pct_adelanto is not None
                            else (cot.pct_adelanto or 0))
        if _forma_efectiva == "Contado" and _pct_efectivo < 100:
            raise HTTPException(
                409,
                "Contado exige verificación de Tesorería: la venta debe quedar con el "
                "100% de adelanto informado. Si la pantalla no lo mandó sola, está "
                "desactualizada — recarga la página (o elige otra condición de pago).",
            )

    for field, value in body.model_dump(exclude_none=True).items():
        if field in _CAMPOS_NO_PERSISTENTES:
            continue
        setattr(cot, field, value)
    if es_cierre_nuevo and not cot.fecha_venta:
        cot.fecha_venta = datetime.utcnow()
    if es_cierre_nuevo:
        for _it in cot.items:
            if (_it.estado_linea or "cotizado") == "cotizado":
                _it.estado_linea = "por_comprar"
        # El LEAD de una venta cerrada pasa a 'vendido' (2026-08-22). Antes, el único
        # camino era el clic del asesor en el detalle del lead: si se le olvidaba —y se
        # le olvidaba— el lead quedaba 'en_proceso' para siempre, inflaba el contador de
        # «sin contactar» y el embudo mostraba como abiertos leads que ya se habían
        # vendido. IDEMPOTENTE y sin pisar hacia atrás: solo mueve leads que aún están
        # abiertos ('pendiente'/'en_proceso'), así un lead ya 'vendido' a mano o
        # 'cerrado' por el despacho se queda como está. `vendidos_total` SÍ se suma acá
        # (ver el comentario de la línea que lo hace): el PATCH del asesor ya no ocurre
        # porque el cierre marca el lead solo, y el contador se quedaba en cero.
        if cot.lead_id:
            # LOCK sobre la fila del LEAD, igual que en el PATCH del asesor: los DOS
            # caminos deciden con `_lead.estado` si esta venta ya se contó, así que los
            # dos tienen que bloquear la fila donde vive esa evidencia. Con el lock en
            # uno solo, un cierre y un PATCH simultáneos —o dos cierres— leían ambos el
            # mismo estado abierto y la ficha terminaba con DOS ventas donde hubo una:
            # reproducido 3 de 5 rondas, con 200 en las dos peticiones (silencioso).
            _lead = (db.query(MonzaLead).filter(MonzaLead.id == cot.lead_id)
                     .populate_existing().with_for_update().first())
            # 'rechazado' entra en la lista: cerrar la venta es la evidencia MÁS fuerte de
            # que ese lead no estaba rechazado (el cliente volvió y compró). Sin él, el
            # lead se quedaba en 'rechazado', el contador de ventas de la ficha nunca
            # sumaba, y la tarjeta del cliente terminaba mostrando «Vendidos: 0» junto a
            # un LTV con la plata de esa misma venta: dos números de la misma tarjeta
            # contradiciéndose. Los que NO entran siguen siendo 'vendido' y 'cerrado',
            # donde esta venta YA se contó.
            if _lead and (_lead.estado or "pendiente") in ("pendiente", "en_proceso", "rechazado"):
                _lead.estado = "vendido"
                _lead.fecha_actualizacion = datetime.utcnow()
                # `vendidos_total` de la ficha lo sumaba el PATCH del asesor al marcar
                # el lead vendido a mano. Como ahora el cierre lo marca solo, ese PATCH
                # ya no ocurre y el contador se quedaba en cero para siempre: se suma
                # ACÁ, en la MISMA transición (de abierto a vendido), así el número no
                # se duplica si después alguien re-marca a mano: el PATCH exige que el
                # estado cambie Y que no venga de 'vendido'/'cerrado', que son los dos
                # estados donde esta venta YA se contó. (Sin esa segunda condición el
                # equipo de testing reprodujo el doble conteo: cerrar la venta suma, el
                # despacho deja el lead en 'cerrado', y el asesor que ve un lead cerrado
                # y lo marca «vendido» —lo natural— sumaba de nuevo.) Va al cliente del
                # LEAD, que es de quien habla este contador (el LTV, que es plata, va al
                # cliente facturado).
                # …y solo si esta venta NO se contó ya antes. La transición del lead no
                # alcanza como evidencia: cerrar → revertir la venta → reabrir el lead a
                # mano → volver a cerrar es un camino permitido (los tres pasos devuelven
                # 200) y sumaba una SEGUNDA venta donde hubo una sola. La evidencia
                # correcta ya está en la base: si esta cotización tiene versiones de
                # cierre previas, es un RE-cierre y su venta ya está contada. Restar en la
                # reversión sería peor —el lead queda en 'vendido', el re-cierre ya no
                # sumaría por el gate de arriba, y la venta terminaría con el contador en
                # cero: se cambiaría un sobre-conteo por un sub-conteo.
                _es_recierre = (db.query(func.count(MonzaCotizacionCierre.id))
                                .filter(MonzaCotizacionCierre.cotizacion_id == cot.id)
                                .scalar() or 0) > 0
                if _lead.cliente_id and not _es_recierre:
                    # Mismo lock que el LTV: el contador también es
                    # lectura-modificación-escritura.
                    _c_lead = (db.query(MonzaCliente)
                               .filter(MonzaCliente.id == _lead.cliente_id)
                               .populate_existing().with_for_update().first())
                    if _c_lead:
                        _c_lead.vendidos_total = (_c_lead.vendidos_total or 0) + 1
                db.add(MonzaLeadActividad(
                    lead_id=_lead.id,
                    tipo="cambio_estado",
                    descripcion=f"Lead marcado como vendido por el cierre de {cot.numero}",
                    usuario=current_user.email.split("@")[0].title(),
                    usuario_id=getattr(current_user, "id", None),
                ))
        # La foto del cierre se toma DESPUÉS del volcado: tiene que retratar los datos con
        # que la venta quedó cerrada, no los que tenía antes. Si esta es la segunda o
        # tercera vez que se cierra, queda como una versión nueva y las anteriores siguen
        # ahí, marcadas como revertidas.
        _registrar_cierre(db, cot,
                          usuario_id=getattr(current_user, "id", None),
                          usuario_email=current_user.email)
    if body.estado == "despachado" and not cot.fecha_despacho:
        # Día de CHILE (ver monza_fechas): columna civil, no puede llevar el día UTC.
        cot.fecha_despacho = hoy_chile()
    if body.estado == "despachado":
        for _it in cot.items:
            if (_it.estado_linea or "cotizado") != "despachado":
                _it.estado_linea = "despachado"
        # Efectos SOLO en el despacho NUEVO (la transición manda): el re-PATCH no debe
        # volver a sumar el LTV ni re-cerrar el lead. El MISMO helper lo llama
        # cerrar_despacho, que es el camino real de la operación.
        if es_despacho_nuevo:
            aplicar_efectos_venta_despachada(db, cot)
    db.commit()
    db.refresh(cot)
    # Log/notif de VENTA solo en el cierre NUEVO: re-enviar 'vendida' (edición de la
    # OC, reintento del modal) no debe duplicar la notificación ni el log del cierre.
    _action = body.estado.upper() if body.estado else "UPDATE"
    if _action == "VENDIDA" and not es_cierre_nuevo:
        _action = "UPDATE"
    # Mismo amortiguador para el despacho: el re-PATCH queda como UPDATE en el log.
    if _action == "DESPACHADO" and not es_despacho_nuevo:
        _action = "UPDATE"
    _log(db, current_user.email, _action if _action in ("VENDIDA", "DESPACHADO") else "UPDATE",
         "cotizacion", cot.id, cot.numero,
         f"Estado → {cot.estado}" if body.estado else "Actualización")
    if es_cierre_nuevo:
        _cli = cot.cliente.nombre if cot.cliente else ""
        crear_notif(db, f"Nueva venta · {cot.numero}", f"{_cli} — ${int(cot.total_bruto or 0):,}".replace(",", "."), "success", "/monzaparts/ventas", "cotizacion", cot.id)
    elif es_despacho_nuevo:
        # Solo el despacho NUEVO notifica: el re-PATCH no repite la notificación.
        crear_notif(db, f"Despacho realizado · {cot.numero}", "Cotización despachada al cliente", "info", "/monzaparts/despachos", "cotizacion", cot.id)
    return _cot_dict(cot, db)



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
    # Mismas tres defensas que el repositorio de adjuntos (monza_router_documentos), que
    # es su gemelo: esta puerta se había quedado sin ninguna. Las constantes se importan
    # ahí dentro para no arriesgar un ciclo de imports a nivel de módulo.
    from monza_router_documentos import _EXT_PERMITIDAS, _MAX_DOC_BYTES
    ext = (os.path.splitext(file.filename)[1] if file.filename else ".pdf").lower()
    # (1) Lista blanca: fuera lo que un navegador ejecutaría.
    if ext not in _EXT_PERMITIDAS:
        raise HTTPException(
            status_code=400,
            detail=f"Tipo de archivo no permitido: '{ext or 'sin extensión'}'. "
                   "Acepta PDF, imágenes, Excel, Word, CSV, TXT y comprimidos.",
        )
    # (2) Tope de tamaño ANTES de materializar el archivo en memoria (`file.size` lo
    #     cuenta starlette del lado del servidor: no es un dato del cliente).
    if (file.size or 0) > _MAX_DOC_BYTES:
        raise HTTPException(status_code=400, detail="Archivo demasiado grande (máximo 20 MB)")
    filename = f"despacho_{cot_id}_{int(datetime.utcnow().timestamp())}{ext}"
    dest_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "despachos")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, filename)
    content_bytes = await file.read()
    if not content_bytes:
        raise HTTPException(status_code=400, detail="El archivo llegó vacío: súbelo de nuevo")
    if len(content_bytes) > _MAX_DOC_BYTES:
        raise HTTPException(status_code=400, detail="Archivo demasiado grande (máximo 20 MB)")
    with open(dest, "wb") as f:
        f.write(content_bytes)
    cot.documento_path = filename
    cot.tipo_documento = tipo
    # (3) Si el commit falla, el archivo no queda plantado en disco para siempre.
    try:
        db.commit()
    except Exception:
        db.rollback()
        try:
            os.remove(dest)
        except OSError:
            pass
        raise
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
    ORANGE   = colors.HexColor("#C92A3D")
    PEACH_BG = colors.HexColor("#F4CCCC")
    AMBER_H  = colors.HexColor("#EA9999")
    DARK     = colors.HexColor("#1E293B")
    GRAY_ALT = colors.HexColor("#FAFAFA")
    LGRAY    = colors.HexColor("#E2E8F0")
    WHITE    = colors.white
    TOTAL_BG = colors.HexColor("#C92A3D")

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
    LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "logo_monzaparts.png")
    if os.path.exists(LOGO_PATH):
        logo = Image(LOGO_PATH, width=3.5 * cm, height=2.5 * cm, kind="proportional")
    else:
        logo = [Paragraph("MONZAPARTS", ps("logo_nm", 13.5, bold=True, color=ORANGE)),
                Paragraph("REPUESTOS AUTOMOTRICES", ps("logo_sb", 6.5, color=colors.HexColor("#94A3B8")))]

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
        [Paragraph("Comentario:", lbl_b), Paragraph(cot.oc_cliente or "", val_b),
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
    # 8 columnas (arreglos del equipo 2026-08-21: el PDF no mostraba calidad ni plazo):
    # Repuesto 4.1 | Marca 2.0 | Procedencia 2.2 | Calidad 2.0 | Cant. 1.1 |
    # Precio Unit. 2.2 | TOTAL 2.4 | Plazo entrega 2.0  → Σ = 18.0 cm ✓
    # Anchos MEDIDOS (stringWidth Helvetica-Bold 9 + padding 4pt/lado): la cabecera
    # «Procedencia» exige ≥2.2, «Aftermarket» ≥2.0 y «Cantidad» no cabe — va «Cant.»
    # (vocabulario ya usado en Abastecimiento). No angostar sin re-medir.
    col_ws = [4.1*cm, 2.0*cm, 2.2*cm, 2.0*cm, 1.1*cm, 2.2*cm, 2.4*cm, 2.0*cm]

    thead = [
        Paragraph("<b>Repuesto</b>",      hdr_l),
        Paragraph("<b>Marca</b>",         hdr_c),
        Paragraph("<b>Procedencia</b>",   hdr_c),
        Paragraph("<b>Calidad</b>",       hdr_c),
        Paragraph("<b>Cant.</b>",         hdr_c),
        Paragraph("<b>Precio Unit.</b>",  hdr_r),
        Paragraph("<b>TOTAL</b>",         hdr_r),
        Paragraph("<b>Plazo entrega</b>", hdr_c),
    ]

    def fmt_clp(n):
        if not n:
            return "—"
        return "$" + f"{int(n):,}".replace(",", ".")

    # La calidad se guarda en minúsculas (la calculadora la baja al aplicar); el PDF la
    # imprime con la etiqueta de la pantalla. 'sin_calificar'/NULL = "—"; un valor libre
    # (Remanufacturado, Usado) pasa capitalizado tal cual.
    _CALIDAD_PDF = {"genuine": "Genuine", "oem": "OEM", "aftermarket": "Aftermarket"}

    def fmt_calidad(c):
        # strip().lower(): 'sin_calificar' puede venir con mayúsculas o espacios por
        # ItemUpdate/CotItemIn (texto libre) — el guion no debe esquivarse por casing.
        if not c or c.strip().lower() == "sin_calificar":
            return "—"
        c = c.strip()
        return _CALIDAD_PDF.get(c.lower(), c[:1].upper() + c[1:])

    irows = [thead]
    for it in cot.items:
        irows.append([
            Paragraph(it.descripcion or "", it_l),
            Paragraph(it.marca or "", it_c),
            Paragraph(it.procedencia or "—", it_c),
            Paragraph(fmt_calidad(it.calidad), it_c),
            Paragraph(str(it.cantidad), it_c),
            Paragraph(fmt_clp(it.precio_unitario_clp), it_r),
            Paragraph(fmt_clp(it.subtotal_clp), it_r),
            # plazo_s (8pt centrado) a propósito: «7-10 días hábiles» envuelve en 2.0 cm
            # con más holgura que a 9pt. El estilo existía huérfano desde que la columna
            # Plazo se quitó — vuelve a su uso original.
            Paragraph(it.plazo_entrega or "—", plazo_s),
        ])

    n_data = len(irows) - 1
    row_bgs = []
    for i in range(1, n_data + 1):
        bg = WHITE if i % 2 == 1 else colors.HexColor("#F8F9FA")
        row_bgs.append(("BACKGROUND", (0, i), (-1, i), bg))

    # repeatRows=1: con 2+ páginas la cabecera se repite — sin esto, las cotizaciones
    # largas mostraban la página 2 sin rótulos (defecto que las 2 columnas nuevas
    # volvían visible: quedaban sin nombre justo donde el equipo las pidió).
    items_tbl = Table(irows, colWidths=col_ws, repeatRows=1)
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
    # Anchos PROPIOS, no col_ws: con 8 columnas de ítems, ninguna pareja de columnas
    # compartidas aguanta «TOTAL NETO.» (66.7pt en bold 9) ni un total de 9 dígitos sin
    # partirse en dos líneas. La banda label+valor termina en 10.4+3.0+2.6 = 16.0 cm,
    # alineada con el borde derecho de la columna TOTAL de la tabla de ítems.
    tot_col_ws = [10.4 * cm, 3.0 * cm, 2.6 * cm, 2.0 * cm]
    tot_rows = [
        ["", Paragraph("<b>TOTAL NETO.</b>", tot_lbl), Paragraph(fmt_clp(cot.total_neto), tot_val), ""],
        ["", Paragraph("IVA.",               tot_lbl), Paragraph(fmt_clp(cot.iva_monto),  tot_val), ""],
        ["", Paragraph("<b>TOTAL.</b>",       grand_lbl),Paragraph(fmt_clp(cot.total_bruto),grand_val), ""],
    ]
    tot_tbl = Table(tot_rows, colWidths=tot_col_ws)
    tot_tbl.setStyle(TableStyle([
        ("FONTSIZE",     (0, 0), (-1, -1), 9),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",   (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        # Fondo naranja en la fila TOTAL (banda label+valor, fila 2)
        ("BACKGROUND",   (1, 2), (2, 2), TOTAL_BG),
        # Bordes de la banda de totales
        ("BOX",          (1, 0), (2, 2), 0.5, LGRAY),
        ("LINEBELOW",    (1, 0), (2, 1), 0.3, LGRAY),
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
