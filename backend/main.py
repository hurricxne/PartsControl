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
from monza_router_abastecimiento import router as monza_abastecimiento_router
from monza_router_bodega import router as monza_bodega_router
from monza_router_logistica import router as monza_logistica_router
from monza_router_notificaciones import router as monza_notif_router
from monza_router_documentos import router as monza_docs_router
from monza_router_catalog import router as monza_catalog_router
from monza_router_clientes import router as monza_clientes_router
from routers.worker import worker_router, scraping_router
from routers import notificaciones, bodega
from routers import despachos

# Create / migrate tables
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
app.include_router(monza_config_router)
app.include_router(monza_leads_router)
app.include_router(monza_cotizador_router)
app.include_router(monza_cotizaciones_router)
app.include_router(monza_ventas_router)
app.include_router(monza_despachos_router)
app.include_router(monza_logs_router)
app.include_router(monza_abastecimiento_router)
app.include_router(monza_bodega_router)
app.include_router(monza_logistica_router)
app.include_router(monza_notif_router)
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
    try:
        from scheduler import start_scheduler
        start_scheduler()
    except Exception as e:
        print(f"[startup] scheduler failed: {e}")
