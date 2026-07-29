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


def test_blank_password_disables_password_login(db_session):
    """A blank/unset password means password login is disabled, not an error."""
    user = User(username="client", email="client@test.com")
    assert user.password is None
    assert user.has_password is False
    # No password set yet: verify_password must safely return False, never raise.
    assert user.verify_password("anything") is False
    assert user.verify_password("") is False


def test_set_password_with_none_or_empty_clears_password(db_session):
    """Passing None or '' to set_password clears/blanks the password."""
    user = User(username="client2", email="client2@test.com")
    user.set_password("realpassword123")
    assert user.has_password is True

    user.set_password(None)
    assert user.password is None
    assert user.has_password is False
    assert user.verify_password("realpassword123") is False

    user.set_password("anotherpassword123")
    assert user.has_password is True
    user.set_password("")
    assert user.password is None
    assert user.has_password is False


def test_has_password_property(db_session):
    user = User(username="client3", email="client3@test.com")
    assert user.has_password is False
    user.set_password("hunter2hunter2")
    assert user.has_password is True




