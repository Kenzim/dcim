"""
Tests for user API endpoints
"""
import pytest
import hashlib


def _derive_token_id(token: str) -> str:
    """Derive token_id from token using SHA256 (same as auth.py)"""
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def test_login_success(client, test_user, mock_redis):
    """Test successful login"""
    response = client.post(
        "/api/users/login",
        json={
            "username": "testuser",
            "password": "testpassword123"
        }
    )
    
    assert response.status_code == 200
    data = response.json()
    
    # Check response structure
    assert "token" in data
    assert "user_id" in data
    assert "username" in data
    assert "is_admin" in data
    
    # Check values
    assert data["username"] == "testuser"
    assert data["user_id"] == test_user.id
    assert data["is_admin"] is False
    
    # Check token was stored in Redis HASH
    token_id = _derive_token_id(data["token"])
    token_key = f"tok:{token_id}"
    stored_data = mock_redis.hgetall(token_key)
    assert stored_data is not None
    assert len(stored_data) > 0
    
    # Verify stored data
    assert stored_data["user_id"] == str(test_user.id)
    assert stored_data["username"] == "testuser"
    assert stored_data["email"] == "test@example.com"
    
    # Check token_id was added to user's ZSET
    user_toks_key = f"user_toks:{test_user.id}"
    assert user_toks_key in mock_redis._zsets
    assert token_id in mock_redis._zsets[user_toks_key]
    
    # Check cookie was set
    assert "auth_token" in response.cookies
    assert response.cookies["auth_token"] == data["token"]


def test_login_invalid_username(client, test_user):
    """Test login with invalid username"""
    response = client.post(
        "/api/users/login",
        json={
            "username": "nonexistent",
            "password": "testpassword123"
        }
    )
    assert response.status_code == 401
    assert "Invalid" in response.json()["detail"]


def test_login_invalid_password(client, test_user):
    """Test login with invalid password"""
    response = client.post(
        "/api/users/login",
        json={
            "username": "testuser",
            "password": "wrongpassword"
        }
    )
    assert response.status_code == 401
    assert "Invalid" in response.json()["detail"]


def test_login_with_email(client, test_user):
    """Test login using email instead of username"""
    response = client.post(
        "/api/users/login",
        json={
            "email": "test@example.com",
            "password": "testpassword123"
        }
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["username"] == "testuser"
    assert "token" in data


def test_get_current_user_requires_admin(client, test_user):
    """Test that getting current user details requires admin"""
    # First login to get token (non-admin user)
    login_response = client.post(
        "/api/users/login",
        json={
            "username": "testuser",
            "password": "testpassword123"
        }
    )
    token = login_response.json()["token"]
    
    # Get user details using Bearer token - should fail with 403
    response = client.get(
        "/api/users/me",
        headers={"Authorization": f"Bearer {token}"}
    )
    
    assert response.status_code == 403
    assert "Admin access required" in response.json()["detail"]


def test_get_current_user_with_admin(client, test_admin_user):
    """Test getting current user details with admin user"""
    # First login as admin to get token
    login_response = client.post(
        "/api/users/login",
        json={
            "username": "admin",
            "password": "adminpassword123"
        }
    )
    token = login_response.json()["token"]
    
    # Get user details using Bearer token
    response = client.get(
        "/api/users/me",
        headers={"Authorization": f"Bearer {token}"}
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["username"] == "admin"
    assert data["email"] == "admin@example.com"
    assert data["id"] == test_admin_user.id
    assert data["is_admin"] is True


def test_get_current_user_with_cookie_requires_admin(client, test_user):
    """Test that getting current user details with cookie requires admin"""
    # First login to get cookie (non-admin user)
    login_response = client.post(
        "/api/users/login",
        json={
            "username": "testuser",
            "password": "testpassword123"
        }
    )
    
    # Cookie should be set automatically
    token = login_response.cookies.get("auth_token")
    assert token is not None
    
    # Get user details - should fail with 403
    response = client.get("/api/users/me")
    
    assert response.status_code == 403
    assert "Admin access required" in response.json()["detail"]


def test_logout_with_cookie(client, test_user, mock_redis):
    """Test logout using cookie"""
    # Login first
    login_response = client.post(
        "/api/users/login",
        json={
            "username": "testuser",
            "password": "testpassword123"
        }
    )
    token = login_response.json()["token"]
    token_id = _derive_token_id(token)
    token_key = f"tok:{token_id}"
    
    # Verify token exists in Redis
    assert mock_redis.hgetall(token_key) is not None
    assert len(mock_redis.hgetall(token_key)) > 0
    
    # Logout
    logout_response = client.post("/api/users/logout")
    
    assert logout_response.status_code == 200
    assert logout_response.json()["message"] == "Logged out successfully"
    
    # Verify token was deleted from Redis
    assert len(mock_redis.hgetall(token_key)) == 0
    
    # Verify token_id was removed from user's ZSET
    user_toks_key = f"user_toks:{test_user.id}"
    assert token_id not in mock_redis._zsets.get(user_toks_key, {})
    
    # Verify cookie was cleared - TestClient may not show deleted cookies
    # Try to access /me endpoint - should fail without cookie
    me_response = client.get("/api/users/me")
    assert me_response.status_code == 401


def test_logout_with_bearer_token(client, test_user, mock_redis):
    """Test logout using Bearer token"""
    # Login first
    login_response = client.post(
        "/api/users/login",
        json={
            "username": "testuser",
            "password": "testpassword123"
        }
    )
    token = login_response.json()["token"]
    token_id = _derive_token_id(token)
    token_key = f"tok:{token_id}"
    
    # Logout with Bearer token
    logout_response = client.post(
        "/api/users/logout",
        headers={"Authorization": f"Bearer {token}"}
    )
    
    assert logout_response.status_code == 200
    
    # Verify token was deleted
    assert len(mock_redis.hgetall(token_key)) == 0


def test_get_sessions_requires_admin(client, test_user, mock_redis):
    """Test that getting sessions requires admin"""
    # Login first (non-admin user)
    login_response = client.post(
        "/api/users/login",
        json={
            "username": "testuser",
            "password": "testpassword123"
        }
    )
    token = login_response.json()["token"]
    
    # Get sessions - should fail with 403
    response = client.get("/api/users/sessions")
    
    assert response.status_code == 403
    assert "Admin access required" in response.json()["detail"]


def test_get_sessions_with_admin(client, test_admin_user, mock_redis):
    """Test getting user sessions with admin user"""
    # Login as admin
    login_response = client.post(
        "/api/users/login",
        json={
            "username": "admin",
            "password": "adminpassword123"
        }
    )
    token = login_response.json()["token"]
    token_id = _derive_token_id(token)
    
    # Get sessions
    response = client.get("/api/users/sessions")
    
    assert response.status_code == 200
    sessions = response.json()
    assert isinstance(sessions, list)
    assert len(sessions) == 1
    
    session = sessions[0]
    assert "token" in session
    assert "created_at" in session
    assert "last_seen_at" in session
    assert "last_seen_ip" in session
    assert "is_current" in session
    assert session["is_current"] is True
    # Token should be masked
    assert "..." in session["token"]


def test_get_sessions_multiple_tokens_with_admin(client, test_admin_user, mock_redis):
    """Test getting sessions with multiple tokens (admin user)"""
    # Login twice to create two sessions
    login1 = client.post(
        "/api/users/login",
        json={
            "username": "admin",
            "password": "adminpassword123"
        }
    )
    token1 = login1.json()["token"]
    token_id1 = _derive_token_id(token1)
    
    login2 = client.post(
        "/api/users/login",
        json={
            "username": "admin",
            "password": "adminpassword123"
        }
    )
    token2 = login2.json()["token"]
    token_id2 = _derive_token_id(token2)
    
    # Get sessions using token2 (should show both, token2 marked as current)
    response = client.get(
        "/api/users/sessions",
        cookies={"auth_token": token2}
    )
    
    assert response.status_code == 200
    sessions = response.json()
    assert len(sessions) == 2
    
    # Find current session
    current_session = next(s for s in sessions if s["is_current"])
    assert current_session is not None
    # Current session should be token2
    assert token_id2[:8] in current_session["token"]


def test_get_current_user_unauthorized(client):
    """Test getting current user without authentication"""
    response = client.get("/api/users/me")
    # Should be 401 (unauthorized) not 403 (forbidden) when no auth
    assert response.status_code == 401


def test_logout_unauthorized(client):
    """Test logout without authentication"""
    response = client.post("/api/users/logout")
    assert response.status_code == 401


def test_get_sessions_unauthorized(client):
    """Test getting sessions without authentication"""
    response = client.get("/api/users/sessions")
    # Should be 401 (unauthorized) not 403 (forbidden) when no auth
    assert response.status_code == 401


def test_login_password_too_long(client, test_user):
    """Test login with password exceeding 72 bytes"""
    long_password = "a" * 73  # 73 bytes
    response = client.post(
        "/api/users/login",
        json={
            "username": "testuser",
            "password": long_password
        }
    )
    # Should fail validation before reaching password check
    assert response.status_code == 422  # Validation error


def test_admin_login(client, test_admin_user, mock_redis):
    """Test admin user login"""
    response = client.post(
        "/api/users/login",
        json={
            "username": "admin",
            "password": "adminpassword123"
        }
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["is_admin"] is True
    assert data["username"] == "admin"
    
    # Verify admin flag in Redis
    token_id = _derive_token_id(data["token"])
    token_key = f"tok:{token_id}"
    stored_data = mock_redis.hgetall(token_key)
    assert stored_data["is_admin"] == "true"


def test_delete_session_requires_admin(client, test_user, mock_redis):
    """Test that deleting a session requires admin"""
    # Login twice to create two sessions (non-admin user)
    login1 = client.post(
        "/api/users/login",
        json={
            "username": "testuser",
            "password": "testpassword123"
        }
    )
    token1 = login1.json()["token"]
    token_id1 = _derive_token_id(token1)
    
    login2 = client.post(
        "/api/users/login",
        json={
            "username": "testuser",
            "password": "testpassword123"
        }
    )
    token2 = login2.json()["token"]
    
    # Try to delete session - should fail with 403
    response = client.delete(
        f"/api/users/sessions/{token_id1}",
        cookies={"auth_token": token2}
    )
    
    assert response.status_code == 403
    assert "Admin access required" in response.json()["detail"]


def test_delete_session_with_admin(client, test_admin_user, mock_redis):
    """Test deleting a session with admin user"""
    # Login twice to create two sessions
    login1 = client.post(
        "/api/users/login",
        json={
            "username": "admin",
            "password": "adminpassword123"
        }
    )
    token1 = login1.json()["token"]
    token_id1 = _derive_token_id(token1)
    
    login2 = client.post(
        "/api/users/login",
        json={
            "username": "admin",
            "password": "adminpassword123"
        }
    )
    token2 = login2.json()["token"]
    token_id2 = _derive_token_id(token2)
    
    # Verify both tokens exist
    assert len(mock_redis.hgetall(f"tok:{token_id1}")) > 0
    assert len(mock_redis.hgetall(f"tok:{token_id2}")) > 0
    
    # Delete session 1 using token2 (current session)
    response = client.delete(
        f"/api/users/sessions/{token_id1}",
        cookies={"auth_token": token2}
    )
    
    assert response.status_code == 200
    assert response.json()["message"] == "Session deleted successfully"
    
    # Verify token1 was deleted
    assert len(mock_redis.hgetall(f"tok:{token_id1}")) == 0
    
    # Verify token1 was removed from ZSET
    user_toks_key = f"user_toks:{test_admin_user.id}"
    assert token_id1 not in mock_redis._zsets.get(user_toks_key, {})
    
    # Verify token2 still exists
    assert len(mock_redis.hgetall(f"tok:{token_id2}")) > 0


def test_delete_current_session_with_admin(client, test_admin_user, mock_redis):
    """Test that deleting current session is not allowed (admin user)"""
    # Login as admin
    login_response = client.post(
        "/api/users/login",
        json={
            "username": "admin",
            "password": "adminpassword123"
        }
    )
    token = login_response.json()["token"]
    token_id = _derive_token_id(token)
    
    # Try to delete current session
    response = client.delete(
        f"/api/users/sessions/{token_id}",
        cookies={"auth_token": token}
    )
    
    assert response.status_code == 400
    assert "Cannot delete current session" in response.json()["detail"]


def test_delete_nonexistent_session_with_admin(client, test_admin_user, mock_redis):
    """Test deleting a non-existent session (admin user)"""
    # Login as admin
    login_response = client.post(
        "/api/users/login",
        json={
            "username": "admin",
            "password": "adminpassword123"
        }
    )
    token = login_response.json()["token"]
    
    # Try to delete non-existent session
    fake_token_id = "nonexistent_token_id_12345"
    response = client.delete(
        f"/api/users/sessions/{fake_token_id}",
        cookies={"auth_token": token}
    )
    
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_delete_other_user_session(client, test_user, test_admin_user, mock_redis):
    """Test that non-admin users cannot delete sessions (requires admin)"""
    # Login as test_user (non-admin)
    login1 = client.post(
        "/api/users/login",
        json={
            "username": "testuser",
            "password": "testpassword123"
        }
    )
    token1 = login1.json()["token"]
    
    # Login as admin_user
    login2 = client.post(
        "/api/users/login",
        json={
            "username": "admin",
            "password": "adminpassword123"
        }
    )
    token2 = login2.json()["token"]
    token_id2 = _derive_token_id(token2)
    
    # Try to delete admin's session while logged in as test_user
    # Should fail with admin requirement, not "other users" error
    response = client.delete(
        f"/api/users/sessions/{token_id2}",
        cookies={"auth_token": token1}
    )
    
    assert response.status_code == 403
    assert "admin access required" in response.json()["detail"].lower()


def test_delete_other_user_session_with_admin(client, test_user, test_admin_user, mock_redis):
    """Test that admin cannot delete other users' sessions (API still restricts to own sessions)"""
    # Login as test_user
    login1 = client.post(
        "/api/users/login",
        json={
            "username": "testuser",
            "password": "testpassword123"
        }
    )
    token1 = login1.json()["token"]
    token_id1 = _derive_token_id(token1)
    
    # Login as admin_user
    login2 = client.post(
        "/api/users/login",
        json={
            "username": "admin",
            "password": "adminpassword123"
        }
    )
    token2 = login2.json()["token"]
    
    # Try to delete test_user's session while logged in as admin
    # API still checks that token belongs to current user, so admin can't delete other users' sessions
    response = client.delete(
        f"/api/users/sessions/{token_id1}",
        cookies={"auth_token": token2}
    )
    
    # Admin cannot delete other users' sessions (API restriction)
    assert response.status_code == 403
    assert "other users" in response.json()["detail"].lower()


def test_get_sessions_includes_token_id_with_admin(client, test_admin_user, mock_redis):
    """Test that get sessions includes token_id for deletion (admin user)"""
    # Login as admin
    login_response = client.post(
        "/api/users/login",
        json={
            "username": "admin",
            "password": "adminpassword123"
        }
    )
    token = login_response.json()["token"]
    token_id = _derive_token_id(token)
    
    # Get sessions
    response = client.get("/api/users/sessions", cookies={"auth_token": token})
    
    assert response.status_code == 200
    sessions = response.json()
    assert len(sessions) == 1
    assert "token_id" in sessions[0]
    assert sessions[0]["token_id"] == token_id
    assert sessions[0]["token"] == token_id[:8] + "..." + token_id[-8:]  # Masked

