"""Rendering an email event through its template (docs/billing-phase2-design.md 12.1)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.commerce_email import EmailTemplate
from app.services.audit_service import AuditService
from app.services.email_templates import catalogue
from app.services.email_templates.sandbox import (
    EmailTemplateError,
    render_source,
    sanitise_variables,
)

logger = logging.getLogger(__name__)

DEFAULT_LOCALE = "en"
MAX_SUBJECT_CHARS = 255
RENDER_FAILED_ACTION = "billing.email_template_render_failed"
# Control characters and every character str.splitlines() treats as a line break
# (the C0 and C1 controls, U+2028 and U+2029); a header value may contain none of them.
_CONTROL_CHARACTERS = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]+")


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    text: str
    html: Optional[str]
    template_id: Optional[int]
    template_version: Optional[int]
    # True when the stored template failed and the built-in text was used; the
    # template id and version are then those of the template that failed.
    fallback: bool
    # True when the chosen template is disabled: the event is not to be sent.
    # The texts are then the built-in ones.
    skipped: bool = False


def clean_subject(subject: str, limit: int = MAX_SUBJECT_CHARS) -> str:
    """One line, no control or line-break characters (a subject must never carry a header break)."""
    return _CONTROL_CHARACTERS.sub(" ", subject).strip()[:limit]


def select_template(
    db: Session, event_key: str, locale: str, product_id: Optional[int] = None
) -> Optional[EmailTemplate]:
    """The stored template for an event: product variant, then locale, then English."""
    wanted: list[tuple[str, int]] = []
    if product_id:
        wanted.append((locale, int(product_id)))
    wanted.append((locale, 0))
    if locale != DEFAULT_LOCALE:
        wanted.append((DEFAULT_LOCALE, 0))
    for candidate_locale, product_scope in wanted:
        row = db.execute(
            select(EmailTemplate).where(
                EmailTemplate.event_key == event_key,
                EmailTemplate.locale == candidate_locale,
                EmailTemplate.product_scope == product_scope,
            )
        ).scalar_one_or_none()
        if row is not None:
            return row
    return None


def render_stored(
    event_key: str,
    *,
    subject: str,
    body_text: str,
    body_html: Optional[str],
    variables: Mapping[str, Any],
) -> tuple[str, str, Optional[str]]:
    """Render template text with plain data; HTML falls back to the built-in layout when it is empty."""
    data = sanitise_variables(variables)
    rendered_subject = clean_subject(render_source(subject, data))
    if not rendered_subject:
        raise EmailTemplateError("The subject renders to nothing", kind="runtime", field="subject")
    text = render_source(body_text, data)
    if body_html and body_html.strip():
        html = render_source(body_html, data, html=True)
    else:
        html = catalogue.builtin_html(event_key, rendered_subject, text, data)
    return rendered_subject, text, html


def _builtin(event_key: str, variables: Mapping[str, Any]) -> tuple[str, str, Optional[str]]:
    try:
        data = sanitise_variables(variables)
    except EmailTemplateError:
        data = {}
    return catalogue.builtin_email(event_key, data)


def render(
    db: Session,
    event_key: str,
    *,
    locale: str,
    variables: Mapping[str, Any],
    product_id: Optional[int] = None,
) -> RenderedEmail:
    """The email of an event from its stored template, or from the built-in text.

    A template that fails to render is audited and replaced by the built-in
    text (``fallback``); a disabled template yields ``skipped``.
    """
    spec = catalogue.EVENTS.get(event_key)
    row = select_template(db, event_key, locale or DEFAULT_LOCALE, product_id) if spec is not None else None
    if row is None:
        subject, text, html = _builtin(event_key, variables)
        return RenderedEmail(subject, text, html, None, None, False)
    if not row.enabled:
        subject, text, html = _builtin(event_key, variables)
        return RenderedEmail(subject, text, html, row.id, row.version, False, skipped=True)
    allowed = {variable.name for variable in spec.variables}
    visible = {key: value for key, value in variables.items() if key in allowed}
    try:
        subject, text, html = render_stored(
            event_key,
            subject=row.subject,
            body_text=row.body_text,
            body_html=row.body_html,
            variables=visible,
        )
    except Exception as exc:  # noqa: BLE001 - a template must never stop a money email
        error = exc if isinstance(exc, EmailTemplateError) else EmailTemplateError(f"{type(exc).__name__}: {exc}")
        logger.warning("Email template %s (%s) failed to render: %s", row.id, event_key, error.message)
        AuditService.log(
            db,
            action=RENDER_FAILED_ACTION,
            resource_type="email_template",
            resource_id=str(row.id),
            metadata={
                "event_key": event_key,
                "locale": row.locale,
                "version": row.version,
                "error": error.message[:500],
                "line": error.line,
            },
        )
        subject, text, html = _builtin(event_key, variables)
        return RenderedEmail(subject, text, html, row.id, row.version, True)
    return RenderedEmail(subject, text, html, row.id, row.version, False)
