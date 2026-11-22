"""Admin API for exchange rates (docs/billing-phase2-design.md 4.10); every write audited ``billing.tax.exchange_rate_*``.

The fallback when HMRC's site is down: an administrator enters HMRC's
published rate for a month by hand. It is stored as source ``hmrc_monthly``
like a fetched rate, since it is the same published figure, and the audit
entry records who entered it. A stored rate is never changed. A rate no
published invoice has frozen may be deleted, to correct a typing mistake.
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
from app.services.tax import exchange_rates

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
    return {"items": [_payload(row) for row in db.scalars(stmt)]}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_rate(body: RateCreate, auth: AdminDep, db: DbDep):
    if exchange_rates.has_rate(db, body.currency, body.period):
        raise _exists(body)
    try:
        exchange_rates.store_month(
            db, body.period, [(body.currency, body.units_per_gbp)], fetched_at=datetime.now(timezone.utc)
        )
    except IntegrityError:
        db.rollback()
        raise _exists(body) from None
    row = exchange_rates.stored_rate(db, body.currency, body.period)
    _audit(db, auth, "created", row)
    db.commit()
    return _payload(row)


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
