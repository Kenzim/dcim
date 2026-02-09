"""
TFTP Configuration Service

Manages TFTP server configuration stored in the database.
Default root_directory comes from env TFTP_ROOT_DIRECTORY.
"""
import logging
import os
from typing import Optional
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

from app.dao.tftp_config_dao import TFTPConfigDAO
from app.models.tftp_config import TFTPConfigModel

logger = logging.getLogger(__name__)


def _default_root_directory() -> str:
    return os.environ.get("TFTP_ROOT_DIRECTORY", "/shared/tftp")


class TFTPConfig(BaseModel):
    """TFTP server configuration"""
    enabled: bool = Field(default=True, description="Whether TFTP server is enabled")
    root_directory: str = Field(default="/shared/tftp", description="TFTP root directory (chroot)")
    bind_address: str = Field(default="0.0.0.0", description="IP address to bind to")
    bind_port: int = Field(default=69, description="Port to bind to")
    allow_create: bool = Field(default=True, description="Allow file creation (write access)")
    verbose: bool = Field(default=True, description="Verbose logging")
    ipv4_only: bool = Field(default=True, description="IPv4 only (disable IPv6)")


def _row_to_config(row: TFTPConfigModel) -> TFTPConfig:
    """Convert DB row to Pydantic config."""
    return TFTPConfig(
        enabled=row.enabled,
        root_directory=row.root_directory,
        bind_address=row.bind_address,
        bind_port=row.bind_port,
        allow_create=row.allow_create,
        verbose=row.verbose,
        ipv4_only=row.ipv4_only,
    )


class TFTPConfigService:
    """Service for managing TFTP configuration from the database."""

    def get_config(self, db: Session) -> TFTPConfig:
        """Get current TFTP configuration from database."""
        row = TFTPConfigDAO.get_config(db)
        if row is not None:
            return _row_to_config(row)
        row = TFTPConfigDAO.get_or_create(db, _default_root_directory())
        return _row_to_config(row)

    def update_config(self, db: Session, **kwargs) -> TFTPConfig:
        """
        Update TFTP configuration in the database.

        Args:
            db: Database session
            **kwargs: Configuration fields to update

        Returns:
            Updated configuration
        """
        row = TFTPConfigDAO.get_config(db)
        if row is None:
            row = TFTPConfigDAO.get_or_create(db, _default_root_directory())
        current = _row_to_config(row)
        update_data = current.model_dump()
        update_data.update({k: v for k, v in kwargs.items() if v is not None})
        row.enabled = update_data["enabled"]
        row.root_directory = update_data["root_directory"]
        row.bind_address = update_data["bind_address"]
        row.bind_port = update_data["bind_port"]
        row.allow_create = update_data["allow_create"]
        row.verbose = update_data["verbose"]
        row.ipv4_only = update_data["ipv4_only"]
        TFTPConfigDAO.update(db, row)
        return _row_to_config(row)

    def reload(self, db: Session) -> TFTPConfig:
        """Reload configuration from database."""
        return self.get_config(db)


_tftp_config_service: Optional[TFTPConfigService] = None


def get_tftp_config_service() -> TFTPConfigService:
    """Get the global TFTP config service instance."""
    global _tftp_config_service
    if _tftp_config_service is None:
        _tftp_config_service = TFTPConfigService()
    return _tftp_config_service
