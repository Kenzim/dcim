"""Registry of BMC virtual-media profiles."""
from __future__ import annotations

from typing import Optional

from app.models.server import Server
from app.services.virtual_media.asrockrack import AsrockRackVirtualMediaProfile
from app.services.virtual_media.base import VirtualMediaProfile, VirtualMediaUnavailable
from app.services.virtual_media.gigabyte import GigabyteVirtualMediaProfile
from app.services.virtual_media.supermicro import SuperMicroVirtualMediaProfile
from app.services.virtual_media.supermicro_x9 import SuperMicroX9VirtualMediaProfile

_PROFILES: dict[str, VirtualMediaProfile] = {
    AsrockRackVirtualMediaProfile.id: AsrockRackVirtualMediaProfile(),
    GigabyteVirtualMediaProfile.id: GigabyteVirtualMediaProfile(),
    SuperMicroVirtualMediaProfile.id: SuperMicroVirtualMediaProfile(),
    SuperMicroX9VirtualMediaProfile.id: SuperMicroX9VirtualMediaProfile(),
}


def list_profiles() -> list[dict[str, str]]:
    return [p.describe() for p in _PROFILES.values()]


def get_profile(profile_id: str) -> Optional[VirtualMediaProfile]:
    return _PROFILES.get((profile_id or "").strip())


def register_profile(profile: VirtualMediaProfile) -> None:
    """Add or replace a profile (used by tests)."""
    if not profile.id:
        raise VirtualMediaUnavailable("Virtual media profile id is required")
    _PROFILES[profile.id] = profile


def unregister_profile(profile_id: str) -> None:
    _PROFILES.pop((profile_id or "").strip(), None)


def normalize_profile_id(value: Optional[str]) -> Optional[str]:
    """Empty / 'none' means virtual media is off. Unknown ids raise."""
    raw = (value or "").strip()
    if not raw or raw.lower() in ("none", "off", "disabled"):
        return None
    if raw not in _PROFILES:
        raise VirtualMediaUnavailable(f"Unknown virtual media profile '{raw}'")
    return raw


def virtual_media_ready(server: Optional[Server]) -> bool:
    if server is None:
        return False
    try:
        return normalize_profile_id(getattr(server, "virtual_media_profile", None)) is not None
    except VirtualMediaUnavailable:
        return False


def profile_for_server(server: Server) -> VirtualMediaProfile:
    pid = normalize_profile_id(getattr(server, "virtual_media_profile", None))
    if not pid:
        raise VirtualMediaUnavailable("Virtual media is not configured for this server")
    profile = get_profile(pid)
    if profile is None:
        raise VirtualMediaUnavailable("Virtual media is not configured for this server")
    return profile
