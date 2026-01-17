"""
DHCP Configuration Service

Manages DHCP server configuration stored as JSON on disk.
"""
import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class DHCPInterfaceConfig(BaseModel):
    """DHCP interface configuration"""
    interface: str = Field(..., description="Network interface name (e.g., eth1)")
    ip: str = Field(..., description="IP address to bind to (e.g., 192.168.12.74)")


class DHCPConfig(BaseModel):
    """DHCP server configuration"""
    enabled: bool = Field(default=True, description="Whether DHCP server is enabled")
    interfaces: List[DHCPInterfaceConfig] = Field(
        default_factory=lambda: [DHCPInterfaceConfig(interface="eth1", ip="192.168.12.74")],
        description="List of interfaces/IPs to bind to"
    )
    hand_out_leases: bool = Field(default=True, description="Whether to hand out normal DHCP leases")
    default_lease_time: int = Field(default=3600, description="Default lease time in seconds")
    max_lease_time: int = Field(default=7200, description="Maximum lease time in seconds")
    config_file_path: str = Field(default="/root/dcim/dhcpd.conf", description="Path to dhcpd.conf")
    lease_file_path: str = Field(default="/root/dcim/dhcpd.leases", description="Path to dhcpd.leases")


class DHCPConfigService:
    """Service for managing DHCP configuration stored as JSON"""
    
    def __init__(self, config_file: str = "/root/dcim/dhcp_config.json"):
        """
        Initialize DHCP config service.
        
        Args:
            config_file: Path to JSON configuration file
        """
        self.config_file = Path(config_file)
        self._config: Optional[DHCPConfig] = None
    
    def _ensure_config_file(self) -> None:
        """Ensure config file exists with default values if it doesn't."""
        if not self.config_file.exists():
            logger.info(f"Creating default DHCP config file at {self.config_file}")
            default_config = DHCPConfig()
            self._save_config(default_config)
    
    def _load_config(self) -> DHCPConfig:
        """Load configuration from JSON file."""
        self._ensure_config_file()
        
        try:
            with open(self.config_file, 'r') as f:
                data = json.load(f)
            
            # Convert interfaces list to DHCPInterfaceConfig objects
            if "interfaces" in data:
                interfaces = [DHCPInterfaceConfig(**iface) for iface in data["interfaces"]]
                data["interfaces"] = interfaces
            
            return DHCPConfig(**data)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse DHCP config JSON: {e}")
            # Return default config if JSON is invalid
            return DHCPConfig()
        except Exception as e:
            logger.error(f"Failed to load DHCP config: {e}")
            return DHCPConfig()
    
    def _save_config(self, config: DHCPConfig) -> None:
        """Save configuration to JSON file."""
        try:
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            
            # Convert to dict for JSON serialization
            config_dict = config.model_dump()
            
            with open(self.config_file, 'w') as f:
                json.dump(config_dict, f, indent=2)
            
            logger.info(f"Saved DHCP config to {self.config_file}")
        except Exception as e:
            logger.error(f"Failed to save DHCP config: {e}", exc_info=True)
            raise
    
    def get_config(self) -> DHCPConfig:
        """Get current DHCP configuration."""
        if self._config is None:
            self._config = self._load_config()
        return self._config
    
    def update_config(self, **kwargs) -> DHCPConfig:
        """
        Update DHCP configuration.
        
        Args:
            **kwargs: Configuration fields to update
        
        Returns:
            Updated configuration
        """
        current = self.get_config()
        
        # Update fields
        update_data = current.model_dump()
        update_data.update({k: v for k, v in kwargs.items() if v is not None})
        
        # Handle interfaces specially
        if "interfaces" in kwargs:
            if isinstance(kwargs["interfaces"], list):
                # Convert dicts to DHCPInterfaceConfig objects
                interfaces = []
                for iface in kwargs["interfaces"]:
                    if isinstance(iface, dict):
                        interfaces.append(DHCPInterfaceConfig(**iface))
                    elif isinstance(iface, DHCPInterfaceConfig):
                        interfaces.append(iface)
                update_data["interfaces"] = interfaces
        
        updated_config = DHCPConfig(**update_data)
        self._save_config(updated_config)
        self._config = updated_config
        
        return updated_config
    
    def reload(self) -> DHCPConfig:
        """Reload configuration from disk (discard cached version)."""
        self._config = None
        return self.get_config()


# Global instance
_dhcp_config_service: Optional[DHCPConfigService] = None


def get_dhcp_config_service() -> DHCPConfigService:
    """Get the global DHCP config service instance."""
    global _dhcp_config_service
    if _dhcp_config_service is None:
        _dhcp_config_service = DHCPConfigService()
    return _dhcp_config_service
