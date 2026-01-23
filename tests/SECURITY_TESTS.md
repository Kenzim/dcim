# Security Tests Documentation

This document describes the security test suite added to verify the security improvements implemented in the DCIM application.

## Test Files Created

### 1. `tests/services/test_download_token_service.py`

Tests for the download token service functionality:

- **Token Generation:**
  - `test_generate_token` - Verifies tokens are generated and stored correctly
  - `test_generate_token_with_patterns` - Tests token generation with file patterns

- **Token Validation:**
  - `test_validate_token_success` - Valid token validation
  - `test_validate_token_wrong_file` - Rejects access to unauthorized files
  - `test_validate_token_pattern_match` - Pattern matching for file access
  - `test_validate_token_no_restrictions` - Tokens without restrictions allow all files
  - `test_validate_token_expired` - Expired tokens are rejected
  - `test_validate_token_invalid` - Invalid tokens are rejected

- **Token Usage:**
  - `test_mark_token_used` - Tokens can be marked as used
  - `test_mark_token_used_twice` - Tokens can only be used once
  - `test_token_single_use_enforcement` - Single-use enforcement works correctly

- **Token Management:**
  - `test_revoke_token` - Individual token revocation
  - `test_revoke_tokens_for_boot_task` - Bulk token revocation for boot tasks

### 2. `tests/api/test_file_serving_security.py`

Tests for file-serving endpoint security:

- **Disk Image Endpoint:**
  - `test_disk_image_endpoint_requires_token` - Requires token for access
  - `test_disk_image_endpoint_with_invalid_token` - Rejects invalid tokens
  - `test_disk_image_endpoint_with_valid_token` - Accepts valid tokens
  - `test_disk_image_endpoint_token_single_use` - Tokens are single-use
  - `test_disk_image_endpoint_wrong_file` - Rejects access to unauthorized files

- **ISO Endpoint:**
  - `test_iso_endpoint_requires_token` - Requires token for access

- **Scripts Endpoint:**
  - `test_scripts_endpoint_optional_token` - Backward compatible (optional token)
  - `test_scripts_by_id_requires_auth` - Requires authentication
  - `test_scripts_by_id_with_token` - Accepts tokens for access

### 3. `tests/api/test_boot_task_token_injection.py`

Tests for token injection into boot tasks:

- **Token Injection:**
  - `test_boot_task_contains_token` - Verifies token is injected into scripts
  - `test_boot_task_token_generation` - Verifies token is generated when boot task is created
  - `test_boot_task_disk_image_url_contains_token` - Verifies DISK_IMAGE_URL contains token

### 4. `tests/api/test_installation_logs_security.py`

Tests for installation log upload security:

- **Log Upload Security:**
  - `test_installation_logs_accepts_token` - Accepts valid tokens
  - `test_installation_logs_rejects_invalid_token` - Rejects invalid tokens
  - `test_installation_logs_works_without_token` - Backward compatible (works without token)
  - `test_installation_logs_validates_boot_task_match` - Validates token matches boot task

## Running the Tests

Run all security tests:
```bash
pytest tests/services/test_download_token_service.py tests/api/test_file_serving_security.py tests/api/test_boot_task_token_injection.py tests/api/test_installation_logs_security.py -v
```

Run specific test file:
```bash
pytest tests/services/test_download_token_service.py -v
```

Run with coverage:
```bash
pytest tests/services/test_download_token_service.py --cov=app.services.download_token_service --cov-report=term-missing
```

## Test Coverage

The security tests cover:

1. ✅ Token generation and storage
2. ✅ Token validation (valid, invalid, expired)
3. ✅ File access restrictions (specific files, patterns)
4. ✅ Single-use token enforcement
5. ✅ Token expiration
6. ✅ Token revocation
7. ✅ File-serving endpoint authentication
8. ✅ Script access security
9. ✅ Log upload security
10. ✅ Token injection into boot tasks

## Mock Redis

All tests use a mock Redis client (`mock_redis` fixture) that:
- Stores HASH data in memory
- Supports `hset`, `hgetall`, `delete`, `expire`
- Supports `scan_iter` for key iteration
- Supports `exists` for key existence checks

## Test Fixtures

- `mock_redis` - Mock Redis client for all tests
- `token_service` - Download token service with mocked Redis
- `client` - FastAPI test client with database and Redis overrides
- `test_file` - Temporary test file for file-serving tests
- `admin_token` - Admin authentication token for protected endpoints

## Notes

- Some tests may need adjustment based on actual endpoint implementation
- File path patching may be needed for file-serving tests
- Template setup may be required for boot task token injection tests
- Tests are designed to be independent and can run in any order

## Future Enhancements

Potential additional tests to add:

1. Rate limiting tests
2. Token rotation tests
3. Concurrent token usage tests
4. Token expiration edge cases
5. Network-level security tests (firewall/VLAN)
6. HTTPS enforcement tests
7. Audit logging tests
