"""Discord OAuth account linking for commerce onboarding."""

from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.service_instance_crypto import encrypt_api_key
from app.models.commerce_auth_extra import ExternalIdentityProvider, UserExternalIdentity

logger = logging.getLogger(__name__)

_DISCORD_API = "https://discord.com/api/v10"
_DISCORD_AUTH = "https://discord.com/api/oauth2/authorize"


class DiscordLinkError(ValueError):
    pass


class DiscordLinkService:
    @staticmethod
    def _require_config() -> None:
        if not settings.discord_client_id or not settings.discord_client_secret:
            raise DiscordLinkError("Discord OAuth is not configured")
        if not settings.discord_redirect_uri:
            raise DiscordLinkError("Discord redirect URI is not configured")

    @staticmethod
    def build_authorize_url(state: str) -> str:
        DiscordLinkService._require_config()
        params = {
            "client_id": settings.discord_client_id,
            "redirect_uri": settings.discord_redirect_uri,
            "response_type": "code",
            "scope": "identify",
            "state": state,
            "prompt": "consent",
        }
        return f"{_DISCORD_AUTH}?{urlencode(params)}"

    @staticmethod
    def _encrypt_token(token: Optional[str]) -> Optional[str]:
        if not token:
            return None
        encrypted = encrypt_api_key(token)
        return encrypted if encrypted is not None else token

    @staticmethod
    def exchange_code(code: str) -> dict[str, Any]:
        DiscordLinkService._require_config()
        data = {
            "client_id": settings.discord_client_id,
            "client_secret": settings.discord_client_secret.get_secret_value(),
            "grant_type": "authorization_code",
            "code": code.strip(),
            "redirect_uri": settings.discord_redirect_uri,
        }
        with httpx.Client(timeout=15.0) as client:
            response = client.post(
                f"{_DISCORD_API}/oauth2/token",
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response.raise_for_status()
            return response.json()

    @staticmethod
    def _fetch_user(access_token: str) -> dict[str, Any]:
        with httpx.Client(timeout=15.0) as client:
            response = client.get(
                f"{_DISCORD_API}/users/@me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            response.raise_for_status()
            return response.json()

    @staticmethod
    def link_user(
        db: Session,
        *,
        user_id: int,
        code: str,
    ) -> UserExternalIdentity:
        token_payload = DiscordLinkService.exchange_code(code)
        access_token = str(token_payload.get("access_token") or "")
        refresh_token = str(token_payload.get("refresh_token") or "")
        if not access_token:
            raise DiscordLinkError("Discord token exchange did not return an access token")

        profile = DiscordLinkService._fetch_user(access_token)
        provider_user_id = str(profile.get("id") or "")
        if not provider_user_id:
            raise DiscordLinkError("Discord profile is missing user id")

        existing_for_provider = db.execute(
            select(UserExternalIdentity).where(
                UserExternalIdentity.provider == ExternalIdentityProvider.DISCORD,
                UserExternalIdentity.provider_user_id == provider_user_id,
            )
        ).scalar_one_or_none()
        if existing_for_provider is not None and existing_for_provider.user_id != user_id:
            raise DiscordLinkError("This Discord account is already linked to another user")

        row = db.execute(
            select(UserExternalIdentity).where(
                UserExternalIdentity.user_id == user_id,
                UserExternalIdentity.provider == ExternalIdentityProvider.DISCORD,
            )
        ).scalar_one_or_none()
        username = profile.get("global_name") or profile.get("username")
        if row is None:
            row = UserExternalIdentity(
                user_id=user_id,
                provider=ExternalIdentityProvider.DISCORD,
                provider_user_id=provider_user_id,
                username=str(username) if username else None,
                access_token_encrypted=DiscordLinkService._encrypt_token(access_token),
                refresh_token_encrypted=DiscordLinkService._encrypt_token(refresh_token),
            )
            db.add(row)
        else:
            row.provider_user_id = provider_user_id
            row.username = str(username) if username else row.username
            row.access_token_encrypted = DiscordLinkService._encrypt_token(access_token)
            row.refresh_token_encrypted = DiscordLinkService._encrypt_token(refresh_token)
        db.flush()
        return row

    @staticmethod
    def unlink_user(db: Session, *, user_id: int) -> bool:
        row = db.execute(
            select(UserExternalIdentity).where(
                UserExternalIdentity.user_id == user_id,
                UserExternalIdentity.provider == ExternalIdentityProvider.DISCORD,
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        db.delete(row)
        db.flush()
        return True
