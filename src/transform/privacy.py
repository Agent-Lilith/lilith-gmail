import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
from pydantic import BaseModel, Field, field_validator

from core.config import settings
from core.privacy import PrivacyTier

from . import vllm_client


class EmailData(BaseModel):
    body: str = Field(default="", max_length=1_000_000)
    subject: str = Field(default="", max_length=1000)
    sender: str = Field(default="", max_length=500)
    has_attachments: bool = False
    labels: list[str] = Field(default_factory=list, max_length=100)
    debug_email_id: int | None = None

    @field_validator("subject", "body", mode="before")
    @classmethod
    def strip_whitespace(cls, v: str) -> str:
        return v.strip() if isinstance(v, str) else (v or "")

    @field_validator("sender", mode="before")
    @classmethod
    def normalize_sender(cls, v: str) -> str:
        if not v or not isinstance(v, str):
            return (v or "").strip()
        s = v.strip()
        # Allow only sender that looks like an email (contains @); otherwise treat as empty
        # (real-world data often has "unknown", display names without address, etc.)
        if s and "@" not in s:
            return ""
        return s.lower() if s else s


class ClassificationResult(BaseModel):
    tier: int
    tier_name: str
    confidence: float | None = None
    reasoning: str | None = None
    processing_time_ms: int


@dataclass
class ClassificationMetrics:
    total_calls: int = 0
    sensitive_count: int = 0
    personal_count: int = 0
    public_count: int = 0
    errors: int = 0
    avg_latency_ms: float = 0.0


VALID_TIERS = frozenset({"SENSITIVE", "PERSONAL", "PUBLIC"})
TIER_ORDER: tuple[str, ...] = ("SENSITIVE", "PERSONAL", "PUBLIC")
TIER_VARIATIONS: dict[str, str] = {
    "SENS": "SENSITIVE",
    "PRIV": "PERSONAL",
    "PERS": "PERSONAL",
    "PUBL": "PUBLIC",
    "PUB": "PUBLIC",
}

# Word-boundary tier match (first match wins; avoids "NOT PUBLIC" → PUBLIC)
_TIER_WORD_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bSENSITIVE\b", re.IGNORECASE),
    re.compile(r"\bPERSONAL\b", re.IGNORECASE),
    re.compile(r"\bPUBLIC\b", re.IGNORECASE),
)

# Strip <think> / <thinking> blocks so we parse only the final answer
_THINK_PATTERN = re.compile(
    r"<think>[\s\S]*?</think>|"
    r"<think>[\s\S]*$|"
    r"<(?:think|thinking)\b[^>]*>[\s\S]*?</(?:think|thinking)\s*>|"
    r"<(?:think|thinking)\b[^>]*>[\s\S]*$",
    flags=re.IGNORECASE,
)


def _write_classification_prompts_debug(
    email_id: int, system_content: str, user_content: str
) -> None:
    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"classification_prompts_email_{email_id}.txt"
    with open(path, "w", encoding="utf-8") as f:
        f.write("=== SYSTEM PROMPT ===\n\n")
        f.write(system_content)
        f.write("\n\n=== USER PROMPT ===\n\n")
        f.write(user_content)
        f.write("\n")
    logger.info("Wrote classification prompts for debugging: %s", path)


def _strip_think_block(text: str) -> str:
    if not text or not text.strip():
        return text
    out = text
    while True:
        prev, out = out, _THINK_PATTERN.sub("", out).strip()
        if out == prev:
            break
    return out


def _extract_tier_from_text(text: str) -> str | None:
    if not text or not text.strip():
        return None
    upper = text.strip().upper()
    for tier, pattern in zip(TIER_ORDER, _TIER_WORD_PATTERNS, strict=True):
        if pattern.search(upper):
            return tier
    for tier in TIER_ORDER:
        if tier in upper:
            return tier
    return None


def _parse_tier(raw_response: str) -> str:
    raw = raw_response or ""
    cleaned = _strip_think_block(raw).strip().upper()
    if not cleaned:
        tier = _extract_tier_from_text(raw)
        if tier is not None:
            return tier
        raise ValueError(
            "Classification response was empty after stripping think blocks"
        )

    if cleaned in VALID_TIERS:
        return cleaned
    token = cleaned.split()[0] if cleaned.split() else ""
    if token in VALID_TIERS:
        return token
    for variant, tier in TIER_VARIATIONS.items():
        if variant in cleaned:
            return tier
    for tier, pattern in zip(TIER_ORDER, _TIER_WORD_PATTERNS, strict=True):
        if pattern.search(cleaned):
            return tier
    for tier in TIER_ORDER:
        if tier in cleaned:
            return tier

    preview = (
        (raw_response[:100] + "…")
        if len(raw_response or "") > 100
        else (raw_response or "")
    )
    raise ValueError(
        "Could not parse tier from classification response (expected SENSITIVE, PERSONAL, or PUBLIC). "
        f"Preview: {preview!r}"
    )


from core.capabilities_loader import get_classify_max_model_len, get_vllm_model_id

from .prompt_loader import get_classification_prompts
from .spacy_client import sanitize_with_spacy_api

logger = logging.getLogger(__name__)


class PrivacyManager:
    def __init__(self):
        self.vllm_url = (settings.VLLM_URL or "").rstrip("/")
        self._vllm_base = vllm_client._vllm_base_url(self.vllm_url)
        self.metrics = ClassificationMetrics()
        self._metrics_lock = asyncio.Lock()
        prompts = get_classification_prompts()
        self._classify_system_template = prompts["system"]
        self._classify_user_template = prompts["user_template"]
        self._output_labels = "SENSITIVE, PERSONAL, or PUBLIC"

    @asynccontextmanager
    async def _track_classification(self, operation: str):
        start = time.perf_counter()
        try:
            yield
        except Exception as e:
            async with self._metrics_lock:
                self.metrics.errors += 1
            logger.error("Classification error in %s: %s", operation, e)
            raise
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            async with self._metrics_lock:
                self.metrics.total_calls += 1
                n = self.metrics.total_calls
                self.metrics.avg_latency_ms = (
                    self.metrics.avg_latency_ms * (n - 1) + elapsed_ms
                ) / n
                if self.metrics.total_calls % 100 == 0:
                    logger.info(
                        "Classification metrics: %s calls, %.1f ms avg, %s errors",
                        self.metrics.total_calls,
                        self.metrics.avg_latency_ms,
                        self.metrics.errors,
                    )

    async def classify(self, email: EmailData) -> ClassificationResult:
        start = time.perf_counter()
        async with self._track_classification("classify"):
            tier = await self._classify_core(
                email.body,
                email.subject,
                email.sender,
                has_attachments=email.has_attachments,
                labels=email.labels or [],
                debug_email_id=email.debug_email_id,
            )
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        async with self._metrics_lock:
            if tier == PrivacyTier.SENSITIVE:
                self.metrics.sensitive_count += 1
            elif tier == PrivacyTier.PERSONAL:
                self.metrics.personal_count += 1
            else:
                self.metrics.public_count += 1
        tier_name = (
            "SENSITIVE"
            if tier == PrivacyTier.SENSITIVE
            else "PERSONAL"
            if tier == PrivacyTier.PERSONAL
            else "PUBLIC"
            if tier == PrivacyTier.PUBLIC
            else "?"
        )
        return ClassificationResult(
            tier=tier,
            tier_name=tier_name,
            processing_time_ms=elapsed_ms,
        )

    async def _classify_core(
        self,
        body: str,
        subject: str,
        sender: str = "",
        *,
        has_attachments: bool = False,
        labels: list[str] | None = None,
        debug_email_id: int | None = None,
    ) -> int:
        if not self.vllm_url:
            raise RuntimeError(
                "VLLM_URL is not set. Privacy classification requires a running vLLM server."
            )
        max_tokens = get_classify_max_model_len()
        reserve_tokens = 150
        max_prompt_tokens = max(0, max_tokens - reserve_tokens)
        labels = labels or []

        async def token_count_async(text: str) -> int:
            return await asyncio.to_thread(
                vllm_client.token_count_sync, self._vllm_base, text
            )

        body_text = (body or "").strip()
        sender_str = sender or "(unknown)"
        subject_str = subject or ""

        if not body_text:
            return await self._classify_with_llm(
                sender_str,
                subject_str,
                "",
                token_count_async,
                max_prompt_tokens,
                has_attachments=has_attachments,
                labels=labels,
                debug_email_id=debug_email_id,
            )

        body_preview = await self._fit_body_to_budget(
            body_text,
            sender_str,
            subject_str,
            token_count_async,
            max_prompt_tokens,
            has_attachments=has_attachments,
            labels=labels,
        )
        return await self._classify_with_llm(
            sender_str,
            subject_str,
            body_preview,
            token_count_async,
            max_prompt_tokens,
            has_attachments=has_attachments,
            labels=labels,
            debug_email_id=debug_email_id,
        )

    def _template_vars(
        self,
        sender_str: str,
        subject_str: str,
        body_preview: str,
        *,
        has_attachments: bool = False,
        labels: list[str] | None = None,
    ) -> dict:
        return {
            "sender": sender_str,
            "subject": subject_str,
            "body_preview": body_preview,
            "output_labels": self._output_labels,
            "has_attachments": "yes" if has_attachments else "no",
            "labels": ", ".join(labels) if labels else "none",
        }

    def _full_prompt_for_token_count(
        self,
        sender_str: str,
        subject_str: str,
        body_preview: str,
        *,
        has_attachments: bool = False,
        labels: list[str] | None = None,
    ) -> str:
        v = self._template_vars(
            sender_str,
            subject_str,
            body_preview,
            has_attachments=has_attachments,
            labels=labels,
        )
        system_content = self._classify_system_template.format(**v)
        user_content = self._classify_user_template.format(**v)
        return system_content + "\n\n" + user_content

    async def _fit_body_to_budget(
        self,
        body_text: str,
        sender_str: str,
        subject_str: str,
        token_count_async,
        max_prompt_tokens: int,
        *,
        has_attachments: bool = False,
        labels: list[str] | None = None,
    ) -> str:
        def make_prompt(body_preview: str) -> str:
            return self._full_prompt_for_token_count(
                sender_str,
                subject_str,
                body_preview,
                has_attachments=has_attachments,
                labels=labels,
            )

        full_prompt = make_prompt(body_text)
        if await token_count_async(full_prompt) <= max_prompt_tokens:
            return body_text
        n = len(body_text)
        start_len = n // 2 + n // 4  # 3/4 of body
        end_len = n // 4
        if start_len + end_len > n:
            start_len = n // 2
            end_len = n - start_len

        while True:
            if start_len + end_len >= n:
                body_preview = body_text
            else:
                body_preview = body_text[:start_len] + "\n...\n" + body_text[-end_len:]
            prompt = make_prompt(body_preview)
            if await token_count_async(prompt) <= max_prompt_tokens:
                return body_preview
            start_len = max(100, start_len - 500)
            end_len = max(100, end_len - 200)
            if start_len <= 100 and end_len <= 100:
                return body_text[:start_len] + "\n...\n" + body_text[-end_len:]

    async def _classify_with_llm(
        self,
        sender_str: str,
        subject_str: str,
        body_preview: str,
        token_count_async,
        max_prompt_tokens: int,
        *,
        has_attachments: bool = False,
        labels: list[str] | None = None,
        debug_email_id: int | None = None,
    ) -> int:
        v = self._template_vars(
            sender_str,
            subject_str,
            body_preview,
            has_attachments=has_attachments,
            labels=labels,
        )
        system_content = self._classify_system_template.format(**v)
        user_content = self._classify_user_template.format(**v)

        if debug_email_id is not None:
            _write_classification_prompts_debug(
                debug_email_id, system_content, user_content
            )

        full_prompt_for_count = self._full_prompt_for_token_count(
            sender_str,
            subject_str,
            body_preview,
            has_attachments=has_attachments,
            labels=labels,
        )
        n_tokens = await token_count_async(full_prompt_for_count)
        if n_tokens > max_prompt_tokens:
            raise ValueError(
                "Classification prompt exceeds token limit after truncation"
            )

        model_id = get_vllm_model_id()
        logger.debug("Classifying with vLLM model_id=%s", model_id)
        payload = {
            "model": model_id,
            "messages": [
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content},
            ],
            "max_tokens": 64,
            "temperature": 0.0,
            "top_p": 1.0,
            "seed": 42,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.vllm_url}/chat/completions",
                json=payload,
                timeout=120.0,
            )
            if response.status_code == 400:
                try:
                    err_body = response.text[:500] if response.text else "(empty)"
                    logger.warning(
                        "vLLM 400 Bad Request (classification). Response body: %s. Prompt length: %s chars.",
                        err_body,
                        len(user_content),
                    )
                except Exception:
                    pass
            response.raise_for_status()
        data = response.json()
        try:
            raw = data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as e:
            raise ValueError(
                "vLLM returned unexpected response shape for classification"
            ) from e
        logger.info(
            "Classification LLM full response (len=%s): %s",
            len(raw),
            raw,
        )
        tier_str = _parse_tier(raw)
        return getattr(PrivacyTier, tier_str)

    def sanitize(self, body: str, lang: str = "en") -> str:
        if not body:
            return ""
        if not (settings.SPACY_API_URL or "").strip():
            raise RuntimeError(
                "SPACY_API_URL is not set. PII sanitization for PERSONAL emails requires Spacy API."
            )
        return sanitize_with_spacy_api(body, lang=lang or "en")
