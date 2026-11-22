"""Billing job scheduler (P2-J): modes, watermark, claims, dry run, catch-up, lease (design 9 and 11)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from hypothesis import given, settings as hsettings, strategies as st
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.core.database import Base
from app.models.billing_jobs import (
    BILLING_JOB_NAMES,
    BillingJobAction,
    BillingJobRun,
    BillingJobRunItem,
    BillingJobState,
)
from app.models.commerce_account import SystemSetting
from app.models.reseller import RecurringBillingLease
from app.services.billing import periodic
from app.services.billing.jobs import registry, runner
from app.services.billing.jobs.base import JobRun, PlannedAction
from app.services.settings_store import set_setting

T0 = datetime(2027, 5, 10, 9, 30, tzinfo=timezone.utc)
JOB_TABLES = {
    "billing_job_states",
    "billing_job_runs",
    "billing_job_run_items",
    "billing_job_actions",
    "recurring_billing_leases",
}


class FakeJob:
    """Subjects are ``(id, trigger_at)``; the effect is a ``system_settings`` row."""

    name = "generate_invoices_ahead"
    setting_keys = ("billing_invoice_lead_days",)

    def __init__(self, subjects=(), on_execute=None):
        self.subjects = dict(subjects)
        self.on_execute = on_execute
        self.executed: list[int] = []

    def candidates(self, db, run):
        return sorted(self.subjects)

    def plan(self, db, run, subject_id):
        trigger = self.subjects[subject_id]
        if not run.reached(trigger):
            return None
        return PlannedAction(f"s:{subject_id}", "touch", trigger, {"id": subject_id})

    def execute(self, db, run, action):
        assert run.in_window(action.trigger_at)
        if not run.claim(db, action.subject_key):
            return "already_done"
        if self.on_execute:
            self.on_execute(db, run, action)
        db.add(SystemSetting(key=f"fx-{action.detail['id']}", value=1))
        db.flush()
        self.executed.append(action.detail["id"])
        return "done"


@pytest.fixture
def factory(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'jobs.db'}")
    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = make()
    for name in BILLING_JOB_NAMES:
        db.add(BillingJobState(job=name))
    db.commit()
    db.close()
    yield make
    engine.dispose()


@pytest.fixture
def job(monkeypatch):
    fake = FakeJob()
    monkeypatch.setattr(registry, "JOBS", {fake.name: fake})
    return fake


def _mode(factory, to, now, name="generate_invoices_ahead"):
    db = factory()
    try:
        runner.change_mode(db, name, to, now=now, actor_user_id=None)
        db.commit()
    finally:
        db.close()


def _dry_run_then_live(factory, job, *, dry_at, live_at):
    _mode(factory, "dry_run", dry_at)
    result = runner.run_job(factory, job.name, dry_run=True, trigger="schedule", now=dry_at + timedelta(minutes=1))
    assert result.status == "complete"
    _mode(factory, "live", live_at)


def _live(factory, job, now, trigger="admin"):
    return runner.run_job(factory, job.name, dry_run=False, trigger=trigger, now=now)


def _effects(factory):
    db = factory()
    try:
        return sorted(row.key for row in db.query(SystemSetting).filter(SystemSetting.key.like("fx-%")))
    finally:
        db.close()


def _items(factory, run_id):
    db = factory()
    try:
        return [
            (item.subject_key, item.outcome.value, (item.detail or {}).get("skip_reason"))
            for item in db.query(BillingJobRunItem).filter_by(run_id=run_id).order_by(BillingJobRunItem.id)
        ]
    finally:
        db.close()


# ------------------------------------------------------------------ modes


def test_every_job_ships_off_and_off_jobs_do_nothing(factory, job):
    job.subjects = {1: T0 - timedelta(hours=1)}
    db = factory()
    assert {row.job: row.mode.value for row in db.query(BillingJobState)} == {n: "off" for n in BILLING_JOB_NAMES}
    db.close()
    assert periodic.run_automation_jobs(owner="w", now=T0, session_factory=factory) == {job.name: None}
    assert _live(factory, job, T0).status == "not_live"
    assert job.executed == [] and _effects(factory) == []
    db = factory()
    assert db.query(BillingJobRun).count() == 0
    db.close()


def test_mode_rules(factory, job, monkeypatch):
    with pytest.raises(runner.JobModeError, match="only from dry_run"):
        _mode(factory, "live", T0)
    _mode(factory, "dry_run", T0)
    with pytest.raises(runner.JobModeError, match="0 completed scheduled dry runs"):
        _mode(factory, "live", T0 + timedelta(hours=1))
    # An admin dry run does not count towards going live.
    runner.run_job(factory, job.name, dry_run=True, trigger="admin", now=T0 + timedelta(minutes=5))
    with pytest.raises(runner.JobModeError, match="0 completed"):
        _mode(factory, "live", T0 + timedelta(hours=1))
    runner.run_job(factory, job.name, dry_run=True, trigger="schedule", now=T0 + timedelta(minutes=6))
    monkeypatch.setattr(job, "live_preconditions", lambda db: ["templates disabled"], raising=False)
    with pytest.raises(runner.JobModeError, match="templates disabled"):
        _mode(factory, "live", T0 + timedelta(hours=1))
    monkeypatch.setattr(job, "live_preconditions", lambda db: [], raising=False)
    _mode(factory, "live", T0 + timedelta(hours=1))
    db = factory()
    state = db.get(BillingJobState, job.name)
    assert state.mode.value == "live" and state.dry_run_since is None
    assert state.live_since.replace(tzinfo=timezone.utc) == T0 + timedelta(hours=1)
    db.close()
    _mode(factory, "off", T0 + timedelta(hours=2))
    db = factory()
    state = db.get(BillingJobState, job.name)
    assert state.mode.value == "off" and state.live_since is None and state.dry_run_since is None
    db.close()
    with pytest.raises(runner.JobModeError, match="already off"):
        _mode(factory, "off", T0 + timedelta(hours=3))


def test_live_to_dry_run_is_refused(factory, job):
    _dry_run_then_live(factory, job, dry_at=T0 - timedelta(days=1), live_at=T0)
    with pytest.raises(runner.JobModeError, match="switch it off first"):
        _mode(factory, "dry_run", T0 + timedelta(hours=1))
    db = factory()
    state = db.get(BillingJobState, job.name)
    assert state.mode.value == "live" and state.live_since is not None
    db.close()
    _mode(factory, "off", T0 + timedelta(hours=1))
    _mode(factory, "dry_run", T0 + timedelta(hours=2))


# ------------------------------------------------------------------ watermark


def test_trigger_before_live_since_never_acts_and_after_acts_once(factory, job):
    live_at = T0
    job.subjects = {1: live_at - timedelta(minutes=1), 2: live_at + timedelta(minutes=30)}
    _dry_run_then_live(factory, job, dry_at=T0 - timedelta(days=1), live_at=live_at)
    first = _live(factory, job, live_at + timedelta(hours=1))
    assert first.status == "complete" and job.executed == [2]
    assert _items(factory, first.run_id) == [("s:2", "done", None)]
    second = _live(factory, job, live_at + timedelta(hours=2))
    assert job.executed == [2] and second.counts == {"already_done": 1}
    assert _effects(factory) == ["fx-2"]
    db = factory()
    claim = db.query(BillingJobAction).one()
    assert (claim.subject_key, claim.status.value, claim.run_id) == ("s:2", "done", first.run_id)
    db.close()


def test_leaving_live_and_coming_back_never_catches_up_the_off_period(factory, job):
    _dry_run_then_live(factory, job, dry_at=T0 - timedelta(days=1), live_at=T0)
    _mode(factory, "off", T0 + timedelta(hours=1))
    job.subjects = {1: T0 + timedelta(hours=2)}
    _mode(factory, "dry_run", T0 + timedelta(hours=3))
    runner.run_job(factory, job.name, dry_run=True, trigger="schedule", now=T0 + timedelta(hours=3, minutes=10))
    _mode(factory, "live", T0 + timedelta(hours=4))
    _live(factory, job, T0 + timedelta(hours=5))
    assert job.executed == []


def test_config_change_moves_the_watermark(factory, job):
    _dry_run_then_live(factory, job, dry_at=T0 - timedelta(days=1), live_at=T0)
    db = factory()
    set_setting(db, "billing_invoice_lead_days", 7)
    db.commit()
    db.close()
    # Triggered after going live but before the run that sees the change.
    job.subjects = {1: T0 + timedelta(hours=1), 2: T0 + timedelta(hours=3, minutes=30)}
    change_seen_at = T0 + timedelta(hours=3)
    result = _live(factory, job, change_seen_at)
    assert result.counts == {} and job.executed == []
    db = factory()
    state = db.get(BillingJobState, job.name)
    assert state.config_since.replace(tzinfo=timezone.utc) == change_seen_at
    run = db.get(BillingJobRun, result.run_id)
    assert run.watermark_at.replace(tzinfo=timezone.utc) == change_seen_at
    db.close()
    _live(factory, job, T0 + timedelta(hours=4))
    assert job.executed == [2]


def test_catch_up_is_limited(factory, job):
    _dry_run_then_live(factory, job, dry_at=T0 - timedelta(days=30), live_at=T0 - timedelta(days=20))
    now = T0
    job.subjects = {1: now - timedelta(days=3, minutes=1), 2: now - timedelta(days=2, hours=23)}
    result = _live(factory, job, now)
    assert job.executed == [2]
    assert _items(factory, result.run_id) == [("s:1", "skipped", "stale"), ("s:2", "done", None)]


# ------------------------------------------------------------------ dry run


def _row_counts(factory):
    db = factory()
    try:
        names = sa.inspect(db.get_bind()).get_table_names()
        return {name: db.execute(sa.text(f'SELECT COUNT(*) FROM "{name}"')).scalar() for name in names}
    finally:
        db.close()


def test_dry_run_changes_no_non_job_table_and_records_planned_items(factory, job):
    job.subjects = {1: T0 + timedelta(minutes=10), 2: T0 + timedelta(days=5)}
    _mode(factory, "dry_run", T0)
    before = _row_counts(factory)
    result = runner.run_job(factory, job.name, dry_run=True, trigger="schedule", now=T0 + timedelta(hours=1))
    after = _row_counts(factory)
    assert {k: v for k, v in before.items() if k not in JOB_TABLES} == {
        k: v for k, v in after.items() if k not in JOB_TABLES
    }
    assert after["billing_job_actions"] == 0
    assert _items(factory, result.run_id) == [("s:1", "planned", None)]
    assert job.executed == []


def test_admin_dry_run_of_an_off_job_uses_its_own_now(factory, job):
    job.subjects = {1: T0 - timedelta(minutes=1)}
    result = runner.run_job(factory, job.name, dry_run=True, trigger="admin", now=T0)
    assert result.status == "complete" and result.counts == {}
    db = factory()
    run = db.get(BillingJobRun, result.run_id)
    assert run.watermark_at.replace(tzinfo=timezone.utc) == T0 and run.trigger.value == "admin"
    assert db.get(BillingJobState, job.name).mode.value == "off"
    db.close()


# ------------------------------------------------------------------ claims, lease, stuck


def test_a_failing_effect_leaves_no_claim_and_is_retried(factory, job):
    _dry_run_then_live(factory, job, dry_at=T0 - timedelta(days=1), live_at=T0)
    job.subjects = {1: T0 + timedelta(minutes=10)}
    calls = []

    def boom(db, run, action):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("gateway down")

    job.on_execute = boom
    first = _live(factory, job, T0 + timedelta(hours=1))
    assert first.counts == {"failed": 1} and _effects(factory) == []
    db = factory()
    assert db.query(BillingJobAction).count() == 0
    db.close()
    _live(factory, job, T0 + timedelta(hours=2))
    assert _effects(factory) == ["fx-1"]


def test_a_held_lease_refuses_the_run(factory, job):
    _dry_run_then_live(factory, job, dry_at=T0 - timedelta(days=1), live_at=T0)
    db = factory()
    db.merge(RecurringBillingLease(name="billing_job:generate_invoices_ahead", owner="other",
                                   expires_at=T0 + timedelta(days=1)))
    db.commit()
    db.close()
    assert _live(factory, job, T0 + timedelta(hours=1)).status == "locked"


def _lease_seconds(factory):
    from app.services.settings_store import get_setting

    db = factory()
    try:
        return int(get_setting(db, "billing_job_lease_seconds"))
    finally:
        db.close()


def test_a_run_that_loses_its_lease_stops(factory, job, monkeypatch):
    _dry_run_then_live(factory, job, dry_at=T0 - timedelta(days=1), live_at=T0)
    job.subjects = {i: T0 + timedelta(minutes=i) for i in range(1, 4)}
    clock = [0.0]
    monkeypatch.setattr(runner, "_clock", lambda: clock[0], raising=False)
    lease = _lease_seconds(factory)

    def steal_then_list(db, run):
        other = factory()
        other.query(RecurringBillingLease).filter_by(name="billing_job:generate_invoices_ahead").update(
            {"owner": "thief"}
        )
        other.commit()
        other.close()
        clock[0] += lease / 2
        return sorted(job.subjects)

    monkeypatch.setattr(job, "candidates", steal_then_list)
    result = _live(factory, job, T0 + timedelta(hours=1))
    assert result.status == "aborted" and result.error == "lease lost"
    assert job.executed == []


def test_a_slow_run_stops_once_a_second_owner_takes_its_expired_lease(factory, job, monkeypatch):
    """Items that each outlast the lease: the overlap ends with the first run stopping."""
    _dry_run_then_live(factory, job, dry_at=T0 - timedelta(days=1), live_at=T0)
    job.subjects = {i: T0 + timedelta(minutes=i) for i in range(1, 5)}
    clock = [0.0]
    monkeypatch.setattr(runner, "_clock", lambda: clock[0], raising=False)
    lease = _lease_seconds(factory)
    now = T0 + timedelta(hours=1)
    second = []
    plan = job.plan

    def slow(db, run, subject_id):
        clock[0] += lease + 10
        if subject_id == 1:
            other = factory()
            second.append(periodic.acquire_lease(other, "billing_job:generate_invoices_ahead", owner="second",
                                                 seconds=lease, now=now + timedelta(seconds=clock[0])))
            other.close()
        return plan(db, run, subject_id)

    monkeypatch.setattr(job, "plan", slow)
    result = _live(factory, job, now)
    assert second == [True]
    assert result.status == "aborted" and result.error == "lease lost"
    assert job.executed == [1]


def test_a_slow_run_renews_by_time_so_no_second_owner_gets_in(factory, job, monkeypatch):
    _dry_run_then_live(factory, job, dry_at=T0 - timedelta(days=1), live_at=T0)
    job.subjects = {i: T0 + timedelta(minutes=i) for i in range(1, 5)}
    clock = [0.0]
    monkeypatch.setattr(runner, "_clock", lambda: clock[0], raising=False)
    lease = _lease_seconds(factory)
    now = T0 + timedelta(hours=1)
    second = []
    plan = job.plan

    def slow(db, run, subject_id):
        clock[0] += lease * 0.6
        if subject_id == 2:
            # Past the expiry of the lease as first taken.
            other = factory()
            second.append(periodic.acquire_lease(other, "billing_job:generate_invoices_ahead", owner="second",
                                                 seconds=lease, now=now + timedelta(seconds=clock[0])))
            other.close()
        return plan(db, run, subject_id)

    monkeypatch.setattr(job, "plan", slow)
    result = _live(factory, job, now)
    assert second == [False]
    assert result.status == "complete" and job.executed == [1, 2, 3, 4]


@pytest.mark.parametrize("change", ["off", "off_then_dry_run", "live_since_moved"])
def test_a_run_stops_when_the_job_is_switched_off_or_re_armed(factory, job, monkeypatch, change):
    _dry_run_then_live(factory, job, dry_at=T0 - timedelta(days=1), live_at=T0)
    job.subjects = {i: T0 + timedelta(minutes=i) for i in range(1, 4)}
    now = T0 + timedelta(hours=1)
    plan = job.plan

    def switch_during_first(db, run, subject_id):
        if subject_id == 1:
            if change == "live_since_moved":
                other = factory()
                other.get(BillingJobState, job.name).live_since = T0 + timedelta(minutes=30)
                other.commit()
                other.close()
            else:
                _mode(factory, "off", now)
                if change == "off_then_dry_run":
                    _mode(factory, "dry_run", now)
        return plan(db, run, subject_id)

    monkeypatch.setattr(job, "plan", switch_during_first)
    result = _live(factory, job, now)
    assert result.status == "aborted" and result.error == "mode changed"
    assert job.executed == [1] and _effects(factory) == ["fx-1"]


def test_stuck_claims_become_needs_review(factory, job):
    _dry_run_then_live(factory, job, dry_at=T0 - timedelta(days=1), live_at=T0)
    db = factory()
    db.add(BillingJobAction(job=job.name, subject_key="old", status="claimed", claimed_at=T0))
    db.add(BillingJobAction(job=job.name, subject_key="fresh", status="claimed", claimed_at=T0 + timedelta(minutes=50)))
    db.commit()
    db.close()
    _live(factory, job, T0 + timedelta(minutes=61))
    db = factory()
    assert {row.subject_key: row.status.value for row in db.query(BillingJobAction)} == {
        "old": "needs_review",
        "fresh": "claimed",
    }
    db.close()


def test_two_transaction_finish(factory, job):
    _dry_run_then_live(factory, job, dry_at=T0 - timedelta(days=1), live_at=T0)
    db = factory()
    run = JobRun(run_id=None, job=job.name, dry_run=False, now=T0, watermark=T0)
    assert run.claim(db, "service:1:terminate")
    db.commit()
    assert not run.claim(db, "service:1:terminate")
    run.finish(db, "service:1:terminate", "failed", {"error": "power"})
    db.commit()
    row = db.query(BillingJobAction).one()
    assert (row.status.value, row.detail) == ("failed", {"error": "power"})
    db.close()


# ------------------------------------------------------------------ schedule


def test_scheduled_runs_once_per_slot(factory, job):
    _mode(factory, "dry_run", T0 - timedelta(days=1))
    job.subjects = {1: T0}
    at = T0.replace(minute=6)
    assert periodic.run_automation_jobs(owner="w", now=at, session_factory=factory) == {job.name: "complete"}
    assert periodic.run_automation_jobs(owner="w", now=at + timedelta(minutes=20), session_factory=factory) == {
        job.name: None
    }
    db = factory()
    assert db.query(BillingJobRun).count() == 1
    db.close()
    periodic.run_automation_jobs(owner="w", now=at + timedelta(hours=1), session_factory=factory)
    db = factory()
    assert db.query(BillingJobRun).count() == 2
    db.close()


def test_daily_slot_follows_the_automation_time(factory):
    db = factory()
    assert periodic.slot_start(db, "invoice_reminders", T0) == T0.replace(hour=9, minute=0)
    assert periodic.slot_start(db, "invoice_reminders", T0.replace(hour=8)) == T0.replace(
        hour=9, minute=0
    ) - timedelta(days=1)
    assert periodic.slot_start(db, "generate_invoices_ahead", T0.replace(minute=4)) == T0.replace(
        hour=8, minute=5
    )
    db.close()


# ------------------------------------------------------------------ property


@hsettings(max_examples=300, deadline=None)
@given(
    watermark=st.integers(min_value=-10_000, max_value=10_000),
    trigger=st.integers(min_value=-20_000, max_value=20_000),
    now=st.integers(min_value=-10_000, max_value=20_000),
    catch_up=st.integers(min_value=1, max_value=30),
)
def test_window_never_admits_a_trigger_before_the_watermark(watermark, trigger, now, catch_up):
    base = datetime(2027, 1, 1, tzinfo=timezone.utc)
    run = JobRun(
        run_id=1,
        job="x",
        dry_run=False,
        now=base + timedelta(minutes=now),
        watermark=base + timedelta(minutes=watermark),
        catch_up_days=catch_up,
    )
    at = base + timedelta(minutes=trigger)
    if run.in_window(at):
        assert run.watermark <= at <= run.now
        assert at >= run.now - timedelta(days=catch_up)
    if trigger < watermark:
        assert not run.in_window(at) and not run.reached(at)


# ------------------------------------------------------------------ detectors


def test_needs_review_and_stale_detectors(factory, job):
    from app.services.alerting import billing_integrity

    params = {"max_rows": 1}
    db = factory()
    assert billing_integrity.billing_job_needs_review(db, params, T0) == []
    assert billing_integrity.billing_job_stale(db, params, T0) == []
    db.close()
    _dry_run_then_live(factory, job, dry_at=T0 - timedelta(days=1), live_at=T0)
    db = factory()
    for key in ("a", "b"):
        db.add(BillingJobAction(job=job.name, subject_key=key, status="needs_review", claimed_at=T0))
    db.add(BillingJobAction(job=job.name, subject_key="c", status="done", claimed_at=T0))
    db.commit()
    found = billing_integrity.billing_job_needs_review(db, params, T0)
    assert [f.resource_id.split(":")[0] for f in found] == ["billing_job_action", "overflow"]
    # Hourly job: live for under two hours is never stale.
    assert billing_integrity.billing_job_stale(db, params, T0 + timedelta(hours=2)) == []
    stale = billing_integrity.billing_job_stale(db, params, T0 + timedelta(hours=2, minutes=1))
    assert [f.resource_id for f in stale] == ["billing_job:generate_invoices_ahead"]
    db.close()
    periodic.run_automation_jobs(owner="w", now=T0 + timedelta(hours=2, minutes=5), session_factory=factory)
    db = factory()
    assert billing_integrity.billing_job_stale(db, params, T0 + timedelta(hours=3)) == []
    assert billing_integrity.billing_job_stale(db, params, T0 + timedelta(hours=4, minutes=6)) != []
    db.close()


def test_an_item_that_is_not_done_rolls_back_its_writes_and_claim(factory, job, monkeypatch):
    _dry_run_then_live(factory, job, dry_at=T0 - timedelta(days=1), live_at=T0)
    job.subjects = {1: T0 + timedelta(minutes=10)}

    def write_then_skip(db, run, action):
        assert run.claim(db, action.subject_key)
        db.add(SystemSetting(key="fx-1", value=1))
        db.flush()
        return "skipped:changed_under_lock"

    monkeypatch.setattr(job, "execute", write_then_skip)
    result = _live(factory, job, T0 + timedelta(hours=1))
    assert _items(factory, result.run_id) == [("s:1", "skipped", "changed_under_lock")]
    assert _effects(factory) == []
    db = factory()
    assert db.query(BillingJobAction).count() == 0
    db.close()
