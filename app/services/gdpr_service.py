"""GDPR export and anonymization for retail commerce users."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dao.commerce_dao import BillingAccountDAO
from app.models.commerce_audit import UserAuditEvent
from app.models.commerce_auth_extra import ExternalIdentityProvider, UserExternalIdentity
from app.models.commerce_email import EmailMessage
from app.models.commerce_order import Order
from app.models.reseller import Invoice
from app.models.support_ticket import Ticket
from app.models.user import User


_SCRUBBED = "[redacted]"


class GdprService:
    @staticmethod
    def export_user_data(db: Session, user_id: int) -> dict[str, Any]:
        user = db.get(User, user_id)
        if user is None:
            raise ValueError("User not found")

        account = BillingAccountDAO.get_by_user_id(db, user_id)
        profile = account.profile if account and account.profile else None

        orders = []
        if account:
            order_rows = list(
                db.execute(
                    select(Order)
                    .where(Order.billing_account_id == account.id)
                    .order_by(Order.created_at.desc())
                ).scalars()
            )
            orders = [
                {
                    "id": row.id,
                    "order_number": row.order_number,
                    "status": row.status.value,
                    "total_cents": row.total_cents,
                    "currency": row.currency,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in order_rows
            ]

        invoices = []
        if account:
            invoice_rows = list(
                db.execute(
                    select(Invoice)
                    .where(Invoice.billing_account_id == account.id)
                    .order_by(Invoice.created_at.desc())
                ).scalars()
            )
            invoices = [
                {
                    "id": row.id,
                    "invoice_number": row.invoice_number,
                    "purpose": row.purpose.value,
                    "status": row.status.value,
                    "amount_cents": row.amount_cents,
                    "currency": row.currency,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "paid_at": row.paid_at.isoformat() if row.paid_at else None,
                }
                for row in invoice_rows
            ]

        tickets = []
        if account:
            ticket_rows = list(
                db.execute(
                    select(Ticket)
                    .where(Ticket.billing_account_id == account.id)
                    .order_by(Ticket.created_at.desc())
                ).scalars()
            )
            tickets = [
                {
                    "id": row.id,
                    "ticket_number": row.ticket_number,
                    "subject": row.subject,
                    "status": row.status.value,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in ticket_rows
            ]

        emails = list(
            db.execute(
                select(EmailMessage)
                .where(EmailMessage.user_id == user_id)
                .order_by(EmailMessage.created_at.desc())
                .limit(200)
            ).scalars()
        )
        discord = db.execute(
            select(UserExternalIdentity).where(
                UserExternalIdentity.user_id == user_id,
                UserExternalIdentity.provider == ExternalIdentityProvider.DISCORD,
            )
        ).scalar_one_or_none()

        audit = list(
            db.execute(
                select(UserAuditEvent)
                .where(UserAuditEvent.subject_user_id == user_id)
                .order_by(UserAuditEvent.created_at.desc())
                .limit(500)
            ).scalars()
        )

        return {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "user": {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "created_at": user.created_at.isoformat() if user.created_at else None,
            },
            "billing_profile": {
                "legal_name": profile.legal_name if profile else None,
                "company": profile.company if profile else None,
                "country": profile.country if profile else None,
                "invoice_email": profile.invoice_email if profile else None,
            }
            if profile
            else None,
            "orders": orders,
            "invoices": invoices,
            "tickets": tickets,
            "emails": [
                {
                    "id": row.id,
                    "subject": row.subject,
                    "event": row.event,
                    "status": row.status.value,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in emails
            ],
            "discord": {
                "linked": discord is not None,
                "username": discord.username if discord else None,
                "linked_at": discord.linked_at.isoformat()
                if discord and discord.linked_at
                else None,
            },
            "audit_events": [
                {
                    "id": row.id,
                    "action": row.action,
                    "resource_type": row.resource_type,
                    "resource_id": row.resource_id,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in audit
            ],
        }

    @staticmethod
    def anonymize_user(db: Session, user_id: int) -> dict[str, Any]:
        user = db.get(User, user_id)
        if user is None:
            raise ValueError("User not found")
        if user.is_admin:
            raise ValueError("Admin accounts cannot be anonymized via commerce GDPR")

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        anon_username = f"deleted-{user_id}-{stamp}"
        anon_email = f"deleted-{user_id}-{stamp}@anonymized.invalid"

        user.username = anon_username[:255]
        user.email = anon_email[:255]
        user.password = None
        user.external_username = None
        user.external_email = None

        account = BillingAccountDAO.get_by_user_id(db, user_id)
        if account and account.profile:
            profile = account.profile
            profile.legal_name = _SCRUBBED
            profile.company = None
            profile.address_line1 = None
            profile.address_line2 = None
            profile.city = None
            profile.region = None
            profile.postal_code = None
            profile.phone = None
            profile.tax_id = None
            profile.invoice_email = anon_email

        identity = db.execute(
            select(UserExternalIdentity).where(
                UserExternalIdentity.user_id == user_id,
                UserExternalIdentity.provider == ExternalIdentityProvider.DISCORD,
            )
        ).scalar_one_or_none()
        if identity is not None:
            db.delete(identity)

        db.flush()
        return {
            "user_id": user_id,
            "anonymized": True,
            "username": user.username,
            "email": user.email,
        }
