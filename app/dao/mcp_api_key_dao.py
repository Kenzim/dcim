from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

from app.core.mcp_auth import generate_mcp_api_key, hash_mcp_api_key
from app.mcp.scopes import normalize_scopes
from app.models.mcp_api_key import McpApiKey


class McpApiKeyDAO:
    """Data Access Object for McpApiKey."""

    @staticmethod
    def create(
        db: Session,
        *,
        name: str,
        description: Optional[str] = None,
        enabled: bool = False,
        scopes: Optional[List[str]] = None,
        ip_allowlist: Optional[List[str]] = None,
        expires_at: Optional[datetime] = None,
        created_by_user_id: Optional[int] = None,
        api_key: Optional[str] = None,
    ) -> McpApiKey:
        plaintext = api_key or generate_mcp_api_key()
        row = McpApiKey(
            name=name,
            description=description,
            enabled=enabled,
            api_key=hash_mcp_api_key(plaintext),
            api_key_prefix=plaintext[:12],
            scopes=normalize_scopes(scopes),
            ip_allowlist=ip_allowlist,
            expires_at=expires_at,
            created_by_user_id=created_by_user_id,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        row.plaintext_api_key = plaintext
        return row

    @staticmethod
    def get_by_id(db: Session, key_id: int) -> Optional[McpApiKey]:
        return db.query(McpApiKey).filter(McpApiKey.id == key_id).first()

    @staticmethod
    def get_by_api_key(db: Session, api_key: str) -> Optional[McpApiKey]:
        return (
            db.query(McpApiKey)
            .filter(McpApiKey.api_key == hash_mcp_api_key(api_key))
            .first()
        )

    @staticmethod
    def get_all(db: Session, enabled_only: bool = False) -> List[McpApiKey]:
        query = db.query(McpApiKey)
        if enabled_only:
            query = query.filter(McpApiKey.enabled.is_(True))
        return query.order_by(McpApiKey.id.desc()).all()

    @staticmethod
    def update(db: Session, row: McpApiKey) -> McpApiKey:
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def rotate_api_key(db: Session, row: McpApiKey) -> McpApiKey:
        plaintext = generate_mcp_api_key()
        row.api_key = hash_mcp_api_key(plaintext)
        row.api_key_prefix = plaintext[:12]
        db.commit()
        db.refresh(row)
        row.plaintext_api_key = plaintext
        return row

    @staticmethod
    def delete(db: Session, key_id: int) -> bool:
        row = db.query(McpApiKey).filter(McpApiKey.id == key_id).first()
        if not row:
            return False
        db.delete(row)
        db.commit()
        return True

    @staticmethod
    def touch_last_used(
        db: Session, row: McpApiKey, client_ip: Optional[str]
    ) -> McpApiKey:
        from datetime import timezone

        row.last_used_at = datetime.now(timezone.utc)
        row.last_used_ip = client_ip
        db.commit()
        db.refresh(row)
        return row
