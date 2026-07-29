"""Pydantic schemas for the VM guest VNC console.

Shared by the admin, client-portal, and billing mint endpoints, plus the
public launch-ticket redeem endpoint.
"""
from typing import Optional

from pydantic import BaseModel


class VmVncSessionResponse(BaseModel):
    """Returned by the admin/client session-mint endpoints and by
    ``POST /api/vnc/redeem``.

    ``vnc_password`` is the Proxmox-issued, single-session VNC (RFB) ticket
    the noVNC client must present when negotiating the VNC handshake --
    distinct from (and unrelated to) the Proxmox account credentials, which
    never leave the server. Unused when ``console_type`` is ``"serial"``
    (the WS bridge authenticates that stream itself).

    ``console_type`` is ``"vnc"`` for a normal graphical console, or
    ``"serial"`` for VMs configured with a serial display (``vga: serialN``,
    common for cloud-init images with no virtual GPU) -- the frontend uses
    this to choose between the noVNC and xterm.js viewers.

    ``guest_username`` / ``guest_password`` are the guest OS credentials
    stored on the service (e.g. WHMCS ``admin_password``), for the console
    toolbar's reveal / copy / type-password controls. Empty when unknown.
    """

    ws_token: str
    ws_path: str
    vnc_password: str
    expires_in: int
    console_type: str = "vnc"
    service_id: Optional[int] = None
    guest_username: str = ""
    guest_password: str = ""


class VmVncTicketResponse(BaseModel):
    """Returned by the billing API's one-click launch-ticket mint.

    ``launch_url`` opens Rackflow's ``/vnc`` page (e.g. from a WHMCS popup),
    which redeems the ticket for a session and renders the same viewer used
    by the admin/client portals.
    """

    launch_url: str
    expires_in: int


class VmVncRedeemRequest(BaseModel):
    token: str


class VmVncPowerRequest(BaseModel):
    token: str
    action: str  # on | off | reboot | reset


class VmVncPowerResponse(BaseModel):
    ok: bool
    action: str


class VmConsoleTypesResponse(BaseModel):
    """Which Proxmox console types are actually usable for a VM.

    Fetched by the admin/client UI *before* opening a console, so it can
    show a single "Open Console" button when only one type is usable, or
    let the user pick when both are (e.g. a VM with a normal graphical
    display that also has a ``serialN: socket`` device configured).
    """

    vnc: bool
    serial: bool
