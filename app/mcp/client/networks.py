"""Client MCP: private networks, including networks shared with other clients.

Every call goes through the same portal handlers as the client portal, so a
network that is not yours, or not shared with you, is "not found" and owner-only
actions refuse members exactly as the portal does.
"""

from __future__ import annotations

from typing import Any, Optional

from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.api import private_network_sharing as shared_portal
from app.api import private_networks as network_portal
from app.core.client_mcp_auth import ClientMcpAuthContext
from app.dao.private_network_dao import PrivateNetworkDAO
from app.models.private_network import NetworkKind
from app.services import private_network_dhcp

# Sub-action of service_action(action="network") and the minimum key scope it needs.
NETWORK_SCOPE = {
    "attach": "write",
    "invite": "write",
    "invitation_cancel": "write",
    "invitation_accept": "write",
    "invitation_decline": "write",
    "detach": "destructive",
    "member_remove": "destructive",
    "leave": "destructive",
    "dhcp_get": "read",
    "dhcp_set": "write",
    "dhcp_reservation_add": "write",
    "dhcp_reservation_update": "write",
    "dhcp_reservation_remove": "destructive",
}


def _need(sub: str, **values: Any) -> None:
    missing = [name for name, value in values.items() if value in (None, "")]
    if missing:
        raise ToolError(f"network {sub} needs: {', '.join(missing)}")


async def networks_view(db: Session, ctx: ClientMcpAuthContext) -> dict:
    """Networks you own (with members, invitations and DHCP), networks shared with you (with open
    shares and the effective DHCP settings) and invitations to answer."""
    auth = ctx.as_client_auth()
    shared = []
    for network in await shared_portal.shared_with_me(auth, db):
        row = PrivateNetworkDAO.get_network(db, network.id)
        storage = shared_portal.shared_storage(db, row)
        dhcp = private_network_dhcp.member_view(db, row)
        shared.append({**network.model_dump(), "storage": storage.model_dump(), "dhcp": dhcp})
    owned = []
    for network in await network_portal.client_list_networks(auth, db):
        dhcp = None
        if network.kind == NetworkKind.STANDARD.value:
            dhcp = private_network_dhcp.owner_view(db, PrivateNetworkDAO.get_network(db, network.id))
        owned.append({**network.model_dump(), "dhcp": dhcp})
    return {
        "owned": owned,
        "shared": shared,
        "invitations": await shared_portal.my_invitations(auth, db),
    }


def _body(model, values: dict):
    try:
        return model(**values)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        raise ToolError(f"Invalid DHCP parameters: {problems}") from None


async def _dhcp_action(db: Session, auth: dict, sub: str, network_id: int, args: dict) -> Any:
    """Owner-only DHCP sub-actions, through the portal's owner routes."""
    if sub == "dhcp_get":
        return await network_portal.client_dhcp_get(network_id, auth, db)
    if sub == "dhcp_set":
        values = {key: args[key] for key in private_network_dhcp.SETTING_FIELDS if key in args}
        if not values:
            raise ToolError(f"network dhcp_set needs one of: {', '.join(private_network_dhcp.SETTING_FIELDS)}")
        body = _body(network_portal.DhcpSettingsBody, values)
        return await network_portal.client_dhcp_update(network_id, body, auth, db)
    if sub == "dhcp_reservation_add":
        _need(sub, mac=args.get("mac"), ip=args.get("ip"))
        body = _body(network_portal.DhcpReservationBody, {k: args[k] for k in ("mac", "ip", "label") if k in args})
        return await network_portal.client_dhcp_reservation_add(network_id, body, auth, db)
    _need(sub, reservation_id=args.get("reservation_id"))
    reservation_id = int(args["reservation_id"])
    if sub == "dhcp_reservation_update":
        body = _body(network_portal.DhcpReservationUpdate, {k: args[k] for k in ("mac", "ip", "label") if k in args})
        return await network_portal.client_dhcp_reservation_update(network_id, reservation_id, body, auth, db)
    return await network_portal.client_dhcp_reservation_remove(network_id, reservation_id, auth, db)


async def network_action(
    db: Session,
    ctx: ClientMcpAuthContext,
    sub: str,
    *,
    service_id: Optional[int],
    params: Optional[dict],
) -> Any:
    auth = ctx.as_client_auth()
    args = dict(params or {})
    network_id = args.get("network_id")
    invitation_id = args.get("invitation_id")
    if sub in ("invitation_accept", "invitation_decline"):
        _need(sub, invitation_id=invitation_id)
        if sub == "invitation_decline":
            await shared_portal.decline_invitation(int(invitation_id), auth, db)
            return {"declined": True}
        return await shared_portal.accept_invitation(int(invitation_id), auth, db)
    _need(sub, network_id=network_id)
    network_id = int(network_id)
    if sub.startswith("dhcp_"):
        return await _dhcp_action(db, auth, sub, network_id, args)
    network = PrivateNetworkDAO.get_network(db, network_id)
    owned = network is not None and network.owner_user_id == ctx.user_id
    if sub == "invite":
        _need(sub, invitee=args.get("invitee"))
        body = shared_portal.InviteBody(invitee=str(args["invitee"]))
        return await shared_portal.invite(network_id, body, auth, db)
    if sub == "invitation_cancel":
        _need(sub, invitation_id=invitation_id)
        await shared_portal.cancel_invitation(network_id, int(invitation_id), auth, db)
        return {"cancelled": True}
    if sub == "member_remove":
        _need(sub, member_user_id=args.get("member_user_id"))
        await shared_portal.remove_member(network_id, int(args["member_user_id"]), auth, db)
        return {"removed": True}
    if sub == "leave":
        await shared_portal.shared_leave(network_id, auth, db)
        return {"left": True}
    if sub == "attach":
        service_id = service_id or args.get("service_id")
        _need(sub, service_id=service_id)
        body = network_portal.AttachBody(service_id=int(service_id), ip_address=args.get("ip_address"))
        handler = network_portal.client_attach if owned else shared_portal.shared_attach
        return await handler(network_id, body, auth, db)
    _need(sub, attachment_id=args.get("attachment_id"))
    handler = network_portal.client_detach if owned else shared_portal.shared_detach
    return await handler(network_id, int(args["attachment_id"]), auth, db)
