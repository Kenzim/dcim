"""Fail-open audit logging for commerce and account events."""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.commerce_audit import UserAuditEvent

logger = logging.getLogger(__name__)


class AuditService:
    @staticmethod
    def log(
        db: Session,
        *,
        actor_user_id: Optional[int] = None,
        subject_user_id: Optional[int] = None,
        billing_account_id: Optional[int] = None,
        action: str,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        impersonated_by: Optional[int] = None,
    ) -> None:
        try:
            row = UserAuditEvent(
                actor_user_id=actor_user_id,
                subject_user_id=subject_user_id,
                billing_account_id=billing_account_id,
                action=action[:64],
                resource_type=resource_type[:64] if resource_type else None,
                resource_id=str(resource_id)[:255] if resource_id is not None else None,
                ip=ip[:45] if ip else None,
                user_agent=user_agent[:512] if user_agent else None,
                event_metadata=dict(metadata or {}),
                impersonated_by=impersonated_by,
            )
            db.add(row)
            db.flush()
        except Exception:
            logger.warning(
                "Audit log failed for action=%s resource=%s:%s",
                action,
                resource_type,
                resource_id,
                exc_info=True,
            )
