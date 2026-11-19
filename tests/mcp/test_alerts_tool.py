"""Staff MCP alerts tool and the tools/list size budget."""

from datetime import datetime

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from app.models.alerting import Alert
from app.services.alerting.catalogue import Finding
from app.services.alerting.engine import run_cycle

TYPE = "storage_node_offline"


def seed_alert(db, rid="1", label="node-1"):
    run_cycle(
        db,
        now=datetime.utcnow(),
        only_types=[TYPE],
        detectors={TYPE: lambda d, p, n: [Finding(TYPE, "storage_node", rid, label, f"{label} offline", "gone")]},
    )
    return db.query(Alert).filter(Alert.resource_id == rid).one()



@pytest.mark.asyncio
async def test_mcp_alerts_tool(db_session, mcp_sessionlocal, mcp_auth_ctx_write, test_admin_user):
    from app.mcp.tools.alerts import alerts

    alert = seed_alert(db_session)
    seed_alert(db_session, "2", "node-2")
    listed = await alerts(action="list")
    assert listed["total"] == 2
    assert (await alerts(action="list", resource_id="2", resource_type="storage_node"))["total"] == 1
    assert (await alerts(action="get", alert_id=alert.id))["id"] == alert.id
    ack = await alerts(action="acknowledge", alert_id=alert.id, note="seen")
    assert ack["acknowledged"] is True
    assert (await alerts(action="unacknowledge", alert_id=alert.id))["acknowledged"] is False
    silence = await alerts(action="silence", alert_id=alert.id, duration_minutes=15, note="work")
    assert silence["active"] is True
    mute = await alerts(action="silence", resource_type="switch", resource_id="4", duration_minutes=60)
    assert len((await alerts(action="silences"))["items"]) == 2
    assert (await alerts(action="unsilence", silence_id=mute["id"]))["ok"] is True
    with pytest.raises(ToolError):
        await alerts(action="get")
    with pytest.raises(ToolError):
        await alerts(action="silence", alert_id=alert.id)
    with pytest.raises(ToolError):
        await alerts(action="get", alert_id=9999)


@pytest.mark.asyncio
async def test_mcp_alerts_write_needs_write_scope(db_session, mcp_sessionlocal, mcp_auth_ctx_read, test_admin_user):
    from app.mcp.tools.alerts import alerts

    alert = seed_alert(db_session)
    assert (await alerts(action="list"))["total"] == 1
    with pytest.raises(ToolError):
        await alerts(action="acknowledge", alert_id=alert.id)


def test_staff_tool_count_and_size_budget():
    import asyncio
    import json

    import app.mcp.server  # noqa: F401
    from app.mcp.instance import mcp

    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    assert "alerts" in names and len(tools) == 66
    size = len(json.dumps([t.model_dump(mode="json", exclude_none=True) for t in tools], separators=(",", ":")))
    assert size < 38_000
