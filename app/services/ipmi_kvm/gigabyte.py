"""Gigabyte AMI MegaRAC SP-X HTML5 KVM (IVTP on ``wss://bmc/kvm``)."""
from __future__ import annotations

from app.services.ipmi_kvm.asrockrack import AsrockRackKvmProfile


class GigabyteKvmProfile(AsrockRackKvmProfile):
    """Same MegaRAC login/WS path as ASRockRack; validate body includes server_ip."""

    id = "gigabyte"
    display_name = "Gigabyte (AMI MegaRAC HTML5 KVM)"
    include_server_ip_in_validate = True
