"""Pydantic schemas for Serial-over-LAN tickets, send, and profiles."""
from typing import Literal

from pydantic import BaseModel, Field


class SolSessionResponse(BaseModel):
    ws_token: str
    ws_path: str
    profile: str
    expires_in: int


class SolTicketResponse(BaseModel):
    launch_url: str
    expires_in: int


class SolRedeemRequest(BaseModel):
    token: str


class SolProfileInfo(BaseModel):
    id: str
    display_name: str


class SolSendRequest(BaseModel):
    data: str
    encoding: Literal["utf-8", "base64"] = "utf-8"
    wait_ms: int = Field(default=0, ge=0, le=30_000)


class SolSendResponse(BaseModel):
    written: int
    output: str = ""
    encoding: str = "utf-8"
