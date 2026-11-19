"""Staff alerts: list, inspect, acknowledge and silence."""

from __future__ import annotations

from typing import Literal, Optional

from app.mcp.instance import mcp
from app.mcp.runtime import run_tool
from app.services.alerting import admin as alerts_admin

_READ = ("list", "get", "silences")


def _need(action: str, **values) -> None:
    missing = [name for name, value in values.items() if value in (None, "")]
    if missing:
        raise ValueError(f"{action} needs: {', '.join(missing)}")


@mcp.tool()
async def alerts(
    action: Literal["list", "get", "acknowledge", "unacknowledge", "silence", "unsilence", "silences"] = "list",
    alert_id: Optional[int] = None,
    state: Literal["open", "resolved", "all"] = "open",
    severity: Optional[Literal["info", "warning", "critical"]] = None,
    alert_type: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    limit: int = 50,
    note: Optional[str] = None,
    duration_minutes: Optional[int] = None,
    silence_id: Optional[int] = None,
) -> dict:
    """Staff alerts. list (state, severity, alert_type, resource_type/resource_id, limit), get (alert_id), acknowledge or
    unacknowledge (alert_id, note), silence (duration_minutes, note as reason; alert_id mutes that alert, or give
    alert_type and/or resource_type+resource_id, e.g. a switch in maintenance), silences (active), unsilence (silence_id).
    """
    args = {"action": action, "alert_id": alert_id, "silence_id": silence_id}
    if action in _READ:

        def read(db, ctx):
            if action == "list":
                return alerts_admin.list_alerts(
                    db,
                    state=state,
                    severity=severity,
                    alert_type=alert_type,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    limit=max(1, min(int(limit), 200)),
                )
            if action == "get":
                _need("get", alert_id=alert_id)
                return alerts_admin.alert_detail(db, alert_id)
            return {"items": alerts_admin.list_silences(db)}

        return await run_tool("alerts", "read", read, args=args)

    def write(db, ctx):
        user_id = ctx.created_by_user_id
        if action in ("acknowledge", "unacknowledge"):
            _need(action, alert_id=alert_id)
            if action == "acknowledge":
                alerts_admin.acknowledge(db, alert_id, user_id, note)
            else:
                alerts_admin.unacknowledge(db, alert_id)
            return alerts_admin.alert_detail(db, alert_id)
        if action == "silence":
            _need("silence", duration_minutes=duration_minutes)
            if alert_id is not None:
                row = alerts_admin.silence_alert(db, alert_id, user_id=user_id, duration_minutes=duration_minutes, reason=note)
            else:
                row = alerts_admin.create_silence(
                    db,
                    user_id=user_id,
                    alert_type=alert_type,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    duration_minutes=duration_minutes,
                    reason=note,
                )
            return alerts_admin.serialise_silence(row, alerts_admin.utcnow())
        _need("unsilence", silence_id=silence_id)
        alerts_admin.delete_silence(db, silence_id)
        return {"ok": True}

    return await run_tool("alerts", "write", write, args=args)
