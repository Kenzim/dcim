"""Async client for the VM-scoped Proxmox firewall endpoints (docs/vm-firewall.md section 5).

Writes exist only under ``/nodes/{node}/qemu/{vmid}/``; cluster and node firewall endpoints are
read through ``ClusterReader``, which can only GET (section 9.1). Calls go through a transport so
tests plug in ``tests.fakes.proxmox_firewall.FakeProxmoxFirewall``; production uses
``PluginTransport`` around an authenticated ``ProxmoxPlugin`` (its node relocation included).

Every write that Proxmox lets carry a ``digest`` passes the digest of the preceding read.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Optional, Protocol
from urllib.parse import quote, urlencode

from app.services.vm_firewall.planner import CurrentFirewall
from app.services.vm_firewall.render import IpsetEntry

NODE = "{node}"
DIGEST_CONFLICT_MARKERS = ("file changed by other user", "detected modified configuration", "digest")
FIREWALL_SERVICES = ("pve-firewall", "proxmox-firewall")


class ProxmoxApiError(Exception):
    def __init__(self, status: int, message: str, *, method: str = "", path: str = ""):
        super().__init__(f"{method} {path}: HTTP {status}: {message}".strip())
        self.status = status
        self.message = message
        self.method = method
        self.path = path


class DigestConflict(ProxmoxApiError):
    """Proxmox refused a write because the object changed since it was read."""


class VmMissing(ProxmoxApiError):
    """Proxmox has no such VM (after one relocation attempt)."""


def classify(status: int, message: str, *, method: str, path: str) -> ProxmoxApiError:
    lowered = message.lower()
    if any(marker in lowered for marker in DIGEST_CONFLICT_MARKERS) and status in (400, 500):
        return DigestConflict(status, message, method=method, path=path)
    if status == 404 or "does not exist" in lowered:
        return VmMissing(status, message, method=method, path=path)
    return ProxmoxApiError(status, message, method=method, path=path)


class FirewallTransport(Protocol):
    """``request`` returns Proxmox's ``data`` or raises ``ProxmoxApiError``.

    ``path`` is relative to ``/api2/json`` and may contain ``{node}``, which the transport fills
    with the VM's current node at call time (so a relocation between calls is followed).
    """

    async def request(self, method: str, path: str, params: Optional[dict[str, Any]] = None) -> Any: ...


class PluginTransport:
    """Transport over ``ProxmoxPlugin`` authentication and ``_*_with_relocate`` helpers."""

    def __init__(self, plugin):
        self.plugin = plugin

    def _url(self, path: str, query: Optional[dict[str, Any]] = None) -> str:
        url = f"{self.plugin.base_url}/api2/json{path.replace(NODE, str(self.plugin.node))}"
        return f"{url}?{urlencode(query)}" if query else url

    async def request(self, method: str, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        plugin = self.plugin
        if method == "GET":
            response = await plugin._get_with_relocate(lambda: self._url(path, params))
        elif method == "POST":
            response = await plugin._post_with_relocate(lambda: self._url(path), data=params)
        elif method == "PUT":
            response = await plugin._put_with_relocate(lambda: self._url(path), data=params)
        elif method == "DELETE":
            response = await plugin._delete_with_relocate(lambda: self._url(path), params=params)
        else:
            raise ValueError(f"unsupported method {method}")
        if response.status_code >= 400:
            message = response.reason_phrase or ""
            try:
                body = response.json()
                if isinstance(body, dict) and body.get("errors"):
                    message = f"{message}: {body['errors']}"
            except ValueError:
                message = f"{message}: {response.text[:300]}"
            raise classify(response.status_code, message, method=method, path=path)
        body = response.json()
        return body.get("data") if isinstance(body, dict) else body


def _bool(value: Any) -> int:
    return 1 if value in (1, True, "1") else 0


def parse_net(value: str) -> list[tuple[str, Optional[str]]]:
    parts: list[tuple[str, Optional[str]]] = []
    for item in (value or "").split(","):
        if not item:
            continue
        key, sep, val = item.partition("=")
        parts.append((key, val if sep else None))
    return parts


def format_net(parts: list[tuple[str, Optional[str]]]) -> str:
    return ",".join(key if val is None else f"{key}={val}" for key, val in parts)


def net_with_firewall(value: str, flag: bool) -> str:
    """``net0`` with only the ``firewall=`` key replaced (or appended)."""
    parts = parse_net(value)
    wanted = "1" if flag else "0"
    replaced = False
    out = []
    for key, val in parts:
        if key == "firewall":
            if not replaced:
                out.append((key, wanted))
                replaced = True
            continue
        out.append((key, val))
    if not replaced:
        out.append(("firewall", wanted))
    return format_net(out)


def net_firewall_flag(value: Optional[str]) -> bool:
    return any(key == "firewall" and val == "1" for key, val in parse_net(value or ""))


def net_bridges(config: dict[str, Any]) -> dict[str, dict[str, Optional[str]]]:
    """Every ``netN`` of a VM config as a key/value dict (the first item, ``model=MAC``, included)."""
    out = {}
    for key, value in config.items():
        if key.startswith("net") and key[3:].isdigit() and isinstance(value, str):
            out[key] = dict(parse_net(value))
    return out


@dataclass
class NicFlagResult:
    changed: bool
    pending: bool
    net0: str


class VmFirewallClient:
    """The firewall of one VM. ``vmid`` is fixed; the node comes from the transport."""

    def __init__(self, transport: FirewallTransport, vmid: int):
        self.transport = transport
        self.vmid = int(vmid)

    def vm_path(self, suffix: str) -> str:
        return f"/nodes/{NODE}/qemu/{self.vmid}/{suffix}"

    async def _read(self, suffix: str, params: Optional[dict[str, Any]] = None) -> Any:
        return await self.transport.request("GET", self.vm_path(suffix), params)

    async def _write(self, method: str, suffix: str, params: Optional[dict[str, Any]] = None) -> Any:
        if method not in ("POST", "PUT", "DELETE"):
            raise ValueError(f"{method} is not a write")
        clean = {key: value for key, value in (params or {}).items() if value is not None}
        return await self.transport.request(method, self.vm_path(suffix), clean)

    # Options
    async def get_options(self) -> dict[str, Any]:
        return dict(await self._read("firewall/options") or {})

    async def set_options(self, params: dict[str, Any], *, digest: Optional[str], delete: tuple[str, ...] = ()) -> None:
        body = dict(params)
        if delete:
            body["delete"] = ",".join(delete)
        body["digest"] = digest
        await self._write("PUT", "firewall/options", body)

    # Rules
    async def get_rules(self) -> tuple[list[dict[str, Any]], Optional[str]]:
        rows = list(await self._read("firewall/rules") or [])
        rows.sort(key=lambda row: int(row.get("pos", 0)))
        digest = rows[0].get("digest") if rows else None
        return [{k: v for k, v in row.items() if k not in ("pos", "digest", "ipversion", "errors")} for row in rows], digest

    async def create_rule(self, params: dict[str, Any]) -> None:
        """Inserts at position 0 (Proxmox has no digest on create)."""
        await self._write("POST", "firewall/rules", params)

    async def update_rule(
        self,
        pos: int,
        params: dict[str, Any],
        *,
        digest: Optional[str],
        delete: tuple[str, ...] = (),
        moveto: Optional[int] = None,
    ) -> None:
        body = dict(params)
        if delete:
            body["delete"] = ",".join(delete)
        if moveto is not None:
            body = {"moveto": moveto}
        body["digest"] = digest
        await self._write("PUT", f"firewall/rules/{int(pos)}", body)

    async def delete_rule(self, pos: int, *, digest: Optional[str]) -> None:
        await self._write("DELETE", f"firewall/rules/{int(pos)}", {"digest": digest})

    # IP sets
    async def list_ipsets(self) -> tuple[list[str], Optional[str]]:
        rows = list(await self._read("firewall/ipset") or [])
        digest = rows[0].get("digest") if rows else None
        return sorted(row["name"] for row in rows), digest

    async def get_ipset(self, name: str) -> tuple[list[IpsetEntry], Optional[str]]:
        rows = list(await self._read(f"firewall/ipset/{quote(name, safe='')}") or [])
        digest = rows[0].get("digest") if rows else None
        return [IpsetEntry(row["cidr"], bool(_bool(row.get("nomatch")))) for row in rows], digest

    async def create_ipset(self, name: str, *, digest: Optional[str]) -> None:
        await self._write("POST", "firewall/ipset", {"name": name, "digest": digest})

    async def delete_ipset(self, name: str) -> None:
        await self._write("DELETE", f"firewall/ipset/{quote(name, safe='')}", {"force": 1})

    async def add_ipset_entry(self, name: str, cidr: str, *, nomatch: bool = False) -> None:
        await self._write(
            "POST", f"firewall/ipset/{quote(name, safe='')}", {"cidr": cidr, "nomatch": 1 if nomatch else None}
        )

    async def update_ipset_entry(self, name: str, cidr: str, *, nomatch: bool, digest: Optional[str]) -> None:
        await self._write(
            "PUT",
            f"firewall/ipset/{quote(name, safe='')}/{quote(cidr, safe='')}",
            {"nomatch": 1 if nomatch else 0, "digest": digest},
        )

    async def remove_ipset_entry(self, name: str, cidr: str, *, digest: Optional[str]) -> None:
        await self._write(
            "DELETE", f"firewall/ipset/{quote(name, safe='')}/{quote(cidr, safe='')}", {"digest": digest}
        )

    # Whole document
    async def read(self) -> CurrentFirewall:
        options = await self.get_options()
        rules, rules_digest = await self.get_rules()
        names, index_digest = await self.list_ipsets()
        digests = {"options": options.pop("digest", None), "rules": rules_digest, "ipsets": index_digest}
        ipsets = {}
        for name in names:
            entries, digest = await self.get_ipset(name)
            ipsets[name] = entries
            digests[f"ipset:{name}"] = digest
        return CurrentFirewall(options=options, rules=rules, ipsets=ipsets, digests=digests)

    # VM config and the net0 flag
    async def get_config(self, *, current: bool = False) -> dict[str, Any]:
        return dict(await self._read("config", {"current": 1} if current else None) or {})

    async def get_pending(self) -> list[dict[str, Any]]:
        return list(await self._read("pending") or [])

    async def net0_pending(self) -> bool:
        return any(row.get("key") == "net0" and "pending" in row for row in await self.get_pending())

    async def set_net0_firewall_flag(self, flag: bool) -> NicFlagResult:
        """Write ``net0`` with ``firewall=`` set; everything else in ``net0`` is kept as it is.

        ``pending`` is true when Proxmox recorded the change for the next restart (hotplug
        without ``network``)."""
        config = await self.get_config()
        net0 = config.get("net0")
        if not net0:
            raise ProxmoxApiError(400, "VM has no net0", method="GET", path=self.vm_path("config"))
        if net_firewall_flag(net0) == flag:
            return NicFlagResult(changed=False, pending=await self.net0_pending(), net0=net0)
        new_net0 = net_with_firewall(net0, flag)
        await self._write("PUT", "config", {"net0": new_net0, "digest": config.get("digest")})
        return NicFlagResult(changed=True, pending=await self.net0_pending(), net0=new_net0)


class ClusterReader:
    """Read-only access to the cluster and node firewall state used by the readiness check."""

    def __init__(self, transport: FirewallTransport):
        self.transport = transport

    async def _get(self, path: str) -> Any:
        return await self.transport.request("GET", path, None)

    async def cluster_options(self) -> dict[str, Any]:
        return dict(await self._get("/cluster/firewall/options") or {})

    async def nodes(self) -> list[str]:
        return sorted(row["node"] for row in (await self._get("/nodes") or []) if row.get("node"))

    async def node_options(self, node: str) -> dict[str, Any]:
        return dict(await self._get(f"/nodes/{quote(node, safe='')}/firewall/options") or {})

    async def service_state(self, node: str, service: str) -> dict[str, Any]:
        if service not in FIREWALL_SERVICES:
            raise ValueError(f"{service} is not a firewall service")
        return dict(await self._get(f"/nodes/{quote(node, safe='')}/services/{service}/state") or {})


def fingerprint(current: CurrentFirewall) -> str:
    """One value over every digest of the VM's firewall: options, rules, IP set index and sets.

    Proxmox's options digest covers only the options, so on its own it cannot reveal a changed
    rule; this combines them so ``applied_digest`` changes with any of them."""
    text = "\n".join(f"{key}={current.digests.get(key) or ''}" for key in sorted(current.digests))
    return hashlib.sha256(text.encode()).hexdigest()


__all__ = [
    "ClusterReader",
    "DigestConflict",
    "FirewallTransport",
    "NicFlagResult",
    "PluginTransport",
    "ProxmoxApiError",
    "VmFirewallClient",
    "VmMissing",
    "fingerprint",
    "net_bridges",
    "net_firewall_flag",
    "net_with_firewall",
]
