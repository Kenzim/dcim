"""Client users (no impersonation, no password dump)."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import or_

from app.dao.permission_set_dao import PermissionSetDAO
from app.dao.user_dao import UserDAO
from app.mcp.instance import mcp
from app.mcp.runtime import run_tool
from app.mcp.serialize import client_row
from app.models.user import User


@mcp.tool()
async def list_clients(query: Optional[str] = None, limit: int = 50) -> dict:
    """List non-admin client accounts."""

    async def work(db, ctx):
        cap = max(1, min(int(limit or 50), 200))
        q = db.query(User).filter(User.is_admin.is_(False)).order_by(User.username)
        needle = (query or "").strip()
        if needle:
            like = f"%{needle}%"
            q = q.filter(or_(User.username.ilike(like), User.email.ilike(like)))
        rows = q.limit(cap).all()
        return {"clients": [client_row(u) for u in rows]}

    return await run_tool("list_clients", "read", work, args={"query": query, "limit": limit})


@mcp.tool()
async def get_client(user_id: int) -> dict:
    """Get a client profile (no password hash)."""

    async def work(db, ctx):
        row = UserDAO.get_by_id(db, user_id)
        if not row or row.is_admin:
            raise ValueError("Client not found")
        return client_row(row)

    return await run_tool("get_client", "read", work, args={"user_id": user_id})


@mcp.tool()
async def create_client(
    username: str, email: str, permission_set_id: Optional[int] = None
) -> dict:
    """Create a client account without a password (portal via SSO/impersonation only)."""

    async def work(db, ctx):
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


@mcp.tool()
async def set_client_permission_set(user_id: int, permission_set_id: Optional[int] = None) -> dict:
    """Assign or clear a client's default permission preset."""

    async def work(db, ctx):
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
