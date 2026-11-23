"""Migration plugin_p2_effective_api: ``plugin_installations.effective_api``."""

from tests.migrations import helpers

REVISION = "plugin_p2_effective_api"


def test_up_down_up_with_data(p2_engine_at):
    engine = p2_engine_at(helpers.before(REVISION))
    helpers.execute(
        engine,
        "INSERT INTO plugin_installations (id, name, version, manifest, directory, code_hash, status, "
        "granted_permissions, addon_refs) VALUES ('vies_validator', 'VIES', '1.0.0', "
        "'{\"rackflow_api\": \">=1.0\"}', 'vies_validator', 'abc', 'enabled', '[]', '[]')",
    )
    assert "effective_api" not in helpers.columns(engine, "plugin_installations")

    helpers.migrate(engine, REVISION)
    assert "effective_api" in helpers.columns(engine, "plugin_installations")
    assert helpers.scalar(engine, "SELECT effective_api FROM plugin_installations") is None

    helpers.migrate(engine, helpers.before(REVISION))
    assert "effective_api" not in helpers.columns(engine, "plugin_installations")
    assert helpers.scalar(engine, "SELECT id FROM plugin_installations") == "vies_validator"

    helpers.migrate(engine, REVISION)
    helpers.execute(engine, "UPDATE plugin_installations SET effective_api = '1.1'")
    assert helpers.scalar(engine, "SELECT effective_api FROM plugin_installations") == "1.1"
