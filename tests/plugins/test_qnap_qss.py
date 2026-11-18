import base64
import json

import httpx
import pytest

from app.plugins.qnap_qss import QNAPQSSPlugin, QSSError, normalise_port, parse_vlans
from app.plugins.switch_registry import SwitchPluginRegistry

TOKEN = "tok-123"
PASSWORD = "s3cret"


class FakeQSS:
    """In-memory QSS /api/v3 with the switch's own rules."""

    def __init__(self, vlans=None, management_vlan="1", lag_start=28, lag_groups=None, busy=False):
        self.vlans = vlans if vlans is not None else {"1": [{"Port": str(p), "Tagged": False} for p in range(1, 29)]}
        self.management_vlan = management_vlan
        self.lag_start = lag_start
        self.lag_groups = lag_groups or []
        self.busy = busy
        self.calls = []
        self.logins = 0
        self.logouts = 0

    @staticmethod
    def ok(result=None):
        return httpx.Response(200, json={"error_code": 200, "result": result})

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v3")
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        if path == "/users/login":
            if self.busy:
                return httpx.Response(401, json={"error_code": 401, "error_message": "system busy"})
            if body["password"] != base64.b64encode(PASSWORD.encode()).decode():
                return httpx.Response(401, json={"error_code": 401, "error_message": "unauthorized"})
            self.logins += 1
            return self.ok({"AccessToken": TOKEN, "RefreshToken": "r"})
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"error_code": 401})
        if path == "/users/logout":
            self.logouts += 1
            return self.ok()
        if path == "/vlan" and request.method == "GET":
            return self.ok([{"key": k, "val": v} for k, v in sorted(self.vlans.items(), key=lambda kv: int(kv[0]))])
        if path == "/vlan" and request.method in ("POST", "PUT"):
            for entry in body["data"]:
                if not 2 <= int(entry["key"]) <= 4000 or not entry["val"]:
                    return httpx.Response(400, json={"error_code": 400, "error_message": "invalid vlan"})
                exists = entry["key"] in self.vlans
                if (request.method == "POST") == exists:
                    return httpx.Response(400, json={"error_code": 400, "error_message": "bad method"})
                self.vlans[entry["key"]] = entry["val"]
            return self.ok()
        if path == "/vlan" and request.method == "DELETE":
            for key in body["idx"]:
                self.vlans.pop(key, None)
            return self.ok()
        if path == "/ip/config/enabled":
            return self.ok(self.management_vlan)
        if path == "/lacp/info":
            return self.ok({"StartIndex": self.lag_start, "MaxPortChannels": 8, "MaxPortsPerPortChannel": 8})
        if path == "/lacp/group":
            return self.ok(self.lag_groups)
        if path == "/system/board":
            return self.ok({"Product": "QSW", "Model": "QSW-M2116P-2T2S", "MacAddr": "24:5e:be:98:07:43", "PortNum": 20, "SerialNumber": "Q123"})
        if path == "/firmware/info":
            return self.ok({"version": "2.0.1", "number": "40148", "date": "20240101"})
        if path == "/ports/status":
            return self.ok([
                {"key": "1", "val": {"Link": True, "Speed": "10000", "Shutdown": False}},
                {"key": "2", "val": {"Link": False, "Speed": 0, "Shutdown": True}},
                {"key": "29", "val": {"Link": True, "Speed": 20000}},
            ])
        if path == "/ports/statistics":
            return self.ok([{"key": "1", "val": {"RxOctets": 100, "TxOctets": 200}}])
        return httpx.Response(404, json={"error_code": 404})


@pytest.fixture
def fake():
    return FakeQSS()


@pytest.fixture
def plugin(fake, monkeypatch):
    monkeypatch.setattr(QNAPQSSPlugin, "_transport", httpx.MockTransport(fake.handler))
    return QNAPQSSPlugin({"hostname": "192.0.2.10", "password": PASSWORD})


def writes(fake):
    return [(m, p, b) for m, p, b in fake.calls if m != "GET" and not p.startswith("/users/")]


@pytest.mark.parametrize("raw,expected", [("7", "7"), ("Port 7", "7"), ("port7", "7"), ("eth12", "12"), (" 03 ", "3")])
def test_normalise_port(raw, expected):
    assert normalise_port(raw) == expected


@pytest.mark.parametrize("raw", ["", "0", "Ethernet1/5", "7; reboot", "Port 1\nPort 2", "vlan10"])
def test_normalise_port_rejects_anything_else(raw):
    with pytest.raises(ValueError):
        normalise_port(raw)


def test_parse_vlans_skips_junk():
    assert parse_vlans([{"key": "5", "val": [{"Port": "3", "Tagged": True}, {"Port": "x"}]}, {"key": "bad"}]) == {
        5: {"3": True}
    }


def test_registry_discovers_qnap_with_vlan_management():
    registry = SwitchPluginRegistry()
    registry.discover_plugins()
    assert registry.supports_vlan_management("qnap_qss")
    assert registry.supports_monitoring("qnap_qss")


def test_config_requires_password():
    with pytest.raises(ValueError):
        QNAPQSSPlugin({"hostname": "192.0.2.10"})


@pytest.mark.asyncio
async def test_login_uses_base64_password_and_always_logs_out(plugin, fake):
    info = await plugin.get_switch_info()
    assert info["model"] == "QSW-M2116P-2T2S"
    assert info["serial_number"] == "Q123"
    assert info["firmware_version"] == "2.0.1.40148"
    assert fake.calls[0][2]["password"] == base64.b64encode(PASSWORD.encode()).decode()
    assert fake.logins == fake.logouts == 1


@pytest.mark.asyncio
async def test_logout_happens_even_when_an_operation_fails(plugin, fake):
    fake.management_vlan = "10"
    with pytest.raises(QSSError, match="management VLAN"):
        await plugin.add_port_vlan("Port 3", 10)
    assert fake.logins == fake.logouts == 1


@pytest.mark.asyncio
async def test_wrong_password_and_busy_switch_are_reported(fake, monkeypatch):
    monkeypatch.setattr(QNAPQSSPlugin, "_transport", httpx.MockTransport(fake.handler))
    result = await QNAPQSSPlugin({"hostname": "192.0.2.10", "password": "nope"}).test_connection()
    assert result["success"] is False and "credentials" in result["message"]
    fake.busy = True
    result = await QNAPQSSPlugin({"hostname": "192.0.2.10", "password": PASSWORD}).test_connection()
    assert result["success"] is False and "busy" in result["message"]


@pytest.mark.asyncio
async def test_add_port_creates_vlan_tagged_and_keeps_native_vlan(plugin, fake):
    await plugin.create_vlan(3901, "pn-1")
    assert writes(fake) == []  # QSS VLANs are created with their first port
    result = await plugin.add_port_vlan("Port 5", 3901)
    assert result["changed"] is True
    assert fake.vlans["3901"] == [{"Port": "5", "Tagged": True}]
    assert {"Port": "5", "Tagged": False} in fake.vlans["1"]
    state = await plugin.get_port_vlans("Port 5")
    assert state["mode"] == "trunk" and state["native_vlan"] == 1 and 3901 in state["allowed_vlans"]


@pytest.mark.asyncio
async def test_add_port_to_existing_vlan_keeps_other_members_and_is_idempotent(plugin, fake):
    fake.vlans["3901"] = [{"Port": "7", "Tagged": True}]
    await plugin.add_port_vlan("5", 3901)
    assert fake.vlans["3901"] == [{"Port": "5", "Tagged": True}, {"Port": "7", "Tagged": True}]
    fake.calls.clear()
    assert (await plugin.add_port_vlan("5", 3901))["changed"] is False
    assert writes(fake) == []


@pytest.mark.asyncio
async def test_add_refuses_a_port_whose_untagged_vlan_it_is(plugin, fake):
    fake.vlans["3901"] = [{"Port": "5", "Tagged": False}]
    with pytest.raises(QSSError, match="untagged"):
        await plugin.add_port_vlan("5", 3901)


@pytest.mark.asyncio
async def test_remove_port_keeps_other_members_then_deletes_empty_vlan(plugin, fake):
    fake.vlans["3901"] = [{"Port": "5", "Tagged": True}, {"Port": "7", "Tagged": True}]
    await plugin.remove_port_vlan("Port 5", 3901)
    assert fake.vlans["3901"] == [{"Port": "7", "Tagged": True}]
    await plugin.remove_port_vlan("Port 7", 3901)
    assert "3901" not in fake.vlans
    assert ("DELETE", "/vlan", {"idx": ["3901"]}) in fake.calls


@pytest.mark.asyncio
async def test_remove_never_touches_untagged_membership(plugin, fake):
    fake.vlans["20"] = [{"Port": "5", "Tagged": False}, {"Port": "6", "Tagged": True}]
    fake.calls.clear()
    assert (await plugin.remove_port_vlan("5", 20))["changed"] is False
    assert writes(fake) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("vlan", [1, 0, 4001, 4094])
async def test_management_and_out_of_range_vlans_are_refused(plugin, fake, vlan):
    with pytest.raises((QSSError, ValueError)):
        await plugin.add_port_vlan("5", vlan)
    assert writes(fake) == []


@pytest.mark.asyncio
async def test_lag_members_and_lag_interfaces_are_refused(plugin, fake):
    fake.lag_groups = [{"key": "1", "val": {"PortMembers": ["9", "10"]}}]
    with pytest.raises(QSSError, match="link aggregation group"):
        await plugin.add_port_vlan("9", 3901)
    with pytest.raises(QSSError, match="link aggregation interface"):
        await plugin.add_port_vlan("29", 3901)
    assert writes(fake) == []


@pytest.mark.asyncio
async def test_delete_vlan_refuses_when_it_is_a_native_vlan(plugin, fake):
    fake.vlans["20"] = [{"Port": "5", "Tagged": False}]
    with pytest.raises(QSSError, match="untagged"):
        await plugin.delete_vlan(20)
    assert "20" in fake.vlans
    fake.vlans["3901"] = [{"Port": "5", "Tagged": True}]
    assert (await plugin.delete_vlan(3901))["changed"] is True
    assert "3901" not in fake.vlans
    assert (await plugin.delete_vlan(3901))["changed"] is False


@pytest.mark.asyncio
async def test_list_vlans_leaves_out_lag_interfaces(plugin, fake):
    fake.vlans["1"].append({"Port": "29", "Tagged": False})
    fake.vlans["20"] = [{"Port": "7", "Tagged": True}, {"Port": "30", "Tagged": True}]
    vlans = {v["vlan_id"]: v for v in await plugin.list_vlans()}
    assert vlans[1]["untagged_ports"] == [str(p) for p in range(1, 29)]
    assert vlans[20]["tagged_ports"] == ["7"]


@pytest.mark.asyncio
async def test_port_statistics_list_physical_ports_only(plugin):
    stats = await plugin.get_all_port_statistics()
    assert set(stats) == {"Port 1", "Port 2"}
    assert stats["Port 1"]["ifHighSpeed"] == 10000 and stats["Port 1"]["ifOperStatus"] == 1
    assert stats["Port 1"]["ifInOctets"] == 100 and stats["Port 1"]["ifOutOctets"] == 200
    assert stats["Port 2"]["ifAdminStatus"] == 2 and stats["Port 2"]["ifOperStatus"] == 2
