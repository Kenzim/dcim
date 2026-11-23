"""Typed errors of the VM firewall domain layer.

Each carries a stable ``code`` and, where it concerns one input field, a ``field`` path such as
``rules[2].ports`` so the API layer can answer per field without parsing messages.
"""

from __future__ import annotations

from typing import Optional


class FirewallError(Exception):
    """Base class. ``http_status`` is the status the API layer should answer with."""

    http_status = 400
    default_code = "firewall_error"

    def __init__(self, message: str, *, code: Optional[str] = None, field: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.code = code or self.default_code
        self.field = field

    def to_dict(self) -> dict:
        return {"code": self.code, "field": self.field, "message": self.message}


class FirewallValidationError(FirewallError):
    """An input value is not acceptable (422)."""

    http_status = 422
    default_code = "invalid_value"


class FirewallChanged(FirewallError):
    """The caller's ``revision`` is stale (409 ``firewall_changed``)."""

    http_status = 409
    default_code = "firewall_changed"

    def __init__(self, current_revision: int, message: str = "The firewall was changed by someone else; reload it"):
        super().__init__(message, code="firewall_changed", field="revision")
        self.current_revision = current_revision


class FirewallNotFound(FirewallError):
    http_status = 404
    default_code = "not_found"


class FirewallForbidden(FirewallError):
    """The actor may not do this (admin-only field or object, or not the owner)."""

    http_status = 403
    default_code = "admin_only"


class FirewallConflict(FirewallError):
    """The change conflicts with existing data (in use, duplicate name)."""

    http_status = 409
    default_code = "conflict"


class FirewallNotEligible(FirewallError):
    """The service cannot have a managed firewall (section 3.1)."""

    http_status = 409
    default_code = "not_eligible"

    def __init__(self, reasons: list[str]):
        super().__init__("This service cannot use the VM firewall: " + ", ".join(reasons), code="not_eligible")
        self.reasons = list(reasons)
