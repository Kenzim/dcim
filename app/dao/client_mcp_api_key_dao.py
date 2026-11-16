from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from app.core.client_mcp_auth import generate_client_mcp_api_key, hash_client_mcp_api_key
from app.mcp.scopes import normalize_scopes
from app.models.client_mcp_api_key import ClientMcpApiKey


class ClientMcpApiKeyDAO:
    """Data access for ``ClientMcpApiKey``. Lookups by owner never cross users."""

    @staticmethod
    def create(
        db: Session,
        *,
        user_id: int,
        name: str,
        scopes: Optional[List[str]] = None,
        expires_at: Optional[datetime] = None,
        enabled: bool = True,
        api_key: Optional[str] = None,
    ) -> ClientMcpApiKey:
        plaintext = api_key or generate_client_mcp_api_key()
        row = ClientMcpApiKey(
            user_id=user_id,
            name=name,
            enabled=enabled,
            api_key=hash_client_mcp_api_key(plaintext),
            api_key_prefix=plaintext[:12],
            scopes=normalize_scopes(scopes),
            expires_at=expires_at,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        row.plaintext_api_key = plaintext
        return row

    @staticmethod
    def get_by_api_key(db: Session, api_key: str) -> Optional[ClientMcpApiKey]:
        return (
            db.query(ClientMcpApiKey)
            .filter(ClientMcpApiKey.api_key == hash_client_mcp_api_key(api_key))
            .first()
        )

    @staticmethod
    def get_for_user(db: Session, user_id: int, key_id: int) -> Optional[ClientMcpApiKey]:
        return (
            db.query(ClientMcpApiKey)
            .filter(ClientMcpApiKey.id == key_id, ClientMcpApiKey.user_id == user_id)
            .first()
        )

    @staticmethod
    def list_for_user(db: Session, user_id: int) -> List[ClientMcpApiKey]:
        return (
            db.query(ClientMcpApiKey)
            .filter(ClientMcpApiKey.user_id == user_id)
            .order_by(ClientMcpApiKey.id.desc())
            .all()
        )

    @staticmethod
    def count_for_user(db: Session, user_id: int) -> int:
        return db.query(ClientMcpApiKey).filter(ClientMcpApiKey.user_id == user_id).count()

    @staticmethod
    def update(db: Session, row: ClientMcpApiKey) -> ClientMcpApiKey:
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def delete(db: Session, row: ClientMcpApiKey) -> None:
        db.delete(row)
        db.commit()

    @staticmethod
    def touch_last_used(db: Session, row: ClientMcpApiKey, client_ip: Optional[str]) -> None:
        row.last_used_at = datetime.now(timezone.utc)
        row.last_used_ip = client_ip
        db.commit()
