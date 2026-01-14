"""
Pytest configuration and shared fixtures
"""
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
    
    def mock_hset(key, mapping=None, **kwargs):
        if key not in mock_redis_client._hashes:
            mock_redis_client._hashes[key] = {}
        if mapping:
            mock_redis_client._hashes[key].update(mapping)
        if kwargs:
            mock_redis_client._hashes[key].update(kwargs)
        return len(mock_redis_client._hashes[key])
    
    def mock_hgetall(key):
        return mock_redis_client._hashes.get(key, {})
    
    def mock_delete(*keys):
        count = 0
        for key in keys:
            if key in mock_redis_client._hashes:
                del mock_redis_client._hashes[key]
                count += 1
            if key in mock_redis_client._zsets:
                del mock_redis_client._zsets[key]
                count += 1
            if key in mock_redis_client._ttls:
                del mock_redis_client._ttls[key]
        return count
    
    def mock_expire(key, seconds):
        if key in mock_redis_client._hashes or key in mock_redis_client._zsets:
            mock_redis_client._ttls[key] = seconds
            return True
        return False
    
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
    
    mock_redis_client.hset = mock_hset
    mock_redis_client.hgetall = mock_hgetall
    mock_redis_client.delete = mock_delete
    mock_redis_client.expire = mock_expire
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
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    # Override Redis client in all modules that import it
    import app.core.redis as redis_module
    import app.api.user as user_api
    import app.core.auth as auth_module
    
    monkeypatch.setattr(redis_module, "redis_client", mock_redis)
    monkeypatch.setattr(user_api, "redis_client", mock_redis)
    monkeypatch.setattr(auth_module, "redis_client", mock_redis)
    
    app.dependency_overrides[get_db] = override_get_db
    
    with TestClient(app) as test_client:
        yield test_client
    
    app.dependency_overrides.clear()
    # Clear mock Redis storage
    mock_redis._storage.clear()


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

