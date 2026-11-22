"""The email events, their documented variables and their built-in texts (docs/billing-phase2-design.md 12.1, 12.2).

Every event has two built-in forms of the same text: ``builtin_email`` builds
it in Python from whatever data a sender gave (it never fails on a missing
variable), and ``EventTemplateSpec.default_subject`` / ``default_body`` hold it
as a template, which is what "revert to built-in" saves and what the seeded
``email_templates`` rows contain. A test keeps the two forms equal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from app.services.email_layout import render_email_html
from app.services.billing.operator_currency import operator_currency
from app.services.notification_service import NotificationEvent, render_notification

_NOTIFICATION_EVENTS = frozenset(
    {
        NotificationEvent.CYCLE_CHARGE_FAILED,
        NotificationEvent.PAYMENT_ACTION_REQUIRED,
        NotificationEvent.GRACE_WARNING,
        NotificationEvent.SERVICE_SUSPENDED,
        NotificationEvent.PAYMENT_RECOVERED,
        NotificationEvent.TOPUP_RECEIVED,
        NotificationEvent.USDT_PENDING,
        NotificationEvent.USDT_DETECTED,
    }
)


@dataclass(frozen=True)
class VariableSpec:
    name: str
    type: str
    description: str
    sample: Any
    required: bool = True


@dataclass(frozen=True)
class EventTemplateSpec:
    key: str
    title: str
    description: str
    variables: tuple[VariableSpec, ...]
    default_subject: str
    default_body: str
    # True when mail of this event was sent before phase 2, so its template is seeded enabled.
    sends_today: bool = False
    # Only the welcome event accepts a per-product variant.
    accepts_product: bool = False

    @property
    def samples(self) -> dict[str, Any]:
        return {variable.name: variable.sample for variable in self.variables}

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "description": self.description,
            "accepts_product": self.accepts_product,
            "sends_today": self.sends_today,
            "default_subject": self.default_subject,
            "default_body": self.default_body,
            "variables": [
                {
                    "name": variable.name,
                    "type": variable.type,
                    "description": variable.description,
                    "sample": variable.sample,
                    "required": variable.required,
                }
                for variable in self.variables
            ],
        }


_MONEY = '{{ amount_cents|default(0, true)|money(currency|default("USD", true)) }}'
_INVOICE = '{{ invoice_number|default(invoice_id|default(""), true) }}'
_ORDER = '{{ order_number|default(order_id|default(""), true) }}'
_SERVICE = '{{ service_name|default("your service", true) }}'
_TICKET = '{{ ticket_number|default(ticket_id|default(""), true) }}'


def _link(variable: str, label: str) -> str:
    return f'{{% if {variable}|default("", true) %}} {label}: {{{{ {variable} }}}}{{% endif %}}'


def _var(name: str, type_: str, description: str, sample: Any, required: bool = True) -> VariableSpec:
    return VariableSpec(name, type_, description, sample, required)


_INVOICE_VARIABLES = (
    _var("invoice_number", "string", "The invoice number shown to the customer", "INV-2026-0001", False),
    _var("invoice_id", "integer", "The invoice id (used when there is no number)", 1001, False),
)
_MONEY_VARIABLES = (
    _var("amount_cents", "integer", "The amount in minor units (cents)", 1234, False),
    _var("currency", "string", "ISO 4217 currency code, USD when absent", "GBP", False),
)

_SPECS = (
    EventTemplateSpec(
        "welcome",
        "Welcome",
        "Sent after a customer verifies their email address.",
        (_var("username", "string", "The customer's username", "alex", False),),
        "Welcome to Rackflow",
        'Welcome to Rackflow, {{ username|default("there") }}! Your account is ready.',
        accepts_product=True,
    ),
    EventTemplateSpec(
        "verify_email",
        "Verify email address",
        "Sent at registration with the verification link.",
        (_var("verify_url", "string", "The verification link; empty when no public URL is configured", "https://example.com/verify-email?token=abc", False),),
        "Verify your Rackflow email address",
        "Please verify your email address to activate your account." + _link("verify_url", "Open this link"),
        sends_today=True,
    ),
    EventTemplateSpec(
        "password_reset",
        "Password reset",
        "Sent when a customer asks to reset their password.",
        (_var("reset_url", "string", "The reset link", "https://example.com/reset-password?token=abc", False),),
        "Reset your Rackflow password",
        "We received a request to reset your password." + _link("reset_url", "Use this link"),
        sends_today=True,
    ),
    EventTemplateSpec(
        "user_invite",
        "User invitation",
        "Sent when an administrator invites a user.",
        (_var("invite_url", "string", "The invitation link", "https://example.com/accept-invite?token=abc", False),),
        "You have been invited to Rackflow",
        "You have been invited to join Rackflow. Accept the invitation to choose your username and password."
        + _link("invite_url", "Open this link")
        + " The link expires in 7 days.",
        sends_today=True,
    ),
    EventTemplateSpec(
        "order_confirmation",
        "Order confirmation",
        "Sent when an order is placed.",
        (
            _var("order_number", "string", "The order number", "ORD-7", False),
            _var("order_id", "integer", "The order id (used when there is no number)", 7, False),
        )
        + _MONEY_VARIABLES,
        "Your Rackflow order confirmation",
        "Thank you for your order " + _ORDER + ". We received your request and "
        "will notify you when provisioning completes.",
        sends_today=True,
    ),
    EventTemplateSpec(
        "invoice_created",
        "Invoice created",
        "Sent when an invoice is issued to a customer.",
        _INVOICE_VARIABLES + _MONEY_VARIABLES,
        "New Rackflow invoice",
        "Invoice " + _INVOICE + " for " + _MONEY + " is now available in your account.",
        sends_today=True,
    ),
    EventTemplateSpec(
        "invoice_paid",
        "Invoice paid",
        "Sent when an invoice is paid.",
        _INVOICE_VARIABLES + _MONEY_VARIABLES,
        "Rackflow invoice paid",
        "Payment of " + _MONEY + " for invoice " + _INVOICE + " was received. Thank you.",
        sends_today=True,
    ),
    EventTemplateSpec(
        "invoice_overdue",
        "Invoice overdue",
        "Sent when an invoice passes its due date unpaid.",
        _INVOICE_VARIABLES + _MONEY_VARIABLES,
        "Rackflow invoice overdue",
        "Invoice " + _INVOICE + " for " + _MONEY + " is overdue. Please sign in to pay.",
    ),
    EventTemplateSpec(
        "payment_failed",
        "Payment failed",
        "Sent when a card payment attempt fails.",
        _INVOICE_VARIABLES + _MONEY_VARIABLES,
        "Rackflow payment failed",
        "A payment attempt for invoice " + _INVOICE + " (" + _MONEY + ") failed. "
        "Please update your payment method or try again.",
    ),
    EventTemplateSpec(
        "service_provisioned",
        "Service provisioned",
        "Sent when a purchased service is ready.",
        (_var("service_name", "string", "The service name", "vm-01", False),),
        "Your Rackflow service is ready",
        _SERVICE + " has been provisioned and is ready to use.",
    ),
    # ``EmailEvent.SERVICE_SUSPENDED`` and ``NotificationEvent.SERVICE_SUSPENDED``
    # share their value, so this event's text is the reseller suspension notice.
    EventTemplateSpec(
        "service_suspended",
        "Service suspended",
        "Sent when a service is suspended for non-payment (and when a dispute suspends it).",
        (_var("service_name", "string", "The service name", "vm-01", False),) + _INVOICE_VARIABLES,
        "Rackflow service suspended for nonpayment",
        _SERVICE + " was suspended because invoice " + _INVOICE + " remains unpaid. "
        "Pay the invoice in the reseller panel to request automatic recovery.",
        sends_today=True,
    ),
    EventTemplateSpec(
        "service_suspended_nonpayment",
        "Service suspended for non-payment",
        "Sent to a retail customer when the non-payment suspension job suspends their service.",
        (_var("service_name", "string", "The service name", "vm-01", False),) + _INVOICE_VARIABLES,
        "Your Rackflow service is suspended",
        _SERVICE + " was suspended because invoice " + _INVOICE + " remains unpaid. "
        "Sign in to the client area to pay it and restore the service.",
    ),
    EventTemplateSpec(
        "ticket_reply",
        "Support ticket reply",
        "Sent to the ticket owner when staff reply.",
        (
            _var("ticket_number", "string", "The ticket number", "T-1001", False),
            _var("ticket_id", "integer", "The ticket id (used when there is no number)", 1001, False),
        ),
        "New reply to your support ticket",
        "There is a new reply on support ticket " + _TICKET + ".",
    ),
    EventTemplateSpec(
        "invoice_reminder",
        "Invoice reminder",
        "Sent before an invoice falls due.",
        _INVOICE_VARIABLES
        + _MONEY_VARIABLES
        + (_var("due_date", "date (ISO 8601)", "The invoice due date", "2026-12-01"),),
        "Rackflow invoice due soon",
        "Invoice " + _INVOICE + " for " + _MONEY + " is due on {{ due_date }}. Please sign in to pay.",
    ),
    EventTemplateSpec(
        "late_fee_added",
        "Late fee added",
        "Sent when a late fee is added to an overdue invoice.",
        _INVOICE_VARIABLES
        + (
            _var("fee_cents", "integer", "The late fee in minor units (cents)", 250),
            _var("currency", "string", "ISO 4217 currency code, USD when absent", "GBP", False),
        ),
        "Late fee added to your Rackflow invoice",
        'A late fee of {{ fee_cents|money(currency|default("USD", true)) }} was added to invoice '
        + _INVOICE
        + ", which is overdue. Please sign in to pay.",
    ),
    EventTemplateSpec(
        "service_termination_warning",
        "Service termination warning",
        "Sent before a service suspended for non-payment is terminated.",
        (_var("service_name", "string", "The service name", "vm-01", False),)
        + _INVOICE_VARIABLES
        + (_var("terminate_date", "date (ISO 8601)", "The date the service will be terminated", "2026-12-08"),),
        "Your Rackflow service will be terminated",
        _SERVICE + " is suspended for non-payment and will be terminated on {{ terminate_date }} "
        "unless invoice " + _INVOICE + " is paid. Please sign in to pay.",
    ),
    EventTemplateSpec(
        "service_terminated",
        "Service terminated",
        "Sent when a service is terminated for non-payment.",
        (_var("service_name", "string", "The service name", "vm-01", False),),
        "Your Rackflow service was terminated",
        _SERVICE + " was terminated for non-payment.",
    ),
    EventTemplateSpec(
        "cancellation_requested",
        "Cancellation requested",
        "Sent when a cancellation request is recorded.",
        (
            _var("service_name", "string", "The service name", "vm-01", False),
            _var("effective_date", "date (ISO 8601)", "The date the cancellation takes effect", "2026-12-31"),
        ),
        "Your Rackflow cancellation request",
        "We received your request to cancel " + _SERVICE + ". It takes effect on {{ effective_date }}.",
    ),
    EventTemplateSpec(
        "cancellation_completed",
        "Cancellation completed",
        "Sent when a cancelled service has ended.",
        (_var("service_name", "string", "The service name", "vm-01", False),),
        "Your Rackflow service was cancelled",
        _SERVICE + " has been cancelled.",
    ),
    EventTemplateSpec(
        "tax_id_unverified",
        "Tax ID could not be verified",
        "Sent once per billing profile change when the tax ID check ends invalid or badly formatted.",
        (),
        "We could not verify your VAT number",
        "We could not verify the VAT number on your billing profile, so VAT is charged on your invoices. "
        "Please check the number in your account.",
    ),
)

EVENTS: dict[str, EventTemplateSpec] = {spec.key: spec for spec in _SPECS}


def money_text(data: dict) -> str:
    cents = int(data.get("amount_cents") or 0)
    currency = str(data.get("currency") or operator_currency()).upper()
    return f"{currency} {cents / 100:.2f}"


def builtin_subject(event: str) -> str:
    spec = EVENTS.get(event)
    return spec.default_subject if spec is not None else "Rackflow notification"


def builtin_html(event: str, subject: str, body: str, data: dict) -> str:
    """The built-in HTML of an event around a subject and plain text."""
    if event == "password_reset":
        return render_email_html(
            heading="Reset your password",
            paragraphs=["We received a request to reset your password. Choose a new one with the button below."],
            action_url=str(data.get("reset_url") or "") or None,
            action_label="Reset password",
            footnote="If you didn't ask for this, you can ignore this email; your password won't change.",
        )
    if event == "user_invite":
        return render_email_html(
            heading="You have been invited to Rackflow",
            paragraphs=[
                "An account has been created for you. Accept the invitation to choose your username and password.",
                "The link expires in 7 days.",
            ],
            action_url=str(data.get("invite_url") or "") or None,
            action_label="Accept invitation",
            footnote="If you weren't expecting this invitation, you can ignore this email.",
        )
    if event == "verify_email":
        return render_email_html(
            heading="Verify your email address",
            paragraphs=["Please verify your email address to activate your account."],
            action_url=str(data.get("verify_url") or "") or None,
            action_label="Verify email",
        )
    if event in _NOTIFICATION_EVENTS:
        return render_email_html(heading=subject, paragraphs=body.split("\n\n"))
    return render_email_html(heading=subject, paragraphs=[body])


def builtin_email(event: str, data: dict) -> tuple[str, str, Optional[str]]:
    """Subject, plain text and HTML of an event from the texts built into the code."""
    if event in _NOTIFICATION_EVENTS:
        subject, body = render_notification(event, data)
        return subject, body, builtin_html(event, subject, body, data)

    subject = builtin_subject(event)
    order = str(data.get("order_number") or data.get("order_id") or "")
    invoice = str(data.get("invoice_number") or data.get("invoice_id") or "")
    service = str(data.get("service_name") or "your service")
    ticket = str(data.get("ticket_number") or data.get("ticket_id") or "")
    amount = money_text(data)
    verify_url = str(data.get("verify_url") or "")
    reset_url = str(data.get("reset_url") or "")
    invite_url = str(data.get("invite_url") or "")

    bodies = {
        "welcome": (
            f"Welcome to Rackflow, {data.get('username', 'there')}! "
            "Your account is ready."
        ),
        "verify_email": (
            "Please verify your email address to activate your account."
            + (f" Open this link: {verify_url}" if verify_url else "")
        ),
        "password_reset": (
            "We received a request to reset your password."
            + (f" Use this link: {reset_url}" if reset_url else "")
        ),
        "user_invite": (
            "You have been invited to join Rackflow. Accept the invitation to choose your username and password."
            + (f" Open this link: {invite_url}" if invite_url else "")
            + " The link expires in 7 days."
        ),
        "order_confirmation": (
            f"Thank you for your order {order}. We received your request and "
            "will notify you when provisioning completes."
        ),
        "invoice_created": f"Invoice {invoice} for {amount} is now available in your account.",
        "invoice_paid": f"Payment of {amount} for invoice {invoice} was received. Thank you.",
        "invoice_overdue": f"Invoice {invoice} for {amount} is overdue. Please sign in to pay.",
        "payment_failed": (
            f"A payment attempt for invoice {invoice} ({amount}) failed. "
            "Please update your payment method or try again."
        ),
        "service_provisioned": f"{service} has been provisioned and is ready to use.",
        "ticket_reply": f"There is a new reply on support ticket {ticket}.",
        "invoice_reminder": (
            f"Invoice {invoice} for {amount} is due on {data.get('due_date') or 'the due date'}. "
            "Please sign in to pay."
        ),
        "late_fee_added": (
            f"A late fee of {money_text({'amount_cents': data.get('fee_cents'), 'currency': data.get('currency')})} "
            f"was added to invoice {invoice}, which is overdue. Please sign in to pay."
        ),
        "service_termination_warning": (
            f"{service} is suspended for non-payment and will be terminated on "
            f"{data.get('terminate_date') or 'the date shown in your account'} "
            f"unless invoice {invoice} is paid. Please sign in to pay."
        ),
        "service_suspended_nonpayment": (
            f"{service} was suspended because invoice {invoice} remains unpaid. "
            "Sign in to the client area to pay it and restore the service."
        ),
        "service_terminated": f"{service} was terminated for non-payment.",
        "cancellation_requested": (
            f"We received your request to cancel {service}. "
            f"It takes effect on {data.get('effective_date') or 'the date shown in your account'}."
        ),
        "cancellation_completed": f"{service} has been cancelled.",
        "tax_id_unverified": EVENTS["tax_id_unverified"].default_body,
    }
    body = bodies.get(event, "There is an update to your Rackflow account.")
    return subject, body, builtin_html(event, subject, body, data)
