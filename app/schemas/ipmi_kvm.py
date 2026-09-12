"""Pydantic schemas for IPMI HTML5 KVM (launch tickets + WS bridge)."""
from typing import List, Optional

from pydantic import BaseModel, Field


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


class IpmiKvmPowerAction(BaseModel):
    id: str
    label: str
    confirm: Optional[str] = None
    enabled: bool = True


class IpmiKvmPowerStateResponse(BaseModel):
    power_state: str
    actions: List[IpmiKvmPowerAction]
    success: bool = True
    message: str = ""


class IpmiKvmPowerRequest(BaseModel):
    token: Optional[str] = None
    action: str = Field(..., min_length=1, max_length=32)
