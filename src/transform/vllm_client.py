import logging
from typing import List, Union

import httpx

logger = logging.getLogger(__name__)


def _vllm_base_url(v1_url: str) -> str:
    u = (v1_url or "").rstrip("/")
    if u.endswith("/v1"):
        return u[: -3].rstrip("/")
    return u


def tokenize_sync(base_url: str, text: str, timeout: float = 15.0) -> List[int]:
    if not base_url:
        raise RuntimeError("vLLM base URL is not set (VLLM_URL).")
    if not text:
        return []
    url = f"{base_url.rstrip('/')}/tokenize"
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(url, json={"prompt": text})
        resp.raise_for_status()
        data = resp.json()
    if isinstance(data, list) and data and isinstance(data[0], int):
        return data
    if isinstance(data, dict):
        ids = data.get("token_ids") or data.get("tokens") or data.get("ids")
        if isinstance(ids, list) and all(isinstance(x, int) for x in ids):
            return ids
        if isinstance(ids, list) and ids and isinstance(ids[0], list):
            return ids[0]
    raise ValueError("vLLM /tokenize returned unexpected response shape: %s" % type(data))


def token_count_sync(base_url: str, text: str, timeout: float = 15.0) -> int:
    return len(tokenize_sync(base_url, text, timeout=timeout))
