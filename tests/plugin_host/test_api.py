"""HTTP API, guest exec, MCP and the sample plugin end to end."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.dao.billing_integration_dao import BillingIntegrationDAO
from app.dao.user_dao import UserDAO
from app.models.commerce_audit import UserAuditEvent
from app.models.plugin_host import PluginInstallation, PluginServiceLink
from app.models.service import ServiceStatus, ServiceType
from app.plugin_host import outbox, runtime, scheduler, sdk, state
from tests.plugin_host.helpers import base_manifest, copy_example, install, make_service, make_user, run, write_plugin


def login(client, username, password):
    resp = client.post("/api/users/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def admin_headers(client):
    return login(client, "admin", "adminpassword123")


@pytest.fixture
def sample(client, db_session, plugin_env):
    copy_example(plugin_env)
    return plugin_env


def _client_user(db):
    return make_user(db, "clientuser")


def _client_headers(client):
    return login(client, "clientuser", "password12345")


def _actions(panel):
    return {e["action_id"]: e for e in panel["elements"] if e["type"] == "action"}


# ------------------------------------------------------------------ admin management


def test_admin_api_requires_admin(client, test_user, sample):
    headers = login(client, "testuser", "testpassword123")
    assert client.get("/api/admin/plugins", headers=headers).status_code == 403
    assert client.get("/api/admin/plugins").status_code in (401, 403)


def test_admin_install_flow(client, db_session, test_admin_user, sample):
    headers = admin_headers(client)
    listing = client.get("/api/admin/plugins", headers=headers).json()
    assert listing["api_version"] == "1.0" and listing["installed"] == []
    available = listing["available"][0]
    assert available["id"] == "uptime_reminder" and available["valid"]
    keys = [p["key"] for p in available["permissions"]]
    assert "notify.client" in keys

    resp = client.post("/api/admin/plugins/install", headers=headers, json={"directory": "uptime_reminder"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "permissions_not_accepted"

    resp = client.post("/api/admin/plugins/install", headers=headers,
                       json={"directory": "uptime_reminder", "accept_permissions": keys, "enable": True})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "enabled" and body["health"]["state"] == "ok"
    assert {a["id"] for a in body["actions"]} == {"confirm", "remind_now"}

    resp = client.get("/api/admin/plugins/uptime_reminder/settings", headers=headers)
    assert resp.json()["values"]["reminder_days"] == 30
    resp = client.put("/api/admin/plugins/uptime_reminder/settings", headers=headers,
                      json={"values": {"reminder_days": 14, "webhook_token": "tok"}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["values"]["webhook_token"] == "\u2022" * 8
    resp = client.put("/api/admin/plugins/uptime_reminder/settings", headers=headers, json={"values": {"reminder_days": -1}})
    assert resp.status_code == 400

    resp = client.put("/api/admin/plugins/uptime_reminder/addon-refs", headers=headers, json={"addon_refs": ["whmcs:7"]})
    assert resp.json()["addon_refs"] == ["whmcs:7"]
    assert client.put("/api/admin/plugins/uptime_reminder/addon-refs", headers=headers,
                      json={"addon_refs": ["bad"]}).status_code == 400

    for path in ("logs", "deliveries", "schedules", "services"):
        assert client.get(f"/api/admin/plugins/uptime_reminder/{path}", headers=headers).status_code == 200
    assert client.post("/api/admin/plugins/uptime_reminder/disable", headers=headers).json()["status"] == "disabled"
    assert client.post("/api/admin/plugins/uptime_reminder/enable", headers=headers).json()["status"] == "enabled"
    actions = {e.action for e in db_session.execute(select(UserAuditEvent)).scalars()}
    assert {"plugin.install", "plugin.settings", "plugin.disable", "plugin.enable", "plugin.addon_refs"} <= actions
    assert client.post("/api/admin/plugins/uptime_reminder/uninstall", headers=headers, json={}).json() == {"ok": True}
    assert db_session.get(PluginInstallation, "uptime_reminder") is None


def test_admin_service_links(client, db_session, test_admin_user, sample):
    headers = admin_headers(client)
    install(db_session, "uptime_reminder")
    owner = _client_user(db_session)
    service = make_service(db_session, owner)
    resp = client.put("/api/admin/plugins/uptime_reminder/services", headers=headers,
                      json={"items": [{"service_id": service.id, "enabled": False}]})
    assert resp.status_code == 200, resp.text
    item = resp.json()["items"][0]
    assert item["enabled"] is False and item["active"] is False and item["source"] == "admin"
    resp = client.put("/api/admin/plugins/uptime_reminder/services", headers=headers,
                      json={"items": [{"service_id": service.id, "enabled": None}]})
    assert resp.json()["items"][0]["active"] is True


# ------------------------------------------------------------------ the sample end to end


def test_sample_end_to_end(client, db_session, test_admin_user, sample, monkeypatch):
    install(db_session, "uptime_reminder")
    owner = _client_user(db_session)
    service = make_service(db_session, owner)
    run(outbox.run_once(owner="t"))
    run(scheduler.run_once(owner="t"))

    client_headers = _client_headers(client)
    doc = client.get(f"/api/client/services/{service.id}/plugin-ui", headers=client_headers).json()
    assert doc["audience"] == "client"
    panel = doc["panels"][0]
    assert panel["plugin_id"] == "uptime_reminder" and panel["health"] == "ok"
    badge = next(e for e in panel["elements"] if e["type"] == "badge")
    assert badge == {"type": "badge", "label": "Status", "text": "Confirmed", "tone": "success"}
    assert set(_actions(panel)) == {"confirm"}
    assert not any(e.get("label") == "Reminders sent" for e in panel["elements"])
    last = next(e for e in panel["elements"] if e.get("label") == "Last confirmed")
    assert last["value"].endswith(" UTC")

    admin_doc = client.get(f"/api/admin/services/{service.id}/plugin-ui", headers=admin_headers(client)).json()
    assert set(_actions(admin_doc["panels"][0])) == {"confirm", "remind_now"}

    url = f"/api/client/services/{service.id}/plugin-actions/uptime_reminder/confirm"
    resp = client.post(url, headers=client_headers, json={"params": {"note": "still used"}})
    assert resp.status_code == 400 and "Confirmation" in resp.json()["detail"]
    resp = client.post(url, headers=client_headers, json={"params": {"note": "still used"}, "confirm": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] and "recorded" in resp.json()["message"]
    note = next(e for e in resp.json()["panels"][0]["elements"] if e.get("label") == "Client note")
    assert note["value"] == "still used"
    resp = client.post(url, headers=client_headers, json={"params": {"bogus": 1}, "confirm": True})
    assert resp.status_code == 400
    resp = client.post(f"/api/client/services/{service.id}/plugin-actions/uptime_reminder/remind_now",
                       headers=client_headers, json={})
    assert resp.status_code == 403

    sent = []
    monkeypatch.setattr("app.services.client_notifications.notify",
                        lambda db, user_id, category, message, **kw: sent.append((user_id, category)) or 1)
    sent.clear()
    resp = client.post(f"/api/admin/services/{service.id}/plugin-actions/uptime_reminder/remind_now",
                       headers=admin_headers(client), json={})
    assert resp.status_code == 200, resp.text
    assert [s for s in sent if s[1] == "plugin"] == [(owner.id, "plugin")]
    badge = next(e for e in resp.json()["panels"][0]["elements"] if e["type"] == "badge")
    assert badge["text"] == "Please confirm"
    audit_actions = [e.action for e in db_session.execute(select(UserAuditEvent)).scalars()]
    assert "plugin.action" in audit_actions and "plugin.notify_client" in audit_actions


def test_client_cannot_see_other_services(client, db_session, sample):
    install(db_session, "uptime_reminder")
    _client_user(db_session)
    other = make_user(db_session, "someoneelse")
    service = make_service(db_session, other)
    headers = _client_headers(client)
    assert client.get(f"/api/client/services/{service.id}/plugin-ui", headers=headers).status_code == 404
    resp = client.post(f"/api/client/services/{service.id}/plugin-actions/uptime_reminder/confirm", headers=headers,
                       json={"confirm": True})
    assert resp.status_code == 404


def test_panels_hidden_without_ui_client(client, db_session, plugin_env):
    manifest = base_manifest(
        permissions=["service.read"],
        services={"types": ["vm"], "enablement": "all"},
        ui={"panels": [{"id": "p", "title": "Staff only", "audience": ["admin"],
                        "elements": [{"type": "field", "label": "X", "value": 1, "format": "number"}]}]},
    )
    write_plugin(plugin_env, manifest, "def register(plugin):\n    pass\n")
    install(db_session, "demo_plugin")
    owner = _client_user(db_session)
    service = make_service(db_session, owner)
    from app.plugin_host import ui

    assert ui.render(db_session, service, "client")["panels"] == []
    admin = ui.render(db_session, service, "admin")["panels"][0]
    assert admin["elements"] == [{"type": "field", "label": "X", "value": "1"}]


def test_format_values():
    from app.plugin_host.ui import format_value

    assert format_value(1234567, "number") == "1,234,567"
    assert format_value(True, "boolean") == "Yes"
    assert format_value(7500, "duration") == "2 h 5 min"
    assert format_value(1536 * 1024 * 1024, "bytes") == "1.5 GB"
    assert format_value("2026-11-21T10:05:00Z", "datetime") == "2026-11-21 10:05 UTC"
    assert format_value("2026-11-21T10:05:00Z", "date") == "2026-11-21"
    assert format_value(None, "text") is None


# ------------------------------------------------------------------ billing (WHMCS)


def _billing_service(db):
    integration = BillingIntegrationDAO.create(db, name="whmcs-plugins", integration_type="whmcs")
    user = UserDAO.create(db, username="whmcs-owner", email="whmcs-owner@example.test",
                          billing_integration_id=integration.id, external_user_id="42")
    service = make_service(db, user)
    return service, {"Authorization": f"Bearer {integration.plaintext_api_key}"}


def test_billing_render_action_and_addons(client, db_session, plugin_env):
    copy_example(plugin_env)
    manifest = json.loads((plugin_env / "uptime_reminder" / "plugin.json").read_text())
    manifest["services"]["enablement"] = "addon"
    (plugin_env / "uptime_reminder" / "plugin.json").write_text(json.dumps(manifest))
    inst = install(db_session, "uptime_reminder")
    inst.addon_refs = ["whmcs:12"]
    db_session.commit()
    service, headers = _billing_service(db_session)
    url = f"/api/billing/services/{service.id}/plugin-ui"
    assert client.get(url, headers=headers).json()["panels"] == []

    resp = client.post(f"/api/billing/services/{service.id}/addons", headers=headers,
                       json={"addon_ref": "whmcs:12", "status": "active"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["plugins"][0]["enabled"] is True
    doc = client.get(url, headers=headers).json()
    assert doc["panels"][0]["plugin_id"] == "uptime_reminder"
    assert "remind_now" not in _actions(doc["panels"][0])
    admin_doc = client.get(url + "?audience=admin", headers=headers).json()
    labels = {e.get("label") for e in admin_doc["panels"][0]["elements"]}
    assert "Reminders sent" in labels
    assert "Reminders sent" not in {e.get("label") for e in doc["panels"][0]["elements"]}

    resp = client.post(f"/api/billing/services/{service.id}/plugin-actions/uptime_reminder/confirm?audience=client",
                       headers=headers, json={"confirm": True})
    assert resp.status_code == 200, resp.text

    resp = client.post(f"/api/billing/services/{service.id}/addons", headers=headers,
                       json={"addon_ref": "whmcs:12", "status": "terminated"})
    assert resp.json()["plugins"][0]["enabled"] is False
    assert client.get(url, headers=headers).json()["panels"] == []
    link = db_session.execute(select(PluginServiceLink)).scalar_one()
    assert link.source == "addon" and link.addon_ref == "whmcs:12"


def test_billing_cannot_reach_other_integrations(client, db_session, sample):
    install(db_session, "uptime_reminder")
    _service, headers = _billing_service(db_session)
    owner = _client_user(db_session)
    foreign = make_service(db_session, owner)
    assert client.get(f"/api/billing/services/{foreign.id}/plugin-ui", headers=headers).status_code in (403, 404)
    assert client.get(f"/api/billing/services/{foreign.id}/plugin-ui").status_code in (401, 403)


# ------------------------------------------------------------------ guest exec and power


GUEST_CODE = """
def register(plugin):
    @plugin.action("run")
    async def run(ctx, request):
        out = await ctx.guest.exec(request["service"]["id"], ["cmd", "/c", "secret"], sensitive=True)
        ctx.state(request["service"]["id"]).set("out", out["stdout"])
        return {"message": out["stdout"]}

    @plugin.action("reboot")
    async def reboot(ctx, request):
        await ctx.vm.power(request["service"]["id"], "reboot")
        return {"message": "ok"}
"""


def guest_manifest(permissions):
    return base_manifest(
        permissions=permissions,
        services={"types": ["vm"], "enablement": "all"},
        actions=[{"id": "run", "label": "Run"}, {"id": "reboot", "label": "Reboot"}],
    )


class FakeVm:
    def __init__(self):
        self.calls = []

    async def guest_exec(self, argv, input_data=None, max_wait=60.0):
        self.calls.append(argv)
        return {"exitcode": 0, "out-data": "Licensed", "err-data": ""}

    async def guest_agent_ready(self):
        return True

    async def power_reset(self):
        self.calls.append("reset")
        return True


def _staff_actor():
    return {"type": "staff", "user_id": None, "via": "admin"}


def test_guest_exec_is_permission_gated(plugin_env, db_session, monkeypatch):
    from app.plugin_host import ui

    write_plugin(plugin_env, guest_manifest(["service.read"]), GUEST_CODE)
    install(db_session, "demo_plugin")
    owner = make_user(db_session, "gowner")
    service = make_service(db_session, owner)
    vm = FakeVm()
    monkeypatch.setattr(sdk, "resolve_vm", AsyncMock(return_value=vm))
    with pytest.raises(ui.PluginRequestError) as info:
        run(ui.run_action(db_session, service, "demo_plugin", "run", params={}, confirm=False, actor=_staff_actor()))
    assert info.value.status_code == 503
    assert vm.calls == []


def test_guest_exec_audit_and_rate_limit(plugin_env, db_session, monkeypatch):
    from app.core.config import settings as app_settings
    from app.plugin_host import ui

    monkeypatch.setattr(app_settings, "plugin_guest_exec_per_minute", 2)
    write_plugin(plugin_env, guest_manifest(["service.read", "guest.exec", "vm.power"]), GUEST_CODE)
    install(db_session, "demo_plugin")
    owner = make_user(db_session, "gowner2")
    service = make_service(db_session, owner)
    vm = FakeVm()
    monkeypatch.setattr(sdk, "resolve_vm", AsyncMock(return_value=vm))
    monkeypatch.setattr(ui, "ACTIONS_PER_MINUTE", 100)

    def go(action="run"):
        return run(ui.run_action(db_session, service, "demo_plugin", action, params={}, confirm=False,
                                 actor=_staff_actor()))

    assert go()["message"] == "Licensed"
    go()
    with pytest.raises(ui.PluginRequestError):
        go()
    assert len(vm.calls) == 2
    entries = [e for e in db_session.execute(select(UserAuditEvent)).scalars() if e.action == "plugin.guest_exec"]
    assert len(entries) == 2
    text = json.dumps(entries[0].event_metadata)
    assert "secret" not in text and "sha256:" in text
    assert go("reboot")["message"] == "ok"
    assert vm.calls[-1] == "reset"

    service.status = ServiceStatus.SUSPENDED
    db_session.commit()
    with pytest.raises(ui.PluginRequestError):
        go()


def test_guest_exec_refuses_non_vm(plugin_env, db_session, monkeypatch):
    write_plugin(plugin_env, base_manifest(permissions=["service.read", "guest.exec"],
                                           services={"types": ["bare_metal"], "enablement": "all"}),
                 "def register(plugin):\n    pass\n")
    inst = install(db_session, "demo_plugin")
    owner = make_user(db_session, "gowner3")
    service = make_service(db_session, owner, service_type=ServiceType.BARE_METAL)
    ctx = sdk.PluginContext(db_session, plugin_id=inst.id, plugin_version="1.0.0",
                            manifest=runtime.manifest_of(inst), granted=set(inst.granted_permissions), kind="test")
    with pytest.raises(sdk.NotActive):
        asyncio.run(ctx.guest.exec(service.id, ["x"]))
    ctx2 = sdk.PluginContext(db_session, plugin_id=inst.id, plugin_version="1.0.0",
                             manifest=runtime.manifest_of(inst), granted={"service.read"}, kind="test")
    with pytest.raises(sdk.PermissionDenied):
        asyncio.run(ctx2.guest.exec(service.id, ["x"]))
    with pytest.raises(sdk.PermissionDenied):
        ctx2.notify.client(service.id, "hi", key="k")


VETO_CODE = """
def register(plugin):
    @plugin.before("service.power_requested")
    def check(ctx, event):
        return ctx.veto("Maintenance in progress")
"""


def test_power_veto_returns_409(client, db_session, test_admin_user, plugin_env, monkeypatch):
    manifest = base_manifest(permissions=["service.read", "events.veto"],
                             services={"types": ["vm"], "enablement": "all"}, hooks=["service.power_requested"])
    write_plugin(plugin_env, manifest, VETO_CODE)
    install(db_session, "demo_plugin")
    owner = _client_user(db_session)
    service = make_service(db_session, owner)
    vm = FakeVm()
    monkeypatch.setattr("app.api.services_admin._admin_get_vm_plugin", AsyncMock(return_value=(vm, 1, "n", 100)))
    resp = client.post(f"/api/admin/services/{service.id}/vm/power", headers=admin_headers(client),
                       json={"action": "reboot"})
    assert resp.status_code == 409, resp.text
    assert vm.calls == []
    assert "Maintenance in progress" in resp.json()["detail"]


# ------------------------------------------------------------------ MCP


def test_staff_mcp_listing_unchanged_by_plugins(db_session, plugin_env):
    from app.mcp import server  # noqa: F401
    from app.mcp.instance import mcp

    def listing():
        tools = asyncio.run(mcp.list_tools())
        return json.dumps([t.model_dump(mode="json", exclude_none=True) for t in tools], separators=(",", ":"))

    before = listing()
    copy_example(plugin_env)
    install(db_session, "uptime_reminder")
    assert listing() == before
    plugins_tool = next(t for t in asyncio.run(mcp.list_tools()) if t.name == "plugins")
    assert len(json.dumps(plugins_tool.model_dump(mode="json", exclude_none=True), separators=(",", ":"))) < 700


def test_staff_mcp_plugins_tool(db_session, plugin_env, monkeypatch):
    from app.core.mcp_auth import McpAuthContext
    from app.mcp.context import mcp_auth_ctx
    from app.mcp.tools.plugins import plugins
    from tests.conftest import TestingSessionLocal

    monkeypatch.setattr("app.mcp.runtime.SessionLocal", TestingSessionLocal)
    token = mcp_auth_ctx.set(McpAuthContext(key_id=1, name="t", scopes=["read"], created_by_user_id=1,
                                            client_ip="127.0.0.1"))
    try:
        _staff_mcp_checks(db_session, plugin_env, plugins)
    finally:
        mcp_auth_ctx.reset(token)


def _staff_mcp_checks(db_session, plugin_env, plugins):

    copy_example(plugin_env)
    install(db_session, "uptime_reminder")
    owner = make_user(db_session, "mcpowner")
    service = make_service(db_session, owner)
    run(outbox.run_once(owner="t"))
    listed = run(plugins("list"))
    assert listed["items"][0]["id"] == "uptime_reminder"
    tools = run(plugins("tools", plugin_id="uptime_reminder"))
    assert tools["items"][0]["name"] == "uptime_reminder.status"
    result = run(plugins("call", plugin_id="uptime_reminder", tool="status", params={"service_id": service.id}))
    assert result["result"]["status"] == "ok"
    panel = run(plugins("panel", service_id=service.id))
    assert panel["panels"][0]["plugin_id"] == "uptime_reminder"
    db_session.expire_all()
    assert state.read_service_state(db_session, "uptime_reminder", service.id)["status"] == "ok"
