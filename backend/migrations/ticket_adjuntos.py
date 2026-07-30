"""Crea la tabla ticket_adjuntos (MachParts) y el dir uploads/tickets. Idempotente."""
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from database import engine, Base  # noqa: E402
import models.models  # noqa: E402,F401  registra TicketAdjunto
from models.models import TicketAdjunto  # noqa: E402


def run():
    Base.metadata.create_all(bind=engine, tables=[TicketAdjunto.__table__])
    os.makedirs("uploads/tickets", exist_ok=True)
    print("[ticket_adjuntos] tabla ticket_adjuntos + uploads/tickets OK (checkfirst)")


if __name__ == "__main__":
    run()
