"""Legal numbers of invoices, proformas, credit notes and debit notes (design section 5)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Optional

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models.billing_series import DocumentSeries, DocumentType, series_counter_name
from app.models.reseller import InvoiceSequence

MAX_DISPLAY_LENGTH = 64


class NumberingError(ValueError):
    pass


def _value(item) -> str:
    return getattr(item, "value", item)


def _utc(at: datetime) -> datetime:
    if at.tzinfo is None:
        return at.replace(tzinfo=timezone.utc)
    return at.astimezone(timezone.utc)


def _default_session() -> Session:
    from app.core import database

    return database.SessionLocal()


class InvoiceNumbering:
    @staticmethod
    def period(series: DocumentSeries, at: datetime) -> str:
        """``''`` for ``reset_period = never``, else the four-digit UTC year of ``at``."""
        if series.reset_period == "yearly":
            return f"{_utc(at).year:04d}"
        return ""

    @staticmethod
    def format_number(series: DocumentSeries, value: int, at: datetime) -> str:
        """Expand ``{SERIES}``, ``{NUMBER}``, ``{YEAR}`` and ``{MONTH}`` in the series format."""
        when = _utc(at)
        number = str(int(value)).zfill(int(series.number_padding or 0))
        display = (
            (series.format or "{NUMBER}")
            .replace("{SERIES}", series.code)
            .replace("{YEAR}", f"{when.year:04d}")
            .replace("{MONTH}", f"{when.month:02d}")
            .replace("{NUMBER}", number)
        )
        if len(display) > MAX_DISPLAY_LENGTH:
            raise NumberingError(f"number {display!r} is longer than {MAX_DISPLAY_LENGTH} characters")
        return display

    @staticmethod
    def default_series(db: Session, document_type: DocumentType) -> DocumentSeries:
        series = db.scalar(
            select(DocumentSeries).where(
                DocumentSeries.default_for == _value(document_type),
                DocumentSeries.enabled.is_(True),
            )
        )
        if series is None:
            raise NumberingError(f"no enabled default series for {_value(document_type)}")
        return series

    @staticmethod
    def _increment(db: Session, name: str) -> int:
        """Take the next value of counter ``name`` (created at 1) in ``db``'s transaction."""
        if db.get_bind().dialect.name == "sqlite":
            db.execute(
                sqlite_insert(InvoiceSequence)
                .values(name=name, next_value=1)
                .on_conflict_do_nothing(index_elements=["name"])
            )
            allocated = db.scalar(
                update(InvoiceSequence)
                .where(InvoiceSequence.name == name)
                .values(next_value=InvoiceSequence.next_value + 1)
                .returning(InvoiceSequence.next_value - 1)
            )
            if allocated is None:
                raise NumberingError(f"could not allocate from counter {name}")
            return int(allocated)

        from app.services.billing_locks import BillingLocks

        counter = BillingLocks.number_counter(db, name)
        if counter is None:
            counter = InvoiceSequence(name=name, next_value=1)
            db.add(counter)
            db.flush()
            counter = BillingLocks.number_counter(db, name)
        allocated = int(counter.next_value)
        counter.next_value = allocated + 1
        db.flush()
        return allocated

    @staticmethod
    def allocate(
        db: Session,
        series: DocumentSeries,
        *,
        at: datetime,
        invoice_number: Optional[int] = None,
        session_factory: Optional[Callable[[], Session]] = None,
    ) -> tuple[str, int, str]:
        """Allocate one number of ``series``: returns ``(period, value, display)``.

        ``invoice_number`` series take the invoice's ``invoice_number`` (which
        the caller passes) and use no counter. Gapless ``own`` series count in
        the caller's transaction, as its last lock. Other ``own`` series count
        on a separate short session committed at once, so a later rollback of
        the caller leaves a gap but never a duplicate.
        """
        period = InvoiceNumbering.period(series, at)
        if series.number_source == "invoice_number":
            if invoice_number is None:
                raise NumberingError(f"series {series.code} numbers by invoice_number; pass it")
            value = int(invoice_number)
            return period, value, InvoiceNumbering.format_number(series, value, at)

        name = series_counter_name(series.id, period)
        if series.gapless:
            value = InvoiceNumbering._increment(db, name)
        else:
            session = (session_factory or _default_session)()
            try:
                value = InvoiceNumbering._increment(session, name)
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()
        return period, value, InvoiceNumbering.format_number(series, value, at)
