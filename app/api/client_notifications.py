"""Client portal: opt-in notifications about the client's own account and services."""

from __future__ import annotations

from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.client_mcp_keys import ClientDep
from app.core.database import get_db
from app.services import client_notifications as svc
from app.services.audit_service import AuditService

DbDep = Annotated[Session, Depends(get_db)]

router = APIRouter(prefix="/client/notifications", tags=["client-notifications"])


class PreferencesBody(BaseModel):
    categories: Optional[dict[str, bool]] = None
    email_enabled: Optional[bool] = None
    webhook_url: Optional[str] = Field(default=None, max_length=2048)
    regenerate_secret: bool = False


def _own_session(auth: dict) -> int:
    if auth.get("impersonated_by"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Notification settings cannot be changed during impersonation",
        )
    return int(auth["user_id"])


@router.get("")
def get_notifications(auth: ClientDep, db: DbDep) -> dict[str, Any]:
    result = svc.get_preferences(db, int(auth["user_id"]))
    result["can_manage"] = not auth.get("impersonated_by")
    return result


@router.put("")
def put_notifications(body: PreferencesBody, auth: ClientDep, db: DbDep) -> dict[str, Any]:
    user_id = _own_session(auth)
    try:
        result = svc.update_preferences(db, user_id, **body.model_dump(exclude_unset=True))
    except svc.PreferenceError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    changed = sorted(body.model_dump(exclude_unset=True))
    AuditService.log(
        db,
        actor_user_id=user_id,
        subject_user_id=user_id,
        action="notification_prefs_changed",
        resource_type="notification_prefs",
        resource_id=str(user_id),
        metadata={"fields": changed},
    )
    db.commit()
    return result


@router.post("/test")
def send_test(auth: ClientDep, db: DbDep) -> dict[str, Any]:
    user_id = _own_session(auth)
    queued = svc.send_test(db, user_id)
    if not queued:
        raise HTTPException(status_code=400, detail="Turn on email or add a webhook first")
    return {"queued": queued}
