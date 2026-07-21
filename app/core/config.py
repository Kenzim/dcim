from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # Database settings
    database_url: str
    
    # Redis settings
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: Optional[str] = None
    
    # Auth settings
    auth_token_expire_seconds: int = 864000  # 10 days

    # Trust the X-Forwarded-For header when determining the caller's source IP.
    # This must ONLY be enabled when the app sits behind a trusted reverse proxy
    # that sets/overwrites the header; otherwise clients can spoof their identity
    # (e.g. to fetch another server's cloud-init credentials).
    trust_x_forwarded_for: bool = False
    
    # Initial admin (created only when no users exist in DB)
    initial_admin_username: Optional[str] = None
    initial_admin_password: Optional[str] = None
    initial_admin_email: Optional[str] = None
    
    # API settings
    api_title: str = "Rackflow API"
    api_version: str = "1.0.0"
    
    # Static files settings
    static_files_path: str = "./frontend"

    # Optional runner URLs (separate containers). When set, app calls these APIs instead of running subprocesses.
    dhcp_runner_url: Optional[str] = None
    tftp_runner_url: Optional[str] = None
    # Legacy: single URL for combined dhcp+tftp runner (paths /dhcp/* and /tftp/*)
    dhcp_tftp_service_url: Optional[str] = None
    
    # For per-location service instances: encrypt stored API keys so backend can call runners.
    # Must be a base64-encoded 32-byte key. Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    service_instance_encryption_key: Optional[str] = None
    # When True, creating/updating a service instance API key without a configured
    # encryption key is rejected (recommended for production). Left False so dev/tests
    # can run without a key; compose sets the key so this can be safely enabled.
    require_service_instance_encryption: bool = False

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()

