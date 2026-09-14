"""Typed errors for the unified provisioning service."""
from __future__ import annotations

from typing import Optional

_CODE_STATUS = {
    "name_taken": 400,
    "invalid_request": 400,
    "not_found": 404,
    "no_free_server": 409,
    "no_free_ip": 409,
    "conflict": 409,
    "internal": 500,
}


class ProvisioningError(Exception):
    """Raised by ProvisioningService; API layers map ``code`` to HTTP status."""

    def __init__(self, code: str, message: str, *, status_code: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code if status_code is not None else _CODE_STATUS.get(code, 400)

    @property
    def http_status(self) -> int:
        return self.status_code
