import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config import settings
from database import Base, engine
from routers import auth, cotizaciones, cotizador, compras, clientes, ventas, facturas
from monza_router_config import router as monza_config_router
from monza_router_leads import router as monza_leads_router
from monza_router_cotizador import router as monza_cotizador_router
from monza_router_cotizaciones import router as monza_cotizaciones_router
from monza_router_ventas import router as monza_ventas_router
from monza_router_despachos import router as monza_despachos_router
from monza_router_logs import router as monza_logs_router
from monza_router_tickets import router as monza_tickets_router
from monza_router_abastecimiento import router as monza_abastecimiento_router
from monza_router_bodega import router as monza_bodega_router
from monza_router_logistica import router as monza_logistica_router
from monza_router_notificaciones import router as monza_notif_router
from monza_router_integraciones import router as monza_integraciones_router
from monza_recepcion_nacional.router import router as monza_recep_nac_router  # camino físico de la compra nacional Monza
from routers import contabilidad
from routers import venta_clp
from embarques_pricing.router import router as embarques_pricing_router
from compras_contab.router import router as compras_contab_router
from recepcion_nacional.router import router as recepcion_nacional_router  # camino físico de la compra nacional
from tesoreria.router import router as tesoreria_router
from wasabil_dte.router import router as wasabil_dte_router  # módulo aislado (Wasabil DTE: guías de despacho electrónicas al SII)
from monza_router_documentos import router as monza_docs_router
from monza_router_catalog import router as monza_catalog_router
from monza_router_clientes import router as monza_clientes_router
from routers.worker import worker_router, scraping_router
from routers import notificaciones, bodega
from routers import despachos
from routers import tickets

# Crear tablas que falten. OJO: NO agrega columnas a tablas existentes — para eso
# correr los scripts de backend/migrations/ (leccion jul 2026: error 1054 en cascada).
if settings.AUTO_CREATE_TABLES:
    Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="MachParts API",
    description="Validador de cotizaciones CAT - parts.cat.com/es/finningchile",
    version="2.1.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(auth.router,           prefix="/api")
app.include_router(cotizaciones.router,   prefix="/api")
app.include_router(cotizador.router,      prefix="/api")
app.include_router(compras.router,        prefix="/api")
app.include_router(clientes.router,       prefix="/api")
app.include_router(ventas.router,         prefix="/api")
app.include_router(worker_router,         prefix="/api")
app.include_router(scraping_router,       prefix="/api")
app.include_router(facturas.router,       prefix="/api")
app.include_router(notificaciones.router, prefix="/api")
app.include_router(bodega.router,         prefix="/api")
app.include_router(despachos.router,      prefix="/api")
app.include_router(tickets.router,        prefix="/api")
app.include_router(monza_config_router)
app.include_router(monza_leads_router)
app.include_router(monza_cotizador_router)
app.include_router(monza_cotizaciones_router)
app.include_router(monza_ventas_router)
app.include_router(monza_despachos_router)
app.include_router(monza_logs_router)
app.include_router(monza_tickets_router)
app.include_router(monza_abastecimiento_router)
app.include_router(monza_bodega_router)
app.include_router(monza_logistica_router)
app.include_router(monza_notif_router)
app.include_router(monza_integraciones_router)
app.include_router(monza_recep_nac_router)  # /api/monza/recepcion-nacional (SIN prefix extra: el router ya lo trae)
app.include_router(contabilidad.router, prefix="/api")
app.include_router(venta_clp.router)
app.include_router(embarques_pricing_router, prefix="/api")
app.include_router(compras_contab_router, prefix="/api")
app.include_router(recepcion_nacional_router, prefix="/api")  # /api/recepcion-nacional
app.include_router(tesoreria_router, prefix="/api")
app.include_router(wasabil_dte_router, prefix="/api")  # /api/wasabil (guías de despacho electrónicas)
# Contabilidad MonzaParts, activable por flag. El import va DENTRO del if a
# proposito: con el flag apagado los modelos no se cargan, asi create_all no crea
# sus tablas (asi corre PROD desde 2026-07-15; ver deploy/README.md).
if settings.MONZA_CONTAB_ENABLED:
    from monza_contabilidad.router import router as monza_contabilidad_router
    from monza_embarques_pricing.router import router as monza_embarques_pricing_router
    from monza_compras_contab.router import router as monza_compras_contab_router
    from monza_tesoreria.router import router as monza_tesoreria_router
    # Wasabil DTE Monza (guías 52 al SII con el RUT de MonzaParts): dentro del gate
    # a propósito — apagado, el módulo ni se importa ni create_all crea su tabla.
    from monza_wasabil_dte.router import router as monza_wasabil_router
    app.include_router(monza_contabilidad_router)
    app.include_router(monza_embarques_pricing_router)
    app.include_router(monza_compras_contab_router)
    app.include_router(monza_tesoreria_router)
    app.include_router(monza_wasabil_router, prefix="/api")  # /api/monza/wasabil/...
app.include_router(monza_docs_router)
app.include_router(monza_catalog_router)
app.include_router(monza_clientes_router)


@app.get("/api/health")
def health():
    return {"status": "ok", "version": "2.1.0"}


# Ensure required upload directories exist at startup
os.makedirs("results", exist_ok=True)
os.makedirs("uploads", exist_ok=True)
os.makedirs("uploads/bodega", exist_ok=True)

# Serve uploaded bodega photos as static files
app.mount("/uploads/bodega", StaticFiles(directory="uploads/bodega"), name="bodega_uploads")


@app.on_event("startup")
def startup_event():
    # Alarma si un merge/deploy futuro pisa el isolation_level de database.py: los topes
    # de plata dependen de READ COMMITTED como segunda capa (docs/regla-lecturas-de-plata.md).
    try:
        from database import engine as _eng
        from sqlalchemy import text as _text
        with _eng.connect() as _c:
            _iso = _c.execute(_text("SELECT @@transaction_isolation")).scalar()
        # flush=True: con el proceso en segundo plano (nohup/systemd) stdout va a un pipe
        # con buffer completo y el aviso no aparecería en el log del servidor.
        print(f"[startup] isolation={_iso}"
              + ("" if str(_iso).upper().replace("_", "-") == "READ-COMMITTED"
                 else "  <-- ESPERADO READ-COMMITTED! revisa backend/database.py"),
              flush=True)
    except Exception as e:
        print(f"[startup] no se pudo verificar el isolation level: {e}", flush=True)
    try:
        from scheduler import start_scheduler
        start_scheduler()
    except Exception as e:
        print(f"[startup] scheduler failed: {e}")
