"""Combined MCP tools: one tool per resource or action family.

Each tool validates its arguments and then calls the per-action functions in the
other tool modules, which keep the scope checks, confirm prompts and audit
logging. Those functions are no longer advertised as tools on their own.
"""

from __future__ import annotations

from typing import Literal, Optional

from mcp.server.fastmcp.exceptions import ToolError

from app.mcp.instance import mcp
from app.mcp.tools import catalog as catalog_tools
from app.mcp.tools import console as console_tools
from app.mcp.tools import dhcp_tftp as dhcp_tools
from app.mcp.tools import inventory as inventory_tools
from app.mcp.tools import ipam as ipam_tools
from app.mcp.tools import network as network_tools
from app.mcp.tools import power as power_tools
from app.mcp.tools import proxmox as proxmox_tools
from app.mcp.tools import services as service_tools
from app.mcp.tools import users as user_tools


def _require(context: str, **values) -> None:
    missing = [name for name, value in values.items() if value is None]
    if missing:
        raise ToolError(f"{context} needs: {', '.join(missing)}")


def _reject(context: str, **values) -> None:
    given = [name for name, value in values.items() if value is not None]
    if given:
        raise ToolError(f"{context} does not accept: {', '.join(given)}")


@mcp.tool()
async def lookup(
    kind: Literal[
        "locations",
        "racks",
        "server_groups",
        "subnets",
        "products",
        "families",
        "vm_templates",
        "proxmox_clusters",
        "virtual_media_profiles",
        "switch_plugins",
    ],
    location_id: Optional[int] = None,
) -> dict:
    """List reference data. location_id filters racks."""
    if kind == "racks":
        return await inventory_tools.list_racks(location_id=location_id)
    _reject(f"lookup {kind}", location_id=location_id)
    handlers = {
        "locations": inventory_tools.list_locations,
        "server_groups": inventory_tools.list_server_groups,
        "subnets": ipam_tools.list_subnets,
        "products": catalog_tools.list_products,
        "families": catalog_tools.list_families,
        "vm_templates": catalog_tools.list_vm_templates,
        "proxmox_clusters": proxmox_tools.list_proxmox_clusters,
        "virtual_media_profiles": console_tools.list_virtual_media_profiles,
        "switch_plugins": network_tools.list_switch_plugins,
    }
    return await handlers[kind]()


@mcp.tool()
async def save_location(
    location_id: Optional[int] = None, name: Optional[str] = None, description: Optional[str] = None
) -> dict:
    """Create a location (no location_id) or update one."""
    if location_id is None:
        _require("Creating a location", name=name)
        return await inventory_tools.create_location(name=name, description=description)
    return await inventory_tools.update_location(location_id, name=name, description=description)


@mcp.tool()
async def save_rack(
    rack_id: Optional[int] = None,
    location_id: Optional[int] = None,
    name: Optional[str] = None,
    units: Optional[int] = None,
    description: Optional[str] = None,
    row: Optional[int] = None,
    row_position: Optional[int] = None,
) -> dict:
    """Create a rack (no rack_id; needs location_id, name; units default 42) or update one."""
    if rack_id is None:
        _require("Creating a rack", location_id=location_id, name=name)
        return await inventory_tools.create_rack(
            location_id, name, units=units or 42, description=description, row=row, row_position=row_position
        )
    _reject("Updating a rack", location_id=location_id)
    return await inventory_tools.update_rack(
        rack_id, name=name, units=units, description=description, row=row, row_position=row_position
    )


@mcp.tool()
async def save_server(
    server_id: Optional[int] = None,
    name: Optional[str] = None,
    server_ip: Optional[str] = None,
    location_id: Optional[int] = None,
    plugin_name: Optional[str] = None,
    plugin_config: Optional[dict] = None,
    description: Optional[str] = None,
    rack_id: Optional[int] = None,
    rack_unit: Optional[int] = None,
    cpu_count: Optional[int] = None,
    ram_gb: Optional[int] = None,
    enabled: Optional[bool] = None,
) -> dict:
    """Create a server (no server_id; needs name, server_ip, plugin_name, location_id) or update one.

    plugin_name, plugin_config, cpu_count and ram_gb are set on create only; plugin_config is never returned.
    """
    if server_id is None:
        _require("Creating a server", name=name, server_ip=server_ip, plugin_name=plugin_name, location_id=location_id)
        row = await inventory_tools.create_server(
            name,
            server_ip,
            plugin_name,
            location_id,
            plugin_config=plugin_config,
            description=description,
            rack_id=rack_id,
            rack_unit=rack_unit,
            cpu_count=cpu_count or 1,
            ram_gb=ram_gb,
        )
        if enabled is not None:
            row = await inventory_tools.update_server(row["id"], enabled=enabled)
        return row
    _reject(
        "Updating a server", plugin_name=plugin_name, plugin_config=plugin_config, cpu_count=cpu_count, ram_gb=ram_gb
    )
    return await inventory_tools.update_server(
        server_id,
        name=name,
        server_ip=server_ip,
        description=description,
        enabled=enabled,
        rack_id=rack_id,
        rack_unit=rack_unit,
        location_id=location_id,
    )


@mcp.tool()
async def server_info(
    server_id: int,
    view: Literal["detail", "activity", "bandwidth", "power_state", "boot_options", "install_tasks"] = "detail",
    limit: int = 50,
) -> dict:
    """Server detail (with capabilities), activity log, port bandwidth, BMC power state, boot options or install tasks."""
    if view == "activity":
        return await inventory_tools.get_server_activity(server_id, limit=limit)
    handlers = {
        "detail": inventory_tools.get_server,
        "bandwidth": inventory_tools.get_server_bandwidth,
        "power_state": power_tools.get_server_power_state,
        "boot_options": power_tools.get_server_boot_options,
        "install_tasks": power_tools.list_install_tasks,
    }
    return await handlers[view](server_id)


@mcp.tool()
async def power(
    action: str,
    server_id: Optional[int] = None,
    service_id: Optional[int] = None,
    force: bool = False,
    confirm: bool = False,
) -> dict:
    """Power a server or service: on, off, reset (off/reset need confirm=true). force applies to servers."""
    if (server_id is None) == (service_id is None):
        raise ToolError("Pass exactly one of server_id or service_id")
    if server_id is not None:
        return await power_tools.server_power(server_id, action, force=force, confirm=confirm)
    return await service_tools.service_power(service_id, action, confirm=confirm)


@mcp.tool()
async def console_ticket(
    kind: Literal["ipmi", "vnc", "sol"],
    server_id: Optional[int] = None,
    service_id: Optional[int] = None,
    console_type: Optional[str] = None,
    confirm: bool = False,
) -> dict:
    """Mint a short-lived launch URL: ipmi web proxy, VM console (vnc, needs service_id) or serial-over-LAN."""
    if kind == "vnc":
        _require("A VM console ticket", service_id=service_id)
        return await console_tools.mint_vnc_ticket(service_id, console_type=console_type, confirm=confirm)
    _reject(f"A {kind} ticket", console_type=console_type)
    mint = console_tools.mint_ipmi_ticket if kind == "ipmi" else console_tools.mint_sol_ticket
    return await mint(server_id=server_id, service_id=service_id, confirm=confirm)


@mcp.tool()
async def virtual_media(
    action: Literal["status", "insert", "eject"],
    server_id: Optional[int] = None,
    service_id: Optional[int] = None,
    filename: Optional[str] = None,
    boot_once: bool = False,
    confirm: bool = False,
) -> dict:
    """BMC virtual CD: status (with ISO catalog), insert an ISO from the catalog (no reboot), or eject."""
    if action == "insert":
        _require("Inserting media", filename=filename)
        return await console_tools.insert_virtual_media(
            filename, server_id=server_id, service_id=service_id, boot_once=boot_once, confirm=confirm
        )
    _reject(f"Virtual media {action}", filename=filename)
    if action == "eject":
        return await console_tools.eject_virtual_media(server_id=server_id, service_id=service_id, confirm=confirm)
    return await console_tools.get_virtual_media(server_id=server_id, service_id=service_id)


@mcp.tool()
async def dhcp_tftp(
    location_id: int,
    kind: Literal["dhcp", "tftp"] = "dhcp",
    view: Literal["status", "settings", "logs"] = "status",
    limit: int = 100,
) -> dict:
    """DHCP or TFTP runner status, settings or recent logs for a location."""
    if view == "logs":
        return await dhcp_tools.dhcp_tftp_logs(location_id, kind=kind, limit=limit)
    handler = dhcp_tools.dhcp_tftp_status if view == "status" else dhcp_tools.dhcp_tftp_settings
    return await handler(location_id, kind=kind)


@mcp.tool()
async def list_ips(
    pool: Literal["assignments", "vm"] = "assignments",
    service_id: Optional[int] = None,
    assigned: Optional[bool] = None,
    cluster_id: Optional[int] = None,
    q: Optional[str] = None,
) -> dict:
    """IPAM assignments (optionally for one service; credentials redacted) or the VM IP pool."""
    if pool == "assignments":
        _reject("Listing assignments", assigned=assigned, cluster_id=cluster_id, q=q)
        return await ipam_tools.list_ip_assignments(service_id=service_id)
    _reject("Listing the VM pool", service_id=service_id)
    return await ipam_tools.list_vm_ips(assigned=assigned, cluster_id=cluster_id, q=q)


@mcp.tool()
async def ip_assignment(
    action: Literal["assign", "release"],
    service_id: Optional[int] = None,
    subnet_id: Optional[int] = None,
    strategy: Optional[str] = None,
    assignment_id: Optional[int] = None,
) -> dict:
    """Assign an IPAM address to an HTTP-proxy service (credentials returned once), or release an assignment."""
    if action == "assign":
        _require("Assigning an IP", service_id=service_id)
        return await ipam_tools.assign_ip(service_id, subnet_id=subnet_id, strategy=strategy)
    _require("Releasing an IP", assignment_id=assignment_id)
    return await ipam_tools.release_ip(assignment_id)


@mcp.tool()
async def save_switch(
    switch_id: Optional[int] = None,
    name: Optional[str] = None,
    location_id: Optional[int] = None,
    plugin_name: Optional[str] = None,
    plugin_config: Optional[dict] = None,
    description: Optional[str] = None,
    enabled: Optional[bool] = None,
    rack_id: Optional[int] = None,
    rack_unit: Optional[int] = None,
    rack_units: Optional[int] = None,
    model: Optional[str] = None,
    serial_number: Optional[str] = None,
    firmware_version: Optional[str] = None,
    port_count: Optional[int] = None,
) -> dict:
    """Create a switch (no switch_id; needs name, location_id, plugin_name, plugin_config) or update one.

    On update plugin_config merges into the stored one: null removes a key, "***" keeps it. Passwords are masked.
    """
    if switch_id is None:
        _require(
            "Creating a switch", name=name, location_id=location_id, plugin_name=plugin_name, plugin_config=plugin_config
        )
        row = await network_tools.create_switch(
            name,
            location_id,
            plugin_name,
            plugin_config,
            description=description,
            rack_id=rack_id,
            rack_unit=rack_unit,
            model=model,
            serial_number=serial_number,
            port_count=port_count,
            enabled=True if enabled is None else enabled,
        )
        if rack_units is not None or firmware_version is not None:
            row = await network_tools.update_switch(
                row["id"], rack_units=rack_units, firmware_version=firmware_version
            )
        return row
    return await network_tools.update_switch(
        switch_id,
        name=name,
        description=description,
        enabled=enabled,
        location_id=location_id,
        rack_id=rack_id,
        rack_unit=rack_unit,
        rack_units=rack_units,
        model=model,
        serial_number=serial_number,
        firmware_version=firmware_version,
        port_count=port_count,
        plugin_name=plugin_name,
        plugin_config=plugin_config,
    )


@mcp.tool()
async def switch_action(
    action: Literal["test", "regenerate_ports", "update_port"],
    switch_id: Optional[int] = None,
    plugin_name: Optional[str] = None,
    plugin_config: Optional[dict] = None,
    port_id: Optional[int] = None,
    speed_mbps: Optional[int] = None,
    description: Optional[str] = None,
) -> dict:
    """test: probe a switch (switch_id, or plugin_name + plugin_config before adding; read-only on the device).
    regenerate_ports: rebuild the port list (cabled ports kept). update_port: set a port's speed/description.
    """
    if action == "test":
        return await network_tools.test_switch_connection(
            switch_id=switch_id, plugin_name=plugin_name, plugin_config=plugin_config
        )
    _require(f"Switch {action}", switch_id=switch_id)
    _reject(f"Switch {action}", plugin_name=plugin_name, plugin_config=plugin_config)
    if action == "regenerate_ports":
        return await network_tools.regenerate_switch_ports(switch_id)
    _require("Updating a port", port_id=port_id)
    return await network_tools.update_switch_port(
        switch_id, port_id, speed_mbps=speed_mbps, description=description
    )


@mcp.tool()
async def proxmox_inventory(cluster_id: int, sync: bool = False) -> dict:
    """Nodes, storages and templates for a Proxmox cluster; sync=true refreshes from the live API first."""
    if not sync:
        return await proxmox_tools.get_proxmox_inventory(cluster_id)
    result = await proxmox_tools.sync_proxmox(cluster_id)
    return {"sync": result, "inventory": await proxmox_tools.get_proxmox_inventory(cluster_id)}


@mcp.tool()
async def service_info(
    service_id: int,
    view: Literal["detail", "backups", "deployment_jobs"] = "detail",
    limit: int = 20,
) -> dict:
    """Service detail (no passwords or proxy credentials), VM backups with running backup jobs, or deployment jobs."""
    if view == "deployment_jobs":
        return await service_tools.list_deployment_jobs(service_id, limit=limit)
    if view == "backups":
        return await service_tools.list_service_backups(service_id)
    return await service_tools.get_service(service_id)


@mcp.tool()
async def provision(
    kind: Literal["vm", "bare_metal", "http_proxy"],
    name: str,
    product_code: Optional[str] = None,
    owner_user_id: Optional[int] = None,
    description: Optional[str] = None,
    vm_template_id: Optional[int] = None,
    proxmox_cluster_id: Optional[int] = None,
    proxmox_node_name: Optional[str] = None,
    proxmox_vmid: Optional[int] = None,
    auto_provision: bool = True,
    server_id: Optional[int] = None,
    server_group_id: Optional[int] = None,
    template_id: Optional[str] = None,
    ip_count: Optional[int] = None,
    subnet_id: Optional[int] = None,
) -> dict:
    """Create a service. vm: needs product_code, vm_template_id (optional cluster/node/vmid).
    bare_metal: server_id or server_group_id, optional OS template_id. http_proxy: auto-assigns IPAM addresses.
    """
    common = {"owner_user_id": owner_user_id, "description": description}
    if kind == "vm":
        _require("A VM", product_code=product_code, vm_template_id=vm_template_id)
        _reject("A VM", server_id=server_id, server_group_id=server_group_id, template_id=template_id,
                ip_count=ip_count, subnet_id=subnet_id)
        return await service_tools.provision_vm(
            name,
            product_code,
            vm_template_id,
            proxmox_cluster_id=proxmox_cluster_id,
            proxmox_node_name=proxmox_node_name,
            proxmox_vmid=proxmox_vmid,
            auto_provision=auto_provision,
            **common,
        )
    _reject(f"A {kind} service", vm_template_id=vm_template_id, proxmox_cluster_id=proxmox_cluster_id,
            proxmox_node_name=proxmox_node_name, proxmox_vmid=proxmox_vmid)
    if kind == "bare_metal":
        _reject("A bare-metal service", ip_count=ip_count, subnet_id=subnet_id)
        return await service_tools.provision_bare_metal(
            name,
            server_id=server_id,
            server_group_id=server_group_id,
            product_code=product_code,
            template_id=template_id,
            **common,
        )
    _reject("An HTTP-proxy service", server_id=server_id, server_group_id=server_group_id, template_id=template_id)
    return await service_tools.provision_http_proxy(
        name, product_code=product_code, ip_count=ip_count, subnet_id=subnet_id, **common
    )


@mcp.tool()
async def save_client(
    user_id: Optional[int] = None,
    username: Optional[str] = None,
    email: Optional[str] = None,
    permission_set_id: Optional[int] = None,
) -> dict:
    """Create a client without a password (no user_id; needs username, email), or set/clear a client's permission preset."""
    if user_id is None:
        _require("Creating a client", username=username, email=email)
        return await user_tools.create_client(username, email, permission_set_id=permission_set_id)
    _reject("Updating a client", username=username, email=email)
    return await user_tools.set_client_permission_set(user_id, permission_set_id=permission_set_id)
