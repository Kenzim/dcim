"""Helpers for Proxmox cluster API URL → plugin_config and service placement."""
from __future__ import annotations

import functools
from datetime import datetime
from typing import Optional, Protocol, Tuple
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO
from app.plugins.registry import get_registry
from app.services.proxmox_vm_search import find_node_for_vmid
from app.services.service_resource import vm_placement


class _ProxmoxClusterLike(Protocol):
    api_url: str
    username: str
    password: str
    verify_ssl: bool


class ProxmoxPlacementError(RuntimeError):
    """Raised by :func:`resolve_proxmox_plugin_for_service` when a VM
    service's Proxmox placement can't be resolved to a live plugin (missing
    cluster/vmid, unknown cluster, or the VMID genuinely isn't found
    anywhere in the cluster)."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        self.status_code = status_code
        super().__init__(message)


def cluster_to_proxmox_plugin_config(
    cluster: _ProxmoxClusterLike,
    node_name: str,
    vmid: int,
) -> dict:
    """
    Build Proxmox plugin_config from inventory cluster + node + vmid.

    Cluster `api_url` is typically https://host:8006/
    """
    parsed = urlparse((cluster.api_url or "").strip())
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Proxmox cluster api_url must include a hostname")
    port = parsed.port or 8006
    return {
        "hostname": hostname,
        "username": cluster.username,
        "password": cluster.password,
        "port": port,
        "node": node_name,
        "vmid": int(vmid),
        "verify_ssl": bool(cluster.verify_ssl),
    }


def _build_plugin(cluster: _ProxmoxClusterLike, node_name: str, vmid: int):
    config = cluster_to_proxmox_plugin_config(cluster, node_name, int(vmid))
    return get_registry().get_plugin("proxmox", config)


def _persist_node(db: Session, service, node_name: str) -> None:
    """Write a freshly-discovered node back to the ``service_vm`` cache."""
    from app.dao.service_dao import ServiceDAO

    if not service.vm:
        return
    service.vm.proxmox_node_name = node_name
    ServiceDAO.update(db, service)


async def _relocate_vm(db: Session, service, cluster: _ProxmoxClusterLike, vmid: int) -> Optional[str]:
    """Look up ``vmid``'s current node cluster-wide and persist it if found.

    Passed to :meth:`ProxmoxPlugin.set_relocator` so a 404 that happens
    *during* a call (the guest migrated in the narrow race between resolving
    placement and issuing the request) can recover instead of failing.
    """
    found = await find_node_for_vmid(cluster, vmid)
    if found:
        _persist_node(db, service, found)
    return found


def attach_relocator(plugin, db: Session, service, cluster: _ProxmoxClusterLike, vmid: int) -> None:
    """Register the standard 404 relocator on a plugin built outside
    :func:`resolve_proxmox_plugin_for_service` (e.g. :class:`DeploymentContext`,
    which keeps its own cached plugin for the lifetime of a provisioning run
    but still benefits from self-healing if the guest is migrated mid-run)."""
    plugin.set_relocator(functools.partial(_relocate_vm, db, service, cluster, int(vmid)))


async def _resolve_on_cached_node(
    db: Session,
    service,
    cluster,
    *,
    node: str,
    vmid: int,
    persist: bool,
) -> Optional[Tuple[object, int, str, int]]:
    try:
        plugin = _build_plugin(cluster, node, vmid)
    except ValueError as exc:
        raise ProxmoxPlacementError(str(exc), status_code=400) from exc
    if persist:
        plugin.set_relocator(functools.partial(_relocate_vm, db, service, cluster, vmid))
    if await plugin.vm_exists():
        return plugin, cluster.id, node, vmid
    return None


def _placement_not_found_error(cluster_id: int, vmid: int, node: Optional[str]) -> ProxmoxPlacementError:
    return ProxmoxPlacementError(
        f"VM {vmid} was not found on any node in Proxmox cluster {cluster_id}"
        + (f" (last known node: {node})" if node else ""),
        status_code=404,
    )


def _require_cluster_placement(
    db: Session, service
) -> tuple[object, int, Optional[str], int]:
    cluster_id, node_name, raw_vmid = vm_placement(service)
    if cluster_id is None or raw_vmid is None:
        raise ProxmoxPlacementError(
            "VM service is missing Proxmox placement (proxmox_cluster_id, proxmox_vmid)",
            status_code=400,
        )
    cluster = ProxmoxInventoryDAO.get_cluster(db, cluster_id)
    if cluster is None:
        raise ProxmoxPlacementError(f"Unknown proxmox_cluster_id {cluster_id}", status_code=404)
    vmid = int(raw_vmid)
    node = (node_name or "").strip() or None
    return cluster, cluster_id, node, vmid


async def resolve_proxmox_plugin_for_service(
    db: Session,
    service,
    *,
    require_guest: bool = True,
    persist: bool = True,
) -> Tuple[object, int, str, int]:
    """
    Build a live Proxmox plugin for a VM service, treating the cached
    ``service.vm.proxmox_node_name`` as a cache rather than a hard
    requirement — so migrating a guest (e.g. HA, or a load balancer moving
    it between nodes) never breaks control just because the cached node is
    stale.

    Behavior:

    - Requires ``proxmox_cluster_id`` and ``proxmox_vmid``; the node is
      optional.
    - If a node is cached, it's probed (``vm_exists``): a live match is used
      as-is; a 404 (guest moved / cache empty) falls through to a
      cluster-wide search.
    - The cluster-wide search (``GET /cluster/resources``) finds the node
      currently hosting the VMID; when found and different from the cache,
      it's persisted back to ``service.vm.proxmox_node_name``.
    - ``require_guest=False`` (used while provisioning, before the guest
      exists yet) trusts a cached node outright and skips the live
      probe/search — there's nothing to find cluster-wide yet.
    - The returned plugin also gets a one-shot relocator (see
      :func:`_relocate_vm`) so a 404 on the very next call (migrated between
      resolve and use) can self-heal instead of erroring.

    Returns ``(plugin, cluster_id, node_name, vmid)``.

    Raises :class:`ProxmoxPlacementError` when placement is missing/invalid,
    or (with ``require_guest=True``) the VMID isn't found anywhere in the
    cluster.
    """
    cluster, cluster_id, node, vmid = _require_cluster_placement(db, service)

    def _finish(resolved_node: str):
        try:
            plugin = _build_plugin(cluster, resolved_node, vmid)
        except ValueError as exc:
            raise ProxmoxPlacementError(str(exc), status_code=400) from exc
        if persist:
            plugin.set_relocator(functools.partial(_relocate_vm, db, service, cluster, vmid))
        return plugin, cluster_id, resolved_node, vmid

    if node and not require_guest:
        return _finish(node)

    if node:
        cached = await _resolve_on_cached_node(
            db, service, cluster, node=node, vmid=vmid, persist=persist
        )
        if cached is not None:
            return cached

    found_node = await find_node_for_vmid(cluster, vmid)
    if found_node:
        if persist and found_node != node:
            _persist_node(db, service, found_node)
        return _finish(found_node)

    if not require_guest:
        raise ProxmoxPlacementError(
            "VM service is missing Proxmox placement (proxmox_cluster_id, proxmox_node_name, proxmox_vmid)",
            status_code=400,
        )
    raise _placement_not_found_error(cluster_id, vmid, node)


def _node_free_ram_score(node) -> float:
    """
    Free RAM (bytes) from the latest capacity snapshot; nodes without a usable
    snapshot rank last so placement prefers nodes with known headroom.
    """
    snaps = list(node.capacity_snapshots or [])
    if not snaps:
        return float("-inf")
    latest = max(snaps, key=lambda s: s.created_at or datetime.min)
    total = latest.ram_total_bytes
    if total is None:
        return float("-inf")
    used = latest.ram_used_bytes or 0
    return float(total - used)


def _candidate_clusters(db: Session, cluster_id: Optional[int]):
    if cluster_id is not None:
        cluster = ProxmoxInventoryDAO.get_cluster(db, cluster_id)
        if cluster is None:
            raise ValueError(f"Proxmox cluster {cluster_id} not found")
        return [cluster]
    clusters = [c for c in ProxmoxInventoryDAO.list_clusters(db) if c.enabled]
    if not clusters:
        raise ValueError("No enabled Proxmox clusters available for auto-placement")
    return clusters


def _node_has_template(node, template_name: str) -> bool:
    return node.enabled and any((t.name or "") == template_name for t in (node.templates or []))


def _cluster_has_template(cluster, template_name: str) -> bool:
    return any(
        any((t.name or "") == template_name for t in (node.templates or []))
        for node in (cluster.nodes or [])
    )


def _placement_candidate_nodes(cluster, template_name: str, shared_storage: bool):
    if shared_storage and not _cluster_has_template(cluster, template_name):
        return
    for node in cluster.nodes or []:
        if not node.enabled:
            continue
        if not shared_storage and not _node_has_template(node, template_name):
            continue
        yield node


def auto_place_vm(
    db: Session,
    *,
    template_name: str,
    cluster_id: Optional[int] = None,
    shared_storage: bool = False,
) -> Tuple[int, str]:
    """
    Pick a Proxmox (cluster_id, node_name) for a VM whose catalog template is
    ``template_name`` (matches synced ``ProxmoxTemplate.name`` / catalog
    ``proxmox_template_name``).

    - Candidate clusters: the given one when ``cluster_id`` is set, else all
      enabled clusters from inventory.
    - Candidate nodes:
        * default: enabled nodes whose synced template inventory contains
          ``template_name``.
        * ``shared_storage=True``: all enabled nodes in clusters that have the
          template on at least one node (disks are cluster-visible).
    - Chooses the node with the most free RAM (latest capacity snapshot); nodes
      without a snapshot rank last.

    Raises ``ValueError`` when no candidate node exists (inventory not synced or
    template missing on every node).
    """
    best: Optional[Tuple[float, int, str]] = None
    for cluster in _candidate_clusters(db, cluster_id):
        for node in _placement_candidate_nodes(cluster, template_name, shared_storage):
            score = _node_free_ram_score(node)
            if best is None or score > best[0]:
                best = (score, cluster.id, node.node_name)

    if best is None:
        raise ValueError(
            f"No Proxmox node with synced template '{template_name}' found for auto-placement. "
            "Sync cluster inventory (Admin -> Proxmox) or set placement explicitly."
        )
    return best[1], best[2]
