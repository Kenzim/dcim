"""Errors of tax at invoice create and publish that other billing code raises and catches."""

from __future__ import annotations

from typing import Optional


class RetryableTaxPublishError(RuntimeError):
    """Tax could not be frozen on an invoice; nothing was written and the caller may retry.

    The caller rolls back and runs its whole transaction again, which reads
    the tax data afresh. Recalculating in the same transaction would read the
    same old snapshot on MariaDB (REPEATABLE READ). Checkout answers with
    ``message``, the invoice-issuing routes with 409 and ``detail``, and the
    recurring worker leaves the billing for its next pass. Subclasses set
    ``code`` and ``message``.
    """

    code = "tax_publish_retry"
    message = "Tax could not be calculated while this invoice was being issued; please try again"

    def __init__(self, reason: Optional[str] = None) -> None:
        super().__init__(reason or self.message)

    @property
    def detail(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}
