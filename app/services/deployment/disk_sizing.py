"""Helpers for Proxmox disk size parsing and primary-disk selection."""
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

# Bootable hard-disk keys only (exclude efidisk / tpm / unused).
_DISK_KEY_RE = re.compile(r"^(ide|sata|scsi|virtio)\d+$")
_SIZE_RE = re.compile(r"size=(\d+(?:\.\d+)?)([KMGT])?", re.IGNORECASE)


def parse_size_token_to_gb(token: str) -> Optional[float]:
    """Parse a Proxmox size token like ``64G``, ``65536M``, ``1T`` into GiB."""
    raw = (token or "").strip().upper()
    if not raw:
        return None
    m = re.match(r"^(\d+(?:\.\d+)?)([KMGT])?$", raw)
    if not m:
        return None
    value = float(m.group(1))
    unit = m.group(2) or "G"
    if unit == "K":
        return value / (1024 * 1024)
    if unit == "M":
        return value / 1024
    if unit == "G":
        return value
    if unit == "T":
        return value * 1024
    return None


def disk_entry_size_gb(disk_value: str) -> Optional[float]:
    """Return size in GiB for a QEMU disk config value, or None if not resizable."""
    if not disk_value or not isinstance(disk_value, str):
        return None
    lowered = disk_value.lower()
    if "media=cdrom" in lowered:
        return None
    m = _SIZE_RE.search(disk_value)
    if not m:
        return None
    return parse_size_token_to_gb(m.group(1) + (m.group(2) or "G"))


def select_primary_disk(qemu_config: Dict[str, Any]) -> Optional[Tuple[str, float]]:
    """
    Choose the largest non-CDROM hard disk as the guest OS data disk.

    macOS OpenCore templates typically keep a small EFI disk (ide0 ~1G) and a
    larger virtio/sata/scsi data disk — largest-wins picks the data disk.
    """
    best_key: Optional[str] = None
    best_gb = -1.0
    for key, value in (qemu_config or {}).items():
        if not _DISK_KEY_RE.match(str(key)):
            continue
        size_gb = disk_entry_size_gb(str(value))
        if size_gb is None:
            continue
        if size_gb > best_gb:
            best_gb = size_gb
            best_key = str(key)
    if best_key is None:
        return None
    return best_key, best_gb


def needs_grow(current_gb: float, target_gb: float, *, epsilon_gb: float = 0.5) -> bool:
    """True when target is meaningfully larger than current (never shrink)."""
    try:
        cur = float(current_gb)
        tgt = float(target_gb)
    except (TypeError, ValueError):
        return False
    if tgt <= 0:
        return False
    return tgt > cur + epsilon_gb
