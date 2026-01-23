"""
Tests for download token service security
"""
import pytest
from datetime import datetime, timedelta
import json
from app.services.download_token_service import DownloadTokenService, get_download_token_service


@pytest.fixture
def token_service(mock_redis, monkeypatch):
    """Get download token service with mocked Redis"""
    import app.services.download_token_service as token_service_module
    monkeypatch.setattr(token_service_module, "redis_client", mock_redis)
    return get_download_token_service()


def test_generate_token(token_service, mock_redis):
    """Test token generation"""
    boot_task_id = 123
    token = token_service.generate_token(
        boot_task_id=boot_task_id,
        allowed_files=["install.wim", "setup.iso"]
    )
    
    # Token should be generated
    assert token is not None
    assert len(token) > 0
    
    # Check token is stored in Redis
    from app.services.download_token_service import _derive_token_id
    token_id = _derive_token_id(token)
    token_key = f"download_token:{token_id}"
    
    token_data = mock_redis.hgetall(token_key)
    assert token_data is not None
    assert token_data.get("boot_task_id") == str(boot_task_id)
    assert token_data.get("used") == "false"
    
    # Check allowed files
    allowed_files = json.loads(token_data.get("allowed_files", "[]"))
    assert "install.wim" in allowed_files
    assert "setup.iso" in allowed_files


def test_generate_token_with_patterns(token_service, mock_redis):
    """Test token generation with file patterns"""
    boot_task_id = 456
    token = token_service.generate_token(
        boot_task_id=boot_task_id,
        allowed_patterns=["*.wim", "*.iso"]
    )
    
    assert token is not None
    
    from app.services.download_token_service import _derive_token_id
    token_id = _derive_token_id(token)
    token_key = f"download_token:{token_id}"
    
    token_data = mock_redis.hgetall(token_key)
    allowed_patterns = json.loads(token_data.get("allowed_patterns", "[]"))
    assert "*.wim" in allowed_patterns
    assert "*.iso" in allowed_patterns


def test_validate_token_success(token_service, mock_redis):
    """Test successful token validation"""
    boot_task_id = 789
    token = token_service.generate_token(
        boot_task_id=boot_task_id,
        allowed_files=["install.wim"]
    )
    
    # Validate token
    token_data = token_service.validate_token(token, "install.wim")
    assert token_data is not None
    assert token_data.get("boot_task_id") == boot_task_id


def test_validate_token_wrong_file(token_service, mock_redis):
    """Test token validation fails for wrong file"""
    boot_task_id = 101
    token = token_service.generate_token(
        boot_task_id=boot_task_id,
        allowed_files=["install.wim"]
    )
    
    # Try to access different file
    token_data = token_service.validate_token(token, "unauthorized.iso")
    assert token_data is None


def test_validate_token_pattern_match(token_service, mock_redis):
    """Test token validation with pattern matching"""
    boot_task_id = 202
    token = token_service.generate_token(
        boot_task_id=boot_task_id,
        allowed_patterns=["*.wim"]
    )
    
    # Should match pattern
    token_data = token_service.validate_token(token, "install.wim")
    assert token_data is not None
    
    # Should match pattern
    token_data = token_service.validate_token(token, "setup.wim")
    assert token_data is not None
    
    # Should not match
    token_data = token_service.validate_token(token, "install.iso")
    assert token_data is None


def test_validate_token_no_restrictions(token_service, mock_redis):
    """Test token with no file restrictions allows all files"""
    boot_task_id = 303
    token = token_service.generate_token(
        boot_task_id=boot_task_id
    )
    
    # Should allow any file
    token_data = token_service.validate_token(token, "anyfile.wim")
    assert token_data is not None
    
    token_data = token_service.validate_token(token, "anyfile.iso")
    assert token_data is not None


def test_validate_token_expired(token_service, mock_redis):
    """Test token validation fails for expired token"""
    boot_task_id = 404
    token = token_service.generate_token(
        boot_task_id=boot_task_id,
        expires_in=1  # 1 second expiration
    )
    
    # Manually expire the token
    from app.services.download_token_service import _derive_token_id
    token_id = _derive_token_id(token)
    token_key = f"download_token:{token_id}"
    
    # Set expiration to past using mapping form
    expired_time = (datetime.utcnow() - timedelta(seconds=10)).isoformat()
    mock_redis.hset(token_key, mapping={"expires_at": expired_time})
    
    # Validation should fail
    token_data = token_service.validate_token(token, "install.wim")
    assert token_data is None


def test_validate_token_invalid(token_service, mock_redis):
    """Test token validation fails for invalid token"""
    invalid_token = "invalid_token_12345"
    token_data = token_service.validate_token(invalid_token, "install.wim")
    assert token_data is None


def test_mark_token_used(token_service, mock_redis):
    """Test marking token as used"""
    boot_task_id = 505
    token = token_service.generate_token(
        boot_task_id=boot_task_id,
        allowed_files=["install.wim"]
    )
    
    # Mark as used
    result = token_service.mark_token_used(token)
    assert result is True
    
    # Validate should fail (already used)
    token_data = token_service.validate_token(token, "install.wim")
    assert token_data is None


def test_mark_token_used_twice(token_service, mock_redis):
    """Test that using a token twice fails"""
    boot_task_id = 606
    token = token_service.generate_token(
        boot_task_id=boot_task_id,
        allowed_files=["install.wim"]
    )
    
    # First use - should succeed
    token_data = token_service.validate_token(token, "install.wim")
    assert token_data is not None
    
    token_service.mark_token_used(token)
    
    # Second use - should fail
    token_data = token_service.validate_token(token, "install.wim")
    assert token_data is None


def test_revoke_token(token_service, mock_redis):
    """Test token revocation"""
    boot_task_id = 707
    token = token_service.generate_token(
        boot_task_id=boot_task_id,
        allowed_files=["install.wim"]
    )
    
    # Revoke token
    result = token_service.revoke_token(token)
    assert result is True
    
    # Validation should fail
    token_data = token_service.validate_token(token, "install.wim")
    assert token_data is None


def test_revoke_tokens_for_boot_task(token_service, mock_redis):
    """Test revoking all tokens for a boot task"""
    boot_task_id = 808
    
    # Generate multiple tokens
    token1 = token_service.generate_token(boot_task_id=boot_task_id)
    token2 = token_service.generate_token(boot_task_id=boot_task_id)
    token3 = token_service.generate_token(boot_task_id=999)  # Different boot task
    
    # Revoke all tokens for boot_task_id
    revoked_count = token_service.revoke_tokens_for_boot_task(boot_task_id)
    assert revoked_count == 2
    
    # First two tokens should be invalid
    assert token_service.validate_token(token1, "anyfile") is None
    assert token_service.validate_token(token2, "anyfile") is None
    
    # Third token should still be valid
    assert token_service.validate_token(token3, "anyfile") is not None


def test_token_single_use_enforcement(token_service, mock_redis):
    """Test that tokens can only be used once"""
    boot_task_id = 909
    token = token_service.generate_token(
        boot_task_id=boot_task_id,
        allowed_files=["install.wim"]
    )
    
    # First validation and use
    token_data = token_service.validate_token(token, "install.wim")
    assert token_data is not None
    token_service.mark_token_used(token)
    
    # Second attempt should fail
    token_data = token_service.validate_token(token, "install.wim")
    assert token_data is None
