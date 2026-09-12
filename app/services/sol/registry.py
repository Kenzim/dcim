"""Registry of Serial-over-LAN method profiles."""
from __future__ import annotations

from typing import Optional

from app.models.server import Server
from app.services.sol.base import SolProfile, SolUnavailable
from app.services.sol.ipmi_sol import IpmiSolProfile

_PROFILES: dict[str, SolProfile] = {
    IpmiSolProfile.id: IpmiSolProfile(),
}


def list_profiles() -> list[dict[str, str]]:
    return [p.describe() for p in _PROFILES.values()]


def get_profile(profile_id: str) -> Optional[SolProfile]:
    return _PROFILES.get((profile_id or "").strip())


def register_profile(profile: SolProfile) -> None:
    """Add or replace a profile (used by tests)."""
    if not profile.id:
        raise SolUnavailable("SOL profile id is required")
    _PROFILES[profile.id] = profile


def unregister_profile(profile_id: str) -> None:
    _PROFILES.pop((profile_id or "").strip(), None)


def normalize_profile_id(value: Optional[str]) -> Optional[str]:
    """Empty / 'none' means SOL is off. Unknown ids raise."""
    raw = (value or "").strip()
    if not raw or raw.lower() in ("none", "off", "disabled"):
        return None
    if raw not in _PROFILES:
        raise SolUnavailable(f"Unknown SOL profile '{raw}'")
    return raw


def sol_ready(server: Optional[Server]) -> bool:
    if server is None:
        return False
    try:
        return normalize_profile_id(getattr(server, "sol_profile", None)) is not None
    except SolUnavailable:
        return False


def profile_for_server(server: Server) -> SolProfile:
    pid = normalize_profile_id(getattr(server, "sol_profile", None))
    if not pid:
        raise SolUnavailable("Serial-over-LAN is not configured for this server")
    profile = get_profile(pid)
    if profile is None:
        raise SolUnavailable("Serial-over-LAN is not configured for this server")
    return profile
