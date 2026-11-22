"""Billing phase 2 settings (docs/billing-phase2-design.md appendix A) and the tax engine mode."""

from __future__ import annotations

import pytest

from app.models.commerce_account import SystemSetting
from app.services import settings_store
from app.services.billing_shadow import ENGINES, SWITCH_RECORD_PREFIX, billing_engine_mode
from app.services.settings_store import SettingValidationError, get_setting, set_setting

BASE = "/api/admin/settings"

DEFAULTS = {
    "billing_tax_engine": "shadow",
    "billing_tax_enabled": False,
    "billing_tax_seller_country": "",
    "billing_tax_prices_include_tax": False,
    "billing_tax_retail_renewals": True,
    "billing_tax_resellers_enabled": False,
    "billing_tax_provider": "",
    "billing_tax_id_validators": "",
    "billing_tax_id_unavailable_policy": "charge_tax",
    "billing_tax_id_check_timeout_seconds": 8,
    "billing_tax_id_valid_days": 30,
    "billing_tax_id_max_age_days": 30,
    "billing_tax_id_recheck_batch": 200,
    "billing_tax_ip_country_header": "",
    "billing_automation_time_utc": "09:00",
    "billing_job_lease_seconds": 600,
    "billing_job_max_catch_up_days": 3,
    "billing_job_min_dry_runs": 1,
    "billing_job_stuck_minutes": 60,
    "billing_job_run_retention_days": 90,
    "billing_job_tracks_generate_invoices_ahead": "both",
    "billing_job_tracks_invoice_reminders": "both",
    "billing_job_tracks_late_fees": "both",
    "billing_job_tracks_nonpayment_termination": "both",
    "billing_job_tracks_cancellations": "both",
    "billing_invoice_lead_days": 0,
    "billing_reminder_upcoming_days": 3,
    "billing_reminder_overdue_days": "1,7,14",
    "billing_late_fee_type": "none",
    "billing_late_fee_fixed_cents": 0,
    "billing_late_fee_currency": "USD",
    "billing_late_fee_percent_bp": 0,
    "billing_late_fee_min_cents": 0,
    "billing_late_fee_max_cents": 0,
    "billing_late_fee_days": 14,
    "billing_late_fee_min_balance_cents": 1,
}

RANGES = {
    "billing_tax_id_check_timeout_seconds": (1, 30),
    "billing_tax_id_valid_days": (1, 365),
    "billing_tax_id_max_age_days": (1, 365),
    "billing_tax_id_recheck_batch": (1, 5_000),
    "billing_job_lease_seconds": (60, 3_600),
    "billing_job_max_catch_up_days": (1, 30),
    "billing_job_min_dry_runs": (0, 30),
    "billing_job_stuck_minutes": (5, 1_440),
    "billing_job_run_retention_days": (7, 365),
    "billing_invoice_lead_days": (0, 60),
    "billing_reminder_upcoming_days": (0, 30),
    "billing_late_fee_fixed_cents": (0, 1_000_000_000),
    "billing_late_fee_percent_bp": (0, 10_000),
    "billing_late_fee_min_cents": (0, 1_000_000_000),
    "billing_late_fee_max_cents": (0, 1_000_000_000),
    "billing_late_fee_days": (1, 365),
    "billing_late_fee_min_balance_cents": (1, 1_000_000_000),
}


def _admin(client):
    resp = client.post("/api/users/login", json={"username": "admin", "password": "adminpassword123"})
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def test_every_setting_has_its_default_and_group(db_session):
    for key, default in DEFAULTS.items():
        definition = settings_store.SCHEMA[key]
        assert definition.group in {"billing_tax", "billing_automation", "billing_email"}, key
        assert get_setting(db_session, key) == default, key
    assert {"billing_tax", "billing_automation", "billing_email"} <= set(settings_store.GROUPS)


@pytest.mark.parametrize("key", sorted(RANGES))
def test_integer_ranges(db_session, key):
    low, high = RANGES[key]
    assert set_setting(db_session, key, low) == low
    assert set_setting(db_session, key, high) == high
    for bad in (low - 1, high + 1):
        with pytest.raises(SettingValidationError):
            set_setting(db_session, key, bad)
    db_session.rollback()


@pytest.mark.parametrize(
    "key, good, normalised, bad",
    [
        ("billing_tax_engine", "legacy", "legacy", ["v3", ""]),
        ("billing_tax_seller_country", "gb", "GB", ["GBR", "G1"]),
        ("billing_tax_seller_country", "", "", []),
        ("billing_tax_provider", "vies_validator:vies", "vies_validator:vies", ["x:y", "vies", "Vies:x"]),
        ("billing_tax_id_validators", " vies_validator:vies , other_plugin:check ", "vies_validator:vies,other_plugin:check", ["a:b", "vies_validator:vies,vies_validator:vies"]),
        ("billing_tax_id_unavailable_policy", "block_checkout", "block_checkout", ["refuse"]),
        ("billing_tax_ip_country_header", "CF-IPCountry", "CF-IPCountry", ["CF IPCountry", "X:Y"]),
        ("billing_automation_time_utc", "23:59", "23:59", ["24:00", "9:00", "09:60"]),
        ("billing_job_tracks_late_fees", "retail", "retail", ["neither"]),
        ("billing_reminder_overdue_days", "1, 7,14", "1,7,14", ["7,1", "1,1", "0,3", "1,2,3,4", "400", "x"]),
        ("billing_reminder_overdue_days", "", "", []),
        ("billing_late_fee_type", "percent", "percent", ["flat"]),
        ("billing_late_fee_currency", "gbp", "GBP", ["GB", "POUND", "12A"]),
    ],
)
def test_text_validation(db_session, key, good, normalised, bad):
    assert set_setting(db_session, key, good) == normalised
    for value in bad:
        with pytest.raises(SettingValidationError):
            settings_store.SCHEMA[key].validate(value)
    db_session.rollback()


def test_late_fee_minimum_must_not_exceed_a_set_maximum(db_session):
    set_setting(db_session, "billing_late_fee_min_cents", 5_000)  # no maximum: allowed
    with pytest.raises(SettingValidationError):
        set_setting(db_session, "billing_late_fee_max_cents", 4_999)
    assert set_setting(db_session, "billing_late_fee_max_cents", 5_000) == 5_000
    with pytest.raises(SettingValidationError):
        set_setting(db_session, "billing_late_fee_min_cents", 5_001)
    db_session.rollback()


def test_tax_engine_mode_honours_the_switch_record(db_session, caplog):
    assert billing_engine_mode(db_session, "tax") == "shadow"
    set_setting(db_session, "billing_tax_engine", "legacy")
    assert billing_engine_mode(db_session, "tax") == "legacy"
    set_setting(db_session, "billing_tax_engine", "v2")
    assert billing_engine_mode(db_session, "tax") == "shadow"  # no switch record
    db_session.add(SystemSetting(key=SWITCH_RECORD_PREFIX + "tax", value={"mode": "v2"}))
    db_session.flush()
    assert billing_engine_mode(db_session, "tax") == "v2"
    db_session.rollback()
    # The tax engine has its scan (t6_scan), so the scan and detectors iterate it.
    assert "tax" in ENGINES


def test_generic_settings_endpoint_refuses_the_tax_engine(client, db_session, test_admin_user):
    headers = _admin(client)
    put = client.put(f"{BASE}/billing_tax_engine", headers=headers, json={"value": "v2"})
    assert put.status_code == 409
    assert put.json()["detail"]["code"] == "setting_managed_elsewhere"
    assert put.json()["detail"]["managed_by"] == "billing_engines"
    assert client.delete(f"{BASE}/billing_tax_engine", headers=headers).status_code == 409
    items = {item["key"]: item for item in client.get(BASE, headers=headers).json()["items"]}
    assert items["billing_tax_engine"]["read_only"] is True
    assert items["billing_tax_enabled"]["read_only"] is False
    ok = client.put(f"{BASE}/billing_tax_seller_country", headers=headers, json={"value": "gb"})
    assert ok.status_code == 200 and ok.json()["value"] == "GB"
