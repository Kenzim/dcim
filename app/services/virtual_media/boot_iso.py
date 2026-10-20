"""Per-boot iPXE ISOs for virtual-media network boot.

``boot_media/rackflow-vmedia.iso`` (built by ``scripts/build-rackflow-netboot-iso.sh``)
carries ``boot_media/placeholder.ipxe`` as ``autoexec.ipxe`` twice: once in the
ISO9660 tree (BIOS) and once inside the FAT ESP image (UEFI). Both copies are
stored uncompressed, so a boot ISO is produced by overwriting every placeholder
block with a same-length script. Nothing else in the image moves.
"""
from __future__ import annotations

import secrets
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from app.core.config import settings
from app.services.virtual_media.base import VirtualMediaUnavailable

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BOOT_MEDIA_DIR = _REPO_ROOT / "boot_media"
TEMPLATE_ISO = _BOOT_MEDIA_DIR / "rackflow-vmedia.iso"
PLACEHOLDER = _BOOT_MEDIA_DIR / "placeholder.ipxe"
BOOT_ISO_PREFIX = "rfboot-"

_template_cache: dict = {}


def is_boot_iso_name(filename: Optional[str]) -> bool:
    return (filename or "").startswith(BOOT_ISO_PREFIX)


def boot_iso_filename() -> str:
    """Unguessable name: media runners serve ``/isos/{name}`` without auth."""
    return f"{BOOT_ISO_PREFIX}{secrets.token_hex(16)}.iso"


def boot_base_url() -> str:
    """HTTPS origin iPXE chains to. Must present a publicly trusted certificate."""
    base = ((settings.public_base_url or "").strip() or (settings.public_app_url or "").strip()).rstrip("/")
    if urlparse(base).scheme != "https":
        raise VirtualMediaUnavailable(
            "Virtual-media boot needs PUBLIC_BASE_URL (or PUBLIC_APP_URL) set to an https:// URL"
        )
    return base


def vmedia_boot_available() -> bool:
    try:
        boot_base_url()
    except VirtualMediaUnavailable:
        return False
    return TEMPLATE_ISO.is_file() and PLACEHOLDER.is_file()


def render_boot_script(base_url: str, nonce: str) -> str:
    url = f"{base_url.rstrip('/')}/api/servers/interaction/pxe?mac=${{netX/mac}}&t={nonce}"
    return f"""#!ipxe
echo Rackflow virtual-media boot
set attempt:int32 0
:retry
inc attempt
iseq ${{attempt}} 6 && goto fail ||
dhcp || goto wait
chain {url} && goto done ||
:wait
echo Boot attempt ${{attempt}} failed; retrying in 10s
sleep 10
goto retry
:fail
echo
echo Rackflow boot failed: check DHCP for this server and that a reinstall is pending.
shell
:done
"""


def _load_template() -> tuple[bytes, bytes]:
    try:
        mtime = (TEMPLATE_ISO.stat().st_mtime_ns, PLACEHOLDER.stat().st_mtime_ns)
    except FileNotFoundError as exc:
        raise VirtualMediaUnavailable(
            "Virtual-media boot ISO template is missing; run scripts/build-rackflow-netboot-iso.sh"
        ) from exc
    if _template_cache.get("mtime") != mtime:
        _template_cache.update(
            mtime=mtime, iso=TEMPLATE_ISO.read_bytes(), placeholder=PLACEHOLDER.read_bytes()
        )
    return _template_cache["iso"], _template_cache["placeholder"]


def stamp_boot_iso(script: str) -> bytes:
    """Return the template ISO with every placeholder replaced by ``script``."""
    iso, placeholder = _load_template()
    payload = script.encode("ascii")
    if len(payload) + 1 > len(placeholder):
        raise VirtualMediaUnavailable("Boot script does not fit in the ISO placeholder")
    payload += b"\n" + b"#" * (len(placeholder) - len(payload) - 2) + b"\n"
    if iso.count(placeholder) < 2:
        raise VirtualMediaUnavailable("Boot ISO template is missing its BIOS/UEFI script placeholders")
    return iso.replace(placeholder, payload)


def build_boot_iso(nonce: str) -> bytes:
    return stamp_boot_iso(render_boot_script(boot_base_url(), nonce))
