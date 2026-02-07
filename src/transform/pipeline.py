import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from sqlalchemy.orm import Session
from sqlalchemy import delete, select

from core.models import AccountLabel, Email, EmailChunk, EMBEDDING_DIM
from core.privacy import PrivacyTier
from core.embeddings import Embedder
from .privacy import EmailData, PrivacyManager
from .spacy_client import full_redact_for_display
from core.preprocessing import preprocess_body_for_embedding
from .fasttext_client import detect_language
from .chunking import Chunk, chunk_body, weighted_mean_embedding
from core.capabilities_loader import (
    get_capabilities_path,
    get_embed_max_chars,
    get_embed_max_tokens,
    require_capabilities_for_transform,
)

logger = logging.getLogger(__name__)


def _format_exception(exc: BaseException) -> str:
    name = type(exc).__name__
    msg = str(exc).strip() if str(exc) else ""
    out = f"{name}: {msg}" if msg else name
    if hasattr(exc, "response") and exc.response is not None:
        try:
            out += f" (status={getattr(exc.response, 'status_code', None)}, url={getattr(exc.response, 'url', None)})"
        except Exception:
            pass
    return out or repr(exc)

EMBED_BATCH_SIZE = 1
PREPARE_CONCURRENCY = 4
SNIPPET_REDACTED_PLACEHOLDER = "Content redacted"


@dataclass
class _PreparePayload:
    email: Email
    privacy_tier: int
    body_redacted: Optional[str]
    snippet_redacted: Optional[str]  # Safe for display; placeholder for SENSITIVE/PERSONAL, redacted snippet for PUBLIC
    text_to_embed: Optional[str]
    subject: str
    body_type: str  # "full" | "chunked" | "none"
    chunks: list = field(default_factory=list)  # list of Chunk when body_type == "chunked"
    subject_text: str = ""  # text to embed for subject (empty if SENSITIVE or no subject)
    body_text: Optional[str] = None  # text to embed for full body (None if chunked or none)


def _validate_embedding(vec: list | None, name: str, expect_content: bool) -> None:
    if vec is None or (isinstance(vec, list) and len(vec) == 0):
        if expect_content:
            raise ValueError("%s is missing but content was expected" % name)
        return
    if len(vec) != EMBEDDING_DIM:
        raise ValueError("%s has wrong dim %s (expected %s)" % (name, len(vec), EMBEDDING_DIM))
    if expect_content and all(x == 0.0 for x in vec):
        raise ValueError("%s is all zeros; embedding likely failed" % name)


def _validate_transform_result(
    *,
    privacy_tier: int,
    subject_emb: list,
    body_emb: list,
    body_pooled_emb: list,
    chunk_rows: list,
    text_to_embed: str | None,
    subject: str,
) -> None:
    if privacy_tier not in (PrivacyTier.SENSITIVE, PrivacyTier.PERSONAL, PrivacyTier.PUBLIC):
        raise ValueError("Invalid privacy_tier %s" % privacy_tier)
    expect_subject = bool(subject.strip()) and privacy_tier != PrivacyTier.SENSITIVE
    _validate_embedding(subject_emb, "subject_embedding", expect_subject)
    expect_body = bool(text_to_embed and text_to_embed.strip())
    if body_emb:
        _validate_embedding(body_emb, "body_embedding", expect_body)
    if body_pooled_emb:
        _validate_embedding(body_pooled_emb, "body_pooled_embedding", expect_body)
    if expect_body and not body_emb and not body_pooled_emb:
        raise ValueError("Body content to embed but no body_embedding or body_pooled_embedding")
    for i, (_, _, _, vec) in enumerate(chunk_rows):
        _validate_embedding(vec, f"chunk[{i}].embedding", True)


class TransformPipeline:
    def __init__(
        self,
        db: Session,
        privacy_manager: Optional[PrivacyManager] = None,
        embedder: Optional[Embedder] = None,
    ):
        self.db = db
        self.privacy = privacy_manager or PrivacyManager()
        self.embedder = embedder or Embedder()

    def run(
        self,
        account_id: Optional[int] = None,
        email_id: Optional[int] = None,
        force: bool = False,
        batch_size: int = 50,
        limit: Optional[int] = None,
        progress_callback: Optional[Callable[[dict], None]] = None,
    ) -> int:
        caps = require_capabilities_for_transform()
        path = get_capabilities_path()
        embed = caps.get("embedding") or {}
        vllm = caps.get("vllm") or {}
        logger.info(
            "Using capabilities from %s: embed max_tokens=%s (model=%s), vllm model_id=%s",
            path,
            embed.get("max_tokens"),
            embed.get("model_id"),
            vllm.get("model_id"),
        )

        stmt = (
            select(Email)
            .where(Email.deleted_at.is_(None))
            .where(Email.body_text.isnot(None))
        )
        if email_id is not None:
            stmt = stmt.where(Email.id == email_id)
        if account_id is not None:
            stmt = stmt.where(Email.account_id == account_id)
        if not force and email_id is None:
            stmt = stmt.where(Email.transform_completed_at.is_(None))
        stmt = stmt.order_by(Email.id)
        if limit and email_id is None:
            stmt = stmt.limit(limit)
        rows = self.db.execute(stmt).scalars().all()

        total = len(rows)
        if total == 0:
            logger.info(
                "No emails to transform (account_id=%s, email_id=%s, force=%s)",
                account_id,
                email_id,
                force,
            )
            return 0

        logger.info(
            "Transform pipeline: %s emails to process (account_id=%s, email_id=%s, force=%s, batch_size=%s)",
            total,
            account_id,
            email_id,
            force,
            batch_size,
        )

        debug_prompts_for_email_id = email_id
        summary = asyncio.run(
            self._run_batches(
                [e.id for e in rows],
                batch_size,
                total,
                progress_callback=progress_callback,
                debug_prompts_for_email_id=debug_prompts_for_email_id,
            )
        )
        self.db.commit()

        transformed = summary["transformed"]
        failed = summary["failed"]
        by_tier = summary.get("by_tier") or {}
        body_full = summary.get("body_full", 0)
        body_chunked = summary.get("body_chunked", 0)
        logger.info(
            "Transform summary: %s updated, %s failed | By tier: SENSITIVE=%s PERSONAL=%s PUBLIC=%s | Body: full=%s chunked=%s",
            transformed,
            failed,
            by_tier.get(PrivacyTier.SENSITIVE, 0),
            by_tier.get(PrivacyTier.PERSONAL, 0),
            by_tier.get(PrivacyTier.PUBLIC, 0),
            body_full,
            body_chunked,
        )
        return transformed

    async def _run_batches(
        self,
        email_ids: list[int],
        batch_size: int,
        total: int,
        progress_callback: Optional[Callable[[dict], None]] = None,
        debug_prompts_for_email_id: Optional[int] = None,
    ) -> dict:
        transformed = 0
        failed = 0
        by_tier: dict[int, int] = {}
        body_full = 0
        body_chunked = 0
        total_batches = (total + batch_size - 1) // batch_size
        if progress_callback is not None:
            progress_callback({
                "total": total,
                "processed": 0,
                "failed": 0,
                "by_tier": {},
                "body_full": 0,
                "body_chunked": 0,
                "batch_num": 0,
                "total_batches": total_batches,
            })
        for i in range(0, len(email_ids), batch_size):
            batch_ids = email_ids[i : i + batch_size]
            batch_num = i // batch_size + 1
            logger.info(
                "Batch %s/%s: %s emails (%s–%s of %s)",
                batch_num,
                total_batches,
                len(batch_ids),
                i + 1,
                min(i + len(batch_ids), total),
                total,
            )
            batch_result = await self._transform_batch(
                batch_ids, debug_prompts_for_email_id=debug_prompts_for_email_id
            )
            transformed += batch_result["ok"]
            failed += batch_result["failed"]
            for t, c in (batch_result.get("by_tier") or {}).items():
                by_tier[t] = by_tier.get(t, 0) + c
            body_full += batch_result.get("body_full", 0)
            body_chunked += batch_result.get("body_chunked", 0)
            self.db.commit()
            if progress_callback is not None:
                progress_callback({
                    "total": total,
                    "processed": transformed,
                    "failed": failed,
                    "by_tier": dict(by_tier),
                    "body_full": body_full,
                    "body_chunked": body_chunked,
                    "batch_num": batch_num,
                    "total_batches": total_batches,
                })
        return {
            "transformed": transformed,
            "failed": failed,
            "by_tier": by_tier,
            "body_full": body_full,
            "body_chunked": body_chunked,
        }

    def _load_label_maps_for_accounts(self, account_ids: list[int]) -> dict[int, dict[str, str]]:
        if not account_ids:
            return {}
        account_ids = list(set(account_ids))
        rows = self.db.execute(
            select(AccountLabel).where(AccountLabel.account_id.in_(account_ids))
        ).scalars().all()
        out: dict[int, dict[str, str]] = {aid: {} for aid in account_ids}
        for al in rows:
            out[al.account_id][al.label_id] = al.label_name
        return out

    async def _transform_batch(
        self,
        email_ids: list[int],
        *,
        debug_prompts_for_email_id: Optional[int] = None,
    ) -> dict:
        emails = [self.db.get(Email, eid) for eid in email_ids]
        emails = [e for e in emails if e is not None and e.body_text]
        if not emails:
            return {"ok": 0, "failed": 0, "by_tier": {}, "body_full": 0, "body_chunked": 0}

        account_ids = list({e.account_id for e in emails})
        label_maps = self._load_label_maps_for_accounts(account_ids)
        sem = asyncio.Semaphore(PREPARE_CONCURRENCY)

        async def prepare_with_sem(email: Email):
            async with sem:
                label_id_to_name = label_maps.get(email.account_id) or {}
                return await self._prepare_one(
                    email,
                    label_id_to_name=label_id_to_name,
                    debug_prompts_for_email_id=debug_prompts_for_email_id,
                )

        tasks = [prepare_with_sem(e) for e in emails]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        payloads: list[_PreparePayload] = []
        for e, res in zip(emails, results):
            if isinstance(res, Exception):
                err_msg = _format_exception(res)
                logger.warning("Transform failed for email id=%s (prepare): %s", e.id, err_msg)
                continue
            if isinstance(res, _PreparePayload):
                payloads.append(res)

        if not payloads:
            logger.warning("Batch had no successful prepares; email ids in batch: %s", email_ids)
            return {
                "ok": 0,
                "failed": len(emails),
                "by_tier": {},
                "body_full": 0,
                "body_chunked": 0,
            }

        batch_email_ids = [p.email.id for p in payloads]
        logger.info(
            "Embedding batch for email ids %s–%s (%s emails)",
            min(batch_email_ids),
            max(batch_email_ids),
            len(payloads),
        )

        items: list[tuple[int, int, str, str, Optional[tuple]]] = []
        for idx, p in enumerate(payloads):
            if p.subject_text:
                items.append((idx, p.email.id, "subject", p.subject_text, None))
            if p.body_type == "full" and p.body_text:
                items.append((idx, p.email.id, "body", p.body_text, None))
            if p.body_type == "chunked" and p.chunks:
                for c in p.chunks:
                    items.append((idx, p.email.id, "chunk", c.text, (c.position, c.weight)))

        n_subj = sum(1 for _ in (it for it in items if it[2] == "subject"))
        n_body = sum(1 for _ in (it for it in items if it[2] == "body"))
        n_chunk = sum(1 for _ in (it for it in items if it[2] == "chunk"))
        logger.info(
            "Embedding batch: %s subjects, %s bodies, %s chunks (total %s texts)",
            n_subj,
            n_body,
            n_chunk,
            len(items),
        )

        max_chars = get_embed_max_chars()
        max_tokens = get_embed_max_tokens()
        texts = [it[3][:max_chars] for it in items]
        log_ctx = "email ids %s–%s" % (min(batch_email_ids), max(batch_email_ids))
        try:
            vectors = await asyncio.to_thread(
                self.embedder.encode_batch,
                texts,
                batch_size=EMBED_BATCH_SIZE,
                max_chars_per_input=max_chars,
                max_tokens_per_input=max_tokens,
                log_context=log_ctx,
            )
        except Exception as e:
            logger.error(
                "Embedding batch failed for email ids %s: %s",
                batch_email_ids,
                e,
                exc_info=True,
            )
            for eid in batch_email_ids:
                logger.warning("Transform failed for email id=%s (embed): %s", eid, e)
            return {
                "ok": 0,
                "failed": len(emails),
                "by_tier": {},
                "body_full": 0,
                "body_chunked": 0,
            }

        if len(vectors) != len(items):
            logger.error(
                "Embedding batch length mismatch: %s vectors for %s items (email ids %s)",
                len(vectors),
                len(items),
                batch_email_ids,
            )
            raise ValueError(
                "Embed batch length mismatch: got %s vectors for %s items"
                % (len(vectors), len(items))
            )

        payload_embs: dict[int, dict] = {idx: {"subject": None, "body": None, "chunks": []} for idx in range(len(payloads))}
        for (idx, _eid, role, _text, extra), vec in zip(items, vectors):
            if role == "subject":
                payload_embs[idx]["subject"] = vec
            elif role == "body":
                payload_embs[idx]["body"] = vec
            elif role == "chunk" and extra is not None:
                pos, weight = extra
                payload_embs[idx]["chunks"].append((vec, pos, weight))

        ok = 0
        by_tier: dict[int, int] = {}
        body_full = 0
        body_chunked = 0
        for idx, p in enumerate(payloads):
            em = payload_embs[idx]
            subject_emb = em["subject"] or []
            body_emb = em["body"] or []
            chunk_rows: list[tuple[str, int, float, list]] = [
                (p.chunks[i].text, pos, weight, vec)
                for i, (vec, pos, weight) in enumerate(sorted(em["chunks"], key=lambda x: x[1]))
            ]
            body_pooled_emb = weighted_mean_embedding(
                [r[3] for r in chunk_rows],
                [r[2] for r in chunk_rows],
            ) if chunk_rows else []

            try:
                _validate_transform_result(
                    privacy_tier=p.privacy_tier,
                    subject_emb=subject_emb,
                    body_emb=body_emb,
                    body_pooled_emb=body_pooled_emb,
                    chunk_rows=chunk_rows,
                    text_to_embed=p.text_to_embed,
                    subject=p.subject,
                )
            except ValueError as e:
                logger.warning(
                    "Transform failed for email id=%s (validation): %s",
                    p.email.id,
                    e,
                )
                continue

            p.email.privacy_tier = p.privacy_tier
            p.email.body_redacted = p.body_redacted
            p.email.snippet_redacted = p.snippet_redacted
            p.email.subject_embedding = subject_emb if subject_emb else None
            p.email.body_embedding = body_emb if body_emb else None
            p.email.body_pooled_embedding = body_pooled_emb if body_pooled_emb else None
            p.email.transform_completed_at = datetime.now(timezone.utc)

            self.db.execute(delete(EmailChunk).where(EmailChunk.email_id == p.email.id))
            for text, pos, weight, vec in chunk_rows:
                self.db.add(
                    EmailChunk(
                        email_id=p.email.id,
                        chunk_text=text,
                        chunk_position=pos,
                        chunk_weight=weight,
                        chunk_embedding=vec if isinstance(vec, list) else list(vec),
                    )
                )
            ok += 1
            by_tier[p.privacy_tier] = by_tier.get(p.privacy_tier, 0) + 1
            if p.body_type == "full":
                body_full += 1
            elif p.body_type == "chunked":
                body_chunked += 1

        failed = len(emails) - ok
        if failed:
            logger.info(
                "Batch complete: %s succeeded, %s failed (email ids in batch: %s)",
                ok,
                failed,
                email_ids,
            )
        return {
            "ok": ok,
            "failed": failed,
            "by_tier": by_tier,
            "body_full": body_full,
            "body_chunked": body_chunked,
        }

    async def _prepare_one(
        self,
        email: Email,
        *,
        label_id_to_name: Optional[dict[str, str]] = None,
        debug_prompts_for_email_id: Optional[int] = None,
    ) -> _PreparePayload:
        raw_body = email.body_text or ""
        subject = email.subject or ""
        sender = (
            f"{email.from_name} <{email.from_email}>" if (email.from_name and email.from_name.strip()) else (email.from_email or "")
        )
        logger.debug("Prepare email id=%s subject=%r", email.id, (subject[:60] + "…") if len(subject) > 60 else subject)
        body_type: str = "none"

        body_cleaned = preprocess_body_for_embedding(
            raw_body, strip_quotes=True, strip_signatures=True, llm_cleanup=False
        )

        label_ids = list(email.labels or [])
        label_names = (
            [label_id_to_name.get(lid, lid) for lid in label_ids]
            if label_id_to_name is not None
            else label_ids
        )

        debug_email_id = (
            debug_prompts_for_email_id
            if debug_prompts_for_email_id is not None and email.id == debug_prompts_for_email_id
            else None
        )
        classification = await self.privacy.classify(
            EmailData(
                body=raw_body,
                subject=subject,
                sender=sender or "",
                has_attachments=email.has_attachments or False,
                labels=label_names,
                debug_email_id=debug_email_id,
            )
        )
        privacy_tier = classification.tier
        tier_name = classification.tier_name
        logger.debug(
            "Email id=%s classified as %s (%s ms)",
            email.id,
            tier_name,
            classification.processing_time_ms,
        )
        detected_lang = await asyncio.to_thread(detect_language, body_cleaned)
        body_redacted = full_redact_for_display(body_cleaned, lang=detected_lang)
        logger.debug("Email id=%s redacted (%s, lang=%s)", email.id, tier_name, detected_lang)

        if privacy_tier == PrivacyTier.SENSITIVE or privacy_tier == PrivacyTier.PERSONAL:
            snippet_redacted = SNIPPET_REDACTED_PLACEHOLDER
        else:
            raw_snippet = (email.snippet or "").strip()
            snippet_redacted = full_redact_for_display(raw_snippet, lang=detected_lang) if raw_snippet else ""

        text_to_embed = (body_cleaned or "").strip() or None
        subject_text = (subject or "").strip() if (subject or "").strip() else ""
        body_text: Optional[str] = None
        chunks: list[Chunk] = []
        max_tokens = get_embed_max_tokens()

        if text_to_embed and self.embedder.endpoint_url:
            token_count = self.embedder.token_count(text_to_embed)
            if token_count <= max_tokens:
                body_type = "full"
                body_text = text_to_embed
                logger.debug("Email id=%s body fits (%s tokens), will embed full body", email.id, token_count)
            else:
                body_type = "chunked"
                chunks = chunk_body(text_to_embed, self.embedder, max_tokens=max_tokens)
                logger.debug("Email id=%s body long (%s tokens), %s chunks", email.id, token_count, len(chunks))

        return _PreparePayload(
            email=email,
            privacy_tier=privacy_tier,
            body_redacted=body_redacted,
            snippet_redacted=snippet_redacted or None,
            text_to_embed=text_to_embed,
            subject=subject or "",
            body_type=body_type,
            chunks=chunks,
            subject_text=subject_text,
            body_text=body_text,
        )

