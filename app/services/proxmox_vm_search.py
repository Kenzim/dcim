"""Search live Proxmox guests for billing/admin link lookup, and resolve
which node currently hosts a given VMID (HA/live-migration cache refresh)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy.orm import Session

from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO


async def _cluster_auth_headers(cluster) -> Dict[str, str]:
    auth_url = f"{cluster.api_url.rstrip('/')}/api2/json/access/ticket"
    async with httpx.AsyncClient(verify=cluster.verify_ssl, timeout=20.0) as client:
        response = await client.post(
            auth_url,
            data={"username": cluster.username, "password": cluster.password},
        )
        response.raise_for_status()
        payload = response.json().get("data") or {}
        ticket = payload.get("ticket")
        if not ticket:
            raise RuntimeError(f"Proxmox auth failed for cluster {cluster.id}")
        return {
            "Cookie": f"PVEAuthCookie={ticket}",
            "CSRFPreventionToken": payload.get("CSRFPreventionToken") or "",
        }


async def _fetch_cluster_qemu_resources(cluster) -> List[Dict[str, Any]]:
    """Live (non-template) qemu guest rows from ``GET /cluster/resources``.

    Each row is the raw Proxmox dict plus normalized ``vmid`` (int) and
    ``node`` (stripped str) keys. Returns ``[]`` when the cluster can't be
    reached (auth failure, network error, etc.) so callers can treat "no
    answer" the same as "not found" rather than raising.
    """
    try:
        headers = await _cluster_auth_headers(cluster)
        url = f"{cluster.api_url.rstrip('/')}/api2/json/cluster/resources"
        async with httpx.AsyncClient(verify=cluster.verify_ssl, timeout=25.0) as client:
            response = await client.get(url, headers=headers, params={"type": "vm"})
            response.raise_for_status()
            rows = response.json().get("data") or []
    except Exception:
        return []

    out: List[Dict[str, Any]] = []
    for row in rows:
        if (row.get("type") or "") != "qemu":
            continue
        if int(row.get("template") or 0) == 1:
            continue
        try:
            vmid = int(row.get("vmid"))
        except (TypeError, ValueError):
            continue
        node = str(row.get("node") or "").strip()
        if not node:
            continue
        out.append({**row, "vmid": vmid, "node": node})
    return out


async def find_node_for_vmid(cluster, vmid: int) -> Optional[str]:
    """Return the Proxmox node currently hosting ``vmid`` in ``cluster``.

    Used to refresh a stale/missing ``service_vm.proxmox_node_name`` cache:
    the VMID is stable across HA/live migration, only the node changes, so
    this is the cluster-wide fallback when the cached node no longer has the
    guest. Returns ``None`` if the VMID isn't found (or the cluster can't be
    reached).
    """
    target = int(vmid)
    for row in await _fetch_cluster_qemu_resources(cluster):
        if row["vmid"] == target:
            return row["node"]
    return None


def _matches_query(q: str, vmid: Optional[int], name: str) -> bool:
    needle = (q or "").strip().lower()
    if not needle:
        return False
    name_l = (name or "").lower()
    vmid_s = str(int(vmid)) if vmid is not None else ""
    if needle in name_l:
        return True
    if vmid_s and (needle == vmid_s or needle in vmid_s or vmid_s.startswith(needle)):
        return True
    return False


async def search_proxmox_vms(
    db: Session,
    q: str,
    *,
    cluster_id: Optional[int] = None,
    limit: int = 25,
) -> List[Dict[str, Any]]:
    """
    Return qemu guests whose VMID or name matches ``q`` (substring / prefix).

    Each item: cluster_id, cluster_name, node_name, vmid, name, status, template.
    """
    needle = (q or "").strip()
    if not needle:
        return []

    clusters = []
    if cluster_id is not None:
        cluster = ProxmoxInventoryDAO.get_cluster(db, cluster_id)
        if cluster and cluster.enabled:
            clusters = [cluster]
    else:
        clusters = [c for c in ProxmoxInventoryDAO.list_clusters(db) if c.enabled]

    out: List[Dict[str, Any]] = []
    for cluster in clusters:
        for row in await _fetch_cluster_qemu_resources(cluster):
            vmid = row["vmid"]
            name = str(row.get("name") or f"vm-{vmid}")
            if not _matches_query(needle, vmid, name):
                continue
            out.append(
                {
                    "cluster_id": cluster.id,
                    "cluster_name": cluster.name,
                    "node_name": row["node"],
                    "vmid": vmid,
                    "name": name,
                    "status": str(row.get("status") or ""),
                    "template": False,
                }
            )
            if len(out) >= limit:
                return out
    return out
