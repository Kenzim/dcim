"""The names of the plugins Rackflow ships are reserved: a plugin of the plugins folder cannot take one,
and the bundled plugin always wins (docs/plugins/CHANGELOG.md, API 1.1)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.models.commerce_audit import UserAuditEvent
from app.models.plugin_host import PluginInstallation, PluginSetting, PluginState
from app.plugin_host import kinds, lifecycle, loader, runtime
from app.plugin_host.loader import bundled_root, discover, find_package
from app.plugin_host.providers import call_provider
from app.plugin_host.testkit.fixtures import rackflow  # noqa: F401
from plugin_packages.vies_validator.tests.test_vies_plugin import replay_test_service
from tests.plugin_host.helpers import base_manifest, run, write_plugin
from tests.plugin_host.test_api import admin_headers

REPO = Path(__file__).resolve().parents[2]
BUNDLED = REPO / "plugin_packages"
HMRC_FIXTURES = BUNDLED / "hmrc_exchange_rates" / "tests" / "fixtures"
MARKER = "ran.txt"
MESSAGE = "is a reserved name used by a plugin Rackflow ships; rename this plugin."

SHADOW_CODE = f'''from pathlib import Path

Path(__file__).with_name("{MARKER}").write_text("the third-party code was imported")


def register(plugin):
    pass
'''


def vies_answer(rackflow_env):
    replay_test_service(rackflow_env)
    result = call_provider(
        "tax_id_validator", "vies_validator:vies", "check_online",
        kinds.TaxIdCheckRequest("DE", "100", "eu_vat", None, None),
    )
    assert result.status == "valid" and result.registered_name == "John Doe"


def hmrc_answer(rackflow_env):
    rackflow_env.http.respond("monthly_xml_2026-11.xml", 200, (HMRC_FIXTURES / "monthly_xml_2026-11.xml").read_text())
    result = call_provider(
        "currency_rates", "hmrc_exchange_rates:hmrc_monthly", "rates",
        kinds.CurrencyRatesRequest(base="GBP", period="2026-11"),
    )
    assert {r.currency: r.units_per_base for r in result.rates}["USD"] == "1.3396"


CASES = [
    pytest.param("vies_validator", "vies_validator", vies_answer, id="vies-same-directory"),
    pytest.param("vies_validator", "vat_checker_copy", vies_answer, id="vies-same-manifest-id"),
    pytest.param("hmrc_exchange_rates", "hmrc_exchange_rates", hmrc_answer, id="hmrc-same-directory"),
    pytest.param("hmrc_exchange_rates", "rates_copy", hmrc_answer, id="hmrc-same-manifest-id"),
]


def put_third_party(root: Path, directory: str, plugin_id: str) -> Path:
    path = write_plugin(root, base_manifest(id=plugin_id, name="Third party"), SHADOW_CODE)
    return path.rename(root / directory) if path.name != directory else path


def tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def use_shipped_plugins(monkeypatch):
    monkeypatch.setattr(settings, "bundled_plugins_dir", str(BUNDLED))


# ------------------------------------------------------------------ loading


@pytest.mark.parametrize("bundled_id, directory, answers", CASES)
def test_a_third_party_package_with_a_bundled_name_does_not_load_and_the_bundled_one_runs(
    rackflow, monkeypatch, bundled_id, directory, answers
):
    use_shipped_plugins(monkeypatch)
    package = find_package(bundled_id)
    run(lifecycle.install(rackflow.db, bundled_id, accept_permissions=list(package.manifest.permissions), enable=True))
    shadow = put_third_party(rackflow.plugins_dir, directory, bundled_id)
    runtime.reset_all()

    found = {(p.directory, p.bundled): p for p in discover()}
    assert found[(bundled_id, True)].valid and not found[(bundled_id, True)].errors
    assert any(f"`{bundled_id}` {MESSAGE}" in e for e in found[(directory, False)].errors)
    assert not found[(directory, False)].valid

    answers(rackflow)
    assert not (shadow / MARKER).exists(), "the third-party code was never imported"
    rackflow.db.expire_all()
    assert rackflow.db.get(PluginInstallation, bundled_id).status == "enabled"


def test_reserved_names_are_read_from_the_bundled_folder_at_run_time(monkeypatch, tmp_path):
    use_shipped_plugins(monkeypatch)
    assert {"vies_validator", "hmrc_exchange_rates"} <= loader.reserved_names()
    other = tmp_path / "shipped"
    write_plugin(other, base_manifest(id="brand_new_plugin"), "def register(plugin):\n    pass\n")
    monkeypatch.setattr(settings, "bundled_plugins_dir", str(other))
    assert loader.reserved_names() == {"brand_new_plugin"}
    assert bundled_root() == other.resolve()


def test_nothing_is_reserved_when_there_is_no_bundled_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "bundled_plugins_dir", str(tmp_path / "nowhere"))
    assert loader.reserved_names() == set()


def test_two_third_party_packages_with_one_id_behave_as_before(plugin_env):
    write_plugin(plugin_env, base_manifest(id="same_id"), "def register(plugin):\n    pass\n").rename(
        plugin_env / "another_folder"
    )
    write_plugin(plugin_env, base_manifest(id="same_id"), "def register(plugin):\n    pass\n")
    found = {p.directory: p for p in discover()}
    assert found["same_id"].valid and found["same_id"].errors == []
    assert found["another_folder"].errors == ["the directory name must equal the plugin id 'same_id'"]
    assert not any("reserved" in e for p in found.values() for e in p.errors)


def test_a_clash_inside_the_bundled_folder_is_left_alone(plugin_env, monkeypatch, tmp_path):
    shipped = tmp_path / "bundled"
    write_plugin(shipped, base_manifest(id="shipped_one"), "def register(plugin):\n    pass\n")
    (shipped / "shipped_one").rename(shipped / "shipped_two")
    write_plugin(shipped, base_manifest(id="shipped_one"), "def register(plugin):\n    pass\n")
    monkeypatch.setattr(settings, "bundled_plugins_dir", str(shipped))
    found = {p.directory: p for p in discover()}
    assert found["shipped_one"].valid
    assert found["shipped_two"].errors == ["the directory name must equal the plugin id 'shipped_one'"]
    assert not any("reserved" in e for p in found.values() for e in p.errors)


# ------------------------------------------------------------------ installing


@pytest.mark.parametrize("bundled_id, directory", [(c.values[0], c.values[1]) for c in CASES])
def test_the_admin_api_refuses_to_install_a_package_with_a_bundled_name_and_writes_nothing(
    client, db_session, test_admin_user, plugin_env, monkeypatch, bundled_id, directory
):
    use_shipped_plugins(monkeypatch)
    shadow = put_third_party(plugin_env, directory, bundled_id)
    before = tree(plugin_env)
    headers = admin_headers(client)
    plugin_audit = select(func.count()).select_from(UserAuditEvent).where(UserAuditEvent.action.like("plugin.%"))
    audit_before = db_session.execute(plugin_audit).scalar()

    resp = client.post(
        "/api/admin/plugins/install", headers=headers,
        json={"directory": directory, "accept_permissions": [], "enable": True},
    )

    assert resp.status_code == 400, resp.text
    assert f"`{bundled_id}` {MESSAGE}" in resp.json()["detail"]
    db_session.expire_all()
    assert db_session.execute(select(func.count()).select_from(PluginInstallation)).scalar() == 0
    assert db_session.execute(select(func.count()).select_from(PluginSetting)).scalar() == 0
    assert db_session.execute(select(func.count()).select_from(PluginState)).scalar() == 0
    assert db_session.execute(plugin_audit).scalar() == audit_before
    assert tree(plugin_env) == before, "the plugins folder is untouched"
    assert not (shadow / MARKER).exists(), "the third-party code was never imported"
    assert not any(name.startswith("rackflow_plugins.") for name in sys.modules if bundled_id in name)


def test_the_admin_list_shows_the_refused_package_and_still_offers_the_bundled_one(
    client, db_session, test_admin_user, plugin_env, monkeypatch
):
    use_shipped_plugins(monkeypatch)
    put_third_party(plugin_env, "vat_checker_copy", "vies_validator")
    headers = admin_headers(client)
    installed = client.post(
        "/api/admin/plugins/install", headers=headers,
        json={"directory": "vies_validator", "accept_permissions": ["tax.validate_id", "http.outbound"]},
    )
    assert installed.status_code == 200, installed.text

    listing = client.get("/api/admin/plugins", headers=headers).json()
    by_directory = {p["directory"]: p for p in listing["available"]}
    assert by_directory["vies_validator"]["valid"] and by_directory["vies_validator"]["installed"]
    refused = by_directory["vat_checker_copy"]
    assert not refused["valid"] and not refused["installed"] and refused["bundled"] is False
    assert any(MESSAGE in e for e in refused["errors"])
    assert [p["id"] for p in listing["installed"]] == ["vies_validator"]
    assert listing["installed"][0]["bundled"] is True


def test_installing_a_bundled_plugin_still_works_when_no_package_clashes(
    client, db_session, test_admin_user, plugin_env, monkeypatch
):
    use_shipped_plugins(monkeypatch)
    resp = client.post(
        "/api/admin/plugins/install", headers=admin_headers(client),
        json={"directory": "hmrc_exchange_rates", "accept_permissions": ["currency.rates", "http.outbound"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["bundled"] is True


def test_the_lifecycle_refuses_to_install_or_upgrade_a_package_with_a_bundled_name(
    plugin_env, db_session, monkeypatch
):
    use_shipped_plugins(monkeypatch)
    put_third_party(plugin_env, "rates_copy", "hmrc_exchange_rates")
    with pytest.raises(lifecycle.LifecycleError, match="reserved name"):
        run(lifecycle.install(db_session, "rates_copy", accept_permissions=[]))
    put_third_party(plugin_env, "hmrc_exchange_rates", "hmrc_exchange_rates")
    with pytest.raises(lifecycle.LifecycleError, match="reserved name"):
        run(lifecycle.install(db_session, "hmrc_exchange_rates", accept_permissions=[]))
    assert db_session.execute(select(func.count()).select_from(PluginInstallation)).scalar() == 0
    shadow = json.loads((plugin_env / "hmrc_exchange_rates" / "plugin.json").read_text())
    assert shadow["name"] == "Third party"
