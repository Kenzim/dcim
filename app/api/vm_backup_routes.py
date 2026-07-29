"""Shared request models + HTTP mapping for VM backup endpoints."""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException, status
from pydantic import BaseModel, Field

from app.services.vm_backup_service import (
    BackupConfigError,
    BackupForbiddenError,
    BackupQuotaError,
)


class BackupCreateBody(BaseModel):
    notes: Optional[str] = None
    mode: str = Field(default="snapshot", description="snapshot | suspend | stop")
    wait: bool = False


class BackupMutateBody(BaseModel):
    volid: str
    storage: Optional[str] = None
    wait: bool = False
    start: bool = True
    vm_template_id: Optional[int] = Field(
        default=None,
        description="Optional template id matching the backup OS; clears stale metadata when omitted",
    )


def map_backup_error(exc: Exception) -> HTTPException:
    if isinstance(exc, BackupConfigError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, BackupQuotaError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if isinstance(exc, BackupForbiddenError):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=str(exc) or "Backup operation failed",
    )
