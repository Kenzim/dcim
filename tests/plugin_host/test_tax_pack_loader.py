"""A tax_rule_pack plugin's pack file must pass the tax pack format at load (design 7.2, 8.5)."""

from __future__ import annotations

import json

import pytest

from app.plugin_host.loader import find_package
from tests.plugin_host.test_provider_kinds import PACK, put_fixture


def _demo(path):
    return path / "packs" / "demo.json"


def test_the_demo_pack_passes_the_schema(plugin_env):
    put_fixture(plugin_env, PACK)
    package = find_package(PACK)
    assert package.valid, package.errors


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["rules"][0].update(rate_percent="abc"),
        lambda d: d["rules"][0].update(rounding_rule="nearest"),
        lambda d: d["rules"][0].update(jurisdiction="XI"),
        lambda d: d.update(classes=[]),
        lambda d: d.pop("rules"),
        lambda d: d.update(unexpected=True),
    ],
)
def test_a_pack_failing_the_schema_is_refused_at_load(plugin_env, mutate):
    path = put_fixture(plugin_env, PACK)
    data = json.loads(_demo(path).read_text())
    mutate(data)
    _demo(path).write_text(json.dumps(data))
    package = find_package(PACK)
    assert not package.valid
    assert any("does not pass the pack schema" in error for error in package.errors), package.errors
