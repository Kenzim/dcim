"""Deliver queued account emails (``email_messages``) over SMTP.

``EmailMessageService.enqueue`` writes rows with status QUEUED; this module
is the sender that drains them. It runs inside the notification outbox
worker, so no extra process is needed. Delivery uses the same SMTP settings
as the notification outbox.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage as MimeMessage
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, settings
from app.models.commerce_email import EmailMessage, EmailMessageStatus

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
DEFAULT_BATCH_SIZE = 50


def deliver(
    config: Settings,
    *,
    to_address: str,
    subject: str,
    body_text: str,
    body_html: Optional[str],
) -> None:
    message = MimeMessage()
    message["Subject"] = subject
    message["From"] = str(config.smtp_from)
    message["To"] = to_address
    message.set_content(body_text or "")
    if body_html:
        message.add_alternative(body_html, subtype="html")

    with smtplib.SMTP(
        str(config.smtp_host),
        config.smtp_port,
        timeout=config.smtp_timeout_seconds,
    ) as client:
        client.ehlo()
        if config.smtp_starttls:
            tls_context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
            tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
            tls_context.check_hostname = True
            tls_context.verify_mode = ssl.CERT_REQUIRED
            client.starttls(context=tls_context)
            client.ehlo()
        if config.smtp_username and config.smtp_password:
            client.login(config.smtp_username, config.smtp_password.get_secret_value())
        client.send_message(message)


def send_queued_batch(
    db: Session,
    *,
    config: Settings = settings,
    batch_size: int = DEFAULT_BATCH_SIZE,
    now: Optional[datetime] = None,
) -> int:
    """Send up to ``batch_size`` QUEUED messages. Returns how many were sent.

    Rows are claimed (SENDING) and committed before the network call so that
    concurrent workers skip them. A failed send returns to QUEUED until it
    has used ``MAX_ATTEMPTS``, then becomes FAILED with the last error.
    """
    if not config.smtp_enabled:
        # Leave messages queued: they go out once SMTP is switched on.
        return 0

    rows = (
        db.execute(
            select(EmailMessage)
            .where(EmailMessage.status == EmailMessageStatus.QUEUED)
            .order_by(EmailMessage.id)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    if not rows:
        return 0

    for row in rows:
        row.status = EmailMessageStatus.SENDING
        row.attempts = (row.attempts or 0) + 1
    db.commit()

    sent = 0
    for row in rows:
        try:
            deliver(
                config,
                to_address=row.to_address,
                subject=row.subject,
                body_text=row.body_text or "",
                body_html=row.body_html,
            )
        except Exception as exc:  # network, auth or recipient errors are per-row
            row.error = str(exc)[:2000]
            row.status = (
                EmailMessageStatus.FAILED
                if row.attempts >= MAX_ATTEMPTS
                else EmailMessageStatus.QUEUED
            )
            logger.warning("Email %s delivery failed (attempt %s): %s", row.id, row.attempts, exc)
        else:
            row.status = EmailMessageStatus.SENT
            row.sent_at = now or datetime.now(timezone.utc)
            row.error = None
            sent += 1
        db.commit()
    return sent
