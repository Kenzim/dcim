"""Admin API for refunds (docs/billing-phase1-design.md 7): retail and reseller payments.

- ``GET /admin/billing/refunds`` and ``GET /admin/billing/refunds/{id}``: refunds.
- ``POST /admin/billing/refunds``: refund (part of) a payment. The
  ``Idempotency-Key`` header (one per refund dialog) makes a retry return the
  same refund; without it a key ``admin-refund:<payment_id>:<uuid4>`` is made.
  Answers 201 when the refund is done or failed, 202 while the gateway has not
  confirmed it.
- ``POST /admin/billing/refunds/{id}/dispatch``: send a reserved gateway refund now.
- ``POST /admin/billing/refunds/{id}/resolve``: staff resolve a ``review`` (or
  ``pending``) refund as ``succeeded`` (with the gateway reference) or ``failed``.
- ``GET /admin/billing/refund-reviews`` and ``POST /admin/billing/refund-reviews/resolve``:
  money recorded for a refund decision, resolved by a refund to the original
  method, a credit to the account, or a dismissal.

Every change is audited.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import require_admin
from app.core.database import get_db
from app.core.openapi_responses import COMMON_ERROR_RESPONSES
from app.models.refund import Refund, RefundDestination, RefundReason, RefundStatus
from app.services.audit_service import AuditService
from app.services.billing.order_refunds import (
    REVIEW_ACTIONS,
    RefundReviewError,
    list_refund_reviews,
    resolve_refund_review,
)
from app.services.refund_service import RefundError, RefundService

DbDep = Annotated[Session, Depends(get_db)]
AdminDep = Annotated[dict, Depends(require_admin)]

router = APIRouter(
    prefix="/admin/billing",
    tags=["billing-refunds-admin"],
    dependencies=[Depends(require_admin)],
    responses=COMMON_ERROR_RESPONSES,
)

_ERROR_STATUS = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "idempotency_conflict": status.HTTP_409_CONFLICT,
    "already_resolved": status.HTTP_409_CONFLICT,
    "gateway_unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _error(exc: Exception) -> HTTPException:
    code = getattr(exc, "code", "refund_error")
    return HTTPException(
        status_code=_ERROR_STATUS.get(code, status.HTTP_400_BAD_REQUEST),
        detail={"code": code, "message": str(exc)},
    )


def serialize_refund(refund: Refund) -> dict[str, Any]:
    return {
        "id": refund.id,
        "payment_id": refund.payment_id,
        "invoice_id": refund.invoice_id,
        "billing_account_id": refund.billing_account_id,
        "reseller_id": refund.reseller_id,
        "amount_cents": int(refund.amount_cents),
        "currency": refund.currency,
        "destination": refund.destination.value,
        "gateway": refund.gateway,
        "reason": refund.reason.value,
        "reason_text": refund.reason_text,
        "status": refund.status.value,
        "issue_credit_note": bool(refund.issue_credit_note),
        "adjustment_id": refund.adjustment_id,
        "ledger_entry_id": refund.ledger_entry_id,
        "gateway_refund_ref": refund.gateway_refund_ref,
        "attempts": int(refund.attempts or 0),
        "next_attempt_at": refund.next_attempt_at,
        "failure_code": refund.failure_code,
        "failure_message": refund.failure_message,
        "requested_by_user_id": refund.requested_by_user_id,
        "requested_at": refund.requested_at,
        "completed_at": refund.completed_at,
    }


class RefundCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payment_id: int
    amount_cents: int = Field(gt=0)
    destination: RefundDestination = RefundDestination.GATEWAY
    reason: RefundReason = RefundReason.CUSTOMER_REQUEST
    reason_text: Optional[str] = Field(default=None, max_length=512)
    issue_credit_note: Optional[bool] = None
    credit_note_lines: Optional[list[dict[str, Any]]] = None


class RefundResolveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["succeeded", "failed"]
    gateway_refund_ref: Optional[str] = Field(default=None, max_length=255)
    note: str = Field(min_length=1, max_length=1000)


class RefundReviewResolveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_id: int
    reason: str = Field(min_length=1, max_length=64)
    action: Literal[REVIEW_ACTIONS]  # type: ignore[valid-type]
    payment_id: Optional[int] = None
    amount_cents: Optional[int] = Field(default=None, gt=0)
    note: Optional[str] = Field(default=None, max_length=1000)


@router.get("/refunds", responses=COMMON_ERROR_RESPONSES)
def list_refunds(
    db: DbDep,
    status_filter: Annotated[Optional[RefundStatus], Query(alias="status")] = None,
    payment_id: Optional[int] = None,
    invoice_id: Optional[int] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
):
    statement = select(Refund).order_by(Refund.id.desc()).limit(limit)
    if status_filter is not None:
        statement = statement.where(Refund.status == status_filter)
    if payment_id is not None:
        statement = statement.where(Refund.payment_id == payment_id)
    if invoice_id is not None:
        statement = statement.where(Refund.invoice_id == invoice_id)
    return {"items": [serialize_refund(row) for row in db.execute(statement).scalars()]}


@router.get("/refunds/{refund_id}", responses=COMMON_ERROR_RESPONSES)
def get_refund(refund_id: int, db: DbDep):
    refund = db.get(Refund, refund_id)
    if refund is None:
        raise HTTPException(status_code=404, detail="Refund not found")
    return serialize_refund(refund)


def _refund_response(refund: Refund) -> JSONResponse:
    code = status.HTTP_202_ACCEPTED if refund.status == RefundStatus.PENDING else status.HTTP_201_CREATED
    return JSONResponse(status_code=code, content=_jsonable(serialize_refund(refund)))


def _jsonable(payload: dict[str, Any]) -> dict[str, Any]:
    from fastapi.encoders import jsonable_encoder

    return jsonable_encoder(payload)


def _staff_key(header: Optional[str]) -> Optional[str]:
    """Namespace a client-chosen key so it can never match a system key such as ``order-cancel:``."""
    value = (header or "").strip()
    return f"admin:{value}" if value else None


@router.post("/refunds", status_code=201, responses=COMMON_ERROR_RESPONSES)
def create_refund(
    body: RefundCreateBody,
    db: DbDep,
    auth: AdminDep,
    idempotency_key: Annotated[Optional[str], Header(alias="Idempotency-Key", max_length=200)] = None,
):
    key = _staff_key(idempotency_key) or f"admin-refund:{body.payment_id}:{uuid.uuid4()}"
    try:
        refund = RefundService.request(
            db,
            payment_id=body.payment_id,
            amount_cents=body.amount_cents,
            destination=body.destination,
            reason=body.reason,
            actor_user_id=auth.get("user_id"),
            idempotency_key=key,
            reason_text=body.reason_text,
            issue_credit_note=body.issue_credit_note,
            credit_note_lines=body.credit_note_lines,
        )
    except RefundError as exc:
        db.rollback()
        raise _error(exc) from exc
    db.commit()
    return _refund_response(refund)


@router.post("/refunds/{refund_id}/dispatch", responses=COMMON_ERROR_RESPONSES)
def dispatch_refund(refund_id: int, db: DbDep, auth: AdminDep):
    try:
        refund = RefundService.dispatch(db, refund_id)
    except RefundError as exc:
        db.rollback()
        raise _error(exc) from exc
    AuditService.log(
        db,
        actor_user_id=auth.get("user_id"),
        billing_account_id=refund.billing_account_id,
        action="billing.refund_dispatched",
        resource_type="refund",
        resource_id=str(refund.id),
        metadata={"status": refund.status.value},
    )
    db.commit()
    return _refund_response(refund)


@router.post("/refunds/{refund_id}/resolve", responses=COMMON_ERROR_RESPONSES)
def resolve_refund(refund_id: int, body: RefundResolveBody, db: DbDep, auth: AdminDep):
    refund = db.get(Refund, refund_id)
    if refund is None:
        raise HTTPException(status_code=404, detail="Refund not found")
    if refund.status not in (RefundStatus.REVIEW, RefundStatus.PENDING):
        raise HTTPException(
            status_code=409,
            detail={"code": "not_open", "message": f"Refund is {refund.status.value}"},
        )
    if (
        body.outcome == "succeeded"
        and refund.destination == RefundDestination.GATEWAY
        and not (body.gateway_refund_ref or refund.gateway_refund_ref)
    ):
        raise HTTPException(
            status_code=400,
            detail={"code": "gateway_refund_ref_required", "message": "Give the gateway refund reference"},
        )
    actor = auth.get("user_id")
    try:
        if body.outcome == "succeeded":
            refund = RefundService.complete(db, refund_id, gateway_refund_ref=body.gateway_refund_ref)
        else:
            refund = RefundService.fail(
                db,
                refund_id,
                failure_code="staff_marked_failed",
                failure_message=body.note,
                actor_user_id=actor,
            )
    except RefundError as exc:
        db.rollback()
        raise _error(exc) from exc
    AuditService.log(
        db,
        actor_user_id=actor,
        billing_account_id=refund.billing_account_id,
        action="billing.refund_resolved",
        resource_type="refund",
        resource_id=str(refund.id),
        metadata={
            "outcome": body.outcome,
            "gateway_refund_ref": refund.gateway_refund_ref,
            "note": body.note,
            "status": refund.status.value,
        },
    )
    db.commit()
    return serialize_refund(refund)


@router.get("/refund-reviews", responses=COMMON_ERROR_RESPONSES)
def get_refund_reviews(
    db: DbDep,
    open_only: Annotated[bool, Query(alias="open")] = True,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
):
    items = list_refund_reviews(db, limit=limit)
    if open_only:
        items = [item for item in items if not item["resolved"]]
    return {"items": items}


@router.post("/refund-reviews/resolve", responses=COMMON_ERROR_RESPONSES)
def resolve_review(
    body: RefundReviewResolveBody,
    db: DbDep,
    auth: AdminDep,
    idempotency_key: Annotated[Optional[str], Header(alias="Idempotency-Key", max_length=200)] = None,
):
    key = _staff_key(idempotency_key) or (
        f"refund-review:{body.invoice_id}:{body.payment_id}:{body.reason}:{uuid.uuid4()}"
    )
    try:
        result = resolve_refund_review(
            db,
            invoice_id=body.invoice_id,
            payment_id=body.payment_id,
            reason=body.reason,
            action=body.action,
            amount_cents=body.amount_cents,
            note=body.note,
            actor_user_id=auth.get("user_id"),
            idempotency_key=key,
        )
    except RefundReviewError as exc:
        db.rollback()
        raise _error(exc) from exc
    db.commit()
    if result.get("refund_id") is None and (result.get("resolution") or {}).get("refund_id"):
        result["refund_id"] = result["resolution"]["refund_id"]
    if result.get("refund_id") is not None:
        refund = db.get(Refund, int(result["refund_id"]))
        result["refund"] = serialize_refund(refund) if refund is not None else None
    return result
