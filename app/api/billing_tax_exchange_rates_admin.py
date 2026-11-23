"""Admin API for exchange rates (docs/billing-phase2-design.md 4.10); every write audited ``billing.tax.exchange_rate_*``.

The fallback when HMRC's site is down: an administrator enters HMRC's
published rate for a month by hand. It is stored as source ``hmrc_monthly``
like a fetched rate, since it is the same published figure, and the audit
entry records who entered it. It is marked ``hand_entered``, and the fetch
job compares it with HMRC's file once. A stored rate is never changed. A
rate no published invoice or note has frozen may be deleted, to correct a
typing mistake. Storing a rate fills at once every invoice and note whose
sterling VAT waits for it, after which the rate can no longer be deleted, so
``GET /preview`` says how many it would fill and the POST asks for
``confirm: true`` when that is more than none.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.auth import require_admin
from app.core.database import get_db
from app.core.openapi_responses import COMMON_ERROR_RESPONSES
from app.models.invoice_adjustment import InvoiceAdjustment
from app.models.reseller import Invoice
from app.models.tax import TaxExchangeRate
from app.services.audit_service import AuditService
from app.services.tax import exchange_rates, sterling_conversion

DbDep = Annotated[Session, Depends(get_db)]
AdminDep = Annotated[dict, Depends(require_admin)]

router = APIRouter(
    prefix="/admin/billing/tax/exchange-rates",
    tags=["billing-tax-admin"],
    dependencies=[Depends(require_admin)],
    responses=COMMON_ERROR_RESPONSES,
)

PERIOD_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
CURRENCY_PATTERN = r"^[A-Za-z]{3}$"


def _payload(row: TaxExchangeRate) -> dict:
    return {
        "id": row.id,
        "source": row.source,
        "base": row.base,
        "currency": row.currency,
        "period": row.period,
        "units_per_gbp": format(row.units_per_gbp, "f"),
        "period_start": row.period_start.isoformat(),
        "period_end": row.period_end.isoformat(),
        "fetched_at": row.fetched_at.isoformat() if row.fetched_at else None,
        "hand_entered": bool(row.hand_entered),
        "compared_at": row.compared_at.isoformat() if row.compared_at else None,
    }


def _audit(db: Session, auth: dict, action: str, row: TaxExchangeRate) -> None:
    AuditService.log(
        db,
        actor_user_id=auth["user_id"],
        action=f"billing.tax.exchange_rate_{action}",
        resource_type="tax_exchange_rate",
        resource_id=str(row.id),
        metadata={k: v for k, v in _payload(row).items() if k != "id"},
    )


class RateCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period: str = Field(pattern=PERIOD_PATTERN)
    currency: str = Field(pattern=CURRENCY_PATTERN)
    units_per_gbp: StrictStr = Field(max_length=24)
    confirm: bool = False

    @field_validator("currency")
    @classmethod
    def _upper(cls, value: str) -> str:
        value = value.upper()
        if value == exchange_rates.BASE:
            raise ValueError("GBP is the base currency")
        return value

    @field_validator("units_per_gbp")
    @classmethod
    def _rate(cls, value: str) -> str:
        return str(exchange_rates.normalise_rate(value))


def _exists(body: RateCreate) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "tax_exchange_rate_exists",
            "message": f"The {body.currency} rate for {body.period} is stored already and is never changed",
        },
    )


@router.get("")
def list_rates(
    db: DbDep,
    period: Optional[str] = Query(None, pattern=PERIOD_PATTERN),
    currency: Optional[str] = Query(None, pattern=CURRENCY_PATTERN),
    limit: int = Query(200, ge=1, le=1000),
):
    stmt = select(TaxExchangeRate)
    if period:
        stmt = stmt.where(TaxExchangeRate.period == period)
    if currency:
        stmt = stmt.where(TaxExchangeRate.currency == currency.upper())
    stmt = stmt.order_by(TaxExchangeRate.period.desc(), TaxExchangeRate.currency, TaxExchangeRate.id).limit(limit)
    rows = list(db.scalars(stmt))
    ids = [row.id for row in rows]
    used = set()
    if ids:
        used.update(db.scalars(select(Invoice.fx_rate_id).where(Invoice.fx_rate_id.in_(ids)).distinct()))
        used.update(db.scalars(select(InvoiceAdjustment.fx_rate_id).where(InvoiceAdjustment.fx_rate_id.in_(ids)).distinct()))
    return {"items": [{**_payload(row), "in_use": row.id in used} for row in rows]}


def _preview(db: Session, period: str, currency: str) -> dict:
    invoices, notes = sterling_conversion.pending_documents(db, period, currency)
    previous_period = _previous_period(period)
    previous = exchange_rates.stored_rate(db, currency, previous_period)
    return {
        "period": period,
        "currency": currency,
        "stored": exchange_rates.has_rate(db, currency, period),
        "pending_invoices": invoices,
        "pending_notes": notes,
        "previous": (
            {"period": previous.period, "units_per_gbp": format(previous.units_per_gbp, "f")}
            if previous is not None
            else None
        ),
    }


def _previous_period(period: str) -> str:
    year, month = exchange_rates.parse_period(period)
    return f"{year - (month == 1):04d}-{(month - 2) % 12 + 1:02d}"


@router.get("/preview")
def preview_rate(
    db: DbDep,
    period: str = Query(pattern=PERIOD_PATTERN),
    currency: str = Query(pattern=CURRENCY_PATTERN),
):
    """What storing a rate for ``period`` and ``currency`` would fill: pending invoices and notes, and last month's rate."""
    return _preview(db, period, currency.upper())


@router.post("", status_code=status.HTTP_201_CREATED)
def create_rate(body: RateCreate, auth: AdminDep, db: DbDep):
    if exchange_rates.has_rate(db, body.currency, body.period):
        raise _exists(body)
    invoices, notes = sterling_conversion.pending_documents(db, body.period, body.currency)
    if invoices + notes > 0 and not body.confirm:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "tax_exchange_rate_confirm_required",
                "message": (
                    f"Storing this rate fills the sterling VAT of {invoices} invoices and {notes} notes at once, "
                    "and the rate can then no longer be deleted; send confirm: true to store it"
                ),
                "pending_invoices": invoices,
                "pending_notes": notes,
            },
        )
    try:
        outcome = exchange_rates.store_month(
            db, body.period, [(body.currency, body.units_per_gbp)], fetched_at=datetime.now(timezone.utc),
            hand_entered=True,
        )
    except IntegrityError:
        db.rollback()
        raise _exists(body) from None
    row = exchange_rates.stored_rate(db, body.currency, body.period)
    _audit(db, auth, "created", row)
    db.commit()
    return {**_payload(row), "filled_invoices": len(outcome.filled)}


def _in_use() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": "tax_exchange_rate_in_use", "message": "An invoice has frozen this rate"},
    )


@router.delete("/{rate_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_rate(rate_id: int, auth: AdminDep, db: DbDep):
    """Delete a rate no invoice or note references through ``fx_rate_id``.

    The rate is locked for update first, so a publish holding it (shared)
    commits before this goes on. The read below may still miss that publish
    when this transaction's snapshot is older; the foreign keys ``ON DELETE
    RESTRICT`` then refuse the delete itself, which answers the same 409.
    """
    row = db.get(TaxExchangeRate, rate_id, with_for_update=True)
    if row is None:
        raise HTTPException(status_code=404, detail="Exchange rate not found")
    if (
        db.scalar(select(Invoice.id).where(Invoice.fx_rate_id == row.id).limit(1)) is not None
        or db.scalar(select(InvoiceAdjustment.id).where(InvoiceAdjustment.fx_rate_id == row.id).limit(1))
        is not None
    ):
        raise _in_use()
    _audit(db, auth, "deleted", row)
    db.delete(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise _in_use() from None
    db.commit()
