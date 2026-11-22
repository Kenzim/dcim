"""Billing phase 2 plugin catalogue entries (docs/billing-phase2-design.md 8.3 and 8.8)."""

from __future__ import annotations

from app.plugin_host import catalogue


def test_api_version_is_1_1():
    assert catalogue.API_VERSION == "1.1"


def test_tax_permissions():
    expected = {
        "tax.validate_id": ("Validate tax IDs", "medium"),
        "tax.rule_pack": ("Offer tax rule packs", "medium"),
        "tax.calculate": ("Calculate tax", "high"),
    }
    for key, (label, risk) in expected.items():
        permission = catalogue.PERMISSIONS[key]
        assert (permission.label, permission.risk) == (label, risk)
        assert permission.description


def test_phase_2_events():
    expected = {
        "tax_id.checked": ("billing_account_id", "country", "status", "validator", "checked_at"),
        "invoice.reminder_sent": ("invoice_id", "billing_account_id", "reseller_id", "kind"),
        "service.termination_scheduled": ("service_id", "terminate_at", "reason"),
        "service.cancellation_requested": ("service_id", "request_id", "type", "effective_at"),
    }
    for name, required in expected.items():
        spec = catalogue.EVENTS[name]
        assert spec.permission == "billing.read"
        assert spec.required == required
        assert spec.optional == ()
        assert not spec.before and not spec.service_scoped
    # The tax ID itself is never in the payload.
    assert not {"tax_id", "number", "normalised_id"} & set(catalogue.EVENTS["tax_id.checked"].required)
