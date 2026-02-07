from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from core.models import Email
from core.privacy import PrivacyTier


BODY_SENSITIVE_PLACEHOLDER = "[SENSITIVE CONTENT REDACTED]"
BODY_REDACTED_FALLBACK = "[REDACTED CONTENT]"
SNIPPET_NOT_PROCESSED = ""


class ExternalEmail(BaseModel):
    id: str = Field(description="Gmail message ID")
    thread_id: str = Field(description="Gmail thread ID")
    subject: Optional[str] = None
    from_: str = Field(alias="from", description="From header (name <email>)")
    to: List[str] = Field(default_factory=list)
    date: Optional[str] = Field(default=None, description="ISO format sent_at")
    snippet: str = Field(default="", description="Redacted or placeholder snippet only")
    body: str = Field(default="", description="Redacted or placeholder body only")
    labels: List[str] = Field(default_factory=list)
    has_attachments: bool = False

    model_config = {"populate_by_name": True}


def to_external_email(
    email: Email,
    privacy_mode: str = "external",
    *,
    label_id_to_name: Optional[Dict[str, str]] = None,
) -> ExternalEmail:
    if privacy_mode == "external" and email.privacy_tier == PrivacyTier.SENSITIVE:
        body = BODY_SENSITIVE_PLACEHOLDER
    else:
        body = email.body_redacted or BODY_REDACTED_FALLBACK
    snippet = email.snippet_redacted if email.snippet_redacted is not None else SNIPPET_NOT_PROCESSED
    label_ids = list(email.labels or [])
    if label_id_to_name:
        labels_display = [label_id_to_name.get(lid, lid) for lid in label_ids]
    else:
        labels_display = label_ids
    from_str = (
        f"{email.from_name} <{email.from_email}>"
        if (email.from_name and email.from_name.strip())
        else (email.from_email or "")
    )
    date_str = email.sent_at.isoformat() if email.sent_at else None

    return ExternalEmail(
        id=email.gmail_id,
        thread_id=email.gmail_thread_id,
        subject=email.subject,
        from_=from_str,
        to=list(email.to_emails or []),
        date=date_str,
        snippet=snippet or "",
        body=body or "",
        labels=labels_display,
        has_attachments=email.has_attachments or False,
    )


def external_email_to_dict(external: ExternalEmail) -> Dict[str, Any]:
    return external.model_dump(by_alias=True)
