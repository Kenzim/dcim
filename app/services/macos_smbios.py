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

_DEFAULT_SMBIOS_MODEL = "iMacPro1,1"


# Model → serial/board prefixes commonly used with OpenCore GenSMBIOS.
_MODEL_PREFIXES = {
    _DEFAULT_SMBIOS_MODEL: ("C02", "C02"),
    "MacPro7,1": ("F5K", "F5K"),
    "iMac19,1": ("C02", "C02"),
    "MacBookPro16,1": ("C02", "C02"),
}


def _rand_alnum(n: int) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(n))


def generate_smbios(model: str = _DEFAULT_SMBIOS_MODEL) -> Dict[str, str]:
    """Return PlatformInfo Generic fields for OpenCore + Proxmox smbios1.

    Keys: SystemProductName, SystemSerialNumber, MLB, SystemUUID, ROM_HEX
    """
    product = (model or _DEFAULT_SMBIOS_MODEL).strip() or _DEFAULT_SMBIOS_MODEL
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
