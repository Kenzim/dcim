"""A stateful stand-in for NX-API on Cisco Nexus switches.

Each FakeNexus keeps VLANs, interfaces and a vPC domain, answers the ``show``
commands the nxos plugin uses in the same shapes NX-OS 9.3/10.x returns
(JSON-RPC ``cli`` bodies and ``cli_ascii`` running-config text), and applies
configuration commands to its state. FakeNXAPI routes requests by host, so
several switches (a vPC pair) can be driven in one test.
"""

import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Union
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse

ALL = frozenset(range(1, 4095))


def vlan_text(vlans) -> str:
    ordered = sorted(vlans)
    if not ordered:
        return "none"
    out, start, prev = [], ordered[0], ordered[0]
    for v in ordered[1:]:
        if v == prev + 1:
            prev = v
            continue
        out.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = v
    out.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(out)


def parse_vlans(text: str) -> Set[int]:
    out: Set[int] = set()
    for part in text.split(","):
        if part in ("", "none"):
            continue
        if part == "all":
            return set(ALL)
        low, _, high = part.partition("-")
        out.update(range(int(low), int(high or low) + 1))
    return out


@dataclass
class Iface:
    switchport: bool = True
    mode: str = "access"
    access_vlan: int = 1
    native_vlan: int = 1
    allowed: Set[int] = field(default_factory=lambda: set(ALL))
    channel_group: Optional[int] = None
    vpc: Optional[int] = None
    peer_link: bool = False
    edge: str = ""


class NXAPIRejected(Exception):
    pass


class FakeNexus:
    def __init__(self, hostname: str, *, vlans: Optional[Dict[int, Optional[str]]] = None):
        self.hostname = hostname
        self.vlans: Dict[int, Optional[str]] = {1: None, **(vlans or {})}
        self.ifaces: Dict[str, Iface] = {}
        self.vpc_domain: Optional[int] = None
        self.peer_link: Optional[str] = None
        self.vpc_consistency: Dict[int, str] = {}
        self.config_batches: List[List[str]] = []
        self.show_commands: List[str] = []
        self.saves = 0
        self.fail_on: Optional[Union[str, Callable[[str, Optional[str]], bool]]] = None
        self.status_code_override: Optional[int] = None

    # --- topology helpers ---

    def access(self, name: str, vlan: int = 100) -> Iface:
        self.ifaces[name] = Iface(access_vlan=vlan)
        return self.ifaces[name]

    def trunk(self, name: str, allowed="all", native: int = 1) -> Iface:
        allowed_set = set(ALL) if allowed == "all" else set(allowed)
        self.ifaces[name] = Iface(mode="trunk", native_vlan=native, allowed=allowed_set)
        return self.ifaces[name]

    def routed(self, name: str) -> Iface:
        self.ifaces[name] = Iface(switchport=False)
        return self.ifaces[name]

    def port_channel(self, number: int, members: List[str], *, vpc: Optional[int] = None, access_vlan=None,
                     allowed=(100,), native: int = 100) -> Iface:
        pc = f"port-channel{number}"
        if access_vlan is not None:
            self.ifaces[pc] = Iface(access_vlan=access_vlan, vpc=vpc)
        else:
            self.ifaces[pc] = Iface(mode="trunk", native_vlan=native, allowed=set(allowed), vpc=vpc)
        for member in members:
            self.ifaces[member] = Iface(channel_group=number)
        return self.ifaces[pc]

    def vpc_pair_side(self, *, domain: int = 1, peer_link: int = 45, members=("Ethernet1/45", "Ethernet1/46"),
                      peer_link_allowed="all"):
        self.vpc_domain = domain
        self.peer_link = f"port-channel{peer_link}"
        pl = self.port_channel(peer_link, list(members))
        pl.peer_link = True
        pl.native_vlan = 1
        pl.allowed = set(ALL) if peer_link_allowed == "all" else set(peer_link_allowed)
        self.routed("Ethernet1/48")
        return pl

    def iface(self, name: str) -> Iface:
        return self.ifaces[name]

    # --- show output ---

    def _running_interface(self, name: str) -> str:
        i = self.ifaces.setdefault(name, Iface())
        lines = [f"!Command: show running-config interface {name} all", "", f"interface {name}"]
        if not i.switchport:
            lines += ["  no switchport", "  ip address 192.168.1.1/30"]
        else:
            lines += ["  switchport", f"  switchport mode {i.mode}", f"  switchport access vlan {i.access_vlan}",
                      f"  switchport trunk native vlan {i.native_vlan}"]
            allowed = "1-4094" if i.allowed >= ALL else vlan_text(i.allowed)
            lines.append(f"  switchport trunk allowed vlan {allowed}")
            if i.edge:
                lines.append(f"  spanning-tree port type {i.edge}")
            if i.peer_link:
                lines += ["  spanning-tree port type network", "  vpc peer-link"]
            if i.vpc is not None:
                lines.append(f"  vpc {i.vpc}")
            if i.channel_group is not None:
                lines.append(f"  channel-group {i.channel_group} mode active")
        lines.append("  no shutdown")
        return "\n".join(lines) + "\n"

    def _running_vlan(self, vlan: int) -> str:
        lines = [f"!Command: show running-config vlan {vlan}", "", "version 10.3(2) Bios:version 05.47", ""]
        if vlan in self.vlans:
            lines.append(f"vlan {vlan}")
            if self.vlans[vlan]:
                lines += [f"vlan {vlan}", f"  name {self.vlans[vlan]}"]
        return "\n".join(lines) + "\n"

    def _up_vlans(self, pc: str) -> Set[int]:
        i = self.ifaces[pc]
        carried = i.allowed if i.mode == "trunk" else {i.access_vlan}
        return {v for v in carried if v in self.vlans}

    def _vpc_brief(self) -> dict:
        if self.vpc_domain is None:
            return {"vpc-domain-id": "not configured"}
        rows = []
        for name, i in sorted(self.ifaces.items()):
            if i.vpc is None:
                continue
            status = self.vpc_consistency.get(i.vpc, "SUCCESS")
            rows.append({
                "vpc-id": str(i.vpc),
                "vpc-ifindex": "Po" + name.removeprefix("port-channel"),
                "vpc-port-state": "1",
                "phy-port-if-removed": "disabled",
                "vpc-thru-peerlink": "0",
                "vpc-consistency": "consistent" if status == "SUCCESS" else "not consistent",
                "vpc-consistency-status": status,
                "up-vlan-bitset": vlan_text(self._up_vlans(name)),
                "es-attr": "DF: Invalid",
            })
        body = {
            "vpc-domain-id": str(self.vpc_domain),
            "vpc-peer-status": "peer-ok",
            "vpc-peer-status-reason": "SUCCESS",
            "vpc-peer-keepalive-status": "peer-alive",
            "vpc-peer-consistency": "consistent",
            "vpc-per-vlan-peer-consistency": "consistent",
            "vpc-peer-consistency-status": "SUCCESS",
            "num-of-vpcs": str(len(rows)),
            "TABLE_peerlink": {"ROW_peerlink": {
                "peer-link-id": "1",
                "peerlink-ifindex": "Po" + (self.peer_link or "").removeprefix("port-channel"),
                "peer-link-port-state": "1",
                "peer-up-vlan-bitset": vlan_text(self._up_vlans(self.peer_link)),
            }},
        }
        if rows:
            body["TABLE_vpc"] = {"ROW_vpc": rows[0] if len(rows) == 1 else rows}
        return body

    def _port_channel_summary(self, pc: str) -> dict:
        number = int(pc.removeprefix("port-channel"))
        members = [{"port": n, "port-status": "P"} for n, i in sorted(self.ifaces.items()) if i.channel_group == number]
        return {"TABLE_channel": {"ROW_channel": {
            "group": str(number), "port-channel": pc, "layer": "S", "status": "U", "type": "Eth", "prtcl": "LACP",
            "TABLE_member": {"ROW_member": members[0] if len(members) == 1 else members},
        }}}

    def _vlan_brief(self) -> dict:
        rows = [{"vlanshowbr-vlanid": str(v), "vlanshowbr-vlanname": n or f"VLAN{v:04d}", "vlanshowbr-vlanstate": "active"}
                for v, n in sorted(self.vlans.items())]
        return {"TABLE_vlanbriefxbrief": {"ROW_vlanbriefxbrief": rows}}

    def show(self, cmd: str, ascii_output: bool):
        self.show_commands.append(cmd)
        m = re.fullmatch(r"show running-config interface (\S+) all", cmd)
        if m:
            return self._running_interface(m.group(1))
        m = re.fullmatch(r"show running-config vlan (\d+)", cmd)
        if m:
            return self._running_vlan(int(m.group(1)))
        if cmd == "show hostname":
            return self.hostname + "\n"
        if cmd == "show vpc brief":
            return self._vpc_brief()
        m = re.fullmatch(r"show port-channel summary interface (port-channel\d+)", cmd)
        if m:
            return self._port_channel_summary(m.group(1))
        if cmd == "show vlan brief":
            return self._vlan_brief()
        raise NXAPIRejected("% Invalid command at '^' marker.")

    # --- configuration ---

    def _sync_members(self, pc_name: str) -> None:
        pc = self.ifaces[pc_name]
        number = int(pc_name.removeprefix("port-channel"))
        for i in self.ifaces.values():
            if i.channel_group == number:
                i.mode, i.access_vlan, i.native_vlan, i.allowed = pc.mode, pc.access_vlan, pc.native_vlan, set(pc.allowed)

    def configure(self, cmd: str, ctx: dict) -> None:
        if cmd in ("configure terminal", "end"):
            ctx.clear()
            return
        if cmd == "copy running-config startup-config":
            self.saves += 1
            return
        m = re.fullmatch(r"(no )?vlan (\d+)", cmd)
        if m:
            vlan = int(m.group(2))
            if vlan == 1 or vlan >= 3968:
                raise NXAPIRejected(f"% VLAN {vlan} is reserved")
            if m.group(1):
                self.vlans.pop(vlan, None)
                ctx.clear()
            else:
                self.vlans.setdefault(vlan, None)
                ctx.clear()
                ctx["vlan"] = vlan
            return
        if cmd.startswith("name ") and "vlan" in ctx:
            self.vlans[ctx["vlan"]] = cmd[5:]
            return
        m = re.fullmatch(r"interface (\S+)", cmd)
        if m:
            ctx.clear()
            ctx["iface"] = m.group(1)
            self.ifaces.setdefault(m.group(1), Iface())
            return
        name = ctx.get("iface")
        if name is None:
            raise NXAPIRejected("% Invalid command")
        i = self.ifaces[name]
        if i.channel_group is not None and cmd.startswith(("switchport", "spanning-tree")):
            raise NXAPIRejected(f"% Cannot configure port {name}: it is a member of port-channel{i.channel_group}")
        if not i.switchport and cmd.startswith("switchport"):
            raise NXAPIRejected("% Interface is in L3 mode")
        if m := re.fullmatch(r"switchport trunk native vlan (\d+)", cmd):
            i.native_vlan = int(m.group(1))
        elif m := re.fullmatch(r"switchport trunk allowed vlan add (\S+)", cmd):
            i.allowed |= parse_vlans(m.group(1))
        elif m := re.fullmatch(r"switchport trunk allowed vlan remove (\S+)", cmd):
            i.allowed -= parse_vlans(m.group(1))
        elif m := re.fullmatch(r"switchport trunk allowed vlan (\S+)", cmd):
            i.allowed = parse_vlans(m.group(1))
        elif m := re.fullmatch(r"switchport mode (access|trunk)", cmd):
            i.mode = m.group(1)
        elif m := re.fullmatch(r"switchport access vlan (\d+)", cmd):
            i.access_vlan = int(m.group(1))
        elif cmd == "no switchport trunk native vlan":
            i.native_vlan = 1
        elif cmd == "no switchport trunk allowed vlan":
            i.allowed = set(ALL)
        elif cmd == "no spanning-tree port type":
            i.edge = ""
        elif m := re.fullmatch(r"spanning-tree port type (.+)", cmd):
            i.edge = m.group(1)
        else:
            raise NXAPIRejected("% Invalid command")
        if name.startswith("port-channel"):
            self._sync_members(name)

    def _fails(self, cmd: str, iface: Optional[str]) -> bool:
        """``fail_on`` is a command, or a callable given the command and the interface being configured."""
        if self.fail_on is None:
            return False
        return self.fail_on(cmd, iface) if callable(self.fail_on) else cmd == self.fail_on

    def handle(self, payload: list):
        """Answer one JSON-RPC batch the way NX-API does: stop at the first failing command."""
        commands = [entry["params"]["cmd"] for entry in payload]
        if not commands[0].startswith("show "):
            self.config_batches.append(commands)
        replies, ctx, status = [], {}, 200
        for entry in payload:
            cmd = entry["params"]["cmd"]
            try:
                if self._fails(cmd, ctx.get("iface")):
                    raise NXAPIRejected("% Simulated failure")
                if cmd.startswith("show "):
                    out = self.show(cmd, entry["method"] == "cli_ascii")
                    result = {"msg": out} if entry["method"] == "cli_ascii" else {"body": out}
                else:
                    self.configure(cmd, ctx)
                    result = None
                replies.append({"jsonrpc": "2.0", "id": entry["id"], "result": result})
            except NXAPIRejected as exc:
                replies.append({"jsonrpc": "2.0", "id": entry["id"], "error": {
                    "code": -32602, "message": "Invalid params", "data": {"msg": f"{exc}\n"}}})
                status = 500
                break
        return self.status_code_override or status, (replies[0] if len(replies) == 1 else replies)


class FakeNXAPI:
    """Routes NX-API requests to FakeNexus switches by host; patch it in with ``active()``."""

    def __init__(self, *switches: FakeNexus):
        self.switches: Dict[str, FakeNexus] = {s.hostname: s for s in switches}
        self.requests: List[dict] = []

    def add(self, switch: FakeNexus) -> FakeNexus:
        self.switches[switch.hostname] = switch
        return switch

    async def _post(self, url, json=None, headers=None, auth=None):
        host = urlparse(url).hostname
        self.requests.append({"url": url, "auth": auth, "payload": json})
        response = MagicMock()
        switch = self.switches.get(host)
        if switch is None:
            import httpx

            raise httpx.ConnectError(f"no route to {host}")
        response.status_code, body = switch.handle(json)
        response.json.return_value = body
        return response

    def _client(self, *args, **kwargs):
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(side_effect=self._post)
        return client

    @contextmanager
    def active(self):
        with patch("app.plugins.nxos.httpx.AsyncClient", side_effect=self._client):
            yield self
