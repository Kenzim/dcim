"""Carrying a VLAN on a server's switch uplink, including Cisco vPC pairs.

Planning turns a server's uplink ports into targets, each the smallest unit
that must change together:

- ``port``: one switch port.
- ``port-channel``: a port-channel on one switch, configured once however
  many member ports the server has in it.
- ``vpc``: a vPC port-channel and its twin on the vPC peer. Both peers get
  the VLAN, both peer-links carry it, and both port-channels allow it, as one
  operation that is rolled back if any step fails.

Only interfaces reached from the server's own cabling are touched. The vPC
peer-link, routed ports and port-channels with a member cabled to another
switch or another server are refused.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.dao.cable_run_dao import CableRunDAO
from app.dao.network_port_dao import NetworkPortDAO
from app.dao.network_switch_dao import NetworkSwitchDAO
from app.dao.switch_port_dao import SwitchPortDAO
from app.plugins.nxos import normalise_interface
from app.plugins.switch_registry import get_switch_registry

logger = logging.getLogger(__name__)


class UplinkError(Exception):
    def __init__(self, message: str, *, status_code: int = 409):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass
class Leg:
    switch: Any
    plugin: Any
    interface: str
    members: List[str]


@dataclass
class UplinkTarget:
    kind: str
    legs: List[Leg]
    vpc_id: Optional[int] = None
    warnings: List[str] = field(default_factory=list)

    def describe(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "vpc_id": self.vpc_id,
            "legs": [
                {
                    "switch_id": leg.switch.id,
                    "switch": leg.switch.name,
                    "interface": leg.interface,
                    "members": leg.members,
                }
                for leg in self.legs
            ],
            "warnings": list(self.warnings),
        }


def cabled_switch_port(db: Session, server_port) -> Optional[Any]:
    """The switch port at the other end of a server port's cable run, if any."""
    cable = CableRunDAO.get_by_server_port(db, server_port.id)
    if cable is None:
        return None
    return cable.end_a_switch_port or cable.end_b_switch_port


def _canonical(name: str) -> str:
    try:
        return normalise_interface(name)
    except ValueError:
        return str(name)


def _vlan_capable(switch) -> bool:
    return bool(switch.enabled) and get_switch_registry().supports_vlan_management(switch.plugin_name)


def _check_members(db: Session, switch, members: List[str], interface: str, server_id: int) -> None:
    """Refuse an interface whose member ports are cabled to another switch or another server."""
    ports = {_canonical(port.name): port for port in SwitchPortDAO.get_by_switch(db, switch.id)}
    for member in members:
        port = ports.get(_canonical(member))
        cable = CableRunDAO.get_by_switch_port(db, port.id) if port is not None else None
        if cable is None:
            continue
        for end in (cable.end_a_switch_port, cable.end_b_switch_port):
            if end is not None and end.id != port.id:
                raise UplinkError(
                    f"{switch.name} {interface} includes {member}, which is cabled to switch "
                    f"{end.switch.name if end.switch else end.switch_id}; inter-switch links are never changed"
                )
        server_port = cable.end_a_server_port or cable.end_b_server_port
        if server_port is not None and server_port.server_id != server_id:
            raise UplinkError(
                f"{switch.name} {interface} includes {member}, which is cabled to another server"
            )


class UplinkPlanner:
    """Builds the targets for one server. ``dry_run`` plugins record commands instead of sending them."""

    def __init__(self, db: Session, server_id: int, *, dry_run: bool = False, plugin_factory: Optional[Callable] = None):
        self.db = db
        self.server_id = server_id
        self.dry_run = dry_run
        self.plugin_factory = plugin_factory or (
            lambda switch: get_switch_registry().new_plugin(switch.plugin_name, switch.plugin_config)
        )
        self.plugins: Dict[int, Any] = {}

    def plugin_for(self, switch) -> Any:
        if switch.id not in self.plugins:
            plugin = self.plugin_factory(switch)
            if self.dry_run:
                if not getattr(plugin, "SUPPORTS_DRY_RUN", False):
                    raise UplinkError(f"Switch '{switch.name}' (plugin '{switch.plugin_name}') cannot show a dry run")
                plugin.dry_run = True
            self.plugins[switch.id] = plugin
        return self.plugins[switch.id]

    def _peer(self, switch, vpc_id: int, interface: str):
        where = f"{switch.name} {interface} is vPC {vpc_id}"
        if not switch.vpc_peer_switch_id:
            raise UplinkError(
                f"{where}, but no vPC peer is recorded for {switch.name}; run vPC detection on the switches page"
            )
        peer = NetworkSwitchDAO.get_by_id(self.db, switch.vpc_peer_switch_id)
        if peer is None:
            raise UplinkError(f"{where}, but its recorded vPC peer no longer exists in Rackflow")
        if peer.vpc_peer_switch_id != switch.id:
            raise UplinkError(f"{where}, but {peer.name} does not record {switch.name} as its vPC peer")
        if not _vlan_capable(peer):
            raise UplinkError(
                f"{where}, but its vPC peer {peer.name} cannot be configured automatically "
                f"(plugin '{peer.plugin_name}'{'' if peer.enabled else ', disabled'})"
            )
        if not getattr(self.plugin_for(peer), "SUPPORTS_VPC", False):
            raise UplinkError(f"{where}, but the plugin of its vPC peer {peer.name} does not handle vPC")
        return peer

    async def plan(self, uplink: List[Any]) -> List[UplinkTarget]:
        legs_in: List[Tuple[Any, Any, Any]] = []
        for server_port in uplink:
            switch_port = cabled_switch_port(self.db, server_port)
            if switch_port is None:
                raise UplinkError(f"Server port '{server_port.name}' has no cable run to a switch port")
            switch = switch_port.switch
            if not _vlan_capable(switch):
                raise UplinkError(f"Switch '{switch.name}' cannot be configured automatically")
            legs_in.append((switch, switch_port, server_port))

        # Switch-side ends of every cable from this server, to find the vPC peer's leg.
        cabled_on: Dict[int, List[str]] = {}
        for port in NetworkPortDAO.get_by_server(self.db, self.server_id):
            switch_port = cabled_switch_port(self.db, port)
            if switch_port is not None:
                cabled_on.setdefault(switch_port.switch_id, []).append(_canonical(switch_port.name))

        targets: Dict[Any, UplinkTarget] = {}
        keys: List[Tuple[Any, Any, Any]] = []
        for switch, switch_port, server_port in legs_in:
            plugin = self.plugin_for(switch)
            info = await self._describe(plugin, switch_port.name)
            interface = info["interface"]
            if info.get("peer_link"):
                raise UplinkError(f"{switch.name} {switch_port.name} is part of the vPC peer-link; it is never changed")
            if info.get("switchport") is False:
                raise UplinkError(f"{switch.name} {interface} is a routed port, not a switchport")
            _check_members(self.db, switch, info.get("members") or [], interface, self.server_id)
            vpc_id = info.get("vpc_id")
            if vpc_id is None:
                key = ("if", switch.id, interface)
                if key not in targets:
                    kind = "port-channel" if info.get("port_channel") else "port"
                    targets[key] = UplinkTarget(kind, [Leg(switch, plugin, interface, info.get("members") or [])])
                keys.append((key, switch, server_port))
                continue

            peer = self._peer(switch, vpc_id, interface)
            key = ("vpc", frozenset((switch.id, peer.id)), vpc_id)
            keys.append((key, switch, server_port))
            if key in targets:
                continue
            peer_plugin = self.plugin_for(peer)
            peer_interface = await peer_plugin.find_vpc_interface(vpc_id)
            if not peer_interface:
                raise UplinkError(
                    f"{switch.name} {interface} is vPC {vpc_id}, but vPC {vpc_id} is not configured on its peer {peer.name}"
                )
            peer_info = await self._describe(peer_plugin, peer_interface)
            if peer_info.get("peer_link") or peer_info.get("vpc_id") != vpc_id:
                raise UplinkError(f"vPC {vpc_id} on {peer.name} does not resolve to a vPC port-channel ({peer_interface})")
            peer_members = peer_info.get("members") or []
            _check_members(self.db, peer, peer_members, peer_interface, self.server_id)
            target = UplinkTarget(
                "vpc",
                [
                    Leg(switch, plugin, interface, info.get("members") or []),
                    Leg(peer, peer_plugin, peer_info["interface"], peer_members),
                ],
                vpc_id=vpc_id,
            )
            for leg, other in ((target.legs[0], target.legs[1]), (target.legs[1], target.legs[0])):
                cabled = set(cabled_on.get(leg.switch.id, []))
                if not cabled & {_canonical(m) for m in leg.members} and _canonical(leg.interface) not in cabled:
                    target.warnings.append(
                        f"No cable run is recorded from this server to {leg.switch.name} {leg.interface} "
                        f"(vPC {vpc_id}, peer of {other.switch.name} {other.interface}); it is configured anyway"
                    )
            targets[key] = target

        self._check_bonds(keys)
        return list(targets.values())

    @staticmethod
    async def _describe(plugin, port: str) -> Dict[str, Any]:
        describe = getattr(plugin, "describe_port", None)
        if describe is None:
            return {"interface": port, "port_channel": None, "vpc_id": None, "members": [port], "peer_link": False}
        return await describe(port)

    @staticmethod
    def _check_bonds(keys: List[Tuple[Any, Any, Any]]) -> None:
        """Bonded server ports on a vPC pair must land on one vPC.

        A bond whose ports reach both switches of a pair without a vPC
        (orphan ports), or reach different vPCs, is refused. Ports bonded
        across switches that are not vPC peers are configured one by one.
        """
        groups: Dict[str, List[Tuple[Any, Any, Any]]] = {}
        for key, switch, server_port in keys:
            if server_port.lag_group:
                groups.setdefault(server_port.lag_group, []).append((key, switch, server_port))
        for group, entries in groups.items():
            distinct = {entry[0] for entry in entries}
            if len(distinct) < 2:
                continue
            switch_ids = {entry[1].id for entry in entries}
            paired = any(entry[1].vpc_peer_switch_id in switch_ids for entry in entries)
            if any(key[0] == "vpc" for key in distinct) or paired:
                ports = ", ".join(sorted(f"{e[2].name} ({e[1].name})" for e in entries))
                raise UplinkError(
                    f"Bonded ports {ports} reach a vPC pair but not one vPC port-channel; "
                    "fix the switch port-channels (orphan or mismatched member ports)"
                )


async def _rollback(journal: List[Tuple[str, Callable[[], Awaitable[Any]]]]) -> List[str]:
    failures = []
    for description, undo in reversed(journal):
        try:
            await undo()
        except Exception as exc:
            logger.error("Uplink rollback step failed (%s): %s", description, exc)
            failures.append(f"{description}: {exc}")
    return failures


async def _run_steps(steps: List[Tuple[str, Callable, Callable]], *, dry_run: bool) -> None:
    """Run (description, do, undo) steps; on failure undo every step that changed something.

    ``undo`` is given the result of ``do``, so it can restore what that step replaced.
    """
    journal: List[Tuple[str, Callable[[], Awaitable[Any]]]] = []
    for description, do, undo in steps:
        try:
            result = await do()
        except Exception as exc:
            if dry_run:
                raise UplinkError(f"{description} would fail: {exc}") from exc
            failures = await _rollback(journal)
            undone = f"; rolled back {len(journal)} earlier change(s)" if journal else ""
            if failures:
                undone += f", but rollback failed for: {'; '.join(failures)}"
            raise UplinkError(f"{description} failed: {exc}{undone}") from exc
        if isinstance(result, dict) and result.get("changed"):
            journal.append((description, lambda undo=undo, result=result: undo(result)))


def _undo_add_port(plugin, interface: str, vlan: int) -> Callable[[Dict[str, Any]], Awaitable[Any]]:
    """Undo for add_port_vlan: an access port it turned into a trunk goes back to access."""

    def undo(result: Dict[str, Any]):
        if result.get("converted_to_trunk") and hasattr(plugin, "restore_access_port"):
            return plugin.restore_access_port(interface, result["access_vlan"], result.get("stp_port_type"))
        return plugin.remove_port_vlan(interface, vlan)

    return undo


async def _vpc_problems(targets: List[UplinkTarget], vlan: int, *, expect_up: bool) -> List[str]:
    problems = []
    for target in targets:
        if target.kind != "vpc":
            continue
        for leg in target.legs:
            try:
                problems += await leg.plugin.check_vpc_vlan(target.vpc_id, vlan, expect_up=expect_up)
            except Exception as exc:
                problems.append(f"Could not check vPC {target.vpc_id} on {leg.switch.name}: {exc}")
    return problems


async def apply_vlan(targets: List[UplinkTarget], vlan: int, name: str, *, dry_run: bool = False) -> List[str]:
    """Carry ``vlan`` on every target. Returns warnings (missing cables, vPC consistency)."""
    steps = []
    for target in targets:
        for leg in target.legs:
            steps.append((
                f"Create VLAN {vlan} on {leg.switch.name}",
                lambda p=leg.plugin: p.create_vlan(vlan, name),
                lambda _result, p=leg.plugin: p.delete_vlan(vlan, name),
            ))
        if target.kind == "vpc":
            for leg in target.legs:
                steps.append((
                    f"Allow VLAN {vlan} on the vPC peer-link of {leg.switch.name}",
                    lambda p=leg.plugin: p.add_peer_link_vlan(vlan),
                    lambda _result, p=leg.plugin: p.remove_peer_link_vlan(vlan),
                ))
        for leg in target.legs:
            steps.append((
                f"Add VLAN {vlan} to {leg.switch.name} {leg.interface}",
                lambda p=leg.plugin, i=leg.interface: p.add_port_vlan(i, vlan),
                _undo_add_port(leg.plugin, leg.interface, vlan),
            ))
    await _run_steps(steps, dry_run=dry_run)
    warnings = [w for target in targets for w in target.warnings]
    if not dry_run:
        warnings += await _vpc_problems(targets, vlan, expect_up=True)
    return warnings


async def remove_vlan(targets: List[UplinkTarget], vlan: int, *, dry_run: bool = False) -> List[str]:
    """Stop carrying ``vlan`` on every target, in the reverse order of ``apply_vlan``.

    The VLAN itself and the peer-link stay: other attachments may still use
    them. Both go when the network is deleted.
    """
    steps = []
    for target in reversed(targets):
        for leg in reversed(target.legs):
            steps.append((
                f"Remove VLAN {vlan} from {leg.switch.name} {leg.interface}",
                lambda p=leg.plugin, i=leg.interface: p.remove_port_vlan(i, vlan),
                lambda _result, p=leg.plugin, i=leg.interface: p.add_port_vlan(i, vlan),
            ))
    await _run_steps(steps, dry_run=dry_run)
    if dry_run:
        return []
    return await _vpc_problems(targets, vlan, expect_up=False)


def planned_commands(planner: UplinkPlanner) -> List[Dict[str, Any]]:
    """The configuration batches dry-run plugins recorded, per switch."""
    out = []
    for switch_id, plugin in planner.plugins.items():
        switch = NetworkSwitchDAO.get_by_id(planner.db, switch_id)
        out.append({
            "switch_id": switch_id,
            "switch": switch.name if switch else None,
            "batches": [list(batch) for batch in plugin.planned_batches],
        })
    return out
