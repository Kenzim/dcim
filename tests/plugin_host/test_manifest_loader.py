"""Manifest validation and package loading."""

import pytest

from app.plugin_host.loader import PluginLoadError, discover, import_package, package_hash, read_package
from app.plugin_host.manifest import ManifestError, range_allows, validate_manifest
from tests.plugin_host.helpers import EXAMPLES, base_manifest, write_plugin


def test_sample_manifest_is_valid():
    package = read_package(EXAMPLES / "uptime_reminder")
    assert package.valid, package.errors
    loaded = import_package(package)
    assert set(loaded.handlers.on) == {"service.created", "service.suspended", "service.unsuspended"}
    assert set(loaded.handlers.actions) == {"confirm", "remind_now"}


@pytest.mark.parametrize(
    "spec,version,ok",
    [(">=1.0,<2.0", "1.0", True), (">=1.1", "1.0", False), ("<2", "2.0", False), ("==1.0", "1.0", True)],
)
def test_range(spec, version, ok):
    assert range_allows(spec, version) is ok


@pytest.mark.parametrize(
    "overrides,needle",
    [
        ({"id": "Bad-Id"}, "id must match"),
        ({"version": "1.0"}, "semantic version"),
        ({"permissions": ["root"]}, "unknown permission"),
        ({"events": ["service.created"]}, "needs the service.read permission"),
        ({"events": ["nope"], "permissions": ["service.read"]}, "unknown event"),
        ({"hooks": ["service.power_requested"], "permissions": ["service.read"]}, "events.veto"),
        ({"hooks": ["service.created"], "permissions": ["service.read", "events.veto"]}, "no before hook"),
        ({"entry": "../x.py"}, "relative .py path"),
        ({"surprise": 1}, "Extra inputs"),
        ({"tables": []}, "tables are not supported"),
        (
            {"permissions": ["service.read"], "services": {"types": ["vm"]},
             "actions": [{"id": "go", "label": "Go", "audience": ["client"]}]},
            "ui.client",
        ),
        (
            {"permissions": ["service.read"], "services": {"types": ["vm"]},
             "ui": {"panels": [{"id": "p", "title": "P", "elements": [{"type": "action", "action": "missing"}]}]}},
            "undeclared action",
        ),
        ({"schedules": [{"id": "s", "every": "30s"}]}, "60s"),
        ({"settings": [{"key": "token", "label": "T", "type": "secret", "default": "x"}]}, "cannot have a default"),
    ],
)
def test_bad_manifests(overrides, needle):
    with pytest.raises(ManifestError) as info:
        validate_manifest(base_manifest(**overrides))
    assert needle in str(info.value)


def test_handlers_must_match_manifest(plugin_env):
    write_plugin(
        plugin_env,
        base_manifest(permissions=["service.read"], events=["service.created"]),
        "def register(plugin):\n    @plugin.action('extra')\n    def a(ctx, req):\n        return {}\n",
    )
    package = read_package(plugin_env / "demo_plugin")
    with pytest.raises(PluginLoadError) as info:
        import_package(package)
    message = str(info.value)
    assert "'service.created' is declared but has no handler" in message
    assert "'extra' has a handler but is not declared" in message


def test_import_error_is_reported(plugin_env):
    write_plugin(plugin_env, base_manifest(), "raise RuntimeError('boom')\n")
    with pytest.raises(PluginLoadError, match="boom"):
        import_package(read_package(plugin_env / "demo_plugin"))


def test_directory_must_match_id_and_api(plugin_env):
    path = write_plugin(plugin_env, base_manifest(rackflow_api=">=9.0"), "def register(plugin):\n    pass\n")
    path.rename(plugin_env / "other_name")
    package = discover(plugin_env)[0]
    assert not package.valid
    assert any("directory name" in e for e in package.errors)
    assert any("needs Rackflow plugin API" in e for e in package.errors)


def test_hash_changes_with_files(plugin_env):
    path = write_plugin(plugin_env, base_manifest(), "def register(plugin):\n    pass\n")
    first = package_hash(path)
    (path / "__pycache__").mkdir()
    (path / "__pycache__" / "x.pyc").write_bytes(b"ignored")
    assert package_hash(path) == first
    (path / "helper.py").write_text("X = 1\n")
    assert package_hash(path) != first


def test_relative_imports_inside_package(plugin_env):
    path = write_plugin(
        plugin_env, base_manifest(), "from . import helper\n\ndef register(plugin):\n    assert helper.X == 2\n"
    )
    (path / "helper.py").write_text("X = 2\n")
    import_package(read_package(path))
