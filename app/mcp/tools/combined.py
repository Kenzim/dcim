"""Combined MCP tools: one tool per resource or action family.

Each tool validates its arguments and then calls the per-action functions in the
other tool modules, which keep the scope checks, confirm prompts and audit
logging. Those functions are no longer advertised as tools on their own.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from mcp.server.fastmcp.exceptions import ToolError

from app.mcp.instance import mcp
from app.mcp.tools import cabling as cabling_tools
from app.mcp.tools import catalog as catalog_tools
from app.mcp.tools import catalog_admin as catalog_admin_tools
from app.mcp.tools import console as console_tools
from app.mcp.tools import dhcp_tftp as dhcp_tools
from app.mcp.tools import inventory as inventory_tools
from app.mcp.tools import ipam as ipam_tools
from app.mcp.tools import network as network_tools
from app.mcp.tools import pdus as pdu_tools
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
        "kvm_profiles",
        "sol_profiles",
        "switch_plugins",
        "subnet_groups",
        "network_pools",
    ],
    location_id: Optional[int] = None,
) -> dict:
    """List reference data. location_id filters racks. network_pools: private network VLAN pools."""
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
        "kvm_profiles": console_tools.list_kvm_profiles,
        "sol_profiles": console_tools.list_sol_profiles,
        "switch_plugins": network_tools.list_switch_plugins,
        "subnet_groups": catalog_admin_tools.list_subnet_groups,
        "network_pools": network_tools.list_network_pools,
    }
    return await handlers[kind]()


@mcp.tool()
async def save_location(
    location_id: Optional[int] = None,
    name: Optional[str] = None,
    description: Optional[str] = None,
    proxy_port: Optional[int] = None,
    network_pool: Optional[dict[str, Any]] = None,
) -> dict:
    """Create a location (no location_id) or update one.

    proxy_port: port customers use for HTTP proxies on this location's subnets
    (0 clears it, falling back to PROXY_DEFAULT_PORT, 11080 by default).
    network_pool: create or update the location's private network pool; keys vlan_start,
    vlan_end (both needed to create), bridge, enabled, max_networks_per_client,
    max_networks_per_service, reserved_cidrs (replaces), proxy_access_enabled.
    """
    if location_id is None:
        _require("Creating a location", name=name)
        result = await inventory_tools.create_location(name=name, description=description, proxy_port=proxy_port)
    else:
        result = await inventory_tools.update_location(
            location_id, name=name, description=description, proxy_port=proxy_port
        )
    if network_pool is not None:
        result["network_pool"] = await network_tools.save_network_pool(result["id"], network_pool)
    return result


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
    cpu_model: Optional[str] = None,
    ram_gb: Optional[int] = None,
    enabled: Optional[bool] = None,
    locked: Optional[bool] = None,
    lock_reason: Optional[str] = None,
    ipmi_kvm_profile: Optional[str] = None,
    sol_profile: Optional[str] = None,
    virtual_media_profile: Optional[str] = None,
) -> dict:
    """Create a server (no server_id; needs name, server_ip, plugin_name, location_id) or update one.

    plugin_name and plugin_config are set on create only; plugin_config is never returned.
    locked=true blocks any OS install or wipe (lock_reason is shown in the refusal); unlocking is admin-UI only.
    Profiles: ids from lookup(kind="kvm_profiles"|"sol_profiles"|"virtual_media_profiles"); "" clears.
    """
    profiles = {
        k: v
        for k, v in {
            "ipmi_kvm_profile": ipmi_kvm_profile,
            "sol_profile": sol_profile,
            "virtual_media_profile": virtual_media_profile,
        }.items()
        if v is not None
    }
    has_profiles = bool(profiles)
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
        if enabled is not None or cpu_model is not None or locked is not None or has_profiles:
            lock_args = {} if locked is None else {"locked": locked, "lock_reason": lock_reason}
            row = await inventory_tools.update_server(
                row["id"], enabled=enabled, cpu_model=cpu_model, **lock_args, **profiles
            )
        return row
    _reject("Updating a server", plugin_name=plugin_name, plugin_config=plugin_config)
    return await inventory_tools.update_server(
        server_id,
        cpu_count=cpu_count,
        cpu_model=cpu_model,
        ram_gb=ram_gb,
        name=name,
        server_ip=server_ip,
        description=description,
        enabled=enabled,
        rack_id=rack_id,
        rack_unit=rack_unit,
        location_id=location_id,
        locked=locked,
        lock_reason=lock_reason,
        **profiles,
    )


@mcp.tool()
async def server_info(
    server_id: int,
    view: Literal["detail", "activity", "bandwidth", "power_state", "boot_options", "install_tasks", "ports"] = "detail",
    limit: int = 50,
) -> dict:
    """Server detail (with capabilities), activity log, port bandwidth, BMC power state, boot options,
    install tasks, or ports (NICs with the far end of each cable)."""
    if view == "activity":
        return await inventory_tools.get_server_activity(server_id, limit=limit)
    handlers = {
        "detail": inventory_tools.get_server,
        "bandwidth": inventory_tools.get_server_bandwidth,
        "power_state": power_tools.get_server_power_state,
        "boot_options": power_tools.get_server_boot_options,
        "install_tasks": power_tools.list_install_tasks,
        "ports": cabling_tools.list_server_ports,
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
    """Power a server or service: on, off, reset, cycle (all need confirm=true). force applies to servers."""
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
async def save_subnet(
    subnet_id: Optional[int] = None,
    name: Optional[str] = None,
    cidr: Optional[str] = None,
    location_id: Optional[int] = None,
    clear_location: bool = False,
    range_start: Optional[str] = None,
    range_end: Optional[str] = None,
    tags: Optional[list[str]] = None,
    allocation_strategy: Optional[Literal["first_free", "least_recently_used", "spread_subnets"]] = None,
    max_resale_count: Optional[int] = None,
    enabled: Optional[bool] = None,
    proxy_runner_id: Optional[int] = None,
    clear_proxy_runner: bool = False,
) -> dict:
    """Create an IPAM subnet (no subnet_id; needs name, cidr) or update one.

    Create seeds every host address (max 4096), or only range_start..range_end, and refuses CIDRs that
    overlap an existing subnet. cidr, range_start, range_end and tags are set on create only.
    max_resale_count > 1 lets one IP be sold to that many different owners at once.
    Update only: proxy_runner_id pins the subnet to the runner hosting it (proxy fast path; gives it a
    private IP); clear_proxy_runner unpins it (the IP is kept).
    """
    if subnet_id is None:
        _require("Creating a subnet", name=name, cidr=cidr)
        _reject(
            "Creating a subnet",
            clear_location=clear_location or None,
            proxy_runner_id=proxy_runner_id,
            clear_proxy_runner=clear_proxy_runner or None,
        )
        return await ipam_tools.create_subnet(
            name,
            cidr,
            location_id=location_id,
            range_start=range_start,
            range_end=range_end,
            tags=tags,
            allocation_strategy=allocation_strategy or "first_free",
            max_resale_count=1 if max_resale_count is None else max_resale_count,
            enabled=True if enabled is None else enabled,
        )
    _reject("Updating a subnet", cidr=cidr, range_start=range_start, range_end=range_end, tags=tags)
    return await ipam_tools.update_subnet(
        subnet_id,
        name=name,
        enabled=enabled,
        allocation_strategy=allocation_strategy,
        location_id=location_id,
        clear_location=clear_location,
        max_resale_count=max_resale_count,
        proxy_runner_id=proxy_runner_id,
        clear_proxy_runner=clear_proxy_runner,
    )


@mcp.tool()
async def subnet_info(
    subnet_id: int,
    state: Optional[Literal["free", "assigned"]] = None,
    limit: int = 256,
) -> dict:
    """One IPAM subnet with usage counts and its addresses (state, assignment count, service ids), sorted by IP."""
    return await ipam_tools.get_subnet(subnet_id, state=state, limit=limit)


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
    action: Literal["test", "regenerate_ports", "update_port", "detect_vpc", "vlan_dry_run"],
    switch_id: Optional[int] = None,
    plugin_name: Optional[str] = None,
    plugin_config: Optional[dict] = None,
    port_id: Optional[int] = None,
    speed_mbps: Optional[int] = None,
    description: Optional[str] = None,
    server_id: Optional[int] = None,
    vlan_id: Optional[int] = None,
    network_id: Optional[int] = None,
    remove: bool = False,
) -> dict:
    """test: probe a switch (switch_id, or plugin_name + plugin_config before adding; read-only on the device).
    regenerate_ports: rebuild the port list (cabled ports kept). update_port: set a port's speed/description.
    detect_vpc: read every switch's Cisco vPC state (read-only) and record vPC peers (shown as `vpc` on switches).
    vlan_dry_run: exact switch commands for carrying vlan_id (or a private network_id) on server_id's uplink,
    including both vPC peers; remove=true for detaching. Nothing is sent to the switches.
    """
    if action == "test":
        return await network_tools.test_switch_connection(
            switch_id=switch_id, plugin_name=plugin_name, plugin_config=plugin_config
        )
    if action == "detect_vpc":
        return await network_tools.detect_switch_vpc()
    if action == "vlan_dry_run":
        _require("A VLAN dry run", server_id=server_id)
        return await network_tools.switch_vlan_dry_run(server_id, vlan_id=vlan_id, network_id=network_id, remove=remove)
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
    """Service detail (no passwords or proxy credentials), deployment jobs, or backups (backup slots too)."""
    if view == "deployment_jobs":
        return await service_tools.list_deployment_jobs(service_id, limit=limit)
    if view == "backups":
        return await service_tools.list_service_backups(service_id)
    return await service_tools.get_service(service_id)


@mcp.tool()
async def provision(
    kind: Literal["vm", "bare_metal", "http_proxy", "backup_slot"],
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
    size_gb: Optional[int] = None,
) -> dict:
    """Create a service. vm: needs product_code, vm_template_id (optional cluster/node/vmid).
    bare_metal: server_id or server_group_id, optional OS template_id. http_proxy: auto-assigns IPAM addresses.
    backup_slot: owner_user_id, size_gb.
    """
    common = {"owner_user_id": owner_user_id, "description": description}
    if kind == "backup_slot":
        _require("A backup slot", owner_user_id=owner_user_id)
        _reject("A backup slot", vm_template_id=vm_template_id, proxmox_cluster_id=proxmox_cluster_id,
                proxmox_node_name=proxmox_node_name, proxmox_vmid=proxmox_vmid, server_id=server_id,
                server_group_id=server_group_id, template_id=template_id, ip_count=ip_count, subnet_id=subnet_id)
        return await service_tools.provision_backup_slot(name, product_code=product_code, size_gb=size_gb, **common)
    _reject(f"A {kind} service", size_gb=size_gb)
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
    kind: Literal["client", "staff"] = "client",
    invite: bool = False,
    require_2fa: Optional[bool] = None,
) -> dict:
    """Create or update an account (no passwords over MCP).
    No user_id: invite=true emails an invitation (kind client or staff; optional username, permission_set_id for clients; needs email);
    without invite only a passwordless client is created (needs username, email).
    With user_id: set/clear a client's permission_set_id, set require_2fa (client or staff), or change a staff member's email.
    """
    if user_id is None:
        _reject("Creating an account", require_2fa=require_2fa)
        if invite:
            _require("Inviting", email=email)
            return await user_tools.invite_user(email, kind, username=username, permission_set_id=permission_set_id)
        if kind == "staff":
            raise ToolError("Staff accounts are created by invitation: pass invite=true")
        _require("Creating a client", username=username, email=email)
        return await user_tools.create_client(username, email, permission_set_id=permission_set_id)
    _reject("Updating an account", username=username, invite=invite or None)
    result: Optional[dict] = None
    if kind == "staff":
        _reject("Updating a staff member", permission_set_id=permission_set_id)
        if email is None and require_2fa is None:
            raise ToolError("Updating a staff member needs: email or require_2fa")
        if email is not None:
            result = await user_tools.update_staff(user_id, email)
    else:
        _reject("Updating a client", email=email)
        if permission_set_id is not None or require_2fa is None:
            result = await user_tools.set_client_permission_set(user_id, permission_set_id=permission_set_id)
    if require_2fa is not None:
        result = await user_tools.set_require_2fa(user_id, kind, require_2fa)
    return result


@mcp.tool()
async def invite_action(user_id: int, action: Literal["resend", "revoke"]) -> dict:
    """resend: email a fresh invitation link (older ones stop working). revoke: kill the link but keep the account."""
    return await user_tools.invite_action(user_id, action)


@mcp.tool()
async def pdus(
    location_id: Optional[int] = None,
    rack_id: Optional[int] = None,
    pdu_id: Optional[int] = None,
    history_hours: Optional[int] = None,
) -> Any:
    """List PDUs (filter by location_id or rack_id), or give pdu_id for one PDU with its inlets and outlets. Secrets are masked.
    history_hours (up to about a year) returns bucketed power history instead: of the PDU (pdu_id, with inlets, banks and rated current), of a rack (rack_id), of a location (location_id), or of all PDUs. Includes energy in kWh.
    """
    if pdu_id is not None:
        _reject("Viewing one PDU", location_id=location_id, rack_id=rack_id)
    if history_hours is not None and rack_id is not None:
        _reject("History of a rack", location_id=location_id)
    return await pdu_tools.pdus(location_id=location_id, rack_id=rack_id, pdu_id=pdu_id, history_hours=history_hours)


@mcp.tool()
async def save_pdu(
    pdu_id: Optional[int] = None,
    name: Optional[str] = None,
    location_id: Optional[int] = None,
    plugin_name: Optional[str] = None,
    plugin_config: Optional[dict] = None,
    description: Optional[str] = None,
    rack_id: Optional[int] = None,
    rack_unit: Optional[int] = None,
    rack_units: Optional[int] = None,
    enabled: Optional[bool] = None,
    inlet_count: Optional[int] = None,
    outlet_count: Optional[int] = None,
    outlet_metering: Optional[bool] = None,
    outlet_switching: Optional[bool] = None,
    outlet_control_enabled: Optional[bool] = None,
    model: Optional[str] = None,
) -> dict:
    """Create a PDU (no pdu_id; needs name, location_id and plugin_name), which generates its inlets and outlets, or update one (pdu_id). outlet_control_enabled starts false. Masked secrets in plugin_config keep their stored values."""
    if pdu_id is None:
        _require("Creating a PDU", name=name, location_id=location_id, plugin_name=plugin_name)
    return await pdu_tools.save_pdu(
        pdu_id=pdu_id,
        name=name,
        location_id=location_id,
        plugin_name=plugin_name,
        plugin_config=plugin_config,
        description=description,
        rack_id=rack_id,
        rack_unit=rack_unit,
        rack_units=rack_units,
        enabled=enabled,
        inlet_count=inlet_count,
        outlet_count=outlet_count,
        outlet_metering=outlet_metering,
        outlet_switching=outlet_switching,
        outlet_control_enabled=outlet_control_enabled,
        model=model,
    )


@mcp.tool()
async def pdu_action(
    pdu_id: int,
    action: Literal["test", "regenerate_ports", "poll", "link_inlet", "update_outlet", "link_outlet", "delete"],
    inlet_id: Optional[int] = None,
    power_feed_id: Optional[int] = None,
    outlet_id: Optional[int] = None,
    server_power_input_id: Optional[int] = None,
    name: Optional[str] = None,
    bank: Optional[str] = None,
    description: Optional[str] = None,
    locked: Optional[bool] = None,
    inlet_count: Optional[int] = None,
    outlet_count: Optional[int] = None,
    confirm: bool = False,
) -> Any:
    """test: probe the PDU with its stored settings. regenerate_ports: rebuild inlets and outlets (inlet_count / outlet_count override).
    poll: read the PDU now. link_inlet: link inlet_id to power_feed_id (omit power_feed_id to unlink). update_outlet: rename outlet_id or set its bank, description or locked.
    link_outlet: connect outlet_id to server_power_input_id (omit it to unlink). delete: remove the PDU (needs confirm=true). Switching outlets is outlet_power.
    """
    context = f"PDU {action}"
    if action == "update_outlet":
        _require(context, outlet_id=outlet_id)
        if name is None and bank is None and description is None and locked is None:
            raise ToolError(f"{context} needs: name, bank, description or locked")
        _reject(context, inlet_id=inlet_id, power_feed_id=power_feed_id, server_power_input_id=server_power_input_id, inlet_count=inlet_count, outlet_count=outlet_count)
    elif action == "link_outlet":
        _require(context, outlet_id=outlet_id)
        _reject(context, inlet_id=inlet_id, power_feed_id=power_feed_id, name=name, bank=bank, description=description, locked=locked, inlet_count=inlet_count, outlet_count=outlet_count)
    elif action == "link_inlet":
        _require(context, inlet_id=inlet_id)
        _reject(context, outlet_id=outlet_id, server_power_input_id=server_power_input_id, name=name, bank=bank, description=description, locked=locked, inlet_count=inlet_count, outlet_count=outlet_count)
    elif action == "regenerate_ports":
        _reject(context, inlet_id=inlet_id, power_feed_id=power_feed_id, outlet_id=outlet_id, server_power_input_id=server_power_input_id, name=name, bank=bank, description=description, locked=locked)
    else:
        _reject(context, inlet_id=inlet_id, power_feed_id=power_feed_id, outlet_id=outlet_id, server_power_input_id=server_power_input_id, name=name, bank=bank, description=description, locked=locked, inlet_count=inlet_count, outlet_count=outlet_count)
    return await pdu_tools.pdu_action(
        pdu_id,
        action,
        inlet_id=inlet_id,
        power_feed_id=power_feed_id,
        outlet_id=outlet_id,
        server_power_input_id=server_power_input_id,
        name=name,
        bank=bank,
        description=description,
        locked=locked,
        inlet_count=inlet_count,
        outlet_count=outlet_count,
        confirm=confirm,
    )


@mcp.tool()
async def outlet_power(
    action: Literal["on", "off", "cycle"],
    confirm: bool = False,
    pdu_id: Optional[int] = None,
    outlet_id: Optional[int] = None,
    server_id: Optional[int] = None,
    power_input_id: Optional[int] = None,
) -> Any:
    """Switch a PDU outlet on, off or cycle it (needs confirm=true). Give pdu_id and outlet_id, or server_id and power_input_id. The PDU must support switching with outlet control enabled, and the outlet must not be locked."""
    if server_id is not None:
        _require("Switching by server", power_input_id=power_input_id)
        _reject("Switching by server", pdu_id=pdu_id, outlet_id=outlet_id)
    else:
        _require("Switching by outlet", pdu_id=pdu_id, outlet_id=outlet_id)
        _reject("Switching by outlet", power_input_id=power_input_id)
    return await pdu_tools.outlet_power(
        action,
        confirm=confirm,
        pdu_id=pdu_id,
        outlet_id=outlet_id,
        server_id=server_id,
        power_input_id=power_input_id,
    )


@mcp.tool()
async def power_feeds(
    action: Literal["list", "create", "update", "delete"],
    rack_id: Optional[int] = None,
    feed_id: Optional[int] = None,
    name: Optional[str] = None,
    voltage: Optional[int] = None,
    amperage: Optional[int] = None,
    phase: Optional[str] = None,
    max_utilisation_percent: Optional[int] = None,
    description: Optional[str] = None,
    confirm: bool = False,
    history_hours: Optional[int] = None,
) -> Any:
    """list: the feeds in a rack (rack_id), or one feed (feed_id). create: needs rack_id and name. update: needs feed_id. delete: needs feed_id and confirm=true.
    voltage, amperage, phase, max_utilisation_percent and description apply to create and update.
    history_hours (list with feed_id only): adds bucketed power history of the feed, with its limits and energy in kWh.
    """
    if history_hours is not None and (action != "list" or feed_id is None):
        raise ToolError("history_hours needs action=list and feed_id")
    if action == "list":
        _reject("Listing power feeds", name=name, voltage=voltage, amperage=amperage, phase=phase, max_utilisation_percent=max_utilisation_percent, description=description)
        return await pdu_tools.power_feeds(rack_id=rack_id, feed_id=feed_id, history_hours=history_hours)
    if action == "delete":
        _require("Deleting a power feed", feed_id=feed_id)
        _reject("Deleting a power feed", rack_id=rack_id, name=name, voltage=voltage, amperage=amperage, phase=phase, max_utilisation_percent=max_utilisation_percent, description=description)
        return await pdu_tools.delete_power_feed(feed_id, confirm=confirm)
    if action == "create":
        _require("Creating a power feed", rack_id=rack_id, name=name)
        _reject("Creating a power feed", feed_id=feed_id)
    else:
        _require("Updating a power feed", feed_id=feed_id)
        _reject("Updating a power feed", rack_id=rack_id)
    return await pdu_tools.save_power_feed(
        feed_id=feed_id,
        rack_id=rack_id,
        name=name,
        voltage=voltage,
        amperage=amperage,
        phase=phase,
        max_utilisation_percent=max_utilisation_percent,
        description=description,
    )


@mcp.tool()
async def rack_power_summary(rack_id: int, history_hours: Optional[int] = None) -> dict:
    """Power summary for a rack: its power feeds with their aggregated readings. history_hours adds bucketed power history and energy in kWh."""
    return await pdu_tools.rack_power(rack_id, history_hours=history_hours)


@mcp.tool()
async def server_power_summary(server_id: int, history_hours: Optional[int] = None) -> dict:
    """Power summary for a server: its power inputs, the PDU outlets feeding them and their readings.
    history_hours adds bucketed power history and energy in kWh from the linked metered outlets (it says when that cannot be known)."""
    return await pdu_tools.server_power(server_id, history_hours=history_hours)


@mcp.tool()
async def server_power_inputs(
    server_id: int,
    action: Literal["add", "update", "link", "remove"],
    input_id: Optional[int] = None,
    name: Optional[str] = None,
    description: Optional[str] = None,
    pdu_outlet_id: Optional[int] = None,
    confirm: bool = False,
) -> Any:
    """add: a new power input (needs name; pdu_outlet_id optional). update: rename or describe input_id. link: connect input_id to pdu_outlet_id (omit pdu_outlet_id to unplug).
    remove: delete input_id (needs confirm=true).
    """
    context = f"Power input {action}"
    if action == "add":
        _require(context, name=name)
        _reject(context, input_id=input_id)
    elif action == "update":
        _require(context, input_id=input_id)
        if name is None and description is None:
            raise ToolError(f"{context} needs: name or description")
        _reject(context, pdu_outlet_id=pdu_outlet_id)
    elif action == "link":
        _require(context, input_id=input_id)
        _reject(context, name=name, description=description)
    else:
        _require(context, input_id=input_id)
        _reject(context, name=name, description=description, pdu_outlet_id=pdu_outlet_id)
    return await pdu_tools.server_power_inputs(
        server_id,
        action,
        input_id=input_id,
        name=name,
        description=description,
        pdu_outlet_id=pdu_outlet_id,
        confirm=confirm,
    )
