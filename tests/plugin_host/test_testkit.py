"""The plugin test kit (app/plugin_host/testkit) against the real host."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.plugin_host.catalogue import EVENT_LIST
from app.plugin_host.testkit import (
    Harness,
    assert_valid_package,
    check_package,
    sample_payload,
)
from app.plugin_host.testkit.fixtures import rackflow  # noqa: F401
from app.plugin_host.ui import PluginRequestError

MANIFEST = {
    "id": "kit_demo",
    "name": "Kit demo",
    "version": "1.0.0",
    "description": "Exercises every part of the kit.",
    "author": "Tests",
    "licence": "MIT",
    "rackflow_api": ">=1.0,<2.0",
    "entry": "plugin.py",
    "permissions": ["service.read", "billing.read", "events.veto", "http.outbound", "guest.exec", "vm.power", "ui.client",
                    "notify.client", "mcp.staff"],
    "services": {"types": ["vm"], "enablement": "opt_in"},
    "settings": [
        {"key": "hook_url", "label": "Hook", "type": "url"},
        {"key": "token", "label": "Token", "type": "secret"},
        {"key": "greeting", "label": "Greeting", "type": "string", "default": "hello"},
    ],
    "events": ["service.suspended", "invoice.paid"],
    "hooks": ["service.power_requested"],
    "schedules": [{"id": "tick", "every": "1h", "scope": "service"}],
    "actions": [
        {"id": "greet", "label": "Greet", "audience": ["admin", "client"], "params": [
            {"key": "who", "label": "Who", "type": "string"}]},
        {"id": "refuse", "label": "Refuse", "audience": ["admin"]},
    ],
    "ui": {"panels": [{
        "id": "main", "title": "Demo", "audience": ["admin", "client"],
        "elements": [
            {"type": "field", "label": "Last greeting", "state": "greeting", "empty": "none"},
            {"type": "action", "action": "greet"},
        ],
    }]},
    "mcp_tools": [{"id": "peek", "description": "Peek at state", "params": [
        {"key": "service_id", "label": "Service", "type": "integer", "required": True}]}],
}

CODE = '''
def register(plugin):
    @plugin.on("service.suspended")
    def suspended(ctx, event):
        ctx.state(event["service_id"]).set("suspended_at", ctx.now().isoformat())

    @plugin.on("invoice.paid")
    async def paid(ctx, event):
        url = ctx.settings.get("hook_url")
        if not url:
            raise RuntimeError("no hook configured")
        token = ctx.settings.get("token")
        await ctx.http.post(url, json={"invoice": event["payload"]["invoice_id"]}, headers={"X-Token": token})

    @plugin.before("service.power_requested")
    def guard(ctx, event):
        if event["payload"]["action"] == "off":
            return ctx.veto("Stopping is not allowed")

    @plugin.schedule("tick")
    async def tick(ctx, service):
        ctx.state(service["id"]).set("ticks", int(ctx.state(service["id"]).get("ticks", 0)) + 1)
        ctx.notify.client(service["id"], "tick", key=f"t{ctx.now().isoformat()}")
        if await ctx.guest.ping(service["id"]):
            out = await ctx.guest.exec(service["id"], ["uname", "-a"])
            ctx.state(service["id"]).set("uname", out["stdout"])

    @plugin.action("greet")
    def greet(ctx, request):
        who = request["params"].get("who") or "world"
        ctx.state(request["service"]["id"]).set("greeting", f"{ctx.settings.get('greeting')} {who}")
        return {"message": "Greeted"}

    @plugin.action("refuse")
    def refuse(ctx, request):
        raise ctx.ActionError("Nope")

    @plugin.mcp_tool("peek")
    def peek(ctx, params):
        return ctx.state(params["service_id"]).all()
'''


@pytest.fixture
def package(tmp_path):
    folder = tmp_path / "src" / "kit_demo"
    folder.mkdir(parents=True)
    (folder / "plugin.json").write_text(json.dumps(MANIFEST))
    (folder / "plugin.py").write_text(CODE)
    return folder


def test_sample_payload_is_complete_for_every_event():
    for spec in EVENT_LIST:
        payload = sample_payload(spec.name)
        assert set(spec.required) <= set(payload), spec.name
        assert set(payload) <= set(spec.required + spec.optional), spec.name


def test_sample_payload_overrides_and_unknown_fields():
    assert sample_payload("ticket.created", subject="Help")["subject"] == "Help"
    with pytest.raises(KeyError):
        sample_payload("ticket.created", colour="red")


def test_package_checks_report_every_problem(tmp_path):
    folder = tmp_path / "wrong_name"
    folder.mkdir()
    (folder / "plugin.json").write_text(json.dumps(dict(MANIFEST, rackflow_api=">=2.0,<3.0")))
    (folder / "plugin.py").write_text("def register(plugin):\n    pass\n")
    report = check_package(folder)
    assert not report.ok
    text = report.format()
    assert "directory name must equal the plugin id" in text
    assert "needs Rackflow plugin API" in text
    assert not report.supports("1.0")
    assert report.supports("2.1")


def test_package_check_finds_missing_and_undeclared_handlers(package):
    (package / "plugin.py").write_text("def register(plugin):\n    @plugin.on('ticket.created')\n    def x(ctx, e):\n        pass\n")
    report = check_package(package)
    assert any("ticket.created" in e and "not declared" in e for e in report.errors)
    assert any("is declared but has no handler" in e for e in report.errors)


def test_valid_package_has_no_warnings(package):
    report = assert_valid_package(package)
    assert report.warnings == []
    assert report.supports("1.0")


def test_install_enable_and_settings(rackflow, package):
    plugin = rackflow.install(package, settings={"token": "s3cret", "hook_url": "https://hooks.example.com/a"})
    assert plugin.status == "enabled"
    assert plugin.settings()["token"] != "s3cret"
    assert plugin.secret("token") == "s3cret"
    assert plugin.settings()["greeting"] == "hello"
    with pytest.raises(Exception, match="must be an http or https URL"):
        plugin.configure(hook_url="ftp://x")


def test_install_refuses_a_folder_named_differently(rackflow, package, tmp_path):
    renamed = tmp_path / "other" / "renamed_folder"
    renamed.parent.mkdir()
    package.rename(renamed)
    with pytest.raises(Exception, match="directory name must equal"):
        rackflow.install(renamed)


def test_service_events_come_from_the_real_listener(rackflow, package):
    plugin = rackflow.install(package)
    service = rackflow.make_service()
    plugin.activate(service)
    rackflow.set_status(service, "suspended")
    counts = rackflow.deliver()
    assert counts.get("delivered") == 1
    assert "suspended_at" in plugin.state(service)


def test_events_skip_services_the_plugin_is_not_active_for(rackflow, package):
    rackflow.install(package)
    service = rackflow.make_service()
    rackflow.set_status(service, "suspended")
    rackflow.deliver()
    result = rackflow.deliveries(event="service.suspended")
    assert [d.status for d in result] == ["skipped"]


def test_fire_posts_through_the_fake_http_with_the_secret(rackflow, package):
    rackflow.install(package, settings={"token": "s3cret", "hook_url": "https://hooks.example.com/a"})
    rackflow.http.respond("hooks.example.com", 204)
    result = rackflow.fire("invoice.paid", invoice_id=77)
    assert len(result.delivered) == 1 and not result.failed
    request = rackflow.http.requests[0]
    assert request.method == "POST" and request.host == "hooks.example.com"
    assert request.json() == {"invoice": 77}
    assert request.headers["X-Token"] == "s3cret"


def test_http_rules_still_apply_to_the_fake(rackflow, package):
    plugin = rackflow.install(package, settings={"hook_url": "http://hooks.example.com/a"})
    result = rackflow.fire("invoice.paid")
    assert result.failed and "https" in result.errors[0]
    plugin.configure(hook_url="https://hooks.example.com/a")
    rackflow.http.resolve("hooks.example.com", "10.0.0.5")
    result = rackflow.fire("invoice.paid")
    assert "public address" in result.errors[-1]
    assert rackflow.http.requests == []


def test_a_failing_handler_is_recorded_and_counted(rackflow, package):
    plugin = rackflow.install(package)
    result = rackflow.fire("invoice.paid")
    assert result.failed[0].status == "failed"
    assert "no hook configured" in result.errors[0]
    assert plugin.installation.consecutive_failures == 1
    assert any("no hook configured" in m for m in plugin.log_messages("error"))


def test_before_hook_veto(rackflow, package):
    plugin = rackflow.install(package)
    service = rackflow.make_service()
    plugin.activate(service)
    assert rackflow.request_power(service, "reboot") is None
    assert rackflow.request_power(service, "off") == "Kit demo: Stopping is not allowed"
    assert any(e["action"] == "plugin.veto" for e in rackflow.audit_entries("plugin.veto"))


def test_schedule_runs_per_service_with_fake_guest_and_notice(rackflow, package):
    plugin = rackflow.install(package)
    owner = rackflow.make_user("owner")
    service = rackflow.make_service(owner=owner)
    plugin.activate(service)
    rackflow.vm.on_exec = lambda argv: {"stdout": "Linux test"}
    runs = rackflow.run_schedule(plugin, "tick", service)
    assert [(r.status, r.error) for r in runs] == [("ok", None)]
    state = plugin.state(service)
    assert state["ticks"] == 1 and state["uname"] == "Linux test"
    assert rackflow.vm.commands == [["uname", "-a"]]
    assert len(rackflow.client_notices(owner)) == 1


def test_clock_moves_what_the_plugin_sees(rackflow, package):
    plugin = rackflow.install(package)
    service = rackflow.make_service()
    plugin.activate(service)
    rackflow.clock.set(datetime(2030, 1, 2, 3, 4, tzinfo=timezone.utc))
    rackflow.set_status(service, "suspended")
    rackflow.deliver()
    assert plugin.state(service)["suspended_at"].startswith("2030-01-02T03:04")


def test_panels_actions_and_audiences(rackflow, package):
    plugin = rackflow.install(package)
    service = rackflow.make_service()
    plugin.activate(service)
    panel = rackflow.panel(service, "main", "client")
    assert panel["elements"][0] == {"type": "field", "label": "Last greeting", "value": "none"}
    result = rackflow.run_action(plugin, service, "greet", {"who": "Ada"}, as_="client")
    assert result["message"] == "Greeted"
    refreshed = rackflow.panel(service, "main", "client")
    assert refreshed["elements"][0]["value"] == "hello Ada"
    with pytest.raises(PluginRequestError) as refused:
        rackflow.run_action(plugin, service, "refuse")
    assert (refused.value.status_code, refused.value.detail) == (409, "Nope")
    with pytest.raises(PluginRequestError) as forbidden:
        rackflow.run_action(plugin, service, "refuse", as_="client")
    assert forbidden.value.status_code == 403


def test_mcp_tool(rackflow, package):
    plugin = rackflow.install(package)
    service = rackflow.make_service()
    plugin.activate(service)
    rackflow.run_action(plugin, service, "greet", {})
    assert rackflow.run_tool(plugin, "peek", {"service_id": service.id}) == {"greeting": "hello world"}


def test_upgrade_and_uninstall(rackflow, package):
    plugin = rackflow.install(package)
    newer = dict(MANIFEST, version="1.1.0")
    (package / "plugin.json").write_text(json.dumps(newer))
    plugin.upgrade(package)
    assert plugin.installation.version == "1.1.0" and plugin.status == "enabled"
    plugin.uninstall()
    assert plugin.installation is None


def test_each_test_starts_clean(rackflow):
    assert rackflow.deliveries() == []
    assert isinstance(rackflow, Harness)
    assert Path(rackflow.plugins_dir).is_dir()
