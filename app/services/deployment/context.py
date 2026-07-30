"""Execution context passed to deployment steps.

Holds the DB session, service, and job, and lazily resolves the Proxmox plugin,
cluster, template VMID, sizing specs, and IP allocation needed by steps. A fresh
context is created for each worker tick, so cached values are per-tick.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx
from sqlalchemy.orm import Session

from app.dao.product_catalog_dao import VMTemplateDAO
from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO
from app.dao.vm_ip_allocation_dao import VMIPAllocationDAO
from app.models.service import Service
from app.plugins.registry import get_registry
from app.services.deployment.step import DeploymentError
from app.services.proxmox_placement import attach_relocator, cluster_to_proxmox_plugin_config
from app.services.service_resource import vm_placement

logger = logging.getLogger(__name__)


async def _find_template_vmid_live(cluster, node_name: str, template_name: str) -> Optional[int]:
    """Resolve a template VMID directly from the Proxmox API at provision time.

    Mirrors the previous linear executor so we do not hard-depend on synced
    template inventory.
    """
    found = await _find_template_on_nodes_live(cluster, [node_name], template_name)
    return found[1] if found else None


async def _find_template_on_nodes_live(
    cluster,
    node_names: List[str],
    template_name: str,
) -> Optional[Tuple[str, int]]:
    """Scan the given nodes via the Proxmox API for a QEMU template by name."""
    auth_url = f"{cluster.api_url.rstrip('/')}/api2/json/access/ticket"
    base = f"{cluster.api_url.rstrip('/')}/api2/json"
    async with httpx.AsyncClient(verify=cluster.verify_ssl, timeout=20.0) as client:
        auth_resp = await client.post(
            auth_url,
            data={"username": cluster.username, "password": cluster.password},
        )
        auth_resp.raise_for_status()
        payload = auth_resp.json().get("data") or {}
        ticket = payload.get("ticket")
        csrf = payload.get("CSRFPreventionToken")
        if not ticket:
            raise DeploymentError("Failed to authenticate with Proxmox while resolving template")
        headers = {"Cookie": f"PVEAuthCookie={ticket}"}
        if csrf:
            headers["CSRFPreventionToken"] = csrf
        for node_name in node_names:
            if not (node_name or "").strip():
                continue
            qemu_resp = await client.get(f"{base}/nodes/{node_name}/qemu", headers=headers)
            qemu_resp.raise_for_status()
            for row in qemu_resp.json().get("data") or []:
                if int(row.get("template") or 0) != 1:
                    continue
                if str(row.get("name") or "") != template_name:
                    continue
                vmid = row.get("vmid")
                if vmid is not None:
                    return str(node_name).strip(), int(vmid)
    return None


class DeploymentContext:
    def __init__(self, db: Session, service: Service, job) -> None:
        self.db = db
        self.service = service
        self.job = job
        self.logger = logger
        self._plugin = None
        self._cluster = None

    @property
    def vm(self):
        return self.service.vm

    def placement(self):
        return vm_placement(self.service)

    def require_placement(self):
        cid, node, vmid = self.placement()
        if cid is None or not (node or "").strip() or vmid is None:
            raise DeploymentError(
                "VM needs proxmox_cluster_id, proxmox_node_name, and proxmox_vmid before provisioning"
            )
        return cid, node.strip(), int(vmid)

    def get_cluster(self):
        if self._cluster is None:
            cid, _, _ = self.require_placement()
            cluster = ProxmoxInventoryDAO.get_cluster(self.db, cid)
            if not cluster:
                raise DeploymentError("Proxmox cluster not found")
            self._cluster = cluster
        return self._cluster

    def get_plugin(self):
        """Return the (cached) Proxmox plugin for this deployment run.

        Provisioning trusts the placement node outright -- the guest may not
        exist there yet (create/clone), so a cluster-wide search would just
        fail. A relocator is still attached so a guest migrated *during* a
        long-running deployment (e.g. by an admin/HA event, not by us) can
        self-heal on the next node-scoped call instead of hard-failing.
        """
        if self._plugin is None:
            _cid, node, vmid = self.require_placement()
            cluster = self.get_cluster()
            plugin_config = cluster_to_proxmox_plugin_config(cluster, node, vmid)
            self._plugin = get_registry().get_plugin("proxmox", plugin_config)
            attach_relocator(self._plugin, self.db, self.service, cluster, vmid)
        return self._plugin

    def get_template(self):
        vm = self.vm
        if not vm or not vm.vm_template_id:
            raise DeploymentError("VM service has no vm_template_id")
        tmpl = VMTemplateDAO.get_by_id(self.db, vm.vm_template_id)
        if not tmpl:
            raise DeploymentError("VM template catalog row not found")
        return tmpl

    async def resolve_template_location(self) -> Tuple[str, int]:
        """Return ``(template_home_node, template_vmid)`` for the catalog template.

        With ``shared_storage``, search the whole cluster (inventory then live API).
        Otherwise require the template on the placed node.
        """
        cid, node, _ = self.require_placement()
        tmpl = self.get_template()
        template_name = tmpl.proxmox_template_name
        shared = bool(tmpl.shared_storage)

        if shared:
            found = ProxmoxInventoryDAO.find_template_in_cluster(
                self.db, cluster_id=cid, template_name=template_name
            )
            if found is None:
                cluster = self.get_cluster()
                node_names = [
                    str(n.node_name)
                    for n in (cluster.nodes or [])
                    if n.enabled and (n.node_name or "").strip()
                ]
                if not node_names:
                    node_names = [node]
                found = await _find_template_on_nodes_live(cluster, node_names, template_name)
            if found is None:
                raise DeploymentError(
                    f"No Proxmox template named '{template_name}' found in cluster {cid}."
                )
            return found

        template_vmid = ProxmoxInventoryDAO.find_template_vmid_on_node(
            self.db,
            cluster_id=cid,
            node_name=node,
            template_name=template_name,
        )
        if template_vmid is None:
            template_vmid = await _find_template_vmid_live(self.get_cluster(), node, template_name)
        if template_vmid is None:
            raise DeploymentError(
                f"No Proxmox template named '{template_name}' found on node "
                f"'{node}' in cluster {cid}."
            )
        return node, int(template_vmid)

    async def resolve_template_vmid(self) -> int:
        _node, vmid = await self.resolve_template_location()
        return int(vmid)

    def get_specs(self) -> Dict[str, Any]:
        from app.services.vm_provisioning_service import _normalize_vm_specs

        cfg = self.service.config or {}
        vm_plan = cfg.get("vm_plan") or {}
        specs = dict(vm_plan.get("effective_specs") or {})
        if not specs:
            snap = cfg.get("product_snapshot") or {}
            if isinstance(snap, dict):
                specs = dict(snap.get("effective_specs") or {})
        return _normalize_vm_specs(specs)

    def get_ip_allocation(self):
        vm = self.vm
        if vm and vm.vm_ip_allocation_id:
            return VMIPAllocationDAO.get_by_id(self.db, vm.vm_ip_allocation_id)
        return None
