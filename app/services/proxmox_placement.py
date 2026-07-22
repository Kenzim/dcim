"""Helpers for Proxmox cluster API URL → plugin_config and service placement."""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Protocol, Tuple
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO


class _ProxmoxClusterLike(Protocol):
    api_url: str
    username: str
    password: str
    verify_ssl: bool


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


def auto_place_vm(
    db: Session,
    *,
    template_name: str,
    cluster_id: Optional[int] = None,
) -> Tuple[int, str]:
    """
    Pick a Proxmox (cluster_id, node_name) for a VM whose catalog template is
    ``template_name`` (matches synced ``ProxmoxTemplate.name`` / catalog
    ``proxmox_template_name``).

    - Candidate clusters: the given one when ``cluster_id`` is set, else all
      enabled clusters from inventory.
    - Candidate nodes: enabled nodes whose synced template inventory contains
      ``template_name``.
    - Chooses the node with the most free RAM (latest capacity snapshot); nodes
      without a snapshot rank last.

    Raises ``ValueError`` when no candidate node exists (inventory not synced or
    template missing on every node).
    """
    best: Optional[Tuple[float, int, str]] = None
    for cluster in _candidate_clusters(db, cluster_id):
        for node in cluster.nodes or []:
            if not _node_has_template(node, template_name):
                continue
            score = _node_free_ram_score(node)
            if best is None or score > best[0]:
                best = (score, cluster.id, node.node_name)

    if best is None:
        raise ValueError(
            f"No Proxmox node with synced template '{template_name}' found for auto-placement. "
            "Sync cluster inventory (Admin -> Proxmox) or set placement explicitly."
        )
    return best[1], best[2]
