"""MCP user tools: invitations, staff management and require-2FA."""

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy import select

from app.core.config import settings
from app.mcp.tools import combined
from app.mcp.tools.users import delete_staff, list_clients
from app.models.commerce_email import EmailMessage
from app.models.user import User


@pytest.fixture
def public_url(monkeypatch):
    monkeypatch.setattr(settings, "public_app_url", "https://rackflow.test")
    monkeypatch.setattr(settings, "commerce_public_app_url", None)


@pytest.mark.asyncio
async def test_invite_client_and_staff_with_status(db_session, mcp_sessionlocal, mcp_auth_ctx_write, public_url):
    client = await combined.save_client(email="c@example.com", invite=True)
    assert client["kind"] == "client" and client["username"].startswith("invite-")
    staff = await combined.save_client(email="s@example.com", username="staffer", kind="staff", invite=True)
    assert staff["kind"] == "staff"
    assert db_session.get(User, staff["user_id"]).is_admin is True

    emails = db_session.execute(select(EmailMessage.to_address)).scalars().all()
    assert {"c@example.com", "s@example.com"} <= set(emails)

    pending = await list_clients(invited=True)
    assert [r["email"] for r in pending["clients"]] == ["c@example.com"]
    staff_list = await list_clients(kind="staff", invited=True)
    assert [r["email"] for r in staff_list["clients"]] == ["s@example.com"]
    detail = await list_clients(user_id=client["user_id"])
    assert detail["invited"] is True and detail["has_password"] is False
    with pytest.raises(ToolError, match="Client not found"):
        await list_clients(user_id=staff["user_id"])


@pytest.mark.asyncio
async def test_resend_and_revoke(db_session, mcp_sessionlocal, mcp_auth_ctx_write, public_url):
    made = await combined.save_client(email="c@example.com", invite=True)
    resent = await combined.invite_action(made["user_id"], "resend")
    assert resent["user_id"] == made["user_id"]
    assert len(db_session.execute(select(EmailMessage)).scalars().all()) == 2
    await combined.invite_action(made["user_id"], "revoke")
    with pytest.raises(ToolError, match="User not found"):
        await combined.invite_action(999999, "resend")
    # A revoked account stays pending and can be invited again.
    await combined.invite_action(made["user_id"], "resend")


@pytest.mark.asyncio
async def test_save_client_validation(mcp_sessionlocal, mcp_auth_ctx_write, public_url):
    with pytest.raises(ToolError, match="invite=true"):
        await combined.save_client(email="s@example.com", username="s", kind="staff")
    with pytest.raises(ToolError, match="needs: email"):
        await combined.save_client(invite=True)
    with pytest.raises(ToolError, match="does not accept: permission_set_id"):
        await combined.save_client(user_id=2, kind="staff", permission_set_id=1)


@pytest.mark.asyncio
async def test_require_2fa_and_staff_email_update(
    db_session, mcp_sessionlocal, mcp_auth_ctx_write, public_url, monkeypatch
):
    from app.api import admin_users

    monkeypatch.setattr(admin_users, "restrict_sessions_for_2fa_enrollment", lambda _id: None)
    monkeypatch.setattr(admin_users, "lift_session_restrictions", lambda _id: None)
    made = await combined.save_client(email="s@example.com", kind="staff", invite=True)
    await combined.save_client(user_id=made["user_id"], kind="staff", require_2fa=True)
    updated = await combined.save_client(user_id=made["user_id"], kind="staff", email="s2@example.com")
    assert updated["email"] == "s2@example.com"
    db_session.expire_all()
    assert db_session.get(User, made["user_id"]).require_2fa is True

    cl = await combined.save_client(email="c@example.com", invite=True)
    res = await combined.save_client(user_id=cl["user_id"], require_2fa=True)
    assert res == {"require_2fa": True}
    db_session.expire_all()
    assert db_session.get(User, cl["user_id"]).require_2fa is True


@pytest.mark.asyncio
async def test_delete_staff_needs_confirm_and_destructive_scope(
    db_session, mcp_sessionlocal, mcp_auth_ctx_write, public_url
):
    made = await combined.save_client(email="s@example.com", kind="staff", invite=True)
    with pytest.raises(ToolError, match="lacks required scope"):
        await delete_staff(made["user_id"], confirm=True)


@pytest.mark.asyncio
async def test_delete_staff(db_session, mcp_sessionlocal, mcp_auth_ctx_destructive, public_url):
    owner = User(username="owner", email="owner@example.com", is_admin=True)
    other = User(username="other", email="other@example.com", is_admin=True)
    db_session.add_all([owner, other])
    db_session.commit()
    with pytest.raises(ToolError, match="confirm=true"):
        await delete_staff(other.id, confirm=False)
    other_id = other.id
    assert (await delete_staff(other_id, confirm=True))["deleted"] is True
    db_session.expire_all()
    assert db_session.get(User, other_id) is None
    with pytest.raises(ToolError, match="Admin not found"):
        await delete_staff(other_id, confirm=True)
