"""Alert lifecycle: open, no repeat, resolve, hold-down, acknowledge, silence, flapping, storms."""

from datetime import datetime, timedelta

import pytest

from app.models.alerting import (
    Alert,
    AlertChannel,
    AlertRecipient,
    AlertSilence,
    NotificationDelivery,
)
from app.models.user import User
from app.services.alerting import admin as alerts_admin
from app.services.alerting import catalogue
from app.services.alerting.catalogue import Finding
from app.services.alerting.engine import run_cycle

TYPE = "storage_node_offline"
T0 = datetime(2026, 1, 1, 12, 0, 0)


class Source:
    """A detector whose findings the test controls."""

    def __init__(self):
        self.findings: list[Finding] = []
        self.fail = False

    def __call__(self, db, params, now):
        if self.fail:
            raise RuntimeError("boom")
        return list(self.findings)


def finding(resource_id="1", label="node-1", **kw):
    return Finding(TYPE, "storage_node", resource_id, label, f"{label} is offline", "no contact", **kw)


@pytest.fixture
def source():
    return Source()


@pytest.fixture
def channel(db_session):
    row = AlertChannel(
        name="ops",
        kind="webhook",
        enabled=True,
        min_severity="info",
        alert_types=[],
        send_resolved=True,
        config={"url": "plain:https://hooks.example.com/x"},
    )
    db_session.add(row)
    db_session.commit()
    return row


def cycle(db, source, minutes):
    return run_cycle(
        db,
        now=T0 + timedelta(minutes=minutes),
        detectors={TYPE: source},
        only_types=[TYPE],
    )


def deliveries(db, event=None):
    q = db.query(NotificationDelivery)
    if event:
        q = q.filter(NotificationDelivery.event == event)
    return q.order_by(NotificationDelivery.id).all()


def tune(db, **kw):
    catalogue.update_type_setting(db, TYPE, **kw)
    db.commit()


def test_opens_once_and_does_not_repeat(db_session, source, channel):
    source.findings = [finding()]
    r1 = cycle(db_session, source, 0)
    assert r1.opened == 1
    for minute in (1, 2, 30):
        cycle(db_session, source, minute)
    alerts = db_session.query(Alert).all()
    assert len(alerts) == 1
    assert alerts[0].state == "open" and alerts[0].severity == "critical"
    assert [d.event for d in deliveries(db_session)] == ["opened"]
    assert alerts[0].notify_count == 1


def test_resolves_after_hold_down_and_sends_resolved_notice(db_session, source, channel):
    source.findings = [finding()]
    cycle(db_session, source, 0)
    source.findings = []
    cycle(db_session, source, 1)
    assert db_session.query(Alert).one().state == "open"
    cycle(db_session, source, 3)
    assert db_session.query(Alert).one().state == "open"
    cycle(db_session, source, 7)
    alert = db_session.query(Alert).one()
    assert alert.state == "resolved" and alert.active_key is None
    assert [d.event for d in deliveries(db_session)] == ["opened", "resolved"]


def test_condition_returning_inside_hold_down_sends_nothing(db_session, source, channel):
    source.findings = [finding()]
    cycle(db_session, source, 0)
    source.findings = []
    cycle(db_session, source, 2)
    source.findings = [finding()]
    cycle(db_session, source, 3)
    alert = db_session.query(Alert).one()
    assert alert.state == "open" and alert.clear_since is None
    assert [d.event for d in deliveries(db_session)] == ["opened"]


def test_reminder_interval(db_session, source, channel):
    tune(db_session, reminder_interval_seconds=600)
    source.findings = [finding()]
    cycle(db_session, source, 0)
    cycle(db_session, source, 5)
    assert len(deliveries(db_session)) == 1
    cycle(db_session, source, 11)
    cycle(db_session, source, 12)
    assert [d.event for d in deliveries(db_session)] == ["opened", "reminder"]
    cycle(db_session, source, 22)
    assert [d.event for d in deliveries(db_session)] == ["opened", "reminder", "reminder"]


def test_acknowledge_stops_reminders_and_clears_on_resolve(db_session, source, channel, test_admin_user):
    tune(db_session, reminder_interval_seconds=600, hold_down_seconds=0)
    source.findings = [finding()]
    cycle(db_session, source, 0)
    alert = db_session.query(Alert).one()
    alerts_admin.acknowledge(db_session, alert.id, test_admin_user.id, "looking")
    cycle(db_session, source, 30)
    assert [d.event for d in deliveries(db_session)] == ["opened"]
    assert alerts_admin.summary(db_session)["active"] == 0
    assert alerts_admin.summary(db_session)["acknowledged"] == 1
    source.findings = []
    cycle(db_session, source, 31)
    alert = db_session.query(Alert).one()
    assert alert.state == "resolved" and alert.acknowledged_at is None
    assert [d.event for d in deliveries(db_session)] == ["opened", "resolved"]


def test_acknowledge_requires_open_alert(db_session, source, channel, test_admin_user):
    tune(db_session, hold_down_seconds=0)
    source.findings = [finding()]
    cycle(db_session, source, 0)
    source.findings = []
    cycle(db_session, source, 1)
    alert = db_session.query(Alert).one()
    with pytest.raises(alerts_admin.AlertError):
        alerts_admin.acknowledge(db_session, alert.id, test_admin_user.id)


def test_silenced_alert_is_visible_but_sends_nothing_then_notifies_when_silence_ends(
    db_session, source, channel, test_admin_user
):
    db_session.add(
        AlertSilence(
            alert_type=TYPE,
            resource_type="storage_node",
            resource_id="1",
            starts_at=T0 - timedelta(minutes=1),
            ends_at=T0 + timedelta(minutes=30),
            created_by=test_admin_user.id,
        )
    )
    db_session.commit()
    source.findings = [finding()]
    cycle(db_session, source, 0)
    cycle(db_session, source, 10)
    alert = db_session.query(Alert).one()
    assert alert.state == "open" and alert.notified_at is None
    assert deliveries(db_session) == []
    now = T0 + timedelta(minutes=10)
    listed = alerts_admin.list_alerts(db_session, now=now)
    assert listed["items"][0]["silenced"] is True
    summary = alerts_admin.summary(db_session, now=now)
    assert summary["silenced"] == 1 and summary["active"] == 0
    cycle(db_session, source, 40)
    assert [d.event for d in deliveries(db_session)] == ["opened"]


def test_resource_silence_covers_every_type_for_that_resource(db_session, source, channel):
    db_session.add(
        AlertSilence(
            resource_type="storage_node",
            resource_id="1",
            starts_at=T0 - timedelta(minutes=1),
            ends_at=T0 + timedelta(hours=2),
        )
    )
    db_session.commit()
    source.findings = [finding("1"), finding("2", "node-2")]
    cycle(db_session, source, 0)
    events = deliveries(db_session)
    assert len(events) == 1
    assert events[0].payload["alerts"][0]["resource_id"] == "2"


def test_silenced_alert_sends_no_resolved_notice(db_session, source, channel):
    tune(db_session, hold_down_seconds=0)
    source.findings = [finding()]
    cycle(db_session, source, 0)
    db_session.add(
        AlertSilence(alert_type=TYPE, starts_at=T0, ends_at=T0 + timedelta(hours=1), reason="maintenance")
    )
    db_session.commit()
    source.findings = []
    cycle(db_session, source, 5)
    assert db_session.query(Alert).one().state == "resolved"
    assert [d.event for d in deliveries(db_session)] == ["opened"]


def test_silence_validation(db_session, test_admin_user):
    with pytest.raises(alerts_admin.AlertError):
        alerts_admin.create_silence(db_session, user_id=test_admin_user.id, duration_minutes=10)
    with pytest.raises(alerts_admin.AlertError):
        alerts_admin.create_silence(db_session, user_id=test_admin_user.id, alert_type="nope", duration_minutes=10)
    with pytest.raises(alerts_admin.AlertError):
        alerts_admin.create_silence(
            db_session, user_id=test_admin_user.id, alert_type=TYPE, duration_minutes=60 * 24 * 365
        )
    row = alerts_admin.create_silence(
        db_session, user_id=test_admin_user.id, resource_type="switch", resource_id="4", duration_minutes=60
    )
    assert row.id


def test_flapping_condition_is_held_down_then_reported_once(db_session, source, channel):
    tune(db_session, hold_down_seconds=0)
    minute = 0
    for _ in range(6):
        source.findings = [finding()]
        cycle(db_session, source, minute)
        source.findings = []
        cycle(db_session, source, minute + 1)
        minute += 2
    assert db_session.query(Alert).count() == 1
    alert = db_session.query(Alert).one()
    assert alert.state == "resolved"
    events = [d.event for d in deliveries(db_session)]
    assert events.count("opened") == 3
    assert events.count("flapping") == 1
    assert events.count("resolved") == 3
    assert alert.resolve_notified is False and alert.resolve_notice_due_at is not None
    cycle(db_session, source, minute + 5)
    assert [d.event for d in deliveries(db_session)].count("resolved") == 3
    cycle(db_session, source, minute + 40)
    assert [d.event for d in deliveries(db_session)].count("resolved") == 4
    cycle(db_session, source, minute + 80)
    assert [d.event for d in deliveries(db_session)].count("resolved") == 4


def test_new_alert_after_reopen_window(db_session, source, channel):
    tune(db_session, hold_down_seconds=0)
    source.findings = [finding()]
    cycle(db_session, source, 0)
    source.findings = []
    cycle(db_session, source, 1)
    source.findings = [finding()]
    cycle(db_session, source, 120)
    assert db_session.query(Alert).count() == 2
    assert [d.event for d in deliveries(db_session)] == ["opened", "resolved", "opened"]


def test_severity_escalation_notifies_and_drop_is_quiet(db_session, source, channel):
    source.findings = [finding(severity="warning")]
    cycle(db_session, source, 0)
    source.findings = [finding(severity="critical")]
    cycle(db_session, source, 1)
    source.findings = [finding(severity="warning")]
    cycle(db_session, source, 2)
    assert [d.event for d in deliveries(db_session)] == ["opened", "escalated"]
    assert db_session.query(Alert).one().severity == "warning"


def test_failing_detector_neither_opens_nor_resolves(db_session, source, channel):
    source.findings = [finding()]
    cycle(db_session, source, 0)
    source.fail = True
    result = cycle(db_session, source, 30)
    assert result.failed_types == [TYPE]
    alert = db_session.query(Alert).one()
    assert alert.state == "open" and alert.clear_since is None


def test_disabled_type_closes_open_alerts_quietly(db_session, source, channel):
    source.findings = [finding()]
    cycle(db_session, source, 0)
    tune(db_session, enabled=False)
    cycle(db_session, source, 1)
    assert db_session.query(Alert).one().state == "resolved"
    assert [d.event for d in deliveries(db_session)] == ["opened"]
    source.findings = [finding()]
    cycle(db_session, source, 2)
    assert db_session.query(Alert).filter(Alert.state == "open").count() == 0


def test_severity_override_and_threshold_params(db_session):
    tune(db_session, severity="info", params={"offline_after_seconds": 900})
    eff = catalogue.effective_settings(db_session)[TYPE]
    assert eff.severity == "info" and eff.params["offline_after_seconds"] == 900
    with pytest.raises(ValueError):
        catalogue.update_type_setting(db_session, TYPE, params={"offline_after_seconds": 1})
    with pytest.raises(ValueError):
        catalogue.update_type_setting(db_session, TYPE, params={"nope": 1})
    with pytest.raises(ValueError):
        catalogue.update_type_setting(db_session, TYPE, severity="loud")


def test_storm_control_sends_one_summary(db_session, source, channel):
    source.findings = [finding(str(i), f"node-{i}") for i in range(8)]
    cycle(db_session, source, 0)
    rows = deliveries(db_session)
    assert len(rows) == 1 and rows[0].event == "summary"
    assert len(rows[0].payload["alerts"]) == 8
    assert db_session.query(Alert).count() == 8


def test_channel_filters(db_session, source):
    db_session.add_all(
        [
            AlertChannel(name="crit-only", kind="webhook", min_severity="critical", alert_types=[],
                         send_resolved=True, config={"url": "plain:https://a.example.com/"}),
            AlertChannel(name="warn", kind="slack", min_severity="warning", alert_types=["switch_unreachable"],
                         send_resolved=True, config={"url": "plain:https://b.example.com/"}),
            AlertChannel(name="no-resolve", kind="discord", min_severity="info", alert_types=[],
                         send_resolved=False, config={"url": "plain:https://c.example.com/"}),
            AlertChannel(name="off", kind="webhook", enabled=False, min_severity="info", alert_types=[],
                         send_resolved=True, config={"url": "plain:https://d.example.com/"}),
        ]
    )
    db_session.commit()
    tune(db_session, hold_down_seconds=0, severity="warning")
    source.findings = [finding()]
    cycle(db_session, source, 0)
    assert sorted(d.target for d in deliveries(db_session)) == ["no-resolve"]
    source.findings = []
    cycle(db_session, source, 1)
    assert sorted(d.target for d in deliveries(db_session)) == ["no-resolve"]


def test_email_channel_and_staff_recipient(db_session, source, test_admin_user, test_user):
    db_session.add(
        AlertChannel(name="mail", kind="email", min_severity="info", alert_types=[], send_resolved=True,
                     config={"addresses": ["oncall@example.com", "bad address", "oncall@example.com"]})
    )
    db_session.add(AlertRecipient(user_id=test_admin_user.id, enabled=True, min_severity="critical"))
    db_session.add(AlertRecipient(user_id=test_user.id, enabled=True, min_severity="info"))
    db_session.commit()
    source.findings = [finding()]
    cycle(db_session, source, 0)
    rows = deliveries(db_session)
    assert sorted(d.target for d in rows) == ["admin@example.com", "oncall@example.com"]
    assert all(d.channel_kind == "email" for d in rows)
    assert "[ALERT CRITICAL]" in rows[0].payload["subject"]


def test_summary_counts_by_severity(db_session, source, channel):
    source.findings = [finding("1"), finding("2", severity="warning")]
    cycle(db_session, source, 0)
    s = alerts_admin.summary(db_session)
    assert s["active"] == 2 and s["by_severity"]["critical"] == 1 and s["by_severity"]["warning"] == 1
    assert s["has_destination"] is True


def test_list_alerts_filters_and_pagination(db_session, source, channel):
    tune(db_session, hold_down_seconds=0)
    source.findings = [finding(str(i), f"node-{i}") for i in range(4)]
    cycle(db_session, source, 0)
    source.findings = source.findings[:3]
    cycle(db_session, source, 1)
    assert alerts_admin.list_alerts(db_session)["total"] == 3
    assert alerts_admin.list_alerts(db_session, state="resolved")["total"] == 1
    assert alerts_admin.list_alerts(db_session, state="all")["total"] == 4
    page = alerts_admin.list_alerts(db_session, limit=2, offset=0)
    assert len(page["items"]) == 2 and page["total"] == 3
    assert alerts_admin.list_alerts(db_session, query="node-2")["total"] == 1
    with pytest.raises(alerts_admin.AlertError):
        alerts_admin.list_alerts(db_session, state="weird")


def test_uses_users_table_for_recipients(db_session, test_admin_user):
    assert User.__tablename__ == "users"
    alerts_admin.set_recipient(db_session, test_admin_user.id, enabled=True, min_severity="warning")
    rows = alerts_admin.list_recipients(db_session)
    assert rows[0]["enabled"] is True


def test_failing_detector_leaves_no_notices_behind(db_session, source, channel):
    """A detector that opens an alert and then raises must not queue a notice for the rolled-back alert."""

    def half_done(db, params, now):
        db.add(Alert(alert_type=TYPE, resource_type="storage_node", resource_id="9", severity="critical",
                     title="x", state="open", active_key="k9", opened_at=now, last_seen_at=now, details={}))
        raise RuntimeError("boom")

    result = run_cycle(db_session, now=T0, detectors={TYPE: half_done}, only_types=[TYPE])
    assert result.failed_types == [TYPE] and result.notices == []
    assert db_session.query(Alert).count() == 0 and deliveries(db_session) == []
