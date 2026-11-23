# ruff: noqa: F811
"""VM firewall through the client MCP (services view=firewall, service_action action=firewall) and the staff tool."""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from app.core.mcp_auth import McpAuthContext
from app.mcp.client import tools
from app.mcp.client.context import client_mcp_auth_ctx
from app.mcp.context import mcp_auth_ctx
from app.mcp.tools import firewall as staff_firewall
from app.models.commerce_audit import UserAuditEvent
from app.models.vm_firewall import ServiceFirewall
from app.services.vm_firewall import service as svc
from tests.api.test_vm_firewall_client_api import proxmox, vms, world  # noqa: F401  (fixtures)
from tests.api.vm_firewall_helpers import FLAGGED_NET0, SSH, WEB, fake_proxmox
from tests.mcp.test_client_mcp import as_client, cmcp_db, rate_limit_redis  # noqa: F401  (fixtures)


@pytest.fixture(autouse=True)
def _reset_context():
    yield
    client_mcp_auth_ctx.set(None)


def _error(exc_info) -> dict:
    return json.loads(str(exc_info.value))


async def _fw(user, scopes, op, as_client, service_id, confirm=False, **params):
    as_client(user, scopes)
    return await tools.service_action(action="firewall", op=op, service_id=service_id, params=params, confirm=confirm)


async def _view(user, as_client, service_id, scopes=("read",)):
    as_client(user, list(scopes))
    return await tools.services(view="firewall", service_id=service_id)


def _events(db, service_id, action=None):
    db.expire_all()
    query = db.query(UserAuditEvent).filter(
        UserAuditEvent.resource_type == "service", UserAuditEvent.resource_id == str(service_id)
    )
    if action:
        query = query.filter(UserAuditEvent.action == action)
    return query.order_by(UserAuditEvent.id).all()


# Client MCP


@pytest.mark.asyncio
async def test_view_is_the_portal_view_with_attachable_groups(db_session, world, vms, as_client):
    alice, alice_vm = world.users["alice"], vms[0]
    group = svc.create_group(db_session, world.actor("alice"), name="web", rules=[WEB])
    db_session.commit()
    view = await _view(alice, as_client, alice_vm.id)
    assert view["available"] is True and view["revision"] == 1 and view["client_enabled"] is False
    assert [g["id"] for g in view["available_groups"]] == [group.id]
    assert "managed" not in view and "rendered" not in view


@pytest.mark.asyncio
async def test_another_clients_vm_is_not_found_for_view_and_every_op(db_session, world, vms, as_client):
    alice = world.users["alice"]
    bob_vm = vms[1]
    with pytest.raises(ToolError) as exc:
        await _view(alice, as_client, bob_vm.id)
    assert _error(exc) == {"code": "not_found", "field": None, "message": "Service not found"}
    with pytest.raises(ToolError) as missing:
        await _view(alice, as_client, 999999)
    assert str(missing.value) == str(exc.value)
    calls = {
        "enable": {"confirm_interruption": True},
        "policy": {"policy_in": "ACCEPT"},
        "add_rule": {"rule": SSH},
        "delete_rule": {"rule_id": 1},
        "replace_rules": {"rules": [SSH]},
        "preset": {"preset": "ssh"},
        "attach_group": {"group_id": 1},
        "detach_group": {"group_id": 1},
        "disable": {},
    }
    for op, params in calls.items():
        with pytest.raises(ToolError) as refused:
            await _fw(alice, ["write"], op, as_client, bob_vm.id, confirm=True, **params)
        assert _error(refused)["code"] == "not_found", op
    assert db_session.get(ServiceFirewall, bob_vm.id) is None


@pytest.mark.asyncio
async def test_permission_false_blocks_writes_but_not_the_view(db_session, world, vms, as_client):
    alice, alice_vm = world.users["alice"], vms[0]
    alice_vm.permission_overrides = {"vm.firewall": False}
    db_session.commit()
    assert (await _view(alice, as_client, alice_vm.id))["available"] is True
    for op, params in (("enable", {"confirm_interruption": True}), ("preset", {"preset": "ssh"}), ("disable", {})):
        with pytest.raises(ToolError) as exc:
            await _fw(alice, ["write"], op, as_client, alice_vm.id, confirm=True, **params)
        body = _error(exc)
        assert body["code"] == "forbidden" and "vm.firewall" in body["message"]
    assert db_session.get(ServiceFirewall, alice_vm.id) is None


@pytest.mark.asyncio
async def test_scope_and_confirm(db_session, world, vms, proxmox, as_client):
    alice, alice_vm = world.users["alice"], vms[0]
    with pytest.raises(ToolError, match="lacks required scope: write"):
        await _fw(alice, ["read"], "preset", as_client, alice_vm.id, preset="ssh")
    with pytest.raises(ToolError, match="confirm=true"):
        await _fw(alice, ["write"], "disable", as_client, alice_vm.id)
    with pytest.raises(ToolError, match="confirm=true"):
        await _fw(alice, ["write"], "replace_rules", as_client, alice_vm.id, rules=[SSH])
    with pytest.raises(ToolError, match="needs op"):
        await _fw(alice, ["write"], "nonsense", as_client, alice_vm.id)
    with pytest.raises(ToolError, match="needs: service_id"):
        await _fw(alice, ["write"], "enable", as_client, None)
    # Other ops need no confirm=true.
    view = await _fw(alice, ["write"], "preset", as_client, alice_vm.id, preset="ssh")
    assert [r["ports"] for r in view["rules"]][0] == "22"
    assert proxmox.writes == []


@pytest.mark.asyncio
async def test_enable_needs_the_interruption_confirmation_and_audits_the_client(db_session, world, vms, proxmox, as_client):
    alice, alice_vm = world.users["alice"], vms[0]
    with pytest.raises(ToolError) as exc:
        await _fw(alice, ["write"], "enable", as_client, alice_vm.id)
    body = _error(exc)
    assert body["code"] == "interruption_confirmation_required" and body["field"] == "confirm_interruption"
    assert set(body) == {"code", "field", "message"}
    assert db_session.get(ServiceFirewall, alice_vm.id) is None

    view = await _fw(alice, ["write"], "enable", as_client, alice_vm.id, confirm_interruption=True)
    assert view["client_enabled"] is True and view["state"] == "pending"
    rows = _events(db_session, alice_vm.id, "firewall.enabled")
    assert len(rows) == 1 and rows[0].actor_user_id == alice.id
    assert _events(db_session, alice_vm.id, "client_mcp.service_firewall")[0].actor_user_id == alice.id

    with pytest.raises(ToolError) as exc:
        await _fw(alice, ["destructive"], "disable", as_client, alice_vm.id, confirm=False)
    assert "confirm=true" in str(exc.value)
    off = await _fw(alice, ["write"], "disable", as_client, alice_vm.id, confirm=True)
    assert off["client_enabled"] is False
    assert [r.actor_user_id for r in _events(db_session, alice_vm.id, "firewall.disabled")] == [alice.id]


@pytest.mark.asyncio
async def test_enable_with_net0_flag_on_needs_no_confirmation(db_session, world, vms, monkeypatch, as_client):
    fake_proxmox(monkeypatch, world, vms, net0=FLAGGED_NET0)
    view = await _fw(world.users["alice"], ["write"], "enable", as_client, vms[0].id)
    assert view["client_enabled"] is True


@pytest.mark.asyncio
async def test_rule_preset_policy_and_group_edits(db_session, world, vms, proxmox, as_client):
    alice, alice_vm = world.users["alice"], vms[0]
    group = svc.create_group(db_session, world.actor("alice"), name="web", rules=[WEB])
    db_session.commit()

    view = await _fw(alice, ["write"], "add_rule", as_client, alice_vm.id, rule=SSH)
    ssh_id = view["rules"][0]["id"]
    assert view["revision"] == 2
    view = await _fw(alice, ["write"], "policy", as_client, alice_vm.id, policy_out="DROP")
    assert view["policy_out"] == "DROP"
    view = await _fw(alice, ["write"], "attach_group", as_client, alice_vm.id, group_id=group.id)
    assert [r["kind"] for r in view["rules"]] == ["rule", "group"]
    view = await _fw(alice, ["write"], "detach_group", as_client, alice_vm.id, group_id=group.id)
    assert [r["kind"] for r in view["rules"]] == ["rule"]
    view = await _fw(alice, ["write"], "preset", as_client, alice_vm.id, preset="web")
    assert len(view["rules"]) > 1 and view["rules"][-1]["comment"].startswith("Web")
    view = await _fw(alice, ["write"], "delete_rule", as_client, alice_vm.id, rule_id=ssh_id)
    assert ssh_id not in [r["id"] for r in view["rules"]]
    view = await _fw(alice, ["write"], "replace_rules", as_client, alice_vm.id, confirm=True, rules=[SSH, WEB])
    assert [r["ports"] for r in view["rules"]] == ["22", "80"]
    assert all(r.actor_user_id == alice.id for r in _events(db_session, alice_vm.id) if r.action.startswith("firewall."))
    assert proxmox.writes == []


@pytest.mark.asyncio
async def test_missing_params_and_bad_rules_answer_field_errors(db_session, world, vms, as_client):
    alice, alice_vm = world.users["alice"], vms[0]
    for op, match in (("add_rule", "rule"), ("delete_rule", "rule_id"), ("preset", "preset"), ("policy", "policy_in"),
                      ("attach_group", "group_id")):
        with pytest.raises(ToolError, match=match):
            await _fw(alice, ["write"], op, as_client, alice_vm.id)
    with pytest.raises(ToolError) as exc:
        await _fw(alice, ["write"], "add_rule", as_client, alice_vm.id, rule={**SSH, "ports": "99999"})
    body = _error(exc)
    assert body["code"] and body["field"] and body["message"]
    with pytest.raises(ToolError) as exc:
        await _fw(alice, ["write"], "policy", as_client, alice_vm.id, policy_in="MAYBE")
    assert _error(exc)["field"] == "policy_in"
    with pytest.raises(ToolError) as exc:
        await _fw(alice, ["write"], "add_rule", as_client, alice_vm.id, rule="ssh")
    assert _error(exc)["code"] == "invalid_value"


@pytest.mark.asyncio
async def test_a_concurrent_change_answers_firewall_changed(db_session, world, vms, as_client):
    alice, alice_vm = world.users["alice"], vms[0]
    await _fw(alice, ["write"], "add_rule", as_client, alice_vm.id, rule=SSH)
    with pytest.raises(ToolError) as exc:
        await _fw(alice, ["write"], "add_rule", as_client, alice_vm.id, rule=WEB, revision=1)
    body = _error(exc)
    assert body["code"] == "firewall_changed" and body["field"] == "revision" and body["current_revision"] == 2
    view = await _fw(alice, ["write"], "add_rule", as_client, alice_vm.id, rule=WEB, revision=2)
    assert len(view["rules"]) == 2


@pytest.mark.asyncio
async def test_hidden_admin_group_answers_like_a_missing_one(db_session, world, vms, as_client):
    alice, alice_vm = world.users["alice"], vms[0]
    hidden = svc.create_group(db_session, world.staff_actor, name="internal", rules=[SSH])
    shown = svc.create_group(db_session, world.staff_actor, name="offered", rules=[WEB], client_selectable=True)
    db_session.commit()
    with pytest.raises(ToolError) as hid:
        await _fw(alice, ["write"], "attach_group", as_client, alice_vm.id, group_id=hidden.id)
    with pytest.raises(ToolError) as absent:
        await _fw(alice, ["write"], "attach_group", as_client, alice_vm.id, group_id=999999)
    assert str(hid.value) == str(absent.value) and _error(hid)["code"] == "not_found"
    with pytest.raises(ToolError) as hid_rule:
        await _fw(alice, ["write"], "replace_rules", as_client, alice_vm.id, confirm=True,
                  rules=[{"kind": "group", "group_id": hidden.id}])
    with pytest.raises(ToolError) as absent_rule:
        await _fw(alice, ["write"], "replace_rules", as_client, alice_vm.id, confirm=True,
                  rules=[{"kind": "group", "group_id": 999999}])
    assert str(hid_rule.value) == str(absent_rule.value)
    view = await _view(alice, as_client, alice_vm.id)
    assert [g["id"] for g in view["available_groups"]] == [shown.id]
    attached = await _fw(alice, ["write"], "attach_group", as_client, alice_vm.id, group_id=shown.id)
    assert attached["rules"][0]["group_id"] == shown.id


@pytest.mark.asyncio
async def test_view_holds_no_internal_address_or_hidden_name(db_session, world, vms, as_client):
    from tests.api.vm_firewall_helpers import assert_nothing_hidden, seed_hidden_admin_objects

    alice, alice_vm = world.users["alice"], vms[0]
    seed_hidden_admin_objects(db_session, world.staff_actor, alice_vm.id)
    view = await _view(alice, as_client, alice_vm.id)
    assert_nothing_hidden(view)
    assert [g["name"] for g in view["groups"]] == ["Provided by us"]


@pytest.mark.asyncio
async def test_attached_hidden_group_id_answers_like_a_missing_id_and_shows_nothing_more(db_session, world, vms, as_client):
    from tests.api.vm_firewall_helpers import seed_hidden_admin_objects

    alice, alice_vm = world.users["alice"], vms[0]
    hidden = seed_hidden_admin_objects(db_session, world.staff_actor, alice_vm.id)["group"]
    other_vm = world.vm("alice")
    db_session.commit()
    entry = {"id": hidden.id, "name": "Provided by us", "description": None, "provided_by_us": True, "rules": []}
    view = await _view(alice, as_client, alice_vm.id)
    assert view["groups"] == [entry]
    assert hidden.id not in [g["id"] for g in view["available_groups"]]

    async def error(op, service_id, **params):
        with pytest.raises(ToolError) as exc:
            await _fw(alice, ["write"], op, as_client, service_id, confirm=True, **params)
        return _error(exc)

    async def calls(group_id):
        ref = {"kind": "group", "group_id": group_id}
        return {
            "attach on another VM": await error("attach_group", other_vm.id, group_id=group_id),
            "detach on another VM": await error("detach_group", other_vm.id, group_id=group_id),
            "rules on another VM": await error("replace_rules", other_vm.id, rules=[ref]),
            "add rule on another VM": await error("add_rule", other_vm.id, rule=ref),
        }

    hidden_calls = await calls(hidden.id)
    assert hidden_calls == await calls(hidden.id + 100000)
    assert hidden_calls["attach on another VM"]["code"] == "not_found"
    assert hidden_calls["rules on another VM"]["code"] == "reference_not_found"
    saved = await _fw(alice, ["write"], "replace_rules", as_client, alice_vm.id, confirm=True, rules=view["rules"])
    assert saved["groups"] == [entry]


@pytest.mark.asyncio
async def test_rules_staff_set_cannot_be_removed_through_the_mcp(db_session, world, vms, as_client):
    from tests.api.vm_firewall_helpers import seed_hidden_admin_objects

    alice, alice_vm = world.users["alice"], vms[0]
    hidden = seed_hidden_admin_objects(db_session, world.staff_actor, alice_vm.id)["group"]
    view = await _view(alice, as_client, alice_vm.id)
    group_rule, ipset_rule = view["rules"]
    for op, params in (
        ("replace_rules", {"rules": [ipset_rule]}),
        ("replace_rules", {"rules": [group_rule, SSH]}),
        ("delete_rule", {"rule_id": group_rule["id"]}),
        ("delete_rule", {"rule_id": ipset_rule["id"]}),
        ("detach_group", {"group_id": hidden.id}),
    ):
        with pytest.raises(ToolError) as exc:
            await _fw(alice, ["write"], op, as_client, alice_vm.id, confirm=True, **params)
        assert _error(exc)["code"] == "staff_attached", (op, params)
    view_after = await _view(alice, as_client, alice_vm.id)
    assert view_after == view
    added = await _fw(alice, ["write"], "add_rule", as_client, alice_vm.id, rule=SSH)
    assert [r["id"] for r in added["rules"][:2]] == [group_rule["id"], ipset_rule["id"]]


@pytest.mark.asyncio
async def test_suspended_vm_refuses_firewall_ops(db_session, world, vms, as_client):
    from app.models.service import ServiceStatus

    alice, alice_vm = world.users["alice"], vms[0]
    alice_vm.status = ServiceStatus.SUSPENDED
    db_session.commit()
    with pytest.raises(ToolError) as exc:
        await _fw(alice, ["read", "write"], "add_rule", as_client, alice_vm.id, rule=SSH)
    assert _error(exc)["code"] == "service_not_active"


@pytest.mark.asyncio
async def test_another_clients_group_is_not_attachable(db_session, world, vms, as_client):
    alice, alice_vm = world.users["alice"], vms[0]
    theirs = svc.create_group(db_session, world.actor("bob"), name="bobs", rules=[WEB])
    db_session.commit()
    with pytest.raises(ToolError) as exc:
        await _fw(alice, ["write"], "attach_group", as_client, alice_vm.id, group_id=theirs.id)
    assert _error(exc)["code"] == "not_found"
    assert (await _view(alice, as_client, alice_vm.id))["available_groups"] == []


@pytest.mark.asyncio
async def test_not_eligible_location_is_reported_with_reasons(db_session, world, vms, proxmox, as_client):
    from app.models.vm_firewall import FirewallLocationSettings

    db_session.get(FirewallLocationSettings, world.location.id).enabled = False
    db_session.commit()
    with pytest.raises(ToolError) as exc:
        await _fw(world.users["alice"], ["write"], "enable", as_client, vms[0].id, confirm_interruption=True)
    body = _error(exc)
    assert body["code"] == "not_eligible" and "location_disabled" in body["reasons"]


def test_client_tools_list_names_the_firewall_view_and_action():
    from app.mcp.client import server  # noqa: F401
    from app.mcp.client.instance import client_mcp

    listed = {t.name: t for t in client_mcp._tool_manager.list_tools()}
    assert "firewall" in listed["services"].parameters["properties"]["view"]["enum"]
    assert "firewall" in listed["service_action"].parameters["properties"]["action"]["enum"]
    assert "op" in listed["service_action"].parameters["properties"]


# Staff MCP


@pytest.fixture
def staff(world, mcp_sessionlocal):
    def _use(scope):
        ctx = McpAuthContext(
            key_id=9, name="staff", scopes=[scope], created_by_user_id=world.staff.id, client_ip="127.0.0.1"
        )
        return mcp_auth_ctx.set(ctx)

    token = _use("write")
    yield _use
    try:
        mcp_auth_ctx.reset(token)
    except ValueError:
        mcp_auth_ctx.set(None)


@pytest.mark.asyncio
async def test_staff_get_set_check_and_apply(db_session, world, vms, proxmox, staff):
    alice_vm = vms[0]
    fw = staff_firewall.firewall
    view = await fw(action="get", service_id=alice_vm.id)
    assert view["managed"] is True and "rendered" in view and view["client_enabled"] is False
    location = await fw(action="get", location_id=world.location.id)
    assert location["location_id"] == world.location.id and location["enabled"] is True
    with pytest.raises(ToolError, match="exactly one of"):
        await fw(action="get")

    with pytest.raises(ToolError) as exc:
        await fw(action="set", service_id=alice_vm.id, enabled=True)
    assert json.loads(str(exc.value))["code"] == "interruption_confirmation_required"
    done = await fw(action="set", service_id=alice_vm.id, enabled=True, policy_in="ACCEPT", confirm=True)
    assert done["client_enabled"] is True and done["policy_in"] == "ACCEPT"
    rows = _events(db_session, alice_vm.id, "firewall.enabled")
    assert [r.actor_user_id for r in rows] == [world.staff.id]

    replaced = await fw(action="set", service_id=alice_vm.id, rules=[SSH], confirm=True)
    assert [r["ports"] for r in replaced["rules"]] == ["22"]

    checked = await fw(action="check", service_id=alice_vm.id)
    assert checked["in_sync"] is False
    applied = await fw(action="apply", service_id=alice_vm.id)
    assert applied["result"]["ok"] is True and proxmox.writes
    assert [r.actor_user_id for r in _events(db_session, alice_vm.id, "firewall.apply_requested")] == [world.staff.id]
    assert (await fw(action="check", service_id=alice_vm.id))["in_sync"] is True


@pytest.mark.asyncio
async def test_staff_set_needs_confirm_for_disable_and_rules(db_session, world, vms, proxmox, staff):
    fw = staff_firewall.firewall
    with pytest.raises(ToolError, match="confirm=true"):
        await fw(action="set", service_id=vms[0].id, enabled=False)
    with pytest.raises(ToolError, match="confirm=true"):
        await fw(action="set", service_id=vms[0].id, rules=[SSH])
    with pytest.raises(ToolError, match="needs one of"):
        await fw(action="set", service_id=vms[0].id)
    assert db_session.get(ServiceFirewall, vms[0].id) is None
    done = await fw(action="set", service_id=vms[0].id, policy_out="DROP")
    assert done["policy_out"] == "DROP"


@pytest.mark.asyncio
async def test_staff_scopes_and_errors(db_session, world, vms, proxmox, staff):
    fw = staff_firewall.firewall
    staff("read")
    assert (await fw(action="get", service_id=vms[0].id))["service_id"] == vms[0].id
    assert (await fw(action="status"))["problems"] == []
    for action in ("set", "apply", "readiness"):
        with pytest.raises(ToolError, match="lacks required scope: write"):
            await fw(action=action, service_id=vms[0].id, location_id=world.location.id, policy_in="DROP")
    with pytest.raises(ToolError) as exc:
        await fw(action="check", service_id=vms[0].id)
    assert json.loads(str(exc.value))["code"] == "firewall_not_found"
    with pytest.raises(ToolError) as missing:
        await fw(action="get", service_id=999999)
    assert json.loads(str(missing.value))["code"] == "not_found"


@pytest.mark.asyncio
async def test_staff_status_lists_failing_vms_and_readiness_rechecks(db_session, world, vms, proxmox, staff):
    fw = staff_firewall.firewall
    row = svc.get_or_create(db_session, vms[0].id)
    row.state = "error"
    row.last_error = "boom"
    db_session.commit()
    status = await fw(action="status", location_id=world.location.id)
    assert status["worker"] == {}
    assert [(p["service_id"], p["last_error"]) for p in status["problems"]] == [(vms[0].id, "boom")]
    assert (await fw(action="status", location_id=world.location.id + 1000))["problems"] == []

    checked = await fw(action="readiness", location_id=world.location.id)
    assert checked["readiness_checked_at"] is not None
    with pytest.raises(ToolError, match="needs: location_id"):
        await fw(action="readiness")


def test_staff_tool_text_and_schema_fit_the_budget():
    import app.mcp.server  # noqa: F401
    from app.mcp.instance import mcp

    tool = next(t for t in asyncio.run(mcp.list_tools()) if t.name == "firewall")
    size = len(tool.description.encode()) + len(json.dumps(tool.inputSchema, separators=(",", ":")).encode())
    assert size <= 550
    assert tool.inputSchema["properties"]["action"]["enum"] == ["get", "set", "apply", "check", "status", "readiness"]
