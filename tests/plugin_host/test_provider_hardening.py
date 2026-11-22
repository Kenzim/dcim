"""Provider call hardening: isolation between plugins, exact answer types, the circuit and the rate limit
(docs/plugin-system.md section 18.3)."""

from __future__ import annotations

from tests.plugin_host.helpers import base_manifest, install, write_plugin
from tests.plugin_host.test_provider_kinds import (  # noqa: F401 - shared helpers of the provider tests
    TAX,
    VALIDATOR,
    failures,
    setup_plugin,
    tax_call,
    tax_request,
    validator_call,
)

import pytest

from app.plugin_host import kinds
from app.plugin_host.providers import ProviderUnavailable, call_provider

SAME_ID_CODE = """
from datetime import datetime, timezone

from rackflow_plugin.kinds import TaxIdCheckResult, TaxProviderPart, TaxProviderResult


class Validator:
    def supports(self, country, id_type):
        return True

    def check_format(self, country, tax_id):
        return True

    def check_online(self, request):
        return TaxIdCheckResult(status="valid", checked_at=datetime(2026, 11, 22, tzinfo=timezone.utc))


class Tax:
    def supports(self, request):
        return True

    def calculate(self, request):
        return TaxProviderResult(
            lines={line.key: (TaxProviderPart("Zero", "0.0000", "XX", line.net_cents, 0),) for line in request.lines}
        )


def register(plugin):
    plugin.provider("tax_id_validator", "same")(Validator)
    plugin.provider("tax_provider", "same")(Tax)
"""


def test_two_kinds_with_the_same_provider_id_have_separate_rate_limits(plugin_env, db_session):
    limit = {"calls": 1, "per_seconds": 3600}
    manifest = base_manifest(
        rackflow_api=">=1.1,<2",
        permissions=["tax.validate_id", "tax.calculate"],
        provides=[
            {"kind": "tax_id_validator", "id": "same", "label": "V", "id_types": ["eu_vat"], "rate_limit": limit},
            {"kind": "tax_provider", "id": "same", "label": "T", "rate_limit": limit},
        ],
    )
    write_plugin(plugin_env, manifest, SAME_ID_CODE)
    install(db_session, "demo_plugin")
    request = kinds.TaxIdCheckRequest("DE", "1", "eu_vat")
    assert call_provider("tax_id_validator", "demo_plugin:same", "check_online", request).status == "valid"
    assert set(call_provider("tax_provider", "demo_plugin:same", "calculate", tax_request()).lines) == {"a", "b"}
    with pytest.raises(ProviderUnavailable) as err:
        call_provider("tax_provider", "demo_plugin:same", "calculate", tax_request())
    assert err.value.reason == "rate_limited"
