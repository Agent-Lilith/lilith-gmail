import logging

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

DETECT_CONFIDENCE_THRESHOLD = 0.5


def detect_language(text: str) -> str:
    url = (settings.FASTTEXT_LANGDETECT_URL or "").rstrip("/")
    if not url:
        raise RuntimeError(
            "FASTTEXT_LANGDETECT_URL is not set. Language detection requires the fastText service."
        )
    if not (text or "").strip():
        return "en"
    payload = {"text": text.strip(), "k": 1}
    with httpx.Client(timeout=10.0) as client:
        r = client.post(f"{url}/detect", json=payload)
        r.raise_for_status()
        data = r.json()
    predictions = data.get("predictions") or []
    if not predictions:
        return "en"
    first = predictions[0] if isinstance(predictions[0], dict) else {}
    lang = (first.get("language") or "").strip()
    confidence = first.get("confidence")
    if isinstance(confidence, (int, float)) and confidence < DETECT_CONFIDENCE_THRESHOLD:
        return "en"
    if lang and len(lang) >= 2:
        base = lang.split("_")[0].lower()[:2]
        if base.isalpha():
            return base
    return "en"
