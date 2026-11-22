"""Sample event payloads for plugin tests.

``sample_payload`` returns a complete payload for any event in the catalogue, so a test only
names the fields it cares about. Fields that identify a service come from a real service
when one is given.
"""

from __future__ import annotations

from typing import Any, Optional

from app.models.service import Service, ServiceType
from app.plugin_host.catalogue import EVENTS

_SAMPLE_FIELDS: dict[str, Any] = {
    "order_id": 501,
    "order_number": "ORD-501",
    "billing_account_id": 7,
    "department_id": 1,
    "total_cents": 1999,
    "due_cents": 1999,
    "refunded_cents": 1999,
    "amount_cents": 1999,
    "balance_cents": 5000,
    "currency": "GBP",
    "invoice_id": 301,
    "invoice_number": "INV-301",
    "number_display": "INV-301",
    "document_type": "invoice",
    "purpose": "order",
    "due_at": "2026-12-01T00:00:00Z",
    "reason": "Sample reason",
    "credit_note_id": 41,
    "debit_note_id": 42,
    "refund_id": 61,
    "payment_id": 71,
    "destination": "original",
    "gateway": "stripe",
    "gateway_refund_ref": "re_sample",
    "outcome": "credited",
    "ledger_entry_id": 81,
    "entry_type": "credit",
    "ticket_id": 901,
    "ticket_number": "TCK-901",
    "subject": "Sample ticket",
    "reseller_id": None,
    "provisioning_source": "internal",
    "product_code": "vm-small",
    "os_code": "debian-12",
    "previous_status": "pending",
    "status": "active",
    "action": "reboot",
    "source": "admin",
    "actor_user_id": None,
}


def _type_value(service: Service) -> str:
    kind = service.service_type
    return kind.value if isinstance(kind, ServiceType) else str(kind)


def _status_value(service: Service) -> str:
    status = service.status
    return getattr(status, "value", str(status))


def sample_payload(name: str, service: Optional[Service] = None, **overrides: Any) -> dict[str, Any]:
    """A payload with every required and optional field of event ``name``.

    Service identity fields (``service_id``, ``service_type``, ``owner_user_id``, and
    ``status`` or ``product_code`` where the event has them) come from ``service`` when given.
    ``overrides`` win over everything. Raises ``KeyError`` for an event that does not exist.
    """
    spec = EVENTS[name]
    payload: dict[str, Any] = {}
    for key in spec.required + spec.optional:
        payload[key] = _SAMPLE_FIELDS.get(key)
    if service is not None:
        payload.update(
            service_id=service.id,
            service_type=_type_value(service),
            owner_user_id=service.owner_user_id,
        )
        if "status" in spec.required:
            payload["status"] = _status_value(service)
        if "product_code" in spec.required:
            payload["product_code"] = service.product_code
        if "os_code" in spec.required:
            payload["os_code"] = service.os_code
    fields = spec.required + spec.optional
    unknown = sorted(set(overrides) - set(fields))
    if unknown:
        raise KeyError(f"{name} has no field {', '.join(unknown)}; its fields are {', '.join(fields)}")
    payload.update(overrides)
    return {k: v for k, v in payload.items() if k in fields}
