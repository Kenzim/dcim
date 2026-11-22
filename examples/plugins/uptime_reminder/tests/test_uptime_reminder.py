from pathlib import Path

import pytest

from app.plugin_host.testkit import assert_valid_package
from app.plugin_host.ui import PluginRequestError

PLUGIN = Path(__file__).resolve().parents[1]


@pytest.fixture
def setup(rackflow):
    plugin = rackflow.install(PLUGIN, settings={"reminder_days": 10})
    owner = rackflow.make_user("client")
    service = rackflow.make_service(owner=owner)
    rackflow.deliver()
    return plugin, owner, service


def test_package_is_valid():
    assert_valid_package(PLUGIN)


def test_a_new_service_starts_a_reminder_period(rackflow, setup):
    plugin, _owner, service = setup
    state = plugin.state(service)
    assert state["status"] == "ok"
    badge = rackflow.panel(service, "status", "client")["elements"][0]
    assert (badge["text"], badge["tone"]) == ("Confirmed", "success")


def test_a_due_service_asks_the_client_and_posts_to_the_webhook(rackflow, setup):
    plugin, owner, service = setup
    plugin.configure(webhook_url="https://hooks.example.com/uptime", webhook_token="tok")
    rackflow.clock.advance(days=11)
    runs = rackflow.run_schedule(plugin, "check", service)
    assert [r.status for r in runs] == ["ok"]
    assert plugin.state(service)["status"] == "due"
    assert plugin.state(service)["reminders_sent"] == 1
    assert "Please confirm that you still need your service" in rackflow.client_notices(owner)[0]["subject"]
    request = rackflow.http.requests[0]
    assert request.headers["Authorization"] == "Bearer tok"
    assert request.json() == {"event": "reminder_due", "service_id": service.id}


def test_a_failing_webhook_is_logged_not_fatal(rackflow, setup):
    plugin, _owner, service = setup
    plugin.configure(webhook_url="https://hooks.example.com/uptime")
    rackflow.http.fail("hooks.example.com")
    rackflow.clock.advance(days=11)
    assert rackflow.run_schedule(plugin, "check", service)[0].status == "ok"
    assert any("Webhook failed" in m for m in plugin.log_messages("warning"))


def test_the_client_confirms_and_the_panel_changes(rackflow, setup):
    plugin, _owner, service = setup
    rackflow.clock.advance(days=11)
    rackflow.run_schedule(plugin, "check", service)
    assert rackflow.panel(service, "status", "client")["elements"][0]["text"] == "Please confirm"
    with pytest.raises(PluginRequestError) as needs_confirm:
        rackflow.run_action(plugin, service, "confirm", {"note": "Still here"}, as_="client")
    assert needs_confirm.value.status_code == 400
    result = rackflow.run_action(plugin, service, "confirm", {"note": "Still here"}, as_="client", confirm=True)
    assert result["message"].startswith("Thanks")
    state = plugin.state(service)
    assert (state["status"], state["note"]) == ("ok", "Still here")


def test_remind_now_is_for_admins_only(rackflow, setup):
    plugin, _owner, service = setup
    with pytest.raises(PluginRequestError) as forbidden:
        rackflow.run_action(plugin, service, "remind_now", as_="client")
    assert forbidden.value.status_code == 403
    rackflow.run_action(plugin, service, "remind_now")
    assert plugin.state(service)["status"] == "due"


def test_suspending_pauses_and_unsuspending_restarts(rackflow, setup):
    plugin, _owner, service = setup
    rackflow.set_status(service, "suspended")
    rackflow.deliver()
    assert plugin.state(service)["status"] == "paused"
    rackflow.set_status(service, "active")
    rackflow.deliver()
    assert plugin.state(service)["status"] == "ok"
