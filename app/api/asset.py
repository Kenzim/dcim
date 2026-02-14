"""
API endpoints for the asset manager: upload, list, get, delete, and serve image files.
All images are stored in the DB (content column).
"""
import logging
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
from fastapi.responses import Response
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.core.database import get_db
from app.core.auth import require_admin
from app.dao.asset_dao import AssetDAO
from app.models.asset import AssetLabel
from app.models.server import Server

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/assets", tags=["assets"])

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"}
EXTENSION_TO_MEDIA_TYPE = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB


class AssetResponse(BaseModel):
    id: int
    filename: str
    label: str
    description: Optional[str]
    created_at: str

    class Config:
        from_attributes = True


@router.get("", response_model=List[AssetResponse])
def list_assets(
    label: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """List assets, optionally filtered by label. No auth required for listing (for picker UIs)."""
    label_enum = None
    if label is not None:
        try:
            label_enum = AssetLabel(label)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid label. Must be one of: {[e.value for e in AssetLabel]}",
            )
    assets = AssetDAO.get_all(db, label=label_enum)
    return [
        AssetResponse(
            id=a.id,
            filename=a.filename,
            label=a.label.value,
            description=a.description,
            created_at=a.created_at.isoformat() if a.created_at else "",
        )
        for a in assets
    ]


@router.get("/labels", response_model=List[dict])
def list_labels():
    """Return available asset labels (for filters and upload form)."""
    return [{"value": e.value, "label": e.name.replace("_", " ").title()} for e in AssetLabel]


@router.get("/{asset_id}", response_model=AssetResponse)
def get_asset(
    asset_id: int,
    db: Session = Depends(get_db),
):
    """Get a single asset by ID."""
    asset = AssetDAO.get_by_id(db, asset_id)
    if not asset:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
    return AssetResponse(
        id=asset.id,
        filename=asset.filename,
        label=asset.label.value,
        description=asset.description,
        created_at=asset.created_at.isoformat() if asset.created_at else "",
    )


@router.get("/{asset_id}/file")
def serve_asset_file(
    asset_id: int,
    db: Session = Depends(get_db),
):
    """Serve the asset image from DB."""
    asset = AssetDAO.get_by_id(db, asset_id)
    if not asset:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
    content = getattr(asset, "content", None)
    if not content or len(content) == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset has no content")
    media_type = getattr(asset, "content_type", None) or "application/octet-stream"
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f"inline; filename=\"{asset.filename}\""},
    )


@router.post("", response_model=AssetResponse, status_code=status.HTTP_201_CREATED)
async def upload_asset(
    file: UploadFile = File(...),
    label: str = Form(AssetLabel.GENERIC.value),
    description: Optional[str] = Form(None),
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Upload a new image asset. Admin only."""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Allowed types: {', '.join(ALLOWED_EXTENSIONS)}",
        )
    try:
        label_enum = AssetLabel(label)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid label. Must be one of: {[e.value for e in AssetLabel]}",
        )

    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File too large (max {MAX_FILE_SIZE // (1024*1024)} MB)",
        )

    content_type = EXTENSION_TO_MEDIA_TYPE.get(suffix, "application/octet-stream")

    asset = AssetDAO.create(
        db,
        filename=file.filename or f"image{suffix}",
        label=label_enum,
        description=description,
        content=content,
        content_type=content_type,
    )
    logger.info("Uploaded asset id=%s filename=%s label=%s (stored in DB)", asset.id, asset.filename, asset.label)
    return AssetResponse(
        id=asset.id,
        filename=asset.filename,
        label=asset.label.value,
        description=asset.description,
        created_at=asset.created_at.isoformat() if asset.created_at else "",
    )


@router.delete("/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_asset(
    asset_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Delete an asset. Clears server preview references first, then deletes the row."""
    asset = AssetDAO.get_by_id(db, asset_id)
    if not asset:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
    db.query(Server).filter(Server.preview_asset_id == asset_id).update({Server.preview_asset_id: None})
    AssetDAO.delete(db, asset_id)
    logger.info("Deleted asset id=%s", asset_id)
