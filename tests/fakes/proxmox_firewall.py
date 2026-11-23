"""In-memory Proxmox firewall for tests: a ``FirewallTransport`` for ``app.services.vm_firewall``.

Models one cluster: per-VM ``.fw`` state (options, positional rules, IP sets), the VM config with
``net0`` and pending changes (hotplug without ``network``), datacenter and node firewall options,
and firewall service states. It follows Proxmox's API semantics:

- each object (options, rule list, IP set index, each IP set) has its own digest; it changes on
  every write to that object, and a write carrying a stale digest is refused;
- ``POST .../rules`` inserts at position 0; a rule without ``enable`` is disabled;
- ``PUT .../rules/{pos}`` with ``moveto`` moves the rule before the rule at ``moveto``.

Fault injection: ``fail_next`` (HTTP errors such as 5xx), ``on_next_write`` (another writer edits
the VM just before a write lands, a digest race), ``crash_after`` (the process dies after N writes).

Usage::

    fake = FakeProxmoxFirewall(nodes=["pve1", "pve2"])
    vm = fake.add_vm(100, node="pve1")
    client = VmFirewallClient(fake, 100)      # or fake.client(100)
    reader = ClusterReader(fake)              # or fake.reader()
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import unquote

from app.services.vm_firewall.proxmox_fw import ClusterReader, VmFirewallClient, classify

STALE_DIGEST = "detected modified configuration - file changed by other user? Try again."
RULE_KEYS = ("type", "action", "proto", "dport", "sport", "source", "dest", "icmp-type", "enable", "log",
             "comment", "macro", "iface")
OPTION_KEYS = ("enable", "policy_in", "policy_out", "ipfilter", "macfilter", "dhcp", "ndp", "radv",
               "log_level_in", "log_level_out")


class FakeCrash(Exception):
    """The process applying the firewall died (raised instead of performing a write)."""


@dataclass
class FakeVm:
    vmid: int
    node: str
    options: dict[str, Any] = field(default_factory=dict)
    rules: list[dict[str, Any]] = field(default_factory=list)
    ipsets: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    pending: dict[str, Any] = field(default_factory=dict)
    hotplug_network: bool = True
    running: bool = True
    generations: dict[str, int] = field(default_factory=dict)

    def digest(self, obj: str) -> str:
        if obj == "options":
            content: Any = self.options
        elif obj == "rules":
            content = self.rules
        elif obj == "ipsets":
            content = sorted(self.ipsets)
        elif obj == "config":
            content = self.config
        else:
            content = self.ipsets.get(obj[len("ipset:"):], [])
        text = json.dumps([content, self.generations.get(obj, 0)], sort_keys=True, default=str)
        return hashlib.sha1(text.encode()).hexdigest()

    def touch(self, obj: str) -> None:
        self.generations[obj] = self.generations.get(obj, 0) + 1

    @property
    def net0(self) -> str:
        return self.config.get("net0", "")

    def reboot(self) -> None:
        """Apply pending config changes, as a stop and start does."""
        self.config.update(self.pending)
        self.pending.clear()
        self.touch("config")

    # Direct edits that bypass the API (another admin in the Proxmox UI).
    def add_foreign_rule(self, rule: dict[str, Any], pos: int = 0) -> None:
        self.rules.insert(pos, dict(rule))
        self.touch("rules")

    def set_option(self, key: str, value: Any) -> None:
        self.options[key] = value
        self.touch("options")


class FakeProxmoxFirewall:
    """Implements ``request(method, path, params)`` like ``PluginTransport``."""

    def __init__(
        self,
        *,
        nodes: Optional[list[str]] = None,
        cluster_options: Optional[dict[str, Any]] = None,
        node_options: Optional[dict[str, dict[str, Any]]] = None,
        services: Optional[dict[str, dict[str, str]]] = None,
    ):
        self.nodes = list(nodes or ["pve1"])
        self.cluster_options = dict(cluster_options if cluster_options is not None else {"enable": 1, "policy_in": "DROP"})
        self.node_options = {n: dict((node_options or {}).get(n, {"enable": 1})) for n in self.nodes}
        self.services = {
            n: dict((services or {}).get(n, {"pve-firewall": "running", "proxmox-firewall": "running"}))
            for n in self.nodes
        }
        self.vms: dict[int, FakeVm] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.writes: list[tuple[str, str, dict[str, Any]]] = []
        self._failures: list[dict[str, Any]] = []
        self._before_write: list[Callable[["FakeProxmoxFirewall"], None]] = []
        self._crash_after: Optional[int] = None

    # Set-up
    def add_vm(
        self,
        vmid: int,
        *,
        node: Optional[str] = None,
        net0: str = "virtio=BC:24:11:00:00:01,bridge=vmbr0",
        hotplug_network: bool = True,
        running: bool = True,
        config: Optional[dict[str, Any]] = None,
    ) -> FakeVm:
        vm = FakeVm(
            vmid=int(vmid),
            node=node or self.nodes[0],
            config={"net0": net0, **(config or {})},
            hotplug_network=hotplug_network,
            running=running,
        )
        self.vms[vm.vmid] = vm
        return vm

    def client(self, vmid: int) -> VmFirewallClient:
        return VmFirewallClient(self, vmid)

    def reader(self) -> ClusterReader:
        return ClusterReader(self)

    # Fault injection
    def fail_next(
        self,
        status: int = 500,
        message: str = "Internal Server Error",
        *,
        count: int = 1,
        method: Optional[str] = None,
        path_contains: Optional[str] = None,
    ) -> None:
        self._failures.append(
            {"status": status, "message": message, "count": count, "method": method, "path": path_contains}
        )

    def on_next_write(self, callback: Callable[["FakeProxmoxFirewall"], None]) -> None:
        """Run ``callback`` just before the next write is processed."""
        self._before_write.append(callback)

    def crash_after(self, writes: Optional[int]) -> None:
        """Let ``writes`` more writes succeed, then raise ``FakeCrash`` on every write."""
        self._crash_after = None if writes is None else len(self.writes) + writes

    # Transport
    async def request(self, method: str, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        params = dict(params or {})
        self.calls.append((method, path, params))
        for failure in list(self._failures):
            if failure["method"] not in (None, method):
                continue
            if failure["path"] is not None and failure["path"] not in path:
                continue
            failure["count"] -= 1
            if failure["count"] <= 0:
                self._failures.remove(failure)
            raise classify(failure["status"], failure["message"], method=method, path=path)
        if method != "GET":
            if self._crash_after is not None and len(self.writes) >= self._crash_after:
                raise FakeCrash(f"crashed before {method} {path}")
            callbacks, self._before_write = self._before_write, []
            for callback in callbacks:
                callback(self)
            self.writes.append((method, path, params))
        return self._dispatch(method, path, params)

    def _error(self, status: int, message: str, method: str, path: str):
        return classify(status, message, method=method, path=path)

    def _dispatch(self, method: str, path: str, params: dict[str, Any]) -> Any:
        if path == "/cluster/firewall/options" and method == "GET":
            return dict(self.cluster_options)
        if path == "/nodes" and method == "GET":
            return [{"node": n, "status": "online"} for n in self.nodes]
        match = re.fullmatch(r"/nodes/([^/]+)/firewall/options", path)
        if match and method == "GET":
            return dict(self.node_options.get(unquote(match.group(1)), {}))
        match = re.fullmatch(r"/nodes/([^/]+)/services/([^/]+)/state", path)
        if match and method == "GET":
            node, service = unquote(match.group(1)), match.group(2)
            state = self.services.get(node, {}).get(service)
            if state is None:
                raise self._error(500, f"service '{service}' does not exist", method, path)
            return {"service": service, "state": state}
        match = re.fullmatch(r"/nodes/\{node\}/qemu/(\d+)/(.+)", path)
        if not match:
            raise AssertionError(f"fake Proxmox has no endpoint {method} {path}")
        vmid = int(match.group(1))
        vm = self.vms.get(vmid)
        if vm is None:
            raise self._error(500, f"Configuration file 'nodes/pve1/qemu-server/{vmid}.conf' does not exist", method, path)
        return self._vm(vm, method, match.group(2), params, path)

    def _check(self, vm: FakeVm, obj: str, params: dict[str, Any], method: str, path: str) -> None:
        digest = params.get("digest")
        if digest is not None and digest != vm.digest(obj):
            raise self._error(500, STALE_DIGEST, method, path)

    def _vm(self, vm: FakeVm, method: str, sub: str, params: dict[str, Any], path: str) -> Any:
        if sub == "firewall/options":
            if method == "GET":
                return {**copy.deepcopy(vm.options), "digest": vm.digest("options")}
            if method == "PUT":
                self._check(vm, "options", params, method, path)
                for key in filter(None, str(params.get("delete", "")).split(",")):
                    vm.options.pop(key, None)
                for key, value in params.items():
                    if key in ("delete", "digest"):
                        continue
                    if key not in OPTION_KEYS:
                        raise self._error(400, f"property '{key}' is not allowed", method, path)
                    vm.options[key] = value
                vm.touch("options")
                return None
        if sub == "firewall/rules":
            if method == "GET":
                digest = vm.digest("rules")
                return [{**copy.deepcopy(rule), "pos": pos, "digest": digest} for pos, rule in enumerate(vm.rules)]
            if method == "POST":
                rule = {key: value for key, value in params.items() if key in RULE_KEYS}
                if "type" not in rule or "action" not in rule:
                    raise self._error(400, "type and action are required", method, path)
                rule.setdefault("enable", 0)
                vm.rules.insert(0, rule)
                vm.touch("rules")
                return None
        match = re.fullmatch(r"firewall/rules/(\d+)", sub)
        if match:
            pos = int(match.group(1))
            if pos >= len(vm.rules):
                raise self._error(400, "no rule at specified position", method, path)
            if method == "GET":
                return {**copy.deepcopy(vm.rules[pos]), "pos": pos, "digest": vm.digest("rules")}
            self._check(vm, "rules", params, method, path)
            if method == "DELETE":
                del vm.rules[pos]
            elif method == "PUT":
                moveto = params.get("moveto")
                if moveto is not None:
                    moveto = int(moveto)
                    rule = vm.rules.pop(pos)
                    target = moveto if moveto <= pos else moveto - 1
                    vm.rules.insert(min(target, len(vm.rules)), rule)
                else:
                    rule = vm.rules[pos]
                    for key in filter(None, str(params.get("delete", "")).split(",")):
                        rule.pop(key, None)
                    for key, value in params.items():
                        if key in RULE_KEYS:
                            rule[key] = value
            vm.touch("rules")
            return None
        if sub == "firewall/ipset":
            if method == "GET":
                digest = vm.digest("ipsets")
                return [{"name": name, "digest": digest} for name in sorted(vm.ipsets)]
            if method == "POST":
                self._check(vm, "ipsets", params, method, path)
                name = params["name"]
                if name in vm.ipsets:
                    raise self._error(500, f"IPSet '{name}' already exists", method, path)
                vm.ipsets[name] = []
                vm.touch("ipsets")
                return None
        match = re.fullmatch(r"firewall/ipset/([^/]+)", sub)
        if match:
            name = unquote(match.group(1))
            entries = vm.ipsets.get(name)
            if entries is None:
                raise self._error(500, f"no such IPSet '{name}'", method, path)
            if method == "GET":
                digest = vm.digest(f"ipset:{name}")
                return [{**copy.deepcopy(entry), "digest": digest} for entry in entries]
            if method == "POST":
                cidr = params["cidr"]
                if any(entry["cidr"] == cidr for entry in entries):
                    raise self._error(500, f"entry for '{cidr}' already exists", method, path)
                entry = {"cidr": cidr}
                if params.get("nomatch") in (1, "1", True):
                    entry["nomatch"] = 1
                entries.insert(0, entry)
                vm.touch(f"ipset:{name}")
                return None
            if method == "DELETE":
                if entries and params.get("force") not in (1, "1", True):
                    raise self._error(500, "IPSet is not empty", method, path)
                del vm.ipsets[name]
                vm.generations.pop(f"ipset:{name}", None)
                vm.touch("ipsets")
                return None
        match = re.fullmatch(r"firewall/ipset/([^/]+)/(.+)", sub)
        if match:
            name, cidr = unquote(match.group(1)), unquote(match.group(2))
            entries = vm.ipsets.get(name)
            index = next((i for i, e in enumerate(entries or []) if e["cidr"] == cidr), None)
            if entries is None or index is None:
                raise self._error(500, f"no such IP/Network '{cidr}'", method, path)
            if method == "GET":
                return {**entries[index], "digest": vm.digest(f"ipset:{name}")}
            self._check(vm, f"ipset:{name}", params, method, path)
            if method == "PUT":
                entry = {"cidr": cidr}
                if params.get("nomatch") in (1, "1", True):
                    entry["nomatch"] = 1
                entries[index] = entry
            elif method == "DELETE":
                del entries[index]
            vm.touch(f"ipset:{name}")
            return None
        if sub == "config":
            if method == "GET":
                config = dict(vm.config)
                if params.get("current") not in (1, "1", True):
                    config.update(vm.pending)
                return {**config, "digest": vm.digest("config")}
            if method == "PUT":
                self._check(vm, "config", params, method, path)
                for key, value in params.items():
                    if key == "digest":
                        continue
                    if key.startswith("net") and vm.running and not vm.hotplug_network:
                        vm.pending[key] = value
                    else:
                        vm.config[key] = value
                        vm.pending.pop(key, None)
                vm.touch("config")
                return None
        if sub == "pending" and method == "GET":
            out = []
            for key in sorted(set(vm.config) | set(vm.pending)):
                row: dict[str, Any] = {"key": key}
                if key in vm.config:
                    row["value"] = vm.config[key]
                if key in vm.pending:
                    row["pending"] = vm.pending[key]
                out.append(row)
            return out
        raise AssertionError(f"fake Proxmox has no endpoint {method} {path}")
