"""
Pytest configuration and shared fixtures
"""
import os

# Use SQLite for tests - must be set before app imports so migrations use it
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
# Disable initial admin creation so test fixtures can create users without conflicts
os.environ["INITIAL_ADMIN_USERNAME"] = ""
os.environ["INITIAL_ADMIN_PASSWORD"] = ""
# Tests intentionally exercise the plaintext/no-encryption-key path for service
# instance API keys (see tests/dao/test_service_instance_dao.py), so opt out of
# the secure-by-default production requirement here.
os.environ["REQUIRE_SERVICE_INSTANCE_ENCRYPTION"] = "false"
# Some tests introspect /openapi.json to assert on route schemas; keep the
# docs endpoints enabled in the test environment even though they're disabled
# by default in production.
os.environ["DISABLE_PUBLIC_API_DOCS"] = "false"
# Keep /mcp unmounted unless a test explicitly remounts it. A developer .env
# with MCP_ENABLED=true would otherwise import FastMCP during app import.
os.environ["MCP_ENABLED"] = "false"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from unittest.mock import Mock

from app.main import app
from app.core.database import Base, get_db
from app.models.user import User


# Test database URL (in-memory SQLite for testing)
SQLALCHEMY_TEST_DATABASE_URL = "sqlite:///:memory:"

engine = create_engine(
    SQLALCHEMY_TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(scope="function")
def mock_redis():
    """Mock Redis client for testing - supports HASH and ZSET operations"""
    mock_redis_client = Mock()
    # Store HASH data: {key: {field: value}}
    mock_redis_client._hashes = {}
    # Store ZSET data: {key: {member: score}}
    mock_redis_client._zsets = {}
    # Store TTLs: {key: seconds}
    mock_redis_client._ttls = {}
    # Store simple integer counters (e.g. rate limiting): {key: count}
    mock_redis_client._counters = {}
    
    def mock_hset(key, *args, mapping=None, **kwargs):
        """
        Mock hset supporting multiple forms:
        - hset(key, mapping={...})
        - hset(key, field, value)
        - hset(key, **kwargs)
        """
        if key not in mock_redis_client._hashes:
            mock_redis_client._hashes[key] = {}
        
        # Handle hset(key, field, value) form (3 positional arguments)
        if len(args) == 2 and mapping is None and not kwargs:
            field, value = args
            mock_redis_client._hashes[key][field] = value
            return 1
        
        # Handle hset(key, mapping={...}) form
        if mapping:
            mock_redis_client._hashes[key].update(mapping)
        
        # Handle hset(key, **kwargs) form
        if kwargs:
            mock_redis_client._hashes[key].update(kwargs)
        
        return len(mock_redis_client._hashes[key])
    
    def mock_hgetall(key):
        return mock_redis_client._hashes.get(key, {})

    def mock_hsetnx(key, field, value):
        """Set field only if it does not already exist (atomic single-winner)."""
        if key not in mock_redis_client._hashes:
            mock_redis_client._hashes[key] = {}
        if field in mock_redis_client._hashes[key]:
            return 0
        mock_redis_client._hashes[key][field] = value
        return 1
    
    def mock_delete(*keys):
        count = 0
        for key in keys:
            if key in mock_redis_client._hashes:
                del mock_redis_client._hashes[key]
                count += 1
            if key in mock_redis_client._zsets:
                del mock_redis_client._zsets[key]
                count += 1
            if key in mock_redis_client._counters:
                del mock_redis_client._counters[key]
                count += 1
            if key in mock_redis_client._ttls:
                del mock_redis_client._ttls[key]
        return count
    
    def mock_expire(key, seconds):
        if (
            key in mock_redis_client._hashes
            or key in mock_redis_client._zsets
            or key in mock_redis_client._counters
        ):
            mock_redis_client._ttls[key] = seconds
            return True
        return False
    
    def mock_exists(key):
        return (
            key in mock_redis_client._hashes
            or key in mock_redis_client._zsets
            or key in mock_redis_client._counters
        )

    def mock_incr(key, amount=1):
        """Mock INCR: atomically increment (and implicitly create) an integer counter."""
        mock_redis_client._counters[key] = mock_redis_client._counters.get(key, 0) + amount
        return mock_redis_client._counters[key]
    
    def mock_scan_iter(match=None):
        """Mock scan_iter for iterating over keys"""
        keys = list(mock_redis_client._hashes.keys()) + list(mock_redis_client._zsets.keys())
        if match:
            import fnmatch
            keys = [k for k in keys if fnmatch.fnmatch(k, match)]
        return iter(keys)
    
    def mock_zadd(key, mapping):
        if key not in mock_redis_client._zsets:
            mock_redis_client._zsets[key] = {}
        mock_redis_client._zsets[key].update(mapping)
        return len(mapping)
    
    def mock_zrem(key, *members):
        if key not in mock_redis_client._zsets:
            return 0
        count = 0
        for member in members:
            if member in mock_redis_client._zsets[key]:
                del mock_redis_client._zsets[key][member]
                count += 1
        return count
    
    def mock_zrevrange(key, start, end):
        if key not in mock_redis_client._zsets:
            return []
        # Sort by score descending
        items = sorted(
            mock_redis_client._zsets[key].items(),
            key=lambda x: x[1],
            reverse=True
        )
        # Return just the members (not scores)
        members = [item[0] for item in items]
        # Handle slice
        if end == -1:
            return members[start:]
        return members[start:end+1]
    
    class MockPipeline:
        def __init__(self, redis_mock):
            self.redis_mock = redis_mock
            self._commands = []
        
        def hgetall(self, key):
            self._commands.append(('hgetall', key))
            return self  # Return self for chaining
        
        def execute(self):
            results = []
            for cmd, key in self._commands:
                if cmd == 'hgetall':
                    results.append(mock_hgetall(key))
            self._commands = []
            return results
    
    def mock_pipeline():
        return MockPipeline(mock_redis_client)
    
    def mock_ttl(key):
        if (
            key not in mock_redis_client._hashes
            and key not in mock_redis_client._zsets
            and key not in mock_redis_client._counters
        ):
            return -2
        return mock_redis_client._ttls.get(key, -1)

    mock_redis_client.hset = mock_hset
    mock_redis_client.hgetall = mock_hgetall
    mock_redis_client.hsetnx = mock_hsetnx
    mock_redis_client.delete = mock_delete
    mock_redis_client.expire = mock_expire
    mock_redis_client.ttl = mock_ttl
    mock_redis_client.exists = mock_exists
    mock_redis_client.incr = mock_incr
    mock_redis_client.scan_iter = mock_scan_iter
    mock_redis_client.zadd = mock_zadd
    mock_redis_client.zrem = mock_zrem
    mock_redis_client.zrevrange = mock_zrevrange
    mock_redis_client.pipeline = mock_pipeline
    
    return mock_redis_client


@pytest.fixture(scope="function")
def db_session():
    """Create a fresh database session for each test"""
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture(scope="function")
def client(db_session, mock_redis, monkeypatch):
    """Create a test client with database and Redis overrides"""
    # Skip migrations - we use create_all in db_session. Migrations use MySQL syntax that fails on SQLite.
    def noop_migrations():
        pass
    monkeypatch.setattr("app.main._run_migrations", noop_migrations)

    # Make app use our test engine so lifespan (seed_categories, etc.) sees the same DB with tables
    import app.core.database as db_module
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "SessionLocal", TestingSessionLocal)

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    # Override Redis client in all modules that import it
    import app.core.redis as redis_module
    import app.api.user as user_api
    import app.core.auth as auth_module
    import app.core.rate_limit as rate_limit_module
    import app.services.download_token_service as token_service_module
    import app.services.ipmi_ticket_service as ipmi_ticket_module
    import app.services.user_session_service as user_session_service_module
    import app.services.client_portal_service as client_portal_service_module
    import app.services.vm_vnc_ticket_service as vm_vnc_ticket_module
    import app.services.ipmi_kvm_ticket_service as ipmi_kvm_ticket_module

    monkeypatch.setattr(redis_module, "redis_client", mock_redis)
    monkeypatch.setattr(user_api, "redis_client", mock_redis)
    monkeypatch.setattr(auth_module, "redis_client", mock_redis)
    monkeypatch.setattr(rate_limit_module, "redis_client", mock_redis)
    monkeypatch.setattr(token_service_module, "redis_client", mock_redis)
    monkeypatch.setattr(ipmi_ticket_module, "redis_client", mock_redis)
    monkeypatch.setattr(user_session_service_module, "redis_client", mock_redis)
    monkeypatch.setattr(client_portal_service_module, "redis_client", mock_redis)
    monkeypatch.setattr(vm_vnc_ticket_module, "redis_client", mock_redis)
    monkeypatch.setattr(ipmi_kvm_ticket_module, "redis_client", mock_redis)

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as test_client:
        yield test_client
    
    app.dependency_overrides.clear()
    # Clear mock Redis storage between tests
    mock_redis._hashes.clear()
    mock_redis._zsets.clear()
    mock_redis._ttls.clear()
    mock_redis._counters.clear()


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

