"""Registry of IPMI HTML5 KVM vendor profiles."""
from __future__ import annotations

from typing import Optional

from app.models.server import Server
from app.services.ipmi_kvm.asrockrack import AsrockRackKvmProfile
from app.services.ipmi_kvm.base import IpmiKvmProfile, IpmiKvmUnavailable
from app.services.ipmi_kvm.gigabyte import GigabyteKvmProfile
from app.services.ipmi_kvm.supermicro import SuperMicroKvmProfile

_PROFILES: dict[str, IpmiKvmProfile] = {
    AsrockRackKvmProfile.id: AsrockRackKvmProfile(),
    GigabyteKvmProfile.id: GigabyteKvmProfile(),
    SuperMicroKvmProfile.id: SuperMicroKvmProfile(),
}


def list_profiles() -> list[dict[str, str]]:
    return [p.describe() for p in _PROFILES.values()]


def get_profile(profile_id: str) -> Optional[IpmiKvmProfile]:
    return _PROFILES.get((profile_id or "").strip())


def normalize_profile_id(value: Optional[str]) -> Optional[str]:
    """Empty / 'none' means no native KVM. Unknown ids raise."""
    raw = (value or "").strip()
    if not raw or raw.lower() in ("none", "off", "disabled"):
        return None
    if raw not in _PROFILES:
        raise IpmiKvmUnavailable(f"Unknown IPMI KVM profile '{raw}'")
    return raw


def kvm_ready(server: Optional[Server]) -> bool:
    if server is None:
        return False
    try:
        return normalize_profile_id(getattr(server, "ipmi_kvm_profile", None)) is not None
    except IpmiKvmUnavailable:
        return False


def profile_for_server(server: Server) -> IpmiKvmProfile:
    pid = normalize_profile_id(getattr(server, "ipmi_kvm_profile", None))
    if not pid:
        raise IpmiKvmUnavailable("HTML5 KVM is not configured for this server")
    profile = get_profile(pid)
    if profile is None:
        raise IpmiKvmUnavailable("HTML5 KVM is not configured for this server")
    return profile
