"""A3 daily shadow scan, shadow_since, switch criterion and credit detectors (design 6.4, 6.5, 10.4)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.dao.commerce_dao import BillingAccountDAO
from app.models.billing_shadow import BillingShadowMismatch
from app.models.commerce_account import SystemSetting
from app.models.reseller import CreditLedgerEntry, CreditLedgerEntryType, Reseller
from app.models.user import User
from app.services import billing_shadow
from app.services.alerting import billing_integrity
from app.services.billing_shadow import HISTORY_LIMIT, SCAN_RESULT_KEY, BillingShadowService
from app.services.credit_ledger_service import CreditLedgerService
from app.services.settings_store import set_setting
from tests.conftest import TestingSessionLocal
from tests.services.test_billing_p1c_ledger import set_credit_mode

NOW = datetime(2026, 11, 21, 3, 0, tzinfo=timezone.utc)
PARAMS = {"max_rows": 50}


@pytest.fixture(autouse=True)
def _quiet_c1(monkeypatch):
    # Ledger writes in these tests are set-up; their c1 records are not under test.
    real = BillingShadowService.record

    def record(*args, session_factory=None, **kwargs):
        if session_factory is None:
            return None
        return real(*args, session_factory=session_factory, **kwargs)

    monkeypatch.setattr(BillingShadowService, "record", staticmethod(record))


def _reseller(db, name: str, cents: int = 0) -> Reseller:
    reseller = Reseller(user=User(username=f"{name}-s", email=f"{name}-s@example.com"))
    db.add(reseller)
    db.commit()
    if cents:
        CreditLedgerService.credit(db, reseller.id, cents)
    return reseller


def _scan(db, **kwargs):
    result = BillingShadowService.scan(db, NOW, session_factory=TestingSessionLocal, **kwargs)
    db.commit()
    return result


def _keys(result, engine="credit"):
    return sorted(finding["check_key"] for finding in result["engines"][engine].get("findings", []))


def _open_rows(db, engine="credit"):
    db.expire_all()
    return list(
        db.scalars(
            select(BillingShadowMismatch).where(
                BillingShadowMismatch.engine == engine, BillingShadowMismatch.resolved_at.is_(None)
            )
        )
    )


def test_clean_chains_give_no_findings(db_session):
    set_credit_mode(db_session, "shadow")
    reseller = _reseller(db_session, "clean", 1_000)
    CreditLedgerService.debit(db_session, reseller.id, 300)
    result = _scan(db_session)
    assert result["engines"]["credit"]["subjects_checked"] == 1
    assert result["engines"]["credit"]["mismatches_found"] == 0
    assert BillingShadowService.last_scan(db_session, "credit")["mismatches_found"] == 0


def test_legacy_engine_is_not_scanned(db_session):
    set_credit_mode(db_session, "legacy")
    assert _scan(db_session)["engines"]["credit"] == {"skipped": "legacy"}


def test_each_credit_check_detects_its_drift(db_session):
    set_credit_mode(db_session, "shadow")
    cache = _reseller(db_session, "cache", 500)
    chain = _reseller(db_session, "chain", 500)
    CreditLedgerService.debit(db_session, chain.id, 100)
    mirror = _reseller(db_session, "mirror", 500)
    unlinked = _reseller(db_session, "unlinked", 500)

    cache.cached_balance_cents = 450  # reseller cache off its chain
    BillingAccountDAO.get_by_reseller_id(db_session, cache.id).credit_balance_cents = 450
    first = db_session.scalar(
        select(CreditLedgerEntry).where(CreditLedgerEntry.reseller_id == chain.id).order_by(CreditLedgerEntry.id)
    )
    first.balance_after_cents = 499  # chain broken at the first entry
    BillingAccountDAO.get_by_reseller_id(db_session, mirror.id).credit_balance_cents = 1  # mirror differs
    # An old process wrote an entry without the account link.
    unlinked.cached_balance_cents += 50
    db_session.add(CreditLedgerEntry(
        reseller_id=unlinked.id, entry_type=CreditLedgerEntryType.TOPUP, amount_cents=50,
        balance_after_cents=unlinked.cached_balance_cents, ledger_metadata={},
    ))
    db_session.commit()

    result = _scan(db_session)
    findings = result["engines"]["credit"]["findings"]
    by_subject = {(f["check_key"], f["subject_id"]) for f in findings}
    cache_account = BillingAccountDAO.get_by_reseller_id(db_session, cache.id).id
    chain_account = BillingAccountDAO.get_by_reseller_id(db_session, chain.id).id
    mirror_account = BillingAccountDAO.get_by_reseller_id(db_session, mirror.id).id
    assert ("c2_cache_vs_chain", cache_account) in by_subject
    assert ("c2_cache_vs_sum", cache_account) in by_subject
    assert ("c2_chain_continuity", chain_account) in by_subject
    assert ("c2_mirror", mirror_account) in by_subject
    assert ("c2_unlinked", unlinked.id) in by_subject
    unlinked_account = BillingAccountDAO.get_by_reseller_id(db_session, unlinked.id).id
    assert ("c2_mirror", unlinked_account) in by_subject
    rows = _open_rows(db_session)
    assert {(row.check_key, row.subject_id) for row in rows} == by_subject


def test_dry_run_records_and_stores_nothing(db_session):
    set_credit_mode(db_session, "shadow")
    reseller = _reseller(db_session, "dry", 500)
    reseller.cached_balance_cents = 1
    db_session.commit()
    result = _scan(db_session, dry_run=True, scheduled=True)
    assert "c2_cache_vs_sum" in _keys(result)
    assert _open_rows(db_session) == []
    assert db_session.get(SystemSetting, SCAN_RESULT_KEY) is None
    assert billing_shadow.shadow_since(db_session, "credit") is None


def test_history_is_capped(db_session):
    set_credit_mode(db_session, "shadow")
    for _ in range(HISTORY_LIMIT + 3):
        _scan(db_session)
    assert len(BillingShadowService.scan_history(db_session, "credit")) == HISTORY_LIMIT


def test_shadow_since_set_only_by_a_scheduled_real_scan(db_session):
    set_credit_mode(db_session, "shadow")
    billing_shadow.evaluate_switch(db_session, "credit", NOW)
    _scan(db_session)
    assert billing_shadow.shadow_since(db_session, "credit") is None
    _scan(db_session, scheduled=True)
    assert billing_shadow.shadow_since(db_session, "credit") == NOW
    BillingShadowService.scan(db_session, NOW + timedelta(days=1), scheduled=True)
    assert billing_shadow.shadow_since(db_session, "credit") == NOW
    record = billing_shadow.switch_record(db_session, "credit")
    assert record["shadow_since_set_by"] == "scheduled_shadow_scan"


def _seed_quiet_history(db, engine: str, days: int, now: datetime) -> None:
    since = now - timedelta(days=days, hours=1)
    billing_shadow.set_shadow_since(db, engine, at=since, by="test")
    for day in range(days):
        started = since + timedelta(days=day, minutes=30)
        billing_shadow._append_history(db, engine, {
            "started_at": started.isoformat(), "finished_at": (started + timedelta(minutes=1)).isoformat(),
            "subjects_checked": 1, "mismatches_found": 0, "scheduled": True, "complete": True,
        })
    BillingShadowService.store_scan_result(
        db, engine, started_at=now - timedelta(hours=2), finished_at=now - timedelta(hours=1),
        subjects_checked=1, mismatches_found=0,
    )
    db.commit()


def _failing(db, engine="credit", now=NOW, **kwargs):
    return [item["item"] for item in billing_shadow.evaluate_switch(db, engine, now, **kwargs) if not item["ok"]]


def test_switch_criterion_passes_after_a_clean_quiet_period(db_session):
    set_credit_mode(db_session, "shadow")
    _reseller(db_session, "quiet", 500)
    assert "quiet_period" in _failing(db_session)
    _seed_quiet_history(db_session, "credit", 14, NOW)
    assert _failing(db_session) == []


def test_switch_criterion_names_each_failing_item(db_session):
    set_credit_mode(db_session, "shadow")
    reseller = _reseller(db_session, "fail", 500)
    _seed_quiet_history(db_session, "credit", 14, NOW)
    # A reseller without a billing account.
    db_session.add(Reseller(user=User(username="noacct", email="noacct@example.com")))
    # An unlinked entry and a differing reseller account cache.
    db_session.add(CreditLedgerEntry(
        reseller_id=reseller.id, entry_type=CreditLedgerEntryType.TOPUP, amount_cents=10,
        balance_after_cents=510, ledger_metadata={},
    ))
    reseller.cached_balance_cents = 510
    db_session.commit()
    failing = _failing(db_session)
    assert {"no_unlinked_entries", "every_reseller_has_account", "reseller_caches_match"} <= set(failing)


def test_switch_criterion_open_rows_and_recent_mismatch_restart_the_clock(db_session):
    set_credit_mode(db_session, "shadow")
    _seed_quiet_history(db_session, "credit", 14, NOW)
    BillingShadowService.record("credit", "c1_cache_pair", "billing_account", 1, 1, 2,
                                session_factory=TestingSessionLocal, now=NOW - timedelta(days=2))
    db_session.expire_all()
    failing = _failing(db_session)
    assert "no_open_mismatches" in failing and "quiet_period" in failing and "daily_scans" in failing


def test_switch_criterion_stale_or_dirty_latest_scan(db_session):
    set_credit_mode(db_session, "shadow")
    _seed_quiet_history(db_session, "credit", 14, NOW)
    assert "daily_scans" in _failing(db_session, now=NOW + timedelta(hours=30))
    BillingShadowService.store_scan_result(db_session, "credit", started_at=NOW, finished_at=NOW,
                                           subjects_checked=1, mismatches_found=2)
    assert "daily_scans" in _failing(db_session)


def test_invoice_switch_needs_credit_v2_or_confirmation(db_session):
    set_setting(db_session, "billing_invoice_engine", "shadow")
    set_credit_mode(db_session, "shadow")
    _seed_quiet_history(db_session, "invoice", 14, NOW)
    assert _failing(db_session, "invoice") == ["credit_engine_v2_or_confirmed"]
    assert _failing(db_session, "invoice", confirm_overpayments_to_reseller_cache=True) == []


# --- detectors ---


def test_shadow_mismatch_detector(db_session):
    assert billing_integrity.shadow_mismatch(db_session, PARAMS, NOW) == []
    BillingShadowService.record("credit", "c2_mirror", "billing_account", 3, 1, 2,
                                session_factory=TestingSessionLocal, now=NOW)
    db_session.expire_all()
    findings = billing_integrity.shadow_mismatch(db_session, PARAMS, NOW)
    assert [(f.resource_id, f.details["open_by_check"]) for f in findings] == [("credit", {"c2_mirror": 1})]


def test_shadow_scan_stale_detector(db_session):
    set_credit_mode(db_session, "shadow")
    assert billing_integrity.shadow_scan_stale(db_session, PARAMS, NOW) == []  # job disabled
    set_setting(db_session, "billing_shadow_scan_enabled", True)
    stale = billing_integrity.shadow_scan_stale(db_session, PARAMS, NOW)
    assert {f.resource_id for f in stale} >= {"shadow_scan:credit"}
    BillingShadowService.store_scan_result(db_session, "credit", started_at=NOW, finished_at=NOW,
                                           subjects_checked=0, mismatches_found=0)
    stale = billing_integrity.shadow_scan_stale(db_session, PARAMS, NOW + timedelta(hours=1))
    assert "shadow_scan:credit" not in {f.resource_id for f in stale}


def test_ledger_chain_broken_detector(db_session):
    set_credit_mode(db_session, "shadow")
    good = _reseller(db_session, "good", 300)
    bad = _reseller(db_session, "bad", 300)
    assert billing_integrity.ledger_chain_broken(db_session, PARAMS, NOW) == []
    entry = db_session.scalar(select(CreditLedgerEntry).where(CreditLedgerEntry.reseller_id == bad.id))
    entry.balance_after_cents = 301
    db_session.commit()
    findings = billing_integrity.ledger_chain_broken(db_session, PARAMS, NOW)
    bad_account = BillingAccountDAO.get_by_reseller_id(db_session, bad.id).id
    assert [f.resource_id for f in findings] == [str(bad_account)]
    assert findings[0].details["first_broken_entry_id"] == entry.id
    assert good.id != bad.id


def test_negative_retail_balance_detector(db_session):
    set_credit_mode(db_session, "shadow")
    user = User(username="neg-c", email="neg-c@example.com")
    db_session.add(user)
    db_session.flush()
    account = BillingAccountDAO.ensure_client_account(db_session, user.id)
    CreditLedgerService.credit_account(db_session, account.id, 100)
    assert billing_integrity.negative_retail_balance(db_session, PARAMS, NOW) == []
    CreditLedgerService.debit_account(db_session, account.id, 150, allow_negative_balance=True)
    db_session.commit()
    findings = billing_integrity.negative_retail_balance(db_session, PARAMS, NOW)
    assert [(f.resource_id, f.details["balance_cents"]) for f in findings] == [(str(account.id), -50)]


# --- partial refunds in i2_scan ---


def _refunded_invoice(db, name: str, *, status, refunded: int):
    from app.models.reseller import Invoice, InvoicePurpose, InvoiceStatus, Payment
    from app.services.invoice_service import InvoiceService

    reseller = _reseller(db, name)
    invoice = InvoiceService.create(db, reseller_id=reseller.id, purpose=InvoicePurpose.CYCLE_CHARGE, amount_cents=1_000)
    invoice.status = InvoiceStatus.PAID
    db.add(Payment(invoice_id=invoice.id, gateway="stripe", status=status, amount_cents=1_000,
                   refunded_cents=refunded, currency="USD", external_ref=f"pi_{name}"))
    db.commit()
    assert isinstance(invoice, Invoice)
    return invoice


def _invoice_switch_after_scan(db):
    now = datetime.now(timezone.utc)
    set_setting(db, "billing_invoice_engine", "shadow")
    set_credit_mode(db, "shadow")
    _seed_quiet_history(db, "invoice", 14, now)
    result = BillingShadowService.scan(db, now, session_factory=TestingSessionLocal)
    db.commit()
    db.expire_all()
    failing = _failing(db, "invoice", now=now + timedelta(minutes=5), confirm_overpayments_to_reseller_cache=True)
    return result["engines"]["invoice"], failing


@pytest.mark.parametrize("status_name", ["SUCCEEDED", "PARTIALLY_REFUNDED"])
def test_exact_partial_refund_difference_does_not_block_the_switch(db_session, status_name):
    from app.models.reseller import PaymentStatus

    invoice = _refunded_invoice(db_session, f"pr-{status_name.lower()}", status=PaymentStatus[status_name], refunded=200)
    summary, failing = _invoice_switch_after_scan(db_session)
    assert summary["mismatches_found"] == 0
    assert [(f["check_key"], f["subject_id"]) for f in summary["expected_differences"]] == [
        (billing_shadow.EXPECTED_PARTIAL_REFUND, invoice.id)
    ]
    assert failing == []
    assert billing_shadow.open_mismatch_counts(db_session, "invoice") == {}
    assert billing_shadow.open_mismatch_counts(db_session, "invoice", expected=True) == {
        billing_shadow.EXPECTED_PARTIAL_REFUND: 1
    }
    assert billing_integrity.shadow_mismatch(db_session, PARAMS, NOW) == []


def test_partial_refund_plus_one_cent_blocks_the_switch(db_session, monkeypatch):
    import dataclasses

    from app.models.reseller import PaymentStatus
    from app.services.invoice_service import InvoiceService

    invoice = _refunded_invoice(db_session, "pr-extra", status=PaymentStatus.SUCCEEDED, refunded=200)
    real = InvoiceService.balance

    def off_by_one(db, target):
        value = real(db, target)
        return dataclasses.replace(value, due_cents=value.due_cents + 1, remaining_cents=value.remaining_cents + 1)

    monkeypatch.setattr(InvoiceService, "balance", staticmethod(off_by_one))
    summary, failing = _invoice_switch_after_scan(db_session)
    assert [(f["check_key"], f["subject_id"]) for f in summary["findings"]] == [("i2_scan", invoice.id)]
    assert summary["expected_differences"] == []
    assert {"no_open_mismatches", "daily_scans", "quiet_period"} <= set(failing)


def test_on_demand_scans_do_not_satisfy_the_daily_scan_criterion(db_session):
    set_credit_mode(db_session, "shadow")
    _reseller(db_session, "ondemand", 500)
    billing_shadow.set_shadow_since(db_session, "credit", at=NOW - timedelta(days=15), by="test")
    db_session.commit()
    for _ in range(14):
        _scan(db_session)
    assert len(BillingShadowService.scan_history(db_session, "credit")) == 14
    assert "daily_scans" in _failing(db_session)


def test_a_missing_day_of_scheduled_scans_fails_the_daily_scan_criterion(db_session):
    set_credit_mode(db_session, "shadow")
    _reseller(db_session, "gap", 500)
    since = NOW - timedelta(days=15, hours=1)
    billing_shadow.set_shadow_since(db_session, "credit", at=since, by="test")
    for day in range(15):
        if day == 6:
            continue
        started = since + timedelta(days=day, hours=2)
        billing_shadow._append_history(db_session, "credit", {
            "started_at": started.isoformat(), "finished_at": (started + timedelta(minutes=1)).isoformat(),
            "subjects_checked": 1, "mismatches_found": 0, "scheduled": True, "complete": True,
        })
    db_session.commit()
    items = {item["item"]: item for item in billing_shadow.evaluate_switch(db_session, "credit", NOW)}
    assert not items["daily_scans"]["ok"]
    assert items["daily_scans"]["days_missing"] == [(since + timedelta(days=6, hours=2)).date().isoformat()]


def test_scheduled_scans_with_mismatches_or_old_entries_do_not_count(db_session):
    set_credit_mode(db_session, "shadow")
    _reseller(db_session, "dirty", 500)
    since = NOW - timedelta(days=15, hours=1)
    billing_shadow.set_shadow_since(db_session, "credit", at=since, by="test")
    for day in range(15):
        started = since + timedelta(days=day, hours=2)
        entry = {
            "started_at": started.isoformat(), "finished_at": (started + timedelta(minutes=1)).isoformat(),
            "subjects_checked": 1, "mismatches_found": 0, "scheduled": True, "complete": True,
        }
        if day == 3:
            entry["mismatches_found"] = 2
        if day == 5:
            # Written before entries recorded whether the daily job ran them.
            del entry["scheduled"], entry["complete"]
        billing_shadow._append_history(db_session, "credit", entry)
    db_session.commit()
    items = {item["item"]: item for item in billing_shadow.evaluate_switch(db_session, "credit", NOW)}
    assert not items["daily_scans"]["ok"]
    assert len(items["daily_scans"]["days_missing"]) == 2


def test_admin_scans_do_not_push_scheduled_scans_out_of_the_history(db_session):
    for index in range(HISTORY_LIMIT):
        billing_shadow._append_history(db_session, "credit", {"started_at": str(index), "scheduled": True})
    for index in range(5):
        billing_shadow._append_history(db_session, "credit", {"started_at": f"admin{index}", "scheduled": False})
    history = BillingShadowService.scan_history(db_session, "credit")
    assert len(history) == HISTORY_LIMIT
    # Each admin scan is kept until the next one replaces it; no scheduled scan goes.
    assert sum(1 for entry in history if entry["scheduled"]) == HISTORY_LIMIT - 1
    assert history[0]["started_at"] == "1" and history[-1]["started_at"] == "admin4"


def test_scan_records_scheduled_and_complete(db_session):
    set_credit_mode(db_session, "shadow")
    _reseller(db_session, "flags", 500)
    _scan(db_session)
    _scan(db_session, scheduled=True)
    history = BillingShadowService.scan_history(db_session, "credit")
    assert [(entry["scheduled"], entry["complete"]) for entry in history[-2:]] == [(False, True), (True, True)]
