"""Billing phase 2 interface stubs (docs/billing-phase2-design.md 14.3) import and refuse to run."""

from __future__ import annotations

import inspect
from datetime import datetime

import pytest

from app.plugin_host import providers
from app.services.alerting import billing_integrity, catalogue as alert_catalogue
from app.services.alerting import detectors
from app.services.billing.jobs import base as job_base
from app.models.billing_jobs import BILLING_JOB_NAMES
from app.services.billing.jobs import registry as job_registry
from app.services.email_templates import render as email_render
from app.services.invoice_service import InvoiceService
from app.services.tax import context, engine, legal_notes, mode, pack_import, result, validators
from app.services.tax.packs import registry as pack_registry

NOW = datetime(2026, 11, 22, 12, 0, 0)

STUBS = [
    lambda: validators.TaxIdService.check(None, None, force=True),
    lambda: pack_registry.PackRegistry.list(None),
    lambda: pack_registry.PackRegistry.load(None, "uk_vat", "2026.1"),
    lambda: pack_import.PackImporter.preview(None, "uk_vat", "2026.1", NOW),
    lambda: pack_import.PackImporter.apply(None, "uk_vat", "2026.1", NOW, diff_hash="x", actor_user_id=None),
    lambda: legal_notes.render_legal_note(None, "outside_scope", "en"),
]


@pytest.mark.parametrize("call", STUBS)
def test_stubs_raise_not_implemented(call):
    with pytest.raises(NotImplementedError):
        call()


def test_types_and_registry_exist():
    assert set(job_registry.JOBS) <= set(BILLING_JOB_NAMES)
    assert issubclass(providers.ProviderUnavailable, Exception)
    assert providers.ProviderUnavailable("timeout").reason == "timeout"
    assert providers.ProviderRef("vies_validator", "vies", "tax_id_validator").ref == "vies_validator:vies"
    assert pack_registry.TaxRulePackRegistry is pack_registry.PackRegistry
    for cls in (result.TaxResult, context.TaxContext, job_base.JobRunResult, email_render.RenderedEmail):
        assert inspect.isclass(cls)
    assert mode.tax_engine_mode.__name__ == "tax_engine_mode"


def test_tax_engine_mode_defaults_to_shadow(db_session):
    assert mode.tax_engine_mode(db_session) == "shadow"


def test_tax_result_keyword_is_accepted():
    for method in (InvoiceService.create, InvoiceService.create_cycle):
        parameter = inspect.signature(method).parameters["tax_result"]
        assert parameter.default is None
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


PHASE2_DETECTORS = (
    "billing_tax_line_sum",
    "billing_tax_summary_consistent",
    "billing_tax_evidence_conflict",
    "billing_tax_not_registered",
    "billing_job_needs_review",
    "billing_job_stale",
    "billing_email_template_render_failed",
)


@pytest.mark.parametrize("key", PHASE2_DETECTORS)
def test_phase2_detector_stubs_find_nothing_and_ship_disabled(db_session, key):
    spec = alert_catalogue.CATALOGUE[key]
    assert spec.enabled_by_default is False
    assert detectors.DETECTORS[key] is billing_integrity.BILLING_DETECTORS[key]
    assert detectors.DETECTORS[key](db_session, spec.defaults(), NOW) == []
