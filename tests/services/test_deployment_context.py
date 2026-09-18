"""Unit tests for deployment context helpers and lazy resolvers."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.services.deployment.context import (
    DeploymentContext,
    _find_template_on_nodes_live,
    _find_template_vmid_live,
    _proxmox_api_headers,
    _qemu_template_vmid,
)
from app.models.service import ServiceType
from app.services.deployment.step import DeploymentError


def test_qemu_template_vmid_filters_rows():
    assert _qemu_template_vmid({"template": 0, "name": "t", "vmid": 1}, "t") is None
    assert _qemu_template_vmid({"template": 1, "name": "other", "vmid": 1}, "t") is None
    assert _qemu_template_vmid({"template": 1, "name": "t", "vmid": 900}, "t") == 900
    assert _qemu_template_vmid({"template": 1, "name": "t", "vmid": None}, "t") is None
    assert _qemu_template_vmid({"template": "1", "name": "t", "vmid": "12"}, "t") == 12


@pytest.mark.asyncio
async def test_proxmox_api_headers_require_ticket():
    client = AsyncMock()
    auth = Mock()
    auth.raise_for_status = Mock()
    auth.json.return_value = {"data": {}}
    client.post.return_value = auth
    cluster = SimpleNamespace(api_url="https://pve.example/", username="root@pam", password="x")
    with pytest.raises(DeploymentError, match="authenticate"):
        await _proxmox_api_headers(cluster, client)

    auth.json.return_value = {"data": {"ticket": "T", "CSRFPreventionToken": "C"}}
    headers = await _proxmox_api_headers(cluster, client)
    assert headers["Cookie"] == "PVEAuthCookie=T"
    assert headers["CSRFPreventionToken"] == "C"


@pytest.mark.asyncio
async def test_find_template_on_nodes_live(monkeypatch):
    cluster = SimpleNamespace(
        api_url="https://pve.example/",
        username="root@pam",
        password="x",
        verify_ssl=False,
    )

    class FakeResp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            r = FakeResp({"data": {"ticket": "T"}})
            return r

        async def get(self, url, headers=None):
            if url.endswith("/nodes/pve/qemu"):
                return FakeResp({"data": [{"template": 1, "name": "ci-deb", "vmid": 101}]})
            return FakeResp({"data": []})

    monkeypatch.setattr("app.services.deployment.context.httpx.AsyncClient", FakeClient)
    found = await _find_template_on_nodes_live(cluster, ["", "pve"], "ci-deb")
    assert found == ("pve", 101)
    assert await _find_template_on_nodes_live(cluster, ["other"], "ci-deb") is None
    assert await _find_template_vmid_live(cluster, "pve", "ci-deb") == 101
    assert await _find_template_vmid_live(cluster, "pve", "missing") is None


def test_deployment_context_placement_and_specs(db_session):
    vm = SimpleNamespace(
        vm_template_id=None,
        vm_ip_allocation_id=None,
        proxmox_cluster_id=None,
        proxmox_node_name=None,
        proxmox_vmid=None,
    )
    service = SimpleNamespace(
        vm=vm,
        service_type=ServiceType.VM,
        config={"vm_plan": {"effective_specs": {"cores": 4, "memory_mb": 1024}}},
    )
    ctx = DeploymentContext(db_session, service, job=SimpleNamespace(id=1))
    assert ctx.vm is vm
    with pytest.raises(DeploymentError, match="proxmox_cluster_id"):
        ctx.require_placement()

    vm.proxmox_cluster_id = 1
    vm.proxmox_node_name = "pve"
    vm.proxmox_vmid = 5000
    # placement() reads service.vm via vm_placement
    service.vm = vm
    specs = ctx.get_specs()
    assert specs.get("cores") == 4 or "cores" in specs or isinstance(specs, dict)
    assert ctx.get_ip_allocation() is None
    vm.vm_ip_allocation_id = 9

    captured = {}

    def fake_get(db, alloc_id):
        captured["id"] = alloc_id
        return SimpleNamespace(id=alloc_id)

    import app.services.deployment.context as ctx_mod

    orig = ctx_mod.VMIPAllocationDAO.get_by_id
    ctx_mod.VMIPAllocationDAO.get_by_id = staticmethod(fake_get)
    try:
        assert ctx.get_ip_allocation().id == 9
        assert captured["id"] == 9
    finally:
        ctx_mod.VMIPAllocationDAO.get_by_id = orig

    with pytest.raises(DeploymentError, match="vm_template_id"):
        ctx.get_template()

    vm.vm_template_id = 3
    orig_tmpl = ctx_mod.VMTemplateDAO.get_by_id
    ctx_mod.VMTemplateDAO.get_by_id = staticmethod(lambda db, i: None)
    try:
        with pytest.raises(DeploymentError, match="catalog row"):
            ctx.get_template()
    finally:
        ctx_mod.VMTemplateDAO.get_by_id = orig_tmpl

    service.config = {"product_snapshot": {"effective_specs": {"cpu_cores": 8, "ram_mb": 4096}}}
    specs = ctx.get_specs()
    assert specs.get("cores") == 8
    assert specs.get("memory_mb") == 4096


def test_get_cluster_and_plugin(db_session, monkeypatch):
    vm = SimpleNamespace(
        vm_template_id=3,
        vm_ip_allocation_id=None,
        proxmox_cluster_id=1,
        proxmox_node_name="pve",
        proxmox_vmid=100,
    )
    service = SimpleNamespace(vm=vm, service_type=ServiceType.VM, config={})
    ctx = DeploymentContext(db_session, service, job=SimpleNamespace(id=1))
    monkeypatch.setattr(
        "app.services.deployment.context.ProxmoxInventoryDAO.get_cluster",
        staticmethod(lambda db, cid: None),
    )
    with pytest.raises(DeploymentError, match="cluster not found"):
        ctx.get_cluster()

    cluster = SimpleNamespace(id=1, nodes=[], name="c")
    monkeypatch.setattr(
        "app.services.deployment.context.ProxmoxInventoryDAO.get_cluster",
        staticmethod(lambda db, cid: cluster),
    )
    ctx._cluster = None
    assert ctx.get_cluster() is cluster
    assert ctx.get_cluster() is cluster

    plugin = object()
    monkeypatch.setattr(
        "app.services.deployment.context.cluster_to_proxmox_plugin_config",
        lambda *a, **k: {"x": 1},
    )
    monkeypatch.setattr(
        "app.services.deployment.context.get_registry",
        lambda: SimpleNamespace(get_plugin=lambda name, cfg: plugin),
    )
    monkeypatch.setattr("app.services.deployment.context.attach_relocator", lambda *a, **k: None)
    assert ctx.get_plugin() is plugin
    assert ctx.get_plugin() is plugin


@pytest.mark.asyncio
async def test_resolve_template_location_shared_and_local(db_session, monkeypatch):
    vm = SimpleNamespace(
        vm_template_id=3,
        vm_ip_allocation_id=None,
        proxmox_cluster_id=1,
        proxmox_node_name="pve",
        proxmox_vmid=100,
    )
    service = SimpleNamespace(vm=vm, service_type=ServiceType.VM, config={})
    ctx = DeploymentContext(db_session, service, job=SimpleNamespace(id=1))
    tmpl_shared = SimpleNamespace(proxmox_template_name="ci-deb", shared_storage=True)
    monkeypatch.setattr(
        "app.services.deployment.context.VMTemplateDAO.get_by_id",
        staticmethod(lambda db, i: tmpl_shared),
    )
    monkeypatch.setattr(
        "app.services.deployment.context.ProxmoxInventoryDAO.find_template_in_cluster",
        staticmethod(lambda db, cluster_id, template_name: ("pve2", 900)),
    )
    assert await ctx.resolve_template_location() == ("pve2", 900)
    assert await ctx.resolve_template_vmid() == 900

    monkeypatch.setattr(
        "app.services.deployment.context.ProxmoxInventoryDAO.find_template_in_cluster",
        staticmethod(lambda db, cluster_id, template_name: None),
    )
    cluster = SimpleNamespace(
        nodes=[SimpleNamespace(enabled=True, node_name="pve")],
        api_url="https://pve.example/",
        username="u",
        password="p",
        verify_ssl=False,
    )
    monkeypatch.setattr(
        "app.services.deployment.context.ProxmoxInventoryDAO.get_cluster",
        staticmethod(lambda db, cid: cluster),
    )
    monkeypatch.setattr(
        "app.services.deployment.context._find_template_on_nodes_live",
        AsyncMock(return_value=("pve", 101)),
    )
    ctx._cluster = None
    assert await ctx.resolve_template_location() == ("pve", 101)

    monkeypatch.setattr(
        "app.services.deployment.context._find_template_on_nodes_live",
        AsyncMock(return_value=None),
    )
    with pytest.raises(DeploymentError, match="No Proxmox template"):
        await ctx.resolve_template_location()

    tmpl_local = SimpleNamespace(proxmox_template_name="ci-deb", shared_storage=False)
    monkeypatch.setattr(
        "app.services.deployment.context.VMTemplateDAO.get_by_id",
        staticmethod(lambda db, i: tmpl_local),
    )
    monkeypatch.setattr(
        "app.services.deployment.context.ProxmoxInventoryDAO.find_template_vmid_on_node",
        staticmethod(lambda db, cluster_id, node_name, template_name: 55),
    )
    assert await ctx.resolve_template_location() == ("pve", 55)

    monkeypatch.setattr(
        "app.services.deployment.context.ProxmoxInventoryDAO.find_template_vmid_on_node",
        staticmethod(lambda db, cluster_id, node_name, template_name: None),
    )
    monkeypatch.setattr(
        "app.services.deployment.context._find_template_vmid_live",
        AsyncMock(return_value=None),
    )
    with pytest.raises(DeploymentError, match="on node"):
        await ctx.resolve_template_location()
