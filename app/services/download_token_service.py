"""
Service for managing one-time download tokens for file serving endpoints.

These tokens are used to secure file downloads (install.wim, ISOs, etc.) during
OS installation. Each token is single-use and expires after a set time.
"""
import secrets
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any
from app.core.redis import redis_client
import logging

logger = logging.getLogger(__name__)

# Token expiration time (15 minutes for image downloads)
TOKEN_EXPIRATION_SECONDS = 900

# Token key prefix in Redis
TOKEN_KEY_PREFIX = "download_token:"


def _derive_token_id(token: str) -> str:
    """Derive token_id from token using SHA256"""
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


class DownloadTokenService:
    """Service for managing one-time download tokens"""
    
    @staticmethod
    def generate_token(
        boot_task_id: int,
        allowed_files: Optional[List[str]] = None,
        allowed_patterns: Optional[List[str]] = None,
        expires_in: int = TOKEN_EXPIRATION_SECONDS,
        single_use: bool = True,
    ) -> str:
        """
        Generate a download token.
        
        Args:
            boot_task_id: ID of the boot task this token is for
            allowed_files: List of specific filenames this token can access (e.g., ["install.wim"])
            allowed_patterns: List of filename patterns this token can access (e.g., ["*.iso", "*.wim"])
            expires_in: Token expiration time in seconds (default: 24 hours)
            single_use: If True, token is invalidated after first use. If False, valid until expiry (e.g. for ISO boot where iPXE requests the same URL multiple times).
        
        Returns:
            The generated token string
        """
        # Generate random token
        token = secrets.token_urlsafe(32)
        token_id = _derive_token_id(token)
        token_key = f"{TOKEN_KEY_PREFIX}{token_id}"
        
        # Store token metadata in Redis
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=expires_in)
        
        token_data = {
            "boot_task_id": str(boot_task_id),
            "created_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "used": "false",
            "single_use": "true" if single_use else "false",
            "allowed_files": json.dumps(allowed_files or []),
            "allowed_patterns": json.dumps(allowed_patterns or []),
        }
        
        redis_client.hset(token_key, mapping=token_data)
        redis_client.expire(token_key, expires_in)
        
        logger.info(f"Generated download token for boot_task {boot_task_id} (expires in {expires_in}s)")
        
        return token
    
    @staticmethod
    def validate_token(token: str, filename: str) -> Optional[Dict[str, Any]]:
        """
        Validate a token and check if it can access the requested file.
        
        Args:
            token: The token to validate
            filename: The filename being requested
        
        Returns:
            Token metadata dict if valid, None if invalid
        """
        token_id = _derive_token_id(token)
        token_key = f"{TOKEN_KEY_PREFIX}{token_id}"
        
        # Get token data from Redis
        token_data = redis_client.hgetall(token_key)
        
        if not token_data:
            logger.warning(f"Download token not found or expired: {token_id[:8]}...")
            return None
        
        # Check if already used
        if token_data.get("used", "false").lower() == "true":
            logger.warning(f"Download token already used: {token_id[:8]}...")
            return None
        
        # Check expiration
        expires_at_str = token_data.get("expires_at")
        if expires_at_str:
            expires_at = datetime.fromisoformat(expires_at_str)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > expires_at:
                logger.warning(f"Download token expired: {token_id[:8]}...")
                redis_client.delete(token_key)  # Clean up expired token
                return None
        
        # Check if filename is allowed
        allowed_files_json = token_data.get("allowed_files", "[]")
        allowed_patterns_json = token_data.get("allowed_patterns", "[]")
        
        try:
            allowed_files = json.loads(allowed_files_json)
            allowed_patterns = json.loads(allowed_patterns_json)
        except json.JSONDecodeError:
            logger.error(f"Invalid token data format for {token_id[:8]}...")
            return None
        
        single_use = str(token_data.get("single_use", "true")).lower() == "true"
        result = {
            "boot_task_id": int(token_data.get("boot_task_id", 0)),
            "created_at": token_data.get("created_at"),
            "expires_at": token_data.get("expires_at"),
            "single_use": single_use,
        }
        # If no restrictions, allow all files
        if not allowed_files and not allowed_patterns:
            return result
        # Check specific files
        if allowed_files and filename in allowed_files:
            return result
        # Check patterns
        if allowed_patterns:
            import fnmatch
            for pattern in allowed_patterns:
                if fnmatch.fnmatch(filename, pattern):
                    return result
        
        # File not allowed
        logger.warning(f"Download token does not allow access to '{filename}': {token_id[:8]}...")
        return None
    
    @staticmethod
    def mark_token_used(token: str) -> bool:
        """
        Mark a token as used (one-time use).
        
        Args:
            token: The token to mark as used
        
        Returns:
            True if token was marked as used, False if token not found
        """
        token_id = _derive_token_id(token)
        token_key = f"{TOKEN_KEY_PREFIX}{token_id}"
        
        # Check if token exists
        if not redis_client.exists(token_key):
            return False
        
        # Mark as used
        redis_client.hset(token_key, "used", "true")
        logger.info(f"Marked download token as used: {token_id[:8]}...")
        
        return True
    
    @staticmethod
    def revoke_token(token: str) -> bool:
        """
        Revoke a token (delete it).
        
        Args:
            token: The token to revoke
        
        Returns:
            True if token was revoked, False if token not found
        """
        token_id = _derive_token_id(token)
        token_key = f"{TOKEN_KEY_PREFIX}{token_id}"
        
        deleted = redis_client.delete(token_key)
        if deleted:
            logger.info(f"Revoked download token: {token_id[:8]}...")
        
        return deleted > 0
    
    @staticmethod
    def revoke_tokens_for_boot_task(boot_task_id: int) -> int:
        """
        Revoke all tokens for a specific boot task.
        
        Args:
            boot_task_id: The boot task ID
        
        Returns:
            Number of tokens revoked
        """
        # Scan for tokens with matching boot_task_id
        # Note: This is not efficient for large numbers of tokens, but should be fine for normal use
        pattern = f"{TOKEN_KEY_PREFIX}*"
        revoked_count = 0
        
        for key in redis_client.scan_iter(match=pattern):
            token_data = redis_client.hgetall(key)
            if token_data.get("boot_task_id") == str(boot_task_id):
                redis_client.delete(key)
                revoked_count += 1
        
        if revoked_count > 0:
            logger.info(f"Revoked {revoked_count} download token(s) for boot_task {boot_task_id}")
        
        return revoked_count


# Singleton instance
_download_token_service = None


def get_download_token_service() -> DownloadTokenService:
    """Get the singleton DownloadTokenService instance"""
    global _download_token_service
    if _download_token_service is None:
        _download_token_service = DownloadTokenService()
    return _download_token_service
