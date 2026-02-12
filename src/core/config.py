from common.config import BaseAgentSettings
from cryptography.fernet import Fernet


class Settings(BaseAgentSettings):
    EMAIL_ENCRYPTION_KEY: str = ""
    GOOGLE_CLOUD_PROJECT: str = ""
    PUBSUB_TOPIC: str = ""
    PUBSUB_SUBSCRIPTION: str = ""
    GMAIL_SCOPES: list[str] = [
        "https://www.googleapis.com/auth/gmail.readonly",
    ]
    WEBHOOK_URL: str = ""
    MCP_EMAIL_ACCOUNT_ID: int | None = None


settings = Settings()


def encrypt_token(token: str) -> bytes:
    if not settings.EMAIL_ENCRYPTION_KEY:
        raise ValueError("EMAIL_ENCRYPTION_KEY not set")
    f = Fernet(settings.EMAIL_ENCRYPTION_KEY.encode())
    return f.encrypt(token.encode())


def decrypt_token(encrypted: bytes) -> str:
    if not settings.EMAIL_ENCRYPTION_KEY:
        raise ValueError("EMAIL_ENCRYPTION_KEY not set")
    f = Fernet(settings.EMAIL_ENCRYPTION_KEY.encode())
    return f.decrypt(encrypted).decode()
