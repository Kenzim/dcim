"""Cancellation requests and non-payment policies (docs/billing-phase2-design.md 10.4 to 10.6).

Client: ``GET /api/client/commerce/cancellation-requests`` and
``POST .../{id}/withdraw``. Reseller panel: the same for its own services,
plus ``POST`` to request one. Admin: the queue with filters, ``POST`` on
behalf, ``POST .../{id}/withdraw``, and ``GET``/``PUT``
``/api/admin/billing/nonpayment-policies``.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.api.reseller_panel import ResellerDep
from app.core.auth import require_admin
from app.core.commerce_auth import (
    get_client_billing_account,
    require_client_session,
    require_commerce_enabled,
)
from app.core.database import get_db
from app.core.openapi_responses import COMMON_ERROR_RESPONSES
from app.models.billing_jobs import (
    BillingNonpaymentPolicy,
    CancellationRequest,
    CancellationStatus,
    NonpaymentPolicyScope,
    UnpaidInvoiceAction,
)
from app.models.commerce_account import BillingAccount
from app.models.reseller import ResellerProductAccess, ServiceBilling
from app.services.audit_service import AuditService
from app.services.billing.cancellations import (
    CancellationError,
    CancellationService,
    serialise_request,
)

DbDep = Annotated[Session, Depends(get_db)]
AdminDep = Annotated[dict, Depends(require_admin)]
ClientAccountDep = Annotated[BillingAccount, Depends(get_client_billing_account)]
IdempotencyHeader = Annotated[
    Optional[str], Header(alias="Idempotency-Key", min_length=1, max_length=128)
]
StatusQuery = Annotated[Optional[CancellationStatus], Query()]
LimitQuery = Annotated[int, Query(ge=1, le=200)]
OffsetQuery = Annotated[int, Query(ge=0)]


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CancellationBody(RequestModel):
    service_id: StrictInt = Field(gt=0)
    type: Literal["end_of_period", "immediate"] = "end_of_period"
    reason: Optional[str] = Field(default=None, max_length=1000)
    invoice_action: Literal["cancel_unpaid", "keep"] = "cancel_unpaid"


class PolicyBody(RequestModel):
    scope: Literal["retail_default", "reseller_default", "product"]
    product_id: Optional[StrictInt] = Field(default=None, gt=0)
    suspend_after_days: Optional[StrictInt] = Field(default=None, ge=0, le=365)
    terminate_after_days: Optional[StrictInt] = Field(default=None, ge=0, le=730)
    termination_notice_days: StrictInt = Field(default=7, ge=0, le=90)
    unpaid_invoice_action_on_terminate: Literal["keep", "cancel"] = "keep"
    enabled: bool = True


def _http(exc: CancellationError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


def _list(db: Session, *conditions, status: Optional[CancellationStatus], limit: int, offset: int) -> list[dict]:
    statement = select(CancellationRequest).where(*conditions)
    if status is not None:
        statement = statement.where(CancellationRequest.status == status)
    rows = db.execute(
        statement.order_by(CancellationRequest.id.desc()).limit(limit).offset(offset)
    ).scalars()
    return [serialise_request(row) for row in rows]


def _own_request(db: Session, request_id: int, *condition) -> CancellationRequest:
    request = db.execute(
        select(CancellationRequest).where(CancellationRequest.id == request_id, *condition)
    ).scalar_one_or_none()
    if request is None:
        raise HTTPException(status_code=404, detail="Cancellation request not found")
    return request


def _withdraw(db: Session, request_id: int, actor_user_id: Optional[int]) -> dict:
    try:
        request = CancellationService.withdraw(db, request_id, actor_user_id=actor_user_id)
    except CancellationError as exc:
        db.rollback()
        raise _http(exc) from exc
    result = serialise_request(request)
    db.commit()
    return result


def _reseller_key(reseller_id: int, key: Optional[str]) -> Optional[str]:
    """A reseller's Idempotency-Key, scoped to that reseller.

    Keys are unique across all requests, so a key another reseller used is a
    different key here, and its existence is never revealed. The header is
    hashed so the stored key stays within 128 characters.
    """
    if key is None or not key.strip():
        return None
    return f"reseller:{int(reseller_id)}:{hashlib.sha256(key.strip().encode()).hexdigest()}"


def _request(db: Session, body: CancellationBody, *, actor_user_id: Optional[int], source: str, key: Optional[str]) -> dict:
    try:
        request = CancellationService.request(
            db,
            service_id=body.service_id,
            type=body.type,
            reason=body.reason,
            actor_user_id=actor_user_id,
            source=source,
            idempotency_key=key or f"{source}:{body.service_id}:{uuid4().hex}",
            invoice_action=body.invoice_action,
        )
    except CancellationError as exc:
        db.rollback()
        raise _http(exc) from exc
    result = CancellationService.summary(db, request)
    db.commit()
    return result


# ---------------------------------------------------------------- client

client_router = APIRouter(
    prefix="/client/commerce",
    tags=["commerce-client"],
    dependencies=[Depends(require_commerce_enabled), Depends(require_client_session)],
    responses=COMMON_ERROR_RESPONSES,
)


@client_router.get("/cancellation-requests")
def client_list_cancellation_requests(
    account: ClientAccountDep,
    db: DbDep,
    status: StatusQuery = None,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
):
    return _list(
        db, CancellationRequest.billing_account_id == account.id, status=status, limit=limit, offset=offset
    )


@client_router.post("/cancellation-requests/{request_id}/withdraw")
def client_withdraw_cancellation_request(request_id: int, account: ClientAccountDep, db: DbDep):
    _own_request(db, request_id, CancellationRequest.billing_account_id == account.id)
    return _withdraw(db, request_id, account.user_id)


# ---------------------------------------------------------------- reseller

reseller_router = APIRouter(
    prefix="/reseller-panel",
    tags=["reseller-panel"],
    responses=COMMON_ERROR_RESPONSES,
)


@reseller_router.get("/cancellation-requests")
def reseller_list_cancellation_requests(
    reseller: ResellerDep,
    db: DbDep,
    status: StatusQuery = None,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
):
    return _list(db, CancellationRequest.reseller_id == reseller.id, status=status, limit=limit, offset=offset)


@reseller_router.post("/cancellation-requests")
def reseller_request_cancellation(
    body: CancellationBody,
    reseller: ResellerDep,
    db: DbDep,
    idempotency_key: IdempotencyHeader = None,
):
    try:
        CancellationService.check_owner(db, body.service_id, reseller_id=reseller.id)
    except CancellationError as exc:
        raise _http(exc) from exc
    return _request(
        db, body, actor_user_id=reseller.user_id, source="reseller", key=_reseller_key(reseller.id, idempotency_key)
    )


@reseller_router.post("/cancellation-requests/{request_id}/withdraw")
def reseller_withdraw_cancellation_request(request_id: int, reseller: ResellerDep, db: DbDep):
    _own_request(db, request_id, CancellationRequest.reseller_id == reseller.id)
    return _withdraw(db, request_id, reseller.user_id)


# ---------------------------------------------------------------- admin

admin_router = APIRouter(
    prefix="/admin/billing",
    tags=["billing-cancellations-admin"],
    dependencies=[Depends(require_admin)],
    responses=COMMON_ERROR_RESPONSES,
)


@admin_router.get("/cancellation-requests")
def admin_list_cancellation_requests(
    db: DbDep,
    status: StatusQuery = None,
    service_id: Annotated[Optional[int], Query(gt=0)] = None,
    billing_account_id: Annotated[Optional[int], Query(gt=0)] = None,
    reseller_id: Annotated[Optional[int], Query(gt=0)] = None,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
):
    conditions = []
    if service_id is not None:
        conditions.append(CancellationRequest.service_id == service_id)
    if billing_account_id is not None:
        conditions.append(CancellationRequest.billing_account_id == billing_account_id)
    if reseller_id is not None:
        conditions.append(CancellationRequest.reseller_id == reseller_id)
    return _list(db, *conditions, status=status, limit=limit, offset=offset)


@admin_router.post("/cancellation-requests")
def admin_request_cancellation(
    body: CancellationBody,
    auth: AdminDep,
    db: DbDep,
    idempotency_key: IdempotencyHeader = None,
):
    return _request(db, body, actor_user_id=auth.get("user_id"), source="admin", key=idempotency_key)


@admin_router.post("/cancellation-requests/{request_id}/withdraw")
def admin_withdraw_cancellation_request(request_id: int, auth: AdminDep, db: DbDep):
    _own_request(db, request_id)
    return _withdraw(db, request_id, auth.get("user_id"))


def _serialise_policy(row: BillingNonpaymentPolicy) -> dict:
    return {
        "id": row.id,
        "scope": getattr(row.scope, "value", row.scope),
        "product_id": row.product_id or None,
        "suspend_after_days": row.suspend_after_days,
        "terminate_after_days": row.terminate_after_days,
        "termination_notice_days": row.termination_notice_days,
        "unpaid_invoice_action_on_terminate": getattr(
            row.unpaid_invoice_action_on_terminate, "value", row.unpaid_invoice_action_on_terminate
        ),
        "enabled": bool(row.enabled),
    }


def _reseller_sold(db: Session, product_id: int) -> bool:
    return bool(
        db.scalar(select(exists().where(ResellerProductAccess.product_id == product_id)))
        or db.scalar(
            select(
                exists().where(
                    ServiceBilling.product_id == product_id, ServiceBilling.reseller_id.is_not(None)
                )
            )
        )
    )


@admin_router.get("/nonpayment-policies")
def admin_list_nonpayment_policies(db: DbDep):
    rows = db.execute(
        select(BillingNonpaymentPolicy).order_by(BillingNonpaymentPolicy.scope, BillingNonpaymentPolicy.product_id)
    ).scalars()
    return [_serialise_policy(row) for row in rows]


@admin_router.put("/nonpayment-policies")
def admin_put_nonpayment_policy(body: PolicyBody, auth: AdminDep, db: DbDep):
    """Create or replace the policy row of ``(scope, product_id)``.

    Reseller suspension stays with the recurring pass: ``suspend_after_days``
    must be NULL on ``reseller_default`` and on rows for products resellers sell.
    """
    from app.models.product_catalog import Product

    scope = NonpaymentPolicyScope(body.scope)
    if scope == NonpaymentPolicyScope.PRODUCT:
        if body.product_id is None:
            raise HTTPException(status_code=422, detail="product_id is required for a product policy")
        if db.get(Product, body.product_id) is None:
            raise HTTPException(status_code=404, detail="Product not found")
        product_id = int(body.product_id)
    else:
        if body.product_id is not None:
            raise HTTPException(status_code=422, detail="product_id is only for a product policy")
        product_id = 0
    if body.suspend_after_days is not None and (
        scope == NonpaymentPolicyScope.RESELLER_DEFAULT
        or (scope == NonpaymentPolicyScope.PRODUCT and _reseller_sold(db, product_id))
    ):
        raise HTTPException(
            status_code=422,
            detail="Reseller suspension is set by the reseller overdue policy; suspend_after_days must be empty",
        )
    if (
        body.suspend_after_days is not None
        and body.terminate_after_days is not None
        and body.terminate_after_days < body.suspend_after_days
    ):
        raise HTTPException(status_code=422, detail="terminate_after_days must not be before suspend_after_days")
    if body.terminate_after_days is not None and body.termination_notice_days > body.terminate_after_days:
        raise HTTPException(status_code=422, detail="termination_notice_days must not exceed terminate_after_days")

    db.flush()
    row = db.execute(
        select(BillingNonpaymentPolicy)
        .where(BillingNonpaymentPolicy.scope == scope, BillingNonpaymentPolicy.product_id == product_id)
        .with_for_update()
    ).scalar_one_or_none()
    before = _serialise_policy(row) if row is not None else None
    if row is None:
        row = BillingNonpaymentPolicy(scope=scope, product_id=product_id)
        db.add(row)
    row.suspend_after_days = body.suspend_after_days
    row.terminate_after_days = body.terminate_after_days
    row.termination_notice_days = body.termination_notice_days
    row.unpaid_invoice_action_on_terminate = UnpaidInvoiceAction(body.unpaid_invoice_action_on_terminate)
    row.enabled = body.enabled
    db.flush()
    after = _serialise_policy(row)
    AuditService.log(
        db,
        actor_user_id=auth.get("user_id"),
        action="billing.nonpayment_policy_updated",
        resource_type="billing_nonpayment_policy",
        resource_id=str(row.id),
        metadata={"before": before, "after": after},
    )
    db.commit()
    return after


router = APIRouter()
router.include_router(admin_router)
router.include_router(client_router)
router.include_router(reseller_router)
