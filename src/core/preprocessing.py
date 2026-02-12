import logging
import re
import unicodedata

from core.config import settings

logger = logging.getLogger(__name__)

# URL path segments that often indicate tracking pixels / click redirects
TRACKING_URL_KEYWORDS = (
    r"track(?:ing)?|open(?:ed)?|pixel|beacon|unsub(?:scribe)?|"
    r"redirect|click|mail(?:track|open)|read.?receipt|"
    r"analytics|trace|log\.(?:open|click)|notify\.(?:open|click)"
)
TRACKING_URL_REGEX = re.compile(
    r"https?://[^\s<>\"']*(?:" + TRACKING_URL_KEYWORDS + r")[^\s<>\"']*",
    re.IGNORECASE,
)

# HTML: strip tracking pixels (1x1/small img or img with tracking src) and script/iframe/object/embed
_IMG_TAG = re.compile(
    r"<img\s[^>]*>",
    re.IGNORECASE | re.DOTALL,
)
_IMG_1X1_OR_SMALL = re.compile(
    r"\b(?:width|height)\s*=\s*[\"']?1[\"']?|\b(?:width|height)\s*:\s*1px",
    re.IGNORECASE,
)
_IMG_TRACKING_SRC = re.compile(
    r"\bsrc\s*=\s*[\"']?[^\"'\s]*(?:" + TRACKING_URL_KEYWORDS + r")[^\"'\s]*[\"']?",
    re.IGNORECASE,
)
_SCRIPT_LIKE = re.compile(
    r"</?(?:script|iframe|object|embed)\b[^>]*>",
    re.IGNORECASE,
)


def strip_tracking_pixels_from_html(html: str) -> str:
    if not html or not html.strip():
        return html
    text = html

    text = _SCRIPT_LIKE.sub("", text)

    def is_tracking_img(match: re.Match) -> bool:
        tag = match.group(0)
        if _IMG_1X1_OR_SMALL.search(tag):
            return True
        if _IMG_TRACKING_SRC.search(tag):
            return True
        return False

    text = _IMG_TAG.sub(lambda m: "" if is_tracking_img(m) else m.group(0), text)
    return text


def strip_tracking_urls_from_text(text: str) -> str:
    if not text or not text.strip():
        return text
    return TRACKING_URL_REGEX.sub("[LINK]", text)


# Format, Control, Private Use, Not Assigned (strip fingerprinting / noise)
INVISIBLE_UNICODE_CATEGORIES = {"Cf", "Cc", "Co", "Cn"}
ZERO_WIDTH_CHARS = "\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2060\u2061\u2062\u2063\ufeff"


def strip_invisible_unicode(text: str) -> str:
    if not text:
        return text
    for c in ZERO_WIDTH_CHARS:
        text = text.replace(c, "")
    result = []
    for c in text:
        if c in " \t\n\r":
            result.append(c)
            continue
        cat = unicodedata.category(c)
        if cat in INVISIBLE_UNICODE_CATEGORIES:
            continue
        result.append(c)
    return "".join(result)


# Signature / disclaimer delimiters (e.g. "-- ", mobile "Sent from", legal blocks)
SIGNATURE_PATTERNS = [
    r"\n\s*Sent from my (?:iPhone|iPad|Android|Samsung|Galaxy|Pixel)\b.*",
    r"\n\s*Get Outlook for\s+.*",
    r"\n\s*Sent from (?:Mail|Gmail)?\s+for (?:iOS|Android)\s*.*",
    r"\n\s*_{3,}\s*\n\s*From:\s+.*",
    r"\n\s*--\s*\n",
    r"\n\s*_{5,}\s*$",
    r"\n\s*-\s{0,2}$",
]
SIGNATURE_REGEX = re.compile(
    "|".join(f"({p})" for p in SIGNATURE_PATTERNS), re.IGNORECASE | re.DOTALL
)
DISCLAIMER_STARTS = [
    r"\n\s*(?:This\s+)?(?:e-?mail|message|communication)\s+(?:is\s+)?(?:confidential|intended only).*",
    r"\n\s*Disclaimer\s*:.*",
    r"\n\s*CONFIDENTIALITY\s+NOTICE\s*:.*",
    r"\n\s*If you (?:received|have received) this (?:e-?mail|message) in error.*",
    r"\n\s*Please consider the environment before printing.*",
    r"\n\s*\[?PRIVACY\]?.*",
]
DISCLAIMER_REGEX = re.compile(
    "|".join(f"({p})" for p in DISCLAIMER_STARTS), re.IGNORECASE | re.DOTALL
)

# Quoted reply boundaries: "On ... wrote:", "From: ... Sent:", "Forwarded message", etc.
QUOTE_PATTERNS = [
    r"\n\s*On\s+.+?\s+wrote\s*:\s*\n",
    r"\n\s*_{3,}\s*\n\s*From:\s+",
    r"\n-{3,}\s*Original Message\s*-{3,}\s*\n",
    r"\n\s*_{2,}\s*\n\s*From:\s+",
    r"\n\s*On\s+\d{1,2}/\d{1,2}/\d{2,4}.+?\n",
    r"\n\s*----------\s+Forwarded message\s+----------\s*\n",
    r"\n\s*Begin forwarded message\s*:.*",
]
QUOTE_REGEX = re.compile(
    "|".join(f"({p})" for p in QUOTE_PATTERNS), re.IGNORECASE | re.DOTALL
)


def _strip_by_first_match(text: str, regex: re.Pattern) -> str:
    m = regex.search(text)
    if not m:
        return text
    return text[: m.start()].rstrip()


def strip_signatures_and_disclaimers(body: str) -> str:
    if not body or not body.strip():
        return body
    text = body
    text = _strip_by_first_match(text, SIGNATURE_REGEX)
    text = _strip_by_first_match(text, DISCLAIMER_REGEX)
    return text.strip()


def strip_quoted_replies(body: str) -> str:
    if not body or not body.strip():
        return body
    m = QUOTE_REGEX.search(body)
    if not m:
        return body.strip()
    return body[: m.start()].rstrip()


def preprocess_body_for_embedding(
    body: str,
    strip_quotes: bool = True,
    strip_signatures: bool = True,
    strip_tracking: bool = True,
    strip_invisible: bool = True,
    llm_cleanup: bool = False,
) -> str:
    if not body or not body.strip():
        return ""
    text = body.strip()
    if strip_invisible:
        text = strip_invisible_unicode(text)
    if strip_tracking:
        text = strip_tracking_urls_from_text(text)
    if strip_quotes:
        text = strip_quoted_replies(text)
    if strip_signatures:
        text = strip_signatures_and_disclaimers(text)
    if llm_cleanup and settings.VLLM_URL:
        text = _llm_extract_main_content(text) or text
    return text.strip()


def _llm_extract_main_content(body: str) -> str | None:
    import httpx

    if not body or len(body) > 12000:
        return None
    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.post(
                f"{settings.VLLM_URL.rstrip('/')}/chat/completions",
                json={
                    "model": settings.VLLM_MODEL,
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                "Extract only the main content of this email that the sender wrote. "
                                "Remove quoted previous messages, email signatures, and legal disclaimers. "
                                "Return only the extracted main content, nothing else.\n\n"
                                "Email:\n" + body
                            ),
                        }
                    ],
                    "max_tokens": 4096,
                },
            )
            r.raise_for_status()
            out = (
                r.json()
                .get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
            )
            return out if out else None
    except Exception as e:
        logger.debug("LLM main-content extraction failed: %s", e)
        return None
