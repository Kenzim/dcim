"""Server lock: a locked server must never be reinstalled or wiped by any path."""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session


def locked_message(server) -> str:
    reason = (getattr(server, "lock_reason", None) or "").strip()
    return f"Server is locked: {reason or 'no reason given'}"


class ServerLockedError(HTTPException):
    """409 raised when an install or wipe is attempted on a locked server."""

    def __init__(self, server) -> None:
        super().__init__(status_code=status.HTTP_409_CONFLICT, detail=locked_message(server))


def ensure_server_unlocked(server) -> None:
    """Raise ``ServerLockedError`` if the server object is locked."""
    if server is not None and getattr(server, "locked", False):
        raise ServerLockedError(server)


def ensure_server_id_unlocked(db: Session, server_id: Optional[int]) -> None:
    """Same check by id, reading the current row (not a stale cached value)."""
    if server_id is None:
        return
    from app.models.server import Server

    server = db.query(Server).filter(Server.id == server_id).first()
    if server is not None:
        db.refresh(server)
        ensure_server_unlocked(server)
