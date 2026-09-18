"""Discord OAuth account linking."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.core.config import settings
from app.dao.user_dao import UserDAO
from app.models.commerce_auth_extra import ExternalIdentityProvider, UserExternalIdentity
from app.services.discord_link_service import DiscordLinkError, DiscordLinkService


def test_require_config_and_authorize_url(monkeypatch):
    monkeypatch.setattr(settings, "discord_client_id", None)
    monkeypatch.setattr(settings, "discord_client_secret", None)
    with pytest.raises(DiscordLinkError, match="not configured"):
        DiscordLinkService.build_authorize_url("st")
    monkeypatch.setattr(settings, "discord_client_id", "cid")
    monkeypatch.setattr(settings, "discord_client_secret", SimpleNamespace(get_secret_value=lambda: "sec"))
    monkeypatch.setattr(settings, "discord_redirect_uri", None)
    with pytest.raises(DiscordLinkError, match="redirect"):
        DiscordLinkService.build_authorize_url("st")
    monkeypatch.setattr(settings, "discord_redirect_uri", "https://app.example/discord")
    url = DiscordLinkService.build_authorize_url("abc")
    assert "client_id=cid" in url
    assert "state=abc" in url


def test_encrypt_token_passthrough(monkeypatch):
    assert DiscordLinkService._encrypt_token(None) is None
    monkeypatch.setattr(
        "app.services.discord_link_service.encrypt_api_key", lambda token: f"enc:{token}"
    )
    assert DiscordLinkService._encrypt_token("tok") == "enc:tok"
    monkeypatch.setattr("app.services.discord_link_service.encrypt_api_key", lambda token: None)
    assert DiscordLinkService._encrypt_token("tok") == "tok"


def test_exchange_and_fetch_user(monkeypatch):
    monkeypatch.setattr(settings, "discord_client_id", "cid")
    monkeypatch.setattr(settings, "discord_client_secret", SimpleNamespace(get_secret_value=lambda: "sec"))
    monkeypatch.setattr(settings, "discord_redirect_uri", "https://app.example/discord")

    token_resp = Mock()
    token_resp.raise_for_status = Mock()
    token_resp.json.return_value = {"access_token": "at"}
    me_resp = Mock()
    me_resp.raise_for_status = Mock()
    me_resp.json.return_value = {"id": "99", "username": "n"}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            return token_resp

        def get(self, *a, **k):
            return me_resp

    monkeypatch.setattr("app.services.discord_link_service.httpx.Client", FakeClient)
    assert DiscordLinkService.exchange_code("code")["access_token"] == "at"
    assert DiscordLinkService._fetch_user("at")["id"] == "99"


def test_link_and_unlink_user(db_session, monkeypatch, test_user):
    monkeypatch.setattr(settings, "discord_client_id", "cid")
    monkeypatch.setattr(settings, "discord_client_secret", SimpleNamespace(get_secret_value=lambda: "sec"))
    monkeypatch.setattr(settings, "discord_redirect_uri", "https://app.example/discord")
    monkeypatch.setattr(
        DiscordLinkService,
        "exchange_code",
        staticmethod(lambda code: {"access_token": "at", "refresh_token": "rt"}),
    )
    monkeypatch.setattr(
        DiscordLinkService,
        "_fetch_user",
        staticmethod(lambda token: {"id": "discord-1", "username": "duser"}),
    )
    row = DiscordLinkService.link_user(db_session, user_id=test_user.id, code="abc")
    assert row.provider == ExternalIdentityProvider.DISCORD
    assert row.provider_user_id == "discord-1"
    again = DiscordLinkService.link_user(db_session, user_id=test_user.id, code="abc")
    assert again.provider_user_id == "discord-1"
    assert DiscordLinkService.unlink_user(db_session, user_id=test_user.id) is True
    assert DiscordLinkService.unlink_user(db_session, user_id=test_user.id) is False

    monkeypatch.setattr(
        DiscordLinkService,
        "exchange_code",
        staticmethod(lambda code: {}),
    )
    with pytest.raises(DiscordLinkError, match="access token"):
        DiscordLinkService.link_user(db_session, user_id=test_user.id, code="x")

    monkeypatch.setattr(
        DiscordLinkService,
        "exchange_code",
        staticmethod(lambda code: {"access_token": "at"}),
    )
    monkeypatch.setattr(
        DiscordLinkService,
        "_fetch_user",
        staticmethod(lambda token: {"username": "noid"}),
    )
    with pytest.raises(DiscordLinkError, match="user id"):
        DiscordLinkService.link_user(db_session, user_id=test_user.id, code="x")

    other = UserDAO.create(db_session, username="discord-other", email="discord-other@example.com")
    monkeypatch.setattr(
        DiscordLinkService,
        "_fetch_user",
        staticmethod(lambda token: {"id": "taken-discord", "username": "taken"}),
    )
    DiscordLinkService.link_user(db_session, user_id=other.id, code="abc")
    with pytest.raises(DiscordLinkError, match="already linked"):
        DiscordLinkService.link_user(db_session, user_id=test_user.id, code="abc")
