"""
Tests for User model
"""
import pytest
from app.models.user import User


def test_set_password(db_session):
    """Test password hashing"""
    user = User(username="test", email="test@test.com")
    user.set_password("mypassword")
    assert user.password != "mypassword"
    assert user.password.startswith("$2b$")  # bcrypt hash format


def test_verify_password(db_session):
    """Test password verification"""
    user = User(username="test", email="test@test.com")
    user.set_password("mypassword")
    assert user.verify_password("mypassword") is True
    assert user.verify_password("wrongpassword") is False


def test_password_too_long(db_session):
    """Test password length validation"""
    user = User(username="test", email="test@test.com")
    long_password = "a" * 100  # 100 bytes
    with pytest.raises(ValueError):
        user.set_password(long_password)

