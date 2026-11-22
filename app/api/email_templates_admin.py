"""Admin API for the email templates (docs/billing-phase2-design.md 12.4).

Every write is audited (``billing.email_template.*``). A save is validated by
rendering the subject and both bodies against the event's sample data with
``StrictUndefined``; an invalid template answers 422 with the field, line and
message of each error.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.auth import require_admin
from app.core.database import get_db
from app.core.openapi_responses import COMMON_ERROR_RESPONSES
from app.core.rate_limit import enforce_rate_limit
from app.models.commerce_email import EmailTemplate, EmailTemplateVersion
from app.models.product_catalog import Product
from app.models.user import User
from app.services.audit_service import AuditService
from app.services.email_templates import admin as templates_admin
from app.services.email_templates import catalogue, sandbox

DbDep = Annotated[Session, Depends(get_db)]
AdminDep = Annotated[dict, Depends(require_admin)]

router = APIRouter(
    prefix="/admin/commerce/email-templates",
    tags=["email-templates-admin"],
    dependencies=[Depends(require_admin)],
    responses=COMMON_ERROR_RESPONSES,
)

_LOCALE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$")
_ADDRESS = re.compile(r"^[^@\s,;<>\"'\\]+@[^@\s,;<>\"'\\]+\.[^@\s,;<>\"'\\]+$")
_NOT_FOUND = "Email template not found"
PREVIEW_LIMIT = 60
PREVIEW_WINDOW_SECONDS = 60


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _serialise(row: EmailTemplate, *, body: bool = False) -> dict[str, Any]:
    spec = catalogue.EVENTS.get(row.event_key)
    data: dict[str, Any] = {
        "id": row.id,
        "event_key": row.event_key,
        "event_title": spec.title if spec is not None else row.event_key,
        "locale": row.locale,
        "product_scope": row.product_scope,
        "subject": row.subject,
        "enabled": bool(row.enabled),
        "version": row.version,
        "updated_by_user_id": row.updated_by_user_id,
        "updated_at": _iso(row.updated_at),
    }
    if body:
        data["body_text"] = row.body_text
        data["body_html"] = row.body_html
    return data


def _invalid(exc: templates_admin.TemplateInvalid) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={"code": "template_invalid", "message": str(exc), "errors": exc.as_list()},
    )


def _get(db: Session, template_id: int, *, lock: bool = False) -> EmailTemplate:
    row = db.get(EmailTemplate, template_id, with_for_update=lock)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    return row


def _audit(db: Session, auth: dict, action: str, row: EmailTemplate, **metadata: Any) -> None:
    AuditService.log(
        db,
        actor_user_id=auth["user_id"],
        action=f"billing.email_template.{action}",
        resource_type="email_template",
        resource_id=str(row.id),
        metadata={"event_key": row.event_key, "locale": row.locale, "product_scope": row.product_scope, **metadata},
    )


@router.get("", responses=COMMON_ERROR_RESPONSES)
def list_templates(
    db: DbDep,
    event_key: Optional[str] = None,
    locale: Optional[str] = None,
):
    stmt = select(EmailTemplate).order_by(EmailTemplate.event_key, EmailTemplate.locale, EmailTemplate.product_scope)
    if event_key:
        stmt = stmt.where(EmailTemplate.event_key == event_key)
    if locale:
        stmt = stmt.where(EmailTemplate.locale == locale)
    return {"templates": [_serialise(row) for row in db.execute(stmt).scalars()]}


@router.get("/events", responses=COMMON_ERROR_RESPONSES)
def list_events(db: DbDep):
    counts: dict[str, int] = {}
    for key in db.execute(select(EmailTemplate.event_key)).scalars():
        counts[key] = counts.get(key, 0) + 1
    return {
        "events": [{**spec.as_dict(), "templates": counts.get(spec.key, 0)} for spec in catalogue.EVENTS.values()],
        "filters": list(sandbox.ALLOWED_FILTERS),
        "tests": list(sandbox.ALLOWED_TESTS),
        "limits": {
            "source_bytes": sandbox.MAX_SOURCE_BYTES,
            "output_chars": sandbox.MAX_OUTPUT_CHARS,
            "elements": sandbox.MAX_NODES,
            "subject_chars": templates_admin.MAX_SUBJECT_SOURCE,
            "test_sends_per_hour": templates_admin.TEST_SEND_LIMIT,
        },
    }


class CreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_key: str = Field(max_length=64)
    locale: str = Field(default="en", max_length=16)
    product_scope: int = Field(default=0, ge=0)
    subject: str = Field(max_length=templates_admin.MAX_SUBJECT_SOURCE)
    body_text: str
    body_html: Optional[str] = None
    enabled: bool = False


@router.post("", status_code=status.HTTP_201_CREATED, responses=COMMON_ERROR_RESPONSES)
def create_template(body: CreateBody, auth: AdminDep, db: DbDep):
    spec = catalogue.EVENTS.get(body.event_key)
    if spec is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown email event")
    if not _LOCALE.match(body.locale):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid locale")
    if body.product_scope and not spec.accepts_product:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Only the welcome event accepts a per-product template",
        )
    if body.product_scope and db.get(Product, body.product_scope) is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Product not found")
    exists = db.execute(
        select(EmailTemplate.id).where(
            EmailTemplate.event_key == body.event_key,
            EmailTemplate.locale == body.locale,
            EmailTemplate.product_scope == body.product_scope,
        )
    ).first()
    if exists is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A template for this variant already exists")
    try:
        row = templates_admin.create_template(
            db,
            event_key=body.event_key,
            locale=body.locale,
            product_scope=body.product_scope,
            subject=body.subject,
            body_text=body.body_text,
            body_html=body.body_html,
            enabled=body.enabled,
            actor_user_id=auth["user_id"],
        )
    except templates_admin.TemplateInvalid as exc:
        raise _invalid(exc) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A template for this variant already exists") from exc
    _audit(db, auth, "create", row, enabled=row.enabled, version=row.version)
    db.commit()
    db.refresh(row)
    return _serialise(row, body=True)


@router.get("/{template_id}", responses=COMMON_ERROR_RESPONSES)
def get_template(template_id: int, db: DbDep):
    row = _get(db, template_id)
    spec = catalogue.EVENTS.get(row.event_key)
    return {**_serialise(row, body=True), "event": spec.as_dict() if spec is not None else None}


class UpdateBody(BaseModel):
    """The full text, or only ``enabled`` to switch the template on or off.

    A save whose text equals the stored text keeps the version number and
    writes no version row.
    """

    model_config = ConfigDict(extra="forbid")

    subject: Optional[str] = Field(default=None, max_length=templates_admin.MAX_SUBJECT_SOURCE)
    body_text: Optional[str] = None
    body_html: Optional[str] = None
    enabled: Optional[bool] = None

    @model_validator(mode="after")
    def _text_comes_whole(self) -> "UpdateBody":
        if (self.subject is None) != (self.body_text is None):
            raise ValueError("subject and body_text are sent together")
        if self.subject is None and ("body_html" in self.model_fields_set or self.enabled is None):
            raise ValueError("send the text (subject and body_text), or only enabled")
        return self


@router.put("/{template_id}", responses=COMMON_ERROR_RESPONSES)
def update_template(template_id: int, body: UpdateBody, auth: AdminDep, db: DbDep):
    row = _get(db, template_id, lock=True)
    was_enabled = bool(row.enabled)
    text_sent = body.subject is not None
    try:
        templates_admin.save_template(
            db,
            row,
            subject=body.subject if text_sent else row.subject,
            body_text=body.body_text if text_sent else row.body_text,
            body_html=body.body_html if text_sent else row.body_html,
            enabled=body.enabled,
            actor_user_id=auth["user_id"],
        )
    except templates_admin.TemplateInvalid as exc:
        db.rollback()
        raise _invalid(exc) from exc
    _audit(db, auth, "update", row, version=row.version, enabled_before=was_enabled, enabled_after=bool(row.enabled))
    db.commit()
    db.refresh(row)
    return _serialise(row, body=True)


class PreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    variables: Optional[dict[str, Any]] = None
    subject: Optional[str] = Field(default=None, max_length=templates_admin.MAX_SUBJECT_SOURCE)
    body_text: Optional[str] = None
    body_html: Optional[str] = None


@router.post("/{template_id}/preview", responses=COMMON_ERROR_RESPONSES)
def preview_template(template_id: int, auth: AdminDep, db: DbDep, body: Optional[PreviewBody] = None):
    """Render the template, or a draft sent with the request, against samples. Never sends.

    Limited to 60 previews per admin per minute.
    """
    enforce_rate_limit(f"email_template_preview:{auth['user_id']}", PREVIEW_LIMIT, PREVIEW_WINDOW_SECONDS)
    row = _get(db, template_id)
    body = body or PreviewBody()
    draft_html: Any = body.body_html if "body_html" in body.model_fields_set else templates_admin.KEEP
    try:
        return templates_admin.preview(
            row,
            variables=body.variables,
            subject=body.subject,
            body_text=body.body_text,
            body_html=draft_html,
        )
    except templates_admin.TemplateInvalid as exc:
        raise _invalid(exc) from exc
    except sandbox.EmailTemplateError as exc:
        raise _invalid(templates_admin.TemplateInvalid([exc])) from exc


class SendTestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_address: Optional[str] = Field(default=None, max_length=320)
    variables: Optional[dict[str, Any]] = None


@router.post("/{template_id}/test-send", status_code=status.HTTP_201_CREATED, responses=COMMON_ERROR_RESPONSES)
def test_send_template(template_id: int, auth: AdminDep, db: DbDep, body: Optional[SendTestBody] = None):
    """Queue a marked test message to the calling admin (or a given address); 10 per admin per hour."""
    row = _get(db, template_id)
    body = body or SendTestBody()
    address = (body.to_address or "").strip()
    if not address:
        user = db.get(User, auth["user_id"])
        address = (user.email if user is not None else "") or ""
    if not _ADDRESS.match(address):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="A valid recipient address is required")
    try:
        message = templates_admin.queue_test_send(
            db, row, admin_user_id=auth["user_id"], to_address=address, variables=body.variables
        )
    except templates_admin.SendRateLimited as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"At most {templates_admin.TEST_SEND_LIMIT} test sends per hour",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
    except templates_admin.TemplateInvalid as exc:
        raise _invalid(exc) from exc
    _audit(db, auth, "test_send", row, message_id=message.id, to_address=address, version=row.version)
    db.commit()
    return {"message_id": message.id, "to_address": message.to_address, "subject": message.subject, "status": "queued"}


@router.post("/{template_id}/reset", responses=COMMON_ERROR_RESPONSES)
def reset_template(template_id: int, auth: AdminDep, db: DbDep):
    """Save the built-in text of the event as a new version."""
    row = _get(db, template_id, lock=True)
    templates_admin.reset_template(db, row, actor_user_id=auth["user_id"])
    _audit(db, auth, "reset", row, version=row.version)
    db.commit()
    db.refresh(row)
    return _serialise(row, body=True)


@router.get("/{template_id}/versions", responses=COMMON_ERROR_RESPONSES)
def list_versions(template_id: int, db: DbDep, limit: Annotated[int, Query(ge=1, le=200)] = 50):
    _get(db, template_id)
    rows = db.execute(
        select(EmailTemplateVersion)
        .where(EmailTemplateVersion.template_id == template_id)
        .order_by(EmailTemplateVersion.version.desc())
        .limit(limit)
    ).scalars()
    return {
        "versions": [
            {
                "version": row.version,
                "subject": row.subject,
                "body_text": row.body_text,
                "body_html": row.body_html,
                "saved_by_user_id": row.saved_by_user_id,
                "saved_at": _iso(row.saved_at),
            }
            for row in rows
        ]
    }


@router.post("/{template_id}/restore/{version}", responses=COMMON_ERROR_RESPONSES)
def restore_version(template_id: int, version: int, auth: AdminDep, db: DbDep):
    """Save an older version's text as a new version."""
    row = _get(db, template_id, lock=True)
    old = templates_admin.version_row(db, template_id, version)
    if old is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found")
    try:
        templates_admin.restore_version(db, row, old, actor_user_id=auth["user_id"])
    except templates_admin.TemplateInvalid as exc:
        db.rollback()
        raise _invalid(exc) from exc
    _audit(db, auth, "restore", row, restored_version=version, version=row.version)
    db.commit()
    db.refresh(row)
    return _serialise(row, body=True)

