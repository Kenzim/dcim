"""Sterling VAT on UK VAT invoices in another currency (docs/billing-phase2-design.md 4.10).

Rates live in ``tax_exchange_rates`` (currency units per GBP 1, one row per
source, currency and month) and are written once, by the job
``tax_exchange_rates_fetch`` or an administrator, and never updated. Publish
reads only that table, so no network call happens while invoices are locked.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.tax import SellerTaxRegistration, TaxExchangeRate
from app.services.billing.rounding import round_half_up
from app.services.tax.errors import RetryableTaxPublishError

SOURCE_HMRC = "hmrc_monthly"
BASE = "GBP"
UK = ZoneInfo("Europe/London")
SCALE = 1_000_000  # units_per_gbp has six decimals (Numeric(18, 6))
MAX_RATE = Decimal("999999999999.999999")

FX_RATE_MISSING_MESSAGE = "The HMRC exchange rate for this month is not stored yet; please try again later"


class ExchangeRateMissing(RetryableTaxPublishError):
    """A UK VAT invoice in another currency has no stored HMRC rate for its tax point's month."""

    code = "tax_fx_rate_missing"
    message = FX_RATE_MISSING_MESSAGE

    def __init__(self, currency: str, period: str) -> None:
        self.currency = currency
        self.period = period
        super().__init__(f"no {SOURCE_HMRC} rate for {currency} in {period}")


@dataclass(frozen=True)
class Conversion:
    rate: Decimal
    period: str
    source: str
    tax_total_gbp_cents: int
    rate_id: int


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def period_of(moment: datetime) -> str:
    """The ``YYYY-MM`` of ``moment`` in UK time."""
    local = _aware(moment).astimezone(UK)
    return f"{local.year:04d}-{local.month:02d}"


def parse_period(period: str) -> tuple[int, int]:
    """``(year, month)`` of a ``YYYY-MM`` text; raises ``ValueError``."""
    if not isinstance(period, str) or len(period) != 7 or period[4] != "-":
        raise ValueError("a period is YYYY-MM")
    year, month = int(period[:4]), int(period[5:])
    if not 2000 <= year <= 9999 or not 1 <= month <= 12:
        raise ValueError("a period is YYYY-MM")
    return year, month


def period_bounds(period: str) -> tuple[date, date]:
    year, month = parse_period(period)
    start = date(year, month, 1)
    following = date(year + month // 12, month % 12 + 1, 1)
    return start, following - timedelta(days=1)


def next_period(period: str) -> str:
    year, month = parse_period(period)
    return f"{year + month // 12:04d}-{month % 12 + 1:02d}"


def normalise_rate(value: Any) -> Decimal:
    """A rate as stored: above zero, at most six decimals and twelve whole digits; raises ``ValueError``."""
    try:
        rate = Decimal(str(value).strip())
    except Exception as exc:  # noqa: BLE001 - any parse failure is a bad rate
        raise ValueError("the rate must be a decimal number") from exc
    if not rate.is_finite() or rate <= 0 or rate > MAX_RATE:
        raise ValueError("the rate must be above zero and below one trillion")
    if rate != rate.quantize(Decimal("0.000001")):
        raise ValueError("the rate has at most six decimals")
    return rate.quantize(Decimal("0.000001"))


def to_gbp_cents(cents: int, rate: Decimal) -> int:
    """``cents / rate`` rounded half up to the penny, in integer arithmetic."""
    micro = int((Decimal(rate) * SCALE).to_integral_value(rounding=ROUND_HALF_UP))
    return round_half_up(int(cents) * SCALE, micro)


def stored_rate(
    db: Session, currency: str, period: str, source: str = SOURCE_HMRC, *, share_lock: bool = False
) -> Optional[TaxExchangeRate]:
    """The stored rate; ``share_lock`` holds it until commit so it cannot be deleted meanwhile."""
    stmt = select(TaxExchangeRate).where(
        TaxExchangeRate.source == source,
        TaxExchangeRate.base == BASE,
        TaxExchangeRate.currency == currency.upper(),
        TaxExchangeRate.period == period,
    )
    if share_lock:
        stmt = stmt.with_for_update(read=True)
    return db.execute(stmt).scalar_one_or_none()


def has_rate(db: Session, currency: str, period: str, source: str = SOURCE_HMRC) -> bool:
    return stored_rate(db, currency, period, source) is not None


def needs_conversion(registration: Optional[SellerTaxRegistration], currency: str, tax_cents: int) -> bool:
    """A UK registration, VAT above zero and a currency other than sterling."""
    if registration is None or (registration.jurisdiction or "").upper() != "GB":
        return False
    return int(tax_cents or 0) > 0 and str(currency or "").upper() != BASE


def convert_at_publish(
    db: Session, registration: Optional[SellerTaxRegistration], currency: str, tax_cents: int, tax_point_at: datetime
) -> Optional[Conversion]:
    """The sterling VAT to freeze on an invoice, None when none is needed; raises ``ExchangeRateMissing``."""
    if not needs_conversion(registration, currency, tax_cents):
        return None
    period = period_of(tax_point_at)
    # Shared, so an administrator cannot delete the rate while this publish freezes it.
    row = stored_rate(db, currency, period, share_lock=True)
    if row is None:
        raise ExchangeRateMissing(str(currency).upper(), period)
    rate = Decimal(row.units_per_gbp)
    return Conversion(rate, period, row.source, to_gbp_cents(int(tax_cents), rate), int(row.id))


@dataclass(frozen=True)
class NoteRate:
    """The rate a credit or debit note converts its VAT at, and the row it was read from."""

    rate: Decimal
    period: str
    source: str
    rate_id: Optional[int]


def _engine_taxed(invoice: Any) -> bool:
    return bool(invoice.tax_engine) and invoice.tax_engine != "legacy"


def note_rate(db: Session, invoice: Any, tax_cents: int) -> Optional[NoteRate]:
    """The rate of a note with VAT ``tax_cents`` on ``invoice``, None when it needs no sterling.

    A note uses its invoice's frozen rate. On a UK VAT invoice in another
    currency that froze none because its own VAT was zero, a taxed note uses
    the stored rate of the UK month of the invoice's tax point (the rule of
    publish), share-locked after the invoice as publish does, and raises
    ``ExchangeRateMissing`` when that is not stored.
    """
    if int(tax_cents or 0) <= 0 or invoice is None or not _engine_taxed(invoice):
        return None
    if invoice.fx_rate is not None:
        rate_id = int(invoice.fx_rate_id) if invoice.fx_rate_id is not None else None
        return NoteRate(Decimal(invoice.fx_rate), str(invoice.fx_period), str(invoice.fx_source), rate_id)
    registration = (
        db.get(SellerTaxRegistration, int(invoice.seller_registration_id))
        if invoice.seller_registration_id is not None
        else None
    )
    currency = str(invoice.currency or "").upper()
    if not needs_conversion(registration, currency, int(tax_cents)):
        return None
    period = period_of(invoice.tax_point_at or invoice.issued_at)
    row = stored_rate(db, currency, period, share_lock=True)
    if row is None:
        raise ExchangeRateMissing(currency, period)
    return NoteRate(Decimal(row.units_per_gbp), period, row.source, int(row.id))


def credited_gbp_cents(invoice_tax_cents: int, credited_tax_cents: int, rate: Decimal) -> int:
    """The sterling of ``credited_tax_cents`` of VAT credited in all on an invoice.

    The part up to the invoice's own VAT converts as the invoice's VAT does,
    and the rest (VAT of debit notes) as the debit notes' VAT does, so
    crediting everything gives exactly the invoice's sterling VAT plus its
    debit notes' sterling VAT.
    """
    on_invoice = max(0, min(int(credited_tax_cents), int(invoice_tax_cents)))
    return to_gbp_cents(on_invoice, rate) + to_gbp_cents(int(credited_tax_cents) - on_invoice, rate)


def note_gbp_cents(
    *, credit: bool, invoice_tax_cents: int, before_cents: int, tax_cents: int, rate: Decimal
) -> int:
    """A note's sterling VAT, worked out cumulatively over the invoice's notes of its kind.

    ``before_cents`` is the VAT of the notes of the same kind issued before
    it (in id order, as ``tax_base_rows`` counts them). The note gets the
    sterling of everything so far, itself included, less the sterling of
    what came before, so the notes' figures never drift from the total.
    """
    def total(cents: int) -> int:
        if credit:
            return credited_gbp_cents(invoice_tax_cents, cents, rate)
        return to_gbp_cents(cents, rate)

    return total(int(before_cents) + int(tax_cents)) - total(int(before_cents))


def gb_registration_active(db: Session, at: datetime) -> bool:
    from app.services.tax import rules_repo

    return any(
        (registration.jurisdiction or "").upper() == "GB"
        for registration in rules_repo.registrations_valid_at(db, at)
    )


NEXT_MONTH_FROM_DAY = 25


def rates_needed(db: Session, now: datetime) -> Optional[str]:
    """The currency that needs sterling rates, or None: a ``GB`` registration in force and an operator currency other than GBP."""
    from app.services.billing.operator_currency import operator_currency

    currency = operator_currency()
    if currency == BASE or not gb_registration_active(db, _aware(now)):
        return None
    return currency


def missing_periods(db: Session, now: datetime, *, include_next: bool = True) -> list[str]:
    """Months whose rate is needed and not stored: the current UK month, and from the 25th the next."""
    currency = rates_needed(db, now)
    if currency is None:
        return []
    current = period_of(now)
    periods = [current]
    if include_next and _aware(now).astimezone(UK).day >= NEXT_MONTH_FROM_DAY:
        periods.append(next_period(current))
    return [period for period in periods if not has_rate(db, currency, period)]


@dataclass(frozen=True)
class StoreOutcome:
    inserted: tuple[str, ...]
    unchanged: tuple[str, ...]
    conflicts: tuple[tuple[str, Decimal, Decimal], ...]  # currency, stored, offered


def store_month(
    db: Session,
    period: str,
    rates: Iterable[tuple[str, Any]],
    *,
    source: str = SOURCE_HMRC,
    fetched_at: Optional[datetime] = None,
    hand_entered: bool = False,
) -> StoreOutcome:
    """Insert each currency of ``period`` that is not stored yet; never change a stored one.

    A stored rate that differs from the one offered is reported in
    ``conflicts`` and kept. ``hand_entered`` marks the inserted rows as typed
    in by an administrator. Runs in the caller's transaction, which commits;
    a row stored meanwhile by another writer makes the flush raise
    ``IntegrityError`` (the unique key), and the caller rolls back.
    """
    start, end = period_bounds(period)
    fetched_at = _aware(fetched_at or datetime.now(timezone.utc))
    existing = {
        row.currency: Decimal(row.units_per_gbp)
        for row in db.execute(
            select(TaxExchangeRate).where(
                TaxExchangeRate.source == source, TaxExchangeRate.base == BASE, TaxExchangeRate.period == period
            )
        ).scalars()
    }
    inserted, unchanged, conflicts = [], [], []
    for currency, value in rates:
        code = str(currency).upper()
        rate = normalise_rate(value)
        if code in existing:
            if existing[code] == rate:
                unchanged.append(code)
            else:
                conflicts.append((code, existing[code], rate))
            continue
        db.add(
            TaxExchangeRate(
                source=source,
                base=BASE,
                currency=code,
                period=period,
                units_per_gbp=rate,
                period_start=start,
                period_end=end,
                fetched_at=fetched_at,
                hand_entered=hand_entered,
            )
        )
        existing[code] = rate
        inserted.append(code)
    db.flush()
    return StoreOutcome(tuple(inserted), tuple(unchanged), tuple(conflicts))
