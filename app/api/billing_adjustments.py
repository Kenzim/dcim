"""Credit and debit notes: admin issue and list, client and reseller list and download.

Admin routes live under ``/api/admin/billing``; clients read their own notes under
``/api/client/commerce/adjustments`` and resellers under
``/api/reseller-panel/adjustments``. A client or reseller only ever sees notes
of their own account, and a note of someone else answers 404.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.auth import require_admin, require_reseller, security
from app.core.commerce_auth import (
    _impersonated_by_from_token,
    _token_from_request,
    get_client_billing_account,
    require_client_session,
    require_commerce_enabled,
)
from app.core.config import settings
from app.core.database import get_db
from app.core.openapi_responses import COMMON_ERROR_RESPONSES
from app.dao.reseller_dao import ResellerDAO
from app.models.commerce_account import BillingAccount
from app.models.commerce_invoice_ext import InvoiceLineKind
from app.models.invoice_adjustment import (
    AdjustmentKind,
    AdjustmentOutcome,
    AdjustmentReason,
    InvoiceAdjustment,
)
from app.models.reseller import Invoice, Reseller, ResellerStatus
from app.services.adjustment_pdf_service import AdjustmentPdfService
from app.services.audit_service import AuditService
from app.services.billing.settlement import commit_and_fulfil
from app.services.invoice_adjustment_service import (
    AdjustmentConflictError,
    InvoiceAdjustmentError,
    InvoiceAdjustmentService,
    new_idempotency_key,
)

DbDep = Annotated[Session, Depends(get_db)]
AdminDep = Annotated[dict, Depends(require_admin)]
ClientAccountDep = Annotated[BillingAccount, Depends(get_client_billing_account)]
ResellerAuthDep = Annotated[dict, Depends(require_reseller)]
IdempotencyHeader = Annotated[
    Optional[str], Header(alias="Idempotency-Key", min_length=1, max_length=255)
]
_NOTE_NOT_FOUND = "Credit or debit note not found"


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoteLineBody(RequestModel):
    description: Optional[str] = Field(default=None, min_length=1, max_length=512)
    quantity: StrictInt = Field(default=1, ge=1)
    unit_cents: StrictInt = Field(ge=0)
    tax_cents: StrictInt = Field(default=0, ge=0)
    kind: Optional[InvoiceLineKind] = None
    invoice_line_id: Optional[StrictInt] = Field(default=None, gt=0)


class CreditNoteBody(RequestModel):
    amount_cents: Optional[StrictInt] = Field(default=None, gt=0)
    lines: Optional[list[NoteLineBody]] = Field(default=None, min_length=1, max_length=100)
    reason: AdjustmentReason
    reason_text: Optional[str] = Field(default=None, max_length=512)
    outcome: AdjustmentOutcome = AdjustmentOutcome.REDUCED_BALANCE

    @model_validator(mode="after")
    def _amount_or_lines(self) -> "CreditNoteBody":
        if (self.amount_cents is None) == (self.lines is None):
            raise ValueError("give either amount_cents or lines")
        return self


class DebitNoteBody(RequestModel):
    lines: list[NoteLineBody] = Field(min_length=1, max_length=100)
    reason: AdjustmentReason
    reason_text: Optional[str] = Field(default=None, max_length=512)


def _client_ip(request: Request) -> Optional[str]:
    client_ip = request.client.host if request.client else None
    if settings.trust_x_forwarded_for and "x-forwarded-for" in request.headers:
        client_ip = request.headers["x-forwarded-for"].split(",")[0].strip()
    return client_ip


def _block_impersonation(
    request: Request, credentials: Optional[HTTPAuthorizationCredentials]
) -> None:
    if _impersonated_by_from_token(_token_from_request(request, credentials)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Money actions are blocked during impersonation",
        )


def serialize_note(note: InvoiceAdjustment, *, admin: bool = False, with_lines: bool = False) -> dict:
    payload: dict[str, Any] = {
        "id": note.id,
        "kind": note.kind.value,
        "invoice_id": note.invoice_id,
        "number_display": note.number_display,
        "currency": note.currency,
        "subtotal_cents": note.subtotal_cents,
        "tax_cents": note.tax_cents,
        "total_cents": note.total_cents,
        "reason": note.reason_code.value,
        "reason_text": note.reason_text,
        "outcome": note.outcome.value,
        "status": note.status.value,
        "issued_at": note.issued_at,
        "customer_notes": note.customer_notes,
    }
    if admin:
        payload.update(
            {
                "billing_account_id": note.billing_account_id,
                "reseller_id": note.reseller_id,
                "ledger_entry_id": note.ledger_entry_id,
                "created_by_user_id": note.created_by_user_id,
                "idempotency_key": note.idempotency_key,
            }
        )
    if with_lines:
        payload["lines"] = [
            {
                "id": line.id,
                "invoice_line_id": line.invoice_line_id,
                "kind": line.kind.value,
                "description": line.description,
                "quantity": line.quantity,
                "unit_cents": line.unit_cents,
                "total_cents": line.total_cents,
                "tax_cents": line.tax_cents,
                "period_start": line.period_start,
                "period_end": line.period_end,
            }
            for line in note.lines
        ]
    return payload


def _pdf_response(db: Session, note: InvoiceAdjustment) -> StreamingResponse:
    try:
        pdf_bytes = AdjustmentPdfService.render_pdf_bytes(db, note)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    prefix = "credit-note" if note.kind == AdjustmentKind.CREDIT else "debit-note"
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", note.number_display)
    return StreamingResponse(
        iter([pdf_bytes]),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{prefix}-{safe}.pdf"'},
    )


def _list_notes(
    db: Session,
    *conditions: Any,
    kind: Optional[AdjustmentKind],
    limit: int,
    offset: int,
) -> list[InvoiceAdjustment]:
    stmt = select(InvoiceAdjustment).where(*conditions)
    if kind is not None:
        stmt = stmt.where(InvoiceAdjustment.kind == kind)
    stmt = (
        stmt.options(selectinload(InvoiceAdjustment.lines))
        .order_by(InvoiceAdjustment.issued_at.desc(), InvoiceAdjustment.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(db.execute(stmt).scalars())


def _own_note(db: Session, note_id: int, *conditions: Any) -> InvoiceAdjustment:
    note = db.execute(
        select(InvoiceAdjustment)
        .where(InvoiceAdjustment.id == note_id, *conditions)
        .options(selectinload(InvoiceAdjustment.lines))
    ).scalar_one_or_none()
    if note is None:
        raise HTTPException(status_code=404, detail=_NOTE_NOT_FOUND)
    return note


KindQuery = Annotated[Optional[AdjustmentKind], Query()]
LimitQuery = Annotated[int, Query(ge=1, le=100)]
OffsetQuery = Annotated[int, Query(ge=0)]

# ---------------------------------------------------------------- admin

admin_router = APIRouter(
    prefix="/admin/billing",
    tags=["billing-adjustments-admin"],
    dependencies=[Depends(require_admin)],
    responses=COMMON_ERROR_RESPONSES,
)


def _invoice_or_404(db: Session, invoice_id: int) -> Invoice:
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice


def _issue(
    db: Session,
    request: Request,
    auth: dict,
    invoice: Invoice,
    *,
    action: str,
    issue,
) -> dict:
    try:
        note = issue()
        AuditService.log(
            db,
            actor_user_id=auth.get("user_id"),
            billing_account_id=invoice.billing_account_id,
            action=action,
            resource_type="invoice",
            resource_id=str(invoice.id),
            ip=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            metadata={
                "adjustment_id": note.id,
                "number": note.number_display,
                "total_cents": int(note.total_cents),
                "reason": note.reason_code.value,
            },
        )
        commit_and_fulfil(db)
    except AdjustmentConflictError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InvoiceAdjustmentError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="The note conflicts with one just issued") from exc
    db.refresh(note)
    return serialize_note(note, admin=True, with_lines=True)


@admin_router.post(
    "/invoices/{invoice_id}/credit-notes",
    status_code=status.HTTP_201_CREATED,
    responses=COMMON_ERROR_RESPONSES,
)
def issue_credit_note(
    invoice_id: int,
    body: CreditNoteBody,
    request: Request,
    auth: AdminDep,
    db: DbDep,
    idempotency_key: IdempotencyHeader = None,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Issue a credit note by amount or by lines; it reduces what the invoice still owes."""
    _block_impersonation(request, credentials)
    invoice = _invoice_or_404(db, invoice_id)
    key = idempotency_key or new_idempotency_key("admin-credit-note", invoice.id)
    return _issue(
        db,
        request,
        auth,
        invoice,
        action="credit_note.issue",
        issue=lambda: InvoiceAdjustmentService.issue_credit_note(
            db,
            invoice,
            lines=[line.model_dump(exclude_none=True) for line in body.lines]
            if body.lines is not None
            else None,
            amount_cents=body.amount_cents,
            reason=body.reason,
            outcome=body.outcome,
            actor_user_id=auth.get("user_id"),
            idempotency_key=key,
            reason_text=body.reason_text,
        ),
    )


@admin_router.post(
    "/invoices/{invoice_id}/debit-notes",
    status_code=status.HTTP_201_CREATED,
    responses=COMMON_ERROR_RESPONSES,
)
def issue_debit_note(
    invoice_id: int,
    body: DebitNoteBody,
    request: Request,
    auth: AdminDep,
    db: DbDep,
    idempotency_key: IdempotencyHeader = None,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Issue a debit note that raises what the invoice is worth."""
    _block_impersonation(request, credentials)
    invoice = _invoice_or_404(db, invoice_id)
    key = idempotency_key or new_idempotency_key("admin-debit-note", invoice.id)
    return _issue(
        db,
        request,
        auth,
        invoice,
        action="debit_note.issue",
        issue=lambda: InvoiceAdjustmentService.issue_debit_note(
            db,
            invoice,
            lines=[line.model_dump(exclude_none=True) for line in body.lines],
            reason=body.reason,
            actor_user_id=auth.get("user_id"),
            idempotency_key=key,
            reason_text=body.reason_text,
        ),
    )


@admin_router.get("/invoices/{invoice_id}/adjustments")
def list_invoice_adjustments(invoice_id: int, db: DbDep, kind: KindQuery = None):
    _invoice_or_404(db, invoice_id)
    rows = _list_notes(
        db, InvoiceAdjustment.invoice_id == invoice_id, kind=kind, limit=100, offset=0
    )
    return [serialize_note(row, admin=True, with_lines=True) for row in rows]


@admin_router.get("/adjustments")
def list_adjustments(
    db: DbDep,
    kind: KindQuery = None,
    billing_account_id: Annotated[Optional[int], Query(gt=0)] = None,
    reseller_id: Annotated[Optional[int], Query(gt=0)] = None,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
):
    conditions = []
    if billing_account_id is not None:
        conditions.append(InvoiceAdjustment.billing_account_id == billing_account_id)
    if reseller_id is not None:
        conditions.append(InvoiceAdjustment.reseller_id == reseller_id)
    rows = _list_notes(db, *conditions, kind=kind, limit=limit, offset=offset)
    return [serialize_note(row, admin=True) for row in rows]


@admin_router.get("/adjustments/{adjustment_id}")
def get_adjustment(adjustment_id: int, db: DbDep):
    return serialize_note(_own_note(db, adjustment_id), admin=True, with_lines=True)


@admin_router.get("/adjustments/{adjustment_id}/pdf")
def get_adjustment_pdf(adjustment_id: int, db: DbDep):
    return _pdf_response(db, _own_note(db, adjustment_id))


# ---------------------------------------------------------------- client

client_router = APIRouter(
    prefix="/client/commerce",
    tags=["commerce-client"],
    dependencies=[Depends(require_commerce_enabled), Depends(require_client_session)],
    responses=COMMON_ERROR_RESPONSES,
)


@client_router.get("/adjustments")
def client_list_adjustments(
    account: ClientAccountDep,
    db: DbDep,
    kind: KindQuery = None,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
):
    rows = _list_notes(
        db, InvoiceAdjustment.billing_account_id == account.id, kind=kind, limit=limit, offset=offset
    )
    return [serialize_note(row) for row in rows]


@client_router.get("/adjustments/{adjustment_id}")
def client_get_adjustment(adjustment_id: int, account: ClientAccountDep, db: DbDep):
    note = _own_note(db, adjustment_id, InvoiceAdjustment.billing_account_id == account.id)
    return serialize_note(note, with_lines=True)


@client_router.get("/adjustments/{adjustment_id}/pdf")
def client_get_adjustment_pdf(adjustment_id: int, account: ClientAccountDep, db: DbDep):
    note = _own_note(db, adjustment_id, InvoiceAdjustment.billing_account_id == account.id)
    return _pdf_response(db, note)


# ---------------------------------------------------------------- reseller

reseller_router = APIRouter(
    prefix="/reseller-panel",
    tags=["reseller-panel"],
    responses=COMMON_ERROR_RESPONSES,
)


def _current_reseller(auth: ResellerAuthDep, db: DbDep) -> Reseller:
    reseller = ResellerDAO.get_by_user_id(db, int(auth["user_id"]))
    if reseller is None:
        raise HTTPException(status_code=403, detail="Reseller account not found")
    if reseller.status != ResellerStatus.ACTIVE:
        raise HTTPException(status_code=403, detail="Reseller account is not active")
    return reseller


ResellerDep = Annotated[Reseller, Depends(_current_reseller)]


@reseller_router.get("/adjustments")
def reseller_list_adjustments(
    reseller: ResellerDep,
    db: DbDep,
    kind: KindQuery = None,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
):
    rows = _list_notes(
        db, InvoiceAdjustment.reseller_id == reseller.id, kind=kind, limit=limit, offset=offset
    )
    return [serialize_note(row) for row in rows]


@reseller_router.get("/adjustments/{adjustment_id}")
def reseller_get_adjustment(adjustment_id: int, reseller: ResellerDep, db: DbDep):
    note = _own_note(db, adjustment_id, InvoiceAdjustment.reseller_id == reseller.id)
    return serialize_note(note, with_lines=True)


@reseller_router.get("/adjustments/{adjustment_id}/pdf")
def reseller_get_adjustment_pdf(adjustment_id: int, reseller: ResellerDep, db: DbDep):
    note = _own_note(db, adjustment_id, InvoiceAdjustment.reseller_id == reseller.id)
    return _pdf_response(db, note)


router = APIRouter()
router.include_router(admin_router)
router.include_router(client_router)
router.include_router(reseller_router)
