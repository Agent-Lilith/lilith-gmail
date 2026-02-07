from typing import List, Optional

from cryptography.fernet import Fernet
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql+psycopg://lilith:lilith@localhost:5432/lilith_emails"
    EMBEDDING_URL: str = "http://127.0.0.1:6003"
    EMAIL_ENCRYPTION_KEY: str = ""
    GOOGLE_CLOUD_PROJECT: str = ""
    PUBSUB_TOPIC: str = "gmail-notifications"
    GMAIL_SCOPES: List[str] = [
        "https://www.googleapis.com/auth/gmail.readonly",
    ]
    VLLM_URL: str = "http://127.0.0.1:6001/v1"
    VLLM_MODEL: str = "Qwen3-8B-AWQ"
    SPACY_API_URL: str = "http://127.0.0.1:6004"
    FASTTEXT_LANGDETECT_URL: str = "http://127.0.0.1:6005"
    WEBHOOK_URL: str = "https://your-domain.com/webhook/gmail"
    MCP_EMAIL_ACCOUNT_ID: Optional[int] = None

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


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
