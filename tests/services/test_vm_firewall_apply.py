"""Applying to Proxmox against the fake: convergence, digest conflicts, crashes, hotplug, drift, readiness,
and the rule that only VM-scoped endpoints are ever written (section 9.1)."""

import ast
import inspect
import random
from pathlib import Path

import pytest

from app.models.vm_firewall import FirewallLocationSettings
from app.services.vm_firewall import proxmox_fw
from app.services.vm_firewall import service as fw
from app.services.vm_firewall.apply import apply, check, readiness
from app.services.vm_firewall.planner import matches
from app.services.vm_firewall.proxmox_fw import (
    ClusterReader,
    VmFirewallClient,
    net_firewall_flag,
    net_with_firewall,
)
from app.services.vm_firewall.render import GUARD_MARKER, render
from tests.fakes.proxmox_firewall import FakeProxmoxFirewall
from tests.fakes.vm_firewall_world import make_world

RULES = [
    {"direction": "in", "action": "ACCEPT", "protocol": "tcp", "ports": "22"},
    {"direction": "in", "action": "ACCEPT", "protocol": "tcp", "ports": "80,443"},
    {"direction": "in", "action": "ACCEPT", "protocol": "icmp", "icmp_type": "echo-request"},
]


@pytest.fixture
def world(db_session):
    return make_world(db_session)


def _setup(world, *, rules=RULES, hotplug=True, baseline=True, net0=None):
    vm = world.vm()
    fake = FakeProxmoxFirewall()
    kw = {"net0": net0} if net0 else {}
    fake_vm = fake.add_vm(vm.vm.proxmox_vmid, hotplug_network=hotplug, **kw)
    if baseline:
        fw.create_baseline(world.db, world.staff_actor, name="g", scope_type="global",
                           settings={"smtp_out": "block"},
                           rules=[{"direction": "in", "action": "DROP", "protocol": "tcp", "ports": "3306"}])
    row = fw.get_or_create(world.db, vm.id)
    fw.set_firewall(world.db, vm.id, world.actor("alice"), revision=row.revision, client_enabled=True)
    fw.replace_rules(world.db, vm.id, world.actor("alice"), list(rules), revision=row.revision)
    return vm, row, fake, fake_vm


def _comments(fake_vm):
    return [r.get("comment") for r in fake_vm.rules]


@pytest.mark.asyncio
async def test_first_apply_converges_and_records_state(world):
    vm, row, fake, fake_vm = _setup(world)
    result = await apply(world.db, vm.id, fake.client(fake_vm.vmid))
    assert result.ok and result.state == "applied"
    assert matches(await fake.client(fake_vm.vmid).read(), render(world.db, vm.id))
    assert fake_vm.options["enable"] == 1 and net_firewall_flag(fake_vm.net0)
    assert (row.applied_revision, row.applied_hash, row.nic_flag) == (row.revision, row.desired_hash, "on")
    assert row.applied_digest and row.last_error is None
    assert _comments(fake_vm)[:2] == [_comments(fake_vm)[0], "rf:smtp25"]
    # A second apply has nothing to do.
    writes = len(fake.writes)
    again = await apply(world.db, vm.id, fake.client(fake_vm.vmid))
    assert again.ok and again.ops == 0 and len(fake.writes) == writes


@pytest.mark.asyncio
async def test_enable_written_after_everything_else(world):
    vm, row, fake, fake_vm = _setup(world)
    await apply(world.db, vm.id, fake.client(fake_vm.vmid))
    paths = [(m, p, params) for m, p, params in fake.writes]
    enable_on = [i for i, (m, p, params) in enumerate(paths) if p.endswith("firewall/options") and params.get("enable") == 1]
    flag = [i for i, (m, p, params) in enumerate(paths) if p.endswith("/config")]
    assert enable_on == [len(paths) - 1] and flag and flag[0] < enable_on[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", range(8))
async def test_converges_from_arbitrary_state(world, seed):
    rnd = random.Random(seed)
    vm, row, fake, fake_vm = _setup(world)
    for _ in range(rnd.randint(1, 6)):
        fake_vm.add_foreign_rule({"type": rnd.choice(["in", "out"]), "action": rnd.choice(["ACCEPT", "DROP"]),
                                  "dport": str(rnd.randint(1, 9999)), "proto": "tcp", "enable": 1,
                                  "comment": rnd.choice(["rf:c999", "manual", "rf:guard", None])},
                                 rnd.randint(0, len(fake_vm.rules)))
    fake_vm.ipsets["stray"] = [{"cidr": "192.0.2.0/24"}]
    fake_vm.ipsets["ipfilter-net0"] = [{"cidr": "10.9.9.9"}]
    fake_vm.set_option("policy_in", "ACCEPT")
    fake_vm.set_option("log_ratelimit", "enable=0")
    result = await apply(world.db, vm.id, fake.client(fake_vm.vmid))
    assert result.ok, result.error
    current = await fake.client(fake_vm.vmid).read()
    assert matches(current, render(world.db, vm.id))
    assert "stray" not in fake_vm.ipsets and "log_ratelimit" not in fake_vm.options
    assert GUARD_MARKER not in _comments(fake_vm)


@pytest.mark.asyncio
async def test_every_write_carries_the_digest_of_the_preceding_read(world):
    vm, row, fake, fake_vm = _setup(world)
    await apply(world.db, vm.id, fake.client(fake_vm.vmid))
    fw.replace_rules(world.db, vm.id, world.actor("alice"), list(reversed(RULES)) + [
        {"direction": "out", "action": "DROP", "protocol": "tcp", "ports": "6667"}], revision=row.revision)
    start = len(fake.writes)
    await apply(world.db, vm.id, fake.client(fake_vm.vmid))
    digestable = [(m, p, params) for m, p, params in fake.writes[start:]
                  if (m == "PUT" or (m == "DELETE" and "/rules/" in p))]
    assert digestable and all(params.get("digest") for _, _, params in digestable)


@pytest.mark.asyncio
async def test_digest_conflict_is_retried(world):
    vm, row, fake, fake_vm = _setup(world)
    await apply(world.db, vm.id, fake.client(fake_vm.vmid))
    fw.replace_rules(world.db, vm.id, world.actor("alice"), RULES[:1], revision=row.revision)
    fake.on_next_write(lambda f: fake_vm.add_foreign_rule({"type": "in", "action": "ACCEPT", "enable": 1}, 0))
    result = await apply(world.db, vm.id, fake.client(fake_vm.vmid))
    assert result.ok and result.replans >= 1
    assert matches(await fake.client(fake_vm.vmid).read(), render(world.db, vm.id))


@pytest.mark.asyncio
async def test_persistent_conflict_gives_up_after_three_replans(world):
    vm, row, fake, fake_vm = _setup(world)
    await apply(world.db, vm.id, fake.client(fake_vm.vmid))
    fw.replace_rules(world.db, vm.id, world.actor("alice"), RULES[:1], revision=row.revision)
    def racer(f):
        fake_vm.add_foreign_rule({"type": "in", "action": "ACCEPT", "enable": 1})
        f.on_next_write(racer)

    fake.on_next_write(racer)
    result = await apply(world.db, vm.id, fake.client(fake_vm.vmid))
    assert not result.ok and result.state == "error"
    assert row.state == "error" and row.next_attempt_at is not None and row.failing_since is not None
    assert row.applied_revision != row.revision


@pytest.mark.asyncio
async def test_crash_mid_apply_leaves_the_guard_and_the_next_apply_repairs(world):
    vm, row, fake, fake_vm = _setup(world)
    await apply(world.db, vm.id, fake.client(fake_vm.vmid))
    fw.replace_rules(world.db, vm.id, world.actor("alice"), [
        {"direction": "in", "action": "ACCEPT", "protocol": "tcp", "ports": "8080"},
        {"direction": "in", "action": "ACCEPT", "protocol": "tcp", "ports": "8443"},
    ], revision=row.revision)
    fake.crash_after(5)
    result = await apply(world.db, vm.id, fake.client(fake_vm.vmid))
    assert not result.ok and "crashed" in result.error
    guards = [r for r in fake_vm.rules if r.get("comment") == GUARD_MARKER]
    assert guards and all(str(g["enable"]) == "1" for g in guards)
    fake.crash_after(None)
    result = await apply(world.db, vm.id, fake.client(fake_vm.vmid))
    assert result.ok and GUARD_MARKER not in _comments(fake_vm)
    assert matches(await fake.client(fake_vm.vmid).read(), render(world.db, vm.id))


@pytest.mark.asyncio
async def test_server_error_is_recorded(world):
    vm, row, fake, fake_vm = _setup(world)
    fake.fail_next(503, "Service Unavailable", method="POST")
    result = await apply(world.db, vm.id, fake.client(fake_vm.vmid))
    assert not result.ok and "503" in row.last_error and row.state == "error"
    result = await apply(world.db, vm.id, fake.client(fake_vm.vmid))
    assert result.ok and row.last_error is None and row.failing_since is None


@pytest.mark.asyncio
async def test_hotplug_off_reports_pending_reboot(world):
    vm, row, fake, fake_vm = _setup(world, hotplug=False)
    result = await apply(world.db, vm.id, fake.client(fake_vm.vmid))
    assert result.ok and result.state == "pending_reboot" and row.nic_flag == "pending_reboot"
    assert not net_firewall_flag(fake_vm.config["net0"]) and net_firewall_flag(fake_vm.pending["net0"])
    # Applying again does not write net0 again.
    configs = [w for w in fake.writes if w[1].endswith("/config")]
    await apply(world.db, vm.id, fake.client(fake_vm.vmid))
    assert [w for w in fake.writes if w[1].endswith("/config")] == configs
    fake_vm.reboot()
    result = await apply(world.db, vm.id, fake.client(fake_vm.vmid))
    assert result.state == "applied" and row.nic_flag == "on"


@pytest.mark.asyncio
async def test_missing_vm(world):
    vm, row, fake, fake_vm = _setup(world)
    del fake.vms[fake_vm.vmid]
    result = await apply(world.db, vm.id, fake.client(fake_vm.vmid))
    assert result.state == "vm_missing" and row.state == "vm_missing"


@pytest.mark.asyncio
async def test_client_off_with_baseline_only_disables(world):
    vm, row, fake, fake_vm = _setup(world)
    await apply(world.db, vm.id, fake.client(fake_vm.vmid))
    fw.set_firewall(world.db, vm.id, world.actor("alice"), revision=row.revision, client_enabled=False)
    result = await apply(world.db, vm.id, fake.client(fake_vm.vmid))
    assert result.state == "off" and fake_vm.options["enable"] == 0
    assert all(c.startswith("rf:b") or c.startswith("rf:smtp") for c in _comments(fake_vm))
    assert fake_vm.options["policy_in"] == "ACCEPT"


@pytest.mark.asyncio
async def test_excluded_is_not_touched(world):
    vm, row, fake, fake_vm = _setup(world)
    fw.set_admin_fields(world.db, vm.id, world.staff_actor, revision=row.revision, excluded=True)
    result = await apply(world.db, vm.id, fake.client(fake_vm.vmid))
    assert result.state == "unmanaged" and fake.writes == []


@pytest.mark.asyncio
async def test_check_reports_drift_without_writing(world):
    vm, row, fake, fake_vm = _setup(world)
    await apply(world.db, vm.id, fake.client(fake_vm.vmid))
    report = await check(world.db, vm.id, fake.client(fake_vm.vmid))
    assert report.in_sync and report.digest_matches and report.nic_flag == "on"
    writes = len(fake.writes)
    fake_vm.add_foreign_rule({"type": "in", "action": "ACCEPT", "enable": 1, "comment": "hand-made"})
    fake_vm.set_option("policy_in", "ACCEPT")
    report = await check(world.db, vm.id, fake.client(fake_vm.vmid))
    assert not report.in_sync and not report.digest_matches
    assert any("no Rackflow marker" in d for d in report.differences)
    assert any("policy_in" in d for d in report.differences)
    assert len(fake.writes) == writes


# net0


def test_net0_flag_keeps_everything_else():
    net0 = "virtio=BC:24:11:AA:BB:CC,bridge=vmbr0,tag=42,link_down=1,rate=12.5,queues=4"
    assert net_with_firewall(net0, True) == net0 + ",firewall=1"
    on = "virtio=BC:24:11:AA:BB:CC,bridge=vmbr0,firewall=1,tag=42,rate=12.5"
    assert net_with_firewall(on, False) == "virtio=BC:24:11:AA:BB:CC,bridge=vmbr0,firewall=0,tag=42,rate=12.5"


@pytest.mark.asyncio
async def test_set_net0_flag_against_the_fake():
    fake = FakeProxmoxFirewall()
    vm = fake.add_vm(5, net0="e1000=BC:24:11:00:00:09,bridge=vmbr0,tag=7,rate=10")
    result = await fake.client(5).set_net0_firewall_flag(True)
    assert result.changed and not result.pending
    assert vm.config["net0"] == "e1000=BC:24:11:00:00:09,bridge=vmbr0,tag=7,rate=10,firewall=1"
    assert not (await fake.client(5).set_net0_firewall_flag(True)).changed


@pytest.mark.asyncio
async def test_fake_rejects_a_stale_digest():
    fake = FakeProxmoxFirewall()
    fake.add_vm(5)
    client = fake.client(5)
    options = await client.get_options()
    await client.set_options({"policy_in": "DROP"}, digest=options["digest"])
    with pytest.raises(proxmox_fw.DigestConflict):
        await client.set_options({"policy_in": "ACCEPT"}, digest=options["digest"])


# Readiness


@pytest.mark.asyncio
async def test_readiness_passes_on_a_healthy_cluster():
    fake = FakeProxmoxFirewall(nodes=["pve1", "pve2"])
    result = await readiness(fake.reader())
    assert result.ready and result.reasons == []


@pytest.mark.asyncio
async def test_readiness_failures():
    fake = FakeProxmoxFirewall(
        nodes=["pve1", "pve2", "pve3"],
        cluster_options={"enable": 0},
        node_options={"pve1": {"nftables": 1}, "pve2": {"nftables": 1}, "pve3": {"nftables": 0}},
        services={"pve1": {"pve-firewall": "running", "proxmox-firewall": "stopped"}},
    )
    result = await readiness(fake.reader())
    assert not result.ready
    assert "datacenter firewall disabled" in result.reasons
    assert "node pve1: proxmox-firewall not running" in result.reasons
    assert any("nftables" in r and "pve3" in r and "pve1" not in r for r in result.reasons)
    assert fake.writes == []


@pytest.mark.asyncio
async def test_location_enable_needs_a_passing_readiness(world):
    location = world.db.get(FirewallLocationSettings, world.location.id)
    location.enabled = False
    world.db.flush()
    from app.services.vm_firewall.errors import FirewallConflict

    with pytest.raises(FirewallConflict):
        fw.update_location_settings(world.db, world.staff_actor, world.location.id, enabled=True)
    fake = FakeProxmoxFirewall()
    fw.record_readiness(world.db, world.location.id, world.cluster.id, (await readiness(fake.reader())).to_dict())
    assert fw.update_location_settings(world.db, world.staff_actor, world.location.id, enabled=True).enabled


# Only VM-scoped endpoints are written (section 9.1)


class _Recorder:
    def __init__(self):
        self.calls = []

    async def request(self, method, path, params=None):
        self.calls.append((method, path))
        if path.endswith("/config"):
            return {"net0": "virtio=BC:24:11:00:00:01,bridge=vmbr0", "digest": "d"}
        return [] if method == "GET" else None


@pytest.mark.asyncio
async def test_every_write_url_is_under_the_vm():
    recorder = _Recorder()
    client = VmFirewallClient(recorder, 4242)
    writers = []
    for name, method in inspect.getmembers(VmFirewallClient, inspect.iscoroutinefunction):
        if name.startswith("_") or name.startswith("get_") or name in ("read", "list_ipsets", "net0_pending"):
            continue
        writers.append(name)
        params = list(inspect.signature(method).parameters.values())[1:]
        args, kwargs = [], {}
        for param in params:
            sample = {"pos": 0, "params": {"type": "in", "action": "DROP"}, "name": "rf-s1", "cidr": "192.0.2.0/24",
                      "flag": True, "nomatch": False}.get(param.name)
            if param.kind == param.KEYWORD_ONLY:
                if param.default is param.empty:
                    kwargs[param.name] = sample if sample is not None else "d"
            else:
                args.append(sample)
        await getattr(client, name)(*args, **kwargs)
    assert {"set_options", "create_rule", "update_rule", "delete_rule", "create_ipset", "delete_ipset",
            "add_ipset_entry", "update_ipset_entry", "remove_ipset_entry", "set_net0_firewall_flag"} <= set(writers)
    writes = [(m, p) for m, p in recorder.calls if m != "GET"]
    assert writes and all(p.startswith("/nodes/{node}/qemu/4242/") for _, p in writes)


@pytest.mark.asyncio
async def test_cluster_reader_only_reads():
    recorder = _Recorder()
    reader = ClusterReader(recorder)
    await reader.cluster_options()
    await reader.nodes()
    await reader.node_options("pve1")
    await reader.service_state("pve1", "pve-firewall")
    assert {m for m, _ in recorder.calls} == {"GET"}
    with pytest.raises(ValueError):
        await reader.service_state("pve1", "sshd")


def test_no_code_writes_cluster_or_node_firewall():
    package = Path(proxmox_fw.__file__).parent
    for path in package.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "request":
                # Only the client's _read/_write and the reader's _get call the transport.
                assert path.name == "proxmox_fw.py", f"{path.name} calls the transport directly"
    source = Path(proxmox_fw.__file__).read_text()
    tree = ast.parse(source)
    reader = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ClusterReader")
    methods = {c.value for c in ast.walk(reader) if isinstance(c, ast.Constant) and c.value in ("POST", "PUT", "DELETE")}
    assert methods == set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "/firewall" in node.value:
            text = node.value
            if text.startswith("/cluster/") or (text.startswith("/nodes/") and "/qemu/" not in text):
                assert node.lineno in range(reader.lineno, reader.end_lineno + 1), text
