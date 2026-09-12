"""Mint a viewer-only WS ticket for Serial-over-LAN."""
from __future__ import annotations

import asyncio

from app.models.server import Server
from app.services.sol.registry import profile_for_server
from app.services.sol.ticket_service import mint_viewer_session


def _mint_bridged_session_sync(server: Server) -> dict:
    profile = profile_for_server(server)
    minted = mint_viewer_session(server.id, profile.id)
    return {
        "ws_token": minted["ws_token"],
        "ws_path": "/api/sol/ws",
        "profile": profile.id,
        "expires_in": minted["expires_in"],
    }


async def mint_bridged_session(server: Server) -> dict:
    return await asyncio.to_thread(_mint_bridged_session_sync, server)
