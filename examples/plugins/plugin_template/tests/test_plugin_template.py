from pathlib import Path

from app.plugin_host.testkit import assert_valid_package

PLUGIN = Path(__file__).resolve().parents[1]


def test_package_is_valid():
    assert_valid_package(PLUGIN)


def test_created_service_gets_the_greeting(rackflow):
    plugin = rackflow.install(PLUGIN, settings={"greeting": "Hi"})
    service = rackflow.make_service()
    plugin.activate(service)
    rackflow.fire("service.created", service)
    assert plugin.state(service)["message"] == "Hi! Your service is ready."


def test_client_can_say_hello_and_the_panel_updates(rackflow):
    plugin = rackflow.install(PLUGIN)
    service = rackflow.make_service()
    plugin.activate(service)
    result = rackflow.run_action(plugin, service, "say_hello", {"name": "Ada"}, as_="client")
    assert result["message"] == "Hello said."
    panel = rackflow.panel(service, "hello", "client")
    assert panel["elements"][0]["value"] == "Welcome, Ada!"
    assert panel["elements"][1]["value"] == "1"
