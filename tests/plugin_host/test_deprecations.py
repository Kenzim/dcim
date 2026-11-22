"""The deprecation registry and the warnings the kit gives authors (docs/plugins/deprecation-policy.md)."""

import json

from app.plugin_host.catalogue import API_VERSION, EVENTS, PERMISSIONS
from app.plugin_host.testkit import check_package, deprecations, reference
from app.plugin_host.testkit.deprecations import KINDS, Deprecation


def _major(version: str) -> int:
    return int(version.split(".")[0])


def _minor_tuple(version: str) -> tuple[int, int]:
    major, minor = version.split(".")[:2]
    return int(major), int(minor)


def test_every_entry_is_complete_and_names_something_real():
    sdk_names = {e["name"] for e in reference.sdk_surface()}
    manifest_fields = {
        f"{model}.{field}" for model, spec in reference.api_surface()["manifest"].items() for field in spec["fields"]
    }
    for item in deprecations.DEPRECATIONS:
        assert item.kind in KINDS
        assert item.replacement.strip(), f"{item.name} needs advice on what to use instead"
        assert _minor_tuple(item.since) <= _minor_tuple(API_VERSION)
        assert _major(item.removal) > _major(item.since), "an item is removed in a later major version"
        known = {
            "permission": set(PERMISSIONS),
            "event": set(EVENTS),
            "hook": set(EVENTS),
            "manifest_field": manifest_fields,
            "sdk": sdk_names,
        }[item.kind]
        assert item.name in known, f"{item.kind} {item.name} does not exist"


def test_deprecated_items_that_a_package_uses_are_warned_about(tmp_path, monkeypatch):
    folder = tmp_path / "old_style"
    folder.mkdir()
    manifest = {
        "id": "old_style", "name": "Old", "version": "1.0.0", "description": "d", "author": "a", "licence": "MIT",
        "rackflow_api": ">=1.0,<2.0", "entry": "plugin.py", "permissions": ["support.read"], "events": ["ticket.created"],
    }
    (folder / "plugin.json").write_text(json.dumps(manifest))
    (folder / "plugin.py").write_text(
        "def register(plugin):\n    @plugin.on('ticket.created')\n    def h(ctx, e):\n        ctx.log.info('x')\n"
    )
    assert check_package(folder).warnings == []
    monkeypatch.setattr(
        deprecations,
        "DEPRECATIONS",
        (
            Deprecation("event", "ticket.created", "1.0", "2.0", "use ticket.opened"),
            Deprecation("sdk", "ctx.log.info", "1.0", "2.0", "use ctx.log.notice"),
            Deprecation("permission", "billing.read", "1.0", "2.0", "unused here"),
        ),
    )
    warnings = check_package(folder).warnings
    assert len(warnings) == 2
    assert "event ticket.created is deprecated since plugin API 1.0 and will be removed in API 2.0" in warnings[0]
    assert "ctx.log.notice" in warnings[1]
