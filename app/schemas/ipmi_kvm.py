"""Pydantic schemas for IPMI HTML5 KVM (launch tickets + WS bridge)."""
from typing import Optional

from pydantic import BaseModel


class IpmiKvmSessionResponse(BaseModel):
    """Returned by ``POST /api/kvm/redeem``. BMC cookies/tokens stay server-side."""

    ws_token: str
    ws_path: str
    decode_worker_path: str
    profile: str
    expires_in: int


class IpmiKvmTicketResponse(BaseModel):
    """Returned by the billing API's one-click KVM launch-ticket mint."""

    launch_url: str
    expires_in: int


class IpmiKvmRedeemRequest(BaseModel):
    token: str


class IpmiKvmProfileInfo(BaseModel):
    id: str
    display_name: str
