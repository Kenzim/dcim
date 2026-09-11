"""Admin commerce operations: orders, invoices, payments, email, audit, settings."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.core.auth import require_admin
from app.core.database import get_db
from app.dao.commerce_dao import BillingAccountDAO, SystemSettingDAO
from app.dao.order_dao import OrderDAO, OrderStatusHistoryDAO
from app.models.commerce_account import BillingAccountStatus
from app.models.commerce_audit import UserAuditEvent
from app.models.commerce_email import EmailMessage, EmailMessageStatus
from app.models.commerce_gateway_log import PaymentGatewayLog
from app.models.commerce_order import Order, OrderStatus
from app.models.commerce_webhook import WebhookEndpoint
from app.models.reseller import Invoice, InvoiceStatus, Payment, PaymentStatus
from app.models.user import User
from app.services.audit_service import AuditService
from app.services.checkout_service import CheckoutError, CheckoutService
from app.services.commerce_lifecycle_service import (
    CommerceLifecycleError,
    CommerceLifecycleService,
)
from app.services.commerce_webhook_service import CommerceWebhookService
from app.services.email_message_service import EmailMessageService
from app.services.gdpr_service import GdprService
from app.services.invoice_pdf_service import InvoicePdfService
from app.services.invoice_service import InvoiceService
from app.core.openapi_responses import COMMON_ERROR_RESPONSES

router = APIRouter(
    prefix="/admin/commerce",
    tags=["commerce-admin"],
    dependencies=[Depends(require_admin)],
    responses=COMMON_ERROR_RESPONSES,
)


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MarkPaidBody(RequestModel):
    reason: str = Field(min_length=1, max_length=2000)


class SettingUpdateBody(RequestModel):
    value: dict[str, Any] = Field(default_factory=dict)


class WebhookCreateBody(RequestModel):
    url: str = Field(min_length=8, max_length=2048)
    secret: str = Field(min_length=8, max_length=255)
    events: list[str] = Field(default_factory=list)
    enabled: bool = True


class WebhookUpdateBody(RequestModel):
    url: Optional[str] = Field(default=None, min_length=8, max_length=2048)
    secret: Optional[str] = Field(default=None, min_length=8, max_length=255)
    events: Optional[list[str]] = None
    enabled: Optional[bool] = None


class ForceCancelBody(RequestModel):
    when: str = Field(default="immediate", pattern="^(end_of_cycle|immediate)$")
    reason: Optional[str] = Field(default=None, max_length=2000)


def _client_ip(request: Request) -> Optional[str]:
    client_ip = request.client.host if request.client else None
    from app.core.config import settings

    if settings.trust_x_forwarded_for and "x-forwarded-for" in request.headers:
        client_ip = request.headers["x-forwarded-for"].split(",")[0].strip()
    return client_ip


def _serialize_order_item(item) -> dict:
    return {
        "id": item.id,
        "frontend_product_id": item.frontend_product_id,
        "price_plan_id": item.price_plan_id,
        "cycle_interval": item.cycle_interval,
        "quantity": item.quantity,
        "setup_cents": item.setup_cents,
        "recurring_cents": item.recurring_cents,
        "currency": item.currency,
        "config": item.config or {},
        "name_snapshot": item.name_snapshot,
        "fulfill_status": item.fulfill_status.value,
        "service_id": item.service_id,
        "error_message": item.error_message,
    }


def _serialize_order(order: Order, *, include_items: bool = True) -> dict:
    payload = {
        "id": order.id,
        "order_number": order.order_number,
        "billing_account_id": order.billing_account_id,
        "user_id": order.user_id,
        "status": order.status.value,
        "currency": order.currency,
        "setup_cents": order.setup_cents,
        "recurring_cents": order.recurring_cents,
        "tax_cents": order.tax_cents,
        "discount_cents": order.discount_cents,
        "total_cents": order.total_cents,
        "coupon_code": order.coupon_code,
        "terms_version": order.terms_version,
        "terms_accepted_at": order.terms_accepted_at,
        "checkout_nonce": order.checkout_nonce,
        "notes": order.notes,
        "admin_notes": order.admin_notes,
        "accepted_at": order.accepted_at,
        "accepted_by_user_id": order.accepted_by_user_id,
        "fraud_score": order.fraud_score,
        "client_ip": order.client_ip,
        "invoice_id": order.invoice_id,
        "created_at": order.created_at,
        "updated_at": order.updated_at,
    }
    if include_items and order.items is not None:
        payload["items"] = [_serialize_order_item(item) for item in order.items]
    return payload


def _serialize_invoice(db: Session, invoice: Invoice, *, payments: bool = False) -> dict:
    payload = InvoiceService.serialize(db, invoice, payments=payments)
    payload["billing_account_id"] = invoice.billing_account_id
    payload["order_id"] = invoice.order_id
    return payload


def _serialize_email(row: EmailMessage) -> dict:
    return {
        "id": row.id,
        "billing_account_id": row.billing_account_id,
        "user_id": row.user_id,
        "to_address": row.to_address,
        "subject": row.subject,
        "event": row.event,
        "template_key": row.template_key,
        "status": row.status.value,
        "error": row.error,
        "related_type": row.related_type,
        "related_id": row.related_id,
        "created_at": row.created_at,
        "sent_at": row.sent_at,
    }


def _serialize_gateway_log(row: PaymentGatewayLog) -> dict:
    return {
        "id": row.id,
        "gateway": row.gateway,
        "direction": row.direction.value,
        "operation": row.operation,
        "payment_id": row.payment_id,
        "invoice_id": row.invoice_id,
        "billing_account_id": row.billing_account_id,
        "http_status": row.http_status,
        "request_summary": row.request_summary,
        "response_summary": row.response_summary,
        "correlation_id": row.correlation_id,
        "created_at": row.created_at,
    }


def _serialize_audit(row: UserAuditEvent) -> dict:
    return {
        "id": row.id,
        "actor_user_id": row.actor_user_id,
        "subject_user_id": row.subject_user_id,
        "billing_account_id": row.billing_account_id,
        "action": row.action,
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "ip": row.ip,
        "user_agent": row.user_agent,
        "metadata": row.event_metadata or {},
        "impersonated_by": row.impersonated_by,
        "created_at": row.created_at,
    }


def _serialize_billing_account(row) -> dict:
    return {
        "id": row.id,
        "account_type": row.account_type.value,
        "user_id": row.user_id,
        "reseller_id": row.reseller_id,
        "status": row.status.value,
        "currency": row.currency,
        "credit_balance_cents": row.credit_balance_cents,
        "tax_exempt": row.tax_exempt,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "username": row.user.username if row.user else None,
        "email": row.user.email if row.user else None,
        "reseller_name": row.reseller.name if row.reseller else None,
    }


def _get_order_or_404(db: Session, order_id: int) -> Order:
    order = OrderDAO.get(db, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


def _get_invoice_or_404(db: Session, invoice_id: int) -> Invoice:
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice


# --- Orders ---


@router.get("/orders")
def list_orders(
    status_filter: Optional[OrderStatus] = Query(default=None, alias="status"),
    q: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    rows = OrderDAO.list_admin(
        db, status=status_filter, q=q, limit=limit, offset=offset
    )
    return [_serialize_order(row, include_items=False) for row in rows]


@router.get("/orders/{order_id}", responses={**COMMON_ERROR_RESPONSES})
def get_order(order_id: int, db: Session = Depends(get_db)):
    order = db.execute(
        select(Order).options(joinedload(Order.items)).where(Order.id == order_id)
    ).unique().scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return _serialize_order(order)


@router.post("/orders/{order_id}/accept", responses={**COMMON_ERROR_RESPONSES})
def accept_order(
    order_id: int,
    request: Request,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    order = _get_order_or_404(db, order_id)
    if order.status not in {
        OrderStatus.PENDING_ACCEPTANCE,
        OrderStatus.PENDING_PAYMENT,
        OrderStatus.PAID_PENDING_FULFILLMENT,
    }:
        raise HTTPException(
            status_code=400,
            detail=f"Order cannot be accepted from status {order.status.value}",
        )
    prior = order.status
    order.accepted_at = datetime.now(timezone.utc)
    order.accepted_by_user_id = auth.get("user_id")
    if prior == OrderStatus.PENDING_ACCEPTANCE:
        OrderStatusHistoryDAO.append(
            db,
            order_id=order.id,
            from_status=prior,
            to_status=OrderStatus.PAID_PENDING_FULFILLMENT,
            actor_user_id=auth.get("user_id"),
            note="Order accepted by admin",
        )
        order.status = OrderStatus.PAID_PENDING_FULFILLMENT
        db.flush()
    try:
        order = CheckoutService.mark_paid_and_fulfill(db, order)
    except CheckoutError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    AuditService.log(
        db,
        actor_user_id=auth.get("user_id"),
        billing_account_id=order.billing_account_id,
        action="order.accept",
        resource_type="order",
        resource_id=str(order.id),
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    db.commit()
    return _serialize_order(order)


@router.post("/orders/{order_id}/retry-fulfill", responses={**COMMON_ERROR_RESPONSES})
def retry_fulfill_order(
    order_id: int,
    request: Request,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    order = _get_order_or_404(db, order_id)
    if order.status not in {
        OrderStatus.PROVISION_ERROR,
        OrderStatus.PAID_PENDING_FULFILLMENT,
        OrderStatus.FULFILLING,
    }:
        raise HTTPException(
            status_code=400,
            detail=f"Order cannot be re-fulfilled from status {order.status.value}",
        )
    try:
        order = CheckoutService.mark_paid_and_fulfill(db, order)
    except CheckoutError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    AuditService.log(
        db,
        actor_user_id=auth.get("user_id"),
        billing_account_id=order.billing_account_id,
        action="order.retry_fulfill",
        resource_type="order",
        resource_id=str(order.id),
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    db.commit()
    return _serialize_order(order)


@router.post("/orders/{order_id}/cancel", responses={**COMMON_ERROR_RESPONSES})
def cancel_order(
    order_id: int,
    request: Request,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    order = _get_order_or_404(db, order_id)
    if order.status in {OrderStatus.ACTIVE, OrderStatus.CANCELLED}:
        raise HTTPException(
            status_code=400,
            detail=f"Order cannot be cancelled from status {order.status.value}",
        )
    prior = order.status
    order.status = OrderStatus.CANCELLED
    OrderStatusHistoryDAO.append(
        db,
        order_id=order.id,
        from_status=prior,
        to_status=OrderStatus.CANCELLED,
        actor_user_id=auth.get("user_id"),
        note="Cancelled by admin",
    )
    AuditService.log(
        db,
        actor_user_id=auth.get("user_id"),
        billing_account_id=order.billing_account_id,
        action="order.cancel",
        resource_type="order",
        resource_id=str(order.id),
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    db.commit()
    return _serialize_order(order)


# --- Invoices ---


@router.get("/invoices")
def list_invoices(
    status_filter: Optional[InvoiceStatus] = Query(default=None, alias="status"),
    billing_account_id: Optional[int] = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    stmt = (
        select(Invoice)
        .where(Invoice.billing_account_id.isnot(None))
        .order_by(Invoice.created_at.desc(), Invoice.id.desc())
    )
    if status_filter is not None:
        stmt = stmt.where(Invoice.status == status_filter)
    if billing_account_id is not None:
        stmt = stmt.where(Invoice.billing_account_id == billing_account_id)
    rows = list(db.execute(stmt.offset(offset).limit(limit)).scalars())
    return [_serialize_invoice(db, row) for row in rows]


@router.get("/invoices/{invoice_id}", responses={**COMMON_ERROR_RESPONSES})
def get_invoice(invoice_id: int, db: Session = Depends(get_db)):
    invoice = _get_invoice_or_404(db, invoice_id)
    return _serialize_invoice(db, invoice, payments=True)


@router.get("/invoices/{invoice_id}/pdf", responses={**COMMON_ERROR_RESPONSES})
def get_invoice_pdf(invoice_id: int, db: Session = Depends(get_db)):
    invoice = _get_invoice_or_404(db, invoice_id)
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


@router.post("/invoices/{invoice_id}/mark-paid", responses={**COMMON_ERROR_RESPONSES})
def mark_invoice_paid(
    invoice_id: int,
    body: MarkPaidBody,
    request: Request,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    invoice = _get_invoice_or_404(db, invoice_id)
    if invoice.status == InvoiceStatus.VOID:
        raise HTTPException(status_code=400, detail="Cannot mark a void invoice as paid")
    invoice.status = InvoiceStatus.PAID
    invoice.paid_at = datetime.now(timezone.utc)
    AuditService.log(
        db,
        actor_user_id=auth.get("user_id"),
        billing_account_id=invoice.billing_account_id,
        action="invoice.mark_paid_manual",
        resource_type="invoice",
        resource_id=str(invoice.id),
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
        metadata={"reason": body.reason},
    )
    try:
        CommerceWebhookService.enqueue(
            db,
            "invoice.paid",
            {
                "invoice_id": invoice.id,
                "invoice_number": invoice.invoice_number,
                "billing_account_id": invoice.billing_account_id,
                "order_id": invoice.order_id,
            },
        )
    except Exception:
        pass
    db.commit()
    return _serialize_invoice(db, invoice)


@router.post("/invoices/{invoice_id}/void", responses={**COMMON_ERROR_RESPONSES})
def void_invoice(
    invoice_id: int,
    request: Request,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    invoice = _get_invoice_or_404(db, invoice_id)
    if invoice.status == InvoiceStatus.PAID:
        raise HTTPException(status_code=400, detail="Cannot void a paid invoice")
    invoice.status = InvoiceStatus.VOID
    AuditService.log(
        db,
        actor_user_id=auth.get("user_id"),
        billing_account_id=invoice.billing_account_id,
        action="invoice.void",
        resource_type="invoice",
        resource_id=str(invoice.id),
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    db.commit()
    return _serialize_invoice(db, invoice)


# --- Transactions ---


@router.get("/transactions")
def list_transactions(
    gateway: Optional[str] = None,
    status_filter: Optional[PaymentStatus] = Query(default=None, alias="status"),
    billing_account_id: Optional[int] = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    stmt = (
        select(Payment)
        .join(Invoice, Payment.invoice_id == Invoice.id)
        .where(Invoice.billing_account_id.isnot(None))
        .order_by(Payment.created_at.desc(), Payment.id.desc())
    )
    if gateway:
        stmt = stmt.where(Payment.gateway == gateway.strip())
    if status_filter is not None:
        stmt = stmt.where(Payment.status == status_filter)
    if billing_account_id is not None:
        stmt = stmt.where(Invoice.billing_account_id == billing_account_id)
    rows = list(db.execute(stmt.offset(offset).limit(limit)).scalars())
    return [
        {
            **InvoiceService.serialize_payment(row),
            "invoice_id": row.invoice_id,
            "billing_account_id": row.invoice.billing_account_id if row.invoice else None,
        }
        for row in rows
    ]


# --- Gateway logs ---


@router.get("/gateway-logs")
def list_gateway_logs(
    gateway: Optional[str] = None,
    invoice_id: Optional[int] = None,
    billing_account_id: Optional[int] = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    stmt = select(PaymentGatewayLog).order_by(
        PaymentGatewayLog.created_at.desc(), PaymentGatewayLog.id.desc()
    )
    if gateway:
        stmt = stmt.where(PaymentGatewayLog.gateway == gateway.strip())
    if invoice_id is not None:
        stmt = stmt.where(PaymentGatewayLog.invoice_id == invoice_id)
    if billing_account_id is not None:
        stmt = stmt.where(PaymentGatewayLog.billing_account_id == billing_account_id)
    rows = list(db.execute(stmt.offset(offset).limit(limit)).scalars())
    return [_serialize_gateway_log(row) for row in rows]


# --- Email messages ---


@router.get("/email-messages")
def list_email_messages(
    status_filter: Optional[EmailMessageStatus] = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    rows = EmailMessageService.list_admin(
        db, status=status_filter, limit=limit, offset=offset
    )
    return [_serialize_email(row) for row in rows]


@router.post("/email-messages/{message_id}/retry", responses={**COMMON_ERROR_RESPONSES})
def retry_email_message(message_id: int, db: Session = Depends(get_db)):
    row = EmailMessageService.retry(db, message_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Email message not found")
    db.commit()
    return _serialize_email(row)


# --- Audit ---


@router.get("/audit-events")
def list_audit_events(
    action: Optional[str] = None,
    billing_account_id: Optional[int] = None,
    subject_user_id: Optional[int] = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    stmt = select(UserAuditEvent).order_by(
        UserAuditEvent.created_at.desc(), UserAuditEvent.id.desc()
    )
    if action:
        stmt = stmt.where(UserAuditEvent.action == action.strip()[:64])
    if billing_account_id is not None:
        stmt = stmt.where(UserAuditEvent.billing_account_id == billing_account_id)
    if subject_user_id is not None:
        stmt = stmt.where(UserAuditEvent.subject_user_id == subject_user_id)
    rows = list(db.execute(stmt.offset(offset).limit(limit)).scalars())
    return [_serialize_audit(row) for row in rows]


# --- Billing accounts ---


@router.get("/billing-accounts")
def list_billing_accounts(
    status_filter: Optional[BillingAccountStatus] = Query(default=None, alias="status"),
    q: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    rows = BillingAccountDAO.list_for_admin(
        db, status=status_filter, q=q, limit=limit, offset=offset
    )
    return [_serialize_billing_account(row) for row in rows]


@router.get("/billing-accounts/{account_id}", responses={**COMMON_ERROR_RESPONSES})
def get_billing_account(account_id: int, db: Session = Depends(get_db)):
    row = BillingAccountDAO.get(db, account_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Billing account not found")
    payload = _serialize_billing_account(row)
    if row.profile:
        payload["profile"] = {
            "legal_name": row.profile.legal_name,
            "company": row.profile.company,
            "address_line1": row.profile.address_line1,
            "address_line2": row.profile.address_line2,
            "city": row.profile.city,
            "region": row.profile.region,
            "postal_code": row.profile.postal_code,
            "country": row.profile.country,
            "phone": row.profile.phone,
            "tax_id": row.profile.tax_id,
            "invoice_email": row.profile.invoice_email,
        }
    return payload


# --- System settings ---


@router.get("/settings/{key}", responses={**COMMON_ERROR_RESPONSES})
def get_setting(key: str, db: Session = Depends(get_db)):
    value = SystemSettingDAO.get(db, key.strip())
    if value is None:
        raise HTTPException(status_code=404, detail="Setting not found")
    return {"key": key.strip(), "value": value}


@router.put("/settings/{key}")
def put_setting(key: str, body: SettingUpdateBody, db: Session = Depends(get_db)):
    row = SystemSettingDAO.set(db, key.strip(), body.value)
    db.commit()
    return {"key": row.key, "value": row.value, "updated_at": row.updated_at}


# --- Outbound webhooks ---


@router.get("/webhooks")
def list_webhooks(db: Session = Depends(get_db)):
    rows = list(db.execute(select(WebhookEndpoint).order_by(WebhookEndpoint.id)).scalars())
    return [CommerceWebhookService.serialize_endpoint(row) for row in rows]


@router.post("/webhooks", status_code=status.HTTP_201_CREATED)
def create_webhook(body: WebhookCreateBody, db: Session = Depends(get_db)):
    row = CommerceWebhookService.create_endpoint(
        db,
        url=body.url,
        secret=body.secret,
        events=body.events,
        enabled=body.enabled,
    )
    db.commit()
    return CommerceWebhookService.serialize_endpoint(row)


@router.put("/webhooks/{endpoint_id}", responses={**COMMON_ERROR_RESPONSES})
def update_webhook(
    endpoint_id: int,
    body: WebhookUpdateBody,
    db: Session = Depends(get_db),
):
    row = db.get(WebhookEndpoint, endpoint_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
    if body.url is not None:
        row.url = body.url.strip()
    if body.secret is not None:
        import hashlib

        row.secret_hash = hashlib.sha256(body.secret.encode("utf-8")).hexdigest()
    if body.events is not None:
        row.events = body.events
    if body.enabled is not None:
        row.enabled = body.enabled
    db.commit()
    return CommerceWebhookService.serialize_endpoint(row)


@router.delete(
    "/webhooks/{endpoint_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**COMMON_ERROR_RESPONSES},
)
def delete_webhook(endpoint_id: int, db: Session = Depends(get_db)):
    row = db.get(WebhookEndpoint, endpoint_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
    db.delete(row)
    db.commit()


# --- GDPR ---


@router.post("/users/{user_id}/export", responses={**COMMON_ERROR_RESPONSES})
def export_user(user_id: int, db: Session = Depends(get_db)):
    if db.get(User, user_id) is None:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        payload = GdprService.export_user_data(db, user_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return payload


@router.post("/users/{user_id}/anonymize", responses={**COMMON_ERROR_RESPONSES})
def anonymize_user(user_id: int, db: Session = Depends(get_db)):
    try:
        result = GdprService.anonymize_user(db, user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return result


# --- Service lifecycle (admin force) ---


@router.post("/services/{service_id}/cancel", responses={**COMMON_ERROR_RESPONSES})
def force_cancel_service(
    service_id: int,
    body: ForceCancelBody,
    db: Session = Depends(get_db),
):
    from app.dao.commerce_dao import BillingAccountDAO
    from app.models.service import Service

    service = db.get(Service, service_id)
    if service is None:
        raise HTTPException(status_code=404, detail="Service not found")
    account = BillingAccountDAO.get_by_user_id(db, service.owner_user_id or 0)
    if account is None:
        raise HTTPException(
            status_code=400,
            detail="Service owner has no billing account",
        )
    try:
        result = CommerceLifecycleService.request_cancellation_sync(
            db,
            service_id,
            account,
            when=body.when,  # type: ignore[arg-type]
            reason=body.reason or "Admin forced cancellation",
            force=True,
        )
    except CommerceLifecycleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return result
