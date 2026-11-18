"""Proxy runners: the standalone nodes that serve customer HTTP-proxy IPs.

Every enabled runner is served every published assignment (all active http_proxy services on
enabled subnets); placing the IPs on nodes is left to the operator, so floating IPs and anycast
work without Rackflow knowing which node holds an address.
"""

from __future__ import annotations

from collections import Counter
from typing import Literal, Optional

from app.api import proxy_runners as runner_api
from app.api.runner_proxy import build_published_assignments
from app.core.config import settings
from app.dao.proxy_runner_dao import ProxyRunnerDAO
from app.mcp.instance import mcp
from app.models.ipam import IPAddress
from app.mcp.runtime import run_tool
from app.services.proxy_fast_path import ProxyAccessService


def _runner_row(row) -> dict:
    if not isinstance(row, runner_api.ProxyRunnerResponse):
        row = runner_api._to_response(row)
    return row.model_dump(mode="json", exclude={"api_key"})


def _held_back_reason(assignment) -> str:
    ip_row = assignment.ip
    if not ip_row or not ip_row.subnet:
        return "ip_missing"
    if not ip_row.subnet.enabled:
        return "subnet_disabled"
    service = assignment.service
    if service is None:
        return "service_missing"
    status = getattr(service.status, "value", service.status)
    return f"service_{status}"


def _published_view(db, service_id: Optional[int], limit: int) -> dict:
    rows, version, held_back = build_published_assignments(db)
    runners = ProxyRunnerDAO.get_all(db)

    ip_to_subnet: dict[str, Optional[str]] = {}
    ips = {r["bind_ip"] for r in rows}
    if ips:
        for ip_row in db.query(IPAddress).filter(IPAddress.ip_address.in_(ips)).all():
            ip_to_subnet[ip_row.ip_address] = ip_row.subnet.name if ip_row.subnet else None
    by_subnet = Counter(ip_to_subnet.get(r["bind_ip"]) or "?" for r in rows)

    entries = [
        {"service_id": r["service_id"], "bind_ip": r["bind_ip"], "subnet": ip_to_subnet.get(r["bind_ip"])}
        for r in rows
        if service_id is None or r["service_id"] == service_id
    ]
    held = [
        {
            "service_id": a.service_id,
            "ip": a.ip.ip_address if a.ip else None,
            "reason": _held_back_reason(a),
        }
        for a in held_back
        if service_id is None or a.service_id == service_id
    ]
    cap = max(1, min(int(limit), 1000))
    return {
        "version": version,
        "published": len(rows),
        "published_by_subnet": dict(by_subnet),
        "held_back": len(held_back),
        "held_back_by_reason": dict(Counter(h["reason"] for h in held)),
        "runners": {
            "enabled": sum(1 for r in runners if r.enabled),
            "online": sum(1 for r in runners if r.enabled and ProxyRunnerDAO.is_online(r)),
        },
        "entries": entries[:cap],
        "held_back_entries": held[:cap],
        "truncated": len(entries) > cap or len(held) > cap,
    }


def _fast_path_view(db, runner_id: Optional[int]) -> dict:
    """What each placed runner serves on the fast path: tenants and addresses, never credentials."""
    runners = ProxyRunnerDAO.get_all(db)
    if runner_id is not None:
        runners = [r for r in runners if r.id == runner_id]
        if not runners:
            raise ValueError("Proxy runner not found")
    out = []
    for runner in runners:
        config = ProxyAccessService.runner_config(db, runner)
        row = {
            "runner_id": runner.id,
            "name": runner.name,
            "location_id": runner.location_id,
            "vlan_mode": getattr(runner.vlan_mode, "value", runner.vlan_mode),
            "active": config is not None,
        }
        if config is not None:
            row["port"] = config["port"]
            row["tenants"] = [
                {
                    "network_id": t["network_id"],
                    "vlan": t["vlan"],
                    "addresses": t["addresses"],
                    "assignments": len(t["assignments"]),
                }
                for t in config["tenants"]
            ]
        out.append(row)
    return {"cidr": settings.proxy_access_cidr, "runners": out}


@mcp.tool()
async def proxy_runner(
    action: Literal["list", "create", "update", "published", "fast_path"] = "list",
    runner_id: Optional[int] = None,
    name: Optional[str] = None,
    enabled: Optional[bool] = None,
    service_id: Optional[int] = None,
    limit: int = 200,
    location_id: Optional[int] = None,
    server_id: Optional[int] = None,
    vlan_mode: Optional[Literal["preconfigured", "switch"]] = None,
    clear_placement: bool = False,
) -> dict:
    """Proxy runner nodes. list (or one by runner_id), create (needs name; API key returned once),
    update (name, enabled; fast path placement: location_id, server_id, vlan_mode, or clear_placement),
    published: what every runner is served (IPs per service, no credentials) and what is held back and why;
    filter with service_id. fast_path: per runner, the client tenants and private IPs it serves.
    """
    if action in ("list", "published", "fast_path"):
        async def read(db, ctx):
            if action == "published":
                return _published_view(db, service_id, limit)
            if action == "fast_path":
                return _fast_path_view(db, runner_id)
            if runner_id is not None:
                return _runner_row(await runner_api.get_proxy_runner(runner_id, auth={}, db=db))
            rows = ProxyRunnerDAO.get_all(db)
            return {"runners": [_runner_row(r) for r in rows]}

        return await run_tool("proxy_runner", "read", read, args={"action": action, "runner_id": runner_id})

    async def write(db, ctx):
        placement = {
            key: value
            for key, value in (("location_id", location_id), ("server_id", server_id), ("vlan_mode", vlan_mode))
            if value is not None
        }
        if action == "create":
            if runner_id is not None or enabled is not None or placement or clear_placement:
                raise ValueError("Creating a runner takes only: name")
            if not name:
                raise ValueError("Creating a runner needs: name")
            created = await runner_api.create_proxy_runner(runner_api.ProxyRunnerCreate(name=name), auth={}, db=db)
            out = created.model_dump(mode="json")
            out["note"] = "api_key is shown once; set it as the runner's API key."
            return out
        if runner_id is None:
            raise ValueError("Updating a runner needs: runner_id")
        if clear_placement and placement:
            raise ValueError("Pass clear_placement or placement fields, not both")
        if name is None and enabled is None and not placement and not clear_placement:
            raise ValueError(
                "Updating a runner needs at least one of: name, enabled, location_id, server_id, vlan_mode, "
                "clear_placement"
            )
        if clear_placement:
            placement = {"location_id": None, "server_id": None, "vlan_mode": "preconfigured"}
        fields = {key: value for key, value in (("name", name), ("enabled", enabled)) if value is not None}
        updated = await runner_api.update_proxy_runner(
            runner_id, runner_api.ProxyRunnerUpdate(**fields, **placement), auth={}, db=db
        )
        return updated.model_dump(mode="json", exclude={"api_key"})

    return await run_tool(
        "proxy_runner",
        "write",
        write,
        args={
            "action": action,
            "runner_id": runner_id,
            "name": name,
            "enabled": enabled,
            "location_id": location_id,
            "server_id": server_id,
            "vlan_mode": vlan_mode,
            "clear_placement": clear_placement,
        },
    )


@mcp.tool()
async def rotate_proxy_runner_key(runner_id: int, confirm: bool = False) -> dict:
    """Issue a new API key for a proxy runner (returned once). The old key stops working at once."""

    async def work(db, ctx):
        rotated = await runner_api.rotate_proxy_runner_key(runner_id, auth={}, db=db)
        return {"id": rotated.id, "api_key": rotated.api_key, "note": "Old key revoked; update the runner."}

    return await run_tool(
        "rotate_proxy_runner_key",
        "destructive",
        work,
        destructive=True,
        confirm=confirm,
        args={"runner_id": runner_id, "confirm": confirm},
    )


@mcp.tool()
async def delete_proxy_runner(runner_id: int, confirm: bool = False) -> dict:
    """Delete a proxy runner; its key stops working and it stops receiving the proxy config."""

    async def work(db, ctx):
        await runner_api.delete_proxy_runner(runner_id, auth={}, db=db)
        return {"deleted": True, "runner_id": runner_id}

    return await run_tool(
        "delete_proxy_runner",
        "destructive",
        work,
        destructive=True,
        confirm=confirm,
        args={"runner_id": runner_id, "confirm": confirm},
    )
