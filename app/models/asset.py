import enum
from sqlalchemy import Column, Integer, String, Text, DateTime, Enum as SQLEnum, LargeBinary
from sqlalchemy.sql import func
from app.core.database import Base


class AssetLabel(str, enum.Enum):
    """Intended use / category for an asset. Any image can be used anywhere; this is for browsing and filtering."""
    SERVER_PREVIEW = "server_preview"
    LOCATION_ICON = "location_icon"
    RACK_IMAGE = "rack_image"
    GENERIC = "generic"


class Asset(Base):
    """
    Asset model - stores metadata and optionally the image bytes for uploaded images used across the DCIM.

    When content is set, the image is stored in the DB; otherwise storage_path may point to a file on disk (legacy).
    Labels indicate intended use (e.g. server preview) to make browsing easier; any asset can be used for any purpose.
    """
    __tablename__ = "assets"

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String(255), nullable=False)  # Original filename
    storage_path = Column(String(512), nullable=True, unique=True, index=True)  # Legacy: path on disk when content is null
    content = Column(LargeBinary, nullable=True)  # Image bytes when stored in DB
    content_type = Column(String(128), nullable=True)  # MIME type e.g. image/png
    label = Column(
        SQLEnum(AssetLabel, native_enum=False, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        index=True,
        default=AssetLabel.GENERIC,
    )
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self):
        return f"<Asset(id={self.id}, filename='{self.filename}', label={self.label})>"
