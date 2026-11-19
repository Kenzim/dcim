"""Per-boot iPXE ISOs for virtual-media network boot.

``boot_media/rackflow-vmedia.iso`` (built by ``scripts/build-rackflow-netboot-iso.sh``)
carries ``boot_media/placeholder.ipxe`` as ``autoexec.ipxe`` twice: once in the
ISO9660 tree (BIOS) and once inside the FAT ESP image (UEFI). Both copies are
stored uncompressed, so a boot ISO is produced by overwriting every placeholder
block with a same-length script. Nothing else in the image moves.

``boot_media/rackflow-vmedia-snp.iso`` is the same template with ``snp.efi`` as the
UEFI program instead of ``ipxe.efi``; the server's virtual-media profile picks one
(``VirtualMediaProfile.uefi_loader``).
"""
from __future__ import annotations

import secrets
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from app.core.config import settings
from app.services.pxe_boot_urls import lan_boot_base_url
from app.services.virtual_media.base import VirtualMediaUnavailable

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BOOT_MEDIA_DIR = _REPO_ROOT / "boot_media"
TEMPLATE_ISO = _BOOT_MEDIA_DIR / "rackflow-vmedia.iso"
TEMPLATE_ISO_SNP = _BOOT_MEDIA_DIR / "rackflow-vmedia-snp.iso"
PLACEHOLDER = _BOOT_MEDIA_DIR / "placeholder.ipxe"
BOOT_ISO_PREFIX = "rfboot-"

_template_cache: dict = {}


def is_boot_iso_name(filename: Optional[str]) -> bool:
    return (filename or "").startswith(BOOT_ISO_PREFIX)


def boot_iso_filename() -> str:
    """Unguessable name: media runners serve ``/isos/{name}`` without auth."""
    return f"{BOOT_ISO_PREFIX}{secrets.token_hex(16)}.iso"


def _explicit_boot_base_url() -> Optional[str]:
    explicit = (settings.vmedia_boot_base_url or "").strip().rstrip("/")
    if not explicit:
        return None
    parsed = urlparse(explicit)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise VirtualMediaUnavailable("VMEDIA_BOOT_BASE_URL must be an http:// or https:// URL")
    return explicit


def boot_base_url(db, pxe_ip: Optional[str]) -> str:
    """Origin iPXE chains to for the server whose PXE IP is ``pxe_ip``.

    ``VMEDIA_BOOT_BASE_URL`` overrides it for every server. Otherwise it is the
    same per-subnet ``http://{next-server}:8000`` that dhcpd hands PXE clients.
    """
    return _explicit_boot_base_url() or lan_boot_base_url(db, pxe_ip)


def pending_boot_args(db, server_id: int) -> Optional[tuple[str, str]]:
    """``(base_url, nonce)`` for the server's pending virtual-media boot task, else None."""
    from app.dao import BootTaskDAO, NetworkPortDAO

    task = BootTaskDAO.get_pending_by_server(db, server_id) if server_id else None
    if (
        task is None
        or task.boot_method != "vmedia"
        or not task.pxe_nonce
        or task.pxe_nonce_used_at is not None
    ):
        return None
    pxe_port = NetworkPortDAO.get_pxe_boot_port(db, server_id)
    return boot_base_url(db, pxe_port.pxe_ip if pxe_port else None), task.pxe_nonce


def uefi_loader_for(db, server_id: int) -> str:
    """UEFI program on the server's boot CD: its virtual-media profile's choice, else ``ipxe``."""
    from app.dao.server_dao import ServerDAO
    from app.services.virtual_media.registry import profile_for_server

    server = ServerDAO.get_by_id(db, server_id) if server_id else None
    if server is None:
        return "ipxe"
    try:
        return profile_for_server(server).uefi_loader(server)
    except VirtualMediaUnavailable:
        return "ipxe"


def vmedia_boot_available() -> bool:
    try:
        _explicit_boot_base_url()
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


def _template_path(uefi_loader: str) -> Path:
    if uefi_loader == "ipxe":
        return TEMPLATE_ISO
    if uefi_loader == "snp":
        return TEMPLATE_ISO_SNP
    raise VirtualMediaUnavailable(f"Unknown UEFI boot program '{uefi_loader}'")


def _load_template(path: Path) -> tuple[bytes, bytes]:
    try:
        mtime = (path.stat().st_mtime_ns, PLACEHOLDER.stat().st_mtime_ns)
    except FileNotFoundError as exc:
        raise VirtualMediaUnavailable(
            f"Virtual-media boot ISO template {path.name} is missing; run scripts/build-rackflow-netboot-iso.sh"
        ) from exc
    cached = _template_cache.get(path)
    if cached is None or cached[0] != mtime:
        cached = (mtime, path.read_bytes(), PLACEHOLDER.read_bytes())
        _template_cache[path] = cached
    return cached[1], cached[2]


def stamp_boot_iso(script: str, uefi_loader: str = "ipxe") -> bytes:
    """Return the template ISO with every placeholder replaced by ``script``."""
    iso, placeholder = _load_template(_template_path(uefi_loader))
    payload = script.encode("ascii")
    if len(payload) + 1 > len(placeholder):
        raise VirtualMediaUnavailable("Boot script does not fit in the ISO placeholder")
    payload += b"\n" + b"#" * (len(placeholder) - len(payload) - 2) + b"\n"
    if iso.count(placeholder) < 2:
        raise VirtualMediaUnavailable("Boot ISO template is missing its BIOS/UEFI script placeholders")
    return iso.replace(placeholder, payload)


def build_boot_iso(base_url: str, nonce: str, uefi_loader: str = "ipxe") -> bytes:
    return stamp_boot_iso(render_boot_script(base_url, nonce), uefi_loader)
