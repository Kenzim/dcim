"""Transactional invoice issuance, the invoice state machine, balance and serialisation.

``InvoiceService`` is the only writer of ``invoices`` and ``invoice_lines``
(docs/billing-phase1-design.md sections 3 to 5). Every invoice is created as
``draft``, given its lines and then published (``draft`` to ``open``) in the
same transaction. ``transition()`` is the one place that assigns
``invoices.status``; which edges it accepts depends on the invoice engine mode
(``legacy``, ``shadow`` or ``v2``). A move to ``paid`` is only made by the
settlement function (``app/services/billing/settlement.py``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Sequence, Union

from sqlalchemy import and_, case, func, select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models.commerce_invoice_ext import InvoiceLine, InvoiceLineKind
from app.models.invoice_adjustment import AdjustmentKind, InvoiceAdjustment
from app.models.reseller import (
    Invoice,
    InvoiceDocumentType,
    InvoicePurpose,
    InvoiceSequence,
    InvoiceStatus,
    Payment,
    PaymentStatus,
)
from app.services import invoice_guard  # noqa: F401  (installs the frozen-column guard)

logger = logging.getLogger(__name__)

# Invoice statuses the frozen client and reseller APIs have always shown.
PUBLIC_INVOICE_STATUSES = frozenset(
    {"draft", "open", "paid", "void", "failed", "pending_action", "overdue"}
)
_PUBLIC_STATUS_OF = {
    "partially_paid": "open",
    "collections": "overdue",
    # Paid and then returned: nothing is owed and nothing should be retried.
    "refunded": "paid",
}


def public_invoice_status(status: Union[InvoiceStatus, str]) -> str:
    """The ``status`` shown on ``/api/client/commerce/*`` and ``/api/reseller/*``.

    Those APIs keep today's seven values; the real value goes in ``status_detail``.
    """
    value = getattr(status, "value", status)
    public = _PUBLIC_STATUS_OF.get(value, value)
    if public not in PUBLIC_INVOICE_STATUSES:
        raise ValueError(f"unknown invoice status {value!r}")
    return public


_PAID_PAYMENT_STATUSES = (
    PaymentStatus.SUCCEEDED,
    PaymentStatus.PARTIALLY_REFUNDED,
    PaymentStatus.REFUNDED,
)

S = InvoiceStatus

# Engine v2 (design 3.3), plus the edges the status rule of 3.2 reaches that 3.3
# does not list (a property test pins that the rule never leaves the table):
# open to refunded and pending_action to refunded (a refund's credit note after
# the invoice reopened), open to collections (a dispute under the reopen
# policy), pending_action to void (a credit note covering an invoice with a
# payment still pending), paid to pending_action and paid to void (after a
# reversed payment). Every edge not listed is refused.
V2_EDGES: dict[InvoiceStatus, frozenset[InvoiceStatus]] = {
    S.DRAFT: frozenset({S.OPEN}),
    S.OPEN: frozenset(
        {S.PENDING_ACTION, S.PAID, S.PARTIALLY_PAID, S.OVERDUE, S.VOID, S.REFUNDED, S.COLLECTIONS}
    ),
    S.PENDING_ACTION: frozenset(
        {S.OPEN, S.PARTIALLY_PAID, S.OVERDUE, S.COLLECTIONS, S.PAID, S.REFUNDED, S.VOID}
    ),
    S.PARTIALLY_PAID: frozenset(
        {S.PENDING_ACTION, S.PAID, S.OVERDUE, S.COLLECTIONS, S.OPEN, S.REFUNDED, S.VOID}
    ),
    S.OVERDUE: frozenset({S.PENDING_ACTION, S.PAID, S.COLLECTIONS, S.REFUNDED, S.VOID}),
    S.COLLECTIONS: frozenset({S.PENDING_ACTION, S.PAID, S.REFUNDED, S.VOID}),
    S.PAID: frozenset(
        {S.COLLECTIONS, S.PARTIALLY_PAID, S.OPEN, S.REFUNDED, S.PENDING_ACTION, S.VOID}
    ),
    S.VOID: frozenset(),
    S.REFUNDED: frozenset(),
    S.FAILED: frozenset(),
}

# Engines legacy and shadow (design 3.4): today's edges, plus the ones today's
# writers reach that 3.4 does not list (an overdue invoice that starts a card
# payment, a reseller deploy charge voided after a failed deploy, a reseller
# invoice paid after it was voided, a cancelled cycle whose invoice had failed).
LEGACY_EDGES: dict[InvoiceStatus, frozenset[InvoiceStatus]] = {
    S.DRAFT: frozenset({S.OPEN}),
    S.OPEN: frozenset({S.PAID, S.PENDING_ACTION, S.OVERDUE, S.VOID, S.FAILED}),
    S.PENDING_ACTION: frozenset({S.PAID, S.OPEN, S.OVERDUE, S.VOID, S.FAILED}),
    S.OVERDUE: frozenset({S.PAID, S.PENDING_ACTION, S.VOID, S.FAILED}),
    S.PAID: frozenset({S.OPEN, S.VOID, S.FAILED}),
    S.FAILED: frozenset({S.PAID, S.VOID}),
    S.VOID: frozenset({S.PAID}),
}
# Statuses only v2 writes, read as their legacy counterpart if the engine is
# moved back to shadow while such invoices exist.
_LEGACY_FROM = {
    S.PARTIALLY_PAID: S.OPEN,
    S.COLLECTIONS: S.OVERDUE,
    S.REFUNDED: S.PAID,
}

TERMINAL_V2 = frozenset({S.VOID, S.REFUNDED, S.FAILED})

# Statuses that mean "something is still owed" (design 4.2).
UNPAID_STATUSES = frozenset(
    {S.OPEN, S.PENDING_ACTION, S.PARTIALLY_PAID, S.OVERDUE, S.COLLECTIONS}
)

CAUSE_SETTLEMENT = "settlement"
CAUSE_PUBLISH = "publish"
CAUSE_DISPUTE = "dispute"

# Line kind written for the single line of each automatic invoice (design 1.3.1).
_PURPOSE_LINE_KIND = {
    InvoicePurpose.CREDIT_TOPUP: InvoiceLineKind.OTHER,
    InvoicePurpose.DEPLOY_CHARGE: InvoiceLineKind.SETUP,
    InvoicePurpose.CYCLE_CHARGE: InvoiceLineKind.PRODUCT,
    InvoicePurpose.UPGRADE: InvoiceLineKind.SETUP,
}


@dataclass(frozen=True)
class InvoiceBalance:
    """What an invoice is worth and what has been paid against it (design 4.1)."""

    total_cents: int  # invoices.amount_cents
    debit_cents: int  # debit notes on the invoice
    credit_cents: int  # credit notes on the invoice
    worth_cents: int  # total + debit - credit
    paid_gross_cents: int  # payments succeeded, partially refunded or refunded
    refunded_cents: int  # refunded_cents of those payments
    paid_net_cents: int  # paid_gross - refunded
    pending_cents: int  # pending payments, not deducted
    due_cents: int  # worth - paid_net; negative means overpaid
    remaining_cents: int  # max(0, due_cents)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass(frozen=True)
class NewLine:
    """One line to write on a new invoice."""

    description: str
    unit_cents: int
    quantity: int = 1
    kind: Optional[InvoiceLineKind] = None
    tax_cents: int = 0
    order_item_id: Optional[int] = None
    service_id: Optional[int] = None
    service_billing_id: Optional[int] = None
    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None
    taxable: Optional[bool] = None
    tax_class_id: Optional[int] = None
    tax_snapshot: Optional[dict] = None

    @property
    def total_cents(self) -> int:
        return int(self.quantity) * int(self.unit_cents)


@dataclass(frozen=True)
class InvoiceTotals:
    subtotal_cents: int
    discount_cents: int
    tax_cents: int
    amount_cents: int
    problems: tuple[str, ...] = field(default_factory=tuple)


class InvoiceError(ValueError):
    pass


class InvoiceTransitionError(InvoiceError):
    """An edge that is not in the transition table of the current engine mode."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _kind(value: Any) -> Optional[str]:
    return getattr(value, "value", value)


def compute_totals(lines: Iterable[Any], *, prices_include_tax: bool) -> InvoiceTotals:
    """The v2 totals of a set of lines and the line rules they break (design 1.3.1).

    ``lines`` are ``NewLine`` or ``InvoiceLine`` objects (``quantity``,
    ``unit_cents``, ``total_cents``, ``tax_cents``, ``kind``).
    """
    problems: list[str] = []
    subtotal = discount = tax = 0
    for index, line in enumerate(lines):
        quantity = int(line.quantity)
        unit = int(line.unit_cents)
        total = int(line.total_cents)
        line_tax = int(line.tax_cents or 0)
        kind = _kind(line.kind)
        label = f"line {index + 1}"
        if kind is None:
            problems.append(f"{label} has no kind")
        if quantity < 1:
            problems.append(f"{label} quantity must be at least 1")
        if total != quantity * unit:
            problems.append(f"{label} total is not quantity times unit price")
        if line_tax < 0:
            problems.append(f"{label} tax must not be negative")
        if kind == InvoiceLineKind.DISCOUNT.value:
            if unit > 0 or total > 0:
                problems.append(f"{label} is a discount with a positive amount")
            if line_tax != 0:
                problems.append(f"{label} is a discount with tax")
            discount -= total
        else:
            if unit < 0:
                problems.append(f"{label} has a negative unit price")
            subtotal += total
        tax += line_tax
    amount = subtotal - discount + (0 if prices_include_tax else tax)
    if amount < 0:
        problems.append("the invoice total is negative")
    return InvoiceTotals(subtotal, discount, tax, amount, tuple(problems))


_zero_amount_support: dict[str, bool] = {}


def zero_amounts_supported(db: Session) -> bool:
    """Whether ``invoices`` accepts ``amount_cents = 0`` (revision ``billing_p1_invoice_checks``).

    Decided once per database URL in this process by reading the constraint list;
    a failed read refuses zero totals for that call and is tried again next time.
    """
    bind = db.get_bind()
    key = str(bind.engine.url)
    cached = _zero_amount_support.get(key)
    if cached is not None:
        return cached
    try:
        if bind.dialect.name == "sqlite":
            sql = db.execute(
                text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'invoices'")
            ).scalar()
            supported = bool(sql) and "ck_invoices_positive_amount" not in sql
        else:
            names = set(
                db.execute(
                    text(
                        "SELECT CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS "
                        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'invoices' "
                        "AND CONSTRAINT_TYPE = 'CHECK'"
                    )
                ).scalars()
            )
            supported = "ck_invoices_positive_amount" not in names
    except Exception:  # noqa: BLE001 - unknown means the old rule stays, for this call only
        logger.exception("Could not read the invoices constraints; zero totals stay refused")
        return False
    _zero_amount_support[key] = supported
    return supported


_PROFILE_FIELDS = (
    "legal_name",
    "company",
    "address_line1",
    "address_line2",
    "city",
    "region",
    "postal_code",
    "country",
    "tax_id",
    "invoice_email",
)


def seller_snapshot(at: datetime) -> dict:
    """The seller block the PDF prints, as of ``at`` (design 5.5)."""
    from app.core.config import settings

    return {
        "company_name": settings.company_name,
        "company_address": settings.company_address,
        "company_tax_id": settings.company_tax_id,
        "captured_at": at.isoformat(),
    }


def buyer_snapshot(db: Session, invoice: Invoice, at: datetime) -> dict:
    """The buyer block from the account's billing profile, else the user's name and email."""
    from app.models.commerce_account import BillingAccount, BillingProfile
    from app.models.reseller import Reseller
    from app.models.user import User

    account = None
    if invoice.billing_account_id is not None:
        account = db.get(BillingAccount, invoice.billing_account_id)
    elif invoice.reseller_id is not None:
        account = db.execute(
            select(BillingAccount).where(BillingAccount.reseller_id == invoice.reseller_id)
        ).scalar_one_or_none()
    snapshot: dict = {name: None for name in _PROFILE_FIELDS}
    profile = None
    if account is not None:
        profile = db.execute(
            select(BillingProfile).where(BillingProfile.billing_account_id == account.id)
        ).scalar_one_or_none()
    if profile is not None:
        for name in _PROFILE_FIELDS:
            snapshot[name] = getattr(profile, name)
    else:
        user_id = account.user_id if account is not None else None
        if user_id is None and invoice.reseller_id is not None:
            reseller = db.get(Reseller, invoice.reseller_id)
            user_id = reseller.user_id if reseller is not None else None
        user = db.get(User, user_id) if user_id is not None else None
        if user is not None:
            snapshot["legal_name"] = user.username
            snapshot["invoice_email"] = user.email
    snapshot.update(
        {
            "account_type": _kind(account.account_type) if account is not None else (
                "reseller" if invoice.reseller_id is not None else "client"
            ),
            "billing_account_id": account.id if account is not None else invoice.billing_account_id,
            "reseller_id": invoice.reseller_id,
            "captured_at": at.isoformat(),
        }
    )
    return snapshot


def engine_mode(db: Session) -> str:
    from app.services.billing_shadow import billing_engine_mode

    return billing_engine_mode(db, "invoice")


def _shadow_record(check_key: str, invoice: Invoice, legacy: Any, v2: Any, context: str) -> None:
    from app.services.billing_shadow import BillingShadowService

    BillingShadowService.record("invoice", check_key, "invoice", int(invoice.id), legacy, v2, context)


class InvoiceService:
    _SEQUENCE_NAME = "invoice"
    _FIRST_NUMBER = 100001
    UNPAID_STATUSES = UNPAID_STATUSES

    @staticmethod
    def _require_positive_cents(amount_cents: int) -> None:
        if type(amount_cents) is not int or amount_cents <= 0:
            raise InvoiceError(
                "amount_cents must be a positive integer number of cents"
            )

    @staticmethod
    def _initial_next_value(db: Session) -> int:
        largest = db.scalar(select(func.max(Invoice.invoice_number)))
        return max(InvoiceService._FIRST_NUMBER, int(largest or 0) + 1)

    @staticmethod
    def allocate_number(db: Session) -> int:
        """Allocate one number atomically on SQLite and with a row lock on MySQL."""
        dialect = db.get_bind().dialect.name
        if dialect == "sqlite":
            db.execute(
                sqlite_insert(InvoiceSequence)
                .values(
                    name=InvoiceService._SEQUENCE_NAME,
                    next_value=InvoiceService._initial_next_value(db),
                )
                .on_conflict_do_nothing(index_elements=["name"])
            )
            allocated = db.scalar(
                update(InvoiceSequence)
                .where(InvoiceSequence.name == InvoiceService._SEQUENCE_NAME)
                .values(next_value=InvoiceSequence.next_value + 1)
                .returning(InvoiceSequence.next_value - 1)
            )
            if allocated is None:
                raise InvoiceError("Could not allocate an invoice number")
            return int(allocated)

        sequence = db.execute(
            select(InvoiceSequence)
            .where(InvoiceSequence.name == InvoiceService._SEQUENCE_NAME)
            .with_for_update()
        ).scalar_one_or_none()
        if sequence is None:
            sequence = InvoiceSequence(
                name=InvoiceService._SEQUENCE_NAME,
                next_value=InvoiceService._initial_next_value(db),
            )
            db.add(sequence)
            db.flush()
        allocated = int(sequence.next_value)
        sequence.next_value = allocated + 1
        db.flush()
        return allocated

    # --- Creation and publishing ---

    @staticmethod
    def _proforma_enabled(db: Session) -> bool:
        from app.services.settings_store import get_setting

        return bool(get_setting(db, "billing_proforma_enabled"))

    @staticmethod
    def create(
        db: Session,
        *,
        reseller_id: Optional[int] = None,
        purpose: InvoicePurpose,
        amount_cents: int,
        description: Optional[str] = None,
        service_id: Optional[int] = None,
        due_at: Optional[datetime] = None,
        currency: str = "USD",
        billing_account_id: Optional[int] = None,
        order_id: Optional[int] = None,
        lines: Optional[Sequence[NewLine]] = None,
        shadow_lines: Optional[Sequence[NewLine]] = None,
        prices_include_tax: bool = False,
        customer_notes: Optional[str] = None,
        locale: Optional[str] = None,
        series_id: Optional[int] = None,
        publish: bool = True,
    ) -> Invoice:
        """Create an invoice as ``draft`` with its lines and, by default, publish it.

        ``amount_cents`` is the legacy total; with no ``lines`` one line of that
        amount is written. In engine ``v2`` the total is computed from the lines
        and must equal ``amount_cents``. ``shadow_lines`` are the lines the v2
        engine would write when they differ from ``lines`` (checkout's tax
        line); in ``shadow`` their total is compared with the legacy total.
        """
        mode = engine_mode(db)
        if mode == "v2" and type(amount_cents) is int and amount_cents == 0:
            if not zero_amounts_supported(db):
                raise InvoiceError(
                    "amount_cents must be positive until the invoices check revision is applied"
                )
        else:
            InvoiceService._require_positive_cents(amount_cents)
        currency = currency.strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise InvoiceError("currency must be a three-letter code")
        if reseller_id is None and billing_account_id is None:
            raise InvoiceError("reseller_id or billing_account_id is required")
        if mode == "v2":
            InvoiceService._lock_owner(db, reseller_id=reseller_id, billing_account_id=billing_account_id)
        if lines is None:
            lines = [
                NewLine(
                    description=(description or purpose.value.replace("_", " "))[:512],
                    unit_cents=int(amount_cents),
                    kind=_PURPOSE_LINE_KIND.get(purpose, InvoiceLineKind.OTHER),
                    service_id=service_id,
                )
            ]
        invoice = Invoice(
            invoice_number=InvoiceService.allocate_number(db),
            reseller_id=reseller_id,
            billing_account_id=billing_account_id,
            order_id=order_id,
            service_id=service_id,
            purpose=purpose,
            status=InvoiceStatus.DRAFT,
            amount_cents=amount_cents,
            currency=currency,
            description=description,
            due_at=due_at,
            model_version=2 if mode == "v2" else None,
            document_type=(
                InvoiceDocumentType.PROFORMA
                if InvoiceService._proforma_enabled(db)
                else InvoiceDocumentType.INVOICE
            ),
            prices_include_tax=prices_include_tax if mode == "v2" else None,
            # ck_invoices_v2_totals: a v2 row carries totals from its insert; publish computes them.
            subtotal_cents=0 if mode == "v2" else None,
            discount_cents=0 if mode == "v2" else None,
            tax_cents=0 if mode == "v2" else None,
            customer_notes=customer_notes,
            locale=locale,
            series_id=series_id,
        )
        db.add(invoice)
        db.flush()
        for sort, line in enumerate(lines):
            db.add(
                InvoiceLine(
                    invoice_id=invoice.id,
                    description=line.description[:512],
                    quantity=int(line.quantity),
                    unit_cents=int(line.unit_cents),
                    total_cents=line.total_cents,
                    tax_cents=int(line.tax_cents or 0),
                    sort_order=sort,
                    order_item_id=line.order_item_id,
                    kind=line.kind,
                    service_id=line.service_id,
                    service_billing_id=line.service_billing_id,
                    period_start=line.period_start,
                    period_end=line.period_end,
                    taxable=line.taxable,
                    tax_class_id=line.tax_class_id,
                    tax_snapshot=line.tax_snapshot,
                )
            )
        db.flush()
        db.expire(invoice, ["invoice_lines"])
        if mode == "shadow":
            totals = compute_totals(
                shadow_lines if shadow_lines is not None else lines,
                prices_include_tax=prices_include_tax,
            )
            if totals.problems or totals.amount_cents != int(amount_cents):
                _shadow_record(
                    "i3_totals",
                    invoice,
                    {"amount_cents": int(amount_cents)},
                    {"amount_cents": totals.amount_cents, "problems": list(totals.problems)},
                    "create",
                )
        elif mode == "v2":
            totals = compute_totals(lines, prices_include_tax=prices_include_tax)
            if totals.amount_cents != int(amount_cents):
                raise InvoiceError(
                    f"invoice lines add up to {totals.amount_cents}, not {int(amount_cents)}"
                )
        if publish:
            InvoiceService.publish(db, invoice)
        return invoice

    @staticmethod
    def _lock_owner(
        db: Session, *, reseller_id: Optional[int], billing_account_id: Optional[int]
    ) -> None:
        """Lock the new invoice's reseller and billing account before any number row.

        The account is the one ``BillingLocks.for_invoice`` will lock for the
        invoice: its own, else the reseller's. In engine ``v2`` publish settles
        in the same transaction, and settlement locks both.
        """
        from app.models.commerce_account import BillingAccount
        from app.services.billing_locks import BillingLocks

        if reseller_id is not None:
            BillingLocks.reseller(db, int(reseller_id))
        account_id = billing_account_id
        if account_id is None and reseller_id is not None:
            account_id = db.scalar(
                select(BillingAccount.id).where(BillingAccount.reseller_id == reseller_id)
            )
        if account_id is not None:
            BillingLocks.billing_account(db, int(account_id))

    @staticmethod
    def publish(db: Session, invoice: Invoice) -> Invoice:
        """``draft`` to ``open``: totals, snapshots, number, ``invoice.created`` (design 3.3).

        For a v2 invoice the lines are validated and the totals written; any
        other invoice keeps its legacy total and NULL v2 totals. The number is
        allocated last. A v2 invoice is then recomputed, so a zero total
        settles at once.
        """
        from app.services.billing import settlement

        if invoice.status != InvoiceStatus.DRAFT:
            raise InvoiceTransitionError(
                f"invoice {invoice.id} is {invoice.status.value}, only a draft can be published"
            )
        db.flush()
        lines = list(
            db.execute(
                select(InvoiceLine)
                .where(InvoiceLine.invoice_id == invoice.id)
                .order_by(InvoiceLine.sort_order, InvoiceLine.id)
            ).scalars()
        )
        v2 = invoice.model_version == 2
        if v2:
            prices_include_tax = bool(invoice.prices_include_tax)
            totals = compute_totals(lines, prices_include_tax=prices_include_tax)
            if not lines:
                raise InvoiceError("a v2 invoice needs at least one line")
            if totals.problems:
                raise InvoiceError("invalid invoice lines: " + "; ".join(totals.problems))
            if totals.amount_cents == 0 and not zero_amounts_supported(db):
                raise InvoiceError("zero-total invoices need the invoices check revision")
            invoice.subtotal_cents = totals.subtotal_cents
            invoice.discount_cents = totals.discount_cents
            invoice.tax_cents = totals.tax_cents
            invoice.prices_include_tax = prices_include_tax
            invoice.amount_cents = totals.amount_cents
        now = _utcnow()
        invoice.issued_at = now
        invoice.seller_snapshot = seller_snapshot(now)
        invoice.buyer_snapshot = buyer_snapshot(db, invoice, now)
        auto_apply = v2 and InvoiceService._auto_apply_credit(db, invoice)
        if v2:
            # The recompute below settles and may credit an overpayment, and
            # auto-apply writes the ledger: reseller, account and invoice come
            # before the number counter in the lock order.
            from app.services.billing_locks import BillingLocks

            BillingLocks.for_invoice(db, invoice.id, reseller=True, billing_account=True)
        InvoiceService._allocate_publish_number(db, invoice, now, required=v2)
        InvoiceService.transition(db, invoice, InvoiceStatus.OPEN, cause=CAUSE_PUBLISH)
        settlement.fire(db, settlement.INVOICE_CREATED, settlement.InvoiceCreated(invoice=invoice))
        if auto_apply:
            from app.services.credit_ledger_service import CreditLedgerService

            CreditLedgerService.apply_to_invoice(
                db, invoice, idempotency_key=f"auto-apply:{invoice.id}"
            )
        if v2:
            InvoiceService.recompute(db, invoice, cause=CAUSE_PUBLISH)
        return invoice

    @staticmethod
    def _auto_apply_credit(db: Session, invoice: Invoice) -> bool:
        """Retail auto-apply of account credit after publish (design 6.7).

        Needs retail credit to be live (``retail_credit_available``: credit
        engine v2 and ``billing_credit_enabled_retail``) as well as the
        auto-apply setting.
        """
        from app.services.credit_ledger_service import CreditLedgerService
        from app.services.settings_store import get_setting

        if invoice.reseller_id is not None or invoice.billing_account_id is None:
            return False
        if invoice.purpose == InvoicePurpose.CREDIT_TOPUP:
            return False
        if not get_setting(db, "billing_credit_auto_apply_retail"):
            return False
        return CreditLedgerService.retail_credit_available(db)

    @staticmethod
    def _allocate_publish_number(
        db: Session, invoice: Invoice, at: datetime, *, required: bool
    ) -> None:
        """The legal number at publish: an invoice number, or a proforma number (design 5.1)."""
        from app.models.billing_series import DocumentSeries, DocumentType
        from app.services.invoice_numbering import InvoiceNumbering, NumberingError

        proforma = invoice.document_type == InvoiceDocumentType.PROFORMA
        try:
            if proforma:
                series = InvoiceNumbering.default_series(db, DocumentType.PROFORMA)
            elif invoice.series_id is not None:
                series = db.get(DocumentSeries, invoice.series_id)
                if series is None or not series.enabled or series.document_type != DocumentType.INVOICE:
                    raise NumberingError(f"series {invoice.series_id} cannot number invoices")
            else:
                series = InvoiceNumbering.default_series(db, DocumentType.INVOICE)
        except NumberingError:
            if required:
                raise
            logger.warning("Invoice %s published without a legal number: no series", invoice.id)
            return
        period, value, display = InvoiceNumbering.allocate(
            db, series, at=at, invoice_number=invoice.invoice_number
        )
        if proforma:
            invoice.proforma_series_id = series.id
            invoice.proforma_number_period = period
            invoice.proforma_number_value = value
            invoice.proforma_number_display = display
        else:
            invoice.series_id = series.id
            invoice.number_period = period
            invoice.number_value = value
            invoice.number_display = display
        db.flush()

    @staticmethod
    def convert_proforma(db: Session, invoice: Invoice, at: datetime) -> None:
        """Give a paid proforma its invoice number and make it an invoice (design 5.4).

        Runs inside the settlement transaction under the guard's
        ``proforma_conversion`` allowance; the proforma number and the
        snapshots stay.
        """
        from app.models.billing_series import DocumentType
        from app.services.invoice_numbering import InvoiceNumbering

        if invoice.document_type != InvoiceDocumentType.PROFORMA:
            return
        series = InvoiceNumbering.default_series(db, DocumentType.INVOICE)
        with invoice_guard.allow(
            invoice_guard.PROFORMA_CONVERSION,
            {"document_type", "issued_at", "series_id", "number_period", "number_value", "number_display"},
            invoice.id,
        ):
            period, value, display = InvoiceNumbering.allocate(
                db, series, at=at, invoice_number=invoice.invoice_number
            )
            invoice.series_id = series.id
            invoice.number_period = period
            invoice.number_value = value
            invoice.number_display = display
            invoice.document_type = InvoiceDocumentType.INVOICE
            invoice.issued_at = at
            db.flush()

    @staticmethod
    def delete_draft(db: Session, invoice: Invoice) -> None:
        """Hard delete a draft and its lines (design 3.3); never a published invoice."""
        if invoice.status != InvoiceStatus.DRAFT:
            raise InvoiceTransitionError("only a draft invoice can be deleted")
        if invoice.number_display is not None or invoice.proforma_number_display is not None:
            raise InvoiceTransitionError("an invoice with a legal number cannot be deleted")
        db.delete(invoice)
        db.flush()

    @staticmethod
    def create_topup(
        db: Session,
        *,
        reseller_id: Optional[int] = None,
        amount_cents: int,
        billing_account_id: Optional[int] = None,
    ) -> Invoice:
        return InvoiceService.create(
            db,
            reseller_id=reseller_id,
            billing_account_id=billing_account_id,
            purpose=InvoicePurpose.CREDIT_TOPUP,
            amount_cents=amount_cents,
            description=(
                "Reseller account credit top-up"
                if reseller_id is not None
                else "Account credit top-up"
            ),
            due_at=datetime.now(timezone.utc),
        )

    @staticmethod
    def create_deploy(
        db: Session,
        *,
        reseller_id: int,
        amount_cents: int,
        description: str,
        service_id: Optional[int] = None,
        currency: str = "USD",
    ) -> Invoice:
        return InvoiceService.create(
            db,
            reseller_id=reseller_id,
            purpose=InvoicePurpose.DEPLOY_CHARGE,
            amount_cents=amount_cents,
            description=description,
            service_id=service_id,
            due_at=datetime.now(timezone.utc),
            currency=currency,
        )

    @staticmethod
    def create_cycle(
        db: Session,
        *,
        reseller_id: Optional[int],
        service_id: int,
        amount_cents: int,
        description: str,
        due_at: Optional[datetime] = None,
        currency: str = "USD",
        billing_account_id: Optional[int] = None,
        service_billing_id: Optional[int] = None,
        period_start: Optional[datetime] = None,
        period_end: Optional[datetime] = None,
    ) -> Invoice:
        line = NewLine(
            description=description[:512],
            unit_cents=int(amount_cents) if type(amount_cents) is int else amount_cents,
            kind=InvoiceLineKind.PRODUCT,
            service_id=service_id,
            service_billing_id=service_billing_id,
            period_start=period_start,
            period_end=period_end,
        )
        return InvoiceService.create(
            db,
            reseller_id=reseller_id,
            purpose=InvoicePurpose.CYCLE_CHARGE,
            amount_cents=amount_cents,
            description=description,
            service_id=service_id,
            due_at=due_at or datetime.now(timezone.utc),
            currency=currency,
            billing_account_id=billing_account_id,
            lines=[line],
        )

    @staticmethod
    def get_for_update(db: Session, invoice_id: int) -> Optional[Invoice]:
        """Lock the invoice (and its order first) and re-read it; see ``billing_locks``."""
        from app.services.billing_locks import BillingLocks

        return BillingLocks.for_invoice(db, invoice_id)

    # --- Balance ---

    @staticmethod
    def _legacy_allocated_cents(db: Session, invoice_id: int) -> int:
        value = db.scalar(
            select(func.coalesce(func.sum(Payment.amount_cents), 0)).where(
                Payment.invoice_id == invoice_id,
                Payment.status == PaymentStatus.SUCCEEDED,
            )
        )
        return int(value or 0)

    @staticmethod
    def allocated_cents(db: Session, invoice_id: int) -> int:
        """Succeeded payments (legacy and shadow), or ``balance().paid_net_cents`` in v2.

        Deprecated for new code: use ``balance()``.
        """
        if engine_mode(db) == "v2":
            invoice = db.get(Invoice, invoice_id)
            if invoice is not None:
                return InvoiceService.balance(db, invoice).paid_net_cents
        return InvoiceService._legacy_allocated_cents(db, invoice_id)

    @staticmethod
    def balance(db: Session, invoice: Invoice, *, for_update: bool = False) -> InvoiceBalance:
        """The invoice's balance from two aggregate queries. Never writes.

        A payment marked ``refunded`` whose ``refunded_cents`` is still 0 was
        refunded by a path that does not write the column; it counts as
        refunded in full, the fact ``billing_backfill`` records for older rows.
        A partially refunded payment never counts more than its amount as
        refunded. An invoice that is not flushed yet has no payments or notes.

        ``for_update`` makes both queries locking reads of the invoice's
        payments and notes, for a caller that holds the invoice lock and
        decides money from the result: under REPEATABLE READ (MySQL, MariaDB)
        a plain read sees the transaction's first snapshot, which may predate
        a payment committed while it waited for the lock. Payments and notes
        come after the invoice in the lock order.
        """
        total = int(invoice.amount_cents or 0)
        if invoice.id is None:
            return InvoiceBalance(total, 0, 0, total, 0, 0, 0, 0, total, max(0, total))
        refunded_column = case(
            (
                and_(Payment.status == PaymentStatus.REFUNDED, Payment.refunded_cents == 0),
                Payment.amount_cents,
            ),
            (Payment.refunded_cents > Payment.amount_cents, Payment.amount_cents),
            else_=Payment.refunded_cents,
        )
        payments_query = (
            select(
                Payment.status,
                func.coalesce(func.sum(Payment.amount_cents), 0),
                func.coalesce(func.sum(refunded_column), 0),
            )
            .where(Payment.invoice_id == invoice.id)
            .group_by(Payment.status)
        )
        notes_query = (
            select(
                InvoiceAdjustment.kind,
                func.coalesce(func.sum(InvoiceAdjustment.total_cents), 0),
            )
            .where(InvoiceAdjustment.invoice_id == invoice.id)
            .group_by(InvoiceAdjustment.kind)
        )
        if for_update:
            db.flush()
            payments_query = payments_query.with_for_update()
            notes_query = notes_query.with_for_update()
        payments = db.execute(payments_query).all()
        paid_gross = refunded = pending = 0
        for status, amount, refunded_sum in payments:
            if status in _PAID_PAYMENT_STATUSES:
                paid_gross += int(amount)
                refunded += int(refunded_sum)
            elif status == PaymentStatus.PENDING:
                pending += int(amount)
        notes = dict(db.execute(notes_query).all())
        debit = int(notes.get(AdjustmentKind.DEBIT, 0))
        credit = int(notes.get(AdjustmentKind.CREDIT, 0))
        worth = total + debit - credit
        paid_net = paid_gross - refunded
        due = worth - paid_net
        return InvoiceBalance(
            total_cents=total,
            debit_cents=debit,
            credit_cents=credit,
            worth_cents=worth,
            paid_gross_cents=paid_gross,
            refunded_cents=refunded,
            paid_net_cents=paid_net,
            pending_cents=pending,
            due_cents=due,
            remaining_cents=max(0, due),
        )

    # --- The state machine ---

    @staticmethod
    def target_status(
        db: Session,
        invoice: Invoice,
        balance: InvoiceBalance,
        *,
        cause: str,
    ) -> InvoiceStatus:
        """The status the v2 rule gives (design 3.2), for a balance with ``due >= 0``.

        Terminal statuses and drafts are returned unchanged. A negative due is
        reported as ``paid``; the caller converts the overpayment first.
        """
        status = invoice.status
        if status in TERMINAL_V2 or status == S.DRAFT:
            return status
        if balance.due_cents <= 0:
            if balance.due_cents < 0 or balance.paid_net_cents > 0:
                return S.PAID
            if balance.refunded_cents > 0:
                return S.REFUNDED
            if balance.credit_cents > 0:
                return S.VOID
            return S.PAID
        if balance.pending_cents > 0:
            return S.PENDING_ACTION
        if status == S.COLLECTIONS:
            return S.COLLECTIONS
        if cause == CAUSE_DISPUTE and InvoiceService._dispute_reopens(db, invoice):
            return S.COLLECTIONS
        if status == S.OVERDUE:
            return S.OVERDUE
        if balance.paid_net_cents > 0:
            return S.PARTIALLY_PAID
        return S.OPEN

    @staticmethod
    def _dispute_reopens(db: Session, invoice: Invoice) -> bool:
        from app.services.settings_store import get_setting

        key = (
            "billing_dispute_policy_reseller"
            if invoice.reseller_id is not None
            else "billing_dispute_policy_retail"
        )
        return get_setting(db, key) == "reopen_invoice"

    @staticmethod
    def recompute(
        db: Session,
        invoice: Invoice,
        *,
        cause: str,
        payment: Optional[Payment] = None,
    ) -> Invoice:
        """Derive the status from ``balance()`` and move the invoice there (design 3.1).

        Goes through the settlement function, which takes the invoice lock,
        converts an overpayment, settles a paid invoice and makes any other
        move with ``transition()``. In engines ``legacy`` and ``shadow`` it is
        today's settlement (``mark_paid_if_fully_allocated``). ``payment`` is
        the payment whose change caused the call, if any.
        """
        from app.services.billing.settlement import settle_invoice

        return settle_invoice(db, invoice.id, payment=payment, cause=cause)

    @staticmethod
    def transition(
        db: Session,
        invoice: Invoice,
        to: InvoiceStatus,
        *,
        cause: str,
        actor_user_id: Optional[int] = None,
        reason: Optional[str] = None,
    ) -> Invoice:
        """The one writer of ``invoices.status`` (design 3.3 and 3.4).

        The caller holds the invoice row lock. Moving to the current status is
        a no-op. Raises ``InvoiceTransitionError`` for an edge that is not in
        the table of the current engine mode, and for any move to ``paid``
        whose cause is not ``settlement``.
        """
        from app.services.audit_service import AuditService
        from app.services.billing import settlement

        to = InvoiceStatus(to)
        current = invoice.status
        if current == to:
            return invoice
        if to == S.PAID and cause != CAUSE_SETTLEMENT:
            raise InvoiceTransitionError("only the settlement function marks an invoice paid")
        mode = engine_mode(db)
        if mode == "v2":
            allowed = V2_EDGES.get(current, frozenset())
        else:
            allowed = LEGACY_EDGES.get(_LEGACY_FROM.get(current, current), frozenset())
        if to not in allowed:
            raise InvoiceTransitionError(
                f"invoice {invoice.id} cannot move from {current.value} to {to.value} "
                f"(engine {mode})"
            )
        now = _utcnow()
        invoice.status = to
        invoice.status_changed_at = now
        if to == S.PAID:
            invoice.paid_at = invoice.paid_at or now
        elif to == S.OPEN and current == S.PAID and mode != "v2":
            # Legacy reopening clears the payment time, as it always has.
            invoice.paid_at = None
        if to == S.VOID:
            invoice.voided_at = now
            invoice.void_reason = (reason or cause)[:255]
        db.flush()
        AuditService.log(
            db,
            actor_user_id=actor_user_id,
            billing_account_id=invoice.billing_account_id,
            action="invoice.status",
            resource_type="invoice",
            resource_id=str(invoice.id),
            metadata={
                "from": current.value,
                "to": to.value,
                "cause": cause,
                "reason": reason,
                "engine": mode,
            },
        )
        if mode == "v2":
            if to == S.OVERDUE:
                due = InvoiceService.balance(db, invoice).remaining_cents
                settlement.fire(
                    db,
                    settlement.INVOICE_OVERDUE,
                    settlement.InvoiceOverdue(invoice=invoice, due_cents=due),
                )
            elif to == S.VOID:
                settlement.fire(
                    db,
                    settlement.INVOICE_VOIDED,
                    settlement.InvoiceVoided(invoice=invoice, reason=invoice.void_reason),
                )
            elif to == S.REFUNDED:
                refunded = InvoiceService.balance(db, invoice).refunded_cents
                settlement.fire(
                    db,
                    settlement.INVOICE_REFUNDED,
                    settlement.InvoiceRefunded(invoice=invoice, refunded_cents=refunded),
                )
        return invoice

    @staticmethod
    def void(
        db: Session,
        invoice: Invoice,
        *,
        reason: str,
        actor_user_id: Optional[int] = None,
    ) -> Invoice:
        """Void an unpaid invoice, keeping its number (design 3.3).

        Locks the invoice. In engine ``v2`` only ``open`` and ``overdue``
        invoices with nothing paid, nothing pending, no credit or debit note
        and no refund in progress can be voided; in ``legacy`` and ``shadow``
        today's edges apply. Voiding a void invoice is a no-op.
        """
        from app.models.refund import Refund, RefundStatus

        locked = InvoiceService.get_for_update(db, invoice.id) or invoice
        if locked.status == S.VOID:
            return locked
        if engine_mode(db) == "v2":
            if locked.status not in {S.OPEN, S.OVERDUE}:
                raise InvoiceTransitionError(
                    f"a {locked.status.value} invoice cannot be voided; issue a credit note"
                )
            # Locking reads, so payments, notes and refunds committed while
            # this transaction waited for the invoice lock are seen.
            balance = InvoiceService.balance(db, locked, for_update=True)
            if balance.paid_net_cents or balance.pending_cents:
                raise InvoiceTransitionError("an invoice with payments cannot be voided")
            if balance.debit_cents or balance.credit_cents:
                raise InvoiceTransitionError("an invoice with credit or debit notes cannot be voided")
            refunding = db.scalar(
                select(func.count(Refund.id))
                .where(
                    Refund.invoice_id == locked.id,
                    Refund.status.in_([RefundStatus.PENDING, RefundStatus.REVIEW]),
                )
                .with_for_update()
            )
            if refunding:
                raise InvoiceTransitionError("an invoice with a refund in progress cannot be voided")
        return InvoiceService.transition(
            db, locked, S.VOID, cause="void", actor_user_id=actor_user_id, reason=reason
        )

    @staticmethod
    def change_due_date(
        db: Session,
        invoice: Invoice,
        due_at: Optional[datetime],
        *,
        actor_user_id: Optional[int] = None,
    ) -> Invoice:
        """Change ``due_at`` of a draft or unpaid invoice, audited (design 3.5)."""
        from app.services.audit_service import AuditService

        locked = InvoiceService.get_for_update(db, invoice.id) or invoice
        if locked.status not in UNPAID_STATUSES and locked.status != S.DRAFT:
            raise InvoiceError(f"the due date of a {locked.status.value} invoice cannot change")
        if due_at is not None and due_at.tzinfo is None:
            due_at = due_at.replace(tzinfo=timezone.utc)
        previous = locked.due_at
        locked.due_at = due_at
        db.flush()
        AuditService.log(
            db,
            actor_user_id=actor_user_id,
            billing_account_id=locked.billing_account_id,
            action="invoice.due_date",
            resource_type="invoice",
            resource_id=str(locked.id),
            metadata={
                "from": previous.isoformat() if previous else None,
                "to": due_at.isoformat() if due_at else None,
            },
        )
        return locked

    @staticmethod
    def mark_paid_if_fully_allocated(
        db: Session,
        invoice_id: int,
        *,
        payment: Optional[Payment] = None,
        source: Optional[str] = None,
    ) -> Invoice:
        """Settle the invoice through the single settlement function (alias of ``recompute``)."""
        from app.services.billing.settlement import settle_invoice

        return settle_invoice(db, invoice_id, payment=payment, source=source)

    # --- Serialisers ---

    @staticmethod
    def display_number(invoice: Invoice) -> str:
        """The legal number when set, else the internal number, as old invoices show."""
        return invoice.number_display or invoice.proforma_number_display or str(invoice.invoice_number)

    @staticmethod
    def serialize_payment(payment: Payment) -> dict:
        return {
            "id": payment.id,
            "gateway": payment.gateway,
            "status": payment.status.value,
            "amount_cents": payment.amount_cents,
            "currency": payment.currency,
            "external_ref": payment.external_ref,
            "failure_code": payment.failure_code,
            "failure_message": payment.failure_message,
            "processed_at": payment.processed_at,
            "refunded_at": payment.refunded_at,
            "created_at": payment.created_at,
            "refunded_cents": int(payment.refunded_cents or 0),
        }

    @staticmethod
    def _serialize_adjustment(row: InvoiceAdjustment) -> dict:
        return {
            "id": row.id,
            "kind": _kind(row.kind),
            "number_display": row.number_display,
            "total_cents": int(row.total_cents),
            "tax_cents": int(row.tax_cents or 0),
            "currency": row.currency,
            "reason_code": _kind(row.reason_code),
            "outcome": _kind(row.outcome),
            "issued_at": row.issued_at,
        }

    @staticmethod
    def _serialize_refund(row) -> dict:
        return {
            "id": row.id,
            "payment_id": row.payment_id,
            "amount_cents": int(row.amount_cents),
            "currency": row.currency,
            "destination": _kind(row.destination),
            "reason": _kind(row.reason),
            "status": _kind(row.status),
            "adjustment_id": row.adjustment_id,
            "requested_at": row.requested_at,
            "completed_at": row.completed_at,
        }

    @staticmethod
    def serialize(db: Session, invoice: Invoice, *, payments: bool = False) -> dict:
        """Invoice fields for every API; v2 money fields come from ``balance()``.

        In ``legacy`` and ``shadow`` ``allocated_cents`` and ``remaining_cents``
        keep their old meaning; ``balance`` always carries the v2 view.
        ``payments=True`` (detail views) adds payments, credit and debit notes
        and refunds.
        """
        balance = InvoiceService.balance(db, invoice)
        if engine_mode(db) == "v2":
            allocated = balance.paid_net_cents
            remaining = balance.remaining_cents
        else:
            allocated = InvoiceService._legacy_allocated_cents(db, invoice.id)
            remaining = max(0, invoice.amount_cents - allocated)
        payload = {
            "id": invoice.id,
            "invoice_number": invoice.invoice_number,
            "reseller_id": invoice.reseller_id,
            "service_id": invoice.service_id,
            "purpose": invoice.purpose.value,
            "status": public_invoice_status(invoice.status),
            "status_detail": invoice.status.value,
            "amount_cents": invoice.amount_cents,
            "allocated_cents": allocated,
            "remaining_cents": remaining,
            "currency": invoice.currency,
            "description": invoice.description,
            "due_at": invoice.due_at,
            "paid_at": invoice.paid_at,
            "created_at": invoice.created_at,
            "updated_at": invoice.updated_at,
            "number_display": invoice.number_display,
            "display_number": InvoiceService.display_number(invoice),
            "proforma_number_display": invoice.proforma_number_display,
            "document_type": _kind(invoice.document_type) or InvoiceDocumentType.INVOICE.value,
            "issued_at": invoice.issued_at,
            "balance": balance.as_dict(),
        }
        if payments:
            from app.models.refund import Refund

            payload["payments"] = [
                InvoiceService.serialize_payment(row)
                for row in sorted(invoice.payments, key=lambda item: item.id)
            ]
            payload["adjustments"] = [
                InvoiceService._serialize_adjustment(row)
                for row in db.execute(
                    select(InvoiceAdjustment)
                    .where(InvoiceAdjustment.invoice_id == invoice.id)
                    .order_by(InvoiceAdjustment.id)
                ).scalars()
            ]
            payload["refunds"] = [
                InvoiceService._serialize_refund(row)
                for row in db.execute(
                    select(Refund).where(Refund.invoice_id == invoice.id).order_by(Refund.id)
                ).scalars()
            ]
        return payload
