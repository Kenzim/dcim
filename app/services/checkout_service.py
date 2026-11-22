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
from app.models.commerce_coupon_tax import Coupon, CouponAppliesTo, TaxRate
from app.models.commerce_invoice_ext import InvoiceLineKind
from app.models.commerce_order import (
    Order,
    OrderItemFulfillStatus,
    OrderStatus,
)
from app.models.product_catalog import Product
from app.models.reseller import Invoice, InvoicePurpose
from app.models.service import ProvisioningSource, Service, ServiceType
from app.models.storefront import (
    FrontendProduct,
    PricePlan,
    PricePlanCycle,
    ProductOption,
    ProductOptionValue,
)
from app.services.billing import email_events
from app.services.billing.coupons import (
    FIRST_INVOICE_ONLY_NOTE,
    CouponRuleError,
    check_stored_rules,
    first_invoice_only,
    record_coupon_usage,
    release_coupon_usage,
)
from app.services.billing.cycles import CycleError, resolve_cycle
from app.services.billing.order_refunds import apply_order_refund_policy
from app.services.commerce_recurring_service import CommerceRecurringService
from app.plugin_host import events as plugin_events
from app.services.email_message_service import EmailEvent, EmailMessageService
from app.services.invoice_service import InvoiceService, NewLine, engine_mode
from app.services.provisioning import (
    ALLOWED_PROVISION_KEYS,
    ProvisionRequest,
    ProvisioningActor,
    ProvisioningError,
    ProvisioningService,
)

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
    kind: str = "product"


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
    coupon_first_invoice_only: bool = False
    # Tax engine fields (docs/billing-phase2-design.md 6.2); empty unless tax engine v2 decides.
    tax_lines: list[dict] = field(default_factory=list)
    tax_summary: list[dict] = field(default_factory=list)
    legal_notes: list[str] = field(default_factory=list)
    reverse_charge: bool = False
    # The engine's result: in v2 the one the invoice uses, in shadow the one compared.
    tax_result: Optional[Any] = field(default=None, repr=False, compare=False)
    tax_evidence: dict = field(default_factory=dict, repr=False, compare=False)


# QuoteResult fields that are not part of the quote API response.
QUOTE_INTERNAL_FIELDS = frozenset({"tax_result", "tax_evidence"})


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
        try:
            resolved = resolve_cycle(plan, cycle_interval)
        except CycleError as exc:
            raise CheckoutError(str(exc)) from exc
        return resolved.setup_cents, resolved.recurring_cents

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
        account: Optional[BillingAccount] = None,
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
        try:
            check_stored_rules(
                db, coupon, billing_account_id=account.id if account else None
            )
        except CouponRuleError as exc:
            raise CheckoutError(str(exc)) from exc
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
    def _tax_rate(db: Session, account: BillingAccount) -> Optional[TaxRate]:
        """The ``tax_rates`` row for the account's profile, or None.

        The candidates are today's: the country's enabled rows, limited to the
        national rows and the profile's region when the profile has one. Of
        several, a profile with a region takes its regional row and one
        without takes a national row; ties go to the lowest id. A region of
        ``''`` is a literal region, as in the query this replaces.
        """
        profile = BillingProfileDAO.get_by_account(db, account.id)
        if profile is None or not profile.country:
            return None
        stmt = select(TaxRate).where(
            TaxRate.country == profile.country.upper(),
            TaxRate.enabled.is_(True),
        )
        if profile.region:
            stmt = stmt.where(
                (TaxRate.region.is_(None)) | (TaxRate.region == profile.region)
            ).order_by(TaxRate.region.is_(None), TaxRate.id)
        else:
            stmt = stmt.order_by(TaxRate.region.is_(None).desc(), TaxRate.id)
        return db.execute(stmt.limit(1)).scalars().first()

    @staticmethod
    def _tax_cents(
        db: Session,
        account: BillingAccount,
        taxable_cents: int,
    ) -> int:
        if taxable_cents <= 0 or account.tax_exempt:
            return 0
        rate = CheckoutService._tax_rate(db, account)
        if rate is None:
            return 0
        return (taxable_cents * int(rate.rate_bps)) // 10_000

    @staticmethod
    def _ensure_tax_checkout_allowed(db: Session, account: BillingAccount) -> None:
        """Design 7.4's "block checkout" tax ID policy; owned by the packs and tax ID worker.

        A no-op until ``app.services.tax.validators.ensure_checkout_allowed``
        exists; then this is the one line that calls it.
        """
        return None

    @staticmethod
    def _tax_mode(db: Session) -> str:
        """The checkout's tax mode: ``v2`` needs the invoice engine in ``v2`` too."""
        from app.services.tax.mode import tax_engine_mode

        mode = tax_engine_mode(db)
        if mode == "v2" and engine_mode(db) != "v2":
            return "shadow"
        return mode

    @staticmethod
    def _priced_lines(lines: list[QuoteLine]) -> list[tuple[QuoteLine, InvoiceLineKind]]:
        """The quote lines a v2 invoice writes, in order, with their line kinds."""
        kinds = {
            "setup": InvoiceLineKind.SETUP,
            "product": InvoiceLineKind.PRODUCT,
            "option": InvoiceLineKind.OPTION,
            "discount": InvoiceLineKind.DISCOUNT,
        }
        priced = []
        for line in lines:
            if line.kind == "tax" or line.total_cents == 0:
                continue
            kind = kinds.get(line.kind, InvoiceLineKind.OTHER)
            if line.total_cents < 0:
                kind = InvoiceLineKind.DISCOUNT
            priced.append((line, kind))
        return priced

    @staticmethod
    def _product_tax_class_id(product: FrontendProduct) -> Optional[int]:
        catalog = getattr(product, "product", None)
        return getattr(catalog, "tax_class_id", None) if catalog is not None else None

    @staticmethod
    def _tax_result(
        db: Session,
        account: BillingAccount,
        lines: list[QuoteLine],
        *,
        tax_class_id: Optional[int],
        evidence: dict,
        prices_include_tax: bool,
        prepare: bool,
    ):
        """The engine's tax for the quote lines, keyed as the invoice will key them.

        One function for the quote and the checkout invoice, so they agree
        (design 4.5). ``prepare`` may call a provider (never under a lock);
        otherwise only the built-in rules are used.
        """
        from app.services.tax.context import TaxContextBuilder, TaxLineInput, class_code_of
        from app.services.tax.engine import TaxEngine

        code = class_code_of(db, tax_class_id)
        inputs = [
            TaxLineInput(
                key=f"line:{index}",
                net_cents=int(line.total_cents),
                tax_class_code=code,
                kind=kind.value,
            )
            for index, (line, kind) in enumerate(CheckoutService._priced_lines(lines))
        ]
        profile = BillingProfileDAO.get_by_account(db, account.id)
        context = TaxContextBuilder.for_checkout(
            db, account, profile, inputs, datetime.now(timezone.utc), evidence,
            prices_include_tax=prices_include_tax,
        )
        return TaxEngine.prepare(db, context) if prepare else TaxEngine.calculate(db, context)

    @staticmethod
    def _tax_evidence(db: Session, account: BillingAccount, client_ip: Optional[str], ip_country: Optional[str]) -> dict:
        """Location evidence recorded on the order (design 7.6)."""
        profile = BillingProfileDAO.get_by_account(db, account.id)
        country = (getattr(profile, "country", None) or "").strip().upper() or None
        return {
            "billing_country": country,
            "ip_address": client_ip,
            "ip_country": (ip_country or "").strip().upper() or None,
        }

    @staticmethod
    def quote(
        db: Session,
        account: BillingAccount,
        frontend_product_id: int,
        price_plan_id: int,
        cycle_interval: Optional[str],
        options: Optional[dict[str, Any]] = None,
        coupon_code: Optional[str] = None,
        *,
        evidence: Optional[dict] = None,
    ) -> QuoteResult:
        """Price an order. ``evidence`` is the checkout's location evidence (design 7.6).

        Tax engine ``legacy`` and ``shadow``: ``_tax_cents`` decides (in
        ``shadow`` the engine's result rides along for ``t1_checkout``); ``v2``:
        the engine decides and the quote carries its parts (design 4.7).
        """
        product = CheckoutService._load_product_bundle(db, frontend_product_id)
        plan = next((p for p in product.price_plans if p.id == price_plan_id), None)
        if plan is None or not plan.enabled:
            raise CheckoutError("Price plan is unavailable")
        if plan.currency.upper() != account.currency.upper():
            raise CheckoutError("Price plan currency does not match account currency")

        _, option_delta = CheckoutService._normalize_options(
            product, options
        )
        setup, period = CheckoutService._resolve_cycle_price(plan, cycle_interval)
        # Option prices are per billing period; the setup fee is charged once.
        recurring = period + option_delta
        subtotal = setup + recurring
        coupon = CheckoutService._find_coupon(
            db, coupon_code, frontend_product=product, account=account
        )
        discount = CheckoutService._coupon_discount(coupon, subtotal)
        taxable = max(0, subtotal - discount)

        title = f"{product.name} — {plan.name}"
        lines: list[QuoteLine] = []
        if setup:
            lines.append(
                QuoteLine(
                    description=f"{title} setup fee",
                    quantity=1,
                    unit_cents=setup,
                    total_cents=setup,
                    kind="setup",
                )
            )
        period_label = f" ({cycle_interval})" if cycle_interval else ""
        lines.append(
            QuoteLine(
                description=f"{title}{period_label}",
                quantity=1,
                unit_cents=period,
                total_cents=period,
                recurring_cents=recurring,
                kind="product",
            )
        )
        if option_delta:
            lines.append(
                QuoteLine(
                    description="Configuration options",
                    quantity=1,
                    unit_cents=option_delta,
                    total_cents=option_delta,
                    kind="option",
                )
            )
        first_only = bool(discount and coupon is not None and first_invoice_only(coupon))
        if discount:
            label = f"Coupon {coupon.code if coupon else ''}".strip()
            lines.append(
                QuoteLine(
                    description=f"{label} ({FIRST_INVOICE_ONLY_NOTE})" if first_only else label,
                    quantity=1,
                    unit_cents=-discount,
                    total_cents=-discount,
                    kind="discount",
                )
            )
        tax_mode = CheckoutService._tax_mode(db)
        evidence = dict(evidence or {})
        tax_result = None
        tax_fields: dict[str, Any] = {}
        prices_include_tax = False
        if tax_mode == "v2":
            from app.services.settings_store import get_setting

            prices_include_tax = bool(get_setting(db, "billing_tax_prices_include_tax"))
            tax_result = CheckoutService._tax_result(
                db, account, lines, tax_class_id=CheckoutService._product_tax_class_id(product),
                evidence=evidence, prices_include_tax=prices_include_tax, prepare=True,
            )
            tax = tax_result.tax_cents
            tax_fields = CheckoutService._quote_tax_fields(tax_result)
        else:
            tax = CheckoutService._tax_cents(db, account, taxable) if tax_mode == "legacy" else None
            if tax is None:
                tax, tax_result = CheckoutService._shadow_tax(
                    db, account, taxable, lines, product, evidence
                )
        total = taxable + (0 if prices_include_tax else tax)
        if tax:
            lines.append(
                QuoteLine(
                    description="Tax" if not prices_include_tax else "Tax included",
                    quantity=1,
                    unit_cents=tax,
                    total_cents=tax,
                    kind="tax",
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
            coupon_first_invoice_only=first_only,
            tax_result=tax_result,
            tax_evidence=evidence,
            **tax_fields,
        )

    @staticmethod
    def _shadow_tax(
        db: Session,
        account: BillingAccount,
        taxable: int,
        lines: list[QuoteLine],
        product: FrontendProduct,
        evidence: dict,
    ):
        """``(_tax_cents, the engine's result beside it)`` for tax engine ``shadow``.

        The engine never fails a checkout in shadow: an error is logged and
        the result is None. When ``_tax_cents`` raises, ``t1_checkout`` records
        that as its own outcome before the error propagates.
        """
        engine_result = None
        try:
            engine_result = CheckoutService._tax_result(
                db, account, lines, tax_class_id=CheckoutService._product_tax_class_id(product),
                evidence=evidence, prices_include_tax=False, prepare=False,
            )
        except Exception:  # noqa: BLE001 - a shadow calculation never fails a checkout
            logger.exception("tax shadow: engine failed for billing account %s", account.id)
        try:
            tax = CheckoutService._tax_cents(db, account, taxable)
        except Exception as exc:
            from app.services.tax.shadow import record_legacy_raised

            record_legacy_raised("billing_account", int(account.id), type(exc).__name__, engine_result)
            raise
        return tax, engine_result

    @staticmethod
    def _quote_tax_fields(result) -> dict[str, Any]:
        tax_lines = [
            {
                "line": key,
                "name": part.name,
                "rate_percent": str(part.rate_percent),
                "jurisdiction": part.jurisdiction,
                "base_cents": int(part.base_cents),
                "tax_cents": int(part.tax_cents),
                "reverse_charge": bool(part.reverse_charge),
            }
            for key, line in result.lines.items()
            for part in line.parts
        ]
        return {
            "tax_lines": tax_lines,
            "tax_summary": result.summary_json(),
            "legal_notes": list(result.legal_note_keys),
            "reverse_charge": any(item["reverse_charge"] for item in tax_lines),
        }

    @staticmethod
    def _create_commerce_invoice(
        db: Session,
        *,
        account: BillingAccount,
        order: Order,
        quote: QuoteResult,
        order_item_id: int,
        tax_class_id: Optional[int] = None,
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
        legacy_lines = CheckoutService._legacy_invoice_lines(quote, order_item_id)
        v2_lines = CheckoutService._v2_invoice_lines(quote, order_item_id, tax_class_id=tax_class_id)
        v2 = engine_mode(db) == "v2"
        tax_v2 = v2 and CheckoutService._tax_mode(db) == "v2"
        from app.services.tax.invoice_tax import TAX_RULES_CHANGED_MESSAGE, TaxRuleChanged

        try:
            invoice = InvoiceService.create(
                db,
                reseller_id=reseller_id,
                billing_account_id=account.id,
                order_id=order.id,
                purpose=InvoicePurpose.ORDER_CHARGE,
                amount_cents=int(quote.total_cents),
                currency=quote.currency,
                description=f"Order #{order.order_number}",
                due_at=datetime.now(timezone.utc),
                lines=v2_lines if v2 else legacy_lines,
                shadow_lines=v2_lines,
                prices_include_tax=tax_v2 and CheckoutService._prices_include_tax(db),
                tax_result=quote.tax_result if tax_v2 else None,
            )
        except TaxRuleChanged as exc:
            raise CheckoutError(TAX_RULES_CHANGED_MESSAGE) from exc
        if not tax_v2 and quote.tax_result is not None:
            from app.services.tax.shadow import compare_checkout

            compare_checkout(
                int(quote.tax_cents), quote.tax_result, subject_type="invoice", subject_id=int(invoice.id),
                context="checkout",
            )
        order.invoice_id = invoice.id
        db.flush()
        return invoice

    @staticmethod
    def _prices_include_tax(db: Session) -> bool:
        from app.services.settings_store import get_setting

        return bool(get_setting(db, "billing_tax_prices_include_tax"))

    @staticmethod
    def _legacy_invoice_lines(quote: QuoteResult, order_item_id: int) -> list[NewLine]:
        """Today's lines: tax as its own line, no line kinds."""
        return [
            NewLine(
                description=line.description[:512],
                quantity=line.quantity,
                unit_cents=line.unit_cents,
                tax_cents=line.total_cents if line.kind == "tax" else 0,
                order_item_id=(
                    order_item_id if line.kind in {"setup", "product", "option"} else None
                ),
            )
            for line in quote.lines
            if line.total_cents != 0
        ]

    @staticmethod
    def _v2_invoice_lines(
        quote: QuoteResult, order_item_id: int, *, tax_class_id: Optional[int] = None
    ) -> list[NewLine]:
        """Design 1.3.1: every line has a kind and the tax sits on the taxed lines.

        The quote's tax is spread over the charged lines in proportion to their
        totals by the largest remainder rule; a negative option total is
        written as a discount line, since only discounts may be negative. In
        tax engine ``v2`` the engine replaces that spread with its own rows.
        """
        from app.services.billing.rounding import tax_split_largest_remainder

        priced = CheckoutService._priced_lines(quote.lines)
        charged = [
            index
            for index, (_line, kind) in enumerate(priced)
            if kind != InvoiceLineKind.DISCOUNT
        ]
        shares = tax_split_largest_remainder(
            int(quote.tax_cents), [int(priced[index][0].total_cents) for index in charged]
        )
        tax_of = dict(zip(charged, shares))
        return [
            NewLine(
                description=line.description[:512],
                quantity=line.quantity,
                unit_cents=line.unit_cents,
                kind=kind,
                tax_cents=tax_of.get(index, 0),
                taxable=kind != InvoiceLineKind.DISCOUNT,
                tax_class_id=tax_class_id if kind != InvoiceLineKind.DISCOUNT else None,
                order_item_id=(
                    order_item_id if line.kind in {"setup", "product", "option"} else None
                ),
            )
            for index, (line, kind) in enumerate(priced)
        ]

    @staticmethod
    def _enqueue_checkout_emails(
        db: Session,
        *,
        account: BillingAccount,
        order: Order,
        quote: QuoteResult,
        user_id: int,
        invoice: Optional[Invoice],
    ) -> None:
        from app.models.user import User

        user = db.get(User, user_id)
        recipient = user.email if user else None
        if not recipient:
            return
        EmailMessageService.enqueue(
            db,
            to_address=recipient,
            event=EmailEvent.ORDER_CONFIRMATION,
            idempotency_key=f"order:{order.id}:confirmation",
            variables={
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
        if invoice is None:
            return
        EmailMessageService.enqueue(
            db,
            to_address=recipient,
            event=EmailEvent.INVOICE_CREATED,
            idempotency_key=f"invoice:{invoice.id}:created",
            variables={
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
        ip_country: Optional[str] = None,
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
        tax_mode = CheckoutService._tax_mode(db)
        if tax_mode == "v2":
            CheckoutService._ensure_tax_checkout_allowed(db, account)
        evidence = (
            CheckoutService._tax_evidence(db, account, client_ip, ip_country) if tax_mode != "legacy" else {}
        )
        quote = CheckoutService.quote(
            db,
            account,
            frontend_product_id,
            price_plan_id,
            cycle_interval,
            options,
            coupon_code,
            evidence=evidence,
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
        if tax_mode != "legacy":
            order.tax_evidence = evidence
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

        invoice = None
        if quote.total_cents > 0:
            invoice = CheckoutService._create_commerce_invoice(
                db,
                account=account,
                order=order,
                quote=quote,
                order_item_id=item.id,
                tax_class_id=CheckoutService._product_tax_class_id(product),
            )

        db.flush()
        try:
            CheckoutService._enqueue_checkout_emails(
                db,
                account=account,
                order=order,
                quote=quote,
                user_id=user_id,
                invoice=invoice,
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
    def _provision_keys_from_item_config(config: Optional[dict[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for value in (config or {}).values():
            if not isinstance(value, dict):
                continue
            key = value.get("provision_key")
            raw = value.get("provision_value")
            if key in ALLOWED_PROVISION_KEYS and raw not in (None, ""):
                out[str(key)] = raw
        return out

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
        catalog_type = None
        if catalog is not None and catalog.family is not None:
            catalog_type = catalog.family.service_type
        service_type = CheckoutService._map_service_type(catalog_type or product.service_type)
        keys = CheckoutService._provision_keys_from_item_config(item.config)

        def _int(name: str):
            raw = keys.get(name)
            if raw in (None, ""):
                return None
            return int(raw)

        req = ProvisionRequest(
            name=item.name_snapshot or f"order-{order.order_number}-{item.id}",
            service_type=service_type,
            owner_user_id=order.user_id,
            product_code=product_code,
            description=f"Order #{order.order_number}",
            provisioning_source=ProvisioningSource.BILLING,
            auto_provision=True,
            service_config=dict(item.config or {}),
            extra_snapshot={
                "frontend_product_id": product.id,
                "price_plan_id": item.price_plan_id,
                "cycle_interval": item.cycle_interval,
            },
            permission_set_id=product.permission_set_id,
            server_group_id=_int("server_group_id"),
            server_id=_int("server_id"),
            template_id=keys.get("template_id"),
            vm_template_id=_int("vm_template_id"),
            proxmox_cluster_id=_int("proxmox_cluster_id"),
            proxmox_node_name=keys.get("proxmox_node_name"),
            proxmox_vmid=_int("proxmox_vmid"),
            ip_count=_int("ip_count"),
            subnet_id=_int("subnet_id"),
            subnet_group_id=_int("subnet_group_id"),
            allocation_strategy=keys.get("allocation_strategy"),
            size_gb=_int("size_gb"),
            location_id=_int("location_id"),
        )
        actor = ProvisioningActor(
            kind="commerce",
            actor_id=int(order.user_id or 0),
            name="checkout",
            source="commerce",
        )
        try:
            return ProvisioningService.create(db, req, actor)
        except ProvisioningError as exc:
            raise CheckoutError(exc.message) from exc

    @staticmethod
    def _fulfill_order_item(db: Session, locked: Order, item: Any) -> Optional[str]:
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
            email_events.send_service_provisioned(db, service)
            return None
        except Exception as exc:
            item.fulfill_status = OrderItemFulfillStatus.FAILED
            item.error_message = str(exc)[:2000]
            logger.exception("Provision failed for order item %s", item.id)
            return f"Item {item.id}: {exc}"

    @staticmethod
    def mark_paid_and_fulfill(db: Session, order: Order) -> Order:
        locked = OrderDAO.get_for_update(db, order.id)
        if locked is None:
            raise CheckoutError("Order not found")
        if locked.status not in {
            OrderStatus.PENDING_PAYMENT,
            OrderStatus.PENDING_ACCEPTANCE,
            OrderStatus.PAID_PENDING_FULFILLMENT,
        }:
            raise CheckoutError(f"Order cannot be fulfilled from status {locked.status.value}")
        if locked.invoice_id is None and locked.coupon_code:
            # Orders without an invoice never settle, so their coupon use counts here.
            record_coupon_usage(db, order=locked)

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
            failure = CheckoutService._fulfill_order_item(db, locked, item)
            if failure:
                errors.append(failure)
            db.flush()

        if errors:
            locked.status = OrderStatus.PROVISION_ERROR
            locked.admin_notes = (
                (locked.admin_notes or "")
                + "\nProvision failures:\n"
                + "\n".join(errors)
            ).strip()
            OrderStatusHistoryDAO.append(
                db,
                order_id=locked.id,
                from_status=OrderStatus.FULFILLING,
                to_status=OrderStatus.PROVISION_ERROR,
                note="Provisioning failed",
            )
            apply_order_refund_policy(db, locked, reason="provisioning_failed")
            if locked.invoice_id is None and locked.coupon_code:
                # No invoice means no money was taken, so the use counted above is given back.
                release_coupon_usage(db, order=locked, reason="provisioning_failed")
            logger.warning(
                "Order %s provision errors; refund policy applied",
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
                plugin_events.emit(
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
            except Exception:
                logger.warning(
                    "Failed to enqueue order.paid webhook for order %s",
                    locked.id,
                    exc_info=True,
                )
        db.flush()
        return locked
