"""NFS storage: storage nodes (the servers running the storage runner) and nfs_storage services with their shares."""

from __future__ import annotations

from typing import List, Literal, Optional

from app.api import storage as storage_api
from app.mcp.instance import mcp
from app.mcp.runtime import run_tool


@mcp.tool()
async def storage_node(
    action: Literal["list", "create", "update", "vlan_ports"] = "list",
    node_id: Optional[int] = None,
    server_id: Optional[int] = None,
    name: Optional[str] = None,
    enabled: Optional[bool] = None,
    vlan_mode: Optional[Literal["switch", "preconfigured"]] = None,
    vlan_port_ids: Optional[List[int]] = None,
) -> dict:
    """Storage nodes. list (or one by node_id, with its tenants and the switch ports that carry
    customer VLANs plus any problem), create (server_id; name, vlan_mode, vlan_port_ids optional;
    the runner API key is returned once), update (name, enabled, vlan_mode, vlan_port_ids),
    vlan_ports (server_id: preview which switch ports would carry VLANs, before making it a node).
    vlan_mode: switch = Rackflow adds each customer VLAN to the node's switch ports through the
    switch plugin; preconfigured = the node already receives every VLAN (pre-trunked ports or a
    VLAN-aware bridge), so no switch calls are made. vlan_port_ids empty = the PXE uplink and its bond.
    """
    if action in ("list", "vlan_ports"):

        async def read(db, ctx):
            if action == "vlan_ports":
                if server_id is None:
                    raise ValueError("vlan_ports needs: server_id")
                return await storage_api.server_vlan_ports(server_id, auth={}, db=db)
            if node_id is not None:
                return await storage_api.get_node(node_id, auth={}, db=db)
            return {"nodes": await storage_api.list_nodes(auth={}, db=db, server_id=server_id)}

        return await run_tool("storage_node", "read", read, args={"action": action, "node_id": node_id})

    async def write(db, ctx):
        if action == "create":
            if server_id is None or node_id is not None:
                raise ValueError("Creating a storage node needs server_id (and no node_id)")
            out = await storage_api.create_node(
                storage_api.NodeCreate(
                    server_id=server_id,
                    name=name,
                    vlan_mode=vlan_mode or "switch",
                    vlan_port_ids=vlan_port_ids or [],
                    enabled=True if enabled is None else enabled,
                ),
                auth={},
                db=db,
            )
            out["note"] = "api_key is shown once; set it as the storage runner's API key."
            return out
        if node_id is None:
            raise ValueError("Updating a storage node needs: node_id")
        changes = {
            k: v
            for k, v in {"name": name, "enabled": enabled, "vlan_mode": vlan_mode, "vlan_port_ids": vlan_port_ids}.items()
            if v is not None
        }
        if not changes:
            raise ValueError("Updating a storage node needs at least one of: name, enabled, vlan_mode, vlan_port_ids")
        return await storage_api.update_node(node_id, storage_api.NodeUpdate(**changes), auth={}, db=db)

    return await run_tool(
        "storage_node",
        "write",
        write,
        args={"action": action, "node_id": node_id, "server_id": server_id, "enabled": enabled, "vlan_mode": vlan_mode},
    )


@mcp.tool()
async def rotate_storage_node_key(node_id: int, confirm: bool = False) -> dict:
    """Issue a new API key for a storage node (returned once). The old key stops working at once."""

    async def work(db, ctx):
        out = await storage_api.rotate_node_key(node_id, auth={}, db=db)
        out["note"] = "Old key revoked; update the storage runner."
        return out

    return await run_tool(
        "rotate_storage_node_key",
        "destructive",
        work,
        destructive=True,
        confirm=confirm,
        args={"node_id": node_id, "confirm": confirm},
    )


@mcp.tool()
async def delete_storage_node(node_id: int, confirm: bool = False) -> dict:
    """Delete a storage node and its key. Refused while any storage (even terminated) is placed on it."""

    async def work(db, ctx):
        await storage_api.delete_node(node_id, auth={}, db=db)
        return {"deleted": True, "node_id": node_id}

    return await run_tool(
        "delete_storage_node",
        "destructive",
        work,
        destructive=True,
        confirm=confirm,
        args={"node_id": node_id, "confirm": confirm},
    )


@mcp.tool()
async def storage(
    action: Literal[
        "list", "view", "create", "link", "resize", "share_create", "share_update", "share_delete"
    ] = "list",
    service_id: Optional[int] = None,
    owner_user_id: Optional[int] = None,
    name: Optional[str] = None,
    size_gb: Optional[int] = None,
    network_id: Optional[int] = None,
    node_id: Optional[int] = None,
    share_id: Optional[int] = None,
    quota_gb: Optional[int] = None,
    clients: Optional[List[str]] = None,
    all_clients: Optional[bool] = None,
    read_only: Optional[bool] = None,
    confirm: bool = False,
) -> dict:
    """NFS storage services. list (optionally by owner_user_id), view (service_id): status is
    ready only when the node's network attachment is applied and the runner reports ready.
    create (owner_user_id, name, size_gb; network_id and node_id optional), link (service_id,
    network_id; node_id optional, default the least loaded node), resize (service_id, size_gb;
    never below the shares' total quota), share_create (service_id, name, quota_gb; clients,
    all_clients, read_only), share_update (service_id, share_id; quota_gb, clients, all_clients,
    read_only), share_delete (service_id, share_id, confirm=true; data is destroyed after the grace period).
    """
    args = {"action": action, "service_id": service_id, "share_id": share_id, "network_id": network_id}
    if action in ("list", "view"):

        async def read(db, ctx):
            if action == "view":
                if service_id is None:
                    raise ValueError("view needs: service_id")
                return await storage_api.admin_get_storage(service_id, auth={}, db=db)
            return {"storage": await storage_api.admin_list_storage(auth={}, db=db, owner_user_id=owner_user_id)}

        return await run_tool("storage", "read", read, args=args)

    async def write(db, ctx):
        if action == "create":
            if owner_user_id is None or not name or size_gb is None:
                raise ValueError("create needs: owner_user_id, name, size_gb")
            body = storage_api.StorageCreate(
                owner_user_id=owner_user_id, name=name, size_gb=size_gb, network_id=network_id, node_id=node_id
            )
            return await storage_api.admin_create_storage(body, auth={}, db=db)
        if service_id is None:
            raise ValueError(f"{action} needs: service_id")
        if action == "link":
            if network_id is None:
                raise ValueError("link needs: network_id")
            body = storage_api.AdminLinkBody(network_id=network_id, node_id=node_id)
            return await storage_api.admin_link_storage(service_id, body, auth={}, db=db)
        if action == "resize":
            if size_gb is None:
                raise ValueError("resize needs: size_gb")
            return await storage_api.admin_resize_storage(
                service_id, storage_api.ResizeBody(size_gb=size_gb), auth={}, db=db
            )
        if action == "share_create":
            if not name or quota_gb is None:
                raise ValueError("share_create needs: name, quota_gb")
            body = storage_api.ShareCreate(
                name=name,
                quota_gb=quota_gb,
                clients=clients or [],
                all_clients=bool(all_clients),
                read_only=bool(read_only),
            )
            return await storage_api.admin_create_share(service_id, body, auth={}, db=db)
        if share_id is None:
            raise ValueError(f"{action} needs: share_id")
        if action == "share_update":
            changes = {
                k: v
                for k, v in {
                    "quota_gb": quota_gb, "clients": clients, "all_clients": all_clients, "read_only": read_only
                }.items()
                if v is not None
            }
            if not changes:
                raise ValueError("share_update needs at least one of: quota_gb, clients, all_clients, read_only")
            body = storage_api.ShareUpdate(**changes)
            return await storage_api.admin_update_share(service_id, share_id, body, auth={}, db=db)
        return await storage_api.admin_delete_share(service_id, share_id, auth={}, db=db)

    deleting = action == "share_delete"
    return await run_tool(
        "storage",
        "destructive" if deleting else "write",
        write,
        destructive=deleting,
        confirm=confirm,
        args=args,
    )
