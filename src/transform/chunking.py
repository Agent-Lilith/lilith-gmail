import logging
import re
from dataclasses import dataclass

from core.embeddings import Embedder

logger = logging.getLogger(__name__)

CHUNK_TARGET_TOKENS = 7500


@dataclass
class Chunk:
    text: str
    position: int
    weight: float


def _split_into_paragraphs(text: str) -> list[str]:
    if not text.strip():
        return []
    text = re.sub(r"\r\n?", "\n", text)
    blocks = re.split(r"\n\s*\n", text)
    return [b.strip() for b in blocks if b.strip()]


def chunk_body(
    body: str,
    embedder: Embedder,
    max_tokens: int = 8192,
    target_chunk_tokens: int = CHUNK_TARGET_TOKENS,
) -> list[Chunk]:
    if not body or not body.strip():
        return []
    token_count = embedder.token_count(body)
    if token_count <= max_tokens:
        return []

    paragraphs = _split_into_paragraphs(body)
    if not paragraphs:
        # Fallback: split on sentence boundaries
        sentences = re.split(r"(?<=[.!?])\s+", body)
        paragraphs = [s.strip() for s in sentences if s.strip()] or [body]

    chunks: list[Chunk] = []
    current: list[str] = []
    current_tokens = 0
    position = 0

    for para in paragraphs:
        para_tokens = embedder.token_count(para)
        if para_tokens > target_chunk_tokens:
            # Long paragraph: split on sentence boundaries
            sentences = re.split(r"(?<=[.!?])\s+", para)
            for sent in sentences:
                sent = sent.strip()
                if not sent:
                    continue
                st = embedder.token_count(sent)
                if current_tokens + st > target_chunk_tokens and current:
                    chunk_text = "\n\n".join(current)
                    weight = 2.0 if position == 0 else 1.0
                    chunks.append(
                        Chunk(text=chunk_text, position=position, weight=weight)
                    )
                    position += 1
                    current = []
                    current_tokens = 0
                current.append(sent)
                current_tokens += st
            continue

        if current_tokens + para_tokens > target_chunk_tokens and current:
            chunk_text = "\n\n".join(current)
            weight = 2.0 if position == 0 else 1.0
            chunks.append(Chunk(text=chunk_text, position=position, weight=weight))
            position += 1
            current = []
            current_tokens = 0
        current.append(para)
        current_tokens += para_tokens

    if current:
        chunk_text = "\n\n".join(current)
        weight = 2.0 if position == 0 else 1.0
        chunks.append(Chunk(text=chunk_text, position=position, weight=weight))

    return chunks


def weighted_mean_embedding(
    embeddings: list[list[float]], weights: list[float]
) -> list[float]:
    if not embeddings or not weights or len(embeddings) != len(weights):
        return []
    dim = len(embeddings[0])
    total_weight = sum(weights)
    if total_weight == 0:
        return [0.0] * dim
    out = [0.0] * dim
    for emb, w in zip(embeddings, weights):
        for i in range(dim):
            out[i] += emb[i] * w
    for i in range(dim):
        out[i] /= total_weight
    return out
