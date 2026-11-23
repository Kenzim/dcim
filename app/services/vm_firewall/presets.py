"""Rule presets offered to clients, and the first-time suggestion from the VM's operating system."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.service_vm import ServiceVm
from app.models.product_catalog import VMTemplate


@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    description: str
    rules: tuple[dict[str, Any], ...] = ()
    policy_in: Optional[str] = None


def _in(protocol: str, *, ports: Optional[str] = None, icmp_type: Optional[str] = None, comment: str) -> dict:
    rule: dict[str, Any] = {"direction": "in", "action": "ACCEPT", "protocol": protocol, "comment": comment}
    if ports is not None:
        rule["ports"] = ports
    if icmp_type is not None:
        rule["icmp_type"] = icmp_type
    return rule


PRESETS: dict[str, Preset] = {
    preset.key: preset
    for preset in (
        Preset("ssh", "SSH", "Allow SSH (TCP 22) from anywhere.", (_in("tcp", ports="22", comment="SSH"),)),
        Preset("rdp", "Remote Desktop", "Allow Remote Desktop (TCP 3389) from anywhere.", (_in("tcp", ports="3389", comment="Remote Desktop"),)),
        Preset(
            "web",
            "Web server",
            "Allow HTTP and HTTPS (TCP 80 and 443, UDP 443) from anywhere.",
            (
                _in("tcp", ports="80,443", comment="Web"),
                _in("udp", ports="443", comment="Web (HTTP/3)"),
            ),
        ),
        Preset(
            "ping",
            "Ping",
            "Allow ping over IPv4 and IPv6.",
            (
                _in("icmp", icmp_type="echo-request", comment="Ping"),
                _in("ipv6-icmp", icmp_type="echo-request", comment="Ping (IPv6)"),
            ),
        ),
        Preset(
            "allow_all",
            "Allow everything",
            "Accept all incoming traffic; your rules can still block specific traffic.",
            policy_in="ACCEPT",
        ),
    )
}

SUGGESTIONS = {"linux": ("ssh", "ping"), "windows": ("rdp", "ping")}


def get_preset(key: str) -> Preset:
    from app.services.vm_firewall.errors import FirewallValidationError

    preset = PRESETS.get(key)
    if preset is None:
        raise FirewallValidationError("unknown preset", code="unknown_preset", field="preset")
    return preset


def os_family(db: Session, service_id: int) -> Optional[str]:
    """``linux`` or ``windows`` from the VM template the service was built from, else None."""
    vm = db.get(ServiceVm, service_id)
    if vm is None or vm.vm_template_id is None:
        return None
    template = db.get(VMTemplate, vm.vm_template_id)
    if template is None:
        return None
    os_type = (template.os_type or "").strip().lower()
    if os_type.startswith("win"):
        return "windows"
    if "linux" in os_type:
        return "linux"
    return None


def suggested_presets(db: Session, service_id: int) -> tuple[str, ...]:
    """Presets to suggest the first time a client opens the firewall (Linux: SSH and ping; Windows:
    Remote Desktop and ping). Unknown operating systems get both remote access presets."""
    family = os_family(db, service_id)
    if family is None:
        return ("ssh", "rdp", "ping")
    return SUGGESTIONS[family]
