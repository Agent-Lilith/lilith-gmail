import asyncio
import logging
from typing import List, Optional, Union

import httpx

from core.config import settings
from core.models import EMBEDDING_DIM

logger = logging.getLogger(__name__)


class Embedder:
    def __init__(self, endpoint_url: Optional[str] = None) -> None:
        self.endpoint_url = (endpoint_url or settings.EMBEDDING_URL or "").rstrip("/")
        if not self.endpoint_url:
            logger.warning("EMBEDDING_URL not set; transform and semantic search will fail for embedding")
        else:
            logger.info("Embedder: TEI at %s (dim=%s)", self.endpoint_url, EMBEDDING_DIM)

    def _sync_post(
        self, text: Union[str, List[str]], path: str = "/embed"
    ) -> Union[List[float], List[List[float]]]:
        if not text:
            return [] if isinstance(text, list) else [0.0] * EMBEDDING_DIM
        if not self.endpoint_url:
            raise RuntimeError(
                "EMBEDDING_URL is not set. Embedding and tokenize require a running TEI server."
            )
        timeout = 300.0 if path == "/embed" else 60.0
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                f"{self.endpoint_url}{path}",
                json={"inputs": text if isinstance(text, list) else [text]},
            )
            resp.raise_for_status()
            data = resp.json()
        if path == "/embed":
            if isinstance(text, str):
                return data[0] if data and isinstance(data[0], list) else data
            return data
        return data

    def tokenize(self, text: str) -> List[int]:
        if not text:
            return []
        if not self.endpoint_url:
            raise RuntimeError("EMBEDDING_URL is not set. Tokenize requires a running TEI server.")
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                f"{self.endpoint_url}/tokenize",
                json={"inputs": [text]},
            )
            resp.raise_for_status()
            data = resp.json()
        if isinstance(data, list) and data and isinstance(data[0], list):
            return data[0]
        if isinstance(data, list) and data and isinstance(data[0], int):
            return data
        raise ValueError("TEI /tokenize returned unexpected response shape")

    def token_count(self, text: str) -> int:
        return len(self.tokenize(text))

    async def encode(
        self, text: Union[str, List[str]]
    ) -> Union[List[float], List[List[float]]]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sync_post, text, "/embed")

    def encode_sync(
        self, text: Union[str, List[str]]
    ) -> Union[List[float], List[List[float]]]:
        return self._sync_post(text, "/embed")

    def _embed_one(self, text: str) -> List[float]:
        result = self._sync_post([text], "/embed")
        if not isinstance(result, list) or len(result) != 1:
            raise ValueError("TEI /embed returned unexpected shape for single text")
        v = result[0]
        if not isinstance(v, list) or len(v) != EMBEDDING_DIM:
            raise ValueError(
                "TEI /embed returned invalid vector (dim %s, expected %s)"
                % (len(v) if isinstance(v, list) else "non-list", EMBEDDING_DIM)
            )
        return list(v)

    def _truncate_to_max_tokens(self, text: str, max_tokens: int) -> str:
        if not text or max_tokens <= 0:
            return text
        n = self.token_count(text)
        if n <= max_tokens:
            return text
        max_len = int(len(text) * max_tokens / n)
        for _ in range(15):
            if max_len <= 0:
                return ""
            truncated = text[:max_len]
            if self.token_count(truncated) <= max_tokens:
                return truncated
            max_len = int(max_len * 0.9)
        return text[:max_len] if max_len > 0 else ""

    def encode_batch(
        self,
        texts: List[str],
        batch_size: int = 4,
        max_chars_per_input: Optional[int] = None,
        max_tokens_per_input: Optional[int] = None,
        log_context: Optional[str] = None,
    ) -> List[List[float]]:
        if not texts:
            return []
        if max_chars_per_input is not None and max_chars_per_input > 0:
            texts = [t[:max_chars_per_input] for t in texts]
        if max_tokens_per_input is not None and max_tokens_per_input > 0:
            cap = min(max_tokens_per_input, 8192)
            min_chars_to_check = cap * 3
            truncated_count = 0
            new_texts = []
            for t in texts:
                if len(t) <= min_chars_to_check:
                    new_texts.append(t)
                elif self.token_count(t) > cap:
                    new_texts.append(self._truncate_to_max_tokens(t, cap))
                    truncated_count += 1
                else:
                    new_texts.append(t)
            texts = new_texts
            if truncated_count:
                logger.debug(
                    "Truncated %s texts to <=%s tokens for embed",
                    truncated_count,
                    cap,
                )
        n = len(texts)
        out: List[List[float]] = []
        num_batches = (n + batch_size - 1) // batch_size
        for i in range(0, n, batch_size):
            sub = texts[i : i + batch_size]
            batch_num = i // batch_size + 1
            ctx = (log_context + ", " if log_context else "") + "sub-batch %s/%s (%s texts)" % (
                batch_num,
                num_batches,
                len(sub),
            )
            logger.debug("Embed %s", ctx)
            try:
                vecs = self._sync_post(sub, "/embed")
            except httpx.HTTPStatusError as e:
                if e.response.status_code != 413:
                    raise
                if len(sub) > 1:
                    logger.warning(
                        "413 Payload Too Large for %s texts in %s; retrying one at a time",
                        len(sub),
                        ctx,
                    )
                    for j, one_text in enumerate(sub):
                        try:
                            v = self._embed_one(one_text)
                            out.append(v)
                        except httpx.HTTPStatusError as e2:
                            if e2.response.status_code == 413 and len(one_text) > 256:
                                truncated = one_text[: len(one_text) // 2]
                                logger.warning(
                                    "413 for single text (len=%s); retrying truncated to %s chars",
                                    len(one_text),
                                    len(truncated),
                                )
                                v = self._embed_one(truncated)
                                out.append(v)
                            else:
                                raise
                    continue
                if len(sub[0]) > 256:
                    truncated = sub[0][: len(sub[0]) // 2]
                    logger.warning(
                        "413 for single text (len=%s); retrying truncated to %s chars (%s)",
                        len(sub[0]),
                        len(truncated),
                        ctx,
                    )
                    v = self._embed_one(truncated)
                    out.append(v)
                else:
                    raise
                continue
            if not isinstance(vecs, list):
                raise ValueError("TEI /embed batch returned non-list")
            if len(vecs) != len(sub):
                raise ValueError(
                    "TEI /embed batch length mismatch: got %s vectors for %s texts (%s)"
                    % (len(vecs), len(sub), ctx)
                )
            for v in vecs:
                if not isinstance(v, list) or len(v) != EMBEDDING_DIM:
                    raise ValueError(
                        "TEI /embed returned invalid vector (dim %s, expected %s) in %s"
                        % (len(v) if isinstance(v, list) else "non-list", EMBEDDING_DIM, ctx)
                    )
                out.append(list(v))
        if len(out) != n:
            raise ValueError("Embed batch total length mismatch: got %s vectors for %s texts" % (len(out), n))
        return out
