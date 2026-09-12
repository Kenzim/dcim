from typing import Annotated
"""Public VM guest VNC console endpoints: launch-ticket redeem + WS bridge.

Mounted at ``/api/vnc``.

``POST /redeem`` is intentionally unauthenticated -- like
``GET /api/client/sso/redeem`` -- because the launch ticket itself, minted
server-side by an already-authenticated caller (the billing API on behalf of
a WHMCS session), *is* the credential.

``WS /ws`` proxies raw RFB frames between the browser (noVNC) and Proxmox's
own ``vncwebsocket`` endpoint, so Proxmox account credentials never reach the
browser. This intentionally does not reuse the separate ``ipmi_proxy_runner``
edge service: that runner proxies an entire BMC/Proxmox web UI by design,
which would expose far more than a single VM's console.
"""
import asyncio
import json
import logging

import websockets
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.openapi_responses import COMMON_ERROR_RESPONSES
from app.dao.proxmox_inventory_dao import ProxmoxInventoryDAO
from app.dao.service_dao import ServiceDAO
from app.plugins.base import PowerState
from app.plugins.proxmox import ConsoleTypeUnavailable
from app.plugins.registry import get_registry
from app.models.service_vm import VMGuestState
from app.schemas.vm_vnc import (
    VmVncPowerRequest,
    VmVncPowerResponse,
    VmVncRedeemRequest,
    VmVncSessionResponse,
)
from app.services.proxmox_placement import (
    ProxmoxPlacementError,
    cluster_to_proxmox_plugin_config,
    resolve_proxmox_plugin_for_service,
)
from app.services.ipmi_kvm.bmc_tls import ssl_context_for_verify
from app.services.vm_guest_credentials import session_guest_fields
from app.services.vm_vnc_ticket_service import (
    get_ws_session,
    mint_ws_session,
    redeem_launch_ticket,
    refresh_ws_session,
    update_ws_session_node,
)

logger = logging.getLogger(__name__)

DbDep = Annotated[Session, Depends(get_db)]

router = APIRouter(prefix="/vnc", tags=["vm-vnc"])


def _placement_http_error(exc: ProxmoxPlacementError) -> HTTPException:
    """Map a resolver failure to the status code these public console
    endpoints have always used: missing/incomplete placement (400 from the
    resolver) reads as 409 Conflict here -- "VM isn't ready for a console
    yet" -- while a genuinely unknown cluster/VMID keeps its own status."""
    status_code = status.HTTP_409_CONFLICT if exc.status_code == 400 else exc.status_code
    return HTTPException(status_code=status_code, detail=str(exc))


@router.post("/redeem", response_model=VmVncSessionResponse, responses={**COMMON_ERROR_RESPONSES})
async def redeem_vnc_launch_ticket(body: VmVncRedeemRequest, db: DbDep):
    """Consume a launch ticket and mint a WS session for the ``/vnc`` page.

    Called by the browser (unauthenticated) after a WHMCS "Open VNC console"
    popup lands on Rackflow's ``/vnc?t=...``.
    """
    redeemed = redeem_launch_ticket(body.token)
    if redeemed is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Console link is invalid or has expired")
    service_id = redeemed["service_id"]
    requested_console_type = redeemed["console_type"]

    service = ServiceDAO.get_by_id(db, service_id)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")

    try:
        plugin, cid, _node, vmid = await resolve_proxmox_plugin_for_service(db, service)
    except ProxmoxPlacementError as exc:
        raise _placement_http_error(exc) from exc

    try:
        power_state = await plugin.get_power_state()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Could not reach Proxmox: {exc}"
        ) from exc
    if power_state != PowerState.ON:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="VM must be running to open a console")

    try:
        console = await plugin.open_console_proxy(console_type=requested_console_type)
    except ConsoleTypeUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Failed to open console: {exc}"
        ) from exc

    # ``plugin.node`` reflects any mid-call relocation (see ProxmoxPlugin
    # relocator) that may have happened during the power/console calls above.
    session = mint_ws_session(
        service.id, cid, plugin.node, vmid, console["port"], console["ticket"], console["console_type"]
    )
    logger.info("Redeemed VM %s launch ticket for service %s", console["console_type"], service.id)
    return VmVncSessionResponse(
        ws_token=session["ws_token"],
        ws_path="/api/vnc/ws",
        vnc_password=console["ticket"],
        expires_in=session["expires_in"],
        console_type=console["console_type"],
        **session_guest_fields(service),
    )


@router.post("/refresh", response_model=VmVncSessionResponse, responses={**COMMON_ERROR_RESPONSES})
async def refresh_vnc_session(body: VmVncRedeemRequest, db: DbDep):
    """Mint a fresh Proxmox console proxy for an existing WS session.

    Used by the console viewer's Reconnect button. The Rackflow ``ws_token``
    stays the same, but Proxmox's vncproxy/termproxy tickets die when the
    upstream WebSocket closes -- reconnecting with the old ticket just fails
    (looks like a disconnect). This opens a new proxy of the same
    ``console_type`` and writes the new port/ticket into the session.
    Authenticated only by the WS session token itself (same as ``/ws``).
    """
    session = get_ws_session(body.token)
    if session is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Console session is invalid or has expired")

    service = ServiceDAO.get_by_id(db, session["service_id"])
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")

    # Rebuild from the service's current placement (not the frozen session
    # node) so a migrate mid-console can recover on refresh.
    try:
        plugin, _cid, node, _vmid = await resolve_proxmox_plugin_for_service(db, service)
    except ProxmoxPlacementError as exc:
        raise _placement_http_error(exc) from exc

    try:
        power_state = await plugin.get_power_state()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Could not reach Proxmox: {exc}"
        ) from exc
    if power_state != PowerState.ON:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="VM must be running to open a console")

    try:
        console = await plugin.open_console_proxy(console_type=session.get("console_type") or "vnc")
    except ConsoleTypeUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Failed to open console: {exc}"
        ) from exc

    updated = refresh_ws_session(
        body.token,
        console["port"],
        console["ticket"],
        console["console_type"],
        node_name=plugin.node if plugin.node != node else None,
    )
    if updated is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Console session is invalid or has expired")

    # Remaining TTL is whatever Redis still has; surface the configured
    # session TTL as a conservative upper bound for the client UI.
    guest_fields = session_guest_fields(service)
    logger.info(
        "Refreshed VM %s WS session for service %s", console["console_type"], session["service_id"]
    )
    return VmVncSessionResponse(
        ws_token=body.token,
        ws_path="/api/vnc/ws",
        vnc_password=console["ticket"],
        expires_in=settings.vm_vnc_session_ttl_seconds,
        console_type=console["console_type"],
        **guest_fields,
    )


@router.post("/power", response_model=VmVncPowerResponse, responses={**COMMON_ERROR_RESPONSES})
async def console_power_action(body: VmVncPowerRequest, db: DbDep):
    """Power on/off/reboot the VM bound to a console WS session.

    Authenticated only by the ``ws_token`` (same as ``/ws`` / ``/refresh``),
    so the unauthenticated ``/vnc`` popup can offer power controls without a
    Rackflow login. Placement comes from the session -- the client cannot
    retarget a different VM.
    """
    session = get_ws_session(body.token)
    if session is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Console session is invalid or has expired")

    service = ServiceDAO.get_by_id(db, session["service_id"])
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")

    # Rebuild from the service's current placement (not the frozen session
    # node) so a migrate mid-console can still be power-controlled.
    try:
        plugin, _cid, node, _vmid = await resolve_proxmox_plugin_for_service(db, service)
    except ProxmoxPlacementError as exc:
        raise _placement_http_error(exc) from exc
    if node != session["node_name"]:
        # Node changed since the session was minted/refreshed -- keep the
        # Redis cache fresh so a subsequent /refresh doesn't retry a stale node.
        update_ws_session_node(body.token, node)

    action = (body.action or "").strip().lower()
    try:
        if action == "on":
            ok = await plugin.power_on()
            new_state = VMGuestState.RUNNING
        elif action == "off":
            ok = await plugin.power_off(force=False)
            new_state = VMGuestState.STOPPED
        elif action in ("reboot", "reset"):
            ok = await plugin.power_reset()
            new_state = VMGuestState.RUNNING
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid action; use on|off|reboot|reset",
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Power action '{action}' failed: {exc}"
        ) from exc

    if not ok:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Power action '{action}' failed"
        )

    if service.vm:
        service.vm.guest_state = new_state
        service.vm.guest_last_error = None
        ServiceDAO.update(db, service)

    logger.info("Console power '%s' for service %s", action, service.id)
    return VmVncPowerResponse(ok=True, action=action)


async def _pipe_browser_to_upstream(websocket: WebSocket, upstream) -> None:
    """VNC (RFB) direction: raw byte pass-through to Proxmox.

    Text frames are a small Rackflow control channel (not forwarded upstream).
    Used for console RTT probes: ``{"type":"ping","t":...}`` is answered with
    ``{"type":"pong","t":...}`` so the noVNC viewer can show latency without
    injecting bytes into the RFB stream.
    """
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            data = message.get("bytes")
            if data is not None:
                await upstream.send(data)
                continue
            text = message.get("text")
            if text is None:
                continue
            try:
                control = json.loads(text)
            except ValueError:
                continue
            if control.get("type") == "ping":
                await websocket.send_text(json.dumps({"type": "pong", "t": control.get("t")}))
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.debug("VM VNC: browser->upstream pipe ended: %s", exc)


async def _pipe_upstream_to_browser(websocket: WebSocket, upstream) -> None:
    """Shared by both console types: Proxmox never frames server->client data."""
    try:
        async for data in upstream:
            if isinstance(data, str):
                data = data.encode("utf-8")
            await websocket.send_bytes(data)
    except Exception as exc:  # noqa: BLE001
        logger.debug("VM VNC: upstream->browser pipe ended: %s", exc)


async def _pipe_browser_to_upstream_serial(websocket: WebSocket, upstream) -> None:
    """Serial console direction: translate our simple browser protocol into
    Proxmox's ``pve-xtermjs`` line protocol (see ``SerialConsole.svelte``):

    - Binary frames from the browser are raw keystroke/paste bytes, wrapped
      as Proxmox's ``0:<byte-length>:<data>`` "normal message".
    - Text frames are our own JSON control messages; only
      ``{"type": "resize", "cols": N, "rows": N}`` is recognized, translated
      to Proxmox's ``1:<cols>:<rows>:`` "resize message".

    Server->client data is unframed (handled by ``_pipe_upstream_to_browser``
    unchanged); only the client->server direction needs Proxmox's framing.
    """
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            data = message.get("bytes")
            if data is not None:
                await upstream.send(f"0:{len(data)}:".encode("utf-8") + data)
                continue
            text = message.get("text")
            if text is None:
                continue
            try:
                control = json.loads(text)
            except ValueError:
                continue
            if control.get("type") == "resize":
                cols = int(control.get("cols") or 80)
                rows = int(control.get("rows") or 24)
                await upstream.send(f"1:{cols}:{rows}:")
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.debug("VM VNC: browser->upstream (serial) pipe ended: %s", exc)


async def _serial_keepalive(upstream) -> None:
    """Proxmox's termproxy times out an idle connection after 5 minutes;
    the reference xterm.js client pings every 30s to prevent that."""
    try:
        while True:
            await asyncio.sleep(25)
            await upstream.send("2")
    except Exception as exc:  # noqa: BLE001
        logger.debug("VM VNC: serial keepalive ended: %s", exc)


@router.websocket("/ws")
async def vnc_websocket(websocket: WebSocket, token: str, db: DbDep):
    """Bridge the browser's noVNC WebSocket to Proxmox's ``vncwebsocket``.

    Auth is via the ``token`` query param (a WS session token minted by one
    of the admin/client/redeem endpoints above) rather than cookies, so this
    works the same regardless of which surface opened the console.
    """
    session = get_ws_session(token)
    if session is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    cluster = ProxmoxInventoryDAO.get_cluster(db, session["cluster_id"])
    if cluster is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    try:
        plugin_config = cluster_to_proxmox_plugin_config(cluster, session["node_name"], session["vmid"])
    except ValueError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    plugin = get_registry().get_plugin("proxmox", plugin_config)

    try:
        cookie = await plugin.get_vnc_auth_cookie()
    except Exception as exc:
        logger.warning("VM VNC: failed to authenticate to Proxmox for session: %s", exc)
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        return

    upstream_url = plugin.vnc_websocket_url(session["vnc_port"], session["vnc_ticket"])
    is_serial = session.get("console_type") == "serial"

    ssl_context = ssl_context_for_verify(plugin.verify_ssl)

    await websocket.accept(subprotocol="binary")
    try:
        async with websockets.connect(
            upstream_url,
            additional_headers={"Cookie": cookie},
            subprotocols=["binary"],
            ssl=ssl_context,
            max_size=None,
            open_timeout=15,
        ) as upstream:
            if is_serial:
                # Proxmox's xterm.js console authenticates the term stream
                # itself with a leading "user:ticket\n" line, sent as the
                # very first message (see pve-xtermjs's client).
                await upstream.send(f"{plugin.username}:{session['vnc_ticket']}\n")
                tasks = [
                    asyncio.create_task(_pipe_browser_to_upstream_serial(websocket, upstream)),
                    asyncio.create_task(_pipe_upstream_to_browser(websocket, upstream)),
                    asyncio.create_task(_serial_keepalive(upstream)),
                ]
            else:
                tasks = [
                    asyncio.create_task(_pipe_browser_to_upstream(websocket, upstream)),
                    asyncio.create_task(_pipe_upstream_to_browser(websocket, upstream)),
                ]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
    except Exception as exc:  # noqa: BLE001
        logger.warning("VM VNC: upstream bridge failed for service %s: %s", session["service_id"], exc)
    finally:
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass
