"""Client commerce API: browse, quote, checkout, orders, invoices, support."""

from __future__ import annotations

import secrets
from dataclasses import asdict
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.core.commerce_auth import (
    get_client_billing_account,
    require_account_owns,
    require_client_session,
    require_commerce_enabled,
)
from app.core.config import settings
from app.core.database import get_db
from app.core.openapi_responses import COMMON_ERROR_RESPONSES
from app.core.rate_limit import enforce_rate_limit
from app.dao.order_dao import OrderDAO
from app.dao.storefront_dao import FrontendProductDAO
from app.models.commerce_account import BillingAccount
from app.models.commerce_audit import UserAuditEvent
from app.models.commerce_order import Order, OrderStatus
from app.models.reseller import Invoice, InvoiceStatus
from app.models.storefront import FrontendProductVisibility
from app.models.support_ticket import TicketPriority, TicketStatus
from app.services.billing_account_service import BillingAccountService
from app.services.checkout_service import CheckoutError, CheckoutService
from app.services.commerce_lifecycle_service import (
    CommerceLifecycleError,
    CommerceLifecycleService,
)
from app.services.discord_link_service import DiscordLinkError, DiscordLinkService
from app.services.email_message_service import EmailMessageService
from app.services.invoice_pdf_service import InvoicePdfService
from app.services.invoice_service import InvoiceService
from app.services.ticket_service import TicketService

router = APIRouter(
    prefix="/client/commerce",
    tags=["commerce-client"],
    dependencies=[Depends(require_commerce_enabled), Depends(require_client_session)],
    responses=COMMON_ERROR_RESPONSES,
)


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QuoteRequest(RequestModel):
    frontend_product_id: StrictInt = Field(gt=0)
    price_plan_id: StrictInt = Field(gt=0)
    cycle_interval: Optional[str] = Field(default=None, max_length=32)
    options: dict[str, Any] = Field(default_factory=dict)
    coupon_code: Optional[str] = Field(default=None, max_length=64)


class CheckoutRequest(RequestModel):
    frontend_product_id: StrictInt = Field(gt=0)
    price_plan_id: StrictInt = Field(gt=0)
    cycle_interval: Optional[str] = Field(default=None, max_length=32)
    options: dict[str, Any] = Field(default_factory=dict)
    coupon_code: Optional[str] = Field(default=None, max_length=64)
    checkout_nonce: str = Field(min_length=8, max_length=128)
    terms_version: Optional[str] = Field(default=None, max_length=64)


class ProfileUpdate(RequestModel):
    legal_name: Optional[str] = Field(default=None, max_length=255)
    company: Optional[str] = Field(default=None, max_length=255)
    address_line1: Optional[str] = Field(default=None, max_length=255)
    address_line2: Optional[str] = Field(default=None, max_length=255)
    city: Optional[str] = Field(default=None, max_length=128)
    region: Optional[str] = Field(default=None, max_length=128)
    postal_code: Optional[str] = Field(default=None, max_length=32)
    country: Optional[str] = Field(default=None, max_length=2)
    phone: Optional[str] = Field(default=None, max_length=32)
    tax_id: Optional[str] = Field(default=None, max_length=64)
    invoice_email: Optional[str] = Field(default=None, max_length=320)


class DiscordCallbackBody(RequestModel):
    code: str = Field(min_length=1, max_length=2048)
    state: str = Field(min_length=1, max_length=128)


class TicketCreate(RequestModel):
    department_id: StrictInt = Field(gt=0)
    subject: str = Field(min_length=1, max_length=512)
    body_text: str = Field(min_length=1, max_length=65535)
    priority: TicketPriority = TicketPriority.MEDIUM
    service_id: Optional[StrictInt] = Field(default=None, gt=0)


class TicketMessageCreate(RequestModel):
    body_text: str = Field(min_length=1, max_length=65535)


class TotpConfirmBody(RequestModel):
    code: str = Field(min_length=6, max_length=8)


class TotpDisableBody(RequestModel):
    code: str = Field(min_length=6, max_length=8)


class ServiceCancelBody(RequestModel):
    when: str = Field(default="end_of_cycle", pattern="^(end_of_cycle|immediate)$")
    reason: Optional[str] = Field(default=None, max_length=2000)


class ServiceUpgradeBody(RequestModel):
    frontend_product_id: StrictInt = Field(gt=0)
    price_plan_id: StrictInt = Field(gt=0)
    cycle_interval: Optional[str] = Field(default=None, max_length=32)


def _client_ip(request: Request) -> Optional[str]:
    client_ip = request.client.host if request.client else None
    if settings.trust_x_forwarded_for and "x-forwarded-for" in request.headers:
        client_ip = request.headers["x-forwarded-for"].split(",")[0].strip()
    return client_ip


def _block_impersonation_money_actions(auth: dict) -> None:
    if auth.get("impersonated_by"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Money actions are blocked during impersonation",
        )


def _serialize_client_product(row, *, detailed: bool = False) -> dict:
    payload = {
        "id": row.id,
        "name": row.name,
        "slug": row.slug,
        "short_description": row.short_description,
        "category_id": row.category_id,
        "service_type": row.service_type,
        "visibility": row.visibility.value,
        "features": row.features or [],
        "specs": row.specs or {},
        "require_discord": row.require_discord,
    }
    if detailed:
        payload["description_html"] = row.description_html
        payload["price_plans"] = [
            {
                "id": plan.id,
                "name": plan.name,
                "currency": plan.currency,
                "pricing_model": plan.pricing_model.value,
                "setup_cents": plan.setup_cents,
                "trial_days": plan.trial_days,
                "cycles": [
                    {
                        "id": cycle.id,
                        "interval": cycle.interval.value,
                        "price_cents": cycle.price_cents,
                    }
                    for cycle in plan.cycles
                    if cycle.enabled
                ],
            }
            for plan in row.price_plans
            if plan.enabled
        ]
        payload["options"] = [
            {
                "id": opt.id,
                "code": opt.code,
                "name": opt.name,
                "required": opt.required,
                "option_type": opt.option_type.value,
                "values": [
                    {
                        "id": val.id,
                        "code": val.code,
                        "name": val.name,
                        "price_delta_cents": val.price_delta_cents,
                    }
                    for val in opt.values
                    if val.enabled
                ],
            }
            for opt in row.options
        ]
    return payload


def _serialize_order(order: Order) -> dict:
    return {
        "id": order.id,
        "order_number": order.order_number,
        "status": order.status.value,
        "currency": order.currency,
        "setup_cents": order.setup_cents,
        "recurring_cents": order.recurring_cents,
        "tax_cents": order.tax_cents,
        "discount_cents": order.discount_cents,
        "total_cents": order.total_cents,
        "coupon_code": order.coupon_code,
        "invoice_id": order.invoice_id,
        "created_at": order.created_at,
        "updated_at": order.updated_at,
        "items": [
            {
                "id": item.id,
                "name_snapshot": item.name_snapshot,
                "fulfill_status": item.fulfill_status.value,
                "service_id": item.service_id,
            }
            for item in (order.items or [])
        ],
    }


def _serialize_invoice(db: Session, invoice: Invoice) -> dict:
    payload = InvoiceService.serialize(db, invoice)
    payload.pop("reseller_id", None)
    return payload


def _serialize_ticket(ticket, *, include_messages: bool = False) -> dict:
    payload = {
        "id": ticket.id,
        "ticket_number": ticket.ticket_number,
        "department_id": ticket.department_id,
        "department_name": ticket.department.name if ticket.department else None,
        "service_id": ticket.service_id,
        "subject": ticket.subject,
        "status": ticket.status.value,
        "priority": ticket.priority.value,
        "created_at": ticket.created_at,
        "updated_at": ticket.updated_at,
        "closed_at": ticket.closed_at,
    }
    if include_messages:
        payload["messages"] = [
            {
                "id": msg.id,
                "body_text": msg.body_text,
                "author_user_id": msg.author_user_id,
                "created_at": msg.created_at,
            }
            for msg in ticket.messages
            if not msg.is_staff_note
        ]
    return payload


# --- Products ---


@router.get("/products")
def browse_products(
    category_id: Optional[int] = None,
    q: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    *,
    db: Annotated[Session, Depends(get_db)],
):
    rows = FrontendProductDAO.list_enabled_products(
        db,
        visibility=[
            FrontendProductVisibility.PUBLIC,
            FrontendProductVisibility.PRIVATE,
        ],
        category_id=category_id,
        q=q,
        limit=limit,
        offset=offset,
    )
    return [_serialize_client_product(row) for row in rows]


@router.get("/products/{product_id}")
def get_product(product_id: int, db: Annotated[Session, Depends(get_db)]):
    row = FrontendProductDAO.get_product_with_plans(db, product_id)
    if row is None or not row.enabled:
        raise HTTPException(status_code=404, detail="Product not found")
    if row.visibility == FrontendProductVisibility.HIDDEN:
        raise HTTPException(status_code=404, detail="Product not found")
    return _serialize_client_product(row, detailed=True)


@router.post("/quote")
def quote_checkout(
    body: QuoteRequest,
    *,
    account: Annotated[BillingAccount, Depends(get_client_billing_account)],
    db: Annotated[Session, Depends(get_db)],
):
    try:
        quote = CheckoutService.quote(
            db,
            account,
            body.frontend_product_id,
            body.price_plan_id,
            body.cycle_interval,
            body.options,
            body.coupon_code,
        )
    except CheckoutError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        **asdict(quote),
        "lines": [asdict(line) for line in quote.lines],
    }


@router.post("/checkout", status_code=status.HTTP_201_CREATED)
def checkout(
    body: CheckoutRequest,
    request: Request,
    auth: Annotated[dict, Depends(require_client_session)],
    account: Annotated[BillingAccount, Depends(get_client_billing_account)],
    db: Annotated[Session, Depends(get_db)],
):
    _block_impersonation_money_actions(auth)
    enforce_rate_limit(
        f"commerce:checkout:account:{account.id}",
        settings.commerce_checkout_rate_limit_per_account,
        settings.commerce_checkout_rate_limit_window_seconds,
    )
    try:
        order = CheckoutService.place_order(
            db,
            account=account,
            user_id=int(auth["user_id"]),
            frontend_product_id=body.frontend_product_id,
            price_plan_id=body.price_plan_id,
            cycle_interval=body.cycle_interval,
            options=body.options,
            coupon_code=body.coupon_code,
            checkout_nonce=body.checkout_nonce,
            terms_version=body.terms_version,
            client_ip=_client_ip(request),
        )
    except CheckoutError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    order = OrderDAO.get(db, order.id)
    return _serialize_order(order)


# --- Orders ---


@router.get("/orders")
def list_orders(
    status_filter: Optional[OrderStatus] = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    *,
    account: Annotated[BillingAccount, Depends(get_client_billing_account)],
    db: Annotated[Session, Depends(get_db)],
):
    rows = OrderDAO.list_for_account(
        db, account.id, status=status_filter, limit=limit, offset=offset
    )
    return [_serialize_order(row) for row in rows]


@router.get("/orders/{order_id}")
def get_order(
    order_id: int,
    account: Annotated[BillingAccount, Depends(get_client_billing_account)],
    db: Annotated[Session, Depends(get_db)],
):
    order = db.execute(
        select(Order).options(joinedload(Order.items)).where(Order.id == order_id)
    ).unique().scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    require_account_owns(order.billing_account_id, account)
    return _serialize_order(order)


# --- Invoices ---


@router.get("/invoices")
def list_invoices(
    status_filter: Optional[InvoiceStatus] = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    *,
    account: Annotated[BillingAccount, Depends(get_client_billing_account)],
    db: Annotated[Session, Depends(get_db)],
):
    stmt = (
        select(Invoice)
        .where(Invoice.billing_account_id == account.id)
        .order_by(Invoice.created_at.desc(), Invoice.id.desc())
    )
    if status_filter is not None:
        stmt = stmt.where(Invoice.status == status_filter)
    rows = list(db.execute(stmt.offset(offset).limit(limit)).scalars())
    return [_serialize_invoice(db, row) for row in rows]


@router.get("/invoices/{invoice_id}")
def get_invoice(
    invoice_id: int,
    account: Annotated[BillingAccount, Depends(get_client_billing_account)],
    db: Annotated[Session, Depends(get_db)],
):
    invoice = db.get(Invoice, invoice_id)
    if invoice is None or invoice.billing_account_id != account.id:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return _serialize_invoice(db, invoice)


@router.get("/invoices/{invoice_id}/pdf")
def get_invoice_pdf(
    invoice_id: int,
    account: Annotated[BillingAccount, Depends(get_client_billing_account)],
    db: Annotated[Session, Depends(get_db)],
):
    invoice = db.get(Invoice, invoice_id)
    if invoice is None or invoice.billing_account_id != account.id:
        raise HTTPException(status_code=404, detail="Invoice not found")
    try:
        pdf_bytes = InvoicePdfService.render_pdf_bytes(db, invoice)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return StreamingResponse(
        iter([pdf_bytes]),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="invoice-{invoice.invoice_number}.pdf"'
        },
    )


@router.post("/invoices/{invoice_id}/pay")
def pay_invoice(
    invoice_id: int,
    auth: Annotated[dict, Depends(require_client_session)],
    account: Annotated[BillingAccount, Depends(get_client_billing_account)],
    db: Annotated[Session, Depends(get_db)],
):
    _block_impersonation_money_actions(auth)
    invoice = db.get(Invoice, invoice_id)
    if invoice is None or invoice.billing_account_id != account.id:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.status == InvoiceStatus.PAID:
        return {"status": "paid", "invoice_id": invoice.id}
    if invoice.status == InvoiceStatus.VOID:
        raise HTTPException(status_code=400, detail="Invoice is void")

    if not settings.stripe_secret_key:
        return {
            "status": "manual",
            "message": (
                "Pay via admin mark-paid or configure Stripe for retail checkout"
            ),
            "invoice_id": invoice.id,
        }

    try:
        import importlib

        stripe = importlib.import_module("stripe")
        intent = stripe.PaymentIntent.create(
            amount=int(invoice.amount_cents),
            currency=(invoice.currency or "USD").lower(),
            automatic_payment_methods={"enabled": True},
            metadata={
                "rackflow_invoice_id": str(invoice.id),
                "rackflow_billing_account_id": str(account.id),
            },
            api_key=settings.stripe_secret_key,
        )
        return {
            "status": "requires_payment_method",
            "invoice_id": invoice.id,
            "client_secret": intent.client_secret,
            "payment_intent_id": intent.id,
        }
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Could not create payment intent: {exc}",
        ) from exc


# --- Payment methods (stub) ---


@router.get("/payment-methods")
def list_payment_methods():
    return {
        "items": [],
        "message": (
            "Saved payment methods are not vaulted for retail clients yet. "
            "Use Stripe Elements at pay-time via POST /invoices/{id}/pay."
        ),
    }


# --- Ticket departments ---


@router.get("/ticket-departments")
def list_ticket_departments(db: Annotated[Session, Depends(get_db)]):
    from app.dao.ticket_dao import TicketDepartmentDAO

    rows = TicketDepartmentDAO.list_enabled(db)
    return [
        {
            "id": row.id,
            "name": row.name,
            "code": row.code,
            "description": row.description,
            "sort_order": row.sort_order,
        }
        for row in rows
    ]


# --- Service lifecycle ---


@router.post("/services/{service_id}/cancel")
def cancel_service(
    service_id: int,
    body: ServiceCancelBody,
    account: Annotated[BillingAccount, Depends(get_client_billing_account)],
    db: Annotated[Session, Depends(get_db)],
):
    try:
        result = CommerceLifecycleService.request_cancellation_sync(
            db,
            service_id,
            account,
            when=body.when,  # type: ignore[arg-type]
            reason=body.reason,
        )
    except CommerceLifecycleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return result


@router.post("/services/{service_id}/upgrade", status_code=status.HTTP_201_CREATED)
def upgrade_service(
    service_id: int,
    body: ServiceUpgradeBody,
    auth: Annotated[dict, Depends(require_client_session)],
    account: Annotated[BillingAccount, Depends(get_client_billing_account)],
    db: Annotated[Session, Depends(get_db)],
):
    _block_impersonation_money_actions(auth)
    try:
        result = CommerceLifecycleService.create_upgrade_quote(
            db,
            service_id=service_id,
            account=account,
            frontend_product_id=body.frontend_product_id,
            price_plan_id=body.price_plan_id,
            cycle_interval=body.cycle_interval,
        )
    except CommerceLifecycleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return result


# --- Emails ---


@router.get("/emails")
def list_emails(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    *,
    auth: Annotated[dict, Depends(require_client_session)],
    db: Annotated[Session, Depends(get_db)],
):
    rows = EmailMessageService.list_for_user(
        db, int(auth["user_id"]), limit=limit, offset=offset
    )
    return [
        {
            "id": row.id,
            "subject": row.subject,
            "event": row.event,
            "status": row.status.value,
            "created_at": row.created_at,
            "sent_at": row.sent_at,
        }
        for row in rows
    ]


# --- Activity ---


@router.get("/activity")
def list_activity(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    *,
    auth: Annotated[dict, Depends(require_client_session)],
    db: Annotated[Session, Depends(get_db)],
):
    user_id = int(auth["user_id"])
    rows = list(
        db.execute(
            select(UserAuditEvent)
            .where(UserAuditEvent.subject_user_id == user_id)
            .order_by(UserAuditEvent.created_at.desc(), UserAuditEvent.id.desc())
            .offset(offset)
            .limit(limit)
        ).scalars()
    )
    return [
        {
            "id": row.id,
            "action": row.action,
            "resource_type": row.resource_type,
            "resource_id": row.resource_id,
            "metadata": row.event_metadata or {},
            "created_at": row.created_at,
        }
        for row in rows
    ]


# --- Billing profile ---


@router.get("/profile")
def get_profile(
    account: Annotated[BillingAccount, Depends(get_client_billing_account)],
    db: Annotated[Session, Depends(get_db)],
):
    profile = BillingAccountService.get_profile(db, account.id)
    if profile is None:
        return {"billing_account_id": account.id}
    return {
        "billing_account_id": account.id,
        "legal_name": profile.legal_name,
        "company": profile.company,
        "address_line1": profile.address_line1,
        "address_line2": profile.address_line2,
        "city": profile.city,
        "region": profile.region,
        "postal_code": profile.postal_code,
        "country": profile.country,
        "phone": profile.phone,
        "tax_id": profile.tax_id,
        "invoice_email": profile.invoice_email,
    }


@router.put("/profile")
def update_profile(
    body: ProfileUpdate,
    account: Annotated[BillingAccount, Depends(get_client_billing_account)],
    db: Annotated[Session, Depends(get_db)],
):
    data = body.model_dump(exclude_unset=True)
    if "country" in data and data["country"]:
        data["country"] = data["country"].upper()[:2]
    profile = BillingAccountService.upsert_profile(db, account.id, **data)
    db.commit()
    return {
        "billing_account_id": account.id,
        "legal_name": profile.legal_name,
        "company": profile.company,
        "address_line1": profile.address_line1,
        "address_line2": profile.address_line2,
        "city": profile.city,
        "region": profile.region,
        "postal_code": profile.postal_code,
        "country": profile.country,
        "phone": profile.phone,
        "tax_id": profile.tax_id,
        "invoice_email": profile.invoice_email,
    }


# --- Discord ---


@router.get("/discord/authorize-url")
def discord_authorize_url(
    auth: Annotated[dict, Depends(require_client_session)],
):
    from app.core.redis import redis_client

    state = secrets.token_urlsafe(32)
    redis_client.setex(f"commerce:discord:state:{state}", 600, str(auth["user_id"]))
    try:
        url = DiscordLinkService.build_authorize_url(state)
    except DiscordLinkError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"authorize_url": url, "state": state}


@router.post("/discord/callback")
def discord_callback(
    body: DiscordCallbackBody,
    auth: Annotated[dict, Depends(require_client_session)],
    db: Annotated[Session, Depends(get_db)],
):
    _block_impersonation_money_actions(auth)
    from app.core.redis import redis_client

    key = f"commerce:discord:state:{body.state}"
    stored = redis_client.get(key)
    if not stored or str(stored) != str(auth["user_id"]):
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")
    redis_client.delete(key)
    try:
        identity = DiscordLinkService.link_user(
            db, user_id=int(auth["user_id"]), code=body.code
        )
    except DiscordLinkError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return {
        "provider": identity.provider.value,
        "username": identity.username,
        "linked_at": identity.linked_at,
    }


@router.delete("/discord", status_code=status.HTTP_204_NO_CONTENT)
def discord_unlink(
    auth: Annotated[dict, Depends(require_client_session)],
    db: Annotated[Session, Depends(get_db)],
):
    _block_impersonation_money_actions(auth)
    DiscordLinkService.unlink_user(db, user_id=int(auth["user_id"]))
    db.commit()


# --- Support tickets ---


@router.get("/tickets")
def list_tickets(
    status_filter: Optional[TicketStatus] = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    *,
    account: Annotated[BillingAccount, Depends(get_client_billing_account)],
    db: Annotated[Session, Depends(get_db)],
):
    rows = TicketService.list_for_account(
        db, account.id, status=status_filter, limit=limit, offset=offset
    )
    return [_serialize_ticket(row) for row in rows]


@router.post("/tickets", status_code=status.HTTP_201_CREATED)
def create_ticket(
    body: TicketCreate,
    auth: Annotated[dict, Depends(require_client_session)],
    account: Annotated[BillingAccount, Depends(get_client_billing_account)],
    db: Annotated[Session, Depends(get_db)],
):
    try:
        ticket = TicketService.create_ticket(
            db,
            account=account,
            user_id=int(auth["user_id"]),
            department_id=body.department_id,
            subject=body.subject,
            body_text=body.body_text,
            priority=body.priority,
            service_id=body.service_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return _serialize_ticket(ticket)


@router.get("/tickets/{ticket_id}")
def get_ticket(
    ticket_id: int,
    account: Annotated[BillingAccount, Depends(get_client_billing_account)],
    db: Annotated[Session, Depends(get_db)],
):
    from app.dao.ticket_dao import TicketDAO, TicketMessageDAO

    ticket = TicketDAO.get(db, ticket_id)
    if ticket is None or ticket.billing_account_id != account.id:
        raise HTTPException(status_code=404, detail="Ticket not found")
    messages = [
        msg
        for msg in TicketMessageDAO.list_for_ticket(db, ticket_id)
        if not msg.is_staff_note
    ]
    ticket.messages = messages
    return _serialize_ticket(ticket, include_messages=True)


@router.post("/tickets/{ticket_id}/messages", status_code=status.HTTP_201_CREATED)
def add_ticket_message(
    ticket_id: int,
    body: TicketMessageCreate,
    auth: Annotated[dict, Depends(require_client_session)],
    account: Annotated[BillingAccount, Depends(get_client_billing_account)],
    db: Annotated[Session, Depends(get_db)],
):
    from app.dao.ticket_dao import TicketDAO

    ticket = TicketDAO.get(db, ticket_id)
    if ticket is None or ticket.billing_account_id != account.id:
        raise HTTPException(status_code=404, detail="Ticket not found")
    try:
        message = TicketService.add_message(
            db,
            ticket_id=ticket_id,
            author_user_id=int(auth["user_id"]),
            body_text=body.body_text,
            is_staff_note=False,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return {
        "id": message.id,
        "body_text": message.body_text,
        "created_at": message.created_at,
    }


# --- TOTP 2FA stubs ---


@router.post("/2fa/setup")
def totp_setup(
    auth: Annotated[dict, Depends(require_client_session)],
    db: Annotated[Session, Depends(get_db)],
):
    import pyotp

    from app.core.service_instance_crypto import encrypt_api_key
    from app.models.commerce_auth_extra import UserTotpSecret
    from app.models.user import User

    user = db.get(User, int(auth["user_id"]))
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    secret = pyotp.random_base32()
    encrypted = encrypt_api_key(secret) or secret
    row = db.get(UserTotpSecret, user.id)
    if row is None:
        row = UserTotpSecret(
            user_id=user.id,
            secret_encrypted=encrypted,
            enabled=False,
            recovery_codes_hash=[],
        )
        db.add(row)
    else:
        row.secret_encrypted = encrypted
        row.enabled = False
        row.confirmed_at = None
    db.commit()
    uri = pyotp.totp.TOTP(secret).provisioning_uri(
        name=user.email,
        issuer_name=settings.company_name or "Rackflow",
    )
    return {"secret": secret, "otpauth_uri": uri}


@router.post("/2fa/confirm")
def totp_confirm(
    body: TotpConfirmBody,
    auth: Annotated[dict, Depends(require_client_session)],
    db: Annotated[Session, Depends(get_db)],
):
    import pyotp

    from app.core.service_instance_crypto import decrypt_api_key
    from app.models.commerce_auth_extra import UserTotpSecret

    row = db.get(UserTotpSecret, int(auth["user_id"]))
    if row is None:
        raise HTTPException(status_code=400, detail="TOTP is not set up")
    secret = decrypt_api_key(row.secret_encrypted) or row.secret_encrypted
    if not pyotp.TOTP(secret).verify(body.code, valid_window=1):
        raise HTTPException(status_code=400, detail="Invalid TOTP code")
    from datetime import datetime, timezone

    row.enabled = True
    row.confirmed_at = datetime.now(timezone.utc)
    db.commit()
    return {"enabled": True}


@router.post("/2fa/disable")
def totp_disable(
    body: TotpDisableBody,
    auth: Annotated[dict, Depends(require_client_session)],
    db: Annotated[Session, Depends(get_db)],
):
    import pyotp

    from app.core.service_instance_crypto import decrypt_api_key
    from app.models.commerce_auth_extra import UserTotpSecret

    row = db.get(UserTotpSecret, int(auth["user_id"]))
    if row is None or not row.enabled:
        raise HTTPException(status_code=400, detail="TOTP is not enabled")
    secret = decrypt_api_key(row.secret_encrypted) or row.secret_encrypted
    if not pyotp.TOTP(secret).verify(body.code, valid_window=1):
        raise HTTPException(status_code=400, detail="Invalid TOTP code")
    db.delete(row)
    db.commit()
    return {"enabled": False}
