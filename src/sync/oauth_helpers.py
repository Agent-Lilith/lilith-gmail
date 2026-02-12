import json
import logging
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from core.config import decrypt_token, encrypt_token, settings

logger = logging.getLogger(__name__)

SCOPES = settings.GMAIL_SCOPES


def credentials_from_token(encrypted: bytes) -> Credentials:
    token_json = decrypt_token(encrypted)
    data = json.loads(token_json)
    return Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=data.get("client_id"),
        client_secret=data.get("client_secret"),
        scopes=data.get("scopes", SCOPES),
    )


def token_from_credentials(creds: Credentials) -> bytes:
    data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or SCOPES),
    }
    return encrypt_token(json.dumps(data))


def run_local_oauth(
    client_secrets_path: str,
    token_path: str | None = None,
    prompt_consent: bool = True,
) -> Credentials:
    flow = InstalledAppFlow.from_client_secrets_file(client_secrets_path, SCOPES)
    kwargs: dict[str, Any] = {"port": 0}
    if prompt_consent:
        kwargs["prompt"] = "consent"
    creds = flow.run_local_server(**kwargs)
    if token_path:
        Path(token_path).parent.mkdir(parents=True, exist_ok=True)
        encrypted = token_from_credentials(creds)
        Path(token_path).write_bytes(encrypted)
        logger.info(f"Saved credentials to {token_path}")
    return creds


def ensure_valid_credentials(creds: Credentials) -> Credentials:
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds
