"""Admin API for the billing engines: modes, shadow mismatches, scans and switching.

The credit and invoice engines each run as ``legacy``, ``shadow`` or ``v2``
(docs/billing-phase1-design.md 6.4 and 6.5). Switching to ``v2`` is only
allowed here, after the switch criterion holds, and every switch writes the
``billing_engine_switch:<engine>`` record that ``billing_engine_mode`` needs to
honour ``v2``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.auth import require_admin
from app.core.database import get_db
from app.core.openapi_responses import COMMON_ERROR_RESPONSES
from app.models.billing_shadow import BillingShadowMismatch
from app.models.commerce_account import BillingAccount, BillingAccountType
from app.models.invoice_adjustment import InvoiceAdjustment
from app.models.refund import Refund
from app.models.reseller import Payment, Reseller
from app.services import billing_shadow
from app.services.audit_service import AuditService
from app.services.billing_shadow import (
    ENGINE_SETTING,
    ENGINES,
    BillingShadowService,
    billing_engine_mode,
)
from app.services.settings_store import get_setting, set_setting

DbDep = Annotated[Session, Depends(get_db)]
AdminDep = Annotated[dict, Depends(require_admin)]
EngineName = Literal["invoice", "credit"]

router = APIRouter(
    prefix="/admin/billing/engines",
    tags=["billing-engines-admin"],
    dependencies=[Depends(require_admin)],
    responses=COMMON_ERROR_RESPONSES,
)

RESYNC_MAX_ACCOUNTS = 500


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _engine_state(db: Session, engine: str, now: datetime) -> dict:
    criteria = billing_shadow.evaluate_switch(db, engine, now)
    return {
        "engine": engine,
        "setting": get_setting(db, ENGINE_SETTING[engine]),
        "mode": billing_engine_mode(db, engine),
        "switch_record": billing_shadow.switch_record(db, engine),
        "shadow_since": _iso(billing_shadow.shadow_since(db, engine)),
        "last_scan": BillingShadowService.last_scan(db, engine),
        "scan_history_count": len(BillingShadowService.scan_history(db, engine)),
        "open_mismatches": billing_shadow.open_mismatch_counts(db, engine),
        "switch_criterion": {"ok": all(item["ok"] for item in criteria), "items": criteria},
    }


@router.get("", responses=COMMON_ERROR_RESPONSES)
def list_engines(db: DbDep):
    now = _now()
    return {
        "engines": [_engine_state(db, engine, now) for engine in ENGINES],
        "scan_enabled": bool(get_setting(db, "billing_shadow_scan_enabled")),
        "scan_dry_run": bool(get_setting(db, "billing_shadow_scan_dry_run")),
        "min_days": int(get_setting(db, "billing_shadow_min_days")),
    }


def _mismatch_payload(row: BillingShadowMismatch) -> dict:
    return {
        "id": row.id,
        "engine": row.engine,
        "check_key": row.check_key,
        "subject_type": row.subject_type,
        "subject_id": row.subject_id,
        "legacy_value": row.legacy_value,
        "v2_value": row.v2_value,
        "context": row.context,
        "occurrences": row.occurrences,
        "first_seen_at": row.first_seen_at,
        "last_seen_at": row.last_seen_at,
        "resolved_at": row.resolved_at,
        "resolved_by_user_id": row.resolved_by_user_id,
        "resolution_note": row.resolution_note,
    }


@router.get("/mismatches", responses=COMMON_ERROR_RESPONSES)
def list_mismatches(
    db: DbDep,
    engine: Optional[EngineName] = None,
    check_key: Annotated[Optional[str], Query(max_length=64)] = None,
    open_only: bool = True,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    stmt = select(BillingShadowMismatch)
    if engine is not None:
        stmt = stmt.where(BillingShadowMismatch.engine == engine)
    if check_key is not None:
        stmt = stmt.where(BillingShadowMismatch.check_key == check_key)
    if open_only:
        stmt = stmt.where(BillingShadowMismatch.resolved_at.is_(None))
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = db.scalars(
        stmt.order_by(BillingShadowMismatch.last_seen_at.desc(), BillingShadowMismatch.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return {"total": total, "items": [_mismatch_payload(row) for row in rows]}


class ResolveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field(min_length=3, max_length=2000)


@router.post("/mismatches/{mismatch_id}/resolve", responses=COMMON_ERROR_RESPONSES)
def resolve_mismatch(mismatch_id: int, body: ResolveBody, auth: AdminDep, db: DbDep):
    row = db.get(BillingShadowMismatch, mismatch_id, with_for_update=True)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mismatch not found")
    if row.resolved_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Mismatch is already resolved")
    row.resolved_at = _now()
    row.resolved_by_user_id = auth["user_id"]
    row.resolution_note = body.note
    AuditService.log(
        db,
        actor_user_id=auth["user_id"],
        action="billing.shadow_mismatch.resolve",
        resource_type="billing_shadow_mismatch",
        resource_id=str(row.id),
        metadata={"engine": row.engine, "check_key": row.check_key, "note": body.note},
    )
    db.commit()
    db.refresh(row)
    return _mismatch_payload(row)


class ScanBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dry_run: bool = False


@router.post("/scan", responses=COMMON_ERROR_RESPONSES)
def run_scan(auth: AdminDep, db: DbDep, body: Optional[ScanBody] = None):
    dry_run = body.dry_run if body is not None else False
    result = BillingShadowService.scan(db, _now(), dry_run=dry_run, scheduled=False)
    if dry_run:
        db.rollback()
        return result
    AuditService.log(
        db,
        actor_user_id=auth["user_id"],
        action="billing.shadow_scan.run",
        resource_type="billing_engine",
        metadata={
            engine: {key: value for key, value in summary.items() if key != "findings"}
            for engine, summary in result["engines"].items()
        },
    )
    db.commit()
    return result


class SwitchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: Literal["legacy", "shadow", "v2"]
    confirm_overpayments_to_reseller_cache: bool = False


def _conflict(code: str, message: str, **extra) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT, detail={"code": code, "message": message, **extra}
    )


def _invoice_rollback_blocker(db: Session) -> Optional[str]:
    if db.scalar(select(InvoiceAdjustment.id).limit(1)) is not None:
        return "an invoice adjustment exists"
    partial = db.scalar(
        select(Refund.id)
        .join(Payment, Payment.id == Refund.payment_id)
        .where(Refund.amount_cents < Payment.amount_cents)
        .limit(1)
    )
    if partial is not None:
        return "a partial refund exists"
    return None


@router.post("/{engine}/switch", responses=COMMON_ERROR_RESPONSES)
def switch_engine(engine: EngineName, body: SwitchBody, auth: AdminDep, db: DbDep):
    now = _now()
    # Serialise switches of one engine: the record row is locked before the mode is read.
    billing_shadow.switch_record(db, engine, for_update=True)
    current = billing_engine_mode(db, engine)
    target = body.to
    if target == current:
        raise _conflict("already_in_mode", f"The {engine} engine is already {current}")
    criteria: Optional[list[dict]] = None
    if target == "v2":
        criteria = billing_shadow.evaluate_switch(
            db,
            engine,
            now,
            confirm_overpayments_to_reseller_cache=body.confirm_overpayments_to_reseller_cache,
        )
        failing = [item for item in criteria if not item["ok"]]
        if failing:
            raise _conflict(
                "switch_criterion_not_met",
                f"Switch criterion not met: {failing[0]['item']}",
                failing=[item["item"] for item in failing],
                criteria=criteria,
            )
    elif target == "shadow":
        if current == "v2" and engine == "invoice":
            blocker = _invoice_rollback_blocker(db)
            if blocker is not None:
                raise _conflict("rollback_refused", f"The invoice engine cannot go back to shadow: {blocker}")
    else:
        if current != "shadow":
            raise _conflict("rollback_refused", f"The {engine} engine can only go to legacy from shadow")

    set_setting(db, ENGINE_SETTING[engine], target, actor_user_id=auth["user_id"])
    record = billing_shadow.switch_record(db, engine)
    record.update(
        {
            "mode": target,
            "previous_mode": current,
            "switched_at": now.isoformat(),
            "switched_by_user_id": auth["user_id"],
        }
    )
    if criteria is not None:
        record["criteria"] = criteria
    else:
        record.pop("criteria", None)
    billing_shadow.write_switch_record(db, engine, record)
    if target == "legacy":
        billing_shadow.clear_shadow_since(db, engine)
    elif target == "shadow":
        # Entering shadow from either side restarts the quiet period.
        billing_shadow.set_shadow_since(
            db, engine, at=now, by=f"engine_switch:user:{auth['user_id']}", reset=True
        )
    AuditService.log(
        db,
        actor_user_id=auth["user_id"],
        action="billing.engine.switch",
        resource_type="billing_engine",
        resource_id=engine,
        metadata={"from": current, "to": target, "criteria": criteria},
    )
    db.commit()
    return _engine_state(db, engine, _now())


class ResyncBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    billing_account_ids: list[int] = Field(min_length=1, max_length=RESYNC_MAX_ACCOUNTS)


@router.post("/credit/resync-account-caches", responses=COMMON_ERROR_RESPONSES)
def resync_account_caches(body: ResyncBody, auth: AdminDep, db: DbDep):
    """Copy ``resellers.cached_balance_cents`` into named reseller accounts' caches.

    Refused once the credit engine is v2, when the account cache is the
    authoritative one.
    """
    if billing_engine_mode(db, "credit") == "v2":
        raise _conflict("credit_engine_v2", "Account caches cannot be resynchronised once the credit engine is v2")
    results = []
    for account_id in sorted(set(body.billing_account_ids)):
        owner = db.execute(
            select(BillingAccount.account_type, BillingAccount.reseller_id).where(BillingAccount.id == account_id)
        ).first()
        if owner is None:
            results.append({"billing_account_id": account_id, "status": "not_found"})
            continue
        if owner.account_type != BillingAccountType.RESELLER or owner.reseller_id is None:
            results.append({"billing_account_id": account_id, "status": "refused_not_reseller"})
            continue
        reseller = db.scalar(select(Reseller).where(Reseller.id == owner.reseller_id).with_for_update())
        account = db.scalar(select(BillingAccount).where(BillingAccount.id == account_id).with_for_update())
        old = int(account.credit_balance_cents or 0)
        new = int(reseller.cached_balance_cents or 0)
        if old == new:
            results.append({"billing_account_id": account_id, "status": "unchanged", "balance_cents": new})
            continue
        account.credit_balance_cents = new
        AuditService.log(
            db,
            actor_user_id=auth["user_id"],
            billing_account_id=account_id,
            action="billing.credit.resync_account_cache",
            resource_type="billing_account",
            resource_id=str(account_id),
            metadata={"reseller_id": reseller.id, "old_cents": old, "new_cents": new},
        )
        db.flush()
        results.append({"billing_account_id": account_id, "status": "updated", "old_cents": old, "new_cents": new})
    db.commit()
    return {"results": results}
