"""
Tests for User DAO
"""
import pytest
from app.dao import UserDAO


def test_create_user(db_session):
    """Test creating a user"""
    user = UserDAO.create(
        db_session,
        username="newuser",
        email="newuser@example.com",
        password="password123",
        is_admin=False
    )
    assert user.id is not None
    assert user.username == "newuser"
    assert user.email == "newuser@example.com"
    assert user.is_admin is False
    assert user.password != "password123"  # Should be hashed


def test_create_user_with_no_password(db_session):
    """Non-admin clients can be created with a blank password (portal access via
    impersonation / billing SSO only, until an admin sets a password)."""
    user = UserDAO.create(
        db_session,
        username="noPasswordClient",
        email="nopassword@example.com",
        is_admin=False,
    )
    assert user.id is not None
    assert user.password is None
    assert user.has_password is False
    assert user.verify_password("") is False
    assert user.verify_password("anything") is False


def test_get_by_username(db_session, test_user):
    """Test getting user by username"""
    user = UserDAO.get_by_username(db_session, "testuser")
    assert user is not None
    assert user.username == "testuser"


def test_get_by_email(db_session, test_user):
    """Test getting user by email"""
    user = UserDAO.get_by_email(db_session, "test@example.com")
    assert user is not None
    assert user.email == "test@example.com"


def test_get_by_id(db_session, test_user):
    """Test getting user by ID"""
    user = UserDAO.get_by_id(db_session, test_user.id)
    assert user is not None
    assert user.id == test_user.id




