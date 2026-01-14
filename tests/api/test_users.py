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


def test_get_current_user_with_bearer_token(client, test_user):
    """Test getting current user details using Bearer token"""
    # First login to get token
    login_response = client.post(
        "/api/users/login",
        json={
            "username": "testuser",
            "password": "testpassword123"
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
    assert data["username"] == "testuser"
    assert data["email"] == "test@example.com"
    assert data["id"] == test_user.id
    assert data["is_admin"] is False


def test_get_current_user_with_cookie(client, test_user):
    """Test getting current user details using cookie"""
    # First login to get cookie
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
    
    # Get user details - cookie should be sent automatically
    response = client.get("/api/users/me")
    
    assert response.status_code == 200
    data = response.json()
    assert data["username"] == "testuser"
    assert data["email"] == "test@example.com"
    assert data["id"] == test_user.id
    assert data["is_admin"] is False


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


def test_get_sessions(client, test_user, mock_redis):
    """Test getting user sessions"""
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


def test_get_sessions_multiple_tokens(client, test_user, mock_redis):
    """Test getting sessions with multiple tokens"""
    # Login twice to create two sessions
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
    assert response.status_code == 401


def test_logout_unauthorized(client):
    """Test logout without authentication"""
    response = client.post("/api/users/logout")
    assert response.status_code == 401


def test_get_sessions_unauthorized(client):
    """Test getting sessions without authentication"""
    response = client.get("/api/users/sessions")
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

