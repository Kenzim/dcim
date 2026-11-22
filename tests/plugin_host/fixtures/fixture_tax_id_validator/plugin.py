"""A tax_id_validator for the host tests. The `mode` setting decides what check_online does."""

import time
from datetime import datetime, timezone

from rackflow_plugin.kinds import TaxIdCheckResult, current_context


class Unprintable(Exception):
    """An exception that raises ``error`` when anything tries to show it."""

    def __init__(self, error):
        super().__init__()
        self.error = error

    def __str__(self):
        raise self.error("no message for you")

    __repr__ = __str__


def _now():
    return datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


def register(plugin):
    @plugin.provider("tax_id_validator", "fixture")
    class Validator:
        def supports(self, country, id_type):
            return id_type == "eu_vat" and country in ("DE", "FR")

        def check_format(self, country, tax_id):
            return tax_id.isdigit()

        def check_online(self, request):
            ctx = current_context()
            mode = ctx.settings.get("mode")
            if mode == "raise":
                raise RuntimeError("the registry exploded")
            if mode == "sysexit":
                raise SystemExit(3)
            if mode == "unprintable_value_error":
                raise Unprintable(ValueError)
            if mode == "unprintable_interrupt":
                raise Unprintable(KeyboardInterrupt)
            if mode == "hang":
                time.sleep(3)
            if mode == "bad_status":
                return TaxIdCheckResult(status="maybe", checked_at=_now())
            if mode == "no_error_code":
                return TaxIdCheckResult(status="unavailable", checked_at=_now())
            if mode == "not_a_result":
                return {"status": "valid"}
            if mode == "async":
                return self._later(request)
            if mode == "unavailable":
                return TaxIdCheckResult(status="unavailable", checked_at=_now(), error_code="MS_UNAVAILABLE")
            if mode == "invalid":
                return TaxIdCheckResult(status="invalid", checked_at=_now(), error_code="INVALID_INPUT")
            ctx.store.set("last_country", request.country)
            return TaxIdCheckResult(
                status="valid",
                checked_at=_now(),
                request_ref="REF-1",
                registered_name="Example GmbH",
                registered_address="1 Example Strasse, Berlin",
            )

        async def _later(self, request):
            return TaxIdCheckResult(status="valid", checked_at=_now(), request_ref="ASYNC")
