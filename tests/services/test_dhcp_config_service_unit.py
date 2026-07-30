import pytest
from unittest.mock import MagicMock
from app.services import dhcp_config_service as mod


def row(**overrides):
    values = dict(enabled=True, interfaces=[], dns_servers=None, hand_out_leases=True,
                  default_lease_time=3600, max_lease_time=7200, config_file_path="/c", lease_file_path="/l")
    values.update(overrides); return MagicMock(**values)


@pytest.mark.parametrize("interfaces,dns,expected", [
    ([], None, ("eth1", ["1.1.1.1", "1.0.0.1"])),
    ([{"interface": "ens1", "ip": "10.0.0.2"}], ["8.8.8.8"], ("ens1", ["8.8.8.8"])),
])
def test_row_to_config_defaults_and_dict_interfaces(interfaces, dns, expected):
    config = mod._row_to_config(row(interfaces=interfaces, dns_servers=dns))
    assert (config.interfaces[0].interface, config.dns_servers) == expected


@pytest.mark.parametrize("name,default,envvalue", [
    ("_default_config_path", "/shared/dhcp/dhcpd.conf", "/env.conf"),
    ("_default_lease_path", "/shared/dhcp/dhcpd.leases", "/env.leases"),
])
def test_default_paths(monkeypatch, name, default, envvalue):
    env = "DHCP_CONFIG_FILE_PATH" if "config" in name else "DHCP_LEASE_FILE_PATH"
    monkeypatch.delenv(env, raising=False); assert getattr(mod, name)() == default
    monkeypatch.setenv(env, envvalue); assert getattr(mod, name)() == envvalue


@pytest.mark.parametrize("existing", [True, False])
def test_get_config(monkeypatch, existing):
    stored, created = row(), row(config_file_path="/made")
    monkeypatch.setattr(mod.DHCPConfigDAO, "get_config", lambda _: stored if existing else None)
    monkeypatch.setattr(mod.DHCPConfigDAO, "get_or_create", lambda *a: created)
    assert mod.DHCPConfigService().get_config(MagicMock()).config_file_path == ("/c" if existing else "/made")


def test_get_by_service_instance_missing_and_present(monkeypatch):
    service = mod.DHCPConfigService()
    monkeypatch.setattr(mod.DHCPConfigDAO, "get_by_service_instance_id", lambda *a: None)
    assert service.get_config_by_service_instance(MagicMock(), 4) is None
    monkeypatch.setattr(mod.DHCPConfigDAO, "get_by_service_instance_id", lambda *a: row())
    assert service.get_config_by_service_instance(MagicMock(), 4).config_file_path == "/c"


@pytest.mark.parametrize("interfaces", [
    [{"interface": "ens2", "ip": "10.0.0.2"}],
    [mod.DHCPInterfaceConfig(interface="ens3", ip="10.0.0.3")],
])
def test_update_row_serializes_interfaces(monkeypatch, interfaces):
    db, stored = MagicMock(), row()
    update = MagicMock(); monkeypatch.setattr(mod.DHCPConfigDAO, "update", update)
    config = mod.DHCPConfigService()._update_row(db, stored, interfaces=interfaces, enabled=False)
    assert stored.enabled is False and stored.interfaces[0]["interface"] in {"ens2", "ens3"}
    update.assert_called_once_with(db, stored); assert config.enabled is False


@pytest.mark.parametrize("method", ["update_config", "update_config_for_service_instance"])
def test_update_paths_create_rows(monkeypatch, method):
    db, stored = MagicMock(), row()
    monkeypatch.setattr(mod.DHCPConfigDAO, "get_config", lambda _: None)
    monkeypatch.setattr(mod.DHCPConfigDAO, "get_or_create", lambda *a: stored)
    monkeypatch.setattr(mod.DHCPConfigDAO, "get_or_create_for_service_instance", lambda *a: stored)
    monkeypatch.setattr(mod.DHCPConfigDAO, "update", MagicMock())
    service = mod.DHCPConfigService()
    args = (db,) if method == "update_config" else (db, 7)
    assert getattr(service, method)(*args, max_lease_time=900).max_lease_time == 900


def test_create_for_instance_reload_and_singleton(monkeypatch):
    stored = row(); monkeypatch.setattr(mod.DHCPConfigDAO, "get_or_create_for_service_instance", lambda *a: stored)
    service = mod.DHCPConfigService()
    assert service.get_or_create_config_for_service_instance(MagicMock(), 1, "/a", "/b").config_file_path == "/c"
    service.get_config = MagicMock(return_value="fresh"); assert service.reload(MagicMock()) == "fresh"
    monkeypatch.setattr(mod, "_dhcp_config_service", None)
    assert mod.get_dhcp_config_service() is mod.get_dhcp_config_service()
