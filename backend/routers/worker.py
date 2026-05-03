"""
Router stub para worker y scraping.
Endpoints usados por el frontend para consultar estado del worker remoto.
"""
from fastapi import APIRouter, Depends
from models.models import User
from auth import get_current_user

worker_router = APIRouter(prefix="/worker", tags=["worker"])
scraping_router = APIRouter(prefix="/scraping", tags=["scraping"])


@worker_router.get("/status")
def worker_status(current_user: User = Depends(get_current_user)):
    return {"active": False, "last_seen": None, "version": "1.0.0", "configured": False}


@worker_router.get("/heartbeat")
def worker_heartbeat(current_user: User = Depends(get_current_user)):
    return {"heartbeat": False}


@worker_router.get("/jobs")
def worker_jobs(current_user: User = Depends(get_current_user)):
    return []


@worker_router.post("/submit/{cotizacion_id}")
def worker_submit(cotizacion_id: int, current_user: User = Depends(get_current_user)):
    return {"ok": True, "cotizacion_id": cotizacion_id}


@scraping_router.get("/status")
def scraping_status(current_user: User = Depends(get_current_user)):
    return {"active": False}


@scraping_router.get("/pending")
def scraping_pending(current_user: User = Depends(get_current_user)):
    return []


@scraping_router.get("/results")
def scraping_results(current_user: User = Depends(get_current_user)):
    return []


@scraping_router.post("/results")
def scraping_post_results(current_user: User = Depends(get_current_user)):
    return {"ok": True}
