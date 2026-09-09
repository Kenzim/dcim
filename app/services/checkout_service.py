"""Checkout quoting, order placement, and post-payment fulfillment."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.dao.commerce_dao import BillingProfileDAO, SystemSettingDAO
from app.dao.order_dao import OrderDAO, OrderItemDAO, OrderStatusHistoryDAO
from app.dao.storefront_dao import FrontendProductDAO
from app.models.commerce_account import BillingAccount, BillingAccountType
from app.models.commerce_coupon_tax import Coupon, CouponAppliesTo, CouponRedemption, TaxRate
from app.models.commerce_invoice_ext import InvoiceLine
from app.models.commerce_order import (
    Order,
    OrderItemFulfillStatus,
    OrderStatus,
)
from app.models.product_catalog import Product
from app.models.reseller import Invoice, InvoicePurpose, InvoiceStatus
from app.models.service import ProvisioningSource, Service, ServiceStatus, ServiceType
from app.models.storefront import (
    FrontendProduct,
    PricePlan,
    PricePlanCycle,
    PricePlanInterval,
    PricePlanPricingModel,
    ProductOption,
    ProductOptionValue,
)
from app.services.commerce_recurring_service import CommerceRecurringService
from app.services.commerce_webhook_service import CommerceWebhookService
from app.services.email_message_service import EmailEvent, EmailMessageService
from app.services.invoice_service import InvoiceService

logger = logging.getLogger(__name__)


class CheckoutError(ValueError):
    pass


@dataclass(frozen=True)
class QuoteLine:
    description: str
    quantity: int
    unit_cents: int
    total_cents: int
    recurring_cents: int = 0


@dataclass(frozen=True)
class QuoteResult:
    currency: str
    setup_cents: int
    recurring_cents: int
    option_delta_cents: int
    subtotal_cents: int
    discount_cents: int
    tax_cents: int
    total_cents: int
    coupon_code: Optional[str] = None
    lines: list[QuoteLine] = field(default_factory=list)


class CheckoutService:
    @staticmethod
    def _load_product_bundle(
        db: Session, frontend_product_id: int
    ) -> FrontendProduct:
        product = FrontendProductDAO.get_product_with_plans(db, frontend_product_id)
        if product is None or not product.enabled:
            raise CheckoutError("Product is unavailable")
        return product

    @staticmethod
    def _resolve_cycle_price(
        plan: PricePlan,
        cycle_interval: Optional[str],
    ) -> tuple[int, int]:
        setup = int(plan.setup_cents or 0)
        if plan.pricing_model == PricePlanPricingModel.FREE:
            return 0, 0
        if plan.pricing_model == PricePlanPricingModel.ONE_TIME:
            cycle = next((c for c in plan.cycles if c.enabled), None)
            return setup, int(cycle.price_cents if cycle else 0)
        if not cycle_interval:
            raise CheckoutError("Billing cycle is required for recurring plans")
        try:
            interval = PricePlanInterval(cycle_interval)
        except ValueError as exc:
            raise CheckoutError("Invalid billing cycle") from exc
        cycle = next(
            (c for c in plan.cycles if c.enabled and c.interval == interval),
            None,
        )
        if cycle is None:
            raise CheckoutError("Selected billing cycle is unavailable")
        return setup, int(cycle.price_cents)

    @staticmethod
    def _normalize_options(
        product: FrontendProduct,
        options: Optional[dict[str, Any]],
    ) -> tuple[dict[str, Any], int]:
        selected = dict(options or {})
        delta = 0
        normalized: dict[str, Any] = {}
        for option in product.options:
            raw = selected.get(option.code)
            if raw is None or raw == "":
                if option.required:
                    raise CheckoutError(f"Required option missing: {option.name}")
                continue
            if option.option_type.value == "select":
                value = CheckoutService._resolve_option_value(option, raw)
                delta += int(value.price_delta_cents or 0)
                normalized[option.code] = {
                    "value_code": value.code,
                    "name": value.name,
                    "provision_key": value.provision_key,
                    "provision_value": value.provision_value,
                }
            else:
                normalized[option.code] = {"text": str(raw)}
        return normalized, delta

    @staticmethod
    def _resolve_option_value(
        option: ProductOption, raw: Any
    ) -> ProductOptionValue:
        code = str(raw)
        value = next(
            (
                row
                for row in option.values
                if row.enabled and row.code == code
            ),
            None,
        )
        if value is None:
            raise CheckoutError(f"Invalid option value for {option.name}")
        return value

    @staticmethod
    def _find_coupon(
        db: Session,
        coupon_code: Optional[str],
        *,
        frontend_product: FrontendProduct,
    ) -> Optional[Coupon]:
        if not coupon_code:
            return None
        coupon = db.execute(
            select(Coupon).where(
                func.lower(Coupon.code) == coupon_code.strip().lower(),
                Coupon.enabled.is_(True),
            )
        ).scalar_one_or_none()
        if coupon is None:
            raise CheckoutError("Coupon code is invalid")
        now = datetime.now(timezone.utc)
        if coupon.starts_at and coupon.starts_at > now:
            raise CheckoutError("Coupon is not active yet")
        if coupon.expires_at and coupon.expires_at < now:
            raise CheckoutError("Coupon has expired")
        if coupon.max_uses is not None and coupon.used_count >= coupon.max_uses:
            raise CheckoutError("Coupon usage limit reached")
        if coupon.applies_to == CouponAppliesTo.PRODUCT:
            if coupon.frontend_product_id != frontend_product.id:
                raise CheckoutError("Coupon does not apply to this product")
        elif coupon.applies_to == CouponAppliesTo.CATEGORY:
            if coupon.category_id != frontend_product.category_id:
                raise CheckoutError("Coupon does not apply to this product category")
        return coupon

    @staticmethod
    def _coupon_discount(
        coupon: Optional[Coupon], subtotal_cents: int
    ) -> int:
        if coupon is None or subtotal_cents <= 0:
            return 0
        if coupon.amount_off_cents is not None:
            return min(subtotal_cents, int(coupon.amount_off_cents))
        if coupon.percent_off is not None:
            return min(subtotal_cents, (subtotal_cents * int(coupon.percent_off)) // 100)
        return 0

    @staticmethod
    def _tax_cents(
        db: Session,
        account: BillingAccount,
        taxable_cents: int,
    ) -> int:
        if taxable_cents <= 0 or account.tax_exempt:
            return 0
        profile = BillingProfileDAO.get_by_account(db, account.id)
        if profile is None or not profile.country:
            return 0
        stmt = select(TaxRate).where(
            TaxRate.country == profile.country.upper(),
            TaxRate.enabled.is_(True),
        )
        if profile.region:
            stmt = stmt.where(
                (TaxRate.region.is_(None)) | (TaxRate.region == profile.region)
            )
        rate = db.execute(
            stmt.order_by(TaxRate.region.is_(None), TaxRate.region.desc())
        ).scalar_one_or_none()
        if rate is None:
            return 0
        return (taxable_cents * int(rate.rate_bps)) // 10_000

    @staticmethod
    def quote(
        db: Session,
        account: BillingAccount,
        frontend_product_id: int,
        price_plan_id: int,
        cycle_interval: Optional[str],
        options: Optional[dict[str, Any]] = None,
        coupon_code: Optional[str] = None,
    ) -> QuoteResult:
        product = CheckoutService._load_product_bundle(db, frontend_product_id)
        plan = next((p for p in product.price_plans if p.id == price_plan_id), None)
        if plan is None or not plan.enabled:
            raise CheckoutError("Price plan is unavailable")
        if plan.currency.upper() != account.currency.upper():
            raise CheckoutError("Price plan currency does not match account currency")

        _, option_delta = CheckoutService._normalize_options(
            product, options
        )
        setup, recurring = CheckoutService._resolve_cycle_price(plan, cycle_interval)
        setup += option_delta
        recurring += option_delta
        subtotal = setup + recurring
        coupon = CheckoutService._find_coupon(
            db, coupon_code, frontend_product=product
        )
        discount = CheckoutService._coupon_discount(coupon, subtotal)
        taxable = max(0, subtotal - discount)
        tax = CheckoutService._tax_cents(db, account, taxable)
        total = taxable + tax

        lines = [
            QuoteLine(
                description=f"{product.name} — {plan.name}",
                quantity=1,
                unit_cents=setup,
                total_cents=setup,
                recurring_cents=recurring,
            )
        ]
        if option_delta:
            lines.append(
                QuoteLine(
                    description="Configuration options",
                    quantity=1,
                    unit_cents=option_delta,
                    total_cents=option_delta,
                )
            )
        if discount:
            lines.append(
                QuoteLine(
                    description=f"Coupon {coupon.code if coupon else ''}".strip(),
                    quantity=1,
                    unit_cents=-discount,
                    total_cents=-discount,
                )
            )
        if tax:
            lines.append(
                QuoteLine(
                    description="Tax",
                    quantity=1,
                    unit_cents=tax,
                    total_cents=tax,
                )
            )

        return QuoteResult(
            currency=account.currency.upper(),
            setup_cents=setup,
            recurring_cents=recurring,
            option_delta_cents=option_delta,
            subtotal_cents=subtotal,
            discount_cents=discount,
            tax_cents=tax,
            total_cents=total,
            coupon_code=coupon.code if coupon else None,
            lines=lines,
        )

    @staticmethod
    def _create_commerce_invoice(
        db: Session,
        *,
        account: BillingAccount,
        order: Order,
        quote: QuoteResult,
        order_item_id: int,
    ) -> Invoice:
        if quote.total_cents <= 0:
            raise CheckoutError("Cannot create invoice for zero total")
        reseller_id = (
            int(account.reseller_id)
            if (
                account.account_type == BillingAccountType.RESELLER
                and account.reseller_id is not None
            )
            else None
        )
        invoice = Invoice(
            invoice_number=InvoiceService.allocate_number(db),
            reseller_id=reseller_id,
            billing_account_id=account.id,
            order_id=order.id,
            purpose=InvoicePurpose.ORDER_CHARGE,
            status=InvoiceStatus.OPEN,
            amount_cents=int(quote.total_cents),
            currency=quote.currency,
            description=f"Order #{order.order_number}",
            due_at=datetime.now(timezone.utc),
        )
        db.add(invoice)
        db.flush()
        sort = 0
        for line in quote.lines:
            if line.total_cents == 0:
                continue
            db.add(
                InvoiceLine(
                    invoice_id=invoice.id,
                    description=line.description[:512],
                    quantity=line.quantity,
                    unit_cents=line.unit_cents,
                    total_cents=line.total_cents,
                    tax_cents=quote.tax_cents if line.description == "Tax" else 0,
                    sort_order=sort,
                    order_item_id=order_item_id if sort == 0 else None,
                )
            )
            sort += 1
        order.invoice_id = invoice.id
        db.flush()
        return invoice

    @staticmethod
    def place_order(
        db: Session,
        *,
        account: BillingAccount,
        user_id: int,
        frontend_product_id: int,
        price_plan_id: int,
        cycle_interval: Optional[str],
        options: Optional[dict[str, Any]] = None,
        coupon_code: Optional[str] = None,
        checkout_nonce: str,
        terms_version: Optional[str] = None,
        client_ip: Optional[str] = None,
    ) -> Order:
        if not checkout_nonce.strip():
            raise CheckoutError("Checkout nonce is required")
        existing = db.execute(
            select(Order).where(Order.checkout_nonce == checkout_nonce.strip())
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        product = CheckoutService._load_product_bundle(db, frontend_product_id)
        plan = next((p for p in product.price_plans if p.id == price_plan_id), None)
        if plan is None:
            raise CheckoutError("Price plan is unavailable")
        normalized_options, _ = CheckoutService._normalize_options(product, options)
        quote = CheckoutService.quote(
            db,
            account,
            frontend_product_id,
            price_plan_id,
            cycle_interval,
            options,
            coupon_code,
        )

        status = (
            OrderStatus.PENDING_ACCEPTANCE
            if quote.total_cents == 0
            else OrderStatus.PENDING_PAYMENT
        )
        order = OrderDAO.create(
            db,
            billing_account_id=account.id,
            user_id=user_id,
            status=status,
            currency=quote.currency,
            setup_cents=quote.setup_cents,
            recurring_cents=quote.recurring_cents,
            tax_cents=quote.tax_cents,
            discount_cents=quote.discount_cents,
            total_cents=quote.total_cents,
            coupon_code=quote.coupon_code,
            terms_version=terms_version or settings.commerce_terms_version,
            terms_accepted_at=datetime.now(timezone.utc),
            checkout_nonce=checkout_nonce.strip(),
            client_ip=client_ip,
        )
        item = OrderItemDAO.create(
            db,
            order_id=order.id,
            frontend_product_id=frontend_product_id,
            price_plan_id=price_plan_id,
            cycle_interval=cycle_interval,
            quantity=1,
            setup_cents=quote.setup_cents,
            recurring_cents=quote.recurring_cents,
            currency=quote.currency,
            config=normalized_options,
            name_snapshot=product.name,
        )
        OrderStatusHistoryDAO.append(
            db,
            order_id=order.id,
            from_status=None,
            to_status=status,
            actor_user_id=user_id,
            note="Order placed",
        )

        if quote.coupon_code:
            coupon = CheckoutService._find_coupon(
                db, quote.coupon_code, frontend_product=product
            )
            if coupon is not None:
                coupon.used_count = int(coupon.used_count or 0) + 1
                db.add(
                    CouponRedemption(
                        coupon_id=coupon.id,
                        billing_account_id=account.id,
                        order_id=order.id,
                    )
                )

        invoice = None
        if quote.total_cents > 0:
            invoice = CheckoutService._create_commerce_invoice(
                db,
                account=account,
                order=order,
                quote=quote,
                order_item_id=item.id,
            )

        db.flush()
        try:
            from app.models.user import User

            user = db.get(User, user_id)
            recipient = user.email if user else None
            if recipient:
                EmailMessageService.enqueue(
                    db,
                    to_address=recipient,
                    event=EmailEvent.ORDER_CONFIRMATION,
                    idempotency_key=f"order:{order.id}:confirmation",
                    data={
                        "order_number": order.order_number,
                        "order_id": order.id,
                        "amount_cents": quote.total_cents,
                        "currency": quote.currency,
                    },
                    billing_account_id=account.id,
                    user_id=user_id,
                    related_type="order",
                    related_id=str(order.id),
                )
                if invoice is not None:
                    EmailMessageService.enqueue(
                        db,
                        to_address=recipient,
                        event=EmailEvent.INVOICE_CREATED,
                        idempotency_key=f"invoice:{invoice.id}:created",
                        data={
                            "invoice_number": invoice.invoice_number,
                            "invoice_id": invoice.id,
                            "amount_cents": quote.total_cents,
                            "currency": quote.currency,
                        },
                        billing_account_id=account.id,
                        user_id=user_id,
                        related_type="invoice",
                        related_id=str(invoice.id),
                    )
        except Exception:
            logger.warning("Failed to enqueue checkout emails for order %s", order.id, exc_info=True)
        return order

    @staticmethod
    def _map_service_type(raw: str) -> ServiceType:
        normalized = (raw or "").strip().lower()
        for candidate in ServiceType:
            if candidate.value == normalized:
                return candidate
        return ServiceType.BARE_METAL

    @staticmethod
    def _provision_order_item(
        db: Session,
        *,
        order: Order,
        item: Any,
    ) -> Service:
        product = CheckoutService._load_product_bundle(db, item.frontend_product_id)
        catalog: Optional[Product] = product.product
        product_code = catalog.code if catalog else None
        service_type = CheckoutService._map_service_type(product.service_type)

        service = Service(
            name=item.name_snapshot,
            owner_user_id=order.user_id,
            service_type=service_type,
            status=ServiceStatus.PENDING,
            description=f"Order #{order.order_number}",
            config=dict(item.config or {}),
            product_code=product_code,
            product_snapshot={
                "frontend_product_id": product.id,
                "price_plan_id": item.price_plan_id,
                "cycle_interval": item.cycle_interval,
            },
            permission_set_id=product.permission_set_id,
            provisioning_source=ProvisioningSource.BILLING,
        )
        db.add(service)
        db.flush()
        return service

    @staticmethod
    def mark_paid_and_fulfill(db: Session, order: Order) -> Order:
        locked = OrderDAO.get_for_update(db, order.id)
        if locked is None:
            raise CheckoutError("Order not found")
        if locked.status not in {
            OrderStatus.PENDING_PAYMENT,
            OrderStatus.PENDING_ACCEPTANCE,
        }:
            raise CheckoutError(f"Order cannot be fulfilled from status {locked.status.value}")

        prior = locked.status
        locked.status = OrderStatus.FULFILLING
        OrderStatusHistoryDAO.append(
            db,
            order_id=locked.id,
            from_status=prior,
            to_status=OrderStatus.FULFILLING,
            note="Payment received; fulfillment started",
        )
        db.flush()

        errors: list[str] = []
        for item in OrderItemDAO.list_for_order(db, locked.id):
            item.fulfill_status = OrderItemFulfillStatus.FULFILLING
            db.flush()
            try:
                service = CheckoutService._provision_order_item(
                    db, order=locked, item=item
                )
                item.service_id = service.id
                product_bundle = CheckoutService._load_product_bundle(
                    db, item.frontend_product_id
                )
                plan = next(
                    (p for p in product_bundle.price_plans if p.id == item.price_plan_id),
                    None,
                )
                account = db.get(BillingAccount, locked.billing_account_id)
                if plan is not None and account is not None:
                    catalog_product = (
                        product_bundle.product.id if product_bundle.product else None
                    )
                    CommerceRecurringService.ensure_for_fulfilled_item(
                        db,
                        service=service,
                        item=item,
                        billing_account=account,
                        plan=plan,
                        product_id=catalog_product,
                    )
                item.fulfill_status = OrderItemFulfillStatus.FULFILLED
            except Exception as exc:
                item.fulfill_status = OrderItemFulfillStatus.FAILED
                item.error_message = str(exc)[:2000]
                errors.append(f"Item {item.id}: {exc}")
                logger.exception("Provision failed for order item %s", item.id)
            db.flush()

        if errors:
            locked.status = OrderStatus.PROVISION_ERROR
            locked.admin_notes = (
                (locked.admin_notes or "")
                + "\nProvision failures:\n"
                + "\n".join(errors)
                + "\nRefund path stub: manual review required."
            ).strip()
            OrderStatusHistoryDAO.append(
                db,
                order_id=locked.id,
                from_status=OrderStatus.FULFILLING,
                to_status=OrderStatus.PROVISION_ERROR,
                note="Provisioning failed",
            )
            logger.warning(
                "Order %s provision errors; refund stub logged",
                locked.order_number,
            )
        else:
            locked.status = OrderStatus.ACTIVE
            OrderStatusHistoryDAO.append(
                db,
                order_id=locked.id,
                from_status=OrderStatus.FULFILLING,
                to_status=OrderStatus.ACTIVE,
                note="Fulfillment complete",
            )
            try:
                CommerceWebhookService.enqueue(
                    db,
                    "order.paid",
                    {
                        "order_id": locked.id,
                        "order_number": locked.order_number,
                        "billing_account_id": locked.billing_account_id,
                        "total_cents": locked.total_cents,
                        "currency": locked.currency,
                    },
                )
                if locked.invoice_id is not None:
                    CommerceWebhookService.enqueue(
                        db,
                        "invoice.paid",
                        {
                            "invoice_id": locked.invoice_id,
                            "order_id": locked.id,
                            "billing_account_id": locked.billing_account_id,
                        },
                    )
            except Exception:
                logger.warning(
                    "Failed to enqueue order.paid webhook for order %s",
                    locked.id,
                    exc_info=True,
                )
        db.flush()
        return locked
