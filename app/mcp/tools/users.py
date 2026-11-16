"""Client users (no impersonation, no password dump)."""

from __future__ import annotations

from typing import Literal, Optional

from sqlalchemy import or_
from starlette.requests import Request

from app.api import admin_users as users_api
from app.dao.permission_set_dao import PermissionSetDAO
from app.dao.user_dao import UserDAO
from app.mcp.instance import mcp
from app.mcp.runtime import run_tool
from app.mcp.serialize import client_row
from app.models.user import User
from app.services import user_invite_service


Kind = Literal["client", "staff"]


def _auth(ctx) -> dict:
    if ctx.created_by_user_id is None:
        raise ValueError("This MCP key has no owning user")
    return {"user_id": ctx.created_by_user_id}


def _request(ctx) -> Request:
    """Minimal request so the admin handlers record the key's IP like the UI does."""
    return Request({"type": "http", "headers": [], "client": (ctx.client_ip or "mcp", 0)})


def _account_row(db, row: User) -> dict:
    out = client_row(row)
    out["kind"] = user_invite_service.kind_of(row)
    out["invited"] = user_invite_service.is_pending(db, row)
    out["require_2fa"] = bool(row.require_2fa)
    return out


@mcp.tool()
async def list_clients(
    query: Optional[str] = None,
    limit: int = 50,
    user_id: Optional[int] = None,
    kind: Kind = "client",
    invited: Optional[bool] = None,
) -> dict:
    """List accounts: kind=client (default) or staff (admins). With user_id: that account. invited=true/false filters pending invitations."""

    def work(db, ctx):
        is_staff = kind == "staff"
        if user_id is not None:
            row = UserDAO.get_by_id(db, user_id)
            if not row or bool(row.is_admin) != is_staff:
                raise ValueError("Staff member not found" if is_staff else "Client not found")
            return _account_row(db, row)
        cap = max(1, min(int(limit or 50), 200))
        q = db.query(User).filter(User.is_admin.is_(is_staff)).order_by(User.username)
        needle = (query or "").strip()
        if needle:
            like = f"%{needle}%"
            q = q.filter(or_(User.username.ilike(like), User.email.ilike(like)))
        pending = user_invite_service.pending_user_ids(db)
        rows = [r for r in q.all() if invited is None or (r.id in pending) == invited][:cap]
        out = []
        for r in rows:
            item = client_row(r)
            item.update(kind=kind, invited=r.id in pending, require_2fa=bool(r.require_2fa))
            out.append(item)
        return {"clients": out}

    return await run_tool(
        "list_clients", "read", work, args={"query": query, "limit": limit, "user_id": user_id, "kind": kind}
    )


async def get_client(user_id: int) -> dict:
    """Get a client profile (no password hash)."""
    return await list_clients(user_id=user_id)


async def create_client(
    username: str, email: str, permission_set_id: Optional[int] = None
) -> dict:
    """Create a client account without a password (portal via SSO/impersonation only)."""

    def work(db, ctx):
        if UserDAO.get_by_username(db, username):
            raise ValueError("Username already in use")
        if UserDAO.get_by_email(db, email):
            raise ValueError("Email already in use")
        if permission_set_id is not None and PermissionSetDAO.get_by_id(db, permission_set_id) is None:
            raise ValueError("Permission set not found")
        row = UserDAO.create(db, username=username, email=email, password=None, is_admin=False)
        if permission_set_id is not None:
            row.permission_set_id = permission_set_id
            UserDAO.update(db, row)
        return client_row(row)

    return await run_tool(
        "create_client",
        "write",
        work,
        args={"username": username, "email": email, "permission_set_id": permission_set_id},
    )


async def set_client_permission_set(user_id: int, permission_set_id: Optional[int] = None) -> dict:
    """Assign or clear a client's default permission preset."""

    def work(db, ctx):
        row = UserDAO.get_by_id(db, user_id)
        if not row or row.is_admin:
            raise ValueError("Client not found")
        if permission_set_id is not None and PermissionSetDAO.get_by_id(db, permission_set_id) is None:
            raise ValueError("Permission set not found")
        row.permission_set_id = permission_set_id
        UserDAO.update(db, row)
        return client_row(row)

    return await run_tool(
        "set_client_permission_set",
        "write",
        work,
        args={"user_id": user_id, "permission_set_id": permission_set_id},
    )


async def invite_user(
    email: str, kind: Kind, username: Optional[str] = None, permission_set_id: Optional[int] = None
) -> dict:
    """Create a passwordless account and email its invitation link."""

    async def work(db, ctx):
        body = users_api.InviteCreate(
            email=email, kind=kind, username=username, permission_set_id=permission_set_id
        )
        result = await users_api.invite_user(body, _request(ctx), _auth(ctx), db)
        return result.model_dump()

    return await run_tool(
        "invite_user",
        "write",
        work,
        args={"email": email, "kind": kind, "username": username, "permission_set_id": permission_set_id},
    )


async def invite_action(user_id: int, action: Literal["resend", "revoke"]) -> dict:
    """Resend (fresh link, older ones retired) or revoke (kill the link, keep the account) a pending invitation."""

    async def work(db, ctx):
        handler = users_api.resend_invite if action == "resend" else users_api.revoke_invite
        result = await handler(user_id, _request(ctx), _auth(ctx), db)
        return result.model_dump()

    return await run_tool(f"invite_{action}", "write", work, args={"user_id": user_id})


async def set_require_2fa(user_id: int, kind: Kind, require_2fa: bool) -> dict:
    """Require (or stop requiring) two-factor authentication for an account."""

    async def work(db, ctx):
        handler = users_api.set_admin_require_2fa if kind == "staff" else users_api.set_client_require_2fa
        return await handler(user_id, users_api.RequireTwoFactorBody(require_2fa=require_2fa), _request(ctx), _auth(ctx), db)

    return await run_tool(
        "set_require_2fa", "write", work, args={"user_id": user_id, "kind": kind, "require_2fa": require_2fa}
    )


async def update_staff(user_id: int, email: str) -> dict:
    """Change a staff member's email."""

    async def work(db, ctx):
        result = await users_api.update_admin(user_id, users_api.AdminUpdate(email=email), _auth(ctx), db)
        return result.model_dump()

    return await run_tool("update_staff", "write", work, args={"user_id": user_id, "email": email})


@mcp.tool()
async def delete_staff(user_id: int, confirm: bool = False) -> dict:
    """Delete a staff (admin) account. Not your own, not the last admin."""

    async def work(db, ctx):
        if user_id == ctx.created_by_user_id:
            raise ValueError("Cannot delete the account that owns this MCP key")
        await users_api.delete_admin(user_id, _auth(ctx), db)
        return {"deleted": True, "user_id": user_id}

    return await run_tool(
        "delete_staff",
        "destructive",
        work,
        destructive=True,
        confirm=confirm,
        args={"user_id": user_id, "confirm": confirm},
    )
