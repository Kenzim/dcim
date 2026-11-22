"""Editing, previewing and test-sending email templates (docs/billing-phase2-design.md 12.4).

The admin API (``app/api/email_templates_admin.py``) is a thin layer over
these functions. Nothing here commits; the caller does.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.commerce_email import (
    EmailMessage,
    EmailMessageStatus,
    EmailTemplate,
    EmailTemplateVersion,
)
from app.models.user import User
from app.services.email_templates import catalogue
from app.services.email_templates.render import render_stored
from app.services.email_templates.sandbox import (
        EmailTemplateError,
    compile_source,
    render_source,
    sanitise_variables,
)

TEST_SEND_LIMIT = 10
TEST_SEND_WINDOW = timedelta(hours=1)
TEST_RELATED_TYPE = "email_template_test"
TEST_KEY_PREFIX = "email_template_test"
MAX_SUBJECT_SOURCE = 255


KEEP: Any = object()


class TemplateInvalid(Exception):
    """One or more fields of a template do not compile or render against the samples."""

    def __init__(self, errors: list[EmailTemplateError]):
        super().__init__("; ".join(error.message for error in errors))
        self.errors = errors

    def as_list(self) -> list[dict[str, Any]]:
        return [error.as_dict() for error in self.errors]


class SendRateLimited(Exception):
    def __init__(self, retry_after_seconds: int):
        super().__init__("Too many test sends")
        self.retry_after_seconds = retry_after_seconds


def sample_variables(event_key: str, overrides: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    spec = catalogue.EVENTS[event_key]
    variables = dict(spec.samples)
    allowed = set(variables)
    for key, value in (overrides or {}).items():
        if key not in allowed:
            raise TemplateInvalid(
                [EmailTemplateError(f"{key!r} is not a variable of this event", kind="undefined", field="variables")]
            )
        variables[key] = value
    return variables


def validate(
    event_key: str,
    *,
    subject: str,
    body_text: str,
    body_html: Optional[str],
    variables: Optional[Mapping[str, Any]] = None,
) -> tuple[str, str, Optional[str]]:
    """Compile and render every field against sample data; collects every error before raising."""
    data = sanitise_variables(variables if variables is not None else sample_variables(event_key))
    errors: list[EmailTemplateError] = []
    results: dict[str, Optional[str]] = {"subject": None, "body_text": None, "body_html": None}
    if len(subject) > MAX_SUBJECT_SOURCE:
        errors.append(EmailTemplateError(f"The subject is longer than {MAX_SUBJECT_SOURCE} characters", kind="limit", field="subject"))
    elif not subject.strip():
        errors.append(EmailTemplateError("The subject is empty", kind="syntax", field="subject"))
    if not body_text.strip():
        errors.append(EmailTemplateError("The text body is empty", kind="syntax", field="body_text"))
    fields = [("subject", subject, False), ("body_text", body_text, False)]
    if body_html and body_html.strip():
        fields.append(("body_html", body_html, True))
    failed = {error.field for error in errors}
    for field, source, html in fields:
        if field in failed:
            continue
        try:
            compile_source(source, html=html)
            results[field] = render_source(source, data, html=html)
        except EmailTemplateError as exc:
            errors.append(exc.with_field(field))
    if errors:
        raise TemplateInvalid(errors)
    rendered_subject, text, html_out = render_stored(
        event_key, subject=subject, body_text=body_text, body_html=body_html, variables=data
    )
    return rendered_subject, text, html_out


def builtin_text(event_key: str) -> tuple[str, str]:
    spec = catalogue.EVENTS[event_key]
    return spec.default_subject, spec.default_body


def _append_version(
    db: Session,
    row: EmailTemplate,
    *,
    subject: str,
    body_text: str,
    body_html: Optional[str],
    actor_user_id: Optional[int],
) -> EmailTemplateVersion:
    row.subject = subject
    row.body_text = body_text
    row.body_html = body_html or None
    row.version = int(row.version or 0) + 1
    row.updated_by_user_id = actor_user_id
    row.updated_at = datetime.now(timezone.utc)
    version = EmailTemplateVersion(
        template_id=row.id,
        version=row.version,
        subject=subject,
        body_text=body_text,
        body_html=body_html or None,
        saved_by_user_id=actor_user_id,
    )
    db.add(version)
    db.flush()
    return version


def create_template(
    db: Session,
    *,
    event_key: str,
    locale: str,
    product_scope: int,
    subject: str,
    body_text: str,
    body_html: Optional[str],
    enabled: bool,
    actor_user_id: Optional[int],
) -> EmailTemplate:
    validate(event_key, subject=subject, body_text=body_text, body_html=body_html)
    row = EmailTemplate(
        event_key=event_key,
        locale=locale,
        product_scope=product_scope,
        subject=subject,
        body_text=body_text,
        body_html=body_html or None,
        enabled=enabled,
        version=1,
        updated_by_user_id=actor_user_id,
    )
    db.add(row)
    db.flush()
    db.add(
        EmailTemplateVersion(
            template_id=row.id,
            version=1,
            subject=subject,
            body_text=body_text,
            body_html=body_html or None,
            saved_by_user_id=actor_user_id,
        )
    )
    db.flush()
    return row


def save_template(
    db: Session,
    row: EmailTemplate,
    *,
    subject: str,
    body_text: str,
    body_html: Optional[str],
    enabled: Optional[bool],
    actor_user_id: Optional[int],
) -> EmailTemplate:
    validate(row.event_key, subject=subject, body_text=body_text, body_html=body_html)
    if enabled is not None:
        row.enabled = enabled
    _append_version(db, row, subject=subject, body_text=body_text, body_html=body_html, actor_user_id=actor_user_id)
    return row


def reset_template(db: Session, row: EmailTemplate, *, actor_user_id: Optional[int]) -> EmailTemplate:
    """Save the event's built-in text as a new version (the enabled flag is kept)."""
    subject, body_text = builtin_text(row.event_key)
    _append_version(db, row, subject=subject, body_text=body_text, body_html=None, actor_user_id=actor_user_id)
    return row


def version_row(db: Session, template_id: int, version: int) -> Optional[EmailTemplateVersion]:
    return db.execute(
        select(EmailTemplateVersion).where(
            EmailTemplateVersion.template_id == template_id, EmailTemplateVersion.version == version
        )
    ).scalar_one_or_none()


def restore_version(
    db: Session, row: EmailTemplate, old: EmailTemplateVersion, *, actor_user_id: Optional[int]
) -> EmailTemplate:
    """Save an older version's text as a new version."""
    validate(row.event_key, subject=old.subject, body_text=old.body_text, body_html=old.body_html)
    _append_version(
        db, row, subject=old.subject, body_text=old.body_text, body_html=old.body_html, actor_user_id=actor_user_id
    )
    return row


def preview(
    row: EmailTemplate,
    *,
    variables: Optional[Mapping[str, Any]] = None,
    subject: Optional[str] = None,
    body_text: Optional[str] = None,
    body_html: Any = KEEP,
) -> dict[str, Optional[str]]:
    """Render a stored template (or a draft of it) with samples and overrides; nothing is stored or sent."""
    data = sample_variables(row.event_key, variables)
    html_source = row.body_html if body_html is KEEP else body_html
    rendered_subject, text, html = validate(
        row.event_key,
        subject=subject if subject is not None else row.subject,
        body_text=body_text if body_text is not None else row.body_text,
        body_html=html_source,
        variables=data,
    )
    return {"subject": rendered_subject, "text": text, "html": html}


def _test_key_prefix(admin_user_id: int) -> str:
    return f"{TEST_KEY_PREFIX}:{int(admin_user_id)}:"


def remaining_test_sends(
    db: Session, admin_user_id: int, now: datetime, *, locking: bool = False
) -> tuple[int, int]:
    """How many test sends the admin has left this hour, and seconds until the oldest one ages out.

    ``locking`` reads the sends with ``FOR UPDATE``, so the count sees every
    send committed so far even when the transaction already holds an older
    snapshot (MariaDB repeatable read).
    """
    since = now - TEST_SEND_WINDOW
    prefix = _test_key_prefix(admin_user_id)
    stmt = (
        select(EmailMessage.created_at)
        .where(EmailMessage.idempotency_key.startswith(prefix), EmailMessage.created_at >= since)
        .order_by(EmailMessage.created_at)
    )
    if locking:
        stmt = stmt.with_for_update()
    times = list(db.execute(stmt).scalars())
    left = max(0, TEST_SEND_LIMIT - len(times))
    retry = 0
    if not left and times:
        oldest = times[0]
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        retry = max(1, int((oldest + TEST_SEND_WINDOW - now).total_seconds()))
    return left, retry


def queue_test_send(
    db: Session,
    row: EmailTemplate,
    *,
    admin_user_id: int,
    to_address: str,
    variables: Optional[Mapping[str, Any]] = None,
    now: Optional[datetime] = None,
) -> EmailMessage:
    """Queue one marked test message rendered from the stored template, within the hourly limit.

    The admin's user row is locked until the caller commits, so concurrent
    test sends of one admin count and insert one after another.
    """
    now = now or datetime.now(timezone.utc)
    db.execute(select(User.id).where(User.id == admin_user_id).with_for_update())
    left, retry = remaining_test_sends(db, admin_user_id, now, locking=True)
    if left <= 0:
        raise SendRateLimited(retry)
    data = sample_variables(row.event_key, variables)
    subject, text, html = validate(
        row.event_key, subject=row.subject, body_text=row.body_text, body_html=row.body_html, variables=data
    )
    message = EmailMessage(
        to_address=to_address[:320],
        subject=f"[Test] {subject}"[:998],
        body_text=text,
        body_html=html,
        template_key=f"test:{row.event_key}"[:128],
        event=row.event_key[:64],
        status=EmailMessageStatus.QUEUED,
        template_id=row.id,
        template_version=row.version,
        render_fallback=False,
        related_type=TEST_RELATED_TYPE,
        related_id=str(row.id),
        idempotency_key=f"{_test_key_prefix(admin_user_id)}{secrets.token_hex(8)}",
    )
    db.add(message)
    db.flush()
    return message
