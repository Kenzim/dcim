"""Server NICs and cable runs.

Cable ends are written as ``server:<port_id>``, ``switch:<port_id>``, or by device and
port name: ``server:<server_id>/<port name>``, ``switch:<switch_id>/<port name>``
(port names may themselves contain ``/``, e.g. ``switch:1/Ethernet1/23``).
Patch panel ends name a side: ``patch:<patch_port_id>/front`` or
``patch:<panel_id>/<port number or label>/rear``.
"""

from __future__ import annotations

from typing import Literal, Optional

from mcp.server.fastmcp.exceptions import ToolError

from app.api.server import convert_network_ports_to_response
from app.dao import CableRunDAO, NetworkPortDAO, ServerDAO, SwitchPortDAO
from app.mcp.instance import mcp
from app.mcp.runtime import run_tool
from app.models.network_port import NetworkPort
from app.models.patch_panel import PatchPanelPort
from app.models.switch_port import SwitchPort
from app.services.cable_trace_service import describe_path, describe_ref, trace_from_port

_PORT_FIELDS = (
    "mac_address",
    "speed_mbps",
    "model",
    "pci_address",
    "is_physical",
    "lag_group",
    "monitor_bandwidth",
    "pxe_boot",
    "pxe_ip",
    "description",
)


def _resolve_patch_end(db, ref: str, rest: str) -> tuple[str, int, object, str]:
    body, _, side = rest.rpartition("/")
    side = side.strip().lower()
    if side not in ("front", "rear") or not body:
        raise ValueError(f"Bad cable end {ref!r}: patch ends need a side, e.g. patch:<port_id>/front or patch:<panel_id>/<port>/rear")
    device, slash, port_name = body.partition("/")
    try:
        number = int(device)
    except ValueError:
        raise ValueError(f"Bad cable end {ref!r}: expected a numeric id after 'patch:'") from None
    if slash:
        q = db.query(PatchPanelPort).filter(PatchPanelPort.patch_panel_id == number)
        port = q.filter(PatchPanelPort.label == port_name).first()
        if port is None and port_name.isdigit():
            port = q.filter(PatchPanelPort.number == int(port_name)).first()
        if not port:
            raise ValueError(f"No port {port_name!r} on patch panel {number}")
    else:
        port = db.get(PatchPanelPort, number)
        if not port:
            raise ValueError(f"Patch panel port {number} not found")
    return "patch", port.id, port, side


def _resolve_end(db, ref: str) -> tuple[str, int, object, Optional[str]]:
    """Turn an end reference into (type, port_id, port row, side); side is set only for patch ports."""
    kind, sep, rest = (ref or "").strip().partition(":")
    kind = kind.strip().lower()
    if sep and kind == "patch" and rest:
        return _resolve_patch_end(db, ref, rest)
    if not sep or kind not in ("server", "switch") or not rest:
        raise ValueError(
            f"Bad cable end {ref!r}: use server:<port_id>, switch:<port_id>, <type>:<device_id>/<port name>, "
            "or patch:<port_id>/<front|rear>"
        )
    device, slash, port_name = rest.partition("/")
    try:
        number = int(device)
    except ValueError:
        raise ValueError(f"Bad cable end {ref!r}: expected a numeric id after '{kind}:'") from None
    model, owner = (NetworkPort, NetworkPort.server_id) if kind == "server" else (SwitchPort, SwitchPort.switch_id)
    if slash:
        port = db.query(model).filter(owner == number, model.name == port_name).first()
        if not port:
            raise ValueError(f"No port named {port_name!r} on {kind} {number}")
    else:
        port = (NetworkPortDAO if kind == "server" else SwitchPortDAO).get_by_id(db, number)
        if not port:
            raise ValueError(f"{kind.capitalize()} port {number} not found")
    return kind, port.id, port, None


def _end_label(db, ref) -> dict:
    info = describe_ref(db, ref) or {}
    label = {
        "type": ref[0],
        "port_id": ref[1],
        "port": info.get("port_name"),
        "device_id": info.get("device_id"),
        "device": info.get("device_name"),
    }
    if ref[0] == "patch":
        label["side"] = ref[2]
    return label


def _cable_row(db, run) -> dict:
    end_a, end_b = run.ends()
    return {
        "id": run.id,
        "a": _end_label(db, end_a),
        "b": _end_label(db, end_b),
        "cable_type": run.cable_type,
        "speed_mbps": run.speed_mbps,
        "description": run.description,
    }


async def list_server_ports(server_id: int) -> dict:
    def work(db, ctx):
        if not ServerDAO.get_by_id(db, server_id):
            raise ValueError("Server not found")
        ports = NetworkPortDAO.get_by_server(db, server_id)
        return {"server_id": server_id, "ports": convert_network_ports_to_response(db, server_id, ports)}

    return await run_tool("list_server_ports", "read", work, args={"server_id": server_id})


@mcp.tool()
async def save_server_ports(server_id: int, ports: list[dict]) -> dict:
    """Add or update a server's NICs. Each item: name (required), port_id (optional, to rename),
    speed_mbps (required for new), mac_address, model, pci_address, is_physical, lag_group,
    monitor_bandwidth, pxe_boot, pxe_ip, description. Matched by port_id, else by name.
    Ports not listed are left alone; cables are kept. At most one pxe_boot port per server.
    """

    def work(db, ctx):
        if not ServerDAO.get_by_id(db, server_id):
            raise ValueError("Server not found")
        if not ports:
            raise ValueError("ports is empty")
        existing = {p.name: p for p in NetworkPortDAO.get_by_server(db, server_id)}
        by_id = {p.id: p for p in existing.values()}
        created, updated = [], []
        for item in ports:
            unknown = set(item) - set(_PORT_FIELDS) - {"name", "port_id"}
            if unknown:
                raise ValueError(f"Unknown port fields: {', '.join(sorted(unknown))}")
            name = (item.get("name") or "").strip()
            if not name:
                raise ValueError("Every port needs a name")
            port_id = item.get("port_id")
            port = by_id.get(port_id) if port_id is not None else existing.get(name)
            if port_id is not None and port is None:
                raise ValueError(f"Port {port_id} is not on server {server_id}")
            fields = {k: item[k] for k in _PORT_FIELDS if k in item}
            if port is None:
                if fields.get("speed_mbps") is None:
                    raise ValueError(f"New port {name!r} needs speed_mbps")
                port = NetworkPortDAO.create(db, server_id=server_id, name=name, **fields)
                existing[name] = by_id[port.id] = port
                created.append(name)
            else:
                if name != port.name:
                    if name in existing:
                        raise ValueError(f"Server {server_id} already has a port named {name!r}")
                    existing.pop(port.name, None)
                    port.name = name
                    existing[name] = port
                for key, value in fields.items():
                    setattr(port, key, value)
                NetworkPortDAO.update(db, port)
                updated.append(name)
        pxe = [p.name for p in existing.values() if p.pxe_boot]
        if len(pxe) > 1:
            db.rollback()
            raise ValueError(f"Only one PXE boot port allowed per server, got: {', '.join(pxe)}")
        return {
            "server_id": server_id,
            "created": created,
            "updated": updated,
            "ports": convert_network_ports_to_response(db, server_id, NetworkPortDAO.get_by_server(db, server_id)),
        }

    return await run_tool(
        "save_server_ports",
        "write",
        work,
        args={"server_id": server_id, "names": [str(p.get("name")) for p in ports or []]},
    )


@mcp.tool()
async def delete_server_port(server_id: int, port_id: int, confirm: bool = False) -> dict:
    """Delete one server NIC from Rackflow; its cable run is deleted too."""

    def work(db, ctx):
        port = NetworkPortDAO.get_by_id(db, port_id)
        if not port or port.server_id != server_id:
            raise ValueError(f"Port {port_id} is not on server {server_id}")
        NetworkPortDAO.delete(db, port_id)
        return {"deleted": True, "server_id": server_id, "port_id": port_id}

    return await run_tool(
        "delete_server_port",
        "destructive",
        work,
        destructive=True,
        confirm=confirm,
        args={"server_id": server_id, "port_id": port_id, "confirm": confirm},
    )


@mcp.tool()
async def cable(
    action: Literal["list", "connect", "update", "disconnect", "trace"],
    server_id: Optional[int] = None,
    switch_id: Optional[int] = None,
    patch_panel_id: Optional[int] = None,
    cable_id: Optional[int] = None,
    a: Optional[str] = None,
    b: Optional[str] = None,
    runs: Optional[list[dict]] = None,
    cable_type: Optional[str] = None,
    speed_mbps: Optional[int] = None,
    description: Optional[str] = None,
) -> dict:
    """Cable runs. Ends: server:<port_id> | switch:<port_id> | server:<server_id>/<name> | switch:<switch_id>/<name>
    | patch:<patch_port_id>/<front|rear> | patch:<panel_id>/<port number or label>/<front|rear>.
    list: by server_id, switch_id, patch_panel_id or cable_id (else all). connect: a + b, or
    runs=[{a,b,cable_type,speed_mbps,description}] (all-or-nothing). update: cable_id, metadata only.
    disconnect: cable_id or a (one end). trace: a; follows the cable through patch panels to the far device.
    """
    if action == "trace":
        if not a:
            raise ToolError("trace needs a")

        def trace(db, ctx):
            end_type, end_id, _, side = _resolve_end(db, a)
            return describe_path(db, trace_from_port(db, end_type, end_id, side))

        return await run_tool("cable", "read", trace, args={"action": action, "a": a})

    if action == "list":
        def read(db, ctx):
            if cable_id is not None:
                run = CableRunDAO.get_by_id(db, cable_id)
                if not run:
                    raise ValueError("Cable run not found")
                found = [run]
            elif server_id is not None:
                found = CableRunDAO.get_by_server(db, server_id)
            elif switch_id is not None:
                found = CableRunDAO.get_by_switch(db, switch_id)
            elif patch_panel_id is not None:
                found = CableRunDAO.get_by_patch_panel(db, patch_panel_id)
            else:
                found = CableRunDAO.get_all(db, limit=1000)
            return {"cables": [_cable_row(db, r) for r in found]}

        return await run_tool("cable", "read", read, args={"action": action, "server_id": server_id, "switch_id": switch_id})

    if action == "connect":
        if runs is not None and (a or b):
            raise ToolError("Pass either a + b or runs, not both")
        items = runs if runs is not None else [
            {"a": a, "b": b, "cable_type": cable_type, "speed_mbps": speed_mbps, "description": description}
        ]
        if not items:
            raise ToolError("Nothing to connect")

        def connect(db, ctx):
            planned, used = [], set()
            for i, item in enumerate(items, 1):
                if not item.get("a") or not item.get("b"):
                    raise ValueError(f"Run {i}: needs a and b")
                try:
                    a_type, a_id, _, a_side = _resolve_end(db, item["a"])
                    b_type, b_id, _, b_side = _resolve_end(db, item["b"])
                except ValueError as exc:
                    raise ValueError(f"Run {i}: {exc}") from None
                end_a, end_b = (a_type, a_id, a_side), (b_type, b_id, b_side)
                if end_a == end_b:
                    raise ValueError(f"Run {i}: cannot connect a port to itself")
                for end, ref in ((end_a, item["a"]), (end_b, item["b"])):
                    if end in used:
                        raise ValueError(f"Run {i}: {ref} is used twice in this request")
                    used.add(end)
                    if CableRunDAO.get_by_port(db, *end):
                        raise ValueError(f"Run {i}: {ref} is already cabled")
                planned.append((end_a, end_b, item))
            made = []
            for end_a, end_b, item in planned:
                run = CableRunDAO.create(
                    db,
                    port_a_type=end_a[0],
                    port_a_id=end_a[1],
                    port_b_type=end_b[0],
                    port_b_id=end_b[1],
                    cable_type=item.get("cable_type"),
                    speed_mbps=item.get("speed_mbps"),
                    description=item.get("description"),
                    port_a_side=end_a[2],
                    port_b_side=end_b[2],
                )
                made.append(_cable_row(db, run))
            return {"created": made}

        return await run_tool(
            "cable", "write", connect, args={"action": action, "runs": [f"{i.get('a')} <-> {i.get('b')}" for i in items]}
        )

    if action == "update":
        if cable_id is None:
            raise ToolError("update needs cable_id")

        def update(db, ctx):
            run = CableRunDAO.get_by_id(db, cable_id)
            if not run:
                raise ValueError("Cable run not found")
            if cable_type is not None:
                run.cable_type = cable_type
            if speed_mbps is not None:
                run.speed_mbps = speed_mbps
            if description is not None:
                run.description = description
            return _cable_row(db, CableRunDAO.update(db, run))

        return await run_tool("cable", "write", update, args={"action": action, "cable_id": cable_id})

    if (cable_id is None) == (a is None):
        raise ToolError("disconnect needs exactly one of cable_id or a")

    def disconnect(db, ctx):
        if cable_id is not None:
            run = CableRunDAO.get_by_id(db, cable_id)
        else:
            end_type, end_id, _, side = _resolve_end(db, a)
            run = CableRunDAO.get_by_port(db, end_type, end_id, side)
        if not run:
            raise ValueError("Cable run not found")
        row = _cable_row(db, run)
        CableRunDAO.delete(db, run.id)
        return {"deleted": row}

    return await run_tool("cable", "write", disconnect, args={"action": action, "cable_id": cable_id, "a": a})
