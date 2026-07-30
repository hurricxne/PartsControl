"""Crea las tablas tickets + ticket_respuestas (MachParts). Idempotente."""
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from database import engine, Base  # noqa: E402
import models.models  # noqa: E402,F401  registra Ticket + TicketRespuesta
from models.models import Ticket, TicketRespuesta  # noqa: E402


def run():
    Base.metadata.create_all(bind=engine, tables=[Ticket.__table__, TicketRespuesta.__table__])
    print("[tickets] tablas tickets + ticket_respuestas OK (checkfirst)")


if __name__ == "__main__":
    run()
