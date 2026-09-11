"""Open a BMC HTML5 KVM session and persist it for the Rackflow WS bridge."""
from __future__ import annotations

import asyncio

from app.models.server import Server
from app.services.ipmi_kvm.base import BmcKvmAuth
from app.services.ipmi_kvm.registry import profile_for_server
from app.services.ipmi_kvm_ticket_service import mint_viewer_session


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


def _mint_bridged_session_sync(server: Server) -> dict:
    profile = profile_for_server(server)
    minted = mint_viewer_session(server.id, profile.id)
    return {
        "ws_token": minted["ws_token"],
        "ws_path": "/api/kvm/ws",
        "decode_worker_path": f"/api/kvm/assets/{profile.decode_worker_path}",
        "profile": profile.id,
        "expires_in": minted["expires_in"],
    }


async def mint_bridged_session(server: Server) -> dict:
    """Mint a viewer-only WS ticket. BMC login happens when the hub starts."""
    return await asyncio.to_thread(_mint_bridged_session_sync, server)
