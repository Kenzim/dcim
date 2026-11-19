"""Generate plausible Apple SMBIOS identity for OpenCore PlatformInfo.

Used by the macOS guest-agent deployment strategy and the randomize_smbios
runtime action. Values are intentionally invalid for Apple Check Coverage
(random serials that do not match a sold device).
"""
from __future__ import annotations

import random
import string
import uuid
from typing import Dict, Optional

DEFAULT_SMBIOS_MODEL = "iMacPro1,1"


# Model → serial/board prefixes commonly used with OpenCore GenSMBIOS.
_MODEL_PREFIXES = {
    DEFAULT_SMBIOS_MODEL: ("C02", "C02"),
    "MacPro7,1": ("F5K", "F5K"),
    "iMac19,1": ("C02", "C02"),
    "MacBookPro16,1": ("C02", "C02"),
}


def _rand_alnum(n: int) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(n))


def generate_smbios(model: str = DEFAULT_SMBIOS_MODEL) -> Dict[str, str]:
    """Return PlatformInfo Generic fields for OpenCore + Proxmox smbios1.

    Keys: SystemProductName, SystemSerialNumber, MLB, SystemUUID, ROM_HEX
    """
    product = (model or DEFAULT_SMBIOS_MODEL).strip() or DEFAULT_SMBIOS_MODEL
    serial_prefix, mlb_prefix = _MODEL_PREFIXES.get(product, ("C02", "C02"))
    # 12-char serial (prefix + body), 17-char MLB — GenSMBIOS-shaped.
    serial = f"{serial_prefix}{_rand_alnum(12 - len(serial_prefix))}"
    mlb = f"{mlb_prefix}{_rand_alnum(17 - len(mlb_prefix))}"
    smuuid = str(uuid.uuid4()).upper()
    rom_hex = "".join(f"{random.randint(0, 255):02X}" for _ in range(6))
    return {
        "SystemProductName": product,
        "SystemSerialNumber": serial,
        "MLB": mlb,
        "SystemUUID": smuuid,
        "ROM_HEX": rom_hex,
    }


def smbios_from_smbios1(smbios1: Optional[str], *, default_model: str = DEFAULT_SMBIOS_MODEL) -> Optional[Dict[str, str]]:
    """Rebuild PlatformInfo Generic fields from a Proxmox ``smbios1`` value.

    The serial, UUID and product name come from ``smbios1``. ``smbios1`` carries no board serial or ROM, so
    those are derived from the serial and UUID: the same VM always gets the same values. Returns None when
    ``smbios1`` has no serial or UUID.
    """
    import base64
    import hashlib

    from app.services.vm_identity_stamp import parse_smbios1_fields

    fields = parse_smbios1_fields(smbios1)
    encoded = fields.get("base64") in ("1", "true", "True")

    def text(key: str) -> str:
        raw = fields.get(key) or ""
        if not raw or not encoded:
            return raw
        try:
            return base64.b64decode(raw).decode("utf-8")
        except Exception:
            return raw

    serial = text("serial").strip()
    smuuid = (fields.get("uuid") or "").strip().upper()
    if not serial or not smuuid:
        return None
    product = text("product").strip() or default_model
    digest = hashlib.sha256(f"{smuuid}:{serial}".encode("utf-8")).digest()
    alphabet = string.ascii_uppercase + string.digits
    mlb_prefix = _MODEL_PREFIXES.get(product, ("C02", "C02"))[1]
    mlb = mlb_prefix + "".join(alphabet[b % len(alphabet)] for b in digest[: 17 - len(mlb_prefix)])
    return {
        "SystemProductName": product,
        "SystemSerialNumber": serial,
        "MLB": mlb,
        "SystemUUID": smuuid,
        "ROM_HEX": digest[-6:].hex().upper(),
    }


def smbios1_config_value(sm: Dict[str, str], *, sku: Optional[str] = None) -> str:
    """Build Proxmox ``smbios1`` config string (base64-encoded fields).

    Optional ``sku`` is used for RackFlow durable OS identity (``rf1:tpl=…``)
    and is preserved across macOS SMBIOS randomize when passed through.
    """
    import base64

    def b64(s: str) -> str:
        return base64.b64encode(s.encode("utf-8")).decode("ascii")

    parts = [
        f"uuid={sm['SystemUUID']}",
        "base64=1",
        f"serial={b64(sm['SystemSerialNumber'])}",
        f"manufacturer={b64('Apple Inc.')}",
        f"product={b64(sm['SystemProductName'])}",
        f"family={b64('Mac')}",
    ]
    if sku:
        parts.append(f"sku={b64(sku)}")
    return ",".join(parts)
