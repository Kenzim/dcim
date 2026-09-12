"""Gigabyte AMI MegaRAC virtual CD (same Redfish path as ASRockRack)."""
from __future__ import annotations

from app.services.virtual_media.asrockrack import AsrockRackVirtualMediaProfile


class GigabyteVirtualMediaProfile(AsrockRackVirtualMediaProfile):
    id = "gigabyte"
    display_name = "Gigabyte (AMI MegaRAC virtual media)"
