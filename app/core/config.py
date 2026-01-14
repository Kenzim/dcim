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
    
    # API settings
    api_title: str = "DCIM Boot Controller API"
    api_version: str = "1.0.0"
    
    # Static files settings
    static_files_path: str = "./frontend"
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()

