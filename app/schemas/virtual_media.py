"""Pydantic schemas for BMC virtual media."""
from typing import List, Optional

from pydantic import BaseModel, Field


class VirtualMediaProfileInfo(BaseModel):
    id: str
    display_name: str


class VirtualMediaIsoInfo(BaseModel):
    filename: str
    size_bytes: int
    size_mb: float


class VirtualMediaStatusResponse(BaseModel):
    available: bool
    profile: Optional[str] = None
    inserted: bool = False
    device: str = ""
    image_name: str = ""
    isos: List[VirtualMediaIsoInfo] = []
    boot_once_supported: bool = True
    boot_once_applied: Optional[bool] = None


class VirtualMediaInsertRequest(BaseModel):
    filename: str
    boot_once: bool = Field(default=False)
