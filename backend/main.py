from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import os

from config import settings
from database import Base, engine
from routers import auth, cotizaciones, cotizador, compras, clientes, ventas, facturas
from routers.worker import worker_router, scraping_router

# Create / migrate tables
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="MachParts API",
    description="Validador de cotizaciones CAT - parts.cat.com/es/finningchile",
    version="2.0.0",
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
app.include_router(auth.router,          prefix="/api")
app.include_router(cotizaciones.router,  prefix="/api")
app.include_router(cotizador.router,     prefix="/api")
app.include_router(compras.router,       prefix="/api")
app.include_router(clientes.router,      prefix="/api")
app.include_router(ventas.router,        prefix="/api")
app.include_router(worker_router,        prefix="/api")
app.include_router(scraping_router,      prefix="/api")
app.include_router(facturas.router,      prefix="/api")


@app.get("/api/health")
def health():
    return {"status": "ok", "version": "2.0.0"}


# Static file directories
os.makedirs("results", exist_ok=True)
os.makedirs("uploads", exist_ok=True)
