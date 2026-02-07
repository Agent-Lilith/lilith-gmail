import logging
import re
from typing import List, Optional

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

REDACT_LABELS = {"PERSON", "GPE", "LOC", "FAC", "ORG"}

# Keys, tokens, SSH blocks, API secrets; order matters (more specific first)
SENSITIVE_PATTERNS: List[tuple[re.Pattern, str]] = [
    (re.compile(r"-----BEGIN (?:OPENSSH |RSA |DSA |EC |)PRIVATE KEY-----[\s\S]*?-----END (?:OPENSSH |RSA |DSA |EC |)PRIVATE KEY-----", re.IGNORECASE), "[REDACTED]"),
    (re.compile(r"Bearer\s+[A-Za-z0-9\-_.~+/]+=*", re.IGNORECASE), "[REDACTED]"),
    (re.compile(r"access_token[\s=:]+[\w\-.]+\.[\w\-.]+\.[\w\-]+", re.IGNORECASE), "access_token=[REDACTED]"),
    (re.compile(r"(?:api[_-]?key|apikey|api_secret|secret_key|auth[_-]?token)[\s=:]+[\w\-~./+=]+", re.IGNORECASE), "[REDACTED]"),
    (re.compile(r"(?:password|passwd|pwd|token)[\s=:]+[\S]+", re.IGNORECASE), "[REDACTED]"),
    (re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}(?:-[A-Z0-9]{4})*\b", re.IGNORECASE), "[REDACTED]"),
    (re.compile(r"\b[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}(?:-[A-Z0-9]{5})*\b", re.IGNORECASE), "[REDACTED]"),
    (re.compile(r"\b[A-Fa-f0-9]{32,}\b"), "[REDACTED]"),
    (re.compile(r"\b[A-Za-z0-9+/]{20,}={0,2}\b"), "[REDACTED]"),
    (re.compile(r"(?:license\s+key|product\s+key|serial\s+number|activation\s+key)[\s:]+[\w\-]+", re.IGNORECASE), "[REDACTED]"),
]


def _normalize_entity(e: dict) -> dict | None:
    start = e.get("start") if "start" in e else e.get("start_char") or e.get("first_index")
    end = e.get("end") if "end" in e else e.get("end_char") or e.get("last_index")
    label = e.get("label") or e.get("entity") or e.get("name") or e.get("type")
    if start is None or end is None or label is None:
        return None
    return {"start": int(start), "end": int(end), "label": str(label).upper()}


SPACY_NER_ENDPOINT = "/ner"


def get_entities(text: str, lang: str = "en") -> List[dict]:
    url = (settings.SPACY_API_URL or "").rstrip("/")
    if not url:
        return []
    payload = {"text": text, "lang": (lang or "en")[:10]}
    with httpx.Client(timeout=15.0) as client:
        r = client.post(f"{url}{SPACY_NER_ENDPOINT}", json=payload)
        r.raise_for_status()
        data = r.json()
    raw: list = []
    if isinstance(data, list):
        raw = [e for e in data if isinstance(e, dict)]
    elif isinstance(data, dict):
        raw = (
            data.get("entities")
            or data.get("extractions")
            or data.get("ents")
            or []
        )
    out = []
    for e in raw:
        n = _normalize_entity(e)
        if n:
            out.append(n)
    return out


def redact_sensitive_patterns(text: str) -> str:
    if not text:
        return ""
    out = text
    for pattern, replacement in SENSITIVE_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def sanitize_with_spacy_api(body: str, lang: str = "en") -> str:
    if not body:
        return ""
    sanitized = body
    # PII regexes: email, phone, card, SSN, 9-digit ID
    sanitized = re.sub(r"[\w.\-]+@[\w.\-]+\.\w+", "[EMAIL]", sanitized)
    sanitized = re.sub(r"\+?\d[\d \-]{8,}\d", "[PHONE]", sanitized)
    sanitized = re.sub(
        r"\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b", "[CARD]", sanitized
    )
    sanitized = re.sub(r"\b\d{3}-\d{2}-\d{4}\b", "[SSN]", sanitized)
    sanitized = re.sub(r"\b\d{9}\b", "[ID]", sanitized)

    entities = get_entities(sanitized, lang=lang or "en")
    by_start = sorted(
        (e for e in entities if e.get("label") in REDACT_LABELS),
        key=lambda x: x.get("start", 0),
        reverse=True,
    )
    for e in by_start:
        start = e.get("start")
        end = e.get("end")
        label = e.get("label") or "ENTITY"
        if start is None or end is None or start < 0 or end > len(sanitized):
            continue
        sanitized = sanitized[:start] + f"[{label}]" + sanitized[end:]
    return sanitized


def full_redact_for_display(body: str, lang: str = "en") -> str:
    if not body:
        return ""
    step = sanitize_with_spacy_api(body, lang=lang or "en")
    return redact_sensitive_patterns(step)
