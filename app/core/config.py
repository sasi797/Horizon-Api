from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # App
    APP_NAME: str = "BTS API"
    DEBUG: bool = False

    # Database
    DATABASE_URL: str

    # Redis / Celery
    REDIS_URL: str = "redis://localhost:6379/0"

    # JWT
    SECRET_KEY: str = "change-this-secret"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Email (Microsoft Graph API polling)
    MAILBOX_EMAIL: str = ""         # Outlook mailbox to poll, e.g. bookings@company.com
    EMAIL_POLL_INTERVAL_SECONDS: int = 30
    # Only process emails from this sender (leave empty to allow all)
    ALLOWED_SENDER: str = ""
    # Only process emails received after this datetime (ISO format: 2026-05-25T15:30:00+05:30)
    PROCESS_EMAILS_SINCE: str = ""

    # Azure AD (client credentials — needs Mail.Read + Mail.ReadWrite Application permissions)
    AZURE_CLIENT_ID: str = ""
    AZURE_TENANT_ID: str = ""
    AZURE_CLIENT_SECRET: str = ""

    # Transport API
    TRANSPORT_API_URL: str = "https://transport.example.com/api"
    TRANSPORT_API_KEY: str = ""
    TRANSPORT_MAX_RETRIES: int = 3

    # S3 Storage
    AWS_REGION: str = "ap-south-1"
    S3_BUCKET: str = ""
    S3_PREFIX: str = "BTSEmailAttachments"

    # Microsoft Graph Webhook (change notifications)
    # Set to your public HTTPS backend URL, e.g. https://api.yourcompany.com
    WEBHOOK_BASE_URL: str = ""
    # Random secret string — Graph echoes it back in every notification so we can verify it's genuine
    GRAPH_WEBHOOK_SECRET: str = "bts-webhook-secret-change-me"

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
