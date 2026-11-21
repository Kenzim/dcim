"""Client MCP tools. Every tool calls the client-portal handler for its capability.

The handlers receive an auth dict for the key's owner, so ownership checks
(foreign ids look like 404), client permission presets and activity logging
are the same as in the client portal. No admin handler is imported here.
"""

from __future__ import annotations

from typing import Any, Literal, Optional
from urllib.parse import parse_qs, urlparse

from fastapi import HTTPException
from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy.orm import Session

from app.api import backup_slots as slot_portal
from app.api import commerce_client as commerce
from app.api import private_networks as network_portal
from app.api import services_client as portal
from app.api import storage_client as storage_portal
from app.mcp.scopes import scope_allows
from app.plugin_host import ui as plugin_ui
from app.api.vm_backup_routes import BackupCreateBody, BackupMutateBody
from app.core.client_mcp_auth import ClientMcpAuthContext
from app.core.config import settings
from app.dao.commerce_dao import BillingAccountDAO
from app.dao.service_dao import ServiceDAO
from app.mcp.client.instance import client_mcp
from app.mcp.client.networks import NETWORK_SCOPE, network_action, networks_view
from app.mcp.client.runtime import run_client_tool
from app.services import client_notifications
from app.services.proxy_fast_path import CREDENTIAL_FIELDS
from app.models.reseller import InvoiceStatus
from app.models.service import ServiceType
from app.models.support_ticket import TicketPriority, TicketStatus
from app.schemas.billing import PowerAction
from app.services.server_lock import ensure_server_unlocked
from app.services.service_resource import service_linked_server

_LIST_FIELDS = (
    "id",
    "name",
    "service_type",
    "status",
    "description",
    "product_code",
    "os_code",
    "primary_ip",
    "power_state",
    "power_available",
)
_DETAIL_FIELDS = _LIST_FIELDS + (
    "guest_state",
    "permissions",
    "kvm_console_available",
    "sol_console_available",
    "console_available",
    "backups_available",
    "virtual_media_available",
)
_INSTALL_FIELDS = ("status", "progress_percent", "os_name", "created_at", "completed_at")

# Minimum key scope per service_action action.
_ACTION_SCOPE = {
    "power_on": "write",
    "power_off": "write",
    "reboot": "write",
    "reset": "write",
    "console": "write",
    "backup_create": "write",
    "backup_retain": "write",
    "set_ssh_keys": "write",
    "backup_delete": "destructive",
    "backup_release": "destructive",
    "backup_restore": "destructive",
    "reinstall": "destructive",
    "run_action": "destructive",
    "fast_path_on": "write",
    "fast_path_off": "write",
    "network": "write",
    "plugin": "write",
}


def _pick(row: dict, fields: tuple[str, ...]) -> dict:
    return {key: row[key] for key in fields if key in row}


def _auth(ctx: ClientMcpAuthContext) -> dict:
    return ctx.as_client_auth()


def _need(action: str, **values: Any) -> None:
    missing = [name for name, value in values.items() if value in (None, "")]
    if missing:
        raise ToolError(f"{action} needs: {', '.join(missing)}")


def _commerce_account(db: Session, ctx: ClientMcpAuthContext):
    if not settings.commerce_retail_enabled:
        raise ToolError("Billing and tickets are not enabled")
    account = BillingAccountDAO.ensure_client_account(db, ctx.user_id)
    db.flush()
    return account


def _owned_service(db: Session, ctx: ClientMcpAuthContext, service_id: int):
    service = ServiceDAO.get_by_id(db, service_id)
    if not service or service.owner_user_id != ctx.user_id:
        raise HTTPException(status_code=404, detail="Service not found")
    return service


async def _plugin_action(
    db: Session, ctx: ClientMcpAuthContext, service_id: int, name: Optional[str], params, confirm: bool
):
    """Run a plugin's client action or client MCP tool, named plugin_id.id."""
    plugin_id, _, item = (name or "").partition(".")
    if not plugin_id or not item:
        raise ToolError("plugin needs name=plugin_id.action_or_tool (see services view=plugins)")
    service = _owned_service(db, ctx, service_id)
    actor = {"type": "client", "user_id": ctx.user_id, "via": "mcp"}
    tool = plugin_ui.client_tool_spec(db, plugin_id, item)
    try:
        if tool is None:
            params = dict(params or {})
            return await plugin_ui.run_action(db, service, plugin_id, item, params=params, confirm=confirm, actor=actor)
        if not scope_allows(ctx.scopes, tool.scope):
            raise ToolError(f"MCP key lacks required scope: {tool.scope}")
        merged = {**dict(params or {}), "service_id": service.id} if tool.service_scoped else dict(params or {})
        return await plugin_ui.run_tool(db, plugin_id, item, params=merged, actor=actor, audience="client")
    except plugin_ui.PluginRequestError as exc:
        raise ToolError(exc.detail) from exc


def _fast_path(overview: dict, service_id: Optional[int]) -> dict:
    """The fast path overview without credentials, narrowed to one service's proxy IPs when service_id is given."""
    endpoints = [
        {k: v for k, v in e.items() if k not in CREDENTIAL_FIELDS}
        for e in overview["endpoints"]
        if service_id is None or e["service_id"] == service_id
    ]
    if service_id is None:
        return {**overview, "endpoints": endpoints}
    wanted = {e["location_id"] for e in endpoints}
    return {
        **overview,
        "locations": [loc for loc in overview["locations"] if loc["location_id"] in wanted],
        "endpoints": endpoints,
    }


def _fast_path_locations(overview: dict, service_id: int) -> list[int]:
    locations = sorted({e["location_id"] for e in _fast_path(overview, service_id)["endpoints"]})
    if not locations:
        raise ToolError("This service has no proxy IPs that can use the fast path")
    return locations


def _console_url(response) -> str:
    location = response.headers.get("location", "")
    error = parse_qs(urlparse(location).query).get("e")
    if error:
        raise ToolError(error[0])
    base = (getattr(settings, "public_app_url", None) or settings.public_base_url or "").rstrip("/")
    return f"{base}{location}" if base else location


@client_mcp.tool()
async def account(
    view: Literal["me", "profile", "activity", "notifications"] = "me",
    limit: int = 25,
    notifications: Optional[dict] = None,
) -> Any:
    """Your account: me (username, email), profile (billing details), activity (recent audit events),
    notifications (your opt-in settings; pass notifications={"categories": {name: bool}, "email_enabled": bool} to change them)."""

    async def work(db: Session, ctx: ClientMcpAuthContext):
        if view == "me":
            return {"id": ctx.user_id, "username": ctx.username, "email": ctx.email}
        if view == "notifications":
            if notifications is None:
                return client_notifications.get_preferences(db, ctx.user_id)
            unknown = set(notifications) - {"categories", "email_enabled"}
            if unknown:
                raise ToolError(f"notifications accepts only categories and email_enabled, not: {', '.join(sorted(unknown))}")
            try:
                return client_notifications.update_preferences(db, ctx.user_id, **notifications)
            except client_notifications.PreferenceError as exc:
                raise ToolError(str(exc)) from exc
        if view == "profile":
            return commerce.get_profile(_commerce_account(db, ctx), db)
        _commerce_account(db, ctx)
        rows = commerce.list_activity(min(max(limit, 1), 100), 0, auth=_auth(ctx), db=db)
        return [_pick(row, ("action", "resource_type", "resource_id", "created_at")) for row in rows]

    scope = "write" if view == "notifications" and notifications is not None else "read"
    return await run_client_tool("account", scope, work, args={"view": view})


@client_mcp.tool()
async def services(
    view: Literal["list", "detail", "backups", "ssh_keys", "actions", "fast_path", "networks", "plugins"] = "list",
    service_id: Optional[int] = None,
    service_type: Optional[str] = None,
) -> Any:
    """Your services. list (service_type filters), detail, backups (a VM's backups and your backup slots, or a backup_slot service's retained backup and the VMs it can be restored onto), ssh_keys (VM), actions (server actions), plugins (plugin panels and tools).
    fast_path: private proxy addresses per location and proxy IP (service_id optional).
    networks (no service_id): your private networks with members, invitations and DHCP settings, networks shared with you with their open shares and effective DHCP settings, and invitations to answer. All others need service_id."""

    async def work(db: Session, ctx: ClientMcpAuthContext):
        auth = _auth(ctx)
        if view == "list":
            rows = await portal.list_my_services(service_type, auth=auth, db=db)
            return [_pick(row.model_dump(), _LIST_FIELDS) for row in rows]
        if view == "fast_path":
            return _fast_path(await network_portal.client_fast_path(auth, db), service_id)
        if view == "networks":
            return await networks_view(db, ctx)
        _need(f"services view={view}", service_id=service_id)
        if view == "detail":
            row = await portal.client_get_service(service_id, auth, db)
            out = _pick(row, _DETAIL_FIELDS)
            if row.get("installation"):
                out["installation"] = _pick(row["installation"], _INSTALL_FIELDS)
            return out
        if view == "backups":
            if _owned_service(db, ctx, service_id).service_type == ServiceType.BACKUP_SLOT:
                return await slot_portal.client_get_slot(service_id, auth, db)
            out = await portal.client_list_vm_backups(service_id, auth, db)
            return {**out, **await slot_portal.client_list_slots(auth, db)}
        if view == "ssh_keys":
            return await portal.client_get_vm_ssh_keys(service_id, auth, db)
        if view == "plugins":
            service = _owned_service(db, ctx, service_id)
            return {**plugin_ui.render(db, service, "client"), "tools": plugin_ui.client_tools(db, service)}
        return await portal.client_list_strategy_actions(service_id, auth, db)

    return await run_client_tool("services", "read", work, args={"view": view, "service_id": service_id})


@client_mcp.tool()
async def service_action(
    action: Literal[
        "network",
        "power_on",
        "power_off",
        "reboot",
        "reset",
        "console",
        "backup_create",
        "backup_retain",
        "set_ssh_keys",
        "backup_delete",
        "backup_release",
        "backup_restore",
        "reinstall",
        "run_action",
        "fast_path_on",
        "fast_path_off",
        "plugin",
    ],
    service_id: Optional[int] = None,
    confirm: bool = False,
    console_type: Optional[Literal["kvm", "sol", "vnc", "serial"]] = None,
    backup_mode: Literal["snapshot", "suspend", "stop"] = "snapshot",
    notes: Optional[str] = None,
    volid: Optional[str] = None,
    storage: Optional[str] = None,
    vm_template_id: Optional[int] = None,
    ssh_public_keys: Optional[str] = None,
    name: Optional[str] = None,
    params: Optional[dict] = None,
    slot_service_id: Optional[int] = None,
    replace: bool = False,
) -> Any:
    """Act on one of your services; confirm=true required except console.

    write scope: power_on, power_off, reboot, reset, console (one-time browser link; console_type kvm/sol for servers, vnc/serial for VMs), backup_create (backup_mode, notes; suspend/stop interrupt the VM), set_ssh_keys (VM; replaces all keys, ssh_public_keys), backup_retain (service_id=VM, volid, slot_service_id; keeps that backup in your backup slot; replace=true swaps out what the slot holds).
    destructive scope: backup_delete and backup_restore (volid, storage; a volid held in a backup slot restores onto service_id, any of your VMs of the same type (cloud-init Linux or macOS), and replaces everything on it; on macOS, Apple account sign-ins may need redoing because they are tied to the VM's serial number), backup_release (service_id=backup_slot; deletes its retained backup), reinstall (VM; optional vm_template_id, ssh_public_keys), run_action (name, params).
    plugin (write): name = plugin_id.id from services view=plugins, params.
    write scope, proxies: fast_path_on and fast_path_off turn the private proxy network on or off in the locations of this proxy's IPs (for all your servers there).
    network (service_id only for attach): name = attach, detach, invite, invitation_cancel, member_remove, leave, invitation_accept or invitation_decline; params = network_id plus attach: service_id (or here), ip_address; detach: attachment_id; invite: invitee (email or username); invitation_cancel: invitation_id; member_remove: member_user_id; invitation_accept and invitation_decline: invitation_id only. Write: attach, invite, invitation_cancel, invitation_accept, invitation_decline. Destructive: detach, member_remove, leave. Members attach and detach their own services; the rest is owner-only.
    network DHCP (owner only, params network_id plus): dhcp_get (read, no confirm); dhcp_set (write; any of enabled, range_start, range_end (both null removes the range), lease_seconds (null = default), gateway, dns_servers, domain, mtu, routes [{destination, gateway}], ntp_servers); dhcp_reservation_add (write; mac, ip, label); dhcp_reservation_update (write; reservation_id, mac, ip, label); dhcp_reservation_remove (destructive; reservation_id).
    """
    if action == "network":
        if name not in NETWORK_SCOPE:
            raise ToolError(f"network needs name, one of: {', '.join(NETWORK_SCOPE)}")
        min_scope = NETWORK_SCOPE[name]
    else:
        if service_id is None:
            raise ToolError(f"{action} needs: service_id")
        min_scope = _ACTION_SCOPE[action]
    needs_confirm = action != "console" and not (action == "backup_create" and backup_mode == "snapshot")
    if action == "network" and min_scope == "read":
        needs_confirm = False

    async def work(db: Session, ctx: ClientMcpAuthContext):
        auth = _auth(ctx)
        if action == "network":
            return await network_action(db, ctx, name, service_id=service_id, params=params)
        if action in ("power_on", "power_off", "reboot", "reset"):
            verb = {"power_on": "on", "power_off": "off"}.get(action, action)
            return await portal.client_power_service(service_id, PowerAction(action=verb), auth, db)
        if action == "console":
            _need("console", console_type=console_type)
            if console_type == "kvm":
                resp = await portal.kvm_popup_redirect_handler(service_id, auth, db)
            elif console_type == "sol":
                resp = await portal.sol_popup_redirect_handler(service_id, auth, db)
            else:
                resp = await portal.vnc_popup_redirect(
                    service_id, "serial" if console_type == "serial" else None, auth=auth, db=db
                )
            return {"url": _console_url(resp), "note": "One-time link; open it in a browser soon."}
        if action == "backup_create":
            body = BackupCreateBody(notes=notes, mode=backup_mode, wait=False)
            return await portal.client_create_vm_backup(service_id, body, auth, db)
        if action == "set_ssh_keys":
            _need("set_ssh_keys", ssh_public_keys=ssh_public_keys)
            body = portal.VmSshKeysBody(ssh_public_keys=ssh_public_keys)
            return await portal.client_put_vm_ssh_keys(service_id, body, auth, db)
        if action == "backup_retain":
            _need(action, volid=volid, slot_service_id=slot_service_id)
            slot_body = slot_portal.RetainBody(source_service_id=service_id, volid=volid, replace=replace)
            return await slot_portal.client_retain(slot_service_id, slot_body, auth, db)
        if action == "backup_release":
            return await slot_portal.client_release(service_id, auth, db)
        if action in ("backup_delete", "backup_restore"):
            _need(action, volid=volid)
            body = BackupMutateBody(volid=volid, storage=storage, wait=False, confirm_data_loss=confirm)
            handler = (
                portal.client_delete_vm_backup if action == "backup_delete" else portal.client_restore_vm_backup
            )
            return await handler(service_id, body, auth, db)
        if action == "reinstall":
            service = _owned_service(db, ctx, service_id)
            ensure_server_unlocked(service_linked_server(db, service))
            body = portal.VmReinstallBody(vm_template_id=vm_template_id, ssh_public_keys=ssh_public_keys)
            return await portal.client_reinstall_vm(service_id, body, auth=auth, db=db)
        if action in ("fast_path_on", "fast_path_off"):
            overview = await network_portal.client_fast_path(auth, db)
            enabled = {loc["location_id"] for loc in overview["locations"] if loc["network_id"] is not None}
            for location_id in _fast_path_locations(overview, service_id):
                if action == "fast_path_on":
                    toggle = network_portal.FastPathToggle(location_id=location_id)
                    await network_portal.client_fast_path_on(toggle, auth, db)
                elif location_id in enabled:
                    await network_portal.client_fast_path_off(location_id, auth, db)
            return _fast_path(await network_portal.client_fast_path(auth, db), service_id)
        if action == "plugin":
            return await _plugin_action(db, ctx, service_id, name, params, confirm)
        _need("run_action", name=name)
        body = portal.ClientStrategyActionBody(params=params or {})
        return await portal.client_run_strategy_action(service_id, name, body, auth, db)

    return await run_client_tool(
        f"service_{action}",
        min_scope,
        work,
        confirm_required=needs_confirm,
        confirm=confirm,
        args={
            "service_id": service_id,
            "action": action,
            "console_type": console_type,
            "volid": volid,
            "name": name,
            **({"network_id": params.get("network_id")} if action == "network" and params else {}),
        },
    )


@client_mcp.tool()
async def billing(
    view: Literal["invoices", "invoice", "orders", "order"] = "invoices",
    id: Optional[int] = None,
    status: Optional[str] = None,
    limit: int = 25,
    offset: int = 0,
) -> Any:
    """Your invoices and orders (read only). invoice and order need id; status filters lists."""

    async def work(db: Session, ctx: ClientMcpAuthContext):
        account_row = _commerce_account(db, ctx)
        page = {"limit": min(max(limit, 1), 100), "offset": max(offset, 0)}
        if view == "invoices":
            flt = InvoiceStatus(status) if status else None
            return commerce.list_invoices(flt, page["limit"], page["offset"], account=account_row, db=db)
        if view == "orders":
            from app.models.commerce_order import OrderStatus

            flt = OrderStatus(status) if status else None
            return commerce.list_orders(flt, page["limit"], page["offset"], account=account_row, db=db)
        _need(f"billing view={view}", id=id)
        if view == "invoice":
            return commerce.get_invoice(id, account_row, db)
        return commerce.get_order(id, account_row, db)

    return await run_client_tool("billing", "read", work, args={"view": view, "invoice_id": id if view == "invoice" else None, "order_id": id if view == "order" else None})


@client_mcp.tool()
async def tickets(
    action: Literal["list", "get", "departments", "open", "reply"] = "list",
    ticket_id: Optional[int] = None,
    department_id: Optional[int] = None,
    subject: Optional[str] = None,
    body: Optional[str] = None,
    priority: Literal["low", "medium", "high", "urgent"] = "medium",
    service_id: Optional[int] = None,
    status: Optional[str] = None,
    limit: int = 25,
) -> Any:
    """Support tickets. Read scope: list (status filters), get, departments. Write scope: open (department_id, subject, body; optional service_id, priority), reply (ticket_id, body)."""
    min_scope = "write" if action in ("open", "reply") else "read"

    async def work(db: Session, ctx: ClientMcpAuthContext):
        account_row = _commerce_account(db, ctx)
        auth = _auth(ctx)
        if action == "departments":
            return commerce.list_ticket_departments(db)
        if action == "list":
            flt = TicketStatus(status) if status else None
            return commerce.list_tickets(flt, min(max(limit, 1), 100), 0, account=account_row, db=db)
        if action == "get":
            _need("tickets get", ticket_id=ticket_id)
            return commerce.get_ticket(ticket_id, account_row, db)
        if action == "open":
            _need("tickets open", department_id=department_id, subject=subject, body=body)
            if service_id is not None:
                _owned_service(db, ctx, service_id)
            payload = commerce.TicketCreate(
                department_id=department_id,
                subject=subject,
                body_text=body,
                priority=TicketPriority(priority),
                service_id=service_id,
            )
            return commerce.create_ticket(payload, auth, account_row, db)
        _need("tickets reply", ticket_id=ticket_id, body=body)
        payload = commerce.TicketMessageCreate(body_text=body)
        return commerce.add_ticket_message(ticket_id, payload, auth, account_row, db)

    return await run_client_tool(
        f"tickets_{action}", min_scope, work, args={"ticket_id": ticket_id, "service_id": service_id}
    )


_STORAGE_SCOPE = {
    "list": "read",
    "view": "read",
    "networks": "read",
    "link": "write",
    "share_create": "write",
    "share_update": "write",
    "share_delete": "destructive",
    "sftp_user_create": "write",
    "sftp_user_update": "write",
    "sftp_user_delete": "destructive",
}


@client_mcp.tool()
async def storage(
    action: Literal[
        "list", "view", "networks", "link", "share_create", "share_update", "share_delete",
        "sftp_user_create", "sftp_user_update", "sftp_user_delete",
    ] = "list",
    service_id: Optional[int] = None,
    network_id: Optional[int] = None,
    share_id: Optional[int] = None,
    name: Optional[str] = None,
    quota_gb: Optional[int] = None,
    clients: Optional[list[str]] = None,
    all_clients: Optional[bool] = None,
    read_only: Optional[bool] = None,
    sftp_access: Optional[Literal["off", "ro", "rw"]] = None,
    user_id: Optional[int] = None,
    public_keys: Optional[list[str]] = None,
    allowed_ips: Optional[list[str]] = None,
    new_password: bool = False,
    enabled: Optional[bool] = None,
    confirm: bool = False,
) -> Any:
    """Your NFS storage. list, view (service_id): status is ready only once the storage is on
    your network and set up; each share has its mount command and fstab line; sftp has the
    SFTP endpoint and users. networks (service_id): your private networks it can be linked to.
    link (service_id; network_id, or none to create a network). share_create (service_id, name,
    quota_gb; open to your whole private network unless you list clients = IPs on it,
    all_clients, read_only, sftp_access off/ro/rw), share_update (service_id, share_id; same fields),
    share_delete (service_id, share_id, confirm=true; the data is destroyed after a grace
    period). sftp_user_create (service_id, name; public_keys and/or new_password=true for a
    generated password shown once; allowed_ips CIDRs), sftp_user_update (service_id, user_id;
    public_keys, new_password, allowed_ips, enabled), sftp_user_delete (service_id, user_id,
    confirm=true).
    """
    async def work(db: Session, ctx: ClientMcpAuthContext):
        auth = _auth(ctx)
        if action == "list":
            return await storage_portal.client_list_storage(auth=auth, db=db)
        _need(f"storage {action}", service_id=service_id)
        if action == "view":
            return await storage_portal.client_get_storage(service_id, auth=auth, db=db)
        if action == "networks":
            return await storage_portal.client_linkable_networks(service_id, auth=auth, db=db)
        if action == "link":
            body = storage_portal.LinkBody(network_id=network_id, network_name=name)
            return await storage_portal.client_link_storage(service_id, body, auth=auth, db=db)
        if action.startswith("sftp_user_"):
            return await _storage_sftp_user(db, auth)
        if action == "share_create":
            _need("storage share_create", name=name, quota_gb=quota_gb)
            body = storage_portal.ShareCreate(
                name=name,
                quota_gb=quota_gb,
                clients=clients or [],
                all_clients=all_clients,
                read_only=bool(read_only),
                sftp_access=sftp_access or "off",
            )
            return await storage_portal.client_create_share(service_id, body, auth=auth, db=db)
        _need(f"storage {action}", share_id=share_id)
        if action == "share_update":
            changes = {
                key: value
                for key, value in {
                    "quota_gb": quota_gb, "clients": clients, "all_clients": all_clients, "read_only": read_only,
                    "sftp_access": sftp_access,
                }.items()
                if value is not None
            }
            if not changes:
                raise ToolError(
                    "storage share_update needs at least one of: quota_gb, clients, all_clients, read_only, sftp_access"
                )
            body = storage_portal.ShareUpdate(**changes)
            return await storage_portal.client_update_share(service_id, share_id, body, auth=auth, db=db)
        return await storage_portal.client_delete_share(service_id, share_id, auth=auth, db=db)

    async def _storage_sftp_user(db: Session, auth: dict):
        if action == "sftp_user_create":
            _need("storage sftp_user_create", name=name)
            body = storage_portal.SftpUserCreate(
                name=name, public_keys=public_keys or [], generate_password=new_password, allowed_ips=allowed_ips or []
            )
            return await storage_portal.client_create_sftp_user(service_id, body, auth=auth, db=db)
        _need(f"storage {action}", user_id=user_id)
        if action == "sftp_user_update":
            changes = {
                key: value
                for key, value in {"public_keys": public_keys, "allowed_ips": allowed_ips, "enabled": enabled}.items()
                if value is not None
            }
            if new_password:
                changes["generate_password"] = True
            if not changes:
                raise ToolError(
                    "storage sftp_user_update needs at least one of: public_keys, new_password, allowed_ips, enabled"
                )
            body = storage_portal.SftpUserUpdate(**changes)
            return await storage_portal.client_update_sftp_user(service_id, user_id, body, auth=auth, db=db)
        return await storage_portal.client_delete_sftp_user(service_id, user_id, auth=auth, db=db)

    return await run_client_tool(
        f"storage_{action}",
        _STORAGE_SCOPE[action],
        work,
        confirm_required=action in ("share_delete", "sftp_user_delete"),
        confirm=confirm,
        args={"service_id": service_id, "share_id": share_id, "network_id": network_id, "user_id": user_id},
    )
