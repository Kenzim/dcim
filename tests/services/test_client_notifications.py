"""Client notifications: opt-in, scoping, no internal detail, rate limit, webhooks, API and MCP."""

import json
from datetime import datetime, timedelta

import pytest

from app.core.config import settings
from app.models.alerting import Alert, ClientNotificationPref, NotificationDelivery
from app.models.location import Location
from app.models.server import Server
from app.models.service import ProvisioningSource, Service, ServiceStatus, ServiceType
from app.models.storage import StorageNode, StorageShare, StorageTenant
from app.models.user import User
from app.dao.service_dao import ServiceDAO
from app.services import client_notifications as cn
from app.services.alerting import detectors as d
from app.services.alerting.catalogue import CATALOGUE
from app.services.alerting.engine import run_cycle
from app.services.alerting.secrets import unseal
from app.services.audit_service import AuditService

SECRET_NAMES = ("hv-secret-01", "ceph-node-internal", "10.99.0.7", "bmc-secret", "rack-internal")


def make_user(db, name):
    user = User(username=name, email=f"{name}@example.com", is_admin=False)
    user.set_password("secret123")
    db.add(user)
    db.commit()
    return user


def make_vm(db, owner, name):
    return ServiceDAO.create_vm(
        db,
        name=name,
        owner_user_id=owner.id,
        status=ServiceStatus.ACTIVE,
        provisioning_source=ProvisioningSource.INTERNAL,
        config={"vm_ip_address": "10.99.0.7", "proxmox_node": "hv-secret-01"},
    )


def opt_in(db, user, *categories, webhook=False, email=True):
    row = ClientNotificationPref(user_id=user.id, email_enabled=email, categories={c: True for c in categories})
    if webhook:
        from app.services.alerting.secrets import seal

        row.webhook_url = seal("https://hooks.example.org/in")
        row.webhook_secret = seal("whsec_x")
    db.add(row)
    db.commit()
    return row


def rows(db, user=None):
    q = db.query(NotificationDelivery).filter(NotificationDelivery.audience == "client")
    if user is not None:
        q = q.filter(NotificationDelivery.user_id == user.id)
    return q.order_by(NotificationDelivery.id).all()


def audit(db, user, action, **kw):
    AuditService.log(db, actor_user_id=user.id, subject_user_id=user.id, action=action,
                     ip="203.0.113.9", user_agent="UnitTest/1.0", **kw)
    db.commit()


@pytest.fixture
def alice(db_session):
    return make_user(db_session, "alice")


@pytest.fixture
def bob(db_session):
    return make_user(db_session, "bob")


def test_nothing_is_sent_without_opt_in(db_session, alice):
    audit(db_session, alice, "login_success")
    assert rows(db_session) == []
    opt_in(db_session, alice, "failed_sign_in")
    audit(db_session, alice, "login_success")
    assert rows(db_session) == []


@pytest.mark.parametrize(
    "action,category",
    [
        ("login_success", "sign_in"),
        ("login_failed", "failed_sign_in"),
        ("login_2fa_failed", "failed_sign_in"),
        ("2fa_enabled", "two_factor"),
        ("2fa_disabled", "two_factor"),
        ("recovery_code_used", "two_factor"),
        ("password_reset_requested", "password"),
        ("password_reset_completed", "password"),
        ("client_mcp_key.create", "mcp_keys"),
        ("client_mcp_key.delete", "mcp_keys"),
    ],
)
def test_audit_events_map_to_categories(db_session, alice, action, category):
    opt_in(db_session, alice, category)
    audit(db_session, alice, action)
    out = rows(db_session, alice)
    assert len(out) == 1 and out[0].event == category and out[0].target == "alice@example.com"
    assert "203.0.113.9" in out[0].payload["text"]


def test_unmapped_audit_events_and_staff_only_events_are_ignored(db_session, alice):
    opt_in(db_session, alice, *cn.CATEGORIES)
    audit(db_session, alice, "client_mcp.account")
    audit(db_session, alice, "mcp.proxy_runner")
    audit(db_session, alice, "notification_prefs_changed")
    AuditService.log(db_session, actor_user_id=alice.id, action="login_failed")  # unknown account: no subject
    db_session.commit()
    assert rows(db_session) == []


def test_events_only_reach_their_own_subject(db_session, alice, bob):
    opt_in(db_session, alice, "sign_in")
    opt_in(db_session, bob, "sign_in", webhook=True)
    audit(db_session, alice, "login_success")
    assert [r.user_id for r in rows(db_session)] == [alice.id]
    assert rows(db_session, bob) == []


def test_admin_action_on_a_client_notifies_that_client_only(db_session, alice, bob, test_admin_user):
    opt_in(db_session, alice, "password")
    opt_in(db_session, bob, "password")
    AuditService.log(db_session, actor_user_id=test_admin_user.id, subject_user_id=alice.id,
                     action="password_set_by_admin")
    db_session.commit()
    out = rows(db_session)
    assert [r.user_id for r in out] == [alice.id]
    assert "admin" not in out[0].payload["text"].lower().replace("administr", "")


def test_staff_action_does_not_leak_the_staff_members_address(db_session, alice, test_admin_user):
    opt_in(db_session, alice, "two_factor", webhook=True)
    AuditService.log(db_session, actor_user_id=test_admin_user.id, subject_user_id=alice.id,
                     action="require_2fa_changed", ip="198.51.100.77", user_agent="StaffBrowser/9")
    db_session.commit()
    blob = json.dumps([r.payload for r in rows(db_session)])
    assert rows(db_session) and "198.51.100.77" not in blob and "StaffBrowser" not in blob


def test_audit_hook_failure_never_breaks_logging(db_session, alice, monkeypatch):
    from app.models.commerce_audit import UserAuditEvent

    opt_in(db_session, alice, "sign_in")

    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(cn, "notify", boom)
    audit(db_session, alice, "login_success")
    assert db_session.query(UserAuditEvent).filter(UserAuditEvent.action == "login_success").count() == 1


def test_service_suspended_and_terminated_are_caught_at_the_status_change(db_session, alice, bob):
    opt_in(db_session, alice, "service_suspended", "service_terminated")
    opt_in(db_session, bob, "service_suspended", "service_terminated")
    mine = make_vm(db_session, alice, "alice-vm")
    theirs = make_vm(db_session, bob, "bob-vm")
    mine.status = ServiceStatus.SUSPENDED
    db_session.commit()
    mine.status = ServiceStatus.ACTIVE
    db_session.commit()
    mine.status = ServiceStatus.TERMINATED
    db_session.commit()
    out = rows(db_session, alice)
    assert [r.event for r in out] == ["service_suspended", "service_terminated"]
    assert "alice-vm" in out[0].payload["text"]
    assert rows(db_session, bob) == []
    assert theirs.status == ServiceStatus.ACTIVE


def test_unchanged_or_other_status_changes_send_nothing(db_session, alice):
    opt_in(db_session, alice, "service_suspended", "service_terminated")
    svc = make_vm(db_session, alice, "vm")
    svc.name = "renamed"
    db_session.commit()
    svc.status = ServiceStatus.PENDING if hasattr(ServiceStatus, "PENDING") else ServiceStatus.ACTIVE
    db_session.commit()
    assert rows(db_session) == []


def test_service_without_owner_notifies_nobody(db_session):
    svc = Service(name="orphan", service_type=ServiceType.VM, status=ServiceStatus.ACTIVE)
    db_session.add(svc)
    db_session.commit()
    svc.status = ServiceStatus.SUSPENDED
    db_session.commit()
    assert rows(db_session) == []


def test_payloads_never_carry_infrastructure_detail(db_session, alice):
    opt_in(db_session, alice, *cn.CATEGORIES, webhook=True)
    svc = make_vm(db_session, alice, "alice-vm")
    svc.status = ServiceStatus.SUSPENDED
    db_session.commit()
    from app.models.alerting import VmBackupRun

    run = VmBackupRun(service_id=svc.id, upid="UPID:hv-secret-01:0001", status="failed",
                      started_at=datetime.utcnow(), finished_at=datetime.utcnow(),
                      error="vzdump failed on ceph-node-internal rack-internal bmc-secret")
    db_session.add(run)
    db_session.commit()
    cn.notify_backup_finished(db_session, run)
    audit(db_session, alice, "login_success")
    out = rows(db_session)
    assert len(out) >= 6
    blob = json.dumps([r.payload for r in out]) + " ".join(r.target for r in out)
    for secret in SECRET_NAMES:
        assert secret not in blob
    assert "UPID" not in blob and "vzdump" not in blob
    assert any("failed" in r.payload.get("text", r.payload.get("message", "")) for r in out)


def test_backup_and_install_notifications(db_session, alice, bob):
    from app.models.alerting import VmBackupRun
    from app.models.vm_deployment_job import DeploymentJobStatus, VMDeploymentJob

    opt_in(db_session, alice, "backup", "reinstall")
    opt_in(db_session, bob, "backup", "reinstall")
    svc = make_vm(db_session, alice, "alice-vm")
    ok = VmBackupRun(service_id=svc.id, upid="u", status="succeeded", started_at=datetime.utcnow(),
                     finished_at=datetime.utcnow())
    db_session.add(ok)
    db_session.commit()
    cn.notify_backup_finished(db_session, ok)
    job = VMDeploymentJob(service_id=svc.id, strategy_name="reinstall_vm", status=DeploymentJobStatus.SUCCEEDED,
                          finished_at=datetime.utcnow())
    restore = VMDeploymentJob(service_id=svc.id, strategy_name="restore_backup", status=DeploymentJobStatus.SUCCEEDED)
    db_session.add_all([job, restore])
    db_session.commit()
    cn.notify_job_finished(db_session, job, True)
    cn.notify_job_finished(db_session, restore, True)
    db_session.commit()
    out = rows(db_session)
    assert [r.event for r in out] == ["backup", "reinstall"] and {r.user_id for r in out} == {alice.id}


def test_rate_limit_per_client_and_channel(db_session, alice, bob, monkeypatch):
    from app.services.alerting import catalogue

    catalogue.update_global_settings(db_session, {"client_rate_limit_per_hour": 3})
    db_session.commit()
    opt_in(db_session, alice, "sign_in")
    opt_in(db_session, bob, "sign_in")
    for _ in range(6):
        audit(db_session, alice, "login_success")
    audit(db_session, bob, "login_success")
    assert len(rows(db_session, alice)) == 3
    assert len(rows(db_session, bob)) == 1


def test_email_switch_and_webhook_channel(db_session, alice):
    opt_in(db_session, alice, "sign_in", webhook=True, email=False)
    audit(db_session, alice, "login_success")
    out = rows(db_session)
    assert [r.channel_kind for r in out] == ["webhook"]
    body = out[0].payload
    assert body["category"] == "sign_in" and set(body["data"]) <= set(cn._ALLOWED_DATA)
    assert "alice" not in json.dumps(body)


def test_storage_quota_client_alert_notifies_owner_once(db_session, alice, bob):
    opt_in(db_session, alice, "storage_quota")
    opt_in(db_session, bob, "storage_quota")
    svc = Service(name="alice-store", service_type=ServiceType.NFS_STORAGE, status=ServiceStatus.ACTIVE,
                  owner_user_id=alice.id)
    db_session.add(svc)
    loc = Location(name="L", description="")
    db_session.add(loc)
    db_session.flush()
    server = Server(name="srv", server_ip="10.0.0.1", location_id=loc.id, plugin_name="ipmi", plugin_config={})
    db_session.add(server)
    db_session.flush()
    node = StorageNode(name="ceph-node-internal", server_id=server.id, api_key_hash="x", enabled=True)
    db_session.add(node)
    db_session.flush()
    tenant = StorageTenant(service_id=svc.id, node_id=node.id, size_bytes=1)
    db_session.add(tenant)
    db_session.flush()
    share = StorageShare(tenant_id=tenant.id, name="data", quota_bytes=1000, used_bytes=950)
    db_session.add(share)
    db_session.commit()

    for minute in (0, 1, 2):
        run_cycle(db_session, now=datetime(2026, 1, 1, 12, minute), only_types=["client_storage_quota"],
                  detectors={"client_storage_quota": d.client_storage_quota})
    alert = db_session.query(Alert).one()
    assert alert.audience == "client" and alert.owner_user_id == alice.id
    out = rows(db_session)
    assert [r.user_id for r in out] == [alice.id] and len(out) == 1
    assert "95%" in out[0].payload["text"] and "ceph-node-internal" not in out[0].payload["text"]
    # staff alert list never shows it
    from app.services.alerting import admin as alerts_admin

    assert alerts_admin.list_alerts(db_session, state="all")["total"] == 0
    assert d.client_storage_quota.__name__ and CATALOGUE["client_storage_quota"].audience == "client"


# ---------------------------------------------------------------- preferences and webhook safety


def public(host, port):
    return ["93.184.216.34"]


def test_preferences_validation(db_session, alice):
    with pytest.raises(cn.PreferenceError):
        cn.update_preferences(db_session, alice.id, categories={"nope": True})
    with pytest.raises(cn.PreferenceError):
        cn.update_preferences(db_session, alice.id, categories={"sign_in": "yes"})
    result = cn.update_preferences(db_session, alice.id, categories={"sign_in": True}, email_enabled=False)
    assert result["categories"]["sign_in"] is True and result["email_enabled"] is False
    assert set(result["categories"]) == set(cn.CATEGORIES)


def test_client_webhook_must_be_https_public_and_secret_shown_once(db_session, alice):
    for bad in ("http://hooks.example.org/x", "https://u:p@hooks.example.org/x", "ftp://x.example.org"):
        with pytest.raises(cn.PreferenceError):
            cn.update_preferences(db_session, alice.id, webhook_url=bad, resolver=public)
    with pytest.raises(cn.PreferenceError):
        cn.update_preferences(db_session, alice.id, webhook_url="https://internal.example.org/x",
                              resolver=lambda h, p: ["10.0.0.9"])
    with pytest.raises(cn.PreferenceError):
        cn.update_preferences(db_session, alice.id, webhook_url="https://169.254.169.254/latest")
    first = cn.update_preferences(db_session, alice.id, webhook_url="https://hooks.example.org/in/abc", resolver=public)
    secret = first["webhook"]["secret"]
    assert secret.startswith("whsec_") and first["webhook"]["url"] == "https://hooks.example.org/\u2026"
    again = cn.get_preferences(db_session, alice.id)
    assert "secret" not in again["webhook"] and "abc" not in json.dumps(again)
    row = db_session.query(ClientNotificationPref).one()
    assert unseal(row.webhook_secret) == secret
    rotated = cn.update_preferences(db_session, alice.id, regenerate_secret=True)
    assert rotated["webhook"]["secret"] != secret
    removed = cn.update_preferences(db_session, alice.id, webhook_url="")
    assert removed["webhook"]["configured"] is False


def test_client_webhook_delivery_is_strict_and_signed(db_session, alice):
    from app.services.alerting import safe_http
    from app.services.alerting.sender import DeliverySender, sign

    cn.update_preferences(db_session, alice.id, categories={"sign_in": True}, webhook_url="https://hooks.example.org/in",
                          resolver=public)
    secret = unseal(db_session.query(ClientNotificationPref).one().webhook_secret)
    audit(db_session, alice, "login_success")
    calls = []

    def post(url, body, headers, **kw):
        calls.append((url, body, headers, kw))
        return safe_http.HttpResult(200, "")

    now = datetime.utcnow() + timedelta(seconds=5)
    DeliverySender(http_post=post, clock=lambda: now).send_batch(db_session, owner="w")
    url, body, headers, kw = calls[0]
    assert kw["public_only"] is True and kw["https_only"] is True
    assert headers["X-Rackflow-Signature"] == sign(secret, body)


def test_failed_client_webhook_marks_a_safe_error(db_session, alice):
    from app.services.alerting import safe_http
    from app.services.alerting.sender import DeliverySender

    cn.update_preferences(db_session, alice.id, categories={"sign_in": True}, webhook_url="https://hooks.example.org/in",
                          resolver=public, email_enabled=False)
    audit(db_session, alice, "login_success")

    def post(url, body, headers, **kw):
        raise safe_http.WebhookUrlError("The URL must point to a public address")

    DeliverySender(http_post=post, clock=lambda: datetime.utcnow() + timedelta(seconds=5)).send_batch(db_session, owner="w")
    assert cn.get_preferences(db_session, alice.id)["webhook"]["error"] == "The last webhook delivery failed"


def test_client_email_is_delivered_through_the_sender(db_session, alice, monkeypatch):
    from app.services.alerting.sender import DeliverySender

    opt_in(db_session, alice, "sign_in")
    audit(db_session, alice, "login_success")
    sent = []
    monkeypatch.setattr(settings, "smtp_enabled", True)
    DeliverySender(email_sender=lambda **kw: sent.append(kw),
                   clock=lambda: datetime.utcnow() + timedelta(seconds=5)).send_batch(db_session, owner="w")
    assert sent[0]["to_address"] == "alice@example.com"
    assert "Account, Notifications" in sent[0]["body_text"]


# ---------------------------------------------------------------- HTTP API


def login(client, name, password="secret123"):
    resp = client.post("/api/users/login", json={"username": name, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def test_api_get_put_test_and_isolation(client, db_session, alice, bob, monkeypatch):
    monkeypatch.setattr(cn.safe_http, "_default_resolver", lambda host, port: ["93.184.216.34"])
    headers = login(client, "alice")
    got = client.get("/api/client/notifications", headers=headers)
    assert got.status_code == 200 and got.json()["categories"]["sign_in"] is False
    put = client.put("/api/client/notifications", headers=headers,
                     json={"categories": {"sign_in": True}, "webhook_url": "https://hooks.example.org/in"})
    assert put.status_code == 200, put.text
    assert put.json()["webhook"]["secret"].startswith("whsec_")
    bad = client.put("/api/client/notifications", headers=headers, json={"webhook_url": "http://hooks.example.org"})
    assert bad.status_code == 400
    assert client.put("/api/client/notifications", headers=headers, json={"categories": {"x": True}}).status_code == 400
    assert client.post("/api/client/notifications/test", headers=headers).json()["queued"] == 2
    bob_view = client.get("/api/client/notifications", headers=login(client, "bob")).json()
    assert bob_view["categories"]["sign_in"] is False and bob_view["webhook"]["configured"] is False
    from app.models.commerce_audit import UserAuditEvent

    assert db_session.query(UserAuditEvent).filter(UserAuditEvent.action == "notification_prefs_changed").count() >= 1


def test_api_requires_client_session(client, test_admin_user):
    assert client.get("/api/client/notifications").status_code in (401, 403)
    headers = login(client, "admin", "adminpassword123")
    assert client.get("/api/client/notifications", headers=headers).status_code == 403


def test_api_blocked_during_impersonation(client, db_session, alice, test_admin_user):
    from app.api import client_notifications as api

    with pytest.raises(Exception) as exc:
        api._own_session({"user_id": alice.id, "impersonated_by": test_admin_user.id})
    assert getattr(exc.value, "status_code", None) == 403


# ---------------------------------------------------------------- MCP


@pytest.mark.asyncio
async def test_client_mcp_account_notifications(db_session, alice, bob):
    from app.core.client_mcp_auth import ClientMcpAuthContext
    from app.mcp.client import tools
    from app.mcp.client.context import client_mcp_auth_ctx
    from tests.conftest import TestingSessionLocal
    import app.mcp.client.runtime as runtime

    runtime.SessionLocal = TestingSessionLocal

    def as_user(user, scopes):
        client_mcp_auth_ctx.set(
            ClientMcpAuthContext(key_id=1, name="t", scopes=scopes, user_id=user.id, username=user.username,
                                 email=user.email, client_ip="127.0.0.1")
        )

    from mcp.server.fastmcp.exceptions import ToolError

    try:
        as_user(alice, ["read"])
        view = await tools.account(view="notifications")
        assert view["categories"]["backup"] is False
        with pytest.raises(ToolError):
            await tools.account(view="notifications", notifications={"categories": {"backup": True}})
        as_user(alice, ["read", "write"])
        changed = await tools.account(view="notifications", notifications={"categories": {"backup": True}})
        assert changed["categories"]["backup"] is True
        with pytest.raises(ToolError):
            await tools.account(view="notifications", notifications={"webhook_url": "https://x.example.org"})
        as_user(bob, ["read"])
        assert (await tools.account(view="notifications"))["categories"]["backup"] is False
    finally:
        client_mcp_auth_ctx.set(None)
