"""Client MCP (/client-mcp): keys, trust boundary, tenant isolation, scopes, confirm, presets."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from mcp.server.fastmcp.exceptions import ToolError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient as StarletteClient

from app.core.auth import get_current_user
from app.core.client_mcp_auth import (
    CLIENT_MCP_KEY_PREFIX,
    ClientMcpAuthContext,
    authenticate_client_mcp_bearer,
    hash_client_mcp_api_key,
    is_client_mcp_api_key,
)
from app.core.client_permissions import PermissionKey
from app.core.mcp_auth import MCPAuthError, authenticate_mcp_bearer, is_mcp_api_key
from app.dao.client_mcp_api_key_dao import ClientMcpApiKeyDAO
from app.dao.mcp_api_key_dao import McpApiKeyDAO
from app.dao.service_dao import ServiceDAO
from app.mcp.client import tools
from app.mcp.client.context import client_mcp_auth_ctx
from app.mcp.client.server import ClientMCPAuthASGI
from app.mcp.server import MCPAuthASGI
from app.models.location import Location
from app.models.permission_set import PermissionSet
from app.models.server import Server
from app.models.service import ProvisioningSource, ServiceStatus
from app.models.user import User
from app.plugins.base import PowerState
from tests.conftest import TestingSessionLocal


def _user(db, username, **kwargs):
    user = User(username=username, email=f"{username}@example.com", is_admin=False, **kwargs)
    user.set_password("secret123")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _server(db, name, ip, locked=False):
    loc = Location(name=f"{name}-loc", description="t")
    db.add(loc)
    db.commit()
    server = Server(
        name=name,
        server_ip=ip,
        location_id=loc.id,
        plugin_name="ipmi",
        plugin_config={"hostname": ip, "username": "admin", "password": "bmc-secret"},
        ipmi_viewer_username="viewer",
        ipmi_viewer_password="viewer-secret",
        locked=locked,
    )
    db.add(server)
    db.commit()
    db.refresh(server)
    return server


def _bm_service(db, owner, name, server):
    return ServiceDAO.create_bare_metal(
        db,
        name=name,
        server_id=server.id,
        owner_user_id=owner.id,
        status=ServiceStatus.ACTIVE,
        provisioning_source=ProvisioningSource.INTERNAL,
    )


def _vm_service(db, owner, name):
    return ServiceDAO.create_vm(
        db,
        name=name,
        owner_user_id=owner.id,
        status=ServiceStatus.ACTIVE,
        provisioning_source=ProvisioningSource.INTERNAL,
        config={"vm_ip_address": "192.0.2.50"},
    )


@pytest.fixture
def rate_limit_redis(mock_redis, monkeypatch):
    import app.core.rate_limit as rate_limit_module

    monkeypatch.setattr(rate_limit_module, "redis_client", mock_redis)
    return mock_redis


@pytest.fixture
def cmcp_db(monkeypatch, rate_limit_redis):
    monkeypatch.setattr("app.core.database.SessionLocal", TestingSessionLocal)
    monkeypatch.setattr("app.mcp.client.runtime.SessionLocal", TestingSessionLocal)
    return TestingSessionLocal


def _as(user, scopes, key_id=1):
    ctx = ClientMcpAuthContext(
        key_id=key_id,
        name="t",
        scopes=scopes,
        user_id=user.id,
        username=user.username,
        email=user.email,
        client_ip="127.0.0.1",
    )
    return client_mcp_auth_ctx.set(ctx)


@pytest.fixture
def as_client(cmcp_db):
    def _use(user, scopes):
        _as(user, scopes)

    yield _use
    client_mcp_auth_ctx.set(None)


# ---------------------------------------------------------------------------
# Keys and the trust boundary
# ---------------------------------------------------------------------------


def test_key_create_hashes_and_prefixes(db_session):
    user = _user(db_session, "alice")
    row = ClientMcpApiKeyDAO.create(db_session, user_id=user.id, name="laptop", scopes=["read"])
    plaintext = row.plaintext_api_key
    assert plaintext.startswith(CLIENT_MCP_KEY_PREFIX)
    assert is_client_mcp_api_key(plaintext)
    assert not is_mcp_api_key(plaintext)
    assert row.api_key == hash_client_mcp_api_key(plaintext) == hashlib.sha256(plaintext.encode()).hexdigest()
    assert plaintext not in row.api_key
    assert row.api_key_prefix == plaintext[:12]
    assert row.enabled is True and row.scopes == ["read"]
    assert ClientMcpApiKeyDAO.get_by_api_key(db_session, plaintext).id == row.id
    assert ClientMcpApiKeyDAO.get_by_api_key(db_session, plaintext + "x") is None


def test_staff_prefix_is_not_client_prefix():
    assert not is_client_mcp_api_key("rfmcp_abc")
    assert not is_mcp_api_key("rfcmcp_abc")


def test_authenticate_live_key_touches_last_used(db_session, cmcp_db):
    user = _user(db_session, "alice")
    row = ClientMcpApiKeyDAO.create(db_session, user_id=user.id, name="k", scopes=["write"])
    ctx = authenticate_client_mcp_bearer(row.plaintext_api_key, "198.51.100.4")
    assert ctx.user_id == user.id and ctx.scopes == ["write"]
    assert ctx.as_client_auth()["is_admin"] is False
    db_session.expire_all()
    fresh = ClientMcpApiKeyDAO.get_for_user(db_session, user.id, row.id)
    assert fresh.last_used_ip == "198.51.100.4" and fresh.last_used_at is not None


def test_authenticate_rejects_disabled_expired_and_unknown(db_session, cmcp_db):
    user = _user(db_session, "alice")
    off = ClientMcpApiKeyDAO.create(db_session, user_id=user.id, name="off", enabled=False)
    old = ClientMcpApiKeyDAO.create(
        db_session,
        user_id=user.id,
        name="old",
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    for token, message in (
        (off.plaintext_api_key, "disabled"),
        (old.plaintext_api_key, "expired"),
        (CLIENT_MCP_KEY_PREFIX + "nope", "Invalid"),
    ):
        with pytest.raises(MCPAuthError) as exc:
            authenticate_client_mcp_bearer(token, "127.0.0.1")
        assert exc.value.status_code == 401
        assert message.lower() in exc.value.detail.lower()


def test_staff_key_rejected_on_client_auth_and_client_key_on_staff_auth(db_session, cmcp_db):
    user = _user(db_session, "alice")
    staff = McpApiKeyDAO.create(db_session, name="ops", enabled=True, scopes=["destructive"])
    client_key = ClientMcpApiKeyDAO.create(db_session, user_id=user.id, name="k")
    with pytest.raises(MCPAuthError) as exc:
        authenticate_client_mcp_bearer(staff.plaintext_api_key, "127.0.0.1")
    assert exc.value.status_code == 401
    with pytest.raises(MCPAuthError) as exc:
        authenticate_mcp_bearer(client_key.plaintext_api_key, "127.0.0.1")
    assert exc.value.status_code == 401


def test_client_key_rejected_by_get_current_user_and_rest_api(db_session, client):
    user = _user(db_session, "alice")
    row = ClientMcpApiKeyDAO.create(db_session, user_id=user.id, name="k", scopes=["destructive"])
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "client": ("127.0.0.1", 1)})
    with pytest.raises(HTTPException) as exc:
        get_current_user(
            request=request,
            credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=row.plaintext_api_key),
            auth_token=None,
            db=db_session,
        )
    assert exc.value.status_code == 401
    headers = {"Authorization": f"Bearer {row.plaintext_api_key}"}
    for path in ("/api/services/me", "/api/client/services/me", "/api/client/mcp-keys", "/api/users/me"):
        assert client.get(path, headers=headers).status_code == 401


def test_auth_failures_are_rate_limited(db_session, cmcp_db, monkeypatch):
    import app.core.client_mcp_auth as module

    monkeypatch.setattr(module.settings, "mcp_rate_limit_per_ip", 1)
    with pytest.raises(MCPAuthError) as first:
        authenticate_client_mcp_bearer(CLIENT_MCP_KEY_PREFIX + "bad", "203.0.113.9")
    assert first.value.status_code == 401
    with pytest.raises(MCPAuthError) as second:
        authenticate_client_mcp_bearer(CLIENT_MCP_KEY_PREFIX + "bad", "203.0.113.9")
    assert second.value.status_code == 429


def test_key_stops_working_when_owner_changes(db_session, cmcp_db):
    user = _user(db_session, "alice")
    row = ClientMcpApiKeyDAO.create(db_session, user_id=user.id, name="k")
    token = row.plaintext_api_key
    authenticate_client_mcp_bearer(token, "127.0.0.1")

    preset = PermissionSet(name="no-mcp", permissions={PermissionKey.ACCOUNT_MCP: False})
    db_session.add(preset)
    db_session.commit()
    user.permission_set_id = preset.id
    db_session.commit()
    with pytest.raises(MCPAuthError):
        authenticate_client_mcp_bearer(token, "127.0.0.1")

    user.permission_set_id = None
    user.is_admin = True
    db_session.commit()
    with pytest.raises(MCPAuthError):
        authenticate_client_mcp_bearer(token, "127.0.0.1")

    user.is_admin = False
    user.is_reseller = True
    db_session.commit()
    with pytest.raises(MCPAuthError):
        authenticate_client_mcp_bearer(token, "127.0.0.1")

    user.is_reseller = False
    db_session.commit()
    authenticate_client_mcp_bearer(token, "127.0.0.1")

    db_session.delete(user)
    db_session.commit()
    with pytest.raises(MCPAuthError):
        authenticate_client_mcp_bearer(token, "127.0.0.1")


def _asgi_probe(wrapper):
    async def ok(request):
        return PlainTextResponse("inside")

    return StarletteClient(wrapper(Starlette(routes=[Route("/", ok, methods=["GET", "POST"])])))


def test_client_asgi_requires_client_key(db_session, cmcp_db):
    user = _user(db_session, "alice")
    client_key = ClientMcpApiKeyDAO.create(db_session, user_id=user.id, name="k")
    staff = McpApiKeyDAO.create(db_session, name="ops", enabled=True)
    http = _asgi_probe(ClientMCPAuthASGI)
    assert http.get("/").status_code == 401
    assert http.get("/", headers={"Authorization": f"Bearer {staff.plaintext_api_key}"}).status_code == 401
    ok = http.get("/", headers={"Authorization": f"Bearer {client_key.plaintext_api_key}"})
    assert ok.status_code == 200 and ok.text == "inside"


def test_staff_asgi_rejects_client_key(db_session, cmcp_db):
    user = _user(db_session, "alice")
    client_key = ClientMcpApiKeyDAO.create(db_session, user_id=user.id, name="k")
    http = _asgi_probe(MCPAuthASGI)
    assert http.get("/", headers={"Authorization": f"Bearer {client_key.plaintext_api_key}"}).status_code == 401


def test_client_endpoint_404_when_disabled(client):
    assert client.post("/client-mcp").status_code == 404
    assert client.get("/client-mcp/anything").status_code == 404


def test_tool_surface_is_compact():
    from app.mcp.client import server  # noqa: F401  (registers and slims)
    from app.mcp.client.instance import client_mcp

    listed = client_mcp._tool_manager.list_tools()
    assert sorted(t.name for t in listed) == ["account", "billing", "service_action", "services", "storage", "tickets"]
    assert '"title"' not in str([t.parameters for t in listed])


# ---------------------------------------------------------------------------
# Tools: isolation, scopes, confirm, presets
# ---------------------------------------------------------------------------


@pytest.fixture
def world(db_session):
    alice = _user(db_session, "alice")
    bob = _user(db_session, "bob")
    a_srv = _server(db_session, "a-srv", "10.1.0.1")
    b_srv = _server(db_session, "b-srv", "10.1.0.2")
    return {
        "alice": alice,
        "bob": bob,
        "a_bm": _bm_service(db_session, alice, "a-bm", a_srv),
        "b_bm": _bm_service(db_session, bob, "b-bm", b_srv),
        "a_vm": _vm_service(db_session, alice, "a-vm"),
        "b_vm": _vm_service(db_session, bob, "b-vm"),
    }


def _registry(state=PowerState.ON):
    plugin = Mock()
    plugin.get_power_state = AsyncMock(return_value=state)
    plugin.power_on = AsyncMock(return_value=True)
    plugin.power_off = AsyncMock(return_value=True)
    plugin.power_reset = AsyncMock(return_value=True)
    registry = patch("app.api.services_client.get_registry")
    return plugin, registry


@pytest.mark.asyncio
async def test_list_only_returns_own_services(world, as_client):
    as_client(world["alice"], ["read"])
    with _registry()[1] as reg:
        reg.return_value.get_plugin.return_value = _registry()[0]
        rows = await tools.services(view="list")
    assert {row["name"] for row in rows} == {"a-bm", "a-vm"}
    assert all(set(row) <= set(tools._LIST_FIELDS) for row in rows)


@pytest.mark.asyncio
async def test_detail_hides_bmc_and_staff_fields(world, as_client):
    as_client(world["alice"], ["read"])
    plugin, registry = _registry()
    with registry as reg:
        reg.return_value.get_plugin.return_value = plugin
        row = await tools.services(view="detail", service_id=world["a_bm"].id)
    assert row["name"] == "a-bm" and row["power_state"] == "on"
    flat = str(row)
    for leaked in ("viewer-secret", "bmc-secret", "ipmi_viewer", "a-srv", "server_enabled"):
        assert leaked not in flat


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call",
    [
        lambda s: tools.services(view="detail", service_id=s),
        lambda s: tools.services(view="backups", service_id=s),
        lambda s: tools.services(view="ssh_keys", service_id=s),
        lambda s: tools.services(view="actions", service_id=s),
        lambda s: tools.service_action(service_id=s, action="power_on", confirm=True),
        lambda s: tools.service_action(service_id=s, action="console", console_type="kvm"),
        lambda s: tools.service_action(service_id=s, action="backup_create"),
        lambda s: tools.service_action(service_id=s, action="backup_restore", volid="v", confirm=True),
        lambda s: tools.service_action(service_id=s, action="backup_delete", volid="v", confirm=True),
        lambda s: tools.service_action(service_id=s, action="set_ssh_keys", ssh_public_keys="k", confirm=True),
        lambda s: tools.service_action(service_id=s, action="reinstall", confirm=True),
        lambda s: tools.service_action(service_id=s, action="run_action", name="x", confirm=True),
    ],
)
@pytest.mark.parametrize("kind", ["b_bm", "b_vm"])
async def test_other_clients_service_is_not_found(world, as_client, call, kind):
    as_client(world["alice"], ["read", "write", "destructive"])
    with patch("app.api.services_client.get_registry") as reg:
        reg.return_value.get_plugin.side_effect = AssertionError("must not reach a plugin")
        with pytest.raises(ToolError, match="not found"):
            await call(world[kind].id)


@pytest.mark.asyncio
async def test_unknown_service_id_is_not_found(world, as_client):
    as_client(world["alice"], ["read"])
    with pytest.raises(ToolError, match="not found"):
        await tools.services(view="detail", service_id=999999)


@pytest.mark.asyncio
async def test_read_key_cannot_power_or_reinstall(world, as_client):
    as_client(world["alice"], ["read"])
    with pytest.raises(ToolError, match="lacks required scope: write"):
        await tools.service_action(service_id=world["a_bm"].id, action="power_on", confirm=True)
    with pytest.raises(ToolError, match="lacks required scope: destructive"):
        await tools.service_action(service_id=world["a_vm"].id, action="reinstall", confirm=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["reinstall", "backup_restore", "backup_delete", "run_action"])
async def test_write_key_cannot_do_destructive_actions(world, as_client, action):
    as_client(world["alice"], ["write"])
    with pytest.raises(ToolError, match="lacks required scope: destructive"):
        await tools.service_action(
            service_id=world["a_vm"].id, action=action, confirm=True, volid="v", name="x"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["power_on", "power_off", "reboot", "reset"])
async def test_power_requires_confirm(world, as_client, action):
    as_client(world["alice"], ["destructive"])
    with pytest.raises(ToolError, match="confirm=true"):
        await tools.service_action(service_id=world["a_bm"].id, action=action)


@pytest.mark.asyncio
async def test_reinstall_requires_confirm_and_destructive_runs(db_session, world, as_client):
    preset = PermissionSet(name="can-reinstall", permissions={PermissionKey.VM_REINSTALL: True})
    db_session.add(preset)
    db_session.commit()
    world["alice"].permission_set_id = preset.id
    db_session.commit()
    as_client(world["alice"], ["destructive"])
    with pytest.raises(ToolError, match="confirm=true"):
        await tools.service_action(service_id=world["a_vm"].id, action="reinstall")
    seen = []

    async def fake_reinstall(db, service, **kwargs):
        seen.append(service.id)
        return {"status": "ok", "message": "VM reinstall queued"}

    reinstall = AsyncMock(side_effect=fake_reinstall)
    with patch("app.services.vm_reinstall_service.reinstall_vm_guest", reinstall):
        result = await tools.service_action(service_id=world["a_vm"].id, action="reinstall", confirm=True)
    assert result["status"] == "ok"
    reinstall.assert_awaited_once()
    assert seen == [world["a_vm"].id]


@pytest.mark.asyncio
async def test_reinstall_denied_by_default_preset(world, as_client):
    as_client(world["alice"], ["destructive"])
    with pytest.raises(ToolError, match="vm.reinstall"):
        await tools.service_action(service_id=world["a_vm"].id, action="reinstall", confirm=True)


@pytest.mark.asyncio
async def test_power_runs_through_portal_handler(world, as_client):
    as_client(world["alice"], ["write"])
    plugin, registry = _registry()
    with registry as reg:
        reg.return_value.get_plugin.return_value = plugin
        result = await tools.service_action(service_id=world["a_bm"].id, action="reboot", confirm=True)
    assert result["status"] == "success" and result["action"] == "reboot"
    plugin.power_reset.assert_awaited_once()


@pytest.mark.asyncio
async def test_permission_preset_denial_is_honoured(db_session, world, as_client):
    preset = PermissionSet(name="no-power", permissions={PermissionKey.BMS_POWER: False})
    db_session.add(preset)
    db_session.commit()
    world["alice"].permission_set_id = preset.id
    db_session.commit()
    as_client(world["alice"], ["write"])
    plugin, registry = _registry()
    with registry as reg:
        reg.return_value.get_plugin.return_value = plugin
        with pytest.raises(ToolError, match="(?i)permission|not allowed|forbidden"):
            await tools.service_action(service_id=world["a_bm"].id, action="power_on", confirm=True)
    plugin.power_on.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_refuses_when_account_mcp_denied_after_auth(db_session, world, as_client):
    preset = PermissionSet(name="no-mcp", permissions={PermissionKey.ACCOUNT_MCP: False})
    db_session.add(preset)
    db_session.commit()
    world["alice"].permission_set_id = preset.id
    db_session.commit()
    as_client(world["alice"], ["read"])
    with pytest.raises(ToolError, match="no longer permitted"):
        await tools.account(view="me")


@pytest.mark.asyncio
async def test_locked_server_blocks_reinstall(db_session, world, as_client):
    srv = _server(db_session, "locked-srv", "10.1.0.9", locked=True)
    vm = _vm_service(db_session, world["alice"], "locked-vm")
    as_client(world["alice"], ["destructive"])
    reinstall = AsyncMock(return_value={})
    with patch("app.services.vm_reinstall_service.reinstall_vm_guest", reinstall), patch(
        "app.mcp.client.tools.service_linked_server", return_value=srv
    ):
        with pytest.raises(ToolError, match="Server is locked"):
            await tools.service_action(service_id=vm.id, action="reinstall", confirm=True)
    reinstall.assert_not_awaited()


@pytest.mark.asyncio
async def test_account_me_and_audit_logged(db_session, world, as_client):
    from app.models.commerce_audit import UserAuditEvent

    as_client(world["alice"], ["read"])
    assert (await tools.account(view="me"))["username"] == "alice"
    db_session.expire_all()
    events = db_session.query(UserAuditEvent).filter(UserAuditEvent.action == "client_mcp.account").all()
    assert len(events) == 1 and events[0].subject_user_id == world["alice"].id


@pytest.mark.asyncio
async def test_tickets_are_scoped_to_the_account(db_session, world, as_client):
    from app.models.support_ticket import TicketDepartment

    db_session.add(TicketDepartment(name="Support", code="support"))
    db_session.commit()
    dept = db_session.query(TicketDepartment).first()

    as_client(world["bob"], ["write"])
    bob_ticket = await tools.tickets(action="open", department_id=dept.id, subject="bob", body="hi")

    as_client(world["alice"], ["write"])
    assert await tools.tickets(action="list") == []
    with pytest.raises(ToolError, match="not found"):
        await tools.tickets(action="get", ticket_id=bob_ticket["id"])
    with pytest.raises(ToolError, match="not found"):
        await tools.tickets(action="reply", ticket_id=bob_ticket["id"], body="hello")
    with pytest.raises(ToolError, match="not found"):
        await tools.tickets(
            action="open", department_id=dept.id, subject="x", body="y", service_id=world["b_vm"].id
        )
    own = await tools.tickets(
        action="open", department_id=dept.id, subject="mine", body="y", service_id=world["a_vm"].id
    )
    assert (await tools.tickets(action="get", ticket_id=own["id"]))["subject"] == "mine"


@pytest.mark.asyncio
async def test_read_key_cannot_open_ticket(world, as_client):
    as_client(world["alice"], ["read"])
    with pytest.raises(ToolError, match="lacks required scope: write"):
        await tools.tickets(action="open", department_id=1, subject="s", body="b")


@pytest.mark.asyncio
async def test_billing_scoped_to_own_account(db_session, world, as_client):
    as_client(world["alice"], ["read"])
    assert await tools.billing(view="invoices") == []
    with pytest.raises(ToolError, match="not found"):
        await tools.billing(view="invoice", id=12345)


# ---------------------------------------------------------------------------
# Client portal REST: create/list/disable/delete, impersonation, presets, staff view
# ---------------------------------------------------------------------------


def _login(client, username, password="secret123"):
    resp = client.post("/api/users/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


@pytest.fixture
def mcp_on(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "client_mcp_enabled", True)


def test_client_creates_lists_disables_deletes_keys(client, db_session, mcp_on):
    alice = _user(db_session, "alice")
    bob = _user(db_session, "bob")
    headers = _login(client, "alice")

    created = client.post(
        "/api/client/mcp-keys",
        headers=headers,
        json={"name": "laptop", "scopes": ["read", "write"], "expires_in_days": 30},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["api_key"].startswith(CLIENT_MCP_KEY_PREFIX)
    assert body["scopes"] == ["read", "write"] and body["expires_at"]

    listing = client.get("/api/client/mcp-keys", headers=headers).json()
    assert listing["allowed"] is True and listing["server_enabled"] is True
    assert listing["endpoint"].endswith("/client-mcp")
    assert len(listing["keys"]) == 1 and "api_key" not in listing["keys"][0]
    assert body["api_key"] not in str(listing)

    key_id = body["id"]
    assert client.patch(f"/api/client/mcp-keys/{key_id}", headers=headers, json={"enabled": False}).json()["enabled"] is False
    with pytest.raises(MCPAuthError):
        with patch("app.core.database.SessionLocal", TestingSessionLocal):
            authenticate_client_mcp_bearer(body["api_key"], "127.0.0.1")

    other = ClientMcpApiKeyDAO.create(db_session, user_id=bob.id, name="bobs")
    assert client.delete(f"/api/client/mcp-keys/{other.id}", headers=headers).status_code == 404
    assert client.patch(f"/api/client/mcp-keys/{other.id}", headers=headers, json={"enabled": False}).status_code == 404

    assert client.delete(f"/api/client/mcp-keys/{key_id}", headers=headers).status_code == 204
    assert client.get("/api/client/mcp-keys", headers=headers).json()["keys"] == []
    assert ClientMcpApiKeyDAO.list_for_user(db_session, alice.id) == []


def test_create_validates_scopes_limit_and_flag(client, db_session, monkeypatch):
    from app.core.config import settings

    _user(db_session, "alice")
    headers = _login(client, "alice")
    assert client.post("/api/client/mcp-keys", headers=headers, json={"name": "k"}).status_code == 503
    monkeypatch.setattr(settings, "client_mcp_enabled", True)
    assert client.post("/api/client/mcp-keys", headers=headers, json={"name": "k", "scopes": ["root"]}).status_code == 400
    monkeypatch.setattr(settings, "client_mcp_max_keys_per_user", 1)
    assert client.post("/api/client/mcp-keys", headers=headers, json={"name": "a"}).status_code == 201
    assert client.post("/api/client/mcp-keys", headers=headers, json={"name": "b"}).status_code == 400


def test_preset_denial_blocks_creation_and_enabling(client, db_session, mcp_on):
    alice = _user(db_session, "alice")
    key = ClientMcpApiKeyDAO.create(db_session, user_id=alice.id, name="old", enabled=False)
    preset = PermissionSet(name="no-mcp", permissions={PermissionKey.ACCOUNT_MCP: False})
    db_session.add(preset)
    db_session.commit()
    alice.permission_set_id = preset.id
    db_session.commit()
    headers = _login(client, "alice")
    assert client.post("/api/client/mcp-keys", headers=headers, json={"name": "k"}).status_code == 403
    assert client.patch(f"/api/client/mcp-keys/{key.id}", headers=headers, json={"enabled": True}).status_code == 403
    listing = client.get("/api/client/mcp-keys", headers=headers).json()
    assert listing["allowed"] is False
    assert client.delete(f"/api/client/mcp-keys/{key.id}", headers=headers).status_code == 204


def test_impersonation_session_cannot_create_or_change_keys(client, db_session, mock_redis, mcp_on):
    alice = _user(db_session, "alice")
    key = ClientMcpApiKeyDAO.create(db_session, user_id=alice.id, name="k")
    headers = _login(client, "alice")
    token = headers["Authorization"].split()[1]
    mock_redis._hashes[f"tok:{hashlib.sha256(token.encode()).hexdigest()}"]["impersonated_by"] = "99"

    assert client.post("/api/client/mcp-keys", headers=headers, json={"name": "k2"}).status_code == 403
    assert client.patch(f"/api/client/mcp-keys/{key.id}", headers=headers, json={"enabled": False}).status_code == 403
    assert client.delete(f"/api/client/mcp-keys/{key.id}", headers=headers).status_code == 403
    listing = client.get("/api/client/mcp-keys", headers=headers).json()
    assert listing["can_manage"] is False and len(listing["keys"]) == 1


def test_staff_and_anonymous_cannot_use_client_key_routes(client, db_session, test_admin_user, mcp_on):
    assert client.get("/api/client/mcp-keys").status_code == 401
    headers = _login(client, "admin", "adminpassword123")
    assert client.get("/api/client/mcp-keys", headers=headers).status_code == 403
    assert client.post("/api/client/mcp-keys", headers=headers, json={"name": "k"}).status_code == 403


def test_staff_can_list_and_revoke_client_keys(client, db_session, test_admin_user, mcp_on):
    alice = _user(db_session, "alice")
    key = ClientMcpApiKeyDAO.create(db_session, user_id=alice.id, name="k")
    admin = _login(client, "admin", "adminpassword123")
    rows = client.get(f"/api/admin/clients/{alice.id}/mcp-keys", headers=admin).json()
    assert [r["id"] for r in rows] == [key.id] and "api_key" not in rows[0]

    assert client.patch(
        f"/api/admin/clients/{alice.id}/mcp-keys/{key.id}", headers=admin, json={"enabled": False}
    ).json()["enabled"] is False
    assert client.delete(f"/api/admin/clients/{alice.id}/mcp-keys/{key.id}", headers=admin).status_code == 204
    assert client.get(f"/api/admin/clients/{alice.id}/mcp-keys", headers=admin).json() == []

    # a client session cannot use the staff routes
    client_headers = _login(client, "alice")
    assert client.get(f"/api/admin/clients/{alice.id}/mcp-keys", headers=client_headers).status_code == 403


@pytest.mark.asyncio
async def test_fast_path_view_and_toggle_are_owner_scoped(db_session, as_client, monkeypatch):
    from app.dao.ipam_dao import IPAMDAO
    from app.dao.private_network_dao import PrivateNetworkDAO
    from app.dao.proxy_runner_dao import ProxyRunnerDAO
    from app.models.service import ServiceType

    alice, bob = _user(db_session, "alice"), _user(db_session, "bob")
    location = Location(name="fast-loc", description="")
    db_session.add(location)
    db_session.commit()
    pool = PrivateNetworkDAO.create_pool(
        db_session, location_id=location.id, vlan_start=2200, vlan_end=2209, bridge="vmbr0",
        max_networks_per_client=2, max_networks_per_service=4,
    )
    pool.proxy_access_enabled = True
    runner, _ = ProxyRunnerDAO.create(db_session, name="fast-r", api_key="kfr")
    runner.location_id = location.id
    db_session.commit()
    subnet = IPAMDAO.create_subnet(db_session, name="fast", cidr="203.0.113.64/29", location_id=location.id)
    subnet.proxy_runner_id, subnet.fast_path_ip = runner.id, "100.80.0.9"
    db_session.commit()
    srv = _server(db_session, "px-srv", "10.1.0.9")
    proxy = ServiceDAO.create_bare_metal(
        db_session, name="a-px", server_id=srv.id, owner_user_id=alice.id, service_type=ServiceType.HTTP_PROXY,
        status=ServiceStatus.ACTIVE, provisioning_source=ProvisioningSource.INTERNAL,
    )
    IPAMDAO.assign_ip(db_session, proxy.id, subnet_id=subnet.id, username="ua", password="pa-secret")

    as_client(alice, ["read", "write"])
    view = await tools.services(view="fast_path", service_id=proxy.id)
    assert [(e["fast_path_ip"], e["active"]) for e in view["endpoints"]] == [("100.80.0.9", False)]
    with pytest.raises(ToolError, match="confirm"):
        await tools.service_action(proxy.id, "fast_path_on")
    on = await tools.service_action(proxy.id, "fast_path_on", confirm=True)
    assert on["endpoints"][0]["active"] is True and on["locations"][0]["enabled"] is True

    as_client(bob, ["read", "write"])
    assert (await tools.services(view="fast_path"))["endpoints"] == []
    with pytest.raises(ToolError, match="no proxy IPs"):
        await tools.service_action(proxy.id, "fast_path_off", confirm=True)

    as_client(alice, ["read", "write"])
    off = await tools.service_action(proxy.id, "fast_path_off", confirm=True)
    assert off["locations"][0]["enabled"] is False
    again = await tools.service_action(proxy.id, "fast_path_off", confirm=True)
    assert again["locations"][0]["enabled"] is False
