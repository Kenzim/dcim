import pytest
from app.plugins.switch_base import SwitchPlugin, SwitchPluginCategory
from app.plugins import switch_registry as mod


class Fake(SwitchPlugin):
    PLUGIN_NAME, PLUGIN_VERSION = "fake", "1"
    SUPPORTED_CATEGORIES = [SwitchPluginCategory.MONITORING]
    CONFIG_TEMPLATE = {"type": "object"}
    async def test_connection(self): return {"success": True}


class Replacement(Fake):
    PLUGIN_VERSION = "2"


def test_register_list_info_and_monitoring():
    registry = mod.SwitchPluginRegistry(); registry.register_plugin(Fake)
    assert registry.list_plugins()[0]["name"] == "fake"
    assert registry.get_plugin_info("fake")["version"] == "1"
    assert registry.get_plugin_info("missing") is None
    assert registry.supports_monitoring("fake") and not registry.supports_monitoring("missing")


def test_register_requires_name():
    class Nameless(Fake): PLUGIN_NAME = ""
    with pytest.raises(ValueError, match="PLUGIN_NAME"):
        mod.SwitchPluginRegistry().register_plugin(Nameless)


def test_get_plugin_is_cached_by_config_identity_and_unknown_fails():
    registry = mod.SwitchPluginRegistry(); registry.register_plugin(Fake)
    config = {"host": "a"}
    assert registry.get_plugin("fake", config) is registry.get_plugin("fake", config)
    assert registry.get_plugin("fake", {"host": "a"}) is not registry.get_plugin("fake", config)
    with pytest.raises(KeyError, match="not found"):
        registry.get_plugin("bad", {})


def test_register_overwrites_metadata():
    registry = mod.SwitchPluginRegistry(); registry.register_plugin(Fake); registry.register_plugin(Replacement)
    assert registry.get_plugin_info("fake")["version"] == "2"


def test_discover_plugin_file(tmp_path):
    plugin = tmp_path / "example.py"
    plugin.write_text(
        "from app.plugins.switch_base import SwitchPlugin, SwitchPluginCategory\n"
        "class DiskPlugin(SwitchPlugin):\n"
        " PLUGIN_NAME='disk'; PLUGIN_VERSION='1'; SUPPORTED_CATEGORIES=[SwitchPluginCategory.MONITORING]; CONFIG_TEMPLATE={}\n"
        " async def test_connection(self): return {'success': True}\n"
    )
    registry = mod.SwitchPluginRegistry(); registry.discover_plugins(tmp_path)
    assert registry.get_plugin_info("disk")["name"] == "disk"


def test_discovery_ignores_broken_files(tmp_path):
    (tmp_path / "broken.py").write_text("raise RuntimeError('broken')")
    registry = mod.SwitchPluginRegistry(); registry.discover_plugins(tmp_path)
    assert registry.list_plugins() == []


def test_global_registry_is_cached(monkeypatch):
    monkeypatch.setattr(mod, "_switch_registry", None)
    registry = mod.get_switch_registry()
    assert registry is mod.get_switch_registry()
