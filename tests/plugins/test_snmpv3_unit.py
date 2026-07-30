import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.plugins.snmpv3 import SNMPv3Plugin
from app.plugins import snmpv3 as mod


def config(**extra):
    value = {"hostname": "switch", "username": "snmpuser", "security_level": "authPriv",
             "auth_password": "password", "priv_password": "password"}
    value.update(extra); return value


@pytest.mark.parametrize("username,message", [("", "username is required"), ("short", "at least 8")])
def test_constructor_validation(username, message):
    with pytest.raises(ValueError, match=message):
        SNMPv3Plugin(config(username=username))


@pytest.mark.parametrize("level", ["noAuthNoPriv", "authNoPriv", "authPriv"])
def test_builds_user_data_for_each_security_level(level):
    plugin = SNMPv3Plugin(config(security_level=level))
    assert plugin.user_data is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["enable_port", "disable_port"])
async def test_write_methods_are_rejected(method):
    with pytest.raises(NotImplementedError, match="read-only"):
        await getattr(SNMPv3Plugin(config()), method)("1")


@pytest.mark.asyncio
async def test_connection_rejects_empty_hostname():
    plugin = SNMPv3Plugin(config(hostname=""))
    result = await plugin.test_connection()
    assert result["details"]["error_type"] == "configuration"


@pytest.mark.asyncio
async def test_connection_success(monkeypatch):
    plugin = SNMPv3Plugin(config())
    monkeypatch.setattr(plugin, "_get_transport", AsyncMock(return_value="transport"))
    with patch.object(mod, "get_cmd", new=AsyncMock(return_value=(None, None, 0, [("oid", "Switch OS")]))):
        result = await plugin.test_connection()
    assert result["success"] and result["details"]["sysDescr"] == "Switch OS"


@pytest.mark.asyncio
@pytest.mark.parametrize("error,fragment", [
    ("authentication failed", "authentication error"), ("timeout waiting", "connection timeout"),
    ("ciphertext invalid", "privacy/encryption error"), ("unknown user", "Device not found"),
])
async def test_connection_error_indications(monkeypatch, error, fragment):
    plugin = SNMPv3Plugin(config()); monkeypatch.setattr(plugin, "_get_transport", AsyncMock(return_value="x"))
    with patch.object(mod, "get_cmd", new=AsyncMock(return_value=(error, None, 0, []))):
        result = await plugin.test_connection()
    assert not result["success"] and fragment.lower() in result["message"].lower()


@pytest.mark.asyncio
async def test_connection_error_status(monkeypatch):
    plugin = SNMPv3Plugin(config()); status = MagicMock(); status.prettyPrint.return_value = "bad"
    monkeypatch.setattr(plugin, "_get_transport", AsyncMock(return_value="x"))
    with patch.object(mod, "get_cmd", new=AsyncMock(return_value=(None, status, 1, [("oid", "x")]))):
        assert "SNMP error: bad" in (await plugin.test_connection())["message"]


@pytest.mark.asyncio
async def test_port_status_and_lookup(monkeypatch):
    plugin = SNMPv3Plugin(config())
    monkeypatch.setattr(plugin, "get_all_port_statistics", AsyncMock(return_value={"eth0": {"ifIndex": 1, "ifOperStatus": 1, "ifAdminStatus": 1, "ifSpeed": 2, "name": "eth0"}}))
    assert (await plugin.get_port_statistics("1"))["name"] == "eth0"
    assert (await plugin.get_port_status("eth0"))["oper_status"] == 1
    with pytest.raises(ValueError): await plugin.get_port_statistics("99")
