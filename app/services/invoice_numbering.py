"""Legal numbers of invoices, proformas, credit notes and debit notes (design section 5)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Optional

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models.billing_series import (
    DocumentSeries,
    DocumentType,
    series_counter_name,
    series_start_name,
)
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
    def _increment(db: Session, series_id: int, period: str) -> int:
        """Take the next value of the series' counter (created at 1) in ``db``'s transaction."""
        if db.get_bind().dialect.name == "sqlite":
            name = series_counter_name(series_id, period)
            _insert_if_missing(db, name, 1)
            allocated = db.scalar(
                update(InvoiceSequence)
                .where(InvoiceSequence.name == name)
                .values(next_value=InvoiceSequence.next_value + 1)
                .returning(InvoiceSequence.next_value - 1)
            )
            if allocated is None:
                raise NumberingError(f"could not allocate from counter {name}")
            if allocated == 1:
                _insert_if_missing(db, series_start_name(series_id, period), 1)
            return int(allocated)

        counter = lock_series_counter(db, series_id, period)
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

        if series.gapless:
            value = InvoiceNumbering._increment(db, series.id, period)
        else:
            session = (session_factory or _default_session)()
            try:
                value = InvoiceNumbering._increment(session, series.id, period)
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()
        return period, value, InvoiceNumbering.format_number(series, value, at)


# --- Series configuration (design 5.2 and 5.3) ---

SERIES_TAGS = ("{SERIES}", "{NUMBER}", "{YEAR}", "{MONTH}")
_CODE_CHARACTERS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def validate_series_config(
    *,
    code: str,
    document_type: str,
    format: str,
    number_padding: int,
    number_source: str,
    gapless: bool,
    reset_period: str,
) -> None:
    """Raise ``NumberingError`` when a series configuration breaks a rule of design 5.2.

    The format needs ``{NUMBER}`` (and ``{YEAR}`` for a yearly series), may hold
    no other braces than the four tags, and its longest expansion (12 digits or
    the padding, year 9999, month 12) must fit ``MAX_DISPLAY_LENGTH``.
    """
    if not code or len(code) > 16 or not set(code) <= _CODE_CHARACTERS:
        raise NumberingError("code must be 1 to 16 letters, digits, '-' or '_'")
    if document_type not in {item.value for item in DocumentType}:
        raise NumberingError(f"unknown document type {document_type!r}")
    if number_source not in ("own", "invoice_number"):
        raise NumberingError("number_source must be 'own' or 'invoice_number'")
    if reset_period not in ("never", "yearly"):
        raise NumberingError("reset_period must be 'never' or 'yearly'")
    if type(number_padding) is not int or not 0 <= number_padding <= 12:
        raise NumberingError("number_padding must be between 0 and 12")
    if "{NUMBER}" not in format:
        raise NumberingError("format must contain {NUMBER}")
    if reset_period == "yearly" and "{YEAR}" not in format:
        raise NumberingError("a yearly series needs {YEAR} in its format")
    stripped = format
    for tag in SERIES_TAGS:
        stripped = stripped.replace(tag, "")
    if "{" in stripped or "}" in stripped:
        raise NumberingError("format may only use the tags " + ", ".join(SERIES_TAGS))
    longest = (
        format.replace("{SERIES}", code)
        .replace("{YEAR}", "9999")
        .replace("{MONTH}", "12")
        .replace("{NUMBER}", "9" * max(12, number_padding))
    )
    if len(longest) > MAX_DISPLAY_LENGTH:
        raise NumberingError(
            f"the longest number of this format has {len(longest)} characters, "
            f"more than {MAX_DISPLAY_LENGTH}"
        )
    if number_source == "invoice_number" and (
        document_type != DocumentType.INVOICE.value or gapless or reset_period != "never"
    ):
        raise NumberingError(
            "number_source 'invoice_number' is only for a non-gapless invoice series "
            "that never resets"
        )


def series_max_value(db: Session, series_id: int, period: Optional[str] = None) -> int:
    """The largest number value issued in a series (in ``period`` when given), 0 if none."""
    from sqlalchemy import func, or_

    from app.models.invoice_adjustment import InvoiceAdjustment
    from app.models.reseller import Invoice

    sources = (
        (Invoice.series_id, Invoice.number_period, Invoice.number_value),
        (Invoice.proforma_series_id, Invoice.proforma_number_period, Invoice.proforma_number_value),
        (InvoiceAdjustment.series_id, InvoiceAdjustment.number_period, InvoiceAdjustment.number_value),
    )
    largest = 0
    for series_column, period_column, value_column in sources:
        stmt = select(func.max(value_column)).where(series_column == series_id)
        if period == "":
            stmt = stmt.where(or_(period_column == "", period_column.is_(None)))
        elif period is not None:
            stmt = stmt.where(period_column == period)
        largest = max(largest, int(db.scalar(stmt) or 0))
    return largest


def series_in_use(db: Session, series_id: int) -> bool:
    """Whether any invoice, proforma number or note references the series."""
    from app.models.invoice_adjustment import InvoiceAdjustment
    from app.models.reseller import Invoice

    for column in (Invoice.series_id, Invoice.proforma_series_id, InvoiceAdjustment.series_id):
        if db.scalar(select(column).where(column == series_id).limit(1)) is not None:
            return True
    return False


def counter_next_value(db: Session, series: DocumentSeries, at: datetime) -> int:
    """The value the series' counter gives next in the period of ``at`` (1 with no counter)."""
    name = series_counter_name(series.id, InvoiceNumbering.period(series, at))
    value = db.scalar(select(InvoiceSequence.next_value).where(InvoiceSequence.name == name))
    return int(value) if value is not None else 1


def set_counter_at_least(db: Session, series: DocumentSeries, value: int, at: datetime) -> int:
    """Raise the series' counter for the period of ``at`` so it gives ``value`` next.

    Counters only go up: a ``value`` below the counter, or at or below a number
    already issued in that period, raises ``NumberingError``. So does raising a
    gapless counter once the period has issued numbers, as that leaves a gap.
    Before anything is issued the start row moves with the counter. Returns the
    new next value. Takes the counter row lock on MySQL.
    """
    if type(value) is not int or value < 1:
        raise NumberingError("start_value must be a positive integer")
    period = InvoiceNumbering.period(series, at)
    counter = lock_series_counter(db, series.id, period)
    issued = series_max_value(db, series.id, period)
    if value <= issued:
        raise NumberingError(
            f"start_value {value} is not above {issued}, the largest number already issued"
        )
    if value < int(counter.next_value):
        raise NumberingError(
            f"start_value {value} is below the counter's next value {counter.next_value}"
        )
    if series.gapless and issued and value > int(counter.next_value):
        raise NumberingError(
            f"series {series.code} is gapless and has issued numbers up to {issued}; "
            f"raising its counter to {value} would leave a gap"
        )
    counter.next_value = value
    if not issued:
        start = series_start_name(series.id, period)
        _insert_if_missing(db, start, value)
        db.execute(update(InvoiceSequence).where(InvoiceSequence.name == start).values(next_value=value))
    db.flush()
    return value


def _insert_if_missing(db: Session, name: str, value: int) -> None:
    """Insert counter ``name`` at ``value`` unless it exists; safe when transactions race.

    On MySQL and MariaDB this is ``INSERT ... ON DUPLICATE KEY UPDATE`` with no
    read first. It takes the row's exclusive lock at once, so racing creators
    queue on it. ``INSERT IGNORE`` would take a shared lock on an existing row,
    and sessions upgrading it to ``FOR UPDATE`` deadlock. A plain read first
    would open a read view, and with ``innodb_snapshot_isolation`` (MariaDB
    11.6 and later) the locking read after it fails with error 1020 once
    another session commits the row.
    """
    dialect = db.get_bind().dialect.name
    values = {"name": name, "next_value": value}
    if dialect == "sqlite":
        db.execute(sqlite_insert(InvoiceSequence).values(**values).on_conflict_do_nothing(index_elements=["name"]))
    elif dialect in ("mysql", "mariadb"):
        from sqlalchemy.dialects.mysql import insert as mysql_insert

        statement = mysql_insert(InvoiceSequence).values(**values)
        db.execute(statement.on_duplicate_key_update(name=statement.inserted.name))
    else:
        from sqlalchemy.exc import IntegrityError

        try:
            with db.begin_nested():
                db.execute(InvoiceSequence.__table__.insert().values(**values))
        except IntegrityError:
            pass


def lock_series_counter(db: Session, series_id: int, period: str) -> InvoiceSequence:
    """The series' counter for ``period``, created at 1 if missing, locked for the caller's transaction.

    While the counter is at 1 nothing has been handed out, so its start row is
    created at 1 too, under the counter's lock. A counter already past 1 with
    no start row is left without one, and the gapless check reports it.
    """
    from app.services.billing_locks import BillingLocks

    name = series_counter_name(series_id, period)
    _insert_if_missing(db, name, 1)
    counter = BillingLocks.number_counter(db, name)
    if counter is None:
        raise NumberingError(f"could not create counter {name}")
    if int(counter.next_value) == 1:
        _insert_if_missing(db, series_start_name(series_id, period), 1)
    return counter


def lock_series_numbering(db: Session, series: DocumentSeries, at: datetime) -> None:
    """Hold back publishes that number from ``series`` while its configuration changes.

    Takes the global invoice number row, then the series' counter for the
    period of ``at``: the order publish takes them. An ``invoice_number``
    publish holds the first from allocation to commit, an ``own`` publish
    holds the second, so checks made after this see every number in flight.
    """
    if db.get_bind().dialect.name != "sqlite":
        db.execute(select(InvoiceSequence).where(InvoiceSequence.name == "invoice").with_for_update())
    lock_series_counter(db, series.id, InvoiceNumbering.period(series, at))


def invoice_number_floor(db: Session) -> int:
    """The next internal invoice number: above every ``invoice_number`` already allocated."""
    value = db.scalar(select(InvoiceSequence.next_value).where(InvoiceSequence.name == "invoice"))
    return int(value or 1)


def delete_series_counters(db: Session, series_id: int) -> None:
    """Remove every counter and start row of a series (only for a series that issued nothing)."""
    from sqlalchemy import delete, or_

    db.execute(
        delete(InvoiceSequence).where(
            or_(
                InvoiceSequence.name.like(series_counter_name(series_id, "") + "%"),
                InvoiceSequence.name.like(series_start_name(series_id, "") + "%"),
            )
        )
    )
