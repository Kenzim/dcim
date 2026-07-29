"""Transactional invoice issuance, allocation accounting, and serialization."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models.reseller import (
    Invoice,
    InvoicePurpose,
    InvoiceSequence,
    InvoiceStatus,
    Payment,
    PaymentStatus,
)


class InvoiceError(ValueError):
    pass


class InvoiceService:
    _SEQUENCE_NAME = "invoice"
    _FIRST_NUMBER = 100001

    @staticmethod
    def _require_positive_cents(amount_cents: int) -> None:
        if type(amount_cents) is not int or amount_cents <= 0:
            raise InvoiceError(
                "amount_cents must be a positive integer number of cents"
            )

    @staticmethod
    def _initial_next_value(db: Session) -> int:
        largest = db.scalar(select(func.max(Invoice.invoice_number)))
        return max(InvoiceService._FIRST_NUMBER, int(largest or 0) + 1)

    @staticmethod
    def allocate_number(db: Session) -> int:
        """Allocate one number atomically on SQLite and with a row lock on MySQL."""
        dialect = db.get_bind().dialect.name
        if dialect == "sqlite":
            db.execute(
                sqlite_insert(InvoiceSequence)
                .values(
                    name=InvoiceService._SEQUENCE_NAME,
                    next_value=InvoiceService._initial_next_value(db),
                )
                .on_conflict_do_nothing(index_elements=["name"])
            )
            allocated = db.scalar(
                update(InvoiceSequence)
                .where(InvoiceSequence.name == InvoiceService._SEQUENCE_NAME)
                .values(next_value=InvoiceSequence.next_value + 1)
                .returning(InvoiceSequence.next_value - 1)
            )
            if allocated is None:
                raise InvoiceError("Could not allocate an invoice number")
            return int(allocated)

        sequence = db.execute(
            select(InvoiceSequence)
            .where(InvoiceSequence.name == InvoiceService._SEQUENCE_NAME)
            .with_for_update()
        ).scalar_one_or_none()
        if sequence is None:
            sequence = InvoiceSequence(
                name=InvoiceService._SEQUENCE_NAME,
                next_value=InvoiceService._initial_next_value(db),
            )
            db.add(sequence)
            db.flush()
        allocated = int(sequence.next_value)
        sequence.next_value = allocated + 1
        db.flush()
        return allocated

    @staticmethod
    def create(
        db: Session,
        *,
        reseller_id: int,
        purpose: InvoicePurpose,
        amount_cents: int,
        description: Optional[str] = None,
        service_id: Optional[int] = None,
        due_at: Optional[datetime] = None,
        currency: str = "USD",
    ) -> Invoice:
        InvoiceService._require_positive_cents(amount_cents)
        currency = currency.strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise InvoiceError("currency must be a three-letter code")
        invoice = Invoice(
            invoice_number=InvoiceService.allocate_number(db),
            reseller_id=reseller_id,
            service_id=service_id,
            purpose=purpose,
            status=InvoiceStatus.OPEN,
            amount_cents=amount_cents,
            currency=currency,
            description=description,
            due_at=due_at,
        )
        db.add(invoice)
        db.flush()
        return invoice

    @staticmethod
    def create_topup(
        db: Session, *, reseller_id: int, amount_cents: int
    ) -> Invoice:
        return InvoiceService.create(
            db,
            reseller_id=reseller_id,
            purpose=InvoicePurpose.CREDIT_TOPUP,
            amount_cents=amount_cents,
            description="Reseller account credit top-up",
            due_at=datetime.now(timezone.utc),
        )

    @staticmethod
    def create_deploy(
        db: Session,
        *,
        reseller_id: int,
        amount_cents: int,
        description: str,
        service_id: Optional[int] = None,
        currency: str = "USD",
    ) -> Invoice:
        return InvoiceService.create(
            db,
            reseller_id=reseller_id,
            purpose=InvoicePurpose.DEPLOY_CHARGE,
            amount_cents=amount_cents,
            description=description,
            service_id=service_id,
            due_at=datetime.now(timezone.utc),
            currency=currency,
        )

    @staticmethod
    def create_cycle(
        db: Session,
        *,
        reseller_id: int,
        service_id: int,
        amount_cents: int,
        description: str,
        due_at: Optional[datetime] = None,
        currency: str = "USD",
    ) -> Invoice:
        return InvoiceService.create(
            db,
            reseller_id=reseller_id,
            purpose=InvoicePurpose.CYCLE_CHARGE,
            amount_cents=amount_cents,
            description=description,
            service_id=service_id,
            due_at=due_at or datetime.now(timezone.utc),
            currency=currency,
        )

    @staticmethod
    def get_for_update(db: Session, invoice_id: int) -> Optional[Invoice]:
        if db.get_bind().dialect.name == "sqlite":
            # SQLite ignores SELECT ... FOR UPDATE. A no-op UPDATE acquires its
            # database write lock before a gateway call can race settlement.
            db.execute(
                update(Invoice)
                .where(Invoice.id == invoice_id)
                .values(updated_at=Invoice.updated_at)
            )
        return db.execute(
            select(Invoice)
            .where(Invoice.id == invoice_id)
            .with_for_update()
        ).scalar_one_or_none()

    @staticmethod
    def allocated_cents(db: Session, invoice_id: int) -> int:
        value = db.scalar(
            select(func.coalesce(func.sum(Payment.amount_cents), 0)).where(
                Payment.invoice_id == invoice_id,
                Payment.status == PaymentStatus.SUCCEEDED,
            )
        )
        return int(value or 0)

    @staticmethod
    def mark_paid_if_fully_allocated(
        db: Session, invoice_id: int
    ) -> Invoice:
        invoice = InvoiceService.get_for_update(db, invoice_id)
        if invoice is None:
            raise InvoiceError("Invoice not found")
        allocated = InvoiceService.allocated_cents(db, invoice.id)
        if allocated > invoice.amount_cents:
            raise InvoiceError("Invoice payment allocations exceed its total")
        if allocated == invoice.amount_cents:
            invoice.status = InvoiceStatus.PAID
            invoice.paid_at = invoice.paid_at or datetime.now(timezone.utc)
        elif invoice.status == InvoiceStatus.PAID:
            invoice.status = InvoiceStatus.OPEN
            invoice.paid_at = None
        db.flush()
        return invoice

    @staticmethod
    def serialize_payment(payment: Payment) -> dict:
        return {
            "id": payment.id,
            "gateway": payment.gateway,
            "status": payment.status.value,
            "amount_cents": payment.amount_cents,
            "currency": payment.currency,
            "external_ref": payment.external_ref,
            "failure_code": payment.failure_code,
            "failure_message": payment.failure_message,
            "processed_at": payment.processed_at,
            "refunded_at": payment.refunded_at,
            "created_at": payment.created_at,
        }

    @staticmethod
    def serialize(db: Session, invoice: Invoice, *, payments: bool = False) -> dict:
        allocated = InvoiceService.allocated_cents(db, invoice.id)
        payload = {
            "id": invoice.id,
            "invoice_number": invoice.invoice_number,
            "reseller_id": invoice.reseller_id,
            "service_id": invoice.service_id,
            "purpose": invoice.purpose.value,
            "status": invoice.status.value,
            "amount_cents": invoice.amount_cents,
            "allocated_cents": allocated,
            "remaining_cents": max(0, invoice.amount_cents - allocated),
            "currency": invoice.currency,
            "description": invoice.description,
            "due_at": invoice.due_at,
            "paid_at": invoice.paid_at,
            "created_at": invoice.created_at,
            "updated_at": invoice.updated_at,
        }
        if payments:
            payload["payments"] = [
                InvoiceService.serialize_payment(row)
                for row in sorted(invoice.payments, key=lambda item: item.id)
            ]
        return payload
