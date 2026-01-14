"""
User fixtures for testing
"""
import pytest
from app.models.user import User


@pytest.fixture
def test_user(db_session):
    """Create a test user"""
    user = User(
        username="testuser",
        email="test@example.com",
        is_admin=False
    )
    user.set_password("testpassword123")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def test_admin_user(db_session):
    """Create a test admin user"""
    user = User(
        username="admin",
        email="admin@example.com",
        is_admin=True
    )
    user.set_password("adminpassword123")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user

