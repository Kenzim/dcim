"""
DHCP Configuration Service

Manages DHCP server configuration stored in the database.
Defaults for file paths come from env: DHCP_CONFIG_FILE_PATH, DHCP_LEASE_FILE_PATH.
"""
import logging
import os
from typing import List, Optional  # noqa: F401 - Optional used in return type
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

from app.dao.dhcp_config_dao import DHCPConfigDAO
from app.models.dhcp_config import DHCPConfigModel

logger = logging.getLogger(__name__)


def _default_config_path() -> str:
    return os.environ.get("DHCP_CONFIG_FILE_PATH", "/shared/dhcp/dhcpd.conf")


def _default_lease_path() -> str:
    return os.environ.get("DHCP_LEASE_FILE_PATH", "/shared/dhcp/dhcpd.leases")


class DHCPInterfaceConfig(BaseModel):
    """DHCP interface / subnet configuration"""
    interface: str = Field(..., description="Network interface name (e.g., eth1)")
    ip: str = Field(..., description="IP address to bind to (e.g., 192.168.12.74)")
    cidr: Optional[int] = Field(
        None,
        description="CIDR notation (e.g., 24 for /24). If not provided, defaults to /24",
    )
    netmask: Optional[str] = Field(
        None,
        description="Netmask (e.g., 255.255.255.0). Alternative to CIDR. If both provided, CIDR takes precedence",
    )
    gateway: Optional[str] = Field(
        None,
        description="Default gateway (routers) for this subnet (e.g., 192.168.1.1). "
        "Servers on this subnet will receive this via DHCP option routers.",
    )
    range_start: Optional[str] = Field(
        None,
        description="Optional start of dynamic lease range (e.g. 192.168.1.100). If not set, a sensible default is generated.",
    )
    range_end: Optional[str] = Field(
        None,
        description="Optional end of dynamic lease range (e.g. 192.168.1.200). If not set, a sensible default is generated.",
    )


class DHCPConfig(BaseModel):
    """DHCP server configuration"""
    enabled: bool = Field(default=True, description="Whether DHCP server is enabled")
    interfaces: List[DHCPInterfaceConfig] = Field(
        default_factory=lambda: [DHCPInterfaceConfig(interface="eth1", ip="192.168.12.74")],
        description="List of interfaces/IPs to bind to"
    )
    dns_servers: Optional[List[str]] = Field(
        default_factory=lambda: ["1.1.1.1", "1.0.0.1"],
        description="DNS servers to send via DHCP option domain-name-servers (default: Cloudflare 1.1.1.1, 1.0.0.1)"
    )
    hand_out_leases: bool = Field(default=True, description="Whether to hand out normal DHCP leases")
    default_lease_time: int = Field(default=3600, description="Default lease time in seconds")
    max_lease_time: int = Field(default=7200, description="Maximum lease time in seconds")
    config_file_path: str = Field(default="/root/dcim/dhcpd.conf", description="Path to dhcpd.conf")
    lease_file_path: str = Field(default="/root/dcim/dhcpd.leases", description="Path to dhcpd.leases")


def _row_to_config(row: DHCPConfigModel) -> DHCPConfig:
    """Convert DB row to Pydantic config."""
    interfaces = []
    if row.interfaces:
        for iface in row.interfaces:
            if isinstance(iface, dict):
                interfaces.append(DHCPInterfaceConfig(**iface))
            else:
                interfaces.append(iface)
    if not interfaces:
        interfaces = [DHCPInterfaceConfig(interface="eth1", ip="192.168.12.74")]
    # Default DNS servers to Cloudflare when not set in DB
    dns = row.dns_servers or ["1.1.1.1", "1.0.0.1"]
    return DHCPConfig(
        enabled=row.enabled,
        interfaces=interfaces,
        dns_servers=dns,
        hand_out_leases=row.hand_out_leases,
        default_lease_time=row.default_lease_time,
        max_lease_time=row.max_lease_time,
        config_file_path=row.config_file_path,
        lease_file_path=row.lease_file_path,
    )


class DHCPConfigService:
    """Service for managing DHCP configuration from the database."""

    def get_config(self, db: Session) -> DHCPConfig:
        """Get current DHCP configuration from database (legacy global config)."""
        row = DHCPConfigDAO.get_config(db)
        if row is not None:
            return _row_to_config(row)
        row = DHCPConfigDAO.get_or_create(db, _default_config_path(), _default_lease_path())
        return _row_to_config(row)

    def get_config_by_service_instance(
        self, db: Session, service_instance_id: int
    ) -> Optional[DHCPConfig]:
        """Get DHCP config for a service instance, or None if not configured."""
        row = DHCPConfigDAO.get_by_service_instance_id(db, service_instance_id)
        if row is None:
            return None
        return _row_to_config(row)

    def get_or_create_config_for_service_instance(
        self, db: Session, service_instance_id: int, config_path: str, lease_path: str
    ) -> DHCPConfig:
        row = DHCPConfigDAO.get_or_create_for_service_instance(
            db, service_instance_id, config_path, lease_path
        )
        return _row_to_config(row)

    def _update_row(self, db: Session, row: DHCPConfigModel, **kwargs) -> DHCPConfig:
        """
        Update DHCP configuration in the database.

        Args:
            db: Database session
            **kwargs: Configuration fields to update

        Returns:
            Updated configuration
        """
        current = _row_to_config(row)
        update_data = current.model_dump()
        update_data.update({k: v for k, v in kwargs.items() if v is not None})
        if "interfaces" in kwargs:
            if isinstance(kwargs["interfaces"], list):
                interfaces = []
                for iface in kwargs["interfaces"]:
                    if isinstance(iface, dict):
                        interfaces.append(iface)
                    else:
                        interfaces.append(iface.model_dump())
                update_data["interfaces"] = interfaces
        row.enabled = update_data["enabled"]
        row.interfaces = update_data["interfaces"]
        row.dns_servers = update_data.get("dns_servers")
        row.hand_out_leases = update_data["hand_out_leases"]
        row.default_lease_time = update_data["default_lease_time"]
        row.max_lease_time = update_data["max_lease_time"]
        row.config_file_path = update_data["config_file_path"]
        row.lease_file_path = update_data["lease_file_path"]
        DHCPConfigDAO.update(db, row)
        return _row_to_config(row)

    def update_config(self, db: Session, **kwargs) -> DHCPConfig:
        """Update the global DHCP configuration row."""
        row = DHCPConfigDAO.get_config(db)
        if row is None:
            row = DHCPConfigDAO.get_or_create(
                db, _default_config_path(), _default_lease_path()
            )
        return self._update_row(db, row, **kwargs)

    def update_config_for_service_instance(
        self, db: Session, service_instance_id: int, **kwargs
    ) -> DHCPConfig:
        """Update DHCP configuration for a specific service instance."""
        row = DHCPConfigDAO.get_or_create_for_service_instance(
            db,
            service_instance_id,
            _default_config_path(),
            _default_lease_path(),
        )
        return self._update_row(db, row, **kwargs)

    def reload(self, db: Session) -> DHCPConfig:
        """Reload configuration from database (no cache)."""
        return self.get_config(db)


_dhcp_config_service: Optional[DHCPConfigService] = None


def get_dhcp_config_service() -> DHCPConfigService:
    """Get the global DHCP config service instance."""
    global _dhcp_config_service
    if _dhcp_config_service is None:
        _dhcp_config_service = DHCPConfigService()
    return _dhcp_config_service
