"""An installation records which root it came from, and a package of the other root is never used for it.

A third-party plugin installed under a name that a later Rackflow release ships must not be upgraded into
Rackflow's plugin, and Rackflow's plugin must start with none of the third-party plugin's permissions,
settings, secrets or data (docs/plugins/CHANGELOG.md, API 1.1).
"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.models.commerce_audit import UserAuditEvent
from app.models.plugin_host import PluginInstallation, PluginSetting, PluginState, PluginStatus
from app.plugin_host import lifecycle, runtime, state
from app.plugin_host.loader import find_package, package_hash
from tests.plugin_host.helpers import base_manifest, install, run, write_plugin
from tests.plugin_host.test_api import admin_headers

REPO = Path(__file__).resolve().parents[2]
BUNDLED_PACKAGES = REPO / "plugin_packages"
PLUGIN_ID = "demo_plugin"
CLASH = "third-party plugin of that name"
LEFTOVER = "was kept when it was uninstalled"

THIRD_PARTY_CODE = """from pathlib import Path

Path(__file__).parent.with_name("third_party_ran.txt").write_text("imported")


def register(plugin):
    pass
"""

SHIPPED_CODE = """from pathlib import Path

Path(__file__).parent.with_name("shipped_ran.txt").write_text("imported")


def register(plugin):
    pass
"""


@pytest.fixture
def shipped(plugin_env, monkeypatch, tmp_path):
    root = tmp_path / "bundled"
    root.mkdir(exist_ok=True)
    monkeypatch.setattr(settings, "bundled_plugins_dir", str(root))
    return root


def put_shipped(shipped: Path, version: str = "1.1.0", code: str = SHIPPED_CODE) -> Path:
    return write_plugin(shipped, base_manifest(id=PLUGIN_ID, name="Rackflow demo", version=version), code)


def install_third_party(plugin_env: Path, db) -> PluginInstallation:
    manifest = base_manifest(
        version="1.0.0",
        permissions=["service.read"],
        settings=[{"key": "region", "label": "Region", "type": "string", "default": "eu"}],
    )
    write_plugin(plugin_env, manifest, THIRD_PARTY_CODE)
    inst = install(db, PLUGIN_ID)
    db.add(PluginSetting(plugin_id=PLUGIN_ID, key="api_token", sealed_value="sealed-third-party-secret"))
    state.StateView(db, PLUGIN_ID).set("cursor", "third-party-cursor")
    db.commit()
    return inst


def row_counts(db) -> dict[str, int]:
    return {
        "installations": db.execute(select(func.count()).select_from(PluginInstallation)).scalar(),
        "settings": db.execute(select(func.count()).select_from(PluginSetting)).scalar(),
        "state": db.execute(select(func.count()).select_from(PluginState)).scalar(),
    }


def plugin_audit_count(db) -> int:
    return db.execute(
        select(func.count()).select_from(UserAuditEvent).where(UserAuditEvent.action.like("plugin.%"))
    ).scalar()


# ------------------------------------------------------------------ a third-party install, then a bundled plugin of that name


def test_a_third_party_install_is_not_loaded_upgraded_or_offered_the_bundled_plugin_of_its_name(
    plugin_env, shipped, db_session
):
    inst = install_third_party(plugin_env, db_session)
    assert inst.status == PluginStatus.ENABLED
    put_shipped(shipped)
    (plugin_env / "third_party_ran.txt").unlink()
    runtime.reset_all()

    assert runtime.load_installed(inst) is None
    db_session.refresh(inst)
    assert inst.status == PluginStatus.FILES_CHANGED and CLASH in inst.disabled_reason
    assert not (shipped / "shipped_ran.txt").exists(), "Rackflow's code was never imported"
    assert not (plugin_env / "third_party_ran.txt").exists()

    before = (row_counts(db_session), plugin_audit_count(db_session))
    with pytest.raises(lifecycle.LifecycleError, match=CLASH):
        run(lifecycle.upgrade(db_session, inst, accept_permissions=[]))
    db_session.refresh(inst)
    assert (row_counts(db_session), plugin_audit_count(db_session)) == before
    assert inst.version == "1.0.0" and inst.granted_permissions == ["service.read"]
    assert not (shipped / "shipped_ran.txt").exists()

    view = lifecycle.overview(db_session)["installed"][0]
    assert view["upgrade"] is None and CLASH in view["upgrade_blocked"] and view["bundled"] is False
    with pytest.raises(lifecycle.LifecycleError, match=CLASH):
        run(lifecycle.set_enabled(db_session, inst, True))


def test_after_uninstalling_the_third_party_plugin_the_bundled_one_starts_clean(plugin_env, shipped, db_session):
    inst = install_third_party(plugin_env, db_session)
    put_shipped(shipped)
    runtime.reset_all()

    run(lifecycle.uninstall(db_session, inst))
    shutil.rmtree(plugin_env / PLUGIN_ID)
    assert row_counts(db_session) == {"installations": 0, "settings": 0, "state": 0}

    bundled = run(lifecycle.install(db_session, PLUGIN_ID, accept_permissions=[], enable=True))
    assert bundled.version == "1.1.0" and bundled.status == PluginStatus.ENABLED
    assert bundled.granted_permissions == []
    assert row_counts(db_session) == {"installations": 1, "settings": 0, "state": 0}
    assert state.StateView(db_session, PLUGIN_ID).get("cursor") is None


def test_the_admin_api_refuses_the_upgrade_with_a_400_and_writes_nothing(
    client, db_session, test_admin_user, plugin_env, shipped
):
    install_third_party(plugin_env, db_session)
    put_shipped(shipped)
    headers = admin_headers(client)
    before = (row_counts(db_session), plugin_audit_count(db_session))

    resp = client.post(f"/api/admin/plugins/{PLUGIN_ID}/upgrade", headers=headers, json={"accept_permissions": []})

    assert resp.status_code == 400, resp.text
    assert CLASH in resp.json()["detail"]
    db_session.expire_all()
    assert (row_counts(db_session), plugin_audit_count(db_session)) == before
    listing = client.get("/api/admin/plugins", headers=headers).json()["installed"][0]
    assert listing["upgrade"] is None and CLASH in listing["upgrade_blocked"]
    assert not (shipped / "shipped_ran.txt").exists()


# ------------------------------------------------------------------ data an uninstall keeps


def test_kept_data_blocks_the_bundled_install_until_it_is_purged_explicitly(
    client, db_session, test_admin_user, plugin_env, shipped
):
    inst = install_third_party(plugin_env, db_session)
    put_shipped(shipped)
    run(lifecycle.uninstall(db_session, inst, keep_data=True))
    shutil.rmtree(plugin_env / PLUGIN_ID)
    assert row_counts(db_session) == {"installations": 0, "settings": 2, "state": 1}
    headers = admin_headers(client)

    refused = client.post(
        "/api/admin/plugins/install", headers=headers, json={"directory": PLUGIN_ID, "accept_permissions": []}
    )
    assert refused.status_code == 400, refused.text
    assert LEFTOVER in refused.json()["detail"] and "2 settings" in refused.json()["detail"]
    assert "1 state" in refused.json()["detail"] and "purge" in refused.json()["detail"]
    db_session.expire_all()
    assert row_counts(db_session) == {"installations": 0, "settings": 2, "state": 1}, "nothing was deleted by the refusal"
    assert not (shipped / "shipped_ran.txt").exists()

    audit_before = plugin_audit_count(db_session)
    purged = client.post(f"/api/admin/plugins/{PLUGIN_ID}/purge-data", headers=headers)
    assert purged.status_code == 200, purged.text
    assert purged.json()["deleted"] == {"settings": 2, "state": 1}
    db_session.expire_all()
    assert row_counts(db_session) == {"installations": 0, "settings": 0, "state": 0}
    entry = db_session.execute(
        select(UserAuditEvent).where(UserAuditEvent.action == "plugin.purge_data")
    ).scalar_one()
    assert entry.resource_id == PLUGIN_ID and entry.event_metadata["deleted"] == {"settings": 2, "state": 1}
    assert plugin_audit_count(db_session) == audit_before + 1

    installed = client.post(
        "/api/admin/plugins/install", headers=headers, json={"directory": PLUGIN_ID, "accept_permissions": []}
    )
    assert installed.status_code == 200, installed.text
    assert installed.json()["bundled"] is True and installed.json()["permissions"] == []
    assert row_counts(db_session) == {"installations": 1, "settings": 0, "state": 0}


def test_purging_is_refused_while_the_plugin_is_installed(client, db_session, test_admin_user, plugin_env):
    install_third_party(plugin_env, db_session)
    resp = client.post(f"/api/admin/plugins/{PLUGIN_ID}/purge-data", headers=admin_headers(client))
    assert resp.status_code == 400
    assert row_counts(db_session)["settings"] == 1 + 1


def test_a_bundled_plugin_keeps_its_own_data_across_an_uninstall_that_keeps_data(plugin_env, shipped, db_session):
    put_shipped(shipped)
    inst = run(lifecycle.install(db_session, PLUGIN_ID, accept_permissions=[]))
    state.StateView(db_session, PLUGIN_ID).set("cursor", "ours")
    db_session.commit()
    run(lifecycle.uninstall(db_session, inst, keep_data=True))
    again = run(lifecycle.install(db_session, PLUGIN_ID, accept_permissions=[]))
    assert state.StateView(db_session, PLUGIN_ID).get("cursor") == "ours"


def test_a_third_party_install_keeps_its_data_across_an_uninstall_that_keeps_data(plugin_env, db_session):
    inst = install_third_party(plugin_env, db_session)
    run(lifecycle.uninstall(db_session, inst, keep_data=True))
    again = run(lifecycle.install(db_session, PLUGIN_ID, accept_permissions=["service.read"]))
    assert again.status == PluginStatus.DISABLED
    assert state.StateView(db_session, PLUGIN_ID).get("cursor") == "third-party-cursor"


# ------------------------------------------------------------------ the other direction


def test_a_bundled_install_refuses_a_later_plugins_folder_copy_of_its_name(plugin_env, shipped, db_session):
    put_shipped(shipped, version="1.0.0")
    inst = install(db_session, PLUGIN_ID)
    write_plugin(plugin_env, base_manifest(version="2.0.0"), THIRD_PARTY_CODE)
    runtime.reset_all()

    with pytest.raises(lifecycle.LifecycleError, match="reserved name"):
        run(lifecycle.install(db_session, PLUGIN_ID, accept_permissions=[]))
    assert runtime.load_installed(inst) is not None
    assert not (plugin_env / "third_party_ran.txt").exists()
    with pytest.raises(lifecycle.LifecycleError, match="already installed"):
        run(lifecycle.upgrade(db_session, inst, accept_permissions=[]))
    db_session.refresh(inst)
    assert inst.status == PluginStatus.ENABLED and inst.version == "1.0.0"


def test_a_bundled_install_whose_shipped_folder_is_gone_does_not_run_a_plugins_folder_copy(
    plugin_env, shipped, db_session
):
    put_shipped(shipped, version="1.0.0")
    inst = install(db_session, PLUGIN_ID)
    shutil.rmtree(shipped / PLUGIN_ID)
    write_plugin(plugin_env, base_manifest(version="2.0.0"), THIRD_PARTY_CODE)
    runtime.reset_all()

    assert runtime.load_installed(inst) is None
    db_session.refresh(inst)
    assert inst.status == PluginStatus.FILES_CHANGED and "third-party copy" in inst.disabled_reason
    assert not (plugin_env / "third_party_ran.txt").exists()
    with pytest.raises(lifecycle.LifecycleError, match="third-party copy"):
        run(lifecycle.upgrade(db_session, inst, accept_permissions=[]))
    assert lifecycle.overview(db_session)["installed"][0]["upgrade"] is None


# ------------------------------------------------------------------ normal upgrades and the backfill


def test_a_bundled_install_with_an_older_hash_shows_files_changed_and_upgrades_normally(
    plugin_env, shipped, db_session
):
    put_shipped(shipped, version="1.0.0")
    inst = install(db_session, PLUGIN_ID)
    put_shipped(shipped, version="1.1.0", code=SHIPPED_CODE + "\n# a later release\n")
    runtime.reset_all()

    assert runtime.load_installed(inst) is None
    db_session.refresh(inst)
    assert inst.status == PluginStatus.FILES_CHANGED and CLASH not in (inst.disabled_reason or "")
    assert lifecycle.overview(db_session)["installed"][0]["upgrade"]["version"] == "1.1.0"

    run(lifecycle.upgrade(db_session, inst, accept_permissions=[]))
    db_session.refresh(inst)
    assert inst.version == "1.1.0" and inst.status == PluginStatus.ENABLED


def test_a_third_party_install_still_upgrades_to_a_newer_third_party_version(plugin_env, db_session):
    inst = install_third_party(plugin_env, db_session)
    manifest = base_manifest(version="1.1.0", permissions=["service.read"], settings=[])
    write_plugin(plugin_env, manifest, THIRD_PARTY_CODE)
    run(lifecycle.upgrade(db_session, inst, accept_permissions=[]))
    db_session.refresh(inst)
    assert inst.version == "1.1.0"


def load_backfill():
    path = REPO / "alembic" / "versions" / "plugin_bundled_identity.py"
    spec = importlib.util.spec_from_file_location("plugin_bundled_identity_revision", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.backfill


def add_row(db, plugin_id: str, directory: str, code_hash: str) -> None:
    db.add(PluginInstallation(
        id=plugin_id, name=plugin_id, version="1.0.0", manifest={}, directory=directory, code_hash=code_hash,
        bundled=False, status=PluginStatus.DISABLED, granted_permissions=[], addon_refs=[],
    ))


def test_the_backfill_decides_by_location_and_uses_the_hash_only_to_break_a_tie(plugin_env, db_session, monkeypatch):
    monkeypatch.setattr(settings, "bundled_plugins_dir", str(BUNDLED_PACKAGES))
    third_party = write_plugin(plugin_env, base_manifest(id="only_third_party"), THIRD_PARTY_CODE)
    shutil.copytree(BUNDLED_PACKAGES / "vies_validator", plugin_env / "vies_validator", ignore=shutil.ignore_patterns("__pycache__"))
    (plugin_env / "vies_validator" / "notes.txt").write_text("a different copy")
    shipped_hash = package_hash(BUNDLED_PACKAGES / "vies_validator")
    copy_hash = package_hash(plugin_env / "vies_validator")
    assert shipped_hash != copy_hash

    add_row(db_session, "hmrc_exchange_rates", "hmrc_exchange_rates", "0" * 64)
    add_row(db_session, "only_third_party", "only_third_party", package_hash(third_party))
    add_row(db_session, "gone", "gone", "1" * 64)
    add_row(db_session, "tie_shipped", "vies_validator", shipped_hash)
    add_row(db_session, "tie_copy", "vies_validator", copy_hash)
    add_row(db_session, "tie_neither", "vies_validator", "2" * 64)
    db_session.commit()

    load_backfill()(db_session.connection())
    db_session.commit()
    db_session.expire_all()

    flags = {row.id: row.bundled for row in db_session.execute(select(PluginInstallation)).scalars()}
    assert flags == {
        "hmrc_exchange_rates": True,
        "only_third_party": False,
        "gone": False,
        "tie_shipped": True,
        "tie_copy": False,
        "tie_neither": False,
    }


@pytest.mark.parametrize("plugin_id", ["vies_validator", "hmrc_exchange_rates"])
def test_a_backfilled_shipped_plugin_loads_and_upgrades_after_a_release_changed_its_code(
    plugin_env, db_session, monkeypatch, plugin_id
):
    monkeypatch.setattr(settings, "bundled_plugins_dir", str(BUNDLED_PACKAGES))
    inst = install(db_session, plugin_id)
    assert inst.bundled is True
    inst.bundled = False  # the state of a row from before the column existed
    db_session.commit()

    load_backfill()(db_session.connection())
    db_session.commit()
    db_session.refresh(inst)
    assert inst.bundled is True
    runtime.reset_all()
    assert runtime.load_installed(inst) is not None

    inst.code_hash = "0" * 64  # stored by a release whose shipped code has since changed
    db_session.commit()
    runtime.reset_all()
    assert runtime.load_installed(inst) is None
    db_session.refresh(inst)
    assert inst.status == PluginStatus.FILES_CHANGED and CLASH not in (inst.disabled_reason or "")

    run(lifecycle.upgrade(db_session, inst, accept_permissions=[]))
    db_session.refresh(inst)
    assert inst.code_hash == find_package(plugin_id).code_hash and inst.bundled is True
