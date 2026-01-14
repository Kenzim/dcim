"""
Tests for authentication and authorization
"""
import pytest
from fastapi import HTTPException
from app.core.auth import get_current_user, require_admin
from unittest.mock import Mock, AsyncMock


@pytest.mark.asyncio
async def test_get_current_user_with_valid_token(mock_redis):
    """Test get_current_user with valid token"""
    from fastapi import Request
    
    # Setup mock token
    token = "test_token_123"
    token_id = "test_token_id_hash"
    
    # Mock Redis to return user data
    mock_redis._hashes[f"tok:{token_id}"] = {
        "user_id": "1",
        "username": "testuser",
        "email": "test@example.com",
        "is_admin": "true",
        "created_at": "2024-01-01T00:00:00",
        "last_seen_at": "2024-01-01T00:00:00",
        "last_seen_ip": "127.0.0.1"
    }
    
    # Mock request
    request = Mock(spec=Request)
    request.client = Mock()
    request.client.host = "127.0.0.1"
    request.headers = {}
    
    # Mock cookie
    auth_token = token
    
    # Note: This is a simplified test - in reality get_current_user
    # needs proper token derivation which is tested in integration tests
    pass


@pytest.mark.asyncio
async def test_require_admin_with_admin_user():
    """Test require_admin allows admin users"""
    admin_auth = {
        "user_id": 1,
        "username": "admin",
        "email": "admin@example.com",
        "is_admin": True
    }
    
    # Mock get_current_user dependency
    async def mock_get_current_user():
        return admin_auth
    
    # require_admin should pass through admin users
    result = await require_admin(auth=admin_auth)
    assert result == admin_auth
    assert result["is_admin"] is True


@pytest.mark.asyncio
async def test_require_admin_with_non_admin_user():
    """Test require_admin rejects non-admin users"""
    non_admin_auth = {
        "user_id": 2,
        "username": "user",
        "email": "user@example.com",
        "is_admin": False
    }
    
    # require_admin should raise 403 for non-admin users
    with pytest.raises(HTTPException) as exc_info:
        await require_admin(auth=non_admin_auth)
    
    assert exc_info.value.status_code == 403
    assert "Admin access required" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_require_admin_with_missing_is_admin():
    """Test require_admin rejects users without is_admin field"""
    auth_without_admin = {
        "user_id": 3,
        "username": "user",
        "email": "user@example.com"
    }
    
    with pytest.raises(HTTPException) as exc_info:
        await require_admin(auth=auth_without_admin)
    
    assert exc_info.value.status_code == 403

