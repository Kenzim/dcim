"""Channels: rendering, signing, delivery retries, URL safety, retention and the lease."""

import json
from datetime import datetime, timedelta

import pytest

from app.core.config import settings
from app.models.alerting import (
    Alert,
    AlertChannel,
    AlertSilence,
    NotificationDelivery,
    VmBackupRun,
)
from app.models.reseller import RecurringBillingLease
from app.models.service import Service, ServiceStatus, ServiceType
from app.services.alerting import admin as alerts_admin
from app.services.alerting import backup_watch, lease, render, retention, safe_http
from app.services.alerting.catalogue import Finding
from app.services.alerting.engine import run_cycle
from app.services.alerting.scrub import scrub
from app.services.alerting.secrets import mask_url, seal, unseal
from app.services.alerting.sender import DeliverySender, sign

T0 = datetime(2026, 1, 1, 12, 0, 0)
TYPE = "storage_node_offline"


class FakeHttp:
    def __init__(self, status=204):
        self.status = status
        self.calls = []
        self.raise_exc = None

    def __call__(self, url, body, headers, **kw):
        self.calls.append((url, body, headers, kw))
        if self.raise_exc:
            raise self.raise_exc
        return safe_http.HttpResult(self.status, "")


class FakeMail:
    def __init__(self):
        self.sent = []
        self.fail = False

    def __call__(self, **kw):
        if self.fail:
            raise OSError("smtp down")
        self.sent.append(kw)


def sample_item(event="opened", severity="critical"):
    return {
        "id": 7, "event": event, "type": TYPE, "severity": severity, "title": "node-1 is offline",
        "message": "no contact for 10 minutes", "resource_type": "storage_node", "resource_id": "1",
        "resource_label": "node-1", "opened_at": "2026-01-01T12:00:00Z", "resolved_at": None,
        "notify_count": 1, "details": {"age_seconds": 600},
    }


def queue_one(db, **channel):
    row = AlertChannel(name=channel.pop("name", "ops"), enabled=True, min_severity="info", alert_types=[],
                       send_resolved=True, **channel)
    db.add(row)
    db.commit()
    run_cycle(
        db,
        now=T0,
        detectors={TYPE: lambda db_, p, n: [Finding(TYPE, "storage_node", "1", "node-1", "node-1 is offline", "gone")]},
        only_types=[TYPE],
    )
    return db.query(NotificationDelivery).order_by(NotificationDelivery.id.desc()).first()


def make_sender(**kw):
    clock = kw.pop("clock", None) or (lambda: T0)
    return DeliverySender(clock=clock, **kw)


# ---------------------------------------------------------------- rendering


def test_generic_webhook_body():
    body = render.channel_payload("webhook", [sample_item()], sent_at="2026-01-01T12:00:00Z", summary=False)
    assert body["version"] == 1 and body["event"] == "opened"
    assert body["alerts"][0]["resource_label"] == "node-1"
    json.dumps(body)


def test_discord_and_slack_use_native_formats():
    discord = render.channel_payload("discord", [sample_item()], sent_at="x", summary=False)
    embed = discord["embeds"][0]
    assert embed["title"].startswith("[Rackflow] [ALERT CRITICAL]") and embed["color"] == 0xDC2626
    resolved = render.channel_payload("discord", [sample_item("resolved")], sent_at="x", summary=False)
    assert resolved["embeds"][0]["color"] == render.RESOLVED_COLOUR
    slack = render.channel_payload("slack", [sample_item()], sent_at="x", summary=False)
    assert slack["attachments"][0]["color"] == "#dc2626" and "node-1 is offline" in slack["text"]
    summary = render.channel_payload("slack", [sample_item(), sample_item()], sent_at="x", summary=True)
    assert "2 alert notices" in summary["text"]


def test_email_subject_and_body():
    mail = render.channel_payload("email", [sample_item("resolved")], sent_at="x", summary=False)
    assert mail["subject"] == "[Rackflow] [RESOLVED] node-1 is offline"
    assert "no contact for 10 minutes" in mail["text"]
    assert "<p>" in mail["html"]


# ---------------------------------------------------------------- delivery


def test_webhook_delivery_is_signed_and_marked_sent(db_session):
    row = queue_one(db_session, kind="webhook",
                    config={"url": seal("https://hooks.example.com/in"), "secret": seal("s3cret")})
    http = FakeHttp(200)
    assert make_sender(http_post=http).send_batch(db_session, owner="w1") == 1
    url, body, headers, kw = http.calls[0]
    assert url == "https://hooks.example.com/in"
    assert headers["X-Rackflow-Signature"] == sign("s3cret", body)
    assert headers["X-Rackflow-Event"] == "opened"
    assert kw["public_only"] is False
    db_session.refresh(row)
    assert row.status == "sent" and row.attempts == 1 and row.claim_token is None


def test_discord_delivery_has_no_signature_and_accepts_204(db_session):
    queue_one(db_session, kind="discord", config={"url": seal("https://discord.com/api/webhooks/1/abc")})
    http = FakeHttp(204)
    assert make_sender(http_post=http).send_batch(db_session, owner="w1") == 1
    assert "X-Rackflow-Signature" not in http.calls[0][2]
    assert "embeds" in json.loads(http.calls[0][1])


def test_server_error_is_retried_with_backoff_then_dies(db_session):
    row = queue_one(db_session, kind="webhook", config={"url": seal("https://hooks.example.com/in")})
    http = FakeHttp(503)
    now = [T0]
    sender = make_sender(http_post=http, clock=lambda: now[0])
    sender.send_batch(db_session, owner="w1")
    db_session.refresh(row)
    assert row.status == "failed" and row.attempts == 1
    assert row.next_attempt_at == T0 + timedelta(seconds=60)
    assert "503" in row.error
    assert sender.send_batch(db_session, owner="w1") == 0  # not due yet
    for _ in range(settings.alert_delivery_max_attempts - 1):
        now[0] += timedelta(hours=2)
        sender.send_batch(db_session, owner="w1")
    db_session.refresh(row)
    assert row.status == "dead" and row.attempts == settings.alert_delivery_max_attempts


def test_client_error_is_permanent(db_session):
    row = queue_one(db_session, kind="webhook", config={"url": seal("https://hooks.example.com/in")})
    make_sender(http_post=FakeHttp(404)).send_batch(db_session, owner="w1")
    db_session.refresh(row)
    assert row.status == "dead" and row.attempts == 1


def test_transport_exception_is_retried_and_error_is_scrubbed(db_session):
    row = queue_one(db_session, kind="webhook", config={"url": seal("https://hooks.example.com/in")})
    http = FakeHttp()
    http.raise_exc = ConnectionError("connect failed token=abc123")
    make_sender(http_post=http).send_batch(db_session, owner="w1")
    db_session.refresh(row)
    assert row.status == "failed" and "abc123" not in row.error


def test_deleted_or_disabled_channel(db_session):
    row = queue_one(db_session, kind="webhook", config={"url": seal("https://hooks.example.com/in")})
    channel = db_session.query(AlertChannel).one()
    channel.enabled = False
    db_session.commit()
    http = FakeHttp()
    make_sender(http_post=http).send_batch(db_session, owner="w1")
    db_session.refresh(row)
    assert row.status == "skipped" and http.calls == []
    row2 = NotificationDelivery(idempotency_key="orphan", channel_kind="webhook", target="gone", event="opened",
                                payload={}, channel_id=None, status="queued")
    db_session.add(row2)
    db_session.commit()
    make_sender(http_post=http).send_batch(db_session, owner="w1")
    db_session.refresh(row2)
    assert row2.status == "dead"


def test_email_delivery_and_smtp_disabled(db_session, monkeypatch):
    row = queue_one(db_session, kind="email", config={"addresses": ["oncall@example.com"]})
    mail = FakeMail()
    monkeypatch.setattr(settings, "smtp_enabled", False)
    make_sender(email_sender=mail).send_batch(db_session, owner="w1")
    db_session.refresh(row)
    assert row.status == "skipped" and mail.sent == []
    row.status = "queued"
    db_session.commit()
    monkeypatch.setattr(settings, "smtp_enabled", True)
    assert make_sender(email_sender=mail).send_batch(db_session, owner="w1") == 1
    assert mail.sent[0]["to_address"] == "oncall@example.com"
    assert mail.sent[0]["subject"].startswith("[Rackflow] [ALERT CRITICAL]")


def test_expired_claim_is_picked_up_again(db_session):
    row = queue_one(db_session, kind="webhook", config={"url": seal("https://hooks.example.com/in")})
    row.status = "sending"
    row.claim_token = "dead-worker"
    row.claim_expires_at = T0 - timedelta(minutes=1)
    db_session.commit()
    assert make_sender(http_post=FakeHttp()).send_batch(db_session, owner="w2") == 1


def test_live_claim_is_left_alone(db_session):
    row = queue_one(db_session, kind="webhook", config={"url": seal("https://hooks.example.com/in")})
    row.status = "sending"
    row.claim_token = "other"
    row.claim_expires_at = T0 + timedelta(minutes=1)
    db_session.commit()
    assert make_sender(http_post=FakeHttp()).send_batch(db_session, owner="w2") == 0


def test_one_dead_channel_does_not_block_another(db_session):
    db_session.add(AlertChannel(name="bad", kind="webhook", min_severity="info", alert_types=[], send_resolved=True,
                                config={"url": seal("https://bad.example.com/")}))
    queue_one(db_session, name="good", kind="slack", config={"url": seal("https://hooks.slack.com/services/x")})

    def post(url, body, headers, **kw):
        if "bad" in url:
            return safe_http.HttpResult(500, "")
        return safe_http.HttpResult(200, "")

    assert make_sender(http_post=post).send_batch(db_session, owner="w1") == 1
    statuses = {d.target: d.status for d in db_session.query(NotificationDelivery).all()}
    assert statuses == {"bad": "failed", "good": "sent"}


# ---------------------------------------------------------------- admin operations on channels


def test_channel_crud_hides_secrets(db_session, test_admin_user):
    ch = alerts_admin.create_channel(
        db_session, name="hook", kind="webhook", user_id=test_admin_user.id,
        url="https://hooks.example.com/abc/def?token=zzz", secret="topsecret",
    )
    data = alerts_admin.serialise_channel(ch)
    assert data["url"] == "https://hooks.example.com/\u2026" and data["has_secret"] is True
    assert "zzz" not in json.dumps(data) and "topsecret" not in json.dumps(data)
    alerts_admin.update_channel(db_session, ch.id, min_severity="critical", secret="")
    assert alerts_admin.serialise_channel(ch)["has_secret"] is False
    assert unseal(ch.config["url"]).startswith("https://hooks.example.com/abc")
    with pytest.raises(alerts_admin.AlertError):
        alerts_admin.create_channel(db_session, name="hook", kind="webhook", user_id=None, url="https://x.example.com")
    with pytest.raises(alerts_admin.AlertError):
        alerts_admin.create_channel(db_session, name="bad", kind="webhook", user_id=None, url="ftp://x.example.com")
    with pytest.raises(alerts_admin.AlertError):
        alerts_admin.create_channel(db_session, name="m", kind="email", user_id=None, addresses=["nope"])
    with pytest.raises(alerts_admin.AlertError):
        alerts_admin.create_channel(db_session, name="t", kind="slack", user_id=None, url="https://a.example.com",
                                    alert_types=["not_a_type"])
    alerts_admin.delete_channel(db_session, ch.id)
    with pytest.raises(alerts_admin.NotFound):
        alerts_admin.delete_channel(db_session, ch.id)


def test_send_test_notice_reports_failure(db_session, test_admin_user):
    ch = alerts_admin.create_channel(db_session, name="hook", kind="discord", user_id=None,
                                     url="https://discord.com/api/webhooks/1/x")
    ok = alerts_admin.send_test_notice(db_session, ch.id, sender=make_sender(http_post=FakeHttp(204)))
    assert ok == {"ok": True, "errors": []}
    bad = alerts_admin.send_test_notice(db_session, ch.id, sender=make_sender(http_post=FakeHttp(500)))
    assert bad["ok"] is False and "500" in bad["errors"][0]
    assert db_session.query(NotificationDelivery).count() == 0


# ---------------------------------------------------------------- URL safety


def fake_resolver(*addresses):
    return lambda host, port: list(addresses)


def test_validate_url_public_only_blocks_private_and_loopback():
    for address in ("10.0.0.5", "127.0.0.1", "169.254.169.254", "192.168.1.1", "::1", "fd00::1", "100.64.0.1"):
        with pytest.raises(safe_http.WebhookUrlError):
            safe_http.validate_url("https://x.example.com/h", public_only=True, https_only=True,
                                   resolver=fake_resolver(address))
    with pytest.raises(safe_http.WebhookUrlError):
        safe_http.validate_url("https://x.example.com/h", public_only=True, https_only=True,
                               resolver=fake_resolver("8.8.8.8", "10.0.0.1"))
    scheme, host, port, path, addrs = safe_http.validate_url(
        "https://x.example.com:8443/h?a=1", public_only=True, https_only=True, resolver=fake_resolver("8.8.8.8"))
    assert (scheme, host, port, path, addrs) == ("https", "x.example.com", 8443, "/h?a=1", ["8.8.8.8"])


def test_validate_url_literals_and_schemes():
    with pytest.raises(safe_http.WebhookUrlError):
        safe_http.validate_url("https://127.0.0.1/h", public_only=True, https_only=True)
    with pytest.raises(safe_http.WebhookUrlError):
        safe_http.validate_url("http://x.example.com/h", public_only=True, https_only=True,
                               resolver=fake_resolver("8.8.8.8"))
    with pytest.raises(safe_http.WebhookUrlError):
        safe_http.validate_url("https://user:pw@x.example.com/h", public_only=False, https_only=True,
                               resolver=fake_resolver("8.8.8.8"))
    with pytest.raises(safe_http.WebhookUrlError):
        safe_http.validate_url("file:///etc/passwd", public_only=False, https_only=False)
    safe_http.validate_url("http://10.1.2.3:9000/h", public_only=False, https_only=False)


def test_post_rejects_private_target_before_connecting():
    with pytest.raises(safe_http.WebhookUrlError):
        safe_http.post("https://127.0.0.1/x", b"{}", {}, public_only=True, https_only=True)


# ---------------------------------------------------------------- helpers


def test_scrub_removes_credentials():
    text = "failed https://user:hunter2@host/x password=abc token: def Bearer abc.def.ghi community public"
    out = scrub(text)
    for secret in ("hunter2", "abc.def", "password=abc", "token: def"):
        assert secret not in out
    assert len(scrub("x" * 1000)) == 300


def test_seal_roundtrip_and_plain_marker():
    assert unseal(seal("hello")) == "hello"
    assert unseal(None) is None
    assert mask_url("https://discord.com/api/webhooks/1/abc") == "https://discord.com/\u2026"


# ---------------------------------------------------------------- retention and lease


def test_retention_prunes_old_rows_and_keeps_open_alerts(db_session):
    def alert(state, resolved_days, key):
        return Alert(alert_type=TYPE, resource_type="storage_node", resource_id=key, severity="info", title="t",
                     state=state, opened_at=T0 - timedelta(days=200), last_seen_at=T0 - timedelta(days=200),
                     resolved_at=T0 - timedelta(days=resolved_days) if resolved_days is not None else None,
                     details={}, active_key=f"k{key}" if state == "open" else None)

    db_session.add_all([alert("resolved", 100, "1"), alert("resolved", 10, "2"), alert("open", None, "3")])
    now = T0
    db_session.add_all(
        [
            AlertSilence(alert_type=TYPE, starts_at=now - timedelta(days=60), ends_at=now - timedelta(days=40)),
            AlertSilence(alert_type=TYPE, starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=1)),
        ]
    )
    for i, (status, age) in enumerate([("sent", 40), ("sent", 5), ("skipped", 40), ("dead", 100), ("dead", 10),
                                       ("queued", 400)]):
        db_session.add(NotificationDelivery(idempotency_key=f"d{i}", channel_kind="webhook", target="t", event="e",
                                            payload={}, status=status, created_at=now - timedelta(days=age),
                                            updated_at=now - timedelta(days=age)))
    svc = Service(name="s", service_type=ServiceType.VM, status=ServiceStatus.ACTIVE)
    db_session.add(svc)
    db_session.flush()
    db_session.add_all(
        [
            VmBackupRun(service_id=svc.id, upid="a", status="succeeded", started_at=now - timedelta(days=200),
                        finished_at=now - timedelta(days=199)),
            VmBackupRun(service_id=svc.id, upid="b", status="failed", started_at=now - timedelta(days=2),
                        finished_at=now - timedelta(days=2)),
            VmBackupRun(service_id=svc.id, upid="c", status="running", started_at=now - timedelta(days=300)),
        ]
    )
    db_session.commit()
    counts = retention.prune(db_session, now)
    assert counts == {"alerts": 1, "silences": 1, "deliveries_sent": 2, "deliveries_failed": 1, "backup_runs": 1}
    assert db_session.query(Alert).count() == 2
    assert db_session.query(AlertSilence).count() == 1
    assert sorted(d.status for d in db_session.query(NotificationDelivery).all()) == ["dead", "queued", "sent"]
    assert db_session.query(VmBackupRun).count() == 2


def test_lease_is_exclusive_and_renewable(db_session):
    assert lease.acquire(db_session, owner="a", seconds=60, now=T0)
    assert not lease.acquire(db_session, owner="b", seconds=60, now=T0 + timedelta(seconds=30))
    assert lease.acquire(db_session, owner="a", seconds=60, now=T0 + timedelta(seconds=30))
    assert lease.acquire(db_session, owner="b", seconds=60, now=T0 + timedelta(minutes=5))
    lease.release(db_session, owner="a")
    assert db_session.get(RecurringBillingLease, "alerts").owner == "b"
    lease.release(db_session, owner="b")
    assert lease.acquire(db_session, owner="c", seconds=60, now=T0 + timedelta(minutes=5, seconds=1))


# ---------------------------------------------------------------- backup watch


class FakePlugin:
    def __init__(self, data):
        self.data = data

    async def get_proxmox_task_status(self, upid):
        return self.data


@pytest.mark.asyncio
async def test_backup_watch_closes_runs(db_session, monkeypatch):
    svc = Service(name="vm", service_type=ServiceType.VM, status=ServiceStatus.ACTIVE)
    db_session.add(svc)
    db_session.commit()
    ok = backup_watch.record_started(db_session, svc.id, "UPID:ok")
    bad = backup_watch.record_started(db_session, svc.id, "UPID:bad")
    running = backup_watch.record_started(db_session, svc.id, "UPID:run")
    answers = {
        "UPID:ok": {"status": "stopped", "exitstatus": "OK"},
        "UPID:bad": {"status": "stopped", "exitstatus": "job errors"},
        "UPID:run": {"status": "running"},
    }

    import app.services.vm_backup_service as vbs

    class P:
        async def get_proxmox_task_status(self, upid):
            return answers[upid]

    async def get_plugin(db, service):
        return P(), 1, "n", 100

    monkeypatch.setattr(vbs, "get_vm_backup_plugin", get_plugin)
    closed = await backup_watch.poll_running(db_session, now=datetime.utcnow())
    assert closed == 2
    for run in (ok, bad, running):
        db_session.refresh(run)
    assert (ok.status, bad.status, running.status) == ("succeeded", "failed", "running")
    assert bad.error == "job errors"
    late = await backup_watch.poll_running(db_session, now=datetime.utcnow() + timedelta(hours=7))
    db_session.refresh(running)
    assert late == 1 and running.status == "failed"
