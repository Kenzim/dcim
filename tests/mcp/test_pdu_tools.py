"""MCP: PDU, power feed and outlet tools, with scopes and outlet guards."""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from app.core.mcp_auth import McpAuthContext
from app.mcp.context import mcp_auth_ctx
from app.mcp.tools import combined
from app.models.location import Location
from app.models.rack import Rack
from app.models.server import Server
from app.plugins.pdu.base import PduCapability, PduPlugin
from app.plugins.pdu.registry import get_pdu_registry


class FakeMcpPdu(PduPlugin):
    """Switched 4-outlet PDU that records outlet actions instead of switching anything."""

    PLUGIN_NAME = "fake_mcp_pdu"
    PLUGIN_VERSION = "0"
    SUPPORTED_CAPABILITIES = [PduCapability.METERING, PduCapability.OUTLET_METERING, PduCapability.OUTLET_CONTROL]
    CONFIG_TEMPLATE = {
        "type": "object",
        "properties": {
            "hostname": {"type": "string"},
            "community": {"type": "string", "format": "password"},
        },
    }
    actions: list = []

    async def test_connection(self):
        return {"success": True, "message": "ok", "details": {}}

    async def get_pdu_info(self):
        return {"inlet_count": 1, "outlet_count": 4, "outlet_metering": True, "outlet_switching": True}

    async def get_readings(self):
        return {"inlets": [], "banks": [], "outlets": []}

    async def set_outlet_state(self, position, action):
        FakeMcpPdu.actions.append((position, action))
        return {"position": position, "action": action}


@pytest.fixture(autouse=True)
def fake_pdu_plugin(mcp_sessionlocal):
    get_pdu_registry().register(FakeMcpPdu)
    FakeMcpPdu.actions = []


@contextmanager
def scoped_key(*scopes: str):
    ctx = McpAuthContext(key_id=9, name="test-pdu", scopes=list(scopes), created_by_user_id=1, client_ip="127.0.0.1")
    token = mcp_auth_ctx.set(ctx)
    try:
        yield ctx
    finally:
        mcp_auth_ctx.reset(token)


@pytest.fixture
def site(db_session):
    loc = Location(name="PDU-DC", description="")
    db_session.add(loc)
    db_session.flush()
    rack = Rack(location_id=loc.id, name="Rack P", units=42)
    db_session.add(rack)
    db_session.commit()
    db_session.refresh(loc)
    db_session.refresh(rack)
    return loc, rack


async def _make_pdu(loc, rack, **overrides):
    fields = dict(
        name="pdu-a",
        location_id=loc.id,
        plugin_name=FakeMcpPdu.PLUGIN_NAME,
        plugin_config={"hostname": "10.0.0.5", "community": "s3cret-community"},
        rack_id=rack.id,
        inlet_count=1,
        outlet_count=4,
        outlet_switching=True,
    )
    fields.update(overrides)
    with scoped_key("write"):
        return await combined.save_pdu(**fields)


async def _outlet(pdu_id, position=1):
    with scoped_key("read"):
        detail = await combined.pdus(pdu_id=pdu_id)
    return next(o for o in detail["outlets"] if o["position"] == position)


async def _enable_control(pdu_id):
    with scoped_key("write"):
        await combined.save_pdu(pdu_id=pdu_id, outlet_control_enabled=True)


@pytest.mark.asyncio
async def test_read_key_cannot_save_pdu(site, mcp_sessionlocal):
    loc, rack = site
    with scoped_key("read"):
        with pytest.raises(ToolError):
            await combined.save_pdu(name="nope", location_id=loc.id, plugin_name=FakeMcpPdu.PLUGIN_NAME)


@pytest.mark.asyncio
async def test_create_pdu_generates_outlets_and_defaults_control_off(site, mcp_sessionlocal):
    loc, rack = site
    row = await _make_pdu(loc, rack)
    assert row["outlet_control_enabled"] is False
    detail = await _outlet_detail(row["id"])
    assert len(detail["outlets"]) == 4
    assert {o["position"] for o in detail["outlets"]} == {1, 2, 3, 4}


async def _outlet_detail(pdu_id):
    with scoped_key("read"):
        return await combined.pdus(pdu_id=pdu_id)


@pytest.mark.asyncio
async def test_pdu_read_masks_secrets(site, mcp_sessionlocal):
    loc, rack = site
    row = await _make_pdu(loc, rack)
    with scoped_key("read"):
        listing = await combined.pdus(location_id=loc.id)
        detail = await combined.pdus(pdu_id=row["id"])
    assert "s3cret-community" not in str(listing)
    assert "s3cret-community" not in str(detail)


@pytest.mark.asyncio
async def test_outlet_power_needs_destructive_scope_and_confirm(site, mcp_sessionlocal):
    loc, rack = site
    row = await _make_pdu(loc, rack)
    await _enable_control(row["id"])
    outlet = await _outlet(row["id"])
    with scoped_key("write"):
        with pytest.raises(ToolError):
            await combined.outlet_power("on", confirm=True, pdu_id=row["id"], outlet_id=outlet["id"])
    with scoped_key("destructive"):
        with pytest.raises(ToolError):
            await combined.outlet_power("on", confirm=False, pdu_id=row["id"], outlet_id=outlet["id"])
    assert FakeMcpPdu.actions == []


@pytest.mark.asyncio
async def test_outlet_power_switches_outlet_when_allowed(site, mcp_sessionlocal):
    loc, rack = site
    row = await _make_pdu(loc, rack)
    await _enable_control(row["id"])
    outlet = await _outlet(row["id"], position=2)
    with scoped_key("destructive"):
        result = await combined.outlet_power("cycle", confirm=True, pdu_id=row["id"], outlet_id=outlet["id"])
    assert FakeMcpPdu.actions == [(2, "cycle")]
    assert result is not None


@pytest.mark.asyncio
async def test_outlet_power_refused_when_control_disabled(site, mcp_sessionlocal):
    loc, rack = site
    row = await _make_pdu(loc, rack)
    outlet = await _outlet(row["id"])
    with scoped_key("destructive"):
        with pytest.raises(ToolError, match="Outlet control is not enabled on this PDU"):
            await combined.outlet_power("on", confirm=True, pdu_id=row["id"], outlet_id=outlet["id"])
    assert FakeMcpPdu.actions == []


@pytest.mark.asyncio
async def test_outlet_power_refused_when_outlet_locked(site, mcp_sessionlocal):
    loc, rack = site
    row = await _make_pdu(loc, rack)
    await _enable_control(row["id"])
    outlet = await _outlet(row["id"])
    with scoped_key("write"):
        await combined.pdu_action(row["id"], "update_outlet", outlet_id=outlet["id"], locked=True)
    with scoped_key("destructive"):
        with pytest.raises(ToolError, match="Outlet is locked"):
            await combined.outlet_power("off", confirm=True, pdu_id=row["id"], outlet_id=outlet["id"])
    assert FakeMcpPdu.actions == []


@pytest.mark.asyncio
async def test_outlet_power_by_server_uses_linked_outlet(site, db_session, mcp_sessionlocal):
    loc, rack = site
    server = Server(name="pdu-srv", server_ip="10.9.9.9", location_id=loc.id, plugin_name="ipmi", plugin_config={})
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)
    row = await _make_pdu(loc, rack)
    await _enable_control(row["id"])
    outlet = await _outlet(row["id"], position=3)
    with scoped_key("write"):
        summary = await combined.server_power_inputs(server.id, "add", name="PSU1")
        input_id = summary["inputs"][0]["id"]
        await combined.server_power_inputs(server.id, "link", input_id=input_id, pdu_outlet_id=outlet["id"])
    with scoped_key("destructive"):
        await combined.outlet_power("on", confirm=True, server_id=server.id, power_input_id=input_id)
    assert FakeMcpPdu.actions == [(3, "on")]


@pytest.mark.asyncio
async def test_write_key_cannot_delete_pdu_and_delete_needs_confirm(site, mcp_sessionlocal):
    loc, rack = site
    row = await _make_pdu(loc, rack)
    with scoped_key("write"):
        with pytest.raises(ToolError):
            await combined.pdu_action(row["id"], "delete", confirm=True)
    with scoped_key("destructive"):
        with pytest.raises(ToolError):
            await combined.pdu_action(row["id"], "delete", confirm=False)
    with scoped_key("read"):
        assert (await combined.pdus(pdu_id=row["id"]))["id"] == row["id"]


@pytest.mark.asyncio
async def test_power_feed_create_and_rack_summary(site, mcp_sessionlocal):
    loc, rack = site
    with scoped_key("read"):
        with pytest.raises(ToolError):
            await combined.power_feeds("create", rack_id=rack.id, name="A1")
    with scoped_key("write"):
        feed = await combined.power_feeds("create", rack_id=rack.id, name="A1", voltage=230, amperage=16)
    with scoped_key("read"):
        feeds = await combined.power_feeds("list", rack_id=rack.id)
        summary = await combined.rack_power_summary(rack.id)
    assert any(f["id"] == feed["id"] for f in feeds)
    assert "feeds" in summary


@pytest.mark.asyncio
async def test_write_key_cannot_remove_power_input(site, db_session, mcp_sessionlocal):
    loc, rack = site
    server = Server(name="pdu-srv-2", server_ip="10.9.9.10", location_id=loc.id, plugin_name="ipmi", plugin_config={})
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)
    with scoped_key("write"):
        summary = await combined.server_power_inputs(server.id, "add", name="PSU2")
        input_id = summary["inputs"][0]["id"]
        with pytest.raises(ToolError):
            await combined.server_power_inputs(server.id, "remove", input_id=input_id, confirm=True)


async def _poll_samples(db_session, pdu_id, minutes=90, power=1000.0):
    """Store `minutes` of history for a PDU as if it had been polled every minute until now."""
    from datetime import datetime, timedelta, timezone

    from app.dao.pdu_dao import PduDAO
    from app.services import power_service

    pdu = PduDAO.get_by_id(db_session, pdu_id)
    now = datetime.now(timezone.utc)
    for m in range(minutes, 0, -1):
        power_service.apply_readings(
            db_session,
            pdu,
            {"inlets": [{"position": 1, "voltage_v": 208.0, "power_w": power, "current_a": power / 208.0}],
             "banks": [{"name": "A", "current_a": 3.0, "rated_current_a": 16}]},
            now=now - timedelta(minutes=m),
        )


@pytest.mark.asyncio
async def test_pdus_history_is_bucketed_and_read_scoped(site, db_session, mcp_sessionlocal):
    loc, rack = site
    row = await _make_pdu(loc, rack)
    await _poll_samples(db_session, row["id"])

    with scoped_key("read"):
        out = await combined.pdus(pdu_id=row["id"], history_hours=24)
        assert out["scope"] == "pdu" and out["resolution"] == "raw"
        assert 1 <= len(out["metrics"]["power_w"]) <= 25  # buckets, not the 90 raw samples
        assert out["metrics"]["power_w"][0]["value"] == 1000.0
        assert out["energy"]["method"] == "integrated" and out["energy"]["kwh"] > 0
        assert out["banks"][0]["rated_current_a"] == 16.0

        week = await combined.pdus(pdu_id=row["id"], history_hours=168)
        assert week["resolution"] == "5m"

        by_rack = await combined.pdus(rack_id=rack.id, history_hours=24)
        assert by_rack["scope"] == "rack" and by_rack["metrics"]["power_w"][0]["value"] == 1000.0
        by_location = await combined.pdus(location_id=loc.id, history_hours=24)
        assert by_location["scope"] == "location"
        overall = await combined.pdus(history_hours=24)
        assert overall["scope"] == "all" and overall["pdu_count"] == 1

        with pytest.raises(ToolError):
            await combined.pdus(pdu_id=999, history_hours=24)
        with pytest.raises(ToolError):
            await combined.pdus(rack_id=rack.id, location_id=loc.id, history_hours=24)


@pytest.mark.asyncio
async def test_feed_rack_and_server_summaries_take_history_hours(site, db_session, mcp_sessionlocal):
    loc, rack = site
    row = await _make_pdu(loc, rack)
    with scoped_key("write"):
        feed = await combined.power_feeds("create", rack_id=rack.id, name="A1", voltage=208, amperage=30)
        detail = await combined.pdus(pdu_id=row["id"])
        await combined.pdu_action(row["id"], "link_inlet", inlet_id=detail["inlets"][0]["id"], power_feed_id=feed["id"])
    await _poll_samples(db_session, row["id"], minutes=30)
    server = Server(name="hist-srv", server_ip="10.9.9.11", location_id=loc.id, plugin_name="ipmi", plugin_config={})
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)

    with scoped_key("read"):
        feed_out = await combined.power_feeds("list", feed_id=feed["id"], history_hours=24)
        assert feed_out["history"]["metrics"]["power_w"][0]["value"] == 1000.0
        assert feed_out["history"]["limits"]["usable_current_a"] == 24.0
        with pytest.raises(ToolError):
            await combined.power_feeds("list", rack_id=rack.id, history_hours=24)
        rack_out = await combined.rack_power_summary(rack.id, history_hours=24)
        assert "feeds" in rack_out and rack_out["history"]["metrics"]["power_w"][0]["value"] == 1000.0
        server_out = await combined.server_power_summary(server.id, history_hours=24)
        assert server_out["history"]["available"] is False and server_out["history"]["reason"]
        assert "history" not in await combined.server_power_summary(server.id)
