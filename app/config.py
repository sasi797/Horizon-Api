from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str

    # Redis
    redis_url: str = "redis://:bts_redis_pass@localhost:6379/0"

    # JWT
    secret_key: str = "change-me-in-production-super-secret-key-32chars"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

    # CORS
    cors_origins: str = "http://localhost:3000,http://localhost:3001"

    # Email — reads IMAP_USER / IMAP_PASSWORD from .env (case-insensitive match)
    imap_user: str = ""
    imap_password: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_from_name: str = "BTS Support"

    # S3 Storage
    aws_region: str = "ap-south-1"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    s3_bucket: str = ""
    s3_prefix: str = "BTSEmailAttachments"

    # Anthropic / PDF extraction
    anthropic_api_key: str = ""

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",")]


settings = Settings()
