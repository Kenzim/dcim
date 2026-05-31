from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field, field_serializer
from app.core.database import get_db
from app.core.auth import require_admin
from app.dao import ServerDAO, LocationDAO, RackDAO, DiskDAO, NetworkPortDAO, ServerGroupDAO, CableRunDAO, SwitchPortDAO, SwitchBandwidthSampleDAO, NetworkSwitchDAO, ServerCapabilityDAO
from app.dao.boot_task_dao import BootTaskDAO
from app.dao.hardware_detection_report_dao import HardwareDetectionReportDAO
from app.dao.server_activity_dao import ServerActivityDAO
from app.models.server import Server
from app.models.network_switch import NetworkSwitch
from app.models.server_activity import ServerActivity, ServerActivityEventType
from app.models.hardware_detection_report import HardwareDetectionReport, HardwareDetectionReportStatus
from app.models.disk import Disk, DiskType
from app.models.network_port import NetworkPort
from app.models.cable_run import CableRun
from app.models.boot_task import BootType, BootTaskStatus
from app.plugins.registry import get_registry
from app.plugins.base import PowerState
from app.dao import ServiceInstanceDAO
from app.services.temp_os_service import get_temp_os_service
from app.services.download_token_service import get_download_token_service
from app.services.server_activity_logger import (
    log_server_activity_attempt,
    log_server_activity_success,
    log_server_activity_failure,
)
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

router = APIRouter()

# Maximum rack units a single server can occupy (sanity limit)
MAX_SERVER_RACK_UNITS = 10


def _rack_placement_overlaps(
    db: Session,
    rack_id: int,
    rack_unit: int,
    rack_units: int,
    exclude_server_id: Optional[int] = None,
) -> bool:
    """Return True if the given placement overlaps any existing server or switch in the rack.
    Uses explicit queries so we see current DB state. Switches are 1U at their rack_unit."""
    start, end = rack_unit, rack_unit + rack_units - 1
    # Check other servers
    q = db.query(Server).filter(Server.rack_id == rack_id, Server.rack_unit.isnot(None))
    if exclude_server_id is not None:
        q = q.filter(Server.id != exclude_server_id)
    for s in q.all():
        s_end = s.rack_unit + (s.rack_units or 1) - 1
        if start <= s_end and s.rack_unit <= end:
            return True
    # Check switches (each occupies rack_units U starting at rack_unit)
    for sw in db.query(NetworkSwitch).filter(
        NetworkSwitch.rack_id == rack_id,
        NetworkSwitch.rack_unit.isnot(None),
    ).all():
        sw_end = sw.rack_unit + (getattr(sw, "rack_units", 1) or 1) - 1
        if start <= sw_end and sw.rack_unit <= end:
            return True
    return False


async def _regenerate_location_dhcp_if_present(db: Session, location_id: int) -> None:
    """Regenerate and push DHCP config for this location if it has a DHCP service instance."""
    try:
        instance = ServiceInstanceDAO.get_by_location_and_type(db, location_id, "dhcp")
        if not instance:
            return
        from app.services.dhcp_config_generator import generate_dhcpd_conf
        from app.services.dhcp_config_service import get_dhcp_config_service
        from app.services.runner_client import call_dhcp_runner

        config_svc = get_dhcp_config_service()
        config = config_svc.get_config_by_service_instance(db, instance.id)
        if not config:
            config = config_svc.get_or_create_config_for_service_instance(
                db, instance.id, "/shared/dhcp/dhcpd.conf", "/shared/dhcp/dhcpd.leases"
            )
        result = generate_dhcpd_conf(config, db, location_id=location_id, return_content=True)
        if not result:
            return
        content, interface_names = result
        headers = {"X-Runner-Interfaces": ",".join(interface_names)} if interface_names else None
        code, _ = await call_dhcp_runner(instance, db, "PUT", "/config", raw_body=content.encode(), extra_headers=headers)
        if code in (200, 201):
            logger.info(f"Regenerated DHCP config for location {location_id} after server change")
    except Exception as e:
        logger.warning(f"Failed to regenerate DHCP for location {location_id}: {e}")


def convert_network_ports_to_response(db: Session, server_id: int, network_ports: List[Any]) -> List[Dict[str, Any]]:
    """Convert network ports to response dicts with cable_run (other end) when connected."""
    cable_runs = CableRunDAO.get_by_server(db, server_id)
    # Map: server_port_id -> (cable_run, other_type, other_port_id)
    port_to_cable = {}
    for cr in cable_runs:
        for server_port_id in (cr.end_a_server_port_id, cr.end_b_server_port_id):
            if server_port_id is None:
                continue
            other = cr.get_other_end(server_port_id=server_port_id)
            if other:
                other_type, other_port_id = other
                port_to_cable[server_port_id] = (cr, other_type, other_port_id)
                break

    result = []
    for port in network_ports:
        port_dict = {
            "id": port.id,
            "server_id": port.server_id,
            "name": port.name,
            "mac_address": port.mac_address,
            "speed_mbps": port.speed_mbps,
            "model": getattr(port, "model", None),
            "pci_address": getattr(port, "pci_address", None),
            "is_physical": getattr(port, "is_physical", True),
            "lag_group": port.lag_group,
            "monitor_bandwidth": getattr(port, "monitor_bandwidth", False),
            "pxe_boot": port.pxe_boot,
            "pxe_ip": port.pxe_ip,
            "description": port.description,
            "cable_run": None,
        }
        if port.id in port_to_cable:
            cr, other_type, other_port_id = port_to_cable[port.id]
            port_name = None
            device_id = None
            device_name = None
            if other_type == "switch":
                other_port = SwitchPortDAO.get_by_id(db, other_port_id)
                if other_port and other_port.switch:
                    port_name = other_port.name
                    device_id = other_port.switch_id
                    device_name = other_port.switch.name
            else:
                other_port = NetworkPortDAO.get_by_id(db, other_port_id)
                if other_port and other_port.server:
                    port_name = other_port.name
                    device_id = other_port.server_id
                    device_name = other_port.server.name
            port_dict["cable_run"] = {
                "other_end_type": other_type,
                "other_end_port_id": other_port_id,
                "other_end_port_name": port_name,
                "other_end_device_id": device_id,
                "other_end_device_name": device_name,
                "cable_run_id": cr.id,
            }
        result.append(port_dict)
    return result


def convert_disks_to_response(disks: List[Disk]) -> List[Dict[str, Any]]:
    """Convert Disk model objects to response format with lowercase type"""
    result = []
    for disk in disks:
        disk_dict = {
            "id": disk.id,
            "server_id": disk.server_id,
            "type": disk.type.value.lower() if hasattr(disk.type, 'value') else str(disk.type).lower(),
            "capacity_gb": disk.capacity_gb,
            "model": getattr(disk, "model", None),
            "description": disk.description,
            "serial_number": disk.serial_number,
            "is_os_disk": disk.is_os_disk
        }
        result.append(disk_dict)
    return result


class DiskCreate(BaseModel):
    type: str  # "ssd" or "hdd"
    capacity_gb: int
    model: str | None = None
    description: str | None = None
    serial_number: str | None = None  # Disk serial number for matching
    is_os_disk: bool = False  # Flag to mark this as the OS installation disk


class DiskResponse(BaseModel):
    id: int
    server_id: int
    type: str
    capacity_gb: int
    model: str | None
    description: str | None
    serial_number: str | None
    is_os_disk: bool

    class Config:
        from_attributes = True
    
    @field_serializer('type')
    def serialize_type(self, value: Any) -> str:
        """Convert disk type to lowercase for API response"""
        if isinstance(value, str):
            return value.lower()
        # Handle enum values (DiskType enum)
        if hasattr(value, 'value'):
            return str(value.value).lower()
        # Handle DiskType enum directly
        if isinstance(value, DiskType):
            return value.value.lower()
        return str(value).lower()


class NetworkPortCreate(BaseModel):
    name: str
    mac_address: str | None = None
    speed_mbps: int
    model: str | None = None
    pci_address: str | None = None
    is_physical: bool = True
    lag_group: str | None = None
    monitor_bandwidth: bool = False
    pxe_boot: bool = False
    pxe_ip: str | None = None
    description: str | None = None


class CableRunOtherEnd(BaseModel):
    """Other end of a cable run (for display on server/switch detail)."""
    other_end_type: str  # "switch" | "server"
    other_end_port_id: int
    other_end_port_name: str | None
    other_end_device_id: int | None
    other_end_device_name: str | None
    cable_run_id: int


class NetworkPortResponse(BaseModel):
    id: int
    server_id: int
    name: str
    mac_address: str | None
    speed_mbps: int
    model: str | None = None
    pci_address: str | None = None
    is_physical: bool = True
    lag_group: str | None
    monitor_bandwidth: bool
    pxe_boot: bool
    pxe_ip: str | None
    description: str | None
    cable_run: CableRunOtherEnd | None = None

    class Config:
        from_attributes = True


class ServerCreate(BaseModel):
    name: str
    server_ip: str
    description: str | None = None
    cpu_count: int = 1
    cpu_model: str | None = None
    ram_gb: int | None = None
    port_speed_mbps: int | None = None
    location_id: int
    rack_id: int | None = None
    rack_unit: int | None = None
    rack_units: int = 1  # Number of U the server occupies (height)
    plugin_name: str
    plugin_config: dict
    boot_mode: str = "uefi"  # "uefi" or "bios" (deprecated - use pxe_boot_mode and os_boot_mode)
    pxe_boot_mode: str = "uefi"  # "uefi" or "bios" - controls what DHCP serves initially
    os_boot_mode: str = "uefi"  # "uefi" or "bios" - controls how the server boots the installed OS
    pxe_kernel_args_general: str | None = None
    pxe_kernel_args_network: str | None = None
    disks: List[DiskCreate] = []
    network_ports: List[NetworkPortCreate] = []
    # IPMI Web Proxy configuration
    ipmi_proxy_enabled: bool = False
    ipmi_web_management_url: str | None = None
    ipmi_viewer_username: str | None = None
    ipmi_viewer_password: str | None = None
    server_group_ids: List[int] = []  # List of server group IDs to assign server to


class ServerUpdate(BaseModel):
    name: str | None = None
    server_ip: str | None = None
    description: str | None = None
    comments: str | None = None
    cpu_count: int | None = None
    cpu_model: str | None = None
    ram_gb: int | None = None
    port_speed_mbps: int | None = None
    location_id: int | None = None
    rack_id: int | None = None
    rack_unit: int | None = None
    rack_units: int | None = None  # Number of U the server occupies (height)
    plugin_name: str | None = None
    plugin_config: dict | None = None
    enabled: bool | None = None
    boot_mode: str | None = None  # "uefi" or "bios" (deprecated - use pxe_boot_mode and os_boot_mode)
    pxe_boot_mode: str | None = None  # "uefi" or "bios" - controls what DHCP serves initially
    os_boot_mode: str | None = None  # "uefi" or "bios" - controls how the server boots the installed OS
    pxe_kernel_args_general: str | None = None
    pxe_kernel_args_network: str | None = None
    disks: List[DiskCreate] | None = None
    network_ports: List[NetworkPortCreate] | None = None
    # IPMI Web Proxy configuration
    ipmi_proxy_enabled: bool | None = None
    ipmi_web_management_url: str | None = None
    ipmi_viewer_username: str | None = None
    ipmi_viewer_password: str | None = None
    server_group_ids: List[int] | None = None  # List of server group IDs to assign server to
    preview_asset_id: int | None = None  # Optional image from asset manager for server preview


class ServerResponse(BaseModel):
    id: int
    uuid: str | None = None  # For IPMI proxy URLs
    name: str
    server_ip: str
    description: str | None
    comments: str | None = None
    cpu_count: int
    cpu_model: str | None
    ram_gb: int | None
    port_speed_mbps: int | None
    location_id: int
    location_name: str | None = None  # Resolved name for display
    rack_id: int | None = None
    rack_unit: int | None = None
    rack_units: int = 1  # Number of U the server occupies (height)
    plugin_name: str
    plugin_config: dict
    enabled: bool
    boot_mode: str  # "uefi" or "bios" (deprecated - use pxe_boot_mode and os_boot_mode)
    pxe_boot_mode: str  # "uefi" or "bios" - controls what DHCP serves initially
    os_boot_mode: str  # "uefi" or "bios" - controls how the server boots the installed OS
    pxe_kernel_args_general: str | None = None
    pxe_kernel_args_network: str | None = None
    tested_capabilities: List[str] | None = None
    test_logs: str | None = None
    credentials: dict | None = None  # OS installation passwords and credentials
    disks: List[DiskResponse] = []
    network_ports: List[NetworkPortResponse] = []
    plugin_categories: List[str] = []  # Capability ids (backward compat, derived from effective_capabilities)
    effective_capabilities: List[dict] = []  # Full capability definitions with UI schema for config-driven rendering
    server_groups: List[dict] = []  # Server groups this server belongs to
    # IPMI Web Proxy configuration
    ipmi_proxy_enabled: bool
    ipmi_web_management_url: str | None
    ipmi_viewer_username: str | None
    ipmi_viewer_password: str | None = None  # Don't expose password in responses
    preview_asset_id: int | None = None

    class Config:
        from_attributes = True


class ServerTestRequest(BaseModel):
    plugin_name: str
    plugin_config: dict


class ServerCapabilitiesUpdateRequest(BaseModel):
    capability_states: Dict[str, bool]


class ServerBootSetRequest(BaseModel):
    device: str
    persistent: bool = False
    uefi: bool | None = None


class ServerKernelArgsPreviewRequest(BaseModel):
    temp_os_id: str = "debian-live"
    pxe_kernel_args_general: str | None = None
    pxe_kernel_args_network: str | None = None


def _build_server_capabilities_payload(db: Session, server: Server) -> Dict[str, Any]:
    registry = get_registry()
    plugin_class = registry.get_plugin_class(server.plugin_name)
    if not plugin_class or not getattr(plugin_class, "CAPABILITIES", []):
        return {
            "server_id": server.id,
            "plugin_name": server.plugin_name,
            "declared_capabilities": [],
            "effective_capabilities": [],
            "capability_states": {},
        }
    rows = ServerCapabilityDAO.list_by_server(db, server.id)
    override_enabled = {row.capability_id: bool(row.enabled) for row in rows}
    declared_caps = []
    capability_states: Dict[str, bool] = {}
    for cap in plugin_class.CAPABILITIES:
        enabled = override_enabled.get(cap.id, not cap.optional)
        capability_states[cap.id] = enabled
        cap_dict = cap.to_dict()
        cap_dict["enabled"] = enabled
        cap_dict["overridden"] = cap.id in override_enabled
        declared_caps.append(cap_dict)
    effective_caps = [c for c in declared_caps if c.get("enabled")]
    return {
        "server_id": server.id,
        "plugin_name": server.plugin_name,
        "declared_capabilities": declared_caps,
        "effective_capabilities": effective_caps,
        "capability_states": capability_states,
    }


class ServerActivityResponse(BaseModel):
    id: int
    server_id: int
    event_type: str
    action: str
    status: str
    message: str
    source: str
    details: Dict[str, Any] | None = None
    created_at: str | None = None

    @classmethod
    def from_model(cls, entry: ServerActivity) -> "ServerActivityResponse":
        return cls(
            id=entry.id,
            server_id=entry.server_id,
            event_type=entry.event_type.value,
            action=entry.action,
            status=entry.status.value,
            message=entry.message,
            source=entry.source,
            details=entry.details or {},
            created_at=entry.created_at.isoformat() if entry.created_at else None,
        )


class HardwareDetectionRunResponse(BaseModel):
    report_id: int
    boot_task_id: int
    status: str


class HardwareDetectionReportResponse(BaseModel):
    id: int
    server_id: int
    boot_task_id: int | None
    status: str
    source_ip: str | None
    detected_inventory: Dict[str, Any] | None
    created_by_user_id: int | None
    reviewed_by_user_id: int | None
    submitted_at: str | None
    applied_at: str | None
    rejected_at: str | None
    created_at: str | None

    @classmethod
    def from_model(cls, report: HardwareDetectionReport) -> "HardwareDetectionReportResponse":
        return cls(
            id=report.id,
            server_id=report.server_id,
            boot_task_id=report.boot_task_id,
            status=report.status.value,
            source_ip=report.source_ip,
            detected_inventory=report.detected_inventory or {},
            created_by_user_id=report.created_by_user_id,
            reviewed_by_user_id=report.reviewed_by_user_id,
            submitted_at=report.submitted_at.isoformat() if report.submitted_at else None,
            applied_at=report.applied_at.isoformat() if report.applied_at else None,
            rejected_at=report.rejected_at.isoformat() if report.rejected_at else None,
            created_at=report.created_at.isoformat() if report.created_at else None,
        )


class HardwareDetectionApplyRequest(BaseModel):
    nic_remap: Dict[str, int] = Field(default_factory=dict)  # old_port_id -> detected_nic_index
    notes: str | None = None


def _normalize_mac(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    mac = value.replace("-", ":").replace(".", "").strip().lower()
    if ":" not in mac and len(mac) == 12:
        mac = ":".join(mac[i : i + 2] for i in range(0, 12, 2))
    return mac


def _disk_type_from_detected(disk: Dict[str, Any]) -> DiskType:
    disk_type = str(disk.get("type") or "").lower()
    return DiskType.HDD if disk_type == "hdd" else DiskType.SSD


def _compute_hardware_detection_diff(
    server: Server,
    current_disks: List[Disk],
    current_ports: List[NetworkPort],
    detected: Dict[str, Any],
) -> Dict[str, Any]:
    cpu_detected = (detected.get("cpu") or {})
    current_server = {
        "cpu_count": server.cpu_count,
        "cpu_model": server.cpu_model,
        "ram_gb": server.ram_gb,
    }
    detected_server = {
        "cpu_count": cpu_detected.get("count"),
        "cpu_model": cpu_detected.get("model"),
        "ram_gb": detected.get("ram_gb"),
    }

    current_disks_view = [
        {
            "id": d.id,
            "serial_number": d.serial_number,
            "model": getattr(d, "model", None),
            "capacity_gb": d.capacity_gb,
            "type": d.type.value.lower() if hasattr(d.type, "value") else str(d.type).lower(),
            "is_os_disk": d.is_os_disk,
        }
        for d in current_disks
    ]
    detected_disks_view = detected.get("disks") or []

    current_nics_view = [
        {
            "id": p.id,
            "name": p.name,
            "mac_address": _normalize_mac(p.mac_address),
            "speed_mbps": p.speed_mbps,
            "model": getattr(p, "model", None),
            "pci_address": getattr(p, "pci_address", None),
            "is_physical": getattr(p, "is_physical", True),
        }
        for p in current_ports
        if getattr(p, "is_physical", True)
    ]
    detected_nics_view = [n for n in (detected.get("nics") or []) if n.get("is_physical", True) and n.get("pci_address")]

    return {
        "server": {
            "current": current_server,
            "detected": detected_server,
            "changed": current_server != detected_server,
        },
        "disks": {
            "current": current_disks_view,
            "detected": detected_disks_view,
        },
        "nics": {
            "current": current_nics_view,
            "detected": detected_nics_view,
        },
    }


def _build_hardware_detection_script(base_url: str, report_token: str) -> str:
    safe_base = (base_url or "").rstrip("/")
    return f"""#!/bin/bash
set -euo pipefail
LOG_FILE="/tmp/rackflow-hardware-detection.log"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "Starting hardware detection"
API_BASE="{safe_base}"
REPORT_TOKEN="{report_token}"

tmp_report="$(mktemp)"

python3 - <<'PY' > "$tmp_report"
import json
import os
import re
from pathlib import Path

def read(path, default=None):
    try:
        return Path(path).read_text().strip()
    except Exception:
        return default

cpu_count = None
cpu_model = None
cpuinfo = read("/proc/cpuinfo", "") or ""
physical_ids = set()
for line in cpuinfo.splitlines():
    if "physical id" in line:
        physical_ids.add(line.split(":", 1)[1].strip())
    if "model name" in line and cpu_model is None:
        cpu_model = line.split(":", 1)[1].strip()
if physical_ids:
    cpu_count = len(physical_ids)

ram_gb = None
try:
    import subprocess
    out = subprocess.check_output(["dmidecode", "-t", "memory"], text=True, timeout=5)
    total_mb = 0
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Size:") and "No Module" not in line and "Unknown" not in line:
            m = re.search(r"Size:\\s*(\\d+)\\s*(MB|GB)", line, re.I)
            if m:
                val, unit = int(m.group(1)), (m.group(2) or "").upper()
                total_mb += val if unit == "MB" else val * 1024
    if total_mb > 0:
        ram_gb = max(1, int(round(total_mb / 1024)))
except Exception:
    pass
if ram_gb is None:
    meminfo = read("/proc/meminfo", "") or ""
    for line in meminfo.splitlines():
        if line.startswith("MemTotal:"):
            kb = int(re.findall(r"\\d+", line)[0])
            ram_gb = max(1, int(round(kb / 1024 / 1024)))
            break

disks = []
for dev in sorted(Path("/sys/block").glob("*")):
    name = dev.name
    if name.startswith(("loop", "ram", "sr", "fd", "dm-", "zram")):
        continue
    if not (Path("/dev") / name).exists():
        continue
    rotational = read(dev / "queue/rotational", "1")
    dtype = "ssd" if rotational == "0" else "hdd"
    size_sectors = read(dev / "size", "0")
    try:
        # Report size in decimal GB (10^9), not GiB
        bytes_total = int(size_sectors) * 512
        capacity_gb = int(round(bytes_total / 1_000_000_000))
    except Exception:
        capacity_gb = None
    serial = read(dev / "device/serial")
    model = read(dev / "device/model")
    disks.append({{
        "name": name,
        "serial_number": serial,
        "model": model,
        "capacity_gb": capacity_gb,
        "type": dtype,
        "is_os_disk": False
    }})

nics = []
for iface in sorted(Path("/sys/class/net").glob("*")):
    name = iface.name
    if name == "lo":
        continue
    device_path = (iface / "device")
    if not device_path.exists():
        continue
    resolved = os.path.realpath(device_path)
    if "/pci" not in resolved or "usb" in resolved.lower():
        continue
    mac = read(iface / "address")
    pci_address = os.path.basename(resolved)
    model = read(device_path / "uevent", "")
    speed_mbps = None
    try:
        import subprocess
        out = subprocess.check_output(["ethtool", name], text=True, timeout=2, stderr=subprocess.DEVNULL)
        lines = out.splitlines()
        in_modes = False
        for line in lines:
            if "Supported link modes" in line or "Advertised link modes" in line:
                in_modes = True
                continue
            if in_modes:
                # Indented lines list the actual modes; stop when the block ends
                if not line.startswith(" "):
                    in_modes = False
                    continue
                for part in re.findall(r"(\\d+)base", line):
                    val = int(part)
                    if val == 2500:
                        val = 2000
                    if speed_mbps is None or val > speed_mbps:
                        speed_mbps = val
        if speed_mbps is None:
            for line in lines:
                if line.strip().startswith("Speed:"):
                    m = re.search(r"(\\d+)\\s*Mb/s", line)
                    if m:
                        speed_mbps = int(m.group(1))
                        if speed_mbps == 2500:
                            speed_mbps = 2000
                    break
    except Exception:
        pass
    if speed_mbps is None:
        speed = read(iface / "speed")
        try:
            if speed and speed not in ("unknown", "-1"):
                speed_mbps = int(speed)
                if speed_mbps == 2500:
                    speed_mbps = 2000
        except Exception:
            pass
    nics.append({{
        "name": name,
        "mac_address": mac,
        "speed_mbps": speed_mbps,
        "model": model.splitlines()[0] if model else None,
        "pci_address": pci_address,
        "is_physical": True
    }})

def speed_tier(mbps):
    if mbps is None or mbps <= 0:
        return "1g"
    if mbps >= 10000:
        return "10g"
    if mbps >= 2000:
        return "2g"
    return "1g"

nics.sort(key=lambda n: (-(n["speed_mbps"] or 0), n.get("pci_address") or ""))
tier_index = {{}}
for nic in nics:
    tier = speed_tier(nic.get("speed_mbps"))
    idx = tier_index.get(tier, 0)
    tier_index[tier] = idx + 1
    nic["name"] = "en" + tier + str(idx)

print(json.dumps({{
    "cpu_count": cpu_count,
    "cpu_model": cpu_model,
    "ram_gb": ram_gb,
    "nics": nics,
    "disks": disks,
    "detector_version": "v1"
}}))
PY

curl -f -sS -X POST "$API_BASE/api/servers/interaction/hardware-detection/report?token=$REPORT_TOKEN" \\
  -H "Content-Type: application/json" \\
  --data-binary "@$tmp_report"

echo "Hardware detection report uploaded"
"""


_BOOT_ORDER_FIX_SCRIPT_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "finalize-pxe-bootorder.sh"


def _apply_hardware_detection_report(
    db: Session,
    server: Server,
    report: HardwareDetectionReport,
    nic_remap: Dict[str, int],
) -> Dict[str, Any]:
    detected = report.detected_inventory or {}
    detected_cpu = detected.get("cpu") or {}
    detected_ram_gb = detected.get("ram_gb")
    detected_nics = [n for n in (detected.get("nics") or []) if n.get("is_physical", True) and n.get("pci_address")]
    detected_disks = detected.get("disks") or []

    if detected_cpu.get("count"):
        server.cpu_count = int(detected_cpu["count"])
    if detected_cpu.get("model"):
        server.cpu_model = str(detected_cpu["model"]).strip()
    if detected_ram_gb:
        server.ram_gb = int(detected_ram_gb)

    current_ports = NetworkPortDAO.get_by_server(db, server.id)
    current_physical_ports = [p for p in current_ports if getattr(p, "is_physical", True)]
    by_pci = {str(getattr(p, "pci_address", "")).lower(): p for p in current_physical_ports if getattr(p, "pci_address", None)}
    by_mac = {(_normalize_mac(p.mac_address) or ""): p for p in current_physical_ports if p.mac_address}

    target_by_index: Dict[int, NetworkPort] = {}
    target_ids = set()
    for idx, nic in enumerate(detected_nics):
        pci = str(nic.get("pci_address") or "").strip().lower()
        mac = _normalize_mac(nic.get("mac_address"))
        matched = by_pci.get(pci) or (by_mac.get(mac) if mac else None)
        nic_name = str(nic.get("name") or f"eth{idx}").strip()
        nic_speed = nic.get("speed_mbps") if nic.get("speed_mbps") else 1000
        if matched:
            matched.name = nic_name
            matched.mac_address = mac
            matched.speed_mbps = int(nic_speed)
            matched.model = nic.get("model")
            matched.pci_address = pci or None
            matched.is_physical = True
            target = matched
        else:
            target = NetworkPort(
                server_id=server.id,
                name=nic_name,
                mac_address=mac,
                speed_mbps=int(nic_speed),
                model=nic.get("model"),
                pci_address=pci or None,
                is_physical=True,
                monitor_bandwidth=True,
                pxe_boot=False,
            )
            db.add(target)
            db.flush()
        target_by_index[idx] = target
        target_ids.add(target.id)

    for old_port_id_str, detected_idx in (nic_remap or {}).items():
        try:
            old_port_id = int(old_port_id_str)
            idx = int(detected_idx)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"Invalid NIC remap entry: {old_port_id_str}->{detected_idx}")
        target_port = target_by_index.get(idx)
        if not target_port:
            raise HTTPException(status_code=400, detail=f"Invalid detected NIC index in remap: {idx}")
        old_port = NetworkPortDAO.get_by_id(db, old_port_id)
        if not old_port or old_port.server_id != server.id:
            raise HTTPException(status_code=400, detail=f"Invalid old NIC id in remap: {old_port_id}")
        for cr in CableRunDAO.get_by_server(db, server.id):
            if cr.end_a_server_port_id == old_port_id:
                cr.end_a_server_port_id = target_port.id
            if cr.end_b_server_port_id == old_port_id:
                cr.end_b_server_port_id = target_port.id

    stale_ports = [p for p in current_physical_ports if p.id not in target_ids]
    for stale in stale_ports:
        if CableRunDAO.get_by_server_port(db, stale.id):
            mapped = str(stale.id) in (nic_remap or {})
            if not mapped:
                raise HTTPException(
                    status_code=409,
                    detail=f"Physical NIC '{stale.name}' is connected. Provide a remap before applying.",
                )
        db.delete(stale)

    current_disks = DiskDAO.get_by_server(db, server.id)
    by_serial = {str(d.serial_number).strip(): d for d in current_disks if d.serial_number}
    target_disk_ids = set()
    for disk in detected_disks:
        serial = (disk.get("serial_number") or "").strip() or None
        model = (disk.get("model") or "").strip() or None
        capacity_gb = int(disk.get("capacity_gb") or 0)
        if capacity_gb <= 0:
            continue
        disk_type = _disk_type_from_detected(disk)
        existing = by_serial.get(serial) if serial else None
        desc = model if model else (disk.get("name") or "").strip() or None
        if existing:
            existing.capacity_gb = capacity_gb
            existing.model = model
            existing.type = disk_type
            existing.description = desc
            existing.is_os_disk = bool(disk.get("is_os_disk"))
            target_disk_ids.add(existing.id)
        else:
            new_disk = Disk(
                server_id=server.id,
                type=disk_type,
                capacity_gb=capacity_gb,
                model=model,
                description=desc,
                serial_number=serial,
                is_os_disk=bool(disk.get("is_os_disk")),
            )
            db.add(new_disk)
            db.flush()
            target_disk_ids.add(new_disk.id)

    for d in current_disks:
        if d.id not in target_disk_ids:
            db.delete(d)

    db.commit()
    return {
        "updated_cpu_count": server.cpu_count,
        "updated_ram_gb": server.ram_gb,
        "updated_nic_count": len(target_ids),
        "updated_disk_count": len(target_disk_ids),
    }


def _get_effective_capabilities_for_server(db: Session, server: Server) -> List[Dict[str, Any]]:
    """Compute effective capabilities from plugin CAPABILITIES and SQL capability states."""
    registry = get_registry()
    plugin_class = registry.get_plugin_class(server.plugin_name)
    if not plugin_class or not getattr(plugin_class, "CAPABILITIES", []):
        return []
    override_rows = ServerCapabilityDAO.list_by_server(db, server.id)
    override_enabled = {row.capability_id: bool(row.enabled) for row in override_rows}
    effective: List[Dict[str, Any]] = []
    for cap in plugin_class.CAPABILITIES:
        enabled = override_enabled.get(cap.id, not cap.optional)
        if enabled:
            effective.append(cap.to_dict())
    return effective


def _server_has_capability(db: Session, server: Server, capability_id: str) -> bool:
    """Check if server has a specific capability enabled."""
    registry = get_registry()
    plugin_class = registry.get_plugin_class(server.plugin_name)
    if not plugin_class or not getattr(plugin_class, "CAPABILITIES", []):
        return False
    row = ServerCapabilityDAO.get_by_server_and_capability(db, server.id, capability_id)
    for cap in plugin_class.CAPABILITIES:
        if cap.id == capability_id:
            if row is not None:
                return bool(row.enabled)
            return not cap.optional
    return False


@router.post("/test", response_model=dict)
async def test_server_connection(
    test_data: ServerTestRequest,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Test server connection using plugin's test_connection method"""
    # Get plugin instance from registry
    registry = get_registry()
    try:
        plugin_instance = registry.get_plugin(test_data.plugin_name, test_data.plugin_config)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plugin '{test_data.plugin_name}' not found in registry"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to instantiate plugin: {str(e)}"
        )
    
    # Call test_connection
    try:
        result = await plugin_instance.test_connection()
        return result
    except Exception as e:
        return {
            "success": False,
            "message": f"Connection test failed: {str(e)}",
            "details": {"error": str(e)}
        }


@router.post("/{server_id}/test-capabilities", response_model=dict)
async def test_server_capabilities(
    server_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """
    Return effective capabilities for a server (declaration-only, no probing).
    
    Capabilities are declared by the plugin and resolved via server_capabilities SQL overrides.
    """
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Server not found"
        )
    effective = _get_effective_capabilities_for_server(db, server)
    return {
        "success": True,
        "server_id": server_id,
        "server_name": server.name,
        "effective_capabilities": effective,
        "message": "Capabilities are declared by plugins and resolved by SQL capability states.",
    }


@router.get("/{server_id}/capabilities", response_model=dict)
async def get_server_capabilities(
    server_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Server not found")
    return _build_server_capabilities_payload(db, server)


@router.post("/{server_id}/capabilities", response_model=dict)
async def update_server_capabilities(
    server_id: int,
    payload: ServerCapabilitiesUpdateRequest,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Server not found")

    registry = get_registry()
    plugin_class = registry.get_plugin_class(server.plugin_name)
    declared_ids = {
        cap.id for cap in getattr(plugin_class, "CAPABILITIES", []) or []
    }
    unknown = [cap_id for cap_id in payload.capability_states.keys() if cap_id not in declared_ids]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown capabilities for plugin '{server.plugin_name}': {', '.join(sorted(unknown))}",
        )

    ServerCapabilityDAO.set_capability_states(
        db=db,
        server_id=server.id,
        capability_states=payload.capability_states,
        source="manual",
    )
    return _build_server_capabilities_payload(db, server)


@router.get("/", response_model=List[ServerResponse])
async def list_servers(
    skip: int = 0,
    limit: int = 100,
    enabled_only: bool = False,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """List all servers"""
    servers = ServerDAO.get_all(db, skip=skip, limit=limit, enabled_only=enabled_only)
    result = []
    for server in servers:
        disks = DiskDAO.get_by_server(db, server.id)
        effective = _get_effective_capabilities_for_server(db, server)
        plugin_categories = [c["id"] for c in effective]
        
        network_ports = NetworkPortDAO.get_by_server(db, server.id)
        server_dict = {k: v.value if hasattr(v, 'value') else v for k, v in server.__dict__.items()}
        # Don't expose password in responses
        server_dict["ipmi_viewer_password"] = None
        # Normalize rack_units: DB may have NULL for servers created before column existed
        if server_dict.get("rack_units") is None:
            server_dict["rack_units"] = 1
        
        # Resolve location name for display
        location = LocationDAO.get_by_id(db, server.location_id)
        server_dict["location_name"] = location.name if location else None
        
        # Get server groups
        server_groups = []
        if server.server_groups:
            for group in server.server_groups:
                server_groups.append({
                    "id": group.id,
                    "name": group.name,
                    "description": group.description
                })
        
        result.append({
            **server_dict,
            "disks": convert_disks_to_response(disks),
            "network_ports": network_ports,
            "plugin_categories": plugin_categories,
            "effective_capabilities": effective,
            "server_groups": server_groups
        })
    return result


@router.post("/", response_model=ServerResponse, status_code=status.HTTP_201_CREATED)
async def create_server(
    server_data: ServerCreate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Create a new server"""
    try:
        logger.info(f"Creating server: name={server_data.name}, plugin_name={server_data.plugin_name}, location_id={server_data.location_id}")
        logger.debug(f"Server data: disks={len(server_data.disks) if server_data.disks else 0}, network_ports={len(server_data.network_ports) if server_data.network_ports else 0}")
    except Exception as e:
        logger.warning(f"Error logging server creation request: {e}")
    
    try:
        # Validate plugin exists
        # Plugin validation already done above
        
        # Validate location (required)
        location = LocationDAO.get_by_id(db, server_data.location_id)
        if not location:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Location not found"
            )
        
        rack_units_create = getattr(server_data, "rack_units", 1) or 1
        if rack_units_create < 1 or rack_units_create > MAX_SERVER_RACK_UNITS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"rack_units must be between 1 and {MAX_SERVER_RACK_UNITS}"
            )

        # Validate rack if provided
        if server_data.rack_id is not None:
            rack = RackDAO.get_by_id(db, server_data.rack_id)
            if not rack:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Rack not found"
                )
            # Ensure rack is in the same location
            if rack.location_id != server_data.location_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Rack must be in the same location as the server"
                )
            if rack_units_create > rack.units:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"rack_units ({rack_units_create}) exceeds rack height ({rack.units}U)"
                )
            if server_data.rack_unit is not None:
                if server_data.rack_unit < 1 or server_data.rack_unit > rack.units:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Rack unit must be between 1 and {rack.units}"
                    )
                if server_data.rack_unit + rack_units_create - 1 > rack.units:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Server (U{server_data.rack_unit}-{server_data.rack_unit + rack_units_create - 1}) would extend past rack height ({rack.units}U)"
                    )
                if _rack_placement_overlaps(db, server_data.rack_id, server_data.rack_unit, rack_units_create):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Rack position U{server_data.rack_unit}-{server_data.rack_unit + rack_units_create - 1} overlaps another server in this rack"
                    )
            # If rack_unit is provided but rack_id is not, that's invalid
            if server_data.rack_unit is not None and server_data.rack_id is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="rack_unit requires rack_id"
                )

        # Check if server with same name already exists
        existing = ServerDAO.get_by_name(db, server_data.name)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Server with this name already exists"
            )
        
        # Convert boot_mode strings to enums
        try:
            from app.models.server import BootMode
            boot_mode = BootMode(server_data.boot_mode.lower())
            # Handle new pxe_boot_mode and os_boot_mode fields
            pxe_boot_mode = None
            os_boot_mode = None
            if hasattr(server_data, 'pxe_boot_mode') and server_data.pxe_boot_mode:
                pxe_boot_mode = BootMode(server_data.pxe_boot_mode.lower())
            if hasattr(server_data, 'os_boot_mode') and server_data.os_boot_mode:
                os_boot_mode = BootMode(server_data.os_boot_mode.lower())
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid boot_mode: {str(e)}. Must be 'uefi' or 'bios'"
            )
        
        # Create server
        server = ServerDAO.create(
            db,
            name=server_data.name,
            server_ip=server_data.server_ip,
            description=server_data.description,
            cpu_count=server_data.cpu_count,
            cpu_model=server_data.cpu_model,
            ram_gb=server_data.ram_gb,
            port_speed_mbps=server_data.port_speed_mbps,
            location_id=server_data.location_id,
            rack_id=server_data.rack_id,
            rack_unit=server_data.rack_unit,
            rack_units=rack_units_create,
            plugin_name=server_data.plugin_name,
            plugin_config=server_data.plugin_config,
            boot_mode=boot_mode,
            pxe_boot_mode=pxe_boot_mode,
            os_boot_mode=os_boot_mode,
            pxe_kernel_args_general=server_data.pxe_kernel_args_general.strip() if server_data.pxe_kernel_args_general and server_data.pxe_kernel_args_general.strip() else None,
            pxe_kernel_args_network=server_data.pxe_kernel_args_network.strip() if server_data.pxe_kernel_args_network and server_data.pxe_kernel_args_network.strip() else None,
            ipmi_proxy_enabled=server_data.ipmi_proxy_enabled,
            ipmi_web_management_url=server_data.ipmi_web_management_url,
            ipmi_viewer_username=server_data.ipmi_viewer_username,
            ipmi_viewer_password=server_data.ipmi_viewer_password
        )
        
        # Create disks
        if server_data.disks:
            for disk_data in server_data.disks:
                try:
                    # Convert to uppercase to match enum values (SSD, HDD)
                    disk_type = DiskType(disk_data.type.upper())
                except ValueError as e:
                    logger.error(f"Invalid disk type: {disk_data.type}. Error: {e}")
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Invalid disk type: {disk_data.type}. Must be 'ssd' or 'hdd'"
                    )
                
                DiskDAO.create(
                    db,
                    server_id=server.id,
                    type=disk_type,
                    capacity_gb=disk_data.capacity_gb,
                    model=disk_data.model,
                    description=disk_data.description,
                    serial_number=disk_data.serial_number,
                    is_os_disk=disk_data.is_os_disk
                )
        
        # Create network ports
        if server_data.network_ports:
            for port_data in server_data.network_ports:
                NetworkPortDAO.create(
                    db,
                    server_id=server.id,
                    name=port_data.name,
                    mac_address=port_data.mac_address,
                    speed_mbps=port_data.speed_mbps,
                    model=port_data.model,
                    pci_address=port_data.pci_address,
                    is_physical=port_data.is_physical,
                    lag_group=port_data.lag_group,
                    monitor_bandwidth=port_data.monitor_bandwidth,
                    pxe_boot=port_data.pxe_boot,
                    pxe_ip=port_data.pxe_ip,
                    description=port_data.description
                )
        
        # Assign server to groups
        if server_data.server_group_ids:
            for group_id in server_data.server_group_ids:
                group = ServerGroupDAO.get_by_id(db, group_id)
                if group:
                    group.servers.append(server)
            db.commit()
        
        # Refresh to get disks and network ports (with cable_run info for "Connected To")
        db.refresh(server)
        disks = DiskDAO.get_by_server(db, server.id)
        network_ports = NetworkPortDAO.get_by_server(db, server.id)
        network_ports_with_cables = convert_network_ports_to_response(db, server.id, network_ports)
        
        # Build response, excluding password
        server_dict = {k: v.value if hasattr(v, 'value') else v for k, v in server.__dict__.items()}
        server_dict["ipmi_viewer_password"] = None
        if server_dict.get("rack_units") is None:
            server_dict["rack_units"] = 1
        
        effective = _get_effective_capabilities_for_server(db, server)
        plugin_categories = [c["id"] for c in effective]
        
        # Regenerate per-location DHCP if this location has a runner
        if server.location_id:
            await _regenerate_location_dhcp_if_present(db, server.location_id)
        
        # Get server groups
        server_groups = []
        if server.server_groups:
            for group in server.server_groups:
                server_groups.append({
                    "id": group.id,
                    "name": group.name,
                    "description": group.description
                })
        
        return {
            **server_dict,
            "disks": convert_disks_to_response(disks),
            "network_ports": network_ports_with_cables,
            "plugin_categories": plugin_categories,
            "effective_capabilities": effective,
            "server_groups": server_groups
        }
    except HTTPException:
        # Re-raise HTTP exceptions as-is
        raise
    except Exception as e:
        logger.error(f"Unexpected error creating server: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error creating server: {str(e)}"
        )


@router.get("/{server_id}", response_model=ServerResponse)
async def get_server(
    server_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Get a server by ID"""
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Server not found"
        )
    
    disks = DiskDAO.get_by_server(db, server.id)
    network_ports = NetworkPortDAO.get_by_server(db, server.id)
    network_ports_with_cables = convert_network_ports_to_response(db, server.id, network_ports)
    
    effective = _get_effective_capabilities_for_server(db, server)
    plugin_categories = [c["id"] for c in effective]
    
    server_dict = {k: v.value if hasattr(v, 'value') else v for k, v in server.__dict__.items()}
    # Don't expose password in responses
    server_dict["ipmi_viewer_password"] = None
    if server_dict.get("rack_units") is None:
        server_dict["rack_units"] = 1
    
    # Resolve location and rack names for display
    location = LocationDAO.get_by_id(db, server.location_id)
    location_name = location.name if location else None
    rack_name = None
    if server.rack_id:
        rack = RackDAO.get_by_id(db, server.rack_id)
        rack_name = rack.name if rack else None
    elif server.rack_unit is not None and server.location_id:
        racks_in_location = RackDAO.get_by_location(db, server.location_id)
        if len(racks_in_location) == 1:
            only_rack = racks_in_location[0]
            rack_name = only_rack.name
            # Repair data: set rack_id so future loads and list views show the rack
            server.rack_id = only_rack.id
            ServerDAO.update(db, server)
            server_dict["rack_id"] = only_rack.id

    # Get server groups
    server_groups = []
    if server.server_groups:
        for group in server.server_groups:
            server_groups.append({
                "id": group.id,
                "name": group.name,
                "description": group.description
            })

    return {
        **server_dict,
        "location_name": location_name,
        "rack_name": rack_name,
        "disks": convert_disks_to_response(disks),
        "network_ports": network_ports_with_cables,
        "plugin_categories": plugin_categories,
        "effective_capabilities": effective,
        "server_groups": server_groups
    }


@router.post("/{server_id}/hardware-detection/run", response_model=HardwareDetectionRunResponse)
async def run_hardware_detection(
    server_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Queue built-in hardware detection boot workflow for a server."""
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Server not found")

    pxe_port = NetworkPortDAO.get_pxe_boot_port(db, server_id)
    if not pxe_port:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Server has no PXE boot port configured",
        )

    from app.api.server_interaction import _get_base_url_for_pxe_ip

    base_url = _get_base_url_for_pxe_ip(db, pxe_port.pxe_ip if pxe_port else None)
    temp_os_service = get_temp_os_service()
    os_config = temp_os_service.get_os_config("debian-live")
    if not os_config:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="debian-live temporary OS not found",
        )

    kernel_url = temp_os_service.get_kernel_url("debian-live", base_url)
    initrd_url = temp_os_service.get_initrd_url("debian-live", base_url)
    kernel_params = temp_os_service.get_kernel_params("debian-live")
    squashfs_url = temp_os_service.get_squashfs_url("debian-live", base_url)
    if squashfs_url and "fetch=" not in kernel_params:
        kernel_params = f"{kernel_params} fetch={squashfs_url}"

    log_server_activity_attempt(
        db,
        server_id=server.id,
        event_type=ServerActivityEventType.INSTALL,
        action="hardware_detection",
        source="admin_api",
        message="Hardware detection requested",
        details={"requested_by_user_id": auth.get("user_id")},
    )

    boot_task = BootTaskDAO.create(
        db=db,
        server_id=server.id,
        boot_type=BootType.TEMP_OS,
        temp_os_id="debian-live",
        kernel_url=kernel_url,
        initrd_url=initrd_url,
        kernel_params=kernel_params,
        script_content="#!/bin/bash\necho 'Initializing hardware detection workflow'\n",
        description="Run hardware detection",
    )

    report = HardwareDetectionReportDAO.create(
        db,
        server_id=server.id,
        created_by_user_id=auth.get("user_id"),
        boot_task_id=boot_task.id,
        status=HardwareDetectionReportStatus.PENDING,
    )

    token_service = get_download_token_service()
    report_token = token_service.generate_token(
        boot_task_id=boot_task.id,
        allowed_patterns=["hardware-detection-report-*"],
        expires_in=3600,
        single_use=False,
    )
    boot_task.script_content = _build_hardware_detection_script(base_url, report_token)
    db.commit()
    db.refresh(boot_task)

    log_server_activity_success(
        db,
        server_id=server.id,
        event_type=ServerActivityEventType.INSTALL,
        action="hardware_detection",
        source="admin_api",
        message="Hardware detection boot task queued",
        details={"report_id": report.id, "boot_task_id": boot_task.id},
    )

    return HardwareDetectionRunResponse(
        report_id=report.id,
        boot_task_id=boot_task.id,
        status=report.status.value,
    )


@router.post("/{server_id}/boot/fix-boot-order", response_model=dict)
async def queue_boot_order_fix(
    server_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Queue a one-time boot-order correction task.

    This boots Debian Live with the finalize-pxe-bootorder.sh script so the
    next PXE boot can reorder UEFI BootOrder and set BootNext appropriately.
    """
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Server not found")

    if not _BOOT_ORDER_FIX_SCRIPT_PATH.exists():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Boot order fix script not found on server",
        )

    pxe_port = NetworkPortDAO.get_pxe_boot_port(db, server_id)
    if not pxe_port:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Server has no PXE boot port configured",
        )

    from app.api.server_interaction import _get_base_url_for_pxe_ip

    base_url = _get_base_url_for_pxe_ip(db, pxe_port.pxe_ip if pxe_port else None)
    temp_os_service = get_temp_os_service()
    os_config = temp_os_service.get_os_config("debian-live")
    if not os_config:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="debian-live temporary OS not found",
        )

    kernel_url = temp_os_service.get_kernel_url("debian-live", base_url)
    initrd_url = temp_os_service.get_initrd_url("debian-live", base_url)
    kernel_params = temp_os_service.get_kernel_params("debian-live")
    squashfs_url = temp_os_service.get_squashfs_url("debian-live", base_url)
    if squashfs_url and "fetch=" not in kernel_params:
        kernel_params = f"{kernel_params} fetch={squashfs_url}"

    try:
        script_content = _BOOT_ORDER_FIX_SCRIPT_PATH.read_text()
        script_content = "export AUTO_REBOOT=0\n" + script_content
    except Exception as exc:  # pragma: no cover
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to read boot order fix script: {exc}",
        )

    log_server_activity_attempt(
        db,
        server_id=server.id,
        event_type=ServerActivityEventType.INSTALL,
        action="boot_order_fix",
        source="admin_api",
        message="Boot order correction requested",
        details={"requested_by_user_id": auth.get("user_id")},
    )

    boot_task = BootTaskDAO.create(
        db=db,
        server_id=server.id,
        boot_type=BootType.TEMP_OS,
        temp_os_id="debian-live",
        kernel_url=kernel_url,
        initrd_url=initrd_url,
        kernel_params=kernel_params,
        iso_url=None,
        script_url=None,
        script_content=script_content,
        description="Boot order correction (Debian Live)",
    )

    log_server_activity_success(
        db,
        server_id=server.id,
        event_type=ServerActivityEventType.INSTALL,
        action="boot_order_fix",
        source="admin_api",
        message="Boot order correction queued",
        details={"boot_task_id": boot_task.id},
    )

    return {
        "status": "queued",
        "boot_task_id": boot_task.id,
    }


@router.get("/{server_id}/hardware-detection/reports", response_model=List[HardwareDetectionReportResponse])
async def list_hardware_detection_reports(
    server_id: int,
    status_filter: Optional[str] = None,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Server not found")

    status_enum = None
    if status_filter:
        try:
            status_enum = HardwareDetectionReportStatus(status_filter.lower())
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid status_filter")
    reports = HardwareDetectionReportDAO.list_by_server(db, server_id, status=status_enum, limit=100)
    return [HardwareDetectionReportResponse.from_model(r) for r in reports]


@router.get("/{server_id}/hardware-detection/reports/{report_id}", response_model=HardwareDetectionReportResponse)
async def get_hardware_detection_report(
    server_id: int,
    report_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    report = HardwareDetectionReportDAO.get_by_id(db, report_id)
    if not report or report.server_id != server_id:
        raise HTTPException(status_code=404, detail="Hardware detection report not found")
    return HardwareDetectionReportResponse.from_model(report)


@router.get("/{server_id}/hardware-detection/reports/{report_id}/diff", response_model=dict)
async def get_hardware_detection_diff(
    server_id: int,
    report_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    report = HardwareDetectionReportDAO.get_by_id(db, report_id)
    if not report or report.server_id != server_id:
        raise HTTPException(status_code=404, detail="Hardware detection report not found")
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    current_disks = DiskDAO.get_by_server(db, server_id)
    current_ports = NetworkPortDAO.get_by_server(db, server_id)
    diff = _compute_hardware_detection_diff(server, current_disks, current_ports, report.detected_inventory or {})
    return {
        "report_id": report.id,
        "status": report.status.value,
        "diff": diff,
    }


@router.post("/{server_id}/hardware-detection/reports/{report_id}/reject", response_model=HardwareDetectionReportResponse)
async def reject_hardware_detection_report(
    server_id: int,
    report_id: int,
    payload: HardwareDetectionApplyRequest,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    report = HardwareDetectionReportDAO.get_by_id(db, report_id)
    if not report or report.server_id != server_id:
        raise HTTPException(status_code=404, detail="Hardware detection report not found")
    HardwareDetectionReportDAO.mark_rejected(
        db,
        report=report,
        reviewed_by_user_id=auth.get("user_id"),
        notes=payload.notes,
    )
    return HardwareDetectionReportResponse.from_model(report)


@router.post("/{server_id}/hardware-detection/reports/{report_id}/apply", response_model=dict)
async def apply_hardware_detection_report(
    server_id: int,
    report_id: int,
    payload: HardwareDetectionApplyRequest,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    report = HardwareDetectionReportDAO.get_by_id(db, report_id)
    if not report or report.server_id != server_id:
        raise HTTPException(status_code=404, detail="Hardware detection report not found")
    if report.status != HardwareDetectionReportStatus.SUBMITTED:
        raise HTTPException(status_code=400, detail="Only submitted reports can be applied")
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    current_disks = DiskDAO.get_by_server(db, server_id)
    current_ports = NetworkPortDAO.get_by_server(db, server_id)
    diff = _compute_hardware_detection_diff(server, current_disks, current_ports, report.detected_inventory or {})
    apply_summary = _apply_hardware_detection_report(db, server, report, payload.nic_remap or {})
    HardwareDetectionReportDAO.mark_applied(
        db,
        report=report,
        reviewed_by_user_id=auth.get("user_id"),
        nic_remap=payload.nic_remap or {},
        diff_snapshot=diff,
        notes=payload.notes,
    )
    return {
        "status": "applied",
        "report_id": report.id,
        "summary": apply_summary,
    }


@router.delete("/{server_id}/hardware-detection/reports/{report_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_hardware_detection_report(
    server_id: int,
    report_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Delete a hardware detection report (admin only)."""
    report = HardwareDetectionReportDAO.get_by_id(db, report_id)
    if not report or report.server_id != server_id:
        raise HTTPException(status_code=404, detail="Hardware detection report not found")
    HardwareDetectionReportDAO.delete(db, report)


def _downsample_bandwidth_server(samples: list, resolution_minutes: int):
    """Bucket samples by time window; one row per window with rate over that interval."""
    if not samples or resolution_minutes < 2:
        return samples
    bucket_sec = resolution_minutes * 60
    by_bucket: dict[int, list] = {}
    for row in samples:
        try:
            ts_str = row.get("sampled_at")
            if not ts_str:
                continue
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            key = int(ts.timestamp() // bucket_sec) * bucket_sec
            if key not in by_bucket:
                by_bucket[key] = []
            by_bucket[key].append(row)
        except Exception:
            continue
    out = []
    for key in sorted(by_bucket.keys()):
        bucket = by_bucket[key]
        first, last = bucket[0], bucket[-1]
        try:
            ts_first = datetime.fromisoformat((first.get("sampled_at") or "").replace("Z", "+00:00"))
            ts_last = datetime.fromisoformat((last.get("sampled_at") or "").replace("Z", "+00:00"))
            duration_sec = max((ts_last - ts_first).total_seconds(), 1.0)
        except Exception:
            duration_sec = float(bucket_sec)
        delta_in = last.get("bytes_in", 0) - first.get("bytes_in", 0)
        delta_out = last.get("bytes_out", 0) - first.get("bytes_out", 0)
        if delta_in < 0:
            delta_in = last.get("bytes_in", 0)
        if delta_out < 0:
            delta_out = last.get("bytes_out", 0)
        rate_in = delta_in * 8 / 1_000_000 / duration_sec
        rate_out = delta_out * 8 / 1_000_000 / duration_sec
        out.append({
            "sampled_at": first.get("sampled_at"),
            "bytes_in": last.get("bytes_in", 0),
            "bytes_out": last.get("bytes_out", 0),
            "bytes_in_interval": delta_in,
            "bytes_out_interval": delta_out,
            "rate_in_mbps": round(rate_in, 4),
            "rate_out_mbps": round(rate_out, 4),
        })
    return out


@router.get("/{server_id}/bandwidth", response_model=dict)
async def get_server_bandwidth(
    server_id: int,
    hours: int = 24,
    resolution_minutes: int = 0,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Get stored bandwidth data for switch ports linked to this server. Bytes are cumulative; Rate is the difference over the chosen interval."""
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Server not found",
        )
    cable_runs = CableRunDAO.get_by_server(db, server_id)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    result = []
    for cr in cable_runs:
        server_port_id = cr.end_a_server_port_id or cr.end_b_server_port_id
        switch_port_id = cr.end_a_switch_port_id or cr.end_b_switch_port_id
        if not server_port_id or not switch_port_id:
            continue
        switch_port = SwitchPortDAO.get_by_id(db, switch_port_id)
        if not switch_port or not switch_port.switch_id:
            continue
        server_port = NetworkPortDAO.get_by_id(db, server_port_id)
        switch = NetworkSwitchDAO.get_by_id(db, switch_port.switch_id)
        raw = SwitchBandwidthSampleDAO.get_history(
            db,
            switch_id=switch_port.switch_id,
            port_identifier=switch_port.name,
            since=since,
            limit=2000,
        )
        prev = None
        samples = []
        for s in raw:
            rate_in = rate_out = None
            bytes_in_interval = bytes_out_interval = None
            if prev and s.sampled_at and prev.sampled_at:
                try:
                    dt = (s.sampled_at - prev.sampled_at).total_seconds()
                    if dt > 0:
                        delta_in = s.bytes_in - prev.bytes_in
                        delta_out = s.bytes_out - prev.bytes_out
                        if delta_in < 0:
                            delta_in = s.bytes_in
                        if delta_out < 0:
                            delta_out = s.bytes_out
                        bytes_in_interval = delta_in
                        bytes_out_interval = delta_out
                        rate_in = delta_in * 8 / 1_000_000 / dt
                        rate_out = delta_out * 8 / 1_000_000 / dt
                except Exception:
                    pass
            prev = s
            samples.append({
                "sampled_at": s.sampled_at.isoformat() if s.sampled_at else None,
                "bytes_in": s.bytes_in,
                "bytes_out": s.bytes_out,
                "bytes_in_interval": bytes_in_interval,
                "bytes_out_interval": bytes_out_interval,
                "rate_in_mbps": round(rate_in, 4) if rate_in is not None else None,
                "rate_out_mbps": round(rate_out, 4) if rate_out is not None else None,
            })
        if resolution_minutes >= 2:
            samples = _downsample_bandwidth_server(samples, resolution_minutes)
        result.append({
            "server_port_id": server_port_id,
            "server_port_name": server_port.name if server_port else None,
            "switch_id": switch_port.switch_id,
            "switch_name": switch.name if switch else None,
            "port_identifier": switch_port.name,
            "samples": samples,
        })
    return {"server_id": server_id, "hours": hours, "resolution_minutes": resolution_minutes or None, "ports": result}


@router.put("/{server_id}", response_model=ServerResponse)
async def update_server(
    server_id: int,
    server_data: ServerUpdate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Update a server"""
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Server not found"
        )
    
    # Update fields if provided
    if server_data.name is not None:
        # Check if name is being changed and if new name already exists
        if server_data.name != server.name:
            existing = ServerDAO.get_by_name(db, server_data.name)
            if existing:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Server with this name already exists"
                )
        server.name = server_data.name
    
    if server_data.server_ip is not None:
        server.server_ip = server_data.server_ip
    if server_data.description is not None:
        server.description = server_data.description
    if server_data.comments is not None:
        server.comments = server_data.comments
    if server_data.preview_asset_id is not None:
        server.preview_asset_id = server_data.preview_asset_id
    if server_data.cpu_count is not None:
        server.cpu_count = server_data.cpu_count
    if server_data.cpu_model is not None:
        server.cpu_model = server_data.cpu_model
    if server_data.ram_gb is not None:
        server.ram_gb = server_data.ram_gb
    if server_data.port_speed_mbps is not None:
        server.port_speed_mbps = server_data.port_speed_mbps
    if server_data.location_id is not None:
        location = LocationDAO.get_by_id(db, server_data.location_id)
        if not location:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Location not found"
            )
        server.location_id = server_data.location_id
    
    # Handle rack assignment
    if server_data.rack_id is not None or server_data.rack_unit is not None or server_data.rack_units is not None:
        # If rack_id is being cleared (set to None explicitly), clear both
        if server_data.rack_id is None:
            server.rack_id = None
            server.rack_unit = None
            server.rack_units = 1
        else:
            # Validate rack exists
            rack = RackDAO.get_by_id(db, server_data.rack_id)
            if not rack:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Rack not found"
                )
            # Ensure rack is in the same location as server
            if rack.location_id != server.location_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Rack must be in the same location as the server"
                )
            server.rack_id = server_data.rack_id
            rack_units_val = server_data.rack_units if server_data.rack_units is not None else (server.rack_units or 1)
            if rack_units_val < 1 or rack_units_val > min(rack.units, MAX_SERVER_RACK_UNITS):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"rack_units must be between 1 and {min(rack.units, MAX_SERVER_RACK_UNITS)}"
                )
            server.rack_units = rack_units_val
            # Effective position: from payload or keep existing (so changing only rack_units still validates overlap)
            effective_rack_unit = server_data.rack_unit if server_data.rack_unit is not None else server.rack_unit
            if effective_rack_unit is not None:
                if effective_rack_unit < 1 or effective_rack_unit > rack.units:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Rack unit must be between 1 and {rack.units}"
                    )
                if effective_rack_unit + rack_units_val - 1 > rack.units:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Server (U{effective_rack_unit}-{effective_rack_unit + rack_units_val - 1}) would extend past rack height ({rack.units}U)"
                    )
                if _rack_placement_overlaps(db, server_data.rack_id, effective_rack_unit, rack_units_val, exclude_server_id=server.id):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Rack position U{effective_rack_unit}-{effective_rack_unit + rack_units_val - 1} overlaps another server in this rack"
                    )
                server.rack_unit = effective_rack_unit
            elif server_data.rack_id is not None:
                # Only clear position when we're not just updating size (keep existing position if they omitted rack_unit)
                if server_data.rack_unit is None and server.rack_unit is not None and server_data.rack_units is not None:
                    # They sent rack_units but not rack_unit: keep current position to avoid disappearing from rack view
                    pass
                else:
                    server.rack_unit = None
    
    if server_data.plugin_name is not None:
        # Plugin validation already done above
        server.plugin_name = server_data.plugin_name
    if server_data.plugin_config is not None:
        server.plugin_config = server_data.plugin_config
    if server_data.enabled is not None:
        server.enabled = server_data.enabled
    if server_data.boot_mode is not None:
        try:
            from app.models.server import BootMode
            server.boot_mode = BootMode(server_data.boot_mode.lower())
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid boot_mode: {server_data.boot_mode}. Must be 'uefi' or 'bios'"
            )
    if server_data.pxe_boot_mode is not None:
        try:
            from app.models.server import BootMode
            server.pxe_boot_mode = BootMode(server_data.pxe_boot_mode.lower())
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid pxe_boot_mode: {server_data.pxe_boot_mode}. Must be 'uefi' or 'bios'"
            )
    if server_data.os_boot_mode is not None:
        try:
            from app.models.server import BootMode
            server.os_boot_mode = BootMode(server_data.os_boot_mode.lower())
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid os_boot_mode: {server_data.os_boot_mode}. Must be 'uefi' or 'bios'"
            )
    if "pxe_kernel_args_general" in server_data.model_fields_set:
        server.pxe_kernel_args_general = (
            server_data.pxe_kernel_args_general.strip()
            if server_data.pxe_kernel_args_general and server_data.pxe_kernel_args_general.strip()
            else None
        )
    if "pxe_kernel_args_network" in server_data.model_fields_set:
        server.pxe_kernel_args_network = (
            server_data.pxe_kernel_args_network.strip()
            if server_data.pxe_kernel_args_network and server_data.pxe_kernel_args_network.strip()
            else None
        )
    
    # Update IPMI proxy settings
    if server_data.ipmi_proxy_enabled is not None:
        server.ipmi_proxy_enabled = server_data.ipmi_proxy_enabled
    if server_data.ipmi_web_management_url is not None:
        server.ipmi_web_management_url = server_data.ipmi_web_management_url
    if server_data.ipmi_viewer_username is not None:
        server.ipmi_viewer_username = server_data.ipmi_viewer_username
    if server_data.ipmi_viewer_password is not None:
        server.ipmi_viewer_password = server_data.ipmi_viewer_password
    
    # Update disks if provided
    if server_data.disks is not None:
        # Delete existing disks
        DiskDAO.delete_by_server(db, server.id)
        # Create new disks
        for disk_data in server_data.disks:
            try:
                # Convert to uppercase to match enum values (SSD, HDD)
                disk_type = DiskType(disk_data.type.upper())
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid disk type: {disk_data.type}. Must be 'ssd' or 'hdd'"
                )
            
            DiskDAO.create(
                db,
                server_id=server.id,
                type=disk_type,
                capacity_gb=disk_data.capacity_gb,
                model=disk_data.model,
                description=disk_data.description,
                serial_number=disk_data.serial_number,
                is_os_disk=disk_data.is_os_disk
            )
    
    # Update network ports if provided
    if server_data.network_ports is not None:
        # Delete existing network ports
        NetworkPortDAO.delete_by_server(db, server.id)
        # Create new network ports
        for port_data in server_data.network_ports:
            NetworkPortDAO.create(
                db,
                server_id=server.id,
                name=port_data.name,
                mac_address=port_data.mac_address,
                speed_mbps=port_data.speed_mbps,
                model=port_data.model,
                pci_address=port_data.pci_address,
                is_physical=port_data.is_physical,
                lag_group=port_data.lag_group,
                monitor_bandwidth=port_data.monitor_bandwidth,
                pxe_boot=port_data.pxe_boot,
                pxe_ip=port_data.pxe_ip,
                description=port_data.description
            )
    
    # Update server groups if provided
    if server_data.server_group_ids is not None:
        # Clear existing groups and assign new ones
        server.server_groups.clear()
        for group_id in server_data.server_group_ids:
            group = ServerGroupDAO.get_by_id(db, group_id)
            if group:
                server.server_groups.append(group)
    
    ServerDAO.update(db, server)
    
    # Refresh to get disks and network ports (with cable_run info for "Connected To")
    disks = DiskDAO.get_by_server(db, server.id)
    network_ports = NetworkPortDAO.get_by_server(db, server.id)
    network_ports_with_cables = convert_network_ports_to_response(db, server.id, network_ports)

    # Regenerate DHCP configuration and reload service (if network ports or PXE boot mode changed)
    if (server_data.network_ports is not None or 
        server_data.boot_mode is not None or 
        server_data.pxe_boot_mode is not None or 
        server_data.server_ip is not None):
        if server.location_id:
            await _regenerate_location_dhcp_if_present(db, server.location_id)
    
    # Build response, excluding password
    server_dict = {k: v.value if hasattr(v, 'value') else v for k, v in server.__dict__.items()}
    server_dict["ipmi_viewer_password"] = None
    if server_dict.get("rack_units") is None:
        server_dict["rack_units"] = 1
    
    effective = _get_effective_capabilities_for_server(db, server)
    plugin_categories = [c["id"] for c in effective]

    # Resolve location and rack names for display
    location = LocationDAO.get_by_id(db, server.location_id)
    location_name = location.name if location else None
    rack_name = None
    if server.rack_id:
        rack = RackDAO.get_by_id(db, server.rack_id)
        rack_name = rack.name if rack else None
    elif server.rack_unit is not None and server.location_id:
        racks_in_location = RackDAO.get_by_location(db, server.location_id)
        if len(racks_in_location) == 1:
            only_rack = racks_in_location[0]
            rack_name = only_rack.name
            server.rack_id = only_rack.id
            ServerDAO.update(db, server)
            server_dict["rack_id"] = only_rack.id

    # Get server groups
    server_groups = []
    if server.server_groups:
        for group in server.server_groups:
            server_groups.append({
                "id": group.id,
                "name": group.name,
                "description": group.description
            })

    return {
        **server_dict,
        "location_name": location_name,
        "rack_name": rack_name,
        "disks": convert_disks_to_response(disks),
        "network_ports": network_ports_with_cables,
        "plugin_categories": plugin_categories,
        "effective_capabilities": effective,
        "server_groups": server_groups
    }


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_server(
    server_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Delete a server"""
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Server not found"
        )
    location_id = server.location_id
    success = ServerDAO.delete(db, server_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Server not found"
        )
    if location_id:
        await _regenerate_location_dhcp_if_present(db, location_id)
        # Don't fail the deletion if DHCP config regeneration fails


# ========== Power Control Endpoints ==========


@router.get("/{server_id}/activity", response_model=List[ServerActivityResponse])
async def get_server_activity(
    server_id: int,
    limit: int = 100,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Get unified server activity log entries for a server."""
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Server not found"
        )

    safe_limit = max(1, min(limit, 500))
    entries = ServerActivityDAO.get_by_server(db, server_id, limit=safe_limit)
    return [ServerActivityResponse.from_model(entry) for entry in entries]


@router.get("/{server_id}/power-state", response_model=dict)
async def get_server_power_state(
    server_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Get current power state of a server"""
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Server not found"
        )
    
    if not _server_has_capability(db, server, "power_control"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Plugin '{server.plugin_name}' does not support power control"
        )
    
    # Get plugin instance and call get_power_state
    registry = get_registry()
    try:
        plugin_instance = registry.get_plugin(server.plugin_name, server.plugin_config)
        power_state = await plugin_instance.get_power_state()
        return {
            "power_state": power_state.value,
            "server_id": server_id,
            "server_name": server.name
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get power state: {str(e)}"
        )


@router.post("/{server_id}/power-on", response_model=dict)
async def power_on_server(
    server_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Power on a server"""
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Server not found"
        )
    
    if not _server_has_capability(db, server, "power_control"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Server does not have power control capability enabled"
        )

    log_server_activity_attempt(
        db,
        server_id=server.id,
        event_type=ServerActivityEventType.POWER,
        action="on",
        source="admin_api",
        message="Power on requested",
        details={"requested_by_user_id": auth.get("user_id")},
    )

    registry = get_registry()
    try:
        plugin_instance = registry.get_plugin(server.plugin_name, server.plugin_config)
        success = await plugin_instance.power_on()
        if success:
            log_server_activity_success(
                db,
                server_id=server.id,
                event_type=ServerActivityEventType.POWER,
                action="on",
                source="admin_api",
                message="Power on command sent successfully",
                details={"requested_by_user_id": auth.get("user_id")},
            )
        else:
            log_server_activity_failure(
                db,
                server_id=server.id,
                event_type=ServerActivityEventType.POWER,
                action="on",
                source="admin_api",
                message="Power on command failed",
                details={"requested_by_user_id": auth.get("user_id")},
            )
        return {
            "success": success,
            "message": "Power on command sent successfully" if success else "Power on command failed",
            "server_id": server_id,
            "server_name": server.name
        }
    except Exception as e:
        log_server_activity_failure(
            db,
            server_id=server.id,
            event_type=ServerActivityEventType.POWER,
            action="on",
            source="admin_api",
            message="Power on request failed",
            details={"requested_by_user_id": auth.get("user_id")},
            error=e,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to power on server: {str(e)}"
        )


@router.post("/{server_id}/power-off", response_model=dict)
async def power_off_server(
    server_id: int,
    force: bool = False,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Power off a server"""
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Server not found"
        )
    
    if not _server_has_capability(db, server, "power_control"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Server does not have power control capability enabled"
        )

    log_server_activity_attempt(
        db,
        server_id=server.id,
        event_type=ServerActivityEventType.POWER,
        action="off",
        source="admin_api",
        message="Power off requested",
        details={"requested_by_user_id": auth.get("user_id"), "force": force},
    )

    registry = get_registry()
    try:
        plugin_instance = registry.get_plugin(server.plugin_name, server.plugin_config)
        success = await plugin_instance.power_off(force=force)
        if success:
            log_server_activity_success(
                db,
                server_id=server.id,
                event_type=ServerActivityEventType.POWER,
                action="off",
                source="admin_api",
                message="Power off command sent successfully",
                details={"requested_by_user_id": auth.get("user_id"), "force": force},
            )
        else:
            log_server_activity_failure(
                db,
                server_id=server.id,
                event_type=ServerActivityEventType.POWER,
                action="off",
                source="admin_api",
                message="Power off command failed",
                details={"requested_by_user_id": auth.get("user_id"), "force": force},
            )
        return {
            "success": success,
            "message": "Power off command sent successfully" if success else "Power off command failed",
            "server_id": server_id,
            "server_name": server.name,
            "force": force
        }
    except Exception as e:
        log_server_activity_failure(
            db,
            server_id=server.id,
            event_type=ServerActivityEventType.POWER,
            action="off",
            source="admin_api",
            message="Power off request failed",
            details={"requested_by_user_id": auth.get("user_id"), "force": force},
            error=e,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to power off server: {str(e)}"
        )


@router.post("/{server_id}/power-reset", response_model=dict)
async def power_reset_server(
    server_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Reset/reboot a server"""
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Server not found"
        )
    
    if not _server_has_capability(db, server, "power_control"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Server does not have power control capability enabled"
        )

    log_server_activity_attempt(
        db,
        server_id=server.id,
        event_type=ServerActivityEventType.POWER,
        action="reset",
        source="admin_api",
        message="Power reset requested",
        details={"requested_by_user_id": auth.get("user_id")},
    )

    registry = get_registry()
    try:
        plugin_instance = registry.get_plugin(server.plugin_name, server.plugin_config)
        success = await plugin_instance.power_reset()
        if success:
            log_server_activity_success(
                db,
                server_id=server.id,
                event_type=ServerActivityEventType.POWER,
                action="reset",
                source="admin_api",
                message="Power reset command sent successfully",
                details={"requested_by_user_id": auth.get("user_id")},
            )
        else:
            log_server_activity_failure(
                db,
                server_id=server.id,
                event_type=ServerActivityEventType.POWER,
                action="reset",
                source="admin_api",
                message="Power reset command failed",
                details={"requested_by_user_id": auth.get("user_id")},
            )
        return {
            "success": success,
            "message": "Reset command sent successfully" if success else "Reset command failed",
            "server_id": server_id,
            "server_name": server.name
        }
    except Exception as e:
        log_server_activity_failure(
            db,
            server_id=server.id,
            event_type=ServerActivityEventType.POWER,
            action="reset",
            source="admin_api",
            message="Power reset request failed",
            details={"requested_by_user_id": auth.get("user_id")},
            error=e,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to reset server: {str(e)}"
        )


@router.get("/{server_id}/boot/options", response_model=dict)
async def get_server_boot_options(
    server_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Server not found")
    if not _server_has_capability(db, server, "boot_order"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Boot order capability is disabled for this server",
        )

    registry = get_registry()
    try:
        plugin_instance = registry.get_plugin(server.plugin_name, server.plugin_config)
        options = await plugin_instance.get_boot_options()
        try:
            current = await plugin_instance.get_boot_order()
        except NotImplementedError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
        except Exception as exc:
            logger.warning(
                "Failed to read current boot order for server %s: %s",
                server.id,
                exc,
            )
            current = {
                "current_device": None,
                "persistent": None,
                "uefi": None,
                "error": str(exc),
            }
        return {
            "server_id": server.id,
            "server_name": server.name,
            "options": options,
            "current": current,
        }
    except NotImplementedError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to load boot options: {str(exc)}",
        )


@router.post("/{server_id}/boot/set", response_model=dict)
async def set_server_boot_option(
    server_id: int,
    payload: ServerBootSetRequest,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Server not found")
    if not _server_has_capability(db, server, "boot_order"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Boot order capability is disabled for this server",
        )

    log_server_activity_attempt(
        db,
        server_id=server.id,
        event_type=ServerActivityEventType.POWER,
        action="set_boot_device",
        source="admin_api",
        message="Boot device change requested",
        details={
            "requested_by_user_id": auth.get("user_id"),
            "device": payload.device,
            "persistent": payload.persistent,
            "uefi": payload.uefi,
        },
    )

    registry = get_registry()
    try:
        plugin_instance = registry.get_plugin(server.plugin_name, server.plugin_config)
        success = await plugin_instance.set_next_boot_device(
            device=payload.device,
            persistent=payload.persistent,
            uefi=payload.uefi,
        )
        if not success:
            log_server_activity_failure(
                db,
                server_id=server.id,
                event_type=ServerActivityEventType.POWER,
                action="set_boot_device",
                source="admin_api",
                message="Boot device change failed",
                details={"device": payload.device, "persistent": payload.persistent, "uefi": payload.uefi},
            )
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Failed to set boot device")

        log_server_activity_success(
            db,
            server_id=server.id,
            event_type=ServerActivityEventType.POWER,
            action="set_boot_device",
            source="admin_api",
            message="Boot device updated",
            details={"device": payload.device, "persistent": payload.persistent, "uefi": payload.uefi},
        )

        current = await plugin_instance.get_boot_order()
        return {
            "success": True,
            "server_id": server.id,
            "server_name": server.name,
            "current": current,
        }
    except HTTPException:
        raise
    except ValueError as exc:
        log_server_activity_failure(
            db,
            server_id=server.id,
            event_type=ServerActivityEventType.POWER,
            action="set_boot_device",
            source="admin_api",
            message="Boot device validation failed",
            details={"device": payload.device},
            error=exc,
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except NotImplementedError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        log_server_activity_failure(
            db,
            server_id=server.id,
            event_type=ServerActivityEventType.POWER,
            action="set_boot_device",
            source="admin_api",
            message="Boot device request failed",
            details={"device": payload.device, "persistent": payload.persistent, "uefi": payload.uefi},
            error=exc,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to set boot device: {str(exc)}",
        )


@router.post("/{server_id}/boot/kernel-args-preview", response_model=dict)
async def preview_server_kernel_args(
    server_id: int,
    payload: ServerKernelArgsPreviewRequest,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    server = ServerDAO.get_by_id(db, server_id)
    if not server:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Server not found")

    from app.api.server_interaction import build_temp_os_kernel_args_preview

    return build_temp_os_kernel_args_preview(
        db=db,
        server=server,
        temp_os_id=(payload.temp_os_id or "debian-live").strip() or "debian-live",
        general_args_override=payload.pxe_kernel_args_general,
        network_args_override=payload.pxe_kernel_args_network,
    )

