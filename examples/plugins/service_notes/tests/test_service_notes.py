from pathlib import Path

import pytest

from app.plugin_host.testkit import assert_valid_package
from app.plugin_host.ui import PluginRequestError

PLUGIN = Path(__file__).resolve().parents[1]


@pytest.fixture
def setup(rackflow):
    plugin = rackflow.install(PLUGIN)
    owner = rackflow.make_user("client")
    service = rackflow.make_service(owner=owner)
    return plugin, owner, service


def elements(rackflow, service, audience):
    """The panel's elements by (type, label); an action is keyed by its id."""
    return {
        (e["type"], e.get("action_id") or e.get("label")): e for e in rackflow.panel(service, "notes", audience)["elements"]
    }


def test_package_is_valid():
    assert_valid_package(PLUGIN)


def test_a_new_service_shows_an_empty_panel(rackflow, setup):
    _plugin, _owner, service = setup
    shown = elements(rackflow, service, "client")
    assert shown[("badge", "Review")]["text"] == "No note yet"
    assert shown[("field", "Note")]["value"] == "None"
    assert ("action", "save_note") in shown
    assert ("action", "clear_note") not in shown, "nothing to clear yet"
    assert ("action", "mark_reviewed") not in shown, "staff only"


def test_the_client_saves_a_note_and_the_panel_shows_it(rackflow, setup):
    plugin, _owner, service = setup
    result = rackflow.run_action(plugin, service, "save_note", {"label": "Shop", "note": "Runs the web shop"}, as_="client")
    assert result["message"] == "Saved."
    shown = elements(rackflow, service, "client")
    assert shown[("badge", "Review")]["text"] == "Up to date"
    assert (shown[("field", "Label")]["value"], shown[("field", "Note")]["value"]) == ("Shop", "Runs the web shop")
    assert ("action", "clear_note") in shown
    assert ("field", "Changed by") not in shown, "staff only"
    assert elements(rackflow, service, "admin")[("field", "Changed by")]["value"] == "client"
    assert [e["action"] for e in rackflow.audit_entries("plugin.service_notes")] == ["plugin.service_notes.note_saved"]


def test_the_form_is_prefilled_from_state(rackflow, setup):
    plugin, _owner, service = setup
    rackflow.run_action(plugin, service, "save_note", {"label": "Shop", "note": "Hello"})
    save = elements(rackflow, service, "client")[("action", "save_note")]
    assert {p["key"]: p["default"] for p in save["params"]} == {"label": "Shop", "note": "Hello"}


def test_an_empty_save_is_refused_with_a_message(rackflow, setup):
    plugin, _owner, service = setup
    with pytest.raises(PluginRequestError) as refused:
        rackflow.run_action(plugin, service, "save_note", {"label": " ", "note": ""}, as_="client")
    assert (refused.value.status_code, refused.value.detail) == (409, "Enter a label or a note, or use Clear note")


def test_params_are_validated_before_the_handler_runs(rackflow, setup):
    plugin, _owner, service = setup
    with pytest.raises(PluginRequestError) as refused:
        rackflow.run_action(plugin, service, "save_note", {"note": "x" * 501}, as_="client")
    assert refused.value.status_code == 400
    assert plugin.state(service) == {}


def test_clearing_asks_for_confirmation(rackflow, setup):
    plugin, _owner, service = setup
    rackflow.run_action(plugin, service, "save_note", {"note": "Keep me"})
    with pytest.raises(PluginRequestError) as unconfirmed:
        rackflow.run_action(plugin, service, "clear_note", as_="client")
    assert unconfirmed.value.status_code == 400
    assert plugin.state(service)["note"] == "Keep me"
    rackflow.run_action(plugin, service, "clear_note", as_="client", confirm=True)
    assert plugin.state(service) == {}


def test_a_client_cannot_use_a_staff_action(rackflow, setup):
    plugin, _owner, service = setup
    with pytest.raises(PluginRequestError) as forbidden:
        rackflow.run_action(plugin, service, "mark_reviewed", as_="client")
    assert forbidden.value.status_code == 403


def test_an_old_note_is_flagged_and_the_client_is_told_once(rackflow, setup):
    plugin, owner, service = setup
    rackflow.run_action(plugin, service, "save_note", {"note": "Old"}, as_="client")
    rackflow.clock.advance(days=89)
    rackflow.run_schedule(plugin, "review", service)
    assert plugin.state(service)["review_status"] == "current"
    rackflow.clock.advance(days=2)
    assert rackflow.run_schedule(plugin, "review", service)[0].status == "ok"
    assert plugin.state(service)["review_status"] == "stale"
    rackflow.run_schedule(plugin, "review", service)
    assert len(rackflow.client_notices(owner)) == 1

    shown = elements(rackflow, service, "client")
    assert shown[("badge", "Review")]["text"] == "Needs review"
    assert shown[("text", None)]["tone"] == "warning"
    assert ("action", "mark_reviewed") not in shown
    assert ("action", "mark_reviewed") in elements(rackflow, service, "admin")


def test_staff_review_clears_the_flag(rackflow, setup):
    plugin, _owner, service = setup
    rackflow.run_action(plugin, service, "save_note", {"note": "Old"})
    rackflow.clock.advance(days=100)
    rackflow.run_schedule(plugin, "review", service)
    rackflow.run_action(plugin, service, "mark_reviewed")
    assert plugin.state(service)["review_status"] == "current"
    assert ("action", "mark_reviewed") not in elements(rackflow, service, "admin")


def test_the_review_window_and_notice_are_settings(rackflow, setup):
    plugin, owner, service = setup
    plugin.configure(review_after_days=7, notify_owner=False)
    rackflow.run_action(plugin, service, "save_note", {"note": "Soon"})
    rackflow.clock.advance(days=8)
    rackflow.run_schedule(plugin, "review", service)
    assert plugin.state(service)["review_status"] == "stale"
    assert rackflow.client_notices(owner) == []


def test_a_service_without_a_note_is_left_alone(rackflow, setup):
    plugin, _owner, service = setup
    rackflow.clock.advance(days=400)
    assert rackflow.run_schedule(plugin, "review", service)[0].status == "ok"
    assert plugin.state(service) == {}


def test_staff_can_read_the_note_through_mcp(rackflow, setup):
    plugin, _owner, service = setup
    rackflow.run_action(plugin, service, "save_note", {"label": "Shop", "note": "Hello"})
    assert rackflow.run_tool(plugin, "get_note", {"service_id": service.id}) == {
        "label": "Shop", "note": "Hello", "review_status": "current",
    }


def test_another_service_type_is_not_covered(rackflow, setup):
    plugin, owner, _service = setup
    proxy = rackflow.make_service(owner=owner, type="http_proxy")
    assert rackflow.panels(proxy, "client") == []
