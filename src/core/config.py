from common.config import BaseAgentSettings
from typing import List, Optional

class Settings(BaseAgentSettings):
    EMAIL_ENCRYPTION_KEY: str = ""
    GOOGLE_CLOUD_PROJECT: str = ""
    PUBSUB_TOPIC: str = ""
    GMAIL_SCOPES: List[str] = [
        "https://www.googleapis.com/auth/gmail.readonly",
    ]
    SPACY_API_URL: str = ""
    FASTTEXT_LANGDETECT_URL: str = ""
    WEBHOOK_URL: str = ""
    MCP_EMAIL_ACCOUNT_ID: Optional[int] = None

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
