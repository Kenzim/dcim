"""Public catalogue of the plugin API: version, permissions and events (docs/plugin-system.md sections 6 and 7)."""

from __future__ import annotations

from dataclasses import dataclass, field

API_VERSION = "1.1"
# Every plugin API version this Rackflow runs, oldest first. A plugin is
# compatible when its ``rackflow_api`` range allows at least one of them.
SUPPORTED_API_VERSIONS = ("1.0", "1.1")


@dataclass(frozen=True)
class Permission:
    key: str
    label: str
    description: str
    risk: str

    def as_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "description": self.description, "risk": self.risk}


PERMISSION_LIST = (
    Permission("service.read", "Read services", "Read basic facts of the services it is active for and receive service events.", "low"),
    Permission(
        "billing.read",
        "Billing events",
        "Receive billing events: orders and invoices paid, invoices created, overdue, voided and refunded, "
        "credit and debit notes issued, payments refunded, account credit added or applied, invoice "
        "reminders sent, tax ID check results, service terminations scheduled and cancellations requested.",
        "low",
    ),
    Permission("support.read", "Support events", "Receive ticket created events.", "low"),
    Permission("events.veto", "Refuse operations", "Run checks before operations such as power actions and refuse them.", "medium"),
    Permission("notify.client", "Notify clients", "Send notices to the owners of services it is active for.", "medium"),
    Permission("http.outbound", "Outbound HTTP", "Make HTTP requests to public addresses.", "medium"),
    Permission("ui.client", "Client interface", "Show panels and buttons to clients in the portal, WHMCS and client MCP.", "medium"),
    Permission("mcp.staff", "Staff MCP tools", "Offer tools to staff MCP keys.", "medium"),
    Permission("mcp.client", "Client MCP tools", "Offer tools to client MCP keys.", "medium"),
    Permission("vm.power", "VM power", "Start, shut down, stop, reboot or reset VMs it is active for.", "high"),
    Permission("guest.exec", "Run commands in VMs", "Run commands inside VMs it is active for through the QEMU guest agent.", "high"),
    # Plugin API 1.1 provider kinds (docs/billing-phase2-design.md 8.3).
    Permission(
        "tax.validate_id",
        "Validate tax IDs",
        "Receive customers' tax identification numbers and countries to check them.",
        "medium",
    ),
    Permission("tax.rule_pack", "Offer tax rule packs", "Offer tax rules that an administrator may review and apply.", "medium"),
    Permission(
        "tax.calculate",
        "Calculate tax",
        "Receive basket lines, amounts and the buyer's address and tax ID, and decide the tax on invoices.",
        "high",
    ),
)
PERMISSIONS = {p.key: p for p in PERMISSION_LIST}


def describe_permissions(keys) -> list[dict]:
    out = []
    for key in sorted(set(keys or [])):
        perm = PERMISSIONS.get(key)
        out.append(perm.as_dict() if perm else {"key": key, "label": key, "description": "", "risk": "high"})
    return out


@dataclass(frozen=True)
class EventSpec:
    name: str
    permission: str
    required: tuple[str, ...]
    optional: tuple[str, ...] = ()
    service_scoped: bool = False
    before: bool = False
    mutable: tuple[str, ...] = field(default_factory=tuple)


_SERVICE_FIELDS = ("service_id", "service_type", "owner_user_id")

EVENT_LIST = (
    EventSpec(
        "service.created",
        "service.read",
        ("service_id", "service_type", "status", "owner_user_id", "product_code", "os_code", "provisioning_source"),
        service_scoped=True,
    ),
    EventSpec("service.activated", "service.read", _SERVICE_FIELDS + ("previous_status",), service_scoped=True),
    EventSpec("service.suspended", "service.read", _SERVICE_FIELDS + ("previous_status",), service_scoped=True),
    EventSpec("service.unsuspended", "service.read", _SERVICE_FIELDS + ("status",), service_scoped=True),
    EventSpec("service.terminated", "service.read", _SERVICE_FIELDS + ("previous_status",), service_scoped=True),
    EventSpec(
        "service.power_requested",
        "service.read",
        ("service_id", "service_type", "action", "source", "actor_user_id"),
        service_scoped=True,
        before=True,
    ),
    EventSpec("order.paid", "billing.read", ("order_id", "order_number", "billing_account_id", "total_cents", "currency")),
    EventSpec(
        "invoice.paid",
        "billing.read",
        ("invoice_id", "billing_account_id"),
        ("invoice_number", "order_id", "reseller_id", "number_display", "total_cents", "currency", "document_type"),
    ),
    EventSpec(
        "invoice.created",
        "billing.read",
        ("invoice_id", "billing_account_id", "purpose", "total_cents", "currency"),
        ("reseller_id", "invoice_number", "number_display", "order_id", "service_id", "due_at", "document_type"),
    ),
    EventSpec(
        "invoice.overdue",
        "billing.read",
        ("invoice_id", "billing_account_id", "due_cents", "currency"),
        ("reseller_id", "invoice_number", "number_display", "due_at"),
    ),
    EventSpec(
        "invoice.voided",
        "billing.read",
        ("invoice_id", "billing_account_id", "reason"),
        ("reseller_id", "invoice_number", "number_display"),
    ),
    EventSpec(
        "invoice.refunded",
        "billing.read",
        ("invoice_id", "billing_account_id", "refunded_cents", "currency"),
        ("reseller_id", "invoice_number", "number_display"),
    ),
    EventSpec(
        "credit_note.issued",
        "billing.read",
        ("credit_note_id", "invoice_id", "billing_account_id", "total_cents", "currency", "reason", "outcome"),
        ("reseller_id", "number_display", "refund_id"),
    ),
    EventSpec(
        "debit_note.issued",
        "billing.read",
        ("debit_note_id", "invoice_id", "billing_account_id", "total_cents", "currency", "reason", "outcome"),
        ("reseller_id", "number_display"),
    ),
    EventSpec(
        "payment.refunded",
        "billing.read",
        (
            "refund_id",
            "payment_id",
            "invoice_id",
            "billing_account_id",
            "amount_cents",
            "currency",
            "destination",
            "reason",
        ),
        ("reseller_id", "gateway", "gateway_refund_ref", "credit_note_id"),
    ),
    EventSpec(
        "credit.added",
        "billing.read",
        ("ledger_entry_id", "billing_account_id", "amount_cents", "balance_cents", "currency", "entry_type"),
        ("reseller_id", "invoice_id", "refund_id"),
    ),
    EventSpec(
        "credit.applied",
        "billing.read",
        ("ledger_entry_id", "billing_account_id", "invoice_id", "amount_cents", "balance_cents", "currency"),
        ("reseller_id", "payment_id"),
    ),
    # Billing phase 2 (docs/billing-phase2-design.md 8.8).
    EventSpec(
        "tax_id.checked",
        "billing.read",
        ("billing_account_id", "country", "status", "validator", "checked_at"),
    ),
    EventSpec(
        "invoice.reminder_sent",
        "billing.read",
        ("invoice_id", "billing_account_id", "reseller_id", "kind"),
    ),
    EventSpec(
        "service.termination_scheduled",
        "billing.read",
        ("service_id", "terminate_at", "reason"),
    ),
    EventSpec(
        "service.cancellation_requested",
        "billing.read",
        ("service_id", "request_id", "type", "effective_at"),
    ),
    EventSpec(
        "ticket.created",
        "support.read",
        ("ticket_id", "ticket_number", "billing_account_id", "department_id", "subject"),
    ),
)
EVENTS = {e.name: e for e in EVENT_LIST}

POWER_ACTIONS = ("on", "off", "shutdown", "reboot", "reset")
POWER_SOURCES = ("admin", "client", "billing", "reseller", "mcp", "client_mcp", "console", "plugin")
