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

    # Indigo (NPA) AddJob integration — see docs/indigo-addjob-integration.md
    # in Horizon-Web. Each customer account is a separate NPA instance with its
    # own login, so the account number picked on the manifest decides where the
    # export is booked and as whom. Keyed by the 'account_number' dropdown value
    # exactly as it is stored on the manifest. Override the whole map in .env
    # with a JSON object under the same name.
    INDIGO_ACCOUNTS: dict[str, dict[str, str]] = {
        "SPL001": {
            "base_url": "https://apps.neilporterassociates.co.uk/iWebService/V1",
            "username": "SPLTEST",
            "password": "SPLTest123!",
        },
        "S1102": {
            "base_url": "https://horizonexpress.neilporterassociates.co.uk/iWebService/V1",
            "username": "SPL",
            "password": "TtOmyxHE",
        },
    }

    # Nexus has no API for us — employees are created by driving its New
    # Employee form in a headless browser (app/services/nexus_sync.py). These
    # are the login credentials that automation signs in with; set in .env.
    NEXUS_BASE_URL: str = "https://nexus.linkworks.in"
    NEXUS_USERNAME: str = ""
    NEXUS_PASSWORD: str = ""

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
