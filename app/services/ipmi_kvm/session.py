"""Open a BMC HTML5 KVM session and persist it for the Rackflow WS bridge."""
from __future__ import annotations

from app.models.server import Server
from app.services.ipmi_kvm.base import BmcKvmAuth
from app.services.ipmi_kvm.registry import profile_for_server
from app.services.ipmi_kvm_ticket_service import mint_ws_session


def auth_from_ws_session(data: dict) -> BmcKvmAuth:
    return BmcKvmAuth(
        https_base=data["https_base"],
        origin=data["https_base"],
        hostname=data["hostname"],
        cookie=data["cookie"],
        csrf=data["csrf"],
        kvm_token=data["kvm_token"],
        client_ip=data["client_ip"],
        username=data["username"],
        server_ip=data.get("server_ip") or "",
    )


async def mint_bridged_session(server: Server) -> dict:
    """Login to the BMC, mint a WS session, return browser-safe payload."""
    profile = profile_for_server(server)
    auth = await profile.login(server)
    minted = mint_ws_session(
        server.id,
        profile.id,
        https_base=auth.origin,
        cookie=auth.cookie,
        csrf=auth.csrf,
        kvm_token=auth.kvm_token,
        client_ip=auth.client_ip,
        username=auth.username,
        hostname=auth.hostname,
        server_ip=auth.server_ip,
    )
    return {
        "ws_token": minted["ws_token"],
        "ws_path": "/api/kvm/ws",
        "decode_worker_path": f"/api/kvm/assets/{profile.decode_worker_path}",
        "profile": profile.id,
        "expires_in": minted["expires_in"],
    }
