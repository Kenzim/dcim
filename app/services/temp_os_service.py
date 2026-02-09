"""
Service for managing temporary OS configurations.

Temporary OSes are stored in tftp/pxe/temp_os/{os_id}/ with:
- config.json: Configuration file

For debian-live, boot files (kernel, initrd, squashfs) are served from /root/dcim/tftp/
via the API endpoint /api/servers/interaction/tftp/
"""
import json
import logging
from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

# Base directory for temporary OSes
BASE_DIR = Path(__file__).parent.parent.parent / "tftp" / "pxe" / "temp_os"


class TempOSConfig(BaseModel):
    """Configuration for a temporary OS"""
    id: str = Field(..., description="Unique identifier (e.g., 'debian-live')")
    name: str = Field(..., description="Display name")
    description: str = Field(default="", description="Description of the OS")
    kernel_file: str = Field(..., description="Kernel filename")
    initrd_file: str = Field(..., description="Initrd filename")
    squashfs_file: Optional[str] = Field(default=None, description="Squashfs filename (for live OSes)")
    modloop_file: Optional[str] = Field(default=None, description="Modloop filename (e.g. for Alpine)")
    kernel_params: str = Field(default="", description="Default kernel parameters")
    version: Optional[str] = Field(default=None, description="OS version")
    flavor: Optional[str] = Field(default=None, description="OS flavor/variant")
    requires_modloop: bool = Field(default=False, description="Whether this OS requires a modloop file")
    
    @field_validator('id')
    @classmethod
    def validate_id(cls, v: str) -> str:
        """Validate ID is alphanumeric with dashes/underscores"""
        if not v.replace('-', '').replace('_', '').isalnum():
            raise ValueError("ID must be alphanumeric with dashes or underscores only")
        return v.lower()


class TempOSService:
    """Service for managing temporary OS configurations"""
    
    def __init__(self, base_dir: Path = BASE_DIR):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
    
    def scan_os_configs(self) -> List[TempOSConfig]:
        """Scan the temp_os directory and load all configurations"""
        configs = []
        
        if not self.base_dir.exists():
            logger.warning(f"Temporary OS directory does not exist: {self.base_dir}")
            return configs
        
        for os_dir in self.base_dir.iterdir():
            if not os_dir.is_dir():
                continue
            
            config_file = os_dir / "config.json"
            if not config_file.exists():
                logger.warning(f"No config.json found in {os_dir}")
                continue
            
            try:
                with open(config_file, 'r') as f:
                    config_data = json.load(f)
                
                # Validate required fields
                os_id = config_data.get('id', os_dir.name)
                kernel_file = config_data.get('kernel_file')
                initrd_file = config_data.get('initrd_file')
                
                if not kernel_file or not initrd_file:
                    logger.warning(f"Missing kernel_file or initrd_file in {config_file}")
                    continue
                
                # For debian-live, files are served from /root/dcim/tftp/ via API
                # No need to check for local kernel/initrd files in temp_os directory
                
                config = TempOSConfig(**config_data)
                configs.append(config)
                logger.debug(f"Loaded temporary OS config: {config.id}")
                
            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON in {config_file}: {e}")
                continue
            except Exception as e:
                logger.error(f"Error loading config from {config_file}: {e}")
                continue
        
        return configs
    
    def get_os_config(self, os_id: str) -> Optional[TempOSConfig]:
        """Get configuration for a specific temporary OS"""
        configs = self.scan_os_configs()
        for config in configs:
            if config.id == os_id:
                return config
        return None
    
    def get_kernel_url(self, os_id: str, base_url: str = "http://192.168.12.74:8000") -> Optional[str]:
        """Get the kernel URL for a temporary OS"""
        config = self.get_os_config(os_id)
        if not config:
            return None
        # All temp OS files are served from their respective directories
        return f"{base_url}/api/servers/interaction/temp-os/{os_id}/files/{config.kernel_file}"
    
    def get_initrd_url(self, os_id: str, base_url: str = "http://192.168.12.74:8000") -> Optional[str]:
        """Get the initrd URL for a temporary OS"""
        config = self.get_os_config(os_id)
        if not config:
            return None
        # All temp OS files are served from their respective directories
        return f"{base_url}/api/servers/interaction/temp-os/{os_id}/files/{config.initrd_file}"
    
    def get_squashfs_url(self, os_id: str, base_url: str = "http://192.168.12.74:8000") -> Optional[str]:
        """Get the squashfs URL for a temporary OS (if available)"""
        config = self.get_os_config(os_id)
        if not config or not config.squashfs_file:
            return None
        return f"{base_url}/api/servers/interaction/temp-os/{os_id}/files/{config.squashfs_file}"
    
    def get_kernel_params(self, os_id: str, additional_params: Optional[str] = None) -> str:
        """Get kernel parameters for a temporary OS"""
        config = self.get_os_config(os_id)
        if not config:
            return additional_params or ""
        
        params = config.kernel_params
        
        # Add additional parameters if provided
        if additional_params:
            params = f"{params} {additional_params}".strip()
        
        return params
    
    def get_os_dir(self, os_id: str) -> Optional[Path]:
        """Get the directory path for a temporary OS"""
        os_dir = self.base_dir / os_id
        if os_dir.exists() and os_dir.is_dir():
            return os_dir
        return None


# Singleton instance
_temp_os_service: Optional[TempOSService] = None


def get_temp_os_service() -> TempOSService:
    """Get the singleton TempOSService instance"""
    global _temp_os_service
    if _temp_os_service is None:
        _temp_os_service = TempOSService()
    return _temp_os_service
