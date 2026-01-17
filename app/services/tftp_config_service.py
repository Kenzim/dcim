"""
TFTP Configuration Service

Manages TFTP server configuration stored as JSON on disk.
"""
import json
import logging
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class TFTPConfig(BaseModel):
    """TFTP server configuration"""
    enabled: bool = Field(default=True, description="Whether TFTP server is enabled")
    root_directory: str = Field(default="/root/dcim/tftp", description="TFTP root directory (chroot)")
    bind_address: str = Field(default="192.168.12.74", description="IP address to bind to")
    bind_port: int = Field(default=69, description="Port to bind to")
    allow_create: bool = Field(default=True, description="Allow file creation (write access)")
    verbose: bool = Field(default=True, description="Verbose logging")
    ipv4_only: bool = Field(default=True, description="IPv4 only (disable IPv6)")


class TFTPConfigService:
    """Service for managing TFTP configuration stored as JSON"""
    
    def __init__(self, config_file: str = "/root/dcim/tftp_config.json"):
        """
        Initialize TFTP config service.
        
        Args:
            config_file: Path to JSON configuration file
        """
        self.config_file = Path(config_file)
        self._config: Optional[TFTPConfig] = None
    
    def _ensure_config_file(self) -> None:
        """Ensure config file exists with default values if it doesn't."""
        if not self.config_file.exists():
            logger.info(f"Creating default TFTP config file at {self.config_file}")
            default_config = TFTPConfig()
            self._save_config(default_config)
    
    def _load_config(self) -> TFTPConfig:
        """Load configuration from JSON file."""
        self._ensure_config_file()
        
        try:
            with open(self.config_file, 'r') as f:
                data = json.load(f)
            
            return TFTPConfig(**data)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse TFTP config JSON: {e}")
            # Return default config if JSON is invalid
            return TFTPConfig()
        except Exception as e:
            logger.error(f"Failed to load TFTP config: {e}")
            return TFTPConfig()
    
    def _save_config(self, config: TFTPConfig) -> None:
        """Save configuration to JSON file."""
        try:
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            
            # Convert to dict for JSON serialization
            config_dict = config.model_dump()
            
            with open(self.config_file, 'w') as f:
                json.dump(config_dict, f, indent=2)
            
            logger.info(f"Saved TFTP config to {self.config_file}")
        except Exception as e:
            logger.error(f"Failed to save TFTP config: {e}", exc_info=True)
            raise
    
    def get_config(self) -> TFTPConfig:
        """Get current TFTP configuration."""
        if self._config is None:
            self._config = self._load_config()
        return self._config
    
    def update_config(self, **kwargs) -> TFTPConfig:
        """
        Update TFTP configuration.
        
        Args:
            **kwargs: Configuration fields to update
        
        Returns:
            Updated configuration
        """
        current = self.get_config()
        
        # Update fields
        update_data = current.model_dump()
        update_data.update({k: v for k, v in kwargs.items() if v is not None})
        
        updated_config = TFTPConfig(**update_data)
        self._save_config(updated_config)
        self._config = updated_config
        
        return updated_config
    
    def reload(self) -> TFTPConfig:
        """Reload configuration from disk (discard cached version)."""
        self._config = None
        return self.get_config()


# Global instance
_tftp_config_service: Optional[TFTPConfigService] = None


def get_tftp_config_service() -> TFTPConfigService:
    """Get the global TFTP config service instance."""
    global _tftp_config_service
    if _tftp_config_service is None:
        _tftp_config_service = TFTPConfigService()
    return _tftp_config_service
