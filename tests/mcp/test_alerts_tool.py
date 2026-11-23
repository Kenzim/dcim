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
    assert "alerts" in names and "plugins" in names and len(tools) == 67
    size = len(json.dumps([t.model_dump(mode="json", exclude_none=True) for t in tools], separators=(",", ":")))
    assert size < 38_000


@pytest.mark.asyncio
async def test_mcp_silences_write_the_admin_api_audit_rows(db_session, mcp_sessionlocal, mcp_auth_ctx_write, test_admin_user):
    from app.mcp.tools.alerts import alerts
    from app.models.alerting import AlertSilence
    from app.models.commerce_audit import UserAuditEvent

    alert = seed_alert(db_session)
    by_alert = await alerts(action="silence", alert_id=alert.id, duration_minutes=15, note="work")
    mute = await alerts(action="silence", resource_type="switch", resource_id="4", duration_minutes=60, note="maint")
    await alerts(action="unsilence", silence_id=mute["id"])
    db_session.expire_all()
    rows = db_session.query(UserAuditEvent).filter(UserAuditEvent.action.like("alert.silence_%")).order_by(
        UserAuditEvent.id
    ).all()
    assert [(row.action, row.resource_id) for row in rows] == [
        ("alert.silence_created", str(by_alert["id"])),
        ("alert.silence_created", str(mute["id"])),
        ("alert.silence_deleted", str(mute["id"])),
    ]
    assert rows[0].event_metadata["alert_id"] == alert.id and rows[0].event_metadata["reason"] == "work"
    assert rows[2].event_metadata["resource_type"] == "switch" and rows[2].event_metadata["reason"] == "maint"
    assert {row.actor_user_id for row in rows} == {mcp_auth_ctx_write.created_by_user_id}
    assert db_session.get(AlertSilence, mute["id"]) is None


@pytest.mark.asyncio
async def test_an_mcp_silence_whose_audit_fails_is_not_kept(
    db_session, mcp_sessionlocal, mcp_auth_ctx_write, test_admin_user, monkeypatch
):
    import app.api.alerts as alerts_api
    from app.mcp.tools.alerts import alerts
    from app.models.alerting import AlertSilence

    def failing(*args, **kwargs):
        raise RuntimeError("audit down")

    monkeypatch.setattr(alerts_api, "_audit", failing)
    with pytest.raises(ToolError):
        await alerts(action="silence", resource_type="switch", resource_id="5", duration_minutes=60)
    db_session.expire_all()
    assert db_session.query(AlertSilence).count() == 0
