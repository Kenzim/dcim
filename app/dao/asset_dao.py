from sqlalchemy.orm import Session
from typing import Optional, List
from app.models.asset import Asset, AssetLabel


class AssetDAO:
    """Data Access Object for Asset model."""

    @staticmethod
    def create(
        db: Session,
        filename: str,
        label: AssetLabel = AssetLabel.GENERIC,
        description: Optional[str] = None,
        content: Optional[bytes] = None,
        content_type: Optional[str] = None,
    ) -> Asset:
        """Create a new asset record. Image bytes stored in DB (content/content_type)."""
        asset = Asset(
            filename=filename,
            content=content,
            content_type=content_type,
            label=label,
            description=description,
        )
        db.add(asset)
        db.commit()
        db.refresh(asset)
        return asset

    @staticmethod
    def get_by_id(db: Session, asset_id: int) -> Optional[Asset]:
        """Get asset by ID."""
        return db.query(Asset).filter(Asset.id == asset_id).first()

    @staticmethod
    def get_all(
        db: Session,
        label: Optional[AssetLabel] = None,
        limit: Optional[int] = None,
    ) -> List[Asset]:
        """Get all assets, optionally filtered by label, newest first."""
        query = db.query(Asset)
        if label is not None:
            query = query.filter(Asset.label == label)
        query = query.order_by(Asset.created_at.desc())
        if limit is not None:
            query = query.limit(limit)
        return query.all()

    @staticmethod
    def delete(db: Session, asset_id: int) -> bool:
        """Delete an asset by ID. Caller must clear FK references (e.g. servers.preview_asset_id) first."""
        asset = db.query(Asset).filter(Asset.id == asset_id).first()
        if asset:
            db.delete(asset)
            db.commit()
            return True
        return False
